# tests/test_activity_catalog.py
# 活动目录（core/activity_catalog.py）及其派生关系的守护测试。
#
# 这张目录存在的意义是：新加一项占槽活动时，退出指令、开始指令、代理任务名
# 都能自动跟到对应名单上，不用挨个手改。所以测试的重点不是「目录内容对不对」，
# 而是「派生链有没有断」——断了不会报错，只会让玩家卡死（或重伤照样开跑）。
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()

# BUSY_BYPASS 里这几条不属于任何活动（周本临时状态），历史上就没进 ALLOWED。
# 它们靠 bypass_busy 直接跳过忙碌判定，所以不在 ALLOWED 里也能用。
BYPASS_NOT_IN_ALLOWED = {"周本离开", "周本取消"}

# 维护动作：不占槽、不进 user_cd，不在目录里，由 auto_proxy 手写。
MAINTENANCE_TASKS = {"farm": "灵田", "fertilize": "施肥"}


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestActivityCatalogShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog_mod = _imp("core.activity_catalog")
        cls.CATALOG = cls.catalog_mod.CATALOG
        cls.ActivitySpec = cls.catalog_mod.ActivitySpec
        cls.UserStatus = _imp("models_extended").UserStatus

    def test_catalog_covers_all_slot_activities(self):
        """占槽活动必须都在目录里；新增活动时这条会提醒补登记。"""
        expected = {
            int(self.UserStatus.CULTIVATING),
            int(self.UserStatus.ADVENTURING),
            int(self.UserStatus.EXPLORING),
            int(self.UserStatus.SECT_TASK),
            int(self.UserStatus.DELIVERING),
            int(self.UserStatus.RESTING),
            int(self.UserStatus.TREASURE),
        }
        self.assertEqual(set(self.CATALOG), expected)

    def test_status_and_key_unique(self):
        self.assertEqual(len(self.CATALOG), len({s.status for s in self.CATALOG.values()}))
        self.assertEqual(len(self.CATALOG), len({s.key for s in self.CATALOG.values()}))

    def test_proxy_mode_valid(self):
        allowed = {
            self.catalog_mod.PROXY_NONE,
            self.catalog_mod.PROXY_CONFIG,
            self.catalog_mod.PROXY_TASK,
        }
        for spec in self.CATALOG.values():
            self.assertIn(spec.proxy_mode, allowed, spec.key)

    def test_no_inert_fields(self):
        """spec 里不能有没人读的字段（首版的 exclusive 就是这么误导人的）。"""
        field_names = set(self.ActivitySpec.__dataclass_fields__)
        self.assertNotIn("exclusive", field_names)
        self.assertNotIn("state_text", field_names)

    def test_commands_non_empty(self):
        for spec in self.CATALOG.values():
            for cmd in (*spec.exit_commands, *spec.action_commands):
                self.assertTrue(cmd, f"{spec.key} 有空的指令名")
                self.assertEqual(cmd, cmd.strip(), f"{spec.key} 的指令名带空白：{cmd!r}")

    def test_exit_commands_not_prefix_of_each_other(self):
        """退出指令互不为前缀：BYPASS 走 startswith，短前缀会误放行一大片。"""
        exits = [c for s in self.CATALOG.values() for c in s.exit_commands]
        for short in exits:
            for long in exits:
                if short == long:
                    continue
                self.assertFalse(
                    long.startswith(short),
                    f"{short!r} 是 {long!r} 的前缀，会被 startswith 误放行",
                )


