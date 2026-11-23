# tests/test_strategy_round9_fixes.py
# 「仙域争霸」v4.4.1 第九轮复核修复回归（计略全链路 / NPC 经济）：
#   离间目标状态白名单写错导致随军/任务/研究中的敌将必然落空 /
#   计略延迟生效期间据点易主仍打到自己人 / NPC 国库产出漏乘计略减益 /
#   NPC 军团覆没残粮未退库 / 计略结算结果不回推给玩家。
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


class TestStrategyRound9Fixes(unittest.IsolatedAsyncioTestCase):
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
        for uid in ("u1", "u2"):
            await self.db.create_player(self.Player(
                user_id=uid, gold=10_000_000, user_name=f"测试{uid}"))
        self.m = self.StrategyManager(self.db)
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        self.p1 = await self.m.enter_game("u1", "甲", node_id=await self._free_node())
        self.p2 = await self.m.enter_game("u2", "乙", node_id=await self._free_node())

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

    async def _enemy_node(self) -> dict:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        node = nodes[-1]
        node["owner_faction"] = int(self.p2["faction_id"])
        node["npc_garrison"] = {"troops": 100, "morale": 80}
        await self.m.save_node(node)
        await self.m._commit()
        return await self.m.get_node(self.wid, int(node["id"]))

    async def _strategist(self) -> dict:
        c = (await self.m.get_chars(owner_user_id="u1", world_id=self.wid))[0]
        c = await self.m.get_char(int(c["id"]))
        c["role"] = self.R.ROLE_STRATEGIST
        c["status"] = "idle"
        c["injury"] = 0
        c["stamina"] = 100
        c["stats"] = {**(c.get("stats") or {}), "wisdom": 100}
        await self.m.save_char(c)
        await self.m._commit()
        return c

    async def _mk_char(self, status: str, faction_id: int, name: str) -> dict:
        cid = await self.m.insert_char({
            "world_id": self.wid, "faction_id": int(faction_id),
            "owner_user_id": "", "name": name, "role": "general",
            "status": status, "loyalty": 80, "location": {"node_id": 0},
            "stats": {"leadership": 60, "wisdom": 50},
        })
        c = await self.m.get_char(int(cid))
        c["status"] = status
        await self.m.save_char(c)
        await self.m._commit()
        return c

    def _task(self, tid: int, plot: str, target: int, fid: int,
              target_fid: int) -> dict:
        return {"id": tid, "task_type": f"plot_{plot}",
                "faction_id": int(fid), "target_faction": int(target_fid),
                "payout": {"plot": plot, "target": int(target)}}

    # ------------------------------------------------------------------
    # 修复项回归
    # ------------------------------------------------------------------
    async def test_alienate_targets_army_task_research_chars(self):
        """角色状态是 idle/task/army/research；随军/任务/研究中的敌将可离间。"""
        ex = await self._strategist()
        cases = [("army", 1201, "随军甲"), ("task", 1202, "任务乙"),
                 ("research", 1203, "研究丙")]
        with mock.patch.object(self.R, "plot_success_rate", return_value=100.0), \
                mock.patch.object(self.R, "plot_expose", return_value=False):
            for status, tid, name in cases:
                target = await self._mk_char(
                    status, int(self.p2["faction_id"]), name)
                out = await self.m._settle_plot_task(
                    await self.m.get_world(),
                    self._task(tid, "alienate", int(target["id"]),
                               int(self.p1["faction_id"]),
                               int(self.p2["faction_id"])),
                    ex, None)
                self.assertTrue(out["success"], f"{status}: {out['texts']}")
                fresh = await self.m.get_char(int(target["id"]))
                self.assertLess(int(fresh["loyalty"]), 80, status)

    async def test_plot_aborts_when_target_node_captured_by_self(self):
        """计略延迟期间据点易主为友方：不得再降自家民心/产出。"""
        ex = await self._strategist()
        node = await self._enemy_node()
        node["owner_faction"] = int(self.p1["faction_id"])
        node["morale"] = 80
        await self.m.save_node(node)
        await self.m._commit()
        with mock.patch.object(self.R, "plot_success_rate", return_value=100.0), \
                mock.patch.object(self.R, "plot_expose", return_value=False):
            out = await self.m._settle_plot_task(
                await self.m.get_world(),
                self._task(1210, "rumor", int(node["id"]),
                           int(self.p1["faction_id"]),
                           int(self.p2["faction_id"])),
                ex, None)
        self.assertFalse(out["success"])
        self.assertTrue(any("已归我方" in t for t in out["texts"]))
        fresh = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(fresh["morale"]), 80)
        self.assertEqual(self.m.plot_output_mult(fresh), 1.0)

    async def test_arson_does_not_burn_own_grain_after_capture(self):
        ex = await self._strategist()
        node = await self._enemy_node()
        node["owner_faction"] = int(self.p1["faction_id"])
        await self.m.save_node(node)
        p = await self.m.get_player("u1", self.wid)
        p["grain"] = 50_000
        await self.m.save_player(p)
        await self.m._commit()
        with mock.patch.object(self.R, "plot_success_rate", return_value=100.0), \
                mock.patch.object(self.R, "plot_expose", return_value=False):
            out = await self.m._settle_plot_task(
                await self.m.get_world(),
                self._task(1211, "arson", int(node["id"]),
                           int(self.p1["faction_id"]),
                           int(self.p2["faction_id"])),
                ex, None)
        self.assertFalse(out["success"])
        fresh = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(fresh["grain"]), 50_000)

    async def test_npc_output_applies_plot_debuff(self):
        """谣言 -20% 产出必须作用于 NPC 国库（此前 NPC 分支漏乘）。"""
        npc_fid, node = None, None
        for n in await self.m.get_nodes(self.wid):
            f = await self.m.get_faction(int(n["owner_faction"] or 0))
            if f and f.get("kind") == "npc" and n["type"] != "ruin":
                npc_fid, node = int(f["id"]), n
                break
        self.assertIsNotNone(npc_fid)
        # 只留一个据点，便于用国库增量核对乘区
        for n in await self.m.get_nodes(self.wid):
            if int(n["owner_faction"] or 0) == npc_fid and n["id"] != node["id"]:
                n["owner_faction"] = 0
                await self.m.save_node(n)
        node["morale"] = 80
        await self.m.save_node(node)
        f = await self.m.get_faction(npc_fid)
        f["treasury"] = {"coin": 0, "grain": 0, "iron": 0}
        await self.m.save_faction(f)
        await self.m._commit()
        await self.m._step_distribute_output(await self.m.get_world(), False)
        base = int((await self.m.get_faction(npc_fid))["treasury"]["coin"])
        self.assertGreater(base, 0)

        f = await self.m.get_faction(npc_fid)
        f["treasury"]["coin"] = 0
        await self.m.save_faction(f)
        ex = await self._strategist()
        with mock.patch.object(self.R, "plot_success_rate", return_value=100.0), \
                mock.patch.object(self.R, "plot_expose", return_value=False):
            out = await self.m._settle_plot_task(
                await self.m.get_world(),
                self._task(1212, "rumor", int(node["id"]),
                           int(self.p1["faction_id"]), npc_fid),
                ex, None)
        self.assertTrue(out["success"], out["texts"])
        nd = await self.m.get_node(self.wid, int(node["id"]))
        nd["morale"] = 80  # 隔离民心乘区，只观察产出减益
        await self.m.save_node(nd)
        await self.m._commit()
        await self.m._step_distribute_output(await self.m.get_world(), False)
        after = int((await self.m.get_faction(npc_fid))["treasury"]["coin"])
        self.assertAlmostEqual(after / float(base), 0.8, delta=0.03)

    async def test_npc_destroyed_army_refunds_residual_grain(self):
        rows = await self.m._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "AND alive=1", (self.wid,))
        fid = int(rows[0]["id"])
        f = await self.m.get_faction(fid)
        f["treasury"] = {"coin": 0, "grain": 0, "iron": 0}
        await self.m.save_faction(f)
        node = [n for n in await self.m.get_nodes(self.wid)
                if int(n["owner_faction"]) == fid][0]
        army = {"world_id": self.wid, "faction_id": fid, "owner_user_id": "",
                "name": "测试军", "node_id": int(node["id"]), "characters": [],
                "troop_type": "sword_shield", "veteran": 0, "troops": 100,
                "grain": 777, "morale": 50, "state": "idle"}
        await self.m.insert_army(army)
        result = {"winner": "defender",
                  "attackers": [{"id": int(army["id"]), "troops_after": 0,
                                 "morale": 0, "routed": False}],
                  "defenders": [], "atk_losses": 100, "dfn_losses": 0}
        await self.m._apply_battle_aftermath(
            await self.m.get_world(), node, [army], [], result)
        await self.m._commit()
        fresh = await self.m.get_faction(fid)
        self.assertEqual(int(fresh["treasury"]["grain"]), 777)
        self.assertIsNone(await self.m.get_army(int(army["id"])))

    async def test_plot_result_notified_to_executor_owner(self):
        """计略到期结果（成功/落空/败露）必须回推给玩家。"""
        ex = await self._strategist()
        node = await self._enemy_node()
        ex2 = await self.m.get_char(int(ex["id"]))
        ex2["status"] = "task"
        await self.m.save_char(ex2)
        tid = await self.m._execute("""
            INSERT INTO strategy_tasks (world_id, faction_id, character_id, node_id,
                task_type, start_at, end_at, payout_json, status, target_faction)
            VALUES (?,?,?,?,?,?,?,?,'active',?)
        """, (self.wid, int(self.p1["faction_id"]), int(ex["id"]),
              int(node["id"]), "plot_rumor", 1, 2,
              '{"plot":"rumor","target":%d,"ticks":2}' % int(node["id"]),
              int(self.p2["faction_id"])))
        await self.m._commit()
        got = []

        async def _notifier(uid, kind, payload):
            got.append((uid, kind, payload))

        self.m.set_notifier(_notifier)
        with mock.patch.object(self.R, "plot_success_rate", return_value=100.0), \
                mock.patch.object(self.R, "plot_expose", return_value=False):
            await self.m._step_settle_tasks(await self.m.get_world())
        self.assertTrue(got, "计略结算没有任何玩家通知")
        uid, kind, payload = got[-1]
        self.assertEqual(uid, "u1")
        self.assertEqual(kind, "plot_result")
        self.assertIn("谣言", payload["text"])
        row = await self.m._fetchone(
            "SELECT status FROM strategy_tasks WHERE id=?", (tid,))
        self.assertEqual(row["status"], "done")


if __name__ == "__main__":
    unittest.main()
