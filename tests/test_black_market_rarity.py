# 黑市稀有度与溢价回归测试
import unittest

import test_recipe_pavilion as stub
from test_recipe_pavilion import ROOT

_imp = stub._imp


class TestBlackMarketRarity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.gm_module = _imp("managers.gamble_manager")
        cls.gm = cls.gm_module.GambleManager(None, cls.cm, None)

    def test_listings_are_rare_and_high_ranked(self):
        fake = 0
        high_ranked = 0
        total = 0
        original_time = self.gm_module.time.time
        try:
            for hour in range(100):
                self.gm_module.time.time = lambda h=hour: h * 3600 + 10
                items, _ = self.gm._get_black_market_items()
                self.assertEqual(len(items), 3)
                for item in items:
                    total += 1
                    if item.get("fake"):
                        fake += 1
                    elif self.gm._rank_index(item.get("rank", "")) >= 6:
                        high_ranked += 1
        finally:
            self.gm_module.time.time = original_time
        # 真品中圣品及以上应占多数，假货只占少数
        self.assertGreaterEqual(high_ranked / max(1, total - fake), 0.6)
        self.assertLessEqual(fake / max(1, total), 0.15)

    def test_rare_listings_carry_heavy_premium(self):
        original_time = self.gm_module.time.time
        try:
            for hour in range(20):
                self.gm_module.time.time = lambda h=hour: h * 3600 + 10
                items, _ = self.gm._get_black_market_items()
                for item in items:
                    if item.get("fake"):
                        continue
                    base = max(1, int(item["data"].get("price", 0) or 0))
                    self.assertGreater(item["price"], base * 1.5, item["name"])
        finally:
            self.gm_module.time.time = original_time


if __name__ == "__main__":
    unittest.main()
