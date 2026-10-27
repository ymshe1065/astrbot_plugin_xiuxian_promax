# 物品规则与战斗入口一致性回归测试
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


class TestItemRules(unittest.TestCase):
    def setUp(self):
        self.rules = _imp("core.item_rules")

    def test_normalize_shop_type(self):
        norm = self.rules.normalize_shop_type
        self.assertEqual(norm({"type": "法器", "subtype": "武器"}), "weapon")
        self.assertEqual(norm({"type": "法器", "subtype": "防具"}), "armor")
        self.assertEqual(norm({"type": "法器", "subtype": "饰品"}), "accessory")
        self.assertEqual(norm({"type": "功法"}), "technique")
        self.assertEqual(norm({"type": "丹药", "effect": {"add_hp": 1}}), "legacy_pill")
        self.assertEqual(norm({"type": "丹药", "effect_type": "temporary"}), "inventory_pill")
        self.assertEqual(norm({"type": "legacy_pill", "data": {"effect_type": "temporary"}}), "inventory_pill")

    def test_required_level_prefers_explicit(self):
        get_req = self.rules.get_required_level
        self.assertEqual(get_req({"rank": "天品", "required_level_index": 13}), 13)
        self.assertEqual(get_req({"rank": "天品", "required_level_index": 13, "data": {}}), 13)
        self.assertEqual(get_req({"rank": "天阶"}), 18)
        self.assertEqual(get_req({"rank": "天阶", "data": {"required_level_index": 13}}), 13)
        self.assertEqual(get_req({"rank": "凡品", "required_level_index": 0}), 0)

    def test_normalize_equipment_type(self):
        norm = self.rules.normalize_equipment_type
        self.assertEqual(norm({"type": "法器", "subtype": "武器"}), "weapon")
        self.assertEqual(norm({"type": "法器", "subtype": "防具"}), "armor")
        self.assertEqual(norm({"type": "功法"}), "technique")
        self.assertEqual(norm({"type": "心法"}), "main_technique")
        self.assertEqual(norm({"type": "main_technique"}), "main_technique")

    def test_validate_rate(self):
        ok, _ = self.rules.validate_rate("crit_rate", 5)
        self.assertTrue(ok)
        ok, _ = self.rules.validate_rate("crit_rate", 500)
        self.assertFalse(ok)


class TestCombatHelper(unittest.TestCase):
    def test_helper_reads_equipment_and_effects(self):
        Player = _imp("models").Player
        CombatManager = _imp("managers.combat_manager").CombatManager

        class Config:
            weapons_data = {
                "测试剑": {
                    "type": "weapon",
                    "physical_damage": 1000,
                    "magic_damage": 0,
                    "effects": {"crit_rate": 5},
                }
            }
            items_data = {
                "测试甲": {"type": "法器", "subtype": "防具", "physical_defense": 500}
            }
            techniques_data = {}

            def get_item_element(self, name):
                return ""

        player = Player(user_id="u1", user_name="测试道友", experience=10000)
        player.weapon = "测试剑"
        player.armor = "测试甲"
        player.permanent_bonus = "{}"

        stats = CombatManager.build_player_combat_stats(player, Config())
        self.assertGreater(stats.atk, 1000)
        self.assertGreater(stats.defense, 0)
        self.assertEqual(stats.effects.get("crit_rate"), 5)
