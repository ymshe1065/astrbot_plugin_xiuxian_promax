# tests/test_bank_deposit_cap.py
# 银行存款上限回归测试：按大境界递增、上限按「余额+待领利息」判定、超额提示可存额度
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


class _FakeConfigManager:
    """只提供境界表，够 BankManager 取境界名。"""

    def __init__(self, game_config=None, level_names=None):
        self.game_config = game_config or {}
        self._names = level_names or [f"境界{i}" for i in range(36)]

    def get_level_data(self, cultivation_type="灵修"):
        return [{"level_name": n} for n in self._names]


class TestBankDepositCap(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        bank_mod = _imp("managers.bank_manager")
        self.BankManager = bank_mod.BankManager
        self.DEFAULT_MAX_DEPOSIT = bank_mod.DEFAULT_MAX_DEPOSIT
        self.DEFAULT_DEPOSIT_CAPS = bank_mod.DEFAULT_DEPOSIT_CAPS
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
        await self.db.create_player(self.Player(user_id="u1", gold=60_000_000_000))
        await self.conn.commit()

        self.manager = self.BankManager(self.db, {})

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    # ---- 工具 ----

    def _player(self, level_index: int):
        p = self.Player(user_id="u1", gold=0)
        p.level_index = level_index
        return p

    async def _set_level(self, level_index: int):
        await self.conn.execute(
            "UPDATE players SET level_index = ? WHERE user_id = 'u1'", (level_index,)
        )
        await self.conn.commit()

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

    # ---- 分档表 ----

    async def test_caps_are_monotonic_and_cover_all_levels(self):
        caps = self.manager.deposit_caps
        self.assertGreaterEqual(len(caps), 13)
        self.assertEqual(caps[0][0], 0)
        self.assertEqual(caps[-1][0], 35)
        values = [cap for _, cap in caps]
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(values), len(set(values)))

    async def test_default_cap_by_level_index(self):
        expect = {
            0: 5_000_000,             # 炼气期
            9: 5_000_000,             # 炼气期十层，同一档
            12: 10_000_000,           # 筑基期后期
            15: 20_000_000,           # 金丹期后期
            18: 50_000_000,           # 元婴期后期
            21: 100_000_000,          # 化神期后期
            24: 200_000_000,          # 炼虚期后期
            27: 500_000_000,          # 合体期后期
            30: 1_000_000_000,        # 大乘期后期
            31: 2_000_000_000,        # 渡劫期
            32: 5_000_000_000,        # 地仙
            33: 10_000_000_000,       # 天仙
            34: 20_000_000_000,       # 大罗金仙
            35: 50_000_000_000,       # 混元大罗金仙
        }
        for level, cap in expect.items():
            with self.subTest(level=level):
                self.assertEqual(self.manager.deposit_cap(self._player(level)), cap)

    async def test_every_major_realm_increases_cap(self):
        caps = self.manager.deposit_caps
        for (_, prev_cap), (min_level, cap) in zip(caps, caps[1:]):
            with self.subTest(level=min_level):
                self.assertGreater(cap, prev_cap)

    async def test_game_config_matches_code_defaults(self):
        import json

        game_cfg = json.loads((ROOT / "config" / "game_config.json").read_text(encoding="utf-8"))
        bank = game_cfg["bank"]
        configured = tuple((int(c["min_level"]), int(c["cap"])) for c in bank["deposit_caps"])
        self.assertEqual(configured, self.DEFAULT_DEPOSIT_CAPS)
        self.assertEqual(bank["deposit_cap_scale"], 1.0)

    async def test_config_can_override_caps(self):
        mgr = self.BankManager(
            self.db,
            {"BANK": {"DEPOSIT_CAPS": [{"min_level": 0, "cap": 1_000_000},
                                       {"min_level": 22, "cap": 8_000_000}]}},
        )
        self.assertEqual(mgr.deposit_cap(self._player(0)), 1_000_000)
        self.assertEqual(mgr.deposit_cap(self._player(5)), 1_000_000)
        self.assertEqual(mgr.deposit_cap(self._player(22)), 8_000_000)
        self.assertEqual(mgr.deposit_cap(self._player(35)), 8_000_000)

    async def test_scale_multiplies_all_caps(self):
        mgr = self.BankManager(self.db, {"BANK": {"DEPOSIT_CAP_SCALE": 2.0}})
        base = self.manager.deposit_cap(self._player(22))
        self.assertEqual(mgr.deposit_cap(self._player(22)), base * 2)

    async def test_invalid_caps_fall_back_to_defaults(self):
        mgr = self.BankManager(self.db, {"BANK": {"DEPOSIT_CAPS": [{"min_level": "x", "cap": -1}]}})
        self.assertEqual(mgr.deposit_caps, self.DEFAULT_DEPOSIT_CAPS)

    async def test_game_config_caps_are_loaded_from_config_manager(self):
        cm = _FakeConfigManager(game_config={
            "bank": {"deposit_caps": [{"min_level": 0, "cap": 3_000_000}], "deposit_cap_scale": 1.5}
        })
        mgr = self.BankManager(self.db, {}, cm)
        self.assertEqual(mgr.deposit_cap(self._player(0)), 4_500_000)

    async def test_caps_still_loaded_when_plugin_config_has_bank_section(self):
        # 真实部署里插件配置往往被 schema 默认值填满 BANK 段，
        # 分档表只在 game_config 里，不能被整段替换吃掉
        cm = _FakeConfigManager(game_config={
            "bank": {"deposit_caps": [{"min_level": 0, "cap": 7_000_000},
                                      {"min_level": 22, "cap": 70_000_000}]}
        })
        mgr = self.BankManager(self.db, {"BANK": {"MAX_DEPOSIT": 100_000_000}}, cm)
        self.assertEqual(mgr.deposit_cap(self._player(0)), 7_000_000)
        self.assertEqual(mgr.deposit_cap(self._player(22)), 70_000_000)

    async def test_plugin_config_overrides_game_config_scalar(self):
        cm = _FakeConfigManager(game_config={"bank": {"daily_interest_rate": 0.002}})
        mgr = self.BankManager(self.db, {"BANK": {"DAILY_INTEREST_RATE": 0.0005}}, cm)
        self.assertAlmostEqual(mgr.daily_interest_rate, 0.0005)

    async def test_cap_info_reports_next_realm(self):
        cm = _FakeConfigManager(level_names=[f"等级{i}" for i in range(36)])
        mgr = self.BankManager(self.db, {}, cm)
        info = mgr.deposit_cap_info(self._player(22))
        self.assertEqual(info["cap"], 200_000_000)
        self.assertEqual(info["level_name"], "等级22")
        self.assertEqual(info["next_cap"], 500_000_000)
        self.assertEqual(info["next_level_name"], "等级25")

    async def test_top_realm_has_no_next_cap(self):
        info = self.manager.deposit_cap_info(self._player(35))
        self.assertIsNone(info["next_cap"])

    # ---- 存款判定 ----

    async def test_deposit_exactly_to_cap_allowed(self):
        await self._set_level(22)  # 炼虚期，上限 2 亿
        ok, msg = await self._deposit(200_000_000)
        self.assertTrue(ok, msg)
        account = await self.db.ext.get_bank_account("u1")
        self.assertEqual(account["balance"], 200_000_000)

    async def test_deposit_beyond_cap_rejected_with_room_hint(self):
        await self._set_level(22)
        ok, msg = await self._deposit(200_000_000)
        self.assertTrue(ok, msg)

        ok, msg = await self._deposit(1)
        self.assertFalse(ok)
        self.assertIn("存款上限为 200,000,000", msg)
        self.assertIn("本次最多还能存 0", msg)
        # 突破提示：下一档是合体期 5 亿
        self.assertIn("500,000,000", msg)

    async def test_partial_room_reported(self):
        await self._set_level(0)  # 炼气期，上限 500 万
        ok, msg = await self._deposit(4_000_000)
        self.assertTrue(ok, msg)

        ok, msg = await self._deposit(2_000_000)
        self.assertFalse(ok)
        self.assertIn("本次最多还能存 1,000,000", msg)

        ok, msg = await self._deposit(1_000_000)
        self.assertTrue(ok, msg)
        account = await self.db.ext.get_bank_account("u1")
        self.assertEqual(account["balance"], 5_000_000)

    async def test_rejected_deposit_keeps_gold_untouched(self):
        await self._set_level(13)  # 金丹期，上限 2000 万
        ok, msg = await self._deposit(20_000_000)
        self.assertTrue(ok, msg)
        before = (await self.db.get_player_by_id("u1")).gold

        ok, _ = await self._deposit(5_000_000)
        self.assertFalse(ok)
        after = (await self.db.get_player_by_id("u1")).gold
        self.assertEqual(before, after)

    async def test_breakthrough_raises_cap_for_existing_balance(self):
        await self._set_level(22)
        ok, msg = await self._deposit(200_000_000)
        self.assertTrue(ok, msg)

        await self._set_level(25)  # 突破到合体期
        ok, msg = await self._deposit(300_000_000)
        self.assertTrue(ok, msg)
        account = await self.db.ext.get_bank_account("u1")
        self.assertEqual(account["balance"], 500_000_000)

    # ---- 利息计入上限 ----

    async def test_pending_interest_counts_toward_cap(self):
        await self._set_level(13)  # 上限 2000 万
        await self._set_balance(20_000_000 - 1, int(time.time()) - 3 * 86400)

        ok, msg = await self._deposit(1000)
        self.assertFalse(ok)
        self.assertIn("待领利息", msg)
        self.assertIn("本次最多还能存 0", msg)

    async def test_interest_overflow_detected_before_deposit(self):
        await self._set_level(13)
        await self._set_balance(20_000_000, int(time.time()))
        ok, msg = await self._deposit(1)
        self.assertFalse(ok)
        self.assertIn("本次最多还能存 0", msg)

    # ---- 提示文案 ----

    async def test_success_message_shows_cap_and_remaining_room(self):
        await self._set_level(16)  # 元婴期，上限 5000 万
        ok, msg = await self._deposit(1_000_000)
        self.assertTrue(ok, msg)
        self.assertIn("存款上限：50,000,000", msg)
        self.assertIn("还能存 49,000,000", msg)


if __name__ == "__main__":
    unittest.main()