class TestDerivedLists(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.utils = _imp("handlers.utils")
        cls.catalog_mod = _imp("core.activity_catalog")
        cls.CATALOG = cls.catalog_mod.CATALOG

    def test_types_unchanged(self):
        """派生时类型不能混：ALLOWED 是 list，BYPASS / INJURED 是 tuple。"""
        self.assertIsInstance(self.utils.BUSY_STATE_ALLOWED_COMMANDS, list)
        self.assertIsInstance(self.utils.BUSY_BYPASS_COMMANDS, tuple)
        self.assertIsInstance(self.utils.INJURED_BLOCKED_COMMANDS, tuple)

    def test_every_exit_command_can_escape_busy(self):
        """漏一条退出指令 = 玩家卡死出不来，且不报错。"""
        for spec in self.CATALOG.values():
            for cmd in spec.exit_commands:
                self.assertIn(cmd, self.utils.BUSY_STATE_ALLOWED_COMMANDS, spec.key)
                self.assertIn(cmd, self.utils.BUSY_BYPASS_COMMANDS, spec.key)

    def test_every_action_command_blocked_when_injured(self):
        """漏一条开始指令 = 重伤玩家照样能跑这项活动。"""
        for spec in self.CATALOG.values():
            for cmd in spec.action_commands:
                self.assertIn(cmd, self.utils.INJURED_BLOCKED_COMMANDS, spec.key)

    def test_bypass_extra_items_do_not_grow(self):
        """BYPASS 里不属于活动、也不在 ALLOWED 的项只能是历史上那两条。"""
        bypass = set(self.utils.BUSY_BYPASS_COMMANDS)
        allowed = set(self.utils.BUSY_STATE_ALLOWED_COMMANDS)
        self.assertEqual(bypass - allowed, BYPASS_NOT_IN_ALLOWED)

    def test_allowed_list_has_no_duplicates(self):
        items = self.utils.BUSY_STATE_ALLOWED_COMMANDS
        self.assertEqual(len(items), len(set(items)))

    def test_bypass_list_has_no_duplicates(self):
        items = self.utils.BUSY_BYPASS_COMMANDS
        self.assertEqual(len(items), len(set(items)))

    def test_handwritten_readonly_items_survived(self):
        """派生是追加不是替换，只读/社交/代理类手写项不能被冲掉。"""
        allowed = set(self.utils.BUSY_STATE_ALLOWED_COMMANDS)
        for cmd in (
            "我的信息", "储物戒", "丹药背包", "仙缘商店",
            "接受双修", "拒绝切磋",
            "自动代理", "关闭代理",
            "调息", "收功", "解卡",
            "完成悬赏", "放弃悬赏",
        ):
            self.assertIn(cmd, allowed)


class TestProxyTables(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.apm = _imp("managers.auto_proxy_manager")
        cls.catalog_mod = _imp("core.activity_catalog")

    def test_task_status_is_exactly_escort_sect(self):
        """历练/秘境是 config 驱动，进了这张表会被当成押镖逐轮重复结算。"""
        UserStatus = _imp("models_extended").UserStatus
        self.assertEqual(
            self.apm.TASK_STATUS,
            {
                "escort": int(UserStatus.DELIVERING),
                "sect_task": int(UserStatus.SECT_TASK),
            },
        )

    def test_task_status_matches_catalog(self):
        self.assertEqual(self.apm.TASK_STATUS, self.catalog_mod.task_status())
        self.assertEqual(set(self.apm.TASK_STATUS), {"escort", "sect_task"})

    def test_task_names_keep_maintenance_actions(self):
        """灵田/施肥不在目录里，纯派生会让它们从表里消失并露出英文 key。"""
        for key, name in MAINTENANCE_TASKS.items():
            self.assertEqual(self.apm.TASK_NAMES.get(key), name)
        self.assertNotIn("farm", self.catalog_mod.task_names())
        self.assertNotIn("fertilize", self.catalog_mod.task_names())

    def test_task_names_cover_all_six_tasks(self):
        self.assertEqual(set(self.apm.TASK_NAMES),
                         {"escort", "sect_task", "farm", "rift", "fertilize", "adventure"})

    def test_status_task_reverse_map(self):
        self.assertEqual(self.apm.STATUS_TASK.get(int(self.catalog_mod.ESCORT.status)), "escort")
        self.assertEqual(self.apm.STATUS_TASK.get(int(self.catalog_mod.SECT_TASK.status)), "sect_task")


class TestCatalogValidation(unittest.TestCase):
    def test_prefix_conflict_rejected(self):
        """构建期自检要能拦住前缀冲突的退出指令。"""
        mod = _imp("core.activity_catalog")
        bad = {
            1: mod.ActivitySpec(status=1, key="a", name="甲", exit_commands=("退出",)),
            2: mod.ActivitySpec(status=2, key="b", name="乙", exit_commands=("退出历练",)),
        }
        with self.assertRaises(ValueError):
            mod._validate(bad)

    def test_status_key_mismatch_rejected(self):
        mod = _imp("core.activity_catalog")
        bad = {1: mod.ActivitySpec(status=2, key="a", name="甲")}
        with self.assertRaises(ValueError):
            mod._validate(bad)

    def test_duplicate_key_rejected(self):
        mod = _imp("core.activity_catalog")
        bad = {
            1: mod.ActivitySpec(status=1, key="same", name="甲"),
            2: mod.ActivitySpec(status=2, key="same", name="乙"),
        }
        with self.assertRaises(ValueError):
            mod._validate(bad)


if __name__ == "__main__":
    unittest.main()
