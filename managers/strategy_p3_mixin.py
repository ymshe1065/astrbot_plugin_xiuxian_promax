# -*- coding: utf-8 -*-
"""「仙域争霸」P3 Mixin（设计方案 §5.5/§5.6/§6.5/§11/§17）。

覆盖：成就系统、坊市互市（玩家挂单 + NPC 做市）、匪寨、自动商路、
运粮队与劫粮、赛季阶段/合纵/结算/英烈祠名将堂/老兵结转、
追赶礼包、P3 事件效果（流将/劫掠/会武）。

临时态（匪寨/商路缓存/粮队/会武/结转已领）统一存 strategy_worlds.data_json；
挂单存 strategy_market_orders；赛季存档 strategy_season_results / strategy_memorials。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from ..core import strategy_rules as R
from .strategy_errors import StrategyError

logger = logging.getLogger("astrbot")


def _now() -> int:
    return int(time.time())


#: 计数型成就阈值（其余为一次性标记型 need=None）
ACHIEVEMENT_NEEDS = {
    "conquest_bandit_10": 10,
    "civil_farm_100k": 100_000,
    "civil_build_5": 5,
}

#: 流将招募价（按稀有度）
WANDERER_PRICE = {"common": 1000, "fine": 2000, "rare": 4000,
                  "epic": 8000, "legend": 15000}


class StrategyP3Mixin:
    """P3 互市 / 匪寨 / 商路 / 粮队 / 赛季 / 成就（§5.5/§5.6/§6.5/§11）。"""

    # ------------------------------------------------------------------
    # 成就（§6.5）
    # ------------------------------------------------------------------
    async def _award_achievement(self, user_id: str, key: str, count: int = 1) -> bool:
        """成就进度 +1（或标记），达阈值解锁并发放战功。返回是否新解锁。"""
        if not user_id:
            return False
        world = await self.get_world()
        if not world:
            return False
        p = await self.get_player(user_id, world["id"])
        if not p:
            return False
        ach = p.get("achievements") or {}
        prog = ach.setdefault("progress", {})
        unlocked = ach.setdefault("unlocked", {})
        if key in unlocked:
            return False
        prog[key] = int(prog.get(key, 0)) + max(0, int(count))
        need = ACHIEVEMENT_NEEDS.get(key)  # None → 一次性
        if not R.achievement_unlocked(prog, key, need):
            p["achievements"] = ach
            await self.save_player(p)
            await self._commit()
            return False
        unlocked[key] = _now()
        merit = int((self.main_cfg().get("achievements") or {}).get(
            key, {}).get("merit", 0))
        p["merit"] = int(p.get("merit", 0)) + merit
        p["merit_season"] = int(p.get("merit_season", 0)) + merit
        p["achievements"] = ach
        await self.save_player(p)
        await self._commit()
        name = (self.main_cfg().get("achievements") or {}).get(key, {}).get(
            "name", key)
        logger.info(f"[strategy] 玩家 {user_id} 解锁成就「{name}」+{merit} 战功")
        return True

    async def get_achievements_view(self, user_id: str) -> dict:
        world = await self.get_world()
        if not world:
            return {"unlocked": {}, "progress": {}}
        p = await self.get_player(user_id, world["id"])
        ach = (p or {}).get("achievements") or {}
        return {"unlocked": ach.get("unlocked") or {},
                "progress": ach.get("progress") or {}}

    # ------------------------------------------------------------------
    # 坊市互市（§5.5）
    # ------------------------------------------------------------------
    async def market_list(self, user_id: str, node_id: int, item_type: str,
                          amount: int, unit_price: int) -> dict:
        world, p = await self._require_player(user_id)
        mcfg = self.main_cfg().get("market") or {}
        item_type = str(item_type)
        amount, unit_price = int(amount), int(unit_price)
        if item_type not in ("grain", "iron", "recruit_token"):
            raise StrategyError("仅可挂售 粮草/精铁/招募令")
        if amount <= 0 or unit_price <= 0:
            raise StrategyError("数量与单价须为正")
        if int(p.get(item_type, 0)) < amount:
            raise StrategyError(f"{item_type} 不足（现有 {int(p.get(item_type, 0))}）")
        node = await self.get_node(world["id"], int(node_id))
        if not node or node["type"] not in ("market", "city"):
            raise StrategyError("只能在坊市/仙城挂单")
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM strategy_market_orders WHERE world_id=? "
            "AND seller_user=? AND status='open'", (world["id"], user_id))
        limit = int(mcfg.get("max_orders_per_user", 20))
        if int((row or {}).get("n", 0)) >= limit:
            raise StrategyError(f"挂单数已达上限（{limit}）")
        p[item_type] = int(p[item_type]) - amount
        await self.save_player(p)
        oid = await self._execute("""
            INSERT INTO strategy_market_orders (world_id, node_id, seller_faction,
                seller_user, item_type, amount, unit_price, status, created_at, expires_at)
            VALUES (?,?,?,?,?,?,?,'open',?,?)
        """, (
            world["id"], int(node["id"]), int(p["faction_id"]), user_id,
            item_type, amount, unit_price,
            _now(), _now() + int(mcfg.get("order_expire_hours", 48)) * 3600))
        await self._commit()
        return {"ok": True, "order_id": oid,
                "msg": f"已在「{node['name']}」挂售 {item_type} ×{amount}"
                       f"（单价 {unit_price}，48h 有效）"}

    async def market_buy(self, user_id: str, order_id: int, amount: int = 0) -> dict:
        # C2：互市撮合全程互斥（防并发双买/买撤并发 TOCTOU 双花）；
        # 落库改用条件 UPDATE + 行数校验，双保险
        async with self._market_lock:
            return await self._market_buy(user_id, order_id, amount)

    async def _market_buy(self, user_id: str, order_id: int, amount: int = 0) -> dict:
        world, p = await self._require_player(user_id)
        mcfg = self.main_cfg().get("market") or {}
        row = await self._fetchone(
            "SELECT * FROM strategy_market_orders WHERE id=? AND world_id=?",
            (int(order_id), world["id"]))
        if not row or row["status"] != "open":
            raise StrategyError("订单不存在或已成交/下架")
        if int(row["expires_at"]) < _now():
            raise StrategyError("订单已过期")
        if row["seller_user"] == user_id:
            raise StrategyError("不能购买自己的挂单")
        if mcfg.get("cross_faction_market_only", True) and row["seller_user"] and \
                int(row["seller_faction"]) == int(p["faction_id"]):
            raise StrategyError("互市仅限跨势力交易（同势力请自行转移）")
        avail = int(row["amount"])
        want = avail if int(amount) <= 0 else min(int(amount), avail)
        if want <= 0:
            raise StrategyError("数量无效")
        cost = want * int(row["unit_price"])
        if int(p.get("coin", 0)) < cost:
            raise StrategyError(f"铜钱不足：需 {cost}")
        p["coin"] -= cost
        tax = R.market_tax(cost, float(mcfg.get("tax_pct", 0.05)))
        payout = cost - tax
        item = row["item_type"]
        if item == "intel":
            tavern = p.get("tavern") or {}
            tavern["intel"] = int(tavern.get("intel", 0)) + want
            p["tavern"] = tavern
        else:
            p[item] = int(p.get(item, 0)) + want
        await self.save_player(p)
        # 卖方入账
        if row["seller_user"]:
            sp = await self.get_player(row["seller_user"], world["id"])
            if sp:
                sp["coin"] = int(sp.get("coin", 0)) + payout
                await self.save_player(sp)
        else:
            sf = await self.get_faction(int(row["seller_faction"]))
            if sf:
                treasury = sf.get("treasury") or {}
                treasury["coin"] = int(treasury.get("coin", 0)) + payout
                sf["treasury"] = treasury
                await self.save_faction(sf)
        rest = avail - want
        if rest <= 0:
            n = await self._exec_count(
                "UPDATE strategy_market_orders SET status='filled', amount=0, "
                "buyer_user=? WHERE id=? AND status='open' AND amount=?",
                (user_id, int(order_id), avail))
        else:
            n = await self._exec_count(
                "UPDATE strategy_market_orders SET amount=? WHERE id=? "
                "AND status='open' AND amount=?", (rest, int(order_id), avail))
        if n != 1:
            # 订单已被并发修改（撤单/他人买入）：整笔回滚，买卖双方不损
            await self.db.conn.rollback()
            raise StrategyError("订单已被并发修改，请重试")
        await self._commit()
        return {"ok": True,
                "msg": f"购入 {item} ×{want}，花费 {cost} 铜钱（含税 {tax}）"}

    async def market_cancel(self, user_id: str, order_id: int) -> dict:
        # C2：与买入同锁，撤单退货与撮合互斥
        async with self._market_lock:
            return await self._market_cancel(user_id, order_id)

    async def _market_cancel(self, user_id: str, order_id: int) -> dict:
        world, p = await self._require_player(user_id)
        row = await self._fetchone(
            "SELECT * FROM strategy_market_orders WHERE id=? AND world_id=? "
            "AND seller_user=?", (int(order_id), world["id"], user_id))
        if not row or row["status"] != "open":
            raise StrategyError("订单不存在或已结束")
        item = row["item_type"]
        if item in ("grain", "iron", "recruit_token"):
            p[item] = int(p.get(item, 0)) + int(row["amount"])
            await self.save_player(p)
        n = await self._exec_count(
            "UPDATE strategy_market_orders SET status='cancelled', amount=0 "
            "WHERE id=? AND status='open' AND amount=?",
            (int(order_id), int(row["amount"])))
        if n != 1:
            # 已被并发买入成交：回滚退货，防「货款双得」
            await self.db.conn.rollback()
            raise StrategyError("订单已被并发修改，无法撤单")
        await self._commit()
        return {"ok": True, "msg": f"已撤单，退回 {item} ×{int(row['amount'])}"}

    async def market_orders(self, user_id: str, node_id: int = 0) -> list[dict]:
        world, p = await self._require_player(user_id)
        sql = ("SELECT * FROM strategy_market_orders WHERE world_id=? AND status='open' "
               "AND expires_at>?")
        params: list = [world["id"], _now()]
        if int(node_id):
            sql += " AND node_id=?"
            params.append(int(node_id))
        sql += " ORDER BY unit_price ASC LIMIT 200"
        rows = await self._fetchall(sql, tuple(params))
        out = []
        for r in rows:
            out.append({"order_id": r["id"], "node_id": r["node_id"],
                        "item_type": r["item_type"], "amount": r["amount"],
                        "unit_price": r["unit_price"],
                        "seller_faction": r["seller_faction"],
                        "seller_user": r["seller_user"],
                        "npc": not r["seller_user"],
                        "mine": r["seller_user"] == user_id,
                        "expires_at": r["expires_at"]})
        return out

    async def _step_market(self, world: dict):
        """tick：过期订单下架并退货。"""
        rows = await self._fetchall(
            "SELECT * FROM strategy_market_orders WHERE world_id=? AND status='open' "
            "AND expires_at<?", (world["id"], _now()))
        for r in rows:
            if r["seller_user"] and r["item_type"] in ("grain", "iron", "recruit_token"):
                sp = await self.get_player(r["seller_user"], world["id"])
                if sp:
                    sp[r["item_type"]] = int(sp.get(r["item_type"], 0)) + int(r["amount"])
                    await self.save_player(sp)
            await self._execute(
                "UPDATE strategy_market_orders SET status='expired', amount=0 WHERE id=?",
                (int(r["id"]),))
        if rows:
            await self._commit()

    async def _step_market_maker(self, world: dict):
        """NPC 做市：每 npc_maker_interval_ticks 在坊市挂卖单（价格锚点）。"""
        mcfg = self.main_cfg().get("market") or {}
        interval = max(1, int(mcfg.get("npc_maker_interval_ticks", 8)))
        if int(world.get("tick_no", 0)) % interval != 0:
            return
        rng = self._rng("maker", world["id"], world["tick_no"])
        spread = float(mcfg.get("npc_maker_spread_pct", 0.15))
        base_prices = mcfg.get("base_prices") or {}
        amount_range = {"grain": (500, 2000), "iron": (200, 800),
                        "recruit_token": (1, 3), "intel": (1, 2)}
        nodes = [n for n in await self.get_nodes(world["id"])
                 if n["type"] == "market" and int(n["owner_faction"]) > 0]
        made = 0
        for node in nodes:
            cnt = await self._fetchone(
                "SELECT COUNT(*) AS n FROM strategy_market_orders WHERE node_id=? "
                "AND status='open' AND seller_user=''", (node["id"],))
            if int((cnt or {}).get("n", 0)) >= 6:
                continue
            item = rng.choice(list(base_prices.keys()) or ["grain"])
            lo, hi = amount_range.get(item, (100, 500))
            amount = rng.randint(lo, hi)
            price = R.npc_maker_price(float(base_prices.get(item, 2)), rng,
                                      spread, is_sell=True)
            await self._execute("""
                INSERT INTO strategy_market_orders (world_id, node_id, seller_faction,
                    seller_user, item_type, amount, unit_price, status, created_at,
                    expires_at)
                VALUES (?,?,?,'',?,?,?,'open',?,?)
            """, (world["id"], int(node["id"]), int(node["owner_faction"]),
                  item, amount, price, _now(),
                  _now() + int(mcfg.get("order_expire_hours", 48)) * 3600))
            made += 1
        if made:
            await self._commit()

    # ------------------------------------------------------------------
    # 匪寨（§5.4）
    # ------------------------------------------------------------------
    async def _step_bandits(self, world: dict):
        bcfg = self.main_cfg().get("bandit") or {}
        data = world.get("data") or {}
        bandits = data.get("bandits") or {}
        dirty = False
        ownerless = self.main_cfg().get("ownerless_garrison") or {}
        # 到期清理
        for nid in [k for k, v in bandits.items()
                    if _now() > int(v.get("until", 0))]:
            bandits.pop(nid)
            node = await self.get_node(world["id"], int(nid))
            if node and int(node["owner_faction"]) == 0:
                node["npc_garrison"] = {
                    "troops": int(ownerless.get(node["type"], 800)), "morale": 80}
                await self.save_node(node)
            dirty = True
        # 每周生成 2~4 处（无主据点）
        week = (_now() - int(world.get("started_at", 0))) // (7 * 86400)
        if data.get("bandit_week") != week:
            data["bandit_week"] = week
            dirty = True
            lo, hi = bcfg.get("per_week") or [2, 4]
            rng = self._rng("bandits", world["id"], week)
            cands = [n for n in await self.get_nodes(world["id"])
                     if int(n["owner_faction"]) == 0 and str(n["id"]) not in bandits]
            rng.shuffle(cands)
            tlo, thi = bcfg.get("garrison_troops") or [800, 1500]
            for node in cands[:rng.randint(int(lo), int(hi))]:
                troops = rng.randint(int(tlo), int(thi))
                node["npc_garrison"] = {"troops": troops, "morale": 90}
                await self.save_node(node)
                bandits[str(node["id"])] = {
                    "spawned_at": _now(), "origin": "weekly", "troops": troops,
                    "until": _now() + int(bcfg.get("exist_hours", 72)) * 3600}
        if dirty:
            data["bandits"] = bandits
            world["data"] = data
            await self.save_world(world)
            await self._commit()

    async def get_bandits_view(self, world: dict) -> list[dict]:
        data = world.get("data") or {}
        out = []
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        for nid, info in (data.get("bandits") or {}).items():
            node = nodes.get(int(nid))
            if not node or _now() > int(info.get("until", 0)):
                continue
            out.append({"node_id": int(nid), "name": node["name"],
                        "type": node["type"],
                        "troops": int((node.get("npc_garrison") or {}).get("troops", 0)),
                        "until": int(info.get("until", 0))})
        return out

    async def raid_bandit(self, user_id: str, army_id: int,
                          node_id: int = 0) -> dict:
        """讨伐匪寨：军团须驻扎在匪寨相邻据点（edge_only），立即野战结算。"""
        world, p = await self._require_player(user_id)
        bcfg = self.main_cfg().get("bandit") or {}
        data = world.get("data") or {}
        bandits = data.get("bandits") or {}
        army = await self._own_army(p, army_id)
        if army.get("state") != "idle" or int(army.get("troops", 0)) <= 0:
            raise StrategyError("军团须为驻扎状态且有兵力")
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        here = nodes.get(int(army["node_id"]))
        if not here:
            raise StrategyError("军团位置异常")
        adj_bandits = [int(x) for x in (here.get("adj") or [])
                       if str(x) in bandits and _now() <= int(bandits[str(x)].get("until", 0))
                       and int(nodes.get(int(x), {}).get("owner_faction", -1)) == 0]
        if not adj_bandits:
            raise StrategyError("相邻据点没有匪寨")
        target_id = int(node_id) if int(node_id) in adj_bandits else adj_bandits[0]
        node = nodes[target_id]
        atk_sim = await self._army_sim(army)
        dfn_sim = self._garrison_sim(node)
        if not atk_sim or not dfn_sim:
            raise StrategyError("兵力异常，无法讨伐")
        rng = self._rng("raid_bandit", army["id"], target_id, world["tick_no"])
        result = R.simulate_battle([atk_sim], [dfn_sim], rng,
                                   night=R.is_night(time.localtime().tm_hour),
                                   defending_node=True)
        report = R.render_battle_report(result, title=f"{node['name']}·讨伐匪寨")
        await self._persist_battle(world, node, int(p["faction_id"]), 0,
                                   "bandit", result, report, [atk_sim], [dfn_sim])
        await self._apply_battle_aftermath(world, node, [army], [], result,
                                           [atk_sim], [dfn_sim])
        if result["winner"] != "attacker":
            await self._commit()
            return {"ok": True, "win": False,
                    "msg": f"讨伐失利，匪众据守「{node['name']}」未下\n{report}"}
        # 胜利：清寨 + 战功 + 30% 招募令 + 成就进度
        bandits.pop(str(target_id), None)
        data["bandits"] = bandits
        world["data"] = data
        await self.save_world(world)
        gar = node.get("npc_garrison") or {}
        gar["troops"] = 0
        node["npc_garrison"] = gar
        await self.save_node(node)
        mlo, mhi = bcfg.get("merit_reward") or [100, 200]
        merit = rng.randint(int(mlo), int(mhi))
        merit_msg = ""
        pp = await self.get_player(user_id, world["id"])
        if pp:
            pp["merit"] = int(pp.get("merit", 0)) + merit
            pp["merit_season"] = int(pp.get("merit_season", 0)) + merit
            if rng.random() < float(bcfg.get("token_reward_chance", 0.3)):
                pp["recruit_token"] = int(pp.get("recruit_token", 0)) + 1
                merit_msg = "，缴获招募令 ×1"
            else:
                merit_msg = ""
            await self.save_player(pp)
        await self._award_achievement(user_id, "conquest_bandit_10", count=1)
        await self._commit()
        return {"ok": True, "win": True,
                "msg": f"🏴 匪寨「{node['name']}」已扫平！战功 +{merit}{merit_msg}\n{report}"}

    # ------------------------------------------------------------------
    # 自动商路（§5.5）
    # ------------------------------------------------------------------
    async def _ally_or_self_fn(self, world: dict):
        """构造 is_ally_or_self(fid_a, fid_b)（含外交缓存）。"""
        factions = {int(f["id"]): f for f in self._decode_all(
            "strategy_factions",
            await self._fetchall("SELECT * FROM strategy_factions WHERE world_id=?",
                                 (world["id"],)))}
        cache: dict[tuple[int, int], bool] = {}

        def fn(a: int, b: int) -> bool:
            if a == b:
                return True
            key = (min(a, b), max(a, b))
            if key not in cache:
                fa = factions.get(a)
                rel = R.relation_of((fa or {}).get("diplomacy") or {}, b, _now())
                cache[key] = rel == R.REL_ALLY
            return cache[key]
        return fn

    async def _compute_trade_routes(self, world: dict) -> list[dict]:
        nodes = await self.get_nodes(world["id"])
        adjacency = {n["id"]: [int(x) for x in (n.get("adj") or [])] for n in nodes}
        node_type = {n["id"]: n["type"] for n in nodes}
        owner = {n["id"]: int(n["owner_faction"]) for n in nodes}
        fn = await self._ally_or_self_fn(world)
        return R.find_trade_routes(adjacency, node_type, owner, fn)

    async def _step_trade_routes(self, world: dict):
        trcfg = self.main_cfg().get("trade_route") or {}
        coin = int(trcfg.get("coin_per_tick", 20))
        routes = await self._compute_trade_routes(world)
        data = world.get("data") or {}
        seen = set(data.get("trade_routes_seen") or [])
        new_keys = set()
        for rt in routes:
            key = f"{rt['a']}-{rt['b']}"
            new_keys.add(key)
        fresh = new_keys - seen
        for rt in routes:
            # 收益归属：路径起点所有者
            owner_fid = await self._route_owner(world, rt)
            if owner_fid <= 0:
                continue
            await self._pay_route_income(world, owner_fid, coin)
        if fresh:
            # 新商路 → 开路先锋成就（归属势力全员）
            for rt in routes:
                if f"{rt['a']}-{rt['b']}" in fresh:
                    fid = await self._route_owner(world, rt)
                    rows = await self._fetchall(
                        "SELECT user_id FROM strategy_players WHERE world_id=? "
                        "AND faction_id=?", (world["id"], fid))
                    for row in rows:
                        await self._award_achievement(row["user_id"],
                                                      "civil_trade_route")
        data["trade_routes_seen"] = sorted(new_keys)
        world["data"] = data
        await self.save_world(world)
        await self._commit()

    async def _route_owner(self, world: dict, route: dict) -> int:
        start = int((route.get("path") or [route["a"]])[0])
        node = await self.get_node(world["id"], start)
        return int((node or {}).get("owner_faction", 0))

    async def _pay_route_income(self, world: dict, fid: int, coin: int):
        f = await self.get_faction(fid)
        if not f:
            return
        rows = await self._fetchall(
            "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
            (world["id"], fid))
        if f.get("kind") == "npc" or len(rows) != 1:
            treasury = f.get("treasury") or {}
            treasury["coin"] = int(treasury.get("coin", 0)) + coin
            f["treasury"] = treasury
            await self.save_faction(f)
        else:
            pp = await self.get_player(rows[0]["user_id"], world["id"])
            if pp:
                pp["coin"] = int(pp.get("coin", 0)) + coin
                await self.save_player(pp)

    async def _trade_route_node_ids(self, world: dict) -> set[int]:
        """商路途经节点集合（trade 任务 +30% 用，§5.5）。"""
        out: set[int] = set()
        for rt in await self._compute_trade_routes(world):
            out.update(int(x) for x in rt.get("path") or [])
        return out

    async def get_trade_routes_view(self, world: dict) -> list[dict]:
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        out = []
        for rt in await self._compute_trade_routes(world):
            a, b = nodes.get(rt["a"]), nodes.get(rt["b"])
            out.append({"a": rt["a"], "b": rt["b"],
                        "a_name": (a or {}).get("name", "?"),
                        "b_name": (b or {}).get("name", "?"),
                        "path": rt["path"]})
        return out

    # ------------------------------------------------------------------
    # 运粮队（§5.6）
    # ------------------------------------------------------------------
    async def send_convoy(self, user_id: str, from_node: int, to_node: int,
                          grain: int, escort_army_id: int = 0) -> dict:
        world, p = await self._require_player(user_id)
        grain = int(grain)
        if grain <= 0:
            raise StrategyError("运粮量须为正")
        if int(p.get("grain", 0)) < grain:
            raise StrategyError(f"粮草不足（现有 {int(p.get('grain', 0))}）")
        src = await self._owned_node(p, int(from_node))
        dst = await self.get_node(world["id"], int(to_node))
        if not dst:
            raise StrategyError("目的地不存在")
        if int(dst["owner_faction"]) != int(p["faction_id"]):
            fn = await self._ally_or_self_fn(world)
            if not fn(int(p["faction_id"]), int(dst["owner_faction"])):
                raise StrategyError("只能向本方或盟友据点运粮")
        path = await self.find_path(world["id"], int(src["id"]), int(dst["id"]))
        if not path or len(path) < 2:
            raise StrategyError("两地不通")
        escort_troops = 0
        if int(escort_army_id):
            army = await self._own_army(p, escort_army_id)
            if army.get("state") != "idle" or int(army["node_id"]) != int(src["id"]):
                raise StrategyError("护卫军团须驻扎在出发地")
            escort_troops = int(army.get("troops", 0))
        p["grain"] -= grain
        await self.save_player(p)
        data = world.get("data") or {}
        convoys = data.setdefault("convoys", [])
        cid = f"cv{_now()}_{user_id[-4:]}"
        convoys.append({"id": cid, "owner": user_id,
                        "faction": int(p["faction_id"]),
                        "from": int(src["id"]), "to": int(dst["id"]),
                        "path": [int(x) for x in path], "idx": 0,
                        "grain": grain, "escort_troops": escort_troops,
                        "created_tick": int(world.get("tick_no", 0))})
        data["convoys"] = convoys[-40:]
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return {"ok": True, "convoy_id": cid,
                "msg": f"🚚 运粮队出发：{src['name']} → {dst['name']}"
                       f"（粮 {grain}，护军 {escort_troops}，"
                       f"{len(path) - 1} tick 后抵达）"}

    async def _step_convoys(self, world: dict):
        ccfg = self.main_cfg().get("convoy") or {}
        data = world.get("data") or {}
        convoys = data.get("convoys") or []
        if not convoys:
            return
        speed = max(1, int(ccfg.get("speed_edges", 1)))
        remaining = []
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        fn = await self._ally_or_self_fn(world)
        for cv in convoys:
            cv["idx"] = int(cv.get("idx", 0)) + speed
            path = cv.get("path") or []
            arrived = cv["idx"] >= len(path) - 1
            if arrived:
                dst = nodes.get(int(cv["to"]))
                if dst:
                    stock = dst.get("stock") or {}
                    stock["grain"] = int(stock.get("grain", 0)) + int(cv["grain"])
                    dst["stock"] = stock
                    await self.save_node(dst)
                continue
            cur_id = path[min(cv["idx"], len(path) - 1)]
            if int(cv.get("raided_at", -1)) == int(cur_id):
                remaining.append(cv)
                continue
            # 劫粮判定：当前节点上的敌对军团/匪寨
            raider = await self._find_convoy_raider(world, cur_id, int(cv["faction"]), fn)
            if raider:
                rng = self._rng("convoy", cv["id"], cur_id)
                if R.convoy_raid_success(rng, int(cv.get("escort_troops", 0)),
                                         int(raider["troops"])):
                    take = int(int(cv["grain"]) * float(ccfg.get("raid_take_pct", 0.5)))
                    cv["grain"] = int(cv["grain"]) - take
                    await self._give_raid_spoils(world, raider, take)
                    cv["raided_at"] = int(cur_id)
                    if cv["grain"] <= 0:
                        continue  # 粮尽被截
            remaining.append(cv)
        # idx 每 tick 都在推进，必须落盘（否则进度丢失）
        data["convoys"] = remaining
        world["data"] = data
        await self.save_world(world)
        await self._commit()

    async def _find_convoy_raider(self, world: dict, node_id: int,
                                  convoy_fid: int, fn) -> Optional[dict]:
        node = await self.get_node(world["id"], int(node_id))
        if not node:
            return None
        bandits = ((world.get("data") or {}).get("bandits") or {})
        if str(node_id) in bandits:
            return {"kind": "bandit", "troops": int((node.get("npc_garrison") or {}).get(
                "troops", 0)), "faction": 0, "owner": "", "node_id": int(node_id)}
        best = None
        for a in await self.get_armies(world["id"], node_id=int(node_id),
                                       states=("idle", "siege")):
            afid = int(a.get("faction_id", 0))
            if afid == convoy_fid or (afid == 0):
                continue
            if afid > 0 and fn(convoy_fid, afid):
                continue  # 盟友不劫粮
            if not best or int(a["troops"]) > int(best["troops"]):
                best = {"kind": "army", "troops": int(a["troops"]),
                        "faction": afid, "owner": a.get("owner_user_id", ""),
                        "node_id": int(node_id)}
        return best

    async def _give_raid_spoils(self, world: dict, raider: dict, grain: int):
        if grain <= 0:
            return
        if raider.get("owner"):
            pp = await self.get_player(raider["owner"], world["id"])
            if pp:
                pp["grain"] = int(pp.get("grain", 0)) + grain
                await self.save_player(pp)
        elif raider.get("faction", 0) > 0:
            f = await self.get_faction(int(raider["faction"]))
            if f:
                treasury = f.get("treasury") or {}
                treasury["grain"] = int(treasury.get("grain", 0)) + grain
                f["treasury"] = treasury
                await self.save_faction(f)
        elif raider.get("kind") == "bandit":
            # 劫粮归劫方：匪寨截得的粮草入库节点 stock（讨伐/占领后可回收），
            # 此前该分支缺失，被匪截的粮凭空蒸发
            node = await self.get_node(world["id"], int(raider.get("node_id", 0)))
            if node:
                stock = node.get("stock") or {}
                stock["grain"] = int(stock.get("grain", 0)) + grain
                node["stock"] = stock
                await self.save_node(node)

    async def get_convoys_view(self, user_id: str) -> list[dict]:
        world, p = await self._require_player(user_id)
        data = world.get("data") or {}
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        out = []
        for cv in data.get("convoys") or []:
            if cv.get("owner") != user_id and int(cv.get("faction", 0)) != int(p["faction_id"]):
                continue
            path = cv.get("path") or []
            idx = min(int(cv.get("idx", 0)), max(0, len(path) - 1))
            out.append({"id": cv["id"], "grain": cv["grain"],
                        "escort_troops": cv.get("escort_troops", 0),
                        "from": (nodes.get(int(cv["from"])) or {}).get("name", "?"),
                        "to": (nodes.get(int(cv["to"])) or {}).get("name", "?"),
                        "cur_node": (nodes.get(path[idx]) or {}).get("name", "?")
                        if path else "?",
                        "progress": f"{idx}/{max(1, len(path) - 1)}"})
        return out

    # ------------------------------------------------------------------
    # 赛季（§11）：阶段 / 合纵 / 结算 / 结转 / 追赶
    # ------------------------------------------------------------------
    def _season_day(self, world: dict) -> int:
        return max(0, (_now() - int(world.get("started_at", 0))) // 86400)

    async def _step_season(self, world: dict):
        scfg = self.main_cfg().get("season") or {}
        season_days = int(self.main_cfg().get("season_days", 75))
        day = self._season_day(world)
        phase = R.season_phase(day, season_days, int(scfg.get("mid_day", 31)),
                               int(scfg.get("late_day", 56)))
        data = world.get("data") or {}
        data["season_day"] = day
        data["season_phase"] = phase
        # 霸权 + 合纵迟滞
        nodes = await self.get_nodes(world["id"])
        types_by_fid: dict[int, list[str]] = {}
        for n in nodes:
            fid = int(n["owner_faction"])
            if fid > 0:
                types_by_fid.setdefault(fid, []).append(n["type"])
        shares = R.hegemony_share(types_by_fid, scfg.get("node_score"))
        top_fid, top_pct = 0, 0.0
        if shares:
            top_fid, top_pct = max(shares.items(), key=lambda kv: kv[1])
        coal = data.get("coalition") or {}
        active = R.coalition_state(top_pct, bool(coal.get("active")),
                                   float(scfg.get("hegemony_trigger_pct", 0.4)),
                                   float(scfg.get("hegemony_release_pct", 0.3)))
        data["coalition"] = {"active": active, "fid": top_fid,
                             "pct": round(top_pct, 4)}
        if phase == "final" and not data.get("final_flag"):
            data["final_flag"] = True
            logger.info(f"[strategy] 世界#{world['id']} 进入决战时刻（末 3 天）")
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        if _now() >= int(world.get("ends_at", 0)) or day >= season_days:
            await self._settle_season(world)

    def npc_aggression_mult(self, world: dict, fid: int, target_fid: int) -> float:
        """NPC 出征侵略性倍率：决战 +30%；合纵讨霸 ×1.5。"""
        scfg = self.main_cfg().get("season") or {}
        data = world.get("data") or {}
        mult = 1.0
        if data.get("season_phase") == "final":
            mult *= 1 + float(scfg.get("final_aggression_bonus", 0.3))
        coal = data.get("coalition") or {}
        if coal.get("active") and int(coal.get("fid", 0)) == int(target_fid) \
                and int(fid) != int(target_fid):
            mult *= float(scfg.get("hegemony_npc_aggression_mult", 1.5))
        return mult

    def season_output_mult(self, world: dict) -> float:
        scfg = self.main_cfg().get("season") or {}
        if (world.get("data") or {}).get("season_phase") == "final":
            return 1 + float(scfg.get("final_output_bonus", 0.2))
        return 1.0

    def player_output_mult(self, p: dict) -> float:
        """追赶礼包产出加成（14 天 ×1.5，§17）。"""
        ccfg = self.main_cfg().get("catchup") or {}
        until = int(((p.get("newcomer") or {}).get("catchup_boost_until", 0)))
        if until > _now():
            return float(ccfg.get("output_mult", 1.5))
        return 1.0

    async def _settle_season(self, world: dict) -> dict:
        """赛季结算：霸权排名 / 荣誉发放 / 英烈祠名将堂 / 老兵结转 / 开新世界。"""
        scfg = self.main_cfg().get("season") or {}
        season_no = int(world.get("season_no", 1))
        # C3：结算幂等守卫——本赛季已有存档即已结算过，拒绝重复触发
        # （防 GM 重置赛季与到期自动结算竞态导致荣誉双发/新世界双开）
        done = await self._fetchone(
            "SELECT id FROM strategy_season_results WHERE world_id=? AND season_no=?",
            (int(world["id"]), season_no))
        if done:
            raise StrategyError(f"赛季 S{season_no} 已结算过，请勿重复触发")
        nodes = await self.get_nodes(world["id"])
        types_by_fid: dict[int, list[str]] = {}
        for n in nodes:
            fid = int(n["owner_faction"])
            if fid > 0:
                types_by_fid.setdefault(fid, []).append(n["type"])
        shares = R.hegemony_share(types_by_fid, scfg.get("node_score"))
        factions = {int(f["id"]): f for f in self._decode_all(
            "strategy_factions",
            await self._fetchall("SELECT * FROM strategy_factions WHERE world_id=?",
                                 (world["id"],)))}
        scores = sorted(
            [{"faction_id": fid,
              "name": (factions.get(fid) or {}).get("name", "?"),
              "kind": (factions.get(fid) or {}).get("kind", "?"),
              "nodes": len(types_by_fid.get(fid, [])),
              "hegemony_pct": round(pct, 4)}
             for fid, pct in shares.items()],
            key=lambda x: -x["hegemony_pct"])
        # Top 玩家（战功赛季榜）
        top_rows = await self._fetchall(
            "SELECT user_id, merit_season, faction_id FROM strategy_players "
            "WHERE world_id=? ORDER BY merit_season DESC LIMIT ?",
            (world["id"], int(scfg.get("top_player_count", 10))))
        top_players = [{"user_id": r["user_id"],
                        "merit_season": int(r["merit_season"]),
                        "faction": (factions.get(int(r["faction_id"])) or {}).get(
                            "name", "?")}
                       for r in top_rows]
        # 荣誉发放（全体玩家：战功 → 荣誉点）
        rate = float(scfg.get("honor_per_merit", 1.0))
        for r in await self._fetchall(
                "SELECT user_id FROM strategy_players WHERE world_id=?",
                (world["id"],)):
            pp = await self.get_player(r["user_id"], world["id"])
            if not pp:
                continue
            gained = R.honor_from_merit(int(pp.get("merit_season", 0)), rate)
            honor = pp.get("honor") or {}
            honor["total"] = int(honor.get("total", 0)) + gained
            honor["last_season"] = {"season_no": season_no, "gained": gained}
            honor.setdefault("spent", {})
            pp["honor"] = honor
            await self.save_player(pp)
        # 御林军团老兵结转（veteran ≥300 → 50%）
        carryover: dict[str, dict] = {}
        for a in await self.get_armies(world["id"]):
            owner = a.get("owner_user_id") or ""
            if not owner:
                continue
            keep = R.veteran_carryover(int(a.get("veteran", 0)),
                                       float(scfg.get("veteran_carryover_pct", 0.5)))
            if keep <= 0:
                continue
            old = carryover.get(owner)
            if not old or keep > int(old.get("veteran", 0)):
                carryover[owner] = {"veteran": keep,
                                    "army_name": a.get("name", "")}
        # 英烈祠 / 名将堂
        champion = scores[0]["name"] if scores else "无"
        await self._execute("""
            INSERT INTO strategy_memorials (season_no, world_id, kind, char_name,
                user_id, faction_name, snapshot_json, created_at)
            VALUES (?,?, 'season_summary','','',?,?,?)
        """, (season_no, int(world["id"]), champion,
              json.dumps({"scores": scores[:10], "top_players": top_players[:3]},
                         ensure_ascii=False), _now()))
        for tp in top_players[:3]:
            await self._execute("""
                INSERT INTO strategy_memorials (season_no, world_id, kind, char_name,
                    user_id, faction_name, snapshot_json, created_at)
                VALUES (?,?, 'hall_of_fame','',?,?,?,?)
            """, (season_no, int(world["id"]), tp["user_id"], tp["faction"],
                  json.dumps({"merit_season": tp["merit_season"]},
                             ensure_ascii=False), _now()))
        for c in await self.get_chars(world_id=world["id"], status="dead"):
            if c.get("rarity") in ("epic", "legend"):
                await self._execute("""
                    INSERT INTO strategy_memorials (season_no, world_id, kind,
                        char_name, user_id, faction_name, snapshot_json, created_at)
                    VALUES (?,?, 'fame',?,?,?,?,?)
                """, (season_no, int(world["id"]), c["name"],
                      c.get("owner_user_id", ""),
                      (factions.get(int(c.get("faction_id", 0))) or {}).get("name", ""),
                      json.dumps({"rarity": c.get("rarity"), "level": c.get("level")},
                                 ensure_ascii=False), _now()))
        # 赛季存档
        await self._execute("""
            INSERT INTO strategy_season_results (world_id, season_no, ended_at,
                faction_scores_json, top_players_json, data_json)
            VALUES (?,?,?,?,?,?)
        """, (int(world["id"]), season_no, _now(),
              json.dumps(scores, ensure_ascii=False),
              json.dumps(top_players, ensure_ascii=False),
              json.dumps({"carryover": carryover,
                          "coalition": (world.get("data") or {}).get("coalition")},
                         ensure_ascii=False)))
        world["state"] = "ended"
        await self.save_world(world)
        await self._commit()
        logger.info(f"[strategy] 赛季 S{season_no} 结算完成：霸主「{champion}」，"
                    f"结转 {len(carryover)} 人")
        new_world = await self._create_world(season_no=season_no + 1)
        return {"ok": True, "season_no": season_no, "champion": champion,
                "scores": scores[:5], "top_players": top_players,
                "new_world_id": new_world["id"]}

    async def get_season_view(self, world: dict) -> dict:
        data = world.get("data") or {}
        rows = await self._fetchall(
            "SELECT season_no, faction_scores_json, top_players_json, ended_at "
            "FROM strategy_season_results ORDER BY season_no DESC LIMIT 3")
        history = [{"season_no": r["season_no"], "ended_at": r["ended_at"],
                    "scores": json.loads(r["faction_scores_json"] or "[]")[:3],
                    "top": json.loads(r["top_players_json"] or "[]")[:3]}
                   for r in rows]
        return {"season_no": int(world.get("season_no", 1)),
                "day": int(data.get("season_day", 0)),
                "phase": data.get("season_phase", "early"),
                "season_days": int(self.main_cfg().get("season_days", 75)),
                "ends_at": int(world.get("ends_at", 0)),
                "coalition": data.get("coalition") or {},
                "final": bool(data.get("final_flag")),
                "history": history}

    async def claim_carryover(self, user_id: str, army_id: int) -> dict:
        """御林老兵结转：把上赛季 veteran 50% 注入本赛季一支军团（一次性）。"""
        world, p = await self._require_player(user_id)
        prev = await self._fetchone(
            "SELECT data_json FROM strategy_season_results WHERE season_no=? "
            "ORDER BY id DESC LIMIT 1", (int(world.get("season_no", 1)) - 1,))
        if not prev:
            raise StrategyError("没有上赛季存档")
        carry = ((json.loads(prev["data_json"] or "{}")).get("carryover") or {}).get(user_id)
        if not carry or int(carry.get("veteran", 0)) <= 0:
            raise StrategyError("你没有可结转的御林老兵（上赛季 veteran ≥300）")
        data = world.get("data") or {}
        claimed = data.setdefault("carryover_claimed", [])
        if user_id in claimed:
            raise StrategyError("本赛季已领取过结转")
        army = await self._own_army(p, army_id)
        army["veteran"] = min(R.VETERAN_MAX,
                              int(army.get("veteran", 0)) + int(carry["veteran"]))
        await self.save_army(army)
        claimed.append(user_id)
        data["carryover_claimed"] = claimed
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return {"ok": True,
                "msg": f"🎖「{carry.get('army_name', '旧部')}」的老兵经验已注入"
                       f"「{army['name']}」（veteran +{int(carry['veteran'])}）"}

    async def claim_catchup(self, user_id: str) -> dict:
        """追赶礼包（§17）：中期入场者一次性领取缩放资源包 + 14 天产出加成。"""
        world, p = await self._require_player(user_id)
        ccfg = self.main_cfg().get("catchup") or {}
        newcomer = p.get("newcomer") or {}
        if newcomer.get("catchup_claimed"):
            raise StrategyError("追赶礼包已领取")
        season_days = int(self.main_cfg().get("season_days", 75))
        joined_at = int(p.get("created_at", 0) or 0)
        lag_days = (joined_at - int(world.get("started_at", 0))) // 86400 \
            if joined_at else self._season_day(world)
        if lag_days < int(ccfg.get("days", 7)):
            raise StrategyError("开服 7 天内入场者无需追赶礼包")
        day = self._season_day(world)
        mult = R.catchup_scale(day, season_days,
                               float(ccfg.get("mid_season_mult", 1.5)))
        pack = R.scale_pack(self.main_cfg().get("starter_pack") or {}, mult)
        for k in ("coin", "grain", "iron", "recruit_token"):
            if int(pack.get(k, 0)) > 0:
                p[k] = int(p.get(k, 0)) + int(pack[k])
        newcomer["catchup_claimed"] = _now()
        newcomer["catchup_boost_until"] = _now() + int(ccfg.get("output_days", 14)) * 86400
        p["newcomer"] = newcomer
        await self.save_player(p)
        # 引荐奖励
        ref = str(newcomer.get("referred_by") or "")
        if ref and ref != user_id:
            rp = await self.get_player(ref, world["id"])
            if rp:
                bonus = int(ccfg.get("referral_merit", 200))
                rp["merit"] = int(rp.get("merit", 0)) + bonus
                rp["merit_season"] = int(rp.get("merit_season", 0)) + bonus
                await self.save_player(rp)
        await self._commit()
        return {"ok": True,
                "msg": f"📦 追赶礼包已发放（×{mult:.2f}）："
                       + "、".join(f"{k}+{v}" for k, v in pack.items()
                                   if k in ("coin", "grain", "iron", "recruit_token")
                                   and int(v) > 0)
                       + f"；产出 ×{float(ccfg.get('output_mult', 1.5))} "
                         f"持续 {int(ccfg.get('output_days', 14))} 天"}

    # ------------------------------------------------------------------
    # P3 事件效果（§11.1：流将 / 劫掠来袭 / 仙盟会武）
    # ------------------------------------------------------------------
    async def _p3_event_onset(self, world: dict, ev: dict, target: dict,
                              rng) -> None:
        """P3 事件的一次性效果（由主类 _apply_event_onset 兜底调用）。"""
        data = world.get("data") or {}
        dirty = False
        for eff in ev.get("effects") or []:
            etype = eff.get("type")
            if etype == "spawn_wandering_char":
                cnt = eff.get("count", 1)
                if isinstance(cnt, (list, tuple)) and len(cnt) >= 2:
                    count = rng.randint(int(cnt[0]), int(cnt[1]))
                else:
                    count = int(cnt[0] if isinstance(cnt, (list, tuple)) else cnt)
                rarity = eff.get("rarity", "epic")
                cands = [n for n in await self.get_nodes(world["id"])
                         if int(n["owner_faction"]) == 0] or \
                        await self.get_nodes(world["id"])
                for i in range(count):
                    if not cands:
                        break
                    node = rng.choice(cands)
                    c = R.generate_character(rng, rarity=rarity,
                                             role=R.ROLE_GENERAL,
                                             name_pools=self.cfg.get("names") or {})
                    c.update({"world_id": world["id"], "faction_id": 0,
                              "owner_user_id": "", "loyalty": 50,
                              "status": "wander",
                              "location": {"node_id": int(node["id"])}})
                    await self.insert_char(c)
                    logger.info(f"[strategy] 流将 {c['name']}（{rarity}）现身"
                                f"「{node['name']}」")
            elif etype == "bandit_raid":
                bandits = data.setdefault("bandits", {})
                for nid in target.get("node_ids") or []:
                    node = await self.get_node(world["id"], int(nid))
                    if not node:
                        continue
                    pct = float(eff.get("stock_loss_pct", 0.1))
                    stock = node.get("stock") or {}
                    if stock:
                        node["stock"] = {k: max(0, int(v or 0) - int(int(v or 0) * pct))
                                         for k, v in stock.items()}
                    lo, hi = eff.get("troops") or [600, 1200]
                    node["npc_garrison"] = {"troops": rng.randint(int(lo), int(hi)),
                                            "morale": 90}
                    await self.save_node(node)
                    bandits[str(node["id"])] = {
                        "spawned_at": _now(), "origin": "event",
                        "until": _now() + int(eff.get("hours", 24)) * 3600}
                dirty = True
            elif etype == "tournament":
                data["tournament_until_tick"] = int(world.get("tick_no", 0)) + \
                    int(ev.get("duration_ticks", 24))
                data["tournament_wins"] = {}
                dirty = True
        if dirty:
            world["data"] = data
            await self.save_world(world)
        await self._commit()

    async def _tournament_win(self, world: dict, user_id: str) -> bool:
        """仙盟会武期间斗法获胜计数；夺魁（5 胜）→ 成就。"""
        data = world.get("data") or {}
        until = int(data.get("tournament_until_tick", 0))
        if until <= 0 or int(world.get("tick_no", 0)) > until:
            return False
        wins = data.setdefault("tournament_wins", {})
        wins[user_id] = int(wins.get(user_id, 0)) + 1
        champion = int(wins[user_id]) >= 5
        if champion:
            # C-M7：连胜清零须在落盘之前（否则重启后计数仍 ≥5，
            # 下一次获胜重复触发夺魁成就）
            wins[user_id] = 0
        data["tournament_wins"] = wins
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        if champion:
            await self._award_achievement(user_id, "legend_duel_champion")
            return True
        return False

    async def recruit_wanderer(self, user_id: str, char_id: int) -> dict:
        """招募在野流将（天降流将事件产物）。"""
        world, p = await self._require_player(user_id)
        c = await self.get_char(int(char_id))
        if not c or c.get("status") != "wander":
            raise StrategyError("对方不是在野流将")
        # C-M6：与酒馆招募同口径的角色数量上限（此前流将可无限突破 roster cap）
        rcfg = self.main_cfg().get("recruit") or {}
        cap = int(rcfg.get("character_cap_base", 10)) + \
            int(rcfg.get("character_cap_per_level", 3)) * 0  # P0：势力等级恒 1
        owned = await self.get_chars(owner_user_id=user_id, world_id=world["id"])
        if len(owned) >= cap:
            raise StrategyError(f"角色数量已达上限（{cap}）")
        price = int(WANDERER_PRICE.get(c.get("rarity", "common"), 2000))
        if int(p.get("coin", 0)) < price:
            raise StrategyError(f"铜钱不足：招募 {c['name']} 需 {price}")
        p["coin"] -= price
        c["status"] = "idle"
        c["owner_user_id"] = user_id
        c["faction_id"] = int(p["faction_id"])
        c["loyalty"] = 60
        await self.save_char(c)
        await self.save_player(p)
        await self._commit()
        return {"ok": True,
                "msg": f"🎴 {c['name']}（{c.get('rarity')}）感念诚意，慨然入仕！"}
