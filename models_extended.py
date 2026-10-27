# models_extended.py - 新增模型定义

from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, List, Optional
import json

if TYPE_CHECKING:
    from .config_manager import ConfigManager


class UserStatus(IntEnum):
    """用户状态枚举"""
    IDLE = 0           # 空闲
    CULTIVATING = 1    # 闭关中
    ADVENTURING = 2    # 历练中
    EXPLORING = 3      # 探索秘境中
    SECT_TASK = 4      # 宗门任务中
    TRIBULATION = 5    # 渡劫中 (新增)
    DELIVERING = 6     # 押镖中 (新增)
    RAIDING = 7        # 掠夺中 (新增)
    SECT_BATTLE = 8    # 宗门大比中 (新增)
    RESTING = 9        # 调息中

    @classmethod
    def get_name(cls, status: int) -> str:
        """获取状态名称"""
        names = {
            cls.IDLE: "空闲",
            cls.CULTIVATING: "闭关中",
            cls.ADVENTURING: "历练中",
            cls.EXPLORING: "探索秘境中",
            cls.SECT_TASK: "宗门任务中",
            cls.TRIBULATION: "渡劫中",
            cls.DELIVERING: "押镖中",
            cls.RAIDING: "掠夺中",
            cls.SECT_BATTLE: "宗门大比中",
            cls.RESTING: "调息中",
        }
        return names.get(status, "忙碌中")

@dataclass
class Sect:
    """宗门数据模型"""
    sect_id: int
    sect_name: str
    sect_owner: str
    sect_scale: int = 0
    sect_used_stone: int = 0
    # sect_fairyland/mainbuff/secbuff/elixir_room_level 为旧版兼容字段，当前玩法不参与结算
    sect_fairyland: int = 0
    sect_materials: int = 0
    mainbuff: str = "0"
    secbuff: str = "0"
    elixir_room_level: int = 0
    buildings: str = "{}"  # 宗门建筑等级 (JSON)
    sect_level: int = 1
    sect_exp: int = 0
    sect_war_cd_until: int = 0
    sect_war_protect_until: int = 0
    sect_tournament_cd_until: int = 0
    
    def get_mainbuff_list(self) -> List[int]:
        try:
            if self.mainbuff == "0" or not self.mainbuff:
                return []
            return json.loads(self.mainbuff) if isinstance(self.mainbuff, str) else [self.mainbuff]
        except:
            return []
    
    def set_mainbuff_list(self, buff_list: List[int]):
        self.mainbuff = json.dumps(buff_list, ensure_ascii=False) if buff_list else "0"
    
    def get_secbuff_list(self) -> List[int]:
        try:
            if self.secbuff == "0" or not self.secbuff:
                return []
            return json.loads(self.secbuff) if isinstance(self.secbuff, str) else [self.secbuff]
        except:
            return []
    
    def set_secbuff_list(self, buff_list: List[int]):
        self.secbuff = json.dumps(buff_list, ensure_ascii=False) if buff_list else "0"

@dataclass
class BuffInfo:
    """Buff信息数据模型"""
    id: int
    user_id: str
    main_buff: int = 0
    sec_buff: int = 0
    faqi_buff: int = 0
    fabao_weapon: int = 0
    armor_buff: int = 0
    atk_buff: int = 0
    blessed_spot: int = 0
    sub_buff: int = 0

@dataclass
class Boss:
    """Boss数据模型"""
    boss_id: int
    boss_name: str
    boss_level: str
    hp: int
    max_hp: int
    atk: int
    defense: int = 0
    stone_reward: int = 0
    create_time: int = 0
    status: int = 1
    archetype: str = "balanced"

@dataclass
class Rift:
    """秘境数据模型"""
    rift_id: int
    rift_name: str
    rift_level: int
    required_level: int
    rewards: str = "{}"
    
    def get_rewards(self) -> dict:
        try:
            return json.loads(self.rewards)
        except:
            return {}
    
    def set_rewards(self, rewards_dict: dict):
        self.rewards = json.dumps(rewards_dict, ensure_ascii=False)

@dataclass
class ImpartInfo:
    """传承信息数据模型"""
    id: int
    user_id: str
    impart_hp_per: float = 0.0
    impart_mp_per: float = 0.0
    impart_atk_per: float = 0.0
    impart_know_per: float = 0.0
    impart_burst_per: float = 0.0

@dataclass
class UserCd:
    """用户CD信息数据模型"""
    user_id: str
    type: int = UserStatus.IDLE
    create_time: int = 0
    scheduled_time: int = 0
    extra_data: str = "{}"
    
    def get_extra_data(self) -> dict:
        try:
            return json.loads(self.extra_data)
        except:
            return {}
    
    def set_extra_data(self, data: dict):
        self.extra_data = json.dumps(data, ensure_ascii=False)

# ===== v6.1 新增模型 =====

@dataclass
class DailyTask:
    """日常任务模型"""
    task_id: int
    user_id: str
    task_level: str  # 黄/玄/地/天
    title: str
    target_type: str
    target_count: int
    current_progress: int
    rewards_json: str
    status: int  # 1进行中, 2已完成, 3已领奖/过期, 4已放弃
    expire_time: int

@dataclass
class WorldBoss:
    """世界Boss模型"""
    boss_id: int
    boss_name: str
    boss_level: int  # 1=凡界场, 2=神域场
    max_hp: int
    current_hp: int
    atk: int
    defense: int
    spawn_time: int
    expire_time: int
    status: int  # 1存活, 2击杀, 3逃跑