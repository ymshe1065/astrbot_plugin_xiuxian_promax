# tests/test_pill_economy.py
# 丹药价格与炼制成本平衡回归测试
import math
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


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
        # 每个 (类别, 品阶) 的参考价取「目录里第一个出现的材料」，与符箓口径一致：
        # 新增材料一律追加在末尾，参考价不会因扩容而漂移。
        self.material_groups = {}
        for material in (self.cm.material_catalog or {}).values():
            key = (material.get("category"), material.get("tier"))
            self.material_groups.setdefault(key, int(material.get("price", 0) or 0))

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

        price = self.material_groups.get((category, tier), 0)
        if not price:
            raise AssertionError(f"材料类别 {category}/{tier} 缺少参考价")
        return count * int(price)

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
        self.assertGreaterEqual(len(self.recipes), 200)
        for recipe in self.recipes:
            pill = self.rules.pill(recipe.get("output_pill", ""))
            self.assertIsNotNone(pill, f"缺少成品丹药: {recipe.get('output_pill')}")

    def test_high_rank_recipes_use_emperor_materials(self):
        """道品以上必须吃帝品主材料，混元先天额外吃周本专属材料。"""
        checked = 0
        for recipe in self.recipes:
            pill = self.rules.pill(recipe.get("output_pill", ""))
            if not pill or pill.get("rank") not in ("道品", "仙品", "混元先天"):
                continue
            specs = [s for s in (recipe.get("materials") or []) if isinstance(s, dict)]
            self.assertTrue(
                any(not s.get("name") and s.get("tier") == "帝品" for s in specs),
                f"{recipe['name']} 没有帝品主材料：{specs}",
            )
            if pill.get("rank") == "混元先天":
                self.assertTrue(
                    any(s.get("name") == "天外陨核" for s in specs),
                    f"{recipe['name']} 缺少天外陨核",
                )
            checked += 1
        self.assertGreaterEqual(checked, 30)

    def test_high_rank_pill_tiers_are_complete(self):
        """圣品补上皇品与帝品之间的断档，道品以上至少覆盖五类常用效果。"""
        exp_ranks = {p.get("rank") for p in self.cm.exp_pills_data.values()}
        self.assertIn("圣品", exp_ranks)
        expected = {
            "cultivation_boost", "permanent_attribute", "combat_boost",
            "defensive_boost", "recovery", "breakthrough_boost", "protection",
        }
        for rank in ("圣品", "道品", "仙品", "混元先天"):
            subtypes = {
                p.get("subtype") for p in self.cm.utility_pills_data.values()
                if p.get("rank") == rank
            }
            missing = expected - subtypes
            self.assertFalse(missing, f"{rank} 缺少效果类别：{sorted(missing)}")

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
        self.assertEqual(checked, len(self.recipes))
