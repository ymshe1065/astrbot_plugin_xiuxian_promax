# -*- coding: utf-8 -*-
"""「仙域争霸」v4.6.1 R4 运行时压测回归（CI 快速版）。

覆盖长局才会暴露的问题：
- 旧档 / 损坏 npc_ai 字段不能让 tick 永久回滚；
- 低兵 / 高兵力闲置 NPC 军团回城后都要并入驻军，不能在长局里无限堆积；
- 灭亡 NPC 势力的在外军团必须清退，不能攻下节点形成幽灵势力；
- 灭亡势力围城军清退后必须清除悬挂的 ``siege_state``（否则城防自修/建造/传送永久阻塞）；
- 威胁外交的宣战上限（每 tick / 每日，含第 0 天）与不接壤不宣战；
- 赛季结算后新世界不得继承 npc_ai；读档后威胁状态连续。

真正的数百 tick 长压测放在 ``if __name__ == "__main__"`` 里，不在 CI 跑。
"""
import itertools
import json
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


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class _ZeroRng:
    """强制外交掷点必中（仅用于 npc_declare / npc_peace）。"""

    def random(self):
        return 0.0

    def randint(self, a, b):
        return a

    def choice(self, seq):
        return seq[0]


class TestStrategyRuntimeStress(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        sm_mod = _imp("managers.strategy_manager")
        self.StrategyManager = sm_mod.StrategyManager
        self.StrategyError = sm_mod.StrategyError

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
            user_id="u1", gold=100000, user_name="测试道人"))

        self.m = self.StrategyManager(self.db)
        self.m.main_cfg().setdefault("hex", {})["field_encounter_mode"] = "auto"
        self.m.main_cfg()["tick_minutes"] = 15
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

    def _patch_rng(self, keys):
        orig = self.m._rng

        def rng(*seeds):
            if seeds and str(seeds[0]) in keys:
                return _ZeroRng()
            return orig(*seeds)

        self.m._rng = rng
        return lambda: setattr(self.m, "_rng", orig)

    async def _next_tick(self):
        world = await self.m.get_world()
        world["tick_no"] = int(world.get("tick_no", 0)) + 1
        await self.m.save_world(world)
        await self.m._commit()
        await self.m._step_npc_diplomacy(world)
        world = await self.m.get_world()
        return world, self.m._npc_ai_state(world)

    # ------------------------------------------------------------------
    # 1. 损坏旧档自愈
    # ------------------------------------------------------------------
    async def test_corrupt_npc_ai_fields_self_heal_and_tick_advances(self):
        await self.m._tick_locked(False, {})
        ids = await self._npc_ids()
        world = await self.m.get_world()
        tpd = self.m._ticks_per_day()
        ai = self.m._npc_ai_state(world)
        ai["declarations"] = {
            "day": int(world.get("tick_no", 0)) // tpd, "count": "NaN"}
        # 单测故意同时覆盖：非 dict 行 / 字符串数值 / 非法 phase / 超大整数
        ai["accum"] = {str(ids[0]): {str(ids[1]): "oops"}}
        ai["stats"] = {str(ids[0]): {
            "captures_7d": ["bad", 3], "attacks_7d": "no",
            "share_history": "no"}}
        pair = self.m._pair(ai, ids[0], ids[1])
        pair.update({"phase": "truce", "truce_until_tick": 10 ** 400,
                     "no_declare_until_tick": "NaN",
                     "war_score": "NaN", "exhaustion": "x",
                     "hostile_since_tick": "?"})
        ai["pairs"][str(ids[0])]["broken-row"] = ["not", "dict"]
        # json 可承载超大整数：float() 会抛 OverflowError，必须被 _safe_int 兜住
        ai["declarations"]["count"] = 10 ** 400
        world["data"]["winds"] = "broken"
        await self.m.save_world(world)
        await self.m._commit()

        before = int(world.get("tick_no", 0))
        summary = await self.m._tick_locked(False, {})
        self.assertTrue(summary.get("ok"), summary.get("error"))
        world = await self.m.get_world()
        self.assertEqual(int(world.get("tick_no", 0)), before + 1,
                         "损坏的 npc_ai 不应让 tick 永久回滚")
        ai = self.m._npc_ai_state(world)
        self.assertIsInstance(ai["declarations"].get("count"), int)
        self.assertIsInstance(ai["declarations"].get("day"), int)
        cleaned = ai["pairs"][str(ids[0])][str(ids[1])]
        self.assertIsInstance(cleaned["truce_until_tick"], int)
        self.assertIsInstance(cleaned["no_declare_until_tick"], int)
        for key in ("war_score", "exhaustion"):
            self.assertIsInstance(cleaned[key], float)
            self.assertGreaterEqual(cleaned[key], 0.0)
            self.assertLessEqual(cleaned[key], 100.0)
        for value in (ai["accum"].get(str(ids[0])) or {}).values():
            self.assertIsInstance(value, float)
        stat = ai["stats"].get(str(ids[0])) or {}
        self.assertIsInstance(stat.get("captures_7d"), list)
        self.assertIsInstance(stat.get("attacks_7d"), list)
        self.assertIsInstance(stat.get("share_history"), list)
        # 非 dict 的 pair 目标行已被丢弃 / 重建
        self.assertNotIn("broken-row", ai["pairs"][str(ids[0])])
        self.assertIsInstance((world.get("data") or {}).get("winds"), list,
                              "损坏的 winds 应在 tick 结束时自愈为列表")
        # world.data 仍可序列化
        json.dumps(world.get("data") or {}, ensure_ascii=False)

        # 非列表 winds 读视图不崩、tick 后自愈
        self.assertIsInstance((world.get("data") or {}).get("winds"), list)
        # 列表里单条 visible_at 损坏也不能让 _gc_world_data 抛错卡死 tick
        world["data"]["winds"] = [{"visible_at": "abc", "text": "旧风声",
                                   "kind": "war", "tick": "?"}]
        await self.m.save_world(world)
        await self.m._commit()
        view = self.m._npc_world_view(world, {})
        self.assertIsInstance(view.get("winds"), list)
        summary = await self.m._tick_locked(False, {})
        self.assertTrue(summary.get("ok"), summary.get("error"))
        world = await self.m.get_world()
        self.assertIsInstance((world.get("data") or {}).get("winds"), list)

    # ------------------------------------------------------------------
    # 2. 低兵残军归队 + 灭亡势力军团清退
    # ------------------------------------------------------------------
    async def test_idle_low_troop_npc_army_merges_into_home_garrison(self):
        ids = await self._npc_ids()
        fid = ids[0]
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n.get("owner_faction", 0) or 0) == fid)
        faction = await self.m.get_faction(fid)
        # 国库低于守财底线：本 tick 不发动新的出征， garrison 只受并军影响
        faction["treasury"] = {"coin": 0, "grain": 0}
        await self.m.save_faction(faction)
        garrison_before = int(
            (node.get("npc_garrison") or {}).get("troops", 0) or 0)
        aid = await self.m.insert_army({
            "world_id": self.wid, "faction_id": fid, "name": "残军",
            "node_id": int(node["id"]), "troops": 120, "grain": 0,
            "morale": 80, "state": "idle"})
        # 高兵力闲置军团同样不会归队/复用：必须一并并入驻军，否则长局
        # 会以 400~700 兵/支的速度无限堆积（实测 1500 tick 达 60+ 支）
        aid2 = await self.m.insert_army({
            "world_id": self.wid, "faction_id": fid, "name": "归师",
            "node_id": int(node["id"]), "troops": 460, "grain": 0,
            "morale": 80, "state": "idle"})
        await self.m._commit()
        await self.m._step_npc_ai(await self.m.get_world())
        await self.m._commit()
        self.assertIsNone(await self.m.get_army(aid),
                          "低兵残军应并入驻军后解散")
        self.assertIsNone(await self.m.get_army(aid2),
                          "高兵力闲置军团也应并入驻军，不能无限堆积")
        node_after = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(
            int((node_after.get("npc_garrison") or {}).get("troops", 0)),
            garrison_before + 580, "两支军团的兵力都应回到本城驻军")

    async def test_dead_npc_faction_armies_disbanded_no_ghost_capture(self):
        """灭亡势力在外军团必须清退：否则会攻下节点变成幽灵势力。"""
        fid = await self.m._execute(
            "INSERT INTO strategy_factions (world_id,name,color,kind,leader_id,"
            "personality_json,treasury_json,reputation,level,member_cap,"
            "capital_node_id,faction_state,alive) VALUES "
            "(?,?,'#999999','npc','','{}','{}',0,1,10,0,'normal',1)",
            (self.wid, "将亡之盟"))
        await self.m._commit()
        # 预置该势力的 pair / accum / stats，验证灭亡当 tick 即清理
        world = await self.m.get_world()
        ai = self.m._npc_ai_state(world)
        other = (await self._npc_ids())[0]
        self.m._pair(ai, fid, other)["phase"] = "war"
        self.m._pair(ai, other, fid)["phase"] = "war"
        ai["accum"].setdefault(str(fid), {})[str(other)] = 80.0
        ai["accum"].setdefault(str(other), {})[str(fid)] = 80.0
        ai["stats"].setdefault(str(fid), {})["captures_7d"] = [1]
        await self.m.save_world(world)
        await self.m._commit()
        # 无据点 + 一支在外军团
        free = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n.get("owner_faction", 0) or 0) == 0)
        aid = await self.m.insert_army({
            "world_id": self.wid, "faction_id": fid, "name": "孤军",
            "node_id": int(free["id"]), "troops": 900, "grain": 10,
            "morale": 80, "state": "idle"})
        await self.m._commit()
        world = await self.m.get_world()
        await self.m._step_npc_ai(world)
        # 生产 tick 中后续 _step_season 会保存 world；单测手动落盘等价
        await self.m.save_world(world)
        await self.m._commit()
        f = await self.m.get_faction(fid)
        self.assertEqual(int(f.get("alive", 1) or 0), 0)
        self.assertEqual(f.get("faction_state"), "destroyed")
        self.assertIsNone(await self.m.get_army(aid),
                          "灭亡势力的军团应被清退")
        world = await self.m.get_world()
        ai = self.m._npc_ai_state(world)
        self.assertNotIn(str(fid), ai.get("pairs") or {})
        self.assertNotIn(str(fid), ai.get("accum") or {})
        self.assertNotIn(str(fid), ai.get("stats") or {})
        for row in (ai.get("pairs") or {}).values():
            self.assertNotIn(str(fid), row)
        for row in (ai.get("accum") or {}).values():
            self.assertNotIn(str(fid), row)
        for node in await self.m.get_nodes(self.wid):
            self.assertNotEqual(int(node.get("owner_faction", 0) or 0), fid,
                                "灭亡势力不得继续拥有节点")
        # 直接调用 _capture_node 也必须拒绝（兜底所有占城路径）
        world = await self.m.get_world()
        free = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n.get("owner_faction", 0) or 0) == 0)
        owner_before = int(free.get("owner_faction", 0) or 0)
        freed = await self.m._capture_node(world, free, fid)
        self.assertEqual(freed, [])
        fresh = await self.m.get_node(self.wid, int(free["id"]))
        self.assertEqual(int(fresh.get("owner_faction", 0) or 0), owner_before)

    # ------------------------------------------------------------------
    # 2b. 灭亡势力围城军清退后不得留下悬挂 siege_state
    # ------------------------------------------------------------------
    async def test_dead_npc_siege_army_clears_orphan_siege_state(self):
        """灭亡势力的围城军被清退后，目标节点不得永久显示“围城中”。

        ``_step_sieges`` 只遍历现存围城军团；军团被删除后该节点永远
        轮不到结算，城防自修 / 建造 / 人物传送会被一直阻塞。
        """
        fid = await self.m._execute(
            "INSERT INTO strategy_factions (world_id,name,color,kind,leader_id,"
            "personality_json,treasury_json,reputation,level,member_cap,"
            "capital_node_id,faction_state,alive) VALUES "
            "(?,?,'#888888','npc','','{}','{}',0,1,10,0,'normal',1)",
            (self.wid, "将亡围城盟"))
        await self.m._commit()
        # 选一个属于存活势力的据点，预置“被该灭亡势力围城”的状态
        alive_ids = set(await self._npc_ids())
        alive_ids.discard(fid)
        target = next(n for n in await self.m.get_nodes(self.wid)
                      if int(n.get("owner_faction", 0) or 0) in alive_ids)
        target["siege_state"] = {"ticks": 3, "attacker_faction": fid,
                                 "choice": "auto"}
        await self.m.save_node(target)
        aid = await self.m.insert_army({
            "world_id": self.wid, "faction_id": fid, "name": "围城孤军",
            "node_id": int(target["id"]), "troops": 900, "grain": 0,
            "morale": 80, "state": "siege"})
        await self.m._commit()
        await self.m._step_npc_ai(await self.m.get_world())
        await self.m._commit()
        self.assertIsNone(await self.m.get_army(aid),
                          "灭亡势力的围城军应被清退")
        fresh = await self.m.get_node(self.wid, int(target["id"]))
        self.assertEqual(fresh.get("siege_state") or {}, {},
                         "围城军被清退后必须清除悬挂的 siege_state")

    # ------------------------------------------------------------------
    # 3. 宣战上限 / 不接壤不宣战
    # ------------------------------------------------------------------
    async def _clear_diplomacy_and_inject_h(self, pairs_h):
        for fid in await self._npc_ids():
            f = await self.m.get_faction(fid)
            f["diplomacy"] = {}
            await self.m.save_faction(f)
        world = await self.m.get_world()
        ai = self.m._npc_ai_state(world)
        ai["pairs"] = {}
        ai["accum"] = {}
        ai["declarations"] = {"day": int(world.get("tick_no", 0)) //
                              self.m._ticks_per_day(), "count": 0}
        for observer, target in pairs_h:
            ai["accum"].setdefault(str(observer), {})[str(target)] = 98.0
        world["data"]["npc_ai"] = ai
        await self.m.save_world(world)
        await self.m._commit()

    async def test_threat_declaration_tick_and_day_caps_including_day_zero(self):
        ids = await self._npc_ids()
        snap = await self.m._threat_snapshot(self.world)
        pairs = [(a, b) for a, b in itertools.permutations(ids, 2)
                 if str(snap["factions"][a].get("kind")) == "npc"
                 and str(snap["factions"][b].get("kind")) == "npc"]
        await self._clear_diplomacy_and_inject_h(pairs)
        restore = self._patch_rng(("npc_declare",))
        # 在真正宣战前记录发起方当前战争数（比事后从 pair 反推可靠）
        observed_active_wars = []
        orig_declare = self.m._declare_npc_war

        async def spy_declare(world, snap2, observer, target, pair, goal):
            active = sum(
                1 for other, rp in
                (self.m._npc_ai_state(world)["pairs"].get(
                    str(observer["id"])) or {}).items()
                if isinstance(rp, dict) and rp.get("phase") == "war"
                and int(other) != int(target["id"]))
            observed_active_wars.append(active)
            return await orig_declare(world, snap2, observer, target, pair, goal)

        self.m._declare_npc_war = spy_declare
        tpd = self.m._ticks_per_day()
        per_tick = {}
        per_day = {}
        try:
            for _ in range(tpd + 10):
                world = await self.m.get_world()
                ai = self.m._npc_ai_state(world)
                before = {
                    (int(o), int(t)): p.get("last_declare_tick")
                    for o, row in (ai.get("pairs") or {}).items()
                    for t, p in (row or {}).items()
                    if isinstance(p, dict)}
                world["tick_no"] = int(world.get("tick_no", 0)) + 1
                await self.m.save_world(world)
                await self.m._commit()
                await self.m._step_npc_diplomacy(world)
                world = await self.m.get_world()
                ai = self.m._npc_ai_state(world)
                tick = int(world.get("tick_no", 0))
                day = tick // tpd
                for o, row in (ai.get("pairs") or {}).items():
                    for t, p in (row or {}).items():
                        if not isinstance(p, dict):
                            continue
                        if int(p.get("last_declare_tick", 0) or 0) != tick:
                            continue
                        if before.get((int(o), int(t))) == tick:
                            continue
                        if int(o) >= int(t):
                            continue
                        per_tick[tick] = per_tick.get(tick, 0) + 1
                        per_day[day] = per_day.get(day, 0) + 1
        finally:
            self.m._declare_npc_war = orig_declare
            restore()
        self.assertTrue(per_day, "注入高敌意后应能触发宣战")
        self.assertTrue(observed_active_wars, "应实际走到 _declare_npc_war")
        self.assertLessEqual(max(per_tick.values()), 1,
                             "每 tick 最多一场新战争")
        self.assertLessEqual(per_day.get(0, 0), 4,
                             "第 0 天（tick<96）宣战不得超过 4 场")
        self.assertLessEqual(max(per_day.values()), 4,
                             "每日宣战不得超过 4 场")
        cap = int(self.m._npc_war_cfg()["max_active_wars_per_npc"])
        self.assertTrue(all(n < cap for n in observed_active_wars),
                        f"宣战发起方同时战争数必须 < {cap}: {observed_active_wars}")

    async def test_expedition_cooldown_and_min_troops_guard(self):
        """霸主远征：冷却 ≥8 tick、出发点守军 ≥800 才可作为远征源。"""
        ids = await self._npc_ids()
        a, b = ids[0], ids[1]
        world = {"id": self.wid, "tick_no": 100, "data": {"npc_ai": {}}}
        nodes = {
            1: {"id": 1, "owner_faction": a, "adj": [2],
                "npc_garrison": {"troops": 2000}},
            2: {"id": 2, "owner_faction": b, "adj": [1],
                "npc_garrison": {"troops": 10}},
        }
        pair = self.m._pair(self.m._npc_ai_state(world), a, b)
        cfg = self.m._npc_war_cfg()["expedition"]
        cooldown = int(cfg["cooldown_ticks"])
        pair["last_expedition_tick"] = 100
        self.assertIsNone(await self.m._npc_expedition_target(
            world, {"nodes": nodes}, {"id": a}, {"id": b}),
            f"冷却 {cooldown} tick 内不得再派远征军")
        pair["last_expedition_tick"] = 100 - cooldown
        self.assertIsNotNone(await self.m._npc_expedition_target(
            world, {"nodes": nodes}, {"id": a}, {"id": b}))
        nodes[1]["npc_garrison"] = {
            "troops": int(cfg["min_troops"]) - 1}
        self.assertIsNone(await self.m._npc_expedition_target(
            world, {"nodes": nodes}, {"id": a}, {"id": b}),
            "出发点守军不足 min_troops 时不得远征")

    async def test_threat_does_not_declare_nonadjacent_nonhegemon(self):
        ids = await self._npc_ids()
        snap = await self.m._threat_snapshot(self.world)
        chosen = None
        threshold = float(
            self.m._npc_threat_cfg()["hegemon_share_threshold"])
        for a, b in itertools.permutations(ids, 2):
            if a >= b:
                continue
            if float(snap["border_raw"][a][b]) > 0:
                continue
            if snap["hegemon"].get(a) or snap["hegemon"].get(b):
                continue
            if float(snap["share"].get(a, 0)) >= threshold or \
                    float(snap["share"].get(b, 0)) >= threshold:
                continue
            chosen = (a, b)
            break
        self.assertIsNotNone(chosen, "模板中应存在不接壤且非霸主的 NPC 对")
        a, b = chosen
        await self._clear_diplomacy_and_inject_h([(a, b), (b, a)])
        restore = self._patch_rng(("npc_declare",))
        try:
            for _ in range(30):
                await self._next_tick()
        finally:
            restore()
        world = await self.m.get_world()
        ai = self.m._npc_ai_state(world)
        pair = self.m._pair(ai, a, b)
        self.assertNotEqual(pair["phase"], "war",
                            "不接壤且非霸主的 NPC 对不得经由威胁层宣战")

    # ------------------------------------------------------------------
    # 4. 读档 / 赛季重置
    # ------------------------------------------------------------------
    async def test_reload_keeps_threat_state_and_can_continue(self):
        for _ in range(5):
            await self.m._tick_locked(False, {})
        await self.m._commit()
        world = await self.m.get_world()
        before = json.dumps(world["data"].get("npc_ai") or {},
                            sort_keys=True, ensure_ascii=False)
        m2 = self.StrategyManager(self.db)
        m2.main_cfg().setdefault("hex", {})["field_encounter_mode"] = "auto"
        m2.main_cfg()["tick_minutes"] = 15
        world2 = await m2.ensure_world()
        self.assertEqual(int(world2["id"]), int(world["id"]))
        after = json.dumps(world2["data"].get("npc_ai") or {},
                           sort_keys=True, ensure_ascii=False)
        self.assertEqual(before, after)
        summary = await m2._tick_locked(False, {})
        self.assertTrue(summary.get("ok"), summary.get("error"))
        world3 = await m2.get_world()
        self.assertEqual(int(world3["tick_no"]), int(world["tick_no"]) + 1)

    async def test_season_settlement_starts_clean_world_without_npc_ai(self):
        for _ in range(3):
            await self.m._tick_locked(False, {})
        world = await self.m.get_world()
        self.assertIn("npc_ai", world["data"])
        old_id = int(world["id"])
        world["ends_at"] = int(time.time()) - 10
        await self.m.save_world(world)
        await self.m._commit()
        summary = await self.m._tick_locked(False, {})
        self.assertTrue(summary.get("ok"), summary.get("error"))
        new_world = await self.m.get_world()
        self.assertNotEqual(int(new_world["id"]), old_id)
        self.assertEqual(new_world["state"], "active")
        self.assertNotIn("npc_ai", new_world.get("data") or {},
                         "新赛季世界不得继承旧 npc_ai 状态")
        old_row = await self.m._fetchone(
            "SELECT state FROM strategy_worlds WHERE id=?", (old_id,))
        self.assertEqual(old_row["state"], "ended")

    # ------------------------------------------------------------------
    # 5. 快速长跑冒烟：60 tick 不变量
    # ------------------------------------------------------------------
    async def test_short_long_run_keeps_invariants(self):
        for _ in range(30):
            summary = await self.m._tick_locked(False, {})
            self.assertTrue(summary.get("ok"), summary.get("error"))
        world = await self.m.get_world()
        data = world.get("data") or {}
        json.dumps(data, ensure_ascii=False)  # 可序列化
        ai = data.get("npc_ai") or {}
        for key in ("pairs", "accum", "stats"):
            self.assertIsInstance(ai.get(key), dict)
        alive = {int(r["id"]) for r in await self.m._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND alive=1",
            (self.wid,))}
        for o, row in (ai.get("pairs") or {}).items():
            self.assertIn(int(o), alive, "灭亡势力 pair 不得残留")
            for t, pair in (row or {}).items():
                self.assertIn(int(t), alive)
                self.assertIn(pair.get("phase"), (
                    "calm", "alert", "prepare", "hostile", "war",
                    "truce", "cooldown"))
                self.assertGreaterEqual(float(pair.get("war_score", 50)), 0)
                self.assertLessEqual(float(pair.get("war_score", 50)), 100)
                self.assertGreaterEqual(float(pair.get("exhaustion", 0)), 0)
                self.assertLessEqual(float(pair.get("exhaustion", 0)), 100)
        for o, row in (ai.get("accum") or {}).items():
            self.assertIn(int(o), alive)
            for value in (row or {}).values():
                self.assertGreaterEqual(float(value), 0)
                self.assertLessEqual(float(value), 100)
        for node in await self.m.get_nodes(self.wid):
            own = int(node.get("owner_faction", 0) or 0)
            self.assertFalse(own > 0 and own not in alive,
                             f"幽灵节点 {node['id']} 属于灭亡势力 {own}")
        for army in await self.m.get_armies(self.wid):
            fid = int(army.get("faction_id", 0) or 0)
            self.assertFalse(fid > 0 and fid not in alive,
                             f"灭亡势力 {fid} 仍有军团 {army['id']}")
        winds = data.get("winds")
        if isinstance(winds, list):
            self.assertLessEqual(
                len(winds), int(self.m._npc_wind_cfg()["keep"]))
        # 天下视图不显示死亡势力 / 幽灵战争
        f_meta = {}
        for f in self.m._decode_all("strategy_factions", await self.m._fetchall(
                "SELECT * FROM strategy_factions WHERE world_id=?", (self.wid,))):
            f_meta[int(f["id"])] = f
        view = self.m._npc_world_view(world, f_meta)
        self.assertTrue(all(r["id"] in alive for r in view["factions"]))
        for row in view["factions"]:
            for war in row["wars"]:
                self.assertIn(int(war["target"]), alive)


