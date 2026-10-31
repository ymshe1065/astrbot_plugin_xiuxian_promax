"""统一物品估值函数：回收、坊市、拍卖、NPC 问价共用同一套价值计算。

规则：
- 基础价：材料以 material_catalog 为单一数据源，其余取配置价；
- 品质倍率：一般 1.0 / 良好 1.05 / 极佳 1.10 / 大师 1.15；
- 词条加成：每 1 条词条 +5% 基础价；
- 回收价：估值 × 60%。

强化（forge_level）是玩家全局属性，不属于单件物品价值，不计入估值。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from ..config_manager import ConfigManager

QUALITY_MULT = {"一般": 1.0, "良好": 1.05, "极佳": 1.10, "大师": 1.15}
AFFIX_BONUS_PER_ENTRY = 0.05
RECYCLE_RATE = 0.60


def quality_price_multiplier(quality: Optional[str]) -> float:
    return QUALITY_MULT.get(str(quality or "一般"), 1.0)


def affix_price_multiplier(affixes: Optional[List]) -> float:
    count = len([a for a in (affixes or []) if a]) if affixes is not None else 0
    return 1.0 + AFFIX_BONUS_PER_ENTRY * min(count, 12)


def base_price(config_manager: "ConfigManager", item_name: str) -> int:
    try:
        return int(config_manager.get_item_price(item_name) or 0)
    except Exception:
        return 0


def item_value(
    config_manager: "ConfigManager",
    item_name: str,
    quality: Optional[str] = "一般",
    affixes: Optional[List] = None,
) -> int:
    base = base_price(config_manager, item_name)
    if base <= 0:
        return 0
    value = base * quality_price_multiplier(quality) * affix_price_multiplier(affixes)
    return max(1, int(round(value)))


def recycle_value(
    config_manager: "ConfigManager",
    item_name: str,
    quality: Optional[str] = "一般",
    affixes: Optional[List] = None,
) -> int:
    return max(0, int(round(item_value(config_manager, item_name, quality, affixes) * RECYCLE_RATE)))
