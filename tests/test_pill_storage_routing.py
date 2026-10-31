# 丹药发放路由与历史丹药归位回归测试
import json
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


class _L:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


api_mod.logger = _L(); api_mod.AstrBotConfig = dict
event_mod.AstrMessageEvent = object; api_mod.event = event_mod
all_mod = types.ModuleType("astrbot.api.all"); all_mod.At = object; all_mod.Plain = object
api_mod.all = all_mod
sys.modules.setdefault("astrbot", astrbot); sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod); sys.modules.setdefault("astrbot.api.all", all_mod)


def _imp(p):
    return __import__(f"{PKG}.{p}", fromlist=["x"])


class TestPillStorageRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = _imp("data")
        self.ConfigManager = _imp("config_manager").ConfigManager
        self.Player = _imp("models").Player
        self.StorageRingManager = _imp("core").StorageRingManager
        self.cm = self.ConfigManager(ROOT)
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = self.data.DataBase(self.path)
        await self.db.connect()
        await self.data.MigrationManager(self.db.conn, self.cm).migrate()
        self.ring = self.StorageRingManager(self.db, self.cm)
        self.pill = "六品破境增益丹"

    async def asyncTearDown(self):
        await self.db.close()
        try: os.remove(self.path)
        except OSError: pass

    async def _new_player(self, user_id="u1"):
        player = self.Player(user_id=user_id, user_name="道友", storage_ring="基础储物戒")
        await self.db.create_player(player)
        return await self.db.get_player_by_id(user_id)

    async def test_grant_item_routes_pill_to_pill_inventory(self):
        player = await self._new_player()
        ok, _ = await self.ring.grant_item(player, self.pill, 2)
        self.assertTrue(ok)
        player = await self.db.get_player_by_id("u1")
        self.assertEqual(player.get_pills_inventory().get(f"{self.pill}|一般"), 2)
        self.assertNotIn(self.pill, player.get_storage_ring_items())

    async def test_pill_recognized_with_quality_suffix(self):
        self.assertTrue(self.cm.is_pill(f"{self.pill}|良好"))
        self.assertTrue(self.ring.is_pill(f"{self.pill}|极佳"))
        player = await self._new_player()
        ok, _ = await self.ring.grant_item(player, f"{self.pill}|良好", 1)
        self.assertTrue(ok)
        player = await self.db.get_player_by_id("u1")
        self.assertEqual(player.get_pills_inventory().get(f"{self.pill}|良好"), 1)
        self.assertNotIn(f"{self.pill}|良好", player.get_storage_ring_items())

    async def test_grant_item_keeps_materials_in_storage_ring(self):
        player = await self._new_player()
        ok, _ = await self.ring.grant_item(player, "玄铁", 1)
        self.assertTrue(ok)
        player = await self.db.get_player_by_id("u1")
        self.assertEqual(player.get_storage_ring_items().get("玄铁"), 1)
        self.assertFalse(player.get_pills_inventory())

    async def test_store_item_still_rejects_pill(self):
        player = await self._new_player()
        ok, msg = await self.ring.store_item(player, self.pill, 1)
        self.assertFalse(ok)
        self.assertIn("丹药", msg)
        player = await self.db.get_player_by_id("u1")
        self.assertNotIn(self.pill, player.get_storage_ring_items())

    async def test_claim_overflow_routes_pill_to_pill_inventory(self):
        player = await self._new_player()
        player.set_overflow_items({self.pill: 1})
        await self.db.update_player(player)
        claimed, _ = await self.ring.claim_overflow(player)
        self.assertGreaterEqual(claimed, 1)
        player = await self.db.get_player_by_id("u1")
        self.assertEqual(player.get_pills_inventory().get(f"{self.pill}|一般"), 1)
        self.assertNotIn(self.pill, player.get_storage_ring_items())
        self.assertFalse(player.get_overflow_items())

    async def test_migration_moves_legacy_pills_back_to_inventory(self):
        player = await self._new_player()
        player.set_storage_ring_items({self.pill: 1, "四品破境增益丹": 1, "玄铁": 2})
        player.set_overflow_items({"七品化神增益丹": 3})
        await self.db.update_player(player)
        # 模拟旧存档：数据库版本停留在 v55
        await self.db.conn.execute("UPDATE db_info SET version = 55")
        await self.db.conn.commit()

        await self.data.MigrationManager(self.db.conn, self.cm).migrate()

        player = await self.db.get_player_by_id("u1")
        inventory = player.get_pills_inventory()
        self.assertEqual(inventory.get(f"{self.pill}|一般"), 1)
        self.assertEqual(inventory.get("四品破境增益丹|一般"), 1)
        self.assertEqual(inventory.get("七品化神增益丹|一般"), 3)
        self.assertEqual(player.get_storage_ring_items(), {"玄铁": 2})
        self.assertFalse(player.get_overflow_items())


if __name__ == "__main__":
    unittest.main()
