# tests/test_audit_round3.py
# 第三轮审计回归：功法复制、赠予退回、物品显示、容量显示、旧存档 JSON
import json
import time
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp, _FakeEvent


class TestGiftAndRing(unittest.IsolatedAsyncioTestCase):
    """赠予拒绝流程与储物戒容量展示（复用装备实例夹具）。"""

    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    async def test_reject_gift_with_missing_sender_cancels_request(self):
        receiver = await self._player("receiver")
        gift_id = await self.db.ext.create_pending_gift(
            receiver_id="receiver", sender_id="ghost", sender_name="已消失的道友",
            item_name="灵草", count=1,
        )
        Handler = _imp("handlers.storage_ring_handler").StorageRingHandler
        handler = Handler(self.db, self.cm)
        event = _FakeEvent("receiver", "receiver")
        results = [
            r async for r in handler.handle_reject_gift.__wrapped__(handler, receiver, event)
        ]
        self.assertTrue(any("作废" in str(r) for r in results), results)
        self.assertIsNone(await self.db.ext.get_pending_gift("receiver"))

    async def test_reject_gift_returns_item_to_overflow_no_silent_loss(self):
        sender = await self._player("sender")
        receiver = await self._player("receiver")
        # 把赠送者储物戒塞满，退回时应进入溢出仓库而不是被忽略
        capacity = self.ring.get_total_capacity(sender)
        sender.storage_ring_items = json.dumps({"灵草": capacity}, ensure_ascii=False)
        await self.db.update_player(sender)
        await self.db.ext.create_pending_gift(
            receiver_id="receiver", sender_id="sender", sender_name="sender",
            item_name="玄铁", count=2,
        )
        Handler = _imp("handlers.storage_ring_handler").StorageRingHandler
        handler = Handler(self.db, self.cm)
        event = _FakeEvent("receiver", "receiver")
        results = [
            r async for r in handler.handle_reject_gift.__wrapped__(handler, receiver, event)
        ]
        fresh = await self.db.get_player_by_id("sender")
        self.assertEqual(fresh.get_overflow_items().get("玄铁"), 2, results)
        self.assertIsNone(await self.db.ext.get_pending_gift("receiver"))
        # 重复拒绝不会再次发放
        results2 = [
            r async for r in handler.handle_reject_gift.__wrapped__(handler, receiver, event)
        ]
        self.assertTrue(any("没有待处理" in str(r) for r in results2), results2)

    async def test_retrieve_message_uses_total_capacity_with_enchant(self):
        p = await self._player()
        p.permanent_bonus = json.dumps({"storage_enchant": 5}, ensure_ascii=False)
        await self.db.update_player(p)
        await self.ring.store_item(p, "玄铁", 1)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.ring.retrieve_item(p, "玄铁", 1)
        self.assertTrue(ok, msg)
        total = self.ring.get_total_capacity(p)
        self.assertIn(f"/{total}容量", msg)
        p = await self.db.get_player_by_id("u1")
        self.assertLessEqual(self.ring.get_used_slots(p), total)


class TestEquipmentRound3(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    async def test_technique_from_techniques_json_can_equip_and_unequip_once(self):
        name = "回归测试剑诀"
        self.cm.techniques_data[name] = {
            "name": name, "type": "main_technique", "rank": "黄阶",
            "exp_multiplier": 0.05, "price": 100,
        }
        p = await self._player()
        await self.ring.store_item(p, name, 1)
        Handler = _imp("handlers.equipment_handler").EquipmentHandler
        handler = Handler(self.db, self.cm)
        event = _FakeEvent("u1", "u1")

        results = [
            r async for r in handler.handle_equip_item.__wrapped__(handler, p, event, name)
        ]
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.main_technique, name, results)
        self.assertEqual(p.get_storage_ring_items().get(name, 0), 0)

        for _ in range(1):
            p = await self.db.get_player_by_id("u1")
            out = [
                r async for r in handler.handle_unequip_item.__wrapped__(handler, p, event, "心法")
            ]
            self.assertTrue(any("已卸下" in str(r) for r in out), out)
            p = await self.db.get_player_by_id("u1")
            self.assertEqual(p.main_technique, "")
            # 卸下只应回库一次，不能被 handler 再存一次而复制
            self.assertEqual(p.get_storage_ring_items().get(name, 0), 1)

    async def test_equip_prompt_uses_realm_decay(self):
        cfg = {
            "name": "衰减测试剑", "type": "weapon", "rank": "黄阶",
            "equipment_rates": {"physical_damage": 0.18, "magic_damage": 0.18},
        }
        self.cm.items_data["衰减测试剑"] = cfg
        item = self.em.parse_item_from_name(
            "衰减测试剑", self.cm.items_data, self.cm.weapons_data, player_level=12
        )
        self.assertIsNotNone(item)
        self.assertAlmostEqual(item.equipment_rates["physical_damage"], 0.18 * 0.8 ** 4)
        raw = self.em.parse_item_from_name("衰减测试剑", self.cm.items_data, self.cm.weapons_data)
        self.assertAlmostEqual(raw.equipment_rates["physical_damage"], 0.18)


