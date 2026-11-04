"""五行武侠装备：效果收集、面板/交易展示与战斗结算回归。"""
import json
import unittest
from unittest import mock

from test_equipment_instances import ROOT, _imp

GEAR_FILE = "elemental_equipment.json"


def _load_gear():
    return json.loads((ROOT / "config" / GEAR_FILE).read_text(encoding="utf-8"))


def _make_stats(name, *, hp=100000, atk=1000, effects=None, **kwargs):
    stats_cls = _imp("managers.combat_manager").CombatStats
    data = dict(
        user_id=name, name=name, hp=hp, max_hp=hp, mp=0, max_mp=0,
        atk=atk, defense=0, physical_atk=atk, magic_atk=0,
        physical_defense=0, magic_defense=0, effects=dict(effects or {}),
    )
    data.update(kwargs)
    return stats_cls(**data)


class TestElementalEquipmentCollection(unittest.TestCase):
    """80 件装备的每一条声明效果都必须进入战斗与展示链路。"""

    @classmethod
    def setUpClass(cls):
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.balance = _imp("core.equipment_balance")
        cls.equipment_manager = _imp("core.equipment_manager").EquipmentManager
        cls.combat = _imp("managers.combat_manager").CombatManager
        cls.instance_manager = _imp("core.item_instance_manager").ItemInstanceManager
        cls.shop = _imp("core.shop_manager").ShopManager
        cls.player_cls = _imp("models").Player
        cls.gear = _load_gear()

    def _equip(self, name, cfg, quality="一般"):
        # 配置里的 body 在穿戴表中映射为 armor
        slot = "armor" if cfg["equip_slot"] == "body" else cfg["equip_slot"]
        player = self.player_cls(user_id="tester", level_index=35)
        im = self.instance_manager(None, self.cm)
        inst = im.create_instances(player, name, quality=quality, location="equipped", slot=slot)[0]
        player.set_equipped_instances({slot: inst["id"]})
        return player, inst

    def test_every_declared_effect_reaches_combat_and_display(self):
        em = self.equipment_manager(None, self.cm)
        shop = self.shop({}, self.cm)
        failures = []
        checked = 0
        for name, cfg in self.gear.items():
            player, inst = self._equip(name, cfg)
            effects = self.combat.collect_combat_effects(player, self.cm)
            rates = em.get_equipment_rates(player)
            bonus_text = em.describe_item_bonus(player, name, inst)
            trade_text = "\n".join(shop.get_trade_details(
                {"name": name, "data": cfg, "price": cfg["price"], "rank": cfg["rank"]},
                instance=inst,
            ))
            for key, value in cfg["effects"].items():
                checked += 1
                if key in ("physical_damage_multiplier", "magic_damage_multiplier"):
                    attr = {"physical_damage_multiplier": "physical_damage",
                            "magic_damage_multiplier": "magic_damage"}[key]
                    if rates.get(attr, 0) + 1e-9 < min(float(value), 0.10):
                        failures.append((name, key, "rate", rates.get(attr), value))
                else:
                    cap = self.balance.GEAR_EFFECT_CAPS.get(key)
                    want = min(float(value), cap) if cap is not None else float(value)
                    got = effects.get(key)
                    if got is None or got + 1e-6 < want:
                        failures.append((name, key, "combat", got, want))
                label = em.EFFECT_LABELS.get(key, key)
                if label not in bonus_text:
                    failures.append((name, key, "bonus-display", bonus_text, label))
                if label not in trade_text:
                    failures.append((name, key, "trade-display", label))
        self.assertGreaterEqual(checked, 160)
        self.assertEqual(failures, [])

    def test_magic_multiplier_enters_magic_damage_pool(self):
        em = self.equipment_manager(None, self.cm)
        names = [n for n, c in self.gear.items()
                 if "magic_damage_multiplier" in c["effects"]]
        self.assertTrue(names)
        for name in names:
            cfg = self.gear[name]
            player, _ = self._equip(name, cfg)
            rates = em.get_equipment_rates(player)
            expected = min(float(cfg["effects"]["magic_damage_multiplier"]), 0.10)
            self.assertGreaterEqual(rates.get("magic_damage", 0) + 1e-9, expected, name)
            self.assertLessEqual(rates["magic_damage"], self.balance.RATE_CAPS["magic_damage"])


