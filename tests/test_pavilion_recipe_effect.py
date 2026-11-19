"""丹阁丹方货架的产出效果（F9 补完）。

`/丹阁 丹方` 此前每件只写「记载XX炼制方法的丹方。」——知道怎么买，
不知道买来干什么。而 690 张丹方的产出效果其实全都能算出来：
配方里有产出丹药名，丹药配置里有 exp_gain / breakthrough_bonus /
effect{add_hp,add_experience} / effect_type=temporary。

本文件锁六件事：

1. 货架上每张丹方都有「产出:」效果行，且不再有「效果未录入」；
2. 效果数值取自丹药配置，不是另写一份死文案；
3. 占位描述（记载…丹方）不再出现在详情里；
4. 网页网格的第三行（entry.effect）就是这条效果，用途进明细；
5. 突破丹方额外写清突破目标境界；
6. 真缺数据时仍然显式说「未录入」，不许静默留白。
"""
import sys
import unittest
from pathlib import Path

import test_equipment_instances as fixtures  # noqa: F401  装 astrbot 桩
from test_equipment_instances import _imp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


class TestPavilionRecipeEffect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.sm = _imp("core.shop_manager").ShopManager({"VALUES": {}}, cls.cm)
        cls.recipes = cls.sm.get_recipes_for_display(0)
        cls.breakthrough = cls.sm.get_breakthrough_recipes_for_display(0)

    def _shelf_item(self, name: str) -> dict:
        for item in self.recipes + self.breakthrough:
            if item["name"] == name:
                return {**item, "stock": 3}
        self.fail(f"货架里没有 {name}")

    def _effect_line(self, name: str) -> str:
        details = self.sm.get_trade_details(self._shelf_item(name))
        lines = [d for d in details if d.startswith("产出:")]
        self.assertTrue(lines, f"{name} 没有产出效果行：{details}")
        return lines[0]

    def test_every_recipe_has_a_computable_effect(self):
        """690 张丹方全量检查：产出效果必须算得出来。"""
        blanks = []
        for item in self.recipes + self.breakthrough:
            info = _imp("core.pill_effect_text").recipe_scroll_info(self.cm, item["data"])
            if not info["effect"]:
                blanks.append(item["name"])
        self.assertEqual(blanks, [], f"这些丹方算不出产出效果：{blanks[:10]}")

    def test_effect_line_present_and_not_missing(self):
        """抽样走真实详情链路，确认效果行落到了展示层。"""
        sample = [it["name"] for it in self.recipes[:5]] + [it["name"] for it in self.breakthrough[:3]]
        for name in sample:
            line = self._effect_line(name)
            self.assertNotIn("效果未录入", line, name)

    def test_effect_value_comes_from_pill_config(self):
        """《炼气丹方》的 500 修为来自炼气丹的 exp_gain，不是硬编码文案。"""
        pill = self.cm.exp_pills_data.get("炼气丹") or self.cm.items_data.get("炼气丹")
        self.assertIsInstance(pill, dict)
        gain = int(pill["exp_gain"])
        line = self._effect_line("《炼气丹方》")
        self.assertIn(f"{gain}~", line)
        self.assertIn("修为", line)

    def test_placeholder_description_is_not_shown(self):
        details = self.sm.get_trade_details(self._shelf_item("《炼气丹方》"))
        joined = "；".join(details)
        self.assertNotIn("炼制方法的丹方", joined)

    def test_legacy_effect_data_is_read(self):
        """items.json 里 443 条丹药只写「这是一件X品丹药」，效果在 effect 字段里。"""
        line = self._effect_line("《造化丹方》")
        self.assertIn("气血", line)
        self.assertIn("~", line)

    def test_breakthrough_recipe_shows_target_realm(self):
        details = self.sm.get_trade_details(self._shelf_item("《筑基丹方》"))
        joined = "；".join(details)
        self.assertIn("突破成功率", joined)
        self.assertIn("突破至", joined)
        self.assertIn("筑基期", joined)

    def test_web_entry_third_line_is_the_effect(self):
        """网页格子的第三行取 entry.effect：必须就是产出效果。"""
        entries = self.sm.build_pavilion_entries([self._shelf_item("《炼气丹方》")])
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["kind"], "丹方")
        self.assertIn("修为", entry["effect"])
        self.assertTrue(any("习得后炼制" in note for note in entry["notes"]), entry["notes"])
        # 效果行不能同时出现在明细里，否则详情抽屉同一句话出现两次
        self.assertFalse(any(note.startswith("产出:") for note in entry["notes"]))

    def test_text_shelf_also_carries_effect(self):
        """QQ 端文本货架与卡片读同一份详情，效果必须一起带过去。"""
        text = self.sm.format_pavilion_display("丹方阁", [self._shelf_item("《炼气丹方》")], 0, 0)
        self.assertIn("产出:", text)
        self.assertIn("修为", text)

    def test_bitmap_card_lines_carry_effect(self):
        """QQ 图片卡片走 _pavilion_section_lines，同样得带效果行。"""
        handler = _imp("handlers.shop_handler").ShopHandler.__new__(_imp("handlers.shop_handler").ShopHandler)
        handler.shop_manager = self.sm
        handler.config = {"VALUES": {}}
        count, lines = handler._pavilion_section_lines([self._shelf_item("《炼气丹方》")])
        self.assertEqual(count, 1)
        joined = "\n".join(lines)
        self.assertIn("产出:", joined)
        self.assertIn("修为", joined)

    def test_missing_data_is_explicit_not_blank(self):
        """配方指向不存在的丹药时，不许静默留白。"""
        scroll = {"name": "《查无此丹方》", "type": "丹方", "rank": "凡品", "price": 1, "recipe_id": -1}
        details = self.sm.get_trade_details({**scroll, "type": "recipe", "data": scroll, "stock": 1})
        joined = "；".join(details)
        self.assertIn("效果未录入", joined)

    def test_effect_line_has_no_fake_command_slash(self):
        """效果文案不能带半角斜杠，否则网页上会多出点不动的按钮。"""
        import re
        cjk_slash = re.compile(r"/[\u4e00-\u9fff]")
        for item in self.recipes[:40] + self.breakthrough:
            details = self.sm.get_trade_details({**item, "stock": 1})
            for line in details:
                if line.startswith("产出:"):
                    self.assertIsNone(cjk_slash.search(line), f"效果行含假指令：{line}")


