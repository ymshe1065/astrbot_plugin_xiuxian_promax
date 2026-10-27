# tests/test_boss_archetypes.py
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
    boss_config = {
        "archetypes": {
            "warrior": {
                "name": "狂战型", "weight": 30, "atk_mult": 1.1, "defense_mult": 0.9,
                "physical_atk_share": 0.8, "magic_atk_share": 0.2,
                "physical_defense_share": 0.7, "magic_defense_share": 0.3,
                "defense_flat": 30,
            },
            "mage": {
                "name": "法尊型", "weight": 30, "atk_mult": 1.1, "defense_mult": 0.8,
                "physical_atk_share": 0.2, "magic_atk_share": 0.8,
                "physical_defense_share": 0.3, "magic_defense_share": 0.7,
                "defense_flat": 20,
            },
        }
    }


class TestBossArchetypes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.BossManager = _imp("managers.boss_manager").BossManager
        cls.CombatManager = _imp("managers.combat_manager").CombatManager
        cls.Boss = _imp("models_extended").Boss

    def test_archetype_splits_physical_and_magic(self):
        mgr = self.BossManager(None, self.CombatManager(), FakeConfig())
        base = self.Boss(
            boss_id=1, boss_name="测试", boss_level="金丹", hp=100000, max_hp=100000,
            atk=1000, defense=100, stone_reward=1000, archetype="warrior",
        )
        warrior = mgr.build_boss_combat_stats(base)
        base.archetype = "mage"
        mage = mgr.build_boss_combat_stats(base)
        self.assertGreater(warrior.physical_atk, warrior.magic_atk)
        self.assertGreater(mage.magic_atk, mage.physical_atk)
        self.assertGreater(warrior.physical_defense, warrior.magic_defense)
        self.assertGreater(mage.magic_defense, mage.physical_defense)

    def test_pick_archetype_returns_configured_key(self):
        mgr = self.BossManager(None, self.CombatManager(), FakeConfig())
        for _ in range(20):
            self.assertIn(mgr.pick_archetype(), {"warrior", "mage"})


if __name__ == "__main__":
    unittest.main()
