import asyncio
import json
import time
import random
from datetime import datetime
from functools import wraps
from pathlib import Path
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.filter.command import GreedyStr
from .data import DataBase, MigrationManager
from .config_manager import ConfigManager
from .handlers.utils import with_player_lock, idle_player_lock, get_player_lock, get_event_flag
from .handlers import (
    MiscHandler, PlayerHandler, EquipmentHandler, BreakthroughHandler, 
    PillHandler, ShopHandler, StorageRingHandler,
    SectHandlers, BossHandlers, CombatHandlers, RankingHandlers,
    RiftHandlers, AdventureHandlers, AlchemyHandlers, ImpartHandlers,
    NicknameHandler, BankHandlers, BountyHandlers, ImpartPkHandlers,
    BlessedLandHandlers, SpiritFarmHandlers, DualCultivationHandlers, SpiritEyeHandlers,
    DailyTaskHandlers, WorldBossHandlers, RaidHandlers, RobberyHandlers, ReincarnationHandlers,
    GambleHandlers, ForgeHandlers, DemonInvasionHandlers, PetHandlers,
    AuctionHandlers, AuctionHouseHandlers, SectShopHandler, AutoProxyHandlers, BattleEventHandlers
)
from .managers import (
    CombatManager, SectManager, BossManager, RiftManager,
    RankingManager, AdventureManager, AlchemyManager, ImpartManager,
    BankManager, BountyManager, ImpartPkManager,
    BlessedLandManager, SpiritFarmManager, DualCultivationManager, SpiritEyeManager,
    DailyTaskManager, WorldBossManager, RaidManager, RobberyManager, ReincarnationManager,
    TutorialManager, GambleManager, ForgeManager, GlobalEventManager,
    DemonInvasionManager, PetManager,
    AuctionManager, AuctionHouseManager, EscortManager, EncounterManager, LifeSkillManager, TalismanManager, CalamityManager,
    AutoProxyManager, BattleEventManager
)
from .models_extended import UserStatus


def require_whitelist(func):
    """装饰器：检查群聊白名单权限"""
    @wraps(func)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        if not self._check_access(event):
            await self._send_access_denied_message(event)
            return
        async for result in func(self, event, *args, **kwargs):
            yield result
    return wrapper

# 指令定义
CMD_HELP = "修仙帮助"
CMD_START_XIUXIAN = "我要修仙"
CMD_PLAYER_INFO = "我的信息"
CMD_START_CULTIVATION = "闭关"
CMD_END_CULTIVATION = "出关"
CMD_CHECK_IN = "签到"
CMD_SHOW_EQUIPMENT = "我的装备"
CMD_EQUIP_ITEM = "装备"
CMD_UNEQUIP_ITEM = "卸下"
CMD_BREAKTHROUGH = "突破"
CMD_BREAKTHROUGH_INFO = "突破信息"
CMD_USE_PILL = "服用丹药"
CMD_SHOW_PILLS = "丹药背包"
CMD_PILL_INFO = "丹药信息"
CMD_PILL_PAVILION = "丹阁"
CMD_WEAPON_PAVILION = "器阁"
CMD_TREASURE_PAVILION = "百宝阁"
CMD_ITEM_INFO = "物品信息"
CMD_BUY = "购买"
CMD_STORAGE_RING = "储物戒"
CMD_RETRIEVE_ITEM = "取出"
CMD_UPGRADE_RING = "更换储物戒"
CMD_DISCARD_ITEM = "丢弃"
CMD_GIFT_ITEM = "赠予"
CMD_ACCEPT_GIFT = "接收"
CMD_REJECT_GIFT = "拒绝"
CMD_SEARCH_ITEM = "搜索物品"
CMD_RETRIEVE_ALL = "取出所有"
CMD_CREATE_SECT = "创建宗门"
CMD_JOIN_SECT = "加入宗门"
CMD_LEAVE_SECT = "退出宗门"
CMD_MY_SECT = "我的宗门"
CMD_SECT_LIST = "宗门列表"
CMD_SECT_DONATE = "宗门捐献"
CMD_SECT_KICK = "踢出成员"
CMD_SECT_TRANSFER = "宗主传位"
CMD_SECT_TASK = "宗门任务"
CMD_FINISH_SECT_TASK = "完成宗门任务"
CMD_SECT_POSITION = "职位变更"
CMD_BOSS_INFO = "世界Boss"
CMD_BOSS_FIGHT = "挑战Boss"
CMD_SPAWN_BOSS = "生成Boss"
CMD_RANK_LEVEL = "境界排行"
CMD_RANK_POWER = "战力排行"
CMD_RANK_WEALTH = "灵石排行"
CMD_RANK_SECT = "宗门排行"
CMD_RANK_DEPOSIT = "存款排行"
CMD_RANK_CONTRIBUTION = "贡献排行"
CMD_DUEL = "决斗"
CMD_SPAR = "切磋"
CMD_RIFT_LIST = "秘境列表"
CMD_RIFT_EXPLORE = "探索秘境"
CMD_RIFT_COMPLETE = "完成探索"
CMD_RIFT_EXIT = "退出秘境"
CMD_ADVENTURE_START = "开始历练"
CMD_ADVENTURE_COMPLETE = "完成历练"
CMD_ADVENTURE_STATUS = "历练状态"
CMD_ADVENTURE_INFO = "历练信息"
CMD_ALCHEMY_RECIPES = "丹药配方"
CMD_RECIPE_BOOK = "丹方"
CMD_LEARN_RECIPE = "学习丹方"
CMD_ALCHEMY_CRAFT = "炼丹"
CMD_IMPART_INFO = "传承信息"
CMD_CHANGE_NICKNAME = "改道号"
CMD_BANK_INFO = "银行"
CMD_BANK_DEPOSIT = "存灵石"
CMD_BANK_WITHDRAW = "取灵石"
CMD_BANK_INTEREST = "领取利息"
CMD_BANK_LOAN = "贷款"
CMD_BANK_REPAY = "还款"
CMD_BANK_TRANSACTIONS = "银行流水"
CMD_BANK_BREAKTHROUGH_LOAN = "突破贷款"
CMD_BOUNTY_LIST = "悬赏令"
CMD_BOUNTY_ACCEPT = "接取悬赏"
CMD_BOUNTY_STATUS = "悬赏状态"
CMD_BOUNTY_COMPLETE = "完成悬赏"
CMD_BOUNTY_ABANDON = "放弃悬赏"
CMD_IMPART_CHALLENGE = "传承挑战"
CMD_IMPART_RANKING = "传承排行"
CMD_BLESSED_LAND_INFO = "我的洞天"
CMD_BLESSED_LAND_BUY = "购买洞天"
CMD_BLESSED_LAND_UPGRADE = "升级洞天"
CMD_BLESSED_LAND_COLLECT = "洞天收取"
CMD_BLESSED_LAND_ADVANCE = "进阶洞天"
CMD_SPIRIT_FARM_INFO = "我的灵田"
CMD_SPIRIT_FARM_CREATE = "开垦灵田"
CMD_SPIRIT_FARM_PLANT = "种植"
CMD_SPIRIT_FARM_HARVEST = "收获"
CMD_SPIRIT_FARM_REMOVE = "铲除"
CMD_SPIRIT_FARM_UPGRADE = "升级灵田"
CMD_DUAL_CULT_REQUEST = "双修"
CMD_DUAL_CULT_ACCEPT = "接受双修"
CMD_DUAL_CULT_REJECT = "拒绝双修"
CMD_SPIRIT_EYE_INFO = "灵眼信息"
CMD_SPIRIT_EYE_CLAIM = "抢占灵眼"
CMD_SPIRIT_EYE_COLLECT = "灵眼收取"
CMD_SPIRIT_EYE_RELEASE = "释放灵眼"
CMD_REBIRTH = "弃道重修"
CMD_AUTO_PROXY = "自动代理"
CMD_AUTO_PROXY_OFF = "关闭代理"
CMD_AUTO_PROXY_STATUS = "代理状态"
CMD_BATTLE_EVENT_ATTACK = "讨伐"
CMD_BATTLE_EVENT_STATUS = "江湖事件"
CMD_BATTLE_EVENT_SPAWN = "生成江湖事件"

