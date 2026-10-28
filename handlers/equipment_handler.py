# handlers/equipment_handler.py

from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..core import EquipmentManager, PillManager, StorageRingManager, ItemRules
from ..config_manager import ConfigManager
from ..models import Player
from .utils import player_required

CMD_SHOW_EQUIPMENT = "我的装备"
CMD_EQUIP_ITEM = "装备"
CMD_UNEQUIP_ITEM = "卸下"

__all__ = ["EquipmentHandler"]

class EquipmentHandler:
    """装备系统处理器"""

    def __init__(self, db: DataBase, config_manager: ConfigManager):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_manager = StorageRingManager(db, config_manager)
        self.equipment_manager = EquipmentManager(db, config_manager, self.storage_ring_manager)
        self.pill_manager = PillManager(db, config_manager)

    async def _refresh_combat_cache(self, player: Player):
        """装备变化后刷新 player.atk 缓存，供灵兽等旧调用方使用。"""
        try:
            from ..managers.combat_manager import CombatManager
            stats = CombatManager.build_player_combat_stats(
                player, self.config_manager, db=self.db
            )
            player.atk = int(stats.atk)
            await self.db.update_player_fields(player.user_id, atk=player.atk)
        except Exception:
            pass

    @player_required
    async def handle_show_equipment(self, player: Player, event: AstrMessageEvent):
        """显示玩家当前装备"""
        display_name = event.get_sender_name()

        # 获取所有已装备物品
        equipped_items = self.equipment_manager.get_equipped_items(
            player,
            self.config_manager.items_data,
            self.config_manager.weapons_data
        )

        await self.pill_manager.update_temporary_effects(player)
        pill_multipliers = self.pill_manager.calculate_pill_attribute_effects(player)

        equipped_map = player.get_equipped_instances()
        def equipped_label(slot, name):
            if not name:
                return "未装备"
            inst = self.equipment_manager.instance_manager.get_instance(player, equipped_map.get(slot, 0))
            return f"{name}·{inst.get('quality', '一般')}" if inst else name

        gear = self.equipment_manager.get_gear_slots(player)
        gear_rows = [(ItemRules.SLOT_LABELS[slot], equipped_label(slot, name)) for slot, (name, _) in gear.items()]
        equipment_lines = [f"=== {display_name} 的装备 ===\n"]
        equipment_lines.extend(f"【{label}】{name}\n" for label, name in gear_rows)
        equipment_lines.append(f"【主修心法】{player.main_technique or '未装备'}\n")
        physical_damage_bonus = magic_damage_bonus = physical_defense_bonus = magic_defense_bonus = 0
        mental_power_bonus = max_spiritual_qi_bonus = exp_multiplier = 0

        # 功法列表
        techniques_list = player.get_techniques_list()
        equipment_lines.append(f"【功法】({len(techniques_list)}/3)\n")
        if techniques_list:
            for i, tech in enumerate(techniques_list, 1):
                equipment_lines.append(f"  {i}. {tech}\n")
        else:
            equipment_lines.append("  未装备\n")

        # 总属性加成：相对境界基础的绝对增量，并附带百分比以便理解新规则
        rates = self.equipment_manager.get_equipment_rates(player)
        life_parts = []
        if equipped_items or rates:
            equipment_lines.append("\n--- 装备属性加成（境界基础×百分比）---\n")
            total_attrs = player.get_total_attributes(equipped_items, pill_multipliers, self.equipment_manager.get_base_attributes(player))

            base = self.equipment_manager.get_base_attributes(player)
            magic_damage_bonus = total_attrs["magic_damage"] - base["magic_damage"]
            physical_damage_bonus = total_attrs["physical_damage"] - base["physical_damage"]
            magic_defense_bonus = total_attrs["magic_defense"] - base["magic_defense"]
            physical_defense_bonus = total_attrs["physical_defense"] - base["physical_defense"]
            mental_power_bonus = total_attrs["mental_power"] - base["mental_power"]
            max_spiritual_qi_bonus = total_attrs["max_spiritual_qi"] - player.max_spiritual_qi
            exp_multiplier = total_attrs["exp_multiplier"]

            from ..core.technique_bonus import TechniqueBonusManager
            life = TechniqueBonusManager.collect_life_effects(player, self.config_manager)
            life_parts = []
            if life.get("gold_bonus"):
                life_parts.append(f"灵石收益+{life['gold_bonus']:.1%}")
            if life.get("drop_bonus"):
                life_parts.append(f"掉落几率+{life['drop_bonus']:.1%}")
            if life.get("cooldown_reduction"):
                life_parts.append(f"冷却缩减{life['cooldown_reduction']:.1%}")
            if life.get("luck_bonus"):
                life_parts.append(f"气运+{int(life['luck_bonus'])}")
            if life_parts:
                equipment_lines.append(f"🌿 生活加成：{' · '.join(life_parts)}\n")

            def _line(label: str, bonus: int, rate_key: str) -> str:
                rate = rates.get(rate_key, 0)
                return f"{label} +{bonus}" + (f"（+{rate:.1%}）\n" if rate else "\n")

            if physical_damage_bonus > 0 or rates.get("physical_damage"):
                equipment_lines.append(_line("🗡️ 物伤", physical_damage_bonus, "physical_damage"))
            if magic_damage_bonus > 0 or rates.get("magic_damage"):
                equipment_lines.append(_line("⚔️ 法伤", magic_damage_bonus, "magic_damage"))
            if physical_defense_bonus > 0 or rates.get("physical_defense"):
                equipment_lines.append(_line("🪨 物防", physical_defense_bonus, "physical_defense"))
            if magic_defense_bonus > 0 or rates.get("magic_defense"):
                equipment_lines.append(_line("🛡️ 法防", magic_defense_bonus, "magic_defense"))
            if mental_power_bonus > 0 or rates.get("mental_power"):
                equipment_lines.append(_line("🧠 精神力", mental_power_bonus, "mental_power"))
            if max_spiritual_qi_bonus > 0:
                equipment_lines.append(f"✨ 灵气容量 +{max_spiritual_qi_bonus}\n")
            if exp_multiplier > 0:
                equipment_lines.append(f"📈 修为倍率 +{exp_multiplier:.1%}\n")

        equipment_lines.append("=" * 28)
        text_msg = "".join(equipment_lines)

        # ===== 装备卡片：成功只发图，失败/关闭降级为纯文本 =====
        sent = False
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            if card_enabled("CARD_EQUIP"):
                element_of = lambda n: (self.config_manager.get_item_element(n) if n else "")
                equip_rows = gear_rows + [("主修心法", player.main_technique or "未装备")]
                if element_of(player.main_technique) or element_of(player.weapon):
                    equip_rows.append(("五行属性",
                        element_of(player.main_technique) or element_of(player.weapon)))
                tech_lines = [f"{i}. {t}" for i, t in enumerate(techniques_list, 1)] or ["未装备"]
                card_path = await render_card_async(
                    "combat",
                    f"{display_name} 的装备",
                    f"功法 {len(techniques_list)}/3",
                    sections=[
                        {"name": "装备栏", "rows": equip_rows},
                        {"name": "功法", "lines": tech_lines},
                        {"name": "加成总览", "lines": [
                            f"物伤 +{physical_damage_bonus}（+{rates.get('physical_damage', 0):.1%}）· 法伤 +{magic_damage_bonus}（+{rates.get('magic_damage', 0):.1%}）",
                            f"物防 +{physical_defense_bonus}（+{rates.get('physical_defense', 0):.1%}）· 法防 +{magic_defense_bonus}（+{rates.get('magic_defense', 0):.1%}）",
                            f"精神力 +{mental_power_bonus} · 修为倍率 +{exp_multiplier:.1%}",
                            f"生活加成：{' · '.join(life_parts) if life_parts else '无'}",
                        ]},
                    ],
                    footer="装备按境界基础百分比加成，同属性加法叠加 · 装备 <物品名> [序号] [部位]",
                )
                if card_path:
                    yield event.image_result(card_path)
                    sent = True
        except Exception:
            pass
        if not sent:
            yield event.plain_result(text_msg)

    @player_required
    async def handle_equip_item(self, player: Player, event: AstrMessageEvent, item_name: str):
        """装备物品"""
        if not item_name or item_name.strip() == "":
            yield event.plain_result(f"请指定要装备的物品名称\n用法：{CMD_EQUIP_ITEM} 物品名称")
            return

        item_name = item_name.strip()
        target_slot = ""
        tokens = item_name.split()
        if len(tokens) > 1 and (tokens[-1] in ItemRules.SLOT_ALIASES
                                or tokens[-1] in ItemRules.SLOT_GROUP_ALIASES):
            target_slot = tokens.pop()
            item_name = " ".join(tokens)
        instance_index = 1
        tokens = item_name.split()
        if len(tokens) >= 2 and tokens[-1].isdigit():
            instance_index = max(1, int(tokens[-1]))
            item_name = " ".join(tokens[:-1])

        # 检查物品是否存在于配置中（items -> weapons -> techniques）
        item_config = self.config_manager.items_data.get(item_name)
        if not item_config:
            item_config = self.config_manager.weapons_data.get(item_name)
        if not item_config and hasattr(self.config_manager, "techniques_data"):
            item_config = self.config_manager.techniques_data.get(item_name)

        if not item_config:
            yield event.plain_result(f"未找到物品：{item_name}")
            return

        item_type = ItemRules.normalize_equipment_type(item_config)
        equippable_types = ItemRules.EQUIPPABLE_TYPES

        if item_type not in equippable_types:
            yield event.plain_result(f"【{item_name}】不是可装备的物品类型")
            return

        # 优先使用装备实例，同名装备按序号选择
        inst = None
        item_removed_from_storage = False
        if item_type in ItemRules.GEAR_TYPES:
            inst = self.equipment_manager.instance_manager.find_for_equip(player, item_name, instance_index)
            if not inst:
                overflow = self.equipment_manager.instance_manager.list_instances(player, "overflow", item_name)
                if overflow:
                    yield event.plain_result(
                        f"❌ 【{item_name}】目前在溢出仓库（储物戒已满，未进入储物戒）。\n"
                        f"请先清理储物戒，再发送 /领取溢出 取回后装备。"
                    )
                    return
                yield event.plain_result(f"❌ 储物戒中没有第 {instance_index} 件【{item_name}】可装备。")
                return
        if inst:
            item = self.equipment_manager.parse_item_from_name(
                item_name, self.config_manager.items_data, self.config_manager.weapons_data, inst,
                player_level=player.level_index,
            )
            success, message = await self.equipment_manager.equip_item(player, item, int(inst["id"]), target_slot)
        else:
            # 兼容旧数据：储物戒名称堆叠
            if not self.storage_ring_manager.has_item(player, item_name, 1):
                yield event.plain_result(
                    f"❌ 储物戒中没有【{item_name}】\n"
                    f"请先通过购买或获得该装备"
                )
                return

            success, retrieve_msg = await self.storage_ring_manager.retrieve_item(player, item_name, 1)
            if not success:
                yield event.plain_result(f"❌ 无法从储物戒取出装备：{retrieve_msg}")
                return
            item_removed_from_storage = True

            item = self.equipment_manager.parse_item_from_name(
                item_name, self.config_manager.items_data, self.config_manager.weapons_data,
                player_level=player.level_index,
            )
            success, message = await self.equipment_manager.equip_item(player, item, target_slot=target_slot)

        if success:
            await self._refresh_combat_cache(player)
            # 以实际装备实例为准展示属性，避免旧存档路径取到未附品质的配置值
            display_item = item
            quality = ""
            if item.item_type in ItemRules.GEAR_TYPES:
                actual_inst = inst
                if not actual_inst:
                    candidates = self.equipment_manager.instance_manager.list_instances(player, "equipped", item.name)
                    actual_inst = max(candidates, key=lambda x: int(x.get("id", 0) or 0)) if candidates else None
                if actual_inst:
                    quality = actual_inst.get("quality", "一般")
                    parsed = self.equipment_manager.parse_item_from_name(
                        item.name, self.config_manager.items_data, self.config_manager.weapons_data, actual_inst,
                        player_level=player.level_index,
                    )
                    if parsed:
                        display_item = parsed

            attr_display = display_item.get_attribute_display()
            result_msg = (
                f"✅ {message}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"属性加成：{attr_display}"
            )
            if quality:
                mult = self.equipment_manager.instance_manager.quality_multiplier(quality)
                result_msg += f"\n品质：{quality}（主属性×{mult:.2f}）"
                if quality != "一般":
                    base_item = self.equipment_manager.parse_item_from_name(
                        item.name, self.config_manager.items_data, self.config_manager.weapons_data,
                        player_level=player.level_index,
                    )
                    if base_item:
                        result_msg += f"\n基础属性：{base_item.get_attribute_display()}"
            yield event.plain_result(result_msg)
        else:
            # 仅“旧名称堆叠”路径在取出后失败时才回存；
            # 实例路径失败时装备本体仍在储物戒，不能再次 store_item，
            # 否则会新建实例并复制装备。
            if item_removed_from_storage:
                await self.storage_ring_manager.store_item(player, item_name, 1, silent=True)
            yield event.plain_result(f"❌ {message}")

    @player_required
    async def handle_unequip_item(self, player: Player, event: AstrMessageEvent, slot_or_name: str):
        """卸下装备"""
        if not slot_or_name or slot_or_name.strip() == "":
            yield event.plain_result(
                f"请指定要卸下的装备\n"
                f"用法：{CMD_UNEQUIP_ITEM} 武器/头饰/甲胄/手套/靴子/戒指1/戒指2/护符/心法/功法名称"
            )
            return

        slot_or_name = slot_or_name.strip()

        if slot_or_name == player.main_technique:
            slot_or_name = "心法"
        # unequip_item 内部已统一把心法/功法/装备实例放回储物戒；
        # 此处不能再调用 store_item，否则会新建实例造成无限复制。
        success, message = await self.equipment_manager.unequip_item(player, slot_or_name)
        if success:
            await self._refresh_combat_cache(player)
        yield event.plain_result(f"{'✅' if success else '❌'} {message}")
