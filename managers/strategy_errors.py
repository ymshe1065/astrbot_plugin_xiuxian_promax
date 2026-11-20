# -*- coding: utf-8 -*-
"""「仙域争霸」共享异常（managers 内各 mixin 与主管理器共用）。

独立成模块的原因：strategy_manager.py 多继承五个 mixin
（faction/captive/plot/battle/p3），mixin 若反向导入
strategy_manager 会形成循环依赖。
"""
from __future__ import annotations


class StrategyError(Exception):
    """动作失败统一异常（面板转 4xx 文本）。"""
    pass


class StrategyBusyError(StrategyError):
    """世界 tick 正在推进，写操作稍后重试（面板 423）。"""
    pass
