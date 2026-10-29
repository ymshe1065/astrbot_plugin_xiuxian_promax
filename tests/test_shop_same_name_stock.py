"""同名货架条目库存定位与合并回归测试。"""
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp, _FakeEvent


class TestShopSameNameStock(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)
        self.shop = _imp("core.shop_manager").ShopManager({}, self.cm)

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    @staticmethod
    def _entry(item_id, stock, price=10, name="测试材料"):
        return {
            "name": name,
            "type": "material",
            "rank": "凡品",
            "original_price": price,
            "discount": 1.0,
            "price": price,
            "stock": stock,
            "data": {"id": item_id, "name": name, "type": "材料", "price": price},
        }

    async def test_decrement_uses_entry_key(self):
        await self.db.update_shop_data("material_pavilion", 1, [
            self._entry("1", 0),
            self._entry("2", 3, price=20),
        ])
        ok, _, remaining = await self.db.decrement_shop_item_stock(
            "material_pavilion", "测试材料", 2,
            entry_key={"id": "2", "quality": ""},
        )
        self.assertTrue(ok)
        self.assertEqual(remaining, 1)
        _, shelf = await self.db.get_shop_data("material_pavilion")
        self.assertEqual(int(shelf[0]["stock"]), 0)
        self.assertEqual(int(shelf[1]["stock"]), 1)

    async def test_decrement_without_key_falls_back_to_stocked_row(self):
        await self.db.update_shop_data("material_pavilion", 1, [
            self._entry("1", 0),
            self._entry("2", 2),
        ])
        ok, _, remaining = await self.db.decrement_shop_item_stock("material_pavilion", "测试材料", 1)
        self.assertTrue(ok)
        self.assertEqual(remaining, 1)
        _, shelf = await self.db.get_shop_data("material_pavilion")
        self.assertEqual(int(shelf[0]["stock"]), 0)
        self.assertEqual(int(shelf[1]["stock"]), 1)

    async def test_increment_restores_matching_row(self):
        await self.db.update_shop_data("sect_shop_1", 1, [
            self._entry("1", 1),
            self._entry("2", 0),
        ])
        handler = _imp("handlers.sect_shop_handler").SectShopHandler(self.db, self.shop, self.ring, {}, None)
        await handler._restore_stock("sect_shop_1", "测试材料", {"id": "2", "quality": ""})
        _, shelf = await self.db.get_shop_data("sect_shop_1")
        self.assertEqual(int(shelf[0]["stock"]), 1)
        self.assertEqual(int(shelf[1]["stock"]), 1)

    async def test_dedupe_keeps_first_and_sums_stock(self):
        shelf = [self._entry("1", 1), self._entry("2", 2), self._entry("3", 1, name="其他材料")]
        changed = self.shop.dedupe_same_name_items(shelf)
        self.assertTrue(changed)
        self.assertEqual([x["name"] for x in shelf], ["测试材料", "其他材料"])
        self.assertEqual(int(shelf[0]["stock"]), 3)
        self.assertEqual(int(shelf[1]["stock"]), 1)

    async def test_purchase_merges_duplicate_shelf_rows(self):
        p = await self._player(gold=100000)
        base = next(v for v in self.cm.items_data.values() if v.get("type") == "材料")
        entry = {
            "name": base["name"], "type": "material", "rank": base.get("rank", "凡品"),
            "original_price": 10, "discount": 1.0, "price": 10, "stock": 0,
            "data": {**base},
        }
        await self.db.update_shop_data("material_pavilion", 1, [entry, {**entry, "stock": 2}])
        handler = _imp("handlers.shop_handler").ShopHandler(self.db, {}, self.cm)
        event = _FakeEvent(p.user_id, p.user_id, f"/购买 {base['name']}")
        results = [r async for r in handler.handle_buy.__wrapped__(handler, p, event, base["name"])]
        text = "\n".join(results)
        self.assertIn("成功购买", text, text)
        _, shelf = await self.db.get_shop_data("material_pavilion")
        self.assertEqual(len(shelf), 1)
        self.assertEqual(int(shelf[0]["stock"]), 1)
        p = await self.db.get_player_by_id(p.user_id)
        self.assertLess(p.gold, 100000)


if __name__ == "__main__":
    unittest.main()
