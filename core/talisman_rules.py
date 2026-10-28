"""符箓品阶、分类与效果推演规则。"""

from __future__ import annotations

from typing import Dict

TIER_POWER = {
    "凡品": 0.15, "黄阶": 0.25, "灵品": 0.3, "玄阶": 0.4,
    "珍品": 0.45, "地阶": 0.55, "圣品": 0.6, "天阶": 0.8,
    "帝品": 0.85, "道品": 0.9, "仙品": 1.0, "神品": 1.1, "混元先天": 1.2,
}
TIER_LEVEL = {
    "凡品": 0, "黄阶": 0, "灵品": 0, "玄阶": 3, "珍品": 3,
    "地阶": 9, "圣品": 9, "天阶": 18, "帝品": 18, "道品": 24,
    "仙品": 30, "神品": 36, "混元先天": 42,
}
TIER_GOLD = {
    "凡品": 200, "黄阶": 300, "灵品": 500, "玄阶": 900,
    "珍品": 1500, "地阶": 3000, "圣品": 5000, "天阶": 12000,
    "帝品": 16000, "道品": 22000, "仙品": 50000, "神品": 80000, "混元先天": 120000,
}
TIER_PRICE = {
    "凡品": 500, "黄阶": 800, "灵品": 1500, "玄阶": 2600,
    "珍品": 4000, "地阶": 9000, "圣品": 14000, "天阶": 35000,
    "帝品": 45000, "道品": 60000, "仙品": 120000, "神品": 200000, "混元先天": 300000,
}

CATEGORY_KEYWORDS = [
    ("炼丹", ("引雷", "避劫", "稳魂", "聚灵")),
    ("探索", ("寻宝", "遁地", "星辰")),
    ("回复", ("回春",)),
    ("攻击", ("焚天", "紫电", "诛仙", "破军", "噬魂", "青锋", "九天", "烈焰", "雷霆", "雷", "火")),
    ("防御", ("山河", "长生", "太极", "八卦", "两仪", "四象", "乾坤", "混元", "无极", "太虚", "金刚")),
    ("控制", ("寒霜", "幽冥", "九幽", "寂灭", "混沌", "冰", "霜")),
    ("增益", ("鸿蒙", "造化", "万古")),
]

NON_TALISMAN_ITEMS = {"化身傀儡"}


def infer_category(name: str, recipe_category: str = "") -> str:
    if recipe_category:
        return recipe_category
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in name for keyword in keywords):
            return category
    return "增益"


def build_profile(name: str, rank: str, category: str = "") -> Dict:
    """根据符名、品级与分类推演符箓效果。"""
    power = float(TIER_POWER.get(rank, 0.3))
    category = category or infer_category(name)
    is_array = "阵" in str(name)
    if is_array:
        power *= 1.2
    duration = 30 if is_array else 15
    effects: Dict[str, float] = {}
    desc_parts = []

    if category == "攻击":
        mult = min(0.50, round(0.15 + power * 0.5, 2))
        effects["physical_damage_multiplier"] = mult
        effects["magic_damage_multiplier"] = mult
        effects["crit_rate_bonus"] = int(8 + power * 15)
        desc_parts.append(f"物伤/法伤+{mult:.0%}，暴击+{effects['crit_rate_bonus']}%")
    elif category == "防御":
        mult = min(0.50, round(0.2 + power * 0.6, 2))
        effects["physical_defense_multiplier"] = mult
        effects["magic_defense_multiplier"] = mult
        desc_parts.append(f"双防+{mult:.0%}")
    elif category == "控制":
        effects["dodge_bonus"] = int(8 + power * 20)
        effects["counter_damage_bonus"] = int(5 + power * 15)
        effects["lifesteal_bonus"] = int(3 + power * 10)
        desc_parts.append(
            f"闪避+{effects['dodge_bonus']}%，反击+{effects['counter_damage_bonus']}%，吸血+{effects['lifesteal_bonus']}%"
        )
    elif category == "探索":
        mult = round(0.15 + power * 0.4, 2)
        effects["exploration_reward_multiplier"] = mult
        effects["rift_reward_multiplier"] = mult
        effects["drop_chance_bonus"] = int(5 + power * 20)
        effects["dodge_bonus"] = int(3 + power * 8)
        effects["luck_bonus"] = int(2 + power * 8)
        desc_parts.append(
            f"历练/秘境收益+{mult:.0%}，掉落概率+{effects['drop_chance_bonus']}%，"
            f"闪避+{effects['dodge_bonus']}%，气运+{effects['luck_bonus']}"
        )
    elif category == "炼丹":
        effects["alchemy_success_bonus"] = int(6 + power * 15)
        effects["alchemy_quality_bonus"] = int(3 + power * 12)
        if any(keyword in name for keyword in ("引雷", "避劫")):
            effects["tribulation_shield"] = 1
            desc_parts.append("免疫一次丹劫毁丹")
        desc_parts.append(
            f"炼丹成功率+{effects['alchemy_success_bonus']}%，高品质率+{effects['alchemy_quality_bonus']}%"
        )
    elif category == "回复":
        effects["heal_percent"] = round(0.25 + power * 0.2, 2)
        desc_parts.append(f"立即恢复 {effects['heal_percent']:.0%} 气血/灵气")
    else:
        mult = round(0.1 + power * 0.3, 2)
        effects["cultivation_multiplier"] = mult
        effects["lifespan_regen_per_minute"] = int(1 + power * 3)
        effects["crit_rate_bonus"] = int(2 + power * 5)
        effects["luck_bonus"] = int(1 + power * 6)
        desc_parts.append(
            f"修炼速度+{mult:.0%}，寿命回复+{effects['lifespan_regen_per_minute']}/分钟，"
            f"气运+{effects['luck_bonus']}"
        )

    return {
        "name": name,
        "rank": rank,
        "category": category,
        "duration": duration,
        "effects": effects,
        "description": "；".join(desc_parts) if desc_parts else "特殊效果",
    }


__all__ = [
    "TIER_POWER", "TIER_LEVEL", "TIER_GOLD", "TIER_PRICE",
    "CATEGORY_KEYWORDS", "NON_TALISMAN_ITEMS",
    "infer_category", "build_profile",
]
