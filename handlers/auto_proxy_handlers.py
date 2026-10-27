# handlers/auto_proxy_handlers.py
"""自动代理指令处理器"""
from astrbot.api.event import AstrMessageEvent

from ..data import DataBase
from ..managers.auto_proxy_manager import AutoProxyManager

__all__ = ["AutoProxyHandlers"]


class AutoProxyHandlers:
    def __init__(self, db: DataBase, proxy_mgr: AutoProxyManager):
        self.db = db
        self.proxy_mgr = proxy_mgr

    @staticmethod
    def _origin(event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    async def handle_enable(self, event: AstrMessageEvent, task: str = ""):
        """开启自动代理；支持直接指定活动或关闭/查看状态"""
        user_id = event.get_sender_id()
        raw = (task or "").strip()

        if raw.lower() in ("关闭", "停止", "off", "stop"):
            async for r in self.handle_disable(event):
                yield r
            return
        if raw.lower() in ("状态", "status"):
            yield event.plain_result(await self.proxy_mgr.status_text(user_id))
            return

        _, msg = await self.proxy_mgr.enable(user_id, self._origin(event), raw)
        yield event.plain_result(msg)

    async def handle_disable(self, event: AstrMessageEvent):
        """关闭自动代理"""
        user_id = event.get_sender_id()
        was_enabled = await self.proxy_mgr.disable(user_id, "manual_close")
        if was_enabled:
            yield event.plain_result(
                "🛑 自动代理已关闭。\n"
                "当前正在进行的活动不会中断，可继续手动结算或放弃。"
            )
        else:
            yield event.plain_result("💡 自动代理当前未开启。")

    async def handle_status(self, event: AstrMessageEvent):
        """查看自动代理状态"""
        yield event.plain_result(await self.proxy_mgr.status_text(event.get_sender_id()))
