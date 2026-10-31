# tests/test_display_updates.py
"""单图货物架、名称完整显示与装备比较回归。"""
import unittest
from unittest.mock import AsyncMock, patch

import test_equipment_instances as fixtures
from test_equipment_instances import ROOT, _imp, _FakeEvent


class TestRealmRange(unittest.TestCase):
    def test_applicable_realm_uses_rank_band(self):
        cm = _imp("config_manager").ConfigManager(ROOT)
        realm = _imp("core.realm_rules")
        # 天品默认解锁 13（金丹期初期），下一档皇品 16，故区间止于 15（金丹期后期）
        text = realm.format_applicable_realm(
            13, cm.level_data, cm.body_level_data, rank="天品"
        )
        self.assertIn("金丹期初期", text)
        self.assertIn("金丹期后期", text)
        # 最高品阶一直覆盖到最高境界
        top = realm.format_applicable_realm(
            35, cm.level_data, cm.body_level_data, rank="混元先天"
        )
        self.assertIn("混元大罗金仙", top)


class TestFullNameRendering(unittest.TestCase):
    def test_long_item_name_is_not_truncated(self):
        image_module = _imp("utils.image_generator")
        if not image_module.HAS_PIL:
            self.skipTest("未安装 Pillow")
        rendered = []

        def capture(canvas, xy, text, *args, **kwargs):
            rendered.append(str(text))
            return xy[0]

        def capture_draw(draw_self, xy, text, *args, **kwargs):
            rendered.append(str(text))

        with patch.object(image_module, "_draw_mixed", side_effect=capture), \
                patch.object(image_module.ImageDraw.ImageDraw, "text", capture_draw):
            path = image_module.render_card(
                "bag", "道友 的储物戒", "",
                sections=[{"name": "物品清单", "rows": [
                    ("六品破境增益丹", "x1"),
                    ("噬魂幽冥草草", "x3"),
                ]}],
                footer="",
            )
        self.assertTrue(path)
        joined = "".join(rendered)
        self.assertIn("六品破境增益丹", joined)
        self.assertIn("噬魂幽冥草草", joined)
        self.assertNotIn("…", joined)
        import os
        os.remove(path)


class TestWrappedRowLayout(unittest.TestCase):
    def test_wrapped_value_lines_use_separate_rows(self):
        image_module = _imp("utils.image_generator")
        if not image_module.HAS_PIL:
            self.skipTest("未安装 Pillow")
        drawn = []

        def capture(canvas, xy, text, *args, **kwargs):
            drawn.append((str(text), xy[1]))
            return xy[0]

        with patch.object(image_module, "_draw_mixed", side_effect=capture):
            path = image_module.render_card(
                "bag", "道友 的信息", "",
                sections=[{"name": "装备与宗门", "rows": [
                    ("功法生活", "灵石+3.0% · 掉落+1.0% · 冷却-1.0% · 气运+5 · 额外加成"),
                ]}],
                footer="",
            )
        self.assertTrue(path)
        value_ys = [y for text, y in drawn if "灵石+3.0%" in text or "额外加成" in text]
        self.assertGreaterEqual(len(value_ys), 2)
        self.assertEqual(len(value_ys), len(set(value_ys)), "折行文字不应叠在同一基线上")
        import os
        os.remove(path)


