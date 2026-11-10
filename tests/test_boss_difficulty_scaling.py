# tests/test_boss_difficulty_scaling.py
"""世界Boss / 团本 / 江湖事件的境界难度与伤害上限缩放测试。"""

import random
import sys
import types
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

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


class FakeConfig:
    def __init__(self, cfg):
        self.boss_config = cfg


def _shares(picker, rounds=6000):
    random.seed(20261110)
    counts = Counter(picker() for _ in range(rounds))
    return {key: count / rounds for key, count in counts.items()}


class TestRealmFactor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scaling = _imp("core.boss_scaling")

    def test_factor_stays_in_range_and_clamps(self):
        factor = self.scaling.realm_factor
        self.assertEqual(factor(0, 0), 0.0)
        self.assertAlmostEqual(factor(31, 31), 1.0, places=6)
        self.assertAlmostEqual(factor(80, 80), 1.0, places=6)

    def test_factor_grows_with_realm(self):
        factor = self.scaling.realm_factor
        early = factor(12, 9)
        blend = factor(25, 18)
        top = factor(31, 23)
        self.assertLess(early, blend)
        self.assertLess(blend, top)
        self.assertGreater(blend, 0.5)

    def test_max_level_dominates_average(self):
        # 单个渡劫玩家 + 大量低境界小号：仍应保有较高系数，而不是被平均值拖平。
        factor = self.scaling.realm_factor(31, 5)
        self.assertGreater(factor, 0.65)
        self.assertLess(factor, 0.8)

    def test_scaled_damage_cap(self):
        cap = self.scaling.scaled_damage_cap
        self.assertAlmostEqual(cap(20, 0, 0.8, 45), 20.0)
        self.assertAlmostEqual(cap(20, 10, 0.8, 45), 28.0)
        self.assertAlmostEqual(cap(20, 31, 0.8, 45), 44.8)
        self.assertAlmostEqual(cap(20, 999, 0.8, 45), 45.0)
        # 手动覆写基础值高于封顶值时，以基础值为准，避免旧测试/旧配置被压缩。
        self.assertAlmostEqual(cap(100, 0, 1.0, 45), 100.0)


