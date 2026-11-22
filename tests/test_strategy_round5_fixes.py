# tests/test_strategy_round5_fixes.py
# 「仙域争霸」v4.2.1 第五轮 BUG/冲突修复回归：
#   tick 内灵石注入不提前提交 / 补跑产出上限 / 外交提案保留原关系与好感 /
#   押运角色不被出走逻辑清空 / 任务失败保留待重试 / 释放俘虏复位落点官职 /
#   insert_char 落点 / 入盟不改写被俘角色阵营 / 断粮计数持久化 /
#   官职卸任 / 参数 TypeError 归一为 400 / 退盟回收联盟科技 /
#   副主提取国库每日累计上限 / 总开关拦截动作
import asyncio
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


R = _imp("core.strategy_rules")


class TestStrategyRound5Fixes(unittest.IsolatedAsyncioTestCase):
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

    async def _new_char(self, uid: str, **kw) -> dict:
        p = await self.m.get_player(uid, self.wid)
        c = {
            "world_id": self.wid, "faction_id": int(p["faction_id"]),
            "owner_user_id": uid, "name": kw.pop("name", f"角色{uid}"),
            "role": "general", "rarity": "common", "profession": "sword",
            "stats": {}, "traits": [], "aptitude": {}, "exp": 0, "level": 1,
            "loyalty": 80, "injury": 0, "stamina": 100, "status": "idle",
            "location": {"node_id": int((await self._owned_node(uid))["id"])},
        }
        c.update(kw)
        await self.m.insert_char(c)
        await self.m._commit()
        return await self.m.get_char(c["id"])

    # ------------------------------------------------------------------
    # 1. tick 内 _inject_gold 不得提前提交（P0）
    # ------------------------------------------------------------------
    async def test_inject_gold_defers_commit_inside_tick(self):
        calls = []
        real = self.db.update_player

        async def spy(player, commit=True):
            calls.append(bool(commit))
            return await real(player, commit=commit)

        self.m._tx_task = asyncio.current_task()
        try:
            with mock.patch.object(self.db, "update_player", side_effect=spy):
                gold, _merit = await self.m._inject_gold(self.p1, 1000)
        finally:
            self.m._tx_task = None
        self.assertEqual(gold, 1000)
        self.assertEqual(calls, [False], "tick 事务内提前 commit 会破坏整段回滚")

    # ------------------------------------------------------------------
    # 2. 补跑产出 tick 有硬上限，避免长期停机持锁数千次
    # ------------------------------------------------------------------
    async def test_catchup_output_ticks_capped(self):
        self.m.main_cfg()["max_catchup_ticks"] = 2
        self.m.main_cfg()["max_catchup_output_ticks"] = 5
        w = await self.m.get_world()
        w["last_tick_at"] = _now() - self.m.tick_seconds() * 100
        await self.m.save_world(w)
        await self.m._commit()
        repeats = []

        async def fake_tick(output_only=False, repeat=1):
            repeats.append((bool(output_only), int(repeat)))
            return {"ok": True}

        with mock.patch.object(self.m, "tick", side_effect=fake_tick):
            await self.m.run_tick_catchup()
        self.assertEqual(repeats[0], (True, 5))
        self.assertEqual(len(repeats), 3)  # 1 次产出补跑 + 2 次完整补跑

    # ------------------------------------------------------------------
    # 3. 外交提案期间保留既有关系与好感
    # ------------------------------------------------------------------
    async def test_diplomacy_proposal_keeps_relation_and_favor(self):
        fid1 = int(self.p1["faction_id"])
        fid2 = int(self.p2["faction_id"])
        f1 = await self.m.get_faction(fid1)
        f2 = await self.m.get_faction(fid2)
        f1["diplomacy"] = {str(fid2): {"rel": R.REL_WAR, "war_at": 0,
                                       "score": 42}}
        f2["diplomacy"] = {str(fid1): {"rel": R.REL_WAR, "war_at": 0,
                                       "score": 42}}
        await self.m.save_faction(f1)
        await self.m.save_faction(f2)
        await self.m._commit()

        await self.m.diplomacy_propose("u1", fid2, "peace", indemnity=0)
        f1 = await self.m.get_faction(fid1)
        entry = (f1.get("diplomacy") or {}).get(str(fid2)) or {}
        self.assertTrue(str(entry.get("rel", "")).startswith("propose_"))
        # 提案只是视图：真实关系仍是战争，不能借提案绕过停战/备战门禁
        self.assertEqual(R.relation_of(f1.get("diplomacy") or {}, fid2, _now()),
                         R.REL_WAR)

        await self.m.diplomacy_respond("u2", fid1, False)
        f1 = await self.m.get_faction(fid1)
        f2 = await self.m.get_faction(fid2)
        self.assertEqual(R.relation_of(f1.get("diplomacy") or {}, fid2, _now()),
                         R.REL_WAR)
        self.assertEqual(int((f1["diplomacy"][str(fid2)]).get("score", 0)), 42)
        self.assertEqual(int((f2["diplomacy"][str(fid1)]).get("score", 0)), 42)

    # ------------------------------------------------------------------
    # 4. 押运中的角色不被低忠诚出走逻辑清空
    # ------------------------------------------------------------------
    async def test_transfer_status_skipped_by_loyalty_defect(self):
        c = await self._new_char("u1", status="transfer", loyalty=5)
        world = await self.m.get_world()
        await self.m._step_loyalty_defect(world)
        fresh = await self.m.get_char(c["id"])
        self.assertEqual(fresh["status"], "transfer")
        self.assertEqual(int(fresh["faction_id"]), int(c["faction_id"]))

    # ------------------------------------------------------------------
    # 5. 任务结算失败保留 active，下 tick 可重试
    # ------------------------------------------------------------------
    async def test_task_settle_failure_stays_active(self):
        gen = next(c for c in await self.m.get_chars(
            owner_user_id="u1", world_id=self.wid) if c["role"] == "general")
        node = await self._owned_node("u1")
        r = await self.m.dispatch_task("u1", gen["id"], int(node["id"]),
                                       "farm", 1)
        await self.m._execute(
            "UPDATE strategy_tasks SET end_at=? WHERE id=?",
            (_now() - 10, r["task_id"]))
        world = await self.m.get_world()
        with mock.patch.object(self.m, "_pay_task",
                               mock.AsyncMock(side_effect=RuntimeError("boom"))):
            await self.m._step_settle_tasks(world)
        tasks = await self.m.get_tasks(self.wid, character_id=gen["id"],
                                       status="active")
        self.assertTrue(any(int(t["id"]) == int(r["task_id"]) for t in tasks),
                        "结算失败的任务被错误置为 done")
        fresh = await self.m.get_char(gen["id"])
        self.assertEqual(fresh["status"], "task")

    # ------------------------------------------------------------------
    # 6. 释放俘虏：落点回到己方据点、官职清空
    # ------------------------------------------------------------------
    async def test_captive_release_resets_location_and_appointment(self):
        c = await self._new_char("u1")
        enemy = await self._owned_node("u2")
        ok = await self.m._capture_char(c, int(self.p2["faction_id"]),
                                        int(enemy["id"]))
        self.assertTrue(ok)
        c = await self.m.get_char(c["id"])
        c["appointed_position"] = "taishou"
        c["appointed_node_id"] = int(enemy["id"])
        await self.m.save_char(c)
        await self.m._commit()

        await self.m._return_captive_home(c)
        fresh = await self.m.get_char(c["id"])
        own = await self._owned_node("u1")
        self.assertEqual(fresh["status"], "idle")
        self.assertEqual(int(fresh["captor_faction"]), 0)
        self.assertEqual(str(fresh.get("appointed_position") or ""), "")
        self.assertEqual(int(fresh["location"]["node_id"]), int(own["id"]))

    # ------------------------------------------------------------------
    # 7. insert_char 写入 location_json
    # ------------------------------------------------------------------
    async def test_insert_char_persists_location(self):
        node = await self._owned_node("u1")
        c = await self._new_char("u1", name="落点测试")
        self.assertEqual(int(c["location"]["node_id"]), int(node["id"]))

    # ------------------------------------------------------------------
    # 8. 入盟/崩溃迁移不改写被俘角色阵营
    # ------------------------------------------------------------------
    async def test_move_player_to_faction_skips_captive(self):
        p2 = await self.m.get_player("u2", self.wid)
        c = await self._new_char("u2", status="captive",
                                 captor_faction=int(self.p1["faction_id"]),
                                 imprisoned_node_id=int(
                                     (await self._owned_node("u1"))["id"]))
        await self.m._move_player_to_faction(
            self.world, p2, int(self.p1["faction_id"]))
        fresh = await self.m.get_char(c["id"])
        self.assertEqual(fresh["status"], "captive")
        self.assertEqual(int(fresh["faction_id"]), int(c["faction_id"]))

    # ------------------------------------------------------------------
    # 9. 断粮计数持久化（v78）
    # ------------------------------------------------------------------
    async def test_starve_ticks_persisted(self):
        army = {"world_id": self.wid,
                "faction_id": int(self.p1["faction_id"]),
                "owner_user_id": "u1", "name": "断粮军",
                "node_id": int((await self._owned_node("u1"))["id"]),
                "characters": [], "troops": 100, "grain": 0}
        aid = await self.m.insert_army(army)
        army["id"] = aid
        army["starve_ticks"] = 4
        await self.m.save_army(army)
        await self.m._commit()
        fresh = await self.m.get_army(aid)
        self.assertEqual(int(fresh.get("starve_ticks", 0)), 4)

    # ------------------------------------------------------------------
    # 10. 官职可卸任（position="")
    # ------------------------------------------------------------------
    async def test_faction_set_position_empty_unappoints(self):
        host_fid = await self._create_multi_faction()
        await self.m.faction_set_position("u1", "u2", "situ")
        await self.m.faction_set_position("u1", "u2", "")
        members = {m["user_id"]: (m.get("position") or "")
                   for m in await self.m.get_members(host_fid)}
        self.assertEqual(members["u2"], "")

    # ------------------------------------------------------------------
    # 11. 参数 null 触发 TypeError → StrategyError（REST 400）
    # ------------------------------------------------------------------
    async def test_handle_action_type_error_normalized(self):
        with self.assertRaises(self.StrategyError):
            await self.m.handle_action("u1", "send_convoy", {
                "from_node": None, "to_node": 1, "grain": 10})

    # ------------------------------------------------------------------
    # 12. 退盟回收联盟科技
    # ------------------------------------------------------------------
    async def test_alliance_tech_restored_on_quit(self):
        host_fid = await self._create_multi_faction()
        own2 = int((await self.m.get_player("u2", self.wid))["faction_id"])
        host = await self.m.get_faction(host_fid)
        host["tech"] = {"military": 3, "agri": 0, "stratagem": 0}
        await self.m.save_faction(host)
        await self.m._sync_alliance_meta(host)
        await self.m._commit()
        member = await self.m.get_faction(own2)
        self.assertEqual(R.tech_level(member.get("tech") or {}, "military"), 3)

        await self.m.faction_quit("u2")
        member = await self.m.get_faction(own2)
        self.assertLess(R.tech_level(member.get("tech") or {}, "military"), 3,
                        "退盟后仍保留盟主科技（可反复横跳白嫖）")

    # ------------------------------------------------------------------
    # 13. 副主提取国库：每日累计上限
    # ------------------------------------------------------------------
    async def test_deputy_withdraw_daily_cap(self):
        host_fid = await self._create_multi_faction()
        f = await self.m.get_faction(host_fid)
        f["treasury"] = {"coin": 30_000}
        await self.m.save_faction(f)
        await self.m._commit()
        await self.m.treasury_withdraw("u2", "coin", 9_000)
        with self.assertRaises(self.StrategyError):
            await self.m.treasury_withdraw("u2", "coin", 9_000)

    # ------------------------------------------------------------------
    # 14. 总开关关闭时动作被拦截
    # ------------------------------------------------------------------
    async def test_disabled_blocks_handle_action(self):
        self.m.main_cfg()["enabled"] = False
        try:
            with self.assertRaises(self.StrategyError):
                await self.m.handle_action("u1", "refresh_tavern", {})
        finally:
            self.m.main_cfg()["enabled"] = True

    # ------------------------------------------------------------------
    # 15. battle.js：加载失败不启动轮询
    # ------------------------------------------------------------------
    async def test_battle_js_skips_poll_on_load_failure(self):
        src = (ROOT / "webchat_room" / "static" / "strategy" / "battle.js") \
            .read_text(encoding="utf-8")
        self.assertIn("const ok = await refresh();", src)
        self.assertIn("if (!ok)", src)


if __name__ == "__main__":
    unittest.main()
