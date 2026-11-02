"""护劫符图常驻 + 炼制前丹劫提示的回归测试。

背景：护劫类符图（避劫符/混元符）原本走稀有刷新，而丹劫是概率事件，
买不到符图就抽不出护盾，丹药只能被劈毁。现在护劫类符图常驻上架，
并在开炉前提醒玩家缺护劫符——背包里已经有够用的护劫符时不再打扰。
"""
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp

# 护劫类符图（配置里带 tribulation_shield 效果）
SHIELD_RECIPES = {"聚灵符": 4, "避劫符": 6, "混元符": 9}
# 需要丹劫护盾的丹药：灵品需 2、圣品需 6
PILL_SPIRIT = "筑基丹"
PILL_HOLY = "化神丹"


class TestShieldRecipeAlwaysAvailable(unittest.TestCase):
    def setUp(self):
        self.shop_mod = _imp("core.shop_manager")
        self.cm = _imp("config_manager").ConfigManager(fixtures.ROOT)

    def _entries(self):
        shop = self.shop_mod.ShopManager({}, self.cm)
        return {e["data"]["base_name"]: e for e in shop.get_talisman_recipes_for_display(4)}

    def test_shield_recipes_are_permanent_regardless_of_rank(self):
        entries = self._entries()
        for name, shield in SHIELD_RECIPES.items():
            entry = entries.get(name)
            self.assertIsNotNone(entry, f"{name} 不在符图货架")
            self.assertTrue(entry["always_available"], f"{name} 应常驻")
            self.assertFalse(entry["rare"], f"{name} 不应标记为稀有")
            self.assertEqual(entry["data"]["tribulation_shield"], shield)

    def test_non_shield_xuan_recipe_still_rare(self):
        """非护劫类的玄品符图仍走稀有刷新，避免整张货架失去稀有度。"""
        entry = self._entries().get("星辰符")
        self.assertIsNotNone(entry)
        self.assertFalse(entry["always_available"])
        self.assertTrue(entry["rare"])

    def test_recipe_description_mentions_shield(self):
        entry = self._entries()["避劫符"]
        self.assertIn("护劫", entry["data"]["description"])
        self.assertIn("常驻", entry["data"]["description"])

    def test_shield_helper_rejects_non_shield(self):
        shop = self.shop_mod.ShopManager({}, self.cm)
        self.assertEqual(shop.recipe_tribulation_shield({"category": "攻击", "rank": "圣品"}), 0)
        self.assertEqual(shop.recipe_tribulation_shield({"rank": "玄品", "category": "炼丹"}), 6)


