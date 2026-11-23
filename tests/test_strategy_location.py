# tests/test_strategy_location.py
# 「仙域争霸 · 人物所在地机制」回归（设计文档 §17）：
#   传送 / 任务所在地 / 持续结算 / 编军 / 民心 / 抓将 / 大牢招募 /
#   擂台地点 / 瘟疫谣言联动 / 城镇潜力与异步建造 / 守军自然增长 / NPC 发展与经济
import json
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


class _FixedRng:
    """确定性随机源：用于事件选点/掷点，避免 flake。"""

    def __init__(self, node_id: int = 0):
        self.node_id = int(node_id)

    def randint(self, a, b):
        return int(a)

    def choices(self, seq, weights=None):
        return [seq[0]]

    def choice(self, seq):
        for x in seq:
            if isinstance(x, dict) and int(x.get("id", 0) or 0) == self.node_id:
                return x
        return seq[0]

    def random(self):
        return 0.0

    def uniform(self, a, b):
        return float(a)


class TestStrategyLocation(unittest.IsolatedAsyncioTestCase):
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
        self.m.main_cfg().setdefault("hex", {})["field_encounter_mode"] = "auto"
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        self.p1 = await self._enter("u1", "甲")
        self.p2 = await self._enter("u2", "乙")
        self.p3 = await self._enter("u3", "丙")
        for uid in ("u1", "u2", "u3"):
            await self._give(uid, coin=2_000_000, grain=200_000, iron=100_000)

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

    async def _enter(self, user_id: str, nickname: str = "") -> dict:
        return await self.m.enter_game(user_id, nickname,
                                       node_id=await self._free_node())

    async def _char_of(self, uid: str, role: str = "general"):
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == role:
                return c
        return None

    async def _fetchall(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            keys = [d[0] for d in cur.description]
            return [dict(zip(keys, r)) for r in rows]

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        cur = await self.conn.execute(sql, params)
        return cur.lastrowid or 0

    async def _place(self, char, node_id: int, *, status: str = "idle"):
        c = await self.m.get_char(int(char["id"]))
        c["location"] = {"node_id": int(node_id)}
        if status is not None:
            c["status"] = status
        await self.m.save_char(c)
        char["location"] = dict(c["location"])
        if status is not None:
            char["status"] = status
        return c

    async def _own(self, uid: str, node: dict) -> dict:
        p = await self.m.get_player(uid, self.wid)
        node["owner_faction"] = int(p["faction_id"])
        node["protect_until"] = 0
        node["siege_state"] = {}
        await self.m.save_node(node)
        return node

    async def _owned_node(self, uid: str, *, ntype: str = "") -> dict:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"
                 and (not ntype or n["type"] == ntype)]
        if not nodes and ntype:
            # 随机地图里同类型据点可能已被 NPC 占满：测试直接改派归属
            nodes = [n for n in await self.m.get_nodes(self.wid)
                     if n["type"] == ntype]
        self.assertTrue(nodes, f"没有可占领的 {ntype or '普通'} 据点")
        return await self._own(uid, nodes[0])

    async def _home(self, uid: str) -> dict:
        p = await self.m.get_player(uid, self.wid)
        f = await self.m.get_faction(int(p["faction_id"]))
        return await self.m.get_node(self.wid, int(f["capital_node_id"]))

    async def _finish_construction(self, node_id: int):
        world = await self.m.get_world()
        node = await self.m.get_node(self.wid, int(node_id))
        cons = dict(node.get("construction") or {})
        self.assertTrue(cons, "节点应处于建造中")
        cons["end_tick"] = int(world.get("tick_no", 0) or 0)
        cons["end_at"] = _now() - 1
        node["construction"] = cons
        await self.m.save_node(node)
        await self.m._step_construction(world)
        return await self.m.get_node(self.wid, int(node_id))

    def _force_settle(self, task_id: int, *, next_hours: int = 1) -> int:
        """构造一个已到点的持续任务结算时间（返回到期时间戳）。"""
        return _now() - 10

    # ------------------------------------------------------------------
    # 传送（§3）
    # ------------------------------------------------------------------
    async def test_move_char_own_and_allied(self):
        gen = await self._char_of("u1")
        target = await self._owned_node("u1")
        r = await self.m.move_char("u1", int(gen["id"]), int(target["id"]))
        self.assertTrue(r["ok"])
        c = await self.m.get_char(int(gen["id"]))
        self.assertEqual(self.R.char_node_id(c), int(target["id"]))
        # 联盟据点也可传送（alliance_id 指向盟主）
        ally_node = await self._owned_node("u2")
        f1 = await self.m.get_faction(int(self.p1["faction_id"]))
        f2 = await self.m.get_faction(int(self.p2["faction_id"]))
        f2["alliance_id"] = int(f1["id"])
        await self.m.save_faction(f2)
        await self.m._commit()
        r2 = await self.m.move_char("u1", int(gen["id"]), int(ally_node["id"]))
        self.assertTrue(r2["ok"])
        self.assertEqual(self.R.char_node_id(
            await self.m.get_char(int(gen["id"]))), int(ally_node["id"]))

    async def test_move_char_rejects_ownerless_enemy_ruin(self):
        gen = await self._char_of("u1")
        nodes = await self.m.get_nodes(self.wid)
        ownerless = next(n for n in nodes if int(n["owner_faction"]) == 0
                         and n["type"] != "ruin")
        enemy = next(n for n in nodes
                     if int(n["owner_faction"]) > 0
                     and int(n["owner_faction"]) != int(self.p1["faction_id"]))
        ruin = next(n for n in nodes if n["type"] == "ruin")
        for bad in (ownerless, enemy, ruin):
            with self.assertRaises(self.StrategyError):
                await self.m.move_char("u1", int(gen["id"]), int(bad["id"]))

    async def test_move_char_requires_idle(self):
        gen = await self._char_of("u1")
        target = await self._owned_node("u1")
        await self._place(gen, (await self._home("u1"))["id"], status="task")
        with self.assertRaises(self.StrategyError):
            await self.m.move_char("u1", int(gen["id"]), int(target["id"]))

    async def test_move_char_into_besieged_allowed_but_not_out(self):
        gen = await self._char_of("u1")
        home = await self._home("u1")
        siege = await self._owned_node("u1")
        siege["siege_state"] = {"attacker": 999}
        await self.m.save_node(siege)
        # 可进入围城据点
        r = await self.m.move_char("u1", int(gen["id"]), int(siege["id"]))
        self.assertTrue(r["ok"])
        # 不可离开围城据点
        with self.assertRaises(self.StrategyError):
            await self.m.move_char("u1", int(gen["id"]), int(home["id"]))

    # ------------------------------------------------------------------
    # 任务所在地 / 持续结算（§4 / §4.5）
    # ------------------------------------------------------------------
    async def test_local_task_requires_location_then_stays(self):
        gen = await self._char_of("u1")
        node = await self._owned_node("u1")
        # 异地：拒绝
        with self.assertRaises(self.StrategyError):
            await self.m.dispatch_task("u1", int(gen["id"]), int(node["id"]),
                                       "farm", 1)
        await self._place(gen, int(node["id"]))
        r = await self.m.dispatch_task("u1", int(gen["id"]), int(node["id"]),
                                       "farm", 1)
        self.assertTrue(r["ok"])
        task = (await self.m.get_tasks(self.wid, status="active"))[0]
        payout = task["payout"]
        self.assertEqual(int(payout["sustained"]), 1)
        self.assertEqual(int(task["end_at"]), 0)
        self.assertGreater(int(payout["next_settle_at"]), 0)
        self.assertEqual(int(payout["origin_node_id"]), int(node["id"]))
        # 强制小时结算
        payout["next_settle_at"] = _now() - 1
        await self.m._execute(
            "UPDATE strategy_tasks SET payout_json=? WHERE id=?",
            (json.dumps(payout, ensure_ascii=False), int(task["id"])))
        await self.m._step_settle_tasks(await self.m.get_world())
        c = await self.m.get_char(int(gen["id"]))
        self.assertEqual(c["status"], "task")
        self.assertEqual(self.R.char_node_id(c), int(node["id"]))
        task2 = (await self.m.get_tasks(self.wid, status="active"))[0]
        self.assertEqual(int(task2["payout"]["settled_hours"]), 1)

    async def test_expedition_returns_to_origin_and_falls_back_if_lost(self):
        self.m.main_cfg()["task"]["scout_capture_chance"] = 0.0
        gen = await self._char_of("u1")
        origin = await self._home("u1")
        safe = await self._owned_node("u1")
        await self._place(gen, int(origin["id"]))
        target = next(n for n in await self.m.get_nodes(self.wid)
                      if int(n["id"]) != int(origin["id"]))
        r = await self.m.dispatch_task(
            "u1", int(gen["id"]), int(origin["id"]), "scout", 1,
            target_node_id=int(target["id"]))
        self.assertTrue(r["ok"])
        task = (await self.m.get_tasks(self.wid, status="active"))[0]
        self.assertEqual(int(task["node_id"]), int(target["id"]))
        self.assertEqual(int(task["payout"]["origin_node_id"]),
                         int(origin["id"]))
        # 执行期间人物仍在出发地
        c = await self.m.get_char(int(gen["id"]))
        self.assertEqual(self.R.char_node_id(c), int(origin["id"]))
        # 出发地易主 → 结算后回最近己方据点
        await self._execute("UPDATE strategy_nodes SET owner_faction=? WHERE id=?",
                            (int(self.p2["faction_id"]), int(origin["id"])))
        await self._execute("UPDATE strategy_tasks SET end_at=? WHERE id=?",
                            (_now() - 1, int(task["id"])))
        await self.m._step_settle_tasks(await self.m.get_world())
        c2 = await self.m.get_char(int(gen["id"]))
        self.assertEqual(c2["status"], "idle")
        dest = self.R.char_node_id(c2)
        self.assertNotEqual(dest, int(target["id"]))
        self.assertEqual(dest, int(safe["id"]))
        node = await self.m.get_node(self.wid, dest)
        self.assertIsNotNone(node)
        self.assertEqual(int(node["owner_faction"]),
                         int(self.p1["faction_id"]))

    async def test_sustained_recall_proportional_without_half_loss(self):
        gen = await self._char_of("u1")
        node = await self._owned_node("u1")
        await self._place(gen, int(node["id"]))
        r = await self.m.dispatch_task("u1", int(gen["id"]), int(node["id"]),
                                       "farm", 1)
        task = (await self.m.get_tasks(self.wid, status="active"))[0]
        start = int(task["start_at"])
        with mock.patch("astrbot_plugin_xiuxian_promax.managers."
                        "strategy_manager._now", return_value=start + 1800):
            out = await self.m.recall_task("u1", int(r["task_id"]))
        self.assertTrue(out["ok"])
        # 半小 = 2 tick（4 tick/h），旧版 50% 召回损失会只剩 1 tick
        self.assertIn("2 tick", out["msg"])
        self.assertEqual(int(out["payout"].get("kind", "coin") == "none"), 0)

    async def test_sustained_stamina_exhaustion_stops_task(self):
        gen = await self._char_of("u1")
        node = await self._owned_node("u1")
        await self._place(gen, int(node["id"]))
        r = await self.m.dispatch_task("u1", int(gen["id"]), int(node["id"]),
                                       "mining", 1)
        c = await self.m.get_char(int(gen["id"]))
        c["stamina"] = 5
        await self.m.save_char(c)
        task = (await self.m.get_tasks(self.wid, status="active"))[0]
        payout = task["payout"]
        payout["next_settle_at"] = _now() - 1
        await self.m._execute(
            "UPDATE strategy_tasks SET payout_json=? WHERE id=?",
            (json.dumps(payout, ensure_ascii=False), int(task["id"])))
        await self.m._step_settle_tasks(await self.m.get_world())
        c2 = await self.m.get_char(int(gen["id"]))
        self.assertEqual(c2["status"], "idle")
        gone = await self.m._fetchone(
            "SELECT status FROM strategy_tasks WHERE id=?", (int(r["task_id"]),))
        self.assertEqual(gone["status"], "done")

    async def test_scout_recall_has_no_payout(self):
        self.m.main_cfg()["task"]["scout_capture_chance"] = 0.0
        gen = await self._char_of("u1")
        origin = await self._home("u1")
        await self._place(gen, int(origin["id"]))
        target = next(n for n in await self.m.get_nodes(self.wid)
                      if int(n["id"]) != int(origin["id"]))
        r = await self.m.dispatch_task(
            "u1", int(gen["id"]), int(origin["id"]), "scout", 1,
            target_node_id=int(target["id"]))
        out = await self.m.recall_task("u1", int(r["task_id"]))
        self.assertTrue(out["ok"])
        self.assertEqual(int(out["payout"].get("amount", 0)), 0)
        c = await self.m.get_char(int(gen["id"]))
        self.assertEqual(c["status"], "idle")
        self.assertEqual(self.R.char_node_id(c), int(origin["id"]))

    # ------------------------------------------------------------------
    # 编军所在地（§4.2）
    # ------------------------------------------------------------------
    async def test_create_army_requires_location_and_disband_keeps_node(self):
        gen = await self._char_of("u1")
        node = await self._owned_node("u1")
        with self.assertRaises(self.StrategyError):
            await self.m.create_army("u1", int(gen["id"]), [], int(node["id"]),
                                     "sword_shield", 300)
        await self._place(gen, int(node["id"]))
        r = await self.m.create_army("u1", int(gen["id"]), [], int(node["id"]),
                                     "sword_shield", 300)
        army = await self.m.get_army(int(r["army_id"]))
        # 模拟军团停靠到另一座己方据点后解散
        other = await self._owned_node("u1")
        army["node_id"] = int(other["id"])
        army["state"] = "idle"
        await self.m.save_army(army)
        await self.m.disband_army("u1", int(r["army_id"]))
        c = await self.m.get_char(int(gen["id"]))
        self.assertEqual(c["status"], "idle")
        self.assertEqual(self.R.char_node_id(c), int(other["id"]))

    # ------------------------------------------------------------------
    # 民心（§5 / §6）
    # ------------------------------------------------------------------
    async def test_charm_garrison_bonus_and_caps(self):
        low = {"stats": {"charm": 60}}
        high = {"stats": {"charm": 100}}
        self.assertEqual(self.R.morale_garrison_bonus([low]), 2)
        self.assertEqual(self.R.morale_garrison_bonus([high]), 4)
        self.assertEqual(
            self.R.morale_garrison_bonus([high, high, high]), 6)
        self.assertEqual(
            self.R.morale_garrison_bonus([low, low, low, low]), 6)

    async def test_governor_resident_morale_bonus(self):
        gen = await self._char_of("u1")
        node = await self._owned_node("u1")
        gen["stats"] = dict(gen.get("stats") or {})
        gen["stats"]["charm"] = 0
        await self.m.save_char(gen)
        # 移走其他驻守角色（清空 location），避免魅力加成混入
        for c in await self.m.get_chars(owner_user_id="u1", world_id=self.wid):
            if int(c["id"]) != int(gen["id"]):
                c["location"] = {}
                c["status"] = "idle"
                await self.m.save_char(c)
        node["governor_char_id"] = int(gen["id"])
        node["buildings"] = []
        node["morale"] = 0
        await self.m.save_node(node)
        # 清空科技，排除「仁政/征募」对民心恢复的额外加成
        f = await self.m.get_faction(int(self.p1["faction_id"]))
        f["tech"] = {}
        await self.m.save_faction(f)
        await self._place(gen, int(node["id"]))
        self.m.main_cfg()["garrison"]["grow_pct"] = 0.0
        with mock.patch.object(self.R, "riot_check", return_value=False):
            await self.m._step_recovery(await self.m.get_world())
        resident = await self.m.get_node(self.wid, int(node["id"]))
        # 太守驻守：基础 1 → 太守 2 + 固定 1 = 3（民心 < 80 时）
        self.assertEqual(int(resident["morale"]), 3)
        # 太守离城：加成失效，仅 +1
        resident["morale"] = 0
        await self.m.save_node(resident)
        await self._place(gen, (await self._home("u1"))["id"])
        with mock.patch.object(self.R, "riot_check", return_value=False):
            await self.m._step_recovery(await self.m.get_world())
        away = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(away["morale"]), 1)

    # ------------------------------------------------------------------
    # 抓将 / 大牢 / 名册（§7）
    # ------------------------------------------------------------------
    async def test_city_fall_captures_all_residents_not_remote_or_attacker(self):
        city = await self._owned_node("u2", ntype="city")
        if not city:
            city = await self._owned_node("u2")
        u2_chars = await self.m.get_chars(owner_user_id="u2", world_id=self.wid)
        inside = u2_chars[0]
        remote = u2_chars[1]
        u1_char = await self._char_of("u1")
        await self._place(inside, int(city["id"]))
        await self._place(remote, (await self._home("u2"))["id"])
        await self._place(u1_char, int(city["id"]))
        before = await self.m._roster_count("u2", self.wid)
        with mock.patch.object(self.R, "capture_check", return_value=True):
            got = await self.m.roll_captures(
                await self.m.get_world(), await self.m.get_node(
                    self.wid, int(city["id"])), [], "city_fall",
                int(self.p1["faction_id"]))
        self.assertIn(inside["name"], got)
        self.assertNotIn(remote["name"], got)
        self.assertNotIn(u1_char["name"], got)
        inside2 = await self.m.get_char(int(inside["id"]))
        self.assertEqual(inside2["status"], "captive")
        self.assertEqual(int(inside2["captor_faction"]),
                         int(self.p1["faction_id"]))
        # 俘虏不占名册槽位
        self.assertEqual(await self.m._roster_count("u2", self.wid), before - 1)

    async def test_recruit_captive_requires_free_roster_slot(self):
        city = await self._owned_node("u2")
        victim = (await self.m.get_chars(owner_user_id="u2",
                                        world_id=self.wid))[0]
        await self._place(victim, int(city["id"]))
        with mock.patch.object(self.R, "capture_check", return_value=True):
            got = await self.m.roll_captures(
                await self.m.get_world(), city, [], "city_fall",
                int(self.p1["faction_id"]))
        self.assertTrue(got)
        cid = int((await self.m.get_char(int(victim["id"])))["id"])
        rcfg = self.m.main_cfg()["recruit"]
        rcfg.update({"character_cap_base": 2, "character_cap_per_node": 0,
                     "character_cap_max": 30})
        with self.assertRaises(self.StrategyError) as ctx:
            await self.m.recruit_captive("u1", cid)
        self.assertIn("名册已满", str(ctx.exception))
        rcfg.update({"character_cap_base": 10, "character_cap_per_node": 0,
                     "character_cap_max": 30})
        res = await self.m.recruit_captive("u1", cid)
        self.assertTrue(res["ok"])
        c = await self.m.get_char(cid)
        self.assertEqual(c["status"], "idle")
        self.assertEqual(c["owner_user_id"], "u1")
        self.assertGreater(self.R.char_node_id(c), 0)

    # ------------------------------------------------------------------
    # 擂台地点（§8）
    # ------------------------------------------------------------------
    async def test_duel_accept_requires_arena_location(self):
        gen1 = await self._char_of("u1")
        gen2 = await self._char_of("u2")
        for g in (gen1, gen2):
            g["stats"] = dict(g.get("stats") or {})
            g["stats"]["valor"] = 90
            g["injury"] = 0
            await self.m.save_char(g)
        home = await self._home("u1")
        home["buildings"] = [b for b in (home.get("buildings") or [])
                             if (b.get("id") if isinstance(b, dict) else b)
                             != "arena"]
        home["buildings"].append({"id": "arena", "level": 1})
        await self.m.save_node(home)
        await self._place(gen1, int(home["id"]))
        await self._place(gen2, (await self._home("u2"))["id"])
        r = await self.m.duel_arena_open("u1", int(gen1["id"]), 100)
        with self.assertRaises(self.StrategyError) as ctx:
            await self.m.duel_arena_accept("u2", str(r["duel_id"]),
                                           int(gen2["id"]))
        self.assertIn("传送", str(ctx.exception))
        await self._place(gen2, int(home["id"]))
        r2 = await self.m.duel_arena_accept("u2", str(r["duel_id"]),
                                            int(gen2["id"]))
        self.assertTrue(r2["ok"])

    # ------------------------------------------------------------------
    # 事件：瘟疫 / 谣言驻守联动（§9 / §15）
    # ------------------------------------------------------------------
    async def _insert_plague(self, node: dict, ticks: int = 4):
        world = await self.m.get_world()
        tick_no = int(world.get("tick_no", 0) or 0)
        await self.m._execute("""
            INSERT INTO strategy_events (world_id, event_id, scope, target_json,
                start_at, end_at, start_tick, end_tick, data_json)
            VALUES (?,'plague_test','region',?,?,?,?,?,?)
        """, (self.wid,
              json.dumps({"node_ids": [int(node["id"])]}, ensure_ascii=False),
              _now(), _now() + 600, tick_no, tick_no + ticks,
              json.dumps({"name": "瘟疫", "effects": [
                  {"type": "node_dot", "troops_pct": -3, "morale": -10}]},
                  ensure_ascii=False)))

    async def test_plague_halved_by_resident_and_immune_by_doctor(self):
        node = await self._owned_node("u1")
        char = await self._char_of("u1")
        self.m.main_cfg()["garrison"]["grow_pct"] = 0.0
        node["npc_garrison"] = {"troops": 2000, "morale": 80}
        node["morale"] = 100
        await self.m.save_node(node)
        f = await self.m.get_faction(int(self.p1["faction_id"]))
        f["tech"] = {}
        await self.m.save_faction(f)
        await self._place(char, (await self._home("u1"))["id"])
        away = await self.m.get_char(int(char["id"]))
        away["location"] = {}
        await self.m.save_char(away)
        char = await self.m.get_char(int(char["id"]))
        char["traits"] = [t for t in (char.get("traits") or [])
                          if t != "doctor"]
        await self.m.save_char(char)

        async def _run():
            # 每轮把守军复位到上限，排除自然增长干扰
            fresh = await self.m.get_node(self.wid, int(node["id"]))
            fresh["npc_garrison"] = {"troops": 2000, "morale": 80}
            fresh["morale"] = 100
            await self.m.save_node(fresh)
            await self.m._step_recovery(await self.m.get_world())
            return int((await self.m.get_node(
                self.wid, int(node["id"])))["npc_garrison"]["troops"])

        # 无驻守：-3%
        await self._insert_plague(node)
        self.assertEqual(await _run(), int(2000 * 0.97))
        # 有驻守：减半 -1.5%
        await self._place(char, int(node["id"]))
        self.assertEqual(await _run(), int(2000 * 0.985))
        # doctor 驻守：完全免疫
        char = await self.m.get_char(int(char["id"]))
        char["traits"] = list(set((char.get("traits") or []) + ["doctor"]))
        await self.m.save_char(char)
        self.assertEqual(await _run(), 2000)

    async def test_rumor_governor_halves_morale_and_reduces_duration(self):
        gen = await self._char_of("u1")
        node = await self._owned_node("u1")
        node["governor_char_id"] = int(gen["id"])
        await self.m.save_node(node)
        await self._place(gen, int(node["id"]))
        cfg = self.m.cfg
        old_events = cfg.get("events")
        cfg["events"] = {
            "roll_per_tick": [1, 1],
            "events": [{
                "id": "rumor_test", "name": "谣言", "scope": "region",
                "duration_ticks": 4, "weight": 100,
                "effects": [{"type": "rumor", "morale": -10}],
            }],
        }
        try:
            with mock.patch.object(self.m, "_rng",
                                   return_value=_FixedRng(int(node["id"]))):
                world = await self.m.get_world()
                await self.m._step_events(world)
            row = await self.m._fetchone(
                "SELECT * FROM strategy_events WHERE world_id=? "
                "AND event_id='rumor_test' ORDER BY id DESC LIMIT 1", (self.wid,))
            self.assertIsNotNone(row)
            effects = (json.loads(row["data_json"]) or {}).get("effects")
            self.assertEqual(int(effects[0]["morale"]), -5)
            self.assertEqual(int(row["end_tick"]) - int(row["start_tick"]), 3)
            # 太守离城：恢复全额 -10，时长 4
            await self._execute(
                "DELETE FROM strategy_events WHERE world_id=? AND event_id='rumor_test'",
                (self.wid,))
            world = await self.m.get_world()
            data = world.get("data") or {}
            data["event_cd"] = {}
            world["data"] = data
            await self.m.save_world(world)
            await self._place(gen, (await self._home("u1"))["id"])
            with mock.patch.object(self.m, "_rng",
                                   return_value=_FixedRng(int(node["id"]))):
                await self.m._step_events(world)
            row2 = await self.m._fetchone(
                "SELECT * FROM strategy_events WHERE world_id=? "
                "AND event_id='rumor_test' ORDER BY id DESC LIMIT 1", (self.wid,))
            effects2 = (json.loads(row2["data_json"]) or {}).get("effects")
            self.assertEqual(int(effects2[0]["morale"]), -10)
            self.assertEqual(
                int(row2["end_tick"]) - int(row2["start_tick"]), 4)
        finally:
            cfg["events"] = old_events

    async def test_plot_rumor_mitigated_by_resident_governor(self):
        """真实谣言计略（非配置事件）也应吃驻守太守的减半/时长 -1。"""
        node = await self._owned_node("u1", ntype="city")
        node["morale"] = 90
        node["governor_char_id"] = 0
        await self.m.save_node(node)
        gov = await self._char_of("u1")
        node = await self.m.get_node(self.wid, int(node["id"]))
        node["governor_char_id"] = int(gov["id"])
        await self.m.save_node(node)
        await self._place(gov, int(node["id"]))
        actor = await self._char_of("u2")

        async def _run(task_id: int) -> dict:
            task = {"id": task_id, "world_id": self.wid,
                    "character_id": int(actor["id"]),
                    "faction_id": int(self.p2["faction_id"]),
                    "task_type": "plot_rumor", "node_id": int(node["id"]),
                    "target_faction": int(self.p1["faction_id"]),
                    "payout": {"plot": "rumor", "target": int(node["id"])}}
            with mock.patch.object(self.m, "_rng",
                                   return_value=_FixedRng(int(node["id"]))):
                return await self.m._settle_plot_task(
                    await self.m.get_world(), task, actor, None)

        res = await _run(9001)
        self.assertTrue(res["success"])
        n1 = await self.m.get_node(self.wid, int(node["id"]))
        # 谣言 -15 → 太守驻守减半 → -7（int 截断）
        self.assertEqual(int(n1["morale"]), 90 - 7)
        row = await self.m._fetchone(
            "SELECT * FROM strategy_events WHERE world_id=? AND event_id='rumor' "
            "ORDER BY id DESC LIMIT 1", (self.wid,))
        self.assertEqual(int(row["end_tick"]) - int(row["start_tick"]), 2)
        # 太守离城：恢复全额 -15、3 tick
        await self._place(gov, (await self._home("u1"))["id"])
        n1["morale"] = 90
        await self.m.save_node(n1)
        res2 = await _run(9002)
        self.assertTrue(res2["success"])
        n2 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(n2["morale"]), 90 - 15)
        row2 = await self.m._fetchone(
            "SELECT * FROM strategy_events WHERE world_id=? AND event_id='rumor' "
            "ORDER BY id DESC LIMIT 1", (self.wid,))
        self.assertEqual(int(row2["end_tick"]) - int(row2["start_tick"]), 3)

    # ------------------------------------------------------------------
    # 城池：建设类型 / 潜力 / 阶梯费 / 异步建造（§10.1~10.3）
    # ------------------------------------------------------------------
    async def test_build_and_upgrade_open_to_mine_grain_market(self):
        for ntype, potential in (("mine", 2), ("grain", 2), ("market", 3)):
            node = await self._owned_node("u1", ntype=ntype)
            node["level"] = 1
            node["potential"] = potential
            await self.m.save_node(node)
            r = await self.m.build("u1", int(node["id"]), "tower")
            self.assertTrue(r["ok"], ntype)
            # 遗迹仍不可建设
            self.assertTrue((await self.m.upgrade_node(
                "u1", int(node["id"])))["ok"], ntype)
            done = await self._finish_construction(int(node["id"]))
            self.assertEqual(int(done["level"]), 2)
            if potential <= 2:
                with self.assertRaises(self.StrategyError):
                    await self.m.upgrade_node("u1", int(node["id"]))
        ruin = next(n for n in await self.m.get_nodes(self.wid)
                    if n["type"] == "ruin")
        with self.assertRaises(self.StrategyError):
            await self.m.build("u1", int(ruin["id"]), "tower")

    async def test_node_upgrade_tier_cost_and_async_construction(self):
        self.assertEqual(self.R.node_upgrade_cost(1), (5000, 500))
        self.assertEqual(self.R.node_upgrade_cost(2), (20000, 2000))
        self.assertEqual(self.R.node_upgrade_cost(3), (45000, 4500))
        node = await self._owned_node("u1", ntype="city")
        node["level"] = 1
        node["potential"] = 5
        node["construction"] = {}
        await self.m.save_node(node)
        p0 = await self.m.get_player("u1", self.wid)
        coin0, iron0 = int(p0["coin"]), int(p0["iron"])
        r = await self.m.upgrade_node("u1", int(node["id"]))
        self.assertTrue(r["ok"])
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(coin0 - int(p1["coin"]), 5000)
        self.assertEqual(iron0 - int(p1["iron"]), 500)
        n1 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(n1["level"]), 1)
        self.assertTrue(n1["construction"])
        self.assertEqual(set(n1["construction"]),
                         {"to_level", "end_at", "end_tick", "started_at", "by"})
        # 建造中不可重复升级
        with self.assertRaises(self.StrategyError):
            await self.m.upgrade_node("u1", int(node["id"]))
        done = await self._finish_construction(int(node["id"]))
        self.assertEqual(int(done["level"]), 2)
        self.assertEqual(int(done["defense"]), 2000)

    async def test_construction_pauses_under_siege_and_voids_on_capture(self):
        node = await self._owned_node("u1", ntype="city")
        node["level"] = 1
        node["potential"] = 5
        await self.m.save_node(node)
        await self.m.upgrade_node("u1", int(node["id"]))
        n1 = await self.m.get_node(self.wid, int(node["id"]))
        end_tick0 = int(n1["construction"]["end_tick"])
        # 围城：顺延 1 tick，仍保留在建
        n1["siege_state"] = {"attacker": int(self.p2["faction_id"])}
        await self.m.save_node(n1)
        await self.m._step_construction(await self.m.get_world())
        n2 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertTrue(n2["construction"])
        self.assertEqual(int(n2["construction"]["end_tick"]), end_tick0 + 1)
        # 解围后到期完成
        n2["siege_state"] = {}
        await self.m.save_node(n2)
        await self._finish_construction(int(node["id"]))
        self.assertEqual(int((await self.m.get_node(
            self.wid, int(node["id"])))["level"]), 2)
        # 易主：在建作废
        await self.m.upgrade_node("u1", int(node["id"]))
        world = await self.m.get_world()
        cap_node = await self.m.get_node(self.wid, int(node["id"]))
        await self.m._capture_node(world, cap_node, int(self.p2["faction_id"]),
                                   by_force=False)
        n3 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertFalse(n3["construction"])
        self.assertEqual(int(n3["level"]), 2)  # 受降保留等级

    async def test_force_capture_drops_one_level_surrender_keeps(self):
        node = await self._owned_node("u1", ntype="city")
        node["level"] = 4
        await self.m.save_node(node)
        world = await self.m.get_world()
        await self.m._capture_node(world, node, int(self.p2["faction_id"]),
                                   by_force=True)
        forced = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(forced["level"]), 3)
        forced["owner_faction"] = int(self.p1["faction_id"])
        forced["level"] = 4
        await self.m.save_node(forced)
        await self.m._capture_node(world, forced, int(self.p2["faction_id"]),
                                   by_force=False)
        surrendered = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(surrendered["level"]), 4)

    # ------------------------------------------------------------------
    # 守军自然增长（§10.4）
    # ------------------------------------------------------------------
    async def test_garrison_growth_both_sides_and_siege_pause(self):
        R = self.R
        node = await self._owned_node("u1")
        node["level"] = 1
        node["morale"] = 100
        node["npc_garrison"] = {"troops": 0, "morale": 80}
        await self.m.save_node(node)
        # 清空科技，避免征募/仁政干扰守军与民心断言
        f = await self.m.get_faction(int(self.p1["faction_id"]))
        f["tech"] = {}
        await self.m.save_faction(f)
        # 移走驻守角色，只看基础 +2%
        for c in await self.m.get_chars(owner_user_id="u1", world_id=self.wid):
            c["location"] = {}
            c["status"] = "idle"
            await self.m.save_char(c)
        await self.m._step_recovery(await self.m.get_world())
        n1 = await self.m.get_node(self.wid, int(node["id"]))
        base = int(R.garrison_cap(1) * 0.02)
        self.assertEqual(int(n1["npc_garrison"]["troops"]), base)
        # 玩家驻守加成：5 名角色 → 封顶 +5%
        n1["npc_garrison"] = {"troops": 0, "morale": 80}
        await self.m.save_node(n1)
        chars = list(await self.m.get_chars(owner_user_id="u1",
                                            world_id=self.wid))
        while len(chars) < 5:
            rng = random.Random(1000 + len(chars))
            c = R.generate_character(rng, rarity="fine", role=R.ROLE_GENERAL,
                                     name_pools={})
            c.update({"world_id": self.wid, "faction_id": self.p1["faction_id"],
                      "owner_user_id": "u1", "loyalty": 100, "status": "idle"})
            await self.m.insert_char(c)
            chars.append(c)
        for c in chars[:5]:
            await self._place(c, int(node["id"]))
        await self.m._step_recovery(await self.m.get_world())
        n2 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(n2["npc_garrison"]["troops"]),
                         base + int(R.garrison_cap(1) * 0.05))
        # NPC：+2% × 民心/100
        npc_node = next(n for n in await self.m.get_nodes(self.wid)
                        if int(n["owner_faction"]) > 0
                        and n["type"] != "ruin")
        npc_node["level"] = 1
        npc_node["morale"] = 50
        npc_node["siege_state"] = {}
        npc_node["npc_garrison"] = {"troops": 0, "morale": 50}
        await self.m.save_node(npc_node)
        await self.m._step_recovery(await self.m.get_world())
        n3 = await self.m.get_node(self.wid, int(npc_node["id"]))
        self.assertEqual(int(n3["npc_garrison"]["troops"]),
                         int(int(R.garrison_cap(1) * 0.02) * 0.5))
        # 围城：不增长
        n3["siege_state"] = {"attacker": 1}
        n3["npc_garrison"] = {"troops": 0, "morale": 50}
        await self.m.save_node(n3)
        await self.m._step_recovery(await self.m.get_world())
        n4 = await self.m.get_node(self.wid, int(npc_node["id"]))
        self.assertEqual(int(n4["npc_garrison"]["troops"]), 0)

    async def test_npc_garrison_no_respawn_countdown(self):
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n["owner_faction"]) > 0 and n["type"] != "ruin")
        node["npc_garrison"] = {"troops": 0, "morale": 0}
        node["respawn_at"] = _now() - 1
        await self.m.save_node(node)
        await self.m._step_npc_ai(await self.m.get_world())
        n2 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(n2["npc_garrison"]["troops"]), 0)

    # ------------------------------------------------------------------
    # NPC 发展 / 经济 / 民心（§10.6~10.8）
    # ------------------------------------------------------------------
    async def _npc_faction_node(self, level: int = 1, potential: int = 5):
        row = await self.m._fetchone(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "ORDER BY id LIMIT 1", (self.wid,))
        fid = int(row["id"])
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == fid]
        node = nodes[0]
        # 同势力其他据点全部压到潜力上限，保证只考核目标据点
        for n in nodes:
            n["level"] = int(R_node_potential(n))
            n["construction"] = {}
            await self.m.save_node(n)
        node["level"] = level
        node["potential"] = potential
        node["construction"] = {}
        node["siege_state"] = {}
        node["develop_ready_at"] = 0
        await self.m.save_node(node)
        f = await self.m.get_faction(fid)
        f["treasury"] = {"coin": 1_000_000, "grain": 500_000,
                         "iron": 100_000, "reserved_coin": 0}
        await self.m.save_faction(f)
        world = await self.m.get_world()
        data = world.get("data") or {}
        data["npc_develop"] = {}
        world["data"] = data
        await self.m.save_world(world)
        return fid, node

    async def test_npc_develop_four_conditions(self):
        fid, node = await self._npc_faction_node(level=1, potential=5)
        world = await self.m.get_world()
        # ① 稳定期：首次懒初始化，不启动建造
        await self.m._step_npc_develop(world)
        n1 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertFalse(n1["construction"])
        self.assertGreater(int(n1["develop_ready_at"]), 0)
        # ② 稳定期到达 + 国库充足 → 启动建造并扣费
        n1["develop_ready_at"] = _now() - 1
        await self.m.save_node(n1)
        f0 = await self.m.get_faction(fid)
        coin0 = int((f0.get("treasury") or {}).get("coin", 0))
        world = await self.m.get_world()
        await self.m._step_npc_develop(world)
        n2 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertTrue(n2["construction"])
        self.assertEqual(int(n2["construction"]["to_level"]), 2)
        f1 = await self.m.get_faction(fid)
        self.assertEqual(coin0 - int((f1.get("treasury") or {})["coin"]), 5000)
        # ④ 节奏上限：刚启动后即使又有新目标也暂不启动
        n2["construction"] = {}
        await self.m.save_node(n2)
        world = await self.m.get_world()
        await self.m._step_npc_develop(world)
        n3 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertFalse(n3["construction"])
        # ③ 国库不足：清空节奏并降低国库
        world = await self.m.get_world()
        data = world.get("data") or {}
        data["npc_develop"] = {}
        world["data"] = data
        await self.m.save_world(world)
        f2 = await self.m.get_faction(fid)
        f2["treasury"] = {"coin": 5000, "grain": 0, "iron": 0,
                          "reserved_coin": 0}
        await self.m.save_faction(f2)
        n3["develop_ready_at"] = _now() - 1
        await self.m.save_node(n3)
        await self.m._step_npc_develop(world)
        n4 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertFalse(n4["construction"])
        # 围城：跳过
        f3 = await self.m.get_faction(fid)
        f3["treasury"] = {"coin": 1_000_000, "grain": 0, "iron": 100_000,
                          "reserved_coin": 0}
        await self.m.save_faction(f3)
        n4["siege_state"] = {"attacker": 1}
        n4["develop_ready_at"] = _now() - 1
        await self.m.save_node(n4)
        await self.m._step_npc_develop(world)
        n5 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertFalse(n5["construction"])

    async def test_npc_develop_skips_unaffordable_top_candidate(self):
        """候选须先按“国库够”过滤：买不起的高价目标不能挡住买得起的低价目标。"""
        fid, cheap = await self._npc_faction_node(level=1, potential=5)
        others = [n for n in await self.m.get_nodes(self.wid)
                  if int(n["owner_faction"]) == fid
                  and int(n["id"]) != int(cheap["id"])]
        if others:
            expensive = others[0]
        else:
            # 该势力可能只剩一座据点（受前面用例影响）：借一座据点做测试
            expensive = next(n for n in await self.m.get_nodes(self.wid)
                             if int(n["owner_faction"]) != fid
                             and int(n["id"]) != int(cheap["id"]))
            expensive["owner_faction"] = fid
        # 同类型/同产出/无相邻 → 排序只按 -level（L4 排在 L1 前面）
        for n in (cheap, expensive):
            n["type"] = "city"
            n["potential"] = 5
            n["adj"] = []
            n["construction"] = {}
            n["siege_state"] = {}
            n["develop_ready_at"] = _now() - 1
        cheap["level"] = 1
        expensive["level"] = 4
        await self.m.save_node(cheap)
        await self.m.save_node(expensive)
        f = await self.m.get_faction(fid)
        floor = self.m._npc_reserve(f)
        # 国库只够 L1→L2（5000/500），不够 L4→L5（80000/8000）
        coin0 = int(floor["coin"]) + 5_000 + 100
        iron0 = int(floor["iron"]) + 500 + 100
        f["treasury"] = {"coin": coin0, "grain": 0, "iron": iron0,
                         "reserved_coin": 0}
        await self.m.save_faction(f)
        world = await self.m.get_world()
        data = world.get("data") or {}
        data["npc_develop"] = {}
        world["data"] = data
        await self.m.save_world(world)
        await self.m._step_npc_develop(world)
        n_cheap = await self.m.get_node(self.wid, int(cheap["id"]))
        n_exp = await self.m.get_node(self.wid, int(expensive["id"]))
        self.assertTrue(n_cheap["construction"])
        self.assertEqual(int(n_cheap["construction"]["to_level"]), 2)
        self.assertFalse(n_exp["construction"])
        f2 = await self.m.get_faction(fid)
        self.assertEqual(int(f2["treasury"]["coin"]), coin0 - 5_000)
        self.assertEqual(int(f2["treasury"]["iron"]), iron0 - 500)

    async def test_npc_develop_stabilize_starts_while_busy(self):
        """稳定期应在占领/完成后立即起算，不被“同时在建/间隔期”延后。"""
        fid, target = await self._npc_faction_node(level=1, potential=5)
        owned = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == fid]
        if len(owned) < 2:
            extra = next(n for n in await self.m.get_nodes(self.wid)
                         if int(n["owner_faction"]) != fid
                         and int(n["id"]) != int(target["id"]))
            extra["owner_faction"] = fid
            await self.m.save_node(extra)
            owned.append(extra)
        busy = next(n for n in owned if int(n["id"]) != int(target["id"]))
        busy["construction"] = {"to_level": 2, "end_at": _now() + 3600,
                                "end_tick": 999999, "started_at": _now(),
                                "by": "npc:test"}
        await self.m.save_node(busy)
        target["develop_ready_at"] = 0
        await self.m.save_node(target)
        world = await self.m.get_world()
        data = world.get("data") or {}
        data["npc_develop"] = {}
        world["data"] = data
        await self.m.save_world(world)
        await self.m._step_npc_develop(world)
        n = await self.m.get_node(self.wid, int(target["id"]))
        self.assertGreater(int(n["develop_ready_at"]), _now())

    async def test_npc_spawn_army_costs_treasury_and_morale(self):
        # 选一个带相邻据点的 NPC 城
        row = await self.m._fetchone(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "ORDER BY id LIMIT 1", (self.wid,))
        fid = int(row["id"])
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n["owner_faction"]) == fid and n.get("adj"))
        node["morale"] = 80
        node["npc_garrison"] = {"troops": 1000, "morale": 80}
        await self.m.save_node(node)
        f = await self.m.get_faction(fid)
        f["treasury"] = {"coin": 100_000, "grain": 50_000, "iron": 1000,
                         "reserved_coin": 0}
        await self.m.save_faction(f)
        target = next(n for n in await self.m.get_nodes(self.wid)
                      if int(n["id"]) != int(node["id"]))
        world = await self.m.get_world()
        f = await self.m.get_faction(fid)
        coin0 = int(f["treasury"]["coin"])
        grain0 = int(f["treasury"]["grain"])
        army = await self.m._spawn_npc_army(world, f, node, target, 1000)
        self.assertIsNotNone(army)
        self.assertEqual(int(army["grain"]), 1000)  # 100 粮/百兵
        f2 = await self.m.get_faction(fid)
        pers = f.get("personality") or {}
        tt = self.R.TROOP_TYPES.get(
            pers.get("troop_pref") or "sword_shield") or {}
        base_tt = self.R.TROOP_TYPES["sword_shield"]
        ratio = float(tt.get("recruit_coin", base_tt["recruit_coin"])) / \
            float(base_tt["recruit_coin"])
        expect_coin = max(1, int(round(1000 / 100 * 500 * ratio)))
        self.assertEqual(coin0 - int(f2["treasury"]["coin"]), expect_coin)
        self.assertEqual(grain0 - int(f2["treasury"]["grain"]), 1000 + 1000)
        n2 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(n2["morale"]), 60)  # 2 民心/百兵
        # 国库低于守财底线：不组建
        f2["treasury"] = {"coin": 5000, "grain": 2000, "iron": 500,
                          "reserved_coin": 0}
        await self.m.save_faction(f2)
        self.assertIsNone(await self.m._spawn_npc_army(
            world, f2, n2, target, 500))

    async def test_npc_auto_retreat_refunds_grain(self):
        row = await self.m._fetchone(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "ORDER BY id LIMIT 1", (self.wid,))
        fid = int(row["id"])
        f = await self.m.get_faction(fid)
        f["treasury"] = {"coin": 10_000, "grain": 10_000, "iron": 1000,
                         "reserved_coin": 0}
        await self.m.save_faction(f)
        grain0 = 10_000
        army = await self.m.insert_army({
            "world_id": self.wid, "faction_id": fid, "owner_user_id": "",
            "name": "断粮军", "node_id": 1, "characters": [],
            "troop_type": "sword_shield", "veteran": 0,
            "troops": 0, "grain": 500, "morale": 10, "state": "march"})
        army = await self.m.get_army(int(army))
        await self.m._auto_retreat(army)
        f2 = await self.m.get_faction(fid)
        self.assertEqual(int(f2["treasury"]["grain"]), grain0 + 500)

    async def test_npc_relief_triggers_and_daily_cap(self):
        row = await self.m._fetchone(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "ORDER BY id LIMIT 1", (self.wid,))
        fid = int(row["id"])
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n["owner_faction"]) == fid)
        node["morale"] = 20
        await self.m.save_node(node)
        f = await self.m.get_faction(fid)
        f["treasury"] = {"coin": 100_000, "grain": 100_000, "iron": 1000,
                         "reserved_coin": 0}
        await self.m.save_faction(f)
        world = await self.m.get_world()
        data = world.get("data") or {}
        data["npc_relief"] = {}
        world["data"] = data
        await self.m.save_world(world)
        await self.m._step_npc_economy(world)
        n2 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertGreaterEqual(int(n2["morale"]), 26)
        f2 = await self.m.get_faction(fid)
        self.assertEqual(int(f2["treasury"]["coin"]), 97_000)

    async def test_market_maker_uses_real_npc_stock_and_expiry_refunds(self):
        mcfg = self.m.main_cfg()["market"]
        mcfg["npc_maker_interval_ticks"] = 8
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["type"] == "market")
        row = await self.m._fetchone(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "ORDER BY id LIMIT 1", (self.wid,))
        fid = int(row["id"])
        node["owner_faction"] = fid
        await self.m.save_node(node)
        f = await self.m.get_faction(fid)
        f["treasury"] = {"coin": 100_000, "grain": 20_000, "iron": 20_000,
                         "reserved_coin": 0}
        f["personality"] = dict(f.get("personality") or {})
        await self.m.save_faction(f)
        world = await self.m.get_world()
        world["tick_no"] = 8
        await self.m.save_world(world)
        await self.m._step_market_maker(world)
        order = await self.m._fetchone(
            "SELECT * FROM strategy_market_orders WHERE world_id=? AND node_id=? "
            "AND status='open' AND seller_user='' ORDER BY id DESC LIMIT 1",
            (self.wid, int(node["id"])))
        self.assertIsNotNone(order)
        key = str(order["item_type"])
        rows = await self.m._fetchall(
            "SELECT COALESCE(SUM(amount),0) AS n FROM strategy_market_orders "
            "WHERE world_id=? AND seller_faction=? AND status='open' "
            "AND item_type=?", (self.wid, fid, key))
        total_open = int((rows[0] if rows else {}).get("n", 0) or 0)
        f2 = await self.m.get_faction(fid)
        self.assertEqual(int(f2["treasury"][key]), 20_000 - total_open)
        # 过期：剩余库存全部退回国库
        await self.m._execute(
            "UPDATE strategy_market_orders SET expires_at=? "
            "WHERE world_id=? AND seller_faction=? AND status='open'",
            (_now() - 1, self.wid, fid))
        await self.m._step_market(await self.m.get_world())
        f3 = await self.m.get_faction(fid)
        self.assertEqual(int(f3["treasury"][key]), 20_000)

    async def test_market_offer_freezes_coin_and_delivers_item(self):
        mcfg = self.m.main_cfg()["market"]
        mcfg.update({"npc_offer_interval_ticks": 1, "npc_offer_chance": 1.0,
                     "npc_offer_cooldown_hours": 0})
        market = next(n for n in await self.m.get_nodes(self.wid)
                      if n["type"] == "market")
        await self._own("u1", market)
        await self._give("u1", grain=5000)
        r = await self.m.market_list("u1", int(market["id"]), "grain", 100, 2)
        oid = int(r["order_id"])
        npc_ids = [int(x["id"]) for x in await self.m._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc'",
            (self.wid,))]
        for fid in npc_ids:
            f = await self.m.get_faction(fid)
            f["treasury"] = {"coin": 100_000, "grain": 0, "iron": 0,
                             "reserved_coin": 0}
            await self.m.save_faction(f)
        world = await self.m.get_world()
        world["tick_no"] = 1
        await self.m.save_world(world)
        await self.m._step_market_offers(world)
        offer = await self.m._fetchone(
            "SELECT * FROM strategy_market_offers WHERE world_id=? AND order_id=? "
            "AND status='pending' ORDER BY id DESC LIMIT 1", (self.wid, oid))
        self.assertIsNotNone(offer)
        buyer = int(offer["buyer_faction"])
        gross = int(offer["amount"]) * int(offer["unit_price"])
        f1 = await self.m.get_faction(buyer)
        self.assertEqual(int((f1["treasury"]).get("reserved_coin", 0)), gross)
        self.assertEqual(int(f1["treasury"]["coin"]), 100_000)
        coin_seller0 = int((await self.m.get_player("u1", self.wid))["coin"])
        res = await self.m.market_offer_accept("u1", int(offer["id"]))
        self.assertTrue(res["ok"])
        f2 = await self.m.get_faction(buyer)
        self.assertEqual(int(f2["treasury"]["coin"]), 100_000 - gross)
        self.assertEqual(int(f2["treasury"].get("reserved_coin", 0)), 0)
        self.assertEqual(int(f2["treasury"]["grain"]), int(offer["amount"]))
        coin_seller1 = int((await self.m.get_player("u1", self.wid))["coin"])
        self.assertGreater(coin_seller1, coin_seller0)

    async def test_npc_market_offer_respects_treasury_reserve(self):
        """§10.7：国库低于守财底线不收购；出价后 coin - reserved ≥ reserve。"""
        mcfg = self.m.main_cfg()["market"]
        mcfg.update({"npc_offer_interval_ticks": 1, "npc_offer_chance": 1.0,
                     "npc_offer_cooldown_hours": 0})
        market = next(n for n in await self.m.get_nodes(self.wid)
                      if n["type"] == "market")
        await self._give("u1", grain=10_000)
        await self.m.market_list("u1", int(market["id"]), "grain", 10_000, 2)
        npc_rows = await self.m._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc'",
            (self.wid,))

        async def _fund(coin: int):
            for row in npc_rows:
                f = await self.m.get_faction(int(row["id"]))
                f["treasury"] = {"coin": coin, "grain": 0, "iron": 0,
                                 "reserved_coin": 0}
                # 性格 trade_bias 会乘进报价概率（可能 <1），这里固定为 1
                # 避免「1.0 × 0.9 仍可能掷空」造成的偶发失败。
                f["personality"] = {"trade_bias": 1.0}
                await self.m.save_faction(f)

        # 全员低于最低守财底线（5000×0.5=2500）→ 不出价
        await _fund(1000)
        world = await self.m.get_world()
        world["tick_no"] = 1
        await self.m.save_world(world)
        await self.m._step_market_offers(world)
        self.assertFalse(await self.m.market_offers("u1"))
        # 国库 6000：出价冻结后仍须保留各势力守财底线
        await _fund(6000)
        world = await self.m.get_world()
        world["tick_no"] = 2
        await self.m.save_world(world)
        await self.m._step_market_offers(world)
        offers = await self.m.market_offers("u1")
        self.assertTrue(offers)
        for off in offers:
            buyer = await self.m.get_faction(int(off["buyer_faction"]))
            reserve = int(self.m._npc_reserve(buyer)["coin"])
            self.assertLessEqual(int(off["gross"]), max(0, 6000 - reserve))

    # ------------------------------------------------------------------
    # 视图片段（§17）
    # ------------------------------------------------------------------
    async def test_state_view_exposes_location_and_city_fields(self):
        gen = await self._char_of("u1")
        node = await self._owned_node("u1", ntype="city")
        node["level"] = 1
        node["potential"] = 5
        await self.m.save_node(node)
        await self._place(gen, int(node["id"]))
        await self.m.upgrade_node("u1", int(node["id"]))
        st = await self.m.get_state("u1")
        n = next(x for x in st["nodes"] if int(x["id"]) == int(node["id"]))
        self.assertEqual(int(n["potential"]), 5)
        self.assertIsNotNone(n["construction"])
        self.assertEqual(int(n["upgrade_cost"]["coin"]), 5000)
        self.assertTrue(any(int(r["id"]) == int(gen["id"])
                            for r in n["residents"]))
        ch = next(c for c in st["player"]["chars"]
                  if int(c["id"]) == int(gen["id"]))
        self.assertTrue(ch["can_move"])
        self.assertEqual(int(ch["location_node_id"]), int(node["id"]))
        # 任务片段：单独用另一名角色派本地任务
        other = next(c for c in st["player"]["chars"]
                     if int(c["id"]) != int(gen["id"]))
        await self._place(other, int(node["id"]))
        # 节点正在建造不影响任务
        r = await self.m.dispatch_task("u1", int(other["id"]), int(node["id"]),
                                       "farm", 1)
        st2 = await self.m.get_state("u1")
        task = next(t for t in st2["player"]["tasks"]
                    if int(t["id"]) == int(r["task_id"]))
        self.assertTrue(task["sustained"])
        self.assertGreater(int(task["next_settle_at"]), 0)
        self.assertEqual(int(task["origin_node_id"]), int(node["id"]))


