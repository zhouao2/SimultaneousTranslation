"""
同声传译 WebSocket 服务器
"""
import asyncio
import json
import logging
import websockets
from websockets.server import serve
from typing import Optional, Dict, List
import sys
import os
import uuid
import inspect

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.volcengine_client import VolcengineASTClient
from backend.audio_processor import validate_audio_format
from backend.text_filter import filter_text
from backend.text_corrector import correct_text, get_corrector

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TranslationSession:
    """翻译会话管理器（全局单例）"""
    
    _instance = None
    _lock = None
    
    @classmethod
    def _ensure_lock(cls):
        """确保锁已初始化"""
        if cls._lock is None:
            cls._lock = asyncio.Lock()
    
    def __init__(self, config: dict):
        """
        初始化会话管理器
        
        Args:
            config: 配置字典
        """
        self.config = config
        self.volcengine_client: Optional[VolcengineASTClient] = None
        self.controller_websocket = None  # 控制端连接
        self.viewer_websockets: Dict[str, any] = {}  # 查看端连接字典 {client_id: websocket}
        self.current_source_text = ""  # 当前原文
        self.current_target_text = ""  # 当前译文
        self.completed_source_lines: List[str] = []  # 已完成的原文行
        self.completed_target_lines: List[str] = []  # 已完成的译文行
        self.max_history_lines = 8  # 最大历史记录数
        
    @classmethod
    async def get_instance(cls, config: dict):
        """获取单例实例"""
        cls._ensure_lock()
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls(config)
            return cls._instance
    
    async def add_controller(self, websocket, volcengine_client: VolcengineASTClient):
        """添加控制端连接"""
        if self.controller_websocket is not None:
            logger.warning("已有控制端连接，关闭旧连接")
            try:
                if hasattr(self.controller_websocket, 'close'):
                    await self.controller_websocket.close()
            except:
                pass
        
        self.controller_websocket = websocket
        self.volcengine_client = volcengine_client
        logger.info("控制端已连接")
    
    async def remove_controller(self):
        """移除控制端连接"""
        self.controller_websocket = None
        logger.info("控制端已断开")
    
    async def add_viewer(self, websocket) -> str:
        """添加查看端连接，返回客户端ID"""
        client_id = str(uuid.uuid4())
        self.viewer_websockets[client_id] = websocket
        logger.info(f"查看端已连接，客户端ID: {client_id}，当前查看端数量: {len(self.viewer_websockets)}")
        return client_id
    
    async def remove_viewer(self, client_id: str):
        """移除查看端连接"""
        if client_id in self.viewer_websockets:
            del self.viewer_websockets[client_id]
            logger.info(f"查看端已断开，客户端ID: {client_id}，当前查看端数量: {len(self.viewer_websockets)}")
    
    async def broadcast_to_viewers(self, message: dict):
        """向所有查看端广播消息"""
        if not self.viewer_websockets:
            return
        
        disconnected_clients = []
        for client_id, ws in self.viewer_websockets.items():
            try:
                # 检查连接是否打开
                if hasattr(ws, 'closed') and ws.closed:
                    disconnected_clients.append(client_id)
                    continue
                if hasattr(ws, 'open') and not ws.open:
                    disconnected_clients.append(client_id)
                    continue
                
                # 发送消息
                if hasattr(ws, 'send_str'):
                    # send_str 是异步方法（WebSocketAdapter 中定义的）
                    await ws.send_str(json.dumps(message))
                elif hasattr(ws, 'send'):
                    await ws.send(json.dumps(message))
                else:
                    logger.warning(f"查看端 {client_id} 的 WebSocket 对象不支持发送消息")
            except Exception as e:
                logger.error(f"向查看端 {client_id} 发送消息失败: {e}")
                disconnected_clients.append(client_id)
        
        # 清理断开的连接
        for client_id in disconnected_clients:
            await self.remove_viewer(client_id)
    
    async def send_state_to_viewer(self, websocket):
        """向新连接的查看端发送当前状态"""
        state_message = {
            "type": "state_sync",
            "source_text": self.current_source_text,
            "target_text": self.current_target_text,
            "completed_source_lines": self.completed_source_lines[-self.max_history_lines:],
            "completed_target_lines": self.completed_target_lines[-self.max_history_lines:]
        }
        
        try:
            if hasattr(websocket, 'send_str'):
                # send_str 是异步方法（WebSocketAdapter 中定义的）
                await websocket.send_str(json.dumps(state_message))
            elif hasattr(websocket, 'send'):
                await websocket.send(json.dumps(state_message))
            else:
                logger.warning("WebSocket 对象不支持发送消息")
        except Exception as e:
            logger.error(f"发送状态同步消息失败: {e}", exc_info=True)
            # 不抛出异常，避免影响连接建立


