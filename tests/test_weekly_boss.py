# tests/test_weekly_boss.py
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

    def exception(self, *a, **k):
        pass

    def debug(self, *a, **k):
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


class TestWeeklyBoss(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        ConfigManager = _imp("config_manager").ConfigManager
        self.Player = _imp("models").Player
        self.WeeklyBossManager = _imp("managers.weekly_boss_manager").WeeklyBossManager
        self.RecoveryManager = _imp("managers.recovery_manager").RecoveryManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)
        await self.conn.commit()

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        self.config_manager = ConfigManager(ROOT)
        self.ring = _FakeRing()
        self.manager = self.WeeklyBossManager(
            self.db, self.config_manager, self.ring
        )
        self.migration = migration

    async def asyncTearDown(self):
        await self.db.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    async def _create_player(self, user_id: str, level: int = 22, pills: dict = None):
        player = self.Player(
            user_id=user_id,
            level_index=level,
            user_name=user_id,
            experience=100,
            gold=0,
            cultivation_type="灵修",
        )
        player.set_pills_inventory(pills or {})
        await self.db.create_player(player)
        return player

    async def test_migration_creates_tables(self):
        async with self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'weekly_boss%'"
        ) as cursor:
            names = {row[0] for row in await cursor.fetchall()}
        self.assertEqual(
            names,
            {
                "weekly_boss_cycles",
                "weekly_boss_runs",
                "weekly_boss_members",
                "weekly_boss_claims",
                "weekly_boss_leaderboard",
                "weekly_boss_attempts",
            },
        )

    async def test_cycle_is_stable(self):
        first = await self.manager.ensure_current_cycle()
        second = await self.manager.ensure_current_cycle()
        self.assertEqual(first["cycle_id"], second["cycle_id"])
        self.assertEqual(first["boss_name"], second["boss_name"])
        self.assertGreater(int(first["end_time"]), int(first["start_time"]))

    async def test_team_create_join_leave(self):
        leader = await self._create_player("leader")
        member = await self._create_player("member")

        ok, msg = await self.manager.create_team(leader, "测试队")
        self.assertTrue(ok, msg)
        cycle = await self.manager.ensure_current_cycle()
        run = await self.manager._find_member_run(cycle["cycle_id"], "leader", 0)
        self.assertIsNotNone(run)
        rid = run["run_id"]

        ok, msg = await self.manager.join_team(member, str(rid))
        self.assertTrue(ok, msg)
        self.assertEqual(len(await self.manager._run_members(rid)), 2)

        ok, msg = await self.manager.leave_team(member)
        self.assertTrue(ok, msg)
        self.assertEqual(len(await self.manager._run_members(rid)), 1)

        ok, msg = await self.manager.leave_team(leader)
        self.assertTrue(ok, msg)
        self.assertIsNone(
            await self.manager._find_member_run(cycle["cycle_id"], "leader", 0)
        )

    async def test_boss_hp_scales_with_members(self):
        level = 22
        hp1 = self.manager._boss_max_hp(level, 1)
        hp2 = self.manager._boss_max_hp(level, 2)
        hp4 = self.manager._boss_max_hp(level, 4)
        self.assertGreater(hp1, 0)
        self.assertLess(hp1, hp2)
        self.assertLess(hp2, hp4)

    async def test_loadout_recommend_and_inventory_check(self):
        player = await self._create_player(
            "pillman",
            pills={
                "回春散·天|一般": 4,
                "狂暴丹|一般": 2,
            },
        )
        ok, msg = await self.manager.create_team(player, "药队")
        self.assertTrue(ok, msg)

        ok, msg = await self.manager.set_loadout(player, "推荐")
        self.assertTrue(ok, msg)
        loadout = self.manager._recommend_loadout(player)
        self.assertTrue(loadout["prepare"])
        self.assertTrue(loadout["combat"])

        ok, msg = await self.manager.set_loadout(player, "回春散·天*5")
        self.assertFalse(ok)
        self.assertIn("库存不足", msg)

    async def test_full_battle_settles_and_deducts_pills(self):
        player = await self._create_player(
            "solo",
            pills={
                "回春散·天|一般": 6,
                "狂暴丹|一般": 3,
            },
        )
        ok, msg = await self.manager.create_team(player, "独行队")
        self.assertTrue(ok, msg)
        ok, msg = await self.manager.set_loadout(player, "回春散·天*6 狂暴丹*3")
        self.assertTrue(ok, msg)

        ok, msg = await self.manager.prepare_battle(player)
        self.assertTrue(ok, msg)
        self.assertIn("确认开战", msg)

        ok, msg = await self.manager.confirm_battle(player)
        self.assertTrue(ok, msg)
        self.assertTrue("周本通关" in msg or "周本团灭" in msg)

        cycle = await self.manager.ensure_current_cycle()
        used = await self.manager._cycle_attempts_used(cycle["cycle_id"], "solo")
        self.assertEqual(used, 1)

        player_after = await self.db.get_player_by_id("solo")
        inventory = player_after.get_pills_inventory()
        # 战前 3 枚狂暴丹必定扣除
        self.assertEqual(int(inventory.get("狂暴丹|一般", 0) or 0), 0)
        # 战中恢复丹未使用时不消耗，使用过则减少
        remaining = int(inventory.get("回春散·天|一般", 0) or 0)
        self.assertGreaterEqual(remaining, 0)
        self.assertLessEqual(remaining, 6)

        rows = await self.manager._fetchall(
            "SELECT * FROM weekly_boss_leaderboard WHERE cycle_id = ?",
            (cycle["cycle_id"],),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], "solo")

    async def test_hp_feedback_writes_back_and_can_injure(self):
        player = await self._create_player("hurt")
        await self.db.update_player_fields("hurt", hp=1000)
        player.hp = 1000

        runtimes = [
            {
                "user_id": "hurt",
                "name": "hurt",
                "max_hp": 1000,
                "hp": 0,
                "real_hp": 1000,
                "real_max_hp": 1000,
            }
        ]
        results = await self.manager._write_back_real_hp(
            runtimes, {"hurt": player}
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["hp"], 1)
        self.assertTrue(results[0]["injured"])

        reloaded = await self.db.get_player_by_id("hurt")
        self.assertEqual(int(reloaded.hp), 1)
        recovery = self.RecoveryManager(self.db, self.config_manager)
        self.assertTrue(await recovery.is_injured(reloaded))

    async def test_hp_feedback_never_heals_and_partial_loss_is_not_injury(self):
        low = await self._create_player("low_hp")
        await self.db.update_player_fields("low_hp", hp=400)
        low.hp = 400
        healthy = await self._create_player("healthy")
        await self.db.update_player_fields("healthy", hp=1000)
        healthy.hp = 1000

        runtimes = [
            {
                "user_id": "low_hp",
                "name": "low_hp",
                "max_hp": 1000,
                "hp": 900,
                "real_hp": 400,
                "real_max_hp": 1000,
            },
            {
                "user_id": "healthy",
                "name": "healthy",
                "max_hp": 1000,
                "hp": 800,
                "real_hp": 1000,
                "real_max_hp": 1000,
            },
        ]
        results = {
            item["user_id"]: item
            for item in await self.manager._write_back_real_hp(
                runtimes, {"low_hp": low, "healthy": healthy}
            )
        }
        # 已在低血量时不会因周本回血，只按损耗继续扣减
        self.assertEqual(results["low_hp"]["hp"], 300)
        self.assertEqual(results["healthy"]["hp"], 800)
        self.assertFalse(results["healthy"]["injured"])

    async def test_single_member_penalty_warning(self):
        player = await self._create_player("solo2", pills={"回春散·天|一般": 6})
        await self.manager.create_team(player, "单人")
        ok, msg = await self.manager.prepare_battle(player)
        self.assertTrue(ok, msg)
        self.assertIn("单人挑战", msg)

    async def test_refresh_and_reset(self):
        player = await self._create_player("admin_target")
        cycle = await self.manager.ensure_current_cycle()

        ok, msg = await self.manager.refresh_cycle()
        self.assertTrue(ok, msg)
        self.assertIn("周本已刷新", msg)

        ok, msg = await self.manager.reset_player_cd("admin_target")
        self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main()
