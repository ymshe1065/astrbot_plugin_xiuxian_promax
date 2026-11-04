# tests/test_my_status_command.py
# /我的状态：列出所有生效中的限时状态（状态名 / 效果 / 剩余时间或次数）。
import time
import unittest

from test_equipment_instances import _FakeEvent, _imp


def _pill_handler(db, cm):
    return _imp("handlers.pill_handler").PillHandler(db, cm)


class TestMyStatusRender(unittest.IsolatedAsyncioTestCase):
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

    def _set_effects(self, effects):
        self.player.set_active_pill_effects(effects)

    async def _render(self):
        return await self.handler._render_my_status(self.player)

    async def test_empty_state(self):
        self._set_effects([])
        text = await self._render()
        self.assertIn("我的状态", text)
        self.assertIn("没有生效中的", text)

    async def test_shows_name_effect_and_remaining(self):
        now = int(time.time())
        self._set_effects([
            {
                "pill_name": "鸿蒙符", "talisman_name": "鸿蒙符", "source": "talisman",
                "category": "增益", "quality": "极品",
                "start_time": now, "expiry_time": now + 120 * 60,
                "duration_minutes": 120, "last_tick_time": now,
                "cultivation_multiplier": 0.22, "luck_bonus": 3,
                "lifespan_regen_per_minute": 2,
            }
        ])
        text = await self._render()
        self.assertIn("鸿蒙符·极品", text)      # 状态名带品质
        self.assertIn("修炼速度+22%", text)     # 状态效果
        self.assertIn("气运+3", text)
        self.assertIn("2小时", text)            # 剩余时间

    async def test_shows_remaining_uses_for_run_scoped(self):
        now = int(time.time())
        self._set_effects([
            {
                "pill_name": "遁地符", "talisman_name": "遁地符", "source": "talisman",
                "category": "探索", "start_time": now, "expiry_time": 0,
                "duration_minutes": 0, "remaining_uses": 4, "last_tick_time": now,
                "exploration_reward_multiplier": 0.25, "rift_reward_multiplier": 0.25,
            }
        ])
        text = await self._render()
        self.assertIn("剩余 4 次", text)
        self.assertNotIn("已到期", text)

    async def test_expired_effect_is_filtered(self):
        now = int(time.time())
        self._set_effects([
            {
                "pill_name": "修炼加速丹", "quality": "一般",
                "start_time": now - 7200, "expiry_time": now - 60,
                "duration_minutes": 60, "last_tick_time": now,
                "cultivation_multiplier": 0.25,
            },
            {
                "pill_name": "鸿蒙符", "talisman_name": "鸿蒙符", "source": "talisman",
                "quality": "一般", "start_time": now, "expiry_time": now + 3600,
                "duration_minutes": 60, "last_tick_time": now,
                "cultivation_multiplier": 0.22,
            },
        ])
        text = await self._render()
        self.assertNotIn("修炼加速丹", text)
        self.assertIn("鸿蒙符", text)
        self.assertIn("共 1 个状态生效中", text)

    async def test_dirty_effect_does_not_crash(self):
        now = int(time.time())
        self._set_effects([
            "脏数据",
            None,
            {
                "pill_name": "鸿蒙符", "talisman_name": "鸿蒙符", "source": "talisman",
                "quality": "一般", "start_time": now, "expiry_time": now + 600,
                "duration_minutes": 10, "last_tick_time": now,
                "cultivation_multiplier": 0.22,
            },
        ])
        text = await self._render()
        self.assertIn("鸿蒙符", text)

    async def test_cultivation_summary(self):
        now = int(time.time())
        self._set_effects([
            {
                "pill_name": "玄元加速丹", "quality": "一般",
                "start_time": now, "expiry_time": now + 3600,
                "duration_minutes": 60, "last_tick_time": now,
                "cultivation_multiplier": 0.75,
            }
        ])
        text = await self._render()
        self.assertIn("修炼速度 ×1.75", text)
        self.assertIn("上限", text)

    async def test_resurrection_pill_listed(self):
        self._set_effects([])
        self.player.has_resurrection_pill = True
        self.player.resurrection_pill_quality = "上品"
        text = await self._render()
        self.assertIn("回生丹（上品）", text)

    async def test_does_not_claim_cap(self):
        """产品明确要求不加封顶，文案里不能出现「封顶/上限 24 小时」之类。"""
        now = int(time.time())
        self._set_effects([
            {
                "pill_name": "鸿蒙符", "talisman_name": "鸿蒙符", "source": "talisman",
                "quality": "一般", "start_time": now,
                "expiry_time": now + 120 * 20 * 60,
                "duration_minutes": 120 * 20, "last_tick_time": now,
                "cultivation_multiplier": 0.22,
            }
        ])
        text = await self._render()
        # 2400 分钟 = 1 天 16 小时，如实展示，不压缩
        self.assertIn("1天16小时", text)
        self.assertNotIn("封顶", text)


if __name__ == "__main__":
    unittest.main()
