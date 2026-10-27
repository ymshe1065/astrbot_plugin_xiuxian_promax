# handlers/auction_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.auction_manager import AuctionManager
from ..models import Player
from .utils import player_required, set_event_flag

class AuctionHandlers:
    """玩家交易坊市处理器"""
    
    def __init__(self, db: DataBase, auction_mgr: AuctionManager):
        self.db = db
        self.auction_mgr = auction_mgr
        self.last_sell_success = False

    async def handle_list(self, event: AstrMessageEvent):
        """查看坊市"""
        msg = await self.auction_mgr.get_market_list()

        # ===== 坊市卡片：成功只发图，失败/关闭降级为文本 =====
        sent = False
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            if card_enabled("CARD_MARKET"):
                items = await self.auction_mgr.get_market_items(10)
                if items:
                    lines = []
                    for it in items:
                        name = it['item_name'] if len(it['item_name']) <= 14 else it['item_name'][:14] + "…"
                        lines.append(f"[{it['id']}] {name} x{it['count']} · {it['price'] * it['count']:,}灵石")
                    card_path = await render_card_async(
                        "shop",
                        "🏪 玩家交易坊市",
                        f"在售 {len(items)} 件 · 交易税 5%",
                        sections=[{"name": "最新上架", "lines": lines}],
                        footer="/坊市购买 <编号> · /我的坊市 查看NPC问价",
                    )
                    if card_path:
                        yield event.image_result(card_path)
                        sent = True
        except Exception:
            pass
        if not sent:
            yield event.plain_result(msg)

    @player_required
    async def handle_list_item(self, player: Player, event: AstrMessageEvent, args: str = ""):
        """上架物品到坊市"""
        set_event_flag(event, "market_sell_success", False)
        if not args or not args.strip():
            yield event.plain_result(
                "🏪 上架物品说明\n"
                "━━━━━━━━━━━━━━━\n"
                "用法：/坊市上架 <物品名> [品质] <数量> <单价>\n"
                "示例：/坊市上架 天阶·诛仙剑 1 50000\n"
                "示例：/坊市上架 筑基丹 良好 1 8000"
            )
            return

        # 考虑到物品名称可能带空格，从右侧切分最后两个参数
        parts = args.strip().rsplit(" ", 2)
        if len(parts) < 3:
            yield event.plain_result("❌ 参数不足！请确保包含：物品名/丹药名、数量、单价。\n示例：/坊市上架 极品灵草 5 1000")
            return

        item_name = parts[0].strip()
        try:
            count = int(parts[1])
            price = int(parts[2])
        except ValueError:
            yield event.plain_result(
                "❌ 数量和单价必须是纯数字！\n"
                "正确格式：/坊市上架 <物品名或丹药名> [品质] <数量> <单价>\n"
                "示例：/坊市上架 玄铁 5 1000"
            )
            return

        quality = ""
        try:
            pm = self.auction_mgr._pill_manager()
        except Exception:
            pm = None
        if pm:
            tokens = item_name.split()
            if len(tokens) >= 2 and tokens[-1] in pm.QUALITY_ORDER:
                quality = tokens.pop()
                item_name = " ".join(tokens)
            elif "|" in item_name:
                base_name, embedded_quality = pm.split_pill_key(item_name)
                item_name = base_name
                quality = embedded_quality

        if count <= 0 or price <= 0:
            yield event.plain_result("❌ 数量和单价必须大于 0。\n示例：/坊市上架 玄铁 5 1000")
            return

        success, msg = await self.auction_mgr.list_item(player, item_name, count, price, quality)
        set_event_flag(event, "market_sell_success", bool(success))
        yield event.plain_result(msg)

    @player_required
    async def handle_my_market(self, player: Player, event: AstrMessageEvent):
        """查看个人坊市与NPC问价"""
        msg = await self.auction_mgr.get_player_market(player)
        yield event.plain_result(msg)

    @player_required
    async def handle_accept_offer(self, player: Player, event: AstrMessageEvent, offer_id: str = "0"):
        """接受NPC问价并卖出"""
        try:
            oid = int(offer_id)
        except ValueError:
            yield event.plain_result(
                "❌ 请输入正确的问价编号！例如：/坊市卖出 1\n"
                "💡 先发送 /我的坊市 查看NPC问价。"
            )
            return
        if oid <= 0:
            yield event.plain_result("❌ 问价编号必须大于 0。")
            return

        success, msg = await self.auction_mgr.accept_offer(player, oid)
        yield event.plain_result(msg)

    @player_required
    async def handle_decline_offer(self, player: Player, event: AstrMessageEvent, offer_id: str = "0"):
        """拒绝本次NPC问价，继续等待"""
        try:
            oid = int(offer_id)
        except ValueError:
            yield event.plain_result(
                "❌ 请输入正确的问价编号！例如：/坊市等待 1\n"
                "💡 先发送 /我的坊市 查看NPC问价。"
            )
            return
        if oid <= 0:
            yield event.plain_result("❌ 问价编号必须大于 0。")
            return

        success, msg = await self.auction_mgr.decline_offer(player, oid)
        yield event.plain_result(msg)

    @player_required
    async def handle_buy(self, player: Player, event: AstrMessageEvent, auction_id: str = "0"):
        """从坊市购买物品"""
        try:
            aid = int(auction_id)
        except ValueError:
            yield event.plain_result(
                "❌ 请输入正确的商品编号！例如：/坊市购买 1\n"
                "💡 先发送 /坊市 查看在售商品与编号。"
            )
            return
            
        if aid <= 0:
            yield event.plain_result("❌ 商品编号必须大于 0。")
            return
        
        success, msg = await self.auction_mgr.buy_item(player, aid)
        yield event.plain_result(msg)

    @player_required
    async def handle_cancel(self, player: Player, event: AstrMessageEvent, auction_id: str = "0"):
        """下架坊市物品"""
        try:
            aid = int(auction_id)
        except ValueError:
            yield event.plain_result(
                "❌ 请输入正确的商品编号！例如：/坊市下架 1\n"
                "💡 编号可在 /坊市 列表中查看。"
            )
            return
            
        if aid <= 0:
            yield event.plain_result("❌ 商品编号必须大于 0。")
            return
            
        success, msg = await self.auction_mgr.cancel_item(player, aid)
        yield event.plain_result(msg)