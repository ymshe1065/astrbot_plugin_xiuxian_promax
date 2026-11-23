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
    "civil_mine_100k": 100_000,
    "civil_build_5": 5,
}

#: 流将已改为坊市战功暗标，底价由 core.strategy_rules.wanderer_* 计算


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
        # 成就战功同样走观察期/多开门禁，避免成为绕过通道
        merit = await self._settle_merit(user_id, merit, "achievement")
        if merit > 0:
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
    #: 坊市可挂售品类（market.items 缺省时兜底）
    _MARKET_ITEMS = ("grain", "iron", "recruit_token", "intel")

    def _market_items(self) -> list[str]:
        """当前配置允许挂售的品类（与 market_list 校验口径一致）。"""
        mcfg = self.main_cfg().get("market") or {}
        return [str(x) for x in (mcfg.get("items") or self._MARKET_ITEMS)]

    def _market_price_range(self, item: str) -> tuple[int, int, int]:
        """玩家挂单允许的价格区间 (最低, 最高, 基准)；基准缺失时最高为 0。"""
        mcfg = self.main_cfg().get("market") or {}
        base = (mcfg.get("base_prices") or {}).get(item, 0)
        return R.market_price_band(
            base, float(mcfg.get("player_price_band_pct", 0.5) or 0))

    @staticmethod
    def _market_item_qty(p: dict, item: str) -> int:
        """玩家持有量：情报存于 tavern.intel，其余为钱包字段。"""
        if item == "intel":
            return int((p.get("tavern") or {}).get("intel", 0) or 0)
        return int(p.get(item, 0) or 0)

    @staticmethod
    def _market_item_add(p: dict, item: str, amount: int) -> None:
        amount = int(amount)
        if item == "intel":
            tavern = p.get("tavern") or {}
            tavern["intel"] = int(tavern.get("intel", 0) or 0) + amount
            p["tavern"] = tavern
        else:
            p[item] = int(p.get(item, 0) or 0) + amount

    async def _market_at_war(self, fid_a: int, fid_b: int) -> bool:
        """双方是否处于交战（§5.5 交战势力不可互市）。"""
        fid_a, fid_b = int(fid_a or 0), int(fid_b or 0)
        if fid_a <= 0 or fid_b <= 0 or fid_a == fid_b:
            return False
        fa = await self.get_faction(fid_a)
        if not fa:
            return False
        return R.relation_of(fa.get("diplomacy") or {}, fid_b, _now()) == R.REL_WAR

    async def market_list(self, user_id: str, node_id: int, item_type: str,
                          amount: int, unit_price: int) -> dict:
        world, p = await self._require_player(user_id)
        mcfg = self.main_cfg().get("market") or {}
        item_type = str(item_type)
        amount, unit_price = int(amount), int(unit_price)
        items = self._market_items()
        if item_type not in items:
            raise StrategyError(
                "仅可挂售 " + "/".join(R.RESOURCE_NAMES.get(x, x) for x in items))
        base_prices = mcfg.get("base_prices") or {}
        if unit_price <= 0:
            # 未定价 → 取配置基准价（情报 market.base_prices.intel）
            unit_price = int(base_prices.get(item_type, 0) or 0)
        if amount <= 0 or unit_price <= 0:
            raise StrategyError("数量与单价须为正")
        lo, hi, base = self._market_price_range(item_type)
        if hi and not (lo <= unit_price <= hi):
            raise StrategyError(
                f"{R.RESOURCE_NAMES.get(item_type, item_type)} 定价须在市场价 "
                f"±{float(mcfg.get('player_price_band_pct', 0.5) or 0) * 100:.0f}% "
                f"内（{lo}~{hi}，基准 {base}）")
        held = self._market_item_qty(p, item_type)
        if held < amount:
            raise StrategyError(
                f"{R.RESOURCE_NAMES.get(item_type, item_type)} 不足（现有 {held}）")
        node = await self.get_node(world["id"], int(node_id))
        if not node or node["type"] not in ("city", "market"):
            raise StrategyError("只能在仙城/坊市挂单")
        # §5.5：不能在本方正在交战的势力所属坊市挂单（挂单侧校验）
        owner = int(node.get("owner_faction", 0) or 0)
        if owner and owner != int(p["faction_id"]) and \
                mcfg.get("cross_faction_market_only", True) and \
                await self._market_at_war(int(p["faction_id"]), owner):
            raise StrategyError("交战势力不可互市")
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM strategy_market_orders WHERE world_id=? "
            "AND seller_user=? AND status='open'", (world["id"], user_id))
        limit = int(mcfg.get("max_orders_per_user", 20))
        if int((row or {}).get("n", 0)) >= limit:
            raise StrategyError(f"挂单数已达上限（{limit}）")
        self._market_item_add(p, item_type, -amount)
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
                "msg": f"已在「{node['name']}」挂售 "
                       f"{R.RESOURCE_NAMES.get(item_type, item_type)} ×{amount}"
                       f"（单价 {unit_price}，"
                       f"{int(mcfg.get('order_expire_hours', 48))}h 有效）"}

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
        # §5.5 交战势力不可互市（买卖双方，含 NPC 卖方）
        if mcfg.get("cross_faction_market_only", True) and \
                int(row["seller_faction"] or 0) != int(p["faction_id"]) and \
                await self._market_at_war(int(p["faction_id"]),
                                          int(row["seller_faction"] or 0)):
            raise StrategyError("交战势力不可互市")
        avail = int(row["amount"])
        want = avail if int(amount) <= 0 else min(int(amount), avail)
        if want <= 0:
            raise StrategyError("数量无效")
        # §5.6 商队：我方商队途经的坊市享 convoy.discount_pct 折扣
        discount = await self._convoy_discount(world, p, int(row["node_id"]))
        cost = int(round(want * int(row["unit_price"]) * (1.0 - discount)))
        cost = max(0, cost)
        if int(p.get("coin", 0)) < cost:
            raise StrategyError(f"铜钱不足：需 {cost}")
        p["coin"] -= cost
        tax = R.market_tax(cost, float(mcfg.get("tax_pct", 0.05)))
        payout = cost - tax
        item = row["item_type"]
        self._market_item_add(p, item, want)
        await self.save_player(p)
        # 卖方入账
        if row["seller_user"]:
            sp = await self.get_player(row["seller_user"], world["id"])
            if sp:
                sp["coin"] = int(sp.get("coin", 0)) + payout
                await self.save_player(sp)
        else:
            sf = await self.get_faction(int(row["seller_faction"] or 0))
            if sf:
                treasury = sf.get("treasury") or {}
                treasury["coin"] = int(treasury.get("coin", 0)) + payout
                sf["treasury"] = treasury
                await self.save_faction(sf)
            else:
                # 无归属势力的 NPC 单不应存在；显式告警，避免货款静默蒸发
                logger.warning(
                    f"[strategy] NPC 卖单 #{int(order_id)} 缺少 seller_faction，"
                    f"货款 {payout} 未入账")
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
        if rest <= 0:
            # 挂单售罄：同步作废仍在等待中的 NPC 收购报价
            await self._close_market_offers(int(order_id))
        await self._commit()
        # 与 NPC 势力通商 → 好感小幅提升（每日上限，失败不影响成交）
        if not row["seller_user"]:
            try:
                await self.add_trade_favor(int(p["faction_id"]),
                                           int(row["seller_faction"]))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[strategy] 通商好感结算失败：{e}")
        extra = f"，商队折扣 {discount * 100:.0f}%" if discount > 0 else ""
        return {"ok": True,
                "msg": f"购入 {R.RESOURCE_NAMES.get(item, item)} ×{want}，"
                       f"花费 {cost} 铜钱"
                       f"（含税 {tax}{extra}）",
                "discount": discount, "amount": want,
                "unit_price": int(row["unit_price"]),
                "cost": int(cost), "tax": int(tax)}

    async def convoy_discount_for(self, user_id: str, node_id: int) -> float:
        """我方商队途经该坊市时的价格折扣比例（§5.6，0~0.5）。

        供主管理器 market_buy / 价格计算复用；本类 `_market_buy` 已内置
        同一折扣，调用方不要重复扣减。
        """
        world = await self.get_world()
        if not world:
            return 0.0
        p = await self.get_player(user_id, world["id"])
        if not p:
            return 0.0
        return await self._convoy_discount(world, p, int(node_id))

    async def _convoy_discount(self, world: dict, p: dict,
                               node_id: int) -> float:
        dcfg = self.main_cfg().get("convoy") or {}
        pct = float(dcfg.get("discount_pct", 0) or 0)
        if pct <= 0 or int(node_id) <= 0:
            return 0.0
        for cv in (world.get("data") or {}).get("convoys") or []:
            if cv.get("owner") != p.get("user_id") and \
                    int(cv.get("faction", 0) or 0) != int(p["faction_id"]):
                continue
            path = [int(x) for x in (cv.get("path") or [])]
            if int(node_id) in path:
                return max(0.0, min(0.5, pct / 100.0))
        return 0.0

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
        n = await self._exec_count(
            "UPDATE strategy_market_orders SET status='cancelled', amount=0 "
            "WHERE id=? AND status='open' AND amount=?",
            (int(order_id), int(row["amount"])))
        if n != 1:
            # 已被并发买入成交：不能退货（autocommit 下 rollback 无效，
            # 必须先做条件更新再退款，避免「货已退回 + 订单仍成交」双得）。
            raise StrategyError("订单已被并发修改，无法撤单")
        await self._close_market_offers(int(order_id))
        if item in self._market_items():
            self._market_item_add(p, item, int(row["amount"]))
            await self.save_player(p)
        await self._commit()
        return {"ok": True,
                "msg": f"已撤单，退回 {R.RESOURCE_NAMES.get(item, item)} "
                       f"×{int(row['amount'])}"}

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
                        "item_type": r["item_type"],
                        "item_name": R.RESOURCE_NAMES.get(r["item_type"],
                                                          r["item_type"]),
                        "amount": r["amount"],
                        "unit_price": r["unit_price"],
                        "seller_faction": r["seller_faction"],
                        "seller_user": r["seller_user"],
                        "npc": not r["seller_user"],
                        "mine": r["seller_user"] == user_id,
                        "expires_at": r["expires_at"]})
        return out

    async def _close_market_offers(self, order_id: int) -> None:
        """挂单成交/撤销后，作废仍处于待处理的收购报价并退回冻结铜钱。"""
        rows = await self._fetchall(
            "SELECT * FROM strategy_market_offers WHERE order_id=? "
            "AND status='pending'", (int(order_id),))
        for offer in rows:
            n = await self._exec_count(
                "UPDATE strategy_market_offers SET status='expired' "
                "WHERE id=? AND status='pending'", (int(offer["id"]),))
            if n == 1:
                await self._release_offer_reserve(offer)

    async def market_offers(self, user_id: str) -> list[dict]:
        """当前玩家收到的 NPC 收购报价（仅本人可见，且挂单仍有效）。"""
        world = await self.get_world()
        if not world:
            return []
        now = _now()
        rows = await self._fetchall("""
            SELECT f.*, o.amount AS order_amount, o.expires_at AS order_expires_at
            FROM strategy_market_offers f
            JOIN strategy_market_orders o ON o.id=f.order_id
            WHERE f.world_id=? AND f.seller_user=? AND f.status='pending'
              AND f.expires_at>? AND o.status='open' AND o.expires_at>?
            ORDER BY f.created_at ASC, f.id ASC
        """, (world["id"], user_id, now, now))
        mcfg = self.main_cfg().get("market") or {}
        tax_pct = float(mcfg.get("tax_pct", 0.05))
        names: dict[int, str] = {}
        out = []
        for r in rows:
            amount = min(int(r["amount"]), int(r["order_amount"]))
            if amount <= 0:
                continue
            price = int(r["unit_price"])
            gross = amount * price
            tax = R.market_tax(gross, tax_pct)
            fid = int(r["buyer_faction"] or 0)
            if fid and fid not in names:
                f = await self.get_faction(fid)
                names[fid] = str((f or {}).get("name") or "")
            out.append({
                "offer_id": r["id"], "order_id": r["order_id"],
                "node_id": r["node_id"], "item_type": r["item_type"],
                "item_name": R.RESOURCE_NAMES.get(r["item_type"], r["item_type"]),
                "amount": amount, "unit_price": price,
                "gross": gross, "tax": tax, "payout": gross - tax,
                "buyer_faction": fid,
                "buyer_name": names.get(fid) or "坊市行商",
                "created_at": r["created_at"],
                "expires_at": min(int(r["expires_at"]),
                                  int(r["order_expires_at"])),
            })
        return out

    async def market_offer_accept(self, user_id: str, offer_id: int) -> dict:
        """接受 NPC 收购报价：即时成交，卖家扣除交易税后入账。"""
        async with self._market_lock:
            world, p = await self._require_player(user_id)
            now = _now()
            offer = await self._fetchone(
                "SELECT * FROM strategy_market_offers WHERE id=? AND world_id=? "
                "AND seller_user=? AND status='pending'",
                (int(offer_id), world["id"], user_id))
            if not offer:
                raise StrategyError("报价不存在或已处理")
            if int(offer["expires_at"]) <= now:
                raise StrategyError("报价已过期")
            order = await self._fetchone(
                "SELECT * FROM strategy_market_orders WHERE id=? AND world_id=?",
                (int(offer["order_id"]), world["id"]))
            if not order or order["status"] != "open":
                raise StrategyError("挂单已成交或已下架")
            if int(order["expires_at"]) <= now:
                raise StrategyError("挂单已过期")
            mcfg = self.main_cfg().get("market") or {}
            buyer_fid = int(offer["buyer_faction"] or 0)
            # §5.5 交战势力不可互市（报价生成后若已开战，接受侧同样拦截）
            if mcfg.get("cross_faction_market_only", True) and buyer_fid and \
                    await self._market_at_war(int(p["faction_id"]), buyer_fid):
                raise StrategyError("交战势力不可互市")
            avail = int(order["amount"])
            amount = min(int(offer["amount"]), avail)
            if amount <= 0:
                raise StrategyError("挂单可售数量不足")
            price = int(offer["unit_price"])
            gross = amount * price
            tax = R.market_tax(gross, float(mcfg.get("tax_pct", 0.05)))
            payout = gross - tax
            rest = avail - amount
            if rest <= 0:
                n = await self._exec_count(
                    "UPDATE strategy_market_orders SET status='filled', amount=0, "
                    "buyer_user=? WHERE id=? AND status='open' AND amount=?",
                    (f"npc:{buyer_fid}", int(order["id"]), avail))
            else:
                n = await self._exec_count(
                    "UPDATE strategy_market_orders SET amount=? WHERE id=? "
                    "AND status='open' AND amount=?", (rest, int(order["id"]), avail))
            if n != 1:
                await self.db.conn.rollback()
                raise StrategyError("挂单已被并发修改，请重试")
            n2 = await self._exec_count(
                "UPDATE strategy_market_offers SET status='accepted' "
                "WHERE id=? AND status='pending'", (int(offer_id),))
            if n2 != 1:
                await self.db.conn.rollback()
                raise StrategyError("报价已被处理")
            if rest <= 0:
                await self._close_market_offers(int(order["id"]))
            p["coin"] = int(p.get("coin", 0)) + payout
            await self.save_player(p)
            buyer = await self.get_faction(buyer_fid) if buyer_fid else None
            if buyer:
                # §10.7 成交：释放本报价冻结，扣国库铜钱，物品入买家国库
                treasury = buyer.get("treasury") or {}
                frozen = int(offer["amount"]) * price
                treasury["reserved_coin"] = max(
                    0, int(treasury.get("reserved_coin", 0) or 0) - frozen)
                treasury["coin"] = max(
                    0, int(treasury.get("coin", 0) or 0) - gross)
                item_key = str(order["item_type"])
                if item_key in ("grain", "iron"):
                    treasury[item_key] = int(
                        treasury.get(item_key, 0) or 0) + amount
                buyer["treasury"] = treasury
                await self.save_faction(buyer)
            await self._commit()
            buyer_name = str((buyer or {}).get("name") or "坊市行商")
            if buyer and str(buyer.get("kind") or "") == "npc":
                try:
                    await self.add_trade_favor(int(p["faction_id"]), buyer_fid)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[strategy] NPC 收购好感结算失败：{e}")
            item = order["item_type"]
            extra = "（挂单已全部售出）" if rest <= 0 else f"（挂单剩余 {rest}）"
            return {"ok": True, "amount": amount, "gross": gross,
                    "tax": tax, "payout": payout, "rest": rest,
                    "msg": f"已向「{buyer_name}」售出 "
                           f"{R.RESOURCE_NAMES.get(item, item)} ×{amount}，"
                           f"成交 {gross} 铜钱（税 {tax}，实收 {payout}）{extra}"}

    async def market_offer_decline(self, user_id: str, offer_id: int) -> dict:
        """拒绝 NPC 收购报价，挂单继续保留等待其他买家（退回冻结铜钱）。"""
        async with self._market_lock:
            world, _p = await self._require_player(user_id)
            offer = await self._fetchone(
                "SELECT * FROM strategy_market_offers WHERE id=? AND world_id=? "
                "AND seller_user=? AND status='pending'",
                (int(offer_id), world["id"], user_id))
            if not offer:
                raise StrategyError("报价不存在或已处理")
            n = await self._exec_count(
                "UPDATE strategy_market_offers SET status='declined' "
                "WHERE id=? AND world_id=? AND seller_user=? AND status='pending'",
                (int(offer_id), world["id"], user_id))
            if n != 1:
                raise StrategyError("报价不存在或已处理")
            await self._release_offer_reserve(offer)
            await self._commit()
            return {"ok": True, "msg": "已拒绝本次报价，挂单继续保留"}

    async def _step_market(self, world: dict):
        """tick：过期订单下架并退货，同时作废该订单的待处理报价。

        NPC 做市单（``seller_user=''``）的剩余量退回所属势力国库（§10.7 真实库存）。
        """
        rows = await self._fetchall(
            "SELECT * FROM strategy_market_orders WHERE world_id=? AND status='open' "
            "AND expires_at<?", (world["id"], _now()))
        for r in rows:
            # 先条件下架，再退货：与撤单/买入并发时，只有真正抢到
            # open→expired 的一方能退款，避免货被退回后订单又成交。
            n = await self._exec_count(
                "UPDATE strategy_market_orders SET status='expired', amount=0 "
                "WHERE id=? AND status='open' AND amount=?",
                (int(r["id"]), int(r["amount"])))
            if n != 1:
                continue
            await self._close_market_offers(int(r["id"]))
            if r["seller_user"] and r["item_type"] in self._market_items():
                sp = await self.get_player(r["seller_user"], world["id"])
                if sp:
                    self._market_item_add(sp, r["item_type"], int(r["amount"]))
                    await self.save_player(sp)
            elif not r["seller_user"] and int(r["seller_faction"] or 0) > 0:
                # NPC 卖单：把冻结在国库里的真实库存退回（仅 grain/iron）
                await self._market_refund_npc_stock(
                    int(r["seller_faction"]), str(r["item_type"]),
                    int(r["amount"]))
        if rows:
            await self._commit()

    async def _market_refund_npc_stock(self, faction_id: int, item: str,
                                       amount: int) -> int:
        """NPC 坊市库存退回国库（过期/撤单），返回实际退回量（§10.7）。"""
        item = str(item or "")
        amount = max(0, int(amount or 0))
        if amount <= 0 or item not in ("grain", "iron"):
            return 0
        f = await self.get_faction(int(faction_id))
        if not f:
            return 0
        self._treasury_add(f, **{item: amount})
        await self.save_faction(f)
        return amount

    async def _step_market_maker(self, world: dict):
        """NPC 做市（§10.7）：只挂国库真实持有的 grain/iron，挂单即冻结库存。

        - NPC 自有坊市：由据点归属势力供货；
        - 玩家/无主坊市：由“行商”（库存最充裕的 NPC 势力）调货供货，
          保持坊市流动性，同时货源仍来自真实国库。
        """
        mcfg = self.main_cfg().get("market") or {}
        ecfg = self._npc_economy_cfg()
        interval = max(1, int(mcfg.get("npc_maker_interval_ticks", 8)))
        tick_no = int(world.get("tick_no", 0))
        # 性格 trade_bias 可把挂单间隔缩短到 1/4（取整至少 1 tick）
        if tick_no % max(1, interval // 4) != 0:
            return
        rng = self._rng("maker", world["id"], tick_no)
        spread = float(mcfg.get("npc_maker_spread_pct", 0.15))
        base_prices = mcfg.get("base_prices") or {}
        sell_items = [str(x) for x in (ecfg.get("npc_sell_items") or [])
                      if str(x) in ("grain", "iron")]
        surplus_pct = max(0.0, min(1.0, float(ecfg["surplus_sell_pct"])))
        nodes = [n for n in await self.get_nodes(world["id"])
                 if n["type"] == "market"]
        npc_rows = await self._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "AND alive=1", (world["id"],))
        npc_ids = [int(x["id"]) for x in npc_rows]
        if not nodes or not npc_ids:
            return
        npc_set = set(npc_ids)
        faction_cache: dict[int, Optional[dict]] = {}
        made = 0
        for node in nodes:
            cnt = await self._fetchone(
                "SELECT COUNT(*) AS n FROM strategy_market_orders WHERE world_id=? "
                "AND node_id=? AND status='open' AND seller_user=''",
                (world["id"], node["id"]))
            if int((cnt or {}).get("n", 0)) >= 6:
                continue
            owner = int(node["owner_faction"] or 0)
            sellers = [owner] if owner in npc_set else npc_ids
            best: Optional[tuple[dict, str, int]] = None
            for sid in sellers:
                if sid not in faction_cache:
                    faction_cache[sid] = await self.get_faction(sid)
                f = faction_cache[sid]
                if not f or str(f.get("kind") or "") == "player":
                    continue
                bias = self._npc_personality_mult(f, "trade_bias")
                eff_interval = max(1, int(interval / max(0.1, bias)))
                # 活跃性格可在更短间隔多挂一轮；基础间隔 tick 仍保底执行
                if tick_no % eff_interval != 0 and tick_no % interval != 0:
                    continue
                reserve = self._npc_reserve(f)
                treasury = self._npc_treasury(f)
                # 供给 = 国库库存 − 守财底线×2；且须超过库存的 surplus_sell_pct
                for item in sell_items:
                    stock = int(treasury.get(item, 0) or 0)
                    surplus = stock - int(reserve.get(item, 0)) * 2
                    if surplus <= 0:
                        continue
                    if surplus < int(stock * surplus_pct):
                        continue
                    if best is None or surplus > best[2]:
                        best = (f, item, surplus)
            if not best:
                continue
            f, item, surplus = best
            amount = max(1, min(surplus, int(surplus * rng.uniform(0.3, 0.6))))
            base = float(base_prices.get(item, 2))
            price = R.npc_maker_price(base, rng, spread, is_sell=True)
            # 挂牌即从国库扣减（天然冻结），过期/撤单再退回
            if not self._treasury_spend(f, **{item: amount}):
                continue
            await self._execute("""
                INSERT INTO strategy_market_orders (world_id, node_id, seller_faction,
                    seller_user, item_type, amount, unit_price, status, created_at,
                    expires_at)
                VALUES (?,?,?,'',?,?,?,'open',?,?)
            """, (world["id"], int(node["id"]), int(f["id"]),
                  item, amount, price, _now(),
                  _now() + int(mcfg.get(
                      "npc_order_expire_hours",
                      mcfg.get("order_expire_hours", 48))) * 3600))
            await self.save_faction(f)
            made += 1
        if made:
            await self._commit()

    async def _step_market_offers(self, world: dict):
        """NPC 收购报价（§10.7）：需求驱动选品/选买家，出价前冻结国库铜钱。

        冻结口径：``treasury.reserved_coin += amount × price``；成交时释放
        冻结并把物品入买家国库；过期/拒绝/挂单结束则退回冻结。
        """
        mcfg = self.main_cfg().get("market") or {}
        ecfg = self._npc_economy_cfg()
        now = _now()
        # 1) 作废已过期 / 挂单已成交或下架的报价，并退回对应冻结铜钱
        stale = await self._fetchall("""
            SELECT f.* FROM strategy_market_offers f
            WHERE f.world_id=? AND f.status='pending' AND (
                f.expires_at<=? OR NOT EXISTS (
                    SELECT 1 FROM strategy_market_orders o
                    WHERE o.id=f.order_id
                      AND o.status='open' AND o.expires_at>?))
        """, (world["id"], now, now))
        expired = 0
        for offer in stale:
            n = await self._exec_count(
                "UPDATE strategy_market_offers SET status='expired' "
                "WHERE id=? AND status='pending'", (int(offer["id"]),))
            if n != 1:
                continue
            expired += 1
            await self._release_offer_reserve(offer)
        # 2) 到报价间隔才生成新报价（default：每 8 tick ≈ 2h）
        interval = max(1, int(mcfg.get("npc_offer_interval_ticks", 8)))
        if int(world.get("tick_no", 0)) % interval != 0:
            if expired:
                await self._commit()
            return
        chance = max(0.0, min(1.0, float(mcfg.get("npc_offer_chance", 0.6))))
        cooldown = max(0, int(mcfg.get("npc_offer_cooldown_hours", 2))) * 3600
        rows = await self._fetchall("""
            SELECT o.* FROM strategy_market_orders o
            WHERE o.world_id=? AND o.status='open' AND o.seller_user<>''
              AND o.expires_at>?
              AND NOT EXISTS (SELECT 1 FROM strategy_market_offers f
                              WHERE f.order_id=o.id AND f.status='pending')
              AND NOT EXISTS (SELECT 1 FROM strategy_market_offers f2
                              WHERE f2.order_id=o.id AND f2.created_at>?)
            ORDER BY o.id ASC
        """, (world["id"], now, now - cooldown))
        npcs = await self._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? "
            "AND kind='npc' AND alive=1", (world["id"],))
        made = 0
        # 仅 grain/iron 参与 NPC 收购（token/intel 无 NPC 产出源/国库口径）
        offer_items = {str(x) for x in (ecfg.get("npc_offer_items") or [])
                       if str(x) in ("grain", "iron")} or {"grain", "iron"}
        if rows and npcs:
            node_owner = {int(n["id"]): int(n["owner_faction"] or 0)
                          for n in await self.get_nodes(world["id"])}
            rng = self._rng("market_offers", world["id"], world["tick_no"])
            expire_hours = max(1, int(mcfg.get("npc_offer_expire_hours", 12)))
            min_pct = max(0.01, min(1.0, float(
                mcfg.get("npc_offer_min_pct", 0.3))))
            max_pct = max(min_pct, min(1.0, float(
                mcfg.get("npc_offer_max_pct", 1.0))))
            spread = float(mcfg.get("npc_offer_spread_pct", 0.15))
            buyers: dict[int, dict] = {}
            for x in npcs:
                fid = int(x["id"])
                bf = await self.get_faction(fid)
                if bf:
                    buyers[fid] = bf
            # 坊市活跃度：trade_bias 越高，报价越勤（概率放大一次）
            max_bias = max(
                (self._npc_personality_mult(bf, "trade_bias")
                 for bf in buyers.values()), default=1.0)
            chance_eff = min(1.0, chance * max(0.2, max_bias))
            for r in rows:
                item = str(r["item_type"])
                if item not in offer_items:
                    continue
                avail = int(r["amount"])
                if avail <= 0:
                    continue
                if rng.random() >= chance_eff:
                    continue
                price = R.npc_offer_price(int(r["unit_price"]), rng, spread)
                if price <= 0:
                    continue
                want = max(1, min(avail, int(round(
                    avail * rng.uniform(min_pct, max_pct)))))
                # 需求驱动选买家：国库缺口越大越优先，其次 node 归属势力
                owner = node_owner.get(int(r["node_id"]), 0)
                ranked: list[tuple[float, int]] = []
                for bid, bf in buyers.items():
                    score = self._npc_need_score(bf, item)
                    if int(bid) == int(owner):
                        score += 0.5
                    ranked.append((score, int(bid)))
                ranked.sort(key=lambda kv: (-kv[0], kv[1]))
                for _score, bid in ranked:
                    bf = buyers[bid]
                    t = self._npc_treasury(bf)
                    # §10.7 守财底线：出价后 coin - reserved_coin 不得低于
                    # treasury_reserve（收购同样受底线约束，保住东山再起本金）。
                    reserve_coin = int(self._npc_reserve(bf).get("coin", 0) or 0)
                    free_coin = max(
                        0, int(t["coin"]) - int(t["reserved_coin"]) - reserve_coin)
                    amount = min(want, free_coin // price)
                    if amount <= 0:
                        continue
                    expires = min(now + expire_hours * 3600,
                                  int(r["expires_at"]))
                    if expires <= now:
                        continue
                    # 出价前冻结：reserved_coin 计入国库 JSON（无需迁移）
                    gross = int(amount) * price
                    self._treasury_add(bf, reserved_coin=gross)
                    await self.save_faction(bf)
                    await self._execute("""
                        INSERT INTO strategy_market_offers (world_id, order_id,
                            node_id, seller_user, buyer_faction, item_type, amount,
                            unit_price, status, created_at, expires_at)
                        VALUES (?,?,?,?,?,?,?,?,'pending',?,?)
                    """, (world["id"], int(r["id"]), int(r["node_id"]),
                          str(r["seller_user"]), int(bid), item,
                          amount, price, now, expires))
                    made += 1
                    break
        if made:
            logger.info(f"[strategy] 坊市 NPC 收购报价新增 {made} 条")
        if made or expired:
            await self._commit()

    def _npc_need_score(self, f: dict, item: str) -> float:
        """国库缺口评分（0~1）：目标库存 = 守财底线×3，越缺越想要（§10.7）。"""
        if item not in ("grain", "iron"):
            return 0.0
        reserve = self._npc_reserve(f)
        stock = int(self._npc_treasury(f).get(item, 0) or 0)
        target = max(1.0, float(reserve.get(item, 0)) * 3.0)
        return max(0.0, min(1.0, (target - stock) / target))

    async def _release_offer_reserve(self, offer: dict) -> int:
        """退回某条报价冻结的铜钱（成交/过期/拒绝/挂单结束共用）。"""
        fid = int(offer.get("buyer_faction") or 0)
        gross = int(offer.get("amount", 0) or 0) * int(
            offer.get("unit_price", 0) or 0)
        if fid <= 0 or gross <= 0:
            return 0
        f = await self.get_faction(fid)
        if not f:
            return 0
        t = dict(f.get("treasury") or {})
        t["reserved_coin"] = max(
            0, int(t.get("reserved_coin", 0) or 0) - gross)
        f["treasury"] = t
        await self.save_faction(f)
        return gross

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
                # 清理 lair 缓存：只删 data.bandits 会让 last_raid_tick 残留，
                # 同节点周刷新匪寨后当 tick 就会被判定“已到劫掠间隔”立刻劫掠。
                lair = dict(node.get("lair") or {})
                if lair.get("bandit") or str(lair.get("source") or "") == "bandit_data":
                    for k in ("bandit", "source", "last_raid_tick"):
                        lair.pop(k, None)
                    node["lair"] = lair
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
                # 新匪寨从当前 tick 重新起算劫掠间隔（清理旧的 lair 缓存）
                lair = dict(node.get("lair") or {})
                for k in ("bandit", "source", "last_raid_tick"):
                    lair.pop(k, None)
                node["lair"] = lair
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
            # 与 raid_bandit 的可讨伐条件保持一致：只展示无主匪寨
            if int(node.get("owner_faction", 0) or 0) != 0:
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
        if self._is_reorganizing(army, world):
            raise StrategyError(self._reorg_msg(army, world))
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
        await self._apply_battle_aftermath(world, node, [army], [], result)
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
        # §6.4 势力任务：剿匪安境
        await self._faction_task_progress(int(p["faction_id"]), "subdue", 1)
        mlo, mhi = bcfg.get("merit_reward") or [100, 200]
        merit = rng.randint(int(mlo), int(mhi))
        merit = await self._settle_merit(user_id, merit, "raid_bandit")
        merit_msg = ""
        pp = await self.get_player(user_id, world["id"])
        if pp:
            if merit > 0:
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
                fb = factions.get(b)
                aid_a = int((fa or {}).get("alliance_id") or 0)
                aid_b = int((fb or {}).get("alliance_id") or 0)
                if aid_a and aid_a == aid_b:
                    cache[key] = True
                else:
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

    async def _routes_for_faction(self, world: dict,
                                  faction_id: int) -> list[dict]:
        """本方及盟友商路网络中的路线（长度/数量决定额外加成）。"""
        fid = int(faction_id or 0)
        if fid <= 0:
            return []
        routes = await self._compute_trade_routes(world)
        if not routes:
            return []
        fn = await self._ally_or_self_fn(world)
        return [rt for rt in routes
                if fn(fid, int(rt.get("owner") or 0))]

    async def _trade_route_task_bonus(self, world: dict,
                                      faction_id: int) -> float:
        """当前势力（含盟友）商路网络提供的贸易任务产出加成。"""
        trcfg = self.main_cfg().get("trade_route") or {}
        routes = await self._routes_for_faction(world, faction_id)
        return R.trade_route_task_bonus(
            routes,
            base=float(trcfg.get("trade_task_bonus", 0.3)),
            per_route=float(trcfg.get("task_bonus_per_route", 0.05)),
            per_edge=float(trcfg.get("task_bonus_per_edge", 0.02)),
            max_bonus=float(trcfg.get("task_bonus_max", 1.0)),
        )

    async def trade_route_output_bonus(self, world: dict) -> dict[int, float]:
        """商路途经据点的自然产出加成（全势力共用一张商路网）。"""
        trcfg = self.main_cfg().get("trade_route") or {}
        routes = await self._compute_trade_routes(world)
        return R.trade_route_output_bonus(
            routes,
            base=float(trcfg.get("node_output_bonus_base", 0.0)),
            per_route=float(trcfg.get("node_output_bonus_per_route", 0.02)),
            per_edge=float(trcfg.get("node_output_bonus_per_edge", 0.01)),
            max_bonus=float(trcfg.get("node_output_bonus_max", 0.4)),
        )

    async def _step_trade_routes(self, world: dict):
        trcfg = self.main_cfg().get("trade_route") or {}
        coin = int(trcfg.get("coin_per_tick", 20))
        routes = await self._compute_trade_routes(world)
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        data = world.get("data") or {}
        # v4.0.17：seen 从「key 列表」升级为 {key: {owner, label}}，
        # 以便商路被切断时知道该通知哪个势力并展示两端名称。
        raw_seen = data.get("trade_routes_seen") or {}
        if isinstance(raw_seen, dict):
            prev = {str(k): (v if isinstance(v, dict) else
                             {"owner": int(v or 0), "label": str(k)})
                    for k, v in raw_seen.items()}
        else:
            # 旧版只存 key 列表：尽力还原两端名称与起点归属，避免升级后
            # 第一条被切断的商路因 owner=0 而漏发提醒。
            prev = {}
            for key in raw_seen:
                key = str(key)
                try:
                    a_id, b_id = (int(x) for x in key.split("-", 1))
                except (TypeError, ValueError):
                    prev[key] = {"owner": 0, "label": key}
                    continue
                a, b = nodes.get(a_id), nodes.get(b_id)
                prev[key] = {
                    "owner": int((a or {}).get("owner_faction", 0) or 0),
                    "label": f"{(a or {}).get('name', key)} ⇄ "
                             f"{(b or {}).get('name', key)}"}
        current: dict[str, dict] = {}
        for rt in routes:
            key = f"{rt['a']}-{rt['b']}"
            a, b = nodes.get(rt["a"]), nodes.get(rt["b"])
            label = f"{(a or {}).get('name', '?')} ⇄ {(b or {}).get('name', '?')}"
            # 收益归属：路径起点所有者
            owner_fid = await self._route_owner(world, rt)
            current[key] = {"owner": owner_fid, "label": label}
            if owner_fid <= 0:
                continue
            await self._pay_route_income(world, owner_fid, coin)
        fresh = set(current) - set(prev)
        broken = set(prev) - set(current)
        # 新商路 → 开路先锋成就（归属势力全员）
        for key in fresh:
            fid = int(current[key].get("owner") or 0)
            if fid <= 0:
                continue
            rows = await self._fetchall(
                "SELECT user_id FROM strategy_players WHERE world_id=? "
                "AND faction_id=?", (world["id"], fid))
            for row in rows:
                await self._award_achievement(row["user_id"],
                                              "civil_trade_route")
        # 商路被第三方切断 → 推送提醒（§5.5）
        for key in broken:
            info = prev.get(key) or {}
            fid = int(info.get("owner") or 0)
            if fid <= 0:
                continue
            rows = await self._fetchall(
                "SELECT user_id FROM strategy_players WHERE world_id=? "
                "AND faction_id=?", (world["id"], fid))
            await self._notify(
                world, [r["user_id"] for r in rows], "trade_route_cut",
                {"trade_route_key": key,
                 "text": f"商路中断：{info.get('label', key)}"
                         "（途经据点被敌方切断，收益已停止）"})
        data["trade_routes_seen"] = current
        world["data"] = data
        await self.save_world(world)
        await self._commit()

    async def _route_owner(self, world: dict, route: dict) -> int:
        owner = int(route.get("owner") or 0)
        if owner > 0:
            return owner
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
            path = rt["path"]
            out.append({"a": rt["a"], "b": rt["b"],
                        "owner": int(rt.get("owner") or 0),
                        "edge_count": max(0, len(path) - 1),
                        "a_name": (a or {}).get("name", "?"),
                        "b_name": (b or {}).get("name", "?"),
                        "path": path,
                        "path_names": [(nodes.get(int(nid)) or {}).get("name", "?")
                                       for nid in path]})
        return out

    # ------------------------------------------------------------------
    # 运粮队（§5.6）
    # ------------------------------------------------------------------
    async def _find_convoy_path(self, world: dict, faction_id: int,
                                src: int, dst: int) -> Optional[list[int]]:
        """运粮路径 BFS：只允许经过本方/盟友据点（§5.6 沿商路行进）。"""
        if src == dst:
            return [dst]
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        if src not in nodes or dst not in nodes:
            return None
        fn = await self._ally_or_self_fn(world)
        if not fn(int(faction_id), int(nodes[dst]["owner_faction"])):
            return None
        from collections import deque

        prev: dict[int, Optional[int]] = {src: None}
        queue = deque([src])
        while queue:
            cur = queue.popleft()
            for nxt in nodes[cur].get("adj") or []:
                nxt = int(nxt)
                if nxt in prev or nxt not in nodes:
                    continue
                owner = int(nodes[nxt]["owner_faction"])
                if owner <= 0 or not fn(int(faction_id), owner):
                    continue
                prev[nxt] = cur
                if nxt == dst:
                    path = [dst]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])
                    return list(reversed(path))
                queue.append(nxt)
        return None

    async def _recruit_troop_cost(self, p: dict, troop_type: str,
                                  troops: int) -> tuple[int, int, dict]:
        """按编军机制计算兵力花费（铜钱/粮草，含动员与农政折扣）。"""
        troops = max(0, int(troops))
        tt = R.TROOP_TYPES.get(troop_type, R.TROOP_TYPES["sword_shield"])
        troop_mult = await self._faction_buff(
            int(p["faction_id"]), "troop_cost_mult")
        coin = int(troops * tt["recruit_coin"] / 10 * troop_mult)
        grain = int(troops * tt["recruit_grain"] / 10 * troop_mult)
        faction = await self.get_faction(int(p["faction_id"]))
        grain = int(grain * float(R.tech_effects(
            (faction or {}).get("tech") or {}).get("grain_cost_mult", 1.0)))
        return coin, grain, tt

    async def send_convoy(self, user_id: str, from_node: int, to_node: int,
                          grain: int, escort_army_id: int = 0,
                          escort_troops: int = 0,
                          escort_troop_type: str = "") -> dict:
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
        path = await self._find_convoy_path(
            world, int(p["faction_id"]), int(src["id"]), int(dst["id"]))
        if not path or len(path) < 2:
            raise StrategyError("两地不通：运粮路线须全程经由本方/盟友据点")
        ccfg = self.main_cfg().get("convoy") or {}
        extra_troops = int(escort_troops or 0)
        escort_troops = 0
        escort_army = None
        if int(escort_army_id):
            escort_army = await self._own_army(p, escort_army_id)
            if escort_army.get("state") != "idle" or \
                    int(escort_army["node_id"]) != int(src["id"]):
                raise StrategyError("护卫军团须空闲且驻扎在出发地")
            escort_troops = int(escort_army.get("troops", 0))
        # 额外护送军队：与编军同价，按兵种招募价 + 势力动员/农政折扣计算
        if extra_troops < 0:
            raise StrategyError("护送军队兵力不能为负")
        extra_type = str(escort_troop_type or "").strip() or str(
            ccfg.get("escort_troop_type") or "sword_shield")
        extra_coin = extra_grain = 0
        extra_tt = R.TROOP_TYPES.get(extra_type)
        if extra_troops > 0:
            min_extra = max(1, int(ccfg.get("escort_min_troops", 100)))
            if extra_troops < min_extra:
                raise StrategyError(f"额外护送军队至少 {min_extra} 兵")
            if not extra_tt:
                raise StrategyError("护送军队兵种无效")
            extra_coin, extra_grain, extra_tt = await self._recruit_troop_cost(
                p, extra_type, extra_troops)
            if p["coin"] < extra_coin or p["grain"] < grain + extra_grain:
                raise StrategyError(
                    f"资源不足：运粮 {grain} 粮 + 护送军需铜钱 "
                    f"{extra_coin} / 粮草 {extra_grain}")
        data = world.get("data") or {}
        convoys = data.setdefault("convoys", [])
        # v4.3.0：队列满则拒绝新发货——此前挤出旧队会吞掉护送军/粮草
        # （旧队被切片丢弃后护送军靠孤儿清扫才释放，粮已蒸发）。
        max_convoys = int(ccfg.get("max_active", 40))
        if len(convoys) >= max_convoys:
            raise StrategyError(
                f"粮队队列已满（{len(convoys)}/{max_convoys}），"
                f"请等待在途粮队抵达后再发货")
        # 额外护送军为一次性征召，抵达/被截后不返还；先付粮草再扣运粮
        p["grain"] -= grain + extra_grain
        if extra_coin:
            p["coin"] -= extra_coin
        await self.save_player(p)
        cid = f"cv{_now()}_{time.time_ns() % 100000000:08d}"
        convoys.append({"id": cid, "owner": user_id,
                        "faction": int(p["faction_id"]),
                        "from": int(src["id"]), "to": int(dst["id"]),
                        "path": [int(x) for x in path], "idx": 0,
                        "grain": grain,
                        "escort_troops": escort_troops + extra_troops,
                        "escort_paid_troops": extra_troops,
                        "escort_troop_type": extra_type if extra_troops else "",
                        "escort_army_id": int(escort_army_id) if escort_army else 0,
                        "created_tick": int(world.get("tick_no", 0))})
        data["convoys"] = convoys
        world["data"] = data
        if escort_army:
            # 护送期间军团被占用：随粮队移动，抵达/被截后释放（§5.6）
            escort_army["state"] = "escort"
            escort_army["path"] = []
            escort_army["edge_pos"] = 0.0
            await self.save_army(escort_army)
        await self.save_world(world)
        await self._commit()
        extra_msg = ""
        if extra_troops:
            extra_msg = (f"，额外护军 {extra_troops}（{extra_tt.get('name', extra_type)}，"
                         f"耗铜钱 {extra_coin}/粮草 {extra_grain}）")
        return {"ok": True, "convoy_id": cid,
                "escort_troops": escort_troops + extra_troops,
                "escort_cost": {"coin": extra_coin, "grain": extra_grain},
                "msg": f"🚚 运粮队出发：{src['name']} → {dst['name']}"
                       f"（粮 {grain}，护军 {escort_troops + extra_troops}"
                       f"{extra_msg}，{len(path) - 1} tick 后抵达）"}

    async def _release_convoy_escort(self, cv: dict, node_id: int = 0) -> bool:
        """释放粮队护送军：恢复 idle 并落到指定据点（缺省保持原地）。"""
        aid = int(cv.get("escort_army_id") or 0)
        if aid <= 0:
            return False
        army = await self.get_army(aid)
        if not army or army.get("state") != "escort":
            return False
        army["state"] = "idle"
        army["path"] = []
        army["edge_pos"] = 0.0
        land = int(node_id or army.get("node_id") or 0)
        # §4.4：护送沿途据点可能易主，落点须仍为己方/联盟，否则退回最近己方据点
        land = await self._char_fallback_location(
            int(army.get("world_id") or 0),
            {"faction_id": int(army.get("faction_id") or 0)}, land)
        if land > 0:
            army["node_id"] = int(land)
        await self.save_army(army)
        # 护送结束：成员所在地与军团落点同步（§4.2）
        await self._sync_army_chars_location(army)
        return True

    async def _release_orphan_convoy_escorts(self, world: dict,
                                             convoys: list[dict]) -> int:
        """兜底：清理已无对应粮队却仍处于 escort 的军团。"""
        keep = {int(c.get("escort_army_id") or 0) for c in convoys}
        keep.discard(0)
        released = 0
        for army in await self.get_armies(world["id"], states=("escort",)):
            if int(army["id"]) in keep:
                continue
            army["state"] = "idle"
            army["path"] = []
            army["edge_pos"] = 0.0
            # 停靠点若已易主为敌方/无主，退回最近己方据点（§4.4）
            land = await self._char_fallback_location(
                int(world["id"]), {"faction_id": int(army.get("faction_id") or 0)},
                int(army.get("node_id") or 0))
            if land > 0:
                army["node_id"] = int(land)
            await self.save_army(army)
            # 孤儿护送军解除占用：成员所在地同步到军团停靠点
            await self._sync_army_chars_location(army)
            released += 1
        return released

    async def _step_convoys(self, world: dict):
        ccfg = self.main_cfg().get("convoy") or {}
        data = world.get("data") or {}
        convoys = data.get("convoys") or []
        if not convoys:
            if await self._release_orphan_convoy_escorts(world, []):
                await self._commit()
            return
        speed_cfg = max(1, int(ccfg.get("speed_edges", 1)))
        base_troops = max(0, int(ccfg.get("troops", 100)))
        raid_pct = float(ccfg.get("raid_take_pct", 0.5))
        remaining = []
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        fn = await self._ally_or_self_fn(world)
        effects_cache: dict[int, dict] = {}
        for i, cv in enumerate(convoys):
            fid = int(cv.get("faction", 0))
            if fid not in effects_cache:
                faction = await self.get_faction(fid) if fid > 0 else None
                effects_cache[fid] = R.tech_effects(
                    (faction or {}).get("tech") or {})
            eff = effects_cache[fid]
            # 农政科技 L4：运粮队机动 +1（§8.4）
            speed = max(1, speed_cfg + int(eff.get("convoy_move_bonus", 0)))
            path = cv.get("path") or []
            cur_idx = int(cv.get("idx", 0))
            target_idx = min(cur_idx + speed, max(0, len(path) - 1))
            arrived = target_idx >= len(path) - 1
            # 逐格巡检：机动 >1 时必须检查所有经过的中间节点，否则农政
            # 提速后可以“跳过”敌境节点直达目的地而不被劫。
            wiped_out = False
            for step_i in range(cur_idx + 1, target_idx + 1):
                cell_id = int(path[step_i])
                if int(cv.get("raided_at", -1)) == cell_id:
                    continue
                raider = await self._find_convoy_raider(
                    world, cell_id, int(cv["faction"]), fn)
                if not raider:
                    continue
                rng = self._rng("convoy", cv["id"], cell_id)
                defense = base_troops + int(cv.get("escort_troops", 0))
                if not R.convoy_raid_success(rng, defense, int(raider["troops"])):
                    continue
                # 农政科技 L4：被劫损失 -50%
                take = int(int(cv["grain"]) * raid_pct *
                           float(eff.get("convoy_loss_mult", 1.0)))
                take = min(int(cv["grain"]), max(1, take))
                cv["grain"] = int(cv["grain"]) - take
                cv["raided_at"] = int(cell_id)
                # 先落盘损失，再发劫粮收益：否则 tick 在发放后异常退出
                # 会按未扣减的旧粮量重复被劫（战利品凭空翻倍）。
                data["convoys"] = remaining + [cv] + convoys[i + 1:]
                world["data"] = data
                await self.save_world(world)
                await self._commit()
                await self._give_raid_spoils(world, raider, take)
                await self._notify(
                    world, [cv.get("owner")], "convoy_raided",
                    {"convoy_id": cv["id"], "node_id": int(cell_id),
                     "text": f"运粮队「{(nodes.get(int(cv['from'])) or {}).get('name', '?')}"
                             f" → {(nodes.get(int(cv['to'])) or {}).get('name', '?')}」"
                             f"在「{(nodes.get(int(cell_id)) or {}).get('name', '?')}」"
                             f"被劫，损失粮草 {take}（剩余 {int(cv['grain'])}）"})
                if cv["grain"] <= 0:
                    # 粮尽被截：护送军退回出发地，队伍从队列移除
                    cv["idx"] = step_i
                    wiped_out = True
                    break
            if wiped_out:
                await self._release_convoy_escort(cv, int(cv.get("from", 0)))
                data["convoys"] = remaining + convoys[i + 1:]
                world["data"] = data
                await self.save_world(world)
                await self._commit()
                continue
            cv["idx"] = target_idx
            if arrived:
                dst = nodes.get(int(cv["to"]))
                dst_owner = int((dst or {}).get("owner_faction", 0) or 0)
                # 抵达时复查归属：途中目的地可能已被敌方夺取/清零，粮草
                # 不能直接充入敌方库存（并顺带把护送军丢进敌城）。
                deliver = bool(dst) and (
                    dst_owner == fid or
                    (dst_owner > 0 and fn(fid, dst_owner)))
                # 先落盘「粮队离队」，再入库粮食：tick 若在入库后异常退出，
                # 不会在下一 tick 按原进度重复发粮。
                data["convoys"] = remaining + convoys[i + 1:]
                world["data"] = data
                await self.save_world(world)
                await self._commit()
                if deliver:
                    stock = dst.get("stock") or {}
                    stock["grain"] = int(stock.get("grain", 0)) + int(cv["grain"])
                    dst["stock"] = stock
                    await self.save_node(dst)
                    await self._release_convoy_escort(cv, int(cv["to"]))
                    await self._notify(
                        world, [cv.get("owner")], "convoy_arrived",
                        {"convoy_id": cv["id"], "node_id": int(cv["to"]),
                         "text": f"运粮队抵达「{dst.get('name', '?')}」，"
                                 f"入库粮草 {int(cv['grain'])}"})
                else:
                    back = nodes.get(int(cv.get("from", 0)))
                    back_owner = int((back or {}).get("owner_faction", 0) or 0)
                    back_ok = bool(back) and (
                        back_owner == fid or
                        (back_owner > 0 and fn(fid, back_owner)))
                    land = int(back["id"]) if back_ok else 0
                    if back_ok:
                        stock = back.get("stock") or {}
                        stock["grain"] = int(stock.get("grain", 0)) + int(cv["grain"])
                        back["stock"] = stock
                        await self.save_node(back)
                    await self._release_convoy_escort(cv, land)
                    await self._notify(
                        world, [cv.get("owner")], "convoy_lost",
                        {"convoy_id": cv["id"], "node_id": land,
                         "text": f"运粮队目的地「{(dst or {}).get('name', '?')}」"
                                 f"已失守，" + (f"粮草折返「{back.get('name', '?')}」"
                                                if back_ok else
                                                f"粮草 {int(cv['grain'])} 失散")})
                continue
            cur_id = path[target_idx]
            # 护送军随粮队行进（仍在 escort 状态，不可另行出征）
            if int(cv.get("escort_army_id") or 0):
                army = await self.get_army(int(cv["escort_army_id"]))
                if army and army.get("state") == "escort" and \
                        int(army.get("node_id", 0)) != int(cur_id):
                    army["node_id"] = int(cur_id)
                    await self.save_army(army)
                    # 护送军随粮队移动：成员 location 同步（§4.2）
                    await self._sync_army_chars_location(army)
            remaining.append(cv)
        # idx 每 tick 都在推进，必须落盘（否则进度丢失）
        data["convoys"] = remaining
        world["data"] = data
        await self._release_orphan_convoy_escorts(world, remaining)
        await self.save_world(world)
        await self._commit()

    async def _find_convoy_raider(self, world: dict, node_id: int,
                                  convoy_fid: int, fn) -> Optional[dict]:
        node = await self.get_node(world["id"], int(node_id))
        if not node:
            return None
        bandits = ((world.get("data") or {}).get("bandits") or {})
        info = bandits.get(str(node_id))
        if info is not None:
            # 与 _step_bandit_raids 同口径：只有「仍无主且未过期」的匪寨
            # 才能劫粮；城池被玩家攻占后 data.bandits 可能残留，不能继续
            # 把新主的城当匪寨。
            until = int((info or {}).get("until", 0) or 0)
            if int(node.get("owner_faction", 0) or 0) <= 0 and \
                    (until <= 0 or _now() <= until):
                return {"kind": "bandit", "troops": int(
                    (node.get("npc_garrison") or {}).get("troops", 0)),
                    "faction": 0, "owner": "", "node_id": int(node_id)}
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
            cur_id = int(path[idx]) if path else 0
            out.append({"id": cv["id"], "grain": cv["grain"],
                        "escort_troops": cv.get("escort_troops", 0),
                        "escort_paid_troops": int(cv.get("escort_paid_troops") or 0),
                        "escort_troop_type": cv.get("escort_troop_type") or "",
                        "escort_army_id": int(cv.get("escort_army_id") or 0),
                        "from": (nodes.get(int(cv["from"])) or {}).get("name", "?"),
                        "to": (nodes.get(int(cv["to"])) or {}).get("name", "?"),
                        "cur_node": (nodes.get(cur_id) or {}).get("name", "?")
                        if path else "?",
                        "from_id": int(cv["from"]), "to_id": int(cv["to"]),
                        "cur_node_id": cur_id,
                        "progress": f"{idx}/{max(1, len(path) - 1)}"})
        for sp in (world.get("data") or {}).get("army_supplies") or []:
            if sp.get("owner") != user_id and int(sp.get("faction", 0)) != int(p["faction_id"]):
                continue
            army = await self.get_army(int(sp.get("army_id", 0) or 0))
            out.append({"id": sp.get("id"), "kind": "army_supply",
                        "grain": int(sp.get("grain", 0) or 0),
                        "escort_troops": int((self.main_cfg().get("convoy") or {}).get(
                            "troops", 100)) * 2,
                        "escort_army_id": 0,
                        "from": (nodes.get(int(sp.get("node_id", 0) or 0)) or {}).get(
                            "name", "?"),
                        "to": (army or {}).get("name", "?"),
                        "cur_node": (nodes.get(int(sp.get("node_id", 0) or 0)) or {}).get(
                            "name", "?"),
                        "from_id": int(sp.get("node_id", 0) or 0),
                        "to_id": int((army or {}).get("node_id", 0) or 0),
                        "cur_node_id": int(sp.get("node_id", 0) or 0),
                        "progress": "追击中"})
        return out

    # ------------------------------------------------------------------
    # 匪寨周期劫掠（§9.1）
    # ------------------------------------------------------------------
    async def _step_bandit_raids(self, world: dict) -> int:
        """匪寨每隔 `bandit.raid_interval_ticks` 劫掠相邻玩家据点。

        扣库存 30%、民心 -5，并向据点所属势力玩家发通知；状态写
        `strategy_nodes.lair_json.last_raid_tick`。由主 Agent 在 tick 管线接线。
        """
        bcfg = self.main_cfg().get("bandit") or {}
        interval = max(1, int(bcfg.get("raid_interval_ticks", 8) or 8))
        tick_no = int(world.get("tick_no", 0))
        stock_pct = 0.3
        morale_delta = -5
        data = world.get("data") or {}
        bandits = data.get("bandits") or {}
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        kind_cache: dict[int, str] = {}
        raided = 0
        for nid, node in nodes.items():
            # 匪寨判定不再绑定 type=="ruin"：无主据点/城池崩塌/事件生成
            # 的匪寨都可能分布在 city/pass/branch/mine 等类型上，统一以
            # lair_json / data.bandits 标记为准。
            lair = node.get("lair") or {}
            in_bandits = str(nid) in bandits and \
                int(node.get("owner_faction", 0) or 0) <= 0
            # data.bandits 派生的临时匪寨：随数据清理失效（lair 只作缓存）；
            # lair_json.bandit=true 的静态匪寨长期存在
            is_lair = in_bandits or (
                bool(lair.get("bandit")) and lair.get("source") != "bandit_data")
            if not is_lair:
                continue
            info = bandits.get(str(nid)) or {}
            if int(info.get("until", 0) or 0) and _now() > int(info["until"]):
                continue
            if "last_raid_tick" not in lair:
                # 新匪寨：从当前 tick 起算，避免「生成即劫掠」
                lair["bandit"] = True
                if in_bandits:
                    lair["source"] = "bandit_data"
                lair["last_raid_tick"] = tick_no
                node["lair"] = lair
                await self.save_node(node)
                continue
            lair.setdefault("bandit", True)
            if in_bandits and not lair.get("source"):
                lair["source"] = "bandit_data"
            if not R.bandit_raid_ready({"lair": lair}, tick_no, interval):
                continue
            targets = []
            for adj in node.get("adj") or []:
                t = nodes.get(int(adj))
                if not t or int(t.get("owner_faction", 0) or 0) <= 0:
                    continue
                fid = int(t["owner_faction"])
                if fid not in kind_cache:
                    f = await self.get_faction(fid)
                    kind_cache[fid] = str((f or {}).get("kind", ""))
                if kind_cache[fid] != "player":
                    continue
                targets.append(t)
            if not targets:
                continue
            # 优先劫掠存粮最多的据点，每轮最多 2 处
            targets.sort(key=lambda t: -int((t.get("stock") or {}).get("grain", 0) or 0))
            for t in targets[:2]:
                stock = t.get("stock") or {}
                lost: dict[str, int] = {}
                for k, v in list(stock.items()):
                    loss = int(int(v or 0) * stock_pct)
                    if loss > 0:
                        lost[k] = loss
                        stock[k] = max(0, int(v or 0) - loss)
                if lost:
                    t["stock"] = stock
                new_morale = await self._bump_morale(t, morale_delta, "bandit_raid")
                t["morale"] = new_morale
                await self.save_node(t)
                fid = int(t["owner_faction"])
                rows = await self._fetchall(
                    "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
                    (world["id"], fid))
                loss_txt = "、".join(
                    f"{R.RESOURCE_NAMES.get(k, k)}×{v}"
                    for k, v in lost.items()) or "无"
                await self._notify(
                    world, [r["user_id"] for r in rows], "bandit_raid",
                    {"node_id": int(t["id"]),
                     "text": f"匪寨「{node['name']}」劫掠「{t['name']}」："
                             f"损失 {loss_txt}，民心 {morale_delta}"
                             f"（现 {new_morale}）"})
                raided += 1
            lair["last_raid_tick"] = tick_no
            node["lair"] = lair
            await self.save_node(node)
        if raided:
            await self._commit()
        return raided

    # ------------------------------------------------------------------
    # 会武分档报名（§11）/ 名将堂·英烈祠（§11）
    # ------------------------------------------------------------------
    def arena_reward_mult(self, node: Optional[dict] = None) -> float:
        """会武奖励乘区：战鼓楼 duel.drum_tower_mult × 擂将台（arena）加成。"""
        mult = float((self.main_cfg().get("duel") or {}).get(
            "drum_tower_mult", 2.0) or 2.0)
        if node:
            mult *= 1.0 + 0.10 * min(5, self._building_level(node, "arena"))
        return mult

    async def arena_register(self, user_id: str, bracket: str) -> dict:
        """会武按战功分档报名（novice/veteran/master，不可跨档）。"""
        world, p = await self._require_player(user_id)
        dcfg = self.main_cfg().get("duel") or {}
        my_bracket = R.arena_bracket(int(p.get("merit", 0) or 0))
        bracket = str(bracket or "").strip() or my_bracket
        if bracket not in ("novice", "veteran", "master"):
            raise StrategyError(
                "会武档位无效（"
                + "/".join(R.ARENA_BRACKET_NAMES.values()) + "）")
        if bracket != my_bracket:
            raise StrategyError(
                f"档位不符：你当前为「{R.ARENA_BRACKET_NAMES.get(my_bracket, my_bracket)}」"
                f"档，不可跨档报名")
        fee = int(dcfg.get("arena_stake_min", 100))
        if int(p.get("coin", 0)) < fee:
            raise StrategyError(f"铜钱不足：报名费 {fee}")
        data = world.get("data") or {}
        arena = data.setdefault("duel_arena", {})
        season_no = int(world.get("season_no", 1) or 1)
        key = f"arenaS{season_no}_{bracket}_{user_id}"
        now = _now()
        # 清理过期/已结算报名，避免 world.data 膨胀
        for k in [k for k, v in arena.items()
                  if str(v.get("state")) in ("signup", "done")
                  and now > int(v.get("expire_at", 0) or 0)]:
            arena.pop(k, None)
        if str((arena.get(key) or {}).get("state")) == "signup":
            raise StrategyError("已报名本档会武，请等待开赛")
        f = await self.get_faction(int(p["faction_id"]))
        cap_node = None
        if f and int(f.get("capital_node_id") or 0):
            cap_node = await self.get_node(world["id"], int(f["capital_node_id"]))
        reward_mult = self.arena_reward_mult(cap_node)
        p["coin"] = int(p.get("coin", 0)) - fee
        arena[key] = {"challenger": user_id, "char_id": 0, "char_name": "",
                      "stake": fee, "state": "signup", "bracket": bracket,
                      "merit": int(p.get("merit", 0) or 0),
                      "reward_mult": reward_mult,
                      "created_at": now, "expire_at": now + 7 * 86400}
        data["duel_arena"] = arena
        world["data"] = data
        await self.save_world(world)
        await self.save_player(p)
        await self._commit()
        await self._audit(user_id, "arena_register", {
            "bracket": bracket, "fee": fee, "reward_mult": reward_mult})
        return {"ok": True, "bracket": bracket, "fee": fee,
                "reward_mult": reward_mult, "signup_id": key,
                "msg": f"🥁 已报名「{R.ARENA_BRACKET_NAMES.get(bracket, bracket)}」"
                       f"档会武（报名费 {fee}，奖励 ×{reward_mult:g}）"}

    async def get_memorials(self, world_id: int) -> dict:
        """名将堂 / 英烈祠（§11）：按赛季倒序，最多各 20 条。

        赛季结算会创建新世界（新 world_id），历史殿堂若按当前 world_id
        过滤会在每个新赛季恒为空，因此这里跨世界读取全部赛季记录。
        """
        rows = await self._fetchall(
            "SELECT * FROM strategy_memorials "
            "ORDER BY season_no DESC, id DESC LIMIT 120")
        hall: list[dict] = []
        shrine: list[dict] = []
        season_no = 0
        for r in rows:
            season = int(r.get("season_no", 1) or 1)
            season_no = max(season_no, season)
            snap = r.get("snapshot_json")
            if isinstance(snap, str):
                try:
                    snap = json.loads(snap or "{}")
                except (ValueError, TypeError):
                    snap = {}
            if not isinstance(snap, dict):
                snap = {}
            kind = str(r.get("kind") or "")
            item = {"name": r.get("char_name") or snap.get("name")
                    or r.get("user_id") or "",
                    "rarity": snap.get("rarity", ""),
                    "faction": r.get("faction_name", ""),
                    "season": season,
                    "merit": int(snap.get("merit_season", snap.get("merit", 0)) or 0),
                    "achievements": snap.get("achievements") or [],
                    "kind": kind}
            if kind == "fame":
                # 阵亡/名将陈列 → 英烈祠
                shrine.append(item)
            else:
                # hall_of_fame / season_summary → 名将堂
                hall.append(item)
        world = await self.get_world()
        if world and int(world.get("id", 0) or 0) == int(world_id):
            season_no = max(season_no, int(world.get("season_no", 0) or 0))
        return {"hall": hall[:20], "shrine": shrine[:20],
                "season_no": season_no or 1}

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
        """个人产出加成：追赶礼包（14 天 ×1.5）与荣誉商店开局加成取较高者。"""
        ccfg = self.main_cfg().get("catchup") or {}
        mult = 1.0
        until = int(((p.get("newcomer") or {}).get("catchup_boost_until", 0)))
        if until > _now():
            mult = max(mult, float(ccfg.get("output_mult", 1.5)))
        boost = (p.get("honor") or {}).get("boost") or {}
        if int(boost.get("until", 0) or 0) > _now():
            mult = max(mult, float(boost.get("mult", 1.2) or 1.2))
        return mult

    async def _settle_season(self, world: dict) -> dict:
        """赛季结算：霸权排名 / 荣誉发放 / 英烈祠名将堂 / 老兵结转 / 开新世界。"""
        scfg = self.main_cfg().get("season") or {}
        season_no = int(world.get("season_no", 1))
        # C3：结算幂等守卫——先占位写入本赛季存档行，再执行发奖。
        # 此前是「先逐人发荣誉、最后才写守卫行」：中途异常重试会把荣誉
        # 再发一遍；且 autocommit 下无法整笔回滚。现在以
        # data_json.state == "settling" 标记进行中，重试时续做并靠
        # honor.last_season + 先清后插战报实现逐项幂等。
        done = await self._fetchone(
            "SELECT * FROM strategy_season_results WHERE world_id=? AND season_no=?",
            (int(world["id"]), season_no))
        if done:
            try:
                prev = json.loads(done.get("data_json") or "{}")
            except (TypeError, ValueError):
                prev = {}
            if not isinstance(prev, dict) or prev.get("state") != "settling":
                raise StrategyError(f"赛季 S{season_no} 已结算过，请勿重复触发")
        else:
            await self._execute("""
                INSERT INTO strategy_season_results (world_id, season_no, ended_at,
                    faction_scores_json, top_players_json, data_json)
                VALUES (?,?,?, '[]', '[]', ?)
            """, (int(world["id"]), season_no, _now(),
                  json.dumps({"state": "settling"}, ensure_ascii=False)))
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
            # 续做保护：同一赛季已发过荣誉的玩家直接跳过（逐人幂等）
            last = honor.get("last_season") or {}
            if int((last or {}).get("season_no", 0) or 0) == season_no:
                continue
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
        # 英烈祠 / 名将堂（续做时先清本赛季旧记录，避免重复陈列）
        await self._execute(
            "DELETE FROM strategy_memorials WHERE world_id=? AND season_no=?",
            (int(world["id"]), season_no))
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
        # 赛季存档：更新先前占位的守卫行（不再重复 INSERT）
        final_data = json.dumps({"carryover": carryover,
                                 "coalition": (world.get("data") or {}).get("coalition")},
                                ensure_ascii=False)
        upd = await self._exec_count("""
            UPDATE strategy_season_results
            SET ended_at=?, faction_scores_json=?, top_players_json=?, data_json=?
            WHERE world_id=? AND season_no=?
        """, (_now(), json.dumps(scores, ensure_ascii=False),
              json.dumps(top_players, ensure_ascii=False), final_data,
              int(world["id"]), season_no))
        if upd != 1:
            await self._execute("""
                INSERT INTO strategy_season_results (world_id, season_no, ended_at,
                    faction_scores_json, top_players_json, data_json)
                VALUES (?,?,?,?,?,?)
            """, (int(world["id"]), season_no, _now(),
                  json.dumps(scores, ensure_ascii=False),
                  json.dumps(top_players, ensure_ascii=False), final_data))
        world["state"] = "ended"
        await self.save_world(world)
        await self._commit()
        logger.info(f"[strategy] 赛季 S{season_no} 结算完成：霸主「{champion}」，"
                    f"结转 {len(carryover)} 人")
        new_world = await self._create_world(season_no=season_no + 1)
        return {"ok": True, "season_no": season_no, "champion": champion,
                "scores": scores[:5], "top_players": top_players,
                "new_world_id": new_world["id"]}

    @staticmethod
    def _season_scores_view(raw) -> list[dict]:
        """赛季历史积分兼容：存档字段是 hegemony_pct，前端旧版读 score。"""
        try:
            scores = json.loads(raw or "[]") if isinstance(raw, str) else (raw or [])
        except (TypeError, ValueError):
            scores = []
        out: list[dict] = []
        for s in (scores or [])[:3]:
            if not isinstance(s, dict):
                continue
            item = dict(s)
            if "score" not in item:
                item["score"] = item.get("hegemony_pct", 0)
            out.append(item)
        return out

    async def get_season_view(self, world: dict) -> dict:
        data = world.get("data") or {}
        rows = await self._fetchall(
            "SELECT season_no, faction_scores_json, top_players_json, ended_at "
            "FROM strategy_season_results ORDER BY season_no DESC LIMIT 3")
        history = [{"season_no": r["season_no"], "ended_at": r["ended_at"],
                    "scores": self._season_scores_view(r.get("faction_scores_json")),
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
        # 引荐奖励：引荐人必须真实存在，且发放走 _settle_merit 的
        # 观察期/多开嫌疑门禁（此前直接加 merit，可被小号刷战功）。
        ref = str(newcomer.get("referred_by") or "")
        if ref and ref != user_id:
            rp = await self.get_player(ref, world["id"])
            if rp:
                bonus = int(ccfg.get("referral_merit", 200))
                gain = await self._settle_merit(ref, bonus, "referral")
                if gain > 0:
                    rp["merit"] = int(rp.get("merit", 0)) + gain
                    rp["merit_season"] = int(rp.get("merit_season", 0)) + gain
                    await self.save_player(rp)
        await self._commit()
        return {"ok": True,
                "msg": f"📦 追赶礼包已发放（×{mult:.2f}）："
                       + "、".join(
                           f"{R.RESOURCE_NAMES.get(k, k)}+{v}"
                           for k, v in pack.items()
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
                rar_weights = eff.get("rarity_weights")
                if isinstance(rar_weights, dict) and rar_weights:
                    rar_keys = list(rar_weights.keys())
                    rar_w = [max(0, int(v)) for v in rar_weights.values()]
                else:
                    rar_keys = [str(eff.get("rarity", "epic"))]
                    rar_w = [1]
                acfg = self._auction_cfg()
                cands = await self._auction_nodes(world)
                row = await self._fetchone(
                    "SELECT COUNT(*) AS n FROM strategy_char_auctions "
                    "WHERE world_id=? AND status='open'", (world["id"],))
                open_n = int((row or {}).get("n", 0) or 0)
                cap = int(acfg.get("max_open_per_world", 6))
                for i in range(count):
                    if not cands or open_n >= cap:
                        break
                    node = rng.choice(cands)
                    rarity = rng.choices(rar_keys, weights=rar_w)[0]
                    c = R.generate_character(rng, rarity=rarity,
                                             role=R.ROLE_GENERAL,
                                             name_pools=self.cfg.get("names") or {})
                    c.update({"world_id": world["id"], "faction_id": 0,
                              "owner_user_id": "", "loyalty": 50,
                              "status": "wander", "auction_cooldown_until": 0,
                              "location": {"node_id": int(node["id"]),
                                           "origin": "auction"}})
                    await self.insert_char(c)
                    await self._open_auction(world, c, int(node["id"]))
                    open_n += 1
                    logger.info(f"[strategy] 流将 {c['name']}（{rarity}）现身"
                                f"「{node['name']}」，进入坊市战功暗标")
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
                    # 有主据点只吃「库存损失」，不得写入 npc_garrison /
                    # bandits：否则会给受害方凭空加守军，且视图展示为可讨伐
                    # 但 raid_bandit 要求 owner_faction==0，玩家点了必失败。
                    if int(node.get("owner_faction", 0) or 0) <= 0:
                        node["npc_garrison"] = {
                            "troops": rng.randint(int(lo), int(hi)), "morale": 90}
                        bandits[str(node["id"])] = {
                            "spawned_at": _now(), "origin": "event",
                            "until": _now() + int(eff.get("hours", 24)) * 3600}
                    await self.save_node(node)
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

    # ------------------------------------------------------------------
    # 坊市流将战功暗标（§11「天降流将」：极品/传世流亡角色，密封竞价）
    # ------------------------------------------------------------------
    def _auction_cfg(self) -> dict:
        return self.main_cfg().get("wanderer_auction") or {}

    async def _auction_nodes(self, world: dict) -> list[dict]:
        """流将挂拍地点：优先坊市/仙城，其次无主据点，最后全图。"""
        nodes = await self.get_nodes(world["id"])
        markets = [n for n in nodes if n["type"] in ("market", "city")]
        ownerless = [n for n in nodes if int(n["owner_faction"]) == 0]
        return markets or ownerless or nodes

    def _season_progress(self, world: dict) -> float:
        days = max(1, int(self.main_cfg().get("season_days", 75)))
        return max(0.0, min(1.0, float(self._season_day(world)) / days))

    async def _world_merit_mult(self, world: dict) -> float:
        """世界战功水位系数：战功中位数越高，流将底价越贵。"""
        acfg = self._auction_cfg()
        ref = max(1.0, float(acfg.get("world_merit_ref", 2000) or 2000))
        cap = max(1.0, float(acfg.get("world_merit_mult_cap", 2.0) or 2.0))
        rows = await self._fetchall(
            "SELECT merit_season FROM strategy_players "
            "WHERE world_id=? AND merit_season>0", (world["id"],))
        vals = sorted(int(r["merit_season"] or 0) for r in rows)
        if not vals:
            return 1.0
        median = vals[len(vals) // 2]
        return max(1.0, min(cap, float(median) / ref))

    async def _auction_access(self, world: dict, p: Optional[dict], row: dict,
                              now: int) -> dict:
        """坊市地缘准入：控制方及盟友在优先期可出价，之后向全服开放。"""
        acfg = self._auction_cfg()
        node = await self.get_node(world["id"], int(row["node_id"]))
        controller = int((node or {}).get("owner_faction", 0) or 0)
        out = {"allowed": True, "access": "open", "reason": "",
               "controller_faction": controller, "controller_name": "无主",
               "priority_until": 0}
        if controller <= 0 or not bool(acfg.get("market_binding", True)):
            return out
        cf = await self.get_faction(controller)
        out["controller_name"] = (cf or {}).get("name", "?")
        priority_until = int(row.get("created_at", 0)) + int(
            float(acfg.get("controller_priority_hours", 2) or 0) * 3600)
        out["priority_until"] = priority_until
        if not p:
            out.update({"allowed": False, "reason": "尚未加入仙域争霸"})
            return out
        my_fid = int(p.get("faction_id", 0) or 0)
        my_faction = await self.get_faction(my_fid) if my_fid else None
        ally_ids = await self._alliance_ids(world["id"], my_faction) \
            if my_faction else {my_fid}
        if controller in ally_ids:
            out["access"] = "controller"
            return out
        if now < priority_until:
            left = max(1, priority_until - now)
            hours = max(1, (left + 3599) // 3600)
            out.update({
                "allowed": False, "access": "priority",
                "reason": f"「{out['controller_name']}」优先竞价中，约 {hours} 小时后全服开放"})
            return out
        out["access"] = "open"
        return out

    async def _auction_quota_used(self, world: dict, user_id: str,
                                  rarity: str) -> int:
        if not user_id:
            return 0
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM strategy_char_auctions "
            "WHERE world_id=? AND season_no=? AND status='settled' "
            "AND settled_user=? AND rarity=?",
            (world["id"], int(world.get("season_no", 1) or 1),
             str(user_id), str(rarity)))
        return int((row or {}).get("n", 0) or 0)

    async def _auction_pending_wins(self, world: dict, user_id: str,
                                    rarity: str,
                                    exclude_auction_id: int = 0) -> int:
        """统计玩家当前处于最高暗标的其它拍卖（防一人同时锁定多个名额）。"""
        if not user_id:
            return 0
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM strategy_char_auctions a "
            "WHERE a.world_id=? AND a.season_no=? AND a.status='open' "
            "AND a.rarity=? AND a.id<>? AND EXISTS ("
            "  SELECT 1 FROM strategy_auction_bids b "
            "  WHERE b.auction_id=a.id AND b.user_id=? AND b.status='active' "
            "    AND b.amount=(SELECT MAX(b2.amount) FROM strategy_auction_bids b2 "
            "                 WHERE b2.auction_id=a.id AND b2.status='active'))",
            (world["id"], int(world.get("season_no", 1) or 1), str(rarity),
             int(exclude_auction_id or 0), str(user_id)))
        return int((row or {}).get("n", 0) or 0)

    async def _auction_quota_left(self, world: dict, user_id: str,
                                  rarity: str, exclude_auction_id: int = 0) -> int:
        acfg = self._auction_cfg()
        limit = R.wanderer_quota_limit(acfg, rarity)
        if limit <= 0:
            return 999
        used = await self._auction_quota_used(world, user_id, rarity)
        pending = await self._auction_pending_wins(
            world, user_id, rarity, exclude_auction_id)
        return max(0, limit - used - pending)

    async def _refresh_auction_aggregate(self, auction_id: int) -> None:
        agg = await self._fetchone(
            "SELECT COUNT(*) AS n, MAX(amount) AS mx "
            "FROM strategy_auction_bids WHERE auction_id=? AND status='active'",
            (int(auction_id),)) or {}
        top = await self._fetchone(
            "SELECT user_id FROM strategy_auction_bids "
            "WHERE auction_id=? AND status='active' "
            "ORDER BY amount DESC, created_at ASC, id ASC LIMIT 1",
            (int(auction_id),)) or {}
        await self._execute(
            "UPDATE strategy_char_auctions SET bid_count=?, current_price=?, "
            "bidder_user=? WHERE id=?",
            (int(agg.get("n", 0) or 0), int(agg.get("mx", 0) or 0),
             str(top.get("user_id", "") or ""), int(auction_id)))

    async def _refund_auction_bid(self, world: dict, bid: dict,
                                  status: str) -> None:
        uid = str(bid.get("user_id", "") or "")
        amount = int(bid.get("amount", 0) or 0)
        if uid and amount > 0:
            p = await self.get_player(uid, world["id"])
            if p:
                p["merit"] = int(p.get("merit", 0) or 0) + amount
                await self.save_player(p)
        await self._execute(
            "UPDATE strategy_auction_bids SET status=?, updated_at=? WHERE id=?",
            (str(status), _now(), int(bid["id"])))

    async def _open_auction(self, world: dict, c: dict, node_id: int) -> int:
        """把一名在野流将挂上坊市暗标，返回拍卖 id。"""
        acfg = self._auction_cfg()
        start = R.wanderer_auction_price(
            c, acfg, self._season_progress(world),
            await self._world_merit_mult(world))
        now = _now()
        return await self._execute("""
            INSERT INTO strategy_char_auctions (world_id, node_id, char_id,
                start_price, current_price, buyout_price, bidder_user, bid_count,
                status, created_at, expire_at, currency, sealed, season_no, rarity)
            VALUES (?,?,?,?,0,0,'',0,'open',?,?,'merit',1,?,?)
        """, (int(world["id"]), int(node_id), int(c["id"]), int(start),
              now, now + int(acfg.get("duration_hours", 12)) * 3600,
              int(world.get("season_no", 1) or 1),
              str(c.get("rarity", "common") or "common")))

    async def get_wanderer_auctions_view(self, world: dict,
                                         user_id: str = "") -> list[dict]:
        """当前开放的流将暗标（不泄露他人出价，仅返回人数与自己的暗标）。"""
        acfg = self._auction_cfg()
        now = _now()
        rows = await self._fetchall(
            "SELECT * FROM strategy_char_auctions WHERE world_id=? "
            "AND status='open' AND expire_at>? ORDER BY expire_at ASC",
            (world["id"], now))
        if not rows:
            return []
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        chars = {c["id"]: c for c in await self.get_chars(
            world_id=world["id"], status="wander")}
        p = await self.get_player(user_id, world["id"]) if user_id else None
        cap_info = await self._char_cap_info(world["id"], p) if p else None
        owned = await self._roster_count(user_id, world["id"]) if p else 0
        out = []
        for r in rows:
            c = chars.get(int(r["char_id"]))
            if not c:
                continue
            rarity = str(r.get("rarity") or c.get("rarity") or "common")
            my_bid_row = await self._fetchone(
                "SELECT * FROM strategy_auction_bids WHERE auction_id=? "
                "AND user_id=? AND status='active'",
                (int(r["id"]), str(user_id))) if user_id else None
            my_bid = int((my_bid_row or {}).get("amount", 0) or 0)
            bidder_count = int((await self._fetchone(
                "SELECT COUNT(*) AS n FROM strategy_auction_bids "
                "WHERE auction_id=? AND status='active'",
                (int(r["id"]),)) or {}).get("n", 0) or 0)
            access = await self._auction_access(world, p, r, now)
            quota_limit = R.wanderer_quota_limit(acfg, rarity)
            quota_used = await self._auction_quota_used(world, user_id, rarity)
            quota_left = await self._auction_quota_left(
                world, user_id, rarity, int(r["id"]))
            node = nodes.get(int(r["node_id"])) or {}
            block = ""
            if not p:
                block = "尚未加入仙域争霸"
            elif not access.get("allowed"):
                block = str(access.get("reason") or "当前不可出价")
            elif quota_limit > 0 and quota_left <= 0:
                block = f"本赛季{R.RARITY_NAMES.get(rarity, rarity)}名额已用完"
            elif cap_info and owned >= int(cap_info["cap"]):
                block = (f"角色数量已达上限（{owned}/{cap_info['cap']}）"
                         f"：扩建据点可增加名额")
            out.append({
                "id": int(r["id"]), "char_id": int(c["id"]),
                "name": c["name"], "rarity": rarity,
                "rarity_name": R.RARITY_NAMES.get(rarity, rarity),
                "role": c.get("role", ""),
                "role_name": R.ROLE_NAMES.get(c.get("role", ""), ""),
                "profession": c.get("profession", ""),
                "profession_name": R.PROFESSION_NAMES.get(
                    c.get("profession", ""), ""),
                "stats": c.get("stats") or {},
                "traits": c.get("traits") or [],
                "trait_names": [R.TRAIT_NAMES.get(t, t)
                                for t in (c.get("traits") or [])],
                "personality": c.get("personality") or {},
                "personality_top_name": R.TOP_PERSONALITY_NAMES.get(
                    str((c.get("personality") or {}).get("top") or ""), ""),
                "personality_neg_name": R.NEGATIVE_PERSONALITY_NAMES.get(
                    str((c.get("personality") or {}).get("negative") or ""), ""),
                "node_id": int(r["node_id"]),
                "node_name": node.get("name", ""),
                "controller_faction": int(access.get("controller_faction", 0) or 0),
                "controller_name": access.get("controller_name", "无主"),
                "access": access.get("access", "open"),
                "access_reason": access.get("reason", ""),
                "priority_until": int(access.get("priority_until", 0) or 0),
                "currency": "merit",
                "sealed": True,
                "start_price": int(r["start_price"]),
                "min_next": R.wanderer_min_bid(
                    int(r["start_price"]), 0, False, acfg, own_bid=my_bid),
                "my_bid": my_bid,
                "has_my_bid": my_bid > 0,
                "bidder_count": bidder_count,
                "bidder_label": "你已出价" if my_bid > 0 else (
                    f"{bidder_count} 人已出价" if bidder_count else ""),
                "can_bid": bool(p) and not block,
                "block_reason": block,
                "quota_limit": quota_limit,
                "quota_used": quota_used,
                "quota_left": quota_left,
                # 兼容旧前端字段：暗标不再公开当前价/一口价
                "current_price": 0,
                "buyout_price": 0,
                "created_at": int(r["created_at"]),
                "expire_at": int(r["expire_at"]),
            })
        return out

    async def bid_wanderer(self, user_id: str, auction_id: int,
                           amount: int = 0, buyout: bool = False) -> dict:
        """坊市流将暗标出价（战功即时托管，仅自己可见，到期统一开标）。"""
        if buyout:
            raise StrategyError("一口价已取消，请使用暗标出价")
        async with self._market_lock:
            world, p = await self._require_player(user_id)
            acfg = self._auction_cfg()
            row = await self._fetchone(
                "SELECT * FROM strategy_char_auctions WHERE id=? AND world_id=?",
                (int(auction_id), world["id"]))
            if not row or row["status"] != "open":
                raise StrategyError("拍卖不存在或已结束")
            now = _now()
            if int(row["expire_at"]) <= now:
                raise StrategyError("拍卖已结束，等待开标")
            c = await self.get_char(int(row["char_id"]))
            if not c or int(c.get("world_id", 0) or 0) != int(world["id"]) \
                    or c.get("status") != "wander":
                raise StrategyError("该流将已离去")
            access = await self._auction_access(world, p, row, now)
            if not access.get("allowed"):
                raise StrategyError(str(access.get("reason") or "当前不可出价"))
            rarity = str(row.get("rarity") or c.get("rarity") or "common")
            quota_left = await self._auction_quota_left(
                world, user_id, rarity, int(row["id"]))
            limit = R.wanderer_quota_limit(acfg, rarity)
            if limit > 0 and quota_left <= 0:
                used = await self._auction_quota_used(world, user_id, rarity)
                raise StrategyError(
                    f"本赛季{R.RARITY_NAMES.get(rarity, rarity)}竞得名额"
                    f"已用完（{used}/{limit}）")
            cap_info = await self._char_cap_info(world["id"], p)
            cap = int(cap_info["cap"])
            owned = await self._roster_count(user_id, world["id"])
            if owned >= cap:
                raise StrategyError(
                    f"角色数量已达上限（{owned}/{cap}）"
                    f"：当前据点 {cap_info['nodes']} 座，扩建据点可增加名额")
            price = int(amount or 0)
            start = int(row["start_price"] or 0)
            if price < start:
                raise StrategyError(f"暗标不能低于底价 {start} 战功")
            old = await self._fetchone(
                "SELECT * FROM strategy_auction_bids WHERE auction_id=? "
                "AND user_id=? AND status='active'",
                (int(row["id"]), user_id))
            old_amount = int((old or {}).get("amount", 0) or 0)
            if price == old_amount:
                raise StrategyError("暗标金额未变化")
            delta = price - old_amount
            if delta > 0 and int(p.get("merit", 0) or 0) < delta:
                raise StrategyError(
                    f"战功不足：当前 {int(p.get('merit', 0) or 0)}，"
                    f"需补 {delta}")
            p["merit"] = int(p.get("merit", 0) or 0) - delta
            await self.save_player(p)
            if old:
                await self._execute(
                    "UPDATE strategy_auction_bids SET amount=?, updated_at=?, "
                    "status='active' WHERE id=?",
                    (price, now, int(old["id"])))
            else:
                await self._execute("""
                    INSERT INTO strategy_auction_bids (auction_id, world_id,
                        user_id, amount, status, created_at, updated_at)
                    VALUES (?,?,?,?,'active',?,?)
                """, (int(row["id"]), int(world["id"]), user_id, price,
                      now, now))
            await self._refresh_auction_aggregate(int(row["id"]))
            # 防狙击：最后 anti_snipe_sec 内有人提交/改价则顺延（总延时有上限）
            new_expire = int(row["expire_at"])
            anti = int(acfg.get("anti_snipe_sec", 300))
            max_ext = int(acfg.get("max_extend_sec", 600))
            if anti > 0 and new_expire - now <= anti:
                limit_ts = int(row["created_at"]) + \
                    int(acfg.get("duration_hours", 12)) * 3600 + max_ext
                new_expire = min(max(new_expire + anti, now + anti), limit_ts)
                if new_expire <= now:
                    new_expire = now + anti
                await self._execute(
                    "UPDATE strategy_char_auctions SET expire_at=? "
                    "WHERE id=? AND status='open'",
                    (new_expire, int(row["id"])))
            await self._commit()
            word = "改价" if old else "提交"
            return {"ok": True,
                    "amount": price,
                    "msg": f"🔒 暗标已{word}：{price} 战功"
                           f"（仅你可见，到期统一开标；被超价不退还，落标全额退还）"}

    async def cancel_wanderer_bid(self, user_id: str,
                                  auction_id: int) -> dict:
        """撤回自己的暗标，托管战功全额退还。"""
        async with self._market_lock:
            world, p = await self._require_player(user_id)
            row = await self._fetchone(
                "SELECT * FROM strategy_char_auctions WHERE id=? AND world_id=?",
                (int(auction_id), world["id"]))
            if not row or row["status"] != "open":
                raise StrategyError("拍卖不存在或已结束")
            if int(row["expire_at"]) <= _now():
                raise StrategyError("拍卖已结束，等待开标")
            bid = await self._fetchone(
                "SELECT * FROM strategy_auction_bids WHERE auction_id=? "
                "AND user_id=? AND status='active'",
                (int(row["id"]), user_id))
            if not bid:
                raise StrategyError("你没有进行中的暗标")
            await self._refund_auction_bid(world, bid, "cancelled")
            await self._refresh_auction_aggregate(int(row["id"]))
            await self._commit()
            return {"ok": True,
                    "msg": f"↩️ 已撤回暗标，退还 {int(bid['amount'])} 战功"}

    async def _expire_wanderer_auction(self, world: dict, row: dict,
                                       c: Optional[dict], status: str = "expired") -> None:
        """流拍/无人可交付：退还全部暗标，流将进入游历冷却，不删除角色。"""
        now = _now()
        bids = await self._fetchall(
            "SELECT * FROM strategy_auction_bids "
            "WHERE auction_id=? AND status='active'", (int(row["id"]),))
        for bid in bids:
            await self._refund_auction_bid(world, bid, "lost")
        if c and c.get("status") == "wander" and not c.get("owner_user_id"):
            acfg = self._auction_cfg()
            c["auction_cooldown_until"] = now + int(
                float(acfg.get("roam_days", 7) or 0) * 86400)
            nodes = await self._auction_nodes(world)
            cur_id = int((c.get("location") or {}).get("node_id", 0) or 0)
            choices = [n for n in nodes if int(n["id"]) != cur_id] or nodes
            if choices:
                node = choices[now % len(choices)]
                c["location"] = {"node_id": int(node["id"]),
                                 "origin": "auction_roam"}
            await self.save_char(c)
        await self._execute(
            "UPDATE strategy_char_auctions SET status=?, settled_at=?, "
            "bidder_user='', current_price=0 WHERE id=?",
            (str(status), now, int(row["id"])))

    async def _settle_wanderer_auction(self, world: dict, row: dict,
                                       resume: bool = False) -> bool:
        """到期开标：最高暗标成交；名额/名册不足则顺延给下一位。

        ``resume=True`` 用于续做上次异常中断、停留在 ``settling`` 的拍卖：
        跳过 open→settling 的 CAS，直接按现有暗标重新结算（结算本身
        幂等：成交/退款/状态更新均按条件写入）。
        """
        now = _now()
        if not resume:
            n = await self._exec_count(
                "UPDATE strategy_char_auctions SET status='settling' "
                "WHERE id=? AND status='open'", (int(row["id"]),))
            if n != 1:
                return False
        acfg = self._auction_cfg()
        c = await self.get_char(int(row["char_id"]))
        # v4.3.0 resume 防双得利：角色已交割给赢家（或 bid 已 won）时，
        # 只补完拍卖行状态，绝不再把赢家暗标当 invalid 退款。
        if resume:
            won = await self._fetchone(
                "SELECT * FROM strategy_auction_bids WHERE auction_id=? "
                "AND status='won' LIMIT 1", (int(row["id"]),))
            if won:
                uid = str(won.get("user_id", "") or "")
                price = int(won.get("amount", 0) or 0)
                await self._execute(
                    "UPDATE strategy_char_auctions SET status='settled', "
                    "settled_at=?, settled_user=?, settled_price=?, "
                    "current_price=?, bidder_user=? WHERE id=? AND status='settling'",
                    (now, uid, price, price, uid, int(row["id"])))
                others = await self._fetchall(
                    "SELECT * FROM strategy_auction_bids WHERE auction_id=? "
                    "AND status='active'", (int(row["id"]),))
                for other in others:
                    await self._refund_auction_bid(world, other, "lost")
                return True
            if c and c.get("owner_user_id") and c.get("status") != "wander":
                owner = str(c.get("owner_user_id") or "")
                owner_bid = await self._fetchone(
                    "SELECT * FROM strategy_auction_bids WHERE auction_id=? "
                    "AND user_id=? AND status IN ('active','won') "
                    "ORDER BY CASE status WHEN 'won' THEN 0 ELSE 1 END, "
                    "amount DESC LIMIT 1",
                    (int(row["id"]), owner))
                if owner_bid:
                    price = int(owner_bid.get("amount", 0) or 0)
                    if owner_bid.get("status") != "won":
                        await self._execute(
                            "UPDATE strategy_auction_bids SET status='won', "
                            "updated_at=? WHERE id=?",
                            (now, int(owner_bid["id"])))
                    await self._execute(
                        "UPDATE strategy_char_auctions SET status='settled', "
                        "settled_at=?, settled_user=?, settled_price=?, "
                        "current_price=?, bidder_user=? "
                        "WHERE id=? AND status='settling'",
                        (now, owner, price, price, owner, int(row["id"])))
                    others = await self._fetchall(
                        "SELECT * FROM strategy_auction_bids WHERE auction_id=? "
                        "AND status='active'", (int(row["id"]),))
                    for other in others:
                        await self._refund_auction_bid(world, other, "lost")
                    return True
        bids = await self._fetchall(
            "SELECT * FROM strategy_auction_bids WHERE auction_id=? "
            "AND status='active' ORDER BY amount DESC, created_at ASC, id ASC",
            (int(row["id"]),))
        if not bids:
            await self._expire_wanderer_auction(world, row, c, "expired")
            return False
        rarity = str((row or {}).get("rarity") or
                     (c or {}).get("rarity") or "common")
        limit = R.wanderer_quota_limit(acfg, rarity)
        for bid in bids:
            uid = str(bid.get("user_id", "") or "")
            price = int(bid.get("amount", 0) or 0)
            p = await self.get_player(uid, world["id"]) if uid else None
            ok = bool(c) and int(c.get("world_id", 0) or 0) == int(world["id"]) \
                and c.get("status") == "wander" and not c.get("owner_user_id") \
                and bool(p)
            if ok and p:
                cap_info = await self._char_cap_info(world["id"], p)
                owned = await self._roster_count(uid, world["id"])
                if owned >= int(cap_info["cap"]):
                    ok = False
            if ok:
                used = await self._auction_quota_used(world, uid, rarity)
                if limit > 0 and used >= limit:
                    ok = False
            if not ok:
                await self._refund_auction_bid(world, bid, "invalid")
                continue
            c["status"] = "idle"
            c["owner_user_id"] = uid
            c["faction_id"] = int(p["faction_id"])
            c["loyalty"] = 60
            c["auction_cooldown_until"] = 0
            await self.save_char(c)
            await self._execute(
                "UPDATE strategy_auction_bids SET status='won', updated_at=? "
                "WHERE id=?", (now, int(bid["id"])))
            # 其余暗标全额退还
            others = await self._fetchall(
                "SELECT * FROM strategy_auction_bids WHERE auction_id=? "
                "AND status='active'", (int(row["id"]),))
            for other in others:
                await self._refund_auction_bid(world, other, "lost")
            await self._execute(
                "UPDATE strategy_char_auctions SET status='settled', "
                "settled_at=?, settled_user=?, settled_price=?, "
                "current_price=?, bidder_user=? WHERE id=?",
                (now, uid, price, price, uid, int(row["id"])))
            logger.info(f"[strategy] 流将 {c['name']} 以 {price} 战功被 {uid} 竞得")
            await self._notify(
                world, [uid], "wanderer_auction",
                {"auction_id": int(row["id"]), "char_id": int(c["id"]),
                 "text": f"🏆 你以 {price} 战功竞得流将「{c['name']}」"})
            return True
        # 所有暗标都因名额/名册失效：流将转入游历
        await self._expire_wanderer_auction(world, row, c, "failed")
        return False

    async def _step_wanderer_auctions(self, world: dict) -> None:
        """tick：到期开标；把冷却结束的在野流将补挂到坊市。"""
        acfg = self._auction_cfg()
        now = _now()
        rows = await self._fetchall(
            "SELECT * FROM strategy_char_auctions WHERE world_id=? "
            "AND (status='settling' OR (status='open' AND expire_at<=?))",
            (world["id"], now))
        for r in rows:
            await self._settle_wanderer_auction(
                world, r, resume=(str(r["status"]) == "settling"))
        # 旧版本遗留流将、离间/俘虏流亡角色统一补挂（每 tick 补到上限）
        cnt = await self._fetchone(
            "SELECT COUNT(*) AS n FROM strategy_char_auctions "
            "WHERE world_id=? AND status='open'", (world["id"],))
        open_n = int((cnt or {}).get("n", 0) or 0)
        cap = int(acfg.get("max_open_per_world", 4))
        if open_n < cap:
            pool = await self._fetchall("""
                SELECT * FROM strategy_characters c
                WHERE c.world_id=? AND c.status='wander' AND c.owner_user_id=''
                  AND COALESCE(c.auction_cooldown_until, 0)<=?
                  AND NOT EXISTS (SELECT 1 FROM strategy_char_auctions a
                                  WHERE a.char_id=c.id
                                    AND a.status IN ('open','settling'))
                ORDER BY c.id ASC LIMIT ?
            """, (world["id"], now, cap - open_n))
            cands = await self._auction_nodes(world)
            by_id = {n["id"]: n for n in cands}
            for row in pool:
                c = self._decode("strategy_characters", row)
                nid = int((c.get("location") or {}).get("node_id", 0) or 0)
                node = by_id.get(nid) or (cands[0] if cands else None)
                if not node:
                    break
                await self._open_auction(world, c, int(node["id"]))
                open_n += 1
        await self._commit()

    async def recruit_wanderer(self, user_id: str, char_id: int) -> dict:
        """旧接口保留：在野流将已统一改为坊市限时竞拍。"""
        world, _p = await self._require_player(user_id)
        c = await self.get_char(int(char_id))
        if c and int(c.get("world_id", 0) or 0) != int(world["id"]):
            raise StrategyError("对象不在当前世界")
        raise StrategyError("在野流将已改为坊市战功暗标，请到坊市出价竞得")
