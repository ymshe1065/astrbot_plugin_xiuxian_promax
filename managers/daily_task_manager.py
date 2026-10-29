# managers/daily_task_manager.py
import random
import time
import json
import hashlib
from typing import Tuple, List, Dict
from ..data import DataBase
from ..models import Player

class DailyTaskManager:
    """日常任务管理器"""
    
    LEVEL_LIMITS = {
        "黄": {"max": 5, "base_exp": 500, "base_gold": 200},
        "玄": {"max": 3, "base_exp": 2000, "base_gold": 1000},
        "地": {"max": 2, "base_exp": 8000, "base_gold": 4000},
        "天": {"max": 1, "base_exp": 30000, "base_gold": 15000}
    }

    TASK_POOL = {
        "黄": [
            {"title": "晨间吐纳", "type": "cultivate", "count": 1, "desc": "完成一次闭关并出关"},
            {"title": "仙门签到", "type": "check_in", "count": 1, "desc": "完成一次每日签到"},
            {"title": "灵田照料", "type": "harvest", "count": 1, "desc": "收获1次灵田作物"},
            {"title": "山门历练", "type": "adventure", "count": 1, "desc": "完成一次历练路线"}
        ],
        "玄": [
            {"title": "闭关苦修", "type": "cultivate", "count": 1, "desc": "完成一次闭关并出关"},
            {"title": "历练闯荡", "type": "adventure", "count": 1, "desc": "完成一次历练路线"},
            {"title": "秘境探索", "type": "explore_rift", "count": 1, "desc": "完成一次秘境探索"},
            {"title": "镖行千里", "type": "escort", "count": 1, "desc": "完成一次押镖"},
            {"title": "丹火初燃", "type": "craft_pill", "count": 1, "desc": "成功炼制一次丹药"}
        ],
        "地": [
            {"title": "切磋斗法", "type": "spar", "count": 1, "desc": "参与一次玩家切磋"},
            {"title": "炼丹大师", "type": "craft_pill", "count": 2, "desc": "成功炼制2次丹药"},
            {"title": "灵田丰收", "type": "harvest", "count": 3, "desc": "收获3次灵田作物"},
            {"title": "秘境深探", "type": "explore_rift", "count": 2, "desc": "完成2次秘境探索"},
            {"title": "宗门执役", "type": "sect_task", "count": 1, "desc": "完成一次宗门任务"},
            {"title": "护镖万里", "type": "escort", "count": 2, "desc": "完成2次押镖"}
        ],
        "天": [
            {"title": "弑神之战", "type": "boss_damage", "count": 2, "desc": "对世界Boss造成至少2%伤害"},
            {"title": "越级挑战", "type": "win_pvp", "count": 1, "desc": "在决斗中击败一名玩家"},
            {"title": "团本先锋", "type": "boss_damage", "count": 3, "desc": "参与3次世界Boss/团本讨伐"},
            {"title": "丹道宗师", "type": "craft_pill", "count": 3, "desc": "成功炼制3次丹药"}
        ]
    }

    def __init__(self, db: DataBase, config_manager=None):
        self.db = db
        self.config_manager = config_manager

    # ===== v6.17 每日随机任务池：每人当日可接任务不同 =====
    def _daily_seed(self, user_id: str) -> int:
        """每人每日独立种子（日期+用户ID）"""
        import hashlib
        day = time.strftime("%Y%m%d")
        return int(hashlib.md5(f"{day}_{user_id}".encode()).hexdigest()[:8], 16)

    def get_daily_pool(self, user_id: str, level: str, picks: int = 3):
        """该玩家今日该品阶可接的随机任务（每日固定，不会因查看变化）"""
        import random as _random
        pool = self.TASK_POOL.get(level, [])
        if not pool:
            return []
        level_seed = int(hashlib.md5(level.encode()).hexdigest()[:8], 16)
        rng = _random.Random(self._daily_seed(user_id) + level_seed)
        idx = rng.sample(range(len(pool)), min(picks, len(pool)))
        return [(i, pool[i]) for i in idx]

    async def show_board(self, player: Player) -> str:
        """任务榜 v2：今日可接任务（随机池）+ 已接任务进度"""
        active_tasks = await self.db.ext.get_user_active_tasks(player.user_id, include_completed=True)

        msg = ["📋 天道任务榜", "━━━━━━━━━━━━━━━"]

        # 已接任务（含已完成待领取，编号用于 /放弃任务）
        if active_tasks:
            msg.append("【进行中】")
            for t in active_tasks:
                status_str = "✅ 已完成待领取" if t["status"] == 2 else f"进度：{t['current_progress']}/{t['target_count']}"
                msg.append(f"#{t['id']} [{t['task_level']}阶] {t['title']} - {status_str}")
        else:
            msg.append("【进行中】暂无。")

        # 今日可接（每品阶随机 3 个）；已接取/已完成的标题不再重复展示
        done_titles = {t["title"] for t in active_tasks}
        msg.append("")
        msg.append("【今日可接】（每人每日随机，明早刷新）")
        for level in self.LEVEL_LIMITS:
            active_count = sum(1 for t in active_tasks if t["task_level"] == level)
            max_limit = self.LEVEL_LIMITS[level]["max"]
            if active_count >= max_limit:
                msg.append(f"  [{level}阶] 今日该品阶名额已满（{max_limit}）")
                continue
            pool = [(i, t) for i, t in self.get_daily_pool(player.user_id, level)
                    if t["title"] not in done_titles]
            if pool:
                names = " / ".join(t["title"] for _, t in pool)
                msg.append(f"  [{level}阶] {names}")
        msg.append("")
        msg.append("💡 /接取任务 <黄/玄/地/天> 从今日随机任务中抽一个接取")
        msg.append("💡 /领取任务奖励 结算已完成任务")
        return "\n".join(msg)

    def _scale_reward(self, player: Player, level: str) -> dict:
        """根据玩家境界动态缩放奖励，并附加防膨胀材料"""
        config = self.LEVEL_LIMITS[level]
        level_bonus = 1 + (player.level_index * 0.15) 
        
        exp = int(config["base_exp"] * level_bonus)
        gold = int(config["base_gold"] * level_bonus)
        
        materials = []
        if level == "黄": materials = [{"name": "灵草", "count": 3}]
        elif level == "玄": materials = [{"name": "玄铁", "count": 2}]
        elif level == "地": materials = [{"name": "洗练石", "count": 1}]
        elif level == "天": materials = [{"name": "混元铁", "count": 1}]

        return {"exp": exp, "gold": gold, "materials": materials}

    async def get_task_board(self, player: Player) -> str:
        """查看今日任务面板（v6.17 代理到新版任务榜）"""
        return await self.show_board(player)

    async def generate_task(self, player: Player, level: str) -> Tuple[bool, str]:
        """接取任务（v6.17：从今日随机池抽取，每人任务不同）"""
        if level not in self.LEVEL_LIMITS:
            return False, "❌ 无效的任务品阶，请选择 黄/玄/地/天。"

        # 计算过期时间（今天 23:59:59）
        now = int(time.time())
        today_end = int(time.mktime(time.localtime(now)[:3] + (23, 59, 59, 0, 0, 0)))

        # 今日接取上限：已完成/已放弃也计入，防止靠放弃无限刷新
        max_limit = self.LEVEL_LIMITS[level]["max"]
        current_level_count = await self.db.ext.count_daily_tasks_today(
            player.user_id, level, today_end
        )
        if current_level_count >= max_limit:
            return False, f"❌ 你今日的【{level}阶】任务接取次数已用尽（上限 {max_limit} 次）。"

        # 从今日随机池中抽取（去掉已在进行中的同名任务）
        daily_pool = self.get_daily_pool(player.user_id, level)
        active_tasks = await self.db.ext.get_user_active_tasks(player.user_id)
        doing_titles = {t["title"] for t in active_tasks}
        candidates = [(i, tpl) for i, tpl in daily_pool if tpl["title"] not in doing_titles]
        if not candidates:
            return False, f"❌ 今日【{level}阶】的随机任务你都已接取/在进行中，明日再来。"

        import random as _random
        idx, template = _random.choice(candidates)
        
        # 动态计算奖励
        rewards = self._scale_reward(player, level)

        # 宗门任务堂：日常任务修为/灵石 +3%/级
        try:
            if player.sect_id:
                sect = await self.db.ext.get_sect_by_id(player.sect_id)
                if sect:
                    levels = json.loads(getattr(sect, "buildings", "{}") or "{}")
                    task_hall_lv = int(levels.get("任务堂", 0))
                    if task_hall_lv > 0:
                        mult = 1 + 0.03 * min(5, task_hall_lv)
                        rewards["exp"] = int(rewards.get("exp", 0) * mult)
                        rewards["gold"] = int(rewards.get("gold", 0) * mult)
        except Exception:
            pass

        rewards_json = json.dumps(rewards, ensure_ascii=False)
        
        await self.db.ext.create_daily_task(
            user_id=player.user_id,
            task_level=level,
            title=template["title"],
            target_type=template["type"],
            target_count=template["count"],
            rewards_json=rewards_json,
            expire_time=today_end
        )
        
        return True, f"✅ 已接取【{level}阶】任务：{template['title']}\n目标：{template['desc']}"

    async def abandon_task(self, player: Player, task_id: int) -> Tuple[bool, str]:
        """放弃一个进行中的日常任务；已完成的任务必须先领奖。"""
        task = await self.db.ext.get_daily_task(task_id, player.user_id)
        if not task:
            return False, "❌ 没有找到这个任务，请用 /任务榜 查看编号。"

        if task["status"] == 2:
            return False, "❌ 该任务已完成，请先 /领取任务奖励，不能放弃。"
        if task["status"] != 1:
            return False, "❌ 该任务已结束或已放弃。"
        if task["expire_time"] <= int(time.time()):
            return False, "❌ 该任务已过期。"

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE daily_tasks SET status = 4 "
                "WHERE id = ? AND user_id = ? AND status = 1",
                (task_id, player.user_id),
            )
            if cursor.rowcount == 0:
                await self.db.conn.rollback()
                return False, "❌ 放弃失败，任务状态已变化，请重试。"

            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        return True, (
            f"🗑️ 已放弃【{task['task_level']}阶】任务：{task['title']}\n"
            "本次进度作废，不扣灵石/灵气，但会占用今日接取次数。"
        )

    async def claim_rewards(self, player: Player) -> Tuple[bool, str, list]:
        """结算所有已完成的任务（加入事务防并发卡双倍奖励）"""
        # 开启事务锁
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            now = int(time.time())
            # 事务内重新查询任务状态，确保是最新的；过期任务不允许结算
            async with self.db.conn.execute(
                "SELECT * FROM daily_tasks WHERE user_id = ? AND status = 2 AND expire_time >= ?",
                (player.user_id, now)
            ) as cursor:
                completed_tasks = [dict(r) for r in await cursor.fetchall()]
            
            if not completed_tasks:
                await self.db.conn.rollback()
                return False, "❌ 当前没有已完成待领取的任务。", []
                
            total_exp = 0
            total_gold = 0
            total_items = []
            
            for task in completed_tasks:
                # 更新任务状态，并校验是否真的更新成功（防止其他协程抢先修改）
                cursor_update = await self.db.conn.execute(
                    "UPDATE daily_tasks SET status = 3 WHERE id = ? AND status = 2 AND expire_time >= ?",
                    (task["id"], now)
                )
                if cursor_update.rowcount == 0:
                    # 如果受影响行数为0，说明在事务期间状态被改了（极端并发情况）
                    await self.db.conn.rollback()
                    return False, "❌ 领取失败，任务状态已变更，请重试。", []
                    
                rewards = json.loads(task["rewards_json"])
                total_exp += rewards.get("exp", 0)
                total_gold += rewards.get("gold", 0)
                total_items.extend(rewards.get("materials", []))
                
            # 事务内重新获取玩家数据，防止并发覆盖
            fresh_player = await self.db.get_player_by_id(player.user_id)
            if not fresh_player:
                await self.db.conn.rollback()
                return False, "❌ 玩家不存在。", []
                
            # 发放基础奖励
            try:
                from .title_manager import TitleManager
                total_exp = int(total_exp * (1 + TitleManager.get_exp_bonus(fresh_player)))
            except Exception:
                pass
            try:
                from .pet_manager import PetManager
                pet_mgr = PetManager(self.db)
                gold_bonus = await pet_mgr.get_life_bonus(fresh_player.user_id, "gold_bonus")
                if gold_bonus > 0:
                    total_gold = int(total_gold * (1 + gold_bonus))
            except Exception:
                pass
            try:
                from ..core.technique_bonus import TechniqueBonusManager
                total_gold = TechniqueBonusManager.apply_gold_bonus(
                    fresh_player, total_gold, self.config_manager
                )
            except Exception:
                pass
            fresh_player.experience += total_exp
            fresh_player.gold += total_gold
            await self.db.update_player(fresh_player, commit=False)
            
            await self.db.conn.commit()
            
            msg = [
                "🎁 任务奖励结算成功！",
                f"获得修为：+{total_exp:,}",
                f"获得灵石：+{total_gold:,}"
            ]
            if total_items:
                msg.append(f"获得材料：{total_items}")
                
            return True, "\n".join(msg), total_items
            
        except Exception as e:
            await self.db.conn.rollback()
            raise