# 炼丹管理器 P0 修复回归测试
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


class _FakeDB:
    def __init__(self, player):
        self.player = player

    async def get_player_by_id(self, user_id):
        return self.player if user_id == self.player.user_id else None


class _FakeStorage:
    def get_item_count(self, player, item_name):
        return 3 if item_name == "灵草" else 0


class _FakeConfig:
    alchemy_config = {}
    alchemy_recipes = {
        "炼气丹": {
            "id": 1,
            "name": "炼气丹",
            "level_required": 0,
            "materials": {"灵草": 2, "灵石": 100},
            "success_rate": 85,
        },
        "聚气丹": {
            "id": 2,
            "name": "聚气丹",
            "level_required": 0,
            "materials": {"灵草": 1, "灵石": 50},
            "success_rate": 80,
        },
    }
    exp_pills_data = {"炼气丹": {"name": "炼气丹", "exp_gain": 500, "rank": "凡品"}}
    utility_pills_data = {}
    pills_data = {}


class TestAlchemyFixes(unittest.IsolatedAsyncioTestCase):
    async def test_recipe_list_available(self):
        Player = _imp("models").Player
        AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
        player = Player(user_id="u1", user_name="道友")
        mgr = AlchemyManager(_FakeDB(player), _FakeConfig(), _FakeStorage())
        ok, text = await mgr.get_available_recipes("u1")
        self.assertTrue(ok)
        self.assertIn("[1] 炼气丹", text)
        self.assertIn("灵草x2(✓)", text)
        self.assertIn("灵石x100", text)
        self.assertIn("可炼制", text)

    async def test_recipe_list_only_shows_learned(self):
        Player = _imp("models").Player
        AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
        player = Player(user_id="u1", user_name="道友", learned_recipes="[1]")
        mgr = AlchemyManager(_FakeDB(player), _FakeConfig(), _FakeStorage())
        ok, text = await mgr.get_available_recipes("u1")
        self.assertTrue(ok)
        self.assertIn("[1] 炼气丹", text)
        self.assertNotIn("[2] 聚气丹", text)
        self.assertNotIn("未学习", text)


if __name__ == "__main__":
    unittest.main()
