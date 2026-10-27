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

        # 构建装备显示
        equipment_lines = [
            f"=== {display_name} 的装备 ===\n",
            f"【武器】{equipped_label('weapon', player.weapon)}\n",
            f"【防具】{equipped_label('armor', player.armor)}\n",
            f"【主修心法】{player.main_technique if player.main_technique else '未装备'}\n",
        ]

        # 功法列表
        techniques_list = player.get_techniques_list()
        equipment_lines.append(f"【功法】({len(techniques_list)}/3)\n")
        if techniques_list:
            for i, tech in enumerate(techniques_list, 1):
                equipment_lines.append(f"  {i}. {tech}\n")
        else:
            equipment_lines.append("  未装备\n")

        # 总属性加成
        if equipped_items:
            equipment_lines.append("\n--- 装备属性加成 ---\n")
            total_attrs = player.get_total_attributes(equipped_items, pill_multipliers)

            # 计算加成值（总属性 - 基础属性）
            magic_damage_bonus = total_attrs["magic_damage"] - player.magic_damage
            physical_damage_bonus = total_attrs["physical_damage"] - player.physical_damage
            magic_defense_bonus = total_attrs["magic_defense"] - player.magic_defense
            physical_defense_bonus = total_attrs["physical_defense"] - player.physical_defense
            mental_power_bonus = total_attrs["mental_power"] - player.mental_power
            max_spiritual_qi_bonus = total_attrs["max_spiritual_qi"] - player.max_spiritual_qi
            exp_multiplier = total_attrs["exp_multiplier"]

            if magic_damage_bonus > 0:
                equipment_lines.append(f"⚔️ 法伤 +{magic_damage_bonus}\n")
            if physical_damage_bonus > 0:
                equipment_lines.append(f"🗡️ 物伤 +{physical_damage_bonus}\n")
            if magic_defense_bonus > 0:
                equipment_lines.append(f"🛡️ 法防 +{magic_defense_bonus}\n")
            if physical_defense_bonus > 0:
                equipment_lines.append(f"🪨 物防 +{physical_defense_bonus}\n")
            if mental_power_bonus > 0:
                equipment_lines.append(f"🧠 精神力 +{mental_power_bonus}\n")
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
                equip_rows = [
                    ("武器", equipped_label("weapon", player.weapon)),
                    ("防具", equipped_label("armor", player.armor)),
                    ("主修心法", player.main_technique or "未装备"),
                ]
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
                            f"物伤 +{physical_damage_bonus} · 法伤 +{magic_damage_bonus}",
                            f"物防 +{physical_defense_bonus} · 法防 +{magic_defense_bonus}",
                            f"精神力 +{mental_power_bonus} · 修为倍率 +{exp_multiplier:.1%}",
                        ]},
                    ],
                    footer="装备 <物品名> · 卸下 <武器/防具/心法/功法名>",
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
        equippable_types = ["weapon", "armor", "main_technique", "technique"]

        if item_type not in equippable_types:
            yield event.plain_result(f"【{item_name}】不是可装备的物品类型")
            return

        # 优先使用装备实例，同名装备按序号选择
        inst = None
        item_removed_from_storage = False
        if item_type in ("weapon", "armor"):
            inst = self.equipment_manager.instance_manager.find_for_equip(player, item_name, instance_index)
            if not inst:
                yield event.plain_result(f"❌ 储物戒中没有第 {instance_index} 件【{item_name}】可装备。")
                return
        if inst:
            item = self.equipment_manager.parse_item_from_name(
                item_name, self.config_manager.items_data, self.config_manager.weapons_data, inst
            )
            success, message = await self.equipment_manager.equip_item(player, item, int(inst["id"]))
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
                item_name, self.config_manager.items_data, self.config_manager.weapons_data
            )
            success, message = await self.equipment_manager.equip_item(player, item)

        if success:
            # 显示属性加成
            attr_display = item.get_attribute_display()
            result_msg = (
                f"✅ {message}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"属性加成：{attr_display}"
            )
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
                f"用法：{CMD_UNEQUIP_ITEM} 武器/防具/心法/功法名称"
            )
            return

        slot_or_name = slot_or_name.strip()

        # 支持直接输入具体装备名：自动映射到对应槽位
        if player.weapon and slot_or_name == player.weapon:
            slot_or_name = "武器"
        elif player.armor and slot_or_name == player.armor:
            slot_or_name = "防具"
        elif player.main_technique and slot_or_name == player.main_technique:
            slot_or_name = "心法"

        # 获取卸下前的装备名称，用于存入储物戒
        unequipped_item_name = None
        had_instance = False
        equipped_map = player.get_equipped_instances()
        if slot_or_name in ["武器", "weapon"]:
            unequipped_item_name = player.weapon
            had_instance = bool(equipped_map.get("weapon"))
        elif slot_or_name in ["防具", "armor"]:
            unequipped_item_name = player.armor
            had_instance = bool(equipped_map.get("armor"))
        elif slot_or_name in ["主修心法", "心法", "main_technique"]:
            unequipped_item_name = player.main_technique
        else:
            # 检查功法列表
            techniques_list = player.get_techniques_list()
            if slot_or_name in techniques_list:
                unequipped_item_name = slot_or_name

        # 卸下装备
        success, message = await self.equipment_manager.unequip_item(player, slot_or_name)

        if success:
            storage_msg = ""
            # 武器/防具实例由 EquipmentManager 直接移回实例仓库；心法/功法仍按名称存取
            needs_store = unequipped_item_name and (
                slot_or_name not in ("武器", "weapon", "防具", "armor") or not had_instance
            )
            if needs_store:
                store_success, store_msg = await self.storage_ring_manager.store_item(
                    player, unequipped_item_name, 1, silent=True
                )
                if store_success:
                    storage_msg = f"\n已存入储物戒"
                else:
                    storage_msg = f"\n⚠️ 存入储物戒失败：{store_msg}"

            yield event.plain_result(f"✅ {message}{storage_msg}")
        else:
            # 失败时列出当前可卸下的装备，便于玩家对照
            available = []
            if player.weapon:
                available.append(f"武器【{player.weapon}】(/卸下 武器 或 /卸下 {player.weapon})")
            if player.armor:
                available.append(f"防具【{player.armor}】(/卸下 防具)")
            if player.main_technique:
                available.append(f"心法【{player.main_technique}】(/卸下 心法)")
            for tech in player.get_techniques_list():
                available.append(f"功法【{tech}】(/卸下 {tech})")
            listing = "\n".join(available) if available else "你当前没有穿戴任何装备"
            yield event.plain_result(f"❌ {message}\n\n当前可卸下：\n{listing}")
