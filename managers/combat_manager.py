# managers/combat_manager.py
"""
战斗系统管理器 - 处理HP/MP/ATK系统和战斗逻辑
"""

import random
from typing import Tuple, Dict, Optional, List
from dataclasses import dataclass

@dataclass
class PetCombatState:
    """灵兽独立战斗状态"""
    user_id: str
    name: str
    pet_type: str
    level: int
    hp: int
    max_hp: int
    atk: int
    defense: int = 0
    guard_rate: float = 0.15
    mechanic: str = ""
    pet_id: int = 0
    revive_mult: float = 1.0

    @property
    def alive(self) -> bool:
        return self.hp > 0


@dataclass
class CombatStats:
    """战斗属性"""
    user_id: str
    name: str  # 道号
    hp: int  # 当前气血
    max_hp: int  # 最大气血
    mp: int  # 当前灵气（兼容字段）
    max_mp: int  # 最大灵气（兼容字段）
    atk: int  # 攻击力
    defense: int = 0  # 防御力
    crit_rate: int = 0  # 会心率（百分比）
    exp: int = 0  # 修为（用于计算攻击力）
    effects: dict = None # 战斗特效（功法/词条/灵兽）
    element: str = ""  # 五行属性（金木水火土，空为无属性）
    pet: Optional[PetCombatState] = None  # 主战灵兽
    support_pets: Optional[List[PetCombatState]] = None  # 辅助出战灵兽
    # 物法双轨面板；未显式设置时由 atk/defense 按 50/50 回退
    physical_atk: int = 0
    magic_atk: int = 0
    physical_defense: int = 0
    magic_defense: int = 0
    defense_scale: int = 0
    overflow_bonus: float = 0.0


