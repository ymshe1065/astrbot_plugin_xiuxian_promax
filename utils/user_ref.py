# -*- coding: utf-8 -*-
"""目标玩家解析：兼容 QQ 数字 ID、网页端 web_<账号> 与道号。

网页聊天室的用户 ID 形如 ``web_<账号>``，不是纯数字。旧逻辑只接受
``isdigit()``，导致候选列表里点出来的网页玩家无法决斗/双修/赠予/宗门操作。
这里统一成一套解析：先按 ID 查，查不到再按道号查。
"""
from __future__ import annotations

from typing import Any


def looks_like_user_id(token: str) -> bool:
    """是否像平台用户 ID（QQ 数字或 webroom 的 web_ 前缀）。"""
    text = str(token or "").strip().lstrip("@").strip()
    return bool(text) and (text.startswith("web_") or text.isdigit())


async def resolve_user_id(db: Any, token: str) -> str:
    """把 ID 或道号解析成玩家 user_id；解析不到返回空字符串。"""
    text = str(token or "").strip().lstrip("@").strip()
    if not text:
        return ""

    if looks_like_user_id(text):
        try:
            player = await db.get_player_by_id(text)
        except Exception:
            player = None
        # ID 形式即使查不到也原样返回，交给调用方给出「尚未修仙」提示
        return str(getattr(player, "user_id", "") or text) if player else text

    try:
        player = await db.get_player_by_name(text)
    except Exception:
        player = None
    if player:
        return str(getattr(player, "user_id", "") or "")
    return ""
