# loan_rules.py
"""贷款额度与欠款身份阈值（方案A）：按押镖日收入 E(L) 动态缩放。

- 可贷额度 = min(硬顶 1000 万, max(保底 10 万, 押镖日收入 × 0.5))
- 欠款身份线 = 押镖日收入 × 0.5 / 1 / 2 天（低境界保底 1万/5万/10万）
"""

DAILY_ESCORT_TRIPS = 48          # 经济基准：24h 全勤 48 趟
LOAN_INCOME_RATIO = 0.5          # 可贷额度 = 日收入的一半
LOAN_FLOOR = 100_000             # 低境界保底额度
LOAN_HARD_CAP = 10_000_000       # 硬顶，防无限膨胀
WARN_DAYS = 0.5                  # 负债修士
HIGH_DAYS = 1.0                  # 欠款大户
BLOCK_DAYS = 2.0                 # 老赖（暂停购买）
WARN_FLOOR = 10_000
HIGH_FLOOR = 50_000
BLOCK_FLOOR = 100_000


# L0~L2 押镖未开放，用历练过渡日收益（见《经济基准表》）
TRANSITION_DAILY_INCOME = {0: 324, 1: 399, 2: 504}


def daily_income(level_index: int) -> int:
    """押镖日收入（名义值）：每趟镖银 × 48 趟（惰性导入避免循环依赖）。"""
    level = int(level_index or 0)
    if level < 3:
        return TRANSITION_DAILY_INCOME.get(level, 324)
    from .managers.escort_manager import EscortManager
    return EscortManager.calculate_escort_gold_base(level) * DAILY_ESCORT_TRIPS


def loan_limit(level_index: int) -> int:
    """当前境界可贷额度。"""
    dynamic = int(daily_income(level_index) * LOAN_INCOME_RATIO)
    return min(LOAN_HARD_CAP, max(LOAN_FLOOR, dynamic))


def debt_thresholds(level_index: int) -> dict:
    """欠款身份阈值：warn / high / block 三档（灵石）。"""
    income = daily_income(level_index)
    return {
        "warn": max(WARN_FLOOR, int(income * WARN_DAYS)),
        "high": max(HIGH_FLOOR, int(income * HIGH_DAYS)),
        "block": max(BLOCK_FLOOR, int(income * BLOCK_DAYS)),
    }


__all__ = [
    "DAILY_ESCORT_TRIPS", "LOAN_INCOME_RATIO", "LOAN_FLOOR", "LOAN_HARD_CAP",
    "WARN_DAYS", "HIGH_DAYS", "BLOCK_DAYS", "WARN_FLOOR", "HIGH_FLOOR", "BLOCK_FLOOR",
    "daily_income", "loan_limit", "debt_thresholds",
]