class TestStrategyV440Round5(unittest.IsolatedAsyncioTestCase):
    """v4.4.0 第 5 轮：跨轮收口（哗变/遗迹/存档自愈/计略/赎金/体力）。"""

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
        self.m.main_cfg().setdefault("hex", {})["field_encounter_mode"] = "auto"
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        self.p1 = await self.m.enter_game("u1", "甲", node_id=await self._free_node())
        self.p2 = await self.m.enter_game("u2", "乙", node_id=await self._free_node())
        for uid in ("u1", "u2"):
            p = await self.m.get_player(uid, self.wid)
            p.update({"coin": 2_000_000, "grain": 200_000, "iron": 100_000})
            await self.m.save_player(p)
        await self.m._commit()

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _free_node(self) -> int:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        nodes.sort(key=lambda n: (n["type"] not in ("city", "pass", "branch"),
                                  -int(n.get("level", 1)), int(n["id"])))
        self.assertTrue(nodes)
        return int(nodes[0]["id"])

    async def _home(self, uid: str) -> dict:
        p = await self.m.get_player(uid, self.wid)
        f = await self.m.get_faction(int(p["faction_id"]))
        return await self.m.get_node(self.wid, int(f["capital_node_id"]))

    async def _own(self, uid: str) -> dict:
        p = await self.m.get_player(uid, self.wid)
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n["owner_faction"]) == 0 and n["type"] != "ruin")
        node["owner_faction"] = int(p["faction_id"])
        node["protect_until"] = 0
        node["siege_state"] = {}
        await self.m.save_node(node)
        return node

    async def _char(self, uid: str, role: str = "general"):
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == role:
                return c
        return None

    async def _place(self, char, node_id: int):
        c = await self.m.get_char(int(char["id"]))
        c["location"] = {"node_id": int(node_id)}
        c["status"] = "idle"
        await self.m.save_char(c)
        return c

    async def _new_char(self, uid: str, role: str, **kw) -> dict:
        p = await self.m.get_player(uid, self.wid)
        rng = random.Random(f"{uid}-{role}-{kw.get('salt', 0)}")
        c = self.R.generate_character(rng, rarity="rare", role=role,
                                      name_pools={})
        c.update({"world_id": self.wid, "faction_id": int(p["faction_id"]),
                  "owner_user_id": uid, "loyalty": 90, "status": "idle",
                  "location": {}})
        c.update(kw.get("extra") or {})
        await self.m.insert_char(c)
        return await self.m.get_char(int(c["id"]))

    # --- 1. 哗变只伤本城驻守角色 -------------------------------------
    async def test_riot_injures_only_residents_of_that_node(self):
        home = await self._home("u1")
        other = await self._own("u1")
        a = await self._char("u1")
        b = await self._new_char("u1", "official")
        # 本城只留 a 一名驻守，其余 u1 角色全放到 other，断言才具确定性
        for c0 in await self.m.get_chars(owner_user_id="u1", world_id=self.wid):
            await self._place(c0, int(home["id"])
                              if int(c0["id"]) == int(a["id"])
                              else int(other["id"]))
        self.assertEqual(
            (await self.m.get_char(int(a["id"])))["status"], "idle")
        await self._place(b, int(other["id"]))
        await self.m._riot_injure_char(
            await self.m.get_world(), home, _FixedRng(-1))
        a2 = await self.m.get_char(int(a["id"]))
        b2 = await self.m.get_char(int(b["id"]))
        self.assertGreater(int(a2["injury"]), 0)
        self.assertEqual(int(b2["injury"]), 0)

    # --- 1b. v80→v83 迁移：潜力回填 + 新增列幂等 ---------------------
    async def test_v80_upgrade_backfills_potential_and_adds_columns(self):
        migration = _imp("data.migration")
        home = await self._home("u1")
        await self.conn.execute("ALTER TABLE strategy_nodes DROP COLUMN potential")
        await self.conn.execute(
            "ALTER TABLE strategy_nodes DROP COLUMN construction_json")
        await self.conn.execute(
            "ALTER TABLE strategy_nodes DROP COLUMN develop_ready_at")
        await self.conn.execute(
            "ALTER TABLE strategy_factions DROP COLUMN salary_json")
        await self.conn.execute(
            "ALTER TABLE strategy_characters DROP COLUMN personality_json")
        await self.conn.commit()
        await migration._ensure_latest_columns(self.conn)
        n = await self.m.get_node(self.wid, int(home["id"]))
        self.assertEqual(int(n["potential"]),
                         int(self.R.NODE_POTENTIAL_BY_TYPE[n["type"]]))
        # 重复执行幂等（不得因列已存在报错，也不得把显式潜力重置为 3）
        n["potential"] = 4
        await self.m.save_node(n)
        await self.conn.commit()
        await migration._ensure_latest_columns(self.conn)
        n2 = await self.m.get_node(self.wid, int(home["id"]))
        self.assertEqual(int(n2["potential"]), 4)
        tables = {
            "strategy_nodes": {"potential", "construction_json",
                               "develop_ready_at"},
            "strategy_factions": {"salary_json"},
            "strategy_characters": {"personality_json"},
        }
        for table, cols in tables.items():
            async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
                got = {r[1] for r in await cur.fetchall()}
            self.assertTrue(cols <= got, (table, cols - got))

    # --- 2. 遗迹不能派遣/编军/建造/升级 -------------------------------
    async def test_ruin_rejects_dispatch_army_build_upgrade(self):
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["type"] == "ruin")
        node["owner_faction"] = int(self.p1["faction_id"])
        await self.m.save_node(node)
        c = await self._char("u1")
        c = await self.m.get_char(int(c["id"]))
        c["location"] = {"node_id": int(node["id"])}
        await self.m.save_char(c)
        with self.assertRaises(self.StrategyError):
            await self.m.dispatch_task("u1", int(c["id"]), int(node["id"]),
                                       "farm", 1)
        with self.assertRaises(self.StrategyError):
            await self.m.create_army("u1", int(c["id"]), [], int(node["id"]),
                                     "sword_shield", 100)
        with self.assertRaises(self.StrategyError):
            await self.m.build("u1", int(node["id"]), "manor")
        with self.assertRaises(self.StrategyError):
            await self.m.upgrade_node("u1", int(node["id"]))

    # --- 3. 旧存档 idle 落点自愈 --------------------------------------
    async def test_repair_relocates_invalid_idle_location(self):
        c = await self._char("u1")
        c = await self.m.get_char(int(c["id"]))
        c["location"] = {"node_id": 999_999}
        await self.m.save_char(c)
        p = await self.m.get_player("u1", self.wid)
        await self.m._repair_player_state(await self.m.get_world(), p)
        c2 = await self.m.get_char(int(c["id"]))
        self.assertNotEqual(self.R.char_node_id(c2), 999_999)
        dest = await self.m.get_node(self.wid, self.R.char_node_id(c2))
        self.assertIsNotNone(dest)
        self.assertEqual(int(dest["owner_faction"]), int(self.p1["faction_id"]))
        # 敌方据点同样回退
        enemy = await self._own("u2")
        c3 = await self.m.get_char(int(c["id"]))
        c3["location"] = {"node_id": int(enemy["id"])}
        await self.m.save_char(c3)
        await self.m._repair_player_state(await self.m.get_world(), p)
        c4 = await self.m.get_char(int(c["id"]))
        self.assertNotEqual(self.R.char_node_id(c4), int(enemy["id"]))
        self.assertEqual(int((await self.m.get_node(
            self.wid, self.R.char_node_id(c4)))["owner_faction"]),
            int(self.p1["faction_id"]))

    # --- 4. 计略任务召回不发通用产出 ---------------------------------
    async def test_plot_recall_has_no_generic_payout(self):
        strat = await self._new_char("u1", "strategist")
        enemy = await self._own("u2")
        # char_id=0：后端自动选军师（本用例仅一名）
        r = await self.m.dispatch_plot(
            "u1", 0, "rumor", int(enemy["id"]))
        task = (await self.m.get_tasks(self.wid, status="active"))[0]
        self.assertEqual(int(task["character_id"]), int(strat["id"]))
        p0 = await self.m.get_player("u1", self.wid)
        coin0, grain0 = int(p0["coin"]), int(p0["grain"])
        out = await self.m.recall_task("u1", int(r["task_id"]))
        self.assertTrue(out["ok"])
        self.assertEqual(int(out["payout"].get("amount", 0)), 0)
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p1["coin"]), coin0)
        self.assertEqual(int(p1["grain"]), grain0)
        c2 = await self.m.get_char(int(strat["id"]))
        self.assertEqual(c2["status"], "idle")

    # --- 5. 同联盟据点不可施展计略 -----------------------------------
    async def test_plot_rejects_allied_node(self):
        await self._new_char("u1", "strategist")
        f1 = await self.m.get_faction(int(self.p1["faction_id"]))
        f2 = await self.m.get_faction(int(self.p2["faction_id"]))
        f1["alliance_id"] = int(f2["id"])
        f2["alliance_id"] = int(f2["id"])
        await self.m.save_faction(f1)
        await self.m.save_faction(f2)
        ally_node = await self._home("u2")
        with self.assertRaises(self.StrategyError):
            await self.m.dispatch_plot(
                "u1", 0, "rumor", int(ally_node["id"]))

    # --- 6. 势力崩溃释放大牢俘虏并清赎金 -----------------------------
    async def test_collapse_releases_captives_and_clears_ransom(self):
        jail = await self._home("u1")
        cap = await self._new_char("u2", "general")
        cap = await self.m.get_char(int(cap["id"]))
        cap.update({"status": "captive", "captor_faction": int(self.p1["faction_id"]),
                    "imprisoned_node_id": int(jail["id"])})
        cap["location"] = {"node_id": int(jail["id"]),
                           "home_faction": int(self.p2["faction_id"]),
                           "home_owner": "u2"}
        await self.m.save_char(cap)
        await self.m.set_ransom("u1", int(cap["id"]), 5000)
        # 崩溃势力的研究角色也要回到 idle，不能永久卡在 research
        rc = await self._new_char("u1", "strategist")
        rc = await self.m.get_char(int(rc["id"]))
        rc["status"] = "research"
        await self.m.save_char(rc)
        f = await self.m.get_faction(int(self.p1["faction_id"]))
        members = await self.m.get_members(int(f["id"]))
        nodes = await self.m.get_nodes(self.wid)
        await self.m._collapse_faction(await self.m.get_world(), f,
                                       members, nodes, {})
        c2 = await self.m.get_char(int(cap["id"]))
        self.assertIn(c2["status"], ("idle", "wander"))
        self.assertEqual(int(c2.get("captor_faction") or 0), 0)
        rc2 = await self.m.get_char(int(rc["id"]))
        self.assertEqual(rc2["status"], "idle")
        world = await self.m.get_world()
        self.assertNotIn(str(int(cap["id"])),
                         (world.get("data") or {}).get("ransoms") or {})

    # --- 7. 释放/入册清理赎金 ----------------------------------------
    async def test_release_and_recruit_clear_ransom(self):
        jail = await self._home("u1")
        cap = await self._new_char("u2", "general")
        cap = await self.m.get_char(int(cap["id"]))
        cap.update({"status": "captive", "captor_faction": int(self.p1["faction_id"]),
                    "imprisoned_node_id": int(jail["id"])})
        cap["location"] = {"node_id": int(jail["id"]),
                           "home_faction": int(self.p2["faction_id"]),
                           "home_owner": "u2"}
        await self.m.save_char(cap)
        await self.m.set_ransom("u1", int(cap["id"]), 5000)
        await self.m.release_captive("u1", int(cap["id"]))
        world = await self.m.get_world()
        self.assertNotIn(str(int(cap["id"])),
                         (world.get("data") or {}).get("ransoms") or {})
        # 入册路径
        cap2 = await self._new_char("u2", "official")
        cap2 = await self.m.get_char(int(cap2["id"]))
        cap2.update({"status": "captive", "captor_faction": int(self.p1["faction_id"]),
                     "imprisoned_node_id": int(jail["id"])})
        cap2["location"] = {"node_id": int(jail["id"]),
                            "home_faction": int(self.p2["faction_id"]),
                            "home_owner": "u2"}
        await self.m.save_char(cap2)
        await self.m.set_ransom("u1", int(cap2["id"]), 5000)
        await self.m.recruit_captive("u1", int(cap2["id"]))
        world2 = await self.m.get_world()
        self.assertNotIn(str(int(cap2["id"])),
                         (world2.get("data") or {}).get("ransoms") or {})
        # tick GC：历史遗留的孤儿赎金条目（角色已在野/陨落）自动清理
        data = world2.get("data") or {}
        data.setdefault("ransoms", {})["999999"] = {"amount": 1}
        world2["data"] = data
        await self.m.save_world(world2)
        await self.m._step_captives(world2)
        world3 = await self.m.get_world()
        self.assertNotIn("999999", (world3.get("data") or {}).get("ransoms") or {})

    # --- 8. 持续任务首小时体力不双扣 --------------------------------
    async def test_sustained_task_first_hour_not_double_stamina(self):
        node = await self._own("u1")
        c = await self._char("u1")
        await self._place(c, int(node["id"]))
        r = await self.m.dispatch_task("u1", int(c["id"]), int(node["id"]),
                                       "farm", 1)
        c1 = await self.m.get_char(int(c["id"]))
        self.assertEqual(int(c1["stamina"]), 80)  # 派遣已扣首小时
        for expected in (80, 60):
            task = (await self.m.get_tasks(self.wid, status="active"))[0]
            payout = dict(task["payout"])
            payout["next_settle_at"] = _now() - 10
            await self.m._execute(
                "UPDATE strategy_tasks SET payout_json=? WHERE id=?",
                (json.dumps(payout, ensure_ascii=False), int(r["task_id"])))
            await self.m._step_settle_tasks(await self.m.get_world())
            c2 = await self.m.get_char(int(c["id"]))
            self.assertEqual(int(c2["stamina"]), expected)


