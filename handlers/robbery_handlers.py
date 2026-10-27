# handlers/robbery_handlers.py
from astrbot.api.event import AstrMessageEvent
from astrbot.api.all import At
from ..data import DataBase
from ..managers.robbery_manager import RobberyManager
from ..models import Player
from .utils import player_required, set_event_flag

class RobberyHandlers:
    def __init__(self, db: DataBase, robbery_mgr: RobberyManager):
        self.db = db
        self.robbery_mgr = robbery_mgr
        self.last_shield_success = False

    @player_required
    async def handle_use_shield(self, player: Player, event: AstrMessageEvent):
        success, msg = await self.robbery_mgr.use_shield(player)
        set_event_flag(event, "shield_success", bool(success))
        yield event.plain_result(msg)

    @player_required
    async def handle_rob(self, player: Player, event: AstrMessageEvent, target: str = ""):
        # 解析目标（从 At 中提取）
        target_id = None
        if hasattr(event, 'message_obj') and event.message_obj:
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    target_id = str(comp.qq)
                    break
                    
        if not target_id and target.strip().lstrip("@").isdigit():
            target_id = target.strip().lstrip("@")

        if not target_id:
            yield event.plain_result("❌ 请使用 @某人 指定你要潜入的洞府对象。")
            return

        success, msg, caught = await self.robbery_mgr.rob_player(player, target_id)
        
        if caught:
            # 被挂树上，全服广播
            player_name = player.user_name or player.user_id[:6]
            broadcast_msg = (
                f"🚨 抓贼啦！🚨\n"
                f"━━━━━━━━━━━━━━━\n"
                f"修士【{player_name}】试图潜入他人洞府行窃，\n"
                f"不幸触发阵法被擒，已被挂在宗门外的大树上示众三天三夜！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"（损失了一个【迷神香】）"
            )
            
            # 这里通过 main.py 的广播接口发，如果没有，就只回复当前群
            yield event.plain_result(broadcast_msg)
        else:
            yield event.plain_result(msg)