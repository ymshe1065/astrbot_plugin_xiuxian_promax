# managers/bank_manager.py
"""灵石银行系统管理器 - 包含存取款、贷款、流水记录功能"""
import time
from decimal import Decimal, ROUND_DOWN
from typing import Tuple, List, Optional
from astrbot.api import logger
from ..data import DataBase
from ..models import Player
from ..loan_rules import loan_limit

__all__ = ["BankManager"]

# 银行配置默认值
DEFAULT_DAILY_INTEREST_RATE = 0.001  # 存款日利率 0.1%
# 未配置分档时的统一存款上限（兜底）。实际生效的是按大境界递增的 DEFAULT_DEPOSIT_CAPS。
DEFAULT_MAX_DEPOSIT = 100_000_000
# 存款上限按大境界递增：(该档起始 level_index, 上限)
# level_index 与 level_config.json / body_level_config.json 对齐，两套修炼路线都是 36 级、分档边界一致，
# 所以灵修和体修用同一张表；名字只写在文档里，代码只认 level_index，避免自定义境界名对不上。
# 曲线约每档 ×2～2.5：炼气 500 万 → 炼虚 2 亿 → 混元 500 亿。
DEFAULT_DEPOSIT_CAPS = (
    (0, 5_000_000),             # 炼气期
    (10, 10_000_000),           # 筑基期
    (13, 20_000_000),           # 金丹期
    (16, 50_000_000),           # 元婴期
    (19, 100_000_000),          # 化神期
    (22, 200_000_000),          # 炼虚期
    (25, 500_000_000),          # 合体期
    (28, 1_000_000_000),        # 大乘期
    (31, 2_000_000_000),        # 渡劫期
    (32, 5_000_000_000),        # 地仙
    (33, 10_000_000_000),       # 天仙
    (34, 20_000_000_000),       # 大罗金仙
    (35, 50_000_000_000),       # 混元大罗金仙
)
DEFAULT_DEPOSIT_CAP_SCALE = 1.0  # 分档上限的整体倍率，面板上可直接调
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
        
        # 配置分层：game_config.json 的 bank 段作底层默认，插件配置（BANK / VALUES.BANK）覆盖同名项。
        # 关键点：分档表只写在 game_config 里，而插件配置通常由 schema 默认值填满 BANK 段，
        # 若沿用「BANK 非空就整段替换」的老写法，分档表永远读不到。
        game_bank = {}
        if config_manager is not None:
            game_bank = (getattr(config_manager, "game_config", {}) or {}).get("bank", {}) or {}
        bank_config = {
            "DAILY_INTEREST_RATE": game_bank.get("daily_interest_rate", DEFAULT_DAILY_INTEREST_RATE),
            "MAX_DEPOSIT": game_bank.get("max_deposit", DEFAULT_MAX_DEPOSIT),
            "DEPOSIT_CAPS": game_bank.get("deposit_caps"),
            "DEPOSIT_CAP_SCALE": game_bank.get("deposit_cap_scale", DEFAULT_DEPOSIT_CAP_SCALE),
            "LOAN_INTEREST_RATE": game_bank.get("loan_interest_rate", DEFAULT_LOAN_INTEREST_RATE),
            "LOAN_DURATION_DAYS": game_bank.get("loan_duration_days", DEFAULT_LOAN_DURATION_DAYS),
            "MAX_LOAN_AMOUNT": game_bank.get("max_loan_amount", DEFAULT_MAX_LOAN_AMOUNT),
            "MIN_LOAN_AMOUNT": game_bank.get("min_loan_amount", DEFAULT_MIN_LOAN_AMOUNT),
            "BREAKTHROUGH_LOAN_RATE": game_bank.get("breakthrough_loan_rate", DEFAULT_BREAKTHROUGH_LOAN_RATE),
            "BREAKTHROUGH_LOAN_DURATION": game_bank.get("breakthrough_loan_duration", DEFAULT_BREAKTHROUGH_LOAN_DURATION),
        }
        user_bank = self.config.get("BANK") or (self.config.get("VALUES") or {}).get("BANK") or {}
        bank_config.update({k: v for k, v in user_bank.items() if v is not None})
        self.daily_interest_rate = bank_config.get("DAILY_INTEREST_RATE", DEFAULT_DAILY_INTEREST_RATE)
        self.max_deposit = bank_config.get("MAX_DEPOSIT", DEFAULT_MAX_DEPOSIT)
        self.deposit_caps = self._parse_deposit_caps(bank_config.get("DEPOSIT_CAPS"))
        self.deposit_cap_scale = self._parse_scale(bank_config.get("DEPOSIT_CAP_SCALE"))
        self.loan_interest_rate = bank_config.get("LOAN_INTEREST_RATE", DEFAULT_LOAN_INTEREST_RATE)
        self.loan_duration_days = bank_config.get("LOAN_DURATION_DAYS", DEFAULT_LOAN_DURATION_DAYS)
        self.max_loan_amount = bank_config.get("MAX_LOAN_AMOUNT", DEFAULT_MAX_LOAN_AMOUNT)
        self.min_loan_amount = bank_config.get("MIN_LOAN_AMOUNT", DEFAULT_MIN_LOAN_AMOUNT)
        self.breakthrough_loan_rate = bank_config.get("BREAKTHROUGH_LOAN_RATE", DEFAULT_BREAKTHROUGH_LOAN_RATE)
        self.breakthrough_loan_duration = bank_config.get("BREAKTHROUGH_LOAN_DURATION", DEFAULT_BREAKTHROUGH_LOAN_DURATION)
        self.loan_hold_ratio = bank_config.get("LOAN_HOLD_RATIO", DEFAULT_LOAN_HOLD_RATIO)
        self.loan_hold_min = bank_config.get("LOAN_HOLD_MIN", DEFAULT_LOAN_HOLD_MIN)
        self.loan_hold_free_limit = bank_config.get("LOAN_HOLD_FREE_LIMIT", DEFAULT_LOAN_HOLD_FREE_LIMIT)
    
    # ===== 存款上限（按大境界分档） =====

    @staticmethod
    def _parse_deposit_caps(raw) -> Tuple[Tuple[int, int], ...]:
        """把配置里的分档表规整成 ((min_level, cap), ...)；配置非法时退回默认表。"""
        if not raw:
            return DEFAULT_DEPOSIT_CAPS
        entries = []
        if isinstance(raw, dict):
            raw_items = list(raw.items())
            for min_level, cap in raw_items:
                entries.append((min_level, cap))
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, dict):
                    entries.append((item.get("min_level"), item.get("cap")))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    entries.append((item[0], item[1]))
        parsed = {}
        for min_level, cap in entries:
            try:
                level = int(min_level)
                value = int(cap)
            except (TypeError, ValueError):
                continue
            if level < 0 or value <= 0:
                continue
            parsed[level] = value
        if not parsed:
            return DEFAULT_DEPOSIT_CAPS
        return tuple(sorted(parsed.items()))

    @staticmethod
    def _parse_scale(raw) -> float:
        try:
            scale = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_DEPOSIT_CAP_SCALE
        return scale if scale > 0 else DEFAULT_DEPOSIT_CAP_SCALE

    @staticmethod
    def _level_index(player: Player) -> int:
        try:
            return max(0, int(getattr(player, "level_index", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _level_name(self, player: Player, index: Optional[int]) -> Optional[str]:
        """按 level_index 取境界名，用于面板展示；没有配置管理器时返回 None。"""
        if self.config_manager is None or index is None:
            return None
        try:
            data = self.config_manager.get_level_data(
                getattr(player, "cultivation_type", "灵修") or "灵修"
            )
            if 0 <= index < len(data):
                return data[index].get("level_name")
        except Exception:
            return None
        return None

    def deposit_cap_info(self, player: Player) -> dict:
        """当前境界的存款上限，以及突破后能提升到多少。"""
        level = self._level_index(player)
        cap = self.max_deposit
        cap_min_level = 0
        next_min_level = None
        next_cap = None
        for min_level, value in self.deposit_caps:
            if level >= min_level:
                cap, cap_min_level = value, min_level
            elif next_min_level is None:
                next_min_level, next_cap = min_level, value

        scaled_cap = int(cap * self.deposit_cap_scale)
        scaled_next = int(next_cap * self.deposit_cap_scale) if next_cap else None
        return {
            "cap": scaled_cap,
            "level_index": level,
            "cap_min_level": cap_min_level,
            "level_name": self._level_name(player, level),
            "next_cap": scaled_next,
            "next_level_index": next_min_level,
            "next_level_name": self._level_name(player, next_min_level),
        }

    def deposit_cap(self, player: Player) -> int:
        """当前境界可存款总额；未命中任何分档时退回统一上限。"""
        return self.deposit_cap_info(player)["cap"]

    @staticmethod
    def effective_loan_limit(player: Player) -> int:
        """当前境界可贷额度（方案A：押镖日收入 × 0.5，保底 10 万、硬顶 1000 万）。"""
        try:
            level = int(getattr(player, "level_index", 0) or 0)
        except (TypeError, ValueError):
            level = 0
        return loan_limit(level)

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
    
    def _settle_interest_time(self, balance: int, last_interest_time: int, now: int) -> Tuple[int, int]:
        """结算旧余额的整天利息，返回 (应入账利息, 新计息起点)。

        计息起点只按「整天」前移，不清掉不足一天的零头进度；
        只有 claim_interest 才把起点重置为当前时间。
        频繁存取款不再丢失计息进度，也不会重复结算已入账的整天利息。
        """
        pending_interest = self._calculate_interest(balance, last_interest_time)
        if last_interest_time <= 0:
            return pending_interest, now  # 首次建户：从此刻开始计息
        days_passed = max(0, (now - last_interest_time) // 86400)
        return pending_interest, last_interest_time + days_passed * 86400
    
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

            now = int(time.time())
            # 先结算旧余额已产生的整天利息（防临领取前大额存入套取历史利息），
            # 计息起点只按整天前移，不再重置为当前时间——频繁存取不清零头进度
            last_interest_time = int(bank_data["last_interest_time"]) if bank_data else 0
            pending_interest, interest_time = self._settle_interest_time(
                current_balance, last_interest_time, now
            )
            # 上限按境界分档，且按「余额 + 待领利息」算——否则利息能把余额顶过上限，限额形同虚设
            available = current_balance + pending_interest
            cap_info = self.deposit_cap_info(player)
            cap = cap_info["cap"]
            room = max(0, cap - available)
            if amount > room:
                await self.db.conn.rollback()
                interest_note = f"（含待领利息 {pending_interest:,}）" if pending_interest else ""
                realm_note = f"（{cap_info['level_name']}）" if cap_info.get("level_name") else ""
                msg = (
                    f"存款上限为 {cap:,} 灵石{realm_note}。\n"
                    f"当前余额 {available:,}{interest_note}，"
                    f"本次最多还能存 {room:,}。"
                )
                if cap_info.get("next_cap"):
                    next_label = cap_info.get("next_level_name") or "下一境界"
                    msg += f"\n突破到 {next_label} 后上限提升至 {cap_info['next_cap']:,}。"
                return False, msg

            player.gold -= amount
            await self.db.update_player(player, commit=False)

            new_balance = available + amount
            
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
            used = new_balance / cap if cap else 0
            realm_note = f"（{cap_info['level_name']}）" if cap_info.get("level_name") else ""
            return True, (
                f"成功存入 {amount:,} 灵石！\n"
                f"当前余额：{new_balance:,} 灵石\n"
                f"存款上限：{cap:,}{realm_note}，已用 {used:.1%}，还能存 {max(0, cap - new_balance):,}"
            )
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
            # 取款同样只结算整天利息并按整天前移计息起点，保证存取款前后的利息归属清晰
            if bank_data:
                pending_interest, interest_time = self._settle_interest_time(
                    int(bank_data["balance"]), int(bank_data["last_interest_time"]), now
                )
            else:
                pending_interest, interest_time = 0, now
            available = (bank_data["balance"] if bank_data else 0) + pending_interest
            if not bank_data or available < amount:
                await self.db.conn.rollback()
                return False, f"余额不足！当前可取出：{available:,} 灵石。"

            new_balance = available - amount
            
            await self.db.conn.execute(
                "INSERT INTO bank_accounts (user_id, balance, last_interest_time) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance, last_interest_time = excluded.last_interest_time",
                (player.user_id, new_balance, interest_time)
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
        
        limit = self.effective_loan_limit(player)
        if amount > limit:
            return False, (
                f"超过你的可贷额度：{limit:,} 灵石（按当前境界押镖日收入动态计算）。"
            )
        
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
        """检查并处理逾期贷款 - 逾期玩家掉落一个大境界（不再删除角色）

        与指令路径 handlers/utils._check_loan_status 存在竞态：每笔贷款单独包
        BEGIN IMMEDIATE 事务，事务内用条件 UPDATE 抢占（只有仍是 active/overdue
        的贷款才能转坏账），rowcount=0 说明已被其他路径处理或已还清，直接跳过，
        杜绝同一笔贷款双重跌境。
        """
        now = int(time.time())
        overdue_loans = await self.db.ext.get_overdue_loans(now)
        processed = []

        from ..core.realm_penalty import fall_one_major_realm

        for loan in overdue_loans:
            await self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                # 事务内条件更新抢占这笔贷款；已坏账/已还清的不会被重复处罚
                cursor = await self.db.conn.execute(
                    "UPDATE bank_loans SET status = 'bad_debt' "
                    "WHERE id = ? AND status IN ('active', 'overdue')",
                    (loan["id"],),
                )
                if not cursor.rowcount:
                    await self.db.conn.rollback()
                    continue

                player = await self.db.get_player_by_id(loan["user_id"])
                if not player:
                    # 角色已不存在：贷款直接转坏账（上面的条件更新已生效）
                    await self.db.conn.commit()
                    continue

                player_name = player.user_name or f"道友{player.user_id[:6]}"

                fall_info = fall_one_major_realm(player, self.config_manager)
                # 境界跌落用定向 UPDATE 持久化，避免旧快照全字段覆盖丢失并发修改
                await self.db.conn.execute(
                    "UPDATE players SET level_index = ?, experience = ? WHERE user_id = ?",
                    (player.level_index, player.experience, player.user_id),
                )

                # 写流水；跌境 + 置坏账 + 流水同一事务提交
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
            except Exception as e:
                await self.db.conn.rollback()
                logger.error(f"处理逾期贷款失败（id={loan.get('id')}）: {e}")
                continue

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