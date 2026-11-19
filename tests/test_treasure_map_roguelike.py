"""藏宝图肉鸽奇遇回归。

覆盖：
- 每层结算后生成奇遇，未处理前不能继续深入；
- 奇遇选择写入临时效果并清空待处理事件，同一奇遇不能重复领取；
- 灵石不足时拒绝付费选项且不扣灵石；
- 奇遇致死会清空战利品与临时效果；
- 撤退/结算后临时效果与待处理事件一并失效；
- 护身灵符能抵挡一次陷阱伤害并被消耗；
- 数据库迁移补齐 effects / event 两列。
"""

import json
import time
import unittest

try:
    import test_equipment_instances as fixtures
except ModuleNotFoundError:  # 从仓库根目录运行时走包内导入
    from tests import test_equipment_instances as fixtures


class _FixedRng:
    def __init__(self, value=0.0):
        self._value = value

    def random(self):
        return self._value

    def uniform(self, a, b):  # noqa: ARG002 - 固定伤害比例
        return self._value

    def choice(self, seq):
        return seq[0]


class TestTreasureMapRoguelike(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TestEquipmentInstances.asyncSetUp
    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player
    _imp = staticmethod(fixtures._imp)

    def _manager(self):
        mod = self._imp("managers.treasure_map_manager")
        return mod.TreasureMapManager(self.db, self.ring, self.cm)

    async def _insert_run(self, user_id, floor=0, hp=1000, max_hp=1000,
                          loot=None, effects=None, event=None):
        now = int(time.time())
        await self.db.conn.execute(
            "INSERT INTO treasure_map_runs "
            "(user_id, floor, map_tier, hp, max_hp, heal_used, heal_budget, "
            "loot, log, effects, event, status, started_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?, 0, 180, ?, '[]', ?, ?, 'active', ?, ?)",
            (
                user_id, floor, hp, max_hp,
                json.dumps(loot or [], ensure_ascii=False),
                json.dumps(effects or [], ensure_ascii=False),
                json.dumps(event or {}, ensure_ascii=False),
                now, now,
            ),
        )
        await self.db.conn.commit()

    async def _row(self, user_id):
        async with self.db.conn.execute(
            "SELECT * FROM treasure_map_runs WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            keys = [d[0] for d in cursor.description]
        return dict(zip(keys, row)) if row else None

    def _event(self, options):
        return {
            "key": "unit_event", "name": "试炼奇遇", "desc": "测试用奇遇。",
            "floor": 2, "options": options,
        }

    async def test_advance_generates_event_and_blocks_next_floor(self):
        p = await self._player("tm_event", gold=1000)
        p.hp = 1_000_000
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        await self._insert_run(p.user_id, floor=0, hp=1_000_000, max_hp=1000)
        mgr = self._manager()

        ok, msg = await mgr.advance(p)
        self.assertTrue(ok, msg)
        self.assertIn("奇遇", msg)
        row = await self._row(p.user_id)
        event = json.loads(row["event"])
        self.assertTrue(event.get("options"), "推进后必须生成待处理奇遇")
        self.assertEqual(int(event["floor"]), 2)

        ok, msg = await mgr.advance(p)
        self.assertFalse(ok, "奇遇未处理前不能继续深入")
        self.assertIn("奇遇", msg)
        self.assertEqual(int((await self._row(p.user_id))["floor"]), 1)

    async def test_choose_applies_effect_and_cannot_replay(self):
        p = await self._player("tm_choose", gold=1000)
        p.hp = 900
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        event = self._event([
            {
                "label": "炼体", "desc": "获得铜皮铁骨。",
                "actions": [{"type": "effect", "key": "iron_body", "stacks": 2}],
            }
        ])
        await self._insert_run(p.user_id, floor=1, hp=900, event=event)
        mgr = self._manager()

        ok, msg = await mgr.choose(p, 1)
        self.assertTrue(ok, msg)
        self.assertIn("铜皮铁骨", msg)
        row = await self._row(p.user_id)
        self.assertEqual(json.loads(row["event"]), {})
        effects = json.loads(row["effects"])
        self.assertEqual(effects[0]["key"], "iron_body")
        self.assertEqual(effects[0]["stacks"], 2)

        ok, msg = await mgr.choose(p, 1)
        self.assertFalse(ok, "同一奇遇不能重复选择")
        effects_after = json.loads((await self._row(p.user_id))["effects"])
        self.assertEqual(effects_after, effects)

    async def test_choose_rejects_unaffordable_cost(self):
        p = await self._player("tm_cost", gold=100)
        p.hp = 500
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        event = self._event([
            {
                "label": "买符", "desc": "花灵石买护身符。", "cost_gold": 500,
                "actions": [{"type": "effect", "key": "ward", "stacks": 1}],
            }
        ])
        await self._insert_run(p.user_id, floor=1, hp=500, event=event)
        mgr = self._manager()

        ok, msg = await mgr.choose(p, 1)
        self.assertFalse(ok)
        self.assertIn("灵石", msg)
        fresh = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(int(fresh.gold or 0), 100, "付费选项失败不能扣灵石")
        row = await self._row(p.user_id)
        self.assertTrue(json.loads(row["event"]).get("options"), "失败时奇遇应保留")
        self.assertEqual(json.loads(row["effects"]), [])

    async def test_choose_death_clears_loot_and_effects(self):
        p = await self._player("tm_event_death", gold=1000)
        p.hp = 500
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        event = self._event([
            {
                "label": "以命换宝", "desc": "赌上全部气血。",
                "actions": [{"type": "hp", "pct": -1.0}],
            }
        ])
        await self._insert_run(
            p.user_id, floor=3, hp=500, loot=[{"kind": "gold", "amount": 888}],
            effects=[{"key": "gold_luck", "stacks": 2}], event=event,
        )
        mgr = self._manager()

        ok, msg = await mgr.choose(p, 1)
        self.assertTrue(ok, msg)
        self.assertIn("倒下了", msg)
        row = await self._row(p.user_id)
        self.assertEqual(row["status"], "ended")
        self.assertEqual(json.loads(row["loot"]), [])
        self.assertEqual(json.loads(row["effects"]), [])
        self.assertEqual(json.loads(row["event"]), {})
        fresh = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(int(fresh.hp or 0), 1)

    async def test_retreat_clears_temporary_effects(self):
        p = await self._player("tm_retreat_effects", gold=1000)
        p.hp = 900
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        await self._insert_run(
            p.user_id, floor=2, hp=900,
            loot=[{"kind": "gold", "amount": 300}],
            effects=[{"key": "iron_body", "stacks": 1}],
            event=self._event([{"label": "离开", "desc": "", "actions": []}]),
        )
        mgr = self._manager()

        ok, msg = await mgr.retreat(p)
        self.assertTrue(ok, msg)
        row = await self._row(p.user_id)
        self.assertEqual(row["status"], "ended")
        self.assertEqual(json.loads(row["effects"]), [])
        self.assertEqual(json.loads(row["event"]), {})
        self.assertEqual(int((await self.db.get_player_by_id(p.user_id)).gold), 1300)

    async def test_ward_blocks_one_trap(self):
        p = await self._player("tm_ward", gold=1000)
        p.hp = 800
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        await self._insert_run(
            p.user_id, floor=1, hp=800,
            effects=[{"key": "ward", "stacks": 1}],
        )
        mgr = self._manager()
        mod = self._imp("managers.treasure_map_manager")
        original = mod.seeded_rng
        mod.seeded_rng = lambda *a, **k: _FixedRng(0.0)  # 必定触发陷阱
        try:
            ok, msg = await mgr.advance(p)
        finally:
            mod.seeded_rng = original
        self.assertTrue(ok, msg)
        self.assertIn("护身灵符挡下陷阱", msg)
        row = await self._row(p.user_id)
        self.assertEqual(json.loads(row["effects"]), [], "护身符触发后应被消耗")
        self.assertEqual(int(row["hp"]), 800, "护身符应完全抵消本次陷阱伤害")

    async def test_effect_rates_are_capped(self):
        mod = self._imp("managers.treasure_map_manager")
        effects = [{"key": "iron_body", "stacks": 99}]
        self.assertAlmostEqual(
            mod.TreasureMapManager.effect_rate(effects, "iron_body"), 5 * 0.08
        )
        self.assertIn("40%", mod.TreasureMapManager.effect_value_text("iron_body", 5))

    async def test_migration_adds_roguelike_columns(self):
        async with self.db.conn.execute("PRAGMA table_info(treasure_map_runs)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        self.assertIn("effects", columns)
        self.assertIn("event", columns)

    async def test_web_arg_options_list_pending_event_choices(self):
        p = await self._player("tm_web", gold=1000)
        event = self._event([
            {"label": "拿效果", "desc": "获得铜皮铁骨。", "actions": []},
            {"label": "花灵石", "desc": "花灵石买东西。", "cost_gold": 500, "actions": []},
        ])
        await self._insert_run(p.user_id, floor=1, hp=500, event=event)
        plugin = type("_DummyPlugin", (), {})()
        plugin.treasure_map_mgr = self._manager()
        arg_options = self._imp("webchat_room.arg_options")
        options, msg = await arg_options._src_treasure_tiers(
            plugin, p, {}, ["选择"]
        )
        self.assertEqual(msg, "")
        self.assertEqual([opt["value"] for opt in options], ["1", "2"])
        self.assertIn("500", str(options[1].get("desc") or ""))
        self.assertIn("试炼奇遇", str(options[0].get("group") or ""))


if __name__ == "__main__":
    unittest.main()
