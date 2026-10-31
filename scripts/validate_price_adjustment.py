"""校验 docs/price_adjustment 下的价格表；不改游戏配置。"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "price_adjustment"


def rows(name: str):
    return list(csv.DictReader((DATA / name).open(encoding="utf-8-sig")))


def main() -> int:
    errors = []
    materials = rows("materials.csv")
    if len(materials) != 174:
        errors.append(f"材料数量 {len(materials)} != 174")
    if any(int(r["new_price"]) <= 0 for r in materials):
        errors.append("存在非正数材料价格")

    packages = rows("packages.csv")
    if len(packages) != 16:
        errors.append(f"材料包数量 {len(packages)} != 16")
    for row in packages:
        ratio = float(row["expected_over_price"])
        if not 0.75 <= ratio <= 0.90:
            errors.append(f"{row['name']} 期望/售价 {ratio:.3f} 超出 0.75～0.90")

    recipes = rows("recipes.csv")
    if len(recipes) != 42:
        errors.append(f"丹方数量 {len(recipes)} != 42")
    for row in recipes:
        expected = float(row["expected_cost"])
        price = float(row["new_pill_price"])
        if price < expected:
            errors.append(f"{row['output_pill']} 新价低于期望成本")
        if expected > 0 and price / expected > 1.45:
            errors.append(f"{row['output_pill']} 毛利率过高：{price / expected:.2f}x")
        if "NoneNone" in row["materials"] or "灵草" in row["materials"]:
            errors.append(f"{row['name']} 仍有无效材料字段")
        if float(row["scroll_price"]) <= 0:
            errors.append(f"{row['name']} 丹方卷轴价非正")

    pills = rows("pills.csv")
    names = [r["name"] for r in pills]
    if len(names) != len(set(names)):
        errors.append("pills.csv 存在重名")
    if any(int(r["new_price"]) < 0 for r in pills):
        errors.append("存在负丹药价格")

    exceptions = rows("equipment_exceptions.csv")
    for row in exceptions:
        if int(row["proposed_price"]) <= 0:
            errors.append(f"{row['name']} 异常装备价格非正")

    print(
        f"materials={len(materials)} packages={len(packages)} "
        f"recipes={len(recipes)} pills={len(pills)} exceptions={len(exceptions)}"
    )
    if errors:
        for error in errors[:30]:
            print("ERROR:", error)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
