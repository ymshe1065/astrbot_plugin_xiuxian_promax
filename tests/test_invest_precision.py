# -*- coding: utf-8 -*-
"""仙市精度回归：基金份额 2 位小数、股票整数股、历史持仓归整。"""
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
from tests._astrbot_stub import install as _install_astrbot_stub  # noqa: E402

_install_astrbot_stub()

import aiosqlite  # noqa: E402


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


FIXED_SECRET = "unit-test-secret-9f3a"


async def _noop(*_args, **_kwargs):
    return None


class TestInvestPrecision(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _player(self, uid: str = "u1", gold: int = 1_000_000):
        await self.db.create_player(self.Player(user_id=uid, gold=gold, user_name="测试道友"))
        return await self.db.get_player_by_id(uid)

    def _patch(self, tick: int = 100):
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(self.InvestManager, "tick_of", classmethod(lambda cls, moment=None: tick))
        )
        stack.enter_context(
            mock.patch.object(self.InvestManager, "is_open", classmethod(lambda cls, moment=None: True))
        )
        # 跳过盘面结算，测试只关心精度，不关心价格走势
        stack.enter_context(mock.patch.object(self.InvestManager, "settle_to", _noop))
        return stack

    async def _set_price(self, code: str, price: float):
        await self.conn.execute("UPDATE invest_products SET price = ? WHERE code = ?", (price, code))
        await self.conn.commit()

    async def _holding(self, code: str):
        cur = await self.conn.execute(
            "SELECT shares, cost FROM invest_holdings WHERE user_id = 'u1' AND code = ?", (code,)
        )
        return await cur.fetchone()

    async def test_fund_subscribe_floors_to_two_decimals(self):
        player = await self._player()
        await self._set_price("F01", 1.2345)
        with self._patch():
            ok, msg = await self.manager.subscribe_fund(player, "F01", 10_000)
        self.assertTrue(ok, msg)
        row = await self._holding("F01")
        self.assertAlmostEqual(float(row["shares"]), 8100.44, places=6)
        self.assertIn("8100.44", msg)

    async def test_fund_redeem_below_min_rejected(self):
        player = await self._player()
        await self._set_price("F01", 1.0)
        with self._patch():
            ok, _ = await self.manager.subscribe_fund(player, "F01", 100)
            ok2, msg2 = await self.manager.redeem_fund(player, "F01", 0.005)
        self.assertTrue(ok)
        self.assertFalse(ok2)
        self.assertIn("0.01", msg2)

    async def test_fund_redeem_dust_clears_position(self):
        player = await self._player()
        await self._set_price("F01", 1.0)
        await self.conn.execute(
            "INSERT INTO invest_holdings (user_id, code, shares, cost, hold_since, last_buy) "
            "VALUES ('u1', 'F01', 10.005, 10000, 0, 0)"
        )
        await self.conn.commit()
        with self._patch():
            ok, msg = await self.manager.redeem_fund(player, "F01", 10.0)
        self.assertTrue(ok, msg)
        self.assertIsNone(await self._holding("F01"))

    async def test_stock_fractional_lots_rejected(self):
        player = await self._player()
        await self._set_price("S01", 10.0)
        with self._patch():
            ok, msg = await self.manager.buy_stock(player, "S01", 1.5)
        self.assertFalse(ok)
        self.assertIn("整数", msg)

    async def test_legacy_fractional_stock_normalized_on_sell(self):
        player = await self._player()
        await self._set_price("S01", 10.0)
        await self.conn.execute(
            "INSERT INTO invest_holdings (user_id, code, shares, cost, hold_since, last_buy) "
            "VALUES ('u1', 'S01', 150.7, 200000, 0, 0)"
        )
        await self.conn.commit()
        with self._patch():
            ok, msg = await self.manager.sell_stock(player, "S01", 1)
        self.assertTrue(ok, msg)
        row = await self._holding("S01")
        self.assertEqual(int(row["shares"]), 50)
        self.assertLess(int(row["cost"]), 200000)

    async def test_buy_stock_after_dust_position_inserts_new_row(self):
        """历史小于 1 股的小数持仓被清理后，买入仍能正常建立新持仓。"""
        player = await self._player()
        await self._set_price("S01", 10.0)
        await self.conn.execute(
            "INSERT INTO invest_holdings (user_id, code, shares, cost, hold_since, last_buy) "
            "VALUES ('u1', 'S01', 0.4, 4, 0, 0)"
        )
        await self.conn.commit()
        with self._patch():
            ok, msg = await self.manager.buy_stock(player, "S01", 1)
        self.assertTrue(ok, msg)
        row = await self._holding("S01")
        self.assertEqual(int(row["shares"]), 100)

    async def test_normalize_holdings_precision(self):
        await self.conn.execute(
            "INSERT INTO invest_holdings (user_id, code, shares, cost, hold_since, last_buy) "
            "VALUES ('u1', 'S01', 120.9, 100000, 0, 0)"
        )
        await self.conn.execute(
            "INSERT INTO invest_holdings (user_id, code, shares, cost, hold_since, last_buy) "
            "VALUES ('u1', 'F01', 10.456, 12345, 0, 0)"
        )
        await self.conn.execute(
            "INSERT INTO invest_holdings (user_id, code, shares, cost, hold_since, last_buy) "
            "VALUES ('u1', 'F02', 0.004, 5, 0, 0)"
        )
        await self.conn.commit()
        await self.manager._normalize_holdings_precision()
        stock = await self._holding("S01")
        fund = await self._holding("F01")
        dust = await self._holding("F02")
        self.assertEqual(int(stock["shares"]), 120)
        self.assertAlmostEqual(float(fund["shares"]), 10.45, places=6)
        self.assertIsNone(dust)


if __name__ == "__main__":
    unittest.main()
