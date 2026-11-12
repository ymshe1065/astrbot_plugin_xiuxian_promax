# 储物戒堆叠计容 与 空间附魔定价（次数越多越贵）回归测试
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


def _imp(p):
    return __import__(f"{PKG}.{p}", fromlist=["x"])


class _RingFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = _imp("data")
        self.ConfigManager = _imp("config_manager").ConfigManager
        self.Player = _imp("models").Player
        self.StorageRingManager = _imp("core").StorageRingManager
        self.cm = self.ConfigManager(ROOT)
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = self.data.DataBase(self.path)
        await self.db.connect()
        await self.data.MigrationManager(self.db.conn, self.cm).migrate()
        self.ring = self.StorageRingManager(self.db, self.cm)

    async def asyncTearDown(self):
        await self.db.close()
        try: os.remove(self.path)
        except OSError: pass

    async def _player(self, ring="基础储物戒", level_index=0, gold=0):
        p = self.Player(user_id="u1", user_name="道友", storage_ring=ring,
                        level_index=level_index, gold=gold)
        await self.db.create_player(p)
        return await self.db.get_player_by_id("u1")


class TestStacking(_RingFixture):
    async def test_each_slot_holds_fifty(self):
        """单格 50 个上限：100 个占 2 格，500 个占 10 格。"""
        p = await self._player()
        base = self.ring.get_used_slots(p)
        ok, msg = await self.ring.store_item(p, "玄铁", 100)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_used_slots(p), base + 2)
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 100)

        # 继续叠加，超出 50 的部分逐格增加
        ok, msg = await self.ring.store_item(p, "玄铁", 400)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_used_slots(p), base + 10)
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 500)

        # 500 → 550 刚好再占一格
        ok, msg = await self.ring.store_item(p, "玄铁", 50)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_used_slots(p), base + 11)

    async def test_new_kind_takes_one_slot(self):
        p = await self._player()
        base = self.ring.get_used_slots(p)
        await self.ring.store_item(p, "玄铁", 1)
        p = await self.db.get_player_by_id("u1")
        await self.ring.store_item(p, "灵草", 1)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_used_slots(p), base + 2)

    async def test_full_ring_accepts_within_same_slot(self):
        """戒指满格时，未满 50 的那一格仍可继续叠加。"""
        p = await self._player()
        cap = self.ring.get_total_capacity(p)
        stack = {f"测试材料{i}": 1 for i in range(cap)}
        stack["测试材料0"] = 30
        p.storage_ring_items = json.dumps(stack, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_available_slots(p), 0)
        self.assertEqual(self.ring.stack_slots(30), 1)

        self.assertTrue(self.ring.can_accept(p, "测试材料0", 20))
        ok, msg = await self.ring.store_item(p, "测试材料0", 20, allow_overflow=False)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("测试材料0"), 50)

        # 该格已满，再放一个就需要新格 → 拒绝
        self.assertFalse(self.ring.can_accept(p, "测试材料0", 1))

    async def test_full_ring_rejects_new_kind(self):
        p = await self._player()
        cap = self.ring.get_total_capacity(p)
        p.storage_ring_items = json.dumps(
            {f"测试材料{i}": 1 for i in range(cap)}, ensure_ascii=False
        )
        await self.db.update_player(p)
        self.assertFalse(self.ring.can_accept(p, "玄铁", 1))
        ok, msg = await self.ring.store_item(p, "玄铁", 1, allow_overflow=False)
        self.assertFalse(ok)
        self.assertIn("容量不足", msg)

    async def test_full_ring_routes_to_overflow_by_default(self):
        """满员时默认（allow_overflow=True）存入新品种会进溢出仓库并明确提示。

        迁移自原 test_overflow.py：堆叠/领取段与 test_full_ring_accepts_within_same_slot、
        test_claim_overflow_fills_one_slot 重复，仅保留独有的默认溢出路由断言。
        """
        p = await self._player()
        cap = self.ring.get_total_capacity(p)
        p.storage_ring_items = json.dumps(
            {f"测试材料{i}": 1 for i in range(cap)}, ensure_ascii=False
        )
        await self.db.update_player(p)
        ok, msg = await self.ring.store_item(p, "玄铁", 1)
        self.assertTrue(ok)
        self.assertIn("溢出", msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_overflow_items().get("玄铁"), 1)
        self.assertNotIn("玄铁", p.get_storage_ring_items())

    async def test_slot_count_helpers(self):
        """stack_slots / extra_slots / room_for 的边界值。"""
        self.assertEqual(self.ring.stack_slots(0), 0)
        self.assertEqual(self.ring.stack_slots(1), 1)
        self.assertEqual(self.ring.stack_slots(50), 1)
        self.assertEqual(self.ring.stack_slots(51), 2)
        self.assertEqual(self.ring.stack_slots(120), 3)
        # 从 49 加到 51：跨格，占 1 个新格；加到 101 占 2 个新格
        self.assertEqual(self.ring.extra_slots(49, 2), 1)
        self.assertEqual(self.ring.extra_slots(49, 52), 2)
        self.assertEqual(self.ring.extra_slots(50, 50), 1)
        # 剩余格能装多少
        self.assertEqual(self.ring.room_for(0, 0), 0)
        self.assertEqual(self.ring.room_for(0, 1), 50)
        self.assertEqual(self.ring.room_for(30, 1), 70)
        self.assertEqual(self.ring.room_for(50, 2), 100)

    async def test_equipment_still_takes_one_slot_each(self):
        p = await self._player()
        # 挑一件真实装备
        weapon = None
        for name, cfg in self.cm.items_data.items():
            if isinstance(cfg, dict) and cfg.get("type") in ("法器", "武器") and cfg.get("name"):
                if self.ring.is_equipment_item(cfg["name"]):
                    weapon = cfg["name"]
                    break
        self.assertIsNotNone(weapon, "配置里应存在装备")
        base = self.ring.get_used_slots(p)
        vol = self.ring._get_item_volume(weapon)
        ok, msg = await self.ring.store_item(p, weapon, 3)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        # 装备不堆叠：按实例逐件占格（体积由配置的 volume 决定）
        self.assertEqual(self.ring.get_used_slots(p), base + vol * 3)

    async def test_claim_overflow_fills_one_slot(self):
        p = await self._player()
        cap = self.ring.get_total_capacity(p)
        p.storage_ring_items = json.dumps(
            {f"测试材料{i}": 1 for i in range(cap)}, ensure_ascii=False
        )
        p.overflow_items = json.dumps({"玄铁": 5000}, ensure_ascii=False)
        await self.db.update_player(p)

        # 腾出一格只能取回 50 个，其余留在溢出仓库
        items = p.get_storage_ring_items()
        items.pop("测试材料0", None)
        p.set_storage_ring_items(items)
        await self.db.update_player(p)

        claimed, msg = await self.ring.claim_overflow(p)
        self.assertEqual(claimed, 1, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 50)
        self.assertEqual(p.get_overflow_items().get("玄铁"), 4950)
        # 再腾一格可继续取回
        items = p.get_storage_ring_items()
        items.pop("测试材料1", None)
        p.set_storage_ring_items(items)
        await self.db.update_player(p)
        await self.ring.claim_overflow(p)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 100)
        self.assertEqual(p.get_overflow_items().get("玄铁"), 4900)


