# 装备/符箓/丹药平衡与内容断链回归测试
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
    async def update_player(self, player, commit=True):
        return True


class TestContentLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config_dir = ROOT / "config"
        cls.names = set()
        for filename in ("items.json", "weapons.json", "pills.json", "exp_pills.json",
                         "utility_pills.json", "storage_rings.json", "techniques.json"):
            data = json.loads((config_dir / filename).read_text(encoding="utf-8"))
            values = data.values() if isinstance(data, dict) else data
            for item in values:
                if isinstance(item, dict) and item.get("name"):
                    cls.names.add(item["name"])
        materials = json.loads((config_dir / "material_catalog.json").read_text(encoding="utf-8"))
        cls.names |= {item["name"] for item in materials.get("materials", []) if isinstance(item, dict)}

    def test_adventure_drops_exist(self):
        data = json.loads((ROOT / "config" / "adventure_config.json").read_text(encoding="utf-8"))
        for table in data.get("drop_tables", {}).values():
            for item in table:
                self.assertIn(item.get("name"), self.names)

    def test_rift_and_pill_drops_exist(self):
        data = json.loads((ROOT / "config" / "game_config.json").read_text(encoding="utf-8"))
        rift = data.get("rift", {})
        for key in ("drop_tables", "pill_drop_tables"):
            for table in rift.get(key, {}).values():
                for item in table:
                    self.assertIn(item.get("name"), self.names)

    def test_utility_pills_have_unique_names(self):
        data = json.loads((ROOT / "config" / "utility_pills.json").read_text(encoding="utf-8"))
        names = [item.get("name") for item in data if isinstance(item, dict)]
        self.assertEqual(len(names), len(set(names)))


class TestBalanceFixes(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.Player = _imp("models").Player
        cls.PillManager = _imp("core.pill_manager").PillManager
        cls.ItemInstanceManager = _imp("core.item_instance_manager").ItemInstanceManager
        cls.ShopManager = _imp("core.shop_manager").ShopManager

    def _pill_manager(self):
        return self.PillManager(_FakeDB(), self.cm)

    async def test_wash_marrow_increases_permanent_limit(self):
        pm = self._pill_manager()
        player = self.Player(user_id="u1", level_index=25)
        player.pills_inventory = json.dumps({"洗髓丹|一般": 1}, ensure_ascii=False)
        ok, _ = await pm.use_pill(player, "洗髓丹", "一般")
        self.assertTrue(ok)
        self.assertAlmostEqual(pm.get_base_attribute_limit_bonus(player), 0.05)

    async def test_chaos_pill_gets_random_combat_effects(self):
        pm = self._pill_manager()
        player = self.Player(user_id="u1", level_index=13)
        player.pills_inventory = json.dumps({"狂乱丹|一般": 1}, ensure_ascii=False)
        ok, _ = await pm.use_pill(player, "狂乱丹", "一般")
        self.assertTrue(ok)
        effect = player.get_active_pill_effects()[0]
        self.assertTrue(effect.get("random_effect_key"))
        self.assertIn(effect.get("random_effect_key"), {
            "physical_damage_multiplier", "magic_damage_multiplier",
            "physical_defense_multiplier", "magic_defense_multiplier",
        })

    async def test_debuff_shield_blocks_next_debuff(self):
        pm = self._pill_manager()
        player = self.Player(user_id="u1", level_index=16)
        player.pills_inventory = json.dumps({"定魂丹|一般": 1, "虚弱丹|一般": 1}, ensure_ascii=False)
        ok, _ = await pm.use_pill(player, "定魂丹", "一般")
        self.assertTrue(ok)
        self.assertTrue(player.has_debuff_shield)
        ok, _ = await pm.use_pill(player, "虚弱丹", "一般")
        self.assertTrue(ok)
        self.assertFalse(player.has_debuff_shield)
        self.assertEqual(player.get_active_pill_effects(), [])

    def test_talisman_shop_keeps_type_and_effect(self):
        shop = self.ShopManager({}, self.cm)
        item = shop.find_item_by_name("引雷符")
        self.assertEqual(item["type"], "符箓")
        details = shop.get_item_details(item)
        self.assertIn("类别：炼丹", details)
        self.assertIn("炼丹成功率", details)


    def test_item_info_covers_all_configured_items(self):
        shop = self.ShopManager({}, self.cm)
        names = set()
        for source in (
            self.cm.items_data, self.cm.weapons_data, self.cm.techniques_data,
            self.cm.pills_data, self.cm.exp_pills_data, self.cm.utility_pills_data,
            self.cm.storage_rings_data,
        ):
            for cfg in source.values():
                if isinstance(cfg, dict) and cfg.get("name"):
                    names.add(cfg["name"])
        names |= {name for name in (getattr(self.cm, "talisman_recipes_data", {}) or {})}
        names |= {m.get("name") for m in (getattr(self.cm, "material_catalog", {}) or {}).values() if isinstance(m, dict)}
        missing = [name for name in sorted(names) if shop.find_item_by_name(name) is None]
        self.assertEqual(missing, [])

    def test_quality_roll_is_valid(self):
        for _ in range(50):
            self.assertIn(self.ItemInstanceManager.roll_quality(), self.ItemInstanceManager.QUALITY_MULTIPLIER)


class TestStorageRingProgression(unittest.TestCase):
    """储物戒容量与境界门槛应平滑递增，避免后期出现断层。"""

    @classmethod
    def setUpClass(cls):
        data = json.loads((ROOT / "config" / "storage_rings.json").read_text(encoding="utf-8"))
        cls.rings = sorted(data.values(), key=lambda r: int(r["capacity"]))
        cls.levels = json.loads((ROOT / "config" / "level_config.json").read_text(encoding="utf-8"))

    def test_capacity_and_required_level_are_monotonic(self):
        for prev, cur in zip(self.rings, self.rings[1:]):
            self.assertGreater(int(cur["capacity"]), int(prev["capacity"]))
            self.assertGreaterEqual(
                int(cur["required_level_index"]), int(prev["required_level_index"])
            )
        for ring in self.rings:
            self.assertLess(int(ring["required_level_index"]), len(self.levels))

    def test_late_game_curve_has_no_big_gap(self):
        late_rings = [r for r in self.rings if int(r["capacity"]) >= 200]
        for prev, cur in zip(late_rings, late_rings[1:]):
            ratio = int(cur["capacity"]) / int(prev["capacity"])
            level_gap = int(cur["required_level_index"]) - int(prev["required_level_index"])
            self.assertLessEqual(ratio, 1.5, f"{prev['name']} → {cur['name']} 容量跳涨过大")
            self.assertLessEqual(level_gap, 3, f"{prev['name']} → {cur['name']} 境界间隔过大")


if __name__ == "__main__":
    unittest.main()
