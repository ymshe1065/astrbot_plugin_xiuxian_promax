# -*- coding: utf-8 -*-
"""全命令渲染通道普查（可复跑）。

用途：给《呈现层设计方案 v4》提供「每条指令实际走哪条渲染通道」的事实底表，
避免再次出现「出图 = 已结构化」这类口径错误。

用法：
    python scripts/audit_render.py            # 打印统计 + 写 docs/_render_audit.json
    python scripts/audit_render.py --md       # 额外输出 markdown 表格

判定口径（只看代码，不跑运行时）：
    image    —— 该命令（含其委派的 handler 方法）会 yield event.image_result(...)
    panel    —— 命中 webchat_room/visibility.PANEL_COMMANDS，走专用面板渲染
    payload  —— 出参带 payload_extra（结构化 groups/facets）
    text     —— 只有 plain_result
    unknown  —— 既无 plain 也无 image（纯转发/特殊分支，需人工看）
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
HANDLERS = ROOT / "handlers"
COMMANDS_PY = ROOT / "webchat_room" / "commands.py"
VISIBILITY = ROOT / "webchat_room" / "visibility.py"
OUT_JSON = ROOT / "docs" / "_render_audit.json"


# ---------- 1. 索引所有 handler 方法的输出形态 ----------

def _iter_methods(path: Path):
    """产出 (method_name, func_node)。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node


def _outputs_of(node: ast.AST) -> set[str]:
    """扫描函数体里的输出调用。"""
    found: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        # event.plain_result / event.image_result
        if isinstance(fn, ast.Attribute):
            if fn.attr.endswith("plain_result"):
                found.add("text")
            elif fn.attr.endswith("image_result"):
                found.add("image")
            elif fn.attr in ("render_card_async", "result_card", "render_card"):
                found.add("image")
        if isinstance(fn, ast.Name) and fn.id in ("render_card_async", "result_card"):
            found.add("image")
        # payload_extra=... 关键字
        for kw in sub.keywords:
            if kw.arg == "payload_extra":
                found.add("payload")
    return found


def build_handler_index() -> dict[str, set[str]]:
    index: dict[str, set[str]] = defaultdict(set)
    for path in sorted(HANDLERS.glob("*.py")):
        for name, node in _iter_methods(path):
            outs = _outputs_of(node)
            if outs:
                index[name] |= outs
    return index


# ---------- 2. 解析 main.py 的命令 ----------

def _cmd_name_from_decorator(dec: ast.AST) -> str | None:
    if not isinstance(dec, ast.Call):
        return None
    fn = dec.func
    if not isinstance(fn, ast.Attribute) or fn.attr != "command":
        return None
    if not dec.args:
        return None
    arg = dec.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    # 常量名（CMD_XXX）
    if isinstance(arg, ast.Name):
        return "{" + arg.id + "}"
    return None


def parse_commands() -> list[dict]:
    src = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = {
        t.id: n.value.value
        for n in tree.body
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
        for t in [n.targets[0]]
        if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
    }
    rows: list[dict] = []
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        # 类内方法索引：命令常委派给同类的 _run_xxx 私有方法
        local_index: dict[str, set[str]] = {}
        local_delegates: dict[str, set[str]] = {}
        for fn in cls.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local_index[fn.name] = _outputs_of(fn)
                deps: set[str] = set()
                for sub in ast.walk(fn):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                        deps.add(sub.func.attr)
                local_delegates[fn.name] = deps
        # 两轮回填：私有方法常委派给 handler 对象，需要把 handler 的输出形态带进来
        for _ in range(2):
            for name, deps in local_delegates.items():
                for d in deps:
                    local_index[name] |= HANDLER_INDEX.get(d, set()) | local_index.get(d, set())
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names: list[str] = []
            for dec in fn.decorator_list:
                raw = _cmd_name_from_decorator(dec)
                if raw:
                    if raw.startswith("{"):
                        raw = consts.get(raw[1:-1], raw)
                    names.append(raw)
            if not names:
                continue
            outs = _outputs_of(fn)
            # 委派的 handler 方法：self.<attr>.<method>(...) / self._run_xxx(...)
            delegates: list[str] = []
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    owner = sub.func.value
                    if isinstance(owner, ast.Name) and owner.id == "self":
                        delegates.append(sub.func.attr)
                    if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) and owner.value.id == "self":
                        delegates.append(sub.func.attr)
            for d in delegates:
                outs |= HANDLER_INDEX.get(d, set())
                outs |= local_index.get(d, set())
            rows.append(
                {
                    "cmd": names[0],
                    "aliases": names[1:],
                    "func": fn.name,
                    "line": fn.lineno,
                    "outputs": sorted(outs),
                    "delegates": sorted(set(delegates)),
                    "text": text_shape(fn),
                }
            )
    return rows


# ---------- 3. 目录分组 / 看板 / 面板 ----------

def parse_groups() -> dict[str, str]:
    """cmd -> 所属分组名（来自 webchat_room/commands.py）。"""
    text = COMMANDS_PY.read_text(encoding="utf-8")
    tree = ast.parse(text)
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        if "items" not in keys or "name" not in keys:
            continue
        gname = None
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                gname = v.value
        if not gname:
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "items" and isinstance(v, ast.List):
                for elt in v.elts:
                    if isinstance(elt, ast.Dict):
                        for kk, vv in zip(elt.keys, elt.values):
                            if isinstance(kk, ast.Constant) and kk.value == "cmd" and isinstance(vv, ast.Constant):
                                mapping[vv.value.lstrip("/")] = gname
    return mapping