class TestEnchantCostCurve(unittest.IsolatedAsyncioTestCase):
    """附魔定价：每 5 次一轮，单次消耗 +1 颗（1→2→3…）。"""

    async def asyncSetUp(self):
        self.ring = _imp("core").StorageRingManager(None, None)

    async def test_single_cost_curve(self):
        costs = [self.ring.enchant_cost(i) for i in range(16)]
        self.assertEqual(
            costs,
            [1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4],
        )

    async def test_total_cost_sums_the_ladder(self):
        # 前 30 次 = 5×1 + 5×2 + 5×3 + 5×4 + 5×5 + 5×6 = 105 颗
        self.assertEqual(self.ring.enchant_total_cost(0, 30), 105)
        self.assertEqual(self.ring.enchant_total_cost(0, 1), 1)
        # 从第 6 次开始接着算
        self.assertEqual(self.ring.enchant_total_cost(5, 5), 10)
        self.assertEqual(self.ring.enchant_total_cost(0, 0), 0)

    async def test_affordable_stops_at_budget(self):
        # 2 颗只够买 2 次（第 5 次之后才开始涨）
        self.assertEqual(self.ring.enchant_affordable(0, 2, 10), 2)
        # 9 颗 = 前 5 次共 5 颗 + 第 6、7 次各 2 颗 → 正好 7 次，第 8 次要 2 颗已不够
        self.assertEqual(self.ring.enchant_affordable(0, 9, 10), 7)
        self.assertEqual(self.ring.enchant_affordable(0, 0, 10), 0)


