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

    @player_required
    async def handle_rest(self, player: Player, event: AstrMessageEvent):
        cd = await self.db.ext.get_user_cd(player.user_id)
        if not cd:
            await self.db.ext.create_user_cd(player.user_id)
            cd = await self.db.ext.get_user_cd(player.user_id)
        if int(cd.type) == int(UserStatus.RESTING):
            yield event.plain_result(
                "🧘 你正在调息中。\n"
                "💡 发送 /收功 可以结束调息；气血和真元满时会自动结束。"
            )
            return
        if int(cd.type) != int(UserStatus.IDLE):
            yield event.plain_result("❌ 你当前正忙，无法开始调息。")
            return

        from ..managers.combat_manager import CombatManager
        stats = CombatManager.build_player_combat_stats(
            player, self.config_manager, db=self.db
        )
        if player.hp >= stats.max_hp and player.mp >= stats.max_mp:
            yield event.plain_result("✅ 你的气血和真元已满，无需调息。")
            return

        now = int(time.time())
        extra = self._extra(cd)
        extra["resting_since"] = now
        extra["rest_last_tick"] = now
        await self.db.ext.set_user_busy(
            player.user_id, UserStatus.RESTING, 0, extra_data=extra
        )
        player.state = "调息中"
        await self.db.update_player(player)
        yield event.plain_result(
            "🧘 开始调息\n"
            "━━━━━━━━━━━━━━━\n"
            "每分钟恢复 5% 最大气血和真元\n"
            "气血/真元满时自动结束，不设冷却\n"
            "💡 发送 /收功 可提前结束"
        )

    @player_required
    async def handle_stop_rest(self, player: Player, event: AstrMessageEvent):
        cd = await self.db.ext.get_user_cd(player.user_id)
        if not cd or int(cd.type) != int(UserStatus.RESTING):
            yield event.plain_result("❌ 你当前没有在调息。")
            return
        extra = self._extra(cd)
        extra.pop("resting_since", None)
        extra.pop("rest_last_tick", None)
        await self.db.ext.set_user_busy(
            player.user_id, UserStatus.IDLE, 0, extra_data=extra
        )
        player.state = "空闲"
        await self.db.update_player(player)
        yield event.plain_result("✅ 调息结束。")
