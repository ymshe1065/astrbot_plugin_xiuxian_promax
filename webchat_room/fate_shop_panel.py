# -*- coding: utf-8 -*-
"""网页聊天室「仙缘商店」面板：卡片式浏览 + 点一下就买。

网页端不再需要记编号，而是：

- ``GET  /api/fate_shop``：返回仙缘点、八个分组、每件商品的已购层数与是否买得起；
- ``POST /api/fate_shop/buy``：直接买一层，返回最新面板状态；
- ``POST /api/fate_shop/exchange``：灵石换仙缘点（1000 万 / 点，每世 100 点）。

真正的扣点与账本写入仍走 :class:`ReincarnationManager`，与 ``/仙缘兑换`` 共用同一套校验，
避免网页端和聊天端出现两套价格或两套封顶。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..main import XiuXianPlugin

from ..core.reincarnation_rules import (
    FATE_EXCHANGE_MAX,
    GOLD_PER_FATE_POINT,
    SHOP_GROUPS,
    SHOP_ITEMS,
    fate_exchange_key,
    fate_exchange_points,
    item_at_cap,
    item_owned,
    parse_bonus,
    shop_price,
)


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


async def _exchange_used(plugin: "XiuXianPlugin", user_id: str) -> int:
    try:
        raw = await plugin.db.ext.get_system_config(fate_exchange_key(user_id))
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


async def fate_shop_snapshot(plugin: "XiuXianPlugin", user_id: str) -> dict[str, Any]:
    """面板快照（不修改存档）。"""
    player = await plugin.db.get_player_by_id(user_id)
    if not player:
        return {"ok": True, "has_player": False, "message": "尚未创建角色，请先发送 /我要修仙"}

    bonus = parse_bonus(player)
    points = int(getattr(player, "fate_points", 0) or 0)

    groups: list[dict[str, Any]] = []
    for name, note in SHOP_GROUPS:
        items = []
        for item_id, item in SHOP_ITEMS.items():
            if item.get("group") != name:
                continue
            owned = item_owned(bonus, item_id)
            cap = int(item.get("cap") or 0)
            price = shop_price(item_id, owned)
            items.append(
                {
                    "id": item_id,
                    "name": item["name"],
                    "desc": item["desc"],
                    "cost": int(price or 0),
                    "unit": item.get("unit", ""),
                    "owned": owned,
                    "cap": cap,
                    "maxed": bool(cap and owned >= cap),
                    "affordable": bool(price) and points >= int(price),
                }
            )
        if items:
            groups.append({"name": name, "note": note, "items": items})

    used = await _exchange_used(plugin, user_id)
    gold = int(getattr(player, "gold", 0) or 0)
    return {
        "ok": True,
        "has_player": True,
        "points": points,
        "groups": groups,
        "gold": gold,
        "exchange": {
            "gold_per_point": GOLD_PER_FATE_POINT,
            "used": used,
            "max": FATE_EXCHANGE_MAX,
            "left": max(0, FATE_EXCHANGE_MAX - used),
            "affordable": min(FATE_EXCHANGE_MAX - used, fate_exchange_points(gold)),
        },
    }


async def fate_shop_buy(plugin: "XiuXianPlugin", user_id: str, item_id: int) -> dict[str, Any]:
    """买一层，返回 (ok, msg) + 最新快照。"""
    player = await plugin.db.get_player_by_id(user_id)
    if not player:
        return {"ok": False, "error": "尚未创建角色，请先发送 /我要修仙"}

    mgr = getattr(plugin, "reincarnation_mgr", None)
    if mgr is None:
        return {"ok": False, "error": "仙缘商店模块未加载"}

    ok, message = await mgr.buy_fate_shop_item(player, int(item_id))
    snap = await fate_shop_snapshot(plugin, user_id)
    snap["ok"] = bool(ok)
    snap["message"] = _clean(message)
    return snap


async def fate_shop_exchange(
    plugin: "XiuXianPlugin", user_id: str, points: int = 0
) -> dict[str, Any]:
    """灵石换仙缘点：points<=0 时按余额与剩余额度换最大值。"""
    player = await plugin.db.get_player_by_id(user_id)
    if not player:
        return {"ok": False, "error": "尚未创建角色，请先发送 /我要修仙"}

    used = await _exchange_used(plugin, user_id)
    left = max(0, FATE_EXCHANGE_MAX - used)
    if left <= 0:
        snap = await fate_shop_snapshot(plugin, user_id)
        snap.update({"ok": False, "message": f"本轮已换满 {FATE_EXCHANGE_MAX} 点，等下世再换"})
        return snap

    want = int(points or 0)
    if want <= 0:
        want = min(left, fate_exchange_points(int(getattr(player, "gold", 0) or 0)))
    want = min(want, left)
    if want <= 0:
        snap = await fate_shop_snapshot(plugin, user_id)
        snap.update(
            {
                "ok": False,
                "message": f"灵石不足：{GOLD_PER_FATE_POINT // 10000}万灵石换 1 点",
            }
        )
        return snap

    cost = want * GOLD_PER_FATE_POINT
    gold = int(getattr(player, "gold", 0) or 0)
    if gold < cost:
        snap = await fate_shop_snapshot(plugin, user_id)
        snap.update(
            {
                "ok": False,
                "message": f"灵石不足：换 {want} 点需要 {cost // 10000} 万，当前 {gold // 10000} 万",
            }
        )
        return snap

    player.gold = gold - cost
    player.fate_points = int(getattr(player, "fate_points", 0) or 0) + want
    await plugin.db.update_player(player)
    try:
        await plugin.db.ext.set_system_config(fate_exchange_key(user_id), str(used + want))
    except Exception:
        pass

    snap = await fate_shop_snapshot(plugin, user_id)
    snap.update(
        {
            "ok": True,
            "message": f"消耗 {cost // 10000} 万灵石，兑换 {want} 仙缘点（本轮剩 {left - want} 点额度）",
        }
    )
    return snap
