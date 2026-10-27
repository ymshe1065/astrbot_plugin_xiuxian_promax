"""统一物品类型、境界门槛与百分比单位规则。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


RANK_REQ_LEVEL = {
    "玄阶": 3,
    "地阶": 9,
    "天阶": 18,
    "灵品": 3,
    "地品": 9,
    "天品": 18,
    "皇品": 24,
    "帝品": 30,
    "道品": 36,
    "仙品": 42,
    "神品": 45,
    "混元先天": 48,
}

_RATE_KEYS = {
    "exp_multiplier",
    "crit_rate",
    "crit_damage",
    "lifesteal",
    "counter_damage",
    "combo_rate",
    "physical_damage_multiplier",
    "magic_damage_multiplier",
    "cultivation_multiplier",
    "status_resist",
    "dodge",
}

EQUIPPABLE_TYPES = {
    "weapon",
    "armor",
    "accessory",
    "head",
    "body",
    "boots",
    "gloves",
    "ring",
    "amulet",
    "main_technique",
    "technique",
}

SLOT_BY_TYPE = {
    "weapon": "weapon",
    "armor": "body",
    "head": "head",
    "body": "body",
    "boots": "boots",
    "gloves": "gloves",
    "ring": "ring1",
    "amulet": "amulet",
    "accessory": "amulet",
    "main_technique": "main_technique",
    "technique": "technique",
}


def _raw_value(item: Optional[Dict[str, Any]], key: str, default=None):
    if not isinstance(item, dict):
        return default
    if key in item:
        return item[key]
    data = item.get("data")
    if isinstance(data, dict) and key in data:
        return data[key]
    return default


def normalize_equipment_type(item: Optional[Dict[str, Any]]) -> str:
    """将两套旧配置的装备类型统一为装备槽位类型。"""
    if not isinstance(item, dict):
        return ""
    raw = str(item.get("type", "") or "")
    subtype = str(item.get("subtype", "") or "")

    if raw in ("weapon", "武器"):
        return "weapon"
    if raw in ("armor", "防具"):
        return "armor"
    if raw in ("accessory", "饰品"):
        return "amulet"
    if raw in ("head", "头饰", "头部"):
        return "head"
    if raw in ("body", "护甲", "衣服"):
        return "body"
    if raw in ("boots", "靴子", "鞋"):
        return "boots"
    if raw in ("gloves", "手套"):
        return "gloves"
    if raw in ("ring", "戒指"):
        return "ring"
    if raw in ("amulet", "护符"):
        return "amulet"
    if raw == "main_technique":
        return "main_technique"
    if raw in ("technique", "功法"):
        return "technique"
    if raw == "心法":
        return "main_technique"

    if raw in ("法器", "武器", "防具", "饰品"):
        if subtype == "武器":
            return "weapon"
        if subtype == "防具":
            return "armor"
        if subtype == "饰品":
            return "amulet"
        if subtype == "头饰":
            return "head"
        if subtype == "靴子":
            return "boots"
        if subtype == "手套":
            return "gloves"
        return "weapon"

    return raw.lower()


def normalize_pill_type(item: Optional[Dict[str, Any]]) -> str:
    if not isinstance(item, dict):
        return ""
    raw = str(item.get("type", "") or "")
    if raw in ("pill", "exp_pill", "utility_pill"):
        return raw
    if raw in ("丹药", "legacy_pill"):
        return "inventory_pill"
    return raw


def normalize_shop_type(item: Optional[Dict[str, Any]]) -> str:
    """返回商店购买分支使用的规范类型。"""
    if not isinstance(item, dict):
        return ""
    raw = str(item.get("type", "") or "")

    if raw in ("weapon", "武器"):
        return "weapon"
    if raw in ("armor", "防具"):
        return "armor"
    if raw in ("accessory", "饰品"):
        return "accessory"
    if raw in ("main_technique", "心法"):
        return "main_technique"
    if raw in ("technique", "功法"):
        return "technique"
    if raw in ("pill", "exp_pill", "utility_pill", "legacy_pill"):
        return "inventory_pill"
    if raw == "丹药":
        return "inventory_pill"
    if raw == "丹方":
        return "recipe"
    if raw in ("材料包", "material_package"):
        return "material_package"
    if raw in ("material", "材料", "符箓", "talisman", "杂物"):
        return "material"

    if raw in ("法器", "武器", "防具", "饰品"):
        subtype = str(item.get("subtype", "") or "")
        if subtype == "武器":
            return "weapon"
        if subtype == "防具":
            return "armor"
        if subtype == "饰品":
            return "accessory"
        return "weapon"

    return raw


def resolve_required_level(required_level_index: Any, rank: str = "") -> int:
    try:
        explicit = int(required_level_index)
    except (TypeError, ValueError):
        explicit = None

    if explicit is not None and explicit >= 0:
        return explicit
    return int(RANK_REQ_LEVEL.get(rank or "", 0))


def get_required_level(item: Optional[Dict[str, Any]]) -> int:
    if not isinstance(item, dict):
        return 0
    explicit = _raw_value(item, "required_level_index")
    rank = str(_raw_value(item, "rank", "") or "")
    return resolve_required_level(explicit, rank)


def get_equip_slot(item: Optional[Dict[str, Any]]) -> str:
    return SLOT_BY_TYPE.get(normalize_equipment_type(item), "")


def is_equipment(item: Optional[Dict[str, Any]]) -> bool:
    return normalize_equipment_type(item) in EQUIPPABLE_TYPES


def is_rate_key(key: str) -> bool:
    return key in _RATE_KEYS


def normalize_rate(value: Any, as_percent: bool = False) -> float:
    """统一倍率/百分比：内部返回小数；as_percent=True 时返回百分数。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if as_percent:
        return number
    return number / 100.0 if abs(number) > 1 else number


def validate_rate(key: str, value: Any) -> Tuple[bool, str]:
    if key not in _RATE_KEYS:
        return True, ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False, f"{key} 不是有效数字：{value!r}"
    if key in ("crit_rate", "lifesteal", "counter_damage", "combo_rate", "status_resist", "dodge"):
        if number < 0 or number > 100:
            return False, f"{key} 应在 0-100 之间，当前为 {number}"
    elif number < -10 or number > 10:
        return False, f"{key} 倍率异常，当前为 {number}"
    return True, ""


class ItemRules:
    RANK_REQ_LEVEL = RANK_REQ_LEVEL
    EQUIPPABLE_TYPES = EQUIPPABLE_TYPES
    SLOT_BY_TYPE = SLOT_BY_TYPE
    normalize_equipment_type = staticmethod(normalize_equipment_type)
    normalize_pill_type = staticmethod(normalize_pill_type)
    normalize_shop_type = staticmethod(normalize_shop_type)
    resolve_required_level = staticmethod(resolve_required_level)
    get_required_level = staticmethod(get_required_level)
    get_equip_slot = staticmethod(get_equip_slot)
    is_equipment = staticmethod(is_equipment)
    is_rate_key = staticmethod(is_rate_key)
    normalize_rate = staticmethod(normalize_rate)
    validate_rate = staticmethod(validate_rate)
