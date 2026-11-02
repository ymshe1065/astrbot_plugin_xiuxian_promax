# 切原石掉落表与定价回归测试
import asyncio
import os
import random
import sys
import unittest
from collections import Counter, defaultdict

sys.path.insert(0, os.getcwd())
from test_equipment_instances import _imp, ROOT  # noqa: E402

GambleManager = _imp("managers.gamble_manager").GambleManager
ItemValuation = _imp("core.item_valuation")
ConfigManager = _imp("config_manager").ConfigManager


class StoneLootTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cm = ConfigManager(ROOT)
        cls.ring = _imp("core").StorageRingManager(None, cls.cm)
        cls.gm = GambleManager(None, cls.cm, cls.ring)
        cls.material_category = {
            name: (info or {}).get("category")
            for name, info in cls.cm.material_catalog.items()
        }

    def roll_many(self, count, seed=20261102):
        rng = random.Random(seed)
        for _ in range(count):
            for loot in self.gm.roll_stone_loot(rng):
                yield loot


class TestStonePrice(StoneLootTestBase):
    def test_price_is_5000(self):
        self.assertEqual(self.gm.STONE_PRICE, 5000)

    def test_reject_invalid_count(self):
        """越界数量在扣费前返回，不触碰数据库。"""

        class _P:
            gold = 10 ** 9

        for bad in (0, -1, 11):
            ok, msg = asyncio.run(self.gm.cut_stone(_P(), bad))
            self.assertFalse(ok)
            self.assertIn("最多切", msg)


class TestStoneDropRates(StoneLootTestBase):
    N = 60000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rng = random.Random(20261102)
        cls.events = Counter()
        cls.ranks = defaultdict(Counter)
        cls.categories = Counter()
        cls.quantities = defaultdict(Counter)
        cls.prices = 0
        cls.recycles = 0
        for _ in range(cls.N):
            for loot in cls.gm.roll_stone_loot(cls.rng):
                kind = loot["kind"]
                cls.events[kind] += 1
                if kind in ("gear", "material"):
                    cls.ranks[kind][loot["rank"]] += 1
                if kind == "material":
                    cls.categories[cls.material_category.get(loot["name"], "?")] += 1
                    cls.quantities[loot["rank"]][loot["qty"]] += 1
                if loot.get("name"):
                    cls.prices += cls.cm.get_item_price(loot["name"]) * loot["qty"]
                    cls.recycles += (
                        ItemValuation.recycle_value(cls.cm, loot["name"], "一般", None) * loot["qty"]
                    )

    def test_gear_chance_unchanged(self):
        """装备总概率 4.9%（玄品 3.92% / 圣品 0.98%）必须保持不变。"""
        gear = self.events["gear"] / self.N
        self.assertAlmostEqual(gear, 0.049, delta=0.006)
        saint = self.ranks["gear"]["圣品"] / self.N
        xuan = self.ranks["gear"]["玄品"] / self.N
        self.assertAlmostEqual(saint, 0.0098, delta=0.002)
        self.assertAlmostEqual(xuan, 0.0392, delta=0.004)

    def test_egg_chance(self):
        self.assertAlmostEqual(self.events["egg"] / self.N, 0.001, delta=0.0008)

    def test_materials_are_mostly_ore(self):
        total = sum(self.categories.values())
        ore = self.categories["矿材"] / total
        self.assertGreaterEqual(ore, 0.75)

    def test_material_rank_gradient(self):
        """凡品 > 黄品 > 玄品 > 圣品，掉率逐级递减。"""
        total = sum(self.ranks["material"].values())
        rates = [self.ranks["material"][r] / total for r in self.gm.STONE_RANKS]
        for higher, lower in zip(rates, rates[1:]):
            self.assertGreater(higher, lower)
        self.assertAlmostEqual(rates[0], 0.42, delta=0.03)
        self.assertAlmostEqual(rates[3], 0.03, delta=0.01)

    def test_quantities_follow_table(self):
        ore_qty = {rank: qty for rank, qty, _ in self.gm.STONE_ORE_TABLE}
        other_qty = {rank: qty for rank, qty, _ in self.gm.STONE_MATERIAL_TABLE}
        for rank, counter in self.quantities.items():
            expected = set(ore_qty.values()) | set(other_qty.values())
            self.assertTrue(set(counter) <= expected, f"{rank} 份数异常: {set(counter)}")
            self.assertTrue(all(qty >= 1 for qty in counter))

    def test_expected_value_below_price(self):
        """期望物品价值接近但不高于原石价，避免无限刷灵石。"""
        value_ratio = self.prices / self.N / self.gm.STONE_PRICE
        recycle_ratio = self.recycles / self.N / self.gm.STONE_PRICE
        self.assertGreater(value_ratio, 0.85)
        self.assertLess(value_ratio, 1.0)
        self.assertGreater(recycle_ratio, 0.45)
        self.assertLess(recycle_ratio, 0.65)

    def test_storage_volume_reasonable(self):
        """单块原石平均占用容量不超过 5 格，避免基础储物戒一次就爆。"""
        volume = sum(
            qty * count for counter in self.quantities.values() for qty, count in counter.items()
        )
        self.assertLess(volume / self.N, 5.0)


