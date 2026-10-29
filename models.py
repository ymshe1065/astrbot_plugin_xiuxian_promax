# models.py

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional
import json

if TYPE_CHECKING:
    from .config_manager import ConfigManager

SHIELD_OUTPUT_PENALTY = 0.2
ARRAY_MASTER_SHIELD_OUTPUT_PENALTY = 0.1

@dataclass
class Item:
    """装备物品模型"""

    item_id: str  # 物品唯一ID
    name: str  # 物品名称
    item_type: str  # 装备类型：weapon（武器）、armor（防具）、main_technique（主修心法）、technique（功法）
    description: str = ""  # 物品描述

    # 装备品级相关
    rank: str = ""  # 统一品阶：凡品、黄品、玄品、灵品、地品、天品、皇品、圣品、帝品、道品、仙品、混元先天
    required_level_index: int = 0  # 需要的最低境界level_index
    weapon_category: str = ""  # 武器类别：剑、刀、阔刀、琴、匕首、符箓、鼎、棍、枪、笔

    # 装备属性加成
    magic_damage: int = 0  # 法伤加成
    physical_damage: int = 0  # 物伤加成
    magic_defense: int = 0  # 法防加成
    physical_defense: int = 0  # 物防加成
    mental_power: int = 0  # 精神力加成

    # 心法专属属性
    exp_multiplier: float = 0.0  # 修为倍率加成（仅心法有效）
    spiritual_qi: int = 0  # 灵气加成（仅心法有效，灵修）
    blood_qi: int = 0  # 气血加成（仅心法有效，体修）

    equipment_rates: dict = field(default_factory=dict)  # 装备主属性百分比
    life_effects: dict = field(default_factory=dict)  # 普通功法生活加成

    def get_attribute_display(self) -> str:
        """获取属性加成的显示文本"""
        if self.equipment_rates:
            from .core.equipment_balance import ATTRIBUTE_LABELS
            return "、".join(f"境界基础{ATTRIBUTE_LABELS.get(k, k)}+{v:.1%}" for k, v in self.equipment_rates.items() if v)
        attrs = []
        if self.magic_damage > 0:
            attrs.append(f"法伤+{self.magic_damage}")
        if self.physical_damage > 0:
            attrs.append(f"物伤+{self.physical_damage}")
        if self.magic_defense > 0:
            attrs.append(f"法防+{self.magic_defense}")
        if self.physical_defense > 0:
            attrs.append(f"物防+{self.physical_defense}")
        if self.mental_power > 0:
            attrs.append(f"精神力+{self.mental_power}")
        if self.exp_multiplier > 0:
            attrs.append(f"修为倍率+{self.exp_multiplier:.1%}")
        if self.spiritual_qi > 0:
            attrs.append(f"灵气+{self.spiritual_qi}")
        if self.blood_qi > 0:
            attrs.append(f"气血+{self.blood_qi}")
        from .core.technique_bonus import LIFE_EFFECT_LABELS
        for key, value in (self.life_effects or {}).items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number <= 0:
                continue
            label = LIFE_EFFECT_LABELS.get(key, key)
            attrs.append(f"{label}+{int(number)}" if key == "luck_bonus" else f"{label}+{number:.1%}")
        return "、".join(attrs) if attrs else "无属性加成"

