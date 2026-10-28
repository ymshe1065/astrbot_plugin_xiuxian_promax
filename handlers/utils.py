# handlers/utils.py
# 通用工具函数和装饰器

import asyncio
import re
import time
from functools import wraps
from typing import Callable, Coroutine, AsyncGenerator

from astrbot.api.event import AstrMessageEvent
from ..models import Player
from ..models_extended import UserStatus



def set_event_flag(event: AstrMessageEvent, key: str, value=True) -> None:
    """按事件保存本次指令结果，避免 Handler 实例变量在并发用户间串线。"""
    flags = getattr(event, "_xiuxian_flags", None)
    if not isinstance(flags, dict):
        flags = {}
        setattr(event, "_xiuxian_flags", flags)
    flags[key] = value


def get_event_flag(event: AstrMessageEvent, key: str, default=False):
    flags = getattr(event, "_xiuxian_flags", None)
    if isinstance(flags, dict) and key in flags:
        return flags[key]
    return default

# 指令常量
CMD_START_XIUXIAN = "我要修仙"
CMD_PLAYER_INFO = "我的信息"
CMD_START_CULTIVATION = "闭关"
CMD_END_CULTIVATION = "出关"
CMD_CHECK_IN = "签到"

# 忙碌状态下允许执行的命令白名单（只能查信息、收菜、领奖）
BUSY_STATE_ALLOWED_COMMANDS = [
    # 基础信息查看
    CMD_PLAYER_INFO, "我的信息", "我的装备", "我的灵兽", "孵化灵兽", "喂养灵兽", "灵兽出战", "灵兽改名", "放生灵兽", "吞噬灵兽", "我的洞天", "我的灵田", "我的宗门",
    "银行", "储物戒", "丹药背包", "修仙帮助", "物品信息", "黑市", "天魔状态", "查验天魔", "世界Boss", "世界boss", "世界Boss状态", "灵眼信息", "江湖事件", "通缉令",
    "宗门商店", "宗门兑换", "我的坊市", "坊市我的", "坊市卖出", "坊市等待",
    "拍卖会", "拍卖会信息", "拍卖会报名", "拍卖会入场", "拍卖会出价", "拍卖会领取",
    
    # 收菜与领奖（不打架，纯收益）
    CMD_CHECK_IN, "签到", "领取利息", "洞天收取", "收获", "灵眼收取", 
    "领取任务奖励", "放弃任务", "放弃历练", "放弃宗门任务", "仙灵代收",
    "完成悬赏", "完成探索", "完成历练", "完成宗门任务",
    
    # 榜单与状态
    "任务榜", "悬赏状态", "历练状态", "境界排行", "战力排行", "灵石排行", "宗门排行", "存款排行", "贡献排行",
    "传承排行", "功德榜", "气运榜", "师德榜", "气运", "团本排行",
    
    # 退出/放弃类（必须允许，否则会卡死）
    CMD_END_CULTIVATION, "出关", "解卡",
    # 退出/完成类
    "完成历练", "完成悬赏", "放弃悬赏", "完成探索", "退出秘境",
    "完成宗门任务", "完成押镖", "放弃押镖",

    # 押镖专属（押镖中必须能结算/放弃自己的镖）
    "押镖",

    # 社交回应（双修/切磋请求到达时对方可能正忙，必须能回应）
    "接受双修", "拒绝双修", "接受切磋", "拒绝切磋",

    # 全服事件查询（魔潮肆虐时也该能看进度）
    "魔潮状态", "抵御魔潮",

    # v6.26 自动代理（忙碌中也必须能查看/关闭/改绑）
    "自动代理", "关闭代理", "代理状态",

    # 调息/收功与恢复
    "调息", "收功", "服用丹药", "丹药背包",
]

