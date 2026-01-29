#!/usr/bin/env python3
"""
启动服务器脚本 - 支持 iPad 访问
使用 aiohttp 同时提供 HTTP/HTTPS 服务器（前端文件）和 WebSocket/WSS 服务器
"""
import asyncio
import json
import logging
import os
import sys
import socket
import ssl
import aiohttp
from aiohttp import web
from aiohttp.web_runner import AppRunner, TCPSite

# 添加项目路径
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)
# 确保 backend 目录在路径中
backend_dir = os.path.join(project_root, 'backend')
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from backend.server import TranslationServer, load_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_local_ip():
    """获取本机局域网 IP 地址"""
    try:
        # 连接到一个远程地址来获取本机 IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


async def websocket_handler(request):
    """WebSocket 处理器"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    # 从URL参数获取客户端角色
    query_params = request.query
    client_role = query_params.get("role", "controller")  # 默认为控制端
    
    if client_role not in ["controller", "viewer"]:
        client_role = "controller"
    
    # 创建翻译服务器实例
    config = load_config()
    server = TranslationServer(config, client_role=client_role)
    
    # 创建适配器，让aiohttp的WebSocket看起来像websockets库的WebSocket
    class WebSocketAdapter:
        def __init__(self, ws):
            self.ws = ws
        
        @property
        def open(self):
            return not self.ws.closed
        
        @property
        def closed(self):
            return self.ws.closed
        
        @property
        def remote_address(self):
            """返回远程地址（用于日志）"""
            return getattr(self.ws, 'remote', 'unknown')
        
        async def send(self, data):
            if isinstance(data, str):
                await self.ws.send_str(data)
            else:
                await self.ws.send_bytes(data)
        
        async def send_str(self, data):
            """异步发送字符串消息"""
            await self.ws.send_str(data)
        
        async def send_bytes(self, data):
            """异步发送二进制消息"""
            await self.ws.send_bytes(data)
    
    server.client_websocket = WebSocketAdapter(ws)
    
    try:
        client_address = request.remote
        logger.info(f"客户端连接: {client_address}, 角色: {client_role}")
        
        # 处理客户端连接（根据角色不同处理）
        # 创建适配器并传入
        adapter = WebSocketAdapter(ws)
        server.client_websocket = adapter
        
        # 初始化连接（这里会创建 volcengine_client 并连接）
        try:
            logger.info(f"开始初始化客户端连接，角色: {client_role}, WebSocket 状态: closed={ws.closed}")
            
            # iOS Safari 特殊处理：等待 WebSocket 连接完全建立
            # iOS Safari 的 WebSocket 连接可能需要额外时间才能稳定
            if client_role == "viewer":
                # 等待一小段时间，确保 WebSocket 连接完全建立
                await asyncio.sleep(0.2)  # 延迟 200ms
                # 再次检查连接状态
                if ws.closed:
                    logger.warning("WebSocket 连接在延迟期间已关闭")
                    return ws
            
            await server.handle_client(adapter, client_role=client_role)
            logger.info(f"客户端连接初始化成功，角色: {client_role}, WebSocket 状态: closed={ws.closed}")
        except Exception as e:
            logger.error(f"初始化连接失败: {e}", exc_info=True)
            logger.error(f"异常类型: {type(e).__name__}, 异常消息: {str(e)}")
            # 如果连接失败，不继续处理消息
            if not ws.closed:
                try:
                    await ws.send_json({
                        "type": "error",
                        "message": f"初始化失败: {str(e)}"
                    })
                except Exception as send_error:
                    logger.error(f"发送错误消息失败: {send_error}")
            return ws
        
        # 接收客户端消息（消息循环）
        # 只有连接成功后才进入消息循环
        logger.info(f"开始消息循环，角色: {client_role}")
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await server._handle_client_message(msg.data)
                elif msg.type == web.WSMsgType.BINARY:
                    # 二进制消息（音频数据）
                    await server._handle_client_message(msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f'WebSocket 错误: {ws.exception()}')
                    break
                elif msg.type == web.WSMsgType.CLOSE:
                    logger.info('WebSocket 连接关闭')
                    break
        except asyncio.CancelledError:
            logger.info("消息循环被取消")
            raise
        except Exception as e:
            logger.error(f"消息循环错误: {e}", exc_info=True)
        finally:
            # 连接关闭时清理资源
            logger.info(f"开始清理资源，角色: {client_role}")
            await server.cleanup(client_role)
                
    except Exception as e:
        logger.error(f"处理客户端连接时出错: {e}", exc_info=True)
        try:
            if not ws.closed:
                await ws.send_json({
                    "type": "error",
                    "message": str(e)
                })
        except:
            pass
    finally:
        logger.info(f"客户端断开连接: {client_address}")
    
    return ws


async def start_server(port=15677, use_https=False):
    """启动服务器（同时处理 HTTP 和 WebSocket）"""
    app = web.Application()

    # 读取配置（用于展示/生成访问地址等；不影响实际绑定 0.0.0.0）
    try:
        config = load_config()
    except Exception:
        config = {}
    server_config = (config or {}).get("server", {}) if isinstance(config, dict) else {}
    advertise_ip = server_config.get("advertise_ip")
    
    # 静态文件服务（前端文件）
    frontend_dir = os.path.join(os.path.dirname(__file__), 'frontend')
    
    # 处理 favicon.ico 请求（避免 404 错误）
    async def favicon_handler(request):
        return web.Response(status=204)  # No Content

    app.router.add_get('/favicon.ico', favicon_handler)

    # WebSocket 路由（必须在静态文件路由之前，以确保优先匹配）
    app.router.add_get('/ws', websocket_handler)

    # 根路径返回 index.html（控制端）
    async def index_handler(request):
        index_path = os.path.join(frontend_dir, 'index.html')
        return web.FileResponse(index_path)
    
    app.router.add_get('/', index_handler)
    
    # /viewer 路径返回 viewer.html（查看端）
    async def viewer_handler(request):
        viewer_path = os.path.join(frontend_dir, 'viewer.html')
        if os.path.exists(viewer_path):
            return web.FileResponse(viewer_path)
        else:
            return web.Response(text="查看端页面未找到", status=404)
    
    app.router.add_get('/viewer', viewer_handler)
    
    # 静态文件（CSS、JS等）- 使用通配符匹配所有文件
    async def static_handler(request):
        filename = request.match_info['filename']
        file_path = os.path.join(frontend_dir, filename)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return web.FileResponse(file_path)
        else:
            return web.Response(status=404)
    
    app.router.add_get('/{filename}', static_handler)
    
    # 配置 SSL
    ssl_context = None
    if use_https:
        cert_file = os.path.join(os.path.dirname(__file__), 'ssl', 'cert.pem')
        key_file = os.path.join(os.path.dirname(__file__), 'ssl', 'key.pem')
        
        if not os.path.exists(cert_file) or not os.path.exists(key_file):
            logger.error("SSL 证书文件不存在！")
            logger.info("请先运行: ./generate_cert.sh")
            raise FileNotFoundError("SSL 证书文件不存在")
        
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(cert_file, key_file)
        
    # 检查端口是否被占用
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('0.0.0.0', port))
        sock.close()
    except OSError as e:
        if e.errno == 48:  # Address already in use
            logger.error(f"端口 {port} 已被占用！")
            logger.info("请使用以下命令关闭占用端口的进程：")
            logger.info(f"  ./kill_port.sh {port}")
            sys.exit(1)
        else:
            raise
        
    # 启动服务器
    runner = AppRunner(app)
    await runner.setup()
    
    protocol = "https" if use_https else "http"
    site = TCPSite(runner, '0.0.0.0', port, ssl_context=ssl_context)
    await site.start()
    
    logger.info("=" * 60)
    logger.info("同声传译实时翻译系统")
    logger.info("=" * 60)
    logger.info(f"服务器启动: {protocol}://0.0.0.0:{port}")
    logger.info(f"本地访问: {protocol}://localhost:{port}")
    local_ip = advertise_ip.strip() if isinstance(advertise_ip, str) and advertise_ip.strip() else get_local_ip()
    logger.info(f"局域网访问: {protocol}://{local_ip}:{port}")
    logger.info(f"WebSocket 地址: {protocol.replace('http', 'ws')}://{local_ip}:{port}/ws")
    if use_https:
        logger.info(f"iPad/手机访问: 在设备浏览器中输入 {protocol}://{local_ip}:{port}")
        logger.info("注意：首次访问会显示安全警告，点击'高级' -> '继续访问'")
    else:
        logger.warning("注意：移动设备需要 HTTPS 才能使用麦克风功能")
        logger.info("使用 --https 参数启动 HTTPS 服务器")
    logger.info("=" * 60)
    
    try:
        await asyncio.Future()  # 永久运行
    except KeyboardInterrupt:
        logger.info("服务器已停止")
    finally:
        await runner.cleanup()


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='同声传译实时翻译系统服务器')
    parser.add_argument('--https', action='store_true', help='使用 HTTPS 服务器（支持移动设备麦克风）')
    parser.add_argument('--port', type=int, default=15677, help='服务器端口（默认: 15677）')
    args = parser.parse_args()
    
    try:
        asyncio.run(start_server(port=args.port, use_https=args.https))
    except KeyboardInterrupt:
        logger.info("服务器已停止")


if __name__ == "__main__":
    main()
