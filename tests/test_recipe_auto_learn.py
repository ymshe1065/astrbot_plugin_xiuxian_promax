"""丹方购买即参悟回归测试。

背景：丹方卷轴原先购买后存入储物戒，还需玩家手动 /学习丹方；
符图则购买即习得。现在丹方对齐符箓，丹阁/突破丹方购买后直接写入已学列表，
不再落储物戒，也不再重复扣费。
"""
import json
import unittest

import test_equipment_instances as fixtures
from test_equipment_instances import _imp, _FakeEvent

BASE = "炼气丹方"
SCROLL = "《炼气丹方》"
SHELF_PRICE = 50


class TestRecipeAutoLearn(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TestEquipmentInstances.asyncSetUp(self)
        self.handler = _imp("handlers.shop_handler").ShopHandler(self.db, {}, self.cm)
        self.recipe = self.cm.pill_recipes_data[1]
        self.recipe_id = int(self.recipe["id"])
        await self._seed_shelf(self.cm.items_data[SCROLL])

    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player

    async def _seed_shelf(self, data, stock=5):
        entry = {
            "name": SCROLL, "type": "recipe", "rank": data.get("rank", "灵品"),
            "original_price": SHELF_PRICE, "discount": 1.0,
            "price": SHELF_PRICE, "stock": stock, "data": {**data},
        }
        await self.db.update_shop_data("recipe_pavilion", 1, [entry])

    async def _buy(self, player, text):
        event = _FakeEvent(player.user_id, player.user_id, f"/购买 {text}")
        self.handler._remember_viewed_pavilion(event, "recipe_pavilion")
        arg = text.split(" ", 1)[0]
        results = [r async for r in self.handler.handle_buy.__wrapped__(self.handler, player, event, arg)]
        return "\n".join(results)

    async def test_buy_recipe_learns_immediately(self):
        player = await self._player(gold=1000)
        text = await self._buy(player, SCROLL)
        self.assertIn("已当场参悟", text, text)

        player = await self.db.get_player_by_id(player.user_id)
        self.assertIn(self.recipe_id, json.loads(player.learned_recipes or "[]"))
        # 卷轴不再落储物戒
        self.assertNotIn(SCROLL, player.get_storage_ring_items())
        # 只扣一次货架价
        self.assertEqual(player.gold, 1000 - SHELF_PRICE)

    async def test_buy_quantity_clamped_to_one(self):
        player = await self._player(gold=1000)
        text = await self._buy(player, f"{SCROLL} 3")
        self.assertIn("丹方每次限购 1 份", text, text)

        player = await self.db.get_player_by_id(player.user_id)
        self.assertIn(self.recipe_id, json.loads(player.learned_recipes or "[]"))
        self.assertEqual(player.gold, 1000 - SHELF_PRICE)
        _, shelf = await self.db.get_shop_data("recipe_pavilion")
        self.assertEqual(int(shelf[0]["stock"]), 4)

    async def test_already_learned_does_not_charge(self):
        player = await self._player(gold=1000)
        await self._buy(player, SCROLL)
        player = await self.db.get_player_by_id(player.user_id)
        gold_after_first = player.gold

        text = await self._buy(player, SCROLL)
        self.assertIn("已经学会", text, text)
        player = await self.db.get_player_by_id(player.user_id)
        self.assertEqual(player.gold, gold_after_first)
        _, shelf = await self.db.get_shop_data("recipe_pavilion")
        self.assertEqual(int(shelf[0]["stock"]), 4)

    async def test_learn_recipe_still_consumes_scroll_for_other_sources(self):
        """其他途径获得的卷轴仍可 /学习丹方，且不会重复收灵石。"""
        player = await self._player(gold=1000)
        ok, msg = await self.handler.storage_ring_manager.store_item(player, SCROLL, 1)
        self.assertTrue(ok, msg)
        player = await self.db.get_player_by_id(player.user_id)

        manager = _imp("managers.alchemy_manager").AlchemyManager(
            self.db, self.cm, self.handler.storage_ring_manager
        )
        ok, msg = await manager.learn_recipe(player, SCROLL)
        self.assertTrue(ok, msg)
        player = await self.db.get_player_by_id(player.user_id)
        self.assertIn(self.recipe_id, json.loads(player.learned_recipes or "[]"))
        self.assertNotIn(SCROLL, player.get_storage_ring_items())
        self.assertEqual(player.gold, 1000)


if __name__ == "__main__":
    unittest.main()
