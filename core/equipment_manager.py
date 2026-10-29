# core/equipment_manager.py

import json
from typing import Optional, List, Dict, TYPE_CHECKING
from ..models import Player, Item
from ..data import DataBase
from .item_rules import ItemRules
from .item_instance_manager import ItemInstanceManager
from .equipment_balance import (
    equipment_rates, effective_rates, effective_effects, realm_attributes,
    RATE_CAPS, GEAR_EFFECT_CAPS, QUALITY, ATTRIBUTE_LABELS,
)
from ..rank_rules import normalize_rank

if TYPE_CHECKING:
    from ..config_manager import ConfigManager
    from .storage_ring_manager import StorageRingManager

class EquipmentManager:
    """装备管理器 - 处理装备的穿戴、卸下和属性计算"""

    def __init__(self, db: DataBase, config_manager: "ConfigManager" = None, storage_ring_manager: "StorageRingManager" = None):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_manager = storage_ring_manager
        self.instance_manager = ItemInstanceManager(db, config_manager)

    def get_combat_effects(self, player: Player) -> dict:
        """统一使用战斗入口的特效汇总，避免面板与实战分叉。"""
        from ..managers.combat_manager import CombatManager
        return CombatManager.collect_combat_effects(player, self.config_manager)

    def get_gear_effects(self, player: Player) -> dict:
        effects = {}
        for name, inst in self.get_gear_slots(player).values():
            cfg = (getattr(self.config_manager, "items_data", {}) or {}).get(name) or (getattr(self.config_manager, "weapons_data", {}) or {}).get(name, {})
            quality = (inst or {}).get("quality", "一般")
            sources = list(effective_effects(cfg, quality).items())
            sources += [(a.get("key"), a.get("value", 0)) for a in (inst or {}).get("affixes", [])]
            for key, value in sources:
                if key in GEAR_EFFECT_CAPS and key not in ("physical_damage_multiplier", "magic_damage_multiplier"):
                    effects[key] = effects.get(key, 0) + max(0, value)
        return {key: min(value, GEAR_EFFECT_CAPS[key]) for key, value in effects.items()}

    def get_base_attributes(self, player: Player) -> dict:
        return realm_attributes(player, self.config_manager)

    def get_equipment_rates(self, player: Player) -> dict:
        rates = {}
        for name, inst in self.get_gear_slots(player).values():
            cfg = (getattr(self.config_manager, "items_data", {}) or {}).get(name) or (getattr(self.config_manager, "weapons_data", {}) or {}).get(name, {})
            if not cfg:
                continue
            for key, value in effective_rates(cfg, player.level_index, (inst or {}).get("quality", "一般")).items():
                rates[key] = rates.get(key, 0) + value
            # 装备增伤词条并入主属性加法池，不能再乘一次最终攻击。
            sources = list(cfg.get("effects", {}).items())
            sources += [(a.get("key"), a.get("value", 0)) for a in (inst or {}).get("affixes", [])]
            for key, value in sources:
                attr = {"physical_damage_multiplier": "physical_damage", "magic_damage_multiplier": "magic_damage"}.get(key)
                if attr:
                    rates[attr] = rates.get(attr, 0) + min(.10, max(0, value))
        return {key: min(value, RATE_CAPS[key]) for key, value in rates.items()}

    # ===== 单件装备加成展示 =====
    EFFECT_LABELS = {
        "crit_rate": "暴击率", "crit_damage": "暴击伤害", "lifesteal": "吸血",
        "counter_damage": "反伤", "combo_rate": "连击率", "dodge": "闪避",
        "defense_bonus": "额外防御", "physical_damage_multiplier": "物伤倍率",
        "magic_damage_multiplier": "法伤倍率", "status_resist": "状态抗性",
        "extra_attack_chance": "快击概率", "extra_attack_ratio": "快击倍率",
        "extra_attack_count": "快击段数", "extra_attack_second_ratio": "快击二段倍率",
        "extra_attack_can_crit": "快击可暴击", "extra_attack_can_proc": "快击触发特效",
        "armor_penetration": "无视防御", "sunder_chance": "破防概率",
        "sunder_pct": "破防减防", "sunder_turns": "破防回合",
        "crit_chain_chance": "暴击连锁概率", "crit_chain_ratio": "暴击连锁倍率",
        "crit_chain_can_crit": "暴击连锁可暴击", "crit_defense_ignore": "暴击无视防御",
        "combo_extra_hit_ratio": "连击二段倍率", "combo_can_crit": "连击可暴击",
        "combo_lifesteal_pct": "连击吸血",
        "stun_chance": "麻痹概率", "stun_turns": "麻痹回合",
        "stun_extend_chance": "麻痹延长概率", "stun_resist_pierce": "控制抗性穿透",
        "burn_chance": "灼烧概率", "burn_turns": "灼烧回合",
        "burn_damage_pct": "灼烧伤害", "burn_heal_reduction": "灼烧降疗",
        "burn_detonate": "灼烧爆燃",
        "frost_chance": "寒霜概率", "frost_atk_reduction": "寒霜减攻",
        "frost_turns": "寒霜回合", "frost_dodge_reduction": "寒霜降闪避",
        "freeze_chance": "冻结概率",
        "curse_chance": "诅咒概率", "curse_defense_pct": "诅咒减防",
        "curse_turns": "诅咒回合", "curse_damage_reduction": "诅咒减伤",
    }
    EFFECT_TURN_KEYS = {
        "extra_attack_count", "sunder_turns", "stun_turns", "burn_turns",
        "frost_turns", "curse_turns",
    }
    EFFECT_BOOL_KEYS = {
        "extra_attack_can_crit", "extra_attack_can_proc", "burn_detonate",
        "crit_chain_can_crit", "combo_can_crit",
    }
    TECHNIQUE_ATTR_LABELS = {
        "spiritual_qi": "灵气", "magic_damage": "法伤", "physical_damage": "物伤",
        "magic_defense": "法防", "physical_defense": "物防", "mental_power": "精神力",
    }

    def _item_config(self, name: str) -> dict:
        if not name:
            return {}
        items = getattr(self.config_manager, "items_data", {}) or {}
        weapons = getattr(self.config_manager, "weapons_data", {}) or {}
        techniques = getattr(self.config_manager, "techniques_data", {}) or {}
        return items.get(name) or weapons.get(name) or techniques.get(name) or {}

    def item_bonus_values(self, player: Player, name: str, instance: dict = None) -> dict:
        """返回单件装备在玩家当前境界下的实际加成（百分比存小数）。

        主属性已包含品质倍率与跨境界效率衰减；特殊效果按装备来源上限封顶。
        """
        cfg = self._item_config(name)
        if not cfg:
            return {}
        item_type = ItemRules.normalize_equipment_type(cfg)
        quality = (instance or {}).get("quality", "一般")
        values: dict = {}

        if item_type in ItemRules.GEAR_TYPES:
            for key, value in effective_rates(cfg, player.level_index, quality).items():
                if value:
                    values[key] = float(value)
            effects: dict = {}
            for key, value in effective_effects(cfg, quality).items():
                try:
                    effects[key] = effects.get(key, 0.0) + float(value)
                except (TypeError, ValueError):
                    continue
            for affix in (instance or {}).get("affixes") or []:
                key = affix.get("key")
                if not key:
                    continue
                try:
                    effects[key] = effects.get(key, 0.0) + float(affix.get("value", 0) or 0)
                except (TypeError, ValueError):
                    continue
            for key, value in effects.items():
                cap = GEAR_EFFECT_CAPS.get(key)
                if cap is not None:
                    value = min(value, cap)
                if value:
                    values[key] = value
        else:
            try:
                exp_mult = float(cfg.get("exp_multiplier", 0) or 0)
            except (TypeError, ValueError):
                exp_mult = 0.0
            if exp_mult > 0:
                values["exp_multiplier"] = exp_mult
            for key in self.TECHNIQUE_ATTR_LABELS:
                try:
                    value = float(cfg.get(key, 0) or 0)
                except (TypeError, ValueError):
                    value = 0.0
                if value:
                    values[key] = value
            for key, value in (cfg.get("effects") or {}).items():
                try:
                    values[key] = float(value)
                except (TypeError, ValueError):
                    continue

        for key, value in (cfg.get("life_effects") or {}).items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number:
                values[key] = number
        return values

    @staticmethod
    def format_bonus_value(key: str, value: float, absolute: bool = False) -> str:
        """统一加成数值格式：概率类用百分点，倍率用百分比。"""
        if absolute:
            return f"{value:+.0f}"
        if key in EquipmentManager.EFFECT_BOOL_KEYS:
            return "已解锁" if value else "未解锁"
        if key in EquipmentManager.EFFECT_TURN_KEYS:
            return f"{int(value)}回合"
        if key in ("crit_rate", "lifesteal", "counter_damage", "combo_rate", "dodge", "status_resist",
                   "extra_attack_chance", "extra_attack_ratio", "extra_attack_second_ratio",
                   "armor_penetration", "sunder_chance", "sunder_pct", "crit_chain_chance",
                   "crit_chain_ratio", "crit_defense_ignore", "combo_extra_hit_ratio",
                   "combo_lifesteal_pct", "stun_chance", "stun_extend_chance", "stun_resist_pierce",
                   "burn_chance", "burn_damage_pct", "burn_heal_reduction", "frost_chance",
                   "frost_atk_reduction", "frost_dodge_reduction", "freeze_chance", "curse_chance",
                   "curse_defense_pct", "curse_damage_reduction"):
            return f"{value:+g}%"
        if key in ("luck_bonus", "defense_bonus"):
            return f"{value:+.0f}"
        if key.startswith("add_"):
            return f"{value:+.0f}"
        if key in ATTRIBUTE_LABELS or ItemRules.is_rate_key(key):
            return f"{value:+.1%}"
        return f"{value:+g}"

    def bonus_label(self, key: str) -> str:
        from .technique_bonus import LIFE_EFFECT_LABELS
        return (ATTRIBUTE_LABELS.get(key) or self.EFFECT_LABELS.get(key)
                or LIFE_EFFECT_LABELS.get(key) or self.TECHNIQUE_ATTR_LABELS.get(key) or key)

    def describe_item_bonus(self, player: Player, name: str, instance: dict = None) -> str:
        """单件装备/功法的一行加成描述。"""
        cfg = self._item_config(name)
        if not cfg:
            return "无额外加成"
        values = self.item_bonus_values(player, name, instance)
        if not values:
            return "无额外加成"
        is_gear = ItemRules.normalize_equipment_type(cfg) in ItemRules.GEAR_TYPES
        return " · ".join(
            f"{self.bonus_label(key)}"
            f"{self.format_bonus_value(key, value, absolute=(not is_gear and key in self.TECHNIQUE_ATTR_LABELS))}"
            for key, value in values.items()
        )

    def gear_bonus_score(self, player: Player, name: str, instance: dict = None) -> float:
        """用于判定两枚戒指中哪一枚更弱（只看主属性）。"""
        values = self.item_bonus_values(player, name, instance)
        return sum(value for key, value in values.items() if key in ATTRIBUTE_LABELS)

    def parse_item_from_name(self, item_name: str, items_data: dict, weapons_data: dict = None, instance: dict = None, player_level: int = None) -> Optional[Item]:
        """从物品名称解析为Item对象"""
        if not item_name: return None
        item_config = items_data.get(item_name)
        if not item_config and weapons_data: item_config = weapons_data.get(item_name)
        if not item_config and self.config_manager is not None:
            # 功法/心法独立存放在 techniques.json，不能只查 items/weapons。
            item_config = (getattr(self.config_manager, "techniques_data", {}) or {}).get(item_name)
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

        quality_mult = 1.0
        if instance:
            quality_mult = self.instance_manager.quality_multiplier(instance.get("quality", "一般"))
            magic_damage = int(magic_damage * quality_mult)
            physical_damage = int(physical_damage * quality_mult)
            magic_defense = int(magic_defense * quality_mult)
            physical_defense = int(physical_defense * quality_mult)
            mental_power = int(mental_power * quality_mult)

        rates = {}
        if item_type in ItemRules.GEAR_TYPES:
            quality = (instance or {}).get("quality", "一般")
            if player_level is None:
                rates = {key: value * QUALITY.get(quality, 1.0)
                         for key, value in equipment_rates(item_config).items()}
            else:
                # 与实战一致：穿戴展示也要计入境界效率衰减
                rates = effective_rates(item_config, player_level, quality)
            physical_damage = magic_damage = physical_defense = magic_defense = mental_power = 0

        return Item(
            equipment_rates=rates,
            item_id=item_config.get("id", item_name), name=item_name, item_type=item_type,
            description=item_config.get("description", ""), rank=normalize_rank(item_config.get("rank"), ""),
            required_level_index=ItemRules.get_required_level(item_config), weapon_category=item_config.get("weapon_category", ""),
            magic_damage=magic_damage, physical_damage=physical_damage, magic_defense=magic_defense, physical_defense=physical_defense,
            mental_power=mental_power, exp_multiplier=item_config.get("exp_multiplier", 0.0),
            spiritual_qi=item_config.get("spiritual_qi", 0), blood_qi=item_config.get("blood_qi", 0),
            life_effects=dict(item_config.get("life_effects") or {})
        )

    def get_gear_slots(self, player: Player) -> dict:
        """槽位以实例映射为准；兼容旧武器、防具名称字段。"""
        result = {}
        mapping = player.get_equipped_instances()
        for slot in ItemRules.SLOT_LABELS:
            inst = self.instance_manager.get_instance(player, mapping.get(slot, 0))
            name = inst.get("name", "") if inst else getattr(player, slot, "") if slot in ("weapon", "armor") else ""
            result[slot] = (name, inst)
        return result

    def get_equipped_items(self, player: Player, items_data: dict, weapons_data: dict = None) -> List[Item]:
        """汇总八个装备槽位、主修心法和功法。"""
        equipped = []
        # 汇总后统一取整及封顶，避免多槽位舍入、品质或自定义配置绕过预算。
        base = self.get_base_attributes(player)
        rates = self.get_equipment_rates(player)
        if rates:
            equipped.append(Item(item_id="equipped_gear", name="装备合计", item_type="gear_total",
                                 **{key: int(base[key] * rate) for key, rate in rates.items()}))
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

    async def equip_item(self, player: Player, item: Item, instance_id: int = 0, target_slot: str = "") -> tuple[bool, str]:
        """装备物品（支持装备实例）"""
        if item is None:
            return False, "❌ 未找到该装备的配置数据，无法穿戴。"
        can_equip, error_msg = self.check_equipment_level_requirement(player, item)
        if not can_equip:
            return False, error_msg

        if item.item_type in ItemRules.GEAR_TYPES:
            slot = ItemRules.SLOT_BY_TYPE[item.item_type]
            slot = "armor" if slot == "body" else slot
            mapping = player.get_equipped_instances()
            if item.item_type == "ring" and not target_slot:
                slot = next((key for key in ("ring1", "ring2") if not mapping.get(key)), "ring1")
            if target_slot:
                group = ItemRules.SLOT_GROUP_ALIASES.get(target_slot)
                if group:
                    if slot not in group:
                        return False, "装备与指定部位不匹配。"
                    if item.item_type == "ring":
                        slot = next((key for key in ("ring1", "ring2") if not mapping.get(key)), "ring1")
                else:
                    requested = ItemRules.SLOT_ALIASES.get(target_slot)
                    allowed = ("ring1", "ring2") if item.item_type == "ring" else (slot,)
                    if requested not in allowed:
                        return False, "装备与指定部位不匹配。"
                    slot = requested
            # 先验证新实例，避免失败时把旧装备移走。
            inst = self.instance_manager.get_instance(player, instance_id) if instance_id else None
            if instance_id and (not inst or inst.get("name") != item.name or inst.get("location") != "storage"):
                return False, "❌ 装备实例不可用，已取消换装。"
            old_item, old_inst = self.get_gear_slots(player)[slot]
            if old_inst:
                # 新装备来自储物戒时会腾出对应格数，净占用不变；否则需要额外空间。
                freeing = (
                    self.storage_ring_manager._get_item_volume(item.name)
                    if inst and self.storage_ring_manager else 0
                )
                ok, msg = self._storage_accepts_instance(player, old_item or item.name, freeing)
                if not ok:
                    return False, msg
                self.instance_manager.move(player, old_inst["id"], "storage")
            elif old_item:
                ok, msg = await self._try_store_old_equipment(player, old_item)
                if not ok:
                    return False, msg
            if not inst:
                inst = self.instance_manager.create_instances(
                    player, item.name, quality=self.instance_manager.roll_quality()
                )[0]
            self.instance_manager.move(player, inst["id"], "equipped", slot)
            mapping[slot] = int(inst["id"])
            player.set_equipped_instances(mapping)
            if slot in ("weapon", "armor"):
                setattr(player, slot, item.name)
            await self.db.update_player(player)
            label = ItemRules.SLOT_LABELS[slot]
            prefix = f"旧装备【{old_item}】已存入储物戒\n" if old_item else ""
            return True, f"{prefix}已装备{label}【{item.name}】（{item.rank}）"

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
        slot = ItemRules.SLOT_ALIASES.get(slot_or_name)
        if not slot:
            slot = next((key for key, (name, _) in self.get_gear_slots(player).items() if name == slot_or_name), None)
        if slot:
            name, inst = self.get_gear_slots(player)[slot]
            if not name:
                return False, f"未装备{ItemRules.SLOT_LABELS[slot]}"
            if inst:
                ok, msg = self._storage_accepts_instance(player, name)
                if not ok:
                    return False, msg
                self.instance_manager.move(player, inst["id"], "storage")
            else:
                ok, msg = await self._try_store_old_equipment(player, name)
                if not ok:
                    return False, msg
            mapping = player.get_equipped_instances()
            mapping.pop(slot, None)
            player.set_equipped_instances(mapping)
            if slot in ("weapon", "armor"):
                setattr(player, slot, "")
            await self.db.update_player(player)
            return True, f"已卸下{ItemRules.SLOT_LABELS[slot]}【{name}】，已存入储物戒"
        elif slot_or_name in ["主修心法", "心法", "main_technique"]:
            if not player.main_technique: return False, "未装备主修心法"
            item_name = player.main_technique
            ok, msg = await self._try_store_old_equipment(player, item_name)
            if not ok:
                return False, f"储物戒空间不足，无法回收主修心法【{item_name}】：{msg}"
            player.main_technique = ""
            await self.db.update_player(player)
            return True, f"已卸下主修心法【{item_name}】，已存入储物戒"

        techniques_list = player.get_techniques_list()
        if slot_or_name in techniques_list:
            ok, msg = await self._try_store_old_equipment(player, slot_or_name)
            if not ok:
                return False, f"储物戒空间不足，无法回收功法【{slot_or_name}】：{msg}"
            techniques_list.remove(slot_or_name)
            player.set_techniques_list(techniques_list)
            await self.db.update_player(player)
            return True, f"已卸下功法【{slot_or_name}】，已存入储物戒"
        return False, f"未找到装备：{slot_or_name}"

    def _storage_accepts_instance(self, player: Player, item_name: str, freeing: int = 0) -> tuple:
        """检查储物戒是否还有空间容纳一件装备实例（freeing 为同时腾出的格数）。"""
        if not self.storage_ring_manager:
            return True, ""
        volume = self.storage_ring_manager._get_item_volume(item_name)
        available = self.storage_ring_manager.get_available_slots(player)
        if available + max(0, int(freeing)) >= volume:
            return True, ""
        capacity = self.storage_ring_manager.get_total_capacity(player)
        return False, (
            f"储物戒容量不足！装备【{item_name}】需要 {volume} 格，"
            f"仅剩 {available}（上限 {capacity}）。请先清理储物戒或使用 /领取溢出。"
        )

    async def _try_store_old_equipment(self, player: Player, item_name: str) -> tuple:
        """尝试将旧装备存入储物戒，返回 (是否成功, 失败消息)"""
        if not self.storage_ring_manager:
            return True, ""
        success, msg = await self.storage_ring_manager.store_item(player, item_name, 1, silent=True)
        if success:
            return True, ""
        return False, msg