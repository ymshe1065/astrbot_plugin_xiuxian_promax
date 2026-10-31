"""创建角色时的职业定位展示。"""
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
api_mod.logger = types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None,
    error=lambda *a, **k: None, exception=lambda *a, **k: None,
)
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


def _imp(path):
    return __import__(f"{PKG}.{path}", fromlist=["x"])


class _DB:
    async def get_player_by_id(self, user_id):
        return None


class _Event:
    def get_sender_id(self):
        return "u1"

    def get_message_str(self):
        return ""

    def plain_result(self, text):
        return text


class TestClassRoleCreation(unittest.IsolatedAsyncioTestCase):
    def test_all_classes_have_unique_role_codes(self):
        mod = _imp("handlers.player_handler")
        self.assertEqual(set(mod.CLASS_ROLE_INFO), set(mod.VALID_CULTIVATIONS))
        codes = [v["code"] for v in mod.CLASS_ROLE_INFO.values()]
        self.assertEqual(len(codes), len(set(codes)))

    async def test_creation_menu_shows_roles(self):
        mod = _imp("handlers.player_handler")
        handler = object.__new__(mod.PlayerHandler)
        handler.db = _DB()
        results = [x async for x in handler.handle_start_xiuxian(_Event(), "")]
        text = results[0]
        for name, info in mod.CLASS_ROLE_INFO.items():
            self.assertIn(f"【{name}】定位：{info['name']}", text)
        self.assertIn("定位：", text)
        self.assertIn("我要修仙 剑修", text)


if __name__ == "__main__":
    unittest.main()
