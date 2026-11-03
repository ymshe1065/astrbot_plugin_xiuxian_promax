# 洞天仓库（藏宝阁）· 一键转移 · 一键回收 回归测试
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


class _Base(unittest.IsolatedAsyncioTestCase):
    LAND_TYPE = 1

    async def asyncSetUp(self):
        self.data = _imp("data")
        self.ConfigManager = _imp("config_manager").ConfigManager
        self.Player = _imp("models").Player
        self.cm = self.ConfigManager(ROOT)
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = self.data.DataBase(self.path)
        await self.db.connect()
        await self.data.MigrationManager(self.db.conn, self.cm).migrate()

        self.StorageRingManager = _imp("core.storage_ring_manager").StorageRingManager
        self.WarehouseManager = _imp("core.warehouse_manager").WarehouseManager
        self.ItemInstanceManager = _imp("core.item_instance_manager").ItemInstanceManager

        self.ring = self.StorageRingManager(self.db, self.cm)
        self.wh = self.WarehouseManager(self.db, self.cm, self.ring)
        self.im = self.ItemInstanceManager(self.db, self.cm)

        self.player = await self._player()
        await self.db.conn.execute(
            "INSERT INTO blessed_lands (user_id, land_type, land_name, level, exp_bonus, gold_per_hour) "
            "VALUES (?, ?, '小洞天', 1, 0.05, 100)",
            (self.player.user_id, self.LAND_TYPE),
        )
        await self.db.conn.commit()

    async def asyncTearDown(self):
        await self.db.close()
        try: os.remove(self.path)
        except OSError: pass

    async def _player(self, gold=500000, ring="基础储物戒"):
        p = self.Player(user_id="u1", user_name="道友", storage_ring=ring,
                        level_index=20, gold=gold)
        await self.db.create_player(p)
        return await self.db.get_player_by_id("u1")

    async def _open_warehouse(self, level=1):
        p = await self.db.get_player_by_id("u1")
        p.warehouse_level = level
        await self.db.update_player(p)
        return await self.db.get_player_by_id("u1")

    async def _equipment_name(self):
        for name, cfg in self.cm.items_data.items():
            if isinstance(cfg, dict) and cfg.get("name") and self.ring.is_equipment_item(cfg["name"]):
                return cfg["name"]
        raise AssertionError("配置里应存在装备")


class TestWarehouseOpen(_Base):
    async def test_cannot_open_without_blessed_land(self):
        await self.db.conn.execute("DELETE FROM blessed_lands WHERE user_id = ?", ("u1",))
        await self.db.conn.commit()
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.wh.open_warehouse(p)
        self.assertFalse(ok)
        self.assertIn("洞天", msg)

    async def test_open_costs_gold_and_sets_level(self):
        p = await self.db.get_player_by_id("u1")
        gold_before = p.gold
        ok, msg = await self.wh.open_warehouse(p)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.warehouse_level, 1)
        self.assertEqual(p.gold, gold_before - self.WarehouseManager.OPEN_COST)
        self.assertEqual(self.wh.get_capacity(p), 50)
        self.assertEqual(self.wh.get_available_slots(p), 50)

    async def test_upgrade_limited_by_land_quality(self):
        p = await self._open_warehouse(1)
        self.assertEqual(await self.wh.get_max_level(p), 5)
        ok, msg = await self.wh.upgrade_warehouse(p, 99)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.warehouse_level, 5)
        self.assertEqual(self.wh.get_capacity(p), 250)
        ok, msg = await self.wh.upgrade_warehouse(p, 1)
        self.assertFalse(ok)
        self.assertIn("上限", msg)

    async def test_upgrade_cost_growth(self):
        self.assertEqual(self.wh.upgrade_cost(1), 15000)
        self.assertEqual(self.wh.upgrade_cost(2), 17000)
        self.assertEqual(self.wh.upgrade_cost(3), 20000)
        self.assertGreater(self.wh.upgrade_cost(10), self.wh.upgrade_cost(5))

    async def test_warehouse_survives_land_advance(self):
        p = await self._open_warehouse(3)
        # 模拟进阶洞天：整行 DELETE + INSERT
        await self.db.conn.execute("DELETE FROM blessed_lands WHERE user_id = ?", ("u1",))
        await self.db.conn.execute(
            "INSERT INTO blessed_lands (user_id, land_type, land_name, level, exp_bonus, gold_per_hour) "
            "VALUES (?, 2, '中洞天', 1, 0.1, 500)",
            ("u1",),
        )
        await self.db.conn.commit()
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.wh.get_level(p), 3)
        self.assertEqual(await self.wh.get_max_level(p), 10)


