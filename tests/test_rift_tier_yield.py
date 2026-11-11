# tests/test_rift_tier_yield.py
# 深档收益：tier_yield 乘产、high_tier_weight_mult 抬最高品阶材料
import asyncio
import json
import random
import sys
import unittest
from collections import Counter
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


ConfigManager = _imp("config_manager").ConfigManager
RiftManager = _imp("managers.rift_manager").RiftManager
Player = _imp("models").Player
Rift = _imp("models_extended").Rift

CFG = json.loads((ROOT / "config" / "rift_config.json").read_text(encoding="utf-8"))
RIFTS = {r["key"]: r for r in CFG["rifts"]}
CATALOG = {m["name"]: m for m in json.loads(
    (ROOT / "config" / "material_catalog.json").read_text(encoding="utf-8")
)["materials"]}


def make_rift(diff: dict, key: str = "tianlei") -> Rift:
    rc = RIFTS[key]
    return Rift(
        rift_id=0,
        rift_name=rc["name"],
        rift_level=int(diff["required_level"]),
        required_level=int(diff["required_level"]),
        rewards=json.dumps(diff, ensure_ascii=False),
        rift_key=rc["key"],
        product=rc.get("product", ""),
        difficulty=int(diff["tier"]),
        duration=int(diff["duration"]),
    )


def make_player(level: int) -> Player:
    p = Player(user_id="sim")
    p.level_index = level
    return p


def diff_of(key: str, tier: int) -> dict:
    return RIFTS[key]["difficulties"][tier - 1]


class TestTierYield(unittest.TestCase):
    def setUp(self):
        self.mgr = RiftManager(None, ConfigManager(ROOT))

    def test_base_rewards_scale_with_tier_yield(self):
        """修为/灵石单趟产出按 tier_yield 放大（三重 1.95）。"""
        diff = dict(diff_of("tianlei", 3))
        diff["exp"] = [1000, 1000]
        diff["gold"] = [500, 500]
        exp, gold = RiftManager._roll_base_rewards(make_rift(diff))
        self.assertEqual(exp, 1950)
        self.assertEqual(gold, 975)

    def test_no_scaling_when_tier_yield_is_one(self):
        diff = dict(diff_of("tianlei", 1))
        diff["exp"] = [1000, 1000]
        diff["gold"] = [500, 500]
        exp, gold = RiftManager._roll_base_rewards(make_rift(diff))
        self.assertEqual((exp, gold), (1000, 500))

    def test_third_tier_material_counts_beat_second(self):
        """同为天雷绝域，三重单趟材料件数应明显高于二重（1.95 倍量）。"""
        p = make_player(27)
        random.seed(20261111)
        d2, d3 = diff_of("tianlei", 2), diff_of("tianlei", 3)
        r2, r3 = make_rift(d2), make_rift(d3)
        n2 = n3 = 0
        for _ in range(1200):
            for item in asyncio.run(self.mgr._roll_rift_drops(p, r2, 0, chance_scale=1.0)):
                if len(item) == 2:
                    n2 += int(item[1])
            for item in asyncio.run(self.mgr._roll_rift_drops(p, r3, 0, chance_scale=1.0)):
                if len(item) == 2:
                    n3 += int(item[1])
        ratio = n3 / max(1, n2)
        self.assertGreater(ratio, 1.6, f"三重/二重材料件数只有 {ratio:.2f}")


class TestHighTierWeight(unittest.TestCase):
    def setUp(self):
        self.mgr = RiftManager(None, ConfigManager(ROOT))

    def _sample(self, mult: float, runs: int = 3000) -> Counter:
        diff = dict(diff_of("tianlei", 3))
        diff["high_tier_weight_mult"] = mult
        diff["item_chance"] = 100
        diff["extra_drop_chance"] = 0
        diff["equipment_chance"] = 0
        diff["tier_yield"] = 1.0
        p = make_player(27)
        rift = make_rift(diff)
        random.seed(7788)
        counter: Counter = Counter()
        for _ in range(runs):
            for item in asyncio.run(self.mgr._roll_rift_drops(p, rift, 0, chance_scale=1.0)):
                if len(item) == 2:
                    counter[item[0]] += 1
        return counter

    def test_high_tier_mult_raises_top_rank_share(self):
        """天雷三重的帝品材料（劫雷精粹/九转劫雷液/劫灰玄铁）应因权重放大而变多。"""
        high = {name for name, m in CATALOG.items() if m.get("tier") == "帝品"}
        base = self._sample(1.0)
        boosted = self._sample(2.0)
        share_base = sum(v for k, v in base.items() if k in high) / max(1, sum(base.values()))
        share_new = sum(v for k, v in boosted.items() if k in high) / max(
            1, sum(boosted.values())
        )
        self.assertGreater(share_new, share_base * 1.3, f"{share_base:.3f} → {share_new:.3f}")

    def test_weights_only_touch_top_rank(self):
        """权重放大只作用在最高品阶，低阶材料权重不变。"""
        table = [
            {"name": "九转劫雷液", "weight": 10},  # 帝品
            {"name": "太虚雷髓", "weight": 10},    # 圣品
        ]
        weights = self.mgr._rift_drop_weights(table, 2.0)
        self.assertEqual(weights, [20.0, 10.0])

    def test_mult_one_is_passthrough(self):
        table = [{"name": "九转劫雷液", "weight": 7}, {"name": "太虚雷髓", "weight": 3}]
        self.assertEqual(self.mgr._rift_drop_weights(table, 1.0), [7.0, 3.0])


class TestTierChainSanity(unittest.TestCase):
    """三档的时长与强度要保证三重「爬一档明显赚」。

    一重→二重不在此列：两者的判定基数（item_chance / extra）本来就不同，
    一重靠「短平快」刷次数，不能只用 tier_yield ÷ 时长 比较。
    """

    def test_third_tier_hourly_beats_second(self):
        for rc in CFG["rifts"]:
            yields = [float(d.get("tier_yield") or 1.0) for d in rc["difficulties"]]
            durations = [int(d["duration"]) for d in rc["difficulties"]]
            hourly = [y / (t / 3600.0) for y, t in zip(yields, durations)]
            self.assertLess(hourly[1] * 1.2, hourly[2], rc["name"])


if __name__ == "__main__":
    unittest.main()
