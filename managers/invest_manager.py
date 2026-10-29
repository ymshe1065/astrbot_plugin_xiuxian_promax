# managers/invest_manager.py
"""仙市投资系统：基金申购 / 股票买卖 / 盘次结算 / 分红。

三条设计前提，改动前请先读：

1. **时间以 UTC+8 计**，每日 8 盘（07/09/…/21 点），23:00–次日 07:00 封盘。
   21:00–23:00 是尾盘：价格冻结但可以买卖。
2. **价格只在盘次边界结算**，查询纯读。反复刷 `/仙市` 不会改变任何价格，
   这是防止「刷新等高价」的根本手段，不要为了「看起来实时」把结算塞进查询路径。
3. **所有随机都走 ``_rng()``**，种子 = md5(参数 : 服务器密钥)。密钥首次启动随机生成并落库，
   不进代码。漏掉任何一处就等于把未来价格暴露给玩家（同盘次买入、下盘卖出即可套利）。
"""

from __future__ import annotations

import hashlib
import math
import random
import secrets
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from ..data import DataBase
from ..models import Player

__all__ = ["InvestManager"]

# ===== 时序 =====

TZ_CST = timezone(timedelta(hours=8))
EPOCH_DATE = date(2026, 1, 1)
TICKS_PER_DAY = 8
SLOT_HOURS = (7, 9, 11, 13, 15, 17, 19, 21)
MARKET_OPEN_HOUR = 7
MARKET_CLOSE_HOUR = 23

# 补结算最多回溯 56 盘（7 个交易日），更早的行情不再重算
MAX_BACKFILL_TICKS = 56

# ===== 交易规则 =====

LOT_SIZE = 100
FEE_RATE = 0.003
MIN_FEE = 100
MAX_LOTS_PER_TICK = 20
MAX_POS_PER_STOCK = 500_000
MAX_POS_TOTAL = 2_000_000
LOAN_LIMIT_FOR_STOCK = 100_000

# 赎回费阶梯：(持有盘数上限, 费率)，按顺序取第一个满足的
REDEEM_FEE_TIERS = ((14, 0.015), (42, 0.005), (10 ** 9, 0.0))

# ===== 分红 =====

DIVIDEND_INTERVAL = 21   # 每 21 盘结算一次
DIVIDEND_MIN_HOLD = 8    # 需连续持有满 8 盘

# ===== 价格引擎 =====

MOMENTUM_WEIGHT = 0.25
REVERSION_WEIGHT = 0.08
REVERSION_CAP = 0.012
CYCLE_TICKS = 56         # 7 个交易日一轮

# 形态剧本：四盘节奏系数。同一个利好，有四种完全不同的走法，
# 这张表是「消息决定走向但决定不了结果」的全部来源。
PATTERN_SCRIPT = {
    "single": (1.0, 1.0, 1.0, 1.0),
    "spike": (2.2, 0.6, 0.0, 0.0),
    "pump_dump": (1.4, 1.0, -0.6, -1.0),
    "reverse": (-1.2, -0.8, 1.2, 1.6),
}

PATTERN_LABEL = {
    "single": "单边",
    "spike": "脉冲",
    "pump_dump": "冲高回落",
    "reverse": "先抑后扬",
}

# ===== 品种 =====
# (代码, 名称, 关联系统, 基准价, 每盘σ, 周期振幅, 周期相位, 单盘限幅, 日熔断, 消息基准)

STOCK_DEFS = (
    ("S01", "丹阁股", "炼丹", 42.0, 0.0055, 0.0016, 0, 0.030, 0.08, 0.0045),
    ("S02", "器阁股", "锻造", 38.0, 0.0070, 0.0022, 7, 0.035, 0.09, 0.0055),
    ("S03", "镖局股", "押镖", 25.0, 0.0045, 0.0014, 14, 0.030, 0.07, 0.0038),
    ("S04", "灵田股", "种植", 30.0, 0.0060, 0.0019, 21, 0.030, 0.08, 0.0048),
    ("S05", "御兽股", "灵兽", 55.0, 0.0085, 0.0026, 28, 0.040, 0.10, 0.0065),
    ("S06", "拍卖行股", "拍卖", 68.0, 0.0075, 0.0024, 35, 0.035, 0.09, 0.0058),
)

# (代码, 名称, 风险等级, 基准净值, 每盘σ, 周收益, 消息基准)
FUND_DEFS = (
    ("F01", "灵脉·稳", 1, 1.0, 0.0006, 0.009, 0.0012),
    ("F02", "仙市·衡", 2, 1.0, 0.0011, 0.013, 0.0016),
    ("F03", "险峰·进", 3, 1.0, 0.0020, 0.010, 0.0022),
)

FUND_FLOOR = 0.85        # 净值硬底