if __name__ == "__main__":
    # 手动长压测：默认 300 tick（可用 TICKS / TMIN 环境变量覆盖）
    import asyncio

    TICKS = int(os.environ.get("TICKS", "300"))
    TMIN = int(os.environ.get("TMIN", "1"))

    async def _stress():
        case = TestStrategyRuntimeStress("test_short_long_run_keeps_invariants")
        await case.asyncSetUp()
        case.m.main_cfg()["tick_minutes"] = TMIN
        t0 = time.time()
        try:
            for i in range(TICKS):
                summary = await case.m._tick_locked(False, {})
                if not summary.get("ok"):
                    print(f"tick {i} failed: {summary.get('error')}")
                    break
            world = await case.m.get_world()
            ai = world["data"].get("npc_ai") or {}
            alive = {int(r["id"]) for r in await case.m._fetchall(
                "SELECT id FROM strategy_factions WHERE world_id=? AND alive=1",
                (case.wid,))}
            errors = []
            json.dumps(world["data"], ensure_ascii=False)
            for o, row in (ai.get("pairs") or {}).items():
                if int(o) not in alive:
                    errors.append(f"dead pair owner {o}")
                for t, p in (row or {}).items():
                    if int(t) not in alive:
                        errors.append(f"dead pair target {o}->{t}")
                    for key in ("war_score", "exhaustion"):
                        value = float(p.get(key, 0) or 0)
                        if not 0 <= value <= 100:
                            errors.append(f"{key} out {o}->{t}={value}")
            for node in await case.m.get_nodes(case.wid):
                own = int(node.get("owner_faction", 0) or 0)
                if own > 0 and own not in alive:
                    errors.append(f"ghost node {node['id']} owner {own}")
            for army in await case.m.get_armies(case.wid):
                fid = int(army.get("faction_id", 0) or 0)
                if fid > 0 and fid not in alive:
                    errors.append(f"ghost army {army['id']} owner {fid}")
            print(f"ticks={TICKS} tmin={TMIN} elapsed={time.time()-t0:.1f}s "
                  f"per_tick={(time.time()-t0)/TICKS*1000:.1f}ms")
            print("data size", len(json.dumps(world["data"], ensure_ascii=False)))
            print("pairs", sum(len(r) for r in (ai.get("pairs") or {}).values()))
            print("armies", len(await case.m.get_armies(case.wid)))
            print("invariant errors", len(errors), errors[:5])
        finally:
            await case.asyncTearDown()

    asyncio.run(_stress())
