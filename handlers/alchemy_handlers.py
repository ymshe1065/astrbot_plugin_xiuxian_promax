# handlers/alchemy_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..core.material_selection import split_material_options
from ..managers.alchemy_manager import AlchemyManager
from ..data.data_manager import DataBase
from .utils import set_event_flag
from ..models_extended import UserStatus

# 火候别名：数字 1-3 为旧写法，中文关键字便于直接连写批量次数
FIRE_ALIASES = {
    "猛火": "1", "猛火急攻": "1",
    "文火": "2", "文火慢煨": "2",
    "守中": "3", "控火守中": "3",
}


class AlchemyHandlers:
    def __init__(self, db: DataBase, alchemy_mgr: AlchemyManager):
        self.db = db
        self.alchemy_mgr = alchemy_mgr

    @staticmethod
    def _parse_craft_options(tokens):
        """解析火候与数量：/炼丹 <ID> [火候] [数量|all]。

        返回 (fire, amount, error)。amount 为 None / 正整数 / "all"。
        """
        fire = None
        amount = None
        for index, raw in enumerate(tokens):
            token = str(raw).strip()
            if not token:
                continue
            lower = token.lower()
            if lower in ("all", "全部", "所有"):
                amount = "all"
                continue
            if lower in FIRE_ALIASES:
                fire = FIRE_ALIASES[lower]
                continue
            normalized = lower[1:] if lower.startswith(("x", "×")) else lower
            if normalized.endswith("次") or normalized.endswith("颗"):
                normalized = normalized[:-1]
            if normalized.isdigit():
                value = int(normalized)
                # 旧写法 /炼丹 3 1 中，第一位数字仍是火候
                if fire is None and amount is None and index == 0 and token.isdigit() and value in (1, 2, 3):
                    fire = str(value)
                elif value > 0:
                    amount = value
                continue
            return fire, amount, f"❌ 无法识别参数【{token}】。用法：/炼丹 <ID> [猛火/文火/守中] [次数|all]"
        return fire, amount, ""

    async def handle_recipes(self, event: AstrMessageEvent):
        """已学丹药配方（图片卡片展示，失败降级为文本）"""
        user_id = event.get_sender_id()
        success, msg, rows, card_lines, entries = await self.alchemy_mgr.get_available_recipes(user_id)
        player = await self.db.get_player_by_id(user_id)
        display_name = (player.user_name if player else "") or user_id or "道友"
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            if card_lines and card_enabled("CARD_ALCHEMY"):
                craftable = sum(1 for it in entries if it.get("state") == "ok")
                card_path = await render_card_async(
                    "craft",
                    f"{display_name} 的已学丹方",
                    f"共 {len(rows)} 个配方" + (f" · {craftable} 个可炼" if entries else ""),
                    sections=[{
                        "name": "已学丹方",
                        "lines": card_lines,
                        # 网页端走网格：品阶页签 + 点格子出详情，不再刷 /物品信息
                        "view": "grid" if entries else "",
                        "entries": entries,
                    }],
                    footer="💡 /学习丹方 <ID或名称> · /炼丹 <ID> [火候] [次数|all]",
                )
                if card_path:
                    yield event.image_result(card_path)
                    return
        except Exception:
            pass
        yield event.plain_result(msg)

    async def handle_learn_recipe(self, event: AstrMessageEvent, identifier: str):
        """学习丹方"""
        if not identifier or not str(identifier).strip():
            yield event.plain_result(
                "❌ 请指定要学习的丹方名称。\n"
                "💡 用法：/学习丹方 <丹方名>\n"
                "💡 发送 /丹阁 丹方 查看可购买丹方，/丹方 查看已学丹方。"
            )
            return
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
        
        # 炼丹属于杂务：不占活动槽位，只要还有空闲分身就能开炉
        # （忙碌拦截统一由 @player_required 按分身槽位判定）
        
        tokens = (args or "").strip().split()
        if not tokens:
            yield event.plain_result("❌ 请输入丹药配方ID，例如：/炼丹 3 猛火 10")
            return
        pill_id = tokens[0]
        try:
            options, materials = split_material_options(tokens[1:])
        except ValueError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        fire_choice, amount, error = self._parse_craft_options(options)
        if error:
            yield event.plain_result(error)
            return

        # 尝试将pill_id转换为整数
        try:
            pill_id_int = int(pill_id)
        except ValueError:
            yield event.plain_result("❌ 请输入有效的丹药配方ID")
            return

        success, msg, result_data = await self.alchemy_mgr.craft_pill(
            user_id, pill_id_int, fire_choice, amount, materials=materials or None
        )
        # 记录本次是否真正炼成（丹劫毁丹/失败不算），供任务进度判定
        success_count = 0
        perfect_count = 0
        if result_data:
            success_count = int(result_data.get("success_count", 1 if result_data.get("success") else 0) or 0)
            perfect_count = int(result_data.get("perfect_count", 1 if result_data.get("perfect") else 0) or 0)
        set_event_flag(event, "craft_success", success_count > 0)
        set_event_flag(event, "craft_perfect", perfect_count > 0)
        set_event_flag(event, "craft_success_count", success_count)
        set_event_flag(event, "craft_perfect_count", perfect_count)
        yield event.plain_result(msg)
