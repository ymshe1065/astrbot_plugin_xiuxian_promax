# data/database_extended.py
"""
扩展数据库操作类，包含宗门、Boss、秘境等新系统的CRUD方法
"""

import aiosqlite
import json
import time
from typing import List, Optional
from ..models_extended import (
    Sect, BuffInfo, Boss, Rift, ImpartInfo, UserCd, UserStatus
)

# players.state 的展示文案：闭关沿用既有的“修炼中”，其余用 UserStatus 名称
_STATE_TEXT = {
    UserStatus.IDLE: "空闲",
    UserStatus.CULTIVATING: "修炼中",
    UserStatus.ADVENTURING: "历练中",
    UserStatus.EXPLORING: "探索秘境中",
    UserStatus.SECT_TASK: "宗门任务中",
    UserStatus.TRIBULATION: "渡劫中",
    UserStatus.DELIVERING: "押镖中",
    UserStatus.RAIDING: "掠夺中",
    UserStatus.SECT_BATTLE: "宗门大比中",
}


class DatabaseExtended:
    """数据库扩展操作类"""
    
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        self._bounty_tables_ensured = False  # 避免每次查询都执行 DDL+commit 破坏外层事务
    
    # ===== 宗门系统 CRUD =====
    
    async def create_sect(self, sect: Sect):
        """创建宗门"""
        await self.conn.execute(
            """
            INSERT INTO sects (
                sect_name, sect_owner, sect_scale, sect_used_stone,
                sect_fairyland, sect_materials, mainbuff, secbuff, elixir_room_level
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sect.sect_name, sect.sect_owner, sect.sect_scale,
                sect.sect_used_stone, sect.sect_fairyland, sect.sect_materials,
                sect.mainbuff, sect.secbuff, sect.elixir_room_level
            )
        )
        await self.conn.commit()
        
        # 获取刚插入的sect_id
        async with self.conn.execute("SELECT last_insert_rowid()") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
    
    async def get_sect_by_id(self, sect_id: int) -> Optional[Sect]:
        """根据ID获取宗门信息"""
        async with self.conn.execute(
            "SELECT * FROM sects WHERE sect_id = ?",
            (sect_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return Sect(**dict(row))
            return None
    
    async def get_sect_by_owner(self, owner_id: str) -> Optional[Sect]:
        """根据宗主ID获取宗门信息"""
        async with self.conn.execute(
            "SELECT * FROM sects WHERE sect_owner = ?",
            (owner_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return Sect(**dict(row))
            return None
    
    async def get_sect_by_name(self, sect_name: str) -> Optional[Sect]:
        """根据宗门名称获取宗门信息"""
        async with self.conn.execute(
            "SELECT * FROM sects WHERE sect_name = ?",
            (sect_name,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return Sect(**dict(row))
            return None
    
    async def update_sect(self, sect: Sect):
        """更新宗门信息"""
        await self.conn.execute(
            """
            UPDATE sects SET
                sect_name = ?, sect_owner = ?, sect_scale = ?, sect_used_stone = ?,
                sect_fairyland = ?, sect_materials = ?, mainbuff = ?, secbuff = ?,
                elixir_room_level = ?, buildings = ?, sect_level = ?, sect_exp = ?,
                sect_war_cd_until = ?, sect_war_protect_until = ?, sect_tournament_cd_until = ?
            WHERE sect_id = ?
            """,
            (
                sect.sect_name, sect.sect_owner, sect.sect_scale,
                sect.sect_used_stone, sect.sect_fairyland, sect.sect_materials,
                sect.mainbuff, sect.secbuff, sect.elixir_room_level,
                getattr(sect, "buildings", "{}"),
                getattr(sect, "sect_level", 1), getattr(sect, "sect_exp", 0),
                getattr(sect, "sect_war_cd_until", 0),
                getattr(sect, "sect_war_protect_until", 0),
                getattr(sect, "sect_tournament_cd_until", 0),
                sect.sect_id
            )
        )
        await self.conn.commit()
    
    async def delete_sect(self, sect_id: int):
        """解散宗门：清理成员归属、宗门任务、命令、大比、战争、商店和宗门行。"""
        try:
            await self.ensure_system_config_table()
        except Exception:
            pass
        started_transaction = False
        if not self.conn.in_transaction:
            await self.conn.execute("BEGIN IMMEDIATE")
            started_transaction = True
        try:
            async with self.conn.execute(
                "SELECT user_id FROM players WHERE sect_id = ?", (sect_id,)
            ) as cursor:
                member_ids = [row[0] for row in await cursor.fetchall()]

            if member_ids:
                placeholders = ",".join("?" for _ in member_ids)
                # 只释放正在做宗门任务的成员，其它闭关/历练状态不打断
                await self.conn.execute(
                    f"UPDATE players SET state = '空闲' WHERE user_id IN ({placeholders}) "
                    "AND user_id IN (SELECT user_id FROM user_cd WHERE type = ?)",
                    (*member_ids, int(UserStatus.SECT_TASK)),
                )
                await self.conn.execute(
                    f"UPDATE user_cd SET type = 0, create_time = 0, scheduled_time = 0, "
                    f"extra_data = '{{}}' WHERE user_id IN ({placeholders}) AND type = ?",
                    (*member_ids, int(UserStatus.SECT_TASK)),
                )
                await self.conn.execute(
                    f"UPDATE players SET sect_id = 0, sect_position = 0, "
                    f"sect_contribution = 0, sect_debt = 0 "
                    f"WHERE user_id IN ({placeholders})",
                    member_ids,
                )

            await self.conn.execute("DELETE FROM sect_orders WHERE sect_id = ?", (sect_id,))
            await self.conn.execute(
                "DELETE FROM sect_tournament_entries WHERE tournament_id IN "
                "(SELECT id FROM sect_tournaments WHERE sect_id = ?)", (sect_id,)
            )
            await self.conn.execute("DELETE FROM sect_tournaments WHERE sect_id = ?", (sect_id,))
            await self.conn.execute(
                "DELETE FROM sect_wars WHERE attacker_id = ? OR defender_id = ?",
                (sect_id, sect_id),
            )
            await self.conn.execute(
                "DELETE FROM shop WHERE shop_id = ?", (f"sect_shop_{sect_id}",)
            )
            await self.conn.execute(
                "DELETE FROM system_config WHERE key = ?", (f"sect_score_{sect_id}",)
            )
            await self.conn.execute("DELETE FROM sects WHERE sect_id = ?", (sect_id,))
            if started_transaction:
                await self.conn.commit()
        except Exception:
            if started_transaction:
                await self.conn.rollback()
            raise
    
    async def get_all_sects(self) -> List[Sect]:
        """获取所有宗门"""
        async with self.conn.execute("SELECT * FROM sects ORDER BY sect_scale DESC") as cursor:
            rows = await cursor.fetchall()
            return [Sect(**dict(row)) for row in rows]
    
    async def update_sect_materials(self, sect_id: int, materials: int, operation: int = 1):
        """更新宗门资材
        
        Args:
            sect_id: 宗门ID
            materials: 资材数量
            operation: 1=增加, 2=减少
        """
        if operation == 1:
            await self.conn.execute(
                "UPDATE sects SET sect_materials = sect_materials + ? WHERE sect_id = ?",
                (materials, sect_id)
            )
        else:
            await self.conn.execute(
                "UPDATE sects SET sect_materials = sect_materials - ? WHERE sect_id = ?",
                (materials, sect_id)
            )
        await self.conn.commit()
    
    async def donate_to_sect(self, sect_id: int, stone_num: int):
        """宗门捐献（增加灵石和建设度）"""
        await self.conn.execute(
            """
            UPDATE sects SET 
                sect_used_stone = sect_used_stone + ?,
                sect_scale = sect_scale + ?
            WHERE sect_id = ?
            """,
            (stone_num, stone_num * 10, sect_id)  # 1灵石 = 10建设度
        )
        await self.conn.commit()
    
    # ===== BuffInfo 系统 CRUD =====
    
    async def create_buff_info(self, user_id: str):
        """初始化用户的buff信息"""
        await self.conn.execute(
            """
            INSERT INTO buff_info (
                user_id, main_buff, sec_buff, faqi_buff, fabao_weapon,
                armor_buff, atk_buff, blessed_spot, sub_buff
            ) VALUES (?, 0, 0, 0, 0, 0, 0, 0, 0)
            """,
            (user_id,)
        )
        await self.conn.commit()
    
    async def get_buff_info(self, user_id: str) -> Optional[BuffInfo]:
        """获取用户buff信息"""
        async with self.conn.execute(
            "SELECT * FROM buff_info WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return BuffInfo(**dict(row))
            return None
    
    async def update_buff_info(self, buff_info: BuffInfo):
        """更新用户buff信息"""
        await self.conn.execute(
            """
            UPDATE buff_info SET
                main_buff = ?, sec_buff = ?, faqi_buff = ?, fabao_weapon = ?,
                armor_buff = ?, atk_buff = ?, blessed_spot = ?, sub_buff = ?
            WHERE user_id = ?
            """,
            (
                buff_info.main_buff, buff_info.sec_buff, buff_info.faqi_buff,
                buff_info.fabao_weapon, buff_info.armor_buff, buff_info.atk_buff,
                buff_info.blessed_spot, buff_info.sub_buff, buff_info.user_id
            )
        )
        await self.conn.commit()
    
    async def update_user_main_buff(self, user_id: str, buff_id: int):
        """更新用户主修功法"""
        await self.conn.execute(
            "UPDATE buff_info SET main_buff = ? WHERE user_id = ?",
            (buff_id, user_id)
        )
        await self.conn.commit()
    
    async def update_user_sec_buff(self, user_id: str, buff_id: int):
        """更新用户辅修功法"""
        await self.conn.execute(
            "UPDATE buff_info SET sec_buff = ? WHERE user_id = ?",
            (buff_id, user_id)
        )
        await self.conn.commit()
    
    # ===== Boss 系统 CRUD =====
    
    async def create_boss(self, boss: Boss) -> int:
        """创建Boss"""
        await self.conn.execute(
            """
            INSERT INTO boss (
                boss_name, boss_level, hp, max_hp, atk, defense,
                stone_reward, create_time, status, archetype
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                boss.boss_name, boss.boss_level, boss.hp, boss.max_hp,
                boss.atk, boss.defense, boss.stone_reward,
                boss.create_time, boss.status, getattr(boss, "archetype", "balanced")
            )
        )
        await self.conn.commit()
        
        async with self.conn.execute("SELECT last_insert_rowid()") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
    
    async def get_active_boss(self) -> Optional[Boss]:
        """获取当前存活的Boss"""
        async with self.conn.execute(
            "SELECT * FROM boss WHERE status = 1 ORDER BY create_time DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return Boss(**dict(row))
            return None
    
    async def get_boss_by_id(self, boss_id: int) -> Optional[Boss]:
        """根据ID获取Boss信息"""
        async with self.conn.execute(
            "SELECT * FROM boss WHERE boss_id = ?",
            (boss_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return Boss(**dict(row))
            return None
    
    async def update_boss(self, boss: Boss):
        """更新Boss信息"""
        await self.conn.execute(
            """
            UPDATE boss SET
                boss_name = ?, boss_level = ?, hp = ?, max_hp = ?, atk = ?,
                defense = ?, stone_reward = ?, status = ?, archetype = ?
            WHERE boss_id = ?
            """,
            (
                boss.boss_name, boss.boss_level, boss.hp, boss.max_hp,
                boss.atk, boss.defense, boss.stone_reward, boss.status,
                getattr(boss, "archetype", "balanced"), boss.boss_id
            )
        )
        await self.conn.commit()
    
    async def defeat_boss(self, boss_id: int):
        """标记Boss为已击败"""
        await self.conn.execute(
            "UPDATE boss SET status = 0 WHERE boss_id = ?",
            (boss_id,)
        )
        await self.conn.commit()
    
    # ===== 秘境系统 CRUD =====
    
    async def create_rift(self, rift: Rift) -> int:
        """创建秘境"""
        await self.conn.execute(
            """
            INSERT INTO rifts (
                rift_name, rift_level, required_level, rewards
            ) VALUES (?, ?, ?, ?)
            """,
            (rift.rift_name, rift.rift_level, rift.required_level, rift.rewards)
        )
        await self.conn.commit()
        
        async with self.conn.execute("SELECT last_insert_rowid()") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
    
    async def get_rift_by_id(self, rift_id: int) -> Optional[Rift]:
        """根据ID获取秘境信息"""
        async with self.conn.execute(
            "SELECT * FROM rifts WHERE rift_id = ?",
            (rift_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return Rift(**dict(row))
            return None
    
    async def get_all_rifts(self) -> List[Rift]:
        """获取所有秘境"""
        async with self.conn.execute(
            "SELECT * FROM rifts ORDER BY rift_level ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [Rift(**dict(row)) for row in rows]
    
    # ===== 传承系统 CRUD =====
    
    async def create_impart_info(self, user_id: str):
        """初始化用户传承信息"""
        await self.conn.execute(
            """
            INSERT INTO impart_info (
                user_id, impart_hp_per, impart_mp_per, impart_atk_per,
                impart_know_per, impart_burst_per
            ) VALUES (?, 0.0, 0.0, 0.0, 0.0, 0.0)
            """,
            (user_id,)
        )
        await self.conn.commit()

    async def ensure_impart_info(self, user_id: str) -> None:
        """幂等确保传承记录存在（兼容旧库与新角色）。"""
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO impart_info (
                user_id, impart_hp_per, impart_mp_per, impart_atk_per,
                impart_know_per, impart_burst_per
            ) VALUES (?, 0.0, 0.0, 0.0, 0.0, 0.0)
            """,
            (user_id,)
        )
        await self.conn.commit()
    
    async def get_impart_info(self, user_id: str) -> Optional[ImpartInfo]:
        """获取用户传承信息"""
        async with self.conn.execute(
            "SELECT * FROM impart_info WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return ImpartInfo(**dict(row))
            return None

    async def get_all_impart_info(self) -> dict:
        """批量获取所有传承信息（排行榜等全服遍历场景，避免逐玩家查询）"""
        result = {}
        async with self.conn.execute("SELECT * FROM impart_info") as cursor:
            async for row in cursor:
                info = ImpartInfo(**dict(row))
                result[info.user_id] = info
        return result
    
    async def update_impart_info(self, impart: ImpartInfo):
        """更新用户传承信息"""
        await self.conn.execute(
            """
            UPDATE impart_info SET
                impart_hp_per = ?, impart_mp_per = ?, impart_atk_per = ?,
                impart_know_per = ?, impart_burst_per = ?
            WHERE user_id = ?
            """,
            (
                impart.impart_hp_per, impart.impart_mp_per, impart.impart_atk_per,
                impart.impart_know_per, impart.impart_burst_per, impart.user_id
            )
        )
        await self.conn.commit()
    
    # ===== 用户CD系统 CRUD =====
    
    async def create_user_cd(self, user_id: str):
        """初始化用户CD信息（防呆版）"""
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO user_cd (user_id, type, create_time, scheduled_time, extra_data) 
            VALUES (?, 0, 0, 0, '{}')
            """,
            (user_id,)
        )
        await self.conn.commit()
    
    async def get_user_cd(self, user_id: str) -> Optional[UserCd]:
        """获取用户CD信息"""
        async with self.conn.execute(
            "SELECT * FROM user_cd WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return UserCd(**dict(row))
            return None
    
    async def update_user_cd(self, user_cd: UserCd):
        """更新用户CD信息"""
        await self.conn.execute(
            """
            UPDATE user_cd SET
                type = ?, create_time = ?, scheduled_time = ?, extra_data = ?
            WHERE user_id = ?
            """,
            (user_cd.type, user_cd.create_time, user_cd.scheduled_time, user_cd.extra_data, user_cd.user_id)
        )
        await self.conn.commit()
    
    async def set_user_busy(self, user_id: str, busy_type: int, scheduled_time: int = 0, extra_data: dict = None, commit: bool = True):
        """设置用户忙碌状态（防呆版）。commit=False 用于外层事务。"""
        import time
        import json
        extra_json = json.dumps(extra_data or {}, ensure_ascii=False)
        # 使用 UPSERT 语法，如果用户不存在则插入，存在则更新，绝对不会报 no such column
        await self.conn.execute(
            """
            INSERT INTO user_cd (user_id, type, create_time, scheduled_time, extra_data) 
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET 
                type = excluded.type,
                create_time = excluded.create_time,
                scheduled_time = excluded.scheduled_time,
                extra_data = excluded.extra_data
            """,
            (user_id, busy_type, int(time.time()), scheduled_time, extra_json)
        )
        # 同步 players.state，保证“我的信息”等界面显示真实状态
        state_text = _STATE_TEXT.get(busy_type, "忙碌中")
        await self.conn.execute(
            "UPDATE players SET state = ? WHERE user_id = ?",
            (state_text, user_id),
        )
        if commit:
            await self.conn.commit()
    
    async def set_user_free(self, user_id: str, commit: bool = True):
        """设置用户为空闲状态"""
        await self.set_user_busy(user_id, 0, 0, commit=commit)

    # ===== v6.26 自动代理 =====

    async def get_auto_proxy(self, user_id: str) -> Optional[dict]:
        """获取玩家的自动代理配置"""
        async with self.conn.execute(
            "SELECT * FROM auto_proxy WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def upsert_auto_proxy(
        self,
        user_id: str,
        enabled: int,
        task_type: str = "",
        origin: str = "",
        last_error: str = "",
        commit: bool = True,
    ):
        """写入自动代理配置（不存在则插入）"""
        await self.conn.execute(
            """
            INSERT INTO auto_proxy (user_id, enabled, task_type, origin, last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled = excluded.enabled,
                task_type = excluded.task_type,
                origin = excluded.origin,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (user_id, int(enabled), task_type or "", origin or "", last_error or "", int(time.time())),
        )
        if commit:
            await self.conn.commit()

    async def list_enabled_auto_proxies(self) -> List[dict]:
        """列出所有已开启的代理，并附带当前 user_cd 状态"""
        async with self.conn.execute(
            """
            SELECT ap.*, uc.type AS cd_type, uc.scheduled_time AS cd_scheduled_time
            FROM auto_proxy ap
            LEFT JOIN user_cd uc ON uc.user_id = ap.user_id
            WHERE ap.enabled = 1
            ORDER BY ap.updated_at ASC
            """
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def delete_auto_proxy(self, user_id: str):
        """删除代理记录（玩家数据被清理时使用）"""
        await self.conn.execute("DELETE FROM auto_proxy WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    # ===== Player扩展字段更新方法 =====
    
    async def update_player_hp_mp(self, user_id: str, hp: int, mp: int):
        """更新玩家HP和MP"""
        await self.conn.execute(
            "UPDATE players SET hp = ?, mp = ? WHERE user_id = ?",
            (hp, mp, user_id)
        )
        await self.conn.commit()
    
    async def update_player_sect_info(self, user_id: str, sect_id: int, sect_position: int):
        """更新玩家宗门信息"""
        await self.conn.execute(
            "UPDATE players SET sect_id = ?, sect_position = ? WHERE user_id = ?",
            (sect_id, sect_position, user_id)
        )
        await self.conn.commit()
    
    async def settle_sect_debt(self, user_id: str) -> int:
        """用当前灵石自动偿还宗门欠款，返回实际还款额。"""
        # 用 SQL 直接处理，避免依赖上层 DataBase 实例
        async with self.conn.execute(
            "SELECT gold, sect_debt, sect_id FROM players WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return 0
        gold, debt, sect_id = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        if debt <= 0 or gold <= 0 or sect_id <= 0:
            return 0
        pay = min(gold, debt)
        await self.conn.execute(
            "UPDATE players SET gold = gold - ?, sect_debt = sect_debt - ? WHERE user_id = ?",
            (pay, pay, user_id),
        )
        await self.conn.execute(
            "UPDATE sects SET sect_used_stone = sect_used_stone + ? WHERE sect_id = ?",
            (pay, sect_id),
        )
        await self.conn.commit()
        return pay

    async def settle_all_sect_debts(self) -> int:
        """扫描并偿还所有宗门欠款，返回总还款额。"""
        async with self.conn.execute(
            "SELECT user_id FROM players WHERE sect_debt > 0 AND gold > 0 AND sect_id > 0"
        ) as cursor:
            rows = await cursor.fetchall()
        total = 0
        for row in rows:
            total += await self.settle_sect_debt(row[0])
        return total

    async def update_player_sect_contribution(self, user_id: str, contribution: int):
        """更新玩家宗门贡献度"""
        await self.conn.execute(
            "UPDATE players SET sect_contribution = ? WHERE user_id = ?",
            (contribution, user_id)
        )
        await self.conn.commit()
    
    async def increment_sect_task_count(self, user_id: str, count: int = 1):
        """增加宗门任务完成次数"""
        await self.conn.execute(
            "UPDATE players SET sect_task = sect_task + ? WHERE user_id = ?",
            (count, user_id)
        )
        await self.conn.commit()
    
    async def reset_sect_tasks(self):
        """重置所有用户的宗门任务次数（定时任务）"""
        await self.conn.execute("UPDATE players SET sect_task = 0")
        await self.conn.commit()
    
    async def reset_sect_elixir_get(self):
        """重置所有用户的宗门丹药领取标记（定时任务）"""
        await self.conn.execute("UPDATE players SET sect_elixir_get = 0")
        await self.conn.commit()
    
    async def get_sect_members(self, sect_id: int) -> List:
        """获取宗门所有成员"""
        from ..models import Player
        async with self.conn.execute(
            "SELECT * FROM players WHERE sect_id = ? ORDER BY sect_position ASC, level_index DESC",
            (sect_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            # 简化返回，只返回部分字段
            from dataclasses import fields
            PLAYER_FIELDS = {f.name for f in fields(Player)}
            return [Player(**{k: v for k, v in dict(row).items() if k in PLAYER_FIELDS}) for row in rows]
    
    # ===== Phase 2: 灵石银行 CRUD =====
    
    async def get_bank_account(self, user_id: str) -> Optional[dict]:
        """获取银行账户信息"""
        async with self.conn.execute(
            "SELECT balance, last_interest_time FROM bank_accounts WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"balance": row[0], "last_interest_time": row[1]}
            return None
    
    async def update_bank_account(self, user_id: str, balance: int, last_interest_time: int):
        """更新或创建银行账户"""
        await self.conn.execute(
            """
            INSERT INTO bank_accounts (user_id, balance, last_interest_time)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET 
                balance = excluded.balance,
                last_interest_time = excluded.last_interest_time
            """,
            (user_id, balance, last_interest_time)
        )
        await self.conn.commit()
    
    # ===== Phase 2: 悬赏令系统 CRUD =====
    
    async def ensure_bounty_tables(self):
        """确保悬赏系统表存在（运行时检查）"""
        await self.conn.execute("""
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
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_bounty_user ON bounty_tasks(user_id)")
        await self.conn.commit()
    
    async def get_active_bounty(self, user_id: str) -> Optional[dict]:
        """获取用户当前进行中的悬赏任务"""
        if not self._bounty_tables_ensured:
            await self.ensure_bounty_tables()  # 仅首次确保表存在
            self._bounty_tables_ensured = True
        async with self.conn.execute(
            "SELECT * FROM bounty_tasks WHERE user_id = ? AND status = 1",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None
    
    async def create_bounty(self, user_id: str, bounty_id: int, bounty_name: str, 
                           target_type: str, target_count: int, rewards: str, 
                           expire_time: int):
        """创建悬赏任务"""
        import time
        await self.conn.execute(
            """
            INSERT INTO bounty_tasks (
                user_id, bounty_id, bounty_name, target_type, 
                target_count, current_progress, rewards, 
                start_time, expire_time, status
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 1)
            """,
            (user_id, bounty_id, bounty_name, target_type, 
             target_count, rewards, int(time.time()), expire_time)
        )
        await self.conn.commit()
    
    async def update_bounty_progress(self, user_id: str, progress: int):
        """更新悬赏任务进度"""
        await self.conn.execute(
            "UPDATE bounty_tasks SET current_progress = ? WHERE user_id = ? AND status = 1",
            (progress, user_id)
        )
        await self.conn.commit()
    
    async def complete_bounty(self, user_id: str) -> bool:
        """完成悬赏任务"""
        await self.conn.execute(
            "UPDATE bounty_tasks SET status = 2 WHERE user_id = ? AND status = 1",
            (user_id,)
        )
        await self.conn.commit()
        return True
    
    async def cancel_bounty(self, user_id: str):
        """取消悬赏任务"""
        await self.conn.execute(
            "UPDATE bounty_tasks SET status = 0 WHERE user_id = ? AND status = 1",
            (user_id,)
        )
        await self.conn.commit()
    
    # ===== 系统配置 CRUD =====
    
    async def ensure_system_config_table(self):
        """确保系统配置表存在（仅首次执行 DDL，避免隐式 commit 破坏外层事务）"""
        if getattr(self, "_system_config_ensured", False):
            return
        started_transaction = False
        if not self.conn.in_transaction:
            await self.conn.execute("BEGIN IMMEDIATE")
            started_transaction = True
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER DEFAULT 0
            )
        """)
        if started_transaction:
            await self.conn.commit()
        self._system_config_ensured = True
    
    async def get_system_config(self, key: str) -> Optional[str]:
        """获取系统配置"""
        await self.ensure_system_config_table()
        async with self.conn.execute(
            "SELECT value FROM system_config WHERE key = ?",
            (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
    
    async def set_system_config(self, key: str, value: str):
        """设置系统配置"""
        import time
        await self.ensure_system_config_table()
        await self.conn.execute(
            """
            INSERT INTO system_config (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
            """,
            (key, value, int(time.time()), value, int(time.time()))
        )
        await self.conn.commit()
    
    # ===== 赠予请求系统 CRUD =====
    
    async def create_pending_gift(self, receiver_id: str, sender_id: str, sender_name: str,
                                   item_name: str, count: int, expires_hours: int = 24,
                                   item_kind: str = "item", item_quality: str = "",
                                   instance_id: int = 0) -> int:
        """创建赠予请求
        
        Args:
            receiver_id: 接收者ID
            sender_id: 发送者ID
            sender_name: 发送者名称
            item_name: 物品名称
            count: 物品数量
            expires_hours: 过期时间（小时），默认24小时
            
        Returns:
            新创建的赠予请求ID
        """
        import time
        now = int(time.time())
        expires_at = now + expires_hours * 3600
        
        await self.conn.execute(
            """
            INSERT INTO pending_gifts (
                receiver_id, sender_id, sender_name, item_name, count, created_at, expires_at,
                item_kind, item_quality, instance_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (receiver_id, sender_id, sender_name, item_name, count, now, expires_at, item_kind, item_quality, instance_id)
        )
        await self.conn.commit()
        
        async with self.conn.execute("SELECT last_insert_rowid()") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
    
    async def get_pending_gift(self, receiver_id: str, cleanup: bool = True) -> Optional[dict]:
        """获取接收者的待处理赠予请求（最新的一个）。

        cleanup=False 用于外层已开启事务的接收流程，避免过期清理内部 commit 破坏原子性。
        """
        import time
        now = int(time.time())
        
        if cleanup:
            await self.cleanup_expired_gifts()
        
        async with self.conn.execute(
            """
            SELECT id, receiver_id, sender_id, sender_name, item_name, count, created_at, expires_at,
                   item_kind, item_quality, instance_id
            FROM pending_gifts 
            WHERE receiver_id = ? AND expires_at > ?
            ORDER BY created_at DESC 
            LIMIT 1
            """,
            (receiver_id, now)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "receiver_id": row[1],
                    "sender_id": row[2],
                    "sender_name": row[3],
                    "item_name": row[4],
                    "count": row[5],
                    "created_at": row[6],
                    "expires_at": row[7],
                    "item_kind": row[8] if len(row) > 8 else "item",
                    "item_quality": row[9] if len(row) > 9 else "",
                    "instance_id": row[10] if len(row) > 10 else 0
                }
            return None
    
    async def get_all_pending_gifts(self, receiver_id: str) -> List[dict]:
        """获取接收者的所有待处理赠予请求"""
        import time
        now = int(time.time())
        
        async with self.conn.execute(
            """
            SELECT id, receiver_id, sender_id, sender_name, item_name, count, created_at, expires_at,
                   item_kind, item_quality, instance_id
            FROM pending_gifts 
            WHERE receiver_id = ? AND expires_at > ?
            ORDER BY created_at DESC
            """,
            (receiver_id, now)
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": row[0],
                    "receiver_id": row[1],
                    "sender_id": row[2],
                    "sender_name": row[3],
                    "item_name": row[4],
                    "count": row[5],
                    "created_at": row[6],
                    "expires_at": row[7],
                    "item_kind": row[8] if len(row) > 8 else "item",
                    "item_quality": row[9] if len(row) > 9 else "",
                    "instance_id": row[10] if len(row) > 10 else 0
                }
                for row in rows
            ]
    
    async def delete_pending_gift(self, gift_id: int):
        """删除赠予请求"""
        await self.conn.execute(
            "DELETE FROM pending_gifts WHERE id = ?",
            (gift_id,)
        )
        await self.conn.commit()
    
    async def delete_pending_gift_by_receiver(self, receiver_id: str):
        """删除接收者的所有赠予请求"""
        await self.conn.execute(
            "DELETE FROM pending_gifts WHERE receiver_id = ?",
            (receiver_id,)
        )
        await self.conn.commit()
    
    async def _return_gift_to_sender(self, gift: dict):
        """把赠予请求中的物品退回发送者，避免过期/清理时资产消失。"""
        sender_id = gift.get("sender_id")
        item_name = gift.get("item_name")
        try:
            count = int(gift.get("count", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        if not sender_id or not item_name or count <= 0:
            return
        async with self.conn.execute(
            "SELECT pills_inventory, storage_ring_items FROM players WHERE user_id = ?",
            (sender_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return
        try:
            pills = json.loads(row[0]) if row[0] else {}
        except Exception:
            pills = {}
        try:
            storage = json.loads(row[1]) if row[1] else {}
        except Exception:
            storage = {}
        if not isinstance(pills, dict):
            pills = {}
        if not isinstance(storage, dict):
            storage = {}
        equipment_instances = None
        if gift.get("item_kind") == "pill":
            quality = gift.get("item_quality") or "一般"
            key = f"{item_name}|{quality}"
            pills[key] = int(pills.get(key, 0) or 0) + count
        elif gift.get("item_kind") == "equipment":
            async with self.conn.execute(
                "SELECT equipment_instances FROM players WHERE user_id = ?", (sender_id,)
            ) as cursor:
                eq_row = await cursor.fetchone()
            try:
                equipment_instances = json.loads(eq_row[0]) if eq_row and eq_row[0] else []
            except Exception:
                equipment_instances = []
            if not isinstance(equipment_instances, list):
                equipment_instances = []
            for inst in equipment_instances:
                if isinstance(inst, dict) and int(inst.get("id", 0) or 0) == int(gift.get("instance_id", 0) or 0):
                    # 过期赠予退回溢出位置，避免直接塞满储物戒；玩家可用 /领取溢出 取回
                    inst["location"] = "overflow"
                    inst["slot"] = ""
                    break
        else:
            storage[item_name] = int(storage.get(item_name, 0) or 0) + count
        if equipment_instances is not None:
            await self.conn.execute(
                "UPDATE players SET pills_inventory = ?, storage_ring_items = ?, equipment_instances = ? WHERE user_id = ?",
                (json.dumps(pills, ensure_ascii=False), json.dumps(storage, ensure_ascii=False),
                 json.dumps(equipment_instances, ensure_ascii=False), sender_id),
            )
        else:
            await self.conn.execute(
                "UPDATE players SET pills_inventory = ?, storage_ring_items = ? WHERE user_id = ?",
                (json.dumps(pills, ensure_ascii=False), json.dumps(storage, ensure_ascii=False), sender_id),
            )

    async def cleanup_expired_gifts(self):
        """清理过期赠予请求：物品退回发送者，而不是直接删除。"""
        import time
        now = int(time.time())
        async with self.conn.execute(
            "SELECT id, sender_id, item_name, count, item_kind, item_quality, instance_id "
            "FROM pending_gifts WHERE expires_at < ?",
            (now,),
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        for gift in rows:
            await self._return_gift_to_sender(gift)
            await self.conn.execute("DELETE FROM pending_gifts WHERE id = ?", (gift["id"],))
        if rows:
            await self.conn.commit()

    async def return_gifts_for_receiver(self, receiver_id: str, commit: bool = True):
        """接收者被删除/弃道前，把待接收赠予退回发送者。"""
        async with self.conn.execute(
            "SELECT id, sender_id, item_name, count, item_kind, item_quality, instance_id "
            "FROM pending_gifts WHERE receiver_id = ?",
            (receiver_id,)
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        for gift in rows:
            await self._return_gift_to_sender(gift)
            await self.conn.execute("DELETE FROM pending_gifts WHERE id = ?", (gift["id"],))
        if rows and commit:
            await self.conn.commit()
    
    # ===== Phase 3: 银行贷款系统 CRUD =====
    
    async def get_active_loan(self, user_id: str) -> Optional[dict]:
        """获取用户当前活跃的贷款"""
        async with self.conn.execute(
            """SELECT id, user_id, principal, interest_rate, borrowed_at, due_at, status, loan_type,
                      paid_interest, paid_principal
               FROM bank_loans WHERE user_id = ? AND status IN ('active', 'overdue')""",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "user_id": row[1],
                    "principal": row[2],
                    "interest_rate": row[3],
                    "borrowed_at": row[4],
                    "due_at": row[5],
                    "status": row[6],
                    "loan_type": row[7],
                    "paid_interest": row[8] or 0,
                    "paid_principal": row[9] or 0
                }
            return None
    
    async def create_loan(self, user_id: str, principal: int, interest_rate: float, 
                          borrowed_at: int, due_at: int, loan_type: str = "normal") -> int:
        """创建贷款记录"""
        await self.conn.execute(
            """INSERT INTO bank_loans (user_id, principal, interest_rate, borrowed_at, due_at, status, loan_type)
               VALUES (?, ?, ?, ?, ?, 'active', ?)""",
            (user_id, principal, interest_rate, borrowed_at, due_at, loan_type)
        )
        await self.conn.commit()
        async with self.conn.execute("SELECT last_insert_rowid()") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    
    async def close_loan(self, loan_id: int):
        """关闭贷款（标记为已还清）"""
        await self.conn.execute(
            "UPDATE bank_loans SET status = 'closed' WHERE id = ?",
            (loan_id,)
        )
        await self.conn.commit()
    
    async def mark_loan_overdue(self, loan_id: int):
        """标记贷款逾期"""
        await self.conn.execute(
            "UPDATE bank_loans SET status = 'overdue' WHERE id = ?",
            (loan_id,)
        )
        await self.conn.commit()
    
    async def get_overdue_loans(self, current_time: int) -> List[dict]:
        """获取所有逾期贷款"""
        loans = []
        async with self.conn.execute(
            """SELECT id, user_id, principal, interest_rate, borrowed_at, due_at, loan_type
               FROM bank_loans WHERE status IN ('active', 'overdue') AND due_at < ?""",
            (current_time,)
        ) as cursor:
            async for row in cursor:
                loans.append({
                    "id": row[0],
                    "user_id": row[1],
                    "principal": row[2],
                    "interest_rate": row[3],
                    "borrowed_at": row[4],
                    "due_at": row[5],
                    "loan_type": row[6]
                })
        return loans
    
    # ===== Phase 3: 银行交易流水 CRUD =====
    
    async def add_bank_transaction(self, user_id: str, trans_type: str, amount: int, 
                                    balance_after: int, description: str, created_at: int):
        """添加银行交易流水"""
        await self.conn.execute(
            """INSERT INTO bank_transactions (user_id, trans_type, amount, balance_after, description, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, trans_type, amount, balance_after, description, created_at)
        )
        await self.conn.commit()
    
    async def get_bank_transactions(self, user_id: str, limit: int = 20) -> List[dict]:
        """获取用户银行交易流水"""
        transactions = []
        async with self.conn.execute(
            """SELECT id, trans_type, amount, balance_after, description, created_at
               FROM bank_transactions WHERE user_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit)
        ) as cursor:
            async for row in cursor:
                transactions.append({
                    "id": row[0],
                    "trans_type": row[1],
                    "amount": row[2],
                    "balance_after": row[3],
                    "description": row[4],
                    "created_at": row[5]
                })
        return transactions
    
    async def get_deposit_ranking(self, limit: int = 10) -> List[dict]:
        """获取存款排行榜"""
        rankings = []
        async with self.conn.execute(
            """SELECT user_id, balance FROM bank_accounts
               WHERE balance > 0
               ORDER BY balance DESC LIMIT ?""",
            (limit,)
        ) as cursor:
            async for row in cursor:
                rankings.append({
                    "user_id": row[0],
                    "balance": row[1]
                })
        return rankings

        # ===== v6.1 日常任务 CRUD =====
    
    async def get_user_active_tasks(self, user_id: str) -> List[dict]:
        """获取用户当前进行中的任务（顺带清理已过期任务，避免占用每日接取上限）"""
        import time as _time
        await self.conn.execute(
            "UPDATE daily_tasks SET status = 3 WHERE user_id = ? AND status = 1 AND expire_time < ?",
            (user_id, int(_time.time()))
        )
        await self.conn.commit()
        async with self.conn.execute(
            "SELECT * FROM daily_tasks WHERE user_id = ? AND status = 1 AND expire_time > ? ORDER BY task_level DESC",
            (user_id, int(_time.time()))
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def count_daily_tasks_today(self, user_id: str, task_level: str, expire_time: int) -> int:
        """统计今天该品阶已接取的任务数（包含已完成/已放弃，防止靠放弃无限刷新）。"""
        async with self.conn.execute(
            "SELECT COUNT(*) FROM daily_tasks "
            "WHERE user_id = ? AND task_level = ? AND expire_time = ?",
            (user_id, task_level, expire_time)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def create_daily_task(self, user_id: str, task_level: str, title: str, 
                                target_type: str, target_count: int, rewards_json: str, expire_time: int) -> int:
        """创建接取任务"""
        await self.conn.execute(
            """INSERT INTO daily_tasks (user_id, task_level, title, target_type, target_count, rewards_json, expire_time)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, task_level, title, target_type, target_count, rewards_json, expire_time)
        )
        await self.conn.commit()
        async with self.conn.execute("SELECT last_insert_rowid()") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def add_task_progress(self, user_id: str, target_type: str, count: int = 1):
        """增加任务进度（供其他 Manager 调用的接口）"""
        import time as _time
        now = int(_time.time())
        async with self.conn.execute(
            "SELECT * FROM daily_tasks WHERE user_id = ? AND target_type = ? AND status = 1 AND expire_time >= ?",
            (user_id, target_type, now)
        ) as cursor:
            tasks = await cursor.fetchall()
            
        for task in tasks:
            new_progress = min(task['target_count'], task['current_progress'] + count)
            if new_progress >= task['target_count']:
                # 任务完成
                await self.conn.execute(
                    "UPDATE daily_tasks SET current_progress = ?, status = 2 WHERE id = ?",
                    (new_progress, task['id'])
                )
            else:
                await self.conn.execute(
                    "UPDATE daily_tasks SET current_progress = ? WHERE id = ?",
                    (new_progress, task['id'])
                )
        await self.conn.commit()

    async def complete_daily_task(self, task_id: int, user_id: str):
        """标记任务为已领奖"""
        await self.conn.execute(
            "UPDATE daily_tasks SET status = 3 WHERE id = ? AND user_id = ? AND status = 2",
            (task_id, user_id)
        )
        await self.conn.commit()

    async def abandon_daily_task(self, task_id: int, user_id: str) -> bool:
        """放弃一个进行中的任务（status=4），返回是否真的更新成功。"""
        cursor = await self.conn.execute(
            "UPDATE daily_tasks SET status = 4 "
            "WHERE id = ? AND user_id = ? AND status = 1",
            (task_id, user_id)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_daily_task(self, task_id: int, user_id: str) -> Optional[dict]:
        """按编号获取用户自己的日常任务。"""
        async with self.conn.execute(
            "SELECT * FROM daily_tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None
