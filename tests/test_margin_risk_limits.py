# tests/test_margin_risk_limits.py
# 仙市杠杆风控回归：同标的笔数、单玩家持仓笔数、总保证金上限、
# 倍数按标的波动收口、状态页额度展示，以及「拒绝开仓不扣灵石」。
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()

FIXED_SECRET = "unit-test-secret-margin"


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestMarginConfig(unittest.IsolatedAsyncioTestCase):
    """配置解析与分档上限，纯函数，不需要数据库。

    在 Python 3.9 下 ``InvestManager.__init__`` 会创建 ``asyncio.Lock()``，
    必须在事件循环里构造，所以这里用 ``IsolatedAsyncioTestCase``。
    """

    def setUp(self):
        self.InvestManager = _imp("managers.invest_manager").InvestManager
        self.Player = _imp("models").Player

    async def test_default_leverage_tiers(self):
        mgr = self.InvestManager(None, {}, None)
        self.assertEqual(mgr.max_leverage_for(0.045), 5)   # S03
        self.assertEqual(mgr.max_leverage_for(0.055), 5)   # S01
        self.assertEqual(mgr.max_leverage_for(0.060), 3)   # S04
        self.assertEqual(mgr.max_leverage_for(0.075), 3)   # S02 / S06
        self.assertEqual(mgr.max_leverage_for(0.085), 2)   # S05
        self.assertEqual(mgr.max_leverage_for(0), 2)       # 脏数据按最高风险处理

    async def test_leverage_tiers_from_config(self):
        mgr = self.InvestManager(
            None, {"INVEST": {"LEVERAGE_SIGMA_CAPS": "0.08:5"}}, None
        )
        self.assertEqual(mgr.max_leverage_for(0.09), 5)  # 超出最后一档沿用最后一档

    async def test_bad_leverage_tiers_fall_back(self):
        mgr = self.InvestManager(
            None, {"INVEST": {"LEVERAGE_SIGMA_CAPS": "0.06/5"}}, None
        )
        self.assertEqual(mgr.margin_leverage_caps,
                         self.InvestManager.MARGIN_LEVERAGE_SIGMA_CAPS)
        self.assertEqual(mgr.max_leverage_for(0.085), 2)

    async def test_int_config_zero_means_unlimited(self):
        mgr = self.InvestManager(
            None, {"INVEST": {"MAX_POSITIONS": 0, "MAX_POSITIONS_PER_CODE": "abc"}}, None
        )
        self.assertEqual(mgr.margin_max_positions, 0)
        self.assertEqual(mgr.margin_max_positions_per_code, 1)  # 非法值退回默认

    async def test_total_cap_by_level(self):
        mgr = self.InvestManager(None, {}, None)
        cases = {0: 5_000_000, 13: 5_000_000, 16: 10_000_000,
                 22: 40_000_000, 35: 10_000_000_000}
        for level, expect in cases.items():
            player = self.Player(user_id="u1", level_index=level)
            self.assertEqual(mgr.margin_total_cap(player), expect,
                             msg=f"level_index={level} 的总保证金上限不符")

    async def test_total_cap_override_wins(self):
        mgr = self.InvestManager(None, {"INVEST": {"MAX_TOTAL_MARGIN": 123_456}}, None)
        self.assertEqual(mgr.margin_total_cap(self.Player(user_id="u1", level_index=35)), 123_456)


