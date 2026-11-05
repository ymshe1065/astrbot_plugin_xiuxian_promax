# -*- coding: utf-8 -*-
"""仙市面板结构化：伪表格拆成行数据，交给前端按真表格排版。"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ts_market_under_test", ROOT / "webchat_room" / "text_struct.py")
ts = importlib.util.module_from_spec(_spec)
sys.modules["ts_market_under_test"] = ts
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
    "S02 器阁         8.90   ▼0.50%   ▲1.20%   ▼0.30%",
    "S03 镖局        23.10   ▲2.80%   ▲5.60%   ▲1.90%",
    "　（本盘 / 今日 / 近5盘）",
    LINE,
    "基金",
    "F01 灵脉·稳     1.0234   ▲0.10%   ▲0.30%  ★",
    "F02 仙市·衡     1.1180   ▲0.42%   ▲1.10%  ★★",
    LINE,
    "下盘 14:00　·　距收市 1 小时 20 分",
    "指令：/行情 <代码>　/买入股 <代码> <手>　/申购 <代码> <金额>",
])

CLOSED_TEXT = "\n".join([
    "「 仙市 · 已收市 」",
    LINE,
    "股票",
    "S01 丹阁        12.34   —   ▲3.40%   ▲0.80%",
    LINE,
    "闭市中　·　距开盘 8 小时 0 分",
])


class TestMarketCard(unittest.TestCase):
    def setUp(self):
        self.card = ts.parse_text(MARKET_TEXT)
        self.market = self.card["market"]

    def test_kind_and_headline(self):
        self.assertEqual(self.card["kind"], "market")
        self.assertEqual(self.card["title"], "仙市 · 第 3 盘 · 午市")
        self.assertEqual(self.market["tick"], 3)
        self.assertFalse(self.market["closed"])
        self.assertAlmostEqual(self.market["progress"], 3 / 16)

    def test_status_line_becomes_subtitle(self):
        self.assertEqual(self.card["subtitle"], "下盘 14:00 · 距收市 1 小时 20 分")

    def test_rows_split_into_cells(self):
        rows = self.market["rows"]
        self.assertEqual([r["code"] for r in rows], ["S01", "S02", "S03", "F01", "F02"])
        first = rows[0]
        self.assertEqual(first["name"], "丹阁")
        self.assertEqual(first["price"], "12.34")
        self.assertEqual(first["pcts"], ["▲1.20%", "▲3.40%", "▲0.80%"])
        self.assertEqual(first["kind"], "stock")

    def test_fund_rows_keep_risk_stars(self):
        fund = [r for r in self.market["rows"] if r["kind"] == "fund"]
        self.assertEqual([r["risk"] for r in fund], [1, 2])
        self.assertEqual(fund[0]["price"], "1.0234")
        # 星号不能粘进名称或涨跌幅
        self.assertEqual(fund[0]["name"], "灵脉·稳")
        self.assertEqual(fund[0]["pcts"], ["▲0.10%", "▲0.30%"])

    def test_column_names_come_from_note_line(self):
        self.assertEqual(self.market["stock_cols"], ["本盘", "今日", "近5盘"])
        self.assertEqual(self.market["fund_cols"], ["本盘", "今日"])

    def test_shrunk_row_still_parses(self):
        """名字顶满列宽时价格会粘在名字后面，仍要拆开。"""
        card = ts.parse_market("\n".join([
            "「 仙市 · 第 1 盘 · 早市 」",
            "股票",
            "S04 灵田药材铺8.90   ▲0.10%   ▲0.20%   ▲0.30%",
        ]))
        self.assertIsNotNone(card)
        row = card["market"]["rows"][0]
        self.assertEqual(row["name"], "灵田药材铺")
        self.assertEqual(row["price"], "8.90")

    def test_closed_market(self):
        card = ts.parse_text(CLOSED_TEXT)
        self.assertEqual(card["kind"], "market")
        self.assertTrue(card["market"]["closed"])
        self.assertEqual(card["market"]["progress"], 0.0)
        self.assertEqual(card["subtitle"], "闭市中 · 距开盘 8 小时 0 分")
        # 涨跌幅为「—」时仍算一列，不能因为不是百分比就吞掉
        self.assertEqual(card["market"]["rows"][0]["pcts"], ["—", "▲3.40%", "▲0.80%"])

    def test_actions_extracted(self):
        cmds = [a["cmd"] for a in self.card["actions"]]
        self.assertIn("/行情", cmds)
        self.assertIn("/买入股", cmds)
        self.assertIn("/申购", cmds)

    def test_not_a_market_panel(self):
        """普通长文本不能被误判成行情卡片。"""
        self.assertIsNone(ts.parse_market("「 仙市 」闭市了，今天没开"))
        self.assertNotEqual((ts.parse_text("📊 境界排行榜\n━━━\n1. 青云\n2. 白衣") or {}).get("kind"), "market")


if __name__ == "__main__":
    unittest.main()
