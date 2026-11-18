# handlers/gamble_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.gamble_manager import GambleManager
from ..models import Player
from .utils import player_required, set_event_flag

class GambleHandlers:
    def __init__(self, db: DataBase, gamble_mgr: GambleManager):
        self.db = db
        self.gamble_mgr = gamble_mgr
        self.last_gamble_success = False
        self.last_blackmarket_success = False

    @player_required
    async def handle_cut_stone(self, player: Player, event: AstrMessageEvent, count: str = "1"):
        set_event_flag(event, "gamble_success", False)
        try:
            count_int = int(count)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的数字！")
            return
        success, msg = await self.gamble_mgr.cut_stone(player, count_int)
        set_event_flag(event, "gamble_success", bool(success))
        yield event.plain_result(msg)

    @staticmethod
    def _parse_index(text: str) -> int:
        try:
            return int(str(text or "").strip().translate(
                str.maketrans("０１２３４５６７８９", "0123456789")
            ))
        except (TypeError, ValueError):
            return 0

    async def _show_black_market(self, event: AstrMessageEvent):
        success, msg = self.gamble_mgr.get_black_market()
        # 网页端：换成网格后「一件商品一格」，不必在十几行文本里找编号
        try:
            from ..utils.image_generator import render_web_card_async
            entries = self.gamble_mgr.build_black_market_entries()
            if entries:
                card = await render_web_card_async(
                    "shop",
                    "🕶️ 黑市 · 商店",
                    f"{len(entries)} 件在售 · 每小时刷新",
                    [{
                        "name": "在售",
                        "view": "grid",
                        "entries": entries,
                        "facet": {"label": "", "okLabel": "在售"},
                    }],
                    "点格子购买 · /黑市 购买 <编号> · 真假自负",
                )
                if card:
                    yield event.image_result(card)
                    return
        except Exception:
            pass
        yield event.plain_result(msg)

    @player_required
    async def handle_market(self, player: Player, event: AstrMessageEvent, args: str = ""):
        """黑市入口：商店 / 原石 / 开窗 / 切割 / 购买。"""
        set_event_flag(event, "gamble_success", False)
        set_event_flag(event, "blackmarket_success", False)
        text = (args or "").strip()
        # 贪婪参数在不同适配器下可能丢失，回退到原始消息再剥离一次指令前缀
        if not text:
            try:
                raw = (event.get_message_str() or "").strip().lstrip("/").strip()
                if raw.startswith("黑市"):
                    text = raw[2:].strip()
            except Exception:
                text = ""
        if not text or text in ("帮助", "菜单", "help"):
            yield event.plain_result(
                "🕶️ 黑市\n"
                "━━━━━━━━━━━━━━━\n"
                "🛒 `/黑市 商店` 查看本小时黑市货架\n"
                "💎 `/黑市 原石` 查看五档赌石与真假线索\n"
                "⛏️ `/黑市 切原石 <编号>` 切开本小时该档原石\n"
                "🔎 `/黑市 开窗 <编号>` 付费揭示一条真线索（每块仅一次）\n"
                "💳 `/黑市 购买 <编号>` 购买黑市商店商品\n"
                "━━━━━━━━━━━━━━━\n"
                "⚠️ 商店真假自负；赌石十切九空，线索真假混杂，切垮自负。\n"
                "💡 旧指令 `/切原石 <数量>`、`/黑市购买 <编号>` 仍可使用。"
            )
            return

        parts = text.split()
        sub = parts[0]
        rest = " ".join(parts[1:]).strip()
        if sub in ("商店", "商城", "市场", "货架"):
            async for result in self._show_black_market(event):
                yield result
            return
        if sub in ("原石", "赌石", "看石"):
            success, msg = await self.gamble_mgr.get_stone_market(player.user_id)
            yield event.plain_result(msg)
            return
        if sub in ("切原石", "切石", "切"):
            index = self._parse_index(rest or (parts[1] if len(parts) > 1 else ""))
            if index <= 0:
                yield event.plain_result("❌ 请指定原石编号，例如：/黑市 切原石 3")
                return
            success, msg = await self.gamble_mgr.cut_tier_stone(player, index)
            set_event_flag(event, "gamble_success", bool(success))
            yield event.plain_result(msg)
            return
        if sub in ("开窗", "开小窗"):
            index = self._parse_index(rest or (parts[1] if len(parts) > 1 else ""))
            if index <= 0:
                yield event.plain_result("❌ 请指定原石编号，例如：/黑市 开窗 2")
                return
            success, msg = await self.gamble_mgr.open_stone_window(player, index)
            yield event.plain_result(msg)
            return
        if sub in ("购买", "买"):
            index = self._parse_index(rest or (parts[1] if len(parts) > 1 else ""))
            if index <= 0:
                yield event.plain_result("❌ 请指定商品编号，例如：/黑市 购买 1")
                return
            success, msg = await self.gamble_mgr.buy_black_market(player, index)
            set_event_flag(event, "blackmarket_success", bool(success))
            yield event.plain_result(msg)
            return
        yield event.plain_result(
            f"❌ 未知的黑市子命令「{sub}」。\n"
            "💡 可用：`/黑市 商店`、`/黑市 原石`、`/黑市 切原石 <编号>`、"
            "`/黑市 开窗 <编号>`、`/黑市 购买 <编号>`"
        )

    @player_required
    async def handle_buy_market(self, player: Player, event: AstrMessageEvent, index: str = "0"):
        set_event_flag(event, "blackmarket_success", False)
        try:
            idx_int = int(index)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的编号！")
            return
        success, msg = await self.gamble_mgr.buy_black_market(player, idx_int)
        set_event_flag(event, "blackmarket_success", bool(success))
        yield event.plain_result(msg)