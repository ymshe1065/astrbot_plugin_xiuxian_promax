"""灵田批量铲除参数解析。"""
import asyncio
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
api_mod.logger = types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None,
    error=lambda *a, **k: None, exception=lambda *a, **k: None,
)
api_mod.AstrBotConfig = dict
api_mod.__path__ = []
event_mod = types.ModuleType("astrbot.api.event")
event_mod.AstrMessageEvent = object
all_mod = types.ModuleType("astrbot.api.all")
all_mod.At = object
all_mod.Plain = object
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod)
sys.modules.setdefault("astrbot.api.all", all_mod)


def _imp(path):
    return __import__(f"{PKG}.{path}", fromlist=["x"])


class _Event:
    def plain_result(self, text):
        return text


class _Mgr:
    def __init__(self):
        self.removed = None

    async def get_user_farm(self, user_id):
        return {"crops": [{"slot": 1}, {"slot": 2}, {"slot": 5}]}

    async def remove_crops(self, player, slots):
        self.removed = slots
        return True, "ok"


class TestFarmBatchRemove(unittest.IsolatedAsyncioTestCase):
    async def test_range_comma_and_all(self):
        H = _imp("handlers.spirit_farm_handlers").SpiritFarmHandlers
        mgr = _Mgr()
        handler = H(None, mgr)
        p = types.SimpleNamespace(user_id="u1")
        for raw, expected in (("1-3", [1, 2, 3]), ("1,2,5", [1, 2, 5]), ("all", [1, 2, 5])):
            event = _Event()
            results = [
                x async for x in handler.handle_remove_crop.__wrapped__(
                    handler, p, event, raw
                )
            ]
            self.assertEqual(mgr.removed, expected)
            self.assertEqual(results, ["ok"])


if __name__ == "__main__":
    unittest.main()
