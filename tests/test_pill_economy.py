# tests/test_pill_economy.py
# 丹药价格与炼制成本平衡回归测试
import math
import statistics
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


def ceil_to_nice(value: float) -> int:
    """把价格向上取整到人类可读的台阶。"""
    if value < 1_000:
        step = 10
    elif value < 10_000:
        step = 100
    elif value < 1_000_000:
        step = 1_000
    elif value < 100_000_000:
        step = 10_000
    else:
        step = 100_000
    return max(step, int(math.ceil(value / step) * step))


class PillEconomyRules:
    """按炼制成本为丹阁价格提供统一参考。"""

    MARGIN = 1.5
    SHOP_DISCOUNT_MIN = 0.8
    TRIBULATION_SURVIVAL = 0.75

    def __init__(self, config_manager):
        self.cm = config_manager
        self.material_groups = {}
        for material in (self.cm.material_catalog or {}).values():
            key = (material.get("category"), material.get("tier"))
            self.material_groups.setdefault(key, []).append(
                int(material.get("price", 0) or 0)
            )

    def pill(self, name: str):
        for data in (
            self.cm.pills_data,
            self.cm.exp_pills_data,
            self.cm.utility_pills_data,
        ):
            if name in data:
                return data[name]
        item = (self.cm.items_data or {}).get(name)
        return item if isinstance(item, dict) and item.get("type") == "丹药" else None

    def material_value(self, spec) -> int:
        if isinstance(spec, str):
            name, count, category, tier = spec, 1, None, None
        elif isinstance(spec, dict):
            name = spec.get("name")
            count = int(spec.get("count", 1) or 1)
            category = spec.get("category")
            tier = spec.get("tier")
        else:
            raise AssertionError(f"无法识别的材料配置: {spec!r}")

        if name:
            material = (self.cm.material_catalog or {}).get(name, {})
            price = int(material.get("price", 0) or 0)
            if price <= 0:
                item = (self.cm.items_data or {}).get(name, {})
                price = int(item.get("price", 0) or 0)
            if price <= 0:
                raise AssertionError(f"材料 {name} 缺少价格")
            return count * price

        values = self.material_groups.get((category, tier), [])
        if not values:
            raise AssertionError(f"材料类别 {category}/{tier} 缺少参考价")
        return count * int(statistics.median(values))

    def expected_cost(self, recipe: dict) -> float:
        materials = recipe.get("materials", {})
        gold_cost = int(recipe.get("gold_cost", 0) or 0)

        specs = []
        if isinstance(materials, dict):
            if "灵石" in materials:
                gold_cost = int(materials.get("灵石", gold_cost) or gold_cost)
            specs = [(name, count) for name, count in materials.items() if name != "灵石"]
        elif isinstance(materials, list):
            specs = materials
        else:
            raise AssertionError(f"配方 {recipe.get('name')} 材料格式无效")

        material_cost = 0
        for spec in specs:
            if isinstance(spec, tuple):
                material_cost += self.material_value({"name": spec[0], "count": spec[1]})
            else:
                material_cost += self.material_value(spec)

        success = float(recipe.get("success_rate", 0) or 0) / 100.0
        if success <= 0:
            raise AssertionError(f"配方 {recipe.get('name')} 成功率无效")

        tribulation = (
            self.TRIBULATION_SURVIVAL
            if int(recipe.get("level_required", 0) or 0) >= 6
            else 1.0
        )
        return (material_cost + gold_cost) / (success * tribulation)

    def target_price(self, recipe: dict) -> int:
        return ceil_to_nice(self.expected_cost(recipe) * self.MARGIN)


class TestPillEconomy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.ConfigManager = _imp("config_manager").ConfigManager
        cls.cm = cls.ConfigManager(ROOT)
        cls.rules = PillEconomyRules(cls.cm)
        cls.recipes = list((cls.cm.pill_recipes_data or {}).values())

    def test_all_recipe_outputs_have_pills(self):
        self.assertEqual(len(self.recipes), 42)
        for recipe in self.recipes:
            pill = self.rules.pill(recipe.get("output_pill", ""))
            self.assertIsNotNone(pill, f"缺少成品丹药: {recipe.get('output_pill')}")

    def test_shop_price_is_above_expected_craft_cost(self):
        checked = 0
        for recipe in self.recipes:
            pill = self.rules.pill(recipe.get("output_pill", ""))
            if not pill:
                continue
            price = int(pill.get("price", 0) or 0)
            expected = self.rules.expected_cost(recipe)
            target = self.rules.target_price(recipe)
            self.assertGreaterEqual(
                price,
                target,
                f"{recipe['name']} 购买价 {price} 低于炼制目标价 {target}",
            )
            # 即使刷到最低折扣，购买仍应明显高于期望炼制成本。
            self.assertGreaterEqual(
                price * self.rules.SHOP_DISCOUNT_MIN,
                expected * 1.15,
                f"{recipe['name']} 折后价不足以覆盖炼制成本",
            )
            checked += 1
        self.assertEqual(checked, 42)
