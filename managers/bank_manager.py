# managers/bank_manager.py
"""灵石银行系统管理器 - 包含存取款、贷款、流水记录功能"""
import time
from decimal import Decimal, ROUND_DOWN
from typing import Tuple, List, Optional
from ..data import DataBase
from ..models import Player

__all__ = ["BankManager"]

# 银行配置默认值
DEFAULT_DAILY_INTEREST_RATE = 0.001  # 存款日利率 0.1%
DEFAULT_MAX_DEPOSIT = 10000000  # 最大存款上限 1000万
DEFAULT_LOAN_INTEREST_RATE = 0.005  # 贷款日利率 0.5%
DEFAULT_LOAN_DURATION_DAYS = 7  # 贷款期限 7天
DEFAULT_MAX_LOAN_AMOUNT = 1000000  # 最大贷款额度 100万
DEFAULT_MIN_LOAN_AMOUNT = 1000  # 最小贷款额度 1000
DEFAULT_BREAKTHROUGH_LOAN_RATE = 0.008  # 突破贷款日利率 0.8%（更高风险）
DEFAULT_BREAKTHROUGH_LOAN_DURATION = 3  # 突破贷款期限 3天

# 大额贷款持有门槛：贷款超过免费额度后，至少持有贷款额的 20%（最低 5000）
DEFAULT_LOAN_HOLD_RATIO = 0.20
DEFAULT_LOAN_HOLD_MIN = 5000
DEFAULT_LOAN_HOLD_FREE_LIMIT = 10000


