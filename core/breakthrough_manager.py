# managers/breakthrough_manager.py

import random
from typing import Optional, Tuple
from astrbot.api import logger

from ..models import Player
from ..data import DataBase
from ..config_manager import ConfigManager


class BreakthroughManager:
    """突破管理器 - 处理境界突破相关逻辑"""

    def __init__(self, db: DataBase, config_manager: ConfigManager, config: dict):
        self.db = db
        self.config_manager = config_manager
        self.config = config

    def check_breakthrough_requirements(self, player: Player) -> Tuple[bool, str]:
        """检查玩家是否满足突破条件"""
        level_data = self.config_manager.get_level_data(player.cultivation_type)

        if player.level_index >= len(level_data) - 1:
            return False, "你已经达到了最高境界，无法继续突破！"

        next_level_index = player.level_index + 1
        next_level_data = level_data[next_level_index]
        required_exp = next_level_data.get("exp_needed", 0)

        if player.experience < required_exp:
            current_level = level_data[player.level_index]["level_name"]
            next_level = next_level_data["level_name"]
            return False, (
                f"修为不足！\n"
                f"当前境界：{current_level}\n"
                f"当前修为：{player.experience}\n"
                f"突破至【{next_level}】需要修为：{required_exp}"
            )

        return True, ""

    def calculate_breakthrough_success_rate(
        self,
        player: Player,
        pill_name: Optional[str] = None,
        temp_bonus: float = 0.0,
        pill_quality: Optional[str] = None,
        quality_multiplier: float = 1.0,
    ) -> Tuple[float, str]:
        """计算突破成功率"""
        level_data = self.config_manager.get_level_data(player.cultivation_type)

        next_level_index = player.level_index + 1
        next_level_data = level_data[next_level_index]
        base_success_rate = next_level_data.get("success_rate", 0.5)

        info_lines = [f"基础成功率：{base_success_rate:.1%}"]
        final_rate = base_success_rate + temp_bonus
        max_rate = 1.0

        if temp_bonus:
            info_lines.append(f"临时丹药加成：{temp_bonus:+.1%}")

        # 功德/业力修正：每点 ±1%，上限 ±10%
        karma = getattr(player, "karma", 0) or 0
        karma_bonus = 0.0
        if karma != 0:
            karma_bonus = max(-0.10, min(0.10, karma * 0.01))
            final_rate += karma_bonus
            if karma > 0:
                info_lines.append(f"功德庇佑：{karma_bonus:+.1%}（功德 {karma}）")
            else:
                info_lines.append(f"业力缠身：{karma_bonus:+.1%}（业力 {karma}，多行善可消解）")

        # 气运修正：每点 +0.2%，上限 +10%
        from ..managers.luck_manager import LuckManager
        luck_bonus = LuckManager.percent_bonus(
            player, per_point=0.2, cap=10.0, config_manager=self.config_manager
        ) / 100.0
        if luck_bonus:
            final_rate += luck_bonus
            info_lines.append(
                f"气运加持：{luck_bonus:+.1%}（气运 {LuckManager.get_total_luck(player, self.config_manager)}）"
            )

        if pill_name:
            base_name = pill_name.split("|", 1)[0]
            pill_data = self.config_manager.pills_data.get(base_name)
            if pill_data and pill_data.get("subtype") == "breakthrough":
                breakthrough_bonus = float(pill_data.get("breakthrough_bonus", 0)) * float(quality_multiplier or 1.0)
                max_rate = pill_data.get("max_success_rate", 1.0)
                final_rate = min(
                    base_success_rate + temp_bonus + karma_bonus + luck_bonus + breakthrough_bonus,
                    max_rate,
                )
                quality_label = f"·{pill_quality}" if pill_quality else ""
                info_lines.append(f"破境丹【{base_name}{quality_label}】加成：+{breakthrough_bonus:.1%}")
                info_lines.append(f"最大成功率限制：{max_rate:.1%}")
            else:
                logger.warning(f"无效的破境丹：{pill_name}")

        final_rate = max(0.0, min(final_rate, max_rate))
        info_lines.append(f"最终成功率：{final_rate:.1%}")
        
        return final_rate, "\n".join(info_lines)

    async def execute_breakthrough(
        self,
        player: Player,
        pill_name: Optional[str] = None,
        temp_bonus: float = 0.0,
        death_rate_multiplier: float = 1.0,
        pill_quality: Optional[str] = None,
    ) -> Tuple[bool, str, bool, bool]:
        """执行突破。返回 (是否成功, 消息, 是否删号, 是否真正发起突破)"""
        # 1. 检查突破条件
        can_breakthrough, error_msg = self.check_breakthrough_requirements(player)
        if not can_breakthrough:
            return False, error_msg, False, False

        # ===== 经济回收：突破需缴纳灵石（境界越高越贵）=====
        breakthrough_cost = 1000 + player.level_index * 800
        if player.gold < breakthrough_cost:
            return False, (
                f"❌ 突破需要缴纳 {breakthrough_cost:,} 灵石作为闭关资费（当前持有 {player.gold:,}）。\n"
                f"💡 无论成败资费皆消耗，请做好万全准备再闯关。"
            ), False, False

        # ===== 扣除玩家背包里的破境丹（支持品质）=====
        actual_pill_key = None
        quality_multiplier = 1.0
        if pill_name:
            from .pill_manager import PillManager
            pm = PillManager(self.db, self.config_manager)
            actual_pill_key = pm.select_pill_key(player.get_pills_inventory(), pill_name, pill_quality)
            if not actual_pill_key:
                quality_hint = f"（{pill_quality}）" if pill_quality else ""
                return False, f"❌ 你的背包中没有【{pill_name}】{quality_hint}，无法使用！", False, False
            base_name, actual_quality = pm.split_pill_key(actual_pill_key)
            pill_data = self.config_manager.pills_data.get(base_name)
            if not pill_data or pill_data.get("subtype") != "breakthrough":
                return False, f"❌ 【{base_name}】不是有效的破境丹，已中止突破（未消耗丹药）！", False, False
            target_level = pill_data.get("target_level_index", -1)
            if target_level != player.level_index + 1:
                return False, f"❌ 【{base_name}·{actual_quality}】不适用于本次突破（它用于突破到第{target_level + 1}境界），已中止（未消耗丹药）！", False, False

            quality_multiplier = pm.get_breakthrough_multiplier(actual_quality)
            inventory = player.get_pills_inventory()
            inventory[actual_pill_key] = int(inventory[actual_pill_key]) - 1
            if inventory[actual_pill_key] <= 0:
                del inventory[actual_pill_key]
            player.set_pills_inventory(inventory)
            await self.db.update_player(player)
            pill_name = base_name
            pill_quality = actual_quality
        # =========================================

        # 2. 计算成功率
        success_rate, rate_info = self.calculate_breakthrough_success_rate(
            player, pill_name, temp_bonus, pill_quality, quality_multiplier
        )

        level_data = self.config_manager.get_level_data(player.cultivation_type)

        # 3. 判定突破结果（资费已缴，无论成败不退）
        player.gold -= breakthrough_cost
        cost_note = f"\n💰 突破资费：-{breakthrough_cost:,} 灵石（剩余 {player.gold:,}）"
        random_value = random.random()
        breakthrough_success = random_value < success_rate

        current_level_name = level_data[player.level_index]["level_name"]
        next_level_index = player.level_index + 1
        next_level_data = level_data[next_level_index]
        next_level_name = next_level_data["level_name"]

        if breakthrough_success:
            # ===== 突破成功 =====
            player.level_index = next_level_index

            lifespan_gain = next_level_data.get("breakthrough_lifespan_gain", 0)
            mental_power_gain = next_level_data.get("breakthrough_mental_power_gain", 0)
            physical_damage_gain = next_level_data.get("breakthrough_physical_damage_gain", 0)
            magic_damage_gain = next_level_data.get("breakthrough_magic_damage_gain", 0)
            physical_defense_gain = next_level_data.get("breakthrough_physical_defense_gain", 0)
            magic_defense_gain = next_level_data.get("breakthrough_magic_defense_gain", 0)

            if player.cultivation_type == "体修":
                blood_qi_gain = next_level_data.get("breakthrough_blood_qi_gain", 0)
                player.max_blood_qi += blood_qi_gain
                player.blood_qi = player.max_blood_qi
                energy_name = "气血"
                energy_gain = blood_qi_gain
            else:
                spiritual_qi_gain = next_level_data.get("breakthrough_spiritual_qi_gain", 0)
                player.max_spiritual_qi += spiritual_qi_gain
                player.spiritual_qi = player.max_spiritual_qi
                energy_name = "灵气"
                energy_gain = spiritual_qi_gain

            player.lifespan += lifespan_gain
            player.physical_damage += physical_damage_gain
            player.magic_damage += magic_damage_gain
            player.physical_defense += physical_defense_gain
            player.magic_defense += magic_defense_gain
            player.mental_power += mental_power_gain

            await self.db.update_player(player)
            
            loan_msg = await self._handle_breakthrough_loan_repay(player)

            if player.cultivation_type == "体修":
                success_msg = (
                    f"✨ 突破成功！✨\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"{rate_info}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"恭喜你从【{current_level_name}】突破至【{next_level_name}】！\n"
                    f"境界提升，肉身更加强横！\n"
                    f"\n【属性增长】\n"
                    f"寿命 +{lifespan_gain}\n"
                    f"最大气血 +{energy_gain}\n"
                    f"物伤 +{physical_damage_gain}\n"
                    f"物防 +{physical_defense_gain}\n"
                    f"法防 +{magic_defense_gain}\n"
                    f"精神力 +{mental_power_gain}\n"
                    f"\n【当前属性】\n"
                    f"寿命：{player.lifespan}\n"
                    f"最大气血：{player.max_blood_qi}\n"
                    f"物伤：{player.physical_damage}\n"
                    f"物防：{player.physical_defense}\n"
                    f"法防：{player.magic_defense}\n"
                    f"精神力：{player.mental_power}"
                )
            else:
                success_msg = (
                    f"✨ 突破成功！✨\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"{rate_info}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"恭喜你从【{current_level_name}】突破至【{next_level_name}】！\n"
                    f"境界提升，实力大增！\n"
                    f"\n【属性增长】\n"
                    f"寿命 +{lifespan_gain}\n"
                    f"最大灵气 +{energy_gain}\n"
                    f"法伤 +{magic_damage_gain}\n"
                    f"物伤 +{physical_damage_gain}\n"
                    f"法防 +{magic_defense_gain}\n"
                    f"物防 +{physical_defense_gain}\n"
                    f"精神力 +{mental_power_gain}\n"
                    f"\n【当前属性】\n"
                    f"寿命：{player.lifespan}\n"
                    f"最大灵气：{player.max_spiritual_qi}\n"
                    f"法伤：{player.magic_damage}\n"
                    f"物伤：{player.physical_damage}\n"
                    f"法防：{player.magic_defense}\n"
                    f"物防：{player.physical_defense}\n"
                    f"精神力：{player.mental_power}"
                )

            logger.info(f"玩家 {player.user_id} 突破成功：{current_level_name} -> {next_level_name}")

            if loan_msg:
                success_msg += f"\n\n{loan_msg}"
            success_msg += (
                f"\n💰 突破资费：-{breakthrough_cost:,} 灵石（剩余 {player.gold:,}）"
            )

            # 师徒联动：弟子突破，师父得师德（v6.16）
            try:
                from ..managers.social_manager import SocialManager
                social = SocialManager(self.db)
                _, mentor_msg = await social.on_disciple_breakthrough(player.user_id)
                if mentor_msg:
                    success_msg += mentor_msg
            except Exception:
                pass

            return True, success_msg, False, True

        else:
            # ===== 突破失败 =====
            death_low, death_high = self._get_death_probability_range()
            death_rate = random.uniform(death_low, death_high)
            if player.cultivation_type == "体修":
                death_rate *= self._get_body_death_multiplier()
            death_rate = max(0.0, min(1.0, death_rate * death_rate_multiplier))
            died = random.random() < death_rate

            if died:
                # 原“死亡”判定：统一改为掉落一个大境界，不再删除角色
                from .pill_manager import PillManager
                pill_manager = PillManager(self.db, self.config_manager)
                res_quality = (
                    getattr(player, "resurrection_pill_quality", pill_manager.DEFAULT_QUALITY)
                    or pill_manager.DEFAULT_QUALITY
                )
                resurrected = await pill_manager.handle_resurrection(player)

                if resurrected:
                    resurrection_msg = (
                        f"💀 突破失败，走火入魔！💀\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"{rate_info}\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"你在突破【{next_level_name}】时走火入魔...\n"
                        f"\n"
                        f"⚡ 回生丹效果触发！⚡\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"🌟 回生丹护住你的道基，本次未跌落境界！\n"
                        f"💊 回生丹效果已消耗（{res_quality}）\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"请继续修炼，再接再厉！"
                    )
                    resurrection_msg += cost_note
                    logger.info(f"玩家 {player.user_id} 突破失败触发回生丹，避免境界跌落")
                    return False, resurrection_msg, False, True

                # 掉落一个大境界
                from .realm_penalty import fall_one_major_realm
                fall_info = fall_one_major_realm(player, self.config_manager)
                await self.db.update_player(player)

                fall_msg = (
                    f"💀 突破失败，走火入魔！💀\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"{rate_info}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"你在突破【{next_level_name}】时走火入魔，道基受损...\n"
                    f"\n"
                    f"📉 境界跌落：{fall_info['old_name']} → {fall_info['new_name']}\n"
                )
                if fall_info.get("exp_lost"):
                    fall_msg += f"修为受损：-{fall_info['exp_lost']:,}\n"
                fall_msg += (
                    f"━━━━━━━━━━━━━━━\n"
                    f"所幸保住性命，请重新修炼，再攀高峰！"
                )
                fall_msg += cost_note
                logger.info(
                    f"玩家 {player.user_id} 突破失败并跌落境界：{current_level_name} -> {next_level_name}，"
                    f"实际境界 {fall_info['old_name']} -> {fall_info['new_name']}，"
                    f"跌落概率 {death_rate:.2%}"
                )
                return False, fall_msg, False, True

            else:
                # 失败但未死
                exp_penalty = int(player.experience * 0.1)
                player.experience = max(0, player.experience - exp_penalty)
                await self.db.update_player(player)

                fail_msg = (
                    f"❌ 突破失败 ❌\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"{rate_info}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"突破【{next_level_name}】失败，但幸运地保住了性命\n"
                    f"修为受损，损失了 {exp_penalty} 点修为\n"
                    f"当前修为：{player.experience}"
                )
                fail_msg += cost_note
                fail_msg += "\n请继续修炼，再接再厉！"
                logger.info(f"玩家 {player.user_id} 突破失败：{current_level_name} -> {next_level_name}，损失修为 {exp_penalty}")
                return False, fail_msg, False, True
    
    def _get_death_probability_range(self) -> Tuple[float, float]:
        """读取并校验突破失败境界跌落概率区间，异常配置回退到 1%~10%。"""
        values = self.config.get("VALUES", {}) or {}
        raw = values.get("BREAKTHROUGH_DEATH_PROBABILITY", [0.01, 0.1])
        try:
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                low, high = float(raw[0]), float(raw[1])
            elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
                low = high = float(raw)
            else:
                low, high = 0.01, 0.1
        except (TypeError, ValueError):
            low, high = 0.01, 0.1
        low = max(0.0, min(1.0, low))
        high = max(0.0, min(1.0, high))
        if low > high:
            low, high = high, low
        return low, high

    def _get_body_death_multiplier(self) -> float:
        """体修突破失败的境界跌落风险倍率。"""
        values = self.config.get("VALUES", {}) or {}
        try:
            multiplier = float(values.get("BODY_BREAKTHROUGH_DEATH_MULTIPLIER", 1.5))
        except (TypeError, ValueError):
            multiplier = 1.5
        return max(0.0, multiplier)

    async def _handle_breakthrough_loan_repay(self, player: Player) -> str:
        """处理突破贷款自动还款"""
        try:
            loan = await self.db.ext.get_active_loan(player.user_id)
            if not loan or loan["loan_type"] != "breakthrough":
                return ""
            
            import time
            now = int(time.time())
            days_borrowed = max(1, (now - loan["borrowed_at"]) // 86400)
            accrued_interest = int(
                loan["principal"] * loan["interest_rate"] * days_borrowed
            )
            paid_interest = int(loan.get("paid_interest", 0) or 0)
            paid_principal = int(loan.get("paid_principal", 0) or 0)
            interest_due = max(0, accrued_interest - paid_interest)
            principal_due = max(0, int(loan["principal"]) - paid_principal)
            total_due = principal_due + interest_due
            
            if player.gold >= total_due:
                player.gold -= total_due
                await self.db.update_player(player)
                await self.db.ext.close_loan(loan["id"])
                
                bank_data = await self.db.ext.get_bank_account(player.user_id)
                balance = bank_data["balance"] if bank_data else 0
                await self.db.ext.add_bank_transaction(
                    player.user_id, "auto_repay", -total_due, balance,
                    f"突破成功自动还款：本金{principal_due:,}+利息{interest_due:,}", now
                )
                
                return (
                    f"💰 突破贷款自动还款成功！\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"已还本金：{principal_due:,} 灵石\n"
                    f"已还利息：{interest_due:,} 灵石\n"
                    f"当前持有：{player.gold:,} 灵石"
                )
            else:
                # ===== 修复点：如果钱不够，强制抽血抵扣或直接逾期 =====
                # 设定惩罚：如果钱不够，直接扣除所有灵石，并且血量减半（走火入魔）
                penalty_gold = player.gold
                player.gold = 0
                if player.cultivation_type == "体修":
                    player.blood_qi = max(1, player.blood_qi // 2)
                else:
                    player.hp = max(1, player.hp // 2)
                await self.db.update_player(player)
                
                # 将贷款标记为逾期
                await self.db.ext.mark_loan_overdue(loan["id"])
                
                return (
                    f"⚠️ 危险！突破贷款自动还款失败！\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"应还金额：{total_due:,} 灵石\n"
                    f"你只有：{penalty_gold:,} 灵石\n"
                    f"【天道降罚】你的灵石被全部清空，气血减半！\n"
                    f"贷款已被标记为逾期，请立刻前往银行还款，否则将掉落一个大境界！"
                )
        except Exception as e:
            logger.warning(f"处理突破贷款自动还款异常: {e}")
            return ""