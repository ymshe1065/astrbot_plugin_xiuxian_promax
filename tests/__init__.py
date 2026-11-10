# -*- coding: utf-8 -*-
"""测试包入口：先安装统一的 astrbot 桩，再加载任何用例。

各测试文件自身的桩仍保留，用于单独执行 ``python tests/test_x.py``；
以 ``tests.test_x`` 方式运行时，这里已装好的桩会让它们的
``sys.modules.setdefault`` 自动空转。
"""
from ._astrbot_stub import install

install()
