"""指定材料的扣料、分步状态与前端候选回归。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_alchemy_batch import _imp, _make_mgr
import test_talisman_craft_book as talisman_fixtures

selection = _imp('core.material_selection')
arg_options = _imp('webchat_room.arg_options')

INFO = {
    '幽冥草': {'category': '灵植', 'tier': '圣品'},
    '九幽仙芝': {'category': '灵植', 'tier': '圣品'},
    '灵草': {'category': '灵植', 'tier': '凡品'},
    '圣矿': {'category': '矿材', 'tier': '圣品'},
}


def make_selected_manager():
    mgr, db, player = _make_mgr({'幽冥草': 5, '九幽仙芝': 20}, gold=10000)
    mgr.recipes[1]['materials'] = [{'category': '灵植', 'tier': '圣品', 'count': 2}]
    mgr._material_info = lambda name: INFO.get(name, {})
    return mgr, db, player


class MaterialSelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_and_validate(self):
        args, mats = selection.split_material_options(['-', 'all', '圣品灵植＝幽冥草'])
        self.assertEqual(args, ['all'])
        self.assertEqual(mats, {'圣品灵植': '幽冥草'})
        for tokens in (['圣品灵植='], ['=幽冥草'], ['圣品灵植=幽冥草', '圣品灵植=九幽仙芝']):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                selection.split_material_options(tokens)

    async def test_all_only_consumes_selected_stack(self):
        mgr, db, player = make_selected_manager()
        with patch('random.randint', return_value=1):
            ok, msg, data = await mgr.craft_pill('u1', 1, '守中', 'all', {'圣品灵植': '幽冥草'})
        self.assertTrue(ok, msg)
        self.assertEqual(data['crafted'], 2)
        self.assertEqual(player.get_storage_ring_items(), {'幽冥草': 1, '九幽仙芝': 20})
        self.assertNotIn('name', mgr.recipes[1]['materials'][0])

    async def test_pending_fire_keeps_material_choice(self):
        mgr, db, player = make_selected_manager()
        ok, msg, data = await mgr.craft_pill('u1', 1, count='all', materials={'圣品灵植': '幽冥草'})
        self.assertTrue(ok, msg)
        self.assertIn('幽冥草', msg)
        self.assertEqual(player.get_storage_ring_items()['幽冥草'], 5)
        await mgr.set_fire_choice('u1', '2')
        with patch('random.randint', return_value=1):
            ok, msg, data = await mgr.craft_pill('u1', 1)
        self.assertTrue(ok, msg)
        self.assertEqual(data['crafted'], 2)
        self.assertEqual(player.get_storage_ring_items()['九幽仙芝'], 20)

    async def test_missing_or_invalid_selection_never_spends(self):
        for key, name in [('圣品灵植', '灵草'), ('圣品矿材', '圣矿'), ('圣品灵植', '不存在'), ('圣品灵植', '幽冥草')]:
            with self.subTest(key=key, name=name):
                mgr, db, player = make_selected_manager()
                player.set_storage_ring_items({'九幽仙芝': 20})
                ok, msg, _ = await mgr.craft_pill('u1', 1, '守中', materials={key: name})
                self.assertFalse(ok, msg)
                self.assertEqual(player.gold, 10000)
                self.assertEqual(player.get_storage_ring_items(), {'九幽仙芝': 20})

    def test_multiple_categories_and_fixed_materials(self):
        recipe = {'materials': [
            {'category': '灵植', 'tier': '圣品', 'count': 2},
            {'category': '矿材', 'tier': '圣品', 'count': 1},
            {'name': '固定材料', 'count': 1},
        ]}
        result = selection.select_recipe_materials(recipe, {'圣品灵植': '幽冥草', '圣品矿材': '圣矿'}, INFO.get)
        self.assertEqual([s['name'] for s in result['materials']], ['幽冥草', '圣矿', '固定材料'])
        self.assertNotIn('name', recipe['materials'][0])
        with self.assertRaises(ValueError):
            selection.select_recipe_materials(recipe, {'固定材料': '幽冥草'}, INFO.get)

    async def test_web_candidates_filter_ring_and_recipe(self):
        mgr, db, player = make_selected_manager()
        player.set_storage_ring_items({'幽冥草': 5, '灵草': 5, '圣矿': 9})
        plugin = SimpleNamespace(db=db, alchemy_mgr=mgr)
        data = await arg_options.build_arg_options(plugin, 'u1', '炼丹', ['1', '-', '-'])
        self.assertTrue(data['field']['optional'])
        self.assertEqual([o['value'] for o in data['options']], ['圣品灵植=幽冥草'])
        data = await arg_options.build_arg_options(plugin, 'u1', '炼丹', ['1', '-', '-', '圣品灵植=幽冥草'])
        self.assertEqual(data['options'], [])

    def test_explicit_count_is_not_legacy_fire(self):
        parser = _imp('handlers.alchemy_handlers').AlchemyHandlers._parse_craft_options
        self.assertEqual(parser(['x1']), (None, 1, ''))
        self.assertEqual(parser(['1']), ('1', None, ''))


class SelectedTalismanTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = talisman_fixtures.TestTalismanCraftBook.asyncSetUp
    asyncTearDown = talisman_fixtures.TestTalismanCraftBook.asyncTearDown
    _player = talisman_fixtures.TestTalismanCraftBook._player

    async def test_selected_talisman_stops_without_fallback(self):
        herbs = [n for n, i in self.cm.material_catalog.items() if i['category'] == '灵植' and i['tier'] == '凡品'][:2]
        self.player.set_storage_ring_items({herbs[0]: 2, herbs[1]: 9})
        with patch.object(self.tm_mod.random, 'randint', return_value=1):
            ok, msg = await self.tm.craft_talisman(self.player, '稳魂符', 100, {'凡品灵植': herbs[0]})
        self.assertTrue(ok, msg)
        fresh = await self.db.get_player_by_id(self.player.user_id)
        self.assertEqual(fresh.get_storage_ring_items().get(herbs[0], 0), 0)
        self.assertEqual(fresh.get_storage_ring_items()[herbs[1]], 9)

    async def test_invalid_talisman_material_leaves_inventory_untouched(self):
        self.player.set_storage_ring_items({'幽冥草': 5})
        gold = self.player.gold
        ok, msg = await self.tm.craft_talisman(self.player, '稳魂符', 1, {'凡品灵植': '幽冥草'})
        self.assertFalse(ok, msg)
        self.assertEqual(self.player.gold, gold)
        self.assertEqual(self.player.get_storage_ring_items(), {'幽冥草': 5})

    async def test_talisman_material_picker(self):
        herbs = [n for n, i in self.cm.material_catalog.items() if i['category'] == '灵植' and i['tier'] == '凡品']
        self.player.set_storage_ring_items({herbs[0]: 2, '幽冥草': 3})
        options, _ = await arg_options._src_craft_materials(
            SimpleNamespace(talisman_mgr=self.tm), self.player, {'craft': '制符'}, ['稳魂符', '-'])
        self.assertEqual([o['value'] for o in options], [f'凡品灵植={herbs[0]}'])
