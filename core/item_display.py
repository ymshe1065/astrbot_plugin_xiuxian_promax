"""物品展示后缀：统一「物品名（种类·细分 | 品阶）」的写法。

储物戒、藏宝阁、搜索结果、回收预览都用同一套标签，避免同一种物品
在不同面板里说法不一致（例如储物戒写「玄铁」、仓库写「材料」）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .item_rules import ItemRules

# 装备槽位 → 玩家看得懂的部位说法（内部值来自 ItemRules.normalize_equipment_type）
SLOT_LABELS: Dict[str, str] = {
    "weapon": "武器",
    "head": "头饰",
    "body": "甲胄",
    "armor": "甲胄",
    "gloves": "手套",
    "boots": "靴子",
    "ring": "戒指",
    "amulet": "护符",
    "main_technique": "心法",
    "technique": "技能",
}

# 丹药配置里的 subtype → 中文（目前只有突破类）
PILL_SUBTYPE_LABELS: Dict[str, str] = {
    "breakthrough": "突破",
    "exp": "修为",
    "cultivation": "修为",
    "combat": "战斗",
    "combat_boost": "战斗",
}


def _item_config(item_name: str, config_manager: Any) -> Dict[str, Any]:
    if config_manager is None:
        return {}
    try:
        return config_manager.get_item_config(item_name) or {}
    except Exception:
        return {}


def _material_config(item_name: str, config_manager: Any) -> Dict[str, Any]:
    """材料目录优先：灵植/矿材/妖兽/灵液只在这张表里。"""
    if config_manager is None:
        return {}
    try:
        return config_manager.get_material_info(item_name) or {}
    except Exception:
        return {}


def _talisman_recipe(item_name: str, config_manager: Any) -> Dict[str, Any]:
    if config_manager is None:
        return {}
    recipes = getattr(config_manager, "talisman_recipes_data", {}) or {}
    if isinstance(recipes, dict):
        return recipes.get(item_name) or {}
    return {}


def _is_recipe_scroll(item_name: str, config_manager: Any) -> bool:
    if config_manager is None:
        return False
    try:
        return bool(config_manager.get_recipe_by_scroll(item_name))
    except Exception:
        return False


def item_meta(item_name: str, config_manager: Any = None) -> Dict[str, str]:
    """解析物品种类、细分与品阶；无法识别时 kind 为空串。"""
    name = str(item_name or "")
    if not name:
        return {"kind": "", "sub": "", "rank": ""}

    cfg = _item_config(name, config_manager)
    material = _material_config(name, config_manager)

    kind = ""
    sub = ""

    raw_type = str(cfg.get("type") or "")

    # 非装备类别必须先判：功法在装备槽位表里也占一格（technique），
    # 若先走 is_equipment，心法会被误标成「装备·心法」。
    if material:
        kind, sub = "材料", str(material.get("category") or "")
    elif raw_type in ("符箓", "talisman"):
        kind = "符箓"
        sub = str(_talisman_recipe(name, config_manager).get("category") or "")
    elif raw_type in ("丹药", "pill"):
        kind = "丹药"
        sub = PILL_SUBTYPE_LABELS.get(str(cfg.get("subtype") or ""), "")
    elif raw_type == "丹方" or _is_recipe_scroll(name, config_manager):
        kind = "丹方"
    elif raw_type in ("功法", "technique", "main_technique", "心法"):
        kind = "功法"
        tech = (getattr(config_manager, "techniques_data", {}) or {}).get(name) or {}
        tech_type = str(tech.get("type") or "")
        if tech_type == "main_technique" or raw_type in ("main_technique", "心法"):
            sub = "心法"
        else:
            sub = "技能"
    elif raw_type == "材料":
        kind = "材料"
    elif ItemRules.is_equipment(cfg):
        kind = "装备"
        sub = SLOT_LABELS.get(ItemRules.normalize_equipment_type(cfg), "")

    rank = str(material.get("tier") or cfg.get("rank") or cfg.get("tier") or "")
    return {"kind": kind, "sub": sub, "rank": rank}


def item_tag(item_name: str, config_manager: Any = None, *, with_rank: bool = True) -> str:
    """返回物品信息后缀，如「（材料·矿材 | 黄品）」。

    识别不出种类时返回空串，调用方直接拼接即可，不会出现空括号。
    """
    meta = item_meta(item_name, config_manager)
    if not meta["kind"]:
        return ""
    head = meta["kind"] + (f"·{meta['sub']}" if meta["sub"] else "")
    if with_rank and meta["rank"]:
        return f"（{head} | {meta['rank']}）"
    return f"（{head}）"


def item_label(item_name: str, config_manager: Any = None) -> str:
    """物品名 + 信息后缀，可直接放进列表行。"""
    return f"{item_name}{item_tag(item_name, config_manager)}"


# 展示后缀：仅匹配 item_tag 生成的格式，避免误删物品名自带的括号
_DISPLAY_TAG_RE = re.compile(
    r"[（(](?:材料|丹药|丹方|符箓|装备|功法)(?:·[^（）()|]+)?(?:\s*\|[^（）()]+)?[）)]"
    r"(?:·[^（）()·]+)?\s*$"
)


def strip_item_tag(label: str) -> str:
    """把「无极骨（材料·妖兽 | 玄品）」还原成可查询的「无极骨」。"""
    raw = str(label or "").strip()
    stripped = _DISPLAY_TAG_RE.sub("", raw).strip()
    return stripped or raw


def equipment_label(instance: Dict[str, Any], config_manager: Any = None) -> str:
    """装备实例：名称（装备·部位 | 品阶）·品质。

    实例里没有 rank，按名称回查配置；查不到时省略品阶。
    """
    name = str((instance or {}).get("name") or "")
    quality = str((instance or {}).get("quality") or "一般")
    if not name:
        return quality
    return f"{item_label(name, config_manager)}·{quality}"


__all__ = [
    "SLOT_LABELS",
    "PILL_SUBTYPE_LABELS",
    "item_meta",
    "item_tag",
    "item_label",
    "strip_item_tag",
    "equipment_label",
]