class TranslationServer:
    """翻译服务器"""
    
    def __init__(self, config: dict, client_role: str = "controller",
                 access_db=None, code_id: Optional[int] = None):
        """
        初始化服务器

        Args:
            config: 配置字典
            client_role: 客户端角色 ("controller" 或 "viewer")
            access_db: 访问控制数据库（可选，用于用量计量与额度管控）
            code_id: 当前连接绑定的访问码 ID（可选）
        """
        self.config = config
        self.client_role = client_role
        self.volcengine_client: Optional[VolcengineASTClient] = None
        self.client_websocket = None
        self.session: Optional[TranslationSession] = None
        self.viewer_client_id = None
        self.access_db = access_db
        self.code_id = code_id
        self.db_session_id = None  # 访问计量会话记录 ID
        
    async def handle_client(self, websocket, client_role: str = "controller"):
        """
        处理客户端连接（初始化阶段）
        
        Args:
            websocket: 客户端 WebSocket 连接
            client_role: 客户端角色 ("controller" 或 "viewer")
        """
        self.client_role = client_role
        self.client_websocket = websocket
        
        # 获取全局会话实例
        self.session = await TranslationSession.get_instance(self.config)
        
        try:
            client_address = websocket.remote_address if hasattr(websocket, 'remote_address') else "unknown"
            logger.info(f"客户端连接: {client_address}, 角色: {client_role}")
            
            if client_role == "controller":
                # 控制端：初始化火山引擎连接
                await self._handle_controller_connection(websocket)
            else:
                # 查看端：只接收广播消息
                await self._handle_viewer_connection(websocket)
                
        except Exception as e:
            logger.error(f"处理客户端连接时出错: {e}", exc_info=True)
            try:
                await self._send_to_client({
                    "type": "error",
                    "message": str(e)
                })
            except:
                pass
            raise  # 重新抛出异常，让上层处理
    
    async def cleanup(self, client_role: str):
        """
        清理资源（在连接真正关闭时调用）
        
        Args:
            client_role: 客户端角色
        """
        logger.info(f"开始清理资源，角色: {client_role}")
        if client_role == "controller":
            # 结束计量会话（墙钟时长已入库，供用量报表使用）
            if self.access_db and self.db_session_id:
                try:
                    self.access_db.end_session(self.db_session_id)
                    self.access_db.audit(f"code:{self.code_id}", "session_end",
                                         f"计量会话 #{self.db_session_id} 结束")
                except Exception as e:
                    logger.error(f"结束计量会话失败: {e}")
                self.db_session_id = None
            if self.session:
                await self.session.remove_controller()
            if self.volcengine_client:
                try:
                    await self.volcengine_client.close()
                except:
                    pass
        else:
            if self.viewer_client_id and self.session:
                await self.session.remove_viewer(self.viewer_client_id)
        self.client_websocket = None
        logger.info(f"资源清理完成，角色: {client_role}")
    
    async def _handle_controller_connection(self, websocket):
        """处理控制端连接"""
        # 初始化火山引擎客户端
        volc_config = self.config["volcengine"]
        translation_config = self.config.get("translation", {})
        tts_config = self.config.get("tts", {})
        
        self.volcengine_client = VolcengineASTClient(
            api_key=volc_config["api_key"],
            resource_id=volc_config.get("resource_id", "volc.service_type.10053"),
            source_lang=translation_config.get("source_language", "zh"),
            target_lang=translation_config.get("target_language", "en"),
            tts_speaker_id=tts_config.get("speaker_id", ""),
            speech_rate=tts_config.get("speech_rate", 0)
        )
        
        # 设置消息回调
        self.volcengine_client.on_message = self._handle_volcengine_message
        self.volcengine_client.on_error = self._handle_volcengine_error
        
        # 连接到火山引擎
        logger.info("正在连接到火山引擎 API...")
        try:
            await self.volcengine_client.connect()
            logger.info("已连接到火山引擎 API")
            
            # 验证连接状态
            if not self.volcengine_client.connected:
                raise Exception("火山引擎客户端连接失败：connected 状态为 False")
            
            # 发送开始会话请求
            logger.info("发送 StartSession 请求...")
            await self.volcengine_client.send_start_session()
            logger.info("StartSession 请求已发送，等待响应...")
            
            # 注册到会话管理器
            await self.session.add_controller(websocket, self.volcengine_client)
            
            # 通知前端连接成功
            await self._send_to_client({
                "type": "connected",
                "message": "已连接到翻译服务"
            })
            logger.info("已通知前端连接成功")

            # 访问计量：登记会话（仅统计用量，不做额度限制）
            if self.access_db and self.code_id:
                try:
                    self.db_session_id = self.access_db.start_session(self.code_id)
                    self.access_db.audit(f"code:{self.code_id}", "session_start",
                                         f"计量会话 #{self.db_session_id} 开始")
                except Exception as e:
                    logger.error(f"访问计量初始化失败（不影响翻译功能）: {e}")
        except Exception as e:
            logger.error(f"连接火山引擎失败: {e}", exc_info=True)
            # 清理失败的连接
            if self.volcengine_client:
                try:
                    await self.volcengine_client.close()
                except:
                    pass
                self.volcengine_client = None
            
            # 通知前端连接失败
            await self._send_to_client({
                "type": "error",
                "message": f"连接翻译服务失败: {str(e)}"
            })
            raise  # 重新抛出异常，让上层处理
    
    async def _handle_viewer_connection(self, websocket):
        """处理查看端连接"""
        # 注册到会话管理器（这是关键步骤，如果失败应该抛出异常）
        try:
            logger.debug(f"开始注册查看端，websocket 类型: {type(websocket)}, 是否有 send_str: {hasattr(websocket, 'send_str')}")
            self.viewer_client_id = await self.session.add_viewer(websocket)
            logger.info(f"查看端已注册，客户端ID: {self.viewer_client_id}")
        except Exception as e:
            logger.error(f"注册查看端失败: {e}", exc_info=True)
            raise  # 注册失败应该抛出异常
        
        # iOS Safari 特殊处理：延迟发送消息，确保连接完全建立
        # iOS Safari 在 WebSocket 连接刚建立时可能无法立即接收消息
        import asyncio
        await asyncio.sleep(0.1)  # 延迟 100ms，确保连接稳定
        
        # 发送当前状态（失败不应该影响连接建立）
        try:
            await self.session.send_state_to_viewer(websocket)
            logger.debug("已发送状态同步消息")
        except Exception as e:
            logger.error(f"发送状态同步消息失败: {e}", exc_info=True)
            # 不抛出异常，避免影响连接建立
        
        # 再次延迟，确保第一条消息已发送
        await asyncio.sleep(0.05)  # 延迟 50ms
        
        # 通知连接成功（失败不应该影响连接建立）
        try:
            await self._send_to_client({
                "type": "connected",
                "message": "已连接到查看端服务"
            })
            logger.debug("已发送连接成功消息")
        except Exception as e:
            logger.error(f"发送连接成功消息失败: {e}", exc_info=True)
            # 不抛出异常，避免影响连接建立（消息发送失败不应该导致连接失败）
        
        # 查看端的消息循环在 start_server.py 中处理
        # 这里只处理查看端的控制消息（如心跳）
    
    async def _handle_client_message(self, message):
        """
        处理来自客户端的消息
        
        Args:
            message: 客户端消息
        """
        try:
            if isinstance(message, bytes):
                # 音频数据（二进制）
                if not self.volcengine_client:
                    logger.warning("收到音频数据但 volcengine_client 未初始化（可能是查看端或连接失败）")
                    logger.debug(f"客户端角色: {self.client_role}, volcengine_client: {self.volcengine_client}")
                    return
                
                if not self.volcengine_client.connected:
                    logger.warning("收到音频数据但火山引擎客户端未连接，尝试重新连接...")
                    logger.debug(f"volcengine_client.connected: {self.volcengine_client.connected}")
                    logger.debug(f"volcengine_client.websocket: {self.volcengine_client.websocket}")
                    # 尝试重新连接
                    try:
                        logger.info("开始重新连接火山引擎...")
                        await self.volcengine_client.connect()
                        if self.volcengine_client.connected:
                            logger.info("重新连接成功，发送 StartSession...")
                            await self.volcengine_client.send_start_session()
                        else:
                            logger.error("重新连接失败：connected 仍为 False")
                            return
                    except Exception as e:
                        logger.error(f"重新连接失败: {e}", exc_info=True)
                        return
                
                # 检查会话是否已启动
                if not self.volcengine_client.session_id:
                    logger.warning("收到音频数据但会话未启动，尝试启动会话...")
                    await self.volcengine_client.send_start_session()
                
                # 记录音频数据接收（每 100 次记录一次，避免日志过多）
                if not hasattr(self, '_audio_receive_count'):
                    self._audio_receive_count = 0
                self._audio_receive_count += 1
                if self._audio_receive_count == 1:
                    logger.info(f"✓ 开始接收音频数据，第一个包大小: {len(message)} 字节")
                if self._audio_receive_count % 100 == 0:
                    logger.info(f"已接收 {self._audio_receive_count} 个音频包，当前包大小: {len(message)} 字节")
                
                await self.volcengine_client.send_audio(message)
            elif isinstance(message, str):
                # JSON 消息
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning(f"无法解析 JSON 消息: {message[:100]}")
                    return
                
                msg_type = data.get("type")
                
                # 查看端的心跳消息
                if self.client_role == "viewer" and msg_type == "ping":
                    await self._send_to_client({"type": "pong"})
                    return
                
                # 控制端的消息处理
                if self.client_role != "controller":
                    return  # 查看端不处理其他消息
                
                if msg_type == "audio":
                    # 音频数据（base64 编码）
                    import base64
                    audio_data = base64.b64decode(data.get("data", ""))
                    if self.volcengine_client and self.volcengine_client.connected:
                        await self.volcengine_client.send_audio(audio_data)
                elif msg_type == "start":
                    # 开始翻译
                    if self.volcengine_client and not self.volcengine_client.connected:
                        await self.volcengine_client.connect()
                        await self.volcengine_client.send_start_session()
                elif msg_type == "stop":
                    # 停止翻译
                    logger.info("收到停止翻译请求")
                    if self.volcengine_client:
                        await self.volcengine_client.close()
                    
                    # 清除会话状态
                    if self.session:
                        self.session.current_source_text = ""
                        self.session.current_target_text = ""
                        self.session.completed_source_lines = []
                        self.session.completed_target_lines = []
                        logger.info("已清除会话状态")
                    
                    # 广播停止消息给所有查看端
                    if self.session:
                        await self.session.broadcast_to_viewers({
                            "type": "translation_stopped",
                            "message": "翻译已停止"
                        })
                        logger.info("已广播停止消息给所有查看端")
                elif msg_type == "update_language":
                    # 更新翻译语言
                    source_lang = data.get("source_language", "zh")
                    target_lang = data.get("target_language", "en")
                    logger.info(f"收到语言更新请求: source_language={source_lang}, target_language={target_lang}")

                    # 校验：S2S 模式要求源语言或目标语言至少一个为中文/英语
                    if source_lang not in ("zh", "en") and target_lang not in ("zh", "en"):
                        logger.warning(f"无效的语言组合: {source_lang} -> {target_lang}（必须有一方为 zh/en）")
                        await self._send_to_client({
                            "type": "error",
                            "message": f"无效的语言组合: {source_lang} -> {target_lang}，源语言和目标语言中至少需要一个是中文或英语"
                        })
                        return

                    # 更新配置，保证后续重连也使用新语言
                    self.config.setdefault("translation", {})
                    self.config["translation"]["source_language"] = source_lang
                    self.config["translation"]["target_language"] = target_lang
                    
                    # 如果客户端已连接，需要重新启动会话以应用新语言
                    if self.volcengine_client and self.volcengine_client.connected:
                        # 更新客户端的语言设置
                        self.volcengine_client.source_lang = source_lang
                        self.volcengine_client.target_lang = target_lang
                        logger.info(f"已更新客户端语言设置为: {source_lang} -> {target_lang}")
                        
                        # 如果会话已启动，需要重新启动会话以应用新语言
                        if self.volcengine_client.session_id:
                            logger.info("会话已启动，重新启动会话以应用新语言...")
                            await self.volcengine_client.close()
                            await asyncio.sleep(0.5)  # 等待连接完全关闭
                            await self.volcengine_client.connect()
                            await self.volcengine_client.send_start_session()
                            logger.info("会话已重新启动，新语言已应用")
                            
                            # 通知前端语言已更新
                            await self._send_to_client({
                                "type": "language_updated",
                                "message": f"语言已更新为: {source_lang} -> {target_lang}"
                            })
                        else:
                            # 会话未启动，直接更新配置即可
                            logger.info("会话未启动，语言设置将在下次启动会话时应用")
                            await self._send_to_client({
                                "type": "language_updated",
                                "message": f"语言设置已保存: {source_lang} -> {target_lang}"
                            })
                    else:
                        # 客户端未连接，配置已更新，下次连接时生效
                        logger.info("客户端未连接，语言设置已保存到配置，将在连接时应用")
                        await self._send_to_client({
                            "type": "language_updated",
                            "message": f"语言设置已保存: {source_lang} -> {target_lang}"
                        })
                elif msg_type == "update_voice":
                    # 更新TTS音色
                    speaker_id = data.get("speaker_id", "")
                    logger.info(f"收到音色更新请求: speaker_id={speaker_id}")
                    
                    # 如果客户端已连接，需要重新启动会话以应用新音色
                    if self.volcengine_client and self.volcengine_client.connected:
                        # 更新客户端的音色设置
                        self.volcengine_client.tts_speaker_id = speaker_id
                        logger.info(f"已更新客户端音色设置为: {speaker_id or '默认音色'}")
                        
                        # 如果会话已启动，需要重新启动会话以应用新音色
                        if self.volcengine_client.session_id:
                            logger.info("会话已启动，重新启动会话以应用新音色...")
                            await self.volcengine_client.close()
                            await asyncio.sleep(0.5)  # 等待连接完全关闭
                            await self.volcengine_client.connect()
                            await self.volcengine_client.send_start_session()
                            logger.info("会话已重新启动，新音色已应用")
                            
                            # 通知前端音色已更新
                            await self._send_to_client({
                                "type": "voice_updated",
                                "message": f"音色已更新为: {speaker_id or '默认音色'}"
                            })
                        else:
                            # 会话未启动，直接更新配置即可
                            logger.info("会话未启动，音色设置将在下次启动会话时应用")
                            await self._send_to_client({
                                "type": "voice_updated",
                                "message": f"音色设置已保存: {speaker_id or '默认音色'}"
                            })
                    else:
                        # 客户端未连接，更新配置中的音色设置
                        tts_config = self.config.get("tts", {})
                        tts_config["speaker_id"] = speaker_id
                        logger.info(f"客户端未连接，已更新配置中的音色设置为: {speaker_id or '默认音色'}")
                        await self._send_to_client({
                            "type": "voice_updated",
                            "message": f"音色设置已保存: {speaker_id or '默认音色'}"
                        })
        except Exception as e:
            logger.error(f"处理客户端消息时出错: {e}", exc_info=True)
    
    async def _handle_volcengine_message(self, data: dict):
        """
        处理来自火山引擎的消息
        
        Args:
            data: 火山引擎响应数据
        """
        try:
            event = data.get("event")
            text = data.get("text", "")

            # 访问计量：记录 UsageResponse (154) 的 token 消耗
            if event == 154 and self.access_db and self.db_session_id and self.code_id:
                try:
                    self._record_usage_event(data)
                except Exception as e:
                    logger.error(f"记录用量失败（不影响翻译功能）: {e}")

            # 更新会话状态（先纠正，再过滤，最后缓存）
            if self.session:
                if event == 651:  # SourceSubtitleResponse
                    if text:
                        corrected_text = correct_text(text)
                        filtered_text = filter_text(corrected_text, replacement="***")
                        self.session.current_source_text = filtered_text
                    else:
                        self.session.current_source_text = ""
                elif event == 652:  # SourceSubtitleEnd
                    if text:
                        corrected_text = correct_text(text)
                        filtered_text = filter_text(corrected_text, replacement="***")
                        self.session.completed_source_lines.append(filtered_text)
                        if len(self.session.completed_source_lines) > self.session.max_history_lines:
                            self.session.completed_source_lines.pop(0)
                        self.session.current_source_text = ""
                elif event == 654:  # TranslationSubtitleResponse
                    if text:
                        corrected_text = correct_text(text)
                        filtered_text = filter_text(corrected_text, replacement="***")
                        self.session.current_target_text = filtered_text
                    else:
                        self.session.current_target_text = ""
                elif event == 655:  # TranslationSubtitleEnd
                    if text:
                        corrected_text = correct_text(text)
                        filtered_text = filter_text(corrected_text, replacement="***")
                        self.session.completed_target_lines.append(filtered_text)
                        if len(self.session.completed_target_lines) > self.session.max_history_lines:
                            self.session.completed_target_lines.pop(0)
                        self.session.current_target_text = ""
            
            # 记录所有重要事件的调用
            if event in [350, 351, 352, 650, 651, 652, 653, 654, 655]:
                logger.info(f"📥 _handle_volcengine_message 被调用，event={event}, text='{text[:50] if text else ''}'")
            
            # 调试：检查接收到的数据
            if event in [350, 351, 352]:  # TTS 相关事件
                event_name = {
                    350: "TTSSentenceStart",
                    351: "TTSSentenceEnd",
                    352: "TTSResponse"
                }.get(event, f"Unknown({event})")
                audio_size = data.get('data_length', 0)
                logger.info(f"🔊 转发 TTS 数据到前端: {event_name}, 音频大小: {audio_size} 字节")
                logger.debug(f"完整 data 对象: {data}")
            elif event in [650, 651, 652, 653, 654, 655]:  # 原文/译文相关事件
                event_name = {
                    650: "SourceSubtitleStart",
                    651: "SourceSubtitleResponse",
                    652: "SourceSubtitleEnd",
                    653: "TranslationSubtitleStart",
                    654: "TranslationSubtitleResponse",
                    655: "TranslationSubtitleEnd"
                }.get(event, f"Unknown({event})")
                logger.info(f"📤 转发翻译数据到前端: {event_name}, text='{text}' (类型: {type(text).__name__}, 长度: {len(text) if text else 0})")
                logger.debug(f"完整 data 对象: {data}")
            
            # 确保 text 字段存在且为字符串
            if "text" not in data:
                logger.warning(f"数据中缺少 'text' 字段: {data}")
                data["text"] = ""
            elif data["text"] is None:
                logger.warning(f"数据中 'text' 字段为 None: {data}")
                data["text"] = ""
            
            # 先纠正文本，再过滤敏感词
            if data.get("text"):
                original_text = data["text"]
                # 1. 纠正识别错误
                corrected_text = correct_text(original_text)
                # 2. 过滤敏感词
                filtered_text = filter_text(corrected_text, replacement="***")
                if corrected_text != original_text:
                    logger.debug(f"文本已纠正，原始: '{original_text[:50]}...', 纠正后: '{corrected_text[:50]}...'")
                if filtered_text != corrected_text:
                    logger.debug(f"文本已过滤敏感词，原始长度: {len(corrected_text)}, 过滤后长度: {len(filtered_text)}")
                data["text"] = filtered_text
            
            # 转发给控制端
            message_to_send = {
                "type": "translation",
                "data": data
            }
            
            # 调试：检查要发送的消息
            if event in [650, 651, 652, 653, 654, 655]:
                logger.debug(f"要发送的消息中 text 值: '{message_to_send['data'].get('text', 'MISSING')}'")
            
            await self._send_to_client(message_to_send)
            
            # 广播给所有查看端
            if self.session:
                await self.session.broadcast_to_viewers(message_to_send)
            
            logger.debug(f"已发送消息到客户端并广播给查看端: event={event}")
        except Exception as e:
            logger.error(f"转发火山引擎消息时出错: {e}", exc_info=True)
    
    async def _handle_volcengine_error(self, error: Exception):
        """
        处理火山引擎错误

        Args:
            error: 错误对象
        """
        logger.error(f"火山引擎错误: {error}")
        await self._send_to_client({
            "type": "error",
            "message": f"翻译服务错误: {str(error)}"
        })

    def _record_usage_event(self, data: dict):
        """解析 UsageResponse 的计量数据并落库"""
        usage = data.get("usage") or {}
        # MessageToDict 输出驼峰键；兼容蛇形命名
        meta = usage.get("responseMeta") or usage.get("response_meta") or {}
        billing = meta.get("billing") or {}
        duration_msec = int(float(billing.get("durationMsec") or billing.get("duration_msec") or 0))
        tokens = {"input_audio_tokens": 0.0, "output_audio_tokens": 0.0, "output_text_tokens": 0.0}
        for item in billing.get("items", []):
            unit = item.get("unit", "")
            if unit in tokens:
                tokens[unit] += float(item.get("quantity") or 0)
        self.access_db.record_usage(
            self.db_session_id, self.code_id, duration_msec,
            tokens["input_audio_tokens"], tokens["output_audio_tokens"], tokens["output_text_tokens"])
        logger.info(f"📊 用量已记录: 音频 {duration_msec}ms, "
                    f"输入音频 {tokens['input_audio_tokens']}, "
                    f"输出音频 {tokens['output_audio_tokens']}, "
                    f"输出文本 {tokens['output_text_tokens']} tokens")

    async def _send_to_client(self, message: dict):
        """
        发送消息给客户端
        
        Args:
            message: 消息字典
        """
        if self.client_websocket:
            try:
                # 检查连接状态
                if hasattr(self.client_websocket, 'closed') and self.client_websocket.closed:
                    logger.warning("客户端 WebSocket 已关闭，无法发送消息")
                    return
                if hasattr(self.client_websocket, 'open') and not self.client_websocket.open:
                    logger.warning("客户端 WebSocket 未打开，无法发送消息")
                    return
                
                # 发送消息
                if hasattr(self.client_websocket, 'send_str'):
                    # send_str 是异步方法（WebSocketAdapter 中定义的）
                    await self.client_websocket.send_str(json.dumps(message))
                elif hasattr(self.client_websocket, 'send'):
                    await self.client_websocket.send(json.dumps(message))
                else:
                    logger.warning("客户端 WebSocket 对象不支持发送消息")
                
                logger.debug(f"✓ 消息已发送到客户端: {message.get('type', 'unknown')}")
            except Exception as e:
                logger.error(f"发送消息给客户端失败: {e}", exc_info=True)
        else:
            logger.warning("客户端 WebSocket 不存在，无法发送消息")


