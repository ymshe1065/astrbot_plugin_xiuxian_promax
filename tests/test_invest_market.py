# tests/test_invest_market.py
# 仙市投资系统回归：时序、结算确定性、价格约束、T+0 同价、手续费下限、
# 贷款准入、持仓上限、封盘拦截、赎回费阶梯、分红、补结算重放一致
import contextlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")


class _StubLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


api_mod.logger = _StubLogger()
api_mod.AstrBotConfig = dict
event_mod.AstrMessageEvent = object
api_mod.event = event_mod
all_mod = types.ModuleType("astrbot.api.all")
all_mod.At = object
all_mod.Plain = object
api_mod.all = all_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod)
sys.modules.setdefault("astrbot.api.all", all_mod)


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


FIXED_SECRET = "unit-test-secret-9f3a"


class TestInvestTiming(unittest.TestCase):
    """盘次换算纯函数，不需要数据库。"""

    def setUp(self):
        self.InvestManager = _imp("managers.invest_manager").InvestManager

    def test_slot_boundaries(self):
        from datetime import date, datetime
        base_day = (date(2026, 10, 29) - date(2026, 1, 1)).days
        cases = {
            (6, 59): (base_day - 1, 15),
            (7, 0): (base_day, 0),
            (8, 59): (base_day, 1),
            (9, 0): (base_day, 2),
            (14, 59): (base_day, 7),
            (15, 0): (base_day, 8),
            (21, 0): (base_day, 14),
            (22, 59): (base_day, 15),
            (23, 0): (base_day, 15),
            (23, 59): (base_day, 15),
        }
        for (hour, minute), (expect_day, expect_slot) in cases.items():
            tick = self.InvestManager.tick_of(datetime(2026, 10, 29, hour, minute))
            self.assertEqual(
                (tick // 16, tick % 16),
                (expect_day, expect_slot),
                msg=f"{hour:02d}:{minute:02d} 应为第 {expect_day} 天第 {expect_slot} 盘",
            )

    def test_monotonic_over_four_days(self):
        from datetime import datetime, timedelta
        moment = datetime(2026, 10, 29, 0, 0)
        last = -1
        for _ in range(4 * 24 * 60):
            tick = self.InvestManager.tick_of(moment)
            self.assertGreaterEqual(tick, last, msg=f"盘次在 {moment} 倒退")
            last = tick
            moment += timedelta(minutes=1)

    def test_sixteen_new_ticks_per_day(self):
        from datetime import datetime, timedelta
        moment = datetime(2026, 10, 29, 0, 0)
        ticks = set()
        for _ in range(24 * 60):
            ticks.add(self.InvestManager.tick_of(moment))
            moment += timedelta(minutes=1)
        # 含凌晨延续的前一日最后一盘，共 17 个；新增盘次为 16
        self.assertEqual(len(ticks), 17)

    def test_news_every_tick(self):
        from datetime import datetime
        day = self.InvestManager.tick_of(datetime(2026, 10, 29, 12, 0)) // 16
        labels = [
            self.InvestManager.slot_label(t)
            for t in range(day * 16, (day + 1) * 16)
        ]
        self.assertEqual(labels, [f"{hour:02d}:00" for hour in range(7, 23)])

    def test_market_open_window(self):
        from datetime import datetime
        self.assertFalse(self.InvestManager.is_open(datetime(2026, 10, 29, 6, 59)))
        self.assertTrue(self.InvestManager.is_open(datetime(2026, 10, 29, 7, 0)))
        self.assertTrue(self.InvestManager.is_open(datetime(2026, 10, 29, 22, 59)))
        self.assertFalse(self.InvestManager.is_open(datetime(2026, 10, 29, 23, 0)))
        self.assertFalse(self.InvestManager.is_open(datetime(2026, 10, 30, 3, 0)))


class TestInvestMarket(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.migration = migration
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

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    # ===== 辅助 =====

    async def _player(self, uid: str = "u1", gold: int = 1_000_000):
        await self.db.create_player(self.Player(user_id=uid, gold=gold, user_name="测试道友"))
        return await self.db.get_player_by_id(uid)

    def _patch_tick(self, tick: int, opened: bool = True):
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(self.InvestManager, "tick_of", classmethod(lambda cls, moment=None: tick))
        )
        stack.enter_context(
            mock.patch.object(self.InvestManager, "is_open", classmethod(lambda cls, moment=None: opened))
        )
        return stack

    async def _price(self, code: str) -> float:
        return float(
            await self.manager._scalar("SELECT price FROM invest_products WHERE code = ?", (code,))
        )

    # ===== 结算 =====

    async def test_settle_is_deterministic(self):
        """相同 tick 序列必须得到相同价格——这是补结算能重放的前提。"""
        for tick in range(1, 21):
            await self.manager._settle_tick(tick)
        first = await self._price("S01")

        # 换一条全新的库，用同样的 secret 重跑
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        import aiosqlite
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        try:
            await self.migration._create_all_tables_v2(conn)
            await self.migration._ensure_latest_columns(conn)
            db2 = self.DataBase(path)
            db2.conn = conn
            db2.ext = self.DatabaseExtended(conn)
            await db2.ext.set_system_config("invest_secret", FIXED_SECRET)
            mgr2 = self.InvestManager(db2, {}, None)
            mgr2._secret = FIXED_SECRET
            await mgr2._seed_products()
            for tick in range(1, 21):
                await mgr2._settle_tick(tick)
            second = float(
                await mgr2._scalar("SELECT price FROM invest_products WHERE code = 'S01'")
            )
            self.assertAlmostEqual(first, second, places=6)
        finally:
            await conn.close()
            os.remove(path)

    async def test_price_limits_respected(self):
        """单盘不超过 price_limit，日累计不越 day_up / day_down 熔断。"""
        await self.manager._settle_tick(1)
        for tick in range(2, 100):
            await self.manager._settle_tick(tick)
            row = (await self.manager._rows(
                "SELECT day_open, price_limit, day_up, day_down FROM invest_products WHERE code = 'S01'"
            ))[0]
            change = float(
                await self.manager._scalar(
                    "SELECT change_rate FROM invest_prices WHERE code = 'S01' AND tick = ?", (tick,)
                )
            )
            price = await self._price("S01")
            self.assertLessEqual(
                abs(change), float(row["price_limit"]) + 1e-6, msg=f"第 {tick} 盘超出单盘限幅"
            )
            day_open = float(row["day_open"])
            self.assertLessEqual(
                price, day_open * (1 + float(row["day_up"])) + 1e-4,
                msg=f"第 {tick} 盘超出日上限",
            )
            self.assertGreaterEqual(
                price, day_open * (1 - float(row["day_down"])) - 1e-4,
                msg=f"第 {tick} 盘跌破日下限",
            )

    async def test_prices_stay_bounded_long_run(self):
        """长期不应跑飞：软回归要能把价格拉在合理量级内。"""
        for tick in range(1, 400):
            await self.manager._settle_tick(tick)
        for code, base in (("S01", 42.0), ("S05", 55.0), ("F01", 1.0)):
            price = await self._price(code)
            self.assertLess(price, base * 12, msg=f"{code} 价格跑飞：{price}")
            self.assertGreater(price, base * 0.05, msg=f"{code} 价格崩塌：{price}")

    async def test_news_generated_every_tick(self):
        day = 5
        for slot in range(4):
            await self.manager._settle_tick(day * 16 + slot)
        rows = await self.manager._rows(
            "SELECT slot FROM invest_events WHERE trade_day = ? ORDER BY slot", (day,)
        )
        self.assertEqual([r["slot"] for r in rows], [0, 1, 2, 3])

    async def test_news_is_idempotent(self):
        """同一盘重复触发不能生成第二条消息。"""
        day = 6
        await self.manager._settle_tick(day * 8)
        await self.manager._ensure_news(day, 0)
        await self.manager._ensure_news(day, 0)
        count = await self.manager._scalar(
            "SELECT COUNT(*) FROM invest_events WHERE trade_day = ? AND slot = 0", (day,)
        )
        self.assertEqual(count, 1)

    async def test_backfill_matches_incremental(self):
        """一次性补算 40 盘，结果必须与逐盘结算完全一致。"""
        for tick in range(1, 41):
            await self.manager._settle_tick(tick)
        incremental = await self.manager._rows(
            "SELECT code, tick, close FROM invest_prices ORDER BY code, tick"
        )

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        import aiosqlite
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        try:
            await self.migration._create_all_tables_v2(conn)
            await self.migration._ensure_latest_columns(conn)
            db2 = self.DataBase(path)
            db2.conn = conn
            db2.ext = self.DatabaseExtended(conn)
            await db2.ext.set_system_config("invest_secret", FIXED_SECRET)
            mgr2 = self.InvestManager(db2, {}, None)
            mgr2._secret = FIXED_SECRET
            await mgr2._seed_products()
            await db2.ext.set_system_config("invest_last_tick", "0")
            with mock.patch.object(
                self.InvestManager, "tick_of", classmethod(lambda cls, moment=None: 40)
            ):
                moved = await mgr2.settle_to()
            self.assertEqual(moved, 40)
            backfilled = await mgr2._rows(
                "SELECT code, tick, close FROM invest_prices ORDER BY code, tick"
            )
            self.assertEqual(len(incremental), len(backfilled))
            for a, b in zip(incremental, backfilled):
                self.assertEqual(a["code"], b["code"])
                self.assertEqual(a["tick"], b["tick"])
                self.assertAlmostEqual(float(a["close"]), float(b["close"]), places=6)
        finally:
            await conn.close()
            os.remove(path)

    async def test_settle_to_is_idempotent(self):
        with mock.patch.object(self.InvestManager, "tick_of", classmethod(lambda cls, moment=None: 10)):
            await self.db.ext.set_system_config("invest_last_tick", "0")
            first = await self.manager.settle_to()
            second = await self.manager.settle_to()
        self.assertEqual(first, 10)
        self.assertEqual(second, 0)

    # ===== 交易 =====

    async def test_buy_and_sell_same_tick_only_costs_fees(self):
        """T+0：同盘次买卖同价，来回只亏手续费。"""
        await self.manager._settle_tick(10)
        player = await self._player(gold=1_000_000)
        start_gold = player.gold
        price = await self._price("S01")

        with self._patch_tick(10):
            ok, msg = await self.manager.buy_stock(player, "S01", 1)
            self.assertTrue(ok, msg)
            after_buy = (await self.db.get_player_by_id("u1")).gold
            amount = int(round(100 * price))
            buy_fee = max(20, int(amount * 0.0015))
            self.assertEqual(after_buy, start_gold - amount - buy_fee)

            ok, msg = await self.manager.sell_stock(player, "S01", 1)
            self.assertTrue(ok, msg)
            after_sell = (await self.db.get_player_by_id("u1")).gold
            sell_fee = max(20, int(amount * 0.0015))
            self.assertEqual(after_sell, start_gold - buy_fee - sell_fee)

    async def test_min_fee_floor(self):
        """小额交易按 20 灵石最低手续费收，防止刷单。"""
        await self.manager._settle_tick(10)
        player = await self._player()
        with self._patch_tick(10):
            ok, msg = await self.manager.buy_stock(player, "S03", 1)
        self.assertTrue(ok, msg)
        fee = await self.manager._scalar(
            "SELECT fee FROM invest_orders WHERE user_id = ? ORDER BY id DESC LIMIT 1", ("u1",)
        )
        self.assertEqual(int(fee), 20)

    async def test_single_tick_lot_cap(self):
        await self.manager._settle_tick(10)
        player = await self._player()
        with self._patch_tick(10):
            ok, msg = await self.manager.buy_stock(player, "S01", 25)
        self.assertFalse(ok)
        self.assertIn("20", msg)

    async def test_closed_market_blocks_trade(self):
        await self.manager._settle_tick(10)
        player = await self._player()
        with self._patch_tick(10, opened=False):
            ok, msg = await self.manager.buy_stock(player, "S01", 1)
        self.assertFalse(ok)
        self.assertIn("闭市", msg)

    async def test_loan_threshold_blocks_stock(self):
        await self.manager._settle_tick(10)
        player = await self._player()
        await self.conn.execute(
            "INSERT INTO bank_loans (user_id, principal, interest_rate, borrowed_at, due_at, status) "
            "VALUES (?, ?, 0.005, ?, ?, 'active')",
            ("u1", 150_000, 0, 9999999999),
        )
        await self.conn.commit()
        with self._patch_tick(10):
            ok, msg = await self.manager.buy_stock(player, "S01", 1)
        self.assertFalse(ok)
        self.assertIn("10", msg.replace(",", ""))

    async def test_loan_under_threshold_allowed(self):
        await self.manager._settle_tick(10)
        player = await self._player()
        await self.conn.execute(
            "INSERT INTO bank_loans (user_id, principal, interest_rate, borrowed_at, due_at, status) "
            "VALUES (?, ?, 0.005, ?, ?, 'active')",
            ("u1", 30_000, 0, 9999999999),
        )
        await self.conn.commit()
        with self._patch_tick(10):
            ok, msg = await self.manager.buy_stock(player, "S01", 1)
        self.assertTrue(ok, msg)

    async def test_fund_rejects_any_loan(self):
        await self.manager._settle_tick(10)
        player = await self._player()
        await self.conn.execute(
            "INSERT INTO bank_loans (user_id, principal, interest_rate, borrowed_at, due_at, status) "
            "VALUES (?, ?, 0.005, ?, ?, 'active')",
            ("u1", 5_000, 0, 9999999999),
        )
        await self.conn.commit()
        with self._patch_tick(10):
            ok, msg = await self.manager.subscribe_fund(player, "F01", 10_000)
        self.assertFalse(ok)
        self.assertIn("贷款", msg)

    async def test_cannot_sell_what_not_held(self):
        await self.manager._settle_tick(10)
        player = await self._player()
        with self._patch_tick(10):
            ok, msg = await self.manager.sell_stock(player, "S02", 1)
        self.assertFalse(ok)
        self.assertIn("没有持有", msg)

    async def test_insufficient_gold_rejected(self):
        await self.manager._settle_tick(10)
        player = await self._player(gold=500)
        with self._patch_tick(10):
            ok, msg = await self.manager.buy_stock(player, "S06", 1)
        self.assertFalse(ok)
        self.assertIn("灵石不足", msg)

    async def test_unknown_code_rejected(self):
        await self.manager._settle_tick(10)
        player = await self._player()
        with self._patch_tick(10):
            ok, msg = await self.manager.buy_stock(player, "S99", 1)
        self.assertFalse(ok)
        self.assertIn("不存在", msg)

    # ===== 基金 =====

    async def test_fund_redeem_fee_tiers(self):
        await self.manager._settle_tick(10)
        player = await self._player()

        with self._patch_tick(100):
            ok, msg = await self.manager.subscribe_fund(player, "F01", 10_000)
            self.assertTrue(ok, msg)

        nav = await self._price("F01")
        # 持有 5 盘 → 0.5%
        with self._patch_tick(105):
            ok, msg = await self.manager.redeem_fund(player, "F01", 0)
        self.assertTrue(ok, msg)
        fee = int(await self.manager._scalar(
            "SELECT fee FROM invest_orders WHERE user_id = ? ORDER BY id DESC LIMIT 1", ("u1",)
        ))
        self.assertAlmostEqual(fee, int(10_000 * nav * 0.005), delta=2)

        # 持有 30 盘 → 0.2%
        with self._patch_tick(200):
            await self.manager.subscribe_fund(player, "F01", 10_000)
        nav2 = await self._price("F01")
        with self._patch_tick(230):
            ok, _ = await self.manager.redeem_fund(player, "F01", 0)
        self.assertTrue(ok)
        fee2 = int(await self.manager._scalar(
            "SELECT fee FROM invest_orders WHERE user_id = ? ORDER BY id DESC LIMIT 1", ("u1",)
        ))
        self.assertAlmostEqual(fee2, int(10_000 * nav2 * 0.002), delta=2)

        # 持有 90 盘 → 免赎回费
        with self._patch_tick(300):
            await self.manager.subscribe_fund(player, "F01", 10_000)
        with self._patch_tick(390):
            ok, _ = await self.manager.redeem_fund(player, "F01", 0)
        self.assertTrue(ok)
        fee3 = int(await self.manager._scalar(
            "SELECT fee FROM invest_orders WHERE user_id = ? ORDER BY id DESC LIMIT 1", ("u1",)
        ))
        self.assertEqual(fee3, 0)

    async def test_fund_shares_track_nav(self):
        await self.manager._settle_tick(10)
        player = await self._player()
        nav = await self._price("F01")
        with self._patch_tick(20):
            ok, msg = await self.manager.subscribe_fund(player, "F01", 50_000)
        self.assertTrue(ok, msg)
        shares = float(await self.manager._scalar(
            "SELECT shares FROM invest_holdings WHERE user_id = 'u1' AND code = 'F01'"
        ))
        self.assertAlmostEqual(shares, 50_000 / nav, places=4)

    # ===== 分红 =====

    async def test_dividend_requires_holding_window(self):
        await self.manager._settle_tick(19)
        player = await self._player()
        with self._patch_tick(31):
            ok, msg = await self.manager.buy_stock(player, "S01", 1)
        self.assertTrue(ok, msg)

        # 持有不足 12 盘（32 - 31 = 1）：不派息
        await self.manager._settle_tick(32)
        count = await self.manager._scalar("SELECT COUNT(*) FROM invest_dividends")
        self.assertEqual(int(count), 0)

        # hold_since 改到 20，64 - 20 = 44 盘，应派息
        await self.conn.execute(
            "UPDATE invest_holdings SET hold_since = 20 WHERE user_id = 'u1' AND code = 'S01'"
        )
        await self.conn.commit()
        await self.manager._settle_tick(64)
        count = await self.manager._scalar("SELECT COUNT(*) FROM invest_dividends")
        self.assertEqual(int(count), 1)
        amount = int(await self.manager._scalar("SELECT amount FROM invest_dividends LIMIT 1"))
        self.assertGreater(amount, 0)

    # ===== 展示层数据 =====

    async def test_overview_shape(self):
        for tick in range(1, 6):
            await self.manager._settle_tick(tick)
        with self._patch_tick(5):
            data = await self.manager.get_overview()
        self.assertEqual(len(data["items"]), 9)  # 6 股票 + 3 基金
        self.assertEqual(len(data["news"]), 5)
        self.assertTrue(all("change" in i and "today" in i for i in data["items"]))
        self.assertIn("current_hint", data)

    async def test_holdings_profit(self):
        await self.manager._settle_tick(10)
        player = await self._player()
        with self._patch_tick(10):
            await self.manager.buy_stock(player, "S01", 2)
            rows = await self.manager.get_holdings("u1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shares"], 200)
        self.assertLess(rows[0]["profit"], 0)  # 含买入手续费，必然浮亏