class TestStoneFallback(StoneLootTestBase):
    def test_rank_fallback_order(self):
        self.assertEqual(self.gm._rank_fallback("玄品"), ["玄品", "黄品", "凡品", "圣品"])
        self.assertEqual(self.gm._rank_fallback("圣品"), ["圣品", "玄品", "黄品", "凡品"])

    def test_missing_rank_falls_back(self):
        """某品阶池为空时退回最近品阶，不应切出空气。"""
        gm = GambleManager(None, self.cm, self.ring)
        gm._stone_pools = ({("矿材", "凡品"): ["灭世石"]}, {})
        rng = random.Random(1)
        loot = gm._roll_stone_material(gm.STONE_ORE_TABLE, ("矿材",), rng)
        self.assertEqual(loot["name"], "灭世石")
        self.assertEqual(loot["rank"], "凡品")

    def test_empty_pool_reports_dust(self):
        gm = GambleManager(None, self.cm, self.ring)
        gm._stone_pools = ({}, {})
        rng = random.Random(1)
        loot = gm._roll_stone_material(gm.STONE_ORE_TABLE, ("矿材",), rng)
        self.assertEqual(loot["kind"], "dust")
        self.assertIsNone(loot["name"])


class TestStoneLabels(StoneLootTestBase):
    def test_labels(self):
        cases = [
            ({"kind": "gear", "rank": "圣品", "name": "诛仙剑", "qty": 1}, "天地异象"),
            ({"kind": "gear", "rank": "玄品", "name": "万古弓", "qty": 1}, "金光四射"),
            ({"kind": "egg", "rank": "", "name": "护府令", "qty": 3}, "护府令】x3"),
            ({"kind": "material", "rank": "圣品", "name": "混元铁", "qty": 1}, "矿脉交辉"),
            ({"kind": "material", "rank": "凡品", "name": "灭世石", "qty": 6}, "碎石渣"),
        ]
        for loot, expect in cases:
            self.assertIn(expect, self.gm._loot_label(loot))
        self.assertIn("一无所获", self.gm._loot_label({"kind": "dust", "name": None, "qty": 0}))


class TestCutStoneEndToEnd(StoneLootTestBase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile

        self.data = _imp("data")
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = self.data.DataBase(self.path)
        await self.db.connect()
        await self.data.MigrationManager(self.db.conn, self.cm).migrate()
        self.ring_mgr = _imp("core").StorageRingManager(self.db, self.cm)
        self.gm = GambleManager(self.db, self.cm, self.ring_mgr)
        player = _imp("models").Player(user_id="u1", user_name="u1", level_index=10, gold=100000)
        await self.db.create_player(player)
        self.player = await self.db.get_player_by_id("u1")

    async def asyncTearDown(self):
        await self.db.close()
        try:
            os.remove(self.path)
        except OSError:
            pass

    async def test_deducts_price_and_stores(self):
        random.seed(3)
        ok, msg = await self.gm.cut_stone(self.player, 3)
        self.assertTrue(ok, msg)
        fresh = await self.db.get_player_by_id("u1")
        self.assertEqual(fresh.gold, 100000 - 3 * self.gm.STONE_PRICE)
        self.assertIn("5,000/块", msg)
        self.assertGreater(sum(fresh.get_storage_ring_items().values()), 0)

    async def test_insufficient_gold(self):
        self.player.gold = 100
        await self.db.update_player(self.player)
        ok, msg = await self.gm.cut_stone(self.player, 1)
        self.assertFalse(ok)
        self.assertIn("灵石不足", msg)
        fresh = await self.db.get_player_by_id("u1")
        self.assertEqual(fresh.gold, 100)


if __name__ == "__main__":
    unittest.main()