class TestWarehouseTransfer(_Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.p = await self._open_warehouse(1)

    async def test_transfer_materials_in_and_out(self):
        p = self.p
        await self.ring.store_item(p, "玄铁", 500)
        await self.ring.store_item(p, "灵草", 20)
        p = await self.db.get_player_by_id("u1")
        ring_used = self.ring.get_used_slots(p)

        ok, msg = await self.wh.transfer_in(p)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items(), {})
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 500)
        self.assertEqual(self.wh.get_used_slots(p), 2)
        self.assertEqual(self.ring.get_used_slots(p), 0)
        self.assertLess(self.ring.get_used_slots(p), ring_used)

        ok, msg = await self.wh.transfer_out(p)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 500)
        self.assertEqual(p.get_warehouse_items(), {})

    async def test_transfer_by_category(self):
        p = self.p
        await self.ring.store_item(p, "玄铁", 10)
        await self.ring.store_item(p, "空间石", 3)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.wh.transfer_in(p, "材料")
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(len(p.get_warehouse_items()), 2)

    async def test_transfer_equipment_keeps_quality(self):
        p = self.p
        name = await self._equipment_name()
        self.im.create_instances(p, name, 2, quality="大师", location="storage")
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")

        ok, msg = await self.wh.transfer_in(p)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        wh_insts = self.im.list_instances(p, "warehouse", name)
        self.assertEqual(len(wh_insts), 2)
        self.assertTrue(all(x.get("quality") == "大师" for x in wh_insts))
        self.assertEqual(len(self.im.list_instances(p, "storage", name)), 0)

        ok, msg = await self.wh.transfer_out(p)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(len(self.im.list_instances(p, "storage", name)), 2)

    async def test_batch_respects_remaining_slots(self):
        """批量转移时必须按本轮已占用后的剩余格数判定，不能只看转移前的容量。"""
        p = self.p
        p.warehouse_items = json.dumps({f"填充{i}": 1 for i in range(49)}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.wh.get_available_slots(p), 1)

        await self.ring.store_item(p, "玄铁", 5)
        p = await self.db.get_player_by_id("u1")
        await self.ring.store_item(p, "灵草", 5)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(len(p.get_storage_ring_items()), 2)

        ok, msg = await self.wh.transfer_in(p)
        p = await self.db.get_player_by_id("u1")
        # 只剩 1 格：只能转 1 种，另一种留在储物戒
        self.assertEqual(len(p.get_warehouse_items()), 50)
        self.assertEqual(len(p.get_storage_ring_items()), 1)

    async def test_warehouse_full_keeps_item_in_ring(self):
        p = self.p
        # 仓库只有 50 格，灌入 50 种不同材料占满
        p.warehouse_items = json.dumps({f"填充{i}": 1 for i in range(50)}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.wh.get_available_slots(p), 0)

        await self.ring.store_item(p, "玄铁", 3)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.wh.transfer_in(p)
        self.assertFalse(ok)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 3)
        self.assertNotIn("玄铁", p.get_warehouse_items())

    async def test_same_stack_still_transfers_when_full(self):
        p = self.p
        p.warehouse_items = json.dumps({"玄铁": 10, **{f"填充{i}": 1 for i in range(49)}}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        await self.ring.store_item(p, "玄铁", 5)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.wh.get_available_slots(p), 0)
        ok, msg = await self.wh.transfer_in(p)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 15)


