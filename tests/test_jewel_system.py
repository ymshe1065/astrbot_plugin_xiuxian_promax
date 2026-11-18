# 珠宝体系与分档赌石回归测试
import json
import random
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_equipment_instances import _imp, ROOT  # noqa: E402

ConfigManager = _imp("config_manager").ConfigManager
Player = _imp("models").Player
JewelManager = _imp("core.jewel_manager").JewelManager
JewelRules = _imp("core.jewel_manager").JewelRules
ShopManager = _imp("core.shop_manager").ShopManager
GambleManager = _imp("managers.gamble_manager").GambleManager
AuctionHouseManager = _imp("managers.auction_house_manager").AuctionHouseManager
WorldBossManager = _imp("managers.world_boss_manager").WorldBossManager
StorageRingManager = _imp("core").StorageRingManager


class JewelTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cm = ConfigManager(ROOT)
        cls.ring = StorageRingManager(None, cls.cm)
        cls.manager = JewelManager(None, cls.cm, cls.ring)

    def new_player(self):
        return Player(user_id="jewel_u1", user_name="珠玉道人", level_index=20)


class TestJewelRules(JewelTestBase):
    def test_quality_scales_affixes_and_price(self):
        template = self.manager.template_by_name("破军晶")
        low = self.manager.roll_jewel(template=template, quality="一般")
        high = self.manager.roll_jewel(template=template, quality="大师")
        self.assertEqual(low["rank"], "黄品")
        self.assertEqual(high["rank"], "帝品")
        self.assertGreater(high["price"], low["price"])
        low_values = [a["value"] for a in low["affixes"]]
        high_values = [a["value"] for a in high["affixes"]]
        self.assertGreater(sum(high_values), sum(low_values))

    def test_socket_capacity_and_expand_cost(self):
        self.assertEqual(JewelRules.socket_base("一般"), 1)
        self.assertEqual(JewelRules.socket_base("良好"), 2)
        self.assertEqual(JewelRules.socket_base("极佳"), 3)
        self.assertEqual(JewelRules.socket_base("大师"), 4)
        inst = {"quality": "良好", "socket_extra": 0}
        self.assertEqual(JewelRules.next_expand_cost(inst), 2)
        inst["socket_extra"] = 1
        self.assertEqual(JewelRules.next_expand_cost(inst), 4)
        inst["socket_extra"] = 10
        self.assertIsNone(JewelRules.next_expand_cost(inst))
        self.assertEqual(JewelRules.socket_capacity(inst), 6)

    def test_socket_category_restriction(self):
        player = self.new_player()
        attack_gem = self.manager.add_jewel(player, self.manager.roll_jewel(
            template=self.manager.template_by_name("破军晶"), quality="良好"
        ))
        armor = {"id": 1, "name": "玄铁甲", "type": "armor", "quality": "良好"}
        weapon = {"id": 2, "name": "玄铁剑", "type": "weapon", "quality": "一般"}
        ok, msg = self.manager.can_socket(player, armor, attack_gem)
        self.assertFalse(ok)
        self.assertIn("攻击类装备", msg)
        ok, _ = self.manager.can_socket(player, weapon, attack_gem)
        self.assertTrue(ok)

    def test_socket_unsocket_roundtrip_and_cost(self):
        player = self.new_player()
        weapon = {"id": 2, "name": "玄铁剑", "type": "weapon", "quality": "一般", "sockets": []}
        gem = self.manager.add_jewel(player, self.manager.roll_jewel(
            template=self.manager.template_by_name("破军晶"), quality="大师"
        ))
        ok, msg = self.manager.socket_gem_into_instance(player, weapon, gem)
        self.assertTrue(ok)
        self.assertEqual(len(weapon["sockets"]), 1)
        self.assertEqual(self.manager.get_inventory(player), [])
        rates, _ = self.manager.instance_bonuses(weapon)
        self.assertGreater(sum(rates.values()), 0)
        ok, msg, cost = self.manager.unsocket_gem(player, weapon, 1)
        self.assertTrue(ok)
        self.assertEqual(cost, 40000)
        self.assertEqual(weapon["sockets"], [])
        inventory = self.manager.get_inventory(player)
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["quality"], "大师")

    def test_instance_quality_limits_sockets(self):
        player = self.new_player()
        weapon = {"id": 2, "name": "玄铁剑", "type": "weapon", "quality": "一般", "sockets": []}
        gems = [
            self.manager.add_jewel(player, self.manager.roll_jewel(
                template=self.manager.template_by_name("破军晶"), quality="一般"
            ))
            for _ in range(2)
        ]
        ok, _ = self.manager.socket_gem_into_instance(player, weapon, gems[0])
        self.assertTrue(ok)
        ok, msg = self.manager.socket_gem_into_instance(player, weapon, gems[1])
        self.assertFalse(ok)
        self.assertIn("孔位已满", msg)


