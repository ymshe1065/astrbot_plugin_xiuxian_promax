# tests/test_strategy_manager.py
# 「仙域争霸」世界管理器集成测试（P0）：
# 建世界 / 入场新手包 / 酒馆 / 任务 / 编军上限与成本 / 行军攻占 / 灵石日限额 / tick 补跑 / 快照迷雾
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


class TestStrategyManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        sm_mod = _imp("managers.strategy_manager")
        self.StrategyManager = sm_mod.StrategyManager
        self.StrategyError = sm_mod.StrategyError
        self.StrategyBusyError = sm_mod.StrategyBusyError

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
            user_id="u1", gold=1000, user_name="测试道人"))

        self.m = self.StrategyManager(self.db)
        # 测试用：tick 间隔 1 分钟
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]

    async def _free_node(self) -> int:
        """选一座无主可入场据点（优先城池/关隘/分舵）。"""
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        nodes.sort(key=lambda n: (n["type"] not in ("city", "pass", "branch"),
                                  -int(n.get("level", 1)), int(n["id"])))
        self.assertTrue(nodes, "没有可选起始据点")
        return int(nodes[0]["id"])

    async def _enter(self, user_id: str = "u1", nickname: str = "测试道人",
                     referral: str = "") -> dict:
        return await self.m.enter_game(user_id, nickname, referral=referral,
                                       node_id=await self._free_node())

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    # ------------------------------------------------------------------
    async def test_world_creation(self):
        nodes = await self.m.get_nodes(self.wid)
        self.assertEqual(len(nodes), 36)
        # 连通性：BFS 可达所有节点
        by_id = {n["id"]: n for n in nodes}
        seen = {1}
        stack = [1]
        while stack:
            cur = stack.pop()
            for nxt in by_id[cur]["adj"]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        self.assertEqual(len(seen), 36)
        # NPC 势力 6~8 家，占据约 60%
        factions = await self._fetchall(
            "SELECT * FROM strategy_factions WHERE kind='npc' AND alive=1")
        self.assertTrue(6 <= len(factions) <= 8)
        npc_owned = [n for n in nodes if n["owner_faction"] != 0]
        ratio = len(npc_owned) / 36
        self.assertTrue(0.45 <= ratio <= 0.75, f"NPC 占比 {ratio}")
        # 无主残军分档：城 3000 / 关 2000 / 矿田 1200 / 其余 800
        cfg = self.m.main_cfg()["ownerless_garrison"]
        for n in nodes:
            if n["owner_faction"] == 0:
                self.assertEqual(n["npc_garrison"]["troops"], cfg[n["type"]])
        # 城池/关隘/分舵初始城防 1000
        for n in nodes:
            if n["type"] in ("city", "pass", "branch"):
                self.assertGreaterEqual(n["defense"], 1000)

    async def _fetchall(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            keys = [d[0] for d in cur.description]
            return [dict(zip(keys, r)) for r in rows]

    async def test_enter_game_starter_pack(self):
        p = await self._enter()
        self.assertEqual(p["coin"], 5000)
        self.assertEqual(p["grain"], 2000)
        self.assertEqual(p["recruit_token"], 2)
        f = await self.m.get_faction(p["faction_id"])
        self.assertEqual(f["kind"], "player")
        # 入场即成为个人势力主公（成员名册必须有 leader 行）
        members = await self.m.get_members(p["faction_id"])
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["user_id"], "u1")
        self.assertEqual(members[0]["position"], "leader")
        # 新手的起始据点会同步设为主城，避免无据点→无法编军/派遣的死锁
        home = await self.m.get_node(self.wid, int(f["capital_node_id"]))
        self.assertIsNotNone(home)
        self.assertEqual(int(home["owner_faction"]), int(p["faction_id"]))
        self.assertGreater(int(home.get("protect_until", 0)), 0)
        chars = await self.m.get_chars(owner_user_id="u1", world_id=self.wid)
        roles = sorted(c["role"] for c in chars)
        self.assertEqual(roles, ["general", "official"])
        self.assertTrue(all(c["rarity"] == "common" for c in chars))
        # 幂等：二次入场不重复发放
        await self._enter()
        chars2 = await self.m.get_chars(owner_user_id="u1", world_id=self.wid)
        self.assertEqual(len(chars2), 2)
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == int(p["faction_id"])]
        self.assertEqual(len(nodes), 1)

    async def test_enter_requires_chosen_free_node(self):
        """入场必须由玩家自选无主据点，不再自动分配。"""
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        target = nodes[0]
        with self.assertRaises(self.StrategyError):
            await self.m.enter_game("u1", "测试道人")
        p = await self.m.enter_game("u1", "测试道人", node_id=int(target["id"]))
        f = await self.m.get_faction(p["faction_id"])
        self.assertEqual(int(f["capital_node_id"]), int(target["id"]))
        got = await self.m.get_node(self.wid, int(target["id"]))
        self.assertEqual(int(got["owner_faction"]), int(p["faction_id"]))
        # 已被占据的据点不可再选；遗迹不可作为起始据点
        await self.db.create_player(self.Player(
            user_id="u2", gold=1000, user_name="测试乙"))
        with self.assertRaises(self.StrategyError):
            await self.m.enter_game("u2", "测试乙", node_id=int(target["id"]))
        ruin = next((n for n in await self.m.get_nodes(self.wid)
                     if n["type"] == "ruin"), None)
        if ruin:
            with self.assertRaises(self.StrategyError):
                await self.m.enter_game("u2", "测试乙", node_id=int(ruin["id"]))

    async def test_legacy_player_repair_and_rebuild(self):
        """旧存档自愈：补主公成员行 / 补起始据点；流亡后可在无主节点重建。"""
        p = await self._enter()
        f = await self.m.get_faction(p["faction_id"])
        home = await self.m.get_node(self.wid, int(f["capital_node_id"]))
        # 模拟修复前的旧存档：没有成员行、没有据点、没有主城
        await self.m.delete_member(int(p["faction_id"]), "u1")
        home["owner_faction"] = 0
        home["npc_garrison"] = {"troops": 800, "morale": 80}
        home["protect_until"] = 0
        await self.m.save_node(home)
        f["capital_node_id"] = 0
        await self.m.save_faction(f)
        p = await self.m.get_player("u1", self.wid)
        p["tutorial"] = {}
        await self.m.save_player(p)
        await self.m._commit()
        p = await self._enter()
        members = await self.m.get_members(p["faction_id"])
        self.assertEqual([m["position"] for m in members], ["leader"])
        f2 = await self.m.get_faction(p["faction_id"])
        self.assertGreater(int(f2["capital_node_id"]), 0)
        home2 = await self.m.get_node(self.wid, int(f2["capital_node_id"]))
        self.assertEqual(int(home2["owner_faction"]), int(p["faction_id"]))
        # 流亡重建：选另一个无主城池/关隘/分舵，付灵石后归入本势力
        main_player = await self.db.get_player_by_id("u1")
        main_player.gold = 3_000_000
        await self.db.update_player(main_player)
        f2["capital_node_id"] = 0
        await self.m.save_faction(f2)
        home2["owner_faction"] = 0
        await self.m.save_node(home2)
        await self.m._commit()
        target = next(n for n in await self.m.get_nodes(self.wid)
                      if int(n["owner_faction"]) == 0 and n["type"] in ("city", "pass", "branch"))
        r = await self.m.rebuild_capital("u1", int(target["id"]))
        self.assertTrue(r["ok"])
        f3 = await self.m.get_faction(p["faction_id"])
        self.assertEqual(int(f3["capital_node_id"]), int(target["id"]))
        t3 = await self.m.get_node(self.wid, int(target["id"]))
        self.assertEqual(int(t3["owner_faction"]), int(p["faction_id"]))

    async def test_guide_toggle_persists(self):
        await self._enter()
        r = await self.m.handle_action("u1", "set_guide", {"visible": False})
        self.assertTrue(r["ok"])
        p = await self.m.get_player("u1", self.wid)
        self.assertTrue((p.get("tutorial") or {}).get("guide_hidden"))
        await self.m.handle_action("u1", "set_guide", {"visible": True})
        p = await self.m.get_player("u1", self.wid)
        self.assertFalse((p.get("tutorial") or {}).get("guide_hidden"))

    async def test_tavern_and_recruit(self):
        p = await self._enter()
        tavern = await self.m.refresh_tavern(p, self.wid)
        self.assertEqual(len(tavern["candidates"]), 5)
        # 手动刷新扣 300 币，日上限 3
        p = await self.m.get_player("u1", self.wid)
        coin0 = p["coin"]
        await self.m.refresh_tavern(p, self.wid, manual=True)
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(p["coin"], coin0 - 300)
        for _ in range(2):
            await self.m.refresh_tavern(p, self.wid, manual=True)
        with self.assertRaises(self.StrategyError):
            await self.m.refresh_tavern(p, self.wid, manual=True)
        # 普通招募扣 800 币并入队
        p = await self.m.get_player("u1", self.wid)
        p["coin"] = 5000
        await self.m.save_player(p)
        n0 = len(await self.m.get_chars(owner_user_id="u1", world_id=self.wid))
        char = await self.m.recruit(p, self.wid, 0)
        self.assertEqual(char["owner_user_id"], "u1")
        n1 = len(await self.m.get_chars(owner_user_id="u1", world_id=self.wid))
        self.assertEqual(n1, n0 + 1)
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(p["coin"], 4200)

    async def test_premium_recruit_pity(self):
        # 45 抽会超默认角色上限 10：调高上限避免干扰保底验证
        self.m.main_cfg()["recruit"]["character_cap_base"] = 200
        p = await self._enter()
        await self.m.refresh_tavern(p, self.wid)
        p = await self.m.get_player("u1", self.wid)
        p["recruit_token"] = 50
        await self.m.save_player(p)
        got_epic_plus = 0
        for i in range(45):
            if not (p.get("tavern") or {}).get("candidates"):
                await self.m.refresh_tavern(p, self.wid)
                p = await self.m.get_player("u1", self.wid)
            c = await self.m.recruit(p, self.wid, 0, premium=True)
            if c["rarity"] in ("epic", "legend"):
                got_epic_plus += 1
            p = await self.m.get_player("u1", self.wid)
        self.assertGreaterEqual(got_epic_plus, 1)  # 40 抽保底必触发

    async def test_dispatch_and_recall_task(self):
        p = await self._enter()
        gen = next(c for c in await self.m.get_chars(
            owner_user_id="u1", world_id=self.wid) if c["role"] == "general")
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["owner_faction"] == 0 and n["type"] == "grain")
        node["owner_faction"] = p["faction_id"]
        await self.m.save_node(node)
        r = await self.m.dispatch_task("u1", gen["id"], node["id"], "farm", 1)
        self.assertTrue(r["ok"])
        gen2 = await self.m.get_char(gen["id"])
        self.assertEqual(gen2["status"], "task")
        self.assertEqual(gen2["stamina"], 80)  # 任务耗 20 体力
        grain0 = (await self.m.get_player("u1", self.wid))["grain"]
        r2 = await self.m.recall_task("u1", r["task_id"])
        self.assertTrue(r2["ok"])
        gen3 = await self.m.get_char(gen["id"])
        self.assertEqual(gen3["status"], "idle")
        grain1 = (await self.m.get_player("u1", self.wid))["grain"]
        self.assertGreaterEqual(grain1, grain0)
        # 非己方据点拒绝派遣
        enemy = next(n for n in await self.m.get_nodes(self.wid)
                     if n["owner_faction"] not in (0, p["faction_id"]))
        with self.assertRaises(self.StrategyError):
            await self.m.dispatch_task("u1", gen["id"], enemy["id"], "farm", 1)

    async def test_create_army_limits_and_costs(self):
        p = await self._enter()
        chars = await self.m.get_chars(owner_user_id="u1", world_id=self.wid)
        gen = next(c for c in chars if c["role"] == "general")
        off = next(c for c in chars if c["role"] == "official")
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["owner_faction"] == 0 and n["type"] == "grain")
        node["owner_faction"] = p["faction_id"]
        await self.m.save_node(node)
        p = await self.m.get_player("u1", self.wid)
        p["coin"], p["grain"] = 500_000, 500_000
        await self.m.save_player(p)
        # 内政官不能当主将
        with self.assertRaises(self.StrategyError):
            await self.m.create_army("u1", off["id"], [], node["id"],
                                     "sword_shield", 500)
        # 超带兵上限（个人势力主公 3000）
        with self.assertRaises(self.StrategyError):
            await self.m.create_army("u1", gen["id"], [], node["id"],
                                     "sword_shield", 3100)
        # 正常编成：500 剑盾 = 500×50/10=2500 币 + 500×20/10=1000 粮
        coin0 = (await self.m.get_player("u1", self.wid))["coin"]
        r = await self.m.create_army("u1", gen["id"], [], node["id"],
                                     "sword_shield", 500)
        self.assertTrue(r["ok"])
        coin1 = (await self.m.get_player("u1", self.wid))["coin"]
        self.assertEqual(coin0 - coin1, 2500)
        army = await self.m.get_army(r["army_id"])
        self.assertEqual(army["troops"], 500)
        # 携粮 = 统率×50（上限）
        self.assertEqual(army["grain"], gen["stats"]["leadership"] * 50)
        # 并发上限 2 支：再招两名非内政官角色编第二、三支
        async def _recruit_general():
            for _ in range(6):
                pnow = await self.m.get_player("u1", self.wid)
                tavern = pnow.get("tavern") or {}
                if not tavern.get("candidates"):
                    await self.m.refresh_tavern(pnow, self.wid)
                    pnow = await self.m.get_player("u1", self.wid)
                    tavern = pnow["tavern"]
                idx = next((i for i, c in enumerate(tavern["candidates"])
                            if c["role"] != "official"), None)
                if idx is None:
                    await self.m.refresh_tavern(pnow, self.wid, manual=True)
                    continue
                return await self.m.recruit(pnow, self.wid, idx)
            raise AssertionError("6 次刷新仍未招到武将/军师")

        c2 = await _recruit_general()
        await self.m.create_army("u1", c2["id"], [], node["id"], "flying_sword", 200)
        c3 = await _recruit_general()
        with self.assertRaises(self.StrategyError):
            await self.m.create_army("u1", c3["id"], [], node["id"], "beast_horde", 200)

    async def test_march_and_capture(self):
        p = await self._enter()
        gen = next(c for c in await self.m.get_chars(
            owner_user_id="u1", world_id=self.wid) if c["role"] == "general")
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        home = next(n for n in nodes.values() if n["owner_faction"] == 0)
        home["owner_faction"] = p["faction_id"]
        await self.m.save_node(home)
        # 目标：相邻任意非我方据点，弱化守军 + 拆城防，保证速胜且走占领分支
        target_id = next(x for x in home["adj"]
                         if nodes[x]["owner_faction"] != p["faction_id"])
        target = nodes[target_id]
        target["npc_garrison"] = {"troops": 300, "morale": 70}
        target["defense"] = 0
        target["siege_state"] = {}
        await self.m.save_node(target)
        p = await self.m.get_player("u1", self.wid)
        p["coin"], p["grain"] = 500_000, 500_000
        await self.m.save_player(p)
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 1400)
        army_id = r["army_id"]
        r2 = await self.m.march("u1", army_id, target_id)
        self.assertTrue(r2["ok"])
        # 推进 tick 直到占领
        captured = False
        for _ in range(10):
            await self.m.tick()
            node = await self.m.get_node(self.wid, target_id)
            if node["owner_faction"] == p["faction_id"]:
                captured = True
                break
        self.assertTrue(captured, "1400 兵打 300 残军应在 10 tick 内占领")
        battles = await self.m._battle_list(self.wid, p["faction_id"], limit=5)
        self.assertGreaterEqual(len(battles), 1)
        self.assertIn("会战", battles[0]["report_text"])

    async def test_gold_injection_daily_cap(self):
        await self._enter()
        # A3 新契约：_inject_gold(p, amount) 接收调用方 player dict 就地更新，
        # 由调用方 save_player 落库（防内部重读日志被旧快照覆盖、日限额失效）
        p = await self.m.get_player("u1", self.wid)
        got, merit = await self.m._inject_gold(p, 3_000_000)
        await self.m.save_player(p)
        await self.m._commit()
        self.assertEqual(got, 3_000_000)
        self.assertEqual(merit, 0)
        got2, merit2 = await self.m._inject_gold(p, 4_000_000)
        await self.m.save_player(p)
        await self.m._commit()
        self.assertEqual(got2, 2_000_000)          # 5M 日限额只剩 2M
        self.assertEqual(merit2, int(2_000_000 * 0.00002))  # 超额转战功 40
        player = await self.db.get_player_by_id("u1")
        self.assertEqual(player.gold, 1000 + 5_000_000)
        p2 = await self.m.get_player("u1", self.wid)
        self.assertEqual(p2["merit"], merit2)
        today = time.strftime("%Y%m%d")
        self.assertEqual(p2["daily_output_log"][today], 5_000_000)

    async def test_tick_catchup_marks_output_only(self):
        await self._enter()
        w = await self.m.get_world()
        w["last_tick_at"] = _now_minus(60 * 20)  # 欠 20 tick > 上限 8
        await self.m.save_world(w)
        await self.m._commit()
        results = await self.m.run_tick_catchup()
        # 欠 20 tick：12 个折叠为 1 个 output_only 批 + 8 个完整补跑 = 9 个结果
        self.assertEqual(len(results), 9)
        self.assertTrue(results[0]["output_only"])       # 12 tick 折叠为补产出
        for r in results[1:]:
            self.assertFalse(r["output_only"])
        w2 = await self.m.get_world()
        self.assertGreaterEqual(w2["tick_no"], 20)

    async def test_snapshot_fog(self):
        p = await self._enter()
        st = await self.m.get_state("u1")
        self.assertTrue(st["joined"])
        self.assertEqual(len(st["nodes"]), 36)
        # 远离我方视野的敌对节点：守军只给区间
        far = [n for n in st["nodes"]
               if n["garrison"] is None and n["garrison_bucket"] is not None]
        self.assertTrue(far, "应存在迷雾节点")
        for n in far:
            self.assertIn(n["garrison_bucket"],
                          ("3000+", "2000+", "1000+", "500+", "100+", "<100", "0"))
        # 玩家视图齐全
        pv = st["player"]
        self.assertEqual(pv["coin"], p["coin"])
        self.assertEqual(len(pv["chars"]), 2)
        self.assertIn("troop_types", st["meta"])

    async def test_busy_lock_blocks_actions(self):
        await self._enter()
        await self.m._world_lock.acquire()
        try:
            with self.assertRaises(self.StrategyBusyError):
                await self.m.handle_action("u1", "refresh_tavern", {})
        finally:
            self.m._world_lock.release()

    async def test_build_and_upgrade(self):
        p = await self._enter()
        # 模板 4 座城池全部会成为 NPC 都城（level=2）：改用非都城的关隘（level=1）测试
        city = next(n for n in await self.m.get_nodes(self.wid)
                    if n["type"] == "pass" and n["level"] == 1)
        city["owner_faction"] = p["faction_id"]
        city["siege_state"] = {}
        await self.m.save_node(city)
        p = await self.m.get_player("u1", self.wid)
        p["coin"], p["iron"] = 100_000, 100_000
        await self.m.save_player(p)
        r = await self.m.build("u1", city["id"], "tower")
        self.assertTrue(r["ok"])
        node = await self.m.get_node(self.wid, city["id"])
        self.assertEqual(node["buildings"][0]["id"], "tower")
        with self.assertRaises(self.StrategyError):
            await self.m.build("u1", city["id"], "no_such")
        r2 = await self.m.upgrade_node("u1", city["id"])
        self.assertTrue(r2["ok"])
        node = await self.m.get_node(self.wid, city["id"])
        self.assertEqual(node["level"], 2)
        self.assertEqual(node["defense"], 2000)


def _now_minus(sec: int) -> int:
    return int(time.time()) - sec


if __name__ == "__main__":
    unittest.main(verbosity=2)
