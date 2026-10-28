# tests/test_auction_house.py
# 拍卖会回归测试：排期、预告、报名、竞价退款、结算发放、AI 降级
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
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


class TestAuctionHouse(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.ConfigManager = _imp("config_manager").ConfigManager
        self.StorageRingManager = _imp("core.storage_ring_manager").StorageRingManager
        self.AuctionHouseManager = _imp("managers.auction_house_manager").AuctionHouseManager
        self.AuctionAIBroadcaster = _imp("utils.auction_ai").AuctionAIBroadcaster
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
        self.config_manager = self.ConfigManager(ROOT)
        self.storage = self.StorageRingManager(self.db, self.config_manager)
        self.manager = self.AuctionHouseManager(
            self.db,
            self.storage,
            self.config_manager,
            {
                "AUCTION_HOUSE": {
                    "ENABLED": True,
                    "CYCLE_DAYS": 3,
                    "ANNOUNCE_START_HOUR": 10,
                    "ANNOUNCE_END_HOUR": 21,
                    "EVENT_START_HOUR": 22,
                    "ENTRY_FEE": 500,
                    "LOT_DURATION": 600,
                    "BID_COUNTDOWN": 60,
                    "LOT_GAP": 10,
                    "MIN_INCREMENT_PCT": 0.05,
                    "LOT_COUNT": 10,
                    "AI_ENABLED": False,
                }
            },
        )

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _player(self, uid: str, gold: int = 5000, name: str = "测试道友"):
        await self.db.create_player(self.Player(user_id=uid, gold=gold, user_name=name))
        return await self.db.get_player_by_id(uid)

    async def _insert_event(self, phase: str = "live", *, now: int = None,
                            announce_start: int = None, event_start: int = None,
                            current_lot: int = 0, lot_count: int = 1) -> dict:
        now = int(now or time.time())
        announce_start = int(announce_start if announce_start is not None else now - 3600)
        event_start = int(event_start if event_start is not None else now - 60)
        announce_date = datetime.fromtimestamp(announce_start).strftime("%Y-%m-%d")
        cursor = await self.conn.execute(
            """INSERT INTO auction_house_events
               (cycle_no, cycle_start, announce_date, announce_start, event_start,
                event_end, phase, lot_count, current_lot, last_reminder_slot,
                created_at, updated_at)
               VALUES (1, ?, ?, ?, ?, 0, ?, ?, ?, '', ?, ?)""",
            (now - 86400, announce_date, announce_start, event_start,
             phase, lot_count, current_lot, now, now),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT * FROM auction_house_events WHERE id=?", (cursor.lastrowid,)
        ) as cur:
            return dict(await cur.fetchone())

    async def _insert_lot(self, event_id: int, *, lot_no: int = 1, category: str = "pill",
                          name: str = "筑基丹", rank: str = "灵品", quality: str = "良好",
                          count: int = 1, start_price: int = 1000, status: str = "live",
                          lot_start: int = None, bid_deadline: int = 0,
                          highest_bidder: str = "", highest_bid: int = 0,
                          payload: dict = None) -> dict:
        now = int(time.time())
        lot_start = int(lot_start if lot_start is not None else now - 10)
        preview = f"{rank}·{'丹药' if category == 'pill' else '拍品'} ×{count}"
        cursor = await self.conn.execute(
            """INSERT INTO auction_house_lots
               (event_id, lot_no, category, preview_label, item_name, item_rank,
                item_quality, count, payload_json, start_price, current_price, status,
                lot_start, bid_deadline, highest_bidder, highest_bid, winner_id,
                winning_price, settled_at, delivery_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, 0, 'none', ?)""",
            (event_id, lot_no, category, preview, name, rank, quality, count,
             __import__("json").dumps(payload or {}, ensure_ascii=False),
             start_price, max(start_price, highest_bid), status, lot_start,
             bid_deadline, highest_bidder, highest_bid, now),
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT * FROM auction_house_lots WHERE id=?", (cursor.lastrowid,)
        ) as cur:
            return dict(await cur.fetchone())

    async def _entry(self, event_id: int, uid: str):
        await self.conn.execute(
            "INSERT OR IGNORE INTO auction_house_entries (event_id, user_id, entry_fee, created_at) VALUES (?, ?, 500, ?)",
            (event_id, uid, int(time.time())),
        )
        await self.conn.commit()

    async def test_schedule_same_day_and_preview_hides_names(self):
        now = int(time.time())
        event = await self.manager.ensure_event(now)
        self.assertEqual(event["phase"], "scheduled")
        announce_dt = datetime.strptime(event["announce_date"], "%Y-%m-%d")
        start_dt = datetime.fromtimestamp(int(event["event_start"]))
        self.assertEqual(announce_dt.date(), start_dt.date())
        self.assertEqual(start_dt.hour, 22)

        announce_start = self.manager._at_hour(now, self.manager.announce_start_hour)
        event_start = self.manager._at_hour(announce_start, self.manager.event_start_hour)
        if event_start <= announce_start:
            event_start += 86400
        await self.conn.execute(
            "UPDATE auction_house_events SET announce_start=?, event_start=?, announce_date=? WHERE id=?",
            (announce_start, event_start, datetime.fromtimestamp(announce_start).strftime("%Y-%m-%d"), event["id"]),
        )
        await self.conn.commit()

        queued = await self.manager.tick(announce_start + 30)
        self.assertGreaterEqual(queued, 1)
        event = await self.manager._fetchone(
            "SELECT * FROM auction_house_events WHERE id=?", (event["id"],)
        )
        self.assertEqual(event["phase"], "announcing")
        self.assertEqual(int(event["lot_count"]), 10)
        lots = await self.manager._fetchall(
            "SELECT * FROM auction_house_lots WHERE event_id=? ORDER BY lot_no", (event["id"],)
        )
        self.assertEqual(len(lots), 10)
        self.assertEqual(
            {lot["category"] for lot in lots},
            {"equipment", "talisman", "pill", "recipe", "technique", "main_technique"},
        )
        prices = [int(lot["start_price"]) for lot in lots]
        self.assertEqual(prices, sorted(prices))
        rank_tiers = [
            _imp("managers.auction_house_manager").RANK_TIER.get(lot["item_rank"], 1)
            for lot in lots
        ]
        self.assertEqual(rank_tiers, sorted(rank_tiers))
        for lot in lots:
            self.assertNotIn(lot["item_name"], lot["preview_label"])
            self.assertFalse(lot["preview_label"][0].isdigit())
        status = await self.manager.get_status()
        self.assertIn("拍品预告", status)

    async def test_register_deducts_once_and_rejects_duplicate(self):
        event = await self._insert_event("announcing", announce_start=int(time.time()) - 60,
                                         event_start=int(time.time()) + 3600)
        player = await self._player("u1", gold=1000)
        ok, _ = await self.manager.register(player)
        self.assertTrue(ok)
        fresh = await self.db.get_player_by_id("u1")
        self.assertEqual(fresh.gold, 500)
        ok, msg = await self.manager.register(fresh)
        self.assertFalse(ok)
        self.assertIn("已经报名", msg)
        entry = await self.manager._fetchone(
            "SELECT * FROM auction_house_entries WHERE event_id=? AND user_id=?",
            (event["id"], "u1"),
        )
        self.assertIsNotNone(entry)

        poor = await self._player("u2", gold=100)
        ok, msg = await self.manager.register(poor)
        self.assertFalse(ok)
        self.assertIn("灵石不足", msg)

    async def test_bid_outbid_refund_and_self_topup(self):
        now = int(time.time())
        event = await self._insert_event("live", now=now, current_lot=1, lot_count=1)
        await self._insert_lot(event["id"], start_price=1000, status="live", lot_start=now - 5)
        p1 = await self._player("p1", gold=5000, name="甲")
        p2 = await self._player("p2", gold=5000, name="乙")
        p3 = await self._player("p3", gold=5000, name="丙")
        await self._entry(event["id"], "p1")
        await self._entry(event["id"], "p2")

        ok, _ = await self.manager.bid(p1, "1000")
        self.assertTrue(ok)
        self.assertEqual((await self.db.get_player_by_id("p1")).gold, 4000)
        lot = await self.manager._fetchone(
            "SELECT * FROM auction_house_lots WHERE event_id=?", (event["id"],)
        )
        self.assertEqual(lot["highest_bidder"], "p1")
        self.assertEqual(lot["status"], "countdown")
        self.assertEqual(int(lot["bid_deadline"]), now + 60)

        ok, _ = await self.manager.bid(p2, "1100")
        self.assertTrue(ok)
        self.assertEqual((await self.db.get_player_by_id("p1")).gold, 5000)
        self.assertEqual((await self.db.get_player_by_id("p2")).gold, 3900)
        lot = await self.manager._fetchone(
            "SELECT * FROM auction_house_lots WHERE event_id=?", (event["id"],)
        )
        self.assertEqual(lot["highest_bidder"], "p2")
        self.assertEqual(int(lot["highest_bid"]), 1100)

        ok, msg = await self.manager.bid(p1, "1100")
        self.assertFalse(ok)
        self.assertIn("出价过低", msg)
        ok, msg = await self.manager.bid(p3, "2000")
        self.assertFalse(ok)
        self.assertIn("尚未报名", msg)

        ok, _ = await self.manager.bid(p2, "1200")
        self.assertTrue(ok)
        self.assertEqual((await self.db.get_player_by_id("p2")).gold, 3800)

    async def test_no_entries_cancels_at_start(self):
        now = int(time.time())
        event = await self._insert_event(
            "announcing", now=now, announce_start=now - 3600,
            event_start=now - 1, lot_count=0,
        )
        await self.manager.tick(now)
        event = await self.manager._fetchone(
            "SELECT * FROM auction_house_events WHERE id=?", (event["id"],)
        )
        self.assertEqual(event["phase"], "settled")
        messages = await self.manager.drain_messages()
        self.assertTrue(any("无人报名" in (m.get("fallback_text") or "") for m in messages))

    async def test_full_event_flow_from_announce_to_first_lot(self):
        now = int(time.time())
        event = await self._insert_event(
            "announcing", now=now, announce_start=now - 3600,
            event_start=now - 1, lot_count=0,
        )
        player = await self._player("flow1", gold=100000, name="竞拍者")
        await self._entry(event["id"], "flow1")

        await self.manager.tick(now)
        event = await self.manager._fetchone(
            "SELECT * FROM auction_house_events WHERE id=?", (event["id"],)
        )
        self.assertEqual(event["phase"], "live")
        self.assertEqual(int(event["current_lot"]), 1)
        lot = await self.manager._fetchone(
            "SELECT * FROM auction_house_lots WHERE event_id=? AND lot_no=1",
            (event["id"],),
        )
        self.assertEqual(lot["status"], "live")
        self.assertGreater(int(lot["id"]), 0)

        ok, msg = await self.manager.bid(player, str(lot["start_price"]))
        self.assertTrue(ok, msg)
        await self.manager.tick(now + 61)
        lot = await self.manager._fetchone(
            "SELECT * FROM auction_house_lots WHERE id=?", (lot["id"],)
        )
        self.assertEqual(lot["status"], "sold")
        self.assertEqual(lot["winner_id"], "flow1")

    async def test_self_topup_can_use_previous_bid(self):
        now = int(time.time())
        event = await self._insert_event("live", now=now, current_lot=1, lot_count=1)
        await self._insert_lot(event["id"], start_price=1000, status="live", lot_start=now - 5)
        p1 = await self._player("p1", gold=1050, name="甲")
        await self._entry(event["id"], "p1")

        ok, _ = await self.manager.bid(p1, "1000")
        self.assertTrue(ok)
        self.assertEqual((await self.db.get_player_by_id("p1")).gold, 50)

        ok, msg = await self.manager.bid(await self.db.get_player_by_id("p1"), "1050")
        self.assertTrue(ok, msg)
        self.assertEqual((await self.db.get_player_by_id("p1")).gold, 0)

    async def test_settlement_delivers_pill_and_passed_lot(self):
        now = int(time.time())
        event = await self._insert_event("live", now=now, current_lot=1, lot_count=1,
                                         event_start=now - 600)
        await self._insert_lot(event["id"], category="pill", name="筑基丹", quality="良好",
                               status="countdown", lot_start=now - 120,
                               bid_deadline=now - 1, highest_bidder="w1", highest_bid=1500)
        await self._player("w1", gold=0, name="赢家")
        await self._entry(event["id"], "w1")

        await self.manager.tick(now)
        lot = await self.manager._fetchone(
            "SELECT * FROM auction_house_lots WHERE event_id=?", (event["id"],)
        )
        self.assertEqual(lot["status"], "sold")
        self.assertEqual(lot["winner_id"], "w1")
        winner = await self.db.get_player_by_id("w1")
        self.assertEqual(winner.get_pills_inventory().get("筑基丹|良好"), 1)
        messages = await self.manager.drain_messages()
        self.assertTrue(any(m["kind"] == "result" for m in messages))

        event2 = await self._insert_event("live", now=now, current_lot=1, lot_count=1,
                                          event_start=now - 600)
        await self._insert_lot(event2["id"], category="pill", name="聚气丹", status="live",
                               lot_start=now - 601)
        await self.manager.tick(now)
        lot2 = await self.manager._fetchone(
            "SELECT * FROM auction_house_lots WHERE event_id=?", (event2["id"],)
        )
        self.assertEqual(lot2["status"], "passed")
        await self.manager.tick(now + 20)
        event2 = await self.manager._fetchone(
            "SELECT * FROM auction_house_events WHERE id=?", (event2["id"],)
        )
        self.assertEqual(event2["phase"], "settled")

    async def test_lot_detail_uses_quality_adjusted_rates(self):
        lot = {
            "item_name": "乾坤伞", "item_rank": "玄阶", "item_quality": "大师", "count": 1,
            "payload_json": __import__("json").dumps({
                "equipment_rates": {"physical_damage": 0.06, "magic_damage": 0.06},
            }),
        }
        text = "\n".join(self.manager._lot_detail_lines(lot))
        self.assertIn("·大师", text)
        self.assertIn("主属性（品质后）：物伤+6.9%、法伤+6.9%", text)

    async def test_equipment_delivery_keeps_quality_and_affixes(self):
        player = await self._player("eq1", gold=0)
        lot = {
            "id": 999, "category": "equipment", "item_name": "乾坤伞", "count": 1,
            "item_quality": "大师",
            "payload_json": '{"affixes":[{"name":"暴击率","value":5,"display":"暴击率+5%"}]}',
        }
        ok, msg = await self.manager._deliver_lot(lot, "eq1")
        self.assertTrue(ok, msg)
        fresh = await self.db.get_player_by_id("eq1")
        inst = [x for x in fresh.get_equipment_instances() if x.get("name") == "乾坤伞"]
        self.assertEqual(len(inst), 1)
        self.assertEqual(inst[0]["quality"], "大师")
        self.assertEqual(inst[0]["affixes"][0]["name"], "暴击率")

    async def test_start_price_is_around_twenty_percent_of_base_price(self):
        now = int(time.time())
        shop = _imp("core.shop_manager").ShopManager({}, self.config_manager)
        entries = [
            {"category": "pill", "tier": 3, "count": 1},
            {"category": "technique", "tier": 2, "count": 1},
            {"category": "main_technique", "tier": 4, "count": 1},
        ]
        for entry in entries:
            lot = self.manager._generate_lot(1, 1, entry, set(), 0, now)
            self.assertIsNotNone(lot)
            found = shop.find_item_by_name(lot["item_name"])
            self.assertIsNotNone(found, lot["item_name"])
            base = int(found["price"])
            self.assertGreaterEqual(lot["start_price"], int(base * 0.16))
            self.assertLessEqual(lot["start_price"], int(base * 0.24) + 1)

    async def test_technique_delivery_goes_to_storage_ring(self):
        await self._player("tech1", gold=0)
        lot = {
            "id": 1000, "category": "technique", "item_name": "黄阶功法：基础剑诀",
            "count": 1, "item_quality": "一般", "payload_json": "{}",
        }
        ok, msg = await self.manager._deliver_lot(lot, "tech1")
        self.assertTrue(ok, msg)
        fresh = await self.db.get_player_by_id("tech1")
        self.assertEqual(fresh.get_storage_ring_items().get("黄阶功法：基础剑诀"), 1)

    async def test_ai_fallback_without_provider(self):
        class FakeContext:
            async def get_current_chat_provider_id(self, umo=None):
                return None

        ai = self.AuctionAIBroadcaster(FakeContext(), enabled=True, bid_enabled=True)
        text = await ai.compose("start", {"facts": "事实"}, "模板兜底", "umo")
        self.assertEqual(text, "模板兜底")
        disabled = self.AuctionAIBroadcaster(FakeContext(), enabled=False)
        text = await disabled.compose("bid", {"facts": "事实"}, "模板兜底", "umo")
        self.assertEqual(text, "模板兜底")

        class FakeResp:
            completion_text = "仙乐齐鸣，万宝阁灯火通明。"

        class FakeProviderContext:
            async def get_current_chat_provider_id(self, umo=None):
                return "provider-1"

            async def llm_generate(self, chat_provider_id=None, prompt=""):
                return FakeResp()

        ai2 = self.AuctionAIBroadcaster(FakeProviderContext(), enabled=True)
        text = await ai2.compose("start", {"facts": "事实"}, "模板兜底", "umo")
        self.assertIn("仙乐齐鸣", text)
        self.assertIn("事实", text)

    async def test_migration_tables_idempotent(self):
        migration = _imp("data.migration")
        await migration._ensure_auction_house_v47(self.conn)
        await migration._ensure_auction_house_v47(self.conn)
        async with self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'auction_house_%'"
        ) as cur:
            names = {r[0] for r in await cur.fetchall()}
        self.assertEqual(names, {
            "auction_house_events", "auction_house_lots", "auction_house_entries",
            "auction_house_bids", "auction_house_messages",
        })


if __name__ == "__main__":
    unittest.main()
