# handlers/rift_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..managers.rift_manager import RiftManager
from ..data.data_manager import DataBase
from .utils import set_event_flag

class RiftHandlers:
    def __init__(self, db: DataBase, rift_mgr: RiftManager):
        self.db = db
        self.rift_mgr = rift_mgr

    async def handle_rift_list(self, event: AstrMessageEvent):
        """秘境列表（带玩家境界标注）"""
        user_id = event.get_sender_id()
        success, msg = await self.rift_mgr.list_rifts(user_id)
        yield event.plain_result(msg)

    async def handle_rift_detail(self, event: AstrMessageEvent, rift_id: int):
        """秘境详情（产出与掉落表）"""
        success, msg = await self.rift_mgr.rift_detail(int(rift_id))
        yield event.plain_result(msg)

    async def handle_rift_explore(self, event: AstrMessageEvent, rift_id: int):
        """探索秘境"""
        user_id = event.get_sender_id()
        if not rift_id:
            yield event.plain_result("❌ 请输入秘境ID")
            return
        success, msg = await self.rift_mgr.enter_rift(user_id, int(rift_id))
        yield event.plain_result(msg)

    async def handle_rift_explore_by_name(self, event: AstrMessageEvent, name: str, difficulty: str = ""):
        """按秘境名探索（如 /探索秘境 万妖洞 三重）"""
        user_id = event.get_sender_id()
        rift, candidates = await self.rift_mgr.find_rift(name, difficulty)
        if rift:
            success, msg = await self.rift_mgr.enter_rift(user_id, int(rift.rift_id))
            yield event.plain_result(msg)
            return
        if not candidates:
            yield event.plain_result(
                f"❌ 没找到叫「{name}」的秘境，使用 /秘境列表 查看全部秘境"
            )
            return
        lines = [f"🌀 「{name}」有多处难度，请指定（或在指令后加难度，如 /探索秘境 {name} 二重）："]
        for r in candidates[:9]:
            lines.append(
                f"  · {r.display_name} (ID:{r.rift_id}) 需境界{r.required_level}"
            )
        yield event.plain_result("\n".join(lines))
        
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
