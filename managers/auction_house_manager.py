# managers/auction_house_manager.py
"""全服拍卖会事件：排期、报名、逐件竞价、结算与播报出站。"""

import asyncio
import json
import math
import random
import time
from datetime import datetime
from typing import List, Optional, Tuple

from astrbot.api import logger

from ..core.item_rules import ItemRules
from ..data import DataBase
from ..models import Player

__all__ = ["AuctionHouseManager"]

GEAR_TYPES = {"weapon", "armor", "head", "body", "boots", "gloves", "ring", "amulet", "accessory"}
SLOT_PREVIEW_LABELS = {
    "weapon": "武器", "head": "头饰", "armor": "甲胄", "body": "甲胄",
    "gloves": "手套", "boots": "靴子", "ring1": "戒指", "ring2": "戒指",
    "amulet": "护符",
}
RANK_TIER = {
    "凡品": 1, "黄阶": 1,
    "灵品": 2, "玄阶": 2,
    "珍品": 3, "地阶": 3, "地品": 3,
    "圣品": 4, "天阶": 4, "天品": 4, "皇品": 4, "帝品": 4, "道品": 4,
    "仙品": 5, "混元先天": 5, "神品": 5,
}
CATEGORY_LABELS = {
    "equipment": "装备", "talisman": "符箓", "pill": "丹药", "recipe": "丹方",
    "technique": "功法", "main_technique": "心法", "material": "材料",
}


