# tests/test_rift_equipment_drop_nerf.py
# 秘境高阶（越阶）装备掉率削弱：越阶权重、按秘境等级配置、旧配置兼容、收益上限

import json
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))

from test_equipment_instances import _imp, ROOT as PKG_ROOT  # noqa: E402

equipment_drop = _imp("core.equipment_drop")
item_valuation = _imp("core.item_valuation")
rank_rules = _imp("rank_rules")
RiftManager = _imp("managers.rift_manager").RiftManager
default_configs = _imp("data.default_configs")


class _FakeConfigManager:
    """只带 rift_config 的假配置，避免加载整套配置。"""

    def __init__(self, rift_config):
        self.rift_config = rift_config


class TestUpRankWeight(unittest.TestCase):
    """越阶权重只削「基准 +1」，不改变低阶与基准阶的比例。"""

    @classmethod
    def setUpClass(cls):
        cls.cm = _imp("config_manager").ConfigManager(PKG_ROOT)
        cls._pool = equipment_drop.build_equipment_pool(cls.cm)
        cls._orig_pool = equipment_drop.build_equipment_pool
        equipment_drop.build_equipment_pool = lambda *a, **k: cls._pool

    @classmethod
    def tearDownClass(cls):
        equipment_drop.build_equipment_pool = cls._orig_pool

    def _shares(self, up_weight, level=15, n=4000, seed=20261102):
        base = rank_rules.level_to_rank(level)
        base_index = rank_rules.RANK_NAMES.index(base)
        up_rank = rank_rules.RANK_NAMES[base_index + 1]
        below_rank = rank_rules.RANK_NAMES[base_index - 1]
        rng = random.Random(seed)
        counts = {"below": 0, "base": 0, "up": 0}
        for _ in range(n):
            drop = equipment_drop.roll_equipment_drop(
                self.cm, level, None, rng, up_rank_weight=up_weight
            )
            self.assertIsNotNone(drop)
            if drop["rank"] == up_rank:
                counts["up"] += 1
            elif drop["rank"] == base:
                counts["base"] += 1
            elif drop["rank"] == below_rank:
                counts["below"] += 1
        return {k: v / n for k, v in counts.items()}, up_rank

    def test_default_keeps_twenty_percent(self):
        shares, _ = self._shares(equipment_drop.DEFAULT_UP_RANK_WEIGHT)
        self.assertAlmostEqual(shares["up"], 0.20, delta=0.03)
        self.assertAlmostEqual(shares["base"], 0.60, delta=0.04)

    def test_quarter_weight_cuts_up_share(self):
        shares, _ = self._shares(0.25)
        # 1 : 3 : 0.25 → 越阶 4/17 ≈ 5.9%
        self.assertAlmostEqual(shares["up"], 0.0588, delta=0.02)

    def test_zero_weight_never_drops_up_rank(self):
        shares, up_rank = self._shares(0.0)
        self.assertEqual(shares["up"], 0.0, f"up_weight=0 仍掉了 {up_rank}")
        self.assertAlmostEqual(shares["base"], 0.75, delta=0.03)

    def test_lower_rank_ratio_untouched(self):
        """低阶 : 基准 = 1 : 3 不随越阶权重变化。"""
        for weight in (1.0, 0.25, 0.0):
            shares, _ = self._shares(weight, n=3000)
            self.assertAlmostEqual(shares["below"] / shares["base"], 1 / 3, delta=0.06)

    def test_invalid_weight_falls_back(self):
        shares, _ = self._shares("bad-value")
        self.assertAlmostEqual(shares["up"], 0.20, delta=0.03)


