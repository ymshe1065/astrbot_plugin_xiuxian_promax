# handlers/misc_handler.py

from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..models import Player
from ..models_extended import UserStatus

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("xiuxian.misc_handler")

from ..utils.static_pages import build_help_text, HELP_VERSION
from .utils import player_required, with_player_lock, set_event_flag

__all__ = ["MiscHandler"]

class MiscHandler:
    """杂项指令处理器 - 提供帮助信息、一键日常与强制解卡"""

    VERSION = HELP_VERSION  # v6.39.1：历练路线时长压缩至 30-60 分钟，单次收益保持不变

    def __init__(self, db: DataBase, config: dict = None):
        self.db = db
        self.config = config or {}

    async def handle_help(self, event: AstrMessageEvent):
        """显示修仙指令大全：优先发送预生成图片，失败自动降级文本"""
        image = await self._help_image()
        if image:
            yield event.image_result(image)
            return
        yield event.plain_result(build_help_text())

    async def _help_image(self):
        try:
            from ..utils.image_generator import card_enabled
            from ..utils.static_image_generator import ensure_page

            if not card_enabled("CARD_HELP"):
                return None
            cards = self.config.get("IMAGE_CARDS", {}) or {}
            auto_regen = bool(cards.get("STATIC_AUTO_REGEN", True))
            return await ensure_page("help", allow_generate=auto_regen)
        except Exception as e:
            logger.warning(f"【修仙插件】帮助图片获取失败，降级为文本: {e}")
            return None

    @player_required
    async def handle_daily_helper(self, player: Player, event: AstrMessageEvent):
        """一键日常：智能引导玩家结算挂机收益"""
        # 这里为了保持底层逻辑的独立性和稳定性，采用清单指引模式。
        msg = (
            f"📦 【{player.user_name or '道友'}】的每日修行清单\n"
            "━━━━━━━━━━━━━━━\n"
            "由于各系统的结算时间略有不同，请依次发送以下指令获取收益：\n\n"
            "1️⃣ 发送 `/签到` (获取灵石/节日福利)\n"
            "2️⃣ 发送 `/洞天收取` (获取洞天产出的修为和灵石)\n"
            "3️⃣ 发送 `/收获` (获取灵田作物)\n"
            "4️⃣ 发送 `/领取利息` (取出银行利息)\n"
            "5️⃣ 发送 `/灵眼收取` (收取灵眼修为)\n"
            "━━━━━━━━━━━━━━━\n"
            "💡 每天花 5 秒点完，道友早日飞升！"
        )
        yield event.plain_result(msg)

    @with_player_lock
    async def handle_force_unlock(self, event: AstrMessageEvent):
        """强制解卡（不走忙碌状态拦截，保证任何时候都能自救）"""
        set_event_flag(event, "unlock_success", False)
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("道友尚未踏入仙途，请发送「我要修仙」开启你的旅程。")
            return
        user_cd = await self.db.ext.get_user_cd(player.user_id)
        
        if (not user_cd or user_cd.type == UserStatus.IDLE) and player.state == "空闲":
            yield event.plain_result("✅ 你的状态非常健康，无需解卡。")
            return
            
        # ===== 核心修复：强制清空所有可能残留的状态和进度 =====
        
        # 1. 强制重置数据库中的 CD 表，并且显式清空 extra_data
        await self.db.ext.set_user_busy(player.user_id, UserStatus.IDLE, 0, extra_data={})
        
        # 2. 强制重置玩家模型中的数据
        player.state = "空闲"
        player.cultivation_start_time = 0
        await self.db.update_player(player)
        
        set_event_flag(event, "unlock_success", True)
        yield event.plain_result(
            "🔓 状态已强制重置！\n"
            "━━━━━━━━━━━━━━━\n"
            "无论你之前是在闭关、历练还是探索秘境，现在都已恢复【空闲】。\n"
            "⚠️ 警告：解卡视为**强制放弃**当前所有进度，绝不会发放任何奖励！\n"
            "注：强制解卡不会返还已损失的门票或道具。"
        )