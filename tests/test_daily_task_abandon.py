# tests/test_daily_task_abandon.py
# 日常任务放弃回归测试：不扣灵石/灵气，但仍占用当日接取次数
import os
import sys
import tempfile
import time
import json
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestDailyTaskAbandon(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.DailyTaskManager = _imp("managers.daily_task_manager").DailyTaskManager
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
        await self.db.create_player(self.Player(user_id="u1", gold=1000))
        self.manager = self.DailyTaskManager(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def test_abandon_does_not_charge_and_keeps_daily_count(self):
        now = int(time.time())
        today_end = int(time.mktime(time.localtime(now)[:3] + (23, 59, 59, 0, 0, 0)))
        rewards = json.dumps({"exp": 100, "gold": 200, "materials": []})
        task_id = await self.db.ext.create_daily_task(
            "u1", "黄", "每日签到", "check_in", 1, rewards, today_end
        )

        before_count = await self.db.ext.count_daily_tasks_today("u1", "黄", today_end)
        ok, msg = await self.manager.abandon_task(await self.db.get_player_by_id("u1"), task_id)

        self.assertTrue(ok, msg)
        player = await self.db.get_player_by_id("u1")
        self.assertEqual(player.gold, 1000)
        self.assertEqual(player.spiritual_qi, 100)
        task = await self.db.ext.get_daily_task(task_id, "u1")
        self.assertEqual(task["status"], 4)
        after_count = await self.db.ext.count_daily_tasks_today("u1", "黄", today_end)
        self.assertEqual(after_count, before_count)


if __name__ == "__main__":
    unittest.main()
