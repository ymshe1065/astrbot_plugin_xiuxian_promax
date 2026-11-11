# tests/test_list_unlock_label.py
# 秘境 / 历练列表的「解锁境界」展示：境界名 + 境界等级
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


def _imp(name):
    return __import__(f"{PKG}.{name}", fromlist=["*"])


class TestRealmLevelLabel(unittest.TestCase):
    """只写「合体期」看不出是初/中/后期；只写「25级」不知道是哪个境界。"""

    def setUp(self):
        self.levels = _imp("config_manager").ConfigManager(ROOT).level_data
        self.fn = _imp("core.realm_rules").level_label

    def test_name_and_index_together(self):
        self.assertEqual(self.fn(self.levels, 25), "合体期初期·25级")
        self.assertEqual(self.fn(self.levels, 27), "合体期后期·27级")

    def test_missing_level_table_keeps_number_only(self):
        """配置没加载时不能拼出「境界25·25级」这种把数字写两遍的兜底。"""
        self.assertEqual(self.fn([], 25), "25级")

    def test_invalid_index_returns_empty(self):
        self.assertEqual(self.fn(self.levels, -1), "")
        self.assertEqual(self.fn(self.levels, None), "")


class TestRiftListLevelLabel(unittest.TestCase):
    def setUp(self):
        cm = _imp("config_manager").ConfigManager(ROOT)
        self.levels = cm.level_data
        self.mgr = _imp("managers.rift_manager").RiftManager(None, cm, None)

    def test_level_label_uses_realm_name(self):
        self.assertEqual(self.mgr._level_label(25), "合体期初期·25级")

    def test_row_puts_unlock_first(self):
        row = {
            "title": "天雷绝域·一重",
            "required_level": 25,
            "minutes": 20,
            "facet": "矿材",
            "exp": 2500,
            "gold": 100,
            "level_text": self.mgr._level_label(25),
        }
        self.mgr._decorate_rift_row(row, 3)
        # 网页端按 rows 顺序渲染，解锁境界排第一行
        self.assertEqual(row["rows"][0], ["解锁", "合体期初期·25级"])
        self.assertIn("合体期初期·25级", row["line"])

    def test_locked_text_names_realm(self):
        row = {"required_level": 35, "level_text": self.mgr._level_label(35)}
        self.assertIn("混元大罗金仙", row["level_text"])
        self.assertIn("35级", row["level_text"])


class TestRouteListLevelLabel(unittest.TestCase):
    def setUp(self):
        cm = _imp("config_manager").ConfigManager(ROOT)
        self.levels = cm.level_data
        self.mgr = _imp("managers.adventure_manager").AdventureManager(None, None, cm)

    def test_level_label_uses_realm_name(self):
        self.assertEqual(self.mgr._level_label(26), "合体期中期·26级")

    def test_locked_row_lock_text_has_realm_and_level(self):
        import asyncio

        data = asyncio.run(self.mgr.build_route_list(""))
        locked = next(g for g in data["groups"] if g["key"] == "locked")
        for item in locked["items"]:
            self.assertTrue(item["lock_text"].startswith("需"))
            self.assertIn("级", item["lock_text"])
            # 「需境界 20」这种只有数字的旧文案不许再出现
            self.assertNotIn("需境界 ", item["lock_text"])
            self.assertEqual(item["level_text"], self.mgr._level_label(item["level"]))

    def test_rows_carry_unlock_line(self):
        import asyncio

        data = asyncio.run(self.mgr.build_route_list(""))
        item = data["groups"][0]["items"][0]
        self.assertEqual(item["rows"][0][0], "解锁")
        self.assertIn("级", item["rows"][0][1])


if __name__ == "__main__":
    unittest.main()
