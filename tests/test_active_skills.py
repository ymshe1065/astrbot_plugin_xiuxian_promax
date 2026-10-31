"""主修心法主动技能与灵气资源、灵兽加成展示回归。"""
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


class TestActiveSkillConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.techniques = json.loads(
            (ROOT / "config" / "techniques.json").read_text(encoding="utf-8")
        )

    def test_all_main_techniques_have_active_skill(self):
        main = [v for v in self.techniques.values() if v.get("type") == "main_technique"]
        self.assertEqual(len(main), 20)
        for cfg in main:
            skill = cfg.get("active_skill")
            self.assertIsNotNone(skill, cfg.get("name"))
            self.assertEqual(skill.get("trigger_round"), 2)
            self.assertEqual(
                skill.get("mp_cost"),
                {"凡品": 500, "黄品": 4000, "玄品": 20000, "圣品": 100000}[cfg["rank"]],
                cfg.get("name"),
            )


class TestActiveSkillEngine(unittest.TestCase):
    def setUp(self):
        self.CombatStats = _imp("managers.combat_manager").CombatStats
        self.CombatManager = _imp("managers.combat_manager").CombatManager

    def _stats(self, uid, name, skill=None, mp=1000, max_mp=1000):
        return self.CombatStats(
            user_id=uid, name=name, hp=10000, max_hp=10000, mp=mp, max_mp=max_mp,
            atk=1000, defense=100, physical_atk=1000, magic_atk=0,
            physical_defense=100, magic_defense=0, effects={},
            active_skill=skill or {},
        )

    def test_pvp_triggers_skill_once_and_drains_mp(self):
        pierce = {"name": "裂金破甲斩", "trigger_round": 2, "mp_cost": 500,
                  "type": "pierce", "value": 0.2, "extra_damage": 0.5}
        p1 = self._stats("u1", "甲", pierce, mp=600, max_mp=1000)
        p2 = self._stats("u2", "乙")
        result = self.CombatManager.player_vs_player(p1, p2, combat_type=1)
        log = "\n".join(result["combat_log"])
        self.assertIn("裂金破甲斩", log)
        self.assertEqual(p1.skill_casts, 1)
        self.assertLessEqual(p1.mp, 600)

    def test_freeze_and_burn_statuses(self):
        freeze = {"name": "玄冰凝霜", "trigger_round": 2, "mp_cost": 500, "type": "freeze", "value": 0.5}
        burn = {"name": "离火燎原", "trigger_round": 2, "mp_cost": 500, "type": "burn", "value": 0.05}
        attacker = self._stats("u1", "甲")
        defender = self._stats("u2", "乙")
        self.CombatManager._apply_active_skill(attacker, defender, freeze, [])
        self.assertIn("freeze", defender.statuses)
        self.CombatManager._apply_active_skill(attacker, defender, burn, [])
        self.assertIn("burn", defender.statuses)

    def test_heal_and_guard(self):
        heal = {"name": "青木回春", "trigger_round": 2, "mp_cost": 500, "type": "heal", "value": 0.2}
        guard = {"name": "不动明王", "trigger_round": 2, "mp_cost": 500, "type": "guard", "value": 0.3}
        attacker = self._stats("u1", "甲")
        attacker.hp = 2000
        self.CombatManager._apply_active_skill(attacker, self._stats("u2", "乙"), heal, [])
        self.assertGreater(attacker.hp, 2000)
        attacker2 = self._stats("u3", "丙")
        self.CombatManager._apply_active_skill(attacker2, self._stats("u4", "丁"), guard, [])
        self.assertIn("guard", attacker2.statuses)
        self.assertGreater(attacker2.atk, 1000)


class TestPetBonusDisplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.PetManager = _imp("managers.pet_manager").PetManager
        cls.manager = cls.PetManager(None, None)

    def _pet(self, pet_type, level, dead_until=0):
        return {"pet_type": pet_type, "level": level, "dead_until": dead_until}

    def test_describe_bonus_and_mechanic(self):
        fox = self._pet("火灵狐", 5)
        text = self.manager.describe_pet_bonus(fox)
        self.assertIn("暴击率", text)
        self.assertIn("灼烧", self.manager.describe_pet_mechanic(fox))
        rat = self._pet("噬金鼠", 5)
        self.assertIn("灵石收益", self.manager.describe_pet_bonus(rat))
        dead = self._pet("火灵狐", 5, dead_until=10**12)
        self.assertIn("暂停", self.manager.describe_pet_bonus(dead))

    def test_aggregate_skips_dead_pets(self):
        pets = [self._pet("火灵狐", 5), self._pet("噬金鼠", 3), self._pet("火灵狐", 5, dead_until=10**12)]
        totals = self.manager.aggregate_pet_bonuses(pets)
        self.assertGreater(totals.get("crit_rate", 0), 0)
        self.assertGreater(totals.get("gold_bonus", 0), 0)


class TestRestRegenNerf(unittest.TestCase):
    def test_rest_rate_and_caps(self):
        recovery = _imp("managers.recovery_manager").RecoveryManager
        self.assertEqual(recovery.REST_RATE, 0.03)
        self.assertEqual(recovery.REST_QI_BONUS_CAP, 1.0)


if __name__ == "__main__":
    unittest.main()
