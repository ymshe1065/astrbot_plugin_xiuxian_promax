# handlers/invest_agent_handlers.py
"""仙市投资代理人指令：/仙市 代理 ..."""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent

from ..managers.invest_agent_manager import AGENT_DEFS, AGENT_ALIASES, InvestAgentManager
from ..models import Player
from .utils import player_required

LINE = "━━━━━━━━━━━━━━━━━━━━━━━━"

USAGE = (
    "「 仙市投资代理人 」\n"
    f"{LINE}\n"
    "查看：/仙市 代理\n"
    "雇佣：/仙市 代理 雇佣 <青禾|玄衡|赤霄> <本金> <8|16|32>\n"
    "状态：/仙市 代理 状态\n"
    "停止：/仙市 代理 停止\n"
    "领取：/仙市 代理 领取\n"
    "记录：/仙市 代理 记录\n"
    f"{LINE}\n"
    "代理人只做股票/基金，不做杠杆；不保本，管理费开仓即扣。\n"
    "等级：每个代理人独立成长，按委托盈亏获得/扣减经验，升级降费并提高本金上限。"
)


class InvestAgentHandlers:
    def __init__(self, db, invest_agent_mgr: InvestAgentManager):
        self.db = db
        self.agent_mgr = invest_agent_mgr

    @player_required
    async def handle(self, player: Player, event: AstrMessageEvent, args: str = ""):
        tokens = str(args or "").strip().split()
        if tokens and tokens[0] in ("代理", "代理人", "投资代理", "agent"):
            tokens = tokens[1:]
        if not tokens or tokens[0] in ("状态", "查看", "列表", "help", "帮助"):
            yield event.plain_result(await self.agent_mgr.status_text(player.user_id))
            return
        sub = tokens[0]
        if sub in ("雇佣", "委托", "start"):
            if len(tokens) < 4:
                yield event.plain_result(USAGE)
                return
            agent_key = tokens[1]
            try:
                principal = int(tokens[2])
                duration = int(tokens[3])
            except (TypeError, ValueError):
                yield event.plain_result("❌ 本金和期限必须是整数。")
                return
            ok, msg = await self.agent_mgr.start(player, agent_key, principal, duration)
            yield event.plain_result(("✅ " if ok else "❌ ") + msg.lstrip("✅❌ "))
            return
        if sub in ("停止", "取消", "stop"):
            ok, msg = await self.agent_mgr.stop(player)
            yield event.plain_result(("✅ " if ok else "❌ ") + msg.lstrip("✅❌ "))
            return
        if sub in ("领取", "结算", "claim"):
            ok, msg = await self.agent_mgr.claim(player)
            yield event.plain_result(("✅ " if ok else "❌ ") + msg.lstrip("✅❌ "))
            return
        if sub in ("记录", "历史", "history"):
            yield event.plain_result(await self.agent_mgr.history_text(player.user_id))
            return
        yield event.plain_result(USAGE)

    @staticmethod
    def agent_keys() -> list:
        return list(AGENT_DEFS.keys())

    @staticmethod
    def normalize_agent(value: str) -> str:
        return AGENT_ALIASES.get(str(value).strip(), str(value).strip().lower())