class TestJewelShop(JewelTestBase):
    def test_treasure_pool_contains_buyable_jewels(self):
        shop = ShopManager({}, self.cm)
        jewels = shop.get_jewels_for_display(12)
        self.assertTrue(jewels)
        self.assertTrue(all(item["type"] == "jewel" for item in jewels))
        for item in jewels:
            self.assertIn("·", item["name"])
            self.assertTrue(item["data"].get("affixes"))
            self.assertGreater(item["price"], 0)
        found = shop.find_item_by_name(jewels[0]["name"])
        self.assertIsNotNone(found)
        self.assertEqual(found["type"], "jewel")
        detail = shop.get_item_details(found)
        self.assertIn("词条:", detail)
        self.assertIn("可镶嵌:", detail)


class TestTierStone(JewelTestBase):
    def setUp(self):
        self.gm = GambleManager(None, self.cm, self.ring)

    def test_candidates_are_stable_and_show_true_and_false_clues(self):
        first = self.gm._get_stone_candidates("stone_u1")
        second = self.gm._get_stone_candidates("stone_u1")
        self.assertEqual(len(first), 5)
        for left, right in zip(first, second):
            self.assertEqual(left["name"], right["name"])
            self.assertEqual(left["price"], right["price"])
            expected = self.gm.STONE_TIER_CONFIG[left["index"] - 1]
            self.assertEqual(len(left["clues"]), expected["display_clues"])
            self.assertEqual(
                sum(1 for clue in left["clues"] if clue["true"]),
                expected["true_clues"],
            )
            self.assertEqual(left["hidden"], right["hidden"])
        # 价位越高，真线索数量不减少
        true_counts = [
            sum(1 for c in item["clues"] if c["true"]) for item in first
        ]
        self.assertEqual(true_counts, sorted(true_counts))

    def test_tier_loot_includes_jewel_at_high_tiers(self):
        found = False
        for tier in (3, 4, 5):
            for seed in range(40):
                hidden = self.gm._roll_tier_loot(tier, random.Random(seed))
                if hidden.get("kind") == "jewel":
                    found = True
                    self.assertTrue(hidden["jewel"]["affixes"])
                    break
            if found:
                break
        self.assertTrue(found)

    def test_tier_prices_and_empty_rates_are_high_stakes(self):
        config = self.gm.STONE_TIER_CONFIG
        self.assertEqual(
            [cfg["price"] for cfg in config],
            [3600, 12000, 32000, 72000, 150000],
        )
        for cfg in config:
            self.assertTrue(cfg.get("risk"))
            self.assertGreaterEqual(cfg["empty_chance"], 0.06)
            self.assertGreaterEqual(cfg["display_clues"], 3)
            self.assertLess(cfg["jewel_chance"], 0.7)
        # 越高档，空石率越高；稀有珠宝概率也越高，形成高风险高回报
        empties = [cfg["empty_chance"] for cfg in config]
        self.assertEqual(empties, [0.08, 0.12, 0.16, 0.20, 0.25])
        self.assertEqual(empties, sorted(empties))
        rare_share = [
            cfg["jewel_chance"]
            * sum(cfg["quality_weights"].get(q, 0) for q in ("极佳", "大师"))
            / sum(cfg["quality_weights"].values())
            for cfg in config
        ]
        self.assertEqual(rare_share, sorted(rare_share))
        self.assertGreaterEqual(self.gm.STONE_WINDOW_COST_RATE, 0.25)
        self.assertEqual(self.gm.STONE_WINDOW_MAX, 1)

    def test_false_clues_outnumber_and_mimic_true_clues(self):
        false_texts = set()
        for seed in range(80):
            candidates = self.gm._get_stone_candidates(f"clue_u{seed}")
            for item in candidates:
                false_clues = [c for c in item["clues"] if not c["true"]]
                true_clues = [c for c in item["clues"] if c["true"]]
                self.assertGreaterEqual(len(false_clues), len(true_clues))
                false_texts.update(c["text"] for c in false_clues)
        self.assertGreaterEqual(len(false_texts), 10)
        # 假线索会用“也可能 / 未必 / 兴许 / 谁知道”等模糊措辞模仿真线索
        self.assertTrue(any(
            word in text for text in false_texts
            for word in ("也可能", "未必", "兴许", "谁知道", "做不得准")
        ))

    def test_high_tier_can_roll_double_jewel(self):
        found_double = False
        for seed in range(800):
            hidden = self.gm._roll_tier_loot(5, random.Random(seed))
            if hidden.get("kind") == "jewel" and len(hidden.get("loot") or []) >= 2:
                found_double = True
                self.assertTrue(all(x.get("jewel") for x in hidden["loot"]))
                break
        self.assertTrue(found_double)


