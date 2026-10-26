# managers/world_boss_manager.py
import time
import random
from typing import Tuple, List, Dict
from astrbot.api import logger
from ..data import DataBase
from ..models import Player

class WorldBossManager:
    """全服世界Boss管理器 - 动态水位与防膨胀机制"""

    BOSS_NAMES = ["深渊魔龙", "远古天魔", "九幽冥王", "吞天巨兽", "血海老祖"]

    def __init__(self, db: DataBase, config_manager, storage_ring_mgr):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_mgr = storage_ring_mgr

    async def calculate_dynamic_hp(self) -> Tuple[int, int]:
        """动态水位评估：计算全服活跃玩家战力，返回 (目标血量, 参与人数基数)"""
        players = await self.db.get_all_players()
        if not players:
            return 500_000, 10

        active_powers = []
        for p in players:
            if p.experience > 1000:
                power = p.experience + p.physical_damage + p.magic_damage
                active_powers.append(power)

        if not active_powers:
            return 500_000, 10

        active_powers.sort()
        trim_start = int(len(active_powers) * 0.2)
        trim_end = int(len(active_powers) * 0.95)
        valid_powers = active_powers[trim_start:trim_end]
        if not valid_powers:
            valid_powers = active_powers

        avg_power = sum(valid_powers) / len(valid_powers)
        player_count = len(valid_powers)

        target_hp = int(avg_power * player_count * 3)
        target_hp = max(500_000, min(target_hp, 5_000_000_000))
        return target_hp, player_count

    async def spawn_boss(self) -> Tuple[bool, str]:
        """生成世界Boss"""
        async with self.db.conn.execute("SELECT * FROM world_boss WHERE status = 1") as cursor:
            if await cursor.fetchone():
                return False, "❌ 当前已有世界Boss存活！"

        max_hp, p_count = await self.calculate_dynamic_hp()
        boss_name = random.choice(self.BOSS_NAMES)
        now = int(time.time())
        expire_time = now + 86400 # 存活 24 小时

        await self.db.conn.execute(
            """INSERT INTO world_boss (boss_name, boss_level, max_hp, current_hp, atk, defense, spawn_time, expire_time)
               VALUES (?, 2, ?, ?, ?, 50, ?, ?)""",
            (boss_name, max_hp, max_hp, int(max_hp * 0.001), now, expire_time)
        )
        await self.db.conn.commit()

        return True, (
            f"🌪️ 【天道警示】世界Boss降临！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👹 名称：{boss_name}\n"
            f"💀 境界：灭世级\n"
            f"❤️ 血量：{max_hp:,} （基于全服 {p_count} 名活跃修士战力生成）\n"
            f"⏳ 存活时间：24小时\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 发送 /挑战世界Boss 参与讨伐！"
        )

    async def challenge_boss(self, player: Player) -> Tuple[bool, str]:
        """挑战世界Boss"""
        now = int(time.time())

        # 0. 境界门槛：境界3（炼气四层）以上才能参与，防小号白嫖全服奖励
        MIN_BOSS_LEVEL = 3
        if player.level_index < MIN_BOSS_LEVEL:
            return False, (
                f"❌ 境界不足，无法参与讨伐！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"挑战世界Boss需要达到境界等级 {MIN_BOSS_LEVEL}\n"
                f"你当前境界等级：{player.level_index}（还差 {MIN_BOSS_LEVEL - player.level_index} 级）\n"
                f"💡 通过 /闭关 /历练 提升修为后 /突破 提高境界"
            )

        # 1. 检查Boss状态
        async with self.db.conn.execute("SELECT * FROM world_boss WHERE status = 1") as cursor:
            row = await cursor.fetchone()
            if not row:
                return False, "❌ 当前没有存活的世界Boss。"
            boss = dict(row)

        if now > boss["expire_time"]:
            return False, "❌ Boss已逃遁，请等待下次降临。"

        # 2. 检查个人挑战次数与冷却
        async with self.db.conn.execute(
            "SELECT * FROM world_boss_damage WHERE boss_id = ? AND user_id = ?",
            (boss["boss_id"], player.user_id)
        ) as cursor:
            record = await cursor.fetchone()
            if record:
                rec = dict(record)
                # 每日挑战次数不设上限，只保留 5 分钟冷却
                if now - rec["last_attack_time"] < 300: # 5分钟CD
                    return False, f"❌ 调息中，请 {300 - (now - rec['last_attack_time'])} 秒后再来。"
            else:
                rec = {"total_damage": 0, "attack_count": 0, "id": 0}

        # 3. 核心修复：实时计算攻击力，防止面板未刷新导致的伤害异常
        from .combat_manager import CombatManager
        cm = CombatManager()
        realtime_atk = cm.calculate_atk(
            player.experience, player.atkpractice, 0, 
            player.physical_damage, player.magic_damage, player.cultivation_type
        )
        base_damage = int(realtime_atk * 100 + player.experience * 0.1)
        
        damage_percent = (base_damage / boss["max_hp"]) * 100

        # 强制限制在 0.5% ~ 20% 之间
        damage_percent = max(0.5, min(20.0, damage_percent))
        final_damage = int(boss["max_hp"] * (damage_percent / 100))
        
        if final_damage > boss["current_hp"]:
            final_damage = boss["current_hp"]

        # 4. 开启事务进行并发扣血与记录
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            new_hp = boss["current_hp"] - final_damage
            is_kill = False
            if new_hp <= 0:
                new_hp = 0
                is_kill = True
                await self.db.conn.execute("UPDATE world_boss SET current_hp = ?, status = 2 WHERE boss_id = ?", (new_hp, boss["boss_id"]))
            else:
                await self.db.conn.execute("UPDATE world_boss SET current_hp = ? WHERE boss_id = ?", (new_hp, boss["boss_id"]))

            # 记录伤害
            if record:
                await self.db.conn.execute(
                    "UPDATE world_boss_damage SET total_damage = total_damage + ?, attack_count = attack_count + 1, last_attack_time = ? WHERE id = ?",
                    (final_damage, now, rec.get("id", 0))
                )
            else:
                await self.db.conn.execute(
                    "INSERT INTO world_boss_damage (boss_id, user_id, total_damage, attack_count, last_attack_time) VALUES (?, ?, ?, 1, ?)",
                    (boss["boss_id"], player.user_id, final_damage, now)
                )
            
            await self.db.conn.commit()
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"世界Boss战斗异常: {e}")
            return False, "❌ 战斗异常，请稍后再试。"

        self.last_challenge_ok = True  # 真正打出伤害，供任务进度判定

        # 5. 构造战报
        msg = [
            f"⚔️ 你对【{boss['boss_name']}】造成了 {final_damage:,} 点伤害！",
            f"━━━━━━━━━━━━━━━",
            f"📊 伤害占比：{damage_percent:.2f}%",
            f"❤️ Boss剩余血量：{new_hp:,} / {boss['max_hp']:,}",
        ]

        # 6. 击杀结算
        if is_kill:
            settle_msg = await self.settle_boss_kill(boss["boss_id"], player.user_id)
            msg.append("\n" + settle_msg)
        else:
            msg.append("\n💡 使用 /世界Boss状态 查看当前状态")

        return True, "\n".join(msg)

    async def settle_boss_kill(self, boss_id: int, killer_id: str) -> str:
        """结算Boss击杀奖励（真实发奖 + 尾刀称号）"""
        async with self.db.conn.execute(
            "SELECT * FROM world_boss_damage WHERE boss_id = ? ORDER BY total_damage DESC",
            (boss_id,)
        ) as cursor:
            damages = [dict(r) for r in await cursor.fetchall()]

        msg = [
            f"🎉 世界Boss已被成功击杀！",
            f"━━━━━━━━━━━━━━━",
        ]
        
        for i, dmg in enumerate(damages):
            rank = i + 1
            target_player = await self.db.get_player_by_id(dmg["user_id"])
            if not target_player: continue

            # 默认显示名字
            display_name = target_player.user_name or dmg['user_id']

            if dmg["user_id"] == killer_id:
                # 发尾刀奖励
                await self.storage_ring_mgr.store_item(target_player, "洗练石", 2, silent=True)
                await self.db.add_player_gold(target_player.user_id, 50000)
                target_player.gold += 50000

                # ===== 核心修复：实装尾刀称号 =====
                if not target_player.title or target_player.title != "灭世魔神":
                    await self.db.update_player_fields(target_player.user_id, title="灭世魔神")
                    target_player.title = "灭世魔神"
                    msg.append(f"👑 恭喜【{display_name}】斩杀世界Boss，获得唯一称号：【灭世魔神】！")
                # 更新显示名（加上称号前缀）
                display_name = f"【灭世魔神】{display_name}"
                # ==================================

                msg.append(f"🏆 {rank}. {display_name} - 尾刀大奖(洗练石x2, 5万灵石)")

            elif rank <= 3:
                await self.storage_ring_mgr.store_item(target_player, "洗练石", 1, silent=True)
                await self.db.add_player_gold(target_player.user_id, 20000)
                target_player.gold += 20000
                # 显示带有称号的名字
                if target_player.title:
                    display_name = f"【{target_player.title}】{display_name}"
                msg.append(f"🥇 {rank}. {display_name} - 主力输出(洗练石x1, 2万灵石)")

            else:
                await self.db.add_player_gold(target_player.user_id, 5000)
                target_player.gold += 5000
                # 显示带有称号的名字
                if target_player.title:
                    display_name = f"【{target_player.title}】{display_name}"
                msg.append(f"🥉 {rank}. {display_name} - 参与奖(5千灵石)")

        msg.append("━━━━━━━━━━━━━━━")
        msg.append("💡 奖励已发放至各位修士的储物戒/账户！")

        # 缓存结算文本，由指令层负责全服广播
        self.last_settle_msg = "\n".join(msg)

        return "\n".join(msg)