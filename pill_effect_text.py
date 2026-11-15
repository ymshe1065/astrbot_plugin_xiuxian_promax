# -*- coding: utf-8 -*-
"""丹药效果文案的单一来源。

丹方卷轴描述、``/丹药背包`` 网格、``/丹药配方`` 列表都需要回答
「这一炉丹炼出来到底干什么」。历史数据存在三种写法：

* ``exp_gain`` / ``breakthrough_bonus`` / ``restore_*_percent`` 等顶层字段；
* 老版 ``effect: {add_experience: 9680, ...}``；
* ``effect_type: temporary`` + ``*_multiplier``。

这里统一生成一句话，避免各界面各写一套、同一颗丹出现三种说法。
仅供纯数据拼装，不依赖 ConfigManager / Manager，避免循环导入。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "GENERIC_PILL_DESC_RE",
    "MISSING_EFFECT",
    "quality_caps",
    "describe_pill_effect",
]

# items.json 里 443 条丹药的描述都是这句，说了等于没说
GENERIC_PILL_DESC_RE = re.compile(r"这是一件.+丹药|蕴含着天地之力")
# 数据层没有效果定义时的统一文案；调用方再决定是否加 ⚠️
MISSING_EFFECT = "丹药效果"

# 老版 effect 字典字段 → 展示名；add_breakthrough_bonus 是百分比，单独处理
_LEGACY_LABELS: Tuple[Tuple[str, str], ...] = (
    ("add_hp", "气血·灵气"),
    ("add_experience", "修为"),
    ("add_max_hp", "气血上限"),
    ("add_spiritual_power", "法伤"),
    ("add_mental_power", "精神力"),
    ("add_attack", "物伤"),
    ("add_defense", "物防"),
    ("add_gold", "灵石"),
)

# 临时状态倍率字段 → 展示名
_TEMP_LABELS: Tuple[Tuple[str, str], ...] = (
    ("cultivation_multiplier", "修炼速度"),
    ("physical_damage_multiplier", "物伤"),
    ("magic_damage_multiplier", "法伤"),
    ("physical_defense_multiplier", "物防"),
    ("magic_defense_multiplier", "法防"),
)

# 永久属性字段 → 展示名
_PERMANENT_LABELS: Tuple[Tuple[str, str], ...] = (
    ("physical_damage_gain", "物伤"),
    ("magic_damage_gain", "法伤"),
    ("physical_defense_gain", "物防"),
    ("magic_defense_gain", "法防"),
    ("mental_power_gain", "精神力"),
    ("lifespan_gain", "寿命"),
    ("max_blood_qi_gain", "气血上限"),
    ("max_spiritual_qi_gain", "灵气上限"),
    ("base_attribute_limit_increase", "永久属性上限"),
)


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def quality_caps(pill_quality_data: Any) -> Tuple[float, float]:
    """从 pill_quality.json 取品质对「普通数值 / 突破成功率」的放大上界。"""
    effect_cap = 1.0
    breakthrough_cap = 1.0
    qualities = {}
    if isinstance(pill_quality_data, dict):
        qualities = pill_quality_data.get("qualities") or {}
    if isinstance(qualities, dict):
        for entry in qualities.values():
            if not isinstance(entry, dict):
                continue
            effect = _number(entry.get("effect_multiplier"))
            breakthrough = _number(entry.get("breakthrough_multiplier"))
            if effect is not None:
                effect_cap = max(effect_cap, effect)
            if breakthrough is not None:
                breakthrough_cap = max(breakthrough_cap, breakthrough)
    return effect_cap, breakthrough_cap


def _int_text(value: float) -> str:
    return f"{int(round(value)):,}"


def _span_int(value: float, cap: float, signed: bool = False) -> str:
    """数值区间：一般 ~ 品质上界；cap<=1 时只显示单值。"""
    low = value
    high = value * cap if cap > 1 else value
    sign = "+" if signed and low >= 0 else ""
    low_text = f"{sign}{_int_text(abs(low))}" if low >= 0 else f"-{_int_text(abs(low))}"
    if abs(high - low) < 1e-6:
        return low_text
    high_text = f"+{_int_text(abs(high))}" if high >= 0 else f"-{_int_text(abs(high))}"
    return f"{low_text}~{high_text}"


def _pct(value: float, signed: bool = True) -> str:
    text = f"{value * 100:.0f}%"
    return f"+{text}" if signed and value >= 0 else text


def _span_pct(value: float, cap: float, signed: bool = False) -> str:
    low = value
    high = value * cap if cap > 1 else value
    low_text = _pct(low, signed)
    if abs(high - low) < 1e-6:
        return low_text
    high_text = _pct(high, signed)
    return f"{low_text}~{high_text}"


def _join(parts: Iterable[str]) -> str:
    """去重后拼接；效果文案里禁止半角斜杠（前端会误抽成指令按钮）。"""
    result: List[str] = []
    for part in parts:
        text = str(part or "").strip().replace("/", "、")
        if text and text not in result:
            result.append(text)
    return "；".join(result)


def describe_pill_effect(
    pill_config: Any,
    *,
    effect_cap: float = 1.0,
    breakthrough_cap: float = 1.0,
) -> str:
    """返回一颗丹的效果文案；数据层确实没写效果时返回空串。

    优先使用结构化字段（能给出品质区间），其次才退回配置里的描述。
    """
    if not isinstance(pill_config, dict):
        return ""

    parts: List[str] = []
    subtype = str(pill_config.get("subtype") or "")
    effect_type = str(pill_config.get("effect_type") or "")

    # ---- 顶层数值字段 ----
    exp_gain = _number(pill_config.get("exp_gain"))
    if exp_gain:
        parts.append(f"增加 {_span_int(exp_gain, effect_cap)} 修为")

    breakthrough_bonus = _number(pill_config.get("breakthrough_bonus"))
    if breakthrough_bonus:
        parts.append(f"提升 {_span_pct(breakthrough_bonus, breakthrough_cap)} 突破成功率")

    restore_hp = _number(pill_config.get("restore_hp_percent"))
    if restore_hp:
        parts.append(f"恢复 {_span_pct(restore_hp, effect_cap)} 最大气血")

    restore_qi = _number(pill_config.get("restore_qi_percent"))
    if restore_qi:
        parts.append(f"恢复 {_span_pct(restore_qi, effect_cap)} 最大灵气")

    # ---- 老版 effect 字典 ----
    legacy = pill_config.get("effect")
    if isinstance(legacy, dict) and legacy:
        for key, label in _LEGACY_LABELS:
            value = _number(legacy.get(key))
            if not value:
                continue
            parts.append(f"{label} {_span_int(value, effect_cap, signed=True)}")
        bonus = _number(legacy.get("add_breakthrough_bonus"))
        if bonus:
            parts.append(
                f"突破成功率 {_span_pct(bonus, breakthrough_cap)}（1小时）"
            )

    # ---- 临时状态 ----
    if effect_type == "temporary" or any(key in pill_config for key, _ in _TEMP_LABELS):
        for key, label in _TEMP_LABELS:
            value = _number(pill_config.get(key))
            if not value:
                continue
            parts.append(f"{label} {_span_pct(value, effect_cap)}")
        duration = _number(pill_config.get("duration_minutes"))
        if duration and duration > 0:
            parts.append(f"持续 {int(duration)} 分钟")
        for key, label in (
            ("blood_qi_regen_per_minute", "气血回复"),
            ("spiritual_qi_regen_per_minute", "灵气回复"),
            ("lifespan_regen_per_minute", "寿命回复"),
        ):
            value = _number(pill_config.get(key))
            if value:
                parts.append(f"{label} +{_int_text(value)}/分")
        for key, label in (
            ("blood_qi_cost_per_minute", "气血消耗"),
            ("lifespan_cost_per_minute", "寿命消耗"),
        ):
            value = _number(pill_config.get(key))
            if value:
                parts.append(f"{label} -{_int_text(value)}/分")

    # ---- 永久属性 ----
    if effect_type == "permanent":
        for key, label in _PERMANENT_LABELS:
            value = _number(pill_config.get(key))
            if value:
                parts.append(f"{label} {_span_int(value, 1.0, signed=True)}")
        cultivation = _number(pill_config.get("cultivation_multiplier"))
        if cultivation:
            parts.append(f"修炼速度 {_pct(cultivation)}")
        death_protection = _number(pill_config.get("death_protection_multiplier"))
        if death_protection:
            parts.append(f"免死保护 {_pct(death_protection)}")

    # ---- 即时效果 ----
    if effect_type == "instant":
        if pill_config.get("resurrects") or subtype == "resurrection":
            parts.append("抵消一次突破失败的境界跌落")
        if pill_config.get("resets_permanent_pills"):
            parts.append("重置所有永久属性丹药效果")
        if pill_config.get("blocks_next_debuff"):
            parts.append("抵消下一次负面效果")
        blood = _number(pill_config.get("blood_qi_restore"))
        if blood is not None:
            parts.append(
                "瞬间恢复气血至满" if blood < 0 else f"瞬间恢复气血 {_int_text(blood)}"
            )
        qi = _number(pill_config.get("spiritual_qi_restore"))
        if qi is not None:
            parts.append("瞬间恢复灵气至满" if qi < 0 else f"瞬间恢复灵气 {_int_text(qi)}")

    if subtype == "resurrection" and not any("境界跌落" in p for p in parts):
        parts.append("抵消一次突破失败的境界跌落")

    text = _join(parts)
    if text:
        return text

    # 数据层没写结构化效果时，退回人工描述（但过滤「蕴含天地之力」这类废话）
    desc = str(pill_config.get("description") or "").strip()
    if desc and not GENERIC_PILL_DESC_RE.search(desc):
        return desc.replace("/", "、")
    return ""