class BankManager:
    """灵石银行管理器"""
    
    def __init__(self, db: DataBase, config: dict = None, config_manager=None):
        self.db = db
        self.config = config or {}
        self.config_manager = config_manager
        
        # 从配置读取，使用默认值作为后备（兼容根键、VALUES 层与 game_config）
        bank_config = self.config.get("BANK") or (self.config.get("VALUES") or {}).get("BANK") or {}
        if not bank_config and config_manager is not None:
            game_bank = (getattr(config_manager, "game_config", {}) or {}).get("bank", {}) or {}
            bank_config = {
                "DAILY_INTEREST_RATE": game_bank.get("daily_interest_rate", DEFAULT_DAILY_INTEREST_RATE),
                "MAX_DEPOSIT": game_bank.get("max_deposit", DEFAULT_MAX_DEPOSIT),
                "LOAN_INTEREST_RATE": game_bank.get("loan_interest_rate", DEFAULT_LOAN_INTEREST_RATE),
                "LOAN_DURATION_DAYS": game_bank.get("loan_duration_days", DEFAULT_LOAN_DURATION_DAYS),
                "MAX_LOAN_AMOUNT": game_bank.get("max_loan_amount", DEFAULT_MAX_LOAN_AMOUNT),
                "MIN_LOAN_AMOUNT": game_bank.get("min_loan_amount", DEFAULT_MIN_LOAN_AMOUNT),
                "BREAKTHROUGH_LOAN_RATE": game_bank.get("breakthrough_loan_rate", DEFAULT_BREAKTHROUGH_LOAN_RATE),
                "BREAKTHROUGH_LOAN_DURATION": game_bank.get("breakthrough_loan_duration", DEFAULT_BREAKTHROUGH_LOAN_DURATION),
            }
        self.daily_interest_rate = bank_config.get("DAILY_INTEREST_RATE", DEFAULT_DAILY_INTEREST_RATE)
        self.max_deposit = bank_config.get("MAX_DEPOSIT", DEFAULT_MAX_DEPOSIT)
        self.loan_interest_rate = bank_config.get("LOAN_INTEREST_RATE", DEFAULT_LOAN_INTEREST_RATE)
        self.loan_duration_days = bank_config.get("LOAN_DURATION_DAYS", DEFAULT_LOAN_DURATION_DAYS)
        self.max_loan_amount = bank_config.get("MAX_LOAN_AMOUNT", DEFAULT_MAX_LOAN_AMOUNT)
        self.min_loan_amount = bank_config.get("MIN_LOAN_AMOUNT", DEFAULT_MIN_LOAN_AMOUNT)
        self.breakthrough_loan_rate = bank_config.get("BREAKTHROUGH_LOAN_RATE", DEFAULT_BREAKTHROUGH_LOAN_RATE)
        self.breakthrough_loan_duration = bank_config.get("BREAKTHROUGH_LOAN_DURATION", DEFAULT_BREAKTHROUGH_LOAN_DURATION)
        self.loan_hold_ratio = bank_config.get("LOAN_HOLD_RATIO", DEFAULT_LOAN_HOLD_RATIO)
        self.loan_hold_min = bank_config.get("LOAN_HOLD_MIN", DEFAULT_LOAN_HOLD_MIN)
        self.loan_hold_free_limit = bank_config.get("LOAN_HOLD_FREE_LIMIT", DEFAULT_LOAN_HOLD_FREE_LIMIT)
    
    def _required_holdings(self, amount: int) -> int:
        """大额贷款要求的最低持有金额。"""
        if amount <= self.loan_hold_free_limit:
            return 0
        return max(int(self.loan_hold_min), int(amount * self.loan_hold_ratio))

    # ===== 存款相关 =====
    
    async def get_bank_info(self, player: Player) -> dict:
        """获取银行账户信息"""
        bank_data = await self.db.ext.get_bank_account(player.user_id)
        if not bank_data:
            bank_info = {"balance": 0, "last_interest_time": 0, "pending_interest": 0}
        else:
            pending_interest = self._calculate_interest(
                bank_data["balance"], 
                bank_data["last_interest_time"]
            )
            bank_info = {
                "balance": bank_data["balance"],
                "last_interest_time": bank_data["last_interest_time"],
                "pending_interest": pending_interest
            }
        
        loan = await self.db.ext.get_active_loan(player.user_id)
        bank_info["loan"] = loan
        
        return bank_info
    
    def _calculate_interest(self, balance: int, last_time: int) -> int:
        """计算待领利息（使用Decimal精确计算）"""
        if balance <= 0 or last_time <= 0:
            return 0
        
        now = int(time.time())
        days_passed = (now - last_time) // 86400
        
        if days_passed < 1:
            return 0
        
        balance_d = Decimal(str(balance))
        rate_d = Decimal(str(self.daily_interest_rate))
        
        compound = (1 + rate_d) ** days_passed - 1
        interest = balance_d * compound
        
        return int(interest.quantize(Decimal('1'), rounding=ROUND_DOWN))
    
    async def deposit(self, player: Player, amount: int) -> Tuple[bool, str]:
        """存入灵石"""
        if amount <= 0:
            return False, "存款金额必须大于0。"
        
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(player.user_id)
            if player.gold < amount:
                await self.db.conn.rollback()
                return False, f"灵石不足！你只有 {player.gold:,} 灵石。"
            
            bank_data = await self.db.ext.get_bank_account(player.user_id)
            current_balance = bank_data["balance"] if bank_data else 0
            
            if current_balance + amount > self.max_deposit:
                await self.db.conn.rollback()
                return False, f"存款上限为 {self.max_deposit:,} 灵石，当前余额 {current_balance:,}。"
            
            player.gold -= amount
            await self.db.update_player(player, commit=False)

            now = int(time.time())
            # 先结算旧余额已产生的利息，再重置计息起点；防止临领取前大额存入套取历史利息
            pending_interest = self._calculate_interest(
                current_balance, bank_data["last_interest_time"] if bank_data else 0
            )
            new_balance = current_balance + pending_interest + amount
            interest_time = now
            
            # 直接底层 SQL 更新账户
            await self.db.conn.execute(
                "INSERT INTO bank_accounts (user_id, balance, last_interest_time) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance, last_interest_time = excluded.last_interest_time",
                (player.user_id, new_balance, interest_time)
            )
            
            # 直接底层 SQL 写流水
            await self.db.conn.execute(
                "INSERT INTO bank_transactions (user_id, trans_type, amount, balance_after, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (player.user_id, "deposit", amount, new_balance, "存入灵石", now)
            )
            
            await self.db.conn.commit()
            return True, f"成功存入 {amount:,} 灵石！\n当前余额：{new_balance:,} 灵石"
        except Exception as e:
            await self.db.conn.rollback()
            raise
    
    async def withdraw(self, player: Player, amount: int) -> Tuple[bool, str]:
        """取出灵石"""
        if amount <= 0:
            return False, "取款金额必须大于0。"
        
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(player.user_id)
            bank_data = await self.db.ext.get_bank_account(player.user_id)

            now = int(time.time())
            # 取款同样先结算利息并重置计息起点，保证存取款前后的利息归属清晰
            pending_interest = self._calculate_interest(
                bank_data["balance"], bank_data["last_interest_time"]
            ) if bank_data else 0
            available = (bank_data["balance"] if bank_data else 0) + pending_interest
            if not bank_data or available < amount:
                await self.db.conn.rollback()
                return False, f"余额不足！当前可取出：{available:,} 灵石。"

            new_balance = available - amount
            
            await self.db.conn.execute(
                "INSERT INTO bank_accounts (user_id, balance, last_interest_time) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance, last_interest_time = excluded.last_interest_time",
                (player.user_id, new_balance, now)
            )
            
            player.gold += amount
            await self.db.update_player(player, commit=False)
            
            await self.db.conn.execute(
                "INSERT INTO bank_transactions (user_id, trans_type, amount, balance_after, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (player.user_id, "withdraw", -amount, new_balance, "取出灵石", now)
            )
            
            await self.db.conn.commit()
            return True, f"成功取出 {amount:,} 灵石！\n当前余额：{new_balance:,} 灵石\n当前持有：{player.gold:,} 灵石"
        except Exception as e:
            await self.db.conn.rollback()
            raise
    
    async def claim_interest(self, player: Player) -> Tuple[bool, str]:
        """领取利息（事务化，防止并发双领）"""
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 事务内重读账户，确保利息以最新余额计算
            bank_data = await self.db.ext.get_bank_account(player.user_id)
            if not bank_data or bank_data["balance"] <= 0:
                await self.db.conn.rollback()
                return False, "你还没有存款，无法领取利息。"

            interest = self._calculate_interest(
                bank_data["balance"],
                bank_data["last_interest_time"]
            )

            if interest <= 0:
                await self.db.conn.rollback()
                return False, "利息不足1灵石，请明日再来。"

            new_balance = bank_data["balance"] + interest
            now = int(time.time())

            # 直接底层 SQL 更新账户（附带余额校验，防并发重复领取）
            cursor = await self.db.conn.execute(
                "UPDATE bank_accounts SET balance = ?, last_interest_time = ? "
                "WHERE user_id = ? AND balance = ? AND last_interest_time = ?",
                (new_balance, now, player.user_id, bank_data["balance"], bank_data["last_interest_time"])
            )
            if cursor.rowcount == 0:
                await self.db.conn.rollback()
                return False, "❌ 领取失败，账户刚发生变化，请重试。"

            # 写流水
            await self.db.conn.execute(
                "INSERT INTO bank_transactions (user_id, trans_type, amount, balance_after, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (player.user_id, "interest", interest, new_balance, "领取利息", now)
            )

            await self.db.conn.commit()

            return True, f"成功领取利息 {interest:,} 灵石！\n当前余额：{new_balance:,} 灵石"
        except Exception:
            await self.db.conn.rollback()
            raise
    
    # ===== 贷款相关 =====
    
    async def get_loan_info(self, player: Player) -> Optional[dict]:
        """获取贷款详情"""
        loan = await self.db.ext.get_active_loan(player.user_id)
        if not loan:
            return None
        
        now = int(time.time())
        days_borrowed = (now - loan["borrowed_at"]) // 86400
        days_remaining = max(0, (loan["due_at"] - now) // 86400)
        
        principal = int(loan["principal"])
        paid_interest = int(loan.get("paid_interest", 0) or 0)
        paid_principal = int(loan.get("paid_principal", 0) or 0)

        # 利息始终按原始本金计算；部分还款只抵扣“已产生的利息/本金”
        accrued_interest = int(
            principal * loan["interest_rate"] * max(1, days_borrowed)
        )
        interest_due = max(0, accrued_interest - paid_interest)
        principal_due = max(0, principal - paid_principal)
        total_due = principal_due + interest_due

        is_overdue = now > loan["due_at"]

        return {
            **loan,
            "days_borrowed": days_borrowed,
            "days_remaining": days_remaining,
            "accrued_interest": accrued_interest,
            "current_interest": interest_due,
            "remaining_principal": principal_due,
            "paid_interest": paid_interest,
            "paid_principal": paid_principal,
            "total_due": total_due,
            "is_overdue": is_overdue
        }
    
    async def borrow(self, player: Player, amount: int, loan_type: str = "normal") -> Tuple[bool, str]:
        """申请贷款"""
        if amount < self.min_loan_amount:
            return False, f"最小贷款金额为 {self.min_loan_amount:,} 灵石。"
        
        if amount > self.max_loan_amount:
            return False, f"最大贷款金额为 {self.max_loan_amount:,} 灵石。"
        
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(player.user_id)
            existing_loan = await self.db.ext.get_active_loan(player.user_id)
            if existing_loan:
                await self.db.conn.rollback()
                return False, "你已有未还清的贷款，请先还款后再申请新贷款。"

            # 大额贷款需要一定的持有金额（灵石 + 银行存款）
            required_holdings = self._required_holdings(amount)
            if required_holdings > 0:
                bank_data = await self.db.ext.get_bank_account(player.user_id)
                bank_balance = int(bank_data["balance"]) if bank_data else 0
                holdings = int(player.gold) + bank_balance
                if holdings < required_holdings:
                    await self.db.conn.rollback()
                    return False, (
                        "❌ 持有金额不足，无法申请大额贷款！\n"
                        f"贷款金额：{amount:,} 灵石\n"
                        f"需要持有：{required_holdings:,} 灵石\n"
                        f"当前持有：{holdings:,}（灵石 {player.gold:,} + 存款 {bank_balance:,}）"
                    )
            
            if loan_type == "breakthrough":
                interest_rate = self.breakthrough_loan_rate
                duration_days = self.breakthrough_loan_duration
                type_name = "突破贷款"
            else:
                interest_rate = self.loan_interest_rate
                duration_days = self.loan_duration_days
                type_name = "普通贷款"
            
            now = int(time.time())
            due_at = now + duration_days * 86400
            
            # 直接底层 SQL 创建贷款
            await self.db.conn.execute(
                """INSERT INTO bank_loans (user_id, principal, interest_rate, borrowed_at, due_at, status, loan_type)
                   VALUES (?, ?, ?, ?, ?, 'active', ?)""",
                (player.user_id, amount, interest_rate, now, due_at, loan_type)
            )
            
            player.gold += amount
            await self.db.update_player(player, commit=False)
            
            bank_data = await self.db.ext.get_bank_account(player.user_id)
            balance = bank_data["balance"] if bank_data else 0
            
            await self.db.conn.execute(
                "INSERT INTO bank_transactions (user_id, trans_type, amount, balance_after, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (player.user_id, "loan", amount, balance, f"{type_name}：借入{amount:,}灵石", now)
            )
            
            total_interest = int(amount * interest_rate * duration_days)
            total_due = amount + total_interest
            
            await self.db.conn.commit()
            return True, (
                f"💰 {type_name}成功！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"借入金额：{amount:,} 灵石\n"
                f"日利率：{interest_rate:.1%}\n"
                f"还款期限：{duration_days} 天\n"
                f"到期应还：约 {total_due:,} 灵石\n"
                + (f"持有门槛：{required_holdings:,} 灵石（已满足）\n" if required_holdings > 0 else "")
                + f"━━━━━━━━━━━━━━━\n"
                f"当前持有：{player.gold:,} 灵石\n"
                f"💀 逾期将掉落一个大境界！"
            )
        except Exception as e:
            await self.db.conn.rollback()
            raise
    
    async def repay(self, player: Player, amount: Optional[int] = None) -> Tuple[bool, str]:
        """还款；amount 为空时全额还清，否则先还利息再还本金。"""
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(player.user_id)
            loan_info = await self.get_loan_info(player)
            if not loan_info:
                await self.db.conn.rollback()
                return False, "你当前没有需要偿还的贷款。"

            total_due = int(loan_info["total_due"])
            if total_due <= 0:
                await self.db.conn.execute(
                    "UPDATE bank_loans SET status = 'closed' WHERE id = ?",
                    (loan_info["id"],),
                )
                await self.db.conn.commit()
                return True, "✅ 贷款余额已清零，贷款已关闭。"

            if amount is None:
                pay = total_due
            else:
                try:
                    pay = int(amount)
                except (TypeError, ValueError):
                    await self.db.conn.rollback()
                    return False, "❌ 还款金额必须是整数。"
                if pay <= 0:
                    await self.db.conn.rollback()
                    return False, "❌ 还款金额必须大于 0。"
                pay = min(pay, total_due)

            if player.gold < pay:
                await self.db.conn.rollback()
                return False, (
                    f"灵石不足！\n"
                    f"本次计划还款：{pay:,} 灵石\n"
                    f"当前持有：{player.gold:,} 灵石\n"
                    f"还差：{pay - player.gold:,} 灵石"
                )

            interest_due = int(loan_info["current_interest"])
            principal_due = int(loan_info["remaining_principal"])
            pay_interest = min(pay, interest_due)
            pay_principal = min(pay - pay_interest, principal_due)
            remainder = pay - pay_interest - pay_principal
            # 理论上不会出现余数；若出现，补到本金
            if remainder > 0:
                pay_principal += remainder

            remaining = max(0, total_due - pay)
            if remaining <= 0:
                status = "closed"
            else:
                # 部分还款不抹除逾期状态，避免变相逃避银行追杀
                status = "overdue" if loan_info.get("status") == "overdue" else "active"
            new_paid_interest = int(loan_info["paid_interest"]) + pay_interest
            new_paid_principal = int(loan_info["paid_principal"]) + pay_principal

            player.gold -= pay
            await self.db.update_player(player, commit=False)

            await self.db.conn.execute(
                "UPDATE bank_loans SET paid_interest = ?, paid_principal = ?, status = ? "
                "WHERE id = ?",
                (new_paid_interest, new_paid_principal, status, loan_info["id"]),
            )

            bank_data = await self.db.ext.get_bank_account(player.user_id)
            balance = bank_data["balance"] if bank_data else 0
            now = int(time.time())
            desc = (
                f"还款：利息{pay_interest:,}+本金{pay_principal:,}"
                if status == "closed"
                else f"部分还款：利息{pay_interest:,}+本金{pay_principal:,}，剩余{remaining:,}"
            )
            await self.db.conn.execute(
                "INSERT INTO bank_transactions (user_id, trans_type, amount, balance_after, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (player.user_id, "repay", -pay, balance, desc, now),
            )

            loan_type_name = (
                "突破贷款" if loan_info["loan_type"] == "breakthrough" else "普通贷款"
            )
            await self.db.conn.commit()

            if status == "closed":
                return True, (
                    f"✅ 还款成功，贷款已结清！\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"贷款类型：{loan_type_name}\n"
                    f"本次还利息：{pay_interest:,} 灵石\n"
                    f"本次还本金：{pay_principal:,} 灵石\n"
                    f"本次支付：{pay:,} 灵石\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"当前持有：{player.gold:,} 灵石"
                )

            status_label = "overdue（逾期）" if status == "overdue" else "active（未结清）"
            return True, (
                f"✅ 部分还款成功！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"贷款类型：{loan_type_name}\n"
                f"本次还利息：{pay_interest:,} 灵石\n"
                f"本次还本金：{pay_principal:,} 灵石\n"
                f"本次支付：{pay:,} 灵石\n"
                f"剩余应还：{remaining:,} 灵石\n"
                f"（剩余本金 {max(0, principal_due - pay_principal):,} + "
                f"剩余利息 {max(0, interest_due - pay_interest):,}）\n"
                f"贷款状态：{status_label}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"当前持有：{player.gold:,} 灵石"
            )
        except Exception as e:
            await self.db.conn.rollback()
            raise
    
    async def check_and_process_overdue_loans(self) -> List[dict]:
        """检查并处理逾期贷款 - 逾期玩家掉落一个大境界（不再删除角色）"""
        now = int(time.time())
        overdue_loans = await self.db.ext.get_overdue_loans(now)
        processed = []

        from ..core.realm_penalty import fall_one_major_realm

        for loan in overdue_loans:
            player = await self.db.get_player_by_id(loan["user_id"])
            if not player:
                await self.db.conn.execute(
                    "UPDATE bank_loans SET status = 'bad_debt' WHERE id = ?",
                    (loan["id"],),
                )
                await self.db.conn.commit()
                continue

            player_name = player.user_name or f"道友{player.user_id[:6]}"

            fall_info = fall_one_major_realm(player, self.config_manager)
            await self.db.update_player_fields(
                player.user_id,
                level_index=player.level_index,
                experience=player.experience,
            )

            # 贷款转为坏账，避免同一笔欠款每小时重复触发境界跌落
            await self.db.conn.execute(
                "UPDATE bank_loans SET status = 'bad_debt' WHERE id = ?",
                (loan["id"],),
            )

            # 写流水
            now = int(time.time())
            await self.db.conn.execute(
                "INSERT INTO bank_transactions (user_id, trans_type, amount, balance_after, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    loan["user_id"], "bank_kill", 0, 0,
                    f"逾期未还款，境界跌落：{fall_info['old_name']}→{fall_info['new_name']}",
                    now,
                )
            )
            await self.db.conn.commit()

            processed.append({
                **loan,
                **fall_info,
                "player_name": player_name,
                "realm_fall": bool(fall_info.get("dropped")),
            })

        return processed
    
    # ===== 流水相关 =====
    
    async def get_transactions(self, user_id: str, limit: int = 20) -> List[dict]:
        """获取交易流水"""
        return await self.db.ext.get_bank_transactions(user_id, limit)
    
    # ===== 排行榜 =====
    
    async def get_deposit_ranking(self, limit: int = 10) -> List[dict]:
        """获取存款排行榜"""
        return await self.db.ext.get_deposit_ranking(limit)