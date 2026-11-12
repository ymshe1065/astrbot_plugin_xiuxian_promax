# handlers/reincarnation_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.reincarnation_manager import ReincarnationManager
from ..models import Player
from ..core.reincarnation_rules import (
    DAO_GIFT_BONUS,
    DAO_GIFT_COST,
    DAO_GIFT_LABELS,
    format_dao_gift,
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
        """查看仙缘商店"""
        bonus_data = parse_bonus(player)
        rebirth = await self._rebirth_times(player)
        speed = 0.0
        try:
            speed = float(bonus_data.get("global_cultivation_multiplier", 0.0) or 0.0)
        except (TypeError, ValueError):
            speed = 0.0
        gift_line = format_dao_gift(bonus_data)
        gift_detail = " / ".join(
            f"{DAO_GIFT_LABELS[key]}+{value:,}" for key, value in DAO_GIFT_BONUS.items()
        )
        msg = (
            f"🌟 仙缘商店（当前仙缘点：{player.fate_points}）\n"
            f"完美转生 {player.reincarnation_count} 次 · 弃道重修 {rebirth} 次\n"
            f"已购：修炼加速 +{round(speed * 100)}%"
            + (f" · {gift_line}" if gift_line else "")
            + "\n"
            f"━━━━━━━━━━━━━━━\n"
            f"1. 兑换【修炼加速】：消耗 100 仙缘点，永久修炼速度 +10%（可无限叠加，跨世保留）\n"
            f"2. 兑换【初始灵石】：消耗 50 仙缘点，获得 10,000 灵石（当世消费）\n"
            f"3. 兑换【大道馈赠】：消耗 {DAO_GIFT_COST} 仙缘点，永久增加 "
            f"{gift_detail}（跨世保留）\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 使用 `/仙缘兑换 <编号>` 进行购买"
        )
        yield event.plain_result(msg)

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
