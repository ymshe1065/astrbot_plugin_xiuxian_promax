# handlers/impart_pk_handlers.py
"""传承PK处理器"""
import re
import time
from astrbot.api.event import AstrMessageEvent
from astrbot.api.all import At
from ..data import DataBase
from ..managers.impart_pk_manager import ImpartPkManager
from ..models import Player
from .utils import player_required

__all__ = ["ImpartPkHandlers"]

IMPART_CHALLENGE_COOLDOWN = 300


class ImpartPkHandlers:
    """传承PK处理器"""
    
    def __init__(self, db: DataBase, impart_pk_mgr: ImpartPkManager):
        self.db = db
        self.impart_pk_mgr = impart_pk_mgr
    
    @player_required
    async def handle_impart_challenge(self, player: Player, event: AstrMessageEvent, target_info: str = ""):
        """发起传承挑战"""
        # 解析目标：优先读取 At 组件，兼容纯数字 ID
        target_id = None
        if hasattr(event, "message_obj") and event.message_obj:
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    target_id = str(getattr(comp, "qq", "") or getattr(comp, "target", ""))
                    break
        target_id = target_id or self._extract_user_id(target_info)
        if not target_id:
            yield event.plain_result(
                "⚔️ 传承挑战\n"
                "━━━━━━━━━━━━━━━\n"
                "争夺对方的传承加成！\n"
                "胜利：获得传承ATK加成\n"
                "失败：损失1%修为\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 用法：/传承挑战 @某人"
            )
            return
        
        if target_id == player.user_id:
            yield event.plain_result("❌ 不能挑战自己。")
            return

        cd_key = f"impart_pk_cd_{player.user_id}"
        cd_raw = await self.db.ext.get_system_config(cd_key)
        now = int(time.time())
        if cd_raw and cd_raw.isdigit() and now < int(cd_raw):
            remain = int(cd_raw) - now
            yield event.plain_result(
                f"❌ 传承挑战调息中，请 {remain // 60} 分 {remain % 60} 秒后再来。"
            )
            return

        # 获取目标玩家
        target = await self.db.get_player_by_id(target_id)
        if not target:
            yield event.plain_result("❌ 对方还未踏入修仙之路。")
            return
        
        # 发起挑战
        wins, log, rewards = await self.impart_pk_mgr.challenge_impart(player, target)
        await self.db.ext.set_system_config(
            cd_key, str(int(time.time()) + IMPART_CHALLENGE_COOLDOWN)
        )
        
        if wins:
            result_msg = (
                f"🎉 传承挑战胜利！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"对手：{target.user_name or target_id[:8]}\n"
                f"获得ATK传承：+{rewards.get('impart_atk_gain', 0):.2%}\n"
            )
        else:
            result_msg = (
                f"💀 传承挑战失败...\n"
                f"━━━━━━━━━━━━━━━\n"
                f"对手：{target.user_name or target_id[:8]}\n"
                f"损失修为：-{rewards.get('exp_loss', 0):,}\n"
            )
        
        yield event.plain_result(result_msg)
    
    @player_required
    async def handle_impart_ranking(self, player: Player, event: AstrMessageEvent):
        """传承排行榜"""
        rankings = await self.impart_pk_mgr.get_impart_ranking(10)
        
        if not rankings:
            yield event.plain_result("📊 传承排行榜暂无数据。")
            return
        
        lines = ["🏆 传承排行榜\n━━━━━━━━━━━━━━━"]
        for i, r in enumerate(rankings, 1):
            lines.append(f"{i}. {r['user_name']} - ATK+{r['atk_per']:.1%}")
        lines.append("━━━━━━━━━━━━━━━")
        
        yield event.plain_result("\n".join(lines))
    
    def _extract_user_id(self, msg: str) -> str:
        """从消息中提取用户ID"""
        if not msg:
            return ""
        # 匹配 @xxx 或纯数字
        at_match = re.search(r'\[CQ:at,qq=(\d+)\]', msg)
        if at_match:
            return at_match.group(1)
        # 纯数字
        num_match = re.search(r'(\d{5,12})', msg)
        if num_match:
            return num_match.group(1)
        return ""
