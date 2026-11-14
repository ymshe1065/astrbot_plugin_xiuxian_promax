# -*- coding: utf-8 -*-
"""身外化身：活动槽位规则。

一个槽位就是一个分身
--------------------
0 号槽永远是本体，天生可用；仙缘商店的【身外化身】依次解锁 1~4 号槽。
每个槽位最多挂一项耗时活动（历练 / 秘境 / 押镖 / 宗门任务 / 闭关 …），
所以「还能不能干点别的」等价于「还有没有空闲槽位」：

    空闲槽位 = total_slots(player) - busy_slot_count(user_id)

杂务（炼丹、逛坊市、逛百宝阁…）本身不占槽位、瞬间完成，但只要所有分身都出动了
就没人去做 —— 这就是「忙碌时无法分心他顾」的由来，也是身外化身的价值所在。

定价
----
阶梯价，每买一尊涨价：60 / 120 / 240 / 480（四尊满，合计 900 仙缘点）。
"""

from typing import Any, Optional, Tuple

from .reincarnation_rules import item_owned, parse_bonus

# 对应 reincarnation_rules.SHOP_ITEMS 里的【身外化身】
AVATAR_ITEM_ID = 52

# 最多再添 4 尊化身 = 本体 + 4 = 5 个槽位
MAX_AVATARS = 4

# 阶梯价：第 1 尊 60、第 2 尊 120 …… 按「已拥有的尊数」取下标
AVATAR_PRICES: Tuple[int, ...] = (60, 120, 240, 480)

SLOT_NAMES = ("本体", "化身·一", "化身·二", "化身·三", "化身·四")


def avatar_price_at(owned: int) -> Optional[int]:
    """已买 ``owned`` 尊时，下一尊的价钱；买满返回 None。"""
    try:
        idx = int(owned)
    except (TypeError, ValueError):
        return None
    if idx < 0 or idx >= len(AVATAR_PRICES):
        return None
    return AVATAR_PRICES[idx]


def avatar_count(player: Any) -> int:
    """已拥有的身外化身数量（不含本体）。"""
    try:
        return max(0, min(MAX_AVATARS, int(item_owned(parse_bonus(player), AVATAR_ITEM_ID))))
    except Exception:
        return 0


def total_slots(player: Any) -> int:
    """可用活动槽位总数 = 本体 1 + 化身 N。"""
    return 1 + avatar_count(player)


def next_avatar_price(player: Any) -> Optional[int]:
    """下一尊化身的价钱；已买满返回 None。"""
    return avatar_price_at(avatar_count(player))


def is_avatar_maxed(player: Any) -> bool:
    return avatar_count(player) >= MAX_AVATARS


def slot_label(slot: int) -> str:
    """槽位展示名：0 → 本体，N → 化身·N。"""
    try:
        idx = int(slot)
    except (TypeError, ValueError):
        return "分身"
    if 0 <= idx < len(SLOT_NAMES):
        return SLOT_NAMES[idx]
    return f"化身·{idx}"


def slot_summary(total: int, busy: int) -> str:
    """「分身 2/3 在忙」这类摘要。"""
    total = max(1, int(total or 1))
    busy = max(0, min(total, int(busy or 0)))
    return f"{busy}/{total} 在忙（空闲 {total - busy}）"


async def ensure_free_slot(db: Any, player: Any, user_id: str) -> Tuple[bool, str]:
    """还有空闲分身就返回 ``(True, "")``，否则返回 ``(False, 提示文案)``。

    各活动管理器在「开始一项耗时活动」前统一调这里，保证不会因为分身不够
    而悄悄占用到不存在的槽位。
    """
    from .parallel_rules import no_slot_hint

    total = total_slots(player)
    busy = await db.ext.busy_slot_count(user_id)
    if busy < total:
        return True, ""
    names = await db.ext.busy_activity_names(user_id)
    return False, no_slot_hint(total, busy, names, maxed=is_avatar_maxed(player))
