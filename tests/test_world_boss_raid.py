# tests/test_world_boss_raid.py
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


class _FakeRing:
    def __init__(self):
        self.items = []

    async def store_item(self, player, item_name, count=1, silent=False, external_transaction=False):
        self.items.append((item_name, count))
        return True, ""

    async def grant_item(self, player, item_name, count=1, **kwargs):
        return await self.store_item(player, item_name, count, **kwargs)


class TestWorldBossRaid(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.WorldBossManager = _imp("managers.world_boss_manager").WorldBossManager
        self.RaidManager = _imp("managers.raid_manager").RaidManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        self.ring = _FakeRing()
        self.world_boss = self.WorldBossManager(self.db, None, self.ring)
        self.raid = self.RaidManager(self.db, None, self.ring, self.world_boss)

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _create_player(self, user_id: str, level: int = 10, exp: int = 100000):
        player = self.Player(
            user_id=user_id,
            level_index=level,
            user_name=user_id,
            experience=exp,
            gold=0,
        )
        await self.db.create_player(player)
        return player

    async def test_world_boss_type_and_idempotent_settlement(self):
        ok, msg = await self.world_boss.spawn_boss()
        self.assertTrue(ok, msg)
        boss = await self.world_boss.get_active_boss()
        self.assertTrue(boss["boss_type"])
        self.assertIn("element", boss)

        player = await self._create_player("u1", level=20)
        await self.conn.execute("UPDATE world_boss SET current_hp = 1 WHERE boss_id = ?", (boss["boss_id"],))
        await self.conn.commit()
        self.world_boss.cooldown = 0
        ok, msg = await self.world_boss.challenge_boss(player)
        self.assertTrue(ok, msg)
        self.assertIn("击杀", msg)
        self.assertIn("世界Boss战斗结果", msg)
        self.assertNotIn("第 1 回合", msg)

        first_gold = (await self.db.get_player_by_id("u1")).gold
        self.assertGreater(first_gold, 0)
        again = await self.world_boss.settle_boss_kill(boss["boss_id"], "u1")
        self.assertEqual(again, "")
        second_gold = (await self.db.get_player_by_id("u1")).gold
        self.assertEqual(first_gold, second_gold)
        self.assertEqual(await self.world_boss.settle_pending_bosses(), [])

    async def test_raid_phase_clear_and_settlement(self):
        await self._create_player("u1")
        await self._create_player("u2", exp=90000)
        ok, msg = await self.raid.spawn_raid(difficulty=1)
        self.assertTrue(ok, msg)
        raid = await self.raid.get_active_raid()
        self.assertIsNotNone(raid)
        self.assertEqual(raid["total_phases"], 2)

        ok, msg = await self.raid.spawn_raid(difficulty=1)
        self.assertFalse(ok)

        self.raid.cooldown = 0
        self.raid.damage_cap_pct = 100.0
        # 该用例验证阶段结算，不依赖旧版“囤修为直接增伤”的数值。
        from unittest.mock import AsyncMock
        self.world_boss.calculate_damage = AsyncMock(return_value=(10**12, 100.0, "火"))
        p1 = await self.db.get_player_by_id("u1")
        ok, msg = await self.raid.challenge_raid(p1)
        self.assertTrue(ok, msg)
        self.assertIn("阶段", msg)
        p2 = await self.db.get_player_by_id("u2")
        ok, msg = await self.raid.challenge_raid(p2)
        self.assertTrue(ok, msg)
        self.assertIn("通关", msg)

        raid_id = raid["raid_id"]
        async with self.conn.execute("SELECT * FROM world_boss_raids WHERE raid_id = ?", (raid_id,)) as cursor:
            row = await cursor.fetchone()
        self.assertEqual(row["status"], 4)
        self.assertEqual(row["settled"], 1)
        self.assertGreater((await self.db.get_player_by_id("u1")).gold, 0)
        self.assertTrue(self.ring.items)
        self.assertEqual(await self.raid.settle_raid(raid_id), "")
        self.assertEqual(await self.raid.settle_pending_raids(), [])


if __name__ == "__main__":
    unittest.main()
