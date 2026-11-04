# tests/test_command_registry.py
# 指令注册回归：常量被后赋值覆盖、同名方法互相覆盖，都会让指令静默失效；
# 装饰器挤在同一行会被解析成矩阵乘法，语法检查能过但导入即崩。
# 这里只做静态解析，不导入插件（避免拉起 astrbot 与数据库）。
import ast
import collections
import io
import re
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = ROOT / "main.py"
INVEST_HANDLERS_PY = ROOT / "handlers" / "invest_handlers.py"

# 扫描范围：插件自身的 Python 源码（跳过测试与临时规划目录）
_SCAN_DIRS = ("core", "handlers", "managers", "data", "utils", "webchat_room")
_SCAN_SKIP_PARTS = {"tests", "_planning_tmp", "__pycache__", ".git"}


def _decorators_on_same_line(source: str) -> list:
    """找出把多个装饰器写在同一行的位置（如 `@a()    @b`）。

    Python 语法检查能通过，但会被解析成矩阵乘法 `a() @ b`，
    导入/定义类时对函数做 @ 运算，直接抛 TypeError。
    """
    at_per_line = collections.Counter()
    first_token_per_line = {}
    ignored = {
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.COMMENT,
        tokenize.ENDMARKER,
        tokenize.ENCODING,
    }
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type in ignored:
                continue
            first_token_per_line.setdefault(tok.start[0], tok)
            if tok.type == tokenize.OP and tok.string == "@":
                at_per_line[tok.start[0]] += 1
    except (tokenize.TokenError, IndentationError):
        return []

    offenders = []
    for lineno, count in at_per_line.items():
        if count < 2:
            continue
        first = first_token_per_line.get(lineno)
        if first is not None and first.type == tokenize.OP and first.string == "@":
            offenders.append(lineno)
    return sorted(offenders)


def _iter_plugin_sources():
    """插件自身的 Python 源码：根目录下的文件 + 白名单子目录。"""
    for py in ROOT.rglob("*.py"):
        rel = py.relative_to(ROOT)
        if _SCAN_SKIP_PARTS & set(rel.parts):
            continue
        if len(rel.parts) > 1 and rel.parts[0] not in _SCAN_DIRS:
            continue
        yield py


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

    def test_decorators_are_not_packed_on_one_line(self):
        """`@a()    @b` 会被解析成矩阵乘法 `a() @ b`，语法检查通过但导入即崩。"""
        offenders = [f"{py.relative_to(ROOT)}:{line}" for py in _iter_plugin_sources()
                     for line in _decorators_on_same_line(py.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [], f"这些行把多个装饰器写在了同一行：{offenders}")

    def test_warehouse_commands_registered(self):
        """一键取出是主命令，一键取回保留为旧名别名；单件取出另有一条。"""
        # 仓库系命令用字面量注册，_registered_commands 只认 CMD_* 常量，故直接查源码
        self.assertIn('@filter.command("一键转移"', self.source)
        self.assertIn('@filter.command("一键取出"', self.source)
        self.assertIn('@filter.command("一键取回"', self.source)
        self.assertIn('@filter.command("取出"', self.source)
        # 同一处理方法被两条指令共用是允许的，但方法名不能重复定义
        self.assertEqual(self.source.count("async def handle_take_item"), 1)
        self.assertEqual(self.source.count("async def handle_transfer_out("), 1)

    def test_invest_commands_registered(self):
        """仙市七件套必须全部注册为独立指令。"""
        commands = set(_registered_commands(self.source))
        expected = {"仙市", "行情", "买入股", "卖出股", "申购", "赎回", "我的持仓", "仙市流水", "仙市订阅"}
        self.assertTrue(expected.issubset(commands), f"缺失指令：{expected - commands}")

    def test_pill_bag_command_registered(self):
        """丹药背包 经 CMD_SHOW_PILLS 常量注册且只出现一次（自 test_pill_bag_command.py 归并）。"""
        self.assertIn('CMD_SHOW_PILLS = "丹药背包"', self.source)
        self.assertEqual(self.source.count("@filter.command(CMD_SHOW_PILLS"), 1)


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
