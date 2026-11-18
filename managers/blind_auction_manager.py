# managers/blind_auction_manager.py
"""拍卖暗标：只给模糊描述的密封拍品，玩家盲出价，价高者得。

设计要点：
- 每 6 小时一轮，每轮 3 件暗标；拍品结果由 round_key + lot_no 的种子稳定生成，
  刷新页面不会改变结果，防止反复刷信息。
- 出价即冻结灵石，被更高价顶掉或落败时原路退回；结算与发奖在同一个事务里，
  避免重复领奖或灵石复制。
- 可付费请鉴定师看一次「品阶区间」，但鉴定结果有 20% 概率被做局误导。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from ..core.jewel_manager import JewelManager
from ..data import DataBase
from ..models import Player
from ..rank_rules import normalize_rank, rank_tier

__all__ = ["BlindAuctionManager"]

ROUND_SECONDS = 6 * 3600
LOTS_PER_ROUND = 3
APPRAISE_RATE = 0.03
MISLEAD_CHANCE = 0.20
LOT_KINDS = ("gear", "jewel", "material")
KIND_WEIGHTS = (52, 18, 30)


class BlindAuctionManager:
    def __init__(self, db: DataBase, storage_ring_mgr, config_manager, jewel_manager: JewelManager = None):
        self.db = db
        self.storage_ring_mgr = storage_ring_mgr
        self.config_manager = config_manager
        self.jewel_manager = jewel_manager or JewelManager(db, config_manager, storage_ring_mgr)
        # 后台任务与玩家指令可能同时触发结算，用进程内锁串行化
        self._settle_lock = asyncio.Lock()

    # ================= 基础 =================
    @staticmethod
    def round_key(now: Optional[int] = None) -> str:
        now = int(now or time.time())
        block = now // ROUND_SECONDS
        return f"blind:{block}"

    @staticmethod
    def round_bounds(now: Optional[int] = None) -> Tuple[int, int]:
        now = int(now or time.time())
        block = now // ROUND_SECONDS
        return block * ROUND_SECONDS, (block + 1) * ROUND_SECONDS

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        keys = [d[0] for d in cursor.description]
        return dict(zip(keys, row))

    async def _fetchall(self, sql: str, params: tuple = ()) -> List[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            keys = [d[0] for d in cursor.description]
        return [dict(zip(keys, row)) for row in rows]

    # ================= 拍品生成 =================
    def _roll_rank(self, rng, min_tier: int = 1, max_tier: int = 5) -> str:
        from ..rank_rules import RANK_NAMES
        pool = [r for r in RANK_NAMES if min_tier <= rank_tier(r, 1) <= max_tier]
        weights = [max(1, 6 - rank_tier(r, 1)) for r in pool]
        return rng.choices(pool, weights=weights, k=1)[0]

    def _generate_payload(self, rng, retry: int = 0) -> dict:
        kind = rng.choices(LOT_KINDS, weights=KIND_WEIGHTS, k=1)[0]
        if kind == "jewel":
            qualities = {"一般": 50, "良好": 30, "极佳": 15, "大师": 5}
            jewel = self.jewel_manager.roll_jewel(
                quality_weights=qualities, rng=rng
            )
            return {
                "kind": "jewel",
                "name": jewel.get("name"),
                "rank": jewel.get("rank"),
                "quality": jewel.get("quality"),
                "qty": 1,
                "value": int(jewel.get("price") or 0),
                "jewel": jewel,
            }
        if kind == "gear":
            rank = self._roll_rank(rng, 2, 5)
            pools = []
            for data in (
                getattr(self.config_manager, "weapons_data", {}) or {},
                getattr(self.config_manager, "items_data", {}) or {},
            ):
                for name, cfg in data.items():
                    if not isinstance(cfg, dict):
                        continue
                    if normalize_rank(cfg.get("rank"), "") != rank:
                        continue
                    if str(cfg.get("type") or "").lower() not in (
                        "weapon", "armor", "equipment", "装备", "武器", "防具"
                    ) and cfg.get("item_type") not in ("weapon", "armor"):
                        continue
                    pools.append((name, cfg))
            if not pools:
                if retry >= 2:
                    return {
                        "kind": "material", "name": "玄铁", "rank": "玄品",
                        "quality": "", "qty": 1, "value": 2000,
                    }
                return self._generate_payload(rng, retry + 1)
            name, cfg = rng.choice(pools)
            try:
                value = int(cfg.get("price") or 0)
            except (TypeError, ValueError):
                value = 0
            if value <= 0:
                from ..rank_rules import RANK_PRICE
                value = int(RANK_PRICE.get(rank, 5000))
            return {
                "kind": "gear",
                "name": name,
                "rank": rank,
                "quality": rng.choices(
                    ["一般", "良好", "极佳", "大师"], weights=[55, 28, 14, 3], k=1
                )[0],
                "qty": 1,
                "value": value,
            }
        # material
        rank = self._roll_rank(rng, 1, 5)
        catalog = getattr(self.config_manager, "material_catalog", {}) or {}
        pool = [
            (name, info) for name, info in catalog.items()
            if isinstance(info, dict) and normalize_rank(info.get("tier"), "") == rank
        ]
        if not pool:
            if retry >= 2:
                return {
                    "kind": "material", "name": "玄铁", "rank": "玄品",
                    "quality": "", "qty": 1, "value": 2000,
                }
            return self._generate_payload(rng, retry + 1)
        name, info = rng.choice(pool)
        qty = rng.choice([1, 1, 2, 3, 5])
        value = int(info.get("price") or 0) * qty
        return {
            "kind": "material", "name": name, "rank": rank,
            "quality": "", "qty": qty, "value": max(1, value),
        }

    def _describe(self, payload: dict, rng) -> Tuple[str, str]:
        kind = payload.get("kind")
        rank = payload.get("rank") or "未知"
        if kind == "gear":
            templates = [
                f"一只被禁制缠住的兵器匣，灵压约在【{rank}】上下",
                f"蒙尘的{('长匣' if rng.random() < 0.5 else '布包')}，隐约透出金铁气，疑似【{rank}】器物",
                f"残留剑意的密封拍品，鉴定师只肯说「不低于{rank}」",
            ]
        elif kind == "jewel":
            templates = [
                f"一枚被封在蜡丸里的宝珠，灵光内敛，约【{rank}】",
                f"看不清材质的珠玉，触手温润，可能达到【{rank}】",
                f"密封玉盒中传来清越碰撞声，鉴定为【{rank}】珠宝",
            ]
        else:
            templates = [
                f"一袋封灵材料，灵气驳杂，品阶约【{rank}】",
                f"沉甸甸的矿材包裹，鉴定师估价在【{rank}】",
                f"不知名的灵材匣，隐约有药香，约【{rank}】",
            ]
        return rng.choice(templates), rank

    async def _ensure_round(self, now: Optional[int] = None) -> dict:
        now = int(now or time.time())
        key = self.round_key(now)
        start, end = self.round_bounds(now)
        row = await self._fetchone(
            "SELECT * FROM blind_auction_rounds WHERE round_key = ?", (key,)
        )
        if not row:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO blind_auction_rounds "
                "(round_key, start_ts, end_ts, settled, created_at) VALUES (?, ?, ?, 0, ?)",
                (key, start, end, now),
            )
            await self.db.conn.commit()
            row = await self._fetchone(
                "SELECT * FROM blind_auction_rounds WHERE round_key = ?", (key,)
            )
        lots = await self._fetchall(
            "SELECT * FROM blind_auction_lots WHERE round_key = ? ORDER BY lot_no", (key,)
        )
        if not lots:
            for lot_no in range(1, LOTS_PER_ROUND + 1):
                rng = random.Random(f"{key}:lot:{lot_no}")
                payload = self._generate_payload(rng)
                hint, _rank = self._describe(payload, rng)
                base = max(1000, int(payload.get("value") or 0))
                min_bid = max(500, int(base * rng.uniform(0.18, 0.30)))
                await self.db.conn.execute(
                    "INSERT OR IGNORE INTO blind_auction_lots "
                    "(round_key, lot_no, payload, base_value, min_bid, hint) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key, lot_no, json.dumps(payload, ensure_ascii=False), base, min_bid, hint),
                )
            await self.db.conn.commit()
            lots = await self._fetchall(
                "SELECT * FROM blind_auction_lots WHERE round_key = ? ORDER BY lot_no", (key,)
            )
        row["lots"] = lots
        return row

    # ================= 结算 =================
    async def settle_due(self, now: Optional[int] = None) -> int:
        now = int(now or time.time())
        async with self._settle_lock:
            rounds = await self._fetchall(
                "SELECT * FROM blind_auction_rounds WHERE settled = 0 AND end_ts <= ?", (now,)
            )
            settled = 0
            for rnd in rounds:
                if await self._settle_round(rnd["round_key"], now):
                    settled += 1
            return settled

    async def _settle_round(self, key: str, now: int) -> bool:
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            rnd = await self._fetchone(
                "SELECT * FROM blind_auction_rounds WHERE round_key = ?", (key,)
            )
            if not rnd or int(rnd.get("settled") or 0):
                await self.db.conn.rollback()
                return False
            lots = await self._fetchall(
                "SELECT * FROM blind_auction_lots WHERE round_key = ? AND settled = 0 ORDER BY lot_no",
                (key,),
            )
            for lot in lots:
                bids = await self._fetchall(
                    "SELECT * FROM blind_auction_bids WHERE round_key = ? AND lot_no = ? "
                    "AND status = 'active' ORDER BY amount DESC, created_at ASC",
                    (key, lot["lot_no"]),
                )
                winner = bids[0] if bids else None
                if winner:
                    try:
                        player = await self.db.get_player_by_id(winner["user_id"])
                    except Exception:
                        player = None
                    if not player:
                        winner = None
                if winner:
                    payload = json.loads(lot.get("payload") or "{}")
                    ok, reason = await self._award(winner["user_id"], payload)
                    if ok:
                        await self.db.conn.execute(
                            "UPDATE blind_auction_bids SET status = 'won' WHERE id = ?",
                            (winner["id"],),
                        )
                        await self.db.conn.execute(
                            "UPDATE blind_auction_lots SET winner_id = ?, winning_bid = ?, settled = 1 "
                            "WHERE id = ?",
                            (winner["user_id"], int(winner["amount"]), lot["id"]),
                        )
                    else:
                        # 发奖失败：退回该玩家并作废，避免吞钱
                        await self.db.conn.execute(
                            "UPDATE players SET gold = gold + ? WHERE user_id = ?",
                            (int(winner["amount"]), winner["user_id"]),
                        )
                        await self.db.conn.execute(
                            "UPDATE blind_auction_bids SET status = 'refunded' WHERE id = ?",
                            (winner["id"],),
                        )
                        await self.db.conn.execute(
                            "UPDATE blind_auction_lots SET settled = 1 WHERE id = ?", (lot["id"],)
                        )
                        logger.warning(f"【暗标】发奖失败，已退款 {winner['user_id']}: {reason}")
                else:
                    await self.db.conn.execute(
                        "UPDATE blind_auction_lots SET settled = 1 WHERE id = ?", (lot["id"],)
                    )
                # 未中标的冻结金额全部退回
                for bid in bids:
                    if winner and bid["id"] == winner["id"]:
                        continue
                    if bid["status"] != "active":
                        continue
                    await self.db.conn.execute(
                        "UPDATE players SET gold = gold + ? WHERE user_id = ?",
                        (int(bid["amount"]), bid["user_id"]),
                    )
                    await self.db.conn.execute(
                        "UPDATE blind_auction_bids SET status = 'lost' WHERE id = ?", (bid["id"],)
                    )

            await self.db.conn.execute(
                "UPDATE blind_auction_rounds SET settled = 1 WHERE round_key = ?", (key,)
            )
            await self.db.conn.commit()
            return True
        except Exception:
            await self.db.conn.rollback()
            raise

    async def _award(self, user_id: str, payload: dict) -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "玩家不存在"
        kind = payload.get("kind")
        name = payload.get("name")
        if not name:
            return False, "拍品信息异常"
        if kind == "jewel" and isinstance(payload.get("jewel"), dict):
            gem = self.jewel_manager.add_jewel(player, dict(payload["jewel"]))
            await self.db.update_player(player, commit=False)
            return True, gem.get("name", name)
        if kind == "gear":
            ok, msg = await self.storage_ring_mgr.store_item(
                player, name, int(payload.get("qty") or 1), silent=True,
                quality=payload.get("quality") or "", external_transaction=True,
            )
            if ok:
                await self.db.update_player(player, commit=False)
            return ok, msg
        ok, msg = await self.storage_ring_mgr.store_item(
            player, name, int(payload.get("qty") or 1), silent=True,
            external_transaction=True,
        )
        if ok:
            await self.db.update_player(player, commit=False)
        return ok, msg

    # ================= 玩家操作 =================
    async def get_status(self, user_id: str, now: Optional[int] = None) -> str:
        now = int(now or time.time())
        await self.settle_due(now)
        rnd = await self._ensure_round(now)
        key = rnd["round_key"]
        appraisals = {
            (row["lot_no"]): row["hint"]
            for row in await self._fetchall(
                "SELECT lot_no, hint FROM blind_auction_appraisals WHERE round_key = ? AND user_id = ?",
                (key, user_id),
            )
        }
        lines = ["🔨 拍卖会 · 暗标场（每 6 小时一轮）", "━━━━━━━━━━━━━━━"]
        for idx, lot in enumerate(rnd["lots"], 1):
            bids = await self._fetchall(
                "SELECT amount FROM blind_auction_bids WHERE round_key = ? AND lot_no = ? "
                "AND status = 'active' ORDER BY amount DESC LIMIT 1",
                (key, lot["lot_no"]),
            )
            top = int(bids[0]["amount"]) if bids else 0
            lines.append(f"[{idx}] {lot['hint']}")
            lines.append(
                f"    起拍：{int(lot['min_bid']):,} 灵石"
                + (f"｜当前暗价：{top:,}" if top else "｜尚无暗价")
            )
            if lot["lot_no"] in appraisals:
                lines.append(f"    🔎 你的鉴定：{appraisals[lot['lot_no']]}")
            my_bid = await self._fetchone(
                "SELECT amount, status FROM blind_auction_bids "
                "WHERE round_key = ? AND lot_no = ? AND user_id = ?",
                (key, lot["lot_no"], user_id),
            )
            if my_bid:
                state = "已中标" if my_bid["status"] == "won" else (
                    "已退回" if my_bid["status"] == "refunded" else (
                        "未中标" if my_bid["status"] == "lost" else "冻结中"
                    )
                )
                lines.append(f"    🎫 你的暗价：{int(my_bid['amount']):,}（{state}）")
        remaining = max(0, int(rnd["end_ts"]) - now)
        lines.extend([
            "━━━━━━━━━━━━━━━",
            f"⏳ 本轮结束：{remaining // 3600} 时 {(remaining % 3600) // 60} 分后",
            "💡 `/拍卖会 暗标出价 <编号> <金额>` 盲出价（出价即冻结）",
            "💡 `/拍卖会 暗标鉴定 <编号>` 付费鉴定品阶区间（可能被做局）",
            "⚠️ 只给模糊描述，价高者得；落败与顶价会原路退回。",
        ])
        return "\n".join(lines)

    async def appraise(self, player: Player, lot_no: int) -> Tuple[bool, str]:
        now = int(time.time())
        rnd = await self._ensure_round(now)
        if now >= int(rnd["end_ts"]):
            return False, "❌ 本轮暗标已经结束，请等待下一轮。"
        lot = next((x for x in rnd["lots"] if int(x["lot_no"]) == int(lot_no)), None)
        if not lot:
            return False, "❌ 无效的暗标编号。"
        existing = await self._fetchone(
            "SELECT * FROM blind_auction_appraisals WHERE round_key = ? AND lot_no = ? AND user_id = ?",
            (rnd["round_key"], lot_no, player.user_id),
        )
        if existing:
            return False, f"❌ 你已经鉴定过这件暗标了：{existing['hint']}"
        cost = max(300, int(int(lot["base_value"]) * APPRAISE_RATE))
        if player.gold < cost:
            return False, f"❌ 鉴定需要 {cost:,} 灵石，你当前只有 {player.gold:,}。"
        rng = random.Random(f"blind-appraise:{rnd['round_key']}:{lot_no}:{player.user_id}")
        payload = json.loads(lot.get("payload") or "{}")
        rank = payload.get("rank") or "未知"
        from ..rank_rules import RANK_NAMES
        tier = rank_tier(rank, 1)
        if rng.random() < MISLEAD_CHANCE:
            fake_tier = max(1, min(5, tier + rng.choice([-1, 1])))
            rank = RANK_NAMES[fake_tier - 1] if fake_tier - 1 < len(RANK_NAMES) else rank
        kind_label = {"gear": "器物", "jewel": "珠宝", "material": "灵材"}.get(
            payload.get("kind"), "物品"
        )
        hint = f"像是【{rank}】品阶的{kind_label}，具体真假当面难断"
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (cost, player.user_id, cost),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 灵石不足，鉴定失败。"
            await self.db.conn.execute(
                "INSERT OR REPLACE INTO blind_auction_appraisals "
                "(round_key, lot_no, user_id, hint, created_at) VALUES (?, ?, ?, ?, ?)",
                (rnd["round_key"], lot_no, player.user_id, hint, now),
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise
        player.gold -= cost
        return True, (
            f"🔎 你花 {cost:,} 灵石请鉴定师掌眼。\n"
            f"鉴定结论：{hint}\n"
            f"⚠️ 暗标场鱼龙混杂，鉴定师也可能被收买。"
        )

    async def place_bid(self, player: Player, lot_no: int, amount: int) -> Tuple[bool, str]:
        now = int(time.time())
        await self.settle_due(now)
        rnd = await self._ensure_round(now)
        if rnd.get("settled"):
            return False, "❌ 本轮暗标已结算，请等待下一轮。"
        if now >= int(rnd["end_ts"]):
            return False, "❌ 本轮暗标已经结束。"
        lot = next((x for x in rnd["lots"] if int(x["lot_no"]) == int(lot_no)), None)
        if not lot:
            return False, "❌ 无效的暗标编号。"
        amount = int(amount)
        min_bid = int(lot["min_bid"])
        if amount < min_bid:
            return False, f"❌ 暗价不能低于起拍价 {min_bid:,} 灵石。"
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 出价与结算可能并发：事务内重读轮次/拍品/旧出价，
            # 避免结算刚把出价置为 lost 后又被本事务重新激活并冻结灵石。
            round_row = await self._fetchone(
                "SELECT settled, end_ts FROM blind_auction_rounds WHERE round_key = ?",
                (rnd["round_key"],),
            )
            if not round_row or int(round_row.get("settled") or 0):
                await self.db.conn.rollback()
                return False, "❌ 本轮暗标已结算，请等待下一轮。"
            if now >= int(round_row.get("end_ts") or 0):
                await self.db.conn.rollback()
                return False, "❌ 本轮暗标已经结束。"
            fresh_lot = await self._fetchone(
                "SELECT min_bid FROM blind_auction_lots WHERE round_key = ? AND lot_no = ?",
                (rnd["round_key"], lot_no),
            )
            if not fresh_lot:
                await self.db.conn.rollback()
                return False, "❌ 无效的暗标编号。"
            min_bid = int(fresh_lot.get("min_bid") or 0)
            if amount < min_bid:
                await self.db.conn.rollback()
                return False, f"❌ 暗价不能低于起拍价 {min_bid:,} 灵石。"
            existing = await self._fetchone(
                "SELECT * FROM blind_auction_bids WHERE round_key = ? AND lot_no = ? AND user_id = ?",
                (rnd["round_key"], lot_no, player.user_id),
            )
            old_amount = int(existing["amount"]) if existing else 0
            if existing and existing["status"] != "active":
                old_amount = 0
            if existing and existing["status"] == "active" and amount <= old_amount:
                await self.db.conn.rollback()
                return False, f"❌ 你的新暗价必须高于当前暗价 {old_amount:,}。"
            delta = amount - old_amount
            if delta > 0:
                cursor = await self.db.conn.execute(
                    "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                    (delta, player.user_id, delta),
                )
                if not cursor.rowcount:
                    await self.db.conn.rollback()
                    return False, "❌ 灵石不足，出价失败。"
            elif delta < 0:
                await self.db.conn.execute(
                    "UPDATE players SET gold = gold + ? WHERE user_id = ?",
                    (-delta, player.user_id),
                )
            await self.db.conn.execute(
                "INSERT INTO blind_auction_bids "
                "(round_key, lot_no, user_id, amount, status, created_at) "
                "VALUES (?, ?, ?, ?, 'active', ?) "
                "ON CONFLICT(round_key, lot_no, user_id) DO UPDATE SET "
                "amount = excluded.amount, status = 'active', created_at = excluded.created_at",
                (rnd["round_key"], lot_no, player.user_id, amount, now),
            )
            await self.db.conn.commit()
            player.gold -= delta
        except Exception:
            await self.db.conn.rollback()
            raise
        return True, (
            f"🎫 你对暗标 [{lot_no}] 出价 {amount:,} 灵石（已冻结）。\n"
            f"💡 可在本轮结束前继续加价；落败会自动退回。"
        )