# 符箓系统（效果接线 / 丹劫对档 / 符阁 / 熟练度）回归测试
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

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
    def __init__(self):
        self.saved = 0

    async def update_player(self, player, commit=True):
        self.saved += 1
        return True


class _FakeRing:
    """储物戒替身：只关心已用槽位，避免测试依赖完整 StorageRingManager。"""

    def __init__(self):
        self.stored = {}

    def get_available_slots(self, player):
        return 99

    async def store_item(self, player, name, count=1, silent=False, **kwargs):
        items = player.get_storage_ring_items()
        items[name] = items.get(name, 0) + int(count)
        player.set_storage_ring_items(items)
        self.stored[name] = self.stored.get(name, 0) + int(count)
        return True, f"获得 {name}x{count}"


class TestTalismanConfig(unittest.TestCase):
    """配置表本身的一致性。"""

    @classmethod
    def setUpClass(cls):
        cls.recipes = json.loads((ROOT / "config" / "talisman_recipes.json").read_text(encoding="utf-8"))
        cls.items = json.loads((ROOT / "config" / "items.json").read_text(encoding="utf-8"))
        cls.by_name = {
            cfg["name"]: cfg for cfg in cls.items.values()
            if isinstance(cfg, dict) and cfg.get("type") == "符箓"
        }

    def test_recipe_table_shape(self):
        self.assertEqual(len(self.recipes), 34)
        self.assertEqual(len({r["name"] for r in self.recipes}), 34)
        self.assertEqual(sum(1 for r in self.recipes if r.get("basic")), 10)
        ranks = {r["rank"] for r in self.recipes}
        self.assertEqual(ranks, {"凡品", "黄品", "玄品", "圣品"})

    def test_all_recipe_products_are_talisman_items(self):
        for recipe in self.recipes:
            cfg = self.by_name.get(recipe["name"])
            self.assertIsNotNone(cfg, f"{recipe['name']} 不在 items.json")
            self.assertEqual(cfg.get("type"), "符箓")
            self.assertEqual(cfg.get("rank"), recipe["rank"])
            self.assertFalse(cfg.get("legacy"), f"{recipe['name']} 不应标 legacy")

    def test_price_matches_cost_formula(self):
        base = {"凡品": 100, "黄品": 1080, "玄品": 9900, "圣品": 70500}
        for recipe in self.recipes:
            cost = sum(base[m["tier"]] * m["count"] for m in recipe["materials"])
            expected = int(round(cost * 1.4 + recipe["gold_cost"]))
            self.assertEqual(recipe["price"], expected, recipe["name"])
            self.assertEqual(self.by_name[recipe["name"]]["price"], expected, recipe["name"])

    def test_legacy_talismans_are_marked(self):
        legacy = [name for name, cfg in self.by_name.items() if cfg.get("legacy")]
        recipe_names = {r["name"] for r in self.recipes}
        self.assertTrue(legacy)
        self.assertFalse(set(legacy) & recipe_names)

    def test_category_covers_eight_kinds(self):
        categories = {r["category"] for r in self.recipes}
        self.assertEqual(
            categories,
            {"攻击", "防御", "控制", "回复", "炼丹", "探索", "增益", "遁术"},
        )


class TestTalismanRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = _imp("core.talisman_rules")

    def test_shield_and_demand_tables(self):
        self.assertEqual(self.rules.SHIELD_BY_RANK, {"凡品": 2, "黄品": 4, "玄品": 6, "圣品": 9})
        self.assertEqual(self.rules.TRIBULATION_DEMAND["圣品"], 6)
        self.assertEqual(self.rules.TRIBULATION_DEMAND["仙品"], 9)
        self.assertEqual(self.rules.TRIBULATION_DEMAND["凡品"], 0)
        self.assertEqual(self.rules.TRIBULATION_RATE["凡品"], 0)

    def test_build_profile_uses_explicit_effects(self):
        profile = self.rules.build_profile(
            "山河符", "黄品", "防御",
            effects={"physical_defense_multiplier": 0.33},
        )
        self.assertEqual(profile["effects"]["physical_defense_multiplier"], 0.33)
        self.assertEqual(profile["duration"], 15)

    def test_array_suffix_no_longer_grants_bonus(self):
        base = self.rules.build_profile("寒霜符", "凡品", "控制")
        array = self.rules.build_profile("寒霜阵", "凡品", "控制")
        self.assertEqual(base["duration"], array["duration"])

    def test_proficiency_success_table(self):
        cases = [
            (80, "凡品", 0, 80),
            (74, "黄品", 0, 50),
            (65, "玄品", 0, 17),
            (60, "圣品", 0, 5),
            (74, "黄品", 30, 79),
            (65, "玄品", 110, 75),
            (60, "圣品", 410, 80.5),
            (80, "凡品", 410, 99),
        ]
        for rate, rank, points, expected in cases:
            got = self.rules.proficiency_success(rate, rank, points)
            self.assertAlmostEqual(got, expected, places=1, msg=f"{rank}@{points}")

    def test_proficiency_gain_failure_half(self):
        self.assertEqual(self.rules.proficiency_gain("圣品", 1, True), 8)
        self.assertEqual(self.rules.proficiency_gain("圣品", 1, False), 4)
        self.assertEqual(self.rules.proficiency_gain("凡品", 9, False), 4)

    def test_proficiency_level_caps(self):
        self.assertEqual(self.rules.proficiency_level(999999), len(self.rules.PROFICIENCY_TIERS) - 1)

    def test_rank_fallback_by_level(self):
        self.assertEqual(self.rules.rank_from_level(8), "黄品")
        self.assertEqual(self.rules.rank_from_level(18), "圣品")
        self.assertEqual(self.rules.rank_from_level(31), "仙品")


