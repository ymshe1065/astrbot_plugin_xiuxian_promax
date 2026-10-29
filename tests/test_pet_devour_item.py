# tests/test_pet_devour_item.py
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


class _StubConfig:
    def __init__(self):
        self.items_data = {
            "灵兽内丹": {
                "name": "灵兽内丹",
                "type": "材料",
                "rank": "地阶",
                "price": 1200,
                "volume": 1,
            }
        }
        self.weapons_data = {}
        self.storage_rings_data = {"储物戒": {"type": "storage_ring", "capacity": 20}}


class TestPetDevourItem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.PetManager = _imp("managers.pet_manager").PetManager
        self.StorageRingManager = _imp("core.storage_ring_manager").StorageRingManager
        self.Player = _imp("models").Player

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        self.player = self.Player(user_id="u1", user_name="道友", gold=1_000_000)
        await self.db.create_player(self.player)

        self.config = _StubConfig()
        self.storage_mgr = self.StorageRingManager(self.db, self.config)
        self.pet_mgr = self.PetManager(self.db, self.config, self.storage_mgr)

    async def asyncTearDown(self):
        await self.conn.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    async def _give_core(self, count: int):
        self.player.set_storage_ring_items({"灵兽内丹": count})
        await self.db.update_player(self.player)

    async def test_devour_core_raises_level_and_consumes_item(self):
        ok, _ = await self.pet_mgr.adopt_pet(self.player, "火灵狐")
        self.assertTrue(ok)
        await self._give_core(2)

        ok, msg = await self.pet_mgr.devour_item(self.player, "1")
        self.assertTrue(ok, msg)
        self.assertIn("Lv.1 → Lv.2", msg)

        pet = await self.pet_mgr.get_pet("u1")
        self.assertEqual(pet["level"], 2)
        self.assertEqual(self.storage_mgr.get_item_count(self.player, "灵兽内丹"), 1)

        ok, _ = await self.pet_mgr.devour_item(self.player, "1", "灵兽内丹")
        self.assertTrue(ok)
        pet = await self.pet_mgr.get_pet("u1")
        self.assertEqual(pet["level"], 3)
        self.assertEqual(self.storage_mgr.get_item_count(self.player, "灵兽内丹"), 0)

        ok, msg = await self.pet_mgr.devour_item(self.player, "1")
        self.assertFalse(ok)
        self.assertIn("没有【灵兽内丹】", msg)

    async def test_devour_core_triggers_evolution(self):
        ok, _ = await self.pet_mgr.adopt_pet(self.player, "火灵狐")
        self.assertTrue(ok)
        await self.conn.execute(
            "UPDATE player_pets SET level = 19, exp = 0 WHERE user_id = ?", ("u1",)
        )
        await self.conn.commit()
        await self._give_core(1)

        ok, msg = await self.pet_mgr.devour_item(self.player, "1")
        self.assertTrue(ok, msg)
        self.assertIn("进化为【炎灵狐】", msg)

        pet = await self.pet_mgr.get_pet("u1")
        self.assertEqual(pet["level"], 20)
        self.assertEqual(pet["pet_type"], "炎灵狐")

    async def test_rejects_other_item_and_dead_pet(self):
        ok, _ = await self.pet_mgr.adopt_pet(self.player, "火灵狐")
        self.assertTrue(ok)
        await self._give_core(1)

        ok, msg = await self.pet_mgr.devour_item(self.player, "1", "妖兽精血")
        self.assertFalse(ok)
        self.assertIn("只有【灵兽内丹】", msg)
        self.assertEqual(self.storage_mgr.get_item_count(self.player, "灵兽内丹"), 1)

        await self.conn.execute(
            "UPDATE player_pets SET dead_until = ? WHERE user_id = ?",
            (int(__import__("time").time()) + 600, "u1"),
        )
        await self.conn.commit()
        ok, msg = await self.pet_mgr.devour_item(self.player, "1")
        self.assertFalse(ok)
        self.assertIn("复活中", msg)
        self.assertEqual(self.storage_mgr.get_item_count(self.player, "灵兽内丹"), 1)


if __name__ == "__main__":
    unittest.main()
