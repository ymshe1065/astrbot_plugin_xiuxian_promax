# -*- coding: utf-8 -*-
"""网页聊天室「仙市投资代理人」面板。

- ``GET  /api/invest/agents``：代理人列表 + 当前委托 + 待领取 + 历史；
- ``POST /api/invest/agents/start``：雇佣，body = agent / principal / duration；
- ``POST /api/invest/agents/stop``：提前停止；
- ``POST /api/invest/agents/claim``：领取结算。

数值与校验全部走 :class:`InvestAgentManager`，网页端不复制第二套规则。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..main import XiuXianPlugin


def _agent_mgr(plugin: "XiuXianPlugin"):
    return getattr(plugin, "invest_agent_mgr", None)


def _clean(message: str, limit: int = 160) -> str:
    """把管理器返回的多行文案压成提示条能放的一两行。"""
    parts: list[str] = []
    for line in str(message or "").splitlines():
        text = line.strip()
        if not text or set(text) <= {"━", "-", " ", "─"}:
            continue
        parts.append(text)
        if len(parts) >= 2:
            break
    return " / ".join(parts)[:limit]


async def invest_agent_snapshot(plugin: "XiuXianPlugin", user_id: str) -> dict[str, Any]:
    """面板快照（只推进结算，不改变持仓策略）。"""
    player = await plugin.db.get_player_by_id(user_id)
    if not player:
        return {"ok": True, "has_player": False, "message": "尚未创建角色，请先发送 /我要修仙"}
    mgr = _agent_mgr(plugin)
    if mgr is None:
        return {"ok": False, "error": "仙市代理人模块未加载"}
    snap = await mgr.get_snapshot(user_id)
    snap["has_player"] = True
    snap["gold"] = int(getattr(player, "gold", 0) or 0)
    return snap


async def invest_agent_start(
    plugin: "XiuXianPlugin", user_id: str, agent_key: str, principal: int, duration: int
) -> dict[str, Any]:
    """雇佣代理人；与聊天端 /仙市 代理 雇佣 共用玩家锁与同一套校验。"""
    from ..handlers.utils import get_player_lock

    mgr = _agent_mgr(plugin)
    if mgr is None:
        return {"ok": False, "error": "仙市代理人模块未加载"}
    async with get_player_lock(user_id):
        player = await plugin.db.get_player_by_id(user_id)
        if not player:
            return {"ok": False, "error": "尚未创建角色，请先发送 /我要修仙"}
        ok, message = await mgr.start(player, agent_key, int(principal), int(duration))
        snap = await invest_agent_snapshot(plugin, user_id)
    snap["ok"] = bool(ok)
    snap["message"] = _clean(message)
    return snap


async def invest_agent_stop(plugin: "XiuXianPlugin", user_id: str) -> dict[str, Any]:
    from ..handlers.utils import get_player_lock

    mgr = _agent_mgr(plugin)
    if mgr is None:
        return {"ok": False, "error": "仙市代理人模块未加载"}
    async with get_player_lock(user_id):
        player = await plugin.db.get_player_by_id(user_id)
        if not player:
            return {"ok": False, "error": "尚未创建角色，请先发送 /我要修仙"}
        ok, message = await mgr.stop(player)
        snap = await invest_agent_snapshot(plugin, user_id)
    snap["ok"] = bool(ok)
    snap["message"] = _clean(message)
    return snap


async def invest_agent_claim(plugin: "XiuXianPlugin", user_id: str) -> dict[str, Any]:
    from ..handlers.utils import get_player_lock

    mgr = _agent_mgr(plugin)
    if mgr is None:
        return {"ok": False, "error": "仙市代理人模块未加载"}
    async with get_player_lock(user_id):
        player = await plugin.db.get_player_by_id(user_id)
        if not player:
            return {"ok": False, "error": "尚未创建角色，请先发送 /我要修仙"}
        ok, message = await mgr.claim(player)
        snap = await invest_agent_snapshot(plugin, user_id)
    snap["ok"] = bool(ok)
    snap["message"] = _clean(message)
    return snap