# 受伤时禁止行动类指令
INJURED_BLOCKED_COMMANDS = (
    "押镖", "劫镖", "切磋", "接受切磋", "决斗", "挑战Boss", "挑战世界Boss",
    "历练", "开始历练", "探索秘境", "开始探索", "进入秘境",
    "宗门任务", "接取宗门任务", "宗门大比", "报名宗门大比", "团本",
    "传承挑战", "闭关",
)

# 这些是“退出/放弃当前状态”的指令，无论处于什么忙碌状态都必须放行
BUSY_BYPASS_COMMANDS = (
    "出关", "解卡",
    "完成历练", "完成悬赏", "放弃悬赏", "完成探索", "退出秘境",
    "完成宗门任务", "完成押镖", "放弃押镖",
    "放弃任务", "放弃历练", "放弃宗门任务",
    "自动代理", "关闭代理", "代理状态",
)

# 每个玩家一把状态锁：把“检查状态 → 修改状态”串行化，避免并发开始闭关/历练时互相覆盖
_PLAYER_LOCKS: dict[str, asyncio.Lock] = {}


def get_player_lock(user_id: str) -> asyncio.Lock:
    lock = _PLAYER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _PLAYER_LOCKS[user_id] = lock
    return lock


def _handler_config_manager(handler):
    """从 Handler 自身或它持有的管理器上找到插件 ConfigManager。

    player_required 等装饰器以前只读 handler.config_manager；不少 Handler
    只有 mgr 属性，导致恢复逻辑以 config_manager=None 计算气血上限，
    把玩家当前气血错误压到较低的回退上限。
    """
    manager = getattr(handler, "config_manager", None)
    if manager is not None:
        return manager
    try:
        values = vars(handler).values()
    except TypeError:
        return None
    for value in values:
        manager = getattr(value, "config_manager", None)
        if manager is not None:
            return manager
    return None


def with_player_lock(func: Callable[..., Coroutine[any, any, AsyncGenerator[any, None]]]):
    """给不经过 player_required 的“退出/解卡”指令也加上同一把玩家状态锁。"""

    @wraps(func)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        user_id = event.get_sender_id()
        # 先占锁再读档：避免锁外旧快照在锁内被整字段回写，覆盖其他指令的并发修改
        async with get_player_lock(user_id):
            player = await self.db.get_player_by_id(user_id)
            if not player:
                yield event.plain_result(f"道友尚未踏入仙途，请发送「{CMD_START_XIUXIAN}」开启你的旅程。")
                return
            try:
                from ..managers.recovery_manager import RecoveryManager
                recovery = None
                config_manager = _handler_config_manager(self)
                if config_manager is not None:
                    recovery = RecoveryManager(self.db, config_manager)
                    await recovery.apply_lazy_recovery(player)
                command_text = _normalize_command_text(event.get_message_str())
                if (
                    recovery is not None
                    and command_text.startswith(INJURED_BLOCKED_COMMANDS)
                    and await recovery.is_injured(player)
                ):
                    yield event.plain_result(
                        "❌ 你当前身受重伤，气血不足 20%，无法进行此项行动。\n"
                        "💡 请先 /调息 或服用回春散恢复气血。"
                    )
                    return
            except Exception:
                pass
            async for result in func(self, event, *args, **kwargs):
                yield result

    return wrapper


def idle_player_lock(func: Callable[..., Coroutine[any, any, AsyncGenerator[any, None]]]):
    """给内联写状态指令加玩家锁，并要求玩家处于空闲状态。"""

    @wraps(func)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        user_id = event.get_sender_id()
        async with get_player_lock(user_id):
            player = await self.db.get_player_by_id(user_id)
            if not player:
                yield event.plain_result(f"道友尚未踏入仙途，请发送「{CMD_START_XIUXIAN}」开启你的旅程。")
                return
            try:
                from ..managers.recovery_manager import RecoveryManager
                config_manager = _handler_config_manager(self)
                if config_manager is not None:
                    await RecoveryManager(
                        self.db, config_manager
                    ).apply_lazy_recovery(player)
            except Exception:
                pass
            user_cd = await self.db.ext.get_user_cd(player.user_id)
            busy = (user_cd and user_cd.type != UserStatus.IDLE) or (
                (not user_cd or user_cd.type == UserStatus.IDLE)
                and (getattr(player, "state", "") or "") == "修炼中"
            )
            if busy:
                status_name = UserStatus.get_name(user_cd.type) if user_cd and user_cd.type != UserStatus.IDLE else player.state
                yield event.plain_result(f"❌ 道友当前正「{status_name}」，无法进行此操作。")
                return
            async for result in func(self, event, *args, **kwargs):
                yield result

    return wrapper


