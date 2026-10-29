# 统一品阶回归测试：唯一名称阶梯、旧名兼容与配置数据一致性
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

    def exception(self, *a, **k):
        pass


api_mod.logger = _StubLogger()
api_mod.AstrBotConfig = dict
event_mod.AstrMessageEvent = object
api_mod.event = event_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod)

from astrbot_plugin_xiuxian_promax.rank_rules import (  # noqa: E402
    RANK_ALIASES,
    RANK_GOLD,
    RANK_LEVELS,
    RANK_NAMES,
    RANK_POWER,
    RANK_PRICE,
    RANK_TABLE,
    RANK_TIERS,
    format_rank,
    is_known_rank,
    level_to_rank,
    normalize_rank,
    rank_level,
    rank_tier,
)
from astrbot_plugin_xiuxian_promax.core.item_rules import get_required_level  # noqa: E402
from astrbot_plugin_xiuxian_promax.core.talisman_rules import (  # noqa: E402
    TIER_GOLD,
    TIER_LEVEL,
    TIER_POWER,
    TIER_PRICE,
)

LEGACY_TOKENS = ("黄阶", "玄阶", "地阶", "天阶", "珍品", "神品")


class TestRankLadder(unittest.TestCase):
    def test_ladder_order_and_levels(self):
        self.assertEqual(
            RANK_NAMES,
            (
                "凡品", "黄品", "玄品", "灵品", "地品", "天品",
                "皇品", "圣品", "帝品", "道品", "仙品", "混元先天",
            ),
        )
        self.assertEqual(
            [RANK_LEVELS[name] for name in RANK_NAMES],
            [0, 3, 9, 10, 12, 13, 16, 18, 22, 28, 32, 35],
        )

    def test_tier_and_values_are_monotonic(self):
        self.assertEqual(
            [RANK_TIERS[name] for name in RANK_NAMES],
            [1, 2, 3, 3, 3, 4, 4, 4, 4, 4, 5, 5],
        )
        for name, tier in RANK_TIERS.items():
            self.assertIn(tier, (1, 2, 3, 4, 5), name)
        for table in (RANK_POWER, RANK_GOLD, RANK_PRICE):
            values = [table[name] for name in RANK_NAMES]
            self.assertEqual(values, sorted(values), values)

    def test_legacy_aliases(self):
        self.assertEqual(
            RANK_ALIASES,
            {
                "黄阶": "凡品",
                "玄阶": "黄品",
                "地阶": "玄品",
                "天阶": "圣品",
                "珍品": "地品",
                "神品": "仙品",
            },
        )
        for legacy, canonical in RANK_ALIASES.items():
            self.assertEqual(normalize_rank(legacy), canonical)
            self.assertTrue(is_known_rank(legacy))
            self.assertEqual(rank_level(legacy), RANK_LEVELS[canonical])
            self.assertEqual(rank_tier(legacy), RANK_TIERS[canonical])

    def test_legacy_tiers_are_preserved(self):
        """迁移前的稀有度分档应尽量保持不变，仅灵品因阶梯顺序合并上移一档。"""
        legacy_tiers = {
            "黄阶": 1, "玄阶": 2, "地阶": 3, "天阶": 4,
            "凡品": 1, "珍品": 3, "地品": 3, "天品": 4,
            "皇品": 4, "圣品": 4, "帝品": 4, "道品": 4,
            "仙品": 5, "神品": 5, "混元先天": 5,
        }
        for name, tier in legacy_tiers.items():
            self.assertEqual(rank_tier(name), tier, name)
        self.assertEqual(rank_tier("灵品"), 3)

    def test_normalize_unknown_and_empty(self):
        self.assertEqual(normalize_rank(None), "")
        self.assertEqual(normalize_rank("  "), "")
        self.assertEqual(normalize_rank("", "凡品"), "凡品")
        self.assertEqual(normalize_rank("不明品阶"), "不明品阶")
        self.assertFalse(is_known_rank("不明品阶"))
        self.assertEqual(format_rank("不明品阶"), "未知")

    def test_level_to_rank(self):
        self.assertEqual(level_to_rank(-1), "凡品")
        self.assertEqual(level_to_rank(0), "凡品")
        self.assertEqual(level_to_rank(2), "凡品")
        self.assertEqual(level_to_rank(3), "黄品")
        self.assertEqual(level_to_rank(14), "天品")
        self.assertEqual(level_to_rank(35), "混元先天")
        self.assertEqual(level_to_rank(99), "混元先天")

    def test_item_rules_accept_legacy_and_canonical(self):
        self.assertEqual(get_required_level({"rank": "圣品"}), 18)
        self.assertEqual(get_required_level({"rank": "天阶"}), 18)
        self.assertEqual(get_required_level({"rank": "黄品"}), 3)
        self.assertEqual(get_required_level({"rank": "玄阶"}), 3)
        self.assertEqual(get_required_level({"rank": "圣品", "required_level_index": 13}), 13)

    def test_talisman_tables_share_the_ladder(self):
        for name in RANK_NAMES:
            self.assertEqual(TIER_POWER[name], RANK_POWER[name])
            self.assertEqual(TIER_LEVEL[name], RANK_LEVELS[name])
            self.assertEqual(TIER_GOLD[name], RANK_GOLD[name])
            self.assertEqual(TIER_PRICE[name], RANK_PRICE[name])
        for legacy, canonical in RANK_ALIASES.items():
            self.assertEqual(TIER_POWER[legacy], RANK_POWER[canonical])


class TestConfigRanks(unittest.TestCase):
    def test_all_config_ranks_are_canonical(self):
        config_dir = ROOT / "config"
        for path in sorted(config_dir.glob("*.json")):
            if path.name in ("level_config.json", "body_level_config.json", "pill_quality.json"):
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            ranks = set()
            self._collect(data, ranks)
            for rank in ranks:
                self.assertIn(
                    rank, RANK_NAMES,
                    f"{path.name} 含非标准品阶：{rank}",
                )

    def test_no_legacy_tokens_remain_in_config(self):
        for path in sorted((ROOT / "config").glob("*.json")):
            text = path.read_text(encoding="utf-8")
            for token in LEGACY_TOKENS:
                self.assertNotIn(token, text, f"{path.name} 仍含旧品阶名 {token}")

    @staticmethod
    def _collect(node, ranks):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("rank", "tier") and isinstance(value, str) and value:
                    ranks.add(value)
                else:
                    TestConfigRanks._collect(value, ranks)
        elif isinstance(node, list):
            for value in node:
                TestConfigRanks._collect(value, ranks)


if __name__ == "__main__":
    unittest.main()
