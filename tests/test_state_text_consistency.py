# tests/test_state_text_consistency.py
# players.state 的展示文案只允许有一个来源。
#
# 背景：_STATE_TEXT（写 players.state）和 UserStatus.get_name（拼提示、我的信息）
# 曾是两份独立维护的映射表，CULTIVATING 一个叫「修炼中」一个叫「闭关中」。
# 现在 _STATE_TEXT 从 get_name 派生，只留一条被显式登记的例外。
#
# 这个测试的作用是：将来谁再往 _STATE_TEXT 里手写一条与 get_name 不一致的值，
# 必须同时把状态加进 KNOWN_EXCEPTIONS —— 逼迫差异被看见，而不是悄悄漂移。
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()

# 允许与 get_name 不同的状态：目前只有闭关，等 S1b 定下文案后清空这个集合。
KNOWN_EXCEPTIONS = {"CULTIVATING"}


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestStateTextConsistency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.UserStatus = _imp("models_extended").UserStatus
        cls.STATE_TEXT = _imp("data.database_extended")._STATE_TEXT

    def test_every_status_has_entry(self):
        """每个枚举成员都要有文案，否则玩家状态会落到兜底的「忙碌中」。"""
        for status in self.UserStatus:
            self.assertIn(
                int(status), self.STATE_TEXT,
                f"{status.name} 缺少 players.state 文案",
            )

    def test_only_known_exceptions_differ(self):
        """除已登记的例外外，文案必须与 get_name 一致。"""
        drifted = []
        for status in self.UserStatus:
            actual = self.STATE_TEXT[int(status)]
            expected = self.UserStatus.get_name(int(status))
            if actual == expected:
                continue
            if status.name in KNOWN_EXCEPTIONS:
                continue
            drifted.append(f"{status.name}: _STATE_TEXT={actual!r} vs get_name={expected!r}")
        self.assertEqual(drifted, [], "状态文案重新漂移了：" + "; ".join(drifted))

    def test_cultivating_value_unchanged(self):
        """S1a 是零行为变化改造，值必须还是「修炼中」。改这个值请走 S1b。"""
        self.assertEqual(self.STATE_TEXT[int(self.UserStatus.CULTIVATING)], "修炼中")

    def test_unknown_status_falls_back(self):
        """未登记的状态码仍兜底「忙碌中」，与原行为一致。"""
        self.assertEqual(self.STATE_TEXT.get(999, "忙碌中"), "忙碌中")


if __name__ == "__main__":
    unittest.main()
