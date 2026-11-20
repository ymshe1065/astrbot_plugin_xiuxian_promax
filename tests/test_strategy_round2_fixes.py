# tests/test_strategy_round2_fixes.py
# 「仙域争霸」v4.1.0 第二轮冲突/BUG 修复回归：
#   谣言单一乘区 / 野战不吃守城加成 / 联盟动员令上溯 / 非 ruin 匪寨劫掠 /
#   溃败重整期禁出 / 互市返税按实际成交额 / 哗变同 tick 去重 / 小事件概率配置
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


class TestStrategyRound2Fixes(unittest.IsolatedAsyncioTestCase):
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

    async def _enter(self, user_id: str, nickname: str,
                     referral: str = "") -> dict:
        return await self.m.enter_game(user_id, nickname, referral=referral,
                                       node_id=await self._free_node())

    async def _char_of(self, uid: str, role: str = "general"):
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == role:
                return c
        return None

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

    async def _fetchall(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------
    # 1. 谣言：只剩 plot_output_mult 一个乘区（0.8 而非 0.64）
    # ------------------------------------------------------------------
    async def test_rumor_single_output_multiplier(self):
        fid = int(self.p1["faction_id"])
        target_fid = int(self.p2["faction_id"])
        target = await self._owned_node("u2")
        c = await self._char_of("u1", "general")
        p = await self.m.get_player("u1", self.wid)
        world = await self.m.get_world()
        task = {"id": 9901, "task_type": "plot_rumor", "faction_id": fid,
                "node_id": int(target["id"]), "target_faction": target_fid,
                "payout": {"plot": "rumor", "target": int(target["id"]),
                           "ticks": 2}}
        with mock.patch.object(self.R, "plot_success_rate", return_value=100.0):
            outline = await self.m._settle_plot_task(world, task, c, p)
        self.assertTrue(outline["success"])

        node = await self.m.get_node(self.wid, int(target["id"]))
        self.assertAlmostEqual(self.m.plot_output_mult(node), 0.8, places=3)

        rows = await self._fetchall(
            "SELECT data_json FROM strategy_events WHERE world_id=? "
            "AND event_id='rumor' ORDER BY id DESC LIMIT 1", (self.wid,))
        self.assertTrue(rows, "谣言事件未写入")
        effects = (json.loads(rows[0]["data_json"]) or {}).get("effects") or []
        self.assertTrue(effects)
        # 事件只作展示：绝不能出现 node_output（否则与 plot_output_mult 叠乘）
        self.assertTrue(all(e.get("type") == "plot_effect" for e in effects))
        self.assertFalse(any(e.get("type") == "node_output" for e in effects))

    # ------------------------------------------------------------------
    # 2. 野战/关隘战不再默认给守方守城加成
    # ------------------------------------------------------------------
    async def test_sim_view_defending_node_only_for_siege_or_wall(self):
        base = {"statuses": {}, "terrain": {}, "temp_terrain": {},
                "round": 1, "size": (13, 9)}
        unit = {"pos": (0, 0), "uid": 1, "side": "b", "army_id": 10,
                "faction_id": 2, "troop_type": "sword_shield", "troops": 100,
                "morale": 100, "veteran": 0}

        field = dict(base, mode="field")
        self.assertFalse(self.H._sim_view(field, unit)["defending_node"])

        siege = dict(base, mode="siege")
        self.assertTrue(self.H._sim_view(siege, unit)["defending_node"])

        on_wall = dict(base, mode="field",
                       terrain={self.H._key(0, 0): self.H.T_WALL})
        self.assertTrue(self.H._sim_view(on_wall, unit)["defending_node"])

        atk = dict(unit, side="a")
        self.assertFalse(self.H._sim_view(siege, atk)["defending_node"])

    # ------------------------------------------------------------------
    # 3. 联盟动员令：成员势力上溯到盟主查 buff
    # ------------------------------------------------------------------
    async def test_alliance_mobilization_buff_reaches_members(self):
        host_fid = await self._create_multi_faction()
        member_fid = int(self.p2["faction_id"])
        self.assertNotEqual(member_fid, host_fid)
        f = await self.m.get_faction(member_fid)
        f["alliance_id"] = int(host_fid)
        await self.m.save_faction(f)
        await self.m._commit()

        await self._give("u1", merit=1000)
        await self.m.mobilization_issue("u1", "march")
        self.assertAlmostEqual(
            await self.m._faction_buff(member_fid, "grain_cost_mult"),
            0.7, places=3)
        # 盟主自身同样生效
        self.assertAlmostEqual(
            await self.m._faction_buff(host_fid, "grain_cost_mult"),
            0.7, places=3)

    # ------------------------------------------------------------------
    # 4. 非 ruin 类型的匪寨也会周期劫掠
    # ------------------------------------------------------------------
    async def test_bandit_raids_non_ruin_node(self):
        mine = await self._owned_node("u1")
        mine["stock"] = {"coin": 1000, "grain": 1000, "iron": 1000}
        mine["morale"] = 80
        await self.m.save_node(mine)
        await self.m._commit()

        nodes = await self.m.get_nodes(self.wid)
        lair = next(n for n in nodes
                    if int(n["id"]) in [int(x) for x in (mine.get("adj") or [])]
                    and int(n["owner_faction"]) == 0)
        # 强制改成城市类型，验证不再依赖 type=="ruin"
        lair["type"] = "city"
        interval = int((self.m.main_cfg().get("bandit") or {})
                       .get("raid_interval_ticks", 8))
        world = await self.m.get_world()
        tick = int(world.get("tick_no", 0) or 0)
        lair["lair"] = {"bandit": True, "last_raid_tick": tick - interval}
        await self.m.save_node(lair)
        await self.m._commit()

        world = await self.m.get_world()
        raided = await self.m._step_bandit_raids(world)
        self.assertGreaterEqual(raided, 1)

        mine = await self.m.get_node(self.wid, int(mine["id"]))
        stock = mine.get("stock") or {}
        self.assertEqual(int(stock.get("grain", 0)), 700)  # 扣 30%
        self.assertEqual(int(stock.get("coin", 0)), 700)
        self.assertEqual(int(mine.get("morale", 0)), 75)   # -5
        lair = await self.m.get_node(self.wid, int(lair["id"]))
        self.assertEqual(int((lair.get("lair") or {}).get("last_raid_tick")),
                         tick)

    # ------------------------------------------------------------------
    # 5. 溃败重整期禁止出征/补员/讨伐/出击/介入
    # ------------------------------------------------------------------
    async def test_reorganizing_army_cannot_act(self):
        node = await self._owned_node("u1")
        gen = await self._char_of("u1", "general")
        r = await self.m.create_army(
            "u1", int(gen["id"]), [], int(node["id"]), "sword_shield", 100)
        aid = int(r["army_id"])
        army = await self.m.get_army(aid)
        world = await self.m.get_world()
        army["reorganize_until_tick"] = int(world.get("tick_no", 0)) + 3
        await self.m.save_army(army)
        await self.m._commit()

        with self.assertRaises(self.StrategyError) as cm:
            await self.m.march("u1", aid, int(node["id"]))
        self.assertIn("重整", str(cm.exception))

        with self.assertRaises(self.StrategyError) as cm:
            await self.m.refill_army("u1", aid, 50)
        self.assertIn("重整", str(cm.exception))

    # ------------------------------------------------------------------
    # 6. 互市返税按实际成交额（partial 买入不超返）
    # ------------------------------------------------------------------
    async def test_market_tax_refund_uses_actual_cost(self):
        node = await self._owned_node("u1")
        node["type"] = "city"
        node["buildings"] = [{"id": "market_stall", "level": 2}]
        await self.m.save_node(node)
        await self.m._commit()

        npc = await self.m._fetchone(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "AND alive=1 LIMIT 1", (self.wid,))
        self.assertIsNotNone(npc, "缺少 NPC 势力")
        await self.m._execute(
            "INSERT INTO strategy_market_orders (world_id, node_id, "
            "seller_faction, seller_user, item_type, amount, unit_price, "
            "status, created_at, expires_at) VALUES (?,?,?,'','grain',10,100,"
            "'open',?,?)",
            (self.wid, int(node["id"]), int(npc["id"]), _now(),
             _now() + 3600))
        await self.m._commit()
        oid = int((await self.m._fetchone(
            "SELECT id FROM strategy_market_orders ORDER BY id DESC LIMIT 1"))["id"])

        before = int((await self.m.get_player("u1", self.wid))["coin"])
        res = await self.m.market_buy("u1", oid, 5)  # 只买 5 件
        cost = 5 * 100
        expect_refund = int(round(cost * 0.01 * 2))
        self.assertEqual(int(res.get("cost", 0)), cost)
        self.assertEqual(int(res.get("tax_refund", 0)), expect_refund)
        after = int((await self.m.get_player("u1", self.wid))["coin"])
        self.assertEqual(after, before - cost + expect_refund)
        # 订单剩余 5 件
        row = await self.m._fetchone(
            "SELECT amount, status FROM strategy_market_orders WHERE id=?",
            (oid,))
        self.assertEqual(int(row["amount"]), 5)
        self.assertEqual(row["status"], "open")

    # ------------------------------------------------------------------
    # 7. 势力事件「哗变」与低民心哗变同 tick 互斥
    # ------------------------------------------------------------------
    async def test_riot_deduped_between_event_and_morale(self):
        a = await self._owned_node("u1")
        b = await self._owned_node("u2")
        for node in (a, b):
            node["morale"] = 0
            node["npc_garrison"] = {"troops": 1000, "morale": 70}
            await self.m.save_node(node)
        world = await self.m.get_world()
        tick = int(world.get("tick_no", 0) or 0)
        data = world.get("data") or {}
        data["riot_tick_nodes"] = {str(a["id"]): tick}
        world["data"] = data
        await self.m.save_world(world)
        await self.m._commit()

        async def _noop(*args, **kwargs):
            return None

        world = await self.m.get_world()
        with mock.patch.object(self.R, "riot_check", return_value=True), \
                mock.patch.object(self.m, "_riot_injure_char", _noop):
            await self.m._step_recovery(world)

        a2 = await self.m.get_node(self.wid, int(a["id"]))
        b2 = await self.m.get_node(self.wid, int(b["id"]))
        # 已标记：不被扣兵；未标记：扣 30%
        self.assertEqual(int((a2.get("npc_garrison") or {})["troops"]), 1000)
        self.assertEqual(int((b2.get("npc_garrison") or {})["troops"]), 700)

    # ------------------------------------------------------------------
    # 8. 任务小事件概率：配置值直接生效（不再被 10% 硬编码吞掉）
    # ------------------------------------------------------------------
    def test_mini_event_roll_honours_configured_chance(self):
        rng = random.Random(7)
        self.assertIsNone(self.R.mini_event_roll(rng, 0.0))
        self.assertIsNotNone(self.R.mini_event_roll(rng, 1.0))


if __name__ == "__main__":
    unittest.main()