def player_required(func: Callable[..., Coroutine[any, any, AsyncGenerator[any, None]]]):
    """
    一个装饰器，用于需要玩家登录才能执行的指令。
    它会自动检查玩家是否存在、状态是否空闲（特定指令除外），否则将玩家对象作为参数注入。
    同时检查贷款状态，如有贷款则显示还款提示。
    """
    @wraps(func)
    async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
        # self 是 Handler 类的实例 (e.g., PlayerHandler)
        user_id = event.get_sender_id()
        async with get_player_lock(user_id):
            # 锁内重新读档，避免旧快照覆盖并发修改
            player = await self.db.get_player_by_id(user_id)
            if not player:
                yield event.plain_result(f"道友尚未踏入仙途，请发送「{CMD_START_XIUXIAN}」开启你的旅程。")
                return

            injured = False
            try:
                from ..managers.recovery_manager import RecoveryManager
                config_manager = _handler_config_manager(self)
                if config_manager is not None:
                    recovery = RecoveryManager(self.db, config_manager)
                    await recovery.apply_lazy_recovery(player)
                    injured = await recovery.is_injured(player)
            except Exception:
                pass

            # 检查贷款状态并处理逾期
            loan_warning = await _check_loan_status(self.db, player)
            if loan_warning:
                if loan_warning.get("is_dead"):
                    # 玩家因逾期被追杀，删除数据
                    yield event.plain_result(loan_warning["message"])
                    return

            # 有灵石时自动偿还宗门欠款
            try:
                paid = await self.db.ext.settle_sect_debt(player.user_id)
                if paid:
                    player.gold = max(0, player.gold - paid)
                    player.sect_debt = max(0, int(getattr(player, "sect_debt", 0)) - paid)
            except Exception:
                pass

            message_text = event.get_message_str().strip()
            command_text = _normalize_command_text(message_text)
            bypass_busy = command_text.startswith(BUSY_BYPASS_COMMANDS)

            # 低气血受伤状态：禁止行动类指令
            if injured and command_text.startswith(INJURED_BLOCKED_COMMANDS):
                yield event.plain_result(
                    "❌ 你当前身受重伤，气血不足 20%，无法进行此项行动。\n"
                    "💡 请先 /调息 或服用回春散恢复气血。"
                )
                return

            # 欠款过多：暂停购买/消费类指令
            if (
                loan_warning
                and loan_warning.get("purchase_blocked")
                and command_text.startswith(PURCHASE_BLOCKED_COMMANDS)
            ):
                yield event.plain_result(
                    loan_warning.get("blocked_message")
                    or "🚫 欠款过多，购买类功能已暂停，请先 /还款。"
                )
                return

            # ===== 核心状态拦截判定 =====
            user_cd = await self.db.ext.get_user_cd(player.user_id)
            if not bypass_busy and user_cd and user_cd.type != UserStatus.IDLE:
                # 玩家处于忙碌状态（如闭关、历练、渡劫），检查命令是否在白名单中
                is_allowed = _is_command_allowed(message_text, BUSY_STATE_ALLOWED_COMMANDS)

                if not is_allowed:
                    status_name = UserStatus.get_name(user_cd.type)
                    yield event.plain_result(f"❌ 道友当前正在「{status_name}」，无法分心他顾。\n💡 可使用「我的信息」「签到」「银行」等基础指令。")
                    return

            # 状态检查：user_cd 缺失或为空闲时，用 player.state 兜底（兼容异常中断的闭关记录）
            if (
                not bypass_busy
                and (not user_cd or user_cd.type == UserStatus.IDLE)
                and player.state == "修炼中"
            ):
                # 允许闭关/出关自救，避免 user_cd 丢失后既不能重新闭关也不能出关
                if not command_text.startswith((CMD_START_CULTIVATION, CMD_END_CULTIVATION)):
                    is_allowed = _is_command_allowed(message_text, BUSY_STATE_ALLOWED_COMMANDS)
                    if not is_allowed:
                        yield event.plain_result(f"❌ 道友当前正在「{player.state}」中，无法分心他顾。\n💡 可使用「出关」「我的信息」「签到」等基础指令。")
                        return

            # 将 player 对象作为第一个参数传递给原始函数
            async for result in func(self, player, event, *args, **kwargs):
                yield result

            # 如果有贷款警告，在指令执行完后显示（自身已展示欠款信息的指令除外）
            if (
                loan_warning
                and loan_warning.get("warning_message")
                and not command_text.startswith(LOAN_SELF_DISPLAY_COMMANDS)
            ):
                # 指令可能已还款；只有当前仍然存在贷款时才追加提醒，避免显示还款前的旧状态
                current_loan = await self.db.ext.get_active_loan(user_id)
                if current_loan:
                    yield event.plain_result(loan_warning["warning_message"])

    return wrapper


