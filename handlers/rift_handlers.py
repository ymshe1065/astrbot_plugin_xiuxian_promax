# handlers/rift_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..managers.rift_manager import RiftManager
from ..data.data_manager import DataBase
from ..utils.list_card import to_card_parts
from ..utils.result_card import render_result_card
from .utils import set_event_flag

class RiftHandlers:
    def __init__(self, db: DataBase, rift_mgr: RiftManager):
        self.db = db
        self.rift_mgr = rift_mgr

    async def handle_rift_list(self, event: AstrMessageEvent, product: str = ""):
        """秘境列表（按可进入状态分组 + 材料价值排序 + 产物筛选）"""
        user_id = event.get_sender_id()
        data = await self.rift_mgr.build_rift_list(user_id, product)
        if not data:
            yield event.plain_result("❌ 当前没有开放的秘境！")
            return
        ok, msg = await self.rift_mgr.list_rifts(user_id, product, data=data)
        sections, payload = to_card_parts(
            data, "rift_list", top_n=self.rift_mgr.LIST_TOP_N,
            note=(
                "修为是象征量，材料才是秘境的主产出；★ 为同境界段内的材料价值档"
                "；🔒 三重需该门槛境界可装备的最高品阶全套（八格齐全）"
            ),
        )
        card = await render_result_card(
            "秘境列表", msg, background="adventure", subtitle=data["subtitle"],
            footer="/探索秘境 <ID> 进入 · /秘境详情 <ID> 看掉落",
            sections=sections, enabled_key="CARD_RIFT", payload_extra=payload,
        )
        if card and hasattr(event, "image_result"):
            yield event.image_result(card)
        else:
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
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        success, msg = await self.rift_mgr.enter_rift(user_id, int(rift_id), umo=umo)
        yield event.plain_result(msg)

    async def handle_rift_explore_by_name(self, event: AstrMessageEvent, name: str, difficulty: str = ""):
        """按秘境名探索（如 /探索秘境 万妖洞 三重）"""
        user_id = event.get_sender_id()
        rift, candidates = await self.rift_mgr.find_rift(name, difficulty)
        if rift:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            success, msg = await self.rift_mgr.enter_rift(user_id, int(rift.rift_id), umo=umo)
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
                f"  · {r.display_name} (ID:{r.rift_id}) 需{self.rift_mgr._level_label(int(r.required_level or 0))}"
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
