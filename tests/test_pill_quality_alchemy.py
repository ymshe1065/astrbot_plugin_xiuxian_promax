# 丹药品质、旧丹药兼容、战斗接入与丹方回归测试
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

    async def update_player(self, player, commit=True):
        self.player = player
        return None


class _FakeExt:
    async def get_sect_by_id(self, sect_id):
        return None


class _FakeDBExt(_FakeDB):
    def __init__(self, player):
        super().__init__(player)
        self.ext = _FakeExt()


class TestPillQuality(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.config_manager = _imp("config_manager").ConfigManager(ROOT)

    async def test_highest_quality_selected_and_exp_scaled(self):
        models = _imp("models")
        pill_module = _imp("core.pill_manager")
        player = models.Player(user_id="u1", level_index=5, experience=0)
        player.pills_inventory = json.dumps({"炼气丹|良好": 1, "炼气丹|大师": 1}, ensure_ascii=False)
        db = _FakeDB(player)
        pm = pill_module.PillManager(db, self.config_manager)
        ok, msg = await pm.use_pill(player, "炼气丹")
        self.assertTrue(ok)
        self.assertIn("炼气丹·大师", msg)
        self.assertEqual(player.experience, 800)
        inventory = player.get_pills_inventory()
        self.assertNotIn("炼气丹|大师", inventory)
        self.assertIn("炼气丹|良好", inventory)

    async def test_legacy_effect_pill_enters_inventory_path(self):
        models = _imp("models")
        pill_module = _imp("core.pill_manager")
        player = models.Player(user_id="u1", level_index=0, spiritual_qi=0, max_spiritual_qi=1000)
        player.pills_inventory = json.dumps({"造化丹|一般": 1}, ensure_ascii=False)
        db = _FakeDB(player)
        pm = pill_module.PillManager(db, self.config_manager)
        ok, msg = await pm.use_pill(player, "造化丹")
        self.assertTrue(ok)
        self.assertGreater(player.spiritual_qi, 0)
        self.assertNotIn("造化丹|一般", player.get_pills_inventory())

    async def test_temporary_pill_applies_to_unified_combat_stats(self):
        models = _imp("models")
        combat = _imp("managers.combat_manager").CombatManager
        player = models.Player(user_id="u1", level_index=5, experience=100000, spiritual_qi=500, max_spiritual_qi=500)
        player.active_pill_effects = json.dumps([{
            "pill_name": "测试增伤丹", "subtype": "combat_boost",
            "expiry_time": 9999999999, "physical_damage_multiplier": 0.5,
        }], ensure_ascii=False)
        db = _FakeDBExt(player)
        boosted = combat.build_player_combat_stats(player, self.config_manager, db=db)
        plain = combat.build_player_combat_stats(
            models.Player(user_id="u2", level_index=5, experience=100000,
                          spiritual_qi=500, max_spiritual_qi=500),
            self.config_manager, db=_FakeDBExt(models.Player(user_id="u2")),
        )
        self.assertGreater(boosted.max_hp, plain.max_hp)
        self.assertGreater(boosted.atk, plain.atk)

    def test_recipe_scrolls_injected(self):
        scrolls = [name for name, item in self.config_manager.items_data.items() if item.get("type") == "丹方"]
        self.assertTrue(scrolls)
        self.assertIn("《筑基丹方》", scrolls)
        recipe = self.config_manager.get_recipe_by_scroll("《筑基丹方》")
        self.assertIsNotNone(recipe)
        self.assertEqual(recipe.get("output_pill"), "筑基丹")
        packages = [i for i in self.config_manager.items_data.values() if i.get("type") == "材料包"]
        self.assertEqual(len(packages), 16)

    def test_root_alchemy_bonus(self):
        alchemy = _imp("managers.alchemy_manager").AlchemyManager
        models = _imp("models")
        player = models.Player(user_id="u1", spiritual_root="木灵根")
        mgr = alchemy(_FakeDB(player), self.config_manager, None)
        bonus = mgr.get_alchemy_bonus(player)
        self.assertGreater(bonus["success_rate"], 0)
        player.spiritual_root = "火灵根"
        bonus = mgr.get_alchemy_bonus(player)
        self.assertGreater(bonus["perfect_rate"], 0)


if __name__ == "__main__":
    unittest.main()
