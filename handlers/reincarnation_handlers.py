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
    fate_ritual_rate,
    format_permanent_summary,
    format_ritual_rate,
    item_owned,
    items_in_group,
    parse_bonus,
    perform_fate_ritual,
    shop_entries,
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
        ritual = fate_ritual_rate(
            player, getattr(self.reinc_mgr, "config_manager", None)
        )
        lines += [
            "━━━━━━━━━━━━━━━",
            f"🌀 每轮转生自带 {FREE_ROOT_REROLLS} 次免费灵根重抽，用完后消耗【灵根洗炼】额度",
            "💰 【转世资粮】按「上一世巅峰境界」折算：层数 × "
            f"{NEWLIFE_ESCORT_TRIPS_PER_STACK:g} 趟镖银（约 0.5 天镖银），转生后直接到账",
            f"🔮 仙缘祭祀：{GOLD_PER_FATE_POINT // 10000} 万灵石 / 次，本轮还剩 {left} 次额度"
            f" · 当前成功率 {float(ritual['rate']) * 100:.1f}%（{format_ritual_rate(ritual)}）"
            "（发 `/仙缘祭祀 <次数>` 或 `/仙缘兑换 灵石 <次数>`）",
            "💡 使用 `/仙缘兑换 <编号>` 购买；网页聊天室的「仙缘」标签页可点卡片直接买",
        ]

        # 网页端：49 件商品要 60+ 行文本，换成按分组筛选的网格
        try:
            from ..utils.image_generator import render_web_card_async
            entries = shop_entries(bonus_data)
            if entries:
                card = await render_web_card_async(
                    "info",
                    "🌟 仙缘商店",
                    f"当前仙缘点 {player.fate_points} · 完美转生 {player.reincarnation_count} 次 "
                    f"· 弃道重修 {rebirth} 次",
                    [{
                        "name": "商品",
                        "view": "grid",
                        "entries": entries,
                        "facet": {"label": "分组", "okLabel": "可兑"},
                    }],
                    "点格子兑换 · /仙缘兑换 <编号> · /仙缘祭祀 <次数> 以灵石祭天换点",
                )
                if card:
                    yield event.image_result(card)
                    return
        except Exception:
            pass
        yield event.plain_result("\n".join(lines))

    async def _exchange_left(self, player: Player) -> int:
        """本轮（这一世）还剩多少可成功获得的仙缘点额度。"""
        try:
            raw = await self.db.ext.get_system_config(fate_exchange_key(player.user_id))
            used = max(0, int(raw or 0))
        except (TypeError, ValueError):
            used = 0
        return max(0, FATE_EXCHANGE_MAX - used)

    @player_required
    async def handle_fate_exchange(
        self, player: Player, event: AstrMessageEvent, item_id: str = "0", count: str = ""
    ):
        """`/仙缘兑换 <编号>` 买商品；`/仙缘兑换 灵石 <次数>` 举行仙缘祭祀。"""
        token = str(item_id or "").strip()
        if token in ("灵石", "gold", "灵石头", "祭祀", "上供", "香火"):
            async for r in self._exchange_gold_for_points(player, event, count):
                yield r
            return

        try:
            idx = int(token)
        except ValueError:
            yield event.plain_result(
                "❌ 请输入正确的商品编号！例如：/仙缘兑换 1\n"
                "🔮 或用灵石祭祀：/仙缘祭祀 10（每次 1000 万灵石，小概率得 1 点）"
            )
            return

        if idx <= 0:
            yield event.plain_result("❌ 请输入正确的商品编号！例如：/仙缘兑换 1")
            return

        success, msg = await self.reinc_mgr.buy_fate_shop_item(player, idx)
        yield event.plain_result(msg)

    @player_required
    async def handle_fate_ritual(
        self, player: Player, event: AstrMessageEvent, count: str = ""
    ):
        """`/仙缘祭祀 <次数>`：以灵石为祭品，按日期/气运/功德小概率换仙缘点。"""
        async for r in self._exchange_gold_for_points(player, event, count):
            yield r

    async def _exchange_gold_for_points(
        self, player: Player, event: AstrMessageEvent, count: str
    ):
        """灵石祭祀 → 仙缘点：每次 1000 万灵石，独立判定，命中才 +1 点。"""
        left = await self._exchange_left(player)
        if left <= 0:
            yield event.plain_result(
                f"❌ 本轮已积满 {FATE_EXCHANGE_MAX} 点仙缘，等下一世再来。"
            )
            return

        raw = str(count or "").strip()
        if raw == "":
            # 裸发 /仙缘祭祀 只上一份祭品，避免一次误触烧掉全部余额
            want = 1
        elif raw == "0":
            want = min(left, fate_exchange_points(int(player.gold or 0)))
        else:
            try:
                want = int(raw)
            except ValueError:
                yield event.plain_result(
                    "❌ 请输入祭祀次数，例如：/仙缘祭祀 10"
                )
                return
            want = max(0, min(want, left))

        if want <= 0:
            yield event.plain_result(
                f"❌ 灵石不足：每次祭祀需 {GOLD_PER_FATE_POINT // 10000} 万灵石，"
                f"当前持有 {int(player.gold or 0):,} 灵石。"
            )
            return

        cost = want * GOLD_PER_FATE_POINT
        if int(player.gold or 0) < cost:
            yield event.plain_result(
                f"❌ 灵石不足：祭祀 {want} 次需要 {cost:,}（{cost // 10000} 万），"
                f"当前 {int(player.gold or 0):,}。"
            )
            return

        result = perform_fate_ritual(
            player, want, getattr(self.reinc_mgr, "config_manager", None)
        )
        success = int(result["success"])

        # 祭祀一旦举行，香火钱不再退还：失败只烧灵石，成功才记点并占用额度。
        player.gold -= cost
        if success:
            player.fate_points = int(player.fate_points or 0) + success
        await self.db.update_player(player)
        if success:
            try:
                used = FATE_EXCHANGE_MAX - left + success
                await self.db.ext.set_system_config(
                    fate_exchange_key(player.user_id), str(used)
                )
            except Exception:
                pass

        rate = float(result["rate"]) * 100
        if success:
            outcome = f"🌠 天道回应：成功 {success} 次，仙缘点 +{success}"
        else:
            outcome = "🌫️ 香火散尽，天道未应——此次祭祀没有换到仙缘点"
        yield event.plain_result(
            f"🔮 仙缘祭祀\n"
            f"━━━━━━━━━━━━━━━\n"
            f"成功率：{rate:.1f}%（{format_ritual_rate(result)}）\n"
            f"祭品：{want} 次 × {GOLD_PER_FATE_POINT // 10000} 万 = {cost:,} 灵石\n"
            f"{outcome}\n"
            f"当前仙缘点：{player.fate_points}\n"
            f"本轮额度：剩余 {left - success}/{FATE_EXCHANGE_MAX} 点\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 节日与初一/十五更易通神；气运、功德越高，天道回应越勤"
        )
