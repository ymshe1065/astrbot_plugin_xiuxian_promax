# tests/test_drop_balance.py
"""世界Boss/团本/江湖事件掉落品质与档位回归测试。"""
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestDropBalance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.WorldBossManager = _imp("managers.world_boss_manager").WorldBossManager
        cls.RaidManager = _imp("managers.raid_manager").RaidManager
        cls.BattleEventManager = _imp("managers.battle_event_manager").BattleEventManager
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.Player = _imp("models").Player
        cls.ItemInstanceManager = _imp("core.item_instance_manager").ItemInstanceManager

        prices = {}
        items = json.loads((ROOT / "config" / "items.json").read_text(encoding="utf-8"))
        for item in items.values():
            if isinstance(item, dict) and item.get("name"):
                prices[item["name"]] = int(item.get("price") or 0)
        materials = json.loads(
            (ROOT / "config" / "material_catalog.json").read_text(encoding="utf-8")
        )
        cls.names = set(prices)
        for item in materials.get("materials", []):
            if isinstance(item, dict) and item.get("name"):
                cls.names.add(item["name"])
                prices.setdefault(item["name"], int(item.get("price") or 0))
        cls.prices = prices

    def _table_ev(self, table):
        total_weight = sum(max(1, int(item.get("weight", 1))) for item in table)
        expected = 0.0
        for item in table:
            count = (int(item.get("min", 1)) + int(item.get("max", 1))) / 2
            expected += (
                self.prices.get(item["name"], 0)
                * count
                * max(1, int(item.get("weight", 1)))
                / total_weight
            )
        return expected

    def test_drop_tables_have_strictly_increasing_value(self):
        tables = self.WorldBossManager.DROP_TABLES
        ev = {key: self._table_ev(table) for key, table in tables.items()}
        self.assertIn("legend", tables)
        self.assertLess(ev["low"], ev["mid"], ev)
        self.assertLess(ev["mid"], ev["high"], ev)
        self.assertLess(ev["high"], ev["legend"], ev)
        self.assertAlmostEqual(ev["low"], 551, delta=5)
        self.assertAlmostEqual(ev["mid"], 1065, delta=5)
        self.assertAlmostEqual(ev["high"], 1701, delta=5)
        self.assertAlmostEqual(ev["legend"], 4544, delta=5)

    def test_drop_items_exist_and_high_tier_has_saint_material(self):
        saint = {
            item["name"]
            for item in self.cm.material_catalog.values()
            if isinstance(item, dict) and item.get("tier") == "圣品"
        }
        emperor = {
            item["name"]
            for item in self.cm.material_catalog.values()
            if isinstance(item, dict) and item.get("tier") == "帝品"
        }
        for table in self.WorldBossManager.DROP_TABLES.values():
            for item in table:
                self.assertIn(item["name"], self.names)
        for key in ("high", "legend"):
            names = {item["name"] for item in self.WorldBossManager.DROP_TABLES[key]}
            self.assertTrue(names & saint, f"{key} 缺少圣品掉落")
        legend_names = {item["name"] for item in self.WorldBossManager.DROP_TABLES["legend"]}
        self.assertTrue(legend_names & emperor, "legend 缺少帝品材料掉落")

    def test_top_content_uses_highest_table(self):
        boss_types = self.WorldBossManager.BOSS_TYPES
        self.assertEqual(boss_types["venom_queen"]["drop_table"], "legend")
        self.assertEqual(boss_types["thunder_qilin"]["drop_table"], "legend")
        self.assertEqual(self.RaidManager.RAID_TYPES["chaos_palace"]["drop_table"], "legend")
        self.assertEqual(self.BattleEventManager.TIER_META[5]["drop_table"], "legend")
        self.assertEqual(self.RaidManager._drop_table_for_difficulty(1), "mid")
        self.assertEqual(self.RaidManager._drop_table_for_difficulty(2), "high")
        self.assertEqual(self.RaidManager._drop_table_for_difficulty(3), "legend")

    def test_equipment_reward_keeps_rolled_quality(self):
        mgr = self.WorldBossManager(None, self.cm, None)
        mgr.EQUIPMENT_CHANCES = {
            "world_boss": {"killer": (100, 0), "top3": (0, 0), "participate": (0, 0)}
        }
        mgr.EQUIPMENT_CHANCE_CAPS = {"killer": 100, "top3": 0, "participate": 0}
        player = self.Player(user_id="u1", level_index=0)
        drop = mgr.roll_equipment_reward(player, "killer", 1, source="world_boss")
        self.assertIsNotNone(drop)
        self.assertIn(drop["quality"], self.ItemInstanceManager.QUALITY_MULTIPLIER)


if __name__ == "__main__":
    unittest.main()
