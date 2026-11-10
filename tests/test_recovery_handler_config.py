# tests/test_recovery_handler_config.py
# 没有 config_manager 属性的 Handler 也必须能拿到插件配置，
# 不能用回退模板把玩家当前气血压低。
import asyncio
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestRecoveryHandlerConfig(unittest.TestCase):
    def test_handler_can_find_config_manager_from_managed_object(self):
        utils = _imp("handlers.utils")
        marker = object()

        class Manager:
            config_manager = marker

        class Handler:
            def __init__(self):
                self.mgr = Manager()

        self.assertIs(utils._handler_config_manager(Handler()), marker)

    def test_recovery_without_config_does_not_clamp_hp(self):
        RecoveryManager = _imp("managers.recovery_manager").RecoveryManager
        player = types.SimpleNamespace(
            user_id="u1", hp=250000, spiritual_qi=1000, max_spiritual_qi=1000
        )
        manager = RecoveryManager(None, None)
        asyncio.run(manager.apply_lazy_recovery(player))
        self.assertEqual(player.hp, 250000)


if __name__ == "__main__":
    unittest.main()