class TestEnchantNoCap(_RingFixture):
    async def _stones(self, p, count):
        ok, msg = await self.ring.store_item(p, "空间石", count)
        self.assertTrue(ok, msg)
        return await self.db.get_player_by_id("u1")

    async def test_enchant_has_no_upper_limit(self):
        # 30 次 = 105 颗空间石 = +150 格；次数本身不封顶
        p = await self._player(ring="玄铁储物戒", level_index=20, gold=0)
        p = await self._stones(p, 105)
        base = self.ring.get_ring_capacity(p.storage_ring)
        ok, msg = await self.ring.enchant_ring(p, 30)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_enchant_level(p), 30)
        self.assertEqual(self.ring.get_enchant_bonus(p), 150)
        self.assertEqual(self.ring.get_total_capacity(p), base + 150)

    async def test_enchant_twice_accumulates(self):
        p = await self._player(ring="玄铁储物戒", level_index=20)
        p = await self._stones(p, 30)
        ok, msg = await self.ring.enchant_ring(p, 5)   # 5 颗
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.ring.enchant_ring(p, 5)   # 第 6~10 次，每次 2 颗 = 10 颗
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_enchant_level(p), 10)
        self.assertEqual(self.ring.get_enchant_bonus(p), 50)
        self.assertEqual(self.ring.get_item_count(p, "空间石"), 15)

    async def test_second_round_is_more_expensive(self):
        """同样 5 次，第 6~10 次要比第 1~5 次多花一倍空间石。"""
        p = await self._player(ring="玄铁储物戒", level_index=20)
        p = await self._stones(p, 15)
        await self.ring.enchant_ring(p, 5)
        p = await self.db.get_player_by_id("u1")
        # 只剩 10 颗，正好够第 6~10 次
        ok, msg = await self.ring.enchant_ring(p, 5)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_item_count(p, "空间石"), 0)

    async def test_enchant_rejects_when_not_enough_stones(self):
        p = await self._player(ring="玄铁储物戒", level_index=20)
        p = await self._stones(p, 2)
        ok, msg = await self.ring.enchant_ring(p, 10)
        self.assertFalse(ok)
        self.assertIn("空间石不足", msg)
        self.assertIn("最多可附魔 2 次", msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_enchant_bonus(p), 0)

    async def test_enchant_survives_ring_upgrade(self):
        p = await self._player(ring="玄铁储物戒", level_index=20, gold=300000)
        p = await self._stones(p, 5)
        ok, msg = await self.ring.enchant_ring(p, 5)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_enchant_bonus(p), 25)

        ok, msg = await self.ring.upgrade_ring(p, "星辰储物戒")
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.storage_ring, "星辰储物戒")
        self.assertEqual(self.ring.get_enchant_bonus(p), 25)
        self.assertEqual(
            self.ring.get_total_capacity(p),
            self.ring.get_ring_capacity("星辰储物戒") + 25,
        )

    async def test_ring_info_reports_next_cost(self):
        p = await self._player(ring="玄铁储物戒", level_index=20)
        p = await self._stones(p, 5)
        await self.ring.enchant_ring(p, 5)
        p = await self.db.get_player_by_id("u1")
        info = self.ring.get_storage_ring_info(p)
        self.assertEqual(info["enchant_level"], 5)
        self.assertEqual(info["enchant_next_cost"], 2)
        self.assertEqual(info["capacity"], self.ring.get_total_capacity(p))


if __name__ == "__main__":
    unittest.main()
