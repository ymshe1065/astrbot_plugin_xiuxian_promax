# tests/test_invest_news_balance.py
# v68 风声平衡：可信度分层、假消息、幅度下调、新闻冲击成本
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()

FIXED_SECRET = "unit-test-secret-news-balance"


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestNewsBiasMath(unittest.TestCase):
    """不依赖数据库的 bias 计算。"""

    def setUp(self):
        mod = _imp("managers.invest_manager")
        self.mod = mod
        self.mgr = mod.InvestManager.__new__(mod.InvestManager)
        self.stock = {"code": "S01", "kind": "stock", "msg_base": 0.1}
        self.fund = {"code": "F01", "kind": "fund", "msg_base": 0.1}

    def _news(self, **overrides):
        item = {
            "trade_day": 0,
            "slot": 10,
            "code": "S01",
            "pattern": "single",
            "direction": 1,
            "strength": 1.0,
            "credibility": "confirmed",
            "narrative_direction": 1,
        }
        item.update(overrides)
        return [item]

    def test_confirmed_news_applies_full_scale(self):
        bias = self.mgr._bias_for(self.stock, 0, 10, self._news())
        self.assertAlmostEqual(bias, 0.1 * self.mod.NEWS_BIAS_SCALE)

    def test_rumor_is_discounted(self):
        bias = self.mgr._bias_for(self.stock, 0, 10, self._news(credibility="rumor"))
        self.assertAlmostEqual(
            bias, 0.1 * self.mod.NEWS_BIAS_SCALE * self.mod.CREDIBILITY_SCALE["rumor"]
        )

    def test_false_news_uses_actual_direction(self):
        # 标题是利好，但实际 direction=-1：价格应按利空走
        bias = self.mgr._bias_for(
            self.stock, 0, 10,
            self._news(direction=-1, narrative_direction=1),
        )
        self.assertLess(bias, 0)

    def test_market_news_scale_and_fund_discount(self):
        news = self._news(code="*", credibility="rumor")
        stock_bias = self.mgr._bias_for(self.stock, 0, 10, news)
        fund_bias = self.mgr._bias_for(self.fund, 0, 10, news)
        self.assertAlmostEqual(
            stock_bias,
            0.1 * self.mod.NEWS_BIAS_SCALE
            * self.mod.CREDIBILITY_SCALE["rumor"]
            * self.mod.MARKET_NEWS_SCALE,
        )
        self.assertAlmostEqual(
            fund_bias,
            0.1 * self.mod.NEWS_BIAS_SCALE
            * self.mod.CREDIBILITY_SCALE["rumor"]
            * self.mod.MARKET_NEWS_SCALE
            * self.mod.FUND_NEWS_SCALE,
        )

    def test_individual_news_does_not_affect_funds(self):
        self.assertEqual(self.mgr._bias_for(self.fund, 0, 10, self._news()), 0.0)

    def test_hint_lines_show_credibility_tag(self):
        handlers = _imp("handlers.invest_handlers")
        confirmed = "\n".join(handlers._hint_lines(
            "下盘风声",
            {"pattern": "single", "title": "契约", "code": "", "credibility": "confirmed"},
        ))
        self.assertIn("（正式）", confirmed)
        rumor = "\n".join(handlers._hint_lines(
            "下盘风声",
            {"pattern": "single", "title": "传闻", "code": "", "credibility": "rumor"},
        ))
        self.assertIn("（传闻）", rumor)
        calm = "\n".join(handlers._hint_lines(
            "下盘风声",
            {"pattern": "calm", "title": "今日盘面平静", "code": "", "credibility": "confirmed"},
        ))
        self.assertNotIn("（正式）", calm)


class TestNewsBalanceFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        self.migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.InvestManager = _imp("managers.invest_manager").InvestManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.migration._create_all_tables_v2(self.conn)
        await self.migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        await self.db.ext.set_system_config("invest_secret", FIXED_SECRET)

        self.manager = self.InvestManager(self.db, {}, None)
        self.manager._secret = FIXED_SECRET
        await self.manager._seed_products()
        self.manager.settle_to = mock.AsyncMock(return_value=0)

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    def _at(self, tick: int):
        return (
            mock.patch.object(
                self.InvestManager, "tick_of",
                classmethod(lambda cls, moment=None: tick),
            ),
            mock.patch.object(
                self.InvestManager, "is_open",
                classmethod(lambda cls, moment=None: True),
            ),
        )

    async def _player(self, uid: str = "u1", gold: int = 1_000_000):
        await self.db.create_player(self.Player(user_id=uid, gold=gold, user_name="测试道友"))
        return await self.db.get_player_by_id(uid)

    async def _insert_event(self, tick: int, code: str = "S01", pattern: str = "single",
                            direction: int = 1, credibility: str = "confirmed"):
        day, slot = divmod(tick, 16)
        await self.conn.execute(
            "INSERT INTO invest_events "
            "(trade_day, slot, code, pattern, direction, base_bias, strength, credibility, "
            " narrative_direction, title, detail) "
            "VALUES (?, ?, ?, ?, ?, 0.0, 1.0, ?, ?, '测试风声', '测试细节')",
            (day, slot, code, pattern, direction, credibility, direction),
        )
        await self.conn.commit()

    # ===== 生成分布 =====

    async def test_news_generation_has_credibility_and_false_news(self):
        for tick in range(1, 401):
            day, slot = divmod(tick, 16)
            await self.manager._ensure_news(day, slot)
        rows = await self.manager._rows(
            "SELECT code, pattern, direction, credibility, narrative_direction "
            "FROM invest_events"
        )
        real = [r for r in rows if r["pattern"] != "calm"]
        self.assertTrue(real, "400 盘里应至少有一条真消息")
        self.assertTrue(all(r["credibility"] in ("confirmed", "rumor") for r in real))
        self.assertTrue(all(int(r["narrative_direction"]) != 0 for r in real))
        self.assertTrue(
            any(int(r["direction"]) == int(r["narrative_direction"]) for r in real),
            "应存在按标题方向兑现的消息",
        )
        self.assertTrue(
            any(int(r["direction"]) == -int(r["narrative_direction"]) for r in real),
            "应存在与标题相反结算的假消息",
        )
        market = [r for r in real if r["code"] == "*"]
        self.assertTrue(market, "应生成过全市场风声")
        self.assertTrue(all(r["credibility"] == "rumor" for r in market))
        confirmed = sum(1 for r in real if r["credibility"] == "confirmed")
        ratio = confirmed / len(real)
        self.assertGreater(ratio, 0.32, "确认消息比例过低")
        self.assertLess(ratio, 0.68, "确认消息比例过高")

    # ===== 冲击成本 =====

    async def test_news_impact_rate_decays_by_offset(self):
        self.manager._ensure_news = mock.AsyncMock()
        await self._insert_event(100)
        cases = {
            99: 0.008,   # 抢跑下一盘
            100: 0.008,  # 消息当盘
            101: 0.005,
            102: 0.0025,
            103: 0.0,
            104: 0.0,
        }
        for tick, expect in cases.items():
            rate = await self.manager._news_impact_rate("S01", tick)
            self.assertAlmostEqual(rate, expect, msg=f"tick {tick}")

    async def test_market_news_impact_applies_to_all_stocks(self):
        self.manager._ensure_news = mock.AsyncMock()
        await self._insert_event(100, code="*", credibility="rumor")
        for code in ("S01", "S02", "S05"):
            self.assertAlmostEqual(
                await self.manager._news_impact_rate(code, 99), 0.008, msg=code
            )

    async def test_buy_and_sell_use_impact_price(self):
        await self._player()
        self.manager._ensure_news = mock.AsyncMock()
        await self._insert_event(101)  # 下一盘有 S01 消息
        tick_patch, open_patch = self._at(100)
        with tick_patch, open_patch:
            ok, msg = await self.manager.buy_stock(
                await self.db.get_player_by_id("u1"), "S01", 1
            )
        self.assertTrue(ok, msg)
        self.assertIn("新闻冲击", msg)
        buy = (await self.manager._rows(
            "SELECT price FROM invest_orders WHERE user_id = 'u1' AND side = 'buy'"
        ))[0]
        self.assertAlmostEqual(float(buy["price"]), 42.0 * 1.008, places=4)

        with tick_patch, open_patch:
            ok, msg = await self.manager.sell_stock(
                await self.db.get_player_by_id("u1"), "S01", 1
            )
        self.assertTrue(ok, msg)
        self.assertIn("新闻冲击", msg)
        sell = (await self.manager._rows(
            "SELECT price FROM invest_orders WHERE user_id = 'u1' AND side = 'sell'"
        ))[0]
        self.assertAlmostEqual(float(sell["price"]), 42.0 * (1 - 0.008), places=4)

    async def test_margin_open_uses_impact_price(self):
        await self._player()
        self.manager._ensure_news = mock.AsyncMock()
        await self._insert_event(101)
        tick_patch, open_patch = self._at(100)
        with tick_patch, open_patch:
            ok, msg = await self.manager.open_margin(
                await self.db.get_player_by_id("u1"), "S01", 2, 10_000
            )
        self.assertTrue(ok, msg)
        self.assertIn("新闻冲击", msg)
        row = (await self.manager._rows(
            "SELECT entry_price FROM invest_margin_positions WHERE user_id = 'u1'"
        ))[0]
        self.assertAlmostEqual(float(row["entry_price"]), 42.0 * 1.008, places=4)

    # ===== 迁移 =====

    async def test_v68_columns_and_backfill(self):
        async with self.conn.execute("PRAGMA table_info(invest_events)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        self.assertIn("credibility", columns)
        self.assertIn("narrative_direction", columns)

        await self.conn.execute(
            "INSERT INTO invest_events "
            "(trade_day, slot, code, pattern, direction, base_bias, strength, title, detail) "
            "VALUES (1, 1, 'S01', 'single', -1, 0.0, 1.0, '旧消息', '旧细节')"
        )
        await self.conn.commit()
        await self.migration._ensure_invest_news_v68(self.conn)
        row = (await self.manager._rows(
            "SELECT narrative_direction FROM invest_events WHERE trade_day = 1 AND slot = 1"
        ))[0]
        self.assertEqual(int(row["narrative_direction"]), -1)
        # 重复迁移不应报错
        await self.migration._migrate_to_v68(self.conn, None)


if __name__ == "__main__":
    unittest.main()
