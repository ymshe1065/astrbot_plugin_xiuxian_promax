# -*- coding: utf-8 -*-
"""webchat_room 引导入口：注册 webroom 平台适配器。

在插件 __init__ 中调用（插件加载早于平台初始化，这是注册生效的前提）。
幂等：重复注册会被跳过。
"""
from __future__ import annotations

from astrbot.api import logger


def bootstrap_webroom() -> bool:
    try:
        from .webroom_adapter import register

        ok = register()
        if ok:
            logger.info("【修仙插件】网页聊天室(webroom)平台适配器已就绪；"
                        "在后台「平台适配」新增 type=webroom 的实例并启用即可。")
        return ok
    except Exception as e:
        logger.error(f"【修仙插件】webroom 注册异常: {e}")
        return False