class TestMarginRiskLimits(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.InvestManager = _imp("managers.invest_manager").InvestManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        await self.db.ext.set_system_config("invest_secret", FIXED_SECRET)

        self.manager = self.InvestManager(self.db, {}, None)
        self.manager._secret = FIXED_SECRET
        await self.manager._seed_products()
        await self.manager._settle_tick(10)

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    # ===== 辅助 =====

    async def _player(self, uid: str = "u1", gold: int = 10_000_000):
        await self.db.create_player(
            self.Player(user_id=uid, gold=gold, user_name="测试道友")
        )
        return await self.db.get_player_by_id(uid)

    def _patch_tick(self, tick: int = 10, opened: bool = True):
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(self.InvestManager, "tick_of",
                              classmethod(lambda cls, moment=None: tick))
        )
        stack.enter_context(
            mock.patch.object(self.InvestManager, "is_open",
                              classmethod(lambda cls, moment=None: opened))
        )
        return stack

    async def _open(self, player, code: str, leverage: int, margin: int = 50_000):
        with self._patch_tick():
            return await self.manager.open_margin(player, code, leverage, margin)

    async def _open_count(self, uid: str) -> int:
        return int(await self.manager._scalar(
            "SELECT COUNT(*) FROM invest_margin_positions WHERE user_id = ?", (uid,)
        ))

    # ===== 同标的笔数 =====

    async def test_same_code_rejected_after_first(self):
        player = await self._player()
        ok, msg = await self._open(player, "S01", 3)
        self.assertTrue(ok, msg)
        ok, msg = await self._open(player, "S01", 3)
        self.assertFalse(ok)
        self.assertIn("同一标的", msg)
        self.assertEqual(await self._open_count(player.user_id), 1)

    async def test_same_code_allowed_when_cap_raised(self):
        self.manager.margin_max_positions_per_code = 2
        player = await self._player()
        self.assertTrue((await self._open(player, "S01", 3))[0])
        ok, msg = await self._open(player, "S01", 3)
        self.assertTrue(ok, msg)
        self.assertEqual(await self._open_count(player.user_id), 2)

    # ===== 总持仓笔数 =====

    async def test_max_positions_cap(self):
        player = await self._player()
        for code in ("S01", "S02", "S03"):
            ok, msg = await self._open(player, code, 3)
            self.assertTrue(ok, msg)
        ok, msg = await self._open(player, "S04", 3)
        self.assertFalse(ok)
        self.assertIn("最多同时持有", msg)
        self.assertEqual(await self._open_count(player.user_id), 3)

    # ===== 总保证金上限 =====

    async def test_total_margin_cap(self):
        self.manager.margin_total_cap_override = 100_000
        player = await self._player()
        self.assertTrue((await self._open(player, "S01", 3, 60_000))[0])
        gold_before = (await self.db.get_player_by_id(player.user_id)).gold
        ok, msg = await self._open(player, "S02", 3, 60_000)
        self.assertFalse(ok)
        self.assertIn("总保证金超过上限", msg)
        self.assertIn("还能开 40,000", msg)
        # 拒绝不能扣款、不能落库
        self.assertEqual((await self.db.get_player_by_id(player.user_id)).gold, gold_before)
        self.assertEqual(await self._open_count(player.user_id), 1)

    async def test_total_margin_cap_counts_only_open(self):
        self.manager.margin_total_cap_override = 100_000
        player = await self._player()
        ok, msg = await self._open(player, "S01", 3, 80_000)
        self.assertTrue(ok, msg)
        rows = await self.manager._rows(
            "SELECT id FROM invest_margin_positions WHERE user_id = ?", (player.user_id,)
        )
        with self._patch_tick():
            ok, msg = await self.manager.close_margin(player, int(rows[0]["id"]))
        self.assertTrue(ok, msg)
        # 平仓后额度释放，可以再开
        ok, msg = await self._open(player, "S02", 3, 80_000)
        self.assertTrue(ok, msg)

    # ===== 倍数按波动收口 =====

    async def test_leverage_capped_by_sigma(self):
        player = await self._player()
        ok, msg = await self._open(player, "S04", 5)  # σ=6.0%
        self.assertFalse(ok)
        self.assertIn("最多只能开 3 倍", msg)
        ok, msg = await self._open(player, "S04", 3)
        self.assertTrue(ok, msg)

        ok, msg = await self._open(player, "S05", 3)  # σ=8.5%
        self.assertFalse(ok)
        self.assertIn("最多只能开 2 倍", msg)
        ok, msg = await self._open(player, "S05", 2)
        self.assertTrue(ok, msg)

    async def test_five_times_allowed_on_low_sigma(self):
        player = await self._player()
        ok, msg = await self._open(player, "S01", 5)  # σ=5.5%
        self.assertTrue(ok, msg)

    # ===== 状态页 =====

    async def test_status_shows_exposure(self):
        player = await self._player()
        self.assertTrue((await self._open(player, "S01", 3))[0])
        text = await self.manager.get_margin_status(player)
        self.assertIn("额度 50,000 / 5,000,000", text)
        self.assertIn("持仓 1/3 笔", text)
        self.assertIn("同一标的 1 笔", text)

    async def test_success_message_shows_quota(self):
        player = await self._player()
        ok, msg = await self._open(player, "S01", 3)
        self.assertTrue(ok, msg)
        self.assertIn("额度占用：50,000 / 5,000,000", msg)


if __name__ == "__main__":
    unittest.main()
