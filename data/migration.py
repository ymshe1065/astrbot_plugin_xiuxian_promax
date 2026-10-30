# data/migration.py

from __future__ import annotations

import aiosqlite
import json
from typing import Dict, Callable, Awaitable, TYPE_CHECKING
from astrbot.api import logger

if TYPE_CHECKING:
    from ..config_manager import ConfigManager

LATEST_DB_VERSION = 53  # v44: 装备平衡货架失效；v45: 灵气与 mp 兼容同步；v46: 战斗小事件；v47: 拍卖会事件；v48: 每日切磋/决斗次数与切磋请求；v49: 统一物品品阶名称；v50: 仙市投资系统（基金/股票）；v51: 符箓熟练度与符图习得；v52: 仙市 v6（16 盘 + 赌场级波动 + 不对称日熔断）；v53: 拍卖会未开拍拍品按固定品阶曲线重建（并兼容分叉旧库补授符图）

MIGRATION_TASKS: Dict[int, Callable[[aiosqlite.Connection, ConfigManager], Awaitable[None]]] = {}

def migration(version: int):
    """注册数据库迁移任务的装饰器"""
    def decorator(func: Callable[[aiosqlite.Connection, ConfigManager], Awaitable[None]]):
        MIGRATION_TASKS[version] = func
        return func
    return decorator

async def _add_column_if_missing(conn: aiosqlite.Connection, table: str, column: str, ddl_type: str):
    """幂等加列：全新建表已包含最新列时，增量迁移的 ALTER 不能因重复列而失败"""
    if await _column_exists(conn, table, column):
        return
    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


async def _column_exists(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    """判断表里是否已有某列（用于区分分叉旧库与全新库）"""
    async with conn.execute(f"PRAGMA table_info({table})") as cursor:
        existing = {row[1] for row in await cursor.fetchall()}
    return column in existing


async def _ensure_sect_v30_tables(conn: aiosqlite.Connection):
    """创建 v30 宗门拓展表（新库/旧库共用）。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sect_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sect_id INTEGER NOT NULL,
            issuer_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            order_type TEXT NOT NULL,
            target_value INTEGER NOT NULL DEFAULT 0,
            progress INTEGER NOT NULL DEFAULT 0,
            penalty_gold INTEGER NOT NULL DEFAULT 500,
            created_at INTEGER NOT NULL DEFAULT 0,
            deadline INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            origin TEXT NOT NULL DEFAULT ''
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sect_orders_target ON sect_orders(target_id, status)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sect_orders_deadline ON sect_orders(status, deadline)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sect_tournaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sect_id INTEGER NOT NULL,
            host_id TEXT NOT NULL,
            origin TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'registering',
            registration_end INTEGER NOT NULL DEFAULT 0,
            started_at INTEGER NOT NULL DEFAULT 0,
            finished_at INTEGER NOT NULL DEFAULT 0,
            winner_id TEXT NOT NULL DEFAULT '',
            bracket_json TEXT NOT NULL DEFAULT '[]'
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sect_tournaments_status ON sect_tournaments(status, registration_end)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sect_tournament_entries (
            tournament_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            seed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tournament_id, user_id)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sect_wars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attacker_id INTEGER NOT NULL,
            defender_id INTEGER NOT NULL,
            winner_id INTEGER NOT NULL DEFAULT 0,
            gold_taken INTEGER NOT NULL DEFAULT 0,
            log_json TEXT NOT NULL DEFAULT '[]',
            created_at INTEGER NOT NULL DEFAULT 0,
            origin TEXT NOT NULL DEFAULT ''
        )
    """)


