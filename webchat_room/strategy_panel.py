# -*- coding: utf-8 -*-
"""网页聊天室「仙域争霸」面板（P0，设计方案 §12.2）。

- ``GET  /api/strategy/state`` ：世界全量快照（战争迷雾过滤在 manager 内完成）；
- ``POST /api/strategy/action``：统一动作分发 ``{"action": ..., "params": {...}}``。

所有规则与落库都走 :class:`StrategyManager`，与后续聊天端指令共用同一套
校验，避免网页端和 QQ 端出现两套数值。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..main import XiuXianPlugin

try:  # AstrBot 生产环境：插件以包加载，相对导入可用
    from ..managers.strategy_errors import StrategyBusyError, StrategyError
except ImportError:  # 测试以顶层包导入 webchat_room：按文件加载轻量异常模块
    import importlib.util as _ilu
    import sys as _sys
    from pathlib import Path as _Path

    _MOD_NAME = "managers.strategy_errors"
    # 优先复用已注册模块，保证与 managers 包内导入的异常类身份一致
    _mod = _sys.modules.get(_MOD_NAME)
    if _mod is None:
        _spec = _ilu.spec_from_file_location(
            _MOD_NAME,
            _Path(__file__).resolve().parents[1] / "managers" / "strategy_errors.py",
        )
        _mod = _ilu.module_from_spec(_spec)
        _sys.modules[_MOD_NAME] = _mod
        _spec.loader.exec_module(_mod)
    StrategyError = _mod.StrategyError
    StrategyBusyError = _mod.StrategyBusyError

__all__ = ["StrategyBusyError", "StrategyError", "strategy_action",
           "strategy_state", "strategy_logs"]


def _manager(plugin: "XiuXianPlugin"):
    mgr = getattr(plugin, "strategy_mgr", None)
    if mgr is None:
        raise StrategyError("争霸玩法未启用（插件未加载 StrategyManager）")
    fn = getattr(mgr, "enabled", None)
    if callable(fn) and not fn():
        raise StrategyError("仙域争霸未开启")
    return mgr


async def strategy_state(plugin: "XiuXianPlugin", user_id: str) -> dict[str, Any]:
    return await _manager(plugin).get_state(user_id)


async def strategy_logs(plugin: "XiuXianPlugin", user_id: str, kind: str = "",
                        before_id: int = 0, limit: int = 20) -> dict[str, Any]:
    """「天下 → 江湖纪事」分页查询（按所属势力/联盟过滤可见性）。"""
    return await _manager(plugin).get_world_logs_for_user(
        user_id, kind=kind, before_id=before_id, limit=limit)


async def strategy_action(plugin: "XiuXianPlugin", user_id: str, action: str,
                          params: dict[str, Any] | None = None,
                          nickname: str = "") -> dict[str, Any]:
    if action == "enter" and not (params or {}).get("nickname"):
        params = dict(params or {})
        params["nickname"] = nickname or user_id
    return await _manager(plugin).handle_action(user_id, action, params)
