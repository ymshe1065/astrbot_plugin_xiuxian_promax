# tests/test_market_npc_offers.py
# 坊市NPC问价回归测试：随机区间、接受卖出、拒绝等待、过期重新排期
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

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


class TestMarketNpcOffers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.AuctionManager = _imp("managers.auction_manager").AuctionManager
        self.Player = _imp("models").Player

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        await self.db.create_player(self.Player(user_id="seller", gold=0))

        self.manager = self.AuctionManager(
            self.db,
            None,
            {
                "MARKET_NPC": {
                    "ENABLED": True,
                    "MIN_INTERVAL": 1,
                    "MAX_INTERVAL": 1,
                    "MIN_RATE": 0.8,
                    "MAX_RATE": 1.2,
                    "OFFER_EXPIRE": 3600,
                    "CHECK_INTERVAL": 5,
                }
            },
        )

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _insert_listing(self, price: int = 50, count: int = 2, next_offer_time: int = None):
        now = int(time.time())
        await self.conn.execute(
            "INSERT INTO auction_items "
            "(seller_id, item_name, count, price, create_time, next_offer_time) "
            "VALUES ('seller', '测试灵草', ?, ?, ?, ?)",
            (count, price, now, next_offer_time if next_offer_time is not None else now - 1),
        )
        await self.conn.commit()
        async with self.conn.execute("SELECT last_insert_rowid()") as cursor:
            return (await cursor.fetchone())[0]

    async def _offers(self):
        async with self.conn.execute("SELECT * FROM auction_npc_offers ORDER BY id") as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def _listing(self, auction_id: int):
        async with self.conn.execute("SELECT * FROM auction_items WHERE id = ?", (auction_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def test_offer_price_within_configured_range_and_accept(self):
        now = int(time.time())
        auction_id = await self._insert_listing(price=50, count=2)

        generated, expired = await self.manager.process_npc_offers(now)
        self.assertEqual((generated, expired), (1, 0))

        offers = await self._offers()
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer["auction_id"], auction_id)
        self.assertGreaterEqual(offer["offer_price"], 40)
        self.assertLessEqual(offer["offer_price"], 60)
        self.assertEqual((await self._listing(auction_id))["next_offer_time"], -1)

        player = await self.db.get_player_by_id("seller")
        ok, msg = await self.manager.accept_offer(player, offer["id"])
        self.assertTrue(ok, msg)

        player = await self.db.get_player_by_id("seller")
        self.assertEqual(player.gold, offer["offer_price"] * 2)
        self.assertIsNone(await self._listing(auction_id))
        self.assertEqual(await self._offers(), [])

    async def test_decline_schedules_next_offer(self):
        now = int(time.time())
        auction_id = await self._insert_listing()
        await self.manager.process_npc_offers(now)
        offer = (await self._offers())[0]

        player = await self.db.get_player_by_id("seller")
        ok, msg = await self.manager.decline_offer(player, offer["id"])
        self.assertTrue(ok, msg)

        offers = await self._offers()
        self.assertEqual(offers[0]["status"], "rejected")
        self.assertGreater((await self._listing(auction_id))["next_offer_time"], now)

    async def test_expired_offer_is_replaced_after_interval(self):
        now = int(time.time())
        auction_id = await self._insert_listing()
        await self.manager.process_npc_offers(now)
        offer = (await self._offers())[0]

        await self.conn.execute(
            "UPDATE auction_npc_offers SET expire_time = ? WHERE id = ?", (now - 1, offer["id"])
        )
        await self.conn.commit()

        generated, expired = await self.manager.process_npc_offers(now)
        self.assertEqual((generated, expired), (0, 1))
        offers = await self._offers()
        self.assertEqual(offers[0]["status"], "expired")
        next_time = (await self._listing(auction_id))["next_offer_time"]
        self.assertGreater(next_time, now)

        generated, expired = await self.manager.process_npc_offers(next_time)
        self.assertEqual((generated, expired), (1, 0))
        pending = [o for o in await self._offers() if o["status"] == "pending"]
        self.assertEqual(len(pending), 1)


if __name__ == "__main__":
    unittest.main()
