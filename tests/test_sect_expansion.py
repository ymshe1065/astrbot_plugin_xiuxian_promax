# tests/test_sect_expansion.py
# 宗门拓展的纯逻辑测试（不依赖真实数据库）
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


class TestSectExpansion(unittest.TestCase):
    def setUp(self):
        self.SectManager = _imp("managers.sect_manager").SectManager
        self.Sect = _imp("models_extended").Sect
        self.Player = _imp("models").Player
        self.mgr = self.SectManager(None, None)

    def _sect(self, exp=0, level=1, buildings="{}"):
        return self.Sect(
            sect_id=1, sect_name="测试宗", sect_owner="u1",
            sect_exp=exp, sect_level=level, buildings=buildings,
        )

    def test_level_thresholds(self):
        sect = self._sect(exp=0)
        self.assertEqual(self.mgr.get_sect_level(sect), 1)
        sect.sect_exp = 10000
        self.assertEqual(self.mgr.apply_sect_exp(sect, 0)[1], 2)
        sect.sect_exp = 60000
        self.assertEqual(self.mgr.apply_sect_exp(sect, 0)[1], 4)
        sect.sect_exp = 100000
        self.assertEqual(self.mgr.apply_sect_exp(sect, 0)[1], 5)

    def test_building_limit(self):
        self.assertEqual(self.mgr.get_building_limit(self._sect(level=1)), 2)
        self.assertEqual(self.mgr.get_building_limit(self._sect(level=4)), 5)
        self.assertEqual(self.mgr.get_building_limit(self._sect(level=5)), 5)

    def test_order_position_limits(self):
        self.assertEqual(self.mgr._position_limit(0, 5), 8)
        self.assertEqual(self.mgr._position_limit(1, 5), 3)
        self.assertEqual(self.mgr._position_limit(2, 5), 1)
        self.assertEqual(self.mgr._position_limit(3, 5), 0)

    def test_war_sort_low_to_high(self):
        players = [
            self.Player("outer", sect_position=4),
            self.Player("leader", sect_position=0),
            self.Player("elder", sect_position=1),
            self.Player("inner", sect_position=3),
        ]
        order = [p.user_id for p in self.mgr._war_sort(players)]
        self.assertEqual(order, ["outer", "inner", "elder", "leader"])

    def test_level_up_text_unlocks_shop(self):
        text = self.mgr.get_level_up_text(1, 2)
        self.assertIn("商堂", text)


if __name__ == "__main__":
    unittest.main()
