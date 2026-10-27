# managers/robbery_manager.py
import time
import random
from typing import Tuple, TYPE_CHECKING
from ..data import DataBase
from ..models import Player

if TYPE_CHECKING:
    from ..core import StorageRingManager

class RobberyManager:
    def __init__(self, db: DataBase, storage_ring_mgr: "StorageRingManager"):
        self.db = db
        self.storage_ring_mgr = storage_ring_mgr

    async def use_shield(self, player: Player) -> Tuple[bool, str]:
        """使用护府令"""
        now = int(time.time())
        if player.shield_expire_time > now:
            remain_h = (player.shield_expire_time - now) // 3600
            return False, f"❌ 你的护府大阵已经开启，剩余 {remain_h} 小时。"

        if not self.storage_ring_mgr.has_item(player, "护府令", 1):
            return False, "❌ 你的储物戒里没有【护府令】，请前往百宝阁购买。"

        success, _ = await self.storage_ring_mgr.retrieve_item(player, "护府令", 1)
        if not success:
            return False, "❌ 消耗护府令失败。"

        # 阵法师专精：护府大阵持续时间延长至36小时，产出惩罚降至10%
        duration = 86400
        duration_note = ""
        penalty_pct = 20
        if getattr(player, "life_skill", "") == "阵法师":
            duration = 86400 + 43200
            penalty_pct = 10
            duration_note = "\n⚒️ 阵法师专精生效：大阵持续36小时"

        player.shield_start_time = now
        player.shield_expire_time = now + duration
        await self.db.update_player(player)

        return True, (
            "🛡️ 护府大阵已开启！\n"
            "━━━━━━━━━━━━━━━\n"
            f"在接下来的 {duration // 3600} 小时内，你将免疫所有玩家的潜入偷窃。\n"
            f"⚠️ 代价：你的洞天福地与灵田产出将降低 {penalty_pct}%。"
            f"{duration_note}"
        )

    async def rob_player(self, attacker: Player, target_id: str) -> Tuple[bool, str, bool]:
        """发起掠夺"""
        now = int(time.time())

        if attacker.user_id == target_id:
            return False, "❌ 不能偷自己。", False

        target = await self.db.get_player_by_id(target_id)
        if not target:
            return False, "❌ 对方不存在或未踏入修仙之路。", False

        # 检查防守方护盾
        if target.shield_expire_time > now:
            remain_h = (target.shield_expire_time - now) // 3600
            return False, f"❌ 对方开启了护府大阵，你被阵法反噬弹飞了！（剩余 {remain_h} 小时）", False

        # 先确认目标有可偷资产，再消耗道具
        async with self.db.conn.execute("SELECT gold_per_hour, last_collect_time FROM blessed_lands WHERE user_id = ?", (target_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            return False, "❌ 对方穷得连洞天都没有，你白跑了一趟。", False

        gold_per_hour, last_collect = row
        hours_passed = (now - last_collect) / 3600
        pending_hours = max(0, min(24, int(hours_passed)))
        credit_start = max(last_collect, now - pending_hours * 3600)
        output_mult = target.shield_output_multiplier(credit_start, now)
        pending_gold = int(pending_hours * gold_per_hour * output_mult)

        if pending_gold < 100:
            return False, "❌ 对方洞府穷得叮当响，没有任何油水可捞。", False

        # 检查进攻方道具
        if not self.storage_ring_mgr.has_item(attacker, "迷神香", 1):
            return False, "❌ 潜入失败：你的储物戒里没有【迷神香】。", False

        await self.storage_ring_mgr.retrieve_item(attacker, "迷神香", 1)

        # 成功率判定：受双方境界差影响
        success_rate = 0.6
        if target.level_index > attacker.level_index:
            success_rate -= (target.level_index - attacker.level_index) * 0.05
        success_rate = max(0.1, min(0.9, success_rate))

        if random.random() < success_rate:
            # 偷窃成功
            rob_amount = int(pending_gold * 0.15) # 偷走未收取收益的 15%
            attacker.gold += rob_amount
            attacker.karma = (getattr(attacker, "karma", 0) or 0) - 1  # 行窃增加业力
            await self.db.update_player(attacker)

            # ===== 只扣掉被偷走的那部分可领取产出 =====
            # 洞天最多累计 24 小时：若目标已挂机超过 24 小时，超出部分本来就会溢出，
            # 必须从“可领取窗口”里扣除，否则偷窃相当于凭空印钞。
            effective_rate = gold_per_hour * output_mult
            stolen_hours = rob_amount / effective_rate if effective_rate > 0 else 0
            remaining_hours = max(0.0, pending_hours - stolen_hours)
            new_collect_time = max(
                int(last_collect + stolen_hours * 3600),
                int(now - remaining_hours * 3600),
            )
            await self.db.conn.execute(
                "UPDATE blessed_lands SET last_collect_time = ? WHERE user_id = ?",
                (new_collect_time, target_id)
            )
            await self.db.conn.commit()
            # ===================================

            return True, f"🦹 潜入成功！你从【{target.user_name or target_id[:6]}】的洞天里偷走了 {rob_amount:,} 灵石！", False
        else:
            # 偷窃失败（挂树上）
            return True, "⚠️ 潜入失败！", True
