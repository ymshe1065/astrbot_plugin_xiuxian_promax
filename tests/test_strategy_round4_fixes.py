# tests/test_strategy_round4_fixes.py
# 「仙域争霸」v4.1.8 第四轮 BUG/冲突修复回归：
#   hex 移动起点兼容 / 巷战守方胜落库 / 离间复核俘虏 / 暴露关押归属 /
#   纵火多人势力 / 粮队归属复查与逐格巡检 / tick 整段事务 /
#   联盟据点交接权限 / 和谈赔款方向 / 联盟名册治理
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


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


def _now() -> int:
    return int(time.time())


H = _imp("core.strategy_hex")
R = _imp("core.strategy_rules")


def mk_army(i, fid, tt="sword_shield", troops=1000, prof="sword", **kw):
    return {"id": i, "faction_id": fid, "troop_type": tt, "troops": troops,
            "morale": 100, "veteran": 0, "siege_engine": False,
            "leader": {"id": i, "name": f"将{i}", "role": "general",
                       "profession": prof, "rarity": "rare",
                       "stats": {"leadership": 70, "valor": 80, "wisdom": 70,
                                 "admin": 50, "charm": 50, "luck": 50},
                       "injury": 0, "traits": [], "aptitude": {}, "bonds": {}},
            **kw}


