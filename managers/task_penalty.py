"""任务放弃惩罚：统一按奖励的 80% 收取，缺失奖励时收固定灵石。"""

from typing import Tuple

ABANDON_RATE = 0.8
DEFAULT_FLAT_PENALTY = 500


def calc_abandon_penalty(gold_reward: int = 0, qi_reward: int = 0) -> Tuple[int, str]:
    """返回 (惩罚数值, 货币类型)。货币类型为 gold 或 qi。"""
    if gold_reward and gold_reward > 0:
        return max(1, int(gold_reward * ABANDON_RATE)), "gold"
    if qi_reward and qi_reward > 0:
        return max(1, int(qi_reward * ABANDON_RATE)), "qi"
    return DEFAULT_FLAT_PENALTY, "gold"


async def charge_abandon_penalty(
    player,
    db,
    cost: int,
    currency: str = "gold",
    commit: bool = True,
) -> Tuple[bool, str]:
    """扣除放弃惩罚；灵石/灵气不足时返回失败。"""
    if cost <= 0:
        return True, ""

    if currency == "qi":
        current = int(getattr(player, "spiritual_qi", 0) or 0)
        if current < cost:
            return False, f"❌ 灵气不足，放弃需要 {cost:,} 灵气（当前 {current:,}）。"
        player.spiritual_qi = current - cost
    else:
        current = int(getattr(player, "gold", 0) or 0)
        if current < cost:
            return False, f"❌ 灵石不足，放弃需要 {cost:,} 灵石（当前 {current:,}）。"
        player.gold = current - cost

    await db.update_player(player, commit=commit)
    return True, ""