class TestItemDisplayRound3(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cm = _imp("config_manager").ConfigManager(fixtures.ROOT)
        self.ShopManager = _imp("core.shop_manager").ShopManager

    async def test_legacy_temporary_pill_shows_capped_effect(self):
        shop = self.ShopManager({}, self.cm)
        item = shop.find_item_by_name("混元散")
        self.assertIsNotNone(item)
        details = shop.get_item_details(item)
        self.assertIn("效果:", details)
        self.assertIn("物伤+50%（已达上限）", details)

    async def test_duplicate_item_names_are_gone(self):
        import collections

        counts = collections.Counter(v.get("name") for v in self.cm.items_data.values())
        self.assertEqual([k for k, v in counts.items() if v > 1], [])

    async def test_item_names_are_unique_and_old_recovery_items_resolve(self):
        shop = self.ShopManager({}, self.cm)
        item = shop.find_item_by_name("回春杖")
        self.assertIsNotNone(item)
        data = item.get("data") or item
        self.assertEqual(str(data.get("id")), "rec_9001")


class TestPlayerJsonRound3(unittest.IsolatedAsyncioTestCase):
    async def test_null_and_invalid_json_fields_fall_back(self):
        Player = _imp("models").Player
        p = Player(user_id="null1")
        for attr in (
            "techniques", "active_pill_effects", "pills_inventory",
            "storage_ring_items", "overflow_items", "equipment_instances",
            "equipped_instances", "permanent_pill_gains",
        ):
            setattr(p, attr, None)
        self.assertEqual(p.get_techniques_list(), [])
        self.assertEqual(p.get_active_pill_effects(), [])
        self.assertEqual(p.get_pills_inventory(), {})
        self.assertEqual(p.get_storage_ring_items(), {})
        self.assertEqual(p.get_overflow_items(), {})
        self.assertEqual(p.get_equipment_instances(), [])
        self.assertEqual(p.get_equipped_instances(), {})
        self.assertEqual(p.get_permanent_pill_gains(), {})

        p.techniques = "{}"  # 类型不符
        p.storage_ring_items = "[]"
        self.assertEqual(p.get_techniques_list(), [])
        self.assertEqual(p.get_storage_ring_items(), {})


class TestMarketCleanup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    async def test_cleanup_removes_listing_when_instance_disappeared(self):
        seller = await self._player("seller")
        await self.ring.store_item(seller, self.weapon, 1)
        seller = await self.db.get_player_by_id("seller")
        auction = self.AuctionManager(self.db, self.ring, {})
        ok, msg = await auction.list_item(seller, self.weapon, 1, 1000)
        self.assertTrue(ok, msg)
        seller = await self.db.get_player_by_id("seller")
        inst = self.im.list_instances(seller, "auction", self.weapon)[0]
        self.im.remove(seller, int(inst["id"]))
        await self.db.update_player(seller)

        removed = await auction.cleanup_orphan_listings()
        self.assertEqual(removed, 1)
        self.assertEqual(await auction.get_market_items(10), [])


if __name__ == "__main__":
    unittest.main()
