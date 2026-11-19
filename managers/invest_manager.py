# managers/invest_manager.py
"""仙市投资系统：基金申购 / 股票买卖 / 盘次结算 / 分红。

三条设计前提，改动前请先读：

1. **时间以 UTC+8 计**，每日 16 盘（07:00–22:00 每整点一盘），07:00–23:00 为开市，
   23:00–次日 07:00 封盘。
2. **价格只在盘次边界结算**，查询纯读。反复刷 `/仙市` 不会改变任何价格，
   这是防止「刷新等高价」的根本手段，不要为了「看起来实时」把结算塞进查询路径。
3. **所有随机都走 ``_rng()``**，种子 = md5(参数 : 服务器密钥)。密钥首次启动随机生成并落库，
   不进代码。漏掉任何一处就等于把未来价格暴露给玩家（同盘次买入、下盘卖出即可套利）。
"""

from __future__ import annotations

import asyncio
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
from .bank_manager import DEFAULT_DEPOSIT_CAPS

__all__ = ["InvestManager"]

# ===== 时序 =====

TZ_CST = timezone(timedelta(hours=8))
EPOCH_DATE = date(2026, 1, 1)
TICKS_PER_DAY = 16
SLOT_HOURS = (7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22)
MARKET_OPEN_HOUR = 7
MARKET_CLOSE_HOUR = 23

# 补结算最多回溯 112 盘（7 个交易日），更早的行情不再重算
MAX_BACKFILL_TICKS = 112

# ===== 交易规则 =====

LOT_SIZE = 100
FEE_RATE = 0.0015
MIN_FEE = 20
# 基金份额保留 2 位小数，股票只按整数股交易；灵石金额一律取整
SHARE_DECIMALS = 2
SHARE_UNIT = 10 ** SHARE_DECIMALS
MIN_FUND_SHARES = 1 / SHARE_UNIT
MAX_LOTS_PER_TICK = 0     # 单盘单标的买入手数上限；0 = 不限（仅受灵石余额约束）
MAX_POS_PER_STOCK = 0     # 单只股票持仓市值上限；0 = 不限（灵石余额仍是硬约束）
MAX_POS_TOTAL = 0         # 股票总持仓市值上限；0 = 不限
LOAN_LIMIT_FOR_STOCK = 100_000

# 赎回费阶梯：(持有盘数上限, 费率)，按顺序取第一个满足的
REDEEM_FEE_TIERS = ((24, 0.005), (84, 0.002), (10 ** 9, 0.0))

# ===== 分红 =====

DIVIDEND_INTERVAL = 32   # 每 32 盘（2 天）结算一次
DIVIDEND_MIN_HOLD = 12   # 需连续持有满 12 盘
DIVIDEND_RATE = 0.0035   # 分红率 0.35%

# ===== 价格引擎 =====

MOMENTUM_WEIGHT = 0.35
REVERSION_WEIGHT = 0.012
REVERSION_CAP = 0.025
CYCLE_TICKS = 112        # 7 个交易日一轮

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
# 股票：(代码, 名称, 关联系统, 基准价, 每盘σ, 周期振幅, 周期相位, 单盘限幅, 日上限, 日下限, 消息基准)
# v6：σ×10、消息基准×22、单盘限幅 ±15%、不对称日熔断 +400%/−75%

STOCK_DEFS = (
    ("S01", "丹阁股", "炼丹", 42.0, 0.055, 0.016, 0, 0.15, 4.0, 0.75, 0.079),
    ("S02", "器阁股", "锻造", 38.0, 0.070, 0.022, 7, 0.15, 4.0, 0.75, 0.097),
    ("S03", "镖局股", "押镖", 25.0, 0.045, 0.014, 14, 0.15, 4.0, 0.75, 0.067),
    ("S04", "灵田股", "种植", 30.0, 0.060, 0.019, 21, 0.15, 4.0, 0.75, 0.084),
    ("S05", "御兽股", "灵兽", 55.0, 0.085, 0.026, 28, 0.18, 4.5, 0.78, 0.114),
    ("S06", "拍卖行股", "拍卖", 68.0, 0.075, 0.024, 35, 0.15, 4.0, 0.75, 0.102),
)

# (代码, 名称, 风险等级, 基准净值, 每盘σ, 日漂移, 消息基准)
# v6：日漂移 2%/5%/10%（weekly_return 字段复用为「日漂移」，结算时 ÷16）
FUND_DEFS = (
    ("F01", "灵脉·稳", 1, 1.0, 0.010, 0.020, 0.0012),
    ("F02", "仙市·衡", 2, 1.0, 0.025, 0.050, 0.0016),
    ("F03", "险峰·进", 3, 1.0, 0.050, 0.100, 0.0022),
)

# 基金净值硬底（按风险等级）：v6 从统一 0.85 改为分档，跌到硬底后不再下探
FUND_FLOOR = {1: 0.60, 2: 0.50, 3: 0.40}

# ===== 消息池 =====
# 每条 = (形态, 标题, 细节)。方向由「利好组 / 利空组」决定（1:1 抽方向）。
# 形态藏在措辞里：看「盛传/抢购」是陷阱（pump_dump），看「三年/每月/契约」才是真单边（single），
# 「承诺三日内」是先抑后扬（reverse）。玩家自己归纳，这个学习过程本身是玩法。

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
        ("reverse", -1, "器阁被曝以次充好", "阁主承诺三日内回收旧货，信誉待验。"),
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
        ("reverse", -1, "灵田被曝私售禁种灵植", "庄主承诺三日内自查并公示田册。"),
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

# 全市场消息：10% 概率取代个股消息，影响所有标的（利好/利空分列）
MARKET_NEWS = (
    ("single", -1, "坊市整顿，各铺停业自查", "三日之内不得开张，各业皆受影响。"),
    ("single", 1, "新灵石矿脉现世", "市面银钱充裕，各铺生意兴隆。"),
)

MARKET_NEWS_CHANCE = 0.10

# 每盘真消息概率（其余为「盘面平静」氛围文案，不产生 bias）
# 纯掷骰：不做连续平静保底，长草期与连出消息都可能出现，长度由随机决定
NEWS_CHANCE = 0.33

# 盘面平静文案池（不影响行情，只让风声「每盘都有」）
CALM_NEWS = (
    "今日盘面平静，各铺如常开张。",
    "坊间无甚大事，买卖两清。",
    "市面波澜不惊，看客三三两两。",
    "仙市照常运转，无风无浪。",
    "今日无新闻，行情随波逐流。",
)

# 非个股消息的传导折扣
MARKET_NEWS_SCALE = 0.5

# 消息影响窗口：当盘 + 后 3 盘（共 4 盘）
NEWS_WINDOW = 4