async def _ensure_npc_v29_tables(conn: aiosqlite.Connection):
    """创建 v29 NPC 道侣/师徒表（新库/旧库共用）。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS npc_partners (
            user_id TEXT PRIMARY KEY,
            npc_id TEXT NOT NULL,
            bond INTEGER NOT NULL DEFAULT 0,
            daily_time INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS master_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            master_id TEXT NOT NULL,
            disciple_id TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL DEFAULT 0,
            graduated INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_master_relations_master ON master_relations(master_id)")


async def _ensure_auction_npc_v32(conn: aiosqlite.Connection):
    """创建 v32 坊市 NPC 问价表，并补齐上架商品的问价调度列（新库/旧库共用）。"""
    await _add_column_if_missing(conn, "auction_items", "next_offer_time", "INTEGER NOT NULL DEFAULT 0")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS auction_npc_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id INTEGER NOT NULL,
            seller_id TEXT NOT NULL,
            item_name TEXT NOT NULL,
            count INTEGER NOT NULL,
            listed_price INTEGER NOT NULL,
            offer_price INTEGER NOT NULL,
            npc_name TEXT NOT NULL,
            create_time INTEGER NOT NULL,
            expire_time INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            responded_time INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_npc_offer_auction ON auction_npc_offers(auction_id, status)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_npc_offer_seller ON auction_npc_offers(seller_id, status)")


async def _ensure_auto_proxy_v35(conn: aiosqlite.Connection):
    """创建 v35 自动代理表（新库/旧库共用）。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS auto_proxy (
            user_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            task_type TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            config_json TEXT NOT NULL DEFAULT '{}',
            updated_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_auto_proxy_enabled ON auto_proxy(enabled)")


async def _ensure_world_boss_v38(conn: aiosqlite.Connection):
    """创建/补齐 v38 世界Boss与团本相关表列（新库/旧库共用）。"""
    await _add_column_if_missing(conn, "world_boss", "boss_type", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "world_boss", "element", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "world_boss", "settled", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "world_boss", "killer_id", "TEXT NOT NULL DEFAULT ''")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS world_boss_raids (
            raid_id INTEGER PRIMARY KEY AUTOINCREMENT,
            boss_name TEXT NOT NULL,
            boss_type TEXT NOT NULL DEFAULT '',
            element TEXT NOT NULL DEFAULT '',
            difficulty INTEGER NOT NULL DEFAULT 1,
            max_hp INTEGER NOT NULL,
            current_hp INTEGER NOT NULL,
            phase INTEGER NOT NULL DEFAULT 1,
            total_phases INTEGER NOT NULL DEFAULT 1,
            phase_max_hp INTEGER NOT NULL,
            min_level INTEGER NOT NULL DEFAULT 6,
            min_players INTEGER NOT NULL DEFAULT 2,
            status INTEGER NOT NULL DEFAULT 1,
            spawn_time INTEGER NOT NULL,
            expire_time INTEGER NOT NULL,
            settled INTEGER NOT NULL DEFAULT 0,
            total_damage INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_world_boss_raids_status ON world_boss_raids(status, expire_time)"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS world_boss_raid_damage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raid_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            total_damage INTEGER NOT NULL DEFAULT 0,
            attack_count INTEGER NOT NULL DEFAULT 0,
            last_attack_time INTEGER NOT NULL,
            UNIQUE(raid_id, user_id)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_raid_damage ON world_boss_raid_damage(raid_id)"
    )


async def _ensure_battle_events_v46(conn: aiosqlite.Connection):
    """创建江湖战斗小事件表（新库/旧库共用）。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS battle_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
            event_name TEXT NOT NULL,
            tier INTEGER NOT NULL DEFAULT 1,
            element TEXT NOT NULL DEFAULT '',
            max_hp INTEGER NOT NULL,
            current_hp INTEGER NOT NULL,
            min_level INTEGER NOT NULL DEFAULT 0,
            spawn_time INTEGER NOT NULL,
            expire_time INTEGER NOT NULL,
            status INTEGER NOT NULL DEFAULT 1,
            settled INTEGER NOT NULL DEFAULT 0,
            killer_id TEXT NOT NULL DEFAULT ''
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_battle_events_status ON battle_events(status, expire_time)"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS battle_event_damage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            total_damage INTEGER NOT NULL DEFAULT 0,
            attack_count INTEGER NOT NULL DEFAULT 0,
            last_attack_time INTEGER NOT NULL,
            UNIQUE(event_id, user_id)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_battle_event_damage ON battle_event_damage(event_id)"
    )


async def _ensure_trade_v39(conn: aiosqlite.Connection):
    """v39：坊市/赠予支持丹药，记录物品类型与品质。"""
    await _add_column_if_missing(conn, "auction_items", "item_kind", "TEXT NOT NULL DEFAULT 'item'")
    await _add_column_if_missing(conn, "auction_items", "item_quality", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "auction_items", "instance_id", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "pending_gifts", "item_kind", "TEXT NOT NULL DEFAULT 'item'")
    await _add_column_if_missing(conn, "pending_gifts", "item_quality", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "pending_gifts", "instance_id", "INTEGER NOT NULL DEFAULT 0")


async def _ensure_auction_house_v47(conn: aiosqlite.Connection):
    """v47：拍卖会事件、拍品、报名、出价与播报出站表。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS auction_house_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_no INTEGER NOT NULL DEFAULT 0,
            cycle_start INTEGER NOT NULL DEFAULT 0,
            announce_date TEXT NOT NULL DEFAULT '',
            announce_start INTEGER NOT NULL DEFAULT 0,
            event_start INTEGER NOT NULL DEFAULT 0,
            event_end INTEGER NOT NULL DEFAULT 0,
            phase TEXT NOT NULL DEFAULT 'scheduled',
            lot_count INTEGER NOT NULL DEFAULT 0,
            current_lot INTEGER NOT NULL DEFAULT 0,
            last_reminder_slot TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS auction_house_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            lot_no INTEGER NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            preview_label TEXT NOT NULL DEFAULT '',
            item_name TEXT NOT NULL DEFAULT '',
            item_rank TEXT NOT NULL DEFAULT '',
            item_quality TEXT NOT NULL DEFAULT '',
            count INTEGER NOT NULL DEFAULT 1,
            payload_json TEXT NOT NULL DEFAULT '{}',
            start_price INTEGER NOT NULL DEFAULT 0,
            current_price INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            lot_start INTEGER NOT NULL DEFAULT 0,
            bid_deadline INTEGER NOT NULL DEFAULT 0,
            highest_bidder TEXT NOT NULL DEFAULT '',
            highest_bid INTEGER NOT NULL DEFAULT 0,
            winner_id TEXT NOT NULL DEFAULT '',
            winning_price INTEGER NOT NULL DEFAULT 0,
            settled_at INTEGER NOT NULL DEFAULT 0,
            delivery_status TEXT NOT NULL DEFAULT 'none',
            created_at INTEGER NOT NULL DEFAULT 0,
            UNIQUE(event_id, lot_no)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS auction_house_entries (
            event_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            entry_fee INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (event_id, user_id)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS auction_house_bids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            lot_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            user_name TEXT NOT NULL DEFAULT '',
            amount INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS auction_house_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            lot_id INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL DEFAULT '',
            dedupe_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL DEFAULT '{}',
            fallback_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL DEFAULT 0,
            sent_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_ah_lots_event ON auction_house_lots(event_id, lot_no)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_ah_lots_status ON auction_house_lots(event_id, status)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_ah_bids_lot ON auction_house_bids(lot_id, id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_ah_entries_user ON auction_house_entries(event_id, user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_ah_messages_status ON auction_house_messages(status, id)")


async def _ensure_talisman_v51(conn: aiosqlite.Connection, grant_legacy: bool = True):
    """符箓熟练度与符图习得：加两列，并给老存档补授原有的非基础符图。

    基础配方（basic=true）人人可画，不需要落库；``grant_legacy`` 补的是
    v6.41 之前已经能靠改名配方画出来的 5 张，避免老玩家突然失去绘制能力。
    只在版本迁移时补授——兜底路径（每次启动）不能给新玩家白发符图。
    """
    await _add_column_if_missing(conn, "players", "talisman_exp", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "talisman_recipes", "TEXT NOT NULL DEFAULT '[]'")
    if not grant_legacy:
        return
    legacy_products = ["聚灵符", "避劫符", "寻宝符", "星辰符", "鸿蒙符"]
    await conn.execute(
        "UPDATE players SET talisman_recipes = ? WHERE COALESCE(talisman_recipes, '') IN ('', '[]')",
        (json.dumps(legacy_products, ensure_ascii=False),),
    )


async def _ensure_latest_columns(conn: aiosqlite.Connection):
    """终极兜底：确保 NPC、宗门、贷款等最新增量列/表齐全。"""
    await _add_column_if_missing(conn, "players", "karma", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "life_skill", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "players", "shield_start_time", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "sect_debt", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "spirit_farms", "fertility", "INTEGER NOT NULL DEFAULT 100")
    await _add_column_if_missing(conn, "blessed_lands", "furniture", "TEXT NOT NULL DEFAULT '[]'")
    await _add_column_if_missing(conn, "sects", "buildings", "TEXT NOT NULL DEFAULT '{}'")
    await _add_column_if_missing(conn, "players", "virtue_points", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "learned_recipes", "TEXT NOT NULL DEFAULT '[]'")
    await _add_column_if_missing(conn, "players", "resurrection_pill_quality", "TEXT NOT NULL DEFAULT '一般'")
    await _add_column_if_missing(conn, "players", "overflow_items", "TEXT NOT NULL DEFAULT '{}'")
    await _add_column_if_missing(conn, "players", "equipment_instances", "TEXT NOT NULL DEFAULT '[]'")
    await _add_column_if_missing(conn, "players", "equipped_instances", "TEXT NOT NULL DEFAULT '{}'")
    await _add_column_if_missing(conn, "players", "luck", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "boss", "archetype", "TEXT NOT NULL DEFAULT 'balanced'")
    await _ensure_trade_v39(conn)
    await _ensure_npc_v29_tables(conn)
    await _add_column_if_missing(conn, "sects", "sect_level", "INTEGER NOT NULL DEFAULT 1")
    await _add_column_if_missing(conn, "sects", "sect_exp", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "sects", "sect_war_cd_until", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "sects", "sect_war_protect_until", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "sects", "sect_tournament_cd_until", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "bank_loans", "paid_interest", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "bank_loans", "paid_principal", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_sect_v30_tables(conn)
    await _ensure_auction_npc_v32(conn)
    await _ensure_auto_proxy_v35(conn)
    await _add_column_if_missing(conn, "auto_proxy", "config_json", "TEXT NOT NULL DEFAULT '{}'")
    await _ensure_world_boss_v38(conn)
    await _ensure_battle_events_v46(conn)
    await _ensure_auction_house_v47(conn)
    await _ensure_combat_limits_v48(conn)
    await _ensure_invest_v50(conn)
    await _ensure_talisman_v51(conn, grant_legacy=False)
    await _add_column_if_missing(conn, "player_pets", "max_hp", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "player_pets", "hp", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "player_pets", "last_regen_time", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "player_pets", "dead_until", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "player_pets", "slot", "INTEGER NOT NULL DEFAULT 1")
    await _add_column_if_missing(conn, "player_pets", "is_main", "INTEGER NOT NULL DEFAULT 1")
    await conn.commit()

class MigrationManager:
    """数据库迁移管理器"""

    def __init__(self, conn: aiosqlite.Connection, config_manager: ConfigManager):
        self.conn = conn
        self.config_manager = config_manager

    async def migrate(self):
        await self.conn.execute("PRAGMA foreign_keys = ON")
        async with self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='db_info'") as cursor:
            if await cursor.fetchone() is None:
                logger.info("未检测到数据库版本，将进行全新安装...")
                await self.conn.commit()  # 清掉可能的隐式事务，避免 BEGIN 嵌套报错
                await self.conn.execute("BEGIN")
                # 使用最新的建表函数
                await _create_all_tables_v2(self.conn)
                await self.conn.execute("INSERT INTO db_info (version) VALUES (?)", (LATEST_DB_VERSION,))
                await self.conn.commit()
                # 全新建表的 SQL 可能未包含最新增量列，统一补齐
                await _ensure_latest_columns(self.conn)
                logger.info(f"数据库已初始化到最新版本: v{LATEST_DB_VERSION}")
                return

        async with self.conn.execute("SELECT version FROM db_info") as cursor:
            row = await cursor.fetchone()
            current_version = row[0] if row else 0

        logger.info(f"当前数据库版本: v{current_version}, 最新版本: v{LATEST_DB_VERSION}")
        if current_version < LATEST_DB_VERSION:
            logger.info("检测到数据库需要升级...")
            for version in sorted(MIGRATION_TASKS.keys()):
                if current_version < version:
                    logger.info(f"正在执行数据库升级: v{current_version} -> v{version} ...")
                    await self.conn.commit()  # 部分迁移内部含 commit，先清掉隐式事务避免 BEGIN 嵌套
                    await self.conn.execute("BEGIN")
                    try:
                        await MIGRATION_TASKS[version](self.conn, self.config_manager)
                        await self.conn.execute("UPDATE db_info SET version = ?", (version,))
                        await self.conn.commit()
                        current_version = version
                        logger.info(f"数据库升级成功: v{version}")
                    except Exception as e:
                        await self.conn.rollback()
                        logger.error(f"数据库升级失败: v{version}. 错误: {str(e)}")
                        raise
            logger.info(f"数据库已升级到最新版本: v{LATEST_DB_VERSION}")
        else:
            logger.info("数据库已是最新版本，无需升级。")

        # 终极兜底：无论升级路径如何，确保最新增量列齐全（修复历史坏库）
        await _ensure_latest_columns(self.conn)

async def _create_all_tables_v1(conn: aiosqlite.Connection):
    """创建所有表 - v1，只保留玩家基础信息"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS db_info (
            version INTEGER NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id TEXT PRIMARY KEY,
            level_index INTEGER NOT NULL DEFAULT 0,
            spiritual_root TEXT NOT NULL DEFAULT '未知',
            experience INTEGER NOT NULL DEFAULT 0,
            gold INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT '空闲',
            hp INTEGER NOT NULL DEFAULT 100,
            max_hp INTEGER NOT NULL DEFAULT 100,
            attack INTEGER NOT NULL DEFAULT 10,
            defense INTEGER NOT NULL DEFAULT 5,
            spiritual_power INTEGER NOT NULL DEFAULT 50,
            mental_power INTEGER NOT NULL DEFAULT 50
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_player_level ON players(level_index)")
    logger.info("数据库表已创建完成（v1）")

@migration(2)
async def _migrate_to_v2(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v2 - 新属性系统（灵修/体修）"""
    await conn.execute("DROP TABLE IF EXISTS players")
    await _create_all_tables_v2(conn)

@migration(3)
async def _migrate_to_v3(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v3 - 添加闭关系统"""
    await _add_column_if_missing(conn, "players", "cultivation_start_time", "INTEGER NOT NULL DEFAULT 0")

@migration(4)
async def _migrate_to_v4(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v4 - 添加签到系统"""
    await _add_column_if_missing(conn, "players", "last_check_in_date", "TEXT NOT NULL DEFAULT ''")

@migration(5)
async def _migrate_to_v5(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v5 - 添加装备系统"""
    await _add_column_if_missing(conn, "players", "weapon", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "players", "armor", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "players", "main_technique", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "players", "techniques", "TEXT NOT NULL DEFAULT '[]'")
    await _add_column_if_missing(conn, "players", "max_spiritual_qi", "INTEGER NOT NULL DEFAULT 1000")

@migration(6)
async def _migrate_to_v6(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v6 - 添加丹药系统"""
    await _add_column_if_missing(conn, "players", "active_pill_effects", "TEXT NOT NULL DEFAULT '[]'")
    await _add_column_if_missing(conn, "players", "permanent_pill_gains", "TEXT NOT NULL DEFAULT '{}'")
    await _add_column_if_missing(conn, "players", "has_resurrection_pill", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "pills_inventory", "TEXT NOT NULL DEFAULT '{}'")

@migration(7)
async def _migrate_to_v7(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v7 - 丹药系统扩展字段"""
    await _add_column_if_missing(conn, "players", "has_debuff_shield", "INTEGER NOT NULL DEFAULT 0")

@migration(8)
async def _migrate_to_v8(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v8 - 添加商店系统"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS shop (
            shop_id TEXT PRIMARY KEY,
            last_refresh_time INTEGER NOT NULL DEFAULT 0,
            current_items TEXT NOT NULL DEFAULT '[]'
        )
    """)
    await conn.execute("""
        INSERT OR IGNORE INTO shop (shop_id, last_refresh_time, current_items)
        VALUES ('global', 0, '[]')
    """)

@migration(9)
async def _migrate_to_v9(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v9 - 添加体修气血系统"""
    await _add_column_if_missing(conn, "players", "blood_qi", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "max_blood_qi", "INTEGER NOT NULL DEFAULT 0")

@migration(10)
async def _migrate_to_v10(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v10 - 清理废弃字段（equipment_inventory等）"""
    async with conn.execute("PRAGMA table_info(players)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}

    valid_columns = {
        'user_id', 'level_index', 'spiritual_root', 'cultivation_type', 'lifespan',
        'experience', 'gold', 'state', 'cultivation_start_time', 'last_check_in_date',
        'spiritual_qi', 'max_spiritual_qi', 'blood_qi', 'max_blood_qi',
        'magic_damage', 'physical_damage', 'magic_defense', 'physical_defense', 'mental_power',
        'weapon', 'armor', 'main_technique', 'techniques',
        'active_pill_effects', 'permanent_pill_gains', 'has_resurrection_pill', 'has_debuff_shield', 'pills_inventory'
    }

    deprecated_columns = columns - valid_columns
    if deprecated_columns:
        columns_to_keep = columns & valid_columns
        columns_str = ', '.join(columns_to_keep)

        await conn.execute("""
            CREATE TABLE players_new (
                user_id TEXT PRIMARY KEY,
                level_index INTEGER NOT NULL DEFAULT 0,
                spiritual_root TEXT NOT NULL DEFAULT '未知',
                cultivation_type TEXT NOT NULL DEFAULT '灵修',
                lifespan INTEGER NOT NULL DEFAULT 100,
                experience INTEGER NOT NULL DEFAULT 0,
                gold INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT '空闲',
                cultivation_start_time INTEGER NOT NULL DEFAULT 0,
                last_check_in_date TEXT NOT NULL DEFAULT '',
                spiritual_qi INTEGER NOT NULL DEFAULT 100,
                max_spiritual_qi INTEGER NOT NULL DEFAULT 1000,
                blood_qi INTEGER NOT NULL DEFAULT 0,
                max_blood_qi INTEGER NOT NULL DEFAULT 0,
                magic_damage INTEGER NOT NULL DEFAULT 10,
                physical_damage INTEGER NOT NULL DEFAULT 10,
                magic_defense INTEGER NOT NULL DEFAULT 5,
                physical_defense INTEGER NOT NULL DEFAULT 5,
                mental_power INTEGER NOT NULL DEFAULT 100,
                weapon TEXT NOT NULL DEFAULT '',
                armor TEXT NOT NULL DEFAULT '',
                main_technique TEXT NOT NULL DEFAULT '',
                techniques TEXT NOT NULL DEFAULT '[]',
                active_pill_effects TEXT NOT NULL DEFAULT '[]',
                permanent_pill_gains TEXT NOT NULL DEFAULT '{}',
                has_resurrection_pill INTEGER NOT NULL DEFAULT 0,
                has_debuff_shield INTEGER NOT NULL DEFAULT 0,
                pills_inventory TEXT NOT NULL DEFAULT '{}',
                storage_ring TEXT NOT NULL DEFAULT '基础储物戒',
                storage_ring_items TEXT NOT NULL DEFAULT '{}'
            )
        """)

        await conn.execute(f"""
            INSERT INTO players_new ({columns_str})
            SELECT {columns_str} FROM players
        """)

        await conn.execute("DROP TABLE players")
        await conn.execute("ALTER TABLE players_new RENAME TO players")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_player_level ON players(level_index)")

@migration(11)
async def _migrate_to_v11(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v11 - 添加储物戒系统"""
    await _add_column_if_missing(conn, "players", "storage_ring", "TEXT NOT NULL DEFAULT '基础储物戒'")
    await _add_column_if_missing(conn, "players", "storage_ring_items", "TEXT NOT NULL DEFAULT '{}'")

async def _create_all_tables_v2(conn: aiosqlite.Connection):
    """创建所有表 - v2版本，完整修仙系统"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS db_info (
            version INTEGER NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id TEXT PRIMARY KEY,
            user_name TEXT NOT NULL DEFAULT '',
            level_index INTEGER NOT NULL DEFAULT 0,
            cultivation_type TEXT NOT NULL DEFAULT '灵修',
            experience INTEGER NOT NULL DEFAULT 0,
            gold INTEGER NOT NULL DEFAULT 0,
            hp INTEGER NOT NULL DEFAULT 0,
            mp INTEGER NOT NULL DEFAULT 0,
            atk INTEGER NOT NULL DEFAULT 0,
            atkpractice INTEGER NOT NULL DEFAULT 0,
            sect_id INTEGER NOT NULL DEFAULT 0,
            sect_position INTEGER NOT NULL DEFAULT 4,
            sect_contribution INTEGER NOT NULL DEFAULT 0,
            sect_task INTEGER NOT NULL DEFAULT 0,
            sect_elixir_get INTEGER NOT NULL DEFAULT 0,
            blessed_spot_flag INTEGER NOT NULL DEFAULT 0,
            blessed_spot_name TEXT NOT NULL DEFAULT '',
            level_up_rate INTEGER NOT NULL DEFAULT 0,
            
            spiritual_root TEXT NOT NULL DEFAULT '未知',
            lifespan INTEGER NOT NULL DEFAULT 100,
            state TEXT NOT NULL DEFAULT '空闲',
            cultivation_start_time INTEGER NOT NULL DEFAULT 0,
            last_check_in_date TEXT NOT NULL DEFAULT '',
            spiritual_qi INTEGER NOT NULL DEFAULT 100,
            max_spiritual_qi INTEGER NOT NULL DEFAULT 1000,
            blood_qi INTEGER NOT NULL DEFAULT 0,
            max_blood_qi INTEGER NOT NULL DEFAULT 0,
            magic_damage INTEGER NOT NULL DEFAULT 10,
            physical_damage INTEGER NOT NULL DEFAULT 10,
            magic_defense INTEGER NOT NULL DEFAULT 5,
            physical_defense INTEGER NOT NULL DEFAULT 5,
            mental_power INTEGER NOT NULL DEFAULT 100,
            
            weapon TEXT NOT NULL DEFAULT '',
            armor TEXT NOT NULL DEFAULT '',
            main_technique TEXT NOT NULL DEFAULT '',
            techniques TEXT NOT NULL DEFAULT '[]',
            
            active_pill_effects TEXT NOT NULL DEFAULT '[]',
            permanent_pill_gains TEXT NOT NULL DEFAULT '{}',
            has_resurrection_pill INTEGER NOT NULL DEFAULT 0,
            has_debuff_shield INTEGER NOT NULL DEFAULT 0,
            pills_inventory TEXT NOT NULL DEFAULT '{}',
            storage_ring TEXT NOT NULL DEFAULT '基础储物戒',
            storage_ring_items TEXT NOT NULL DEFAULT '{}',
            
            daily_pill_usage TEXT NOT NULL DEFAULT '{}',
            last_daily_reset TEXT NOT NULL DEFAULT '',
            shield_start_time INTEGER NOT NULL DEFAULT 0,
            shield_expire_time INTEGER NOT NULL DEFAULT 0,
            tutorial_step INTEGER NOT NULL DEFAULT 0,
            reincarnation_count INTEGER NOT NULL DEFAULT 0,
            fate_points INTEGER NOT NULL DEFAULT 0,
            permanent_bonus TEXT NOT NULL DEFAULT '{}',
            title TEXT NOT NULL DEFAULT '',
            karma INTEGER NOT NULL DEFAULT 0,
            luck INTEGER NOT NULL DEFAULT 0,
            life_skill TEXT NOT NULL DEFAULT ''
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_player_level ON players(level_index)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS shop (
            shop_id TEXT PRIMARY KEY,
            last_refresh_time INTEGER NOT NULL DEFAULT 0,
            current_items TEXT NOT NULL DEFAULT '[]'
        )
    """)
    await conn.execute("""
        INSERT OR IGNORE INTO shop (shop_id, last_refresh_time, current_items) VALUES ('global', 0, '[]')
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sects (
            sect_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sect_name TEXT NOT NULL UNIQUE,
            sect_owner TEXT NOT NULL,
            sect_scale INTEGER NOT NULL DEFAULT 0,
            sect_used_stone INTEGER NOT NULL DEFAULT 0,
            sect_fairyland INTEGER NOT NULL DEFAULT 0,
            sect_materials INTEGER NOT NULL DEFAULT 0,
            mainbuff TEXT NOT NULL DEFAULT '0',
            secbuff TEXT NOT NULL DEFAULT '0',
            elixir_room_level INTEGER NOT NULL DEFAULT 0,
            buildings TEXT NOT NULL DEFAULT '{}'
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sect_owner ON sects(sect_owner)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sect_scale ON sects(sect_scale DESC)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS buff_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL UNIQUE,
            main_buff INTEGER NOT NULL DEFAULT 0,
            sec_buff INTEGER NOT NULL DEFAULT 0,
            faqi_buff INTEGER NOT NULL DEFAULT 0,
            fabao_weapon INTEGER NOT NULL DEFAULT 0,
            armor_buff INTEGER NOT NULL DEFAULT 0,
            atk_buff INTEGER NOT NULL DEFAULT 0,
            blessed_spot INTEGER NOT NULL DEFAULT 0,
            sub_buff INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_buff_user ON buff_info(user_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS boss (
            boss_id INTEGER PRIMARY KEY AUTOINCREMENT,
            boss_name TEXT NOT NULL,
            boss_level TEXT NOT NULL,
            hp INTEGER NOT NULL,
            max_hp INTEGER NOT NULL,
            atk INTEGER NOT NULL,
            defense INTEGER NOT NULL DEFAULT 0,
            stone_reward INTEGER NOT NULL DEFAULT 0,
            create_time INTEGER NOT NULL DEFAULT 0,
            status INTEGER NOT NULL DEFAULT 1,
            archetype TEXT NOT NULL DEFAULT 'balanced'
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_boss_status ON boss(status, create_time DESC)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS rifts (
            rift_id INTEGER PRIMARY KEY AUTOINCREMENT,
            rift_name TEXT NOT NULL,
            rift_level INTEGER NOT NULL,
            required_level INTEGER NOT NULL,
            rewards TEXT NOT NULL DEFAULT '{}'
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS impart_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL UNIQUE,
            impart_hp_per REAL NOT NULL DEFAULT 0.0,
            impart_mp_per REAL NOT NULL DEFAULT 0.0,
            impart_atk_per REAL NOT NULL DEFAULT 0.0,
            impart_know_per REAL NOT NULL DEFAULT 0.0,
            impart_burst_per REAL NOT NULL DEFAULT 0.0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_impart_user ON impart_info(user_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_cd (
            user_id TEXT PRIMARY KEY,
            type INTEGER NOT NULL DEFAULT 0,
            create_time INTEGER NOT NULL DEFAULT 0,
            scheduled_time INTEGER NOT NULL DEFAULT 0,
            extra_data TEXT NOT NULL DEFAULT '{}'
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_gifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receiver_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            sender_name TEXT NOT NULL DEFAULT '',
            item_name TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_gifts_receiver ON pending_gifts(receiver_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_gifts_expires ON pending_gifts(expires_at)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank_accounts (
            user_id TEXT PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            last_interest_time INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank_loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            principal INTEGER NOT NULL DEFAULT 0,
            interest_rate REAL NOT NULL DEFAULT 0.005,
            borrowed_at INTEGER NOT NULL,
            due_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            loan_type TEXT NOT NULL DEFAULT 'normal'
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_loans_user ON bank_loans(user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_loans_status ON bank_loans(status)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            trans_type TEXT NOT NULL,
            amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL DEFAULT 0,
            description TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_trans_user ON bank_transactions(user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_trans_time ON bank_transactions(created_at)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS blessed_lands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL UNIQUE,
            land_type INTEGER NOT NULL DEFAULT 1,
            land_name TEXT NOT NULL DEFAULT '小洞天',
            level INTEGER NOT NULL DEFAULT 1,
            exp_bonus REAL NOT NULL DEFAULT 0.05,
            gold_per_hour INTEGER NOT NULL DEFAULT 100,
            last_collect_time INTEGER NOT NULL DEFAULT 0,
            furniture TEXT NOT NULL DEFAULT '[]'
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_blessed_lands_user ON blessed_lands(user_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS spirit_farms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL UNIQUE,
            level INTEGER NOT NULL DEFAULT 1,
            crops TEXT NOT NULL DEFAULT '[]',
            fertility INTEGER NOT NULL DEFAULT 100
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_spirit_farms_user ON spirit_farms(user_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS dual_cultivation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL UNIQUE,
            last_dual_time INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_dual_user ON dual_cultivation(user_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS spirit_eyes (
            eye_id INTEGER PRIMARY KEY AUTOINCREMENT,
            eye_type INTEGER NOT NULL DEFAULT 1,
            eye_name TEXT NOT NULL DEFAULT '下品灵眼',
            exp_per_hour INTEGER NOT NULL DEFAULT 500,
            spawn_time INTEGER NOT NULL,
            owner_id TEXT,
            owner_name TEXT,
            claim_time INTEGER,
            last_collect_time INTEGER
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_spirit_eyes_owner ON spirit_eyes(owner_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS dual_cultivation_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id TEXT NOT NULL,
            from_name TEXT NOT NULL,
            target_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_dual_req_target ON dual_cultivation_requests(target_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_dual_req_expires ON dual_cultivation_requests(expires_at)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS combat_cooldowns (
            user_id TEXT PRIMARY KEY,
            last_duel_time INTEGER NOT NULL DEFAULT 0,
            last_spar_time INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bounty_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            bounty_id INTEGER NOT NULL,
            bounty_name TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_count INTEGER NOT NULL,
            current_progress INTEGER NOT NULL DEFAULT 0,
            rewards TEXT NOT NULL DEFAULT '{}',
            start_time INTEGER NOT NULL,
            expire_time INTEGER NOT NULL,
            status INTEGER NOT NULL DEFAULT 1
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bounty_user ON bounty_tasks(user_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            task_level TEXT NOT NULL,
            title TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_count INTEGER NOT NULL,
            current_progress INTEGER NOT NULL DEFAULT 0,
            rewards_json TEXT NOT NULL DEFAULT '{}',
            status INTEGER NOT NULL DEFAULT 1,
            expire_time INTEGER NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_user ON daily_tasks(user_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS world_boss (
            boss_id INTEGER PRIMARY KEY AUTOINCREMENT,
            boss_name TEXT NOT NULL,
            boss_level INTEGER NOT NULL,
            max_hp INTEGER NOT NULL,
            current_hp INTEGER NOT NULL,
            atk INTEGER NOT NULL,
            defense INTEGER NOT NULL,
            spawn_time INTEGER NOT NULL,
            expire_time INTEGER NOT NULL,
            status INTEGER NOT NULL DEFAULT 1
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS world_boss_damage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boss_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            total_damage INTEGER NOT NULL DEFAULT 0,
            attack_count INTEGER NOT NULL DEFAULT 0,
            last_attack_time INTEGER NOT NULL,
            UNIQUE(boss_id, user_id)
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_boss_damage ON world_boss_damage(boss_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS player_pets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            pet_name TEXT NOT NULL,
            pet_type TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 1,
            exp INTEGER NOT NULL DEFAULT 0,
            max_hp INTEGER NOT NULL DEFAULT 0,
            hp INTEGER NOT NULL DEFAULT 0,
            last_regen_time INTEGER NOT NULL DEFAULT 0,
            dead_until INTEGER NOT NULL DEFAULT 0,
            slot INTEGER NOT NULL DEFAULT 1,
            is_main INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id, slot)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS auction_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id TEXT NOT NULL,
            item_name TEXT NOT NULL,
            count INTEGER NOT NULL,
            price INTEGER NOT NULL,
            create_time INTEGER NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_auction_seller ON auction_items(seller_id)")

    import json
    default_rifts = [
        (1, "青云秘境", 1, 0, json.dumps({"exp": [500, 1500], "gold": [200, 800]})),
        (2, "落日峡谷", 2, 3, json.dumps({"exp": [1500, 4000], "gold": [500, 2000]})),
        (3, "万妖洞", 3, 6, json.dumps({"exp": [3000, 8000], "gold": [1000, 5000]})),
        (4, "玄冰地宫", 4, 10, json.dumps({"exp": [5000, 15000], "gold": [2000, 10000]})),
        (5, "上古遗迹", 5, 15, json.dumps({"exp": [10000, 30000], "gold": [5000, 20000]})),
    ]
    for rift in default_rifts:
        await conn.execute(
            "INSERT OR IGNORE INTO rifts (rift_id, rift_name, rift_level, required_level, rewards) VALUES (?, ?, ?, ?, ?)",
            rift
        )

@migration(12)
async def _migrate_to_v12(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v12 - 添加完整修仙系统（宗门、Boss、秘境、战斗系统等）"""
    await _add_column_if_missing(conn, "players", "user_name", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "players", "level_up_rate", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "hp", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "mp", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "atk", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "atkpractice", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "sect_id", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "sect_position", "INTEGER NOT NULL DEFAULT 4")
    await _add_column_if_missing(conn, "players", "sect_contribution", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "sect_task", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "sect_elixir_get", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "blessed_spot_flag", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "blessed_spot_name", "TEXT NOT NULL DEFAULT ''")
    
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sects (
            sect_id INTEGER PRIMARY KEY AUTOINCREMENT, sect_name TEXT NOT NULL UNIQUE, sect_owner TEXT NOT NULL,
            sect_scale INTEGER NOT NULL DEFAULT 0, sect_used_stone INTEGER NOT NULL DEFAULT 0, sect_fairyland INTEGER NOT NULL DEFAULT 0,
            sect_materials INTEGER NOT NULL DEFAULT 0, mainbuff TEXT NOT NULL DEFAULT '0', secbuff TEXT NOT NULL DEFAULT '0',
            elixir_room_level INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sect_owner ON sects(sect_owner)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sect_scale ON sects(sect_scale DESC)")
    
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS buff_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL UNIQUE,
            main_buff INTEGER NOT NULL DEFAULT 0, sec_buff INTEGER NOT NULL DEFAULT 0, faqi_buff INTEGER NOT NULL DEFAULT 0,
            fabao_weapon INTEGER NOT NULL DEFAULT 0, armor_buff INTEGER NOT NULL DEFAULT 0, atk_buff INTEGER NOT NULL DEFAULT 0,
            blessed_spot INTEGER NOT NULL DEFAULT 0, sub_buff INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_buff_user ON buff_info(user_id)")
    
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS boss (
            boss_id INTEGER PRIMARY KEY AUTOINCREMENT, boss_name TEXT NOT NULL, boss_level TEXT NOT NULL,
            hp INTEGER NOT NULL, max_hp INTEGER NOT NULL, atk INTEGER NOT NULL, defense INTEGER NOT NULL DEFAULT 0,
            stone_reward INTEGER NOT NULL DEFAULT 0, create_time INTEGER NOT NULL DEFAULT 0, status INTEGER NOT NULL DEFAULT 1,
            archetype TEXT NOT NULL DEFAULT 'balanced'
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_boss_status ON boss(status, create_time DESC)")
    
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS rifts (
            rift_id INTEGER PRIMARY KEY AUTOINCREMENT, rift_name TEXT NOT NULL, rift_level INTEGER NOT NULL,
            required_level INTEGER NOT NULL, rewards TEXT NOT NULL DEFAULT '{}'
        )
    """)
    
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS impart_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL UNIQUE,
            impart_hp_per REAL NOT NULL DEFAULT 0.0, impart_mp_per REAL NOT NULL DEFAULT 0.0, impart_atk_per REAL NOT NULL DEFAULT 0.0,
            impart_know_per REAL NOT NULL DEFAULT 0.0, impart_burst_per REAL NOT NULL DEFAULT 0.0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_impart_user ON impart_info(user_id)")
    
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_cd (
            user_id TEXT PRIMARY KEY, type INTEGER NOT NULL DEFAULT 0,
            create_time INTEGER NOT NULL DEFAULT 0, scheduled_time INTEGER NOT NULL DEFAULT 0
        )
    """)

@migration(13)
async def _migrate_to_v13(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v13 - Phase 1: 道号系统、每日限制、物品绑定"""
    try: await conn.execute("ALTER TABLE players ADD COLUMN daily_pill_usage TEXT NOT NULL DEFAULT '{}'")
    except: pass
    try: await conn.execute("ALTER TABLE players ADD COLUMN last_daily_reset TEXT NOT NULL DEFAULT ''")
    except: pass

@migration(14)
async def _migrate_to_v14(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v14 - Phase 2: 灵石银行、悬赏令系统"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank_accounts (
            user_id TEXT PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, last_interest_time INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bounty_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, bounty_id INTEGER NOT NULL, bounty_name TEXT NOT NULL,
            target_type TEXT NOT NULL, target_count INTEGER NOT NULL, current_progress INTEGER NOT NULL DEFAULT 0,
            rewards TEXT NOT NULL DEFAULT '{}', start_time INTEGER NOT NULL, expire_time INTEGER NOT NULL, status INTEGER NOT NULL DEFAULT 1
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bounty_user ON bounty_tasks(user_id)")

@migration(15)
async def _migrate_to_v15(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v15 - 添加默认秘境数据"""
    import json
    default_rifts = [
        (1, "青云秘境", 1, 0, json.dumps({"exp": [500, 1500], "gold": [200, 800]})),
        (2, "落日峡谷", 2, 3, json.dumps({"exp": [1500, 4000], "gold": [500, 2000]})),
        (3, "万妖洞", 3, 6, json.dumps({"exp": [3000, 8000], "gold": [1000, 5000]})),
        (4, "玄冰地宫", 4, 10, json.dumps({"exp": [5000, 15000], "gold": [2000, 10000]})),
        (5, "上古遗迹", 5, 15, json.dumps({"exp": [10000, 30000], "gold": [5000, 20000]})),
    ]
    for rift in default_rifts:
        try: await conn.execute("INSERT OR IGNORE INTO rifts (rift_id, rift_name, rift_level, required_level, rewards) VALUES (?, ?, ?, ?, ?)", rift)
        except: pass

@migration(16)
async def _migrate_to_v16(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v16 - Phase 4 扩展功能"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS blessed_lands (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL UNIQUE, land_type INTEGER NOT NULL DEFAULT 1,
            land_name TEXT NOT NULL DEFAULT '小洞天', level INTEGER NOT NULL DEFAULT 1, exp_bonus REAL NOT NULL DEFAULT 0.05,
            gold_per_hour INTEGER NOT NULL DEFAULT 100, last_collect_time INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_blessed_lands_user ON blessed_lands(user_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS spirit_farms (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL UNIQUE, level INTEGER NOT NULL DEFAULT 1, crops TEXT NOT NULL DEFAULT '[]'
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_spirit_farms_user ON spirit_farms(user_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS dual_cultivation (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL UNIQUE, last_dual_time INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_dual_user ON dual_cultivation(user_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS spirit_eyes (
            eye_id INTEGER PRIMARY KEY AUTOINCREMENT, eye_type INTEGER NOT NULL DEFAULT 1, eye_name TEXT NOT NULL DEFAULT '下品灵眼',
            exp_per_hour INTEGER NOT NULL DEFAULT 500, spawn_time INTEGER NOT NULL, owner_id TEXT, owner_name TEXT, claim_time INTEGER
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_spirit_eyes_owner ON spirit_eyes(owner_id)")
    import time
    now = int(time.time())
    initial_eyes = [(1, "下品灵眼", 500, now), (1, "下品灵眼", 500, now), (2, "中品灵眼", 2000, now)]
    for eye in initial_eyes:
        await conn.execute("INSERT INTO spirit_eyes (eye_type, eye_name, exp_per_hour, spawn_time) VALUES (?, ?, ?, ?)", eye)

@migration(17)
async def _migrate_to_v17(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v17 - 赠予请求持久化"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_gifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, receiver_id TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT NOT NULL DEFAULT '',
            item_name TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_gifts_receiver ON pending_gifts(receiver_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_gifts_expires ON pending_gifts(expires_at)")

@migration(18)
async def _migrate_to_v18(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v18 - 银行贷款与交易流水系统"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank_accounts (
            user_id TEXT PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, last_interest_time INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank_loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, principal INTEGER NOT NULL DEFAULT 0,
            interest_rate REAL NOT NULL DEFAULT 0.005, borrowed_at INTEGER NOT NULL, due_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', loan_type TEXT NOT NULL DEFAULT 'normal'
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_loans_user ON bank_loans(user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_loans_status ON bank_loans(status)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, trans_type TEXT NOT NULL, amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL DEFAULT 0, description TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_trans_user ON bank_transactions(user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_trans_time ON bank_transactions(created_at)")

@migration(19)
async def _migrate_to_v19(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v19 - 银行系统表完整性修复"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank_accounts (
            user_id TEXT PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, last_interest_time INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank_loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, principal INTEGER NOT NULL DEFAULT 0,
            interest_rate REAL NOT NULL DEFAULT 0.005, borrowed_at INTEGER NOT NULL, due_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', loan_type TEXT NOT NULL DEFAULT 'normal'
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_loans_user ON bank_loans(user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_loans_status ON bank_loans(status)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, trans_type TEXT NOT NULL, amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL DEFAULT 0, description TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_trans_user ON bank_transactions(user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_trans_time ON bank_transactions(created_at)")

@migration(20)
async def _migrate_to_v20(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v20 - 用户CD表添加额外数据字段"""
    try: await conn.execute("ALTER TABLE user_cd ADD COLUMN extra_data TEXT NOT NULL DEFAULT '{}'")
    except Exception as e: logger.warning(f"添加extra_data字段失败（可能已存在）: {e}")
    try: await conn.execute("ALTER TABLE spirit_eyes ADD COLUMN last_collect_time INTEGER")
    except Exception as e: logger.warning(f"添加last_collect_time字段失败（可能已存在）: {e}")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS dual_cultivation_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, from_id TEXT NOT NULL, from_name TEXT NOT NULL, target_id TEXT NOT NULL,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_dual_req_target ON dual_cultivation_requests(target_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_dual_req_expires ON dual_cultivation_requests(expires_at)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS combat_cooldowns (
            user_id TEXT PRIMARY KEY, last_duel_time INTEGER NOT NULL DEFAULT 0, last_spar_time INTEGER NOT NULL DEFAULT 0
        )
    """)

@migration(21)
async def _migrate_to_v21(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v21 - v6.1 核心系统扩展（日常任务、世界Boss）"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, task_level TEXT NOT NULL, title TEXT NOT NULL,
            target_type TEXT NOT NULL, target_count INTEGER NOT NULL, current_progress INTEGER NOT NULL DEFAULT 0,
            rewards_json TEXT NOT NULL DEFAULT '{}', status INTEGER NOT NULL DEFAULT 1, expire_time INTEGER NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_user ON daily_tasks(user_id)")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS world_boss (
            boss_id INTEGER PRIMARY KEY AUTOINCREMENT, boss_name TEXT NOT NULL, boss_level INTEGER NOT NULL,
            max_hp INTEGER NOT NULL, current_hp INTEGER NOT NULL, atk INTEGER NOT NULL, defense INTEGER NOT NULL,
            spawn_time INTEGER NOT NULL, expire_time INTEGER NOT NULL, status INTEGER NOT NULL DEFAULT 1
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS world_boss_damage (
            id INTEGER PRIMARY KEY AUTOINCREMENT, boss_id INTEGER NOT NULL, user_id TEXT NOT NULL,
            total_damage INTEGER NOT NULL DEFAULT 0, attack_count INTEGER NOT NULL DEFAULT 0,
            last_attack_time INTEGER NOT NULL, UNIQUE(boss_id, user_id)
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_boss_damage ON world_boss_damage(boss_id)")

@migration(22)
async def _migrate_to_v22(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v22 - 增加玩家护府令状态"""
    try: await conn.execute("ALTER TABLE players ADD COLUMN shield_expire_time INTEGER NOT NULL DEFAULT 0")
    except Exception as e: logger.warning(f"添加 shield_expire_time 字段失败（可能已存在）: {e}")

@migration(23)
async def _migrate_to_v23(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v23 - 新手引导与飞升转生"""
    try: await conn.execute("ALTER TABLE players ADD COLUMN tutorial_step INTEGER NOT NULL DEFAULT 0")
    except Exception: pass
    try: await conn.execute("ALTER TABLE players ADD COLUMN reincarnation_count INTEGER NOT NULL DEFAULT 0")
    except Exception: pass
    try: await conn.execute("ALTER TABLE players ADD COLUMN fate_points INTEGER NOT NULL DEFAULT 0")
    except Exception: pass
    try: await conn.execute("ALTER TABLE players ADD COLUMN permanent_bonus TEXT NOT NULL DEFAULT '{}'")
    except Exception: pass

@migration(24)
async def _migrate_to_v24(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v24 - 灵兽系统"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS player_pets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, pet_name TEXT NOT NULL,
            pet_type TEXT NOT NULL, level INTEGER NOT NULL DEFAULT 1, exp INTEGER NOT NULL DEFAULT 0,
            max_hp INTEGER NOT NULL DEFAULT 0, hp INTEGER NOT NULL DEFAULT 0,
            last_regen_time INTEGER NOT NULL DEFAULT 0, dead_until INTEGER NOT NULL DEFAULT 0,
            slot INTEGER NOT NULL DEFAULT 1, is_main INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id, slot)
        )
    """)

@migration(25)
async def _migrate_to_v25(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v25 - 称号系统"""
    logger.info("开始迁移到v25：称号系统")
    try:
        await conn.execute("ALTER TABLE players ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    except Exception as e:
        logger.warning(f"添加 title 字段失败（可能已存在）: {e}")
    await conn.commit()
    logger.info("v25迁移完成：称号字段已添加")

@migration(26)
async def _migrate_to_v26(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v26 - 玩家交易坊市"""
    logger.info("开始迁移到v26：玩家交易坊市")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS auction_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id TEXT NOT NULL,
            item_name TEXT NOT NULL,
            count INTEGER NOT NULL,
            price INTEGER NOT NULL,
            create_time INTEGER NOT NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_auction_seller ON auction_items(seller_id)")
    await conn.commit()
    logger.info("v26迁移完成：交易坊市表已建立")

@migration(27)
async def _migrate_to_v27(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v27 - 灵田肥力系统"""
    logger.info("开始迁移到v27：灵田肥力系统")
    await _add_column_if_missing(conn, "spirit_farms", "fertility", "INTEGER NOT NULL DEFAULT 100")
    # 存量灵田补默认肥力
    await conn.execute("UPDATE spirit_farms SET fertility = 100 WHERE fertility IS NULL OR fertility <= 0")
    await conn.commit()
    logger.info("v27迁移完成：灵田肥力字段已添加")

@migration(28)
async def _migrate_to_v28(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v28 - 功德因果/生活职业/洞天装修/宗门建筑"""
    logger.info("开始迁移到v28：功德/生活职业/装修/宗门建筑")
    await _add_column_if_missing(conn, "players", "life_skill", "TEXT NOT NULL DEFAULT ''")
    await _add_column_if_missing(conn, "players", "karma", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "blessed_lands", "furniture", "TEXT NOT NULL DEFAULT '[]'")
    await _add_column_if_missing(conn, "sects", "buildings", "TEXT NOT NULL DEFAULT '{}'")
    await conn.commit()
    logger.info("v28迁移完成")

@migration(29)
async def _migrate_to_v29(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到v29 - NPC道侣（仙灵结缘）+ 师徒系统"""
    logger.info("开始迁移到v29：NPC道侣/师徒系统")
    await _add_column_if_missing(conn, "players", "virtue_points", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "learned_recipes", "TEXT NOT NULL DEFAULT '[]'")
    await _add_column_if_missing(conn, "players", "resurrection_pill_quality", "TEXT NOT NULL DEFAULT '一般'")
    await _ensure_npc_v29_tables(conn)
    await conn.commit()
    logger.info("v29迁移完成")

@migration(30)
async def _migrate_to_v30(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v30 - 宗门等级/命令/大比/宗门战 + 贷款部分还款"""
    logger.info("开始迁移到 v30：宗门功能拓展")
    await _add_column_if_missing(conn, "players", "sect_debt", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "sects", "sect_level", "INTEGER NOT NULL DEFAULT 1")
    await _add_column_if_missing(conn, "sects", "sect_exp", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "sects", "sect_war_cd_until", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "sects", "sect_war_protect_until", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "sects", "sect_tournament_cd_until", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "bank_loans", "paid_interest", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "bank_loans", "paid_principal", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_sect_v30_tables(conn)

    # 老宗门按建设度折算经验，避免全部掉回 1 级；已有建筑等级不回收
    await conn.execute("UPDATE sects SET sect_exp = MIN(sect_scale, 100000) WHERE sect_exp = 0")
    await conn.execute("""
        UPDATE sects SET sect_level = CASE
            WHEN sect_exp >= 100000 THEN 5
            WHEN sect_exp >= 60000 THEN 4
            WHEN sect_exp >= 30000 THEN 3
            WHEN sect_exp >= 10000 THEN 2
            ELSE 1 END
    """)
    await conn.commit()
    logger.info("v30迁移完成")

@migration(31)
async def _migrate_to_v31(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """v31 兼容：已经到 v30 的本地库补上 NPC/宗门/贷款字段。"""
    logger.info("开始迁移到 v31：兼容 NPC/宗门/贷款字段")
    await _add_column_if_missing(conn, "players", "virtue_points", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "players", "learned_recipes", "TEXT NOT NULL DEFAULT '[]'")
    await _add_column_if_missing(conn, "players", "resurrection_pill_quality", "TEXT NOT NULL DEFAULT '一般'")
    await _ensure_npc_v29_tables(conn)
    await _add_column_if_missing(conn, "players", "sect_debt", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "sects", "sect_level", "INTEGER NOT NULL DEFAULT 1")
    await _add_column_if_missing(conn, "sects", "sect_exp", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "sects", "sect_war_cd_until", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "sects", "sect_war_protect_until", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "sects", "sect_tournament_cd_until", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "bank_loans", "paid_interest", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "bank_loans", "paid_principal", "INTEGER NOT NULL DEFAULT 0")
    await _ensure_sect_v30_tables(conn)
    await conn.commit()
    logger.info("v31迁移完成")

@migration(32)
async def _migrate_to_v32(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v32 - 坊市 NPC 随机问价机制。"""
    logger.info("开始迁移到 v32：坊市 NPC 问价")
    await _ensure_auction_npc_v32(conn)
    await conn.commit()
    logger.info("v32迁移完成：坊市 NPC 问价表已建立")

@migration(33)
async def _migrate_to_v33(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v33 - 灵兽独立精力、脱战恢复与死亡复活。"""
    logger.info("开始迁移到 v33：灵兽独立精力/复活机制")
    await _add_column_if_missing(conn, "player_pets", "max_hp", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "player_pets", "hp", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "player_pets", "last_regen_time", "INTEGER NOT NULL DEFAULT 0")
    await _add_column_if_missing(conn, "player_pets", "dead_until", "INTEGER NOT NULL DEFAULT 0")
    await conn.commit()
    logger.info("v33迁移完成：灵兽精力字段已补齐")

@migration(34)
async def _migrate_to_v34(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v34 - 多灵兽槽位、主战标记与自定义名称。"""
    logger.info("开始迁移到 v34：多灵兽槽位")
    async with conn.execute("PRAGMA table_info(player_pets)") as cursor:
        old_cols = {row[1] for row in await cursor.fetchall()}
    if not old_cols:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS player_pets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, pet_name TEXT NOT NULL,
                pet_type TEXT NOT NULL, level INTEGER NOT NULL DEFAULT 1, exp INTEGER NOT NULL DEFAULT 0,
                max_hp INTEGER NOT NULL DEFAULT 0, hp INTEGER NOT NULL DEFAULT 0,
                last_regen_time INTEGER NOT NULL DEFAULT 0, dead_until INTEGER NOT NULL DEFAULT 0,
                slot INTEGER NOT NULL DEFAULT 1, is_main INTEGER NOT NULL DEFAULT 1,
                UNIQUE(user_id, slot)
            )
        """)
        await conn.commit()
        return

    slot_expr = "slot" if "slot" in old_cols else "1"
    main_expr = "is_main" if "is_main" in old_cols else "1"
    await conn.execute("ALTER TABLE player_pets RENAME TO player_pets_v33")
    await conn.execute("""
        CREATE TABLE player_pets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, pet_name TEXT NOT NULL,
            pet_type TEXT NOT NULL, level INTEGER NOT NULL DEFAULT 1, exp INTEGER NOT NULL DEFAULT 0,
            max_hp INTEGER NOT NULL DEFAULT 0, hp INTEGER NOT NULL DEFAULT 0,
            last_regen_time INTEGER NOT NULL DEFAULT 0, dead_until INTEGER NOT NULL DEFAULT 0,
            slot INTEGER NOT NULL DEFAULT 1, is_main INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id, slot)
        )
    """)
    await conn.execute(f"""
        INSERT INTO player_pets
        (id, user_id, pet_name, pet_type, level, exp, max_hp, hp, last_regen_time, dead_until, slot, is_main)
        SELECT id, user_id, pet_name, pet_type, level, exp, max_hp, hp, last_regen_time, dead_until, {slot_expr}, {main_expr}
        FROM player_pets_v33
    """)
    await conn.execute("DROP TABLE player_pets_v33")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_player_pets_user ON player_pets(user_id)")
    await conn.commit()
    logger.info("v34迁移完成：多灵兽槽位已建立")

@migration(35)
async def _migrate_to_v35(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v35 - 自动代理状态表。"""
    logger.info("开始迁移到 v35：自动代理")
    await _ensure_auto_proxy_v35(conn)
    await conn.commit()
    logger.info("v35迁移完成：自动代理表已建立")

@migration(36)
async def _migrate_to_v36(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v36 - 护府大阵生效起始时间。"""
    logger.info("开始迁移到 v36：护府大阵起止时间")
    await _add_column_if_missing(conn, "players", "shield_start_time", "INTEGER NOT NULL DEFAULT 0")
    await conn.execute("""
        UPDATE players
        SET shield_start_time = shield_expire_time - CASE
            WHEN life_skill = '阵法师' THEN 129600
            ELSE 86400
        END
        WHERE shield_expire_time > 0 AND shield_start_time = 0
    """)
    await conn.commit()
    logger.info("v36迁移完成：护府大阵起止时间已补齐")


async def _ensure_pill_v37(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """v37：丹药品质背包 key、丹方学习、回生丹品质，并清理历史丹药卡单。"""
    await _add_column_if_missing(conn, "players", "learned_recipes", "TEXT NOT NULL DEFAULT '[]'")
    await _add_column_if_missing(conn, "players", "resurrection_pill_quality", "TEXT NOT NULL DEFAULT '一般'")

    default_recipes = []
    pill_names = set()
    try:
        recipes = getattr(config_manager, "pill_recipes_data", {}) or {}
        default_recipes = [str(rid) for rid in sorted(recipes)[:3]]
        pill_names = set(config_manager.get_all_pill_names())
    except Exception:
        default_recipes = []
        pill_names = set()

    def parse_json(raw, fallback):
        try:
            value = json.loads(raw) if raw else fallback
        except Exception:
            return fallback
        return value if isinstance(value, type(fallback)) else fallback

    async with conn.execute(
        "SELECT user_id, pills_inventory, learned_recipes, storage_ring_items FROM players"
    ) as cursor:
        rows = await cursor.fetchall()

    state = {}
    for row in rows:
        user_id = row[0]
        inventory = parse_json(row[1], {})
        learned = parse_json(row[2], [])
        storage = parse_json(row[3], {})
        migrated = {}
        for key, count in inventory.items():
            clean_key = str(key)
            base_name = clean_key.split("|", 1)[0]
            quality = clean_key.rsplit("|", 1)[1] if "|" in clean_key else "一般"
            if quality not in ("一般", "良好", "极佳", "大师"):
                quality = "一般"
            new_key = f"{base_name}|{quality}"
            migrated[new_key] = int(count or 0) + migrated.get(new_key, 0)

        if isinstance(storage, dict):
            for item_name, count in list(storage.items()):
                base_name = str(item_name).split("|", 1)[0]
                if base_name in pill_names:
                    key = f"{base_name}|一般"
                    migrated[key] = int(count or 0) + migrated.get(key, 0)
                    del storage[item_name]

        if not learned and default_recipes:
            learned = default_recipes
        state[user_id] = [migrated, learned, storage]

    # 历史坊市丹药：退回卖家丹药背包，删除卡单
    try:
        async with conn.execute("SELECT id, seller_id, item_name, count FROM auction_items") as cursor:
            auction_rows = await cursor.fetchall()
        for row in auction_rows:
            auction_id, seller_id, item_name, count = row[0], row[1], row[2], row[3]
            base_name = str(item_name).split("|", 1)[0]
            if base_name not in pill_names:
                continue
            if seller_id in state:
                key = f"{base_name}|一般"
                state[seller_id][0][key] = int(count or 0) + state[seller_id][0].get(key, 0)
            await conn.execute("DELETE FROM auction_npc_offers WHERE auction_id = ?", (auction_id,))
            await conn.execute("DELETE FROM auction_items WHERE id = ?", (auction_id,))
    except Exception:
        pass

    # 历史赠予丹药：退回发送者丹药背包并取消赠予
    try:
        async with conn.execute("SELECT id, sender_id, item_name, count FROM pending_gifts") as cursor:
            gift_rows = await cursor.fetchall()
        for row in gift_rows:
            gift_id, sender_id, item_name, count = row[0], row[1], row[2], row[3]
            base_name = str(item_name).split("|", 1)[0]
            if base_name not in pill_names:
                continue
            if sender_id in state:
                key = f"{base_name}|一般"
                state[sender_id][0][key] = int(count or 0) + state[sender_id][0].get(key, 0)
            await conn.execute("DELETE FROM pending_gifts WHERE id = ?", (gift_id,))
    except Exception:
        pass

    for user_id, (inventory, learned, storage) in state.items():
        await conn.execute(
            "UPDATE players SET pills_inventory = ?, learned_recipes = ?, storage_ring_items = ? WHERE user_id = ?",
            (
                json.dumps(inventory, ensure_ascii=False),
                json.dumps(learned, ensure_ascii=False),
                json.dumps(storage, ensure_ascii=False),
                user_id,
            ),
        )


@migration(37)
async def _migrate_to_v37(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v37 - 丹药品质、丹方学习与回生丹品质。"""
    logger.info("开始迁移到 v37：丹药品质与丹方")
    await _ensure_pill_v37(conn, config_manager)
    logger.info("v37迁移完成：丹药品质 key、默认丹方与回生丹品质已建立")


@migration(38)
async def _migrate_to_v38(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v38 - 世界Boss种类、防重复结算与团本系统。"""
    logger.info("开始迁移到 v38：世界Boss种类与团本")
    await _ensure_world_boss_v38(conn)
    await conn.execute("UPDATE world_boss SET settled = 1 WHERE status = 2")
    await conn.commit()
    logger.info("v38迁移完成：世界Boss种类字段与团本表已建立")


@migration(39)
async def _migrate_to_v39(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v39 - 丹药进入坊市与赠予交易。"""
    logger.info("开始迁移到 v39：丹药交易字段")
    await _ensure_trade_v39(conn)
    logger.info("v39迁移完成：坊市/赠予已支持丹药品质")


@migration(40)
async def _migrate_to_v40(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v40 - 溢出仓库与装备实例化。"""
    logger.info("开始迁移到 v40：溢出仓库与装备实例")
    await _add_column_if_missing(conn, "players", "overflow_items", "TEXT NOT NULL DEFAULT '{}'")
    await _add_column_if_missing(conn, "players", "equipment_instances", "TEXT NOT NULL DEFAULT '[]'")
    await _add_column_if_missing(conn, "players", "equipped_instances", "TEXT NOT NULL DEFAULT '{}'")

    from ..core.item_rules import ItemRules
    gear_types = {"weapon", "armor", "head", "body", "boots", "gloves", "ring", "amulet", "accessory"}
    items_data = getattr(config_manager, "items_data", {}) or {}
    weapons_data = getattr(config_manager, "weapons_data", {}) or {}

    def item_type(name: str) -> str:
        cfg = items_data.get(name) or weapons_data.get(name) or {}
        return ItemRules.normalize_equipment_type(cfg)

    async with conn.execute(
        "SELECT user_id, storage_ring_items, equipment_instances, equipped_instances, weapon, armor, permanent_bonus FROM players"
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        user_id = row[0]
        try:
            storage = json.loads(row[1]) if row[1] else {}
        except Exception:
            storage = {}
        try:
            instances = json.loads(row[2]) if row[2] else []
        except Exception:
            instances = []
        try:
            equipped = json.loads(row[3]) if row[3] else {}
        except Exception:
            equipped = {}
        if not isinstance(storage, dict):
            storage = {}
        if not isinstance(instances, list):
            instances = []
        if not isinstance(equipped, dict):
            equipped = {}

        next_id = 1 + max([int(x.get("id", 0) or 0) for x in instances if isinstance(x, dict)] or [0])

        def add_instance(name: str, location: str, slot: str = "") -> int:
            nonlocal next_id
            inst = {
                "id": next_id, "name": name, "type": item_type(name),
                "quality": "一般", "affixes": [], "location": location, "slot": slot,
            }
            instances.append(inst)
            next_id += 1
            return inst["id"]

        for name, count in list(storage.items()):
            if item_type(name) not in gear_types:
                continue
            try:
                count = int(count or 0)
            except Exception:
                count = 0
            for _ in range(max(0, count)):
                add_instance(name, "storage")
            storage.pop(name, None)

        for slot, field in (("weapon", "weapon"), ("armor", "armor")):
            name = row[4] if slot == "weapon" else row[5]
            if not name or item_type(name) not in gear_types:
                continue
            existing = equipped.get(slot)
            valid = any(isinstance(x, dict) and int(x.get("id", 0) or 0) == int(existing or 0) for x in instances)
            if not valid:
                equipped[slot] = add_instance(name, "equipped", slot)

        # 旧版全局洗练词条迁移到已装备武器/防具实例
        try:
            bonus = json.loads(row[6]) if row[6] else {}
        except Exception:
            bonus = {}
        if isinstance(bonus, dict):
            affixes = bonus.get("affixes") or []
            if affixes:
                target_id = equipped.get("weapon") or equipped.get("armor")
                for inst in instances:
                    if int(inst.get("id", 0) or 0) == int(target_id or 0):
                        inst["affixes"] = affixes
                        bonus["affixes"] = []
                        break

        await conn.execute(
            "UPDATE players SET storage_ring_items = ?, equipment_instances = ?, equipped_instances = ?, permanent_bonus = ? WHERE user_id = ?",
            (
                json.dumps(storage, ensure_ascii=False),
                json.dumps(instances, ensure_ascii=False),
                json.dumps(equipped, ensure_ascii=False),
                json.dumps(bonus, ensure_ascii=False),
                user_id,
            ),
        )
    logger.info("v40迁移完成：溢出仓库与装备实例已建立")


@migration(41)
async def _migrate_to_v41(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v41 - 气运系统。"""
    logger.info("开始迁移到 v41：气运系统")
    await _add_column_if_missing(conn, "players", "luck", "INTEGER NOT NULL DEFAULT 0")
    await conn.commit()
    logger.info("v41迁移完成：气运字段已添加")


@migration(42)
async def _migrate_to_v42(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v42 - 已到 v40/41 的库补齐装备实例与溢出字段。"""
    logger.info("开始迁移到 v42：装备实例回填")
    await _migrate_to_v40(conn, config_manager)
    logger.info("v42迁移完成：装备实例与溢出字段已确认")


async def _migrate_boss_archetype_v43(conn: aiosqlite.Connection):
    """补齐 Boss 物法流派字段（幂等）。"""
    await _add_column_if_missing(
        conn, "boss", "archetype", "TEXT NOT NULL DEFAULT 'balanced'"
    )


async def _migrate_equipment_slots_v43(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """将旧防具实例归入具体部位，保持 ID、品质、词条及所有权不变。"""
    from ..core.item_rules import ItemRules
    items_data = getattr(config_manager, "items_data", {}) or {}
    weapons_data = getattr(config_manager, "weapons_data", {}) or {}
    async with conn.execute(
        "SELECT user_id, equipment_instances, equipped_instances, armor FROM players"
    ) as cursor:
        rows = await cursor.fetchall()
    for user_id, raw_instances, raw_mapping, armor in rows:
        instances = json.loads(raw_instances or "[]")
        mapping = json.loads(raw_mapping or "{}")
        for inst in instances:
            cfg = items_data.get(inst.get("name")) or weapons_data.get(inst.get("name"))
            if cfg:
                inst["type"] = ItemRules.normalize_equipment_type(cfg)
        # armor 是历史字段，继续用于甲胄；新部位仅存在 JSON 映射中。
        old_id = mapping.get("armor")
        inst = next((x for x in instances if x.get("id") == old_id), None)
        if inst:
            slot = ItemRules.SLOT_BY_TYPE.get(inst.get("type"), "body")
            slot = "armor" if slot == "body" else slot
            if slot != "armor":
                mapping.pop("armor", None)
                armor = ""
                if mapping.get(slot):
                    inst.update(location="storage", slot="")
                else:
                    mapping[slot] = inst["id"]
                    inst.update(location="equipped", slot=slot)
        await conn.execute(
            "UPDATE players SET equipment_instances=?, equipped_instances=?, armor=? WHERE user_id=?",
            (json.dumps(instances, ensure_ascii=False), json.dumps(mapping), armor, user_id),
        )


@migration(43)
async def _migrate_to_v43(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """v43：Boss 物法流派 + 装备部位迁移。"""
    logger.info("开始迁移到 v43：Boss 流派与装备部位")
    await _migrate_boss_archetype_v43(conn)
    await _migrate_equipment_slots_v43(conn, config_manager)
    await conn.commit()
    logger.info("v43迁移完成：Boss 流派与装备部位已处理")


@migration(44)
async def _migrate_to_v44(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """v44：装备平衡版本，清理旧价货架与过期 NPC 报价。"""
    logger.info("开始迁移到 v44：装备平衡货架失效")
    await conn.execute("DELETE FROM shop")
    await conn.execute("UPDATE auction_npc_offers SET status='expired' WHERE status='pending'")
    await _migrate_boss_archetype_v43(conn)
    await _migrate_equipment_slots_v43(conn, config_manager)
    await conn.commit()
    logger.info("v44迁移完成：货架已失效，装备部位已确认")


@migration(45)
async def _migrate_to_v45(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """v45：灵气统一后同步 mp 兼容字段，并补充自动代理灵田配置字段。"""
    logger.info("开始迁移到 v45：灵气与 mp 同步")
    await conn.execute("UPDATE players SET mp = COALESCE(spiritual_qi, 0)")
    await _add_column_if_missing(conn, "auto_proxy", "config_json", "TEXT NOT NULL DEFAULT '{}'")
    await conn.commit()
    logger.info("v45迁移完成：mp 已同步为 spiritual_qi")


@migration(46)
async def _migrate_to_v46(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v46 - 江湖战斗小事件表。"""
    logger.info("开始迁移到 v46：江湖战斗小事件")
    await _ensure_battle_events_v46(conn)
    await conn.commit()
    logger.info("v46迁移完成：江湖事件表已建立")


@migration(47)
async def _migrate_to_v47(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v47 - 拍卖会事件表。"""
    logger.info("开始迁移到 v47：拍卖会事件")
    await _ensure_auction_house_v47(conn)
    await conn.commit()
    logger.info("v47迁移完成：拍卖会事件表已建立")


async def _ensure_combat_limits_v48(conn: aiosqlite.Connection):
    """v48：每日切磋/决斗次数与切磋邀请表。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS combat_daily (
            user_id TEXT NOT NULL,
            day TEXT NOT NULL,
            duel_count INTEGER NOT NULL DEFAULT 0,
            spar_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS spar_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id TEXT NOT NULL,
            from_name TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT 0,
            expires_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_spar_requests_target ON spar_requests(target_id, expires_at)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_spar_requests_from ON spar_requests(from_id, expires_at)"
    )


async def _ensure_invest_v50(conn: aiosqlite.Connection):
    """v50：仙市投资系统（基金/股票）表。

    价格只在盘次边界写库，查询纯读；消息自落库以便补结算完整重放。
    """
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS invest_products (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            base_price REAL NOT NULL,
            price REAL NOT NULL,
            prev_close REAL NOT NULL,
            prev_change REAL NOT NULL DEFAULT 0,
            day_open REAL NOT NULL DEFAULT 0,
            sigma REAL NOT NULL DEFAULT 0.006,
            cycle_amp REAL NOT NULL DEFAULT 0,
            cycle_phase INTEGER NOT NULL DEFAULT 0,
            price_limit REAL NOT NULL DEFAULT 0.03,
            day_limit REAL NOT NULL DEFAULT 0.08,
            day_up REAL NOT NULL DEFAULT 4.00,
            day_down REAL NOT NULL DEFAULT 0.75,
            msg_base REAL NOT NULL DEFAULT 0,
            weekly_return REAL NOT NULL DEFAULT 0,
            dividend_rate REAL NOT NULL DEFAULT 0,
            risk_level INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'trading'
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS invest_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_day INTEGER NOT NULL,
            slot INTEGER NOT NULL,
            code TEXT NOT NULL,
            pattern TEXT NOT NULL,
            direction INTEGER NOT NULL,
            base_bias REAL NOT NULL,
            strength REAL NOT NULL DEFAULT 1,
            title TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT ''
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inv_events_day ON invest_events(trade_day, slot)"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS invest_holdings (
            user_id TEXT NOT NULL,
            code TEXT NOT NULL,
            shares REAL NOT NULL DEFAULT 0,
            cost INTEGER NOT NULL DEFAULT 0,
            hold_since INTEGER NOT NULL DEFAULT 0,
            last_buy INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, code)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS invest_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            code TEXT NOT NULL,
            side TEXT NOT NULL,
            shares REAL NOT NULL,
            price REAL NOT NULL,
            amount INTEGER NOT NULL,
            fee INTEGER NOT NULL DEFAULT 0,
            tick INTEGER NOT NULL DEFAULT 0,
            backfilled INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inv_orders_user ON invest_orders(user_id, created_at DESC)"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS invest_prices (
            code TEXT NOT NULL,
            tick INTEGER NOT NULL,
            close REAL NOT NULL,
            change_rate REAL NOT NULL DEFAULT 0,
            msg_tag TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (code, tick)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS invest_dividends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            code TEXT NOT NULL,
            amount INTEGER NOT NULL,
            tick INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inv_div_user ON invest_dividends(user_id, created_at DESC)"
    )


@migration(48)
async def _migrate_to_v48(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v48 - 每日切磋/决斗次数与切磋邀请。"""
    logger.info("开始迁移到 v48：切磋/决斗每日限制")
    await _ensure_combat_limits_v48(conn)
    await conn.commit()
    logger.info("v48迁移完成：切磋/决斗每日限制表已建立")


# ===== v49：统一物品品阶名称 =====
# 旧版并存「黄阶/玄阶/地阶/天阶」与「凡品/灵品/...」两套名称，
# 自 v49 起统一为 rank_rules.RANK_TABLE 中的 12 档「品」阶。
# 这里只改名，不改任何物品的 required_level_index。


def _rank_rename_text(value):
    from ..rank_rules import RANK_ALIASES

    if not isinstance(value, str) or not value:
        return value
    for old, new in RANK_ALIASES.items():
        value = value.replace(old, new)
    return value


def _rank_rename_key_map(raw):
    """把 {物品名: 数量} 这类 JSON 的键改名；无变化返回 None。"""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    renamed = {}
    changed = False
    for key, count in data.items():
        new_key = _rank_rename_text(str(key))
        if new_key != key:
            changed = True
        if new_key in renamed:
            try:
                renamed[new_key] = int(renamed[new_key] or 0) + int(count or 0)
            except (TypeError, ValueError):
                renamed[new_key] = count
        else:
            renamed[new_key] = count
    return renamed if changed else None


def _rank_rename_instance_list(raw):
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    changed = False
    for inst in data:
        if isinstance(inst, dict) and inst.get("name"):
            new_name = _rank_rename_text(str(inst["name"]))
            if new_name != inst["name"]:
                inst["name"] = new_name
                changed = True
    return data if changed else None


def _rank_rename_text_list(raw):
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    changed = False
    result = []
    for value in data:
        new_value = _rank_rename_text(value) if isinstance(value, str) else value
        if new_value != value:
            changed = True
        result.append(new_value)
    return result if changed else None


async def _rank_rename_player_items_v49(conn: aiosqlite.Connection):
    async with conn.execute(
        "SELECT user_id, storage_ring_items, overflow_items, equipment_instances, "
        "equipped_instances, techniques, weapon, armor, main_technique FROM players"
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        user_id = row[0]
        updates = {}

        storage = _rank_rename_key_map(row[1])
        if storage is not None:
            updates["storage_ring_items"] = json.dumps(storage, ensure_ascii=False)

        overflow = _rank_rename_key_map(row[2])
        if overflow is not None:
            updates["overflow_items"] = json.dumps(overflow, ensure_ascii=False)

        instances = _rank_rename_instance_list(row[3])
        if instances is not None:
            updates["equipment_instances"] = json.dumps(instances, ensure_ascii=False)

        # 已穿戴实例（按部位保存）同样要改名，否则装备会因旧名查不到配置而失效。
        equipped = _rank_rename_instance_list(row[4])
        if equipped is not None:
            updates["equipped_instances"] = json.dumps(equipped, ensure_ascii=False)

        techniques = _rank_rename_text_list(row[5])
        if techniques is not None:
            updates["techniques"] = json.dumps(techniques, ensure_ascii=False)

        for index, column in ((6, "weapon"), (7, "armor"), (8, "main_technique")):
            value = row[index]
            new_value = _rank_rename_text(value)
            if new_value != value:
                updates[column] = new_value

        if not updates:
            continue
        assignments = ", ".join(f"{column} = ?" for column in updates)
        await conn.execute(
            f"UPDATE players SET {assignments} WHERE user_id = ?",
            (*updates.values(), user_id),
        )


async def _rank_rename_trade_tables_v49(conn: aiosqlite.Connection):
    from ..rank_rules import normalize_rank

    async with conn.execute("SELECT id, item_name FROM auction_npc_offers") as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        new_name = _rank_rename_text(row[1])
        if new_name != row[1]:
            await conn.execute("UPDATE auction_npc_offers SET item_name = ? WHERE id = ?", (new_name, row[0]))

    async with conn.execute("SELECT id, item_name FROM auction_items") as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        new_name = _rank_rename_text(row[1])
        if new_name != row[1]:
            await conn.execute("UPDATE auction_items SET item_name = ? WHERE id = ?", (new_name, row[0]))

    async with conn.execute("SELECT id, item_name FROM pending_gifts") as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        new_name = _rank_rename_text(row[1])
        if new_name != row[1]:
            await conn.execute("UPDATE pending_gifts SET item_name = ? WHERE id = ?", (new_name, row[0]))

    async with conn.execute(
        "SELECT id, item_name, item_rank, preview_label, payload_json FROM auction_house_lots"
    ) as cursor:
        rows = await cursor.fetchall()
    for row in rows:
        lot_id, item_name, item_rank, preview_label, payload_json = row
        await conn.execute(
            "UPDATE auction_house_lots SET item_name = ?, item_rank = ?, preview_label = ?, payload_json = ? WHERE id = ?",
            (
                _rank_rename_text(item_name) or "",
                normalize_rank(item_rank or "", item_rank or ""),
                _rank_rename_text(preview_label) or "",
                _rank_rename_text(payload_json) or "{}",
                lot_id,
            ),
        )

    async with conn.execute("SELECT shop_id, current_items FROM shop") as cursor:
        rows = await cursor.fetchall()
    for shop_id, raw in rows:
        try:
            items = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        changed = False
        for entry in items:
            if not isinstance(entry, dict):
                continue
            for key in ("name", "rank"):
                if isinstance(entry.get(key), str):
                    new_value = _rank_rename_text(entry[key])
                    if new_value != entry[key]:
                        entry[key] = new_value
                        changed = True
            data = entry.get("data")
            if isinstance(data, dict):
                for key in ("name", "rank"):
                    if isinstance(data.get(key), str):
                        new_value = _rank_rename_text(data[key])
                        if new_value != data[key]:
                            data[key] = new_value
                            changed = True
        if changed:
            await conn.execute(
                "UPDATE shop SET current_items = ? WHERE shop_id = ?",
                (json.dumps(items, ensure_ascii=False), shop_id),
            )


@migration(49)
async def _migrate_to_v49(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v49 - 统一物品品阶名称（仅改名，不改解锁等级）。"""
    logger.info("开始迁移到 v49：统一物品品阶名称")
    await _rank_rename_player_items_v49(conn)
    await _rank_rename_trade_tables_v49(conn)
    await conn.commit()
    logger.info("v49迁移完成：物品品阶名称已统一")


@migration(50)
async def _migrate_to_v50(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v50 - 仙市投资系统（基金/股票）。"""
    logger.info("开始迁移到 v50：仙市投资系统")
    await _ensure_invest_v50(conn)
    await conn.commit()
    logger.info("v50迁移完成：仙市投资表已建立")


@migration(51)
async def _migrate_to_v51(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v51 - 符箓熟练度与符图习得。"""
    logger.info("开始迁移到 v51：符箓熟练度与符图习得")
    await _ensure_talisman_v51(conn)
    await conn.commit()
    logger.info("v51迁移完成：符箓熟练度与符图习得已就绪")


@migration(52)
async def _migrate_to_v52(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v52 - 仙市 v6：16 盘 + 赌场级波动 + 不对称日熔断。

    给 invest_products 加 day_up / day_down 两列（不对称日熔断，取代对称 day_limit）。
    day_limit 保留用于兼容，但结算逻辑改读 day_up / day_down。
    存量标的的实际数值由 InvestManager._seed_products 强制刷新（INSERT OR REPLACE），
    这里只负责加列与默认值。
    """
    logger.info("开始迁移到 v52：仙市 v6（16 盘 + 不对称日熔断）")
    await _add_column_if_missing(conn, "invest_products", "day_up", "REAL NOT NULL DEFAULT 4.00")
    await _add_column_if_missing(conn, "invest_products", "day_down", "REAL NOT NULL DEFAULT 0.75")
    await conn.commit()
    logger.info("v52迁移完成：invest_products 已加 day_up/day_down 列")


@migration(53)
async def _migrate_to_v53(conn: aiosqlite.Connection, config_manager: ConfigManager):
    """迁移到 v53 - 合并 v6.44：拍卖会固定品阶曲线 + 分叉旧库符箓补齐。

    仅清理 scheduled/announcing 阶段的拍品：这些场次尚未开拍、没有出价，
    下次查看或开拍时会按新蓝图重新生成；进行中和已结束场次保持原样。

    另有从拍卖分叉版（v51 = 拍卖迁移）升级的存档：它们没跑过符箓 v51，
    这里按「升级前是否已有 talisman_exp 列」判断，只给这类旧库补授 5 张老符图，
    避免 v6.42+ 新玩家白发符图。
    """
    had_talisman_column = await _column_exists(conn, "players", "talisman_exp")
    await _ensure_talisman_v51(conn, grant_legacy=not had_talisman_column)
    cursor = await conn.execute(
        """DELETE FROM auction_house_lots
           WHERE event_id IN (
               SELECT id FROM auction_house_events WHERE phase IN ('scheduled', 'announcing')
           )"""
    )
    removed = max(0, int(cursor.rowcount or 0))
    await conn.commit()
    logger.info(f"v53迁移完成：已清理 {removed} 件未开拍拍品，等待按新曲线重建")