class TestElementalEquipmentCombat(unittest.TestCase):
    """逐类验证特殊效果在战斗回合中真实结算。"""

    @classmethod
    def setUpClass(cls):
        cls.combat = _imp("managers.combat_manager").CombatManager

    def _attack(self, effects, defender_effects=None, **kwargs):
        attacker = _make_stats("甲", effects=effects, **kwargs)
        defender = _make_stats("乙", effects=defender_effects or {})
        damage, logs, heal = self.combat._take_turn(attacker, defender, round_num=1)
        return attacker, defender, damage, logs, heal

    def test_extra_attack_fires(self):
        _, _, damage, logs, _ = self._attack(
            {"extra_attack_chance": 100, "extra_attack_ratio": 30}
        )
        self.assertGreater(damage, 0)
        self.assertIn("触发快击", "\n".join(logs))

    def test_burn_applies_and_ticks(self):
        _, defender, _, logs, _ = self._attack(
            {"burn_chance": 100, "burn_damage_pct": 10, "burn_turns": 2,
             "burn_heal_reduction": 50}
        )
        self.assertIn("点燃", "\n".join(logs))
        burn = defender.statuses.get("burn")
        self.assertIsNotNone(burn)
        self.assertEqual(burn["damage_pct"], 10)
        self.assertEqual(burn["heal_reduction"], 50)
        hp_before = defender.hp
        _, tick_logs, _ = self.combat._take_turn(defender, _make_stats("甲"), round_num=2)
        self.assertLess(defender.hp, hp_before)
        self.assertIn("受到灼烧", "\n".join(tick_logs))

    def test_frost_reduces_attack_and_dodge(self):
        _, defender, _, logs, _ = self._attack(
            {"frost_chance": 100, "frost_atk_reduction": 20,
             "frost_dodge_reduction": 3, "frost_turns": 2}
        )
        self.assertIn("寒霜缠身", "\n".join(logs))
        frost = defender.statuses.get("frost")
        self.assertIsNotNone(frost)
        self.assertEqual(frost["atk_reduction"], 20)
        self.assertEqual(frost["dodge_reduction"], 3)

    def test_freeze_skips_next_turn(self):
        _, defender, _, logs, _ = self._attack({"freeze_chance": 100})
        self.assertIn("冻结", "\n".join(logs))
        self.assertIn("freeze", defender.statuses)
        damage, skip_logs, _ = self.combat._take_turn(defender, _make_stats("甲"), round_num=2)
        self.assertEqual(damage, 0)
        self.assertIn("无法行动", "\n".join(skip_logs))

    def test_stun_and_resist_pierce(self):
        _, defender, _, logs, _ = self._attack({"stun_chance": 100})
        self.assertIn("麻痹", "\n".join(logs))
        self.assertIn("stun", defender.statuses)
        # 高状态抗性可以挡下控制；穿透抗性后重新生效
        with mock.patch("random.uniform", return_value=85):
            _, resisted, _, _, _ = self._attack(
                {"stun_chance": 100}, {"status_resist": 90}
            )
        self.assertNotIn("stun", resisted.statuses)
        with mock.patch("random.uniform", return_value=85):
            _, pierced, _, _, _ = self._attack(
                {"stun_chance": 100, "stun_resist_pierce": 100}, {"status_resist": 90}
            )
        self.assertIn("stun", pierced.statuses)

    def test_sunder_lowers_defense(self):
        _, defender, _, logs, _ = self._attack(
            {"sunder_chance": 100, "sunder_pct": 30, "sunder_turns": 2}
        )
        self.assertIn("击破", "\n".join(logs))
        sunder = defender.statuses.get("sunder")
        self.assertIsNotNone(sunder)
        self.assertEqual(sunder["pct"], 30)

    def test_armor_penetration_increases_damage(self):
        defender = _make_stats("乙", physical_defense=5000)
        normal = _make_stats("甲", effects={})
        piercing = _make_stats("甲", effects={"armor_penetration": 100})
        with mock.patch("random.uniform", return_value=1.0), \
                mock.patch("random.randint", return_value=99):
            base = self.combat._execute_attack(normal, defender)[0]
            boosted = self.combat._execute_attack(piercing, defender)[0]
        self.assertGreater(base, 0)
        self.assertGreater(boosted, base)

    def test_dodge_avoids_attack(self):
        attacker = _make_stats("甲")
        defender = _make_stats("乙", effects={"dodge": 5})
        with mock.patch("random.randint", return_value=1):
            damage, logs, _ = self.combat._execute_attack(attacker, defender)
        self.assertEqual(damage, 0)
        self.assertIn("避开", "\n".join(logs))

    def test_lifesteal_heals_attacker(self):
        attacker = _make_stats("甲", hp=50000)
        attacker.max_hp = 100000
        attacker.hp = 50000
        defender = _make_stats("乙")
        attacker.effects = {"lifesteal": 30}
        with mock.patch("random.uniform", return_value=1.0), \
                mock.patch("random.randint", return_value=99):
            _, _, heal = self.combat._execute_attack(attacker, defender)
        self.assertGreater(heal, 0)

    def test_combo_and_second_hit(self):
        _, _, damage, logs, _ = self._attack(
            {"combo_rate": 100, "combo_extra_hit_ratio": 50}
        )
        text = "\n".join(logs)
        self.assertIn("功法连击", text)
        self.assertIn("连击二段", text)
        self.assertGreater(damage, 0)

    def test_burn_heal_reduction_affects_lifesteal(self):
        attacker = _make_stats("甲")
        attacker.effects = {"lifesteal": 30, "burn_chance": 100,
                            "burn_damage_pct": 10, "burn_heal_reduction": 100}
        defender = _make_stats("乙")
        attacker.statuses["burn"] = {"turns": 2, "damage_pct": 1, "heal_reduction": 100}
        with mock.patch("random.uniform", return_value=1.0), \
                mock.patch("random.randint", return_value=99):
            _, _, heal = self.combat._execute_attack(attacker, defender)
        self.assertEqual(heal, 0)


