# 装备实例化与交易回归测试
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
astrbot = types.ModuleType("astrbot"); api_mod = types.ModuleType("astrbot.api"); event_mod = types.ModuleType("astrbot.api.event")


class _L:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


api_mod.logger = _L(); api_mod.AstrBotConfig = dict
event_mod.AstrMessageEvent = object; api_mod.event = event_mod
all_mod = types.ModuleType("astrbot.api.all"); all_mod.At = object; all_mod.Plain = object
api_mod.all = all_mod
sys.modules.setdefault("astrbot", astrbot); sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod); sys.modules.setdefault("astrbot.api.all", all_mod)


def _imp(p):
    return __import__(f"{PKG}.{p}", fromlist=["x"])


class _FakeAt:
    def __init__(self, qq): self.qq = qq


class _FakePlain:
    def __init__(self, text): self.text = text


class _FakeMessageObj:
    def __init__(self, components): self.message = components


class _FakeEvent:
    def __init__(self, user_id, name, text="", components=None):
        self._user_id = user_id
        self._name = name
        self._text = text
        self.message_obj = _FakeMessageObj(components or [])
        self.results = []

    def get_sender_id(self): return self._user_id
    def get_sender_name(self): return self._name
    def get_message_str(self): return self._text
    def plain_result(self, text):
        self.results.append(text)
        return text


