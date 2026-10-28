# 洞天批量升级：次数 / 目标等级 / 一键满级
import os
import sys
import tempfile
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


class TestBlessedLandUpgrade(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        module = _imp("managers.blessed_land_manager")
        self.parse_upgrade_args = module.parse_upgrade_args
        self.BlessedLandManager = module.BlessedLandManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        await self.db.create_player(self.Player(user_id="u1", level_index=5))

        self.mgr = self.BlessedLandManager(self.db, None)
        await self.conn.execute(
            "INSERT INTO blessed_lands (user_id, land_type, land_name, level, exp_bonus, gold_per_hour) "
            "VALUES (?, 1, '小洞天', 1, 0.055, 115)",
            ("u1",),
        )
        await self.conn.commit()

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _level(self):
        async with self.conn.execute(
            "SELECT level FROM blessed_lands WHERE user_id = ?", ("u1",)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0])

    async def test_parse_upgrade_args(self):
        self.assertEqual(self.parse_upgrade_args(""), ("times", 1))
        self.assertEqual(self.parse_upgrade_args("3"), ("times", 3))
        self.assertEqual(self.parse_upgrade_args("1-5"), ("target", 5))
        self.assertEqual(self.parse_upgrade_args("5-1"), ("target", 5))
        self.assertEqual(self.parse_upgrade_args("all"), ("all", 0))
        self.assertEqual(self.parse_upgrade_args("全部"), ("all", 0))
        self.assertEqual(self.parse_upgrade_args("abc"), (None, None))

    async def test_upgrade_multiple_times(self):
        player = await self.db.get_player_by_id("u1")
        player.gold = 10000
        await self.db.update_player(player)

        ok, msg = await self.mgr.upgrade_blessed_land(player, times=3)
        self.assertTrue(ok, msg)
        self.assertEqual(await self._level(), 4)
        self.assertEqual(player.gold, 7000)
        self.assertIn("连升 3 级", msg)

    async def test_upgrade_all_stops_when_gold_runs_out(self):
        player = await self.db.get_player_by_id("u1")
        player.gold = 2500
        await self.db.update_player(player)

        ok, msg = await self.mgr.upgrade_blessed_land(player, fill_all=True)
        self.assertTrue(ok, msg)
        self.assertEqual(await self._level(), 3)
        self.assertEqual(player.gold, 500)
        self.assertIn("灵石不足", msg)

    async def test_upgrade_to_target_level(self):
        player = await self.db.get_player_by_id("u1")
        player.gold = 10000
        await self.db.update_player(player)

        ok, msg = await self.mgr.upgrade_blessed_land(player, target_level=5)
        self.assertTrue(ok, msg)
        self.assertEqual(await self._level(), 5)
        self.assertEqual(player.gold, 6000)
        self.assertIn("最高等级", msg)

    async def test_upgrade_invalid_target_is_rejected(self):
        player = await self.db.get_player_by_id("u1")
        player.gold = 10000
        await self.db.update_player(player)

        ok, msg = await self.mgr.upgrade_blessed_land(player, target_level=1)
        self.assertFalse(ok)
        self.assertIn("需高于当前", msg)
        self.assertEqual(await self._level(), 1)


if __name__ == "__main__":
    unittest.main()
