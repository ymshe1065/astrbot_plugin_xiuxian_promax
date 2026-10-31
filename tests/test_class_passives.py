"""职业被动：只强化长板，并确保鬼修/剑修不再补防御。"""
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import ROOT, _imp


class TestClassPassives(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.balance = _imp("core.equipment_balance")
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.Player = _imp("models").Player
        cls.Combat = _imp("managers.combat_manager").CombatManager

    def _player(self, cultivation):
        p = self.Player(user_id="u1", level_index=18)
        p.cultivation_type = cultivation
        return p

    def _base(self, cultivation):
        return self.balance.realm_attributes(self._player(cultivation), self.cm)

    def test_no_class_gets_contradictory_defense_passive(self):
        self.assertNotIn("defense", self.balance.CLASS_PASSIVES["鬼修"])
        self.assertNotIn("defense", self.balance.CLASS_PASSIVES["剑修"])
        self.assertNotIn("physical_damage", self.balance.CLASS_PASSIVES["体修"])
        self.assertNotIn("magic_damage", self.balance.CLASS_PASSIVES["体修"])
        self.assertNotIn("physical_damage", self.balance.CLASS_PASSIVES["佛修"])
        self.assertNotIn("magic_damage", self.balance.CLASS_PASSIVES["佛修"])

    def test_ghost_defense_is_base_only(self):
        base = self._base("鬼修")
        scale = base["defense_scale"]
        expected_total = int(scale * .35 * .88)
        # 原模板只拆分到物防/法防，不做任何额外防御被动。
        self.assertEqual(
            base["physical_defense"] + base["magic_defense"], expected_total
        )

    def test_body_hp_and_buddha_defense_follow_passive(self):
        body = self._base("体修")
        buddha = self._base("佛修")
        scale = body["defense_scale"]
        self.assertEqual(body["max_hp"], int(scale * 8 * 1.06))
        self.assertEqual(buddha["max_hp"], int(scale * 8 * 1.03))
        self.assertGreater(buddha["physical_defense"], body["physical_defense"])

    def test_effects_are_applied_to_combat(self):
        sword = self.Combat.collect_combat_effects(self._player("剑修"), self.cm)
        ghost = self.Combat.collect_combat_effects(self._player("鬼修"), self.cm)
        magic = self.Combat.collect_combat_effects(self._player("魔修"), self.cm)
        self.assertGreaterEqual(sword["crit_rate"], 2)
        self.assertGreaterEqual(ghost["armor_penetration"], 3)
        self.assertGreaterEqual(magic["lifesteal"], 4)


if __name__ == "__main__":
    unittest.main()
