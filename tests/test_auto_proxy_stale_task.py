# tests/test_auto_proxy_stale_task.py
# 自动代理：关闭后残留的 task_type 不得挡住秘境/历练代理
#
# 背景：``task_type`` 是「上次跑的活动」，/关闭代理 会特意保留它方便下次一键恢复。
# 但秘境与历练代理的互斥判定曾直接看「task_type 有没有值」，于是玩家只要开过押镖
# 或宗门任务代理再关掉，这两个代理就永久开不起来——再点 /关闭代理 只会把同样的值
# 写回去，/解卡 也只清 user_cd，形成死锁。判定必须带上总开关 enabled。
import os
import sys
import tempfile
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


class TestAutoProxyStaleTaskType(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        models_extended = _imp("models_extended")
        DataBase = _imp("data").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.UserStatus = models_extended.UserStatus
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

        self.rift_mgr = self.RiftManager(self.db, None, None)
        self.manager = self.AutoProxyManager(
            self.db, None, None, rift_mgr=self.rift_mgr
        )

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _row(self):
        return await self.db.ext.get_auto_proxy("u1")

    async def test_stale_task_type_does_not_block_rift(self):
        """关掉押镖代理后，秘境代理必须还能开。"""
        # 押镖代理跑过一轮并被关闭：enabled=0，task_type 作为「上次活动」保留
        await self.db.ext.upsert_auto_proxy("u1", 0, "escort", "", "", "{}")

        ok, msg = await self.manager.enable("u1", "umo://group", "秘境 1")
        self.assertTrue(ok, msg)
        self.assertIn("秘境代理已开启", msg)
        row = await self._row()
        self.assertEqual(row["enabled"], 1)
        self.assertEqual(row["task_type"], "")  # 开秘境时清空任务目标

    async def test_running_primary_allows_rift_in_parallel(self):
        """押镖代理真的在跑时，秘境代理照样能开：两条各占一个分身槽。"""
        await self.db.ext.upsert_auto_proxy("u1", 1, "escort", "押镖", "", "{}")

        ok, msg = await self.manager.enable("u1", "umo://group", "秘境 1")
        self.assertTrue(ok, msg)
        self.assertIn("秘境代理已开启", msg)
        row = await self._row()
        # 押镖代理还在跑，任务目标必须留着，否则 tick 就不续押镖了
        self.assertEqual(row["task_type"], "escort")
        self.assertEqual(row["enabled"], 1)

    async def test_disable_then_rift_is_not_a_deadlock(self):
        """复现玩家处境：开着押镖代理 → 关掉 → 再开秘境，不能被卡住。"""
        await self.db.ext.upsert_auto_proxy("u1", 1, "escort", "押镖", "", "{}")
        ok1, _ = await self.manager.enable("u1", "umo://group", "秘境 1")
        self.assertTrue(ok1)

        await self.manager.disable("u1", "manual_close")
        row = await self._row()
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["task_type"], "escort")  # 关闭时刻意保留

        ok2, msg2 = await self.manager.enable("u1", "umo://group", "秘境 1")
        self.assertTrue(ok2, msg2)
        row = await self._row()
        # 残留的 last task 不能被这次写回复活成「押镖代理在跑」
        self.assertEqual(row["task_type"], "")

    def test_primary_running_truth_table(self):
        """task_type 有值 ≠ 代理在跑；必须 enabled 为真。"""
        running = self.AutoProxyManager._primary_running
        self.assertFalse(running(None))
        self.assertFalse(running({}))
        self.assertFalse(running({"enabled": 0, "task_type": "escort"}))
        self.assertFalse(running({"enabled": 1, "task_type": ""}))
        self.assertFalse(running({"enabled": 1, "task_type": "   "}))
        self.assertTrue(running({"enabled": 1, "task_type": "escort"}))

    def test_any_proxy_active_ignores_stale_task_type(self):
        """残留值不能让总开关误判成「还有代理在跑」。"""
        any_active = self.AutoProxyManager._any_proxy_active
        off = {"farm": {"enabled": False}, "fertilize": {"enabled": False},
               "rift": {"enabled": False}, "adventure": {"enabled": False}}
        self.assertFalse(any_active(False, off))
        on = dict(off, rift={"enabled": True})
        self.assertTrue(any_active(False, on))

    async def test_clear_stale_state_clears_closed_proxy(self):
        """/解卡 的兜底：清掉「已关闭代理」残留的目标活动。"""
        await self.db.ext.upsert_auto_proxy("u1", 0, "escort", "押镖", "", "{}")
        self.assertTrue(await self.manager.clear_stale_state("u1"))
        row = await self._row()
        self.assertEqual(row["task_type"], "")
        self.assertEqual(row["enabled"], 0)

    async def test_clear_stale_state_keeps_running_proxy(self):
        """正在跑的代理绝不能被解卡清掉目标。"""
        await self.db.ext.upsert_auto_proxy("u1", 1, "escort", "押镖", "", "{}")
        self.assertFalse(await self.manager.clear_stale_state("u1"))
        row = await self._row()
        self.assertEqual(row["task_type"], "escort")

    async def test_clear_stale_state_keeps_rift_proxy(self):
        """只开着秘境代理时，task_type 本来就是空的，不该被误判成残留。"""
        await self.db.ext.upsert_auto_proxy(
            "u1", 1, "", "", "",
            '{"rift": {"enabled": true, "rift_id": 1}}',
        )
        self.assertFalse(await self.manager.clear_stale_state("u1"))
        row = await self._row()
        self.assertEqual(row["enabled"], 1)

    async def test_stale_task_type_does_not_block_adventure(self):
        """历练代理与押镖代理互斥，同样不能被残留值挡住。"""
        class _FakeAdv:
            routes = {}

            def resolve_route(self, token):
                return ""

        self.manager.adventure_mgr = _FakeAdv()
        await self.db.ext.upsert_auto_proxy("u1", 0, "escort", "", "", "{}")

        ok, msg = await self.manager._enable_adventure("u1", "umo://group", "历练 不存在的路线")
        self.assertNotIn("互斥", msg)
        self.assertIn("找不到可代理的历练路线", msg)

    async def test_running_primary_does_not_block_adventure(self):
        """押镖代理在跑时，历练代理照样能开：两条各占一个分身槽。"""
        class _FakeAdv:
            routes = {}

            def resolve_route(self, token):
                return ""

        self.manager.adventure_mgr = _FakeAdv()
        await self.db.ext.upsert_auto_proxy("u1", 1, "escort", "押镖", "", "{}")

        ok, msg = await self.manager._enable_adventure("u1", "umo://group", "历练 不存在的路线")
        # 不再因为「押镖代理在跑」被拒，只按路线本身报错
        self.assertNotIn("互斥", msg)
        self.assertIn("找不到可代理的历练路线", msg)


if __name__ == "__main__":
    unittest.main()
