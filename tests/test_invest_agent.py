# tests/test_invest_agent.py
# 仙市投资代理人回归：费用、委托约束、结算、止损止盈、领取幂等、迁移
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

FIXED_SECRET = "unit-test-secret-invest-agent"


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestInvestAgentMath(unittest.TestCase):
    """组合计算纯函数，不依赖数据库。"""

    def setUp(self):
        self.mod = _imp("managers.invest_agent_manager")
        self.mgr = self.mod.InvestAgentManager.__new__(self.mod.InvestAgentManager)
        self.agent = self.mod.AGENT_DEFS["xuanheng"]

    def test_liquidate_profit_charges_profit_share(self):
        positions = {"S01": {"kind": "stock", "shares": 1000, "cost": 20_000}}
        cash, final_value, profit_fee, penalty = self.mgr._liquidate(
            80_000, positions, {"S01": 30.0}, 100_000, self.agent, "matured"
        )
        # 1000 股 × 30 × (1-滑点) ≈ 29,955；总市值约 109,955
        self.assertGreater(final_value, 100_000)
        self.assertAlmostEqual(final_value, int(round(80_000 + 1000 * 30.0 * (1 - 0.0015))), delta=1)
        self.assertEqual(profit_fee, int(round((final_value - 100_000) * self.agent["profit_share"])))
        self.assertEqual(penalty, 0)
        self.assertEqual(cash, final_value)

    def test_liquidate_loss_charges_nothing_extra(self):
        positions = {"F01": {"kind": "fund", "shares": 5000.0, "cost": 60_000}}
        cash, final_value, profit_fee, penalty = self.mgr._liquidate(
            40_000, positions, {"F01": 0.9}, 100_000, self.agent, "matured"
        )
        self.assertEqual(profit_fee, 0)
        self.assertEqual(penalty, 0)
        self.assertLess(final_value, 100_000)
        self.assertEqual(cash, final_value)

    def test_early_stop_penalty_is_one_percent(self):
        _, _, _, penalty = self.mgr._liquidate(100_000, {}, {}, 100_000, self.agent, "stopped")
        self.assertEqual(penalty, 1_000)
        _, _, _, matured_penalty = self.mgr._liquidate(100_000, {}, {}, 100_000, self.agent, "matured")
        self.assertEqual(matured_penalty, 0)

    # ===== 等级 =====

    def test_level_thresholds(self):
        self.assertEqual(self.mod.level_for_exp(0), 1)
        self.assertEqual(self.mod.level_for_exp(79), 1)
        self.assertEqual(self.mod.level_for_exp(80), 2)
        self.assertEqual(self.mod.level_for_exp(239), 2)
        self.assertEqual(self.mod.level_for_exp(240), 3)
        self.assertEqual(self.mod.level_for_exp(600), 4)
        self.assertEqual(self.mod.level_for_exp(1200), 5)
        self.assertEqual(self.mod.level_for_exp(999_999), 5)

    def test_level_benefits(self):
        qinghe = self.mod.AGENT_DEFS["qinghe"]
        self.assertAlmostEqual(self.mod.effective_fee(qinghe, 1), 0.008)
        self.assertAlmostEqual(self.mod.effective_fee(qinghe, 5), 0.008 * 0.8)
        self.assertAlmostEqual(self.mod.effective_share(qinghe, 1), 0.08)
        self.assertAlmostEqual(self.mod.effective_share(qinghe, 5), 0.04)
        self.assertEqual(self.mod.principal_cap(qinghe, 1), 200_000)
        self.assertEqual(self.mod.principal_cap(qinghe, 5), 1_000_000)

    def test_exp_delta_profit_loss_and_progress(self):
        # 16 盘、+10%：16 × (1 + 1.0) = 32
        self.assertEqual(self.mod.exp_delta(100_000, 110_000, 100, 116, 116), 32)
        # 16 盘、-20%：16 × (1 - 2.0) = -16
        self.assertEqual(self.mod.exp_delta(100_000, 80_000, 100, 116, 116), -16)
        # 收益率夹在 ±30%：-50% 仍按 -30% 计
        self.assertEqual(self.mod.exp_delta(100_000, 50_000, 100, 116, 116), -32)
        # 提前停止按进度折算：32 盘只跑了 1 盘，收益 -1%
        early = self.mod.exp_delta(100_000, 99_000, 100, 132, 100)
        self.assertGreaterEqual(early, -1)
        self.assertLessEqual(early, 1)


class TestInvestAgentFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        self.migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.InvestManager = _imp("managers.invest_manager").InvestManager
        self.InvestAgentManager = _imp("managers.invest_agent_manager").InvestAgentManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.migration._create_all_tables_v2(self.conn)
        await self.migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        await self.db.ext.set_system_config("invest_secret", FIXED_SECRET)

        self.invest = self.InvestManager(self.db, {}, None)
        self.invest._secret = FIXED_SECRET
        await self.invest._seed_products()
        # 结算推进交给行情管理器测；这里只测代理人自身，避免生成整段行情
        self.invest.settle_to = mock.AsyncMock(return_value=0)
        self.mgr = self.InvestAgentManager(self.db, self.invest)
        self.tick = 100

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    def _at(self, tick: int):
        return mock.patch.object(
            self.InvestManager, "tick_of", classmethod(lambda cls, moment=None: tick)
        )

    async def _player(self, uid: str = "u1", gold: int = 1_000_000):
        await self.db.create_player(self.Player(user_id=uid, gold=gold, user_name="测试道友"))
        return await self.db.get_player_by_id(uid)

    async def _mandate(self, uid: str = "u1"):
        rows = await self.mgr._rows(
            "SELECT * FROM invest_agent_mandates WHERE user_id = ? ORDER BY id DESC", (uid,)
        )
        return rows[0] if rows else None

    async def _set_prices(self, tick: int, factor: float):
        await self.conn.execute(
            "INSERT OR REPLACE INTO invest_prices (code, tick, close, change_rate, msg_tag) "
            "SELECT code, ?, price * ?, ? - 1, '' FROM invest_products",
            (tick, factor, factor),
        )
        await self.conn.commit()

    # ===== 委托约束 =====

    async def test_start_charges_management_fee(self):
        player = await self._player(gold=1_000_000)
        with self._at(100):
            ok, msg = await self.mgr.start(player, "青禾", 100_000, 8)
        self.assertTrue(ok, msg)
        fresh = await self.db.get_player_by_id("u1")
        self.assertEqual(fresh.gold, 900_000)

        row = await self._mandate()
        self.assertEqual(row["agent_key"], "qinghe")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["management_fee"], 800)
        self.assertEqual(row["cash"], 99_200)
        self.assertEqual(row["start_tick"], 101)
        self.assertEqual(row["end_tick"], 109)
        self.assertEqual(row["last_settle_tick"], 100)

    async def test_only_one_active_mandate(self):
        player = await self._player()
        with self._at(100):
            ok, _ = await self.mgr.start(player, "玄衡", 100_000, 8)
            self.assertTrue(ok)
            ok2, msg2 = await self.mgr.start(player, "赤霄", 100_000, 8)
        self.assertFalse(ok2)
        self.assertIn("进行中", msg2)
        fresh = await self.db.get_player_by_id("u1")
        self.assertEqual(fresh.gold, 900_000, "第二次委托必须不扣灵石")

    async def test_start_rejects_bad_input(self):
        player = await self._player()
        cases = [
            ("不存在", 100_000, 8),
            ("青禾", 9_999, 8),
            ("青禾", 1_000_001, 8),
            ("青禾", 100_000, 9),
        ]
        for agent, principal, duration in cases:
            with self._at(100):
                ok, _ = await self.mgr.start(player, agent, principal, duration)
            self.assertFalse(ok, f"{agent}/{principal}/{duration} 不应通过")
        self.assertIsNone(await self._mandate())

    async def test_start_rejects_insufficient_gold(self):
        player = await self._player(gold=50_000)
        with self._at(100):
            ok, msg = await self.mgr.start(player, "青禾", 100_000, 8)
        self.assertFalse(ok)
        self.assertIn("只有", msg)

    async def test_start_rejects_loan_above_threshold(self):
        player = await self._player()
        await self.conn.execute(
            "INSERT INTO bank_loans "
            "(user_id, principal, borrowed_at, due_at, status, paid_principal) "
            "VALUES ('u1', 200000, 0, 0, 'active', 0)"
        )
        await self.conn.commit()
        with self._at(100):
            ok, msg = await self.mgr.start(player, "青禾", 100_000, 8)
        self.assertFalse(ok)
        self.assertIn("贷款", msg)

    # ===== 等级 =====

    async def _set_level(self, agent_key: str, exp: int, level: int, uid: str = "u1"):
        await self.conn.execute(
            "INSERT OR REPLACE INTO invest_agent_levels "
            "(user_id, agent_key, exp, level, updated_at) VALUES (?, ?, ?, ?, 0)",
            (uid, agent_key, exp, level),
        )
        await self.conn.commit()

    async def test_level_cap_limits_principal(self):
        await self._player()
        with self._at(100):
            ok, msg = await self.mgr.start(await self.db.get_player_by_id("u1"), "青禾", 250_000, 8)
        self.assertFalse(ok)
        self.assertIn("上限", msg)

        await self._set_level("qinghe", 1200, 5)
        with self._at(100):
            ok, msg = await self.mgr.start(await self.db.get_player_by_id("u1"), "青禾", 250_000, 8)
        self.assertTrue(ok, msg)

    async def test_level_discounts_management_fee(self):
        await self._player()
        await self._set_level("qinghe", 240, 3)
        with self._at(100):
            ok, msg = await self.mgr.start(await self.db.get_player_by_id("u1"), "青禾", 100_000, 8)
        self.assertTrue(ok, msg)
        row = await self._mandate()
        # Lv.3：管理费 0.8% × 90% = 0.72%
        self.assertEqual(row["management_fee"], 720)
        self.assertEqual(row["cash"], 99_280)

    async def test_maturity_awards_exp(self):
        await self._player()
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "青禾", 100_000, 8)
        with self._at(109):
            await self.mgr.settle_due()
        row = await self._mandate()
        delta = int(row["exp_delta"])
        self.assertGreater(delta, 0)
        lvl = await self.mgr._row(
            "SELECT * FROM invest_agent_levels WHERE user_id = 'u1' AND agent_key = 'qinghe'"
        )
        self.assertIsNotNone(lvl)
        self.assertEqual(int(lvl["exp"]), delta)
        self.assertEqual(int(lvl["level"]), 1)

    async def test_loss_deducts_exp_but_level_does_not_drop(self):
        await self._player()
        await self._set_level("chixiao", 300, 3)
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "赤霄", 100_000, 8)
        with self._at(101):
            await self.mgr.settle_due()
        await self._set_prices(102, 0.5)
        with self._at(102):
            await self.mgr.settle_due()
        row = await self._mandate()
        self.assertEqual(row["close_reason"], "stop_loss")
        self.assertLess(int(row["exp_delta"]), 0)
        lvl = await self.mgr._row(
            "SELECT * FROM invest_agent_levels WHERE user_id = 'u1' AND agent_key = 'chixiao'"
        )
        self.assertEqual(int(lvl["level"]), 3)
        self.assertGreaterEqual(int(lvl["exp"]), 240)
        self.assertLess(int(lvl["exp"]), 300)

    async def test_early_stop_exp_is_proportional(self):
        await self._player()
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "赤霄", 100_000, 32)
        with self._at(101):
            await self.mgr.settle_due()
        with self._at(102):
            ok, msg = await self.mgr.stop(await self.db.get_player_by_id("u1"))
        self.assertTrue(ok, msg)
        row = await self._mandate()
        self.assertLessEqual(abs(int(row["exp_delta"])), 2)

    # ===== 结算 =====

    async def test_settle_before_start_does_nothing(self):
        await self._player()
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "青禾", 100_000, 8)
            handled = await self.mgr.settle_due()
        self.assertEqual(handled, 0)
        row = await self._mandate()
        self.assertEqual(row["last_settle_tick"], 100)
        self.assertEqual(row["positions_json"], "{}")

    async def test_settle_allocates_positions_and_keeps_cash_non_negative(self):
        await self._player()
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "玄衡", 100_000, 8)
        with self._at(101):
            handled = await self.mgr.settle_due()
        self.assertEqual(handled, 1)
        row = await self._mandate()
        self.assertEqual(row["last_settle_tick"], 101)
        self.assertEqual(row["status"], "active")
        self.assertGreaterEqual(row["cash"], 0)
        self.assertNotEqual(row["positions_json"], "{}")
        logs = await self.mgr._rows(
            "SELECT action FROM invest_agent_logs WHERE mandate_id = ?", (row["id"],)
        )
        self.assertIn("rebalance", [x["action"] for x in logs])

    async def test_fund_shares_keep_two_decimals(self):
        await self._player()
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "青禾", 100_000, 8)
        with self._at(101):
            await self.mgr.settle_due()
        row = await self._mandate()
        positions = self.mgr._load_positions(row["positions_json"])
        funds = [p for p in positions.values() if p["kind"] == "fund"]
        self.assertTrue(funds)
        for pos in funds:
            self.assertEqual(round(float(pos["shares"]), 2), float(pos["shares"]))

    async def test_offline_backfill_advances_to_current_tick(self):
        await self._player()
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "赤霄", 100_000, 16)
        with self._at(103):
            handled = await self.mgr.settle_due()
        self.assertEqual(handled, 1)
        row = await self._mandate()
        self.assertEqual(row["last_settle_tick"], 103)
        self.assertGreaterEqual(row["cash"], 0)

    async def test_stop_loss_closes_mandate(self):
        await self._player()
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "赤霄", 100_000, 32)
        with self._at(101):
            await self.mgr.settle_due()
        await self._set_prices(102, 0.5)
        with self._at(102):
            await self.mgr.settle_due()
        row = await self._mandate()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["close_reason"], "stop_loss")
        self.assertGreaterEqual(row["payout"], 0)
        self.assertEqual(row["positions_json"], "{}")

    async def test_take_profit_charges_profit_share(self):
        await self._player()
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "赤霄", 100_000, 32)
        with self._at(101):
            await self.mgr.settle_due()
        await self._set_prices(102, 1.6)
        with self._at(102):
            await self.mgr.settle_due()
        row = await self._mandate()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["close_reason"], "take_profit")
        self.assertGreater(row["profit_fee"], 0)
        self.assertEqual(row["payout"], max(0, row["final_value"] - row["profit_fee"]))

    async def test_maturity_closes_and_claim_is_idempotent(self):
        await self._player(gold=1_000_000)
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "青禾", 100_000, 8)
        with self._at(101):
            await self.mgr.settle_due()
        with self._at(109):
            await self.mgr.settle_due()
        row = await self._mandate()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["close_reason"], "matured")
        self.assertGreater(row["payout"], 0)

        player = await self.db.get_player_by_id("u1")
        before = player.gold
        ok, msg = await self.mgr.claim(player)
        self.assertTrue(ok, msg)
        after = (await self.db.get_player_by_id("u1")).gold
        self.assertEqual(after, before + row["payout"])

        player = await self.db.get_player_by_id("u1")
        ok2, _ = await self.mgr.claim(player)
        self.assertFalse(ok2)
        self.assertEqual((await self.db.get_player_by_id("u1")).gold, after)

    async def test_early_stop_penalty_and_claim(self):
        await self._player()
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "玄衡", 100_000, 16)
        with self._at(101):
            await self.mgr.settle_due()
        with self._at(102):
            ok, msg = await self.mgr.stop(await self.db.get_player_by_id("u1"))
        self.assertTrue(ok, msg)
        row = await self._mandate()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["close_reason"], "stopped")
        self.assertLessEqual(row["payout"], 99_200 - 1_000)
        self.assertGreaterEqual(row["payout"], 0)

        player = await self.db.get_player_by_id("u1")
        before = player.gold
        ok, _ = await self.mgr.claim(player)
        self.assertTrue(ok)
        after = (await self.db.get_player_by_id("u1")).gold
        self.assertEqual(after, before + row["payout"])

    async def test_stop_without_mandate_fails(self):
        player = await self._player()
        with self._at(100):
            ok, msg = await self.mgr.stop(player)
        self.assertFalse(ok)
        self.assertIn("没有", msg)

    async def test_snapshot_shape(self):
        await self._player()
        with self._at(100):
            await self.mgr.start(await self.db.get_player_by_id("u1"), "玄衡", 100_000, 8)
        with self._at(101):
            snap = await self.mgr.get_snapshot("u1")
        self.assertEqual(len(snap["agents"]), 3)
        self.assertIsNotNone(snap["active"])
        self.assertEqual(snap["active"]["agent_key"], "xuanheng")
        self.assertIn("stop_loss_value", snap["active"])
        self.assertIn("take_profit_value", snap["active"])


