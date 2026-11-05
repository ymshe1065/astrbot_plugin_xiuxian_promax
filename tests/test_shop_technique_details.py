# -*- coding: utf-8 -*-
"""百宝阁物品详情的文案回归：不露原始键名、不整段重复。

货架卡片（图片与网页共用 lines）曾经把心法的 effects 输出两遍：
一遍是未翻译的「效果: burn_chance+17, ...」，一遍是「特效: burn_chance +17」，
并且功法自带描述里的绝技说明又被「说明:」重复一次。
"""
import unittest

from test_equipment_instances import _imp, ROOT

TECHNIQUE = "焚天煮海道典"


class TestShopTechniqueDetails(unittest.TestCase):
    def setUp(self):
        self.cm = _imp("config_manager").ConfigManager(ROOT)
        self.shop = _imp("core.shop_manager").ShopManager({}, self.cm)

    def _item(self, name, price=3134794, discount=0.78):
        data = dict(self.cm.techniques_data[name])
        return {"name": name, "rank": data.get("rank", "帝品"), "price": price,
                "type": "main_technique", "stock": 1, "discount": discount,
                "element": data.get("element", ""), "data": data}

    def test_effects_are_translated_not_raw_keys(self):
        lines = self.shop.get_trade_details(self._item(TECHNIQUE))
        text = "\n".join(lines)
        self.assertNotIn("burn_chance", text)
        self.assertNotIn("burn_damage_pct", text)
        self.assertNotIn("burn_heal_reduction", text)
        self.assertIn("灼烧概率+17%", text)
        self.assertIn("灼烧伤害+7%", text)
        self.assertIn("灼烧降疗+18%", text)

    def test_details_have_no_duplicated_lines(self):
        lines = self.shop.get_trade_details(self._item(TECHNIQUE))
        self.assertEqual(len(lines), len(set(lines)), lines)
        self.assertFalse(any(line.startswith("特效:") for line in lines), lines)

    def test_technique_bonus_line_not_named_like_skill_effect(self):
        lines = self.shop.get_trade_details(self._item(TECHNIQUE))
        starts = [line.split(":", 1)[0] for line in lines if ":" in line]
        # 「加成」给被动数值，「效果：」留给绝技，两者不再同名
        self.assertIn("加成", starts)
        self.assertEqual(starts.count("效果"), 0, lines)

    def test_skill_note_skipped_when_description_already_has_it(self):
        data = self.cm.techniques_data[TECHNIQUE]
        lines = self.shop.get_trade_details(self._item(TECHNIQUE))
        self.assertFalse(any(line.startswith("说明：") for line in lines), lines)
        self.assertIn(data["description"], "\n".join(lines))

    def test_skill_note_kept_without_parent_description(self):
        skill = dict(self.cm.techniques_data[TECHNIQUE]["active_skill"])
        lines = _imp("core.technique_skill").skill_summary_lines(skill)
        self.assertTrue(any(line.startswith("说明：") for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