def R_node_potential(node: dict) -> int:
    return _imp("core.strategy_rules").node_potential(node)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class _NoCaptureRng:
    """永不触发被俘判定（用于验证城陷存活者落点）。"""

    def random(self):
        return 0.999

    def randint(self, a, b):
        return int(a)

    def choice(self, seq):
        return seq[0]

    def choices(self, seq, weights=None):
        return [seq[0]]

    def uniform(self, a, b):
        return float(a)


class TestStrategyV440Round6(unittest.IsolatedAsyncioTestCase):
    """v4.4.0 第 6 轮：遗迹落点 / 城陷存活者 / 旧档升级冒烟。"""

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
        for uid in ("u1", "u2"):
            await self.db.create_player(self.Player(
                user_id=uid, gold=10_000_000, user_name=f"测试{uid}"))
        self.m = self.StrategyManager(self.db)
        self.m.main_cfg().setdefault("hex", {})["field_encounter_mode"] = "auto"
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        self.p1 = await self.m.enter_game("u1", "甲", node_id=await self._free())
        self.p2 = await self.m.enter_game("u2", "乙", node_id=await self._free())
        for uid in ("u1", "u2"):
            p = await self.m.get_player(uid, self.wid)
            p.update({"coin": 2_000_000, "grain": 200_000, "iron": 100_000})
            await self.m.save_player(p)
        await self.m._commit()

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _free(self) -> int:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        self.assertTrue(nodes)
        return int(nodes[0]["id"])

    async def _home(self, uid: str) -> dict:
        p = await self.m.get_player(uid, self.wid)
        f = await self.m.get_faction(int(p["faction_id"]))
        return await self.m.get_node(self.wid, int(f["capital_node_id"]))

    async def _char(self, uid: str, role: str = "general") -> dict:
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == role:
                return c
        self.fail(f"{uid} 缺少 {role} 角色")

    async def test_disband_on_owned_ruin_relocates_members(self):
        """解散驻留遗迹的军团：角色不得以 idle 状态留在遗迹（§3）。"""
        p = await self.m.get_player("u1", self.wid)
        fid = int(p["faction_id"])
        ruin = next(n for n in await self.m.get_nodes(self.wid)
                    if n["type"] == "ruin")
        ruin["owner_faction"] = fid
        await self.m.save_node(ruin)
        c = await self.m.get_char(int((await self._char("u1"))["id"]))
        c["status"] = "army"
        c["location"] = {"node_id": int(ruin["id"])}
        await self.m.save_char(c)
        army = {"world_id": self.wid, "faction_id": fid, "owner_user_id": "u1",
                "name": "遗迹驻军", "node_id": int(ruin["id"]),
                "characters": [int(c["id"])], "troops": 100, "grain": 10,
                "state": "idle", "path": [], "edge_pos": 0.0,
                "troop_type": "sword_shield", "veteran": 0, "morale": 80}
        await self.m.insert_army(army)
        await self.m._commit()
        a = (await self.m.get_armies(self.wid, owner_user_id="u1"))[0]
        await self.m.disband_army("u1", int(a["id"]))
        c2 = await self.m.get_char(int(c["id"]))
        nid = self.R.char_node_id(c2)
        node = await self.m.get_node(self.wid, nid) if nid else None
        self.assertNotEqual(nid, int(ruin["id"]))
        self.assertTrue(nid == 0 or (
            node and node["type"] in self.R.CHAR_DWELL_NODE_TYPES
            and int(node["owner_faction"]) == fid))

    async def test_retreat_does_not_land_on_owned_ruin(self):
        """只剩遗迹时撤退不得把军团/角色停在遗迹（§3/§4.4）。"""
        p = await self.m.get_player("u1", self.wid)
        fid = int(p["faction_id"])
        nodes = await self.m.get_nodes(self.wid)
        ruin = next(n for n in nodes if n["type"] == "ruin")
        for n in nodes:
            if int(n["id"]) != int(ruin["id"]) and \
                    int(n.get("owner_faction") or 0) == fid:
                n["owner_faction"] = 0
                await self.m.save_node(n)
        ruin["owner_faction"] = fid
        await self.m.save_node(ruin)
        enemy = await self._home("u2")
        c = await self.m.get_char(int((await self._char("u1"))["id"]))
        c["status"] = "army"
        c["location"] = {"node_id": int(enemy["id"])}
        await self.m.save_char(c)
        army = {"world_id": self.wid, "faction_id": fid, "owner_user_id": "u1",
                "name": "残军", "node_id": int(enemy["id"]),
                "characters": [int(c["id"])], "troops": 100, "grain": 10,
                "state": "idle", "path": [], "edge_pos": 0.0,
                "troop_type": "sword_shield", "veteran": 0, "morale": 80}
        await self.m.insert_army(army)
        await self.m._commit()
        a = (await self.m.get_armies(self.wid, owner_user_id="u1"))[0]
        await self.m.retreat_army("u1", int(a["id"]))
        a2 = await self.m.get_army(int(a["id"]))
        self.assertTrue(a2 is None or int(a2.get("node_id") or 0) != int(ruin["id"]))
        c2 = await self.m.get_char(int(c["id"]))
        self.assertNotEqual(self.R.char_node_id(c2), int(ruin["id"]))

    async def test_city_fall_survivor_and_tick_sweep_relocate(self):
        """城陷未被俘的 idle 角色不得滞留敌城；tick 安全网同样自愈。"""
        node = await self._home("u2")
        c = await self.m.get_char(int((await self._char("u2"))["id"]))
        c["status"] = "idle"
        c["location"] = {"node_id": int(node["id"])}
        await self.m.save_char(c)
        p1 = await self.m.get_player("u1", self.wid)
        node = await self.m.get_node(self.wid, int(node["id"]))
        node["owner_faction"] = int(p1["faction_id"])
        await self.m.save_node(node)
        await self.m._commit()
        with mock.patch.object(self.m, "_rng", return_value=_NoCaptureRng()):
            await self.m.roll_captures(
                await self.m.get_world(), node, [], "city_fall",
                int(p1["faction_id"]))
        c2 = await self.m.get_char(int(c["id"]))
        self.assertNotEqual(self.R.char_node_id(c2), int(node["id"]))
        # 再制造一个“任务中滞留敌城”的旧档态，tick 的落点安全网须自愈
        enemy = await self.m.get_node(self.wid, int(node["id"]))
        c2 = await self.m.get_char(int(c["id"]))
        c2["status"] = "task"
        c2["location"] = {"node_id": int(enemy["id"])}
        await self.m.save_char(c2)
        res = await self.m.tick()
        self.assertTrue(res.get("ok"), res)
        c3 = await self.m.get_char(int(c["id"]))
        nid = self.R.char_node_id(c3)
        self.assertNotEqual(nid, int(enemy["id"]))

    async def test_ransom_gc_drops_changed_captor(self):
        """旧赎金条目在关押方变化后必须由 tick GC 清掉，避免旧价复用。"""
        jail = await self._home("u1")
        c = await self.m.get_char(int((await self._char("u2"))["id"]))
        c.update({"status": "captive", "captor_faction": int(self.p1["faction_id"]),
                  "imprisoned_node_id": int(jail["id"])})
        c["location"] = {"node_id": int(jail["id"]),
                         "home_faction": int(self.p2["faction_id"]),
                         "home_owner": "u2"}
        await self.m.save_char(c)
        await self.m.set_ransom("u1", int(c["id"]), 5000)
        # 模拟被第三方重新擒获：关押方变化，旧报价失效
        c = await self.m.get_char(int(c["id"]))
        c["captor_faction"] = int(self.p2["faction_id"])
        await self.m.save_char(c)
        world = await self.m.get_world()
        await self.m._step_captives(world)
        world2 = await self.m.get_world()
        self.assertNotIn(str(int(c["id"])),
                         (world2.get("data") or {}).get("ransoms") or {})

    async def test_v80_world_upgrade_and_location_self_heal(self):
        """v80 旧档：真实 MigrationManager 升级 + 进场自愈 + 首个 tick。"""
        import aiosqlite
        p2 = await self.m.get_player("u2", self.wid)
        enemy = await self._home("u2")
        ruin = next(n for n in await self.m.get_nodes(self.wid)
                    if n["type"] == "ruin")
        chars = await self.m.get_chars(owner_user_id="u1", world_id=self.wid)
        locs = [{"node_id": int(enemy["id"])},
                {"node_id": int(ruin["id"])}, {"node_id": 999_999}, {}]
        for i, c in enumerate(chars):
            c["location"] = locs[i % len(locs)]
            c["status"] = "idle"
            await self.m.save_char(c)
        w = await self.m.get_world()
        data = dict(w.get("data") or {})
        for key in ("npc_develop", "ransoms", "secret"):
            data.pop(key, None)
        w["data"] = data
        await self.m.save_world(w)
        for table, col in (
                ("strategy_nodes", "potential"),
                ("strategy_nodes", "construction_json"),
                ("strategy_nodes", "develop_ready_at"),
                ("strategy_factions", "salary_json"),
                ("strategy_characters", "personality_json")):
            await self.conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        await self.conn.execute("UPDATE db_info SET version=80")
        await self.conn.commit()
        await self.db.close()

        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        db = self.DataBase(self.db_path)
        db.conn = conn
        db.ext = self.DatabaseExtended(conn)
        self.db, self.conn = db, conn
        await self.migration.MigrationManager(conn, None).migrate()
        m = self.StrategyManager(db)
        p = await m.enter_game("u1", "甲")
        self.assertIsNotNone(p)
        nodes = await m.get_nodes(self.wid)
        for n in nodes:
            self.assertEqual(int(n["potential"]),
                             int(self.R.NODE_POTENTIAL_BY_TYPE[n["type"]]))
        for c in await m.get_chars(world_id=self.wid):
            if c.get("status") == "idle" and self.R.char_node_id(c) > 0:
                n = await m.get_node(self.wid, self.R.char_node_id(c))
                self.assertIsNotNone(n)
                self.assertIn(n["type"], self.R.CHAR_DWELL_NODE_TYPES)
        res = await m.tick()
        self.assertTrue(res.get("ok"), res)
