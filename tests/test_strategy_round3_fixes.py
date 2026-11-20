# tests/test_strategy_round3_fixes.py
# 「仙域争霸」v4.1.6 第三轮 BUG/冲突修复回归：
#   势力颜色 XSS / 流亡重建口径 / 联盟名册 joined_at / 势力任务类型去重 /
#   溃军二次处罚幂等 / 赎金失效校验 / 离间出走官职清理 / 行军粮耗科技
import os
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


class TestStrategyRound3Fixes(unittest.IsolatedAsyncioTestCase):
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

    async def _free_capital_node(self) -> int:
        """准备一个无主且可作为主城的据点（城池/关隘/分舵）。

        standard36 模板下城池/关隘/分舵可能全被 NPC 占据或作为首府，
        此时把一个 NPC 的非首府据点腾空，避免测试因世界布局偶发失败。
        """
        own_fid = int(self.p1["faction_id"])

        def usable(n: dict) -> bool:
            return (n["type"] in ("city", "pass", "branch")
                    and int(n["owner_faction"]) != own_fid
                    and int(n.get("protect_until", 0) or 0) <= _now()
                    and not n.get("siege_state"))

        nodes = [n for n in await self.m.get_nodes(self.wid) if usable(n)]
        nodes.sort(key=lambda n: (int(n["owner_faction"]) != 0,
                                 -int(n.get("level", 1)), int(n["id"])))
        self.assertTrue(nodes, "没有可用作主城的据点")

        target = nodes[0]
        if int(target["owner_faction"]) == 0:
            return int(target["id"])
        # 腾空 NPC 据点：优先非首府，避免留下悬挂的 capital_node_id
        rows = await self.m._fetchall(
            "SELECT id, capital_node_id FROM strategy_factions WHERE world_id=?",
            (self.wid,))
        caps = {int(r["capital_node_id"] or 0) for r in rows}
        plain = [n for n in nodes if int(n["id"]) not in caps]
        target = (plain or nodes)[0]
        target["owner_faction"] = 0
        target["npc_garrison"] = {"troops": int(
            target.get("npc_garrison", {}).get("troops", 800) or 800),
            "morale": 80}
        target["protect_until"] = 0
        target["siege_state"] = {}
        await self.m.save_node(target)
        await self.m._commit()
        return int(target["id"])

    async def _enter(self, user_id: str, nickname: str) -> dict:
        return await self.m.enter_game(user_id, nickname,
                                       node_id=await self._free_node())

    async def _owned_node(self, uid: str) -> dict:
        p = await self.m.get_player(uid, self.wid)
        for n in await self.m.get_nodes(self.wid):
            if int(n["owner_faction"]) == int(p["faction_id"]):
                return n
        self.fail(f"{uid} 没有据点")

    async def _char_of(self, uid: str, role: str = "general"):
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == role:
                return c
        return None

    async def _create_multi_faction(self) -> int:
        r = await self.m.create_faction("u1", "凌云盟")
        pid = r["petition_id"]
        await self.m.faction_cosign("u2", pid)
        r3 = await self.m.faction_cosign("u3", pid)
        return int(r3["faction_id"])

    # ------------------------------------------------------------------
    # 1. 势力颜色：非法值必须回退为安全色
    # ------------------------------------------------------------------
    async def test_norm_color_rejects_xss_payload(self):
        self.assertEqual(self.R.norm_color("#a1b2c3"), "#a1b2c3")
        self.assertEqual(self.R.norm_color("#fff"), "#fff")
        self.assertEqual(self.R.norm_color(
            'red"></span><img src=x onerror=alert(1)>', "#123456"), "#123456")

        await self._give("u1", coin=50_000)
        r = await self.m.create_faction(
            "u1", "毒色盟", 'red"></span><img src=x onerror=alert(1)>')
        pet = ((await self.m.get_world()).get("data") or {})["faction_petitions"][
            r["petition_id"]]
        self.assertRegex(pet["color"], r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
        self.assertNotIn("<", pet["color"])

    # ------------------------------------------------------------------
    # 2. 联盟名册继承真实入盟时间（避免 72h 战功观察期误杀）
    # ------------------------------------------------------------------
    async def test_alliance_roster_keeps_joined_at(self):
        host_fid = await self._create_multi_faction()
        member_fid = int(self.p2["faction_id"])
        self.assertNotEqual(member_fid, host_fid)

        old_joined = _now() - 100 * 3600
        await self.m.upsert_member(member_fid, "u2")
        await self.m._execute(
            "UPDATE strategy_members SET joined_at=? "
            "WHERE faction_id=? AND user_id=?", (old_joined, member_fid, "u2"))
        # 先移出盟主名册，模拟重新结盟
        await self.m.delete_member(host_fid, "u2")
        member = await self.m.get_faction(member_fid)
        host = await self.m.get_faction(host_fid)
        await self.m._add_faction_to_alliance(self.world, member, host)
        await self.m._commit()

        row = await self.m.get_member(host_fid, "u2")
        self.assertEqual(int(row["joined_at"]), old_joined)
        self.assertTrue(self.m._is_joined_long_enough(row, 72))

    # ------------------------------------------------------------------
    # 3. 势力任务同批不生成重复类型
    # ------------------------------------------------------------------
    async def test_faction_tasks_have_unique_types(self):
        f = await self.m.get_faction(int(self.p1["faction_id"]))
        tasks = await self.m._generate_faction_tasks(self.world, f, 3)
        await self.m._commit()
        self.assertEqual(len(tasks), 3)
        self.assertEqual(len({t["task_type"] for t in tasks}), 3)

    # ------------------------------------------------------------------
    # 4. 重建：仅流亡态可远程重建；写迁都 CD 并清匪寨标记
    # ------------------------------------------------------------------
    async def test_rebuild_requires_exiled_and_clears_bandit(self):
        f = await self.m.get_faction(int(self.p1["faction_id"]))
        # 仍有主城 → 不允许在无主节点重建
        target = await self.m.get_node(self.wid, await self._free_capital_node())
        with self.assertRaises(self.StrategyError):
            await self.m.rebuild_capital("u1", int(target["id"]))

        # 主城已陷但仍持有其他据点（capital=0, state=normal）→ 也不允许
        f["capital_node_id"] = 0
        f["faction_state"] = "normal"
        await self.m.save_faction(f)
        await self.m._commit()
        with self.assertRaises(self.StrategyError):
            await self.m.rebuild_capital("u1", int(target["id"]))

        # 流亡态 → 可重建
        f = await self.m.get_faction(int(f["id"]))
        f["faction_state"] = "exiled"
        await self.m.save_faction(f)
        await self.m._commit()
        world = await self.m.get_world()
        data = world.get("data") or {}
        data.setdefault("bandits", {})[str(target["id"])] = {
            "spawned_at": _now(), "until": _now() + 86400}
        world["data"] = data
        await self.m.save_world(world)
        await self.m._commit()
        target["lair"] = {"bandit": True, "source": "bandit_data"}
        await self.m.save_node(target)
        await self.m._commit()

        out = await self.m.rebuild_capital("u1", int(target["id"]))
        self.assertTrue(out["ok"])
        f = await self.m.get_faction(int(f["id"]))
        self.assertEqual(int(f["capital_node_id"]), int(target["id"]))
        self.assertEqual(str(f["faction_state"]), "normal")
        raw = await self.m._fetchone(
            "SELECT last_capital_move_at FROM strategy_factions WHERE id=?",
            (int(f["id"]),))
        self.assertGreater(int(raw["last_capital_move_at"]), 0)
        world = await self.m.get_world()
        self.assertNotIn(str(target["id"]),
                         (world.get("data") or {}).get("bandits") or {})
        node = await self.m.get_node(self.wid, int(target["id"]))
        self.assertFalse((node.get("lair") or {}).get("bandit"))

    # ------------------------------------------------------------------
    # 5. 城破时已进入重整期的溃军不再二次折损
    # ------------------------------------------------------------------
    async def test_capture_node_skips_reorganizing_army_penalty(self):
        node = await self._owned_node("u1")
        fid1 = int(self.p1["faction_id"])
        fid2 = int(self.p2["faction_id"])
        army = {"world_id": self.wid, "faction_id": fid1,
                "owner_user_id": "u1", "name": "残军", "node_id": int(node["id"]),
                "characters": [], "troops": 1000, "grain": 1000,
                "morale": 80, "state": "idle"}
        aid = await self.m.insert_army(army)
        army["id"] = aid
        army["reorganize_until_tick"] = int(self.world["tick_no"]) + 2
        await self.m.save_army(army)
        await self.m._commit()

        world = await self.m.get_world()
        node = await self.m.get_node(self.wid, int(node["id"]))
        await self.m._capture_node(world, node, fid2)
        await self.m._commit()

        after = await self.m.get_army(aid)
        self.assertEqual(int(after["troops"]), 1000,
                         "重整期溃军被二次 30% 折损")

    # ------------------------------------------------------------------
    # 6. 赎金：收款方与当前关押方不一致时拒绝支付
    # ------------------------------------------------------------------
    async def test_ransom_rejects_stale_captor(self):
        node = await self._owned_node("u1")
        victim = await self._char_of("u2")
        self.assertIsNotNone(victim)
        fid1 = int(self.p1["faction_id"])
        fid3 = int(self.p3["faction_id"])
        self.assertTrue(await self.m._capture_char(
            victim, fid1, int(node["id"])))
        await self.m.set_ransom("u1", int(victim["id"]), 1000)

        # 俘虏被第三方重新擒获：旧赎金收款方失效
        victim = await self.m.get_char(int(victim["id"]))
        victim["captor_faction"] = fid3
        await self.m.save_char(victim)
        await self.m._commit()

        with self.assertRaises(self.StrategyError):
            await self.m.pay_ransom("u2", int(victim["id"]))
        # 玩家铜钱未被扣
        p2 = await self.m.get_player("u2", self.wid)
        self.assertEqual(int(p2["coin"]), 500_000)

    # ------------------------------------------------------------------
    # 7. 离间出走后官职/关押标记必须清理
    # ------------------------------------------------------------------
    async def test_alienate_flight_clears_appointment(self):
        target_node = await self._owned_node("u2")
        target = await self._char_of("u2")
        self.assertIsNotNone(target)
        target["loyalty"] = 5
        target["appointed_position"] = "taishou"
        target["appointed_node_id"] = int(target_node["id"])
        await self.m.save_char(target)
        await self.m._commit()
        c = await self._char_of("u1")
        self.assertIsNotNone(c)
        p = await self.m.get_player("u1", self.wid)
        world = await self.m.get_world()
        task = {"id": 8801, "task_type": "plot_alienate",
                "faction_id": int(self.p1["faction_id"]),
                "target_faction": int(self.p2["faction_id"]),
                "payout": {"plot": "alienate",
                           "target": int(target["id"]), "ticks": 2}}
        with mock.patch.object(self.R, "plot_success_rate", return_value=100.0):
            out = await self.m._settle_plot_task(world, task, c, p)
        self.assertTrue(out["success"])
        target = await self.m.get_char(int(target["id"]))
        self.assertEqual(str(target["status"]), "wander")
        self.assertEqual(int(target["faction_id"]), 0)
        self.assertEqual(str(target.get("appointed_position") or ""), "")
        self.assertEqual(int(target.get("appointed_node_id") or 0), 0)

    # ------------------------------------------------------------------
    # 8. 行军粮耗叠加农政科技 grain_cost_mult
    # ------------------------------------------------------------------
    async def test_march_upkeep_applies_tech_multiplier(self):
        f = await self.m.get_faction(int(self.p1["faction_id"]))
        f["tech"] = {"agri": 2}
        await self.m.save_faction(f)
        await self.m._commit()
        node = await self._owned_node("u1")
        adj = int((node.get("adj") or [0])[0])
        self.assertTrue(adj, "起始据点没有相邻节点")
        army = {"world_id": self.wid, "faction_id": int(f["id"]),
                "owner_user_id": "u1", "name": "行军", "node_id": int(node["id"]),
                "characters": [], "troops": 1000, "grain": 10_000,
                "morale": 100, "state": "march", "path": [adj]}
        aid = await self.m.insert_army(army)
        army["id"] = aid
        army["path"] = [adj]
        army["edge_pos"] = 99.0
        await self.m.save_army(army)
        await self.m._commit()

        world = await self.m.get_world()
        with mock.patch.object(self.R, "grain_upkeep", return_value=1000):
            await self.m._step_march(world)
        after = await self.m.get_army(aid)
        # 1000 × 农政 L2 0.9 = 900
        self.assertEqual(int(after["grain"]), 9_100)


if __name__ == "__main__":
    unittest.main()