# 贷款身份阈值（可按需要调整）
LOAN_IDENTITY_WARN = 10_000
LOAN_IDENTITY_HIGH = 50_000
LOAN_IDENTITY_BLOCK = 100_000

# 欠款过多时暂停的购买/消费类指令
PURCHASE_BLOCKED_COMMANDS = (
    "购买", "坊市购买", "黑市购买", "宗门兑换", "切原石",
    "拍卖会报名", "拍卖会入场",
)

# 这些指令自身已展示贷款身份/欠款信息，不再追加重复的还款提醒
LOAN_SELF_DISPLAY_COMMANDS = ("我的信息", "银行", "还款")


def _loan_identity(total_due: int, remaining_days: int) -> tuple[str, bool]:
    """根据欠款金额和剩余时间返回 (身份标签, 是否暂停购买)。"""
    if total_due >= LOAN_IDENTITY_BLOCK:
        return "🔴 老赖", True
    if remaining_days <= 1 and total_due >= LOAN_IDENTITY_HIGH:
        return "🔴 紧急欠款人", True
    if total_due >= LOAN_IDENTITY_HIGH:
        return "🟠 欠款大户", False
    if total_due >= LOAN_IDENTITY_WARN:
        return "🟡 负债修士", False
    return "🟢 信用良好", False


def _normalize_command_text(message_text: str) -> str:
    """把群聊消息归一化成纯命令文本，去掉 @、空白、零宽字符和前缀斜杠。"""
    clean_text = message_text.strip()

    # 去掉开头的方括号身份标记，例如 [主人(2448262042)]、[At:123]
    clean_text = re.sub(r"^(?:\[[^\]]*\]\s*)+", "", clean_text)
    # 去掉 @昵称
    clean_text = re.sub(r"^@\S+\s*", "", clean_text)

    # 去掉开头所有非文字字符：空格、/、零宽字符、标点等
    clean_text = re.sub(r"^[^\w\u4e00-\u9fa5]+", "", clean_text)
    return clean_text.strip()


def _is_command_allowed(message_text: str, allowed_commands: list) -> bool:
    """检查命令是否在允许列表中（完全匹配或“命令 + 空格”，避免黑市放行黑市购买）。"""
    clean_text = _normalize_command_text(message_text)
    for cmd in allowed_commands:
        if clean_text == cmd:
            return True
        if clean_text.startswith(cmd + " ") or clean_text.startswith(cmd + "\u3000"):
            return True
    return False


