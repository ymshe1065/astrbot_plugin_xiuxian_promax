# -*- coding: utf-8 -*-
"""忙碌拦截：还有没有空闲分身可用。

v3.45.12 起「并行」不再靠买档位解锁指令清单，而是**活动槽位**：
本体 + 身外化身各占一个槽，每个槽最多挂一项耗时活动。杂务本身不占槽、
瞬间做完，但总得有人手 —— 所以判定只看一件事：

    空闲槽位 > 0  → 杂务放行
    空闲槽位 = 0  → 拦下，提示去仙缘商店请【身外化身】

只有「重开人生」类指令永远禁止：它们会把槽位记录与人物数据弄错位。
"""

import re
from typing import Any, Iterable, Tuple

# 永远禁止（与分身多少无关）
RESET_FORBIDDEN: Tuple[str, ...] = (
    "我要修仙",
    "弃道重修",
    "飞升",
    "重启修仙",
)


def clean_command(text: str) -> str:
    """归一化成纯指令文本（去 @、空白、前缀斜杠，与 handlers.utils 口径一致）。"""
    clean = (text or "").strip()
    clean = re.sub(r"^(?:\[[^\]]*\]\s*)+", "", clean)
    clean = re.sub(r"^@\S+\s*", "", clean)
    clean = re.sub(r"^[^\w\u4e00-\u9fa5]+", "", clean)
    return clean.strip()


def _matches(clean_text: str, command: str) -> bool:
    return (
        clean_text == command
        or clean_text.startswith(command + " ")
        or clean_text.startswith(command + "\u3000")
    )


def is_reset_command(command_text: str) -> bool:
    clean = clean_command(command_text)
    return bool(clean) and any(_matches(clean, cmd) for cmd in RESET_FORBIDDEN)


def free_slots(total_slots: int, busy_slots: int) -> int:
    """还有几个分身没出动。"""
    try:
        total = max(1, int(total_slots or 1))
    except (TypeError, ValueError):
        total = 1
    try:
        busy = max(0, int(busy_slots or 0))
    except (TypeError, ValueError):
        busy = 0
    return max(0, total - busy)


def busy_names_text(names: Iterable[str]) -> str:
    """把正在忙的活动名拼成「历练中、押镖中」。"""
    items = [str(n) for n in (names or []) if n]
    return "、".join(items) if items else "某项活动"


def no_slot_hint(
    total_slots: int, busy_slots: int, names: Iterable[str], maxed: bool = False
) -> str:
    """所有分身都出动时的提示。

    ``maxed=True`` 表示化身已请满 4 尊，此时再劝人去仙缘商店是空话，
    改成提示先收掉一项活动。
    """
    total = max(1, int(total_slots or 1))
    busy = max(0, int(busy_slots or 0))
    if maxed:
        tip = (
            "💡 化身已经请满了，先收掉一项活动（用对应的完成/退出指令）腾个槽位，"
            "或等它自己跑完。"
        )
    else:
        tip = "💡 去 /仙缘商店 请一尊【身外化身】，多一个空的槽位就能同时再干一件事。"
    return (
        f"❌ 分身都出去了（{busy}/{total} 在忙：{busy_names_text(names)}），"
        "没人手做别的。\n" + tip
    )


def can_act_while_busy(
    player: Any, command_text: str, total_slots: int, busy_slots: int
) -> Tuple[bool, str]:
    """忙碌状态下这条指令能不能执行。

    返回 ``(是否放行, 提示)``；提示为空表示沿用「无法分心他顾」的原文案。
    """
    clean = clean_command(command_text)
    if is_reset_command(clean):
        return False, (
            "⛔ 重开人生类指令在活动进行中不可用 —— "
            "分身账本与人物数据会错位，硬开会丢进度。\n"
            "💡 先把在跑的活动收完再做。"
        )
    if free_slots(total_slots, busy_slots) > 0:
        return True, ""
    return False, ""
