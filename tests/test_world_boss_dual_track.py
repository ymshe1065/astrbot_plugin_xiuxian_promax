# tests/test_world_boss_dual_track.py
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


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

    def test_world_boss_attack_and_defense_are_dual(self):
        mgr = self._manager()
        boss = {
            "boss_id": 1, "boss_name": "深渊魔龙", "max_hp": 1_000_000,
            "current_hp": 1_000_000, "atk": 100_000, "defense": 50,
            "boss_type": "abyss_dragon", "element": "水",
        }
        stats = mgr.build_world_boss_combat_stats(boss, {"element": "水"})
        self.assertGreater(stats.physical_atk, stats.magic_atk)
        self.assertGreater(stats.physical_defense, stats.magic_defense)
        self.assertEqual(stats.atk, stats.physical_atk + stats.magic_atk)
        self.assertEqual(stats.defense, stats.physical_defense + stats.magic_defense)

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

    async def test_simulate_battle_returns_combat_log(self):
        mgr = self._manager()
        CombatManager = _imp("managers.combat_manager").CombatManager
        CombatStats = _imp("managers.combat_manager").CombatStats

        def make(self_, name, hp, atk, element):
            return CombatStats(
                user_id=name, name=name, hp=hp, max_hp=hp, mp=1000, max_mp=1000,
                atk=atk, defense=100, physical_atk=atk, magic_atk=0,
                physical_defense=100, magic_defense=0, effects={}, element=element,
            )

        async def build_player(player):
            return make(None, "玩家", 5000, 1200, "金")

        def build_boss(boss, type_cfg):
            return make(None, "深渊魔龙", 8000, 900, "木")

        mgr.build_player_boss_stats = build_player
        mgr.build_world_boss_combat_stats = build_boss
        mgr.damage_floor_pct = 0
        mgr.damage_cap_pct = 100
        # v6.57.0: simulate_battle 已纯函数化（模拟过程零落库）；db 置 None 作守卫，触碰数据库会立即报错
        mgr.db = None
        player = SimpleNamespace(user_id="u1", hp=5000, mp=1000, spiritual_qi=1000)
        boss = {"boss_id": 1, "boss_name": "深渊魔龙", "max_hp": 1_000_000,
                "current_hp": 1_000_000, "boss_type": "abyss_dragon", "element": "木"}
        result = await mgr.simulate_battle(player, boss, {"element": "木"})
        # 新契约：战损气血/灵气随结果返回，由调用方在事务提交后写回
        self.assertIn("player_final_hp", result)
        self.assertIn("player_final_mp", result)
        log = "\n".join(result["combat_log"])
        self.assertIn("战斗开始", log)
        self.assertIn("第 1 回合", log)
        self.assertTrue(any("施展" in line or "五行" in line for line in result["combat_log"]))


if __name__ == "__main__":
    unittest.main()
