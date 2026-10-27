# tests/test_global_events.py
# 全服事件回归测试：天魔入侵/魔潮天灾互斥、结算、并发安全与联动
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
    def __init__(self, count=1):
        self.count = count

    def has_item(self, player, item_name, count=1):
        return self.count >= count

    async def retrieve_item(self, player, item_name, count=1):
        if self.count < count:
            return False, "数量不足"
        self.count -= count
        return True, "ok"


class TestGlobalEvents(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.GlobalEventManager = _imp("managers.global_event_manager").GlobalEventManager
        self.DemonInvasionManager = _imp("managers.demon_invasion_manager").DemonInvasionManager
        self.CalamityManager = _imp("managers.calamity_manager").CalamityManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        await self.db.ext.ensure_system_config_table()

        self.events = self.GlobalEventManager(self.db)
        self.demon = self.DemonInvasionManager(self.db, self.events)
        self.tide = self.CalamityManager(self.db, self.events)

    async def asyncTearDown(self):
        await self.db.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    async def _create_players(self, count, gold=0, experience=1000):
        for i in range(count):
            await self.db.create_player(
                self.Player(
                    user_id=f"u{i}",
                    user_name=f"道友{i}",
                    gold=gold,
                    experience=experience,
                    hp=100,
                )
            )

    async def test_events_are_mutually_exclusive(self):
        await self._create_players(12)

        ok, msg = await self.demon.start_invasion(24)
        self.assertTrue(ok, msg)
        self.assertEqual(await self.events.get_active_event(), self.events.EVENT_DEMON)

        ok, msg = await self.tide.start_tide(12)
        self.assertFalse(ok)
        self.assertIn("天魔入侵", msg)

        ok, msg = await self.demon.end_invasion()
        self.assertTrue(ok, msg)
        self.assertEqual(await self.events.get_active_event(), self.events.EVENT_NONE)

        ok, msg = await self.tide.start_tide(12)
        self.assertTrue(ok, msg)
        self.assertEqual(await self.events.get_active_event(), self.events.EVENT_TIDE)

        ok, msg = await self.demon.start_invasion(24)
        self.assertFalse(ok)
        self.assertIn("魔潮天灾", msg)

    async def test_demon_duration_is_clamped(self):
        await self._create_players(10)

        now = int(time.time())
        ok, msg = await self.demon.start_invasion(0)
        self.assertTrue(ok, msg)
        self.assertIn("1小时", msg)
        end_time = int(await self.db.ext.get_system_config("demon_invasion_end_time"))
        self.assertGreaterEqual(end_time, now + 3500)
        self.assertLessEqual(end_time, now + 3700)
        await self.demon.end_invasion()

        ok, msg = await self.demon.start_invasion(9999)
        self.assertTrue(ok, msg)
        self.assertIn("48小时", msg)
        end_time = int(await self.db.ext.get_system_config("demon_invasion_end_time"))
        self.assertGreaterEqual(end_time, now + 47 * 3600)

    async def test_last_demon_catch_settles_and_rewards_mortals(self):
        await self._create_players(10)
        ok, msg = await self.demon.start_invasion(24)
        self.assertTrue(ok, msg)

        demon_ids = json.loads(
            await self.db.ext.get_system_config("demon_invasion_list")
        )
        self.assertEqual(len(demon_ids), 1)
        demon_id = demon_ids[0]
        catcher_id = next(f"u{i}" for i in range(10) if f"u{i}" != demon_id)
        other_mortal_id = next(
            f"u{i}" for i in range(10)
            if f"u{i}" not in (demon_id, catcher_id)
        )

        catcher = await self.db.get_player_by_id(catcher_id)
        ok, msg = await self.demon.catch_demon(catcher, demon_id, FakeRing(1))
        self.assertTrue(ok, msg)
        self.assertIn("最后一名天魔", msg)

        self.assertEqual(await self.events.get_active_event(), self.events.EVENT_NONE)
        self.assertEqual(
            await self.db.ext.get_system_config("demon_invasion_active"), "0"
        )

        catcher_after = await self.db.get_player_by_id(catcher_id)
        other_after = await self.db.get_player_by_id(other_mortal_id)
        demon_after = await self.db.get_player_by_id(demon_id)
        self.assertEqual(catcher_after.gold, 35000)
        self.assertEqual(other_after.gold, 5000)
        self.assertEqual(demon_after.experience, 800)
        self.assertIn("所有天魔均已被揪出", self.demon.pop_last_event_msg())

    async def test_tide_resist_is_atomic_and_settles(self):
        await self._create_players(3, gold=1000000)
        await self.db.ext.set_system_config(
            self.tide.KEY_AFTERMATH, str(int(time.time()) + 3600)
        )

        ok, msg = await self.tide.start_tide(12)
        self.assertTrue(ok, msg)
        marker = await self.db.ext.get_system_config(
            f"tide_auto_{time.strftime('%Y%m%d')}"
        )
        self.assertEqual(marker, "1")

        player = await self.db.get_player_by_id("u0")
        ok, msg = await self.tide.resist(player, 10000)
        self.assertTrue(ok, msg)
        self.assertIn("捐献成功", msg)
        self.assertEqual(
            int(await self.db.ext.get_system_config(self.tide.KEY_PROGRESS)), 10000
        )

        player = await self.db.get_player_by_id("u0")
        ok, msg = await self.tide.resist(player, 40000)
        self.assertTrue(ok, msg)
        self.assertIn("魔潮被成功抵御", msg)

        after = await self.db.get_player_by_id("u0")
        self.assertEqual(after.gold, 960500)
        self.assertEqual(await self.events.get_active_event(), self.events.EVENT_NONE)
        self.assertEqual(await self.db.ext.get_system_config(self.tide.KEY_ACTIVE), "0")
        self.assertFalse(await self.tide.is_aftermath())

        bless_until = int(
            await self.db.ext.get_system_config("tide_bless_u0")
        )
        self.assertGreater(bless_until, int(time.time()))
        async with self.conn.execute(
            "SELECT COUNT(*) FROM system_config WHERE key LIKE 'tide_contrib_%'"
        ) as cursor:
            row = await cursor.fetchone()
        self.assertEqual(row[0], 0)

    async def test_tide_expiry_applies_aftermath_and_releases_event(self):
        await self._create_players(3, gold=1000000)
        ok, msg = await self.tide.start_tide(1)
        self.assertTrue(ok, msg)
        await self.db.ext.set_system_config(self.tide.KEY_END, str(int(time.time()) - 1))

        settled, msg = await self.tide.check_and_expire()
        self.assertTrue(settled)
        self.assertIn("魔潮得逞", msg)
        self.assertEqual(await self.events.get_active_event(), self.events.EVENT_NONE)
        self.assertTrue(await self.tide.is_aftermath())

    async def test_calendar_shows_saturday_today(self):
        from unittest import mock

        saturday = time.struct_time((2026, 10, 24, 10, 0, 0, 5, 297, 0))
        with mock.patch.object(time, "localtime", return_value=saturday):
            text = await self.tide.get_calendar()
        self.assertIn("周六魔潮（今天 20:00）", text)
        self.assertNotIn("还有 7 天", text)

    async def test_failed_start_does_not_mark_daily_auto_event(self):
        key = f"tide_auto_{time.strftime('%Y%m%d')}"
        ok, msg = await self.tide.start_tide(12)
        self.assertFalse(ok)
        self.assertIsNone(await self.db.ext.get_system_config(key))


if __name__ == "__main__":
    unittest.main()
