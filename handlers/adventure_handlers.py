# handlers/adventure_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..managers.adventure_manager import AdventureManager
from ..data.data_manager import DataBase
from ..utils.result_card import render_result_card
from ..utils.list_card import to_card_parts
from .utils import set_event_flag

class AdventureHandlers:
    def __init__(self, db: DataBase, adv_mgr: AdventureManager):
        self.db = db
        self.adv_mgr = adv_mgr

    async def _send_result(self, event: AstrMessageEvent, title: str, msg: str,
                           subtitle: str = "", sections=None):
        """长结果统一发单张图；图片关闭或失败时回退原文。"""
        card = await render_result_card(
            title,
            msg,
            background="adventure",
            subtitle=subtitle,
            footer="/历练状态 查看进度 · /退出历练 提前离开",
            sections=sections,
            enabled_key="CARD_ADVENTURE",
        )
        if card and hasattr(event, "image_result"):
            yield event.image_result(card)
        else:
            yield event.plain_result(msg)

    async def handle_adventure_info(self, event: AstrMessageEvent, product: str = ""):
        """历练信息 - 可走路线按「新解锁优先」排列，支持按产物筛选"""
        user_id = event.get_sender_id()
        data = await self.adv_mgr.build_route_list(user_id, product)
        if not data:
            yield event.plain_result("❌ 未找到历练路线配置，请联系管理员。")
            return

        sections, payload = to_card_parts(
            data, "route_list", top_n=self.adv_mgr.LIST_TOP_N,
            note="同一境界下所有路线修为同速，差别在产物、灵石与风险；走完全程才结算",
        )
        msg = self._route_list_text(data)
        card = await render_result_card(
            "历练路线", msg, background="adventure", subtitle=data["subtitle"],
            footer="/开始历练 <路线名> 出发 · /历练状态 查看进度",
            sections=sections, enabled_key="CARD_ADVENTURE", payload_extra=payload,
        )
        if card and hasattr(event, "image_result"):
            yield event.image_result(card)
        else:
            yield event.plain_result(msg)

    def _route_list_text(self, data: dict) -> str:
        """列表的纯文本版：图片与网页都不可用时兜底。"""
        lines = ["📖 历练路线", str(data.get("subtitle") or ""), "━━━━━━━━━━━━━━━"]
        for group in data.get("groups") or []:
            items = group.get("items") or []
            if not items:
                continue
            if group.get("collapsed"):
                lines.append(f"▸ {group.get('name')}")
                for it in items[:4]:
                    lines.append(f"  🔒 {it['title']}（需境界{it['level']}）")
                continue
            lines.append(f"▸ {group.get('name')}")
            for it in items[: self.adv_mgr.LIST_TOP_N]:
                lines.append(
                    f"  {it['title']}  {it['minutes']}分 · {it['facet']} · "
                    f"{it['risk']}风险 · 修为{self.adv_mgr._fmt_amount(it['exp'])}"
                )
            rest = len(items) - self.adv_mgr.LIST_TOP_N
            if rest > 0:
                lines.append(f"  …另有 {rest} 条，/历练信息 灵植 可筛选")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("💡 /历练信息 灵植 / 矿材 / 灵液 / 妖兽 / 全产物 按产物筛选")
        return "\n".join(lines)

    async def handle_start_adventure(self, event: AstrMessageEvent, route: str = ""):
        """开始历练"""
        user_id = event.get_sender_id()
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        success, msg = await self.adv_mgr.start_adventure(user_id, route, umo=umo)
        # 成功提示和路线选择菜单较长；普通错误保持文本，避免为短提示额外绘图。
        if success or "请选择历练路线" in msg or len(msg) > 80:
            async for result in self._send_result(
                event, "🗺️ 历练出发", msg, subtitle="选择路线后出发"
            ):
                yield result
        else:
            yield event.plain_result(msg)

    async def handle_complete_adventure(self, event: AstrMessageEvent):
        """完成历练（走完才结算，未走完返回剩余时间）"""
        user_id = event.get_sender_id()
        success, msg, _ = await self.adv_mgr.finish_adventure(user_id)
        if success:
            async for result in self._send_result(
                event, "🎉 历练完成", msg, subtitle="本次历练结算"
            ):
                yield result
        else:
            yield event.plain_result(msg)
    
    async def handle_adventure_status(self, event: AstrMessageEvent):
        """历练状态"""
        user_id = event.get_sender_id()
        success, msg = await self.adv_mgr.check_adventure_status(user_id)
        if success:
            async for result in self._send_result(
                event, "📍 历练进度", msg, subtitle="当前历练状态"
            ):
                yield result
        else:
            yield event.plain_result(msg)

    async def handle_abandon_adventure(self, event: AstrMessageEvent):
        """中途退出历练（无奖励、无惩罚）"""
        set_event_flag(event, "adventure_abandon_success", False)
        user_id = event.get_sender_id()
        success, msg = await self.adv_mgr.abandon_adventure(user_id)
        set_event_flag(event, "adventure_abandon_success", success)
        if success:
            async for result in self._send_result(
                event, "🏳️ 历练结束", msg, subtitle="未走完，本次无奖励"
            ):
                yield result
        else:
            yield event.plain_result(msg)
