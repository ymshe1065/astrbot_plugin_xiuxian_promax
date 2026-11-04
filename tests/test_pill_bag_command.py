# tests/test_pill_bag_command.py
# /丹药背包 回归：这条指令以前没有测试覆盖，读取失败会静默冒泡，玩家侧表现成「发指令没反应」。
import asyncio
import json
import time
import unittest

from test_equipment_instances import _imp, _FakeEvent


def _pill_handler(db, cm):
    return _imp("handlers.pill_handler").PillHandler(db, cm)


class TestPillBagAsync(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_equipment_instances import TestEquipmentInstances

        self._case = TestEquipmentInstances(methodName="runTest")
        await self._case.asyncSetUp()
        self.db = self._case.db
        self.cm = self._case.cm
        self.handler = _pill_handler(self.db, self.cm)
        self.player = await self._case._player(gold=1000)

    async def asyncTearDown(self):
        await self._case.asyncTearDown()

    async def _render(self, player):
        results = []
        async for r in self.handler.handle_show_pills(_FakeEvent(player.user_id, "测试", "丹药背包")):
            results.append(r)
        return results

    async def test_empty_bag(self):
        out = await self._render(self.player)
        self.assertEqual(len(out), 1)
        self.assertIn("丹药背包是空的", out[0])

    async def test_groups_by_quality(self):
        inv = self.player.get_pills_inventory()
        inv["炼气丹|一般"] = 3
        inv["炼气丹|良好"] = 1
        self.player.set_pills_inventory(inv)
        await self.db.update_player(self.player)
        player = await self.db.get_player_by_id(self.player.user_id)

        out = await self._render(player)
        text = out[0]
        self.assertIn("炼气丹", text)
        self.assertIn("一般×3", text)
        self.assertIn("良好×1", text)
        self.assertIn("丹药背包", text)

    async def test_unknown_quality_still_visible(self):
        """品质名不在标准序列里时也不能从背包列表消失。"""
        inv = self.player.get_pills_inventory()
        inv["固基丹|下品"] = 2
        self.player.set_pills_inventory(inv)
        await self.db.update_player(self.player)
        player = await self.db.get_player_by_id(self.player.user_id)

        text = (await self._render(player))[0]
        self.assertIn("固基丹", text)
        self.assertIn("×2", text)

    async def test_non_dict_effect_does_not_crash(self):
        player = self.player
        player.active_pill_effects = json.dumps(["脏数据", {"pill_name": "聚气丹", "remaining_uses": 2}])
        await self.db.update_player(player)
        player = await self.db.get_player_by_id(player.user_id)

        text = (await self._render(player))[0]
        self.assertIn("聚气丹", text)
        self.assertIn("剩余 2 次", text)

    async def test_render_failure_falls_back_to_message(self):
        """渲染异常时必须给出可读提示，而不是整条指令静默失败。"""
        async def _boom(_player):
            raise RuntimeError("模拟读取失败")

        self.handler._render_pill_bag = _boom
        out = await self._render(self.player)
        self.assertEqual(len(out), 1)
        self.assertIn("丹药背包读取失败", out[0])

    async def test_model_filters_non_dict_effects(self):
        """模型层读取口必须过滤非 dict 脏条目，保护所有调用方。"""
        self.player.active_pill_effects = json.dumps(["脏数据", {"pill_name": "聚气丹"}, 123])
        self.assertEqual(self.player.get_active_pill_effects(), [{"pill_name": "聚气丹"}])

    async def test_string_expiry_time_renders(self):
        """脏数据：expiry_time 是数字字符串时也应正确归一并正常展示。"""
        now = int(time.time())
        self.player.active_pill_effects = json.dumps([
            {"pill_name": "聚气丹", "expiry_time": str(now + 3600)}
        ])
        await self.db.update_player(self.player)
        player = await self.db.get_player_by_id(self.player.user_id)

        text = (await self._render(player))[0]
        self.assertNotIn("读取失败", text)
        self.assertIn("聚气丹", text)
        self.assertIn("剩余", text)

    async def test_unparseable_expiry_does_not_break_bag(self):
        """脏数据：expiry_time 无法解析时丢弃展示该条，其余效果与背包照常渲染。"""
        now = int(time.time())
        self.player.active_pill_effects = json.dumps([
            {"pill_name": "坏数据丹", "expiry_time": "abc"},
            {"pill_name": "聚气丹", "expiry_time": now + 3600},
        ])
        await self.db.update_player(self.player)
        player = await self.db.get_player_by_id(self.player.user_id)

        text = (await self._render(player))[0]
        self.assertNotIn("读取失败", text)
        self.assertIn("聚气丹", text)

    async def test_string_periodic_values_still_settle(self):
        """脏数据：每分钟恢复值是数字字符串时，周期结算照常生效且不崩。"""
        now = int(time.time())
        self.player.spiritual_qi = 0
        self.player.max_spiritual_qi = 1000
        self.player.active_pill_effects = json.dumps([
            {"pill_name": "回春丹", "expiry_time": str(now + 3600),
             "last_tick_time": str(now - 600), "spiritual_qi_regen_per_minute": "5"}
        ])
        await self.db.update_player(self.player)
        player = await self.db.get_player_by_id(self.player.user_id)

        text = (await self._render(player))[0]
        self.assertNotIn("读取失败", text)
        # player_required 内部会重新读档，结算结果需从库里验证
        settled = await self.db.get_player_by_id(self.player.user_id)
        self.assertGreater(settled.spiritual_qi, 0)


if __name__ == "__main__":
    unittest.main()
