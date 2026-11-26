# tests/test_strategy_siege_ownership.py
# 仙域争霸 v4.8.2：围城归属保护回归。
#   · 已有势力围城后，其他势力不能直接抢归属；
#   · 友方 / 与守方开战者只能增援，破城方仍是原攻方；
#   · 与攻方敌对者需先打城下营地战，原攻方被消灭后才可能接管；
#   · 没有围城军时悬挂的 siege_state 必须被清理。
import os
import sys
import tempfile
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


class TestStrategySiegeOwnership(unittest.IsolatedAsyncioTestCase):
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
        self.m.main_cfg().setdefault("hex", {})[
            "field_encounter_mode"] = "auto"
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        self.p1 = await self._enter("u1", "守方")
        self.p2 = await self._enter("u2", "围城方")
        self.p3 = await self._enter("u3", "挑战方")
        for uid in ("u1", "u2", "u3"):
            await self._give(uid, coin=500_000, grain=50_000, iron=5_000)
        self.fid1 = int(self.p1["faction_id"])
        self.fid2 = int(self.p2["faction_id"])
        self.fid3 = int(self.p3["faction_id"])
        self.target = await self._owned_node("u1")

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

    async def _give(self, uid: str, **res) -> dict:
        p = await self.m.get_player(uid, self.wid)
        for k, v in res.items():
            p[k] = int(v)
        await self.m.save_player(p)
        await self.m._commit()
        return p

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

    async def _mk_army(self, uid: str, node_id: int, troops: int = 800) -> dict:
        await self._give(uid, coin=5_000_000, grain=5_000_000)
        c = await self._char(uid, "general")
        cur = await self.m.get_char(int(c["id"]))
        cur["location"] = {"node_id": int(node_id)}
        await self.m.save_char(cur)
        r = await self.m.create_army(uid, int(c["id"]), [], int(node_id),
                                     "sword_shield", troops)
        return await self.m.get_army(int(r["army_id"]))

    async def _siege_army(self, uid: str, home_node=None,
                          troops: int = 500) -> dict:
        if home_node is None:
            home_node = int((await self._owned_node(uid))["id"])
        a = await self._mk_army(uid, int(home_node), troops)
        a["node_id"] = int(self.target["id"])
        a["state"] = "siege"
        a["path"] = []
        a["edge_pos"] = 0.0
        await self.m.save_army(a)
        return a

    async def _arriving_army(self, uid: str, home_node=None,
                             troops: int = 800) -> dict:
        if home_node is None:
            home_node = int((await self._owned_node(uid))["id"])
        a = await self._mk_army(uid, int(home_node), troops)
        a["node_id"] = int(self.target["id"])
        a["state"] = "march"
        a["path"] = []
        a["edge_pos"] = 0.0
        await self.m.save_army(a)
        return a

    async def _set_war(self, fid_a: int, fid_b: int) -> None:
        for a, b in ((fid_a, fid_b), (fid_b, fid_a)):
            f = await self.m.get_faction(int(a))
            dip = dict(f.get("diplomacy") or {})
            dip[str(int(b))] = {"rel": self.R.REL_WAR, "war_at": 0}
            f["diplomacy"] = dip
            await self.m.save_faction(f)
        await self.m._commit()

    async def _prepare_siege(self, ticks: int = 5) -> dict:
        node = await self.m.get_node(self.wid, int(self.target["id"]))
        node["defense"] = 5000
        node["npc_garrison"] = {"troops": 0, "morale": 60}
        node["protect_until"] = 0
        node["siege_state"] = {"attacker_faction": self.fid2,
                               "ticks": int(ticks), "choice": "auto",
                               "since": 0}
        await self.m.save_node(node)
        await self.m._commit()
        return node

    # ------------------------------------------------------------------
    # 1. 友方（同盟）增援：归属不变，围城进度延续
    # ------------------------------------------------------------------
    async def test_allied_reinforcement_keeps_siege_owner(self):
        node = await self._prepare_siege()
        a2 = await self._siege_army("u2")
        f3 = await self.m.get_faction(self.fid3)
        f3["alliance_id"] = self.fid2
        await self.m.save_faction(f3)
        await self.m._commit()
        a3 = await self._arriving_army("u3")

        await self.m._attack_node(self.world, node, [a3])

        node = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(
            int(node["siege_state"]["attacker_faction"]), self.fid2)
        self.assertEqual(int(node["siege_state"]["ticks"]), 5)
        a3 = await self.m.get_army(int(a3["id"]))
        a2 = await self.m.get_army(int(a2["id"]))
        self.assertEqual(a3["state"], "siege")
        self.assertEqual(a2["state"], "siege")

    # ------------------------------------------------------------------
    # 2. 与守方开战的第三方增援：仍是增援，破城方不变
    # ------------------------------------------------------------------
    async def test_third_party_at_war_with_defender_reinforces(self):
        node = await self._prepare_siege()
        await self._siege_army("u2")
        await self._set_war(self.fid3, self.fid1)
        a3 = await self._arriving_army("u3")

        await self.m._attack_node(self.world, node, [a3])

        node = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(
            int(node["siege_state"]["attacker_faction"]), self.fid2)

    # ------------------------------------------------------------------
    # 3. 中立势力：拒绝加入围城，原地待命
    # ------------------------------------------------------------------
    async def test_neutral_arrival_rejected(self):
        node = await self._prepare_siege()
        await self._siege_army("u2")
        a3 = await self._arriving_army("u3")

        report = await self.m._attack_node(self.world, node, [a3])

        self.assertIn("无法加入围城", report)
        a3 = await self.m.get_army(int(a3["id"]))
        self.assertEqual(a3["state"], "idle")
        node = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(
            int(node["siege_state"]["attacker_faction"]), self.fid2)

    # ------------------------------------------------------------------
    # 4. 与攻方敌对：先打城下营地战；原攻方存活则归属不变
    # ------------------------------------------------------------------
    async def test_hostile_camp_battle_owner_survives(self):
        node = await self._prepare_siege()
        await self._siege_army("u2")
        await self._set_war(self.fid3, self.fid2)
        a3 = await self._arriving_army("u3")

        camp = mock.AsyncMock(return_value="CAMP_REPORT")
        with mock.patch.object(self.m, "_siege_camp_battle", camp):
            report = await self.m._attack_node(self.world, node, [a3])

        self.assertEqual(report, "CAMP_REPORT")
        self.assertTrue(camp.await_count == 1)
        node = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(
            int(node["siege_state"]["attacker_faction"]), self.fid2)

    # ------------------------------------------------------------------
    # 5. 原攻方被消灭后，挑战方才可能接管
    # ------------------------------------------------------------------
    async def test_takeover_only_after_owner_wiped(self):
        node = await self._prepare_siege()
        a2 = await self._siege_army("u2")
        await self._set_war(self.fid3, self.fid2)
        a3 = await self._arriving_army("u3")
        node["defense"] = 0

        async def _win_camp(world, node, attackers, camp, atk_fid, owner_fid):
            for a in camp:
                a["troops"] = 0
                a["_destroyed"] = True
                await self.m.save_army(a)
            return "CAMP_WIN"

        with mock.patch.object(self.m, "_siege_camp_battle", _win_camp):
            report = await self.m._attack_node(self.world, node, [a3])

        self.assertIn("占领", report)
        node = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(node["owner_faction"]), self.fid3)
        a2 = await self.m.get_army(int(a2["id"]))
        self.assertTrue(a2 is None or int(a2["troops"]) == 0)

    # ------------------------------------------------------------------
    # 6. 没有围城军时清理悬挂的 siege_state（城防可自修）
    # ------------------------------------------------------------------
    async def test_step_sieges_clears_orphan_state(self):
        node = await self.m.get_node(self.wid, int(self.target["id"]))
        node["siege_state"] = {"attacker_faction": self.fid2, "ticks": 7,
                               "choice": "auto", "since": 0}
        await self.m.save_node(node)
        await self.m._commit()

        await self.m._step_sieges(await self.m.get_world())

        node = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(node.get("siege_state") or {}, {})

    # ------------------------------------------------------------------
    # 7. 原攻方全灭：现存与守方交战的势力接管围城
    # ------------------------------------------------------------------
    async def test_step_sieges_takeover_when_owner_wiped(self):
        node = await self._prepare_siege(ticks=6)
        a3 = await self._siege_army("u3", troops=600)
        await self._set_war(self.fid3, self.fid2)   # 与旧攻方敌对
        await self._set_war(self.fid3, self.fid1)   # 与守方交战
        self.assertTrue(await self.m._factions_at_war(self.fid3, self.fid2))
        side = await self.m._siege_side_ids(
            await self.m.get_world(), node, self.fid2)
        self.assertNotIn(self.fid3, side)

        reports = await self.m._step_sieges(await self.m.get_world())

        node = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(
            int(node["siege_state"]["attacker_faction"]), self.fid3)
        self.assertTrue(any("接管围城" in r for r in reports))
        a3 = await self.m.get_army(int(a3["id"]))
        self.assertEqual(a3["state"], "siege")


if __name__ == "__main__":
    unittest.main()
