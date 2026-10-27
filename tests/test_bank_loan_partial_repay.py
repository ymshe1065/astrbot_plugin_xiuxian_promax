# tests/test_bank_loan_partial_repay.py
# 分期还款回归测试：已还金额必须能被回读，旧还款不能被覆盖
import asyncio
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

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


class TestBankLoanPartialRepay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.BankManager = _imp("managers.bank_manager").BankManager
        self.Player = _imp("models").Player

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        await self.db.create_player(self.Player(user_id="u1", gold=10_000_000))

        now = int(time.time())
        await self.conn.execute(
            "INSERT INTO bank_loans "
            "(user_id, principal, interest_rate, borrowed_at, due_at, status, loan_type) "
            "VALUES (?, ?, ?, ?, ?, 'active', 'normal')",
            ("u1", 100_000, 0.005, now, now + 7 * 86400),
        )
        await self.conn.commit()
        self.manager = self.BankManager(self.db, {})

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def test_partial_repay_persists_and_updates_total_due(self):
        ok, msg = await self.manager.repay(await self.db.get_player_by_id("u1"), 30_000)
        self.assertTrue(ok, msg)

        loan = await self.db.ext.get_active_loan("u1")
        self.assertEqual(loan["paid_interest"], 500)
        self.assertEqual(loan["paid_principal"], 29_500)

        info = await self.manager.get_loan_info(await self.db.get_player_by_id("u1"))
        self.assertEqual(info["total_due"], 70_500)

        ok, msg = await self.manager.repay(await self.db.get_player_by_id("u1"), 20_000)
        self.assertTrue(ok, msg)
        loan = await self.db.ext.get_active_loan("u1")
        self.assertEqual(loan["paid_principal"], 49_500)

        ok, msg = await self.manager.repay(await self.db.get_player_by_id("u1"), None)
        self.assertTrue(ok, msg)
        self.assertIsNone(await self.db.ext.get_active_loan("u1"))


if __name__ == "__main__":
    unittest.main()
