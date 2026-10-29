"""统一物品品阶规则：全插件唯一的一套「品」阶名称阶梯。

历史版本存在两套显示名：
- 阶系：黄阶 / 玄阶 / 地阶 / 天阶
- 品系：凡品 / 灵品 / 地品 / 天品 / 皇品 / 帝品 / 道品 / 仙品 / 混元先天

两套名字互相交叉（例如 legacy「天阶」的解锁等级高于「天品」），
导致同一名称在不同配置里代表不同等级。现已合并为一套 12 档「品」阶：

    凡品 → 黄品 → 玄品 → 灵品 → 地品 → 天品 → 皇品 → 圣品 → 帝品 → 道品 → 仙品 → 混元先天

合并原则：
1. 每个档位的默认解锁境界（``level``）保持迁移前的原值，不重新平衡数值；
2. 旧名称通过 ``normalize_rank`` 归一化到新名称，读写配置与数据库时都可兼容；
3. 档位（``tier``）随阶梯单调不减，并尽量沿用迁移前各名称的分档，用于拍卖会等稀有度分组。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

__all__ = [
    "RANK_TABLE",
    "RANK_NAMES",
    "RANK_LEVELS",
    "RANK_TIERS",
    "RANK_POWER",
    "RANK_GOLD",
    "RANK_PRICE",
    "RANK_ALIASES",
    "normalize_rank",
    "rank_level",
    "rank_tier",
    "level_to_rank",
    "is_known_rank",
    "format_rank",
]

# name: 统一名称
# level: 未显式配置 required_level_index 时的默认解锁境界
# tier: 稀有度档位（1-5，拍卖会/品质权重使用）
# power: 符箓强度系数
# gold: 符箓绘制灵石消耗
# price: 商店兜底价格
RANK_TABLE: Tuple[Dict[str, object], ...] = (
    {"name": "凡品", "level": 0, "tier": 1, "power": 0.25, "gold": 300, "price": 800},
    {"name": "黄品", "level": 3, "tier": 2, "power": 0.40, "gold": 900, "price": 2600},
    {"name": "玄品", "level": 9, "tier": 3, "power": 0.55, "gold": 3000, "price": 9000},
    {"name": "灵品", "level": 10, "tier": 3, "power": 0.60, "gold": 3600, "price": 12000},
    {"name": "地品", "level": 12, "tier": 3, "power": 0.65, "gold": 4800, "price": 16000},
    {"name": "天品", "level": 13, "tier": 4, "power": 0.70, "gold": 6000, "price": 20000},
    {"name": "皇品", "level": 16, "tier": 4, "power": 0.75, "gold": 9000, "price": 26000},
    {"name": "圣品", "level": 18, "tier": 4, "power": 0.80, "gold": 12000, "price": 35000},
    {"name": "帝品", "level": 22, "tier": 4, "power": 0.85, "gold": 16000, "price": 45000},
    {"name": "道品", "level": 28, "tier": 4, "power": 0.90, "gold": 22000, "price": 60000},
    {"name": "仙品", "level": 32, "tier": 5, "power": 1.00, "gold": 50000, "price": 120000},
    {"name": "混元先天", "level": 35, "tier": 5, "power": 1.20, "gold": 120000, "price": 300000},
)

RANK_NAMES: Tuple[str, ...] = tuple(str(row["name"]) for row in RANK_TABLE)
RANK_LEVELS: Dict[str, int] = {str(row["name"]): int(row["level"]) for row in RANK_TABLE}
RANK_TIERS: Dict[str, int] = {str(row["name"]): int(row["tier"]) for row in RANK_TABLE}
RANK_POWER: Dict[str, float] = {str(row["name"]): float(row["power"]) for row in RANK_TABLE}
RANK_GOLD: Dict[str, int] = {str(row["name"]): int(row["gold"]) for row in RANK_TABLE}
RANK_PRICE: Dict[str, int] = {str(row["name"]): int(row["price"]) for row in RANK_TABLE}

# 旧名称 → 统一名称。等级由新名称决定；迁移前各条目自身的
# required_level_index 原值不会被改写。
RANK_ALIASES: Dict[str, str] = {
    "黄阶": "凡品",
    "玄阶": "黄品",
    "地阶": "玄品",
    "天阶": "圣品",
    "珍品": "地品",
    "神品": "仙品",
}


def normalize_rank(rank: Optional[str], default: str = "") -> str:
    """把任意历史品阶名称归一化为统一名称；空值返回 ``default``。"""
    text = str(rank or "").strip()
    if not text:
        return default
    if text in RANK_LEVELS:
        return text
    return RANK_ALIASES.get(text, text)


def is_known_rank(rank: Optional[str]) -> bool:
    return normalize_rank(rank) in RANK_LEVELS


def rank_level(rank: Optional[str], default: int = 0) -> int:
    """返回品阶对应的默认解锁境界索引。"""
    return RANK_LEVELS.get(normalize_rank(rank), default)


def rank_tier(rank: Optional[str], default: int = 1) -> int:
    """返回品阶的稀有度档位（1-5）。"""
    return RANK_TIERS.get(normalize_rank(rank), default)


def level_to_rank(level: int) -> str:
    """按境界索引反查应显示的品阶（取不高于该境界的最高档）。"""
    try:
        value = int(level)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return RANK_NAMES[0]
    result = RANK_NAMES[0]
    for row in RANK_TABLE:
        if value >= int(row["level"]):
            result = str(row["name"])
        else:
            break
    return result


def format_rank(rank: Optional[str], default: str = "未知") -> str:
    """展示用：归一化后返回，无法识别时返回 ``default``。"""
    text = normalize_rank(rank)
    return text if text in RANK_LEVELS else default
