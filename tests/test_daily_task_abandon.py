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
