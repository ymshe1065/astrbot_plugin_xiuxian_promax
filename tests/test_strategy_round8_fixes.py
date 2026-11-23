# tests/test_strategy_round8_fixes.py
# 「仙域争霸」v4.4.1 第八轮复核修复回归（手操战役 / hex 战棋）：
#   发起人（无自有军团）不再被判观战 / 精锐小队机动在 hex 生效 /
#   倨傲按开战兵力在 hex 生效 / battle_state 下发服务端机动点 /
#   围城手操期间劝降计略不得直接易主
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


class TestStrategyRound8Fixes(unittest.IsolatedAsyncioTestCase):
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
        for uid in ("u1", "u2"):
            await self.db.create_player(self.Player(
                user_id=uid, gold=10_000_000, user_name=f"测试{uid}"))
        self.m = self.StrategyManager(self.db)
        self.m.main_cfg().setdefault("hex", {})["field_encounter_mode"] = "auto"
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        self.p1 = await self.m.enter_game(
            "u1", "甲", node_id=await self._free_node())
        self.p2 = await self.m.enter_game(
            "u2", "乙", node_id=await self._free_node())

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    async def _free_node(self) -> int:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        self.assertTrue(nodes)
        return int(nodes[0]["id"])

    async def _give(self, uid: str, **res):
        p = await self.m.get_player(uid, self.wid)
        for k, v in res.items():
            p[k] = int(v)
        await self.m.save_player(p)
        await self.m._commit()
        return p

    async def _char_of(self, uid: str):
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == "general":
                return c
        return None

    async def _mk_army(self, uid: str, node_id: int, troop_type="sword_shield",
                       troops: int = 1200):
        await self._give(uid, coin=5_000_000, grain=5_000_000)
        c = await self._char_of(uid)
        c2 = await self.m.get_char(int(c["id"]))
        c2["location"] = {"node_id": int(node_id)}
        c2["status"] = "idle"
        await self.m.save_char(c2)
        r = await self.m.create_army(uid, int(c["id"]), [], int(node_id),
                                    troop_type, troops)
        return await self.m.get_army(r["army_id"])

    async def _battle_setup(self, troop_type="sword_shield"):
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        home, adj = next((n, x) for n in nodes.values() if n["type"] != "ruin"
                         for x in n["adj"] if nodes[x]["type"] != "ruin")
        home["owner_faction"] = int(self.p1["faction_id"])
        await self.m.save_node(home)
        target = nodes[adj]
        target["owner_faction"] = int(self.p2["faction_id"])
        target["npc_garrison"] = {"troops": 600, "morale": 70}
        await self.m.save_node(target)
        await self.m._commit()
        army = await self._mk_army("u1", int(home["id"]), troop_type)
        atk_sim = await self.m._army_sim(army)
        gar_sim = self.m._garrison_sim(target)
        world = await self.m.get_world()
        battle = await self.m._create_manual_battle(
            world, target, [atk_sim], [gar_sim] if gar_sim else [],
            [army], [], "field", "field", 300, 0, "u1")
        return world, target, army, battle

    # ------------------------------------------------------------------
    # 修复项回归
    # ------------------------------------------------------------------
    async def test_initiator_without_army_is_participant(self):
        """发起人代势力介入（无自有军团）不得按观战处理：可看到实时战场。"""
        world, target, army, battle = await self._battle_setup()
        # 改由 u2（守方势力成员、无军团）作为发起人重建一场
        atk_sim = await self.m._army_sim(army)
        gar_sim = self.m._garrison_sim(target)
        b2 = await self.m._create_manual_battle(
            world, target, [atk_sim], [gar_sim] if gar_sim else [],
            [army], [], "field", "field", 300, 0, "u2")
        st = await self.m.battle_state("u2", int(b2["id"]))
        self.assertFalse(st["spectator"])
        self.assertTrue(st["units"])
        self.assertEqual(st["my_uids"], [])

    async def test_small_army_move_bonus_in_hex(self):
        """军事 L4 精锐小队机动 +1 必须带进 hex 单位。"""
        H = self.H
        s = H.new_session(
            {"size": [13, 9],
             "terrain": {f"{q},{r}": H.T_PLAIN for q in range(13) for r in range(9)},
             "deploy_a": [[q, r] for q in range(3) for r in range(9)],
             "deploy_b": [[q, r] for q in range(10, 13) for r in range(9)]},
            "field", "seed")
        u = H.add_unit(s, {"id": 1, "faction_id": 1,
                           "troop_type": "sword_shield", "troops": 500,
                           "morale": 100, "veteran": 0, "leader": None,
                           "siege_engine": False, "move_bonus": 1}, "a")
        u["pos"] = [1, 4]
        self.assertEqual(H.move_points(u, s), 4.0)
        u2 = H.add_unit(s, {"id": 2, "faction_id": 1,
                            "troop_type": "sword_shield", "troops": 500,
                            "morale": 100, "veteran": 0, "leader": None,
                            "siege_engine": False}, "b")
        self.assertEqual(H.move_points(u2, s), 3.0)

    async def test_understrength_personality_in_hex(self):
        """倨傲（兵力 <30% 攻防 -8%）必须经 _sim_view 生效。"""
        H, R = self.H, self.R
        s = H.new_session(
            {"size": [13, 9],
             "terrain": {f"{q},{r}": H.T_PLAIN for q in range(13) for r in range(9)},
             "deploy_a": [[q, r] for q in range(3) for r in range(9)],
             "deploy_b": [[q, r] for q in range(10, 13) for r in range(9)]},
            "field", "seed")
        leader = {"stats": {"valor": 50, "leadership": 50},
                  "personality": {"negative": "arrogant"}}
        u = H.add_unit(s, {"id": 1, "faction_id": 1,
                           "troop_type": "sword_shield", "troops": 1000,
                           "morale": 100, "veteran": 0, "leader": leader,
                           "siege_engine": False}, "a")
        u["pos"] = [1, 4]
        full = R.attack_power(H._sim_view(s, u))
        u["troops"] = 100  # <30%
        view = H._sim_view(s, u)
        self.assertEqual(view["start_troops"], 1000)
        weak = R.attack_power(view)
        # 兵力变化本身也影响战力，这里直接校验乘区：无 start_troops 时不会吃到 -8%
        no_baseline = R.attack_power({k: v for k, v in view.items()
                                      if k != "start_troops"})
        self.assertAlmostEqual(weak / no_baseline, 0.92, places=3)
        self.assertGreater(full, weak)

    async def test_battle_state_move_points_matches_engine(self):
        """battle_state 必须下发服务端机动点（前端可达格与服务端一致）。"""
        H = self.H
        world, target, army, battle = await self._battle_setup("heavy_armor")
        st = await self.m.battle_state("u1", int(battle["id"]))
        b = await self.m.get_battle(int(battle["id"]))
        unit = H.get_unit(b["session"], st["my_uids"][0])
        expect = H.move_points(unit, b["session"])
        got = [u["move_points"] for u in st["units"] if u["mine"]]
        self.assertEqual(got, [expect])
        self.assertEqual(got, [2.0])  # 重甲 2，参数顺序错误时会误报 3.0
        # 敌方单位不下发
        enemy = [u["move_points"] for u in st["units"] if not u["mine"]]
        self.assertTrue(all(v is None for v in enemy))

    async def test_persuade_plot_blocked_during_manual_battle(self):
        """手操战役进行中（胜负未分），劝降计略不得直接易主。"""
        R = self.R
        world = await self.m.get_world()
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        home, adj = next((n, x) for n in nodes.values() if n["type"] != "ruin"
                         for x in n["adj"] if nodes[x]["type"] != "ruin")
        home["owner_faction"] = int(self.p1["faction_id"])
        await self.m.save_node(home)
        target = nodes[adj]
        target["owner_faction"] = int(self.p2["faction_id"])
        target["npc_garrison"] = {"troops": 600, "morale": 70}
        target["siege_state"] = {"ticks": 2,
                                 "attacker_faction": int(self.p1["faction_id"])}
        await self.m.save_node(target)
        await self.m._commit()
        army = await self._mk_army("u1", int(home["id"]))
        army["state"] = "siege"
        await self.m.save_army(army)
        atk_sim = await self.m._army_sim(army)
        gar_sim = self.m._garrison_sim(target)
        battle = await self.m._create_manual_battle(
            world, target, [atk_sim], [gar_sim] if gar_sim else [],
            [army], [], "siege", "siege", 90,
            int(target.get("defense", 0)), "u1")
        c = await self._char_of("u1")
        executor = await self.m.get_char(int(c["id"]))
        task = {"id": 999, "task_type": "plot_persuade_plot",
                "faction_id": int(self.p1["faction_id"]),
                "target_faction": int(self.p2["faction_id"]),
                "payout": {"plot": "persuade_plot", "target": int(target["id"])}}
        p = await self.m.get_player("u1", self.wid)
        with mock.patch.object(R, "plot_success_rate", return_value=100.0):
            out = await self.m._settle_plot_task(
                await self.m.get_world(), task, executor, p)
        self.assertTrue(out.get("success"))
        self.assertTrue(any("手操战役进行中" in t for t in out.get("texts") or []))
        node2 = await self.m.get_node(self.wid, int(target["id"]))
        self.assertEqual(int(node2["owner_faction"]), int(self.p2["faction_id"]))
        # 清理：终局战役，避免影响同用例库其他断言
        b = await self.m.get_battle(int(battle["id"]))
        b["state"] = "done"
        await self.m.update_battle_session(b)
        await self.m._commit()


if __name__ == "__main__":
    unittest.main()
