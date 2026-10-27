# handlers/raid_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.raid_manager import RaidManager
from ..models import Player
from .utils import player_required, set_event_flag


class RaidHandlers:
    def __init__(self, db: DataBase, raid_mgr: RaidManager):
        self.db = db
        self.raid_mgr = raid_mgr

    @player_required
    async def handle_challenge(self, player: Player, event: AstrMessageEvent):
        success, msg = await self.raid_mgr.challenge_raid(player)
        set_event_flag(event, "raid_challenge", bool(success))
        broadcast_msg = getattr(self.raid_mgr, "last_broadcast_msg", None)
        if broadcast_msg:
            set_event_flag(event, "raid_broadcast_msg", broadcast_msg)
            self.raid_mgr.last_broadcast_msg = None
        yield event.plain_result(msg)

    async def handle_status(self, event: AstrMessageEvent):
        yield event.plain_result(await self.raid_mgr.get_status())

    async def handle_ranking(self, event: AstrMessageEvent):
        yield event.plain_result(await self.raid_mgr.get_ranking())
