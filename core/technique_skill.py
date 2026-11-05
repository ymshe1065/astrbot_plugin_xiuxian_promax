# core/technique_skill.py
"""主修心法主动绝技的统一读取与文案。

战斗、物品信息与 /心法 查询共用同一套口径，避免展示与实战不一致。
"""

from __future__ import annotations

from typing import Dict, List

SKILL_TYPE_LABELS = {
    "pierce": "破甲爆发",
    "heal": "回春治疗",
    "freeze": "冰封控制",
    "burn": "灼烧持续",
    "guard": "守御减伤",
}


def get_active_skill(config_manager, name: str) -> Dict:
    """读取指定心法的主动绝技配置；不存在时返回空字典。"""
    if not name:
        return {}
    techniques = getattr(config_manager, "techniques_data", {}) or {}
    items = getattr(config_manager, "items_data", {}) or {}
    data = techniques.get(name) or items.get(name) or {}
    return dict(data.get("active_skill") or {})


def skill_type_label(skill: Dict) -> str:
    kind = str((skill or {}).get("type") or "")
    return SKILL_TYPE_LABELS.get(kind, kind or "未知")


def skill_effect_text(skill: Dict) -> str:
    """把绝技数值翻译成与实战一致的文案。"""
    skill = skill or {}
    kind = str(skill.get("type") or "")
    try:
        value = float(skill.get("value") or 0)
    except (TypeError, ValueError):
        value = 0.0

    if kind == "pierce":
        try:
            extra = max(0.0, float(skill.get("extra_damage") or 0))
        except (TypeError, ValueError):
            extra = 0.0
        return f"无视目标 {value:.0%} 物防，并追加 {extra:.0%} 攻击伤害"
    if kind == "heal":
        return f"回复自身已损失气血的 {value:.0%}"
    if kind == "freeze":
        if value <= 0:
            return "冻结目标 1 回合，使其无法行动"
        return f"目标下回合行动概率降至 {value:.0%}"
    if kind == "burn":
        return f"灼烧 3 回合，每回合损失 {value:.0%} 最大生命；重复释放会引爆旧灼烧"
    if kind == "guard":
        return f"本回合与下回合减伤 {value:.0%}，并将 10% 防御转为攻击"
    return str(skill.get("description") or "")


def skill_summary_lines(skill: Dict, parent_description: str = "") -> List[str]:
    """绝技详情行：名称、类型、触发、消耗、效果与配置说明。

    ``parent_description`` 传入物品自身的描述文本；若该描述里已经写过同一句
    绝技说明，就不再单列「说明:」，避免货架上一段话出现两次。
    """
    if not skill:
        return []
    kind = str(skill.get("type") or "")
    try:
        trigger = int(skill.get("trigger_round") or 2)
    except (TypeError, ValueError):
        trigger = 2
    try:
        cost = int(skill.get("mp_cost") or 0)
    except (TypeError, ValueError):
        cost = 0

    lines = [
        f"绝技：{skill.get('name', '心法绝技')}（{skill_type_label(skill)}）",
        f"触发：第 {trigger} 回合起自动施展（PvP 每场 1 次；PvE 每 6 回合可再放）",
        f"消耗：{cost:,} 灵气",
        f"效果：{skill_effect_text(skill)}",
    ]
    description = str(skill.get("description") or "").strip()
    if description and description not in skill_effect_text(skill):
        parent = str(parent_description or "").strip()
        if not (parent and (description in parent or parent in description)):
            lines.append(f"说明：{description}")
    return lines


def skill_brief(skill: Dict) -> str:
    """单行绝技摘要，供 /我的装备 等紧凑展示。"""
    if not skill:
        return ""
    try:
        trigger = int(skill.get("trigger_round") or 2)
    except (TypeError, ValueError):
        trigger = 2
    try:
        cost = int(skill.get("mp_cost") or 0)
    except (TypeError, ValueError):
        cost = 0
    name = skill.get("name", "心法绝技")
    return f"{name}（第 {trigger} 回合，-{cost:,} 灵气）"
