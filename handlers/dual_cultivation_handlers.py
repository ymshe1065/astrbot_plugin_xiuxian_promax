# handlers/dual_cultivation_handlers.py
"""双修处理器"""
import re
from astrbot.api.event import AstrMessageEvent
from astrbot.api.all import At
from ..data import DataBase
from ..managers.dual_cultivation_manager import DualCultivationManager
from ..models import Player
from .utils import player_required
from ..utils.user_ref import resolve_user_id

__all__ = ["DualCultivationHandlers"]


class DualCultivationHandlers:
    """双修处理器"""
    
    def __init__(self, db: DataBase, dual_mgr: DualCultivationManager):
        self.db = db
        self.mgr = dual_mgr
    
    @player_required
    async def handle_dual_request(self, player: Player, event: AstrMessageEvent, target: str = ""):
        """发起双修"""
        # 尝试从 message chain 中获取 at
        target_id = None
        for component in event.message_obj.message:
            if isinstance(component, At):
                target_id = str(component.qq) # 假设是 QQ 适配器
                break
        
        if not target_id:
            # 尝试从 target 参数中提取（纯数字之外也保留道号，交给 resolve 查库）
            target_id = self._extract_user_id(target) or str(target or "").strip().lstrip("@")
        if target_id:
            # web_<账号> / 道号 统一解析成真实 user_id
            target_id = await resolve_user_id(self.db, target_id) or target_id
        
        if not target_id:
            yield event.plain_result(
                "💕 双修系统\n"
                "━━━━━━━━━━━━━━━\n"
                "与他人双修可获得对方10%的修为！\n"
                "冷却时间：1小时\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 使用 /双修 @某人"
            )
            return
        
        success, msg = await self.mgr.send_request(player, target_id)
        yield event.plain_result(msg)
    
    @player_required
    async def handle_accept(self, player: Player, event: AstrMessageEvent):
        """接受双修"""
        success, msg = await self.mgr.accept_request(player)
        yield event.plain_result(msg)
    
    @player_required
    async def handle_reject(self, player: Player, event: AstrMessageEvent):
        """拒绝双修"""
        success, msg = await self.mgr.reject_request(player.user_id)
        yield event.plain_result(msg)
    
    def _extract_user_id(self, msg: str) -> str:
        """提取用户ID"""
        if not msg:
            return ""
        at_match = re.search(r'\[CQ:at,qq=(\d+)\]', msg)
        if at_match:
            return at_match.group(1)
        num_match = re.search(r'(\d{5,12})', msg)
        if num_match:
            return num_match.group(1)
        return ""
