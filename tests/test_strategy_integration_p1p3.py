# tests/test_strategy_integration_p1p3.py
# 「仙域争霸」P1~P3 集成测试：
# 势力会签/入退盟 / 外交 / 俘虏流转 / 计略派遣结算 / hex 手操战役（托管+超时）/
# 互市挂单买卖 / 匪寨讨伐 / 商路运粮 / 赛季结算结转 / 追赶礼包 / 擂台会武 / 成就 / 面板视图
import hashlib
import json
import os
import random
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


def _now() -> int:
    return int(time.time())


class TestStrategyP1P3Integration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        sm_mod = _imp("managers.strategy_manager")
        self.StrategyManager = sm_mod.StrategyManager
        self.StrategyError = sm_mod.StrategyError
        self.R = _imp("core.strategy_rules")
        self.H = _imp("core.strategy_hex")

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        for uid in ("u1", "u2", "u3", "u4"):
            await self.db.create_player(self.Player(
                user_id=uid, gold=10_000_000, user_name=f"测试{uid}"))

        self.m = self.StrategyManager(self.db)
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

    async def _enter(self, user_id: str = "u1", nickname: str = "甲",
                     referral: str = "") -> dict:
        return await self.m.enter_game(user_id, nickname, referral=referral,
                                       node_id=await self._free_node())

    async def _char_of(self, uid: str, role: str = "general"):
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == role:
                return c
        return None

    async def _recruit_role(self, uid: str, role: str):
        """确定性招募指定 role：直接向酒馆注入目标候选再招募。

        不依赖刷新随机——_roll_tavern 的种子含 now//3600，同一小时内刷新
        结果完全相同，靠反复刷新找 role 会随小时段偶发失败。
        """
        rng = random.Random(int(hashlib.md5(
            f"{uid}|{role}|inject".encode("utf-8")).hexdigest()[:8], 16))
        c = self.R.generate_character(
            rng, rarity="fine", role=role,
            name_pools=self.m.cfg.get("names") or {})
        cand = {k: c[k] for k in ("name", "role", "rarity", "profession",
                                  "stats", "traits", "aptitude")}
        p = await self.m.get_player(uid, self.wid)
        tavern = p.get("tavern") or {}
        tavern["candidates"] = [cand]
        # 必须同时把 refresh_at 推到未来，否则 recruit→_tavern 会重掷覆盖注入候选
        tavern["refresh_at"] = int(time.time()) + 4 * 3600
        p["tavern"] = tavern
        await self.m.save_player(p)
        await self.m._commit()
        p = await self.m.get_player(uid, self.wid)
        return await self.m.recruit(p, self.wid, 0)

    async def _mk_army(self, uid: str, node_id: int, troops: int = 800,
                       role: str = "general"):
        await self._give(uid, coin=5_000_000, grain=5_000_000)
        c = await self._char_of(uid, role)
        r = await self.m.create_army(uid, c["id"], [], node_id,
                                     "sword_shield", troops)
        return await self.m.get_army(r["army_id"])

    async def _own(self, uid: str, node: dict) -> dict:
        p = await self.m.get_player(uid, self.wid)
        node["owner_faction"] = int(p["faction_id"])
        await self.m.save_node(node)
        return node

    async def _create_multi_faction(self) -> int:
        """u1/u2/u3 会签建多人势力，返回 faction_id。"""
        r = await self.m.create_faction("u1", "凌云盟")
        pid = r["petition_id"]
        await self.m.faction_cosign("u2", pid)
        r3 = await self.m.faction_cosign("u3", pid)
        return int(r3["faction_id"])

    # ------------------------------------------------------------------
    # P1：势力
    # ------------------------------------------------------------------
    async def test_faction_create_cosign_apply_quit(self):
        # 本用例需容纳第 4 个势力验证申请/审批/退盟链路
        self.m.main_cfg().setdefault("alliance", {})["max_factions"] = 4
        fid = await self._create_multi_faction()
        f = await self.m.get_faction(fid)
        self.assertEqual(f["name"], "凌云盟")
        self.assertEqual(f["kind"], "player")
        self.assertEqual(int((f.get("treasury") or {}).get("coin", 0)), 10000)
        members = await self.m.get_members(fid)
        self.assertEqual(len(members), 3)
        pos = {m["user_id"]: m.get("position", "") for m in members}
        self.assertEqual(pos["u1"], "leader")
        for uid in ("u1", "u2", "u3"):
            p = await self.m.get_player(uid, self.wid)
            if uid == "u1":
                self.assertEqual(int(p["faction_id"]), fid)
            else:
                self.assertNotEqual(int(p["faction_id"]), fid)
            mf = await self.m.get_faction(int(p["faction_id"]))
            self.assertEqual(int(mf["alliance_id"]), fid)
        # 联盟语义：会签后各势力保留自身势力与据点，只把治理权交给盟主
        host = await self.m.get_faction(fid)
        self.assertEqual(host["name"], "凌云盟")
        self.assertEqual(int(host["alliance_id"]), fid)
        self.assertGreater(int(host.get("capital_node_id") or 0), 0)
        for uid in ("u2", "u3"):
            p = await self.m.get_player(uid, self.wid)
            self.assertNotEqual(int(p["faction_id"]), fid)
            mf = await self.m.get_faction(int(p["faction_id"]))
            self.assertEqual(mf["kind"], "player")
            self.assertEqual(int(mf["alliance_id"]), fid)
            # 保留自己的起始据点，不归还无主
            home = await self.m.get_node(self.wid, int(mf["capital_node_id"]))
            self.assertEqual(int(home["owner_faction"]), int(mf["id"]))
            self.assertTrue((p.get("tutorial") or {}).get("home_granted"))
        pos = {m["user_id"]: m.get("position", "") for m in members}
        self.assertEqual(pos["u2"], "deputy")
        self.assertEqual(pos["u3"], "deputy")
        # u4 申请结盟 → u1 审批：保留自身势力与据点
        await self._enter("u4", "丁")
        p4 = await self.m.get_player("u4", self.wid)
        u4_fid = int(p4["faction_id"])
        self.assertTrue((p4.get("tutorial") or {}).get("home_granted"))
        await self.m.faction_apply("u4", fid)
        await self.m.faction_approve("u1", "u4")
        p4 = await self.m.get_player("u4", self.wid)
        self.assertEqual(int(p4["faction_id"]), u4_fid)
        f4 = await self.m.get_faction(u4_fid)
        self.assertEqual(int(f4["alliance_id"]), fid)
        self.assertEqual(len(await self.m.get_members(fid)), 4)
        self.assertTrue((p4.get("tutorial") or {}).get("home_granted"))
        # u4 退出联盟：势力与据点全部保留，不重建空壳
        await self.m.faction_quit("u4")
        p4 = await self.m.get_player("u4", self.wid)
        self.assertEqual(int(p4["faction_id"]), u4_fid)
        f4 = await self.m.get_faction(u4_fid)
        self.assertEqual(int(f4["alliance_id"]), 0)
        self.assertEqual(len(await self.m.get_members(u4_fid)), 1)
        self.assertTrue((p4.get("tutorial") or {}).get("home_granted"))
        self.assertGreater(int(f4.get("capital_node_id") or 0), 0)
        home = await self.m.get_node(self.wid, int(f4["capital_node_id"]))
        self.assertEqual(int(home["owner_faction"]), u4_fid)
        self.assertEqual(len(await self.m.get_members(fid)), 3)

    async def test_treasury_deposit_withdraw_and_position(self):
        fid = await self._create_multi_faction()
        await self._give("u1", coin=50_000)
        await self.m.treasury_deposit("u1", "coin", 20_000)
        f = await self.m.get_faction(fid)
        self.assertEqual(int(f["treasury"]["coin"]), 30_000)  # 10000 会签 + 20000
        # 共同国库：成员势力主为副主，单次提取上限为国库 30%
        with self.assertRaises(self.StrategyError):
            await self.m.treasury_withdraw("u2", "coin", 20_000)
        await self.m.treasury_withdraw("u2", "coin", 5_000)
        f = await self.m.get_faction(fid)
        self.assertEqual(int(f["treasury"]["coin"]), 25_000)
        # 官职任免（联盟名册）
        await self.m.faction_set_position("u1", "u2", "situ")
        members = {m["user_id"]: m.get("position", "")
                   for m in await self.m.get_members(fid)}
        self.assertEqual(members["u2"], "situ")
        # 盟主之位只能在自身势力内移交，联盟成员势力需各自移交
        with self.assertRaises(self.StrategyError):
            await self.m.transfer_leader("u1", "u2")
        f = await self.m.get_faction(fid)
        self.assertEqual(f["leader_id"], "u1")

    async def test_alliance_transfer_node_and_quit_split(self):
        """联盟内据点可交接；退盟保留据点，并按据点占比分割盟主国库。"""
        fid = await self._create_multi_faction()
        host = await self.m.get_faction(fid)
        host["treasury"] = {"coin": 9000}
        await self.m.save_faction(host)
        await self.m._commit()
        p2 = await self.m.get_player("u2", self.wid)
        u2_fid = int(p2["faction_id"])
        # 给 u1 增派一座据点，交接后仍保留主城
        extra = next(n for n in await self.m.get_nodes(self.wid)
                     if int(n["owner_faction"]) == 0 and n["type"] != "ruin")
        await self._own("u1", extra)
        await self.m._commit()
        u1_node = next(n for n in await self.m.get_nodes(self.wid)
                       if int(n["owner_faction"]) == fid and
                       int(n["id"]) != int(host["capital_node_id"]))
        r = await self.m.transfer_node("u1", int(u1_node["id"]), u2_fid)
        self.assertTrue(r["ok"])
        moved = await self.m.get_node(self.wid, int(u1_node["id"]))
        self.assertEqual(int(moved["owner_faction"]), u2_fid)
        # 不能把据点交接给联盟外势力
        npc = await self._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "AND alive=1 LIMIT 1", (self.wid,))
        with self.assertRaises(self.StrategyError):
            await self.m.transfer_node("u1", int(u1_node["id"]), int(npc[0]["id"]))
        # u2 退盟：据点保留，按据点占比拿回盟主国库份额
        counts = {}
        for n in await self.m.get_nodes(self.wid):
            counts[int(n["owner_faction"])] = counts.get(int(n["owner_faction"]), 0) + 1
        total = sum(counts[x] for x in (fid, u2_fid, int(self.p3["faction_id"])))
        expect = int(9000 * counts[u2_fid] / total)
        u2_nodes_before = counts[u2_fid]
        await self.m.faction_quit("u2")
        p2 = await self.m.get_player("u2", self.wid)
        self.assertEqual(int(p2["faction_id"]), u2_fid)
        f2 = await self.m.get_faction(u2_fid)
        self.assertEqual(int(f2["alliance_id"]), 0)
        self.assertEqual(int(f2["treasury"]["coin"]), expect)
        host = await self.m.get_faction(fid)
        self.assertEqual(int(host["treasury"]["coin"]), 9000 - expect)
        still = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == u2_fid]
        self.assertEqual(len(still), u2_nodes_before)
        roster = {m["user_id"] for m in await self.m.get_members(fid)}
        self.assertNotIn("u2", roster)

    async def test_alliance_invite_respond_and_capacity(self):
        """邀请结盟：仅势力主可发/可应；受联盟势力数上限约束。"""
        self.m.main_cfg().setdefault("alliance", {})["max_factions"] = 2
        u1_fid = int(self.p1["faction_id"])
        u2_fid = int(self.p2["faction_id"])
        u3_fid = int(self.p3["faction_id"])
        # 非势力主不能发出邀请（u2 势力只有 u2 一人 → 先让 u1 势力多一人不行，改用 u1 邀请自己）
        with self.assertRaises(self.StrategyError):
            await self.m.faction_invite("u1", "u1")
        # u1 邀请 u2 → u2 接受
        r = await self.m.faction_invite("u1", "u2")
        self.assertTrue(r["ok"])
        world = await self.m.get_world()
        self.assertIn("u2", (world.get("data") or {}).get("join_invites") or {})
        # 非势力主不能接受（u3 无邀请）
        with self.assertRaises(self.StrategyError):
            await self.m.faction_respond_invite("u3", True)
        r = await self.m.faction_respond_invite("u2", True)
        self.assertTrue(r["ok"])
        f1 = await self.m.get_faction(u1_fid)
        f2 = await self.m.get_faction(u2_fid)
        self.assertEqual(int(f1["alliance_id"]), u1_fid)
        self.assertEqual(int(f2["alliance_id"]), u1_fid)
        self.assertEqual(len(await self.m.get_members(u1_fid)), 2)
        # 邀请时已满（2 个势力）→ 直接拒绝邀请
        with self.assertRaises(self.StrategyError):
            await self.m.faction_invite("u1", "u3")
        # 先放宽上限发出邀请，再收紧后接受 → 接受时按容量拒绝
        self.m.main_cfg()["alliance"]["max_factions"] = 3
        await self.m.faction_invite("u1", "u3")
        self.m.main_cfg()["alliance"]["max_factions"] = 2
        with self.assertRaises(self.StrategyError):
            await self.m.faction_respond_invite("u3", True)
        f3 = await self.m.get_faction(u3_fid)
        self.assertEqual(int(f3["alliance_id"]), 0)

    async def test_diplomacy_war_truce_violation(self):
        fid = await self._create_multi_faction()
        npc = await self._fetchall("SELECT * FROM strategy_factions "
                                   "WHERE world_id=? AND kind='npc' AND alive=1 "
                                   "LIMIT 1", (self.wid,))
        npc_id = int(npc[0]["id"])
        # 宣战 → war_pending（备战期）
        r = await self.m.diplomacy_propose("u1", npc_id, "war")
        self.assertTrue(r["ok"])
        f = await self.m.get_faction(fid)
        rel = self.R.relation_of(f.get("diplomacy") or {}, npc_id, _now())
        self.assertEqual(rel, "war_pending")
        # 撕毁判定：war_pending 期间进攻不触发撕毁惩罚（尚未生效）
        rep0 = int(f["reputation"])
        world = await self.m.get_world()
        await self.m.check_truce_violation(world, fid, npc_id)
        f = await self.m.get_faction(fid)
        self.assertEqual(int(f["reputation"]), rep0)
        # 停战提案（NPC 掷点，接受或拒绝都不应抛错）
        await self.m._execute(
            "UPDATE strategy_factions SET diplomacy_json=? WHERE id=?",
            ('{}', fid))
        f = await self.m.get_faction(fid)
        self.m._set_relation(f.setdefault("diplomacy", {}), npc_id,
                             self.R.REL_TRUCE, until=_now() + 48 * 3600)
        await self.m.save_faction(f)
        # 停战期进攻 → 撕毁：声望 -15，关系转 war
        rep0 = int(f["reputation"])
        broken = await self.m.check_truce_violation(world, fid, npc_id)
        self.assertTrue(broken)
        f = await self.m.get_faction(fid)
        self.assertEqual(int(f["reputation"]), rep0 - 15)
        self.assertEqual(self.R.relation_of(f["diplomacy"], npc_id, _now()),
                         self.R.REL_WAR)

    # ------------------------------------------------------------------
    # P1：俘虏
    # ------------------------------------------------------------------
    async def test_captive_capture_ransom_release_persuade(self):
        fid1 = int(self.p1["faction_id"])
        gen2 = await self._char_of("u2", "general")
        nodes = await self.m.get_nodes(self.wid)
        node = nodes[0]
        # 直接俘虏 u2 的武将到 u1 势力
        await self.m._capture_char(gen2, fid1, node["id"])
        c = await self.m.get_char(gen2["id"])
        self.assertEqual(c["status"], "captive")
        self.assertEqual(int(c["captor_faction"]), fid1)
        self.assertEqual((c.get("location") or {}).get("home_owner"), "u2")
        # 双方视图
        v1 = await self.m.get_captives_view(self.wid, fid1)
        self.assertIn(gen2["name"], [x["name"] for x in v1["holding"]])
        v2 = await self.m.get_captives_view(self.wid, int(self.p2["faction_id"]))
        self.assertIn(gen2["name"], [x["name"] for x in v2["held"]])
        # 赎金：u1 定价 500 → u2 支付 → 人回家，赎金入 u1 势力国库
        f1 = await self.m.get_faction(fid1)
        treasury0 = int((f1.get("treasury") or {}).get("coin", 0))
        await self.m.set_ransom("u1", gen2["id"], 500)
        await self._give("u2", coin=5_000)
        r = await self.m.pay_ransom("u2", gen2["id"])
        self.assertTrue(r["ok"])
        c = await self.m.get_char(gen2["id"])
        self.assertEqual(c["status"], "idle")
        self.assertEqual(c["owner_user_id"], "u2")
        f1 = await self.m.get_faction(fid1)
        treasury1 = int((f1.get("treasury") or {}).get("coin", 0))
        self.assertEqual(treasury1 - treasury0, 500)
        # 再俘 → 无条件释放
        await self.m._capture_char(c, fid1, node["id"])
        await self.m.release_captive("u1", gen2["id"])
        c = await self.m.get_char(gen2["id"])
        self.assertEqual(c["status"], "idle")
        # 劝降（结果随机，只验证流程与返回契约）
        await self.m._capture_char(c, fid1, node["id"])
        r = await self.m.persuade_captive("u1", gen2["id"])
        self.assertTrue(r["ok"])
        self.assertIn(r["result"], ("success", "fail"))
        # 解放：据点易主时俘虏解放
        c = await self.m.get_char(gen2["id"])
        if c["status"] == "captive":
            world = await self.m.get_world()
            freed = await self.m.liberate_captives_at_node(world, node, 999999)
            self.assertIn(gen2["name"], freed)
            c = await self.m.get_char(gen2["id"])
            self.assertNotEqual(c["status"], "captive")

    # ------------------------------------------------------------------
    # P1：计略
    # ------------------------------------------------------------------
    async def test_plot_dispatch_and_settle(self):
        strat = await self._recruit_role("u1", self.R.ROLE_STRATEGIST)
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        enemy_node = next(n for n in nodes.values()
                          if n["owner_faction"] > 0
                          and n["owner_faction"] != int(self.p1["faction_id"]))
        morale0 = int(enemy_node["morale"])
        r = await self.m.dispatch_plot("u1", strat["id"], "rumor", enemy_node["id"])
        self.assertTrue(r["ok"])
        task = (await self.m.get_tasks(self.wid))[-1]
        self.assertEqual(task["task_type"], "plot_rumor")
        c = await self.m.get_char(strat["id"])
        self.assertEqual(c["status"], "task")
        # 冷却：同目标重复施展被拒
        strat2 = await self._recruit_role("u1", self.R.ROLE_STRATEGIST)
        with self.assertRaises(self.StrategyError):
            await self.m.dispatch_plot("u1", strat2["id"], "rumor", enemy_node["id"])
        # 到期结算
        await self.m._execute("UPDATE strategy_tasks SET end_at=? WHERE id=?",
                              (_now() - 10, task["id"]))
        world = await self.m.get_world()
        await self.m._step_settle_tasks(world)
        rows = await self._fetchall("SELECT status FROM strategy_tasks WHERE id=?",
                                    (task["id"],))
        self.assertEqual(rows[0]["status"], "done")
        c = await self.m.get_char(strat["id"])
        # 结算后：未败露 → idle；败露被擒（§6.4）→ captive（_step_settle_tasks 不重置）
        self.assertIn(c["status"], ("idle", "captive"))
        node = await self.m.get_node(self.wid, enemy_node["id"])
        # 谣言成功则士气下降；失败则不变（不强制断言胜负）
        self.assertLessEqual(int(node["morale"]), morale0)

    # ------------------------------------------------------------------
    # P2：hex 手操战役
    # ------------------------------------------------------------------
    async def _setup_manual_battle(self, troops: int = 1200):
        # 强制布局：任取一对相邻节点，home 归 u1、target 强制由 NPC 接管，
        # 避免世界随机生成（种子含启动秒数）导致 next() 抛 StopIteration 的 flake。
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        home, adj_id = next((n, x) for n in nodes.values() for x in n["adj"])
        await self._own("u1", home)
        npc = await self.m._fetchone(
            "SELECT id FROM strategy_factions"
            " WHERE world_id=? AND kind='npc' LIMIT 1", (self.wid,))
        target = nodes[adj_id]
        target["owner_faction"] = int(npc["id"])
        target["npc_garrison"] = {"troops": 600, "morale": 70}
        await self.m.save_node(target)
        army = await self._mk_army("u1", home["id"], troops)
        world = await self.m.get_world()
        atk_sim = await self.m._army_sim(army)
        gar_sim = self.m._garrison_sim(target)
        battle = await self.m._create_manual_battle(
            world, target, [atk_sim], [gar_sim] if gar_sim else [],
            [army], [], "field", "field", 300, 0, "u1")
        return world, target, army, battle

    async def test_manual_battle_deploy_start_delegate_finish(self):
        world, node, army, battle = await self._setup_manual_battle()
        bid = battle["id"]
        st = await self.m.battle_state("u1", bid)
        self.assertEqual(st["phase"], "deploy")
        # 部署位置非法 → 报错
        with self.assertRaises(self.StrategyError):
            await self.m.battle_deploy("u1", bid,
                                       [{"uid": 1, "q": -99, "r": -99}])
        await self.m.battle_auto_deploy("u1", bid)
        r = await self.m.battle_start("u1", bid)
        self.assertTrue(r["ok"])
        b = await self.m.get_battle(bid)
        self.assertEqual(b["state"], "battle")
        # 开战后未部署完成的另一战役不能重复 start
        with self.assertRaises(self.StrategyError):
            await self.m.battle_start("u1", bid)
        # 托管 → tick 推进至终局
        await self.m.battle_delegate("u1", bid, True)
        b = await self.m.get_battle(bid)
        sess = b["session"]
        sess["turn_deadline"] = 0
        b["deadline_at"] = 0
        await self.m.update_battle_session(b)
        await self.m._commit()
        for _ in range(5):
            world = await self.m.get_world()
            await self.m._step_manual_battles(world)
            b = await self.m.get_battle(bid)
            if b["state"] == "done":
                break
            sess = b["session"]
            sess["turn_deadline"] = 0
            b["deadline_at"] = 0
            await self.m.update_battle_session(b)
            await self.m._commit()
        self.assertEqual(b["state"], "done")
        self.assertIn((b.get("result") or {}).get("winner"),
                      ("attacker", "defender"))
        self.assertTrue(b["report_text"])
        # 军团脱离 battle 态
        a = await self.m.get_army(army["id"])
        self.assertNotEqual(a["state"], "battle")

    async def test_manual_battle_deploy_timeout_auto(self):
        world, node, army, battle = await self._setup_manual_battle()
        bid = battle["id"]
        # 部署超时：deadline 置过去 → 全托管瞬时模拟
        b = await self.m.get_battle(bid)
        b["deadline_at"] = _now() - 10
        await self.m.update_battle_session(b)
        await self.m._commit()
        world = await self.m.get_world()
        await self.m._step_manual_battles(world)
        b = await self.m.get_battle(bid)
        self.assertEqual(b["state"], "done")
        self.assertIn((b.get("result") or {}).get("winner"),
                      ("attacker", "defender"))
        # 回放可用
        rep = await self.m.get_battle_replay("u1", bid)
        self.assertTrue(rep)

    # ------------------------------------------------------------------
    # P2：擂台 / 会武
    # ------------------------------------------------------------------
    async def test_duel_arena_and_tournament(self):
        gen1 = await self._char_of("u1", "general")
        gen2 = await self._char_of("u2", "general")
        # 单挑资格：武勇 ≥75
        for g in (gen1, gen2):
            g.setdefault("stats", {})["valor"] = 90
            g["injury"] = 0
            await self.m.save_char(g)
        await self._give("u1", coin=10_000)
        await self._give("u2", coin=10_000)
        r = await self.m.duel_arena_open("u1", gen1["id"], 500)
        duel_id = r["duel_id"]
        duels = await self.m.duel_arena_list("u2")
        self.assertTrue(any(d["duel_id"] == duel_id for d in duels))
        # 会武事件开启：胜场应被计数
        world = await self.m.get_world()
        data = world.get("data") or {}
        data["tournament_until_tick"] = int(world["tick_no"]) + 10
        data["tournament_wins"] = {}
        world["data"] = data
        await self.m.save_world(world)
        await self.m._commit()
        r2 = await self.m.duel_arena_accept("u2", duel_id, gen2["id"])
        self.assertTrue(r2["ok"])
        world = await self.m.get_world()
        wins = (world.get("data") or {}).get("tournament_wins") or {}
        self.assertEqual(sum(wins.values()), 1)
        # 总有一方赢得彩头（或平局退款）：两人 coin 总和守恒（税无）
        c1 = int((await self.m.get_player("u1", self.wid))["coin"])
        c2 = int((await self.m.get_player("u2", self.wid))["coin"])
        self.assertEqual(c1 + c2, 20_000)
        # 已结束的擂台不能重复应战
        with self.assertRaises(self.StrategyError):
            await self.m.duel_arena_accept("u2", duel_id, gen2["id"])

    # ------------------------------------------------------------------
    # P3：互市
    # ------------------------------------------------------------------
    async def test_market_list_buy_cancel(self):
        nodes = await self.m.get_nodes(self.wid)
        market = next(n for n in nodes if n["type"] == "market")
        await self._own("u1", market)
        await self._give("u1", grain=5_000, coin=1_000)
        await self._give("u2", coin=10_000)
        r = await self.m.market_list("u1", market["id"], "grain", 500, 3)
        oid = r["order_id"]
        # 挂单即托管：粮已扣
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1["grain"]), 4_500)
        # 不能买自己的单
        with self.assertRaises(self.StrategyError):
            await self.m.market_buy("u1", oid, 100)
        # u2 买 200：花 600，税 30，u1 实收 570
        p2_before = await self.m.get_player("u2", self.wid)
        coin2_before = int(p2_before["coin"])
        grain2_before = int(p2_before["grain"])
        r = await self.m.market_buy("u2", oid, 200)
        self.assertTrue(r["ok"])
        p2 = await self.m.get_player("u2", self.wid)
        self.assertEqual(int(p2["grain"]), grain2_before + 200)
        self.assertEqual(coin2_before - int(p2["coin"]), 600)
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1["coin"]), 1_000 + 570)
        # 撤单退回剩余 300
        await self.m.market_cancel("u1", oid)
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1["grain"]), 4_500 + 300)
        orders = await self.m.market_orders("u2")
        self.assertFalse(any(o["order_id"] == oid for o in orders))

    async def test_market_maker_and_expiry(self):
        nodes = await self.m.get_nodes(self.wid)
        market = next(n for n in nodes if n["type"] == "market")
        await self._own("u1", market)
        world = await self.m.get_world()
        interval = int((self.m.main_cfg().get("market") or {}).get(
            "npc_maker_interval_ticks", 8))
        world["tick_no"] = interval
        await self.m.save_world(world)
        await self.m._commit()
        await self.m._step_market_maker(world)
        orders = await self.m.market_orders("u1")
        npc_orders = [o for o in orders if o["npc"]]
        self.assertGreaterEqual(len(npc_orders), 1)
        # 过期清理：把订单 expires_at 置过去 → _step_market 下架
        await self._execute("UPDATE strategy_market_orders SET expires_at=?",
                            (_now() - 10,))
        world = await self.m.get_world()
        await self.m._step_market(world)
        rows = await self._fetchall(
            "SELECT status FROM strategy_market_orders WHERE world_id=?",
            (self.wid,))
        self.assertTrue(all(r["status"] == "expired" for r in rows))

    # ------------------------------------------------------------------
    # P3：匪寨 / 商路 / 粮队
    # ------------------------------------------------------------------
    async def test_bandit_spawn_and_raid(self):
        world = await self.m.get_world()
        await self.m._step_bandits(world)
        view = await self.m.get_bandits_view(world)
        self.assertGreaterEqual(len(view), 2)
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        b = view[0]
        bnode = nodes[b["node_id"]]
        adj_id = next(x for x in bnode["adj"])
        adj = nodes[adj_id]
        await self._own("u1", adj)
        # 弱化匪军保证速胜
        bnode["npc_garrison"] = {"troops": 300, "morale": 60}
        await self.m.save_node(bnode)
        army = await self._mk_army("u1", adj["id"], 1400)
        r = await self.m.raid_bandit("u1", army["id"], bnode["id"])
        self.assertTrue(r["ok"])
        self.assertTrue(r["win"], "1400 打 300 应获胜")
        # 匪寨清除 + 战功入账 + 战报落库
        world = await self.m.get_world()
        self.assertNotIn(str(bnode["id"]), (world["data"] or {}).get("bandits") or {})
        p1 = await self.m.get_player("u1", self.wid)
        self.assertGreaterEqual(int(p1["merit"]), 100)
        rows = await self._fetchall(
            "SELECT kind FROM strategy_battles WHERE world_id=? AND kind='bandit'",
            (self.wid,))
        self.assertGreaterEqual(len(rows), 1)
        # 相邻无匪寨 → 拒绝（先清掉该据点周围其余匪寨，避免随机生成干扰）
        world = await self.m.get_world()
        data = world.get("data") or {}
        bands = data.get("bandits") or {}
        for nid in adj.get("adj") or []:
            bands.pop(str(nid), None)
        data["bandits"] = bands
        world["data"] = data
        await self.m.save_world(world)
        await self.m._commit()
        with self.assertRaises(self.StrategyError):
            await self.m.raid_bandit("u1", army["id"], adj["id"])

    async def test_trade_routes_and_convoy(self):
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        p1 = await self.m.get_player("u1", self.wid)
        fid = int(p1["faction_id"])
        markets = [n for n in nodes.values() if n["type"] == "market"]
        a, b = markets[0], markets[1]
        path = await self.m.find_path(self.wid, a["id"], b["id"])
        self.assertIsNotNone(path)
        for nid in path:
            nodes[nid]["owner_faction"] = fid
            await self.m.save_node(nodes[nid])
        world = await self.m.get_world()
        routes = await self.m._compute_trade_routes(world)
        self.assertGreaterEqual(len(routes), 1)
        ids = await self.m._trade_route_node_ids(world)
        self.assertIn(a["id"], ids)
        # 我方商路（NPC 商路收益入其国库，不计入玩家钱包）
        my_routes = [rt for rt in routes
                     if await self.m._route_owner(world, rt) == fid]
        self.assertGreaterEqual(len(my_routes), 1)
        # 商路收益：单人个人势力 → 玩家钱包
        coin0 = int((await self.m.get_player("u1", self.wid))["coin"])
        await self.m._step_trade_routes(world)
        coin1 = int((await self.m.get_player("u1", self.wid))["coin"])
        per = int((self.m.main_cfg().get("trade_route") or {}).get("coin_per_tick", 20))
        self.assertEqual(coin1 - coin0, per * len(my_routes))
        # 运粮：a → b
        await self._give("u1", grain=5_000)
        r = await self.m.send_convoy("u1", a["id"], b["id"], 800)
        self.assertTrue(r["ok"])
        stock0 = int((await self.m.get_node(self.wid, b["id"]))
                     .get("stock", {}).get("grain", 0))
        for _ in range(len(path) + 2):
            world = await self.m.get_world()
            await self.m._step_convoys(world)
        stock1 = int((await self.m.get_node(self.wid, b["id"]))
                     .get("stock", {}).get("grain", 0))
        self.assertEqual(stock1 - stock0, 800)
        view = await self.m.get_convoys_view("u1")
        self.assertEqual(view, [])

    # ------------------------------------------------------------------
    # P3：赛季 / 结转 / 追赶
    # ------------------------------------------------------------------
    async def test_season_settle_carryover_and_new_world(self):
        fid = int(self.p1["faction_id"])
        nodes = await self.m.get_nodes(self.wid)
        home = next(n for n in nodes if n["owner_faction"] == 0)
        await self._own("u1", home)
        army = await self._mk_army("u1", home["id"], 500)
        army["veteran"] = 400
        await self.m.save_army(army)
        #  forcing 到期
        world = await self.m.get_world()
        world["ends_at"] = _now() - 10
        await self.m.save_world(world)
        await self.m._commit()
        await self.m.tick()
        # 旧世界已结算，新世界 S2 激活
        rows = await self._fetchall(
            "SELECT * FROM strategy_season_results WHERE season_no=1")
        self.assertEqual(len(rows), 1)
        mem = await self._fetchall(
            "SELECT * FROM strategy_memorials WHERE season_no=1")
        self.assertGreaterEqual(len(mem), 1)
        new_world = await self.m.get_world()
        self.assertEqual(int(new_world["season_no"]), 2)
        self.assertEqual(new_world["state"], "active")
        # 结转：u1 在新世界入场后领取
        nw_nodes = await self.m.get_nodes(new_world["id"])
        home2 = next(n for n in nw_nodes
                     if n["owner_faction"] == 0 and n["type"] != "ruin")
        await self.m.enter_game("u1", "甲", node_id=int(home2["id"]))
        p1 = await self.m.get_player("u1", new_world["id"])
        home2["owner_faction"] = int(p1["faction_id"])
        await self.m.save_node(home2)
        await self._give_world("u1", new_world["id"], coin=5_000_000,
                               grain=5_000_000)
        c = await self._char_of_world("u1", new_world["id"])
        r = await self.m.create_army("u1", c["id"], [], home2["id"],
                                     "sword_shield", 500)
        new_army = await self.m.get_army(r["army_id"])
        res = await self.m.claim_carryover("u1", new_army["id"])
        self.assertTrue(res["ok"])
        a = await self.m.get_army(new_army["id"])
        self.assertEqual(int(a["veteran"]), 200)  # min(VETERAN_MAX=999, 400×50%)
        # 重复领取被拒
        with self.assertRaises(self.StrategyError):
            await self.m.claim_carryover("u1", new_army["id"])

    async def _give_world(self, uid: str, wid: int, **res):
        p = await self.m.get_player(uid, wid)
        for k, v in res.items():
            p[k] = int(v)
        await self.m.save_player(p)
        await self.m._commit()

    async def _char_of_world(self, uid: str, wid: int, role: str = "general"):
        for c in await self.m.get_chars(owner_user_id=uid, world_id=wid):
            if c["role"] == role:
                return c
        raise AssertionError("无武将")

    async def test_catchup_pack_and_referral(self):
        # 世界开服 10 天
        world = await self.m.get_world()
        world["started_at"] = _now() - 10 * 86400
        await self.m.save_world(world)
        await self.m._commit()
        await self._enter("u4", "丁", referral="u1")
        merit0 = int((await self.m.get_player("u1", self.wid))["merit"])
        p4 = await self.m.get_player("u4", self.wid)
        coin0 = int(p4["coin"])
        r = await self.m.claim_catchup("u4")
        self.assertTrue(r["ok"])
        p4 = await self.m.get_player("u4", self.wid)
        self.assertGreater(int(p4["coin"]), coin0)
        self.assertTrue(int((p4.get("newcomer") or {}).get(
            "catchup_boost_until", 0)) > _now())
        # 引荐人得战功
        merit1 = int((await self.m.get_player("u1", self.wid))["merit"])
        self.assertEqual(merit1 - merit0, 200)
        # 产出倍率生效
        self.assertGreater(self.m.player_output_mult(p4), 1.0)
        # 重复领取被拒
        with self.assertRaises(self.StrategyError):
            await self.m.claim_catchup("u4")

    # ------------------------------------------------------------------
    # P3：成就 / 事件 / 流将
    # ------------------------------------------------------------------
    async def test_achievement_unlock(self):
        merit0 = int((await self.m.get_player("u1", self.wid))["merit"])
        for i in range(9):
            unlocked = await self.m._award_achievement("u1", "conquest_bandit_10")
            self.assertFalse(unlocked)
        unlocked = await self.m._award_achievement("u1", "conquest_bandit_10")
        self.assertTrue(unlocked)
        cfg_merit = int((self.m.main_cfg().get("achievements") or {}).get(
            "conquest_bandit_10", {}).get("merit", 0))
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1["merit"]) - merit0, cfg_merit)
        # 再刷不重复解锁
        self.assertFalse(await self.m._award_achievement("u1",
                                                         "conquest_bandit_10"))
        view = await self.m.get_achievements_view("u1")
        self.assertIn("conquest_bandit_10", view["unlocked"])

    async def test_p3_event_onset_wanderer_auction_flow(self):
        import random as _random
        world = await self.m.get_world()
        rng = _random.Random(42)
        ev = {"id": "wandering_general", "duration_ticks": 48,
              "effects": [{"type": "spawn_wandering_char",
                           "count": [1, 1],
                           "rarity_weights": {"epic": 100}}]}
        await self.m._apply_event_onset(world, ev, {}, rng)
        auctions = await self.m.get_wanderer_auctions_view(world, "u1")
        self.assertEqual(len(auctions), 1)
        a = auctions[0]
        self.assertEqual(a["current_price"], 0)
        self.assertEqual(a["bidder_label"], "")
        # 旧的一口价招募入口已关闭
        with self.assertRaises(self.StrategyError):
            await self.m.recruit_wanderer("u1", a["char_id"])
        # u1 出价：铜钱即时托管
        p0 = await self.m.get_player("u1", self.wid)
        coin0 = int(p0["coin"])
        r = await self.m.bid_wanderer("u1", a["id"], a["min_next"])
        self.assertTrue(r["ok"])
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1["coin"]), coin0 - a["min_next"])
        # u2 超价：u1 的托管铜钱自动退还
        au = (await self.m.get_wanderer_auctions_view(world, "u2"))[0]
        r2 = await self.m.bid_wanderer("u2", au["id"], au["min_next"])
        self.assertTrue(r2["ok"])
        p1b = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1b["coin"]), coin0)
        # 到期结算：流将归最高出价者
        await self.m._execute(
            "UPDATE strategy_char_auctions SET expire_at=? WHERE id=?",
            (_now() - 1, au["id"]))
        await self.m._step_wanderer_auctions(await self.m.get_world())
        c = await self.m.get_char(au["char_id"])
        self.assertEqual(c["status"], "idle")
        self.assertEqual(c["owner_user_id"], "u2")
        # 无出价到期：流将遁走（角色删除）
        world = await self.m.get_world()
        await self.m._apply_event_onset(world, ev, {}, rng)
        a2 = (await self.m.get_wanderer_auctions_view(world, "u1"))[0]
        self.assertIsNotNone(await self.m.get_char(a2["char_id"]))
        await self.m._execute(
            "UPDATE strategy_char_auctions SET expire_at=? WHERE id=?",
            (_now() - 1, a2["id"]))
        await self.m._step_wanderer_auctions(await self.m.get_world())
        self.assertIsNone(await self.m.get_char(a2["char_id"]))
        # 一口价：立即成交
        world = await self.m.get_world()
        await self.m._apply_event_onset(world, ev, {}, rng)
        a3 = (await self.m.get_wanderer_auctions_view(world, "u3"))[0]
        r3 = await self.m.bid_wanderer("u3", a3["id"], 0, buyout=True)
        self.assertTrue(r3["ok"])
        c3 = await self.m.get_char(a3["char_id"])
        self.assertEqual(c3["owner_user_id"], "u3")
        #  tournament 效果
        ev2 = {"id": "alliance_tournament", "duration_ticks": 24,
               "effects": [{"type": "tournament"}]}
        world = await self.m.get_world()
        await self.m._apply_event_onset(world, ev2, {}, rng)
        world = await self.m.get_world()
        self.assertGreater(int((world["data"] or {}).get(
            "tournament_until_tick", 0)), 0)
        # bandit_raid 效果
        nodes = await self.m.get_nodes(self.wid)
        target = next(n for n in nodes if n["type"] == "grain")
        ev3 = {"id": "raid_incoming", "duration_ticks": 6,
               "effects": [{"type": "bandit_raid", "stock_loss_pct": 0.1,
                            "troops": [600, 1200], "hours": 24}]}
        world = await self.m.get_world()
        await self.m._apply_event_onset(
            world, ev3, {"node_ids": [target["id"]]}, rng)
        world = await self.m.get_world()
        bandits = (world["data"] or {}).get("bandits") or {}
        self.assertIn(str(target["id"]), bandits)

    # ------------------------------------------------------------------
    # 面板 / tick 全管线
    # ------------------------------------------------------------------
    async def test_get_state_views_and_full_tick(self):
        state = await self.m.get_state("u1")
        self.assertTrue(state["joined"])
        for key in ("season", "bandits", "trade_routes", "meta"):
            self.assertIn(key, state)
        pv = state["player"]
        for key in ("faction", "captives", "market_orders", "convoys",
                    "duels", "achievements", "intel"):
            self.assertIn(key, pv)
        meta = state["meta"]
        for key in ("plot_types", "tech_lines", "achievements", "hex_cfg",
                    "captive_cfg", "market_cfg", "season_cfg"):
            self.assertIn(key, meta)
        # 全管线 tick（含 P1~P3 新步骤）连跑 3 次无异常
        for _ in range(3):
            summary = await self.m.tick()
            self.assertTrue(summary["ok"], summary.get("error"))

    async def test_event_effects_spread_injury_hunt(self):
        """瘟疫 spread 传染 / task_injury 任务受伤 / hunt_merit_mult 讨伐战功倍率。"""
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        grain = next(n for n in nodes.values() if n["type"] == "grain" and n["adj"])
        adj_id = int(grain["adj"][0])
        world = await self.m.get_world()
        tick_no = int(world["tick_no"])
        # 瘟疫事件（chance=1.0 保证确定性）
        await self._execute("""
            INSERT INTO strategy_events (world_id, event_id, scope, target_json,
                start_at, end_at, start_tick, end_tick, data_json)
            VALUES (?,'plague','region',?,?,?,?,?,?)
        """, (self.wid,
              json.dumps({"node_ids": [grain["id"]], "node_name": grain["name"]},
                         ensure_ascii=False),
              _now(), _now() + 8 * 60, tick_no, tick_no + 8,
              json.dumps({"name": "瘟疫", "effects": [
                  {"type": "node_dot", "troops_pct": -3, "morale": -10},
                  {"type": "spread", "to": "adjacent", "chance": 1.0},
                  {"type": "task_injury", "task_types": ["*"],
                   "chance": 1.0, "injury": 10}]}, ensure_ascii=False)))
        # 魔潮事件：讨伐战功 ×2
        await self._execute("""
            INSERT INTO strategy_events (world_id, event_id, scope, target_json,
                start_at, end_at, start_tick, end_tick, data_json)
            VALUES (?,'demon_tide','world','{}',?,?,?,?,?)
        """, (self.wid, _now(), _now() + 8 * 60, tick_no, tick_no + 8,
              json.dumps({"name": "魔潮", "effects": [
                  {"type": "hunt_merit_mult", "mult": 2.0}]}, ensure_ascii=False)))
        world = await self.m.get_world()
        # 1) spread：100% 传染至相邻据点
        await self.m._step_event_spread(world)
        rows = await self._fetchall(
            "SELECT target_json FROM strategy_events WHERE world_id=? "
            "AND event_id='plague'", (self.wid,))
        target = json.loads(rows[0]["target_json"])
        self.assertIn(adj_id, target["node_ids"])
        self.assertIn(grain["id"], target["node_ids"])
        # 再传染一轮：无重复节点
        await self.m._step_event_spread(world)
        rows = await self._fetchall(
            "SELECT target_json FROM strategy_events WHERE world_id=? "
            "AND event_id='plague'", (self.wid,))
        target2 = json.loads(rows[0]["target_json"])
        self.assertEqual(len(set(target2["node_ids"])), len(target2["node_ids"]))
        # 2) task_injury：疫区任务结算 → 伤势 +10
        c = await self._char_of("u1", "general")
        inj0 = int(c["injury"])
        fake_task = {"id": 999999, "task_type": "farm", "node_id": grain["id"]}
        await self.m._roll_task_injury(world, fake_task, c)
        self.assertEqual(int(c["injury"]), inj0 + 10)
        # 非疫区不受影响（若已全图感染则跳过该断言）
        other = next((n for n in nodes.values()
                      if n["id"] not in target2["node_ids"]), None)
        if other:
            fake_task2 = {"id": 999998, "task_type": "farm", "node_id": other["id"]}
            await self.m._roll_task_injury(world, fake_task2, c)
            self.assertEqual(int(c["injury"]), inj0 + 10)
        # 3) hunt_merit_mult：魔潮期讨伐战功 ×2
        self.assertEqual(await self.m._hunt_merit_mult(world), 2.0)

    async def test_diplomacy_proposal_expiry_cleanup(self):
        """过期 propose_* 提案条目在 _step_governance 中被物理清理。"""
        fid = int(self.p1["faction_id"])
        f = await self.m.get_faction(fid)
        dip = f.setdefault("diplomacy", {})
        self.m._set_relation(dip, 99999, "propose_ally", until=_now() - 10)
        self.m._set_relation(dip, 88888, "propose_truce_out", until=_now() + 3600)
        await self.m.save_faction(f)
        world = await self.m.get_world()
        await self.m._step_governance(world)
        f = await self.m.get_faction(fid)
        dip = f.get("diplomacy") or {}
        self.assertNotIn("99999", dip)  # 已过期 → 清理
        self.assertIn("88888", dip)     # 未过期 → 保留

    # ------------------------------------------------------------------
    # 审查修复回归（A1/A2/A4/A-M12/B1/B16/C2/C3）
    # ------------------------------------------------------------------
    async def test_battle_rng_state_roundtrip(self):
        """B1：_rng_state 经 JSON 序列化往返后仍能精确恢复随机流。

        此前 st[2]（gauss_next=None）被 float(None) 炸掉 → 状态恢复静默
        回退到种子重来，托管/回放/续跑三方掷骰序列错位。
        """
        battle = {"id": 77, "map_seed": "seed-abc", "session": {}}
        rng = self.m._battle_rng(battle)
        _ = [rng.random() for _ in range(3)]  # 先推进随机流（保存态 ≠ 种子新态）
        self.m._save_rng(battle, rng)
        expected = [rng.random() for _ in range(5)]
        # 模拟 DB JSON 序列化往返（tuple→list、None→null）
        sess = json.loads(json.dumps(battle["session"]))
        battle2 = {"id": 77, "map_seed": "seed-abc", "session": sess}
        rng2 = self.m._battle_rng(battle2)
        self.assertEqual([rng2.random() for _ in range(5)], expected)
        # 对照：从种子重来的序列必须不同（证明确实走了状态恢复）
        fresh = random.Random("seed-abc")
        self.assertNotEqual([fresh.random() for _ in range(5)], expected)

    async def test_market_buyout_rebuy_cancel_guards(self):
        """C2：挂单买断后 status=filled，再买/撤单均被拒且货物不重复退回。"""
        nodes = await self.m.get_nodes(self.wid)
        mkt = next(n for n in nodes if n["type"] in ("market", "city"))
        await self._give("u1", grain=10_000, coin=100_000)
        await self._give("u2", coin=100_000)
        r = await self.m.market_list("u1", mkt["id"], "grain", 500, 2)
        oid = int(r["order_id"])
        grain2_before = int((await self.m.get_player("u2", self.wid))["grain"])
        res = await self.m.market_buy("u2", oid)  # amount=0 → 全量买入
        self.assertTrue(res["ok"])
        grain2_after = int((await self.m.get_player("u2", self.wid))["grain"])
        self.assertEqual(grain2_after - grain2_before, 500)
        row = await self.m._fetchone(
            "SELECT status, amount FROM strategy_market_orders WHERE id=?", (oid,))
        self.assertEqual(row["status"], "filled")
        self.assertEqual(int(row["amount"]), 0)
        # 买断后重复买入被拒
        with self.assertRaises(self.StrategyError):
            await self.m.market_buy("u2", oid)
        # 买断后卖家撤单被拒（防货款双得）
        with self.assertRaises(self.StrategyError):
            await self.m.market_cancel("u1", oid)

    async def test_settle_season_idempotent_guard(self):
        """C3：同一 (world_id, season_no) 重复结算被幂等守卫拒绝。"""
        world = await self.m.get_world()
        season_no = int(world.get("season_no", 1))
        await self.m._settle_season(world)  # 首次：正常存档并开新世界
        with self.assertRaises(self.StrategyError) as cm:
            await self.m._settle_season(world)
        self.assertIn("已结算", str(cm.exception))
        rows = await self._fetchall(
            "SELECT COUNT(*) AS n FROM strategy_season_results "
            "WHERE world_id=? AND season_no=?", (self.wid, season_no))
        self.assertEqual(int(rows[0]["n"]), 1)  # 无双份存档

    async def test_siege_wipeout_aftermath(self):
        """A1：围城被城防反击全歼 → 军团删除、角色归还、siege_state 清空。

        此前仅从内存列表过滤，DB 残留 troops=0/state=siege 的僵尸军团。
        """
        fid = int(self.p1["faction_id"])
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        home = next(n for n in nodes.values() if n["owner_faction"] == 0)
        await self._own("u1", home)
        target = nodes[int(home["adj"][0])]
        target["owner_faction"] = 0  # 无主：跳过被俘判定，保证断言确定性
        target["npc_garrison"] = {"troops": 100_000, "morale": 90}
        target["siege_state"] = {"ticks": 0, "attacker_faction": fid,
                                 "choice": "auto", "since": _now()}
        await self.m.save_node(target)
        army = await self._mk_army("u1", home["id"], 100)
        cid = int((army.get("characters") or [0])[0])
        army["state"] = "siege"
        army["node_id"] = target["id"]
        army["path"] = []
        await self.m.save_army(army)
        world = await self.m.get_world()
        await self.m._step_sieges(world)
        # 军团已删除（不再残留僵尸行）
        self.assertIsNone(await self.m.get_army(army["id"]))
        left = await self.m.get_armies(self.wid, node_id=target["id"],
                                       states=("siege",))
        self.assertEqual(left, [])
        # 角色归还（非 army 态；伤势 +40 可能陨落）
        c = await self.m.get_char(cid)
        self.assertIn(c["status"], ("idle", "dead"))
        # siege_state 清空（不再悬挂）
        node = await self.m.get_node(self.wid, target["id"])
        self.assertEqual(node.get("siege_state") or {}, {})

    async def test_siege_breach_surrender_attackers_garrison(self):
        """A2：破城受降后节点易主，同势力围城军转入驻守（不再围攻己方城）。"""
        fid = int(self.p1["faction_id"])
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        home = next(n for n in nodes.values() if n["owner_faction"] == 0)
        await self._own("u1", home)
        target = nodes[int(home["adj"][0])]
        target["owner_faction"] = 0
        target["defense"] = 0
        target["npc_garrison"] = {"troops": 0, "morale": 50}
        target["siege_state"] = {"ticks": 3, "attacker_faction": fid,
                                 "choice": "surrender", "since": _now()}
        await self.m.save_node(target)
        army = await self._mk_army("u1", home["id"], 1500)
        army["state"] = "siege"
        army["node_id"] = target["id"]
        army["path"] = []
        await self.m.save_army(army)
        world = await self.m.get_world()
        reports = await self.m._step_sieges(world)
        self.assertTrue(any("受降" in r for r in reports))
        node = await self.m.get_node(self.wid, target["id"])
        self.assertEqual(int(node["owner_faction"]), fid)
        self.assertEqual(node.get("siege_state") or {}, {})
        a = await self.m.get_army(army["id"])
        self.assertEqual(a["state"], "idle")  # 围城军已转驻守

    async def test_march_newbie_protection(self):
        """A4：进攻刚入场玩家节点触发新手保护期（created_at 缺列致其永远跳过）。"""
        fid2 = int(self.p2["faction_id"])
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        home = next(n for n in nodes.values() if n["owner_faction"] == 0)
        await self._own("u1", home)
        target = nodes[int(home["adj"][0])]
        target["owner_faction"] = fid2
        target["protect_until"] = 0  # 绕开节点保护期分支，专测新手保护
        await self.m.save_node(target)
        army = await self._mk_army("u1", home["id"], 800)
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.march("u1", army["id"], target["id"])
        self.assertIn("新手保护期", str(cm.exception))

    async def test_manual_battle_replay_and_report_permission(self):
        """B16 赛中回放拒绝 + A-M12 非参战方战报越权拒绝。"""
        _world, _target, _army, battle = await self._setup_manual_battle()
        bid = int(battle["id"])
        # B16：未终局取回放被拒（防泄露 map_seed/伏兵/指令流）
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.get_battle_replay("u1", bid)
        self.assertIn("战役结束后才能获取回放", str(cm.exception))
        # A-M12：非参战方（u2）无权查看战报
        with self.assertRaises(self.StrategyError) as cm2:
            await self.m.get_battle_report("u2", bid)
        self.assertIn("无权查看", str(cm2.exception))
        # 参战方（u1 攻方）可查
        rep = await self.m.get_battle_report("u1", bid)
        self.assertEqual(int(rep["id"]), bid)

    async def test_task_meta_recall_and_research_points(self):
        """任务选择提示 / 随时召回零结算 / 研究任务科技点抵扣。"""
        meta = self.m._frontend_meta()["task_meta"]
        self.assertEqual(meta["train"]["stat"], "统率")
        self.assertEqual(meta["farm"]["role"], "内政官")
        self.assertEqual(meta["scout"]["output"], "情报")
        self.assertEqual(meta["research"]["output"], "科技点")

        p1 = await self.m.get_player("u1", self.wid)
        f = await self.m.get_faction(int(p1["faction_id"]))
        home = int(f["capital_node_id"])
        general = await self._char_of("u1", "general")
        r = await self.m.dispatch_task("u1", general["id"], home, "farm", 8)
        out = await self.m.recall_task("u1", int(r["task_id"]))
        self.assertEqual(int(out["payout"].get("amount", 0)), 0)
        c = await self.m.get_char(general["id"])
        self.assertEqual(c["status"], "idle")

        f = await self.m.get_faction(int(p1["faction_id"]))
        f["treasury"] = {"coin": 100_000, "iron": 100_000, "grain": 0, "gold": 0}
        f["tech"] = {"points": 600}
        await self.m.save_faction(f)
        await self.m._commit()
        res = await self.m.tech_research("u1", "military")
        self.assertIn("科技点抵扣", res["msg"])
        f = await self.m.get_faction(int(p1["faction_id"]))
        self.assertTrue((f.get("tech") or {}).get("pending"))
        self.assertEqual(int((f.get("tech") or {}).get("points", 0)), 360)

    async def test_char_appointment_unlock_and_troop_cap(self):
        """角色官职：绑角色、按据点解锁、主将带兵上限随角色官职。"""
        p1 = await self.m.get_player("u1", self.wid)
        fid = int(p1["faction_id"])
        f = await self.m.get_faction(fid)
        home = int(f["capital_node_id"])
        general = await self._char_of("u1", "general")
        # 1 据点：仅中郎将
        f = await self.m.get_faction(fid)
        avail = self.R.available_positions(
            await self.m._governance_node_count(self.wid, f))
        self.assertEqual(avail, {"zhonglang": 1})
        await self.m.faction_appoint_char("u1", general["id"], "zhonglang")
        c = await self.m.get_char(general["id"])
        self.assertEqual(c["appointed_position"], "zhonglang")
        self.assertEqual(await self.m._leader_cap(c, f, home), 2000)
        # 未解锁官职报错
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.faction_appoint_char("u1", general["id"], "taiwei")
        self.assertIn("据点不足", str(cm.exception))
        # 扩到 8 据点：太尉/太守解锁
        extra = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"][:7]
        self.assertGreaterEqual(len(extra), 7)
        for n in extra:
            n["owner_faction"] = fid
            await self.m.save_node(n)
        f = await self.m.get_faction(fid)
        node_count = await self.m._governance_node_count(self.wid, f)
        self.assertEqual(node_count, 8)
        avail = self.R.available_positions(node_count)
        self.assertIn("taiwei", avail)
        self.assertEqual(avail["taishou"], 8)
        strategist = await self._recruit_role("u1", self.R.ROLE_STRATEGIST)
        await self.m.faction_appoint_char("u1", strategist["id"], "taiwei")
        c2 = await self.m.get_char(strategist["id"])
        self.assertEqual(c2["appointed_position"], "taiwei")
        self.assertEqual(await self.m._leader_cap(c2, f, home), 3000)
        # 太守须绑据点：辖内 2000，辖外 1500
        await self.m.faction_appoint_char("u1", general["id"], "taishou", home)
        c = await self.m.get_char(general["id"])
        self.assertEqual(c["appointed_position"], "taishou")
        self.assertEqual(int(c["appointed_node_id"]), home)
        self.assertEqual(await self.m._leader_cap(c, f, home), 2000)
        other = int(extra[0]["id"])
        self.assertEqual(await self.m._leader_cap(c, f, other), 1500)
        # 同一据点不可有两个太守
        with self.assertRaises(self.StrategyError):
            await self.m.faction_appoint_char("u1", strategist["id"], "taishou", home)
        # 卸任
        await self.m.faction_appoint_char("u1", general["id"], "")
        c = await self.m.get_char(general["id"])
        self.assertEqual(c["appointed_position"], "")

    async def test_scout_task_reveals_extra_ring(self):
        """侦察任务点亮 2 跳范围，且情报按势力隔离。"""
        p1 = await self.m.get_player("u1", self.wid)
        f1 = await self.m.get_faction(int(p1["faction_id"]))
        home = int(f1["capital_node_id"])
        world = await self.m.get_world()
        n = await self.m._apply_scout_intel(
            world, {"node_id": home, "faction_id": int(p1["faction_id"])}, 32)
        self.assertGreater(n, 1)
        world = await self.m.get_world()
        bucket = ((world.get("data") or {}).get("scouted") or {}).get(
            str(int(p1["faction_id"]))) or {}
        self.assertTrue(bucket)
        state = await self.m.get_state("u1")
        self.assertTrue(any(int(x.get("scouted_until") or 0) > 0
                            for x in state["nodes"]))
        state2 = await self.m.get_state("u2")
        self.assertFalse(any(int(x.get("scouted_until") or 0) > 0
                             for x in state2["nodes"]))

    # ------------------------------------------------------------------
    async def _fetchall(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def _execute(self, sql: str, params: tuple = ()):
        await self.conn.execute(sql, params)
        await self.conn.commit()


if __name__ == "__main__":
    unittest.main()
