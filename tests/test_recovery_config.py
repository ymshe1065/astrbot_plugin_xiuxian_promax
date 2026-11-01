# tests/test_recovery_config.py
import json
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


class TestRecoveryConfig(unittest.TestCase):
    def test_recovery_pills_have_five_tiers_and_scaled_prices(self):
        data = json.loads((ROOT / "config" / "utility_pills.json").read_text(encoding="utf-8"))
        expected_percent = {
            "回春散·凡": 0.10, "回春散·黄": 0.20, "回春散·玄": 0.30,
            "回春散·地": 0.45, "回春散·天": 1.00,
            "聚灵散·凡": 0.10, "聚灵散·黄": 0.20, "聚灵散·玄": 0.30,
            "聚灵散·地": 0.45, "聚灵散·天": 1.00,
        }
        found = {}
        for item in data:
            if item.get("name") in expected_percent:
                key = "restore_hp_percent" if item["name"].startswith("回春散") else "restore_qi_percent"
                found[item["name"]] = (float(item[key]), int(item["price"]))
        for name, (percent, price) in found.items():
            self.assertEqual(percent, expected_percent[name], name)
            self.assertGreater(price, 0, name)
        order = ["凡", "黄", "玄", "地", "天"]
        for prefix in ("回春散", "聚灵散"):
            prices = [found[f"{prefix}·{tier}"][1] for tier in order]
            self.assertEqual(prices, sorted(prices), f"{prefix} 价格应随品阶递增")

    def test_recovery_recipes_exist_for_all_tiers(self):
        data = json.loads((ROOT / "config" / "pill_recipes.json").read_text(encoding="utf-8"))
        outputs = {item.get("output_pill") for item in data if isinstance(item, dict)}
        for name in (
            "回春散·凡", "回春散·黄", "回春散·玄", "回春散·地", "回春散·天",
            "聚灵散·凡", "聚灵散·黄", "聚灵散·玄", "聚灵散·地", "聚灵散·天",
        ):
            self.assertIn(name, outputs)

    def test_recovery_equipment_has_bonus_effects(self):
        data = json.loads((ROOT / "config" / "items.json").read_text(encoding="utf-8"))
        by_name = {v.get("name"): v for v in data.values() if isinstance(v, dict)}
        for name in ("回春杖", "回春甲", "聚灵袍", "长春冠", "灵泉法衣", "回元心经"):
            self.assertIn(name, by_name)
            self.assertTrue(by_name[name].get("effects"))


class TestRestRateText(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.RecoveryManager = _imp("managers.recovery_manager").RecoveryManager
        cls.RecoveryHandler = _imp("handlers.recovery_handler").RecoveryHandler
        cls.Player = _imp("models").Player

    async def test_rest_rates_apply_bonus_and_cap(self):
        mgr = self.RecoveryManager(None, None)
        player = self.Player(user_id="u1", user_name="t")

        async def fake_bonuses(p):
            return {"rest_hp_regen_bonus": 0.30, "rest_qi_regen_bonus": 2.0}

        mgr.get_bonuses = fake_bonuses
        rates = await mgr.get_rest_rates(player)
        self.assertAlmostEqual(rates["hp_rate"], 0.039, places=4)
        self.assertAlmostEqual(rates["qi_bonus"], 1.0, places=4)
        self.assertAlmostEqual(rates["qi_rate"], 0.06, places=4)

    async def test_rest_prompt_shows_actual_rate_and_bonus(self):
        handler = self.RecoveryHandler(None, None)
        player = self.Player(user_id="u1", user_name="t")
        RecoveryManager = self.RecoveryManager

        async def fake_bonuses(self, p):
            return {"rest_hp_regen_bonus": 0.30, "rest_qi_regen_bonus": 1.0}

        original = RecoveryManager.get_bonuses
        RecoveryManager.get_bonuses = fake_bonuses
        try:
            text = await handler._rest_rate_text(player)
        finally:
            RecoveryManager.get_bonuses = original
        self.assertIn("气血 3.9%", text)
        self.assertIn("灵气 6.0%", text)
        self.assertIn("气血加成：+30%", text)
        self.assertIn("灵气加成：+100%", text)
        self.assertIn("封顶 +100%", text)
        self.assertNotIn("5%", text)


if __name__ == "__main__":
    unittest.main()
