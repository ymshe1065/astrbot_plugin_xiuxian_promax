# core/realm_rules.py
"""境界区间规则：把物品门槛换算成「适用境界」区间，供物品信息与商店展示。

设计来源：
- 物品品阶决定一个默认解锁境界（``rank_rules.RANK_LEVELS``）；
- 同一品阶的下一档解锁境界之前，都属于该品阶的完整效率区间
  （装备在此区间内不衰减，落后 3 个境界索引才开始按 ×80% 递减）；
- 物品若显式配置了 ``required_level_index``，以配置值为区间起点。
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

from ..rank_rules import RANK_NAMES, normalize_rank, rank_level

__all__ = [
    "applicable_level_range",
    "format_applicable_realm",
    "level_name_at",
]


def _as_index(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def level_name_at(level_data: Optional[Sequence[dict]], index: int) -> str:
    """按境界索引取名称，越界时给出可读兜底。"""
    if level_data and 0 <= index < len(level_data):
        row = level_data[index]
        if isinstance(row, dict):
            name = str(row.get("level_name", "") or "").strip()
            if name:
                return name
    return f"境界{index}"


def applicable_level_range(
    required_level_index: Any,
    rank: str = "",
    max_level: Optional[int] = None,
) -> Tuple[int, int]:
    """返回物品适用境界的 (起始索引, 结束索引)，闭区间。

    - 起点：显式 ``required_level_index`` 优先，否则取品阶默认解锁境界；
    - 终点：下一品阶默认解锁境界 - 1；已是最高品阶时取最高境界。
    """
    start = _as_index(required_level_index)
    if start is None or start < 0:
        start = rank_level(rank, 0)
    start = max(0, start)

    normalized = normalize_rank(rank)
    end: Optional[int] = None
    if normalized in RANK_NAMES:
        position = RANK_NAMES.index(normalized)
        if position + 1 < len(RANK_NAMES):
            end = rank_level(RANK_NAMES[position + 1]) - 1

    if end is None:
        end = start
    if max_level and max_level > 0:
        end = min(end, max_level - 1)
    end = max(start, end)
    return start, end


def _range_text(level_data: Optional[Sequence[dict]], start: int, end: int) -> str:
    if start == end:
        return level_name_at(level_data, start)
    return f"{level_name_at(level_data, start)}-{level_name_at(level_data, end)}"


def format_applicable_realm(
    required_level_index: Any,
    level_data: Optional[Sequence[dict]],
    body_level_data: Optional[Sequence[dict]] = None,
    rank: str = "",
    include_body: bool = True,
) -> str:
    """格式化「适用境界」文本，例如 ``金丹期初期-金丹期后期``。

    灵修与体修共用同一个 ``level_index``，若两套境界名称不同，会以
    ``（体修：……）`` 的形式补充体修区间，避免体修玩家误判。
    """
    levels = list(level_data or [])
    body_levels = list(body_level_data or [])
    max_level = max(len(levels), len(body_levels))
    start, end = applicable_level_range(required_level_index, rank, max_level or None)
    text = _range_text(levels or None, start, end)

    if include_body and body_levels:
        body_text = _range_text(body_levels, start, end)
        if body_text and body_text != text:
            text = f"{text}（体修：{body_text}）"
    return text