def parse_panels() -> set[str]:
    """PANEL_COMMANDS = frozenset({"我的信息", "储物戒"}) —— 兼容任意包装调用。"""
    text = VISIBILITY.read_text(encoding="utf-8")
    m = re.search(r"^PANEL_COMMANDS\s*=\s*(.+)$", text, re.M)
    if not m:
        return set()
    return set(re.findall(r"[\"']([^\"']+)[\"']", m.group(1)))


HANDLER_INDEX = build_handler_index()


# ---------- 2.5 全局函数索引：按名字找到产出消息的函数体 ----------

SOURCE_DIRS = (ROOT / "handlers", ROOT / "managers", ROOT / "core")


def build_func_index() -> dict[str, list[ast.AST]]:
    index: dict[str, list[ast.AST]] = defaultdict(list)
    files = [MAIN] + [p for d in SOURCE_DIRS for p in sorted(d.glob("*.py"))]
    for path in files:
        for name, node in _iter_methods(path):
            index[name].append(node)
    return index


FUNC_INDEX = build_func_index()

_DIVIDER = re.compile(r"[━─═—]{5,}")
_RECORD = re.compile(r"^\s*[【\[]", re.M)
_KV = re.compile(r"^\s*[^\s：:]{1,18}\s*[：:]", re.M)
_BULLET = re.compile(r"^\s*[·•]\s*\S", re.M)
_CMD = re.compile(r"/[\u4e00-\u9fffA-Za-z][^\s，。、；：）)】\]|]{1,20}")
_PSEUDO_TABLE = re.compile(r"\S\s{2,}\S.*\s{2,}\S")


def _collect_strings(node: ast.AST, seen: set[str], depth: int = 0) -> list[str]:
    """收集函数体内（含向下 3 层委派）的字符串常量。"""
    if depth > 3:
        return []
    chunks: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            chunks.append(sub.value)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            name = sub.func.attr
            if name in seen:
                continue
            seen.add(name)
            for nxt in FUNC_INDEX.get(name, [])[:2]:
                chunks.extend(_collect_strings(nxt, seen, depth + 1))
    return chunks


def text_shape(node: ast.AST) -> dict:
    """从消息模板推断「这条命令的输出长什么样」。"""
    body = "\n".join(_collect_strings(node, set()))
    if not body:
        return {"lines": 0, "shape": "none"}
    lines = [ln for ln in body.split("\n") if ln.strip()]
    longest = max(lines, key=len) if lines else ""
    div = bool(_DIVIDER.search(body))
    rec = len(_RECORD.findall(body))
    kv = len(_KV.findall(body))
    bullets = len(_BULLET.findall(body))
    cmds = len(set(_CMD.findall(body)))
    pseudo = bool(_PSEUDO_TABLE.search(longest)) or any(
        _PSEUDO_TABLE.search(ln) for ln in lines[:20]
    )
    # 分层判定：越靠前越「有结构」，越靠后越难救
    if len(lines) <= 2:
        shape = "S1 单行反馈"
    elif rec and div:
        shape = "S2 记录列表（【】+分隔线）"
    elif kv >= 3:
        shape = "S3 键值面板"
    elif pseudo:
        shape = "S5 伪表格（空格对齐）"
    elif bullets >= 3:
        shape = "S4 要点列表（· / •）"
    else:
        shape = "S6 自由文本/嵌套条目"
    return {
        "lines": len(lines),
        "shape": shape,
        "divider": div,
        "records": rec,
        "kv": kv,
        "bullets": bullets,
        "cmds": cmds,
        "pseudo": pseudo,
        "max_len": len(longest),
    }


def main() -> None:
    rows = parse_commands()
    groups = parse_groups()
    panels = parse_panels()

    for r in rows:
        outs = set(r["outputs"])
        r["group"] = groups.get(r["cmd"], "")
        if r["cmd"] in panels:
            r["channel"] = "panel"
        elif "payload" in outs:
            r["channel"] = "payload"
        elif "image" in outs and "text" in outs:
            r["channel"] = "image+text"      # 图片失败降级文本
        elif "image" in outs:
            r["channel"] = "image"
        elif "text" in outs:
            r["channel"] = "text"
        else:
            r["channel"] = "unknown"

    stat = Counter(r["channel"] for r in rows)
    shapes = Counter(r["text"]["shape"] for r in rows)
    by_group: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_group[r["group"] or "(未分组)"][r["channel"]] += 1

    print(f"命令总数: {len(rows)}")
    print("通道分布:", dict(stat))
    print("形态分布:", dict(shapes))
    print("\n按分组:")
    for g, c in sorted(by_group.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(c.values())
        detail = "、".join(f"{k}={v}" for k, v in c.most_common())
        print(f"  {g:<10} {total:>3}  {detail}")

    print("\n按分组 · 形态:")
    gs: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        gs[r["group"] or "(未分组)"][r["text"]["shape"]] += 1
    for g, c in sorted(gs.items(), key=lambda kv: -sum(kv[1].values())):
        detail = "、".join(f"{k.split(' ')[0]}={v}" for k, v in c.most_common())
        print(f"  {g:<10} {detail}")

    unknown = [r for r in rows if r["channel"] == "unknown"]
    if unknown:
        print("\n未识别通道（需人工看）:", len(unknown))
        for r in unknown:
            print(f"  /{r['cmd']}  {r['func']}  main.py:{r['line']}  delegates={r['delegates']}")

    OUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写 {OUT_JSON.relative_to(ROOT)}")

    if "--md" in __import__("sys").argv:
        print("\n| 命令 | 分组 | 通道 | handler |")
        print("|---|---|---|---|")
        for r in sorted(rows, key=lambda x: (x["group"], x["cmd"])):
            print(f"| `/{r['cmd']}` | {r['group']} | {r['channel']} | {r['func']} |")


if __name__ == "__main__":
    main()
