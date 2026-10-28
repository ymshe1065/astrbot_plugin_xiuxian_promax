# tests/test_combat_limits.py
"""切磋/决斗每日限制与切磋邀请回归测试。"""

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

    def exception(self, *a, **k):
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


class _FakeEvent:
    def __init__(self, user_id, text=""):
        self._user_id = str(user_id)
        self._text = text
        self.results = []

    def get_sender_id(self):
        return self._user_id

    def get_message_str(self):
        return self._text

    def plain_result(self, text):
        self.results.append(text)
        return text


class TestCombatDailyLimits(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _imp("data")
        cls.ConfigManager = _imp("config_manager").ConfigManager
        cls.Player = _imp("models").Player
        cls.CombatManager = _imp("managers.combat_manager").CombatManager
        cls.CombatHandlers = _imp("handlers.combat_handlers").CombatHandlers
        cls.get_event_flag = staticmethod(_imp("handlers.utils").get_event_flag)
        cls.config_manager = cls.ConfigManager(ROOT)

    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = self.data.DataBase(self.db_path)
        await self.db.connect()
        await self.data.MigrationManager(self.db.conn, self.config_manager).migrate()
        self.handlers = self.CombatHandlers(
            self.db, self.CombatManager(), self.config_manager
        )
        await self._create_player("1001", "甲")
        await self._create_player("1002", "乙")

    async def asyncTearDown(self):
        await self.db.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    async def _create_player(self, user_id, name):
        player = self.Player(
            user_id=user_id,
            user_name=name,
            level_index=1,
            experience=1000,
            gold=10000,
            hp=10000,
            spiritual_qi=1000,
            max_spiritual_qi=1000,
            physical_damage=100,
            magic_damage=100,
            physical_defense=50,
            magic_defense=50,
            mental_power=100,
        )
        await self.db.create_player(player)

    async def _run(self, generator):
        async for _ in generator:
            pass

    async def test_combat_limit_tables_exist(self):
        async with self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('combat_daily', 'spar_requests')"
        ) as cursor:
            names = {row[0] for row in await cursor.fetchall()}
        self.assertEqual(names, {"combat_daily", "spar_requests"})

    async def test_migration_v48_recreates_combat_tables(self):
        await self.db.conn.execute("DROP TABLE combat_daily")
        await self.db.conn.execute("DROP TABLE spar_requests")
        await self.db.conn.execute("UPDATE db_info SET version = 47")
        await self.db.conn.commit()
        await self.data.MigrationManager(self.db.conn, self.config_manager).migrate()
        async with self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('combat_daily', 'spar_requests')"
        ) as cursor:
            names = {row[0] for row in await cursor.fetchall()}
        self.assertEqual(names, {"combat_daily", "spar_requests"})

    async def test_daily_pair_limit_blocks_second_duel(self):
        ok, blocked = await self.handlers._consume_daily_pair(
            ["1001", "1002"], "duel_count", 1
        )
        self.assertTrue(ok)
        self.assertIsNone(blocked)

        ok, blocked = await self.handlers._consume_daily_pair(
            ["1001", "1002"], "duel_count", 1
        )
        self.assertFalse(ok)
        self.assertIn(blocked, {"1001", "1002"})

        usage = await self.handlers._get_daily_usage("1001")
        self.assertEqual(usage["duel_count"], 1)

    async def test_spar_requires_acceptance_and_consumes_both(self):
        attacker_event = _FakeEvent("1001", "切磋 1002")
        await self._run(self.handlers.handle_spar(attacker_event, "1002"))

        self.assertTrue(any("切磋邀请" in text for text in attacker_event.results))
        self.assertFalse(any("切磋结束" in text for text in attacker_event.results))
        self.assertIsNotNone(await self.handlers._get_pending_spar_request("1002"))

        usage_before = await self.handlers._get_daily_usage("1001")
        self.assertEqual(usage_before["spar_count"], 0)

        acceptor_event = _FakeEvent("1002", "接受切磋")
        await self._run(self.handlers.handle_spar_accept(acceptor_event))

        self.assertTrue(any("切磋结束" in text for text in acceptor_event.results))
        result = self.get_event_flag(acceptor_event, "spar_result")
        self.assertIsNotNone(result)
        self.assertEqual(set(result.get("participants") or []), {"1001", "1002"})

        usage_a = await self.handlers._get_daily_usage("1001")
        usage_b = await self.handlers._get_daily_usage("1002")
        self.assertEqual(usage_a["spar_count"], 1)
        self.assertEqual(usage_b["spar_count"], 1)

        cd_a = await self.handlers._get_combat_cooldown("1001")
        cd_b = await self.handlers._get_combat_cooldown("1002")
        self.assertGreater(cd_a["last_spar_time"], 0)
        self.assertGreater(cd_b["last_spar_time"], 0)

    async def test_spar_accept_keeps_invite_when_initiator_busy(self):
        await self.handlers._create_spar_request("1001", "甲", "1002")
        await self.db.conn.execute(
            "UPDATE players SET state = '修炼中' WHERE user_id = ?", ("1001",)
        )
        await self.db.conn.commit()

        event = _FakeEvent("1002", "接受切磋")
        await self._run(self.handlers.handle_spar_accept(event))

        self.assertTrue(any("切磋无法进行" in text for text in event.results))
        # 校验失败不应消耗邀请，发起者空闲后仍可接受
        self.assertIsNotNone(await self.handlers._get_pending_spar_request("1002"))

    async def test_spar_accept_keeps_invite_when_initiator_out_of_limits(self):
        await self.handlers._create_spar_request("1001", "甲", "1002")
        spar_limit = self.handlers._daily_limit("spar_daily_limit", 3)
        await self.db.conn.execute(
            """INSERT INTO combat_daily (user_id, day, spar_count) VALUES (?, ?, ?)
               ON CONFLICT(user_id, day) DO UPDATE SET spar_count = ?""",
            ("1001", self.handlers._today(), spar_limit, spar_limit),
        )
        await self.db.conn.commit()

        event = _FakeEvent("1002", "接受切磋")
        await self._run(self.handlers.handle_spar_accept(event))

        self.assertTrue(any("今日切磋次数已用尽" in text for text in event.results))
        self.assertIsNotNone(await self.handlers._get_pending_spar_request("1002"))

        usage = await self.handlers._get_daily_usage("1002")
        self.assertEqual(usage["spar_count"], 0)

    async def test_daily_pair_savepoint_works_inside_open_transaction(self):
        # 模拟调用方已开启事务：旧实现 BEGIN IMMEDIATE 会直接报错
        await self.db.conn.execute(
            "INSERT INTO combat_daily (user_id, day, duel_count) VALUES (?, ?, 0)",
            ("1003", self.handlers._today()),
        )
        ok, blocked = await self.handlers._consume_daily_pair(
            ["1001", "1002"], "duel_count", 1
        )
        self.assertTrue(ok)
        self.assertIsNone(blocked)
        await self.db.conn.commit()

        for uid in ("1001", "1002"):
            usage = await self.handlers._get_daily_usage(uid)
            self.assertEqual(usage["duel_count"], 1)

    async def test_spar_request_expires(self):
        await self.handlers._create_spar_request("1001", "甲", "1002")
        await self.db.conn.execute(
            "UPDATE spar_requests SET expires_at = ? WHERE target_id = ?",
            (int(time.time()) - 1, "1002"),
        )
        await self.db.conn.commit()
        self.assertIsNone(await self.handlers._get_pending_spar_request("1002"))

    async def test_duel_handler_enforces_daily_limit(self):
        first_event = _FakeEvent("1001", "决斗 1002")
        await self._run(self.handlers.handle_duel(first_event, "1002"))
        self.assertTrue(any("决斗结束" in text for text in first_event.results))

        # 清掉冷却，只验证每日次数限制
        await self.db.conn.execute(
            "UPDATE combat_cooldowns SET last_duel_time = 0 WHERE user_id = ?",
            ("1001",),
        )
        await self.db.conn.commit()

        second_event = _FakeEvent("1001", "决斗 1002")
        await self._run(self.handlers.handle_duel(second_event, "1002"))
        self.assertTrue(any("今日决斗次数已用尽" in text for text in second_event.results))


if __name__ == "__main__":
    unittest.main()
