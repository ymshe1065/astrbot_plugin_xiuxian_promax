#!/usr/bin/env python3
"""生成 120 件特殊手套/戒指配置。

用法：python3 scripts/generate_special_equipment.py
输出：config/special_equipment.json
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ITEMS_FILE = ROOT / "config" / "items.json"
OUTPUT_FILE = ROOT / "config" / "special_equipment.json"

RANKS = [
    ("凡品", 0), ("黄品", 3), ("玄品", 9), ("灵品", 10), ("地品", 12), ("天品", 13),
    ("皇品", 16), ("圣品", 18), ("帝品", 22), ("道品", 28), ("仙品", 32), ("混元先天", 35),
]

GLOVES = [
    ("kuai", "快击", [5, 6, 7, 8, 9, 10, 12, 13, 14, 16, 18, 20],
     [20, 22, 24, 27, 30, 33, 37, 40, 44, 50, 58, 70]),
    ("pojia", "破甲", [3, 4, 5, 6, 7, 8, 10, 12, 14, 17, 20, 25],
     [0, 0, 0, 0, 0, 0, 5, 6, 7, 8, 9, 12]),
    ("shixue", "噬血", [2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 17],
     [0] * 12),
    ("jinglei", "惊雷", [2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16],
     [3, 4, 6, 8, 10, 12, 15, 18, 21, 25, 30, 36]),
    ("lianji", "连击", [3, 4, 5, 6, 7, 8, 10, 11, 12, 14, 16, 20],
     [0] * 12),
]

RINGS = [
    ("mabi", "麻痹", [2, 2, 3, 4, 5, 6, 7, 8, 9, 11, 13, 15], [0] * 12),
    ("shaoshao", "灼烧", [8, 9, 10, 12, 13, 14, 16, 18, 20, 23, 26, 30],
     [1.5, 1.8, 2.0, 2.4, 2.7, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0]),
    ("hanshuang", "寒霜", [6, 7, 8, 9, 10, 11, 13, 14, 15, 17, 19, 22],
     [3, 4, 5, 6, 7, 8, 10, 11, 12, 15, 17, 20]),
    ("zuzhou", "诅咒", [6, 7, 8, 9, 10, 11, 13, 14, 15, 17, 19, 22],
     [4, 5, 6, 7, 8, 9, 11, 12, 13, 16, 18, 22]),
    ("shihun", "噬魂", [1, 1, 2, 2, 3, 3, 4, 4, 5, 6, 7, 9],
     [4, 5, 5, 6, 7, 7, 8, 9, 9, 10, 11, 13]),
]

SHOP_WEIGHTS = [60, 56, 52, 48, 44, 40, 34, 30, 26, 20, 16, 12]
# 控制系戒指（麻痹/寒霜）在商店权重上再打八折
CONTROL_RINGS = {"mabi", "hanshuang"}
# 新系价格 = 同品阶灵纹同部位价格 × 0.9


def load_reference_prices() -> dict:
    with open(ITEMS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    values = data.values() if isinstance(data, dict) else data
    prices = {}
    for item in values:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        if "·灵纹手套" in name:
            prices[("gloves", item.get("rank"))] = int(item.get("price") or 0)
        elif "·灵纹戒指" in name:
            prices[("ring", item.get("rank"))] = int(item.get("price") or 0)
    return prices


def special_price(reference: int) -> int:
    return max(100, int(round(reference * 0.9 / 10.0) * 10))


def glove_effects(key: str, values: list, tier: int) -> dict:
    effects = {}
    if key == "kuai":
        effects["extra_attack_chance"] = values[tier]
        effects["extra_attack_ratio"] = GLOVES[0][3][tier]
        if tier >= 3:
            effects["extra_attack_can_crit"] = 1
        if tier >= 6:
            effects["extra_attack_can_proc"] = 1
        if tier >= 9:
            effects["extra_attack_count"] = 2
            effects["extra_attack_second_ratio"] = 70 if tier >= 11 else 50
    elif key == "pojia":
        effects["armor_penetration"] = values[tier]
        if tier >= 6:
            effects["sunder_chance"] = 30
            effects["sunder_pct"] = values[tier]
            effects["sunder_turns"] = 2
    elif key == "shixue":
        effects["lifesteal"] = values[tier]
    elif key == "jinglei":
        effects["crit_rate"] = values[tier]
        effects["crit_damage"] = round(GLOVES[3][3][tier] / 100.0, 3)
        if tier >= 3:
            effects["crit_defense_ignore"] = 10
        if tier >= 6:
            effects["crit_chain_chance"] = 20
            effects["crit_chain_ratio"] = 30
        if tier >= 9:
            effects["crit_chain_can_crit"] = 1
            effects["crit_chain_ratio"] = 50
    elif key == "lianji":
        effects["combo_rate"] = values[tier]
        if tier >= 3:
            effects["combo_can_crit"] = 1
        if tier >= 6:
            effects["combo_lifesteal_pct"] = 10
        if tier >= 9:
            effects["combo_extra_hit_ratio"] = 50
    return effects


def ring_effects(key: str, values: list, tier: int) -> dict:
    effects = {}
    if key == "mabi":
        effects["stun_chance"] = values[tier]
        effects["stun_turns"] = 2 if tier >= 11 else 1
        if tier >= 3:
            effects["stun_resist_pierce"] = 10
        if tier >= 6:
            effects["stun_extend_chance"] = 15
        if tier >= 9:
            effects["stun_resist_pierce"] = 30
        if tier >= 11:
            effects["stun_resist_pierce"] = 50
    elif key == "shaoshao":
        effects["burn_chance"] = values[tier]
        effects["burn_damage_pct"] = RINGS[1][3][tier]
        effects["burn_turns"] = 4 if tier >= 6 else 3
        if tier >= 3:
            effects["burn_heal_reduction"] = 20
        if tier >= 9:
            effects["burn_detonate"] = 1
    elif key == "hanshuang":
        effects["frost_chance"] = values[tier]
        effects["frost_atk_reduction"] = RINGS[2][3][tier]
        effects["frost_turns"] = 2
        if tier >= 3:
            effects["frost_dodge_reduction"] = 3
        if tier >= 6:
            effects["freeze_chance"] = 20
        if tier >= 9:
            effects["freeze_chance"] = 30
        if tier >= 11:
            effects["freeze_chance"] = 40
    elif key == "zuzhou":
        effects["curse_chance"] = values[tier]
        effects["curse_defense_pct"] = RINGS[3][3][tier]
        effects["curse_turns"] = 3 if tier >= 3 else 2
        if tier >= 6:
            effects["curse_damage_reduction"] = 5
        if tier >= 9:
            effects["curse_damage_reduction"] = 8
    elif key == "shihun":
        effects["lifesteal"] = values[tier]
        effects["curse_chance"] = RINGS[4][3][tier]
        effects["curse_defense_pct"] = [
            3, 4, 4, 5, 6, 7, 8, 9, 9, 11, 12, 14,
        ][tier]
        effects["curse_turns"] = 3 if tier >= 9 else 2
        if tier >= 6:
            effects["curse_damage_reduction"] = 5
    return effects


def describe(slot: str, name: str, effects: dict) -> str:
    if slot == "gloves":
        if "extra_attack_chance" in effects:
            text = f"攻击时有 {effects['extra_attack_chance']:g}% 概率追加一次 {effects['extra_attack_ratio']:g}% 伤害的攻击"
            if effects.get("extra_attack_count", 1) >= 2:
                text += f"，共 {effects['extra_attack_count']} 段"
            if effects.get("extra_attack_can_crit"):
                text += "，追加攻击可暴击"
            return text + "。"
        if "armor_penetration" in effects:
            text = f"攻击时无视目标 {effects['armor_penetration']:g}% 防御"
            if effects.get("sunder_chance"):
                text += f"，并有 {effects['sunder_chance']:g}% 概率附加破防（防御 -{effects['sunder_pct']:g}%，{effects['sunder_turns']} 回合）"
            return text + "。"
        if "lifesteal" in effects:
            return f"攻击时吸取 {effects['lifesteal']:g}% 伤害转化为自身气血。"
        if "crit_rate" in effects:
            text = f"暴击率 +{effects['crit_rate']:g}%，暴击伤害 +{effects['crit_damage'] * 100:g}%"
            if effects.get("crit_chain_chance"):
                text += f"，暴击后 {effects['crit_chain_chance']:g}% 概率追加 {effects['crit_chain_ratio']:g}% 雷击"
            return text + "。"
        if "combo_rate" in effects:
            text = f"连击率 +{effects['combo_rate']:g}%"
            if effects.get("combo_extra_hit_ratio"):
                text += f"，触发连击时追加一次 {effects['combo_extra_hit_ratio']:g}% 二段"
            return text + "。"
    if "stun_chance" in effects:
        text = f"命中时 {effects['stun_chance']:g}% 概率使目标麻痹 {effects['stun_turns']} 回合"
        if effects.get("stun_extend_chance"):
            text += f"（{effects['stun_extend_chance']:g}% 概率延长）"
        return text + "。"
    if "burn_chance" in effects:
        text = (f"命中时 {effects['burn_chance']:g}% 概率灼烧目标，"
                f"每回合损失 {effects['burn_damage_pct']:g}% 最大生命，持续 {effects['burn_turns']} 回合")
        if effects.get("burn_detonate"):
            text += "，刷新时爆燃剩余伤害"
        return text + "。"
    if "frost_chance" in effects:
        text = (f"命中时 {effects['frost_chance']:g}% 概率使目标寒霜缠身，"
                f"攻击 -{effects['frost_atk_reduction']:g}%，持续 {effects['frost_turns']} 回合")
        if effects.get("freeze_chance"):
            text += f"，并有 {effects['freeze_chance']:g}% 概率冻结 1 回合"
        return text + "。"
    if "curse_chance" in effects:
        text = (f"命中时 {effects['curse_chance']:g}% 概率诅咒目标，"
                f"防御 -{effects['curse_defense_pct']:g}%，持续 {effects['curse_turns']} 回合")
        if effects.get("curse_damage_reduction"):
            text += f"，伤害 -{effects['curse_damage_reduction']:g}%"
        if effects.get("lifesteal"):
            text = f"攻击时吸取 {effects['lifesteal']:g}% 伤害转化为气血；" + text
        return text + "。"
    return "特殊装备。"


def main() -> None:
    prices = load_reference_prices()
    result = {}
    for slot, groups, prefix in (("gloves", GLOVES, "sp_gloves"), ("ring", RINGS, "sp_ring")):
        cn_slot = "手套" if slot == "gloves" else "戒指"
        factory = glove_effects if slot == "gloves" else ring_effects
        for key, label, values, extras in groups:
            for tier, (rank, level) in enumerate(RANKS):
                effects = factory(key, values, tier)
                name = f"{rank}·{label}{cn_slot}"
                reference = prices.get((slot, rank), 0)
                shop_weight = SHOP_WEIGHTS[tier]
                if slot == "ring" and key in CONTROL_RINGS:
                    shop_weight = max(1, int(shop_weight * 0.8))
                drop_weight = 70 if (slot == "ring" and key in CONTROL_RINGS) else (90 if slot == "gloves" else 80)
                result[name] = {
                    "id": f"{prefix}_{key}_{tier + 1:02d}",
                    "name": name,
                    "type": slot,
                    "subtype": cn_slot,
                    "equip_slot": slot,
                    "rank": rank,
                    "required_level_index": level,
                    "description": describe(slot, name, effects),
                    "price": special_price(reference),
                    "shop_weight": shop_weight,
                    "drop_weight": drop_weight,
                    "volume": 5,
                    "balance_version": 2,
                    "effect_quality_scaling": True,
                    "equipment_rates": {},
                    "effects": effects,
                    "source_tags": ["shop", "treasure", "rift", "adventure", "auction", "black_market"],
                }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已生成 {len(result)} 件特殊装备 -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
