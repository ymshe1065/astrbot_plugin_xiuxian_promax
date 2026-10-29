# core/equipment_drop.py
"""秘境/历练共用的装备掉落辅助。

规则：
- 装备每次只掉 1 件；
- 部位按权重随机；
- 品阶以玩家境界为基准 ±1 阶，中间偏置；
- 区间内无可用装备时向最近品阶扩展；
- 掉落实例时 roll 品质，保持与商店/拍卖一致。
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional

from ..rank_rules import RANK_NAMES, level_to_rank, normalize_rank
from .item_rules import ItemRules

__all__ = [
    "DEFAULT_SLOT_WEIGHTS",
    "build_equipment_pool",
    "roll_equipment_drop",
]

DEFAULT_SLOT_WEIGHTS: Dict[str, int] = {
    "weapon": 16,
    "armor": 16,
    "head": 12,
    "boots": 12,
    "amulet": 12,
    "gloves": 16,
    "ring": 16,
}

_SLOT_ALIASES = {"ring1": "ring", "ring2": "ring", "body": "armor"}


def _slot_of(config: dict) -> str:
    if not isinstance(config, dict):
        return ""
    equip_type = ItemRules.normalize_equipment_type(config)
    if equip_type not in ItemRules.GEAR_TYPES:
        return ""
    slot = ItemRules.get_equip_slot(config)
    return _SLOT_ALIASES.get(slot, slot)


def _rank_index(rank: str) -> Optional[int]:
    normalized = normalize_rank(rank)
    try:
        return RANK_NAMES.index(normalized)
    except ValueError:
        return None


def build_equipment_pool(config_manager) -> Dict[str, List[dict]]:
    """按部位整理可掉落装备池，条目为 {"name", "config"}。"""
    pool: Dict[str, List[dict]] = {}
    sources = (
        getattr(config_manager, "items_data", {}) or {},
        getattr(config_manager, "weapons_data", {}) or {},
    )
    for source in sources:
        for name, config in source.items():
            if not isinstance(config, dict):
                continue
            slot = _slot_of(config)
            if not slot or slot not in DEFAULT_SLOT_WEIGHTS:
                continue
            if int(config.get("price", 0) or 0) <= 0:
                continue
            if _rank_index(config.get("rank", "")) is None:
                continue
            pool.setdefault(slot, []).append({"name": name, "config": config})
    return pool


def _weighted_choice(entries: List[dict], weight_of, rng: random.Random) -> Optional[dict]:
    if not entries:
        return None
    weights = [max(0.0, float(weight_of(entry) or 0)) for entry in entries]
    if sum(weights) <= 0:
        return rng.choice(entries)
    return rng.choices(entries, weights=weights, k=1)[0]


def _pick_rank_index(base: int, rng: random.Random) -> List[int]:
    """基准 ±1 阶，中间权重高；返回按权重抽取顺序的候选索引。"""
    indices = [base - 1, base, base + 1]
    weights = [1.0, 3.0, 1.0]
    valid = [(i, w) for i, w in zip(indices, weights) if 0 <= i < len(RANK_NAMES)]
    if not valid:
        return []
    total = sum(w for _, w in valid)
    roll = rng.uniform(0, total)
    picked = valid[-1][0]
    cumulative = 0.0
    for index, weight in valid:
        cumulative += weight
        if roll <= cumulative:
            picked = index
            break
    ordered = [picked] + [i for i, _ in sorted(valid, key=lambda item: abs(item[0] - base)) if i != picked]
    # 去重并保持顺序
    result = []
    for index in ordered:
        if index not in result:
            result.append(index)
    return result


def roll_equipment_drop(
    config_manager,
    player_level_index: int,
    slot_weights: Optional[Dict[str, int]] = None,
    rng: Optional[random.Random] = None,
) -> Optional[dict]:
    """返回 {"name", "quality", "slot", "rank"} 或 None。"""
    rng = rng or random
    pool = build_equipment_pool(config_manager)
    weights = dict(DEFAULT_SLOT_WEIGHTS)
    if isinstance(slot_weights, dict):
        for slot, weight in slot_weights.items():
            key = _SLOT_ALIASES.get(str(slot), str(slot))
            if key in weights:
                try:
                    weights[key] = max(0, int(weight))
                except (TypeError, ValueError):
                    continue
    usable_slots = [slot for slot in weights if weights[slot] > 0 and pool.get(slot)]
    if not usable_slots:
        return None
    chosen_slot = rng.choices(usable_slots, weights=[weights[s] for s in usable_slots], k=1)[0]

    base_rank = level_to_rank(max(0, int(player_level_index or 0)))
    base_index = _rank_index(base_rank)
    if base_index is None:
        base_index = 0

    slot_entries = pool.get(chosen_slot, [])
    rank_candidates = _pick_rank_index(base_index, rng)
    by_rank: Dict[int, List[dict]] = {}
    for entry in slot_entries:
        index = _rank_index(entry["config"].get("rank", ""))
        if index is not None:
            by_rank.setdefault(index, []).append(entry)

    picked_entry = None
    for index in rank_candidates:
        if by_rank.get(index):
            picked_entry = _weighted_choice(
                by_rank[index],
                lambda item: int(item["config"].get("drop_weight")
                                 or item["config"].get("shop_weight") or 1),
                rng,
            )
            break
    if picked_entry is None:
        # 保底：向最近品阶扩展，找到该部位最近的可用装备。
        nearest = min(by_rank, key=lambda index: abs(index - base_index)) if by_rank else None
        if nearest is None:
            return None
        picked_entry = _weighted_choice(
            by_rank[nearest],
            lambda item: int(item["config"].get("drop_weight")
                             or item["config"].get("shop_weight") or 1),
            rng,
        )
    if not picked_entry:
        return None

    from .item_instance_manager import ItemInstanceManager
    config = picked_entry["config"]
    return {
        "name": picked_entry["name"],
        "quality": ItemInstanceManager.roll_quality(),
        "slot": chosen_slot,
        "rank": normalize_rank(config.get("rank"), base_rank),
    }