class TestTalismanManager(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.Player = _imp("models").Player
        cls.Manager = _imp("managers.talisman_manager").TalismanManager

    def _mgr(self):
        return self.Manager(_FakeDB(), self.cm, _FakeRing())

    def _player(self, **kwargs):
        player = self.Player(user_id="u1", level_index=20, **kwargs)
        player.life_skill = "符箓师"
        return player

    # ---- 改动 A：效果接线 ----
    def test_defense_talisman_exposes_multiplier(self):
        mgr = self._mgr()
        player = self._player(life_skill="")
        items = player.get_storage_ring_items()
        items["山河符"] = 1
        player.set_storage_ring_items(items)
        import asyncio
        asyncio.get_event_loop() if False else None
        profile = mgr.get_talisman_profile("山河符")
        self.assertEqual(profile["category"], "防御")
        self.assertAlmostEqual(profile["effects"]["physical_defense_multiplier"], 0.33)

    async def test_use_talisman_feeds_combat_effects(self):
        mgr = self._mgr()
        player = self._player()
        items = player.get_storage_ring_items()
        items["山河符"] = 1
        player.set_storage_ring_items(items)
        ok, _ = await mgr.use_talisman(player, "山河符", 1)
        self.assertTrue(ok)
        effects = self.Manager.collect_active_combat_effects(player)
        self.assertIn("physical_defense_multiplier", effects)
        self.assertGreaterEqual(effects["physical_defense_multiplier"], 0.33)

    async def test_two_defense_talismans_take_max_not_sum(self):
        mgr = self._mgr()
        player = self._player()
        items = player.get_storage_ring_items()
        items["山河符"] = 1
        items["金刚符"] = 1
        player.set_storage_ring_items(items)
        with mock.patch.object(mgr, "_roll_quality", return_value=("一般", 1.0, 0)):
            await mgr.use_talisman(player, "山河符", 1)
            await mgr.use_talisman(player, "金刚符", 1)
        effects = self.Manager.collect_active_combat_effects(player)
        self.assertAlmostEqual(effects["physical_defense_multiplier"], 0.33, places=2)

    async def test_attack_talisman_returns_damage_multiplier(self):
        mgr = self._mgr()
        player = self._player()
        items = player.get_storage_ring_items()
        items["烈焰符"] = 1
        player.set_storage_ring_items(items)
        await mgr.use_talisman(player, "烈焰符", 1)
        effects = self.Manager.collect_active_combat_effects(player)
        self.assertIn("physical_damage_multiplier", effects)
        self.assertIn("magic_damage_multiplier", effects)
        self.assertIn("crit_rate", effects)

    async def test_control_talisman_uses_real_control_keys(self):
        mgr = self._mgr()
        player = self._player()
        items = player.get_storage_ring_items()
        items["寒霜符"] = 1
        player.set_storage_ring_items(items)
        await mgr.use_talisman(player, "寒霜符", 1)
        effects = self.Manager.collect_active_combat_effects(player)
        self.assertIn("frost_chance", effects)
        self.assertNotIn("dodge", effects)

    async def test_merge_respects_global_cap(self):
        mgr = self._mgr()
        player = self._player()
        active = []
        for name in ("寒霜符", "九幽符", "幽冥符", "缚天符"):
            active.append({
                "source": "talisman", "talisman_name": name,
                "expiry_time": 99999999999, "stun_chance": 18, "dodge_bonus": 20,
            })
        player.set_active_pill_effects(active)
        Combat = _imp("managers.combat_manager").CombatManager
        effects = Combat.clamp_effects(self.Manager.merge_into_combat_effects({}, player))
        self.assertLessEqual(effects["stun_chance"], 30)
        self.assertLessEqual(effects["dodge"], 20)

    # ---- 改动 D：符图习得 ----
    async def test_craft_blocked_without_recipe(self):
        mgr = self._mgr()
        player = self._player()
        ok, msg = await mgr.craft_talisman(player, "星辰符", 1)
        self.assertFalse(ok)
        self.assertIn("符图", msg)

    async def test_basic_recipe_needs_no_learning(self):
        mgr = self._mgr()
        player = self._player()
        self.assertTrue(mgr.is_recipe_learned(player, "烈焰符"))

    async def test_learn_recipe_unlocks_craft(self):
        mgr = self._mgr()
        player = self._player()
        ok, _ = await mgr.learn_recipe(player, "星辰符", commit=False)
        self.assertTrue(ok)
        self.assertTrue(mgr.is_recipe_learned(player, "星辰符"))

    # ---- 改动 E：熟练度 ----
    async def test_failure_still_grants_points(self):
        mgr = self._mgr()
        player = self._player()
        player.gold = 100000
        items = player.get_storage_ring_items()
        items["灵植"] = 10
        player.set_storage_ring_items(items)
        fake_materials = [{"category": "灵植", "tier": "凡品", "count": 1}]
        with mock.patch.object(mgr, "_material_stacks", return_value=[("灵植", 10)]):
            with mock.patch.object(mgr, "_consume_materials", return_value=(True, "", ["灵植×1"])):
                with mock.patch("random.randint", return_value=100):
                    ok, _ = await mgr.craft_talisman(player, "烈焰符", 1)
        self.assertTrue(ok)
        self.assertEqual(player.talisman_exp, 0)  # 凡品 1 张 × 0.5 向下取整 = 0

    async def test_success_grants_rank_points(self):
        mgr = self._mgr()
        player = self._player()
        player.gold = 100000
        with mock.patch.object(mgr, "_consume_materials", return_value=(True, "", ["矿材×2"])):
            with mock.patch("random.randint", return_value=1):
                ok, _ = await mgr.craft_talisman(player, "烈焰符", 3)
        self.assertTrue(ok)
        self.assertGreaterEqual(player.talisman_exp, 3)

    # ---- legacy 兼容 ----
    async def test_legacy_talisman_still_usable(self):
        mgr = self._mgr()
        player = self._player()
        items = player.get_storage_ring_items()
        items["八卦符"] = 1
        player.set_storage_ring_items(items)
        ok, msg = await mgr.use_talisman(player, "八卦符", 1)
        self.assertTrue(ok, msg)


class TestTribulation(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.Player = _imp("models").Player
        cls.Manager = _imp("managers.talisman_manager").TalismanManager
        cls.Alchemy = _imp("managers.alchemy_manager").AlchemyManager

    def _player(self, stock):
        player = self.Player(user_id="u1", level_index=30)
        player.set_storage_ring_items(dict(stock))
        return player

    def test_pick_prefers_cheapest_sufficient_shield(self):
        player = self._player({"稳魂符": 1, "避劫符": 1, "混元符": 1})
        # 劫数 1：三张都够，应挑护盾最小的稳魂符
        pick = self.Manager.pick_shield_talisman(player, 1, self.cm)
        self.assertEqual(pick[0], "稳魂符")
        # 劫数 6：稳魂(2)/避劫(6) 中挑避劫
        pick = self.Manager.pick_shield_talisman(player, 6, self.cm)
        self.assertEqual(pick[0], "避劫符")
        # 劫数 9：只有混元符够
        pick = self.Manager.pick_shield_talisman(player, 9, self.cm)
        self.assertEqual(pick[0], "混元符")

    def test_insufficient_shield_returns_none(self):
        player = self._player({"引雷符": 1})
        self.assertIsNone(self.Manager.pick_shield_talisman(player, 6, self.cm))

    def test_consume_shield_removes_one(self):
        player = self._player({"避劫符": 2})
        used = self.Manager.consume_shield_talisman(player, self.cm, 6)
        self.assertEqual(used, "避劫符")
        self.assertEqual(player.get_storage_ring_items().get("避劫符"), 1)

    def test_pill_rank_lookup(self):
        alchemy = self.Alchemy(_FakeDB(), self.cm)
        self.assertEqual(alchemy._pill_rank_of("凝婴丹"), "圣品")
        self.assertEqual(alchemy._pill_rank_of("混元丹"), "仙品")
        # 造化丹只在 items.json 标凡品，这里以配置为准
        self.assertEqual(alchemy._pill_rank_of("造化丹"), "凡品")

    def test_no_tribulation_for_low_rank_pills(self):
        alchemy = self.Alchemy(_FakeDB(), self.cm)
        rules = _imp("core.talisman_rules")
        for name in ("炼气丹", "聚灵丹", "回春散"):
            rank = alchemy._pill_rank_of(name)
            self.assertEqual(rules.TRIBULATION_DEMAND.get(rank, 0), 0, f"{name} 不应渡劫")


class TestPavilion(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.ShopManager = _imp("core.shop_manager").ShopManager

    def test_talisman_shelf_only_low_rank(self):
        shop = self.ShopManager({}, self.cm)
        entries = shop.get_talismans_for_display(12)
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual(entry["type"], "符箓")
            self.assertIn(entry["rank"], ("凡品", "黄品"))
            self.assertFalse((entry["data"] or {}).get("legacy"))

    def test_recipe_shelf_excludes_basic(self):
        shop = self.ShopManager({}, self.cm)
        entries = shop.get_talisman_recipes_for_display(4)
        names = {e["name"] for e in entries}
        self.assertTrue(names)
        for name in names:
            self.assertFalse(self.cm.talisman_recipes_data[name].get("basic"))
        self.assertIn("聚灵符", names)

    def test_treasure_pavilion_has_no_talisman(self):
        shop = self.ShopManager({}, self.cm)
        entries = shop.get_all_items_for_display(40)
        for entry in entries:
            self.assertNotEqual((entry.get("data") or {}).get("type"), "符箓")

    def test_recipe_price_is_discounted(self):
        shop = self.ShopManager({}, self.cm)
        for entry in shop.get_talisman_recipes_for_display(4):
            base = int(self.cm.talisman_recipes_data[entry["name"]]["price"])
            self.assertEqual(entry["price"], int(base * 0.8))


if __name__ == "__main__":
    unittest.main()
