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
    async def handle_blood_forge(self, player: Player, event: AstrMessageEvent, slot: str = ""):
        """血炼：消耗精血与灵石为已装备实例赌词条。"""
        if not str(slot or "").strip():
            yield event.plain_result(
                "🩸 血炼 · 以精血淬器\n"
                "━━━━━━━━━━━━━━━\n"
                "用法：/血炼 <部位>\n"
                "示例：/血炼 武器\n"
                "说明：消耗 30% 最大气血与灵石，为装备赌一次词条：\n"
                "  · 血炼大成：附加约 1.7~2.2 倍词条\n"
                "  · 血炼小成：附加约 1.15~1.4 倍词条\n"
                "  · 血煞反噬：额外损失气血，无收益\n"
                "  · 魔化：获得强词条，但随机烧毁一条旧词条\n"
                "  · 器毁：装备所有词条被烧空\n"
                "⚠️ 气血低于 55% 时禁止血炼；炼器师灵石消耗减半。"
            )
            return
        success, msg = await self.forge_mgr.blood_forge(player, slot)
        set_event_flag(event, "blood_forge_success", bool(success))
        yield event.plain_result(msg)

    @player_required
    async def handle_forge(self, player: Player, event: AstrMessageEvent, args: str = ""):
        """炼器入口：妖兽材料定向洗练与血炼。"""
        from ..utils.arg_parse import normalize_arg_text

        text = normalize_arg_text(args)
        parts = text.split()
        if parts and parts[0] in ("血炼", "血祭"):
            slot = parts[1] if len(parts) > 1 else ""
            if not slot:
                yield event.plain_result(
                    "🩸 用法：/炼器 血炼 <部位>，例如 /炼器 血炼 武器\n"
                    "💡 也可直接发送 /血炼 <部位>。"
                )
                return
            success, msg = await self.forge_mgr.blood_forge(player, slot)
            set_event_flag(event, "blood_forge_success", bool(success))
            yield event.plain_result(msg)
            return
        if not parts or parts[0] not in ("洗练", "定向洗练"):
            names = "、".join(a["name"] for a in self.forge_mgr.AFFIX_POOL)
            yield event.plain_result(
                "⚒️ 炼器 · 妖兽材料定向洗练 / 血炼\n"
                "━━━━━━━━━━━━━━━\n"
                "用法：/炼器 洗练 <部位> <词条> <妖兽材料> [数量1-5]\n"
                "示例：/炼器 洗练 武器 暴击率 诛仙龙骨铁 2\n"
                "血炼：/炼器 血炼 <部位> 或 /血炼 <部位>\n\n"
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