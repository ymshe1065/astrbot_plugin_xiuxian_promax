# handlers/__init__.py

# 基础指令
from .player_handler import PlayerHandler
from .misc_handler import MiscHandler
from .equipment_handler import EquipmentHandler
from .breakthrough_handler import BreakthroughHandler
from .pill_handler import PillHandler
from .shop_handler import ShopHandler
from .storage_ring_handler import StorageRingHandler

# 宗门与战斗
from .sect_handlers import SectHandlers
from .boss_handlers import BossHandlers
from .combat_handlers import CombatHandlers
from .ranking_handlers import RankingHandlers

# 探索与副业
from .rift_handlers import RiftHandlers
from .adventure_handlers import AdventureHandlers
from .alchemy_handlers import AlchemyHandlers
from .impart_handlers import ImpartHandlers
from .nickname_handler import NicknameHandler

# 经济与特殊系统
from .bank_handlers import BankHandlers
from .bounty_handlers import BountyHandlers
from .impart_pk_handlers import ImpartPkHandlers
from .blessed_land_handlers import BlessedLandHandlers
from .spirit_farm_handlers import SpiritFarmHandlers
from .dual_cultivation_handlers import DualCultivationHandlers
from .spirit_eye_handlers import SpiritEyeHandlers

# v6.1 核心扩展
from .daily_task_handlers import DailyTaskHandlers
from .world_boss_handlers import WorldBossHandlers
from .robbery_handlers import RobberyHandlers
from .reincarnation_handlers import ReincarnationHandlers

# v6.2 - v6.5 扩展
from .gamble_handlers import GambleHandlers
from .forge_handlers import ForgeHandlers
from .demon_invasion_handlers import DemonInvasionHandlers
from .pet_handlers import PetHandlers

# v6.6 交易坊市
from .auction_handlers import AuctionHandlers

# 宗门贡献商店
from .sect_shop_handler import SectShopHandler

__all__ = [
    "PlayerHandler", "MiscHandler", "EquipmentHandler", "BreakthroughHandler", 
    "PillHandler", "ShopHandler", "StorageRingHandler", "SectHandlers", 
    "BossHandlers", "CombatHandlers", "RankingHandlers", "RiftHandlers", 
    "AdventureHandlers", "AlchemyHandlers", "ImpartHandlers", "NicknameHandler", 
    "BankHandlers", "BountyHandlers", "ImpartPkHandlers", "BlessedLandHandlers", 
    "SpiritFarmHandlers", "DualCultivationHandlers", "SpiritEyeHandlers", 
    "DailyTaskHandlers", "WorldBossHandlers", "RobberyHandlers", "ReincarnationHandlers", 
    "GambleHandlers", "ForgeHandlers", "DemonInvasionHandlers", "PetHandlers",
    "AuctionHandlers", "SectShopHandler"
]