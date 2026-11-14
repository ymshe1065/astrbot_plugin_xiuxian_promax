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
TASK_ADVENTURE = "adventure"

TASK_NAMES = {
    TASK_ESCORT: "押镖",
    TASK_SECT: "宗门任务",
    TASK_FARM: "灵田",
    TASK_RIFT: "秘境",
    TASK_FERTILIZE: "施肥",
    TASK_ADVENTURE: "历练",
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
    "历练": TASK_ADVENTURE,
    "历炼": TASK_ADVENTURE,
    "adventure": TASK_ADVENTURE,
}

# 施肥是“维护动作”而不是“活动”，不占用 UserStatus，故不进 TASK_STATUS
DEFAULT_FERTILIZE_THRESHOLD = 0.85
DEFAULT_FERTILIZE_MIN_ROI = 1.0
# 单轮最多补几次肥（每次 +30% 上限，4 次足以从地板补满）
MAX_FERTILIZE_TIMES = 4

# 历练 CD 短于这个秒数时不进闭关：刚进就要出关，白跑一趟还拿不到修为
ADV_MIN_RETREAT_SECONDS = 90
ADV_MODE_CONTINUE = "continue"
ADV_MODE_ALTERNATE = "alternate"

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
        notifier: Optional[Callable[..., Awaitable[None]]] = None,
        title_bump: Optional[Callable[[str, str, int], Awaitable[str]]] = None,
        bounty_mgr: Any = None,
        lock_getter: Optional[Callable[[str], asyncio.Lock]] = None,
        farm_mgr: Any = None,
        rift_mgr: Any = None,
        adventure_mgr: Any = None,
        encounter_mgr: Any = None,
        cultivation_settler: Optional[Callable[[Any], Awaitable[Tuple[bool, str]]]] = None,
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
        self.adventure_mgr = adventure_mgr
        self.encounter_mgr = encounter_mgr
        self._cultivation_settler = cultivation_settler
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

    async def _active_primary(self, user_id: str) -> list:
        """押镖 / 宗门任务当前各占哪个槽位：``[(task_type, user_cd), ...]``。

        v3.45.12 起一个玩家可能有多个活动槽位（本体 + 身外化身），押镖和宗门任务
        可以同时各占一槽；不能再拿「唯一一条 user_cd」当当前活动，否则后开的那项
        永远轮不到结算。
        """
        active = []
        for task, status in TASK_STATUS.items():
            cd = await self.db.ext.get_active_cd(user_id, status)
            if cd is not None:
                active.append((task, cd))
        return active

    async def _free_slots(self, user_id: str, player: Any) -> int:
        """该玩家还剩几个空闲分身槽位（含本体）。"""
        try:
            from ..core.avatar_rules import total_slots

            total = total_slots(player)
            busy = await self.db.ext.busy_slot_count(user_id)
            return max(0, int(total) - int(busy))
        except Exception:
            return 0

    async def _newest_primary(self, user_id: str) -> Optional[str]:
        """押镖/宗门任务里最近开始的那项（多槽位下用来绑定「当前活动」）。"""
        current, newest = None, -1
        for task, cd in await self._active_primary(user_id):
            started = int(getattr(cd, "create_time", 0) or 0)
            if started >= newest:
                current, newest = task, started
        return current

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

        adv = config.get("adventure")
        if not isinstance(adv, dict):
            adv = {}
        adv.setdefault("enabled", False)
        adv.setdefault("mode", ADV_MODE_CONTINUE)
        adv.setdefault("route", "")
        adv.setdefault("route2", "")
        adv.setdefault("retreat", True)
        adv.setdefault("auto_encounter", True)
        adv.setdefault("encounter_choice", 0)
        adv.setdefault("last_route", "")
        adv.setdefault("close_until", 0)
        adv.setdefault("close_route", "")
        adv.setdefault("note", "")
        config["adventure"] = adv
        return config

    @staticmethod
    def _any_proxy_active(task_type: Any, config: dict) -> bool:
        """任一代理是否仍在运行：押镖/宗门任务、灵田、施肥、秘境。

        用于计算总开关 enabled，避免关闭其中一项时静默杀死其它代理。
        """
        return (
            bool(task_type)
            or bool((config.get("farm") or {}).get("enabled"))
            or bool((config.get("fertilize") or {}).get("enabled"))
            or bool((config.get("rift") or {}).get("enabled"))
            or bool((config.get("adventure") or {}).get("enabled"))
        )

    @classmethod
    def _farm_enabled(cls, row: Optional[dict]) -> bool:
        return bool(cls._load_config(row)["farm"].get("enabled"))

    @classmethod
    def _rift_enabled(cls, row: Optional[dict]) -> bool:
        return bool(cls._load_config(row)["rift"].get("enabled"))

    @classmethod
    def _adv_enabled(cls, row: Optional[dict]) -> bool:
        return bool(cls._load_config(row)["adventure"].get("enabled"))

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
    def _is_adventure_request(raw: str) -> bool:
        parts = (raw or "").strip().split()
        if not parts:
            return False
        key = parts[0].lower()
        if key in ("历练", "历炼", "adventure", "adv"):
            return True
        return key.startswith("历练") or key.startswith("adventure")

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

    async def _settle_auto_seclusion_if_any(self, user_id: str, adv: dict) -> str:
        """关闭历练代理前，若当前闭关是代理触发的就先结算，避免被动挂在闭关里。"""
        if not int(adv.get("close_until") or 0):
            return ""
        try:
            cd = await self.db.ext.get_active_cd(user_id, UserStatus.CULTIVATING)
            if cd is None:
                return ""
            player = await self.db.get_player_by_id(user_id)
            if not player or not self._cultivation_settler:
                return ""
            ok, msg = await self._cultivation_settler(player)
            if ok and self.title_bump:
                try:
                    await self.title_bump(user_id, "cultivate_count", 1)
                except Exception:
                    pass
            return f"\n🧘 已为你结算本次自动闭关：\n{msg}" if ok else ""
        except Exception as e:
            logger.warning(f"【自动代理】结算自动闭关失败: {e}")
            return ""

    async def disable(self, user_id: str, reason: str = "") -> bool:
        """关闭全部代理；保留上次活动类型、灵田任务、秘境目标与施肥阈值，方便下次恢复。"""
        row = await self.get(user_id)
        was_enabled = bool(row and row.get("enabled"))
        config = self._load_config(row)
        await self._settle_auto_seclusion_if_any(user_id, config["adventure"])
        config["farm"]["enabled"] = False
        config["rift"]["enabled"] = False
        config["fertilize"]["enabled"] = False
        config["adventure"]["enabled"] = False
        config["adventure"]["close_until"] = 0
        config["adventure"]["close_route"] = ""
        config["adventure"]["note"] = ""
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
        adv_on = bool(config["adventure"].get("enabled"))
        had_primary = bool(row.get("task_type"))
        await self._disable_primary(user_id, reason)
        if farm_on or rift_on or fert_on or adv_on:
            tip = "押镖/宗门任务代理已关闭" if had_primary else "押镖/宗门任务代理未开启"
            keep = "、".join(
                name
                for name, on in (
                    ("灵田", farm_on),
                    ("秘境", rift_on),
                    ("施肥", fert_on),
                    ("历练", adv_on),
                )
                if on
            )
            return True, (
                f"🛑 {tip}；{keep}自动代理继续运行。\n"
                "💡 发送 /关闭代理 灵田 /关闭代理 秘境 /关闭代理 施肥 /关闭代理 历练 可单独停止；发送 /关闭代理 可全部停止。"
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
            or config["adventure"].get("enabled")
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
            ) or bool(config["fertilize"].get("enabled")) or bool(
                config["adventure"].get("enabled")
            )
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
        """玩家开始代理不接管的活动时关闭押镖/宗门任务代理。

        v3.45.12 起有分身槽位：押镖/宗门任务在自己槽位里跑着时，玩家另开一项活动
        不该再把代理关掉（他还有空位，代理之后也能续接）。只有槽位被别的活动占满、
        且没有押镖/宗门任务在跑时，才沿用旧的「暂停」语义。
        """
        row = await self.get(user_id)
        if not row or not row.get("enabled"):
            return False
        farm_on = self._farm_enabled(row)
        rift_on = self._rift_enabled(row)
        fert_on = self._fert_enabled(row)
        adv_on = self._adv_enabled(row)
        if not (row.get("task_type") or "") and (farm_on or rift_on or fert_on or adv_on):
            return False
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False
        # 押镖/宗门任务还在跑：它们占的是自己的槽位，不构成「被其他活动挤掉」
        if await self._active_primary(user_id):
            return False
        busy = await self.db.ext.busy_slot_count(user_id)
        if busy <= 0:
            return False
        # 手动进入秘境/历练：由对应代理接管续接，不能当成“其他活动”关掉
        types = {
            int(cd.type)
            for cd in await self.db.ext.list_user_cds(user_id)
            if int(cd.type or 0) != UserStatus.IDLE
        }
        if types == {int(UserStatus.EXPLORING)} and rift_on:
            return False
        if types == {int(UserStatus.ADVENTURING)} and adv_on:
            return False
        # 还有空闲分身：留着代理，等它自己续接
        if await self._free_slots(user_id, player) > 0:
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
        # 多槽位下押镖与宗门任务可能同时进行：取最近开始的那项作为当前活动
        current = await self._newest_primary(user_id)
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

    @classmethod
    def _farm_scope_text(cls, plots: Optional[list[int]], max_slots: int) -> str:
        if plots is None:
            if max_slots:
                return f"全部格（1-{max_slots}，升级后自动扩展）"
            return "全部格"
        return cls._farm_plots_text(plots)

    @staticmethod
    def _plot_prefix_length(plots: Any) -> int:
        """返回从格1开始连续覆盖的格数；不是连续前缀时返回 0。"""
        try:
            values = sorted({int(v) for v in plots or []})
        except (TypeError, ValueError):
            return 0
        if not values or values != list(range(1, len(values) + 1)):
            return 0
        return len(values)

    def _normalize_farm_plots(
        self, plots: Optional[list[int]], max_slots: int
    ) -> Optional[list[int]]:
        """恰好覆盖当前全部格位时折叠为 None，灵田升级后自动纳入新格位。"""
        if plots and max_slots and self._plot_prefix_length(plots) == max_slots:
            return None
        return plots

    def _heal_legacy_farm_plots(self, farm_cfg: dict, max_slots: int) -> str:
        """修复旧版被固化成 1-N 的“全部格”，避免灵田扩级后新格位脱离代理。

        仅在配置缺少 ``plots_mode`` 标记（旧版未写入）、格号恰好是从 1 开始的
        连续前缀、且格数等于某个历史灵田档位时触发；玩家明确指定的代理范围
        （由 ``plots_mode=explicit`` 标记）不会被改动。
        """
        if "plots_mode" in farm_cfg or not max_slots:
            return ""
        if farm_cfg.get("exclude"):
            return ""
        frozen = self._plot_prefix_length(farm_cfg.get("plots"))
        if not frozen or frozen >= max_slots:
            return ""
        from .spirit_farm_manager import FARM_LEVELS

        legacy_counts = {int(cfg.get("slots") or 0) for cfg in FARM_LEVELS.values()}
        if frozen not in legacy_counts:
            return ""
        farm_cfg["plots"] = None
        farm_cfg["exclude"] = []
        farm_cfg["plots_mode"] = "all"
        return (
            f"🔧 检测到灵田代理范围被锁定在格1-{frozen}，"
            f"已自动扩展为全部格（当前 {max_slots} 格）。"
        )

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
        # “1..当前全部格”等价于全部格，存 None 才能在升级后自动覆盖新格位
        plots = self._normalize_farm_plots(plots, max_slots)

        row = await self.get(user_id) or {}
        config = self._load_config(row)
        farm_cfg = config["farm"]
        farm_cfg["enabled"] = True
        farm_cfg["plots"] = plots
        farm_cfg["plots_mode"] = "all" if plots is None else "explicit"
        farm_cfg["exclude"] = []
        farm_cfg["tasks"] = farm_cfg.get("tasks") if isinstance(farm_cfg.get("tasks"), dict) else {}
        origin = origin or row.get("origin", "")
        # 只保留当前正在进行的押镖/宗门任务，避免旧的 task_type 在仅开启灵田代理时被自动续接
        active_task = await self._newest_primary(user_id) or ""
        await self._write_state(user_id, True, active_task or "", origin, "", config)

        changed, cycle_msg, config_dirty = await self._run_farm_cycle(user_id, await self.db.get_player_by_id(user_id), config)
        if changed or config_dirty:
            await self._write_state(user_id, True, active_task or "", origin, "", config)

        header = (
            "🤖 灵田代理已开启\n"
            "━━━━━━━━━━━━━━━\n"
            f"代理范围：{self._farm_scope_text(plots, max_slots)}\n"
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
                farm_cfg["plots_mode"] = "all"
            else:
                farm_cfg["plots"] = remain
                farm_cfg["exclude"] = []
                farm_cfg["plots_mode"] = "explicit"
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
            farm_cfg["plots_mode"] = "all"
            farm_cfg["exclude"] = []
            farm_cfg["tasks"] = {}
            action = "灵田代理已关闭。"

        keep_enabled = self._any_proxy_active(row.get("task_type"), config)
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
            farm_cfg["plots_mode"] = "all"
        else:
            farm_cfg["plots"] = remain
            farm_cfg["exclude"] = []
            farm_cfg["plots_mode"] = "explicit"
        if not remain:
            farm_cfg["enabled"] = False
        keep_enabled = self._any_proxy_active(row.get("task_type"), config)
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

        heal_msg = self._heal_legacy_farm_plots(
            farm_cfg, self.farm_mgr.get_max_slots(farm)
        )
        slots = self._selected_slots(farm, farm_cfg)
        if not slots:
            farm_cfg["enabled"] = False
            return True, "⚠️ 没有可代理的灵田格，灵田代理已关闭。", True

        selected = set(slots)
        tasks = farm_cfg.get("tasks") if isinstance(farm_cfg.get("tasks"), dict) else {}
        farm_cfg["tasks"] = tasks
        now = int(time.time())
        harvest_slots = []
        config_dirty = bool(heal_msg)

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
            ok, harvest_msg = await self.farm_mgr.harvest(player, slots=harvest_slots)
            if ok:
                # 对照手动 /收获 流程补记日常任务/称号/悬赏进度
                harvest_count = int(
                    (getattr(self.farm_mgr, "last_harvest_counts", {}) or {}).get(user_id, 0)
                    or 0
                )
                bounty_msg = await self._record_farm_progress(user_id, player, harvest_count)
                if bounty_msg:
                    harvest_msg = f"{harvest_msg}\n{bounty_msg}" if harvest_msg else bounty_msg

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
        if heal_msg:
            messages.append(heal_msg)
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
        active_task = await self._newest_primary(user_id) or ""
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
            or bool(config["adventure"].get("enabled"))
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
        name = getattr(rift, "display_name", "") or getattr(rift, "rift_name", "秘境")
        return f"【{name}】ID:{rift.rift_id}（需境界 {rift.required_level}）"

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
            name = str(getattr(rift, "display_name", "") or getattr(rift, "rift_name", ""))
            if text in name:
                # 「混沌天渊」这类不带难度后缀的写法优先落到一重
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
        if (row.get("task_type") or "").strip() or self._adv_enabled(row):
            # 秘境与押镖/宗门任务、历练代理互斥：不再静默取消进行中的任务代理
            return False, (
                "❌ 当前有进行中的代理任务（押镖/宗门任务/历练），"
                "请先 /关闭代理 后再开启秘境代理。"
            )
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
            rift_cd = await self.db.ext.get_active_cd(user_id, UserStatus.EXPLORING)
        except Exception:
            rift_cd = None

        if rift_cd is not None:
            header += (
                "\n\n♻️ 已接管当前探索，完成后自动结算并重新进入。\n"
                "💡 发送 /关闭代理 秘境 可停止。"
            )
            return True, header

        if await self._free_slots(user_id, player) <= 0:
            header += (
                "\n\n⏳ 分身都在忙，等一个槽位空出来后会自动开始秘境探索。\n"
                "💡 发送 /关闭代理 秘境 可停止。"
            )
            return True, header

        ok, start_msg = await self.rift_mgr.enter_rift(user_id, int(rift.rift_id))
        if not ok:
            if "分身都出去了" in (start_msg or ""):
                header += (
                    "\n\n⏳ 分身都在忙，等一个槽位空出来后会自动开始秘境探索。\n"
                    "💡 发送 /关闭代理 秘境 可停止。"
                )
                return True, header
            rift_cfg["enabled"] = False
            await self._write_state(user_id, False, "", origin, "start_failed", config)
            return False, f"{header}\n\n⚠️ 无法开始探索：\n{start_msg}"
        header += f"\n\n♻️ 已开始第一轮探索\n{start_msg}"
        header += "\n💡 发送 /关闭代理 秘境 可停止。"
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
        ) or bool(config["fertilize"].get("enabled")) or bool(
            config["adventure"].get("enabled")
        )
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

    async def _record_farm_progress(
        self, user_id: str, player: Any, count: int
    ) -> str:
        """自动灵田收获后补记日常任务/称号/悬赏进度（口径与手动 /收获 一致）。

        返回悬赏进度提示（无则为空字符串），由调用方拼进通知消息。
        """
        if count <= 0:
            return ""
        try:
            await self.db.ext.add_task_progress(user_id, "harvest", count)
        except Exception as e:
            logger.warning(f"【自动代理】灵田日常进度写入失败: {e}")
        if self.title_bump:
            try:
                await self.title_bump(user_id, "harvest_count", count)
            except Exception as e:
                logger.warning(f"【自动代理】灵田称号计数失败: {e}")
        bounty_msg = ""
        if self.bounty_mgr:
            try:
                has_progress, msg = await self.bounty_mgr.add_bounty_progress(
                    player, "harvest", count
                )
                if has_progress and msg:
                    bounty_msg = msg.strip()
            except Exception as e:
                logger.warning(f"【自动代理】灵田悬赏进度失败: {e}")
        return bounty_msg

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

        # 只看「秘境」占用的那个槽位：本体在历练 / 化身在押镖时，
        # 不能因为 first_busy_cd 读到别的活动就漏结算或误判不在秘境。
        try:
            rift_cd = await self.db.ext.get_active_cd(user_id, UserStatus.EXPLORING)
        except Exception:
            rift_cd = None
        now = int(time.time())

        if rift_cd is not None:
            if rift_cd.scheduled_time and now < int(rift_cd.scheduled_time):
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

        # 不在秘境：分身全出去了就等一个槽位空出来，别因此关掉代理
        if await self._free_slots(user_id, player) <= 0:
            return False, "", False

        ok, start_msg = await self.rift_mgr.enter_rift(user_id, rift_id)
        if not ok:
            # 没槽位属于「等一等」，不算配置错误，不关闭代理
            if "分身" in (start_msg or "") and ("忙" in (start_msg or "") or "没有空闲" in (start_msg or "")):
                return False, "", False
            rift_cfg["enabled"] = False
            return (
                True,
                f"⚠️ 无法开始秘境探索：\n{start_msg}\n"
                "🛑 秘境代理已关闭，发送 /自动代理 秘境 <ID> 可重新开启。",
                False,
            )
        return True, f"🤖 秘境代理：已开始新一轮探索\n{start_msg}", False

    # ===== 历练代理 =====

    def _adv_route_label(self, route_key: str) -> str:
        if self.adventure_mgr and route_key:
            try:
                return self.adventure_mgr.route_name(route_key)
            except Exception:
                pass
        return route_key or "历练"

    def _adv_available_routes(self, player: Any) -> list:
        if not self.adventure_mgr:
            return []
        try:
            routes = list((self.adventure_mgr.routes or {}).values())
        except Exception:
            return []
        level = int(getattr(player, "level_index", 0) or 0)
        rows = [r for r in routes if level >= int(r.get("min_level", 0) or 0)]
        rows.sort(key=lambda r: (-int(r.get("min_level", 0) or 0), str(r.get("name", ""))))
        return rows

    def _adv_choices_text(self, player: Any) -> str:
        routes = self._adv_available_routes(player)
        lines = [
            "🗺️ 请选择历练代理路线",
            "━━━━━━━━━━━━━━━",
        ]
        for route in routes[:16]:
            lines.append(
                f"  · {route.get('name')}（{route.get('product') or '全产物'}"
                f" · {int(route.get('duration', 0) or 0) // 60} 分钟"
                f" · {route.get('risk', '?')}风险）"
            )
        if len(routes) > 16:
            lines.append(f"  …另有 {len(routes) - 16} 条，/历练信息 查看全部")
        if not routes:
            lines.append("  （当前境界没有可走的路线）")
        lines.extend(
            [
                "━━━━━━━━━━━━━━━",
                "💡 单线：/自动代理 历练 <路线>",
                "💡 双线交叉：/自动代理 历练 <路线A> / <路线B>",
                "💡 奇遇固定选项：在末尾追加 奇遇N（奇遇0 = 智能选择）",
                "♻️ 单线在路线休整时自动闭关，休整结束继续；双线在一条休整时自动换另一条。",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _split_adv_routes(rest: str) -> list[str]:
        return [t for t in re.split(r"[\s/|、,，+]+", rest or "") if t]

    async def _enable_adventure(
        self, user_id: str, origin: str, raw_task: str
    ) -> Tuple[bool, str]:
        if not self.adventure_mgr:
            return False, "❌ 历练系统未加载，暂时无法代理历练。"
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 道友尚未踏入仙途，请先发送「我要修仙」。"

        row = await self.get(user_id) or {}
        config = self._load_config(row)
        if (row.get("task_type") or "").strip():
            return False, (
                "❌ 当前有进行中的押镖/宗门任务代理，与历练代理互斥。\n"
                "💡 请先 /关闭代理 押镖 或 /关闭代理 宗门任务。"
            )

        parts = raw_task.strip().split(maxsplit=1)
        rest = parts[1].strip() if len(parts) > 1 else ""
        # 兼容粘连写法：/自动代理 历练灵谷采药
        if not rest:
            m = re.match(r"^(?:历练|历炼|adventure|adv)\s*(.+)$", raw_task.strip(), re.IGNORECASE)
            if m:
                rest = m.group(1).strip()

        encounter_choice: Optional[int] = None
        m = re.search(r"奇遇\s*([0-9]+)", rest)
        if m:
            encounter_choice = int(m.group(1))
            rest = (rest[: m.start()] + " " + rest[m.end():]).strip()

        adv = config["adventure"]
        tokens = self._split_adv_routes(rest)
        route_keys: list[str] = []
        invalid: list[str] = []
        for token in tokens:
            key = self.adventure_mgr.resolve_route(token)
            if not key:
                invalid.append(token)
                continue
            if key not in route_keys:
                route_keys.append(key)

        if invalid:
            names = "、".join(r.get("name", "") for r in self._adv_available_routes(player)[:10])
            return False, (
                f"❌ 找不到可代理的历练路线：{'、'.join(invalid)}\n"
                + (f"💡 可用路线示例：{names}\n" if names else "")
                + "💡 /自动代理 历练 <路线> [备用路线]"
            )
        if len(route_keys) > 2:
            return False, "❌ 历练代理最多设置两条路线（主路线 + 备用路线）。"

        # 未指定路线时：已开启则只更新奇遇策略；否则给出路线选择
        if not route_keys:
            if not adv.get("enabled") or not adv.get("route"):
                return False, self._adv_choices_text(player)
            route_keys = [adv.get("route")]
            if adv.get("mode") == ADV_MODE_ALTERNATE and adv.get("route2"):
                route_keys.append(adv.get("route2"))

        for key in route_keys:
            route = (self.adventure_mgr.routes or {}).get(key) or {}
            if player.level_index < int(route.get("min_level", 0) or 0):
                return False, (
                    f"❌ 境界不足，无法代理【{route.get('name', key)}】"
                    f"（需境界 {route.get('min_level', 0)}，你当前 {player.level_index}）。"
                )

        # 历练与秘境/押镖同属占用状态的活动代理，开启时关闭秘境代理
        rift_closed = bool(config["rift"].get("enabled"))
        config["rift"]["enabled"] = False
        rift_note = "\n（秘境代理已关闭）" if rift_closed else ""

        adv["enabled"] = True
        adv["route"] = route_keys[0]
        adv["route2"] = route_keys[1] if len(route_keys) > 1 else ""
        adv["mode"] = ADV_MODE_ALTERNATE if adv["route2"] else ADV_MODE_CONTINUE
        adv["last_route"] = ""
        # 保留 close_until / close_route：重复发送本命令可能发生在自动闭关途中，
        # 清掉标记会让代理误以为是玩家自己的闭关而永远不出关。
        adv["note"] = ""
        if encounter_choice is not None:
            if encounter_choice < 0:
                return False, "❌ 奇遇固定选项编号不能为负数；奇遇0 表示智能选择。"
            adv["encounter_choice"] = encounter_choice
        origin = origin or row.get("origin", "")
        await self._write_state(user_id, True, "", origin, "", config)

        mode_text = (
            f"双线交叉（{self._adv_route_label(adv['route'])} ⇄ {self._adv_route_label(adv['route2'])}）"
            if adv["mode"] == ADV_MODE_ALTERNATE
            else f"单线续跑（{self._adv_route_label(adv['route'])}）"
        )
        enc_text = (
            f"奇遇固定选 [{adv.get('encounter_choice')}]"
            if int(adv.get("encounter_choice") or 0) > 0
            else ("奇遇自动选择" if adv.get("auto_encounter", True) else "奇遇不自动选择")
        )
        header = (
            "🤖 历练代理已开启\n"
            "━━━━━━━━━━━━━━━\n"
            f"模式：{mode_text}\n"
            f"休整策略：{'路线 CD 期间自动闭关' if adv.get('retreat', True) else '只等待，不闭关'}\n"
            f"奇遇：{enc_text}{rift_note}"
        )

        # 只看历练/闭关对应的槽位：本体在押镖、分身在秘境时不该误判
        try:
            adv_cd = await self.db.ext.get_active_cd(user_id, UserStatus.ADVENTURING)
            cult_cd = await self.db.ext.get_active_cd(user_id, UserStatus.CULTIVATING)
        except Exception:
            adv_cd = None
            cult_cd = None

        if adv_cd is not None:
            return True, header + "\n\n♻️ 已接管当前历练，走完后自动结算并继续。\n💡 /关闭代理 历练 可停止。"
        if cult_cd is not None and int(adv.get("close_until") or 0):
            return True, header + "\n\n♻️ 已接管当前闭关，休整结束后继续历练。"

        changed, msg, dirty = await self._run_adventure_cycle(user_id, player, config, origin)
        if changed or dirty:
            # 把本轮产生的 note（wait_slot / injured / wait_cd …）一并落库，
            # 否则后台每轮都会把同一句提示再推一遍。
            await self._write_state(user_id, True, "", origin, "", config)
        if msg:
            header += f"\n━━━━━━━━━━━━━━━\n{msg}"
        else:
            header += "\n\n⏳ 暂未开始历练，稍后会自动重试。"
        header += "\n💡 /关闭代理 历练 可停止。"
        return True, header

    async def disable_adventure(self, user_id: str, reason: str = "manual_close") -> Tuple[bool, str]:
        row = await self.get(user_id)
        if not row:
            return False, "❌ 道友尚未踏入仙途。"
        config = self._load_config(row)
        adv = config["adventure"]
        if not adv.get("enabled"):
            return False, "💡 历练代理当前未开启。"

        # 代理触发的闭关：关闭代理时一并结算，避免玩家被动挂在闭关里
        extra = await self._settle_auto_seclusion_if_any(user_id, adv)
        adv["enabled"] = False
        adv["close_until"] = 0
        adv["close_route"] = ""
        adv["note"] = ""
        keep_enabled = self._any_proxy_active(row.get("task_type"), config)
        await self._write_state(
            user_id,
            keep_enabled,
            row.get("task_type", ""),
            row.get("origin", ""),
            reason,
            config,
        )
        return True, "🛑 历练代理已关闭。\n当前这一轮历练不会中断。" + extra

    async def _pick_adventure_route(
        self, user_id: str, adv: dict, now: int
    ) -> Tuple[str, int]:
        """选出下一条可用路线；全部在 CD 时返回 ``("", 最早结束时间)``。"""
        primary = str(adv.get("route") or "")
        secondary = str(adv.get("route2") or "")
        if adv.get("mode") != ADV_MODE_ALTERNATE:
            secondary = ""
        routes = [k for k in (primary, secondary) if k]
        if not routes:
            return "", 0
        ends = {}
        for key in routes:
            try:
                ends[key] = int(await self.adventure_mgr.get_route_cooldown(user_id, key))
            except Exception:
                ends[key] = 0
        available = [k for k in routes if ends.get(k, 0) <= now]
        if not available:
            pending = [v for v in ends.values() if v > now]
            return "", (min(pending) if pending else 0)
        if len(routes) > 1:
            last = str(adv.get("last_route") or "")
            # 双线交叉：优先走刚结束那条以外的路线
            for key in [k for k in routes if k != last] + [k for k in routes if k == last]:
                if key in available:
                    return key, 0
        return available[0], 0

    async def _start_adv_seclusion(
        self, user_id: str, player: Any, until_ts: int, route_key: str
    ) -> Tuple[bool, str]:
        if not self._cultivation_settler:
            return False, (
                "⚠️ 自动闭关模块未就绪，历练代理将等待路线休整结束。\n"
                "💡 可稍后重试或手动 /闭关。"
            )
        now = int(time.time())
        fresh = await self.db.get_player_by_id(user_id) or player
        from ..core.avatar_rules import ensure_free_slot, total_slots

        ok_slot, slot_hint = await ensure_free_slot(self.db, fresh, user_id)
        if not ok_slot:
            return False, slot_hint

        started, slot, reason = await self.db.ext.start_activity(
            user_id, UserStatus.CULTIVATING, 0, max_slots=total_slots(fresh)
        )
        if not started and reason != "already_running":
            return False, f"⚠️ 自动闭关未能开始（{reason}）。"

        fresh.state = "修炼中"
        fresh.cultivation_start_time = now
        await self.db.update_player(fresh)
        remain = max(0, until_ts - now)
        minutes = max(1, (remain + 59) // 60)
        return True, (
            f"🧘 历练路线休整中，已自动闭关约 {minutes} 分钟；"
            f"休整结束后继续【{self._adv_route_label(route_key)}】。"
        )

    async def _settle_adv_seclusion(self, user_id: str) -> Tuple[bool, str]:
        if not self._cultivation_settler:
            return False, ""
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, ""
        ok, msg = await self._cultivation_settler(player)
        if ok and self.title_bump:
            try:
                await self.title_bump(user_id, "cultivate_count", 1)
            except Exception:
                pass
        return bool(ok), msg

    @staticmethod
    def _encounter_option_value(outcome: dict) -> float:
        if not isinstance(outcome, dict):
            return 0.0
        gold = float(outcome.get("gold", 0) or 0)
        exp = float(outcome.get("exp", 0) or 0)
        karma = float(outcome.get("karma", 0) or 0)
        luck = float(outcome.get("luck", 0) or 0)
        item = outcome.get("item") or {}
        item_bonus = 3000.0 if item else 0.0
        hp = abs(float(outcome.get("hp_pct", 0) or 0))
        # 修为与灵石大体按 1:1 折算；气血损失按比例重罚，避免自动代理把玩家送进重伤
        return gold + exp + karma * 800 + luck * 500 + item_bonus - hp * 20000

    def _choose_encounter_option(self, payload: dict, adv: dict) -> int:
        """返回奇遇选项编号：固定选项优先，0 表示智能选择。"""
        key = str(payload.get("key") or "")
        fixed = int(adv.get("encounter_choice") or 0)
        if key == "__merit__":
            # 途中善举：默认收下全部收获（2），固定选 1 时改为接济灾民
            if fixed in (1, 2):
                return fixed
            return 2
        if not self.encounter_mgr:
            return 0
        event = (getattr(self.encounter_mgr, "events", {}) or {}).get(key)
        options = (event or {}).get("options") or []
        if not options:
            return 0
        if 1 <= fixed <= len(options):
            return fixed
        success_rate = {"risk": 0.55, "evil": 0.70, "safe": 0.85, "good": 1.0}
        best_idx, best_score = 1, float("-inf")
        for idx, opt in enumerate(options, 1):
            weight = opt.get("weight")
            if weight == "leave":
                score = -1.0
            else:
                rate = success_rate.get(str(weight), 0.85)
                score = rate * self._encounter_option_value(opt.get("win") or {}) + (
                    1 - rate
                ) * self._encounter_option_value(opt.get("lose") or {})
            if score > best_score:
                best_idx, best_score = idx, score
        return best_idx

    async def _auto_pick_encounter(
        self, user_id: str, player: Any, adv: dict
    ) -> Tuple[bool, str]:
        if not self.encounter_mgr or not adv.get("auto_encounter", True):
            return False, ""
        try:
            prefix = getattr(self.encounter_mgr, "STORAGE_KEY_PREFIX", "encounter_")
            raw = await self.db.ext.get_system_config(f"{prefix}{user_id}")
        except Exception:
            return False, ""
        if not raw:
            return False, ""
        try:
            payload = json.loads(raw)
        except Exception:
            return False, ""
        if int(payload.get("expire", 0) or 0) <= int(time.time()):
            return False, ""
        choice = self._choose_encounter_option(payload, adv)
        if choice <= 0:
            return False, ""
        try:
            # 结算扫描可能刚给玩家加过历练奖励，这里必须用最新档，
            # 否则 resolve_choice 写回会覆盖掉刚入账的修为/灵石。
            fresh = await self.db.get_player_by_id(user_id) or player
            ok, msg = await self.encounter_mgr.resolve_choice(fresh, choice)
        except Exception as e:
            logger.warning(f"【自动代理】{user_id} 奇遇自动选择失败: {e}")
            return False, ""
        if not ok:
            return False, ""
        return True, f"✨ 自动奇遇：已按 [{choice}] 结算\n{msg}"

    async def _run_adventure_cycle(
        self, user_id: str, player: Any, config: dict, origin: str = ""
    ) -> Tuple[bool, str, bool]:
        """执行一次历练代理回合；返回 ``(是否有动作, 消息, 配置是否变化)``。"""
        adv = config["adventure"]
        if not adv.get("enabled") or not self.adventure_mgr:
            return False, "", False

        now = int(time.time())
        dirty = False
        messages: list[str] = []

        enc_changed, enc_msg = await self._auto_pick_encounter(user_id, player, adv)
        if enc_changed:
            dirty = True
            messages.append(enc_msg)

        # 只看「历练」那一槽：本体在秘境 / 分身在押镖时，不能因为
        # first_busy_cd 读到别的活动就误判成「已在历练」或「别的活动在跑」。
        try:
            adv_cd = await self.db.ext.get_active_cd(user_id, UserStatus.ADVENTURING)
        except Exception:
            adv_cd = None
        if adv_cd is not None:
            return enc_changed, "\n".join(messages), dirty

        prefix = ""
        close_until = int(adv.get("close_until") or 0)
        if close_until:
            try:
                cult_cd = await self.db.ext.get_active_cd(user_id, UserStatus.CULTIVATING)
            except Exception:
                cult_cd = None
            if cult_cd is not None:
                if now < close_until:
                    # 代理自己安排的闭关还没到点，静默等
                    return enc_changed, "\n".join(messages), dirty
                _ok, cult_msg = await self._settle_adv_seclusion(user_id)
                prefix = cult_msg
            # 到这里说明：闭关已结算，或玩家已手动出关（残留标记）
            adv["close_until"] = 0
            adv["close_route"] = ""
            dirty = True

        route_key, wait_until = await self._pick_adventure_route(user_id, adv, now)
        if not route_key:
            remain = max(0, wait_until - now)
            if not wait_until:
                adv["enabled"] = False
                adv["note"] = "no_route"
                message = "⚠️ 找不到可用的历练路线，历练代理已关闭。"
                if prefix:
                    message = f"{prefix}\n\n{message}"
                if messages:
                    message = "\n".join(messages) + "\n\n" + message
                return True, message, True

            if adv.get("retreat", True) and remain >= ADV_MIN_RETREAT_SECONDS:
                started, seclusion_msg = await self._start_adv_seclusion(
                    user_id, player, wait_until, adv.get("close_route") or adv.get("route")
                )
                if started:
                    adv["close_until"] = wait_until
                    adv["close_route"] = adv.get("route") or ""
                    adv["note"] = ""
                    message = seclusion_msg
                    if prefix:
                        message = f"{prefix}\n\n{message}"
                    if messages:
                        message = "\n".join(messages) + "\n\n" + message
                    return True, message, True
                if adv.get("note") != "cultivate_failed":
                    adv["note"] = "cultivate_failed"
                    message = seclusion_msg
                    if prefix:
                        message = f"{prefix}\n\n{message}"
                    return True, message, True
                return enc_changed, "\n".join(messages), dirty

            # 不闭关：静默等待，只在首次进入等待时提示一次
            if adv.get("note") != "wait_cd":
                adv["note"] = "wait_cd"
                minutes = max(1, (remain + 59) // 60)
                message = f"⏳ 历练路线休整中，约 {minutes} 分钟后继续。"
                if prefix:
                    message = f"{prefix}\n\n{message}"
                if messages:
                    message = "\n".join(messages) + "\n\n" + message
                return True, message, True
            return enc_changed, "\n".join(messages), dirty

        # 有可用路线：受伤时不硬闯，等恢复后自动继续
        try:
            config_manager = getattr(self.escort_mgr, "config_manager", None)
            if config_manager is not None:
                from .recovery_manager import RecoveryManager

                fresh = await self.db.get_player_by_id(user_id) or player
                if await RecoveryManager(self.db, config_manager).is_injured(fresh):
                    if adv.get("note") != "injured":
                        adv["note"] = "injured"
                        message = (
                            "⚠️ 当前身受重伤（气血不足 20%），历练代理暂停出发。\n"
                            "💡 恢复后会自动继续。"
                        )
                        if prefix:
                            message = f"{prefix}\n\n{message}"
                        return True, message, True
                    return enc_changed, "\n".join(messages), dirty
        except Exception as e:
            logger.warning(f"【自动代理】历练受伤检查失败: {e}")

        fresh = await self.db.get_player_by_id(user_id) or player
        if await self._free_slots(user_id, fresh) <= 0:
            # 分身都出去了：等一个槽位，别把「没空位」当成配置错误关代理
            if adv.get("note") != "wait_slot":
                adv["note"] = "wait_slot"
                names = await self.db.ext.busy_activity_names(user_id)
                message = (
                    f"⏳ 分身都在忙（{'、'.join(names) or '其它活动'}），"
                    "历练代理会等一个槽位空出来后继续。"
                )
                if prefix:
                    message = f"{prefix}\n\n{message}"
                if messages:
                    message = "\n".join(messages) + "\n\n" + message
                return True, message, True
            return enc_changed, "\n".join(messages), dirty

        ok, start_msg = await self.adventure_mgr.start_adventure(
            user_id, route_key, umo=origin
        )
        if not ok:
            # CD 竞态：不改配置，下轮重试
            if "休整" in str(start_msg):
                return enc_changed, "\n".join(messages), dirty
            adv["enabled"] = False
            adv["note"] = "start_failed"
            message = (
                f"⚠️ 自动开始历练【{self._adv_route_label(route_key)}】失败：\n{start_msg}\n"
                "🛑 历练代理已暂停，发送 /自动代理 历练 <路线> 可重新开启。"
            )
            if prefix:
                message = f"{prefix}\n\n{message}"
            if messages:
                message = "\n".join(messages) + "\n\n" + message
            return True, message, True

        adv["last_route"] = route_key
        adv["close_until"] = 0
        adv["close_route"] = ""
        adv["note"] = ""
        dirty = True
        message = (
            f"🤖 历练代理：已开始【{self._adv_route_label(route_key)}】\n{start_msg}"
        )
        if prefix:
            message = f"{prefix}\n\n{message}"
        if messages:
            message = "\n".join(messages) + "\n\n" + message
        return True, message, True

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
        if raw_task and self._is_adventure_request(raw_task):
            return await self._enable_adventure(user_id, origin, raw_task)

        requested = self.normalize_task(raw_task) if raw_task else None
        if raw_task and not requested:
            return False, (
                "❌ 暂不支持该活动。\n"
                "💡 目前自动代理支持：/自动代理 押镖、/自动代理 宗门任务、"
                "/自动代理 灵田、/自动代理 秘境、/自动代理 施肥、/自动代理 历练"
            )

        row = await self.get(user_id) or {}
        config = self._load_config(row)
        rift_closed = bool(config["rift"].get("enabled"))
        if rift_closed:
            config["rift"]["enabled"] = False
        adv_closed = bool(config["adventure"].get("enabled"))
        adv_settle_note = ""
        if adv_closed:
            adv_settle_note = await self._settle_auto_seclusion_if_any(
                user_id, config["adventure"]
            )
            config["adventure"]["enabled"] = False
            config["adventure"]["close_until"] = 0
            config["adventure"]["close_route"] = ""
        close_notes = []
        if rift_closed:
            close_notes.append("秘境代理已关闭")
        if adv_closed:
            close_notes.append("历练代理已关闭")
        rift_note = f"\n（{'；'.join(close_notes)}）" if close_notes else ""
        rift_note += adv_settle_note
        # 省略参数时按「正在进行的押镖/宗门任务」补全（多槽位取最近开始的那项）
        current = None
        current_cd = None
        newest = -1
        for task, active_cd in await self._active_primary(user_id):
            started = int(getattr(active_cd, "create_time", 0) or 0)
            if started >= newest:
                current, current_cd, newest = task, active_cd, started

        if not raw_task:
            if not current:
                return False, (
                    "❌ 请指定要代理的内容。\n"
                    "💡 /自动代理 押镖\n"
                    "💡 /自动代理 宗门任务\n"
                    "💡 /自动代理 灵田 [格号...]\n"
                    "💡 /自动代理 秘境 <ID>\n"
                    "💡 /自动代理 施肥 [阈值%]\n"
                    "💡 /自动代理 历练 <路线> [备用路线] [奇遇N]\n"
                    "（仅当已处于押镖/宗门任务中时，可省略参数自动补全）"
                )
            requested = current

        was_enabled = bool(row.get("enabled"))
        origin = origin or row.get("origin", "")
        if not current:
            # 没有可接管的押镖/宗门任务：若分身全在忙别的，先说清楚是谁占着
            busy_cd = await self.db.ext.first_busy_cd(user_id)
            if await self._free_slots(user_id, player) <= 0:
                names = await self.db.ext.busy_activity_names(user_id)
                busy_name = "、".join(names) or (
                    UserStatus.get_name(busy_cd.type)
                    if busy_cd
                    else (getattr(player, "state", "") or "忙碌")
                )
                return False, (
                    f"❌ 你当前正在{busy_name}，分身都出去了，暂时接不了新任务。\n"
                    f"💡 等一项活动结束，或先收掉一项后再发送 /自动代理 押镖 或 /自动代理 宗门任务。"
                )

        preferred = requested or current or row.get("task_type", "")
        await self._write_state(user_id, True, preferred, origin, "", config)

        if current:
            if requested and requested != current:
                return True, (
                    f"🤖 自动代理已开启\n"
                    f"当前正在{UserStatus.get_name(current_cd.type)}，先完成本轮；"
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
        try:
            cds = [
                cd
                for cd in await self.db.ext.list_user_cds(user_id)
                if int(cd.type or 0) != UserStatus.IDLE
            ]
        except Exception:
            cds = []
        config = self._load_config(row)
        farm_cfg = config["farm"]
        farm_on = bool(farm_cfg.get("enabled"))
        rift_cfg = config["rift"]
        rift_on = bool(rift_cfg.get("enabled"))
        fert_cfg = config["fertilize"]
        fert_on = bool(fert_cfg.get("enabled"))
        adv_cfg = config["adventure"]
        adv_on = bool(adv_cfg.get("enabled"))

        if not row.get("enabled"):
            last = row.get("task_type")
            suffix = f"\n上次代理活动：{self.task_name(last)}" if last else ""
            return (
                "🤖 自动代理：未开启\n"
                "━━━━━━━━━━━━━━━\n"
                "发送 /自动代理 押镖、/自动代理 宗门任务、/自动代理 灵田、"
                "/自动代理 秘境、/自动代理 施肥 或 /自动代理 历练 <路线> 开启。"
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
            prefix_len = self._plot_prefix_length(raw_plots)
            if (
                raw_plots is not None
                and max_slots
                and prefix_len
                and prefix_len < max_slots
            ):
                lines.append(
                    f"💡 格{prefix_len + 1}-{max_slots} 未纳入代理，"
                    "发送 /自动代理 灵田 全部格 可覆盖"
                )
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
        if adv_on:
            route_text = self._adv_route_label(adv_cfg.get("route"))
            if adv_cfg.get("mode") == ADV_MODE_ALTERNATE and adv_cfg.get("route2"):
                route_text += " ⇄ " + self._adv_route_label(adv_cfg.get("route2"))
            detail = route_text
            if adv_cfg.get("retreat", True):
                detail += "，CD 自动闭关"
            if int(adv_cfg.get("encounter_choice") or 0) > 0:
                detail += f"，奇遇固定[{adv_cfg.get('encounter_choice')}]"
            elif adv_cfg.get("auto_encounter", True):
                detail += "，奇遇自动"
            lines.append(f"历练代理：运行中（{detail}）")
        if row.get("task_type"):
            lines.append(f"目标活动：{self.task_name(row.get('task_type'))}")
        elif not farm_on and not rift_on and not fert_on and not adv_on:
            lines.append("目标活动：等待选择")

        # 多槽位：逐条列出本体/化身各在做什么，而不是只看「第一条」
        from ..core.avatar_rules import slot_label

        now_ts = int(time.time())
        for active_cd in cds:
            cd_type = int(active_cd.type or 0)
            slot_text = slot_label(int(getattr(active_cd, "slot", 0) or 0))
            remaining = max(0, int(getattr(active_cd, "scheduled_time", 0) or 0) - now_ts)
            if STATUS_TASK.get(cd_type):
                task = STATUS_TASK[cd_type]
                if remaining > 0:
                    text = f"当前进行：{self.task_name(task)}（剩余 {remaining // 60} 分 {remaining % 60} 秒）"
                else:
                    text = f"当前进行：{self.task_name(task)}（可结算，等待代理处理）"
                if row.get("task_type") and row.get("task_type") != task:
                    text += f"，下一轮改跑【{self.task_name(row.get('task_type'))}】"
            elif cd_type == UserStatus.ADVENTURING:
                route_key = ""
                try:
                    if hasattr(active_cd, "get_extra_data"):
                        extra = active_cd.get_extra_data() or {}
                    else:
                        extra = json.loads(getattr(active_cd, "extra_data", "{}") or "{}")
                    route_key = str((extra or {}).get("route_key") or "")
                except Exception:
                    route_key = ""
                label = self._adv_route_label(route_key) if route_key else "历练"
                if adv_on:
                    if remaining > 0:
                        text = f"当前进行：历练「{label}」（剩余 {remaining // 60} 分 {remaining % 60} 秒）"
                    else:
                        text = f"当前进行：历练「{label}」（已走完，等待结算）"
                else:
                    text = f"当前状态：历练中「{label}」（历练代理未开启）"
            elif cd_type == UserStatus.CULTIVATING and adv_on:
                close_until = int(adv_cfg.get("close_until") or 0)
                if close_until:
                    wait = max(0, close_until - now_ts)
                    route_label = self._adv_route_label(adv_cfg.get("route"))
                    text = (
                        f"当前进行：历练 CD 自动闭关（剩余 {wait // 60} 分 {wait % 60} 秒，"
                        f"之后继续「{route_label}」）"
                    )
                else:
                    text = "当前状态：闭关中（历练代理等待出关）"
            elif cd_type == UserStatus.EXPLORING:
                if rift_on:
                    if remaining > 0:
                        text = f"当前进行：探索秘境（剩余 {remaining // 60} 分 {remaining % 60} 秒）"
                    else:
                        text = "当前进行：探索秘境（可结算，等待代理处理）"
                else:
                    text = "当前状态：探索秘境中（秘境代理未开启）"
            else:
                text = f"当前状态：{UserStatus.get_name(cd_type)}"
            lines.append(f"{text} · {slot_text}")

        if not cds and self._legacy_busy(player):
            lines.append(f"当前状态：{getattr(player, 'state', '') or '忙碌'}")
        elif not cds and rift_on:
            lines.append("当前状态：秘境代理运行中，等待空闲后进入秘境")
        elif not cds and adv_on:
            lines.append("当前状态：历练代理运行中，等待空闲后出发")
        elif not cds and farm_on:
            lines.append("当前状态：灵田代理运行中，等待成熟")
        elif not cds and fert_on:
            lines.append("当前状态：自动施肥运行中，肥力不足时补肥")
        elif not cds:
            lines.append("当前状态：空闲（等待下一轮）")

        if row.get("last_error") == "start_failed":
            lines.append("⚠️ 上次自动开始失败，已暂停")
        elif row.get("last_error") == "wait_slot":
            lines.append("⏳ 分身都在忙，押镖/宗门任务代理等待槽位空出")
        if adv_on and adv_cfg.get("note") == "injured":
            lines.append("⚠️ 身受重伤，历练代理等待恢复后继续")
        elif adv_on and adv_cfg.get("note") == "wait_cd":
            lines.append("⏳ 历练路线休整中，等待结束后继续")
        elif adv_on and adv_cfg.get("note") == "wait_slot":
            lines.append("⏳ 分身都在忙，历练代理等待槽位空出")
        lines.append(
            "💡 /关闭代理 押镖|宗门任务|灵田 [格号]|秘境|施肥|历练|全部 停止对应代理"
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

    async def _notify(self, origin: str, msg: str, user_id: str = ""):
        if not origin or not self.notifier:
            return
        try:
            # 新回调带 user_id，老回调（测试/外部实现）仍按两参调用
            await self.notifier(origin, msg, user_id)
        except TypeError:
            try:
                await self.notifier(origin, msg)
            except Exception as e:
                logger.warning(f"【自动代理】消息发送失败: {e}")
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
        adv_cfg = config["adventure"]
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
                await self._notify(origin, fert_msg, user_id)
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
                await self._notify(origin, farm_msg, user_id)
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
                    await self._notify(origin, rift_msg, user_id)
                handled = True

        # 历练代理：空闲时出发；路线 CD 时按配置闭关或换线，并自动处理奇遇
        if adv_cfg.get("enabled") and self.adventure_mgr:
            changed, adv_msg, adv_dirty = await self._run_adventure_cycle(
                user_id, player, config, origin
            )
            if changed or adv_dirty:
                keep_enabled = self._any_proxy_active(row.get("task_type"), config)
                await self._write_state(
                    user_id,
                    keep_enabled,
                    row.get("task_type", ""),
                    origin,
                    row.get("last_error", ""),
                    config,
                )
            if changed:
                if adv_msg:
                    await self._notify(origin, adv_msg, user_id)
                handled = True

        # 押镖/宗门任务可能各占一个槽位（本体 + 化身），逐个结算/续接
        active = await self._active_primary(user_id)
        farm_on = bool(farm_cfg.get("enabled"))
        keep_text = "、".join(
            name
            for name, on in (
                ("灵田", farm_on),
                ("秘境", bool(rift_cfg.get("enabled"))),
                ("施肥", bool(fert_cfg.get("enabled"))),
                ("历练", bool(adv_cfg.get("enabled"))),
            )
            if on
        )

        if active:
            # 只开启灵田代理时，不接管玩家手动开始/尚未结算的押镖、宗门任务；
            # 否则“关闭押镖代理”后，当前这轮结束又会被自动续接。
            if not row.get("task_type"):
                return handled

            for current, cd in active:
                if cd and cd.scheduled_time and now < int(cd.scheduled_time):
                    continue

                fresh_player = await self.db.get_player_by_id(user_id) or player
                ok, finish_msg = await self.finish_activity(fresh_player, current)
                if not ok:
                    await self._notify(
                        origin,
                        f"⚠️ 自动代理结算【{self.task_name(current)}】失败：\n{finish_msg}\n"
                        f"🛑 押镖/宗门任务代理已暂停，发送 /自动代理 押镖 或 /自动代理 宗门任务 可重试。",
                        user_id,
                    )
                    await self._disable_primary(user_id, "finish_failed")
                    return True

                # task_type 是“下一轮目标”：玩家可能在本轮进行中主动改绑了活动
                target = row.get("task_type") or current
                target_status = TASK_STATUS.get(target)
                if target_status and await self.db.ext.get_active_cd(user_id, target_status) is not None:
                    # 目标活动已经在另一个分身的槽位里跑着（押镖 + 宗门任务并存），
                    # 不能再开一轮，否则会被管理器判成“进行中”而误关代理
                    await self._notify(origin, finish_msg, user_id)
                    handled = True
                    continue
                fresh_player = await self.db.get_player_by_id(user_id) or player
                ok, start_msg = await self.start_activity(fresh_player, target)
                if not ok:
                    if "分身都出去了" in (start_msg or ""):
                        # 结算完这一轮后，槽位暂时被别的活动占满：保留代理等下一轮
                        await self._notify(
                            origin,
                            f"{finish_msg}\n\n"
                            f"⏳ 暂时没有空闲分身，【{self.task_name(target)}】下一轮稍后自动继续。",
                            user_id,
                        )
                        handled = True
                        continue
                    await self._notify(
                        origin,
                        f"{finish_msg}\n\n"
                        f"⚠️ 自动代理无法开始下一轮【{self.task_name(target)}】：\n{start_msg}\n"
                        f"🛑 押镖/宗门任务代理已暂停，发送 /自动代理 押镖 或 /自动代理 宗门任务 可重新开启。",
                        user_id,
                    )
                    await self._disable_primary(user_id, "start_failed")
                    return True

                await self._write_state(
                    user_id, True, row.get("task_type", ""), origin, "", config
                )
                await self._notify(
                    origin,
                    f"{finish_msg}\n\n"
                    f"🤖 自动代理：已开始下一轮【{self.task_name(target)}】\n"
                    f"{start_msg}",
                    user_id,
                )
                handled = True
            return handled

        if self._legacy_busy(player) and await self.db.ext.busy_slot_count(user_id) <= 0:
            # 兼容旧数据：user_cd 未建立但 players.state 仍显示忙碌
            # （多槽位下 players.state 由槽位推算，必须确认账本里真的一条都没有）
            if not row.get("task_type"):
                return handled
            await self._notify(
                origin,
                f"🛑 检测到你进入了{getattr(player, 'state', '') or '忙碌'}，"
                + (
                    f"押镖/宗门任务代理已暂停，{keep_text}代理继续运行。"
                    if keep_text
                    else "自动代理已关闭。发送 /自动代理 押镖 或 /自动代理 宗门任务 可重新开启。"
                ),
                user_id,
            )
            await self._disable_primary(user_id, "other_busy")
            return True

        task_type = row.get("task_type", "")
        if not task_type:
            return handled

        # 分身都在别的活动里（闭关/历练/秘境…）：等一个槽位，别关代理
        if await self._free_slots(user_id, player) <= 0:
            if row.get("last_error") != "wait_slot":
                await self._write_state(user_id, True, task_type, origin, "wait_slot", config)
                names = await self.db.ext.busy_activity_names(user_id)
                await self._notify(
                    origin,
                    f"⏳ 分身都在忙（{'、'.join(names) or '其它活动'}），"
                    f"【{self.task_name(task_type)}】代理会等一个槽位空出来后继续。",
                    user_id,
                )
                return True
            return handled

        ok, start_msg = await self.start_activity(player, task_type)
        if not ok:
            if "分身都出去了" in (start_msg or ""):
                # 竞态：刚好被别人占了最后一个槽位，下一轮再试
                return handled
            await self._notify(
                origin,
                f"⚠️ 自动代理无法开始【{self.task_name(task_type)}】：\n{start_msg}\n"
                f"🛑 押镖/宗门任务代理已暂停，发送 /自动代理 押镖 或 /自动代理 宗门任务 可重新开启。",
                user_id,
            )
            await self._disable_primary(user_id, "start_failed")
            return True

        await self._write_state(user_id, True, task_type, origin, "", config)
        await self._notify(
            origin,
            f"🤖 自动代理：已开始新一轮【{self.task_name(task_type)}】\n{start_msg}",
            user_id,
        )
        return True
