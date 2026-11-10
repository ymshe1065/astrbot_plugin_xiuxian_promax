# -*- coding: utf-8 -*-
"""统一的 astrbot 测试桩。

所有测试共享同一套桩，避免各文件用 ``sys.modules.setdefault`` 各自注册，
导致「先导入的文件决定桩内容」，让后续模块 ``from astrbot.api import ...`` 失败。

只补缺失的模块与属性，不会覆盖真实的 astrbot。
``astrbot.core.utils.astrbot_path`` 刻意不在这里安装：多个用例需要把它指向
各自的临时数据目录，由用例自行注册。
"""
import sys
import types


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


def _ensure_module(name: str, **attrs) -> types.ModuleType:
    """取得或创建模块，并只在属性缺失时补齐。"""
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    for key, value in attrs.items():
        if not hasattr(module, key):
            setattr(module, key, value)
    return module


def install(logger=None) -> types.ModuleType:
    """安装 astrbot 公共桩，返回 ``astrbot.api`` 模块。"""
    api = _ensure_module(
        "astrbot.api",
        logger=logger or _Logger(),
        AstrBotConfig=dict,
    )
    if not getattr(api, "__path__", None):
        api.__path__ = []

    event = _ensure_module("astrbot.api.event", AstrMessageEvent=object)
    all_mod = _ensure_module("astrbot.api.all", At=object, Plain=object)
    if not hasattr(api, "event"):
        api.event = event
    if not hasattr(api, "all"):
        api.all = all_mod

    astrbot = _ensure_module("astrbot")
    if not hasattr(astrbot, "api"):
        astrbot.api = api
    return api
