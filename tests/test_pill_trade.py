# 丹药坊市/赠予交易回归测试
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")


class _StubLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


api_mod.logger = _StubLogger()
api_mod.AstrBotConfig = dict
event_mod.AstrMessageEvent = object
api_mod.event = event_mod
all_mod = types.ModuleType("astrbot.api.all")
all_mod.At = object
all_mod.Plain = object
api_mod.all = all_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod)
sys.modules.setdefault("astrbot.api.all", all_mod)


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class _FakeAt:
    def __init__(self, qq):
        self.qq = qq


class _FakePlain:
    def __init__(self, text):
        self.text = text


class _FakeMessageObj:
    def __init__(self, components):
        self.message = components


class _FakeEvent:
    def __init__(self, user_id, name, text="", components=None):
        self._user_id = user_id
        self._name = name
        self._text = text
        self.message_obj = _FakeMessageObj(components or [])
        self.results = []

    def get_sender_id(self):
        return self._user_id

    def get_sender_name(self):
        return self._name

    def get_message_str(self):
        return self._text

    def plain_result(self, text):
        self.results.append(text)
        return text


class TestPillTrade(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _imp("data")
        cls.ConfigManager = _imp("config_manager").ConfigManager
        cls.Player = _imp("models").Player
        cls.AuctionManager = _imp("managers.auction_manager").AuctionManager
        cls.StorageRingManager = _imp("core").StorageRingManager
        cls.StorageRingHandler = _imp("handlers.storage_ring_handler").StorageRingHandler
        cls.config_manager = cls.ConfigManager(ROOT)

    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = self.data.DataBase(self.db_path)
        await self.db.connect()
        await self.data.MigrationManager(self.db.conn, self.config_manager).migrate()
        self.storage = self.StorageRingManager(self.db, self.config_manager)

    async def asyncTearDown(self):
        await self.db.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    async def test_pill_auction_roundtrip_with_quality(self):
        seller = self.Player(user_id="seller", user_name="卖家", gold=0)
        seller.pills_inventory = json.dumps({"筑基丹|良好": 2}, ensure_ascii=False)
        buyer = self.Player(user_id="buyer", user_name="买家", gold=5000)
        await self.db.create_player(seller)
        await self.db.create_player(buyer)

        auction = self.AuctionManager(self.db, self.storage, {})
        ok, msg = await auction.list_item(seller, "筑基丹", 1, 1000, "良好")
        self.assertTrue(ok, msg)
        market = await auction.get_market_items(10)
        self.assertEqual(market[0]["item_name"], "筑基丹·良好")

        buyer = await self.db.get_player_by_id("buyer")
        ok, msg = await auction.buy_item(buyer, market[0]["id"])
        self.assertTrue(ok, msg)
        buyer = await self.db.get_player_by_id("buyer")
        self.assertEqual(buyer.get_pills_inventory().get("筑基丹|良好"), 1)
        seller = await self.db.get_player_by_id("seller")
        self.assertEqual(seller.gold, 950)
        self.assertEqual(seller.get_pills_inventory().get("筑基丹|良好"), 1)

    async def test_pill_cancel_returns_to_inventory(self):
        seller = self.Player(user_id="seller", user_name="卖家", gold=0)
        seller.pills_inventory = json.dumps({"炼气丹|大师": 1}, ensure_ascii=False)
        await self.db.create_player(seller)
        auction = self.AuctionManager(self.db, self.storage, {})
        ok, msg = await auction.list_item(seller, "炼气丹", 1, 100)
        self.assertTrue(ok, msg)
        market = await auction.get_market_items(10)
        # 未指定品质时默认上架最高品质
        self.assertEqual(market[0]["item_name"], "炼气丹·大师")
        seller = await self.db.get_player_by_id("seller")
        ok, msg = await auction.cancel_item(seller, market[0]["id"])
        self.assertTrue(ok, msg)
        seller = await self.db.get_player_by_id("seller")
        self.assertEqual(seller.get_pills_inventory().get("炼气丹|大师"), 1)

    async def test_expired_gift_returns_to_sender(self):
        sender = self.Player(user_id="sender", user_name="送礼者")
        sender.pills_inventory = json.dumps({}, ensure_ascii=False)
        sender.storage_ring_items = json.dumps({}, ensure_ascii=False)
        receiver = self.Player(user_id="receiver", user_name="收礼者")
        await self.db.create_player(sender)
        await self.db.create_player(receiver)
        await self.db.ext.create_pending_gift(
            receiver_id="receiver", sender_id="sender", sender_name="送礼者",
            item_name="筑基丹", count=2, expires_hours=-1,
            item_kind="pill", item_quality="良好",
        )
        await self.db.ext.cleanup_expired_gifts()
        sender = await self.db.get_player_by_id("sender")
        self.assertEqual(sender.get_pills_inventory().get("筑基丹|良好"), 2)
        self.assertIsNone(await self.db.ext.get_pending_gift("receiver"))

    async def test_delete_receiver_returns_pending_pill_to_sender(self):
        sender = self.Player(user_id="sender", user_name="送礼者")
        sender.pills_inventory = json.dumps({}, ensure_ascii=False)
        receiver = self.Player(user_id="receiver", user_name="收礼者")
        await self.db.create_player(sender)
        await self.db.create_player(receiver)
        await self.db.ext.create_pending_gift(
            receiver_id="receiver", sender_id="sender", sender_name="送礼者",
            item_name="筑基丹", count=1, expires_hours=24,
            item_kind="pill", item_quality="极佳",
        )
        await self.db.delete_player_cascade("receiver")
        sender = await self.db.get_player_by_id("sender")
        self.assertEqual(sender.get_pills_inventory().get("筑基丹|极佳"), 1)
        self.assertIsNone(await self.db.get_player_by_id("receiver"))

    async def test_pill_gift_send_and_accept(self):
        module = _imp("handlers.storage_ring_handler")
        module.At = _FakeAt
        module.Plain = _FakePlain
        handler = module.StorageRingHandler(self.db, self.config_manager)

        sender = self.Player(user_id="sender", user_name="送礼者")
        sender.pills_inventory = json.dumps({"筑基丹|极佳": 2}, ensure_ascii=False)
        receiver = self.Player(user_id="receiver", user_name="收礼者")
        await self.db.create_player(sender)
        await self.db.create_player(receiver)

        event = _FakeEvent(
            "sender", "送礼者", text="/赠予 @receiver 筑基丹 极佳 1",
            components=[_FakeAt("receiver"), _FakePlain(" 筑基丹 极佳 1")],
        )
        async for _ in handler.handle_gift_item.__wrapped__(handler, sender, event, ""):
            pass
        self.assertTrue(any("赠予请求已发送" in text for text in event.results), event.results)

        gift = await self.db.ext.get_pending_gift("receiver")
        self.assertIsNotNone(gift)
        self.assertEqual(gift["item_kind"], "pill")
        self.assertEqual(gift["item_quality"], "极佳")

        accept_event = _FakeEvent("receiver", "收礼者")
        async for _ in handler.handle_accept_gift.__wrapped__(handler, receiver, accept_event):
            pass
        receiver = await self.db.get_player_by_id("receiver")
        self.assertEqual(receiver.get_pills_inventory().get("筑基丹|极佳"), 1)


if __name__ == "__main__":
    unittest.main()
