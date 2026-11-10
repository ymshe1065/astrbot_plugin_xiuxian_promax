"""材料阁品阶区间回归：高境界稳定出高品阶材料包，低境界不刷屏帝品。"""
import random
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp, ROOT


class _Player:
    def __init__(self, level_index):
        self.level_index = level_index


class TestMaterialShelfRank(unittest.TestCase):
    def setUp(self):
        _imp("data")
        self.cm = _imp("config_manager").ConfigManager(ROOT)
        self.shop = _imp("core.shop_manager").ShopManager({}, self.cm)

    def _shelf(self, count=20):
        return self.shop.generate_pavilion_items(
            self.shop.get_material_shelf_items, count
        )

    def test_full_material_catalog_is_still_available(self):
        """大 count 仍返回全量材料，供图鉴/测试取“噬魂铁”“矿材包·凡品”。"""
        materials = self.shop.get_materials_for_display(2000)
        names = {item["name"] for item in materials}
        self.assertIn("噬魂铁", names)
        self.assertIn("矿材包·凡品", names)
        self.assertGreaterEqual(len(materials), 200)

    def test_level_27_server_always_shows_top_tier_packages(self):
        """合体后期（level_index=27，帝品区间）材料阁每次至少 1-2 个帝品包。"""
        self.shop.resolve_rank_band([_Player(27)], force=True)
        top_material_names = {
            name for name, item in self.cm.items_data.items()
            if item.get("type") == "材料"
            and self.shop.rank_index(item.get("rank")) >= self.shop.rank_index("帝品")
        }
        self.assertTrue(top_material_names)
        random.seed(20240501)
        for _ in range(30):
            shelf = self._shelf(20)
            names = {item["name"] for item in shelf}
            high = [
                item for item in shelf
                if self.shop.rank_index(item["rank"]) >= self.shop.rank_index("圣品")
            ]
            top_packages = [
                item for item in high
                if item["type"] == "material_package"
                and self.shop.rank_index(item["rank"]) >= self.shop.rank_index("帝品")
            ]
            self.assertGreaterEqual(len(high), 2, [x["name"] for x in shelf])
            self.assertTrue(top_packages, [x["name"] for x in shelf])
            # 帝品散装材料 shop_weight=0，仍只能靠掉落/秘境获取，不能回流货架
            self.assertFalse(names & top_material_names)

    def test_low_level_server_does_not_flood_emperor_items(self):
        """低境界服务器以凡/黄/玄品为主，帝品材料包权重占比极低。"""
        self.shop.resolve_rank_band([_Player(0)], force=True)
        candidates = self.shop.get_material_shelf_items(20)
        total_weight = sum(item["weight"] for item in candidates)
        high_weight = sum(
            item["weight"] for item in candidates
            if self.shop.rank_index(item["rank"]) >= self.shop.rank_index("圣品")
        )
        self.assertGreater(total_weight, 0)
        self.assertLess(high_weight / total_weight, 0.05)

        random.seed(20240502)
        high_seen = 0
        emperor_seen = 0
        for _ in range(30):
            shelf = self._shelf(20)
            high_seen += sum(
                1 for item in shelf
                if self.shop.rank_index(item["rank"]) >= self.shop.rank_index("圣品")
            )
            emperor_seen += sum(1 for item in shelf if item["rank"] == "帝品")
        # 低境界只允许极少量“漏网”的高品阶材料包，不能整屏高阶
        self.assertLessEqual(high_seen, 5)
        self.assertLessEqual(emperor_seen, 3)


if __name__ == "__main__":
    unittest.main()
