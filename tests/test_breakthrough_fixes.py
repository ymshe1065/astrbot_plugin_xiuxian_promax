# tests/test_breakthrough_fixes.py
# 突破机制回归测试：功德修正、死亡概率配置校验、未实际突破不消耗资源
import asyncio
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


class TestBreakthroughFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data.migration")
        cls.ConfigManager = _imp("config_manager").ConfigManager
        cls.BreakthroughManager = _imp("core.breakthrough_manager").BreakthroughManager
        cls.Player = _imp("models").Player
        cls.config_manager = cls.ConfigManager(ROOT)

    def _manager(self, config=None):
        return self.BreakthroughManager(None, self.config_manager, config or {})

    def test_karma_is_not_overwritten_by_pill(self):
        """破境丹加成不能覆盖负业力惩罚。"""
        player = self.Player(user_id="u1", level_index=16, cultivation_type="灵修", karma=-10)
        rate, _ = self._manager().calculate_breakthrough_success_rate(
            player, "养婴丹·中", 0.0, None, 1.0
        )
        # 基础 0.22 + 丹药 0.76 - 业力 0.10 = 0.88，而不是旧逻辑的 0.98
        self.assertAlmostEqual(rate, 0.88, places=6)

    def test_positive_karma_still_capped_by_pill_max(self):
        player = self.Player(user_id="u1", level_index=16, cultivation_type="灵修", karma=10)
        rate, _ = self._manager().calculate_breakthrough_success_rate(
            player, "养婴丹·中", 0.0, None, 1.0
        )
        self.assertAlmostEqual(rate, 0.98, places=6)

    def test_death_probability_range_is_validated(self):
        cases = [
            ({"VALUES": {"BREAKTHROUGH_DEATH_PROBABILITY": [0.2]}}, (0.01, 0.1)),
            ({"VALUES": {"BREAKTHROUGH_DEATH_PROBABILITY": [0.9, 0.1]}}, (0.1, 0.9)),
            ({"VALUES": {"BREAKTHROUGH_DEATH_PROBABILITY": [2, -1]}}, (0.0, 1.0)),
            ({"VALUES": {"BREAKTHROUGH_DEATH_PROBABILITY": 0.05}}, (0.05, 0.05)),
            ({"VALUES": {"BREAKTHROUGH_DEATH_PROBABILITY": "bad"}}, (0.01, 0.1)),
        ]
        for config, expected in cases:
            with self.subTest(config=config):
                self.assertEqual(self._manager(config)._get_death_probability_range(), expected)

    def test_body_death_multiplier_is_validated(self):
        cases = [
            ({}, 1.5),
            ({"VALUES": {"BODY_BREAKTHROUGH_DEATH_MULTIPLIER": 1.2}}, 1.2),
            ({"VALUES": {"BODY_BREAKTHROUGH_DEATH_MULTIPLIER": "bad"}}, 1.5),
            ({"VALUES": {"BODY_BREAKTHROUGH_DEATH_MULTIPLIER": -1}}, 0.0),
        ]
        for config, expected in cases:
            with self.subTest(config=config):
                self.assertEqual(self._manager(config)._get_body_death_multiplier(), expected)

    def test_precondition_failure_returns_not_attempted(self):
        manager = self._manager()
        player = self.Player(user_id="u1", level_index=0, experience=0, gold=1000)
        success, _, died, attempted = asyncio.run(
            manager.execute_breakthrough(player)
        )
        self.assertFalse(success)
        self.assertFalse(died)
        self.assertFalse(attempted)

    def test_missing_pill_returns_not_attempted(self):
        manager = self._manager()
        player = self.Player(user_id="u1", level_index=0, experience=500, gold=1000)
        success, _, died, attempted = asyncio.run(
            manager.execute_breakthrough(player, "筑基丹")
        )
        self.assertFalse(success)
        self.assertFalse(died)
        self.assertFalse(attempted)


if __name__ == "__main__":
    unittest.main()
