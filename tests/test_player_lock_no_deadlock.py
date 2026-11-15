# tests/test_player_lock_no_deadlock.py
"""玩家锁不可重入：同一指令链路里重复加锁会永久死锁。

`get_player_lock()` 返回的是普通 `asyncio.Lock`，同一任务重复 `async with`
不会抛错，而是永远等自己释放 —— 表现为该玩家所有指令与网页看板一起"没反应"，
日志里既没有 ERROR 也没有回复（v3.45.11 的 /仙缘兑换 即此问题）。
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = ROOT / "main.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _decorator_names(node: ast.FunctionDef) -> set:
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _functions(tree: ast.Module) -> dict:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _locked_handler_methods() -> dict:
    """handler 模块里带 @player_required 的方法：方法名 -> 模块名。"""
    found = {}
    for path in sorted((ROOT / "handlers").glob("*.py")):
        for name, node in _functions(_parse(path)).items():
            if "player_required" in _decorator_names(node):
                found.setdefault(name, set()).add(path.name)
    return found


class TestPlayerLockDeadlock(unittest.TestCase):
    def test_fate_exchange_locks_only_once(self):
        """main.py 的 /仙缘兑换 不能叠加 with_player_lock（内层已有 player_required）。"""
        main_funcs = _functions(_parse(MAIN_PY))
        outer = main_funcs["handle_fate_exchange"]
        self.assertNotIn(
            "with_player_lock",
            _decorator_names(outer),
            "main.handle_fate_exchange 又加了 @with_player_lock，会与内层 @player_required 死锁",
        )
        inner = _functions(_parse(ROOT / "handlers" / "reincarnation_handlers.py"))[
            "handle_fate_exchange"
        ]
        self.assertIn(
            "player_required",
            _decorator_names(inner),
            "内层 handle_fate_exchange 需要保留 @player_required 以提供玩家锁与状态校验",
        )

    def test_no_locked_entry_calls_locked_handler(self):
        """main.py 中 @with_player_lock 的入口不应再调用 @player_required 的 handler。

        方法名在多个 handler 模块中重名时（如 handle_status）无法静态确定目标，
        这里只对「全仓库唯一」的方法名做严格断言，避免误报。
        """
        locked = _locked_handler_methods()
        unique = {name for name, mods in locked.items() if len(mods) == 1}
        offenders = []
        for name, node in _functions(_parse(MAIN_PY)).items():
            if "with_player_lock" not in _decorator_names(node):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in unique
                    and isinstance(func.value, ast.Attribute)
                ):
                    offenders.append((name, func.attr, sorted(locked[func.attr])[0]))
        self.assertEqual(offenders, [], f"发现重复加锁的指令入口：{offenders}")


if __name__ == "__main__":
    unittest.main()
