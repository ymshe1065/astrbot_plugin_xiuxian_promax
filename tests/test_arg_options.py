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


if __name__ == "__main__":
    unittest.main()
