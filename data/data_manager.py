# data/data_manager.py

import aiosqlite
import json
from dataclasses import fields
from pathlib import Path
from typing import Tuple, List, Optional
from astrbot.api import logger
from ..models import Player
from .database_extended import DatabaseExtended

# 获取 Player 模型的所有字段名（用于过滤数据库中的多余字段，作为迁移未完成时的兼容）
PLAYER_FIELDS = {f.name for f in fields(Player)}

class DataBase:
    """数据库管理类，提供基础玩家操作"""

    def __init__(self, db_file: str = "xiuxian_data_lite.db"):
        self.db_path = Path(db_file)
        self.conn: aiosqlite.Connection = None
        self.ext: Optional[DatabaseExtended] = None  # 扩展操作类

    async def connect(self):
        """连接数据库"""
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        self.ext = DatabaseExtended(self.conn)  # 初始化扩展操作

    async def close(self):
        """关闭数据库连接"""
        if self.conn:
            try:
                await self.conn.close()
            finally:
                self.conn = None
                self.ext = None

    async def reconnect(self):
        """重连数据库（用于连接意外断开时）"""
        await self.close()
        await self.connect()

    def _connection_alive(self) -> bool:
        """检测底层aiosqlite连接是否仍然可用"""
        if not self.conn:
            return False
        return getattr(self.conn, "_connection", None) is not None

    async def ensure_connection(self):
        """确保数据库连接可用，必要时自动重连"""
        if self._connection_alive():
            return
        logger.warning("[database] 检测到数据库连接断开，正在自动重连...")
        await self.reconnect()

    async def create_player(self, player: Player):
        """创建新玩家"""
        await self.conn.execute(
            """
            INSERT INTO players (
                user_id, level_index, spiritual_root, cultivation_type, user_name, title, lifespan,
                experience, gold, state, cultivation_start_time, last_check_in_date, level_up_rate,
                weapon, armor, main_technique, techniques,
                hp, mp, atk, atkpractice,
                spiritual_qi, max_spiritual_qi, blood_qi, max_blood_qi,
                magic_damage, physical_damage, magic_defense, physical_defense, mental_power,
                sect_id, sect_position, sect_contribution, sect_task, sect_elixir_get,
                blessed_spot_flag, blessed_spot_name,
                active_pill_effects, permanent_pill_gains, has_resurrection_pill, resurrection_pill_quality,
                has_debuff_shield, pills_inventory, learned_recipes,
                storage_ring, storage_ring_items, overflow_items, equipment_instances, equipped_instances,
                daily_pill_usage, last_daily_reset, shield_start_time, shield_expire_time,
                tutorial_step, reincarnation_count, fate_points, permanent_bonus, luck
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                player.user_id,
                player.level_index,
                player.spiritual_root,
                player.cultivation_type,
                player.user_name,
                player.title,
                player.lifespan,
                player.experience,
                player.gold,
                player.state,
                player.cultivation_start_time,
                player.last_check_in_date,
                player.level_up_rate,
                player.weapon,
                player.armor,
                player.main_technique,
                player.techniques,
                player.hp,
                player.mp,
                player.atk,
                player.atkpractice,
                player.spiritual_qi,
                player.max_spiritual_qi,
                player.blood_qi,
                player.max_blood_qi,
                player.magic_damage,
                player.physical_damage,
                player.magic_defense,
                player.physical_defense,
                player.mental_power,
                player.sect_id,
                player.sect_position,
                player.sect_contribution,
                player.sect_task,
                player.sect_elixir_get,
                player.blessed_spot_flag,
                player.blessed_spot_name,
                player.active_pill_effects,
                player.permanent_pill_gains,
                player.has_resurrection_pill,
                getattr(player, "resurrection_pill_quality", "一般"),
                int(player.has_debuff_shield),
                player.pills_inventory,
                getattr(player, "learned_recipes", "[]"),
                player.storage_ring,
                player.storage_ring_items,
                getattr(player, "overflow_items", "{}"),
                getattr(player, "equipment_instances", "[]"),
                getattr(player, "equipped_instances", "{}"),
                player.daily_pill_usage,
                player.last_daily_reset,
                player.shield_start_time,
                player.shield_expire_time,
                player.tutorial_step,
                player.reincarnation_count,
                player.fate_points,
                player.permanent_bonus,
                getattr(player, "luck", 0)
            )
        )
        await self.conn.commit()

    async def get_player_by_id(self, user_id: str) -> Player:
        """根据用户ID获取玩家信息"""
        async with self.conn.execute(
            "SELECT * FROM players WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                filtered_data = {k: v for k, v in dict(row).items() if k in PLAYER_FIELDS}
                return Player(**filtered_data)
            return None

    async def get_player_by_name(self, user_name: str) -> Player:
        """根据道号获取玩家信息"""
        async with self.conn.execute(
            "SELECT * FROM players WHERE user_name = ?",
            (user_name,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                filtered_data = {k: v for k, v in dict(row).items() if k in PLAYER_FIELDS}
                return Player(**filtered_data)
            return None

    async def update_player(self, player: Player, commit: bool = True):
        """更新玩家信息（支持事务传递）"""
        await self.conn.execute(
            """
            UPDATE players SET
                level_index = ?,
                spiritual_root = ?,
                cultivation_type = ?,
                user_name = ?,
                title = ?,
                lifespan = ?,
                experience = ?,
                gold = ?,
                state = ?,
                cultivation_start_time = ?,
                last_check_in_date = ?,
                level_up_rate = ?,
                weapon = ?,
                armor = ?,
                main_technique = ?,
                techniques = ?,
                hp = ?,
                mp = ?,
                atk = ?,
                atkpractice = ?,
                spiritual_qi = ?,
                max_spiritual_qi = ?,
                blood_qi = ?,
                max_blood_qi = ?,
                magic_damage = ?,
                physical_damage = ?,
                magic_defense = ?,
                physical_defense = ?,
                mental_power = ?,
                sect_id = ?,
                sect_position = ?,
                sect_contribution = ?,
                sect_task = ?,
                sect_elixir_get = ?,
                blessed_spot_flag = ?,
                blessed_spot_name = ?,
                active_pill_effects = ?,
                permanent_pill_gains = ?,
                has_resurrection_pill = ?,
                resurrection_pill_quality = ?,
                has_debuff_shield = ?,
                pills_inventory = ?,
                learned_recipes = ?,
                storage_ring = ?,
                storage_ring_items = ?,
                overflow_items = ?,
                equipment_instances = ?,
                equipped_instances = ?,
                daily_pill_usage = ?,
                last_daily_reset = ?,
                shield_start_time = ?,
                shield_expire_time = ?,
                tutorial_step = ?,
                reincarnation_count = ?,
                fate_points = ?,
                permanent_bonus = ?,
                luck = ?,
                karma = ?,
                life_skill = ?,
                virtue_points = ?
            WHERE user_id = ?
            """,
            (
                player.level_index,
                player.spiritual_root,
                player.cultivation_type,
                player.user_name,
                player.title,
                player.lifespan,
                player.experience,
                player.gold,
                player.state,
                player.cultivation_start_time,
                player.last_check_in_date,
                player.level_up_rate,
                player.weapon,
                player.armor,
                player.main_technique,
                player.techniques,
                player.hp,
                player.mp,
                player.atk,
                player.atkpractice,
                player.spiritual_qi,
                player.max_spiritual_qi,
                player.blood_qi,
                player.max_blood_qi,
                player.magic_damage,
                player.physical_damage,
                player.magic_defense,
                player.physical_defense,
                player.mental_power,
                player.sect_id,
                player.sect_position,
                player.sect_contribution,
                player.sect_task,
                player.sect_elixir_get,
                player.blessed_spot_flag,
                player.blessed_spot_name,
                player.active_pill_effects,
                player.permanent_pill_gains,
                player.has_resurrection_pill,
                getattr(player, "resurrection_pill_quality", "一般"),
                int(player.has_debuff_shield),
                player.pills_inventory,
                getattr(player, "learned_recipes", "[]"),
                player.storage_ring,
                player.storage_ring_items,
                getattr(player, "overflow_items", "{}"),
                getattr(player, "equipment_instances", "[]"),
                getattr(player, "equipped_instances", "{}"),
                player.daily_pill_usage,
                player.last_daily_reset,
                player.shield_start_time,
                player.shield_expire_time,
                player.tutorial_step,
                player.reincarnation_count,
                player.fate_points,
                player.permanent_bonus,
                getattr(player, "luck", 0),
                getattr(player, "karma", 0),
                getattr(player, "life_skill", ""),
                getattr(player, "virtue_points", 0),
                player.user_id
            )
        )
        if commit:
            await self.conn.commit()

    # ===== 增量更新工具（避免全字段覆盖导致的并发回滚）=====

    async def update_player_fields(self, user_id: str, commit: bool = True, **fields):
        """只更新指定字段（字段名必须在 Player 模型中存在），其余字段不受影响"""
        if not fields:
            return
        valid = {k: v for k, v in fields.items() if k in PLAYER_FIELDS}
        if not valid:
            return
        set_clause = ", ".join(f"{k} = ?" for k in valid)
        await self.conn.execute(
            f"UPDATE players SET {set_clause} WHERE user_id = ?",
            (*valid.values(), user_id)
        )
        if commit:
            await self.conn.commit()

    async def add_player_gold(self, user_id: str, delta: int, commit: bool = True):
        """原子增减灵石（不会覆盖其他字段的并发修改，负数结果钳为0）"""
        await self.conn.execute(
            "UPDATE players SET gold = MAX(0, gold + ?) WHERE user_id = ?",
            (delta, user_id)
        )
        if commit:
            await self.conn.commit()

    async def add_player_exp(self, user_id: str, delta: int, commit: bool = True):
        """原子增减修为（负数结果钳为0）"""
        await self.conn.execute(
            "UPDATE players SET experience = MAX(0, experience + ?) WHERE user_id = ?",
            (delta, user_id)
        )
        if commit:
            await self.conn.commit()

    async def scale_player_exp(self, user_id: str, ratio: float, commit: bool = True):
        """原子按比例缩放修为（向下取整，结果钳为0）"""
        await self.conn.execute(
            "UPDATE players SET experience = MAX(0, CAST(experience * ? AS INTEGER)) WHERE user_id = ?",
            (ratio, user_id)
        )
        if commit:
            await self.conn.commit()

    async def scale_player_hp(self, user_id: str, ratio: float, minimum: int = 1,
                              commit: bool = True):
        """原子按比例缩放气血，并保证不低于 minimum"""
        await self.conn.execute(
            "UPDATE players SET hp = MAX(?, CAST(hp * ? AS INTEGER)) WHERE user_id = ?",
            (minimum, ratio, user_id)
        )
        if commit:
            await self.conn.commit()

    async def delete_player(self, user_id: str):
        """删除玩家"""
        await self.conn.execute(
            "DELETE FROM players WHERE user_id = ?",
            (user_id,)
        )
        await self.conn.commit()

    async def delete_player_cascade(self, user_id: str):
        """级联删除玩家及所有关联数据"""
        # 先把他人的待赠予物品退回发送者，避免删除角色时连带吞掉别人的资产
        try:
            if self.ext:
                await self.ext.return_gifts_for_receiver(user_id)
        except Exception as e:
            logger.warning(f"[delete_player_cascade] 赠予退回失败: {e}")

        async def safe_execute(sql: str, params: tuple):
            try:
                await self.conn.execute(sql, params)
            except Exception as e:
                sql_preview = sql.strip().split(" ")[0]
                logger.warning(f"[delete_player_cascade] 忽略执行 {sql_preview}: {e}")

        # 如果该玩家是宗主，先把宗门传给职位最高/贡献最高的成员；没人就解散
        try:
            async with self.conn.execute(
                "SELECT sect_id FROM sects WHERE sect_owner = ?", (user_id,)
            ) as cursor:
                owned = await cursor.fetchone()
            if owned:
                sect_id = owned[0]
                async with self.conn.execute(
                    "SELECT user_id FROM players "
                    "WHERE sect_id = ? AND user_id != ? "
                    "ORDER BY sect_position ASC, sect_contribution DESC LIMIT 1",
                    (sect_id, user_id),
                ) as cursor:
                    successor = await cursor.fetchone()
                if successor:
                    await self.conn.execute(
                        "UPDATE sects SET sect_owner = ? WHERE sect_id = ?",
                        (successor[0], sect_id),
                    )
                    await self.conn.execute(
                        "UPDATE players SET sect_position = 0 WHERE user_id = ?",
                        (successor[0],),
                    )
                else:
                    if self.ext:
                        await self.ext.delete_sect(sect_id)
                    else:
                        await self.conn.execute(
                            "DELETE FROM sects WHERE sect_id = ?", (sect_id,)
                        )
        except Exception as e:
            logger.warning(f"[delete_player_cascade] 宗门交接失败: {e}")

        escaped_uid = (
            str(user_id).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        statements = [
            ("UPDATE spirit_eyes SET owner_id = NULL, owner_name = NULL, claim_time = NULL, last_collect_time = NULL WHERE owner_id = ?", (user_id,)),
            ("DELETE FROM npc_partners WHERE user_id = ?", (user_id,)),
            ("DELETE FROM master_relations WHERE master_id = ? OR disciple_id = ?", (user_id, user_id)),
            ("DELETE FROM blessed_lands WHERE user_id = ?", (user_id,)),
            ("DELETE FROM spirit_farms WHERE user_id = ?", (user_id,)),
            ("DELETE FROM bank_accounts WHERE user_id = ?", (user_id,)),
            ("UPDATE bank_loans SET status = 'bad_debt' WHERE user_id = ? AND status IN ('active', 'overdue')", (user_id,)),
            ("DELETE FROM bounty_tasks WHERE user_id = ?", (user_id,)),
            ("DELETE FROM dual_cultivation WHERE user_id = ?", (user_id,)),
            ("DELETE FROM dual_cultivation_requests WHERE from_id = ? OR target_id = ?", (user_id, user_id)),
            ("DELETE FROM combat_daily WHERE user_id = ?", (user_id,)),
            ("DELETE FROM spar_requests WHERE from_id = ? OR target_id = ?", (user_id, user_id)),
            ("DELETE FROM user_cd WHERE user_id = ?", (user_id,)),
            ("DELETE FROM auto_proxy WHERE user_id = ?", (user_id,)),
            ("DELETE FROM buff_info WHERE user_id = ?", (user_id,)),
            ("DELETE FROM impart_info WHERE user_id = ?", (user_id,)),
            ("DELETE FROM combat_cooldowns WHERE user_id = ?", (user_id,)),
            ("DELETE FROM pending_gifts WHERE sender_id = ? OR receiver_id = ?", (user_id, user_id)),
            ("DELETE FROM player_pets WHERE user_id = ?", (user_id,)),
            ("DELETE FROM daily_tasks WHERE user_id = ?", (user_id,)),
            ("DELETE FROM world_boss_damage WHERE user_id = ?", (user_id,)),
            ("DELETE FROM world_boss_raid_damage WHERE user_id = ?", (user_id,)),
            ("DELETE FROM battle_event_damage WHERE user_id = ?", (user_id,)),
            ("DELETE FROM auction_house_entries WHERE user_id = ?", (user_id,)),
            ("DELETE FROM auction_npc_offers WHERE seller_id = ?", (user_id,)),
            ("DELETE FROM auction_items WHERE seller_id = ?", (user_id,)),
            ("DELETE FROM sect_tournament_entries WHERE user_id = ?", (user_id,)),
            ("UPDATE sect_orders SET status = 'cancelled' WHERE status = 'active' AND (issuer_id = ? OR target_id = ?)", (user_id, user_id)),
            # 清理用户专属冷却/次数，但保留重修冷却键（随后会写入新的时间）
            (
                "DELETE FROM system_config WHERE (key LIKE ? ESCAPE '\\' OR key = ?) AND key NOT IN (?, ?)",
                (f"%\\_{escaped_uid}", user_id, f"rebirth_last_{user_id}", f"rebirth_total_{user_id}"),
            ),
        ]

        for sql, params in statements:
            await safe_execute(sql, params)

        await self.conn.execute("DELETE FROM players WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def get_all_players(self):
        """获取所有玩家"""
        async with self.conn.execute("SELECT * FROM players") as cursor:
            rows = await cursor.fetchall()
            return [Player(**{k: v for k, v in dict(row).items() if k in PLAYER_FIELDS}) for row in rows]

    # ===== 商店数据操作 =====

    async def get_shop_data(self, shop_id: str = "global") -> Tuple[int, List[dict]]:
        """获取商店数据"""
        async with self.conn.execute(
            "SELECT last_refresh_time, current_items FROM shop WHERE shop_id = ?",
            (shop_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                last_refresh_time = row[0]
                try:
                    current_items = json.loads(row[1]) if row[1] else []
                except (json.JSONDecodeError, TypeError, ValueError):
                    current_items = []
                return last_refresh_time, current_items
            return 0, []

    async def update_shop_data(self, shop_id: str, last_refresh_time: int, current_items: List[dict]):
        """更新商店数据"""
        items_json = json.dumps(current_items, ensure_ascii=False)
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO shop (shop_id, last_refresh_time, current_items)
            VALUES (?, ?, ?)
            """,
            (shop_id, last_refresh_time, items_json)
        )
        await self.conn.commit()

    async def decrement_shop_item_stock(self, shop_id: str, item_name: str, quantity: int = 1, external_transaction: bool = False) -> tuple[bool, int, int]:
        """尝试扣减指定商店物品的库存（原子操作，可批量）"""
        quantity = max(1, int(quantity))
        if not external_transaction:
            await self.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.conn.execute(
                "SELECT last_refresh_time, current_items FROM shop WHERE shop_id = ?",
                (shop_id,)
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                if not external_transaction:
                    await self.conn.rollback()
                return False, 0, 0

            last_refresh_time = row[0]
            try:
                current_items = json.loads(row[1]) if row[1] else []
            except (json.JSONDecodeError, TypeError, ValueError):
                current_items = []

            target_index = -1
            for idx, item in enumerate(current_items):
                if item.get('name') == item_name:
                    target_index = idx
                    break

            if target_index == -1:
                if not external_transaction:
                    await self.conn.rollback()
                return False, last_refresh_time, 0

            stock = current_items[target_index].get('stock', 0)
            if stock is None or stock <= 0:
                if not external_transaction:
                    await self.conn.rollback()
                return False, last_refresh_time, max(stock or 0, 0)

            if stock < quantity:
                if not external_transaction:
                    await self.conn.rollback()
                return False, last_refresh_time, stock

            new_stock = stock - quantity
            current_items[target_index]['stock'] = new_stock

            items_json = json.dumps(current_items, ensure_ascii=False)
            await self.conn.execute(
                "UPDATE shop SET current_items = ?, last_refresh_time = ? WHERE shop_id = ?",
                (items_json, last_refresh_time, shop_id)
            )
            if not external_transaction:
                await self.conn.commit()
            return True, last_refresh_time, new_stock
        except Exception:
            if not external_transaction:
                await self.conn.rollback()
            raise

    async def increment_shop_item_stock(self, shop_id: str, item_name: str, quantity: int = 1):
        """回滚库存（在购买失败时恢复库存），支持批量"""
        quantity = max(1, int(quantity))
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.conn.execute(
                "SELECT last_refresh_time, current_items FROM shop WHERE shop_id = ?",
                (shop_id,)
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                await self.conn.rollback()
                return

            last_refresh_time = row[0]
            try:
                current_items = json.loads(row[1]) if row[1] else []
            except (json.JSONDecodeError, TypeError, ValueError):
                current_items = []

            for item in current_items:
                if item.get('name') == item_name:
                    current_stock = item.get('stock', 0) or 0
                    item['stock'] = current_stock + quantity
                    break

            items_json = json.dumps(current_items, ensure_ascii=False)
            await self.conn.execute(
                "UPDATE shop SET current_items = ?, last_refresh_time = ? WHERE shop_id = ?",
                (items_json, last_refresh_time, shop_id)
            )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise