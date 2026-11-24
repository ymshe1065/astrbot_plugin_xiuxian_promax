# tests/test_strategy_manager.py
# 「仙域争霸」世界管理器集成测试（P0）：
# 建世界 / 入场新手包 / 酒馆 / 任务 / 编军上限与成本 / 行军攻占 / 灵石日限额 / tick 补跑 / 快照迷雾
import os
import sys
import tempfile
import json
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
        # 野战遭遇默认改为手操窗口；常规数值回归按自动结算口径跑
        self.m.main_cfg().setdefault("hex", {})["field_encounter_mode"] = "auto"
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

    async def _prime_tavern(self, rarity: str, *, coin: int = 5000,
                            token: int = 50, name: str = "测试将") -> dict:
        """注入指定稀有度的酒馆候选，并固定钱包，避免随机稀有度导致 flake。"""
        p = await self.m.get_player("u1", self.wid)
        tavern = dict(p.get("tavern") or {})
        tavern["candidates"] = [{
            "name": name, "role": "general", "rarity": rarity,
            "profession": "sword",
            "stats": {"leadership": 50, "valor": 50, "wisdom": 50,
                      "admin": 50, "charm": 50, "luck": 50},
            "traits": [], "aptitude": {},
        }]
        tavern["refresh_at"] = int(time.time()) + 3600
        p["tavern"] = tavern
        p["coin"] = int(coin)
        p["recruit_token"] = int(token)
        await self.m.save_player(p)
        return p

    async def _place(self, char, node_id: int, *, status: str = "idle"):
        """把角色显式放到某据点（所在地机制测试夹具）。"""
        c = await self.m.get_char(int(char["id"]))
        c["location"] = {"node_id": int(node_id)}
        if status is not None:
            c["status"] = status
        await self.m.save_char(c)
        # 同步回调用方拿到的字典，避免后续 save_char 覆盖落点
        char["location"] = dict(c["location"])
        if status is not None:
            char["status"] = status
        return c

    async def _finish_construction(self, node_id: int):
        """把在建升级的 `end_tick` 拉到当前世界 tick 并结算，返回最新节点。"""
        world = await self.m.get_world()
        node = await self.m.get_node(self.wid, int(node_id))
        cons = dict(node.get("construction") or {})
        self.assertTrue(cons, "节点应处于建造中")
        cons["end_tick"] = int(world.get("tick_no", 0) or 0)
        cons["end_at"] = int(time.time()) - 1
        node["construction"] = cons
        await self.m.save_node(node)
        await self.m._step_construction(world)
        return await self.m.get_node(self.wid, int(node_id))

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
        # 起始据点基础保护期：4 天（96 小时）
        self.assertLessEqual(
            abs(int(home["protect_until"]) - int(time.time()) - 4 * 86400), 10)
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
        # 随机世界可能给无主节点带上保护期；本用例验证「流亡重建」而非免战，
        # 清零避免随机失败（rebuild_capital 会拒绝保护期节点）。
        target["protect_until"] = 0
        await self.m.save_node(target)
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
        # 珍品走铜钱梯度价：1600 币并入队
        p = await self._prime_tavern("rare", coin=5000)
        n0 = len(await self.m.get_chars(owner_user_id="u1", world_id=self.wid))
        char = await self.m.recruit(p, self.wid, 0)
        self.assertEqual(char["owner_user_id"], "u1")
        n1 = len(await self.m.get_chars(owner_user_id="u1", world_id=self.wid))
        self.assertEqual(n1, n0 + 1)
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(p["coin"], 5000 - 1600)

    async def test_character_cap_scales_with_owned_nodes(self):
        p = await self._enter()
        rcfg = self.m.main_cfg()["recruit"]
        rcfg.update({"character_cap_base": 2, "character_cap_per_node": 2,
                     "character_cap_max": 20})
        # 1 座据点：2 + 2×1 = 4；新手已占 2 名
        info = await self.m._char_cap_info(self.wid, p)
        self.assertEqual(info["cap"], 4)
        self.assertEqual(info["nodes"], 1)
        self.assertEqual(await self.m._roster_count("u1", self.wid), 2)
        p = await self._prime_tavern("common")
        await self.m.recruit(p, self.wid, 0)
        p = await self._prime_tavern("common")
        await self.m.recruit(p, self.wid, 0)
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(await self.m._roster_count("u1", self.wid), 4)
        # 名册已满：招募被据点上限拒绝
        p = await self._prime_tavern("common")
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.recruit(p, self.wid, 0)
        self.assertIn("据点", str(cm.exception))
        # 扩建 2 座据点 → 3 座：2 + 2×3 = 8，名额恢复
        p = await self.m.get_player("u1", self.wid)
        free = [n for n in await self.m.get_nodes(self.wid)
                if int(n["owner_faction"]) == 0 and n["type"] != "ruin"][:2]
        for n in free:
            n["owner_faction"] = int(p["faction_id"])
            await self.m.save_node(n)
        info2 = await self.m._char_cap_info(self.wid, p)
        self.assertEqual(info2["nodes"], 3)
        self.assertEqual(info2["cap"], 8)
        p = await self._prime_tavern("common")
        await self.m.recruit(p, self.wid, 0)
        self.assertEqual(await self.m._roster_count("u1", self.wid), 5)

    async def test_dismiss_char_frees_roster_slot(self):
        p = await self._enter()
        rcfg = self.m.main_cfg()["recruit"]
        rcfg.update({"character_cap_base": 2, "character_cap_per_node": 0,
                     "character_cap_max": 20})
        await self.m.refresh_tavern(p, self.wid)
        with self.assertRaises(self.StrategyError):
            await self.m.recruit(p, self.wid, 0)  # 2/2 已满
        official = next(c for c in await self.m.get_chars(
            owner_user_id="u1", world_id=self.wid) if c["role"] == "official")
        r = await self.m.dismiss_char("u1", int(official["id"]))
        self.assertTrue(r["ok"])
        self.assertIsNone(await self.m.get_char(int(official["id"])))
        self.assertEqual(await self.m._roster_count("u1", self.wid), 1)
        p = await self._prime_tavern("common")
        char = await self.m.recruit(p, self.wid, 0)
        self.assertEqual(char["owner_user_id"], "u1")

    async def test_recruit_currency_by_rarity(self):
        await self._enter()
        rcfg = self.m.main_cfg()["recruit"]
        rcfg["character_cap_base"] = 50
        rcfg["character_cap_max"] = 50
        coin_costs = rcfg["coin_cost_by_rarity"]
        token_costs = rcfg["token_cost_by_rarity"]
        # 凡/良/珍：只扣铜钱
        for rarity in ("common", "fine", "rare"):
            p = await self._prime_tavern(rarity, coin=5000, token=50,
                                         name=f"测试{rarity}")
            char = await self.m.recruit(p, self.wid, 0)
            p = await self.m.get_player("u1", self.wid)
            self.assertEqual(char["rarity"], rarity)
            self.assertEqual(p["coin"], 5000 - int(coin_costs[rarity]))
            self.assertEqual(p["recruit_token"], 50)
        # 极/传：只扣募将令
        for rarity in ("epic", "legend"):
            p = await self._prime_tavern(rarity, coin=5000, token=50,
                                         name=f"测试{rarity}")
            char = await self.m.recruit(p, self.wid, 0, premium=True)
            p = await self.m.get_player("u1", self.wid)
            self.assertEqual(char["rarity"], rarity)
            self.assertEqual(p["coin"], 5000)
            self.assertEqual(p["recruit_token"], 50 - int(token_costs[rarity]))

    async def test_recruit_rejects_wrong_currency(self):
        await self._enter()
        rcfg = self.m.main_cfg()["recruit"]
        rcfg["character_cap_base"] = 20
        rcfg["character_cap_max"] = 20
        # 极品不能用铜钱
        p = await self._prime_tavern("epic", coin=5000, token=50)
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.recruit(p, self.wid, 0)
        self.assertIn("募将令", str(cm.exception))
        # 珍品不能用募将令
        p = await self._prime_tavern("rare", coin=5000, token=50)
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.recruit(p, self.wid, 0, premium=True)
        self.assertIn("铜钱", str(cm.exception))
        # 令牌不足
        p = await self._prime_tavern("legend", coin=5000, token=24)
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.recruit(p, self.wid, 0, premium=True)
        self.assertIn("募将令不足", str(cm.exception))

    async def test_tavern_legend_weight_is_fractional(self):
        weights = self.m.main_cfg()["recruit"]["rarity_weights_normal"]
        self.assertAlmostEqual(float(weights["legend"]), 0.1)

    async def test_battle_exp_grows_leader_and_members(self):
        p = await self._enter()
        chars = await self.m.get_chars(owner_user_id="u1", world_id=self.wid)
        gen = next(c for c in chars if c["role"] == "general")
        off = next(c for c in chars if c["role"] == "official")
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["owner_faction"] == 0 and n["type"] == "grain")
        node["owner_faction"] = p["faction_id"]
        await self.m.save_node(node)
        p["coin"], p["grain"] = 500_000, 500_000
        await self.m.save_player(p)
        await self._place(gen, node["id"])
        await self._place(off, node["id"])
        r = await self.m.create_army("u1", gen["id"], [off["id"]],
                                     node["id"], "sword_shield", 300)
        army = await self.m.get_army(r["army_id"])
        result = {
            "winner": "attacker",
            "attackers": [{"id": army["id"], "troops_after": army["troops"],
                           "morale": 90, "routed": False}],
            "defenders": [], "atk_losses": 0, "dfn_losses": 0,
            "peril_events": [],
        }
        await self.m._apply_battle_aftermath(
            self.world, node, [army], [], result)
        leader = await self.m.get_char(gen["id"])
        member = await self.m.get_char(off["id"])
        self.assertGreater(leader["exp"], 0)
        self.assertGreater(member["exp"], 0)
        self.assertLess(member["exp"], leader["exp"])  # 随行阅历为主将 50%

    async def test_dispatch_and_recall_task(self):
        p = await self._enter()
        gen = next(c for c in await self.m.get_chars(
            owner_user_id="u1", world_id=self.wid) if c["role"] == "general")
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["owner_faction"] == 0 and n["type"] == "grain")
        node["owner_faction"] = p["faction_id"]
        await self.m.save_node(node)
        await self._place(gen, node["id"])
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
        self.assertEqual(int((gen3.get("location") or {}).get("node_id")),
                         int(node["id"]))
        grain1 = (await self.m.get_player("u1", self.wid))["grain"]
        self.assertGreaterEqual(grain1, grain0)
        # 非己方据点拒绝派遣
        enemy = next(n for n in await self.m.get_nodes(self.wid)
                     if n["owner_faction"] not in (0, p["faction_id"]))
        with self.assertRaises(self.StrategyError):
            await self.m.dispatch_task("u1", gen["id"], enemy["id"], "farm", 1)

    async def test_task_list_shows_output_and_estimate(self):
        """「进行中任务」快照带产出类型与满期预期收益。"""
        p = await self._enter()
        gen = next(c for c in await self.m.get_chars(
            owner_user_id="u1", world_id=self.wid) if c["role"] == "general")
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["owner_faction"] == 0 and n["type"] == "grain")
        node["owner_faction"] = p["faction_id"]
        await self.m.save_node(node)
        await self._place(gen, node["id"])
        r = await self.m.dispatch_task("u1", gen["id"], node["id"], "farm", 2)
        self.assertTrue(r["ok"])
        state = await self.m.get_state("u1")
        task = next(t for t in state["player"]["tasks"]
                    if int(t["id"]) == int(r["task_id"]))
        self.assertEqual(task["output_kind"], "grain")
        self.assertEqual(task["output_name"], "粮草")
        self.assertGreater(int(task["estimate"]), 0)
        self.assertEqual(task["task_name"], "屯田")
        # 持续任务标记：end_at=0，带 next_settle_at
        self.assertEqual(int(task["end_at"]), 0)
        self.assertEqual(int(task["sustained"]), 1)
        self.assertGreater(int(task["next_settle_at"]), 0)
        # v4.6.6：展示用结算时刻对齐到 world tick（结算只在 tick 管线内发生），
        # 避免「小时边界已到、tick 未到」期间前端倒计时长期停在 0s。
        world = await self.m.get_world()
        tick_sec = self.m.tick_seconds()
        next_tick = int(world["last_tick_at"]) + tick_sec
        expect = int(task["expect_settle_at"])
        self.assertGreaterEqual(expect, int(task["next_settle_at"]))
        self.assertEqual((expect - next_tick) % tick_sec, 0)

    async def test_sustained_expect_settle_points_to_next_tick_when_overdue(self):
        """小时边界已过、tick 未到时，下发时刻应指向下一个推进而不是停在 0s。"""
        p = await self._enter()
        gen = next(c for c in await self.m.get_chars(
            owner_user_id="u1", world_id=self.wid) if c["role"] == "general")
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["owner_faction"] == 0 and n["type"] == "grain")
        node["owner_faction"] = p["faction_id"]
        await self.m.save_node(node)
        await self._place(gen, node["id"])
        r = await self.m.dispatch_task("u1", gen["id"], node["id"], "farm", 1)
        self.assertTrue(r["ok"])
        row = next(t for t in await self.m.get_tasks(self.wid, status="active")
                   if int(t["id"]) == int(r["task_id"]))
        payout = dict(row["payout"])
        payout["next_settle_at"] = int(time.time()) - 30   # 已到点，等 tick
        await self.m._execute(
            "UPDATE strategy_tasks SET payout_json=? WHERE id=?",
            (json.dumps(payout, ensure_ascii=False), int(r["task_id"])))
        state = await self.m.get_state("u1")
        task = next(t for t in state["player"]["tasks"]
                    if int(t["id"]) == int(r["task_id"]))
        world = await self.m.get_world()
        next_tick = int(world["last_tick_at"]) + self.m.tick_seconds()
        self.assertGreaterEqual(int(task["expect_settle_at"]), next_tick)
        self.assertGreater(int(task["expect_settle_at"]), int(time.time()))

    async def test_mining_task_settles_iron(self):
        """采矿任务：非矿据点也产精铁，结算后进账并累计成就进度。"""
        p = await self._enter()
        gen = next(c for c in await self.m.get_chars(
            owner_user_id="u1", world_id=self.wid) if c["role"] == "general")
        # 选一座非矿的己方据点（城市/关隘/谷田/坊市均可）
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["owner_faction"] == 0 and n["type"] != "mine"
                    and n["type"] != "ruin")
        node["owner_faction"] = p["faction_id"]
        await self.m.save_node(node)
        await self._place(gen, node["id"])
        self.assertGreaterEqual(
            float(self.m.main_cfg()["task"]["base_per_tick"]["mining"]), 16)
        iron0 = (await self.m.get_player("u1", self.wid)).get("iron", 0)
        # 关闭体力消耗，验证 8 个小时结算周期的累计产量
        self.m.main_cfg()["stamina"]["enabled"] = False
        r = await self.m.dispatch_task("u1", gen["id"], node["id"], "mining", 1)
        self.assertTrue(r["ok"])
        self.assertIn("采矿", r["msg"])
        # 持续任务：把 next_settle_at 推到过去，逐小时结算 8 次
        for _ in range(8):
            await self.m._execute(
                "UPDATE strategy_tasks SET payout_json=? WHERE id=?",
                (json.dumps({"sustained": 1, "ticks": 4, "exp_ticks": 4,
                             "next_settle_at": int(time.time()) - 10,
                             "settled_hours": 0, "origin_node_id": node["id"],
                             "target_node_id": node["id"]},
                            ensure_ascii=False), r["task_id"]))
            world = await self.m.get_world()
            await self.m._step_settle_tasks(world)
        p2 = await self.m.get_player("u1", self.wid)
        gained = int(p2.get("iron", 0)) - iron0
        self.assertGreater(gained, 500)  # 8h 普通据点也应明显高于旧版 8/tick
        prog = ((p2.get("achievements") or {}).get("progress") or {})
        self.assertGreater(prog.get("civil_mine_100k", 0), 0)

    async def test_merit_shop_iron_pack(self):
        """战功商店精铁包：300 战功 → 500 精铁，周限 3 次。"""
        p = await self._enter()
        p["merit"] = 2000
        await self.m.save_player(p)
        iron0 = int(p.get("iron", 0))
        for i in range(3):
            r = await self.m.buy_merit_shop("u1", "iron_pack")
            self.assertTrue(r["ok"])
            self.assertIn("精铁", r["msg"])
        p2 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p2["iron"]), iron0 + 1500)
        self.assertEqual(int(p2["merit"]), 2000 - 900)
        week = (p2.get("honor") or {}).get("merit_shop_week_iron") or {}
        self.assertEqual(int(week.get("used", 0)), 3)
        with self.assertRaises(self.StrategyError):
            await self.m.buy_merit_shop("u1", "iron_pack")
        # 精铁包与灵石包周计数互相独立
        await self.m.buy_merit_shop("u1", "gold_pack")
        p3 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(((p3.get("honor") or {}).get(
            "merit_shop_week") or {}).get("used", 0)), 1)

    async def test_merit_shop_week_view_resets_across_weeks(self):
        """快照里的周限计数：上周记录应显示为本周 0 次，且不写库。"""
        p = await self._enter()
        honor = p.get("honor") or {}
        honor["merit_shop_week"] = {"week": "197001", "used": 5}
        honor["merit_shop_week_iron"] = {"week": "197001", "used": 3}
        p["honor"] = honor
        await self.m.save_player(p)
        state = await self.m.get_state("u1")
        hv = state["player"]["honor"]
        self.assertEqual(int(hv["merit_shop_week"]["used"]), 0)
        self.assertEqual(int(hv["merit_shop_week_iron"]["used"]), 0)
        self.assertEqual(hv["merit_shop_week"]["week"], time.strftime("%Y%W"))
        # 快照只做视图归一，不能把玩家的周限记录真的清零（否则可绕过限购）
        p2 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p2["honor"]["merit_shop_week"]["used"]), 5)
        # 价目/伤势配置随快照下发，前端不再写死
        self.assertEqual(int(state["meta"]["merit_shop"]["gold_pack"]["weekly_limit"]), 5)
        self.assertIn("elixir_heal", state["meta"]["injury_cfg"])

    async def test_merit_shop_material_pack_and_recommend_letter(self):
        """物资包周限与举荐信定向刷新职责。"""
        p = await self._enter()
        p["merit"] = 3000
        await self.m.save_player(p)
        coin0, grain0, iron0 = (int(p.get("coin", 0)), int(p.get("grain", 0)),
                                int(p.get("iron", 0)))
        for _ in range(3):
            r = await self.m.buy_merit_shop("u1", "material_pack")
            self.assertTrue(r["ok"])
            self.assertIn("物资", r["msg"])
        p2 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p2["coin"]), coin0 + 24000)
        self.assertEqual(int(p2["grain"]), grain0 + 24000)
        self.assertEqual(int(p2["iron"]), iron0 + 1200)
        self.assertEqual(int(p2["merit"]), 3000 - 900)
        week = (p2.get("honor") or {}).get("merit_shop_week_material") or {}
        self.assertEqual(int(week.get("used", 0)), 3)
        with self.assertRaises(self.StrategyError):
            await self.m.buy_merit_shop("u1", "material_pack")
        # 举荐信：200 战功，定向刷新为内政官
        r = await self.m.buy_merit_shop("u1", "recommend_letter", role="official")
        self.assertTrue(r["ok"])
        p3 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p3["merit"]), 3000 - 900 - 200)
        cands = (p3.get("tavern") or {}).get("candidates") or []
        self.assertTrue(cands)
        self.assertTrue(all(c["role"] == "official" for c in cands))
        with self.assertRaises(self.StrategyError):
            await self.m.buy_merit_shop("u1", "recommend_letter", role="bogus")

    async def test_merit_shop_gold_pack_daily_cap_does_not_charge(self):
        """灵石注入剩余额度不足时拒绝兑换，战功与周限均不被扣。"""
        p = await self._enter()
        p["merit"] = 2000
        hard = int(self.m.main_cfg()["gold_output"]["max_daily_gold_cap"])
        p["daily_output_log"] = {time.strftime("%Y%m%d"): hard}
        await self.m.save_player(p)
        with self.assertRaises(self.StrategyError):
            await self.m.buy_merit_shop("u1", "gold_pack")
        p2 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p2["merit"]), 2000)
        week = (p2.get("honor") or {}).get("merit_shop_week") or {}
        self.assertEqual(int(week.get("used", 0)), 0)

    async def test_honor_shop_spend_and_season_limit(self):
        """荣誉商店：荣誉点消费、赛季限购与开局加成。"""
        p = await self._enter()
        honor = p.get("honor") or {}
        honor["total"] = 2000
        p["honor"] = honor
        await self.m.save_player(p)
        gold0 = int((await self.db.get_player_by_id("u1")).gold or 0)
        for _ in range(3):
            r = await self.m.buy_honor_shop("u1", "gold_pack")
            self.assertTrue(r["ok"])
        main = await self.db.get_player_by_id("u1")
        self.assertEqual(int(main.gold or 0), gold0 + 300000)
        p2 = await self.m.get_player("u1", self.wid)
        hv = p2.get("honor") or {}
        self.assertEqual(int(hv["spent"]["gold_pack"]), 1500)
        self.assertEqual(int(hv["season_spent"]["gold_pack"]), 3)
        # 赛季限购 3 次
        with self.assertRaises(self.StrategyError):
            await self.m.buy_honor_shop("u1", "gold_pack")
        # 开局加成：产出倍率写入 honor.boost
        r = await self.m.buy_honor_shop("u1", "boost_pack")
        self.assertTrue(r["ok"])
        p3 = await self.m.get_player("u1", self.wid)
        self.assertGreater(int((p3["honor"].get("boost") or {}).get("until", 0)),
                           int(time.time()))
        self.assertAlmostEqual(self.m.player_output_mult(p3), 1.2, places=3)
        # 剩余荣誉 = 2000 - 1800
        state = await self.m.get_state("u1")
        self.assertEqual(int(state["player"]["honor"]["balance"]), 200)
        # 荣誉不足
        with self.assertRaises(self.StrategyError):
            await self.m.buy_honor_shop("u1", "title_pack")
        # 快照下发荣誉商店配置
        self.assertIn("gold_pack", state["meta"]["honor_shop"])

    async def test_honor_shop_title_pack_grants_main_title(self):
        """荣誉称号：写入主玩法称号图鉴，已拥有时拒绝重复兑换。"""
        p = await self._enter()
        honor = p.get("honor") or {}
        honor["total"] = 1000
        p["honor"] = honor
        await self.m.save_player(p)
        r = await self.m.buy_honor_shop("u1", "title_pack")
        self.assertTrue(r["ok"])
        main = await self.db.get_player_by_id("u1")
        TitleManager = _imp("managers.title_manager").TitleManager
        self.assertTrue(TitleManager(self.db).has_title(main, "赛季霸主"))
        with self.assertRaises(self.StrategyError):
            await self.m.buy_honor_shop("u1", "title_pack")
        p2 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p2["honor"]["spent"]["title_pack"]), 800)
        self.assertEqual(self.m._honor_balance(p2["honor"]), 200)

    async def test_honor_carries_over_to_new_world(self):
        """新赛季是新 world + 新玩家行，入场时必须复制上一赛季荣誉。"""
        p = await self._enter()
        honor = p.get("honor") or {}
        honor["total"] = 1200
        honor["spent"] = {"gold_pack": 500}
        honor["season_spent"] = {"gold_pack": 3}
        p["honor"] = honor
        await self.m.save_player(p)
        new_world = await self.m._create_world(season_no=2)
        nodes = [n for n in await self.m.get_nodes(new_world["id"])
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        self.assertTrue(nodes)
        p2 = await self.m.enter_game(
            "u1", "测试道人", node_id=int(nodes[0]["id"]))
        self.assertEqual(int(p2["world_id"]), int(new_world["id"]))
        hv = p2.get("honor") or {}
        self.assertEqual(int(hv["total"]), 1200)
        self.assertEqual(int(hv["spent"]["gold_pack"]), 500)
        self.assertEqual(hv.get("season_spent"), {})
        self.assertEqual(self.m._honor_balance(hv), 700)

    async def test_honor_shop_equip_instance_requires_main_game_pool(self):
        """装备副本：主玩法配置未注入时拒绝兑换且不扣战功。"""
        p = await self._enter()
        p["merit"] = 5000
        await self.m.save_player(p)
        # 测试用 StrategyManager 未注入 config_manager
        with self.assertRaises(self.StrategyError):
            await self.m.buy_merit_shop("u1", "equip_instance")
        p2 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p2["merit"]), 5000)

    async def test_merit_shop_equip_instance_grants_equipment(self):
        """装备副本：注入主玩法装备池后按境界掉落并存入储物戒，月限 1 次。"""
        from types import SimpleNamespace

        p = await self._enter()
        p["merit"] = 5000
        await self.m.save_player(p)
        await self.m._commit()
        cm = SimpleNamespace(
            items_data={"试炼长剑": {"type": "weapon", "price": 800,
                                     "rank": "凡品", "volume": 1}},
            weapons_data={},
            techniques_data={},
            storage_rings_data={},
            is_pill=lambda _name: False,
        )
        self.m.config_manager = cm
        try:
            r = await self.m.buy_merit_shop("u1", "equip_instance")
            self.assertTrue(r["ok"])
            self.assertIn("试炼长剑", r["msg"])
            p2 = await self.m.get_player("u1", self.wid)
            self.assertEqual(int(p2["merit"]), 5000 - 3000)
            self.assertEqual(
                int((p2["honor"]["merit_shop_month"]["used"] or {})
                    .get("equip_instance", 0)), 1)
            main = await self.db.get_player_by_id("u1")
            owned = [i.get("name") for i in
                     (main.get_equipment_instances() or [])]
            self.assertIn("试炼长剑", owned)
            # 月限 1 次：再次兑换应拒绝且不扣战功
            with self.assertRaises(self.StrategyError):
                await self.m.buy_merit_shop("u1", "equip_instance")
            p3 = await self.m.get_player("u1", self.wid)
            self.assertEqual(int(p3["merit"]), 2000)
        finally:
            self.m.config_manager = None

    async def test_create_army_limits_and_costs(self):
        p = await self._enter()
        chars = await self.m.get_chars(owner_user_id="u1", world_id=self.wid)
        gen = next(c for c in chars if c["role"] == "general")
        off = next(c for c in chars if c["role"] == "official")
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["owner_faction"] == 0 and n["type"] == "grain")
        node["owner_faction"] = p["faction_id"]
        await self.m.save_node(node)
        await self._place(gen, node["id"])
        await self._place(off, node["id"])
        p = await self.m.get_player("u1", self.wid)
        p["coin"], p["grain"] = 500_000, 500_000
        await self.m.save_player(p)
        # 内政官可担任主将（v4.5.1 起）
        r_off = await self.m.create_army("u1", off["id"], [], node["id"],
                                         "sword_shield", 500)
        self.assertTrue(r_off["ok"])
        await self.m.disband_army("u1", r_off["army_id"])
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
        # 并发上限 2 支：再招两名其他角色编第二、三支
        async def _recruit_general():
            for _ in range(6):
                pnow = await self.m.get_player("u1", self.wid)
                tavern = pnow.get("tavern") or {}
                if not tavern.get("candidates"):
                    await self.m.refresh_tavern(pnow, self.wid)
                    pnow = await self.m.get_player("u1", self.wid)
                    tavern = pnow["tavern"]
                idx = next((i for i, c in enumerate(tavern["candidates"])
                            if c["role"] != "official"
                            and c["rarity"] not in ("epic", "legend")), None)
                if idx is None:
                    await self.m.refresh_tavern(pnow, self.wid, manual=True)
                    continue
                return await self.m.recruit(pnow, self.wid, idx)
            raise AssertionError("6 次刷新仍未招到武将/军师")

        c2 = await _recruit_general()
        await self._place(c2, node["id"])
        await self.m.create_army("u1", c2["id"], [], node["id"], "flying_sword", 200)
        c3 = await _recruit_general()
        await self._place(c3, node["id"])
        with self.assertRaises(self.StrategyError):
            await self.m.create_army("u1", c3["id"], [], node["id"], "beast_horde", 200)

    async def test_handover_army_swaps_leader_and_members(self):
        """军团换将：空闲武将可替换主将/随行，兵力/粮草/老兵不变。"""
        R = _imp("core.strategy_rules")
        p, gen, off, home, _target = await self._army_fixture()
        g2 = await self._add_general(p, home["id"], "新帅甲")
        g3 = await self._add_general(p, home["id"], "副将乙")
        r = await self.m.create_army("u1", gen["id"], [off["id"]], home["id"],
                                     "sword_shield", 500)
        aid = r["army_id"]
        before = await self.m.get_army(aid)
        res = await self.m.handover_army("u1", aid, g2["id"], [g3["id"]])
        self.assertTrue(res["ok"])
        a = await self.m.get_army(aid)
        self.assertEqual(a["characters"], [g2["id"], g3["id"]])
        self.assertEqual(a["name"], "新帅甲军")
        self.assertEqual(a["troops"], before["troops"])
        self.assertEqual(a["grain"], before["grain"])
        self.assertEqual(a["veteran"], before["veteran"])
        # 换下的主将/随行回归空闲并留在驻地
        for old in (gen, off):
            c = await self.m.get_char(old["id"])
            self.assertEqual(c["status"], "idle")
            self.assertEqual(R.char_node_id(c), home["id"])
        # 新阵容进入随军状态
        for new in (g2, g3):
            c = await self.m.get_char(new["id"])
            self.assertEqual(c["status"], "army")
            self.assertEqual(R.char_node_id(c), home["id"])

    async def test_handover_army_promotes_member_and_keeps_companions(self):
        """现役成员可提拔为主将；省略 member_ids 时保留现有随行。"""
        R = _imp("core.strategy_rules")
        p, gen, off, home, _target = await self._army_fixture()
        g2 = await self._add_general(p, home["id"], "副将丙")
        r = await self.m.create_army("u1", gen["id"], [g2["id"], off["id"]],
                                     home["id"], "sword_shield", 500)
        aid = r["army_id"]
        # 走统一动作分发，验证 REST 参数接线
        res = await self.m.handle_action(
            "u1", "handover_army",
            {"army_id": aid, "leader_char_id": g2["id"]})
        self.assertTrue(res["ok"])
        a = await self.m.get_army(aid)
        self.assertEqual(a["characters"], [g2["id"], off["id"]])
        old = await self.m.get_char(gen["id"])
        self.assertEqual(old["status"], "idle")
        self.assertEqual(R.char_node_id(old), home["id"])

    async def test_handover_army_allows_official_and_injured_leader(self):
        """v4.5.1：新主将可为内政官或重伤角色（随行仍须能行动）。"""
        R = _imp("core.strategy_rules")
        p, gen, off, home, _target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        aid = r["army_id"]
        # 内政官担任主将
        res = await self.m.handover_army("u1", aid, off["id"], [])
        self.assertTrue(res["ok"])
        self.assertEqual((await self.m.get_army(aid))["characters"], [off["id"]])
        # 重伤武将担任主将
        g2 = await self._add_general(p, home["id"], "带伤主将庚")
        g2["injury"] = R.INJURY_HEAVY
        await self.m.save_char(g2)
        res = await self.m.handover_army("u1", aid, g2["id"], [])
        self.assertTrue(res["ok"])
        self.assertEqual((await self.m.get_army(aid))["characters"], [g2["id"]])
        # 带伤主将 + 健康内政官随行
        res = await self.m.handover_army("u1", aid, g2["id"], [off["id"]])
        self.assertTrue(res["ok"])
        self.assertEqual((await self.m.get_army(aid))["characters"],
                         [g2["id"], off["id"]])

    async def test_handover_army_rejects_invalid_and_over_cap(self):
        """异地/他军/行军中/超上限均拒绝换将。"""
        p, gen, off, home, target_id = await self._army_fixture()
        g2 = await self._add_general(p, home["id"], "新帅丁")
        g3 = await self._add_general(p, home["id"], "别军戊")
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        aid = r["army_id"]
        # 异地空闲角色不能加入
        g4 = await self._add_general(p, target_id, "远客己")
        with self.assertRaises(self.StrategyError):
            await self.m.handover_army("u1", aid, gen["id"], [g4["id"]])
        # 已在其他军团的角色不能加入
        await self._place(g3, home["id"])
        await self.m.create_army("u1", g3["id"], [], home["id"],
                                 "sword_shield", 200)
        with self.assertRaises(self.StrategyError):
            await self.m.handover_army("u1", aid, gen["id"], [g3["id"]])
        # 行军途中不可换将
        a = await self.m.get_army(aid)
        a["state"] = "march"
        await self.m.save_army(a)
        with self.assertRaises(self.StrategyError):
            await self.m.handover_army("u1", aid, g2["id"], [])
        a = await self.m.get_army(aid)
        a["state"] = "idle"
        a["troops"] = 2000  # 无官职主将上限 1500，超出必须拒绝
        await self.m.save_army(a)
        with self.assertRaises(self.StrategyError):
            await self.m.handover_army("u1", aid, g2["id"], [])
        # 拒绝后阵容不得发生改动
        unchanged = await self.m.get_army(aid)
        self.assertEqual(unchanged["characters"], [gen["id"]])
        self.assertEqual(unchanged["troops"], 2000)
        # 兵力回落到上限内（正好 1500）才允许交接
        unchanged["troops"] = 1500
        await self.m.save_army(unchanged)
        res = await self.m.handover_army("u1", aid, g2["id"], [])
        self.assertTrue(res["ok"])
        self.assertEqual(res["troop_cap"], 1500)
        final = await self.m.get_army(aid)
        self.assertEqual(final["troops"], 1500)
        self.assertEqual(final["characters"], [g2["id"]])

    async def test_handover_army_view_syncs_cap_and_army_roles(self):
        """换将后军团实际上限与「主将/随军」标记按新主将实时刷新。"""
        p, gen, off, home, _target = await self._army_fixture()
        g2 = await self._add_general(p, home["id"], "太尉新帅")
        g2["appointed_position"] = "taiwei"
        await self.m.save_char(g2)
        f = await self.m.get_faction(p["faction_id"])
        f["salary"] = {"enabled": True, "paid_role_char_ids": [g2["id"]]}
        await self.m.save_faction(f)
        r = await self.m.create_army("u1", gen["id"], [off["id"]], home["id"],
                                     "sword_shield", 500)
        aid = r["army_id"]
        pv = await self.m._player_view(
            await self.m.get_player("u1", self.wid), self.world,
            await self.m.get_nodes(self.wid))
        before = next(a for a in pv["armies"] if a["id"] == aid)
        self.assertEqual(before["troop_cap"], 1500)
        self.assertEqual(before["leader_id"], gen["id"])
        # 换将到太尉：上限立即变为 3000，主将/随军标记同步
        res = await self.m.handover_army("u1", aid, g2["id"], [off["id"]])
        self.assertEqual(res["troop_cap"], 3000)
        self.assertEqual(res["leader_id"], g2["id"])
        pv = await self.m._player_view(
            await self.m.get_player("u1", self.wid), self.world,
            await self.m.get_nodes(self.wid))
        army = next(a for a in pv["armies"] if a["id"] == aid)
        self.assertEqual(army["leader_id"], g2["id"])
        self.assertEqual(army["leader_name"], "太尉新帅")
        self.assertEqual(army["troop_cap"], 3000)
        chars = {c["id"]: c for c in pv["chars"]}
        self.assertEqual(chars[g2["id"]]["army_role"], "leader")
        self.assertEqual(chars[g2["id"]]["status_name"], "主将")
        self.assertEqual(chars[off["id"]]["army_role"], "member")
        self.assertEqual(chars[off["id"]]["status_name"], "随军")
        self.assertEqual(chars[gen["id"]]["status"], "idle")
        # 官职撤销后上限同口径下调（双向对齐，不保留旧上限）
        g2["appointed_position"] = ""
        await self.m.save_char(g2)
        pv = await self.m._player_view(
            await self.m.get_player("u1", self.wid), self.world,
            await self.m.get_nodes(self.wid))
        army = next(a for a in pv["armies"] if a["id"] == aid)
        self.assertEqual(army["troop_cap"], 1500)

    async def test_army_cap_preview_uses_effective_cap(self):
        """换将弹窗只读预览返回含官职/太守/兵营的实际上限。"""
        p, gen, off, home, _target = await self._army_fixture()
        g2 = await self._add_general(p, home["id"], "中郎将新帅")
        g2["appointed_position"] = "zhonglang"
        await self.m.save_char(g2)
        f = await self.m.get_faction(p["faction_id"])
        f["salary"] = {"enabled": True, "paid_role_char_ids": [g2["id"]]}
        await self.m.save_faction(f)
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        res = await self.m.handle_action(
            "u1", "army_cap_preview",
            {"army_id": r["army_id"], "leader_char_id": g2["id"]})
        self.assertTrue(res["ok"])
        self.assertEqual(res["leader_id"], g2["id"])
        self.assertEqual(res["base_cap"], 2000)
        self.assertEqual(res["troop_cap"], 2000)
        self.assertTrue(res["position_paid"])

    async def test_handover_army_keeps_task_recall_payout(self):
        """换将中断随军任务后，不得用旧角色快照覆盖召回阅历/体力结算。"""
        _p, gen, off, home, _target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [off["id"]], home["id"],
                                     "sword_shield", 500)
        aid = r["army_id"]
        t1 = await self.m.dispatch_task("u1", gen["id"], home["id"], "farm", 1)
        t2 = await self.m.dispatch_task("u1", off["id"], home["id"], "mining", 1)
        now = int(time.time())
        for tid in (t1["task_id"], t2["task_id"]):
            row = await self.m._fetchone(
                "SELECT * FROM strategy_tasks WHERE id=?", (tid,))
            task = self.m._decode("strategy_tasks", row)
            payout = dict(task.get("payout") or {})
            payout["sustained"] = 1
            payout["next_settle_at"] = now
            await self.m._execute(
                "UPDATE strategy_tasks SET start_at=?, payout_json=? WHERE id=?",
                (now - 7200, json.dumps(payout, ensure_ascii=False), int(tid)))
        base = {}
        for cid in (gen["id"], off["id"]):
            c = await self.m.get_char(cid)
            c["exp"] = int(c["exp"]) + 77
            await self.m.save_char(c)
            base[cid] = int(c["exp"])
        g2 = await self._add_general(_p, home["id"], "接任新帅")
        res = await self.m.handover_army("u1", aid, g2["id"], [off["id"]])
        self.assertTrue(res["ok"])
        for cid in (gen["id"], off["id"]):
            c = await self.m.get_char(cid)
            self.assertGreater(
                int(c["exp"]), base[cid],
                "换将保存旧快照覆盖了任务召回阅历")
        # 换下的旧主将回归空闲，留任的随行恢复随军
        self.assertEqual((await self.m.get_char(gen["id"]))["status"], "idle")
        self.assertEqual((await self.m.get_char(off["id"]))["status"], "army")

    async def _add_general(self, p, node_id: int, name: str) -> dict:
        """测试辅助：在指定据点直接落库一名空闲武将。"""
        R = _imp("core.strategy_rules")
        rng = __import__("random").Random(abs(hash(name)) % (2 ** 32))
        c = R.generate_character(rng, rarity="rare", role=R.ROLE_GENERAL,
                                 name=name, name_pools={})
        c.update({"world_id": self.wid, "faction_id": p["faction_id"],
                  "owner_user_id": "u1", "loyalty": 100, "status": "idle",
                  "location": {"node_id": int(node_id)}})
        await self.m.insert_char(c)
        return c

    async def test_march_and_capture(self):
        p = await self._enter()
        gen = next(c for c in await self.m.get_chars(
            owner_user_id="u1", world_id=self.wid) if c["role"] == "general")
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        home = next(n for n in nodes.values() if n["owner_faction"] == 0)
        home["owner_faction"] = p["faction_id"]
        await self.m.save_node(home)
        # 目标：相邻任意非我方、非遗迹据点，清空归属/守军/城防，
        # 保证速胜且走占领分支（避免 NPC 势力/保护期/游军造成随机失败）
        target_id = next(x for x in home["adj"]
                         if nodes[x]["type"] != "ruin")
        target = nodes[target_id]
        target["owner_faction"] = 0
        target["protect_until"] = 0
        target["npc_garrison"] = {"troops": 300, "morale": 70}
        target["defense"] = 0
        target["siege_state"] = {}
        target["construction"] = {}
        target["buildings"] = []
        await self.m.save_node(target)
        p = await self.m.get_player("u1", self.wid)
        p["coin"], p["grain"] = 500_000, 500_000
        await self.m.save_player(p)
        await self._place(gen, home["id"])
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 1400)
        army_id = r["army_id"]
        r2 = await self.m.march("u1", army_id, target_id)
        self.assertTrue(r2["ok"])
        # 推进 tick 直到占领（关闭随机事件/NPC 行动，避免 demon_tide 等改写目标）
        captured = False
        with mock.patch.object(self.m, "_step_events",
                               new=mock.AsyncMock(return_value=[])), \
                mock.patch.object(self.m, "_step_npc_ai",
                                  new=mock.AsyncMock()), \
                mock.patch.object(self.m, "_step_bandits",
                                  new=mock.AsyncMock()):
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

    async def _army_fixture(self):
        """编军测试夹具：己方据点 + 主将 + 内政官 + 充足资源。"""
        p = await self._enter()
        chars = await self.m.get_chars(owner_user_id="u1", world_id=self.wid)
        gen = next(c for c in chars if c["role"] == "general")
        off = next(c for c in chars if c["role"] == "official")
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        home = next(n for n in nodes.values() if n["owner_faction"] == 0)
        home["owner_faction"] = p["faction_id"]
        home["protect_until"] = 0
        await self.m.save_node(home)
        target_id = next(x for x in home["adj"]
                         if nodes[x]["owner_faction"] != p["faction_id"])
        p = await self.m.get_player("u1", self.wid)
        p["coin"], p["grain"] = 500_000, 500_000
        await self.m.save_player(p)
        # §4 所在地机制：编军/任务要求角色已在该据点，夹具统一落位
        await self._place(gen, home["id"])
        await self._place(off, home["id"])
        return p, gen, off, home, target_id

    async def test_create_army_leader_conditions_and_never_spends_iron(self):
        """v4.5.1：主将可为内政官/带伤角色；随行仍须能行动；编军等不耗精铁。"""
        R = _imp("core.strategy_rules")
        p, gen, off, home, target_id = await self._army_fixture()
        # 重伤主将允许上任
        gen["injury"] = R.INJURY_HEAVY
        await self.m.save_char(gen)
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        self.assertTrue(r["ok"])
        await self.m.disband_army("u1", r["army_id"])
        gen["injury"] = 0
        await self.m.save_char(gen)
        # 内政官允许担任主将
        r_off = await self.m.create_army("u1", off["id"], [], home["id"],
                                         "sword_shield", 500)
        self.assertTrue(r_off["ok"])
        await self.m.disband_army("u1", r_off["army_id"])
        # 重伤随行角色仍拒绝
        off["injury"] = R.INJURY_HEAVY
        await self.m.save_char(off)
        with self.assertRaises(self.StrategyError):
            await self.m.create_army("u1", gen["id"], [off["id"]], home["id"],
                                     "sword_shield", 500)
        off["injury"] = 0
        await self.m.save_char(off)
        iron0 = int((await self.m.get_player("u1", self.wid)).get("iron", 0))
        r = await self.m.create_army("u1", gen["id"], [off["id"]], home["id"],
                                     "sword_shield", 500, siege_engine=True)
        aid = r["army_id"]
        await self.m.refill_army("u1", aid, 100)
        await self.m.drill_army("u1", aid)
        await self.m.march("u1", aid, target_id)
        iron1 = int((await self.m.get_player("u1", self.wid)).get("iron", 0))
        self.assertEqual(iron1, iron0, "编军/补员/操练/出征不应消耗精铁")

    async def test_preview_army_cost_matches_create_army_spend(self):
        """编军预算预览不改钱包，且与正式编成的铜钱/粮草扣除完全一致。"""
        p, gen, _off, home, _target = await self._army_fixture()
        coin0, grain0 = int(p["coin"]), int(p["grain"])
        q = (await self.m.preview_army_cost(
            "u1", gen["id"], home["id"], "sword_shield", 500,
            siege_engine=True))["quote"]
        # 只读：预览不得改动钱包与军团
        p_now = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p_now["coin"]), coin0)
        self.assertEqual(int(p_now["grain"]), grain0)
        self.assertFalse(await self.m.get_armies(self.wid, owner_user_id="u1"))
        # 预算口径：征兵 2500 币/1000 粮 + 破城槌，另自动携粮受统率上限约束
        self.assertEqual(q["recruit_coin"], 2500)
        self.assertEqual(q["recruit_grain"], 1000)
        self.assertGreater(q["ram_cost"], 0)
        self.assertTrue(q["affordable"])
        self.assertGreater(q["affordable_troops"], 0)
        # 只读动作走 handle_action 也不开事务/不占限速额度
        prev = await self.m.handle_action("u1", "army_cost_preview", {
            "leader_char_id": gen["id"], "node_id": home["id"],
            "troop_type": "sword_shield", "troops": 500,
            "siege_engine": True})
        self.assertTrue(prev["ok"])
        self.assertEqual(prev["quote"]["coin_cost"], q["coin_cost"])
        self.assertEqual(prev["quote"]["total_grain"], q["total_grain"])
        r = await self.m.create_army(
            "u1", gen["id"], [], home["id"], "sword_shield", 500,
            siege_engine=True)
        p_done = await self.m.get_player("u1", self.wid)
        self.assertEqual(coin0 - int(p_done["coin"]), q["coin_cost"])
        self.assertEqual(grain0 - int(p_done["grain"]), q["total_grain"])
        # 资源不足时给出差额，而不是直接报错
        p_done["coin"] = 1
        await self.m.save_player(p_done)
        poor = (await self.m.preview_army_cost(
            "u1", gen["id"], home["id"], "sword_shield", 500))["quote"]
        self.assertFalse(poor["affordable"])
        self.assertGreater(poor["lacking_coin"], 0)
        self.assertEqual(poor["affordable_troops"], 0)
        await self.m.disband_army("u1", r["army_id"])

    async def test_army_members_can_take_local_tasks_while_stationed(self):
        """驻守军团的主将/随行可在当前据点执行本地任务；远征任务拒绝。"""
        _p, gen, off, home, target_id = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [off["id"]], home["id"],
                                     "sword_shield", 500)
        aid = r["army_id"]
        res = await self.m.dispatch_task("u1", gen["id"], home["id"], "farm", 1)
        self.assertTrue(res["ok"])
        c = await self.m.get_char(gen["id"])
        self.assertEqual(c["status"], "task")
        a = await self.m.get_army(aid)
        self.assertEqual(a["state"], "idle")
        self.assertIn(gen["id"], a["characters"])
        # 随军成员不能执行侦察/劫掠等远征任务
        with self.assertRaises(self.StrategyError):
            await self.m.dispatch_task(
                "u1", off["id"], home["id"], "scout", 1,
                target_node_id=target_id)

    async def test_march_interrupts_army_tasks(self):
        """出军自动中断全军团在途任务，并恢复随军状态。"""
        _p, gen, off, home, target_id = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [off["id"]], home["id"],
                                     "sword_shield", 500)
        aid = r["army_id"]
        t1 = await self.m.dispatch_task("u1", gen["id"], home["id"], "farm", 1)
        t2 = await self.m.dispatch_task("u1", off["id"], home["id"], "mining", 1)
        res = await self.m.march("u1", aid, target_id)
        self.assertTrue(res["ok"])
        self.assertIn("已中断 2 个随军任务", res["msg"])
        for cid in (gen["id"], off["id"]):
            c = await self.m.get_char(cid)
            self.assertEqual(c["status"], "army")
        for tid in (t1["task_id"], t2["task_id"]):
            row = await self.m._fetchone(
                "SELECT status FROM strategy_tasks WHERE id=?", (tid,))
            self.assertEqual(row["status"], "recalled")
        self.assertEqual((await self.m.get_army(aid))["state"], "march")

    async def test_army_member_task_completion_restores_army_status(self):
        """任务自然结束后仍在军团的角色应恢复 status=army，而不是 idle。"""
        _p, gen, _off, home, _target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        aid = r["army_id"]
        res = await self.m.dispatch_task("u1", gen["id"], home["id"], "farm", 1)
        tid = int(res["task_id"])
        row = await self.m._fetchone(
            "SELECT * FROM strategy_tasks WHERE id=?", (tid,))
        task = self.m._decode("strategy_tasks", row)
        payout = dict(task.get("payout") or {})
        payout["sustained"] = 0
        payout["ticks"] = 1
        await self.m._execute(
            "UPDATE strategy_tasks SET end_at=?, payout_json=? WHERE id=?",
            (int(time.time()) - 1,
             json.dumps(payout, ensure_ascii=False), tid))
        await self.m._step_settle_tasks(await self.m.get_world())
        c = await self.m.get_char(gen["id"])
        self.assertEqual(c["status"], "army")
        self.assertEqual(c["location"].get("node_id"), home["id"])
        row = await self.m._fetchone(
            "SELECT status FROM strategy_tasks WHERE id=?", (tid,))
        self.assertEqual(row["status"], "done")
        self.assertEqual((await self.m.get_army(aid))["state"], "idle")

    async def test_disband_keeps_recall_payout(self):
        """v4.5.5：解散军团不得用旧玩家快照覆盖随军任务召回收益。"""
        _p, gen, _off, home, _target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 300)
        aid = r["army_id"]
        res = await self.m.dispatch_task("u1", gen["id"], home["id"], "farm", 1)
        tid = int(res["task_id"])
        row = await self.m._fetchone(
            "SELECT * FROM strategy_tasks WHERE id=?", (tid,))
        task = self.m._decode("strategy_tasks", row)
        payout = dict(task.get("payout") or {})
        # 让下一次召回按满 4 tick 结算（farm 产出粮草）
        payout["next_settle_at"] = int(time.time()) - 1800
        await self.m._execute(
            "UPDATE strategy_tasks SET payout_json=? WHERE id=?",
            (json.dumps(payout, ensure_ascii=False), tid))
        await self.m._commit()

        before = await self.m.get_player("u1", self.wid)
        grain_before = int(before["grain"])
        army = await self.m.get_army(aid)
        await self.m.disband_army("u1", aid)
        after = await self.m.get_player("u1", self.wid)
        # 召回产出 > 0：粮草增量必须严格大于军团携粮返还
        self.assertGreater(int(after["grain"]) - grain_before,
                           int(army["grain"]))

    async def test_heavy_leader_refill_uses_nominal_cap(self):
        """v4.5.5：重伤名义主将的带兵上限不再被健康随行的上限顶掉。"""
        R = _imp("core.strategy_rules")
        _p, gen, off, home, _target = await self._army_fixture()
        gen["injury"] = R.INJURY_HEAVY
        await self.m.save_char(gen)
        # 重伤主将任中郎将（上限 2000）+ 无官职内政官随行（行动主将上限 1500）
        await self.m.faction_appoint_char("u1", int(gen["id"]), "zhonglang")
        r = await self.m.create_army("u1", gen["id"], [off["id"]], home["id"],
                                     "sword_shield", 1500)
        aid = r["army_id"]
        a = await self.m.get_army(aid)
        self.assertEqual(int((await self.m._army_nominal_leader(a))["id"]),
                         int(gen["id"]))
        self.assertEqual(int((await self.m._army_leader(a))["id"]),
                         int(off["id"]))
        # 旧口径会按随行 1500 上限拒绝；新口径按名义主将 2000 放行
        res = await self.m.refill_army("u1", aid, 100)
        self.assertTrue(res["ok"])
        self.assertEqual(int((await self.m.get_army(aid))["troops"]), 1600)

    async def test_dying_leader_rejected_by_create_and_handover(self):
        """v4.5.5：濒死角色不可担任主将（重伤可以）。"""
        R = _imp("core.strategy_rules")
        _p, gen, off, home, _target = await self._army_fixture()
        gen["injury"] = R.INJURY_DYING
        await self.m.save_char(gen)
        with self.assertRaises(self.StrategyError):
            await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 100)
        r = await self.m.create_army("u1", off["id"], [], home["id"],
                                     "sword_shield", 100)
        with self.assertRaises(self.StrategyError):
            await self.m.handover_army("u1", r["army_id"], gen["id"], [])

    async def test_army_commands_reject_empty_or_leaderless_army(self):
        """0 兵力 / 无可用主将的僵尸军团不能补员、操练、出征。"""
        _p, gen, _off, home, target_id = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        aid = r["army_id"]
        a = await self.m.get_army(aid)
        a["troops"] = 0
        await self.m.save_army(a)
        for call in (lambda: self.m.drill_army("u1", aid),
                     lambda: self.m.march("u1", aid, target_id)):
            with self.assertRaises(self.StrategyError):
                await call()
        a = await self.m.get_army(aid)
        a["troops"] = 500
        a["characters"] = []
        await self.m.save_army(a)
        for call in (lambda: self.m.refill_army("u1", aid, 100),
                     lambda: self.m.drill_army("u1", aid),
                     lambda: self.m.march("u1", aid, target_id)):
            with self.assertRaises(self.StrategyError):
                await call()

    async def test_auto_retreat_drops_empty_army_and_orphan_chars(self):
        """0 兵军团与无家可归军团解散后须归还角色，不留僵尸军团。"""
        p, gen, _off, home, _target = await self._army_fixture()
        aid = await self.m.insert_army({
            "world_id": self.wid, "faction_id": p["faction_id"],
            "owner_user_id": "u1", "name": "空营", "node_id": home["id"],
            "characters": [gen["id"]], "troops": 0, "grain": 0,
            "morale": 10, "state": "idle"})
        await self.m._auto_retreat(await self.m.get_army(aid))
        self.assertIsNone(await self.m.get_army(aid))
        self.assertEqual((await self.m.get_char(gen["id"]))["status"], "idle")
        # 无己方据点（不存在的势力）：直接解散，角色同样归还
        aid2 = await self.m.insert_army({
            "world_id": self.wid, "faction_id": 99999,
            "owner_user_id": "", "name": "孤军", "node_id": home["id"],
            "characters": [gen["id"]], "troops": 300, "grain": 0,
            "morale": 50, "state": "march"})
        await self.m._auto_retreat(await self.m.get_army(aid2))
        self.assertIsNone(await self.m.get_army(aid2))
        self.assertEqual((await self.m.get_char(gen["id"]))["status"], "idle")

    async def test_starving_retreat_keeps_march_progress(self):
        """断粮撤退途中不得每 tick 清零 edge_pos，否则慢速军团永远走不回家。"""
        p, gen, _off, home, _target = await self._army_fixture()
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        # 需要一条「下一节点为河（行军耗 3）」的边，单 tick 推进 <1
        river = None
        for n in nodes.values():
            if n.get("terrain") != "river":
                continue
            src = next((s for s in n["adj"]
                        if int(nodes[s]["owner_faction"]) != int(p["faction_id"])), None)
            if src:
                river = (src, n["id"])
                break
        if not river:
            self.skipTest("地图中没有河地形边")
        src, dst = river
        nodes[dst]["owner_faction"] = p["faction_id"]
        nodes[dst]["protect_until"] = 0
        await self.m.save_node(nodes[dst])
        aid = await self.m.insert_army({
            "world_id": self.wid, "faction_id": p["faction_id"],
            "owner_user_id": "u1", "name": "缺粮军", "node_id": src,
            "characters": [gen["id"]], "troops": 1000, "grain": 0,
            "morale": 5, "state": "march", "troop_type": "sword_shield"})
        a = await self.m.get_army(aid)
        a["path"] = [dst]
        a["edge_pos"] = 0.0
        await self.m.save_army(a)
        world = await self.m.get_world()
        await self.m._step_march(world)
        a = await self.m.get_army(aid)
        self.assertGreater(float(a["edge_pos"]), 0.0, "撤退时行军进度被清零")
        self.assertEqual(a["state"], "march")
        # 持续推进若干 tick 后应抵达己方据点并转为驻守
        for _ in range(6):
            await self.m._step_march(world)
            a = await self.m.get_army(aid)
            if not a:
                break
        a = await self.m.get_army(aid)
        self.assertTrue(a is None or int(a["node_id"]) == dst)

    async def test_attack_protected_node_retreats(self):
        """目标在行军途中进入保护期：抵达时须重新判定并撤军。"""
        p, gen, _off, home, _target = await self._army_fixture()
        nodes = await self.m.get_nodes(self.wid)
        target = next(n for n in nodes
                      if int(n["owner_faction"]) not in (0, int(p["faction_id"])))
        target["protect_until"] = int(time.time()) + 3600
        target["siege_state"] = {}
        await self.m.save_node(target)
        aid = await self.m.insert_army({
            "world_id": self.wid, "faction_id": p["faction_id"],
            "owner_user_id": "u1", "name": "试探军", "node_id": home["id"],
            "characters": [gen["id"]], "troops": 500, "grain": 100,
            "morale": 100, "state": "march"})
        a = await self.m.get_army(aid)
        a["path"] = []
        await self.m.save_army(a)
        world = await self.m.get_world()
        owner_before = int((await self.m.get_node(self.wid, target["id"]))["owner_faction"])
        report = await self.m._attack_node(
            world, await self.m.get_node(self.wid, target["id"]), [a])
        self.assertIn("保护期", report)
        node_after = await self.m.get_node(self.wid, target["id"])
        self.assertEqual(int(node_after["owner_faction"]), owner_before)
        self.assertEqual(int(node_after["defense"]), int(target["defense"]))

    async def test_siege_stops_when_node_becomes_protected(self):
        """围城中途目标进入保护期（护山盾/夺城免战）→ 攻方撤围，城防不再被削。"""
        p, gen, _off, home, _target = await self._army_fixture()
        nodes = await self.m.get_nodes(self.wid)
        node = next(n for n in nodes
                    if int(n["owner_faction"]) not in (0, int(p["faction_id"]))
                    and int(n["defense"]) > 0)
        node["protect_until"] = int(time.time()) + 3600
        node["siege_state"] = {"ticks": 1, "attacker_faction": p["faction_id"],
                               "choice": "auto", "since": int(time.time())}
        await self.m.save_node(node)
        aid = await self.m.insert_army({
            "world_id": self.wid, "faction_id": p["faction_id"],
            "owner_user_id": "u1", "name": "围城军", "node_id": node["id"],
            "characters": [gen["id"]], "troops": 800, "grain": 100,
            "morale": 100, "state": "siege"})
        world = await self.m.get_world()
        reports = await self.m._step_sieges(world)
        self.assertTrue(any("保护期" in r for r in reports))
        node_after = await self.m.get_node(self.wid, node["id"])
        self.assertEqual(int(node_after["defense"]), int(node["defense"]))
        self.assertEqual(node_after.get("siege_state") or {}, {})
        a = await self.m.get_army(aid)
        self.assertEqual(a["state"], "march")

    async def test_ram_cost_applies_military_tech_discount(self):
        """兵学 L2 的 ram_cost_mult 应作用于破城槌造价（此前配置未接入）。"""
        p, gen, _off, home, _target = await self._army_fixture()
        f = await self.m.get_faction(p["faction_id"])
        f["tech"] = {"military": 2}
        await self.m.save_faction(f)
        coin0 = int((await self.m.get_player("u1", self.wid))["coin"])
        await self.m.create_army("u1", gen["id"], [], home["id"],
                                 "sword_shield", 500, siege_engine=True)
        coin1 = int((await self.m.get_player("u1", self.wid))["coin"])
        # 剑盾 500 兵 = 2500；破城槌 3000 × 0.7 = 2100
        self.assertEqual(coin0 - coin1, 2500 + 2100)

    async def test_siege_timeout_injures_leader_and_retreats(self):
        """围城超时：攻方粮尽撤退，主将伤势 +timeout_injury（默认 10）。"""
        p, gen, _off, _home, _target = await self._army_fixture()
        cfg = self.m.main_cfg().get("siege") or {}
        nodes = await self.m.get_nodes(self.wid)
        node = next(n for n in nodes
                    if int(n["owner_faction"]) not in (0, int(p["faction_id"]))
                    and int(n["defense"]) > 0)
        node["protect_until"] = 0
        node["siege_state"] = {"ticks": int(cfg.get("max_ticks", 20)) - 1,
                               "attacker_faction": p["faction_id"],
                               "choice": "auto", "since": int(time.time())}
        await self.m.save_node(node)
        aid = await self.m.insert_army({
            "world_id": self.wid, "faction_id": p["faction_id"],
            "owner_user_id": "u1", "name": "久攻军", "node_id": node["id"],
            "characters": [gen["id"]], "troops": 1200, "grain": 100,
            "morale": 100, "state": "siege"})
        injury0 = int((await self.m.get_char(gen["id"]))["injury"])
        reports = await self.m._step_sieges(await self.m.get_world())
        self.assertTrue(any("超时" in r for r in reports))
        self.assertEqual(int((await self.m.get_char(gen["id"]))["injury"]),
                         min(99, injury0 + int(cfg.get("timeout_injury", 10))))
        self.assertNotEqual((await self.m.get_army(aid))["state"], "siege")

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
        # 升级不再即时：先进入 construction，等级不变
        node = await self.m.get_node(self.wid, city["id"])
        self.assertEqual(node["level"], 1)
        self.assertTrue(node.get("construction"))
        self.assertEqual(int(node["construction"]["to_level"]), 2)
        # 建造中不可重复升级
        with self.assertRaises(self.StrategyError):
            await self.m.upgrade_node("u1", city["id"])
        node = await self._finish_construction(city["id"])
        self.assertEqual(node["level"], 2)
        self.assertEqual(node["defense"], 2000)
        self.assertFalse(node.get("construction"))


# ------------------------------------------------------------------
    # v4.1.0 缺口补全回归（建筑 / 民心 / 主城 / 溃败 / NPC / 安全 / buff）
    # ------------------------------------------------------------------
    async def test_build_nine_buildings_and_level_cap(self):
        """§6.6 九种建筑均可建造，等级 1→5，满级/重复建造报错。"""
        p = await self._enter()
        city = next(n for n in await self.m.get_nodes(self.wid)
                    if n["type"] == "pass" and n["level"] == 1)
        city["owner_faction"] = p["faction_id"]
        city["siege_state"] = {}
        await self.m.save_node(city)
        p = await self.m.get_player("u1", self.wid)
        p["coin"], p["iron"] = 1_000_000, 1_000_000
        await self.m.save_player(p)
        self.assertEqual(len(self.m.BUILDINGS), 9)
        for bid in self.m.BUILDINGS:
            r = await self.m.build("u1", city["id"], bid)
            self.assertTrue(r["ok"], bid)
            self.assertEqual(int(r["level"]), 1)
        with self.assertRaises(self.StrategyError):
            await self.m.build("u1", city["id"], "tower")
        for _ in range(4):
            await self.m.upgrade_building("u1", city["id"], "tower")
        node = await self.m.get_node(self.wid, city["id"])
        self.assertEqual(self.m._building_level(node, "tower"), 5)
        with self.assertRaises(self.StrategyError):
            await self.m.upgrade_building("u1", city["id"], "tower")

    async def test_upgrade_node_cap_is_five(self):
        """§10.2 据点等级上限 = 据点潜力（city=5、mine/grain=2）。"""
        p = await self._enter()
        city = next(n for n in await self.m.get_nodes(self.wid)
                    if n["type"] == "city")
        city["owner_faction"] = p["faction_id"]
        city["level"] = 1
        city["potential"] = 5
        city["siege_state"] = {}
        await self.m.save_node(city)
        p = await self.m.get_player("u1", self.wid)
        p["coin"], p["iron"] = 1_000_000, 1_000_000
        await self.m.save_player(p)
        for _ in range(4):
            r = await self.m.upgrade_node("u1", city["id"])
            self.assertTrue(r["ok"])
            await self._finish_construction(city["id"])
        node = await self.m.get_node(self.wid, city["id"])
        self.assertEqual(node["level"], 5)
        with self.assertRaises(self.StrategyError):
            await self.m.upgrade_node("u1", city["id"])
        # 矿/田潜力只有 2 级
        mine = next(n for n in await self.m.get_nodes(self.wid)
                    if n["type"] == "mine")
        mine["owner_faction"] = p["faction_id"]
        mine["level"] = 1
        mine["potential"] = 2
        await self.m.save_node(mine)
        self.assertEqual(self.m._node_level_cap(mine), 2)
        self.assertTrue((await self.m.upgrade_node("u1", mine["id"]))["ok"])
        await self._finish_construction(mine["id"])
        mine2 = await self.m.get_node(self.wid, mine["id"])
        self.assertEqual(mine2["level"], 2)
        with self.assertRaises(self.StrategyError):
            await self.m.upgrade_node("u1", mine["id"])

    async def test_node_output_mult_manor_and_capital(self):
        """城主府 +6%/级；主城额外 +capital_output_bonus。"""
        p = await self._enter()
        f = await self.m.get_faction(p["faction_id"])
        node = await self.m.get_node(self.wid, int(f["capital_node_id"]))
        node["buildings"] = [{"id": "manor", "level": 3}]
        f_capital = dict(f)
        mult = self.m._node_output_mult(node, f_capital)
        self.assertAlmostEqual(mult, 1.18 * 1.1, places=4)

    async def test_govern_relief_morale_and_daily_limits(self):
        """§8.6 赈济：+relief_morale，每据点 2 次/日、每人 3 次/日。"""
        p = await self._enter()
        f = await self.m.get_faction(p["faction_id"])
        node = await self.m.get_node(self.wid, int(f["capital_node_id"]))
        node["morale"] = 50
        await self.m.save_node(node)
        p = await self.m.get_player("u1", self.wid)
        p["coin"] = 100_000
        await self.m.save_player(p)
        r = await self.m.handle_action(
            "u1", "govern_relief", {"node_id": node["id"], "resource": "coin"})
        self.assertEqual(r["morale"], 56)
        r2 = await self.m.handle_action(
            "u1", "govern_relief", {"node_id": node["id"], "resource": "coin"})
        self.assertEqual(r2["morale"], 62)
        with self.assertRaises(self.StrategyError):
            await self.m.handle_action(
                "u1", "govern_relief", {"node_id": node["id"], "resource": "coin"})
        state = await self.m.get_state("u1")
        row = next(n for n in state["nodes"] if n["id"] == node["id"])
        self.assertEqual(row["relief_used"], 2)

    async def test_govern_relief_grain_and_errors(self):
        """赈济可用粮草；资源不足/非己方据点报中文错误。"""
        p = await self._enter()
        f = await self.m.get_faction(p["faction_id"])
        node = await self.m.get_node(self.wid, int(f["capital_node_id"]))
        node["morale"] = 50
        await self.m.save_node(node)
        p = await self.m.get_player("u1", self.wid)
        p["grain"] = 5000
        await self.m.save_player(p)
        before = int((await self.m.get_player("u1", self.wid))["grain"])
        r = await self.m.govern_relief("u1", node["id"], "grain")
        self.assertEqual(r["morale"], 56)
        after = int((await self.m.get_player("u1", self.wid))["grain"])
        self.assertEqual(before - after,
                         int(self.m.main_cfg()["governance"]["relief_grain_cost"]))
        foreign = next(n for n in await self.m.get_nodes(self.wid)
                       if n["owner_faction"] != p["faction_id"])
        with self.assertRaises(self.StrategyError):
            await self.m.govern_relief("u1", foreign["id"], "coin")

    async def test_capital_fall_exiles_faction_without_nodes(self):
        """§8.7 主城陷落且无其他据点 → capital=0 且流亡。"""
        p = await self._enter()
        f = await self.m.get_faction(p["faction_id"])
        cap = await self.m.get_node(self.wid, int(f["capital_node_id"]))
        await self.m._capture_node(await self.m.get_world(), cap, 0)
        f2 = await self.m.get_faction(p["faction_id"])
        self.assertEqual(int(f2["capital_node_id"]), 0)
        self.assertEqual(f2["faction_state"], "exiled")

    async def test_capital_fall_keeps_faction_normal_with_other_nodes(self):
        """仍有其他据点时只清空主城标记，不进入流亡。"""
        p = await self._enter()
        f = await self.m.get_faction(p["faction_id"])
        cap = await self.m.get_node(self.wid, int(f["capital_node_id"]))
        other = next(n for n in await self.m.get_nodes(self.wid)
                     if n["owner_faction"] == 0 and n["type"] != "ruin")
        other["owner_faction"] = p["faction_id"]
        await self.m.save_node(other)
        await self.m._capture_node(await self.m.get_world(), cap, 0)
        f2 = await self.m.get_faction(p["faction_id"])
        self.assertEqual(int(f2["capital_node_id"]), 0)
        self.assertNotEqual(f2["faction_state"], "exiled")

    async def test_ruin_first_capture_sealed_by_default(self):
        """v4.5.1 封存：默认不回赠装备/图纸，也不写 first_captured。"""
        p = await self._enter()
        before = int(p.get("iron", 0) or 0)
        ruin = next(n for n in await self.m.get_nodes(self.wid)
                    if n["type"] == "ruin")
        ruin["owner_faction"] = 0
        ruin["output"] = {}
        await self.m.save_node(ruin)

        class _Rng:
            def random(self):
                return 0.0

        with mock.patch.object(self.m, "_rng", return_value=_Rng()):
            await self.m._capture_node(await self.m.get_world(), ruin,
                                       p["faction_id"])
        node = await self.m.get_node(self.wid, ruin["id"])
        self.assertFalse((node.get("output") or {}).get("first_captured"))
        self.assertNotIn("秘境", ruin.get("_capture_note", ""))
        p2 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p2.get("iron", 0) or 0), before)

    async def test_ruin_first_capture_reward_when_reenabled(self):
        """开关打开时保留旧口径（掉落只触发一次），便于后续版本复活。"""
        p = await self._enter()
        self.m.main_cfg()["ruin_first_capture"] = {"enabled": True, "chance": 0.3}
        ruin = next(n for n in await self.m.get_nodes(self.wid)
                    if n["type"] == "ruin")
        ruin["owner_faction"] = 0
        ruin["output"] = {}
        await self.m.save_node(ruin)

        class _Rng:
            def random(self):
                return 0.0

        with mock.patch.object(self.m, "_rng", return_value=_Rng()):
            await self.m._capture_node(await self.m.get_world(), ruin,
                                       p["faction_id"])
        node = await self.m.get_node(self.wid, ruin["id"])
        self.assertEqual(int((node.get("output") or {}).get("first_captured", 0)), 1)
        self.assertIn("秘境", ruin.get("_capture_note", ""))
        p2 = await self.m.get_player("u1", self.wid)
        self.assertGreaterEqual(int(p2.get("iron", 0)), 300)
        # 二次易主不再掉落
        await self.m._capture_node(await self.m.get_world(), node, 0)
        self.assertEqual(node.get("output", {}).get("first_captured"), 1)

    async def test_auto_retreat_rout_penalty_once_and_reorganize(self):
        """§7.4 溃败：扣 30% 兵、士气压至 30、进入 2 tick 重整。"""
        p, gen, _off, home, _target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        a = await self.m.get_army(r["army_id"])
        a["troops"], a["morale"] = 1000, 90
        a["_routed"] = True
        await self.m.save_army(a)
        await self.m._auto_retreat(a, int(p["faction_id"]))
        a2 = await self.m.get_army(r["army_id"])
        self.assertEqual(a2["troops"], 700)
        self.assertLessEqual(a2["morale"], 30)
        self.assertGreater(int(a2["reorganize_until_tick"]), 0)
        await self.m._auto_retreat(a2, int(p["faction_id"]))
        a3 = await self.m.get_army(r["army_id"])
        self.assertEqual(a3["troops"], 700, "同一次溃败不得重复扣兵")

    async def test_action_rate_limit(self):
        """§18.4 指令限速：超过每分钟上限抛中文错误。"""
        await self._enter()
        sec = self.m.main_cfg()["security"]
        sec["action_rate_per_min"] = 1
        await self.m.handle_action("u1", "refresh_tavern", {})
        with self.assertRaises(self.StrategyError):
            await self.m.handle_action("u1", "refresh_tavern", {})

    async def test_battle_result_truncation_and_archive(self):
        """§18.4 战报超限截断；超期战报归档。"""
        sec = self.m.main_cfg()["security"]
        sec["result_max_bytes"] = 300
        big = {"winner": "attacker", "rounds": 3,
               "round_log": [{"round": 1, "log": ["x" * 500]}],
               "attackers": [{"id": 1, "troops_after": 10}]}
        bid = await self.m.insert_battle({
            "world_id": self.wid, "node_id": 1, "kind": "field",
            "result": big, "report_text": "战" * 2000, "created_at": 1})
        row = await self.m._fetchone(
            "SELECT * FROM strategy_battles WHERE id=?", (bid,))
        b = self.m._decode("strategy_battles", row)
        self.assertTrue((b.get("result") or {}).get("truncated"))
        self.assertEqual((b.get("result") or {}).get("winner"), "attacker")
        self.assertLess(len(b["report_text"].encode()), 2000)
        # 归档清理（result_keep_days 默认 7 天）
        bid2 = await self.m.insert_battle({
            "world_id": self.wid, "node_id": 1, "kind": "field",
            "result": {"winner": "defender"}, "report_text": "旧的",
            "created_at": _now_minus(9 * 86400)})
        await self.m._step_cleanup_battles(await self.m.get_world())
        row2 = await self.m._fetchone(
            "SELECT * FROM strategy_battles WHERE id=?", (bid2,))
        b2 = self.m._decode("strategy_battles", row2)
        self.assertTrue((b2.get("result") or {}).get("archived"))

    async def test_dispatch_scout_any_node_and_raid_adjacency(self):
        """§6.2 侦察可指向任意节点；劫掠目标须与己方据点相邻。"""
        p, _gen, off, home, _target = await self._army_fixture()
        far = next(n for n in await self.m.get_nodes(self.wid)
                   if n["id"] != home["id"])
        r = await self.m.dispatch_task(
            "u1", off["id"], home["id"], "scout", 1,
            target_node_id=int(far["id"]))
        self.assertTrue(r["ok"])
        task = (await self.m.get_tasks(self.wid, status="active"))[0]
        self.assertEqual(int(task["node_id"]), int(far["id"]))
        self.assertEqual(int((task.get("payout") or {}).get("target_node_id")),
                         int(far["id"]))
        # 非相邻目标劫掠
        nodes = await self.m.get_nodes(self.wid)
        own_adj = set(home.get("adj") or [])
        remote = next(n for n in nodes
                      if n["owner_faction"] != p["faction_id"]
                      and n["id"] not in own_adj)
        with self.assertRaises(self.StrategyError):
            await self.m.dispatch_task("u1", off["id"], home["id"], "raid", 1,
                                       target_node_id=int(remote["id"]))

    async def test_supply_task_moves_player_grain_to_army(self):
        """§6.2 运粮：扣玩家粮草，等量补给我方军团。"""
        p, gen, off, home, _target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        army = await self.m.get_army(r["army_id"])
        # 新编军团默认已携满粮（min(库存, 统率×50)），先消耗一部分再补
        army["grain"] = 0
        await self.m.save_army(army)
        army = await self.m.get_army(r["army_id"])
        before_army = int(army["grain"])
        before_player = int((await self.m.get_player("u1", self.wid))["grain"])
        res = await self.m.dispatch_task("u1", off["id"], home["id"], "supply", 1,
                                        target_army_id=int(army["id"]))
        self.assertTrue(res["ok"])
        army2 = await self.m.get_army(r["army_id"])
        p2 = await self.m.get_player("u1", self.wid)
        self.assertGreater(int(army2["grain"]), before_army)
        self.assertLess(int(p2["grain"]), before_player)

    async def test_supply_task_targets_specific_army_and_instant_for_idle(self):
        """v4.3.2：多军团必须指定 army_id；驻扎军团立即到账。"""
        R = _imp("core.strategy_rules")
        p, gen, off, home, _target = await self._army_fixture()
        r1 = await self.m.create_army("u1", gen["id"], [], home["id"],
                                      "sword_shield", 500)
        rng = __import__("random").Random(7)
        c2 = R.generate_character(rng, rarity="rare", role=R.ROLE_GENERAL,
                                  name_pools={})
        c2.update({"world_id": self.wid, "faction_id": p["faction_id"],
                   "owner_user_id": "u1", "loyalty": 100, "status": "idle"})
        await self.m.insert_char(c2)
        await self._place(c2, home["id"])
        r2 = await self.m.create_army("u1", c2["id"], [], home["id"],
                                      "sword_shield", 500)
        for aid in (r1["army_id"], r2["army_id"]):
            a = await self.m.get_army(aid)
            a["grain"] = 0
            await self.m.save_army(a)
        with self.assertRaises(self.StrategyError):
            await self.m.dispatch_task("u1", off["id"], home["id"], "supply", 1)
        res = await self.m.dispatch_task(
            "u1", off["id"], home["id"], "supply", 1,
            target_army_id=int(r2["army_id"]))
        self.assertTrue(res["ok"])
        a1 = await self.m.get_army(r1["army_id"])
        a2 = await self.m.get_army(r2["army_id"])
        self.assertEqual(int(a1["grain"]), 0)
        self.assertGreater(int(a2["grain"]), 0)
        # v4.6.7：驻扎据点就地补粮，不建任务、不占用角色
        self.assertEqual(int(res.get("task_id") or 0), 0)
        self.assertEqual(await self.m.get_tasks(self.wid, status="active"), [])
        self.assertEqual((await self.m.get_char(off["id"]))["status"], "idle")

    async def test_instant_supply_needs_no_runner_or_exp(self):
        """v4.6.7：驻扎军团就地补粮，无需空闲角色、不占人、不给任务经验。"""
        _p, gen, off, home, _target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        army = await self.m.get_army(r["army_id"])
        army["grain"] = 0
        await self.m.save_army(army)
        before = int((await self.m.get_char(off["id"])).get("exp", 0))
        res = await self.m.dispatch_task(
            "u1", 0, home["id"], "supply", 8,   # char_id=0：不选押运角色
            target_army_id=int(army["id"]))
        self.assertTrue(res["ok"])
        self.assertEqual(int(res.get("task_id") or 0), 0)
        self.assertIn("无需押运角色", res["msg"])
        off2 = await self.m.get_char(off["id"])
        self.assertEqual(off2["status"], "idle")
        self.assertEqual(int(off2.get("exp", 0)), before)
        await self.m.tick()
        after = int((await self.m.get_char(off["id"])).get("exp", 0))
        self.assertEqual(after, before)

    async def test_supply_at_allied_node_is_instant(self):
        """v4.6.5/v4.6.7：驻扎盟友据点可就地补粮，无需押运角色或补粮队。"""
        R = _imp("core.strategy_rules")
        p, gen, off, _home, _target = await self._army_fixture()
        p2 = await self._enter("u2", "盟友道人")
        f1 = await self.m.get_faction(int(p["faction_id"]))
        f2 = await self.m.get_faction(int(p2["faction_id"]))
        f2["alliance_id"] = int(f1["id"])
        await self.m.save_faction(f2)
        nodes = await self.m.get_nodes(self.wid)
        ally = next(n for n in nodes
                    if int(n["owner_faction"]) == 0
                    and str(n["type"]) in R.CHAR_DWELL_NODE_TYPES)
        ally["owner_faction"] = int(f2["id"])
        await self.m.save_node(ally)
        await self.m._commit()
        await self._place(gen, ally["id"])
        await self._place(off, ally["id"])
        r = await self.m.create_army("u1", gen["id"], [], ally["id"],
                                     "sword_shield", 500)
        army = await self.m.get_army(r["army_id"])
        army["grain"] = 0
        await self.m.save_army(army)
        res = await self.m.dispatch_task(
            "u1", 0, ally["id"], "supply", 1,
            target_army_id=int(army["id"]))
        self.assertTrue(res["ok"])
        self.assertEqual(int(res.get("task_id") or 0), 0)
        self.assertGreater(int((await self.m.get_army(r["army_id"]))["grain"]), 0)
        self.assertEqual(await self.m.get_tasks(self.wid, status="active"), [])
        self.assertEqual((await self.m.get_char(off["id"]))["status"], "idle")

    async def test_marching_supply_still_requires_runner(self):
        """v4.6.7：行军/围城军团仍必须选空闲角色押运补粮队。"""
        p, gen, off, home, target = await self._army_fixture()
        tnode = await self.m.get_node(self.wid, target)
        tnode["owner_faction"] = p["faction_id"]
        tnode["protect_until"] = 0
        await self.m.save_node(tnode)
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        army = await self.m.get_army(r["army_id"])
        army["grain"] = 0
        army["state"] = "march"
        army["node_id"] = int(home["id"])
        army["path"] = [int(target)]
        army["edge_pos"] = 0.0
        await self.m.save_army(army)
        with self.assertRaises(self.StrategyError):
            await self.m.dispatch_task(
                "u1", 0, home["id"], "supply", 1,
                target_army_id=int(army["id"]))
        res = await self.m.dispatch_task(
            "u1", off["id"], home["id"], "supply", 1,
            target_army_id=int(army["id"]))
        self.assertTrue(res["ok"])
        self.assertGreater(int(res.get("task_id") or 0), 0)
        task = (await self.m.get_tasks(self.wid, status="active"))[0]
        self.assertEqual(int((task["payout"] or {}).get("runner") or 0), 1)
        self.assertEqual((await self.m.get_char(off["id"]))["status"], "task")

    async def test_supply_runner_chases_marching_army_and_delivers(self):
        """v4.3.2：行军军团由补粮队追击，追上即交付并结束任务。"""
        p, gen, off, home, target = await self._army_fixture()
        tnode = await self.m.get_node(self.wid, target)
        tnode["owner_faction"] = p["faction_id"]
        tnode["protect_until"] = 0
        await self.m.save_node(tnode)
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        army = await self.m.get_army(r["army_id"])
        army["grain"] = 0
        # 固定“行军中”状态（不再触发随机地图上的遭遇/游军干扰）
        army["state"] = "march"
        army["node_id"] = int(home["id"])
        army["path"] = [int(target)]
        army["edge_pos"] = 0.0
        await self.m.save_army(army)
        res = await self.m.dispatch_task(
            "u1", off["id"], home["id"], "supply", 1,
            target_army_id=int(army["id"]))
        self.assertTrue(res["ok"])
        world = await self.m.get_world()
        self.assertEqual(len((world.get("data") or {}).get("army_supplies") or []), 1)
        # 固定军团位置并关闭随机匪寨劫掠，保证补粮队必然送达
        with mock.patch.object(self.m, "_step_march", new=mock.AsyncMock()), \
                mock.patch.object(self.m, "_find_convoy_raider",
                                  new=mock.AsyncMock(return_value=None)):
            await self.m.tick()
            await self.m.tick()
        army2 = await self.m.get_army(r["army_id"])
        world2 = await self.m.get_world()
        self.assertGreater(int(army2["grain"]), 0)
        self.assertEqual((world2.get("data") or {}).get("army_supplies") or [], [])
        task = (await self.m.get_tasks(self.wid, status="done"))[-1]
        self.assertEqual(task["task_type"], "supply")
        # v4.3.3：送达后回写 delivered=1，任务展示与召回判定依赖该字段
        self.assertEqual(int((task["payout"] or {}).get("delivered") or 0), 1)
        off2 = await self.m.get_char(off["id"])
        self.assertEqual(off2["status"], "idle")

    async def test_supply_cap_rejects_full_army(self):
        """携粮已满时运粮任务拒绝，避免无限囤粮。"""
        _p, gen, off, home, _target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        with self.assertRaises(self.StrategyError):
            await self.m.dispatch_task("u1", off["id"], home["id"], "supply", 1,
                                       target_army_id=int(r["army_id"]))

    async def test_demon_tide_captures_without_protection(self):
        """v4.3.2：魔潮会占点，但据点不写新城保护期；事件结束保留领地。"""
        p = await self._enter()
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n["owner_faction"]) == int(p["faction_id"]))
        node["stock"] = {"coin": 1000, "grain": 1000, "iron": 1000}
        node["morale"] = 80
        node["defense"] = 1000
        await self.m.save_node(node)
        world = await self.m.get_world()
        await self.m._capture_node(world, node, -1)
        node2 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(node2["owner_faction"]), -1)
        self.assertEqual(int(node2["protect_until"]), 0)
        # 残留保护期会在下一 tick 被清掉；据点仍归魔潮
        node2["protect_until"] = int(time.time()) + 3600
        node2["owner_faction"] = -1
        await self.m.save_node(node2)
        await self.m._step_demon_tide(world)
        node3 = await self.m.get_node(self.wid, int(node["id"]))
        self.assertEqual(int(node3["owner_faction"]), -1)
        self.assertEqual(int(node3["protect_until"]), 0)

    async def test_demon_hunter_targets_strongest_faction(self):
        """v4.3.2：猎潮军登记最强势力，出征目标为该势力据点。"""
        await self._enter()
        world = await self.m.get_world()
        strongest, pct = await self.m._strongest_faction(world)
        self.assertGreater(strongest, 0)
        self.assertGreater(pct, 0)
        rng = __import__("random").Random(3)
        army = await self.m._spawn_demon_hunter(
            world, rng, strongest, set())
        self.assertIsNotNone(army)
        await self.m.save_world(world)
        await self.m._commit()
        world = await self.m.get_world()
        hunter = (world.get("data") or {}).get("demon_hunters", {}).get(
            str(army["id"]))
        self.assertIsNotNone(hunter)
        self.assertEqual(int(hunter["target_faction"]), int(strongest))
        await self.m._step_npc_ai(world)
        army2 = await self.m.get_army(army["id"])
        self.assertEqual(army2["state"], "march")
        dest = int(army2["path"][-1])
        dest_node = await self.m.get_node(self.wid, dest)
        self.assertEqual(int(dest_node["owner_faction"]), int(strongest))

    async def test_demon_tide_periodically_spawns_hunters(self):
        """v4.3.2：魔潮持续期间每 hunter_interval_ticks 额外生成猎潮军。"""
        await self._enter()
        world = await self.m.get_world()
        strongest, _pct = await self.m._strongest_faction(world)
        self.assertGreater(strongest, 0)
        data = world.get("data") or {}
        tick_no = max(10, int(world.get("tick_no", 0) or 0))
        data["demon_next_hunter_tick"] = tick_no
        world["data"] = data
        rules = {"max_armies": 8, "hunter_interval_ticks": 8,
                 "hunter_count": 1}
        await self.m._maybe_spawn_demon_hunters(world, tick_no, data, rules)
        await self.m.save_world(world)
        await self.m._commit()
        armies = await self.m.get_armies(self.wid, faction_id=-1)
        self.assertEqual(len(armies), 1)
        world = await self.m.get_world()
        hunter = (world.get("data") or {}).get("demon_hunters", {}).get(
            str(armies[0]["id"]))
        self.assertEqual(int(hunter["target_faction"]), int(strongest))
        self.assertEqual(int((world.get("data") or {}).get(
            "demon_next_hunter_tick")), tick_no + 8)

    async def test_morale_cap_uses_veteran_tier_and_wipeout_ashes(self):
        """v4.3.2：驻守恢复按精锐/御林上限；全灭保留一半老兵值。"""
        _p, gen, _off, home, _target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        army = await self.m.get_army(r["army_id"])
        army["veteran"] = 300
        army["morale"] = 119
        await self.m.save_army(army)
        world = await self.m.get_world()
        await self.m._step_recovery(world)
        army = await self.m.get_army(r["army_id"])
        self.assertEqual(int(army["morale"]), 120)
        keep = self.m._remember_veteran_ashes(world, "u1", 300)
        self.assertEqual(keep, 150)
        await self.m.save_world(world)
        # 新编军继承一半老兵值
        R = _imp("core.strategy_rules")
        rng = __import__("random").Random(11)
        c2 = R.generate_character(rng, rarity="rare", role=R.ROLE_GENERAL,
                                  name_pools={})
        c2.update({"world_id": self.wid, "faction_id": army["faction_id"],
                   "owner_user_id": "u1", "loyalty": 100, "status": "idle"})
        await self.m.insert_char(c2)
        await self._place(c2, home["id"])
        r2 = await self.m.create_army("u1", c2["id"], [], home["id"],
                                      "sword_shield", 500)
        army2 = await self.m.get_army(r2["army_id"])
        self.assertEqual(int(army2["veteran"]), 150)

    async def test_siege_upkeep_consumes_grain_and_starves(self):
        """§7.2：围城双方粮耗照常，断粮按行军同口径掉士气。"""
        _p, gen, _off, home, target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        army = await self.m.get_army(r["army_id"])
        army["grain"] = 0
        army["morale"] = 100
        army["starve_ticks"] = 0
        world = await self.m.get_world()
        starved = await self.m._consume_army_upkeep(
            world, army, "plain", True)
        self.assertFalse(starved)
        self.assertEqual(int(army["grain"]), 0)
        self.assertEqual(int(army["morale"]), 80)
        self.assertEqual(int(army["starve_ticks"]), 1)

    async def test_partner_task_occupies_both_and_marks_payout(self):
        """§6.1 双人协作：两人同时占用，收益标记 ×1.15。"""
        _p, gen, off, home, _target = await self._army_fixture()
        res = await self.m.dispatch_task(
            "u1", gen["id"], home["id"], "farm", 1,
            partner_char_id=off["id"])
        self.assertTrue(res["ok"])
        c1 = await self.m.get_char(gen["id"])
        c2 = await self.m.get_char(off["id"])
        self.assertEqual(c1["status"], "task")
        self.assertEqual(c2["status"], "task")
        task = (await self.m.get_tasks(self.wid, status="active"))[0]
        self.assertEqual(int((task.get("payout") or {}).get("partner_char_id")),
                         int(off["id"]))

    async def test_overstaff_army_loses_morale_each_tick(self):
        """§7.5 超编军团每 tick 扣士气。"""
        _p, gen, _off, home, target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 500)
        a = await self.m.get_army(r["army_id"])
        a["troops"] = int(gen["stats"]["leadership"]) * 20 + 500
        a["morale"] = 80
        a["node_id"] = int(target)
        await self.m.save_army(a)
        await self.m._step_recovery(await self.m.get_world())
        a2 = await self.m.get_army(r["army_id"])
        self.assertLess(a2["morale"], 80)

    async def test_riot_reduces_garrison_and_raises_morale(self):
        """§8.6 哗变：驻军 -30%、民心 +5（关闭自然增长以隔离断言）。"""
        R = _imp("core.strategy_rules")
        p = await self._enter()
        f = await self.m.get_faction(p["faction_id"])
        node = await self.m.get_node(self.wid, int(f["capital_node_id"]))
        node["morale"] = 0
        node["npc_garrison"] = {"troops": 1000, "morale": 70}
        await self.m.save_node(node)
        # 移走驻守角色，避免玩家驻守加成干扰精确断言
        for c in await self.m.get_chars(faction_id=int(p["faction_id"]),
                                        world_id=self.wid):
            c["location"] = {}
            await self.m.save_char(c)
        self.m.main_cfg()["garrison"].update(
            {"grow_pct": 0, "char_grow_pct": 0})
        with mock.patch.object(R, "riot_check", return_value=True):
            await self.m._step_recovery(await self.m.get_world())
        node2 = await self.m.get_node(self.wid, node["id"])
        self.assertEqual(int(node2["npc_garrison"]["troops"]), 700)
        self.assertGreaterEqual(int(node2["morale"]), 5)

    async def test_npc_garrison_respawn_and_hatred(self):
        """§9.1/§10.4 守军无重生倒计时：respawn_at 不生效，改为自然恢复。"""
        row = await self.m._fetchone(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc'",
            (self.wid,))
        fid = int(row["id"])
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n["owner_faction"]) == fid)
        node["npc_garrison"] = {"troops": 0, "morale": 100}
        node["respawn_at"] = _now_minus(1)
        node["hatred"] = {"1": 10}
        node["siege_state"] = {}
        await self.m.save_node(node)
        # 1) NPC AI 不再按 respawn_at 复活守军
        await self.m._step_npc_ai(await self.m.get_world())
        node2 = await self.m.get_node(self.wid, node["id"])
        self.assertEqual(int(node2["npc_garrison"]["troops"]), 0)
        # 仇恨逐 tick 衰减（不再因重生而强化）
        self.assertLessEqual(int((node2.get("hatred") or {}).get("1", 0)), 10)
        # 2) 自然恢复：+2% 上限 × 民心/100（民心 100 → 2%）
        node2["morale"] = 100
        node2["npc_garrison"] = {"troops": 0, "morale": 100}
        await self.m.save_node(node2)
        cap = _imp("core.strategy_rules").garrison_cap(int(node2["level"]))
        await self.m._step_recovery(await self.m.get_world())
        node3 = await self.m.get_node(self.wid, node["id"])
        self.assertEqual(int(node3["npc_garrison"]["troops"]),
                         int(cap * 0.02))
        # 3) 民心 50 → 恢复减半
        node3["npc_garrison"] = {"troops": 0, "morale": 50}
        node3["morale"] = 50
        await self.m.save_node(node3)
        await self.m._step_recovery(await self.m.get_world())
        node4 = await self.m.get_node(self.wid, node["id"])
        self.assertEqual(int(node4["npc_garrison"]["troops"]),
                         int(int(cap * 0.02) * 0.5))
        # 4) 围城期间不增长
        node4["npc_garrison"] = {"troops": 0, "morale": 100}
        node4["siege_state"] = {"attacker": 1}
        await self.m.save_node(node4)
        await self.m._step_recovery(await self.m.get_world())
        node5 = await self.m.get_node(self.wid, node["id"])
        self.assertEqual(int(node5["npc_garrison"]["troops"]), 0)

    async def test_faction_buff_bonus_and_multiplier(self):
        """§2.1 `_faction_buff`：*_bonus 累加，其余相乘。"""
        p = await self._enter()
        fid = int(p["faction_id"])
        for buff, end in (({"effect": "attack_bonus", "value": 0.1},
                           _now_minus(-3600)),
                          ({"effect": "attack_bonus", "value": 0.05},
                           _now_minus(-3600)),
                          ({"effect": "grain_cost_mult", "value": 0.5},
                           _now_minus(-3600))):
            await self.m._execute(
                "INSERT INTO strategy_mobilizations (world_id, faction_id, "
                "issued_by, kind, buff_json, start_at, end_at, status) "
                "VALUES (?,?,?,?,?,?,?, 'active')",
                (self.wid, fid, "u1", "test", __import__("json").dumps(buff),
                 _now_minus(60), end))
        self.assertAlmostEqual(
            await self.m._faction_buff(fid, "attack_bonus"), 0.15, places=4)
        self.assertAlmostEqual(
            await self.m._faction_buff(fid, "grain_cost_mult"), 0.5, places=4)

    async def test_capture_node_protection_one_day(self):
        """新占领据点保护期：24 小时（1 天）。"""
        p = await self._enter()
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n["owner_faction"]) == 0 and n["type"] != "ruin")
        node["protect_until"] = 0
        world = await self.m.get_world()
        await self.m._capture_node(world, node, int(p["faction_id"]))
        self.assertLessEqual(
            abs(int(node["protect_until"]) - int(time.time()) - 86400), 10)

    async def test_guard_array_extends_shield(self):
        """§6.6 护山阵眼：护山盾时长 +10%/级。"""
        p = await self._enter()
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["owner_faction"] == 0 and n["type"] != "ruin")
        node["owner_faction"] = p["faction_id"]
        node["protect_until"] = 0
        node["buildings"] = [{"id": "guard_array", "level": 2}]
        await self.m.save_node(node)
        p = await self.m.get_player("u1", self.wid)
        p["merit"] = 5000
        await self.m.save_player(p)
        res = await self.m.buy_shield("u1", node["id"])
        self.assertTrue(res["ok"])
        node2 = await self.m.get_node(self.wid, node["id"])
        base = int(self.m.main_cfg()["shield"]["hours"]) * 3600
        self.assertGreaterEqual(int(node2["protect_until"]),
                                int(time.time()) + base + 4300)

    async def test_market_stall_reduces_tax(self):
        """§6.6 坊市摊：成交税 -1%/级（买入方返还）。"""
        p = await self._enter()
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if n["owner_faction"] == 0 and n["type"] != "ruin")
        node["owner_faction"] = p["faction_id"]
        node["buildings"] = [{"id": "market_stall", "level": 2}]
        await self.m.save_node(node)
        npc = await self.m._fetchone(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc'",
            (self.wid,))
        order_id = await self.m._execute(
            "INSERT INTO strategy_market_orders (world_id, node_id, "
            "seller_faction, seller_user, item_type, amount, unit_price, "
            "status, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.wid, node["id"], int(npc["id"]), "", "grain", 10, 100,
             "open", _now_minus(10), _now_minus(-3600)))
        p = await self.m.get_player("u1", self.wid)
        p["coin"] = 10_000
        await self.m.save_player(p)
        res = await self.m.handle_action(
            "u1", "market_buy", {"order_id": order_id, "amount": 10})
        self.assertTrue(res["ok"])
        self.assertEqual(int(res.get("tax_refund", 0)), 20)
        p2 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p2["coin"]), 10_000 - 1000 + 20)

    async def test_apply_aftermath_voluntary_retreat_no_double_penalty(self):
        """主动撤退已按 80% 结算，善后不得再套溃败 30% 扣兵/主将负伤。"""
        _p, gen, _off, home, _target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 1000)
        aid = int(r["army_id"])

        def _result(voluntary: bool) -> dict:
            return {
                "winner": "defender",
                "attackers": [{
                    "id": aid, "troops_after": 800, "morale": 0,
                    "routed": True, "voluntary_retreat": voluntary}],
                "defenders": [], "atk_losses": 200, "dfn_losses": 0,
                "peril_events": [],
            }

        army = await self.m.get_army(aid)
        await self.m._apply_battle_aftermath(
            self.world, home, [army], [], _result(True))
        kept = await self.m.get_army(aid)
        self.assertIsNotNone(kept)
        self.assertEqual(int(kept["troops"]), 800)
        leader = await self.m.get_char(gen["id"])
        self.assertEqual(int(leader.get("injury", 0)), 0)

    async def test_apply_aftermath_rout_still_penalizes(self):
        """非主动撤退（溃败）仍按 rout_troops_pct 扣兵，回归防线。"""
        _p, gen, _off, home, _target = await self._army_fixture()
        r = await self.m.create_army("u1", gen["id"], [], home["id"],
                                     "sword_shield", 1000)
        aid = int(r["army_id"])
        result = {
            "winner": "defender",
            "attackers": [{
                "id": aid, "troops_after": 800, "morale": 0,
                "routed": True, "voluntary_retreat": False}],
            "defenders": [], "atk_losses": 200, "dfn_losses": 0,
            "peril_events": [],
        }
        army = await self.m.get_army(aid)
        await self.m._apply_battle_aftermath(self.world, home, [army], [], result)
        kept = await self.m.get_army(aid)
        pct = float((self.m.main_cfg().get("battle") or {}).get(
            "rout_troops_pct", 0.3) or 0.0)
        expected = int(800 * (1 - min(0.95, pct)))
        self.assertEqual(int(kept["troops"]), expected)
        self.assertLess(expected, 800)

    async def test_pursuit_context_skips_routed_pursuer(self):
        """已溃逃/覆没的己方军团不能再追击并截获敌将。"""
        fleeing = {"id": 3, "faction_id": 11, "troops": 100, "characters": []}
        routed_pursuer = {"id": 1, "faction_id": 33, "troops": 500,
                          "_routed": True, "characters": []}
        destroyed = {"id": 4, "faction_id": 44, "troops": 500,
                     "_destroyed": True, "characters": []}
        healthy = {"id": 2, "faction_id": 22, "troops": 500, "characters": []}
        fid, traits, cav = await self.m._pursuit_context(
            fleeing, [fleeing], [routed_pursuer, destroyed, healthy])
        self.assertEqual(fid, 22)
        self.assertEqual(traits, [])
        self.assertFalse(cav)
        fid2, _t2, _c2 = await self.m._pursuit_context(
            fleeing, [fleeing], [routed_pursuer, destroyed])
        self.assertEqual(fid2, 0)

    async def test_aftermath_premarks_routed_defender_before_pursuit(self):
        """攻方溃逃时，同样溃逃的守方不得被当作追击方。

        主循环按「攻方 → 守方」顺序处理，若不在善后前按战果行预标记
        ``_routed``，结算攻方溃军时守方的溃逃标记还未写入，会被
        ``_pursuit_context`` 当成有效追击方（可截获敌将）。
        """
        def _army(aid: int, fid: int) -> dict:
            return {"id": aid, "faction_id": fid, "world_id": self.wid,
                    "troops": 100, "morale": 40, "veteran": 0,
                    "characters": [], "troop_type": "sword_shield",
                    "owner_user_id": "", "node_id": 0, "state": "battle"}

        def _result(dfn_routed: bool) -> dict:
            return {
                "winner": "defender",
                "attackers": [{"id": 9101, "troops_after": 50, "morale": 20,
                               "routed": True, "voluntary_retreat": False}],
                "defenders": [{"id": 9102, "troops_after": 50 if dfn_routed else 80,
                               "morale": 20, "routed": dfn_routed,
                               "voluntary_retreat": False}],
                "atk_losses": 50, "dfn_losses": 50, "peril_events": [],
            }

        async def _run(dfn_routed: bool) -> list:
            seen = []

            async def _fake_retreat(army, pursuer_faction=0,
                                    pursuer_traits=None, pursuer_cavalry=False):
                seen.append((int(army["id"]), int(pursuer_faction or 0)))

            with mock.patch.object(self.m, "_auto_retreat",
                                   side_effect=_fake_retreat):
                await self.m._apply_battle_aftermath(
                    self.world, {"id": 999999},
                    [_army(9101, 11)], [_army(9102, 22)], _result(dfn_routed))
            return seen

        # 双方都溃逃：各自撤退，均不得成为对方的追击方
        routed_seen = await _run(True)
        self.assertEqual({aid for aid, _f in routed_seen}, {9101, 9102})
        self.assertTrue(all(f == 0 for _aid, f in routed_seen), routed_seen)
        # 守方仍有效：正常追击（预标记不能误伤健康追兵）
        self.assertEqual(await _run(False), [(9101, 22)])

    async def test_battle_highlights(self):
        """§12.6 高光：单挑 / 夜袭 / 以少胜多。"""
        result = {"winner": "attacker", "night": True,
                  "duel_log": ["张三 单挑胜出"],
                  "attackers": [{"troops_before": 500}],
                  "defenders": [{"troops_before": 2000}]}
        tags = self.m._battle_highlights(result, {"events": []})
        self.assertTrue(any("单挑" in t for t in tags))
        self.assertTrue(any("夜袭" in t for t in tags))
        self.assertTrue(any("以少胜多" in t for t in tags))


def _now_minus(sec: int) -> int:
    return int(time.time()) - sec


if __name__ == "__main__":
    unittest.main(verbosity=2)
