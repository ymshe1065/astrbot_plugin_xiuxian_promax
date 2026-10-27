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

    def test_breakthrough_recipes_are_permanent(self):
        recipes = self.shop_manager.get_recipes_for_display(3)
        permanent = [r for r in recipes if r["always_available"]]
        rare = [r for r in recipes if r["rare"]]
        self.assertEqual(len(permanent) + len(rare), len(recipes))
        self.assertEqual(len(permanent), len(self.config_manager.pills_data))
        self.assertTrue(all(r["type"] == "recipe" for r in permanent))
        self.assertIn("《筑基丹方》", {r["name"] for r in permanent})
        self.assertIn("《炼气丹方》", {r["name"] for r in rare})

    def test_pavilion_keeps_all_permanent_and_rotates_rare(self):
        generated = self.shop_manager.generate_pavilion_items(
            self.shop_manager.get_recipes_for_display, 3
        )
        permanent = [i for i in generated if i["always_available"]]
        rare = [i for i in generated if i["rare"]]
        self.assertEqual(len(permanent), len(self.config_manager.pills_data))
        self.assertEqual(len(rare), 3)
        self.assertEqual(len(generated), len(permanent) + len(rare))

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
