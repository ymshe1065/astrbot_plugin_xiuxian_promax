# models.py

from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional
import json

if TYPE_CHECKING:
    from .config_manager import ConfigManager

@dataclass
class Item:
    """装备物品模型"""

    item_id: str  # 物品唯一ID
    name: str  # 物品名称
    item_type: str  # 装备类型：weapon（武器）、armor（防具）、main_technique（主修心法）、technique（功法）
    description: str = ""  # 物品描述

    # 装备品级相关
    rank: str = ""  # 品级：凡品、灵品、地品、天品、皇品、帝品、道品、仙品、混元先天
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

    def get_attribute_display(self) -> str:
        """获取属性加成的显示文本"""
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
    mp: int = 0  # 当前真元值
    atk: int = 0  # 攻击力
    atkpractice: int = 0  # 攻击修炼等级，每级提升4%攻击力

    # 灵修/体修专用属性
    spiritual_qi: int = 100  # 当前灵气（灵修专用）
    max_spiritual_qi: int = 1000  # 最大灵气容量（灵修专用）
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
    sect_task: int = 0  # 宗门任务完成次数
    sect_elixir_get: int = 0  # 宗门丹药领取标记（0未领取，1已领取）
    sect_debt: int = 0  # 宗门命令欠款（灵石）

    # 洞天福地系统
    blessed_spot_flag: int = 0  # 是否开启洞天福地（0未开启，1已开启）
    blessed_spot_name: str = ""  # 洞天福地名称

    # 丹药系统字段
    active_pill_effects: str = "[]"  # 当前生效的临时丹药效果（JSON字符串）
    permanent_pill_gains: str = "{}"  # 永久丹药累积增益（JSON字符串）
    has_resurrection_pill: bool = False  # 是否拥有回生丹效果
    has_debuff_shield: bool = False  # 是否拥有一次负面效果免疫
    pills_inventory: str = "{}"  # 丹药背包（JSON字符串，格式：{pill_id: count}）

    # 储物戒系统字段
    storage_ring: str = "基础储物戒"  # 当前装备的储物戒名称
    storage_ring_items: str = "{}"  # 储物戒中的物品（JSON字符串，格式：{item_name: {count, bound}}）

    # Phase 1: 每日限制系统
    daily_pill_usage: str = "{}"  # 每日丹药使用次数（JSON字符串，格式：{pill_id: count}）
    last_daily_reset: str = ""  # 上次每日重置日期（格式：YYYY-MM-DD）

    # 护府令过期时间（Unix时间戳，0表示未开启）
    shield_expire_time: int = 0

    # 新手引导与转生
    tutorial_step: int = 0  # 新手引导进度 (0-4)
    reincarnation_count: int = 0  # 转生次数
    fate_points: int = 0  # 仙缘点
    permanent_bonus: str = "{}"  # 转生永久加成 (JSON)

    # v6.10 功德因果与生活职业
    karma: int = 0  # 功德/业力（正为功德，负为业力）
    life_skill: str = ""  # 生活职业：炼器师/符箓师/阵法师

    # v6.16 师徒系统
    virtue_points: int = 0  # 师德点（徒弟突破时获得，可兑换奖励）

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

    def get_techniques_list(self) -> List[str]:
        """获取功法列表"""
        try:
            return json.loads(self.techniques)
        except json.JSONDecodeError:
            return []

    def set_techniques_list(self, techniques_list: List[str]):
        """设置功法列表"""
        self.techniques = json.dumps(techniques_list, ensure_ascii=False)

    def get_active_pill_effects(self) -> List[dict]:
        """获取当前生效的临时丹药效果列表"""
        try:
            return json.loads(self.active_pill_effects)
        except json.JSONDecodeError:
            return []

    def set_active_pill_effects(self, effects: List[dict]):
        """设置当前生效的临时丹药效果"""
        self.active_pill_effects = json.dumps(effects, ensure_ascii=False)

    def get_permanent_pill_gains(self) -> dict:
        """获取永久丹药累积增益"""
        try:
            return json.loads(self.permanent_pill_gains)
        except json.JSONDecodeError:
            return {}

    def set_permanent_pill_gains(self, gains: dict):
        """设置永久丹药累积增益"""
        self.permanent_pill_gains = json.dumps(gains, ensure_ascii=False)

    def get_pills_inventory(self) -> dict:
        """获取丹药背包"""
        try:
            return json.loads(self.pills_inventory)
        except json.JSONDecodeError:
            return {}

    def set_pills_inventory(self, inventory: dict):
        """设置丹药背包"""
        self.pills_inventory = json.dumps(inventory, ensure_ascii=False)

    def get_storage_ring_items(self) -> dict:
        """获取储物戒物品"""
        try:
            return json.loads(self.storage_ring_items)
        except json.JSONDecodeError:
            return {}

    def set_storage_ring_items(self, items: dict):
        """设置储物戒物品"""
        self.storage_ring_items = json.dumps(items, ensure_ascii=False)

    def get_total_attributes(self, equipped_items: List[Item], pill_multipliers: Optional[dict] = None) -> dict:
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