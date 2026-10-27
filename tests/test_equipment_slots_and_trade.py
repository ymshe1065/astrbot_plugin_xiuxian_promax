"""多部位装备、旧存档迁移和交易图片回归。"""
import json
import unittest
from unittest.mock import AsyncMock, patch
import test_equipment_instances as fixtures
from test_equipment_instances import _imp, _FakeEvent


class TestSlots(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TestEquipmentInstances.asyncSetUp
    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    async def equip(self, player, name, target=""):
        ok, msg = await self.ring.store_item(player, name, 1)
        self.assertTrue(ok, msg)
        inst = self.im.find_for_equip(player, name)
        item = self.em.parse_item_from_name(name, self.cm.items_data, self.cm.weapons_data, inst)
        ok, msg = await self.em.equip_item(player, item, inst["id"], target)
        self.assertTrue(ok, msg)
        return inst

    async def test_all_slots_persist_and_contribute_to_combat(self):
        p = await self._player()
        p.level_index = 0
        expected_def = expected_atk = 0
        for slot, kind in [("weapon", "weapon"), ("armor", "body"), ("head", "head"), ("gloves", "gloves"),
                           ("boots", "boots"), ("ring1", "ring"), ("ring2", "ring"), ("amulet", "amulet")]:
            name = "测试" + slot
            self.cm.items_data[name] = {"name": name, "type": kind, "physical_damage": 10,
                                       "magic_defense": 20, "effects": {"crit_rate": 1}, "price": 1}
            await self.equip(p, name)
            expected_atk += 10
            expected_def += 20
        p = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(set(p.get_equipped_instances()), set(self.core.ItemRules.SLOT_LABELS))
        combat = _imp("managers.combat_manager").CombatManager
        base = self.em.get_base_attributes(p)
        rates = self.em.get_equipment_rates(p)
        self.assertAlmostEqual(rates["physical_damage"], .34)
        self.assertAlmostEqual(rates["physical_defense"], .26)
        expected_bonus = {
            "physical_atk": int(base["physical_damage"] * .34),
            "magic_atk": int(base["magic_damage"] * .34),
            "physical_defense": int(base["physical_defense"] * .26),
            "magic_defense": int(base["magic_defense"] * .26),
        }
        expected_bonus["atk"] = expected_bonus["physical_atk"] + expected_bonus["magic_atk"]
        expected_bonus["defense"] = expected_bonus["physical_defense"] + expected_bonus["magic_defense"]
        self.assertEqual(combat.calculate_equipment_bonus(p, self.cm), expected_bonus)
        self.assertEqual(combat.collect_combat_effects(p, self.cm)["crit_rate"], 8)
        self.assertEqual(self.em.get_combat_effects(p)["crit_rate"], 8)
        self.assertEqual(len(self.em.get_equipped_items(p, self.cm.items_data, self.cm.weapons_data)), 1)
        for slot in self.core.ItemRules.SLOT_LABELS:
            ok, msg = await self.em.unequip_item(p, slot)
            self.assertTrue(ok, msg)
        self.assertEqual(p.get_equipped_instances(), {})
        self.assertEqual(len(self.im.list_instances(p, "storage")), 8)

    async def test_two_same_rings_and_explicit_replacement(self):
        p = await self._player()
        name = next(n for n, c in self.cm.items_data.items() if c.get("type") == "ring" and c["rank"] == "黄阶")
        first = await self.equip(p, name)
        second = await self.equip(p, name)
        third = await self.equip(p, name, "戒指2")
        self.assertEqual(p.get_equipped_instances()["ring1"], first["id"])
        self.assertEqual(p.get_equipped_instances()["ring2"], third["id"])
        self.assertEqual(self.im.get_instance(p, second["id"])["location"], "storage")

    async def test_invalid_instance_or_slot_preserves_old_equipment(self):
        p = await self._player()
        inst = await self.equip(p, self.weapon)
        item = self.em.parse_item_from_name(self.weapon, self.cm.items_data, self.cm.weapons_data)
        before = (p.equipment_instances, p.equipped_instances, p.weapon)
        for instance_id, target in [(99999, ""), (inst["id"], ""), (99999, "戒指2")]:
            ok, _ = await self.em.equip_item(p, item, instance_id, target)
            self.assertFalse(ok)
            self.assertEqual(before, (p.equipment_instances, p.equipped_instances, p.weapon))

    async def test_migration_preserves_instance_and_is_idempotent(self):
        p = await self._player()
        name = next(n for n, c in self.cm.items_data.items() if c.get("equip_slot") == "head")
        inst = self.im.create_instances(p, name, quality="大师", affixes=[{"key": "crit_rate", "value": 5}], location="equipped", slot="armor")[0]
        p.armor = name
        p.set_equipped_instances({"armor": inst["id"]})
        await self.db.update_player(p)
        await self.db.conn.execute("UPDATE db_info SET version=42")
        await self.db.conn.commit()
        await self.data.MigrationManager(self.db.conn, self.cm).migrate()
        p = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(p.armor, "")
        self.assertEqual(p.get_equipped_instances(), {"head": inst["id"]})
        actual = self.im.get_instance(p, inst["id"])
        self.assertEqual(actual["quality"], "大师")
        self.assertEqual(actual["affixes"], inst["affixes"])
        before = p.equipment_instances
        await self.data.migration._migrate_to_v43(self.db.conn, self.cm)
        p = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(before, p.equipment_instances)

    async def test_catalog_has_all_slots_and_shop_routes(self):
        shop = _imp("core.shop_manager").ShopManager({}, self.cm)
        slots = {self.core.ItemRules.get_equip_slot(x["data"]) for x in shop.get_weapons_for_display(9999)}
        self.assertTrue({"weapon", "head", "body", "gloves", "boots", "ring1", "amulet"} <= slots)
        for x in shop.get_weapons_for_display(9999):
            self.assertIn(self.core.ItemRules.normalize_shop_type(x), self.core.ItemRules.GEAR_TYPES)

    async def test_paginated_card_keeps_all_items_and_stats(self):
        Handler = _imp("handlers.shop_handler").ShopHandler
        handler = Handler.__new__(Handler)
        handler.shop_manager = _imp("core.shop_manager").ShopManager({}, self.cm)
        items = [{"name": f"商品{i}", "type": "head", "rank": "黄阶", "price": 100, "stock": 1,
                  "data": {"type": "head", "physical_damage": 11, "magic_defense": 22, "effects": {"crit_rate": 5}}} for i in range(25)]
        image_module = _imp("utils.image_generator")
        with patch.object(image_module, "card_enabled", return_value=True), patch.object(image_module, "render_card_async", new_callable=AsyncMock, return_value="card.jpg") as render:
            cards = await handler._pavilion_cards("百宝阁", items)
        self.assertEqual(len(cards), 5)
        lines = "\n".join(line for call in render.call_args_list for line in call.kwargs["sections"][0]["lines"])
        self.assertIn("商品24", lines)
        self.assertIn("境界基础物防+6.0%", lines)
        self.assertIn("境界基础法防+6.0%", lines)
        self.assertIn("暴击率 +5%", lines)
        self.assertIn("部位: 头饰", lines)
        with patch.object(image_module, "card_enabled", return_value=True), patch.object(image_module, "render_card_async", new_callable=AsyncMock, side_effect=["ok", None]):
            self.assertEqual(await handler._pavilion_cards("百宝阁", items), [])

    async def test_market_details_use_quality_and_affixes(self):
        p = await self._player()
        name = "交易测试护符"
        self.cm.items_data[name] = {"name": name, "type": "amulet", "physical_damage": 100, "price": 100}
        inst = self.im.create_instances(p, name, quality="大师", affixes=[{"key": "crit_rate", "value": 5}], location="auction")[0]
        await self.db.update_player(p)
        auction = self.AuctionManager(self.db, self.ring, {})
        details = "\n".join(await auction.get_trade_details({"item_name": name, "item_kind": "equipment", "seller_id": p.user_id, "instance_id": inst["id"]}))
        self.assertIn("境界基础物伤+2.3%", details)
        self.assertIn("暴击率 +5%", details)

    async def test_empty_equipment_card_and_ring_handler(self):
        handler = _imp("handlers.equipment_handler").EquipmentHandler(self.db, self.cm)
        p = await self._player()
        event = _FakeEvent(p.user_id, p.user_id)
        event.image_result = lambda path: path
        image_module = _imp("utils.image_generator")
        with patch.object(image_module, "card_enabled", return_value=True), patch.object(image_module, "render_card_async", new_callable=AsyncMock, return_value="card.jpg") as render:
            results = [result async for result in handler.handle_show_equipment.__wrapped__(handler, p, event)]
        self.assertEqual(results, ["card.jpg"])
        self.assertIn(("戒指2", "未装备"), render.call_args.kwargs["sections"][0]["rows"])
        name = next(n for n, c in self.cm.items_data.items() if c.get("type") == "ring" and c["rank"] == "黄阶")
        await self.ring.store_item(p, name, 1)
        results = [r async for r in handler.handle_equip_item.__wrapped__(handler, p, event, name + " 1 戒指2")]
        self.assertTrue(p.get_equipped_instances().get("ring2"), results)
        results = [r async for r in handler.handle_unequip_item.__wrapped__(handler, p, event, "戒指2")]
        self.assertEqual(len(self.im.list_instances(p, "storage", name)), 1, results)

    async def test_pill_effects_keep_their_specific_type(self):
        shop = _imp("core.shop_manager").ShopManager({}, self.cm)
        cases = [(self.cm.exp_pills_data, "立即获得修为"), (self.cm.pills_data, "突破成功率"),
                 (self.cm.utility_pills_data, "效果:")]
        for catalog, expected in cases:
            candidates = [shop.get_trade_details({"name": name, "data": cfg}) for name, cfg in catalog.items()]
            self.assertTrue(any(expected in "\n".join(lines) for lines in candidates), expected)

    async def test_ring_purchase_creates_equippable_instance(self):
        p = await self._player(gold=100000)
        name = next(n for n, c in self.cm.items_data.items() if c.get("type") == "ring" and c["rank"] == "黄阶")
        shop = _imp("core.shop_manager").ShopManager({}, self.cm)
        item = shop.find_item_by_name(name)
        item.update(stock=2, price=100)
        await self.db.update_shop_data("treasure_pavilion", 1, [item])
        handler = _imp("handlers.shop_handler").ShopHandler(self.db, {}, self.cm)
        event = _FakeEvent(p.user_id, p.user_id, "/购买 " + name)
        results = [r async for r in handler.handle_buy.__wrapped__(handler, p, event, name)]
        p = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(p.gold, 99900, results)
        self.assertEqual(self.im.count_storage(p, name), 1)

    async def test_wash_targets_second_ring_only(self):
        p = await self._player()
        name = next(n for n, c in self.cm.items_data.items() if c.get("type") == "ring" and c["rank"] == "黄阶")
        first = await self.equip(p, name)
        second = await self.equip(p, name)
        await self.ring.store_item(p, "洗练石", 1)
        forge = _imp("managers.forge_manager").ForgeManager(self.db, self.cm, self.ring)
        ok, msg = await forge.wash_equipment(p, "戒指2")
        self.assertTrue(ok, msg)
        self.assertEqual(self.im.get_instance(p, first["id"])["affixes"], [])
        self.assertTrue(self.im.get_instance(p, second["id"])["affixes"])

    async def test_migration_conflict_returns_old_armor_without_loss(self):
        p = await self._player()
        name = next(n for n, c in self.cm.items_data.items() if c.get("equip_slot") == "head")
        old, current = self.im.create_instances(p, name, count=2, location="equipped", slot="head")
        self.im.move(p, old["id"], "equipped", "armor")
        p.armor = name
        p.set_equipped_instances({"armor": old["id"], "head": current["id"]})
        await self.db.update_player(p)
        await self.data.migration._migrate_to_v43(self.db.conn, self.cm)
        p = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(p.get_equipped_instances(), {"head": current["id"]})
        self.assertEqual(len(self.im.load(p)), 2)
        self.assertEqual(self.im.get_instance(p, old["id"])["location"], "storage")
