# handlers/breakthrough_handler.py

from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..core import BreakthroughManager, PillManager
from ..config_manager import ConfigManager
from ..models import Player
from .utils import player_required

CMD_BREAKTHROUGH = "突破"
CMD_BREAKTHROUGH_INFO = "突破信息"

__all__ = ["BreakthroughHandler"]


class BreakthroughHandler:
    """突破系统处理器"""

    def __init__(self, db: DataBase, config_manager: ConfigManager, config: dict):
        self.db = db
        self.config_manager = config_manager
        self.config = config
        self.breakthrough_manager = BreakthroughManager(db, config_manager, config)
        self.pill_manager = PillManager(db, config_manager)

    @player_required
    async def handle_breakthrough_info(self, player: Player, event: AstrMessageEvent):
        """查看突破信息"""
        display_name = event.get_sender_name()

        # 根据修炼类型获取对应的境界数据
        level_data = self.config_manager.get_level_data(player.cultivation_type)

        # 检查是否已经是最高境界
        if player.level_index >= len(level_data) - 1:
            yield event.plain_result("你已经达到了最高境界，无法继续突破！")
            return

        await self.pill_manager.update_temporary_effects(player)
        modifiers = self.pill_manager.get_breakthrough_modifiers(player)

        # 获取当前和下一境界信息
        current_level_data = level_data[player.level_index]
        next_level_data = level_data[player.level_index + 1]

        current_level_name = current_level_data["level_name"]
        next_level_name = next_level_data["level_name"]
        required_exp = next_level_data.get("exp_needed", 0)
        base_success_rate = next_level_data.get("success_rate", 0.5)
        temp_bonus = modifiers["temp_bonus"]

        # 检查修为是否满足
        exp_satisfied = player.experience >= required_exp
        exp_status = "✅ 满足" if exp_satisfied else "❌ 不足"

        # 查找适用的破境丹（优先展示背包中已有的最高品质，未持有则按一般品质预览）
        inventory = player.get_pills_inventory()
        available_pills = []
        for pill_name, pill_data in self.config_manager.pills_data.items():
            if (pill_data.get("subtype") == "breakthrough" and
                pill_data.get("target_level_index") == player.level_index + 1):
                owned_qualities = []
                for pill_key, count in inventory.items():
                    base_name, quality = self.pill_manager.split_pill_key(pill_key)
                    if base_name != pill_name:
                        continue
                    try:
                        if int(count) > 0:
                            owned_qualities.append(quality)
                    except (TypeError, ValueError):
                        continue
                if owned_qualities:
                    display_quality = max(owned_qualities, key=self.pill_manager.get_quality_rank)
                else:
                    display_quality = self.pill_manager.DEFAULT_QUALITY
                quality_multiplier = self.pill_manager.get_breakthrough_multiplier(display_quality)
                final_rate, _ = self.breakthrough_manager.calculate_breakthrough_success_rate(
                    player,
                    pill_name,
                    temp_bonus,
                    display_quality if owned_qualities else None,
                    quality_multiplier,
                )
                available_pills.append({
                    "name": pill_name,
                    "rank": pill_data.get("rank", ""),
                    "quality": display_quality,
                    "owned": bool(owned_qualities),
                    "final_rate": final_rate,
                    "max_rate": float(pill_data.get("max_success_rate", 1.0))
                })

        # 构建信息显示
        info_lines = [
            f"=== {display_name} 的突破信息 ===\n",
            f"当前境界：{current_level_name}\n",
            f"下一境界：{next_level_name}\n",
            f"━━━━━━━━━━━━━━━\n",
            f"【突破条件】\n",
            f"所需修为：{required_exp}\n",
            f"当前修为：{player.experience}\n",
            f"修为状态：{exp_status}\n",
            f"━━━━━━━━━━━━━━━\n",
            f"【突破成功率】\n",
            f"基础成功率：{base_success_rate:.1%}\n",
        ]

        if temp_bonus:
            info_lines.append(f"临时丹药加成：{temp_bonus:+.1%}\n")
        karma = getattr(player, "karma", 0) or 0
        if karma != 0:
            karma_bonus = max(-0.10, min(0.10, karma * 0.01))
            if karma > 0:
                info_lines.append(f"功德庇佑：{karma_bonus:+.1%}（功德 {karma}）\n")
            else:
                info_lines.append(f"业力缠身：{karma_bonus:+.1%}（业力 {karma}，/行善 可消解）\n")
        from ..managers.luck_manager import LuckManager
        luck_bonus = LuckManager.percent_bonus(
            player, per_point=0.2, cap=10.0, config_manager=self.config_manager
        ) / 100.0
        if luck_bonus:
            info_lines.append(
                f"气运加持：{luck_bonus:+.1%}（气运 {LuckManager.get_total_luck(player, self.config_manager)}）\n"
            )
        death_reduce = 1 - modifiers["permanent_death_multiplier"]
        if death_reduce > 0:
            info_lines.append(f"突破失败境界跌落概率降低：{death_reduce:.1%}\n")

        if available_pills:
            info_lines.append(f"\n【可用破境丹】\n")
            for pill in available_pills:
                owned_note = "" if pill["owned"] else "（未持有，按一般品质预览）"
                info_lines.append(
                    f"• {pill['name']}·{pill['quality']}（{pill['rank']}）{owned_note}\n"
                    f"  使用后成功率：{pill['final_rate']:.1%}（最高{pill['max_rate']:.1%}）\n"
                )
        else:
            info_lines.append(f"\n暂无适用的破境丹\n")

        # 根据修炼类型显示不同的突破说明
        if player.cultivation_type == "体修":
            info_lines.extend([
                f"━━━━━━━━━━━━━━━\n",
                f"【突破说明】\n",
                f"• 使用命令：{CMD_BREAKTHROUGH} 或 {CMD_BREAKTHROUGH} [破境丹名称]\n",
                f"• 突破成功：境界提升，肉身更强\n",
                f"• 突破失败：损失10%修为，有概率跌落一个大境界\n",
                f"• 境界跌落：不会死亡，修为超过新境界门槛的部分会被压缩\n",
                f"=" * 28
            ])
        else:
            info_lines.extend([
                f"━━━━━━━━━━━━━━━\n",
                f"【突破说明】\n",
                f"• 使用命令：{CMD_BREAKTHROUGH} 或 {CMD_BREAKTHROUGH} [破境丹名称]\n",
                f"• 突破成功：境界提升，实力大增\n",
                f"• 突破失败：损失10%修为，有概率跌落一个大境界\n",
                f"• 境界跌落：不会死亡，修为超过新境界门槛的部分会被压缩\n",
                f"=" * 28
            ])

        yield event.plain_result("".join(info_lines))

    @player_required
    async def handle_breakthrough(self, player: Player, event: AstrMessageEvent, args: str = ""):
        """执行突破（支持 /突破 <丹药名> [品质]）"""
        display_name = event.get_sender_name()

        await self.pill_manager.update_temporary_effects(player)
        modifiers = self.pill_manager.get_breakthrough_modifiers(player)

        # 根据修炼类型获取对应的境界数据
        level_data = self.config_manager.get_level_data(player.cultivation_type)

        pills = (args or "").strip().split()
        pill_quality = None
        if pills and pills[-1] in self.pill_manager.QUALITY_ORDER:
            pill_quality = pills.pop()
        raw_pill_name = " ".join(pills).strip()
        if "|" in raw_pill_name:
            raw_pill_name, embedded_quality = self.pill_manager.split_pill_key(raw_pill_name)
            pill_quality = pill_quality or embedded_quality

        # 如果指定了破境丹，验证其有效性
        if raw_pill_name:
            pill_name = raw_pill_name
            pill_data = self.config_manager.pills_data.get(pill_name)

            if not pill_data:
                # 列出适用于当前突破的破境丹，便于玩家对照
                suitable = [
                    name for name, d in self.config_manager.pills_data.items()
                    if d.get("subtype") == "breakthrough" and d.get("target_level_index") == player.level_index + 1
                ]
                hint = "、".join(suitable) if suitable else "暂无（可直接空手突破）"
                yield event.plain_result(
                    f"❌ 未找到丹药：{pill_name}\n"
                    f"💡 适用于你本次突破的破境丹：{hint}\n"
                    f"💡 也可发送 /丹药背包 查看身上已有的丹药"
                )
                return

            if pill_data.get("subtype") != "breakthrough":
                yield event.plain_result(
                    f"❌ 【{pill_name}】不是破境丹（它是：{pill_data.get('subtype', '其他')}类丹药）。\n"
                    f"💡 请从 /丹药背包 中选择 subtype 为突破丹的丹药"
                )
                return

            # 检查是否适用于当前突破
            target_level = pill_data.get("target_level_index", -1)
            if target_level != player.level_index + 1:
                current_level = level_data[player.level_index]["level_name"]
                # 获取丹药目标境界名称
                target_level_name = f"境界{target_level}"
                if 0 <= target_level < len(level_data):
                    target_level_name = level_data[target_level]["level_name"]
                yield event.plain_result(
                    f"❌ {pill_name} 不适用于当前突破\n"
                    f"当前境界：{current_level}\n"
                    f"此丹药用于突破到：【{target_level_name}】"
                )
                return

            quality_label = f"·{pill_quality}" if pill_quality else ""
            yield event.plain_result(f"使用【{pill_name}{quality_label}】进行突破...")
        else:
            pill_name = None
            yield event.plain_result("开始尝试突破...")

        # 执行突破
        success, message, died, attempted = await self.breakthrough_manager.execute_breakthrough(
            player,
            pill_name,
            modifiers["temp_bonus"],
            modifiers["permanent_death_multiplier"],
            pill_quality,
        )

        # 只有真正发起突破且玩家仍存在时才消耗临时突破丹药
        if attempted and not died and modifiers["has_temp_effects"]:
            await self.pill_manager.consume_breakthrough_effects(player)

        yield event.plain_result(message)
