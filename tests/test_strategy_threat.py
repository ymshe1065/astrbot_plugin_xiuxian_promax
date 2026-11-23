# -*- coding: utf-8 -*-
"""NPC 威胁值与战略外交（v4.6.1）回归测试。

覆盖：
- 纯函数：档位 / 侵略倍率 / 宣战概率 / 龟缩 / 战争疲劳 / 停战接受；
- 管理器：威胁快照落盘、势力对初始化、停战同步、风声写入与前端视图。
"""
import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertFalse(R.threat_is_turtle(stats, 2000, 0.1, True, True, cfg))
        self.assertTrue(R.threat_is_turtle(
            stats, 2000, 0.1, True, True,
            {**cfg, "exclude_hegemon": False}))
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


class TestThreatValueHelpers(unittest.TestCase):
    """旧档自愈数值助手：合法 0 / 负数不得被当成空值，非法值统一回退。"""

    def test_safe_int_semantics(self):
        mod = _imp("managers.strategy_threat_mixin")
        for raw, want in (
                (None, 0), ("", 0), ("12", 12), ("-5", -5), (0, 0),
                (-5, -5), ("abc", 0), (float("nan"), 0),
                (float("inf"), 0), (float("-inf"), 0), (3.9, 3),
                ("1e400", 0), (10 ** 400, 0)):
            self.assertEqual(mod._safe_int(raw), want, f"raw={raw!r}")
        self.assertEqual(mod._safe_int("abc", 7), 7)

    def test_safe_float_and_num_semantics(self):
        mod = _imp("managers.strategy_threat_mixin")
        for raw, want in ((None, 0.0), ("2.5", 2.5), (-3, -3.0), (0, 0.0),
                          ("abc", 0.0), (float("nan"), 0.0),
                          (10 ** 400, 0.0)):
            self.assertAlmostEqual(mod._safe_float(raw), want, msg=f"raw={raw!r}")
        # _num：0 是合法值（民心归零 / 战况崩盘），不能被 falsy 兜底成默认值
        self.assertEqual(mod._num(0, 50), 0.0)
        self.assertEqual(mod._num(None, 50), 50.0)
        self.assertEqual(mod._num("abc", 50), 50.0)
        self.assertEqual(mod._num(10 ** 400, 50), 50.0)


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

    async def test_frontend_view_accepts_legacy_string_winds(self):
        """旧档 winds 可能是字符串数组，也可能是损坏的非列表值。"""
        ids = await self._npc_ids()
        f_meta = {ids[0]: {"id": ids[0], "name": "甲", "kind": "npc",
                           "alive": 1}}
        view = self.m._npc_world_view(
            self.world, f_meta, ["旧风声 <战云>", {"text": "新风声",
                                                  "kind": "war"}])
        self.assertEqual([w["text"] for w in view["winds"]],
                         ["旧风声 <战云>", "新风声"])
        self.assertEqual(view["winds"][0]["kind_name"], "流言")
        self.assertEqual(view["winds"][1]["kind_name"], "战云")
        broken = self.m._npc_world_view(self.world, f_meta, {"bad": 1})
        self.assertEqual(broken["winds"], [])
        self.assertIn("factions", broken)

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

    async def test_gc_heals_corrupt_temp_containers_and_counts(self):
        """损坏的 CD / 日限桶 / 赎金表必须在 tick 时自愈，不能卡死玩家动作。"""
        now = int(time.time())
        today = time.strftime("%Y%m%d", time.localtime(now))
        data = {
            "plot_cd": "bad",
            "persuade_cd": ["bad"],
            "persuade_daily": {"7": {today: "NaN", "19990101": 3}},
            "loyalty_reward_daily": {"u1": "bad"},
            "ransoms": [1, 2],
            "winds": {"bad": 1},
        }
        self.m._gc_world_data(data, 100)
        self.assertEqual(data["plot_cd"], {})
        self.assertEqual(data["persuade_cd"], {})
        # 非法计数按 0 计，过期日期桶丢弃
        self.assertEqual(data["persuade_daily"], {"7": {today: 0}})
        self.assertEqual(data["loyalty_reward_daily"], {})
        self.assertEqual(data["ransoms"], {})
        self.assertEqual(data["winds"], [])

    # ------------------------------------------------------------------
    # v4.6.1 R1 回归：龟缩统计 / 灭亡清理 / 战和状态机 / 备战期 / 联盟
    # ------------------------------------------------------------------
    async def test_seed_wars_leave_slot_for_autonomous_declaration(self):
        """种子战争最多占 max_active_wars_per_npc−1，给自主宣战留空槽。"""
        ids = await self._npc_ids()
        self.assertGreaterEqual(len(ids), 3)
        # 清空这几家 NPC 之间的既有外交，确保能重新预置
        for fid in ids:
            await self.m._execute(
                "UPDATE strategy_factions SET diplomacy_json='{}' WHERE id=?",
                (fid,))
        await self.m._commit()
        factions = []
        for fid in ids:
            f = await self.m.get_faction(fid)
            factions.append({"id": fid,
                             "personality": f.get("personality") or {}})
        with mock.patch.object(
                R, "npc_seed_relation",
                side_effect=lambda *a, **k: (R.REL_WAR, -60)):
            await self.m.seed_npc_diplomacy(self.wid, factions,
                                            random.Random(7))
        war_cap = int((self.m._npc_war_cfg()["max_active_wars_per_npc"])) - 1
        for fid in ids:
            f = await self.m.get_faction(fid)
            wars = [v for v in (f.get("diplomacy") or {}).values()
                    if isinstance(v, dict)
                    and str(v.get("rel") or "") == R.REL_WAR]
            self.assertLessEqual(len(wars), war_cap,
                                 f"势力 {fid} 种子战争 {len(wars)} 场，"
                                 f"未给自主宣战留槽")
        # 幂等：再次补种不会新增战争
        with mock.patch.object(
                R, "npc_seed_relation",
                side_effect=lambda *a, **k: (R.REL_WAR, -60)):
            await self.m.seed_npc_diplomacy(self.wid, factions,
                                            random.Random(8))
        for fid in ids:
            f = await self.m.get_faction(fid)
            wars = [v for v in (f.get("diplomacy") or {}).values()
                    if isinstance(v, dict)
                    and str(v.get("rel") or "") == R.REL_WAR]
            self.assertLessEqual(len(wars), war_cap)

    async def test_tactical_attack_reserves_slot_for_declaration(self):
        """突袭必须给自主宣战留 1 槽；正式战争目标不受限。"""
        self.assertTrue(self.m._npc_tactical_new_war_allowed(R.REL_WAR, 99))
        self.assertTrue(
            self.m._npc_tactical_new_war_allowed(R.REL_NEUTRAL, 0))
        # 默认 max_active_wars_per_npc=2 → 突袭最多占 1 槽
        self.assertFalse(
            self.m._npc_tactical_new_war_allowed(R.REL_NEUTRAL, 1))
        cfg = self.m.main_cfg()
        threat = cfg.setdefault("npc", {}).setdefault("threat", {})
        old = threat.get("enabled", None)
        threat["enabled"] = False
        try:
            # 威胁层关闭：恢复旧的突袭行为
            self.assertTrue(
                self.m._npc_tactical_new_war_allowed(R.REL_NEUTRAL, 99))
        finally:
            if old is None:
                threat.pop("enabled", None)
            else:
                threat["enabled"] = old

    async def test_declaration_day_zero_keeps_daily_counter(self):
        """开局第 0 天是合法 day，不能被 ``or -1`` 当成空值重置每日上限。"""
        self.world["tick_no"] = 5
        state = self.m._npc_ai_state(self.world)
        state["declarations"] = {"day": 0, "count": 4}
        await self.m._step_npc_diplomacy(self.world)
        self.assertEqual(state["declarations"]["day"], 0)
        self.assertEqual(state["declarations"]["count"], 4,
                         "第 0 天已宣战 4 场时不得把计数清零")

    async def test_capture_marks_attacker_not_victim(self):
        """据点易主只记录攻占方的主动攻击，被攻占玩家不因此失去龟缩。"""
        ids = await self._npc_ids()
        a, b = ids[0], ids[1]
        state = self.m._npc_ai_state(self.world)
        pair = self.m._pair(state, a, b)
        pair["phase"] = "war"
        reverse = self.m._pair(state, b, a)
        reverse["phase"] = "war"
        self.m._note_threat_capture(self.world, b, a, "测试城")
        self.assertEqual(pair["war_score"], 60.0)
        self.assertEqual(reverse["war_score"], 40.0)
        stats = self.m._npc_ai_state(self.world)["stats"]
        self.assertIn("captures_7d", stats.get(str(a), {}))
        self.assertIn("attacks_7d", stats.get(str(a), {}))
        # 被攻占方不应被记成“主动攻击者”
        self.assertFalse((stats.get(str(b)) or {}).get("attacks_7d"))
        self.assertFalse((stats.get(str(b)) or {}).get("captures_7d"))
        # 占无主 / 匪寨也算扩张：记录 captures_7d，但不写易主风声
        winds_before = len((self.world.get("data") or {}).get("winds") or [])
        self.m._note_threat_capture(self.world, 0, ids[2], "无主之地")
        stats2 = self.m._npc_ai_state(self.world)["stats"]
        self.assertIn("captures_7d", stats2.get(str(ids[2]), {}))
        winds_after = len((self.world.get("data") or {}).get("winds") or [])
        self.assertEqual(winds_before, winds_after)

    async def test_world_view_relations_prioritize_live_targets(self):
        """已灭亡势力即使残留高 H，也不能挤占总榜前 6 的对外关系位。"""
        ids = await self._npc_ids()
        a, live = ids[0], ids[1]
        f_meta = {
            a: {"id": a, "name": "甲", "kind": "npc", "alive": 1},
            live: {"id": live, "name": "乙", "kind": "npc", "alive": 1},
        }
        h_map = {str(live): 1.0}
        for i in range(6):
            dead = 9000 + i
            f_meta[dead] = {"id": dead, "name": f"亡{i}", "kind": "npc",
                            "alive": 0}
            h_map[str(dead)] = 99.0 - i
        world = self.world
        world.setdefault("data", {})["threat"] = {
            "accum": {str(a): h_map}, "public": {}, "share": {}}
        view = self.m._npc_world_view(world, f_meta, [])
        row = next(r for r in view["factions"] if r["id"] == a)
        self.assertEqual([r["target"] for r in row["relations"]], [live])

    async def test_capture_tolerates_corrupt_pair_and_stats(self):
        """战果统计可先于 tick 自愈被调用：损坏字段不得让占城动作抛错。"""
        ids = await self._npc_ids()
        a, b = ids[0], ids[1]
        state = self.m._npc_ai_state(self.world)
        pair = self.m._pair(state, a, b)
        pair.update({"phase": "war", "captures_by_me": "oops",
                     "captures_by_target": "NaN"})
        reverse = self.m._pair(state, b, a)
        reverse.update({"phase": "war", "captures_by_me": None,
                        "captures_by_target": "bad"})
        state["stats"][str(a)] = {"captures_7d": ["bad", None],
                                  "attacks_7d": "not-a-list"}
        # 不抛异常，且损坏值按 0 处理（不能把 war_score 算成 NaN）
        self.m._note_threat_capture(self.world, b, a, "测试城")
        self.assertEqual(int(pair["captures_by_me"]), 1)
        self.assertEqual(int(pair["captures_by_target"]), 0)
        self.assertEqual(int(reverse["captures_by_target"]), 1)
        self.assertEqual(pair["war_score"], 60.0)
        self.assertEqual(reverse["war_score"], 40.0)
        stat = state["stats"][str(a)]
        self.assertIsInstance(stat["attacks_7d"], list)
        self.assertEqual(stat["attacks_7d"][-1],
                         int(self.world.get("tick_no", 0) or 0))
        # 非法历史值（字符串 / None）应被丢弃，不能以 -1 混进龟缩窗口
        self.assertTrue(all(isinstance(x, int) and x >= 0
                            for x in stat["captures_7d"]))

    async def test_dead_faction_pairs_cleaned_and_hidden(self):
        ids = await self._npc_ids()
        a, b = ids[0], ids[1]
        state = self.m._npc_ai_state(self.world)
        for x, y in ((a, b), (b, a)):
            pair = self.m._pair(state, x, y)
            pair.update({"phase": "war", "war_start_tick": 0,
                         "frenzy_until_tick": 99999, "goal": "hegemon"})
        # 灭亡 b：无据点 + alive=0
        for node in await self.m.get_nodes(self.wid):
            if int(node.get("owner_faction", 0) or 0) == b:
                node["owner_faction"] = 0
                await self.m.save_node(node)
        fb = await self.m.get_faction(b)
        fb["alive"] = 0
        fb["faction_state"] = "destroyed"
        await self.m.save_faction(fb)
        await self.m.save_world(self.world)

        snap = await self.m._threat_snapshot(self.world)
        self.m._update_threat_accum(self.world, snap, int(self.world["tick_no"]))
        self.m._update_pair_phases(self.world, snap, int(self.world["tick_no"]))
        self.m._expire_war_states(self.world, snap, int(self.world["tick_no"]))
        state = self.m._npc_ai_state(self.world)
        self.assertNotIn(str(b), state["pairs"])
        self.assertNotIn(str(b), state["pairs"].get(str(a), {}))
        # 累计敌意里的灭亡势力也要清理，避免污染第三方威胁 / 对外关系
        self.assertNotIn(str(b), state.get("accum") or {})
        self.assertNotIn(str(b), (state.get("accum") or {}).get(str(a), {}))
        # 目标灭亡后狂热应提前结束
        self.assertFalse(self.m._npc_frenzy_state(self.world, a)["active"])

        await self.m.save_world(self.world)
        view = (await self.m.get_state("u1"))["npc_world"]
        self.assertNotIn(b, [r["id"] for r in view["factions"]])
        row_a = next(r for r in view["factions"] if r["id"] == a)
        self.assertNotIn(b, [w["target"] for w in row_a["wars"]])

    async def test_truce_pair_realigns_when_relation_is_war(self):
        """停战期被撕毁（或玩家重新宣战）后 pair 必须立即回到 war。"""
        ids = await self._npc_ids()
        f = await self.m.get_faction(ids[0])
        tf = await self.m.get_faction(ids[1])
        tick = int(self.world.get("tick_no", 0) or 0)
        state = self.m._npc_ai_state(self.world)
        for x, y in ((f["id"], tf["id"]), (tf["id"], f["id"])):
            self.m._pair(state, x, y).update({
                "phase": "truce", "truce_until_tick": tick + 192,
                "no_declare_until_tick": tick + 192,
                "captures_by_me": 3, "last_offer_tick": 5,
                "frenzy_until_tick": 9999})
        self.m._set_relation(f.setdefault("diplomacy", {}), tf["id"],
                             "war", war_at=0)
        self.m._set_relation(tf.setdefault("diplomacy", {}), f["id"],
                             "war", war_at=0)
        await self.m.save_faction(f)
        await self.m.save_faction(tf)
        await self.m.save_world(self.world)
        snap = await self.m._threat_snapshot(self.world)
        self.m._expire_war_states(self.world, snap, tick)
        for x, y in ((f["id"], tf["id"]), (tf["id"], f["id"])):
            row = self.m._pair(state, x, y)
            self.assertEqual(row["phase"], "war")
            self.assertEqual(int(row["captures_by_me"]), 0)
            self.assertEqual(int(row["last_offer_tick"]), 0)
            self.assertEqual(int(row["frenzy_until_tick"]), 0)

    async def test_war_pending_does_not_create_war_pair(self):
        """备战期（war_at 未到）不是战争态；war_at 到期后才进入 war。"""
        ids = await self._npc_ids()
        f = await self.m.get_faction(ids[0])
        tf = await self.m.get_faction(ids[1])
        tick = int(self.world.get("tick_no", 0) or 0)
        state = self.m._npc_ai_state(self.world)
        pair = self.m._pair(state, f["id"], tf["id"])
        pair.update({"phase": "hostile", "hostile_since_tick": tick})
        future = int(time.time()) + 3600
        self.m._set_relation(f.setdefault("diplomacy", {}), tf["id"],
                             "war", war_at=future)
        self.m._set_relation(tf.setdefault("diplomacy", {}), f["id"],
                             "war", war_at=future)
        await self.m.save_faction(f)
        await self.m.save_faction(tf)
        await self.m.save_world(self.world)
        snap = await self.m._threat_snapshot(self.world)
        self.m._expire_war_states(self.world, snap, tick)
        self.assertNotEqual(pair["phase"], "war")
        # 备战期结束：关系变为 war，才建立战争态
        self.m._set_relation(f.setdefault("diplomacy", {}), tf["id"],
                             "war", war_at=0)
        self.m._set_relation(tf.setdefault("diplomacy", {}), f["id"],
                             "war", war_at=0)
        await self.m.save_faction(f)
        await self.m.save_faction(tf)
        await self.m.save_world(self.world)
        snap = await self.m._threat_snapshot(self.world)
        self.m._expire_war_states(self.world, snap, tick)
        self.assertEqual(pair["phase"], "war")

    async def test_truce_expiry_moves_both_sides_to_cooldown(self):
        """停战到期后双方 pair 都必须转入冷却，避免单边仍可按停战免战。"""
        ids = await self._npc_ids()
        f = await self.m.get_faction(ids[0])
        tf = await self.m.get_faction(ids[1])
        # tick 0 时 truce_until_tick=0 会被当成“无到期 tick”（旧档兜底口径）
        tick = 50
        self.world["tick_no"] = tick
        state = self.m._npc_ai_state(self.world)
        until = int(time.time()) + 3600  # 真实时间未到期，tick 已到期
        for x, y in ((f["id"], tf["id"]), (tf["id"], f["id"])):
            self.m._pair(state, x, y).update({
                "phase": "truce", "truce_until_tick": tick,
                "no_declare_until_tick": tick})
        self.m._set_relation(f.setdefault("diplomacy", {}), tf["id"],
                             "truce", until=until)
        self.m._set_relation(tf.setdefault("diplomacy", {}), f["id"],
                             "truce", until=until)
        await self.m.save_faction(f)
        await self.m.save_faction(tf)
        await self.m.save_world(self.world)
        snap = await self.m._threat_snapshot(self.world)
        self.m._expire_war_states(self.world, snap, tick)
        for x, y in ((f["id"], tf["id"]), (tf["id"], f["id"])):
            pair = self.m._pair(state, x, y)
            self.assertEqual(pair["phase"], "cooldown")
            self.assertGreater(int(pair["no_declare_until_tick"]), tick)

    async def test_reject_npc_truce_clears_both_ceasefires(self):
        """拒绝 NPC 求和：双方停火窗口都要清零，不能单边继续挂免战。"""
        fid, _, _ = await self._make_player_faction()
        ids = await self._npc_ids()
        npc_f = await self.m.get_faction(ids[0])
        player = await self.m.get_faction(fid)
        tick = int(self.world.get("tick_no", 0) or 0)
        state = self.m._npc_ai_state(self.world)
        for x, y in ((npc_f["id"], fid), (fid, npc_f["id"])):
            self.m._pair(state, x, y).update({
                "phase": "war", "ceasefire_until_tick": tick + 100})
        await self.m.save_world(self.world)
        await self.m._npc_send_truce_offer(
            self.world, npc_f, player,
            self.m._pair(state, npc_f["id"], fid))
        await self.m.upsert_member(fid, "u1", position="leader")
        res = await self.m.diplomacy_respond("u1", npc_f["id"], False)
        self.assertTrue(res.get("ok"))
        world = await self.m.get_world()
        state = self.m._npc_ai_state(world)
        for x, y in ((npc_f["id"], fid), (fid, npc_f["id"])):
            self.assertEqual(
                int(self.m._pair(state, x, y).get(
                    "ceasefire_until_tick", 0) or 0), 0)

    async def _make_player_faction(self):
        """建一个玩家势力并占一个与 NPC 相邻的无主据点，返回 (fid, obs_fid, node_id)。"""
        now = int(time.time())
        fid = await self.m._execute(
            "INSERT INTO strategy_factions (world_id,name,color,kind,leader_id,"
            "personality_json,treasury_json,reputation,level,member_cap,"
            "capital_node_id,faction_state,alive) "
            "VALUES (?,?,?,'player','u1','{}','{}',0,1,10,0,'normal',1)",
            (self.wid, "测试盟", "#123456"))
        await self.m._execute(
            "INSERT INTO strategy_players (user_id,world_id,faction_id,"
            "created_at,newcomer_json) VALUES (?,?,?,?,?)",
            ("u1", self.wid, fid, now, "{}"))
        nodes = await self.m.get_nodes(self.wid)
        by_id = {n["id"]: n for n in nodes}
        chosen, obs = None, 0
        for node in nodes:
            if int(node.get("owner_faction", 0) or 0) != 0 or \
                    node.get("type") == "ruin":
                continue
            for adj in node.get("adj") or []:
                owner = int(by_id[int(adj)].get("owner_faction", 0) or 0)
                if owner > 0:
                    chosen, obs = node["id"], owner
                    break
            if chosen:
                break
        if not chosen:
            self.skipTest("模板中没有与 NPC 相邻的无主据点")
        node = by_id[chosen]
        node["owner_faction"] = fid
        node["protect_until"] = now + 4 * 86400
        node["defense"] = 2000
        node["level"] = 1
        node["npc_garrison"] = {"troops": 300, "morale": 90}
        await self.m.save_node(node)
        faction = await self.m.get_faction(fid)
        faction["capital_node_id"] = chosen
        await self.m.save_faction(faction)
        return fid, obs, chosen

    async def test_can_declare_uses_player_join_time_not_node_shield(self):
        """新手保护看玩家入局时间；节点保护期不阻止 NPC 宣战。"""
        fid, obs, _ = await self._make_player_faction()
        snap = await self.m._threat_snapshot(self.world)
        target = snap["factions"][fid]
        observer = snap["factions"][obs]
        pair = self.m._pair_of(self.world, obs, fid)
        tick = int(self.world.get("tick_no", 0) or 0)
        self.assertGreater(snap["border_raw"][obs][fid], 0)
        self.assertFalse(await self.m._can_declare(
            self.world, snap, observer, target, pair, tick),
            "新手（created_at=now）应被保护")
        # 老玩家 + 全据点保护期：节点保护不得阻止宣战
        await self.m._execute(
            "UPDATE strategy_players SET created_at=? "
            "WHERE user_id=? AND world_id=?",
            (int(time.time()) - 10 * 86400, "u1", self.wid))
        self.assertTrue(await self.m._can_declare(
            self.world, snap, observer, target, pair, tick),
            "节点保护期不应阻止宣战")
        # 主动放弃新手保护
        await self.m._execute(
            "UPDATE strategy_players SET created_at=?, newcomer_json=? "
            "WHERE user_id=? AND world_id=?",
            (int(time.time()), '{"opt_out": true}', "u1", self.wid))
        self.assertTrue(await self.m._can_declare(
            self.world, snap, observer, target, pair, tick),
            "opt_out 后不再受新手保护")

    async def test_zero_morale_and_zero_war_score_not_coerced(self):
        """民心 0 / 战况 0 是合法极端值，不能被 falsy 兜底成 100 / 50。"""
        ids = await self._npc_ids()
        f = await self.m.get_faction(ids[0])
        tf = await self.m.get_faction(ids[1])
        f["treasury"] = {"coin": 10 ** 9, "grain": 10 ** 9}
        await self.m.save_faction(f)
        state = self.m._npc_ai_state(self.world)
        pair = self.m._pair(state, f["id"], tf["id"])
        tick = int(self.world.get("tick_no", 0) or 0)
        pair.update({"phase": "war", "war_start_troops": 0,
                     "war_start_nodes": 0, "last_capture_tick": tick,
                     "war_score": 0, "exhaustion": 0})
        snap = await self.m._threat_snapshot(self.world)
        snap["avg_morale"][f["id"]] = 0
        snap["accum"][f["id"]] = {k: 0 for k in
                                  snap["accum"].get(f["id"], {})}
        self.m._update_war_state(self.world, snap, f, tf, pair)
        # 仅民心压力一项：0 民心 → 10 疲劳（若被当成 100 则为 0）
        self.assertGreaterEqual(pair["exhaustion"], 9.9)

        # 战况 0 在天下视图里应显示 0，而不是被兜底成 50
        pair["war_score"] = 0
        await self.m.save_world(self.world)
        view = self.m._npc_world_view(self.world, {
            f["id"]: {"id": f["id"], "name": f["name"], "kind": "npc",
                      "alive": 1},
            tf["id"]: {"id": tf["id"], "name": tf["name"], "kind": "npc",
                       "alive": 1},
        }, [])
        row = next(r for r in view["factions"] if r["id"] == f["id"])
        war = next(w for w in row["wars"] if w["target"] == tf["id"])
        self.assertEqual(war["war_score"], 0.0)

    async def test_npc_truce_offer_writes_propose_contract(self):
        """NPC→玩家求和走既有 propose_truce 契约（from / indemnity / until）。"""
        fid, _, _ = await self._make_player_faction()
        ids = await self._npc_ids()
        npc_f = await self.m.get_faction(ids[0])
        player = await self.m.get_faction(fid)
        pair = self.m._pair_of(self.world, npc_f["id"], player["id"])
        pair["phase"] = "war"
        await self.m._npc_send_truce_offer(self.world, npc_f, player, pair)
        player2 = await self.m.get_faction(fid)
        entry = (player2.get("diplomacy") or {}).get(str(npc_f["id"])) or {}
        self.assertEqual(entry.get("rel"), "propose_truce")
        self.assertEqual(int(entry.get("from", 0)), int(npc_f["id"]))
        self.assertEqual(int(entry.get("indemnity", -1)), 0)
        self.assertGreater(int(entry.get("until", 0)), int(time.time()))
        npc2 = await self.m.get_faction(npc_f["id"])
        out = (npc2.get("diplomacy") or {}).get(str(fid)) or {}
        self.assertEqual(out.get("rel"), "propose_truce_out")
        # 玩家接受后：外交 + pair 同步进入 48h 停战
        await self.m.upsert_member(fid, "u1", position="leader")
        res = await self.m.diplomacy_respond("u1", npc_f["id"], True)
        self.assertTrue(res.get("ok"))
        player3 = await self.m.get_faction(fid)
        npc3 = await self.m.get_faction(npc_f["id"])
        self.assertEqual(
            ((player3.get("diplomacy") or {}).get(str(npc_f["id"])) or {}
             ).get("rel"), "truce")
        self.assertEqual(
            ((npc3.get("diplomacy") or {}).get(str(fid)) or {}
             ).get("rel"), "truce")
        w2 = await self.m.get_world()
        for x, y in ((npc_f["id"], fid), (fid, npc_f["id"])):
            self.assertEqual(self.m._pair(
                self.m._npc_ai_state(w2), x, y)["phase"], "truce")

    async def test_npc_peace_offer_not_delayed_by_zero_last_offer(self):
        """首场战争的 last_offer_tick=0 不能被当成“tick 0 刚提过和”。"""
        fid, _, _ = await self._make_player_faction()
        ids = await self._npc_ids()
        npc_f = await self.m.get_faction(ids[0])
        # 强制本次掷点必中 + 疲劳必达
        peace = self.m.main_cfg().setdefault("npc", {}).setdefault(
            "peace", {})
        peace.update({"peace_gate": 0, "min_war_ticks": 0,
                      "peace_base_per_day": 2.0, "peace_span_per_day": 0.0,
                      "max_peace_p_day": 1.0, "no_progress_norm": 1})
        for node in await self.m.get_nodes(self.wid):
            if int(node.get("owner_faction", 0) or 0) == npc_f["id"]:
                node["npc_garrison"] = {"troops": 0, "morale": 0}
                node["morale"] = 0
                await self.m.save_node(node)
        npc_f["treasury"] = {}
        await self.m.save_faction(npc_f)
        tick = int(self.world.get("tick_no", 0) or 0)
        state = self.m._npc_ai_state(self.world)
        pair = self.m._pair(state, npc_f["id"], fid)
        pair.update({"phase": "war", "war_start_tick": tick,
                     "war_start_troops": 10 ** 9, "war_start_nodes": 5,
                     "last_capture_tick": max(0, tick - 1000),
                     "war_score": 50, "exhaustion": 0.0,
                     "frenzy_until_tick": 0, "last_offer_tick": 0})
        await self.m._step_npc_peace(self.world)
        player2 = await self.m.get_faction(fid)
        entry = (player2.get("diplomacy") or {}).get(str(npc_f["id"])) or {}
        self.assertEqual(entry.get("rel"), "propose_truce",
                         "首场战争应在满足疲劳条件时立即可以遣使求和")

    async def test_world_view_uses_live_threat_share(self):
        """天下榜份额取当前 tick 快照，不用可能滞后的 share_history。"""
        await self.m._step_npc_diplomacy(self.world)
        ids = await self._npc_ids()
        data = self.world.setdefault("data", {})
        share = dict((data.get("threat") or {}).get("share") or {})
        share[str(ids[0])] = 0.55
        data["threat"]["share"] = share
        data["threat"]["accum"] = {
            str(ids[0]): {str(ids[1]): 90.0}}
        data["threat"]["public"] = {str(ids[0]): 0.0}
        state = self.m._npc_ai_state(self.world)
        state["stats"][str(ids[0])] = {
            "share_history": [{"tick": 0, "share": 0.01}]}
        await self.m.save_world(self.world)
        view = (await self.m.get_state("u1"))["npc_world"]
        row = next(r for r in view["factions"] if r["id"] == ids[0])
        self.assertEqual(row["share"], 55.0)
        self.assertTrue(row["hegemon"])
        # 侵略倍率按该 NPC 自己的最高敌意，而不是“别人对它的威胁”
        self.assertAlmostEqual(row["threat_mult"], 1.72, places=2)

    async def test_declare_war_symmetric_and_reverse_goal(self):
        """宣战双方 pair 对称；防守方不继承 hegemon 目标（防误触发远征）。"""
        ids = await self._npc_ids()
        f = await self.m.get_faction(ids[0])
        tf = await self.m.get_faction(ids[1])
        state = self.m._npc_ai_state(self.world)
        pair = self.m._pair(state, f["id"], tf["id"])
        snap = await self.m._threat_snapshot(self.world)
        await self.m._declare_npc_war(self.world, snap, f, tf, pair, "hegemon")
        tick = int(self.world.get("tick_no", 0) or 0)
        for x, y in ((f["id"], tf["id"]), (tf["id"], f["id"])):
            row = self.m._pair(state, x, y)
            self.assertEqual(row["phase"], "war")
            self.assertGreater(row["frenzy_until_tick"], tick)
        self.assertEqual(self.m._pair(state, f["id"], tf["id"])["goal"],
                         "hegemon")
        self.assertNotEqual(
            self.m._pair(state, tf["id"], f["id"])["goal"], "hegemon")
        # 双方立即开战（无 24h 备战期）
        for fid, other in ((f, tf), (tf, f)):
            entry = (fid.get("diplomacy") or {}).get(str(other["id"])) or {}
            self.assertEqual(int(entry.get("war_at", -1)), 0)

    async def test_pair_phase_hysteresis_boundaries(self):
        """档位边界 20/40/60 与 5 点滞回（退出线 = 进入线 − 5）。"""
        cfg = self.m._npc_threat_cfg()
        factions = {
            1: {"id": 1, "name": "甲", "kind": "npc", "diplomacy": {}},
            2: {"id": 2, "name": "乙", "kind": "npc", "diplomacy": {}},
        }
        world = {"id": self.wid, "tick_no": 10, "data": {"npc_ai": {}}}
        state = self.m._npc_ai_state(world)
        pair = self.m._pair(state, 1, 2)
        pair["phase"] = "calm"

        def step(h, tick):
            snap = {"cfg": cfg, "factions": factions,
                    "diplo_factions": factions,
                    "accum": {1: {2: h}, 2: {1: 0}},
                    "border_raw": {1: {2: 0}, 2: {1: 0}}}
            self.m._update_pair_phases(world, snap, tick)
            return pair["phase"]

        self.assertEqual(step(0, 10), "calm")
        self.assertEqual(step(19.9, 10), "calm")
        self.assertEqual(step(20, 10), "alert")
        self.assertEqual(step(15, 10), "alert")      # 滞回内
        self.assertEqual(step(14.9, 10), "calm")
        self.assertEqual(step(40, 10), "prepare")
        self.assertEqual(step(35, 10), "prepare")    # 滞回内
        self.assertEqual(step(34.9, 10), "alert")
        self.assertEqual(step(60, 12), "hostile")
        self.assertEqual(int(pair["hostile_since_tick"]), 12)
        self.assertEqual(step(55, 13), "hostile")    # 滞回内
        self.assertEqual(step(54.9, 14), "prepare")
        self.assertEqual(step(80, 15), "hostile")    # coalition 并入 hostile

    async def test_pair_phase_hysteresis_follows_config(self):
        """自定义 hostile 阈值时，退出线也应跟随（进 70 / 出 65）。"""
        cfg = self.m._npc_threat_cfg()
        cfg["tiers"] = {"alert": 20, "prepare": 40, "hostile": 70,
                        "coalition": 90}
        factions = {
            1: {"id": 1, "name": "甲", "kind": "npc", "diplomacy": {}},
            2: {"id": 2, "name": "乙", "kind": "npc", "diplomacy": {}},
        }
        world = {"id": self.wid, "tick_no": 10, "data": {"npc_ai": {}}}
        state = self.m._npc_ai_state(world)
        pair = self.m._pair(state, 1, 2)
        pair["phase"] = "hostile"
        snap = {"cfg": cfg, "factions": factions,
                "diplo_factions": factions,
                "accum": {1: {2: 63}, 2: {1: 0}},
                "border_raw": {1: {2: 0}, 2: {1: 0}}}
        self.m._update_pair_phases(world, snap, 10)
        self.assertEqual(pair["phase"], "prepare")

    async def test_npc_ai_skips_truce_target(self):
        """签订停战后同一 tick 的 AI 不得再向该目标出兵（避免立刻撕约）。"""
        ids = await self._npc_ids()
        nodes = await self.m.get_nodes(self.wid)
        by_id = {n["id"]: n for n in nodes}
        found = None
        for node in nodes:
            owner = int(node.get("owner_faction", 0) or 0)
            if owner not in ids:
                continue
            for adj in node.get("adj") or []:
                other = int(by_id[int(adj)].get("owner_faction", 0) or 0)
                if other in ids and other != owner:
                    found = (owner, other)
                    break
            if found:
                break
        if not found:
            self.skipTest("模板中没有相邻的 NPC 势力")
        a_id, b_id = found
        # 把除 b 外的据点全部并给 a，制造“唯一可攻目标 = b”的受控场景
        for node in nodes:
            owner = int(node.get("owner_faction", 0) or 0)
            if owner == b_id:
                node["npc_garrison"] = {"troops": 100, "morale": 100}
                node["protect_until"] = 0
            else:
                node["owner_faction"] = a_id
                node["npc_garrison"] = {"troops": 5000, "morale": 100}
                node["protect_until"] = 0
            await self.m.save_node(node)
        a = await self.m.get_faction(a_id)
        b = await self.m.get_faction(b_id)
        a["treasury"] = {"coin": 10 ** 9, "grain": 10 ** 9}
        a["diplomacy"] = {str(b_id): {"rel": "truce", "score": 0}}
        b["diplomacy"] = {str(a_id): {"rel": "truce", "score": 0}}
        await self.m.save_faction(a)
        await self.m.save_faction(b)
        await self.m._step_npc_ai(self.world)
        b_nodes = {n["id"] for n in await self.m.get_nodes(self.wid)
                   if int(n.get("owner_faction", 0) or 0) == b_id}

        def _toward_b(armies):
            for army in armies:
                path = [int(x) for x in (army.get("path") or [])]
                if set(path) & b_nodes:
                    return True
            return False

        self.assertFalse(_toward_b(
            await self.m.get_armies(self.wid, faction_id=a_id)),
            "停战目标不应被选为出征目标")

        # 外交真实时间先过期、但 pair 仍在停战期：AI 仍应停手
        a = await self.m.get_faction(a_id)
        b = await self.m.get_faction(b_id)
        a["diplomacy"] = {str(b_id): {"rel": "neutral", "score": 0}}
        b["diplomacy"] = {str(a_id): {"rel": "neutral", "score": 0}}
        await self.m.save_faction(a)
        await self.m.save_faction(b)
        tick = int(self.world.get("tick_no", 0) or 0)
        pair = self.m._pair(self.m._npc_ai_state(self.world), a_id, b_id)
        pair.update({"phase": "truce", "truce_until_tick": tick + 1000,
                     "no_declare_until_tick": tick + 1000})
        await self.m._step_npc_ai(self.world)
        self.assertFalse(_toward_b(
            await self.m.get_armies(self.wid, faction_id=a_id)),
            "pair 仍处 truce 时 AI 不得进攻")

    async def test_expedition_bfs_respects_max_edges(self):
        """霸主远征目标用内存 BFS：最近目标可达，超 max_edges 不派兵。"""
        max_edges = int(self.m._npc_war_cfg()["expedition"]["max_edges"])
        min_troops = int(self.m._npc_war_cfg()["expedition"]["min_troops"])
        world = {"id": self.wid, "tick_no": 1000, "data": {"npc_ai": {}}}
        near = {
            1: {"id": 1, "owner_faction": 1, "adj": [2],
                "npc_garrison": {"troops": min_troops + 200}},
            2: {"id": 2, "owner_faction": 2, "adj": [1],
                "npc_garrison": {"troops": 10}},
        }
        res = await self.m._npc_expedition_target(
            world, {"nodes": near}, {"id": 1}, {"id": 2})
        self.assertIsNotNone(res)
        self.assertEqual(int(res["id"]), 2)
        self.assertEqual(int(res["_from_node"]), 1)

        # 更近的目标据点处于保护期：应跳过，选更远的可攻据点
        shielded = {
            1: {"id": 1, "owner_faction": 1, "adj": [2],
                "npc_garrison": {"troops": min_troops + 200}},
            2: {"id": 2, "owner_faction": 2, "adj": [1, 3],
                "protect_until": int(time.time()) + 3600,
                "npc_garrison": {"troops": 10}},
            3: {"id": 3, "owner_faction": 2, "adj": [2],
                "protect_until": 0, "npc_garrison": {"troops": 10}},
        }
        res = await self.m._npc_expedition_target(
            world, {"nodes": shielded}, {"id": 1}, {"id": 2})
        self.assertIsNotNone(res)
        self.assertEqual(int(res["id"]), 3)

        # 目标只在 max_edges + 1 跳外：不可达
        far = {}
        total = max_edges + 2
        for i in range(1, total + 1):
            adj = []
            if i > 1:
                adj.append(i - 1)
            if i < total:
                adj.append(i + 1)
            far[i] = {"id": i, "owner_faction": 1 if i == 1 else 0,
                      "adj": adj,
                      "npc_garrison": {"troops": min_troops + 200}}
        far[total]["owner_faction"] = 2
        res = await self.m._npc_expedition_target(
            world, {"nodes": far}, {"id": 1}, {"id": 2})
        self.assertIsNone(res)

    async def test_alliance_declaration_resets_member_war_at(self):
        """对联盟盟主宣战后，成员的外交 war_at 也必须清零（立即开战）。"""
        ids = await self._npc_ids()
        obs = await self.m.get_faction(ids[0])
        mem = await self.m.get_faction(ids[1])
        host = await self.m.get_faction(ids[2])
        mem["alliance_id"] = host["id"]
        host["alliance_id"] = host["id"]
        for f in (obs, mem, host):
            f["diplomacy"] = {}
            await self.m.save_faction(f)
        pair = self.m._pair_of(self.world, obs["id"], host["id"])
        snap = await self.m._threat_snapshot(self.world)
        await self.m._declare_npc_war(self.world, snap, obs, host, pair,
                                      "border")
        mem2 = await self.m.get_faction(mem["id"])
        host2 = await self.m.get_faction(host["id"])
        for f in (mem2, host2):
            entry = (f.get("diplomacy") or {}).get(str(obs["id"])) or {}
            self.assertEqual(entry.get("rel"), "war")
            self.assertEqual(int(entry.get("war_at", -1)), 0)

    async def test_player_alliance_war_declaration_notifies_all_members(self):
        """NPC 对玩家联盟宣战：盟主 + 全体成员立即开战，并逐人推送通知。"""
        host_fid, obs, _ = await self._make_player_faction()
        # 联盟成员势力（u2）并入盟主
        mem_fid = await self.m._execute(
            "INSERT INTO strategy_factions (world_id,name,color,kind,leader_id,"
            "personality_json,treasury_json,reputation,level,member_cap,"
            "capital_node_id,faction_state,alive,alliance_id) VALUES "
            "(?,?,'#654321','player','u2','{}','{}',0,1,10,0,'normal',1,?)",
            (self.wid, "盟友势力", host_fid))
        now = int(time.time())
        await self.m._execute(
            "INSERT INTO strategy_players (user_id,world_id,faction_id,"
            "created_at,newcomer_json) VALUES (?,?,?,?,?)",
            ("u2", self.wid, mem_fid, now - 10 * 86400, "{}"))
        await self.m._execute(
            "UPDATE strategy_factions SET alliance_id=? WHERE id=?",
            (host_fid, host_fid))
        await self.m._commit()
        notified: list[tuple[str, str]] = []

        async def fake_notify(uid, kind, payload):
            notified.append((str(uid), str(kind)))

        self.m._notifier = fake_notify
        world = await self.m.get_world()
        snap = await self.m._threat_snapshot(world)
        observer = snap["factions"][obs]
        target = snap["factions"][host_fid]
        pair = self.m._pair_of(world, obs, host_fid)
        await self.m._declare_npc_war(world, snap, observer, target, pair,
                                      "border")
        # 盟主与成员的外交都立即进入战争（war_at=0，无 24h 备战）
        for fid in (host_fid, mem_fid):
            row = await self.m.get_faction(fid)
            entry = (row.get("diplomacy") or {}).get(str(obs)) or {}
            self.assertEqual(entry.get("rel"), "war")
            self.assertEqual(int(entry.get("war_at", -1)), 0)
        # 通知对象必须覆盖盟主与成员两个玩家
        self.assertEqual({uid for uid, _ in notified}, {"u1", "u2"})
        self.assertTrue(all(kind == "war_declared" for _, kind in notified))

    async def test_alliance_capture_counts_toward_host_war(self):
        """攻打联盟成员据点也要记入对盟主战争的战果（war_score 才推进）。"""
        ids = await self._npc_ids()
        obs = await self.m.get_faction(ids[0])
        mem = await self.m.get_faction(ids[1])
        host = await self.m.get_faction(ids[2])
        mem["alliance_id"] = host["id"]
        host["alliance_id"] = host["id"]
        await self.m.save_faction(mem)
        await self.m.save_faction(host)
        state = self.m._npc_ai_state(self.world)
        pair = self.m._pair(state, obs["id"], host["id"])
        pair.update({"phase": "war", "captures_by_me": 0,
                     "captures_by_target": 0})
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n.get("owner_faction", 0) or 0) == mem["id"])
        await self.m._capture_node(self.world, node, obs["id"])
        self.assertEqual(pair["captures_by_me"], 1)
        self.assertNotIn(str(mem["id"]),
                         state["pairs"].get(str(obs["id"]), {}))




if __name__ == "__main__":
    unittest.main()
