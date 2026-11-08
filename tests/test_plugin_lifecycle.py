"""执行实际卸载方法，验证后台任务清理先于数据库关闭。"""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1]


def _terminate_method():
    # 仅加载生产方法，避免测试启动 AstrBot 的平台注册与所有 manager。
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                  and node.name == "XiuXianPlugin")
    method = next(node for node in plugin.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "terminate")
    namespace = {"asyncio": asyncio, "logger": Mock()}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(ROOT / "main.py"), "exec"), namespace)
    return namespace["terminate"]


class PluginLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_terminate_waits_for_cancelled_task_cleanup_before_closing_database(self):
        cleanup = []

        async def background():
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleanup.append("rollback")

        async def close_database():
            cleanup.append("close")

        task = asyncio.create_task(background())
        await asyncio.sleep(0)
        plugin = SimpleNamespace(
            boss_task=task, raid_task=None, loan_check_task=None, spirit_eye_task=None,
            bounty_check_task=None, demon_check_task=None,
            db=SimpleNamespace(close=AsyncMock(side_effect=close_database)),
        )
        try:
            await _terminate_method()(plugin)
            self.assertEqual(cleanup, ["rollback", "close"])
            self.assertTrue(task.done())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failed_background_task_does_not_prevent_database_close(self):
        async def failed():
            raise RuntimeError("background failed")

        task = asyncio.create_task(failed())
        await asyncio.sleep(0)
        plugin = SimpleNamespace(
            boss_task=task, raid_task=None, loan_check_task=None, spirit_eye_task=None,
            bounty_check_task=None, demon_check_task=None,
            db=SimpleNamespace(close=AsyncMock()),
        )
        await _terminate_method()(plugin)
        plugin.db.close.assert_awaited_once()
        await asyncio.gather(task, return_exceptions=True)
