# tests/test_strategy_faction_gaps.py
# 「仙域争霸」v4.1.0 势力缺口补全（Agent C）：
# 势力任务 / 动员令 / 势力事件 / 科技指派角色 / 迁都 / 重建合力 /
# 入盟观察期（官职·战功）/ 多开战功 ×0 / 低忠诚出走
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


class TestStrategyFactionGaps(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.migration = migration
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
        for uid in ("u1", "u2", "u3", "u4"):
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

    async def _enter(self, user_id: str, nickname: str, referral: str = "") -> dict:
        return await self.m.enter_game(user_id, nickname, referral=referral,
                                       node_id=await self._free_node())

    async def _char_of(self, uid: str, role: str = "general"):
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == role:
                return c
        return None

    async def _create_multi_faction(self) -> int:
        """u1/u2/u3 会签建多人势力，返回盟主势力 id。"""
        r = await self.m.create_faction("u1", "凌云盟")
        pid = r["petition_id"]
        await self.m.faction_cosign("u2", pid)
        r3 = await self.m.faction_cosign("u3", pid)
        return int(r3["faction_id"])

    async def _world(self) -> dict:
        return await self.m.get_world()

    async def _set_joined(self, fid: int, uid: str, at: int):
        await self.m._execute(
            "UPDATE strategy_members SET joined_at=? WHERE faction_id=? AND user_id=?",
            (int(at), int(fid), str(uid)))
        await self.m._commit()

    async def _add_owned_node(self, fid: int, *, city: bool = True) -> dict:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        node = next((n for n in nodes if n["type"] in ("city", "pass", "branch")),
                    nodes[0])
        if node["type"] not in ("city", "pass", "branch"):
            node["type"] = "city"
        node["owner_faction"] = int(fid)
        await self.m.save_node(node)
        await self.m._commit()
        return node

    # ------------------------------------------------------------------
    # 1. 势力任务：刷新 / 进度 / 领取
    # ------------------------------------------------------------------
    async def test_faction_task_refresh_progress_and_claim(self):
        fid = await self._create_multi_faction()
        r = await self.m.faction_task_refresh("u1")
        self.assertTrue(r["ok"])
        slots = int((self.m.main_cfg().get("governance") or {})
                    .get("faction_task_slots", 3))
        tasks = await self.m.get_faction_tasks(fid, "open")
        self.assertEqual(len(tasks), slots)
        for t in tasks:
            self.assertEqual(t["status"], "open")
            self.assertGreater(int(t["target"]), 0)
            self.assertGreater(int(t["end_at"]), _now())

        # 进度回填（B 在 tick/捐献/战斗事件里调用）；联盟成员势力 id 自动上溯到盟主
        task = tasks[0]
        member_fid = int(self.p2["faction_id"])
        self.assertNotEqual(member_fid, fid)
        n = await self.m._faction_task_progress(member_fid, task["task_type"],
                                                int(task["target"]) + 5)
        self.assertGreaterEqual(n, 1)
        task = (await self.m.get_faction_tasks(fid, "open"))[0]
        self.assertEqual(int(task["progress"]), int(task["target"]))  # 上限截断

        merit_before = int((await self.m.get_player("u1", self.wid))["merit"])
        res = await self.m.faction_task_claim("u1", int(task["id"]))
        self.assertTrue(res["ok"])
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p["merit"]), merit_before + int(res["reward"]["merit"]))
        claimed = await self.m._fetchone(
            "SELECT status, assigned_user FROM strategy_faction_tasks WHERE id=?",
            (int(task["id"]),))
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["assigned_user"], "u1")
        # 重复领取被拒
        with self.assertRaises(self.StrategyError):
            await self.m.faction_task_claim("u1", int(task["id"]))

    async def test_faction_task_title_migration_v86(self):
        """v86 迁移：「共襄国库」旧任务更名「共襄重建」，其他任务不受影响。"""
        fid = await self._create_multi_faction()
        now = _now()
        old_id = await self.m._execute(
            "INSERT INTO strategy_faction_tasks (world_id, faction_id, task_type, "
            "title, progress, target, reward_json, status, start_at, end_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.wid, fid, "donate", "共襄国库", 2500, 20000,
             '{"merit": 40}', "open", now, now + 86400))
        other_id = await self.m._execute(
            "INSERT INTO strategy_faction_tasks (world_id, faction_id, task_type, "
            "title, progress, target, reward_json, status, start_at, end_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.wid, fid, "conquer", "开疆拓土", 0, 1,
             '{"merit": 80}', "open", now, now + 86400))
        await self.m._commit()

        await self.migration._ensure_strategy_v86(self.conn)
        await self.conn.commit()
        tasks = {int(t["id"]): t for t in
                 await self.m.get_faction_tasks(fid)}
        self.assertEqual(tasks[int(old_id)]["title"], "共襄重建")
        self.assertEqual(int(tasks[int(old_id)]["progress"]), 2500)
        self.assertEqual(tasks[int(other_id)]["title"], "开疆拓土")

        # 可重放：再次执行不会改动已更名或无关任务
        await self.migration._ensure_strategy_v86(self.conn)
        await self.conn.commit()
        again = await self.m._fetchone(
            "SELECT title FROM strategy_faction_tasks WHERE id=?", (int(old_id),))
        self.assertEqual(again["title"], "共襄重建")

    async def test_faction_task_refresh_cooldown_and_auto_refill(self):
        fid = await self._create_multi_faction()
        await self.m.faction_task_refresh("u1")
        # 清掉未完成任务后仍在 CD 内 → 报错
        await self.m._execute(
            "DELETE FROM strategy_faction_tasks WHERE faction_id=?", (fid,))
        await self.m._commit()
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.faction_task_refresh("u1")
        self.assertIn("冷却", str(cm.exception))

        # 把 CD 拨回过去，tick 自动补刷新
        world = await self._world()
        data = world.get("data") or {}
        data.setdefault("faction_task_refresh", {})[str(fid)] = _now() - 13 * 3600
        world["data"] = data
        await self.m.save_world(world)
        await self.m._commit()
        await self.m._step_faction_tasks(world)
        slots = int((self.m.main_cfg().get("governance") or {})
                    .get("faction_task_slots", 3))
        self.assertEqual(len(await self.m.get_faction_tasks(fid, "open")), slots)

        # 到期任务置 expired
        await self.m._execute(
            "UPDATE strategy_faction_tasks SET end_at=? WHERE faction_id=?",
            (_now() - 1, fid))
        world = await self._world()
        await self.m._step_faction_tasks(world)
        rows = await self.m.get_faction_tasks(fid)
        self.assertTrue(rows)
        self.assertTrue(all(t["status"] == "expired" for t in rows))

    # ------------------------------------------------------------------
    # 2. 动员令：buff 生效 + 冷却 + 过期
    # ------------------------------------------------------------------
    async def test_mobilization_buff_and_cooldown(self):
        fid = await self._create_multi_faction()
        await self._give("u1", merit=1000)
        r = await self.m.mobilization_issue("u1", "march")
        self.assertTrue(r["ok"])
        self.assertGreater(int(r["ends_at"]), _now())
        self.assertAlmostEqual(await self.m._faction_buff(fid, "grain_cost_mult"),
                               0.7, places=3)
        # 战功已扣
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p["merit"]), 1000 - 60)
        # 冷却内不可再次发布
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.mobilization_issue("u1", "defend")
        self.assertIn("冷却", str(cm.exception))
        # 到期后 buff 失效
        await self.m._execute(
            "UPDATE strategy_mobilizations SET end_at=? WHERE faction_id=?",
            (_now() - 1, fid))
        world = await self._world()
        await self.m._step_mobilizations(world)
        self.assertEqual(await self.m._faction_buff(fid, "grain_cost_mult"), 1.0)
        row = await self.m._fetchone(
            "SELECT status FROM strategy_mobilizations WHERE faction_id=? "
            "ORDER BY id DESC LIMIT 1", (fid,))
        self.assertEqual(row["status"], "expired")
        # 非法类型
        with self.assertRaises(self.StrategyError):
            await self.m.mobilization_issue("u1", "nonsense")

    # ------------------------------------------------------------------
    # 3. 迁都：扣势力主钱包 + 冷却
    # ------------------------------------------------------------------
    async def test_capital_relocate_cost_and_cooldown(self):
        fid = int(self.p1["faction_id"])
        f = await self.m.get_faction(fid)
        old_capital = int(f["capital_node_id"])
        await self._give("u1", coin=400_000)
        target = await self._add_owned_node(fid)
        r = await self.m.capital_relocate("u1", int(target["id"]))
        self.assertTrue(r["ok"])
        f = await self.m.get_faction(fid)
        self.assertEqual(int(f["capital_node_id"]), int(target["id"]))
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1["coin"]), 100_000)
        self.assertGreater(int(r["cooldown_until"]), _now())
        # 冷却内再迁（回到旧都）被拒
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.capital_relocate("u1", old_capital)
        self.assertIn("冷却", str(cm.exception))
        # 非主城类型被拒
        node = await self._add_owned_node(fid)
        node["type"] = "mine"
        await self.m.save_node(node)
        with self.assertRaises(self.StrategyError):
            await self.m.capital_relocate("u1", int(node["id"]))

    # ------------------------------------------------------------------
    # 4. 重建合力：捐款 + 重建消耗国库/合力并清零
    # ------------------------------------------------------------------
    async def test_rebuild_donate_and_fund_consumed(self):
        fid = int(self.p1["faction_id"])
        # v4.1.0：合力资金以主玩法「灵石」计价（不再扣策略铜钱）
        r = await self.m.rebuild_donate("u1", 100_000)
        self.assertTrue(r["ok"])
        self.assertEqual(int(r["fund"]), 100_000)
        main_p = await self.db.get_player_by_id("u1")
        self.assertEqual(int(main_p.gold), 10_000_000 - 100_000)
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p["coin"]), 500_000)
        row = await self.m._fetchone(
            "SELECT rebuild_fund FROM strategy_factions WHERE id=?", (fid,))
        self.assertEqual(int(row["rebuild_fund"]), 100_000)
        # 非法数额
        with self.assertRaises(self.StrategyError):
            await self.m.rebuild_donate("u1", 0)
        with self.assertRaises(self.StrategyError):
            await self.m.rebuild_donate("u1", 100_000_000)

        # 合力（灵石）抵掉大部分，剩余由发起人主玩法灵石补足（玩家势力无国库）
        await self.m.rebuild_donate("u1", 2_800_000)
        target = await self._add_owned_node(fid)
        res = await self.m.rebuild_capital("u1", int(target["id"]))
        self.assertTrue(res["ok"])
        self.assertIn("合力", res["msg"])
        row = await self.m._fetchone(
            "SELECT rebuild_fund FROM strategy_factions WHERE id=?", (fid,))
        self.assertEqual(int(row["rebuild_fund"]), 0)
        f = await self.m.get_faction(fid)
        self.assertEqual(int(f["capital_node_id"]), int(target["id"]))

    # ------------------------------------------------------------------
    # 5. 科技：指派角色占用 → 完成后归队给经验
    # ------------------------------------------------------------------
    async def test_tech_research_assigns_and_releases_char(self):
        fid = int(self.p1["faction_id"])
        f = await self.m.get_faction(fid)
        await self._give("u1", coin=100_000, iron=100_000)
        c = await self._char_of("u1", "general")
        exp_before = int(c.get("exp", 0))
        r = await self.m.tech_research("u1", "military", char_id=int(c["id"]))
        self.assertTrue(r["ok"])
        self.assertIn(c["name"], r["msg"])
        c = await self.m.get_char(int(c["id"]))
        self.assertEqual(c["status"], "research")
        f = await self.m.get_faction(fid)
        pend = (f.get("tech") or {}).get("pending") or {}
        self.assertEqual(int(pend.get("char_id", 0)), int(c["id"]))

        # 到期：等级 +1，角色 idle 且经验增加
        tech = f.get("tech") or {}
        tech["pending"]["finish_at"] = _now() - 1
        f["tech"] = tech
        await self.m.save_faction(f)
        await self.m._commit()
        world = await self._world()
        await self.m._step_tech(world)
        f = await self.m.get_faction(fid)
        self.assertEqual(int((f.get("tech") or {}).get("military", 0)), 1)
        self.assertFalse((f.get("tech") or {}).get("pending"))
        c = await self.m.get_char(int(c["id"]))
        self.assertEqual(c["status"], "idle")
        self.assertGreater(int(c.get("exp", 0)), exp_before)

    async def test_tech_research_l2_exact_cost_and_insufficient_detail(self):
        """L2 费用口径：国库刚好 1000/1000 必须可研究，不足时错误须带当前余额。"""
        fid = int(self.p1["faction_id"])
        f = await self.m.get_faction(fid)
        f["tech"] = {"military": 1}
        await self.m.save_faction(f)
        await self.m._commit()
        await self._give("u1", coin=1000, iron=999)
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.tech_research("u1", "military")
        msg = str(cm.exception)
        self.assertIn("研究 L2", msg)
        self.assertIn("需铜钱 1000 / 精铁 1000", msg)
        self.assertIn("当前铜钱 1000 / 精铁 999", msg)
        # 失败不扣费、不占用角色
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1["coin"]), 1000)
        self.assertEqual(int(p1["iron"]), 999)
        f = await self.m.get_faction(fid)
        self.assertFalse((f.get("tech") or {}).get("pending"))
        # 刚好够：成功并扣到 0
        await self._give("u1", coin=1000, iron=1000)
        r = await self.m.tech_research("u1", "military")
        self.assertTrue(r["ok"])
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1["coin"]), 0)
        self.assertEqual(int(p1["iron"]), 0)

    async def test_tech_research_requires_idle_char(self):
        fid = int(self.p1["faction_id"])
        f = await self.m.get_faction(fid)
        await self._give("u1", coin=100_000, iron=100_000)
        for c in await self.m.get_chars(faction_id=fid, world_id=self.wid):
            c["status"] = "task"
            await self.m.save_char(c)
        await self.m._commit()
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.tech_research("u1", "agri")
        self.assertIn("空闲角色", str(cm.exception))

    # ------------------------------------------------------------------
    # 6. 安全：入盟观察期限制官职 / 战功
    # ------------------------------------------------------------------
    async def test_position_requires_join_hours(self):
        fid = await self._create_multi_faction()
        # 元勋（u1/u2/u3）入盟 48h，u4 刚入盟
        for uid in ("u1", "u2", "u3"):
            await self._set_joined(fid, uid, _now() - 48 * 3600)
        await self.m.upsert_member(fid, "u4", joined_at=_now())
        await self.m._commit()
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.faction_set_position("u1", "u4", "situ")
        self.assertIn("观察期", str(cm.exception))
        # 元老不受限
        r = await self.m.faction_set_position("u1", "u2", "situ")
        self.assertTrue(r["ok"])
        # 角色级官职同理：新人的角色不可任官
        new_char = await self._char_of("u1", "general")
        await self.m._execute(
            "UPDATE strategy_characters SET faction_id=?, owner_user_id='u4' "
            "WHERE id=?", (fid, int(new_char["id"])))
        await self.m._commit()
        await self._set_joined(fid, "u4", _now())
        with self.assertRaises(self.StrategyError):
            await self.m.faction_appoint_char("u1", int(new_char["id"]),
                                              "zhonglang")

    async def test_merit_gate_join_hours_and_multi_open(self):
        fid = await self._create_multi_faction()
        # 入盟不足 72h → 战功 ×0（u2 为副主，非主公）
        self.assertEqual(await self.m._settle_merit("u2", 100, "test"), 0)
        # 满 72h → 正常发放
        await self._set_joined(fid, "u2", _now() - 80 * 3600)
        self.assertEqual(await self.m._settle_merit("u2", 100, "test"), 100)
        # 24h 内转投两处势力 → 多开 ×0，并写审计
        for i, other in enumerate((fid, fid + 999)):
            await self.m._execute(
                "INSERT INTO strategy_audit (world_id, user_id, action, "
                "detail_json, created_at) VALUES (?,?,?,?,?)",
                (self.wid, "u2", "faction_join",
                 json.dumps({"faction_id": int(other)}), _now() - i * 60))
        await self.m._commit()
        self.assertEqual(await self.m._settle_merit("u2", 100, "test"), 0)
        row = await self.m._fetchone(
            "SELECT detail_json FROM strategy_audit WHERE user_id='u2' AND "
            "action='merit_zero' ORDER BY id DESC LIMIT 1")
        self.assertIsNotNone(row)
        self.assertIn("multi_open", row["detail_json"])

    # ------------------------------------------------------------------
    # 7. 治理：低忠诚出走
    # ------------------------------------------------------------------
    async def test_low_loyalty_char_leaves(self):
        fid = int(self.p1["faction_id"])
        c = await self._char_of("u1", "general")
        c["loyalty"] = 5
        await self.m.save_char(c)
        await self.m._commit()
        world = await self._world()
        await self.m._step_loyalty_defect(world)
        c = await self.m.get_char(int(c["id"]))
        self.assertEqual(c["status"], "wander")
        self.assertEqual(int(c["faction_id"]), 0)
        self.assertEqual(str(c["owner_user_id"]), "")
        row = await self.m._fetchone(
            "SELECT id FROM strategy_audit WHERE action='char_defect' "
            "ORDER BY id DESC LIMIT 1")
        self.assertIsNotNone(row)
        # 势力内其它角色未受影响
        for other in await self.m.get_chars(faction_id=fid, world_id=self.wid):
            self.assertNotEqual(int(other["id"]), int(c["id"]))
            self.assertGreaterEqual(int(other.get("loyalty", 70)), 30)

    # ------------------------------------------------------------------
    # 8. 势力事件：六种 effect 的即时结算
    # ------------------------------------------------------------------
    async def test_faction_event_effects_apply_and_audit(self):
        fid = int(self.p1["faction_id"])
        f = await self.m.get_faction(fid)
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == fid]
        self.assertTrue(nodes)
        node = nodes[0]
        node["morale"] = 50
        await self.m.save_node(node)
        p1 = await self.m.get_player("u1", self.wid)
        coin_before = int(p1.get("coin", 0) or 0)
        ev = {"id": "test_faction_event", "name": "测试势力事件",
              "scope": "faction",
              "effects": [{"type": "morale", "delta": 8},
                          {"type": "treasure", "chance": 1.0,
                           "rewards": {"coin": 1000}}]}
        world = await self._world()
        out = await self.m._apply_faction_event(world, f, ev, random.Random(1))
        self.assertTrue(out["text"])
        node = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(node["morale"]), 58)
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1.get("coin", 0) or 0), coin_before + 1000)
        row = await self.m._fetchone(
            "SELECT detail_json FROM strategy_audit WHERE action='faction_event' "
            "ORDER BY id DESC LIMIT 1")
        self.assertIsNotNone(row)
        self.assertIn("test_faction_event", row["detail_json"])

        # char_leave：低忠诚角色出走
        c = await self._char_of("u1", "official")
        c["loyalty"] = 1
        await self.m.save_char(c)
        await self.m._commit()
        world = await self._world()
        await self.m._apply_faction_event(
            world, await self.m.get_faction(fid),
            {"id": "t_leave", "name": "角色出走", "effects": [
                {"type": "char_leave", "loyalty_below": 30,
                 "leave_chance": 1.0}]},
            random.Random(2))
        c = await self.m.get_char(int(c["id"]))
        self.assertEqual(c["status"], "wander")

    # ------------------------------------------------------------------
    # 8b. tick 掷点入口能真正触发势力事件
    # ------------------------------------------------------------------
    async def test_step_faction_events_fires_some_event(self):
        fid = int(self.p1["faction_id"])
        fired = []
        for _ in range(40):
            world = await self._world()
            world["tick_no"] = int(world.get("tick_no", 0)) + 1
            await self.m.save_world(world)
            fired = await self.m._step_faction_events(world)
            if fired:
                break
        self.assertTrue(fired, "40 个 tick 内未触发任何势力事件")
        row = await self.m._fetchone(
            "SELECT id, detail_json FROM strategy_audit WHERE action='faction_event' "
            "ORDER BY id DESC LIMIT 1")
        self.assertIsNotNone(row)
        detail = json.loads(row["detail_json"])
        self.assertGreater(int(detail["faction_id"]), 0)

    # ------------------------------------------------------------------
    # 9. 面板 faction_ext
    # ------------------------------------------------------------------
    async def test_get_faction_ext_view(self):
        fid = await self._create_multi_faction()
        await self.m.faction_task_refresh("u1")
        await self._give("u1", merit=500)
        await self.m.mobilization_issue("u1", "attack")
        await self.m.rebuild_donate("u1", 12_345)
        f = await self.m.get_faction(fid)
        ext = await self.m.get_faction_ext(await self._world(), f)
        self.assertTrue(ext["tasks"])
        self.assertEqual(ext["mobilization"]["kind"], "attack")
        self.assertAlmostEqual(float(ext["mobilization"]["value"]), 0.1, places=3)
        self.assertEqual(int(ext["rebuild_fund"]), 12_345)
        self.assertGreater(float(ext["capital"]["bonus_pct"]), 0)


    # ------------------------------------------------------------------
    # 10. 科技树（主脉 + 支脉）
    # ------------------------------------------------------------------
    def test_tech_tree_branch_stack_and_lock(self):
        R = self.R
        # 支脉每级 +5%：L3 = 主脉 L3 的森林防 +10% + 支脉 +15%
        eff = R.tech_effects({"military": 3, "nodes": {"mil_b_forest": 3}})
        self.assertAlmostEqual(eff["terrain_def_mult"]["forest"], 0.25, places=3)
        # 未点主脉「地利」时，支脉不生效（防改档）
        locked = R.tech_effects({"military": 2, "nodes": {"mil_b_forest": 5}})
        self.assertNotIn("forest", locked["terrain_def_mult"])
        # 乘区支脉：谷田精耕 L5 与主脉 L1 的 ×1.10 相乘
        agri = R.tech_effects({"agri": 3, "nodes": {"agr_b_grain": 5}})
        self.assertAlmostEqual(
            agri["node_type_output_mult"]["grain"]["grain"],
            1.10 * (1.04 ** 5), places=4)
        # 树视图包含主脉/支脉与解锁关系
        view = R.tech_tree_view({})
        names = {n["id"]: n for n in view["military"]}
        self.assertEqual(names["mil_b_forest"]["parent"], "mil_s3")
        self.assertIn("林地战法", names["mil_s3"]["unlocks"])

    def test_tech_tree_config_merges_by_node_id(self):
        """v4.5.5：tech.tree 只覆盖指定节点，不再整线替换默认树。"""
        R = self.R
        tree = R.tech_tree({"tree": {"military": [
            {"id": "mil_s1", "name": "自定义军制"},
            {"id": "mil_b_new", "name": "新支脉", "kind": "branch",
             "parent": "mil_s1", "max": 5},
        ]}})
        nodes = {n["id"]: n for n in tree["military"]}
        self.assertIn("mil_s2", nodes)          # 未列出的默认主脉保留
        self.assertIn("mil_b_forest", nodes)    # 未列出的默认支脉保留
        self.assertEqual(nodes["mil_s1"]["name"], "自定义军制")
        self.assertEqual(nodes["mil_b_new"]["kind"], "branch")
        self.assertEqual(len(tree["military"]), len(R.TECH_TREE["military"]) + 1)

    async def test_tech_tree_branch_research_and_settle(self):
        fid = int(self.p1["faction_id"])
        f = await self.m.get_faction(fid)
        f["tech"] = {"military": 3}
        await self.m.save_faction(f)
        await self.m._commit()
        await self._give("u1", coin=100_000, iron=100_000)
        r = await self.m.tech_research("u1", "military", node_id="mil_b_forest")
        self.assertTrue(r["ok"])
        self.assertIn("林地战法", r["msg"])
        f = await self.m.get_faction(fid)
        pend = (f.get("tech") or {}).get("pending") or {}
        self.assertEqual(pend.get("node_id"), "mil_b_forest")
        self.assertEqual(pend.get("kind"), "branch")
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1["coin"]), 99_600)
        self.assertEqual(int(p1["iron"]), 99_700)

        # 到期结算：支脉 +1，主脉等级不变
        tech = f.get("tech") or {}
        tech["pending"]["finish_at"] = _now() - 1
        f["tech"] = tech
        await self.m.save_faction(f)
        await self.m._commit()
        await self.m._step_tech(await self._world())
        f = await self.m.get_faction(fid)
        tech = f.get("tech") or {}
        self.assertEqual(int((tech.get("nodes") or {}).get("mil_b_forest", 0)), 1)
        self.assertEqual(int(tech.get("military", 0)), 3)
        self.assertFalse(tech.get("pending"))

    async def test_tech_tree_branch_requires_parent_and_cap(self):
        fid = int(self.p1["faction_id"])
        f = await self.m.get_faction(fid)
        f["tech"] = {"military": 1}
        await self.m.save_faction(f)
        await self.m._commit()
        await self._give("u1", coin=100_000, iron=100_000)
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.tech_research("u1", "military", node_id="mil_b_forest")
        self.assertIn("需先完成 地利", str(cm.exception))

        # 支脉满级后拒绝
        f = await self.m.get_faction(fid)
        f["tech"] = {"military": 3, "nodes": {"mil_b_forest": 5}}
        await self.m.save_faction(f)
        await self.m._commit()
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.tech_research("u1", "military", node_id="mil_b_forest")
        self.assertIn("已满级", str(cm.exception))


if __name__ == "__main__":
    unittest.main()