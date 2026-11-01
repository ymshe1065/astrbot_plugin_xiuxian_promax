"""五行属性覆盖与新增功法/装备批次回归。"""
import json
import unittest
from collections import Counter
from pathlib import Path

import test_equipment_instances as fixtures
from test_equipment_instances import ROOT, _imp

ELEMENTS = {"金", "木", "水", "火", "土"}
RANKS = ["凡品", "黄品", "玄品", "灵品", "地品", "天品", "皇品", "圣品", "帝品", "道品", "仙品", "混元先天"]


class TestElementCoverage(unittest.TestCase):
    def _load(self, name):
        return json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))

    def test_all_equipment_has_explicit_element(self):
        files = ["items.json", "items_v2.json", "special_equipment.json", "elemental_equipment.json"]
        checked = 0
        for filename in files:
            data = self._load(filename)
            values = data.values() if isinstance(data, dict) else data
            for cfg in values:
                if not isinstance(cfg, dict):
                    continue
                if cfg.get("type") not in {"weapon", "head", "body", "gloves", "ring", "法器", "武器", "防具", "饰品"}:
                    continue
                self.assertIn(cfg.get("element"), ELEMENTS, cfg.get("name"))
                checked += 1
        self.assertGreaterEqual(checked, 650)

    def test_all_techniques_have_element(self):
        items = self._load("items.json")
        life = [v for v in items.values() if isinstance(v, dict) and v.get("type") == "功法"]
        tech = list(self._load("techniques.json").values())
        for cfg in life + tech:
            self.assertIn(cfg.get("element"), ELEMENTS, cfg.get("name"))

    def test_new_equipment_batch_shape(self):
        gear = self._load("elemental_equipment.json")
        self.assertEqual(len(gear), 80)
        self.assertEqual(Counter(v["rank"] for v in gear.values()).most_common(1)[0][1], 8)
        self.assertEqual(Counter(v["equip_slot"] for v in gear.values()).most_common(1)[0][1], 10)
        self.assertEqual({v["element"] for v in gear.values()}, ELEMENTS)
        for cfg in gear.values():
            self.assertTrue(cfg.get("effects"), cfg.get("name"))
            self.assertGreater(cfg.get("price", 0), 0)
            self.assertIn(cfg.get("profile"), {"physical", "magic", "hybrid", "guardian"})

    def test_new_equipment_effects_respect_gear_caps(self):
        balance = _imp("core.equipment_balance")
        gear = self._load("elemental_equipment.json")
        for cfg in gear.values():
            for key, value in cfg["effects"].items():
                cap = balance.GEAR_EFFECT_CAPS.get(key)
                self.assertIsNotNone(cap, f"{cfg['name']} 未支持字段 {key}")
                self.assertLessEqual(value, cap, f"{cfg['name']} 的 {key}={value} 超过单件上限 {cap}")


class TestTechniqueExpansion(unittest.TestCase):
    def _load(self, name):
        return json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))

    def test_main_techniques_cover_every_rank_and_element(self):
        tech = self._load("techniques.json")
        main = [v for v in tech.values() if v.get("type") == "main_technique"]
        self.assertEqual(len(main), 60)
        matrix = Counter((v["rank"], v["element"]) for v in main)
        for rank in RANKS:
            for element in ELEMENTS:
                self.assertEqual(matrix[(rank, element)], 1, f"{rank}-{element}")

    def test_life_techniques_reach_high_ranks(self):
        items = self._load("items.json")
        tech = self._load("techniques.json")
        life = [v for v in items.values() if isinstance(v, dict) and v.get("type") == "功法"]
        life += [v for v in tech.values() if v.get("type") == "technique" and v.get("life_effects")]
        self.assertEqual(len(life), 203)
        ranks = {v["rank"] for v in life}
        self.assertEqual(ranks, set(RANKS))
        for cfg in life:
            self.assertTrue(cfg.get("life_effects") or float(cfg.get("exp_multiplier", 0) or 0) > 0, cfg.get("name"))

    def test_no_duplicate_item_names(self):
        names = []
        for filename in ("items.json", "items_v2.json", "special_equipment.json",
                         "elemental_equipment.json", "techniques.json"):
            data = self._load(filename)
            values = data.values() if isinstance(data, dict) else data
            names.extend(v["name"] for v in values if isinstance(v, dict) and v.get("name"))
        duplicates = [name for name, count in Counter(names).items() if count > 1]
        self.assertEqual(duplicates, [])


if __name__ == "__main__":
    unittest.main()
