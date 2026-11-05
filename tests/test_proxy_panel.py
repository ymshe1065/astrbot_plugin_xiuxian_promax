"""网页聊天室「自动代理」面板的回归测试。

覆盖：
- 五项代理的布尔状态与详情；
- 秘境只返回当前境界可进入的本；
- 单项开关调用对应管理器方法；
- 总开关关闭走 disable，开启优先秘境、无秘境时回退押镖/宗门任务。
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

from importlib import import_module  # noqa: E402

proxy_panel = import_module(f"{PKG}.webchat_room.proxy_panel")


class _FarmManager:
    def __init__(self, farm):
        self.farm = farm

    async def get_user_farm(self, user_id):
        return self.farm

    def get_max_slots(self, farm):
        return 6


class _FakeManager:
    def __init__(self, row=None, farm=None):
        self.row = row or {}
        self.farm_mgr = _FarmManager(farm) if farm is not None else None
        self.calls = []

    async def get(self, user_id):
        return self.row

    @staticmethod
    def _load_config(row):
        raw = (row or {}).get("config_json") or "{}"
        try:
            config = json.loads(raw)
        except (TypeError, ValueError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        for key, defaults in (
            ("farm", {"enabled": False, "plots": None, "exclude": [], "tasks": {}}),
            ("rift", {"enabled": False, "rift_id": 0}),
            (
                "fertilize",
                {"enabled": False, "threshold": 0.85, "min_roi": 1.0, "note": ""},
            ),
        ):
            section = config.get(key)
            if not isinstance(section, dict):
                section = {}
            for field, value in defaults.items():
                section.setdefault(field, value)
            config[key] = section
        return config

    @staticmethod
    def _farm_plots_text(plots):
        if plots is None:
            return "全部格"
        return "、".join(f"格{slot}" for slot in plots) or "无"

    @staticmethod
    def task_name(task):
        return proxy_panel.TASK_NAMES.get(task, task)

    async def enable(self, user_id, origin, task):
        self.calls.append(("enable", task))
        return True, f"🤖 {task}已开启"

    async def disable(self, user_id, reason=""):
        self.calls.append(("disable",))
        return True

    async def disable_primary(self, user_id, reason=""):
        self.calls.append(("disable_primary",))
        return True, "🛑 押镖代理已关闭。"

    async def disable_farm(self, user_id, plots_text=""):
        self.calls.append(("disable_farm",))
        return True, "🛑 灵田代理已关闭。"

    async def disable_fert(self, user_id):
        self.calls.append(("disable_fert",))
        return True, "🛑 自动施肥已关闭。"

    async def disable_rift(self, user_id):
        self.calls.append(("disable_rift",))
        return True, "🛑 秘境代理已关闭。"


class _Ext:
    def __init__(self, rifts):
        self.rifts = rifts

    async def get_all_rifts(self):
        return self.rifts

    async def get_user_cd(self, user_id):
        return None


class _DB:
    def __init__(self, player, rifts):
        self.player = player
        self.ext = _Ext(rifts)

    async def get_player_by_id(self, user_id):
        return self.player


class _Plugin:
    def __init__(self, mgr, player, rifts):
        self.auto_proxy_mgr = mgr
        self.db = _DB(player, rifts)


def _rift(rift_id, name, need):
    return SimpleNamespace(rift_id=rift_id, rift_name=name, required_level=need)


class TestProxyPanel(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_states_and_enterable_rifts(self):
        row = {
            "enabled": 1,
            "task_type": "escort",
            "config_json": (
                '{"farm":{"enabled":true,"plots":[1,2]},'
                '"rift":{"enabled":true,"rift_id":2},'
                '"fertilize":{"enabled":true,"threshold":0.7}}'
            ),
        }
        mgr = _FakeManager(row, farm={"fertility": 50})
        rifts = [_rift(1, "青云秘境", 0), _rift(2, "幽冥秘境", 99)]
        plugin = _Plugin(mgr, SimpleNamespace(level_index=10), rifts)

        data = await proxy_panel.proxy_snapshot(plugin, "web_test")

        self.assertTrue(data["ok"])
        self.assertTrue(data["has_player"])
        self.assertTrue(data["enabled"])
        tasks = {task["key"]: task for task in data["tasks"]}
        self.assertTrue(tasks["escort"]["enabled"])
        self.assertTrue(tasks["farm"]["enabled"])
        self.assertTrue(tasks["rift"]["enabled"])
        self.assertTrue(tasks["fertilize"]["enabled"])
        self.assertIn("格1", tasks["farm"]["detail"])
        self.assertIn("阈值 70%", tasks["fertilize"]["detail"])
        # 仅当前境界可进入的本进入下拉框；旧存档的 ID:2 自动回退到 ID:1
        self.assertEqual([rift["id"] for rift in data["rifts"]], [1])
        self.assertEqual(data["rift_id"], 1)

    async def test_toggle_farm_on_and_off(self):
        mgr = _FakeManager({}, farm={"fertility": 10})
        plugin = _Plugin(mgr, SimpleNamespace(level_index=1), [])

        data = await proxy_panel.toggle_proxy(plugin, "web_test", "farm", True)
        self.assertTrue(data["done"])
        self.assertIn(("enable", "灵田"), mgr.calls)
        self.assertIn("灵田代理已开启", data["message"])

        data = await proxy_panel.toggle_proxy(plugin, "web_test", "farm", False)
        self.assertTrue(data["done"])
        self.assertIn(("disable_farm",), mgr.calls)
        self.assertIn("灵田代理已关闭", data["message"])

    async def test_primary_toggle_switches_target(self):
        row = {"enabled": 1, "task_type": "escort", "config_json": "{}"}
        mgr = _FakeManager(row)
        plugin = _Plugin(mgr, SimpleNamespace(level_index=1), [])

        data = await proxy_panel.toggle_proxy(plugin, "web_test", "sect_task", True)
        self.assertTrue(data["done"])
        self.assertIn(("enable", "宗门任务"), mgr.calls)
        self.assertIn("宗门任务代理已开启", data["message"])

        data = await proxy_panel.toggle_proxy(plugin, "web_test", "escort", False)
        self.assertTrue(data["done"])
        self.assertIn(("disable_primary",), mgr.calls)

    async def test_master_off_calls_disable(self):
        mgr = _FakeManager({"enabled": 1})
        plugin = _Plugin(mgr, SimpleNamespace(level_index=1), [])

        data = await proxy_panel.toggle_proxy(plugin, "web_test", "all", False)
        self.assertTrue(data["done"])
        self.assertIn(("disable",), mgr.calls)
        self.assertIn("全部关闭", data["message"])

    async def test_master_on_prefers_enterable_rift(self):
        mgr = _FakeManager({})
        plugin = _Plugin(mgr, SimpleNamespace(level_index=10), [_rift(3, "星陨秘境", 5)])

        data = await proxy_panel.toggle_proxy(plugin, "web_test", "all", True)
        self.assertTrue(data["done"])
        self.assertIn(("enable", "秘境 3"), mgr.calls)

    async def test_master_on_falls_back_to_primary_without_rift(self):
        row = {"enabled": 0, "task_type": "sect_task", "config_json": "{}"}
        mgr = _FakeManager(row)
        plugin = _Plugin(mgr, SimpleNamespace(level_index=1), [])

        data = await proxy_panel.toggle_proxy(plugin, "web_test", "all", True)
        self.assertTrue(data["done"])
        self.assertIn(("enable", "宗门任务"), mgr.calls)


if __name__ == "__main__":
    unittest.main()
