"""新老装备分离与物理/法术/混合/守护倾向回归。"""
import json
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import ROOT, _imp


class TestEquipmentProfiles(unittest.TestCase):
    def setUp(self):
        self.balance = _imp("core.equipment_balance")
        self.items = json.loads((ROOT / "config" / "items_v2.json").read_text(encoding="utf-8"))

    def test_new_items_have_name_price_and_description(self):
        # 新装备已扩展到全部 12 品阶（8 槽位 × 4 职业倾向 × 12 品阶 = 384 条）
        self.assertEqual(len(self.items), 384)
        names = set()
        for item in self.items.values():
            self.assertTrue(item.get("name"))
            self.assertNotIn(item["name"], names)
            names.add(item["name"])
            # 全部新装备按《经济基准表》E(L)×劳动天数定价
            self.assertGreater(item.get("price", 0), 0)
            self.assertTrue(item.get("description"))
            self.assertEqual(item.get("balance_version"), 2)
            self.assertIn(item.get("profile"), self.balance.PROFILE_WEIGHTS)
            self.assertNotIn("equipment_rates", item)

    def test_profile_splits_keep_total_budget(self):
        for item in self.items.values():
            rates = self.balance.equipment_rates(item)
            slot = _imp("core.item_rules").ItemRules.get_equip_slot(item)
            attack, defense = self.balance.SLOT_BUDGET[slot]
            self.assertAlmostEqual(
                rates["physical_damage"] + rates["magic_damage"], attack, places=6
            )
            self.assertAlmostEqual(
                rates["physical_defense"] + rates["magic_defense"], defense, places=6
            )

    def test_old_explicit_rates_stay_fifty_fifty(self):
        old = {"type": "weapon", "equipment_rates": {
            "physical_damage": .18, "magic_damage": .18,
            "physical_defense": 0, "magic_defense": 0,
        }}
        rates = self.balance.equipment_rates(old)
        self.assertEqual(rates["physical_damage"], .18)
        self.assertEqual(rates["magic_damage"], .18)
        self.assertEqual(self.balance.get_equipment_profile(old), "legacy")

    def test_new_gear_loaded_into_drop_pool(self):
        _imp("data")
        cm = _imp("config_manager").ConfigManager(ROOT)
        new_names = {item["name"] for item in self.items.values()}
        self.assertTrue(new_names.issubset(set(cm.items_data)))
        pool = _imp("core.equipment_drop").build_equipment_pool(cm)
        pooled = {
            entry["name"]
            for entries in pool.values()
            for entry in entries
        }
        self.assertTrue(new_names.issubset(pooled))

    def test_profile_aliases(self):
        for raw, expected in (
            ("物理", "physical"), ("法系", "magic"),
            ("双修", "hybrid"), ("守护", "guardian"),
        ):
            self.assertEqual(
                self.balance.get_equipment_profile({"profile": raw}), expected
            )


if __name__ == "__main__":
    unittest.main()