class TestPavilionTalismanRecipeEffect(unittest.TestCase):
    """符阁·符图的详情必须与丹方同口径：买的是产出符箓，不是一张图纸。"""

    @classmethod
    def setUpClass(cls):
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.sm = _imp("core.shop_manager").ShopManager({"VALUES": {}}, cls.cm)
        cls.recipes = cls.sm.get_talisman_recipes_for_display(0)

    def _shelf_item(self, base_name: str) -> dict:
        for item in self.recipes:
            if item["data"].get("base_name") == base_name:
                return {**item, "stock": 3}
        self.fail(f"符图货架里没有 {base_name}")

    def test_every_talisman_recipe_shows_output_effect(self):
        self.assertTrue(self.recipes, "符图货架不应为空")
        for item in self.recipes:
            details = self.sm.get_trade_details({**item, "stock": 1})
            effect_lines = [d for d in details if d.startswith("产出:")]
            self.assertTrue(effect_lines, f"{item['name']} 没有产出效果行：{details}")
            self.assertNotIn("效果未录入", effect_lines[0], item["name"])

    def test_output_line_names_the_crafted_talisman(self):
        item = self._shelf_item("星辰符")
        details = self.sm.get_trade_details(item)
        effect = next(d for d in details if d.startswith("产出:"))
        self.assertIn("星辰符", effect)
        self.assertIn("攻击", effect)

    def test_usage_line_keeps_craft_requirements(self):
        details = self.sm.get_trade_details(self._shelf_item("星辰符"))
        usage = next(d for d in details if d.startswith("用途:"))
        self.assertIn("习得后绘制", usage)
        self.assertIn("需境界等级", usage)
        self.assertIn("成功率", usage)
        self.assertIn("工费", usage)
        self.assertTrue(any(d.startswith("配方材料:") for d in details), details)

    def test_web_entry_uses_output_as_effect(self):
        entries = self.sm.build_pavilion_entries([self._shelf_item("星辰符")])
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["kind"], "符图")
        self.assertEqual(entry["effectLabel"], "产出")
        self.assertIn("星辰符", entry["effect"])
        self.assertTrue(any("习得后绘制" in note for note in entry["notes"]), entry["notes"])
        self.assertFalse(any(note.startswith("产出:") for note in entry["notes"]))

    def test_text_shelf_carries_output_and_usage(self):
        text = self.sm.format_pavilion_display(
            "符阁·符图", [self._shelf_item("星辰符")], 0, 0
        )
        self.assertIn("产出:", text)
        self.assertIn("星辰符", text)
        self.assertIn("用途:", text)


class TestShopShelfDefaultsToFullGrid(unittest.TestCase):
    """货架默认必须给全量，不能落成品阶条的第一档。

    品阶条那套「默认落在第一个可做档」是配方书的语义（我现在能做什么）；
    货架是「这里有什么」，落到单一档位会让 10 张丹方只画得出 1 张。
    这是真实页面（room_server + Chromium）跑出来的问题，单测此前没覆盖到。
    """

    def test_shop_web_section_declares_default_rank(self):
        src = (ROOT / "handlers/shop_handler.py").read_text(encoding="utf-8")
        idx = src.index("def _pavilion_card")
        body = src[idx: idx + 3000]
        self.assertIn('"defaultRank": "*"', body)

    def test_frontend_honours_default_rank(self):
        src = (ROOT / "webchat_room/static/app.js").read_text(encoding="utf-8")
        self.assertIn('sec.defaultRank === "*"', src)
        # 仍要保留配方书的默认落档，否则「现在能做什么」那层优化就没了
        self.assertIn("fallbackRank", src)

    def test_sidecar_passes_default_rank_through(self):
        src = (ROOT / "webchat_room/card_sidecar.py").read_text(encoding="utf-8")
        self.assertIn('sec.get("defaultRank") == "*"', src)


if __name__ == "__main__":
    unittest.main()