class TestSpawnWeights(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.WorldBossManager = _imp("managers.world_boss_manager").WorldBossManager
        cls.RaidManager = _imp("managers.raid_manager").RaidManager
        cls.BattleEventManager = _imp("managers.battle_event_manager").BattleEventManager

    def _world_boss(self):
        return self.WorldBossManager(
            None, FakeConfig({"world_boss": {}}), None
        )

    def _raid(self):
        return self.RaidManager(
            None, FakeConfig({"raid": {}}), None, None
        )

    def _event(self):
        return self.BattleEventManager(
            None, FakeConfig({"small_event": {"timezone": "UTC"}}), None, None
        )

    def _world_boss_tier_shares(self, max_level, realm):
        mgr = self._world_boss()
        shares = _shares(lambda: mgr._types[mgr._choose_boss_key(max_level, realm)]["tier"])
        return shares

    def test_world_boss_low_realm_keeps_mythic_low(self):
        shares = self._world_boss_tier_shares(12, 0.0)
        self.assertLess(shares.get(3, 0.0), 0.14)
        self.assertGreater(shares.get(1, 0.0), 0.5)

    def test_world_boss_high_realm_raises_mythic(self):
        low = self._world_boss_tier_shares(31, 0.0)
        high = self._world_boss_tier_shares(31, 1.0)
        self.assertLess(low.get(3, 0.0), 0.15)
        self.assertGreater(high.get(3, 0.0), 0.28)
        self.assertGreater(high[3], low[3] * 2.5)

    def test_world_boss_min_level_filter_blocks_high_tier(self):
        # 最高境界 3 的服务器只能刷到灭世级。
        shares = self._world_boss_tier_shares(3, 1.0)
        self.assertEqual(set(shares), {1})

    def test_raid_low_realm_keeps_hard_raid_low(self):
        mgr = self._raid()
        shares = _shares(
            lambda: mgr._types[mgr._choose_raid_key(None, 12, 0.0)]["difficulty"]
        )
        self.assertLess(shares.get(3, 0.0), 0.25)

        low_server = _shares(
            lambda: mgr._types[mgr._choose_raid_key(None, 6, 1.0)]["difficulty"]
        )
        self.assertEqual(set(low_server), {1})

    def test_raid_high_realm_prefers_three_star(self):
        mgr = self._raid()
        low = _shares(lambda: mgr._types[mgr._choose_raid_key(None, 31, 0.0)]["difficulty"])
        high = _shares(lambda: mgr._types[mgr._choose_raid_key(None, 31, 1.0)]["difficulty"])
        self.assertLess(low.get(3, 0.0), 0.25)
        self.assertGreater(high.get(3, 0.0), 0.42)
        self.assertGreater(high[3], low[3])

    def test_event_low_realm_keeps_s_tier_low(self):
        mgr = self._event()
        empty = _shares(lambda: mgr._choose_tier(0, 0.0))
        self.assertNotIn(4, empty)
        self.assertNotIn(5, empty)

        mid = _shares(lambda: mgr._choose_tier(12, 0.0))
        self.assertLess(mid.get(5, 0.0), 0.08)

    def test_event_high_realm_raises_s_tier(self):
        mgr = self._event()
        low = _shares(lambda: mgr._choose_tier(31, 0.0))
        high = _shares(lambda: mgr._choose_tier(31, 1.0))
        self.assertLess(low.get(5, 0.0), 0.08)
        self.assertGreater(high.get(5, 0.0), 0.25)
        self.assertGreater(high[5], low[5] * 3)


class TestDamageCapByRealm(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.WorldBossManager = _imp("managers.world_boss_manager").WorldBossManager
        cls.RaidManager = _imp("managers.raid_manager").RaidManager
        cls.BattleEventManager = _imp("managers.battle_event_manager").BattleEventManager

    def _cap(self, manager, level):
        return manager.effective_damage_cap_pct(SimpleNamespace(level_index=level))

    def test_world_boss_cap_scales_and_clamps(self):
        mgr = self.WorldBossManager(None, FakeConfig({"world_boss": {}}), None)
        self.assertAlmostEqual(self._cap(mgr, 0), 20.0)
        self.assertAlmostEqual(self._cap(mgr, 10), 28.0)
        self.assertAlmostEqual(self._cap(mgr, 31), 44.8)
        self.assertAlmostEqual(self._cap(mgr, 999), 45.0)

    def test_raid_cap_scales_and_clamps(self):
        mgr = self.RaidManager(None, FakeConfig({"raid": {}}), None, None)
        self.assertAlmostEqual(self._cap(mgr, 0), 8.0)
        self.assertAlmostEqual(self._cap(mgr, 10), 14.0)
        self.assertAlmostEqual(self._cap(mgr, 31), 26.6)
        self.assertAlmostEqual(self._cap(mgr, 999), 28.0)

    def test_event_cap_scales_and_clamps(self):
        mgr = self.BattleEventManager(
            None, FakeConfig({"small_event": {"timezone": "UTC"}}), None, None
        )
        self.assertAlmostEqual(self._cap(mgr, 0), 12.0)
        self.assertAlmostEqual(self._cap(mgr, 10), 19.0)
        self.assertAlmostEqual(self._cap(mgr, 31), 33.7)
        self.assertAlmostEqual(self._cap(mgr, 999), 35.0)

    def test_legacy_config_without_new_keys_still_scales(self):
        mgr = self.WorldBossManager(
            None,
            FakeConfig({"world_boss": {"damage_cap_pct": 20.0}}),
            None,
        )
        self.assertAlmostEqual(self._cap(mgr, 0), 20.0)
        self.assertAlmostEqual(self._cap(mgr, 31), 44.8)
        self.assertAlmostEqual(self._cap(mgr, 999), 45.0)

    async def test_calculate_damage_uses_player_level_cap(self):
        mgr = self.WorldBossManager(None, FakeConfig({"world_boss": {}}), None)
        mgr.player_element = lambda player: ""

        async def huge_attack(player):
            return 10**9, 0, 10**9

        mgr.calculate_realtime_components = huge_attack
        max_hp = 1_000_000_000
        _, low_pct, _ = await mgr.calculate_damage(
            SimpleNamespace(level_index=0), max_hp, floor_pct=0
        )
        _, high_pct, _ = await mgr.calculate_damage(
            SimpleNamespace(level_index=31), max_hp, floor_pct=0
        )
        self.assertAlmostEqual(low_pct, 20.0)
        self.assertAlmostEqual(high_pct, 44.8)


if __name__ == "__main__":
    unittest.main()
