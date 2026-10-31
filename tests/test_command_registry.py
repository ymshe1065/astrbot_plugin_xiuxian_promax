# tests/test_command_registry.py
# 指令注册回归：常量被后赋值覆盖、同名方法互相覆盖，都会让指令静默失效。
# 这里只做静态解析，不导入插件（避免拉起 astrbot 与数据库）。
import ast
import collections
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = ROOT / "main.py"
INVEST_HANDLERS_PY = ROOT / "handlers" / "invest_handlers.py"


def _resolve_command_constants(source: str) -> dict:
    """按运行时语义解析 CMD_* 常量：后赋值覆盖前赋值。"""
    consts = {}
    for match in re.finditer(r"^(CMD_[A-Z0-9_]+)\s*=\s*\"([^\"]*)\"", source, re.M):
        consts[match.group(1)] = match.group(2)
    return consts


def _registered_commands(source: str) -> list:
    consts = _resolve_command_constants(source)
    commands = []
    for match in re.finditer(r"@filter\.command\(\s*(CMD_[A-Z0-9_]+)\s*,", source):
        name = match.group(1)
        commands.append(consts.get(name, f"<未定义:{name}>"))
    return commands


def _module_level_commands(source: str) -> list:
    """所有 CMD_* 常量名（去重），用于检查定义但没有被任何指令引用的情况。"""
    return sorted(set(re.findall(r"^(CMD_[A-Z0-9_]+)\s*=", source, re.M)))


class TestCommandRegistry(unittest.TestCase):
    def setUp(self):
        self.source = MAIN_PY.read_text(encoding="utf-8")

    def test_no_duplicate_command_names(self):
        """同一个指令名不能被注册两次，否则后注册的会顶掉前者。"""
        duplicated = {
            name: count
            for name, count in collections.Counter(_registered_commands(self.source)).items()
            if count > 1
        }
        self.assertEqual(duplicated, {}, f"指令名重复注册：{duplicated}")

    def test_no_shadowed_handler_methods(self):
        """同名 async 方法会互相覆盖，被覆盖的那个永远不会执行。"""
        names = re.findall(r"^\s{4}async def (\w+)\(", self.source, re.M)
        duplicated = {
            name: count
            for name, count in collections.Counter(names).items()
            if count > 1
        }
        self.assertEqual(duplicated, {}, f"同名处理方法互相覆盖：{duplicated}")

    def test_command_constants_defined_once(self):
        """CMD_* 常量被重复赋值通常意味着复制粘贴改漏，最后一个会静默生效。"""
        assigned = re.findall(r"^(CMD_[A-Z0-9_]+)\s*=", self.source, re.M)
        duplicated = {
            name: count
            for name, count in collections.Counter(assigned).items()
            if count > 1
        }
        self.assertEqual(duplicated, {}, f"指令常量被重复赋值：{duplicated}")

    def test_all_referenced_constants_defined(self):
        missing = [c for c in _registered_commands(self.source) if c.startswith("<未定义:")]
        self.assertEqual(missing, [], f"引用了未定义的指令常量：{missing}")

    def test_invest_commands_registered(self):
        """仙市七件套必须全部注册为独立指令。"""
        commands = set(_registered_commands(self.source))
        expected = {"仙市", "行情", "买入股", "卖出股", "申购", "赎回", "我的持仓", "仙市流水", "仙市订阅"}
        self.assertTrue(expected.issubset(commands), f"缺失指令：{expected - commands}")


class TestInvestHandlerMethods(unittest.TestCase):
    """申购与风声订阅曾经共用 handle_subscribe，后者把前者整段覆盖掉。"""

    def setUp(self):
        tree = ast.parse(INVEST_HANDLERS_PY.read_text(encoding="utf-8"))
        self.cls = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "InvestHandlers"
        )
        self.methods = {
            node.name: node for node in self.cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_no_duplicate_methods(self):
        names = [
            node.name for node in self.cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        duplicated = {n: c for n, c in collections.Counter(names).items() if c > 1}
        self.assertEqual(duplicated, {}, f"同名方法互相覆盖：{duplicated}")

    def test_fund_subscribe_takes_code_and_amount(self):
        method = self.methods.get("handle_subscribe")
        self.assertIsNotNone(method, "缺少基金申购处理方法 handle_subscribe")
        args = [a.arg for a in method.args.args]
        self.assertEqual(args, ["self", "player", "event", "code", "amount"])

    def test_hint_subscribe_is_separate_method(self):
        method = self.methods.get("handle_hint_subscribe")
        self.assertIsNotNone(method, "缺少风声订阅开关方法 handle_hint_subscribe")
        args = [a.arg for a in method.args.args]
        self.assertEqual(args, ["self", "player", "event"])

    def test_main_delegates_to_right_handler(self):
        source = MAIN_PY.read_text(encoding="utf-8")
        self.assertIn("self.invest_handlers.handle_subscribe(event, code, amount)", source)
        self.assertIn("self.invest_handlers.handle_hint_subscribe(event)", source)


if __name__ == "__main__":
    unittest.main()
