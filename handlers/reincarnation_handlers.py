# handlers/reincarnation_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.reincarnation_manager import ReincarnationManager
from ..models import Player
from ..core.reincarnation_rules import (
    FATE_EXCHANGE_MAX,
    FREE_ROOT_REROLLS,
    GOLD_PER_FATE_POINT,
    NEWLIFE_ESCORT_TRIPS_PER_STACK,
    SHOP_GROUPS,
    SHOP_ITEMS,
    shop_price,
    fate_exchange_key,
    fate_exchange_points,
    format_permanent_summary,
    item_owned,
    items_in_group,
    parse_bonus,
)
from .utils import player_required

# /飞升 的确认词：必须显式带一个才会真正执行，裸发 /飞升 只出确认页
ASCEND_CONFIRM_WORDS = ("确认", "确定", "执行", "confirm")


def parse_ascend_args(args: str) -> tuple[str, bool]:
    """把 ``/飞升`` 的参数拆成 (传家宝名, 是否已确认)。

    支持 ``/飞升``、``/飞升 确认``、``/飞升 青锋剑``、``/飞升 青锋剑 确认``。
    确认词只认末尾，避免把名为「确认」之类的物品误当确认。
    """
    tokens = str(args or "").replace("\u3000", " ").split()
    confirmed = False
    while tokens and tokens[-1].lower() in ASCEND_CONFIRM_WORDS:
        confirmed = True
        tokens.pop()
    return " ".join(tokens).strip(), confirmed


