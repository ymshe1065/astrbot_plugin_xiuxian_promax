# handlers/sect_handlers.py
from astrbot.api.event import AstrMessageEvent
from astrbot.api.all import At
from ..managers.sect_manager import SectManager
from ..data.data_manager import DataBase
from ..models_extended import UserStatus
from .utils import set_event_flag

class SectHandlers:
    def __init__(self, db: DataBase, sect_mgr: SectManager):
        self.db = db
        self.sect_mgr = sect_mgr

    async def handle_create_sect(self, event: AstrMessageEvent, name: str):
        """创建宗门"""
        user_id = event.get_sender_id()
        user_cd = await self.db.ext.get_user_cd(user_id)
        if user_cd and user_cd.type != UserStatus.IDLE:
            current_status = UserStatus.get_name(user_cd.type)
            yield event.plain_result(f"❌ 你当前正{current_status}，无法进行此操作！")
            return
        success, msg = await self.sect_mgr.create_sect(user_id, name)
        yield event.plain_result(msg)

    async def handle_join_sect(self, event: AstrMessageEvent, name: str):
        """加入宗门"""
        user_id = event.get_sender_id()
        user_cd = await self.db.ext.get_user_cd(user_id)
        if user_cd and user_cd.type != UserStatus.IDLE:
            current_status = UserStatus.get_name(user_cd.type)
            yield event.plain_result(f"❌ 你当前正{current_status}，无法进行此操作！")
            return
        success, msg = await self.sect_mgr.join_sect(user_id, name)
        yield event.plain_result(msg)
    
    async def handle_leave_sect(self, event: AstrMessageEvent):
        """退出宗门"""
        user_id = event.get_sender_id()
        user_cd = await self.db.ext.get_user_cd(user_id)
        if user_cd and user_cd.type != UserStatus.IDLE:
            current_status = UserStatus.get_name(user_cd.type)
            yield event.plain_result(f"❌ 你当前正{current_status}，无法进行此操作！")
            return
        success, msg = await self.sect_mgr.leave_sect(user_id)
        yield event.plain_result(msg)

    async def handle_disband_sect(self, event: AstrMessageEvent, confirm_text: str = ""):
        """解散宗门"""
        success, msg = await self.sect_mgr.disband_sect(
            event.get_sender_id(), confirm_text
        )
        yield event.plain_result(msg)

    async def handle_my_sect(self, event: AstrMessageEvent):
        """我的宗门"""
        user_id = event.get_sender_id()
        success, msg, _ = await self.sect_mgr.get_sect_info(user_id)
        yield event.plain_result(msg)
    
    async def handle_sect_list(self, event: AstrMessageEvent):
        """宗门列表"""
        success, msg = await self.sect_mgr.list_all_sects()
        yield event.plain_result(msg)
    
    async def handle_donate(self, event: AstrMessageEvent, amount: int):
        """宗门捐献"""
        user_id = event.get_sender_id()
        user_cd = await self.db.ext.get_user_cd(user_id)
        if user_cd and user_cd.type != UserStatus.IDLE:
            current_status = UserStatus.get_name(user_cd.type)
            yield event.plain_result(f"❌ 你当前正{current_status}，无法进行此操作！")
            return
        success, msg = await self.sect_mgr.donate_to_sect(user_id, amount)
        yield event.plain_result(msg)
        
    async def handle_kick_member(self, event: AstrMessageEvent, target: str):
        """踢出宗门成员"""
        user_id = event.get_sender_id()
        target_id = None
        for component in event.message_obj.message:
            if isinstance(component, At):
                target_id = str(component.qq)
                break
        
        if not target_id:
            if target.isdigit(): target_id = target
        
        if not target_id:
            yield event.plain_result("❌ 请指定要踢出的成员（At或输入ID）")
            return

        success, msg = await self.sect_mgr.kick_member(user_id, target_id)
        yield event.plain_result(msg)

    async def handle_transfer(self, event: AstrMessageEvent, target: str):
        """宗主传位"""
        user_id = event.get_sender_id()
        target_id = None
        for component in event.message_obj.message:
            if isinstance(component, At):
                target_id = str(component.qq)
                break
        
        if not target_id and target.isdigit():
             target_id = target
             
        if not target_id:
            yield event.plain_result("❌ 请指定传位目标（At或输入ID）")
            return

        success, msg = await self.sect_mgr.transfer_ownership(user_id, target_id)
        yield event.plain_result(msg)

    async def handle_position_change(self, event: AstrMessageEvent, target: str, position: int):
        """职位变更"""
        user_id = event.get_sender_id()
        target_id = None
        for component in event.message_obj.message:
            if isinstance(component, At):
                target_id = str(component.qq)
                break
        
        if not target_id and target.isdigit():
             target_id = target
             
        if not target_id:
            yield event.plain_result("❌ 请指定目标（At或输入ID）")
            return

        success, msg = await self.sect_mgr.change_position(user_id, target_id, position)
        yield event.plain_result(msg)

    async def handle_sect_task(self, event: AstrMessageEvent):
        """执行宗门任务"""
        set_event_flag(event, "sect_task_started", False)
        user_id = event.get_sender_id()
        success, msg = await self.sect_mgr.perform_sect_task(user_id)
        set_event_flag(event, "sect_task_started", success)
        yield event.plain_result(msg)

    async def handle_finish_sect_task(self, event: AstrMessageEvent):
        """完成宗门任务（解决卡住的情况）"""
        set_event_flag(event, "sect_finish_success", False)
        user_id = event.get_sender_id()
        success, msg = await self.sect_mgr.finish_sect_task(user_id)
        set_event_flag(event, "sect_finish_success", success)
        yield event.plain_result(msg)

    async def handle_abandon_sect_task(self, event: AstrMessageEvent):
        """放弃宗门任务"""
        set_event_flag(event, "sect_abandon_success", False)
        user_id = event.get_sender_id()
        success, msg = await self.sect_mgr.abandon_sect_task(user_id)
        set_event_flag(event, "sect_abandon_success", success)
        yield event.plain_result(msg)

    async def handle_create_order(self, event: AstrMessageEvent, args: str = ""):
        """下达宗门命令：/宗门命令 @人 捐献 1000 24"""
        user_id = event.get_sender_id()
        target_id = None
        for component in event.message_obj.message:
            if isinstance(component, At):
                target_id = str(component.qq)
                break
        parts = [p for p in (args or "").strip().split() if not p.startswith("@")]
        if target_id:
            if len(parts) < 3:
                yield event.plain_result("❌ 用法：/宗门命令 @人 捐献 1000 24")
                return
            order_type, value_str, hours_str = parts[0], parts[1], parts[2]
        else:
            if len(parts) < 4:
                yield event.plain_result("❌ 用法：/宗门命令 @人 捐献 1000 24")
                return
            target_id, order_type, value_str, hours_str = parts[0], parts[1], parts[2], parts[3]
        try:
            target_value = int(value_str)
            hours = int(hours_str)
        except ValueError:
            yield event.plain_result("❌ 数量和时间必须是整数。")
            return
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        success, msg = await self.sect_mgr.create_order(
            user_id, target_id, order_type, target_value, hours, origin
        )
        yield event.plain_result(msg)

    async def handle_list_orders(self, event: AstrMessageEvent):
        success, msg = await self.sect_mgr.list_orders(event.get_sender_id())
        yield event.plain_result(msg)

    async def handle_cancel_order(self, event: AstrMessageEvent, order_id: str = ""):
        try:
            oid = int((order_id or "").strip().lstrip("#"))
        except ValueError:
            yield event.plain_result("❌ 请输入命令编号，例如：/宗门命令撤销 3")
            return
        success, msg = await self.sect_mgr.cancel_order(event.get_sender_id(), oid)
        yield event.plain_result(msg)

    async def handle_start_tournament(self, event: AstrMessageEvent):
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        success, msg = await self.sect_mgr.start_tournament(
            event.get_sender_id(), origin
        )
        yield event.plain_result(msg)

    async def handle_register_tournament(self, event: AstrMessageEvent):
        success, msg = await self.sect_mgr.register_tournament(event.get_sender_id())
        yield event.plain_result(msg)

    async def handle_tournament_status(self, event: AstrMessageEvent):
        success, msg = await self.sect_mgr.get_tournament_status(event.get_sender_id())
        yield event.plain_result(msg)

    async def handle_declare_war(self, event: AstrMessageEvent, target_name: str = ""):
        if not target_name.strip():
            yield event.plain_result("❌ 用法：/宗门宣战 <宗门名>")
            return
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        success, msg = await self.sect_mgr.declare_war(
            event.get_sender_id(), target_name.strip(), origin
        )
        yield event.plain_result(msg)

    async def handle_war_status(self, event: AstrMessageEvent):
        success, msg = await self.sect_mgr.get_war_status(event.get_sender_id())
        yield event.plain_result(msg)

    async def handle_war_report(self, event: AstrMessageEvent, war_id: str = ""):
        success, msg = await self.sect_mgr.get_war_report(
            event.get_sender_id(), war_id
        )
        yield event.plain_result(msg)