@dataclass
class Player:
    """玩家数据模型 - 完整修仙系统"""

    user_id: str
    level_index: int = 0
    spiritual_root: str = "未知"
    cultivation_type: str = "灵修"  # 灵修或体修等
    user_name: str = ""  # 道号
    title: str = ""  # 称号（如：【灭世魔神】）

    # 基础属性
    lifespan: int = 100  # 寿命
    experience: int = 0  # 修为
    gold: int = 0  # 灵石
    state: str = "空闲"
    cultivation_start_time: int = 0  # 闭关开始时间（Unix时间戳，0表示未闭关）
    last_check_in_date: str = ""  # 最后签到日期（格式：YYYY-MM-DD，空字符串表示从未签到）
    level_up_rate: int = 0  # 突破成功率加成

    # 装备栏
    weapon: str = ""  # 武器
    armor: str = ""  # 防具
    main_technique: str = ""  # 主修心法
    techniques: str = "[]"  # 功法列表（JSON字符串，最多3个）

    # 战斗属性（HP/MP/ATK系统）
    hp: int = 0  # 当前气血值
    mp: int = 0  # 灵气兼容别名，正式数据为 spiritual_qi
    atk: int = 0  # 攻击力
    atkpractice: int = 0  # 攻击修炼等级，每级提升4%攻击力

    # 灵修/体修专用属性
    spiritual_qi: int = 100  # 当前灵气（统一资源）
    max_spiritual_qi: int = 1000  # 最大灵气容量
    blood_qi: int = 0  # 当前气血（体修专用）
    max_blood_qi: int = 0  # 最大气血容量（体修专用）
    magic_damage: int = 10  # 法伤
    physical_damage: int = 10  # 物伤
    magic_defense: int = 5  # 法防
    physical_defense: int = 5  # 物防
    mental_power: int = 100  # 精神力

    # 宗门系统字段
    sect_id: int = 0  # 宗门ID（0表示未加入宗门）
    sect_position: int = 4  # 宗门职位：0宗主、1长老、2亲传、3内门、4外门
    sect_contribution: int = 0  # 宗门贡献度
    sect_task: int = 0  # 宗门任务累计完成次数
    sect_elixir_get: int = 0  # 旧版宗门丹药领取标记（兼容旧库，当前玩法未使用）
    sect_debt: int = 0  # 宗门命令欠款（灵石）

    # 洞天福地系统
    blessed_spot_flag: int = 0  # 是否开启洞天福地（0未开启，1已开启）
    blessed_spot_name: str = ""  # 洞天福地名称

    # 丹药系统字段
    active_pill_effects: str = "[]"  # 当前生效的临时丹药效果（JSON字符串）
    permanent_pill_gains: str = "{}"  # 永久丹药累积增益（JSON字符串）
    has_resurrection_pill: bool = False  # 是否拥有回生丹效果
    resurrection_pill_quality: str = "一般"  # 回生丹品质（决定复活后属性保留比例）
    has_debuff_shield: bool = False  # 是否拥有一次负面效果免疫
    pills_inventory: str = "{}"  # 丹药背包（JSON字符串，格式：{"丹药名|品质": count}）
    learned_recipes: str = "[]"  # 已学习丹方ID（JSON数组）

    # 储物戒系统字段
    storage_ring: str = "基础储物戒"  # 当前装备的储物戒名称
    storage_ring_items: str = "{}"  # 储物戒中的物品（JSON字符串，格式：{item_name: count}）
    overflow_items: str = "{}"  # 储物戒满时暂存的溢出物品（JSON字符串，格式：{item_name: count}）
    equipment_instances: str = "[]"  # 装备实例列表（JSON数组）
    equipped_instances: str = "{}"  # 已装备实例槽位映射（JSON对象）

    # Phase 1: 每日限制系统
    daily_pill_usage: str = "{}"  # 每日丹药使用次数（JSON字符串，格式：{pill_id: count}）
    last_daily_reset: str = ""  # 上次每日重置日期（格式：YYYY-MM-DD）

    # 护府令过期时间（Unix时间戳，0表示未开启）
    shield_start_time: int = 0
    shield_expire_time: int = 0

    # 新手引导与转生
    tutorial_step: int = 0  # 新手引导进度 (0-4)
    reincarnation_count: int = 0  # 转生次数
    fate_points: int = 0  # 仙缘点
    permanent_bonus: str = "{}"  # 转生永久加成 (JSON)

    # v6.10 功德因果与生活职业
    karma: int = 0  # 功德/业力（正为功德，负为业力）
    luck: int = 0  # 气运（本体气运，可来自奇遇/称号/词条/符箓）
    life_skill: str = ""  # 生活职业：炼器师/符箓师/阵法师

    # v6.16 师徒系统
    virtue_points: int = 0  # 师德点（徒弟突破时获得，可兑换奖励）

    def shield_output_multiplier(self, period_start: int, period_end: int) -> float:
        """护府大阵生效期间产出系数（按时间重叠比例计算）"""
        if self.shield_expire_time <= 0 or self.shield_start_time <= 0:
            return 1.0
        penalty = ARRAY_MASTER_SHIELD_OUTPUT_PENALTY if self.life_skill == "阵法师" else SHIELD_OUTPUT_PENALTY
        overlap_start = max(int(period_start), int(self.shield_start_time))
        overlap_end = min(int(period_end), int(self.shield_expire_time))
        overlap = max(0, overlap_end - overlap_start)
        duration = max(1, int(period_end) - int(period_start))
        return max(1.0 - penalty, 1.0 - penalty * overlap / duration)

    def get_level(self, config_manager: "ConfigManager") -> str:
        """获取境界名称"""
        level_data = config_manager.get_level_data(self.cultivation_type)
        if 0 <= self.level_index < len(level_data):
            return level_data[self.level_index]["level_name"]
        return "未知境界"

    def get_required_exp(self, config_manager: "ConfigManager") -> int:
        """获取突破到下一境界所需的总修为"""
        level_data = config_manager.get_level_data(self.cultivation_type)
        if self.level_index + 1 < len(level_data):
            return level_data[self.level_index + 1].get("exp_needed", 0)
        return 0

    @staticmethod
    def _load_json_field(raw, default):
        """解析 JSON 字段；NULL、非法 JSON、类型不符时返回 default。"""
        if raw is None:
            return default
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return default
        return data if isinstance(data, type(default)) else default

    def get_techniques_list(self) -> List[str]:
        """获取功法列表"""
        return self._load_json_field(self.techniques, [])

    def set_techniques_list(self, techniques_list: List[str]):
        """设置功法列表"""
        self.techniques = json.dumps(techniques_list, ensure_ascii=False)

    def get_active_pill_effects(self) -> List[dict]:
        """获取当前生效的临时丹药效果列表"""
        return self._load_json_field(self.active_pill_effects, [])

    def set_active_pill_effects(self, effects: List[dict]):
        """设置当前生效的临时丹药效果"""
        self.active_pill_effects = json.dumps(effects, ensure_ascii=False)

    def get_permanent_pill_gains(self) -> dict:
        """获取永久丹药累积增益"""
        return self._load_json_field(self.permanent_pill_gains, {})

    def set_permanent_pill_gains(self, gains: dict):
        """设置永久丹药累积增益"""
        self.permanent_pill_gains = json.dumps(gains, ensure_ascii=False)

    def get_pills_inventory(self) -> dict:
        """获取丹药背包"""
        return self._load_json_field(self.pills_inventory, {})

    def set_pills_inventory(self, inventory: dict):
        """设置丹药背包"""
        self.pills_inventory = json.dumps(inventory, ensure_ascii=False)

    def get_storage_ring_items(self) -> dict:
        """获取储物戒物品"""
        return self._load_json_field(self.storage_ring_items, {})

    def set_storage_ring_items(self, items: dict):
        """设置储物戒物品"""
        self.storage_ring_items = json.dumps(items, ensure_ascii=False)

    def get_overflow_items(self) -> dict:
        """获取溢出仓库物品"""
        return self._load_json_field(self.overflow_items, {})

    def set_overflow_items(self, items: dict):
        """设置溢出仓库物品"""
        self.overflow_items = json.dumps(items, ensure_ascii=False)

    def get_equipment_instances(self) -> list:
        """获取装备实例列表"""
        return self._load_json_field(self.equipment_instances, [])

    def set_equipment_instances(self, instances: list):
        self.equipment_instances = json.dumps(instances, ensure_ascii=False)

    def get_equipped_instances(self) -> dict:
        """获取槽位 -> 实例ID映射"""
        return self._load_json_field(self.equipped_instances, {})

    def set_equipped_instances(self, mapping: dict):
        self.equipped_instances = json.dumps(mapping, ensure_ascii=False)

    def get_total_attributes(self, equipped_items: List[Item], pill_multipliers: Optional[dict] = None, base_attributes: Optional[dict] = None) -> dict:
        """计算包含装备加成和丹药效果的总属性"""
        total = {
            "spiritual_qi": self.spiritual_qi,
            "max_spiritual_qi": self.max_spiritual_qi,
            "blood_qi": self.blood_qi,
            "max_blood_qi": self.max_blood_qi,
            "magic_damage": self.magic_damage,
            "physical_damage": self.physical_damage,
            "magic_defense": self.magic_defense,
            "physical_defense": self.physical_defense,
            "mental_power": self.mental_power,
            "exp_multiplier": 0.0,
        }

        if base_attributes:
            for key in ("physical_damage", "magic_damage", "physical_defense", "magic_defense", "mental_power"):
                total[key] = base_attributes[key]
        seen_techniques = set()
        for item in equipped_items:
            total["magic_damage"] += item.magic_damage
            total["physical_damage"] += item.physical_damage
            total["magic_defense"] += item.magic_defense
            total["physical_defense"] += item.physical_defense
            total["mental_power"] += item.mental_power

            if (
                item.item_type in ("main_technique", "technique")
                and item.name not in seen_techniques
            ):
                seen_techniques.add(item.name)
                total["exp_multiplier"] += item.exp_multiplier
                total["max_spiritual_qi"] += item.spiritual_qi
                total["max_blood_qi"] += item.blood_qi

        if pill_multipliers:
            total["physical_damage"] = int(total["physical_damage"] * pill_multipliers.get("physical_damage", 1.0))
            total["magic_damage"] = int(total["magic_damage"] * pill_multipliers.get("magic_damage", 1.0))
            total["physical_defense"] = int(total["physical_defense"] * pill_multipliers.get("physical_defense", 1.0))
            total["magic_defense"] = int(total["magic_defense"] * pill_multipliers.get("magic_defense", 1.0))

        return total