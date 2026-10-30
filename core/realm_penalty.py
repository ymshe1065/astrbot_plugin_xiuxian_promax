# core/realm_penalty.py
"""境界跌落处罚。

历史版本中突破失败、贷款逾期等场景会直接删除角色数据（即“死亡”）。
现在统一改为：保留角色，仅掉落一个大境界，并同步压缩修为。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..models import Player


def _level_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("level_name", "") or "").strip()
    return str(entry or "").strip()


def _common_prefix_len(left: str, right: str) -> int:
    length = 0
    for a, b in zip(left, right):
        if a != b:
            break
        length += 1
    return length


def major_realm_groups(level_data: Sequence[Any]) -> List[Tuple[int, int]]:
    """按境界名把等级索引切分为大境界区间（闭区间）。

    同一大境界内的等级名共享前缀（如「炼气期一层」「炼气期二层」），
    跨大境界时前缀通常变化（如「炼气期十层」→「筑基期初期」）。
    这样既兼容灵修/体修，也无需硬编码每个境界名。
    """
    if not level_data:
        return []

    groups: List[Tuple[int, int]] = []
    start = 0
    for index in range(1, len(level_data)):
        prev_name = _level_name(level_data[index - 1])
        current_name = _level_name(level_data[index])
        if _common_prefix_len(prev_name, current_name) < 2:
            groups.append((start, index - 1))
            start = index
    groups.append((start, len(level_data) - 1))
    return groups


def major_realm_index(level_data: Sequence[Any], level_index: int) -> int:
    """返回 level_index 所属的大境界序号。"""
    groups = major_realm_groups(level_data)
    for order, (start, end) in enumerate(groups):
        if start <= level_index <= end:
            return order
    return max(0, level_index)


def previous_major_realm_index(level_data: Sequence[Any], level_index: int) -> int:
    """计算掉落一个大境界后的等级索引。

    优先保留玩家在当前大境界中的小境界位置：例如金丹期后期（15）
    掉到筑基期后期（12），而不是直接压回筑基期初期。首个大境界或
    数据异常时回退到当前索引（不再继续下探）。
    """
    if not level_data:
        return max(0, level_index - 1)

    groups = major_realm_groups(level_data)
    if not groups:
        return max(0, level_index - 1)

    order = major_realm_index(level_data, level_index)
    if order <= 0:
        return 0

    current_start, _ = groups[order]
    previous_start, previous_end = groups[order - 1]
    offset = max(0, level_index - current_start)
    return previous_start + min(offset, previous_end - previous_start)


def fall_one_major_realm(
    player: Player,
    config_manager=None,
    level_data: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """让玩家掉落一个大境界，并压缩超出新境界门槛的修为。

    返回本次跌落信息，调用方负责持久化 player。
    """
    if level_data is None and config_manager is not None:
        level_data = config_manager.get_level_data(player.cultivation_type)

    old_index = max(0, int(getattr(player, "level_index", 0) or 0))
    levels = list(level_data or [])
    if levels and 0 <= old_index < len(levels):
        old_name = _level_name(levels[old_index]) or f"境界{old_index}"
    else:
        old_name = f"境界{old_index}"

    if levels and 0 <= old_index < len(levels):
        new_index = previous_major_realm_index(levels, old_index)
    else:
        new_index = max(0, old_index - 1)
    new_index = max(0, min(new_index, old_index))
    player.level_index = new_index

    exp_lost = 0
    if levels and 0 <= new_index < len(levels):
        new_name = _level_name(levels[new_index]) or f"境界{new_index}"
        threshold = int(levels[new_index].get("exp_needed", 0) or 0)
        if player.experience > threshold:
            exp_lost = int(player.experience) - threshold
            player.experience = threshold
    else:
        new_name = f"境界{new_index}"

    return {
        "dropped": new_index < old_index,
        "old_index": old_index,
        "new_index": new_index,
        "old_name": old_name,
        "new_name": new_name,
        "exp_lost": exp_lost,
    }