# ===== 消息池 =====
# 每条 = (形态, 方向, 标题, 细节)。形态藏在措辞里：看「盛传」就别追，
# 看「三年/每月」才是真利好。玩家自己归纳，这个学习过程本身是玩法。

NEWS_POOL: Dict[str, List[Tuple[str, int, str, str]]] = {
    "S01": [
        ("single", 1, "丹阁与青云宗签下三年供丹契约", "每月固定交货三千炉，产能已排到年末。"),
        ("spike", 1, "丹阁一炉九转还魂丹拍出天价", "当场落槌，买家未留名姓。"),
        ("pump_dump", 1, "坊间盛传丹阁新丹方将量产", "各铺抢购囤货，丹阁尚未回应。"),
        ("reverse", 1, "丹阁丹药被曝掺假遭查封", "阁主承诺三日内当众开炉验丹。"),
        ("single", -1, "丹阁主炉炸裂，供丹中断", "修复需月余，订单被迫延期。"),
    ],
    "S02": [
        ("single", 1, "器阁接下边关军备大单", "三年内按月交付，炉火终年不熄。"),
        ("spike", 1, "器阁在百器大会夺魁", "当场获赏，订单当日踏破门槛。"),
        ("pump_dump", 1, "法器供不应求，铺前排起长队", "有商号加价抢货，器阁未表态。"),
        ("single", -1, "器阁主炉炸裂，工期延误", "三成订单需延期交付。"),
    ],
    "S03": [
        ("single", 1, "镖局开辟北境新商路", "长期承运，每月固定走镖三趟。"),
        ("single", -1, "镖局连失三趟镖", "声誉受损，商号纷纷撤单。"),
        ("pump_dump", 1, "镖局运费上调引商号联名抗议", "传闻将被迫回调。"),
        ("reverse", 1, "镖局被曝私吞镖银", "总镖头承诺三日内开箱公示。"),
    ],
    "S04": [
        ("spike", 1, "灵田杂交出高产灵稻", "当场测产，亩产翻倍。"),
        ("single", -1, "灵田遭虫害，减产三成", "需一季方能恢复地力。"),
        ("pump_dump", 1, "坊间囤积灵谷待涨", "粮价短期走高，仓储已满。"),
        ("single", 1, "灵田与药堂签下长期供药契约", "每月固定供货，不随行就市。"),
    ],
    "S05": [
        ("spike", 1, "御兽门在万兽大会夺冠", "当场获赏，灵兽价格应声而起。"),
        ("single", -1, "灵兽瘟病蔓延，多地封栏", "交易停滞，需月余方能解禁。"),
        ("pump_dump", 1, "传闻发现上古灵兽种群", "猎队纷纷出动，尚无实证。"),
        ("single", 1, "御兽门与军中签下战兽契约", "长期供货，每月定量。"),
    ],
    "S06": [
        ("single", 1, "拍卖行秋拍专场成交破纪录", "连场爆满，佣金大增。"),
        ("single", -1, "拍卖行曝赝品丑闻", "买家集体索赔，信誉受损。"),
        ("reverse", 1, "传拍卖行操纵价格被查", "行首承诺三日内公开账簿。"),
        ("spike", 1, "一件残器在拍卖行拍出天价", "当场落槌，全场哗然。"),
    ],
}

# 全市场消息：10% 概率取代个股消息，影响所有标的（利空利多各半）
MARKET_NEWS = (
    ("single", -1, "坊市整顿，各铺停业自查", "三日之内不得开张，各业皆受影响。"),
    ("single", 1, "新灵石矿脉现世", "市面银钱充裕，各铺生意兴隆。"),
)

MARKET_NEWS_CHANCE = 0.10

# 非个股消息的传导折扣
MARKET_NEWS_SCALE = 0.5