class CombatManager:
    """战斗系统管理器"""

    # 五行相生相克：key 克 value（金克木、木克土、土克水、水克火、火克金）
    ELEMENT_OVERCOMES = {"金": "木", "木": "土", "土": "水", "水": "火", "火": "金"}
    ELEMENT_ADVANTAGE_MULT = 1.3   # 克制方伤害 +30%
    ELEMENT_DISADVANTAGE_MULT = 0.8 # 被克制方伤害 -20%

    # 同一次攻击的物法两部分共用同一个浮动系数
    DAMAGE_VARIANCE_MIN = 0.70
    DAMAGE_VARIANCE_MAX = 1.30

    # 基础攻击的物法占比：职业只决定基础盘，物伤/法伤仍全额计入对应分轨
    ATTACK_PROFILES = {
        "体修": (0.8, 0.2),
        "剑修": (0.8, 0.2),
        "鬼修": (0.2, 0.8),
        "灵修": (0.3, 0.7),
        "魔修": (0.5, 0.5),
        "佛修": (0.4, 0.6),
        "御兽": (0.5, 0.5),
    }

    # 溢出属性转换：每 1% 溢出暴击 -> +0.5% 暴伤；每 1% 溢出吸血 -> +0.5% 双攻
    CRIT_OVERFLOW_TO_CRIT_DAMAGE = 0.5
    LIFESTEAL_OVERFLOW_TO_ATTACK = 0.5
    CRIT_RATE_CAP = 100.0
    LIFESTEAL_CAP = 100.0
    COUNTER_DAMAGE_CAP = 100.0

    @classmethod
    def normalize_overflow_effects(cls, effects: dict) -> float:
        """钳制暴击/吸血/反伤上限，并把溢出部分转换为暴伤与双攻。

        返回需要应用到物理攻击与法术攻击上的倍率。
        """
        if not isinstance(effects, dict):
            return 1.0

        def as_float(key: str, default: float = 0.0) -> float:
            try:
                return float(effects.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        crit = as_float("crit_rate")
        if crit > cls.CRIT_RATE_CAP:
            excess = crit - cls.CRIT_RATE_CAP
            effects["crit_rate"] = cls.CRIT_RATE_CAP
            crit_damage = as_float("crit_damage", 1.5) or 1.5
            effects["crit_damage"] = crit_damage + excess * cls.CRIT_OVERFLOW_TO_CRIT_DAMAGE / 100.0

        lifesteal = as_float("lifesteal")
        attack_mult = 1.0
        if lifesteal > cls.LIFESTEAL_CAP:
            excess = lifesteal - cls.LIFESTEAL_CAP
            effects["lifesteal"] = cls.LIFESTEAL_CAP
            attack_mult = 1.0 + excess * cls.LIFESTEAL_OVERFLOW_TO_ATTACK / 100.0

        counter = as_float("counter_damage")
        if counter > cls.COUNTER_DAMAGE_CAP:
            effects["counter_damage"] = cls.COUNTER_DAMAGE_CAP

        return attack_mult

    @classmethod
    def get_attack_profile(cls, cultivation_type: str = "") -> Tuple[float, float]:
        """返回 (物理占比, 法术占比)，未知职业默认均衡。"""
        return cls.ATTACK_PROFILES.get(cultivation_type, (0.5, 0.5))

    @staticmethod
    def calculate_effective_experience(experience: int, cap: int) -> int:
        """境界内修为全额生效，超出部分按 30%/15%/5% 分段衰减。"""
        exp = max(0, int(experience or 0))
        cap = max(0, int(cap or 0))
        if cap <= 0 or exp <= cap:
            return exp
        overflow = exp - cap
        first = min(overflow, cap)
        second = min(max(overflow - cap, 0), cap)
        third = max(overflow - cap * 2, 0)
        return cap + int(first * 0.30 + second * 0.15 + third * 0.05)

    @classmethod
    def get_effective_experience(cls, player, config_manager) -> int:
        """读取当前境界承载上限，返回用于战斗属性的有效修为。"""
        cap = 0
        try:
            cap = int(player.get_required_exp(config_manager) or 0)
        except Exception:
            cap = 0
        return cls.calculate_effective_experience(
            getattr(player, "experience", 0), cap
        )

    MAX_OVERFLOW_BONUS = 1.0

    @classmethod
    def get_experience_overflow_bonus(cls, player, config_manager) -> float:
        """返回修为溢出对境界基础属性的加成比例。

        段内修为不额外加成；超过当前境界突破门槛的部分按
        30% / 15% / 5% 折算，最终最多加成 100%。
        """
        try:
            cap = int(player.get_required_exp(config_manager) or 0)
        except Exception:
            cap = 0
        if cap <= 0:
            return 0.0
        effective = cls.calculate_effective_experience(
            getattr(player, "experience", 0), cap
        )
        if effective <= cap:
            return 0.0
        return min(cls.MAX_OVERFLOW_BONUS, (effective - cap) / cap)

    @classmethod
    def calculate_attack_components(
        cls,
        experience: int,
        atkpractice: int = 0,
        atk_buff: float = 0.0,
        physical_damage: int = 0,
        magic_damage: int = 0,
        cultivation_type: str = "灵修",
    ) -> Dict[str, int]:
        """按职业占比拆分基础攻击，并叠加物伤/法伤。

        物攻 + 法攻仍等于原 calculate_atk 的总攻击，保证战力口径稳定。
        """
        base_atk = int(experience) // 10
        practice_bonus = int(atkpractice or 0) * 0.04

        class_multiplier = 1.0
        if cultivation_type == "剑修":
            class_multiplier = 1.5
        elif cultivation_type == "魔修":
            class_multiplier = 1.3
        elif cultivation_type == "佛修":
            class_multiplier = 0.6

        base_total = int(
            base_atk * (1 + practice_bonus + float(atk_buff or 0)) * class_multiplier
        )
        physical_share, magic_share = cls.get_attack_profile(cultivation_type)
        physical_base = int(base_total * physical_share)
        magic_base = base_total - physical_base

        physical_atk = max(0, physical_base + int(physical_damage or 0))
        magic_atk = max(0, magic_base + int(magic_damage or 0))
        return {
            "physical_atk": physical_atk,
            "magic_atk": magic_atk,
            "atk": physical_atk + magic_atk,
        }

    @staticmethod
    def resolve_attack_components(stats: CombatStats) -> Tuple[int, int]:
        """读取攻击分轨；旧对象只有总攻击时按 50/50 回退。"""
        physical = max(0, int(getattr(stats, "physical_atk", 0) or 0))
        magic = max(0, int(getattr(stats, "magic_atk", 0) or 0))
        total = max(0, int(getattr(stats, "atk", 0) or 0))
        if physical <= 0 and magic <= 0 and total > 0:
            physical = total // 2
            magic = total - physical
        return physical, magic

    @staticmethod
    def resolve_defense_components(stats: CombatStats) -> Tuple[int, int]:
        """读取防御分轨；旧对象只有总防御时按 50/50 回退。"""
        physical = max(0, int(getattr(stats, "physical_defense", 0) or 0))
        magic = max(0, int(getattr(stats, "magic_defense", 0) or 0))
        total = max(0, int(getattr(stats, "defense", 0) or 0))
        if physical <= 0 and magic <= 0 and total > 0:
            physical = total // 2
            magic = total - physical
        return physical, magic

    @staticmethod
    def get_element_from_name(name: str) -> str:
        """从功法/心法/武器名称中推断五行属性（金木水火土）"""
        if not name:
            return ""
        for e in ("金", "木", "水", "火", "土"):
            if e in name:
                return e
        return ""

    @classmethod
    def element_multiplier(cls, attacker_element: str, defender_element: str) -> float:
        """计算五行克制伤害倍率"""
        if not attacker_element or not defender_element:
            return 1.0
        if cls.ELEMENT_OVERCOMES.get(attacker_element) == defender_element:
            return cls.ELEMENT_ADVANTAGE_MULT
        if cls.ELEMENT_OVERCOMES.get(defender_element) == attacker_element:
            return cls.ELEMENT_DISADVANTAGE_MULT
        return 1.0
    
    @staticmethod
    def calculate_hp_mp(experience: int, hp_buff: float = 0.0, mp_buff: float = 0.0) -> Tuple[int, int]:
        """根据修为计算HP和MP"""
        base_hp = experience // 2
        base_mp = experience
        
        hp = int(base_hp * (1 + hp_buff))
        mp = int(base_mp * (1 + mp_buff))
        
        return hp, mp
    
    @staticmethod
    def calculate_atk(experience: int, atkpractice: int = 0, atk_buff: float = 0.0, physical_damage: int = 0, magic_damage: int = 0, cultivation_type: str = "灵修") -> int:
        """根据修为、攻击修炼等级和物伤/法伤计算攻击力（加入了职业分化）"""
        base_atk = experience // 10
        practice_bonus = atkpractice * 0.04  # 每级4%加成
        
        # 职业专属攻击加成
        class_multiplier = 1.0
        if cultivation_type == "剑修":
            class_multiplier = 1.5 # 剑修伤害提升 50%
        elif cultivation_type == "魔修":
            class_multiplier = 1.3 # 魔修伤害提升 30%
        elif cultivation_type == "佛修":
            class_multiplier = 0.6 # 佛修伤害降低 40%
            
        total_atk = int(base_atk * (1 + practice_bonus + atk_buff) * class_multiplier) + physical_damage + magic_damage
        
        return max(total_atk, 1)  # 至少为1

    @staticmethod
    def calculate_equipment_bonus(player, config_manager) -> Dict:
        """计算武器/防具对战斗面板的加成，兼容两套装备配置与装备实例品质。"""
        bonus = {
            "atk": 0,
            "defense": 0,
            "physical_atk": 0,
            "magic_atk": 0,
            "physical_defense": 0,
            "magic_defense": 0,
        }
        if not config_manager:
            return bonus
        from ..core.equipment_manager import EquipmentManager
        manager = EquipmentManager(None, config_manager)
        base = manager.get_base_attributes(player)
        rates = manager.get_equipment_rates(player)
        bonus["physical_atk"] = int(base.get("physical_damage", 0) * rates.get("physical_damage", 0))
        bonus["magic_atk"] = int(base.get("magic_damage", 0) * rates.get("magic_damage", 0))
        bonus["physical_defense"] = int(base.get("physical_defense", 0) * rates.get("physical_defense", 0))
        bonus["magic_defense"] = int(base.get("magic_defense", 0) * rates.get("magic_defense", 0))
        bonus["atk"] = bonus["physical_atk"] + bonus["magic_atk"]
        bonus["defense"] = bonus["physical_defense"] + bonus["magic_defense"]
        return bonus

    @staticmethod
    def get_permanent_bonus_data(player) -> dict:
        try:
            import json
            data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def get_title_combat_bonus(player) -> dict:
        """读取当前最高品阶称号的战斗加成（不依赖数据库写入）。"""
        try:
            from .title_manager import TitleManager
            return TitleManager(None).get_active_bonus(player) or {}
        except Exception:
            return {}

    @staticmethod
    def get_forge_multiplier(player) -> float:
        """炼器等级：每级 +2% 攻击/防御，最高 +15 级。"""
        data = CombatManager.get_permanent_bonus_data(player)
        try:
            level = max(0, min(15, int(data.get("forge_level", 0) or 0)))
        except (TypeError, ValueError):
            level = 0
        return 1.0 + level * 0.02

    @staticmethod
    def build_combat_panel(
        player,
        pill_multipliers: Optional[dict],
        impart_info,
        config_manager,
        effects: Optional[dict] = None,
    ) -> Dict:
        """构建与实战一致的气血/灵气/攻击/防御/暴击面板。"""
        pill_multipliers = pill_multipliers or {}
        # 复制一份，避免展示面板回写调用方的特效字典（名片/排行共用同一份数据）。
        effects = dict(effects or {})
        try:
            from .talisman_manager import TalismanManager
            for key, value in TalismanManager.collect_active_combat_effects(player).items():
                effects[key] = effects.get(key, 0) + value
        except Exception:
            pass

        hp_buff = impart_info.impart_hp_per if impart_info else 0.0
        mp_buff = impart_info.impart_mp_per if impart_info else 0.0
        atk_buff = impart_info.impart_atk_per if impart_info else 0.0

        from ..core.equipment_balance import realm_attributes
        base = realm_attributes(player, config_manager)
        overflow_bonus = CombatManager.get_experience_overflow_bonus(
            player, config_manager
        )
        base_phys = int(int(base.get("physical_damage", 0)) * (1 + overflow_bonus))
        base_magic = int(int(base.get("magic_damage", 0)) * (1 + overflow_bonus))
        base_pdef = int(int(base.get("physical_defense", 0)) * (1 + overflow_bonus))
        base_mdef = int(int(base.get("magic_defense", 0)) * (1 + overflow_bonus))
        base_atk_total = max(1, base_phys + base_magic)
        physical_mult = float(pill_multipliers.get("physical_damage", 1.0) or 1.0)
        magic_mult = float(pill_multipliers.get("magic_damage", 1.0) or 1.0)
        base_max_hp = int(int(base.get("max_hp", 1)) * (1 + overflow_bonus))
        max_hp = max(1, int(base_max_hp * (1 + max(-.5, min(.5, hp_buff)))))
        max_mp = max(0, int(getattr(player, "max_spiritual_qi", 0) or 0))
        raw_hp = int(getattr(player, "hp", 0) or 0)
        raw_mp = int(getattr(player, "spiritual_qi", 0) or 0)
        hp = min(max_hp, raw_hp) if raw_hp > 0 else max_hp
        mp = min(max_mp, max(0, raw_mp))

        equip = CombatManager.calculate_equipment_bonus(player, config_manager)

        title = CombatManager.get_title_combat_bonus(player)
        forge_bonus = CombatManager.get_forge_multiplier(player) - 1.0
        practice_bonus = min(0.20, max(0, int(player.atkpractice or 0)) * 0.01)
        phys_attack_bonus = max(-.5, min(.5, physical_mult - 1 + atk_buff + practice_bonus + forge_bonus))
        magic_attack_bonus = max(-.5, min(.5, magic_mult - 1 + atk_buff + practice_bonus + forge_bonus))
        defense_bonus = max(
            -.5,
            min(
                .5,
                max(
                    float(pill_multipliers.get("physical_defense", 1.0) or 1.0),
                    float(pill_multipliers.get("magic_defense", 1.0) or 1.0),
                ) - 1 + forge_bonus,
            ),
        )
        permanent_pill_limit = 0.30
        try:
            import json
            bonus_data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            if isinstance(bonus_data, dict):
                permanent_pill_limit += max(0.0, min(0.50, float(bonus_data.get("permanent_pill_limit_bonus", 0) or 0)))
        except Exception:
            pass
        permanent_total = min(
            int(base_atk_total * permanent_pill_limit),
            max(0, int(player.physical_damage or 0) + int(player.magic_damage or 0)),
        )
        perm_phys = int(permanent_total * base_phys / base_atk_total)
        perm_magic = permanent_total - perm_phys
        title_total = min(
            int(base_atk_total * .10),
            max(0, int(title.get("atk", 0) or 0) + int(title.get("magic", 0) or 0)),
        )
        title_phys = int(title_total * base_phys / base_atk_total)
        title_magic = title_total - title_phys
        physical_atk = int(base_phys * (1 + phys_attack_bonus)) + int(equip.get("physical_atk", 0)) + perm_phys + title_phys
        magic_atk = int(base_magic * (1 + magic_attack_bonus)) + int(equip.get("magic_atk", 0)) + perm_magic + title_magic
        physical_def = int(base_pdef * (1 + defense_bonus)) + int(equip.get("physical_defense", 0))
        magic_def = int(base_mdef * (1 + defense_bonus)) + int(equip.get("magic_defense", 0))
        permanent_def_total = min(
            int((base_pdef + base_mdef) * permanent_pill_limit),
            max(0, int(player.physical_defense or 0) + int(player.magic_defense or 0)),
        )
        pdef_share = base_pdef / max(1, base_pdef + base_mdef)
        physical_def += int(permanent_def_total * pdef_share)
        magic_def += permanent_def_total - int(permanent_def_total * pdef_share)
        title_def_total = min(
            int((base_pdef + base_mdef) * .10),
            max(0, int(title.get("phys_def", 0) or 0) + int(title.get("magic_def", 0) or 0)),
        )
        physical_def += int(title_def_total * pdef_share)
        magic_def += title_def_total - int(title_def_total * pdef_share)
        effects["crit_rate"] = effects.get("crit_rate", 0) + int(title.get("crit", 0) or 0)
        effects["lifesteal"] = effects.get("lifesteal", 0) + int(title.get("lifesteal", 0) or 0)
        # 传承的会心/爆伤同样属于战斗面板，名片与排行必须与实战口径一致。
        if impart_info:
            effects["crit_rate"] = effects.get("crit_rate", 0) + int(
                float(getattr(impart_info, "impart_know_per", 0.0) or 0.0) * 100
            )
            effects["crit_damage"] = effects.get("crit_damage", 1.5) + float(
                getattr(impart_info, "impart_burst_per", 0.0) or 0.0
            )
        # 暴击/吸血溢出转换会改变双攻，展示面板也需先结算。
        overflow_attack_mult = CombatManager.normalize_overflow_effects(effects)
        if overflow_attack_mult != 1.0:
            physical_atk = max(0, int(physical_atk * overflow_attack_mult))
            magic_atk = max(0, int(magic_atk * overflow_attack_mult))
        atk = max(1, physical_atk + magic_atk)
        defense = max(0, physical_def + magic_def)

        return {
            "hp": hp, "max_hp": max_hp,
            "mp": mp, "max_mp": max_mp,
            "atk": atk, "defense": defense,
            "physical_atk": physical_atk, "magic_atk": magic_atk,
            "physical_defense": physical_def, "magic_defense": magic_def,
            "defense_scale": int(base.get("defense_scale", 0)),
            "overflow_bonus": overflow_bonus,
            "crit_rate": effects.get("crit_rate", 0),
            "crit_damage": effects.get("crit_damage", 1.5),
            "lifesteal": effects.get("lifesteal", 0),
            "counter_damage": effects.get("counter_damage", 0),
        }

    @staticmethod
    def collect_combat_effects(player, config_manager) -> dict:
        """收集功法/装备/全局词条特效，保持与装备管理器一致。"""
        effects = {
            "crit_rate": 0,
            "crit_damage": 1.5,
            "lifesteal": 0,
            "counter_damage": 0,
            "defense_bonus": 0,
            "combo_rate": 0,
            "dodge": 0,
            "physical_damage_multiplier": 1.0,
            "magic_damage_multiplier": 1.0,
        }
        techniques_data = getattr(config_manager, "techniques_data", {}) or {}
        items_data = getattr(config_manager, "items_data", {}) or {}
        for tech_name in player.get_techniques_list():
            tech_config = techniques_data.get(tech_name) or items_data.get(tech_name)
            if tech_config and "effects" in tech_config:
                for key, value in tech_config["effects"].items():
                    if key in effects:
                        effects[key] += value
        if player.main_technique:
            main_config = techniques_data.get(player.main_technique) or items_data.get(player.main_technique)
            if main_config and "effects" in main_config:
                for key, value in main_config["effects"].items():
                    if key in effects:
                        effects[key] += value
        try:
            import json
            bonus_data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            for affix in bonus_data.get("affixes", []):
                key = affix.get("key")
                value = affix.get("value", 0)
                if key in ("physical_damage_multiplier", "magic_damage_multiplier"):
                    continue
                if key in effects:
                    effects[key] += value
        except Exception:
            pass
        from ..core.equipment_manager import EquipmentManager
        from ..core.equipment_balance import GEAR_EFFECT_CAPS
        weapons_data = getattr(config_manager, "weapons_data", {}) or {}
        # 装备特效单独累计后按部位总上限收敛，避免多槽位重复叠满；
        # 不能直接钳制总特效，否则功法/传承提供的暴击、爆伤也会被装备上限误伤。
        gear_effects = {}

        def add_gear_effect(key, value):
            if key in ("physical_damage_multiplier", "magic_damage_multiplier"):
                return
            if key not in effects:
                return
            try:
                value = min(float(GEAR_EFFECT_CAPS.get(key, value)), float(value or 0))
            except (TypeError, ValueError):
                value = 0
            gear_effects[key] = gear_effects.get(key, 0) + value

        for name, _ in EquipmentManager(None, config_manager).get_gear_slots(player).values():
            cfg = items_data.get(name) or weapons_data.get(name, {})
            for key, value in cfg.get("effects", {}).items():
                add_gear_effect(key, value)

        try:
            from ..core.item_instance_manager import ItemInstanceManager
            im = ItemInstanceManager(None, config_manager)
            for instance_id in player.get_equipped_instances().values():
                inst = im.get_instance(player, instance_id)
                if not inst:
                    continue
                for affix in inst.get("affixes", []) or []:
                    add_gear_effect(affix.get("key"), affix.get("value", 0))
        except Exception:
            pass
        for key, cap in GEAR_EFFECT_CAPS.items():
            if key in gear_effects:
                gear_effects[key] = min(gear_effects[key], float(cap))
        for key, value in gear_effects.items():
            effects[key] = effects.get(key, 0) + value
        # 防御脏配置/旧生成器数据：百分比类词条统一钳制到 0-100，避免 500% 暴击/吸血
        for rate_key in ("crit_rate", "lifesteal", "combo_rate", "counter_damage", "dodge"):
            if rate_key in effects:
                try:
                    effects[rate_key] = max(0.0, min(100.0, float(effects[rate_key])))
                except (TypeError, ValueError):
                    effects[rate_key] = 0
        # 装备增伤已进入装备百分比池，特效层不再二次放大。
        effects["physical_damage_multiplier"] = 1.0
        effects["magic_damage_multiplier"] = 1.0
        return CombatManager.clamp_effects(effects)

    @staticmethod
    def clamp_effects(effects: dict) -> dict:
        """统一限制战斗特效上限，避免装备/词条/符箓叠加失控。"""
        from ..core.equipment_balance import GLOBAL_EFFECT_CAPS
        result = dict(effects or {})
        for key, cap in GLOBAL_EFFECT_CAPS.items():
            if key in result:
                try:
                    result[key] = max(0, min(float(cap), float(result[key] or 0)))
                except (TypeError, ValueError):
                    result[key] = 0
        return result

    @classmethod
    def build_player_combat_stats(
        cls,
        player,
        config_manager=None,
        db=None,
        hp_buff: float = 0.0,
        mp_buff: float = 0.0,
        atk_buff: float = 0.0,
        crit_rate: int = 0,
        crit_damage_bonus: float = 0.0,
        extra_effects: Optional[dict] = None,
        element: Optional[str] = None,
        pill_multipliers: Optional[dict] = None,
    ) -> "CombatStats":
        """统一构建玩家战斗属性，确保所有战斗入口读取装备、词条与丹药。"""
        from ..core.equipment_balance import realm_attributes
        base = realm_attributes(player, config_manager)
        overflow_bonus = cls.get_experience_overflow_bonus(player, config_manager)
        base_phys = int(int(base.get("physical_damage", 0)) * (1 + overflow_bonus))
        base_magic = int(int(base.get("magic_damage", 0)) * (1 + overflow_bonus))
        base_pdef = int(int(base.get("physical_defense", 0)) * (1 + overflow_bonus))
        base_mdef = int(int(base.get("magic_defense", 0)) * (1 + overflow_bonus))
        base_atk_total = max(1, base_phys + base_magic)
        base_max_hp = int(int(base.get("max_hp", 1)) * (1 + overflow_bonus))
        max_hp = max(1, int(base_max_hp * (1 + max(-.5, min(.5, hp_buff)))))
        max_mp = max(0, int(getattr(player, "max_spiritual_qi", 0) or 0))
        raw_hp = int(getattr(player, "hp", 0) or 0)
        raw_mp = int(getattr(player, "spiritual_qi", 0) or 0)
        hp = min(max_hp, raw_hp) if raw_hp > 0 else max_hp
        mp = min(max_mp, max(0, raw_mp))

        equip = {"atk": 0, "defense": 0}
        effects = {}
        if config_manager is not None:
            equip = cls.calculate_equipment_bonus(player, config_manager)
            effects = cls.collect_combat_effects(player, config_manager)
        if pill_multipliers is None and config_manager is not None:
            try:
                from ..core.pill_manager import PillManager
                pill_multipliers = PillManager(db, config_manager).calculate_pill_attribute_effects(player)
            except Exception:
                pill_multipliers = {}
        pill_multipliers = pill_multipliers or {}
        if crit_damage_bonus:
            effects["crit_damage"] = effects.get("crit_damage", 1.5) + float(crit_damage_bonus)
        if extra_effects:
            for key, value in extra_effects.items():
                if isinstance(value, (int, float)):
                    effects[key] = effects.get(key, 0) + value
                else:
                    effects[key] = value

        # 符箓效果：暴击/吸血/反击/闪避等
        try:
            from .talisman_manager import TalismanManager
            for key, value in TalismanManager.collect_active_combat_effects(player).items():
                effects[key] = effects.get(key, 0) + value
        except Exception:
            pass

        if element is None and config_manager is not None:
            try:
                element = (
                    config_manager.get_item_element(player.main_technique)
                    or config_manager.get_item_element(player.weapon)
                )
            except Exception:
                element = ""
        if not element:
            element = (
                cls.get_element_from_name(player.main_technique)
                or cls.get_element_from_name(player.weapon)
            )

        physical_mult = float(pill_multipliers.get("physical_damage", 1.0) or 1.0)
        magic_mult = float(pill_multipliers.get("magic_damage", 1.0) or 1.0)
        title = cls.get_title_combat_bonus(player)
        forge_bonus = cls.get_forge_multiplier(player) - 1.0
        practice_bonus = min(0.20, max(0, int(player.atkpractice or 0)) * 0.01)
        phys_attack_bonus = max(-.5, min(.5, physical_mult - 1 + atk_buff + practice_bonus + forge_bonus))
        magic_attack_bonus = max(-.5, min(.5, magic_mult - 1 + atk_buff + practice_bonus + forge_bonus))
        defense_bonus = max(
            -.5,
            min(
                .5,
                max(
                    float(pill_multipliers.get("physical_defense", 1.0) or 1.0),
                    float(pill_multipliers.get("magic_defense", 1.0) or 1.0),
                ) - 1 + forge_bonus,
            ),
        )
        # 永久丹药/先天随机属性保留为有上限的附加值，不作为装备基数。
        permanent_pill_limit = 0.30
        try:
            import json
            bonus_data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            if isinstance(bonus_data, dict):
                permanent_pill_limit += max(0.0, min(0.50, float(bonus_data.get("permanent_pill_limit_bonus", 0) or 0)))
        except Exception:
            pass
        permanent_total = min(
            int(base_atk_total * permanent_pill_limit),
            max(0, int(player.physical_damage or 0) + int(player.magic_damage or 0)),
        )
        perm_phys = int(permanent_total * base_phys / base_atk_total)

        perm_magic = permanent_total - perm_phys
        title_total = min(
            int(base_atk_total * .10),
            max(0, int(title.get("atk", 0) or 0) + int(title.get("magic", 0) or 0)),
        )
        title_phys = int(title_total * base_phys / base_atk_total)
        title_magic = title_total - title_phys
        physical_atk = int(base_phys * (1 + phys_attack_bonus)) + int(equip.get("physical_atk", 0)) + perm_phys + title_phys
        magic_atk = int(base_magic * (1 + magic_attack_bonus)) + int(equip.get("magic_atk", 0)) + perm_magic + title_magic
        physical_def = int(base_pdef * (1 + defense_bonus)) + int(equip.get("physical_defense", 0))
        magic_def = int(base_mdef * (1 + defense_bonus)) + int(equip.get("magic_defense", 0))
        permanent_def_total = min(
            int((base_pdef + base_mdef) * permanent_pill_limit),
            max(0, int(player.physical_defense or 0) + int(player.magic_defense or 0)),
        )
        pdef_share = base_pdef / max(1, base_pdef + base_mdef)
        physical_def += int(permanent_def_total * pdef_share)
        magic_def += permanent_def_total - int(permanent_def_total * pdef_share)
        title_def_total = min(
            int((base_pdef + base_mdef) * .10),
            max(0, int(title.get("phys_def", 0) or 0) + int(title.get("magic_def", 0) or 0)),
        )
        physical_def += int(title_def_total * pdef_share)
        magic_def += title_def_total - int(title_def_total * pdef_share)
        effects["crit_rate"] = effects.get("crit_rate", 0) + int(title.get("crit", 0) or 0)
        effects["lifesteal"] = effects.get("lifesteal", 0) + int(title.get("lifesteal", 0) or 0)
        # 把传承会心并入统一特效池，确保溢出转换在所有战斗入口生效。
        if crit_rate:
            effects["crit_rate"] = effects.get("crit_rate", 0) + float(crit_rate or 0)
        overflow_attack_mult = cls.normalize_overflow_effects(effects)
        if overflow_attack_mult != 1.0:
            physical_atk = max(0, int(physical_atk * overflow_attack_mult))
            magic_atk = max(0, int(magic_atk * overflow_attack_mult))
        final_atk = max(1, physical_atk + magic_atk)
        defense = max(0, physical_def + magic_def)
        return CombatStats(
            user_id=player.user_id,
            name=player.user_name or player.user_id,
            hp=hp,
            max_hp=max_hp,
            mp=mp,
            max_mp=max_mp,
            atk=final_atk,
            defense=defense,
            # 会心已并入 effects，避免 _execute_attack 重复计算
            crit_rate=0,
            exp=player.experience,
            effects=effects,
            element=element or "",
            physical_atk=physical_atk,
            magic_atk=magic_atk,
            physical_defense=physical_def,
            magic_defense=magic_def,
            defense_scale=int(base.get("defense_scale", 0)),
            overflow_bonus=overflow_bonus,
        )

    @staticmethod
    def calculate_combat_power(panel: Dict, mental_power: int = 0) -> int:
        """统一战力公式：气血/10 + 攻击 + 防御 + 精神力/10

        玩家名片、战力排行等所有展示入口都必须调用此方法，
        避免各自维护公式导致同一玩家显示不同战力。
        """
        return int(
            panel.get("hp", 0) / 10
            + panel.get("atk", 0)
            + panel.get("defense", 0)
            + int(mental_power or 0) // 10
        )
    
    @classmethod
    def apply_pet_lineup(
        cls,
        stats,
        main_pet=None,
        support_pets=None,
        pet_effects=None,
        final_owner_atk: Optional[int] = None,
    ):
        """把灵兽/主人加成同步到物法双轨面板，兼容 CombatStats 和 dict 面板。"""
        def get(name, default=0):
            if isinstance(stats, dict):
                return int(stats.get(name, default) or default)
            return int(getattr(stats, name, default) or default)

        def set_value(name, value):
            if isinstance(stats, dict):
                stats[name] = int(value)
            else:
                setattr(stats, name, int(value))

        physical = max(0, get("physical_atk", 0))
        magic = max(0, get("magic_atk", 0))
        total = max(0, get("atk", 0))
        if physical <= 0 and magic <= 0 and total > 0:
            physical, magic = total // 2, total - total // 2

        target_atk = int(final_owner_atk) if final_owner_atk is not None else total
        delta = target_atk - (physical + magic)
        if delta:
            physical += delta
            if physical < 0:
                magic = max(0, magic + physical)
                physical = 0
        physical = max(0, physical)
        magic = max(0, magic)

        p_def = max(0, get("physical_defense", 0))
        m_def = max(0, get("magic_defense", 0))
        total_def = max(0, get("defense", 0))
        if p_def <= 0 and m_def <= 0 and total_def > 0:
            p_def, m_def = total_def // 2, total_def - total_def // 2

        effects = pet_effects or {}
        bonus_def = int(effects.get("defense_bonus", 0) or 0)
        if bonus_def:
            p_def += bonus_def // 2
            m_def += bonus_def - bonus_def // 2

        if isinstance(stats, dict):
            merged = {
                key: stats.get(key, 1.5 if key == "crit_damage" else 0)
                for key in ("crit_rate", "crit_damage", "lifesteal", "counter_damage")
            }
        else:
            merged = dict(getattr(stats, "effects", None) or {})
        for key, value in effects.items():
            if isinstance(value, (int, float)):
                merged[key] = merged.get(key, 0) + value
            else:
                merged[key] = value

        # 暴击/吸血溢出转换会改变双攻，因此必须在写入面板前结算。
        attack_mult = cls.normalize_overflow_effects(merged)
        if attack_mult != 1.0:
            physical = max(0, int(physical * attack_mult))
            magic = max(0, int(magic * attack_mult))

        set_value("physical_atk", physical)
        set_value("magic_atk", magic)
        set_value("physical_defense", p_def)
        set_value("magic_defense", m_def)
        set_value("atk", physical + magic)
        set_value("defense", p_def + m_def)

        if isinstance(stats, dict):
            for key in ("crit_rate", "crit_damage", "lifesteal", "counter_damage"):
                if key in merged:
                    stats[key] = merged[key]
            stats["pet"] = main_pet
            stats["support_pets"] = support_pets or []
        else:
            stats.effects = merged
            stats.pet = main_pet
            stats.support_pets = support_pets or []
        return stats

    @staticmethod
    def apply_damage_reduction(damage: int, defense: int = 0, defense_scale: int = 0) -> int:
        """应用伤害减免；有境界尺度时使用同尺度递减，避免数值膨胀。"""
        if defense <= 0:
            return damage
        if defense_scale and defense_scale > 0:
            reduction_rate = min(0.75, defense / (defense + defense_scale))
        else:
            reduction_rate = defense / (defense + 100)
        final_damage = int(damage * (1 - reduction_rate))
        return max(1, final_damage)
    
    @classmethod
    def _execute_attack(cls, attacker: CombatStats, defender: CombatStats) -> Tuple[int, List[str], int]:
        """执行一次攻击，返回 (最终伤害, 战斗日志列表, 吸血量)"""
        logs = []
        effects = attacker.effects or {}
        defender_effects = defender.effects or {}

        # 0. 闪避（符箓/词条）
        try:
            dodge = min(60.0, float(defender_effects.get("dodge", 0) or 0))
        except (TypeError, ValueError):
            dodge = 0.0
        if dodge > 0 and random.randint(1, 100) <= dodge:
            return 0, [f"{defender.name} 身形一闪，避开了攻击"], 0

        # 1. 物法双轨基础伤害：同一次攻击共用一个 70%~130% 浮动系数
        physical_atk, magic_atk = cls.resolve_attack_components(attacker)
        physical_def, magic_def = cls.resolve_defense_components(defender)
        variance = random.uniform(
            cls.DAMAGE_VARIANCE_MIN, cls.DAMAGE_VARIANCE_MAX
        )
        physical_damage = (
            variance * physical_atk
            * float(effects.get("physical_damage_multiplier", 1.0) or 1.0)
        )
        magic_damage = (
            variance * magic_atk
            * float(effects.get("magic_damage_multiplier", 1.0) or 1.0)
        )

        # 五行克制对一次攻击的物法两部分同时生效
        elem_mult = cls.element_multiplier(attacker.element, defender.element)
        if elem_mult != 1.0:
            physical_damage *= elem_mult
            magic_damage *= elem_mult

        # 2. 一次攻击只判定一次暴击；暴击时物法两部分同时暴击
        base_crit = attacker.crit_rate
        bonus_crit = effects.get("crit_rate", 0)
        if random.randint(0, 100) <= (base_crit + bonus_crit):
            crit_mult = effects.get("crit_damage", 1.5)
            physical_damage *= crit_mult
            magic_damage *= crit_mult
            logs.append(f"💥 {attacker.name} 触发会心一击！")

        # 3. 一次攻击只判定一次连击；连击时物法两部分同时追加
        combo_rate = effects.get("combo_rate", 0)
        if random.randint(0, 100) <= combo_rate:
            physical_damage *= 1.5
            magic_damage *= 1.5
            logs.append(f"⚡ {attacker.name} 触发功法连击，物法两部分同时增强！")

        # 4. 物理按物防、法术按法防分别减伤
        physical_final = (
            cls.apply_damage_reduction(int(physical_damage), physical_def, defender.defense_scale)
            if physical_damage > 0 else 0
        )
        magic_final = (
            cls.apply_damage_reduction(int(magic_damage), magic_def, defender.defense_scale)
            if magic_damage > 0 else 0
        )
        final_damage = physical_final + magic_final
        if physical_final > 0 and magic_final > 0:
            logs.append(
                f"🗡️ {attacker.name} 造成 {final_damage} 点伤害"
                f"（物理 {physical_final} / 法术 {magic_final}）"
            )
        elif physical_final > 0:
            logs.append(f"🗡️ {attacker.name} 造成 {physical_final} 点物理伤害")
        elif magic_final > 0:
            logs.append(f"✨ {attacker.name} 造成 {magic_final} 点法术伤害")

        # 5. 灵兽每回合协战
        attacker_pets = []
        if getattr(attacker, "pet", None) and attacker.pet.alive:
            attacker_pets.append(attacker.pet)
        for support in (getattr(attacker, "support_pets", None) or []):
            if support and support.alive:
                attacker_pets.append(support)
        if attacker_pets:
            pet_count = len(attacker_pets)
            pet_factor = 1.0 / (1 + 0.4 * (pet_count - 1))
            for pet in attacker_pets:
                pet_damage = int(round(random.uniform(0.95, 1.05), 2) * pet.atk * pet_factor)
                pet_damage = cls.apply_damage_reduction(pet_damage, physical_def, defender.defense_scale)
                if pet.mechanic == "burn":
                    burn = max(1, int(pet.atk * pet_factor * 0.3))
                    pet_damage += burn
                    logs.append(f"🔥 【{pet.name}】喷吐灵火，追加灼烧 {burn} 点伤害！")
                if pet_damage > 0:
                    final_damage += pet_damage
                    logs.append(f"🐾 【{pet.name}】协同攻击，追加 {pet_damage} 点伤害！")
                if pet.mechanic == "dragon" and pet_damage > 0:
                    heal_pet = max(1, int(pet_damage * 0.2))
                    attacker.hp = min(attacker.max_hp, attacker.hp + heal_pet)
                    logs.append(f"🐉 【{pet.name}】龙息反哺，{attacker.name} 恢复 {heal_pet} 点气血！")

        # 6. 防守方主战灵兽护主承伤
        defender_pets = []
        if getattr(defender, "pet", None):
            defender_pets.append(defender.pet)
        defender_pets.extend(getattr(defender, "support_pets", None) or [])
        defender_pet = next((p for p in defender_pets if p and p.alive), None)
        if defender_pet and final_damage > 0:
            incoming = int(final_damage * defender_pet.guard_rate)
            if incoming > 0:
                protected = min(incoming, defender_pet.hp)
                pet_loss = cls.apply_damage_reduction(incoming, defender_pet.defense)
                pet_loss = min(defender_pet.hp, max(1, pet_loss))
                defender_pet.hp -= pet_loss
                final_damage -= protected
                logs.append(
                    f"🛡️ 【{defender_pet.name}】替 {defender.name} 挡下 {protected} 点伤害"
                    f"（自身损耗 {pet_loss} 点精力）！"
                )
                if defender_pet.hp <= 0:
                    defender_pet.hp = 0
                    logs.append(f"💀 【{defender_pet.name}】力竭倒下，本场战斗无法继续协战！")

        # 7. 吸血计算
        lifesteal_percent = effects.get("lifesteal", 0)
        heal_amount = 0
        if lifesteal_percent > 0:
            heal_amount = int(final_damage * (lifesteal_percent / 100))
            if heal_amount > 0:
                logs.append(f"🩸 {attacker.name} 吸取了 {heal_amount} 点气血！")

        return final_damage, logs, heal_amount

    @classmethod
    def _shorten_combat_log(cls, combat_log, round_num, winner, player1, player2):
        """只保留前 5 回合、后 5 回合和最终结果，避免长战报触发合并转发。"""
        if winner == player1.user_id:
            win_name = player1.name
        elif winner == player2.user_id:
            win_name = player2.name
        else:
            win_name = "平局"

        result_lines = [
            "",
            "☆━━━━ 战斗结束 ━━━━☆",
            f"胜者：{win_name}",
            f"回合数：{round_num}",
            f"{player1.name} 剩余 HP：{max(0, player1.hp)} | "
            f"{player2.name} 剩余 HP：{max(0, player2.hp)}",
        ]

        if round_num <= 10:
            return combat_log + result_lines

        markers = [
            i for i, line in enumerate(combat_log)
            if line.startswith("-- 第 ") and line.endswith(" 回合 --")
        ]
        if len(markers) <= 10:
            return combat_log + result_lines

        header = combat_log[:markers[0]]
        blocks = []
        for idx, start in enumerate(markers):
            end = markers[idx + 1] if idx + 1 < len(markers) else len(combat_log)
            blocks.append(combat_log[start:end])

        omitted = len(blocks) - 10
        shortened = list(header)
        for block in blocks[:5]:
            shortened.extend(block)
        shortened.append(f"……（中间省略 {omitted} 回合）……")
        for block in blocks[-5:]:
            shortened.extend(block)
        shortened.extend(result_lines)
        return shortened

    @classmethod
    def player_vs_player(cls, player1: CombatStats, player2: CombatStats, combat_type: int = 1) -> Dict:
        """玩家vs玩家战斗 (限制5回合，按剩余血量百分比判定)"""
        combat_log = [f"☆━━━━ 战斗开始 ━━━━☆", f"{player1.name} VS {player2.name}"]
        round_num = 0
        max_rounds = 5  # ★ 修改：最多5回合
        
        while player1.hp > 0 and player2.hp > 0 and round_num < max_rounds:
            round_num += 1
            combat_log.append(f"-- 第 {round_num} 回合 --")
            
            # 玩家1攻击
            dmg1, logs1, heal1 = cls._execute_attack(player1, player2)
            player2.hp = max(0, player2.hp - dmg1)
            player1.hp = min(player1.max_hp, player1.hp + heal1)
            combat_log.extend(logs1)
            combat_log.append(f"{player2.name} 剩余 HP: {max(0, player2.hp)}")
            
            # 反伤判定（针对玩家2的功法）
            counter = (player2.effects or {}).get("counter_damage", 0)
            if counter > 0:
                reflect_dmg = int(dmg1 * (counter / 100))
                player1.hp = max(0, player1.hp - reflect_dmg)
                combat_log.append(f"🛡️ {player2.name} 触发反伤，反弹 {reflect_dmg} 点伤害！")
                combat_log.append(f"{player1.name} 剩余 HP: {max(0, player1.hp)}")
            
            if player2.hp <= 0 or player1.hp <= 0: break
            
            # 玩家2攻击
            dmg2, logs2, heal2 = cls._execute_attack(player2, player1)
            player1.hp = max(0, player1.hp - dmg2)
            player2.hp = min(player2.max_hp, player2.hp + heal2)
            combat_log.extend(logs2)
            combat_log.append(f"{player1.name} 剩余 HP: {max(0, player1.hp)}")
            
            # 反伤判定（针对玩家1的功法）
            counter1 = (player1.effects or {}).get("counter_damage", 0)
            if counter1 > 0:
                reflect_dmg = int(dmg2 * (counter1 / 100))
                player2.hp = max(0, player2.hp - reflect_dmg)
                combat_log.append(f"🛡️ {player1.name} 触发反伤，反弹 {reflect_dmg} 点伤害！")
                combat_log.append(f"{player2.name} 剩余 HP: {max(0, player2.hp)}")
                
            combat_log.append("")
        
        # ★ 修改：判定胜负逻辑
        if player2.hp <= 0 and player1.hp > 0:
            winner = player1.user_id
        elif player1.hp <= 0 and player2.hp > 0:
            winner = player2.user_id
        elif player1.hp <= 0 and player2.hp <= 0:
            winner = "平局"
        else:
            # 打满回合未死亡，按剩余血量百分比判定
            p1_ratio = player1.hp / player1.max_hp if player1.max_hp > 0 else 0
            p2_ratio = player2.hp / player2.max_hp if player2.max_hp > 0 else 0
            if p1_ratio > p2_ratio:
                winner = player1.user_id
            elif p2_ratio > p1_ratio:
                winner = player2.user_id
            else:
                winner = "平局"
        
        # 提取真实的结束血量供展示（不打满血）
        real_p1_hp = max(0, player1.hp)
        real_p2_hp = max(0, player2.hp)
        
        if combat_type == 1:
            p1_hp, p2_hp = player1.max_hp, player2.max_hp
        else:
            p1_hp = max(1, real_p1_hp) if real_p1_hp > 0 else 1
            p2_hp = max(1, real_p2_hp) if real_p2_hp > 0 else 1
            
        combat_log = cls._shorten_combat_log(
            combat_log, round_num, winner, player1, player2
        )
        return {
            "winner": winner, "combat_log": combat_log, "rounds": round_num,
            "player1_final_hp": p1_hp, "player1_final_mp": player1.mp,
            "player2_final_hp": p2_hp, "player2_final_mp": player2.mp,
            "player1_real_hp": real_p1_hp, "player2_real_hp": real_p2_hp
        }

    @classmethod
    def player_vs_player_balanced(
        cls,
        player1: CombatStats,
        player2: CombatStats,
        player2_hp_bonus: float = 0.0,
        draw_to_player2: bool = False,
    ) -> Dict:
        """天道压制 PVP（强制拉平基础属性，限5回合）

        player2_hp_bonus：守方气血加成，用于押镖等防守方场景。
        draw_to_player2：5回合未分胜负时，将平局判给守方。
        """
        p1_orig_hp, p1_orig_atk = player1.hp, player1.atk
        p2_orig_hp, p2_orig_atk = player2.hp, player2.atk
        
        # 强制拉平基础属性 (HP 锁定 10000，守方可获得护镖气血加成)
        player1.hp = player1.max_hp = 10000
        player2.hp = player2.max_hp = int(10000 * (1 + max(0.0, player2_hp_bonus)))
        
        # 攻击力压制 (基础500，并且只保留原装备/词条 20% 的纯附加值优势)
        p1_atk_bonus = max(0, p1_orig_atk - (p1_orig_hp * 0.1))
        player1.atk = 500 + int(p1_atk_bonus * 0.2)
        
        p2_atk_bonus = max(0, p2_orig_atk - (p2_orig_hp * 0.1))
        player2.atk = 500 + int(p2_atk_bonus * 0.2)
        
        # 双方防御力也拉平；天道压制下物法各占一半，保证双轨公平
        player1.defense = 100
        player2.defense = 100
        for stats in (player1, player2):
            stats.physical_atk = stats.atk // 2
            stats.magic_atk = stats.atk - stats.physical_atk
            stats.physical_defense = 50
            stats.magic_defense = 50

        for stats in (player1, player2):
            all_pets = []
            if stats.pet:
                all_pets.append(stats.pet)
            all_pets.extend(stats.support_pets or [])
            for pet in all_pets:
                if pet.hp > 0:
                    pet.atk = 100 + pet.level * 2
                    pet.defense = 50

        combat_log = [
            f"⚖️ 【天道压制】双方基础属性已被强制拉平！",
            f"（但保留了功法、装备、灵兽的特殊加成）",
            f"{player1.name} (ATK {player1.atk}) VS {player2.name} (ATK {player2.atk})",
        ]
        if player2_hp_bonus > 0:
            combat_log.append(
                f"🛡️ {player2.name} 护镖加持，气血 +{int(player2_hp_bonus * 100)}%！"
            )
        combat_log.append("")
        
        round_num = 0
        while player1.hp > 0 and player2.hp > 0 and round_num < 5:  # ★ 修改：最多5回合
            round_num += 1
            combat_log.append(f"-- 第 {round_num} 回合 --")
            
            # 玩家1攻击
            dmg1, logs1, heal1 = cls._execute_attack(player1, player2)
            player2.hp = max(0, player2.hp - dmg1)
            player1.hp = min(player1.max_hp, player1.hp + heal1)
            combat_log.extend(logs1)
            combat_log.append(f"{player2.name} 剩余 HP: {max(0, player2.hp)}")
            
            # 反伤
            counter = (player2.effects or {}).get("counter_damage", 0)
            if counter > 0:
                reflect_dmg = int(dmg1 * (counter / 100))
                player1.hp = max(0, player1.hp - reflect_dmg)
                combat_log.append(f"🛡️ {player2.name} 触发反伤，反弹 {reflect_dmg} 点伤害！")
            
            if player2.hp <= 0: break
            
            # 玩家2攻击
            dmg2, logs2, heal2 = cls._execute_attack(player2, player1)
            player1.hp = max(0, player1.hp - dmg2)
            player2.hp = min(player2.max_hp, player2.hp + heal2)
            combat_log.extend(logs2)
            combat_log.append(f"{player1.name} 剩余 HP: {max(0, player1.hp)}")
            
            # 反伤
            counter1 = (player1.effects or {}).get("counter_damage", 0)
            if counter1 > 0:
                reflect_dmg = int(dmg2 * (counter1 / 100))
                player2.hp = max(0, player2.hp - reflect_dmg)
                combat_log.append(f"🛡️ {player1.name} 触发反伤，反弹 {reflect_dmg} 点伤害！")
                
            combat_log.append("")
        
        # ★ 修改：判定胜负逻辑（打满回合按剩余血量百分比判定）
        was_draw = False
        if player2.hp <= 0 and player1.hp > 0:
            winner = player1.user_id
        elif player1.hp <= 0 and player2.hp > 0:
            winner = player2.user_id
        elif player1.hp <= 0 and player2.hp <= 0:
            winner = "平局"
            was_draw = True
        else:
            p1_ratio = player1.hp / player1.max_hp if player1.max_hp > 0 else 0
            p2_ratio = player2.hp / player2.max_hp if player2.max_hp > 0 else 0
            if p1_ratio > p2_ratio:
                winner = player1.user_id
            elif p2_ratio > p1_ratio:
                winner = player2.user_id
            else:
                winner = "平局"
                was_draw = True

        if was_draw and draw_to_player2:
            winner = player2.user_id

        combat_log = cls._shorten_combat_log(
            combat_log, round_num, winner, player1, player2
        )
        return {
            "winner": winner, 
            "combat_log": combat_log, 
            "rounds": round_num,
            "draw": was_draw,
            "player1_final_hp": 1, "player1_final_mp": 1,
            "player2_final_hp": 1, "player2_final_mp": 1,
            "player1_real_hp": max(0, player1.hp), "player2_real_hp": max(0, player2.hp)
        }
    
    @classmethod
    def player_vs_boss(cls, player: CombatStats, boss: CombatStats) -> Dict:
        """玩家vs Boss战斗 (保持100回合)"""
        combat_log = [f"☆━━━━ Boss战开始 ━━━━☆", f"{player.name} 挑战 {boss.name}"]
        round_num = 0
        total_damage_dealt = 0
        
        while player.hp > 0 and boss.hp > 0 and round_num < 100:
            round_num += 1
            combat_log.append(f"-- 第 {round_num} 回合 --")
            
            dmg, logs, heal = cls._execute_attack(player, boss)
            boss.hp = max(0, boss.hp - dmg)
            total_damage_dealt += dmg
            player.hp = min(player.max_hp, player.hp + heal)
            combat_log.extend(logs)
            combat_log.append(f"{boss.name} 剩余 HP: {max(0, boss.hp)}")
            
            if boss.hp <= 0: break
            
            b_dmg, b_logs, b_heal = cls._execute_attack(boss, player)
            player.hp = max(0, player.hp - b_dmg)
            combat_log.extend(b_logs)
            combat_log.append(f"{player.name} 剩余 HP: {max(0, player.hp)}")
            combat_log.append("")
            
        if boss.hp <= 0:
            winner = player.user_id
            reward = boss.exp
        elif player.hp <= 0:
            winner = boss.user_id
            reward = int(boss.exp * (total_damage_dealt / boss.max_hp))
        else:
            winner = "平局"
            reward = 0
            
        return {
            "winner": winner, "combat_log": combat_log, "rounds": round_num,
            "player_final_hp": max(1, player.hp), "player_final_mp": player.mp,
            "boss_final_hp": max(0, boss.hp), "reward": reward
        }