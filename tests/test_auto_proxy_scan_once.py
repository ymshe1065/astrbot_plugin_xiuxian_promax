# tests/test_auto_proxy_scan_once.py
# 回归：一个玩家有几个活动槽位，list_enabled_auto_proxies 就返回几行
#
# 背景（v62 多槽位改造留下的回归）：
#   user_cd 主键从 user_id 改成 (user_id, slot) 之后，list_enabled_auto_proxies
#   仍然是 LEFT JOIN user_cd ON uc.user_id = ap.user_id —— 少了 slot 条件。
#   于是买满 4 尊身外化身的玩家（5 个槽位）每轮被 process_due 串行处理 5 遍，
#   施肥 / 灵田这类周期动作有重复执行、重复推送的机会。
#
#   联表出来的 cd_type / cd_scheduled_time 全仓无人读取
#   （_process_one 第一行就 self.get(user_id) 自己重查），
#   所以修法是直接去掉这个 JOIN，而不是补 slot = 0。
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


class TestAutoProxyScanOnce(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.UserStatus = _imp("models_extended").UserStatus

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)
        self.ext = DatabaseExtended(self.conn)

    async def asyncTearDown(self):
        await self.conn.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    async def _add_proxy(self, user_id: str, enabled: int = 1,
                         task_type: str = "", updated_at: int = 0):
        await self.conn.execute(
            "INSERT OR REPLACE INTO auto_proxy "
            "(user_id, enabled, task_type, origin, last_error, config_json, updated_at) "
            "VALUES (?, ?, ?, '', '', '{}', ?)",
            (user_id, int(enabled), task_type, int(updated_at)),
        )

    async def _add_slots(self, user_id: str, count: int, cd_type: int = 0):
        for slot in range(count):
            await self.conn.execute(
                "INSERT OR REPLACE INTO user_cd "
                "(user_id, slot, type, create_time, scheduled_time, extra_data) "
                "VALUES (?, ?, ?, 0, 0, '{}')",
                (user_id, slot, int(cd_type)),
            )

    async def test_five_slots_return_one_row(self):
        """1 个玩家 + 5 个槽位（本体 + 4 尊化身）→ 只返回 1 行。"""
        await self._add_proxy("u1", enabled=1, task_type="escort")
        await self._add_slots("u1", 5, cd_type=self.UserStatus.DELIVERING)
        await self.conn.commit()

        rows = await self.ext.list_enabled_auto_proxies()

        self.assertEqual(len(rows), 1, f"重复扫描回归：返回了 {len(rows)} 行，应为 1 行")
        self.assertEqual(rows[0]["user_id"], "u1")
        self.assertEqual(rows[0]["task_type"], "escort")

    async def test_rows_equal_enabled_players(self):
        """3 个玩家（分别 1 / 3 / 5 个槽位）→ 恰好 3 行。"""
        for user_id, slots in (("u1", 1), ("u2", 3), ("u3", 5)):
            await self._add_proxy(user_id, enabled=1, task_type="sect_task")
            await self._add_slots(user_id, slots, cd_type=self.UserStatus.SECT_TASK)
        await self.conn.commit()

        rows = await self.ext.list_enabled_auto_proxies()

        self.assertEqual(len(rows), 3)
        self.assertEqual({r["user_id"] for r in rows}, {"u1", "u2", "u3"})

    async def test_idle_slot_player_not_duplicated(self):
        """全空闲（type=0）的多槽位玩家也不能被放大成多行。"""
        await self._add_proxy("u1", enabled=1)
        await self._add_slots("u1", 4, cd_type=self.UserStatus.IDLE)
        await self.conn.commit()

        rows = await self.ext.list_enabled_auto_proxies()

        self.assertEqual(len(rows), 1)

    async def test_disabled_proxy_excluded(self):
        """关闭的代理不返回，也不因槽位数量变化。"""
        await self._add_proxy("u1", enabled=0)
        await self._add_slots("u1", 5)
        await self._add_proxy("u2", enabled=1)
        await self._add_slots("u2", 2)
        await self.conn.commit()

        rows = await self.ext.list_enabled_auto_proxies()

        self.assertEqual([r["user_id"] for r in rows], ["u2"])

    async def test_order_by_updated_at_kept(self):
        """ORDER BY updated_at ASC 必须保留（process_due 依赖处理顺序）。"""
        await self._add_proxy("late", enabled=1, updated_at=300)
        await self._add_proxy("early", enabled=1, updated_at=100)
        await self._add_proxy("mid", enabled=1, updated_at=200)
        await self.conn.commit()

        rows = await self.ext.list_enabled_auto_proxies()

        self.assertEqual([r["user_id"] for r in rows], ["early", "mid", "late"])

    async def test_joined_columns_are_gone(self):
        """cd_type / cd_scheduled_time 不再随行返回，防止 JOIN 被加回来。"""
        await self._add_proxy("u1", enabled=1)
        await self._add_slots("u1", 2, cd_type=self.UserStatus.DELIVERING)
        await self.conn.commit()

        rows = await self.ext.list_enabled_auto_proxies()

        self.assertNotIn("cd_type", rows[0])
        self.assertNotIn("cd_scheduled_time", rows[0])


if __name__ == "__main__":
    unittest.main()
