# managers/betting_pool_manager.py
"""斗法盘口：NPC 模拟对决与赌石复盘下注。

- 每 2 小时刷新 3 个 NPC 斗法盘口，只给双方模糊信息，玩家判断后下注；
- 赔率已含庄家抽水，长期期望为负；
- 另提供 `/盘口 赌石 <档位> <出货/切垮> <金额>`，直接对赌本档原石能否出货，
  概率取自当前赌石配置，适合“我就赌这一刀”的短平快玩法。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import List, Optional, Tuple

from ..data import DataBase
from ..models import Player

__all__ = ["BettingPoolManager"]

POOL_SECONDS = 2 * 3600
BET_WINDOW = 30 * 60
POOLS_PER_BLOCK = 3
HOUSE_RATE = 0.92
MIN_BET = 200

NPC_NAMES = (
    "铁掌真人", "青锋剑客", "血河散人", "玄龟道人", "赤霄仙子", "无相书生",
    "断岳刀客", "寒潭剑姬", "雷泽老怪", "枯木禅师", "流云公子", "幽冥鬼母",
)
STYLES = ("刚猛体修", "飘逸剑修", "阴狠魔修", "稳健灵修", "诡谲鬼修", "佛门苦修")
INJURIES = ("状态巅峰", "旧伤未愈", "气血略亏", "灵力不稳", "刚经历恶战", "藏有底牌")

STONE_TIERS = (
    {"name": "碎玉原石", "p": 0.05 + 0.010 + 0.0002},
    {"name": "寻常原石", "p": 0.12 + 0.025 + 0.0005},
    {"name": "灵纹原石", "p": 0.26 + 0.045 + 0.001},
    {"name": "秘境原石", "p": 0.44 + 0.075 + 0.002},
    {"name": "太古原石", "p": 0.62 + 0.110 + 0.004},
)


class BettingPoolManager:
    def __init__(self, db: DataBase):
        self.db = db
        # 后台结算与玩家下注可能并发触发，用进程内锁串行化
        self._settle_lock = asyncio.Lock()

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

    @staticmethod
    def _block(ts: Optional[int] = None) -> int:
        return int(ts or time.time()) // POOL_SECONDS

    # ================= 生成 =================
    async def ensure_pools(self, now: Optional[int] = None) -> List[dict]:
        now = int(now or time.time())
        block = self._block(now)
        start = block * POOL_SECONDS
        close_at = min(start + BET_WINDOW, start + POOL_SECONDS - 60)
        for i in range(1, POOLS_PER_BLOCK + 1):
            key = f"duel:{block}:{i}"
            exists = await self._fetchone("SELECT id FROM betting_pools WHERE pool_key = ?", (key,))
            if exists:
                continue
            rng = random.Random(f"pool:{key}")
            name_a, name_b = rng.sample(NPC_NAMES, 2)
            style_a, style_b = rng.sample(STYLES, 2)
            injury_a, injury_b = rng.sample(INJURIES, 2)
            power_a = rng.uniform(0.75, 1.25)
            power_b = rng.uniform(0.75, 1.25)
            p_a = power_a / (power_a + power_b)
            p_a = max(0.20, min(0.80, p_a))
            odds_a = round(HOUSE_RATE / p_a, 2)
            odds_b = round(HOUSE_RATE / (1 - p_a), 2)
            info_a = f"{style_a}｜{injury_a}｜气势 {'强' if power_a > 1.05 else ('弱' if power_a < 0.9 else '平')}"
            info_b = f"{style_b}｜{injury_b}｜气势 {'强' if power_b > 1.05 else ('弱' if power_b < 0.9 else '平')}"
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO betting_pools "
                "(pool_key, kind, title, side_a, side_b, info_a, info_b, odds_a, odds_b, "
                "min_bet, max_bet, open_at, close_at, status) "
                "VALUES (?, 'duel', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                (
                    key, f"第 {i} 场 · {name_a} 对 {name_b}",
                    name_a, name_b, info_a, info_b, odds_a, odds_b,
                    MIN_BET, 500000, start, close_at,
                ),
            )
        await self.db.conn.commit()
        return await self._fetchall(
            "SELECT * FROM betting_pools WHERE pool_key LIKE ? ORDER BY id",
            (f"duel:{block}:%",),
        )

    # ================= 结算 =================
    async def settle_due(self, now: Optional[int] = None) -> int:
        now = int(now or time.time())
        async with self._settle_lock:
            pools = await self._fetchall(
                "SELECT * FROM betting_pools WHERE status = 'open' AND close_at <= ?", (now,)
            )
            settled = 0
            for pool in pools:
                if await self._settle_pool(pool, now):
                    settled += 1
            return settled

    async def _settle_pool(self, pool: dict, now: int) -> bool:
        pool_key = pool["pool_key"]
        rng = random.Random(f"pool-result:{pool_key}")
        if pool.get("kind") == "stone":
            # 赌石盘口：结果由 meta 中的真实概率决定
            try:
                meta = json.loads(pool.get("info_a") or "{}")
            except Exception:
                meta = {}
            p = float(meta.get("p_success") or 0.5)
            result = "A" if rng.random() < p else "B"
        else:
            odds_a = float(pool.get("odds_a") or 1.9)
            p_a = min(0.95, max(0.05, HOUSE_RATE / odds_a))
            result = "A" if rng.random() < p_a else "B"

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 条件更新抢占结算权，防止后台任务与玩家指令重复派彩
            cursor = await self.db.conn.execute(
                "UPDATE betting_pools SET status = 'settled', result = ?, settled_at = ? "
                "WHERE id = ? AND status = 'open'",
                (result, now, pool["id"]),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False
            bets = await self._fetchall(
                "SELECT * FROM betting_bets WHERE pool_id = ? AND status = 'active'", (pool["id"],)
            )
            for bet in bets:
                if bet["side"] == result:
                    odds = float(pool["odds_a"] if result == "A" else pool["odds_b"])
                    payout = int(int(bet["amount"]) * odds)
                    await self.db.conn.execute(
                        "UPDATE players SET gold = gold + ? WHERE user_id = ?",
                        (payout, bet["user_id"]),
                    )
                    await self.db.conn.execute(
                        "UPDATE betting_bets SET status = 'won', payout = ? "
                        "WHERE id = ? AND status = 'active'",
                        (payout, bet["id"]),
                    )
                else:
                    await self.db.conn.execute(
                        "UPDATE betting_bets SET status = 'lost', payout = 0 "
                        "WHERE id = ? AND status = 'active'",
                        (bet["id"],),
                    )
            await self.db.conn.commit()
            return True
        except Exception:
            await self.db.conn.rollback()
            raise

    # ================= 查询 =================
    async def get_board(self, user_id: str, now: Optional[int] = None) -> str:
        now = int(now or time.time())
        await self.settle_due(now)
        pools = await self.ensure_pools(now)
        if not pools:
            return "🎲 当前没有开放的盘口。"
        lines = ["🎲 斗法盘口（NPC 模拟战 · 每 2 小时一轮）", "━━━━━━━━━━━━━━━"]
        for idx, pool in enumerate(pools, 1):
            remaining = max(0, int(pool["close_at"]) - now)
            lines.append(f"[{idx}] {pool['title']}")
            lines.append(f"    A. {pool['side_a']}：{pool['info_a']}｜赔率 {pool['odds_a']}")
            lines.append(f"    B. {pool['side_b']}：{pool['info_b']}｜赔率 {pool['odds_b']}")
            lines.append(f"    ⏳ 下注截止：{remaining // 60} 分 {remaining % 60} 秒后")
            my_bet = await self._fetchone(
                "SELECT side, amount, status, payout FROM betting_bets "
                "WHERE pool_id = ? AND user_id = ?",
                (pool["id"], user_id),
            )
            if my_bet:
                state = {"active": "待开奖", "won": "已中", "lost": "未中"}.get(my_bet["status"], my_bet["status"])
                lines.append(
                    f"    🎫 你的下注：{my_bet['side']} {int(my_bet['amount']):,}（{state}"
                    + (f"，派彩 {int(my_bet['payout']):,}" if my_bet["payout"] else "")
                    + "）"
                )
        lines.extend([
            "━━━━━━━━━━━━━━━",
            "💡 `/盘口 押注 <编号> <A|B> <金额>` 下注（每个盘口限一注）",
            "💡 `/盘口 赌石 <1-5> <出货|切垮> <金额>` 直接对赌原石能否出货",
            "⚠️ 赔率已含庄家抽水，久赌必亏。",
        ])
        return "\n".join(lines)

    # ================= 下注 =================
    async def place_bet(self, player: Player, index: int, side: str, amount: int) -> Tuple[bool, str]:
        now = int(time.time())
        await self.settle_due(now)
        pools = await self.ensure_pools(now)
        if index < 1 or index > len(pools):
            return False, "❌ 无效的盘口编号，请使用 `/盘口` 查看。"
        pool = pools[index - 1]
        if pool["status"] != "open" or now >= int(pool["close_at"]):
            return False, "❌ 该盘口已封盘。"
        side = str(side or "").strip().upper()
        if side not in ("A", "B"):
            return False, "❌ 请选择押 A 或 B。"
        min_bet = int(pool["min_bet"] or MIN_BET)
        max_bet = int(pool["max_bet"] or 500000)
        amount = int(amount)
        if amount < min_bet:
            return False, f"❌ 最低下注 {min_bet:,} 灵石。"
        if amount > max_bet:
            return False, f"❌ 单注最高 {max_bet:,} 灵石。"
        existing = await self._fetchone(
            "SELECT * FROM betting_bets WHERE pool_id = ? AND user_id = ?", (pool["id"], player.user_id)
        )
        if existing:
            return False, "❌ 每个盘口只能下一注，落子无悔。"
        if player.gold < amount:
            return False, f"❌ 灵石不足，你当前只有 {player.gold:,}。"
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 下注与封盘结算可能并发：事务内重读盘口状态，避免封盘后仍插入永不派彩的注单
            fresh_pool = await self._fetchone(
                "SELECT status, close_at FROM betting_pools WHERE id = ?", (pool["id"],)
            )
            if (
                not fresh_pool
                or fresh_pool.get("status") != "open"
                or now >= int(fresh_pool.get("close_at") or 0)
            ):
                await self.db.conn.rollback()
                return False, "❌ 该盘口已封盘。"
            existing = await self._fetchone(
                "SELECT id FROM betting_bets WHERE pool_id = ? AND user_id = ?",
                (pool["id"], player.user_id),
            )
            if existing:
                await self.db.conn.rollback()
                return False, "❌ 每个盘口只能下一注，落子无悔。"
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (amount, player.user_id, amount),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 灵石不足，下注失败。"
            await self.db.conn.execute(
                "INSERT INTO betting_bets (pool_id, user_id, side, amount, status, created_at) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                (pool["id"], player.user_id, side, amount, now),
            )
            await self.db.conn.commit()
            player.gold -= amount
        except Exception:
            await self.db.conn.rollback()
            raise
        odds = pool["odds_a"] if side == "A" else pool["odds_b"]
        return True, (
            f"🎲 下注成功：{pool['title']} → {side}\n"
            f"金额：{amount:,} 灵石｜赔率：{odds}\n"
            f"💡 封盘后自动结算，发送 `/盘口` 可查看结果。"
        )

    async def bet_stone(self, player: Player, tier: int, side: str, amount: int) -> Tuple[bool, str]:
        tier = int(tier or 0)
        if tier < 1 or tier > len(STONE_TIERS):
            return False, "❌ 原石档位只能是 1-5。"
        side = str(side or "").strip()
        if side not in ("出货", "切垮"):
            return False, "❌ 请选择押「出货」或「切垮」。"
        amount = int(amount)
        if amount < MIN_BET:
            return False, f"❌ 最低下注 {MIN_BET:,} 灵石。"
        if amount > 500000:
            return False, "❌ 单注最高 500,000 灵石。"
        if player.gold < amount:
            return False, f"❌ 灵石不足，你当前只有 {player.gold:,}。"
        cfg = STONE_TIERS[tier - 1]
        p_success = max(0.02, min(0.95, float(cfg["p"])))
        p_bet = p_success if side == "出货" else (1 - p_success)
        odds = max(1.05, round(HOUSE_RATE / p_bet, 2))
        now = int(time.time())
        rng = random.Random(f"stone-bet:{player.user_id}:{now}:{amount}:{tier}:{side}")
        success = rng.random() < p_success
        win = (side == "出货" and success) or (side == "切垮" and not success)
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (amount, player.user_id, amount),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 灵石不足，下注失败。"
            payout = int(amount * odds) if win else 0
            if payout:
                await self.db.conn.execute(
                    "UPDATE players SET gold = gold + ? WHERE user_id = ?",
                    (payout, player.user_id),
                )
            await self.db.conn.commit()
            player.gold = player.gold - amount + payout
        except Exception:
            await self.db.conn.rollback()
            raise
        result_text = "出货" if success else "切垮"
        return True, (
            f"🎲 赌石盘口 · {cfg['name']}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"你押：{side} {amount:,} 灵石｜赔率：{odds}\n"
            f"开奖：{result_text}\n"
            f"{'🎉 命中！派彩 ' + format(payout, ',') + ' 灵石' if win else '💀 未命中，赌注尽失'}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 当前灵石：{player.gold:,}"
        )