# tests/test_world_boss_dual_track.py
import sys
import types
import unittest
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
    boss_config = {"world_boss": {}}


class TestWorldBossDualTrack(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.WorldBossManager = _imp("managers.world_boss_manager").WorldBossManager

    def _manager(self):
        return self.WorldBossManager(None, FakeConfig(), None)

    def test_resist_profiles_are_distinct(self):
        mgr = self._manager()
        dragon = mgr.get_resists({"boss_type": "abyss_dragon"})
        demon = mgr.get_resists({"boss_type": "ancient_demon"})
        self.assertGreater(dragon[0], dragon[1])
        self.assertGreater(demon[1], demon[0])

    async def test_damage_uses_boss_resistance(self):
        mgr = self._manager()
        player = SimpleNamespace(user_id="u1", experience=100000)
        mgr.player_element = lambda p: ""

        async def physical_only(p):
            return 1000, 0, 1000

        mgr.calculate_realtime_components = physical_only
        dragon_damage, _, _ = await mgr.calculate_damage(
            player, 1_000_000, boss={"boss_type": "abyss_dragon"},
            floor_pct=0, cap_pct=100,
        )
        demon_damage, _, _ = await mgr.calculate_damage(
            player, 1_000_000, boss={"boss_type": "ancient_demon"},
            floor_pct=0, cap_pct=100,
        )
        self.assertGreater(demon_damage, dragon_damage)

        async def magic_only(p):
            return 0, 1000, 1000

        mgr.calculate_realtime_components = magic_only
        dragon_damage, _, _ = await mgr.calculate_damage(
            player, 1_000_000, boss={"boss_type": "abyss_dragon"},
            floor_pct=0, cap_pct=100,
        )
        demon_damage, _, _ = await mgr.calculate_damage(
            player, 1_000_000, boss={"boss_type": "ancient_demon"},
            floor_pct=0, cap_pct=100,
        )
        self.assertGreater(dragon_damage, demon_damage)


if __name__ == "__main__":
    unittest.main()
