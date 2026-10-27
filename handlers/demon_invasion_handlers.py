# handlers/demon_invasion_handlers.py
from astrbot.api.event import AstrMessageEvent
from astrbot.api.all import At
from ..data import DataBase
from ..managers.demon_invasion_manager import DemonInvasionManager
from ..models import Player
from .utils import player_required, set_event_flag

class DemonInvasionHandlers:
    def __init__(self, db: DataBase, demon_mgr: DemonInvasionManager, storage_ring_mgr):
        self.db = db
        self.demon_mgr = demon_mgr
        self.storage_ring_mgr = storage_ring_mgr
        self.context = None
        self.last_start_success = False
        self.last_start_msg = ""
        self.last_catch_success = False

    async def handle_start(self, event: AstrMessageEvent, hours: str = "24"):
        """管理员开启活动"""
        set_event_flag(event, "demon_start_success", False)
        self.last_start_success = False
        self.last_start_msg = ""
        success, msg = await self.demon_mgr.start_invasion(hours)
        if success:
            self.last_start_success = True
            self.last_start_msg = msg
            set_event_flag(event, "demon_start_success", True)
            set_event_flag(event, "demon_start_msg", msg)
            # 私聊通知中签的天魔
            import json
            demon_list_str = await self.db.ext.get_system_config("demon_invasion_list")
            try:
                demon_ids = json.loads(demon_list_str) if demon_list_str else []
            except (TypeError, ValueError):
                demon_ids = []
            
            if self.context:
                try:
                    from astrbot.api.event import MessageChain
                    platforms = self.context.platform_manager.get_insts()
                    platform_names = []
                    for platform in platforms:
                        try:
                            meta = platform.meta() if hasattr(platform, "meta") and callable(platform.meta) else None
                        except Exception:
                            meta = None
                        name = getattr(meta, "name", "") if meta else ""
                        if name and name not in platform_names:
                            platform_names.append(name)
                    if not platform_names:
                        platform_names = ["unknown"]

                    for uid in demon_ids:
                        for platform_name in platform_names:
                            umo = f"{platform_name}:PrivateMessage:{uid}"
                            try:
                                await self.context.send_message(umo, MessageChain().message(
                                    "🩸【天道密语】\n你已被域外天魔附体！\n潜伏在人群中，不要让其他人发现你。\n"
                                    "活动结束时，如果你还没被找出来，你将获得丰厚奖励！\n"
                                    "（注意：别人可以购买照妖镜查验你，小心行事）"
                                ))
                            except Exception:
                                pass
                except Exception:
                    pass
                    
        yield event.plain_result(msg)

    @player_required
    async def handle_catch(self, player: Player, event: AstrMessageEvent, target: str = ""):
        set_event_flag(event, "demon_catch_success", False)
        target_id = None
        if hasattr(event, 'message_obj') and event.message_obj:
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    target_id = str(comp.qq)
                    break
        if not target_id and target.strip().lstrip("@").isdigit():
            target_id = target.strip().lstrip("@")

        if not target_id:
            yield event.plain_result("❌ 请使用 @某人 指定你要查验的修士。")
            return
        if target_id == player.user_id:
            yield event.plain_result("❌ 不能查验自己。")
            return

        success, msg = await self.demon_mgr.catch_demon(player, target_id, self.storage_ring_mgr)
        self.last_catch_success = bool(success)
        set_event_flag(event, "demon_catch_success", bool(success))
        yield event.plain_result(msg)

    async def handle_status(self, event: AstrMessageEvent):
        """查看天魔活动状态"""
        is_active = await self.db.ext.get_system_config("demon_invasion_active")
        if is_active != "1":
            yield event.plain_result("🌙 当前天下太平，没有天魔入侵。")
            return
            
        end_time_str = await self.db.ext.get_system_config("demon_invasion_end_time")
        try:
            end_time = int(end_time_str) if end_time_str else 0
        except (TypeError, ValueError):
            end_time = 0
        import time
        remaining = max(0, end_time - int(time.time()))
        
        yield event.plain_result(
            "🌑 天魔入侵中！\n"
            "━━━━━━━━━━━━━━━\n"
            "正道修士们，小心身边的道友...\n"
            f"剩余时间：{remaining // 3600}小时 {(remaining % 3600) // 60}分钟\n"
            "💡 使用 /查验天魔 @某人 消耗照妖镜进行抓捕！"
        )