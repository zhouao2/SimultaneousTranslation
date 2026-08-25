"""
邮件发送（SMTP）。发送在线程池执行，失败不阻塞审批流程。
"""
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import datetime

logger = logging.getLogger(__name__)


class Mailer:
    def __init__(self, smtp_config: dict):
        self.enabled = bool(smtp_config.get("enabled"))
        self.host = smtp_config.get("host", "")
        self.port = int(smtp_config.get("port", 465))
        self.username = smtp_config.get("username", "")
        self.password = smtp_config.get("password", "")
        self.use_tls = bool(smtp_config.get("use_tls", True))
        self.from_addr = smtp_config.get("from_addr", self.username)

    def _send_sync(self, to_addr: str, subject: str, body: str):
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(("同声传译系统", self.from_addr))
        msg["To"] = to_addr

        if self.use_tls and self.port == 465:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=15)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=15)
        try:
            if self.use_tls and self.port != 465:
                server.starttls()
            if self.username:
                server.login(self.username, self.password)
            server.sendmail(self.from_addr, [to_addr], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass

    async def send(self, to_addr: str, subject: str, body: str) -> bool:
        """异步发送。返回是否成功。未配置 SMTP 时返回 False 并记录日志。"""
        if not self.enabled or not self.host:
            logger.warning(f"SMTP 未配置，无法发送邮件到 {to_addr}（主题: {subject}）")
            return False
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._send_sync, to_addr, subject, body)
            logger.info(f"邮件已发送: {to_addr}（{subject}）")
            return True
        except Exception as e:
            logger.error(f"发送邮件失败（{to_addr}）: {e}")
            return False

    async def send_access_code(self, to_addr: str, applicant: str, code: str,
                               base_url: str, valid_from: str, quota_min: int) -> bool:
        valid_from_str = ""
        try:
            valid_from_str = datetime.fromisoformat(valid_from).strftime("%m月%d日 %H:%M")
        except Exception:
            valid_from_str = valid_from
        body = f"""{applicant}，你好：

你的同声传译系统使用申请已审批通过。

访问码：{code}
使用入口：{base_url}
生效时间：{valid_from_str} 起（结束时间不限制）
预计时长：{quota_min} 分钟（仅作参考，不做限制）

使用方法：打开使用入口，输入访问码即可进入控制端页面。
查看端（观众大屏）入口：{base_url}/viewer

如有问题请联系系统管理员。"""
        return await self.send(to_addr, "【同声传译系统】访问码审批通过", body)
