# tests/test_world_boss_expiry.py
# 世界Boss过期清理回归测试：过期Boss必须落库为逃遁，且不能阻塞新Boss生成
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


class TestWorldBossExpiry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.WorldBossManager = _imp("managers.world_boss_manager").WorldBossManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        self.manager = self.WorldBossManager(self.db, None, None)

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _insert_boss(self, expire_time: int, status: int = 1):
        await self.conn.execute(
            "INSERT INTO world_boss "
            "(boss_name, boss_level, max_hp, current_hp, atk, defense, spawn_time, expire_time, status) "
            "VALUES ('测试Boss', 2, 100000, 100000, 100, 50, ?, ?, ?)",
            (expire_time - 86400, expire_time, status),
        )
        await self.conn.commit()

    async def _bosses(self):
        async with self.conn.execute(
            "SELECT boss_id, status, expire_time FROM world_boss ORDER BY boss_id"
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def test_expired_bosses_are_marked_escaped(self):
        now = int(time.time())
        await self._insert_boss(now - 10)
        await self._insert_boss(now + 3600)
        await self._insert_boss(now - 10, status=2)

        changed = await self.manager.expire_bosses(now)
        self.assertEqual(changed, 1)
        statuses = [b["status"] for b in await self._bosses()]
        self.assertEqual(statuses, [3, 1, 2])

    async def test_expired_boss_does_not_block_spawn(self):
        now = int(time.time())
        await self._insert_boss(now - 10)

        ok, msg = await self.manager.spawn_boss()
        self.assertTrue(ok, msg)

        bosses = await self._bosses()
        self.assertEqual(bosses[0]["status"], 3)
        alive = [b for b in bosses if b["status"] == 1]
        self.assertEqual(len(alive), 1)
        self.assertGreater(alive[0]["expire_time"], int(time.time()))


if __name__ == "__main__":
    unittest.main()
