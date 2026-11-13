# -*- coding: utf-8 -*-
"""生成《呈现层改造对照》第一章的待改清单表。

数据源：
    docs/_render_audit.json    —— 通道 / 形态（scripts/audit_render.py）
    docs/_cmd_templates.json   —— 回复模板（scripts/dump_templates.py）

输出：markdown 行，形如
    | `/符箓书` | 丹道符箓 | 文本 | 35 | 【攻击】烈焰符（凡品）… | recipe | P0 |

用法：
    python scripts/gen_compare_table.py            # 全部
    python scripts/gen_compare_table.py --probe    # 抽查首行提取质量
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from gen_render_table import P0, build as _unused  # noqa: F401  复用优先级定义

AUDIT = ROOT / "docs" / "_render_audit.json"
TPL = ROOT / "docs" / "_cmd_templates.json"

CJK = re.compile(r"[\u4e00-\u9fff]")
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")

# 首行提取的人工校正：源码里第一条中文串往往是 docstring 或参数名
HEAD_OVERRIDE = {
    "符箓书": "🖌️ 符箓书 · 可绘制符箓",
    "可制符箓": "🖌️ 符箓书 · 可绘制符箓",
    "丹方": "📜 已学丹方列表",
    "丹药配方": "📜 已学丹方列表",
    "符箓配方": "🖌️ 符箓配方总览",
    "我的信息": "=== {道号} 的信息 ===",
    "突破信息": "=== {道号} 的突破信息 ===",
    "仓库": "🏛️ 藏宝阁 Lv.{n}／上限 Lv.{m}",
    "储物戒": "=== {道号} 的储物戒 ===",
    "我的宗门": "🏛️ 宗门信息",
    "宗门列表": "🏛️ 宗门列表",
    "我的装备": "=== {道号} 的装备 ===",
    "我的持仓": "「 我的持仓 」",
    "行情": "「 {名称} · {代码} 」",
    "银行流水": "📋 银行交易流水（最近 N 条）",
    "仙市流水": "📋 仙市成交流水",
    "秘境列表": "🗺️ 秘境列表",
    "历练信息": "📖 历练路线",
    "我的灵田": "🌱 灵田（{n} 格）",
    "我的灵兽": "🐾 你的灵兽（{n}/{slot}）",
    "丹阁": "🏯 丹阁 · {分区}",
    "坊市": "🏪 玩家交易坊市",
    "我的坊市": "🏪 我的坊市",
    "奇遇选择": "✨【奇遇】{标题}",
    "火候": "🔥 选择火候",
    "境界排行": "📊 境界排行榜",
    "战力排行": "⚔️ 战力排行榜",
    "灵石排行": "💰 灵石排行榜",
    "宗门任务": "✨ 你开始执行宗门任务！任务需要1小时完成…",
    "周本加入": "🗡️ 周常副本 · 可加入",
    "世界Boss": "🐉 世界Boss · {名字}",
}


def pick_head(frags: list[dict]) -> str:
    """从模板片段里挑一行最像「回复首行」的中文串。"""
    cands: list[str] = []
    for f in frags:
        s = f["src"]
        s = s.replace("\\n", "\n").split("\n")[0].strip()
        if len(s) < 5 or not CJK.search(s):
            continue
        if re.match(r"^[a-z_]+$", s):
            continue
        cands.append(s)
    if not cands:
        return "（无固定文案，结果由管理器拼接）"
    for c in cands:
        if EMOJI.match(c) or c.startswith(("【", "「", "=", "━")):
            return c[:34]
    return max(cands[:8], key=len)[:34]


def main() -> None:
    rows = json.loads(AUDIT.read_text(encoding="utf-8"))
    tpl = json.loads(TPL.read_text(encoding="utf-8"))
    out: list[str] = []
    for r in sorted(rows, key=lambda x: ((x["group"] or "—"), x["cmd"])):
        cmd = r["cmd"]
        head = HEAD_OVERRIDE.get(cmd)
        if not head:
            head = pick_head((tpl.get(cmd) or {}).get("frags") or [])
        lines = r["text"]["lines"]
        out.append(f"| `/@{cmd}` | {head} | {lines} |")
    if "--probe" in sys.argv:
        print("\n".join(out[:400]))
    else:
        print("\n".join(out))


if __name__ == "__main__":
    main()
