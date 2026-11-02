"""符阁同名符图/成品符购买消歧回归测试。

背景：符图与成品符在配置里同名，旧实现购买只按 name 匹配，导致
① `购买 寻宝符` 可能买到符图；② 已习得符图后再买会被 learn_recipe 拦下并回滚，
同名成品符永远买不到。现在裸名买成品符、显式「符图」才买符图。
"""
import json
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp, _FakeEvent

# 用凡品符做夹具：黄品与护劫类符图现在是常驻不限量，测不了库存扣减
BASE = "疾风符"


class TestTalismanShopDisambiguation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)
        self.handler = _imp("handlers.shop_handler").ShopHandler(self.db, {}, self.cm)
        self.shop = self.handler.shop_manager
        self.recipe_cfg = self.cm.talisman_recipes_data[BASE]
        self.item_cfg = self.cm.items_data[BASE]
        self.recipe_price = int(int(self.recipe_cfg["price"]) * 0.8)
        self.item_price = int(self.item_cfg["price"])
        await self._seed_shelf()

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    async def _seed_shelf(self):
        finished = {
            "name": BASE, "type": "material", "rank": self.item_cfg.get("rank", "凡品"),
            "original_price": self.item_price, "discount": 1.0,
            "price": self.item_price, "stock": 5,
            "data": {**self.item_cfg},
        }
        recipe = {
            "name": f"{BASE}·符图", "type": "talisman_recipe", "rank": self.recipe_cfg.get("rank", "黄品"),
            "original_price": self.recipe_price, "discount": 1.0,
            "price": self.recipe_price, "stock": 3,
            "data": {"type": "符图", "name": BASE, "base_name": BASE,
                     "rank": self.recipe_cfg.get("rank", "黄品")},
        }
        await self.db.update_shop_data("talisman_pavilion", 1, [finished])
        await self.db.update_shop_data("talisman_recipe_pavilion", 1, [recipe])

    async def _buy(self, player, text):
        event = _FakeEvent(player.user_id, player.user_id, f"/购买 {text}")
        # 把最近浏览货架设为符图区，验证选择顺序不依赖浏览顺序
        self.handler._remember_viewed_pavilion(event, "talisman_recipe_pavilion")
        arg = text.split(" ", 1)[0]
        results = [r async for r in self.handler.handle_buy.__wrapped__(self.handler, player, event, arg)]
        return "\n".join(results)

    async def test_bare_name_buys_finished_talisman(self):
        player = await self._player(gold=100000)
        text = await self._buy(player, BASE)
        self.assertIn("成功购买符箓", text, text)
        player = await self.db.get_player_by_id(player.user_id)
        self.assertEqual(player.get_storage_ring_items().get(BASE), 1)
        self.assertNotIn(f"{BASE}·符图", player.get_storage_ring_items())
        # 符图未被习得
        self.assertNotIn(BASE, player.talisman_recipes or "")

    async def test_suffix_buys_recipe(self):
        player = await self._player(gold=100000)
        text = await self._buy(player, f"{BASE}·符图")
        self.assertIn("成功购入符图", text, text)
        player = await self.db.get_player_by_id(player.user_id)
        self.assertIn(BASE, json.loads(player.talisman_recipes or "[]"))
        self.assertNotIn(BASE, player.get_storage_ring_items())

    async def test_plain_suffix_variant_also_buys_recipe(self):
        player = await self._player(gold=100000)
        text = await self._buy(player, f"{BASE}符图")
        self.assertIn("成功购入符图", text, text)
        player = await self.db.get_player_by_id(player.user_id)
        self.assertIn(BASE, json.loads(player.talisman_recipes or "[]"))

    async def test_learned_recipe_no_longer_blocks_finished_talisman(self):
        """旧实现：已习得符图后买同名成品符会被回滚，这里回归验证不再发生。"""
        player = await self._player(gold=100000)
        first = await self._buy(player, f"{BASE}·符图")
        self.assertIn("成功购入符图", first, first)
        player = await self.db.get_player_by_id(player.user_id)

        second = await self._buy(player, BASE)
        self.assertIn("成功购买符箓", second, second)
        player = await self.db.get_player_by_id(player.user_id)
        self.assertEqual(player.get_storage_ring_items().get(BASE), 1)

    async def test_stock_decrement_targets_shelf_entry_name(self):
        player = await self._player(gold=100000)
        await self._buy(player, f"{BASE}·符图")
        _, shelf = await self.db.get_shop_data("talisman_recipe_pavilion")
        self.assertEqual(int(shelf[0]["stock"]), 2)
        _, shelf = await self.db.get_shop_data("talisman_pavilion")
        self.assertEqual(int(shelf[0]["stock"]), 5)

    async def test_legacy_bare_recipe_entry_is_renamed_on_read(self):
        """旧货架里的符图条目是裸名，读取时惰性迁移成「符名·符图」。"""
        legacy = {
            "name": BASE, "type": "talisman_recipe", "rank": "黄品",
            "original_price": self.recipe_price, "discount": 1.0,
            "price": self.recipe_price, "stock": 1,
            "data": {"type": "符图", "name": BASE, "rank": "黄品"},
        }
        await self.db.update_shop_data("talisman_recipe_pavilion", 1, [legacy])
        player = await self._player(gold=100000)
        text = await self._buy(player, f"{BASE}·符图")
        self.assertIn("成功购入符图", text, text)
        _, shelf = await self.db.get_shop_data("talisman_recipe_pavilion")
        self.assertEqual(shelf[0]["name"], f"{BASE}·符图")
        self.assertEqual(int(shelf[0]["stock"]), 0)


class TestTalismanNameHelpers(unittest.TestCase):
    def test_split_query_variants(self):
        shop_mod = _imp("core.shop_manager").ShopManager
        cases = {
            "寻宝符·符图": ("寻宝符", True),
            "寻宝符符图": ("寻宝符", True),
            "符图寻宝符": ("寻宝符", True),
            "寻宝符（符图）": ("寻宝符", True),
            "寻宝符(符图)": ("寻宝符", True),
            "寻宝符": ("寻宝符", False),
        }
        for raw, expect in cases.items():
            self.assertEqual(shop_mod.split_talisman_query(raw), expect, raw)

    def test_entry_base_name_handles_legacy_bare_recipe(self):
        shop_mod = _imp("core.shop_manager").ShopManager
        self.assertEqual(
            shop_mod.entry_base_name({"name": "寻宝符·符图", "type": "talisman_recipe"}),
            "寻宝符",
        )
        self.assertEqual(
            shop_mod.entry_base_name({"name": "寻宝符", "type": "talisman_recipe"}),
            "寻宝符",
        )
        self.assertEqual(
            shop_mod.entry_base_name({"name": "寻宝符", "type": "material"}),
            "寻宝符",
        )


if __name__ == "__main__":
    unittest.main()
