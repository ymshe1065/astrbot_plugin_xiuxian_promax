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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


def _imp(p):
    return __import__(f"{PKG}.{p}", fromlist=["x"])


class _FakeEvent:
    """只收 plain_result 的事件桩，够 /仓库 文本输出用。"""

    def __init__(self):
        self.results = []

    def plain_result(self, text):
        self.results.append(text)
        return text


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
        # 每格 50：玄铁 500 占 10 格，灵草 20 占 1 格
        self.assertEqual(self.wh.get_used_slots(p), 11)
        self.assertEqual(self.ring.get_used_slots(p), 0)
        self.assertLess(self.ring.get_used_slots(p), ring_used)

        ok, msg = await self.wh.transfer_out(p)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 500)
        self.assertEqual(p.get_warehouse_items(), {})

    async def test_warehouse_slots_count_every_fifty(self):
        """仓库与储物戒同规则：每格 50 个，超出占新格。"""
        p = await self.db.get_player_by_id("u1")
        p.warehouse_items = json.dumps({"玄铁": 120, "灵草": 30}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        # 玄铁 120 → 3 格，灵草 30 → 1 格
        self.assertEqual(self.wh.get_used_slots(p), 4)
        self.assertEqual(self.wh.get_available_slots(p), 46)

    async def test_transfer_partial_when_warehouse_has_one_slot(self):
        """仓库只剩 1 格时，一键转移只搬得下 50 个，其余留在储物戒。"""
        p = self.p
        p.warehouse_items = json.dumps(
            {f"填充{i}": 1 for i in range(49)}, ensure_ascii=False
        )
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        await self.ring.store_item(p, "玄铁", 120)
        p = await self.db.get_player_by_id("u1")

        ok, msg = await self.wh.transfer_in(p)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 50)
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 70)
        self.assertIn("70", msg)

    async def test_transfer_by_category(self):
        p = self.p
        await self.ring.store_item(p, "玄铁", 10)
        await self.ring.store_item(p, "空间石", 3)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.wh.transfer_in(p, "材料")
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(len(p.get_warehouse_items()), 2)

    async def test_transfer_out_by_material_subcategory(self):
        """一键取出支持材料细分：灵植 / 矿材 / 妖兽 各取各的。"""
        p = self.p
        await self.ring.store_item(p, "九天草", 5)
        await self.ring.store_item(p, "万古铁", 5)
        await self.ring.store_item(p, "九天骨", 5)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.wh.transfer_in(p)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(len(p.get_warehouse_items()), 3)

        ok, msg = await self.wh.transfer_out(p, "灵植")
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("九天草"), 5)
        self.assertNotIn("万古铁", p.get_storage_ring_items())
        self.assertNotIn("九天骨", p.get_storage_ring_items())
        self.assertEqual(p.get_warehouse_items().get("万古铁"), 5)
        self.assertEqual(p.get_warehouse_items().get("九天骨"), 5)

    async def test_transfer_out_equipment_category_only(self):
        p = self.p
        name = await self._equipment_name()
        self.im.create_instances(p, name, 1, quality="良品", location="warehouse")
        await self.db.update_player(p)
        p.warehouse_items = json.dumps({"万古铁": 3}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.wh.transfer_out(p, "装备")
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(len(self.im.list_instances(p, "storage", name)), 1)
        self.assertEqual(p.get_warehouse_items().get("万古铁"), 3)

    def test_category_parser_accepts_subtypes(self):
        handlers = _imp("handlers.warehouse_handler")
        for cat in ("材料", "符箓", "丹方", "装备", "灵植", "矿材", "妖兽", "灵液", "特殊"):
            self.assertEqual(handlers._parse_category(cat), (cat, True))
        self.assertEqual(handlers._parse_category("随便写"), ("", False))

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

    async def test_item_count_ignores_warehouse(self):
        """仓库里的东西不算「持有」：消耗前必须先取到戒指。"""
        p = self.p
        p.warehouse_items = json.dumps({"玄铁": 7}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_item_count(p, "玄铁"), 0)
        self.assertFalse(self.ring.has_item(p, "玄铁", 1))
        self.assertEqual(self.ring.ring_items(p), {})

    async def test_retrieve_item_rejects_warehouse_only(self):
        p = self.p
        p.warehouse_items = json.dumps({"玄铁": 7}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.ring.retrieve_item(p, "玄铁", 4)
        self.assertFalse(ok, msg)
        self.assertIn("储物戒", msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 7)

    async def test_discard_item_rejects_warehouse_only(self):
        p = self.p
        p.warehouse_items = json.dumps({"玄铁": 7}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.ring.discard_item(p, "玄铁", 2)
        self.assertFalse(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 7)

    async def test_enchant_ignores_warehouse_stones(self):
        p = self.p
        p.warehouse_items = json.dumps({"空间石": 10}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.ring.enchant_ring(p, 10)
        self.assertFalse(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(self.ring.get_enchant_bonus(p), 0)
        self.assertEqual(p.get_warehouse_items().get("空间石"), 10)

    async def test_alchemy_ignores_warehouse_materials(self):
        """炼丹只认储物戒：仓库有料也要先取出，否则判定材料不足。"""
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
        self.assertFalse(ok, msg)
        self.assertIn("材料不足", msg)
        self.assertIn("一键取出", msg)
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 5)

    async def test_alchemy_consumes_after_taking_out(self):
        """取到戒指之后正常消耗。"""
        recipe = {
            "name": "测试丹",
            "materials": [{"name": "玄铁", "count": 2}],
            "gold_cost": 0,
        }
        p = self.p
        p.warehouse_items = json.dumps({"玄铁": 5}, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.wh.transfer_out(p, "材料")
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")

        Alchemy = _imp("managers.alchemy_manager").AlchemyManager
        mgr = Alchemy(self.db, self.cm, self.ring)
        ok, msg, consumed = mgr._consume_materials(p, recipe)
        self.assertTrue(ok, msg)
        self.assertEqual(consumed, ["玄铁×2"])
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 3)

    async def test_craft_talisman_ignores_warehouse_materials(self):
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
        self.assertFalse(ok, msg)
        self.assertIn("符材不足", msg)
        self.assertIn("一键取出", msg)
        for name, before in supply.items():
            self.assertEqual(p.get_warehouse_items().get(name, 0), before)


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


class TestTakeItem(_Base):
    """单件取出：/取出 物品名 [数量]"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.p = await self._open_warehouse(1)
        self.module = _imp("handlers.warehouse_handler")

    async def _stock(self, items):
        p = await self.db.get_player_by_id("u1")
        p.warehouse_items = json.dumps(items, ensure_ascii=False)
        await self.db.update_player(p)
        return await self.db.get_player_by_id("u1")

    async def _fill_ring(self):
        """把储物戒塞满，返回占满后的 player。"""
        p = await self.db.get_player_by_id("u1")
        cap = self.ring.get_total_capacity(p)
        p.storage_ring_items = json.dumps(
            {f"占位{i}": 1 for i in range(cap)}, ensure_ascii=False
        )
        await self.db.update_player(p)
        return await self.db.get_player_by_id("u1")

    async def test_take_partial_count(self):
        p = await self._stock({"玄铁": 100})
        ok, msg = await self.wh.transfer_out_item(p, "玄铁", 30)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 30)
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 70)

    async def test_take_all_when_count_omitted(self):
        p = await self._stock({"玄铁": 42})
        ok, msg = await self.wh.transfer_out_item(p, "玄铁")
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 42)
        self.assertNotIn("玄铁", p.get_warehouse_items())

    async def test_take_more_than_stock_clamps(self):
        p = await self._stock({"玄铁": 8})
        ok, msg = await self.wh.transfer_out_item(p, "玄铁", 999)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 8)
        self.assertIn("只有", msg)

    async def test_take_unknown_item(self):
        p = await self._stock({"玄铁": 5})
        ok, msg = await self.wh.transfer_out_item(p, "不存在的东西", 1)
        self.assertFalse(ok)
        self.assertIn("没有", msg)

    async def test_take_partial_when_ring_has_one_slot(self):
        """戒指只剩 1 格时，取全部也只能取 50 个，其余留在仓库。"""
        p = await self._stock({"玄铁": 500})
        cap = self.ring.get_total_capacity(p)
        p.storage_ring_items = json.dumps(
            {f"占位{i}": 1 for i in range(cap - 1)}, ensure_ascii=False
        )
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")

        ok, msg = await self.wh.transfer_out_item(p, "玄铁")
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 50)
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 450)
        self.assertIn("放得下的部分", msg)

    async def test_take_zero_rejected(self):
        p = await self._stock({"玄铁": 5})
        ok, msg = await self.wh.transfer_out_item(p, "玄铁", 0)
        self.assertFalse(ok)
        self.assertIn("大于 0", msg)

    async def test_take_requires_open_warehouse(self):
        p = await self.db.get_player_by_id("u1")
        p.warehouse_level = 0
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.wh.transfer_out_item(p, "玄铁", 1)
        self.assertFalse(ok)
        self.assertIn("藏宝阁", msg)

    async def test_ring_full_blocks_new_stack(self):
        p = await self._stock({"玄铁": 5})
        p = await self._fill_ring()
        self.assertEqual(self.ring.get_available_slots(p), 0)
        ok, msg = await self.wh.transfer_out_item(p, "玄铁", 1)
        self.assertFalse(ok)
        self.assertIn("空间不足", msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_warehouse_items().get("玄铁"), 5)

    async def test_ring_full_still_takes_existing_stack(self):
        """戒指里已有同名栈时，再取多少都不占新格。"""
        p = await self._stock({"玄铁": 5})
        p = await self._fill_ring()
        ring = p.get_storage_ring_items()
        ring["玄铁"] = 1
        p.storage_ring_items = json.dumps(ring, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")

        ok, msg = await self.wh.transfer_out_item(p, "玄铁", 5)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(p.get_storage_ring_items().get("玄铁"), 6)

    async def test_take_equipment_by_piece(self):
        p = self.p
        name = await self._equipment_name()
        self.im.create_instances(p, name, 3, quality="良品", location="warehouse")
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(len(self.im.list_instances(p, "warehouse", name)), 3)

        ok, msg = await self.wh.transfer_out_item(p, name, 1)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        self.assertEqual(len(self.im.list_instances(p, "storage", name)), 1)
        self.assertEqual(len(self.im.list_instances(p, "warehouse", name)), 2)
        self.assertIn("2 件", msg)

    async def test_take_equipment_keeps_quality(self):
        p = self.p
        name = await self._equipment_name()
        self.im.create_instances(p, name, 1, quality="大师", location="warehouse")
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.wh.transfer_out_item(p, name)
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id("u1")
        insts = self.im.list_instances(p, "storage", name)
        self.assertEqual(len(insts), 1)
        self.assertEqual(insts[0].get("quality"), "大师")

    async def test_item_already_in_ring_hint(self):
        p = await self.db.get_player_by_id("u1")
        await self.ring.store_item(p, "玄铁", 3)
        p = await self.db.get_player_by_id("u1")
        ok, msg = await self.wh.transfer_out_item(p, "玄铁", 1)
        self.assertFalse(ok)
        self.assertIn("储物戒里", msg)


class TestWarehouseDisplayOrder(_Base):
    """藏宝阁清单与储物戒同一口径：品阶从高到低排。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.Handler = _imp("handlers.warehouse_handler").WarehouseHandler
        self.handler = self.Handler(self.db, self.cm)
        await self._open_warehouse(1)

    async def _stock(self, items):
        p = await self.db.get_player_by_id("u1")
        p.warehouse_items = json.dumps(items, ensure_ascii=False)
        await self.db.update_player(p)
        return await self.db.get_player_by_id("u1")

    async def _render(self, p):
        event = _FakeEvent()
        return "".join([r async for r in
                        self.handler.handle_warehouse.__wrapped__(self.handler, p, event)])

    async def test_items_sorted_by_rank_desc(self):
        p = await self._stock(
            {"两仪皮": 1, "天外陨核": 2, "无极骨": 3, "四象皮": 4}
        )
        text = await self._render(p)
        order = ["天外陨核", "四象皮", "无极骨", "两仪皮"]
        positions = [text.index(name) for name in order]
        self.assertEqual(positions, sorted(positions), text)

    async def test_sort_happens_before_twelve_item_cut(self):
        """超过 12 种时先排再截断，高级材料不会被 `…共 N 种` 挤掉。"""
        bulk = {f"凡品杂物{i}": 1 for i in range(13)}
        bulk["天外陨核"] = 1
        p = await self._stock(bulk)
        text = await self._render(p)
        if "…共" in text:  # 确有截断时才校验
            self.assertIn("天外陨核", text)

    async def test_equipment_instances_sorted_by_rank_desc(self):
        """装备实例同样按品阶排：高品装备先列，低品在后。"""
        display = _imp("core.item_display")
        p = await self.db.get_player_by_id("u1")
        names = [n for n in self.cm.items_data if self.ring.is_equipment_item(n)]
        ranked = sorted(
            names, key=lambda n: display.rank_index(display.item_meta(n, self.cm)["rank"])
        )
        low, high = ranked[0], ranked[-1]
        self.assertNotEqual(display.item_meta(low, self.cm)["rank"],
                            display.item_meta(high, self.cm)["rank"])

        # 故意先塞低品，再看渲染顺序
        self.im.create_instances(p, low, 1, location="warehouse")
        self.im.create_instances(p, high, 1, location="warehouse")
        await self.db.update_player(p)
        p = await self.db.get_player_by_id("u1")

        text = await self._render(p)
        self.assertIn("装备实例", text)
        self.assertLess(text.index(high), text.index(low), text)


class TestTakeArgsParse(unittest.TestCase):
    """前台参数解析与名称匹配"""

    def setUp(self):
        self.module = _imp("handlers.warehouse_handler")

    def test_split_name_and_count(self):
        split = self.module._split_take_args
        self.assertEqual(split("聚气丹 30"), ("聚气丹", 30))
        self.assertEqual(split("聚气丹x30"), ("聚气丹", 30))
        self.assertEqual(split("聚气丹×30"), ("聚气丹", 30))
        self.assertEqual(split("聚气丹 全部"), ("聚气丹", None))
        self.assertEqual(split("聚气丹"), ("聚气丹", None))
        self.assertEqual(split("  玄铁   12 "), ("玄铁", 12))
        self.assertEqual(split("30"), ("", None))

    def test_resolve_name(self):
        resolve = self.module._resolve_name
        names = ["玄铁", "玄铁剑", "聚气丹"]
        self.assertEqual(resolve("玄铁", names), ("玄铁", []))
        self.assertEqual(resolve("聚气", names), ("聚气丹", []))
        name, hits = resolve("玄铁", ["玄铁剑", "玄铁矿"])
        self.assertEqual(name, "")
        self.assertEqual(hits, ["玄铁剑", "玄铁矿"])
        name, hits = resolve("不存在", names)
        self.assertEqual((name, hits), ("", []))


if __name__ == "__main__":
    unittest.main()