class InvestManager:
    """仙市投资管理器。所有对外方法在内部完成「先结算到当前盘，再读/写」。"""

    def __init__(self, db: DataBase, config: dict = None, config_manager=None):
        self.db = db
        self.config = config or {}
        self.config_manager = config_manager
        self._secret: Optional[str] = None
        self._ready = False
        # 结算互斥：后台 60 秒定时任务与玩家指令可能并发触发同一盘结算，
        # 全程持锁保证同一时刻只有一条结算流程在推进。
        self._settle_lock = asyncio.Lock()
        # 连续结算失败的盘次计数（防刷屏/便于定位卡死的盘）
        self._tick_failures: Dict[int, int] = {}
        # 杠杆风控（插件配置 INVEST 段可覆盖，缺省走类常量）
        _invest_cfg = (self.config.get("INVEST")
                       or (self.config.get("VALUES") or {}).get("INVEST") or {})
        self.margin_max_positions = self._int_config(
            _invest_cfg, "MAX_POSITIONS", self.MARGIN_MAX_POSITIONS
        )
        self.margin_max_positions_per_code = self._int_config(
            _invest_cfg, "MAX_POSITIONS_PER_CODE", self.MARGIN_MAX_POSITIONS_PER_CODE
        )
        self.margin_total_cap_override = self._int_config(_invest_cfg, "MAX_TOTAL_MARGIN", 0)
        self.margin_leverage_caps = self._parse_leverage_caps(
            _invest_cfg.get("LEVERAGE_SIGMA_CAPS")
        )

    # ===== 配置解析 =====

    @staticmethod
    def _int_config(cfg: dict, key: str, default: int) -> int:
        """插件配置里的整数项：非法值或负数一律退回默认值，0 表示「不限」。"""
        try:
            value = int((cfg or {}).get(key))
        except (TypeError, ValueError):
            return default
        return max(0, value)

    @classmethod
    def _parse_leverage_caps(cls, raw) -> tuple:
        """解析 ``"0.055:5,0.075:3"``；格式非法直接退回默认分档。

        最后一档会补一个 ``inf``，让「高于所有档位」的标的沿用最后一档倍数，
        避免漏配时变成不限倍数。
        """
        if raw is None or str(raw).strip() == "":
            return cls.MARGIN_LEVERAGE_SIGMA_CAPS
        caps = []
        for part in str(raw).replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                sigma_text, lev_text = part.split(":")
                caps.append((float(sigma_text), int(lev_text)))
            except (TypeError, ValueError):
                return cls.MARGIN_LEVERAGE_SIGMA_CAPS
        if not caps:
            return cls.MARGIN_LEVERAGE_SIGMA_CAPS
        caps.sort(key=lambda item: item[0])
        caps.append((float("inf"), caps[-1][1]))
        return tuple(caps)

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
        return day_index * TICKS_PER_DAY + (hour - MARKET_OPEN_HOUR)

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
        else:
            # 存量部署：静态数值字段随代码升级刷新（v6 换了整套 σ/msg_base/限幅/日漂移），
            # 但保留 price/prev_close/prev_change/day_open 等运行时行情状态，避免重置盘面。
            await self._refresh_product_defs()
        await self._normalize_holdings_precision()
        self._ready = True

    async def _seed_products(self):
        for code, name, link, base, sigma, amp, phase, price_limit, day_up, day_down, msg_base in STOCK_DEFS:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO invest_products "
                "(code, name, kind, base_price, price, prev_close, prev_change, day_open, sigma, "
                " cycle_amp, cycle_phase, price_limit, day_limit, day_up, day_down, msg_base, "
                " weekly_return, dividend_rate, risk_level) "
                "VALUES (?, ?, 'stock', ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, 2)",
                (code, name, base, base, base, base, sigma, amp, phase, price_limit, day_up, day_down,
                 msg_base, DIVIDEND_RATE),
            )
        for code, name, risk, base, sigma, drift, msg_base in FUND_DEFS:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO invest_products "
                "(code, name, kind, base_price, price, prev_close, prev_change, day_open, sigma, "
                " cycle_amp, cycle_phase, price_limit, day_limit, day_up, day_down, msg_base, "
                " weekly_return, dividend_rate, risk_level) "
                "VALUES (?, ?, 'fund', ?, ?, ?, 0, ?, ?, 0, 0, 0.15, 0, 4.0, 0.75, ?, ?, 0, ?)",
                (code, name, base, base, base, base, sigma, msg_base, drift, risk),
            )
        await self.db.conn.commit()
        logger.info("【仙市】已初始化 %d 只股票与 %d 只基金", len(STOCK_DEFS), len(FUND_DEFS))

    async def _refresh_product_defs(self):
        """刷新存量标的的静态数值字段，保留运行时价格状态。

        v6 数值升级（σ×10、msg_base×22、限幅、日漂移、硬底、分红率）需要覆盖老部署的旧值；
        INSERT OR IGNORE 不会更新已存在的行，所以这里用 UPDATE 定向刷新。
        """
        for code, name, link, base, sigma, amp, phase, price_limit, day_up, day_down, msg_base in STOCK_DEFS:
            await self.db.conn.execute(
                "UPDATE invest_products SET name = ?, base_price = ?, sigma = ?, cycle_amp = ?, "
                " cycle_phase = ?, price_limit = ?, day_up = ?, day_down = ?, msg_base = ?, "
                " dividend_rate = ? WHERE code = ?",
                (name, base, sigma, amp, phase, price_limit, day_up, day_down, msg_base,
                 DIVIDEND_RATE, code),
            )
        for code, name, risk, base, sigma, drift, msg_base in FUND_DEFS:
            await self.db.conn.execute(
                "UPDATE invest_products SET name = ?, base_price = ?, sigma = ?, msg_base = ?, "
                " weekly_return = ? WHERE code = ?",
                (name, base, sigma, msg_base, drift, code),
            )
        await self.db.conn.commit()
        logger.info("【仙市】已刷新标的静态数值（v6）")

    async def _load_products(self) -> List[dict]:
        return await self._rows("SELECT * FROM invest_products ORDER BY kind DESC, code")

    async def _load_product(self, code: str) -> Optional[dict]:
        rows = await self._rows("SELECT * FROM invest_products WHERE code = ?", (code.upper(),))
        return rows[0] if rows else None

    # ===== 结算 =====

    async def settle_to(self, moment: Optional[datetime] = None) -> int:
        """把行情推进到 moment 所在盘次，返回实际推进的盘数。重复调用无副作用。

        全程持有实例级结算锁：后台定时任务与玩家指令并发结算同一盘时，
        只有一条流程真正推进（分红/风声另有唯一索引 + INSERT OR IGNORE 幂等兜底）。
        锁内不再获取任何玩家级锁，避免交叉死锁。
        """
        async with self._settle_lock:
            return await self._settle_to_locked(moment)

    async def _settle_to_locked(self, moment: Optional[datetime] = None) -> int:
        """settle_to 的锁内实现（调用方需持有 self._settle_lock）。"""
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
                async with self.db.transaction():
                    # 多个管理器/后台任务可能同时补盘；数据库锁内复查已完成进度。
                    latest = await self._get_last_tick()
                    if latest is not None and latest >= tick:
                        continue
                    await self._settle_tick(tick)
                    # 行情、分红与检查点共同提交，崩溃/失败后不会重放半盘行情。
                    await self.db.conn.execute(
                        "UPDATE system_config SET value = ?, updated_at = ? "
                        "WHERE key = 'invest_last_tick'",
                        (str(tick), int(time.time())),
                    )
            except Exception as exc:  # 单盘失败不应拖垮整个循环
                # 只记录失败并中断本轮：last_tick 不推进到失败盘，下轮重试。
                # 绝不把未结算的盘标记为已完成（宁可反复重试并报错，也不跳盘吞行情）。
                fails = self._tick_failures.get(tick, 0) + 1
                self._tick_failures[tick] = fails
                logger.error(f"【仙市】第 {tick} 盘结算失败（连续第 {fails} 次）: {exc}")
                break
            self._tick_failures.pop(tick, None)
            moved += 1
        return moved

    async def _settle_tick(self, tick: int):
        async with self.db.transaction():
            day, slot = divmod(tick, TICKS_PER_DAY)
            products = await self._load_products()

            # 开盘盘重置当日基准，供日线熔断使用
            if slot == 0:
                for p in products:
                    p["day_open"] = p["price"]

            # 每盘都生成风声（30% 真消息，其余平静）。风声可能已由 peek_hint 提前落库，
            # _ensure_news 发现已存在会跳过。结算用「当盘 + 前 3 盘」窗口叠加 bias。
            await self._ensure_news(day, slot)
            news = await self._load_news_window(day, slot)

            for p in products:
                if p["kind"] == "fund":
                    new_price, change = self._advance_fund(p, tick)
                    tag = ""
                else:
                    bias = self._bias_for(p, day, slot, news)
                    new_price, change = self._advance_stock(p, tick, slot, bias)
                    tag = self._tag_for(p, news)
                await self._write_price(p, tick, new_price, change, tag)

            await self._pay_dividends(tick)

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

        # 日线熔断（v6 不对称）：向上允许「一天翻五倍」封顶，向下保证不归零
        day_open = float(product["day_open"]) or price
        if day_open > 0:
            day_up = float(product.get("day_up", 4.0))
            day_down = float(product.get("day_down", 0.75))
            new_price = min(new_price, day_open * (1 + day_up))
            new_price = max(new_price, day_open * (1 - day_down))

        new_price = max(new_price, 0.01)
        real_change = (new_price / price - 1) if price else 0.0
        return round(new_price, 4), real_change

    def _advance_fund(self, product: dict, tick: int) -> Tuple[float, float]:
        rng = self._rng(tick, product["code"], "fund")
        # v6：weekly_return 字段复用为「日漂移」，每盘漂移 = 日漂移 ÷ 16
        drift = float(product["weekly_return"]) / TICKS_PER_DAY
        noise = rng.gauss(0.0, float(product["sigma"]))
        price = float(product["price"])
        floor = FUND_FLOOR.get(int(product["risk_level"]), 0.60)
        new_price = max(price * (1 + drift + noise), floor)
        return round(new_price, 4), (new_price / price - 1) if price else 0.0

    def _bias_for(self, product: dict, day: int, slot: int, news: List[dict]) -> float:
        # news 是「当盘 + 前 3 盘」的消息集合。每条消息有各自的 trade_day/slot，
        # 它在剧本中的位置 = 当前盘距消息盘经过了几盘（0=消息当盘，1=下一盘……）。
        # 跨日时用绝对盘次换算。
        cur_abs = day * TICKS_PER_DAY + slot
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
                continue  # 'calm' 等无剧本，跳过
            msg_abs = int(item["trade_day"]) * TICKS_PER_DAY + int(item["slot"])
            idx = cur_abs - msg_abs
            if idx < 0 or idx >= len(script):
                continue  # 超出影响窗口，跳过
            scale = MARKET_NEWS_SCALE if scope == "*" else 1.0
            if is_fund:
                scale *= MARKET_NEWS_SCALE
            total += script[idx] * float(product["msg_base"]) * float(item["strength"]) * int(item["direction"]) * scale
        return total

    @staticmethod
    def _tag_for(product: dict, news: List[dict]) -> str:
        for item in news:
            if item["code"] in (product["code"], "*") and item.get("pattern") not in (None, "calm", ""):
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
        """为 (day, slot) 生成风声。每盘 33% 概率是真消息（利好利空 1:1），
        其余为「盘面平静」氛围文案（pattern='calm'，不产生 bias）。
        纯掷骰，不做连续平静保底：连出平静或连出消息都由随机决定。

        用确定性种子生成；peek_hint 提前调用本函数时会把结果落库，
        settle 时再次调用发现已存在则跳过，保证风声与结算用同一条消息。

        并发安全：整段包在 BEGIN IMMEDIATE 事务里，事务内复查 + INSERT OR IGNORE
        （invest_events(trade_day, slot) 有唯一索引兜底），风声推送任务与结算任务
        同时生成同一盘也只会落一条。
        """
        async with self.db.transaction():
            exists = await self._scalar(
                "SELECT COUNT(*) FROM invest_events WHERE trade_day = ? AND slot = ?", (day, slot)
            )
            if exists:
                return

            rng = self._rng(day, slot, "news")
            if rng.random() >= NEWS_CHANCE:
                # 盘面平静：无 bias
                title = rng.choice(CALM_NEWS)
                pattern, direction, code, detail = "calm", 0, "", ""
            elif rng.random() < MARKET_NEWS_CHANCE:
                # 全市场消息
                code = "*"
                pattern, direction, title, detail = rng.choice(MARKET_NEWS)
            else:
                # 个股消息：先掷方向（1:1），再从该标的下对应方向的消息里抽
                code = rng.choice(sorted(NEWS_POOL.keys()))
                direction = 1 if rng.random() < 0.5 else -1
                candidates = [n for n in NEWS_POOL[code] if n[1] == direction]
                if not candidates:  # 兜底：该标的某方向无文案时回退到全池
                    candidates = NEWS_POOL[code]
                pattern, direction, title, detail = rng.choice(candidates)

            strength = round(self._rng(day, slot, "strength").uniform(0.7, 1.3), 4)
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO invest_events "
                "(trade_day, slot, code, pattern, direction, base_bias, strength, title, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (day, slot, code, pattern, direction, 0.0, strength, title, detail),
            )

    async def _load_news_window(self, day: int, slot: int) -> List[dict]:
        """加载「当盘 + 前 3 盘」的消息，用于结算时叠加 bias（消息影响 4 盘）。"""
        # 处理跨日边界：把 (day, slot) 及之前 3 个盘次展开成 (day, slot) 列表
        ticks = []
        for back in range(NEWS_WINDOW):
            d, s = day, slot - back
            while s < 0:
                d -= 1
                s += TICKS_PER_DAY
            ticks.append((d, s))
        if not ticks:
            return []
        placeholders = ", ".join("(?, ?)" for _ in ticks)
        params = [v for d, s in ticks for v in (d, s)]
        return await self._rows(
            f"SELECT * FROM invest_events WHERE (trade_day, slot) IN ({placeholders})", tuple(params)
        )

    async def _load_news(self, day: int, slot: int) -> List[dict]:
        return await self._rows(
            "SELECT * FROM invest_events WHERE trade_day = ? AND slot = ?", (day, slot)
        )

    async def today_news(self, moment: Optional[datetime] = None) -> List[dict]:
        """今天已生成的风声（每盘一条，含盘面平静）。按 slot 升序返回。"""
        day = self.tick_of(moment) // TICKS_PER_DAY
        rows = await self._rows(
            "SELECT * FROM invest_events WHERE trade_day = ? ORDER BY slot", (day,)
        )
        return [dict(r, hour=SLOT_HOURS[int(r["slot"])]) for r in rows]

    async def peek_hint(self, tick: int) -> Optional[dict]:
        """提前生成 tick 盘的风声并落库，返回该条消息（供 HH:30 推送）。

        用与 _ensure_news 相同的确定性种子生成；settle 时 _ensure_news 发现已存在会跳过，
        因此风声与结算用的是同一条消息，序列不分裂。返回 None 表示盘面平静（可推送氛围文案，
        也可由调用方决定不推）。
        """
        day, slot = divmod(tick, TICKS_PER_DAY)
        await self._ensure_news(day, slot)
        row = await self._load_news(day, slot)
        if not row:
            return None
        item = row[0]
        item["hour"] = SLOT_HOURS[slot]
        return item

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
            # 幂等：先落分红流水（(user_id, code, tick) 唯一索引 + OR IGNORE），
            # 插入被忽略说明该盘分红已发过（并发/重跑），绝不能再入账灵石。
            cursor = await self.db.conn.execute(
                "INSERT OR IGNORE INTO invest_dividends (user_id, code, amount, tick, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (row["user_id"], row["code"], amount, tick, now_ts),
            )
            if not cursor.rowcount:
                continue
            await self.db.conn.execute(
                "UPDATE players SET gold = gold + ? WHERE user_id = ?", (amount, row["user_id"])
            )

    # ===== 交易辅助 =====

    async def _active_loan(self, user_id: str) -> int:
        # 未清贷款口径与银行一致：active 与 overdue 都算，且按「剩余本金」
        # （principal - paid_principal，钳为 0）求和，部分还款后不再虚高。
        value = await self._scalar(
            "SELECT COALESCE(SUM(MAX(0, principal - COALESCE(paid_principal, 0))), 0) "
            "FROM bank_loans WHERE user_id = ? AND status IN ('active', 'overdue')",
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

    @staticmethod
    def _floor_shares(value: float) -> float:
        """份额向下取整到 2 位小数，避免申购/赎回时凭空多出份额。"""
        return math.floor(float(value) * SHARE_UNIT + 1e-9) / SHARE_UNIT

    async def _normalize_holdings_precision(self):
        """归整历史持仓精度：股票取整、基金份额保留 2 位。可重复执行。"""
        await self.db.conn.execute(
            "UPDATE invest_holdings SET "
            " cost = CAST(ROUND(cost * CAST(shares AS INTEGER) / shares) AS INTEGER), "
            " shares = CAST(shares AS INTEGER) "
            "WHERE shares > 0 AND shares != CAST(shares AS INTEGER) "
            "AND code IN (SELECT code FROM invest_products WHERE kind = 'stock')"
        )
        await self.db.conn.execute(
            "DELETE FROM invest_holdings WHERE shares <= 0 "
            "AND code IN (SELECT code FROM invest_products WHERE kind = 'stock')"
        )
        await self.db.conn.execute(
            "UPDATE invest_holdings SET shares = CAST(shares * 100 AS INTEGER) / 100.0 "
            "WHERE shares > 0 AND shares != ROUND(shares, 2) "
            "AND code IN (SELECT code FROM invest_products WHERE kind = 'fund')"
        )
        await self.db.conn.execute(
            "DELETE FROM invest_holdings WHERE shares < 0.01 "
            "AND code IN (SELECT code FROM invest_products WHERE kind = 'fund')"
        )
        await self.db.conn.commit()

    async def _normalize_stock_shares(self, user_id: str, code: str, held: dict) -> int:
        """股票只允许整数股：历史小数股向下取整，并按比例修正成本。"""
        raw = float(held["shares"])
        whole = int(math.floor(raw + 1e-9))
        if abs(raw - whole) <= 1e-9:
            return whole
        if whole <= 0:
            await self.db.conn.execute(
                "DELETE FROM invest_holdings WHERE user_id = ? AND code = ?",
                (user_id, code),
            )
            return 0
        cost = int(round(int(held["cost"]) * whole / raw))
        await self.db.conn.execute(
            "UPDATE invest_holdings SET shares = ?, cost = ? WHERE user_id = ? AND code = ?",
            (whole, cost, user_id, code),
        )
        return whole

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
        if isinstance(lots, bool) or not isinstance(lots, (int, float)) or float(lots) != int(lots):
            return False, "买入手数必须是整数（1 手 = 100 股）。"
        lots = int(lots)
        if lots <= 0:
            return False, "买入手数必须大于 0，1 手 = 100 股。"
        if MAX_LOTS_PER_TICK > 0 and lots > MAX_LOTS_PER_TICK:
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
            held_shares = await self._normalize_stock_shares(player.user_id, code, held) if held else 0
            new_value = (held_shares + shares) * price
            if MAX_POS_PER_STOCK > 0 and new_value > MAX_POS_PER_STOCK:
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
            if MAX_POS_TOTAL > 0 and stock_total > MAX_POS_TOTAL:
                await self.db.conn.rollback()
                return False, f"股票总持仓上限 {MAX_POS_TOTAL:,} 灵石市值，本次买入后将超出。"

            fresh.gold -= total_cost
            await self.db.update_player(fresh, commit=False)

            tick = self.tick_of()
            if held and held_shares > 0:
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
        if isinstance(lots, bool) or not isinstance(lots, (int, float)) or float(lots) != int(lots):
            return False, "卖出手数必须是整数（1 手 = 100 股）。"
        lots = int(lots)
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
            held_shares = await self._normalize_stock_shares(player.user_id, code, held)
            if held_shares <= 0:
                await self.db.conn.rollback()
                return False, f"你没有持有 {product['name']}。"
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
            if remaining <= 0:
                await self.db.conn.execute(
                    "DELETE FROM invest_holdings WHERE user_id = ? AND code = ?",
                    (player.user_id, code),
                )
            else:
                remaining_cost = round(int(held["cost"]) * remaining / held_shares)
                await self.db.conn.execute(
                    "UPDATE invest_holdings SET shares = ?, cost = ? WHERE user_id = ? AND code = ?",
                    (int(remaining), remaining_cost, player.user_id, code),
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
        shares = self._floor_shares(amount / nav)
        if shares < MIN_FUND_SHARES:
            return False, "申购金额过小，按当前净值不足 0.01 份，请增加金额。"

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
                base = self._floor_shares(float(held["shares"]))
                await self.db.conn.execute(
                    "UPDATE invest_holdings SET shares = ?, cost = cost + ?, last_buy = ? "
                    "WHERE user_id = ? AND code = ?",
                    (round(base + shares, SHARE_DECIMALS), amount, tick, player.user_id, code),
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
        """shares == 0 表示全部赎回；负数直接拒绝（防止误落入全部赎回分支）。"""
        code = (code or "").strip().upper()
        if not math.isfinite(shares) or shares < 0:
            return False, "份额必须为正数或「全部」。"
        want = 0.0
        if shares > 0:
            want = self._floor_shares(shares)
            if want < MIN_FUND_SHARES:
                return False, "赎回份额至少 0.01 份；如需全部赎回请发送「全部」。"
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

            held_shares = self._floor_shares(float(held["shares"]))
            sell_shares = held_shares if shares <= 0 else min(want, held_shares)
            nav = float(product["price"])
            amount = int(sell_shares * nav)

            held_ticks = self.tick_of() - int(held["hold_since"])
            fee_rate = next(rate for cap, rate in REDEEM_FEE_TIERS if held_ticks < cap)
            fee = int(amount * fee_rate)
            net = amount - fee

            fresh.gold += net
            await self.db.update_player(fresh, commit=False)

            remaining = round(held_shares - sell_shares, SHARE_DECIMALS)
            if remaining < MIN_FUND_SHARES:
                await self.db.conn.execute(
                    "DELETE FROM invest_holdings WHERE user_id = ? AND code = ?",
                    (player.user_id, code),
                )
            else:
                remaining_cost = round(int(held["cost"]) * remaining / held_shares)
                await self.db.conn.execute(
                    "UPDATE invest_holdings SET shares = ?, cost = ? WHERE user_id = ? AND code = ?",
                    (remaining, remaining_cost, player.user_id, code),
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

        tier_note = f"（持有 {held_ticks} 盘，赎回费 {fee_rate:.1%}）" if fee_rate else f"（持有 {held_ticks} 盘，免赎回费）"
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
            "current_hint": await self._current_hint(moment),
            "next_hint": await self._next_hint(moment),
        }

    async def _next_hint(self, moment: Optional[datetime] = None) -> Optional[dict]:
        """下一盘的风声（提前生成并落库，与 HH:30 推送、整点结算共用同一条）。

        面板不依赖订阅：任何人打 /仙市 都能提前看到下一盘的风声。
        """
        return await self.peek_hint(self.tick_of(moment) + 1)

    async def _current_hint(self, moment: Optional[datetime] = None) -> Optional[dict]:
        """当前盘的风声（已生效，含「盘面平静」）。

        缺记录时补生成：新部署或停机缺盘后首次打开面板，也能看到当盘风声，
        而不是空白等到整点结算。
        """
        tick = self.tick_of(moment)
        day, slot = divmod(tick, TICKS_PER_DAY)
        await self._ensure_news(day, slot)
        row = await self._load_news(day, slot)
        if row:
            item = row[0]
            item["hour"] = SLOT_HOURS[slot]
            return item
        return None

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

    @staticmethod
    def _merge_bars(bars: List[dict], period: str) -> List[dict]:
        """把日线再聚合成周线 / 月线：开=首日开、高=区间最高、低=区间最低、收=末日收。"""
        merged: List[dict] = []
        cur: Optional[dict] = None
        key = None
        for bar in bars:
            day = date.fromisoformat(bar["date"])
            slot_key = day.isocalendar()[:2] if period == "week" else (day.year, day.month)
            if slot_key != key:
                if cur is not None:
                    merged.append(cur)
                cur = {
                    "d": bar["d"],
                    "date": bar["date"],
                    "o": bar["o"],
                    "h": bar["h"],
                    "l": bar["l"],
                    "c": bar["c"],
                    "chg": 0.0,
                    "n": bar["n"],
                    "tags": list(bar["tags"]),
                }
                key = slot_key
                continue
            cur["h"] = max(cur["h"], bar["h"])
            cur["l"] = min(cur["l"], bar["l"])
            cur["c"] = bar["c"]
            cur["n"] += bar["n"]
            cur["date"] = bar["date"]
            for tag in bar["tags"]:
                if tag not in cur["tags"] and len(cur["tags"]) < 3:
                    cur["tags"].append(tag)
        if cur is not None:
            merged.append(cur)
        for bar in merged:
            bar["chg"] = (bar["c"] / bar["o"] - 1) if bar["o"] else 0.0
        return merged

    async def get_candles(self, code: str, period: str = "day", limit: int = 60,
                          moment: Optional[datetime] = None) -> Optional[dict]:
        """合成 K 线（日 / 周 / 月）。

        ``invest_prices`` 每盘只存收盘价，没有开高低，所以：

        * 日线「开」= 前一日收盘（首日取当日首盘收盘），价格连续、没有隔夜跳空；
        * 「高/低」= 当日盘内极值（含开盘位），影线反映的是盘内波动范围；
        * ``n`` = 这根用了几个盘次，当根还没走满 16 盘时前端画成「进行中」。

        只读：settle_to 之后再查，不额外推进行情。
        """
        await self.settle_to(moment)
        product = await self._load_product((code or "").strip().upper())
        if not product:
            return None
        period = str(period or "day").strip().lower()
        if period not in ("day", "week", "month"):
            period = "day"
        try:
            limit = max(8, min(int(limit or 60), 240))
        except (TypeError, ValueError):
            limit = 60

        rows = await self._rows(
            "SELECT tick, close, msg_tag FROM invest_prices WHERE code = ? ORDER BY tick",
            (product["code"],),
        )
        buckets: Dict[int, dict] = {}
        for row in rows:
            day = int(row["tick"]) // TICKS_PER_DAY
            bucket = buckets.setdefault(day, {"closes": [], "tags": []})
            bucket["closes"].append(float(row["close"]))
            tag = str(row["msg_tag"] or "").strip()
            if tag and tag not in bucket["tags"]:
                bucket["tags"].append(tag)

        bars: List[dict] = []
        prev_close: Optional[float] = None
        for day in sorted(buckets):
            closes = buckets[day]["closes"]
            open_ = prev_close if prev_close is not None else closes[0]
            close = closes[-1]
            bars.append(
                {
                    "d": day,
                    "date": (EPOCH_DATE + timedelta(days=day)).isoformat(),
                    "o": round(open_, 6),
                    "h": round(max([open_] + closes), 6),
                    "l": round(min([open_] + closes), 6),
                    "c": round(close, 6),
                    "chg": round((close / open_ - 1) if open_ else 0.0, 6),
                    "n": len(closes),
                    "tags": buckets[day]["tags"][:2],
                }
            )
            prev_close = close

        if period != "day":
            bars = self._merge_bars(bars, period)
        bars = bars[-limit:]

        tick = self.tick_of(moment)
        day_open = float(product["day_open"]) or float(product["price"])
        prev = bars[-2]["c"] if len(bars) >= 2 else (bars[-1]["o"] if bars else 0.0)
        return {
            "code": product["code"],
            "name": product["name"],
            "kind": str(product["kind"] or "stock"),
            "period": period,
            "tick": tick,
            "label": self.slot_label(tick),
            "price": float(product["price"]),
            "prev_close": round(prev, 6),
            "today": round((float(product["price"]) / day_open - 1) if day_open else 0.0, 6),
            "candles": bars,
            # 少于 8 根时前端降级成折线，免得蜡炷画成一根根竖线
            "enough": len(bars) >= 8,
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

    # ===== 仙市杠杆（v7.10.3）=====
    # 只做股票合约：玩家出保证金，仙市按倍数配资，盈亏按整仓结算。
    # 规则刻意做成「亏损不超过保证金」：爆仓即强平，不会倒欠仙市。
    #
    # v3.48.1 风控：单笔上限本身挡不住「同一标的连开多笔」——截图里的
    # 3 笔 S04 5 倍就是同一张 5500 万名义的大注。所以按玩家统计总敞口。
    MARGIN_LEVERAGES = (2, 3, 5)
    MARGIN_MIN = 10_000
    MARGIN_MAX_TOTAL = 5_000_000
    MARGIN_OPEN_FEE_RATE = 0.002
    MARGIN_CLOSE_FEE_RATE = 0.002
    MARGIN_BORROW_RATE_PER_TICK = 0.0008
    MARGIN_LIQUIDATION_RATIO = 0.20
    # 单玩家同时持仓笔数与同一标的笔数（0 = 不限）
    MARGIN_MAX_POSITIONS = 3
    MARGIN_MAX_POSITIONS_PER_CODE = 1
    # 单玩家总保证金上限 = 钱庄该境界存款上限 × 比例，且不低于地板值。
    # 复用 bank_manager 的分档表，避免两张表随境界调整各走各的。
    MARGIN_TOTAL_CAP_RATIO = 0.20
    MARGIN_TOTAL_CAP_FLOOR = 5_000_000
    # 杠杆倍数按标的每盘波动限制：(σ 上限, 允许的最高倍数)，超出最后一档沿用最后一档。
    # 5 倍的强平距离只有 -16%，只该给 σ≤5.5% 的标的（S01 5.5%、S03 4.5%）。
    MARGIN_LEVERAGE_SIGMA_CAPS = ((0.055, 5), (0.075, 3), (float("inf"), 2))

    def margin_total_cap(self, player: Player) -> int:
        """单玩家杠杆总保证金上限。

        默认按 ``level_index`` 取钱庄默认存款分档的 20%，并保底 5,000,000，
        这样低境界不会被卡得太死，高境界也不会因为钱庄额度膨胀而无限放大。
        插件配置 ``INVEST.MAX_TOTAL_MARGIN > 0`` 时，所有玩家统一用该值；
        若服主改过 ``config/game_config.json`` 的存款分档，也建议直接用该值覆盖。
        """
        if self.margin_total_cap_override > 0:
            return self.margin_total_cap_override
        try:
            level = max(0, int(getattr(player, "level_index", 0) or 0))
        except (TypeError, ValueError):
            level = 0
        deposit_cap = DEFAULT_DEPOSIT_CAPS[0][1]
        for start, value in DEFAULT_DEPOSIT_CAPS:
            if level >= start:
                deposit_cap = value
        return max(
            self.MARGIN_TOTAL_CAP_FLOOR,
            int(deposit_cap * self.MARGIN_TOTAL_CAP_RATIO),
        )

    def max_leverage_for(self, sigma: float) -> int:
        """按标的每盘 σ 给出允许的最高杠杆倍数。"""
        try:
            sigma = float(sigma or 0)
        except (TypeError, ValueError):
            sigma = 0.0
        if sigma <= 0:
            # 波动缺失/为 0 的脏数据按最高风险处理，不能默认放行 5 倍
            return self.margin_leverage_caps[-1][1]
        for limit, leverage in self.margin_leverage_caps:
            if sigma <= limit:
                return leverage
        return self.margin_leverage_caps[-1][1]

    async def _check_margin_exposure(
        self, player: Player, code: str, product_name: str, margin: int,
    ) -> Tuple[bool, str, int, int]:
        """在开仓事务内校验单玩家敞口。

        返回 ``(通过, 错误文案, 已用保证金, 总上限)``。必须在 ``BEGIN IMMEDIATE``
        里调用：只读一次再写会和并发开仓互相看不见，玩家能连点开出一串超限仓位。
        """
        rows = await self._rows(
            "SELECT code, margin FROM invest_margin_positions "
            "WHERE user_id = ? AND status = 'open'",
            (player.user_id,),
        )
        used = sum(int(row.get("margin") or 0) for row in rows)
        cap = self.margin_total_cap(player)
        if self.margin_max_positions > 0 and len(rows) >= self.margin_max_positions:
            return False, (
                f"❌ 最多同时持有 {self.margin_max_positions} 笔杠杆持仓，"
                f"当前已有 {len(rows)} 笔，请先平掉一些。"
            ), used, cap
        if self.margin_max_positions_per_code > 0:
            same = sum(1 for row in rows if str(row.get("code") or "").upper() == code)
            if same >= self.margin_max_positions_per_code:
                return False, (
                    f"❌ {product_name}（{code}）已有 {same} 笔未平仓，"
                    f"同一标的最多 {self.margin_max_positions_per_code} 笔。\n"
                    "想加大仓位请先平掉旧仓，或换一个标的分散。"
                ), used, cap
        if used + margin > cap:
            return False, (
                f"❌ 杠杆总保证金超过上限：已用 {used:,} + 本笔 {margin:,} "
                f"> 上限 {cap:,}。\n"
                f"当前还能开 {max(0, cap - used):,} 灵石。"
            ), used, cap
        return True, "", used, cap

    async def open_margin(self, player: Player, code: str, leverage: int, margin: int) -> Tuple[bool, str]:
        code = (code or "").strip().upper()
        try:
            leverage = int(leverage)
            margin = int(margin)
        except (TypeError, ValueError):
            return False, "❌ 倍数与保证金必须是整数。"
        if leverage not in self.MARGIN_LEVERAGES:
            return False, f"❌ 杠杆倍数只能是 {'/'.join(str(x) for x in self.MARGIN_LEVERAGES)} 倍。"
        if margin < self.MARGIN_MIN:
            return False, f"❌ 保证金至少 {self.MARGIN_MIN:,} 灵石。"
        if margin > self.MARGIN_MAX_TOTAL:
            return False, f"❌ 单笔保证金上限 {self.MARGIN_MAX_TOTAL:,} 灵石。"
        opened, why = self._check_market()
        if not opened:
            return False, why
        await self.settle_to()
        product = await self._load_product(code)
        if not product or product.get("kind") != "stock":
            return False, f"❌ 仙市中不存在股票代码「{code}」。"
        if product.get("status") != "trading":
            return False, "❌ 该标的已停牌。"
        price = float(product["price"])
        if price <= 0:
            return False, "❌ 当前价格异常，无法开仓。"

        # 倍数上限按标的波动收口：5 倍的强平距离只有 -16%，
        # 放在 σ=8.5%/盘的 S05 上，一个交易日内爆仓概率超过 2/3。
        sigma = float(product.get("sigma") or 0)
        allowed_leverage = self.max_leverage_for(sigma)
        if leverage > allowed_leverage:
            choices = "/".join(str(x) for x in self.MARGIN_LEVERAGES if x <= allowed_leverage)
            return False, (
                f"❌ {product['name']}（{code}）每盘波动 {sigma:.1%}，"
                f"波动越大强平距离相对越窄，该标的最多只能开 {allowed_leverage} 倍。\n"
                f"可选倍数：{choices} 倍。想开 {leverage} 倍请换波动更低的标的。"
            )

        # 与现货买股同一道贷款门槛：欠着钱还能加杠杆，
        # 等于让「贷款买币 → 爆仓 → 拍拍屁股走人」成为免费的单向期权。
        loan = await self._active_loan(player.user_id)
        if loan > LOAN_LIMIT_FOR_STOCK:
            return False, (
                f"你当前贷款余额 {loan:,} 灵石，超过 {LOAN_LIMIT_FOR_STOCK:,} 的入市门槛。\n"
                "请先还款后再开杠杆仓。"
            )

        total = margin * leverage
        fee = max(1, int(round(total * self.MARGIN_OPEN_FEE_RATE)))
        if player.gold < margin + fee:
            return False, (
                f"❌ 开 {leverage} 倍杠杆需要保证金 {margin:,} + 开仓费 {fee:,}，"
                f"你当前只有 {player.gold:,}。"
            )
        borrow = total - margin
        shares = round(total / price, 4)
        tick = self.tick_of()
        now = int(time.time())

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 敞口校验必须和扣款/落库同一事务：放在事务外，两个并发开仓
            # 会各自读到旧笔数，双双通过检查后超限。
            passed, why, used, cap = await self._check_margin_exposure(
                player, code, str(product.get("name") or code), margin
            )
            if not passed:
                await self.db.conn.rollback()
                return False, why
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (margin + fee, player.user_id, margin + fee),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 灵石不足，开仓失败。"
            cursor = await self.db.conn.execute(
                "INSERT INTO invest_margin_positions "
                "(user_id, code, leverage, margin, borrow, entry_price, shares, "
                "opened_tick, opened_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                (player.user_id, code, leverage, margin, borrow, price, shares, tick, now),
            )
            position_id = cursor.lastrowid
            await self.db.conn.commit()
            player.gold -= margin + fee
        except Exception:
            await self.db.conn.rollback()
            raise
        return True, (
            f"📈 仙市杠杆开仓成功\n"
            f"━━━━━━━━━━━━━━━\n"
            f"标的：{product['name']}（{code}）\n"
            f"方向：做多｜杠杆：{leverage} 倍\n"
            f"保证金：{margin:,}｜开仓费：{fee:,}\n"
            f"配资：{borrow:,}｜名义仓位：{total:,}\n"
            f"开仓价：{price:.2f}｜持仓编号：{position_id}\n"
            f"额度占用：{used + margin:,} / {cap:,}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⚠️ 每盘配资利息 {self.MARGIN_BORROW_RATE_PER_TICK * 100:.2f}%，"
            f"保证金亏至 {int(self.MARGIN_LIQUIDATION_RATIO * 100)}% 将被强平。\n"
            f"💡 `/仙市 杠杆 平仓 {position_id}` 主动平仓"
        )

    @staticmethod
    def _margin_interest(position: dict, tick: int) -> int:
        borrow = float(position.get("borrow") or 0)
        opened_tick = int(position.get("opened_tick") or tick)
        held = max(1, tick - opened_tick)
        return max(0, int(round(borrow * InvestManager.MARGIN_BORROW_RATE_PER_TICK * held)))

    async def _margin_equity(self, position: dict, price: float, tick: int) -> dict:
        shares = float(position.get("shares") or 0)
        borrow = float(position.get("borrow") or 0)
        margin = float(position.get("margin") or 0)
        value = shares * price
        interest = self._margin_interest(position, tick)
        close_fee = max(0, int(round(value * self.MARGIN_CLOSE_FEE_RATE)))
        equity = value - borrow - interest - close_fee
        return {
            "value": value,
            "interest": interest,
            "close_fee": close_fee,
            "equity": equity,
            "pnl": equity - margin,
        }

    @staticmethod
    def _liquidation_price(position: dict, interest: float, margin: float) -> float:
        """反解强平触发价，口径必须与 :meth:`_margin_equity` 完全一致。

        强平条件是 ``value - borrow - interest - close_fee <= margin * 20%``，
        其中 ``close_fee = value * MARGIN_CLOSE_FEE_RATE``。解出：

            price = (borrow + interest + margin * ratio) / (shares * (1 - close_rate))

        旧写法 ``(borrow + margin * ratio) / shares`` 漏掉了利息与平仓费，
        显示的强平线偏低——玩家按它设止损，实际会被提前强平；而且利息随
        持有盘数增长，强平线是动态上移的，必须把 interest 传进来。
        """
        shares = float(position.get("shares") or 0)
        if shares <= 0:
            return 0.0
        borrow = float(position.get("borrow") or 0)
        need = borrow + float(interest or 0) + float(margin or 0) * InvestManager.MARGIN_LIQUIDATION_RATIO
        denom = shares * (1.0 - InvestManager.MARGIN_CLOSE_FEE_RATE)
        return max(0.0, need / denom) if denom > 0 else 0.0

    async def close_margin(self, player: Player, position_id: int, *,
                           force: bool = False, liquidated: bool = False) -> Tuple[bool, str]:
        await self.settle_to()
        rows = await self._rows(
            "SELECT * FROM invest_margin_positions WHERE id = ? AND user_id = ? AND status = 'open'",
            (int(position_id), player.user_id),
        )
        if not rows:
            return False, "❌ 没有找到可平仓的杠杆持仓。"
        position = rows[0]
        product = await self._load_product(position["code"])
        price = float(product["price"]) if product else float(position["entry_price"])
        tick = self.tick_of()
        calc = await self._margin_equity(position, price, tick)
        equity = calc["equity"]
        margin = float(position.get("margin") or 0)
        if not force and equity <= margin * self.MARGIN_LIQUIDATION_RATIO:
            liquidated = True
        payout = max(0, int(round(equity)))
        pnl = payout - int(margin)
        status = "liquidated" if liquidated else "closed"

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE invest_margin_positions SET status = ?, close_price = ?, payout = ?, "
                "pnl = ?, closed_at = ? WHERE id = ? AND status = 'open'",
                (status, price, payout, pnl, int(time.time()), int(position_id)),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 该持仓已被结算。"
            if payout > 0:
                await self.db.conn.execute(
                    "UPDATE players SET gold = gold + ? WHERE user_id = ?",
                    (payout, player.user_id),
                )
            await self.db.conn.commit()
            player.gold += payout
        except Exception:
            await self.db.conn.rollback()
            raise
        title = "💥 强制平仓" if liquidated else "📉 杠杆平仓"
        return True, (
            f"{title}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"标的：{position['code']}｜{int(position['leverage'])} 倍\n"
            f"开仓价：{float(position['entry_price']):.2f}｜平仓价：{price:.2f}\n"
            f"配资利息：{calc['interest']:,}｜平仓费：{calc['close_fee']:,}\n"
            f"保证金：{int(margin):,}\n"
            f"返还：{payout:,}｜盈亏：{pnl:+,}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 当前灵石：{player.gold:,}"
        )

    async def liquidate_due(self) -> int:
        """后台自动强平：保证金亏至强平线的持仓立即平仓。

        玩家离线时价格仍会波动，如果没有自动强平，持仓可以一直拖到回本，
        杠杆就失去风险意义。这里逐仓检查，平仓本身带条件更新，天然幂等。
        """
        await self.settle_to()
        positions = await self._rows(
            "SELECT * FROM invest_margin_positions WHERE status = 'open' ORDER BY id"
        )
        if not positions:
            return 0
        tick = self.tick_of()
        liquidated = 0
        for pos in positions:
            try:
                player = await self.db.get_player_by_id(pos["user_id"])
                if not player:
                    continue
                product = await self._load_product(pos["code"])
                price = float(product["price"]) if product else float(pos["entry_price"])
                calc = await self._margin_equity(pos, price, tick)
                margin = float(pos.get("margin") or 0)
                if calc["equity"] <= margin * self.MARGIN_LIQUIDATION_RATIO:
                    ok, _ = await self.close_margin(
                        player, int(pos["id"]), force=True, liquidated=True
                    )
                    if ok:
                        liquidated += 1
            except Exception as exc:
                logger.warning(f"【仙市杠杆】自动强平失败 持仓{pos.get('id')}: {exc}")
        return liquidated

    async def get_margin_status(self, player: Player) -> str:
        await self.settle_to()
        positions = await self._rows(
            "SELECT * FROM invest_margin_positions WHERE user_id = ? AND status = 'open' "
            "ORDER BY id DESC", (player.user_id,)
        )
        closed = await self._rows(
            "SELECT * FROM invest_margin_positions WHERE user_id = ? AND status != 'open' "
            "ORDER BY closed_at DESC LIMIT 5", (player.user_id,)
        )
        lines = ["📈 仙市杠杆", "━━━━━━━━━━━━━━━"]
        tick = self.tick_of()
        cap = self.margin_total_cap(player)
        used = sum(int(pos.get("margin") or 0) for pos in positions)
        max_pos = self.margin_max_positions or "∞"
        lines.append(
            f"额度 {used:,} / {cap:,}｜持仓 {len(positions)}/{max_pos} 笔"
        )
        if not positions:
            lines.append("当前没有未平仓的杠杆持仓。")
        for pos in positions:
            product = await self._load_product(pos["code"])
            price = float(product["price"]) if product else float(pos["entry_price"])
            calc = await self._margin_equity(pos, price, tick)
            margin = float(pos.get("margin") or 0)
            rate = calc["pnl"] / margin if margin else 0.0
            lines.append(
                f"[{pos['id']}] {pos['code']} {int(pos['leverage'])}倍 "
                f"保证金 {int(margin):,}"
            )
            lines.append(
                f"    现价 {price:.2f}（开 {float(pos['entry_price']):.2f}）"
                f"｜权益 {int(calc['equity']):,}｜盈亏 {int(calc['pnl']):+,}（{rate:+.1%}）"
            )
            lines.append(
                f"    利息 {calc['interest']:,}｜强平线约 "
                f"{self._liquidation_price(pos, calc['interest'], margin):.2f}"
            )
        if closed:
            lines.append("—— 最近结算 ——")
            for pos in closed:
                tag = "强平" if pos["status"] == "liquidated" else "平仓"
                lines.append(
                    f"[{pos['id']}] {pos['code']} {int(pos['leverage'])}倍 {tag}"
                    f"｜返还 {int(pos['payout'] or 0):,}｜盈亏 {int(pos['pnl'] or 0):+,}"
                )
        per_code = self.margin_max_positions_per_code or "∞"
        lev_text = "，".join(
            [f"σ≤{limit:.1%} 可 {lev} 倍"
             for limit, lev in self.margin_leverage_caps
             if limit != float("inf")]
            + [f"更高 {self.margin_leverage_caps[-1][1]} 倍"]
        )
        lines.extend([
            "━━━━━━━━━━━━━━━",
            "💡 `/仙市 杠杆 <代码> <倍数> <保证金>` 开多仓",
            "💡 `/仙市 杠杆 平仓 <编号>` 主动平仓",
            "⚠️ 杠杆放大盈亏，保证金亏至 20% 会被强制平仓。",
            f"📏 单玩家总保证金按境界限额；同一标的 {per_code} 笔、"
            f"总持仓 {max_pos} 笔；{lev_text}。",
        ])
        return "\n".join(lines)
