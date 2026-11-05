# tests/test_invest_candles.py
# K 线合成：invest_prices 只有收盘价，日线开高低由盘内数据派生，周/月再聚合
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")


class _StubLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


api_mod.logger = _StubLogger()
api_mod.AstrBotConfig = dict
event_mod.AstrMessageEvent = object
api_mod.event = event_mod
all_mod = types.ModuleType("astrbot.api.all")
all_mod.At = object
all_mod.Plain = object
api_mod.all = all_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod)
sys.modules.setdefault("astrbot.api.all", all_mod)


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


FIXED_SECRET = "unit-test-secret-candles"
BASE_DAY = 300  # 2026-01-01 起第 300 天


class TestInvestCandles(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.InvestManager = _imp("managers.invest_manager").InvestManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)

        await self.db.ext.set_system_config("invest_secret", FIXED_SECRET)
        self.manager = self.InvestManager(self.db, {}, None)
        self.manager._secret = FIXED_SECRET
        await self.manager._seed_products()
        # 价格表内容由本用例完全掌控：结算会把真实行情写进去，这里全程挡住
        self.manager.settle_to = mock.AsyncMock(return_value=0)

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _fill(self, days: int = 3, scale: float = 1.0) -> None:
        for d in range(days):
            for s in range(16):
                tick = (BASE_DAY + d) * 16 + s
                close = (40.0 + d * 2.0 + s * 0.1) * scale
                tag = "单边上涨" if (d == 1 and s == 3) else ""
                await self.conn.execute(
                    "INSERT OR REPLACE INTO invest_prices "
                    "(code, tick, close, change_rate, msg_tag) VALUES (?, ?, ?, ?, ?)",
                    ("S01", tick, close, 0.0, tag),
                )
        await self.conn.commit()

    async def test_daily_ohlc_from_closes(self):
        await self._fill(3)
        data = await self.manager.get_candles("S01", "day")
        self.assertIsNotNone(data)
        bars = data["candles"]
        self.assertEqual(len(bars), 3)

        first, second = bars[0], bars[1]
        self.assertEqual(first["d"], BASE_DAY)
        # 首根没有前一日收盘，开盘取当日首盘
        self.assertAlmostEqual(first["o"], 40.0, places=6)
        self.assertAlmostEqual(first["c"], 41.5, places=6)
        self.assertAlmostEqual(first["h"], 41.5, places=6)
        self.assertAlmostEqual(first["l"], 40.0, places=6)
        self.assertEqual(first["n"], 16)
        # 次根开盘接上一根收盘，价格连续、没有隔夜跳空
        self.assertAlmostEqual(second["o"], first["c"], places=6)
        self.assertAlmostEqual(second["c"], 43.5, places=6)
        # 开盘价也参与高低点，否则影线会缺一段
        self.assertAlmostEqual(second["l"], min(second["o"], 42.0), places=6)

        self.assertEqual(data["kind"], "stock")
        self.assertEqual(data["period"], "day")
        self.assertFalse(data["enough"])  # 只有 3 根，前端降级折线

    async def test_news_tags_kept_on_bar(self):
        await self._fill(3)
        data = await self.manager.get_candles("S01", "day")
        self.assertEqual(data["candles"][1]["tags"], ["单边上涨"])
        self.assertEqual(data["candles"][0]["tags"], [])

    async def test_month_merge(self):
        await self._fill(3)
        data = await self.manager.get_candles("S01", "month")
        bars = data["candles"]
        self.assertEqual(len(bars), 1)  # 三天同月
        bar = bars[0]
        self.assertAlmostEqual(bar["o"], 40.0, places=6)
        self.assertAlmostEqual(bar["c"], 45.5, places=6)
        self.assertAlmostEqual(bar["h"], 45.5, places=6)
        self.assertAlmostEqual(bar["l"], 40.0, places=6)
        self.assertEqual(bar["n"], 48)
        self.assertAlmostEqual(bar["chg"], 45.5 / 40.0 - 1, places=6)

    async def test_week_merge_keeps_gaps(self):
        """跨周的数据不能被合成一根：周线根数应大于等于 1 且小于等于日线根数。"""
        await self._fill(10)
        day = await self.manager.get_candles("S01", "day")
        week = await self.manager.get_candles("S01", "week")
        self.assertEqual(len(day["candles"]), 10)
        self.assertLessEqual(len(week["candles"]), 10)
        self.assertGreaterEqual(len(week["candles"]), 2)
        # 每根周线的收盘等于该周最后一天的收盘
        self.assertAlmostEqual(week["candles"][-1]["c"], day["candles"][-1]["c"], places=6)

    async def test_limit_and_period_fallback(self):
        await self._fill(10)
        data = await self.manager.get_candles("S01", "季", "4")
        self.assertEqual(data["period"], "day")  # 未知周期退回日线
        self.assertEqual(len(data["candles"]), 8)  # limit 下限 8

    async def test_unknown_code(self):
        self.assertIsNone(await self.manager.get_candles("S99", "day"))

    async def test_empty_history(self):
        data = await self.manager.get_candles("S01", "day")
        self.assertEqual(data["candles"], [])
        self.assertFalse(data["enough"])
        self.assertAlmostEqual(data["prev_close"], 0.0, places=6)

    async def test_fund_kind(self):
        await self.conn.execute(
            "INSERT OR REPLACE INTO invest_prices "
            "(code, tick, close, change_rate, msg_tag) VALUES (?, ?, ?, ?, ?)",
            ("F01", BASE_DAY * 16, 1.0234, 0.0, ""),
        )
        await self.conn.commit()
        data = await self.manager.get_candles("F01", "day")
        self.assertEqual(data["kind"], "fund")


if __name__ == "__main__":
    unittest.main()
