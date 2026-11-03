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
        cls.ranks = [
            "凡品", "黄品", "玄品", "灵品", "地品", "天品",
            "皇品", "圣品", "帝品", "道品", "仙品", "混元先天",
        ]

    def test_all_main_techniques_have_active_skill(self):
        main = [v for v in self.techniques.values() if v.get("type") == "main_technique"]
        self.assertEqual(len(main), 60)
        self.assertEqual(
            {cfg["rank"] for cfg in main},
            {"凡品", "黄品", "玄品", "灵品", "地品", "天品", "皇品", "圣品", "帝品", "道品", "仙品", "混元先天"},
        )
        mp_cost = {
            "凡品": 500, "黄品": 4000, "玄品": 20000, "灵品": 40000,
            "地品": 55000, "天品": 70000, "皇品": 90000, "圣品": 100000,
            "帝品": 150000, "道品": 250000, "仙品": 450000, "混元先天": 800000,
        }
        for cfg in main:
            skill = cfg.get("active_skill")
            self.assertIsNotNone(skill, cfg.get("name"))
            self.assertEqual(skill.get("trigger_round"), 2)
            self.assertEqual(skill.get("mp_cost"), mp_cost[cfg["rank"]], cfg.get("name"))
            self.assertIn(skill.get("type"), {"pierce", "heal", "freeze", "burn", "guard"}, cfg.get("name"))

    def test_skill_values_progress_with_rank(self):
        """同系绝技随品阶递进，不再出现圣品弱于帝品、玄品弱于灵品等倒挂。"""
        for element in ("金", "木", "水", "火", "土"):
            rows = sorted(
                (
                    v for v in self.techniques.values()
                    if v.get("type") == "main_technique" and v.get("element") == element
                ),
                key=lambda cfg: self.ranks.index(cfg["rank"]),
            )
            values = [float(cfg["active_skill"]["value"]) for cfg in rows]
            if element == "水":
                # 水系 value 是“行动概率”，越高阶应越低（0 = 完全冻结）
                self.assertEqual(values, sorted(values, reverse=True), element)
            else:
                self.assertEqual(values, sorted(values), element)
            if element == "金":
                extra = [float(cfg["active_skill"].get("extra_damage") or 0) for cfg in rows]
                self.assertEqual(extra, sorted(extra), element)

    def test_heal_text_matches_implementation(self):
        """木系实际只回复已损失气血，不再保留未实现的溢出护体描述。"""
        for cfg in self.techniques.values():
            skill = cfg.get("active_skill") or {}
            if skill.get("type") == "heal":
                self.assertNotIn("溢出", str(skill.get("description") or ""), cfg.get("name"))

    def test_water_high_ranks_are_full_freeze(self):
        for cfg in self.techniques.values():
            skill = cfg.get("active_skill") or {}
            if skill.get("type") == "freeze" and cfg["rank"] in self.ranks[3:]:
                self.assertEqual(float(skill.get("value") or 0), 0.0, cfg.get("name"))


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

    def test_combat_log_shows_element_counter_and_final_mp(self):
        p1 = self.CombatStats(
            user_id="u1", name="甲", hp=10000, max_hp=10000, mp=1000, max_mp=1000,
            atk=1000, defense=100, physical_atk=1000, magic_atk=0,
            physical_defense=100, magic_defense=0, effects={}, element="金",
        )
        p2 = self.CombatStats(
            user_id="u2", name="乙", hp=10000, max_hp=10000, mp=1000, max_mp=1000,
            atk=1000, defense=100, physical_atk=1000, magic_atk=0,
            physical_defense=100, magic_defense=0, effects={}, element="木",
        )
        result = self.CombatManager.player_vs_player(p1, p2, combat_type=1)
        log = "\n".join(result["combat_log"])
        self.assertIn("五行克制", log)
        self.assertIn("战斗结束", log)
        self.assertIn("灵气", log)

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

    def test_burn_value_is_converted_to_percentage_points(self):
        """0.02 表示 2%，PvE 灼烧每回合应按 2% 最大生命结算。"""
        burn = {"name": "赤炎焚脉诀", "type": "burn", "value": 0.02}
        defender = self._stats("u2", "乙")
        logs = []
        self.CombatManager._apply_active_skill(self._stats("u1", "甲"), defender, burn, logs)
        self.assertAlmostEqual(defender.statuses["burn"]["damage_pct"], 2.0, places=3)
        self.assertEqual(defender.statuses["burn"]["turns"], 3)
        self.assertTrue(any("2.0%" in line for line in logs))

    def test_freeze_uses_action_rate(self):
        freeze = {"name": "玄冰凝霜", "type": "freeze", "value": 0.2}
        defender = self._stats("u2", "乙")
        self.CombatManager._apply_active_skill(self._stats("u1", "甲"), defender, freeze, [])
        self.assertAlmostEqual(defender.statuses["freeze"]["action_rate"], 0.2, places=3)

        # action_rate=1 必定挣脱，action_rate=0 必定无法行动
        free = self._stats("u3", "丙")
        free.statuses["freeze"] = {"turns": 1, "action_rate": 1.0}
        self.assertFalse(self.CombatManager._tick_statuses(free, []))
        locked = self._stats("u4", "丁")
        locked.statuses["freeze"] = {"turns": 1, "action_rate": 0.0}
        self.assertTrue(self.CombatManager._tick_statuses(locked, []))

    def test_skill_summary_contains_cost_and_effect(self):
        skill = {"name": "裂金破甲斩", "trigger_round": 2, "mp_cost": 500,
                 "type": "pierce", "value": 0.15, "extra_damage": 0.4,
                 "description": "无视对方15%物防，并追加0.4倍攻击伤害。"}
        text = "\n".join(_imp("core.technique_skill").skill_summary_lines(skill))
        self.assertIn("裂金破甲斩", text)
        self.assertIn("500", text)
        self.assertIn("15%", text)
        self.assertIn("第 2 回合", text)


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
