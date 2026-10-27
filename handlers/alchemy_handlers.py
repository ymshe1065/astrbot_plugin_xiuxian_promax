# handlers/alchemy_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..managers.alchemy_manager import AlchemyManager
from ..data.data_manager import DataBase
from .utils import set_event_flag
from ..models_extended import UserStatus

class AlchemyHandlers:
    def __init__(self, db: DataBase, alchemy_mgr: AlchemyManager):
        self.db = db
        self.alchemy_mgr = alchemy_mgr

    async def handle_recipes(self, event: AstrMessageEvent):
        """丹药配方"""
        user_id = event.get_sender_id()
        success, msg = await self.alchemy_mgr.get_available_recipes(user_id)
        yield event.plain_result(msg)

    async def handle_learn_recipe(self, event: AstrMessageEvent, identifier: str):
        """学习丹方"""
        user_id = event.get_sender_id()
        player = await self.db.get_player_by_id(user_id)
        if not player:
            yield event.plain_result("❌ 你还未踏入修仙之路！")
            return
        success, msg = await self.alchemy_mgr.learn_recipe(player, identifier)
        yield event.plain_result(msg)

    async def handle_craft(self, event: AstrMessageEvent, args: str):
        """炼丹"""
        user_id = event.get_sender_id()
        
        # 检查玩家是否存在
        player = await self.db.get_player_by_id(user_id)
        if not player:
            yield event.plain_result("❌ 你还未踏入修仙之路！")
            return
        
        # 检查玩家状态
        user_cd = await self.db.ext.get_user_cd(user_id)
        if user_cd and user_cd.type != UserStatus.IDLE:
            current_status = UserStatus.get_name(user_cd.type)
            yield event.plain_result(f"❌ 你当前正{current_status}，无法炼丹！")
            return
        
        tokens = (args or "").strip().split()
        if not tokens:
            yield event.plain_result("❌ 请输入丹药配方ID，例如：/炼丹 3 猛火")
            return
        pill_id = tokens[0]
        fire_choice = tokens[1] if len(tokens) > 1 else None

        # 尝试将pill_id转换为整数
        try:
            pill_id_int = int(pill_id)
        except ValueError:
            yield event.plain_result("❌ 请输入有效的丹药配方ID")
            return

        success, msg, result_data = await self.alchemy_mgr.craft_pill(user_id, pill_id_int, fire_choice)
        # 记录本次是否真正炼成（丹劫毁丹/失败不算），供任务进度判定
        set_event_flag(event, "craft_success", bool(result_data and result_data.get("success")))
        set_event_flag(event, "craft_perfect", bool(result_data and result_data.get("perfect")))
        yield event.plain_result(msg)
