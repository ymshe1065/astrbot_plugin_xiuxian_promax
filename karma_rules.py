# karma_rules.py
"""功德/业力规则：突破修正、行善成本、奇遇功德与历练善举。"""

KARMA_RATE_PER_POINT = 0.01     # 每点功德 ±1% 突破成功率
KARMA_NEGATIVE_FLOOR = -0.50    # 业力最多 -50%
CHARITY_MIN_COST = 2000         # 行善最低捐献
CHARITY_RATE = 0.05             # 行善成本 = 当前资产(灵石+存款) 5%
GOOD_KARMA = 2                  # 奇遇/善举正面功德
EVIL_KARMA = -2                 # 奇遇负面业力
MERIT_CHANCE = 25               # 历练归来触发善举抉择概率(%)
MERIT_RATIO = 0.40              # 善举损失本次收益比例
MERIT_KARMA = GOOD_KARMA        # 善举功德


def charity_cost(gold: int, bank_balance: int = 0) -> int:
    """行善成本：max(2000, 当前灵石与存款合计的 5%)。"""
    try:
        holdings = max(0, int(gold or 0)) + max(0, int(bank_balance or 0))
    except (TypeError, ValueError):
        holdings = 0
    return max(CHARITY_MIN_COST, int(holdings * CHARITY_RATE))


def karma_rate_bonus(karma: int) -> float:
    """功德对突破成功率的修正：正功德无上限，业力最低 -50%。"""
    try:
        value = int(karma or 0)
    except (TypeError, ValueError):
        value = 0
    return max(KARMA_NEGATIVE_FLOOR, value * KARMA_RATE_PER_POINT)


__all__ = [
    "KARMA_RATE_PER_POINT", "KARMA_NEGATIVE_FLOOR", "CHARITY_MIN_COST", "CHARITY_RATE",
    "GOOD_KARMA", "EVIL_KARMA", "MERIT_CHANCE", "MERIT_RATIO", "MERIT_KARMA",
    "charity_cost", "karma_rate_bonus",
]
