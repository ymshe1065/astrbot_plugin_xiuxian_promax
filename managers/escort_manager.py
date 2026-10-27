# managers/escort_manager.py
"""押镖与劫道系统 - 高风险博弈玩法"""
import time
import json
import random
from typing import Tuple, Dict

from ..data.data_manager import DataBase
from ..models import Player
from ..models_extended import UserStatus
from .combat_manager import CombatManager, CombatStats
from .task_penalty import calc_abandon_penalty, charge_abandon_penalty

__all__ = ["EscortManager"]


class EscortManager:
    """押镖管理器：押镖期间全服可劫镖，劫镖触发强制PVP（v6.12 平衡版）
    平衡要点：押镖收益上调；劫镖为公平满血PVP（无先手惩罚）；
    劫镖有10分钟冷却；劫镖失败赔偿随境界提升；送达获功德+1。"""

    ESCORT_DURATION = 1800          # 押镖时长 30 分钟
    MIN_LEVEL = 3                   # 押镖/劫镖最低境界
    ROB_ITEM = "迷神香"             # 劫镖消耗道具
    ROB_WIN_CUT = 0.3               # 劫镖成功可抢走镖银的 30%
    ROB_COOLDOWN = 600              # 劫镖冷却（秒）

    def __init__(self, db: DataBase, combat_mgr: CombatManager, storage_ring_mgr, config_manager=None):
        self.db = db
        self.combat_mgr = combat_mgr
        self.storage_ring_mgr = storage_ring_mgr
        self.config_manager = config_manager
        self._social = None

    async def _companion_bonus(self, user_id: str, perk_type: str) -> float:
        """仙灵道侣加成（惰性加载 SocialManager）"""
        try:
            if self._social is None:
                from .social_manager import SocialManager
                self._social = SocialManager(self.db)
            return await self._social.companion_bonus_async(user_id, perk_type)
        except Exception:
            return 0.0

    async def start_escort(self, player: Player) -> Tuple[bool, str, Dict]:
        """接镖：进入押镖状态，期间奖励随时间增长，但可被劫"""
        user_id = player.user_id

        if player.level_index < self.MIN_LEVEL:
            return False, f"❌ 押镖需要达到境界等级 {self.MIN_LEVEL}（练气后期）。", {}

        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd:
            await self.db.ext.create_user_cd(user_id)
            user_cd = await self.db.ext.get_user_cd(user_id)
        if user_cd.type != UserStatus.IDLE:
            return False, f"❌ 你当前正{UserStatus.get_name(user_cd.type)}，无法押镖！", {}

        # 镖银与修为按境界浮动（v6.12 上调收益，匹配被劫风险）；绯烟（押镖仙灵）加镖银
        base = 4000 + player.level_index * 1500
        escort_bonus = await self._companion_bonus(user_id, "escort")
        if escort_bonus > 0:
            base = int(base * (1 + escort_bonus))
        escort_gold = base + random.randint(-500, 1500)
        escort_exp = base // 2 + random.randint(0, 600)

        now = int(time.time())
        extra = {
            "escort_gold": escort_gold,
            "escort_exp": escort_exp,
        }
        await self.db.ext.set_user_busy(user_id, UserStatus.DELIVERING, now + self.ESCORT_DURATION, extra_data=extra)

        msg = (
            f"🛡️ 你接下了一趟灵石镖！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"镖银：{escort_gold:,} 灵石\n"
            f"酬劳：{escort_exp:,} 修为\n"
            f"押送时长：{self.ESCORT_DURATION // 60} 分钟\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⚠️ 押镖期间全服玩家可消耗【迷神香】劫镖！\n"
            f"被劫失败将损失部分镖银\n"
            f"💡 时间到后发送 /完成押镖 结算"
        )
        return True, msg, {"gold": escort_gold, "exp": escort_exp}

    async def finish_escort(self, player: Player) -> Tuple[bool, str]:
        """完成押镖：到达时长领全款"""
        user_id = player.user_id
        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.DELIVERING:
            return False, "❌ 你当前没有在押镖！"

        now = int(time.time())
        if user_cd.scheduled_time and now < user_cd.scheduled_time:
            remaining = user_cd.scheduled_time - now
            return False, f"❌ 镖车尚未到达！还需 {remaining // 60} 分 {remaining % 60} 秒。"

        extra = user_cd.get_extra_data() if hasattr(user_cd, "get_extra_data") else {}
        escort_gold = int(extra.get("escort_gold", 2000))
        escort_exp = int(extra.get("escort_exp", 1000))

        player.gold += escort_gold
        player.experience += escort_exp
        player.karma = (getattr(player, "karma", 0) or 0) + 1  # 护镖有功，功德+1
        await self.db.update_player(player)
        await self.db.ext.set_user_free(user_id)

        return True, (
            f"🎉 押镖成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"你平安将镖车护送到了目的地！\n"
            f"获得镖银：+{escort_gold:,} 灵石\n"
            f"获得酬劳：+{escort_exp:,} 修为\n"
            f"✨ 护镖有功，功德 +1（当前 {player.karma}）"
        )

    async def quit_escort(self, player: Player) -> Tuple[bool, str]:
        """放弃押镖：支付镖银 80% 作为违约金，本次无收益。"""
        user_cd = await self.db.ext.get_user_cd(player.user_id)
        if not user_cd or user_cd.type != UserStatus.DELIVERING:
            return False, "❌ 你当前没有在押镖！"

        try:
            extra = user_cd.get_extra_data() if hasattr(user_cd, "get_extra_data") else {}
        except Exception:
            extra = {}
        escort_gold = int((extra or {}).get("escort_gold", 0) or 0)
        cost, currency = calc_abandon_penalty(escort_gold)
        currency_label = "灵气" if currency == "qi" else "灵石"

        fresh = await self.db.get_player_by_id(player.user_id)
        if not fresh:
            return False, "❌ 玩家数据不存在。"
        ok, msg = await charge_abandon_penalty(fresh, self.db, cost, currency)
        if not ok:
            return False, msg

        await self.db.ext.set_user_free(player.user_id)
        return True, (
            "🏃 你扔下镖车跑路了...\n"
            f"向镖局支付违约金：{cost:,} {currency_label}\n"
            "本次押镖无任何收益。"
        )

    async def rob_escort(self, attacker: Player, target_id: str) -> Tuple[bool, str]:
        """劫镖：消耗迷神香，与押镖人强制PVP，胜者抢走部分镖银"""
        if attacker.user_id == target_id:
            return False, "❌ 不能劫自己的镖。"

        if attacker.level_index < self.MIN_LEVEL:
            return False, f"❌ 劫镖需要达到境界等级 {self.MIN_LEVEL}。"

        target = await self.db.get_player_by_id(target_id)
        if not target:
            return False, "❌ 对方还未踏入修仙之路。"

        target_cd = await self.db.ext.get_user_cd(target_id)
        if not target_cd or target_cd.type != UserStatus.DELIVERING:
            return False, "❌ 对方当前没有在押镖！"

        if not self.storage_ring_mgr.has_item(attacker, self.ROB_ITEM, 1):
            return False, f"❌ 劫镖需要消耗【{self.ROB_ITEM}】x1，你的储物戒里没有。"

        # 劫镖冷却（10分钟）
        cd_key = f"escort_rob_cd_{attacker.user_id}"
        cd_value = await self.db.ext.get_system_config(cd_key)
        now_ts = int(time.time())
        if cd_value and cd_value.isdigit() and now_ts < int(cd_value):
            remain = int(cd_value) - now_ts
            return False, f"❌ 劫镖调息中，请 {remain // 60} 分 {remain % 60} 秒后再来。"

        success, _ = await self.storage_ring_mgr.retrieve_item(attacker, self.ROB_ITEM, 1)
        if not success:
            return False, "❌ 消耗迷神香失败。"
        await self.db.ext.set_system_config(cd_key, str(now_ts + self.ROB_COOLDOWN))

        # 用天道压制引擎快速构建双方战斗属性
        def build_stats(p: Player) -> CombatStats:
            return self.combat_mgr.build_player_combat_stats(
                p, self.config_manager, db=self.db
            )

        attacker_stats = build_stats(attacker)
        target_stats = build_stats(target)

        from .pet_manager import PetManager
        pet_mgr = PetManager(self.db)
        for p, stats_item in ((attacker, attacker_stats), (target, target_stats)):
            main_pet, support_pets, pet_effects, atk = await pet_mgr.build_pet_lineup(
                p.user_id, stats_item.max_hp, stats_item.atk,
                stats_item.defense, p.cultivation_type,
            )
            stats_item.atk = atk
            stats_item.pet = main_pet
            stats_item.support_pets = support_pets
            stats_item.effects = pet_effects

        # v6.12 平衡：公平满血对决，劫镖方不再享有先手压制
        result = self.combat_mgr.player_vs_player(attacker_stats, target_stats, combat_type=2)

        for stats_item in (attacker_stats, target_stats):
            pets = []
            if stats_item.pet:
                pets.append(stats_item.pet)
            pets.extend(stats_item.support_pets or [])
            for pet in pets:
                await pet_mgr.save_pet_combat_state(pet)

        extra = target_cd.get_extra_data() if hasattr(target_cd, "get_extra_data") else {}
        escort_gold = int(extra.get("escort_gold", 2000))

        log = "\n".join(result["combat_log"][-8:])  # 只展示最后8行战报，防刷屏

        if result["winner"] == attacker.user_id:
            robbed = int(escort_gold * self.ROB_WIN_CUT)
            extra["escort_gold"] = escort_gold - robbed
            target_cd.extra_data = json.dumps(extra, ensure_ascii=False)
            await self.db.ext.update_user_cd(target_cd)

            attacker.gold += robbed
            attacker.karma = (getattr(attacker, "karma", 0) or 0) - 1  # 劫镖为恶行，增加业力
            await self.db.update_player(attacker)

            return True, (
                f"{log}\n\n"
                f"⚔️ 劫镖成功！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"你从【{target.user_name or target_id[:8]}】的镖车上抢走 {robbed:,} 灵石！\n"
                f"🌑 劫镖为恶行，业力 +1；劫镖调息 10 分钟"
            )
        else:
            # 劫镖失败：赔偿押镖人（随境界提升）
            compensation = 2000 + attacker.level_index * 200
            await self.db.add_player_gold(attacker.user_id, -compensation)
            attacker.gold = max(0, attacker.gold - compensation)
            await self.db.add_player_gold(target.user_id, compensation)
            target.gold += compensation

            return True, (
                f"{log}\n\n"
                f"💀 劫镖失败！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"你被护镖人反杀，赔偿 {compensation:,} 灵石给对方。\n"
                f"❗ 劫镖调息 10 分钟"
            )
