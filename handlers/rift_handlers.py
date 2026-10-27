# handlers/rift_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..managers.rift_manager import RiftManager
from ..data.data_manager import DataBase

class RiftHandlers:
    def __init__(self, db: DataBase, rift_mgr: RiftManager):
        self.db = db
        self.rift_mgr = rift_mgr

    async def handle_rift_list(self, event: AstrMessageEvent):
        """秘境列表"""
        success, msg = await self.rift_mgr.list_rifts()
        yield event.plain_result(msg)

    async def handle_rift_explore(self, event: AstrMessageEvent, rift_id: int):
        """探索秘境"""
        user_id = event.get_sender_id()
        if not rift_id:
            yield event.plain_result("❌ 请输入秘境ID")
            return
        success, msg = await self.rift_mgr.enter_rift(user_id, int(rift_id))
        yield event.plain_result(msg)
        
    async def handle_rift_complete(self, event: AstrMessageEvent):
        """完成探索"""
        user_id = event.get_sender_id()
        success, msg, _ = await self.rift_mgr.finish_exploration(user_id)
        yield event.plain_result(msg)
    
    async def handle_rift_exit(self, event: AstrMessageEvent):
        """退出秘境"""
        set_event_flag(event, "rift_abandon_success", False)
        user_id = event.get_sender_id()
        success, msg = await self.rift_mgr.exit_rift(user_id)
        set_event_flag(event, "rift_abandon_success", success)
        yield event.plain_result(msg)
