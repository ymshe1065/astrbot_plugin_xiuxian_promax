# 炼丹开始阶段展示出炉材料需求
import sys
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


class _FakeExt:
    def __init__(self):
        self.system = {}

    async def get_system_config(self, key):
        return self.system.get(key, "")

    async def set_system_config(self, key, value):
        self.system[key] = value


class _FakeDB:
    def __init__(self, player):
        self.player = player
        self.ext = _FakeExt()

    async def get_player_by_id(self, user_id):
        return self.player if user_id == self.player.user_id else None

    async def update_player(self, player, commit=True):
        self.player = player


class _FakeConfig:
    alchemy_config = {}
    alchemy_recipes = {
        "炼气丹": {
            "id": 1,
            "name": "炼气丹方",
            "output_pill": "炼气丹",
            "level_required": 0,
            "materials": {"灵草": 2, "灵石": 100},
            "success_rate": 85,
        }
    }
    exp_pills_data = {"炼气丹": {"name": "炼气丹", "exp_gain": 500, "rank": "凡品"}}
    utility_pills_data = {}
    pills_data = {}
    items_data = {}


class _OverlapConfig(_FakeConfig):
    alchemy_recipes = {
        "化神丹": {
            "id": 1,
            "name": "化神丹方",
            "output_pill": "化神丹",
            "level_required": 0,
            "materials": [
                {"category": "灵植", "tier": "天阶", "count": 2},
                {"name": "血玉灵芝", "count": 1},
            ],
            "success_rate": 85,
        }
    }

    def get_material_info(self, name):
        if name in ("血玉灵芝", "灵草", "九叶灵芝"):
            return {"name": name, "category": "灵植", "tier": "天阶"}
        return {}


class TestAlchemyStartRequirements(unittest.IsolatedAsyncioTestCase):
    async def test_start_shows_required_materials_and_output(self):
        Player = _imp("models").Player
        AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
        player = Player(user_id="u1", level_index=5, gold=1000)
        player.set_storage_ring_items({"灵草": 2})
        db = _FakeDB(player)
        mgr = AlchemyManager(db, _FakeConfig(), None)

        ok, msg, data = await mgr.craft_pill("u1", 1)
        self.assertTrue(ok, msg)
        self.assertTrue(data and data.get("pending_fire"))
        self.assertIn("出炉：炼气丹", msg)
        self.assertIn("出炉所需材料", msg)
        self.assertIn("灵草 x2", msg)
        self.assertIn("灵石 x100", msg)

    async def test_named_material_is_reserved_before_category_materials(self):
        Player = _imp("models").Player
        AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
        player = Player(user_id="u1", level_index=5, gold=1000)
        player.set_storage_ring_items({"血玉灵芝": 1, "灵草": 2})
        db = _FakeDB(player)
        mgr = AlchemyManager(db, _OverlapConfig(), None)
        recipe = mgr.recipes[1]

        ok, msg, consumed = mgr._consume_materials(player, recipe)
        self.assertTrue(ok, msg)
        self.assertNotIn("血玉灵芝", player.get_storage_ring_items())
        self.assertNotIn("灵草", player.get_storage_ring_items())
        self.assertIn("血玉灵芝×1", consumed)

    async def test_named_material_shortage_detected_after_reservation(self):
        Player = _imp("models").Player
        AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
        player = Player(user_id="u1", level_index=5, gold=1000)
        player.set_storage_ring_items({"血玉灵芝": 1, "灵草": 1})
        db = _FakeDB(player)
        mgr = AlchemyManager(db, _OverlapConfig(), None)
        recipe = mgr.recipes[1]

        ok, msg, consumed = mgr._consume_materials(player, recipe)
        self.assertFalse(ok)
        self.assertIn("材料不足", msg)
        self.assertEqual(consumed, [])
        self.assertEqual(player.get_storage_ring_items(), {"血玉灵芝": 1, "灵草": 1})

    async def test_start_blocks_when_materials_missing(self):
        Player = _imp("models").Player
        AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
        player = Player(user_id="u1", level_index=5, gold=0)
        player.set_storage_ring_items({})
        db = _FakeDB(player)
        mgr = AlchemyManager(db, _FakeConfig(), None)

        ok, msg, data = await mgr.craft_pill("u1", 1)
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn("材料或灵石不足", msg)
        self.assertIn("灵草 x2", msg)
        self.assertIn("灵石 x100", msg)


if __name__ == "__main__":
    unittest.main()
