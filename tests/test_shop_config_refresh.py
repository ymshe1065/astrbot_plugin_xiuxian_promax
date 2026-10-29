# tests/test_shop_config_refresh.py
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")


class _StubLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


api_mod.logger = _StubLogger()
api_mod.AstrBotConfig = dict
event_mod.AstrMessageEvent = object
api_mod.event = event_mod
all_mod = types.ModuleType("astrbot.api.all")
all_mod.At = object
all_mod.Plain = object
api_mod.all = all_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod)
sys.modules.setdefault("astrbot.api.all", all_mod)


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestShopConfigRefresh(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.ConfigManager = _imp("config_manager").ConfigManager
        cls.ShopManager = _imp("core.shop_manager").ShopManager
        cls.cm = cls.ConfigManager(ROOT)
        cls.sm = cls.ShopManager({}, cls.cm)

    def test_stale_technique_data_is_rehydrated(self):
        old_price = 19500
        old_stock = 1
        item = {
            "name": "九天精铁诀",
            "type": "technique",
            "rank": "玄品",
            "price": old_price,
            "stock": old_stock,
            "data": {
                "id": "1416",
                "name": "九天精铁诀",
                "type": "功法",
                "rank": "玄品",
                "exp_multiplier": 2.0,
            },
        }
        changed = self.sm.refresh_item_data([item])
        self.assertTrue(changed)
        self.assertEqual(item["data"]["exp_multiplier"], 0.05)
        self.assertEqual(item["price"], item["data"]["price"])
        self.assertEqual(item["original_price"], item["data"]["price"])
        self.assertEqual(item["stock"], old_stock)
        text = self.sm._get_item_effect_short(item)
        self.assertIn("修炼效率+5%", text)
        self.assertNotIn("200%", text)

    def test_stale_recipe_price_is_updated(self):
        recipe = next(
            item for item in self.cm.items_data.values()
            if item.get("type") == "丹方"
        )
        old_data = dict(recipe)
        old_data["price"] = 99999
        item = {
            "name": recipe["name"],
            "type": "recipe",
            "rank": recipe.get("rank", "灵品"),
            "price": 79999,
            "original_price": 99999,
            "discount": 0.8,
            "stock": 3,
            "data": old_data,
        }

        changed = self.sm.refresh_item_data([item])

        self.assertTrue(changed)
        self.assertEqual(item["original_price"], recipe["price"])
        self.assertEqual(item["price"], int(recipe["price"] * 0.8))
        self.assertEqual(item["data"]["price"], recipe["price"])
        self.assertEqual(item["stock"], 3)

    def test_technique_config_integrity(self):
        count = 0
        for item in self.cm.items_data.values():
            if item.get("type") != "功法":
                continue
            count += 1
            self.assertIn(item.get("rank"), {"凡品", "黄品", "玄品", "圣品"})
            exp = float(item.get("exp_multiplier", 0) or 0)
            self.assertGreaterEqual(exp, 0)
            self.assertLessEqual(exp, 0.2)
            self.assertGreater(int(item.get("price", 0)), 0)
            self.assertTrue(item.get("life_effects") or exp > 0, item.get("name"))
        self.assertEqual(count, 155)


if __name__ == "__main__":
    unittest.main()
