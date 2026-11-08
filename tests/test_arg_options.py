"""网页聊天室参数候选（/api/arg_options）的回归测试。

覆盖：
- 字段计划与 ARG_SPEC 对齐，避免弹层走到不存在的指令；
- 所有 source 均已注册；
- 七职业完整返回；
- 多段参数按已填值推进，填满后 done=True。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

from importlib import import_module  # noqa: E402

arg_options = import_module(f"{PKG}.webchat_room.arg_options")
commands = import_module(f"{PKG}.webchat_room.commands")


class _FakeDB:
    async def get_player_by_id(self, user_id):  # noqa: D401
        return None


class _FakePlugin:
    """只提供 build_arg_options 必需的最小接口，其余访问抛错以验证兜底。"""

    db = _FakeDB()

    def __getattr__(self, name):
        raise AttributeError(name)


class _FakePillManager:
    DEFAULT_QUALITY = "一般"

    def split_pill_key(self, key):
        raw = str(key or "")
        if "|" in raw:
            name, quality = raw.rsplit("|", 1)
            return name, quality
        return raw, self.DEFAULT_QUALITY

    def format_pill_display_name(self, base_name, quality=None):
        quality = quality or self.DEFAULT_QUALITY
        return str(base_name) if quality == self.DEFAULT_QUALITY else f"{base_name}（{quality}）"


class _FakeWeeklyManager:
    pill_slots_prepare = 6
    pill_slots_combat = 6

    def _pill_manager(self):
        return _FakePillManager()

    def _classify_pill(self, base_name):
        if base_name in ("狂暴丹", "固基丹"):
            return "prepare", {}
        if base_name in ("回春散·天", "回魂丹", "回春散"):
            return "recovery", {}
        if base_name == "回生丹":
            return "revive", {}
        return "unsupported", {}


class _WeeklyPlayer:
    user_id = "web_test"
    user_name = "测试道友"
    level_index = 0
    cultivation_type = "剑修"

    def get_pills_inventory(self):
        return {"狂暴丹|一般": 2, "回春散·天|一般": 4, "经验丹|一般": 9}


class _WeeklyDB:
    async def get_player_by_id(self, user_id):
        return _WeeklyPlayer()


class _WeeklyPlugin:
    db = _WeeklyDB()
    weekly_boss_mgr = _FakeWeeklyManager()


class _QualityPlayer(_WeeklyPlayer):
    def get_pills_inventory(self):
        return {"回春散|一般": 1, "回春散|天": 2}


class _QualityDB:
    async def get_player_by_id(self, user_id):
        return _QualityPlayer()


class _QualityPlugin(_WeeklyPlugin):
    db = _QualityDB()


class TestArgOptions(unittest.IsolatedAsyncioTestCase):
    def test_plans_match_arg_spec(self):
        missing = [cmd for cmd in arg_options.FIELD_PLANS if cmd not in commands.ARG_SPEC]
        self.assertEqual(missing, [], f"字段计划缺少 ARG_SPEC：{missing}")
        for cmd, plan in arg_options.FIELD_PLANS.items():
            self.assertGreaterEqual(
                len(plan),
                int(commands.ARG_SPEC[cmd].get("min", 1)),
                f"{cmd} 的字段段数少于最小参数数",
            )

    def test_sources_registered(self):
        used = {
            field.get("source")
            for plan in arg_options.FIELD_PLANS.values()
            for field in plan
            if field.get("source")
        }
        self.assertEqual(sorted(used - set(arg_options.SOURCES)), [])

    async def test_cultivation_types_full(self):
        data = await arg_options.build_arg_options(_FakePlugin(), "web_test", "/我要修仙", [])
        self.assertTrue(data["ok"])
        values = [opt["value"] for opt in data["options"]]
        self.assertEqual(values, ["灵修", "体修", "魔修", "剑修", "鬼修", "佛修", "御兽"])

    async def test_multi_field_progress(self):
        plugin = _FakePlugin()
        first = await arg_options.build_arg_options(plugin, "web_test", "/赠予", [])
        self.assertEqual(first["index"], 0)
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["field"]["key"], "target")
        self.assertFalse(first["done"])

        second = await arg_options.build_arg_options(plugin, "web_test", "/赠予", ["web_a"])
        self.assertEqual(second["index"], 1)
        self.assertEqual(second["field"]["key"], "item")

        done = await arg_options.build_arg_options(
            plugin, "web_test", "/赠予", ["web_a", "灵石", "1"]
        )
        self.assertTrue(done["done"])
        self.assertIsNone(done["field"])

    def test_proxy_plan_extends_by_activity(self):
        self.assertEqual(len(arg_options.plan_for("/自动代理", [])), 1)
        rift = arg_options.plan_for("/自动代理", ["秘境"])
        self.assertEqual(rift[-1]["source"], "proxy_rifts")
        self.assertFalse(rift[-1].get("optional"))
        for activity in ("灵田", "施肥"):
            plan = arg_options.plan_for("/自动代理", [activity])
            self.assertEqual(len(plan), 2, activity)
            self.assertTrue(plan[-1].get("optional"), activity)
        self.assertEqual(len(arg_options.plan_for("/自动代理", ["押镖"])), 1)
        off_farm = arg_options.plan_for("/关闭代理", ["灵田"])
        self.assertEqual(len(off_farm), 2)
        self.assertTrue(off_farm[-1].get("optional"))

    async def test_proxy_dynamic_fields(self):
        data = await arg_options.build_arg_options(
            _FakePlugin(), "web_test", "/自动代理", ["秘境"]
        )
        self.assertTrue(data["ok"])
        self.assertEqual(data["index"], 1)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["field"]["key"], "rift")
        farm = await arg_options.build_arg_options(
            _FakePlugin(), "web_test", "/自动代理", ["灵田"]
        )
        self.assertEqual(farm["field"]["key"], "plots")
        self.assertTrue(farm["field"]["optional"])

    async def test_unknown_command_falls_back_to_manual(self):
        data = await arg_options.build_arg_options(_FakePlugin(), "web_test", "/签到", [])
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no_options")

    async def test_pavilion_sections_are_selectable(self):
        pill = await arg_options.build_arg_options(_FakePlugin(), "web_test", "/丹阁", [])
        self.assertTrue(pill["ok"])
        self.assertEqual(
            [opt["value"] for opt in pill["options"]],
            ["丹药", "丹方", "突破丹方", "材料"],
        )
        talisman = await arg_options.build_arg_options(_FakePlugin(), "web_test", "/符阁", [])
        self.assertEqual(
            [opt["value"] for opt in talisman["options"]],
            ["符箓", "符图"],
        )

    async def test_use_talisman_options_include_legacy_talismans(self):
        import types

        plugin = types.SimpleNamespace(
            config_manager=types.SimpleNamespace(
                talisman_recipes_data={"寻宝符": {"rank": "黄品", "category": "探索"}},
                items_data={
                    "寻宝符": {"type": "符箓", "rank": "黄品"},
                    "八卦符": {"type": "符箓", "rank": "黄品"},
                    "化身傀儡": {"type": "符箓", "rank": "圣品"},
                    "回春散": {"type": "丹药"},
                },
            )
        )
        player = types.SimpleNamespace(
            get_storage_ring_items=lambda: {
                "寻宝符": 1,
                "八卦符": 2,
                "化身傀儡": 1,
                "回春散": 1,
            }
        )
        options, _ = await arg_options._src_talisman_inventory(plugin, player, {}, [])
        values = [opt["value"] for opt in options]
        self.assertIn("寻宝符", values)
        self.assertIn("八卦符", values, "旧版符箓不在配方表里也应出现在 /使用符箓 候选中")
        self.assertNotIn("化身傀儡", values)
        self.assertNotIn("回春散", values)

    async def test_market_quote_requires_code_with_options(self):
        import types

        fake_parent = types.ModuleType(f"{PKG}.managers")
        fake_invest = types.ModuleType(f"{PKG}.managers.invest_manager")
        fake_invest.STOCK_DEFS = [("S01", "丹阁股", "炼丹", 42.0)]
        fake_invest.FUND_DEFS = [("F01", "灵脉·稳", 1, 1.0)]
        with mock.patch.dict(
            sys.modules,
            {
                f"{PKG}.managers": fake_parent,
                f"{PKG}.managers.invest_manager": fake_invest,
            },
        ):
            data = await arg_options.build_arg_options(_FakePlugin(), "web_test", "/行情", [])
        self.assertTrue(data["ok"])
        self.assertEqual(data["field"]["key"], "code")
        values = [opt["value"] for opt in data["options"]]
        self.assertIn("S01", values)
        self.assertIn("F01", values)

    async def test_optional_field_plans_allow_empty(self):
        for cmd in ("升级洞天", "升级仓库", "一键转移", "一键取出", "洗练"):
            plan = arg_options.plan_for(cmd, [])
            self.assertEqual(len(plan), 1, cmd)
            self.assertTrue(plan[0].get("optional"), cmd)
            data = await arg_options.build_arg_options(_FakePlugin(), "web_test", f"/{cmd}", [])
            self.assertTrue(data["ok"], cmd)
            if cmd != "洗练":  # 洗练候选依赖角色已装备的物品
                self.assertGreater(len(data["options"]), 0, cmd)

    # ---- 周本备战：从丹药列表选择，逐枚累加 ----

    def test_weekly_loadout_plan(self):
        base = arg_options.plan_for("/周本备战", [])
        self.assertEqual(len(base), 1)
        self.assertEqual(base[0]["key"], "mode")
        for mode in ("推荐", "清空"):
            self.assertEqual(len(arg_options.plan_for("/周本备战", [mode])), 1, mode)
        picked = arg_options.plan_for("/周本备战", ["狂暴丹"])
        self.assertEqual(len(picked), arg_options.WEEKLY_LOADOUT_SEGMENTS)
        self.assertEqual(picked[1]["key"], "pill")
        self.assertTrue(picked[1]["optional"])

    async def test_weekly_loadout_mode_is_short_circuit(self):
        plugin = _WeeklyPlugin()
        done = await arg_options.build_arg_options(plugin, "web_test", "/周本备战", ["推荐"])
        self.assertTrue(done["done"])
        self.assertIsNone(done["field"])
        self.assertEqual(done["values"], ["推荐"])

    async def test_weekly_loadout_options_from_inventory(self):
        data = await arg_options.build_arg_options(_WeeklyPlugin(), "web_test", "/周本备战", [])
        self.assertTrue(data["ok"])
        values = [opt["value"] for opt in data["options"]]
        self.assertIn("推荐", values)
        self.assertIn("清空", values)
        self.assertIn("狂暴丹", values)
        self.assertIn("回春散·天", values)
        # 非周本丹药不出现；持有 4 枚会给出 ×2/×3/×4 的数量候选
        self.assertNotIn("经验丹", values)
        self.assertIn("回春散·天*4", values)
        self.assertEqual(data["index"], 0)

    async def test_weekly_loadout_deducts_picked(self):
        data = await arg_options.build_arg_options(
            _WeeklyPlugin(), "web_test", "/周本备战", ["狂暴丹*2"]
        )
        self.assertFalse(data["done"])
        self.assertEqual(data["index"], 1)
        self.assertEqual(data["field"]["key"], "pill")
        values = [opt["value"] for opt in data["options"]]
        self.assertNotIn("狂暴丹", values)
        self.assertNotIn("推荐", values)

    async def test_weekly_loadout_marks_quality_when_ambiguous(self):
        data = await arg_options.build_arg_options(
            _QualityPlugin(), "web_test", "/周本备战", []
        )
        values = [opt["value"] for opt in data["options"]]
        # 同名丹药存在两种品质时必须带品质，避免管理器自动选最高品质
        self.assertIn("回春散|一般", values)
        self.assertIn("回春散|天", values)
        self.assertNotIn("回春散", values)

    async def test_weekly_loadout_parses_pasted_list(self):
        data = await arg_options.build_arg_options(
            _WeeklyPlugin(), "web_test", "/周本备战", ["狂暴丹*2 回春散·天*1"]
        )
        self.assertFalse(data["done"])
        values = [opt["value"] for opt in data["options"]]
        self.assertNotIn("狂暴丹", values)
        self.assertNotIn("回春散·天*4", values)
        self.assertIn("回春散·天*3", values)


class _CraftPlayer:
    user_id = "web_craft"
    user_name = "炼丹道友"
    gold = 1000

    def get_storage_ring_items(self):
        return {"灵草": 25}


class _CraftDB:
    async def get_player_by_id(self, user_id):
        return _CraftPlayer()


class _CraftManager:
    MAX_BATCH_CRAFTS = 100
    recipes = {
        1: {
            "id": 1,
            "name": "炼气丹方",
            "output_pill": "炼气丹",
            "materials": {"灵草": 2},
            "gold_cost": 100,
        }
    }

    def max_craftable_count(self, player, recipe, limit=None):
        return 10

    def get_recipe_by_identifier(self, identifier):
        return None


class _CraftPlugin:
    db = _CraftDB()
    alchemy_mgr = _CraftManager()


class TestCraftArgOptions(unittest.IsolatedAsyncioTestCase):
    async def test_craft_count_has_quick_options(self):
        data = await arg_options.build_arg_options(
            _CraftPlugin(), "web_craft", "/炼丹", ["1", "猛火"]
        )
        self.assertTrue(data["ok"])
        self.assertEqual(data["field"]["key"], "count")
        self.assertTrue(data["field"]["optional"])
        values = [opt["value"] for opt in data["options"]]
        self.assertEqual(values, ["1", "5", "10", "all"])
        self.assertIn("all", values)

    async def test_craft_amount_opens_optional_material_picker(self):
        data = await arg_options.build_arg_options(
            _CraftPlugin(), "web_craft", "/炼丹", ["1", "猛火", "all"]
        )
        self.assertFalse(data["done"])
        self.assertEqual(data["field"]["key"], "materials")
        self.assertTrue(data["field"]["optional"])

    async def test_craft_count_falls_back_when_materials_missing(self):
        class _EmptyManager(_CraftManager):
            def max_craftable_count(self, player, recipe, limit=None):
                return 0

        class _EmptyPlugin(_CraftPlugin):
            alchemy_mgr = _EmptyManager()

        data = await arg_options.build_arg_options(
            _EmptyPlugin(), "web_craft", "/炼丹", ["1", "猛火"]
        )
        self.assertEqual(data["options"], [])
        self.assertIn("不足", data["message"])


if __name__ == "__main__":
    unittest.main()