class TestTierStoneWindow(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.cm = ConfigManager(ROOT)
        cls.ring = StorageRingManager(None, cls.cm)
    class _FakeCursor:
        rowcount = 1

    class _FakeConn:
        async def execute(self, *args, **kwargs):
            return TestTierStoneWindow._FakeCursor()

        async def commit(self):
            return None

        async def rollback(self):
            return None

    class _FakeDB:
        def __init__(self, player):
            self.player = player
            self.conn = TestTierStoneWindow._FakeConn()

        async def get_player_by_id(self, user_id):
            return self.player

    def setUp(self):
        self.gm = GambleManager(None, self.cm, self.ring)
        self.player = Player(user_id="window_u1", user_name="开窗人", gold=10 ** 7)
        self.gm.db = self._FakeDB(self.player)
        self.state = {"hour": 0, "bought": [], "revealed": {}}

    async def test_window_only_once_per_stone(self):
        async def _get_state(_user_id):
            return self.state

        async def _save_state(_user_id, state):
            self.state = state

        self.gm._get_stone_state = _get_state
        self.gm._save_stone_state = _save_state
        ok, msg = await self.gm.open_stone_window(self.player, 1)
        self.assertTrue(ok, msg)
        self.assertIn("只能靠你自己判断", msg)
        self.assertIn("1", self.state["revealed"])
        ok, msg = await self.gm.open_stone_window(self.player, 1)
        self.assertFalse(ok)
        self.assertIn("已经开过窗", msg)

    async def test_window_cost_uses_quarter_price(self):
        async def _get_state(_user_id):
            return self.state

        async def _save_state(_user_id, state):
            self.state = state

        self.gm._get_stone_state = _get_state
        self.gm._save_stone_state = _save_state
        candidate = self.gm._get_stone_candidates(self.player.user_id)[0]
        self.player.gold = candidate["price"]
        ok, msg = await self.gm.open_stone_window(self.player, 1)
        self.assertTrue(ok, msg)
        self.assertEqual(
            self.player.gold,
            candidate["price"] - int(candidate["price"] * 0.25),
        )


class TestJewelDistribution(JewelTestBase):
    def test_auction_generates_jewel_lot_with_payload(self):
        manager = AuctionHouseManager(None, self.ring, self.cm)
        lot = manager._generate_lot(
            1, 1, {"category": "jewel", "tier": 3, "count": 1}, set(), 0, 0
        )
        self.assertIsNotNone(lot)
        self.assertEqual(lot["category"], "jewel")
        self.assertIn("珠宝", lot["preview_label"])
        payload = json.loads(lot["payload_json"])
        jewel = payload.get("jewel")
        self.assertTrue(jewel and jewel.get("affixes"))
        self.assertIn(jewel["quality"], lot["item_name"])
        detail = "\n".join(manager._lot_detail_lines(lot))
        self.assertIn("词条：", detail)

    def test_world_boss_jewel_reward(self):
        manager = WorldBossManager(None, self.cm, self.ring)
        player = self.new_player()
        with patch("random.randint", return_value=1):
            jewel = manager.roll_jewel_reward(player, "killer", 3, source="world_boss")
        self.assertIsNotNone(jewel)
        self.assertTrue(jewel.get("affixes"))
        self.assertIn(jewel.get("quality"), {"一般", "良好", "极佳", "大师"})


if __name__ == "__main__":
    unittest.main()
