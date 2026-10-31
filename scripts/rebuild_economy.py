"""经济重建（幂等）：按基准表/材料锚点重算材料价、材料包价、全部丹方、丹药价、符箓价。

执行顺序：
1. material_catalog.json 按 类别基准×品阶倍率 重定价（特殊保留原价）；
2. items.json 同步材料/符箓/丹药价格；
3. pill_recipes.json 为全部丹药补齐丹方，并按期望成本×1.5 重算成品价与卷轴价；
4. pills/exp_pills/utility_pills 同步成品价；
5. talisman_recipes.json 按期望成本×1.35 重算符箓价。

多次运行结果一致。改完押镖常量后跑 scripts/generate_economy_baseline.py，改完价格后跑本脚本。
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"

MATERIAL_BASE = {"灵植": 60, "矿材": 72, "妖兽": 90, "灵液": 120}
TIER_MULT = {"凡品": 1, "黄品": 2.5, "玄品": 6, "灵品": 12, "地品": 25, "天品": 50,
             "皇品": 100, "圣品": 200, "帝品": 500, "道品": 1500, "仙品": 4000, "混元先天": 10000}
RANK_LEVEL = {"凡品": 0, "黄品": 3, "玄品": 9, "灵品": 10, "地品": 12, "天品": 13,
              "皇品": 16, "圣品": 18, "帝品": 22, "道品": 28, "仙品": 32, "混元先天": 35}
RANK_MATERIAL_TIER = {"凡品": "凡品", "黄品": "黄品", "玄品": "玄品", "灵品": "玄品",
                      "地品": "玄品", "天品": "圣品", "皇品": "圣品", "圣品": "圣品",
                      "帝品": "圣品", "道品": "圣品", "仙品": "圣品", "混元先天": "圣品"}
RANK_COUNT = {"凡品": 2, "黄品": 3, "玄品": 4, "灵品": 4, "地品": 5, "天品": 5,
              "皇品": 6, "圣品": 6, "帝品": 7, "道品": 8, "仙品": 9, "混元先天": 10}
RANK_GOLD = {"凡品": 100, "黄品": 300, "玄品": 1000, "灵品": 1500, "地品": 2000, "天品": 3000,
             "皇品": 5000, "圣品": 8000, "帝品": 20000, "道品": 50000, "仙品": 100000, "混元先天": 200000}
RANK_SUCCESS = {"凡品": 85, "黄品": 75, "玄品": 65, "灵品": 63, "地品": 62, "天品": 61,
                "皇品": 60, "圣品": 60, "帝品": 55, "道品": 52, "仙品": 50, "混元先天": 55}


def load(name: str):
    return json.loads((CONFIG / name).read_text(encoding="utf-8"))


INDENT = {"material_catalog.json": 2, "items.json": 4, "pill_recipes.json": 4, "pills.json": 2,
          "exp_pills.json": 4, "utility_pills.json": 4, "talisman_recipes.json": 2}


def dump(name: str, data):
    (CONFIG / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=INDENT.get(name, 2)), encoding="utf-8"
    )


def ceil_to_nice(value: float) -> int:
    if value < 1_000:
        step = 10
    elif value < 10_000:
        step = 100
    elif value < 1_000_000:
        step = 1_000
    elif value < 100_000_000:
        step = 10_000
    else:
        step = 100_000
    return max(step, int(math.ceil(value / step) * step))


def median_groups(catalog: dict) -> dict:
    groups = {}
    for material in catalog.get("materials", []):
        key = (material.get("category"), material.get("tier"))
        groups.setdefault(key, []).append(int(material.get("price") or 0))
    return {key: int(statistics.median(values)) for key, values in groups.items()}


def expected_cost(recipe: dict, medians: dict, by_name: dict) -> float:
    cost = 0.0
    materials = recipe.get("materials")
    specs = [{"name": k, "count": v} for k, v in materials.items()] if isinstance(materials, dict) else (materials or [])
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        count = int(spec.get("count", 0) or 0)
        if spec.get("name"):
            price = by_name.get(spec.get("name"))
            if price is None:
                raise RuntimeError(f"具名材料无价格：{spec.get('name')}")
        else:
            price = medians.get((spec.get("category"), spec.get("tier")))
            if price is None:
                raise RuntimeError(f"无材料价格：{spec}")
        cost += price * count
    cost += int(recipe.get("gold_cost") or 0)
    success = float(recipe.get("success_rate") or 50) / 100.0
    if success <= 0:
        success = 0.5
    tribulation = 0.75 if int(recipe.get("level_required") or 0) >= 6 else 1.0
    return cost / (success * tribulation)



# ===== 旧式丹药效果定价锚点（方案A）=====
ESCORT_GOLD_BASE = 9000
ESCORT_REALM_MULT = {3: 1.0, 10: 1.6, 13: 2.5, 16: 4.0, 19: 6.5, 22: 10.0,
                     25: 16.0, 28: 25.0, 31: 36.0, 34: 44.0, 35: 45.0}
EFFECT_PRICE_DISCOUNT = 0.25


def escort_trip_gold(level: int) -> int:
    points = sorted(ESCORT_REALM_MULT.items())
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
                mult = m1 + (m2 - m1) * (level - l1) / (l2 - l1)
                break
    return int(round(ESCORT_GOLD_BASE * mult / 500.0)) * 500


# L0~L2 押镖未开放，用历练过渡日收益（见《经济基准表》）
TRANSITION_DAILY = {0: 324, 1: 399, 2: 504}


def value_per_exp(level: int) -> float:
    """1 点修为的机会成本 = 押镖日收益 ÷ (每日闭关修为)。闭关 100 修为/分钟 × (1+0.1L)。"""
    if level < 3:
        daily = TRANSITION_DAILY.get(level, 324)
    else:
        daily = escort_trip_gold(level) * 48
    return daily / (1440.0 * 100.0 * (1 + 0.1 * level))


def effect_anchor_price(cfg: dict, rank: str, level_exp: list) -> int:
    """旧式丹药按效果定价：修为/气血/灵气按闭关时间价值，恢复类按境界资源池比例。"""
    eff = cfg.get("effect") or {}
    if not isinstance(eff, dict) or not eff:
        return 0
    level = RANK_LEVEL.get(rank, 0)
    ve = value_per_exp(level)
    discount = EFFECT_PRICE_DISCOUNT

    def nice(value: float) -> int:
        return max(10, int(round(value / 10.0) * 10))

    if "add_experience" in eff:
        amount = max(0, int(eff.get("add_experience") or 0))
        return nice(amount * ve * discount)
    if "add_hp" in eff:
        hp = max(0, int(eff.get("add_hp") or 0))
        return nice(hp * 2 * ve * discount)  # 气血 = 修为/2
    if "add_mp" in eff or "add_spiritual_qi" in eff:
        mp = max(0, int(eff.get("add_mp") or eff.get("add_spiritual_qi") or 0))
        return nice(mp * ve * discount)      # 灵气 = 修为
    pool_exp = 1000
    if 0 <= level < len(level_exp) and isinstance(level_exp[level], dict):
        pool_exp = max(1000, int(level_exp[level].get("exp_needed") or 0))
    if "restore_hp_percent" in eff:
        pct = max(0.0, float(eff.get("restore_hp_percent") or 0))
        amount = pct * pool_exp / 2.0
        return nice(amount * 2 * ve * discount)
    if "restore_qi_percent" in eff or "restore_mp_percent" in eff:
        pct = max(0.0, float(eff.get("restore_qi_percent") or eff.get("restore_mp_percent") or 0))
        amount = pct * pool_exp
        return nice(amount * ve * discount)
    return 0

def rebuild_materials():
    catalog = load("material_catalog.json")
    for material in catalog["materials"]:
        if material.get("category") == "特殊":
            continue
        base = MATERIAL_BASE.get(material.get("category"), 60)
        mult = TIER_MULT.get(material.get("tier"), 1)
        material["price"] = max(10, int(round(base * mult / 10.0) * 10))
    dump("material_catalog.json", catalog)
    return catalog


# 方案A：旧“灵草”丹方转 类别+品阶（地→玄品、天→圣品），数量/灵石按当前材料价平衡
_LEGACY_GRASS = {130: ("凡品", 1, 100), 131: ("凡品", 1, 100), 132: ("黄品", 1, 100),
                 133: ("黄品", 1, 100), 134: ("黄品", 2, 500), 135: ("黄品", 2, 500),
                 136: ("玄品", 1, 500), 137: ("玄品", 1, 500), 138: ("圣品", 1, 2000),
                 139: ("圣品", 1, 2000)}


def rebuild_recipes_and_pills(catalog: dict) -> int:
    recipes = load("pill_recipes.json")
    for recipe in recipes:
        rid = int(recipe.get("id") or 0)
        if rid in _LEGACY_GRASS and isinstance(recipe.get("materials"), dict):
            tier, count, gold = _LEGACY_GRASS[rid]
            recipe["materials"] = [{"category": "灵植", "tier": tier, "count": count}]
            recipe["gold_cost"] = gold
    pills = load("pills.json")
    exp = load("exp_pills.json")
    util = load("utility_pills.json")
    items = load("items.json")

    by_output = {r.get("output_pill"): r for r in recipes}
    medians = median_groups(catalog)
    by_name = {m.get("name"): int(m.get("price") or 0) for m in catalog.get("materials", [])}

    def rank_of(pill: dict) -> str:
        return pill.get("rank") or "凡品"

    # 补齐缺配方的丹药
    next_id = max([int(r.get("id") or 0) for r in recipes] + [199]) + 1
    for pill_list, attr in ((pills, "breakthrough"), (exp, "exp"), (util, "utility")):
        for pill in pill_list:
            name = pill.get("name")
            if not name or name in by_output:
                continue
            rank = rank_of(pill)
            tier = RANK_MATERIAL_TIER.get(rank, "圣品")
            count = RANK_COUNT.get(rank, 2)
            subtype = pill.get("subtype") or ""
            restore = ("restore_hp_percent" in pill) or ("restore_qi_percent" in pill)
            if attr == "exp":
                mats = [{"category": "灵植", "tier": tier, "count": count}]
            elif restore:
                mats = [{"category": "灵植", "tier": tier, "count": count},
                        {"category": "灵液", "tier": tier, "count": max(1, count // 2)}]
            else:
                mats = [{"category": "灵植", "tier": tier, "count": count},
                        {"category": "矿材", "tier": tier, "count": max(1, count // 2)}]
            recipes.append({
                "id": next_id,
                "name": f"{name}方",
                "output_pill": name,
                "level_required": int(pill.get("required_level_index") or RANK_LEVEL.get(rank, 0)),
                "materials": mats,
                "gold_cost": RANK_GOLD.get(rank, 8000),
                "success_rate": RANK_SUCCESS.get(rank, 60),
                "price": 2000,
                "shop_weight": 200,
                "desc": f"记载{name}炼制方法的丹方，学习后可用 /炼丹 炼制。",
            })
            by_output[name] = recipes[-1]
            next_id += 1


    # items.json 旧式丹药：按效果锚点定价并补丹方（价格 = max(效果价, 炼制成本×1.5)）
    level_exp = load("level_config.json")
    for _key, cfg in items.items():
        if not isinstance(cfg, dict) or cfg.get("type") != "丹药":
            continue
        name = cfg.get("name") or ""
        if not name or name in by_output:
            continue
        rank = rank_of(cfg)
        tier = RANK_MATERIAL_TIER.get(rank, "圣品")
        recipes.append({
            "id": next_id,
            "name": f"{name}方",
            "output_pill": name,
            "level_required": int(RANK_LEVEL.get(rank, 0)),
            "materials": [{"category": "灵植", "tier": tier, "count": 1}],
            "gold_cost": max(50, RANK_GOLD.get(rank, 100) // 20),
            "success_rate": RANK_SUCCESS.get(rank, 60),
            "price": 2000,
            "shop_weight": 50,
            "desc": f"记载{name}炼制方法的丹方，学习后可用 /炼丹 炼制。",
        })
        by_output[name] = recipes[-1]
        next_id += 1

    # 重算成品价与卷轴价
    price_by_output = {}
    pill_cfgs = {}
    for pill in pills + exp + util:
        pill_cfgs[pill.get("name")] = pill
    for _key, cfg in items.items():
        if isinstance(cfg, dict) and cfg.get("type") == "丹药" and cfg.get("name"):
            pill_cfgs.setdefault(cfg.get("name"), cfg)
    for recipe in recipes:
        cost_target = ceil_to_nice(expected_cost(recipe, medians, by_name) * 1.5)
        output = recipe["output_pill"]
        cfg = pill_cfgs.get(output) or {}
        effect_price = effect_anchor_price(cfg, cfg.get("rank") or "凡品", level_exp) if cfg else 0
        price_by_output[output] = max(cost_target, effect_price)

    # 突破丹方卷轴价按境界严格递增，避免重算后并列
    breakthrough = sorted(
        (r for r in recipes if 101 <= int(r.get("id") or 0) <= 126),
        key=lambda r: int(r.get("level_required") or 0),
    )
    previous = 0
    for recipe in breakthrough:
        scroll = max(50, int(round(price_by_output[recipe["output_pill"]] * 0.10 / 10.0) * 10))
        if scroll <= previous:
            scroll = previous + 10
        recipe["price"] = scroll
        previous = scroll
    for recipe in recipes:
        if int(recipe.get("id") or 0) in range(101, 127):
            continue
        scroll = max(50, int(round(price_by_output[recipe["output_pill"]] * 0.10 / 10.0) * 10))
        recipe["price"] = scroll

    # 同步成品价
    for pill in pills + exp + util:
        if pill.get("name") in price_by_output:
            pill["price"] = price_by_output[pill["name"]]
    for _, item in items.items():
        if isinstance(item, dict) and item.get("type") == "丹药" and item.get("name") in price_by_output:
            item["price"] = price_by_output[item["name"]]
        if isinstance(item, dict) and item.get("type") == "材料":
            material = next((m for m in catalog["materials"] if m["name"] == item.get("name")), None)
            if material:
                item["price"] = material["price"]

    dump("pill_recipes.json", recipes)
    dump("pills.json", pills)
    dump("exp_pills.json", exp)
    dump("utility_pills.json", util)
    dump("items.json", items)
    return len(recipes)


def rebuild_talismans(catalog: dict):
    tal = load("talisman_recipes.json")
    items = load("items.json")
    medians = median_groups(catalog)
    prices = {}
    for recipe in tal:
        cost = sum(
            (medians.get((s.get("category"), s.get("tier")), 0) or 0) * int(s.get("count", 0) or 0)
            for s in (recipe.get("materials") or []) if isinstance(s, dict)
        ) + int(recipe.get("gold_cost") or 0)
        success = float(recipe.get("success_rate") or 60) / 100.0
        if success <= 0:
            success = 0.6
        prices[recipe.get("name")] = ceil_to_nice(cost / success * 1.35)
    for recipe in tal:
        recipe["price"] = prices[recipe.get("name")]
    for _, item in items.items():
        if isinstance(item, dict) and item.get("type") == "符箓" and item.get("name") in prices:
            item["price"] = prices[item["name"]]
    dump("talisman_recipes.json", tal)
    dump("items.json", items)
    return len(tal)


def main() -> int:
    catalog = rebuild_materials()
    recipe_count = rebuild_recipes_and_pills(catalog)
    talisman_count = rebuild_talismans(catalog)
    print(f"材料 174 条重定价；丹方 {recipe_count} 条；符箓 {talisman_count} 条；")
    print("提示：修改材料锚点后重新运行本脚本即可幂等重算。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
