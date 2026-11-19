# managers/invest_agent_manager.py
"""仙市投资代理人：玩家把本金委托给 NPC，按策略自动调仓。

设计边界（MVP）：
- 只做仙市股票/基金，不做杠杆、不做空；
- 一个玩家同时只能有一个进行中的委托；
- 股票按整手买卖，基金份额保留 2 位小数；
- 委托开始于下一盘，避免用当前盘已知结果套利；
- 结算全部走事务，领取幂等，离线补结算不会重复派彩。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

from ..data import DataBase
from ..models import Player
from .invest_manager import (
    CREDIBILITY_SCALE,
    LOAN_LIMIT_FOR_STOCK,
    LOT_SIZE,
    NEWS_BIAS_SCALE,
    SHARE_UNIT,
    InvestManager,
)

# 交易滑点：买入价上浮、卖出价下调，避免代理人在盘末无成本来回切仓
SLIPPAGE = 0.0015
# 提前停止违约金：本金比例
EARLY_STOP_PENALTY_RATE = 0.01
# 目标与当前仓位偏差低于该比例时不调仓，减少手续费损耗
REBALANCE_MIN_GAP = 0.03

AGENT_DEFS: Dict[str, Dict[str, Any]] = {
    "qinghe": {
        "key": "qinghe",
        "name": "青禾",
        "title": "稳健账房",
        "risk": 1,
        "desc": "灵田出身，偏爱稳当的基金和低波动股票，宁可少赚也不愿大亏。",
        "min_principal": 10_000,
        "max_principal": 1_000_000,
        "durations": (8, 16, 32),
        "stock_cap": 0.30,
        "fund_cap": 0.60,
        "cash_floor": 0.10,
        "stop_loss": 0.06,
        "take_profit": 0.10,
        "rebalance_every": 4,
        "management_fee": 0.008,
        "profit_share": 0.08,
        "stock_count": 2,
        "fund_codes": ("F01", "F02"),
        "risk_aversion": 1.2,
        "risk_pref": -0.5,
    },
    "xuanheng": {
        "key": "xuanheng",
        "name": "玄衡",
        "title": "均衡算师",
        "risk": 2,
        "desc": "散修算师，股基搭配、按盘面调仓，追求稳中带进。",
        "min_principal": 10_000,
        "max_principal": 1_000_000,
        "durations": (8, 16, 32),
        "stock_cap": 0.55,
        "fund_cap": 0.30,
        "cash_floor": 0.15,
        "stop_loss": 0.10,
        "take_profit": 0.18,
        "rebalance_every": 2,
        "management_fee": 0.010,
        "profit_share": 0.12,
        "stock_count": 3,
        "fund_codes": ("F02", "F03"),
        "risk_aversion": 0.6,
        "risk_pref": 0.0,
    },
    "chixiao": {
        "key": "chixiao",
        "name": "赤霄",
        "title": "激进赌徒",
        "risk": 3,
        "desc": "赌坊出身，敢在高波动股票上重仓，回撤大，但一旦踩中行情也最猛。",
        "min_principal": 10_000,
        "max_principal": 1_000_000,
        "durations": (8, 16, 32),
        "stock_cap": 0.75,
        "fund_cap": 0.15,
        "cash_floor": 0.10,
        "stop_loss": 0.18,
        "take_profit": 0.35,
        "rebalance_every": 1,
        "management_fee": 0.015,
        "profit_share": 0.20,
        "stock_count": 3,
        "fund_codes": ("F03",),
        "risk_aversion": 0.2,
        "risk_pref": 0.5,
    },
}

AGENT_ALIASES = {
    "青禾": "qinghe",
    "稳健": "qinghe",
    "1": "qinghe",
    "玄衡": "xuanheng",
    "均衡": "xuanheng",
    "2": "xuanheng",
    "赤霄": "chixiao",
    "激进": "chixiao",
    "3": "chixiao",
}

# ===== 代理人等级 =====
# 每个玩家对每个代理人独立成长；等级不回退，经验可以被亏损扣到本级下限。
LEVEL_MAX = 5
LEVEL_THRESHOLDS = (0, 80, 240, 600, 1200)
LEVEL_TITLES = ("初识", "熟客", "搭档", "信赖", "知己")
# 本金上限随等级成长，最终到 100 万；代理人自身 max_principal 仍是硬上限。
LEVEL_PRINCIPAL_CAPS = (200_000, 350_000, 500_000, 750_000, 1_000_000)
LEVEL_FEE_DISCOUNT = 0.05      # 每级管理费相对减免
LEVEL_SHARE_DISCOUNT = 0.01    # 每级盈利分成绝对减免（百分点）


def level_for_exp(exp: int) -> int:
    """累计经验对应的等级。"""
    level = 1
    for idx, threshold in enumerate(LEVEL_THRESHOLDS, 1):
        if int(exp) >= threshold:
            level = idx
    return min(level, LEVEL_MAX)


def level_floor(level: int) -> int:
    """该等级的累计经验下限。"""
    return LEVEL_THRESHOLDS[max(1, min(int(level), LEVEL_MAX)) - 1]


def level_span(level: int) -> int:
    """升到下一级所需的等级内经验；满级返回 0。"""
    level = max(1, min(int(level), LEVEL_MAX))
    if level >= LEVEL_MAX:
        return 0
    return LEVEL_THRESHOLDS[level] - LEVEL_THRESHOLDS[level - 1]


def effective_fee(agent: dict, level: int) -> float:
    rate = float(agent["management_fee"]) * (1.0 - LEVEL_FEE_DISCOUNT * (max(1, level) - 1))
    return max(0.0, rate)


def effective_share(agent: dict, level: int) -> float:
    rate = float(agent["profit_share"]) - LEVEL_SHARE_DISCOUNT * (max(1, level) - 1)
    return max(0.0, rate)


def principal_cap(agent: dict, level: int) -> int:
    return min(int(agent["max_principal"]), LEVEL_PRINCIPAL_CAPS[max(1, level) - 1])


def exp_delta(
    principal: int, payout: int, start_tick: int, end_tick: int, close_tick: int
) -> int:
    """结算经验：完成给基础经验，盈亏按比例增减，提前停止按已运行比例折算。

    - 基础经验 = 委托期限（盘数）；
    - 收益率每 1% 增减 0.1 倍基础经验，收益率夹在 ±30%；
    - 提前停止按已运行盘数折算，防止开仓即停刷经验。
    """
    duration = max(1, int(end_tick) - int(start_tick))
    progress = (min(int(end_tick), int(close_tick)) - int(start_tick) + 1) / duration
    progress = max(0.0, min(1.0, progress))
    rate = (float(payout) / float(principal) - 1.0) if principal else 0.0
    rate = max(-0.30, min(0.30, rate))
    return int(round(duration * (1.0 + rate * 10.0) * progress))


def _agent_name(key: str) -> str:
    return (AGENT_DEFS.get(key) or {}).get("name", key)


def _floor_shares(value: float) -> float:
    return math.floor(float(value) * SHARE_UNIT + 1e-9) / SHARE_UNIT


def _pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


class InvestAgentManager:
    """仙市投资代理人的委托、调仓与结算。"""

    def __init__(self, db: DataBase, invest_mgr: InvestManager):
        self.db = db
        self.invest_mgr = invest_mgr
        self._op_lock = asyncio.Lock()
        self._price_cache: Dict[str, List[float]] = {}
        self._pending_logs: List[tuple] = []

    # ===== 公共查询 =====

    @staticmethod
    def _agent_view(agent: dict, level_row: Optional[dict]) -> dict:
        level = int((level_row or {}).get("level") or 1)
        level = max(1, min(LEVEL_MAX, level))
        exp = max(0, int((level_row or {}).get("exp") or 0))
        floor = level_floor(level)
        span = level_span(level)
        in_level = max(0, exp - floor)
        return {
            "key": agent["key"],
            "name": agent["name"],
            "title": agent["title"],
            "risk": agent["risk"],
            "desc": agent["desc"],
            "min_principal": agent["min_principal"],
            "max_principal": principal_cap(agent, level),
            "base_max_principal": agent["max_principal"],
            "durations": list(agent["durations"]),
            "stop_loss": agent["stop_loss"],
            "take_profit": agent["take_profit"],
            "management_fee": effective_fee(agent, level),
            "profit_share": effective_share(agent, level),
            "base_management_fee": agent["management_fee"],
            "base_profit_share": agent["profit_share"],
            "level": level,
            "level_max": LEVEL_MAX,
            "level_title": LEVEL_TITLES[level - 1],
            "exp": exp,
            "exp_in_level": in_level,
            "level_span": span,
            "exp_progress": (in_level / span) if span > 0 else 1.0,
            "exp_to_next": max(0, span - in_level) if span > 0 else 0,
        }

    @staticmethod
    def public_agents() -> List[dict]:
        """未登录上下文使用的默认视图（全等级 1）。"""
        return [InvestAgentManager._agent_view(d, None) for d in AGENT_DEFS.values()]

    async def _levels(self, user_id: str) -> Dict[str, dict]:
        rows = await self._rows(
            "SELECT * FROM invest_agent_levels WHERE user_id = ?", (user_id,)
        )
        return {str(r["agent_key"]): r for r in rows}

    async def get_snapshot(self, user_id: str) -> dict:
        """返回代理人列表、当前委托、待领取与历史，供指令和网页共用。"""
        await self.settle_due(user_id)
        products = {p["code"]: p for p in await self.invest_mgr._load_products()}
        levels = await self._levels(user_id)
        active = await self._row(
            "SELECT * FROM invest_agent_mandates "
            "WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        pending_rows = await self._rows(
            "SELECT * FROM invest_agent_mandates "
            "WHERE user_id = ? AND status = 'pending' ORDER BY id DESC",
            (user_id,),
        )
        history_rows = await self._rows(
            "SELECT * FROM invest_agent_mandates "
            "WHERE user_id = ? AND status = 'claimed' "
            "ORDER BY id DESC LIMIT 10",
            (user_id,),
        )
        return {
            "ok": True,
            "agents": [
                self._agent_view(d, levels.get(d["key"])) for d in AGENT_DEFS.values()
            ],
            "active": self._mandate_payload(active, products) if active else None,
            "pending": [self._mandate_payload(r, products) for r in pending_rows],
            "history": [self._mandate_payload(r, products) for r in history_rows],
        }

    async def status_text(self, user_id: str) -> str:
        snap = await self.get_snapshot(user_id)
        lines = ["「 仙市投资代理人 」", "━━━━━━━━━━━━━━━━━━━━━━━━"]
        for a in snap["agents"]:
            progress = (
                "满级"
                if a["level"] >= a["level_max"]
                else f"{a['exp_in_level']}/{a['level_span']}"
            )
            lines.append(
                f"{a['name']} Lv.{a['level']} {a['level_title']}（{progress}）  "
                f"管理费 {a['management_fee']:.2%} / 分成 {a['profit_share']:.0%}"
            )
            lines.append(
                f"　风险 {a['risk']} · 本金上限 {a['max_principal']:,} 灵石"
            )
        lines.append("经验：完成委托按盈亏获得，亏损会扣经验；升级降低费用并提高本金上限。")
        active = snap.get("active")
        if active:
            elapsed = max(0, active["last_settle_tick"] - active["start_tick"] + 1)
            total = max(1, active["end_tick"] - active["start_tick"])
            lines.extend(
                [
                    "━━━━━━━━━━━━━━━━━━━━━━━━",
                    f"进行中：{active['agent_name']} · 本金 {active['principal']:,} 灵石",
                    f"当前市值 {active['value']:,} 灵石（{_pct(active['pnl_pct'])}）",
                    f"已运行 {elapsed} / {total} 盘，剩余 {active['remaining_ticks']} 盘",
                    f"止损线 {active['stop_loss_value']:,}　止盈线 {active['take_profit_value']:,}",
                ]
            )
            positions = active.get("positions") or []
            if positions:
                lines.append("持仓：" + "、".join(
                    f"{p['name']} {p['shares']:g}" for p in positions
                ))
            lines.append("指令：/仙市 代理 状态　/仙市 代理 停止")
        else:
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("当前没有进行中的委托。")
            lines.append("用法：/仙市 代理 雇佣 <青禾|玄衡|赤霄> <本金> <8|16|32>")
        pending = snap.get("pending") or []
        if pending:
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
            for item in pending[:3]:
                exp_text = (
                    f"，经验 {item['exp_delta']:+d}" if item.get("exp_delta") else ""
                )
                lines.append(
                    f"待领取：{item['agent_name']} 第 {item['id']} 号，"
                    f"实得 {item['payout']:,} 灵石（{item['close_reason']}{exp_text}）"
                )
            lines.append("领取：/仙市 代理 领取")
        return "\n".join(lines)

    async def history_text(self, user_id: str) -> str:
        rows = await self._rows(
            "SELECT * FROM invest_agent_mandates "
            "WHERE user_id = ? AND status IN ('pending', 'claimed') "
            "ORDER BY id DESC LIMIT 10",
            (user_id,),
        )
        if not rows:
            return "📭 暂无代理人委托记录。"
        lines = ["「 代理人委托记录 · 最近 10 条 」", "━━━━━━━━━━━━━━━━━━━━━━━━"]
        for row in rows:
            delta = int(row.get("exp_delta") or 0)
            exp_text = f"　经验 {delta:+d}" if delta else ""
            lines.append(
                f"#{row['id']} {_agent_name(row['agent_key'])}  "
                f"本金 {int(row['principal']):,} → 实得 {int(row['payout']):,}  "
                f"({row['close_reason'] or row['status']}){exp_text}"
            )
        return "\n".join(lines)

    # ===== 委托生命周期 =====

    async def start(self, player: Player, agent_key: str, principal: int, duration: int) -> Tuple[bool, str]:
        key = AGENT_ALIASES.get(str(agent_key).strip(), str(agent_key).strip().lower())
        agent = AGENT_DEFS.get(key)
        if not agent:
            return False, "❌ 代理人只能是：青禾 / 玄衡 / 赤霄。"
        try:
            principal = int(principal)
            duration = int(duration)
        except (TypeError, ValueError):
            return False, "❌ 本金和期限必须是整数。"
        if duration not in agent["durations"]:
            return False, f"❌ {agent['name']} 的期限只能是 {'/'.join(str(x) for x in agent['durations'])} 盘。"
        if principal < agent["min_principal"]:
            return False, f"❌ {agent['name']} 的最低本金是 {agent['min_principal']:,} 灵石。"
        if principal > agent["max_principal"]:
            return False, f"❌ {agent['name']} 的单笔本金上限是 {agent['max_principal']:,} 灵石。"

        async with self._op_lock:
            await self.invest_mgr.settle_to()
            active = await self._scalar(
                "SELECT COUNT(*) FROM invest_agent_mandates WHERE user_id = ? AND status = 'active'",
                (player.user_id,),
            )
            if int(active or 0) > 0:
                return False, "❌ 你已有一个进行中的代理人委托，请先等待结算或停止。"
            loan = await self.invest_mgr._active_loan(player.user_id)
            if loan > LOAN_LIMIT_FOR_STOCK:
                return False, (
                    f"❌ 你当前贷款余额 {loan:,} 灵石，超过 {LOAN_LIMIT_FOR_STOCK:,} 的入市门槛。\n"
                    "请先还款后再委托代理人。"
                )

            level_row = await self._row(
                "SELECT * FROM invest_agent_levels WHERE user_id = ? AND agent_key = ?",
                (player.user_id, key),
            )
            level = max(1, min(LEVEL_MAX, int((level_row or {}).get("level") or 1)))
            cap = principal_cap(agent, level)
            if principal > cap:
                return False, (
                    f"❌ {agent['name']} 当前 Lv.{level} 的单笔本金上限是 {cap:,} 灵石。\n"
                    "完成委托积累经验可提高上限。"
                )
            fee_rate = effective_fee(agent, level)
            fee = max(1, int(round(principal * fee_rate)))
            if player.gold < principal:
                return False, (
                    f"❌ 委托需要本金 {principal:,} 灵石（管理费 {fee:,} 从本金中扣），"
                    f"你当前只有 {player.gold:,}。"
                )
            tick = self.invest_mgr.tick_of()
            start_tick = tick + 1
            end_tick = start_tick + duration
            now = int(time.time())
            cash = principal - fee
            await self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.conn.execute(
                    "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                    (principal, player.user_id, principal),
                )
                if not cursor.rowcount:
                    await self.db.conn.rollback()
                    return False, "❌ 灵石不足，委托失败。"
                await self.db.conn.execute(
                    "INSERT INTO invest_agent_mandates "
                    "(user_id, agent_key, principal, cash, positions_json, start_tick, end_tick, "
                    " last_settle_tick, status, management_fee, created_at) "
                    "VALUES (?, ?, ?, ?, '{}', ?, ?, ?, 'active', ?, ?)",
                    (player.user_id, key, principal, cash, start_tick, end_tick, start_tick - 1, fee, now),
                )
                await self.db.conn.commit()
            except Exception:
                await self.db.conn.rollback()
                # 唯一索引兜底：并发下另一个请求已抢到 active
                return False, "❌ 已有进行中的代理人委托。"
            return True, (
                f"✅ 已委托 {agent['name']}（Lv.{level} {LEVEL_TITLES[level - 1]}）。\n"
                f"本金 {principal:,}，管理费 {fee:,}（{fee_rate:.2%}），"
                f"第 {start_tick} 盘开始，共 {duration} 盘。\n"
                f"止损 {agent['stop_loss']:.0%} / 止盈 {agent['take_profit']:.0%}，"
                "不保本，管理费不退。"
            )

    async def stop(self, player: Player) -> Tuple[bool, str]:
        async with self._op_lock:
            await self.invest_mgr.settle_to()
            row = await self._row(
                "SELECT * FROM invest_agent_mandates "
                "WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (player.user_id,),
            )
            if not row:
                return False, "❌ 你没有进行中的代理人委托。"
            tick = self.invest_mgr.tick_of()
            if tick < int(row["start_tick"]):
                return False, "❌ 委托尚未开始，至少要等到下一盘才能停止。"
            await self._settle_one(row, tick, force_reason="stopped")
            return True, "✅ 已停止代理人委托并清仓，请发送 /仙市 代理 领取 结算灵石。"

    async def claim(self, player: Player) -> Tuple[bool, str]:
        async with self._op_lock:
            rows = await self._rows(
                "SELECT * FROM invest_agent_mandates "
                "WHERE user_id = ? AND status = 'pending' ORDER BY id",
                (player.user_id,),
            )
            if not rows:
                return False, "❌ 没有待领取的代理人结算。"
            payout = sum(max(0, int(r["payout"] or 0)) for r in rows)
            now = int(time.time())
            await self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.conn.execute(
                    "UPDATE invest_agent_mandates SET status = 'claimed', claimed_at = ? "
                    "WHERE user_id = ? AND status = 'pending'",
                    (now, player.user_id),
                )
                if not cursor.rowcount:
                    await self.db.conn.rollback()
                    return False, "❌ 该委托已被领取。"
                await self.db.conn.execute(
                    "UPDATE players SET gold = gold + ? WHERE user_id = ?",
                    (payout, player.user_id),
                )
                await self.db.conn.commit()
            except Exception:
                await self.db.conn.rollback()
                raise
            return True, f"✅ 已领取 {len(rows)} 笔代理人结算，共 {payout:,} 灵石。"

    # ===== 结算 =====

    async def settle_due(self, user_id: Optional[str] = None) -> int:
        """把到期/需要调仓的委托推进到当前盘，返回处理数量。"""
        async with self._op_lock:
            await self.invest_mgr.settle_to()
            tick = self.invest_mgr.tick_of()
            if user_id:
                rows = await self._rows(
                    "SELECT * FROM invest_agent_mandates "
                    "WHERE status = 'active' AND user_id = ? ORDER BY id",
                    (user_id,),
                )
            else:
                rows = await self._rows(
                    "SELECT * FROM invest_agent_mandates WHERE status = 'active' ORDER BY id"
                )
            handled = 0
            for row in rows:
                if tick < int(row["start_tick"]):
                    continue
                if tick <= int(row["last_settle_tick"]):
                    continue
                try:
                    await self._settle_one(row, tick)
                    handled += 1
                except Exception as e:
                    logger.error(f"【仙市代理】委托 #{row['id']} 结算异常: {e}", exc_info=True)
            return handled

    async def _settle_one(self, row: dict, current_tick: int, force_reason: str = "") -> None:
        """把单个委托从 last_settle_tick 推进到 current_tick。

        调用方需持有 _op_lock。所有价格在事务外读取，事务内只做组合更新。
        """
        mandate_id = int(row["id"])
        agent = AGENT_DEFS.get(str(row["agent_key"]))
        if not agent:
            return
        products = {p["code"]: p for p in await self.invest_mgr._load_products()}
        prices = await self._prices_at(current_tick, products)
        news = await self._news_at(current_tick)
        await self._load_price_cache()
        start_tick = int(row["start_tick"])
        end_tick = int(row["end_tick"])
        last_tick = int(row["last_settle_tick"])
        cash = int(row["cash"])
        positions = self._load_positions(row["positions_json"])
        principal = int(row["principal"])
        self._pending_logs.clear()

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = await self._row(
                "SELECT * FROM invest_agent_mandates WHERE id = ? AND status = 'active'",
                (mandate_id,),
            )
            if not fresh:
                await self.db.conn.rollback()
                return
            # 事务内复查，避免并发重复推进
            if int(fresh["last_settle_tick"]) >= current_tick and not force_reason:
                await self.db.conn.rollback()
                return
            # 提前停止只按当前盘结算；正常推进逐盘补算。
            ticks = [current_tick] if force_reason else range(max(last_tick + 1, start_tick), current_tick + 1)
            for tick in ticks:
                if force_reason:
                    reason = force_reason
                elif tick >= end_tick:
                    reason = "matured"
                else:
                    value = self._total_value(cash, positions, prices)
                    if value <= principal * (1.0 - agent["stop_loss"]):
                        reason = "stop_loss"
                    elif value >= principal * (1.0 + agent["take_profit"]):
                        reason = "take_profit"
                    else:
                        reason = ""
                if reason:
                    level_row = await self._row(
                        "SELECT * FROM invest_agent_levels WHERE user_id = ? AND agent_key = ?",
                        (fresh["user_id"], fresh["agent_key"]),
                    )
                    level = max(1, min(LEVEL_MAX, int((level_row or {}).get("level") or 1)))
                    share = effective_share(agent, level)
                    cash, final_value, profit_fee, penalty = self._liquidate(
                        cash, positions, prices, principal,
                        {**agent, "profit_share": share}, reason,
                    )
                    payout = max(0, final_value - profit_fee - penalty)
                    now = int(time.time())
                    delta = exp_delta(principal, payout, start_tick, end_tick, tick)
                    new_level, _new_exp, applied = await self._apply_exp(
                        fresh["user_id"], str(fresh["agent_key"]), delta, now
                    )
                    await self._flush_logs()
                    await self._log(
                        mandate_id, tick, reason, "", 0, 0.0,
                        self._reason_text(reason), final_value,
                    )
                    if applied:
                        await self._log(
                            mandate_id, tick, "exp", "", applied, 0.0,
                            f"经验 {applied:+d}（Lv.{new_level}）", final_value,
                        )
                    await self.db.conn.execute(
                        "UPDATE invest_agent_mandates SET cash = ?, positions_json = '{}', "
                        "last_settle_tick = ?, status = 'pending', close_reason = ?, "
                        "profit_fee = ?, final_value = ?, payout = ?, exp_delta = ?, "
                        "settled_at = ? WHERE id = ? AND status = 'active'",
                        (final_value, tick, reason, profit_fee, final_value, payout,
                         applied, now, mandate_id),
                    )
                    await self.db.conn.commit()
                    return

                # 开始盘或到调仓周期：按策略重新配置
                if tick == start_tick or (tick - start_tick) % int(agent["rebalance_every"]) == 0:
                    cash, positions = self._rebalance(
                        mandate_id, tick, agent, cash, positions, products, prices, news
                    )
                    await self._log(
                        mandate_id, tick, "rebalance", "", 0, 0.0,
                        "按策略调仓", self._total_value(cash, positions, prices),
                    )

            await self._flush_logs()
            await self.db.conn.execute(
                "UPDATE invest_agent_mandates SET cash = ?, positions_json = ?, "
                "last_settle_tick = ? WHERE id = ? AND status = 'active'",
                (cash, json.dumps(positions, ensure_ascii=False), current_tick, mandate_id),
            )
            await self.db.conn.commit()
        except Exception:
            self._pending_logs.clear()
            await self.db.conn.rollback()
            raise

    async def _apply_exp(
        self, user_id: str, agent_key: str, delta: int, now: int
    ) -> Tuple[int, int, int]:
        """在结算事务内累加/扣减经验，返回 (新等级, 新经验, 实际变动)。

        等级只升不降：亏损把经验扣到当前等级下限为止，避免一次回撤抹掉长期进度。
        """
        row = await self._row(
            "SELECT * FROM invest_agent_levels WHERE user_id = ? AND agent_key = ?",
            (user_id, agent_key),
        )
        if row:
            exp = int(row["exp"] or 0)
            level = max(1, min(LEVEL_MAX, int(row["level"] or 1)))
            level = max(level, level_for_exp(exp))
            exp = max(exp, level_floor(level))
        else:
            exp = 0
            level = 1
        before = exp
        if delta < 0:
            exp = max(level_floor(level), exp + int(delta))
        else:
            exp += int(delta)
        level = max(level, level_for_exp(exp))
        exp = max(exp, level_floor(level))
        if row:
            await self.db.conn.execute(
                "UPDATE invest_agent_levels SET exp = ?, level = ?, updated_at = ? "
                "WHERE user_id = ? AND agent_key = ?",
                (exp, level, now, user_id, agent_key),
            )
        else:
            await self.db.conn.execute(
                "INSERT INTO invest_agent_levels (user_id, agent_key, exp, level, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, agent_key, exp, level, now),
            )
        return level, exp, exp - before

    # ===== 组合计算 =====

    def _rebalance(
        self,
        mandate_id: int,
        tick: int,
        agent: dict,
        cash: int,
        positions: Dict[str, dict],
        products: Dict[str, dict],
        prices: Dict[str, float],
        news: Dict[str, float],
    ) -> Tuple[int, Dict[str, dict]]:
        total = self._total_value(cash, positions, prices)
        if total <= 0:
            return cash, positions
        stock_budget = total * float(agent["stock_cap"])
        fund_budget = total * float(agent["fund_cap"])
        stock_codes = self._pick_stocks(agent, products, prices, news)
        fund_codes = [c for c in agent["fund_codes"] if c in products and prices.get(c, 0) > 0]
        stock_slots = max(1, len(stock_codes))
        fund_slots = max(1, len(fund_codes))
        targets: Dict[str, float] = {}
        for code in stock_codes:
            targets[code] = stock_budget / stock_slots
        for code in fund_codes:
            targets[code] = fund_budget / fund_slots

        # 先卖：清掉不在目标里的持仓，或超出目标 3% 的部分
        for code in list(positions.keys()):
            price = prices.get(code, 0.0)
            if price <= 0:
                continue
            current = float(positions[code].get("shares", 0)) * price
            target = targets.get(code, 0.0)
            if current <= target + total * REBALANCE_MIN_GAP and code in targets:
                continue
            sell_value = current if code not in targets else max(0.0, current - target)
            cash, positions = self._sell(mandate_id, tick, code, sell_value, cash, positions, prices)

        # 再买：补到目标附近，留出现金底线
        for code, target in targets.items():
            price = prices.get(code, 0.0)
            if price <= 0:
                continue
            current = float(positions.get(code, {}).get("shares", 0)) * price
            gap = target - current
            if gap <= total * REBALANCE_MIN_GAP:
                continue
            cash = self._buy(mandate_id, tick, code, gap, cash, positions, products, prices)
        return cash, positions

    def _pick_stocks(
        self, agent: dict, products: Dict[str, dict], prices: Dict[str, float],
        news: Dict[str, float],
    ) -> List[str]:
        scored = []
        for code, product in products.items():
            if product.get("kind") != "stock" or product.get("status") != "trading":
                continue
            price = prices.get(code, 0.0)
            if price <= 0:
                continue
            closes = self._closes(code, 16)
            if not closes:
                closes = [price]
            momentum = price / closes[-5] - 1 if len(closes) >= 5 and closes[-5] else 0.0
            ma = sum(closes) / len(closes)
            reversion = (ma - price) / ma if ma else 0.0
            returns = [
                closes[i] / closes[i - 1] - 1
                for i in range(1, len(closes)) if closes[i - 1]
            ]
            vol = (sum((r - sum(returns) / len(returns)) ** 2 for r in returns) / len(returns)) ** 0.5 if returns else 0.0
            bias = news.get(code, 0.0) + news.get("*", 0.0) * 0.25
            score = (
                0.45 * momentum + 0.30 * reversion + 0.25 * bias
                - float(agent["risk_aversion"]) * vol * 0.5
                + float(agent["risk_pref"]) * vol * 0.1
            )
            scored.append((score, code))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [code for _, code in scored[: int(agent["stock_count"])]]

    def _buy(
        self, mandate_id: int, tick: int, code: str, amount: float, cash: int,
        positions: Dict[str, dict], products: Dict[str, dict], prices: Dict[str, float],
    ) -> int:
        price = prices.get(code, 0.0)
        if price <= 0 or amount <= 0:
            return cash
        kind = products.get(code, {}).get("kind", "stock")
        spend = min(float(cash), amount)
        if kind == "stock":
            lots = int(spend // (price * (1 + SLIPPAGE) * LOT_SIZE))
            shares = lots * LOT_SIZE
            cost = int(round(shares * price * (1 + SLIPPAGE)))
        else:
            shares = _floor_shares(spend / (price * (1 + SLIPPAGE)))
            if shares < 0.01:
                return cash
            cost = int(round(shares * price * (1 + SLIPPAGE)))
        if shares <= 0 or cost <= 0 or cost > cash:
            return cash
        pos = positions.setdefault(code, {"kind": kind, "shares": 0, "cost": 0})
        pos["shares"] = int(pos.get("shares", 0)) + shares if kind == "stock" else round(float(pos.get("shares", 0)) + shares, 2)
        pos["cost"] = int(pos.get("cost", 0)) + cost
        self._log_soon(mandate_id, tick, "buy", code, shares, price, f"买入 {shares:g}", cash - cost)
        return cash - cost

    def _sell(
        self, mandate_id: int, tick: int, code: str, amount: float, cash: int,
        positions: Dict[str, dict], prices: Dict[str, float],
    ) -> Tuple[int, Dict[str, dict]]:
        pos = positions.get(code)
        price = prices.get(code, 0.0)
        if not pos or price <= 0 or amount <= 0:
            return cash, positions
        kind = pos.get("kind", "stock")
        held = float(pos.get("shares", 0))
        if kind == "stock":
            shares = min(int(held), int(amount // price))
            shares = max(0, shares)
        else:
            shares = min(held, _floor_shares(amount / price))
        if shares <= 0:
            return cash, positions
        proceeds = int(round(shares * price * (1 - SLIPPAGE)))
        pos["shares"] = int(held - shares) if kind == "stock" else round(held - shares, 2)
        if pos["shares"] <= 0:
            positions.pop(code, None)
        self._log_soon(mandate_id, tick, "sell", code, shares, price, f"卖出 {shares:g}", cash + proceeds)
        return cash + proceeds, positions

    def _liquidate(
        self, cash: int, positions: Dict[str, dict], prices: Dict[str, float],
        principal: int, agent: dict, reason: str,
    ) -> Tuple[int, int, int, int]:
        for code, pos in list(positions.items()):
            price = prices.get(code, 0.0)
            if price <= 0:
                continue
            shares = float(pos.get("shares", 0))
            if shares > 0:
                cash += int(round(shares * price * (1 - SLIPPAGE)))
        final_value = int(round(cash))
        profit_fee = int(round(max(0, final_value - principal) * float(agent["profit_share"])))
        penalty = int(round(principal * EARLY_STOP_PENALTY_RATE)) if reason == "stopped" else 0
        return cash, final_value, profit_fee, max(0, penalty)

    @staticmethod
    def _total_value(cash: int, positions: Dict[str, dict], prices: Dict[str, float]) -> float:
        total = float(cash)
        for code, pos in positions.items():
            total += float(pos.get("shares", 0)) * float(prices.get(code, 0.0))
        return total

    # ===== 数据读取 =====

    async def _prices_at(self, tick: int, products: Dict[str, dict]) -> Dict[str, float]:
        rows = await self._rows("SELECT code, close FROM invest_prices WHERE tick = ?", (tick,))
        prices = {str(r["code"]): float(r["close"]) for r in rows}
        for code, product in products.items():
            if code not in prices:
                try:
                    prices[code] = float(product.get("price") or 0)
                except (TypeError, ValueError):
                    prices[code] = 0.0
        return prices

    async def _news_at(self, tick: int) -> Dict[str, float]:
        day, slot = divmod(tick, 16)
        rows = await self._rows(
            "SELECT code, credibility, narrative_direction, direction, strength "
            "FROM invest_events WHERE trade_day = ? AND slot = ?",
            (day, slot),
        )
        result = {}
        for row in rows:
            try:
                # 代理人按「标题叙事方向」理解风声，和玩家看到的是同一层信息；
                # 传闻再按可信度打折，避免 NPC 直接读到底层真实方向。
                direction = int(
                    row.get("narrative_direction") or row.get("direction") or 0
                )
                cred = str(row.get("credibility") or "confirmed")
                result[str(row["code"])] = (
                    float(direction)
                    * float(row["strength"] or 1)
                    * CREDIBILITY_SCALE.get(cred, 1.0)
                    * NEWS_BIAS_SCALE
                )
            except (TypeError, ValueError):
                continue
        return result

    def _closes(self, code: str, limit: int) -> List[float]:
        return self._price_cache.get(code, [])[-limit:]

    async def _load_price_cache(self) -> None:
        self._price_cache = {}
        rows = await self._rows(
            "SELECT code, close FROM invest_prices ORDER BY tick DESC LIMIT 400"
        )
        for row in rows:
            self._price_cache.setdefault(str(row["code"]), []).append(float(row["close"]))
        for code in self._price_cache:
            self._price_cache[code].reverse()

    # ===== 工具 =====

    @staticmethod
    def _load_positions(raw: Any) -> Dict[str, dict]:
        try:
            data = json.loads(raw or "{}")
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _reason_text(reason: str) -> str:
        return {
            "matured": "到期结算",
            "stop_loss": "触发止损",
            "take_profit": "触发止盈",
            "stopped": "提前停止",
        }.get(reason, reason)

    def _log_soon(self, mandate_id: int, tick: int, action: str, code: str,
                  amount: float, price: float, note: str, value_after: int) -> None:
        """记录交易；日志失败不影响结算，后续由 _settle_one 统一 commit。"""
        try:
            self._pending_logs.append(
                (mandate_id, tick, action, code, float(amount), float(price), note, int(value_after), int(time.time()))
            )
        except Exception:
            pass

    async def _flush_logs(self) -> None:
        if not self._pending_logs:
            return
        rows = self._pending_logs
        self._pending_logs = []
        for row in rows:
            await self.db.conn.execute(
                "INSERT INTO invest_agent_logs "
                "(mandate_id, tick, action, code, amount, price, note, value_after, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )

    async def _log(self, mandate_id: int, tick: int, action: str, code: str,
                   amount: float, price: float, note: str, value_after: int) -> None:
        await self.db.conn.execute(
            "INSERT INTO invest_agent_logs "
            "(mandate_id, tick, action, code, amount, price, note, value_after, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mandate_id, tick, action, code, float(amount), float(price), note, int(value_after), int(time.time())),
        )

    async def _row(self, sql: str, params: tuple = ()) -> Optional[dict]:
        rows = await self._rows(sql, params)
        return rows[0] if rows else None

    async def _rows(self, sql: str, params: tuple = ()) -> List[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def _scalar(self, sql: str, params: tuple = ()):
        async with self.db.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    def _mandate_payload(self, row: dict, products: Dict[str, dict]) -> dict:
        positions = self._load_positions(row.get("positions_json"))
        prices = {code: float(p.get("price") or 0) for code, p in products.items()}
        value = int(round(self._total_value(int(row.get("cash") or 0), positions, prices)))
        principal = int(row.get("principal") or 0)
        agent = AGENT_DEFS.get(str(row.get("agent_key"))) or {}
        pos_list = []
        for code, pos in positions.items():
            price = prices.get(code, 0.0)
            shares = float(pos.get("shares", 0))
            pos_list.append({
                "code": code,
                "name": (products.get(code) or {}).get("name", code),
                "kind": pos.get("kind", "stock"),
                "shares": shares,
                "value": int(round(shares * price)),
            })
        return {
            "id": int(row["id"]),
            "agent_key": row["agent_key"],
            "agent_name": agent.get("name", row["agent_key"]),
            "principal": principal,
            "cash": int(row.get("cash") or 0),
            "value": value,
            "pnl": value - principal,
            "pnl_pct": (value / principal - 1) if principal else 0.0,
            "start_tick": int(row["start_tick"]),
            "end_tick": int(row["end_tick"]),
            "duration": max(0, int(row["end_tick"]) - int(row["start_tick"])),
            "last_settle_tick": int(row["last_settle_tick"]),
            "remaining_ticks": max(
                0, int(row["end_tick"]) - int(row.get("last_settle_tick") or 0) - 1
            ),
            "status": row["status"],
            "close_reason": row.get("close_reason") or "",
            "management_fee": int(row.get("management_fee") or 0),
            "profit_fee": int(row.get("profit_fee") or 0),
            "exp_delta": int(row.get("exp_delta") or 0),
            "final_value": int(row.get("final_value") or 0),
            "payout": int(row.get("payout") or 0),
            "stop_loss_value": int(round(principal * (1 - float(agent.get("stop_loss", 0))))),
            "take_profit_value": int(round(principal * (1 + float(agent.get("take_profit", 0))))),
            "positions": pos_list,
            "positions_value": int(round(sum(p["value"] for p in pos_list))),
        }
