# tests/test_auto_proxy_rift.py
# 自动代理秘境：手动选本、境界校验、自动结算续接、关闭与手动退出隔离
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


class FakeEscortManager:
    def __init__(self, db, status):
        self.db = db
        self.status = status

    async def start_escort(self, player):
        await self.db.ext.set_user_busy(
            player.user_id, self.status.DELIVERING, int(time.time()) + 1800,
            {"escort_gold": 100, "escort_exp": 50},
        )
        return True, "escort started", {}

    async def finish_escort(self, player):
        await self.db.ext.set_user_free(player.user_id)
        return True, "escort finished"


class FakeSectManager:
    async def perform_sect_task(self, user_id):
        return False, "sect not used"

    async def finish_sect_task(self, user_id):
        return False, "sect not used"


class TestAutoProxyRift(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        models_extended = _imp("models_extended")
        self.UserStatus = models_extended.UserStatus
        self.Rift = models_extended.Rift
        self.AutoProxyManager = _imp("managers.auto_proxy_manager").AutoProxyManager
        self.RiftManager = _imp("managers.rift_manager").RiftManager
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
        await self.db.create_player(self.Player(user_id="u1", level_index=5))

        # 迁移脚本已内置 5 个秘境：ID1 青云秘境(需0)、ID2 落日峡谷(需3)、ID3 万妖洞(需6)
        self.rift_mgr = self.RiftManager(self.db, None, None)
        self.notices = []

        async def notifier(origin, msg):
            self.notices.append((origin, msg))

        self.bumps = []
        self.bounty_calls = []

        async def bump(uid, key, count):
            self.bumps.append((uid, key, count))
            return ""

        outer = self

        class Bounty:
            async def add_bounty_progress(self, player, tag, count=1):
                outer.bounty_calls.append((player.user_id, tag, count))
                return False, ""

        self.manager = self.AutoProxyManager(
            self.db, FakeEscortManager(self.db, self.UserStatus), FakeSectManager(),
            notifier=notifier, title_bump=bump, bounty_mgr=Bounty(),
            rift_mgr=self.rift_mgr,
        )

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _rift_cfg(self):
        row = await self.db.ext.get_auto_proxy("u1")
        return row, self.manager._load_config(row or {})

    async def test_enable_without_id_prompts_manual_choice(self):
        ok, msg = await self.manager.enable("u1", "umo://group", "秘境")
        self.assertFalse(ok)
        self.assertIn("请选择", msg)
        self.assertIn("青云秘境", msg)
        self.assertIn("落日峡谷", msg)
        self.assertNotIn("万妖洞", msg)  # 境界不足的不列
        self.assertIn("/自动代理 秘境", msg)
        self.assertIsNone(await self.db.ext.get_auto_proxy("u1"))

    async def test_enable_with_id_starts_exploration(self):
        ok, msg = await self.manager.enable("u1", "umo://group", "秘境 1")
        self.assertTrue(ok, msg)
        self.assertIn("秘境代理已开启", msg)
        row, config = await self._rift_cfg()
        self.assertEqual(row["enabled"], 1)
        self.assertTrue(config["rift"]["enabled"])
        self.assertEqual(config["rift"]["rift_id"], 1)
        cd = await self.db.ext.get_user_cd("u1")
        self.assertEqual(cd.type, self.UserStatus.EXPLORING)

    async def test_enable_rejects_rift_above_level(self):
        ok, msg = await self.manager.enable("u1", "umo://group", "秘境 3")
        self.assertFalse(ok)
        self.assertIn("境界不足", msg)
        self.assertIn("青云秘境", msg)

    async def test_process_due_finishes_and_reenters(self):
        await self.manager.enable("u1", "umo://group", "秘境 1")
        cd = await self.db.ext.get_user_cd("u1")
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.EXPLORING, int(time.time()) - 5,
            {"rift_id": 1, "rift_level": 1},
        )

        self.assertEqual(await self.manager.process_due(), 1)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("探索完成", self.notices[0][1])
        self.assertIn("已开始下一轮探索", self.notices[0][1])
        self.assertEqual(self.bumps, [("u1", "rift_count", 1)])
        self.assertEqual(self.bounty_calls, [("u1", "rift", 1)])
        cd = await self.db.ext.get_user_cd("u1")
        self.assertEqual(cd.type, self.UserStatus.EXPLORING)

        # 探索未到期时不做任何动作
        self.assertEqual(await self.manager.process_due(), 0)

    async def test_exploring_with_remaining_time_is_noop(self):
        await self.manager.enable("u1", "umo://group", "秘境 1")
        self.assertEqual(await self.manager.process_due(), 0)
        self.assertEqual(self.notices, [])

    async def test_close_rift_keeps_other_proxies(self):
        await self.manager.enable("u1", "umo://group", "秘境 1")
        row, config = await self._rift_cfg()
        config["farm"]["enabled"] = True
        await self.manager._write_state("u1", True, "", "umo://group", "", config)

        ok, msg = await self.manager.disable_rift("u1")
        self.assertTrue(ok)
        self.assertIn("秘境代理已关闭", msg)
        row, config = await self._rift_cfg()
        self.assertEqual(row["enabled"], 1)
        self.assertFalse(config["rift"]["enabled"])
        self.assertTrue(config["farm"]["enabled"])

    async def test_manual_exit_closes_rift_proxy(self):
        await self.manager.enable("u1", "umo://group", "秘境 1")
        self.assertTrue(await self.manager.note_manual_exit("u1"))
        row, config = await self._rift_cfg()
        self.assertEqual(row["enabled"], 0)
        self.assertFalse(config["rift"]["enabled"])
        self.assertEqual(row["last_error"], "manual_exit")

    async def test_manual_rift_entry_is_not_treated_as_external_activity(self):
        await self.manager.enable("u1", "umo://group", "秘境 1")
        self.assertFalse(await self.manager.note_external_activity_started("u1"))
        row, config = await self._rift_cfg()
        self.assertEqual(row["enabled"], 1)
        self.assertTrue(config["rift"]["enabled"])

    async def test_enable_primary_closes_rift(self):
        await self.manager.enable("u1", "umo://group", "秘境 1")
        await self.db.ext.set_user_free("u1")
        ok, msg = await self.manager.enable("u1", "umo://group", "押镖")
        self.assertIn("秘境代理已关闭", msg)
        _row, config = await self._rift_cfg()
        self.assertFalse(config["rift"]["enabled"])

    async def test_status_shows_rift_target(self):
        await self.manager.enable("u1", "umo://group", "秘境 1")
        text = await self.manager.status_text("u1")
        self.assertIn("秘境代理：运行中", text)
        self.assertIn("青云秘境", text)

    async def test_disable_handler_accepts_rift(self):
        handlers = _imp("handlers.auto_proxy_handlers").AutoProxyHandlers(self.db, self.manager)
        await self.manager.enable("u1", "umo://group", "秘境 1")

        class FakeEvent:
            unified_msg_origin = "umo://group"

            def get_sender_id(self):
                return "u1"

            def plain_result(self, text):
                return text

        results = []
        async for result in handlers.handle_disable(FakeEvent(), "秘境"):
            results.append(result)
        self.assertIn("秘境代理已关闭", results[0])


if __name__ == "__main__":
    unittest.main()
