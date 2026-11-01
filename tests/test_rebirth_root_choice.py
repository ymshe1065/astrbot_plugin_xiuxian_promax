# tests/test_rebirth_root_choice.py
import json
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
    async def execute(self, *a, **k):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _FakeExt:
    def __init__(self):
        self.config_store = {}

    async def get_system_config(self, key):
        return self.config_store.get(key)

    async def set_system_config(self, key, value):
        self.config_store[key] = value

    async def ensure_impart_info(self, user_id):
        return None

    async def get_user_cd(self, user_id):
        return _FakeCD(0)

    async def get_active_loan(self, user_id):
        return None

    async def set_user_free(self, user_id, commit=True):
        return None

    async def return_gifts_for_receiver(self, user_id, commit=True):
        return None

    async def create_user_cd(self, user_id):
        return None


class _FakeCD:
    def __init__(self, status):
        self.type = status


class _FakeDB:
    def __init__(self):
        self.conn = _FakeConn()
        self.ext = _FakeExt()
        self.players = {}

    async def get_player_by_id(self, user_id):
        return self.players.get(user_id)

    async def create_player(self, player):
        self.players[player.user_id] = player

    async def update_player(self, player, commit=True):
        self.players[player.user_id] = player

    async def delete_player_cascade(self, user_id):
        self.players.pop(user_id, None)


class _FakeEvent:
    def __init__(self):
        self.results = []

    def get_sender_id(self):
        return "u1"

    def get_sender_name(self):
        return "测试道友"

    def get_message_str(self):
        return ""

    def plain_result(self, text):
        self.results.append(text)
        return text


class TestRootChoice(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.PlayerHandler = _imp("handlers.player_handler").PlayerHandler
        cls.CultivationManager = _imp("core.cultivation_manager").CultivationManager

    def _handler(self):
        db = _FakeDB()
        config = {"VALUES": {"INITIAL_GOLD": 1000, "LEVEL_EXP_BONUS_PER_LEVEL": 0.1},
                  "SPIRIT_ROOT_WEIGHTS": {}, "SPIRIT_ROOT_SPEEDS": {}}
        handler = self.PlayerHandler(db, config, None)

        async def fake_tutorial(*a, **k):
            return ""

        handler.tutorial_mgr.check_and_advance = fake_tutorial
        return handler, db

    def test_draw_roots_are_distinct(self):
        cm = self.CultivationManager({"SPIRIT_ROOT_WEIGHTS": {}}, None)
        roots = cm.draw_spiritual_roots(3)
        self.assertEqual(len(roots), 3)
        self.assertEqual(len(set(roots)), 3)

    def test_rebirth_points_formula(self):
        handler, _ = self._handler()
        self.assertEqual(handler._rebirth_points(0, 1), 10)
        self.assertEqual(handler._rebirth_points(20, 1_000_000), 10 + 20 + 30)

    async def test_create_flow_three_roots_then_choose(self):
        handler, db = self._handler()
        event = _FakeEvent()
        async for _ in handler.handle_start_xiuxian(event, "剑修"):
            pass
        self.assertEqual(db.players, {})
        pending = await handler._load_root_pending("create_roots", "u1")
        self.assertIsNotNone(pending)
        roots = pending["roots"]
        self.assertEqual(len(roots), 3)
        self.assertIn("选择灵根", event.results[-1])

        event2 = _FakeEvent()
        async for _ in handler.handle_choose_root(event2, "2"):
            pass
        player = db.players.get("u1")
        self.assertIsNotNone(player)
        self.assertEqual(player.spiritual_root, f"{roots[1]}灵根")
        self.assertIn("踏上仙途", event2.results[-1])

    async def test_rebirth_keeps_root_and_meta(self):
        handler, db = self._handler()
        player = handler.cultivation_manager.generate_new_player_stats("u1", "剑修", root="火")
        player.level_index = 20
        player.experience = 1_000_000
        player.gold = 88888
        player.fate_points = 100
        player.reincarnation_count = 1
        player.permanent_bonus = json.dumps({"titles": {"测试称号": 1}}, ensure_ascii=False)
        player.set_storage_ring_items({"灵石袋": 1})
        db.players["u1"] = player

        ok, msg = await handler._perform_rebirth(player, "火")
        self.assertTrue(ok, msg)
        self.assertEqual(player.spiritual_root, "火灵根")
        self.assertEqual(player.level_index, 0)
        self.assertEqual(player.experience, 0)
        self.assertEqual(player.gold, 1000)
        self.assertEqual(player.fate_points, 100 + 10 + 20 + 30)
        self.assertEqual(player.reincarnation_count, 2)
        self.assertIn("测试称号", player.permanent_bonus)
        self.assertEqual(player.get_storage_ring_items(), {})
        self.assertTrue(db.ext.config_store.get("rebirth_last_u1", "").isdigit())
        self.assertEqual(db.ext.config_store.get("rebirth_total_u1"), "1")
        self.assertIn("弃道重修完成", msg)


if __name__ == "__main__":
    unittest.main()