class InvestManager:
    """仙市投资管理器。所有对外方法在内部完成「先结算到当前盘，再读/写」。"""

    def __init__(self, db: DataBase, config: dict = None, config_manager=None):
        self.db = db
        self.config = config or {}
        self.config_manager = config_manager
        self._secret: Optional[str] = None
        self._ready = False

    # ===== 时间与盘次 =====

    @staticmethod
    def now() -> datetime:
        """当前 UTC+8 时刻。绝不用服务器本地时间，跑 UTC 的机器会整体偏 8 小时。"""
        return datetime.now(TZ_CST)

    @classmethod
    def _as_local(cls, moment: Optional[datetime]) -> datetime:
        moment = moment or cls.now()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=TZ_CST)
        return moment.astimezone(TZ_CST)

    @classmethod
    def tick_of(cls, moment: Optional[datetime] = None) -> int:
        """把时刻映射到盘次编号。07:00 是当天第 1 盘，23:00 后归入当天最后一盘。"""
        local = cls._as_local(moment)
        day_index = (local.date() - EPOCH_DATE).days
        hour = local.hour
        if hour < MARKET_OPEN_HOUR:
            return max(0, (day_index - 1) * TICKS_PER_DAY + TICKS_PER_DAY - 1)
        if hour >= MARKET_CLOSE_HOUR:
            return day_index * TICKS_PER_DAY + TICKS_PER_DAY - 1
        return day_index * TICKS_PER_DAY + (hour - MARKET_OPEN_HOUR) // 2

    @classmethod
    def tick_time(cls, tick: int) -> datetime:
        day, slot = divmod(tick, TICKS_PER_DAY)
        return datetime.combine(
            EPOCH_DATE + timedelta(days=day), dt_time(hour=SLOT_HOURS[slot]), tzinfo=TZ_CST
        )

    @classmethod
    def slot_label(cls, tick: int) -> str:
        return f"{SLOT_HOURS[tick % TICKS_PER_DAY]:02d}:00"

    @classmethod
    def is_open(cls, moment: Optional[datetime] = None) -> bool:
        local = cls._as_local(moment)
        return MARKET_OPEN_HOUR <= local.hour < MARKET_CLOSE_HOUR

    @classmethod
    def seconds_to_open(cls, moment: Optional[datetime] = None) -> int:
        """闭市时返回距开盘秒数；开市时返回 0。"""
        local = cls._as_local(moment)
        if cls.is_open(local):
            return 0
        if local.hour >= MARKET_CLOSE_HOUR:
            target = datetime.combine(
                local.date() + timedelta(days=1), dt_time(hour=MARKET_OPEN_HOUR), tzinfo=TZ_CST
            )
        else:
            target = datetime.combine(
                local.date(), dt_time(hour=MARKET_OPEN_HOUR), tzinfo=TZ_CST
            )
        return max(0, int((target - local).total_seconds()))

    @classmethod
    def seconds_to_close(cls, moment: Optional[datetime] = None) -> int:
        local = cls._as_local(moment)
        if not cls.is_open(local):
            return 0
        target = datetime.combine(local.date(), dt_time(hour=MARKET_CLOSE_HOUR), tzinfo=TZ_CST)
        return max(0, int((target - local).total_seconds()))

    # ===== 随机 =====

    def _rng(self, *parts) -> random.Random:
        raw = ":".join(str(p) for p in parts) + ":" + (self._secret or "")
        return random.Random(int(hashlib.md5(raw.encode("utf-8")).hexdigest(), 16))

    async def _ensure_secret(self):
        if self._secret:
            return
        stored = await self.db.ext.get_system_config("invest_secret")
        if not stored:
            stored = secrets.token_hex(16)
            await self.db.ext.set_system_config("invest_secret", stored)
            logger.info("【仙市】已生成行情密钥，本部署的行情序列就此确定")
        self._secret = stored

    # ===== 基础读写 =====

    async def _scalar(self, sql: str, params: tuple = ()):
        async with self.db.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def _rows(self, sql: str, params: tuple = ()) -> List[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def _get_last_tick(self) -> Optional[int]:
        raw = await self.db.ext.get_system_config("invest_last_tick")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    async def _set_last_tick(self, tick: int):
        await self.db.ext.set_system_config("invest_last_tick", str(tick))

    async def ensure_ready(self):
        """首次调用时生成密钥并灌入品种。必须运行在事务之外（内部会 commit）。"""
        if self._ready:
            return
        await self._ensure_secret()
        count = await self._scalar("SELECT COUNT(*) FROM invest_products")
        if not count:
            await self._seed_products()
        self._ready = True

    async def _seed_products(self):
        for code, name, link, base, sigma, amp, phase, price_limit, day_limit, msg_base in STOCK_DEFS:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO invest_products "
                "(code, name, kind, base_price, price, prev_close, prev_change, day_open, sigma, "
                " cycle_amp, cycle_phase, price_limit, day_limit, msg_base, weekly_return, "
                " dividend_rate, risk_level) "
                "VALUES (?, ?, 'stock', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 0, 0.0022, 2)",
                (code, name, base, base, base, base, sigma, amp, phase, price_limit, day_limit, msg_base),
            )
        for code, name, risk, base, sigma, weekly, msg_base in FUND_DEFS:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO invest_products "
                "(code, name, kind, base_price, price, prev_close, prev_change, day_open, sigma, "
                " cycle_amp, cycle_phase, price_limit, day_limit, msg_base, weekly_return, "
                " dividend_rate, risk_level) "
                "VALUES (?, ?, 'fund', ?, ?, ?, 0, ?, ?, 0, 0, 0.02, 0.05, ?, ?, 0, ?)",
                (code, name, base, base, base, base, sigma, msg_base, weekly, risk),
            )
        await self.db.conn.commit()
        logger.info("【仙市】已初始化 %d 只股票与 %d 只基金", len(STOCK_DEFS), len(FUND_DEFS))

    async def _load_products(self) -> List[dict]:
        return await self._rows("SELECT * FROM invest_products ORDER BY kind DESC, code")

    async def _load_product(self, code: str) -> Optional[dict]:
        rows = await self._rows("SELECT * FROM invest_products WHERE code = ?", (code.upper(),))
        return rows[0] if rows else None

    # ===== 结算 =====

    async def settle_to(self, moment: Optional[datetime] = None) -> int:
        """把行情推进到 moment 所在盘次，返回实际推进的盘数。重复调用无副作用。"""
        await self.ensure_ready()
        target = self.tick_of(moment)
        last = await self._get_last_tick()
        if last is None:
            # 首次运行不回补历史，从当前盘开始记录
            await self._set_last_tick(target)
            return 0
        if target <= last:
            return 0

        start = max(last + 1, target - MAX_BACKFILL_TICKS + 1)
        moved = 0
        for tick in range(start, target + 1):
            try:
                await self._settle_tick(tick)
                moved += 1
            except Exception as exc:  # 单盘失败不应拖垮整个循环
                logger.error(f"【仙市】第 {tick} 盘结算失败: {exc}")
                break
        await self._set_last_tick(target)
        await self.db.conn.commit()
        return moved

    async def _settle_tick(self, tick: int):
        day, slot = divmod(tick, TICKS_PER_DAY)
        products = await self._load_products()

        # 开盘盘重置当日基准，供日线熔断使用
        if slot == 0:
            for p in products:
                p["day_open"] = p["price"]

        # 先生成消息、再结算——玩家看到的第 1 盘价格已经含了第一段，
        # 否则消息一公布所有人抢跑，等于白送。
        if slot in (0, 4):
            await self._ensure_news(day, slot)
        news = await self._load_news(day, slot)

        for p in products:
            if p["kind"] == "fund":
                new_price, change = self._advance_fund(p, tick)
                tag = ""
            else:
                bias = self._bias_for(p, slot, news)
                new_price, change = self._advance_stock(p, tick, slot, bias)
                tag = self._tag_for(p, news)
            await self._write_price(p, tick, new_price, change, tag)

        await self._pay_dividends(tick)
        await self.db.conn.commit()

    def _advance_stock(self, product: dict, tick: int, slot: int, bias: float) -> Tuple[float, float]:
        rng = self._rng(tick, product["code"], "settle")
        gap = 1.2 if slot == 0 else 1.0
        price = float(product["price"])
        base = float(product["base_price"]) or 1.0

        noise = rng.gauss(0.0, float(product["sigma"])) * gap
        momentum = MOMENTUM_WEIGHT * float(product["prev_change"])
        cycle = float(product["cycle_amp"]) * math.sin(
            2 * math.pi * ((tick + int(product["cycle_phase"])) % CYCLE_TICKS) / CYCLE_TICKS
        ) * gap
        reversion = 0.0
        if price > 0:
            reversion = max(
                -REVERSION_CAP, min(REVERSION_CAP, -REVERSION_WEIGHT * math.log(price / base))
            )

        raw = noise + momentum + cycle + bias + reversion
        limit = float(product["price_limit"])
        change = max(-limit, min(limit, raw))
        new_price = price * (1 + change)

        # 日线熔断：取消锁定后的最后一道闸
        day_open = float(product["day_open"]) or price
        if day_open > 0:
            day_limit = float(product["day_limit"])
            new_price = min(new_price, day_open * (1 + day_limit))
            new_price = max(new_price, day_open * (1 - day_limit))

        new_price = max(new_price, 0.01)
        real_change = (new_price / price - 1) if price else 0.0
        return round(new_price, 4), real_change

    def _advance_fund(self, product: dict, tick: int) -> Tuple[float, float]:
        rng = self._rng(tick, product["code"], "fund")
        drift = float(product["weekly_return"]) / CYCLE_TICKS
        noise = rng.gauss(0.0, float(product["sigma"]))
        price = float(product["price"])
        new_price = max(price * (1 + drift + noise), FUND_FLOOR)
        return round(new_price, 4), (new_price / price - 1) if price else 0.0

    def _bias_for(self, product: dict, slot: int, news: List[dict]) -> float:
        idx = slot if slot < 4 else slot - 4
        total = 0.0
        is_fund = product["kind"] == "fund"
        for item in news:
            scope = item["code"]
            if scope != product["code"] and scope != "*":
                continue
            if is_fund and scope != "*":
                continue
            script = PATTERN_SCRIPT.get(item["pattern"])
            if not script:
                continue
            scale = MARKET_NEWS_SCALE if scope == "*" else 1.0
            if is_fund:
                scale *= MARKET_NEWS_SCALE
            total += script[idx] * float(product["msg_base"]) * float(item["strength"]) * int(item["direction"]) * scale
        return total

    @staticmethod
    def _tag_for(product: dict, news: List[dict]) -> str:
        for item in news:
            if item["code"] in (product["code"], "*"):
                return item["pattern"]
        return ""

    async def _write_price(self, product: dict, tick: int, price: float, change: float, tag: str):
        await self.db.conn.execute(
            "INSERT OR REPLACE INTO invest_prices (code, tick, close, change_rate, msg_tag) "
            "VALUES (?, ?, ?, ?, ?)",
            (product["code"], tick, price, round(change, 6), tag),
        )
        await self.db.conn.execute(
            "UPDATE invest_products SET price = ?, prev_close = ?, prev_change = ?, day_open = ? "
            "WHERE code = ?",
            (
                price,
                float(product["price"]),
                round(change, 6),
                float(product["day_open"]) or float(product["price"]),
                product["code"],
            ),
        )

    # ===== 消息 =====

    async def _ensure_news(self, day: int, slot: int):
        exists = await self._scalar(
            "SELECT COUNT(*) FROM invest_events WHERE trade_day = ? AND slot = ?", (day, slot)
        )
        if exists:
            return

        rng = self._rng(day, slot, "news")
        if rng.random() < MARKET_NEWS_CHANCE:
            code = "*"
            pattern, direction, title, detail = rng.choice(MARKET_NEWS)
        else:
            code = rng.choice(sorted(NEWS_POOL.keys()))
            pattern, direction, title, detail = rng.choice(NEWS_POOL[code])

        strength = round(self._rng(day, slot, "strength").uniform(0.7, 1.3), 4)
        await self.db.conn.execute(
            "INSERT INTO invest_events "
            "(trade_day, slot, code, pattern, direction, base_bias, strength, title, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (day, slot, code, pattern, direction, 0.0, strength, title, detail),
        )

    async def _load_news(self, day: int, slot: int) -> List[dict]:
        return await self._rows(
            "SELECT * FROM invest_events WHERE trade_day = ? AND slot = ?", (day, slot)
        )

    async def today_news(self, moment: Optional[datetime] = None) -> List[dict]:
        """今天两条消息；未到点的那一场为待发。"""
        day = self.tick_of(moment) // TICKS_PER_DAY
        current_slot = self.tick_of(moment) % TICKS_PER_DAY
        rows = await self._rows(
            "SELECT * FROM invest_events WHERE trade_day = ? ORDER BY slot", (day,)
        )
        by_slot = {r["slot"]: r for r in rows}
        result = []
        for slot in (0, 4):
            hour = SLOT_HOURS[slot]
            if slot in by_slot:
                result.append(dict(by_slot[slot], published=True, hour=hour))
            else:
                pending = current_slot < slot
                result.append({"slot": slot, "hour": hour, "published": False, "pending": pending})
        return result

    # ===== 分红 =====

    async def _pay_dividends(self, tick: int):
        if tick % DIVIDEND_INTERVAL != 0:
            return
        rows = await self._rows(
            "SELECT h.user_id, h.code, h.shares, h.hold_since, p.price, p.dividend_rate "
            "FROM invest_holdings h JOIN invest_products p ON p.code = h.code "
            "WHERE p.kind = 'stock' AND p.dividend_rate > 0 AND h.shares > 0"
        )
        now_ts = int(time.time())
        for row in rows:
            if tick - int(row["hold_since"]) < DIVIDEND_MIN_HOLD:
                continue
            amount = int(float(row["shares"]) * float(row["price"]) * float(row["dividend_rate"]))
            if amount <= 0:
                continue
            await self.db.conn.execute(
                "UPDATE players SET gold = gold + ? WHERE user_id = ?", (amount, row["user_id"])
            )
            await self.db.conn.execute(
                "INSERT INTO invest_dividends (user_id, code, amount, tick, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (row["user_id"], row["code"], amount, tick, now_ts),
            )

    # ===== 交易辅助 =====

    async def _active_loan(self, user_id: str) -> int:
        value = await self._scalar(
            "SELECT COALESCE(SUM(principal), 0) FROM bank_loans "
            "WHERE user_id = ? AND status = 'active'",
            (user_id,),
        )
        return int(value or 0)

    async def _holdings_rows(self, user_id: str) -> List[dict]:
        return await self._rows(
            "SELECT h.*, p.name, p.kind, p.price FROM invest_holdings h "
            "JOIN invest_products p ON p.code = h.code "
            "WHERE h.user_id = ? AND h.shares > 0 ORDER BY h.code",
            (user_id,),
        )

    async def _portfolio_value(self, user_id: str) -> int:
        rows = await self._holdings_rows(user_id)
        return int(sum(float(r["shares"]) * float(r["price"]) for r in rows if r["kind"] == "stock"))

    @staticmethod
    def _calc_fee(amount: int) -> int:
        return max(MIN_FEE, int(amount * FEE_RATE))

    def _check_market(self) -> Tuple[bool, str]:
        if self.is_open():
            return True, ""
        wait = self.seconds_to_open()
        hours, rest = divmod(wait, 3600)
        minutes = rest // 60
        return False, f"仙市已闭市，07:00 开盘（距开盘 {hours} 小时 {minutes} 分）"

    # ===== 股票买卖 =====

    async def buy_stock(self, player: Player, code: str, lots: int) -> Tuple[bool, str]:
        code = (code or "").strip().upper()
        if lots <= 0:
            return False, "买入手数必须大于 0，1 手 = 100 股。"
        if lots > MAX_LOTS_PER_TICK:
            return False, f"单盘单标的最多买入 {MAX_LOTS_PER_TICK} 手，请分盘操作。"

        opened, why = self._check_market()
        if not opened:
            return False, why

        await self.settle_to()
        product = await self._load_product(code)
        if not product or product["kind"] != "stock":
            return False, f"仙市中不存在股票代码「{code}」。发送「/仙市」查看全部标的。"
        if product["status"] != "trading":
            return False, "该标的已停牌。"

        shares = lots * LOT_SIZE
        price = float(product["price"])
        amount = int(round(shares * price))
        fee = self._calc_fee(amount)
        total_cost = amount + fee

        loan = await self._active_loan(player.user_id)
        if loan > LOAN_LIMIT_FOR_STOCK:
            return False, (
                f"你当前贷款余额 {loan:,} 灵石，超过 {LOAN_LIMIT_FOR_STOCK:,} 的入市门槛。\n"
                "请先还款后再买股票。"
            )

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                return False, "道友尚未踏入仙途。"
            if fresh.gold < total_cost:
                await self.db.conn.rollback()
                return False, (
                    f"灵石不足：需要 {total_cost:,}（含手续费 {fee:,}），你只有 {fresh.gold:,}。"
                )

            rows = await self._holdings_rows(player.user_id)
            held = next((r for r in rows if r["code"] == code), None)
            held_shares = float(held["shares"]) if held else 0.0
            new_value = (held_shares + shares) * price
            if new_value > MAX_POS_PER_STOCK:
                await self.db.conn.rollback()
                return False, (
                    f"单只持仓上限 {MAX_POS_PER_STOCK:,} 灵石市值，"
                    f"本次买入后将达 {int(new_value):,}。"
                )
            stock_total = int(
                sum(
                    float(r["shares"]) * float(r["price"])
                    for r in rows
                    if r["kind"] == "stock"
                ) + shares * price
            )
            if stock_total > MAX_POS_TOTAL:
                await self.db.conn.rollback()
                return False, f"股票总持仓上限 {MAX_POS_TOTAL:,} 灵石市值，本次买入后将超出。"

            fresh.gold -= total_cost
            await self.db.update_player(fresh, commit=False)

            tick = self.tick_of()
            if held:
                await self.db.conn.execute(
                    "UPDATE invest_holdings SET shares = shares + ?, cost = cost + ?, last_buy = ? "
                    "WHERE user_id = ? AND code = ?",
                    (shares, total_cost, tick, player.user_id, code),
                )
            else:
                await self.db.conn.execute(
                    "INSERT INTO invest_holdings (user_id, code, shares, cost, hold_since, last_buy) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (player.user_id, code, shares, total_cost, tick, tick),
                )

            await self.db.conn.execute(
                "INSERT INTO invest_orders "
                "(user_id, code, side, shares, price, amount, fee, tick, backfilled, created_at) "
                "VALUES (?, ?, 'buy', ?, ?, ?, ?, ?, 0, ?)",
                (player.user_id, code, shares, price, amount, fee, tick, int(time.time())),
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        return True, (
            f"买入 {product['name']} {lots} 手（{shares} 股）\n"
            f"成交价 {price:.2f}，金额 {amount:,}，手续费 {fee:,}\n"
            f"剩余灵石：{fresh.gold:,}"
        )

    async def sell_stock(self, player: Player, code: str, lots: int) -> Tuple[bool, str]:
        code = (code or "").strip().upper()
        if lots <= 0:
            return False, "卖出手数必须大于 0。"

        opened, why = self._check_market()
        if not opened:
            return False, why

        await self.settle_to()
        product = await self._load_product(code)
        if not product or product["kind"] != "stock":
            return False, f"仙市中不存在股票代码「{code}」。"

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                return False, "道友尚未踏入仙途。"

            rows = await self._holdings_rows(player.user_id)
            held = next((r for r in rows if r["code"] == code), None)
            if not held or float(held["shares"]) <= 0:
                await self.db.conn.rollback()
                return False, f"你没有持有 {product['name']}。"

            price = float(product["price"])
            held_shares = float(held["shares"])
            sell_shares = min(lots * LOT_SIZE, held_shares)
            amount = int(round(sell_shares * price))
            fee = self._calc_fee(amount)
            net = amount - fee
            if net < 0:
                await self.db.conn.rollback()
                return False, "成交金额不足以支付手续费，请加大卖出数量。"

            fresh.gold += net
            await self.db.update_player(fresh, commit=False)

            remaining = held_shares - sell_shares
            if remaining <= 1e-6:
                await self.db.conn.execute(
                    "DELETE FROM invest_holdings WHERE user_id = ? AND code = ?",
                    (player.user_id, code),
                )
            else:
                await self.db.conn.execute(
                    "UPDATE invest_holdings SET shares = ? WHERE user_id = ? AND code = ?",
                    (remaining, player.user_id, code),
                )

            await self.db.conn.execute(
                "INSERT INTO invest_orders "
                "(user_id, code, side, shares, price, amount, fee, tick, backfilled, created_at) "
                "VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, 0, ?)",
                (player.user_id, code, sell_shares, price, amount, fee, self.tick_of(), int(time.time())),
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        return True, (
            f"卖出 {product['name']} {sell_shares / LOT_SIZE:.2f} 手（{int(sell_shares)} 股）\n"
            f"成交价 {price:.2f}，金额 {amount:,}，手续费 {fee:,}\n"
            f"到账 {net:,}，当前灵石 {fresh.gold:,}"
        )

    # ===== 基金申赎 =====

    async def subscribe_fund(self, player: Player, code: str, amount: int) -> Tuple[bool, str]:
        code = (code or "").strip().upper()
        if amount <= 0:
            return False, "申购金额必须大于 0。"

        opened, why = self._check_market()
        if not opened:
            return False, why

        await self.settle_to()
        product = await self._load_product(code)
        if not product or product["kind"] != "fund":
            return False, f"仙市中不存在基金代码「{code}」。"

        loan = await self._active_loan(player.user_id)
        if loan > 0:
            return False, f"你当前有 {loan:,} 灵石贷款未清，基金不接受贷款资金申购。\n请先还清贷款。"

        nav = float(product["price"])
        shares = amount / nav

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                return False, "道友尚未踏入仙途。"
            if fresh.gold < amount:
                await self.db.conn.rollback()
                return False, f"灵石不足：需要 {amount:,}，你只有 {fresh.gold:,}。"

            fresh.gold -= amount
            await self.db.update_player(fresh, commit=False)

            tick = self.tick_of()
            rows = await self._holdings_rows(player.user_id)
            held = next((r for r in rows if r["code"] == code), None)
            if held:
                await self.db.conn.execute(
                    "UPDATE invest_holdings SET shares = shares + ?, cost = cost + ?, last_buy = ? "
                    "WHERE user_id = ? AND code = ?",
                    (shares, amount, tick, player.user_id, code),
                )
            else:
                await self.db.conn.execute(
                    "INSERT INTO invest_holdings (user_id, code, shares, cost, hold_since, last_buy) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (player.user_id, code, shares, amount, tick, tick),
                )

            await self.db.conn.execute(
                "INSERT INTO invest_orders "
                "(user_id, code, side, shares, price, amount, fee, tick, backfilled, created_at) "
                "VALUES (?, ?, 'subscribe', ?, ?, ?, 0, ?, 0, ?)",
                (player.user_id, code, shares, nav, amount, tick, int(time.time())),
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        return True, (
            f"申购 {product['name']} {amount:,} 灵石\n"
            f"成交净值 {nav:.4f}，获得 {shares:.2f} 份\n"
            f"剩余灵石：{fresh.gold:,}"
        )

    async def redeem_fund(self, player: Player, code: str, shares: float) -> Tuple[bool, str]:
        """shares <= 0 表示全部赎回。"""
        code = (code or "").strip().upper()
        opened, why = self._check_market()
        if not opened:
            return False, why

        await self.settle_to()
        product = await self._load_product(code)
        if not product or product["kind"] != "fund":
            return False, f"仙市中不存在基金代码「{code}」。"

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                return False, "道友尚未踏入仙途。"

            rows = await self._holdings_rows(player.user_id)
            held = next((r for r in rows if r["code"] == code), None)
            if not held or float(held["shares"]) <= 0:
                await self.db.conn.rollback()
                return False, f"你没有持有 {product['name']}。"

            held_shares = float(held["shares"])
            sell_shares = held_shares if shares <= 0 else min(shares, held_shares)
            nav = float(product["price"])
            amount = int(sell_shares * nav)

            held_ticks = self.tick_of() - int(held["hold_since"])
            fee_rate = next(rate for cap, rate in REDEEM_FEE_TIERS if held_ticks < cap)
            fee = int(amount * fee_rate)
            net = amount - fee

            fresh.gold += net
            await self.db.update_player(fresh, commit=False)

            remaining = held_shares - sell_shares
            if remaining <= 1e-6:
                await self.db.conn.execute(
                    "DELETE FROM invest_holdings WHERE user_id = ? AND code = ?",
                    (player.user_id, code),
                )
            else:
                await self.db.conn.execute(
                    "UPDATE invest_holdings SET shares = ? WHERE user_id = ? AND code = ?",
                    (remaining, player.user_id, code),
                )

            await self.db.conn.execute(
                "INSERT INTO invest_orders "
                "(user_id, code, side, shares, price, amount, fee, tick, backfilled, created_at) "
                "VALUES (?, ?, 'redeem', ?, ?, ?, ?, ?, 0, ?)",
                (player.user_id, code, sell_shares, nav, amount, fee, self.tick_of(), int(time.time())),
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        tier_note = f"（持有 {held_ticks} 盘，赎回费 {fee_rate:.1%}）" if fee_rate else "（持有满 42 盘，免赎回费）"
        return True, (
            f"赎回 {product['name']} {sell_shares:.2f} 份\n"
            f"成交净值 {nav:.4f}，金额 {amount:,} {tier_note}\n"
            f"到账 {net:,}，当前灵石 {fresh.gold:,}"
        )

    # ===== 查询 =====

    async def get_overview(self, moment: Optional[datetime] = None) -> dict:
        await self.settle_to(moment)
        products = await self._load_products()
        tick = self.tick_of(moment)

        items = []
        for p in products:
            history = await self._rows(
                "SELECT close FROM invest_prices WHERE code = ? ORDER BY tick DESC LIMIT 5",
                (p["code"],),
            )
            closes = [float(r["close"]) for r in history]
            recent = (float(p["price"]) / closes[-1] - 1) if len(closes) >= 2 else 0.0
            day_open = float(p["day_open"]) or float(p["price"])
            items.append(
                {
                    "code": p["code"],
                    "name": p["name"],
                    "kind": p["kind"],
                    "price": float(p["price"]),
                    "change": float(p["prev_change"]),
                    "today": (float(p["price"]) / day_open - 1) if day_open else 0.0,
                    "recent": recent,
                    "risk": int(p["risk_level"]),
                }
            )

        return {
            "tick": tick,
            "label": self.slot_label(tick),
            "open": self.is_open(moment),
            "next_tick": self.slot_label(tick + 1),
            "seconds_to_open": self.seconds_to_open(moment),
            "seconds_to_close": self.seconds_to_close(moment),
            "items": items,
            "news": await self.today_news(moment),
        }

    async def get_quote(self, code: str, moment: Optional[datetime] = None) -> Optional[dict]:
        await self.settle_to(moment)
        product = await self._load_product((code or "").strip().upper())
        if not product:
            return None
        rows = await self._rows(
            "SELECT tick, close, change_rate, msg_tag FROM invest_prices "
            "WHERE code = ? ORDER BY tick DESC LIMIT 8",
            (product["code"],),
        )
        rows.reverse()
        day_open = float(product["day_open"]) or float(product["price"])
        return {
            "code": product["code"],
            "name": product["name"],
            "kind": product["kind"],
            "price": float(product["price"]),
            "base": float(product["base_price"]),
            "change": float(product["prev_change"]),
            "today": (float(product["price"]) / day_open - 1) if day_open else 0.0,
            "risk": int(product["risk_level"]),
            "day_limit": float(product["day_limit"]),
            "history": rows,
        }

    async def get_holdings(self, user_id: str) -> List[dict]:
        await self.settle_to()
        rows = await self._holdings_rows(user_id)
        result = []
        for r in rows:
            shares = float(r["shares"])
            price = float(r["price"])
            value = shares * price
            cost = int(r["cost"])
            result.append(
                {
                    "code": r["code"],
                    "name": r["name"],
                    "kind": r["kind"],
                    "shares": shares,
                    "price": price,
                    "value": int(value),
                    "cost": cost,
                    "profit": int(value) - cost,
                    "profit_rate": ((value - cost) / cost) if cost else 0.0,
                    "hold_ticks": self.tick_of() - int(r["hold_since"]),
                }
            )
        return result

    async def get_orders(self, user_id: str, limit: int = 20) -> List[dict]:
        return await self._rows(
            "SELECT o.*, p.name FROM invest_orders o "
            "LEFT JOIN invest_products p ON p.code = o.code "
            "WHERE o.user_id = ? ORDER BY o.id DESC LIMIT ?",
            (user_id, limit),
        )

    async def get_dividends(self, user_id: str, limit: int = 10) -> List[dict]:
        return await self._rows(
            "SELECT d.*, p.name FROM invest_dividends d "
            "LEFT JOIN invest_products p ON p.code = d.code "
            "WHERE d.user_id = ? ORDER BY d.id DESC LIMIT ?",
            (user_id, limit),
        )
