"""储物戒嵌套事务和坊市失败回滚使用真实 SQLite 验证。"""
import asyncio
import unittest
from unittest import mock

import test_equipment_instances as fixtures


class TestEconomyAtomicity(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TestEquipmentInstances.asyncSetUp
    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    async def test_ring_operations_do_not_commit_outer_transaction(self):
        p = await self._player(gold=1000)
        await self.ring.store_item(p, "玄铁", 4)
        for operation in ("store_item", "retrieve_item", "discard_item", "recycle_item"):
            with self.subTest(operation=operation):
                before = await self.db.get_player_by_id(p.user_id)
                with self.assertRaisesRegex(RuntimeError, "后续步骤失败"):
                    async with self.db.transaction():
                        ok, message = await getattr(self.ring, operation)(before, "玄铁", 1)
                        self.assertTrue(ok, message)
                        raise RuntimeError("后续步骤失败")
                after = await self.db.get_player_by_id(p.user_id)
                self.assertEqual(after.get_storage_ring_items().get("玄铁"), 4)
                self.assertEqual(after.gold, 1000)

    async def test_rejected_listing_preserves_materials(self):
        seller = await self._player("seller")
        await self.ring.store_item(seller, "玄铁", 3)
        await self.db.conn.execute(
            "CREATE TRIGGER reject_listing BEFORE INSERT ON auction_items "
            "BEGIN SELECT RAISE(ABORT, '模拟挂单写入失败'); END"
        )
        auction = self.AuctionManager(self.db, self.ring, {})
        ok, _ = await auction.list_item(seller, "玄铁", 2, 100)
        self.assertFalse(ok)
        fresh = await self.db.get_player_by_id("seller")
        self.assertEqual(fresh.get_storage_ring_items().get("玄铁"), 3)
        async with self.db.conn.execute("SELECT COUNT(*) FROM auction_items") as cursor:
            self.assertEqual((await cursor.fetchone())[0], 0)

    async def test_invalid_storage_counts_cannot_change_inventory(self):
        p = await self._player()
        await self.ring.store_item(p, "玄铁", 3)
        for operation in ("store_item", "retrieve_item", "discard_item"):
            for count in (0, -1):
                with self.subTest(operation=operation, count=count):
                    ok, _ = await getattr(self.ring, operation)(p, "玄铁", count)
                    self.assertFalse(ok)
                    fresh = await self.db.get_player_by_id(p.user_id)
                    self.assertEqual(fresh.get_storage_ring_items().get("玄铁"), 3)

    async def test_equipment_listing_joins_outer_transaction(self):
        seller = await self._player("seller")
        await self.ring.store_item(seller, self.weapon, 1)
        auction = self.AuctionManager(self.db, self.ring, {})
        with self.assertRaisesRegex(RuntimeError, "后续步骤失败"):
            async with self.db.transaction():
                ok, message = await auction.list_item(seller, self.weapon, 1, 100)
                self.assertTrue(ok, message)
                raise RuntimeError("后续步骤失败")
        fresh = await self.db.get_player_by_id("seller")
        self.assertEqual(len(self.im.list_instances(fresh, "storage", self.weapon)), 1)
        async with self.db.conn.execute("SELECT COUNT(*) FROM auction_items") as cursor:
            self.assertEqual((await cursor.fetchone())[0], 0)

    async def test_concurrent_managers_cannot_list_same_pill_twice(self):
        seller = await self._player("seller")
        seller.set_pills_inventory({"筑基丹|一般": 1})
        await self.db.update_player(seller)
        managers = [self.AuctionManager(self.db, self.ring, {}) for _ in range(2)]
        results = await asyncio.gather(*(
            manager.list_item(seller, "筑基丹", 1, 100)
            for manager in managers
        ))
        self.assertEqual(sum(ok for ok, _ in results), 1, results)
        async with self.db.conn.execute("SELECT SUM(count) FROM auction_items") as cursor:
            self.assertEqual((await cursor.fetchone())[0], 1)

    async def test_failed_market_tick_rolls_back_prices_and_checkpoint(self):
        manager = fixtures._imp("managers.invest_manager").InvestManager(self.db, {}, None)
        await manager.ensure_ready()
        await manager._set_last_tick(30)
        original = await manager._load_products()
        write_price = manager._write_price
        writes = 0

        async def fail_second_price(*args):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise RuntimeError("第二个标的写入失败")
            return await write_price(*args)

        moment = manager.tick_time(31)
        with mock.patch.object(manager, "_write_price", side_effect=fail_second_price):
            self.assertEqual(await manager.settle_to(moment), 0)
        self.assertEqual(await manager._load_products(), original)
        self.assertEqual(await manager._get_last_tick(), 30)
        self.assertEqual(await manager._scalar("SELECT COUNT(*) FROM invest_prices WHERE tick = 31"), 0)
        self.assertEqual(await manager.settle_to(moment), 1)
        self.assertEqual(await manager._get_last_tick(), 31)
        prices = await manager._load_products()
        self.assertEqual(await manager.settle_to(moment), 0)
        self.assertEqual(await manager._load_products(), prices)

    async def test_checkpoint_failure_rolls_back_entire_market_tick(self):
        manager = fixtures._imp("managers.invest_manager").InvestManager(self.db, {}, None)
        await manager.ensure_ready()
        await manager._set_last_tick(30)
        original = await manager._load_products()
        await self.db.conn.execute(
            "CREATE TRIGGER reject_tick BEFORE UPDATE OF value ON system_config "
            "WHEN NEW.key = 'invest_last_tick' "
            "BEGIN SELECT RAISE(ABORT, '模拟检查点写入失败'); END"
        )
        # settle_to 的既有契约：失败盘留待重试，返回本轮成功盘数。
        self.assertEqual(await manager.settle_to(manager.tick_time(31)), 0)
        self.assertEqual(await manager._load_products(), original)
        self.assertEqual(await manager._get_last_tick(), 30)

    async def test_concurrent_market_managers_settle_each_tick_once(self):
        cls = fixtures._imp("managers.invest_manager").InvestManager
        managers = [cls(self.db, {}, None), cls(self.db, {}, None)]
        for manager in managers:
            await manager.ensure_ready()
        await managers[0]._set_last_tick(30)
        products = await managers[0]._load_products()
        moment = managers[0].tick_time(31)
        results = await asyncio.gather(*(manager.settle_to(moment) for manager in managers))
        self.assertEqual(sum(results), 1)
        rows = await managers[0]._load_products()
        for before, after in zip(products, rows):
            self.assertEqual(after["prev_close"], before["price"])

    async def test_failed_dividend_rolls_back_journal_and_can_retry(self):
        p = await self._player(gold=1000)
        manager = fixtures._imp("managers.invest_manager").InvestManager(self.db, {}, None)
        await manager.ensure_ready()
        await manager._set_last_tick(31)
        await self.db.conn.execute(
            "INSERT INTO invest_holdings (user_id, code, shares, cost, hold_since, last_buy) "
            "VALUES (?, 'S01', 10000, 420000, 0, 0)", (p.user_id,)
        )
        await self.db.conn.execute(
            "CREATE TRIGGER reject_dividend BEFORE UPDATE OF gold ON players "
            "BEGIN SELECT RAISE(ABORT, '模拟派息入账失败'); END"
        )
        moment = manager.tick_time(32)
        self.assertEqual(await manager.settle_to(moment), 0)
        self.assertEqual(await manager._scalar("SELECT COUNT(*) FROM invest_dividends"), 0)
        self.assertEqual((await self.db.get_player_by_id(p.user_id)).gold, 1000)
        await self.db.conn.execute("DROP TRIGGER reject_dividend")
        self.assertEqual(await manager.settle_to(moment), 1)
        dividend = await manager._scalar("SELECT SUM(amount) FROM invest_dividends")
        self.assertGreater(dividend, 0)
        self.assertEqual((await self.db.get_player_by_id(p.user_id)).gold, 1000 + dividend)
