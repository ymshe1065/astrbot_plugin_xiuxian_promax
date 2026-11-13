"""符箓书（/符箓书·/可制符箓）回归测试。"""
import unittest
from unittest.mock import patch

import test_equipment_instances as fixtures
from test_equipment_instances import _imp


class TestTalismanCraftBook(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)
        self.tm_mod = _imp("managers.talisman_manager")
        self.tm = self.tm_mod.TalismanManager(self.db, self.cm, self.ring)
        self.player = await self._player(gold=10000)
        self.player.life_skill = "符箓师"

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    def _materials(self, category, tier, count=1):
        picks = [
            name for name, info in self.cm.material_catalog.items()
            if info.get("category") == category and info.get("tier") == tier
        ]
        self.assertTrue(picks, f"缺少 {tier}{category} 材料")
        return picks[0], count

    async def test_book_lists_learned_recipes_with_numbers(self):
        msg, card_lines, craftable, _ = self.tm.build_craft_book(self.player)
        self.assertIn("符箓书", msg)
        self.assertIn("成功率", msg)
        self.assertIn("工费", msg)
        self.assertTrue(card_lines, "卡片行不应为空")
        # 没材料时画不了，且要点明卡点
        self.assertEqual(craftable, 0, msg)
        self.assertIn("材料不足", msg)

    async def test_non_talisman_master_sees_no_penalty_note(self):
        """非符箓师只是少一层职业加成，符箓书要明确说「制符不受影响」。"""
        self.player.life_skill = "炼丹师"
        msg, _, _, _ = self.tm.build_craft_book(self.player)
        self.assertIn("制符不受影响", msg)
        self.assertNotIn("暂时无法绘制", msg)

    async def test_non_master_can_craft(self):
        """制符不设职业门槛：炼丹师也能画，只少了符箓师加成。"""
        ore, _ = self._materials("矿材", "凡品", 1)
        herb, _ = self._materials("灵植", "凡品", 1)
        await self.ring.store_item(self.player, ore, 1)
        await self.ring.store_item(self.player, herb, 1)
        self.player = await self.db.get_player_by_id(self.player.user_id)
        self.player.life_skill = "炼丹师"

        with patch.object(self.tm_mod.random, "randint", return_value=1):
            ok, msg = await self.tm.craft_talisman(self.player, "烈焰符", 1)
        self.assertTrue(ok, msg)
        self.assertIn("烈焰符", msg)
        self.assertNotIn("只有【符箓师】", msg)

    async def test_master_keeps_price_bonus(self):
        """符箓师加成保留：同一张符的工费比非符箓师低 20%。"""
        self.player.life_skill = "炼丹师"
        normal = self.tm.resolve_craft_params(self.player, "烈焰符")["gold_cost"]
        self.player.life_skill = "符箓师"
        master = self.tm.resolve_craft_params(self.player, "烈焰符")["gold_cost"]
        self.assertLess(master, normal)
        self.assertEqual(normal - int(round(normal * 0.8)), normal - master)

    async def test_materials_unlock_craftable_count(self):
        ore, n1 = self._materials("矿材", "凡品", 3)
        herb, n2 = self._materials("灵植", "凡品", 3)
        ok, m = await self.ring.store_item(self.player, ore, n1)
        self.assertTrue(ok, m)
        ok, m = await self.ring.store_item(self.player, herb, n2)
        self.assertTrue(ok, m)
        self.player = await self.db.get_player_by_id(self.player.user_id)
        self.player.life_skill = "符箓师"
        msg, _, craftable, _ = self.tm.build_craft_book(self.player)
        self.assertGreaterEqual(craftable, 1, msg)
        self.assertIn("可画", msg)

    async def test_book_gold_matches_actual_craft_cost(self):
        """符箓书显示的工费必须等于 /制符 实际扣款。"""
        ore, _ = self._materials("矿材", "凡品", 1)
        herb, _ = self._materials("灵植", "凡品", 1)
        await self.ring.store_item(self.player, ore, 1)
        await self.ring.store_item(self.player, herb, 1)
        self.player = await self.db.get_player_by_id(self.player.user_id)
        self.player.life_skill = "符箓师"

        params = self.tm.resolve_craft_params(self.player, "烈焰符")
        self.assertIsNotNone(params)
        before = self.player.gold
        with patch.object(self.tm_mod.random, "randint", return_value=1):
            ok, msg = await self.tm.craft_talisman(self.player, "烈焰符", 1)
        self.assertTrue(ok, msg)
        self.assertEqual(before - self.player.gold, params["gold_cost"], msg)

    async def test_locked_recipes_are_listed_separately(self):
        msg, _, _, _ = self.tm.build_craft_book(self.player)
        self.assertIn("符图未习得", msg)

    async def test_level_gate_marks_unavailable(self):
        """境界不足的符要标出需求等级，且不计入可画。"""
        self.player.level_index = 0
        msg, _, craftable, _ = self.tm.build_craft_book(self.player)
        self.assertIn("需境界等级", msg)
        self.assertEqual(craftable, 0, msg)


if __name__ == "__main__":
    unittest.main()
