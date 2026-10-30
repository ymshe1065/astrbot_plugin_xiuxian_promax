# managers/auto_proxy_manager.py
"""自动代理：无人值守结算并续接不需要用户选择的活动（押镖 / 宗门任务 / 秘境）"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable, Optional, Tuple

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 测试/非 AstrBot 环境
    import logging

    logger = logging.getLogger("xiuxian.auto_proxy")

from ..data.data_manager import DataBase
from ..models_extended import UserStatus
from ..utils.arg_parse import PLOT_ALL_KEYWORDS, is_all_keyword, normalize_arg_text

__all__ = ["AutoProxyManager"]

TASK_ESCORT = "escort"
TASK_SECT = "sect_task"
TASK_FARM = "farm"
TASK_RIFT = "rift"
TASK_FERTILIZE = "fertilize"

TASK_NAMES = {
    TASK_ESCORT: "押镖",
    TASK_SECT: "宗门任务",
    TASK_FARM: "灵田",
    TASK_RIFT: "秘境",
    TASK_FERTILIZE: "施肥",
}

TASK_ALIASES = {
    "押镖": TASK_ESCORT,
    "运镖": TASK_ESCORT,
    "escort": TASK_ESCORT,
    "sect": TASK_SECT,
    "sect_task": TASK_SECT,
    "宗门": TASK_SECT,
    "宗门任务": TASK_SECT,
    "灵田": TASK_FARM,
    "farm": TASK_FARM,
    "秘境": TASK_RIFT,
    "探索秘境": TASK_RIFT,
    "rift": TASK_RIFT,
    "施肥": TASK_FERTILIZE,
    "fertilize": TASK_FERTILIZE,
}

# 施肥是“维护动作”而不是“活动”，不占用 UserStatus，故不进 TASK_STATUS
DEFAULT_FERTILIZE_THRESHOLD = 0.85
DEFAULT_FERTILIZE_MIN_ROI = 1.0
# 单轮最多补几次肥（每次 +30% 上限，4 次足以从地板补满）
MAX_FERTILIZE_TIMES = 4

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
        farm_mgr: Any = None,
        rift_mgr: Any = None,
    ):
        self.db = db
        self.escort_mgr = escort_mgr
        self.sect_mgr = sect_mgr
        self.notifier = notifier
        self.title_bump = title_bump
        self.bounty_mgr = bounty_mgr
        self._lock_getter = lock_getter
        self.farm_mgr = farm_mgr
        self.rift_mgr = rift_mgr
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

    @staticmethod
    def _load_config(row: Optional[dict]) -> dict:
        raw = (row or {}).get("config_json") or "{}"
        try:
            config = json.loads(raw)
        except (TypeError, ValueError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        farm = config.get("farm")
        if not isinstance(farm, dict):
            farm = {}
        farm.setdefault("enabled", False)
        farm.setdefault("plots", None)
        farm.setdefault("exclude", [])
        farm.setdefault("tasks", {})
        config["farm"] = farm

        rift = config.get("rift")
        if not isinstance(rift, dict):
            rift = {}
        rift.setdefault("enabled", False)
        rift.setdefault("rift_id", 0)
        config["rift"] = rift

        fert = config.get("fertilize")
        if not isinstance(fert, dict):
            fert = {}
        fert.setdefault("enabled", False)
        fert.setdefault("threshold", DEFAULT_FERTILIZE_THRESHOLD)
        fert.setdefault("min_roi", DEFAULT_FERTILIZE_MIN_ROI)
        fert.setdefault("note", "")
        config["fertilize"] = fert
        return config

    @classmethod
    def _farm_enabled(cls, row: Optional[dict]) -> bool:
        return bool(cls._load_config(row)["farm"].get("enabled"))

    @classmethod
    def _rift_enabled(cls, row: Optional[dict]) -> bool:
        return bool(cls._load_config(row)["rift"].get("enabled"))

    @classmethod
    def _fert_enabled(cls, row: Optional[dict]) -> bool:
        return bool(cls._load_config(row)["fertilize"].get("enabled"))

    @staticmethod
    def _is_fert_request(raw: str) -> bool:
        parts = (raw or "").strip().split()
        if not parts:
            return False
        key = parts[0].lower()
        if key in ("施肥", "施肥代理", "fertilize", "fert", "肥"):
            return True
        # 兼容粘连写法：/自动代理 施肥70
        return key.startswith("施肥") or key.startswith("fertilize")

    @staticmethod
    def _is_farm_request(raw: str) -> bool:
        parts = (raw or "").strip().split()
        if not parts:
            return False
        key = parts[0].lower()
        return key in ("灵田", "farm", "田")

    @staticmethod
    def _is_rift_request(raw: str) -> bool:
        parts = (raw or "").strip().split()
        if not parts:
            return False
        key = parts[0].lower()
        if key in ("秘境", "探索秘境", "rift", "秘"):
            return True
        # 兼容粘连写法：/自动代理 秘境1
        return key.startswith("秘境") or key.startswith("rift")

    @staticmethod
    def _parse_plot_selection(raw: str) -> Tuple[Optional[list[int]], list[str]]:
        """解析格号选择：空=全部；支持 1 2 / 1,2 / 1-3；全部=全部。"""
        tokens = [t for t in re.split(r"[\s,，、]+", normalize_arg_text(raw)) if t]
        if not tokens:
            return None, []
        plots: list[int] = []
        invalid: list[str] = []
        for token in tokens:
            if is_all_keyword(token, PLOT_ALL_KEYWORDS):
                return None, []
            range_match = re.match(r"^(\d+)-(\d+)$", token)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2))
                if start <= 0 or end <= 0:
                    invalid.append(token)
                    continue
                if start > end:
                    start, end = end, start
                plots.extend(range(start, end + 1))
                continue
            if token.isdigit():
                value = int(token)
                if value <= 0:
                    invalid.append(token)
                else:
                    plots.append(value)
                continue
            invalid.append(token)
        return sorted(set(plots)), invalid

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
        config: Optional[dict] = None,
    ):
        if config is None:
            row = await self.get(user_id)
            config = self._load_config(row)
        await self.db.ext.upsert_auto_proxy(
            user_id,
            1 if enabled else 0,
            task_type,
            origin,
            last_error,
            json.dumps(config, ensure_ascii=False),
        )

    async def disable(self, user_id: str, reason: str = "") -> bool:
        """关闭全部代理；保留上次活动类型、灵田任务、秘境目标与施肥阈值，方便下次恢复。"""
        row = await self.get(user_id)
        was_enabled = bool(row and row.get("enabled"))
        config = self._load_config(row)
        config["farm"]["enabled"] = False
        config["rift"]["enabled"] = False
        config["fertilize"]["enabled"] = False
        await self._write_state(
            user_id,
            False,
            (row or {}).get("task_type", ""),
            "",
            reason,
            config,
        )
        return was_enabled

    async def disable_primary(self, user_id: str, reason: str = "manual_close") -> Tuple[bool, str]:
        """只关闭押镖/宗门任务代理，灵田/秘境/施肥自动代理不受影响。"""
        row = await self.get(user_id)
        if not row or not row.get("enabled"):
            return False, "💡 自动代理当前未开启。"
        config = self._load_config(row)
        farm_on = bool(config["farm"].get("enabled"))
        rift_on = bool(config["rift"].get("enabled"))
        fert_on = bool(config["fertilize"].get("enabled"))
        had_primary = bool(row.get("task_type"))
        await self._disable_primary(user_id, reason)
        if farm_on or rift_on or fert_on:
            tip = "押镖/宗门任务代理已关闭" if had_primary else "押镖/宗门任务代理未开启"
            keep = "、".join(
                name
                for name, on in (("灵田", farm_on), ("秘境", rift_on), ("施肥", fert_on))
                if on
            )
            return True, (
                f"🛑 {tip}；{keep}自动代理继续运行。\n"
                "💡 发送 /关闭代理 灵田 /关闭代理 秘境 /关闭代理 施肥 可单独停止；发送 /关闭代理 可全部停止。"
            )
        return True, (
            "🛑 自动代理已关闭。\n"
            "当前正在进行的押镖/宗门任务不会中断，可继续手动结算或放弃。"
        )

    async def _disable_primary(self, user_id: str, reason: str) -> bool:
        """只关闭押镖/宗门任务代理；灵田/秘境/施肥代理继续运行。"""
        row = await self.get(user_id)
        if not row:
            return False
        config = self._load_config(row)
        if (
            config["farm"].get("enabled")
            or config["rift"].get("enabled")
            or config["fertilize"].get("enabled")
        ):
            await self._write_state(
                user_id,
                True,
                "",
                row.get("origin", ""),
                reason,
                config,
            )
        else:
            await self.disable(user_id, reason)
        return True

    async def note_manual_exit(self, user_id: str) -> bool:
        """用户手动退出/放弃时调用；主动放弃秘境会同时关闭秘境代理，灵田代理不受影响。"""
        row = await self.get(user_id)
        if not row or not row.get("enabled"):
            return False
        config = self._load_config(row)
        if config["rift"].get("enabled"):
            config["rift"]["enabled"] = False
            keep_enabled = bool(row.get("task_type")) or bool(
                config["farm"].get("enabled")
            ) or bool(config["fertilize"].get("enabled"))
            await self._write_state(
                user_id,
                keep_enabled,
                row.get("task_type", ""),
                row.get("origin", ""),
                "manual_exit",
                config,
            )
            return True
        return await self._disable_primary(user_id, "manual_exit")

    async def note_external_activity_started(self, user_id: str) -> bool:
        """玩家开始代理不接管的活动时关闭押镖/宗门任务代理。"""
        row = await self.get(user_id)
        if not row or not row.get("enabled"):
            return False
        farm_on = self._farm_enabled(row)
        rift_on = self._rift_enabled(row)
        fert_on = self._fert_enabled(row)
        if not (row.get("task_type") or "") and (farm_on or rift_on or fert_on):
            return False
        cd = await self.db.ext.get_user_cd(user_id)
        if not cd or cd.type in (UserStatus.IDLE, UserStatus.DELIVERING, UserStatus.SECT_TASK):
            return False
        # 玩家手动进入秘境：由秘境代理接管续接，不能当成“其他活动”关掉
        if cd.type == UserStatus.EXPLORING and rift_on:
            return False
        return await self._disable_primary(user_id, "other_busy")

    async def note_activity_started(self, user_id: str, origin: str = "") -> Optional[str]:
        """玩家手动开始受支持活动后绑定当前活动；仅代理开启时生效。"""
        row = await self.get(user_id)
        if not row or not row.get("enabled"):
            return None
        # 仅开启灵田/秘境/施肥代理时，不接管玩家手动开始的押镖/宗门任务；
        # 否则会把灵田/秘境/施肥代理悄悄改绑成押镖/宗门任务代理。
        if not (row.get("task_type") or "") and (
            self._farm_enabled(row)
            or self._rift_enabled(row)
            or self._fert_enabled(row)
        ):
            return None
        cd = await self.db.ext.get_user_cd(user_id)
        current = STATUS_TASK.get(cd.type) if cd else None
        if not current:
            return None
        config = self._load_config(row)
        await self._write_state(
            user_id,
            True,
            current,
            origin or row.get("origin", ""),
            "",
            config,
        )
        return current

    # ===== 灵田代理 =====

    @staticmethod
    def _farm_plots_text(plots: Optional[list[int]]) -> str:
        if plots is None:
            return "全部格"
        if not plots:
            return "无"
        return "、".join(f"格{s}" for s in plots)

    def _selected_slots(self, farm: dict, farm_cfg: dict) -> list[int]:
        max_slots = self.farm_mgr.get_max_slots(farm) if self.farm_mgr else 0
        raw_plots = farm_cfg.get("plots")
        if raw_plots is None:
            slots = list(range(1, max_slots + 1))
        else:
            slots = []
            seen = set()
            for value in raw_plots or []:
                try:
                    slot = int(value)
                except (TypeError, ValueError):
                    continue
                if 1 <= slot <= max_slots and slot not in seen:
                    seen.add(slot)
                    slots.append(slot)
        excluded = set()
        for value in farm_cfg.get("exclude") or []:
            try:
                excluded.add(int(value))
            except (TypeError, ValueError):
                continue
        return sorted(s for s in slots if s not in excluded)

    async def _enable_farm(self, user_id: str, origin: str, raw_task: str) -> Tuple[bool, str]:
        if not self.farm_mgr:
            return False, "❌ 灵田系统未加载，暂时无法代理灵田。"
        farm = await self.farm_mgr.get_user_farm(user_id)
        if not farm:
            return False, "❌ 你还没有灵田，请先使用 /开垦灵田。"

        parts = raw_task.strip().split(maxsplit=1)
        rest = parts[1] if len(parts) > 1 else ""
        plots, invalid = self._parse_plot_selection(rest)
        if invalid:
            return False, (
                f"❌ 无法识别的格子：{'、'.join(invalid)}\n"
                "💡 用法：/自动代理 灵田 [格号...]，例如 /自动代理 灵田 1 2 或 /自动代理 灵田 1-3"
            )
        max_slots = self.farm_mgr.get_max_slots(farm)
        if plots:
            over = [s for s in plots if s > max_slots]
            if over:
                return False, f"❌ 灵田当前只有 {max_slots} 格，无法代理格{over[0]}。"

        row = await self.get(user_id) or {}
        config = self._load_config(row)
        farm_cfg = config["farm"]
        farm_cfg["enabled"] = True
        farm_cfg["plots"] = plots
        farm_cfg["exclude"] = []
        farm_cfg["tasks"] = farm_cfg.get("tasks") if isinstance(farm_cfg.get("tasks"), dict) else {}
        origin = origin or row.get("origin", "")
        # 只保留当前正在进行的押镖/宗门任务，避免旧的 task_type 在仅开启灵田代理时被自动续接
        cd = await self.db.ext.get_user_cd(user_id)
        active_task = STATUS_TASK.get(cd.type) if cd else ""
        await self._write_state(user_id, True, active_task or "", origin, "", config)

        changed, cycle_msg, config_dirty = await self._run_farm_cycle(user_id, await self.db.get_player_by_id(user_id), config)
        if changed or config_dirty:
            await self._write_state(user_id, True, active_task or "", origin, "", config)

        header = (
            "🤖 灵田代理已开启\n"
            "━━━━━━━━━━━━━━━\n"
            f"代理范围：{self._farm_plots_text(plots)}\n"
            "♻️ 成熟后自动收获，并按原格续种；杂交植株需有足够种苗才会续种。\n"
            "💡 肥力不足会打折，可另开 /自动代理 施肥 让代理按阈值补肥。"
        )
        if cycle_msg:
            header += f"\n━━━━━━━━━━━━━━━\n{cycle_msg}"
        else:
            header += "\n💡 当前没有可收获/续种的灵草，成熟后会自动处理。"
        return True, header

    async def disable_farm(self, user_id: str, plots_text: str = "") -> Tuple[bool, str]:
        row = await self.get(user_id)
        if not row:
            return False, "❌ 道友尚未踏入仙途。"
        config = self._load_config(row)
        farm_cfg = config["farm"]
        if not farm_cfg.get("enabled"):
            return False, "💡 灵田代理当前未开启。"

        plots, invalid = self._parse_plot_selection(plots_text)
        if invalid:
            return False, (
                f"❌ 无法识别的格子：{'、'.join(invalid)}\n"
                "💡 用法：/关闭代理 灵田 [格号...]"
            )

        if plots_text.strip() and plots is not None:
            farm = await self.farm_mgr.get_user_farm(user_id)
            if not farm:
                return False, "❌ 你还没有灵田。"
            current = self._selected_slots(farm, farm_cfg)
            removed = [s for s in current if s in set(plots)]
            remain = [s for s in current if s not in set(plots)]
            if not removed:
                return False, f"💡 {self._farm_plots_text(plots)} 不在当前灵田代理范围内。"
            tasks = farm_cfg.get("tasks") if isinstance(farm_cfg.get("tasks"), dict) else {}
            for slot in removed:
                tasks.pop(str(slot), None)
            farm_cfg["tasks"] = tasks
            if farm_cfg.get("plots") is None:
                excluded = set(farm_cfg.get("exclude") or [])
                excluded.update(plots)
                farm_cfg["exclude"] = sorted(int(s) for s in excluded)
            else:
                farm_cfg["plots"] = remain
                farm_cfg["exclude"] = []
            if not remain:
                farm_cfg["enabled"] = False
            action = f"已取消 {self._farm_plots_text(removed)} 的灵田代理"
            if farm_cfg["enabled"]:
                action += f"，继续代理 {self._farm_plots_text(remain)}。"
            else:
                action += "，灵田代理已关闭。"
        else:
            farm_cfg["enabled"] = False
            farm_cfg["plots"] = None
            farm_cfg["exclude"] = []
            farm_cfg["tasks"] = {}
            action = "灵田代理已关闭。"

        keep_enabled = bool(row.get("task_type")) or bool(farm_cfg.get("enabled")) or bool(
            config["fertilize"].get("enabled")
        )
        await self._write_state(
            user_id,
            keep_enabled,
            row.get("task_type", ""),
            row.get("origin", ""),
            row.get("last_error", ""),
            config,
        )
        return True, f"🛑 {action}"

    async def note_farm_planted(self, user_id: str) -> bool:
        """手动种植后同步续种任务，确保代理成熟后按原格补种。"""
        row = await self.get(user_id)
        if not row or not self._farm_enabled(row) or not self.farm_mgr:
            return False
        config = self._load_config(row)
        farm_cfg = config["farm"]
        farm = await self.farm_mgr.get_user_farm(user_id)
        if not farm:
            return False
        slots = set(self._selected_slots(farm, farm_cfg))
        tasks = farm_cfg.get("tasks") if isinstance(farm_cfg.get("tasks"), dict) else {}
        changed = False
        from .spirit_farm_manager import HYBRID_MARKER
        for crop in farm.get("crops", []):
            slot = int(crop.get("slot") or 0)
            herb_name = crop.get("name")
            if herb_name == HYBRID_MARKER:
                continue
            if slot in slots and herb_name and tasks.get(str(slot)) != herb_name:
                tasks[str(slot)] = herb_name
                changed = True
        if not changed:
            return False
        farm_cfg["tasks"] = tasks
        await self._write_state(
            user_id,
            True,
            row.get("task_type", ""),
            row.get("origin", ""),
            row.get("last_error", ""),
            config,
        )
        return True

    async def clear_farm_plot(self, user_id: str, slot: int) -> bool:
        """手动铲除某格后，中止该格的自动续种，避免立刻被种回。"""
        row = await self.get(user_id)
        if not row or not self._farm_enabled(row):
            return False
        config = self._load_config(row)
        farm_cfg = config["farm"]
        if not self.farm_mgr:
            return False
        farm = await self.farm_mgr.get_user_farm(user_id)
        if not farm:
            return False
        current = self._selected_slots(farm, farm_cfg)
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return False
        if slot not in current:
            return False
        remain = [s for s in current if s != slot]
        tasks = farm_cfg.get("tasks") if isinstance(farm_cfg.get("tasks"), dict) else {}
        tasks.pop(str(slot), None)
        farm_cfg["tasks"] = tasks
        if farm_cfg.get("plots") is None:
            excluded = set(farm_cfg.get("exclude") or [])
            excluded.add(slot)
            farm_cfg["exclude"] = sorted(int(s) for s in excluded)
        else:
            farm_cfg["plots"] = remain
            farm_cfg["exclude"] = []
        if not remain:
            farm_cfg["enabled"] = False
        keep_enabled = bool(row.get("task_type")) or bool(farm_cfg.get("enabled"))
        await self._write_state(
            user_id,
            keep_enabled,
            row.get("task_type", ""),
            row.get("origin", ""),
            row.get("last_error", ""),
            config,
        )
        return True

    async def _run_farm_cycle(
        self, user_id: str, player: Any, config: dict
    ) -> Tuple[bool, str, bool]:
        """执行一次灵田收获与续种；返回 (是否发生动作, 消息, 配置是否变化)。"""
        if not self.farm_mgr or not player:
            return False, "", False
        farm_cfg = config["farm"]
        farm = await self.farm_mgr.get_user_farm(user_id)
        if not farm:
            farm_cfg["enabled"] = False
            return True, "⚠️ 灵田已不存在，灵田代理已关闭。", True

        slots = self._selected_slots(farm, farm_cfg)
        if not slots:
            farm_cfg["enabled"] = False
            return True, "⚠️ 没有可代理的灵田格，灵田代理已关闭。", True

        selected = set(slots)
        tasks = farm_cfg.get("tasks") if isinstance(farm_cfg.get("tasks"), dict) else {}
        farm_cfg["tasks"] = tasks
        now = int(time.time())
        harvest_slots = []
        config_dirty = False

        from .spirit_farm_manager import HYBRID_MARKER
        for crop in farm["crops"]:
            slot = int(crop.get("slot") or 0)
            if slot not in selected:
                continue
            herb_name = crop.get("name")
            if not herb_name:
                continue
            if herb_name == HYBRID_MARKER:
                if tasks.pop(str(slot), None) is not None:
                    config_dirty = True
                mature_time = int(crop.get("mature_time", 0) or 0)
                if now >= mature_time:
                    harvest_slots.append(slot)
                continue
            if tasks.get(str(slot)) != herb_name:
                tasks[str(slot)] = herb_name
                config_dirty = True
            mature_time = int(crop.get("mature_time", 0) or 0)
            if now >= mature_time:
                harvest_slots.append(slot)

        harvest_msg = ""
        if harvest_slots:
            _, harvest_msg = await self.farm_mgr.harvest(player, slots=harvest_slots)

        farm_after = await self.farm_mgr.get_user_farm(user_id) or {"crops": []}
        used_slots = {int(c.get("slot") or 0) for c in farm_after.get("crops", [])}
        assignments = {}
        for slot in slots:
            if slot in used_slots:
                continue
            herb_name = tasks.get(str(slot))
            if herb_name:
                assignments[slot] = herb_name
        planted_count, plant_msg = await self.farm_mgr.plant_herb_at_slots(player, assignments)

        changed = bool(harvest_slots or planted_count)
        messages = []
        if harvest_msg:
            messages.append(harvest_msg)
        if plant_msg:
            messages.append(plant_msg)
        return changed, "\n".join(messages), config_dirty

    # ===== 自动施肥（独立子系统，不占用 UserStatus） =====

    async def _enable_fert(
        self, user_id: str, origin: str, raw_task: str
    ) -> Tuple[bool, str]:
        if not self.farm_mgr:
            return False, "❌ 灵田系统未加载，暂时无法自动施肥。"
        farm = await self.farm_mgr.get_user_farm(user_id)
        if not farm:
            return False, "❌ 你还没有灵田，请先使用 /开垦灵田。"

        from .spirit_farm_manager import FERTILIZE_COST

        parts = raw_task.strip().split(maxsplit=1)
        rest = (parts[1] if len(parts) > 1 else "").strip()
        if not rest:
            m = re.match(r"^(?:施肥|fertilize)\s*(\d+)%?$", raw_task.strip(), re.IGNORECASE)
            if m:
                rest = m.group(1)
        threshold = DEFAULT_FERTILIZE_THRESHOLD
        if rest:
            digits = re.sub(r"[%％]", "", rest).strip()
            if not digits.isdigit():
                return False, (
                    "❌ 施肥阈值要填 10-100 的百分比数字。\n"
                    "💡 /自动代理 施肥 70（肥力低于 70% 时自动补肥）"
                )
            value = int(digits)
            if not 10 <= value <= 100:
                return False, "❌ 施肥阈值需要在 10-100 之间。\n💡 例如 /自动代理 施肥 70"
            threshold = value / 100.0

        row = await self.get(user_id) or {}
        config = self._load_config(row)
        fert_cfg = config["fertilize"]
        fert_cfg["enabled"] = True
        fert_cfg["threshold"] = threshold
        fert_cfg["min_roi"] = float(
            fert_cfg.get("min_roi") or DEFAULT_FERTILIZE_MIN_ROI
        )
        fert_cfg["note"] = ""
        origin = origin or row.get("origin", "")
        cd = await self.db.ext.get_user_cd(user_id)
        active_task = STATUS_TASK.get(cd.type) if cd else ""
        await self._write_state(user_id, True, active_task or "", origin, "", config)

        player = await self.db.get_player_by_id(user_id)
        changed, cycle_msg, dirty = await self._run_fert_cycle(user_id, player, config)
        if changed or dirty:
            await self._write_state(
                user_id, True, active_task or "", origin, "", config
            )

        max_f = self.farm_mgr.get_max_fertility(farm)
        fertility = int(farm.get("fertility") or 0)
        gain = self.farm_mgr.get_gain_per_fertilize(farm)
        roi = self.farm_mgr.estimate_fertilize_roi(farm)
        lines = [
            "🤖 自动施肥已开启",
            "━━━━━━━━━━━━━━━",
            f"触发阈值：肥力低于 {threshold:.0%}（{int(max_f * threshold)}/{max_f}）",
            f"单次施肥：+{gain} 肥力 / {FERTILIZE_COST:,} 灵石",
            f"当前肥力：{fertility}/{max_f}",
            (
                f"收益判断：当前作物施肥回报约 {roi:.2f} 倍，值得施 ✅"
                if roi >= DEFAULT_FERTILIZE_MIN_ROI
                else f"收益判断：当前作物施肥回报仅 {roi:.2f} 倍，低于 1 倍时会自动跳过 ⚠️"
            ),
            "💡 只在田里有作物且回报 ≥ 成本时施肥；与灵田代理互不影响。",
        ]
        if cycle_msg:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append(cycle_msg)
        return True, "\n".join(lines)

    async def disable_fert(self, user_id: str) -> Tuple[bool, str]:
        row = await self.get(user_id)
        if not row:
            return False, "❌ 道友尚未踏入仙途。"
        config = self._load_config(row)
        fert_cfg = config["fertilize"]
        if not fert_cfg.get("enabled"):
            return False, "💡 自动施肥当前未开启。"
        fert_cfg["enabled"] = False
        fert_cfg["note"] = ""
        keep_enabled = (
            bool(row.get("task_type"))
            or bool(config["farm"].get("enabled"))
            or bool(config["rift"].get("enabled"))
        )
        await self._write_state(
            user_id,
            keep_enabled,
            row.get("task_type", ""),
            row.get("origin", ""),
            row.get("last_error", ""),
            config,
        )
        return True, "🛑 自动施肥已关闭。\n灵田与押镖/宗门任务代理不受影响。"

    async def _run_fert_cycle(
        self, user_id: str, player: Any, config: dict
    ) -> Tuple[bool, str, bool]:
        """自动施肥：肥力低于阈值且有作物时补肥。

        必须排在灵田收获之前调用 —— 收益按「收获那一刻」的肥力结算，
        顺序反了会让本茬白亏一截肥力。
        返回 ``(是否需要通知, 消息, 配置是否变化)``。
        """
        if not self.farm_mgr or not player:
            return False, "", False
        fert_cfg = config["fertilize"]
        if not fert_cfg.get("enabled"):
            return False, "", False

        farm = await self.farm_mgr.get_user_farm(user_id)
        if not farm:
            fert_cfg["enabled"] = False
            return True, "⚠️ 灵田已不存在，自动施肥已关闭。", True

        from .spirit_farm_manager import FERTILIZE_COST, HYBRID_MARKER

        max_f = self.farm_mgr.get_max_fertility(farm)
        fertility = int(farm.get("fertility") or 0)
        threshold = float(fert_cfg.get("threshold") or DEFAULT_FERTILIZE_THRESHOLD)
        if max_f <= 0 or fertility >= max_f * threshold:
            return False, "", False

        # 空田不施：等续种后自然会补上
        crops = [
            crop
            for crop in (farm.get("crops") or [])
            if crop.get("name") and crop.get("name") != HYBRID_MARKER
        ]
        if not crops:
            return False, "", False

        min_roi = float(
            fert_cfg.get("min_roi")
            if fert_cfg.get("min_roi") is not None
            else DEFAULT_FERTILIZE_MIN_ROI
        )
        roi = self.farm_mgr.estimate_fertilize_roi(farm)
        if roi < min_roi:
            # 回报不足时静默跳过，只在状态页说明，避免刷屏
            if fert_cfg.get("note"):
                fert_cfg["note"] = ""
                return False, "", True
            return False, "", False

        if int(player.gold or 0) < FERTILIZE_COST:
            if fert_cfg.get("note") != "insufficient_gold":
                fert_cfg["note"] = "insufficient_gold"
                return (
                    True,
                    f"⚠️ 自动施肥暂停：灵石不足 {FERTILIZE_COST:,}"
                    f"（当前 {int(player.gold or 0):,}），补足后会自动继续。",
                    True,
                )
            return False, "", False

        gain = self.farm_mgr.get_gain_per_fertilize(farm)
        need = max_f - fertility
        times = min(MAX_FERTILIZE_TIMES, max(1, (need + gain - 1) // gain))
        ok, msg, _done = await self.farm_mgr.fertilize_once(player, times)

        dirty = False
        if fert_cfg.get("note"):
            fert_cfg["note"] = ""
            dirty = True
        if not ok:
            return False, "", dirty
        return True, f"🌱 自动施肥：{msg}", dirty

    # ===== 秘境代理 =====

    @staticmethod
    def _rift_label(rift: Any) -> str:
        return f"【{rift.rift_name}】ID:{rift.rift_id}（需境界 {rift.required_level}）"

    def _rift_choices_text(self, enterable: list) -> str:
        lines = [
            "🌀 请选择要代理探索的秘境：",
            "━━━━━━━━━━━━━━━",
        ]
        for rift in enterable:
            lines.append(f"  · {self._rift_label(rift)}")
        lines.append("💡 发送 /自动代理 秘境 <ID> 开启，例如 /自动代理 秘境 1")
        lines.append("♻️ 开启后到期自动结算，并按同一秘境重新进入。")
        return "\n".join(lines)

    def _match_rift(self, raw: str, rifts: list, enterable: list) -> Optional[Any]:
        """按 ID 或名称匹配秘境（仅允许已满足境界要求的秘境）。"""
        text = (raw or "").strip()
        if not text:
            return None
        digits = re.sub(r"^(id|ID|秘境|rift)\s*", "", text).strip()
        if digits.isdigit():
            target = int(digits)
            for rift in rifts:
                if int(getattr(rift, "rift_id", 0)) == target:
                    return rift
            return None
        for rift in enterable:
            if text in str(getattr(rift, "rift_name", "")):
                return rift
        return None

    async def _enable_rift(
        self, user_id: str, origin: str, raw_task: str
    ) -> Tuple[bool, str]:
        if not self.rift_mgr:
            return False, "❌ 秘境系统未加载，暂时无法代理秘境。"

        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 道友尚未踏入仙途，请先发送「我要修仙」。"

        try:
            rifts = list(await self.db.ext.get_all_rifts() or [])
        except Exception as e:
            logger.warning(f"【自动代理】读取秘境列表失败: {e}")
            rifts = []
        if not rifts:
            return False, "❌ 当前没有开放的秘境，暂时无法代理。"

        enterable = [r for r in rifts if player.level_index >= int(r.required_level)]
        if not enterable:
            return False, (
                "❌ 你当前境界还没有可进入的秘境。\n"
                "💡 先 /闭关 /历练 提升修为，突破后再来代理秘境。"
            )

        parts = raw_task.strip().split(maxsplit=1)
        rest = parts[1].strip() if len(parts) > 1 else ""
        if not rest:
            # 兼容粘连写法：/自动代理 秘境1
            m = re.match(r"^(?:秘境|rift)\s*(\d+)$", raw_task.strip(), re.IGNORECASE)
            if m:
                rest = m.group(1)
        if not rest:
            # 不做自动选择：必须由玩家指定秘境
            return False, self._rift_choices_text(enterable)

        rift = self._match_rift(rest, rifts, enterable)
        if not rift:
            return False, (
                f"❌ 找不到可代理的秘境「{rest}」。\n"
                "💡 请从下列秘境中选择：\n"
                + "\n".join(f"  · {self._rift_label(r)}" for r in enterable)
            )

        if player.level_index < int(rift.required_level):
            level_name = self.rift_mgr._get_level_name(int(rift.required_level))
            return False, (
                f"❌ 【{rift.rift_name}】需要达到【{level_name}】，你当前境界不足。\n"
                "💡 你当前可代理的秘境：\n"
                + "\n".join(f"  · {self._rift_label(r)}" for r in enterable)
            )

        row = await self.get(user_id) or {}
        config = self._load_config(row)
        rift_cfg = config["rift"]
        rift_cfg["enabled"] = True
        rift_cfg["rift_id"] = int(rift.rift_id)
        origin = origin or row.get("origin", "")

        # 秘境与押镖/宗门任务互斥：开启秘境代理时清空任务目标
        await self._write_state(user_id, True, "", origin, "", config)

        header = (
            "🤖 秘境代理已开启\n"
            "━━━━━━━━━━━━━━━\n"
            f"目标秘境：{self._rift_label(rift)}"
        )

        try:
            cd = await self.db.ext.get_user_cd(user_id)
        except Exception:
            cd = None
        cd_type = cd.type if cd else UserStatus.IDLE

        if cd_type == UserStatus.IDLE:
            ok, start_msg = await self.rift_mgr.enter_rift(user_id, int(rift.rift_id))
            if not ok:
                rift_cfg["enabled"] = False
                await self._write_state(user_id, False, "", origin, "start_failed", config)
                return False, f"{header}\n\n⚠️ 无法开始探索：\n{start_msg}"
            header += f"\n\n♻️ 已开始第一轮探索\n{start_msg}"
            header += "\n💡 发送 /关闭代理 秘境 可停止。"
            return True, header

        if cd_type == UserStatus.EXPLORING:
            header += (
                "\n\n♻️ 已接管当前探索，完成后自动结算并重新进入。\n"
                "💡 发送 /关闭代理 秘境 可停止。"
            )
            return True, header

        header += (
            f"\n\n⏳ 你当前正{UserStatus.get_name(cd_type)}，结束后将自动开始秘境探索。\n"
            "💡 发送 /关闭代理 秘境 可停止。"
        )
        return True, header

    async def disable_rift(self, user_id: str) -> Tuple[bool, str]:
        row = await self.get(user_id)
        if not row:
            return False, "❌ 道友尚未踏入仙途。"
        config = self._load_config(row)
        rift_cfg = config["rift"]
        if not rift_cfg.get("enabled"):
            return False, "💡 秘境代理当前未开启。"
        rift_cfg["enabled"] = False
        keep_enabled = bool(row.get("task_type")) or bool(
            config["farm"].get("enabled")
        ) or bool(config["fertilize"].get("enabled"))
        await self._write_state(
            user_id,
            keep_enabled,
            row.get("task_type", ""),
            row.get("origin", ""),
            row.get("last_error", ""),
            config,
        )
        return True, (
            "🛑 秘境代理已关闭。\n"
            "当前这次探索不会中断，可继续 /完成探索 领取奖励。"
        )

    async def _record_rift_progress(self, user_id: str, player: Any):
        """自动结算秘境后补记日常任务/称号/悬赏进度（奇遇仍只手动触发）。"""
        try:
            await self.db.ext.add_task_progress(user_id, "explore_rift", 1)
        except Exception as e:
            logger.warning(f"【自动代理】秘境日常进度写入失败: {e}")
        if self.title_bump:
            try:
                await self.title_bump(user_id, "rift_count", 1)
            except Exception as e:
                logger.warning(f"【自动代理】秘境称号计数失败: {e}")
        if self.bounty_mgr:
            try:
                await self.bounty_mgr.add_bounty_progress(player, "rift", 1)
            except Exception as e:
                logger.warning(f"【自动代理】秘境悬赏进度失败: {e}")

    async def _run_rift_cycle(
        self, user_id: str, player: Any, config: dict
    ) -> Tuple[bool, str, bool]:
        """执行一次秘境结算/开始；返回 (是否发生动作, 消息, 是否已完成一轮)。"""
        if not self.rift_mgr:
            return False, "", False
        rift_cfg = config["rift"]
        if not rift_cfg.get("enabled"):
            return False, "", False

        try:
            rift_id = int(rift_cfg.get("rift_id") or 0)
        except (TypeError, ValueError):
            rift_id = 0
        if rift_id <= 0:
            rift_cfg["enabled"] = False
            return True, "⚠️ 未选择代理秘境，秘境代理已关闭。", False

        try:
            cd = await self.db.ext.get_user_cd(user_id)
        except Exception:
            cd = None
        cd_type = cd.type if cd else UserStatus.IDLE
        now = int(time.time())

        if cd_type == UserStatus.EXPLORING:
            if cd and cd.scheduled_time and now < int(cd.scheduled_time):
                return False, "", False
            ok, msg, _reward = await self.rift_mgr.finish_exploration(user_id)
            if not ok:
                rift_cfg["enabled"] = False
                return (
                    True,
                    f"⚠️ 秘境结算失败：\n{msg}\n"
                    "🛑 秘境代理已关闭，发送 /自动代理 秘境 <ID> 可重新开启。",
                    False,
                )
            ok, start_msg = await self.rift_mgr.enter_rift(user_id, rift_id)
            if not ok:
                rift_cfg["enabled"] = False
                return (
                    True,
                    f"{msg}\n\n⚠️ 无法开始下一轮探索：\n{start_msg}\n"
                    "🛑 秘境代理已关闭，发送 /自动代理 秘境 <ID> 可重新开启。",
                    True,
                )
            return (
                True,
                f"{msg}\n\n🤖 秘境代理：已开始下一轮探索\n{start_msg}",
                True,
            )

        if cd_type != UserStatus.IDLE:
            return False, "", False

        ok, start_msg = await self.rift_mgr.enter_rift(user_id, rift_id)
        if not ok:
            rift_cfg["enabled"] = False
            return (
                True,
                f"⚠️ 无法开始秘境探索：\n{start_msg}\n"
                "🛑 秘境代理已关闭，发送 /自动代理 秘境 <ID> 可重新开启。",
                False,
            )
        return True, f"🤖 秘境代理：已开始新一轮探索\n{start_msg}", False

    # ===== 命令入口 =====

    async def enable(self, user_id: str, origin: str = "", task_type: str = "") -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 道友尚未踏入仙途，请先发送「我要修仙」。"

        raw_task = (task_type or "").strip()
        if raw_task and self._is_fert_request(raw_task):
            return await self._enable_fert(user_id, origin, raw_task)
        if raw_task and self._is_farm_request(raw_task):
            return await self._enable_farm(user_id, origin, raw_task)
        if raw_task and self._is_rift_request(raw_task):
            return await self._enable_rift(user_id, origin, raw_task)

        requested = self.normalize_task(raw_task) if raw_task else None
        if raw_task and not requested:
            return False, (
                "❌ 暂不支持该活动。\n"
                "💡 目前自动代理支持：/自动代理 押镖、/自动代理 宗门任务、"
                "/自动代理 灵田、/自动代理 秘境、/自动代理 施肥"
            )

        row = await self.get(user_id) or {}
        config = self._load_config(row)
        rift_closed = bool(config["rift"].get("enabled"))
        if rift_closed:
            config["rift"]["enabled"] = False
        rift_note = "\n（秘境代理已关闭）" if rift_closed else ""
        cd = await self.db.ext.get_user_cd(user_id)
        current = STATUS_TASK.get(cd.type) if cd else None

        if not raw_task:
            if not current:
                return False, (
                    "❌ 请指定要代理的内容。\n"
                    "💡 /自动代理 押镖\n"
                    "💡 /自动代理 宗门任务\n"
                    "💡 /自动代理 灵田 [格号...]\n"
                    "💡 /自动代理 秘境 <ID>\n"
                    "💡 /自动代理 施肥 [阈值%]\n"
                    "（仅当已处于押镖/宗门任务中时，可省略参数自动补全）"
                )
            requested = current

        was_enabled = bool(row.get("enabled"))
        origin = origin or row.get("origin", "")
        legacy_busy = (not cd) and self._legacy_busy(player)
        busy_other = bool((cd and cd.type != UserStatus.IDLE and not current) or legacy_busy)
        busy_name = UserStatus.get_name(cd.type) if cd else (getattr(player, "state", "") or "忙碌")
        if busy_other:
            return False, (
                f"❌ 你当前正在{busy_name}，自动代理只能接管押镖/宗门任务。\n"
                f"💡 请空闲后再发送 /自动代理 押镖 或 /自动代理 宗门任务。"
            )

        preferred = requested or current or row.get("task_type", "")
        await self._write_state(user_id, True, preferred, origin, "", config)

        if current:
            if requested and requested != current:
                return True, (
                    f"🤖 自动代理已开启\n"
                    f"当前正在{UserStatus.get_name(cd.type)}，先完成本轮；"
                    f"之后将自动改跑【{self.task_name(requested)}】。\n"
                    f"💡 发送 /关闭代理 押镖 或 /关闭代理 宗门任务 可随时停止。"
                    f"{rift_note}"
                )
            return True, (
                f"🤖 自动代理已开启\n"
                f"已接管当前【{self.task_name(current)}】，本轮结束后会自动结算并开始下一轮。\n"
                f"💡 发送 /关闭代理 押镖 或 /关闭代理 宗门任务 可随时停止。"
                f"{rift_note}"
            )

        if not preferred:
            tip = "🤖 自动代理已在运行中" if was_enabled else "🤖 自动代理已开启"
            return True, (
                f"{tip}\n"
                f"请先发送 /押镖 或 /宗门任务 开始第一轮，之后会自动续接。\n"
                f"💡 也可直接使用 /自动代理 押镖、/自动代理 宗门任务、/自动代理 灵田、"
                f"/自动代理 秘境 或 /自动代理 施肥。\n"
                f"发送 /关闭代理 押镖 或 /关闭代理 宗门任务 可随时停止。"
                f"{rift_note}"
            )

        ok, start_msg = await self.start_activity(player, preferred)
        if not ok:
            await self._disable_primary(user_id, "start_failed")
            return False, f"❌ 自动代理启动【{self.task_name(preferred)}】失败：\n{start_msg}"

        return True, (
            f"🤖 自动代理已开启，并已开始第一轮【{self.task_name(preferred)}】\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{start_msg}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"♻️ 每轮结束后会自动结算并开始下一轮，发送 /关闭代理 押镖 或 /关闭代理 宗门任务 可停止。"
            f"{rift_note}"
        )

    async def status_text(self, user_id: str) -> str:
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return "❌ 道友尚未踏入仙途，请先发送「我要修仙」。"

        row = await self.get(user_id) or {}
        cd = await self.db.ext.get_user_cd(user_id)
        current = STATUS_TASK.get(cd.type) if cd else None
        config = self._load_config(row)
        farm_cfg = config["farm"]
        farm_on = bool(farm_cfg.get("enabled"))
        rift_cfg = config["rift"]
        rift_on = bool(rift_cfg.get("enabled"))
        fert_cfg = config["fertilize"]
        fert_on = bool(fert_cfg.get("enabled"))

        if not row.get("enabled"):
            last = row.get("task_type")
            suffix = f"\n上次代理活动：{self.task_name(last)}" if last else ""
            return (
                "🤖 自动代理：未开启\n"
                "━━━━━━━━━━━━━━━\n"
                "发送 /自动代理 押镖、/自动代理 宗门任务、/自动代理 灵田、"
                "/自动代理 秘境 或 /自动代理 施肥 开启。"
                f"{suffix}"
            )

        lines = ["🤖 自动代理：运行中", "━━━━━━━━━━━━━━━"]
        if farm_on:
            farm = await self.farm_mgr.get_user_farm(user_id) if self.farm_mgr else None
            max_slots = self.farm_mgr.get_max_slots(farm) if (self.farm_mgr and farm) else 0
            raw_plots = farm_cfg.get("plots")
            exclude = farm_cfg.get("exclude") or []
            if raw_plots is None and max_slots:
                plot_text = f"全部格（1-{max_slots}）"
                if exclude:
                    plot_text += f"，排除 {self._farm_plots_text(exclude)}"
            else:
                plot_text = self._farm_plots_text(raw_plots or [])
            lines.append(f"灵田代理：运行中（{plot_text}）")
        if rift_on:
            rift_id = int(rift_cfg.get("rift_id") or 0)
            rift_name = ""
            if self.rift_mgr and rift_id:
                try:
                    rift = await self.db.ext.get_rift_by_id(rift_id)
                    rift_name = f"【{rift.rift_name}】" if rift else ""
                except Exception:
                    rift_name = ""
            lines.append(f"秘境代理：运行中（{rift_name}ID:{rift_id}）")
        if fert_on:
            threshold = float(
                fert_cfg.get("threshold") or DEFAULT_FERTILIZE_THRESHOLD
            )
            fert_farm = (
                await self.farm_mgr.get_user_farm(user_id) if self.farm_mgr else None
            )
            if fert_farm:
                max_f = self.farm_mgr.get_max_fertility(fert_farm)
                fertility = int(fert_farm.get("fertility") or 0)
                roi = self.farm_mgr.estimate_fertilize_roi(fert_farm)
                detail = f"肥力 {fertility}/{max_f}，阈值 {threshold:.0%}"
                if roi < DEFAULT_FERTILIZE_MIN_ROI:
                    detail += f"，作物回报 {roi:.2f} 倍（自动跳过）"
                if fert_cfg.get("note") == "insufficient_gold":
                    detail += "，灵石不足已暂停"
                lines.append(f"施肥代理：运行中（{detail}）")
            else:
                lines.append("施肥代理：运行中（无灵田）")
        if row.get("task_type"):
            lines.append(f"目标活动：{self.task_name(row.get('task_type'))}")
        elif not farm_on and not rift_on and not fert_on:
            lines.append("目标活动：等待选择")
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
        elif cd and cd.type == UserStatus.EXPLORING:
            if rift_on:
                remaining = max(0, int(getattr(cd, "scheduled_time", 0) or 0) - int(time.time()))
                if remaining > 0:
                    lines.append(f"当前进行：探索秘境（剩余 {remaining // 60} 分 {remaining % 60} 秒）")
                else:
                    lines.append("当前进行：探索秘境（可结算，等待代理处理）")
            else:
                lines.append("当前状态：探索秘境中（秘境代理未开启）")
        elif cd and cd.type != UserStatus.IDLE:
            if row.get("task_type"):
                lines.append(f"当前状态：{UserStatus.get_name(cd.type)}（押镖/宗门任务代理将暂停）")
            else:
                lines.append(f"当前状态：{UserStatus.get_name(cd.type)}（灵田/秘境代理不受影响）")
        elif not cd and self._legacy_busy(player):
            if row.get("task_type"):
                lines.append(f"当前状态：{player.state}（押镖/宗门任务代理将暂停）")
            else:
                lines.append(f"当前状态：{player.state}（灵田/秘境代理不受影响）")
        elif rift_on:
            lines.append("当前状态：秘境代理运行中，等待空闲后进入秘境")
        elif farm_on:
            lines.append("当前状态：灵田代理运行中，等待成熟")
        elif fert_on:
            lines.append("当前状态：自动施肥运行中，肥力不足时补肥")
        else:
            lines.append("当前状态：空闲（等待下一轮）")

        if row.get("last_error") == "start_failed":
            lines.append("⚠️ 上次自动开始失败，已暂停")
        lines.append(
            "💡 /关闭代理 押镖|宗门任务|灵田 [格号]|秘境|施肥|全部 停止对应代理"
        )
        return "\n".join(lines)

    # ===== 活动执行 =====

    async def start_activity(self, player: Any, task_type: str) -> Tuple[bool, str]:
        # 与手动指令保持一致的受伤拦截：气血不足 20% 时不再自动开始行动。
        config_manager = getattr(self.escort_mgr, "config_manager", None)
        if config_manager is not None:
            try:
                from .recovery_manager import RecoveryManager
                fresh = await self.db.get_player_by_id(player.user_id) or player
                recovery = RecoveryManager(self.db, config_manager)
                if await recovery.is_injured(fresh):
                    return False, (
                        "❌ 你当前身受重伤（气血不足 20%），自动代理已暂停。\n"
                        "💡 请先 /调息 或服用回春散恢复气血，再发送 /自动代理 押镖 重新开启。"
                    )
            except Exception as e:
                logger.warning(f"【自动代理】受伤状态检查失败: {e}")
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

        config = self._load_config(row)
        farm_cfg = config["farm"]
        rift_cfg = config["rift"]
        fert_cfg = config["fertilize"]
        handled = False

        # 自动施肥必须排在灵田收获之前：收益按“收获那一刻”的肥力结算
        if fert_cfg.get("enabled") and self.farm_mgr:
            changed, fert_msg, config_dirty = await self._run_fert_cycle(
                user_id, player, config
            )
            if changed or config_dirty:
                keep_enabled = (
                    bool(row.get("task_type"))
                    or bool(farm_cfg.get("enabled"))
                    or bool(rift_cfg.get("enabled"))
                    or bool(fert_cfg.get("enabled"))
                )
                await self._write_state(
                    user_id,
                    keep_enabled,
                    row.get("task_type", ""),
                    origin,
                    row.get("last_error", ""),
                    config,
                )
            if changed:
                await self._notify(origin, fert_msg)
                handled = True

        # 灵田代理独立于忙碌状态运行
        if farm_cfg.get("enabled") and self.farm_mgr:
            changed, farm_msg, config_dirty = await self._run_farm_cycle(user_id, player, config)
            if changed or config_dirty:
                keep_enabled = (
                    bool(row.get("task_type"))
                    or bool(farm_cfg.get("enabled"))
                    or bool(rift_cfg.get("enabled"))
                    or bool(fert_cfg.get("enabled"))
                )
                await self._write_state(
                    user_id,
                    keep_enabled,
                    row.get("task_type", ""),
                    origin,
                    row.get("last_error", ""),
                    config,
                )
            if changed:
                await self._notify(origin, farm_msg)
                handled = True

        # 秘境代理：接管探索中的结算与空闲时的进入，不触发奇遇
        if rift_cfg.get("enabled") and self.rift_mgr:
            changed, rift_msg, finished = await self._run_rift_cycle(user_id, player, config)
            if changed:
                keep_enabled = (
                    bool(row.get("task_type"))
                    or bool(farm_cfg.get("enabled"))
                    or bool(rift_cfg.get("enabled"))
                    or bool(fert_cfg.get("enabled"))
                )
                await self._write_state(
                    user_id,
                    keep_enabled,
                    row.get("task_type", ""),
                    origin,
                    row.get("last_error", ""),
                    config,
                )
            if finished:
                await self._record_rift_progress(user_id, player)
            if changed:
                if rift_msg:
                    await self._notify(origin, rift_msg)
                handled = True

        cd = await self.db.ext.get_user_cd(user_id)
        cd_type = cd.type if cd else UserStatus.IDLE
        current = STATUS_TASK.get(cd_type)
        farm_on = bool(farm_cfg.get("enabled"))

        if current:
            # 只开启灵田代理时，不接管玩家手动开始/尚未结算的押镖、宗门任务；
            # 否则“关闭押镖代理”后，当前这轮结束又会被自动续接。
            if not row.get("task_type"):
                return handled
            if cd and cd.scheduled_time and now < cd.scheduled_time:
                return handled

            ok, finish_msg = await self.finish_activity(player, current)
            if not ok:
                await self._notify(
                    origin,
                    f"⚠️ 自动代理结算【{self.task_name(current)}】失败：\n{finish_msg}\n"
                    f"🛑 押镖/宗门任务代理已暂停，发送 /自动代理 押镖 或 /自动代理 宗门任务 可重试。",
                )
                await self._disable_primary(user_id, "finish_failed")
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
                    f"🛑 押镖/宗门任务代理已暂停，发送 /自动代理 押镖 或 /自动代理 宗门任务 可重新开启。",
                )
                await self._disable_primary(user_id, "start_failed")
                return True

            await self._notify(
                origin,
                f"{finish_msg}\n\n"
                f"🤖 自动代理：已开始下一轮【{self.task_name(next_task)}】\n"
                f"{start_msg}",
            )
            return True

        if cd_type != UserStatus.IDLE:
            if not row.get("task_type"):
                return handled
            # 玩家进入了闭关/历练等代理不接管的活动：暂停押镖/宗门任务代理
            keep_text = "、".join(
                name
                for name, on in (
                    ("灵田", farm_on),
                    ("秘境", bool(rift_cfg.get("enabled"))),
                    ("施肥", bool(fert_cfg.get("enabled"))),
                )
                if on
            )
            await self._notify(
                origin,
                f"🛑 检测到你进入了{UserStatus.get_name(cd_type)}，"
                + (
                    f"押镖/宗门任务代理已暂停，{keep_text}代理继续运行。"
                    if keep_text
                    else "自动代理已关闭。发送 /自动代理 押镖 或 /自动代理 宗门任务 可重新开启。"
                ),
            )
            await self._disable_primary(user_id, "other_busy")
            return True

        if not cd and self._legacy_busy(player):
            if not row.get("task_type"):
                return handled
            # 兼容旧数据：user_cd 未建立但 players.state 仍显示忙碌
            keep_text = "、".join(
                name
                for name, on in (
                    ("灵田", farm_on),
                    ("秘境", bool(rift_cfg.get("enabled"))),
                    ("施肥", bool(fert_cfg.get("enabled"))),
                )
                if on
            )
            await self._notify(
                origin,
                f"🛑 检测到你进入了{player.state}，"
                + (
                    f"押镖/宗门任务代理已暂停，{keep_text}代理继续运行。"
                    if keep_text
                    else "自动代理已关闭。发送 /自动代理 押镖 或 /自动代理 宗门任务 可重新开启。"
                ),
            )
            await self._disable_primary(user_id, "other_busy")
            return True

        task_type = row.get("task_type", "")
        if not task_type:
            return handled

        ok, start_msg = await self.start_activity(player, task_type)
        if not ok:
            await self._notify(
                origin,
                f"⚠️ 自动代理无法开始【{self.task_name(task_type)}】：\n{start_msg}\n"
                f"🛑 押镖/宗门任务代理已暂停，发送 /自动代理 押镖 或 /自动代理 宗门任务 可重新开启。",
            )
            await self._disable_primary(user_id, "start_failed")
            return True

        await self._notify(
            origin,
            f"🤖 自动代理：已开始新一轮【{self.task_name(task_type)}】\n{start_msg}",
        )
        return True
