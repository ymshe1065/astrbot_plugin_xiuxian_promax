# handlers/pill_handler.py

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
import time

from ..data import DataBase
from ..core import PillManager, ItemRules
from ..core.pill_manager import _safe_int
from ..core.active_effects import describe_effects, describe_remaining, status_title
from ..core.realm_rules import format_applicable_realm
from ..models import Player
from ..config_manager import ConfigManager
from ..pill_effect_text import describe_pill_effect, quality_caps
from ..rank_rules import RANK_NAMES
from .utils import player_required

CMD_USE_PILL = "服用丹药"
CMD_SHOW_PILLS = "丹药背包"
CMD_PILL_INFO = "丹药信息"
CMD_MY_STATUS = "我的状态"

__all__ = ["PillHandler"]


class PillHandler:
    """丹药系统处理器 - 处理丹药使用和查看"""

    def __init__(self, db: DataBase, config_manager: ConfigManager):
        self.db = db
        self.config_manager = config_manager
        self.pill_manager = PillManager(db, config_manager)

    @player_required
    async def handle_use_pill(self, player: Player, event: AstrMessageEvent, args: str = ""):
        """处理服用丹药指令：/服用丹药 <丹药名> [品质] [数量]"""
        # 检查是否提供了丹药名称
        if not args or args.strip() == "":
            yield event.plain_result(
                "请指定要服用的丹药名称！\n"
                f"💡 使用方法：{CMD_USE_PILL} <丹药名称> [品质] [数量]\n"
                f"💡 例如：{CMD_USE_PILL} 炼气丹 或 {CMD_USE_PILL} 筑基丹 良好 2"
            )
            return

        tokens = args.strip().split()
        count = 1
        if tokens and tokens[-1].isdigit():
            count = int(tokens.pop())
        quality = None
        if tokens and tokens[-1] in self.pill_manager.QUALITY_ORDER:
            quality = tokens.pop()
        pill_name = " ".join(tokens).strip()
        if "|" in pill_name:
            base_name, embedded_quality = self.pill_manager.split_pill_key(pill_name)
            pill_name = base_name
            quality = quality or embedded_quality

        if not pill_name:
            yield event.plain_result("❌ 未识别到丹药名称，请检查格式。")
            return
        if count <= 0:
            yield event.plain_result("❌ 数量必须大于 0。")
            return

        # 安全上限，避免误输入 9999 时循环过久
        count = min(count, 100)

        # 先更新临时效果（移除过期的）
        await self.pill_manager.update_temporary_effects(player)

        success, used, message = await self.pill_manager.use_pill_batch(
            player, pill_name, quality, count
        )
        if not success:
            yield event.plain_result(message if str(message).startswith(("❌", "⚠️")) else f"❌ {message}")
            return
        yield event.plain_result(message)

    @player_required
    async def handle_show_pills(self, player: Player, event: AstrMessageEvent):
        """处理查看丹药背包指令

        Args:
            player: 玩家对象
            event: 事件对象
        """
        try:
            message = await self._render_pill_bag(player)
        except Exception:
            # 以前这里异常会直接冒泡，玩家侧表现成「发指令没反应」，服主也看不到原因
            logger.exception(
                "丹药背包读取失败 user=%s", getattr(player, "user_id", "?")
            )
            yield event.plain_result(
                "❌ 丹药背包读取失败，已记录日志。\n"
                "💡 可先发送 /服用丹药 或在 /储物戒 确认丹药状态，并把上面的日志发给服主。"
            )
            return
        try:
            entries = self._pill_entries(player)
        except Exception:
            # 结构化清单只用于网页格子/图片卡片；失败不能拖垮文本背包
            logger.exception(
                "丹药背包结构化清单生成失败 user=%s", getattr(player, "user_id", "?")
            )
            entries = []
        # 网页看板要结构化卡片：物品清单 + 品质/效果字段，直接复用储物戒的格子渲染
        card_path = await self._render_pill_bag_card(event, player, entries)
        if card_path:
            yield event.image_result(card_path)
            return
        yield event.plain_result(message)

    def _pill_entries(self, player: Player) -> list:
        """丹药背包结构化清单：同名不同品质各自成条，带品阶与效果文案。"""
        inventory = player.get_pills_inventory() or {}
        grouped = {}
        for key, count in inventory.items():
            base_name, quality = self.pill_manager.split_pill_key(key)
            try:
                amount = int(count)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            grouped[(base_name, quality)] = grouped.get((base_name, quality), 0) + amount

        if not grouped:
            return []

        effect_cap, breakthrough_cap = quality_caps(
            getattr(self.config_manager, "pill_quality_data", {})
        )
        quality_order = {
            name: index for index, name in enumerate(self.pill_manager.QUALITY_ORDER)
        }
        rank_order = {name: index for index, name in enumerate(RANK_NAMES)}
        effects = {}
        entries = []
        for (base_name, quality), amount in grouped.items():
            config = self.pill_manager.get_pill_by_name(base_name) or {}
            if base_name not in effects:
                effects[base_name] = describe_pill_effect(
                    config,
                    effect_cap=effect_cap,
                    breakthrough_cap=breakthrough_cap,
                )
            effect = effects[base_name] or "⚠️ 效果未录入"
            rank = str(config.get("rank") or "未知")
            entries.append({
                "name": base_name,
                "kind": "丹药",
                "sub": self._get_subtype_display(str(config.get("subtype") or "")),
                "rank": rank,
                "quality": quality,
                "count": amount,
                "effect": effect,
            })

        entries.sort(
            key=lambda it: (
                rank_order.get(it["rank"], len(rank_order)),
                str(it["name"]),
                quality_order.get(it["quality"], len(quality_order)),
            )
        )
        return entries

    async def _render_pill_bag_card(self, event, player: Player, entries: list):
        """生成丹药背包卡片（会写 sidecar）；不可用或失败时返回 None。"""
        if not entries or not hasattr(event, "image_result"):
            return None
        try:
            from ..utils.image_generator import render_card_async, card_enabled

            if not card_enabled("CARD_BAG"):
                return None
            display_name = (
                event.get_sender_name()
                or getattr(player, "user_name", "")
                or player.user_id
                or "道友"
            )
            rows = [
                (
                    f"[{entry['rank']}] {entry['name']}（{entry['quality']}×{entry['count']}）",
                    entry["effect"],
                )
                for entry in entries
            ]
            sections = [{"name": "物品清单", "rows": rows, "entries": entries}]
            active = [
                effect
                for effect in (player.get_active_pill_effects() or [])
                if isinstance(effect, dict)
            ]
            if active:
                now = int(time.time())
                sections.append({
                    "name": "生效中",
                    "lines": [
                        f"{status_title(effect)}　{describe_remaining(effect, now)}　{describe_effects(effect)}"
                        for effect in active
                    ],
                })
            if getattr(player, "has_resurrection_pill", False):
                res_quality = getattr(player, "resurrection_pill_quality", "一般") or "一般"
                sections.append({
                    "name": "回生丹",
                    "lines": [f"🛡️ 已挂回生丹（{res_quality}），可抵消一次境界跌落"],
                })
            total = sum(int(entry["count"]) for entry in entries)
            return await render_card_async(
                "bag",
                f"{display_name} 的丹药背包",
                f"{len(entries)} 种 · 共 {total} 颗",
                sections=sections,
                footer="💡 点击丹药查看效果/服用 · /丹药配方 查看已学丹方",
                payload_extra={"kind": "pillbag"},
            )
        except Exception:
            logger.exception(
                "丹药背包卡片生成失败 user=%s", getattr(player, "user_id", "?")
            )
            return None

    async def _render_pill_bag(self, player: Player) -> str:
        """拼装丹药背包文本；读取失败由调用方兜底提示。"""
        # 先更新临时效果
        await self.pill_manager.update_temporary_effects(player)

        # 获取丹药背包显示
        inventory_display = self.pill_manager.get_pill_inventory_display(player)

        # 获取当前生效的临时效果
        active_effects = player.get_active_pill_effects()
        if not isinstance(active_effects, list):
            active_effects = []
        effects_display = []

        if active_effects:
            effects_display.append("\n--- 当前生效的临时效果 ---")
            for effect in active_effects:
                if not isinstance(effect, dict):
                    continue
                pill_name = effect.get("pill_name", "未知丹药")
                import time
                icon = "🧿" if effect.get("source") == "talisman" else "🌟"
                remaining_uses = effect.get("remaining_uses")
                if remaining_uses is not None:
                    try:
                        uses = max(0, int(remaining_uses))
                    except (TypeError, ValueError):
                        uses = 0
                    if uses > 0:
                        effects_display.append(f"{icon} {pill_name} (剩余 {uses} 次，历练·秘境)")
                    continue
                remaining_seconds = _safe_int(effect.get("expiry_time", 0)) - int(time.time())
                if remaining_seconds > 0:
                    remaining_minutes = remaining_seconds // 60
                    hours = remaining_minutes // 60
                    minutes = remaining_minutes % 60

                    if hours > 0:
                        time_str = f"{hours}小时{minutes}分钟"
                    else:
                        time_str = f"{minutes}分钟"

                    effects_display.append(f"{icon} {pill_name} (剩余: {time_str})")

        # 检查回生丹状态
        resurrection_status = ""
        if player.has_resurrection_pill:
            res_quality = getattr(player, "resurrection_pill_quality", "一般") or "一般"
            resurrection_status = f"\n🛡️ 当前拥有回生丹效果（{res_quality}，可抵消一次境界跌落）"

        # 组合显示
        full_message = inventory_display
        if effects_display:
            full_message += "\n" + "\n".join(effects_display)
        if resurrection_status:
            full_message += resurrection_status

        return full_message

    @player_required
    async def handle_my_status(self, player: Player, event: AstrMessageEvent):
        """处理 /我的状态：列出所有生效中的限时状态。"""
        try:
            message = await self._render_my_status(player)
        except Exception:
            logger.exception(
                "我的状态读取失败 user=%s", getattr(player, "user_id", "?")
            )
            yield event.plain_result(
                "❌ 状态读取失败，已记录日志。\n"
                "💡 可先发送 /丹药背包 确认状态是否正常，并把上面的日志发给服主。"
            )
            return
        yield event.plain_result(message)

    async def _render_my_status(self, player: Player) -> str:
        """拼装「状态名 + 状态效果 + 剩余时间」清单。"""
        now = int(time.time())
        await self.pill_manager.update_temporary_effects(player)

        effects = player.get_active_pill_effects()
        if not isinstance(effects, list):
            effects = []

        def _expiry(effect) -> int:
            try:
                return int(effect.get("expiry_time") or 0)
            except (TypeError, ValueError):
                return 0

        valid = []
        for effect in effects:
            if not isinstance(effect, dict):
                continue
            if effect.get("remaining_uses") is not None:
                valid.append(effect)
                continue
            expiry = _expiry(effect)
            if expiry <= 0 or expiry > now:
                valid.append(effect)

        def sort_key(effect):
            # 限时类按到期先后（快过期的在前），次数类居中，待触发的垫底
            if effect.get("remaining_uses") is not None:
                return (1, 0, str(effect.get("talisman_name") or effect.get("pill_name") or ""))
            expiry = _expiry(effect)
            if expiry <= 0:
                return (2, 0, "")
            return (0, expiry, "")

        valid.sort(key=sort_key)

        lines = ["🧿 我的状态", "━━━━━━━━━━━━━━━"]
        if not valid:
            lines.append("当前没有生效中的丹药或符箓状态。")
        else:
            lines.append(f"共 {len(valid)} 个状态生效中：")
            for effect in valid:
                icon = "📜" if effect.get("source") == "talisman" else "🌟"
                lines.append(
                    f"\n{icon} {status_title(effect)}　{describe_remaining(effect, now)}"
                )
                lines.append(f"   {describe_effects(effect)}")

        extras = []
        try:
            multipliers = self.pill_manager.calculate_pill_attribute_effects(player)
        except Exception:
            multipliers = {}
        cultivation = float(multipliers.get("cultivation_speed", 1.0) or 1.0)
        if cultivation > 1.0 + 1e-6:
            try:
                cap = float(self.config_manager.pill_quality_data.get("temporary_effect_cap", 3.0))
            except Exception:
                cap = 3.0
            extras.append(
                f"📈 修炼速度 ×{cultivation:.2f}"
                f"（+{(cultivation - 1) * 100:.0f}%，上限 ×{cap:.1f}；闭关按覆盖时长折算）"
            )
        for key, label in (
            ("physical_damage", "物伤"),
            ("magic_damage", "法伤"),
            ("physical_defense", "物防"),
            ("magic_defense", "法防"),
        ):
            value = float(multipliers.get(key, 1.0) or 1.0)
            if abs(value - 1.0) > 1e-3:
                extras.append(f"⚔️ {label} ×{value:.2f}")
        try:
            breakthrough = self.pill_manager.get_breakthrough_modifiers(player)
            bonus = float(breakthrough.get("temp_bonus") or 0)
            if bonus:
                extras.append(f"🎯 突破成功率 +{bonus * 100:.0f}%")
        except Exception:
            pass
        if player.has_resurrection_pill:
            res_quality = getattr(player, "resurrection_pill_quality", "一般") or "一般"
            extras.append(f"🛡️ 回生丹（{res_quality}）：可抵消一次境界跌落")

        if extras:
            lines.append("\n━━━━━━━━━━━━━━━")
            lines.extend(extras)

        if valid:
            lines.append("\n💡 同名符箓再次使用会续时，不会覆盖剩余时间")
        lines.append("💡 /丹药背包 看丹药存量；/符箓 看符箓存量")
        return "\n".join(lines)

    @player_required
    async def handle_pill_info(self, player: Player, event: AstrMessageEvent, pill_name: str = ""):
        """处理查看丹药信息指令

        Args:
            player: 玩家对象
            event: 事件对象
            pill_name: 丹药名称
        """
        if not pill_name or pill_name.strip() == "":
            yield event.plain_result(
                "请指定要查看的丹药名称！\n"
                f"💡 使用方法：{CMD_PILL_INFO} [丹药名称]\n"
                f"💡 例如：{CMD_PILL_INFO} 炼气丹"
            )
            return

        pill_name = pill_name.strip()

        # 获取丹药配置
        pill_data = self.pill_manager.get_pill_by_name(pill_name)
        if not pill_data:
            yield event.plain_result(f"❌ 找不到丹药【{pill_name}】的信息！")
            return

        # 构建丹药信息显示
        info_lines = [
            f"--- 丹药信息 ---",
            f"名称：{pill_data.get('name', '未知')}",
            f"品级：{pill_data.get('rank', '未知')}",
            f"类型：{self._get_subtype_display(pill_data.get('subtype', ''))}"
        ]

        # 描述
        description = pill_data.get('description', '')
        if description:
            info_lines.append(f"描述：{description}")

        # 需求境界：按品阶区间展示适用境界
        required_level = pill_data.get('required_level_index', 0)
        if required_level > 0:
            realm_text = format_applicable_realm(
                required_level,
                self.config_manager.level_data,
                self.config_manager.body_level_data,
                rank=str(pill_data.get("rank", "") or ""),
            )
            info_lines.append(f"适用境界：{realm_text}")

        # 价格
        price = pill_data.get('price', 0)
        if price > 0:
            info_lines.append(f"价格：{price} 灵石")

        # 效果描述
        subtype = str(pill_data.get('subtype', '') or '')
        effect_type = pill_data.get('effect_type', '')
        if effect_type or pill_data.get("effect") or subtype:
            info_lines.append(f"\n【效果】")
            info_lines.append(self._get_effect_description(pill_data))

        if subtype not in ("breakthrough",):
            info_lines.append("\n【品质】")
            for quality in self.pill_manager.QUALITY_ORDER:
                mult = self.pill_manager.get_quality_multiplier(quality)
                info_lines.append(f"  {quality}：效果 ×{mult:.2f}")
            if subtype == "resurrection":
                info_lines.append("  效果：抵消一次突破失败的境界跌落")

        info_lines.append("-" * 20)

        yield event.plain_result("\n".join(info_lines))

    def _get_subtype_display(self, subtype: str) -> str:
        """获取丹药子类型的显示名称"""
        subtype_map = {
            "exp": "修为丹",
            "resurrection": "回生丹",
            "cultivation_boost": "修炼加速",
            "permanent_attribute": "永久属性",
            "combat_boost": "战斗增益",
            "defensive_boost": "防御增益",
            "instant_restore": "瞬间恢复",
            "regeneration": "持续恢复",
            "debuff": "负面效果",
            "breakthrough_boost": "突破辅助",
            "breakthrough": "突破丹",
        }
        return subtype_map.get(subtype, "其他")

    def _get_effect_description(self, pill_data: dict) -> str:
        """获取丹药效果描述"""
        effect_type = pill_data.get('effect_type', '')
        subtype = pill_data.get('subtype', '')
        lines = []

        legacy_effect = pill_data.get('effect')
        if isinstance(legacy_effect, dict) and legacy_effect:
            labels = {
                'add_hp': '气血·灵气', 'add_experience': '修为', 'add_max_hp': '上限',
                'add_spiritual_power': '法伤', 'add_mental_power': '精神力',
                'add_attack': '物伤', 'add_defense': '物防', 'add_gold': '灵石',
                'add_breakthrough_bonus': '突破成功率',
            }
            for key, label in labels.items():
                value = legacy_effect.get(key)
                if value:
                    if key == 'add_breakthrough_bonus':
                        lines.append(f"  {label}：{float(value):+.1%}（1小时）")
                    else:
                        lines.append(f"  {label}：{value:+d}")
            return "\n".join(lines) if lines else "  特殊效果"

        if subtype == "exp":
            exp_gain = pill_data.get('exp_gain', 0)
            lines.append(f"  增加修为：{exp_gain}")

        elif subtype == "resurrection":
            lines.append("  抵消一次突破失败的境界跌落")

        elif effect_type == "temporary":
            duration = pill_data.get('duration_minutes', 0)
            if duration:
                lines.append(f"  持续时间：{duration}分钟")

            if 'cultivation_multiplier' in pill_data:
                mult = pill_data['cultivation_multiplier']
                lines.append(f"  修炼速度：{ItemRules.format_bonus_rate(mult, cap=None)}")

            for key, label in (
                ('physical_damage_multiplier', '物伤'),
                ('magic_damage_multiplier', '法伤'),
                ('physical_defense_multiplier', '物防'),
                ('magic_defense_multiplier', '法防'),
            ):
                if key in pill_data:
                    lines.append(f"  {label}：{ItemRules.format_bonus_rate(pill_data[key])}")

        elif effect_type == "permanent":
            lines.append("  永久效果（受30%上限限制）：")

            if 'physical_damage_gain' in pill_data:
                gain = pill_data['physical_damage_gain']
                lines.append(f"  物伤：{gain:+d}")

            if 'magic_damage_gain' in pill_data:
                gain = pill_data['magic_damage_gain']
                lines.append(f"  法伤：{gain:+d}")

            if 'physical_defense_gain' in pill_data:
                gain = pill_data['physical_defense_gain']
                lines.append(f"  物防：{gain:+d}")

            if 'magic_defense_gain' in pill_data:
                gain = pill_data['magic_defense_gain']
                lines.append(f"  法防：{gain:+d}")

            if 'mental_power_gain' in pill_data:
                gain = pill_data['mental_power_gain']
                lines.append(f"  精神力：{gain:+d}")

            if 'lifespan_gain' in pill_data:
                gain = pill_data['lifespan_gain']
                lines.append(f"  寿命：{gain:+d}")

        elif effect_type == "instant":
            if 'spiritual_qi_restore' in pill_data:
                restore = pill_data['spiritual_qi_restore']
                if restore == -1:
                    lines.append("  瞬间恢复灵气至满")
                else:
                    lines.append(f"  瞬间恢复灵气：{restore}")

        return "\n".join(lines) if lines else "  特殊效果"