class AuctionHouseManager:
    """拍卖会管理器（与坊市 AuctionManager 完全独立）。"""

    def __init__(self, db: DataBase, storage_ring_mgr, config_manager, config: dict = None):
        self.db = db
        self.storage_ring_mgr = storage_ring_mgr
        self.config_manager = config_manager
        self.config = config or {}
        self._lock = asyncio.Lock()

        cfg = self.config.get("AUCTION_HOUSE", {}) or {}
        self.enabled = bool(cfg.get("ENABLED", True))
        self.cycle_days = max(1, self._as_int(cfg.get("CYCLE_DAYS"), 3))
        self.announce_start_hour = self._clamp_hour(cfg.get("ANNOUNCE_START_HOUR"), 10)
        self.announce_end_hour = self._clamp_hour(cfg.get("ANNOUNCE_END_HOUR"), 21)
        self.event_start_hour = self._clamp_hour(cfg.get("EVENT_START_HOUR"), 22)
        self.entry_fee = max(0, self._as_int(cfg.get("ENTRY_FEE"), 500))
        self.lot_duration = max(60, self._as_int(cfg.get("LOT_DURATION"), 600))
        self.bid_countdown = max(10, self._as_int(cfg.get("BID_COUNTDOWN"), 60))
        self.lot_gap = max(0, self._as_int(cfg.get("LOT_GAP"), 10))
        self.min_increment_pct = max(0.0, self._as_float(cfg.get("MIN_INCREMENT_PCT"), 0.05))
        self.lot_count_cfg = max(1, self._as_int(cfg.get("LOT_COUNT"), 8))

        ah_cfg = getattr(config_manager, "auction_house_config", {}) or {}
        self.ah_cfg = ah_cfg
        self.tier_labels = dict(ah_cfg.get("tier_labels") or {})
        self.start_price_rate = max(0.01, self._as_float(ah_cfg.get("start_price_rate"), 0.2))
        self.start_price_fluctuation = min(
            1.0, max(0.0, self._as_float(ah_cfg.get("start_price_fluctuation"), 0.2))
        )
        self.affix_counts = dict(ah_cfg.get("affix_counts") or {})
        blueprint = list(ah_cfg.get("lot_blueprint") or [])
        self.lot_blueprint = blueprint[: self.lot_count_cfg] if blueprint else []
        self.lot_count = len(self.lot_blueprint) or self.lot_count_cfg

        # 保底类目：即使拍品数量被裁剪或随机生成失败，也至少补一件。
        guaranteed = ah_cfg.get("guaranteed_categories")
        self.guaranteed_categories = (
            [str(x) for x in guaranteed if str(x).strip()]
            if isinstance(guaranteed, list)
            else ["main_technique"]
        ) or ["main_technique"]
        # 空间石：每场按概率追加，默认至少 80% 出现一件、50% 再追加一件。
        self.space_stone_name = str(ah_cfg.get("space_stone_name") or "空间石")
        self.space_stone_count = max(1, self._as_int(ah_cfg.get("space_stone_count"), 2))
        self.space_stone_chance = min(
            1.0, max(0.0, self._as_float(ah_cfg.get("space_stone_chance"), 0.8))
        )
        self.space_stone_extra_chance = min(
            1.0, max(0.0, self._as_float(ah_cfg.get("space_stone_extra_chance"), 0.5))
        )

        self.ai_enabled = bool(cfg.get("AI_ENABLED", True))
        self.ai_bid_broadcast = bool(cfg.get("AI_BID_BROADCAST", False))
        self.ai_timeout = max(2, self._as_int(cfg.get("AI_TIMEOUT"), 8))
        self.ai_max_chars = max(20, self._as_int(cfg.get("AI_MAX_CHARS"), 300))

    # ================= 基础工具 =================

    @staticmethod
    def _as_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clamp_hour(value, default: int) -> int:
        try:
            hour = int(value)
        except (TypeError, ValueError):
            hour = default
        return max(0, min(23, hour))

    @staticmethod
    def _midnight(ts: int) -> int:
        dt = datetime.fromtimestamp(int(ts))
        return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    @staticmethod
    def _at_hour(ts: int, hour: int) -> int:
        dt = datetime.fromtimestamp(int(ts))
        return int(dt.replace(hour=int(hour), minute=0, second=0, microsecond=0).timestamp())

    @staticmethod
    def _date_str(ts: int) -> str:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")

    @staticmethod
    def _fmt_price(value: int) -> str:
        return f"{int(value):,}"

    @staticmethod
    def _remaining_text(seconds: int) -> str:
        seconds = max(0, int(seconds))
        minutes, sec = divmod(seconds, 60)
        if minutes:
            return f"{minutes}分{sec:02d}秒"
        return f"{sec}秒"

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()) -> List[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ================= 排期与事件状态 =================

    async def ensure_event(self, now: Optional[int] = None) -> dict:
        """返回当前活动场次；没有则按 3 天窗口创建下一场。"""
        now = int(now or time.time())
        async with self._lock:
            return await self._ensure_event_locked(now)

    async def _ensure_event_locked(self, now: int) -> dict:
        if not self.enabled:
            return {}

        active = await self._fetchone(
            "SELECT * FROM auction_house_events "
            "WHERE phase IN ('scheduled', 'announcing', 'live') "
            "ORDER BY id DESC LIMIT 1"
        )
        if active:
            return active

        last = await self._fetchone(
            "SELECT * FROM auction_house_events ORDER BY id DESC LIMIT 1"
        )
        if last:
            cycle_no = int(last.get("cycle_no") or 0) + 1
            base = int(last.get("cycle_start") or 0) + self.cycle_days * 86400
            end_ts = int(last.get("event_end") or 0) or int(last.get("event_start") or 0) or now
            after_event = self._midnight(end_ts) + 86400
            cycle_start = max(base, after_event)
        else:
            cycle_no = 1
            cycle_start = self._midnight(now)

        # 在窗口内选择一天作为预告日；当天 22:00 开拍，已错过则顺延。
        for _ in range(365):
            candidates = []
            for offset in range(self.cycle_days):
                day_start = cycle_start + offset * 86400
                event_start = self._at_hour(day_start, self.event_start_hour)
                if event_start > now + 300:
                    candidates.append(day_start)
            if candidates:
                announce_day = random.choice(candidates)
                announce_start = self._at_hour(announce_day, self.announce_start_hour)
                event_start = self._at_hour(announce_day, self.event_start_hour)
                announce_date = self._date_str(announce_day)
                await self.db.conn.execute(
                    """INSERT INTO auction_house_events
                       (cycle_no, cycle_start, announce_date, announce_start, event_start,
                        event_end, phase, lot_count, current_lot, last_reminder_slot,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 0, 'scheduled', 0, 0, '', ?, ?)""",
                    (cycle_no, cycle_start, announce_date, announce_start, event_start, now, now),
                )
                await self.db.conn.commit()
                return await self._fetchone(
                    "SELECT * FROM auction_house_events ORDER BY id DESC LIMIT 1"
                ) or {}

            cycle_start += self.cycle_days * 86400
            cycle_no += 1

        logger.error("【拍卖会】无法排期：一年内没有可用日期")
        return {}

    async def tick(self, now: Optional[int] = None) -> int:
        """推进拍卖会状态机；返回本轮新增播报数量。"""
        if not self.enabled:
            return 0
        now = int(now or time.time())
        queued = 0
        async with self._lock:
            # 上次进程在发放中途退出时，残留的 delivering 需要回到 pending 才能重试。
            # 由于发放与状态更新都在本锁内完成，这里不会与正在进行的发放冲突。
            await self.db.conn.execute(
                "UPDATE auction_house_lots SET delivery_status='pending' WHERE delivery_status='delivering'"
            )
            await self.db.conn.commit()
            event = await self._ensure_event_locked(now)
            if not event:
                return 0

            if event["phase"] == "scheduled":
                if now >= int(event["announce_start"]):
                    await self._ensure_lots(event, now)
                    await self.db.conn.execute(
                        "UPDATE auction_house_events SET phase='announcing', updated_at=? WHERE id=?",
                        (now, event["id"]),
                    )
                    await self.db.conn.commit()
                    event["phase"] = "announcing"
                    queued += await self._queue_due_reminders(event, now)
                if now >= int(event["event_start"]):
                    queued += await self._start_live(event, now)

            elif event["phase"] == "announcing":
                queued += await self._queue_due_reminders(event, now)
                if now >= int(event["event_start"]):
                    queued += await self._start_live(event, now)

            elif event["phase"] == "live":
                queued += await self._process_live(event, now)

        return queued

    async def _ensure_lots(self, event: dict, now: int) -> List[dict]:
        lots = await self._fetchall(
            "SELECT * FROM auction_house_lots WHERE event_id=? ORDER BY lot_no",
            (event["id"],),
        )
        if lots:
            return lots

        generated = await self._generate_lots(event["id"], now)
        if generated:
            await self.db.conn.execute(
                "UPDATE auction_house_events SET lot_count=?, updated_at=? WHERE id=?",
                (len(generated), now, event["id"]),
            )
            await self.db.conn.commit()
        event["lot_count"] = len(generated)
        return generated

    async def _start_live(self, event: dict, now: int) -> int:
        lots = await self._ensure_lots(event, now)
        if not lots:
            await self._refund_entry_fees(event["id"])
            await self._finish_event(event, now, "本场拍卖会没有可用拍品，已取消（入场费已全额退还）。")
            return 0

        entries = await self._fetchone(
            "SELECT COUNT(*) AS n FROM auction_house_entries WHERE event_id=?",
            (event["id"],),
        )
        if not entries or int(entries.get("n") or 0) <= 0:
            await self._finish_event(event, now, "本场拍卖会无人报名入场，已取消。")
            return 0

        await self.db.conn.execute(
            "UPDATE auction_house_events SET phase='live', current_lot=0, lot_count=?, updated_at=? WHERE id=?",
            (len(lots), now, event["id"]),
        )
        await self.db.conn.commit()
        event["phase"] = "live"
        event["current_lot"] = 0
        event["lot_count"] = len(lots)
        return await self._start_lot(event, 1, now)

    async def _process_live(self, event: dict, now: int) -> int:
        current_no = int(event.get("current_lot") or 0)
        if current_no <= 0:
            return await self._start_lot(event, 1, now)

        lot = await self._fetchone(
            "SELECT * FROM auction_house_lots WHERE event_id=? AND lot_no=?",
            (event["id"], current_no),
        )
        if not lot:
            await self._finish_event(event, now)
            return 0

        status = lot["status"]
        if status == "pending":
            return await self._start_lot(event, current_no, now)

        if status == "live":
            if now >= int(lot["lot_start"]) + self.lot_duration:
                if int(lot.get("highest_bid") or 0) > 0:
                    await self._settle_lot(event, lot, now, sold=True)
                else:
                    await self._settle_lot(event, lot, now, sold=False)
                return 0

        if status == "countdown":
            if now >= int(lot["bid_deadline"]):
                await self._settle_lot(event, lot, now, sold=True)
                return 0
            # 倒计时尚未结束，但固定 10 分钟窗口已经结束，仍等倒计时落槌。
            return 0

        if status in ("sold", "passed"):
            next_at = max(
                int(lot["lot_start"]) + self.lot_duration,
                int(lot.get("bid_deadline") or 0),
            ) + self.lot_gap
            if now >= next_at:
                return await self._advance_lot(event, lot, now)

        return 0

    async def _refund_entry_fees(self, event_id: int) -> int:
        """场次在开拍前取消时退还入场费；entry_fee 置 0 保证重复调用不会多退。"""
        entries = await self._fetchall(
            "SELECT user_id, entry_fee FROM auction_house_entries WHERE event_id=? AND entry_fee>0",
            (event_id,),
        )
        if not entries:
            return 0
        refunded = 0
        for entry in entries:
            fee = int(entry.get("entry_fee") or 0)
            if fee <= 0:
                continue
            await self.db.conn.execute(
                "UPDATE players SET gold = gold + ? WHERE user_id=?",
                (fee, entry["user_id"]),
            )
            refunded += fee
        await self.db.conn.execute(
            "UPDATE auction_house_entries SET entry_fee=0 WHERE event_id=?",
            (event_id,),
        )
        await self.db.conn.commit()
        return refunded

    async def _start_lot(self, event: dict, lot_no: int, now: int) -> int:
        lot = await self._fetchone(
            "SELECT * FROM auction_house_lots WHERE event_id=? AND lot_no=?",
            (event["id"], lot_no),
        )
        if not lot:
            await self._finish_event(event, now)
            return 0

        await self.db.conn.execute(
            """UPDATE auction_house_lots
               SET status='live', lot_start=?, bid_deadline=0, current_price=?,
                   highest_bidder='', highest_bid=0, winner_id='', winning_price=0,
                   settled_at=0, delivery_status='none'
               WHERE id=?""",
            (now, int(lot["start_price"]), lot["id"]),
        )
        await self.db.conn.execute(
            "UPDATE auction_house_events SET current_lot=?, updated_at=? WHERE id=?",
            (lot_no, now, event["id"]),
        )
        await self.db.conn.commit()
        event["current_lot"] = lot_no

        lot["status"] = "live"
        lot["lot_start"] = now
        lot["bid_deadline"] = 0
        lot["current_price"] = int(lot["start_price"])
        await self._queue_message(
            event["id"], lot["id"], "start",
            f"ah:{event['id']}:{lot_no}:start",
            {"scene": "start", "lot_no": lot_no, "facts": self._lot_facts(event, lot)},
            self._lot_start_text(event, lot),
            now,
        )
        return 1

    async def _advance_lot(self, event: dict, lot: dict, now: int) -> int:
        next_no = int(lot["lot_no"]) + 1
        total = int(event.get("lot_count") or 0)
        if total <= 0:
            total = len(await self._fetchall(
                "SELECT 1 FROM auction_house_lots WHERE event_id=?", (event["id"],)
            ))
        if next_no > total:
            await self._finish_event(event, now)
            return 0
        return await self._start_lot(event, next_no, now)

    async def _settle_lot(self, event: dict, lot: dict, now: int, sold: bool) -> None:
        if sold:
            winner_id = lot.get("highest_bidder") or ""
            winning_price = int(lot.get("highest_bid") or 0)
            delivery_status = "delivering" if winner_id else "none"
            await self.db.conn.execute(
                """UPDATE auction_house_lots
                   SET status='sold', winner_id=?, winning_price=?, settled_at=?,
                       delivery_status=?
                   WHERE id=? AND status IN ('live', 'countdown')""",
                (winner_id, winning_price, now, delivery_status, lot["id"]),
            )
            await self.db.conn.commit()
            lot["status"] = "sold"
            lot["winner_id"] = winner_id
            lot["winning_price"] = winning_price
            if winner_id:
                winner = await self.db.get_player_by_id(winner_id)
                lot["winner_name"] = winner.user_name if winner and winner.user_name else winner_id
            if winner_id:
                ok, msg = await self._deliver_lot(lot, winner_id)
                await self.db.conn.execute(
                    "UPDATE auction_house_lots SET delivery_status=? WHERE id=?",
                    ("delivered" if ok else "pending", lot["id"]),
                )
                await self.db.conn.commit()
                if not ok:
                    logger.warning(f"【拍卖会】拍品发放失败 lot={lot['id']}: {msg}")
            payload = {"scene": "result", "lot_no": lot["lot_no"], "facts": self._lot_result_facts(event, lot, True)}
            text = self._lot_result_text(event, lot, True)
        else:
            await self.db.conn.execute(
                """UPDATE auction_house_lots
                   SET status='passed', settled_at=?, delivery_status='none'
                   WHERE id=? AND status='live'""",
                (now, lot["id"]),
            )
            await self.db.conn.commit()
            lot["status"] = "passed"
            payload = {"scene": "result", "lot_no": lot["lot_no"], "facts": self._lot_result_facts(event, lot, False)}
            text = self._lot_result_text(event, lot, False)

        await self._queue_message(
            event["id"], lot["id"], "result",
            f"ah:{event['id']}:{lot['lot_no']}:result",
            payload, text, now,
        )

    async def _finish_event(self, event: dict, now: int, extra: str = "") -> None:
        lots = await self._fetchall(
            "SELECT * FROM auction_house_lots WHERE event_id=? ORDER BY lot_no",
            (event["id"],),
        )
        await self.db.conn.execute(
            "UPDATE auction_house_events SET phase='settled', event_end=?, updated_at=? WHERE id=?",
            (now, now, event["id"]),
        )
        await self.db.conn.commit()
        event["phase"] = "settled"
        event["event_end"] = now
        await self._queue_message(
            event["id"], 0, "summary",
            f"ah:{event['id']}:summary",
            {"scene": "summary", "facts": self._summary_facts(lots), "extra": extra},
            self._summary_text(lots, extra),
            now,
        )

    # ================= 预告与消息 =================

    async def _queue_due_reminders(self, event: dict, now: int) -> int:
        if self._date_str(now) != event.get("announce_date"):
            return 0
        queued = 0
        last_slot = event.get("last_reminder_slot") or ""
        # 使用场次自身的预告开始小时，避免运行中修改配置后旧场次漏发预告。
        try:
            start_hour = datetime.fromtimestamp(int(event.get("announce_start") or now)).hour
        except (TypeError, ValueError, OSError):
            start_hour = self.announce_start_hour
        start_hour = max(0, min(23, start_hour))
        for hour in range(start_hour, self.announce_end_hour + 1):
            hour_ts = self._at_hour(now, hour)
            if now < hour_ts:
                continue
            if now - hour_ts > 600:
                continue
            slot_key = f"{event['announce_date']}-{hour:02d}"
            if slot_key <= last_slot:
                continue
            previews = await self._preview_lines(event)
            facts = (
                f"预告日：{event['announce_date']}\n"
                f"开拍时间：今日 {self.event_start_hour:02d}:00\n"
                f"入场费：{self._fmt_price(self.entry_fee)} 灵石\n"
                f"拍品预告：\n" + "\n".join(previews)
            )
            fallback = (
                f"📢 【拍卖会预告】\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{facts}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"💡 发送 /拍卖会报名 缴纳入场费，今晚 {self.event_start_hour:02d}:00 开拍！"
            )
            queued_ok = await self._queue_message(
                event["id"], 0, "reminder", f"ah:{event['id']}:reminder:{slot_key}",
                {"scene": "reminder", "facts": facts}, fallback, now,
            )
            if not queued_ok:
                # 入队失败时不要推进 slot，否则该小时预告会永久丢失。
                break
            last_slot = slot_key
            queued += 1
        if queued:
            await self.db.conn.execute(
                "UPDATE auction_house_events SET last_reminder_slot=?, updated_at=? WHERE id=?",
                (last_slot, now, event["id"]),
            )
            await self.db.conn.commit()
            event["last_reminder_slot"] = last_slot
        return queued

    async def _queue_message(self, event_id: int, lot_id: int, kind: str,
                             dedupe_key: str, payload: dict, fallback: str, now: int) -> bool:
        try:
            await self.db.conn.execute(
                """INSERT OR IGNORE INTO auction_house_messages
                   (event_id, lot_id, kind, dedupe_key, payload_json, fallback_text,
                    status, created_at, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 0)""",
                (
                    event_id, lot_id, kind, dedupe_key,
                    json.dumps(payload, ensure_ascii=False), fallback, now,
                ),
            )
            await self.db.conn.commit()
            return True
        except Exception as e:
            logger.warning(f"【拍卖会】播报入队失败: {e}")
            return False

    async def drain_messages(self, limit: int = 10, max_age: int = 900) -> List[dict]:
        async with self._lock:
            # 重启后积压的旧场次播报不应再发送；直接标记失败，避免无限重播。
            now = int(time.time())
            cutoff = now - max(60, int(max_age))
            await self.db.conn.execute(
                """UPDATE auction_house_messages SET status='failed', sent_at=?
                   WHERE status='pending' AND created_at < ?""",
                (now, cutoff),
            )
            await self.db.conn.commit()
            return await self._fetchall(
                "SELECT * FROM auction_house_messages WHERE status='pending' ORDER BY id LIMIT ?",
                (max(1, int(limit)),),
            )

    async def mark_message_sent(self, message_id: int, now: Optional[int] = None) -> None:
        await self.db.conn.execute(
            "UPDATE auction_house_messages SET status='sent', sent_at=? WHERE id=?",
            (int(now or time.time()), int(message_id)),
        )
        await self.db.conn.commit()

    async def mark_message_failed(self, message_id: int, now: Optional[int] = None) -> None:
        await self.db.conn.execute(
            "UPDATE auction_house_messages SET status='failed', sent_at=? WHERE id=?",
            (int(now or time.time()), int(message_id)),
        )
        await self.db.conn.commit()

    # ================= 玩家指令 =================

    async def register(self, player: Player) -> Tuple[bool, str]:
        now = int(time.time())
        async with self._lock:
            event = await self._ensure_event_locked(now)
            if not event or event["phase"] != "announcing":
                return False, "❌ 当前没有开放报名的拍卖会。"
            if now >= int(event["event_start"]):
                return False, "⏰ 拍卖会已经开拍，报名已截止。"

            exists = await self._fetchone(
                "SELECT 1 FROM auction_house_entries WHERE event_id=? AND user_id=?",
                (event["id"], player.user_id),
            )
            if exists:
                return False, "✅ 你已经报名本场拍卖会，无需重复缴纳。"

            await self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                fresh = await self.db.get_player_by_id(player.user_id)
                if not fresh:
                    await self.db.conn.rollback()
                    return False, "❌ 角色不存在，无法报名。"
                if self.entry_fee > 0:
                    if int(fresh.gold) < self.entry_fee:
                        await self.db.conn.rollback()
                        return False, (
                            f"❌ 灵石不足！入场费需要 {self._fmt_price(self.entry_fee)} 灵石，"
                            f"你只有 {self._fmt_price(fresh.gold)} 灵石。"
                        )
                    cursor = await self.db.conn.execute(
                        "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                        (self.entry_fee, player.user_id, self.entry_fee),
                    )
                    if int(cursor.rowcount or 0) <= 0:
                        await self.db.conn.rollback()
                        return False, "❌ 灵石不足，报名失败。"
                await self.db.conn.execute(
                    "INSERT INTO auction_house_entries (event_id, user_id, entry_fee, created_at) VALUES (?, ?, ?, ?)",
                    (event["id"], player.user_id, self.entry_fee, now),
                )
                await self.db.conn.commit()
            except Exception as e:
                await self.db.conn.rollback()
                logger.error(f"【拍卖会】报名失败: {e}")
                return False, "❌ 报名失败，请稍后再试。"

            return True, (
                f"✅ 报名成功！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"入场费：{self._fmt_price(self.entry_fee)} 灵石\n"
                f"开拍时间：今日 {self.event_start_hour:02d}:00\n"
                f"💡 发送 /拍卖会 查看拍品预告，开拍后使用 /拍卖会出价 <金额> 竞价。"
            )

    async def bid(self, player: Player, amount_text: str) -> Tuple[bool, str]:
        now = int(time.time())
        try:
            amount = int(str(amount_text).strip().replace(",", ""))
        except (TypeError, ValueError):
            return False, "❌ 出价必须是纯数字，例如：/拍卖会出价 5000"

        async with self._lock:
            event = await self._ensure_event_locked(now)
            if not event or event["phase"] != "live":
                return False, "❌ 当前没有正在进行的拍卖会。"

            current_no = int(event.get("current_lot") or 0)
            lot = await self._fetchone(
                "SELECT * FROM auction_house_lots WHERE event_id=? AND lot_no=?",
                (event["id"], current_no),
            )
            if not lot or lot["status"] not in ("live", "countdown"):
                return False, "❌ 当前没有可出价的拍品。"

            if lot["status"] == "live" and now >= int(lot["lot_start"]) + self.lot_duration:
                return False, "⏰ 本件拍品已结束。"
            if lot["status"] == "countdown" and now >= int(lot["bid_deadline"]):
                return False, "⏰ 本件拍品已落槌。"

            exists = await self._fetchone(
                "SELECT 1 FROM auction_house_entries WHERE event_id=? AND user_id=?",
                (event["id"], player.user_id),
            )
            if not exists:
                return False, "❌ 你尚未报名本场拍卖会，请先发送 /拍卖会报名。"

            current_price = int(lot.get("highest_bid") or 0)
            if current_price <= 0:
                min_bid = int(lot["start_price"])
            else:
                increment = max(1, int(math.ceil(current_price * self.min_increment_pct)))
                min_bid = current_price + increment
            if amount < min_bid:
                return False, f"❌ 出价过低，本次最低出价 {self._fmt_price(min_bid)} 灵石。"

            await self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                fresh_lot = await self._fetchone(
                    "SELECT * FROM auction_house_lots WHERE id=?", (lot["id"],)
                )
                if not fresh_lot or fresh_lot["status"] not in ("live", "countdown"):
                    await self.db.conn.rollback()
                    return False, "❌ 本件拍品已结束。"
                if fresh_lot["status"] == "countdown" and now >= int(fresh_lot["bid_deadline"]):
                    await self.db.conn.rollback()
                    return False, "⏰ 本件拍品已落槌。"
                current_price = int(fresh_lot.get("highest_bid") or 0)
                if current_price > 0:
                    min_bid = current_price + max(1, int(math.ceil(current_price * self.min_increment_pct)))
                else:
                    min_bid = int(fresh_lot["start_price"])
                if amount < min_bid:
                    await self.db.conn.rollback()
                    return False, f"❌ 出价过低，本次最低出价 {self._fmt_price(min_bid)} 灵石。"

                fresh = await self.db.get_player_by_id(player.user_id)
                if not fresh:
                    await self.db.conn.rollback()
                    return False, "❌ 角色不存在，无法出价。"
                prev_bidder = fresh_lot.get("highest_bidder") or ""
                prev_bid = int(fresh_lot.get("highest_bid") or 0)
                available = int(fresh.gold) + (prev_bid if prev_bidder == player.user_id else 0)
                if available < amount:
                    await self.db.conn.rollback()
                    return False, (
                        f"❌ 灵石不足！本次出价 {self._fmt_price(amount)}，"
                        f"你可动用 {self._fmt_price(available)} 灵石。"
                    )

                same_bidder = prev_bidder == player.user_id and prev_bid > 0
                if same_bidder:
                    await self.db.conn.execute(
                        "UPDATE players SET gold = gold + ? WHERE user_id = ?",
                        (prev_bid, player.user_id),
                    )
                cursor = await self.db.conn.execute(
                    "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                    (amount, player.user_id, amount),
                )
                if int(cursor.rowcount or 0) <= 0:
                    await self.db.conn.rollback()
                    return False, "❌ 灵石不足，出价失败。"
                if prev_bidder and prev_bid > 0 and not same_bidder:
                    await self.db.conn.execute(
                        "UPDATE players SET gold = gold + ? WHERE user_id = ?",
                        (prev_bid, prev_bidder),
                    )

                await self.db.conn.execute(
                    """INSERT INTO auction_house_bids
                       (event_id, lot_id, user_id, user_name, amount, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (event["id"], fresh_lot["id"], player.user_id,
                     player.user_name or player.user_id, amount, now),
                )
                deadline = now + self.bid_countdown
                await self.db.conn.execute(
                    """UPDATE auction_house_lots
                       SET status='countdown', current_price=?, highest_bidder=?,
                           highest_bid=?, bid_deadline=?
                       WHERE id=?""",
                    (amount, player.user_id, amount, deadline, fresh_lot["id"]),
                )
                await self.db.conn.commit()
            except Exception as e:
                await self.db.conn.rollback()
                logger.error(f"【拍卖会】出价失败: {e}")
                return False, "❌ 出价失败，请稍后再试。"

            facts = (
                f"拍品：第 {current_no} 件 {lot['preview_label']}\n"
                f"出价人：{player.user_name or player.user_id}\n"
                f"当前价：{self._fmt_price(amount)} 灵石\n"
                f"倒计时：{self.bid_countdown} 秒"
            )
            fallback = (
                f"🔨 【拍卖会·竞价】\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{facts}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"💡 继续加价：/拍卖会出价 <金额>"
            )
            await self._queue_message(
                event["id"], lot["id"], "bid",
                f"ah:{event['id']}:{current_no}:bid:{now}:{player.user_id}:{amount}",
                {"scene": "bid", "facts": facts}, fallback, now,
            )
            return True, (
                f"✅ 出价成功！\n"
                f"当前价：{self._fmt_price(amount)} 灵石\n"
                f"⏳ 倒计时 {self.bid_countdown} 秒，其他修士仍可加价。"
            )

    async def claim_pending(self, player: Player) -> Tuple[bool, str]:
        # 与 tick/结算共用同一把锁，避免结算与补领同时发放同一件拍品。
        async with self._lock:
            lots = await self._fetchall(
                "SELECT * FROM auction_house_lots WHERE winner_id=? AND delivery_status='pending' ORDER BY id",
                (player.user_id,),
            )
            if not lots:
                return False, "📦 你当前没有待领取的拍卖会拍品。"
            delivered = []
            for lot in lots:
                # 原子占位：只有仍为 pending 的记录才能被本次领走。
                cursor = await self.db.conn.execute(
                    """UPDATE auction_house_lots SET delivery_status='delivering'
                       WHERE id=? AND delivery_status='pending'""",
                    (lot["id"],),
                )
                await self.db.conn.commit()
                if cursor.rowcount <= 0:
                    continue
                ok, msg = await self._deliver_lot(lot, player.user_id)
                await self.db.conn.execute(
                    "UPDATE auction_house_lots SET delivery_status=? WHERE id=?",
                    ("delivered" if ok else "pending", lot["id"]),
                )
                await self.db.conn.commit()
                if ok:
                    delivered.append(f"第 {lot['lot_no']} 件：{lot['item_name']} ×{lot['count']}")
                else:
                    logger.warning(f"【拍卖会】补领失败 lot={lot['id']}: {msg}")
            if not delivered:
                return False, "❌ 补领失败，请确认储物戒/溢出仓库状态后联系管理员。"
            return True, "✅ 补领成功！\n" + "\n".join(delivered)

    async def get_status(self, player: Optional[Player] = None) -> str:
        now = int(time.time())
        async with self._lock:
            event = await self._ensure_event_locked(now)
            if not event:
                return "📭 拍卖会暂未开启。"

            registered = False
            if player:
                registered = bool(await self._fetchone(
                    "SELECT 1 FROM auction_house_entries WHERE event_id=? AND user_id=?",
                    (event["id"], player.user_id),
                ))

            lines = ["🏛️ 【拍卖会】", "━━━━━━━━━━━━━━━"]
            phase = event["phase"]
            if phase == "scheduled":
                start_dt = datetime.fromtimestamp(int(event["announce_start"])).strftime("%m-%d %H:%M")
                event_dt = datetime.fromtimestamp(int(event["event_start"])).strftime("%m-%d %H:%M")
                lines.append(f"📅 预告开启：{start_dt}")
                lines.append(f"🔨 开拍时间：{event_dt}")
                lines.append(f"🎟️ 入场费：{self._fmt_price(self.entry_fee)} 灵石")
                lines.append("⏳ 预告尚未开始，请耐心等待。")
                return "\n".join(lines)

            if phase == "announcing":
                remain = max(0, int(event["event_start"]) - now)
                lines.append(f"📅 预告日：{event['announce_date']}")
                lines.append(f"🔨 开拍：今日 {self.event_start_hour:02d}:00（剩余 {self._remaining_text(remain)}）")
                lines.append(f"🎟️ 入场费：{self._fmt_price(self.entry_fee)} 灵石")
                if player:
                    lines.append("✅ 报名状态：已报名" if registered else "❌ 报名状态：未报名（发送 /拍卖会报名）")
                lines.append("")
                lines.append("📜 拍品预告（详情开拍时公布）：")
                lines.extend(await self._preview_lines(event))
                lines.append("━━━━━━━━━━━━━━━")
                lines.append("💡 /拍卖会报名 缴纳入场费；开拍后 /拍卖会出价 <金额>")
                return "\n".join(lines)

            if phase == "live":
                lines.append("🔴 拍卖进行中")
                lines.extend(await self._live_status_lines(event, now))
                lines.append("━━━━━━━━━━━━━━━")
                if player:
                    lines.append("✅ 已报名，可出价" if registered else "❌ 未报名，无法出价")
                lines.append("💡 /拍卖会出价 <金额>（每次加价至少当前价的 5%）")
                return "\n".join(lines)

            lines.append("✅ 本场拍卖会已结束。")
            lines.extend(await self._preview_lines(event))
            lines.append("💡 下一场拍卖会将在新的 3 天窗口内随机预告。")
            return "\n".join(lines)

    # ================= 文案 =================

    async def _preview_lines(self, event: dict) -> List[str]:
        lots = await self._fetchall(
            "SELECT * FROM auction_house_lots WHERE event_id=? ORDER BY lot_no",
            (event["id"],),
        )
        if not lots:
            return ["（拍品尚未生成）"]
        lines = []
        for lot in lots:
            status = lot["status"]
            suffix = {
                "pending": "待拍", "live": "竞拍中", "countdown": "倒计时",
                "sold": "已成交", "passed": "流拍",
            }.get(status, status)
            if status in ("live", "countdown", "sold", "passed"):
                label = f"{lot['preview_label']}【{lot['item_name']}】"
            else:
                label = lot["preview_label"]
            lines.append(f"  #{lot['lot_no']} {label} · {suffix}")
        return lines

    async def _live_status_lines(self, event: dict, now: int) -> List[str]:
        lots = await self._fetchall(
            "SELECT * FROM auction_house_lots WHERE event_id=? ORDER BY lot_no",
            (event["id"],),
        )
        current_no = int(event.get("current_lot") or 0)
        lot = next((x for x in lots if int(x["lot_no"]) == current_no), None)
        if not lot:
            return ["（等待下一件拍品）"]
        lines = [f"🔨 第 {lot['lot_no']}/{len(lots)} 件", ""]
        lines.extend(self._lot_detail_lines(lot))
        lines.append("")
        if lot["status"] == "sold":
            lines.append(f"🏆 已成交：{self._fmt_price(lot.get('winning_price') or 0)} 灵石")
        elif lot["status"] == "passed":
            lines.append("🕊️ 本件流拍，等待下一件。")
        elif lot["status"] == "countdown":
            remain = max(0, int(lot["bid_deadline"]) - now)
            lines.append(f"⏳ 倒计时：{self._remaining_text(remain)}")
        else:
            remain = max(0, int(lot["lot_start"]) + self.lot_duration - now)
            lines.append(f"⏳ 本件剩余：{self._remaining_text(remain)}")
        lines.append(f"💰 当前价：{self._fmt_price(lot.get('highest_bid') or lot['start_price'])} 灵石")
        lines.append(f"🎯 起拍价：{self._fmt_price(lot['start_price'])} 灵石")
        if lot.get("highest_bidder"):
            winner = await self.db.get_player_by_id(lot["highest_bidder"])
            name = (winner.user_name if winner and winner.user_name else lot["highest_bidder"])
            lines.append(f"👤 领先：{name}")
        lines.append("")
        lines.append("📜 后续拍品：")
        for item in lots:
            if int(item["lot_no"]) <= current_no:
                continue
            lines.append(f"  #{item['lot_no']} {item['preview_label']} · 待拍")
        return lines

    def _lot_detail_lines(self, lot: dict) -> List[str]:
        try:
            payload = json.loads(lot.get("payload_json") or "{}")
        except Exception:
            payload = {}
        quality = f"·{lot['item_quality']}" if lot.get("item_quality") else ""
        lines = [
            f"【{lot['item_name']}】{lot['item_rank']}{quality} ×{lot['count']}",
        ]
        desc = payload.get("description") or ""
        if desc:
            lines.append(f"  {desc}")
        rates = payload.get("equipment_rates") or {}
        if rates:
            from ..core.equipment_balance import ATTRIBUTE_LABELS, QUALITY
            mult = QUALITY.get(lot.get("item_quality") or "", 1.0)
            rate_text = "、".join(
                f"{ATTRIBUTE_LABELS.get(k, k)}+{float(v) * mult:.1%}"
                for k, v in rates.items() if float(v or 0) > 0
            )
            if rate_text:
                lines.append(f"  主属性（品质后）：{rate_text}")
        affixes = payload.get("affixes") or []
        if affixes:
            lines.append("  词条：" + "、".join(
                a.get("display") or f"{a.get('name', '')}+{a.get('value', '')}" for a in affixes
            ))
        return lines

    def _lot_facts(self, event: dict, lot: dict) -> str:
        detail = "\n".join(self._lot_detail_lines(lot))
        return (
            f"第 {lot['lot_no']}/{event.get('lot_count') or self.lot_count} 件拍品\n"
            f"{detail}\n"
            f"起拍价：{self._fmt_price(lot['start_price'])} 灵石\n"
            f"规则：固定 {self.lot_duration // 60} 分钟，出价后 {self.bid_countdown} 秒倒计时"
        )

    def _lot_start_text(self, event: dict, lot: dict) -> str:
        return (
            f"🔨 【拍卖会·开拍】\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{self._lot_facts(event, lot)}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 出价：/拍卖会出价 <金额>"
        )

    def _lot_result_facts(self, event: dict, lot: dict, sold: bool) -> str:
        if sold:
            winner = lot.get("winner_name") or lot.get("winner_id") or ""
            return (
                f"第 {lot['lot_no']} 件：{lot['preview_label']}【{lot['item_name']}】\n"
                f"成交人：{winner}\n"
                f"成交价：{self._fmt_price(lot.get('winning_price') or 0)} 灵石"
            )
        return f"第 {lot['lot_no']} 件：{lot['preview_label']}【{lot['item_name']}】\n无人出价，流拍。"

    def _lot_result_text(self, event: dict, lot: dict, sold: bool) -> str:
        if sold:
            return (
                f"🏆 【拍卖会·成交】\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{self._lot_result_facts(event, lot, True)}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"💡 下一件拍品即将登场。"
            )
        return (
            f"🕊️ 【拍卖会·流拍】\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{self._lot_result_facts(event, lot, False)}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 下一件拍品即将登场。"
        )

    def _summary_facts(self, lots: List[dict]) -> str:
        sold = [x for x in lots if x.get("status") == "sold"]
        passed = [x for x in lots if x.get("status") == "passed"]
        total = sum(int(x.get("winning_price") or 0) for x in sold)
        return (
            f"成交件数：{len(sold)}/{len(lots)}\n"
            f"流拍件数：{len(passed)}\n"
            f"成交总额：{self._fmt_price(total)} 灵石"
        )

    def _summary_text(self, lots: List[dict], extra: str = "") -> str:
        head = "📜 【拍卖会·结算】" if not extra else "📭 【拍卖会】"
        return (
            f"{head}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{extra or self._summary_facts(lots)}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 感谢各位道友参与，下一场拍卖会将在新的 3 天窗口内随机预告。"
        )

    # ================= 拍品生成 =================

    async def _generate_lots(self, event_id: int, now: int) -> List[dict]:
        lots: List[dict] = []
        used_names: set = set()
        prev_start = 0
        for entry in self.lot_blueprint:
            if not isinstance(entry, dict):
                continue
            lot_no = len(lots) + 1
            lot = self._generate_lot(event_id, lot_no, entry, used_names, prev_start, now)
            if not lot:
                continue
            prev_start = int(lot["start_price"])
            used_names.add(lot["item_name"])
            lots.append(lot)

        # 保底：主修心法等指定类目至少出现一件（拍品数量被裁剪时也能补上）。
        present = {lot["category"] for lot in lots}
        for category in self.guaranteed_categories:
            if category in present:
                continue
            entry = next(
                (e for e in self.lot_blueprint
                 if isinstance(e, dict) and str(e.get("category") or "") == category),
                None,
            ) or {
                "category": category,
                "tier": 4 if category == "main_technique" else 3,
                "count": 1,
            }
            lot = self._generate_lot(event_id, len(lots) + 1, entry, used_names, 0, now)
            if not lot:
                logger.warning(f"【拍卖会】保底拍品生成失败：{category}")
                continue
            used_names.add(lot["item_name"])
            lots.append(lot)
            present.add(category)

        # 空间石：每场独立判定，默认 80% 出现一件、50% 再追加一件。
        stone_cfg = (getattr(self.config_manager, "items_data", {}) or {}).get(
            self.space_stone_name
        )
        if isinstance(stone_cfg, dict):
            stone_tier = RANK_TIER.get(str(stone_cfg.get("rank") or "地阶"), 3)
            stone_entry = {
                "category": "material",
                "tier": stone_tier,
                "count": self.space_stone_count,
                "item_names": [self.space_stone_name],
            }
            for chance in (self.space_stone_chance, self.space_stone_extra_chance):
                if chance <= 0 or random.random() >= chance:
                    continue
                lot = self._generate_lot(
                    event_id, len(lots) + 1, stone_entry, used_names, 0, now
                )
                if not lot:
                    continue
                used_names.add(lot["item_name"])
                lots.append(lot)

        # 追加的拍品按价格重新排序，保持“由低到高”的展示顺序。
        lots.sort(key=lambda x: int(x["start_price"]))
        for index, lot in enumerate(lots, 1):
            lot["lot_no"] = index

        created: List[dict] = []
        for lot in lots:
            cursor = await self.db.conn.execute(
                """INSERT INTO auction_house_lots
                   (event_id, lot_no, category, preview_label, item_name, item_rank,
                    item_quality, count, payload_json, start_price, current_price,
                    status, lot_start, bid_deadline, highest_bidder, highest_bid,
                    winner_id, winning_price, settled_at, delivery_status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, '', 0,
                           '', 0, 0, 'none', ?)""",
                (
                    lot["event_id"], lot["lot_no"], lot["category"], lot["preview_label"],
                    lot["item_name"], lot["item_rank"], lot["item_quality"], lot["count"],
                    lot["payload_json"], lot["start_price"], lot["current_price"],
                    lot["created_at"],
                ),
            )
            lot["id"] = int(cursor.lastrowid)
            created.append(lot)
        await self.db.conn.commit()
        return created

    def _generate_lot(self, event_id: int, lot_no: int, entry: dict, used_names: set,
                      prev_start: int, now: int) -> Optional[dict]:
        category = str(entry.get("category") or "").strip()
        tier = max(1, min(5, self._as_int(entry.get("tier"), 2)))
        count = max(1, self._as_int(entry.get("count"), 1))
        item_names = entry.get("item_names") or entry.get("item_name")
        selected = self._select_item(category, tier, used_names, item_names)
        if not selected:
            return None

        item_name, item_cfg, rank, payload = selected
        quality = ""
        affixes = []
        if category in ("equipment", "pill"):
            weights = entry.get("quality_weights") or self._default_quality_weights(tier)
            quality = self._weighted_choice(weights) or "一般"
        if category == "equipment":
            affixes = self._roll_affixes(quality)
            payload["affixes"] = affixes
            payload["equipment_rates"] = item_cfg.get("equipment_rates") or {}

        base_price = self._base_price(category, item_cfg, payload) * count
        rate = max(0.01, self._as_float(entry.get("start_price_rate"), self.start_price_rate))
        fluctuation = self._as_float(
            entry.get("start_price_fluctuation"), self.start_price_fluctuation
        )
        fluctuation = min(1.0, max(0.0, fluctuation))
        rate *= random.uniform(1 - fluctuation, 1 + fluctuation)
        price = max(1, int(round(base_price * rate)))
        if prev_start > 0 and price <= prev_start:
            price = prev_start + max(1, int(prev_start * 0.1))

        preview_label = self._preview_label(category, rank, item_cfg, count)
        return {
            "event_id": event_id,
            "lot_no": lot_no,
            "category": category,
            "preview_label": preview_label,
            "item_name": item_name,
            "item_rank": rank,
            "item_quality": quality,
            "count": count,
            "payload_json": json.dumps(payload, ensure_ascii=False),
            "start_price": price,
            "current_price": price,
            "status": "pending",
            "created_at": int(now),
        }

    def _select_item(self, category: str, tier: int, used_names: set, item_names=None):
        candidates = self._category_candidates(category)
        if item_names:
            if isinstance(item_names, (list, tuple, set)):
                allowed = {str(x).strip() for x in item_names if str(x).strip()}
            else:
                allowed = {str(item_names).strip()}
            if allowed:
                candidates = [x for x in candidates if x[0] in allowed]
        if not candidates:
            return None
        preferred = [x for x in candidates if x[2] == tier and x[0] not in used_names]
        if preferred:
            pool = preferred
        else:
            available = [x for x in candidates if x[0] not in used_names] or candidates
            lower = [x for x in available if x[2] <= tier]
            if lower:
                nearest = max(x[2] for x in lower)
                pool = [x for x in lower if x[2] == nearest]
            else:
                nearest = min(x[2] for x in available)
                pool = [x for x in available if x[2] == nearest]
        weights = [max(1, self._as_int(x[1].get("shop_weight"), 10)) for x in pool]
        try:
            name, cfg, _tier, rank, payload = random.choices(pool, weights=weights, k=1)[0]
        except Exception:
            name, cfg, _tier, rank, payload = random.choice(pool)
        return name, cfg, rank, dict(payload)

    def _category_candidates(self, category: str):
        result = []
        if category == "equipment":
            merged = {}
            merged.update(getattr(self.config_manager, "items_data", {}) or {})
            merged.update(getattr(self.config_manager, "weapons_data", {}) or {})
            for name, cfg in merged.items():
                if not isinstance(cfg, dict):
                    continue
                if ItemRules.normalize_equipment_type(cfg) not in GEAR_TYPES:
                    continue
                rank = str(cfg.get("rank") or "凡品")
                result.append((name, cfg, RANK_TIER.get(rank, 1), rank, {
                    "description": cfg.get("description", ""),
                    "category": "equipment",
                }))
        elif category == "talisman":
            for name, cfg in (getattr(self.config_manager, "items_data", {}) or {}).items():
                if not isinstance(cfg, dict) or cfg.get("type") != "符箓":
                    continue
                rank = str(cfg.get("rank") or "黄阶")
                result.append((name, cfg, RANK_TIER.get(rank, 1), rank, {
                    "description": cfg.get("description", ""),
                    "category": "talisman",
                }))
        elif category == "pill":
            merged = {}
            for attr in ("pills_data", "exp_pills_data", "utility_pills_data"):
                merged.update(getattr(self.config_manager, attr, {}) or {})
            for name, cfg in merged.items():
                if not isinstance(cfg, dict):
                    continue
                rank = str(cfg.get("rank") or "凡品")
                result.append((name, cfg, RANK_TIER.get(rank, 1), rank, {
                    "description": cfg.get("description", ""),
                    "category": "pill",
                }))
        elif category in ("technique", "main_technique"):
            merged = {}
            for name, cfg in (getattr(self.config_manager, "techniques_data", {}) or {}).items():
                if not isinstance(cfg, dict):
                    continue
                merged[name] = cfg
            if category == "technique":
                for name, cfg in (getattr(self.config_manager, "items_data", {}) or {}).items():
                    if not isinstance(cfg, dict):
                        continue
                    if cfg.get("type") == "功法":
                        merged.setdefault(name, cfg)
            for name, cfg in merged.items():
                item_type = cfg.get("type")
                if category == "technique":
                    if item_type not in ("technique", "功法"):
                        continue
                elif item_type not in ("main_technique", "心法"):
                    continue
                rank = str(cfg.get("rank") or "凡品")
                result.append((name, cfg, RANK_TIER.get(rank, 1), rank, {
                    "description": cfg.get("description", ""),
                    "category": category,
                }))
        elif category == "material":
            for name, cfg in (getattr(self.config_manager, "items_data", {}) or {}).items():
                if not isinstance(cfg, dict):
                    continue
                if cfg.get("type") not in ("材料", "material"):
                    continue
                rank = str(cfg.get("rank") or "凡品")
                result.append((name, cfg, RANK_TIER.get(rank, 1), rank, {
                    "description": cfg.get("description", ""),
                    "category": "material",
                }))
        elif category == "recipe":
            for recipe in (getattr(self.config_manager, "pill_recipes_data", {}) or {}).values():
                if not isinstance(recipe, dict):
                    continue
                name = self.config_manager.get_recipe_scroll_name(recipe)
                level = self._as_int(recipe.get("level_required"), 0)
                if level >= 15:
                    tier = 5
                elif level >= 10:
                    tier = 4
                elif level >= 5:
                    tier = 3
                else:
                    tier = 2
                cfg = {
                    "price": self._as_int(recipe.get("price"), 2000),
                    "description": recipe.get("desc") or "记载丹方炼制方法的卷轴。",
                }
                rank_label = self.tier_labels.get(str(tier), "灵品")
                result.append((name, cfg, tier, rank_label, {
                    "description": cfg["description"],
                    "category": "recipe",
                    "recipe_id": recipe.get("id"),
                }))
        return result

    @staticmethod
    def _default_quality_weights(tier: int) -> dict:
        return {
            1: {"一般": 60, "良好": 30, "极佳": 10, "大师": 0},
            2: {"一般": 25, "良好": 50, "极佳": 25, "大师": 0},
            3: {"一般": 0, "良好": 40, "极佳": 50, "大师": 10},
            4: {"一般": 0, "良好": 10, "极佳": 60, "大师": 30},
            5: {"一般": 0, "良好": 0, "极佳": 40, "大师": 60},
        }.get(tier, {"一般": 100})

    @staticmethod
    def _weighted_choice(weights: dict) -> str:
        items = [(k, v) for k, v in (weights or {}).items() if int(v or 0) > 0]
        if not items:
            return ""
        names = [x[0] for x in items]
        values = [int(x[1]) for x in items]
        try:
            return random.choices(names, weights=values, k=1)[0]
        except Exception:
            return random.choice(names)

    def _roll_affixes(self, quality: str) -> List[dict]:
        count = self._as_int(self.affix_counts.get(quality), 0)
        if count <= 0:
            return []
        from .forge_manager import ForgeManager
        result = []
        for _ in range(count):
            affix = random.choice(ForgeManager.AFFIX_POOL).copy()
            multiplier = random.uniform(0.8, 1.2)
            if isinstance(affix.get("value"), float):
                affix["value"] = round(float(affix["value"]) * multiplier, 3)
                affix["display"] = f"{affix['name']}+{affix['value'] * 100:.1f}%"
            else:
                affix["value"] = int(float(affix.get("value", 0)) * multiplier)
                affix["display"] = f"{affix['name']}+{affix['value']}"
            result.append(affix)
        return result

    @staticmethod
    def _base_price(category: str, cfg: dict, payload: dict) -> int:
        price = cfg.get("price")
        if price is None:
            price = payload.get("price")
        try:
            price = int(price or 0)
        except (TypeError, ValueError):
            price = 0
        if price <= 0:
            from ..core.equipment_balance import equipment_price
            price = int(equipment_price(cfg))
        return max(1, price)

    @staticmethod
    def _preview_label(category: str, rank: str, cfg: dict, count: int) -> str:
        if category == "equipment":
            slot = ItemRules.get_equip_slot(cfg)
            label = SLOT_PREVIEW_LABELS.get(slot, "装备")
        else:
            label = CATEGORY_LABELS.get(category, "拍品")
        return f"{rank}·{label} ×{count}"

    # ================= 发放 =================

    async def _deliver_lot(self, lot: dict, user_id: str) -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "玩家不存在"
        try:
            payload = json.loads(lot.get("payload_json") or "{}")
        except Exception:
            payload = {}

        category = lot.get("category")
        item_name = lot.get("item_name") or ""
        count = max(1, int(lot.get("count") or 1))
        quality = lot.get("item_quality") or "一般"

        try:
            if category == "pill":
                from ..core.pill_manager import PillManager
                pm = PillManager(self.db, self.config_manager)
                await pm.add_pill_to_inventory(player, item_name, count, quality=quality)
                return True, ""
            if category == "equipment":
                from ..core.item_instance_manager import ItemInstanceManager
                im = ItemInstanceManager(self.db, self.config_manager)
                volume = self.storage_ring_mgr._get_item_volume(item_name) * count
                location = "storage" if self.storage_ring_mgr.get_available_slots(player) >= volume else "overflow"
                im.create_instances(
                    player, item_name, count,
                    quality=quality,
                    affixes=payload.get("affixes") or [],
                    location=location,
                )
                await self.db.update_player(player)
                return True, ""
            ok, msg = await self.storage_ring_mgr.store_item(
                player, item_name, count, silent=True, allow_overflow=True
            )
            return bool(ok), msg
        except Exception as e:
            logger.error(f"【拍卖会】发放异常 lot={lot.get('id')}: {e}")
            return False, str(e)
