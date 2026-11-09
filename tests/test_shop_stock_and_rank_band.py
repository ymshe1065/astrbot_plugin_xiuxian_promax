"""商店装备库存、品阶区间与百宝阁排序/分区回归。"""
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp, ROOT


class _Player:
    def __init__(self, level_index):
        self.level_index = level_index


class TestShopStockAndRankBand(unittest.TestCase):
    def setUp(self):
        _imp("data")
        self.cm = _imp("config_manager").ConfigManager(ROOT)
        self.shop = _imp("core.shop_manager").ShopManager({}, self.cm)

    def test_equipment_stock_is_always_one(self):
        for name, cfg in self.cm.special_equipment_data.items():
            self.assertEqual(self.shop._calculate_stock(9999, cfg), 1, name)
        self.assertEqual(
            self.shop._calculate_stock(100, self.cm.items_data["灵草"]) >= 1
            if "灵草" in self.cm.items_data else True,
            True,
        )

    def test_legacy_equipment_stock_is_clamped_to_one(self):
        items = [
            {"name": "凡品·灵纹法手", "type": "gloves", "stock": 5,
             "data": self.cm.items_data["凡品·灵纹法手"]},
            {"name": "灵草", "type": "material", "stock": 5, "data": {"type": "材料"}},
        ]
        self.assertTrue(self.shop.ensure_items_have_stock(items))
        self.assertEqual(items[0]["stock"], 1)
        self.assertEqual(items[1]["stock"], 5)

    def test_empty_server_rank_band_defaults_to_xuan(self):
        band = self.shop.resolve_rank_band([], force=True)
        self.assertEqual((band["min"], band["max"]), (0, 2))
        self.assertAlmostEqual(band["center"], 2.0)

    def test_rank_band_tracks_server_levels(self):
        players = [_Player(0), _Player(9), _Player(35)]
        band = self.shop.resolve_rank_band(players, force=True)
        self.assertEqual(band["min"], 0)
        self.assertEqual(band["max"], 11)
        self.assertTrue(0 <= band["center"] <= 11)

    def test_rank_band_filters_equipment_but_keeps_consumables(self):
        self.shop.resolve_rank_band([_Player(0)], force=True)
        items = self.shop.get_all_items_for_display(9999)
        gear_ranks = {
            self.shop.rank_index(x["rank"])
            for x in items
            if self.cm.items_data.get(x["name"], {}).get("type") in ("gloves", "ring")
        }
        self.assertTrue(gear_ranks)
        self.assertTrue(all(rank <= 2 for rank in gear_ranks))
        self.assertTrue(any(x["type"] == "pill" for x in items))

    def test_sort_and_group_for_treasure(self):
        items = [
            {"name": "乙", "type": "pill", "rank": "黄品", "price": 10},
            {"name": "甲", "type": "gloves", "rank": "凡品", "price": 20},
            {"name": "丙", "type": "gloves", "rank": "凡品", "price": 10},
        ]
        ordered = self.shop.sort_pavilion_items(items)
        self.assertEqual([x["name"] for x in ordered], ["丙", "甲", "乙"])
        groups = self.shop.group_items_by_rank(ordered)
        self.assertEqual([rank for rank, _ in groups], ["凡品", "黄品"])

    def test_common_and_rare_pick_weights_shift_quality(self):
        self.shop.resolve_rank_band([_Player(0), _Player(9), _Player(20)], force=True)
        weapons = self.shop.get_weapons_for_display(9999)
        ranks = [self.shop.rank_index(x["rank"]) for x in weapons]
        low = next(x for x in weapons if self.shop.rank_index(x["rank"]) == min(ranks))
        high = next(x for x in weapons if self.shop.rank_index(x["rank"]) == max(ranks))
        self.assertGreater(self.shop._pick_weight(high, "rare"), self.shop._pick_weight(low, "rare"))
        self.assertGreater(self.shop._pick_weight(low, "common"), self.shop._pick_weight(high, "common"))

    def test_zero_shop_weight_items_stay_out_of_treasure_pavilion(self):
        """shop_weight=0 的天外陨核/帝品材料不应被百宝阁稀有池刷新出来。"""
        self.shop.resolve_rank_band([_Player(22)], force=True)
        excluded = {"天外陨核", "太初道莲", "太初道金", "太古真龙鳞", "太初源液"}
        pool = self.shop.get_treasure_items_for_display(9999)
        matched = [item for item in pool if item["name"] in excluded]
        self.assertTrue(matched)
        self.assertTrue(all(self.shop._pick_weight(item, "rare") == 0 for item in matched))
        for _ in range(50):
            shelf = self.shop.generate_pavilion_items(
                self.shop.get_treasure_items_for_display, 12, pick_mode="rare"
            )
            self.assertFalse({item["name"] for item in shelf} & excluded)

    def test_legacy_shelf_drops_zero_shop_weight_items(self):
        """配置改为 shop_weight=0 后，旧货架条目在访问时应被清理。"""
        shelf = [
            {
                "name": "天外陨核",
                "type": "material",
                "rank": "帝品",
                "price": 30000,
                "stock": 1,
                "data": self.cm.items_data["天外陨核"],
            },
            {
                "name": "灵草",
                "type": "material",
                "rank": "凡品",
                "price": 10,
                "stock": 1,
                "data": {"type": "材料", "shop_weight": 100},
            },
        ]
        kept, changed = self.shop.filter_stale_shelf_items(shelf)
        self.assertTrue(changed)
        self.assertEqual([item["name"] for item in kept], ["灵草"])

    def test_rank_band_keeps_explicit_zero_weight(self):
        """品阶区间加权不能把 shop_weight=0 重新抬成 1。"""
        self.shop.resolve_rank_band([], force=True)
        name, cfg = next(
            (n, c) for n, c in self.cm.items_data.items()
            if isinstance(c, dict) and c.get("type") == "weapon" and c.get("price", 0) > 0
        )
        entry = {
            "name": name,
            "type": "weapon",
            "rank": cfg.get("rank", "凡品"),
            "price": cfg.get("price", 1),
            "data": {**cfg, "shop_weight": 0},
        }
        filtered = self.shop.filter_by_rank_band([entry])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["weight"], 0)

    def test_treasure_rare_pool_filters_all_types_by_band(self):
        self.shop.resolve_rank_band([_Player(0)], force=True)
        items = self.shop.get_treasure_items_for_display(9999)
        self.assertTrue(items)
        self.assertTrue(all(self.shop.rank_index(x["rank"]) == 0 for x in items))

    def test_treasure_shelf_caps_consumable_stock_to_five(self):
        generated = self.shop.generate_pavilion_items(
            self.shop.get_treasure_items_for_display, 9999, pick_mode="rare", stock_cap=5
        )
        consumables = [it for it in generated if it.get("type") in self.shop.CONSUMABLE_SHELF_TYPES]
        self.assertGreater(len(consumables), 0)
        for item in consumables:
            self.assertGreaterEqual(item["stock"], 1)
            self.assertLessEqual(item["stock"], 5, item["name"])

    def test_rare_recipe_weight_prefers_high_value_scrolls(self):
        recipes = self.shop.get_recipes_for_display(9999)
        by_name = {r["name"]: r for r in recipes if r["rare"]}
        self.assertGreater(
            self.shop._pick_weight(by_name["《回春散·天方》"], "rare"),
            self.shop._pick_weight(by_name["《炼气丹方》"], "rare"),
        )


if __name__ == "__main__":
    unittest.main()
