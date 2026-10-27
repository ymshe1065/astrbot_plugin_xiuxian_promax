# core/equipment_manager.py

import json
from typing import Optional, List, Dict, TYPE_CHECKING
from ..models import Player, Item
from ..data import DataBase
from .item_rules import ItemRules

if TYPE_CHECKING:
    from ..config_manager import ConfigManager
    from .storage_ring_manager import StorageRingManager

class EquipmentManager:
    """装备管理器 - 处理装备的穿戴、卸下和属性计算"""

    def __init__(self, db: DataBase, config_manager: "ConfigManager" = None, storage_ring_manager: "StorageRingManager" = None):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_manager = storage_ring_manager

    def get_combat_effects(self, player: Player) -> dict:
        """获取玩家所有功法/装备/炼器提供的特效加成"""
        effects = {
            "crit_rate": 0,                 # 暴击率
            "crit_damage": 1.5,             # 暴击伤害倍率
            "lifesteal": 0,                 # 吸血百分比
            "counter_damage": 0,            # 反伤百分比
            "defense_bonus": 0,             # 功法提供的额外防御
            "combo_rate": 0,                # 连击/追加伤害概率
            "physical_damage_multiplier": 1.0,
            "magic_damage_multiplier": 1.0
        }
        
        # 1. 遍历已装备的功法
        techniques_list = player.get_techniques_list()
        for tech_name in techniques_list:
            tech_config = self.config_manager.techniques_data.get(tech_name)
            if tech_config and "effects" in tech_config:
                for k, v in tech_config["effects"].items():
                    if k in effects: effects[k] += v
                        
        # 2. 主修心法特效
        if player.main_technique:
            main_config = self.config_manager.techniques_data.get(player.main_technique)
            if main_config and "effects" in main_config:
                for k, v in main_config["effects"].items():
                    if k in effects: effects[k] += v

        # 3. 读取永久炼器与洗练词条特效
        try:
            bonus_data = json.loads(player.permanent_bonus)
            affixes = bonus_data.get("affixes", [])
            for affix in affixes:
                k = affix.get("key")
                v = affix.get("value", 0)
                if k in effects: effects[k] += v
        except Exception:
            pass

        # 4. 读取武器自带特效
        if player.weapon:
            weapon_config = self.config_manager.weapons_data.get(player.weapon) or self.config_manager.items_data.get(player.weapon)
            if weapon_config and "effects" in weapon_config:
                for k, v in weapon_config["effects"].items():
                    if k in effects: effects[k] += v
                        
        # 5. 读取防具自带特效
        if player.armor:
            armor_config = self.config_manager.items_data.get(player.armor) or self.config_manager.weapons_data.get(player.armor)
            if armor_config and "effects" in armor_config:
                for k, v in armor_config["effects"].items():
                    if k in effects: effects[k] += v
        return effects

    def parse_item_from_name(self, item_name: str, items_data: dict, weapons_data: dict = None) -> Optional[Item]:
        """从物品名称解析为Item对象"""
        if not item_name: return None
        item_config = items_data.get(item_name)
        if not item_config and weapons_data: item_config = weapons_data.get(item_name)
        if not item_config: return None

        item_type = ItemRules.normalize_equipment_type(item_config)
        physical_damage = item_config.get("physical_damage", 0)
        physical_defense = item_config.get("physical_defense", 0)
        magic_damage = item_config.get("magic_damage", 0)
        magic_defense = item_config.get("magic_defense", 0)
        mental_power = item_config.get("mental_power", 0)

        if "equip_effects" in item_config:
            ee = item_config["equip_effects"]
            if "attack" in ee: physical_damage = ee["attack"]
            if "defense" in ee: physical_defense = ee["defense"]

        return Item(
            item_id=item_config.get("id", item_name), name=item_name, item_type=item_type,
            description=item_config.get("description", ""), rank=item_config.get("rank", ""),
            required_level_index=ItemRules.get_required_level(item_config), weapon_category=item_config.get("weapon_category", ""),
            magic_damage=magic_damage, physical_damage=physical_damage, magic_defense=magic_defense, physical_defense=physical_defense,
            mental_power=mental_power, exp_multiplier=item_config.get("exp_multiplier", 0.0),
            spiritual_qi=item_config.get("spiritual_qi", 0), blood_qi=item_config.get("blood_qi", 0)
        )

    def get_equipped_items(self, player: Player, items_data: dict, weapons_data: dict = None) -> List[Item]:
        """获取玩家所有已装备的物品"""
        equipped = []
        if player.weapon:
            item = self.parse_item_from_name(player.weapon, items_data, weapons_data)
            if item: equipped.append(item)
        if player.armor:
            item = self.parse_item_from_name(player.armor, items_data, weapons_data)
            if item: equipped.append(item)
        if player.main_technique:
            item = self.parse_item_from_name(player.main_technique, self.config_manager.techniques_data, items_data)
            if item: equipped.append(item)
        for tech_name in player.get_techniques_list():
            item = self.parse_item_from_name(tech_name, self.config_manager.techniques_data, items_data)
            if item: equipped.append(item)
        return equipped

    RANK_REQ_LEVEL = ItemRules.RANK_REQ_LEVEL

    def check_equipment_level_requirement(self, player: Player, item: Item) -> tuple[bool, str]:
        """检查玩家是否满足装备的境界要求。"""
        req_level = item.required_level_index
        if req_level <= 0:
            req_level = ItemRules.RANK_REQ_LEVEL.get(item.rank, 0)

        if player.level_index < req_level:
            required_level_name = self._format_required_level(req_level)
            return False, f"境界不足！装备【{item.name}】（{item.rank}）需要达到【{required_level_name}】以上"
        return True, ""

    def _format_required_level(self, level_index: int) -> str:
        """格式化需求境界名称（同时显示灵修/体修境界）"""
        if not self.config_manager: return f"境界{level_index}"
        names = []
        level_data = getattr(self.config_manager, "level_data", [])
        body_data = getattr(self.config_manager, "body_level_data", [])
        if 0 <= level_index < len(level_data):
            name = level_data[level_index].get("level_name", "")
            if name: names.append(name)
        if 0 <= level_index < len(body_data):
            name = body_data[level_index].get("level_name", "")
            if name and name not in names: names.append(name)
        if not names:
            return f"境界{level_index}"
        return " / ".join(names)

    async def equip_item(self, player: Player, item: Item) -> tuple[bool, str]:
        """装备物品"""
        can_equip, error_msg = self.check_equipment_level_requirement(player, item)
        if not can_equip:
            return False, error_msg

        if item.item_type == "weapon":
            old_item = player.weapon
            if old_item:
                store_ok, store_fail_msg = await self._try_store_old_equipment(player, old_item)
                if not store_ok:
                    return False, f"储物戒空间不足，无法回收旧装备【{old_item}】，已取消换装。请先清理储物戒。\n{store_fail_msg}"
            player.weapon = item.name
            await self.db.update_player(player)
            if old_item:
                return True, f"已将【{old_item}】替换为【{item.name}】（{item.rank}）\n旧装备【{old_item}】已存入储物戒"
            return True, f"已装备武器【{item.name}】（{item.rank}）"

        elif item.item_type == "armor":
            old_item = player.armor
            if old_item:
                store_ok, store_fail_msg = await self._try_store_old_equipment(player, old_item)
                if not store_ok:
                    return False, f"储物戒空间不足，无法回收旧装备【{old_item}】，已取消换装。请先清理储物戒。\n{store_fail_msg}"
            player.armor = item.name
            await self.db.update_player(player)
            if old_item:
                return True, f"已将【{old_item}】替换为【{item.name}】（{item.rank}）\n旧装备【{old_item}】已存入储物戒"
            return True, f"已装备防具【{item.name}】（{item.rank}）"

        elif item.item_type == "main_technique":
            old_item = player.main_technique
            if old_item:
                store_ok, store_fail_msg = await self._try_store_old_equipment(player, old_item)
                if not store_ok:
                    return False, f"储物戒空间不足，无法回收旧心法【{old_item}】，已取消换装。请先清理储物戒。\n{store_fail_msg}"
            player.main_technique = item.name
            await self.db.update_player(player)
            if old_item:
                return True, f"已将主修心法【{old_item}】替换为【{item.name}】（{item.rank}）\n旧心法【{old_item}】已存入储物戒"
            return True, f"已装备主修心法【{item.name}】（{item.rank}）"

        elif item.item_type == "technique":
            techniques_list = player.get_techniques_list()
            if item.name in techniques_list:
                return False, f"功法【{item.name}】已装备"
            if len(techniques_list) >= 3:
                return False, f"功法栏已满（最多3个），请先卸下其他功法"
            techniques_list.append(item.name)
            player.set_techniques_list(techniques_list)
            await self.db.update_player(player)
            return True, f"已装备功法【{item.name}】（{item.rank}）（{len(techniques_list)}/3）"
        else:
            return False, f"未知的装备类型：{item.item_type}"

    async def unequip_item(self, player: Player, slot_or_name: str) -> tuple[bool, str]:
        """卸下装备"""
        if slot_or_name in ["武器", "weapon"]:
            if not player.weapon: return False, "未装备武器"
            item_name = player.weapon
            player.weapon = ""
            await self.db.update_player(player)
            return True, f"已卸下武器【{item_name}】"
        elif slot_or_name in ["防具", "armor"]:
            if not player.armor: return False, "未装备防具"
            item_name = player.armor
            player.armor = ""
            await self.db.update_player(player)
            return True, f"已卸下防具【{item_name}】"
        elif slot_or_name in ["主修心法", "心法", "main_technique"]:
            if not player.main_technique: return False, "未装备主修心法"
            item_name = player.main_technique
            player.main_technique = ""
            await self.db.update_player(player)
            return True, f"已卸下主修心法【{item_name}】"

        techniques_list = player.get_techniques_list()
        if slot_or_name in techniques_list:
            techniques_list.remove(slot_or_name)
            player.set_techniques_list(techniques_list)
            await self.db.update_player(player)
            return True, f"已卸下功法【{slot_or_name}】"
        return False, f"未找到装备：{slot_or_name}"

    async def _try_store_old_equipment(self, player: Player, item_name: str) -> tuple:
        """尝试将旧装备存入储物戒，返回 (是否成功, 失败消息)"""
        if not self.storage_ring_manager:
            return True, ""
        success, msg = await self.storage_ring_manager.store_item(player, item_name, 1, silent=True)
        if success:
            return True, ""
        return False, msg