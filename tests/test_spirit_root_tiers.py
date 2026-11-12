# tests/test_spirit_root_tiers.py
# 灵根阶位阶梯、飞升重塑「必出高一档」保底、灵根图鉴/说明查询
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


SPEEDS = {
    "PSEUDO_ROOT_SPEED": 0.7,
    "QUAD_ROOT_SPEED": 0.8,
    "TRI_ROOT_SPEED": 0.95,
    "DUAL_ROOT_SPEED": 1.15,
    "WUXING_ROOT_SPEED": 1.3,
    "THUNDER_ROOT_SPEED": 1.65,
    "ICE_ROOT_SPEED": 1.6,
    "WIND_ROOT_SPEED": 1.6,
    "DARK_ROOT_SPEED": 1.65,
    "LIGHT_ROOT_SPEED": 1.65,
    "HEAVENLY_ROOT_SPEED": 1.9,
    "YIN_YANG_ROOT_SPEED": 2.3,
    "FUSION_ROOT_SPEED": 2.3,
    "CHAOS_ROOT_SPEED": 2.55,
    "INNATE_BODY_SPEED": 3.2,
    "DIVINE_BODY_SPEED": 2.95,
}

CONFIG = {
    "VALUES": {"INITIAL_GOLD": 1000},
    "SPIRIT_ROOT_WEIGHTS": {},
    "SPIRIT_ROOT_SPEEDS": SPEEDS,
}


class TestTierLadder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        CultivationManager = _imp("core.cultivation_manager").CultivationManager
        cls.cm = CultivationManager(CONFIG, None)

    def test_ladder_is_ordered(self):
        cm = self.cm
        order = ["伪", "金木", "金", "雷", "天雷", "阴阳", "混沌", "先天道体"]
        indexes = [cm.root_tier_index(root) for root in order]
        self.assertEqual(indexes, sorted(indexes))
        self.assertEqual(cm.root_tier_index("伪"), 0)
        self.assertEqual(cm.root_tier_index("先天道体"), len(cm.root_tier_order) - 1)

    def test_name_with_suffix_is_tolerated(self):
        self.assertEqual(self.cm.root_tier_index("雷灵根"), self.cm.root_tier_index("雷"))

    def test_upgrade_is_exactly_one_tier(self):
        cm = self.cm
        dual = set(cm.root_tier_roots(cm.root_tier_index("金木") + 1))
        self.assertIn(cm.upgrade_root_for("金木"), dual)          # 双灵根 → 五行单灵根
        heavenly = set(cm.root_tier_roots(cm.root_tier_index("雷") + 1))
        self.assertIn(cm.upgrade_root_for("雷"), heavenly)        # 变异 → 天灵根
        top = cm.root_tier_order[-1][1]
        self.assertEqual(cm.upgrade_root_for("先天道体"), "")     # 已最高档，不再升
        self.assertEqual(cm.upgrade_root_for("不存在的灵根"), "")

    def test_draw_chance_sums_to_one(self):
        total = sum(
            self.cm.root_draw_chance(root)
            for idx in range(len(self.cm.root_tier_order))
            for root in self.cm.root_tier_roots(idx)
        )
        self.assertAlmostEqual(total, 1.0, places=9)

    def test_thunder_is_rarer_than_single_element(self):
        self.assertLess(self.cm.root_draw_chance("雷"), self.cm.root_draw_chance("金"))


class TestUpgradeGuarantee(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        CultivationManager = _imp("core.cultivation_manager").CultivationManager
        cls.cm = CultivationManager(CONFIG, None)

    def _next_tier(self, base):
        return set(self.cm.root_tier_roots(self.cm.root_tier_index(base) + 1))

    def test_guaranteed_root_always_present(self):
        for _ in range(50):
            roots = self.cm.draw_spiritual_roots(3, upgrade_from="金木水火")
            self.assertEqual(len(roots), 3)
            self.assertEqual(len(set(roots)), 3, roots)
            self.assertTrue(set(roots) & self._next_tier("金木水火"), roots)

    def test_guarantee_follows_previous_life_root(self):
        for _ in range(30):
            roots = self.cm.draw_spiritual_roots(3, upgrade_from="雷")
            self.assertTrue(set(roots) & self._next_tier("雷"), roots)

    def test_top_tier_has_no_guarantee_but_still_draws(self):
        roots = self.cm.draw_spiritual_roots(3, upgrade_from="先天道体")
        self.assertEqual(len(roots), 3)
        self.assertEqual(len(set(roots)), 3)

    def test_no_base_means_plain_draw(self):
        roots = self.cm.draw_spiritual_roots(3)
        self.assertEqual(len(roots), 3)
        self.assertEqual(len(set(roots)), 3)


class TestRootInfoCommand(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        PlayerHandler = _imp("handlers.player_handler").PlayerHandler
        cls.handler = PlayerHandler(None, CONFIG, None)

    def test_detail_shows_tier_speed_and_five_elements(self):
        text = self.handler._format_root_detail("雷")
        self.assertIn("灵根说明 · 雷", text)
        self.assertIn("变异灵根·稀有", text)
        self.assertIn("×1.65", text)
        self.assertIn("五行归属：木", text)   # 雷 → 木
        self.assertIn("雷→木", text)
        self.assertIn("高一档：天灵根", text)

    def test_detail_maps_variant_roots(self):
        self.assertIn("五行归属：水", self.handler._format_root_detail("冰"))
        self.assertIn("五行归属：火", self.handler._format_root_detail("光"))
        self.assertIn("五行归属：木", self.handler._format_root_detail("风"))

    def test_detail_splits_multi_root_elements(self):
        text = self.handler._format_root_detail("金木水火")
        self.assertIn("五行归属：金、木、水、火", text)
        self.assertIn("1/4 分摊", text)

    def test_chaos_covers_all_five(self):
        text = self.handler._format_root_detail("混沌")
        self.assertIn("五行归属：金、木、水、火、土", text)
        self.assertIn("已是最高档", self.handler._format_root_detail("先天道体"))

    def test_atlas_lists_every_tier(self):
        text = self.handler._format_root_atlas()
        self.assertIn("共 10 档", text)
        for idx in range(1, 11):
            self.assertIn(f"{idx}. 【", text)
        self.assertIn("灵根说明 雷", text)

    def test_unknown_root_points_to_atlas(self):
        text = self.handler._format_root_detail("雷雷雷")
        self.assertIn("没找到灵根", text)
        self.assertIn("灵根图鉴", text)


if __name__ == "__main__":
    unittest.main()
