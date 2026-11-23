# -*- coding: utf-8 -*-
"""NPC 威胁值与战略外交（v4.6.1）回归测试。

覆盖：
- 纯函数：档位 / 侵略倍率 / 宣战概率 / 龟缩 / 战争疲劳 / 停战接受；
- 管理器：威胁快照落盘、势力对初始化、停战同步、风声写入与前端视图。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub  # noqa: E402

_install_astrbot_stub()

R = __import__(f"{PKG}.core.strategy_rules", fromlist=["x"])


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestThreatRules(unittest.TestCase):
    def test_tier_thresholds(self):
        tiers = {"alert": 20, "prepare": 40, "hostile": 60, "coalition": 80}
        self.assertEqual(R.threat_tier(0, tiers), "calm")
        self.assertEqual(R.threat_tier(19.9, tiers), "calm")
        self.assertEqual(R.threat_tier(20, tiers), "alert")
        self.assertEqual(R.threat_tier(40, tiers), "prepare")
        self.assertEqual(R.threat_tier(60, tiers), "hostile")
        self.assertEqual(R.threat_tier(80, tiers), "coalition")

    def test_threat_mult_and_cap(self):
        self.assertAlmostEqual(R.threat_mult(0), 1.0)
        self.assertAlmostEqual(R.threat_mult(60), 1.48, places=2)
        self.assertAlmostEqual(R.threat_mult(100), 1.80, places=2)
        # 超过上限时按 cap 截断
        self.assertAlmostEqual(R.threat_mult(100, aggression_gain=1.0,
                                             cap=1.5), 1.5)

    def test_declare_chance_monotonic_and_caps(self):
        a = R.threat_declare_chance(60)
        b = R.threat_declare_chance(80)
        c = R.threat_declare_chance(100)
        self.assertLessEqual(a, b)
        self.assertLessEqual(b, c)
        self.assertGreater(a, 0)
        self.assertLessEqual(c, 0.55)
        # 国力过低：自杀宣战倍率生效，但不为负
        weak = R.threat_declare_chance(80, power_ratio=0.1,
                                       min_power_ratio=0.35,
                                       suicide_declare_mult=0.55)
        normal = R.threat_declare_chance(80, power_ratio=1.0)
        self.assertAlmostEqual(weak, normal * 0.55, places=6)

    def test_share_excludes_unowned(self):
        nodes = [
            {"type": "city", "level": 1, "owner_faction": 1},
            {"type": "city", "level": 1, "owner_faction": 0},
        ]
        share = R.threat_share_by_faction(nodes, {"city": 100})
        self.assertAlmostEqual(share[1], 1.0)
        self.assertNotIn(0, share)

    def test_adjacency_and_public_floor(self):
        self.assertAlmostEqual(
            R.threat_adjacency_factor(1, False), 1.0)
        self.assertAlmostEqual(
            R.threat_adjacency_factor(0, False), 0.30)
        self.assertAlmostEqual(
            R.threat_adjacency_factor(0, True), 0.70)
        floor = R.threat_public_floor(0.80)
        self.assertGreaterEqual(floor, 60)

    def test_turtle_requires_all_conditions(self):
        stats = {}
        cfg = {"enabled": True, "defense_threshold": 1500,
               "hegemon_share_threshold": 0.40}
        self.assertTrue(R.threat_is_turtle(stats, 2000, 0.1, True, False, cfg))
        self.assertFalse(R.threat_is_turtle(stats, 1000, 0.1, True, False, cfg))
        self.assertFalse(R.threat_is_turtle(stats, 2000, 0.1, False, False, cfg))
        self.assertFalse(R.threat_is_turtle(stats, 2000, 0.5, True, False, cfg))
        self.assertFalse(R.threat_is_turtle(
            {"attacks_7d": [1]}, 2000, 0.1, True, False, cfg))

    def test_exhaustion_and_peace_accept(self):
        low = R.threat_exhaustion(0, 0, 0, 0, 0, 0, False, 50)
        high = R.threat_exhaustion(1, 1, 1, 1, 1, 1, True, 0)
        self.assertLess(low, high)
        self.assertGreaterEqual(high, 0)
        self.assertLessEqual(high, 100)
        calm = R.threat_peace_accept(0, 0, 50, 50)
        weary = R.threat_peace_accept(90, 80, 20, 20)
        self.assertLess(calm, weary)
        self.assertGreaterEqual(calm, 0.05)
        self.assertLessEqual(weary, 0.95)

    def test_per_day_to_per_tick(self):
        p_tick = R.per_day_to_per_tick(0.5, 96)
        self.assertAlmostEqual((1 - p_tick) ** 96, 0.5, places=6)
        self.assertGreaterEqual(R.per_day_to_per_tick(0, 96), 0)


class TestThreatManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        sm_mod = _imp("managers.strategy_manager")
        self.StrategyManager = sm_mod.StrategyManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        await self.db.create_player(self.Player(
            user_id="u1", gold=1000, user_name="测试道人"))

        self.m = self.StrategyManager(self.db)
        self.m.main_cfg().setdefault("hex", {})["field_encounter_mode"] = "auto"
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]

    async def asyncTearDown(self):
        try:
            await self.conn.close()
        finally:
            try:
                os.unlink(self.db_path)
            except OSError:
                pass

    async def _npc_ids(self):
        rows = await self.m._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? "
            "AND kind='npc' AND alive=1 ORDER BY id", (self.wid,))
        return [int(r["id"]) for r in rows]

    async def test_snapshot_persists_threat_and_pairs(self):
        npc_ids = await self._npc_ids()
        self.assertGreaterEqual(len(npc_ids), 2, "建世界应生成多个 NPC 势力")
        await self.m._step_npc_diplomacy(self.world)
        data = self.world.get("data") or {}
        self.assertIn("threat", data)
        self.assertIn("npc_ai", data)
        self.assertTrue(data["threat"].get("accum"))
        state = data["npc_ai"]
        self.assertTrue(state.get("pairs"), "势力对状态应已初始化")
        pair = state["pairs"][str(npc_ids[0])][str(npc_ids[1])]
        self.assertIn(pair["phase"], ("calm", "alert", "prepare", "hostile",
                                      "war", "truce", "cooldown"))
        # 威胁回写只含数值，便于前端直接渲染
        self.assertIsInstance(data["threat"]["tick"], int)

    async def test_truce_sync_and_wind(self):
        npc_ids = await self._npc_ids()
        f = await self.m.get_faction(npc_ids[0])
        tf = await self.m.get_faction(npc_ids[1])
        tick = int(self.world.get("tick_no", 0) or 0)
        await self.m._sync_npc_pair_after_truce(self.world, f, tf)
        state = self.m._npc_ai_state(self.world)
        for a, b in ((f["id"], tf["id"]), (tf["id"], f["id"])):
            pair = self.m._pair(state, a, b)
            self.assertEqual(pair["phase"], "truce")
            self.assertGreater(pair["truce_until_tick"], tick)
        self.m._add_wind(self.world, "测试风声：战云压境。", "war")
        winds = (self.world.get("data") or {}).get("winds") or []
        self.assertTrue(winds)
        self.assertEqual(winds[-1]["text"], "测试风声：战云压境。")
        self.assertEqual(winds[-1]["kind"], "war")
        await self.m.save_world(self.world)

    async def test_frontend_view_exposes_npc_world(self):
        await self.m._step_npc_diplomacy(self.world)
        self.m._add_wind(self.world, "测试风声：海晏河清。", "peace")
        await self.m.save_world(self.world)
        st = await self.m.get_state("u1")
        self.assertIn("npc_world", st)
        self.assertIn("winds", st)
        view = st["npc_world"]
        self.assertIn("factions", view)
        self.assertIn("tiers", view)
        self.assertTrue(any(w.get("text") == "测试风声：海晏河清。"
                            for w in view.get("winds") or []))
        npc_rows = [r for r in view["factions"] if r.get("is_npc")]
        self.assertTrue(npc_rows)
        for row in npc_rows:
            for key in ("name", "public_threat", "tier", "threat_mult",
                        "relations", "wars", "personality"):
                self.assertIn(key, row)

    async def test_gc_keeps_wind_shape(self):
        now = int(time.time())
        data = {
            "winds": [
                {"visible_at": now, "text": "新", "kind": "battle", "tick": 1},
                {"visible_at": now - 8 * 86400, "text": "旧",
                 "kind": "plot", "tick": 0},
            ]
        }
        self.m._gc_world_data(data, 1)
        self.assertEqual(len(data["winds"]), 1)
        self.assertEqual(data["winds"][0]["kind"], "battle")


if __name__ == "__main__":
    unittest.main()
