# handlers/gamble_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.gamble_manager import GambleManager
from ..models import Player
from .utils import player_required, set_event_flag

class GambleHandlers:
    def __init__(self, db: DataBase, gamble_mgr: GambleManager):
        self.db = db
        self.gamble_mgr = gamble_mgr
        self.last_gamble_success = False
        self.last_blackmarket_success = False

    @player_required
    async def handle_cut_stone(self, player: Player, event: AstrMessageEvent, count: str = "1"):
        set_event_flag(event, "gamble_success", False)
        try:
            count_int = int(count)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的数字！")
            return
        success, msg = await self.gamble_mgr.cut_stone(player, count_int)
        set_event_flag(event, "gamble_success", bool(success))
        yield event.plain_result(msg)

    async def handle_market(self, event: AstrMessageEvent):
        success, msg = self.gamble_mgr.get_black_market()
        yield event.plain_result(msg)

    @player_required
    async def handle_buy_market(self, player: Player, event: AstrMessageEvent, index: str = "0"):
        set_event_flag(event, "blackmarket_success", False)
        try:
            idx_int = int(index)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的编号！")
            return
        success, msg = await self.gamble_mgr.buy_black_market(player, idx_int)
        set_event_flag(event, "blackmarket_success", bool(success))
        yield event.plain_result(msg)