class TestEquipmentCompare(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    def _handler(self):
        return _imp("handlers.equipment_handler").EquipmentHandler(self.db, self.cm)

    async def test_card_contains_per_item_bonus(self):
        p = await self._player()
        await self.ring.store_item(p, self.weapon, 1)
        p = await self.db.get_player_by_id(p.user_id)
        inst = self.im.find_for_equip(p, self.weapon)
        item = self.em.parse_item_from_name(self.weapon, self.cm.items_data, self.cm.weapons_data, inst)
        await self.em.equip_item(p, item, inst["id"])
        p = await self.db.get_player_by_id(p.user_id)

        event = _FakeEvent(p.user_id, p.user_id)
        event.image_result = lambda path: path
        handler = self._handler()
        image_module = _imp("utils.image_generator")
        with patch.object(image_module, "card_enabled", return_value=True), \
                patch.object(image_module, "render_card_async", new_callable=AsyncMock,
                             return_value="card.jpg") as render:
            results = [r async for r in handler.handle_show_equipment.__wrapped__(handler, p, event)]
        self.assertEqual(results, ["card.jpg"])
        names = [section["name"] for section in render.call_args.kwargs["sections"]]
        self.assertIn("单件加成", names)
        bonus_section = next(s for s in render.call_args.kwargs["sections"] if s["name"] == "单件加成")
        bonus_text = "\n".join(bonus_section["lines"])
        self.assertIn(self.weapon, bonus_text)
        self.assertIn("物伤", bonus_text)

    async def test_card_shows_per_technique_bonus(self):
        p = await self._player()
        tech = next(
            name for name, cfg in self.cm.items_data.items()
            if cfg.get("type") in ("功法", "technique") and cfg.get("life_effects")
        )
        p.set_techniques_list([tech])
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)

        event = _FakeEvent(p.user_id, p.user_id)
        event.image_result = lambda path: path
        handler = self._handler()
        image_module = _imp("utils.image_generator")
        with patch.object(image_module, "card_enabled", return_value=True), \
                patch.object(image_module, "render_card_async", new_callable=AsyncMock,
                             return_value="card.jpg") as render:
            results = [r async for r in handler.handle_show_equipment.__wrapped__(handler, p, event)]
        self.assertEqual(results, ["card.jpg"])
        tech_section = next(s for s in render.call_args.kwargs["sections"] if s["name"] == "功法")
        tech_text = "\n".join(tech_section["lines"])
        self.assertIn(tech, tech_text)
        self.assertRegex(tech_text, r"(灵石收益|掉落几率|冷却缩减|气运|修炼效率)")

    async def test_overview_includes_aggregated_special_effects(self):
        p = await self._player()
        name = "测试总览戒"
        self.cm.items_data[name] = {
            "name": name, "type": "饰品", "subtype": "戒指", "equip_slot": "ring1",
            "rank": "凡品", "required_level_index": 0, "price": 100,
            "equipment_rates": {"physical_damage": 0.02, "magic_damage": 0.02},
            "effects": {"crit_rate": 3, "lifesteal": 2},
        }
        await self.ring.store_item(p, name, 1)
        p = await self.db.get_player_by_id(p.user_id)
        inst = self.im.find_for_equip(p, name)
        item = self.em.parse_item_from_name(name, self.cm.items_data, self.cm.weapons_data, inst)
        await self.em.equip_item(p, item, inst["id"])
        p = await self.db.get_player_by_id(p.user_id)

        event = _FakeEvent(p.user_id, p.user_id)
        event.image_result = lambda path: path
        handler = self._handler()
        image_module = _imp("utils.image_generator")
        with patch.object(image_module, "card_enabled", return_value=True), \
                patch.object(image_module, "render_card_async", new_callable=AsyncMock,
                             return_value="card.jpg") as render:
            results = [r async for r in handler.handle_show_equipment.__wrapped__(handler, p, event)]

        self.assertEqual(results, ["card.jpg"])
        overview = next(s for s in render.call_args.kwargs["sections"] if s["name"] == "加成总览")
        text = "\n".join(overview["lines"])
        self.assertIn("暴击率", text)
        self.assertIn("吸血", text)

    async def test_compare_with_empty_slot_shows_actual_bonus(self):
        p = await self._player()
        await self.ring.store_item(p, self.weapon, 1)
        p = await self.db.get_player_by_id(p.user_id)
        handler = self._handler()
        event = _FakeEvent(p.user_id, p.user_id, text=f"/我的装备 比较 {self.weapon}")
        results = [r async for r in handler.handle_show_equipment.__wrapped__(handler, p, event)]
        text = "\n".join(results)
        self.assertIn("空槽位", text)
        self.assertIn("物伤", text)
        self.assertIn("适用境界", text)

    async def test_compare_with_equipped_slot_shows_delta(self):
        p = await self._player()
        await self.ring.store_item(p, self.weapon, 1)
        p = await self.db.get_player_by_id(p.user_id)
        inst = self.im.find_for_equip(p, self.weapon)
        item = self.em.parse_item_from_name(self.weapon, self.cm.items_data, self.cm.weapons_data, inst)
        await self.em.equip_item(p, item, inst["id"])
        p = await self.db.get_player_by_id(p.user_id)

        strong = "测试神兵"
        self.cm.items_data[strong] = {
            "name": strong, "type": "weapon", "rank": "圣品", "price": 1,
            "required_level_index": 0,
            "equipment_rates": {"physical_damage": 0.30, "magic_damage": 0.30},
        }
        await self.ring.store_item(p, strong, 1)
        p = await self.db.get_player_by_id(p.user_id)
        self.im.roll_quality = lambda: "一般"
        handler = self._handler()
        event = _FakeEvent(p.user_id, p.user_id, text=f"/我的装备 比较 {strong}")
        results = [r async for r in handler.handle_show_equipment.__wrapped__(handler, p, event)]
        text = "\n".join(results)
        self.assertIn("vs 已穿戴", text)
        self.assertIn("▲", text)


if __name__ == "__main__":
    unittest.main()
