# tests/test_strategy_p3_gaps.py
# 「仙域争霸」v4.1.0 缺口补全（Agent D：P3 / 俘虏 / 计略）
# 覆盖：情报挂单、交战禁市、关押位上限、离心劝降、濒死禁劝、押运到达/易主释放、
#       押运途中被营救截获、匪寨周期劫掠、会武分档报名、名将堂读取、谣言产出减益
import hashlib
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


class _ZeroRng:
    """恒定掷点：random()=0（判定必中）、randint=下界（时长为最短）。"""

    def random(self) -> float:
        return 0.0

    def randint(self, a, b):  # noqa: A003
        return int(a)

    def uniform(self, a, b):
        return float(a)

    def choice(self, seq):
        return list(seq)[0]

    def shuffle(self, seq):
        return None


class TestStrategyP3Gaps(unittest.IsolatedAsyncioTestCase):
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

    async def _enter(self, user_id: str = "u1", nickname: str = "甲",
                     referral: str = "") -> dict:
        return await self.m.enter_game(user_id, nickname, referral=referral,
                                       node_id=await self._free_node())

    async def _own(self, uid: str, node: dict) -> dict:
        p = await self.m.get_player(uid, self.wid)
        node["owner_faction"] = int(p["faction_id"])
        await self.m.save_node(node)
        await self.m._commit()
        return node

    async def _char_of(self, uid: str, role: str = "general"):
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == role:
                return c
        return None

    async def _recruit_role(self, uid: str, role: str):
        """确定性招募指定 role（注入酒馆候选，避免随机 flake）。"""
        rng = random.Random(int(hashlib.md5(
            f"{uid}|{role}|inject".encode("utf-8")).hexdigest()[:8], 16))
        c = self.R.generate_character(
            rng, rarity="fine", role=role,
            name_pools=self.m.cfg.get("names") or {})
        cand = {k: c[k] for k in ("name", "role", "rarity", "profession",
                                  "stats", "traits", "aptitude")}
        p = await self.m.get_player(uid, self.wid)
        tavern = p.get("tavern") or {}
        tavern["candidates"] = [cand]
        tavern["refresh_at"] = int(time.time()) + 4 * 3600
        p["tavern"] = tavern
        await self.m.save_player(p)
        await self.m._commit()
        p = await self.m.get_player(uid, self.wid)
        return await self.m.recruit(p, self.wid, 0)

    async def _new_char(self, uid: str, name: str,
                        faction_id: int = 0, loyalty: int = 70) -> dict:
        p = await self.m.get_player(uid, self.wid)
        c = {"world_id": self.wid,
             "faction_id": int(faction_id or p["faction_id"]),
             "owner_user_id": uid, "name": name, "role": "general",
             "rarity": "common", "profession": "sword",
             "stats": {"leadership": 60, "valor": 60, "wisdom": 60,
                       "charm": 60, "politics": 60, "luck": 60},
             "loyalty": loyalty, "status": "idle", "location": {}}
        await self.m.insert_char(c)
        await self.m._commit()
        return await self.m.get_char(c["id"])

    async def _set_char(self, cid: int, **kv):
        c = await self.m.get_char(int(cid))
        c.update(kv)
        await self.m.save_char(c)
        await self.m._commit()
        return await self.m.get_char(int(cid))

    async def _fetchall(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def _market_node(self) -> dict:
        nodes = await self.m.get_nodes(self.wid)
        return next(n for n in nodes if n["type"] in ("city", "market"))

    async def _fund_npc_treasuries(self, coin: int = 100_000) -> list[int]:
        """把各 NPC 国库补足，保证坊市收购报价一定生成。"""
        rows = await self._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc'",
            (self.wid,))
        fids = []
        for row in rows:
            fid = int(row["id"])
            f = await self.m.get_faction(fid)
            f["treasury"] = {"coin": int(coin), "grain": 0, "iron": 0,
                             "reserved_coin": 0}
            # 性格 trade_bias 会乘进报价概率（可能 <1）；测试要求「一定生成」，
            # 固定为 1 避免随机性格组合导致报价掷空。
            f["personality"] = dict(f.get("personality") or {})
            f["personality"]["trade_bias"] = 1.0
            await self.m.save_faction(f)
            fids.append(fid)
        await self.m._commit()
        return fids

    # ------------------------------------------------------------------
    # §5.5 互市
    # ------------------------------------------------------------------
    async def test_market_intel_list_and_buy(self):
        """情报品类可按基准价挂单并成交（存入 tavern.intel）。"""
        node = await self._market_node()
        p = await self.m.get_player("u1", self.wid)
        p["tavern"] = {"intel": 5}
        await self.m.save_player(p)
        await self.m._commit()
        r = await self.m.market_list("u1", node["id"], "intel", 5, 0)
        self.assertTrue(r["ok"])
        order = await self.m._fetchone(
            "SELECT * FROM strategy_market_orders WHERE id=?", (int(r["order_id"]),))
        self.assertEqual(order["item_type"], "intel")
        self.assertEqual(int(order["unit_price"]),
                         int(self.m.main_cfg()["market"]["base_prices"]["intel"]))
        p1 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int((p1["tavern"] or {}).get("intel", 0)), 0)
        r = await self.m.market_buy("u2", int(r["order_id"]), 2)
        self.assertTrue(r["ok"])
        p2 = await self.m.get_player("u2", self.wid)
        self.assertEqual(int((p2["tavern"] or {}).get("intel", 0)), 2)

    async def test_market_messages_use_chinese_names(self):
        """坊市提示/订单展示统一中文品类名（不暴露 intel/grain 等英文键）。"""
        node = await self._market_node()
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.market_list("u1", node["id"], "intel", 1, 0)
        self.assertIn("情报 不足", str(cm.exception))
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.market_list("u1", node["id"], "gem", 1, 1)
        self.assertIn("粮草/精铁/募将令/情报", str(cm.exception))
        await self._give("u1", grain=100)
        r = await self.m.market_list("u1", node["id"], "grain", 10, 2)
        self.assertIn("粮草", r["msg"])
        orders = await self.m.market_orders("u2")
        mine = next(o for o in orders if o["order_id"] == int(r["order_id"]))
        self.assertEqual(mine["item_name"], "粮草")

    async def test_market_war_blocks_trade(self):
        """交战势力之间不可互市（成交侧校验）。"""
        node = await self._market_node()
        await self._give("u1", grain=1_000)
        r = await self.m.market_list("u1", node["id"], "grain", 500, 2)
        oid = int(r["order_id"])
        f2 = await self.m.get_faction(int(self.p2["faction_id"]))
        self.m._set_relation(f2.setdefault("diplomacy", {}),
                             int(self.p1["faction_id"]), self.R.REL_WAR)
        await self.m.save_faction(f2)
        await self.m._commit()
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.market_buy("u2", oid, 10)
        self.assertIn("交战势力不可互市", str(cm.exception))
        # 停战/中立后可正常成交
        f2 = await self.m.get_faction(int(self.p2["faction_id"]))
        self.m._set_relation(f2.setdefault("diplomacy", {}),
                             int(self.p1["faction_id"]), self.R.REL_NEUTRAL)
        await self.m.save_faction(f2)
        await self.m._commit()
        self.assertTrue((await self.m.market_buy("u2", oid, 10))["ok"])

    async def test_market_list_rejects_non_economic_node(self):
        """挂单节点收紧：只能是仙城/坊市。"""
        nodes = await self.m.get_nodes(self.wid)
        ruin = next(n for n in nodes if n["type"] == "ruin")
        await self._give("u1", grain=1_000)
        with self.assertRaises(self.StrategyError):
            await self.m.market_list("u1", ruin["id"], "grain", 10, 2)

    async def test_market_player_price_band(self):
        """玩家挂单价须落在市场价 ±band 内（默认 ±50%）。"""
        node = await self._market_node()
        await self._give("u1", grain=1_000, iron=1_000)
        mcfg = self.m.main_cfg()["market"]
        base = int(mcfg["base_prices"]["iron"])
        band = float(mcfg.get("player_price_band_pct", 0.5))
        lo, hi, _ = self.R.market_price_band(base, band)
        self.assertGreater(lo, 1, "iron 的下界应大于 1，便于验证低价拒绝")
        # 区间外：拒绝
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.market_list("u1", node["id"], "iron", 10, hi + 1)
        self.assertIn("定价须在市场价", str(cm.exception))
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.market_list("u1", node["id"], "iron", 10, lo - 1)
        self.assertIn("定价须在市场价", str(cm.exception))
        # 区间边界：允许
        r1 = await self.m.market_list("u1", node["id"], "iron", 10, lo)
        r2 = await self.m.market_list("u1", node["id"], "iron", 10, hi)
        self.assertTrue(r1["ok"] and r2["ok"])
        rows = await self._fetchall(
            "SELECT unit_price FROM strategy_market_orders WHERE id IN (?,?)",
            (int(r1["order_id"]), int(r2["order_id"])))
        self.assertEqual(sorted(int(x["unit_price"]) for x in rows), [lo, hi])
        # 未传价（0）→ 自动取基准价
        r3 = await self.m.market_list("u1", node["id"], "grain", 10, 0)
        row = await self.m._fetchone(
            "SELECT unit_price FROM strategy_market_orders WHERE id=?",
            (int(r3["order_id"]),))
        self.assertEqual(int(row["unit_price"]),
                         int(mcfg["base_prices"]["grain"]))

    async def test_market_buy_updates_remaining_amount(self):
        """部分买入后挂单可购买量立即扣减，买完转 filled。"""
        node = await self._market_node()
        await self._give("u1", grain=1_000)
        await self._give("u2", coin=100_000)
        r = await self.m.market_list("u1", node["id"], "grain", 500, 2)
        oid = int(r["order_id"])
        await self.m.market_buy("u2", oid, 200)
        order = await self.m._fetchone(
            "SELECT amount, status FROM strategy_market_orders WHERE id=?", (oid,))
        self.assertEqual(int(order["amount"]), 300)
        self.assertEqual(order["status"], "open")
        await self.m.market_buy("u2", oid, 300)
        order = await self.m._fetchone(
            "SELECT amount, status FROM strategy_market_orders WHERE id=?", (oid,))
        self.assertEqual(int(order["amount"]), 0)
        self.assertEqual(order["status"], "filled")

    async def test_market_npc_offer_accept_and_expire(self):
        """NPC 收购报价：接受后扣减挂单量并按税后入账；过期报价自动失效。"""
        node = await self._market_node()
        await self._give("u1", grain=1_000)
        p = await self.m.get_player("u1", self.wid)
        coin0 = int(p["coin"])
        r = await self.m.market_list("u1", node["id"], "grain", 500, 3)
        oid = int(r["order_id"])
        mcfg = self.m.main_cfg()["market"]
        mcfg["npc_offer_interval_ticks"] = 1
        mcfg["npc_offer_chance"] = 1.0
        mcfg["npc_offer_cooldown_hours"] = 0
        await self._fund_npc_treasuries()
        world = await self.m.get_world()
        world["tick_no"] = 1
        await self.m.save_world(world)
        await self.m._commit()
        await self.m._step_market_offers(world)
        offers = await self.m.market_offers("u1")
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(int(offer["order_id"]), oid)
        self.assertGreaterEqual(int(offer["unit_price"]), 1)
        self.assertGreaterEqual(int(offer["payout"]), 0)
        res = await self.m.market_offer_accept("u1", int(offer["offer_id"]))
        self.assertTrue(res["ok"])
        p2 = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p2["coin"]), coin0 + int(res["payout"]))
        order = await self.m._fetchone(
            "SELECT amount, status FROM strategy_market_orders WHERE id=?", (oid,))
        self.assertEqual(int(order["amount"]), 500 - int(res["amount"]))
        self.assertEqual(order["status"], "filled" if res["amount"] == 500 else "open")
        self.assertFalse(await self.m.market_offers("u1"))
        # 再挂一单，制造报价后让挂单过期 → 报价与挂单一起失效
        await self._give("u1", grain=100)
        r2 = await self.m.market_list("u1", node["id"], "grain", 100, 2)
        oid2 = int(r2["order_id"])
        world["tick_no"] = 2
        await self.m.save_world(world)
        await self.m._commit()
        await self.m._step_market_offers(world)
        offers2 = await self.m.market_offers("u1")
        self.assertTrue(any(int(f["order_id"]) == oid2 for f in offers2))
        await self.m._execute(
            "UPDATE strategy_market_orders SET expires_at=? WHERE id=?",
            (_now() - 1, oid2))
        await self.m._commit()
        await self.m._step_market(world)
        await self.m._step_market_offers(world)
        self.assertFalse(any(int(f["order_id"]) == oid2
                             for f in await self.m.market_offers("u1")))
        row = await self.m._fetchone(
            "SELECT status FROM strategy_market_offers WHERE order_id=? "
            "ORDER BY id DESC LIMIT 1", (oid2,))
        self.assertEqual(row["status"], "expired")
        # 过期挂单已退货
        p3 = await self.m.get_player("u1", self.wid)
        self.assertGreaterEqual(int(p3["grain"]), 100)

    async def test_market_offer_decline_keeps_order(self):
        """拒绝报价后挂单保留，报价不会再次生效。"""
        node = await self._market_node()
        await self._give("u1", grain=100)
        r = await self.m.market_list("u1", node["id"], "grain", 100, 2)
        mcfg = self.m.main_cfg()["market"]
        mcfg["npc_offer_interval_ticks"] = 1
        mcfg["npc_offer_chance"] = 1.0
        mcfg["npc_offer_cooldown_hours"] = 0
        await self._fund_npc_treasuries()
        world = await self.m.get_world()
        world["tick_no"] = 1
        await self.m.save_world(world)
        await self.m._commit()
        await self.m._step_market_offers(world)
        offer = (await self.m.market_offers("u1"))[0]
        res = await self.m.handle_action(
            "u1", "market_offer_decline", {"offer_id": int(offer["offer_id"])})
        self.assertTrue(res["ok"])
        self.assertFalse(await self.m.market_offers("u1"))
        order = await self.m._fetchone(
            "SELECT amount, status FROM strategy_market_orders WHERE id=?",
            (int(r["order_id"]),))
        self.assertEqual(order["status"], "open")
        self.assertEqual(int(order["amount"]), 100)
        with self.assertRaises(self.StrategyError):
            await self.m.market_offer_accept("u1", int(offer["offer_id"]))

    # ------------------------------------------------------------------
    # §7.9 俘虏
    # ------------------------------------------------------------------
    async def test_jail_slots_capacity_evicts(self):
        """关押位满员：第 6 名角色被就地驱逐（idle、忠诚 -5）。"""
        node = (await self.m.get_nodes(self.wid))[0]
        fid1 = int(self.p1["faction_id"])
        chars = [await self._new_char("u2", f"俘虏{i}") for i in range(6)]
        captured = 0
        for c in chars[:5]:
            ok = await self.m._capture_char(
                await self.m.get_char(c["id"]), fid1, node["id"])
            self.assertTrue(ok)
            captured += 1
        await self.m._commit()
        row = await self.m._fetchone(
            "SELECT COUNT(*) AS n FROM strategy_characters WHERE world_id=? "
            "AND status='captive' AND imprisoned_node_id=?",
            (self.wid, int(node["id"])))
        self.assertEqual(int(row["n"]), 5)
        # 第 6 名：关押位已满 → 驱逐
        sixth = await self.m.get_char(chars[5]["id"])
        ok = await self.m._capture_char(sixth, fid1, node["id"])
        self.assertFalse(ok)
        sixth = await self.m.get_char(chars[5]["id"])
        self.assertEqual(sixth["status"], "idle")
        self.assertEqual(int(sixth["captor_faction"]), 0)
        self.assertEqual(int(sixth["loyalty"]), 65)
        self.assertEqual(captured, 5)

    async def test_persuade_defect_any_faction(self):
        """离心（忠诚 ≤ defect_loyalty）：非关押方也可 80% 劝降并扣声望。"""
        node = (await self.m.get_nodes(self.wid))[0]
        gen2 = await self._char_of("u2", "general")
        await self.m._capture_char(gen2, int(self.p1["faction_id"]), node["id"])
        await self.m._commit()
        await self._set_char(gen2["id"], loyalty=0)
        rep0 = int((await self.m.get_faction(int(self.p3["faction_id"])))["reputation"])
        with mock.patch.object(self.m, "_rng", lambda *a, **k: _ZeroRng()):
            r = await self.m.persuade_captive("u3", gen2["id"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"], "success")
        c = await self.m.get_char(gen2["id"])
        self.assertEqual(int(c["faction_id"]), int(self.p3["faction_id"]))
        self.assertEqual(c["owner_user_id"], "u3")
        self.assertEqual(c["status"], "idle")
        self.assertEqual(int(c["loyalty"]), 40)
        rep1 = int((await self.m.get_faction(int(self.p3["faction_id"])))["reputation"])
        self.assertEqual(rep1 - rep0,
                         int(self.m.main_cfg()["captive"]["defect_rep_cost"]))
        rows = await self._fetchall(
            "SELECT detail_json FROM strategy_audit WHERE user_id='u3' "
            "AND action='persuade_captive'")
        self.assertTrue(rows)
        detail = json.loads(rows[-1]["detail_json"])
        self.assertEqual(detail["mode"], "defect")

    async def test_persuade_dying_blocked(self):
        """濒死俘虏不可劝降（须先救治）。"""
        node = (await self.m.get_nodes(self.wid))[0]
        gen2 = await self._char_of("u2", "general")
        await self.m._capture_char(gen2, int(self.p1["faction_id"]), node["id"])
        await self.m._commit()
        await self._set_char(gen2["id"], injury=75)  # >= INJURY_DYING
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.persuade_captive("u1", gen2["id"])
        self.assertIn("濒死", str(cm.exception))

    async def test_transfer_captive_arrive_and_owner_change(self):
        """押运：抵达转关押；目的地中途易主则自动释放。"""
        nodes = await self.m.get_nodes(self.wid)
        node_a = nodes[0]
        node_b = nodes[1]
        await self._own("u1", node_b)
        fid1 = int(self.p1["faction_id"])
        c1 = await self._new_char("u2", "押送甲")
        await self.m._capture_char(c1, fid1, node_a["id"])
        await self.m._commit()
        c1 = await self.m.get_char(c1["id"])
        r = await self.m.transfer_captive("u1", c1["id"], node_b["id"])
        self.assertTrue(r["ok"])
        c1 = await self.m.get_char(c1["id"])
        self.assertEqual(c1["status"], "transfer")
        rows = await self._fetchall(
            "SELECT * FROM strategy_captive_transfers WHERE char_id=? "
            "AND status='marching'", (c1["id"],))
        self.assertEqual(len(rows), 1)
        await self.m._execute(
            "UPDATE strategy_captive_transfers SET arrive_at=? WHERE id=?",
            (_now() - 5, int(rows[0]["id"])))
        await self.m._commit()
        world = await self.m.get_world()
        self.assertEqual(await self.m._step_captive_transfers(world), 1)
        c1 = await self.m.get_char(c1["id"])
        self.assertEqual(c1["status"], "captive")
        self.assertEqual(int(c1["imprisoned_node_id"]), int(node_b["id"]))
        self.assertEqual(int(c1["captor_faction"]), fid1)
        # 第二例：目的地易主 → 自动释放
        c2 = await self._new_char("u2", "押送乙")
        await self.m._capture_char(c2, fid1, node_a["id"])
        await self.m._commit()
        c2 = await self.m.get_char(c2["id"])
        r = await self.m.transfer_captive("u1", c2["id"], node_b["id"])
        await self.m._execute(
            "UPDATE strategy_captive_transfers SET arrive_at=? WHERE char_id=? "
            "AND status='marching'", (_now() - 5, c2["id"]))
        node_b = await self.m.get_node(self.wid, node_b["id"])
        node_b["owner_faction"] = 0
        await self.m.save_node(node_b)
        await self.m._commit()
        world = await self.m.get_world()
        await self.m._step_captive_transfers(world)
        c2 = await self.m.get_char(c2["id"])
        self.assertEqual(c2["status"], "idle")
        self.assertEqual(int(c2["faction_id"]), int(self.p2["faction_id"]))
        rows = await self._fetchall(
            "SELECT status FROM strategy_captive_transfers WHERE char_id=?",
            (c2["id"],))
        self.assertEqual(rows[0]["status"], "released")

    async def test_rescue_intercepts_transfer(self):
        """营救计略可截获押运途中的俘虏。"""
        nodes = await self.m.get_nodes(self.wid)
        node_a, node_b = nodes[0], nodes[1]
        await self._own("u1", node_b)
        fid1 = int(self.p1["faction_id"])
        captive = await self._char_of("u2", "general")
        await self.m._capture_char(captive, fid1, node_a["id"])
        await self.m._commit()
        r = await self.m.transfer_captive("u1", captive["id"], node_b["id"])
        self.assertTrue(r["ok"])
        strategist = await self._recruit_role("u2", self.R.ROLE_STRATEGIST)
        await self._give("u2", coin=5_000)
        with mock.patch.object(self.m, "_rng", lambda *a, **k: _ZeroRng()):
            d = await self.m.dispatch_plot(
                "u2", strategist["id"], "rescue", captive["id"])
        self.assertTrue(d["ok"])
        task = (await self.m.get_tasks(self.wid))[-1]
        self.assertEqual(task["task_type"], "plot_rescue")
        await self.m._execute("UPDATE strategy_tasks SET end_at=? WHERE id=?",
                              (_now() - 10, task["id"]))
        await self.m._commit()
        with mock.patch.object(self.m, "_rng", lambda *a, **k: _ZeroRng()):
            world = await self.m.get_world()
            await self.m._step_settle_tasks(world)
        c = await self.m.get_char(captive["id"])
        self.assertEqual(c["status"], "idle")
        self.assertEqual(c["owner_user_id"], "u2")
        rows = await self._fetchall(
            "SELECT status FROM strategy_captive_transfers WHERE char_id=? "
            "AND status='marching'", (captive["id"],))
        self.assertFalse(rows)

    # ------------------------------------------------------------------
    # §9.1 匪寨周期劫掠
    # ------------------------------------------------------------------
    async def test_bandit_periodic_raid(self):
        """匪寨按 raid_interval_ticks 掠夺相邻玩家据点：库存 -30%、民心 -5。"""
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        lair, victim = None, None
        for n in nodes.values():
            for adj in n.get("adj") or []:
                t = nodes.get(int(adj))
                if t and t["type"] != "ruin":
                    lair, victim = n, t
                    break
            if lair:
                break
        self.assertIsNotNone(lair)
        await self._own("u1", victim)
        interval = int(self.m.main_cfg()["bandit"]["raid_interval_ticks"])
        world = await self.m.get_world()
        tick_no = int(world.get("tick_no", 0))
        lair = await self.m.get_node(self.wid, lair["id"])
        lair["type"] = "ruin"
        lair["owner_faction"] = 0
        lair["adj"] = list(lair.get("adj") or [])
        if int(victim["id"]) not in [int(x) for x in lair["adj"]]:
            lair["adj"].append(int(victim["id"]))
        lair["lair"] = {"bandit": True, "last_raid_tick": tick_no - interval}
        await self.m.save_node(lair)
        victim = await self.m.get_node(self.wid, victim["id"])
        victim["stock"] = {"grain": 1_000, "coin": 100}
        victim["morale"] = 80
        await self.m.save_node(victim)
        await self.m._commit()
        world = await self.m.get_world()
        raided = await self.m._step_bandit_raids(world)
        self.assertGreaterEqual(raided, 1)
        victim = await self.m.get_node(self.wid, victim["id"])
        self.assertEqual(int(victim["stock"]["grain"]), 700)
        self.assertEqual(int(victim["stock"]["coin"]), 70)
        self.assertEqual(int(victim["morale"]), 75)
        lair = await self.m.get_node(self.wid, lair["id"])
        self.assertEqual(int(lair["lair"]["last_raid_tick"]), tick_no)
        # 同一 tick 内不重复劫掠
        world = await self.m.get_world()
        self.assertEqual(await self.m._step_bandit_raids(world), 0)

    # ------------------------------------------------------------------
    # §11 会武 / 名将堂
    # ------------------------------------------------------------------
    async def test_arena_register_bracket(self):
        """会武报名按战功分档，不可跨档，扣报名费。"""
        p = await self.m.get_player("u1", self.wid)
        p["merit"] = 100  # novice
        await self.m.save_player(p)
        await self.m._commit()
        coin0 = int((await self.m.get_player("u1", self.wid))["coin"])
        with self.assertRaises(self.StrategyError):
            await self.m.arena_register("u1", "master")
        r = await self.m.arena_register("u1", "novice")
        self.assertTrue(r["ok"])
        self.assertEqual(r["bracket"], "novice")
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(coin0 - int(p["coin"]),
                         int(self.m.main_cfg()["duel"]["arena_stake_min"]))
        world = await self.m.get_world()
        arena = (world.get("data") or {}).get("duel_arena") or {}
        signups = [v for v in arena.values() if v.get("state") == "signup"]
        self.assertTrue(any(v.get("challenger") == "u1" for v in signups))
        self.assertGreater(r["reward_mult"], 0)
        with self.assertRaises(self.StrategyError):
            await self.m.arena_register("u1", "novice")

    async def test_get_memorials(self):
        """名将堂/英烈祠读取：分 kind 归档、按赛季倒序、最多 20 条。"""
        for i in range(3):
            await self.m._execute(
                "INSERT INTO strategy_memorials (season_no, world_id, kind, "
                "char_name, user_id, faction_name, snapshot_json, created_at) "
                "VALUES (?,?,'fame',?,'','烈祠盟',?,?)",
                (1, self.wid, f"英烈{i}", json.dumps({"rarity": "epic",
                                                   "level": 30}), _now()))
        await self.m._execute(
            "INSERT INTO strategy_memorials (season_no, world_id, kind, "
            "char_name, user_id, faction_name, snapshot_json, created_at) "
            "VALUES (?,?,'hall_of_fame','','u1','名将盟',?,?)",
            (1, self.wid, json.dumps({"merit_season": 888}), _now()))
        await self.m._commit()
        out = await self.m.get_memorials(self.wid)
        self.assertIn("hall", out)
        self.assertIn("shrine", out)
        self.assertEqual(len(out["shrine"]), 3)
        self.assertTrue(any(x["name"] == "英烈0" for x in out["shrine"]))
        self.assertTrue(out["hall"])
        self.assertEqual(int(out["hall"][0]["merit"]), 888)
        self.assertGreaterEqual(int(out["season_no"]), 1)
        self.assertLessEqual(len(out["hall"]), 20)
        self.assertLessEqual(len(out["shrine"]), 20)

    # ------------------------------------------------------------------
    # §6.3 计略：谣言产出减益
    # ------------------------------------------------------------------
    async def test_plot_output_mult(self):
        """谣言减益以 plot_output_mult 暴露（0.8），过期自动失效。"""
        node = {"output": {"plot_debuff": {"mult": 0.8,
                                          "until": _now() + 600}}}
        self.assertAlmostEqual(self.m.plot_output_mult(node), 0.8, places=3)
        expired = {"output": {"plot_debuff": {"mult": 0.8,
                                             "until": _now() - 1}}}
        self.assertEqual(self.m.plot_output_mult(expired), 1.0)
        self.assertEqual(self.m.plot_output_mult({"output": {}}), 1.0)
        self.assertEqual(self.m.plot_output_mult(None), 1.0)


if __name__ == "__main__":
    unittest.main()