"""材料包购买与开启回归测试。"""
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp, _FakeEvent


class TestMaterialPackage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    def shop(self):
        return _imp("core.shop_manager").ShopManager({}, self.cm)

    async def _stock_package(self, name, price=100, stock=1):
        item = self.shop().find_item_by_name(name)
        item.update(stock=stock, price=price, original_price=price, discount=1.0)
        await self.db.update_shop_data("material_pavilion", 1, [item])
        return item

    def _handler(self):
        return _imp("handlers.shop_handler").ShopHandler(self.db, {}, self.cm)

    async def test_purchase_opens_package_and_charges(self):
        p = await self._player(gold=100000)
        await self._stock_package("灵植包·圣品", stock=2)
        expected_price = int(self.shop().find_item_by_name("灵植包·圣品")["price"])
        handler = self._handler()
        event = _FakeEvent(p.user_id, p.user_id, "/购买 灵植包·圣品")
        results = [r async for r in handler.handle_buy.__wrapped__(handler, p, event, "灵植包·圣品")]
        text = "\n".join(results)
        self.assertIn("成功购买并开启", text, text)
        self.assertNotIn("未知的物品类型", text, text)
        p = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(p.gold, 100000 - expected_price)
        ring = p.get_storage_ring_items()
        total = sum(ring.values())
        self.assertTrue(1 <= total <= 3, ring)
        for name in ring:
            material = self.cm.material_catalog.get(name)
            self.assertIsNotNone(material, name)
            self.assertEqual((material.get("category"), material.get("tier")), ("灵植", "圣品"))
        _, shelf = await self.db.get_shop_data("material_pavilion")
        self.assertEqual(int(shelf[0]["stock"]), 1)

    async def test_sold_out_package_reports_sold_out(self):
        p = await self._player(gold=100000)
        await self._stock_package("灵植包·圣品", stock=0)
        handler = self._handler()
        event = _FakeEvent(p.user_id, p.user_id, "/购买 灵植包·圣品")
        results = [r async for r in handler.handle_buy.__wrapped__(handler, p, event, "灵植包·圣品")]
        text = "\n".join(results)
        self.assertIn("已售罄", text, text)
        self.assertNotIn("没有找到", text, text)
        p = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(p.gold, 100000)
        self.assertEqual(p.get_storage_ring_items(), {})

    async def test_known_item_not_on_shelf_reports_acquire_hint(self):
        p = await self._player(gold=100000)
        handler = self._handler()
        event = _FakeEvent(p.user_id, p.user_id, "/购买 灵植包·圣品")
        results = [r async for r in handler.handle_buy.__wrapped__(handler, p, event, "灵植包·圣品")]
        text = "\n".join(results)
        self.assertIn("当前未在货架上", text, text)
        self.assertIn("获取途径", text, text)
        self.assertIn("材料阁", text, text)
        self.assertNotIn("未知的物品类型", text, text)

    def test_material_shop_shows_category_label(self):
        shop = self.shop()
        materials = shop.get_materials_for_display(2000)
        ore = next(item for item in materials if item["name"] == "噬魂铁")
        self.assertEqual(ore["category"], "矿石")
        ore = {**ore, "stock": 5, "discount": 1.0, "always_available": False}
        text = shop.format_pavilion_display("材料阁", [ore], 0, 0)
        self.assertIn("材料[矿石]", text)
        package = shop.find_item_by_name("矿材包·凡品")
        package = {**package, "stock": 5, "discount": 1.0, "always_available": False}
        package_text = shop.format_pavilion_display("材料阁", [package], 0, 0)
        self.assertIn("材料包[矿石]", package_text)

    def test_package_pool_only_valid_materials(self):
        shop = self.shop()
        for category in ("灵植", "矿材", "妖兽", "灵液"):
            for tier in ("凡品", "黄品", "玄品", "圣品"):
                pool = shop.get_package_material_pool(category, tier)
                self.assertTrue(pool, f"{category}/{tier}")
                for name, weight in pool:
                    self.assertGreater(weight, 0)
                    data = self.cm.items_data.get(name)
                    self.assertIsInstance(data, dict, name)
                    self.assertEqual(data.get("type"), "材料", name)


if __name__ == "__main__":
    unittest.main()
