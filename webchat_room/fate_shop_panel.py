# -*- coding: utf-8 -*-
"""网页聊天室「仙缘商店」面板：卡片式浏览 + 点一下就买。

网页端不再需要记编号，而是：

- ``GET  /api/fate_shop``：返回仙缘点、八个分组、每件商品的已购层数与是否买得起；
- ``POST /api/fate_shop/buy``：直接买一层，返回最新面板状态；
- ``POST /api/fate_shop/exchange``：仙缘祭祀（1000 万 / 次，小概率成功）。

真正的扣点与账本写入仍走 :class:`ReincarnationManager`，与 ``/仙缘兑换`` 共用同一套校验，
避免网页端和聊天端出现两套价格或两套封顶。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..main import XiuXianPlugin

try:  # 正式环境：插件以包加载，与管理器共用同一套规则常量
    from ..core.reincarnation_rules import (
        FATE_EXCHANGE_MAX,
        FATE_RITUAL_BASE_RATE,
        GOLD_PER_FATE_POINT,
        SHOP_GROUPS,
        SHOP_ITEMS,
        fate_exchange_key,
        fate_exchange_points,
        fate_ritual_rate,
        format_ritual_rate,
        item_at_cap,
        item_owned,
        parse_bonus,
        perform_fate_ritual,
        shop_price,
    )
except ImportError:  # pragma: no cover - 单元测试以顶层包导入时的回退
    import importlib.util as _ilu
    import sys as _sys
    from pathlib import Path as _Path

    _MOD_NAME = "core.reincarnation_rules"
    # 优先复用已注册模块，保证与 core 包内导入的规则身份一致
    _mod = _sys.modules.get(_MOD_NAME)
    if _mod is None:
        _spec = _ilu.spec_from_file_location(
            _MOD_NAME,
            _Path(__file__).resolve().parents[1] / "core" / "reincarnation_rules.py",
        )
        _mod = _ilu.module_from_spec(_spec)
        _sys.modules[_MOD_NAME] = _mod
        _spec.loader.exec_module(_mod)
    FATE_EXCHANGE_MAX = _mod.FATE_EXCHANGE_MAX
    FATE_RITUAL_BASE_RATE = _mod.FATE_RITUAL_BASE_RATE
    GOLD_PER_FATE_POINT = _mod.GOLD_PER_FATE_POINT
    SHOP_GROUPS = _mod.SHOP_GROUPS
    SHOP_ITEMS = _mod.SHOP_ITEMS
    fate_exchange_key = _mod.fate_exchange_key
    fate_exchange_points = _mod.fate_exchange_points
    fate_ritual_rate = _mod.fate_ritual_rate
    format_ritual_rate = _mod.format_ritual_rate
    item_at_cap = _mod.item_at_cap
    item_owned = _mod.item_owned
    parse_bonus = _mod.parse_bonus
    perform_fate_ritual = _mod.perform_fate_ritual
    shop_price = _mod.shop_price


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
            maxed = bool(cap and owned >= cap)
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
                    "maxed": maxed,
                    # 已满的项即使点数够也不能买，价格栏置灰靠 maxed，这里别把它标成「买得起」
                    "affordable": (not maxed) and bool(price) and points >= int(price),
                }
            )
        if items:
            groups.append({"name": name, "note": note, "items": items})

    used = await _exchange_used(plugin, user_id)
    gold = int(getattr(player, "gold", 0) or 0)
    cfg = getattr(_reinc_mgr(plugin), "config_manager", None)
    ritual = fate_ritual_rate(player, cfg)
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
            "base_rate": FATE_RITUAL_BASE_RATE,
            "rate": float(ritual["rate"]),
            "rate_text": format_ritual_rate(ritual),
        },
    }


def _reinc_mgr(plugin: "XiuXianPlugin"):
    """取转生管理器。

    生产里插件上的属性叫 ``reinc_mgr``（``main.py`` 的 ``initialize``），
    早先这里写的是 ``reincarnation_mgr``，取不到就当成「模块未加载」，
    于是网页点卡片购买一直失败——列表能看（不依赖管理器），一买就报错。
    三个来源都试一遍，别再因为改名把购买通路打断。
    """
    mgr = getattr(plugin, "reinc_mgr", None)
    if mgr is None:
        mgr = getattr(plugin, "reincarnation_mgr", None)
    if mgr is None:
        handlers = getattr(plugin, "reinc_handlers", None)
        mgr = getattr(handlers, "reinc_mgr", None)
    return mgr


async def fate_shop_buy(plugin: "XiuXianPlugin", user_id: str, item_id: int) -> dict[str, Any]:
    """买一层，返回 (ok, msg) + 最新快照。"""
    mgr = _reinc_mgr(plugin)
    if mgr is None:
        return {"ok": False, "error": "仙缘商店模块未加载"}

    # 与聊天端 /仙缘兑换 共用同一把玩家锁：否则网页买一层时，
    # 另一端并发写永久账本会用旧快照整行覆盖，出现“扣了点却没记层数”。
    from ..handlers.utils import get_player_lock

    async with get_player_lock(user_id):
        player = await plugin.db.get_player_by_id(user_id)
        if not player:
            return {"ok": False, "error": "尚未创建角色，请先发送 /我要修仙"}

        ok, message = await mgr.buy_fate_shop_item(player, int(item_id))
        snap = await fate_shop_snapshot(plugin, user_id)
    snap["ok"] = bool(ok)
    snap["message"] = _clean(message)
    return snap


async def fate_shop_exchange(
    plugin: "XiuXianPlugin", user_id: str, points: int = 0
) -> dict[str, Any]:
    """仙缘祭祀：points<=0 时按余额与剩余额度上供最大值。"""
    from ..handlers.utils import get_player_lock

    async with get_player_lock(user_id):
        # 锁内重新读档：额度（system_config）与灵石都必须是最新值，
        # 否则网页与聊天端同时兑换会各自按旧额度发点，突破每世上限。
        player = await plugin.db.get_player_by_id(user_id)
        if not player:
            return {"ok": False, "error": "尚未创建角色，请先发送 /我要修仙"}

        used = await _exchange_used(plugin, user_id)
        left = max(0, FATE_EXCHANGE_MAX - used)
        if left <= 0:
            snap = await fate_shop_snapshot(plugin, user_id)
            snap.update({"ok": False, "message": f"本轮已积满 {FATE_EXCHANGE_MAX} 点，等下世再祭"})
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
                    "message": f"灵石不足：祭祀 {want} 次需要 {cost // 10000} 万，当前 {gold // 10000} 万",
                }
            )
            return snap

        result = perform_fate_ritual(
            player, want, getattr(_reinc_mgr(plugin), "config_manager", None)
        )
        success = int(result["success"])

        # 与聊天端一致：祭品一旦上供就不退，失败只是没换到点。
        player.gold = gold - cost
        if success:
            player.fate_points = int(getattr(player, "fate_points", 0) or 0) + success
        await plugin.db.update_player(player)
        if success:
            try:
                await plugin.db.ext.set_system_config(
                    fate_exchange_key(user_id), str(used + success)
                )
            except Exception:
                pass

        snap = await fate_shop_snapshot(plugin, user_id)
    rate = float(result["rate"]) * 100
    if success:
        message = (
            f"祭祀 {want} 次成功 {success} 次，获得 {success} 仙缘点"
            f"（耗 {cost // 10000} 万灵石，成功率 {rate:.1f}%）"
        )
    else:
        message = (
            f"祭祀 {want} 次天道未应，未获得仙缘点"
            f"（耗 {cost // 10000} 万灵石，成功率 {rate:.1f}%）"
        )
    snap.update({"ok": True, "message": message})
    return snap
