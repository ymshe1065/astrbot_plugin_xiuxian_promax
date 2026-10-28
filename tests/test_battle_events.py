import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

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


class FakeRing:
    def __init__(self):
        self.items = []

    async def store_item(self, player, item_name, count=1, silent=False, external_transaction=False):
        self.items.append((item_name, count))
        return True, ""


class TestBattleEvents(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.WorldBossManager = _imp("managers.world_boss_manager").WorldBossManager
        self.BattleEventManager = _imp("managers.battle_event_manager").BattleEventManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        self.ring = FakeRing()
        self.world_boss = self.WorldBossManager(self.db, None, self.ring)
        self.mgr = self.BattleEventManager(self.db, None, self.ring, self.world_boss)

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _create_player(self, user_id: str, level: int = 20, exp: int = 100000):
        player = self.Player(
            user_id=user_id,
            level_index=level,
            user_name=user_id,
            experience=exp,
            gold=0,
        )
        await self.db.create_player(player)
        return player

    def test_active_window_and_next_spawn(self):
        def ts(hour, minute=0, day=27):
            return int(datetime(2026, 10, day, hour, minute).timestamp())

        self.assertFalse(self.mgr.is_active_time(ts(9, 59)))
        self.assertTrue(self.mgr.is_active_time(ts(10, 0)))
        self.assertTrue(self.mgr.is_active_time(ts(23, 59)))
        self.assertFalse(self.mgr.is_active_time(ts(0, 30, day=28)))

        next_time = self.mgr.next_spawn_time(ts(11, 0))
        self.assertGreaterEqual(next_time, ts(14, 0))
        self.assertLessEqual(next_time, ts(15, 0))

        next_day = self.mgr.next_spawn_time(ts(23, 0))
        self.assertGreaterEqual(next_day, ts(10, 0, day=28))
        self.assertLessEqual(next_day, ts(10, 10, day=28))

    async def test_spawn_challenge_kill_and_idempotent_settlement(self):
        self.world_boss.calculate_dynamic_hp = AsyncMock(return_value=(100000, 1))
        player = await self._create_player("u1")
        ok, msg, event = await self.mgr.spawn_event(force_type="wanted", force_tier=1)
        self.assertTrue(ok, msg)
        self.assertEqual(event["event_key"], "wanted")

        ok, msg, _ = await self.mgr.spawn_event(force_type="beast", force_tier=1)
        self.assertFalse(ok)

        await self.conn.execute(
            "UPDATE battle_events SET current_hp = 1 WHERE event_id = ?", (event["event_id"],)
        )
        await self.conn.commit()
        self.world_boss.calculate_damage = AsyncMock(return_value=(10**9, 100.0, "火"))
        ok, msg = await self.mgr.challenge_event(player)
        self.assertTrue(ok, msg)
        self.assertIn("结算", msg)

        first_gold = (await self.db.get_player_by_id("u1")).gold
        self.assertGreater(first_gold, 0)
        self.assertTrue(self.ring.items)

        async with self.conn.execute(
            "SELECT status, settled FROM battle_events WHERE event_id = ?", (event["event_id"],)
        ) as cursor:
            row = await cursor.fetchone()
        self.assertEqual(row["status"], 2)
        self.assertEqual(row["settled"], 1)

        self.assertEqual(await self.mgr.settle_event(event["event_id"], "u1"), "")
        self.assertEqual(await self.mgr.settle_pending_events(), [])
        self.assertEqual((await self.db.get_player_by_id("u1")).gold, first_gold)

    async def test_expired_event_settles_with_partial_reward(self):
        self.world_boss.calculate_dynamic_hp = AsyncMock(return_value=(100000, 1))
        player = await self._create_player("u2")
        ok, msg, event = await self.mgr.spawn_event(force_type="demon", force_tier=2)
        self.assertTrue(ok, msg)
        self.world_boss.calculate_damage = AsyncMock(return_value=(100, 1.0, "水"))
        ok, msg = await self.mgr.challenge_event(player)
        self.assertTrue(ok, msg)
        self.assertFalse(self.mgr.last_kill)

        past = int(time.time()) - 1
        await self.conn.execute(
            "UPDATE battle_events SET expire_time = ? WHERE event_id = ?",
            (past, event["event_id"]),
        )
        await self.conn.commit()
        self.assertEqual(await self.mgr.expire_events(), 1)
        messages = await self.mgr.settle_pending_events()
        self.assertEqual(len(messages), 1)
        self.assertIn("逃逸", messages[0])
        self.assertGreater((await self.db.get_player_by_id("u2")).gold, 0)
        self.assertEqual(self.ring.items, [])


if __name__ == "__main__":
    unittest.main()
