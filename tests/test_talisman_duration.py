# tests/test_talisman_duration.py
# 符箓时长改造：由「全品类一律 15 分钟」改为「用途分族 × 品阶分档」，
# 参考丹药的时长口径（修炼增益 60~720、战斗爆发 15~60）。
import time
import unittest
from unittest.mock import patch

from test_talisman import ROOT, _imp

RANK_ORDER = ["凡品", "黄品", "玄品", "圣品"]

# 目标表：与 core/talisman_rules.DURATION_BY_CATEGORY 同源，改表必须同步这里
EXPECTED = {
    "增益": [60, 120, 180, 360],
    "炼丹": [60, 90, 120, 180],
    "防御": [20, 30, 60, 90],
    "遁术": [20, 30, 45, 60],
    "攻击": [15, 20, 30, 45],
    "控制": [15, 20, 30, 45],
    "回复": [15, 20, 30, 45],
}


class TestDurationTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = _imp("core.talisman_rules")

    def test_every_category_has_four_ranks(self):
        table = self.rules.DURATION_BY_CATEGORY
        for category in EXPECTED:
            self.assertIn(category, table, f"{category} 缺少时长表")
            self.assertEqual(sorted(table[category]), sorted(RANK_ORDER), f"{category} 品阶档位不全")

    def test_ladder_matches_expectation(self):
        for category, expected in EXPECTED.items():
            for rank, minutes in zip(RANK_ORDER, expected):
                self.assertEqual(
                    self.rules.duration_of(category, rank), minutes,
                    f"{category}·{rank} 时长不符",
                )

    def test_ladder_is_monotonic(self):
        """品阶越高时长不能变短，否则升阶反而吃亏。"""
        for category, expected in EXPECTED.items():
            for lower, higher in zip(expected, expected[1:]):
                self.assertLessEqual(lower, higher, f"{category} 时长阶梯不单调")

    def test_fan_and_huang_are_no_longer_identical(self):
        """回归：改造前凡品与黄品都是 15 分钟，升品阶毫无收益。"""
        for category in EXPECTED:
            fan = self.rules.duration_of(category, "凡品")
            huang = self.rules.duration_of(category, "黄品")
            self.assertGreater(huang, fan, f"{category} 黄品没有比凡品长")

    def test_gain_category_reaches_dan_yao_scale(self):
        """增益符要对齐丹药的分钟级，而不是 15 分钟。"""
        self.assertEqual(self.rules.duration_of("增益", "圣品"), 360)
        self.assertGreaterEqual(self.rules.duration_of("增益", "玄品"), 180)

    def test_unknown_category_falls_back_to_rank_table(self):
        self.assertEqual(
            self.rules.duration_of("未知品类", "圣品"),
            self.rules.DURATION_BY_RANK["圣品"],
        )

    def test_exploration_stays_short_and_run_scoped(self):
        """探索符按次数结算，时长只用于展示，不该跟着拉长。"""
        self.assertEqual(self.rules.duration_of("探索", "圣品"), 30)


