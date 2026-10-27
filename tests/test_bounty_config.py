# tests/test_bounty_config.py
# 悬赏配置与历练事件完整性检查：防止模板冲突、不可推进标签与重复事件
import json
import sys
import types
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")


class _StubLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
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


class TestBountyConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bounty = json.loads(
            (ROOT / "config" / "bounty_templates.json").read_text(encoding="utf-8")
        )
        cls.adventure = json.loads(
            (ROOT / "config" / "adventure_config.json").read_text(encoding="utf-8")
        )
        cls.BountyManager = _imp("managers.bounty_manager").BountyManager

    def test_template_ids_and_difficulties_are_valid(self):
        ids = [tpl["id"] for tpl in self.bounty["templates"]]
        self.assertEqual(len(ids), len(set(ids)), "悬赏模板 ID 不能重复")

        difficulties = set(self.bounty["difficulties"])
        for tpl in self.bounty["templates"]:
            self.assertIn(tpl["difficulty"], difficulties)
            self.assertLessEqual(tpl["min_target"], tpl["max_target"])
            self.assertGreater(tpl["weight"], 0)
            self.assertGreater(tpl["time_limit"], 0)
            self.assertTrue(tpl["name"])
            self.assertTrue(tpl["description"])

    def test_progress_tags_are_triggerable(self):
        triggerable = self.BountyManager.TRIGGERABLE_TAGS
        for tpl in self.bounty["templates"]:
            tags = [tag.lower() for tag in tpl["progress_tags"]]
            self.assertTrue(tags, f"{tpl['name']} 缺少进度标签")
            self.assertTrue(
                set(tags) <= triggerable,
                f"{tpl['name']} 含不可推进标签：{sorted(set(tags) - triggerable)}",
            )

    def test_item_tables_exist(self):
        tables = set(self.bounty["item_tables"])
        for tpl in self.bounty["templates"]:
            self.assertIn(tpl.get("item_table", "gather"), tables)

    def test_difficulty_plan_uses_configured_min_level(self):
        mgr = object.__new__(self.BountyManager)
        mgr.difficulties = {
            "easy": {"min_level": 0},
            "normal": {"min_level": 3},
            "hard": {"min_level": 7},
            "elite": {"min_level": 12},
        }
        self.assertEqual(mgr._get_difficulty_plan(0), ["easy"])
        self.assertEqual(mgr._get_difficulty_plan(3), ["easy", "normal"])
        self.assertEqual(mgr._get_difficulty_plan(7), ["easy", "normal", "hard"])
        self.assertEqual(
            mgr._get_difficulty_plan(12), ["easy", "normal", "hard", "elite"]
        )

    def test_sanitize_drops_duplicate_and_invalid_templates(self):
        mgr = object.__new__(self.BountyManager)
        mgr.difficulties = {"easy": {"min_level": 0}}
        mgr.item_tables = {"gather": []}
        templates = [
            {
                "id": 1,
                "name": "正常",
                "difficulty": "easy",
                "progress_tags": ["adventure_scout"],
                "min_target": 2,
                "max_target": 1,
                "item_table": "missing",
            },
            {
                "id": 1,
                "name": "重复",
                "difficulty": "easy",
                "progress_tags": ["adventure_scout"],
            },
            {
                "id": 2,
                "name": "未知难度",
                "difficulty": "nightmare",
                "progress_tags": ["adventure_scout"],
            },
            {
                "id": 3,
                "name": "无标签",
                "difficulty": "easy",
                "progress_tags": [],
            },
        ]
        cleaned = mgr._sanitize_templates(templates)
        self.assertEqual([tpl["id"] for tpl in cleaned], [1])
        self.assertEqual(cleaned[0]["min_target"], 2)
        self.assertEqual(cleaned[0]["max_target"], 2)
        self.assertEqual(cleaned[0]["item_table"], "gather")


class TestAdventureConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adventure = json.loads(
            (ROOT / "config" / "adventure_config.json").read_text(encoding="utf-8")
        )
        cls.triggerable = _imp("managers.bounty_manager").BountyManager.TRIGGERABLE_TAGS

    def test_routes_are_unique_and_tags_are_triggerable(self):
        keys = [route["key"] for route in self.adventure["routes"]]
        self.assertEqual(len(keys), len(set(keys)), "历练路线 key 不能重复")
        for route in self.adventure["routes"]:
            self.assertIn(route.get("bounty_tag"), self.triggerable)
            self.assertTrue(set(route.get("event_weights", {})) <= {"safe", "standard", "risky"})

    def test_route_aliases_do_not_conflict(self):
        index = {}
        for route in self.adventure["routes"]:
            aliases = set(route.get("aliases", [])) | {route["key"], route["name"]}
            for alias in aliases:
                normalized = alias.strip().lower()
                self.assertTrue(normalized, f"{route['key']} 存在空别名")
                if normalized in index:
                    self.assertEqual(
                        index[normalized],
                        route["key"],
                        f"别名冲突：{alias} 同时指向 {index[normalized]} 与 {route['key']}",
                    )
                index[normalized] = route["key"]

    def test_event_groups_are_complete_and_unique(self):
        groups = self.adventure["event_groups"]
        self.assertTrue(set(groups) >= {"safe", "standard", "risky"})
        keys = []
        for group_name, events in groups.items():
            self.assertTrue(events, f"事件组 {group_name} 不能为空")
            for event in events:
                keys.append(event["key"])
                self.assertTrue(event["name"])
                self.assertTrue(event["desc"])
                self.assertGreater(event.get("exp_mult", 0), 0)
                self.assertGreater(event.get("gold_mult", 0), 0)
                self.assertGreaterEqual(event.get("item_chance", 0), 0)
                self.assertLessEqual(event.get("item_chance", 100), 100)
                self.assertGreaterEqual(event.get("bonus_progress", 0), 0)
                if "drop_tier" in event:
                    self.assertIn(event["drop_tier"], {"low", "mid", "high"})
        duplicates = [key for key, count in Counter(keys).items() if count > 1]
        self.assertEqual(duplicates, [], f"事件 key 重复：{duplicates}")

    def test_route_event_group_references_exist(self):
        groups = set(self.adventure["event_groups"])
        for route in self.adventure["routes"]:
            for group_name in route.get("event_weights", {}):
                self.assertIn(group_name, groups)


if __name__ == "__main__":
    unittest.main()
