"""符箓品阶、分类与效果推演规则。"""

from __future__ import annotations

from typing import Dict, Optional

from ..rank_rules import (
    RANK_ALIASES, RANK_GOLD, RANK_LEVELS, RANK_POWER, RANK_PRICE, normalize_rank,
)


def _with_aliases(table: Dict[str, object]) -> Dict[str, object]:
    """在统一阶梯上补齐旧名称，保证历史配置/存档仍可读取。"""
    merged: Dict[str, object] = dict(table)
    for alias, canonical in RANK_ALIASES.items():
        merged.setdefault(alias, table[canonical])
    return merged


TIER_POWER = _with_aliases(RANK_POWER)
TIER_LEVEL = _with_aliases(RANK_LEVELS)
TIER_GOLD = _with_aliases(RANK_GOLD)
TIER_PRICE = _with_aliases(RANK_PRICE)

CATEGORY_KEYWORDS = [
    ("炼丹", ("引雷", "避劫", "稳魂", "聚灵")),
    ("探索", ("寻宝", "遁地", "星辰", "洞天", "破障")),
    ("回复", ("回春", "甘露", "再生", "回天")),
    ("攻击", ("焚天", "紫电", "诛仙", "破军", "噬魂", "青锋", "九天", "烈焰", "雷霆", "雷", "火", "斩仙")),
    ("防御", ("山河", "长生", "太极", "八卦", "两仪", "四象", "乾坤", "混元", "无极", "太虚", "金刚", "石肤", "天罡", "不灭")),
    ("控制", ("寒霜", "幽冥", "九幽", "寂灭", "混沌", "冰", "霜", "缚天")),
    ("增益", ("鸿蒙", "造化", "万古", "疾风", "天机", "大道")),
    ("遁术", ("轻身", "缩地", "虚空", "挪移")),
]

NON_TALISMAN_ITEMS = {"化身傀儡"}

# 符箓持续分钟数，按品阶递增
DURATION_BY_RANK = {"凡品": 15, "黄品": 15, "玄品": 20, "圣品": 30}

# ===== 丹劫刻度：丹药按品阶给劫数，符箓按品阶给护盾 =====
# 判定只有一句：护盾 >= 劫数 即保全。高品阶符向下兼容是这条判定的自然结果。
TRIBULATION_DEMAND: Dict[str, int] = {
    "凡品": 0, "黄品": 0, "玄品": 1, "灵品": 2, "地品": 3, "天品": 4,
    "皇品": 5, "圣品": 6, "帝品": 7, "道品": 8, "仙品": 9, "混元先天": 10,
}

TRIBULATION_RATE: Dict[str, int] = {
    "凡品": 0, "黄品": 0, "玄品": 8, "灵品": 12, "地品": 16, "天品": 20,
    "皇品": 25, "圣品": 30, "帝品": 35, "道品": 40, "仙品": 50, "混元先天": 60,
}

# 护劫符只出四档品阶，数值只由品阶决定，不区分符名
SHIELD_BY_RANK: Dict[str, int] = {"凡品": 2, "黄品": 4, "玄品": 6, "圣品": 9}

# 四处配置都查不到丹药品阶时，按丹方需求等级兜底
FALLBACK_RANK_BY_LEVEL = [
    (31, "仙品"), (27, "道品"), (21, "帝品"), (15, "圣品"),
    (12, "地品"), (9, "灵品"), (6, "黄品"), (0, "凡品"),
]

# ===== 制符熟练度 =====
PROFICIENCY_TIERS = [
    {"name": "初学", "points": 0, "success": 0.0, "extra": 0.0, "cost_discount": 0.0},
    {"name": "入门", "points": 10, "success": 2.5, "extra": 3.0, "cost_discount": 3.0},
    {"name": "熟手", "points": 30, "success": 5.0, "extra": 6.0, "cost_discount": 6.0},
    {"name": "小成", "points": 60, "success": 7.5, "extra": 9.0, "cost_discount": 9.0},
    {"name": "大成", "points": 110, "success": 10.0, "extra": 12.0, "cost_discount": 12.0},
    {"name": "精通", "points": 180, "success": 12.5, "extra": 15.0, "cost_discount": 15.0},
    {"name": "宗师", "points": 280, "success": 15.0, "extra": 18.0, "cost_discount": 18.0},
    {"name": "符圣", "points": 400, "success": 17.5, "extra": 21.0, "cost_discount": 21.0},
]

