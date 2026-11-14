"""配方页产出效果（F9）回归测试。

`/符箓书`、`/符箓配方`、`/丹方` 原来只写成功率、工费、材料——
「能不能做」写清楚了，「做出来干什么用」整块缺席。
这里锁住三件事：

1. 有效果配置的必须显示出来；
2. 数据层没有效果的必须显式写「效果未录入」，不许静默留白；
3. 效果文案里不许出现半角斜杠——它会被前端抽成点不动的假按钮（F8）。
"""
import re
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp

CJK_SLASH = re.compile(r"/[\u4e00-\u9fff]")


class TestTalismanEffectDisplay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)
        self.tm = _imp("managers.talisman_manager").TalismanManager(self.db, self.cm, self.ring)
        self.player = await self._player(gold=10000)

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    async def test_craft_book_shows_effect_line(self):
        msg, card_lines, _, _ = self.tm.build_craft_book(self.player)
        self.assertIn("✨", msg)
        # 烈焰符 effects 里带物理/魔法伤害倍率，描述必然出现「物伤」
        self.assertIn("物伤", msg)
        self.assertTrue(
            any("效果：" in line for line in card_lines),
            "卡片里必须有效果行",
        )

    async def test_effect_line_has_no_fake_command_slash(self):
        """效果文案不能带半角斜杠，否则网页上会多出点不动的按钮。"""
        msg, _, _, _ = self.tm.build_craft_book(self.player)
        effect_lines = [ln for ln in msg.split("\n") if "✨" in ln or "⚠️" in ln]
        self.assertTrue(effect_lines, msg)
        for line in effect_lines:
            self.assertIsNone(CJK_SLASH.search(line), f"效果行含假指令：{line}")

    async def test_recipe_page_puts_effect_under_each_recipe(self):
        text = self.tm.format_recipe_text()
        self.assertIn("✨", text)
        self.assertIn("物伤", text)
        # 每张符一行配方 + 一行效果：效果行数量应约等于配方数
        entries = self.tm.iter_recipes()
        effect_count = len([ln for ln in text.split("\n") if "✨" in ln or "⚠️" in ln])
        self.assertEqual(effect_count, len(entries))

    async def test_run_scoped_shows_times_not_duration(self):
        """探索类按次数生效，显示「持续 X 分钟」会骗人。"""
        line = self.tm.effect_summary("遁地符")
        self.assertIn("次", line)
        self.assertNotIn("持续", line)

    async def test_instant_effect_has_no_duration(self):
        line = self.tm.effect_summary("回春符")
        self.assertIn("立即", line)
        self.assertNotIn("持续", line)


class TestPillRecipeEffectDisplay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)
        self.am = _imp("managers.alchemy_manager").AlchemyManager(self.db, self.cm, self.ring)
        self.player = await self._player(gold=10000)

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    async def test_placeholder_desc_is_regenerated(self):
        """692 条 desc 原本全是「记载XX炼制方法的丹方。」，必须被真实效果替换。"""
        for recipe_id in (130, 131):
            recipe = self.am.recipes[recipe_id]
            self.assertNotIn("炼制方法的丹方", recipe["desc"])
            self.assertNotEqual(recipe["desc"], self.am.DESC_MISSING)

    async def test_effect_has_quality_range(self):
        """数值型效果要写区间，否则「猛火博品质」这个选择在界面上不存在。"""
        desc = self.am.recipes[130]["desc"]
        self.assertRegex(desc, r"\d+%~\d+%")

    async def test_unknown_effect_is_explicit_not_blank(self):
        """443 条丹药在 items.json 里只有「这是一件X品丹药」，必须明说未录入。"""
        self.player.learned_recipes = "[500]"
        await self.db.update_player(self.player)
        ok, msg, _, card_lines, _entries = await self.am.get_available_recipes(self.player.user_id)
        self.assertTrue(ok, msg)
        self.assertIn("效果未录入", msg)
        self.assertIn("⚠️", msg)
        self.assertNotIn("这是一件", msg)
        self.assertTrue(any("效果未录入" in line for line in card_lines))

    async def test_learned_recipe_with_effect_is_shown(self):
        self.player.learned_recipes = "[130]"
        await self.db.update_player(self.player)
        ok, msg, _, _, _entries = await self.am.get_available_recipes(self.player.user_id)
        self.assertTrue(ok, msg)
        self.assertIn("最大气血", msg)


if __name__ == "__main__":
    unittest.main()
