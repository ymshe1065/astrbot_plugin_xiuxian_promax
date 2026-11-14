# tests/test_auto_proxy.py
# 自动代理：开启、绑定、结算续接、手动退出关闭、异常自动关闭
import os
import sys
import tempfile
import time
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


class FakeEscortManager:
    def __init__(self, db, status):
        self.db = db
        self.status = status
        self.started = 0
        self.finished = 0

    async def start_escort(self, player):
        from astrbot_plugin_xiuxian_promax.core.avatar_rules import total_slots

        if await self.db.ext.get_active_cd(player.user_id, self.status.DELIVERING) is not None:
            return False, "❌ 你已经在押镖途中了，先走完这一趟。", {}
        self.started += 1
        await self.db.ext.set_user_busy(
            player.user_id, self.status.DELIVERING, int(time.time()) + 1800,
            {"escort_gold": 100, "escort_exp": 50},
            max_slots=total_slots(player),
        )
        return True, "escort started", {}

    async def finish_escort(self, player):
        self.finished += 1
        await self.db.ext.set_user_free(
            player.user_id, cd_type=self.status.DELIVERING
        )
        return True, "escort finished"


class FakeSectManager:
    def __init__(self, db, status, start_ok=True):
        self.db = db
        self.status = status
        self.start_ok = start_ok
        self.started = 0
        self.finished = 0

    async def perform_sect_task(self, user_id):
        if not self.start_ok:
            return False, "❌ 你还未加入宗门！"
        if await self.db.ext.get_active_cd(user_id, self.status.SECT_TASK) is not None:
            return False, "❌ 宗门任务进行中，先完成或放弃当前这一轮。"
        from astrbot_plugin_xiuxian_promax.core.avatar_rules import total_slots

        self.started += 1
        player = await self.db.get_player_by_id(user_id)
        await self.db.ext.set_user_busy(
            user_id, self.status.SECT_TASK, int(time.time()) + 3600,
            max_slots=total_slots(player) if player else 1,
        )
        return True, "sect started"

    async def finish_sect_task(self, user_id):
        self.finished += 1
        await self.db.ext.set_user_free(user_id, cd_type=self.status.SECT_TASK)
        return True, "sect finished"


