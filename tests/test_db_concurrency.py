# 全局数据库并发回归测试：任务连接隔离 / 事务入口 / 嵌套保存点
import asyncio
import os
import sys
import tempfile
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


class TestDbConcurrency(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.DataBase = _imp("data.data_manager").DataBase
        cls.TaskConnectionRouter = _imp("data.task_connection").TaskConnectionRouter

    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = self.DataBase(self.db_path)
        await self.db.connect()
        await self.db.conn.execute(
            "CREATE TABLE acc (id TEXT PRIMARY KEY, gold INTEGER NOT NULL DEFAULT 0)"
        )
        await self.db.conn.execute(
            "CREATE TABLE log (id INTEGER PRIMARY KEY AUTOINCREMENT, worker INTEGER)"
        )
        await self.db.conn.execute(
            "CREATE TABLE shop (shop_id TEXT PRIMARY KEY, last_refresh_time INTEGER, current_items TEXT)"
        )
        await self.db.conn.execute("INSERT INTO acc (id, gold) VALUES ('a', 0), ('b', 0)")
        await self.db.conn.commit()

    async def asyncTearDown(self):
        await self.db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except OSError:
                pass

    async def _gold(self, key: str) -> int:
        async with self.db.conn.execute("SELECT gold FROM acc WHERE id = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        return int(row[0])

    def test_router_is_installed(self):
        self.assertIsInstance(self.db.conn, self.TaskConnectionRouter)
        self.assertTrue(self.db._connection_alive())

    async def test_each_task_gets_its_own_connection(self):
        conn1 = await self.db.raw_connection()
        conn2 = await asyncio.create_task(self.db.raw_connection())
        self.assertIsNot(conn1, conn2)
        self.assertIsNot(conn1, self.db.conn.default_connection)

    async def test_parallel_transactions_do_not_roll_back_each_other(self):
        async def writer(key: str, fail: bool):
            try:
                async with self.db.transaction() as conn:
                    await conn.execute(
                        "UPDATE acc SET gold = gold + 10 WHERE id = ?", (key,)
                    )
                    await asyncio.sleep(0.05)
                    if fail:
                        raise RuntimeError("模拟事务内异常")
            except RuntimeError:
                pass

        await asyncio.gather(writer("a", False), writer("b", True))
        self.assertEqual(await self._gold("a"), 10)
        self.assertEqual(await self._gold("b"), 0)

    async def test_nested_transaction_rolls_back_only_inner(self):
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE acc SET gold = gold + 5 WHERE id = 'a'")
            try:
                async with self.db.transaction() as inner:
                    await inner.execute(
                        "UPDATE acc SET gold = gold + 100 WHERE id = 'a'"
                    )
                    raise RuntimeError("内层失败")
            except RuntimeError:
                pass
        self.assertEqual(await self._gold("a"), 5)

    async def test_explicit_begin_inside_transaction_degrades_to_savepoint(self):
        async with self.db.transaction():
            # 旧代码里的 BEGIN IMMEDIATE 在事务内不应再报错
            await self.db.conn.execute("BEGIN IMMEDIATE")
            await self.db.conn.execute("UPDATE acc SET gold = gold + 1 WHERE id = 'a'")
            await self.db.conn.commit()
            self.assertTrue(self.db.conn.in_transaction)
            await self.db.conn.execute("UPDATE acc SET gold = gold + 1 WHERE id = 'a'")
        self.assertEqual(await self._gold("a"), 2)

    async def test_parallel_implicit_writes_do_not_conflict(self):
        async def burst(worker: int):
            for _ in range(10):
                await self.db.conn.execute(
                    "INSERT INTO log (worker) VALUES (?)", (worker,)
                )
                await self.db.conn.commit()

        await asyncio.gather(*(burst(i) for i in range(8)))
        async with self.db.conn.execute("SELECT COUNT(*) FROM log") as cursor:
            self.assertEqual(int((await cursor.fetchone())[0]), 80)

    async def test_concurrent_increments_are_not_lost(self):
        async def inc():
            for _ in range(5):
                async with self.db.transaction() as conn:
                    await conn.execute(
                        "UPDATE acc SET gold = gold + 1 WHERE id = 'a'"
                    )

        await asyncio.gather(*(inc() for _ in range(10)))
        self.assertEqual(await self._gold("a"), 50)

    async def test_concurrent_shop_decrement_never_oversells(self):
        items = [
            {"id": 1, "name": "聚气丹", "quality": "一般", "stock": 5, "price": 10}
        ]
        await self.db.update_shop_data("global", 0, items)

        async def buy():
            return await self.db.decrement_shop_item_stock(
                "global", "聚气丹", 1, entry_key={"id": 1}
            )

        results = await asyncio.gather(*(buy() for _ in range(10)))
        self.assertEqual(sum(1 for r in results if r[0]), 5)
        _refresh, final_items = await self.db.get_shop_data("global")
        self.assertEqual(final_items[0]["stock"], 0)

    async def test_uncommitted_changes_are_isolated_between_tasks(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def writer():
            async with self.db.transaction() as conn:
                await conn.execute("UPDATE acc SET gold = 999 WHERE id = 'a'")
                started.set()
                await release.wait()

        async def reader():
            await started.wait()
            async with self.db.conn.execute(
                "SELECT gold FROM acc WHERE id = 'a'"
            ) as cursor:
                row = await cursor.fetchone()
            return int(row[0])

        writer_task = asyncio.create_task(writer())
        try:
            observed = await asyncio.create_task(reader())
        finally:
            release.set()
            await writer_task

        self.assertEqual(observed, 0)
        self.assertEqual(await self._gold("a"), 999)


if __name__ == "__main__":
    unittest.main()
