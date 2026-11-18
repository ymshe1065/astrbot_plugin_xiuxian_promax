# handlers/high_stakes_handlers.py
"""高风险玩法指令：藏宝图、矿脉、斗法盘口。"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent

from ..data import DataBase
from ..managers.betting_pool_manager import BettingPoolManager
from ..managers.mine_vein_manager import MineVeinManager
from ..managers.treasure_map_manager import TreasureMapManager
from ..models import Player
from .utils import player_required, set_event_flag

__all__ = ["HighStakesHandlers"]


class HighStakesHandlers:
    def __init__(self, db: DataBase, treasure_mgr: TreasureMapManager,
                 mine_mgr: MineVeinManager, betting_mgr: BettingPoolManager):
        self.db = db
        self.treasure_mgr = treasure_mgr
        self.mine_mgr = mine_mgr
        self.betting_mgr = betting_mgr

    # ================= 藏宝图 =================
    @player_required
    async def handle_treasure_map(self, player: Player, event: AstrMessageEvent, args: str = ""):
        parts = str(args or "").strip().split()
        sub = parts[0] if parts else ""
        if not sub or sub in ("帮助", "菜单", "状态"):
            yield event.plain_result(await self.treasure_mgr.get_status(player))
            return
        if sub in ("开始", "进入", "购买", "买"):
            tier = 1
            if len(parts) > 1:
                try:
                    tier = int(parts[1])
                except (TypeError, ValueError):
                    yield event.plain_result("❌ 地图档位必须是 1 / 2 / 3。")
                    return
            success, msg = await self.treasure_mgr.start(player, tier)
            set_event_flag(event, "treasure_start_ok", bool(success))
            yield event.plain_result(msg)
            return
        if sub in ("继续", "深入", "下一层", "next"):
            success, msg = await self.treasure_mgr.advance(player)
            set_event_flag(event, "treasure_advance_ok", bool(success))
            yield event.plain_result(msg)
            return
        if sub in ("撤退", "结算", "离开", "跑"):
            success, msg = await self.treasure_mgr.retreat(player)
            set_event_flag(event, "treasure_retreat_ok", bool(success))
            yield event.plain_result(msg)
            return
        yield event.plain_result(
            f"❌ 未知的藏宝图子命令「{sub}」。\n"
            "💡 可用：`/藏宝图`、`/藏宝图 开始 <1-3>`、`/藏宝图 继续`、`/藏宝图 撤退`"
        )

    # ================= 矿脉 =================
    @player_required
    async def handle_mine(self, player: Player, event: AstrMessageEvent, args: str = ""):
        parts = str(args or "").strip().split()
        sub = parts[0] if parts else ""
        if not sub or sub in ("帮助", "菜单", "市场", "勘探"):
            yield event.plain_result(await self.mine_mgr.get_market(player.user_id))
            return
        if sub in ("我的", "状态", "列表"):
            yield event.plain_result(await self.mine_mgr.my_mines(player.user_id))
            return
        if sub in ("购买", "买"):
            if len(parts) < 2:
                yield event.plain_result("❌ 用法：/矿脉 购买 <编号>")
                return
            try:
                index = int(parts[1])
            except (TypeError, ValueError):
                yield event.plain_result("❌ 矿脉编号必须是整数。")
                return
            success, msg = await self.mine_mgr.buy(player, index)
            set_event_flag(event, "mine_buy_ok", bool(success))
            yield event.plain_result(msg)
            return
        if sub in ("开采", "结算", "领取"):
            if len(parts) < 2:
                yield event.plain_result("❌ 用法：/矿脉 开采 <矿脉编号>")
                return
            try:
                mine_id = int(parts[1])
            except (TypeError, ValueError):
                yield event.plain_result("❌ 矿脉编号必须是整数。")
                return
            success, msg = await self.mine_mgr.claim(player, mine_id)
            set_event_flag(event, "mine_claim_ok", bool(success))
            yield event.plain_result(msg)
            return
        if sub in ("合伙", "邀请"):
            if len(parts) < 4:
                yield event.plain_result("❌ 用法：/矿脉 合伙 <矿脉编号> <道号或ID> <份额%>")
                return
            try:
                mine_id = int(parts[1])
                share = int(parts[3])
            except (TypeError, ValueError):
                yield event.plain_result("❌ 矿脉编号与份额必须是整数。")
                return
            success, msg = await self.mine_mgr.invite_partner(player, mine_id, parts[2], share)
            yield event.plain_result(msg)
            return
        if sub in ("接受", "同意"):
            if len(parts) < 2:
                yield event.plain_result("❌ 用法：/矿脉 接受 <矿脉编号>")
                return
            try:
                mine_id = int(parts[1])
            except (TypeError, ValueError):
                yield event.plain_result("❌ 矿脉编号必须是整数。")
                return
            success, msg = await self.mine_mgr.respond_invite(player, mine_id, True)
            yield event.plain_result(msg)
            return
        if sub in ("拒绝", "放弃"):
            if len(parts) < 2:
                yield event.plain_result("❌ 用法：/矿脉 拒绝 <矿脉编号>")
                return
            try:
                mine_id = int(parts[1])
            except (TypeError, ValueError):
                yield event.plain_result("❌ 矿脉编号必须是整数。")
                return
            success, msg = await self.mine_mgr.respond_invite(player, mine_id, False)
            yield event.plain_result(msg)
            return
        yield event.plain_result(
            f"❌ 未知的矿脉子命令「{sub}」。\n"
            "💡 可用：`/矿脉`、`/矿脉 购买 <编号>`、`/矿脉 我的`、`/矿脉 开采 <编号>`、"
            "`/矿脉 合伙 <编号> <道号> <份额%>`、`/矿脉 接受/拒绝 <编号>`"
        )

    # ================= 盘口 =================
    @player_required
    async def handle_betting(self, player: Player, event: AstrMessageEvent, args: str = ""):
        parts = str(args or "").strip().split()
        sub = parts[0] if parts else ""
        if not sub or sub in ("帮助", "菜单", "列表"):
            yield event.plain_result(await self.betting_mgr.get_board(player.user_id))
            return
        if sub in ("押注", "下注"):
            if len(parts) < 4:
                yield event.plain_result("❌ 用法：/盘口 押注 <编号> <A|B> <金额>")
                return
            try:
                index = int(parts[1])
                amount = int(parts[3])
            except (TypeError, ValueError):
                yield event.plain_result("❌ 编号与金额必须是整数。")
                return
            success, msg = await self.betting_mgr.place_bet(player, index, parts[2], amount)
            set_event_flag(event, "betting_ok", bool(success))
            yield event.plain_result(msg)
            return
        if sub in ("赌石", "压石"):
            if len(parts) < 4:
                yield event.plain_result("❌ 用法：/盘口 赌石 <1-5档> <出货|切垮> <金额>")
                return
            try:
                tier = int(parts[1])
                amount = int(parts[3])
            except (TypeError, ValueError):
                yield event.plain_result("❌ 档位与金额必须是整数。")
                return
            success, msg = await self.betting_mgr.bet_stone(player, tier, parts[2], amount)
            set_event_flag(event, "betting_ok", bool(success))
            yield event.plain_result(msg)
            return
        yield event.plain_result(
            f"❌ 未知的盘口子命令「{sub}」。\n"
            "💡 可用：`/盘口`、`/盘口 押注 <编号> <A|B> <金额>`、"
            "`/盘口 赌石 <1-5> <出货|切垮> <金额>`"
        )