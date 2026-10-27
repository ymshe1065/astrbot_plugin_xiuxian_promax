# tests/test_sect_fixes.py
# 宗门状态冲突、踢人失效与战报展示回归测试
import json
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


class TestSectFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.SectManager = _imp("managers.sect_manager").SectManager
        self.Sect = _imp("models_extended").Sect
        self.UserStatus = _imp("models_extended").UserStatus
        self.Player = _imp("models").Player

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        await self.db.create_player(
            self.Player(user_id="u1", user_name="宗主", level_index=5, gold=100_000)
        )
        await self.db.create_player(
            self.Player(user_id="u2", user_name="弟子", level_index=5, gold=100_000)
        )
        self.mgr = self.SectManager(self.db, None)

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    async def _join(self):
        ok, msg = await self.mgr.create_sect("u1", "测试宗")
        self.assertTrue(ok, msg)
        ok, msg = await self.mgr.join_sect("u2", "测试宗")
        self.assertTrue(ok, msg)

    async def test_perform_task_rejects_other_busy_state(self):
        await self._join()
        await self.db.ext.set_user_busy("u1", self.UserStatus.CULTIVATING, 0)
        ok, msg = await self.mgr.perform_sect_task("u1")
        self.assertFalse(ok)
        self.assertIn("无法执行宗门任务", msg)
        cd = await self.db.ext.get_user_cd("u1")
        self.assertEqual(cd.type, self.UserStatus.CULTIVATING)

    async def test_finish_task_does_not_clear_other_busy_state(self):
        await self._join()
        await self.db.ext.set_user_busy("u1", self.UserStatus.DELIVERING, 0)
        ok, msg = await self.mgr.finish_sect_task("u1")
        self.assertFalse(ok)
        self.assertIn("不在宗门任务中", msg)
        cd = await self.db.ext.get_user_cd("u1")
        self.assertEqual(cd.type, self.UserStatus.DELIVERING)

    async def test_kick_member_actually_removes_target(self):
        await self._join()
        await self.db.ext.set_user_busy("u2", self.UserStatus.SECT_TASK, 0)
        ok, msg = await self.mgr.kick_member("u1", "u2")
        self.assertTrue(ok, msg)

        target = await self.db.get_player_by_id("u2")
        self.assertEqual(target.sect_id, 0)
        self.assertEqual(target.sect_position, 0)
        self.assertEqual(target.sect_contribution, 0)
        self.assertEqual(target.state, "空闲")
        cd = await self.db.ext.get_user_cd("u2")
        self.assertEqual(cd.type, self.UserStatus.IDLE)

    async def test_building_upgrade_uses_materials_discount(self):
        await self._join()
        sect = await self.db.ext.get_sect_by_name("测试宗")
        await self.conn.execute(
            "UPDATE sects SET sect_used_stone = 10000, sect_materials = 1000 WHERE sect_id = ?",
            (sect.sect_id,),
        )
        await self.conn.commit()

        ok, msg = await self.mgr.upgrade_building("u1", "藏经阁")
        self.assertTrue(ok, msg)
        sect = await self.db.ext.get_sect_by_name("测试宗")
        self.assertEqual(sect.sect_materials, 0)
        self.assertEqual(sect.sect_used_stone, 6000)
        self.assertEqual(self.mgr.get_building_level(sect, "藏经阁"), 1)

    async def test_owner_cascade_cleans_sect_related_rows(self):
        await self._join()
        sect = await self.db.ext.get_sect_by_name("测试宗")
        await self.conn.execute(
            "UPDATE players SET sect_id = 0, sect_position = 0 WHERE user_id = 'u2'"
        )
        await self.conn.execute(
            "INSERT INTO sect_orders (sect_id, issuer_id, target_id, order_type, target_value) "
            "VALUES (?, 'u1', 'u2', 'donate', 100)",
            (sect.sect_id,),
        )
        await self.conn.execute(
            "INSERT INTO sect_wars (attacker_id, defender_id, winner_id) VALUES (?, 999, ?)",
            (sect.sect_id, sect.sect_id),
        )
        await self.conn.commit()

        await self.db.delete_player_cascade("u1")

        self.assertIsNone(await self.db.ext.get_sect_by_id(sect.sect_id))
        async with self.conn.execute(
            "SELECT COUNT(*) FROM sect_orders WHERE sect_id = ?", (sect.sect_id,)
        ) as cursor:
            self.assertEqual((await cursor.fetchone())[0], 0)
        async with self.conn.execute(
            "SELECT COUNT(*) FROM sect_wars WHERE attacker_id = ?", (sect.sect_id,)
        ) as cursor:
            self.assertEqual((await cursor.fetchone())[0], 0)

    async def test_war_report_parses_log(self):
        await self._join()
        sect = await self.db.ext.get_sect_by_name("测试宗")
        now = int(time.time())
        await self.conn.execute(
            "INSERT INTO sect_wars "
            "(attacker_id, defender_id, winner_id, gold_taken, log_json, created_at, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sect.sect_id, 999, sect.sect_id, 1234,
                json.dumps(["宗主 vs 弟子 → 宗主"], ensure_ascii=False),
                now, "umo://group",
            ),
        )
        await self.conn.commit()
        ok, msg = await self.mgr.get_war_report("u1")
        self.assertTrue(ok, msg)
        self.assertIn("测试宗", msg)
        self.assertIn("宗主 vs 弟子 → 宗主", msg)
        self.assertIn("1,234", msg)


if __name__ == "__main__":
    unittest.main()
