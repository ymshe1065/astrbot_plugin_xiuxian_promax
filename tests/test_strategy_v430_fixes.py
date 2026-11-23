# tests/test_strategy_v430_fixes.py
# 「仙域争霸」v4.3.0 全量审计修复定向回归：
#   劝降钳制 90% / 恶名除外 / 成功迁点+知遇 / 释放主副门槛 /
#   计略停战拦截 / 粮队满拒发 / 敌军 path 遮罩 / 编军人数上限 /
#   defect_loyalty 配置口径 / 世界密钥混入 rng
import os
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


R = _imp("core.strategy_rules")


class TestStrategyV430Fixes(unittest.IsolatedAsyncioTestCase):
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
        for uid in ("u1", "u2"):
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
        for uid in ("u1", "u2"):
            await self._give(uid, coin=500_000, grain=50_000, iron=5_000)

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _enter(self, uid: str, nick: str):
        nodes = await self.m.get_nodes(self.wid)
        free = [n for n in nodes if int(n.get("owner_faction", 0) or 0) == 0
                and n.get("type") != "ruin"]
        return await self.m.enter_game(uid, nick, node_id=int(free[0]["id"]))

    async def _give(self, uid: str, **kw):
        p = await self.m.get_player(uid, self.wid)
        for k, v in kw.items():
            p[k] = int(v)
        await self.m.save_player(p)
        await self.m._commit()

    async def _idle_char(self, uid: str, **extra):
        p = await self.m.get_player(uid, self.wid)
        c = R.generate_character(
            __import__("random").Random(42), rarity="rare", role="general",
            name_pools=self.m.cfg.get("names") or {})
        c.update({
            "world_id": self.wid, "faction_id": p["faction_id"],
            "owner_user_id": uid, "status": "idle", "loyalty": 70,
            "injury": 0, "stamina": 100, "location": {},
        })
        c.update(extra)
        await self.m.insert_char(c)
        await self.m._commit()
        return await self.m.get_char(c["id"])

    async def test_defect_loyalty_config_is_zero(self):
        self.assertEqual(
            int((self.m.main_cfg().get("captive") or {}).get("defect_loyalty", -1)),
            0)

    async def test_persuade_rate_clamped_at_90(self):
        persuader = {"stats": {"charm": 90, "wisdom": 90}}
        rate = R.persuade_rate(1, persuader, reputation=100)
        self.assertLessEqual(rate, 0.90)

    async def test_world_secret_mixed_into_rng(self):
        self.m._world_secret = "secret_a"
        a = self.m._rng("x", 1, 2).random()
        self.m._world_secret = "secret_b"
        b = self.m._rng("x", 1, 2).random()
        self.assertNotEqual(a, b)

    async def test_enemy_army_path_masked_in_get_state(self):
        p1 = await self.m.get_player("u1", self.wid)
        p2 = await self.m.get_player("u2", self.wid)
        nodes = await self.m.get_nodes(self.wid)
        n1 = next(n for n in nodes if int(n["owner_faction"]) == int(p1["faction_id"]))
        gen = await self._idle_char("u2")
        army = {
            "world_id": self.wid, "faction_id": int(p2["faction_id"]),
            "owner_user_id": "u2", "name": "敌骑", "node_id": int(n1["id"]),
            "state": "idle", "path": [], "edge_pos": 0.0,
            "troops": 500, "troop_type": "flying_sword", "morale": 80,
            "veteran": 0, "grain": 100, "characters": [int(gen["id"])],
            "siege_engine": 0,
        }
        aid = await self.m.insert_army(army)
        army["id"] = aid
        army["state"] = "march"
        army["path"] = [1, 2, 3]
        await self.m.save_army(army)
        await self.m._commit()
        state = await self.m.get_state("u1")
        enemy = next((a for a in state["armies"] if a["id"] == aid), None)
        self.assertIsNotNone(enemy)
        self.assertEqual(enemy["path"], [])
        # 自己看己方仍可见完整 path
        state2 = await self.m.get_state("u2")
        mine = next((a for a in state2["armies"] if a["id"] == aid), None)
        self.assertEqual(mine["path"], [1, 2, 3])

    async def test_create_army_rejects_over_max_chars(self):
        p = await self.m.get_player("u1", self.wid)
        nodes = await self.m.get_nodes(self.wid)
        home = next(n for n in nodes if int(n["owner_faction"]) == int(p["faction_id"]))
        leader = await self._idle_char("u1")
        m1 = await self._idle_char("u1")
        m2 = await self._idle_char("u1")
        m3 = await self._idle_char("u1")
        with self.assertRaises(self.StrategyError):
            await self.m.create_army(
                "u1", leader["id"], [m1["id"], m2["id"], m3["id"]],
                int(home["id"]), "flying_sword", 200)

    async def test_plot_blocks_truce(self):
        p1 = await self.m.get_player("u1", self.wid)
        p2 = await self.m.get_player("u2", self.wid)
        f1 = await self.m.get_faction(p1["faction_id"])
        self.m._set_relation(f1.get("diplomacy") or {}, int(p2["faction_id"]),
                             R.REL_TRUCE, until=_now() + 86400)
        f1["diplomacy"] = f1.get("diplomacy") or {}
        # _set_relation mutates the dict passed in; ensure persisted
        dip = f1.get("diplomacy") or {}
        self.m._set_relation(dip, int(p2["faction_id"]), R.REL_TRUCE,
                             until=_now() + 86400)
        f1["diplomacy"] = dip
        await self.m.save_faction(f1)
        await self.m._commit()
        strategist = await self._idle_char("u1", role="strategist")
        nodes = await self.m.get_nodes(self.wid)
        target = next(n for n in nodes
                      if int(n["owner_faction"]) == int(p2["faction_id"]))
        with self.assertRaises(self.StrategyError) as ctx:
            await self.m.dispatch_plot(
                "u1", int(strategist["id"]), "rumor", int(target["id"]))
        self.assertIn("停战", str(ctx.exception))

    async def test_convoy_full_rejects(self):
        p = await self.m.get_player("u1", self.wid)
        nodes = await self.m.get_nodes(self.wid)
        home = next(n for n in nodes
                    if int(n["owner_faction"]) == int(p["faction_id"]))
        # 占领邻接据点，保证运粮通路
        nbrs = list((home.get("neighbors") or home.get("links") or []))
        if not nbrs:
            # 图结构用 edges：从 find_path 找短路径
            for n in nodes:
                if int(n.get("owner_faction", 0) or 0) != 0:
                    continue
                if n.get("type") == "ruin":
                    continue
                path = await self.m.find_path(self.wid, int(home["id"]), int(n["id"]))
                if path and len(path) == 2:
                    n["owner_faction"] = int(p["faction_id"])
                    await self.m.save_node(n)
                    await self.m._commit()
                    dst = n
                    break
            else:
                self.skipTest("找不到邻接无主据点")
                return
        else:
            dst_id = int(nbrs[0])
            dst = next(n for n in nodes if int(n["id"]) == dst_id)
            dst["owner_faction"] = int(p["faction_id"])
            await self.m.save_node(dst)
            await self.m._commit()
        src = home
        data = self.world.get("data") or {}
        data["convoys"] = [
            {"id": f"cv{i}", "owner": "u1", "faction": int(p["faction_id"]),
             "from": int(src["id"]), "to": int(dst["id"]),
             "path": [int(src["id"]), int(dst["id"])], "idx": 0,
             "grain": 10, "escort_troops": 0, "escort_army_id": 0,
             "created_tick": 0}
            for i in range(40)
        ]
        self.world["data"] = data
        await self.m.save_world(self.world)
        await self.m._commit()
        await self._give("u1", grain=100_000)
        with self.assertRaises(self.StrategyError) as ctx:
            await self.m.send_convoy("u1", int(src["id"]), int(dst["id"]), 100)
        self.assertIn("队列已满", str(ctx.exception))

    async def test_release_requires_leader(self):
        p1 = await self.m.get_player("u1", self.wid)
        # 另建一名非主成员
        await self.m.upsert_member(int(p1["faction_id"]), "u2", position="")
        cap = await self._idle_char("u2")
        nodes = await self.m.get_nodes(self.wid)
        jail = next(n for n in nodes
                    if int(n["owner_faction"]) == int(p1["faction_id"]))
        ok = await self.m._capture_char(cap, int(p1["faction_id"]), int(jail["id"]))
        self.assertTrue(ok)
        # 把 u2 的 player 势力改到 p1（否则 _require_player 会失败）
        p2 = await self.m.get_player("u2", self.wid)
        p2["faction_id"] = int(p1["faction_id"])
        await self.m.save_player(p2)
        await self.m._commit()
        with self.assertRaises(self.StrategyError) as ctx:
            await self.m.release_captive("u2", int(cap["id"]))
        self.assertIn("势力主", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
