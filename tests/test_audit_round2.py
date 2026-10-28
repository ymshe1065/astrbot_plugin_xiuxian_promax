import asyncio
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name


class _L:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")
api_mod.logger = _L()
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


class _FakeEvent:
    def plain_result(self, text):
        return text

    def get_sender_id(self):
        return "u1"


class TestContentSanity(unittest.TestCase):
    def test_generated_item_rates_are_sane(self):
        data = json.loads((ROOT / "config" / "items.json").read_text(encoding="utf-8"))
        for item in data.values():
            if not isinstance(item, dict):
                continue
            effects = item.get("effects") or {}
            for key in ("crit_rate", "lifesteal", "combo_rate", "counter_damage", "dodge"):
                if key in effects:
                    self.assertGreaterEqual(effects[key], 0, item.get("name"))
                    self.assertLessEqual(effects[key], 100, item.get("name"))
            exp_multiplier = item.get("exp_multiplier")
            if isinstance(exp_multiplier, (int, float)):
                self.assertLessEqual(exp_multiplier, 0.5, item.get("name"))

    def test_combat_clamps_invalid_rate_effects(self):
        CombatManager = _imp("managers.combat_manager").CombatManager
        Player = _imp("models").Player

        class Config:
            techniques_data = {}
            items_data = {}
            weapons_data = {
                "bad_weapon": {
                    "effects": {"crit_rate": 5000, "lifesteal": 2000, "combo_rate": -5}
                }
            }

        player = Player(user_id="u1", weapon="bad_weapon")
        effects = CombatManager.collect_combat_effects(player, Config())
        self.assertLessEqual(effects["crit_rate"], 100)
        self.assertLessEqual(effects["lifesteal"], 100)
        self.assertEqual(effects["combo_rate"], 0)

    def test_combat_includes_base_defense(self):
        CombatManager = _imp("managers.combat_manager").CombatManager
        Player = _imp("models").Player
        player = Player(
            user_id="u1",
            experience=10000,
            physical_defense=100,
            magic_defense=50,
            physical_damage=0,
            magic_damage=0,
        )
        stats = CombatManager.build_player_combat_stats(player, config_manager=None)
        base = _imp("core.equipment_balance").realm_attributes(player)
        defense = base["physical_defense"] + base["magic_defense"]
        self.assertEqual(stats.defense, defense + int(defense * .30))


class TestManagerRegressions(unittest.TestCase):
    def test_daily_pool_is_stable_for_same_user_and_day(self):
        DailyTaskManager = _imp("managers.daily_task_manager").DailyTaskManager
        mgr = DailyTaskManager(None)
        first = [(i, t["title"]) for i, t in mgr.get_daily_pool("u1", "地")]
        second = [(i, t["title"]) for i, t in mgr.get_daily_pool("u1", "地")]
        self.assertEqual(first, second)

    def test_unknown_adventure_route_is_not_silently_defaulted(self):
        AdventureManager = _imp("managers.adventure_manager").AdventureManager
        mgr = AdventureManager(None)
        self.assertEqual(mgr._resolve_route("不存在的路线"), "")
        self.assertNotEqual(mgr._resolve_route("巡山问道"), "")

    def test_abandon_handlers_do_not_raise_name_error(self):
        SectHandlers = _imp("handlers.sect_handlers").SectHandlers
        AdventureHandlers = _imp("handlers.adventure_handlers").AdventureHandlers
        RiftHandlers = _imp("handlers.rift_handlers").RiftHandlers

        class SectMgr:
            async def finish_sect_task(self, user_id):
                return True, "ok"

            async def abandon_sect_task(self, user_id):
                return True, "ok"

        class AdventureMgr:
            async def abandon_adventure(self, user_id):
                return True, "ok"

        class RiftMgr:
            async def exit_rift(self, user_id):
                return True, "ok"

        async def run(handler, method):
            return [x async for x in getattr(handler, method)(_FakeEvent())]

        self.assertEqual(
            asyncio.run(run(SectHandlers(None, SectMgr()), "handle_finish_sect_task")), ["ok"]
        )
        self.assertEqual(
            asyncio.run(run(SectHandlers(None, SectMgr()), "handle_abandon_sect_task")), ["ok"]
        )
        self.assertEqual(
            asyncio.run(run(AdventureHandlers(None, AdventureMgr()), "handle_abandon_adventure")), ["ok"]
        )
        self.assertEqual(
            asyncio.run(run(RiftHandlers(None, RiftMgr()), "handle_rift_exit")), ["ok"]
        )


if __name__ == "__main__":
    unittest.main()
