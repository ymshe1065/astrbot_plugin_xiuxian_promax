# 气运系统回归测试
import asyncio
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


class TestLuckSystem(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _imp("data")
        cls.ConfigManager = _imp("config_manager").ConfigManager
        cls.Player = _imp("models").Player
        cls.LuckManager = _imp("managers.luck_manager").LuckManager
        cls.config_manager = cls.ConfigManager(ROOT)

    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = self.data.DataBase(self.db_path)
        await self.db.connect()
        await self.data.MigrationManager(self.db.conn, self.config_manager).migrate()

    async def asyncTearDown(self):
        await self.db.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_breakdown_sums_sources(self):
        player = self.Player(user_id="1", luck=3)
        player.permanent_bonus = json.dumps({
            "titles": {"气运之子": 1},
            "affixes": [{"key": "luck", "value": 2}],
        })
        player.equipment_instances = json.dumps([
            {"id": 1, "affixes": [{"key": "luck", "value": 4}]}
        ])
        player.equipped_instances = json.dumps({"weapon": 1})
        player.active_pill_effects = json.dumps([
            {"source": "talisman", "expiry_time": 9999999999, "luck_bonus": 5}
        ])
        parts = self.LuckManager.get_breakdown(player)
        self.assertEqual(parts["base"], 3)
        self.assertEqual(parts["title"], 50)
        self.assertEqual(parts["affix"], 6)
        self.assertEqual(parts["talisman"], 5)
        self.assertEqual(parts["total"], 64)

    def test_expired_talisman_ignored(self):
        player = self.Player(user_id="1", luck=1)
        player.active_pill_effects = json.dumps([
            {"source": "talisman", "expiry_time": 1, "luck_bonus": 5}
        ])
        self.assertEqual(self.LuckManager.get_total_luck(player), 1)

    async def test_luck_persists(self):
        player = self.Player(user_id="luck-test", luck=7)
        await self.db.create_player(player)
        loaded = await self.db.get_player_by_id("luck-test")
        self.assertEqual(loaded.luck, 7)

        loaded.luck = 9
        await self.db.update_player(loaded)
        reloaded = await self.db.get_player_by_id("luck-test")
        self.assertEqual(reloaded.luck, 9)


if __name__ == "__main__":
    unittest.main()