def _init_text_corrector():
    """初始化文本纠正器"""
    try:
        # 查找纠正词库文件
        config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
        correction_file = os.path.join(config_dir, 'corrections.json')
        
        # 如果文件存在，加载它
        if os.path.exists(correction_file):
            get_corrector(correction_file=correction_file)
            logger.info(f"已加载纠正词库: {correction_file}")
        else:
            # 创建默认的纠正词库文件
            default_corrections = {
                "静莹": "静音",
                # 用户可以在这里添加更多纠正规则
            }
            corrector = get_corrector(correction_dict=default_corrections)
            # 保存到文件
            corrector.save_to_file(correction_file)
            logger.info(f"已创建默认纠正词库: {correction_file}")
    except Exception as e:
        logger.error(f"初始化文本纠正器失败: {e}", exc_info=True)


def load_config():
    """加载配置文件"""
    # 初始化文本纠正器
    _init_text_corrector()
    
    # 加载主配置文件
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "config.json"
    )
    
    if not os.path.exists(config_path):
        logger.error(f"配置文件不存在: {config_path}")
        logger.info("请复制 config/config.example.json 为 config/config.json 并填入配置")
        sys.exit(1)
    
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


async def main():
    """主函数"""
    # 加载配置和纠正词库
    config = load_config()
    
    # 创建服务器
    server = TranslationServer(config)
    
    # 获取服务器配置
    server_config = config["server"]
    host = server_config.get("host", "localhost")
    port = server_config.get("port", 8765)
    
    logger.info(f"启动服务器: ws://{host}:{port}")
    
    # 检查端口是否被占用
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.close()
    except OSError as e:
        if e.errno == 48:  # Address already in use
            logger.error(f"端口 {port} 已被占用！")
            logger.info("请执行以下命令查找并关闭占用端口的进程：")
            logger.info(f"  lsof -i :{port}")
            logger.info("或者修改 config/config.json 中的端口号")
            sys.exit(1)
        else:
            raise
    
    # 启动 WebSocket 服务器
    try:
        async with serve(server.handle_client, host, port):
            logger.info(f"服务器运行中，等待客户端连接...")
            await asyncio.Future()  # 永久运行
    except OSError as e:
        if e.errno == 48:
            logger.error(f"端口 {port} 已被占用！")
            logger.info("请关闭占用该端口的其他进程，或修改配置文件中的端口号")
            sys.exit(1)
        else:
            raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("服务器已停止")

