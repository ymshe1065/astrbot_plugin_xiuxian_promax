# -*- coding: utf-8 -*-
"""提取每条命令「实际拼出来的回复文本」的源码模板。

用途：给《呈现层改造对照》提供可信的「改前」证据——直接取 handler 里
plain_result / image_result / render_card_async 的第一个实参，ast.unparse 成
可读模板（变量保留为 {xxx} 形态），并标注 file:line。

用法：
    python scripts/dump_templates.py                 # 全部命令 → docs/_cmd_templates.json
    python scripts/dump_templates.py 符箓书 丹方      # 只看指定命令
"""
from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
OUT = ROOT / "docs" / "_cmd_templates.json"
DIRS = (ROOT / "handlers", ROOT / "managers", ROOT / "core")

RENDER_CALLS = ("plain_result", "image_result", "render_card_async", "result_card", "render_card")


def methods_of(path: Path) -> dict[str, list[ast.AST]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return {}
    out: dict[str, list[ast.AST]] = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name].append(node)
    return out


INDEX: dict[str, list[tuple[Path, ast.AST]]] = defaultdict(list)
for p in [MAIN] + [f for d in DIRS for f in sorted(d.glob("*.py"))]:
    for name, nodes in methods_of(p).items():
        for node in nodes:
            INDEX[name].append((p, node))


def template_of(node: ast.AST, path: Path) -> list[dict]:
    """取出该函数里所有渲染调用的第一实参。"""
    rows: list[dict] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
        if name not in RENDER_CALLS:
            continue
        arg = sub.args[0] if sub.args else None
        if arg is None:
            continue
        try:
            src = ast.unparse(arg)
        except Exception:
            src = "<unparse failed>"
        rows.append(
            {
                "kind": name,
                "loc": f"{path.relative_to(ROOT).as_posix()}:{sub.lineno}",
                "src": src,
                "len": len(src),
            }
        )
    return rows


def ordered_strings(node: ast.AST, limit: int = 60) -> list[str]:
    """按书写位置收集函数体内的 f-string / 字符串常量（含深层）。"""
    items: list[tuple[int, int, str]] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if not sub.value.strip():
                continue
            items.append((sub.lineno, sub.col_offset, sub.value))
        elif isinstance(sub, ast.JoinedStr):
            try:
                items.append((sub.lineno, sub.col_offset, ast.unparse(sub)))
            except Exception:
                pass
    # JoinedStr 会与内部常量重复，按行去重并保留最外层
    seen: set[tuple[int, str]] = set()
    out: list[str] = []
    for line, _col, s in sorted(items):
        key = (line, s[:40])
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def main_consts() -> dict[str, str]:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                out[n.targets[0].id] = n.value.value
    return out


CONSTS = main_consts()


def resolve(cmd: str) -> dict:
    """cmd → main.py 里的命令函数，再展开 2 层委派。"""
    entry = None
    main_methods = methods_of(MAIN)
    for fname, nodes in main_methods.items():
        for node in nodes:
            for dec in getattr(node, "decorator_list", []):
                if not isinstance(dec, ast.Call) or not dec.args:
                    continue
                a = dec.args[0]
                val = None
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    val = a.value
                elif isinstance(a, ast.Name):
                    val = CONSTS.get(a.id)
                if val == cmd:
                    entry = (fname, node)
    if not entry:
        return {"cmd": cmd, "found": False, "templates": []}

    seen: set[str] = set()
    queue = [entry[0]]
    templates: list[dict] = []
    frags: list[dict] = []
    while queue:
        fname = queue.pop(0)
        if fname in seen:
            continue
        seen.add(fname)
        for path, node in INDEX.get(fname, [])[:3]:
            templates.extend(template_of(node, path))
            for s in ordered_strings(node):
                frags.append({"loc": f"{path.relative_to(ROOT).as_posix()}:{node.lineno}", "src": s})
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    callee = sub.func.attr
                    if callee in seen or callee in RENDER_CALLS:
                        continue
                    if callee not in INDEX:
                        continue
                    # 只跟两层，避免把整个管理器拖进来
                    if len(seen) < 8:
                        queue.append(callee)
    return {"cmd": cmd, "found": True, "entry": entry[0], "templates": templates, "frags": frags}


def main() -> None:
    rows = json.loads((ROOT / "docs" / "_render_audit.json").read_text(encoding="utf-8"))
    want = sys.argv[1:]
    out: dict[str, dict] = {}
    for r in rows:
        cmd = r["cmd"]
        if want and cmd not in want:
            continue
        info = resolve(cmd)
        info["group"] = r["group"]
        info["channel"] = r["channel"]
        out[cmd] = info
    if want:
        for cmd in want:
            info = out.get(cmd, {"found": False})
            print(f"\n===== /{cmd}  ({info.get('group')}, {info.get('channel')})")
            if info.get("templates"):
                for t in info["templates"]:
                    print(f"--- {t['kind']}  {t['loc']}")
                    print(t["src"][:1400])
            else:
                for f in (info.get("frags") or [])[:40]:
                    print(f"  · {f['src']}")
    else:
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        hit = sum(1 for v in out.values() if v.get("templates"))
        print(f"命令 {len(out)} 条，命中模板 {hit} 条 → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
