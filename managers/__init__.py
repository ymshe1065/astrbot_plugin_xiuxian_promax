# managers/__init__.py

# 基础系统
from .combat_manager import CombatManager, CombatStats
from .sect_manager import SectManager
from .boss_manager import BossManager
from .rift_manager import RiftManager
from .ranking_manager import RankingManager
from .adventure_manager import AdventureManager
from .alchemy_manager import AlchemyManager
from .impart_manager import ImpartManager
from .bank_manager import BankManager
from .bounty_manager import BountyManager
from .impart_pk_manager import ImpartPkManager

# Phase 4 系统
from .blessed_land_manager import BlessedLandManager
from .spirit_farm_manager import SpiritFarmManager
from .dual_cultivation_manager import DualCultivationManager
from .spirit_eye_manager import SpiritEyeManager

# v6.1 系统
from .daily_task_manager import DailyTaskManager
from .world_boss_manager import WorldBossManager
from .raid_manager import RaidManager
from .robbery_manager import RobberyManager
from .tutorial_manager import TutorialManager
from .reincarnation_manager import ReincarnationManager

# v6.2 - v6.5 系统
from .gamble_manager import GambleManager
from .forge_manager import ForgeManager
from .global_event_manager import GlobalEventManager
from .demon_invasion_manager import DemonInvasionManager
from .pet_manager import PetManager

# v6.6 交易坊市
from .auction_manager import AuctionManager

# v6.8 押镖劫道
from .escort_manager import EscortManager
# v6.8 奇遇系统
from .encounter_manager import EncounterManager
# v6.10 生活职业
from .life_skill_manager import LifeSkillManager
# 气运系统
from .luck_manager import LuckManager
# 符箓体系
from .talisman_manager import TalismanManager
# v6.10 魔潮天灾
from .calamity_manager import CalamityManager
# v6.26 自动代理
from .auto_proxy_manager import AutoProxyManager

__all__ = [
    "CombatManager", "CombatStats", "SectManager", "BossManager", "RiftManager",
    "RankingManager", "AdventureManager", "AlchemyManager", "ImpartManager",
    "BankManager", "BountyManager", "ImpartPkManager",
    "BlessedLandManager", "SpiritFarmManager", "DualCultivationManager", "SpiritEyeManager",
    "DailyTaskManager", "WorldBossManager", "RaidManager", "RobberyManager", "TutorialManager", "ReincarnationManager",
    "GambleManager", "ForgeManager", "GlobalEventManager", "DemonInvasionManager", "PetManager",
    "AuctionManager", "EscortManager", "EncounterManager", "LifeSkillManager", "TalismanManager", "CalamityManager", "LuckManager",
    "AutoProxyManager"
]