# 储物戒堆叠计容 与 空间附魔无上限 回归测试
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


class _L:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


api_mod.logger = _L(); api_mod.AstrBotConfig = dict
event_mod.AstrMessageEvent = object; api_mod.event = event_mod
all_mod = types.ModuleType("astrbot.api.all"); all_mod.At = object; all_mod.Plain = object
api_mod.all = all_mod
sys.modules.setdefault("astrbot", astrbot); sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod); sys.modules.setdefault("astrbot.api.all", all_mod)


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
    async def test_same_material_stacks_in_one_slot(self):
        p = await self._player()
        base = self.ring.get_used_slots(p)
        ok, msg = await self.ring.store_item(p, "玄铁", 100)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_used_slots(p), base + 1)
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 100)

        # 继续叠加不占新格
        ok, msg = await self.ring.store_item(p, "玄铁", 500)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_used_slots(p), base + 1)
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 600)

    async def test_new_kind_takes_one_slot(self):
        p = await self._player()
        base = self.ring.get_used_slots(p)
        await self.ring.store_item(p, "玄铁", 1)
        p = await self.db.get_player_by_id("u1")
        await self.ring.store_item(p, "灵草", 1)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_used_slots(p), base + 2)

    async def test_full_ring_still_accepts_same_stack(self):
        p = await self._player()
        cap = self.ring.get_total_capacity(p)
        p.storage_ring_items = json.dumps(
            {f"测试材料{i}": 1 for i in range(cap)}, ensure_ascii=False
        )
        await self.db.update_player(p)
        self.assertEqual(self.ring.get_available_slots(p), 0)
        self.assertTrue(self.ring.can_accept(p, "测试材料0", 999))
        ok, msg = await self.ring.store_item(p, "测试材料0", 999, allow_overflow=False)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("测试材料0"), 1000)

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

    async def test_claim_overflow_takes_whole_stack(self):
        p = await self._player()
        cap = self.ring.get_total_capacity(p)
        p.storage_ring_items = json.dumps(
            {f"测试材料{i}": 1 for i in range(cap)}, ensure_ascii=False
        )
        p.overflow_items = json.dumps({"玄铁": 5000}, ensure_ascii=False)
        await self.db.update_player(p)

        # 腾出一格后整批取回（5000 个仍只占 1 格）
        items = p.get_storage_ring_items()
        items.pop("测试材料0", None)
        p.set_storage_ring_items(items)
        await self.db.update_player(p)

        claimed, msg = await self.ring.claim_overflow(p)
        self.assertEqual(claimed, 1, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 5000)
        self.assertFalse(p.get_overflow_items())


class TestEnchantNoCap(_RingFixture):
    async def _stones(self, p, count):
        ok, msg = await self.ring.store_item(p, "空间石", count)
        self.assertTrue(ok, msg)
        return await self.db.get_player_by_id("u1")

    async def test_enchant_has_no_upper_limit(self):
        p = await self._player(ring="玄铁储物戒", level_index=20, gold=0)
        p = await self._stones(p, 120)
        base = self.ring.get_ring_capacity(p.storage_ring)
        ok, msg = await self.ring.enchant_ring(p, 120)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_enchant_bonus(p), 600)
        self.assertEqual(self.ring.get_total_capacity(p), base + 600)

    async def test_enchant_twice_accumulates(self):
        p = await self._player(ring="玄铁储物戒", level_index=20)
        p = await self._stones(p, 30)
        await self.ring.enchant_ring(p, 10)
        p = await self.db.get_player_by_id("u1")
        await self.ring.enchant_ring(p, 20)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_enchant_bonus(p), 150)

    async def test_enchant_rejects_when_not_enough_stones(self):
        p = await self._player(ring="玄铁储物戒", level_index=20)
        p = await self._stones(p, 2)
        ok, msg = await self.ring.enchant_ring(p, 10)
        self.assertFalse(ok)
        self.assertIn("空间石不足", msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_enchant_bonus(p), 0)

    async def test_enchant_survives_ring_upgrade(self):
        p = await self._player(ring="玄铁储物戒", level_index=20, gold=300000)
        p = await self._stones(p, 10)
        ok, msg = await self.ring.enchant_ring(p, 10)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_enchant_bonus(p), 50)

        ok, msg = await self.ring.upgrade_ring(p, "星辰储物戒")
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.storage_ring, "星辰储物戒")
        self.assertEqual(self.ring.get_enchant_bonus(p), 50)
        self.assertEqual(
            self.ring.get_total_capacity(p),
            self.ring.get_ring_capacity("星辰储物戒") + 50,
        )


if __name__ == "__main__":
    unittest.main()
