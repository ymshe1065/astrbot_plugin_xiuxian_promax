"""境界主导、装备加法、全入口一致和旧存档安全回归。"""
import copy
import json
import random
import unittest
from unittest.mock import patch
import test_equipment_instances as fixtures
from test_equipment_instances import _imp, ROOT


class TestEquipmentBalance(unittest.TestCase):
    def setUp(self):
        _imp('data')
        self.cm = _imp('config_manager').ConfigManager(ROOT)
        self.Player = _imp('models').Player
        self.combat = _imp('managers.combat_manager').CombatManager
        self.em = _imp('core.equipment_manager').EquipmentManager(None, self.cm)
        self.balance = _imp('core.equipment_balance')

    def suit(self, level, quality='大师', affixes=None):
        p = self.Player(user_id='equipped', level_index=level)
        mapping = {}
        configs = {**self.cm.weapons_data, **self.cm.items_data}
        rules = _imp('core.item_rules').ItemRules
        for slot in rules.SLOT_LABELS:
            wanted = 'body' if slot == 'armor' else 'ring1' if slot == 'ring2' else slot
            candidates = [(name, c) for name, c in configs.items()
                          if rules.get_equip_slot(c) == wanted and rules.get_required_level(c) <= level]
            self.assertTrue(candidates)
            name, cfg = max(candidates, key=lambda pair: (rules.get_required_level(pair[1]), sum(self.balance.equipment_rates(pair[1]).values())))
            inst = self.em.instance_manager.create_instances(p, name, quality=quality, affixes=affixes, location='equipped', slot=slot)[0]
            mapping[slot] = inst['id']
        p.set_equipped_instances(mapping)
        return p

    def test_all_realms_have_bounded_full_suit(self):
        for level in range(len(self.cm.level_data)):
            with self.subTest(level=level):
                p = self.suit(level)
                rates = self.em.get_equipment_rates(p)
                for key, rate in rates.items():
                    self.assertLessEqual(rate, self.balance.RATE_CAPS[key])
                bare = self.Player(user_id='bare', level_index=level)
                a, b = self.combat.build_player_combat_stats(p, self.cm), self.combat.build_player_combat_stats(bare, self.cm)
                self.assertLessEqual(a.atk, b.atk * 1.40)
                self.assertLessEqual(a.defense, b.defense * 1.35)
                self.assertEqual(a.hp, b.hp)

    def test_experience_hoarding_cannot_increase_combat(self):
        p = self.suit(10)
        before = self.combat.build_player_combat_stats(p, self.cm)
        p.experience = 10**18
        after = self.combat.build_player_combat_stats(p, self.cm)
        for key in ('atk', 'defense', 'hp', 'mp', 'defense_scale'):
            self.assertEqual(getattr(before, key), getattr(after, key))
        p.level_index = 13
        higher = self.combat.build_player_combat_stats(p, self.cm)
        self.assertGreater(higher.atk, after.atk)

    def test_quality_and_obsolescence_do_not_multiply_total_stats(self):
        config = {'type': 'weapon', 'required_level_index': 10, 'equipment_rates': {'physical_damage': .18}}
        self.assertAlmostEqual(self.balance.effective_rates(config, 10, '大师')['physical_damage'], .207)
        self.assertAlmostEqual(self.balance.effective_rates(config, 13, '大师')['physical_damage'], .207 * .8)
        self.assertAlmostEqual(self.balance.effective_rates(config, 35)['physical_damage'], .18 * .2)

    def test_legacy_fixed_stats_cannot_bypass_budget(self):
        config = {'type': 'weapon', 'physical_damage': 10**20, 'magic_damage': 10**20}
        self.assertEqual(self.balance.equipment_rates(config)['physical_damage'], .18)
        p = self.suit(18, affixes=[{'key': 'physical_damage_multiplier', 'value': 999}, {'key': 'crit_rate', 'value': 999}, {'key': 'counter_damage', 'value': 999}])
        self.assertEqual(self.em.get_equipment_rates(p)['physical_damage'], .40)
        effects = self.combat.collect_combat_effects(p, self.cm)
        self.assertEqual(effects['physical_damage_multiplier'], 1.0)
        self.assertEqual(effects['crit_rate'], 10)
        self.assertEqual(effects['counter_damage'], 10)

    def test_catalog_has_no_fixed_gear_stats_and_prices_are_monotonic(self):
        rules = _imp('core.item_rules').ItemRules
        prices = {}
        for config in {**self.cm.weapons_data, **self.cm.items_data}.values():
            if rules.normalize_equipment_type(config) not in rules.GEAR_TYPES:
                continue
            self.assertIn('equipment_rates', config)
            self.assertFalse(any(config.get(key) for key in (*self.balance.RATE_CAPS, 'atk', 'equip_effects')))
            self.assertEqual(config['price'], self.balance.equipment_price(config))
            if not config.get('effects'):
                prices.setdefault(rules.get_equip_slot(config), []).append((config['required_level_index'], config['price']))
        for values in prices.values():
            ordered = sorted(set(values))
            for previous, current in zip(ordered, ordered[1:]):
                self.assertGreaterEqual(current[1], previous[1])

    def test_panel_equals_battle_and_does_not_mutate_effects(self):
        p = self.suit(18)
        effects = self.combat.collect_combat_effects(p, self.cm)
        original = copy.deepcopy(effects)
        panel = self.combat.build_combat_panel(p, {}, None, self.cm, effects)
        stats = self.combat.build_player_combat_stats(p, self.cm, pill_multipliers={})
        for key in ('atk', 'defense', 'hp', 'max_hp', 'mp', 'max_mp'):
            self.assertEqual(panel[key], getattr(stats, key))
        self.assertEqual(effects, original)

    def test_reduction_is_scale_invariant_and_capped(self):
        for factor in (1, 100, 1000000):
            damage = self.combat.apply_damage_reduction(1000 * factor, 350 * factor, 1000 * factor)
            self.assertAlmostEqual(damage / (1000 * factor), 1 / 1.35, delta=.001)
        self.assertEqual(self.combat.apply_damage_reduction(1000, 10**20, 1000), 250)

    def test_same_realm_gear_advantage_does_not_replace_major_realm(self):
        for low, high in ((0, 10), (9, 13), (18, 22), (28, 32)):
            for seed in range(10):
                random.seed(seed)
                equipped = self.combat.build_player_combat_stats(self.suit(low), self.cm, element='')
                higher = self.combat.build_player_combat_stats(self.Player(user_id='higher', level_index=high), self.cm, element='')
                result = self.combat.player_vs_player(equipped, higher)
                self.assertEqual(result['winner'], 'higher')

    def test_global_caps_cover_post_build_pet_or_external_bonuses(self):
        effects = self.combat.clamp_effects({key: 99999 for key in self.balance.GLOBAL_EFFECT_CAPS})
        self.assertEqual(effects, self.balance.GLOBAL_EFFECT_CAPS)

    def test_all_classes_have_nonzero_realm_defense(self):
        for kind in ('灵修', '体修', '剑修', '魔修', '佛修', '鬼修', '御兽'):
            base = self.balance.realm_attributes(self.Player(user_id=kind, cultivation_type=kind), self.cm)
            self.assertGreater(base['physical_defense'], 0)
            self.assertGreater(base['magic_defense'], 0)

    def test_rank_tables_stay_unified(self):
        rules = _imp('core.item_rules').ItemRules
        self.assertEqual(self.balance.RANK_LEVEL, rules.RANK_REQ_LEVEL)
        self.assertEqual(rules.get_required_level({'rank': '灵品'}), 10)
        self.assertEqual(rules.get_required_level({'rank': '混元先天'}), 35)


class TestBalanceMigration(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TestEquipmentInstances.asyncSetUp
    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    async def test_v44_refreshes_prices_without_touching_assets(self):
        p = await self._player(gold=12345)
        await self.ring.store_item(p, self.weapon, 1)
        before = await self.db.get_player_by_id(p.user_id)
        await self.db.update_shop_data('treasure_pavilion', 100, [{'name': self.weapon, 'price': 1, 'stock': 1}])
        await self.db.conn.execute('UPDATE db_info SET version=43')
        await self.db.conn.commit()
        await self.data.MigrationManager(self.db.conn, self.cm).migrate()
        after = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(before.equipment_instances, after.equipment_instances)
        self.assertEqual(after.gold, 12345)
        self.assertEqual(await self.db.get_shop_data('treasure_pavilion'), (0, []))
        await self.data.MigrationManager(self.db.conn, self.cm).migrate()
        self.assertEqual((await self.db.get_player_by_id(p.user_id)).equipment_instances, before.equipment_instances)