class TestWarehouseConsumption(_Base):
    """仓库里的材料必须能被炼丹/制符/附魔正常扣掉，否则物品等于消失。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.p = await self._open_warehouse(1)

    async def test_get_item_count_merges_warehouse(self):
        p = self.p
        p.warehouse_items = json.dumps({"玄铁": 7}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_item_count(p, "玄铁"), 7)
        self.assertTrue(self.ring.has_item(p, "玄铁", 5))
        self.assertEqual(self.ring.merged_items(p).get("玄铁"), 7)

    async def test_retrieve_item_falls_back_to_warehouse(self):
        p = self.p
        p.warehouse_items = json.dumps({"玄铁": 7}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.ring.retrieve_item(p, "玄铁", 4)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 3)
        self.assertNotIn("玄铁", p.get_storage_ring_items())

    async def test_discard_item_falls_back_to_warehouse(self):
        p = self.p
        p.warehouse_items = json.dumps({"玄铁": 7}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.ring.discard_item(p, "玄铁", 2)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 5)

    async def test_enchant_uses_warehouse_stones(self):
        p = self.p
        p.warehouse_items = json.dumps({"空间石": 10}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.ring.enchant_ring(p, 10)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_enchant_bonus(p), 50)
        self.assertNotIn("空间石", p.get_warehouse_items())

    async def test_alchemy_consumes_warehouse_materials(self):
        recipe = {
            "name": "测试丹",
            "materials": [{"name": "玄铁", "count": 2}],
            "gold_cost": 0,
        }
        p = self.p
        p.warehouse_items = json.dumps({"玄铁": 5}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")

        Alchemy = _imp("managers.alchemy_manager").AlchemyManager
        mgr = Alchemy(self.db, self.cm, self.ring)
        ok, msg, consumed = mgr._consume_materials(p, recipe)
        self.assertTrue(ok, msg)
        self.assertEqual(consumed, ["玄铁×2"])
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 3)

    async def test_craft_talisman_consumes_warehouse_materials(self):
        Talisman = _imp("managers.talisman_manager").TalismanManager
        recipes = self.cm.talisman_recipes_data
        recipe = recipes[0] if isinstance(recipes, list) else next(iter(recipes.values()))
        specs = [s for s in (recipe.get("materials") or []) if s.get("count")]
        self.assertTrue(specs, "符箓配方应有材料需求")

        catalog = list(self.cm.material_catalog.values())
        supply = {}
        for spec in specs:
            match = None
            for entry in catalog:
                if entry.get("category") != spec.get("category"):
                    continue
                if spec.get("tier") and entry.get("tier") != spec.get("tier"):
                    continue
                match = entry.get("name")
                break
            self.assertIsNotNone(match, f"材料目录里应存在 {spec.get('category')}/{spec.get('tier')} 材料")
            supply[match] = 20

        p = self.p
        p.warehouse_items = json.dumps(supply, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")

        mgr = Talisman(self.db, self.cm, self.ring)
        ok, msg, consumed = mgr._consume_materials(p, recipe)
        self.assertTrue(ok, msg)
        self.assertTrue(consumed)
        for name, before in supply.items():
            self.assertLess(p.get_warehouse_items().get(name, 0), before)


class TestBulkRecycle(_Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.p = await self._open_warehouse(1)

    async def test_plan_skips_equipment_and_expensive(self):
        p = self.p
        await self.ring.store_item(p, "玄铁", 10)
        p = await self.db.get_player_by_id("u1")
        name = await self._equipment_name()
        self.im.create_instances(p, name, 1, quality="一般", location="storage")
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")

        plan = self.ring.plan_bulk_recycle(p)
        names = [x["name"] for x in plan["stacks"]]
        self.assertIn("玄铁", names)
        self.assertEqual(plan["equipment"], [])
        self.assertGreater(plan["total"], 0)

    async def test_plan_keeps_expensive_items(self):
        p = self.p
        # 价格高于阈值的材料应被保留
        expensive = None
        for name, cfg in self.cm.items_data.items():
            if not isinstance(cfg, dict) or not cfg.get("name"):
                continue
            if self.ring.is_equipment_item(cfg["name"]) or self.ring.is_pill(cfg["name"]):
                continue
            try:
                price = int(cfg.get("price", 0) or 0)
            except (TypeError, ValueError):
                continue
            if price >= 20000:
                expensive = cfg["name"]
                break
        self.assertIsNotNone(expensive, "应存在高价材料")
        await self.ring.store_item(p, expensive, 2)
        p = await self.db.get_player_by_id("u1")
        plan = self.ring.plan_bulk_recycle(p)
        self.assertIn(expensive, [x["name"] for x in plan["kept"]])
        self.assertNotIn(expensive, [x["name"] for x in plan["stacks"]])

    async def test_apply_recycles_and_pays(self):
        p = self.p
        await self.ring.store_item(p, "玄铁", 10)
        p = await self.db.get_player_by_id("u1")
        gold_before = p.gold
        plan = self.ring.plan_bulk_recycle(p)
        ok, msg = await self.ring.apply_bulk_recycle(p, plan)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.gold, gold_before + plan["total"])
        self.assertNotIn("玄铁", p.get_storage_ring_items())

    async def test_apply_started_from_warehouse_when_flagged(self):
        p = self.p
        p.warehouse_items = json.dumps({"玄铁": 6}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        plan = self.ring.plan_bulk_recycle(p, include_warehouse=True)
        ok, msg = await self.ring.apply_bulk_recycle(p, plan)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertNotIn("玄铁", p.get_warehouse_items())

    async def test_apply_equipment_when_flagged(self):
        p = self.p
        name = await self._equipment_name()
        self.im.create_instances(p, name, 1, quality="一般", location="storage")
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        gold_before = p.gold
        plan = self.ring.plan_bulk_recycle(p, include_equipment=True)
        self.assertEqual(len(plan["equipment"]), 1)
        ok, msg = await self.ring.apply_bulk_recycle(p, plan)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(len(self.im.list_instances(p, "storage", name)), 0)
        self.assertGreater(p.gold, gold_before)


class TestBulkRecycleHandler(_Base):
    """两步确认：预览不落库，确认才执行。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self._open_warehouse(1)
        self.Handler = _imp("handlers.warehouse_handler").WarehouseHandler
        self.handler = self.Handler(self.db, self.cm)

    async def test_preview_then_confirm(self):
        p = await self.db.get_player_by_id("u1")
        await self.ring.store_item(p, "玄铁", 10)
        p = await self.db.get_player_by_id("u1")
        gold_before = p.gold

        ok, msg = await self.handler._confirm_recycle(p)
        self.assertFalse(ok)
        self.assertIn("没有待确认", msg)

        plan = self.ring.plan_bulk_recycle(p)
        self.handler_module_pending(p, plan)

        ok, msg = await self.handler._confirm_recycle(p)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertNotIn("玄铁", p.get_storage_ring_items())
        self.assertEqual(p.gold, gold_before + plan["total"])

    def handler_module_pending(self, player, plan):
        module = _imp("handlers.warehouse_handler")
        import time

        module._PENDING_RECYCLE[player.user_id] = (
            time.time(),
            {"plan": plan, "include_equipment": False,
             "include_warehouse": False, "include_pills": False},
        )

    async def test_expired_preview_rejected(self):
        p = await self.db.get_player_by_id("u1")
        await self.ring.store_item(p, "玄铁", 10)
        p = await self.db.get_player_by_id("u1")
        plan = self.ring.plan_bulk_recycle(p)
        module = _imp("handlers.warehouse_handler")
        module._PENDING_RECYCLE[p.user_id] = (0, {"plan": plan})
        ok, msg = await self.handler._confirm_recycle(p)
        self.assertFalse(ok)
        self.assertIn("过期", msg)


if __name__ == "__main__":
    unittest.main()
