# handlers/adventure_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..managers.adventure_manager import AdventureManager
from ..data.data_manager import DataBase
from ..utils.result_card import render_result_card
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

    async def handle_adventure_info(self, event: AstrMessageEvent):
        """历练信息 - 显示路线、风险与收益"""
        routes = self.adv_mgr.get_route_overview()
        sections = []
        for route in routes:
            duration = route.get("duration", 0) // 60
            product = route.get("product") or "全产物"
            sections.append({
                "name": f"{route['name']} · {route.get('risk', '未知')}风险",
                "lines": [
                    f"时长：{duration} 分钟 | 推荐境界 ≥ {route.get('min_level', 0)} | 主产：{product}",
                    f"说明：{route.get('description', '')}",
                ],
            })
        async for result in self._send_result(
            event, "📖 历练路线总览",
            f"共 {len(routes)} 条路线",
            subtitle="走完全程才结算，到点自动发放",
            sections=sections,
        ):
            yield result

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