class TestStrategyRound4Fixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        sm_mod = _imp("managers.strategy_manager")
        self.StrategyManager = sm_mod.StrategyManager
        self.StrategyError = sm_mod.StrategyError
        self.sm_mod = sm_mod

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        for uid in ("u1", "u2", "u3"):
            await self.db.create_player(self.Player(
                user_id=uid, gold=10_000_000, user_name=f"测试{uid}"))

        self.m = self.StrategyManager(self.db)
        # 野战遭遇默认改为手操窗口；常规数值回归按自动结算口径跑
        self.m.main_cfg().setdefault("hex", {})["field_encounter_mode"] = "auto"
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        self.p1 = await self._enter("u1", "甲")
        self.p2 = await self._enter("u2", "乙")
        self.p3 = await self._enter("u3", "丙")
        for uid in ("u1", "u2", "u3"):
            await self._give(uid, coin=500_000, grain=50_000, iron=5_000)

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    async def _give(self, uid: str, **res):
        p = await self.m.get_player(uid, self.wid)
        for k, v in res.items():
            p[k] = int(v)
        await self.m.save_player(p)
        await self.m._commit()
        return p

    async def _free_node(self) -> int:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        nodes.sort(key=lambda n: (n["type"] not in ("city", "pass", "branch"),
                                  -int(n.get("level", 1)), int(n["id"])))
        self.assertTrue(nodes, "没有可选起始据点")
        return int(nodes[0]["id"])

    async def _enter(self, user_id: str, nickname: str) -> dict:
        return await self.m.enter_game(user_id, nickname,
                                       node_id=await self._free_node())

    async def _owned_node(self, uid: str) -> dict:
        p = await self.m.get_player(uid, self.wid)
        for n in await self.m.get_nodes(self.wid):
            if int(n["owner_faction"]) == int(p["faction_id"]):
                return n
        self.fail(f"{uid} 没有据点")

    async def _create_multi_faction(self) -> int:
        r = await self.m.create_faction("u1", "凌云盟")
        pid = r["petition_id"]
        await self.m.faction_cosign("u2", pid)
        r3 = await self.m.faction_cosign("u3", pid)
        return int(r3["faction_id"])

    # ------------------------------------------------------------------
    # 1. hex 移动：paht 带起点格也必须能走（P0）
    # ------------------------------------------------------------------
    async def test_hex_move_accepts_path_with_start(self):
        w, h = 9, 9
        terrain = {f"{q},{r}": H.T_PLAIN for q in range(w) for r in range(h)}
        za, zb = H.deploy_zones(w, h, 3)
        s = H.new_session({"size": [w, h], "terrain": terrain,
                           "deploy_a": za, "deploy_b": zb}, "field", "round4")
        u = H.add_unit(s, mk_army(1, 1), "a")
        u["pos"] = [3, 4]
        s["phase"] = "battle"
        rng = random.Random(4)
        check = H.check_move_path(s, u["uid"], [[3, 4], [4, 4]])
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["end"], 1)
        mv = H.do_move(s, u["uid"], [[3, 4], [4, 4]], rng)
        self.assertTrue(mv["ok"], mv)
        self.assertEqual(u["pos"], [4, 4])

    # ------------------------------------------------------------------
    # 2. 巷战守方胜：城防/围城状态必须落库（围城不得永久悬挂）
    # ------------------------------------------------------------------
    async def test_siege_breach_defender_win_persists_node(self):
        node = await self._owned_node("u2")
        node["defense"] = 0
        node["siege_state"] = {"attacker_faction": int(self.p1["faction_id"]),
                               "choice": "street_fight", "ticks": 3}
        node["npc_garrison"] = {"troops": 100, "morale": 80}
        await self.m.save_node(node)
        await self.m._commit()
        attacker = {"id": 999, "faction_id": int(self.p1["faction_id"]),
                    "troops": 1000, "characters": []}
        sim = {"troops": 1000, "morale": 80, "veteran": 0, "leader": {},
               "troop_type": "sword_shield"}
        with mock.patch.object(self.m, "_army_sim",
                               mock.AsyncMock(return_value=dict(sim))), \
                mock.patch.object(self.m, "_faction_buff",
                                  mock.AsyncMock(return_value=0.0)), \
                mock.patch.object(self.m, "_garrison_sim",
                                  return_value=None), \
                mock.patch.object(self.m, "get_armies",
                                  mock.AsyncMock(return_value=[])), \
                mock.patch.object(self.m, "_persist_battle",
                                  mock.AsyncMock()), \
                mock.patch.object(self.m, "_apply_battle_aftermath",
                                  mock.AsyncMock()), \
                mock.patch.object(self.sm_mod.R, "simulate_battle",
                                  return_value={"winner": "defender",
                                                "rounds": []}), \
                mock.patch.object(self.sm_mod.R, "render_battle_report",
                                  return_value="巷战"):
            await self.m._siege_breach(self.world, node, [attacker])
        await self.m._commit()
        fresh = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(fresh["siege_state"], {})
        self.assertEqual(int(fresh["defense"]),
                         int(R.city_defense_max(int(fresh["level"])) * 0.2))

    # ------------------------------------------------------------------
    # 3. 离间复核目标状态：俘虏不得被“离间越狱”
    # ------------------------------------------------------------------
    async def test_alienate_skips_captive_target(self):
        target_fid = int(self.p2["faction_id"])
        chars = await self.m.get_chars(owner_user_id="u2", world_id=self.wid)
        self.assertTrue(chars)
        tc = chars[0]
        tc["status"] = "captive"
        tc["captor_faction"] = int(self.p1["faction_id"])
        tc["imprisoned_node_id"] = int((await self._owned_node("u1"))["id"])
        tc["loyalty"] = 70
        await self.m.save_char(tc)
        ex = (await self.m.get_chars(owner_user_id="u1", world_id=self.wid))[0]
        task = {"id": 991, "task_type": "plot_alienate",
                "faction_id": int(self.p1["faction_id"]),
                "target_faction": target_fid,
                "payout": {"plot": "alienate", "target": int(tc["id"])}}
        with mock.patch.object(self.sm_mod.R, "plot_success_rate",
                               return_value=100.0):
            out = await self.m._settle_plot_task(self.world, task, ex, None)
        fresh = await self.m.get_char(int(tc["id"]))
        self.assertEqual(fresh["status"], "captive")
        self.assertEqual(int(fresh["captor_faction"]),
                         int(self.p1["faction_id"]))
        self.assertEqual(int(fresh["loyalty"]), 70)
        self.assertTrue(any("落空" in t for t in out["texts"]), out["texts"])

    # ------------------------------------------------------------------
    # 4. 计略暴露：执行者关进目标势力据点，而不是自己城里
    # ------------------------------------------------------------------
    async def test_expose_executor_jailed_in_target_node(self):
        enemy_node = await self._owned_node("u2")
        own_node = await self._owned_node("u1")
        ex = (await self.m.get_chars(owner_user_id="u1", world_id=self.wid))[0]
        ex["status"] = "idle"
        ex["location"] = {"node_id": int(own_node["id"])}
        await self.m.save_char(ex)
        f = await self.m.get_faction(int(self.p1["faction_id"]))
        got = await self.m._expose_executor(
            self.world, ex, f, int(self.p2["faction_id"]))
        self.assertTrue(got)
        fresh = await self.m.get_char(int(ex["id"]))
        self.assertEqual(fresh["status"], "captive")
        self.assertEqual(int(fresh["captor_faction"]),
                         int(self.p2["faction_id"]))
        self.assertEqual(int(fresh["imprisoned_node_id"]),
                         int(enemy_node["id"]))
        self.assertNotEqual(int(fresh["imprisoned_node_id"]),
                            int(own_node["id"]))

    # ------------------------------------------------------------------
    # 5. 纵火针对多人势力：必须烧到所有成员
    # ------------------------------------------------------------------
    async def test_burn_owner_grain_hits_every_member(self):
        fid = int(self.p1["faction_id"])
        p2 = await self.m.get_player("u2", self.wid)
        p2["faction_id"] = fid
        p2["grain"] = 1000
        await self.m.save_player(p2)
        p1 = await self.m.get_player("u1", self.wid)
        p1["grain"] = 1000
        await self.m.save_player(p1)
        node = await self._owned_node("u1")
        loss = await self.m._burn_owner_grain(self.world, node, 0.5)
        self.assertEqual(loss, 1000)
        self.assertEqual(int((await self.m.get_player("u1", self.wid))["grain"]), 500)
        self.assertEqual(int((await self.m.get_player("u2", self.wid))["grain"]), 500)

    # ------------------------------------------------------------------
    # 6. 粮队抵达复查归属：目的地被敌方夺取不得充敌粮
    # ------------------------------------------------------------------
    async def test_convoy_destination_lost_returns_grain(self):
        src = await self._owned_node("u1")
        dst = await self._owned_node("u2")
        src_stock = dict(src.get("stock") or {})
        src_stock["grain"] = 0
        src["stock"] = src_stock
        await self.m.save_node(src)
        self.world = await self.m.get_world()
        data = self.world.get("data") or {}
        data["convoys"] = [{
            "id": "cv_r4_lost", "owner": "u1",
            "faction": int(self.p1["faction_id"]),
            "from": int(src["id"]), "to": int(dst["id"]),
            "path": [int(src["id"]), int(dst["id"])], "idx": 0,
            "grain": 1000, "escort_troops": 0, "escort_army_id": 0,
            "created_tick": 1}]
        self.world["data"] = data
        await self.m.save_world(self.world)
        await self.m._commit()
        with mock.patch.object(self.m, "_notify", mock.AsyncMock()):
            await self.m._step_convoys(self.world)
        fresh_src = await self.m.get_node(self.wid, int(src["id"]))
        fresh_dst = await self.m.get_node(self.wid, int(dst["id"]))
        self.assertEqual(int((fresh_src.get("stock") or {}).get("grain", 0)), 1000)
        self.assertEqual(int((fresh_dst.get("stock") or {}).get("grain", 0) or 0),
                         int((dst.get("stock") or {}).get("grain", 0) or 0))
        left = (await self.m.get_world()).get("data", {}).get("convoys") or []
        self.assertFalse(any(c.get("id") == "cv_r4_lost" for c in left))

    # ------------------------------------------------------------------
    # 7. 粮队提速逐格巡检：中间敌境节点仍会被劫
    # ------------------------------------------------------------------
    async def test_convoy_speed_checks_intermediate_nodes(self):
        nodes = await self.m.get_nodes(self.wid)
        own = await self._owned_node("u1")
        # 找一条 4 节点链：自身据点 + 3 个任意相邻节点
        path = [int(own["id"])]
        cur = own
        by_id = {int(n["id"]): n for n in nodes}
        for _ in range(3):
            nxt = next((int(x) for x in (cur.get("adj") or [])
                        if int(x) not in path and int(x) in by_id), None)
            if nxt is None:
                self.skipTest("地图没有足够的相邻节点")
            path.append(nxt)
            cur = by_id[nxt]
        cfg = self.m.main_cfg()
        cfg.setdefault("convoy", {})["speed_edges"] = 2
        self.world = await self.m.get_world()
        data = self.world.get("data") or {}
        data["convoys"] = [{
            "id": "cv_r4_raid", "owner": "u1",
            "faction": int(self.p1["faction_id"]),
            "from": path[0], "to": path[-1], "path": path, "idx": 0,
            "grain": 1000, "escort_troops": 0, "escort_army_id": 0,
            "created_tick": 1}]
        self.world["data"] = data
        await self.m.save_world(self.world)
        await self.m._commit()
        mid = int(path[1])
        raider = {"kind": "army", "troops": 500, "faction": 0, "owner": "",
                  "node_id": mid}

        async def fake_raider(world, node_id, fid, fn):
            return dict(raider) if int(node_id) == mid else None

        with mock.patch.object(self.m, "_find_convoy_raider", fake_raider), \
                mock.patch.object(self.sm_mod.R, "convoy_raid_success",
                                  return_value=True), \
                mock.patch.object(self.m, "_give_raid_spoils",
                                  mock.AsyncMock()) as spoils, \
                mock.patch.object(self.m, "_notify", mock.AsyncMock()):
            await self.m._step_convoys(self.world)
        left = (await self.m.get_world()).get("data", {}).get("convoys") or []
        cv = next(c for c in left if c.get("id") == "cv_r4_raid")
        self.assertEqual(int(cv["idx"]), 2)
        self.assertEqual(int(cv["raided_at"]), mid)
        self.assertLess(int(cv["grain"]), 1000)
        spoils.assert_awaited()

    # ------------------------------------------------------------------
    # 8. tick 整段事务：中途异常必须完整回滚（不再半提交/时钟先落盘）
    # ------------------------------------------------------------------
    async def test_tick_failure_rolls_back_whole_tick(self):
        before = await self.m.get_world()
        with mock.patch.object(self.m, "_step_distribute_output",
                               mock.AsyncMock(side_effect=RuntimeError("boom"))):
            summary = await self.m._tick_locked(False, {})
        self.assertFalse(summary["ok"])
        after = await self.m.get_world()
        self.assertEqual(int(after["tick_no"]), int(before["tick_no"]))
        self.assertEqual(int(after["last_tick_at"]), int(before["last_tick_at"]))
        # 事务状态已复位，下一 tick 能正常提交
        nxt = await self.m.tick()
        self.assertTrue(nxt["ok"], nxt)

    # ------------------------------------------------------------------
    # 9. 联盟据点交接：成员国主不能把盟主据点划给自己
    # ------------------------------------------------------------------
    async def test_transfer_node_member_cannot_take_host_node(self):
        host_fid = await self._create_multi_faction()
        host_node = await self._owned_node("u1")
        own_fid = int((await self.m.get_player("u2", self.wid))["faction_id"])
        with self.assertRaises(self.StrategyError):
            await self.m.transfer_node("u2", int(host_node["id"]), own_fid)
        fresh = await self.m.get_node(self.wid, int(host_node["id"]))
        self.assertEqual(int(fresh["owner_faction"]), int(host_fid))

    # ------------------------------------------------------------------
    # 10. 和谈赔款方向：提案方付款给接受方
    # ------------------------------------------------------------------
    async def test_peace_indemnity_paid_by_proposer(self):
        fid1 = int(self.p1["faction_id"])
        fid2 = int(self.p2["faction_id"])
        await self.m.diplomacy_propose("u1", fid2, "war")
        f1 = await self.m.get_faction(fid1)
        f2 = await self.m.get_faction(fid2)
        f1["treasury"] = {"coin": 10_000}
        f2["treasury"] = {"coin": 10_000}
        await self.m.save_faction(f1)
        await self.m.save_faction(f2)
        await self.m._commit()
        await self.m.diplomacy_propose("u1", fid2, "peace", indemnity=1000)
        await self.m.diplomacy_respond("u2", fid1, True)
        f1 = await self.m.get_faction(fid1)
        f2 = await self.m.get_faction(fid2)
        self.assertEqual(int((f1.get("treasury") or {}).get("coin", 0)), 9_000)
        self.assertEqual(int((f2.get("treasury") or {}).get("coin", 0)), 11_000)

    # ------------------------------------------------------------------
    # 11. 治理/崩溃只用本势力名册：盟主不再因盟友活跃而免于僵尸化
    # ------------------------------------------------------------------
    async def test_governance_uses_own_roster_not_alliance(self):
        host_fid = await self._create_multi_faction()
        own2 = int((await self.m.get_player("u2", self.wid))["faction_id"])
        old = _now() - 40 * 86400
        p1 = await self.m.get_player("u1", self.wid)
        p1["last_seen_at"] = old
        await self.m.save_player(p1)
        for uid in ("u2", "u3"):
            pp = await self.m.get_player(uid, self.wid)
            pp["last_seen_at"] = _now()
            await self.m.save_player(pp)
        await self.m._commit()
        await self.m._step_governance(self.world)
        host = await self.m.get_faction(host_fid)
        self.assertEqual(int(host.get("alive", 0)), 0)
        p2 = await self.m.get_player("u2", self.wid)
        self.assertEqual(int(p2["faction_id"]), own2,
                         "盟友被错误迁出本势力")
        members = await self.m.get_members(own2)
        self.assertTrue(any(m["user_id"] == "u2" for m in members))
