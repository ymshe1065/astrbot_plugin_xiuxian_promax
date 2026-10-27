# 符箓体系回归测试
import asyncio
import json
import os
import sys
import tempfile
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

    def exception(self, *a, **k):
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


class TestTalismanSystem(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _imp("data")
        cls.ConfigManager = _imp("config_manager").ConfigManager
        cls.Player = _imp("models").Player
        cls.TalismanManager = _imp("managers.talisman_manager").TalismanManager
        cls.StorageRingManager = _imp("core").StorageRingManager
        cls.CombatManager = _imp("managers.combat_manager").CombatManager
        cls.AuctionManager = _imp("managers.auction_manager").AuctionManager
        cls.ShopManager = _imp("core.shop_manager").ShopManager
        cls.ItemRules = _imp("core.item_rules")
        cls.config_manager = cls.ConfigManager(ROOT)

    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = self.data.DataBase(self.db_path)
        await self.db.connect()
        await self.data.MigrationManager(self.db.conn, self.config_manager).migrate()
        self.storage = self.StorageRingManager(self.db, self.config_manager)
        self.talisman = self.TalismanManager(self.db, self.config_manager, self.storage)

    async def asyncTearDown(self):
        await self.db.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _player(self, **kwargs):
        kwargs.setdefault("level_index", 10)
        return self.Player(
            user_id="u1", user_name="符师", gold=100000, life_skill="符箓师", **kwargs
        )

    async def test_profiles_cover_existing_and_new_talismans(self):
        talismans = [
            name for name, item in self.config_manager.items_data.items()
            if item.get("type") == "符箓"
        ]
        self.assertGreaterEqual(len(talismans), 77)
        self.assertEqual(self.talisman.get_talisman_profile("山河符")["category"], "防御")
        self.assertEqual(self.talisman.get_talisman_profile("稳魂符")["category"], "炼丹")
        profile = self.talisman.get_talisman_profile("寻宝符")
        self.assertIn("exploration_reward_multiplier", profile["effects"])

    async def test_use_defense_talisman_affects_combat(self):
        player = self._player(experience=100000, physical_defense=100, magic_defense=100)
        player.storage_ring_items = json.dumps({"山河符": 1}, ensure_ascii=False)
        await self.db.create_player(player)
        ok, msg = await self.talisman.use_talisman(player, "山河符")
        self.assertTrue(ok)
        fresh = await self.db.get_player_by_id("u1")
        stats = self.CombatManager.build_player_combat_stats(fresh, self.config_manager, db=self.db)
        self.assertGreater(stats.defense, 0)
        base = _imp("core.equipment_balance").realm_attributes(fresh, self.config_manager)
        self.assertGreater(stats.defense, base["physical_defense"] + base["magic_defense"])

    async def test_alchemy_and_exploration_bonus_keys(self):
        player = self._player()
        player.storage_ring_items = json.dumps({"稳魂符": 1, "寻宝符": 1}, ensure_ascii=False)
        await self.db.create_player(player)
        await self.talisman.use_talisman(player, "稳魂符")
        await self.talisman.use_talisman(player, "寻宝符")
        self.assertGreater(self.TalismanManager.get_active_bonus(player, "alchemy_success_bonus"), 0)
        self.assertGreater(self.TalismanManager.get_active_bonus(player, "exploration_reward_multiplier"), 0)
        self.assertGreater(self.TalismanManager.get_active_bonus(player, "drop_chance_bonus"), 0)

    async def test_craft_consumes_materials_and_outputs_talisman(self):
        from unittest.mock import patch
        player = self._player(level_index=0)
        mats = [
            m for m in self.config_manager.material_catalog.values()
            if m["category"] == "灵植" and m["tier"] == "黄阶"
        ][:1]
        player.storage_ring_items = json.dumps({mats[0]["name"]: 5}, ensure_ascii=False)
        await self.db.create_player(player)
        with patch(f"{PKG}.managers.talisman_manager.random.randint", return_value=1):
            ok, msg = await self.talisman.craft_talisman(player, "稳魂符", 2)
        self.assertTrue(ok, msg)
        fresh = await self.db.get_player_by_id("u1")
        inventory = fresh.get_storage_ring_items()
        self.assertGreaterEqual(inventory.get("稳魂符", 0), 1)
        self.assertLess(inventory.get(mats[0]["name"], 0), 5)

    async def test_talisman_is_tradeable_storage_item(self):
        player = self._player()
        await self.db.create_player(player)
        ok, msg = await self.storage.store_item(player, "寻宝符", 1)
        self.assertTrue(ok, msg)
        self.assertTrue(self.storage.has_item(player, "寻宝符", 1))
        self.assertFalse(self.storage.is_pill("寻宝符"))

    async def test_talisman_auction_roundtrip(self):
        seller = self.Player(user_id="seller", user_name="卖家", gold=0)
        seller.storage_ring_items = json.dumps({"寻宝符": 2}, ensure_ascii=False)
        buyer = self.Player(user_id="buyer", user_name="买家", gold=5000)
        await self.db.create_player(seller)
        await self.db.create_player(buyer)

        auction = self.AuctionManager(self.db, self.storage, {})
        ok, msg = await auction.list_item(seller, "寻宝符", 1, 1000)
        self.assertTrue(ok, msg)
        market = await auction.get_market_items(10)
        self.assertEqual(market[0]["item_name"], "寻宝符")

        buyer = await self.db.get_player_by_id("buyer")
        ok, msg = await auction.buy_item(buyer, market[0]["id"])
        self.assertTrue(ok, msg)
        buyer = await self.db.get_player_by_id("buyer")
        self.assertEqual(buyer.get_storage_ring_items().get("寻宝符"), 1)
        seller = await self.db.get_player_by_id("seller")
        self.assertEqual(seller.gold, 950)

    async def test_talisman_can_enter_shop_and_gift_flow(self):
        shop = self.ShopManager({}, self.config_manager)
        item = shop.find_item_by_name("寻宝符")
        self.assertIsNotNone(item)
        self.assertEqual(self.ItemRules.normalize_shop_type(item), "material")
        all_items = shop.get_all_items_for_display(9999)
        self.assertTrue(any(i["name"] == "寻宝符" for i in all_items))

        sender = self.Player(user_id="sender", user_name="送礼者")
        sender.storage_ring_items = json.dumps({"寻宝符": 1}, ensure_ascii=False)
        receiver = self.Player(user_id="receiver", user_name="收礼者")
        await self.db.create_player(sender)
        await self.db.create_player(receiver)
        ok, _ = await self.storage.retrieve_item(sender, "寻宝符", 1)
        self.assertTrue(ok)
        await self.db.ext.create_pending_gift(
            receiver_id="receiver", sender_id="sender", sender_name="送礼者",
            item_name="寻宝符", count=1, expires_hours=24,
        )
        gift = await self.db.ext.get_pending_gift("receiver")
        self.assertIsNotNone(gift)
        self.assertEqual(gift["item_name"], "寻宝符")
        ok, msg = await self.storage.store_item(receiver, gift["item_name"], gift["count"])
        self.assertTrue(ok, msg)
        receiver = await self.db.get_player_by_id("receiver")
        self.assertEqual(receiver.get_storage_ring_items().get("寻宝符"), 1)


if __name__ == "__main__":
    unittest.main()