class TestRiftWeightConfig(unittest.TestCase):
    """秘境按等级读取越阶权重，旧配置缺键时回退 1.0。"""

    def test_per_level_mapping(self):
        mgr = RiftManager(None, _FakeConfigManager({
            "equipment_up_rank_weight": {"1": 1.0, "2": 0.7, "3": 0.5, "4": 0.35, "5": 0.25},
        }), None)
        self.assertEqual(mgr._equipment_up_rank_weight(1), 1.0)
        self.assertEqual(mgr._equipment_up_rank_weight(5), 0.25)
        # 越深越难出越阶装备
        values = [mgr._equipment_up_rank_weight(i) for i in range(1, 6)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_scalar_applies_to_all_levels(self):
        mgr = RiftManager(None, _FakeConfigManager({"equipment_up_rank_weight": 0.4}), None)
        for level in range(1, 6):
            self.assertEqual(mgr._equipment_up_rank_weight(level), 0.4)

    def test_missing_key_uses_default_mapping(self):
        """旧配置缺 equipment_up_rank_weight 时用缺省映射，同样吃到削弱。"""
        mgr = RiftManager(None, _FakeConfigManager({"equipment_drop_chances": {"5": 22}}), None)
        self.assertEqual(
            mgr._equipment_up_rank_weight(5),
            default_configs.RIFT_CONFIG["equipment_up_rank_weight"]["5"],
        )

    def test_old_config_still_drops_equipment(self):
        """旧版 rift_config.json 没有新键，缺省补齐后掉率不应归零。"""
        mgr = RiftManager(None, _FakeConfigManager({
            "equipment_drop_chances": {"1": 6, "2": 10, "3": 15, "4": 18, "5": 22},
        }), None)
        self.assertEqual(mgr.config["equipment_drop_chances"]["5"], 22)
        self.assertGreater(mgr.config["equipment_up_rank_weight"]["5"], 0)
        self.assertEqual(mgr.explore_duration, 1800)

    def test_no_config_manager_still_works(self):
        mgr = RiftManager(None, None, None)
        self.assertGreater(mgr._equipment_up_rank_weight(5), 0)
        self.assertEqual(mgr.explore_duration, 1800)


class TestShippedConfig(unittest.TestCase):
    """仓库里的配置与代码缺省值必须一致，避免两处漂移。"""

    def setUp(self):
        self.path = PKG_ROOT / "config" / "rift_config.json"
        with open(self.path, encoding="utf-8") as f:
            self.data = json.load(f)

    def test_chances_match_defaults(self):
        self.assertEqual(
            self.data["equipment_drop_chances"],
            default_configs.RIFT_CONFIG["equipment_drop_chances"],
        )

    def test_up_rank_weight_matches_defaults(self):
        self.assertEqual(
            self.data["equipment_up_rank_weight"],
            default_configs.RIFT_CONFIG["equipment_up_rank_weight"],
        )

    def test_high_rifts_are_nerfed(self):
        """新版高阶秘境掉率必须低于旧版，防止被改回去。"""
        old = {"1": 6, "2": 10, "3": 15, "4": 18, "5": 22}
        for level, chance in self.data["equipment_drop_chances"].items():
            if int(level) >= 3:
                self.assertLessEqual(chance, old[level])
        self.assertLess(self.data["equipment_drop_chances"]["5"], old["5"])


class TestRiftFiveIncome(unittest.TestCase):
    """秘境5 装备回收收益上限：单趟不超过金币收益的 2 倍。"""

    GOLD_RANGE = (5000, 20000)  # 上古遗迹 gold_range
    PLAYER_LEVEL = 15           # 进入门槛 required_level

    @classmethod
    def setUpClass(cls):
        cls.cm = _imp("config_manager").ConfigManager(PKG_ROOT)
        cls._pool = equipment_drop.build_equipment_pool(cls.cm)
        cls._orig_pool = equipment_drop.build_equipment_pool
        equipment_drop.build_equipment_pool = lambda *a, **k: cls._pool
        with open(PKG_ROOT / "config" / "rift_config.json", encoding="utf-8") as f:
            cls.cfg = json.load(f)

    @classmethod
    def tearDownClass(cls):
        equipment_drop.build_equipment_pool = cls._orig_pool

    def test_hourly_recycle_within_budget(self):
        chance = int(self.cfg["equipment_drop_chances"]["5"]) / 100
        weight = float(self.cfg["equipment_up_rank_weight"]["5"])
        rng = random.Random(4242)
        n = 6000
        total = 0
        for _ in range(n):
            drop = equipment_drop.roll_equipment_drop(
                self.cm, self.PLAYER_LEVEL, None, rng, up_rank_weight=weight
            )
            if drop:
                total += item_valuation.recycle_value(
                    self.cm, drop["name"], drop["quality"], None
                )
        per_run = total / n * chance
        gold_per_run = sum(self.GOLD_RANGE) / 2
        self.assertLessEqual(
            per_run, gold_per_run * 2,
            f"秘境5 单趟装备回收期望 {per_run:,.0f} 超过金币 {gold_per_run:,.0f} 的 2 倍",
        )


if __name__ == "__main__":
    unittest.main()
