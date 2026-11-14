# 品阶切分：12 档阶序是唯一权威，配置里的分布变了必须立刻报出来
import collections
import json
import sys
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


EXPECTED_RANKS = [
    "凡品", "黄品", "玄品", "灵品", "地品", "天品",
    "皇品", "圣品", "帝品", "道品", "仙品", "混元先天",
]


class TestRankOrder(unittest.TestCase):
    def setUp(self):
        self.rules = _imp("rank_rules")

    def test_ladder_matches_frontend_constant(self):
        """前端 app.js 的 GRID_RANKS 是这份阶梯的镜像，顺序错一格，切档就整体错位。"""
        self.assertEqual(list(self.rules.RANK_NAMES), EXPECTED_RANKS)

    def test_legacy_names_fold_into_ladder(self):
        for old, new in self.rules.RANK_ALIASES.items():
            self.assertIn(new, self.rules.RANK_NAMES, f"{old} → {new} 落空了")
        # 数据里已经不再出现旧名，这条保证「万一回流」也能被归一化
        self.assertEqual(self.rules.normalize_rank("天阶"), "圣品")
        self.assertEqual(self.rules.normalize_rank("珍品"), "地品")

    def test_tier_is_monotonic(self):
        """tier 只用于配色分族，但它必须随阶递增，否则颜色会读出「玄品比地品稀有」。"""
        tiers = [self.rules.rank_tier(name) for name in EXPECTED_RANKS]
        self.assertEqual(tiers, sorted(tiers))


class TestConfigRankDistribution(unittest.TestCase):
    """实测分布：数字变了说明配置被改坏，或有人绕过了品阶字段。"""

    @classmethod
    def setUpClass(cls):
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.pill_recipes = json.loads((ROOT / "config" / "pill_recipes.json").read_text(encoding="utf-8"))
        cls.talisman_recipes = json.loads(
            (ROOT / "config" / "talisman_recipes.json").read_text(encoding="utf-8")
        )
        cls.items = json.loads((ROOT / "config" / "items.json").read_text(encoding="utf-8"))

    def test_every_pill_recipe_has_known_rank(self):
        """692 条丹方全部要能查到品阶：empty/未知值会让格子落到「凡品」色条，静悄悄错。"""
        AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
        mgr = AlchemyManager(None, self.cm, None)
        unknown = []
        for recipe in self.pill_recipes:
            rank = mgr._pill_rank_of(recipe.get("output_pill", ""), recipe)
            if rank not in EXPECTED_RANKS:
                unknown.append((recipe.get("id"), recipe.get("name"), rank))
        self.assertEqual(unknown, [], f"{len(unknown)} 条丹方没有合法品阶")
        self.assertEqual(len(self.pill_recipes), 692)

    def test_pill_recipe_rank_spread(self):
        AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
        mgr = AlchemyManager(None, self.cm, None)
        counter = collections.Counter(
            mgr._pill_rank_of(r.get("output_pill", ""), r) for r in self.pill_recipes
        )
        self.assertEqual(len(counter), 12, f"档数变了：{sorted(counter)}")
        self.assertEqual(counter["凡品"], 265)
        self.assertEqual(counter["混元先天"], 15)

    def test_talisman_recipes_use_four_ranks(self):
        ranks = {r.get("rank") for r in self.talisman_recipes}
        self.assertEqual(ranks, {"凡品", "黄品", "玄品", "圣品"})
        self.assertEqual(len(self.talisman_recipes), 35)

    def test_item_library_rank_spread(self):
        values = [v for v in self.items.values() if isinstance(v, dict)]
        counter = collections.Counter(v.get("rank") for v in values)
        self.assertEqual(
            dict(counter),
            {"凡品": 467, "黄品": 252, "玄品": 156, "圣品": 80, "帝品": 17},
        )


class TestBucketOrdering(unittest.TestCase):
    """分组顺序必须来自 RANK_NAMES，不是字符串排序（「凡品」会排到「帝品」后面）。"""

    @classmethod
    def setUpClass(cls):
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.mgr = _imp("core.shop_manager").ShopManager({}, cls.cm)

    def test_groups_follow_ladder(self):
        items = [
            {"name": "甲", "type": "recipe", "rank": "帝品"},
            {"name": "乙", "type": "recipe", "rank": "凡品"},
            {"name": "丙", "type": "recipe", "rank": "混元先天"},
            {"name": "丁", "type": "recipe", "rank": "黄品"},
        ]
        groups = self.mgr.group_items_by_rank(items)
        self.assertEqual([rank for rank, _ in groups], ["凡品", "黄品", "帝品", "混元先天"])

    def test_groups_merge_legacy_names(self):
        items = [
            {"name": "甲", "type": "recipe", "rank": "天阶"},
            {"name": "乙", "type": "recipe", "rank": "圣品"},
        ]
        groups = self.mgr.group_items_by_rank(items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0], "圣品")
        self.assertEqual(len(groups[0][1]), 2)


if __name__ == "__main__":
    unittest.main()