# 每张符记多少熟练点：品阶越高越值钱，凡品 1 点
PROFICIENCY_RANK_POINTS: Dict[str, int] = {"凡品": 1, "黄品": 2, "玄品": 4, "圣品": 8}

# 不锁可制品阶，只按熟练阶压成功率：这一阶是「刚好画得动」的门槛
PROFICIENCY_RANK_THRESHOLD: Dict[str, int] = {"凡品": 0, "黄品": 2, "玄品": 4, "圣品": 6}

PENALTY_PER_LEVEL = 12.0
BONUS_PER_LEVEL = 3.0
PROFICIENCY_SUCCESS_MIN = 5
PROFICIENCY_SUCCESS_MAX = 99
FAIL_POINTS_RATIO = 0.5


def proficiency_level(points: int) -> int:
    """熟练点对应的阶位下标（0 初学 ~ 7 符圣）。"""
    try:
        points = max(0, int(points or 0))
    except (TypeError, ValueError):
        points = 0
    level = 0
    for index, tier in enumerate(PROFICIENCY_TIERS):
        if points >= int(tier["points"]):
            level = index
    return level


def proficiency_tier(points: int) -> Dict:
    return PROFICIENCY_TIERS[proficiency_level(points)]


def proficiency_success(recipe_rate: float, rank: str, points: int) -> float:
    """成功率 = 基础率 + 2.5×等级 - 12×差级 + 3×超级，钳制到 [5, 99]。"""
    rank = normalize_rank(rank, "凡品")
    level = proficiency_level(points)
    threshold = PROFICIENCY_RANK_THRESHOLD.get(rank, 0)
    rate = (
        float(recipe_rate)
        + PROFICIENCY_TIERS[level]["success"]
        - PENALTY_PER_LEVEL * max(0, threshold - level)
        + BONUS_PER_LEVEL * max(0, level - threshold)
    )
    return float(max(PROFICIENCY_SUCCESS_MIN, min(PROFICIENCY_SUCCESS_MAX, rate)))


def proficiency_gain(rank: str, count: int, success: bool) -> int:
    """成功按张数×品阶系数记点；失败按 50% 计（向下取整）。"""
    unit = PROFICIENCY_RANK_POINTS.get(normalize_rank(rank, "凡品"), 1)
    raw = max(0, int(count or 0)) * unit
    if not success:
        raw = int(raw * FAIL_POINTS_RATIO)
    return raw


# ===== 控制类效果：按品阶分档，命中的是战斗引擎真实支持的键 =====
CONTROL_BY_RANK: Dict[str, Dict[str, float]] = {
    "凡品": {"frost_chance": 15, "frost_turns": 2, "frost_atk_reduction": 8},
    "黄品": {"freeze_chance": 10, "curse_chance": 20, "curse_defense_pct": 10, "curse_turns": 2},
    "玄品": {"stun_chance": 12, "stun_turns": 1, "curse_defense_pct": 18, "curse_turns": 3},
    "圣品": {"stun_chance": 18, "stun_turns": 2, "curse_defense_pct": 25, "burn_heal_reduction": 30},
}


def infer_category(name: str, recipe_category: str = "") -> str:
    if recipe_category:
        return recipe_category
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in name for keyword in keywords):
            return category
    return "增益"


def shield_of_rank(rank: str) -> int:
    """符箓品阶 → 可保全的丹劫护盾。"""
    return int(SHIELD_BY_RANK.get(normalize_rank(rank, "凡品"), 0))


