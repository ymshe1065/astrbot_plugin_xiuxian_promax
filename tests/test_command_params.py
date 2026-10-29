"""指令参数回归：GreedyStr 不能带默认值。

AstrBot 的 ``CommandFilter.init_handler_md`` 会把「有默认值的参数」直接记录为默认值本身，
只有「无默认值」时才记录注解类型。因此 ``GreedyStr = ""`` 会被当成普通字符串默认值，
``validate_and_convert_params`` 里 ``is_greedy`` 判断失效，指令只接收第一个空格分隔的参数，
其余部分被丢弃（例如 ``装备 地品·灵纹戒指 戒指2`` 中的 ``戒指2``）。
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GREEDY_NAME = "GreedyStr"


def _annotation_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant):
        return node.value
    return None


def _iter_params(func_args):
    positional = list(func_args.posonlyargs) + list(func_args.args)
    defaults = [None] * (len(positional) - len(func_args.defaults)) + list(func_args.defaults)
    for arg, default in zip(positional, defaults):
        yield arg, default
    for arg, default in zip(func_args.kwonlyargs, func_args.kw_defaults):
        yield arg, default


class TestCommandParams(unittest.TestCase):
    def test_greedy_str_params_have_no_default(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        greedy_params = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for arg, default in _iter_params(node.args):
                if _annotation_name(arg.annotation) != GREEDY_NAME:
                    continue
                greedy_params.append(f"{node.name}.{arg.arg}")
                if default is not None:
                    offenders.append(f"{node.name}.{arg.arg} = {ast.unparse(default)}")
        self.assertTrue(greedy_params, "main.py 中应至少存在一个 GreedyStr 参数")
        self.assertEqual(
            offenders,
            [],
            "GreedyStr 参数不能带默认值，否则 AstrBot 会把它当成普通 str 只取第一个参数："
            + ", ".join(offenders),
        )

    def test_equip_command_is_greedy(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_equip_item"
        )
        params = {arg.arg: default for arg, default in _iter_params(func.args)}
        self.assertIn("item_name", params)
        self.assertIsNone(params["item_name"])
        annotation = next(arg for arg, _ in _iter_params(func.args) if arg.arg == "item_name")
        self.assertEqual(_annotation_name(annotation.annotation), GREEDY_NAME)

    def test_unequip_command_is_greedy(self):
        """回归：/卸下 戒指 2 需要完整接收栏位参数。"""
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_unequip_item"
        )
        params = {arg.arg: default for arg, default in _iter_params(func.args)}
        self.assertIn("slot_or_name", params)
        self.assertIsNone(params["slot_or_name"])
        annotation = next(arg for arg, _ in _iter_params(func.args) if arg.arg == "slot_or_name")
        self.assertEqual(_annotation_name(annotation.annotation), GREEDY_NAME)


if __name__ == "__main__":
    unittest.main()
