# tests/test_ranking.py
"""排行榜回归测试：战力口径与旧存档脏数据兼容。"""

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    def __init__(self, players):
        self.players = players
        self.ext = SimpleNamespace(get_all_impart_info=self._get_impart_info)

    async def get_all_players(self):
        return self.players

    async def _get_impart_info(self):
        return {}


class _FakeEquipmentManager:
    def get_equipped_items(self, *args, **kwargs):
        return []

    def get_base_attributes(self, player):
        return {}

    def get_combat_effects(self, player):
        return {}


class _FakePetManager:
    def __init__(self, db):
        self.db = db

    async def get_all_pets_grouped(self):
        return {"u1": [{"is_main": 1}]}

    def summarize_lineup(self, *args, **kwargs):
        return 1000, 5000, {}, 0


class TestRankingCalculations(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.rank_mod = _imp("managers.ranking_manager")
        cls.Player = _imp("models").Player

    def _manager(self, db):
        mgr = object.__new__(self.rank_mod.RankingManager)
        mgr.db = db
        mgr.config_manager = SimpleNamespace(
            items_data={},
            weapons_data={},
            get_level_data=lambda cultivation_type="灵修": [
                {"level_name": "炼气期一层", "exp_needed": 0}
            ],
        )
        mgr.combat_mgr = None
        mgr.equipment_manager = _FakeEquipmentManager()
        return mgr

    def _player(self, **overrides):
        data = {
            "user_id": "u1",
            "user_name": "甲",
            "level_index": 1,
            "experience": 10,
            "gold": 100,
            "cultivation_type": "灵修",
            "permanent_bonus": "{}",
        }
        data.update(overrides)
        return SimpleNamespace(**data)

    async def test_power_ranking_matches_name_card_pet_rule(self):
        """灵兽独立出手，主人战力只同步主战特效，不叠加灵兽攻击。"""
        player = self._player(
            get_level=lambda cm: "炼气期二层",
            get_required_exp=lambda cm: 500,
            get_total_attributes=lambda *a, **k: {
                "physical_damage": 11,
                "magic_damage": 12,
                "mental_power": 100,
            },
        )
        mgr = self._manager(_FakeDB([player]))

        with patch.object(self.rank_mod, "CombatManager") as combat_mock, patch(
            f"{PKG}.core.PillManager", create=True
        ) as pill_mock, patch(
            f"{PKG}.managers.pet_manager.PetManager", _FakePetManager
        ):
            pill_mock.return_value.calculate_pill_attribute_effects.return_value = {}
            combat_mock.build_combat_panel.return_value = {
                "hp": 1000,
                "atk": 1000,
                "defense": 100,
            }
            combat_mock.calculate_combat_power.return_value = 1234

            success, message = await mgr.get_power_ranking(viewer_id="u1")

        self.assertTrue(success)
        self.assertIn("1. 甲 · 1,234 ← 你", message)
        self.assertIn("共 1 名修士", message)
        final_owner_atk = combat_mock.apply_pet_lineup.call_args.args[4]
        self.assertEqual(final_owner_atk, 1000)

    async def test_power_ranking_shows_outside_viewer_rank(self):
        p1 = self._player(
            user_id="u1", user_name="甲", get_level=lambda cm: "一层",
            get_total_attributes=lambda *a, **k: {
                "physical_damage": 1, "magic_damage": 1, "mental_power": 10,
            },
        )
        p2 = self._player(
            user_id="u2", user_name="乙", get_level=lambda cm: "一层",
            get_total_attributes=lambda *a, **k: {
                "physical_damage": 1, "magic_damage": 1, "mental_power": 10,
            },
        )
        mgr = self._manager(_FakeDB([p1, p2]))

        with patch.object(self.rank_mod, "CombatManager") as combat_mock, patch(
            f"{PKG}.core.PillManager", create=True
        ) as pill_mock, patch(
            f"{PKG}.managers.pet_manager.PetManager", _FakePetManager
        ):
            pill_mock.return_value.calculate_pill_attribute_effects.return_value = {}
            combat_mock.build_combat_panel.return_value = {
                "hp": 100, "atk": 100, "defense": 10,
            }
            combat_mock.calculate_combat_power.side_effect = [100, 50]
            success, message = await mgr.get_power_ranking(limit=1, viewer_id="u2")

        self.assertTrue(success)
        self.assertIn("1. 甲 · 100", message)
        self.assertNotIn("1. 甲 · 100 ← 你", message)
        self.assertIn("你的排名：第 2 名 · 乙 · 50", message)

    async def test_rankings_tolerate_null_legacy_fields(self):
        player = self._player(
            level_index=None,
            experience=None,
            gold=None,
            get_level=lambda cm: "未知",
            get_required_exp=lambda cm: None,
        )
        mgr = self._manager(_FakeDB([player]))

        level_ok, level_msg = await mgr.get_level_ranking()
        wealth_ok, wealth_msg = await mgr.get_wealth_ranking()

        self.assertTrue(level_ok)
        self.assertIn("境界：未知", level_msg)
        self.assertTrue(wealth_ok)
        self.assertIn("灵石：0", wealth_msg)


if __name__ == "__main__":
    unittest.main()