def tribulation_demand_of(pill_rank: str) -> int:
    return int(TRIBULATION_DEMAND.get(normalize_rank(pill_rank, ""), 0) or 0)


def tribulation_rate_of(pill_rank: str) -> int:
    return int(TRIBULATION_RATE.get(normalize_rank(pill_rank, ""), 0) or 0)


def rank_from_level(level_required: int) -> str:
    """丹方等级兜底品阶（配置里查不到丹药品阶时使用）。"""
    try:
        level = int(level_required or 0)
    except (TypeError, ValueError):
        level = 0
    for threshold, rank in FALLBACK_RANK_BY_LEVEL:
        if level >= threshold:
            return rank
    return "凡品"


def pill_rank_of(name: str, config_manager=None) -> str:
    """按 丹药→经验丹→功能丹→物品 的顺序查丹药品阶。"""
    if not name or config_manager is None:
        return ""
    for attr in ("pills_data", "exp_pills_data", "utility_pills_data", "items_data"):
        data = getattr(config_manager, attr, None) or {}
        cfg = data.get(name)
        if not isinstance(cfg, dict):
            continue
        rank = normalize_rank(cfg.get("rank"), "")
        if rank:
            return rank
    return ""


def _fmt_rate(value: float) -> str:
    return f"{value:.0%}" if abs(value) < 1 else f"{value:.0f}"


def _describe(category: str, effects: Dict[str, float]) -> str:
    """按已生效的键生成可读描述，避免配置与文案脱节。"""
    parts = []
    dmg = effects.get("physical_damage_multiplier") or effects.get("magic_damage_multiplier")
    if dmg:
        parts.append(f"物伤/法伤+{_fmt_rate(float(dmg))}")
    def_mult = effects.get("physical_defense_multiplier") or effects.get("magic_defense_multiplier")
    if def_mult:
        parts.append(f"双防+{_fmt_rate(float(def_mult))}")
    if effects.get("crit_rate_bonus"):
        parts.append(f"暴击+{int(effects['crit_rate_bonus'])}%")
    if effects.get("heal_percent"):
        parts.append(f"立即恢复 {_fmt_rate(float(effects['heal_percent']))} 气血/灵气")
    if effects.get("exploration_reward_multiplier"):
        parts.append(f"历练/秘境收益+{_fmt_rate(float(effects['exploration_reward_multiplier']))}")
    if effects.get("drop_chance_bonus"):
        parts.append(f"掉落概率+{int(effects['drop_chance_bonus'])}%")
    if effects.get("cultivation_multiplier"):
        parts.append(f"修炼速度+{_fmt_rate(float(effects['cultivation_multiplier']))}")
    if effects.get("alchemy_success_bonus"):
        parts.append(f"炼丹成功率+{int(effects['alchemy_success_bonus'])}%")
    if effects.get("tribulation_shield"):
        parts.append(f"护劫（可保全劫数≤{int(effects['tribulation_shield'])}的丹药）")
    if effects.get("stun_chance"):
        parts.append(f"定身 {int(effects['stun_chance'])}%/{int(effects.get('stun_turns', 1))}回合")
    if effects.get("freeze_chance"):
        parts.append(f"冰封 {int(effects['freeze_chance'])}%")
    if effects.get("frost_chance"):
        parts.append(f"霜冻 {int(effects['frost_chance'])}%/{int(effects.get('frost_turns', 1))}回合")
    if effects.get("curse_chance"):
        parts.append(f"诅咒 {int(effects['curse_chance'])}%")
    if effects.get("burn_heal_reduction"):
        parts.append(f"禁疗 -{int(effects['burn_heal_reduction'])}%")
    if effects.get("dodge_bonus"):
        parts.append(f"闪避+{int(effects['dodge_bonus'])}%")
    if effects.get("luck_bonus"):
        parts.append(f"气运+{int(effects['luck_bonus'])}")
    if effects.get("lifespan_regen_per_minute"):
        parts.append(f"寿命回复+{int(effects['lifespan_regen_per_minute'])}/分钟")
    return "；".join(parts)


