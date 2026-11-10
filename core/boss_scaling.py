# core/boss_scaling.py
"""世界Boss / 团本 / 江湖事件共用的难度与伤害上限缩放规则。

三套玩法遵循同一套思路：
- 全服境界系数：最高境界占 70%，平均境界占 30%，按 ``ref_level``（默认
  31 = 渡劫期）归一化后再走指数曲线。指数 > 1 可以压住中低境界服务器的
  高难权重，只有真正出现合体/大乘/渡劫玩家时才会明显抬升。
- 难度权重：低境界时使用 ``weight``，境界系数达到 1 时使用 ``high_weight``，
  中间线性插值。服务器越强，高难内容占比越高。
- 伤害上限：``base + per_level * level_index``，并以 ``cap_max`` 封顶。
  ``cap_max`` 不会低于 ``base``，避免旧配置或手动覆写被意外压缩。
"""

from __future__ import annotations

from typing import Optional, Tuple

DEFAULT_REF_LEVEL = 31
DEFAULT_MAX_LEVEL_WEIGHT = 0.7
DEFAULT_AVG_LEVEL_WEIGHT = 0.3
DEFAULT_CURVE_EXPONENT = 1.4

__all__ = [
    "DEFAULT_REF_LEVEL",
    "DEFAULT_MAX_LEVEL_WEIGHT",
    "DEFAULT_AVG_LEVEL_WEIGHT",
    "DEFAULT_CURVE_EXPONENT",
    "level_factor",
    "realm_factor",
    "scale_weight",
    "scaled_damage_cap",
    "load_server_level_profile",
]


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def level_factor(
    level_index,
    ref_level=DEFAULT_REF_LEVEL,
    curve_exponent=DEFAULT_CURVE_EXPONENT,
) -> float:
    """把单个境界索引映射到 0~1 的系数。"""
    ref = _as_float(ref_level, DEFAULT_REF_LEVEL)
    if ref <= 0:
        return 0.0
    ratio = _clamp(max(0.0, _as_float(level_index)) / ref)
    exponent = _as_float(curve_exponent, DEFAULT_CURVE_EXPONENT)
    if exponent <= 0:
        return 1.0 if ratio > 0 else 0.0
    return ratio ** exponent


def realm_factor(
    max_level,
    avg_level: Optional[float] = None,
    config: Optional[dict] = None,
) -> float:
    """按全服最高/平均境界计算 0~1 的境界系数。

    ``config`` 对应 boss_config.json 顶层的 ``difficulty_scaling``：
    ``ref_level`` / ``max_weight`` / ``avg_weight`` / ``curve_exponent``。
    """
    cfg = config or {}
    ref_level = cfg.get("ref_level", DEFAULT_REF_LEVEL)
    exponent = cfg.get("curve_exponent", DEFAULT_CURVE_EXPONENT)

    max_factor = level_factor(max_level, ref_level, exponent)
    if avg_level is None:
        return max_factor

    avg_factor = level_factor(avg_level, ref_level, exponent)
    max_weight = max(0.0, _as_float(cfg.get("max_weight"), DEFAULT_MAX_LEVEL_WEIGHT))
    avg_weight = max(0.0, _as_float(cfg.get("avg_weight"), DEFAULT_AVG_LEVEL_WEIGHT))
    total = max_weight + avg_weight
    if total <= 0:
        return max_factor
    return _clamp((max_factor * max_weight + avg_factor * avg_weight) / total)


def scale_weight(weight, high_weight, factor) -> float:
    """在基础权重与高境界权重之间按境界系数插值。"""
    base = max(0.0, _as_float(weight, 1.0))
    high = max(0.0, _as_float(high_weight, base))
    return base + (high - base) * _clamp(_as_float(factor))


def scaled_damage_cap(
    base_pct,
    level_index,
    per_level_pct,
    cap_max_pct,
) -> float:
    """单次伤害上限 = 基础值 + 每级增量 × 境界索引，并按上限封顶。"""
    base = max(0.0, _as_float(base_pct))
    per_level = max(0.0, _as_float(per_level_pct))
    level = max(0, int(_as_float(level_index)))
    ceiling = max(base, _as_float(cap_max_pct))
    return min(ceiling, base + per_level * level)


async def load_server_level_profile(db) -> Tuple[int, float]:
    """返回全服 (最高境界索引, 平均境界索引)；读取失败时按空服处理。"""
    try:
        async with db.conn.execute(
            "SELECT MAX(level_index) AS lv, AVG(level_index) AS avg_lv FROM players"
        ) as cursor:
            row = await cursor.fetchone()
    except Exception:
        return 0, 0.0
    if not row:
        return 0, 0.0
    try:
        max_level = int(row["lv"]) if row["lv"] is not None else 0
    except (TypeError, ValueError, IndexError):
        max_level = 0
    try:
        avg_level = float(row["avg_lv"]) if row["avg_lv"] is not None else 0.0
    except (TypeError, ValueError, IndexError):
        avg_level = 0.0
    return max(0, max_level), max(0.0, avg_level)
