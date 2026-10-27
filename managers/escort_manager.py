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
    """押镖管理器：押镖期间全服可劫镖，劫镖触发天道压制PVP（v6.27 平衡版）
    平衡要点：天道压制拉平修为面板；护镖方气血+10%、平局判守方胜；
    高出对方2个大境界禁止劫镖；分成/赔偿随境界差动态调整；
    同一镖主每日最多被劫成功一次；劫镖10分钟冷却；送达获功德+1。"""

    ESCORT_DURATION = 1800          # 押镖时长 30 分钟
    MIN_LEVEL = 3                   # 押镖/劫镖最低境界
    ROB_ITEM = "迷神香"             # 劫镖消耗道具
    ROB_WIN_CUT = 0.3               # 劫镖基础分成 30%
    ROB_COOLDOWN = 600              # 劫镖冷却（秒）
    ROB_REALM_PROTECT_GAP = 2       # 高出对方 2 个大境界时禁止劫镖
    ROB_CUT_STEP = 0.03             # 每级境界差调整 3% 分成
    ROB_CUT_MIN = 0.1               # 劫掠分成下限
    ROB_CUT_MAX = 0.5               # 劫掠分成上限
    ROB_COMP_RATE = 0.2             # 失败赔偿至少为镖银的 20%
    ROB_DEFENDER_HP_BONUS = 0.1     # 护镖方气血加成

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

    @staticmethod
    def _major_realm_name(level_name: str) -> str:
        """从境界名提取大境界：炼气期三层 -> 炼气，铜皮境初期 -> 铜皮。"""
        name = (level_name or "").strip()
        if not name:
            return ""
        for sep in ("境", "期", "体"):
            if sep in name:
                return name.split(sep)[0]
        return name

    def _major_realm_index(self, player: Player) -> int:
        """计算大境界序号；兼容炼气十层与体修境界名。"""
        try:
            level_data = self.config_manager.get_level_data(player.cultivation_type) if self.config_manager else []
            if not level_data or not (0 <= player.level_index < len(level_data)):
                return player.level_index
            realms = []
            for entry in level_data:
                base = self._major_realm_name(entry.get("level_name", ""))
                if not realms or realms[-1] != base:
                    realms.append(base)
            base = self._major_realm_name(level_data[player.level_index].get("level_name", ""))
            return realms.index(base) if base in realms else player.level_index
        except Exception:
            return player.level_index

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

        # 称号「灵石%」加成真正生效（镖行新秀/金牌镖师/天下第一镖等）
        try:
            from .title_manager import TitleManager
            gold_pct = float(TitleManager(None).get_active_bonus(player).get("gold_pct", 0) or 0)
            if gold_pct:
                escort_gold = int(escort_gold * (1 + gold_pct))
        except Exception:
            pass

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
        """劫镖：天道压制 PVP，每日每名镖主最多被劫一次。"""
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

        # 每日一劫：同一镖主每天最多被劫成功一次
        today = time.strftime("%Y-%m-%d")
        daily_key = f"escort_robbed_{target_id}"
        daily_record = await self.db.ext.get_system_config(daily_key)
        if daily_record:
            parts = daily_record.split("|", 2)
            if parts and parts[0] == today:
                robber_name = parts[2] if len(parts) > 2 and parts[2] else "他人"
                return False, (
                    f"❌ 【{target.user_name or target_id[:8]}】今日已被【{robber_name}】劫过一次，\n"
                    "天道规矩：每人每天最多被劫镖一次。"
                )

        attacker_realm = self._major_realm_index(attacker)
        target_realm = self._major_realm_index(target)
        realm_gap = attacker_realm - target_realm
        if realm_gap >= self.ROB_REALM_PROTECT_GAP:
            return False, (
                f"❌ 对方境界远低于你（相差 {realm_gap} 个大境界），\n"
                "天道不容以大欺小，你无法对其劫镖。"
            )

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

        # 用天道压制引擎快速构建双方战斗属性（接入传承加成）
        async def build_stats(p: Player) -> CombatStats:
            impart = None
            try:
                impart = await self.db.ext.get_impart_info(p.user_id)
            except Exception:
                impart = None
            return self.combat_mgr.build_player_combat_stats(
                p,
                self.config_manager,
                db=self.db,
                hp_buff=impart.impart_hp_per if impart else 0.0,
                mp_buff=impart.impart_mp_per if impart else 0.0,
                atk_buff=impart.impart_atk_per if impart else 0.0,
                crit_rate=int((impart.impart_know_per if impart else 0.0) * 100),
                crit_damage_bonus=impart.impart_burst_per if impart else 0.0,
            )

        attacker_stats = await build_stats(attacker)
        target_stats = await build_stats(target)

        from .pet_manager import PetManager
        pet_mgr = PetManager(self.db)
        for p, stats_item in ((attacker, attacker_stats), (target, target_stats)):
            main_pet, support_pets, pet_effects, atk = await pet_mgr.build_pet_lineup(
                p.user_id, stats_item.max_hp, stats_item.atk,
                stats_item.defense, p.cultivation_type,
            )
            self.combat_mgr.apply_pet_lineup(
                stats_item, main_pet, support_pets, pet_effects, atk
            )

        # 天道压制：拉平修为面板，护镖方获得气血加成，平局判护镖方守住
        result = self.combat_mgr.player_vs_player_balanced(
            attacker_stats,
            target_stats,
            player2_hp_bonus=self.ROB_DEFENDER_HP_BONUS,
            draw_to_player2=True,
        )

        for stats_item in (attacker_stats, target_stats):
            pets = []
            if stats_item.pet:
                pets.append(stats_item.pet)
            pets.extend(stats_item.support_pets or [])
            for pet in pets:
                await pet_mgr.save_pet_combat_state(pet)
        # 劫镖使用天道压制面板，气血/灵气会被临时拉平；
        # 这里不能把平衡面板的残余值写回玩家真实状态。
        extra = target_cd.get_extra_data() if hasattr(target_cd, "get_extra_data") else {}
        escort_gold = int(extra.get("escort_gold", 2000))

        log = "\n".join(result["combat_log"][-8:])  # 只展示最后8行战报，防刷屏

        level_diff = attacker.level_index - target.level_index
        if level_diff > 0:
            cut = max(
                self.ROB_CUT_MIN,
                self.ROB_WIN_CUT - self.ROB_CUT_STEP * level_diff,
            )
        else:
            cut = min(
                self.ROB_CUT_MAX,
                self.ROB_WIN_CUT + self.ROB_CUT_STEP * (-level_diff),
            )
        cut_pct = int(round(cut * 100))

        if result["winner"] == attacker.user_id:
            # 事务内重新校验目标仍处于押镖状态，避免目标恰好完成/放弃时
            # 用旧的 UserCd 整行回写把已完成状态“复活”。
            robbed = 0
            await self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                async with self.db.conn.execute(
                    "SELECT type, extra_data FROM user_cd WHERE user_id = ?", (target_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                if not row or int(row["type"] or 0) != int(UserStatus.DELIVERING):
                    await self.db.conn.rollback()
                    await self.db.ext.set_system_config(cd_key, "0")
                    await self.storage_ring_mgr.store_item(attacker, self.ROB_ITEM, 1, silent=True)
                    return True, (
                        f"{log}\n\n"
                        "⚠️ 目标已结束押镖，本次劫镖取消。\n"
                        f"【{self.ROB_ITEM}】已退回，劫镖冷却已清除。"
                    )
                try:
                    fresh_extra = json.loads(row["extra_data"] or "{}")
                except Exception:
                    fresh_extra = {}
                fresh_gold = int(fresh_extra.get("escort_gold", escort_gold))
                robbed = max(1, int(fresh_gold * cut))
                fresh_extra["escort_gold"] = max(0, fresh_gold - robbed)
                cursor = await self.db.conn.execute(
                    "UPDATE user_cd SET extra_data = ? WHERE user_id = ? AND type = ?",
                    (json.dumps(fresh_extra, ensure_ascii=False), target_id, UserStatus.DELIVERING),
                )
                if cursor.rowcount == 0:
                    await self.db.conn.rollback()
                    await self.db.ext.set_system_config(cd_key, "0")
                    await self.storage_ring_mgr.store_item(attacker, self.ROB_ITEM, 1, silent=True)
                    return True, (
                        f"{log}\n\n"
                        "⚠️ 目标押镖状态已变化，本次劫镖取消。\n"
                        f"【{self.ROB_ITEM}】已退回，劫镖冷却已清除。"
                    )
                await self.db.add_player_gold(attacker.user_id, robbed, commit=False)
                await self.db.conn.commit()
            except Exception:
                await self.db.conn.rollback()
                raise

            attacker.gold += robbed
            attacker.karma = (getattr(attacker, "karma", 0) or 0) - 1  # 劫镖为恶行，增加业力
            await self.db.update_player_fields(attacker.user_id, karma=attacker.karma)
            await self.db.ext.set_system_config(
                daily_key,
                f"{today}|{attacker.user_id}|{attacker.user_name or attacker.user_id[:8]}",
            )

            return True, (
                f"{log}\n\n"
                f"⚔️ 劫镖成功！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"你从【{target.user_name or target_id[:8]}】的镖车上抢走 {robbed:,} 灵石！\n"
                f"📜 本次分成：{cut_pct}%（境界差 {level_diff:+d}）\n"
                f"🌑 劫镖为恶行，业力 +1；劫镖调息 10 分钟\n"
                f"🛡️ 该镖主今日已被劫，今日内不会再被其他人劫镖"
            )
        else:
            # 劫镖失败：赔偿押镖人，境界差越大惩罚越重
            base_comp = max(
                2000 + attacker.level_index * 200,
                int(escort_gold * self.ROB_COMP_RATE),
            )
            if level_diff > 0:
                compensation = int(base_comp * (1 + 0.25 * level_diff))
            elif level_diff < 0:
                compensation = int(base_comp * max(0.5, 1 + 0.08 * level_diff))
            else:
                compensation = base_comp

            paid = min(compensation, max(0, attacker.gold))
            await self.db.add_player_gold(attacker.user_id, -paid)
            attacker.gold = max(0, attacker.gold - paid)
            await self.db.add_player_gold(target.user_id, paid)
            target.gold += paid

            reason = "双方势均力敌，护镖人守住了镖车" if result.get("draw") else "你被护镖人击退"
            return True, (
                f"{log}\n\n"
                f"💀 劫镖失败！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{reason}，赔偿 {paid:,} 灵石给对方。\n"
                f"📜 境界差 {level_diff:+d}；劫镖调息 10 分钟"
            )
