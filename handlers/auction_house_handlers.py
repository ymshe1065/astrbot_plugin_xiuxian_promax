# handlers/auction_house_handlers.py
"""拍卖会玩家指令处理器。"""

from astrbot.api.event import AstrMessageEvent

from ..data import DataBase
from ..managers.auction_house_manager import AuctionHouseManager
from ..models import Player
from .utils import player_required


class AuctionHouseHandlers:
    """拍卖会：查看、报名、出价、补领。"""

    def __init__(self, db: DataBase, manager: AuctionHouseManager):
        self.db = db
        self.manager = manager

    @player_required
    async def handle_status(self, player: Player, event: AstrMessageEvent):
        msg = await self.manager.get_status(player)
        yield event.plain_result(msg)

    @player_required
    async def handle_register(self, player: Player, event: AstrMessageEvent):
        success, msg = await self.manager.register(player)
        yield event.plain_result(msg)

    @player_required
    async def handle_bid(self, player: Player, event: AstrMessageEvent, amount: str = ""):
        if not amount or not str(amount).strip():
            yield event.plain_result(
                "🔨 拍卖会出价说明\n"
                "━━━━━━━━━━━━━━━\n"
                "用法：/拍卖会出价 <金额>\n"
                "示例：/拍卖会出价 5000\n"
                "💡 先发送 /拍卖会 查看当前拍品与最低加价。"
            )
            return
        success, msg = await self.manager.bid(player, amount)
        yield event.plain_result(msg)

    @player_required
    async def handle_claim(self, player: Player, event: AstrMessageEvent):
        success, msg = await self.manager.claim_pending(player)
        yield event.plain_result(msg)
