# managers/auto_proxy_manager.py
"""自动代理：无人值守结算并续接不需要用户选择的活动（押镖 / 宗门任务）"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional, Tuple

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 测试/非 AstrBot 环境
    import logging

    logger = logging.getLogger("xiuxian.auto_proxy")

from ..data.data_manager import DataBase
from ..models_extended import UserStatus

__all__ = ["AutoProxyManager"]

TASK_ESCORT = "escort"
TASK_SECT = "sect_task"

TASK_NAMES = {
    TASK_ESCORT: "押镖",
    TASK_SECT: "宗门任务",
}

TASK_ALIASES = {
    "押镖": TASK_ESCORT,
    "运镖": TASK_ESCORT,
    "escort": TASK_ESCORT,
    "sect": TASK_SECT,
    "sect_task": TASK_SECT,
    "宗门": TASK_SECT,
    "宗门任务": TASK_SECT,
}

TASK_STATUS = {
    TASK_ESCORT: UserStatus.DELIVERING,
    TASK_SECT: UserStatus.SECT_TASK,
}

STATUS_TASK = {status: task for task, status in TASK_STATUS.items()}


class AutoProxyManager:
    """管理自动代理开关，并在后台周期内完成“结算当前轮 → 开始下一轮”。

    设计约束：
    - 只处理 user_cd 中登记为押镖/宗门任务的连续活动；
    - 玩家处于其他忙碌状态时自动暂停，空闲后再续接；
    - 手动放弃/解卡会由命令层调用 note_manual_exit 关闭代理；
    - 自动开始失败（无宗门、境界不足、被删除等）时关闭代理并通知。
    """

    SCAN_INTERVAL = 15

    def __init__(
        self,
        db: DataBase,
        escort_mgr: Any,
        sect_mgr: Any,
        notifier: Optional[Callable[[str, str], Awaitable[None]]] = None,
        title_bump: Optional[Callable[[str, str, int], Awaitable[str]]] = None,
        bounty_mgr: Any = None,
        lock_getter: Optional[Callable[[str], asyncio.Lock]] = None,
    ):
        self.db = db
        self.escort_mgr = escort_mgr
        self.sect_mgr = sect_mgr
        self.notifier = notifier
        self.title_bump = title_bump
        self.bounty_mgr = bounty_mgr
        self._lock_getter = lock_getter
        self._fallback_locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, user_id: str) -> asyncio.Lock:
        if self._lock_getter:
            return self._lock_getter(user_id)
        lock = self._fallback_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._fallback_locks[user_id] = lock
        return lock

    # ===== 基础状态 =====

    @staticmethod
    def normalize_task(raw: str) -> Optional[str]:
        key = (raw or "").strip().lower()
        if not key:
            return None
        return TASK_ALIASES.get(key) or TASK_ALIASES.get((raw or "").strip())

    @staticmethod
    def task_name(task_type: str) -> str:
        return TASK_NAMES.get(task_type, "未知活动")

    @staticmethod
    def _legacy_busy(player: Any) -> bool:
        """user_cd 缺失时，用 players.state 兜底判断是否忙碌。"""
        state = (getattr(player, "state", "") or "").strip()
        return bool(state and state != "空闲")

    async def get(self, user_id: str) -> Optional[dict]:
        return await self.db.ext.get_auto_proxy(user_id)

    async def is_enabled(self, user_id: str) -> bool:
        row = await self.get(user_id)
        return bool(row and row.get("enabled"))

    async def _write_state(
        self,
        user_id: str,
        enabled: bool,
        task_type: str,
        origin: str,
        last_error: str = "",
    ):
        await self.db.ext.upsert_auto_proxy(
            user_id,
            1 if enabled else 0,
            task_type,
            origin,
            last_error,
        )

    async def disable(self, user_id: str, reason: str = "") -> bool:
        """关闭代理；保留上次活动类型，方便下次一键恢复。"""
        row = await self.get(user_id)
        was_enabled = bool(row and row.get("enabled"))
        await self._write_state(
            user_id,
            False,
            (row or {}).get("task_type", ""),
            "",
            reason,
        )
        return was_enabled

    async def note_manual_exit(self, user_id: str) -> bool:
        """用户手动退出/放弃时调用；返回本次是否真的关闭了代理。"""
        return await self.disable(user_id, "manual_exit")

    async def note_external_activity_started(self, user_id: str) -> bool:
        """玩家开始代理不接管的活动时立即关闭代理。"""
        row = await self.get(user_id)
        if not row or not row.get("enabled"):
            return False
        cd = await self.db.ext.get_user_cd(user_id)
        if not cd or cd.type in (UserStatus.IDLE, UserStatus.DELIVERING, UserStatus.SECT_TASK):
            return False
        return await self.disable(user_id, "other_busy")

    async def note_activity_started(self, user_id: str, origin: str = "") -> Optional[str]:
        """玩家手动开始受支持活动后绑定当前活动；仅代理开启时生效。"""
        row = await self.get(user_id)
        if not row or not row.get("enabled"):
            return None
        cd = await self.db.ext.get_user_cd(user_id)
        current = STATUS_TASK.get(cd.type) if cd else None
        if not current:
            return None
        await self._write_state(
            user_id,
            True,
            current,
            origin or row.get("origin", ""),
            "",
        )
        return current

    # ===== 命令入口 =====

    async def enable(self, user_id: str, origin: str = "", task_type: str = "") -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 道友尚未踏入仙途，请先发送「我要修仙」。"

        raw_task = (task_type or "").strip()
        requested = self.normalize_task(raw_task) if raw_task else None
        if raw_task and not requested:
            return False, (
                "❌ 暂不支持该活动。\n"
                "💡 目前自动代理支持：/自动代理 押镖、/自动代理 宗门任务"
            )

        row = await self.get(user_id) or {}
        was_enabled = bool(row.get("enabled"))
        cd = await self.db.ext.get_user_cd(user_id)
        current = STATUS_TASK.get(cd.type) if cd else None
        origin = origin or row.get("origin", "")
        legacy_busy = (not cd) and self._legacy_busy(player)
        busy_other = bool((cd and cd.type != UserStatus.IDLE and not current) or legacy_busy)
        busy_name = UserStatus.get_name(cd.type) if cd else (getattr(player, "state", "") or "忙碌")
        if busy_other:
            return False, (
                f"❌ 你当前正在{busy_name}，自动代理只能接管押镖/宗门任务。\n"
                f"💡 请空闲后再发送 /自动代理。"
            )

        preferred = requested or current or row.get("task_type", "")
        await self._write_state(user_id, True, preferred, origin)

        if current:
            if requested and requested != current:
                return True, (
                    f"🤖 自动代理已开启\n"
                    f"当前正在{UserStatus.get_name(cd.type)}，先完成本轮；"
                    f"之后将自动改跑【{self.task_name(requested)}】。\n"
                    f"💡 发送 /关闭代理 可随时停止。"
                )
            return True, (
                f"🤖 自动代理已开启\n"
                f"已接管当前【{self.task_name(current)}】，本轮结束后会自动结算并开始下一轮。\n"
                f"💡 发送 /关闭代理 可随时停止。"
            )

        if not preferred:
            tip = "🤖 自动代理已在运行中" if was_enabled else "🤖 自动代理已开启"
            return True, (
                f"{tip}\n"
                f"请先发送 /押镖 或 /宗门任务 开始第一轮，之后会自动续接。\n"
                f"💡 也可直接使用 /自动代理 押镖 或 /自动代理 宗门任务。\n"
                f"发送 /关闭代理 可随时停止。"
            )

        ok, start_msg = await self.start_activity(player, preferred)
        if not ok:
            await self._write_state(user_id, False, preferred, origin, "start_failed")
            return False, f"❌ 自动代理启动【{self.task_name(preferred)}】失败：\n{start_msg}"

        return True, (
            f"🤖 自动代理已开启，并已开始第一轮【{self.task_name(preferred)}】\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{start_msg}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"♻️ 每轮结束后会自动结算并开始下一轮，发送 /关闭代理 可停止。"
        )

    async def status_text(self, user_id: str) -> str:
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return "❌ 道友尚未踏入仙途，请先发送「我要修仙」。"

        row = await self.get(user_id) or {}
        cd = await self.db.ext.get_user_cd(user_id)
        current = STATUS_TASK.get(cd.type) if cd else None

        if not row.get("enabled"):
            last = row.get("task_type")
            suffix = f"\n上次代理活动：{self.task_name(last)}" if last else ""
            return (
                "🤖 自动代理：未开启\n"
                "━━━━━━━━━━━━━━━\n"
                "发送 /自动代理 开启；可接 /自动代理 押镖 或 /自动代理 宗门任务 直接指定。"
                f"{suffix}"
            )

        lines = [
            "🤖 自动代理：运行中",
            "━━━━━━━━━━━━━━━",
            f"目标活动：{self.task_name(row.get('task_type')) if row.get('task_type') else '等待选择'}",
        ]
        if current:
            if cd and cd.scheduled_time:
                remaining = max(0, cd.scheduled_time - int(time.time()))
                lines.append(
                    f"当前进行：{self.task_name(current)}（剩余 {remaining // 60} 分 {remaining % 60} 秒）"
                )
            else:
                lines.append(f"当前进行：{self.task_name(current)}")
            if row.get("task_type") and row.get("task_type") != current:
                lines.append(f"下一轮目标：{self.task_name(row.get('task_type'))}")
        elif cd and cd.type != UserStatus.IDLE:
            lines.append(f"当前状态：{UserStatus.get_name(cd.type)}（非代理活动，将自动关闭）")
        elif not cd and self._legacy_busy(player):
            lines.append(f"当前状态：{player.state}（非代理活动，将自动关闭）")
        else:
            lines.append("当前状态：空闲（等待下一轮）")

        if row.get("last_error") == "start_failed":
            lines.append("⚠️ 上次自动开始失败，已暂停")
        lines.append("💡 发送 /关闭代理 可停止自动代理")
        return "\n".join(lines)

    # ===== 活动执行 =====

    async def start_activity(self, player: Any, task_type: str) -> Tuple[bool, str]:
        if task_type == TASK_ESCORT:
            ok, msg, _ = await self.escort_mgr.start_escort(player)
            return bool(ok), msg
        if task_type == TASK_SECT:
            ok, msg = await self.sect_mgr.perform_sect_task(player.user_id)
            return bool(ok), msg
        return False, "❌ 暂不支持该活动。"

    async def finish_activity(self, player: Any, task_type: str) -> Tuple[bool, str]:
        if task_type == TASK_ESCORT:
            ok, msg = await self.escort_mgr.finish_escort(player)
            if ok:
                try:
                    await self.db.ext.add_task_progress(player.user_id, "escort", 1)
                except Exception:
                    pass
                if self.title_bump:
                    try:
                        await self.title_bump(player.user_id, "escort_count", 1)
                    except Exception:
                        pass
                if self.bounty_mgr:
                    try:
                        has_progress, bounty_msg = await self.bounty_mgr.add_bounty_progress(
                            player, "escort", 1
                        )
                        if has_progress:
                            msg += bounty_msg
                    except Exception:
                        pass
            return bool(ok), msg
        if task_type == TASK_SECT:
            ok, msg = await self.sect_mgr.finish_sect_task(player.user_id)
            if ok:
                try:
                    await self.db.ext.add_task_progress(player.user_id, "sect_task", 1)
                except Exception:
                    pass
                if self.bounty_mgr:
                    try:
                        has_progress, bounty_msg = await self.bounty_mgr.add_bounty_progress(
                            player, "sect", 1
                        )
                        if has_progress:
                            msg += bounty_msg
                    except Exception:
                        pass
            return bool(ok), msg
        return False, "❌ 暂不支持该活动。"

    # ===== 后台处理 =====

    async def _notify(self, origin: str, msg: str):
        if not origin or not self.notifier:
            return
        try:
            await self.notifier(origin, msg)
        except Exception as e:
            logger.warning(f"【自动代理】消息发送失败: {e}")

    async def process_due(self, now: Optional[int] = None) -> int:
        """扫描并处理到期的代理；返回本次执行的轮次数。"""
        now = int(now or time.time())
        try:
            rows = await self.db.ext.list_enabled_auto_proxies()
        except Exception as e:
            logger.warning(f"【自动代理】读取代理列表失败: {e}")
            return 0

        processed = 0
        for row in rows:
            user_id = str(row.get("user_id") or "")
            if not user_id:
                continue
            try:
                async with self._get_lock(user_id):
                    if await self._process_one(user_id, now):
                        processed += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"【自动代理】处理玩家 {user_id} 失败: {e}")
        return processed

    async def _process_one(self, user_id: str, now: int) -> bool:
        row = await self.get(user_id)
        if not row or not row.get("enabled"):
            return False

        origin = row.get("origin", "")
        player = await self.db.get_player_by_id(user_id)
        if not player:
            await self.db.ext.delete_auto_proxy(user_id)
            return False

        cd = await self.db.ext.get_user_cd(user_id)
        cd_type = cd.type if cd else UserStatus.IDLE
        current = STATUS_TASK.get(cd_type)

        if current:
            if cd and cd.scheduled_time and now < cd.scheduled_time:
                return False

            ok, finish_msg = await self.finish_activity(player, current)
            if not ok:
                await self._notify(
                    origin,
                    f"⚠️ 自动代理结算【{self.task_name(current)}】失败：\n{finish_msg}\n"
                    f"🛑 自动代理已暂停，发送 /自动代理 可重试。",
                )
                await self.disable(user_id, "finish_failed")
                return True

            # task_type 是“下一轮目标”：玩家可能在本轮进行中主动改绑了活动
            next_task = row.get("task_type") or current
            fresh_player = await self.db.get_player_by_id(user_id) or player
            ok, start_msg = await self.start_activity(fresh_player, next_task)
            if not ok:
                await self._notify(
                    origin,
                    f"{finish_msg}\n\n"
                    f"⚠️ 自动代理无法开始下一轮【{self.task_name(next_task)}】：\n{start_msg}\n"
                    f"🛑 自动代理已关闭，发送 /自动代理 可重新开启。",
                )
                await self._write_state(user_id, False, next_task, origin, "start_failed")
                return True

            await self._notify(
                origin,
                f"{finish_msg}\n\n"
                f"🤖 自动代理：已开始下一轮【{self.task_name(next_task)}】\n"
                f"{start_msg}",
            )
            return True

        if cd_type != UserStatus.IDLE:
            # 玩家进入了闭关/历练等代理不接管的活动：按约定自动关闭代理
            await self._notify(
                origin,
                f"🛑 检测到你进入了{UserStatus.get_name(cd_type)}，"
                f"自动代理已关闭。发送 /自动代理 可重新开启。",
            )
            await self.disable(user_id, "other_busy")
            return True

        if not cd and self._legacy_busy(player):
            # 兼容旧数据：user_cd 未建立但 players.state 仍显示忙碌
            await self._notify(
                origin,
                f"🛑 检测到你进入了{player.state}，自动代理已关闭。"
                f"发送 /自动代理 可重新开启。",
            )
            await self.disable(user_id, "other_busy")
            return True

        task_type = row.get("task_type", "")
        if not task_type:
            return False

        ok, start_msg = await self.start_activity(player, task_type)
        if not ok:
            await self._notify(
                origin,
                f"⚠️ 自动代理无法开始【{self.task_name(task_type)}】：\n{start_msg}\n"
                f"🛑 自动代理已关闭，发送 /自动代理 可重新开启。",
            )
            await self._write_state(user_id, False, task_type, origin, "start_failed")
            return True

        await self._notify(
            origin,
            f"🤖 自动代理：已开始新一轮【{self.task_name(task_type)}】\n{start_msg}",
        )
        return True