class TestEquipmentInstances(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = _imp("data")
        self.cm = _imp("config_manager").ConfigManager(ROOT)
        self.Player = _imp("models").Player
        self.core = _imp("core")
        self.AuctionManager = _imp("managers.auction_manager").AuctionManager
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = self.data.DataBase(self.path); await self.db.connect()
        await self.data.MigrationManager(self.db.conn, self.cm).migrate()
        self.ring = self.core.StorageRingManager(self.db, self.cm)
        self.em = self.core.EquipmentManager(self.db, self.cm, self.ring)
        self.im = self.core.ItemInstanceManager(self.db, self.cm)
        self.weapon = next(iter(self.cm.weapons_data.keys()))

    async def asyncTearDown(self):
        await self.db.close()
        try: os.remove(self.path)
        except OSError: pass

    async def _player(self, uid="u1", gold=0):
        p = self.Player(user_id=uid, user_name=uid, level_index=10, gold=gold)
        await self.db.create_player(p)
        return await self.db.get_player_by_id(uid)

    async def test_equipment_store_equip_unequip_uses_instances(self):
        p = await self._player()
        ok, msg = await self.ring.store_item(p, self.weapon, 2)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(len(self.im.list_instances(p, "storage", self.weapon)), 2)
        inst = self.im.find_for_equip(p, self.weapon, 1)
        item = self.em.parse_item_from_name(self.weapon, self.cm.items_data, self.cm.weapons_data, inst)
        ok, msg = await self.em.equip_item(p, item, int(inst["id"]))
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_equipped_instances().get("weapon"), int(inst["id"]))
        ok, msg = await self.em.unequip_item(p, "武器")
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.weapon, "")
        self.assertEqual(len(self.im.list_instances(p, "storage", self.weapon)), 2)

    async def test_equipment_auction_roundtrip(self):
        seller = await self._player("seller")
        buyer = await self._player("buyer", gold=5000)
        await self.ring.store_item(seller, self.weapon, 1)
        seller = await self.db.get_player_by_id("seller")
        auction = self.AuctionManager(self.db, self.ring, {})
        ok, msg = await auction.list_item(seller, self.weapon, 1, 1000)
        self.assertTrue(ok, msg)
        market = await auction.get_market_items(10)
        self.assertIn("·", market[0]["item_name"])
        buyer = await self.db.get_player_by_id("buyer")
        ok, msg = await auction.buy_item(buyer, market[0]["id"])
        self.assertTrue(ok, msg)
        buyer = await self.db.get_player_by_id("buyer")
        self.assertEqual(len(self.im.list_instances(buyer, "storage", self.weapon)), 1)
        seller = await self.db.get_player_by_id("seller")
        self.assertEqual(len(self.im.list_instances(seller, "storage", self.weapon)), 0)
        self.assertEqual(seller.gold, 950)

    async def test_equipment_overflow_and_claim_creates_instance(self):
        p = await self._player()
        cap = self.ring.get_total_capacity(p)
        p.storage_ring_items = json.dumps({"灵草": cap}, ensure_ascii=False)
        await self.db.update_player(p)
        ok, msg = await self.ring.store_item(p, self.weapon, 1)
        self.assertTrue(ok)
        self.assertIn("溢出", msg)
        p = await self.db.get_player_by_id("u1")
        overflow_insts = self.im.list_instances(p, "overflow", self.weapon)
        self.assertEqual(len(overflow_insts), 1)
        self.assertEqual(len(self.im.list_instances(p, "storage", self.weapon)), 0)
        # 修改品质，验证领取溢出后不会被重置为默认品质
        instances = self.im.load(p)
        for inst in instances:
            if inst.get("id") == overflow_insts[0].get("id"):
                inst["quality"] = "大师"
        self.im.save(p, instances)
        await self.db.update_player(p)
        p.storage_ring_items = json.dumps({}, ensure_ascii=False)
        await self.db.update_player(p)
        claimed, msg = await self.ring.claim_overflow(p)
        self.assertGreaterEqual(claimed, 1)
        p = await self.db.get_player_by_id("u1")
        storage_insts = self.im.list_instances(p, "storage", self.weapon)
        self.assertEqual(len(storage_insts), 1)
        self.assertEqual(storage_insts[0].get("quality"), "大师")

    async def test_equipment_gift_send_and_accept(self):
        module = _imp("handlers.storage_ring_handler")
        module.At = _FakeAt
        module.Plain = _FakePlain
        handler = module.StorageRingHandler(self.db, self.cm)
        sender = await self._player("sender")
        receiver = await self._player("receiver")
        await self.ring.store_item(sender, self.weapon, 1)
        sender = await self.db.get_player_by_id("sender")
        event = _FakeEvent(
            "sender", "sender", text=f"/赠予 @receiver {self.weapon} 1",
            components=[_FakeAt("receiver"), _FakePlain(f" {self.weapon} 1")],
        )
        async for _ in handler.handle_gift_item.__wrapped__(handler, sender, event, ""):
            pass
        self.assertTrue(any("赠予请求已发送" in x for x in event.results), event.results)
        gift = await self.db.ext.get_pending_gift("receiver")
        self.assertEqual(gift["item_kind"], "equipment")
        target = await self.db.get_player_by_id("receiver")
        accept_event = _FakeEvent("receiver", "receiver")
        async for _ in handler.handle_accept_gift.__wrapped__(handler, target, accept_event):
            pass
        target = await self.db.get_player_by_id("receiver")
        self.assertEqual(len(self.im.list_instances(target, "storage", self.weapon)), 1)

    async def test_equipment_gift_reject_returns_to_sender(self):
        module = _imp("handlers.storage_ring_handler")
        module.At = _FakeAt
        module.Plain = _FakePlain
        handler = module.StorageRingHandler(self.db, self.cm)
        sender = await self._player("sender")
        await self._player("receiver")
        await self.ring.store_item(sender, self.weapon, 1)
        sender = await self.db.get_player_by_id("sender")
        event = _FakeEvent(
            "sender", "sender", text=f"/赠予 @receiver {self.weapon} 1",
            components=[_FakeAt("receiver"), _FakePlain(f" {self.weapon} 1")],
        )
        async for _ in handler.handle_gift_item.__wrapped__(handler, sender, event, ""):
            pass
        receiver = await self.db.get_player_by_id("receiver")
        reject_event = _FakeEvent("receiver", "receiver")
        async for _ in handler.handle_reject_gift.__wrapped__(handler, receiver, reject_event):
            pass
        sender = await self.db.get_player_by_id("sender")
        receiver = await self.db.get_player_by_id("receiver")
        self.assertEqual(len(self.im.list_instances(sender, "storage", self.weapon)), 1)
        self.assertEqual(len(self.im.list_instances(receiver, "storage", self.weapon)), 0)
        self.assertIsNone(await self.db.ext.get_pending_gift("receiver"))

    async def test_equip_failure_does_not_duplicate_instance(self):
        from astrbot.api.event import AstrMessageEvent
        ItemRules = _imp("core").ItemRules
        EquipmentHandler = _imp("handlers.equipment_handler").EquipmentHandler
        p = await self._player("low")
        p.level_index = 0
        await self.db.update_player(p)
        weapon = next(
            name for name, cfg in self.cm.weapons_data.items()
            if ItemRules.get_required_level(cfg) > 0
        )
        await self.ring.store_item(p, weapon, 1)
        p = await self.db.get_player_by_id("low")
        handler = EquipmentHandler(self.db, self.cm)
        event = _FakeEvent("low", "low", text=f"/装备 {weapon}")
        async for _ in handler.handle_equip_item.__wrapped__(handler, p, event, weapon):
            pass
        p = await self.db.get_player_by_id("low")
        self.assertEqual(len(self.im.list_instances(p, "storage", weapon)), 1)

    async def test_legacy_equipment_migrates_to_instances(self):
        p = await self._player()
        p.weapon = self.weapon
        p.storage_ring_items = json.dumps({self.weapon: 2}, ensure_ascii=False)
        await self.db.update_player(p)
        await self.data.migration._migrate_to_v40(self.db.conn, self.cm)
        p = await self.db.get_player_by_id("u1")
        self.assertNotIn(self.weapon, p.get_storage_ring_items())
        self.assertEqual(len(self.im.list_instances(p, "storage", self.weapon)), 2)
        equipped_id = p.get_equipped_instances().get("weapon")
        self.assertTrue(equipped_id)
        self.assertEqual(self.im.get_instance(p, equipped_id)["name"], self.weapon)


if __name__ == "__main__":
    unittest.main()
