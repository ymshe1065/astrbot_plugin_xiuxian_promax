# handlers/weekly_boss_handlers.py
"""周常 Boss（周本）指令处理器。"""

from astrbot.api.event import AstrMessageEvent

from ..data import DataBase
from ..managers.weekly_boss_manager import WeeklyBossManager
from ..models import Player
from .utils import player_required, resolve_greedy_args, set_event_flag


class WeeklyBossHandlers:
    def __init__(self, db: DataBase, weekly_boss_mgr: WeeklyBossManager):
        self.db = db
        self.weekly_boss_mgr = weekly_boss_mgr

    @player_required
    async def handle_info(self, player: Player, event: AstrMessageEvent):
        yield event.plain_result(await self.weekly_boss_mgr.get_info(player))

    @player_required
    async def handle_create(self, player: Player, event: AstrMessageEvent, team_name: str = ""):
        name = resolve_greedy_args(event, team_name, "周本创建")
        success, msg = await self.weekly_boss_mgr.create_team(player, name)
        set_event_flag(event, "weekly_boss_active", bool(success))
        yield event.plain_result(msg)

    @player_required
    async def handle_join(self, player: Player, event: AstrMessageEvent, run_id: str = ""):
        rid = resolve_greedy_args(event, run_id, "周本加入")
        success, msg = await self.weekly_boss_mgr.join_team(player, rid)
        set_event_flag(event, "weekly_boss_active", bool(success))
        yield event.plain_result(msg)

    @player_required
    async def handle_leave(self, player: Player, event: AstrMessageEvent):
        _success, msg = await self.weekly_boss_mgr.leave_team(player)
        yield event.plain_result(msg)

    @player_required
    async def handle_set_loadout(self, player: Player, event: AstrMessageEvent, args: str = ""):
        raw = resolve_greedy_args(event, args, "周本备战")
        _success, msg = await self.weekly_boss_mgr.set_loadout(player, raw)
        yield event.plain_result(msg)

    @player_required
    async def handle_start(self, player: Player, event: AstrMessageEvent):
        _success, msg = await self.weekly_boss_mgr.prepare_battle(player)
        yield event.plain_result(msg)

    @player_required
    async def handle_confirm(self, player: Player, event: AstrMessageEvent):
        success, msg = await self.weekly_boss_mgr.confirm_battle(player)
        set_event_flag(event, "weekly_boss_finished", bool(success))
        broadcast_msg = getattr(self.weekly_boss_mgr, "last_broadcast_msg", None)
        if broadcast_msg:
            set_event_flag(event, "weekly_boss_broadcast_msg", broadcast_msg)
            self.weekly_boss_mgr.last_broadcast_msg = None
        yield event.plain_result(msg)

    @player_required
    async def handle_cancel(self, player: Player, event: AstrMessageEvent):
        _success, msg = await self.weekly_boss_mgr.cancel_battle(player)
        yield event.plain_result(msg)

    @player_required
    async def handle_status(self, player: Player, event: AstrMessageEvent):
        yield event.plain_result(await self.weekly_boss_mgr.get_status(player))

    @player_required
    async def handle_ranking(self, player: Player, event: AstrMessageEvent):
        _ = player
        yield event.plain_result(await self.weekly_boss_mgr.get_leaderboard())

    async def handle_refresh(self, event: AstrMessageEvent):
        _success, msg = await self.weekly_boss_mgr.refresh_cycle()
        yield event.plain_result(msg)

    async def handle_end(self, event: AstrMessageEvent):
        _success, msg = await self.weekly_boss_mgr.end_cycle()
        yield event.plain_result(msg)

    async def handle_reset_cd(self, event: AstrMessageEvent, target: str = ""):
        raw = resolve_greedy_args(event, target, "重置周本CD")
        target_id = (raw or "").strip().lstrip("@")
        if not target_id:
            yield event.plain_result("❌ 请指定要重置的玩家 ID 或 @对方。")
            return
        # 支持 @昵称：优先按道号匹配
        if not target_id.isdigit():
            player = await self.db.get_player_by_name(target_id)
            if not player:
                yield event.plain_result(f"❌ 未找到道号为【{target_id}】的修士。")
                return
            target_id = player.user_id
        _success, msg = await self.weekly_boss_mgr.reset_player_cd(target_id)
        yield event.plain_result(msg)
