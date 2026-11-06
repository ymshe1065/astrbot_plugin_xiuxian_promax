"""临时状态（丹药 / 符箓）的文案化。

``/我的状态``、``/丹药背包``、``/使用符箓`` 都要把
``player.active_pill_effects`` 里的原始字段翻译成人话。
字段名分散在 pill_manager 与 talisman_manager 两处写入，
这里集中一份映射，避免各界面各写一套导致「同一效果三种说法」。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def format_duration(minutes: int) -> str:
    """分钟数 → 「1小时30分钟」这类中文时长。"""
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return "未知"
    if minutes <= 0:
        return "已到期"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}天{hours}小时" if hours else f"{days}天"
    if hours:
        return f"{hours}小时{mins}分钟" if mins else f"{hours}小时"
    return f"{mins}分钟"


def _percent(value: float) -> str:
    return f"+{value * 100:.0f}%"


def _percent_point(value: float) -> str:
    return f"+{value:.0f}%"


def _point(value: float) -> str:
    return f"+{value:.0f}"


def _per_minute(value: float) -> str:
    return f"+{value:.0f}/分"


def _per_minute_cost(value: float) -> str:
    return f"-{value:.0f}/分"


# 效果字段 → (显示名, 格式化函数)
EFFECT_LABELS: Dict[str, Tuple[str, Any]] = {
    "cultivation_multiplier": ("修炼速度", _percent),
    "physical_damage_multiplier": ("物伤", _percent),
    "magic_damage_multiplier": ("法伤", _percent),
    "physical_defense_multiplier": ("物防", _percent),
    "magic_defense_multiplier": ("法防", _percent),
    "crit_rate_bonus": ("暴击", _percent_point),
    "dodge_bonus": ("闪避", _percent_point),
    "luck_bonus": ("气运", _point),
    "lifespan_regen_per_minute": ("寿命回复", _per_minute),
    "lifespan_cost_per_minute": ("寿命消耗", _per_minute_cost),
    "spiritual_qi_regen_per_minute": ("灵气回复", _per_minute),
    "blood_qi_regen_per_minute": ("气血回复", _per_minute),
    "blood_qi_cost_per_minute": ("气血消耗", _per_minute_cost),
    "breakthrough_bonus": ("突破成功率", _percent),
    "alchemy_success_bonus": ("炼丹成功率", _percent_point),
    "alchemy_quality_bonus": ("炼丹品质", _percent_point),
    # 避劫是限时状态而非层数：显示成「可挡劫数≤6」比「6层」准确
    "tribulation_shield": ("避劫", lambda v: f"可挡劫数≤{v:.0f}"),
    "exploration_reward_multiplier": ("历练收益", _percent),
    "rift_reward_multiplier": ("秘境收益", _percent),
    "drop_chance_bonus": ("掉落", _percent_point),
    "heal_percent": ("已回气血", lambda v: f"{v * 100:.0f}%"),
    "frost_chance": ("霜冻", _percent_point),
    "freeze_chance": ("冰冻", _percent_point),
    "stun_chance": ("眩晕", _percent_point),
    "curse_chance": ("诅咒", _percent_point),
    "curse_defense_pct": ("破防", _percent_point),
    "burn_heal_reduction": ("禁疗", lambda v: f"-{v:.0f}%"),
}

# 不该当成「效果」展示的元字段
META_KEYS = {
    "pill_name", "pill_key", "pill_id", "quality", "subtype", "source",
    "talisman_name", "category", "start_time", "expiry_time",
    "duration_minutes", "last_tick_time", "remaining_uses",
    "random_effect_key", "turns", "frost_turns", "curse_turns", "stun_turns",
    "frost_atk_reduction",
}

# 效果字段的展示顺序：先面板倍率，再功能类，最后负面
EFFECT_ORDER = list(EFFECT_LABELS.keys())


def effect_label(key: str) -> str:
    entry = EFFECT_LABELS.get(key)
    return entry[0] if entry else key


def describe_effects(effect: Dict[str, Any], limit: int = 0) -> str:
    """把一个状态条目的效果字段拼成「物伤+28%；法伤+28%；暴击+12%」。"""
    if not isinstance(effect, dict):
        return "未知效果"
    parts: List[str] = []
    for key in EFFECT_ORDER:
        if key not in effect:
            continue
        label, fmt = EFFECT_LABELS[key]
        try:
            value = float(effect[key])
        except (TypeError, ValueError):
            continue
        if value == 0:
            continue
        parts.append(f"{label}{fmt(value)}")
        if limit and len(parts) >= limit:
            break
    if not parts:
        # 未知字段兜底：至少别显示空白
        for key, value in effect.items():
            if key in META_KEYS or isinstance(value, (dict, list)):
                continue
            try:
                parts.append(f"{key}+{float(value):g}")
            except (TypeError, ValueError):
                continue
    return "；".join(parts) or "—"


def describe_remaining(effect: Dict[str, Any], now: int) -> str:
    """剩余时间 / 剩余次数；无时限返回「待触发」。"""
    if not isinstance(effect, dict):
        return "未知"
    uses = effect.get("remaining_uses")
    if uses is not None:
        try:
            return f"剩余 {max(0, int(uses))} 次"
        except (TypeError, ValueError):
            return "剩余次数异常"
    try:
        expiry = int(effect.get("expiry_time") or 0)
    except (TypeError, ValueError):
        expiry = 0
    if expiry <= 0:
        return "待触发"
    left = expiry - int(now)
    if left <= 0:
        return "已到期"
    return f"剩余 {format_duration(max(1, left // 60))}"


def status_title(effect: Dict[str, Any]) -> str:
    """状态名：符箓/丹药名 + 品质。"""
    name = (
        effect.get("talisman_name")
        or effect.get("pill_name")
        or "未知状态"
    )
    quality = effect.get("quality")
    if quality and quality != "一般":
        return f"{name}·{quality}"
    return str(name)


__all__ = [
    "format_duration",
    "EFFECT_LABELS",
    "META_KEYS",
    "effect_label",
    "describe_effects",
    "describe_remaining",
    "status_title",
]