def build_profile(
    name: str,
    rank: str,
    category: str = "",
    effects: Optional[Dict] = None,
    duration: Optional[int] = None,
    description: str = "",
) -> Dict:
    """根据符名、品阶、分类推演符箓效果。

    配方里显式写了 ``effects`` 时以配置为准（不再靠名字猜），
    否则按品阶系数走公式——legacy 符与自动配方走后者。
    """
    rank = normalize_rank(rank, "凡品")
    power = float(TIER_POWER.get(rank, 0.25))
    category = category or infer_category(name)
    dur = int(duration or DURATION_BY_RANK.get(rank, 15))

    if isinstance(effects, dict) and effects:
        parsed: Dict[str, float] = {}
        for key, value in effects.items():
            try:
                parsed[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return {
            "name": name,
            "rank": rank,
            "category": category,
            "duration": dur,
            "effects": parsed,
            "description": description or _describe(category, parsed) or "特殊效果",
        }

    effects = {}
    if category == "攻击":
        mult = min(0.50, round(0.15 + power * 0.5, 2))
        effects["physical_damage_multiplier"] = mult
        effects["magic_damage_multiplier"] = mult
        effects["crit_rate_bonus"] = int(8 + power * 15)
    elif category == "防御":
        mult = min(0.50, round(0.15 + power * 0.45, 2))
        effects["physical_defense_multiplier"] = mult
        effects["magic_defense_multiplier"] = mult
    elif category == "控制":
        effects.update(CONTROL_BY_RANK.get(rank, CONTROL_BY_RANK["凡品"]))
    elif category == "探索":
        mult = round(0.15 + power * 0.4, 2)
        effects["exploration_reward_multiplier"] = mult
        effects["rift_reward_multiplier"] = mult
        effects["drop_chance_bonus"] = int(5 + power * 20)
    elif category == "炼丹":
        effects["alchemy_success_bonus"] = int(6 + power * 15)
        effects["alchemy_quality_bonus"] = int(3 + power * 12)
        effects["tribulation_shield"] = shield_of_rank(rank)
    elif category == "回复":
        effects["heal_percent"] = round(0.25 + power * 0.2, 2)
    elif category == "遁术":
        effects["dodge_bonus"] = int(6 + power * 15)
        effects["luck_bonus"] = int(3 + power * 10)
    else:
        mult = round(0.10 + power * 0.3, 2)
        effects["cultivation_multiplier"] = mult
        effects["lifespan_regen_per_minute"] = int(1 + power * 3)
        effects["crit_rate_bonus"] = int(2 + power * 5)
        effects["luck_bonus"] = int(1 + power * 6)

    return {
        "name": name,
        "rank": rank,
        "category": category,
        "duration": dur,
        "effects": effects,
        "description": description or _describe(category, effects) or "特殊效果",
    }


__all__ = [
    "TIER_POWER", "TIER_LEVEL", "TIER_GOLD", "TIER_PRICE",
    "CATEGORY_KEYWORDS", "NON_TALISMAN_ITEMS", "DURATION_BY_RANK",
    "TRIBULATION_DEMAND", "TRIBULATION_RATE", "SHIELD_BY_RANK", "CONTROL_BY_RANK",
    "FALLBACK_RANK_BY_LEVEL",
    "PROFICIENCY_TIERS", "PROFICIENCY_RANK_POINTS", "PROFICIENCY_RANK_THRESHOLD",
    "PROFICIENCY_SUCCESS_MIN", "PROFICIENCY_SUCCESS_MAX", "FAIL_POINTS_RATIO",
    "infer_category", "build_profile", "shield_of_rank", "tribulation_demand_of",
    "tribulation_rate_of", "pill_rank_of", "rank_from_level",
    "proficiency_level", "proficiency_tier", "proficiency_success", "proficiency_gain",
]
