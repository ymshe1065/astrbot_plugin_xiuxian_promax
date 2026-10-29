# tests/test_gift_gold.py
# 赠予灵石（直接转账）回归测试
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp, _FakeEvent


class TestGiftGold(unittest.IsolatedAsyncioTestCase):
    """ /赠予 @人 灵石 数量 应直接完成原子转账。 """

    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)
        self.mod = _imp("handlers.storage_ring_handler")
        # 夹具中的 At/Plain 是占位类，替换为可实例化的假组件以便解析消息链
        self.mod.At = fixtures._FakeAt
        self.mod.Plain = fixtures._FakePlain
        self.Handler = self.mod.StorageRingHandler
        self.handler = self.Handler(self.db, self.cm)

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    def _event(self, target_id, text):
        return _FakeEvent(
            "sender",
            "sender",
            components=[fixtures._FakeAt(target_id), fixtures._FakePlain(text)],
        )

    async def _gift(self, sender, target_id, text):
        event = self._event(target_id, text)
        return [
            str(r)
            async for r in self.handler.handle_gift_item.__wrapped__(
                self.handler, sender, event, text
            )
        ]

    async def test_gift_gold_transfers_directly(self):
        sender = await self._player("sender", gold=5000)
        await self._player("receiver", gold=100)

        results = await self._gift(sender, "receiver", "灵石 1200")

        self.assertTrue(any("转账成功" in r for r in results), results)
        sender = await self.db.get_player_by_id("sender")
        receiver = await self.db.get_player_by_id("receiver")
        self.assertEqual(sender.gold, 3800)
        self.assertEqual(receiver.gold, 1300)
        # 直接转账不产生待接收请求
        self.assertIsNone(await self.db.ext.get_pending_gift("receiver"))

    async def test_gift_gold_without_count_defaults_to_one(self):
        sender = await self._player("sender", gold=10)
        await self._player("receiver", gold=0)

        results = await self._gift(sender, "receiver", "灵石")

        self.assertTrue(any("转账成功" in r for r in results), results)
        self.assertEqual((await self.db.get_player_by_id("sender")).gold, 9)
        self.assertEqual((await self.db.get_player_by_id("receiver")).gold, 1)

    async def test_gift_gold_insufficient_keeps_balances(self):
        sender = await self._player("sender", gold=100)
        await self._player("receiver", gold=50)

        results = await self._gift(sender, "receiver", "灵石 500")

        self.assertTrue(any("灵石不足" in r for r in results), results)
        self.assertEqual((await self.db.get_player_by_id("sender")).gold, 100)
        self.assertEqual((await self.db.get_player_by_id("receiver")).gold, 50)
        self.assertIsNone(await self.db.ext.get_pending_gift("receiver"))

    async def test_gift_gold_to_self_rejected(self):
        sender = await self._player("sender", gold=1000)

        results = await self._gift(sender, "sender", "灵石 100")

        self.assertTrue(any("不能赠予" in r for r in results), results)
        self.assertEqual((await self.db.get_player_by_id("sender")).gold, 1000)


if __name__ == "__main__":
    unittest.main()
