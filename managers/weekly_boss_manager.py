# managers/weekly_boss_manager.py
"""周常 Boss（周本）管理器。

与「世界 Boss / 团本」完全独立的一周三次高难团队玩法：

* 天道压制：按基准境界模板重算气血/双攻/双防，只保留 ±15% 养成差异；
* 20 回合团队长线战：真实伤害 AoE、崩山点杀、噬元禁疗、护体盾、软/硬狂暴；
* 战前携带丹药 + 战中自动触发，真实扣除丹药背包；
* 通关后按实际伤害占比分配灵石、材料、装备与专属材料。

设计文档见 ``docs/周常BOSS评估与设计.md``。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from ..data import DataBase
from ..models import Player
from .combat_manager import CombatManager, CombatStats

# 周本 Boss 池：每周期按 cycle_id 稳定抽取一个
WEEKLY_BOSS_TYPES: Dict[str, dict] = {
    "void_sovereign": {
        "name": "虚空吞界者", "element": "水", "icon": "🌌",
        "desc": "自界外裂隙降临的吞界古神，所过之处连天道都被啃噬。",
    },
    "heaven_punisher": {
        "name": "天罚刑主", "element": "金", "icon": "⚡",
        "desc": "执掌雷罚的刑主，以天雷为鞭，专诛逆天而行者。",
    },
    "nether_emperor": {
        "name": "九幽噬魂帝", "element": "木", "icon": "👁️",
        "desc": "统御九幽怨魂的帝君，一息之间可抽干一域生机。",
    },
    "flame_devourer": {
        "name": "焚世业火君", "element": "火", "icon": "🔥",
        "desc": "背负众生业火的魔君，焚尽万物只为铸就自身道基。",
    },
    "earth_warden": {
        "name": "镇岳古神", "element": "土", "icon": "🗿",
        "desc": "沉睡万古的镇岳古神，一怒则山岳倾覆、大地陆沉。",
    },
}

# 品阶 → 恢复丹兜底恢复量（丹药自身带 restore_hp_percent 时优先用自身数值）
RANK_HEAL = {
    "凡品": 0.10, "黄品": 0.17, "玄品": 0.20, "灵品": 0.22,
    "地品": 0.30, "天品": 0.42, "皇品": 0.55, "圣品": 0.65,
    "帝品": 0.70, "道品": 0.85, "仙品": 0.95, "混元先天": 1.0,
}

# 战中治疗递减：第 1~N 枚依次 100% / 90% / ...，最低 40%
HEAL_DECAY = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]

PREP_MULTIPLIER_KEYS = (
    "physical_damage_multiplier", "magic_damage_multiplier",
    "physical_defense_multiplier", "magic_defense_multiplier",
)


class WeeklyBossManager:
    """周常 Boss 管理器。"""

    # 周本特效硬上限（设计值，避免装备/功法无限叠加）
    CRIT_RATE_TOTAL_CAP = 60.0
    CRIT_DAMAGE_TOTAL_CAP = 2.5
    LIFESTEAL_TOTAL_CAP = 20.0

    def __init__(
        self,
        db: DataBase,
        config_manager,
        storage_ring_mgr,
        world_boss_mgr=None,
        title_mgr=None,
    ):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_mgr = storage_ring_mgr
        self.world_boss_mgr = world_boss_mgr
        self.title_mgr = title_mgr

        cfg = {}
        if config_manager is not None:
            cfg = (getattr(config_manager, "boss_config", {}) or {}).get("weekly_boss", {}) or {}
        self._cfg = cfg if isinstance(cfg, dict) else {}

        self._pill_mgr = None
        self._pending: Dict[str, dict] = {}
        self._template_cache: Dict[int, dict] = {}
        self._d0_cache: Dict[int, int] = {}
        self.last_broadcast_msg: Optional[str] = None

    # ================= 配置 =================

    def _get(self, key: str, default):
        value = self._cfg.get(key, default)
        return default if value is None else value

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._get(key, default))
        except (TypeError, ValueError):
            return int(default)

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._get(key, default))
        except (TypeError, ValueError):
            return float(default)

    @property
    def enabled(self) -> bool:
        return bool(self._get("enabled", True))

    @property
    def min_level(self) -> int:
        return max(0, self._cfg_int("min_level", 13))

    @property
    def max_players(self) -> int:
        return max(1, min(8, self._cfg_int("max_players", 8)))

    @property
    def attempts_per_week(self) -> int:
        return max(1, self._cfg_int("attempts_per_week", 3))

    @property
    def hp_unit(self) -> float:
        return max(1.0, self._cfg_float("hp_unit", 40.0))

    @property
    def hp_exponent(self) -> float:
        return max(0.0, self._cfg_float("hp_exponent", 0.65))

    @property
    def solo_hp_mult(self) -> float:
        return max(1.0, self._cfg_float("solo_hp_mult", 1.3))

    @property
    def solo_damage_mult(self) -> float:
        return max(1.0, self._cfg_float("solo_damage_mult", 1.25))

    @property
    def soft_enrage_round(self) -> int:
        return max(2, self._cfg_int("soft_enrage_round", 16))

    @property
    def hard_enrage_round(self) -> int:
        return max(self.soft_enrage_round + 1, self._cfg_int("hard_enrage_round", 20))

    @property
    def pill_slots_prepare(self) -> int:
        return max(0, min(6, self._cfg_int("pill_slots_prepare", 6)))

    @property
    def pill_slots_combat(self) -> int:
        return max(0, min(6, self._cfg_int("pill_slots_combat", 6)))

    @property
    def _reward_cfg(self) -> dict:
        raw = self._get("rewards", {}) or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def _pill_caps(self) -> dict:
        raw = self._get("pill_buff_caps", {}) or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def exclusive_material(self) -> str:
        return str(self._reward_cfg.get("exclusive_material", "天外陨核") or "天外陨核")

    # ================= 基础工具 =================

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()) -> List[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    def _safe_json(raw, default):
        try:
            data = json.loads(raw) if raw else default
        except (TypeError, ValueError):
            return default
        return data if isinstance(data, type(default)) else default

    @staticmethod
    def _fmt(value: int) -> str:
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return "0"

    @staticmethod
    def _pct(value: float, digits: int = 1) -> str:
        return f"{value * 100:.{digits}f}%"

    def _pill_manager(self):
        if self._pill_mgr is None:
            from ..core.pill_manager import PillManager

            self._pill_mgr = PillManager(self.db, self.config_manager)
        return self._pill_mgr

    # ================= 周期 =================

    def _cycle_window(self, now: Optional[int] = None) -> Tuple[int, int]:
        now = int(now or time.time())
        weekday = self._cfg_int("refresh_weekday", 1) % 7
        hour = self._cfg_int("refresh_hour", 0) % 24
        minute = self._cfg_int("refresh_minute", 5) % 60
        duration_days = max(1, self._cfg_int("duration_days", 7))

        current = datetime.fromtimestamp(now)
        days_since = (current.weekday() - weekday) % 7
        start = (current - timedelta(days=days_since)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if start.timestamp() > now:
            start -= timedelta(days=7)
        end = start + timedelta(days=duration_days)
        return int(start.timestamp()), int(end.timestamp())

    async def _resolve_baseline_level(self) -> int:
        mode = str(self._get("baseline_mode", "server_p75") or "server_p75")
        fixed = max(0, self._cfg_int("baseline_level", 22))
        if mode == "fixed":
            return fixed
        try:
            rows = await self._fetchall(
                "SELECT level_index FROM players WHERE level_index >= ? ORDER BY level_index",
                (self.min_level,),
            )
            levels = [int(row.get("level_index") or 0) for row in rows]
        except Exception:
            levels = []
        if not levels:
            return fixed
        index = min(len(levels) - 1, int(len(levels) * 0.75))
        level = max(self.min_level, levels[index])
        max_level = 35
        try:
            if getattr(self.config_manager, "level_data", None):
                max_level = len(self.config_manager.level_data) - 1
        except Exception:
            pass
        return max(0, min(max_level, level))

    @staticmethod
    def _pick_boss_type(cycle_id: str) -> str:
        digest = hashlib.md5(cycle_id.encode("utf-8")).hexdigest()
        keys = sorted(WEEKLY_BOSS_TYPES.keys())
        return keys[int(digest[:8], 16) % len(keys)]

    async def ensure_current_cycle(self, now: Optional[int] = None) -> dict:
        """返回当前周期；跨周或周期缺失时自动创建。"""
        now = int(now or time.time())
        start, end = self._cycle_window(now)
        cycle_id = datetime.fromtimestamp(start).strftime("%Y-%m-%d")

        row = await self._fetchone(
            "SELECT * FROM weekly_boss_cycles WHERE cycle_id = ?", (cycle_id,)
        )
        if row:
            return row

        baseline = await self._resolve_baseline_level()
        boss_type = self._pick_boss_type(cycle_id)
        meta = WEEKLY_BOSS_TYPES[boss_type]
        await self.db.conn.execute(
            "UPDATE weekly_boss_cycles SET status = 2 WHERE status = 1"
        )
        await self.db.conn.execute(
            """
            INSERT INTO weekly_boss_cycles
                (cycle_id, boss_type, boss_name, element, baseline_level,
                 start_time, end_time, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                cycle_id, boss_type, meta["name"], meta["element"],
                int(baseline), start, end, now,
            ),
        )
        await self.db.conn.commit()
        logger.info(f"【周本】新周期 {cycle_id} 已开启：{meta['name']}，基准境界 {baseline}")
        row = await self._fetchone(
            "SELECT * FROM weekly_boss_cycles WHERE cycle_id = ?", (cycle_id,)
        )
        return row or {
            "cycle_id": cycle_id, "boss_type": boss_type, "boss_name": meta["name"],
            "element": meta["element"], "baseline_level": baseline,
            "start_time": start, "end_time": end, "status": 1,
        }

    async def refresh_cycle(self) -> Tuple[bool, str]:
        """管理员手动刷新本周 Boss：清空本周榜单/领奖，重建周期。"""
        now = int(time.time())
        start, end = self._cycle_window(now)
        cycle_id = datetime.fromtimestamp(start).strftime("%Y-%m-%d")
        baseline = await self._resolve_baseline_level()
        boss_type = self._pick_boss_type(cycle_id)
        meta = WEEKLY_BOSS_TYPES[boss_type]

        await self.db.conn.execute(
            "UPDATE weekly_boss_runs SET status = 4 WHERE cycle_id = ? AND status IN (0, 1)",
            (cycle_id,),
        )
        await self.db.conn.execute(
            "UPDATE weekly_boss_cycles SET status = 2 WHERE cycle_id != ? AND status = 1",
            (cycle_id,),
        )
        await self.db.conn.execute(
            "DELETE FROM weekly_boss_leaderboard WHERE cycle_id = ?", (cycle_id,)
        )
        await self.db.conn.execute(
            "DELETE FROM weekly_boss_claims WHERE cycle_id = ?", (cycle_id,)
        )
        await self.db.conn.execute(
            "DELETE FROM weekly_boss_attempts WHERE cycle_id = ?", (cycle_id,)
        )
        exists = await self._fetchone(
            "SELECT cycle_id FROM weekly_boss_cycles WHERE cycle_id = ?", (cycle_id,)
        )
        if exists:
            await self.db.conn.execute(
                """
                UPDATE weekly_boss_cycles
                   SET boss_type = ?, boss_name = ?, element = ?, baseline_level = ?,
                       start_time = ?, end_time = ?, status = 1
                 WHERE cycle_id = ?
                """,
                (
                    boss_type, meta["name"], meta["element"], baseline,
                    start, end, cycle_id,
                ),
            )
        else:
            await self.db.conn.execute(
                """
                INSERT INTO weekly_boss_cycles
                    (cycle_id, boss_type, boss_name, element, baseline_level,
                     start_time, end_time, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    cycle_id, boss_type, meta["name"], meta["element"],
                    baseline, start, end, now,
                ),
            )
        await self.db.conn.commit()
        self._d0_cache.clear()
        self._template_cache.clear()
        return True, (
            f"🔄 周本已刷新：{meta['name']}（{meta['element']}属性），"
            f"基准境界 level {baseline}，本周榜单与领奖记录已重置。"
        )

    async def end_cycle(self) -> Tuple[bool, str]:
        now = int(time.time())
        cycle = await self.ensure_current_cycle(now)
        await self.db.conn.execute(
            "UPDATE weekly_boss_runs SET status = 4 WHERE cycle_id = ? AND status IN (0, 1)",
            (cycle["cycle_id"],),
        )
        await self.db.conn.execute(
            "UPDATE weekly_boss_cycles SET status = 2, end_time = ? WHERE cycle_id = ?",
            (now, cycle["cycle_id"]),
        )
        await self.db.conn.commit()
        return True, f"🛑 本周周本已结束（{cycle.get('boss_name', '')}），下周刷新后重新开启。"

    async def expire_runs(self, now: Optional[int] = None) -> int:
        """清理长时间未开始的准备队伍，避免占位。"""
        now = int(now or time.time())
        timeout = max(300, self._cfg_int("prepare_timeout_seconds", 7200))
        async with self.db.conn.execute(
            "UPDATE weekly_boss_runs SET status = 4 WHERE status = 0 AND created_at <= ?",
            (now - timeout,),
        ) as cursor:
            changed = cursor.rowcount
        if changed:
            await self.db.conn.commit()
        return changed

    def _entry_open(self, cycle: dict, now: Optional[int] = None) -> bool:
        if int(cycle.get("status") or 0) != 1:
            return False
        now = int(now or time.time())
        close_seconds = max(0, self._cfg_int("entry_close_minutes", 5)) * 60
        return now < int(cycle.get("end_time") or 0) - close_seconds

    # ================= 队伍 =================

    async def _cycle_attempts_used(self, cycle_id: str, user_id: str) -> int:
        row = await self._fetchone(
            "SELECT used FROM weekly_boss_attempts WHERE cycle_id = ? AND user_id = ?",
            (cycle_id, user_id),
        )
        try:
            return int(row.get("used") or 0) if row else 0
        except (TypeError, ValueError):
            return 0

    async def _has_claimed(self, cycle_id: str, user_id: str) -> bool:
        row = await self._fetchone(
            "SELECT 1 AS ok FROM weekly_boss_claims WHERE cycle_id = ? AND user_id = ?",
            (cycle_id, user_id),
        )
        return bool(row)

    async def _find_member_run(self, cycle_id: str, user_id: str, status: int) -> Optional[dict]:
        return await self._fetchone(
            """
            SELECT r.* FROM weekly_boss_runs r
              JOIN weekly_boss_members m ON m.run_id = r.run_id
             WHERE r.cycle_id = ? AND m.user_id = ? AND r.status = ?
             ORDER BY r.run_id DESC LIMIT 1
            """,
            (cycle_id, user_id, status),
        )

    async def _run_members(self, run_id: int) -> List[dict]:
        return await self._fetchall(
            "SELECT * FROM weekly_boss_members WHERE run_id = ? ORDER BY joined_at ASC",
            (run_id,),
        )

    async def create_team(self, player: Player, team_name: str = "") -> Tuple[bool, str]:
        if not self.enabled:
            return False, "❌ 周本玩法当前未开启。"
        if int(player.level_index or 0) < self.min_level:
            return False, f"❌ 周本门槛为 level {self.min_level}（金丹期），当前境界不足。"

        cycle = await self.ensure_current_cycle()
        if not self._entry_open(cycle):
            return False, "❌ 本周周本即将结束（结束前 5 分钟停止入场），请等待下周刷新。"
        active = await self._find_member_run(cycle["cycle_id"], player.user_id, 0)
        if active:
            return False, f"❌ 你已在准备中的队伍 #{active['run_id']}，请先 /周本离开。"
        fighting = await self._find_member_run(cycle["cycle_id"], player.user_id, 1)
        if fighting:
            return False, "❌ 你有一场周本战斗尚未结算，请稍后重试。"
        used = await self._cycle_attempts_used(cycle["cycle_id"], player.user_id)
        if used >= self.attempts_per_week:
            return False, f"❌ 本周挑战次数已用尽（{used}/{self.attempts_per_week}）。"

        name = (team_name or "").strip()[:20] or f"{player.user_name or player.user_id}的队伍"
        now = int(time.time())
        try:
            async with self.db.transaction() as conn:
                cursor = await conn.execute(
                    """
                    INSERT INTO weekly_boss_runs
                        (cycle_id, leader_id, team_name, member_count, status, created_at)
                    VALUES (?, ?, ?, 1, 0, ?)
                    """,
                    (cycle["cycle_id"], player.user_id, name, now),
                )
                run_id = cursor.lastrowid
                await conn.execute(
                    """
                    INSERT INTO weekly_boss_members
                        (run_id, user_id, joined_at)
                    VALUES (?, ?, ?)
                    """,
                    (run_id, player.user_id, now),
                )
        except Exception as exc:
            logger.error(f"【周本】创建队伍失败: {exc}")
            return False, "❌ 创建队伍失败，请稍后重试。"

        return True, (
            f"✅ 周本队伍已创建：#{run_id}【{name}】\n"
            f"👹 本周 Boss：{cycle.get('boss_name', '')}（{cycle.get('element', '')}属性）\n"
            f"👥 当前 1/{self.max_players} 人 · 队长：{player.user_name or player.user_id}\n"
            f"💡 队友发送 /周本加入 {run_id} 入队；全队发送 /周本备战 设置丹药；"
            f"队长发送 /周本开始 查看开战预览。"
        )

    async def join_team(self, player: Player, run_id: str) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "❌ 周本玩法当前未开启。"
        try:
            rid = int(str(run_id).strip().lstrip("#"))
        except (TypeError, ValueError):
            return False, "❌ 请输入正确的队伍编号，例如：/周本加入 12"

        cycle = await self.ensure_current_cycle()
        run = await self._fetchone(
            "SELECT * FROM weekly_boss_runs WHERE run_id = ? AND cycle_id = ?",
            (rid, cycle["cycle_id"]),
        )
        if not run:
            return False, "❌ 本周不存在该队伍（可能已过期或编号错误）。"
        if not self._entry_open(cycle):
            return False, "❌ 本周周本即将结束（结束前 5 分钟停止入场），请等待下周刷新。"
        if int(run.get("status") or 0) != 0:
            return False, "❌ 该队伍已经开始战斗或已结束，无法加入。"
        if int(player.level_index or 0) < self.min_level:
            return False, f"❌ 周本门槛为 level {self.min_level}（金丹期），当前境界不足。"

        members = await self._run_members(rid)
        if len(members) >= self.max_players:
            return False, f"❌ 队伍已满（{self.max_players} 人）。"
        if any(str(m.get("user_id")) == str(player.user_id) for m in members):
            return False, "❌ 你已经在这支队伍里了。"

        active = await self._find_member_run(cycle["cycle_id"], player.user_id, 0)
        if active:
            return False, f"❌ 你已在准备中的队伍 #{active['run_id']}，请先 /周本离开。"
        used = await self._cycle_attempts_used(cycle["cycle_id"], player.user_id)
        if used >= self.attempts_per_week:
            return False, f"❌ 本周挑战次数已用尽（{used}/{self.attempts_per_week}）。"

        now = int(time.time())
        rejected = False
        try:
            async with self.db.transaction() as conn:
                # 条件 INSERT：仅当“当前人数 < 上限”且“尚未加入”时才插入，
                # 计数守卫与插入在同一条 SQL/同一事务内完成，防止并发 join 突破人数上限。
                cursor = await conn.execute(
                    """
                    INSERT INTO weekly_boss_members (run_id, user_id, joined_at)
                    SELECT ?, ?, ?
                     WHERE (SELECT COUNT(*) FROM weekly_boss_members WHERE run_id = ?) < ?
                       AND NOT EXISTS (
                           SELECT 1 FROM weekly_boss_members WHERE run_id = ? AND user_id = ?
                       )
                    """,
                    (rid, player.user_id, now, rid, self.max_players, rid, player.user_id),
                )
                if cursor.rowcount == 0:
                    rejected = True
                else:
                    await conn.execute(
                        "UPDATE weekly_boss_runs SET member_count = member_count + 1 WHERE run_id = ?",
                        (rid,),
                    )
        except Exception as exc:
            logger.error(f"【周本】加入队伍失败: {exc}")
            return False, "❌ 加入队伍失败，请稍后重试。"

        if rejected:
            return False, f"❌ 队伍已满（{self.max_players} 人）或你已在这支队伍里。"

        return True, (
            f"✅ 已加入周本队伍 #{rid}【{run.get('team_name', '')}】\n"
            f"👥 当前 {len(members) + 1}/{self.max_players} 人\n"
            f"💡 发送 /周本备战 推荐 自动携带本周丹药，队长发送 /周本开始 查看开战预览。"
        )

    async def leave_team(self, player: Player) -> Tuple[bool, str]:
        cycle = await self.ensure_current_cycle()
        run = await self._find_member_run(cycle["cycle_id"], player.user_id, 0)
        if not run:
            return False, "❌ 你当前不在任何准备中的周本队伍里。"
        rid = int(run["run_id"])
        if str(run.get("leader_id")) == str(player.user_id):
            await self.db.conn.execute("DELETE FROM weekly_boss_members WHERE run_id = ?", (rid,))
            await self.db.conn.execute("DELETE FROM weekly_boss_runs WHERE run_id = ?", (rid,))
            await self.db.conn.commit()
            return True, f"🚪 队长已解散队伍 #{rid}。"
        await self.db.conn.execute(
            "DELETE FROM weekly_boss_members WHERE run_id = ? AND user_id = ?",
            (rid, player.user_id),
        )
        await self.db.conn.execute(
            "UPDATE weekly_boss_runs SET member_count = MAX(1, member_count - 1) WHERE run_id = ?",
            (rid,),
        )
        await self.db.conn.commit()
        return True, f"🚪 已离开周本队伍 #{rid}。"

    # ================= 丹药 =================

    def _classify_pill(self, base_name: str) -> Tuple[str, dict]:
        """返回 (kind, pill_data)；kind 为 prepare / recovery / revive / unsupported。"""
        pill_mgr = self._pill_manager()
        data = pill_mgr.get_pill_by_name(base_name)
        if not data:
            return "unsupported", {}
        subtype = str(data.get("subtype") or "")
        if subtype == "resurrection" or data.get("resurrects"):
            return "revive", data
        if data.get("effect_type") == "instant":
            try:
                heal = float(data.get("restore_hp_percent", 0) or 0)
            except (TypeError, ValueError):
                heal = 0.0
            if heal > 0:
                return "recovery", data
        if any(key in data for key in PREP_MULTIPLIER_KEYS):
            return "prepare", data
        return "unsupported", data

    def _pill_heal_pct(self, data: dict, quality: str) -> float:
        pill_mgr = self._pill_manager()
        try:
            base = float(data.get("restore_hp_percent", 0) or 0)
        except (TypeError, ValueError):
            base = 0.0
        if base <= 0:
            base = RANK_HEAL.get(str(data.get("rank") or ""), 0.10)
        try:
            mult = pill_mgr.get_quality_multiplier(quality)
        except Exception:
            mult = 1.0
        return max(0.01, min(1.0, base * mult))

    def _resolve_entry(self, name: str, quality: str, count: int) -> Optional[dict]:
        pill_mgr = self._pill_manager()
        base, embedded = pill_mgr.split_pill_key(name)
        quality = quality or embedded
        kind, data = self._classify_pill(base)
        if kind == "unsupported":
            return None
        return {
            "name": base,
            "quality": quality or pill_mgr.DEFAULT_QUALITY,
            "count": max(1, int(count)),
            "kind": kind,
        }

    def _entry_rank_score(self, entry: dict) -> int:
        pill_mgr = self._pill_manager()
        try:
            return pill_mgr.get_quality_rank(entry.get("quality", ""))
        except Exception:
            return 0

    def _apply_prep_buffs(self, loadout: dict) -> dict:
        """把战前丹药折算为周本内的固定 buff（不写入玩家真实状态）。"""
        pill_mgr = self._pill_manager()
        phys_atk = magic_atk = phys_def = magic_def = 0.0
        for entry in loadout.get("prepare", []):
            data = pill_mgr.get_pill_by_name(entry.get("name", ""))
            if not data:
                continue
            try:
                qmult = pill_mgr.get_quality_multiplier(entry.get("quality", ""))
            except Exception:
                qmult = 1.0
            count = max(1, int(entry.get("count") or 1))
            phys_atk += float(data.get("physical_damage_multiplier", 0) or 0) * qmult * count
            magic_atk += float(data.get("magic_damage_multiplier", 0) or 0) * qmult * count
            phys_def += float(data.get("physical_defense_multiplier", 0) or 0) * qmult * count
            magic_def += float(data.get("magic_defense_multiplier", 0) or 0) * qmult * count

        caps = self._pill_caps
        atk_cap = float(caps.get("attack", 0.60) or 0.60)
        def_cap = float(caps.get("defense", 0.60) or 0.60)
        dr_cap = float(caps.get("damage_reduction", 0.30) or 0.30)

        phys_mult = max(0.40, min(1.0 + atk_cap, 1.0 + phys_atk))
        magic_mult = max(0.40, min(1.0 + atk_cap, 1.0 + magic_atk))
        pdef_mult = max(0.40, min(1.0 + def_cap, 1.0 + phys_def))
        mdef_mult = max(0.40, min(1.0 + def_cap, 1.0 + magic_def))
        # 防御类丹药同时提供少量真实减伤，弥补真伤无法被防御减免的问题
        def_bonus = max(0.0, (pdef_mult - 1.0 + mdef_mult - 1.0) / 2.0)
        damage_reduction = min(dr_cap, def_bonus * 0.6)
        return {
            "physical_damage_multiplier": phys_mult,
            "magic_damage_multiplier": magic_mult,
            "physical_defense_multiplier": pdef_mult,
            "magic_defense_multiplier": mdef_mult,
            "damage_reduction": damage_reduction,
        }

    def _loadout_counts(self, loadout: dict) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for section in ("prepare", "combat"):
            for entry in loadout.get(section, []):
                key = f"{entry.get('name', '')}|{entry.get('quality', '')}"
                counts[key] = counts.get(key, 0) + max(1, int(entry.get("count") or 1))
        return counts

    def _check_inventory(self, player: Player, loadout: dict) -> Dict[str, int]:
        inventory = player.get_pills_inventory()
        missing = {}
        for key, need in self._loadout_counts(loadout).items():
            have = int(inventory.get(key, 0) or 0)
            if have < need:
                missing[key] = need - have
        return missing

    def _parse_loadout_tokens(self, player: Player, args: str) -> Tuple[bool, str, dict]:
        pill_mgr = self._pill_manager()
        inventory = player.get_pills_inventory()
        tokens = [t for t in re.split(r"[\s,，、;；]+", str(args or "").strip()) if t]
        if not tokens:
            return False, "❌ 请填写丹药清单，例如：/周本备战 回春散·天*3 狂暴丹*2", {}

        entries: Dict[str, dict] = {}
        last_name = ""
        for token in tokens:
            count = 1
            match = re.match(r"^(.+?)[*x×](\d+)$", token)
            if match:
                name = match.group(1)
                count = int(match.group(2))
            elif token.isdigit():
                if not last_name:
                    return False, f"❌ 「{token}」前缺少丹药名。", {}
                name = last_name
                count = int(token)
            else:
                name = token
            last_name = name

            base_name, quality = pill_mgr.split_pill_key(name)
            resolved = pill_mgr.select_pill_key(inventory, base_name, quality or None)
            if not resolved:
                kind, _ = self._classify_pill(base_name)
                if kind == "unsupported":
                    return False, f"❌ 【{base_name}】不是可用于周本的丹药。", {}
                return False, f"❌ 丹药背包中没有【{name}】。", {}
            entry = self._resolve_entry(resolved, "", count)
            if not entry:
                return False, f"❌ 【{base_name}】无法用于周本。", {}
            key = entry["name"]
            if key in entries:
                entries[key]["count"] += entry["count"]
                if self._entry_rank_score(entry) > self._entry_rank_score(entries[key]):
                    entries[key]["quality"] = entry["quality"]
            else:
                entries[key] = entry

        prepare = [e for e in entries.values() if e["kind"] == "prepare"]
        combat = [e for e in entries.values() if e["kind"] in ("recovery", "revive")]
        prep_count = sum(e["count"] for e in prepare)
        combat_count = sum(e["count"] for e in combat)
        if prep_count > self.pill_slots_prepare:
            return False, (
                f"❌ 战前丹药最多 {self.pill_slots_prepare} 枚，当前 {prep_count} 枚。"
            ), {}
        if combat_count > self.pill_slots_combat:
            return False, (
                f"❌ 战中丹药最多 {self.pill_slots_combat} 枚，当前 {combat_count} 枚。"
            ), {}
        return True, "", {"prepare": prepare, "combat": combat}

    def _recommend_loadout(self, player: Player) -> dict:
        pill_mgr = self._pill_manager()
        inventory = player.get_pills_inventory()
        prepared = []
        combat = []

        candidates = []
        for key, raw_count in inventory.items():
            try:
                count = int(raw_count or 0)
            except (TypeError, ValueError):
                count = 0
            if count <= 0:
                continue
            name, quality = pill_mgr.split_pill_key(key)
            kind, data = self._classify_pill(name)
            if kind not in ("prepare", "recovery", "revive"):
                continue
            entry = {
                "name": name, "quality": quality, "count": 1, "kind": kind,
                "data": data, "available": count,
            }
            candidates.append(entry)

        # 战前：按有效 buff 强度优先，每种丹药先各取 1 枚，再补重复
        prep_candidates = [c for c in candidates if c["kind"] == "prepare"]
        prep_candidates.sort(key=lambda c: self._prep_score(c), reverse=True)
        for _round in range(self.pill_slots_prepare):
            picked = None
            for cand in prep_candidates:
                used = next((e["count"] for e in prepared if e["name"] == cand["name"]), 0)
                if used < cand["available"]:
                    picked = cand
                    break
            if not picked:
                break
            existing = next((e for e in prepared if e["name"] == picked["name"]), None)
            if existing:
                existing["count"] += 1
            else:
                prepared.append({
                    "name": picked["name"], "quality": picked["quality"],
                    "count": 1, "kind": "prepare",
                })

        # 战中：1 枚复活丹 + 其余恢复丹（高恢复优先）
        revives = [c for c in candidates if c["kind"] == "revive"]
        revives.sort(key=lambda c: self._entry_rank_score(c), reverse=True)
        if revives and self.pill_slots_combat > 0:
            combat.append({
                "name": revives[0]["name"], "quality": revives[0]["quality"],
                "count": 1, "kind": "revive",
            })
        recoveries = [c for c in candidates if c["kind"] == "recovery"]
        recoveries.sort(
            key=lambda c: self._pill_heal_pct(c["data"], c["quality"]), reverse=True
        )
        remaining = self.pill_slots_combat - sum(e["count"] for e in combat)
        for _round in range(remaining):
            picked = None
            for cand in recoveries:
                used = next((e["count"] for e in combat if e["name"] == cand["name"]), 0)
                if used < cand["available"]:
                    picked = cand
                    break
            if not picked:
                break
            existing = next((e for e in combat if e["name"] == picked["name"]), None)
            if existing:
                existing["count"] += 1
            else:
                combat.append({
                    "name": picked["name"], "quality": picked["quality"],
                    "count": 1, "kind": "recovery",
                })
        return {"prepare": prepared, "combat": combat}

    def _prep_score(self, entry: dict) -> float:
        data = entry.get("data") or {}
        pill_mgr = self._pill_manager()
        try:
            qmult = pill_mgr.get_quality_multiplier(entry.get("quality", ""))
        except Exception:
            qmult = 1.0
        score = 0.0
        for key in PREP_MULTIPLIER_KEYS:
            try:
                value = float(data.get(key, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            # 攻击/防御都计入；负面效果按 40% 折算，避免推荐自残丹
            score += (value if value >= 0 else value * 0.4) * qmult
        return score

    def _format_loadout(self, loadout: dict) -> str:
        def section(title: str, entries: list) -> str:
            if not entries:
                return f"{title}：未携带"
            parts = []
            for entry in entries:
                quality = entry.get("quality") or ""
                label = entry.get("name", "")
                if quality and quality != "一般":
                    label += f"（{quality}）"
                parts.append(f"{label}×{entry.get('count', 1)}")
            return f"{title}：" + "、".join(parts)

        return "\n".join([
            section("🧪 战前", loadout.get("prepare", [])),
            section("💊 战中", loadout.get("combat", [])),
        ])

    async def set_loadout(self, player: Player, args: str) -> Tuple[bool, str]:
        cycle = await self.ensure_current_cycle()
        run = await self._find_member_run(cycle["cycle_id"], player.user_id, 0)
        if not run:
            return False, "❌ 你当前不在准备中的周本队伍里，请先 /周本创建 或 /周本加入。"

        args = (args or "").strip()
        if not args:
            member = await self._fetchone(
                "SELECT pill_loadout FROM weekly_boss_members WHERE run_id = ? AND user_id = ?",
                (run["run_id"], player.user_id),
            )
            loadout = self._safe_json((member or {}).get("pill_loadout"), {})
            current = self._format_loadout(loadout)
            return True, (
                f"🧪 周本备战 · 队伍 #{run['run_id']}\n"
                "━━━━━━━━━━━━━━━\n"
                f"{current}\n"
                "━━━━━━━━━━━━━━━\n"
                f"📌 战前上限 {self.pill_slots_prepare} 枚 + 战中上限 {self.pill_slots_combat} 枚\n"
                "💡 设置：/周本备战 回春散·天*3 狂暴丹*2 回生丹*1\n"
                "💡 自动：/周本备战 推荐　清空：/周本备战 清空\n"
                "⚠️ 战前丹药在开战时扣除；战中丹药只在自动触发时扣除，未使用不消耗。"
            )

        if args in ("清空", "重置", "clear"):
            loadout = {"prepare": [], "combat": []}
            message = "🧹 已清空本次周本丹药清单。"
        elif args in ("推荐", "自动", "auto"):
            loadout = self._recommend_loadout(player)
            if not any(loadout.values()):
                return False, "❌ 丹药背包里没有可用于周本的丹药，请先准备恢复丹/输出丹/防御丹。"
            message = "✅ 已按丹药背包自动搭配本周携带清单。"
        else:
            ok, error, loadout = self._parse_loadout_tokens(player, args)
            if not ok:
                return False, error
            message = "✅ 周本丹药清单已更新。"

        missing = self._check_inventory(player, loadout)
        if missing:
            detail = "、".join(f"{k}缺 {v} 枚" for k, v in missing.items())
            return False, f"❌ 丹药库存不足：{detail}"

        await self.db.conn.execute(
            "UPDATE weekly_boss_members SET pill_loadout = ? WHERE run_id = ? AND user_id = ?",
            (json.dumps(loadout, ensure_ascii=False), run["run_id"], player.user_id),
        )
        await self.db.conn.commit()
        return True, (
            f"{message}\n"
            "━━━━━━━━━━━━━━━\n"
            f"{self._format_loadout(loadout)}\n"
            "━━━━━━━━━━━━━━━\n"
            "⚠️ 丹药在开战确认后真实扣除；战中丹药按气血低于 65% 或阵亡时自动使用。"
        )

    # ================= 天道压制 =================

    def _template(self, level_index: int) -> dict:
        level = max(0, int(level_index or 0))
        if level in self._template_cache:
            return self._template_cache[level]
        from ..core.equipment_balance import realm_attributes

        synthetic = SimpleNamespace(
            level_index=level, cultivation_type="灵修", experience=0,
        )
        base = realm_attributes(synthetic, self.config_manager)
        atk = int(base.get("physical_damage", 0)) + int(base.get("magic_damage", 0))
        defense = int(base.get("physical_defense", 0)) + int(base.get("magic_defense", 0))
        atk_scale = self._cfg_float("baseline_atk_scale", 1.39)
        def_scale = self._cfg_float("baseline_def_scale", 1.30)
        template = {
            "hp": max(1, int(base.get("max_hp", 1))),
            "atk": max(1, int(atk * atk_scale)),
            "defense": max(1, int(defense * def_scale)),
            "defense_scale": max(1, int(base.get("defense_scale", 1))),
            "max_mp": max(0, int(base.get("max_mp", 0))),
        }
        self._template_cache[level] = template
        return template

    def _normalize_factor(self, ratio: float) -> float:
        coef = self._cfg_float("normalize_ratio", 0.15)
        low = self._cfg_float("normalize_min", 0.85)
        high = self._cfg_float("normalize_max", 1.15)
        return max(low, min(high, low + coef * float(ratio)))

    async def _build_member_panel(
        self, player: Player, baseline_level: int, loadout: dict
    ) -> dict:
        """构造周本归一化面板（不写回玩家真实气血）。"""
        try:
            real = CombatManager.build_player_combat_stats(
                player, self.config_manager, db=self.db, pill_multipliers={}
            )
        except Exception as exc:
            logger.warning(f"【周本】构建玩家 {player.user_id} 战斗面板失败: {exc}")
            real = CombatManager.build_player_combat_stats(player, self.config_manager)

        template = self._template(baseline_level)
        real_max_hp = max(1, int(getattr(real, "max_hp", 0) or 1))
        real_atk = max(1, int(getattr(real, "atk", 0) or 1))
        real_def = max(1, int(getattr(real, "defense", 0) or 1))

        hp_factor = self._normalize_factor(real_max_hp / template["hp"])
        atk_factor = self._normalize_factor(real_atk / template["atk"])
        def_factor = self._normalize_factor(real_def / template["defense"])

        max_hp = max(1, int(template["hp"] * hp_factor))
        total_atk = max(1, int(template["atk"] * atk_factor))
        total_def = max(1, int(template["defense"] * def_factor))

        phys_atk_real = max(0, int(getattr(real, "physical_atk", 0) or 0))
        magic_atk_real = max(0, int(getattr(real, "magic_atk", 0) or 0))
        atk_sum = phys_atk_real + magic_atk_real
        phys_share = phys_atk_real / atk_sum if atk_sum > 0 else 0.5
        physical_atk = int(total_atk * phys_share)
        magic_atk = total_atk - physical_atk

        pdef_real = max(0, int(getattr(real, "physical_defense", 0) or 0))
        mdef_real = max(0, int(getattr(real, "magic_defense", 0) or 0))
        def_sum = pdef_real + mdef_real
        pdef_share = pdef_real / def_sum if def_sum > 0 else 0.5
        physical_def = int(total_def * pdef_share)
        magic_def = total_def - physical_def

        buffs = self._apply_prep_buffs(loadout)
        physical_atk = int(physical_atk * buffs["physical_damage_multiplier"])
        magic_atk = int(magic_atk * buffs["magic_damage_multiplier"])
        physical_def = int(physical_def * buffs["physical_defense_multiplier"])
        magic_def = int(magic_def * buffs["magic_defense_multiplier"])

        effects = dict(getattr(real, "effects", None) or {})
        # 周本特效硬上限：基础赠送 +20% 暴击 / +50% 爆伤 / +8% 吸血
        crit_rate = min(
            self.CRIT_RATE_TOTAL_CAP, float(effects.get("crit_rate", 0) or 0) + 20.0
        )
        crit_damage = min(
            self.CRIT_DAMAGE_TOTAL_CAP, float(effects.get("crit_damage", 1.5) or 1.5) + 0.5
        )
        lifesteal = min(
            self.LIFESTEAL_TOTAL_CAP, float(effects.get("lifesteal", 0) or 0) + 8.0
        )
        combo_rate = min(30.0, float(effects.get("combo_rate", 0) or 0))
        effects["crit_rate"] = max(0.0, crit_rate)
        effects["crit_damage"] = max(1.0, crit_damage)
        effects["lifesteal"] = max(0.0, lifesteal)
        effects["combo_rate"] = max(0.0, combo_rate)
        effects["dodge"] = min(15.0, float(effects.get("dodge", 0) or 0))
        effects["counter_damage"] = min(30.0, float(effects.get("counter_damage", 0) or 0))

        name = player.user_name or player.user_id
        stats = CombatStats(
            user_id=player.user_id,
            name=name,
            hp=max_hp,
            max_hp=max_hp,
            mp=int(getattr(real, "max_mp", 0) or 0),
            max_mp=int(getattr(real, "max_mp", 0) or 0),
            atk=physical_atk + magic_atk,
            defense=physical_def + magic_def,
            crit_rate=0,
            exp=int(getattr(player, "experience", 0) or 0),
            effects=effects,
            element=str(getattr(real, "element", "") or ""),
            physical_atk=physical_atk,
            magic_atk=magic_atk,
            physical_defense=physical_def,
            magic_defense=magic_def,
            defense_scale=template["defense_scale"],
            overflow_bonus=0.0,
            active_skill=getattr(real, "active_skill", {}) or {},
        )

        # 灵兽：使用归一化后的主人攻击重新计算，避免真实面板差 3 倍
        try:
            from .pet_manager import PetManager

            pet_mgr = PetManager(self.db)
            main_pet, support_pets, pet_effects, _ = await pet_mgr.build_pet_lineup(
                player.user_id,
                stats.max_hp,
                stats.atk,
                stats.defense,
                player.cultivation_type,
                persist=False,
            )
            if main_pet or support_pets:
                CombatManager.apply_pet_lineup(
                    stats, main_pet, support_pets, pet_effects,
                    final_owner_atk=stats.atk,
                )
        except Exception as exc:
            logger.debug(f"【周本】玩家 {player.user_id} 灵兽出战结算跳过: {exc}")

        return {
            "stats": stats,
            "hp": stats.max_hp,
            "max_hp": stats.max_hp,
            # 真实气血快照：战斗结束后按损耗比例写回，不写回归一化面板
            "real_hp": max(1, int(getattr(real, "hp", 0) or 1)),
            "real_max_hp": real_max_hp,
            "damage_reduction": float(buffs.get("damage_reduction", 0.0)),
            "crit_rate": effects["crit_rate"],
            "crit_damage": effects["crit_damage"],
            "lifesteal": effects["lifesteal"],
            "normalized": {
                "hp": stats.max_hp,
                "atk": stats.atk,
                "defense": stats.defense,
                "physical_atk": physical_atk,
                "magic_atk": magic_atk,
                "physical_defense": physical_def,
                "magic_defense": magic_def,
            },
        }

    # ================= D0 与 Boss 血量 =================

    def _compute_d0(self, baseline_level: int) -> int:
        """基准境界标准输出单位：每回合对 0 防御 Boss 的平均有效伤害。"""
        level = int(baseline_level or 0)
        if level in self._d0_cache:
            return self._d0_cache[level]
        template = self._template(level)
        atk = template["atk"]
        physical = atk // 2
        dummy = CombatStats(
            user_id="__d0__", name="标准修士", hp=template["hp"], max_hp=template["hp"],
            mp=0, max_mp=0, atk=atk, defense=template["defense"], crit_rate=0,
            effects={"crit_rate": 20.0, "crit_damage": 1.7},
            element="", physical_atk=physical, magic_atk=atk - physical,
            physical_defense=template["defense"] // 2,
            magic_defense=template["defense"] - template["defense"] // 2,
            defense_scale=template["defense_scale"],
        )
        boss = self._make_boss_stats("标准靶", "", max(1, atk * 100))
        samples = 300
        total = 0
        for _ in range(samples):
            damage, _, _ = CombatManager._execute_attack(dummy, boss)
            total += damage
        d0 = max(1, total // samples)
        self._d0_cache[level] = d0
        return d0

    def _boss_max_hp(self, baseline_level: int, member_count: int) -> int:
        d0 = self._compute_d0(baseline_level)
        n = max(1, int(member_count))
        hp = self.hp_unit * d0 * (n ** self.hp_exponent)
        if n == 1:
            hp *= self.solo_hp_mult
        return max(1, int(hp))

    @staticmethod
    def _make_boss_stats(name: str, element: str, max_hp: int) -> CombatStats:
        return CombatStats(
            user_id="__weekly_boss__", name=name, hp=max_hp, max_hp=max_hp,
            mp=0, max_mp=0, atk=0, defense=0, crit_rate=0, effects={},
            element=element or "", physical_atk=0, magic_atk=0,
            physical_defense=0, magic_defense=0, defense_scale=0,
        )

    # ================= 战斗 =================

    def _member_runtime(self, player: Player, panel: dict, loadout: dict) -> dict:
        pill_mgr = self._pill_manager()
        recovery = []
        revive = []
        for entry in loadout.get("combat", []):
            data = pill_mgr.get_pill_by_name(entry.get("name", "")) or {}
            kind = entry.get("kind") or self._classify_pill(entry.get("name", ""))[0]
            item = {
                "name": entry.get("name", ""),
                "quality": entry.get("quality", ""),
                "count": max(1, int(entry.get("count") or 1)),
                "kind": kind,
            }
            if kind == "revive":
                revive.append(item)
            else:
                item["heal_pct"] = self._pill_heal_pct(data, item["quality"])
                recovery.append(item)
        recovery.sort(key=lambda x: x["heal_pct"], reverse=True)
        return {
            "user_id": player.user_id,
            "name": player.user_name or player.user_id,
            "player": player,
            "stats": panel["stats"],
            "hp": panel["hp"],
            "max_hp": panel["max_hp"],
            "real_hp": panel.get("real_hp", panel["max_hp"]),
            "real_max_hp": panel.get("real_max_hp", panel["max_hp"]),
            "damage_reduction": panel["damage_reduction"],
            "damage": 0,
            "healing": 0,
            "deaths": 0,
            "recovery": recovery,
            "revive": revive,
            "heal_count": 0,
            "heal_reduction_until": 0,
            "alive": True,
            "deaths_this_fight": 0,
            "normalized": dict(panel.get("normalized") or {}),
            "used": {},
            "revived": 0,
        }

    def _boss_action_damage(self, member: dict, pct: float) -> int:
        raw = int(member["max_hp"] * pct)
        return max(1, raw)

    def _apply_typed_damage(self, member: dict, raw: int, physical_share: float) -> int:
        stats = member["stats"]
        physical = int(raw * physical_share)
        magic = raw - physical
        pdef = max(0, int(getattr(stats, "physical_defense", 0) or 0))
        mdef = max(0, int(getattr(stats, "magic_defense", 0) or 0))
        scale = max(0, int(getattr(stats, "defense_scale", 0) or 0))
        physical = CombatManager.apply_damage_reduction(physical, pdef, scale) if physical > 0 else 0
        magic = CombatManager.apply_damage_reduction(magic, mdef, scale) if magic > 0 else 0
        return physical + magic

    def _hit_member(self, member: dict, damage: int) -> int:
        damage = max(0, int(damage))
        if damage <= 0:
            return 0
        # 防御类战前丹药提供的整体减伤（上限 30%）
        reduction = min(0.30, max(0.0, float(member.get("damage_reduction") or 0)))
        if reduction > 0:
            damage = max(1, int(damage * (1 - reduction)))
        member["hp"] = max(0, int(member["hp"]) - damage)
        if member["hp"] <= 0 and member["alive"]:
            member["alive"] = False
            member["deaths"] += 1
            member["deaths_this_fight"] += 1
        return damage

    def _enrage_multiplier(self, round_no: int, member_count: int) -> float:
        if round_no >= self.soft_enrage_round:
            mult = self._cfg_float("soft_enrage_mult", 1.5)
            mult *= 1.0 + 0.05 * max(0, round_no - self.soft_enrage_round)
        elif round_no >= 6:
            mult = 1.0 + 0.03 * (round_no - 5)
        else:
            mult = 1.0
        if int(member_count) == 1:
            mult *= self.solo_damage_mult
        return mult

    @staticmethod
    def _mark_used(member: dict, item: dict) -> None:
        key = f"{item.get('name', '')}|{item.get('quality', '')}"
        used = member.setdefault("used", {})
        used[key] = int(used.get(key, 0) or 0) + 1

    def _maybe_auto_pills(self, member: dict, round_no: int, logs: List[str]) -> None:
        if not member["alive"]:
            # 阵亡时优先自动使用复活丹（每人每场最多复活 1 次）
            if member["revive"] and int(member.get("revived") or 0) < 1:
                item = member["revive"][0]
                item["count"] -= 1
                if item["count"] <= 0:
                    member["revive"].pop(0)
                self._mark_used(member, item)
                member["revived"] = int(member.get("revived") or 0) + 1
                member["hp"] = max(1, int(member["max_hp"] * 0.30))
                member["alive"] = True
                member["deaths_this_fight"] = 0
                logs.append(
                    f"✨ {member['name']} 触发【{item['name']}】，以 30% 气血复活！"
                )
            return

        ratio = member["hp"] / max(1, member["max_hp"])
        if ratio >= 0.65 or not member["recovery"]:
            return
        item = member["recovery"][0]
        item["count"] -= 1
        if item["count"] <= 0:
            member["recovery"].pop(0)
        self._mark_used(member, item)
        decay_index = min(len(HEAL_DECAY) - 1, member["heal_count"])
        decay = max(0.4, HEAL_DECAY[decay_index])
        heal = int(member["max_hp"] * item["heal_pct"] * decay)
        if member["heal_reduction_until"] >= round_no:
            heal = int(heal * 0.5)
        heal = max(1, heal)
        before = member["hp"]
        member["hp"] = min(member["max_hp"], member["hp"] + heal)
        gained = member["hp"] - before
        member["healing"] += gained
        member["heal_count"] += 1
        suffix = "（噬元禁疗，恢复减半）" if member["heal_reduction_until"] >= round_no else ""
        logs.append(
            f"💊 {member['name']} 自动服用【{item['name']}】，恢复 {self._fmt(gained)} 气血{suffix}"
        )

    def _run_battle(
        self,
        boss_name: str,
        boss_element: str,
        boss_max_hp: int,
        members: List[dict],
        member_count: int,
    ) -> dict:
        """执行 20 回合团队战，返回日志、结果与成员统计（纯计算，不写库）。"""
        boss = self._make_boss_stats(boss_name, boss_element, boss_max_hp)
        boss_shield = 0
        boss_shield_until = 0
        shield_phases_done: set = set()
        terminal_round = 0
        terminal_warned = False
        log_lines: List[str] = []
        round_summaries: List[str] = []
        result = "wipe"
        wipe_reason = ""
        round_no = 0

        for round_no in range(1, self.hard_enrage_round + 1):
            if boss.hp <= 0:
                result = "clear"
                break

            # 终焉读条：20% 血量触发，3 回合内未击杀则团灭
            if terminal_round and round_no > terminal_round:
                result = "wipe"
                wipe_reason = "终焉降临，Boss 未被击杀"
                log_lines.append(f"☠️ 第 {round_no} 回合：终焉降临，全队湮灭！")
                break

            if round_no >= self.hard_enrage_round:
                result = "wipe"
                wipe_reason = f"第 {round_no} 回合硬狂暴"
                log_lines.append(f"☠️ 第 {round_no} 回合：Boss 进入硬狂暴，全屏寂灭！")
                break

            enrage = self._enrage_multiplier(round_no, member_count)
            alive = [m for m in members if m["alive"]]
            if not alive:
                result = "wipe"
                wipe_reason = "全员阵亡"
                break

            # ===== Boss 行动 =====
            round_notes: List[str] = []
            if round_no >= self.soft_enrage_round and round_no == self.soft_enrage_round:
                round_notes.append("🔥 Boss 进入软狂暴，伤害 ×1.5 并持续攀升！")

            # 周本 Boss 行动为自定义实现（天罚/崩山/噬元），不走 _take_turn，
            # 因此玩家挂在 Boss 身上的状态需在此手动结算：
            #   - 灼烧等 DOT 对 Boss 真实血量（boss.hp）结算伤害
            #   - 眩晕/冰冻等控制使 Boss 跳过本次行动
            boss_status_logs: List[str] = []
            boss_controlled = CombatManager._tick_statuses(boss, boss_status_logs)
            CombatManager._decay_statuses(boss)
            round_notes.extend(boss_status_logs)

            if boss.hp <= 0:
                # DOT 直接把 Boss 磨死
                result = "clear"
                round_summaries.append(f"R{round_no}: Boss 被持续伤害击破")
                break

            if boss_controlled:
                round_notes.append(f"⚡ {boss_name} 处于控制状态，跳过本次行动！")
            else:
                # 天罚：70% 真伤 + 30% 物法混合伤害
                for member in alive:
                    raw = self._boss_action_damage(member, 0.065 * enrage)
                    true_part = int(raw * 0.7)
                    typed_part = max(0, raw - true_part)
                    typed = self._apply_typed_damage(member, typed_part, 0.5)
                    self._hit_member(member, true_part + typed)
                    if member["hp"] <= 0:
                        round_notes.append(f"💀 {member['name']} 被天罚击倒")
                round_notes.append(f"天罚 -{self._pct(0.065 * enrage)}最大气血（真伤 70%）")

                # 崩山：每 3 回合点杀一名存活成员
                living = [m for m in members if m["alive"]]
                if round_no % 3 == 0 and living:
                    target = random.choice(living)
                    raw = self._boss_action_damage(target, 0.16 * enrage)
                    typed = self._apply_typed_damage(target, raw, 0.5)
                    self._hit_member(target, typed)
                    round_notes.append(f"⛰️ 崩山→{target['name']} -{self._pct(0.16 * enrage)}（防御减免后）")
                    if target["hp"] <= 0:
                        round_notes.append(f"💀 {target['name']} 被崩山击倒")

                # 噬元：第 5/10/15 回合，真伤 + 2 回合禁疗
                if round_no in (5, 10, 15):
                    for member in [m for m in members if m["alive"]]:
                        raw = self._boss_action_damage(member, 0.08 * enrage)
                        self._hit_member(member, raw)
                        member["heal_reduction_until"] = max(
                            member.get("heal_reduction_until", 0), round_no + 2
                        )
                    round_notes.append("🩸 噬元爆发：全体 8% 真伤，治疗 -50%（2 回合）")

            # ===== 玩家行动 =====
            team_damage = 0
            for member in [m for m in members if m["alive"]]:
                # 双轨血量：先把真实战斗血量同步进归一化面板，使主动技能治疗
                # （按面板损失血量计算并回写面板 hp）能正确反映本轮真实战损，
                # 行动结束后再把面板治疗量统一落回真实血量轨道，避免治疗丢失。
                member["stats"].hp = member["hp"]
                hp_before_action = member["hp"]
                damage, action_logs, heal = CombatManager._take_turn(
                    member["stats"], boss, pvp=False, boss=True, round_num=round_no
                )
                damage = max(0, int(damage))
                # 本次行动中面板 hp 的增量（主动技能治疗/龙息反哺等）+ 返回的吸血，
                # 一并落到真实血量 member["hp"]（上限为 member 的最大HP）。
                gained = max(0, int(member["stats"].hp) - hp_before_action) + max(0, int(heal))
                if gained > 0:
                    before = member["hp"]
                    member["hp"] = min(member["max_hp"], member["hp"] + gained)
                    member["healing"] += member["hp"] - before

                if boss_shield > 0 and damage > 0:
                    reflect = max(1, int(damage * 0.10))
                    absorbed = min(boss_shield, damage)
                    boss_shield -= absorbed
                    dealt = damage - absorbed
                    boss.hp = max(0, boss.hp - dealt)
                    member["damage"] += dealt
                    team_damage += dealt
                    self._hit_member(member, reflect)
                    round_notes.append(
                        f"🛡️ 护体盾吸收 {self._fmt(absorbed)}，反伤 {self._fmt(reflect)}"
                    )
                    if member["hp"] <= 0:
                        round_notes.append(f"💀 {member['name']} 被护体反伤击倒")
                else:
                    dealt = min(damage, max(0, boss.hp))
                    boss.hp = max(0, boss.hp - dealt)
                    member["damage"] += dealt
                    team_damage += dealt

                for line in action_logs:
                    if any(keyword in line for keyword in ("施展", "触发", "会心", "连击")):
                        log_lines.append(f"  {line}")
                if member["hp"] <= 0 and member["alive"]:
                    member["alive"] = False
                    member["deaths"] += 1
                    member["deaths_this_fight"] += 1

            # 护体：70% / 40% 血量阈值触发一次
            hp_ratio = boss.hp / max(1, boss.max_hp)
            for threshold in (0.70, 0.40):
                if hp_ratio <= threshold and threshold not in shield_phases_done:
                    shield_phases_done.add(threshold)
                    boss_shield = max(1, int(boss.max_hp * 0.08))
                    boss_shield_until = round_no + 1
                    round_notes.append(
                        f"🛡️ Boss 血量跌破 {int(threshold * 100)}%，获得 8% 最大气血护盾（1 回合，反伤 10%）"
                    )

            if boss_shield > 0 and round_no >= boss_shield_until:
                boss_shield = 0

            # 终焉：20% 血量触发一次
            if not terminal_warned and hp_ratio <= 0.20 and boss.hp > 0:
                terminal_warned = True
                terminal_round = round_no + 3
                round_notes.append(
                    f"☠️ Boss 血量跌破 20%！终焉读条：{terminal_round} 回合内未击杀则团灭！"
                )

            if boss.hp <= 0:
                result = "clear"
                round_summaries.append(
                    f"R{round_no}: 团队伤害 {self._fmt(team_damage)} | Boss 已击破"
                )
                break

            # ===== 战后丹药与死亡判定 =====
            for member in members:
                self._maybe_auto_pills(member, round_no, round_notes)

            if not any(m["alive"] for m in members):
                result = "wipe"
                wipe_reason = "全员阵亡"
                round_summaries.append(
                    f"R{round_no}: 团队伤害 {self._fmt(team_damage)} | Boss "
                    f"{self._pct(hp_ratio)} | 全员阵亡"
                )
                break

            round_summaries.append(
                f"R{round_no}: 团队伤害 {self._fmt(team_damage)} | Boss {self._pct(hp_ratio)}"
                + (f" | {'；'.join(round_notes[:2])}" if round_notes else "")
            )
            if round_notes:
                log_lines.append(f"— 第 {round_no} 回合 —")
                log_lines.extend(f"  {note}" for note in round_notes)

        if result == "wipe" and not wipe_reason:
            wipe_reason = "战斗超时"

        return {
            "result": result,
            "wipe_reason": wipe_reason,
            "rounds": round_no,
            "boss_hp": max(0, boss.hp),
            "boss_max_hp": boss.max_hp,
            "total_damage": sum(int(m["damage"]) for m in members),
            "members": members,
            "log": log_lines[-200:],
            "round_summaries": round_summaries,
        }

    # ================= 开战预览与确认 =================

    def _pending_key(self, user_id: str) -> str:
        return str(user_id)

    async def prepare_battle(self, player: Player) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "❌ 周本玩法当前未开启。"
        cycle = await self.ensure_current_cycle()
        if not self._entry_open(cycle):
            return False, "❌ 本周周本即将结束（结束前 5 分钟停止入场），请等待下周刷新。"
        run = await self._find_member_run(cycle["cycle_id"], player.user_id, 0)
        if not run:
            return False, "❌ 你当前不在准备中的周本队伍里。"
        rid = int(run["run_id"])
        if str(run.get("leader_id")) != str(player.user_id):
            return False, "❌ 只有队长可以发起开战。"

        members = await self._run_members(rid)
        if not members:
            return False, "❌ 队伍中没有成员。"
        if len(members) > self.max_players:
            return False, f"❌ 队伍人数超过上限 {self.max_players}。"

        panel_lines = []
        warnings = []
        from .recovery_manager import RecoveryManager

        recovery = RecoveryManager(self.db, self.config_manager)
        boss_hp = self._boss_max_hp(int(cycle["baseline_level"]), len(members))
        total_prepared = 0
        for row in members:
            member_player = await self.db.get_player_by_id(str(row["user_id"]))
            if not member_player:
                return False, f"❌ 队员 {row['user_id']} 的角色数据不存在。"
            if int(member_player.level_index or 0) < self.min_level:
                return False, f"❌ 队员 {member_player.user_name or member_player.user_id} 境界不足。"

            try:
                if await recovery.is_injured(member_player):
                    warnings.append(
                        f"⚠️ {member_player.user_name or member_player.user_id} 身受重伤"
                        "（气血 ≤20%），需先调息/服药，否则无法确认开战"
                    )
            except Exception:
                pass

            used = await self._cycle_attempts_used(cycle["cycle_id"], member_player.user_id)
            if used >= self.attempts_per_week:
                return False, (
                    f"❌ 队员 {member_player.user_name or member_player.user_id} "
                    f"本周次数已用尽（{used}/{self.attempts_per_week}）。"
                )
            claimed = await self._has_claimed(cycle["cycle_id"], member_player.user_id)
            loadout = self._safe_json(row.get("pill_loadout"), {})
            missing = self._check_inventory(member_player, loadout)
            if missing:
                detail = "、".join(f"{k}缺 {v}" for k, v in missing.items())
                return False, (
                    f"❌ 队员 {member_player.user_name or member_player.user_id} "
                    f"丹药库存不足：{detail}"
                )
            if not any(loadout.get(section) for section in ("prepare", "combat")):
                warnings.append(f"⚠️ {member_player.user_name or member_player.user_id} 未携带丹药")
            if not any(e.get("kind") in ("recovery", "revive") for e in loadout.get("combat", [])):
                warnings.append(
                    f"⚠️ {member_player.user_name or member_player.user_id} 未携带恢复/复活丹，生存压力极大"
                )
            panel = await self._build_member_panel(
                member_player, int(cycle["baseline_level"]), loadout
            )
            total_prepared += sum(
                int(e.get("count") or 1)
                for e in loadout.get("prepare", [])
            )
            panel_lines.append(
                f"· {member_player.user_name or member_player.user_id} "
                f"气血 {self._fmt(panel['normalized']['hp'])} | "
                f"双攻 {self._fmt(panel['normalized']['atk'])} | "
                f"双防 {self._fmt(panel['normalized']['defense'])} | "
                f"暴击 {panel['crit_rate']:.0f}% | "
                f"丹药 {sum(int(e.get('count') or 1) for e in loadout.get('prepare', []))}"
                f"+{sum(int(e.get('count') or 1) for e in loadout.get('combat', []))}"
                + ("　👑本周已领奖，本次仅计入周榜" if claimed else "")
            )

        if len(members) == 1:
            warnings.append("⚠️ 单人挑战：Boss 血量 ×1.30、伤害 ×1.25，几乎不可能通关。")
        if total_prepared == 0:
            warnings.append("⚠️ 全队没有携带任何战前丹药，输出/防御压力会显著增加。")

        self._pending[self._pending_key(player.user_id)] = {
            "run_id": rid,
            "cycle_id": cycle["cycle_id"],
            "expire_at": int(time.time()) + max(30, self._cfg_int("confirm_timeout_seconds", 90)),
            "members": [str(row["user_id"]) for row in members],
        }

        lines = [
            f"⚔️ 周本开战预览 · 队伍 #{rid}【{run.get('team_name', '')}】",
            "━━━━━━━━━━━━━━━",
            f"👹 Boss：{cycle.get('boss_name', '')}（{cycle.get('element', '')}属性）",
            f"🏔️ 天道压制基准：level {cycle.get('baseline_level')} · 全员面板已归一化",
            f"🩸 Boss 血量：{self._fmt(boss_hp)}（{len(members)} 人，指数 {self.hp_exponent:g}）",
            f"👥 队伍：{len(members)}/{self.max_players} 人",
            "━━━━━━━━━━━━━━━",
            *panel_lines,
            "━━━━━━━━━━━━━━━",
            f"🧪 全队战前丹药合计：{total_prepared} 枚（开战即扣除）",
            "💊 战中丹药：气血 <65% 或阵亡时自动使用，未使用不消耗",
            "🩸 真实气血：战斗损耗将按比例写回角色，战后 ≤20% 会进入重伤状态。",
            "⚠️ 开战将消耗每人 1 次本周挑战次数，并锁定队伍名单。",
            *warnings,
            "━━━━━━━━━━━━━━━",
            f"✅ 确认开战：/周本确认　（{self._cfg_int('confirm_timeout_seconds', 90)} 秒内有效）",
            "❌ 取消：/周本取消",
        ]
        return True, "\n".join(lines)

    async def cancel_battle(self, player: Player) -> Tuple[bool, str]:
        removed = self._pending.pop(self._pending_key(player.user_id), None)
        if not removed:
            return False, "❌ 当前没有待确认的周本开战。"
        return True, "🛑 已取消本次周本开战，丹药与挑战次数均未消耗。"

    async def confirm_battle(self, player: Player) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "❌ 周本玩法当前未开启。"
        key = self._pending_key(player.user_id)
        pending = self._pending.get(key)
        if not pending:
            return False, "❌ 没有待确认的开战。请先发送 /周本开始 查看预览。"
        if int(pending.get("expire_at") or 0) < int(time.time()):
            self._pending.pop(key, None)
            return False, "⌛ 开战预览已过期，请重新发送 /周本开始。"

        cycle = await self.ensure_current_cycle()
        if cycle["cycle_id"] != pending.get("cycle_id"):
            self._pending.pop(key, None)
            return False, "⌛ 周期已变化，请重新发送 /周本开始。"
        if not self._entry_open(cycle):
            self._pending.pop(key, None)
            return False, "❌ 本周周本即将结束（结束前 5 分钟停止入场），请等待下周刷新。"
        run = await self._fetchone(
            "SELECT * FROM weekly_boss_runs WHERE run_id = ?", (pending["run_id"],)
        )
        if not run or int(run.get("status") or 0) != 0:
            self._pending.pop(key, None)
            return False, "❌ 队伍状态已变化，无法开战。"
        if str(run.get("leader_id")) != str(player.user_id):
            self._pending.pop(key, None)
            return False, "❌ 只有队长可以确认开战。"

        members = await self._run_members(int(run["run_id"]))
        member_ids = [str(m["user_id"]) for m in members]
        if sorted(member_ids) != sorted(pending.get("members") or []):
            self._pending.pop(key, None)
            return False, "❌ 队伍名单已变化，请重新发送 /周本开始。"

        # 二次校验库存与次数
        runtimes: List[dict] = []
        member_players: Dict[str, Player] = {}
        from .recovery_manager import RecoveryManager

        recovery = RecoveryManager(self.db, self.config_manager)
        for row in members:
            member_player = await self.db.get_player_by_id(str(row["user_id"]))
            if not member_player:
                return False, f"❌ 队员 {row['user_id']} 数据不存在。"
            try:
                if await recovery.is_injured(member_player):
                    return False, (
                        f"❌ 队员 {member_player.user_name or member_player.user_id} 身受重伤"
                        "（气血 ≤20%），请先 /调息 或服用回春散。"
                    )
            except Exception:
                pass
            used = await self._cycle_attempts_used(cycle["cycle_id"], member_player.user_id)
            if used >= self.attempts_per_week:
                return False, (
                    f"❌ 队员 {member_player.user_name or member_player.user_id} "
                    "本周次数已用尽。"
                )
            loadout = self._safe_json(row.get("pill_loadout"), {})
            missing = self._check_inventory(member_player, loadout)
            if missing:
                detail = "、".join(f"{k}缺 {v}" for k, v in missing.items())
                return False, f"❌ 丹药库存已变化：{detail}，请重新 /周本备战。"
            panel = await self._build_member_panel(
                member_player, int(cycle["baseline_level"]), loadout
            )
            runtimes.append(self._member_runtime(member_player, panel, loadout))
            member_players[member_player.user_id] = member_player

        boss_hp = self._boss_max_hp(int(cycle["baseline_level"]), len(members))
        now = int(time.time())

        # 锁定队伍并扣除每人 1 次挑战次数
        try:
            async with self.db.transaction() as conn:
                await conn.execute(
                    """
                    UPDATE weekly_boss_runs
                       SET status = 1, member_count = ?, max_hp = ?, current_hp = ?,
                           round = 0, total_damage = 0, log_json = '[]',
                           started_at = ?
                     WHERE run_id = ?
                    """,
                    (len(members), boss_hp, boss_hp, now, int(run["run_id"])),
                )
                for member_id in member_ids:
                    await conn.execute(
                        """
                        INSERT INTO weekly_boss_attempts (cycle_id, user_id, used, updated_at)
                        VALUES (?, ?, 1, ?)
                        ON CONFLICT(cycle_id, user_id)
                        DO UPDATE SET used = used + 1, updated_at = excluded.updated_at
                        """,
                        (cycle["cycle_id"], member_id, now),
                    )
        except Exception as exc:
            logger.error(f"【周本】锁定队伍失败: {exc}")
            return False, "❌ 开战失败（队伍锁定异常），请稍后重试。"

        self._pending.pop(key, None)

        # 扣除战前丹药（战中丹药在使用时扣除）
        inventories: Dict[str, dict] = {}
        for row in members:
            member_player = member_players[str(row["user_id"])]
            loadout = self._safe_json(row.get("pill_loadout"), {})
            inventory = member_player.get_pills_inventory()
            for entry in loadout.get("prepare", []):
                key_name = f"{entry.get('name', '')}|{entry.get('quality', '')}"
                need = max(1, int(entry.get("count") or 1))
                inventory[key_name] = max(0, int(inventory.get(key_name, 0) or 0) - need)
                if inventory[key_name] <= 0:
                    inventory.pop(key_name, None)
            inventories[member_player.user_id] = inventory

        # 执行战斗
        try:
            battle = self._run_battle(
                str(cycle.get("boss_name", "周常Boss")),
                str(cycle.get("element", "")),
                boss_hp,
                runtimes,
                len(members),
            )
        except Exception as exc:
            logger.error(f"【周本】战斗执行异常: {exc}")
            battle = {
                "result": "wipe", "wipe_reason": f"战斗异常：{exc}", "rounds": 0,
                "boss_hp": boss_hp, "boss_max_hp": boss_hp, "total_damage": 0,
                "members": runtimes, "log": [], "round_summaries": [],
            }

        # 将战斗中的气血损耗写回真实角色（可进入重伤状态）
        battle["hp_feedback"] = await self._write_back_real_hp(runtimes, member_players)

        # 扣除战中实际使用的丹药
        for runtime in runtimes:
            inventory = inventories.get(runtime["user_id"])
            if inventory is None:
                continue
            for key_name, used in (runtime.get("used") or {}).items():
                if int(used) <= 0:
                    continue
                inventory[key_name] = max(
                    0, int(inventory.get(key_name, 0) or 0) - int(used)
                )
                if inventory[key_name] <= 0:
                    inventory.pop(key_name, None)

        # 写回丹药背包
        for user_id, inventory in inventories.items():
            try:
                await self.db.update_player_fields(
                    user_id, pills_inventory=json.dumps(inventory, ensure_ascii=False)
                )
            except Exception as exc:
                logger.warning(f"【周本】写回玩家 {user_id} 丹药背包失败: {exc}")

        try:
            report = await self._settle(cycle, run, members, battle)
        except Exception as exc:
            logger.error(f"【周本】结算异常: {exc}", exc_info=True)
            return False, f"❌ 战斗已结束，但结算失败：{exc}\n请联系管理员处理。"

        if battle.get("result") == "clear":
            self.last_broadcast_msg = (
                f"🎉 周本通关！队伍【{run.get('team_name', '')}】击破【{cycle.get('boss_name', '')}】，"
                f"用时 {battle.get('rounds', 0)} 回合，{len(members)} 人参与！"
            )
        return True, report

    async def _write_back_real_hp(
        self, runtimes: List[dict], member_players: Dict[str, Player]
    ) -> List[dict]:
        """把周本战斗的气血损耗按比例折算写回真实角色。

        规则：
        - 以周本归一化面板的剩余气血比例作为损耗比例；
        - 扣减真实最大气血的同等比例，且不会因周本反而回血；
        - 最低保留 1 点气血，避免与“0 视为满血”的旧逻辑冲突；
        - 战后真实气血 ≤20% 即进入重伤状态（复用 RecoveryManager 判定）。
        """
        results: List[dict] = []
        for runtime in runtimes:
            user_id = str(runtime.get("user_id") or "")
            player = member_players.get(user_id)
            if not player:
                continue
            real_max = max(1, int(runtime.get("real_max_hp") or 0))
            current = int(runtime.get("real_hp") or real_max)
            current = max(1, min(real_max, current))

            battle_max = max(1, int(runtime.get("max_hp") or 1))
            battle_hp = max(0, min(battle_max, int(runtime.get("hp") or 0)))
            loss_ratio = max(0.0, min(1.0, 1.0 - battle_hp / battle_max))

            consumed = int(round(real_max * loss_ratio))
            if loss_ratio > 0:
                consumed = max(1, consumed)
            after = max(1, current - consumed)
            after = min(current, after)

            try:
                await self.db.update_player_fields(user_id, hp=after)
            except Exception as exc:
                logger.warning(f"【周本】写回玩家 {user_id} 真实气血失败: {exc}")
                continue
            player.hp = after
            ratio = after / real_max
            results.append(
                {
                    "user_id": user_id,
                    "name": runtime.get("name") or player.user_name or user_id,
                    "hp": after,
                    "max_hp": real_max,
                    "ratio": ratio,
                    "injured": ratio <= 0.20,
                }
            )
        return results

    # ================= 结算 =================

    async def _settle(
        self,
        cycle: dict,
        run: dict,
        member_rows: List[dict],
        battle: dict,
    ) -> str:
        run_id = int(run["run_id"])
        cycle_id = str(cycle["cycle_id"])
        now = int(time.time())
        clear = battle.get("result") == "clear"
        status = 2 if clear else 3
        boss_max_hp = max(1, int(battle.get("boss_max_hp") or 1))
        total_damage = max(1, sum(int(m.get("damage") or 0) for m in battle.get("members", [])))

        await self.db.conn.execute(
            """
            UPDATE weekly_boss_runs
               SET status = ?, current_hp = ?, round = ?, total_damage = ?,
                   log_json = ?, ended_at = ?
             WHERE run_id = ?
            """,
            (
                status, int(battle.get("boss_hp") or 0), int(battle.get("rounds") or 0),
                int(battle.get("total_damage") or 0),
                json.dumps(battle.get("round_summaries", []), ensure_ascii=False),
                now, run_id,
            ),
        )
        await self.db.conn.commit()

        runtime_by_id = {str(m["user_id"]): m for m in battle.get("members", [])}
        for row in member_rows:
            user_id = str(row["user_id"])
            runtime = runtime_by_id.get(user_id)
            if not runtime:
                continue
            await self.db.conn.execute(
                """
                UPDATE weekly_boss_members
                   SET damage = ?, healing = ?, deaths = ?, normalized_panel = ?
                 WHERE run_id = ? AND user_id = ?
                """,
                (
                    int(runtime.get("damage") or 0),
                    int(runtime.get("healing") or 0),
                    int(runtime.get("deaths") or 0),
                    json.dumps(runtime.get("normalized") or {}, ensure_ascii=False),
                    run_id, user_id,
                ),
            )
        await self.db.conn.commit()

        ranked = sorted(
            battle.get("members", []),
            key=lambda m: int(m.get("damage") or 0),
            reverse=True,
        )
        killer_id = ""
        if clear and ranked and int(ranked[0].get("damage") or 0) > 0:
            killer_id = str(ranked[0]["user_id"])

        # 周榜
        for runtime in battle.get("members", []):
            user_id = str(runtime["user_id"])
            share = int(runtime.get("damage") or 0) / total_damage
            await self.db.conn.execute(
                """
                INSERT INTO weekly_boss_leaderboard
                    (cycle_id, user_id, best_damage, best_share, clears, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id, user_id) DO UPDATE SET
                    best_damage = MAX(best_damage, excluded.best_damage),
                    best_share = MAX(best_share, excluded.best_share),
                    clears = clears + excluded.clears,
                    updated_at = excluded.updated_at
                """,
                (
                    cycle_id, user_id, int(runtime.get("damage") or 0),
                    float(share), 1 if clear else 0, now,
                ),
            )
        await self.db.conn.commit()

        lines = [
            f"{'🎉 周本通关！' if clear else '💀 周本团灭'}",
            "━━━━━━━━━━━━━━━",
            f"👹 {cycle.get('boss_name', '')}（{cycle.get('element', '')}属性）",
            f"👥 队伍：{run.get('team_name', '')} · {len(member_rows)} 人",
            (
                f"🔁 回合：{battle.get('rounds', 0)}/{self.hard_enrage_round - 1} · "
                f"团队总伤害 {self._fmt(total_damage)}"
            ),
            (
                f"🩸 Boss：{self._fmt(boss_max_hp)} → "
                f"{self._fmt(int(battle.get('boss_hp') or 0))}"
                f"（{self._pct(max(0.0, 1 - int(battle.get('boss_hp') or 0) / boss_max_hp))}）"
            ),
        ]
        if not clear:
            lines.append(f"☠️ 失败原因：{battle.get('wipe_reason', '全员阵亡')}")
        lines.append("━━━━━━━━━━━━━━━")

        if battle.get("round_summaries"):
            lines.extend(battle["round_summaries"][-8:])
            lines.append("━━━━━━━━━━━━━━━")

        # 奖励分配
        reward_lines: List[str] = []
        reward_lines.extend(
            await self._grant_rewards(cycle, run, ranked, total_damage, clear, killer_id, now)
        )

        lines.append("🏆 伤害榜")
        for index, runtime in enumerate(ranked):
            damage = int(runtime.get("damage") or 0)
            share = damage / total_damage if total_damage > 0 else 0
            tag = "🥇" if index == 0 else ("🥈" if index == 1 else "🥉")
            extra = ""
            if int(runtime.get("deaths") or 0):
                extra += f" · 阵亡 {runtime.get('deaths')} 次"
            if int(runtime.get("healing") or 0):
                extra += f" · 治疗 {self._fmt(int(runtime.get('healing') or 0))}"
            lines.append(
                f"{tag} {runtime.get('name', runtime['user_id'])} - "
                f"{self._fmt(damage)}（{self._pct(share)}）{extra}"
            )

        if reward_lines:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("🎁 掉落分配")
            lines.extend(reward_lines)
        hp_feedback = battle.get("hp_feedback") or []
        if hp_feedback:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("🩸 战后气血（已写回真实角色）")
            for item in hp_feedback:
                ratio = float(item.get("ratio") or 0.0)
                state = " · 💔重伤" if item.get("injured") else ""
                lines.append(
                    f"· {item.get('name', item.get('user_id', ''))}："
                    f"{self._fmt(int(item.get('hp') or 0))}/{self._fmt(int(item.get('max_hp') or 0))}"
                    f"（{self._pct(ratio)}）{state}"
                )
            if any(item.get("injured") for item in hp_feedback):
                lines.append("⚠️ 气血已 ≤20%，进入重伤状态，无法继续战斗类行动，请 /调息 或服用回春散。")
        if not clear:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("💡 团灭仅发放 10% 灵石与 1 次材料补偿；本周仍可重试（消耗次数）。")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("📜 发送 /周本排行 查看本周全服伤害榜。")
        return "\n".join(lines)

    async def _grant_rewards(
        self,
        cycle: dict,
        run: dict,
        ranked: List[dict],
        total_damage: int,
        clear: bool,
        killer_id: str,
        now: int,
    ) -> List[str]:
        cycle_id = str(cycle["cycle_id"])
        member_count = max(1, len(ranked))
        reward_cfg = self._reward_cfg
        base_pool = int(reward_cfg.get("stone_pool", 2000000) or 2000000)
        baseline = int(cycle.get("baseline_level") or 22)
        pool_scale = max(0.25, (1.12 ** max(0, baseline - 13)) / (1.12 ** 9))
        pool = int(base_pool * pool_scale)
        lines: List[str] = []

        for index, runtime in enumerate(ranked):
            user_id = str(runtime["user_id"])
            damage = int(runtime.get("damage") or 0)
            if damage <= 0:
                continue
            share = damage / total_damage if total_damage > 0 else 0
            rank = index + 1
            player = await self.db.get_player_by_id(user_id)
            if not player:
                continue
            name = runtime.get("name") or player.user_name or user_id

            if not clear:
                # 团灭补偿：伤害 ≥5% 才有
                if share < 0.05:
                    continue
                stone = int(pool * (member_count ** self.hp_exponent) * 0.10 * share)
                stone = max(100, stone)
                await self.db.add_player_gold(user_id, stone)
                drops = []
                drop = self._roll_legend()
                if drop:
                    drops.append((drop[0], drop[1], ""))
                granted = await self._grant_drops(player, drops)
                detail = f"、{granted}" if granted else ""
                lines.append(
                    f"· {name}：{self._fmt(stone)} 灵石{detail}（失败补偿）"
                )
                continue

            already = await self._has_claimed(cycle_id, user_id)
            if already:
                lines.append(f"· {name}：本周已领取过通关奖励，本次仅计入周榜")
                continue

            stone_mult = 1.0
            if rank == 1:
                stone_mult *= 1.35
            if user_id == killer_id:
                stone_mult *= 1.25
            stone = int(
                pool * (member_count ** self.hp_exponent)
                * (0.9 * share + 0.1 / member_count) * stone_mult
            )
            stone = max(0, stone)

            legend_base = int(reward_cfg.get("legend_rolls_base", 2) or 2)
            legend_share = int(reward_cfg.get("legend_rolls_share", 8) or 8)
            rolls = legend_base + int(legend_share * share)
            if rank == 1:
                rolls += 3
            if user_id == killer_id:
                rolls += 2

            equipment_base = int(reward_cfg.get("equipment_rolls_base", 1) or 1)
            equipment_rolls = equipment_base
            if rank == 1:
                equipment_rolls += 1
            if user_id == killer_id:
                equipment_rolls += 1
            equipment_rolls = min(2, equipment_rolls)

            exclusive = 1 + int(4 * share)
            if rank == 1:
                exclusive += 2
            exclusive = max(1, int(exclusive))

            drops: List[Tuple[str, int, str]] = []
            for _ in range(max(1, min(12, rolls))):
                drop = self._roll_legend()
                if drop:
                    drops.append((drop[0], drop[1], ""))
            if self.config_manager is not None:
                try:
                    from ..core.equipment_drop import roll_equipment_drop

                    for _ in range(equipment_rolls):
                        equip = roll_equipment_drop(
                            self.config_manager, player.level_index, up_rank_weight=0.8
                        )
                        if equip:
                            drops.append(
                                (equip["name"], 1, str(equip.get("quality") or ""))
                            )
                except Exception as exc:
                    logger.warning(f"【周本】装备掉落生成失败: {exc}")
            if exclusive > 0:
                drops.append((self.exclusive_material, exclusive, ""))

            await self.db.add_player_gold(user_id, stone)
            granted_text = await self._grant_drops(player, drops)
            await self.db.conn.execute(
                """
                INSERT OR IGNORE INTO weekly_boss_claims
                    (cycle_id, user_id, run_id, claimed_at)
                VALUES (?, ?, ?, ?)
                """,
                (cycle_id, user_id, int(run["run_id"]), now),
            )
            await self.db.conn.execute(
                "UPDATE weekly_boss_members SET reward_claimed = 1 WHERE run_id = ? AND user_id = ?",
                (int(run["run_id"]), user_id),
            )
            await self.db.conn.commit()

            title_text = ""
            if rank == 1 and self.title_mgr is not None:
                try:
                    is_new, title_msg = self.title_mgr.grant(player, "天外镇守者")
                    if is_new:
                        fields = {"permanent_bonus": player.permanent_bonus}
                        if not player.title:
                            fields["title"] = "天外镇守者"
                            player.title = "天外镇守者"
                        await self.db.update_player_fields(user_id, **fields)
                        title_text = " · 🏅获得称号【天外镇守者】"
                except Exception as exc:
                    logger.warning(f"【周本】称号发放失败: {exc}")

            tail = f"、{granted_text}" if granted_text else ""
            lines.append(
                f"· {name}：{self._fmt(stone)} 灵石"
                f"（材料 {min(12, max(1, rolls))} roll · 装备 {equipment_rolls} 件 · "
                f"{self.exclusive_material}×{exclusive}）{tail}{title_text}"
            )
        return lines

    def _roll_legend(self) -> Optional[Tuple[str, int]]:
        if self.world_boss_mgr is not None:
            try:
                return self.world_boss_mgr.roll_drop("legend")
            except Exception:
                pass
        return None

    async def _grant_drops(
        self, player: Player, drops: List[Tuple[str, int, str]]
    ) -> str:
        if not drops or self.storage_ring_mgr is None:
            return ""
        collected: Dict[str, int] = {}
        lost: Dict[str, int] = {}
        for name, count, quality in drops:
            count = max(0, int(count or 0))
            if count <= 0:
                continue
            try:
                kwargs = {"silent": True}
                if quality:
                    kwargs["quality"] = quality
                success, _ = await self.storage_ring_mgr.grant_item(
                    player, name, count, **kwargs
                )
            except Exception as exc:
                logger.warning(f"【周本】发放 {name}x{count} 失败: {exc}")
                success = False
            label = f"{name}（{quality}）" if quality else name
            bucket = collected if success else lost
            bucket[label] = bucket.get(label, 0) + count
        parts = [f"{name}×{count}" for name, count in collected.items()]
        parts.extend(f"{name}×{count}（储物戒已满）" for name, count in lost.items())
        return "、".join(parts)

    # ================= 查询 =================

    async def get_info(self, player: Player) -> str:
        if not self.enabled:
            return "❌ 周本玩法当前未开启。"
        cycle = await self.ensure_current_cycle()
        used = await self._cycle_attempts_used(cycle["cycle_id"], player.user_id)
        claimed = await self._has_claimed(cycle["cycle_id"], player.user_id)
        run = await self._find_member_run(cycle["cycle_id"], player.user_id, 0)
        last = await self._fetchone(
            """
            SELECT r.* FROM weekly_boss_runs r
              JOIN weekly_boss_members m ON m.run_id = r.run_id
             WHERE r.cycle_id = ? AND m.user_id = ?
             ORDER BY r.run_id DESC LIMIT 1
            """,
            (cycle["cycle_id"], player.user_id),
        )
        end_text = datetime.fromtimestamp(int(cycle["end_time"])).strftime("%m-%d %H:%M")
        template = self._template(int(cycle["baseline_level"]))
        sample_hp = self._boss_max_hp(int(cycle["baseline_level"]), 2)
        lines = [
            "👹 周常 Boss · 高难团队副本",
            "━━━━━━━━━━━━━━━",
            f"🌌 本周 Boss：{cycle.get('boss_name', '')}（{cycle.get('element', '')}属性）",
            f"🏔️ 天道压制基准：level {cycle.get('baseline_level')}",
            f"📐 基准模板：气血 {self._fmt(template['hp'])} · 双攻 {self._fmt(template['atk'])} · 双防 {self._fmt(template['defense'])}",
            f"🩸 参考血量：单人 {self._fmt(self._boss_max_hp(int(cycle['baseline_level']), 1))} · 双人 {self._fmt(sample_hp)}",
            f"⏳ 周期结束：{end_text}（结束前 {self._cfg_int('entry_close_minutes', 5)} 分钟停止入场）",
            "━━━━━━━━━━━━━━━",
            f"🎫 我的次数：{used}/{self.attempts_per_week}",
            f"🎁 本周领奖：{'已领取' if claimed else '未领取'}",
            (
                f"👥 准备中的队伍：#{run['run_id']}【{run.get('team_name', '')}】"
                if run else "👥 准备中的队伍：无"
            ),
            "━━━━━━━━━━━━━━━",
            "⚔️ 机制速览",
            "· 天罚：每回合全体 6.5% 最大气血（70% 真伤）",
            "· 崩山：每 3 回合随机点杀 16% 最大气血",
            "· 噬元：第 5/10/15 回合 8% 真伤 + 2 回合禁疗",
            "· 护体：70%/40% 血量各获得一次 8% 护盾并反伤 10%",
            f"· 狂怒：第 6 回合起伤害递增；第 {self.soft_enrage_round} 回合软狂暴；第 {self.hard_enrage_round} 回合硬狂暴团灭",
            "· 终焉：20% 血量触发，3 回合内未击杀则团灭",
            "· 孤军：单人时 Boss 血量 ×1.30、伤害 ×1.25",
            "· 战后：战斗损耗按比例写回真实气血，气血 ≤20% 会进入重伤状态",
            "━━━━━━━━━━━━━━━",
            "📜 命令：/周本创建 [队名] · /周本加入 <编号> · /周本备战 推荐",
            "　　　　/周本开始（预览）· /周本确认 · /周本状态 · /周本排行",
        ]
        if last:
            status_map = {0: "准备中", 1: "战斗中", 2: "通关", 3: "团灭", 4: "已过期"}
            lines.append(
                f"📌 最近队伍：#{last['run_id']} · {status_map.get(int(last.get('status') or 0), '未知')}"
                f" · 伤害 {self._fmt(int(last.get('total_damage') or 0))}"
            )
        return "\n".join(lines)

    async def get_status(self, player: Player) -> str:
        cycle = await self.ensure_current_cycle()
        run = await self._fetchone(
            """
            SELECT r.* FROM weekly_boss_runs r
              JOIN weekly_boss_members m ON m.run_id = r.run_id
             WHERE r.cycle_id = ? AND m.user_id = ?
             ORDER BY r.run_id DESC LIMIT 1
            """,
            (cycle["cycle_id"], player.user_id),
        )
        if not run:
            return "📭 你本周还没有参加过周本队伍。发送 /周本 查看玩法。"
        members = await self._run_members(int(run["run_id"]))
        status_map = {0: "准备中", 1: "战斗中", 2: "通关", 3: "团灭", 4: "已过期"}
        lines = [
            f"📊 周本队伍 #{run['run_id']}【{run.get('team_name', '')}】",
            "━━━━━━━━━━━━━━━",
            f"状态：{status_map.get(int(run.get('status') or 0), '未知')}"
            f" · 回合 {int(run.get('round') or 0)}"
            f" · 伤害 {self._fmt(int(run.get('total_damage') or 0))}",
            f"Boss 血量：{self._fmt(int(run.get('current_hp') or 0))}/{self._fmt(int(run.get('max_hp') or 0))}",
            "━━━━━━━━━━━━━━━",
        ]
        if members:
            for index, row in enumerate(
                sorted(members, key=lambda r: int(r.get("damage") or 0), reverse=True)
            ):
                name = str(row.get("user_id"))
                try:
                    member_player = await self.db.get_player_by_id(name)
                    if member_player and member_player.user_name:
                        name = member_player.user_name
                except Exception:
                    pass
                lines.append(
                    f"{index + 1}. {name} · 伤害 {self._fmt(int(row.get('damage') or 0))}"
                    f" · 治疗 {self._fmt(int(row.get('healing') or 0))}"
                    f" · 阵亡 {int(row.get('deaths') or 0)}"
                )
        logs = self._safe_json(run.get("log_json"), [])
        if logs:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("最近战报：")
            lines.extend(str(line) for line in logs[-5:])
        return "\n".join(lines)

    async def get_leaderboard(self) -> str:
        cycle = await self.ensure_current_cycle()
        rows = await self._fetchall(
            """
            SELECT * FROM weekly_boss_leaderboard
             WHERE cycle_id = ?
             ORDER BY best_damage DESC, best_share DESC
             LIMIT 10
            """,
            (cycle["cycle_id"],),
        )
        lines = [
            f"🏆 周本伤害榜 · {cycle.get('boss_name', '')}",
            "━━━━━━━━━━━━━━━",
        ]
        if not rows:
            lines.append("本周暂无记录，成为第一个挑战者吧。")
            return "\n".join(lines)
        for index, row in enumerate(rows):
            user_id = str(row.get("user_id"))
            name = user_id
            try:
                player = await self.db.get_player_by_id(user_id)
                if player and player.user_name:
                    name = player.user_name
            except Exception:
                pass
            tag = "🥇" if index == 0 else ("🥈" if index == 1 else ("🥉" if index == 2 else f"{index + 1}."))
            lines.append(
                f"{tag} {name} · 最高伤害 {self._fmt(int(row.get('best_damage') or 0))}"
                f" · 占比 {self._pct(float(row.get('best_share') or 0))}"
                f" · 通关 {int(row.get('clears') or 0)} 次"
            )
        return "\n".join(lines)

    async def reset_player_cd(self, target_user_id: str) -> Tuple[bool, str]:
        cycle = await self.ensure_current_cycle()
        await self.db.conn.execute(
            "DELETE FROM weekly_boss_claims WHERE cycle_id = ? AND user_id = ?",
            (cycle["cycle_id"], str(target_user_id)),
        )
        await self.db.conn.execute(
            "DELETE FROM weekly_boss_attempts WHERE cycle_id = ? AND user_id = ?",
            (cycle["cycle_id"], str(target_user_id)),
        )
        await self.db.conn.commit()
        return True, f"✅ 已重置 {target_user_id} 本周周本次数与领奖状态。"