class TestCounterDamageEverywhere(unittest.TestCase):
    """反伤必须在 PvP 与 Boss 战中都结算。"""

    @classmethod
    def setUpClass(cls):
        cls.combat = _imp("managers.combat_manager").CombatManager

    def test_player_vs_player_reflects_both_sides(self):
        p1 = _make_stats("甲", effects={"counter_damage": 20})
        p2 = _make_stats("乙", effects={"counter_damage": 20})
        result = self.combat.player_vs_player(p1, p2)
        text = "\n".join(result["combat_log"])
        self.assertIn("甲 触发反伤", text)
        self.assertIn("乙 触发反伤", text)

    def test_player_vs_boss_reflects_boss_damage(self):
        player = _make_stats("甲", hp=100000, effects={"counter_damage": 20})
        boss = _make_stats("乙", hp=100000, effects={})
        result = self.combat.player_vs_boss(player, boss)
        self.assertIn("甲 触发反伤", "\n".join(result["combat_log"]))

    def test_player_vs_boss_reflects_player_damage(self):
        player = _make_stats("甲", hp=100000, effects={})
        boss = _make_stats("乙", hp=100000, effects={"counter_damage": 20})
        result = self.combat.player_vs_boss(player, boss)
        self.assertIn("乙 触发反伤", "\n".join(result["combat_log"]))

    def test_boss_reflect_can_finish_boss(self):
        # 玩家攻击很低，靠反伤击杀 Boss；胜负与回合上限不受影响
        player = _make_stats("甲", hp=100000, atk=1, effects={"counter_damage": 100})
        boss = _make_stats("乙", hp=3000, atk=1000, effects={})
        result = self.combat.player_vs_boss(player, boss)
        self.assertEqual(result["boss_final_hp"], 0)
        self.assertEqual(result["winner"], "甲")
        self.assertLessEqual(result["rounds"], 100)


if __name__ == "__main__":
    unittest.main()
