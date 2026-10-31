# handlers/forge_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.forge_manager import ForgeManager
from ..models import Player
from .utils import player_required, set_event_flag

class ForgeHandlers:
    def __init__(self, db: DataBase, forge_mgr: ForgeManager):
        self.db = db
        self.forge_mgr = forge_mgr
        self.last_enhance_success = False
        self.last_wash_success = False

    @player_required
    async def handle_enhance(self, player: Player, event: AstrMessageEvent):
        success, msg = await self.forge_mgr.enhance_equipment(player)
        set_event_flag(event, "enhance_success", bool(success))
        yield event.plain_result(msg)

    @player_required
    async def handle_wash(self, player: Player, event: AstrMessageEvent, slot: str = ""):
        success, msg = await self.forge_mgr.wash_equipment(player, slot)
        set_event_flag(event, "wash_success", bool(success))
        yield event.plain_result(msg)

    @player_required
    async def handle_forge(self, player: Player, event: AstrMessageEvent, args: str = ""):
        """炼器入口：当前开放妖兽材料定向洗练。"""
        from ..utils.arg_parse import normalize_arg_text

        text = normalize_arg_text(args)
        parts = text.split()
        if not parts or parts[0] not in ("洗练", "定向洗练"):
            names = "、".join(a["name"] for a in self.forge_mgr.AFFIX_POOL)
            yield event.plain_result(
                "⚒️ 炼器 · 妖兽材料定向洗练\n"
                "━━━━━━━━━━━━━━━\n"
                "用法：/炼器 洗练 <部位> <词条> <妖兽材料> [数量1-5]\n"
                "示例：/炼器 洗练 武器 暴击率 诛仙龙骨铁 2\n\n"
                f"可指定词条：{names}\n"
                "说明：消耗洗练石×1、妖兽材料×数量及灵石；\n"
                "妖兽材料品阶越高，成功率与词条数值越高。"
            )
            return
        if len(parts) < 4:
            yield event.plain_result(
                "❌ 参数不足。\n"
                "用法：/炼器 洗练 <部位> <词条> <妖兽材料> [数量1-5]"
            )
            return
        slot, affix_token, material_name = parts[1], parts[2], parts[3]
        count = 1
        if len(parts) >= 5:
            try:
                count = int(parts[4])
            except ValueError:
                yield event.plain_result("❌ 材料数量必须是 1-5 的整数。")
                return
        success, msg = await self.forge_mgr.targeted_wash(
            player, slot, affix_token, material_name, count
        )
        set_event_flag(event, "targeted_wash_success", bool(success))
        yield event.plain_result(msg)