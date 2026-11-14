# core/pill_effect_text.py
"""丹药效果文案：/丹方 已学列表、/丹阁 丹方 货架共用一份口径。

同一张丹方会出现在三个界面：/丹方（已学）、/丹阁 丹方（可买）、/炼丹（产出）。
此前「这炉丹做出来干什么」只有 AlchemyManager 内部能拼出来，货架拿不到，
于是丹阁丹方只剩一句「记载XX炼制方法的丹方。」——知道怎么买，不知道买来干什么（F9）。

放在 core 而不是 managers，是为了让 core/shop_manager 也能引：
managers.alchemy_manager 反过来依赖 core，货架再反向导入会绕成环。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .item_rules import format_bonus_rate

# config/pill_recipes.json 的 desc 全是占位文案，两种写法：
# 「记载XX炼制方法的丹方。」与「记载XX炼制方法的丹方，学习后可用 … 炼制。」
# 只用前缀判定，因为后半句有没有都不影响它是不是废话。
PLACEHOLDER_DESC_RE = re.compile(r"^记载.+?炼制方法的丹方")
# items.json 里 443 条丹药的 description 全是这句，同样等于没写：
# 「这是一件凡品丹药，蕴含着天地之力。」——只说品阶，不说作用。
GENERIC_DESC_RE = re.compile(r"这是一件.+丹药|蕴含着天地之力")

# 数据层压根没有效果时的哨兵。展示层据此显式写「效果未录入」，
# 不许静默留白——留白会被读成「这炉丹没用」。
DESC_MISSING = "丹药效果"


def quality_cap(config_manager: Any, key: str = "effect_multiplier") -> float:
    """品质对效果的放大上界（一般 1.0 → 大师 1.6），取自 pill_quality.json。"""
    data = getattr(config_manager, "pill_quality_data", None) or {}
    qualities = data.get("qualities") if isinstance(data, dict) else None
    caps = []
    for entry in (qualities or {}).values():
        if not isinstance(entry, dict):
            continue
        try:
            caps.append(float(entry.get(key, 1.0) or 1.0))
        except (TypeError, ValueError):
            continue
    return max(caps) if caps else 1.0


def find_pill_config(config_manager: Any, name: str) -> Optional[Dict[str, Any]]:
    """按丹药名找配置：修为丹表 → 功能丹表 → 通用丹药表 → items.json。"""
    if not config_manager or not name:
        return None
    for attr in ("exp_pills_data", "utility_pills_data", "pills_data"):
        data = getattr(config_manager, attr, {}) or {}
        if name in data:
            return data[name]
    item = (getattr(config_manager, "items_data", {}) or {}).get(name)
    if isinstance(item, dict) and item.get("type") == "丹药":
        return item
    return None


def pill_effect_text(config_manager: Any, pill_config: Dict[str, Any]) -> str:
    """产出丹药的效果一句话，数值给到品质提升后的上界。

    品质倍率（一般 1.0 / 极佳 1.35 / 大师 1.6）只作用于数值型效果，
    所以直接写成区间——玩家只有看见「一般 500、大师 800」，
    「猛火博品质」这个选择才存在。区间上界取自 pill_quality.json，不硬编码。
    返回空串表示数据层没写效果，由调用方落成 DESC_MISSING。
    """
    if not isinstance(pill_config, dict):
        return ""
    rank = pill_config.get("rank", "")
    gain_cap = quality_cap(config_manager, "effect_multiplier")
    break_cap = quality_cap(config_manager, "breakthrough_multiplier")

    def span(value: float, cap: float, percent: bool = False) -> str:
        high = value * cap
        if percent:
            return f"{value:.0f}%~{high:.0f}%"
        return f"{int(round(value))}~{int(round(high))}"

    if pill_config.get("exp_gain"):
        return f"增加 {span(float(pill_config['exp_gain']), gain_cap)} 修为（{rank}修为丹）"
    if pill_config.get("breakthrough_bonus"):
        bonus = float(pill_config["breakthrough_bonus"]) * 100
        return f"提升 {span(bonus, break_cap, True)} 突破成功率（{rank}破境丹）"
    if pill_config.get("restore_hp_percent"):
        pct = float(pill_config["restore_hp_percent"]) * 100
        return f"恢复 {span(pct, gain_cap, True)} 最大气血"
    if pill_config.get("restore_qi_percent"):
        pct = float(pill_config["restore_qi_percent"]) * 100
        return f"恢复 {span(pct, gain_cap, True)} 最大灵气"

    # items.json 里 443 条「只有一句废话描述」的丹药，效果其实都在结构化字段里：
    # effect{add_hp/add_experience} 285 条、effect_type=temporary 158 条。
    # 早先只读 description，于是这些丹在丹方页被写成「效果未录入」——数据在，没读。
    legacy = pill_config.get("effect")
    if isinstance(legacy, dict) and legacy:
        labels = (
            # 半角斜杠会被前端抽成点不动的假命令按钮（F8），这里一律用「或」
            ("add_hp", "恢复气血或灵气"), ("add_experience", "增加修为"),
            ("add_max_hp", "提升气血或灵气上限"),
            ("add_spiritual_power", "法伤"), ("add_mental_power", "精神力"),
            ("add_attack", "物伤"), ("add_defense", "物防"), ("add_gold", "灵石"),
        )
        parts = []
        for key, label in labels:
            try:
                number = float(legacy.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if number:
                parts.append(f"{label} {span(number, gain_cap)}")
        try:
            bonus = float(legacy.get("add_breakthrough_bonus") or 0) * 100
        except (TypeError, ValueError):
            bonus = 0.0
        if bonus:
            parts.append(f"突破成功率 {span(bonus, break_cap, True)}")
        if parts:
            return "、".join(parts)

    effect_type = str(pill_config.get("effect_type") or "")
    if effect_type == "temporary":
        parts = []
        mult = pill_config.get("cultivation_multiplier")
        if mult:
            parts.append(f"修炼速度 {format_bonus_rate(mult, cap=None)}")
        for key, label in (
            ("physical_damage_multiplier", "物伤"), ("magic_damage_multiplier", "法伤"),
            ("physical_defense_multiplier", "物防"), ("magic_defense_multiplier", "法防"),
        ):
            if pill_config.get(key):
                parts.append(f"{label} {format_bonus_rate(pill_config[key], cap=None)}")
        if pill_config.get("is_random"):
            parts.append("随机一项战斗属性 +50%，其余三项 -50%")
        try:
            minutes = int(pill_config.get("duration_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes:
            parts.append(f"持续 {minutes} 分钟")
        if parts:
            return "、".join(parts)
    if effect_type == "permanent":
        parts = []
        for key, label in (
            ("physical_damage_gain", "物伤"), ("magic_damage_gain", "法伤"),
            ("physical_defense_gain", "物防"), ("magic_defense_gain", "法防"),
            ("mental_power_gain", "精神力"), ("lifespan_gain", "寿命"),
            ("max_spiritual_qi_gain", "最大灵气"), ("max_blood_qi_gain", "最大气血"),
        ):
            try:
                number = float(pill_config.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if number:
                parts.append(f"{label} +{number:.0f}（永久）")
        if parts:
            return "、".join(parts)
    if effect_type == "instant":
        parts = []
        if pill_config.get("restore_hp_percent"):
            parts.append(f"恢复 {float(pill_config['restore_hp_percent']):.0%} 气血")
        qi = pill_config.get("restore_qi_percent")
        if qi is None:
            qi = pill_config.get("restore_mp_percent")
        if qi:
            parts.append(f"恢复 {float(qi):.0%} 灵气")
        if parts:
            return "、".join(parts)

    desc = str(pill_config.get("description") or "").strip()
    if desc and not GENERIC_DESC_RE.search(desc):
        return desc
    return ""


def recipe_scroll_info(config_manager: Any, scroll: Dict[str, Any]) -> Dict[str, Any]:
    """丹方卷轴的能力：能炼出什么丹、这丹干什么用、门槛多少。

    货架项与 items.json 条目同构，都是「收据式」数据：只有 recipe_id，
    真实配方要去 pill_recipes_data 里查，效果再往下一层查丹药配置。
    """
    info: Dict[str, Any] = {"pill": "", "effect": "", "level": 0, "rate": 0.0, "target_level": None}
    data = scroll if isinstance(scroll, dict) else {}
    if not config_manager:
        return info
    recipe = None
    recipe_id = data.get("recipe_id")
    if recipe_id is not None:
        try:
            recipe = config_manager.get_recipe_by_id(recipe_id)
        except Exception:
            recipe = None
    if not isinstance(recipe, dict):
        try:
            recipe = config_manager.get_recipe_by_scroll(str(data.get("name") or ""))
        except Exception:
            recipe = None
    if not isinstance(recipe, dict):
        return info

    pill = str(recipe.get("output_pill") or recipe.get("output") or recipe.get("name") or "")
    info["pill"] = pill
    try:
        info["level"] = int(recipe.get("level_required", recipe.get("level", 0)) or 0)
    except (TypeError, ValueError):
        info["level"] = 0
    try:
        info["rate"] = float(recipe.get("success_rate", recipe.get("success", 0)) or 0)
    except (TypeError, ValueError):
        info["rate"] = 0.0

    pill_config = find_pill_config(config_manager, pill)
    if isinstance(pill_config, dict):
        info["effect"] = pill_effect_text(config_manager, pill_config)
        target = pill_config.get("target_level_index")
        if target is not None:
            try:
                info["target_level"] = int(target)
            except (TypeError, ValueError):
                info["target_level"] = None
    return info
