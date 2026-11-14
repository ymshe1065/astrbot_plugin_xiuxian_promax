# tests/test_exp_pill_ladder.py
# 修为丹梯度回归（v3.45.17）
#
# 背景：四象神木心液（圣品 · 炼气18 · 170 万修为）是自动生成物，数值明显高于
# 同品阶的策划丹（圣元丹 35 万），导致「高阶丹吃不过一颗圣品散丹」。
# 这里以它为新锚点，把圣品及以上的修为丹重排成一条严格单调的梯子：
#   1. 四象自己的数值不动（作为锚）；
#   2. 圣品及以上、同境界或更高的修为丹必须高于 170 万；
#   3. 按境界单调递增，相邻档位倍率落在 1.0~1.6；
#   4. 单颗丹药的修为不得超过该境界升级需求（不能一颗跳一级）；
#   5. 价格与四象保持一致口径：只抬修为，不动价。
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"

ANCHOR_NAME = "四象神木心液"
ANCHOR_EXP = 1_700_000
ANCHOR_LEVEL = 18

# 品阶序（与 rank_rules.RANK_NAMES 一致）
RANKS = [
    "凡品", "黄品", "玄品", "灵品", "地品", "天品",
    "皇品", "圣品", "帝品", "道品", "仙品", "混元先天",
]
SAINT_OR_ABOVE = set(RANKS[RANKS.index("圣品"):])

# 按境界差归一化后的每级倍率（相邻档位可能隔 2~3 级）
STEP_MIN = 1.0
STEP_MAX_PER_LEVEL = 1.3

TAIJI_NAME = "太极阴阳丹"
TAIJI_MIN_RATIO = 5.0      # 太极至少是四象的 5 倍
TAIJI_PRICE = 839_000      # 本次只抬修为，价格不动


def _load(name: str):
    return json.loads((CONFIG / name).read_text(encoding="utf-8"))


class ExpPillLadderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        items = _load("items.json")
        cls.anchor = next(
            v for v in items.values() if isinstance(v, dict) and v.get("name") == ANCHOR_NAME
        )
        cls.levels = _load("level_config.json")
        cls.pills = _load("exp_pills.json")
        cls.ladder = sorted(
            [p for p in cls.pills if p.get("rank") in SAINT_OR_ABOVE],
            key=lambda p: (p["required_level_index"], p["exp_gain"]),
        )

    def test_anchor_is_untouched(self):
        """四象作为锚点，本次不动它的修为。"""
        self.assertEqual(self.anchor["effect"]["add_experience"], ANCHOR_EXP)

    def test_saint_and_above_beat_anchor(self):
        """圣品及以上的修为丹，修为都要高于四象这颗圣品散丹。"""
        low = [
            (p["name"], p["exp_gain"])
            for p in self.ladder
            if p["required_level_index"] >= ANCHOR_LEVEL and p["exp_gain"] <= ANCHOR_EXP
        ]
        self.assertEqual(low, [], f"以下丹药修为不高于四象 {ANCHOR_EXP:,}：{low}")

    def test_ladder_is_monotonic(self):
        """按境界排序后修为严格递增，不允许倒挂。"""
        bad = []
        for prev, cur in zip(self.ladder, self.ladder[1:]):
            if cur["exp_gain"] <= prev["exp_gain"]:
                bad.append((prev["name"], prev["exp_gain"], cur["name"], cur["exp_gain"]))
        self.assertEqual(bad, [], f"修为梯子倒挂：{bad}")

    def test_step_ratio_in_range(self):
        """相邻档位按境界差归一化后，每级倍率不超过 1.3、且不得持平。"""
        bad = []
        for prev, cur in zip(self.ladder, self.ladder[1:]):
            gap = max(1, cur["required_level_index"] - prev["required_level_index"])
            ratio = (cur["exp_gain"] / prev["exp_gain"]) ** (1.0 / gap)
            if not (STEP_MIN < ratio <= STEP_MAX_PER_LEVEL):
                bad.append((prev["name"], cur["name"], round(ratio, 3)))
        self.assertEqual(bad, [], f"每级倍率越界：{bad}")

    def test_taiji_clearly_above_anchor(self):
        """太极阴阳丹（道品 · 28）必须明显高于四象，而不是只高 47%。"""
        taiji = next(p for p in self.pills if p["name"] == TAIJI_NAME)
        self.assertGreaterEqual(taiji["exp_gain"], ANCHOR_EXP * TAIJI_MIN_RATIO)

    def test_pill_never_instantly_levels(self):
        """圣品及以上，单颗丹药的修为不超过该境界升级需求（低境界的老丹不在此列）。"""
        bad = []
        for pill in self.ladder:
            level = int(pill.get("required_level_index") or 0)
            need = int(self.levels[level].get("exp_needed") or 0)
            if need and pill["exp_gain"] >= need:
                bad.append((pill["name"], pill["exp_gain"], need))
        self.assertEqual(bad, [], f"单颗越级：{bad}")

    def test_price_untouched(self):
        """本轮只抬修为，太极的售价保持原值。"""
        taiji = next(p for p in self.pills if p["name"] == TAIJI_NAME)
        self.assertEqual(taiji["price"], TAIJI_PRICE)


if __name__ == "__main__":
    unittest.main()
