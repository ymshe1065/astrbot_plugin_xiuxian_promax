# handlers/recovery_handler.py
import json
import time

from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..models import Player
from ..models_extended import UserStatus
from .utils import player_required


class RecoveryHandler:
    """调息和收功：恢复由 RecoveryManager 在每次访问时惰性结算。"""

    def __init__(self, db: DataBase, config_manager=None):
        self.db = db
        self.config_manager = config_manager

    def _extra(self, cd) -> dict:
        try:
            data = json.loads(cd.extra_data or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _rest_rate_text(self, player: Player) -> str:
        """拼出实际调息速率与加成说明。"""
        try:
            from ..managers.recovery_manager import RecoveryManager
            rates = await RecoveryManager(self.db, self.config_manager).get_rest_rates(player)
        except Exception:
            return "每分钟恢复 3% 最大气血和灵气"
        return (
            f"每分钟恢复：气血 {rates['hp_rate'] * 100:.1f}% · 灵气 {rates['qi_rate'] * 100:.1f}%\n"
            f"基础恢复：{rates['base'] * 100:.0f}%/分钟\n"
            f"气血加成：+{rates['hp_bonus'] * 100:.0f}%（疗养殿/灵泉假山/装备）\n"
            f"灵气加成：+{rates['qi_bonus'] * 100:.0f}%（疗养殿/聚灵池/装备，封顶 +{rates['qi_cap'] * 100:.0f}%）"
        )

    @player_required
    async def handle_rest(self, player: Player, event: AstrMessageEvent):
        rest_cd = await self.db.ext.get_active_cd(player.user_id, UserStatus.RESTING)
        if rest_cd is not None:
            rate_text = await self._rest_rate_text(player)
            yield event.plain_result(
                "🧘 你正在调息中。\n"
                f"{rate_text}\n"
                "💡 发送 /收功 可以结束调息；气血和灵气满时会自动结束。"
            )
            return

        from ..managers.combat_manager import CombatManager
        stats = CombatManager.build_player_combat_stats(
            player, self.config_manager, db=self.db
        )
        if player.hp >= stats.max_hp and player.spiritual_qi >= stats.max_mp:
            yield event.plain_result("✅ 你的气血和灵气已满，无需调息。")
            return

        from ..core.avatar_rules import total_slots

        now = int(time.time())
        started, _slot, why = await self.db.ext.start_activity(
            player.user_id, UserStatus.RESTING, 0,
            extra_data={"resting_since": now, "rest_last_tick": now},
            max_slots=total_slots(player),
        )
        if not started:
            yield event.plain_result(
                "❌ 你已经在调息了。" if why == "already_running"
                else "❌ 分身都出去了，没人手替你调息。"
            )
            return
        player.state = "调息中"
        await self.db.update_player(player)
        rate_text = await self._rest_rate_text(player)
        yield event.plain_result(
            "🧘 开始调息\n"
            "━━━━━━━━━━━━━━━\n"
            f"{rate_text}\n"
            "气血/灵气满时自动结束，不设冷却\n"
            "💡 发送 /收功 可提前结束"
        )

    @player_required
    async def handle_stop_rest(self, player: Player, event: AstrMessageEvent):
        cd = await self.db.ext.get_active_cd(player.user_id, UserStatus.RESTING)
        if not cd:
            yield event.plain_result("❌ 你当前没有在调息。")
            return
        await self.db.ext.set_user_free(player.user_id, cd_type=UserStatus.RESTING)
        player.state = "空闲"
        await self.db.update_player(player)
        yield event.plain_result("✅ 调息结束。")
