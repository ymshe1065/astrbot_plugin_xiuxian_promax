"""装备价格全量异常检测。

检查项：
- 价格非正/缺失；
- 同部位按品阶单调（价格不得随品阶上升而下降）；
- 价格/属性预算比偏离该部位中位数的倍数过大；
- 显式境界门槛与品阶锚点差超过 2 级。

结果写入 docs/price_adjustment/equipment_anomalies.csv。
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
OUT = ROOT / "docs" / "price_adjustment" / "equipment_anomalies.csv"

RANK_ORDER = ["凡品", "黄品", "玄品", "灵品", "地品", "天品", "皇品", "圣品", "帝品", "道品", "仙品", "混元先天"]
RANK_LEVEL = {"凡品": 0, "黄品": 3, "玄品": 9, "灵品": 10, "地品": 12, "天品": 13,
              "皇品": 16, "圣品": 18, "帝品": 22, "道品": 28, "仙品": 32, "混元先天": 35}
SLOT_MAP = {"武器": "weapon", "防具": "body", "甲胄": "body", "头饰": "head", "手套": "gloves",
            "靴子": "boots", "戒指": "ring1", "护符": "amulet", "饰品": "amulet"}
SLOT_BUDGET = {"weapon": (0.18, 0.0), "head": (0.0, 0.06), "body": (0.0, 0.12),
               "gloves": (0.06, 0.0), "boots": (0.0, 0.04), "ring1": (0.04, 0.0), "amulet": (0.02, 0.04)}
PROFILES = {"physical": (0.8, 0.2, 0.75, 0.25), "magic": (0.2, 0.8, 0.25, 0.75),
            "hybrid": (0.5, 0.5, 0.5, 0.5), "guardian": (0.4, 0.6, 0.55, 0.45)}


def load(name: str):
    data = json.loads((CONFIG / name).read_text(encoding="utf-8"))
    return list(data.values()) if isinstance(data, dict) else data


def slot_of(item: dict) -> str:
    if item.get("equip_slot"):
        return item["equip_slot"]
    subtype = str(item.get("subtype") or "")
    return SLOT_MAP.get(subtype, SLOT_MAP.get(str(item.get("type") or ""), ""))


def stat_budget(item: dict) -> float:
    slot = slot_of(item)
    if slot not in SLOT_BUDGET:
        return 0.0
    attack, defense = SLOT_BUDGET[slot]
    if item.get("profile"):
        pa, ma, pd, md = PROFILES.get(item["profile"], (0.5, 0.5, 0.5, 0.5))
        rates = (attack * pa, attack * ma, defense * pd, defense * md)
    elif isinstance(item.get("equipment_rates"), dict):
        rates = item["equipment_rates"]
        rates = (rates.get("physical_damage", 0), rates.get("magic_damage", 0),
                 rates.get("physical_defense", 0), rates.get("magic_defense", 0))
    else:
        rates = (attack, attack, defense, defense)
    return sum(float(x or 0) for x in rates)


def main() -> int:
    rows = []
    all_items = []
    for file in ("items.json", "weapons.json", "special_equipment.json", "items_v2.json"):
        for item in load(file):
            if isinstance(item, dict):
                item = dict(item)
                item["_source"] = file
                all_items.append(item)

    gear = [x for x in all_items if slot_of(x) in SLOT_BUDGET]
    by_slot = {}
    for item in gear:
        by_slot.setdefault(slot_of(item), []).append(item)

    for slot, items in by_slot.items():
        prices = {}
        budgets = []
        for item in items:
            try:
                price = int(item.get("price") or 0)
            except (TypeError, ValueError):
                price = 0
            name = item.get("name")
            rank = item.get("rank")
            if price <= 0:
                rows.append([slot, name, rank, price, "非正价格"])
                continue
            prices[(rank, name)] = price
            budget = stat_budget(item)
            if budget > 0:
                budgets.append((price / budget, name, rank))
            level = item.get("required_level_index")
            if isinstance(level, int) and rank in RANK_LEVEL and abs(level - RANK_LEVEL[rank]) > 2:
                rows.append([slot, name, rank, price, f"门槛与品阶锚点差 {level - RANK_LEVEL[rank]} 级"])
        # 同部位品阶单调
        by_rank = {}
        for (rank, name), price in prices.items():
            by_rank.setdefault(rank, []).append((price, name))
        prev_max = None
        prev_rank = None
        for rank in RANK_ORDER:
            if rank not in by_rank:
                continue
            current = by_rank[rank]
            if prev_max is not None:
                for price, name in current:
                    if price < prev_max:
                        rows.append([slot, name, rank, price, f"价格低于 {prev_rank} 最高价 {prev_max}"])
            prev_max = max(p for p, _ in current)
            prev_rank = rank
        # 价格/预算比离群
        if budgets:
            ratios = [r for r, _, _ in budgets]
            median = statistics.median(ratios)
            for ratio, name, rank in budgets:
                if median > 0 and ratio > median * 6:
                    rows.append([slot, name, rank, round(ratio), f"价格/预算比 {ratio/median:.1f}× 部位中位数"])

    rows.sort(key=lambda x: (x[0], str(x[2]), str(x[1])))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["slot", "name", "rank", "price", "reason"])
        writer.writerows(rows)
    print(f"检测装备 {len(gear)} 件，异常 {len(rows)} 条 -> {OUT}")
    for row in rows[:40]:
        print(" ", row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
