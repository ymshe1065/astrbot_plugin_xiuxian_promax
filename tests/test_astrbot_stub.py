# -*- coding: utf-8 -*-
"""统一 astrbot 桩的守护用例：确保公共属性始终可用。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class AstrbotStubGuardTest(unittest.TestCase):
    def test_common_attributes_available(self):
        from astrbot.api import AstrBotConfig, logger  # noqa: F401
        from astrbot.api.event import AstrMessageEvent  # noqa: F401
        from astrbot.api.all import At, Plain  # noqa: F401

        self.assertIs(AstrBotConfig, dict)
        self.assertTrue(hasattr(logger, "info"))

    def test_install_is_idempotent(self):
        from tests import _astrbot_stub

        first = _astrbot_stub.install()
        second = _astrbot_stub.install()
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
