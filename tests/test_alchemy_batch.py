# 炼丹批量次数 / all 回归测试
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

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


class _FakeConn:
    def __init__(self):
        self.executed = []

    async def execute(self, sql=None, params=None, *a, **k):
        self.executed.append((sql, params))
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


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
        self.conn = _FakeConn()
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


def _make_mgr(ring=None, gold=1000):
    Player = _imp("models").Player
    AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
    player = Player(user_id="u1", level_index=5, gold=gold)
    player.set_storage_ring_items(ring or {})
    db = _FakeDB(player)
    mgr = AlchemyManager(db, _FakeConfig(), None)

    class _FakeSocial:
        async def companion_bonus_async(self, *args, **kwargs):
            return 0.0

    mgr._social = _FakeSocial()
    return mgr, db, player


class TestAlchemyBatch(unittest.IsolatedAsyncioTestCase):
    async def test_batch_count_consumes_materials_and_gold(self):
        mgr, db, player = _make_mgr({"灵草": 25}, gold=1000)
        with mock.patch("random.randint", return_value=1):
            ok, msg, data = await mgr.craft_pill("u1", 1, "守中", 10)

        self.assertTrue(ok, msg)
        self.assertTrue(data and data.get("batch"))
        self.assertEqual(data["crafted"], 10)
        self.assertEqual(data["success_count"], 10)
        self.assertEqual(data["cost"], 1000)
        self.assertEqual(player.gold, 0)
        self.assertEqual(player.get_storage_ring_items(), {"灵草": 5})
        self.assertIn("批量炼丹完成", msg)
        self.assertIn("成丹 10 颗", msg)
        self.assertEqual(sum(player.get_pills_inventory().values()), 10)

    async def test_all_consumes_every_available_material(self):
        mgr, db, player = _make_mgr({"灵草": 21}, gold=10000)
        with mock.patch("random.randint", return_value=1):
            ok, msg, data = await mgr.craft_pill("u1", 1, "守中", "all")

        self.assertTrue(ok, msg)
        # 21 株灵草、每炉 2 株 → 只能炼 10 次
        self.assertEqual(data["crafted"], 10)
        self.assertEqual(player.get_storage_ring_items(), {"灵草": 1})
        self.assertEqual(player.gold, 9000)

    async def test_all_is_limited_by_gold(self):
        mgr, db, player = _make_mgr({"灵草": 100}, gold=350)
        with mock.patch("random.randint", return_value=1):
            ok, msg, data = await mgr.craft_pill("u1", 1, "守中", "all")

        self.assertTrue(ok, msg)
        self.assertEqual(data["crafted"], 3)
        self.assertEqual(player.gold, 50)
        self.assertEqual(player.get_storage_ring_items(), {"灵草": 94})

    async def test_count_resumes_from_fire_control_state(self):
        mgr, db, player = _make_mgr({"灵草": 20}, gold=1000)

        ok, msg, data = await mgr.craft_pill("u1", 1, count=10)
        self.assertTrue(ok, msg)
        self.assertTrue(data and data.get("pending_fire"))
        self.assertIn("连续炼丹 10 次", msg)

        ok, msg = await mgr.set_fire_choice("u1", "2")
        self.assertTrue(ok, msg)
        self.assertIn("连续炼丹 10 次", msg)

        with mock.patch("random.randint", return_value=1):
            ok, msg, data = await mgr.craft_pill("u1", 1)
        self.assertTrue(ok, msg)
        self.assertEqual(data["crafted"], 10)
        self.assertEqual(data["success_count"], 10)

    async def test_zero_available_returns_requirements(self):
        mgr, db, player = _make_mgr({}, gold=0)
        ok, msg, data = await mgr.craft_pill("u1", 1, "守中", "all")
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn("一次也炼不了", msg)
        self.assertIn("灵草 x2", msg)
        self.assertIn("灵石 x100", msg)

    async def test_handler_parses_fire_and_amount(self):
        Handlers = _imp("handlers.alchemy_handlers").AlchemyHandlers
        parse = Handlers._parse_craft_options

        self.assertEqual(parse(["猛火", "10"]), ("1", 10, ""))
        self.assertEqual(parse(["10"]), (None, 10, ""))
        self.assertEqual(parse(["1", "10"]), ("1", 10, ""))
        self.assertEqual(parse(["all"]), (None, "all", ""))
        self.assertEqual(parse(["10", "文火"]), ("2", 10, ""))
        self.assertEqual(parse(["10次"]), (None, 10, ""))
        self.assertEqual(parse(["x10"]), (None, 10, ""))
        self.assertTrue(parse(["奇怪参数"])[2])


if __name__ == "__main__":
    unittest.main()
