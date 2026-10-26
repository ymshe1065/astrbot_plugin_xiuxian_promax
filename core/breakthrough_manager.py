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
        temp_bonus: float = 0.0
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
        if karma != 0:
            karma_bonus = max(-0.10, min(0.10, karma * 0.01))
            final_rate += karma_bonus
            if karma > 0:
                info_lines.append(f"功德庇佑：{karma_bonus:+.1%}（功德 {karma}）")
            else:
                info_lines.append(f"业力缠身：{karma_bonus:+.1%}（业力 {karma}，多行善可消解）")

        if pill_name:
            pill_data = self.config_manager.pills_data.get(pill_name)
            if pill_data and pill_data.get("subtype") == "breakthrough":
                breakthrough_bonus = pill_data.get("breakthrough_bonus", 0)
                max_rate = pill_data.get("max_success_rate", 1.0)
                final_rate = min(base_success_rate + temp_bonus + breakthrough_bonus, max_rate)
                info_lines.append(f"破境丹加成：+{breakthrough_bonus:.1%}")
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
        death_rate_multiplier: float = 1.0
    ) -> Tuple[bool, str, bool]:
        """执行突破"""
        # 1. 检查突破条件
        can_breakthrough, error_msg = self.check_breakthrough_requirements(player)
        if not can_breakthrough:
            return False, error_msg, False

        # ===== 经济回收：突破需缴纳灵石（境界越高越贵）=====
        breakthrough_cost = 1000 + player.level_index * 800
        if player.gold < breakthrough_cost:
            return False, (
                f"❌ 突破需要缴纳 {breakthrough_cost:,} 灵石作为闭关资费（当前持有 {player.gold:,}）。\n"
                f"💡 无论成败资费皆消耗，请做好万全准备再闯关。"
            ), False

        # ===== 核心修复：扣除玩家背包里的破境丹 =====
        if pill_name:
            # 先校验丹药配置，防止扣了丹却没有任何加成
            pill_data = self.config_manager.pills_data.get(pill_name)
            if not pill_data or pill_data.get("subtype") != "breakthrough":
                return False, f"❌ 【{pill_name}】不是有效的破境丹，已中止突破（未消耗丹药）！", False
            target_level = pill_data.get("target_level_index", -1)
            if target_level != player.level_index + 1:
                return False, f"❌ 【{pill_name}】不适用于本次突破（它用于突破到第{target_level + 1}境界），已中止（未消耗丹药）！", False

            inventory = player.get_pills_inventory()
            if pill_name in inventory and inventory[pill_name] > 0:
                inventory[pill_name] -= 1
                if inventory[pill_name] <= 0:
                    del inventory[pill_name]
                player.set_pills_inventory(inventory)
                await self.db.update_player(player)
            else:
                return False, f"❌ 你的背包中没有【{pill_name}】，无法使用！", False
        # =========================================

        # 2. 计算成功率
        success_rate, rate_info = self.calculate_breakthrough_success_rate(player, pill_name, temp_bonus)

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
            success_msg += cost_note

            # 师徒联动：弟子突破，师父得师德（v6.16）
            try:
                from ..managers.social_manager import SocialManager
                social = SocialManager(self.db)
                _, mentor_msg = await social.on_disciple_breakthrough(player.user_id)
                if mentor_msg:
                    success_msg += mentor_msg
            except Exception:
                pass

            return True, success_msg, False

        else:
            # ===== 突破失败 =====
            death_probability_range = self.config.get("VALUES", {}).get("BREAKTHROUGH_DEATH_PROBABILITY", [0.01, 0.1])
            death_rate = random.uniform(death_probability_range[0], death_probability_range[1])
            death_rate = max(0.0, min(1.0, death_rate * death_rate_multiplier))
            died = random.random() < death_rate

            if died:
                # 死亡逻辑
                from .pill_manager import PillManager
                pill_manager = PillManager(self.db, self.config_manager)
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
                        f"🌟 你涅槃重生了！\n"
                        f"⚠️ 但所有属性降低到之前的一半\n"
                        f"💊 回生丹效果已消耗\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"请继续修炼，重回巅峰！"
                    )
                    logger.info(f"玩家 {player.user_id} 突破失败触发回生丹，成功复活")
                    return False, resurrection_msg, False

                # 真死
                await self.db.delete_player_cascade(player.user_id)
                death_msg = (
                    f"💀 突破失败，走火入魔！💀\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"{rate_info}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"你在突破【{next_level_name}】时走火入魔，身死道消...\n"
                    f"所有修为和装备化为虚无\n"
                    f"若想重新修仙，请使用'我要修仙'命令重新开始"
                )
                logger.info(f"玩家 {player.user_id} 突破失败并死亡：{current_level_name} -> {next_level_name}，死亡概率 {death_rate:.2%}")
                return False, death_msg, True

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
                    f"当前修为：{player.experience}\n"
                    f"请继续修炼，再接再厉！"
                )
                logger.info(f"玩家 {player.user_id} 突破失败：{current_level_name} -> {next_level_name}，损失修为 {exp_penalty}")
                return False, fail_msg, False
    
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
                    f"贷款已被标记为逾期，请立刻前往银行还款，否则将被追杀致死！"
                )
        except Exception as e:
            logger.warning(f"处理突破贷款自动还款异常: {e}")
            return ""