class TestAutoProxy(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.UserStatus = _imp("models_extended").UserStatus
        self.AutoProxyManager = _imp("managers.auto_proxy_manager").AutoProxyManager
        self.Player = _imp("models").Player

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        await self.db.create_player(self.Player(user_id="u1", level_index=5, sect_id=1))

        self.escort = FakeEscortManager(self.db, self.UserStatus)
        self.sect = FakeSectManager(self.db, self.UserStatus)
        self.notices = []

        async def notifier(origin, msg):
            self.notices.append((origin, msg))

        self.manager = self.AutoProxyManager(
            self.db, self.escort, self.sect, notifier=notifier,
        )

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def test_enable_without_task_requires_argument_unless_busy(self):
        ok, msg = await self.manager.enable("u1", "umo://group", "")
        self.assertFalse(ok)
        self.assertIn("请指定", msg)
        self.assertIsNone(await self.db.ext.get_auto_proxy("u1"))

        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.DELIVERING, int(time.time()) - 1,
            {"escort_gold": 100, "escort_exp": 50},
        )
        ok, msg = await self.manager.enable("u1", "umo://group", "")
        self.assertTrue(ok, msg)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(row["task_type"], "escort")

        processed = await self.manager.process_due()
        self.assertEqual(processed, 1)
        self.assertEqual(self.escort.finished, 1)
        self.assertEqual(self.escort.started, 1)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("已开始下一轮", self.notices[0][1])
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)

    async def test_manual_exit_disables_proxy(self):
        ok, _ = await self.manager.enable("u1", "umo://group", "押镖")
        self.assertTrue(ok)
        self.assertEqual(self.escort.started, 1)

        closed = await self.manager.note_manual_exit("u1")
        self.assertTrue(closed)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["task_type"], "escort")

    async def test_start_failure_disables_and_notifies(self):
        self.sect.start_ok = False
        ok, msg = await self.manager.enable("u1", "umo://group", "宗门任务")
        self.assertFalse(ok)
        self.assertIn("你还未加入宗门", msg)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 0)

    async def test_other_busy_state_waits_instead_of_closing(self):
        ok, _ = await self.manager.enable("u1", "umo://group", "押镖")
        self.assertTrue(ok)
        self.assertEqual(self.escort.started, 1)

        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.CULTIVATING, int(time.time()) + 3600
        )
        self.assertEqual(await self.manager.process_due(), 1)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(row["last_error"], "wait_slot")
        self.assertEqual(self.escort.started, 1)
        self.assertIn("等一个槽位", self.notices[-1][1])

        # 槽位空出来后自动续接
        await self.db.ext.set_user_free("u1")
        self.assertEqual(await self.manager.process_due(), 1)
        self.assertEqual(self.escort.started, 2)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(row["last_error"], "")

    async def test_external_activity_closes_proxy_immediately(self):
        await self.manager.enable("u1", "umo://group", "押镖")
        self.assertEqual((await self.db.ext.get_auto_proxy("u1"))["enabled"], 1)
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.CULTIVATING, int(time.time()) + 3600
        )
        self.assertTrue(await self.manager.note_external_activity_started("u1"))
        self.assertEqual((await self.db.ext.get_auto_proxy("u1"))["enabled"], 0)

    async def test_enable_refuses_when_other_activity_is_running(self):
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.CULTIVATING, int(time.time()) + 3600
        )
        ok, msg = await self.manager.enable("u1", "umo://group", "押镖")
        self.assertFalse(ok)
        self.assertIn("分身都出去了", msg)
        self.assertIsNone(await self.db.ext.get_auto_proxy("u1"))

    async def test_switch_task_takes_effect_after_current_round(self):
        ok, _ = await self.manager.enable("u1", "umo://group", "押镖")
        self.assertTrue(ok)
        self.assertEqual(self.escort.started, 1)

        ok, msg = await self.manager.enable("u1", "umo://group", "宗门任务")
        self.assertTrue(ok, msg)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["task_type"], "sect_task")

        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.DELIVERING, int(time.time()) - 1,
            {"escort_gold": 100, "escort_exp": 50},
        )
        await self.manager.process_due()
        self.assertEqual(self.escort.finished, 1)
        self.assertEqual(self.sect.started, 1)
        self.assertEqual(self.escort.started, 1)

    async def _handle_disable(self, args: str):
        handlers = _imp("handlers.auto_proxy_handlers").AutoProxyHandlers(self.db, self.manager)

        class FakeEvent:
            unified_msg_origin = "umo://group"

            def get_sender_id(self):
                return "u1"

            def plain_result(self, text):
                return text

        results = []
        async for result in handlers.handle_disable(FakeEvent(), args):
            results.append(result)
        return results

    async def _handle_enable(self, task: str):
        handlers = _imp("handlers.auto_proxy_handlers").AutoProxyHandlers(self.db, self.manager)

        class FakeEvent:
            unified_msg_origin = "umo://group"

            def get_sender_id(self):
                return "u1"

            def plain_result(self, text):
                return text

        results = []
        async for result in handlers.handle_enable(FakeEvent(), task):
            results.append(result)
        return results

    async def test_disable_without_scope_closes_all(self):
        self.assertTrue((await self.manager.enable("u1", "umo://group", "押镖"))[0])
        results = await self._handle_disable("")
        self.assertIn("全部关闭", results[0])
        self.assertEqual((await self.db.ext.get_auto_proxy("u1"))["enabled"], 0)

        self.assertTrue((await self.manager.enable("u1", "umo://group", "押镖"))[0])
        results = await self._handle_disable("押镖")
        self.assertIn("自动代理已关闭", results[0])
        self.assertEqual((await self.db.ext.get_auto_proxy("u1"))["enabled"], 0)

    async def test_disable_escort_keeps_farm(self):
        self.assertTrue((await self.manager.enable("u1", "umo://group", "押镖"))[0])
        row = await self.db.ext.get_auto_proxy("u1")
        config = self.manager._load_config(row)
        config["farm"]["enabled"] = True
        await self.manager._write_state(
            "u1", True, "escort", "umo://group", "", config
        )

        results = await self._handle_disable("押镖")
        self.assertIn("灵田自动代理继续运行", results[0])
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)
        self.assertTrue(self.manager._load_config(row)["farm"]["enabled"])
        self.assertEqual(row["task_type"], "")

    async def test_disabled_primary_does_not_renew_current_task(self):
        self.assertTrue((await self.manager.enable("u1", "umo://group", "押镖"))[0])
        row = await self.db.ext.get_auto_proxy("u1")
        config = self.manager._load_config(row)
        config["farm"]["enabled"] = True
        await self.manager._write_state(
            "u1", True, "escort", "umo://group", "", config
        )
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.DELIVERING, 0,
            {"escort_gold": 100, "escort_exp": 50},
        )

        ok, _ = await self.manager.disable_primary("u1", "manual_close")
        self.assertTrue(ok)
        await self.manager.process_due()

        self.assertEqual(self.escort.finished, 0)
        self.assertEqual(self.escort.started, 1)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(row["task_type"], "")
        self.assertTrue(self.manager._load_config(row)["farm"]["enabled"])

    async def test_auto_proxy_off_alias_closes_all(self):
        self.assertTrue((await self.manager.enable("u1", "umo://group", "押镖"))[0])
        results = await self._handle_enable("关闭")
        self.assertIn("全部关闭", results[0])
        self.assertEqual((await self.db.ext.get_auto_proxy("u1"))["enabled"], 0)

    async def test_finish_side_effects(self):
        bumps = []
        bounty_calls = []

        async def bump(uid, key, count):
            bumps.append((uid, key, count))
            return ""

        class Bounty:
            async def add_bounty_progress(self, player, tag, count=1):
                bounty_calls.append((player.user_id, tag, count))
                return False, ""

        manager = self.AutoProxyManager(
            self.db, self.escort, self.sect,
            notifier=None, title_bump=bump, bounty_mgr=Bounty(),
        )
        await manager.enable("u1", "umo://group", "押镖")
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.DELIVERING, int(time.time()) - 1,
            {"escort_gold": 100, "escort_exp": 50},
        )
        await manager.process_due()
        self.assertEqual(bumps, [("u1", "escort_count", 1)])
        self.assertEqual(bounty_calls, [("u1", "escort", 1)])


    async def _set_avatars(self, count: int):
        import json as _json

        player = await self.db.get_player_by_id("u1")
        player.permanent_bonus = _json.dumps({"avatar_slots": int(count)})
        await self.db.update_player(player)

    async def test_two_primary_tasks_both_settle_in_one_pass(self):
        """本体押镖 + 化身宗门任务：两个槽位都要结算，不能只看第一条。"""
        await self._set_avatars(1)  # 2 个槽位
        due = int(time.time()) - 1
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.DELIVERING, due,
            {"escort_gold": 100, "escort_exp": 50}, max_slots=2,
        )
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.SECT_TASK, due, max_slots=2
        )
        ok, msg = await self.manager.enable("u1", "umo://group")
        self.assertTrue(ok, msg)

        await self.manager.process_due()
        self.assertEqual(self.escort.finished, 1)
        self.assertEqual(self.sect.finished, 1)
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)

    async def test_primary_survives_avatar_holding_other_activity(self):
        """化身在闭关时，本体押镖代理不能被「检测到别处忙碌」关掉。"""
        await self._set_avatars(1)
        ok, msg = await self.manager.enable("u1", "umo://group", "押镖")
        self.assertTrue(ok, msg)
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.CULTIVATING, int(time.time()) + 3600, max_slots=2
        )
        await self.manager.process_due()
        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)
        self.assertFalse(any("检测到你进入了" in m for _, m in self.notices))

        # 押镖到期后照常结算并续接
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.DELIVERING, int(time.time()) - 1,
            {"escort_gold": 100, "escort_exp": 50}, max_slots=2,
        )
        await self.manager.process_due()
        self.assertEqual(self.escort.finished, 1)
        self.assertEqual(self.escort.started, 2)
        self.assertEqual((await self.db.ext.get_auto_proxy("u1"))["enabled"], 1)

    async def test_primary_waits_instead_of_closing_when_slots_full(self):
        """没有可接管的押镖/宗门任务、槽位又被占满时，等待而不是关代理。"""
        ok, _ = await self.manager.enable("u1", "umo://group", "押镖")
        self.assertTrue(ok)
        # 押镖槽被别的活动接管（模拟玩家改去历练），唯一槽位占满
        await self.db.ext.free_slot("u1", 0)
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.ADVENTURING, int(time.time()) + 3600
        )
        await self.manager.process_due()

        row = await self.db.ext.get_auto_proxy("u1")
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(row["last_error"], "wait_slot")
        self.assertEqual(self.escort.started, 1)  # 没再起新一轮
        self.assertTrue(any("等一个槽位" in m for _, m in self.notices))
        self.assertFalse(any("检测到你进入了" in m for _, m in self.notices))

    async def test_start_activity_blocks_injured_player(self):
        cm = _imp("config_manager").ConfigManager(ROOT)
        self.escort.config_manager = cm
        player = await self.db.get_player_by_id("u1")
        player.hp = 1
        await self.db.update_player(player)
        player = await self.db.get_player_by_id("u1")
        ok, msg = await self.manager.start_activity(player, "escort")
        self.assertFalse(ok)
        self.assertIn("重伤", msg)
        self.assertEqual(self.escort.started, 0)


if __name__ == "__main__":
    unittest.main()
