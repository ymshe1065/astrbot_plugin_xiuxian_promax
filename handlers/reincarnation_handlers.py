# handlers/reincarnation_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.reincarnation_manager import ReincarnationManager
from ..models import Player
from ..core.reincarnation_rules import (
    FREE_ROOT_REROLLS,
    NEWLIFE_ESCORT_TRIPS_PER_STACK,
    SHOP_ITEMS,
    format_permanent_summary,
    item_owned,
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

        for item_id in sorted(SHOP_ITEMS):
            item = SHOP_ITEMS[item_id]
            owned = item_owned(bonus_data, item_id)
            cap = int(item.get("cap") or 0)
            progress = f"（已购 {owned}/{cap} 层）" if cap else (f"（已购 {owned} 层）" if owned else "")
            lines.append(
                f"{item_id}. {item['name']} · {item['cost']} 点 · {item['desc']}{progress}"
            )

        lines += [
            "━━━━━━━━━━━━━━━",
            f"🌀 每轮转生自带 {FREE_ROOT_REROLLS} 次免费灵根重抽，用完后消耗【灵根洗炼】额度",
            "💰 【转世资粮】按「上一世巅峰境界」折算：层数 × "
            f"{NEWLIFE_ESCORT_TRIPS_PER_STACK:g} 趟镖银，转生后直接到账",
            "💡 使用 `/仙缘兑换 <编号>` 购买",
        ]
        yield event.plain_result("\n".join(lines))

    @player_required
    async def handle_fate_exchange(self, player: Player, event: AstrMessageEvent, item_id: str = "0"):
        """处理仙缘商店购买指令"""
        try:
            idx = int(item_id)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的商品编号！例如：/仙缘兑换 1")
            return
            
        if idx <= 0:
            yield event.plain_result("❌ 请输入正确的商品编号！例如：/仙缘兑换 1")
            return
            
        success, msg = await self.reinc_mgr.buy_fate_shop_item(player, idx)
        yield event.plain_result(msg)
