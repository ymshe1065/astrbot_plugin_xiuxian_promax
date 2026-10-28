# tests/test_combat_dual_track.py
import random
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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


class TestCombatDualTrack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.CombatManager = _imp("managers.combat_manager").CombatManager
        cls.CombatStats = _imp("managers.combat_manager").CombatStats

    def test_components_sum_to_old_total(self):
        cm = self.CombatManager
        for cultivation in ("剑修", "鬼修", "灵修", "魔修", "佛修", "体修", "御兽"):
            old = cm.calculate_atk(176292, 0, 0.0, 410, 260, cultivation)
            components = cm.calculate_attack_components(
                176292, 0, 0.0, 410, 260, cultivation
            )
            self.assertEqual(components["atk"], old, cultivation)

    def test_effective_experience_overflow_segments(self):
        cm = self.CombatManager
        cap = 350_000
        self.assertEqual(cm.calculate_effective_experience(cap, cap), cap)
        self.assertEqual(
            cm.calculate_effective_experience(471_391, cap),
            cap + int(121_391 * 0.30),
        )
        self.assertEqual(
            cm.calculate_effective_experience(1_000_000, cap),
            cap + int(350_000 * 0.30) + int(300_000 * 0.15),
        )
        self.assertEqual(
            cm.calculate_effective_experience(2_000_000, cap),
            cap + int(350_000 * 0.30) + int(350_000 * 0.15)
            + int(950_000 * 0.05),
        )

    def test_experience_overflow_bonus_is_capped(self):
        cm = self.CombatManager

        class Player:
            experience = 2_000_000

            def get_required_exp(self, config_manager):
                return 350_000

        bonus = cm.get_experience_overflow_bonus(Player(), None)
        expected_effective = (
            350_000 + int(350_000 * 0.30) + int(350_000 * 0.15)
            + int(950_000 * 0.05)
        )
        self.assertAlmostEqual(bonus, (expected_effective - 350_000) / 350_000)

        Player.experience = 10**18
        self.assertEqual(
            cm.get_experience_overflow_bonus(Player(), None),
            cm.MAX_OVERFLOW_BONUS,
        )

    def test_physical_defense_only_reduces_physical(self):
        cm = self.CombatManager
        attacker = self.CombatStats(
            user_id="a", name="甲", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=1000, defense=0, effects={"crit_rate": -1, "combo_rate": -1},
            physical_atk=1000, magic_atk=0,
        )
        low_phys = self.CombatStats(
            user_id="b", name="乙", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=1, defense=0, effects={}, physical_defense=100, magic_defense=0,
        )
        high_magic = self.CombatStats(
            user_id="c", name="丙", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=1, defense=0, effects={}, physical_defense=0, magic_defense=100,
        )
        random.seed(7)
        low_damage, _, _ = cm._execute_attack(attacker, low_phys)
        random.seed(7)
        high_damage, _, _ = cm._execute_attack(attacker, high_magic)
        self.assertLess(low_damage, high_damage)

    def test_magic_defense_only_reduces_magic(self):
        cm = self.CombatManager
        attacker = self.CombatStats(
            user_id="a", name="甲", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=1000, defense=0, effects={"crit_rate": -1, "combo_rate": -1},
            physical_atk=0, magic_atk=1000,
        )
        high_phys = self.CombatStats(
            user_id="b", name="乙", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=1, defense=0, effects={}, physical_defense=100, magic_defense=0,
        )
        high_magic = self.CombatStats(
            user_id="c", name="丙", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=1, defense=0, effects={}, physical_defense=0, magic_defense=100,
        )
        random.seed(11)
        high_phys_damage, _, _ = cm._execute_attack(attacker, high_phys)
        random.seed(11)
        high_magic_damage, _, _ = cm._execute_attack(attacker, high_magic)
        self.assertLess(high_magic_damage, high_phys_damage)

    def test_crit_and_combo_apply_once_to_both_parts(self):
        cm = self.CombatManager
        attacker = self.CombatStats(
            user_id="a", name="甲", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=2000, defense=0,
            effects={"crit_rate": 100, "combo_rate": 100, "crit_damage": 2.0},
            physical_atk=1000, magic_atk=1000,
        )
        defender = self.CombatStats(
            user_id="b", name="乙", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=1, defense=0, effects={},
            physical_defense=0, magic_defense=0,
        )
        with patch(f"{PKG}.managers.combat_manager.random.randint", return_value=0), \
             patch(f"{PKG}.managers.combat_manager.random.uniform", return_value=1.0):
            damage, logs, _ = cm._execute_attack(attacker, defender)
        self.assertEqual(damage, 6000)
        text = "\n".join(logs)
        self.assertEqual(text.count("会心一击"), 1)
        self.assertEqual(text.count("功法连击"), 1)
        self.assertIn("物理", text)
        self.assertIn("法术", text)

    def test_shared_variance_one_roll_for_both_parts(self):
        cm = self.CombatManager
        attacker = self.CombatStats(
            user_id="a", name="甲", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=2000, defense=0,
            effects={"crit_rate": -1, "combo_rate": -1},
            physical_atk=1000, magic_atk=1000,
        )
        defender = self.CombatStats(
            user_id="b", name="乙", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=1, defense=0, effects={},
            physical_defense=0, magic_defense=0,
        )
        with patch(
            f"{PKG}.managers.combat_manager.random.uniform", return_value=1.3
        ) as uniform:
            max_damage, _, _ = cm._execute_attack(attacker, defender)
        self.assertEqual(max_damage, 2600)
        self.assertEqual(uniform.call_count, 1)

        with patch(
            f"{PKG}.managers.combat_manager.random.uniform", return_value=0.7
        ) as uniform:
            min_damage, _, _ = cm._execute_attack(attacker, defender)
        self.assertEqual(min_damage, 1400)
        self.assertEqual(uniform.call_count, 1)

    def test_pet_lineup_syncs_components(self):
        cm = self.CombatManager
        panel = {
            "physical_atk": 1000, "magic_atk": 2000, "atk": 3000,
            "physical_defense": 100, "magic_defense": 200, "defense": 300,
            "crit_rate": 0, "lifesteal": 0,
        }
        cm.apply_pet_lineup(
            panel, None, [], {"defense_bonus": 60, "crit_rate": 5}, 3400
        )
        self.assertEqual(panel["physical_atk"], 1400)
        self.assertEqual(panel["magic_atk"], 2000)
        self.assertEqual(panel["atk"], 3400)
        self.assertEqual(panel["physical_defense"], 130)
        self.assertEqual(panel["magic_defense"], 230)
        self.assertEqual(panel["defense"], 360)
        self.assertEqual(panel["crit_rate"], 5)

    def test_legacy_stats_fallback_split(self):
        cm = self.CombatManager
        old = self.CombatStats(
            user_id="a", name="甲", hp=100, max_hp=100, mp=1, max_mp=1,
            atk=1001, defense=101,
        )
        p_atk, m_atk = cm.resolve_attack_components(old)
        p_def, m_def = cm.resolve_defense_components(old)
        self.assertEqual(p_atk + m_atk, 1001)
        self.assertEqual(p_def + m_def, 101)
        self.assertGreater(p_atk, 0)
        self.assertGreater(m_atk, 0)

    def test_impart_crit_applies_to_panel_and_battle(self):
        cm = self.CombatManager
        config = _imp("config_manager").ConfigManager(ROOT)
        Player = _imp("models").Player
        player = Player(user_id="impart", level_index=12)
        impart = types.SimpleNamespace(
            impart_hp_per=0.0,
            impart_mp_per=0.0,
            impart_atk_per=0.0,
            impart_know_per=0.25,
            impart_burst_per=0.5,
        )
        effects = {
            "crit_rate": 0,
            "crit_damage": 1.5,
            "lifesteal": 0,
            "counter_damage": 0,
        }
        panel = cm.build_combat_panel(player, {}, impart, config, effects)
        self.assertEqual(panel["crit_rate"], 25)
        self.assertAlmostEqual(panel["crit_damage"], 2.0)
        # 面板不得回写调用方的特效字典
        self.assertEqual(effects["crit_rate"], 0)
        self.assertAlmostEqual(effects["crit_damage"], 1.5)

        stats = cm.build_player_combat_stats(
            player,
            config,
            crit_rate=25,
            crit_damage_bonus=0.5,
            pill_multipliers={},
        )
        self.assertEqual(stats.effects["crit_rate"], 25)
        self.assertAlmostEqual(stats.effects["crit_damage"], 2.0)
        # 会心已并入 effects，CombatStats.crit_rate 保持 0 防止重复判定
        self.assertEqual(stats.crit_rate, 0)

    def test_overflow_conversion_applies_without_pets(self):
        cm = self.CombatManager
        config = _imp("config_manager").ConfigManager(ROOT)
        Player = _imp("models").Player
        player = Player(user_id="overflow", level_index=12)
        base = cm.collect_combat_effects(player, config)
        plain = cm.build_combat_panel(player, {}, None, config, dict(base))

        boosted = dict(base)
        boosted["lifesteal"] = boosted.get("lifesteal", 0) + 150
        panel = cm.build_combat_panel(player, {}, None, config, dict(boosted))
        self.assertEqual(panel["lifesteal"], 100)
        self.assertGreater(panel["atk"], plain["atk"])

        stats = cm.build_player_combat_stats(
            player, config, extra_effects={"lifesteal": 150}, pill_multipliers={}
        )
        self.assertEqual(stats.effects["lifesteal"], 100)
        self.assertEqual(panel["atk"], stats.atk)
        self.assertEqual(panel["physical_atk"], stats.physical_atk)
        self.assertEqual(panel["magic_atk"], stats.magic_atk)

        crit = dict(base)
        crit["crit_rate"] = crit.get("crit_rate", 0) + 150
        crit_panel = cm.build_combat_panel(player, {}, None, config, dict(crit))
        overflow = crit["crit_rate"] - 100
        self.assertEqual(crit_panel["crit_rate"], 100)
        self.assertAlmostEqual(
            crit_panel["crit_damage"],
            base.get("crit_damage", 1.5) + overflow * 0.5 / 100.0,
            places=6,
        )

    def test_player_effects_allow_documented_overflow_caps(self):
        cm = self.CombatManager
        config = _imp("config_manager").ConfigManager(ROOT)
        Player = _imp("models").Player
        player = Player(
            user_id="caps",
            level_index=12,
            permanent_bonus='{"affixes":[{"key":"crit_rate","value":60},'
                            '{"key":"lifesteal","value":25}]}',
        )
        effects = cm.collect_combat_effects(player, config)
        # v6.36.9 起暴击/吸血总量上限为 100，不应再被旧的 50/15 上限截断
        self.assertEqual(effects["crit_rate"], 60)
        self.assertEqual(effects["lifesteal"], 25)

        panel = cm.build_combat_panel(player, {}, None, config, dict(effects))
        self.assertEqual(panel["crit_rate"], 60)
        self.assertEqual(panel["lifesteal"], 25)

    def test_balanced_pvp_sets_dual_components(self):
        cm = self.CombatManager
        p1 = self.CombatStats(
            user_id="a", name="甲", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=20000, defense=1000,
        )
        p2 = self.CombatStats(
            user_id="b", name="乙", hp=10000, max_hp=10000, mp=1, max_mp=1,
            atk=20000, defense=1000,
        )
        cm.player_vs_player_balanced(p1, p2)
        self.assertEqual(p1.physical_atk + p1.magic_atk, p1.atk)
        self.assertEqual(p1.physical_defense + p1.magic_defense, p1.defense)
        self.assertEqual(p1.physical_defense, 50)
        self.assertEqual(p1.magic_defense, 50)


if __name__ == "__main__":
    unittest.main()
