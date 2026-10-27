# managers/impart_pk_manager.py
"""传承PK系统管理器"""
import random
from typing import Tuple
from ..data import DataBase
from ..models import Player
from .combat_manager import CombatManager
from .combat_manager import CombatStats

__all__ = ["ImpartPkManager"]


class ImpartPkManager:
    """传承PK管理器 - 玩家间争夺传承的战斗"""
    
    def __init__(self, db: DataBase, combat_mgr: CombatManager, config_manager=None):
        self.db = db
        self.combat_mgr = combat_mgr
        self.config_manager = config_manager
    
    async def challenge_impart(self, attacker: Player, defender: Player) -> Tuple[bool, str, dict]:
        """发起传承挑战
        
        Args:
            attacker: 挑战者
            defender: 被挑战者
            
        Returns:
            (attacker_wins, battle_log, rewards)
        """
        # 获取双方传承等级（兼容旧库/新角色：缺失时幂等初始化）
        await self.db.ext.ensure_impart_info(attacker.user_id)
        await self.db.ext.ensure_impart_info(defender.user_id)
        attacker_impart = await self.db.ext.get_impart_info(attacker.user_id)
        defender_impart = await self.db.ext.get_impart_info(defender.user_id)
        
        # 准备战斗属性（接入双方传承加成，和决斗/切磋保持一致）
        atk_stats = self.combat_mgr.build_player_combat_stats(
            attacker,
            self.config_manager,
            db=self.db,
            hp_buff=attacker_impart.impart_hp_per if attacker_impart else 0.0,
            mp_buff=attacker_impart.impart_mp_per if attacker_impart else 0.0,
            atk_buff=attacker_impart.impart_atk_per if attacker_impart else 0.0,
            crit_rate=int((attacker_impart.impart_know_per if attacker_impart else 0.0) * 100),
            crit_damage_bonus=attacker_impart.impart_burst_per if attacker_impart else 0.0,
        )
        def_stats = self.combat_mgr.build_player_combat_stats(
            defender,
            self.config_manager,
            db=self.db,
            hp_buff=defender_impart.impart_hp_per if defender_impart else 0.0,
            mp_buff=defender_impart.impart_mp_per if defender_impart else 0.0,
            atk_buff=defender_impart.impart_atk_per if defender_impart else 0.0,
            crit_rate=int((defender_impart.impart_know_per if defender_impart else 0.0) * 100),
            crit_damage_bonus=defender_impart.impart_burst_per if defender_impart else 0.0,
        )
        if not atk_stats.name:
            atk_stats.name = "挑战者"
        if not def_stats.name:
            def_stats.name = "防守者"

        # 战斗模拟
        rounds = 0
        max_rounds = 20
        battle_log = []

        while atk_stats.hp > 0 and def_stats.hp > 0 and rounds < max_rounds:
            rounds += 1
            battle_log.append(f"-- 第 {rounds} 回合 --")

            damage, logs, heal = self.combat_mgr._execute_attack(atk_stats, def_stats)
            def_stats.hp -= damage
            atk_stats.hp = min(atk_stats.max_hp, atk_stats.hp + heal)
            battle_log.extend(logs)

            if def_stats.hp <= 0:
                break

            damage, logs, heal = self.combat_mgr._execute_attack(def_stats, atk_stats)
            atk_stats.hp -= damage
            def_stats.hp = min(def_stats.max_hp, def_stats.hp + heal)
            battle_log.extend(logs)

        # 判定胜负
        attacker_wins = def_stats.hp <= 0 or (atk_stats.hp > 0 and atk_stats.hp >= def_stats.hp)
        
        rewards = {}
        if attacker_wins:
            # 胜利奖励：获得传承加成
            impart_gain = random.uniform(0.01, 0.05)  # 1%-5%
            if attacker_impart:
                new_atk_per = min(1.0, attacker_impart.impart_atk_per + impart_gain)
                attacker_impart.impart_atk_per = new_atk_per
                await self.db.ext.update_impart_info(attacker_impart)
                rewards["impart_atk_gain"] = impart_gain
            
            # 失败惩罚
            if defender_impart and defender_impart.impart_atk_per > 0:
                loss = min(impart_gain / 2, defender_impart.impart_atk_per)
                defender_impart.impart_atk_per -= loss
                await self.db.ext.update_impart_info(defender_impart)
                rewards["defender_loss"] = loss
        else:
            # 失败惩罚：损失修为
            exp_loss = int(attacker.experience * 0.01)  # 1%
            attacker.experience = max(0, attacker.experience - exp_loss)
            await self.db.update_player(attacker)
            rewards["exp_loss"] = exp_loss
        
        return attacker_wins, "\n".join(battle_log[-6:]), rewards  # 只返回最后6条log
    
    async def get_impart_ranking(self, limit: int = 10) -> list:
        """获取传承排行榜"""
        # 查询所有传承数据，按攻击加成排序
        async with self.db.conn.execute(
            """
            SELECT user_id, impart_hp_per, impart_mp_per, impart_atk_per, 
                   impart_know_per, impart_burst_per
            FROM impart_info 
            ORDER BY impart_atk_per DESC 
            LIMIT ?
            """,
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                user_id = row[0]
                player = await self.db.get_player_by_id(user_id)
                if player:
                    total_per = row[1] + row[2] + row[3] + row[4] + row[5]
                    results.append({
                        "user_id": user_id,
                        "user_name": player.user_name or user_id[:8],
                        "atk_per": row[3],
                        "total_per": total_per
                    })
            return results
