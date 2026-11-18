# handlers/auction_house_handlers.py
"""拍卖会玩家指令处理器。"""

from astrbot.api.event import AstrMessageEvent

from ..data import DataBase
from ..managers.auction_house_manager import AuctionHouseManager
from ..models import Player
from .utils import player_required


class AuctionHouseHandlers:
    """拍卖会：查看、报名、出价、补领。"""

    def __init__(self, db: DataBase, manager: AuctionHouseManager, blind_manager=None):
        self.db = db
        self.manager = manager
        self.blind_manager = blind_manager

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


    # ===== 暗标场（v7.10.3）=====
    @player_required
    async def handle_blind_status(self, player: Player, event: AstrMessageEvent):
        if not self.blind_manager:
            yield event.plain_result("❌ 暗标场尚未启用。")
            return
        msg = await self.blind_manager.get_status(player.user_id)
        yield event.plain_result(msg)

    @player_required
    async def handle_blind_bid(self, player: Player, event: AstrMessageEvent, args: str = ""):
        if not self.blind_manager:
            yield event.plain_result("❌ 暗标场尚未启用。")
            return
        parts = str(args or "").split()
        if len(parts) < 2:
            yield event.plain_result(
                "🔨 暗标出价说明\n"
                "━━━━━━━━━━━━━━━\n"
                "用法：/拍卖会 暗标出价 <编号> <金额>\n"
                "示例：/拍卖会 暗标出价 1 12000\n"
                "💡 先发送 /拍卖会 暗标 查看暗标与起拍价。"
            )
            return
        try:
            lot_no = int(parts[0])
            amount = int(parts[1])
        except (TypeError, ValueError):
            yield event.plain_result("❌ 编号与金额必须是整数。")
            return
        success, msg = await self.blind_manager.place_bid(player, lot_no, amount)
        yield event.plain_result(msg)

    @player_required
    async def handle_blind_appraise(self, player: Player, event: AstrMessageEvent, lot_no: str = ""):
        if not self.blind_manager:
            yield event.plain_result("❌ 暗标场尚未启用。")
            return
        try:
            index = int(str(lot_no or "").strip())
        except (TypeError, ValueError):
            yield event.plain_result("❌ 请指定暗标编号，例如：/拍卖会 暗标鉴定 1")
            return
        success, msg = await self.blind_manager.appraise(player, index)
        yield event.plain_result(msg)


    @player_required
    async def handle_status_or_blind(self, player: Player, event: AstrMessageEvent, args: str = ""):
        """统一入口：/拍卖会 [暗标|暗标出价 ...|暗标鉴定 ...]"""
        text = str(args or "").strip()
        if not text or text in ("状态", "信息", "名单", "查看"):
            yield event.plain_result(await self.manager.get_status(player))
            return
        parts = text.split()
        head = parts[0]
        if head in ("暗标", "盲拍", "暗标场"):
            if len(parts) == 1:
                if not self.blind_manager:
                    yield event.plain_result("❌ 暗标场尚未启用。")
                    return
                yield event.plain_result(await self.blind_manager.get_status(player.user_id))
                return
            action = parts[1]
            rest = parts[2:]
            if action in ("出价", "下注", "bid"):
                if len(rest) < 2:
                    yield event.plain_result("❌ 用法：/拍卖会 暗标出价 <编号> <金额>")
                    return
                try:
                    lot_no = int(rest[0])
                    amount = int(rest[1])
                except (TypeError, ValueError):
                    yield event.plain_result("❌ 编号与金额必须是整数。")
                    return
                success, msg = await self.blind_manager.place_bid(player, lot_no, amount)
                yield event.plain_result(msg)
                return
            if action in ("鉴定", "掌眼", "appraise"):
                if not rest:
                    yield event.plain_result("❌ 用法：/拍卖会 暗标鉴定 <编号>")
                    return
                try:
                    lot_no = int(rest[0])
                except (TypeError, ValueError):
                    yield event.plain_result("❌ 编号必须是整数。")
                    return
                success, msg = await self.blind_manager.appraise(player, lot_no)
                yield event.plain_result(msg)
                return
            yield event.plain_result(
                "💡 可用：`/拍卖会 暗标`、`/拍卖会 暗标出价 <编号> <金额>`、"
                "`/拍卖会 暗标鉴定 <编号>`"
            )
            return
        yield event.plain_result(await self.manager.get_status(player))