class XiuXianPlugin(Star):
    """修仙插件 - 文字修仙游戏"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        _current_dir = Path(__file__).parent
        self.config_manager = ConfigManager(_current_dir)

        files_config = self.config.get("FILES", {})
        db_filename = files_config.get("DATABASE_FILE", "xiuxian_data_v2.db")
        plugin_data_path = StarTools.get_data_dir("astrbot_plugin_xiuxian_promax")
        plugin_data_path.mkdir(parents=True, exist_ok=True)

        # 项目改名迁移：若历史旧目录有数据而新目录为空，自动整体迁移（按时间从新到旧尝试）
        try:
            import shutil as _shutil
            for old_name in ("astrbot_plugin_xiuxian", "astrbot_plugin_monixiuxian2"):
                old_data_path = plugin_data_path.parent / old_name
                if old_data_path.exists() and not any(plugin_data_path.iterdir()):
                    _shutil.copytree(old_data_path, plugin_data_path, dirs_exist_ok=True)
                    logger.info(f"【修仙插件】已将旧数据目录 {old_name} 迁移至 {plugin_data_path.name}")
                    break
        except Exception as e:
            logger.warning(f"【修仙插件】旧数据目录迁移检查失败（不影响新库使用）: {e}")

        db_path = plugin_data_path / db_filename
        self.db = DataBase(str(db_path))

        # ===== 基础管理器与处理器 =====
        self.misc_handler = MiscHandler(self.db, self.config)
        self.player_handler = PlayerHandler(self.db, self.config, self.config_manager)
        self.equipment_handler = EquipmentHandler(self.db, self.config_manager)
        self.breakthrough_handler = BreakthroughHandler(self.db, self.config_manager, self.config)
        self.pill_handler = PillHandler(self.db, self.config_manager)
        self.shop_handler = ShopHandler(self.db, self.config, self.config_manager)
        self.storage_ring_handler = StorageRingHandler(self.db, self.config_manager)
        
        from .core import StorageRingManager
        self.storage_ring_mgr = StorageRingManager(self.db, self.config_manager)
        
        self.combat_mgr = CombatManager()
        self.sect_mgr = SectManager(self.db, self.config_manager)
        self.boss_mgr = BossManager(self.db, self.combat_mgr, self.config_manager, self.storage_ring_mgr)
        self.rift_mgr = RiftManager(self.db, self.config_manager, self.storage_ring_mgr)
        self.rank_mgr = RankingManager(self.db, self.combat_mgr, self.config_manager)
        self.adventure_mgr = AdventureManager(self.db, self.storage_ring_mgr, self.config_manager)
        self.alchemy_mgr = AlchemyManager(self.db, self.config_manager, self.storage_ring_mgr)
        self.impart_mgr = ImpartManager(self.db)

        # ===== 功能处理器 =====
        self.sect_handlers = SectHandlers(self.db, self.sect_mgr)
        self.sect_shop_handler = SectShopHandler(
            self.db, self.shop_handler.shop_manager, self.storage_ring_mgr,
            self.config, self.sect_mgr,
        )
        self.boss_handlers = BossHandlers(self.db, self.boss_mgr)
        self.combat_handlers = CombatHandlers(self.db, self.combat_mgr, self.config_manager)
        self.ranking_handlers = RankingHandlers(self.db, self.rank_mgr)
        self.rift_handlers = RiftHandlers(self.db, self.rift_mgr)
        self.adventure_handlers = AdventureHandlers(self.db, self.adventure_mgr)
        self.alchemy_handlers = AlchemyHandlers(self.db, self.alchemy_mgr)
        self.impart_handlers = ImpartHandlers(self.db, self.impart_mgr)
        self.nickname_handler = NicknameHandler(self.db)
        
        self.bank_mgr = BankManager(self.db, self.config, self.config_manager)
        self.bounty_mgr = BountyManager(self.db, self.storage_ring_mgr, self.config_manager)
        self.bank_handlers = BankHandlers(self.db, self.bank_mgr)
        self.bounty_handlers = BountyHandlers(self.db, self.bounty_mgr)
        
        self.impart_pk_mgr = ImpartPkManager(self.db, self.combat_mgr, self.config_manager)
        self.impart_pk_handlers = ImpartPkHandlers(self.db, self.impart_pk_mgr)
        
        self.blessed_land_mgr = BlessedLandManager(self.db, self.config_manager)
        self.blessed_land_handlers = BlessedLandHandlers(self.db, self.blessed_land_mgr)
        self.spirit_farm_mgr = SpiritFarmManager(self.db, self.storage_ring_mgr, self.config_manager)
        self.spirit_farm_handlers = SpiritFarmHandlers(self.db, self.spirit_farm_mgr)
        self.dual_cult_mgr = DualCultivationManager(self.db)
        self.dual_cult_handlers = DualCultivationHandlers(self.db, self.dual_cult_mgr)
        self.spirit_eye_mgr = SpiritEyeManager(self.db)
        self.spirit_eye_handlers = SpiritEyeHandlers(self.db, self.spirit_eye_mgr)
        
        # v6.1 系统
        self.robbery_mgr = RobberyManager(self.db, self.storage_ring_mgr)
        self.robbery_handlers = RobberyHandlers(self.db, self.robbery_mgr)
        self.daily_task_mgr = DailyTaskManager(self.db, self.config_manager)
        self.daily_task_handlers = DailyTaskHandlers(self.db, self.daily_task_mgr, self.storage_ring_mgr)
        self.world_boss_mgr = WorldBossManager(self.db, self.config_manager, self.storage_ring_mgr)
        self.world_boss_handlers = WorldBossHandlers(self.db, self.world_boss_mgr)
        self.raid_mgr = RaidManager(self.db, self.config_manager, self.storage_ring_mgr, self.world_boss_mgr)
        self.raid_handlers = RaidHandlers(self.db, self.raid_mgr)
        self.battle_event_mgr = BattleEventManager(
            self.db, self.config_manager, self.storage_ring_mgr, self.world_boss_mgr
        )
        self.battle_event_handlers = BattleEventHandlers(self.db, self.battle_event_mgr)
        self.reinc_mgr = ReincarnationManager(self.db, self.config_manager, self.storage_ring_mgr)
        self.reinc_handlers = ReincarnationHandlers(self.db, self.reinc_mgr)
        self.tutorial_mgr = TutorialManager(self.db)

        # v6.2 & v6.3
        self.gamble_mgr = GambleManager(self.db, self.config_manager, self.storage_ring_mgr)
        self.gamble_handlers = GambleHandlers(self.db, self.gamble_mgr)
        self.forge_mgr = ForgeManager(self.db, self.config_manager, self.storage_ring_mgr)
        self.forge_handlers = ForgeHandlers(self.db, self.forge_mgr)

        # v6.4 天魔入侵 / v6.10 魔潮天灾（全服事件互斥）
        self.event_mgr = GlobalEventManager(self.db)
        self.demon_mgr = DemonInvasionManager(self.db, self.event_mgr)
        self.demon_handlers = DemonInvasionHandlers(self.db, self.demon_mgr, self.storage_ring_mgr)
        self.demon_handlers.context = self.context
        
        # v6.5 灵兽系统
        self.pet_mgr = PetManager(self.db)
        self.pet_handlers = PetHandlers(self.db, self.pet_mgr)

        # v6.6 交易坊市
        self.auction_mgr = AuctionManager(self.db, self.storage_ring_mgr, self.config)
        self.auction_handlers = AuctionHandlers(self.db, self.auction_mgr)

        # v6.35 拍卖会（与坊市独立）
        from .utils.auction_ai import AuctionAIBroadcaster
        auction_cfg = self.config.get("AUCTION_HOUSE", {}) or {}
        self.auction_house_mgr = AuctionHouseManager(
            self.db, self.storage_ring_mgr, self.config_manager, self.config
        )
        self.auction_house_handlers = AuctionHouseHandlers(self.db, self.auction_house_mgr)
        self.auction_ai = AuctionAIBroadcaster(
            self.context,
            enabled=bool(auction_cfg.get("AI_ENABLED", True)),
            bid_enabled=bool(auction_cfg.get("AI_BID_BROADCAST", False)),
            timeout=auction_cfg.get("AI_TIMEOUT", 8),
            max_chars=auction_cfg.get("AI_MAX_CHARS", 300),
            provider_id=auction_cfg.get("AI_PROVIDER_ID", ""),
        )

        # v6.8 押镖劫道 & 奇遇
        self.escort_mgr = EscortManager(self.db, self.combat_mgr, self.storage_ring_mgr, self.config_manager)
        self.encounter_mgr = EncounterManager(self.db, self.storage_ring_mgr, self.config_manager)

        # v6.10 生活职业 & 魔潮天灾
        self.life_skill_mgr = LifeSkillManager(self.db, self.storage_ring_mgr)
        self.talisman_mgr = TalismanManager(self.db, self.config_manager, self.storage_ring_mgr)
        self.calamity_mgr = CalamityManager(self.db, self.event_mgr)

        # v6.16 NPC道侣 & 师徒
        from .managers.social_manager import SocialManager
        self.social_mgr = SocialManager(self.db)
        # PlayerHandler 需要仙灵加成引用（构造时先占位，稍后注入）
        self.player_handler.social_mgr_ref = self.social_mgr

        # v6.17 称号系统
        from .managers.title_hooks import TitleHooks
        self.title_hooks = TitleHooks(self.db)
        self.title_mgr = self.title_hooks._tm()
        self.world_boss_mgr.title_mgr = self.title_mgr
        self.raid_mgr.title_mgr = self.title_mgr

        # v6.26 自动代理
        self.auto_proxy_mgr = AutoProxyManager(
            self.db,
            self.escort_mgr,
            self.sect_mgr,
            notifier=self._send_to_origin,
            title_bump=self._title_bump,
            bounty_mgr=self.bounty_mgr,
            lock_getter=get_player_lock,
            farm_mgr=self.spirit_farm_mgr,
        )
        self.auto_proxy_handlers = AutoProxyHandlers(self.db, self.auto_proxy_mgr)

        # v6.11 图片卡片开关注入
        try:
            from .utils import image_generator as _ig
            card_cfg = dict(self.config.get("IMAGE_CARDS", {}) or {})
            card_cfg["__enabled__"] = card_cfg.pop("ENABLED", True)
            _ig.set_card_flags(card_cfg)
            from .utils import static_image_generator as _sig
            _sig.set_static_image_config(self.config)
        except Exception as e:
            logger.warning(f"卡片配置注入失败（将全部启用）: {e}")
        
        # 后台任务句柄
        self.boss_task = None
        self.raid_task = None
        self.loan_check_task = None
        self.spirit_eye_task = None
        self.bounty_check_task = None
        self.demon_check_task = None
        self.market_npc_task = None
        self.auto_proxy_task = None
        self.battle_event_task = None
        self.auction_house_task = None

        access_control_config = self.config.get("ACCESS_CONTROL", {})
        self.whitelist_groups = [str(g) for g in access_control_config.get("WHITELIST_GROUPS", [])]
        self.boss_admins = [str(a) for a in access_control_config.get("BOSS_ADMINS", [])]

        logger.info(f"【修仙插件】XiuXianPlugin 初始化完成，数据库路径: {db_path}")

    def _check_access(self, event: AstrMessageEvent) -> bool:
        if not self.whitelist_groups: return True
        group_id = event.get_group_id()
        if not group_id: return True
        if str(group_id) in self.whitelist_groups: return True
        return False

    def _check_boss_admin(self, event: AstrMessageEvent) -> bool:
        if not self.boss_admins: return False
        sender_id = str(event.get_sender_id())
        return sender_id in self.boss_admins

    async def _send_access_denied_message(self, event: AstrMessageEvent):
        try:
            await event.send("抱歉，此群聊未在修仙插件的白名单中，无法使用相关功能。")
        except:
            pass

    async def initialize(self):
        await self.db.connect()
        migration_manager = MigrationManager(self.db.conn, self.config_manager)
        await migration_manager.migrate()
        await self.db.ext.ensure_system_config_table()

        try:
            event_state = await self.event_mgr.reconcile()
            if event_state == "both":
                logger.warning(
                    "【修仙插件】检测到天魔入侵与魔潮天灾同时处于开启状态，"
                    "已自动保留一个事件并清理另一个，恢复全服事件互斥。"
                )
        except Exception as e:
            logger.warning(f"【修仙插件】全服事件状态校准失败: {e}")

        # 清理旧黑市写入储物戒的幽灵物品（按旧价退款，只执行一次）
        try:
            refunded = await self.gamble_mgr.cleanup_legacy_black_market_items()
            if refunded:
                logger.info(f"【修仙插件】清理旧黑市幽灵物品，退款 {refunded:,} 灵石")
        except Exception as e:
            logger.warning(f"【修仙插件】旧黑市幽灵物品清理失败: {e}")
        
        self.boss_task = asyncio.create_task(self._schedule_boss_spawn())
        self.raid_task = asyncio.create_task(self._schedule_raid_spawn())
        self.loan_check_task = asyncio.create_task(self._schedule_loan_check())
        self.spirit_eye_task = asyncio.create_task(self._schedule_spirit_eye_spawn())
        self.bounty_check_task = asyncio.create_task(self._schedule_bounty_check())
        self.demon_check_task = asyncio.create_task(self._schedule_demon_check())
        self.tide_check_task = asyncio.create_task(self._schedule_tide_check())
        # v6.17 商店自动上货
        self.restock_task = asyncio.create_task(self._schedule_restock())
        self.sect_event_task = asyncio.create_task(self._schedule_sect_events())
        # v6.22 坊市NPC问价
        self.market_npc_task = asyncio.create_task(self._schedule_market_npc_offers())
        # v6.26 自动代理
        self.auto_proxy_task = asyncio.create_task(self._schedule_auto_proxy())
        # v6.34 江湖战斗小事件
        self.battle_event_task = asyncio.create_task(self._schedule_battle_events())
        # v6.35 拍卖会
        self.auction_house_task = asyncio.create_task(self._schedule_auction_house())
        
        logger.info("【修仙插件】已加载。")

    async def terminate(self):
        for task in (self.boss_task, self.raid_task, self.loan_check_task, self.spirit_eye_task,
                     self.bounty_check_task, self.demon_check_task,
                     getattr(self, "tide_check_task", None),
                     getattr(self, "restock_task", None),
                     getattr(self, "sect_event_task", None),
                     getattr(self, "market_npc_task", None),
                     getattr(self, "auto_proxy_task", None),
                     getattr(self, "battle_event_task", None),
                     getattr(self, "auction_house_task", None)):
            if task: task.cancel()
        await self.db.close()
        logger.info("【修仙插件】已卸载。")

    # ===== 定时任务与广播逻辑 =====
    
    async def _schedule_boss_spawn(self):
        retry_count = 0
        max_retry_delay = 3600
        while True:
            try:
                await self.db.ensure_connection()
                interval = self.config_manager.boss_config.get("spawn_interval", 3600)
                next_spawn_str = await self.db.ext.get_system_config("boss_next_spawn_time")
                current_time = int(time.time())
                
                if next_spawn_str:
                    next_spawn_time = int(next_spawn_str)
                    remaining = next_spawn_time - current_time
                    if remaining > 0:
                        logger.info(f"【修仙插件】动态世界Boss将在 {remaining} 秒后刷新")
                        await asyncio.sleep(remaining)
                else:
                    next_spawn_time = current_time + interval
                    await self.db.ext.set_system_config("boss_next_spawn_time", str(next_spawn_time))
                    await asyncio.sleep(interval)
                
                # 清理过期未击杀的旧世界Boss（睡醒后必须用当前时间，不能用 sleep 前的旧时间）
                now = int(time.time())
                if self.world_boss_mgr:
                    expired = await self.world_boss_mgr.expire_bosses(now)
                    if expired:
                        logger.info(f"【修仙插件】清理了 {expired} 个超时未击杀的旧世界Boss")
                    for settle_msg in await self.world_boss_mgr.settle_pending_bosses():
                        await self._broadcast_world_boss_spawn(settle_msg)

                # 检查并生成新Boss
                if self.world_boss_mgr:
                    async with self.db.conn.execute("SELECT * FROM world_boss WHERE status=1") as cur:
                        if not await cur.fetchone():
                            success, msg = await self.world_boss_mgr.spawn_boss()
                            if success:
                                logger.info(f"【修仙插件】自动生成动态世界Boss")
                                await self._broadcast_world_boss_spawn(msg)
                
                next_spawn_time = int(time.time()) + interval
                await self.db.ext.set_system_config("boss_next_spawn_time", str(next_spawn_time))
                retry_count = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Boss生成任务异常: {e}")
                retry_count += 1
                delay = min(60 * (2 ** retry_count), max_retry_delay)
                logger.info(f"【修仙插件】Boss任务将在 {delay} 秒后重试（第{retry_count}次）")
                await asyncio.sleep(delay)

    async def _schedule_raid_spawn(self):
        retry_count = 0
        max_retry_delay = 3600
        while True:
            try:
                await self.db.ensure_connection()
                if not self.raid_mgr.enabled:
                    await asyncio.sleep(3600)
                    continue
                interval = max(600, int(self.raid_mgr.auto_spawn_interval))
                next_spawn_str = await self.db.ext.get_system_config("raid_next_spawn_time")
                current_time = int(time.time())
                if next_spawn_str and str(next_spawn_str).isdigit():
                    remaining = int(next_spawn_str) - current_time
                    if remaining > 0:
                        logger.info(f"【修仙插件】团本将在 {remaining} 秒后开启")
                        await asyncio.sleep(remaining)
                else:
                    next_spawn_time = current_time + interval
                    await self.db.ext.set_system_config("raid_next_spawn_time", str(next_spawn_time))
                    await asyncio.sleep(interval)

                now = int(time.time())
                expired = await self.raid_mgr.expire_raids(now)
                if expired:
                    logger.info(f"【修仙插件】关闭了 {expired} 个超时团本")
                for settle_msg in await self.raid_mgr.settle_pending_raids():
                    await self._broadcast_world_boss_spawn(settle_msg)

                if not await self.raid_mgr.get_active_raid():
                    success, msg = await self.raid_mgr.spawn_raid()
                    if success:
                        logger.info("【修仙插件】自动开启团本")
                        await self._broadcast_world_boss_spawn(msg)

                next_spawn_time = int(time.time()) + interval
                await self.db.ext.set_system_config("raid_next_spawn_time", str(next_spawn_time))
                retry_count = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"团本生成任务异常: {e}")
                retry_count += 1
                delay = min(60 * (2 ** retry_count), max_retry_delay)
                logger.info(f"【修仙插件】团本任务将在 {delay} 秒后重试（第{retry_count}次）")
                await asyncio.sleep(delay)

    async def _schedule_demon_check(self):
        """定时检查天魔入侵是否超时并自动结算"""
        while True:
            try:
                await self.db.ensure_connection()
                await asyncio.sleep(300)
                
                is_active = await self.db.ext.get_system_config("demon_invasion_active")
                if is_active == "1":
                    end_time_str = await self.db.ext.get_system_config("demon_invasion_end_time")
                    try:
                        end_time = int(end_time_str or 0)
                    except (TypeError, ValueError):
                        end_time = 0
                    if not end_time or int(time.time()) > end_time:
                        success, msg = await self.demon_mgr.end_invasion()
                        if success:
                            logger.info(f"【修仙插件】天魔入侵自动结算：{msg}")
                            self.demon_mgr.pop_last_event_msg()
                            await self._broadcast_world_boss_spawn(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"天魔入侵检查任务异常: {e}")
                await asyncio.sleep(60)

    async def _title_bump(self, user_id: str, key: str, count: int = 1) -> str:
        """称号计数颁发（玩法处一行调用）"""
        try:
            player = await self.db.get_player_by_id(user_id)
            if not player:
                return ""
            return await self.title_hooks.bump(player, key, count)
        except Exception:
            return ""

    async def _auto_proxy_manual_exit(self, event: AstrMessageEvent) -> str:
        """用户手动放弃/退出/解卡时同步关闭代理，返回提示文案。"""
        try:
            closed = await self.auto_proxy_mgr.note_manual_exit(event.get_sender_id())
        except Exception as e:
            logger.warning(f"【自动代理】手动退出关闭失败: {e}")
            return ""
        if not closed:
            return ""
        row = await self.auto_proxy_mgr.get(event.get_sender_id())
        farm_on = bool(row and self.auto_proxy_mgr._farm_enabled(row))
        return (
            "\n\n🤖 检测到你手动退出，押镖/宗门任务代理已同步暂停。"
            + ("灵田代理继续运行。" if farm_on else "自动代理已关闭。")
        )

    async def _auto_proxy_external_activity(self, event: AstrMessageEvent) -> str:
        """用户开始闭关/历练/秘境等代理不接管的活动时关闭代理。"""
        try:
            closed = await self.auto_proxy_mgr.note_external_activity_started(event.get_sender_id())
        except Exception as e:
            logger.warning(f"【自动代理】其他活动关闭失败: {e}")
            return ""
        if not closed:
            return ""
        row = await self.auto_proxy_mgr.get(event.get_sender_id())
        farm_on = bool(row and self.auto_proxy_mgr._farm_enabled(row))
        return (
            "\n\n🛑 检测到你开始了其他活动，押镖/宗门任务代理已同步暂停。"
            + ("灵田代理继续运行。" if farm_on else "自动代理已关闭。")
        )

    async def _send_to_origin(self, origin: str, msg: str):
        if not origin:
            return
        from astrbot.api.event import MessageChain
        try:
            await self.context.send_message(origin, MessageChain().message(msg))
        except Exception as e:
            logger.warning(f"【修仙插件】宗门事件消息发送失败: {e}")

    async def _schedule_sect_events(self):
        """处理宗门命令到期、宗门大比截止和欠款自动偿还。"""
        last_debt_settle = 0
        while True:
            try:
                await asyncio.sleep(30)
                await self.db.ensure_connection()

                for origin, msg in await self.sect_mgr.expire_orders():
                    await self._send_to_origin(origin, msg)

                for origin, msg in await self.sect_mgr.finish_due_tournaments():
                    await self._send_to_origin(origin, msg)

                now = int(time.time())
                if now - last_debt_settle >= 300:
                    await self.db.ext.settle_all_sect_debts()
                    last_debt_settle = now
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"宗门定时任务异常: {e}")
                await asyncio.sleep(30)

    def _warn_no_whitelist(self, event_name: str):
        """全服事件广播因未配置白名单而被丢弃时提示管理员"""
        if not hasattr(self, "_whitelist_warned"):
            self._whitelist_warned = set()
        if event_name not in self._whitelist_warned:
            self._whitelist_warned.add(event_name)
            logger.warning(f"【修仙插件】{event_name} 广播被跳过：未配置白名单 WHITELIST_GROUPS，全服事件将不会通知任何群聊！")

    async def _schedule_restock(self):
        """v6.17 商店自动上货：每 10 分钟检查三阁，售罄/到期自动补货"""
        while True:
            try:
                await self.db.ensure_connection()
                await asyncio.sleep(600)
                n, detail = await self.shop_handler.auto_restock()
                if n:
                    logger.info(f"【修仙插件】自动上货完成（{n} 阁）：{detail}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动上货任务异常: {e}")
                await asyncio.sleep(120)

    async def _schedule_market_npc_offers(self):
        """v6.22 坊市NPC问价：按随机间隔生成问价，并清理过期问价"""
        while True:
            try:
                await self.db.ensure_connection()
                await asyncio.sleep(self.auction_mgr.npc_check_interval)
                generated, expired = await self.auction_mgr.process_npc_offers()
                if generated or expired:
                    logger.info(
                        f"【修仙插件】坊市NPC问价：新增 {generated} 条，过期 {expired} 条"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"坊市NPC问价任务异常: {e}")
                await asyncio.sleep(60)

    async def _schedule_auto_proxy(self):
        """v6.26 自动代理：结算到期活动并自动续接下一轮"""
        while True:
            try:
                await self.db.ensure_connection()
                await asyncio.sleep(self.auto_proxy_mgr.SCAN_INTERVAL)
                count = await self.auto_proxy_mgr.process_due()
                if count:
                    logger.info(f"【修仙插件】自动代理完成 {count} 轮续接")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动代理任务异常: {e}")
                await asyncio.sleep(30)

    async def _schedule_battle_events(self):
        """v6.34 江湖战斗小事件：每日10-24点每3-4小时随机出现"""
        while True:
            try:
                await self.db.ensure_connection()
                now = int(time.time())

                expired = await self.battle_event_mgr.expire_events(now)
                if expired:
                    logger.info(f"【修仙插件】{expired} 个江湖事件超时结束")
                for settle_msg in await self.battle_event_mgr.settle_pending_events():
                    await self._broadcast_world_boss_spawn(settle_msg)

                if not self.battle_event_mgr.enabled:
                    await asyncio.sleep(600)
                    continue

                next_spawn_str = await self.db.ext.get_system_config(
                    self.battle_event_mgr.KEY_NEXT_SPAWN
                )
                try:
                    next_spawn_time = int(next_spawn_str) if next_spawn_str else 0
                except (TypeError, ValueError):
                    next_spawn_time = 0
                if next_spawn_time <= 0:
                    next_spawn_time = self.battle_event_mgr.next_spawn_time(now)
                    await self.db.ext.set_system_config(
                        self.battle_event_mgr.KEY_NEXT_SPAWN, str(next_spawn_time)
                    )

                remaining = next_spawn_time - int(time.time())
                if remaining > 0:
                    await asyncio.sleep(min(remaining, 600))
                    continue

                now = int(time.time())
                if not self.battle_event_mgr.is_active_time(now):
                    next_spawn_time = self.battle_event_mgr.next_spawn_time(now)
                    await self.db.ext.set_system_config(
                        self.battle_event_mgr.KEY_NEXT_SPAWN, str(next_spawn_time)
                    )
                    await asyncio.sleep(60)
                    continue

                if await self.battle_event_mgr.get_active_event():
                    next_spawn_time = self.battle_event_mgr.next_spawn_time(now)
                    await self.db.ext.set_system_config(
                        self.battle_event_mgr.KEY_NEXT_SPAWN, str(next_spawn_time)
                    )
                    await asyncio.sleep(60)
                    continue

                success, msg, _ = await self.battle_event_mgr.spawn_event()
                if success:
                    logger.info("【修仙插件】江湖战斗小事件出现")
                    await self._broadcast_world_boss_spawn(msg)

                next_spawn_time = self.battle_event_mgr.next_spawn_time(int(time.time()))
                await self.db.ext.set_system_config(
                    self.battle_event_mgr.KEY_NEXT_SPAWN, str(next_spawn_time)
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"江湖事件任务异常: {e}")
                await asyncio.sleep(60)

    async def _schedule_auction_house(self):
        """v6.35 拍卖会：推进状态机并发送 AI/模板播报。"""
        while True:
            try:
                await self.db.ensure_connection()
                if not self.auction_house_mgr.enabled:
                    await asyncio.sleep(300)
                    continue
                queued = await self.auction_house_mgr.tick()
                if queued:
                    logger.info(f"【修仙插件】拍卖会新增 {queued} 条播报")
                await self._flush_auction_messages()
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"拍卖会任务异常: {e}")
                await asyncio.sleep(30)

    def _auction_provider_umo(self) -> str:
        """取第一个白名单群作为 AI Provider 的会话来源。"""
        if not self.whitelist_groups:
            return ""
        try:
            platforms = self.context.platform_manager.get_insts()
            if not platforms:
                return ""
            platform = platforms[0]
            platform_name = (
                platform.meta().name
                if hasattr(platform, "meta") and callable(platform.meta)
                else "unknown"
            )
            return f"{platform_name}:GroupMessage:{self.whitelist_groups[0]}"
        except Exception as e:
            logger.warning(f"【拍卖会】获取播报会话失败: {e}")
            return ""

    async def _flush_auction_messages(self):
        if not self.whitelist_groups:
            # 没有白名单时不广播，直接标记已处理，避免积压。
            messages = await self.auction_house_mgr.drain_messages(limit=20)
            for msg in messages:
                await self.auction_house_mgr.mark_message_sent(msg["id"])
            return

        messages = await self.auction_house_mgr.drain_messages(limit=10)
        if not messages:
            return
        umo = self._auction_provider_umo()
        for msg in messages:
            try:
                payload = json.loads(msg.get("payload_json") or "{}")
            except Exception:
                payload = {}
            fallback = msg.get("fallback_text") or ""
            text = await self.auction_ai.compose(
                msg.get("kind") or "", payload, fallback, umo
            )
            await self._broadcast_auction_house(text or fallback)
            await self.auction_house_mgr.mark_message_sent(msg["id"])
            await asyncio.sleep(0.2)

    async def _broadcast_auction_house(self, msg: str):
        from astrbot.api.event import MessageChain
        if not msg:
            return
        if not self.whitelist_groups:
            self._warn_no_whitelist("拍卖会")
            return
        message_chain = MessageChain().message(msg)
        try:
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms:
                platform_name = platform.meta().name if hasattr(platform, 'meta') and callable(platform.meta) else "unknown"
                for group_id in self.whitelist_groups:
                    umo = f"{platform_name}:GroupMessage:{group_id}"
                    try:
                        await self.context.send_message(umo, message_chain)
                    except Exception as e:
                        logger.warning(f"【修仙插件】拍卖会广播失败 (群{group_id}): {e}")
        except Exception as e:
            logger.error(f"【修仙插件】拍卖会广播异常: {e}")

    async def _broadcast_world_boss_spawn(self, msg: str):
        """广播动态世界Boss降临/天魔结算"""
        from astrbot.api.event import MessageChain
        if not self.whitelist_groups:
            self._warn_no_whitelist("世界Boss/魔潮")
            return
        message_chain = MessageChain().message(msg)
        try:
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms:
                platform_name = platform.meta().name if hasattr(platform, 'meta') and callable(platform.meta) else "unknown"
                for group_id in self.whitelist_groups:
                    umo = f"{platform_name}:GroupMessage:{group_id}"
                    try:
                        await self.context.send_message(umo, message_chain)
                    except Exception as e:
                        logger.warning(f"【修仙插件】广播失败 (群{group_id}): {e}")
        except Exception as e:
            logger.error(f"【修仙插件】广播异常: {e}")

    async def _broadcast_boss_defeat(self, player_name: str, battle_result: dict):
        """兼容旧版 Boss 击杀广播"""
        from astrbot.api.event import MessageChain
        if not self.whitelist_groups:
            self._warn_no_whitelist("Boss击杀")
            return
        reward = battle_result.get("reward", 0)
        rounds = battle_result.get("rounds", 0)
        broadcast_msg = (
            f"🎉 世界Boss已被击杀！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"击杀者：{player_name}\n"
            f"战斗回合：{rounds}\n"
            f"获得奖励：{reward} 灵石\n"
            f"━━━━━━━━━━━━━━━\n"
            f"恭喜大侠！下一只Boss即将刷新..."
        )
        message_chain = MessageChain().message(broadcast_msg)
        try:
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms:
                platform_name = platform.meta().name if hasattr(platform, 'meta') and callable(platform.meta) else "unknown"
                for group_id in self.whitelist_groups:
                    umo = f"{platform_name}:GroupMessage:{group_id}"
                    try:
                        await self.context.send_message(umo, message_chain)
                    except Exception as e:
                        logger.warning(f"【修仙插件】Boss击杀广播发送失败 (群{group_id}): {e}")
        except Exception as e:
            logger.error(f"【修仙插件】Boss击杀广播异常: {e}")

    async def _schedule_loan_check(self):
        retry_count = 0
        max_retry_delay = 3600
        while True:
            try:
                await self.db.ensure_connection()
                await asyncio.sleep(3600)
                processed = await self.bank_mgr.check_and_process_overdue_loans()
                if processed:
                    logger.info(f"【修仙插件】处理了 {len(processed)} 笔逾期贷款")
                    for loan_info in processed:
                        if loan_info.get("death"):
                            await self._broadcast_loan_death(loan_info)
                retry_count = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"贷款检查任务异常: {e}")
                retry_count += 1
                delay = min(60 * (2 ** retry_count), max_retry_delay)
                logger.info(f"【修仙插件】贷款检查任务将在 {delay} 秒后重试（第{retry_count}次）")
                await asyncio.sleep(delay)

    async def _broadcast_loan_death(self, loan_info: dict):
        from astrbot.api.event import MessageChain
        if not self.whitelist_groups:
            self._warn_no_whitelist("贷款追杀")
            return
        player_name = loan_info.get("player_name", "某修士")
        principal = loan_info.get("principal", 0)
        broadcast_msg = (
            f"💀 银行追杀公告 💀\n"
            f"━━━━━━━━━━━━━━━\n"
            f"修士【{player_name}】因贷款逾期未还\n"
            f"欠款：{principal:,} 灵石\n"
            f"已被灵石银行追杀致死！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⚠️ 借贷有风险，还款需及时！"
        )
        message_chain = MessageChain().message(broadcast_msg)
        try:
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms:
                platform_name = platform.meta().name if hasattr(platform, 'meta') and callable(platform.meta) else "unknown"
                for group_id in self.whitelist_groups:
                    umo = f"{platform_name}:GroupMessage:{group_id}"
                    try:
                        await self.context.send_message(umo, message_chain)
                    except Exception as e:
                        logger.warning(f"【修仙插件】贷款追杀广播发送失败 (群{group_id}): {e}")
        except Exception as e:
            logger.error(f"【修仙插件】贷款追杀广播异常: {e}")

    async def _schedule_spirit_eye_spawn(self):
        retry_count = 0
        max_retry_delay = 3600
        while True:
            try:
                await self.db.ensure_connection()
                spawn_interval = 7200
                next_spawn_str = await self.db.ext.get_system_config("spirit_eye_next_spawn_time")
                current_time = int(time.time())
                
                if next_spawn_str:
                    next_spawn_time = int(next_spawn_str)
                    remaining = next_spawn_time - current_time
                    if remaining > 0:
                        logger.info(f"【修仙插件】灵眼将在 {remaining} 秒后刷新")
                        await asyncio.sleep(remaining)
                else:
                    next_spawn_time = current_time + spawn_interval
                    await self.db.ext.set_system_config("spirit_eye_next_spawn_time", str(next_spawn_time))
                    await asyncio.sleep(spawn_interval)
                
                success, msg = await self.spirit_eye_mgr.spawn_spirit_eye()
                if success:
                    logger.info(f"【修仙插件】{msg}")
                    await self._broadcast_spirit_eye_spawn(msg)
                
                next_spawn_time = int(time.time()) + spawn_interval
                await self.db.ext.set_system_config("spirit_eye_next_spawn_time", str(next_spawn_time))
                retry_count = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"灵眼生成任务异常: {e}")
                retry_count += 1
                delay = min(60 * (2 ** retry_count), max_retry_delay)
                logger.info(f"【修仙插件】灵眼任务将在 {delay} 秒后重试（第{retry_count}次）")
                await asyncio.sleep(delay)

    async def _schedule_bounty_check(self):
        while True:
            try:
                await self.db.ensure_connection()
                await asyncio.sleep(1800)
                expired = await self.bounty_mgr.check_and_expire_bounties()
                if expired > 0:
                    logger.info(f"【修仙插件】处理了 {expired} 个过期悬赏任务")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"悬赏检查任务异常: {e}")
                await asyncio.sleep(60)

    async def _schedule_tide_check(self):
        """定时检查魔潮：到期结算 + 周六/月初自动开启"""
        while True:
            try:
                await self.db.ensure_connection()
                await asyncio.sleep(300)
                settled, msg = await self.calamity_mgr.auto_check()
                if settled and msg:
                    logger.info(f"【修仙插件】魔潮事件：{msg.splitlines()[0]}")
                    await self._broadcast_world_boss_spawn(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"魔潮检查任务异常: {e}")
                await asyncio.sleep(60)

    async def _broadcast_spirit_eye_spawn(self, msg: str):
        from astrbot.api.event import MessageChain
        if not self.whitelist_groups:
            self._warn_no_whitelist("灵眼降临")
            return
        broadcast_msg = f"👁️ {msg}\n💡 使用 /灵眼信息 查看详情"
        message_chain = MessageChain().message(broadcast_msg)
        try:
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms:
                platform_name = platform.meta().name if hasattr(platform, 'meta') and callable(platform.meta) else "unknown"
                for group_id in self.whitelist_groups:
                    umo = f"{platform_name}:GroupMessage:{group_id}"
                    try:
                        await self.context.send_message(umo, message_chain)
                    except Exception as e:
                        logger.warning(f"【修仙插件】灵眼广播发送失败 (群{group_id}): {e}")
        except Exception as e:
            logger.error(f"【修仙插件】灵眼广播异常: {e}")

    # ================= 指令注册区 =================

    @filter.command(CMD_HELP, "显示帮助信息")
    @require_whitelist
    async def handle_help(self, event: AstrMessageEvent):
        async for r in self.misc_handler.handle_help(event): yield r

    # ===== v6.26 自动代理 =====
    @filter.command(CMD_AUTO_PROXY, "开启自动代理（可指定 押镖/宗门任务/灵田）")
    @require_whitelist
    @with_player_lock
    async def handle_auto_proxy_enable(self, event: AstrMessageEvent, task: GreedyStr = ""):
        async for r in self.auto_proxy_handlers.handle_enable(event, task): yield r

    @filter.command(CMD_AUTO_PROXY_OFF, "关闭自动代理")
    @require_whitelist
    @with_player_lock
    async def handle_auto_proxy_disable(self, event: AstrMessageEvent, args: GreedyStr = ""):
        async for r in self.auto_proxy_handlers.handle_disable(event, args): yield r

    @filter.command(CMD_AUTO_PROXY_STATUS, "查看自动代理状态")
    @require_whitelist
    @with_player_lock
    async def handle_auto_proxy_status(self, event: AstrMessageEvent):
        async for r in self.auto_proxy_handlers.handle_status(event): yield r

    @filter.command(CMD_START_XIUXIAN, "开始你的修仙之路")
    @require_whitelist
    async def handle_start_xiuxian(self, event: AstrMessageEvent, cultivation_type: str = ""):
        async for r in self.player_handler.handle_start_xiuxian(event, cultivation_type): yield r

    @filter.command(CMD_PLAYER_INFO, "查看你的角色信息")
    @require_whitelist
    async def handle_player_info(self, event: AstrMessageEvent):
        async for r in self.player_handler.handle_player_info(event): yield r

    @filter.command(CMD_REBIRTH, "弃道重修（7天一次）")
    @require_whitelist
    async def handle_rebirth(self, event: AstrMessageEvent, confirm: str = ""):
        async for r in self.player_handler.handle_rebirth(event, confirm): yield r

    @filter.command(CMD_START_CULTIVATION, "开始闭关修炼")
    @require_whitelist
    async def handle_start_cultivation(self, event: AstrMessageEvent):
        async for r in self.player_handler.handle_start_cultivation(event): yield r
        note = await self._auto_proxy_external_activity(event)
        if note:
            yield event.plain_result(note.strip())

    @filter.command(CMD_END_CULTIVATION, "结束闭关修炼")
    @require_whitelist
    async def handle_end_cultivation(self, event: AstrMessageEvent):
        async for r in self.player_handler.handle_end_cultivation(event):
            yield r
        if get_event_flag(event, "cultivation_success", False):
            tmsg = await self._title_bump(event.get_sender_id(), "cultivate_count")
            if tmsg:
                yield event.plain_result(tmsg)

    @filter.command(CMD_CHECK_IN, "每日签到领取灵石")
    @require_whitelist
    async def handle_check_in(self, event: AstrMessageEvent):
        async for r in self.player_handler.handle_check_in(event):
            yield r

    @filter.command(CMD_SHOW_EQUIPMENT, "查看已装备的物品")
    @require_whitelist
    async def handle_show_equipment(self, event: AstrMessageEvent):
        async for r in self.equipment_handler.handle_show_equipment(event): yield r

    @filter.command(CMD_EQUIP_ITEM, "装备物品")
    @require_whitelist
    async def handle_equip_item(self, event: AstrMessageEvent, item_name: GreedyStr = ""):
        async for r in self.equipment_handler.handle_equip_item(event, item_name): yield r

    @filter.command(CMD_UNEQUIP_ITEM, "卸下装备")
    @require_whitelist
    async def handle_unequip_item(self, event: AstrMessageEvent, slot_or_name: str = ""):
        async for r in self.equipment_handler.handle_unequip_item(event, slot_or_name): yield r

    @filter.command(CMD_BREAKTHROUGH_INFO, "查看突破信息")
    @require_whitelist
    async def handle_breakthrough_info(self, event: AstrMessageEvent):
        async for r in self.breakthrough_handler.handle_breakthrough_info(event): yield r

    @filter.command(CMD_BREAKTHROUGH, "尝试突破境界，可带破境丹与品质")
    @require_whitelist
    async def handle_breakthrough(self, event: AstrMessageEvent, args: GreedyStr = ""):
        before = await self.db.get_player_by_id(event.get_sender_id())
        before_level = before.level_index if before else None

        async for r in self.breakthrough_handler.handle_breakthrough(event, args):
            yield r

        player = await self.db.get_player_by_id(event.get_sender_id())
        # 只有境界确实提升（突破成功）才推进引导/授予称号；师德由 BreakthroughManager 在成功分支发放
        if not player or before_level is None or player.level_index <= before_level:
            return

        tutorial_msg = await self.tutorial_mgr.check_and_advance(player, "breakthrough")
        if tutorial_msg:
            yield event.plain_result(tutorial_msg)
        # 称号：境界型
        try:
            tmsg = await self.title_hooks.bump_value(player, "breakthrough", player.level_index)
            if tmsg: yield event.plain_result(tmsg)
        except Exception:
            pass

    @filter.command(CMD_USE_PILL, "服用丹药（可指定品质与数量）")
    @require_whitelist
    async def handle_use_pill(self, event: AstrMessageEvent, args: GreedyStr):
        async for r in self.pill_handler.handle_use_pill(event, args): yield r

    @filter.command(CMD_SHOW_PILLS, "查看丹药背包")
    @require_whitelist
    async def handle_show_pills(self, event: AstrMessageEvent):
        async for r in self.pill_handler.handle_show_pills(event): yield r

    @filter.command(CMD_PILL_INFO, "查看丹药信息")
    @require_whitelist
    async def handle_pill_info(self, event: AstrMessageEvent, pill_name: str = ""):
        async for r in self.pill_handler.handle_pill_info(event, pill_name): yield r

    @filter.command(CMD_PILL_PAVILION, "查看丹阁丹药/丹方/材料")
    @require_whitelist
    async def handle_pill_pavilion(self, event: AstrMessageEvent, args: GreedyStr = ""):
        async for r in self.shop_handler.handle_pill_pavilion(event, args): yield r

    @filter.command(CMD_WEAPON_PAVILION, "查看器阁武器")
    @require_whitelist
    async def handle_weapon_pavilion(self, event: AstrMessageEvent):
        async for r in self.shop_handler.handle_weapon_pavilion(event): yield r

    @filter.command(CMD_TREASURE_PAVILION, "查看百宝阁物品")
    @require_whitelist
    async def handle_treasure_pavilion(self, event: AstrMessageEvent):
        async for r in self.shop_handler.handle_treasure_pavilion(event): yield r

    @filter.command(CMD_ITEM_INFO, "查看物品详细效果")
    @require_whitelist
    async def handle_item_info(self, event: AstrMessageEvent, item_name: str = ""):
        async for r in self.shop_handler.handle_item_info(event, item_name): yield r

    @filter.command(CMD_BUY, "购买物品")
    @require_whitelist
    async def handle_buy(self, event: AstrMessageEvent, item_name: str = ""):
        async for r in self.shop_handler.handle_buy(event, item_name): yield r
        if get_event_flag(event, "shop_buy", False):
            tmsg = await self._title_bump(event.get_sender_id(), "buy_count")
            if tmsg:
                yield event.plain_result(tmsg)

    @filter.command("购买物品", "购买物品（/购买 的兼容别名）")
    @require_whitelist
    async def handle_buy_compact(self, event: AstrMessageEvent, item_name: str = ""):
        # 兼容 "购买物品玄铁 5" / "购买物品 玄铁"：剥离"购买物品"前缀后走同一逻辑
        try:
            raw = (event.get_message_str() or "").strip().lstrip('/').lstrip('#').strip()
        except Exception:
            raw = ""
        if raw.startswith("购买物品"):
            raw = raw[len("购买物品"):].strip()
        async for r in self.shop_handler.handle_buy(event, raw): yield r
        if get_event_flag(event, "shop_buy", False):
            tmsg = await self._title_bump(event.get_sender_id(), "buy_count")
            if tmsg:
                yield event.plain_result(tmsg)

    @filter.command(CMD_STORAGE_RING, "查看储物戒信息")
    @require_whitelist
    async def handle_storage_ring(self, event: AstrMessageEvent):
        async for r in self.storage_ring_handler.handle_storage_ring(event): yield r

    @filter.command("领取溢出", "把溢出仓库物品取回储物戒")
    @require_whitelist
    async def handle_claim_overflow(self, event: AstrMessageEvent):
        async for r in self.storage_ring_handler.handle_claim_overflow(event): yield r

    @filter.command(CMD_UPGRADE_RING, "升级储物戒")
    @require_whitelist
    async def handle_upgrade_ring(self, event: AstrMessageEvent, ring_name: str = ""):
        async for r in self.storage_ring_handler.handle_upgrade_ring(event, ring_name): yield r

    @filter.command(CMD_DISCARD_ITEM, "丢弃储物戒中的物品")
    @require_whitelist
    async def handle_discard_item(self, event: AstrMessageEvent, args: GreedyStr):
        async for r in self.storage_ring_handler.handle_discard_item(event, args): yield r

    @filter.command(CMD_GIFT_ITEM, "赠予物品/丹药给其他玩家")
    @require_whitelist
    async def handle_gift_item(self, event: AstrMessageEvent, args: str = ""):
        async for r in self.storage_ring_handler.handle_gift_item(event, args): yield r

    @filter.command(CMD_ACCEPT_GIFT, "接收赠予的物品")
    @require_whitelist
    async def handle_accept_gift(self, event: AstrMessageEvent):
        async for r in self.storage_ring_handler.handle_accept_gift(event): yield r

    @filter.command(CMD_REJECT_GIFT, "拒绝赠予的物品")
    @require_whitelist
    async def handle_reject_gift(self, event: AstrMessageEvent):
        async for r in self.storage_ring_handler.handle_reject_gift(event): yield r

    @filter.command(CMD_SEARCH_ITEM, "搜索储物戒物品")
    @require_whitelist
    async def handle_search_item(self, event: AstrMessageEvent, keyword: str = ""):
        async for r in self.storage_ring_handler.handle_search_item(event, keyword): yield r

    # ===== 宗门 =====
    @filter.command(CMD_CREATE_SECT, "创建宗门")
    @require_whitelist
    @with_player_lock
    async def handle_create_sect(self, event: AstrMessageEvent, name: str = ""):
        if not name:
            yield event.plain_result(f"请输入宗门名称，例如：/{CMD_CREATE_SECT} 逍遥门")
            return
        async for r in self.sect_handlers.handle_create_sect(event, name): yield r

    @filter.command(CMD_JOIN_SECT, "加入宗门")
    @require_whitelist
    @with_player_lock
    async def handle_join_sect(self, event: AstrMessageEvent, name: str = ""):
        if not name:
            yield event.plain_result(f"请输入要加入的宗门名称，例如：/{CMD_JOIN_SECT} 逍遥门")
            return
        async for r in self.sect_handlers.handle_join_sect(event, name): yield r

    @filter.command(CMD_LEAVE_SECT, "退出当前宗门")
    @require_whitelist
    @with_player_lock
    async def handle_leave_sect(self, event: AstrMessageEvent):
        async for r in self.sect_handlers.handle_leave_sect(event): yield r

    @filter.command("解散宗门", "宗主解散当前宗门")
    @require_whitelist
    @with_player_lock
    async def handle_disband_sect(self, event: AstrMessageEvent, confirm_text: str = ""):
        async for r in self.sect_handlers.handle_disband_sect(event, confirm_text): yield r

    @filter.command(CMD_MY_SECT, "查看我的宗门信息")
    @require_whitelist
    async def handle_my_sect(self, event: AstrMessageEvent):
        async for r in self.sect_handlers.handle_my_sect(event): yield r

    @filter.command(CMD_SECT_TASK, "执行宗门任务")
    @require_whitelist
    @with_player_lock
    async def handle_sect_task(self, event: AstrMessageEvent):
        async for r in self.sect_handlers.handle_sect_task(event): yield r
        if get_event_flag(event, "sect_task_started", False):
            await self.auto_proxy_mgr.note_activity_started(
                event.get_sender_id(), str(getattr(event, "unified_msg_origin", "") or "")
            )

    @filter.command(CMD_FINISH_SECT_TASK, "完成宗门任务")
    @require_whitelist
    @with_player_lock
    async def handle_finish_sect_task(self, event: AstrMessageEvent):
        async for r in self.sect_handlers.handle_finish_sect_task(event): yield r
        if get_event_flag(event, "sect_finish_success", False):
            user_id = event.get_sender_id()
            await self.db.ext.add_task_progress(user_id, "sect_task", 1)
            player = await self.db.get_player_by_id(user_id)
            if player:
                has_progress, bounty_msg = await self.bounty_mgr.add_bounty_progress(player, "sect", 1)
                if has_progress:
                    yield event.plain_result(bounty_msg.strip())

    @filter.command("放弃宗门任务", "放弃当前宗门任务（支付违约金）")
    @require_whitelist
    @with_player_lock
    async def handle_abandon_sect_task(self, event: AstrMessageEvent):
        async for r in self.sect_handlers.handle_abandon_sect_task(event): yield r
        if get_event_flag(event, "sect_abandon_success", False):
            note = await self._auto_proxy_manual_exit(event)
            if note:
                yield event.plain_result(note.strip())

    @filter.command("宗门商店", "查看宗门贡献商店")
    @require_whitelist
    async def handle_sect_shop(self, event: AstrMessageEvent):
        async for r in self.sect_shop_handler.handle_shop(event): yield r

    @filter.command("宗门兑换", "用宗门贡献兑换商品")
    @require_whitelist
    async def handle_sect_shop_buy(self, event: AstrMessageEvent, index: str = ""):
        async for r in self.sect_shop_handler.handle_buy(event, index): yield r
        if get_event_flag(event, "sect_shop_buy", False):
            tmsg = await self._title_bump(event.get_sender_id(), "buy_count")
            if tmsg:
                yield event.plain_result(tmsg)

    @filter.command("宗门命令", "向宗门成员下达命令")
    @require_whitelist
    @with_player_lock
    async def handle_sect_order(self, event: AstrMessageEvent, args: GreedyStr):
        async for r in self.sect_handlers.handle_create_order(event, str(args)): yield r

    @filter.command("宗门命令列表", "查看进行中的宗门命令")
    @require_whitelist
    async def handle_sect_order_list(self, event: AstrMessageEvent):
        async for r in self.sect_handlers.handle_list_orders(event): yield r

    @filter.command("宗门命令撤销", "撤销宗门命令")
    @require_whitelist
    @with_player_lock
    async def handle_sect_order_cancel(self, event: AstrMessageEvent, order_id: str = ""):
        async for r in self.sect_handlers.handle_cancel_order(event, order_id): yield r

    @filter.command("宗门大比开启", "宗主开启本宗门大比")
    @require_whitelist
    @with_player_lock
    async def handle_sect_tournament_start(self, event: AstrMessageEvent):
        async for r in self.sect_handlers.handle_start_tournament(event): yield r

    @filter.command("宗门大比报名", "报名参加宗门大比")
    @require_whitelist
    @with_player_lock
    async def handle_sect_tournament_register(self, event: AstrMessageEvent):
        async for r in self.sect_handlers.handle_register_tournament(event): yield r

    @filter.command("宗门大比状态", "查看宗门大比状态")
    @require_whitelist
    async def handle_sect_tournament_status(self, event: AstrMessageEvent):
        async for r in self.sect_handlers.handle_tournament_status(event): yield r

    @filter.command("宗门宣战", "宗主向其他宗门宣战")
    @require_whitelist
    @with_player_lock
    async def handle_sect_declare_war(self, event: AstrMessageEvent, target_name: str = ""):
        async for r in self.sect_handlers.handle_declare_war(event, target_name): yield r

    @filter.command("宗门战争状态", "查看宗门战争状态")
    @require_whitelist
    async def handle_sect_war_status(self, event: AstrMessageEvent):
        async for r in self.sect_handlers.handle_war_status(event): yield r

    @filter.command("宗门战报", "查看最近宗门战报（可带编号）")
    @require_whitelist
    async def handle_sect_war_report(self, event: AstrMessageEvent, war_id: str = ""):
        async for r in self.sect_handlers.handle_war_report(event, war_id): yield r

    @filter.command(CMD_SECT_LIST, "查看宗门列表")
    @require_whitelist
    async def handle_sect_list(self, event: AstrMessageEvent):
        async for r in self.sect_handlers.handle_sect_list(event): yield r

    @filter.command(CMD_SECT_DONATE, "宗门捐献")
    @require_whitelist
    @with_player_lock
    async def handle_sect_donate(self, event: AstrMessageEvent, amount: str = "0"):
        try: amount_int = int(amount)
        except ValueError:
            yield event.plain_result("请输入正确的数字！")
            return
        if amount_int <= 0:
             yield event.plain_result(f"请输入捐献数量，例如：/{CMD_SECT_DONATE} 1000")
             return
        async for r in self.sect_handlers.handle_donate(event, amount_int): yield r

    @filter.command(CMD_SECT_KICK, "踢出宗门成员")
    @require_whitelist
    @with_player_lock
    async def handle_sect_kick(self, event: AstrMessageEvent, target: str = ""):
        async for r in self.sect_handlers.handle_kick_member(event, target): yield r

    @filter.command(CMD_SECT_TRANSFER, "宗主传位")
    @require_whitelist
    @with_player_lock
    async def handle_sect_transfer(self, event: AstrMessageEvent, target: str = ""):
        async for r in self.sect_handlers.handle_transfer(event, target): yield r

    @filter.command(CMD_SECT_POSITION, "变更成员职位")
    @require_whitelist
    @with_player_lock
    async def handle_sect_position(self, event: AstrMessageEvent, target: str = "", position: str = "-1"):
        try: pos_int = int(position)
        except ValueError:
            yield event.plain_result("职位必须是数字！")
            return
        if pos_int < 1 or pos_int > 4:
            yield event.plain_result(
                f"请输入目标和职位ID(1-4)，例如：/{CMD_SECT_POSITION} @某人 1\n"
                "宗主之位移交请使用 /宗主传位 @某人"
            )
            return
        async for r in self.sect_handlers.handle_position_change(event, target, pos_int): yield r

    # ===== 旧版Boss =====
    @filter.command(CMD_BOSS_INFO, "查看世界Boss状态")
    @require_whitelist
    async def handle_boss_info(self, event: AstrMessageEvent):
        # 旧指令统一转发到动态世界Boss，避免两套系统显示不一致
        async for r in self.world_boss_handlers.handle_query(event): yield r
    
    @filter.command("世界boss", "查看世界Boss状态")
    @require_whitelist
    async def handle_boss_info_lower(self, event: AstrMessageEvent):
        async for r in self.world_boss_handlers.handle_query(event): yield r

    @filter.command(CMD_BOSS_FIGHT, "挑战世界Boss(旧)")
    @require_whitelist
    @with_player_lock
    async def handle_boss_fight(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        success, msg, battle_result = await self.boss_handlers.handle_boss_fight(user_id)
        # 击杀时尝试生成结算海报：成功只发图，失败/关闭降级为战报文本
        poster = None
        if success and battle_result and battle_result.get("winner") == user_id:
            try:
                from .utils.image_generator import render_card_async, card_enabled
                if card_enabled("CARD_COMBAT"):
                    player = await self.db.get_player_by_id(user_id)
                    player_name = player.user_name if player and player.user_name else f"道友{str(user_id)[:6]}"
                    poster = await render_card_async(
                        "combat",
                        "⚔️ Boss讨伐胜利",
                        f"击杀者：{player_name}",
                        sections=[
                            {"name": "战报", "rows": [
                                ("战斗回合", f"{battle_result.get('rounds', 0)}"),
                                ("获得奖励", f"{battle_result.get('reward', 0):,} 灵石"),
                                ("剩余气血", f"{battle_result.get('player_final_hp', 0):,}"),
                            ]},
                            {"name": "天道贺词", "lines": [
                                "魔首伏诛，四方宾服。",
                                "此战之功，录入仙史！",
                            ]},
                        ],
                        footer="模拟修仙 · 战斗结算",
                    )
                    if poster:
                        await self.db.ext.add_task_progress(user_id, "boss_damage", 1)
                        await self._title_bump(user_id, "boss_kill_old")
                        await self._broadcast_boss_defeat(player_name, battle_result)
                        yield event.image_result(poster)
                        return
            except Exception:
                poster = None

        yield event.plain_result(msg)
        if success and battle_result and battle_result.get("winner") == user_id:
            await self.db.ext.add_task_progress(user_id, "boss_damage", 1)
            await self._title_bump(user_id, "boss_kill_old")
            player = await self.db.get_player_by_id(user_id)
            player_name = player.user_name if player and player.user_name else f"道友{str(user_id)[:6]}"
            await self._broadcast_boss_defeat(player_name, battle_result)

    @filter.command(CMD_SPAWN_BOSS, "生成旧版Boss（建议用 /生成世界Boss）")
    @require_whitelist
    async def handle_spawn_boss(self, event: AstrMessageEvent):
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有权限生成Boss！")
            return
        success, msg, boss = await self.boss_handlers.handle_spawn_boss()
        yield event.plain_result(msg)
        if success and boss:
            await self._broadcast_boss_spawn(boss)

    # ===== 排行榜 =====
    @filter.command(CMD_RANK_LEVEL, "境界排行")
    @require_whitelist
    async def handle_rank_level(self, event: AstrMessageEvent):
        async for r in self.ranking_handlers.handle_rank_level(event): yield r

    @filter.command(CMD_RANK_POWER, "战力排行")
    @require_whitelist
    async def handle_rank_power(self, event: AstrMessageEvent):
        async for r in self.ranking_handlers.handle_rank_power(event): yield r

    @filter.command(CMD_RANK_WEALTH, "财富排行")
    @require_whitelist
    async def handle_rank_wealth(self, event: AstrMessageEvent):
        async for r in self.ranking_handlers.handle_rank_wealth(event): yield r

    @filter.command(CMD_RANK_SECT, "宗门排行")
    @require_whitelist
    async def handle_rank_sect(self, event: AstrMessageEvent):
        async for r in self.ranking_handlers.handle_rank_sect(event): yield r

    @filter.command(CMD_RANK_DEPOSIT, "存款排行")
    @require_whitelist
    async def handle_rank_deposit(self, event: AstrMessageEvent):
        async for r in self.ranking_handlers.handle_rank_deposit(event): yield r

    @filter.command(CMD_RANK_CONTRIBUTION, "贡献排行")
    @require_whitelist
    async def handle_rank_contribution(self, event: AstrMessageEvent):
        async for r in self.ranking_handlers.handle_rank_sect_contribution(event): yield r

    # ===== 战斗 =====
    @filter.command(CMD_DUEL, "决斗")
    @require_whitelist
    async def handle_duel(self, event: AstrMessageEvent, target: str = ""):
        user_id = event.get_sender_id()
        async for r in self.combat_handlers.handle_duel(event, target):
            yield r
        # 只有获胜才计入 win_pvp 任务进度
        last_result = get_event_flag(event, "duel_result", None)
        if last_result and last_result.get("winner") == user_id:
            await self.db.ext.add_task_progress(user_id, "win_pvp", 1)
            tmsg = await self._title_bump(user_id, "win_pvp")
            if tmsg:
                yield event.plain_result(tmsg)

    @filter.command(CMD_SPAR, "切磋")
    @require_whitelist
    async def handle_spar(self, event: AstrMessageEvent, target: str = ""):
        user_id = event.get_sender_id()
        async for r in self.combat_handlers.handle_spar(event, target):
            yield r
        # 真正开打（非冷却被拒）才计入 spar 任务进度
        if get_event_flag(event, "spar_fought", False):
            await self.db.ext.add_task_progress(user_id, "spar", 1)
            await self._title_bump(user_id, "spar_count")
            # 悬赏联动（spar/pvp tag）
            player = await self.db.get_player_by_id(user_id)
            if player:
                await self.bounty_mgr.add_bounty_progress(player, "spar", 1)
                await self.bounty_mgr.add_bounty_progress(player, "pvp", 1)

    # ===== 秘境 =====
    @filter.command(CMD_RIFT_LIST, "秘境列表")
    @require_whitelist
    async def handle_rift_list(self, event: AstrMessageEvent):
        async for r in self.rift_handlers.handle_rift_list(event): yield r

    @filter.command(CMD_RIFT_EXPLORE, "探索秘境")
    @require_whitelist
    @with_player_lock
    async def handle_rift_explore(self, event: AstrMessageEvent, rift_id: str = "0"):
        try: rift_id_int = int(rift_id)
        except ValueError:
            yield event.plain_result("请输入正确的秘境ID！")
            return
        async for r in self.rift_handlers.handle_rift_explore(event, rift_id_int): yield r
        note = await self._auto_proxy_external_activity(event)
        if note:
            yield event.plain_result(note.strip())

    @filter.command(CMD_RIFT_COMPLETE, "完成秘境探索")
    @require_whitelist
    @with_player_lock
    async def handle_rift_complete(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        success, msg, reward_data = await self.rift_mgr.finish_exploration(user_id)
        if success and reward_data:
            await self.db.ext.add_task_progress(user_id, "explore_rift", 1)
            await self._title_bump(user_id, "rift_count")
            player = await self.db.get_player_by_id(user_id)
            if player:
                has_progress, bounty_msg = await self.bounty_mgr.add_bounty_progress(player, "rift", 1)
                if has_progress: msg += bounty_msg

                # 奇遇系统：秘境归来概率触发三选一事件（25%）
                try:
                    encounter_msg = await self.encounter_mgr.maybe_trigger(player, base_chance=25)
                    if encounter_msg: msg += encounter_msg
                except Exception as e:
                    logger.warning(f"秘境奇遇触发异常: {e}")
        yield event.plain_result(msg)

    @filter.command(CMD_RIFT_EXIT, "退出秘境")
    @require_whitelist
    @with_player_lock
    async def handle_rift_exit(self, event: AstrMessageEvent):
        async for r in self.rift_handlers.handle_rift_exit(event): yield r
        if get_event_flag(event, "rift_abandon_success", False):
            note = await self._auto_proxy_manual_exit(event)
            if note:
                yield event.plain_result(note.strip())

    # ===== 历练 =====
    @filter.command(CMD_ADVENTURE_START, "开始历练")
    @require_whitelist
    @with_player_lock
    async def handle_adventure_start(self, event: AstrMessageEvent, route: str = ""):
        async for r in self.adventure_handlers.handle_start_adventure(event, route): yield r
        note = await self._auto_proxy_external_activity(event)
        if note:
            yield event.plain_result(note.strip())

    @filter.command(CMD_ADVENTURE_COMPLETE, "完成历练")
    @require_whitelist
    @with_player_lock
    async def handle_adventure_complete(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        success, msg, reward_data = await self.adventure_mgr.finish_adventure(user_id)
        if success and reward_data:
            await self.db.ext.add_task_progress(user_id, "adventure", 1)
            await self._title_bump(user_id, "adventure_count")
            player = await self.db.get_player_by_id(user_id)
            if player:
                bounty_tag = reward_data.get("bounty_tag", "adventure")
                bounty_value = reward_data.get("bounty_progress", 1)
                has_progress, bounty_msg = await self.bounty_mgr.add_bounty_progress(player, bounty_tag, bounty_value)
                if has_progress: msg += bounty_msg
                
                tutorial_msg = await self.tutorial_mgr.check_and_advance(player, "adventure")
                if tutorial_msg: msg += tutorial_msg

            # 奇遇系统：历练归来概率触发三选一事件
            try:
                encounter_msg = await self.encounter_mgr.maybe_trigger(player)
                if encounter_msg:
                    msg += encounter_msg
                    await self._title_bump(user_id, "luck_event")
            except Exception as e:
                logger.warning(f"奇遇触发异常: {e}")
        yield event.plain_result(msg)

    @filter.command("放弃历练", "放弃当前历练（支付违约金）")
    @require_whitelist
    @with_player_lock
    async def handle_adventure_abandon(self, event: AstrMessageEvent):
        async for r in self.adventure_handlers.handle_abandon_adventure(event): yield r
        if get_event_flag(event, "adventure_abandon_success", False):
            note = await self._auto_proxy_manual_exit(event)
            if note:
                yield event.plain_result(note.strip())

    @filter.command(CMD_ADVENTURE_STATUS, "历练状态")
    @require_whitelist
    async def handle_adventure_status(self, event: AstrMessageEvent):
        async for r in self.adventure_handlers.handle_adventure_status(event): yield r

    @filter.command(CMD_ADVENTURE_INFO, "历练信息")
    @require_whitelist
    async def handle_adventure_info(self, event: AstrMessageEvent):
        async for r in self.adventure_handlers.handle_adventure_info(event): yield r

    # ===== 炼丹 =====
    @filter.command(CMD_ALCHEMY_RECIPES, "丹药配方")
    @require_whitelist
    async def handle_alchemy_recipes(self, event: AstrMessageEvent):
        async for r in self.alchemy_handlers.handle_recipes(event): yield r

    @filter.command(CMD_RECIPE_BOOK, "查看丹方")
    @require_whitelist
    async def handle_recipe_book(self, event: AstrMessageEvent):
        async for r in self.alchemy_handlers.handle_recipes(event): yield r

    @filter.command(CMD_LEARN_RECIPE, "学习丹方")
    @require_whitelist
    @idle_player_lock
    async def handle_learn_recipe(self, event: AstrMessageEvent, args: GreedyStr = ""):
        async for r in self.alchemy_handlers.handle_learn_recipe(event, args): yield r

    @filter.command(CMD_ALCHEMY_CRAFT, "炼制丹药")
    @require_whitelist
    async def handle_alchemy_craft(self, event: AstrMessageEvent, args: GreedyStr = ""):
        async for r in self.alchemy_handlers.handle_craft(event, args):
            yield r
        # 任务要求"成功炼制"：失败/丹劫毁丹不计进度
        if get_event_flag(event, "craft_success", False):
            user_id = event.get_sender_id()
            await self.db.ext.add_task_progress(user_id, "craft_pill", 1)
            await self._title_bump(user_id, "craft_count")
            if get_event_flag(event, "craft_perfect", False):
                await self._title_bump(user_id, "craft_perfect")
            # 悬赏联动（craft tag）
            player = await self.db.get_player_by_id(user_id)
            if player:
                await self.bounty_mgr.add_bounty_progress(player, "craft", 1)

    # ===== 传承 =====
    @filter.command(CMD_IMPART_INFO, "传承信息")
    @require_whitelist
    async def handle_impart_info(self, event: AstrMessageEvent):
        async for r in self.impart_handlers.handle_impart_info(event): yield r

    # ===== 道号 =====
    @filter.command(CMD_CHANGE_NICKNAME, "改道号")
    @require_whitelist
    async def handle_change_nickname(self, event: AstrMessageEvent, new_name: str = ""):
        async for r in self.nickname_handler.handle_change_nickname(event, new_name): yield r

    # ===== 银行 =====
    @filter.command(CMD_BANK_INFO, "查看银行信息")
    @require_whitelist
    async def handle_bank_info(self, event: AstrMessageEvent):
        async for r in self.bank_handlers.handle_bank_info(event): yield r

    @filter.command(CMD_BANK_DEPOSIT, "存入灵石")
    @require_whitelist
    async def handle_bank_deposit(self, event: AstrMessageEvent, amount: str = "0"):
        try: amount_int = int(amount)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的数字！")
            return
        async for r in self.bank_handlers.handle_deposit(event, amount_int): yield r

    @filter.command(CMD_BANK_WITHDRAW, "取出灵石")
    @require_whitelist
    async def handle_bank_withdraw(self, event: AstrMessageEvent, amount: str = "0"):
        try: amount_int = int(amount)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的数字！")
            return
        async for r in self.bank_handlers.handle_withdraw(event, amount_int): yield r

    @filter.command(CMD_BANK_INTEREST, "领取利息")
    @require_whitelist
    async def handle_bank_interest(self, event: AstrMessageEvent):
        async for r in self.bank_handlers.handle_claim_interest(event): yield r

    @filter.command(CMD_BANK_LOAN, "申请贷款")
    @require_whitelist
    async def handle_bank_loan(self, event: AstrMessageEvent, amount: str = "0"):
        try: amount_int = int(amount)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的数字！")
            return
        async for r in self.bank_handlers.handle_loan(event, amount_int): yield r

    @filter.command(CMD_BANK_REPAY, "偿还贷款")
    @require_whitelist
    async def handle_bank_repay(self, event: AstrMessageEvent, amount: str = ""):
        async for r in self.bank_handlers.handle_repay(event, amount): yield r

    @filter.command(CMD_BANK_TRANSACTIONS, "银行流水")
    @require_whitelist
    async def handle_bank_transactions(self, event: AstrMessageEvent):
        async for r in self.bank_handlers.handle_transactions(event): yield r

    @filter.command(CMD_BANK_BREAKTHROUGH_LOAN, "申请突破贷款")
    @require_whitelist
    async def handle_bank_breakthrough_loan(self, event: AstrMessageEvent, amount: str = "0"):
        try: amount_int = int(amount)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的数字！")
            return
        async for r in self.bank_handlers.handle_breakthrough_loan(event, amount_int): yield r

    # ===== 悬赏 =====
    @filter.command(CMD_BOUNTY_LIST, "查看悬赏任务")
    @require_whitelist
    async def handle_bounty_list(self, event: AstrMessageEvent):
        async for r in self.bounty_handlers.handle_bounty_list(event): yield r

    @filter.command(CMD_BOUNTY_ACCEPT, "接取悬赏任务")
    @require_whitelist
    async def handle_bounty_accept(self, event: AstrMessageEvent, bounty_id: str = "0"):
        try: bounty_id_int = int(bounty_id)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的悬赏编号！")
            return
        async for r in self.bounty_handlers.handle_accept_bounty(event, bounty_id_int): yield r

    @filter.command(CMD_BOUNTY_STATUS, "查看悬赏状态")
    @require_whitelist
    async def handle_bounty_status(self, event: AstrMessageEvent):
        async for r in self.bounty_handlers.handle_bounty_status(event): yield r

    @filter.command(CMD_BOUNTY_COMPLETE, "完成悬赏任务")
    @require_whitelist
    async def handle_bounty_complete(self, event: AstrMessageEvent):
        async for r in self.bounty_handlers.handle_complete_bounty(event): yield r

    @filter.command(CMD_BOUNTY_ABANDON, "放弃悬赏任务")
    @require_whitelist
    async def handle_bounty_abandon(self, event: AstrMessageEvent):
        async for r in self.bounty_handlers.handle_abandon_bounty(event): yield r

    # ===== 传承PK =====
    @filter.command(CMD_IMPART_CHALLENGE, "发起传承挑战")
    @require_whitelist
    async def handle_impart_challenge(self, event: AstrMessageEvent, target: str = ""):
        async for r in self.impart_pk_handlers.handle_impart_challenge(event, target): yield r

    @filter.command(CMD_IMPART_RANKING, "查看传承排行")
    @require_whitelist
    async def handle_impart_ranking(self, event: AstrMessageEvent):
        async for r in self.impart_pk_handlers.handle_impart_ranking(event): yield r

    # ===== 洞天福地 =====
    @filter.command(CMD_BLESSED_LAND_INFO, "查看洞天信息")
    @require_whitelist
    async def handle_blessed_land_info(self, event: AstrMessageEvent):
        async for r in self.blessed_land_handlers.handle_blessed_land_info(event): yield r

    @filter.command(CMD_BLESSED_LAND_BUY, "购买洞天")
    @require_whitelist
    async def handle_blessed_land_buy(self, event: AstrMessageEvent, land_type: str = "0"):
        try: land_int = int(land_type)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的数字！")
            return
        async for r in self.blessed_land_handlers.handle_purchase(event, land_int): yield r

    @filter.command(CMD_BLESSED_LAND_UPGRADE, "升级洞天")
    @require_whitelist
    async def handle_blessed_land_upgrade(self, event: AstrMessageEvent):
        async for r in self.blessed_land_handlers.handle_upgrade(event): yield r
        # 称号：洞天等级
        try:
            p1 = await self.db.get_player_by_id(event.get_sender_id())
            if p1:
                land = None
                async with self.db.conn.execute("SELECT level FROM blessed_lands WHERE user_id = ?", (p1.user_id,)) as cur:
                    row = await cur.fetchone()
                if row and row[0] >= 5:
                    tmsg = await self.title_hooks.bump_value(p1, "land_level", row[0])
                    if tmsg: yield event.plain_result(tmsg)
        except Exception:
            pass

    @filter.command(CMD_BLESSED_LAND_COLLECT, "收取洞天产出")
    @require_whitelist
    async def handle_blessed_land_collect(self, event: AstrMessageEvent):
        async for r in self.blessed_land_handlers.handle_collect(event): yield r

    @filter.command(CMD_BLESSED_LAND_ADVANCE, "进阶洞天")
    @require_whitelist
    async def handle_blessed_land_advance(self, event: AstrMessageEvent, target_type: str = "0"):
        try: target_int = int(target_type)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的数字！")
            return
        async for r in self.blessed_land_handlers.handle_advance(event, target_int): yield r

    # ===== 灵田 =====
    @filter.command(CMD_SPIRIT_FARM_INFO, "查看灵田")
    @require_whitelist
    async def handle_spirit_farm_info(self, event: AstrMessageEvent):
        async for r in self.spirit_farm_handlers.handle_farm_info(event): yield r

    @filter.command(CMD_SPIRIT_FARM_CREATE, "开垦灵田")
    @require_whitelist
    async def handle_spirit_farm_create(self, event: AstrMessageEvent):
        async for r in self.spirit_farm_handlers.handle_create_farm(event): yield r

    @filter.command(CMD_SPIRIT_FARM_PLANT, "种植灵草")
    @require_whitelist
    async def handle_spirit_farm_plant(self, event: AstrMessageEvent, herb_name: GreedyStr = ""):
        async for r in self.spirit_farm_handlers.handle_plant(event, herb_name): yield r
        if get_event_flag(event, "farm_plant_ok", False):
            try:
                await self.auto_proxy_mgr.note_farm_planted(event.get_sender_id())
            except Exception as e:
                logger.warning(f"【自动代理】种植后同步灵田代理失败: {e}")

    @filter.command(CMD_SPIRIT_FARM_HARVEST, "收获灵草")
    @require_whitelist
    async def handle_spirit_farm_harvest(self, event: AstrMessageEvent):
        async for r in self.spirit_farm_handlers.handle_harvest(event):
            yield r
        # 真正收获成功（有成熟作物）才计任务进度
        if get_event_flag(event, "harvest_ok", False):
            harvest_count = int(get_event_flag(event, "harvest_count", 0) or 0)
            # 只在真正收到成熟作物时计入进度；仅清理枯萎不应推进任务/悬赏/称号
            if harvest_count > 0:
                user_id = event.get_sender_id()
                await self.db.ext.add_task_progress(user_id, "harvest", 1)
                await self._title_bump(user_id, "harvest_count")
                player = await self.db.get_player_by_id(user_id)
                if player:
                    has_progress, bounty_msg = await self.bounty_mgr.add_bounty_progress(
                        player, "harvest", harvest_count
                    )
                    if has_progress:
                        yield event.plain_result(bounty_msg.strip())

    @filter.command(CMD_SPIRIT_FARM_REMOVE, "铲除指定格子的灵草")
    @require_whitelist
    async def handle_spirit_farm_remove(self, event: AstrMessageEvent, slot: GreedyStr = ""):
        async for r in self.spirit_farm_handlers.handle_remove_crop(event, slot): yield r
        if get_event_flag(event, "farm_remove_ok", False):
            try:
                await self.auto_proxy_mgr.clear_farm_plot(
                    event.get_sender_id(), get_event_flag(event, "farm_remove_slot", 0)
                )
            except Exception as e:
                logger.warning(f"【自动代理】铲除后同步灵田代理失败: {e}")

    @filter.command(CMD_SPIRIT_FARM_UPGRADE, "升级灵田")
    @require_whitelist
    async def handle_spirit_farm_upgrade(self, event: AstrMessageEvent):
        async for r in self.spirit_farm_handlers.handle_upgrade_farm(event): yield r

    # ===== 双修 =====
    @filter.command(CMD_DUAL_CULT_REQUEST, "发起双修")
    @require_whitelist
    async def handle_dual_cult_request(self, event: AstrMessageEvent, target: str = ""):
        async for r in self.dual_cult_handlers.handle_dual_request(event, target): yield r

    @filter.command(CMD_DUAL_CULT_ACCEPT, "接受双修")
    @require_whitelist
    async def handle_dual_cult_accept(self, event: AstrMessageEvent):
        async for r in self.dual_cult_handlers.handle_accept(event): yield r

    @filter.command(CMD_DUAL_CULT_REJECT, "拒绝双修")
    @require_whitelist
    async def handle_dual_cult_reject(self, event: AstrMessageEvent):
        async for r in self.dual_cult_handlers.handle_reject(event): yield r

    # ===== 灵眼 =====
    @filter.command(CMD_SPIRIT_EYE_INFO, "查看灵眼")
    @require_whitelist
    async def handle_spirit_eye_info(self, event: AstrMessageEvent):
        async for r in self.spirit_eye_handlers.handle_spirit_eye_info(event): yield r

    @filter.command(CMD_SPIRIT_EYE_CLAIM, "抢占灵眼")
    @require_whitelist
    async def handle_spirit_eye_claim(self, event: AstrMessageEvent, eye_id: str = "0"):
        try: eye_id_int = int(eye_id)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的数字！")
            return
        async for r in self.spirit_eye_handlers.handle_claim(event, eye_id_int): yield r

    @filter.command(CMD_SPIRIT_EYE_COLLECT, "收取灵眼产出")
    @require_whitelist
    async def handle_spirit_eye_collect(self, event: AstrMessageEvent):
        async for r in self.spirit_eye_handlers.handle_collect(event): yield r

    @filter.command(CMD_SPIRIT_EYE_RELEASE, "释放灵眼")
    @require_whitelist
    async def handle_spirit_eye_release(self, event: AstrMessageEvent):
        async for r in self.spirit_eye_handlers.handle_release(event): yield r

    # ===== v6.1 日常任务 =====
    @filter.command("任务榜", "查看日常任务")
    @require_whitelist
    async def handle_task_board(self, event: AstrMessageEvent):
        async for r in self.daily_task_handlers.handle_task_board(event): yield r

    @filter.command("接取任务", "接取日常任务")
    @require_whitelist
    async def handle_accept_task(self, event: AstrMessageEvent, level: str = ""):
        async for r in self.daily_task_handlers.handle_accept_task(event, level): yield r

    @filter.command("放弃任务", "放弃进行中的日常任务")
    @require_whitelist
    async def handle_abandon_task(self, event: AstrMessageEvent, task_id: str = ""):
        async for r in self.daily_task_handlers.handle_abandon_task(event, task_id): yield r

    @filter.command("领取任务奖励", "领取日常任务奖励")
    @require_whitelist
    async def handle_claim_reward(self, event: AstrMessageEvent):
        async for r in self.daily_task_handlers.handle_claim_reward(event): yield r

    # ===== v6.1 动态世界Boss =====
    @filter.command("挑战世界Boss", "参与全服世界Boss讨伐")
    @require_whitelist
    async def handle_challenge_world_boss(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        async for r in self.world_boss_handlers.handle_challenge(event): yield r
        # 挑战成功（真正打出伤害）计入 boss_damage 任务进度
        if get_event_flag(event, "world_boss_challenge", False):
            await self.db.ext.add_task_progress(user_id, "boss_damage", 1)
            await self._title_bump(user_id, "boss_damage")
            # 悬赏联动（boss_damage/tide tag）
            player = await self.db.get_player_by_id(user_id)
            if player:
                await self.bounty_mgr.add_bounty_progress(player, "boss_damage", 1)
                await self.bounty_mgr.add_bounty_progress(player, "tide", 1)
        # 击杀结算全服广播（排名奖励公示）
        settle_msg = get_event_flag(event, "world_boss_settle_msg", None)
        if settle_msg:
            await self._broadcast_world_boss_spawn(settle_msg)

    @filter.command("世界Boss状态", "查看动态世界Boss信息")
    @require_whitelist
    async def handle_query_world_boss(self, event: AstrMessageEvent):
        async for r in self.world_boss_handlers.handle_query(event): yield r
            
    @filter.command("生成世界Boss", "手动召唤世界Boss(管理员)")
    @require_whitelist
    async def handle_spawn_world_boss(self, event: AstrMessageEvent):
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有权限！")
            return
        success, msg = await self.world_boss_mgr.spawn_boss()
        yield event.plain_result(msg)

    # ===== v6.34 江湖战斗小事件 =====
    async def _run_battle_event_attack(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        async for r in self.battle_event_handlers.handle_attack(event):
            yield r
        if get_event_flag(event, "battle_event_attack", False):
            await self.db.ext.add_task_progress(user_id, "boss_damage", 1)
            await self._title_bump(user_id, "boss_damage")
            player = await self.db.get_player_by_id(user_id)
            if player:
                await self.bounty_mgr.add_bounty_progress(player, "boss_damage", 1)
                await self.bounty_mgr.add_bounty_progress(player, "tide", 1)
        if get_event_flag(event, "battle_event_kill", False):
            await self._title_bump(user_id, "boss_kill_old")
        settle_msg = get_event_flag(event, "battle_event_settle_msg", None)
        if settle_msg:
            await self._broadcast_world_boss_spawn(settle_msg)

    @filter.command(CMD_BATTLE_EVENT_ATTACK, "讨伐当前江湖事件目标")
    @require_whitelist
    async def handle_battle_event_attack(self, event: AstrMessageEvent):
        async for r in self._run_battle_event_attack(event):
            yield r

    @filter.command("追捕", "讨伐当前江湖事件目标(别名)")
    @require_whitelist
    async def handle_battle_event_attack_alias(self, event: AstrMessageEvent):
        async for r in self._run_battle_event_attack(event):
            yield r

    @filter.command("挑战通缉犯", "讨伐当前江湖事件目标(别名)")
    @require_whitelist
    async def handle_battle_event_attack_alias2(self, event: AstrMessageEvent):
        async for r in self._run_battle_event_attack(event):
            yield r

    @filter.command(CMD_BATTLE_EVENT_STATUS, "查看当前江湖事件")
    @require_whitelist
    async def handle_battle_event_status(self, event: AstrMessageEvent):
        async for r in self.battle_event_handlers.handle_status(event):
            yield r

    @filter.command("通缉令", "查看当前江湖事件(别名)")
    @require_whitelist
    async def handle_battle_event_status_alias(self, event: AstrMessageEvent):
        async for r in self.battle_event_handlers.handle_status(event):
            yield r

    @filter.command(CMD_BATTLE_EVENT_SPAWN, "手动生成江湖事件(管理员)")
    @require_whitelist
    async def handle_spawn_battle_event(self, event: AstrMessageEvent, event_type: str = "", tier: str = ""):
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有权限！")
            return
        force_type = event_type.strip() or None
        force_tier = None
        if tier.strip():
            try:
                force_tier = int(tier.strip())
            except ValueError:
                yield event.plain_result("❌ 等级必须是数字，例如：/生成江湖事件 wanted 5")
                return
        success, msg, _ = await self.battle_event_mgr.spawn_event(force_type, force_tier)
        if success:
            await self._broadcast_world_boss_spawn(msg)
        yield event.plain_result(msg)

    # ===== v6.29 世界Boss团本 =====
    @filter.command("挑战团本", "参与全服多阶段团本讨伐")
    @require_whitelist
    async def handle_challenge_raid(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        async for r in self.raid_handlers.handle_challenge(event): yield r
        if get_event_flag(event, "raid_challenge", False):
            await self.db.ext.add_task_progress(user_id, "boss_damage", 1)
            await self._title_bump(user_id, "boss_damage")
            player = await self.db.get_player_by_id(user_id)
            if player:
                await self.bounty_mgr.add_bounty_progress(player, "boss_damage", 1)
                await self.bounty_mgr.add_bounty_progress(player, "tide", 1)
        broadcast_msg = get_event_flag(event, "raid_broadcast_msg", None)
        if broadcast_msg:
            await self._broadcast_world_boss_spawn(broadcast_msg)

    @filter.command("团本状态", "查看当前团本阶段与进度")
    @require_whitelist
    async def handle_raid_status(self, event: AstrMessageEvent):
        async for r in self.raid_handlers.handle_status(event): yield r

    @filter.command("世界团本", "查看当前团本阶段与进度(别名)")
    @require_whitelist
    async def handle_raid_status_alias(self, event: AstrMessageEvent):
        async for r in self.raid_handlers.handle_status(event): yield r

    @filter.command("团本排行", "查看当前团本伤害榜")
    @require_whitelist
    async def handle_raid_ranking(self, event: AstrMessageEvent):
        async for r in self.raid_handlers.handle_ranking(event): yield r

    @filter.command("开启团本", "手动开启团本(管理员)，可指定难度1-3")
    @require_whitelist
    async def handle_spawn_raid(self, event: AstrMessageEvent, difficulty: str = ""):
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有权限！")
            return
        diff = None
        if str(difficulty).strip():
            try:
                diff = max(1, min(3, int(str(difficulty).strip())))
            except ValueError:
                yield event.plain_result("❌ 难度必须是 1-3，例如：/开启团本 2")
                return
        success, msg = await self.raid_mgr.spawn_raid(diff)
        if success:
            await self._broadcast_world_boss_spawn(msg)
        yield event.plain_result(msg)

    @filter.command("结束团本", "强制关闭当前团本(管理员)")
    @require_whitelist
    async def handle_end_raid(self, event: AstrMessageEvent):
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有权限！")
            return
        success, msg = await self.raid_mgr.force_end_raid()
        yield event.plain_result(msg)

    # ===== v6.1 洞府掠夺 =====
    @filter.command("开启护府", "使用护府令保护洞天")
    @require_whitelist
    async def handle_use_shield(self, event: AstrMessageEvent):
        async for r in self.robbery_handlers.handle_use_shield(event): yield r
        if get_event_flag(event, "shield_success", False):
            tmsg = await self._title_bump(event.get_sender_id(), "shield_count")
            if tmsg:
                yield event.plain_result(tmsg)

    @filter.command("潜入洞府", "潜入他人洞府偷窃")
    @require_whitelist
    async def handle_rob(self, event: AstrMessageEvent, target: str = ""):
        async for r in self.robbery_handlers.handle_rob(event, target): yield r

    # ===== v6.1 飞升转生 =====
    @filter.command("飞升", "达到最高境界后飞升转生（可附加装备名封印为传家宝）")
    @require_whitelist
    async def handle_reincarnate(self, event: AstrMessageEvent, heirloom_name: str = ""):
        before = None
        p0 = await self.db.get_player_by_id(event.get_sender_id())
        if p0: before = p0.reincarnation_count
        async for r in self.reinc_handlers.handle_reincarnate(event, heirloom_name):
            yield r
        # 称号：转生次数（飞升后 count+1）
        try:
            p1 = await self.db.get_player_by_id(event.get_sender_id())
            if p1 and before is not None and p1.reincarnation_count > before:
                tmsg = await self.title_hooks.bump_value(p1, "reincarnation", p1.reincarnation_count)
                if tmsg: yield event.plain_result(tmsg)
        except Exception:
            pass

    @filter.command("解封传家宝", "达到筑基期后解封转生前的装备")
    @require_whitelist
    async def handle_unseal_heirloom(self, event: AstrMessageEvent):
        async for r in self.reinc_handlers.handle_unseal(event):
            yield r

    @filter.command("仙缘商店", "查看转生永久加成")
    @require_whitelist
    async def handle_fate_shop(self, event: AstrMessageEvent):
        async for r in self.reinc_handlers.handle_fate_shop(event): yield r

    # ===== 新增：仙缘兑换 =====
    @filter.command("仙缘兑换", "购买仙缘商店的永久加成")
    @require_whitelist
    async def handle_fate_exchange(self, event: AstrMessageEvent, item_id: str = "0"):
        async for r in self.reinc_handlers.handle_fate_exchange(event, item_id):
            yield r

    # ===== v6.1 真·一键日常 =====
    @filter.command("一键日常", "自动完成签到/收菜等挂机操作")
    @require_whitelist
    @with_player_lock
    async def handle_daily_helper(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        player = await self.db.get_player_by_id(user_id)
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途，请先发送「我要修仙」。")
            return

        # 检查是否处于忙碌状态
        user_cd = await self.db.ext.get_user_cd(user_id)
        if user_cd and user_cd.type != UserStatus.IDLE:
            status_name = UserStatus.get_name(user_cd.type)
            yield event.plain_result(f"❌ 道友当前正「{status_name}」，无法分心打理日常。")
            return

        details = []
        has_any_action = False

        # 1. 签到
        today = datetime.now().strftime("%Y-%m-%d")
        if player.last_check_in_date != today:
            check_in_gold_min = self.config.get("VALUES", {}).get("CHECK_IN_GOLD_MIN", 50)
            check_in_gold_max = self.config.get("VALUES", {}).get("CHECK_IN_GOLD_MAX", 500)
            check_in_gold = random.randint(check_in_gold_min, check_in_gold_max)
            try:
                from .managers.pet_manager import PetManager
                pet_mgr = PetManager(self.db)
                pet = await pet_mgr.get_pet(user_id)
                gold_bonus = pet_mgr.get_gold_bonus(pet)
                if gold_bonus > 0:
                    check_in_gold = int(check_in_gold * (1 + gold_bonus))
            except Exception:
                pass
            try:
                from .core.technique_bonus import TechniqueBonusManager
                check_in_gold = TechniqueBonusManager.apply_gold_bonus(
                    player, check_in_gold, self.config_manager
                )
            except Exception:
                pass
            player.gold += check_in_gold
            player.last_check_in_date = today
            details.append(f"✅ 签到成功，获得 {check_in_gold} 灵石")
            
            # ===== 节日判定 =====
            festival = self.player_handler._get_today_festival()
            if festival:
                festival_gold = festival["gold"]
                try:
                    from .core.technique_bonus import TechniqueBonusManager
                    festival_gold = TechniqueBonusManager.apply_gold_bonus(
                        player, festival_gold, self.config_manager
                    )
                except Exception:
                    pass
                player.gold += festival_gold
                player.experience += festival["exp"]
                item_name = festival.get("item")
                item_count = festival.get("item_count", 1)
                if item_name:
                    await self.storage_ring_mgr.store_item(player, item_name, item_count, silent=True)
                
                details.append(f"🎊 今天是【{festival['name']}】，额外获得 {festival_gold} 灵石、{festival['exp']} 修为及恩赐物品！")
            # ======================
            
            await self.db.ext.add_task_progress(user_id, "check_in", 1)
            has_any_action = True
            # 签到改动立即落库，防止后续子系统未触发 update_player 时被结尾覆盖丢失
            await self.db.update_player(player)
            await self._title_bump(user_id, "checkin_count")

        # 2. 银行利息
        try:
            bank_data = await self.db.ext.get_bank_account(user_id)
            if bank_data and bank_data["balance"] > 0:
                success, msg = await self.bank_mgr.claim_interest(player)
                if success:
                    details.append(f"🏦 银行利息已领取")
                    has_any_action = True
        except Exception as e: logger.warning(f"一键日常-银行利息异常: {e}")

        # 3. 洞天福地
        try:
            success, msg = await self.blessed_land_mgr.collect_income(player)
            if success:
                details.append(f"🏔️ 洞天产出已收取")
                has_any_action = True
        except Exception as e: logger.warning(f"一键日常-洞天异常: {e}")

        # 4. 灵田
        try:
            success, msg = await self.spirit_farm_mgr.harvest(player)
            harvest_count = int(
                (getattr(self.spirit_farm_mgr, "last_harvest_counts", {}) or {}).get(user_id, 0) or 0
            )
            if success and harvest_count > 0:
                details.append(f"🌾 灵田已收获")
                has_any_action = True
                await self.db.ext.add_task_progress(user_id, "harvest", 1)
                await self._title_bump(user_id, "harvest_count")
                player_fresh = await self.db.get_player_by_id(user_id)
                if player_fresh:
                    has_progress, bounty_msg = await self.bounty_mgr.add_bounty_progress(
                        player_fresh, "harvest", harvest_count
                    )
                    if has_progress:
                        details.append(bounty_msg.strip())
        except Exception as e: logger.warning(f"一键日常-灵田异常: {e}")

        # 5. 灵眼
        try:
            success, msg = await self.spirit_eye_mgr.collect_spirit_eye(player)
            if success:
                details.append(f"👁️ 灵眼产出已收取")
                has_any_action = True
        except Exception as e: logger.warning(f"一键日常-灵眼异常: {e}")

        if not has_any_action:
            yield event.plain_result("✨ 你今日的日常打理得井井有条，目前没有可以一键收取的东西。")
            return

        # 仅用于展示最新数值；各子系统已各自持久化，这里不再回写避免用陈旧对象覆盖
        player = await self.db.get_player_by_id(user_id)

        reply_msg = "🪄 【一键日常】托管完成！\n"
        reply_msg += "━━━━━━━━━━━━━━━\n"
        reply_msg += "\n".join(details)
        reply_msg += f"\n━━━━━━━━━━━━━━━\n"
        reply_msg += f"💰 当前持有灵石：{player.gold:,}\n"
        reply_msg += f"📈 当前修为：{player.experience:,}"
        
        yield event.plain_result(reply_msg)

    # ===== v6.1 解卡 =====
    @filter.command("解卡", "强制重置卡死状态")
    @require_whitelist
    @with_player_lock
    async def handle_force_unlock(self, event: AstrMessageEvent):
        async for r in self.misc_handler.handle_force_unlock(event): yield r
        if get_event_flag(event, "unlock_success", False):
            note = await self._auto_proxy_manual_exit(event)
            if note:
                yield event.plain_result(note.strip())

    @filter.command("修仙诊断", "诊断修仙插件运行状态")
    @require_whitelist
    async def handle_xiuxian_diag(self, event: AstrMessageEvent):
        """临时诊断：确认运行时加载的是哪份 utils、消息如何被归一化。"""
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有管理员权限！")
            return
        from .handlers import utils as xiuxian_utils

        player = await self.db.get_player_by_id(event.get_sender_id())
        user_cd = (
            await self.db.ext.get_user_cd(event.get_sender_id()) if player else None
        )
        raw_text = event.get_message_str()
        normalized = xiuxian_utils._normalize_command_text(raw_text)
        my_info_allowed = xiuxian_utils._is_command_allowed(
            "我的信息", xiuxian_utils.BUSY_STATE_ALLOWED_COMMANDS
        )
        lines = [
            "🛠️ 修仙诊断",
            f"utils文件：{xiuxian_utils.__file__}",
            f"原始消息：{raw_text!r}",
            f"归一化后：{normalized!r}",
            f"白名单数量：{len(xiuxian_utils.BUSY_STATE_ALLOWED_COMMANDS)}",
            f"白名单含我的信息：{'我的信息' in xiuxian_utils.BUSY_STATE_ALLOWED_COMMANDS}",
            f"我的信息判定：{my_info_allowed}",
            f"user_cd.type：{getattr(user_cd, 'type', None)}",
            f"player.state：{getattr(player, 'state', None)}",
        ]
        yield event.plain_result("\n".join(lines))

    # ===== v6.2 赌石与黑市 =====
    @filter.command("切原石", "花费灵石赌石")
    @require_whitelist
    async def handle_cut_stone(self, event: AstrMessageEvent, count: str = "1"):
        async for r in self.gamble_handlers.handle_cut_stone(event, count): yield r
        if get_event_flag(event, "gamble_success", False):
            tmsg = await self._title_bump(event.get_sender_id(), "gamble_count")
            if tmsg:
                yield event.plain_result(tmsg)

    @filter.command("黑市", "查看当前小时黑市")
    @require_whitelist
    async def handle_market(self, event: AstrMessageEvent):
        async for r in self.gamble_handlers.handle_market(event): yield r

    @filter.command("黑市购买", "从黑市购买商品")
    @require_whitelist
    async def handle_buy_market(self, event: AstrMessageEvent, index: str = "0"):
        async for r in self.gamble_handlers.handle_buy_market(event, index): yield r
        if get_event_flag(event, "blackmarket_success", False):
            tmsg = await self._title_bump(event.get_sender_id(), "blackmarket_buy")
            if tmsg:
                yield event.plain_result(tmsg)

    # ===== v6.3 炼器与洗练 =====
    @filter.command("强化", "消耗强化石提升炼器等级")
    @require_whitelist
    async def handle_enhance(self, event: AstrMessageEvent):
        async for r in self.forge_handlers.handle_enhance(event): yield r
        if get_event_flag(event, "enhance_success", False):
            uid = event.get_sender_id()
            for key in ("enhance_count", "forge_count"):
                tmsg = await self._title_bump(uid, key)
                if tmsg:
                    yield event.plain_result(tmsg)

    @filter.command("洗练", "消耗洗练石重置装备词条，可指定 武器/防具")
    @require_whitelist
    async def handle_wash(self, event: AstrMessageEvent, slot: str = ""):
        async for r in self.forge_handlers.handle_wash(event, slot): yield r
        if get_event_flag(event, "wash_success", False):
            uid = event.get_sender_id()
            for key in ("wash_count", "forge_count"):
                tmsg = await self._title_bump(uid, key)
                if tmsg:
                    yield event.plain_result(tmsg)

    # ===== v6.4 天魔入侵 =====
    @filter.command("开启天魔入侵", "管理员开启活动")
    @require_whitelist
    async def handle_demon_start(self, event: AstrMessageEvent, hours: str = "24"):
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有权限！")
            return
        async for r in self.demon_handlers.handle_start(event, hours): yield r
        if get_event_flag(event, "demon_start_success", False):
            await self._broadcast_world_boss_spawn(get_event_flag(event, "demon_start_msg", ""))

    @filter.command("结束天魔入侵", "管理员结束活动并结算")
    @require_whitelist
    async def handle_demon_end(self, event: AstrMessageEvent):
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有权限！")
            return
        success, msg = await self.demon_mgr.end_invasion()
        if success:
            self.demon_mgr.pop_last_event_msg()
            await self._broadcast_world_boss_spawn(msg)
        yield event.plain_result(msg)

    @filter.command("查验天魔", "查验某人是否为天魔")
    @require_whitelist
    async def handle_demon_catch(self, event: AstrMessageEvent, target: str = ""):
        async for r in self.demon_handlers.handle_catch(event, target):
            yield r
        # 称号：揪出天魔
        try:
            if get_event_flag(event, "demon_catch_success", False):
                tmsg = await self._title_bump(event.get_sender_id(), "demon_catch")
                if tmsg: yield event.plain_result(tmsg)
        except Exception:
            pass
        end_msg = self.demon_mgr.pop_last_event_msg()
        if end_msg:
            await self._broadcast_world_boss_spawn(end_msg)

    @filter.command("天魔状态", "查看当前活动状态")
    @require_whitelist
    async def handle_demon_status(self, event: AstrMessageEvent):
        async for r in self.demon_handlers.handle_status(event): yield r

    # ===== v6.25 灵兽系统（多宠/修为共享/御兽联动） =====
    @filter.command("我的灵兽", "查看当前携带的灵兽")
    @require_whitelist
    async def handle_my_pet(self, event: AstrMessageEvent):
        async for r in self.pet_handlers.handle_my_pet(event): yield r

    @filter.command("孵化灵兽", "契约一只新灵兽")
    @require_whitelist
    async def handle_adopt_pet(self, event: AstrMessageEvent, pet_name: str = ""):
        async for r in self.pet_handlers.handle_adopt(event, pet_name): yield r

    @filter.command("喂养灵兽", "消耗资源提升灵兽等级")
    @require_whitelist
    async def handle_feed_pet(self, event: AstrMessageEvent, args: GreedyStr = ""):
        uid = event.get_sender_id()
        before = await self.pet_mgr.get_max_pet_level(uid)
        async for r in self.pet_handlers.handle_feed(event, args): yield r
        try:
            p1 = await self.db.get_player_by_id(uid)
            after = await self.pet_mgr.get_max_pet_level(uid)
            if p1 and before is not None and after > before:
                tmsg = await self.title_hooks.bump_value(p1, "pet_level", after)
                if tmsg: yield event.plain_result(tmsg)
        except Exception:
            pass

    @filter.command("灵兽出战", "设置出战的主战灵兽")
    @require_whitelist
    async def handle_set_main_pet(self, event: AstrMessageEvent, args: GreedyStr = ""):
        async for r in self.pet_handlers.handle_set_main(event, args): yield r

    @filter.command("灵兽改名", "给灵兽改一个名字")
    @require_whitelist
    async def handle_rename_pet(self, event: AstrMessageEvent, args: GreedyStr = ""):
        async for r in self.pet_handlers.handle_rename(event, args): yield r

    # ===== v6.5.1 新增：放生与吞噬 =====
    @filter.command("放生灵兽", "放生指定灵兽")
    @require_whitelist
    async def handle_release_pet(self, event: AstrMessageEvent, args: GreedyStr = ""):
        async for r in self.pet_handlers.handle_release(event, args): yield r

    @filter.command("吞噬灵兽", "吞噬指定灵兽，返还部分资源")
    @require_whitelist
    async def handle_devour_pet(self, event: AstrMessageEvent, args: GreedyStr = ""):
        async for r in self.pet_handlers.handle_devour(event, args): yield r

    # ===== v6.6 宗门大比 =====
    @filter.command("开启宗门大比", "管理员开启活动")
    @require_whitelist
    async def handle_start_sect_battle(self, event: AstrMessageEvent):
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有权限！")
            return
        yield event.plain_result("ℹ️ 旧版全服宗门大比已下线，请由宗主使用 /宗门大比开启。")

    @filter.command("结束宗门大比", "管理员结束活动")
    @require_whitelist
    async def handle_end_sect_battle(self, event: AstrMessageEvent):
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有权限！")
            return
        yield event.plain_result("ℹ️ 旧版全服宗门大比已下线，新的宗门大比由宗主开启并自动结算。")

    @filter.command("大比切磋", "旧版宗门切磋（已下线）")
    @require_whitelist
    async def handle_sect_battle(self, event: AstrMessageEvent, target: str = ""):
        yield event.plain_result("ℹ️ 旧版大比切磋已下线，请由宗主使用 /宗门大比开启，成员用 /宗门大比报名。")

    # ===== v6.7 交易坊市 / v6.22 NPC问价 =====
    @filter.command("坊市", "查看玩家交易坊市")
    @require_whitelist
    async def handle_market_list(self, event: AstrMessageEvent):
        async for r in self.auction_handlers.handle_list(event): yield r

    @filter.command("坊市上架", "上架物品/丹药到坊市（丹药可带品质）")
    @require_whitelist
    async def handle_market_sell(self, event: AstrMessageEvent, args: GreedyStr):
        async for r in self.auction_handlers.handle_list_item(event, args): yield r
        if get_event_flag(event, "market_sell_success", False):
            tmsg = await self._title_bump(event.get_sender_id(), "market_sell")
            if tmsg:
                yield event.plain_result(tmsg)

    @filter.command("坊市购买", "购买坊市物品")
    @require_whitelist
    async def handle_market_buy(self, event: AstrMessageEvent, auction_id: str = "0"):
        async for r in self.auction_handlers.handle_buy(event, auction_id): yield r

    @filter.command("坊市下架", "下架坊市物品")
    @require_whitelist
    async def handle_market_cancel(self, event: AstrMessageEvent, auction_id: str = "0"):
        async for r in self.auction_handlers.handle_cancel(event, auction_id): yield r

    @filter.command("我的坊市", "查看我的坊市上架与NPC问价")
    @require_whitelist
    async def handle_my_market(self, event: AstrMessageEvent):
        async for r in self.auction_handlers.handle_my_market(event): yield r

    @filter.command("坊市我的", "查看我的坊市上架与NPC问价")
    @require_whitelist
    async def handle_my_market_alias(self, event: AstrMessageEvent):
        async for r in self.auction_handlers.handle_my_market(event): yield r

    @filter.command("坊市卖出", "接受NPC问价并卖出商品")
    @require_whitelist
    async def handle_market_accept_offer(self, event: AstrMessageEvent, offer_id: str = "0"):
        async for r in self.auction_handlers.handle_accept_offer(event, offer_id): yield r

    @filter.command("坊市等待", "拒绝本次NPC问价，继续等待")
    @require_whitelist
    async def handle_market_decline_offer(self, event: AstrMessageEvent, offer_id: str = "0"):
        async for r in self.auction_handlers.handle_decline_offer(event, offer_id): yield r

    # ===== v6.35 拍卖会 =====
    @filter.command("拍卖会", "查看拍卖会状态与拍品预告")
    @require_whitelist
    async def handle_auction_house_status(self, event: AstrMessageEvent):
        async for r in self.auction_house_handlers.handle_status(event): yield r

    @filter.command("拍卖会信息", "查看拍卖会状态与拍品预告")
    @require_whitelist
    async def handle_auction_house_status_alias(self, event: AstrMessageEvent):
        async for r in self.auction_house_handlers.handle_status(event): yield r

    @filter.command("拍卖会报名", "缴纳灵石报名拍卖会")
    @require_whitelist
    async def handle_auction_house_register(self, event: AstrMessageEvent):
        async for r in self.auction_house_handlers.handle_register(event): yield r

    @filter.command("拍卖会入场", "缴纳灵石报名拍卖会")
    @require_whitelist
    async def handle_auction_house_register_alias(self, event: AstrMessageEvent):
        async for r in self.auction_house_handlers.handle_register(event): yield r

    @filter.command("拍卖会出价", "对当前拍品出价")
    @require_whitelist
    async def handle_auction_house_bid(self, event: AstrMessageEvent, amount: GreedyStr = ""):
        async for r in self.auction_house_handlers.handle_bid(event, amount): yield r

    @filter.command("拍卖会领取", "补领拍卖会成交拍品")
    @require_whitelist
    async def handle_auction_house_claim(self, event: AstrMessageEvent):
        async for r in self.auction_house_handlers.handle_claim(event): yield r

    # ===== v6.8 灵田扩展：施肥与杂交 =====
    @filter.command("施肥", "消耗灵石恢复灵田肥力")
    @require_whitelist
    @idle_player_lock
    async def handle_fertilize(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途，请先发送「我要修仙」。")
            return
        success, msg = await self.spirit_farm_mgr.fertilize(player)
        yield event.plain_result(msg)

    @filter.command("杂交", "将两种灵草杂交出新品种")
    @require_whitelist
    @idle_player_lock
    async def handle_hybridize(self, event: AstrMessageEvent, herb_a: str = "", herb_b: str = ""):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途，请先发送「我要修仙」。")
            return
        success, msg = await self.spirit_farm_mgr.hybridize(player, herb_a, herb_b)
        yield event.plain_result(msg)

    # ===== v6.8 储物戒空间附魔 =====
    @filter.command("储物戒附魔", "消耗空间石永久扩充储物戒容量")
    @require_whitelist
    @idle_player_lock
    async def handle_ring_enchant(self, event: AstrMessageEvent, stones: str = "1"):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途，请先发送「我要修仙」。")
            return
        try:
            stone_count = int(stones)
        except ValueError:
            yield event.plain_result("❌ 请输入有效的空间石数量，例如：/储物戒附魔 2")
            return
        success, msg = await self.storage_ring_mgr.enchant_ring(player, stone_count)
        yield event.plain_result(msg)

    # ===== v6.8 押镖与劫道 =====
    @filter.command("押镖", "接一趟灵石镖（期间可被劫镖）")
    @require_whitelist
    @with_player_lock
    async def handle_start_escort(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途，请先发送「我要修仙」。")
            return
        user_cd = await self.db.ext.get_user_cd(player.user_id)
        if user_cd and user_cd.type != UserStatus.IDLE:
            from .models_extended import UserStatus as _US
            yield event.plain_result(f"❌ 你当前正{_US.get_name(user_cd.type)}，无法押镖！")
            return
        success, msg, _ = await self.escort_mgr.start_escort(player)
        if success:
            player_name = player.user_name or str(player.user_id)[:8]
            await self._broadcast_world_boss_spawn(
                f"🛡️ 修士【{player_name}】接下了一趟灵石镖，正行走于官道之上...\n"
                f"💡 可消耗【迷神香】发送 /劫镖 @此人 截取镖银（天道压制·每日每人最多被劫一次）！"
            )
            await self.auto_proxy_mgr.note_activity_started(
                player.user_id, str(getattr(event, "unified_msg_origin", "") or "")
            )
        yield event.plain_result(msg)

    @filter.command("完成押镖", "押镖时间结束后结算镖银")
    @require_whitelist
    @with_player_lock
    async def handle_finish_escort(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        success, msg = await self.escort_mgr.finish_escort(player)
        if success:
            await self.db.ext.add_task_progress(player.user_id, "escort", 1)
            await self._title_bump(player.user_id, "escort_count")
            has_progress, bounty_msg = await self.bounty_mgr.add_bounty_progress(player, "escort", 1)
            if has_progress: msg += bounty_msg
        yield event.plain_result(msg)

    @filter.command("放弃押镖", "扔下镖车跑路（无收益）")
    @require_whitelist
    @with_player_lock
    async def handle_quit_escort(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        success, msg = await self.escort_mgr.quit_escort(player)
        if success:
            msg += await self._auto_proxy_manual_exit(event)
        yield event.plain_result(msg)

    @filter.command("劫镖", "消耗迷神香截取他人镖银（天道压制PVP·每日一劫）")
    @require_whitelist
    @idle_player_lock
    async def handle_rob_escort(self, event: AstrMessageEvent, target: str = ""):
        from astrbot.api.all import At
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        target_id = None
        if hasattr(event, "message_obj") and event.message_obj:
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    target_id = str(comp.qq)
                    break
        if not target_id and target.strip().lstrip("@").isdigit():
            target_id = target.strip().lstrip("@")
        if not target_id:
            yield event.plain_result("❌ 请使用 @某人 指定你要劫镖的目标。")
            return
        success, msg = await self.escort_mgr.rob_escort(player, target_id)
        yield event.plain_result(msg)
        # 称号：业力（劫镖为恶）
        try:
            p2 = await self.db.get_player_by_id(event.get_sender_id())
            if p2 and (p2.karma or 0) < 0:
                tmsg = await self.title_hooks.bump_value(p2, "karma_neg", p2.karma)
                if tmsg: yield event.plain_result(tmsg)
        except Exception:
            pass

    # ===== v6.8 奇遇选择 =====
    @filter.command("奇遇选择", "对当前奇遇事件做出抉择")
    @require_whitelist
    @with_player_lock
    async def handle_encounter_choice(self, event: AstrMessageEvent, choice: str = "0"):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        try:
            choice_int = int(choice)
        except ValueError:
            yield event.plain_result("❌ 请输入选项编号，例如：/奇遇选择 1")
            return
        success, msg = await self.encounter_mgr.resolve_choice(player, choice_int)
        yield event.plain_result(msg)

    # ===== v6.10 功德因果 =====
    @filter.command("行善", "捐献灵石积累功德（每日一次）")
    @require_whitelist
    @idle_player_lock
    async def handle_do_good(self, event: AstrMessageEvent):
        import time as _time
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        today = _time.strftime("%Y-%m-%d")
        key = f"dogood_{player.user_id}"
        last = await self.db.ext.get_system_config(key)
        if last == today:
            yield event.plain_result("❌ 你今日已经行过善了，明日再来。")
            return
        cost = 2000
        if player.gold < cost:
            yield event.plain_result(f"❌ 行善需要捐献 {cost:,} 灵石（为贫苦散修购置丹药）。")
            return
        player.gold -= cost
        player.karma = (getattr(player, "karma", 0) or 0) + 1
        await self.db.update_player(player)
        await self.db.ext.set_system_config(key, today)
        # 称号：功德
        try:
            tmsg = await self.title_hooks.bump_value(player, "karma", player.karma)
            if tmsg: yield event.plain_result(tmsg)
        except Exception:
            pass
        yield event.plain_result(
            "🪷 行善积德！\n"
            "━━━━━━━━━━━━━━━\n"
            f"你捐献 {cost:,} 灵石济困扶危。\n"
            f"功德 +1（当前功德 {player.karma}）\n"
            "✨ 功德可提升奇遇概率与突破成功率\n"
            "🌑 而偷窃、劫镖会积累业力，反向影响突破"
        )

    # ===== v6.10 丹炉系统 =====
    @filter.command("升级丹炉", "消耗灵石升级丹炉（每级炼丹成功率+3%）")
    @require_whitelist
    @idle_player_lock
    async def handle_upgrade_furnace(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        success, msg = await self.alchemy_mgr.upgrade_furnace(player)
        yield event.plain_result(msg)
        # 称号：五品炉神
        try:
            if success and self.alchemy_mgr.get_furnace_level(player) >= 5:
                tmsg = await self.title_hooks.bump_value(player, "furnace_max", 5)
                if tmsg: yield event.plain_result(tmsg)
        except Exception:
            pass

    # ===== v6.10 洞天装修 =====
    @filter.command("洞天装修", "购买家具布置洞天")
    @require_whitelist
    @idle_player_lock
    async def handle_buy_furniture(self, event: AstrMessageEvent, name: str = ""):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        if not name.strip():
            name = ""  # 走底层的家具清单展示
        success, msg = await self.blessed_land_mgr.buy_furniture(player, name.strip())
        yield event.plain_result(msg)

    # ===== v6.10 宗门建筑 =====
    @filter.command("宗门建筑", "查看宗门建筑")
    @require_whitelist
    async def handle_sect_buildings(self, event: AstrMessageEvent):
        success, msg = await self.sect_mgr.show_buildings(event.get_sender_id())
        yield event.plain_result(msg)

    @filter.command("升级建筑", "升级宗门建筑（宗主/长老，消耗宗门资金）")
    @require_whitelist
    @with_player_lock
    async def handle_upgrade_building(self, event: AstrMessageEvent, name: str = ""):
        if not name.strip():
            # 空参时先展示建筑面板
            success, msg = await self.sect_mgr.show_buildings(event.get_sender_id())
            if not success:
                yield event.plain_result(msg)
            else:
                yield event.plain_result(msg + "\n\n💡 升级请输入：/升级建筑 <建筑名>")
            return
        success, msg = await self.sect_mgr.upgrade_building(event.get_sender_id(), name.strip())
        yield event.plain_result(msg)

    # ===== v6.10 生活职业 =====
    @filter.command("选择生活职业", "选择专精：炼器师/符箓师/阵法师/炼丹师")
    @require_whitelist
    @idle_player_lock
    async def handle_choose_life_skill(self, event: AstrMessageEvent, skill: str = ""):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        success, msg = await self.life_skill_mgr.choose_skill(player, skill.strip())
        yield event.plain_result(msg)
    @filter.command("符箓", "查看符箓体系与绘制配方")
    @require_whitelist
    @with_player_lock
    async def handle_talisman_list(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        yield event.plain_result(self.talisman_mgr.list_talismans(player))

    @filter.command("制符", "绘制符箓；不带参数领取符箓师每日引雷符")
    @require_whitelist
    @idle_player_lock
    async def handle_draw_talisman(self, event: AstrMessageEvent, args: GreedyStr = ""):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        tokens = (args or "").strip().split()
        if not tokens:
            success, msg = await self.life_skill_mgr.daily_talisman(player)
            yield event.plain_result(msg)
            return
        name = tokens[0]
        count = 1
        if len(tokens) > 1:
            try:
                count = int(tokens[1])
            except ValueError:
                yield event.plain_result("❌ 数量必须是数字，例如：/制符 寻宝符 2")
                return
        success, msg = await self.talisman_mgr.craft_talisman(player, name, count)
        yield event.plain_result(msg)

    @filter.command("使用符箓", "使用符箓获得战斗/炼丹/探索加成")
    @require_whitelist
    @idle_player_lock
    async def handle_use_talisman(self, event: AstrMessageEvent, args: GreedyStr = ""):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        tokens = (args or "").strip().split()
        if not tokens:
            yield event.plain_result("❌ 请指定符箓名称，例如：/使用符箓 寻宝符 1")
            return
        name = tokens[0]
        count = 1
        if len(tokens) > 1:
            try:
                count = int(tokens[1])
            except ValueError:
                yield event.plain_result("❌ 数量必须是数字，例如：/使用符箓 寻宝符 2")
                return
        success, msg = await self.talisman_mgr.use_talisman(player, name, count)
        yield event.plain_result(msg)

    # ===== v6.10 魔潮天灾 =====
    @filter.command("开启魔潮", "管理员开启全服天灾事件")
    @require_whitelist
    async def handle_start_tide(self, event: AstrMessageEvent, hours: str = "12"):
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有权限！")
            return
        try:
            h = max(1, min(48, int(hours)))
        except ValueError:
            h = 12
        success, msg = await self.calamity_mgr.start_tide(h)
        if success:
            await self._broadcast_world_boss_spawn(msg)
        yield event.plain_result(msg)

    @filter.command("魔潮状态", "查看魔潮抵御进度")
    @require_whitelist
    async def handle_tide_status(self, event: AstrMessageEvent):
        yield event.plain_result(await self.calamity_mgr.get_status())

    @filter.command("抵御魔潮", "捐献灵石抵御魔潮")
    @require_whitelist
    @with_player_lock
    async def handle_resist_tide(self, event: AstrMessageEvent, amount: str = "0"):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        try:
            amt = int(amount)
        except ValueError:
            yield event.plain_result("❌ 请输入正确的灵石数量，例如：/抵御魔潮 5000")
            return
        success, msg = await self.calamity_mgr.resist(player, amt)
        if "成功抵御" in msg:
            await self._broadcast_world_boss_spawn(
                msg.split("\n\n💰 你本次捐献")[0]
            )
        if success:
            has_progress, bounty_msg = await self.bounty_mgr.add_bounty_progress(
                player, "tide", 1
            )
            if has_progress:
                msg += bounty_msg
        yield event.plain_result(msg)
        # 称号：魔潮抵御次数
        try:
            if success:
                tmsg = await self._title_bump(event.get_sender_id(), "tide_resist")
                if tmsg: yield event.plain_result(tmsg)
        except Exception:
            pass

    # ===== v6.11 管理员工具 =====
    # ===== v6.14 季节系统 =====
    @filter.command("季节", "查看当前灵田季节与加成")
    @require_whitelist
    async def handle_season_info(self, event: AstrMessageEvent):
        from .managers.spirit_farm_manager import SEASON_EFFECTS, get_current_season_index
        season = SEASON_EFFECTS[get_current_season_index()]
        idx = get_current_season_index()
        order = ["春", "夏", "秋", "冬"]
        lines = [
            f"{season['icon']} 当前季节：【{season['name']}】",
            "━━━━━━━━━━━━━━━",
            season["desc"],
            "━━━━━━━━━━━━━━━",
            "四季轮转表：",
            f"  🌱 春·生发：生长+33%，产量+10%",
            f"  ☀️ 夏·炎灼：阳性灵草（火焰花/血灵草）生长+33%",
            f"  🍂 秋·丰收：全灵草产量+25%",
            f"  ❄️ 冬·蛰藏：生长放缓50%，冰心草不受影响",
            "━━━━━━━━━━━━━━━",
            f"季节按现实时间每约一周自动轮转（当前：{order[idx]}季）",
            "💡 发送 /种植 <灵草名> 把握季节加成",
        ]
        yield event.plain_result("\n".join(lines))

    # ===== v6.13 化身托管 =====
    @filter.command("激活化身", "消耗化身傀儡，30天内闭关收益+20%")
    @require_whitelist
    @idle_player_lock
    async def handle_activate_avatar(self, event: AstrMessageEvent):
        import json as _json
        import time as _time
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return

        try:
            bonus = _json.loads(player.permanent_bonus) if player.permanent_bonus else {}
        except Exception:
            bonus = {}
        avatar_until = int(bonus.get("avatar_until", 0))
        if avatar_until > int(_time.time()):
            days = (avatar_until - int(_time.time())) // 86400
            yield event.plain_result(f"❌ 你的化身仍在行功中（剩余约 {days} 天），无需重复激活。")
            return

        if not self.storage_ring_mgr.has_item(player, "化身傀儡", 1):
            yield event.plain_result(
                "❌ 你的储物戒中没有【化身傀儡】。\n"
                "💡 化身傀儡为天阶稀有物品，可留意百宝阁刷新。"
            )
            return

        success, msg = await self.storage_ring_mgr.retrieve_item(player, "化身傀儡", 1)
        if not success:
            yield event.plain_result(f"❌ 消耗化身傀儡失败：{msg}")
            return

        # retrieve 已同步物品数据到 player；保留其他加成，写入 avatar_until
        try:
            bonus = _json.loads(player.permanent_bonus) if player.permanent_bonus else {}
        except Exception:
            bonus = {}
        bonus["avatar_until"] = int(_time.time()) + 30 * 86400
        player.permanent_bonus = _json.dumps(bonus, ensure_ascii=False)
        await self.db.update_player(player)

        yield event.plain_result(
            "🤖 化身激活成功！\n"
            "━━━━━━━━━━━━━━━\n"
            "你以秘法唤醒化身傀儡，它将替你昼夜行功。\n"
            "⏳ 有效期：30 天\n"
            "📈 效果：闭关收益 +20%（与其他加成叠加）\n"
            "消耗：化身傀儡 x1"
        )

    # ===== v6.16 炼丹火候 =====
    @filter.command("火候", "炼丹时选择火候（猛火/文火/守中）")
    @require_whitelist
    async def handle_fire_control(self, event: AstrMessageEvent, choice: str = ""):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        if not choice.strip():
            yield event.plain_result(
                "🔥 火候选择\n"
                "━━━━━━━━━━━━━━━\n"
                "  [1] 猛火急攻 —— 成功率-8%，极品概率大增\n"
                "  [2] 文火慢煨 —— 成功率+6%，难出极品\n"
                "  [3] 控火守中 —— 一切如常\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 先 /炼丹 <ID> 投料，再 /火候 <编号>；开炉时再 /炼丹 <同一ID>"
            )
            return
        success, msg = await self.alchemy_mgr.set_fire_choice(event.get_sender_id(), choice.strip())
        yield event.plain_result(msg)

    # ===== v6.16 仙界日历 =====
    @filter.command("仙界日历", "查看季节与全服事件预告")
    @require_whitelist
    async def handle_calendar(self, event: AstrMessageEvent):
        yield event.plain_result(await self.calamity_mgr.get_calendar())

    # ===== v6.16 NPC道侣（仙灵结缘）=====
    @filter.command("仙灵结缘", "查看仙灵名单或与仙灵结为道侣")
    @require_whitelist
    @idle_player_lock
    async def handle_spirit_bond(self, event: AstrMessageEvent, name: str = ""):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        if not name.strip():
            yield event.plain_result(await self.social_mgr.list_companions())
            return
        success, msg = await self.social_mgr.bond_companion(player, name.strip())
        yield event.plain_result(msg)

    @filter.command("仙灵共修", "每日与仙灵道侣共修（修为+情缘）")
    @require_whitelist
    @idle_player_lock
    async def handle_spirit_commune(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        success, msg = await self.social_mgr.daily_commune(player)
        yield event.plain_result(msg)

    @filter.command("仙灵解除", "与仙灵解除道侣关系（情缘清零）")
    @require_whitelist
    @idle_player_lock
    async def handle_spirit_break(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        success, msg = await self.social_mgr.break_companion(player)
        yield event.plain_result(msg)

    @filter.command("仙灵代收", "仙灵道侣代收洞天闲置产出（消耗1情缘）")
    @require_whitelist
    @with_player_lock
    async def handle_spirit_collect(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        success, msg = await self.social_mgr.companion_collect_land(player)
        yield event.plain_result(msg)

    # ===== v6.16 师徒系统 =====
    @filter.command("拜师", "拜高境界道友为师")
    @require_whitelist
    @idle_player_lock
    async def handle_take_master(self, event: AstrMessageEvent, target: str = ""):
        from astrbot.api.all import At
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        master_id = None
        if hasattr(event, "message_obj") and event.message_obj:
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    master_id = str(comp.qq)
                    break
        if not master_id and target.strip().lstrip("@").isdigit():
            master_id = target.strip().lstrip("@")
        if not master_id:
            yield event.plain_result(
                "🎓 拜师\n━━━━━━━━━━━━━━━\n"
                "用法：/拜师 @高境界道友\n"
                "条件：对方境界须高于你 5 级，且门下未满（2人）\n"
                "收益：师父带你成长，出师双方获大礼"
            )
            return
        success, msg = await self.social_mgr.take_as_disciple(player, master_id)
        yield event.plain_result(msg)

    @filter.command("出师", "为达到筑基的弟子举办出师礼（师父操作）")
    @require_whitelist
    @idle_player_lock
    async def handle_graduate(self, event: AstrMessageEvent, target: str = ""):
        from astrbot.api.all import At
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        disciple_id = None
        if hasattr(event, "message_obj") and event.message_obj:
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    disciple_id = str(comp.qq)
                    break
        if not disciple_id and target.strip().lstrip("@").isdigit():
            disciple_id = target.strip().lstrip("@")
        if not disciple_id:
            yield event.plain_result("用法：/出师 @你的弟子（弟子需达筑基期）")
            return
        success, msg = await self.social_mgr.graduate_disciple(player, disciple_id)
        yield event.plain_result(msg)

    @filter.command("逐出师门", "将弟子逐出师门（师父操作）")
    @require_whitelist
    @idle_player_lock
    async def handle_dismiss(self, event: AstrMessageEvent, target: str = ""):
        from astrbot.api.all import At
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        disciple_id = None
        if hasattr(event, "message_obj") and event.message_obj:
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    disciple_id = str(comp.qq)
                    break
        if not disciple_id and target.strip().lstrip("@").isdigit():
            disciple_id = target.strip().lstrip("@")
        if not disciple_id:
            yield event.plain_result("用法：/逐出师门 @你的弟子")
            return
        success, msg = await self.social_mgr.dismiss_disciple(player, disciple_id)
        yield event.plain_result(msg)

    @filter.command("师徒", "查看我的师徒关系与师德点")
    @require_whitelist
    async def handle_master_info(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        yield event.plain_result(await self.social_mgr.my_master_info(player))

    @filter.command("师德兑换", "用师德点兑换奖励")
    @require_whitelist
    @idle_player_lock
    async def handle_virtue_redeem(self, event: AstrMessageEvent, item_id: str = "0"):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        try:
            idx = int(item_id)
        except ValueError:
            idx = 0
        success, msg = await self.social_mgr.redeem_virtue(player, idx)
        if success and msg.startswith("__NEED_STORE__"):
            # 物品类奖励：存入储物戒
            _, payload = msg.split("__NEED_STORE__", 1)
            item_name, count, note = payload.split("|", 2)
            ok, smsg = await self.storage_ring_mgr.store_item(player, item_name, int(count), silent=True)
            if ok:
                yield event.plain_result(f"✅ {note}，【{item_name}】x{count} 已存入储物戒")
            else:
                yield event.plain_result(f"⚠️ {note}，但储物戒已满：{smsg}")
            return
        yield event.plain_result(msg)

    # ===== v6.16 排行扩展 =====
    @filter.command("功德榜", "查看功德与业力排行")
    @require_whitelist
    async def handle_karma_rank(self, event: AstrMessageEvent):
        success, msg = await self.rank_mgr.get_karma_ranking()
        yield event.plain_result(msg)

    @filter.command("气运榜", "查看全服气运之子")
    @require_whitelist
    async def handle_luck_rank(self, event: AstrMessageEvent):
        success, msg = await self.rank_mgr.get_luck_ranking()
        yield event.plain_result(msg)

    @filter.command("气运", "查看自身气运明细与获取途径")
    @require_whitelist
    async def handle_luck_info(self, event: AstrMessageEvent):
        from .managers.luck_manager import LuckManager
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
        parts = LuckManager.get_breakdown(player, self.config_manager)
        total = parts["total"]
        trigger = LuckManager.percent_bonus(player, 0.5, 20, self.config_manager)
        encounter = LuckManager.percent_bonus(player, 0.25, 15, self.config_manager)
        breakthrough = LuckManager.percent_bonus(player, 0.2, 10, self.config_manager)
        drop = LuckManager.percent_bonus(player, 0.4, 20, self.config_manager)
        reward = LuckManager.percent_bonus(player, 0.3, 30, self.config_manager)
        msg = (
            "🍀 气运详情\n"
            "━━━━━━━━━━━━━━━\n"
            f"本体气运：{parts['base']}\n"
            f"称号加成：{parts['title']:+d}\n"
            f"装备词条：{parts['affix']:+d}\n"
            f"符箓加成：{parts['talisman']:+d}\n"
            f"功法加成：{parts.get('technique', 0):+d}\n"
            f"当前总气运：{total}\n"
            "━━━━━━━━━━━━━━━\n"
            f"奇遇触发：+{trigger:.1f}%\n"
            f"奇遇成功率：+{encounter:.1f}%\n"
            f"突破成功率：+{breakthrough:.1f}%\n"
            f"历练/秘境掉落：+{drop:.1f}%\n"
            f"历练/秘境收益：+{reward:.1f}%\n"
            "━━━━━━━━━━━━━━━\n"
            "💡 获取途径：\n"
            "  · 历练/秘境归来触发奇遇，抉择成功可积累气运\n"
            "  · 洗练装备可出「气运」词条\n"
            "  · 使用探索/增益类神符可临时提升气运\n"
            "  · 收集提供气运加成的称号\n"
            "  · 装备带「气运」词条的普通功法"
        )
        yield event.plain_result(msg)

    @filter.command("师德榜", "查看传道真人排行")
    @require_whitelist
    async def handle_virtue_rank(self, event: AstrMessageEvent):
        success, msg = await self.rank_mgr.get_virtue_ranking()
        yield event.plain_result(msg)

        # ===== v6.17 称号系统 =====
    @filter.command("称号图鉴", "查看全部称号与收集进度")
    @require_whitelist
    @with_player_lock
    async def handle_title_collection(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
            
        try:
            # 1. 触发历史称号校验补齐
            sync_msg = await self.title_mgr.sync_missing_titles(player)
            
            # 2. 渲染图鉴
            text = self.title_mgr.collection_view(player)
            
            # 3. 如果有补发，附加提示
            if sync_msg:
                text += sync_msg
        except Exception as e:
            logger.exception(f"称号图鉴渲染失败 uid={player.user_id}: {e}")
            text = "❌ 称号图鉴读取失败，请稍后再试。"
            
        # 修复了原代码这里重复调用 collection_view 导致兜底失效的 Bug
        yield event.plain_result(text)

    @filter.command("佩戴称号", "更换展示的称号（加成自动按最高品阶生效）")
    @require_whitelist
    @with_player_lock
    async def handle_title_equip(self, event: AstrMessageEvent, name: str = ""):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
            
        # equip_display 内部已经做了去空格、去@符号、自动推荐最高品阶等容错处理
        success, msg = self.title_mgr.equip_display(player, name)
        if success:
            await self.db.update_player(player)
        yield event.plain_result(msg)

    @filter.command("我的称号", "查看当前拥有的所有称号")
    @require_whitelist
    @with_player_lock
    async def handle_my_title(self, event: AstrMessageEvent):
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("❌ 道友尚未踏入仙途。")
            return
            
        try:
            # 1. 触发历史称号校验补齐
            sync_msg = await self.title_mgr.sync_missing_titles(player)
            
            # 2. 渲染拥有的全量称号面板
            res = self.title_mgr.my_titles_summary(player)
            
            # 3. 如果有补发，附加提示
            if sync_msg:
                res += sync_msg
        except Exception as e:
            logger.exception(f"我的称号渲染失败 uid={player.user_id}: {e}")
            res = "❌ 称号数据读取失败，请稍后再试。"
            
        yield event.plain_result(res)

    @filter.command("仙界管理", "管理员工具：查玩家/发灵石/重置状态/公告")
    @require_whitelist
    async def handle_admin_tool(self, event: AstrMessageEvent):
        if not self._check_boss_admin(event):
            yield event.plain_result("❌ 你没有管理员权限！")
            return

        # 1. 尝试获取被 @ 的玩家 ID
        at_id = None
        if hasattr(event, "message_obj") and event.message_obj:
            from astrbot.api.all import At
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    at_id = str(comp.qq)
                    break
                elif isinstance(comp, str) and "[CQ:at,qq=" in comp:
                    import re
                    m = re.search(r"\[CQ:at,qq=(\d+)\]", comp)
                    if m:
                        at_id = m.group(1)
                        break

        # 2. 暴力提取纯文本，防止 @ 组件剥离导致错位
        try:
            raw_text = event.get_plain_text()
        except AttributeError:
            raw_text = event.get_message_str() or ""

        idx = raw_text.find("仙界管理")
        if idx != -1:
            args_str = raw_text[idx + 4:].strip()
        else:
            args_str = raw_text.strip()

        parts = args_str.split()
        clean_parts = [p for p in parts if p.strip()]

        if not clean_parts:
            yield event.plain_result("❌ 请提供子指令。例如：/仙界管理 发灵石 12345 1000")
            return
            
        sub = clean_parts[0]
        arg1 = clean_parts[1] if len(clean_parts) > 1 else ""
        arg2 = clean_parts[2] if len(clean_parts) > 2 else ""

        # ================= 查玩家 =================
        if sub == "查玩家":
            target = None
            # 兼容 @查询
            if at_id and not arg1:
                target = await self.db.get_player_by_id(at_id)
            elif arg1:
                if arg1.isdigit() or (arg1.startswith("@") and arg1[1:].isdigit()):
                    target = await self.db.get_player_by_id(arg1.lstrip("@"))
                else:
                    target = await self.db.get_player_by_name(arg1)
            
            if not target:
                yield event.plain_result(f"❌ 未找到该玩家。")
                return
                
            user_cd = await self.db.ext.get_user_cd(target.user_id)
            loan = await self.db.ext.get_active_loan(target.user_id)
            status = UserStatus.get_name(user_cd.type) if user_cd else "空闲"
            yield event.plain_result(
                f"🔍 玩家数据\n"
                f"━━━━━━━━━━━━━━━\n"
                f"ID：{target.user_id}\n"
                f"道号：{target.user_name or '无'}\n"
                f"职业/境界：{target.cultivation_type} / {target.get_level(self.config_manager)}\n"
                f"修为：{target.experience:,}\n"
                f"灵石：{target.gold:,}\n"
                f"功德/业力：{getattr(target, 'karma', 0):+d}\n"
                f"生活职业：{getattr(target, 'life_skill', '') or '无'}\n"
                f"状态：{status} / {target.state}\n"
                f"转生次数：{target.reincarnation_count}\n"
                f"贷款：{('未还本金 ' + format(max(0, loan['principal'] - loan.get('paid_principal', 0)), ',') + ' 灵石') if loan else '无'}"
            )
            return

        # ================= 发灵石 =================
        elif sub == "发灵石":
            target_id = ""
            amount_str = ""
            
            if arg1 == "全体":
                target_id = "全体"
                amount_str = arg2
            elif at_id:
                # 如果有 @，说明是给被艾特的人发
                target_id = at_id
                # 不管 @ 有没有被文本解析吃掉，参数最后一位必然是金额
                if len(clean_parts) >= 2:
                    amount_str = clean_parts[-1]
            elif arg1.isdigit() or (arg1.startswith("@") and arg1[1:].isdigit()):
                # 纯数字 ID
                target_id = arg1.lstrip("@")
                amount_str = arg2
            elif arg1:
                # 尝试按道号查询
                target = await self.db.get_player_by_name(arg1)
                if target:
                    target_id = target.user_id
                    amount_str = arg2
                else:
                    yield event.plain_result(f"❌ 未找到玩家【{arg1}】。")
                    return
            
            if not target_id or not amount_str:
                yield event.plain_result("❌ 参数格式错误。用法：/仙界管理 发灵石 <玩家ID/道号/@玩家/全体> <数量>")
                return
                
            try:
                amount = int(amount_str)
            except ValueError:
                yield event.plain_result("❌ 灵石数量必须是整数。")
                return

            # --- 全体发放逻辑 ---
            if target_id == "全体":
                await self.db.conn.execute("UPDATE players SET gold = MAX(0, gold + ?)", (amount,))
                await self.db.conn.commit()
                sign = "+" if amount >= 0 else ""
                yield event.plain_result(f"✅ 已向 **全体玩家** 发放 {sign}{amount:,} 灵石。")
                return
                
            # --- 单人发放逻辑 ---
            else:
                target = await self.db.get_player_by_id(target_id)
                if not target:
                    yield event.plain_result(f"❌ 数据库中未找到该玩家（ID: {target_id}）。")
                    return
                
                await self.db.add_player_gold(target_id, amount)
                sign = "+" if amount >= 0 else ""
                yield event.plain_result(f"✅ 已向玩家 {target_id}（{target.user_name or '无名'}）{sign}{amount:,} 灵石。")
                return

        # ================= 重置状态 =================
        elif sub == "重置状态":
            target_id = at_id if at_id else arg1.lstrip("@")
            if not target_id:
                yield event.plain_result("❌ 请指定要重置状态的玩家。")
                return
                
            target = await self.db.get_player_by_id(target_id)
            if not target:
                yield event.plain_result(f"❌ 未找到玩家 {target_id}。")
                return
                
            async with get_player_lock(target_id):
                await self.db.ext.set_user_busy(target_id, UserStatus.IDLE, 0, extra_data={})
                await self.db.update_player_fields(target_id, state="空闲", cultivation_start_time=0)
                await self.auto_proxy_mgr.disable(target_id, "admin_reset")
            yield event.plain_result(f"✅ 已重置玩家 {target_id}（{target.user_name or '无名'}）的卡死状态。")
            return

        # ================= 公告 =================
        elif sub == "公告":
            # 公告直接把后面的所有内容拼接
            text = " ".join(clean_parts[1:]).strip()
            if not text:
                yield event.plain_result("用法：/仙界管理 公告 <文本>")
                return
            await self._broadcast_world_boss_spawn(f"📜【仙界公告】\n{text}")
            yield event.plain_result("✅ 公告已广播。")
            return

        yield event.plain_result(
            "🛠️ 仙界管理工具\n"
            "━━━━━━━━━━━━━━━\n"
            "/仙界管理 查玩家 <ID/道号/@玩家>\n"
            "/仙界管理 发灵石 <ID/道号/@玩家/全体> <数量>\n"
            "/仙界管理 重置状态 <ID/@玩家>\n"
            "/仙界管理 公告 <文本>"
        )