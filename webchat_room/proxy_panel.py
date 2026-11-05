# -*- coding: utf-8 -*-
"""网页聊天室「自动代理」面板：读取开关状态，并支持单项 / 总开关切换。

网页端不再需要记住 ``/自动代理 押镖`` 之类的参数，而是：

- ``GET  /api/proxy``：返回押镖、宗门任务、灵田、秘境、施肥的布尔状态与详情；
- ``POST /api/proxy/toggle``：切换单项，或 ``task=all`` 一键开启 / 关闭全部。

真正的开启/关闭仍走 :class:`AutoProxyManager`，与聊天指令共用同一套校验
（灵田是否开垦、秘境境界是否足够、押镖/宗门任务是否互斥等），避免两套逻辑。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

try:  # 正式环境与管理器共用常量，避免两套名字漂移
    from ..managers.auto_proxy_manager import (
        TASK_ESCORT,
        TASK_FARM,
        TASK_FERTILIZE,
        TASK_NAMES,
        TASK_RIFT,
        TASK_SECT,
        DEFAULT_FERTILIZE_THRESHOLD,
        DEFAULT_FERTILIZE_MIN_ROI,
    )
except ImportError:  # pragma: no cover - 单元测试没有 AstrBot 时的回退
    TASK_ESCORT = "escort"
    TASK_SECT = "sect_task"
    TASK_FARM = "farm"
    TASK_RIFT = "rift"
    TASK_FERTILIZE = "fertilize"
    TASK_NAMES = {
        TASK_ESCORT: "押镖",
        TASK_SECT: "宗门任务",
        TASK_FARM: "灵田",
        TASK_RIFT: "秘境",
        TASK_FERTILIZE: "施肥",
    }
    DEFAULT_FERTILIZE_THRESHOLD = 0.85
    DEFAULT_FERTILIZE_MIN_ROI = 1.0

if TYPE_CHECKING:  # pragma: no cover
    from ..main import XiuXianPlugin

TASK_ORDER = (
    (TASK_ESCORT, "押镖", "自动结算并续接押镖"),
    (TASK_SECT, "宗门任务", "自动结算并续接宗门任务"),
    (TASK_FARM, "灵田", "成熟后自动收获并补种"),
    (TASK_RIFT, "秘境", "到期自动结算并重新进入"),
    (TASK_FERTILIZE, "施肥", "肥力低于阈值时自动补肥"),
)
PRIMARY_TASKS = (TASK_ESCORT, TASK_SECT)


def _brief(message: str, limit: int = 120) -> str:
    """把管理器返回的多行文案压成适合提示条的一两行。"""
    parts: list[str] = []
    for line in str(message or "").splitlines():
        text = line.strip()
        if not text or set(text) <= {"━", "-", " ", "─"}:
            continue
        text = text.lstrip("❌⚠️✅💡🛑🤖 ").strip()
        if text:
            parts.append(text)
        if len(parts) >= 2:
            break
    return " / ".join(parts)[:limit]


def _summary(name: str, ok: bool, message: str) -> str:
    if ok:
        return f"{name}已开启"
    reason = _brief(message) or "开启失败"
    return f"{name}：{reason}"


async def _get_farm(mgr: Any, user_id: str) -> Optional[dict]:
    if not getattr(mgr, "farm_mgr", None):
        return None
    try:
        return await mgr.farm_mgr.get_user_farm(user_id)
    except Exception:
        return None


async def _farm_slots(mgr: Any, farm: Optional[dict]) -> int:
    if not farm or not getattr(mgr, "farm_mgr", None):
        return 0
    try:
        return int(mgr.farm_mgr.get_max_slots(farm) or 0)
    except Exception:
        return 0


async def _all_rifts(plugin: "XiuXianPlugin", player: Any) -> list[dict[str, Any]]:
    """列出全部秘境，并标注当前境界能否进入（自动代理只允许已满足条件的本）。"""
    try:
        raw = await plugin.db.ext.get_all_rifts()
    except Exception:
        return []
    level = int(getattr(player, "level_index", 0) or 0)
    result: list[dict[str, Any]] = []
    for rift in raw or []:
        try:
            need = int(getattr(rift, "required_level", 0) or 0)
        except (TypeError, ValueError):
            need = 0
        result.append(
            {
                "id": int(getattr(rift, "rift_id", 0) or 0),
                "name": str(getattr(rift, "rift_name", "") or "秘境"),
                "required_level": need,
                "enterable": level >= need,
            }
        )
    result.sort(key=lambda item: item["id"])
    return result


async def proxy_snapshot(plugin: "XiuXianPlugin", user_id: str) -> dict[str, Any]:
    """返回自动代理面板所需的全部状态（不修改任何存档）。"""
    mgr = getattr(plugin, "auto_proxy_mgr", None)
    if mgr is None:
        return {"ok": False, "error": "自动代理模块未加载"}

    player = await plugin.db.get_player_by_id(user_id)
    if not player:
        return {
            "ok": True,
            "has_player": False,
            "enabled": False,
            "tasks": [],
            "rifts": [],
            "rift_id": 0,
            "farm": {"has_farm": False, "max_slots": 0, "plots_text": "全部格"},
            "fertilize": {"threshold": int(DEFAULT_FERTILIZE_THRESHOLD * 100)},
            "message": "尚未创建角色，请先发送 /我要修仙",
        }

    row = await mgr.get(user_id) or {}
    config = mgr._load_config(row)
    farm_cfg = config.get("farm") or {}
    rift_cfg = config.get("rift") or {}
    fert_cfg = config.get("fertilize") or {}
    enabled = bool(row.get("enabled"))
    primary = str(row.get("task_type") or "")

    farm = await _get_farm(mgr, user_id)
    max_slots = await _farm_slots(mgr, farm)
    plots = farm_cfg.get("plots")
    plots_text = mgr._farm_plots_text(plots) if hasattr(mgr, "_farm_plots_text") else "全部格"

    rifts = await _all_rifts(plugin, player)
    enterable = [rift for rift in rifts if rift["enterable"]]
    saved_rift_id = int(rift_cfg.get("rift_id") or 0)
    enterable_ids = {int(rift["id"]) for rift in enterable}
    # 旧存档里的秘境可能已不满足境界：回退到第一个可进入的本，避免下拉框选不中
    rift_id = saved_rift_id if saved_rift_id in enterable_ids else 0
    if not rift_id and enterable:
        rift_id = int(enterable[0]["id"])
    rift_name = next((rift["name"] for rift in rifts if rift["id"] == rift_id), "")

    threshold = float(fert_cfg.get("threshold") or DEFAULT_FERTILIZE_THRESHOLD)
    tasks: list[dict[str, Any]] = []
    for key, name, desc in TASK_ORDER:
        available = True
        detail = desc
        if key == TASK_ESCORT or key == TASK_SECT:
            on = enabled and primary == key
            if on:
                detail = "运行中"
            elif primary:
                detail = f"当前代理目标：{TASK_NAMES.get(primary, primary)}"
            else:
                detail = "未开启"
        elif key == TASK_FARM:
            on = enabled and bool(farm_cfg.get("enabled"))
            available = bool(farm)
            if not available:
                detail = "尚未开垦灵田"
            elif on:
                detail = f"运行中 · {plots_text}"
            else:
                detail = f"灵田 {max_slots} 格 · 点击开启"
        elif key == TASK_RIFT:
            on = enabled and bool(rift_cfg.get("enabled"))
            available = bool(enterable)
            if not available:
                detail = "当前境界暂无可进入的秘境"
            elif on:
                detail = f"目标：{rift_name or '秘境'}（ID:{rift_id}）"
            else:
                detail = f"可选 {len(enterable)} 个秘境 · 点击开启"
        else:  # TASK_FERTILIZE
            on = enabled and bool(fert_cfg.get("enabled"))
            available = bool(farm)
            if not available:
                detail = "尚未开垦灵田"
            elif on:
                detail = f"阈值 {threshold:.0%} · 运行中"
            else:
                detail = f"低于 {threshold:.0%} 自动补肥"
        tasks.append(
            {
                "key": key,
                "name": name,
                "desc": desc,
                "enabled": bool(on),
                "available": bool(available),
                "detail": detail,
            }
        )

    return {
        "ok": True,
        "has_player": True,
        "enabled": enabled,
        "tasks": tasks,
        "rifts": [
            {
                "id": rift["id"],
                "name": rift["name"],
                "required_level": rift["required_level"],
            }
            for rift in enterable
        ],
        "rift_id": rift_id,
        "farm": {
            "has_farm": bool(farm),
            "max_slots": max_slots,
            "plots_text": plots_text,
        },
        "fertilize": {
            "threshold": int(round(threshold * 100)),
            "min_roi": float(fert_cfg.get("min_roi") or DEFAULT_FERTILIZE_MIN_ROI),
        },
        "last_error": str(row.get("last_error") or ""),
        "message": "",
    }


async def _toggle_primary(
    mgr: Any, user_id: str, task: str, enabled: bool, origin: str
) -> tuple[bool, str]:
    name = TASK_NAMES.get(task, task)
    if enabled:
        return await mgr.enable(user_id, origin, name)
    row = await mgr.get(user_id) or {}
    if str(row.get("task_type") or "") != task:
        return True, f"💡 {name}代理当前未开启"
    return await mgr.disable_primary(user_id)


async def _toggle_rift(
    plugin: "XiuXianPlugin",
    mgr: Any,
    player: Any,
    user_id: str,
    enabled: bool,
    rift_id: int,
    origin: str,
) -> tuple[bool, str]:
    if not enabled:
        return await mgr.disable_rift(user_id)
    if not rift_id:
        row = await mgr.get(user_id) or {}
        rift_id = int(mgr._load_config(row)["rift"].get("rift_id") or 0)
    if not rift_id:
        enterable = [rift for rift in await _all_rifts(plugin, player) if rift["enterable"]]
        if not enterable:
            return False, "❌ 当前境界没有可代理的秘境"
        rift_id = int(enterable[0]["id"])
    return await mgr.enable(user_id, origin, f"秘境 {rift_id}")


async def _enable_all(
    plugin: "XiuXianPlugin",
    mgr: Any,
    player: Any,
    user_id: str,
    rift_id: int,
    origin: str,
) -> tuple[bool, str]:
    row = await mgr.get(user_id) or {}
    config = mgr._load_config(row)
    farm = await _get_farm(mgr, user_id)
    messages: list[str] = []
    ok_any = False

    if farm:
        for task in (TASK_FARM, TASK_FERTILIZE):
            ok, msg = await mgr.enable(user_id, origin, TASK_NAMES[task])
            ok_any = ok_any or ok
            messages.append(_summary(TASK_NAMES[task], ok, msg))

    target_rift = int(rift_id or config["rift"].get("rift_id") or 0)
    if not target_rift:
        enterable = [rift for rift in await _all_rifts(plugin, player) if rift["enterable"]]
        target_rift = int(enterable[0]["id"]) if enterable else 0

    if target_rift:
        ok, msg = await mgr.enable(user_id, origin, f"秘境 {target_rift}")
        ok_any = ok_any or ok
        messages.append(_summary("秘境", ok, msg))
    else:
        preferred = str(row.get("task_type") or TASK_ESCORT)
        name = TASK_NAMES.get(preferred, "押镖")
        ok, msg = await mgr.enable(user_id, origin, name)
        ok_any = ok_any or ok
        messages.append(_summary(name, ok, msg))

    if not messages:
        return False, "❌ 没有可开启的自动代理"
    return ok_any, "；".join(messages)


async def toggle_proxy(
    plugin: "XiuXianPlugin",
    user_id: str,
    task: str,
    enabled: bool,
    rift_id: int = 0,
    origin: str = "",
) -> dict[str, Any]:
    """切换单项或总开关，返回最新面板状态。"""
    mgr = getattr(plugin, "auto_proxy_mgr", None)
    if mgr is None:
        return {"ok": False, "error": "自动代理模块未加载"}

    player = await plugin.db.get_player_by_id(user_id)
    if not player:
        return {"ok": False, "error": "尚未创建角色，请先发送 /我要修仙"}

    task = str(task or "").strip()
    if task == "all":
        if enabled:
            ok, message = await _enable_all(plugin, mgr, player, user_id, rift_id, origin)
        else:
            was_enabled = await mgr.disable(user_id)
            ok = True
            message = "🛑 自动代理已全部关闭" if was_enabled else "💡 自动代理当前已关闭"
    elif task in TASK_NAMES:
        if task in PRIMARY_TASKS:
            ok, message = await _toggle_primary(mgr, user_id, task, enabled, origin)
        elif task == TASK_FARM:
            if enabled:
                ok, message = await mgr.enable(user_id, origin, "灵田")
            else:
                ok, message = await mgr.disable_farm(user_id)
        elif task == TASK_FERTILIZE:
            if enabled:
                ok, message = await mgr.enable(user_id, origin, "施肥")
            else:
                ok, message = await mgr.disable_fert(user_id)
        else:
            ok, message = await _toggle_rift(
                plugin, mgr, player, user_id, enabled, rift_id, origin
            )
    else:
        return {"ok": False, "error": "未知的自动代理类型"}

    name = TASK_NAMES.get(task, "全部")
    if task == "all":
        text = _brief(message)
    elif ok:
        text = f"{name}代理已{'开启' if enabled else '关闭'}"
    else:
        text = f"{name}：{_brief(message) or '操作失败'}"

    snapshot = await proxy_snapshot(plugin, user_id)
    snapshot["done"] = bool(ok)
    snapshot["message"] = text
    return snapshot
