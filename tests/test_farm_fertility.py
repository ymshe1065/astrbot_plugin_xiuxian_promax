# 灵田肥力分级 + 自动施肥代理
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


class _FakeStorage:
    def __init__(self, items=None):
        self.items = dict(items or {})

    def get_item_count(self, player, item_name):
        return int(self.items.get(item_name, 0))

    async def store_item(self, player, item_name, count=1, silent=False):
        self.items[item_name] = self.get_item_count(player, item_name) + count
        return True, "ok"


class TestFarmFertility(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.migration = migration
        self.farm_mod = _imp("managers.spirit_farm_manager")
        self.SpiritFarmManager = self.farm_mod.SpiritFarmManager
        self.AutoProxyManager = _imp("managers.auto_proxy_manager").AutoProxyManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)

        self.farm_mgr = self.SpiritFarmManager(self.db, _FakeStorage(), None)
        self.notices = []

        async def notifier(origin, msg):
            self.notices.append((origin, msg))

        self.manager = self.AutoProxyManager(
            self.db, None, None, notifier=notifier, farm_mgr=self.farm_mgr,
        )

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    # ===== 工具 =====

    async def _new_player(self, user_id="u1", gold=100000):
        await self.db.create_player(self.Player(user_id=user_id, level_index=5))
        await self.conn.execute(
            "UPDATE players SET gold = ? WHERE user_id = ?", (gold, user_id)
        )
        await self.conn.commit()
        return await self.db.get_player_by_id(user_id)

    async def _new_farm(self, user_id="u1", level=1, fertility=100):
        await self.conn.execute(
            "INSERT INTO spirit_farms (user_id, level, crops, fertility) VALUES (?, ?, '[]', ?)",
            (user_id, level, fertility),
        )
        await self.conn.commit()

    async def _set_farm(self, user_id="u1", level=None, fertility=None, crops=None):
        fields = []
        params = []
        if level is not None:
            fields.append("level = ?")
            params.append(level)
        if fertility is not None:
            fields.append("fertility = ?")
            params.append(fertility)
        if crops is not None:
            fields.append("crops = ?")
            params.append(json.dumps(crops, ensure_ascii=False))
        params.append(user_id)
        await self.conn.execute(
            f"UPDATE spirit_farms SET {', '.join(fields)} WHERE user_id = ?", params
        )
        await self.conn.commit()

    async def _fertility(self, user_id="u1"):
        farm = await self.farm_mgr.get_user_farm(user_id)
        return int(farm["fertility"])

    @staticmethod
    def _mature_crops(herb, count, start_slot=1, now=None):
        now = int(now or time.time())
        return [
            {
                "name": herb,
                "slot": start_slot + i,
                "plant_time": now - 7200,
                "mature_time": now - 1,
            }
            for i in range(count)
        ]

    # ===== 肥力上限分级 =====

    def test_max_fertility_by_level(self):
        expected = {1: 100, 2: 100, 3: 160, 4: 240, 5: 400}
        for level, ceiling in expected.items():
            self.assertEqual(self.farm_mgr.get_max_fertility(level), ceiling)
            self.assertEqual(self.farm_mgr.get_max_fertility({"level": level}), ceiling)
        self.assertEqual(self.farm_mgr.get_max_fertility(None), 100)
        self.assertEqual(self.farm_mgr.get_max_fertility({"level": 99}), 100)

    def test_gain_per_fertilize_is_30_percent_of_cap(self):
        self.assertEqual(self.farm_mgr.get_gain_per_fertilize(1), 30)
        self.assertEqual(self.farm_mgr.get_gain_per_fertilize(3), 48)
        self.assertEqual(self.farm_mgr.get_gain_per_fertilize(4), 72)
        self.assertEqual(self.farm_mgr.get_gain_per_fertilize(5), 120)

    # ===== 收获扣肥力 =====

    async def test_lv5_full_harvest_keeps_60_percent(self):
        player = await self._new_player()
        await self._new_farm(level=5, fertility=400)
        await self._set_farm(crops=self._mature_crops("冰心草", 20))

        ok, msg = await self.farm_mgr.harvest(player)
        self.assertTrue(ok, msg)
        self.assertEqual(await self._fertility(), 240)  # 400 - 8*20

    async def test_lv5_two_harvests_hit_floor_then_stop(self):
        player = await self._new_player()
        await self._new_farm(level=5, fertility=400)
        await self._set_farm(crops=self._mature_crops("冰心草", 20))
        ok, _ = await self.farm_mgr.harvest(player)
        self.assertTrue(ok)
        self.assertEqual(await self._fertility(), 240)

        await self._set_farm(crops=self._mature_crops("冰心草", 20))
        ok, _ = await self.farm_mgr.harvest(player)
        self.assertTrue(ok)
        self.assertEqual(await self._fertility(), 80)  # 地板 = 400*0.2

        await self._set_farm(crops=self._mature_crops("冰心草", 20))
        ok, _ = await self.farm_mgr.harvest(player)
        self.assertTrue(ok)
        self.assertEqual(await self._fertility(), 80)  # 不再下降

    async def test_lv1_one_crop_costs_8_like_before(self):
        player = await self._new_player()
        await self._new_farm(level=1, fertility=100)
        await self._set_farm(crops=self._mature_crops("灵草", 1))

        ok, _ = await self.farm_mgr.harvest(player)
        self.assertTrue(ok)
        self.assertEqual(await self._fertility(), 92)

    # ===== 收益系数 =====

    async def test_yield_scales_with_fertility_ratio(self):
        player = await self._new_player(gold=0)
        await self._new_farm(level=5, fertility=400)
        await self._set_farm(crops=self._mature_crops("冰心草", 10))
        await self.farm_mgr.harvest(player)
        gained_full = (await self.db.get_player_by_id("u1")).gold
        self.assertGreater(gained_full, 0)

        await self._set_farm(fertility=200, crops=self._mature_crops("冰心草", 10))
        full = await self.db.get_player_by_id("u1")
        await self.farm_mgr.harvest(full)
        gained_half = (await self.db.get_player_by_id("u1")).gold - gained_full

        self.assertGreater(gained_half, 0)
        self.assertAlmostEqual(gained_half / gained_full, 0.5, places=1)

    # ===== 施肥 =====

    async def test_fertilize_lv5_restores_120(self):
        player = await self._new_player()
        await self._new_farm(level=5, fertility=240)
        ok, msg = await self.farm_mgr.fertilize(player)
        self.assertTrue(ok, msg)
        self.assertEqual(await self._fertility(), 360)
        fresh = await self.db.get_player_by_id("u1")
        self.assertEqual(fresh.gold, 98000)

    async def test_fertilize_lv1_clamped_at_cap(self):
        player = await self._new_player()
        await self._new_farm(level=1, fertility=92)
        ok, msg = await self.farm_mgr.fertilize(player)
        self.assertTrue(ok, msg)
        self.assertEqual(await self._fertility(), 100)

        ok, msg = await self.farm_mgr.fertilize(player)
        self.assertFalse(ok)
        self.assertIn("无需施肥", msg)

    async def test_fertilize_once_returns_count_and_stops_at_cap(self):
        player = await self._new_player()
        await self._new_farm(level=5, fertility=240)
        ok, msg, count = await self.farm_mgr.fertilize_once(player, 4)
        self.assertTrue(ok, msg)
        self.assertEqual(count, 2)  # 240 → 360 → 400
        self.assertIn("240→400", msg)
        self.assertEqual(await self._fertility(), 400)

    # ===== 迁移 =====

    async def test_migration_scales_fertility_by_percentage(self):
        rows = [
            ("a", 5, 100),
            ("b", 5, 50),
            ("c", 5, 20),
            ("d", 3, 100),
            ("e", 1, 50),
            ("f", 2, 100),
        ]
        for user_id, level, fertility in rows:
            await self.conn.execute(
                "INSERT INTO spirit_farms (user_id, level, crops, fertility) VALUES (?, ?, '[]', ?)",
                (user_id, level, fertility),
            )
        await self.conn.commit()

        await self.migration._migrate_to_v54(self.conn, None)

        expected = {"a": 400, "b": 200, "c": 80, "d": 160, "e": 50, "f": 100}
        for user_id, want in expected.items():
            cursor = await self.conn.execute(
                "SELECT fertility FROM spirit_farms WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            self.assertEqual(int(row["fertility"]), want, user_id)

    # ===== 自动施肥代理 =====

    async def test_enable_fert_parses_threshold(self):
        await self._new_player()
        await self._new_farm(level=5, fertility=400)

        ok, msg = await self.manager.enable("u1", "umo://g", "施肥 70")
        self.assertTrue(ok, msg)
        row = await self.db.ext.get_auto_proxy("u1")
        config = self.manager._load_config(row)
        self.assertTrue(config["fertilize"]["enabled"])
        self.assertAlmostEqual(config["fertilize"]["threshold"], 0.7)
        self.assertEqual(row["enabled"], 1)
        self.assertFalse(config["farm"]["enabled"])

        ok, msg = await self.manager.enable("u1", "umo://g", "施肥 5")
        self.assertFalse(ok)
        self.assertIn("10-100", msg)

    async def test_fert_cycle_skips_when_roi_below_cost(self):
        player = await self._new_player()
        await self._new_farm(level=5, fertility=100)
        await self._set_farm(crops=self._mature_crops("灵草", 20))  # Σgold 2000 → roi 0.3
        ok, _ = await self.manager.enable("u1", "umo://g", "施肥 90")
        self.assertTrue(ok)
        self.assertEqual(await self._fertility(), 100)
        fresh = await self.db.get_player_by_id("u1")
        self.assertEqual(fresh.gold, player.gold)

    async def test_fert_cycle_runs_before_harvest(self):
        # 对照组：只开灵田代理，肥力 60% 收获
        control = await self._new_player("u1", gold=100000)
        await self._new_farm("u1", level=5, fertility=240)
        await self._set_farm(
            "u1", crops=self._mature_crops("冰心草", 10)
        )
        await self.conn.execute(
            "INSERT INTO auto_proxy (user_id, enabled, task_type, origin, config_json) "
            "VALUES (?, 1, '', 'umo://g', ?)",
            ("u1", json.dumps({"farm": {"enabled": True}})),
        )
        await self.conn.commit()
        gold_before = control.gold
        await self.manager.process_due()
        control_after = await self.db.get_player_by_id("u1")
        control_gain = control_after.gold - gold_before
        self.assertEqual(await self._fertility("u1"), 240 - 80)

        # 实验组：灵田 + 施肥，先补肥再收获
        treat = await self._new_player("u2", gold=100000)
        await self._new_farm("u2", level=5, fertility=240)
        await self._set_farm(
            "u2", crops=self._mature_crops("冰心草", 10)
        )
        await self.conn.execute(
            "INSERT INTO auto_proxy (user_id, enabled, task_type, origin, config_json) "
            "VALUES (?, 1, '', 'umo://g', ?)",
            (
                "u2",
                json.dumps(
                    {
                        "farm": {"enabled": True},
                        "fertilize": {"enabled": True, "threshold": 0.85},
                    }
                ),
            ),
        )
        await self.conn.commit()
        treat_before = treat.gold
        await self.manager.process_due()
        treat_after = await self.db.get_player_by_id("u2")
        # 施肥花 2 次 4000，收货后按 400/400 结算，再扣肥 80
        treat_gain = treat_after.gold - treat_before + 4000
        self.assertGreater(control_gain, 0)
        self.assertGreater(treat_gain, control_gain * 1.5)
        self.assertEqual(await self._fertility("u2"), 320)

    async def test_close_primary_keeps_fert_running(self):
        await self._new_player()
        await self._new_farm(level=5, fertility=400)
        ok, _ = await self.manager.enable("u1", "umo://g", "施肥")
        self.assertTrue(ok)

        ok, msg = await self.manager.disable_primary("u1")
        self.assertTrue(ok)
        self.assertIn("施肥", msg)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)
        self.assertTrue(self.manager._load_config(row)["fertilize"]["enabled"])

        ok, msg = await self.manager.disable_fert("u1")
        self.assertTrue(ok)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 0)
        self.assertFalse(self.manager._load_config(row)["fertilize"]["enabled"])

    async def test_disable_all_stops_fert(self):
        await self._new_player()
        await self._new_farm(level=5, fertility=400)
        await self.manager.enable("u1", "umo://g", "施肥")
        await self.manager.disable("u1", "manual_close")
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 0)
        self.assertFalse(self.manager._load_config(row)["fertilize"]["enabled"])

    async def test_status_shows_fert_detail(self):
        await self._new_player()
        await self._new_farm(level=5, fertility=240)
        await self._set_farm(crops=self._mature_crops("冰心草", 10))
        await self.manager.enable("u1", "umo://g", "施肥 70")
        text = await self.manager.status_text("u1")
        self.assertIn("施肥代理：运行中", text)
        self.assertIn("阈值 70%", text)

    async def test_fertilize_insufficient_gold_notifies_once(self):
        player = await self._new_player(gold=500)
        await self._new_farm(level=5, fertility=240)
        await self._set_farm(crops=self._mature_crops("冰心草", 10))
        await self.conn.execute(
            "INSERT INTO auto_proxy (user_id, enabled, task_type, origin, config_json) "
            "VALUES (?, 1, '', 'umo://g', ?)",
            (
                "u1",
                json.dumps(
                    {"fertilize": {"enabled": True, "threshold": 0.85}}
                ),
            ),
        )
        await self.conn.commit()
        self.notices.clear()

        await self.manager.process_due()
        gold_msgs = [m for _, m in self.notices if "灵石不足" in m]
        self.assertEqual(len(gold_msgs), 1)
        self.assertEqual(await self._fertility(), 240)
        self.assertEqual((await self.db.get_player_by_id("u1")).gold, player.gold)

        # 第二次不再重复通知
        await self.manager.process_due()
        gold_msgs = [m for _, m in self.notices if "灵石不足" in m]
        self.assertEqual(len(gold_msgs), 1)


if __name__ == "__main__":
    unittest.main()
