"""生成/校验 docs/经济基准表.md。

基准：24h 押镖名义镖银（48 趟/天、自动代理全勤、不扣劫镖期望）。
L0~L2 无押镖，用历练最优路线金/时过渡。

用法：
    python scripts/generate_economy_baseline.py          # 重写经济基准表
    python scripts/generate_economy_baseline.py --check  # 仅校验表与常量是否同步
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ESCORT_FILE = ROOT / "managers" / "escort_manager.py"
ADVENTURE_FILE = ROOT / "config" / "adventure_config.json"
DOC_FILE = ROOT / "docs" / "经济基准表.md"

MARK_BEGIN = "<!-- ECONOMY_BASELINE_AUTO_BEGIN -->"
MARK_END = "<!-- ECONOMY_BASELINE_AUTO_END -->"

TRIPS_PER_DAY = 48
ESCORT_MIN_LEVEL = 3
MAX_LEVEL = 35


def _read_constant(name: str):
    tree = ast.parse(ESCORT_FILE.read_text(encoding="utf-8"))
    for top in tree.body:
        bodies = [top.body] if isinstance(top, ast.ClassDef) else []
        for body in bodies:
            for node in body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == name:
                            return ast.literal_eval(node.value)
    raise RuntimeError(f"未在 escort_manager.py 中找到常量 {name}")


def _escort_gold(level: int, base: int, multipliers: dict) -> int:
    points = sorted(multipliers.items())
    low_level, low_mult = points[0]
    high_level, high_mult = points[-1]
    if level <= low_level:
        mult = low_mult
    elif level >= high_level:
        mult = high_mult
    else:
        mult = low_mult
        for (l1, m1), (l2, m2) in zip(points, points[1:]):
            if l1 <= level <= l2:
                span = max(1, l2 - l1)
                mult = m1 + (m2 - m1) * (level - l1) / span
                break
    return int(round(base * mult / 500.0)) * 500


def _adventure_gold_per_day(level: int) -> int:
    """L0~L2 过渡：该境界可选的历练路线中金/时最高者 × 24。"""
    adv = json.loads(ADVENTURE_FILE.read_text(encoding="utf-8"))
    routes = [r for r in adv.get("routes", []) if isinstance(r, dict)]
    best = 0.0
    for route in routes:
        if int(route.get("min_level", 0) or 0) > level:
            continue
        base_gold = float(route.get("base_gold_per_min") or 0)
        if base_gold <= 0:
            continue
        reward_duration = float(route.get("reward_duration") or route.get("duration") or 1)
        duration = float(route.get("duration") or reward_duration)
        cooldown_min = float(route.get("fatigue_cooldown") or 0) / 60.0
        per_run = (reward_duration / 60.0) * base_gold
        per_run += level * float(route.get("level_bonus_gold") or 0)
        per_run += float((route.get("completion_bonus") or {}).get("gold", 0))
        cycle = duration + cooldown_min
        gold_per_hour = per_run / cycle * 60.0
        best = max(best, gold_per_hour)
    return int(round(best * 24))


def build_doc() -> str:
    base = int(_read_constant("ESCORT_GOLD_BASE"))
    multipliers = dict(_read_constant("ESCORT_REALM_MULTIPLIERS"))
    lines = []
    lines.append("| 境界等级 | 大境界 | 倍率 | 每趟镖银 | 24h 名义镖银（48 趟） |")
    lines.append("| ---: | --- | ---: | ---: | ---: |")
    for level in range(0, ESCORT_MIN_LEVEL):
        lines.append(f"| {level} | 历练过渡 | — | — | {_adventure_gold_per_day(level):,} |")
    realm_anchor = {3: "炼气", 10: "筑基", 13: "金丹", 16: "元婴", 19: "化神",
                    22: "炼虚", 25: "合体", 28: "大乘", 31: "渡劫", 34: "地仙", 35: "混元先天"}
    for level in range(ESCORT_MIN_LEVEL, MAX_LEVEL + 1):
        mult = _escort_gold(level, base, multipliers) / base if base else 1.0
        trip = _escort_gold(level, base, multipliers)
        realm = realm_anchor.get(level, "")
        lines.append(f"| {level} | {realm} | {mult:.2f} | {trip:,} | {trip * TRIPS_PER_DAY:,} |")
    return "\n".join(lines)


def main() -> int:
    check = "--check" in sys.argv
    table = build_doc()
    auto_block = f"{MARK_BEGIN}\n{table}\n{MARK_END}"
    if DOC_FILE.exists():
        current = DOC_FILE.read_text(encoding="utf-8")
        if check:
            ok = auto_block in current
            print("经济基准表与常量一致" if ok else "经济基准表已过期，请重新生成")
            return 0 if ok else 1
        if MARK_BEGIN in current and MARK_END in current:
            start = current.index(MARK_BEGIN)
            end = current.index(MARK_END) + len(MARK_END)
            new = current[:start] + auto_block + current[end:]
        else:
            new = current + "\n" + auto_block + "\n"
    else:
        new = (
            "# 经济基准表\n\n"
            f"{auto_block}\n"
        )
    DOC_FILE.write_text(new, encoding="utf-8")
    print("已生成", DOC_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