class TestSyncPermanentEntries(unittest.IsolatedAsyncioTestCase):
    """旧货架里没有常驻条目时，读取即补齐，不用等整轮刷新。"""

    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)
        self.handler = _imp("handlers.shop_handler").ShopHandler(self.db, {}, self.cm)
        self.shop = self.handler.shop_manager

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown

    async def test_missing_permanent_recipes_are_appended(self):
        # 模拟旧货架：只有一张稀有符图，没有常驻护劫符图
        await self.db.update_shop_data("talisman_recipe_pavilion", 10, [{
            "name": "星辰符·符图", "type": "talisman_recipe", "rank": "玄品",
            "original_price": 8000, "discount": 1.0, "price": 8000, "stock": 2,
            "data": {"type": "符图", "name": "星辰符", "base_name": "星辰符", "rank": "玄品"},
        }])
        await self.handler._sync_talisman_recipe_shelf()
        _, items = await self.db.get_shop_data("talisman_recipe_pavilion")
        names = {i["name"] for i in items}
        self.assertIn("避劫符·符图", names)
        self.assertIn("混元符·符图", names)
        by_name = {i["name"]: i for i in items}
        self.assertTrue(by_name["避劫符·符图"]["unlimited"])
        self.assertTrue(by_name["避劫符·符图"]["always_available"])
        self.assertGreaterEqual(int(by_name["避劫符·符图"]["stock"]), 9999)

    async def test_existing_entry_is_upgraded_to_permanent(self):
        await self.db.update_shop_data("talisman_recipe_pavilion", 10, [{
            "name": "避劫符·符图", "type": "talisman_recipe", "rank": "玄品",
            "original_price": 8800, "discount": 1.0, "price": 8800, "stock": 1,
            "data": {"type": "符图", "name": "避劫符", "base_name": "避劫符", "rank": "玄品"},
        }])
        await self.handler._sync_talisman_recipe_shelf()
        _, items = await self.db.get_shop_data("talisman_recipe_pavilion")
        entry = next(i for i in items if i["name"] == "避劫符·符图")
        self.assertTrue(entry["always_available"])
        self.assertFalse(entry["rare"])
        self.assertGreaterEqual(int(entry["stock"]), 9999)

    async def test_empty_shelf_is_built_on_purchase(self):
        """新库/货架被清空时，玩家直接 /购买 避劫符·符图 也要能买到。

        丹劫提示把玩家指向符阁，这时货架可能从没建过；若只补条目而货架为空，
        购买会查不到任何东西，提示就成了空头支票。
        """
        await self.db.update_shop_data("talisman_recipe_pavilion", 0, [])
        pavilion_id, item = await self.handler._find_item_in_pavilions("避劫符·符图")
        self.assertEqual(pavilion_id, "talisman_recipe_pavilion")
        self.assertIsNotNone(item, "空货架时买不到常驻护劫符图")
        self.assertEqual(item["name"], "避劫符·符图")
        self.assertTrue(item.get("always_available"))
        # 建好的货架要落库，后续列表和再次购买都看得到
        _, items = await self.db.get_shop_data("talisman_recipe_pavilion")
        self.assertIn("避劫符·符图", {i["name"] for i in items})


class TestAlchemyTribulationHint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)
        self.player = await self._player(gold=100000)
        self.mgr = _imp("managers.alchemy_manager").AlchemyManager(
            self.db, self.cm, self.ring
        )

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    def test_warns_when_no_shield_talisman(self):
        msg = self.mgr.tribulation_hint(self.player, PILL_SPIRIT)
        self.assertIn("丹劫预警", msg)
        self.assertIn("需护盾 ≥ 2", msg)
        self.assertIn("没有护劫符", msg)
        self.assertIn("/符阁 符图", msg)
        self.assertIn("常驻", msg)

    def test_silent_when_backpack_has_enough_shield(self):
        self.player.set_storage_ring_items({"稳魂符": 1})
        self.assertEqual(self.mgr.tribulation_hint(self.player, PILL_SPIRIT), "")

    def test_warns_with_shortfall_when_shield_too_weak(self):
        self.player.set_storage_ring_items({"稳魂符": 1})
        msg = self.mgr.tribulation_hint(self.player, PILL_HOLY)
        self.assertIn("需护盾 ≥ 6", msg)
        self.assertIn("最高护盾只有 2", msg)

    def test_no_warning_for_pills_without_tribulation(self):
        self.assertEqual(self.mgr.tribulation_hint(self.player, "炼气丹"), "")

    async def test_fire_prompt_contains_hint(self):
        text = await self.mgr.start_fire_control(
            "u1", 1, "测试丹方", "  · 灵草 x1", "筑基丹", 80, "⚡ 丹劫预警：缺护劫符"
        )
        ok, msg = text
        self.assertTrue(ok)
        self.assertIn("丹劫预警", msg)
        self.assertIn("请选择火候", msg)

    async def test_fire_prompt_clean_without_hint(self):
        ok, msg = await self.mgr.start_fire_control(
            "u1", 1, "测试丹方", "  · 灵草 x1", "筑基丹", 80
        )
        self.assertTrue(ok)
        self.assertNotIn("丹劫预警", msg)


if __name__ == "__main__":
    unittest.main()
