# -*- coding: utf-8 -*-
"""秘境深档（三重）的装备门槛。

三重的设计意图是「同一处秘境，准备更充分才敢往深处走」。只靠境界门槛拉不
开差距：等级一到，穿一身凡品也照样能进，深档就只剩「打得更久」。这里补一
个可验证的准备度门槛——**八格装备全部达到该秘境门槛境界可装备的最高品阶**。

为什么不用战力
--------------
战力 = 气血/10 + 攻击 + 防御 + 精神力/10，其中 93% 来自境界基础属性，整套
装备只占 6%。按「全套战力」卡，实际检查的是「格子有没有穿满」，少穿一件就
过不去；称号（+10% 攻击）、永久丹（+30% 攻击）还能各自绕过。装备品阶则是
玩家可以主动准备的，也不受这些系统干扰。

为什么按秘境门槛算、不按玩家等级算
----------------------------------
lv35 玩家回刷青云三重，要求应是玄品（青云门槛 9 级可装备的最高品阶），而
不是混元先天——否则低阶秘境会变成永远进不去的死档。
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from ..rank_rules import RANK_LEVELS, level_to_rank, normalize_rank

__all__ = [
    "SLOT_ORDER",
    "SLOT_NAMES",
    "required_gear_rank",
    "equipped_ranks",
    "check",
    "describe",
]

# 与 ItemRules.SLOT_LABELS 同一套槽位；这里固定顺序，提示文案才不会乱跳
SLOT_ORDER = ("weapon", "head", "armor", "gloves", "boots", "ring1", "ring2", "amulet")
SLOT_NAMES = {
    "weapon": "武器",
    "head": "头饰",
    "armor": "甲胄",
    "gloves": "手套",
    "boots": "靴子",
    "ring1": "戒指1",
    "ring2": "戒指2",
    "amulet": "护符",
}


def required_gear_rank(required_level) -> str:
    """某境界可装备的最高品阶（与装备解锁表同源）。"""
    try:
        level = max(0, int(required_level or 0))
    except (TypeError, ValueError):
        level = 0
    return str(level_to_rank(level))


def _rank_score(rank) -> int:
    """品阶序号：用解锁等级当序，比 RANK_TIERS（1~5）细。未知品阶记 -1。"""
    return int(RANK_LEVELS.get(str(normalize_rank(rank or "")), -1))


def _item_rank(inst: dict, config_manager) -> str:
    name = str((inst or {}).get("name") or "")
    if config_manager and name:
        for attr in ("items_data", "weapons_data"):
            table = getattr(config_manager, attr, None) or {}
            cfg = table.get(name)
            if isinstance(cfg, dict) and cfg.get("rank"):
                return str(cfg["rank"])
    return str((inst or {}).get("rank") or "")


def equipped_ranks(player, config_manager) -> Dict[str, str]:
    """{槽位: 品阶}：以 player.equipped_instances 的槽位映射为准。"""
    if player is None:
        return {}
    try:
        mapping = player.get_equipped_instances() or {}
    except Exception:
        mapping = {}
    instances: Dict[int, dict] = {}
    try:
        for inst in player.get_equipment_instances() or []:
            if isinstance(inst, dict):
                instances[int(inst.get("id", 0) or 0)] = inst
    except Exception:
        instances = {}

    result: Dict[str, str] = {}
    for slot in SLOT_ORDER:
        try:
            iid = int(mapping.get(slot, 0) or 0)
        except (TypeError, ValueError):
            iid = 0
        if not iid:
            continue
        inst = instances.get(iid)
        if not inst:
            continue
        result[slot] = _item_rank(inst, config_manager)
    return result


def check(player, required_level, config_manager) -> Tuple[bool, Dict]:
    """返回 (是否达标, 明细)。

    明细键：need（要求品阶）、empty / low（槽位键）、empty_text / low_text
    （可直接展示的中文，low 带实际品阶）。
    """
    need = required_gear_rank(required_level)
    need_score = _rank_score(need)
    ranks = equipped_ranks(player, config_manager)
    empty = [slot for slot in SLOT_ORDER if not ranks.get(slot)]
    low = [
        slot
        for slot in SLOT_ORDER
        if ranks.get(slot) and _rank_score(ranks[slot]) < need_score
    ]
    detail = {
        "need": need,
        "empty": empty,
        "low": low,
        "empty_text": "、".join(SLOT_NAMES.get(s, s) for s in empty),
        "low_text": "、".join(
            f"{SLOT_NAMES.get(s, s)}({ranks.get(s) or '?'})" for s in low
        ),
    }
    return (not empty and not low), detail


def describe(detail: Dict, limit: int = 3) -> str:
    """把明细翻成一句人话，够短才能塞进列表行。"""
    need = str((detail or {}).get("need") or "")
    empty = list((detail or {}).get("empty") or [])
    low = list((detail or {}).get("low") or [])
    parts = []
    if empty:
        parts.append("缺" + "、".join(SLOT_NAMES.get(s, s) for s in empty[:limit]))
        if len(empty) > limit:
            parts[-1] += f" 等 {len(empty)} 格"
    if low:
        parts.append("品阶未达" + "、".join(SLOT_NAMES.get(s, s) for s in low[:limit]))
        if len(low) > limit:
            parts[-1] += f" 等 {len(low)} 格"
    head = f"需{need}全套" if need else "需全套装备"
    return f"{head}（{'；'.join(parts)}）" if parts else head
