"""多部位装备、旧存档迁移和交易图片回归。"""
import json
import time
import unittest
from unittest.mock import AsyncMock, patch
import test_equipment_instances as fixtures
from test_equipment_instances import _imp, _FakeEvent, PKG


class TestSlots(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)
        self.im.roll_quality = lambda: "一般"
        self.ring.instance_manager.roll_quality = lambda: "一般"

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
        name = next(n for n, c in self.cm.items_data.items() if c.get("type") == "ring" and c["rank"] == "凡品")
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

    async def test_pavilion_cards_are_single_long_image(self):
        Handler = _imp("handlers.shop_handler").ShopHandler
        handler = Handler.__new__(Handler)
        handler.shop_manager = _imp("core.shop_manager").ShopManager({}, self.cm)
        items = [{"name": f"商品{i}", "type": "head", "rank": "凡品" if i < 10 else "黄品",
                  "price": 100, "stock": 1,
                  "data": {"type": "head", "physical_damage": 11, "magic_defense": 22,
                           "effects": {"crit_rate": 5}}} for i in range(25)]
        image_module = _imp("utils.image_generator")
        with patch.object(image_module, "card_enabled", return_value=True), patch.object(image_module, "render_card_async", new_callable=AsyncMock, return_value="card.jpg") as render:
            cards = await handler._pavilion_cards("百宝阁", items)
        # 百宝阁只出一张长图，品阶在同一张图内分区
        self.assertEqual(len(cards), 1)
        self.assertEqual(render.call_count, 1)
        sections = render.call_args.kwargs["sections"]
        self.assertEqual([s["name"] for s in sections], ["凡品区", "黄品区"])
        lines = "\n".join(line for section in sections for line in section["lines"])
        self.assertIn("商品24", lines)
        self.assertIn("境界基础物防+6.0%", lines)
        self.assertIn("境界基础法防+6.0%", lines)
        self.assertIn("暴击率 +5%", lines)
        self.assertIn("部位: 头饰", lines)
        for name in ("丹阁", "器阁", "百宝阁"):
            with patch.object(image_module, "card_enabled", return_value=True), patch.object(image_module, "render_card_async", new_callable=AsyncMock, return_value="card.jpg") as render:
                cards = await handler._pavilion_cards(name, items)
            self.assertEqual(len(cards), 1, name)
            self.assertEqual(render.call_count, 1, name)
        with patch.object(image_module, "card_enabled", return_value=True), patch.object(image_module, "render_card_async", new_callable=AsyncMock, return_value=None):
            self.assertEqual(await handler._pavilion_cards("百宝阁", items), [])

    async def test_equip_message_uses_instance_quality_and_shows_base(self):
        p = await self._player()
        inst = self.im.create_instances(p, self.weapon, quality="大师", location="storage")[0]
        await self.db.update_player(p)
        handler = _imp("handlers.equipment_handler").EquipmentHandler(self.db, self.cm)
        event = _FakeEvent(p.user_id, p.user_id, text=f"/装备 {self.weapon}")
        results = [r async for r in handler.handle_equip_item.__wrapped__(handler, p, event, self.weapon)]
        text = "\n".join(results)
        self.assertIn("属性加成：", text)
        self.assertIn("品质：大师（主属性×1.15）", text)
        self.assertIn("基础属性：", text)
        base = "\n".join(self.shop_manager().get_item_details(
            self.shop_manager().find_item_by_name(self.weapon)
        ).splitlines())
        self.assertIn("基础属性:", base)
        self.assertIn("品质浮动:", base)

    def shop_manager(self):
        return _imp("core.shop_manager").ShopManager({}, self.cm)

    async def test_pavilion_gear_gets_quality_and_displays_effective_stats(self):
        shop = self.shop_manager()
        items = shop.generate_pavilion_items(shop.get_weapons_for_display, 9999)
        gear = [x for x in items if shop.is_gear_entry(x)]
        self.assertTrue(gear)
        item = gear[0]
        self.assertIn(item.get("quality"), ("一般", "良好", "极佳", "大师"))
        item["quality"] = "大师"
        details = "\n".join(shop.get_trade_details(item))
        parsed = self.em.parse_item_from_name(
            item["name"], {item["name"]: item["data"]}, instance={"quality": "大师"}
        )
        self.assertIn(parsed.get_attribute_display(), details)
        scale_label = "主属性" if parsed.get_attribute_display() else "特殊效果"
        self.assertIn(f"品质: 大师（{scale_label}×1.15）", details)

        legacy = {"name": item["name"], "type": item["type"], "rank": item["rank"],
                  "price": item["price"], "data": item["data"]}
        self.assertTrue(shop.ensure_equipment_quality([legacy]))
        self.assertIn(legacy["quality"], ("一般", "良好", "极佳", "大师"))

    async def test_shop_purchase_uses_shelf_quality(self):
        p = await self._player(gold=100000)
        name = next(n for n, c in self.cm.items_data.items() if c.get("type") == "ring" and c["rank"] == "凡品")
        shop = self.shop_manager()
        item = shop.find_item_by_name(name)
        item.update(stock=1, price=100, quality="大师")
        await self.db.update_shop_data("treasure_pavilion", 1, [item])
        handler = _imp("handlers.shop_handler").ShopHandler(self.db, {}, self.cm)
        event = _FakeEvent(p.user_id, p.user_id, "/购买 " + name)
        results = [r async for r in handler.handle_buy.__wrapped__(handler, p, event, name)]
        p = await self.db.get_player_by_id(p.user_id)
        instances = self.im.list_instances(p, "storage", name)
        self.assertEqual(len(instances), 1, results)
        self.assertEqual(instances[0]["quality"], "大师")

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
        name = next(n for n, c in self.cm.items_data.items() if c.get("type") == "ring" and c["rank"] == "凡品")
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
        name = next(n for n, c in self.cm.items_data.items() if c.get("type") == "ring" and c["rank"] == "凡品")
        shop = _imp("core.shop_manager").ShopManager({}, self.cm)
        item = shop.find_item_by_name(name)
        item.update(stock=2)
        await self.db.update_shop_data("treasure_pavilion", 1, [item])
        handler = _imp("handlers.shop_handler").ShopHandler(self.db, {}, self.cm)
        event = _FakeEvent(p.user_id, p.user_id, "/购买 " + name)
        results = [r async for r in handler.handle_buy.__wrapped__(handler, p, event, name)]
        p = await self.db.get_player_by_id(p.user_id)
        expected_price = int(self.cm.items_data[name].get("price", 0))
        self.assertEqual(p.gold, 100000 - expected_price, results)
        self.assertEqual(self.im.count_storage(p, name), 1)

    async def test_wash_targets_second_ring_only(self):
        p = await self._player()
        name = next(n for n, c in self.cm.items_data.items() if c.get("type") == "ring" and c["rank"] == "凡品")
        first = await self.equip(p, name)
        second = await self.equip(p, name)
        await self.ring.store_item(p, "洗练石", 1)
        forge = _imp("managers.forge_manager").ForgeManager(self.db, self.cm, self.ring)
        ok, msg = await forge.wash_equipment(p, "戒指2")
        self.assertTrue(ok, msg)
        self.assertEqual(self.im.get_instance(p, first["id"])["affixes"], [])
        self.assertTrue(self.im.get_instance(p, second["id"])["affixes"])

    async def test_targeted_wash_uses_monster_material_and_specified_affix(self):
        p = await self._player()
        inst = await self.equip(p, self.weapon)
        p.gold = 1_000_000
        await self.ring.store_item(p, "洗练石", 2)
        await self.ring.store_item(p, "诛仙龙骨铁", 2)
        forge = _imp("managers.forge_manager").ForgeManager(self.db, self.cm, self.ring)
        with patch(f"{PKG}.managers.forge_manager.random.random", return_value=0.0):
            ok, msg = await forge.targeted_wash(
                p, "武器", "暴击率", "诛仙龙骨铁", 2
            )
        self.assertTrue(ok, msg)
        affixes = self.im.get_instance(p, inst["id"])["affixes"]
        self.assertEqual(affixes[-1]["key"], "crit_rate")
        self.assertIn("暴击率+", affixes[-1]["display"])
        self.assertIn("定向洗练成功", msg)

    async def test_targeted_wash_rejects_non_monster_material(self):
        p = await self._player()
        await self.equip(p, self.weapon)
        await self.ring.store_item(p, "洗练石", 1)
        forge = _imp("managers.forge_manager").ForgeManager(self.db, self.cm, self.ring)
        ok, msg = await forge.targeted_wash(p, "武器", "暴击率", "灵草", 1)
        self.assertFalse(ok)
        self.assertIn("不是妖兽材料", msg)

    async def test_accessory_group_alias_equips_amulet_and_rings(self):
        p = await self._player()
        p.level_index = 0
        amulet, ring = "测试饰品护符", "测试饰品戒指"
        self.cm.items_data[amulet] = {"name": amulet, "type": "amulet", "physical_damage": 5, "price": 1}
        self.cm.items_data[ring] = {"name": ring, "type": "ring", "physical_damage": 5, "price": 1}
        handler = _imp("handlers.equipment_handler").EquipmentHandler(self.db, self.cm)
        event = _FakeEvent(p.user_id, p.user_id)
        expected_slots = ("amulet", "ring1", "ring2")
        for name, expected_slot in zip((amulet, ring, ring), expected_slots):
            ok, msg = await self.ring.store_item(p, name, 1)
            self.assertTrue(ok, msg)
            results = [r async for r in handler.handle_equip_item.__wrapped__(handler, p, event, f"{name} 饰品")]
            self.assertIn(expected_slot, p.get_equipped_instances(), results)
        self.assertEqual(set(p.get_equipped_instances()), set(expected_slots))

    async def test_technique_slot_replacement_and_unequip_by_index(self):
        """回归：/装备 功法名 2 替换第2个功法；/卸下 功法 2 按栏位卸下。"""
        p = await self._player()
        p.level_index = 0
        names = ["测试功法甲", "测试功法乙", "测试功法丙", "测试功法丁"]
        for name in names:
            self.cm.techniques_data[name] = {
                "name": name, "type": "功法", "rank": "凡品",
                "life_effects": {"gold_bonus": 0.01},
            }
        handler = _imp("handlers.equipment_handler").EquipmentHandler(self.db, self.cm)
        event = _FakeEvent(p.user_id, p.user_id)

        for name in names[:3]:
            ok, msg = await self.ring.store_item(p, name, 1)
            self.assertTrue(ok, msg)
            results = [r async for r in handler.handle_equip_item.__wrapped__(handler, p, event, name)]
            self.assertIn("已装备功法", "".join(results), results)
        self.assertEqual(p.get_techniques_list(), names[:3])

        # 满栏时替换第2个
        ok, msg = await self.ring.store_item(p, names[3], 1)
        self.assertTrue(ok, msg)
        results = [r async for r in handler.handle_equip_item.__wrapped__(handler, p, event, f"{names[3]} 2")]
        self.assertIn("替换", "".join(results), results)
        self.assertEqual(p.get_techniques_list(), [names[0], names[3], names[2]])
        self.assertTrue(self.ring.has_item(p, names[1], 1), results)

        # 按栏位卸下第2个
        results = [r async for r in handler.handle_unequip_item.__wrapped__(handler, p, event, "功法 2")]
        self.assertIn("栏位 2", "".join(results), results)
        self.assertEqual(p.get_techniques_list(), [names[0], names[2]])
        self.assertTrue(self.ring.has_item(p, names[3], 1), results)

        # 名称与栏位不符时拒绝卸下
        results = [r async for r in handler.handle_unequip_item.__wrapped__(handler, p, event, f"{names[0]} 2")]
        self.assertIn("不是 2", "".join(results), results)
        self.assertEqual(p.get_techniques_list(), [names[0], names[2]])

    async def test_unequip_ring_by_name_and_slot_index(self):
        """回归：/卸下 戒指名 2 卸下戒指2；/卸下 戒指 1 卸下戒指1。"""
        p = await self._player()
        p.level_index = 0
        name = "测试栏位戒指"
        self.cm.items_data[name] = {"name": name, "type": "ring", "physical_damage": 3, "price": 1}
        handler = _imp("handlers.equipment_handler").EquipmentHandler(self.db, self.cm)
        event = _FakeEvent(p.user_id, p.user_id)

        ok, msg = await self.ring.store_item(p, name, 2)
        self.assertTrue(ok, msg)
        for slot in ("戒指1", "戒指2"):
            results = [r async for r in handler.handle_equip_item.__wrapped__(handler, p, event, f"{name} 1 {slot}")]
            self.assertIn(slot, "".join(results), results)
        self.assertEqual(
            set(p.get_equipped_instances()), {"ring1", "ring2"}
        )

        results = [r async for r in handler.handle_unequip_item.__wrapped__(handler, p, event, f"{name} 2")]
        self.assertIn("戒指2", "".join(results), results)
        self.assertNotIn("ring2", p.get_equipped_instances())
        self.assertIn("ring1", p.get_equipped_instances())

        results = [r async for r in handler.handle_unequip_item.__wrapped__(handler, p, event, "戒指 1")]
        self.assertIn("戒指1", "".join(results), results)
        self.assertNotIn("ring1", p.get_equipped_instances())

        results = [r async for r in handler.handle_unequip_item.__wrapped__(handler, p, event, "戒指 2")]
        self.assertIn("未装备戒指2", "".join(results), results)

        # 显式部位与序号冲突时拒绝
        results = [r async for r in handler.handle_unequip_item.__wrapped__(handler, p, event, "戒指1 2")]
        self.assertIn("不匹配", "".join(results), results)

    async def test_explicit_ring_slot_argument_is_honoured(self):
        """回归：/装备 戒指 戒指2 必须装到戒指2，不能与戒指1互相覆盖。"""
        p = await self._player()
        p.level_index = 0
        name = "测试灵纹戒指"
        self.cm.items_data[name] = {"name": name, "type": "ring", "physical_damage": 3, "price": 1}
        handler = _imp("handlers.equipment_handler").EquipmentHandler(self.db, self.cm)
        event = _FakeEvent(p.user_id, p.user_id)

        ok, msg = await self.ring.store_item(p, name, 2)
        self.assertTrue(ok, msg)
        results = [r async for r in handler.handle_equip_item.__wrapped__(handler, p, event, f"{name} 戒指2")]
        mapping = p.get_equipped_instances()
        self.assertIn("ring2", mapping, results)
        self.assertNotIn("ring1", mapping, results)
        ring2_id = mapping["ring2"]
        self.assertIn("戒指2", "".join(results))

        results = [r async for r in handler.handle_equip_item.__wrapped__(handler, p, event, f"{name} 戒指1")]
        mapping = p.get_equipped_instances()
        self.assertIn("ring1", mapping, results)
        self.assertEqual(mapping["ring2"], ring2_id, "装备戒指1不应覆盖已装备的戒指2")
        self.assertNotEqual(mapping["ring1"], ring2_id)

    async def test_purchase_prefers_last_viewed_pavilion(self):
        p = await self._player(gold=100000)
        name = next(n for n, c in self.cm.items_data.items() if c.get("type") == "ring" and c["rank"] == "凡品")
        shop = self.shop_manager()
        cheap = shop.find_item_by_name(name)
        cheap.update(stock=1, price=100, quality="大师")
        pricey = dict(cheap); pricey.update(price=500, quality="一般")
        await self.db.update_shop_data("weapon_pavilion", 1, [pricey])
        await self.db.update_shop_data("treasure_pavilion", 1, [cheap])
        handler = _imp("handlers.shop_handler").ShopHandler(self.db, {}, self.cm)
        event = _FakeEvent(p.user_id, p.user_id, "/购买 " + name)
        handler._remember_viewed_pavilion(event, "treasure_pavilion")
        results = [r async for r in handler.handle_buy.__wrapped__(handler, p, event, name)]
        p = await self.db.get_player_by_id(p.user_id)
        instances = self.im.list_instances(p, "storage", name)
        self.assertEqual(len(instances), 1, results)
        self.assertEqual(instances[0]["quality"], "大师")
        _, treasure = await self.db.get_shop_data("treasure_pavilion")
        _, weapon = await self.db.get_shop_data("weapon_pavilion")
        self.assertEqual(int(treasure[0]["stock"]), 0, results)
        self.assertEqual(int(weapon[0]["stock"]), 1, results)

    def test_shop_and_pavilion_labels_cover_accessory_slots(self):
        shop = _imp("core.shop_manager").ShopManager({}, self.cm)
        names = {
            "amulet": next(n for n, c in self.cm.items_data.items() if c.get("equip_slot") == "amulet"),
            "ring": next(n for n, c in self.cm.items_data.items() if c.get("type") == "ring"),
        }
        expected = {"amulet": "护符", "ring": "戒指"}
        for kind, name in names.items():
            item = shop.find_item_by_name(name)
            item["stock"] = 1
            text = shop.format_pavilion_display("百宝阁", [item], 1, int(time.time()))
            self.assertIn(expected[kind], text)

    async def test_equip_reports_overflow_location(self):
        p = await self._player()
        p.level_index = 0
        name = "测试溢出戒指"
        self.cm.items_data[name] = {"name": name, "type": "ring", "physical_damage": 5, "price": 1}
        self.im.create_instances(p, name, quality="一般", location="overflow")
        handler = _imp("handlers.equipment_handler").EquipmentHandler(self.db, self.cm)
        event = _FakeEvent(p.user_id, p.user_id)
        results = [r async for r in handler.handle_equip_item.__wrapped__(handler, p, event, name)]
        text = "\n".join(results)
        self.assertIn("溢出", text)
        self.assertIn("/领取溢出", text)

    async def test_sect_shop_backfills_missing_stock(self):
        p = await self._player()
        shop = _imp("core.shop_manager").ShopManager({}, self.cm)
        name = next(n for n, c in self.cm.items_data.items() if c.get("equip_slot") == "amulet")
        item = shop.find_item_by_name(name)
        item.pop("stock", None)
        await self.db.update_shop_data("sect_shop_1", int(time.time()), [item])
        handler = _imp("handlers.sect_shop_handler").SectShopHandler(self.db, shop, self.ring, {}, None)
        _, _, items = await handler._get_shop(1, p)
        self.assertGreater(items[0].get("stock", 0), 0)
        _, persisted = await self.db.get_shop_data("sect_shop_1")
        self.assertGreater(persisted[0].get("stock", 0), 0)

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
