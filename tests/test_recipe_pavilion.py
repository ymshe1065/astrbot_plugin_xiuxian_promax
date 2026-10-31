# 丹方阁常驻突破丹方与稀有丹方轮换回归测试
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


class TestRecipePavilion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.config_manager = _imp("config_manager").ConfigManager(ROOT)
        cls.shop_manager = _imp("core.shop_manager").ShopManager({}, cls.config_manager)

    def test_breakthrough_recipes_are_in_dedicated_section(self):
        recipes = self.shop_manager.get_recipes_for_display(10)
        breakthrough = self.shop_manager.get_breakthrough_recipes_for_display()
        self.assertTrue(all(r["rare"] for r in recipes))
        self.assertTrue(all(r["always_available"] for r in breakthrough))
        self.assertEqual(len(breakthrough), len(self.config_manager.pills_data))
        self.assertIn("《筑基丹方》", {r["name"] for r in breakthrough})
        self.assertIn("《炼气丹方》", {r["name"] for r in recipes})
        # 初始已学丹方不上架
        self.assertNotIn("《回春散·凡方》", {r["name"] for r in recipes + breakthrough})
        self.assertNotIn("《聚灵散·凡方》", {r["name"] for r in recipes + breakthrough})

    def test_pavilion_keeps_all_permanent_and_rotates_rare(self):
        generated = self.shop_manager.generate_pavilion_items(
            self.shop_manager.get_recipes_for_display, 10
        )
        permanent = [i for i in generated if i["always_available"]]
        rare = [i for i in generated if i["rare"]]
        self.assertEqual(len(permanent), 0)
        self.assertEqual(len(rare), 10)
        breakthrough = self.shop_manager.generate_pavilion_items(
            self.shop_manager.get_breakthrough_recipes_for_display, 0
        )
        self.assertEqual(len(breakthrough), len(self.config_manager.pills_data))

    def test_stale_recipe_price_is_refreshed_with_discount(self):
        item = {
            "name": "《筑基丹方》", "type": "recipe", "rank": "灵品",
            "price": 18553, "original_price": 15500, "discount": 1.2,
            "stock": 2,
            "data": {"name": "《筑基丹方》", "type": "丹方", "price": 15500, "recipe_id": 101},
        }
        changed = self.shop_manager.refresh_item_data([item])
        self.assertTrue(changed)
        current = self.config_manager.items_data["《筑基丹方》"]
        self.assertEqual(item["original_price"], int(current["price"]))
        self.assertEqual(item["price"], int(current["price"] * 1.2))
        self.assertEqual(item["stock"], 2)

    def test_recipe_price_below_output_pill(self):
        sources = (
            self.config_manager.pills_data,
            self.config_manager.exp_pills_data,
            self.config_manager.utility_pills_data,
            self.config_manager.items_data,
        )
        for recipe in self.config_manager.pill_recipes_data.values():
            output = recipe.get("output_pill")
            pill = next((source[output] for source in sources if output in source), None)
            self.assertIsNotNone(pill, f"未找到丹方成品：{output}")
            self.assertLess(
                int(recipe.get("price") or 0),
                int(pill.get("price") or 0),
                f"{recipe.get('name')} 价格不应高于成品 {output}",
            )

    def test_breakthrough_recipe_price_grows_with_level(self):
        recipes = sorted(
            (r for r in self.config_manager.pill_recipes_data.values() if 101 <= int(r.get("id", 0)) <= 126),
            key=lambda r: int(r.get("level_required", 0)),
        )
        prices = [int(r.get("price") or 0) for r in recipes]
        self.assertEqual(prices, sorted(prices))
        self.assertEqual(len(prices), len(set(prices)))


if __name__ == "__main__":
    unittest.main()
