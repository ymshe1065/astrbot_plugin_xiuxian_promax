"""特殊手套/戒指：数量、定价、无主属性、品质缩放与掉落区间回归。"""
import json
import random
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp, ROOT


class TestSpecialEquipment(unittest.TestCase):
    def setUp(self):
        _imp("data")
        self.cm = _imp("config_manager").ConfigManager(ROOT)
        self.balance = _imp("core.equipment_balance")
        self.Player = _imp("models").Player
        self.special = {
            name: cfg for name, cfg in self.cm.items_data.items()
            if cfg.get("effect_quality_scaling")
        }

    def test_catalog_has_120_items_and_keeps_lingwen(self):
        self.assertEqual(len(self.special), 120)
        lingwen = [n for n in self.cm.items_data if "灵纹" in n and ("法手" in n or "法戒" in n)]
        self.assertEqual(len(lingwen), 24)
        from collections import Counter
        ranks = Counter(cfg["rank"] for cfg in self.special.values())
        self.assertEqual(set(ranks.values()), {10})
        slots = Counter(cfg["equip_slot"] for cfg in self.special.values())
        self.assertEqual(slots, {"gloves": 60, "ring": 60})

    def test_special_items_have_no_panel_rates(self):
        for name, cfg in self.special.items():
            self.assertEqual(cfg.get("equipment_rates"), {}, name)
            self.assertFalse(any(cfg.get(key) for key in self.balance.RATE_CAPS), name)
            self.assertTrue(cfg.get("effects"), name)
            self.assertTrue(cfg.get("description"), name)

    def test_price_is_ninety_percent_of_reference(self):
        reference = {}
        for name, cfg in self.cm.items_data.items():
            if "灵纹" not in name:
                continue
            if "法手" in name:
                reference[("gloves", cfg["rank"])] = cfg["price"]
            elif "法戒" in name:
                reference[("ring", cfg["rank"])] = cfg["price"]
        self.assertEqual(len(reference), 24)
        for cfg in self.special.values():
            base = reference[(cfg["equip_slot"], cfg["rank"])]
            expected = max(100, int(round(base * 0.9 / 10.0) * 10))
            self.assertEqual(cfg["price"], expected, cfg["name"])

    def test_quality_scales_values_but_not_turns_or_flags(self):
        base = {"effects": {"lifesteal": 10, "stun_turns": 2, "burn_detonate": 1},
                "effect_quality_scaling": True}
        normal = self.balance.effective_effects(base, "一般")
        master = self.balance.effective_effects(base, "大师")
        self.assertEqual(normal["lifesteal"], 10)
        self.assertEqual(master["lifesteal"], 12)  # 10 * 1.15 四舍五入
        self.assertEqual(master["stun_turns"], 2)
        self.assertEqual(master["burn_detonate"], 1)

    def test_equipped_quality_reaches_combat_effects(self):
        ItemInstanceManager = _imp("core.item_instance_manager").ItemInstanceManager
        name = "凡品·噬血手套"
        player = self.Player(user_id="u1", level_index=0)
        im = ItemInstanceManager(None, self.cm)
        inst = im.create_instances(player, name, quality="大师", location="equipped", slot="gloves")[0]
        player.set_equipped_instances({"gloves": inst["id"]})
        effects = self._combat().collect_combat_effects(player, self.cm)
        self.assertEqual(effects["lifesteal"], 2)  # 2% -> 2.3 -> int 2

    def test_extra_attack_can_proc_on_hit_effects(self):
        combat = _imp("managers.combat_manager")
        Stats = combat.CombatStats
        for can_proc, expected in ((1, True), (0, False)):
            effects = {
                "extra_attack_chance": 100,
                "extra_attack_ratio": 50,
                "extra_attack_can_proc": can_proc,
                "burn_chance": 100,
                "burn_turns": 3,
                "burn_damage_pct": 5,
            }
            attacker = Stats(
                user_id="a", name="甲", hp=1000, max_hp=1000, mp=0, max_mp=0,
                atk=200, defense=0, physical_atk=200, magic_atk=0,
                physical_defense=0, magic_defense=0, effects=effects,
            )
            defender = Stats(
                user_id="b", name="乙", hp=1000, max_hp=1000, mp=0, max_mp=0,
                atk=100, defense=0, physical_atk=100, magic_atk=0,
                physical_defense=0, magic_defense=0, effects={},
            )
            random.seed(7)
            damage, logs, _ = combat.CombatManager._execute_attack(attacker, defender)
            self.assertGreater(damage, 0)
            self.assertEqual("burn" in defender.statuses, expected, logs)

    def _combat(self):
        return _imp("managers.combat_manager").CombatManager


class TestEquipmentDrop(unittest.TestCase):
    def setUp(self):
        _imp("data")
        self.cm = _imp("config_manager").ConfigManager(ROOT)
        self.drop = _imp("core.equipment_drop")

    def test_pool_covers_all_slots(self):
        pool = self.drop.build_equipment_pool(self.cm)
        self.assertTrue({"weapon", "armor", "head", "boots", "amulet", "gloves", "ring"} <= set(pool))
        self.assertTrue(any(cfg["name"] in self.cm.special_equipment_data for cfg in pool["gloves"]))

    def test_drop_rank_stays_near_player_level(self):
        rng = random.Random(2026)
        catalog = {**self.cm.weapons_data, **self.cm.items_data}
        for _ in range(80):
            drop = self.drop.roll_equipment_drop(self.cm, 13, rng=rng)
            self.assertIsNotNone(drop)
            self.assertIn(drop["quality"], {"一般", "良好", "极佳", "大师"})
            self.assertIn(drop["name"], catalog)
            self.assertLessEqual(
                abs(catalog[drop["name"]].get("required_level_index", 0) - 13), 10
            )


if __name__ == "__main__":
    unittest.main()