async def _check_loan_status(db, player: Player) -> dict:
    """检查玩家贷款状态"""
    try:
        loan = await db.ext.get_active_loan(player.user_id)
        if not loan:
            return None
        
        now = int(time.time())
        due_at = loan["due_at"]
        
        # 检查是否已逾期
        if now > due_at:
            # 使用事务保护，防止并发删除
            await db.conn.execute("BEGIN IMMEDIATE")
            try:
                # 重新检查贷款状态（overdue 同样属于未结清贷款）
                loan = await db.ext.get_active_loan(player.user_id)
                if not loan or loan["status"] not in ("active", "overdue"):
                    await db.conn.rollback()
                    return None
                
                if now <= loan["due_at"]:
                    await db.conn.rollback()
                    return None
                
                player_name = player.user_name or f"道友{player.user_id[:6]}"
                
                # 删除玩家（级联删除所有关联数据）
                await db.delete_player_cascade(player.user_id)
                # 级联删除已将未结清贷款标记为 bad_debt，无需再改回 overdue
                
                # 记录流水
                await db.ext.add_bank_transaction(
                    player.user_id, "bank_kill", 0, 0,
                    "逾期未还款，被银行追杀致死", now
                )
                
                await db.conn.commit()
                
                loan_type_name = "突破贷款" if loan["loan_type"] == "breakthrough" else "普通贷款"
                
                return {
                    "is_dead": True,
                    "message": (
                        f"💀 银行追杀令 💀\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"道友【{player_name}】因{loan_type_name}逾期未还\n"
                        f"欠款本金：{loan['principal']:,} 灵石\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"银行派出的追杀者已将你击杀！\n"
                        f"所有修为和装备化为虚无...\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"若想重新修仙，请使用「我要修仙」命令"
                    )
                }
            except Exception:
                await db.conn.rollback()
                raise
        
        # 计算剩余时间
        remaining_seconds = due_at - now
        remaining_days = remaining_seconds // 86400
        remaining_hours = (remaining_seconds % 86400) // 3600
        
        # 计算剩余应还金额（扣除已还利息/本金；利息仍按原本金计算）
        days_borrowed = max(1, (now - loan["borrowed_at"]) // 86400)
        accrued_interest = int(
            loan["principal"] * loan["interest_rate"] * days_borrowed
        )
        paid_interest = int(loan.get("paid_interest", 0) or 0)
        paid_principal = int(loan.get("paid_principal", 0) or 0)
        interest_due = max(0, accrued_interest - paid_interest)
        principal_due = max(0, int(loan["principal"]) - paid_principal)
        total_due = principal_due + interest_due
        
        loan_type_name = "突破贷款" if loan["loan_type"] == "breakthrough" else "普通贷款"
        
        # 根据剩余时间设置警告等级
        if remaining_days <= 0:
            urgency = "🔴 紧急"
            time_str = f"{remaining_hours} 小时"
        elif remaining_days <= 1:
            urgency = "🟠 警告"
            time_str = f"{remaining_days} 天 {remaining_hours} 小时"
        else:
            urgency = "🟡 提醒"
            time_str = f"{remaining_days} 天"
        
        identity_label, purchase_blocked = _loan_identity(total_due, remaining_days)
        blocked_message = (
            f"🚫 欠款过多，购买类功能已暂停。\n"
            f"欠款：{total_due:,} 灵石\n"
            f"身份：{identity_label}"
        )

        warning_message = (
            f"\n🏷️ 身份：{identity_label}（欠款 {total_due:,} 灵石，剩余 {time_str}）\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{urgency}【{loan_type_name}还款提醒】\n"
            f"应还金额：{total_due:,} 灵石\n"
            f"剩余时间：{time_str}"
        )
        if purchase_blocked:
            warning_message += "\n🚫 欠款过多，购买类功能已暂停。"

        return {
            "is_dead": False,
            "warning_message": warning_message,
            "purchase_blocked": purchase_blocked,
            "blocked_message": blocked_message,
        }
        
    except Exception:
        return None