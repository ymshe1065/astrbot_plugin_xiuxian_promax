# -*- coding: utf-8 -*-
"""text_struct 解析回归：仙市风声这类「标签 + 要点 + 缩进正文」不能被丢行。"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ts_under_test", ROOT / "webchat_room" / "text_struct.py")
ts = importlib.util.module_from_spec(_spec)
sys.modules["ts_under_test"] = ts
_spec.loader.exec_module(ts)

LINE = "━" * 24

MARKET_TEXT = "\n".join([
    "「 仙市 · 第 3 盘 · 午市 」",
    LINE,
    "当盘风声",
    "· 全市场 · 丹阁与青云宗签下三年供丹契约",
    "　每月固定交货三千炉，产能已排到年末。",
    LINE,
    "下盘风声 · 14:00",
    "· 器阁接下边关军备大单",
    "　三年内按月交付，炉火终年不熄。",
    LINE,
    "股票",
    "S01 丹阁        12.34   ▲1.20%   ▲3.40%   ▲0.80%",
    "　（本盘 / 今日 / 近5盘）",
    LINE,
    "指令：/行情 <代码>　/买入股 <代码> <手>",
])


class TestMarketHints(unittest.TestCase):
    def setUp(self):
        self.card = ts.parse_text(MARKET_TEXT)
        self.assertTrue(self.card, "仙市面板应被结构化")
        self.hints = self.card["market"]["hints"]

    def test_hint_block_keeps_all_three_lines(self):
        """风声的「标签 / 要点 / 缩进正文」都要在，缺一行就是显示不全。"""
        first = self.hints[0]
        self.assertEqual(first["label"], "当盘风声")
        self.assertEqual(first["title"], "全市场 · 丹阁与青云宗签下三年供丹契约")
        self.assertEqual(first["detail"], "每月固定交货三千炉，产能已排到年末。")

    def test_next_hint_block(self):
        second = self.hints[1]
        self.assertEqual(second["label"], "下盘风声")
        self.assertEqual(second["hour"], "14:00")
        self.assertTrue(second["title"].startswith("器阁"))
        self.assertTrue(second["detail"].startswith("三年内"))

    def test_short_message_still_not_structured(self):
        self.assertIsNone(ts.parse_text("✅ 打坐结束，修为 +12"))


if __name__ == "__main__":
    unittest.main()
