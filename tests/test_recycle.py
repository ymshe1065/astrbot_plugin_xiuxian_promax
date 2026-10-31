"""回收指令：按配置价值 60% 回收物品。"""
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp


class TestRecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)
        self.manager = _imp("core.storage_ring_manager").StorageRingManager(self.db, self.cm)

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    async def test_recycle_material_at_sixty_percent(self):
        p = await self._player(gold=0)
        name = "噬魂铁"
        await self.ring.store_item(p, name, 3)
        p = await self.db.get_player_by_id(p.user_id)
        expected = int(self.cm.get_item_price(name) * 0.6) * 2
        ok, message = await self.manager.recycle_item(p, name, 2)
        self.assertTrue(ok, message)
        p = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(p.gold, expected)
        self.assertEqual(p.get_storage_ring_items().get(name), 1)

    async def test_recycle_drug_at_sixty_percent(self):
        p = await self._player(gold=0)
        name = "筑基丹"
        p.set_pills_inventory({f"{name}|一般": 2})
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        expected = int(self.cm.get_item_price(name) * 0.6)
        ok, message = await self.manager.recycle_item(p, name, 1)
        self.assertTrue(ok, message)
        p = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(p.gold, expected)
        self.assertEqual(p.get_pills_inventory().get(f"{name}|一般"), 1)

    async def test_recycle_equipment_instance(self):
        p = await self._player(gold=0)
        await self.ring.store_item(p, self.weapon, 1)
        p = await self.db.get_player_by_id(p.user_id)
        inst = self.im.list_instances(p, "storage", self.weapon)[0]
        quality_mult = {"一般": 1.0, "良好": 1.05, "极佳": 1.10, "大师": 1.15}.get(
            inst.get("quality") or "一般", 1.0
        )
        expected = int(round(self.cm.get_item_price(self.weapon) * quality_mult * 0.6))
        ok, message = await self.manager.recycle_item(p, self.weapon, 1)
        self.assertTrue(ok, message)
        p = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(p.gold, expected)
        self.assertEqual(self.im.list_instances(p, "storage", self.weapon), [])

    async def test_recycle_missing_item_rejected(self):
        p = await self._player(gold=0)
        ok, _ = await self.manager.recycle_item(p, "不存在的物品", 1)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
