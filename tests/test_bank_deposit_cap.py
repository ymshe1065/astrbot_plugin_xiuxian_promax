# tests/test_bank_deposit_cap.py
# 银行存款上限回归测试：默认 1 亿、上限按「余额+待领利息」判定、超额提示可存额度
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


CAP = 100_000_000


class TestBankDepositCap(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        bank_mod = _imp("managers.bank_manager")
        self.BankManager = bank_mod.BankManager
        self.DEFAULT_MAX_DEPOSIT = bank_mod.DEFAULT_MAX_DEPOSIT
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
        await self.db.create_player(self.Player(user_id="u1", gold=300_000_000))
        await self.conn.commit()

        self.manager = self.BankManager(self.db, {})

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _deposit(self, amount: int):
        player = await self.db.get_player_by_id("u1")
        return await self.manager.deposit(player, amount)

    async def _set_balance(self, balance: int, last_interest_time: int):
        await self.conn.execute(
            "INSERT INTO bank_accounts (user_id, balance, last_interest_time) "
            "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "balance = excluded.balance, last_interest_time = excluded.last_interest_time",
            ("u1", balance, last_interest_time),
        )
        await self.conn.commit()

    # ---- 默认值与配置 ----

    async def test_default_cap_is_one_hundred_million(self):
        self.assertEqual(self.DEFAULT_MAX_DEPOSIT, CAP)
        self.assertEqual(self.manager.max_deposit, CAP)

    async def test_config_can_override_cap(self):
        mgr = self.BankManager(self.db, {"BANK": {"MAX_DEPOSIT": 50_000_000}})
        self.assertEqual(mgr.max_deposit, 50_000_000)

    async def test_game_config_value_is_one_hundred_million(self):
        import json

        game_cfg = json.loads((ROOT / "config" / "game_config.json").read_text(encoding="utf-8"))
        self.assertEqual(game_cfg["bank"]["max_deposit"], CAP)

    # ---- 上限判定 ----

    async def test_deposit_exactly_to_cap_allowed(self):
        ok, msg = await self._deposit(CAP)
        self.assertTrue(ok, msg)
        account = await self.db.ext.get_bank_account("u1")
        self.assertEqual(account["balance"], CAP)

    async def test_deposit_beyond_cap_rejected_with_room_hint(self):
        ok, msg = await self._deposit(CAP)
        self.assertTrue(ok, msg)

        ok, msg = await self._deposit(1)
        self.assertFalse(ok)
        self.assertIn("存款上限为 100,000,000", msg)
        self.assertIn("本次最多还能存 0", msg)

    async def test_partial_room_reported(self):
        ok, msg = await self._deposit(90_000_000)
        self.assertTrue(ok, msg)

        ok, msg = await self._deposit(20_000_000)
        self.assertFalse(ok)
        self.assertIn("本次最多还能存 10,000,000", msg)

        ok, msg = await self._deposit(10_000_000)
        self.assertTrue(ok, msg)
        account = await self.db.ext.get_bank_account("u1")
        self.assertEqual(account["balance"], CAP)

    async def test_rejected_deposit_keeps_gold_untouched(self):
        ok, msg = await self._deposit(CAP)
        self.assertTrue(ok, msg)
        before = (await self.db.get_player_by_id("u1")).gold

        ok, _ = await self._deposit(5_000_000)
        self.assertFalse(ok)
        after = (await self.db.get_player_by_id("u1")).gold
        self.assertEqual(before, after)

    # ---- 利息计入上限 ----

    async def test_pending_interest_counts_toward_cap(self):
        # 余额离上限只差 1 灵石，但压了 3 天利息 → 可用额度已被利息吃满
        await self._set_balance(CAP - 1, int(time.time()) - 3 * 86400)

        ok, msg = await self._deposit(1000)
        self.assertFalse(ok)
        self.assertIn("待领利息", msg)
        self.assertIn("本次最多还能存 0", msg)

    async def test_interest_overflow_detected_before_deposit(self):
        # 余额本身已顶格，任何存入都应被拒
        await self._set_balance(CAP, int(time.time()))
        ok, msg = await self._deposit(1)
        self.assertFalse(ok)
        self.assertIn("本次最多还能存 0", msg)

    # ---- 提示文案 ----

    async def test_success_message_shows_cap_and_remaining_room(self):
        ok, msg = await self._deposit(1_000_000)
        self.assertTrue(ok, msg)
        self.assertIn("存款上限：100,000,000", msg)
        self.assertIn("还能存 99,000,000", msg)


if __name__ == "__main__":
    unittest.main()
