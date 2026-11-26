# tests/test_strategy_coalition.py
# 仙域争霸 v4.8.0：合军（联军）与酒馆招募错位修复回归。
import os
import sys
import tempfile
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


class TestStrategyCoalition(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.StrategyError = _imp("managers.strategy_errors").StrategyError
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
            user_id="u1", gold=10_000_000, user_name="测试u1"))

        self.m = self.StrategyManager(self.db)
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        self.p = await self._enter("u1", "甲")
        self.home = await self._owned_node("u1")

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    # ------------------------------------------------------------------
    # 夹具
    # ------------------------------------------------------------------
    async def _enter(self, user_id: str, nickname: str) -> dict:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        nodes.sort(key=lambda n: (n["type"] not in ("city", "pass", "branch"),
                                  -int(n.get("level", 1)), int(n["id"])))
        return await self.m.enter_game(
            user_id, nickname, node_id=int(nodes[0]["id"]))

    async def _owned_node(self, uid: str) -> dict:
        p = await self.m.get_player(uid, self.wid)
        for n in await self.m.get_nodes(self.wid):
            if int(n["owner_faction"]) == int(p["faction_id"]):
                return n
        self.fail(f"{uid} 没有据点")

    async def _char(self, uid: str, role: str) -> dict:
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == role:
                return c
        self.fail(f"{uid} 没有 {role} 角色")

    async def _place(self, char: dict, node_id: int) -> None:
        c = await self.m.get_char(int(char["id"]))
        c["location"] = {"node_id": int(node_id)}
        c["status"] = "idle"
        await self.m.save_char(c)
        char["location"] = dict(c["location"])

    async def _wallet(self, coin: int = 500_000, grain: int = 500_000) -> dict:
        p = await self.m.get_player("u1", self.wid)
        p["coin"], p["grain"] = int(coin), int(grain)
        await self.m.save_player(p)
        await self.m._commit()
        return p

    async def _two_armies(self, troops: int = 300):
        """在首都编两支驻守军团（消耗两个角色）。"""
        await self._wallet()
        gen = await self._char("u1", "general")
        off = await self._char("u1", "official")
        await self._place(gen, int(self.home["id"]))
        await self._place(off, int(self.home["id"]))
        r1 = await self.m.create_army(
            "u1", int(gen["id"]), [], int(self.home["id"]),
            "sword_shield", troops)
        r2 = await self.m.create_army(
            "u1", int(off["id"]), [], int(self.home["id"]),
            "sword_shield", troops)
        a1 = await self.m.get_army(int(r1["army_id"]))
        a2 = await self.m.get_army(int(r2["army_id"]))
        return a1, a2

    async def _friendly_second_node(self) -> dict:
        """把首都的相邻无主据点纳入己方，作为联军行军目标。"""
        home = await self.m.get_node(self.wid, int(self.home["id"]))
        for nid in home.get("adj") or []:
            node = await self.m.get_node(self.wid, int(nid))
            if node and int(node.get("owner_faction") or 0) == 0:
                node["owner_faction"] = int(self.p["faction_id"])
                await self.m.save_node(node)
                await self.m._commit()
                return node
        # 地图生成含时间随机，个别轮次首都没有相邻无主据点：任取一处
        # 无主据点并补一条通路，保证联军行军链路可测（夹具行为，不影响生产）。
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"
                 and int(n["id"]) != int(home["id"])]
        if not nodes:
            self.fail("没有可用的无主据点")
        node = nodes[0]
        adj = [int(x) for x in (home.get("adj") or [])]
        if int(node["id"]) not in adj:
            adj.append(int(node["id"]))
            home["adj"] = adj
            await self.m.save_node(home)
        n_adj = [int(x) for x in (node.get("adj") or [])]
        if int(home["id"]) not in n_adj:
            n_adj.append(int(home["id"]))
            node["adj"] = n_adj
        node["owner_faction"] = int(self.p["faction_id"])
        await self.m.save_node(node)
        await self.m._commit()
        return node

    # ------------------------------------------------------------------
    # 合军：基础校验
    # ------------------------------------------------------------------
    async def test_form_and_disband_coalition(self):
        a1, a2 = await self._two_armies()
        res = await self.m.form_coalition("u1", int(a1["id"]), int(a2["id"]))
        self.assertTrue(res["ok"])
        cid = int(res["coalition_id"])
        a1 = await self.m.get_army(int(a1["id"]))
        a2 = await self.m.get_army(int(a2["id"]))
        self.assertEqual(int(a1["coalition_id"]), cid)
        self.assertEqual(int(a2["coalition_id"]), cid)
        co = await self.m.get_coalition(cid)
        self.assertEqual(sorted(int(x) for x in co["member_ids"]),
                         sorted([int(a1["id"]), int(a2["id"])]))
        self.assertEqual(int(co["leader_army_id"]), int(a1["id"]))
        # 状态快照下发联军字段与酒馆 roll_seq（前端渲染/防错位依赖）
        state = await self.m.get_state("u1")
        avs = {int(x["id"]): x for x in state["player"]["armies"]}
        self.assertEqual(int(avs[int(a1["id"])]["coalition_id"]), cid)
        self.assertEqual(avs[int(a1["id"])]["coalition_role"], "leader")
        self.assertIn("roll_seq", state["player"]["tavern"])
        # 重复合军被拒
        with self.assertRaises(self.StrategyError):
            await self.m.form_coalition("u1", int(a1["id"]), int(a2["id"]))
        res = await self.m.disband_coalition("u1", int(a1["id"]))
        self.assertTrue(res["ok"])
        a1 = await self.m.get_army(int(a1["id"]))
        a2 = await self.m.get_army(int(a2["id"]))
        self.assertEqual(int(a1["coalition_id"]), 0)
        self.assertEqual(int(a2["coalition_id"]), 0)
        self.assertIsNone(await self.m.get_coalition(cid))

    async def test_coalition_requires_same_node(self):
        a1, a2 = await self._two_armies()
        other = await self._friendly_second_node()
        a2["node_id"] = int(other["id"])
        await self.m.save_army(a2)
        await self.m._commit()
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.form_coalition("u1", int(a1["id"]), int(a2["id"]))
        self.assertIn("同一据点", str(cm.exception))

    # ------------------------------------------------------------------
    # 合军：统一行军（同一 tick 推进 / 抵达）
    # ------------------------------------------------------------------
    async def test_coalition_march_lockstep(self):
        a1, a2 = await self._two_armies()
        target = await self._friendly_second_node()
        res = await self.m.form_coalition("u1", int(a1["id"]), int(a2["id"]))
        cid = int(res["coalition_id"])
        out = await self.m.march("u1", int(a2["id"]), int(target["id"]))
        self.assertTrue(out["ok"])
        a1 = await self.m.get_army(int(a1["id"]))
        a2 = await self.m.get_army(int(a2["id"]))
        self.assertEqual(a1["state"], "march")
        self.assertEqual(a2["state"], "march")
        self.assertEqual(a1["path"], a2["path"])
        co = await self.m.get_coalition(cid)
        self.assertEqual(co["status"], "march")
        self.assertEqual(int(co["target_node_id"]), int(target["id"]))
        for _ in range(30):
            await self.m._step_march(self.world)
            a1 = await self.m.get_army(int(a1["id"]))
            a2 = await self.m.get_army(int(a2["id"]))
            self.assertEqual(int(a1["node_id"]), int(a2["node_id"]))
            self.assertEqual(a1["path"], a2["path"])
            if a1["state"] == "idle" and a2["state"] == "idle":
                break
        self.assertEqual(int(a1["node_id"]), int(target["id"]))
        self.assertEqual(int(a2["node_id"]), int(target["id"]))
        co = await self.m.get_coalition(cid)
        self.assertEqual(co["status"], "idle")
        # 抵达后仍独立结算：兵力/携粮各自保留
        self.assertEqual(int(a1["troops"]), 300)
        self.assertEqual(int(a2["troops"]), 300)

    async def test_coalition_disband_blocked_while_marching(self):
        a1, a2 = await self._two_armies()
        target = await self._friendly_second_node()
        await self.m.form_coalition("u1", int(a1["id"]), int(a2["id"]))
        await self.m.march("u1", int(a1["id"]), int(target["id"]))
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.disband_coalition("u1", int(a1["id"]))
        self.assertIn("行军", str(cm.exception))

    async def test_coalition_auto_dissolves_when_member_leaves(self):
        a1, a2 = await self._two_armies()
        other = await self._friendly_second_node()
        res = await self.m.form_coalition("u1", int(a1["id"]), int(a2["id"]))
        cid = int(res["coalition_id"])
        # 拆掉一支：delete_army 应连带解散联军
        await self.m.delete_army(int(a2["id"]))
        a1 = await self.m.get_army(int(a1["id"]))
        self.assertEqual(int(a1["coalition_id"]), 0)
        self.assertIsNone(await self.m.get_coalition(cid))
        void = await self.m.get_node(self.wid, int(other["id"]))
        self.assertIsNotNone(void)

    # ------------------------------------------------------------------
    # 招募错位：候选重掷后拒绝按旧序号招募
    # ------------------------------------------------------------------
    async def test_recruit_rejects_index_when_tavern_rerolled(self):
        await self._wallet(coin=10_000, grain=10_000)
        p = await self.m.get_player("u1", self.wid)
        # 造一份「旧页面」：refresh_at 已过期、roll_seq 小于服务端将重掷的序号
        p["tavern"] = {
            "candidates": [{
                "name": "旧页面候选", "role": "general", "rarity": "rare",
                "profession": "demon", "stats": {}, "traits": [],
                "personality": {}, "aptitude": {},
            }],
            "refreshed_at": 0, "refresh_at": 0, "roll_seq": 3,
            "daily_refresh_date": "", "daily_refresh_used": 0,
        }
        await self.m.save_player(p)
        await self.m._commit()
        before = await self.m.get_player("u1", self.wid)
        coin0 = int(before["coin"])
        roster0 = len(await self.m.get_chars(
            owner_user_id="u1", world_id=self.wid))
        res = await self.m.recruit(before, self.wid, 0,
                                   expected_roll=3, expected_name="旧页面候选")
        self.assertFalse(res.get("ok", True))
        self.assertTrue(res.get("refreshed"))
        after = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(after["coin"]), coin0)
        self.assertEqual(len(await self.m.get_chars(
            owner_user_id="u1", world_id=self.wid)), roster0)
        # 重掷后的新列表可用（带正确 roll/name）
        cur = await self.m.get_player("u1", self.wid)
        tavern = cur.get("tavern") or {}
        cand = (tavern.get("candidates") or [])[0]
        res2 = await self.m.recruit(
            cur, self.wid, 0, expected_roll=int(tavern.get("roll_seq") or 0),
            expected_name=str(cand["name"]))
        self.assertEqual(res2.get("name"), cand["name"])


if __name__ == "__main__":
    unittest.main()
