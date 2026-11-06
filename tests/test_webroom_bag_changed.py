# -*- coding: utf-8 -*-
"""webroom 储物戒变更推送：指纹比对与 bag_changed 事件。"""
import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

_TMP = tempfile.mkdtemp(prefix="xiuxian_bag_changed_")

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
core_mod = types.ModuleType("astrbot.core")
utils_mod = types.ModuleType("astrbot.core.utils")
path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")


class _L:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass
    def debug(self, *a, **k): pass


api_mod.logger = _L()
api_mod.AstrBotConfig = dict
path_mod.get_astrbot_data_path = lambda: _TMP
utils_mod.astrbot_path = path_mod
core_mod.utils = utils_mod
astrbot.api = api_mod
astrbot.core = core_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.core", core_mod)
sys.modules.setdefault("astrbot.core.utils", utils_mod)
sys.modules.setdefault("astrbot.core.utils.astrbot_path", path_mod)

try:
    rs = __import__(f"{PKG}.webchat_room.room_server", fromlist=["x"])
except Exception as exc:  # pragma: no cover - 依赖缺失时跳过
    raise unittest.SkipTest(f"webroom 依赖不可用: {exc}")


class FakePlayer:
    def __init__(self):
        self.storage_ring = "基础储物戒"
        self.permanent_bonus = "{}"
        self._items = {"聚气丹": 3}
        self._overflow = {}
        self._instances = [
            {"id": 1, "name": "青锋剑", "quality": "一般", "location": "storage"},
            {"id": 2, "name": "青锋剑", "quality": "极品", "location": "storage"},
        ]
        self._techniques = []
        self.main_technique = ""

    def get_storage_ring_items(self):
        return dict(self._items)

    def get_overflow_items(self):
        return dict(self._overflow)

    def get_equipment_instances(self):
        return [dict(i) for i in self._instances]

    def get_techniques_list(self):
        return list(self._techniques)


class BagChangedTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = []
        self._orig_plugin = rs._plugin
        self._orig_push = rs.push_to_username
        self.player = FakePlayer()

        async def fake_push(room_id, username, payload):
            self.sent.append((room_id, username, payload))
            return 1

        async def fake_get_player(_key):
            return self.player

        rs.push_to_username = fake_push
        rs._plugin = SimpleNamespace(db=SimpleNamespace(get_player_by_id=fake_get_player))
        rs._bag_sigs.clear()

    def tearDown(self):
        rs._plugin = self._orig_plugin
        rs.push_to_username = self._orig_push
        rs._bag_sigs.clear()

    async def test_first_check_only_seeds_cache(self):
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        self.assertEqual(self.sent, [])
        self.assertIn("web_u1", rs._bag_sigs)

    async def test_storage_change_pushes_bag_changed(self):
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        self.player._items["聚气丹"] = 4
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        self.assertEqual(len(self.sent), 1)
        room_id, username, payload = self.sent[0]
        self.assertEqual(room_id, rs.ROOM_ID)
        self.assertEqual(username, "u1")
        self.assertEqual(payload["type"], "bag_changed")

    async def test_no_change_does_not_push(self):
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        self.assertEqual(self.sent, [])

    async def test_equip_instance_leaving_storage_pushes(self):
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        self.player._instances[0]["location"] = "equipped"
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        self.assertEqual(len(self.sent), 1)

    async def test_enchant_and_ring_change_push(self):
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        self.player.permanent_bonus = '{"storage_enchant": 5}'
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        self.player.storage_ring = "乾坤戒"
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        self.assertEqual(len(self.sent), 2)

    async def test_scheduled_check_runs_and_pushes(self):
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        self.sent.clear()
        self.player._overflow["空间石"] = 1
        rs.schedule_bag_check(rs.ROOM_ID, "web_u1", "u1", delay=0)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if self.sent:
                break
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][2]["type"], "bag_changed")

    async def test_missing_player_is_silent(self):
        async def none(_key):
            return None

        rs._plugin.db.get_player_by_id = none
        await rs._bag_check(rs.ROOM_ID, "web_u1", "u1")
        self.assertEqual(self.sent, [])
        self.assertNotIn("web_u1", rs._bag_sigs)


if __name__ == "__main__":
    unittest.main()
