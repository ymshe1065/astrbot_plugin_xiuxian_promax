"""经济重构回归：押镖方案A曲线、丹方材料可解析、材料单一数据源、材料包分布、拍卖起拍价、基准表同步。"""
import json
import subprocess
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


class TestEconomyRebuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cm = _imp("config_manager").ConfigManager(ROOT)

    def test_all_recipe_specs_resolve_to_catalog_materials(self):
        catalog = {}
        for material in self.cm.material_catalog.values():
            catalog.setdefault((material.get("category"), material.get("tier")), set()).add(
                material.get("name")
            )
        for rid, recipe in self.cm.pill_recipes_data.items():
            raw = recipe.get("materials")
            specs = [{"name": k, "count": v} for k, v in raw.items()] if isinstance(raw, dict) else (raw or [])
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                if spec.get("name"):
                    self.assertIn(
                        spec["name"], self.cm.material_catalog,
                        f"丹方 {rid} 引用不存在的具名材料 {spec['name']}",
                    )
                else:
                    key = (spec.get("category"), spec.get("tier"))
                    self.assertTrue(
                        key in catalog and catalog[key],
                        f"丹方 {rid} 的 {key} 在材料目录中没有对应材料",
                    )

    def test_escort_gold_plan_a_curve(self):
        manager = _imp("managers.escort_manager").EscortManager
        self.assertEqual(manager.calculate_escort_gold_base(3), 9000)
        self.assertEqual(manager.calculate_escort_gold_base(10), 14500)
        self.assertEqual(manager.calculate_escort_gold_base(16), 36000)
        self.assertEqual(manager.calculate_escort_gold_base(35), 405000)
        values = [manager.calculate_escort_gold_base(i) for i in range(3, 36)]
        self.assertEqual(values, sorted(values), "镖银应随境界单调不降")
        self.assertAlmostEqual(values[-1] / values[0], 45.0, delta=0.01)

    def test_package_open_weights(self):
        shop = _imp("core.shop_manager").ShopManager({}, self.cm)
        self.assertEqual([count for count, _ in shop.PACKAGE_OPEN_WEIGHTS], [1, 2, 3])
        self.assertEqual(sum(weight for _, weight in shop.PACKAGE_OPEN_WEIGHTS), 100)

    def test_catalog_price_is_single_source(self):
        name = "噬魂铁"
        original = self.cm.material_catalog[name]["price"]
        try:
            self.cm.material_catalog[name]["price"] = 12345
            self.assertEqual(self.cm.get_item_price(name), 12345)
        finally:
            self.cm.material_catalog[name]["price"] = original

    def test_auction_start_price_is_base(self):
        cfg = json.loads((ROOT / "config" / "auction_house_config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["start_price_rate"], 1.0)
        self.assertEqual(cfg["start_price_fluctuation"], 0.0)
        defaults = _imp("data.default_configs").AUCTION_HOUSE_CONFIG
        self.assertEqual(defaults["start_price_rate"], 1.0)
        self.assertEqual(defaults["start_price_fluctuation"], 0.0)

    def test_legacy_gear_rename_map_resolves_to_new_items(self):
        rename = json.loads(
            (ROOT / "data" / "legacy_gear_rename.json").read_text(encoding="utf-8")
        )
        self.assertGreater(len(rename), 500)
        v2 = json.loads((ROOT / "config" / "items_v2.json").read_text(encoding="utf-8"))
        v2_names = {item["name"] for item in v2.values() if isinstance(item, dict)}
        for old, new in rename.items():
            self.assertNotEqual(old, new)
            self.assertIn(new, v2_names, f"{old} -> {new} 不在新装备目录")
        # 旧装备必须已从 items/weapons 配置中删除
        items = json.loads((ROOT / "config" / "items.json").read_text(encoding="utf-8"))
        weapons = json.loads((ROOT / "config" / "weapons.json").read_text(encoding="utf-8"))
        removed_names = {item.get("name") for item in items.values() if isinstance(item, dict)}
        removed_names |= {item.get("name") for item in (weapons if isinstance(weapons, list) else [])}
        for old in rename:
            self.assertNotIn(old, removed_names, old)

    def test_baseline_doc_in_sync_with_constants(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_economy_baseline.py"), "--check"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 0, (proc.stdout or "") + (proc.stderr or ""))


if __name__ == "__main__":
    unittest.main()
