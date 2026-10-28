# 自动代理灵田：指定格、自动收获续种、取消指定格、铲除联动
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

    def has_item(self, player, item_name, count=1):
        return self.get_item_count(player, item_name) >= count

    async def retrieve_item(self, player, item_name, count=1):
        have = self.get_item_count(player, item_name)
        if have < count:
            return False, f"数量不足（当前：{have}个）"
        left = have - count
        if left:
            self.items[item_name] = left
        else:
            self.items.pop(item_name, None)
        return True, "ok"

    async def store_item(self, player, item_name, count=1, silent=False):
        self.items[item_name] = self.get_item_count(player, item_name) + count
        return True, "ok"


class TestAutoProxyFarm(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.SpiritFarmManager = _imp("managers.spirit_farm_manager").SpiritFarmManager
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
        await self.db.create_player(self.Player(user_id="u1", level_index=5))

        self.farm_mgr = self.SpiritFarmManager(self.db, None, None)
        self.notices = []

        async def notifier(origin, msg):
            self.notices.append((origin, msg))

        self.manager = self.AutoProxyManager(
            self.db, None, None, notifier=notifier, farm_mgr=self.farm_mgr,
        )
        await self.conn.execute(
            "INSERT INTO spirit_farms (user_id, level, crops, fertility) VALUES (?, ?, ?, ?)",
            ("u1", 1, "[]", 100),
        )
        await self.conn.commit()

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _set_crops(self, crops):
        import json
        await self.conn.execute(
            "UPDATE spirit_farms SET crops = ? WHERE user_id = ?",
            (json.dumps(crops, ensure_ascii=False), "u1"),
        )
        await self.conn.commit()

    async def _crops(self):
        farm = await self.farm_mgr.get_user_farm("u1")
        return {int(c["slot"]): c for c in farm["crops"]}

    async def test_enable_farm_harvests_and_replants_selected_slots(self):
        now = int(time.time())
        await self._set_crops([
            {"name": "灵草", "slot": 1, "plant_time": now - 7200, "mature_time": now - 1},
            {"name": "血灵草", "slot": 2, "plant_time": now - 7200, "mature_time": now - 1},
            {"name": "冰心草", "slot": 3, "plant_time": now, "mature_time": now + 3600},
        ])

        ok, msg = await self.manager.enable("u1", "umo://g", "灵田 1 2")
        self.assertTrue(ok, msg)
        self.assertIn("格1", msg)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(self.manager._load_config(row)["farm"]["plots"], [1, 2])

        crops = await self._crops()
        self.assertEqual(crops[1]["name"], "灵草")
        self.assertGreater(crops[1]["mature_time"], now)
        self.assertEqual(crops[2]["name"], "血灵草")
        self.assertGreater(crops[2]["mature_time"], now)
        self.assertEqual(crops[3]["name"], "冰心草")
        self.assertLess(crops[3]["mature_time"], now + 7200)

    async def test_process_due_replants_after_maturity(self):
        now = int(time.time())
        await self._set_crops([
            {"name": "灵草", "slot": 1, "plant_time": now, "mature_time": now + 3600},
            {"name": "冰心草", "slot": 3, "plant_time": now, "mature_time": now + 3600},
        ])
        ok, _ = await self.manager.enable("u1", "umo://g", "灵田")
        self.assertTrue(ok)

        await self._set_crops([
            {"name": "灵草", "slot": 1, "plant_time": now - 7200, "mature_time": now - 1},
            {"name": "冰心草", "slot": 3, "plant_time": now, "mature_time": now + 3600},
        ])
        processed = await self.manager.process_due()
        self.assertEqual(processed, 1)
        self.assertTrue(self.notices)
        crops = await self._crops()
        self.assertEqual(crops[1]["name"], "灵草")
        self.assertGreater(crops[1]["mature_time"], now - 1)
        self.assertEqual(crops[3]["name"], "冰心草")

    async def test_cancel_specific_plot_keeps_others(self):
        now = int(time.time())
        await self._set_crops([
            {"name": "灵草", "slot": 1, "plant_time": now, "mature_time": now + 3600},
            {"name": "血灵草", "slot": 2, "plant_time": now, "mature_time": now + 3600},
        ])
        ok, _ = await self.manager.enable("u1", "umo://g", "灵田")
        self.assertTrue(ok)

        ok, msg = await self.manager.disable_farm("u1", "1")
        self.assertTrue(ok, msg)
        row = await self.db.ext.get_auto_proxy("u1")
        config = self.manager._load_config(row)
        self.assertTrue(config["farm"]["enabled"])
        self.assertEqual(config["farm"]["exclude"], [1])
        farm = await self.farm_mgr.get_user_farm("u1")
        self.assertEqual(self.manager._selected_slots(farm, config["farm"]), [2, 3])
        self.assertNotIn("1", config["farm"]["tasks"])
        self.assertEqual(row["enabled"], 1)

        ok, _ = await self.manager.disable_farm("u1", "")
        self.assertTrue(ok)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertFalse(self.manager._load_config(row)["farm"]["enabled"])
        self.assertEqual(row["enabled"], 0)

    async def test_clear_farm_plot_stops_replant_after_remove(self):
        now = int(time.time())
        await self._set_crops([
            {"name": "灵草", "slot": 1, "plant_time": now, "mature_time": now + 3600},
            {"name": "血灵草", "slot": 2, "plant_time": now, "mature_time": now + 3600},
        ])
        ok, _ = await self.manager.enable("u1", "umo://g", "灵田")
        self.assertTrue(ok)

        success, _ = await self.farm_mgr.remove_crop(
            await self.db.get_player_by_id("u1"), 1
        )
        self.assertTrue(success)
        self.assertTrue(await self.manager.clear_farm_plot("u1", 1))
        row = await self.db.ext.get_auto_proxy("u1")
        config = self.manager._load_config(row)
        self.assertEqual(config["farm"]["exclude"], [1])
        farm = await self.farm_mgr.get_user_farm("u1")
        self.assertEqual(self.manager._selected_slots(farm, config["farm"]), [2, 3])
        self.assertNotIn("1", config["farm"]["tasks"])

    async def test_close_proxy_does_not_stop_farm(self):
        now = int(time.time())
        await self._set_crops([
            {"name": "灵草", "slot": 1, "plant_time": now, "mature_time": now + 3600},
        ])
        ok, _ = await self.manager.enable("u1", "umo://g", "灵田")
        self.assertTrue(ok)

        ok, msg = await self.manager.disable_primary("u1")
        self.assertTrue(ok)
        self.assertIn("灵田自动代理继续运行", msg)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)
        self.assertTrue(self.manager._load_config(row)["farm"]["enabled"])

        ok, _ = await self.manager.disable_farm("u1", "")
        self.assertTrue(ok)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertFalse(self.manager._load_config(row)["farm"]["enabled"])
        self.assertEqual(row["enabled"], 0)

    async def test_plant_herb_requires_farm_level(self):
        player = await self.db.get_player_by_id("u1")
        ok, msg = await self.farm_mgr.plant_herb(player, "冰心草", 1)
        self.assertFalse(ok)
        self.assertIn("Lv.2", msg)
        self.assertEqual(await self._crops(), {})

        ok, msg = await self.farm_mgr.plant_herb(player, "灵草", 1)
        self.assertTrue(ok, msg)
        self.assertEqual((await self._crops())[1]["name"], "灵草")

    async def test_upgrade_unlocks_higher_tier_herbs(self):
        player = await self.db.get_player_by_id("u1")
        player.gold = 100000
        await self.db.update_player(player)

        ok, msg = await self.farm_mgr.upgrade_farm(player)
        self.assertTrue(ok, msg)
        self.assertIn("Lv.2", msg)
        self.assertIn("冰心草", msg)

        ok, msg = await self.farm_mgr.plant_herb(player, "冰心草", 1)
        self.assertTrue(ok, msg)

        ok, msg = await self.farm_mgr.plant_herb(player, "火焰花", 1)
        self.assertFalse(ok)
        self.assertIn("Lv.3", msg)

        farm = await self.farm_mgr.get_user_farm("u1")
        self.assertEqual(farm["level"], 2)

    async def test_auto_proxy_does_not_replant_locked_herb(self):
        now = int(time.time())
        await self._set_crops([
            {"name": "火焰花", "slot": 1, "plant_time": now - 7200, "mature_time": now - 1},
        ])
        ok, _ = await self.manager.enable("u1", "umo://g", "灵田")
        self.assertTrue(ok)
        crops = await self._crops()
        self.assertNotIn(1, crops)

    async def test_hybridize_requires_farm_level(self):
        storage = _FakeStorage({"冰心草": 1, "火焰花": 1})
        self.farm_mgr.storage_ring_manager = storage
        player = await self.db.get_player_by_id("u1")

        ok, msg = await self.farm_mgr.hybridize(player, "冰心草", "火焰花")
        self.assertFalse(ok)
        self.assertIn("Lv.5", msg)
        self.assertEqual(storage.items, {"冰心草": 1, "火焰花": 1})

    async def test_hybrid_occupies_two_plots_and_yields_one_seed(self):
        mgr_mod = _imp("managers.spirit_farm_manager")
        await self.conn.execute(
            "UPDATE spirit_farms SET level = 5 WHERE user_id = ?", ("u1",)
        )
        await self.conn.commit()
        storage = _FakeStorage({"冰心草": 1, "火焰花": 1})
        self.farm_mgr.storage_ring_manager = storage
        player = await self.db.get_player_by_id("u1")

        ok, msg = await self.farm_mgr.hybridize(player, "冰心草", "火焰花")
        self.assertTrue(ok, msg)
        self.assertIn("占用格子", msg)
        self.assertNotIn("冰心草", storage.items)
        self.assertNotIn("火焰花", storage.items)
        self.assertNotIn("冰火双生花", storage.items)

        crops = await self._crops()
        jobs = [c for c in crops.values() if c["name"] == mgr_mod.HYBRID_MARKER]
        self.assertEqual(len(jobs), 2)
        self.assertEqual({c["slot"] for c in jobs}, {1, 2})
        now = int(time.time())
        for crop in crops.values():
            crop["mature_time"] = now - 1
        await self._set_crops(list(crops.values()))

        ok, msg = await self.farm_mgr.harvest(player)
        self.assertTrue(ok, msg)
        self.assertEqual(storage.items.get("冰火双生花"), 1)
        self.assertEqual(await self._crops(), {})

        ok, msg = await self.farm_mgr.plant_herb(player, "冰火双生花", 1)
        self.assertTrue(ok, msg)
        self.assertNotIn("冰火双生花", storage.items)
        crops = await self._crops()
        self.assertEqual(crops[1]["name"], "冰火双生花")

        ok, msg = await self.farm_mgr.plant_herb(player, "冰火双生花", 1)
        self.assertFalse(ok)
        self.assertIn("种苗不足", msg)

    async def test_harvest_normal_crop_yields_four(self):
        now = int(time.time())
        await self._set_crops([
            {"name": "灵草", "slot": 1, "plant_time": now - 7200, "mature_time": now - 1},
        ])
        storage = _FakeStorage({})
        self.farm_mgr.storage_ring_manager = storage
        player = await self.db.get_player_by_id("u1")

        ok, msg = await self.farm_mgr.harvest(player)
        self.assertTrue(ok, msg)
        self.assertEqual(storage.items.get("灵草"), 4)
        self.assertIn("每株产出4份", msg)

    async def test_remove_crop_cancels_whole_hybrid_job(self):
        await self.conn.execute(
            "UPDATE spirit_farms SET level = 5 WHERE user_id = ?", ("u1",)
        )
        await self.conn.commit()
        storage = _FakeStorage({"冰心草": 1, "火焰花": 1})
        self.farm_mgr.storage_ring_manager = storage
        player = await self.db.get_player_by_id("u1")
        ok, msg = await self.farm_mgr.hybridize(player, "冰心草", "火焰花")
        self.assertTrue(ok, msg)

        ok, msg = await self.farm_mgr.remove_crop(player, 2)
        self.assertTrue(ok, msg)
        self.assertIn("取消", msg)
        self.assertEqual(await self._crops(), {})
        self.assertEqual(storage.items, {})

    async def test_hybridize_requires_two_free_plots(self):
        await self.conn.execute(
            "UPDATE spirit_farms SET level = 5 WHERE user_id = ?", ("u1",)
        )
        await self.conn.commit()
        now = int(time.time())
        crops = [
            {"name": "灵草", "slot": slot, "plant_time": now, "mature_time": now + 3600}
            for slot in range(1, 20)
        ]
        await self._set_crops(crops)
        storage = _FakeStorage({"冰心草": 1, "火焰花": 1})
        self.farm_mgr.storage_ring_manager = storage
        player = await self.db.get_player_by_id("u1")

        ok, msg = await self.farm_mgr.hybridize(player, "冰心草", "火焰花")
        self.assertFalse(ok)
        self.assertIn("2 个空格", msg)
        self.assertEqual(storage.items, {"冰心草": 1, "火焰花": 1})

    async def test_new_hybrid_recipes_and_levels(self):
        mgr_mod = _imp("managers.spirit_farm_manager")
        self.assertEqual(
            mgr_mod.HYBRID_RECIPES[frozenset({"血灵草", "九叶灵芝"})], "血玉灵芝"
        )
        self.assertEqual(
            mgr_mod.HYBRID_RECIPES[frozenset({"九叶灵芝", "幽冥草"})], "九幽仙芝"
        )
        self.assertTrue(mgr_mod.requires_seed("血玉灵芝"))
        self.assertTrue(mgr_mod.requires_seed("九幽仙芝"))
        self.assertFalse(mgr_mod.requires_seed("九叶灵芝"))
        self.assertIn("冰心草 + 火焰花", mgr_mod.get_hybrid_formula("冰火双生花"))

    async def test_auto_proxy_replants_hybrid_with_harvested_seed(self):
        await self.conn.execute(
            "UPDATE spirit_farms SET level = 5 WHERE user_id = ?", ("u1",)
        )
        await self.conn.commit()
        now = int(time.time())
        await self._set_crops([
            {"name": "冰火双生花", "slot": 1, "plant_time": now - 86400, "mature_time": now - 1},
        ])
        storage = _FakeStorage({})
        self.farm_mgr.storage_ring_manager = storage

        ok, msg = await self.manager.enable("u1", "umo://g", "灵田")
        self.assertTrue(ok, msg)
        crops = await self._crops()
        self.assertEqual(crops[1]["name"], "冰火双生花")
        self.assertGreater(crops[1]["mature_time"], now)
        self.assertEqual(storage.items.get("冰火双生花"), 3)

    async def test_remove_crop_validates_slot(self):
        now = int(time.time())
        await self._set_crops([
            {"name": "灵草", "slot": 1, "plant_time": now, "mature_time": now + 3600},
        ])
        player = await self.db.get_player_by_id("u1")
        ok, msg = await self.farm_mgr.remove_crop(player, 1)
        self.assertTrue(ok, msg)
        self.assertEqual(await self._crops(), {})
        ok, msg = await self.farm_mgr.remove_crop(player, 1)
        self.assertFalse(ok)
        self.assertIn("没有种植", msg)


    async def test_farm_only_proxy_does_not_rebind_manual_activity(self):
        UserStatus = _imp("models_extended").UserStatus
        ok, msg = await self.manager.enable("u1", "umo://g", "灵田")
        self.assertTrue(ok, msg)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(row["task_type"], "")

        await self.db.ext.set_user_busy(
            "u1", UserStatus.DELIVERING, int(time.time()) + 900
        )
        bound = await self.manager.note_activity_started("u1", "umo://g")
        self.assertIsNone(bound)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["task_type"], "")
        self.assertEqual(row["enabled"], 1)


if __name__ == "__main__":
    unittest.main()