class TestInvestAgentMigration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        self.migration = _imp("data.migration")
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.migration._create_all_tables_v2(self.conn)
        await self.migration._ensure_latest_columns(self.conn)

    async def asyncTearDown(self):
        await self.conn.close()
        os.remove(self.db_path)

    async def _tables(self):
        async with self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            rows = await cursor.fetchall()
        return {row["name"] for row in rows}

    async def test_v66_creates_tables_and_is_repeatable(self):
        tables = await self._tables()
        self.assertIn("invest_agent_mandates", tables)
        self.assertIn("invest_agent_logs", tables)
        # 重复执行不应报错
        await self.migration._migrate_to_v66(self.conn, None)
        await self.migration._migrate_to_v66(self.conn, None)
        tables = await self._tables()
        self.assertIn("invest_agent_mandates", tables)

    async def test_v67_creates_levels_table_and_exp_column(self):
        tables = await self._tables()
        self.assertIn("invest_agent_levels", tables)
        async with self.conn.execute("PRAGMA table_info(invest_agent_mandates)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        self.assertIn("exp_delta", columns)
        # 重复执行不应报错
        await self.migration._migrate_to_v67(self.conn, None)
        await self.migration._migrate_to_v67(self.conn, None)

    async def test_unique_active_index(self):
        import sqlite3

        await self.conn.execute(
            "INSERT INTO invest_agent_mandates "
            "(user_id, agent_key, principal, cash, start_tick, end_tick, last_settle_tick, created_at) "
            "VALUES ('u1', 'qinghe', 10000, 9000, 1, 9, 0, 0)"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.conn.execute(
                "INSERT INTO invest_agent_mandates "
                "(user_id, agent_key, principal, cash, start_tick, end_tick, last_settle_tick, created_at) "
                "VALUES ('u1', 'chixiao', 10000, 9000, 1, 9, 0, 0)"
            )


if __name__ == "__main__":
    unittest.main()
