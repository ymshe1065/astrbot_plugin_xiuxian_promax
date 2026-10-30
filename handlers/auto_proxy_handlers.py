# handlers/auto_proxy_handlers.py
"""自动代理指令处理器"""
from astrbot.api.event import AstrMessageEvent

from ..data import DataBase
from ..managers.auto_proxy_manager import AutoProxyManager, TASK_ESCORT, TASK_SECT
from ..utils.arg_parse import ALL_KEYWORDS, is_all_keyword

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
            async for r in self.handle_disable(event, "全部"):
                yield r
            return
        if raw.lower() in ("状态", "status"):
            yield event.plain_result(await self.proxy_mgr.status_text(user_id))
            return

        _, msg = await self.proxy_mgr.enable(user_id, self._origin(event), raw)
        yield event.plain_result(msg)

    async def handle_disable(self, event: AstrMessageEvent, args: str = ""):
        """关闭自动代理；不填默认全部关闭，也可指定押镖/宗门任务/灵田/秘境。"""
        user_id = event.get_sender_id()
        raw = (args or "").strip() or "全部"
        usage = (
            "❌ 无法识别要关闭的代理内容。\n"
            "💡 /关闭代理（不填=全部关闭）\n"
            "💡 /关闭代理 押镖\n"
            "💡 /关闭代理 宗门任务\n"
            "💡 /关闭代理 灵田 [格号...]\n"
            "💡 /关闭代理 秘境\n"
            "💡 /关闭代理 全部"
        )
        if is_all_keyword(raw, ALL_KEYWORDS):
            was_enabled = await self.proxy_mgr.disable(user_id, "manual_close")
            if was_enabled:
                yield event.plain_result(
                    "🛑 自动代理已全部关闭（含灵田/秘境代理）。\n"
                    "押镖/宗门任务不会中断；灵田成熟作物需手动 /收获；当前秘境探索可继续 /完成探索。"
                )
            else:
                yield event.plain_result("💡 自动代理当前未开启。")
            return
        if self.proxy_mgr._is_farm_request(raw):
            parts = raw.split(maxsplit=1)
            rest = parts[1] if len(parts) > 1 else ""
            ok, msg = await self.proxy_mgr.disable_farm(user_id, rest)
            yield event.plain_result(msg)
            return
        if self.proxy_mgr._is_rift_request(raw):
            ok, msg = await self.proxy_mgr.disable_rift(user_id)
            yield event.plain_result(msg)
            return
        if self.proxy_mgr.normalize_task(raw) in (TASK_ESCORT, TASK_SECT):
            # 只关闭押镖/宗门任务代理；灵田代理继续运行
            ok, msg = await self.proxy_mgr.disable_primary(user_id, "manual_close")
            yield event.plain_result(msg)
            return
        yield event.plain_result(usage)

    async def handle_status(self, event: AstrMessageEvent):
        """查看自动代理状态"""
        yield event.plain_result(await self.proxy_mgr.status_text(event.get_sender_id()))
