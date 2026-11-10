"""交易场所对新增丹药/丹方的覆盖回归。

覆盖丹阁·丹药、丹方阁、突破丹方、百宝阁、黑市、宗门商店、拍卖会与统一名称查询。
"""
import random
import re
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp, ROOT


def _pill_id(item) -> int:
    match = re.search(r"(\d+)$", str(item.get("id") or ""))
    return int(match.group(1)) if match else -1


class TestTradeVenuePillCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.shop = _imp("core.shop_manager").ShopManager({}, cls.cm)
        cls.new_pills = {
            item["name"] for item in cls.cm.pills_data.values()
        } | {
            item["name"] for item in cls.cm.exp_pills_data.values()
            if _pill_id(item) >= 31
        } | {
            item["name"] for item in cls.cm.utility_pills_data.values()
            if _pill_id(item) >= 154
        }
        cls.zero_weight_pills = {
            item["name"] for item in cls.cm.utility_pills_data.values()
            if int(item.get("shop_weight", 0) or 0) == 0
        }

    def test_new_pills_exist_in_three_sources(self):
        self.assertTrue({"圣元丹", "九霄圣丹", "太初灵丹"} <= set(self.cm.exp_pills_data))
        self.assertTrue({"圣体丹", "圣心破境丹", "混元定魂丹"} <= set(self.cm.utility_pills_data))
        self.assertTrue(self.cm.pills_data)

    def test_pill_pavilion_covers_all_three_pill_sources(self):
        names = {item["name"] for item in self.shop.get_pills_for_display(9999)}
        for name in ("圣元丹", "九霄圣丹", "太初灵丹",       # 修为丹
                     "圣体丹", "圣心破境丹", "混元定魂丹",   # 功能丹
                     "合体丹", "混元丹"):                    # 破境丹
            self.assertIn(name, names)

    def test_recipe_pavilion_and_breakthrough_pavilion(self):
        recipe_names = {item["name"] for item in self.shop.get_recipes_for_display(9999)}
        for name in ("《圣元丹方》", "《圣体丹方》", "《混元定魂丹方》"):
            self.assertIn(name, recipe_names)
        breakthrough = {
            item["name"] for item in self.shop.get_breakthrough_recipes_for_display()
        }
        self.assertIn("《合体丹方》", breakthrough)
        self.assertIn("《混元丹方》", breakthrough)
        # 突破丹方常驻区与随机丹方区互斥
        self.assertFalse(recipe_names & breakthrough)

    def test_treasure_pavilion_covers_new_pills_and_skips_zero_weight(self):
        pool = self.shop.get_treasure_items_for_display(9999)
        pool_names = {item["name"] for item in pool}
        self.assertTrue({"圣体丹", "混元定魂丹", "合体丹"} <= pool_names)
        self.assertTrue(self.zero_weight_pills)
        random.seed(20240503)
        seen_new = False
        for _ in range(30):
            shelf = self.shop.generate_pavilion_items(
                self.shop.get_treasure_items_for_display, 12, pick_mode="rare"
            )
            names = {item["name"] for item in shelf}
            self.assertFalse(names & self.zero_weight_pills)
            seen_new = seen_new or bool(names & self.new_pills)
        self.assertTrue(seen_new)

    def test_sect_shop_pool_covers_new_pills_and_skips_zero_weight(self):
        handler = _imp("handlers.sect_shop_handler").SectShopHandler(
            None, self.shop, None, {}, None
        )
        pool = self.shop.get_all_items_for_display(9999)
        pool += self.shop.get_recipes_for_display(9999)
        weighted = handler._weighted_candidates(pool)
        names = {item["name"] for item in weighted}
        self.assertTrue({"圣体丹", "混元定魂丹", "合体丹"} <= names)
        self.assertIn("《圣元丹方》", names)
        self.assertFalse(names & self.zero_weight_pills)

    def test_black_market_covers_new_pills_and_skips_zero_weight(self):
        module = _imp("managers.gamble_manager")
        manager = module.GambleManager(None, self.cm, None)
        original_time = module.time.time
        listed = set()
        try:
            for hour in range(200):
                module.time.time = lambda h=hour: h * 3600 + 10
                items, _ = manager._get_black_market_items()
                listed.update(item["name"] for item in items if not item.get("fake"))
        finally:
            module.time.time = original_time
        self.assertTrue(listed & self.new_pills, sorted(listed & self.new_pills))
        self.assertFalse(listed & self.zero_weight_pills)

    def test_black_market_type_labels_do_not_misclassify_gear(self):
        """装备 subtype（手套/戒指）不能被丹药类型识别逻辑误判为功能丹。"""
        manager = _imp("managers.gamble_manager").GambleManager(None, self.cm, None)
        self.assertEqual(manager._black_market_type_label(
            {"type": "gloves", "subtype": "手套"}), "手套")
        self.assertEqual(manager._black_market_type_label(
            {"type": "ring", "subtype": "戒指"}), "戒指")
        self.assertEqual(manager._black_market_type_label(
            {"type": "pill", "subtype": "breakthrough"}), "丹药")
        self.assertEqual(manager._black_market_type_label(
            {"subtype": "exp"}), "修为丹")
        self.assertEqual(manager._black_market_type_label(
            {"subtype": "resurrection", "effect_type": "instant"}), "功能丹")

    def test_auction_house_pill_and_recipe_candidates_cover_all_sources(self):
        import types

        manager = types.SimpleNamespace(
            config_manager=self.cm,
            _as_int=_imp("managers.auction_house_manager").AuctionHouseManager._as_int,
        )
        candidates = _imp("managers.auction_house_manager").AuctionHouseManager._category_candidates
        pill_names = {name for name, _, _, _, _ in candidates(manager, "pill")}
        for name in ("圣元丹", "九霄圣丹", "圣体丹", "混元定魂丹", "合体丹"):
            self.assertIn(name, pill_names)
        recipe_names = {name for name, _, _, _, _ in candidates(manager, "recipe")}
        for name in ("《圣元丹方》", "《圣体丹方》", "《合体丹方》"):
            self.assertIn(name, recipe_names)

    def test_find_item_by_name_covers_all_pill_sources(self):
        for name in ("圣元丹", "九霄圣丹", "圣体丹", "混元定魂丹", "合体丹"):
            item = self.shop.find_item_by_name(name)
            self.assertIsNotNone(item, name)
            self.assertGreater(int(item.get("price") or 0), 0, name)


if __name__ == "__main__":
    unittest.main()
