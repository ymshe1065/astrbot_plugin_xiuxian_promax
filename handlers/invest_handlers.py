# handlers/invest_handlers.py
"""仙市投资系统处理器：行情查询与基金/股票交易。

展示层只做格式化和参数解析，所有数值判断都在 InvestManager 内完成，
不要在这里写业务规则（否则补结算与实时路径会长出两套逻辑）。
"""

from __future__ import annotations

import time

from astrbot.api.event import AstrMessageEvent

from ..data import DataBase
from ..managers.invest_manager import InvestManager, LOT_SIZE, PATTERN_LABEL
from ..models import Player
from .utils import player_required

__all__ = ["InvestHandlers"]

LINE = "━━━━━━━━━━━━━━━━━━━━━━━━"

SIDE_LABEL = {
    "buy": "买入",
    "sell": "卖出",
    "subscribe": "申购",
    "redeem": "赎回",
}


def _width(text: str) -> int:
    """按显示宽度算长度：中日韩字符占两格，用于纯文本表格对齐。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _pct(value: float) -> str:
    if abs(value) < 0.00005:
        return "—"
    arrow = "▲" if value > 0 else "▼"
    return f"{arrow}{abs(value) * 100:.2f}%"


def _money(value: int) -> str:
    return f"{value:,}"


def _fmt_price(value: float, kind: str) -> str:
    """基金净值要 4 位小数才有区分度，股票 2 位就够。"""
    return f"{value:.4f}" if kind == "fund" else f"{value:.2f}"


class InvestHandlers:
    """仙市投资处理器"""

    def __init__(self, db: DataBase, invest_mgr: InvestManager):
        self.db = db
        self.invest_mgr = invest_mgr

    # ===== 行情总览 =====

    @player_required
    async def handle_market(self, player: Player, event: AstrMessageEvent):
        data = await self.invest_mgr.get_overview()

        if data["open"]:
            wait = data["seconds_to_close"]
            hours, rest = divmod(wait, 3600)
            tail = f"距收市 {hours} 小时 {rest // 60} 分"
            headline = f"「 仙市 · 第 {data['tick'] % 8 + 1} 盘 · {data['label']} 」"
        else:
            wait = data["seconds_to_open"]
            hours, rest = divmod(wait, 3600)
            tail = f"距开盘 {hours} 小时 {rest // 60} 分"
            headline = "「 仙市 · 已收市 」"

        lines = [headline, LINE, "今日消息"]
        for news in data["news"]:
            stamp = f"{news['hour']:02d}:00"
            if news.get("published"):
                prefix = "全市场 · " if news["code"] == "*" else ""
                lines.append(f"· {stamp} {prefix}{news['title']}")
            else:
                lines.append(f"· {stamp} 待发")

        lines.extend([LINE, "股票"])
        for item in data["items"]:
            if item["kind"] != "stock":
                continue
            lines.append(
                f"{item['code']} {_pad(item['name'], 10)}"
                f"{item['price']:>9.2f}  "
                f"{_pct(item['change']):>8}  "
                f"{_pct(item['today']):>8}  "
                f"{_pct(item['recent']):>8}"
            )
        lines.append("　（本盘 / 今日 / 近5盘）")

        funds = [item for item in data["items"] if item["kind"] == "fund"]
        if funds:
            lines.extend([LINE, "基金"])
            for item in funds:
                stars = "★" * item["risk"]
                lines.append(
                    f"{item['code']} {_pad(item['name'], 10)}"
                    f"{item['price']:>9.4f}  "
                    f"{_pct(item['change']):>8}  "
                    f"{_pct(item['today']):>8}  {stars}"
                )

        lines.extend(
            [
                LINE,
                f"{'闭市中' if not data['open'] else '下盘 ' + data['next_tick']}　·　{tail}",
                "指令：/行情 <代码>　/买入股 <代码> <手>　/申购 <代码> <金额>",
            ]
        )
        yield event.plain_result("\n".join(lines))

    @player_required
    async def handle_quote(self, player: Player, event: AstrMessageEvent, code: str = ""):
        code = (code or "").strip().upper()
        if not code:
            yield event.plain_result(
                "❌ 请输入标的代码，例如：/行情 S01\n"
                "股票：S01 丹阁 · S02 器阁 · S03 镖局 · S04 灵田 · S05 御兽 · S06 拍卖行\n"
                "基金：F01 灵脉·稳 · F02 仙市·衡 · F03 险峰·进"
            )
            return

        quote = await self.invest_mgr.get_quote(code)
        if not quote:
            yield event.plain_result(f"❌ 仙市中不存在代码「{code}」。")
            return

        deviation = quote["price"] / quote["base"] - 1 if quote["base"] else 0.0
        lines = [
            f"「 {quote['name']} · {quote['code']} 」",
            LINE,
            f"现价 {_fmt_price(quote['price'], quote['kind'])}　"
            f"本盘 {_pct(quote['change'])}　今日 {_pct(quote['today'])}",
            f"基准 {_fmt_price(quote['base'], quote['kind'])}　偏离 {_pct(deviation)}",
        ]
        if quote["kind"] == "stock":
            lines.append(f"日熔断 ±{quote['day_limit']:.0%}　·　1 手 = {LOT_SIZE} 股")
        else:
            lines.append(f"风险 {'★' * quote['risk']}　·　赎回费按持有盘数递减")

        if quote["history"]:
            lines.extend([LINE, "近 8 盘"])
            prev_day = None
            for row in quote["history"]:
                tick = int(row["tick"])
                day, _slot = divmod(tick, 8)
                if prev_day is not None and day != prev_day:
                    lines.append("　── 次日 ──")
                prev_day = day
                stamp = self.invest_mgr.slot_label(tick)
                tag = PATTERN_LABEL.get(row["msg_tag"] or "", "")
                suffix = f"　{tag}" if tag else ""
                lines.append(
                    f"{stamp}  {_fmt_price(float(row['close']), quote['kind']):>9}  "
                    f"{_pct(float(row['change_rate']))}{suffix}"
                )
        else:
            lines.extend([LINE, "暂无历史行情"])

        if quote["kind"] == "stock":
            hint = f"/买入股 {quote['code']} <手数>　/卖出股 {quote['code']} <手数>　/仙市"
        else:
            hint = f"/申购 {quote['code']} <金额>　/赎回 {quote['code']} <份额|全部>　/仙市"
        lines.extend([LINE, f"指令：{hint}"])
        yield event.plain_result("\n".join(lines))

    # ===== 交易 =====

    @player_required
    async def handle_buy_stock(self, player: Player, event: AstrMessageEvent,
                               code: str = "", lots: str = ""):
        try:
            lots_int = int(str(lots).strip())
        except (TypeError, ValueError):
            yield event.plain_result("❌ 手数必须是整数，例如：/买入股 S01 2（1 手 = 100 股）")
            return
        if not code:
            yield event.plain_result("❌ 请输入标的代码，例如：/买入股 S01 2")
            return

        ok, msg = await self.invest_mgr.buy_stock(player, code, lots_int)
        prefix = "✅" if ok else "❌"
        yield event.plain_result(f"{prefix} {msg}")

    @player_required
    async def handle_sell_stock(self, player: Player, event: AstrMessageEvent,
                                code: str = "", lots: str = ""):
        try:
            lots_int = int(str(lots).strip())
        except (TypeError, ValueError):
            yield event.plain_result("❌ 手数必须是整数，例如：/卖出股 S01 1")
            return
        if not code:
            yield event.plain_result("❌ 请输入标的代码，例如：/卖出股 S01 1")
            return

        ok, msg = await self.invest_mgr.sell_stock(player, code, lots_int)
        prefix = "✅" if ok else "❌"
        yield event.plain_result(f"{prefix} {msg}")

    @player_required
    async def handle_subscribe(self, player: Player, event: AstrMessageEvent,
                               code: str = "", amount: str = ""):
        try:
            amount_int = int(str(amount).strip())
        except (TypeError, ValueError):
            yield event.plain_result("❌ 申购金额必须是整数，例如：/申购 F01 10000")
            return
        if not code:
            yield event.plain_result("❌ 请输入基金代码，例如：/申购 F01 10000")
            return

        ok, msg = await self.invest_mgr.subscribe_fund(player, code, amount_int)
        prefix = "✅" if ok else "❌"
        yield event.plain_result(f"{prefix} {msg}")

    @player_required
    async def handle_redeem(self, player: Player, event: AstrMessageEvent,
                            code: str = "", shares: str = ""):
        if not code:
            yield event.plain_result("❌ 请输入基金代码，例如：/赎回 F01 全部")
            return

        raw = str(shares).strip()
        shares_value = 0.0
        if raw and raw not in ("全部", "all", "ALL"):
            try:
                shares_value = float(raw)
            except ValueError:
                yield event.plain_result("❌ 份额必须是数字或「全部」，例如：/赎回 F01 1000")
                return

        ok, msg = await self.invest_mgr.redeem_fund(player, code, shares_value)
        prefix = "✅" if ok else "❌"
        yield event.plain_result(f"{prefix} {msg}")

    # ===== 持仓与流水 =====

    @player_required
    async def handle_holdings(self, player: Player, event: AstrMessageEvent):
        holdings = await self.invest_mgr.get_holdings(player.user_id)
        if not holdings:
            yield event.plain_result(
                "📭 你还没有任何持仓。\n"
                "发送「/仙市」查看行情，用「/买入股 S01 1」或「/申购 F01 10000」建仓。"
            )
            return

        lines = ["「 我的持仓 」", LINE]
        total_value = 0
        total_profit = 0

        stocks = [h for h in holdings if h["kind"] == "stock"]
        if stocks:
            lines.append("股票")
            for item in stocks:
                total_value += item["value"]
                total_profit += item["profit"]
                lines.append(
                    f"{item['code']} {_pad(item['name'], 10)}"
                    f"{item['shares']:>7.0f}股  成本 {_money(item['cost'])}  "
                    f"市值 {_money(item['value'])}  "
                    f"{'+' if item['profit'] >= 0 else ''}{_money(item['profit'])}"
                    f"（{item['profit_rate']:+.1%}）"
                )

        funds = [h for h in holdings if h["kind"] == "fund"]
        if funds:
            lines.append("基金")
            for item in funds:
                total_value += item["value"]
                total_profit += item["profit"]
                lines.append(
                    f"{item['code']} {_pad(item['name'], 10)}"
                    f"{item['shares']:>9.2f}份  成本 {_money(item['cost'])}  "
                    f"市值 {_money(item['value'])}  "
                    f"{'+' if item['profit'] >= 0 else ''}{_money(item['profit'])}"
                    f"（{item['profit_rate']:+.1%}，持有 {item['hold_ticks']} 盘）"
                )

        dividends = await self.invest_mgr.get_dividends(player.user_id, 3)
        if dividends:
            lines.extend([LINE, "最近分红"])
            for row in dividends:
                stamp = time.strftime("%m-%d %H:%M", time.localtime(int(row["created_at"])))
                lines.append(f"{stamp} {row['name'] or row['code']} +{_money(int(row['amount']))}")

        lines.extend(
            [
                LINE,
                f"合计市值 {_money(total_value)}　总盈亏 {'+' if total_profit >= 0 else ''}{_money(total_profit)}",
                f"现有灵石 {_money(player.gold)}",
            ]
        )
        yield event.plain_result("\n".join(lines))

    @player_required
    async def handle_orders(self, player: Player, event: AstrMessageEvent):
        orders = await self.invest_mgr.get_orders(player.user_id, 20)
        if not orders:
            yield event.plain_result("📋 暂无仙市成交记录。")
            return

        lines = ["「 仙市流水 · 最近 20 笔 」", LINE]
        for row in orders:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(int(row["created_at"])))
            side = SIDE_LABEL.get(row["side"], row["side"])
            name = row["name"] or row["code"]
            if row["side"] in ("subscribe", "redeem"):
                size = f"{float(row['shares']):.2f}份"
            else:
                size = f"{float(row['shares']):.0f}股"
            fee_part = f"　费 {_money(int(row['fee']))}" if int(row["fee"]) else ""
            lines.append(
                f"{stamp} {side} {name} {size} @ {_fmt_price(float(row['price']), 'fund' if str(row['code']).startswith('F') else 'stock')}　"
                f"额 {_money(int(row['amount']))}{fee_part}"
            )
        lines.extend([LINE, f"现有灵石 {_money(player.gold)}"])
        yield event.plain_result("\n".join(lines))
