# tests/test_webchat_command_coverage.py
# 网页聊天室指令目录完整性：所有注册指令都应在按钮网格中，或明确登记为别名。
# 只做静态解析，不导入插件本体。
import collections
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

from importlib import import_module  # noqa: E402

commands = import_module(f"{PKG}.webchat_room.commands")

_COMMAND_RE = re.compile(r"@filter\.command\(\s*(CMD_[A-Z0-9_]+|\"[^\"]*\")\s*,")
_CONST_RE = re.compile(r"^(CMD_[A-Z0-9_]+)\s*=\s*\"([^\"]*)\"", re.M)


def registered_commands() -> list[str]:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    consts = {m.group(1): m.group(2) for m in _CONST_RE.finditer(source)}
    names = []
    for match in _COMMAND_RE.finditer(source):
        token = match.group(1)
        if token.startswith("CMD_"):
            names.append(consts.get(token, f"<未定义:{token}>"))
        else:
            names.append(token.strip('"'))
    return names


def grid_names() -> list[str]:
    return [item["cmd"].lstrip("/") for cat in commands.CATEGORIES for item in cat["items"]]


class TestWebCommandCoverage(unittest.TestCase):
    def test_every_registered_command_is_covered(self):
        """功能指令必须能点到；纯别名可在聊天框直接输入。"""
        grid = set(grid_names())
        missing = [
            name
            for name in registered_commands()
            if name not in grid and name not in commands.ALIASES
        ]
        self.assertEqual(missing, [], f"这些指令没有进入网页按钮目录：{missing}")

    def test_no_duplicate_buttons(self):
        duplicated = {
            name: count
            for name, count in collections.Counter(grid_names()).items()
            if count > 1
        }
        self.assertEqual(duplicated, {}, f"按钮目录出现重复指令：{duplicated}")

    def test_grid_commands_are_registered(self):
        registered = set(registered_commands())
        extra = sorted(set(grid_names()) - registered)
        self.assertEqual(extra, [], f"按钮目录包含未注册指令：{extra}")

    def test_aliases_are_registered_and_not_in_grid(self):
        registered = set(registered_commands())
        grid = set(grid_names())
        self.assertEqual(sorted(commands.ALIASES - registered), [], "别名列表包含未注册指令")
        self.assertEqual(sorted(commands.ALIASES & grid), [], "别名不应重复占用按钮")

    def test_arg_buttons_have_spec(self):
        missing = [
            item["cmd"].lstrip("/")
            for cat in commands.CATEGORIES
            for item in cat["items"]
            if item.get("arg") and item["cmd"].lstrip("/") not in commands.ARG_SPEC
        ]
        self.assertEqual(missing, [], f"带参按钮缺少 ARG_SPEC：{missing}")

    def test_categories_well_formed(self):
        for cat in commands.CATEGORIES:
            self.assertTrue(cat.get("id") and cat.get("name") and cat.get("icon"), cat)
            self.assertTrue(cat.get("items"), f"空分类：{cat.get('name')}")
            for item in cat["items"]:
                self.assertTrue(item.get("cmd", "").startswith("/"), item)
                self.assertTrue(item.get("label"), item)


if __name__ == "__main__":
    unittest.main()
