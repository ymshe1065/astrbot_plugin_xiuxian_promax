# tests/test_recovery_config.py
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestRecoveryConfig(unittest.TestCase):
    def test_recovery_pills_have_five_tiers_and_scaled_prices(self):
        data = json.loads((ROOT / "config" / "utility_pills.json").read_text(encoding="utf-8"))
        expected = {
            "回春散·凡": (0.10, 1000), "回春散·黄": (0.20, 3000),
            "回春散·玄": (0.30, 10000), "回春散·地": (0.45, 50000),
            "回春散·天": (1.00, 200000),
            "聚灵散·凡": (0.10, 1000), "聚灵散·黄": (0.20, 3000),
            "聚灵散·玄": (0.30, 10000), "聚灵散·地": (0.45, 50000),
            "聚灵散·天": (1.00, 200000),
        }
        found = {}
        for item in data:
            if item.get("name") in expected:
                key = "restore_hp_percent" if item["name"].startswith("回春散") else "restore_mp_percent"
                found[item["name"]] = (float(item[key]), int(item["price"]))
        self.assertEqual(found, expected)

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


if __name__ == "__main__":
    unittest.main()