class ReincarnationHandlers:
    """飞升转生与仙缘商店处理器"""
    
    def __init__(self, db: DataBase, reinc_mgr: ReincarnationManager):
        self.db = db
        self.reinc_mgr = reinc_mgr

    @player_required
    async def handle_reincarnate(self, player: Player, event: AstrMessageEvent, args: str = ""):
        """处理飞升指令：默认只出确认页，需带「确认」才执行（支持指定传家宝）"""
        heirloom_name, confirmed = parse_ascend_args(args)
        if not confirmed:
            _ok, msg = await self.reinc_mgr.preview_reincarnate(player, heirloom_name)
            yield event.plain_result(msg)
            return
        success, msg = await self.reinc_mgr.try_reincarnate(player, heirloom_name)
        yield event.plain_result(msg)

    @player_required
    async def handle_unseal(self, player: Player, event: AstrMessageEvent):
        """处理解封传家宝指令"""
        success, msg = await self.reinc_mgr.unseal_heirloom(player)
        yield event.plain_result(msg)

    async def _rebirth_times(self, player: Player) -> int:
        """弃道重修次数：与称号判定同源，读 system_config。"""
        try:
            raw = await self.db.ext.get_system_config(f"rebirth_total_{player.user_id}")
            return int(raw or 0)
        except Exception:
            return 0

    @player_required
    async def handle_fate_shop(self, player: Player, event: AstrMessageEvent):
        """查看仙缘商店（目录来自 SHOP_ITEMS，改价只改一处）"""
        bonus_data = parse_bonus(player)
        rebirth = await self._rebirth_times(player)
        owned_line = format_permanent_summary(bonus_data)

        lines = [
            f"🌟 仙缘商店（当前仙缘点：{player.fate_points}）",
            f"完美转生 {player.reincarnation_count} 次 · 弃道重修 {rebirth} 次",
        ]
        if owned_line:
            lines.append(f"已购：{owned_line}")
        lines.append("━━━━━━━━━━━━━━━")

        for group_name, note in SHOP_GROUPS:
            ids = items_in_group(group_name)
            if not ids:
                continue
            lines.append(f"【{group_name}】{note}")
            for item_id in ids:
                item = SHOP_ITEMS[item_id]
                owned = item_owned(bonus_data, item_id)
                cap = int(item.get("cap") or 0)
                if item_id == 52:
                    # 身外化身按「尊」计，说「层」容易误会成属性叠加
                    progress = (
                        f"（已请 {owned}/{cap} 尊 · 满）" if owned >= cap
                        else f"（已请 {owned}/{cap} 尊）"
                    )
                elif cap:
                    progress = f"（{owned}/{cap} 层 · 满级）" if owned >= cap else f"（{owned}/{cap} 层）"
                else:
                    progress = f"（已购 {owned} 层）" if owned else ""
                price = shop_price(item_id, owned)
                price_text = f"{price} 点" if price else "已满"
                lines.append(
                    f"{item_id}. {item['name']} · {price_text} · {item['desc']}{progress}"
                )

        left = await self._exchange_left(player)
        lines += [
            "━━━━━━━━━━━━━━━",
            f"🌀 每轮转生自带 {FREE_ROOT_REROLLS} 次免费灵根重抽，用完后消耗【灵根洗炼】额度",
            "💰 【转世资粮】按「上一世巅峰境界」折算：层数 × "
            f"{NEWLIFE_ESCORT_TRIPS_PER_STACK:g} 趟镖银（约 0.5 天镖银），转生后直接到账",
            f"💱 灵石换点：{GOLD_PER_FATE_POINT // 10000} 万灵石 = 1 点，本轮还剩 {left} 点额度"
            "（发 `/仙缘兑换 灵石 <点数>` 兑换）",
            "💡 使用 `/仙缘兑换 <编号>` 购买；网页聊天室的「仙缘」标签页可点卡片直接买",
        ]
        yield event.plain_result("\n".join(lines))

    async def _exchange_left(self, player: Player) -> int:
        """本轮（这一世）还剩多少灵石换点额度。"""
        try:
            raw = await self.db.ext.get_system_config(fate_exchange_key(player.user_id))
            used = max(0, int(raw or 0))
        except (TypeError, ValueError):
            used = 0
        return max(0, FATE_EXCHANGE_MAX - used)

    @player_required
    async def handle_fate_exchange(
        self, player: Player, event: AstrMessageEvent, item_id: str = "0", count: str = "0"
    ):
        """`/仙缘兑换 <编号>` 买商品；`/仙缘兑换 灵石 <点数>` 用灵石换仙缘点。"""
        token = str(item_id or "").strip()
        if token in ("灵石", "gold", "灵石头"):
            async for r in self._exchange_gold_for_points(player, count):
                yield r
            return

        try:
            idx = int(token)
        except ValueError:
            yield event.plain_result(
                "❌ 请输入正确的商品编号！例如：/仙缘兑换 1\n"
                "💱 或用灵石换点：/仙缘兑换 灵石 10"
            )
            return

        if idx <= 0:
            yield event.plain_result("❌ 请输入正确的商品编号！例如：/仙缘兑换 1")
            return

        success, msg = await self.reinc_mgr.buy_fate_shop_item(player, idx)
        yield event.plain_result(msg)

    async def _exchange_gold_for_points(self, player: Player, count: str):
        """灵石 → 仙缘点：1 点 = 1000 万灵石，每世最多 100 点。"""
        left = await self._exchange_left(player)
        if left <= 0:
            yield event.plain_result(
                f"❌ 本轮已换满 {FATE_EXCHANGE_MAX} 点额度，等下一世再来。"
            )
            return

        raw = str(count or "").strip()
        if raw in ("", "0"):
            want = min(left, fate_exchange_points(int(player.gold or 0)))
        else:
            try:
                want = int(raw)
            except ValueError:
                yield event.plain_result(f"❌ 请输入要兑换的点数，例如：/仙缘兑换 灵石 10")
                return
            want = max(0, min(want, left))

        if want <= 0:
            yield event.plain_result(
                f"❌ 灵石不足：{GOLD_PER_FATE_POINT // 10000} 万灵石换 1 点，"
                f"当前持有 {int(player.gold or 0):,} 灵石。"
            )
            return

        cost = want * GOLD_PER_FATE_POINT
        if int(player.gold or 0) < cost:
            yield event.plain_result(
                f"❌ 灵石不足：换 {want} 点需要 {cost:,}（{cost // 10000} 万），"
                f"当前 {int(player.gold or 0):,}。"
            )
            return

        player.gold -= cost
        player.fate_points = int(player.fate_points or 0) + want
        await self.db.update_player(player)
        try:
            used = FATE_EXCHANGE_MAX - left + want
            await self.db.ext.set_system_config(fate_exchange_key(player.user_id), str(used))
        except Exception:
            pass

        yield event.plain_result(
            f"💱 灵石换仙缘点\n"
            f"━━━━━━━━━━━━━━━\n"
            f"消耗灵石：-{cost:,}（{cost // 10000} 万）\n"
            f"获得仙缘点：+{want}\n"
            f"当前仙缘点：{player.fate_points}\n"
            f"本轮额度：剩余 {left - want}/{FATE_EXCHANGE_MAX} 点\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 发 /仙缘商店 查看商品，或到网页聊天室的「仙缘」标签页点卡片购买"
        )