class TestDurationApplied(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import os
        import tempfile

        self.data = _imp("data")
        self.Player = _imp("models").Player
        self.TalismanManager = _imp("managers.talisman_manager").TalismanManager
        StorageRingManager = _imp("core").StorageRingManager
        cm = _imp("config_manager").ConfigManager(ROOT)

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = self.data.DataBase(self.db_path)
        await self.db.connect()
        await self.data.MigrationManager(self.db.conn, cm).migrate()
        self.storage = StorageRingManager(self.db, cm)
        self.talisman = self.TalismanManager(self.db, cm, self.storage)

    async def asyncTearDown(self):
        import os

        await self.db.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    async def _player(self, items=None):
        player = self.Player(
            user_id="dur_user", user_name="dur", level_index=20, gold=10 ** 8
        )
        await self.db.create_player(player)
        if items:
            player.set_storage_ring_items(items)
            await self.db.update_player(player)
        return await self.db.get_player_by_id("dur_user")

    def _effect(self, player, name):
        return next(
            (e for e in player.get_active_pill_effects() if e.get("talisman_name") == name),
            None,
        )

    async def test_profile_uses_category_table(self):
        hongmeng = self.talisman.get_talisman_profile("鸿蒙符")
        self.assertEqual(hongmeng["category"], "增益")
        self.assertEqual(hongmeng["duration"], 120)
        liehuo = self.talisman.get_talisman_profile("烈焰符")
        self.assertEqual(liehuo["duration"], 15)

    async def test_second_use_extends_instead_of_overwriting(self):
        player = await self._player({"鸿蒙符": 10})
        with patch.object(self.talisman, "_roll_quality", return_value=("一般", 1.0, 0.0, 0)):
            ok, msg = await self.talisman.use_talisman(player, "鸿蒙符", 1)
        self.assertTrue(ok, msg)
        first = int(self._effect(player, "鸿蒙符")["expiry_time"])

        with patch.object(self.talisman, "_roll_quality", return_value=("一般", 1.0, 0.0, 0)):
            ok, msg = await self.talisman.use_talisman(player, "鸿蒙符", 1)
        self.assertTrue(ok, msg)
        second = int(self._effect(player, "鸿蒙符")["expiry_time"])
        self.assertIn("续时", msg)
        # 第二次应当在第一次的基础上顺延一个完整时长（120 分钟）
        self.assertAlmostEqual(second - first, 120 * 60, delta=5)

    async def test_quality_adds_time_by_ratio(self):
        player = await self._player({"鸿蒙符": 10})
        with patch.object(self.talisman, "_roll_quality", return_value=("极品", 1.4, 0.15, 2)):
            ok, msg = await self.talisman.use_talisman(player, "鸿蒙符", 1)
        self.assertTrue(ok, msg)
        effect = self._effect(player, "鸿蒙符")
        # 120 + round(120 * 0.15) = 138
        self.assertEqual(effect["duration_minutes"], 138)

    async def test_short_talisman_gets_minimum_bonus(self):
        player = await self._player({"烈焰符": 10})
        with patch.object(self.talisman, "_roll_quality", return_value=("极品", 1.4, 0.15, 2)):
            ok, msg = await self.talisman.use_talisman(player, "烈焰符", 1)
        self.assertTrue(ok, msg)
        effect = self._effect(player, "烈焰符")
        # 15 * 0.15 只有 2 分钟，走 5 分钟保底
        self.assertEqual(effect["duration_minutes"], 20)

    async def test_no_duration_cap(self):
        """产品要求不加封顶：20 张就是 20 倍时长。"""
        player = await self._player({"鸿蒙符": 20})
        with patch.object(self.talisman, "_roll_quality", return_value=("一般", 1.0, 0.0, 0)):
            ok, msg = await self.talisman.use_talisman(player, "鸿蒙符", 20)
        self.assertTrue(ok, msg)
        effect = self._effect(player, "鸿蒙符")
        self.assertEqual(effect["duration_minutes"], 120 * 20)

    async def test_exploration_still_counts_uses(self):
        player = await self._player({"洞天符": 5})
        with patch.object(self.talisman, "_roll_quality", return_value=("一般", 1.0, 0.0, 0)):
            ok, msg = await self.talisman.use_talisman(player, "洞天符", 1)
        self.assertTrue(ok, msg)
        effect = self._effect(player, "洞天符")
        self.assertEqual(effect["remaining_uses"], 6)
        self.assertEqual(effect["expiry_time"], 0)

    async def test_quality_adds_uses_for_exploration(self):
        player = await self._player({"遁地符": 5})
        with patch.object(self.talisman, "_roll_quality", return_value=("极品", 1.4, 0.15, 2)):
            ok, msg = await self.talisman.use_talisman(player, "遁地符", 1)
        self.assertTrue(ok, msg)
        # 基础 3 次 + 极品 2 次
        self.assertEqual(self._effect(player, "遁地符")["remaining_uses"], 5)


if __name__ == "__main__":
    unittest.main()
