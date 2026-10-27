# 储物戒溢出仓库回归测试
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


class TestOverflow(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self):
        await self.db.close()
        try: os.remove(self.path)
        except OSError: pass

    async def test_full_storage_goes_to_overflow_and_can_claim(self):
        player = self.Player(user_id="u1", user_name="道友", storage_ring="基础储物戒")
        await self.db.create_player(player)
        player = await self.db.get_player_by_id("u1")
        cap = self.ring.get_total_capacity(player)
        player.storage_ring_items = json.dumps({"灵草": cap}, ensure_ascii=False)
        await self.db.update_player(player)
        ok, msg = await self.ring.store_item(player, "玄铁", 1)
        self.assertTrue(ok)
        self.assertIn("溢出", msg)
        player = await self.db.get_player_by_id("u1")
        self.assertEqual(player.get_overflow_items().get("玄铁"), 1)
        self.assertNotIn("玄铁", player.get_storage_ring_items())

        # 腾出空间后领取
        player.storage_ring_items = json.dumps({}, ensure_ascii=False)
        await self.db.update_player(player)
        claimed, msg = await self.ring.claim_overflow(player)
        self.assertGreaterEqual(claimed, 1)
        player = await self.db.get_player_by_id("u1")
        self.assertEqual(player.get_storage_ring_items().get("玄铁"), 1)
        self.assertFalse(player.get_overflow_items())


if __name__ == "__main__":
    unittest.main()
