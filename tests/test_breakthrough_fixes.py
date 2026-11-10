# tests/test_breakthrough_fixes.py
# 突破机制回归测试：功德修正、死亡概率配置校验、未实际突破不消耗资源
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class _DummyDB:
    async def update_player(self, player):
        return None


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

    def test_boost_pill_extends_breakthrough_pill_cap(self):
        """增益丹加成应叠加在破境丹上限之外，而不是被上限吞掉。"""
        player = self.Player(user_id="u1", level_index=23, cultivation_type="灵修")
        # 破虚丹·后：基础 0.08 + 丹药 0.77 = 0.85（正好顶满丹药上限）
        rate_without_boost, _ = self._manager().calculate_breakthrough_success_rate(
            player, "破虚丹·后", 0.0, None, 1.0
        )
        self.assertAlmostEqual(rate_without_boost, 0.85, places=6)

        # 七品化神增益丹 +20%：上限 0.85 -> 1.00，最终 1.00
        rate_with_boost, info = self._manager().calculate_breakthrough_success_rate(
            player, "破虚丹·后", 0.2, None, 1.0
        )
        self.assertAlmostEqual(rate_with_boost, 1.0, places=6)
        self.assertIn("增益丹额外提升上限", info)

    def test_effective_max_success_rate_is_clamped(self):
        self.assertEqual(self._manager().get_effective_max_success_rate(0.85, 0.2), 1.0)
        self.assertAlmostEqual(self._manager().get_effective_max_success_rate(0.7, 0.1), 0.8)
        self.assertAlmostEqual(self._manager().get_effective_max_success_rate(0.9, -0.2), 0.9)

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

    def test_breakthrough_does_not_require_gold(self):
        manager = self.BreakthroughManager(_DummyDB(), self.config_manager, {})
        player = self.Player(user_id="u1", level_index=0, experience=500, gold=0)
        with patch("random.random", return_value=0.0):
            success, message, died, attempted = asyncio.run(
                manager.execute_breakthrough(player)
            )
        self.assertTrue(success)
        self.assertTrue(attempted)
        self.assertFalse(died)
        self.assertEqual(player.gold, 0)
        self.assertNotIn("资费", message)

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
