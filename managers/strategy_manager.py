# -*- coding: utf-8 -*-
"""「仙域争霸」世界管理器（设计方案 P0，§5/§6/§9/§10/§12）。

职责：
- 世界生命周期（建世界 / tick 调度入口 / 幂等补跑）
- 玩家入场（个人势力 + 新手包）
- 动作分发（招募 / 任务 / 编军 / 行军 / 围城 / 商店 …）
- tick 九步管线（任务结算 → 产出分配 → 围城推进 → 行军粮耗 →
  会战触发 → NPC AI → 事件掷点 → 伤势体力恢复 → 落盘）
- 面板快照（snapshot）

纯规则计算在 ``core/strategy_rules.py``；本模块只负责数据库读写、
校验与调度。P0 简化：每玩家一个「个人势力」（kind="player"），
俘虏体系 P1、hex 手操 P2、互市/成就/赛季 P3（表结构已就位）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from typing import Any, Optional

from ..core import strategy_rules as R
from ..data.migration import (
    _ensure_strategy_v70, _ensure_strategy_v71, _ensure_strategy_v72,
    _ensure_strategy_v74, _ensure_strategy_v75, _ensure_strategy_v76,
    _ensure_strategy_v77,
)
from .strategy_errors import StrategyBusyError, StrategyError
from .strategy_faction_mixin import StrategyFactionMixin
from .strategy_captive_mixin import StrategyCaptiveMixin
from .strategy_plot_mixin import StrategyPlotMixin
from .strategy_battle_mixin import StrategyBattleMixin
from .strategy_p3_mixin import StrategyP3Mixin

logger = logging.getLogger("astrbot")

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")

#: 任务产出资源展示名（面板「进行中任务」与派遣下拉共用）
TASK_OUTPUT_TEXT = {
    "coin": "铜钱", "grain": "粮草", "iron": "精铁", "token": "募将令",
    "intel": "情报", "build": "城防", "train": "精锐", "merit": "战功",
    "heal": "疗伤", "tech": "科技点", "grain_move": "粮草",
    "loot": "掠夺库存", "plot": "计略",
}


def _now() -> int:
    return int(time.time())


def _as_int(value: Any, label: str) -> int:
    """动作参数转整数：非法值统一抛 StrategyError（HTTP 400 而非 500）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise StrategyError(f"参数「{label}」无效")


def _load_json_file(name: str, default: Any = None) -> Any:
    path = os.path.join(_CONFIG_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[strategy] 读取配置 {name} 失败: {e}")
        return default


def _loads(text: Any, default: Any) -> Any:
    try:
        v = json.loads(text) if text else default
        return v if v is not None else default
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[strategy] JSON 列解析失败，回退默认值（数据可能损坏）: {e}")
        return default


def _row_to_dict(cursor, row) -> dict:
    keys = [d[0] for d in cursor.description]
    return dict(zip(keys, row))


#: 主玩法钱包兜底锁：handlers.utils.get_player_lock 导入失败时使用。
#: 必须是模块级共享（每次新建 asyncio.Lock() 会让互斥完全无效）。
_FALLBACK_PLAYER_LOCKS: dict[str, asyncio.Lock] = {}


def _main_wallet_lock(user_id: str) -> asyncio.Lock:
    try:
        from ..handlers.utils import get_player_lock
        return get_player_lock(user_id)
    except Exception:  # noqa: BLE001
        lock = _FALLBACK_PLAYER_LOCKS.get(user_id)
        if lock is None:
            lock = _FALLBACK_PLAYER_LOCKS[user_id] = asyncio.Lock()
        return lock


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------

class StrategyManager(StrategyFactionMixin, StrategyCaptiveMixin,
                      StrategyPlotMixin, StrategyBattleMixin, StrategyP3Mixin):
    """仙域争霸世界管理器（单例，挂在 AstrBotPlugin 上）。

    P1（势力联机/俘虏/外交/计略）、P2（hex 手操战役）、
    P3（互市/匪寨/商路/赛季/成就）以 Mixin 组合。
    """

    def __init__(self, db, config_manager=None):
        self.db = db
        self.config_manager = config_manager
        self._world_lock = asyncio.Lock()
        # 玩家动作串行锁：handle_action 全程持有，防动作间/动作与 tick 并发丢更新。
        # 死锁防护：锁序恒为 _action_lock → _world_lock（tick/poll 同序）；
        # 只持 _action_lock 的路径对 _world_lock 仅做 locked() 非阻塞检查。
        self._action_lock = asyncio.Lock()
        # 互市挂单撮合互斥（防 TOCTOU 双花：并发双买/买撤并发）
        self._market_lock = asyncio.Lock()
        self._cfg_cache: dict[str, Any] = {}
        self._cfg_mtime: float = 0.0
        # 懒加载主玩法玩家锁
        self._get_player_lock = None
        # WS 通知推送回调（main.py 注入 → room_server.notify_strategy）
        self._notifier = None
        # 当前正在执行整段 tick 事务的任务：该任务内的 _commit 只暂存，
        # 由 _tick_locked 统一提交/回滚，避免半提交与“伪回滚”。
        self._tick_task = None

    # ------------------------------------------------------------------
    # 通知分级推送（§12.5：strategy.notify，三档 notify_pref）
    # ------------------------------------------------------------------
    #: kind → 触发所需最低通知档位（0 仅手操窗口 / 1 +己方被攻 / 2 全部事件）
    _NOTIFY_MIN_LEVEL = {
        "battle_open": 0,
        "duel_invite": 0,
        "siege_alert": 1,
        "trade_route_cut": 1,
        "convoy_arrived": 1,
        "convoy_raided": 1,
        "wanderer_auction": 1,
        "bandit_raid": 1,
        "captive_transfer": 1,
        "captive_evicted": 1,
        "faction_task": 1,
        "faction_event": 1,
        "event": 2,
    }

    def set_notifier(self, fn) -> None:
        """注入推送回调：``async fn(user_id, kind, payload) -> None``。"""
        self._notifier = fn

    async def _notify(self, world: dict, user_ids, kind: str,
                      payload: Optional[dict] = None) -> None:
        """按玩家 notify_pref 档位过滤后推送 WS 通知（失败静默，不影响主流程）。"""
        fn = self._notifier
        if fn is None:
            return
        min_level = self._NOTIFY_MIN_LEVEL.get(str(kind), 2)
        seen: set = set()
        for uid in user_ids or []:
            uid = str(uid or "")
            if not uid or uid in seen:
                continue
            seen.add(uid)
            try:
                p = await self.get_player(uid, int(world["id"]))
                level = int(((p or {}).get("notify_pref") or {}).get("level", 1))
                if level < min_level:
                    continue
                await fn(uid, str(kind), payload or {})
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[strategy] 通知推送失败({kind}→{uid}): {e}")

    async def set_notify_pref(self, user_id: str, level: int) -> dict:
        """设置通知档位：0 仅手操窗口 / 1 +己方被攻（默认）/ 2 全部事件。"""
        world, p = await self._require_player(user_id)
        level = max(0, min(2, int(level)))
        pref = p.get("notify_pref") or {}
        pref["level"] = level
        p["notify_pref"] = pref
        await self.save_player(p)
        names = {0: "仅手操窗口", 1: "手操 + 己方被攻", 2: "全部事件"}
        return {"ok": True, "level": level,
                "msg": f"通知档位已设为「{names[level]}」"}

    async def set_guide_visible(self, user_id: str, visible: bool) -> dict:
        """新手指引开关：持久化到 strategy_players.tutorial_json。"""
        world, p = await self._require_player(user_id)
        tut = p.get("tutorial") or {}
        tut["guide_hidden"] = not bool(visible)
        tut["guide_touched_at"] = _now()
        p["tutorial"] = tut
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "visible": bool(visible),
                "msg": "📖 新手指引已开启" if visible else "📖 新手指引已收起"}

    # ------------------------------------------------------------------
    # GM 运维通道（§19：仅超管可用，权限校验在指令层）
    # ------------------------------------------------------------------
    async def gm(self, op: str, args: list[str]) -> dict:
        """GM 操作：开世界 / tick / 注入事件 / 势力平衡 / 重置赛季。"""
        op = str(op or "").strip()
        args = [str(a or "").strip() for a in (args or [])]
        if op in ("开世界", "open"):
            template = args[0] if args else ""
            templates = (self.cfg.get("world") or {}).get("templates") or []
            ids = [str(t.get("template_id", "")) for t in templates]
            if template and template not in ids:
                raise StrategyError(f"模板不存在：{template}（可用：{'、'.join(ids)}）")
            world = await self.get_world()
            season_no = 1
            if world:
                world["state"] = "ended"
                await self.save_world(world)
                await self._commit()
                season_no = int(world.get("season_no", 1)) + 1
            new_world = await self._create_world(
                season_no=season_no, template_id=template)
            return {"ok": True,
                    "msg": f"🌏 已开新世界 #{new_world['id']}"
                           f"（模板 {new_world['template_id']}，赛季 S{season_no}）"}
        if op in ("tick", "推进"):
            try:
                n = int(args[0]) if args else 1
            except ValueError:
                raise StrategyError("tick 次数须为整数") from None
            n = max(1, min(24, n))
            summary = await self.tick(repeat=n)
            evs = "、".join(summary.get("events") or []) or "无"
            return {"ok": True,
                    "msg": f"⏩ 已推进 {n} tick（现 tick {summary.get('tick_no')}）："
                           f"战报 {len(summary.get('battles') or [])} 场 / 事件 {evs}"}
        if op in ("注入事件", "事件", "event"):
            eid = args[0] if args else ""
            ev = next((e for e in ((self.cfg.get("events") or {}).get("events") or [])
                       if str(e.get("id")) == eid), None)
            if not ev:
                raise StrategyError(f"事件不存在：{eid or '(空)'}")
            world = await self.ensure_world()
            target: dict = {}
            if ev.get("scope") == "region":
                nodes = await self.get_nodes(world["id"])
                node = None
                if len(args) > 1 and str(args[1]).lstrip("-").isdigit():
                    node = next((n for n in nodes
                                 if n["id"] == int(args[1])), None)
                    if not node:
                        raise StrategyError(f"据点不存在：{args[1]}")
                elif nodes:
                    node = nodes[0]
                if node:
                    target = {"node_ids": [node["id"]], "node_name": node["name"]}
            duration = int(ev.get("duration_ticks", 8))
            tick_no = int(world["tick_no"])
            await self._execute("""
                INSERT INTO strategy_events (world_id, event_id, scope, target_json,
                    start_at, end_at, start_tick, end_tick, data_json)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (world["id"], ev["id"], ev.get("scope", "region"),
                  json.dumps(target, ensure_ascii=False), _now(),
                  _now() + duration * self.tick_seconds(), tick_no,
                  tick_no + duration,
                  json.dumps({"name": ev.get("name", ev["id"]),
                              "effects": ev.get("effects") or [],
                              "gm": True}, ensure_ascii=False)))
            await self._apply_event_onset(
                world, ev, target, self._rng("gm", world["id"], tick_no))
            await self._commit()
            return {"ok": True,
                    "msg": f"🎲 已注入事件「{ev.get('name', eid)}」"
                           f"（{duration} tick"
                           + (f"，目标 {target.get('node_name')}" if target else "") + "）"}
        if op in ("势力平衡", "平衡", "balance"):
            if len(args) < 2:
                raise StrategyError("用法：势力平衡 <势力id> <铜钱增量> [粮草增量]")
            try:
                fid, coin = int(args[0]), int(args[1])
                grain = int(args[2]) if len(args) > 2 else 0
            except ValueError:
                raise StrategyError("参数须为整数") from None
            f = await self.get_faction(fid)
            if not f:
                raise StrategyError(f"势力不存在：{fid}")
            treasury = f.get("treasury") or {}
            treasury["coin"] = max(0, int(treasury.get("coin", 0)) + coin)
            treasury["grain"] = max(0, int(treasury.get("grain", 0)) + grain)
            f["treasury"] = treasury
            await self.save_faction(f)
            await self._commit()
            logger.info(f"[strategy][GM] 势力平衡：#{fid} 铜钱{coin:+} 粮草{grain:+}")
            return {"ok": True,
                    "msg": f"⚖️ 已调整「{f['name']}」国库：铜钱 {coin:+}、粮草 {grain:+}"
                           f"（现 {treasury['coin']} / {treasury['grain']}）"}
        if op in ("重置赛季", "reset_season"):
            # C3：全程持 _world_lock 防与 tick/动作交错产生双结算；
            # 绝不取 _action_lock（保持恒定锁序、防死锁），也不经
            # ensure_world（其内部会再取 _world_lock 造成自锁）
            async with self._world_lock:
                world = await self.get_world()
                if not world:
                    world = await self._create_world()
                res = await self._settle_season(world)
            return {"ok": True,
                    "msg": f"🔁 赛季 S{res['season_no']} 已强制结算"
                           f"（霸主：{res['champion']}），新世界 #{res['new_world_id']} 开启"}
        raise StrategyError(
            f"未知 GM 操作：{op or '(空)'}"
            "（可用：开世界 <模板> / tick <n> / 注入事件 <id> [据点id] / "
            "势力平衡 <id> <币> [粮] / 重置赛季）")

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @property
    def cfg(self) -> dict:
        """配置四件套（按 mtime 缓存，改文件热生效）。"""
        try:
            mt = os.path.getmtime(os.path.join(_CONFIG_DIR, "strategy_config.json"))
        except OSError:
            mt = 0.0
        if not self._cfg_cache or mt != self._cfg_mtime:
            self._cfg_cache = {
                "main": _load_json_file("strategy_config.json", {}) or {},
                "world": _load_json_file("strategy_world.json", {}) or {},
                "events": _load_json_file("strategy_events.json", {}) or {},
                "names": _load_json_file("strategy_names.json", {}) or {},
                "npc_names": _load_json_file("strategy_npc_names.json", {}) or {},
            }
            self._cfg_mtime = mt
        return self._cfg_cache

    def main_cfg(self) -> dict:
        return self.cfg.get("main") or {}

    def enabled(self) -> bool:
        return bool(self.main_cfg().get("enabled", True))

    def tick_seconds(self) -> int:
        return int(self.main_cfg().get("tick_minutes", 15)) * 60

    def _rng(self, *seeds) -> random.Random:
        """确定性随机：同 seed 同结果（§7.3 可复现）。"""
        h = hashlib.md5("|".join(str(s) for s in seeds).encode("utf-8")).hexdigest()
        return random.Random(int(h[:8], 16))

    # ------------------------------------------------------------------
    # DB 基础
    # ------------------------------------------------------------------
    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return _row_to_dict(cursor, row)

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(cursor, r) for r in rows]

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        cursor = await self.db.conn.execute(sql, params)
        return cursor.lastrowid or 0

    async def _exec_count(self, sql: str, params: tuple = ()) -> int:
        """执行 UPDATE/DELETE 并返回受影响行数（条件更新/乐观锁校验用）。"""
        cursor = await self.db.conn.execute(sql, params)
        return cursor.rowcount if cursor.rowcount is not None else 0

    def _in_tick_task(self) -> bool:
        """当前协程是否运行在 tick 事务内（口径与 :meth:`_commit` 一致）。"""
        return self._tick_task is not None and self._tick_task is asyncio.current_task()

    async def _commit(self):
        # tick 事务内不提前提交：所有步骤共用一笔事务，成功统一 commit，
        # 异常统一 rollback（否则 autocommit 下的 rollback 是空操作）。
        if self._in_tick_task():
            return
        await self.db.conn.commit()

    # ------------------------------------------------------------------
    # 跨模块公共助手（v4.1.0；契约见 docs/design/strategy_gap_fix_contract.md §2.1）
    # ------------------------------------------------------------------
    async def _current_world_id(self) -> int:
        row = await self._fetchone(
            "SELECT id FROM strategy_worlds ORDER BY id DESC LIMIT 1")
        return int((row or {}).get("id", 0) or 0)

    async def _audit(self, user_id: str, action: str,
                     detail: Optional[dict] = None) -> None:
        """策略动作审计（§18.4）。失败静默：审计绝不能影响主流程。"""
        try:
            cfg = self.main_cfg().get("security") or {}
            wid = await self._current_world_id()
            await self._execute(
                "INSERT INTO strategy_audit "
                "(world_id, user_id, action, detail_json, created_at) "
                "VALUES (?,?,?,?,?)",
                (wid, str(user_id or ""), str(action or ""),
                 json.dumps(detail or {}, ensure_ascii=False, default=str), _now()))
            keep = int(cfg.get("audit_keep_days", 30) or 30)
            await self._execute(
                "DELETE FROM strategy_audit WHERE created_at < ?",
                (_now() - keep * 86400,))
        except Exception:  # noqa: BLE001 - 审计失败不影响玩法
            pass

    async def _bump_morale(self, node: dict, delta: int,
                           reason: str = "") -> int:
        """民心增减，返回新值；调用方负责 save_node。

        上限 = 100 + 擂将台等级（§6.6 擂将台「民心 +1/级」）。
        """
        cur = int(node.get("morale", 80) or 0)
        cap = self._morale_cap(node)
        new = max(0, min(cap, cur + int(delta)))
        node["morale"] = new
        return new

    def _morale_cap(self, node: dict) -> int:
        """据点民心上限：基础 100 + 擂将台等级（最高 105）。"""
        return 100 + min(5, self._building_level(node, "arena"))

    def _is_joined_long_enough(self, member_or_player: Optional[dict],
                               hours: int) -> bool:
        """入盟时长校验（§18.4 观察期）；盟主/势力主不受限。"""
        if not hours or int(hours) <= 0:
            return True
        m = member_or_player or {}
        if str(m.get("position") or "") in ("leader", "host"):
            return True
        joined = int(m.get("joined_at", 0) or 0)
        return joined > 0 and _now() - joined >= int(hours) * 3600

    async def _faction_buff(self, faction_id: int, effect: str) -> float:
        """生效中的动员令效果。

        - `*_bonus` 类：返回加成值（无 buff → 0.0）
        - 其余：返回乘区（无 buff → 1.0）

        联盟语义：动员令签发给盟主势力，成员势力的军团/据点也应吃到，
        因此这里统一上溯到盟主再查。
        """
        fid = int(faction_id or 0)
        try:
            f = await self.get_faction(fid)
            if f:
                host = await self._alliance_host(f)
                if host and int(host.get("id") or 0) > 0:
                    fid = int(host["id"])
        except Exception:  # noqa: BLE001 - 上溯失败按原势力查询
            pass
        try:
            rows = await self._fetchall(
                "SELECT buff_json FROM strategy_mobilizations "
                "WHERE faction_id=? AND status='active' AND end_at>?",
                (fid, _now()))
        except Exception:  # noqa: BLE001 - 旧库未迁移时降级为无 buff
            rows = []
        bonus = 0.0
        mult = 1.0
        for row in rows:
            try:
                buff = json.loads(row.get("buff_json") or "{}")
            except (TypeError, ValueError):
                continue
            if str(buff.get("effect") or "") != str(effect):
                continue
            try:
                val = float(buff.get("value", 1) or 0)
            except (TypeError, ValueError):
                continue
            if effect.endswith("_bonus"):
                bonus += val
            else:
                mult *= val
        return bonus if effect.endswith("_bonus") else mult

    def _check_action_rate(self, user_id: str) -> None:
        """策略指令限速（§18.4）：滑动 60s 窗口，超限抛 StrategyError。"""
        cfg = self.main_cfg().get("security") or {}
        limit = int(cfg.get("action_rate_per_min", 0) or 0)
        if limit <= 0:
            return
        bucket = getattr(self, "_action_rate_bucket", None)
        if bucket is None:
            bucket = self._action_rate_bucket = {}
        now = _now()
        dq = bucket.setdefault(str(user_id), [])
        while dq and now - dq[0] > 60:
            dq.pop(0)
        if len(dq) >= limit:
            raise StrategyError(f"操作过于频繁（每分钟上限 {limit} 次），请稍后再试")
        dq.append(now)

    async def ensure_tables(self):
        """确保 strategy_* 表存在（测试库/旧库兜底，IF NOT EXISTS 幂等；就绪后跳过）。"""
        if getattr(self, "_tables_ready", False):
            return
        await _ensure_strategy_v70(self.db.conn)
        await _ensure_strategy_v71(self.db.conn)
        await _ensure_strategy_v72(self.db.conn)
        await _ensure_strategy_v74(self.db.conn)
        await _ensure_strategy_v75(self.db.conn)
        await _ensure_strategy_v76(self.db.conn)
        await _ensure_strategy_v77(self.db.conn)
        await self._commit()
        self._tables_ready = True

    # ------------------------------------------------------------------
    # 行编解码（JSON 列 ↔ dict）
    # ------------------------------------------------------------------
    _JSON_COLS = {
        "strategy_nodes": ("buildings_json", "stock_json", "output_json", "adj_json",
                           "siege_state_json", "npc_garrison_json", "contribution_json",
                           "hatred_json", "lair_json"),
        "strategy_factions": ("personality_json", "treasury_json", "tech_json",
                              "diplomacy_json"),
        "strategy_players": ("honor_json", "daily_output_log_json", "notify_pref_json",
                             "tutorial_json", "achievements_json", "newcomer_json",
                             "rebuild_pay_log_json", "tavern_json"),
        "strategy_characters": ("stats_json", "traits_json", "aptitude_json",
                                "bonds_json", "location_json"),
        "strategy_armies": ("characters_json", "path_json"),
        "strategy_tasks": ("payout_json",),
        "strategy_battles": ("terrain_json", "armies_snapshot_json", "result_json",
                             "session_json"),
        "strategy_events": ("target_json", "data_json"),
        "strategy_worlds": ("data_json",),
    }

    @classmethod
    def _decode(cls, table: str, row: Optional[dict]) -> Optional[dict]:
        if not row:
            return row
        out = dict(row)
        for col in cls._JSON_COLS.get(table, ()):
            key = col[:-5]  # 去掉 _json 后缀
            default = [] if col in ("buildings_json", "adj_json", "traits_json",
                                    "characters_json", "path_json",
                                    "rebuild_pay_log_json", "terrain_json",
                                    "armies_snapshot_json") else {}
            out[key] = _loads(out.pop(col, None), default)
        return out

    @classmethod
    def _decode_all(cls, table: str, rows: list[dict]) -> list[dict]:
        return [cls._decode(table, r) for r in rows]

    # ------------------------------------------------------------------
    # 世界读写（解码后视图）
    # ------------------------------------------------------------------
    async def get_world(self) -> Optional[dict]:
        row = await self._fetchone(
            "SELECT * FROM strategy_worlds WHERE state='active' ORDER BY id DESC LIMIT 1")
        return self._decode("strategy_worlds", row)

    async def get_node(self, world_id: int, node_id: int) -> Optional[dict]:
        row = await self._fetchone(
            "SELECT * FROM strategy_nodes WHERE world_id=? AND id=?", (world_id, node_id))
        return self._decode("strategy_nodes", row)

    async def get_nodes(self, world_id: int) -> list[dict]:
        rows = await self._fetchall("SELECT * FROM strategy_nodes WHERE world_id=?", (world_id,))
        return self._decode_all("strategy_nodes", rows)

    async def save_node(self, node: dict):
        await self.db.conn.execute("""
            UPDATE strategy_nodes SET name=?, type=?, terrain=?, level=?, owner_faction=?,
                buildings_json=?, defense=?, morale=?, stock_json=?, output_json=?,
                pos_x=?, pos_y=?, adj_json=?, siege_state_json=?, npc_garrison_json=?,
                protect_until=?, contribution_json=?, hatred_json=?, respawn_at=?,
                lair_json=?
            WHERE world_id=? AND id=?
        """, (
            node.get("name", ""), node.get("type", "ruin"), node.get("terrain", "plain"),
            int(node.get("level", 1)), int(node.get("owner_faction", 0)),
            json.dumps(node.get("buildings") or [], ensure_ascii=False),
            int(node.get("defense", 0)), int(node.get("morale", 80)),
            json.dumps(node.get("stock") or {}, ensure_ascii=False),
            json.dumps(node.get("output") or {}, ensure_ascii=False),
            int(node.get("pos_x", 0)), int(node.get("pos_y", 0)),
            json.dumps(node.get("adj") or [], ensure_ascii=False),
            json.dumps(node.get("siege_state") or {}, ensure_ascii=False),
            json.dumps(node.get("npc_garrison") or {}, ensure_ascii=False),
            int(node.get("protect_until", 0)),
            json.dumps(node.get("contribution") or {}, ensure_ascii=False),
            json.dumps(node.get("hatred") or {}, ensure_ascii=False),
            int(node.get("respawn_at", 0) or 0),
            json.dumps(node.get("lair") or {}, ensure_ascii=False),
            int(node["world_id"]), int(node["id"]),
        ))

    async def get_faction(self, faction_id: int) -> Optional[dict]:
        row = await self._fetchone("SELECT * FROM strategy_factions WHERE id=?", (faction_id,))
        return self._decode("strategy_factions", row)

    async def save_faction(self, f: dict):
        await self.db.conn.execute("""
            UPDATE strategy_factions SET name=?, color=?, kind=?, leader_id=?,
                personality_json=?, treasury_json=?, reputation=?, level=?, tech_json=?,
                member_cap=?, capital_node_id=?, faction_state=?, rebuild_count=?, alive=?,
                diplomacy_json=?, alliance_id=?
            WHERE id=?
        """, (
            f.get("name", ""), R.norm_color(f.get("color"), "#888888"), f.get("kind", "npc"),
            f.get("leader_id", ""),
            json.dumps(f.get("personality") or {}, ensure_ascii=False),
            json.dumps(f.get("treasury") or {}, ensure_ascii=False),
            int(f.get("reputation", 0)), int(f.get("level", 1)),
            json.dumps(f.get("tech") or {}, ensure_ascii=False),
            int(f.get("member_cap", 10)), int(f.get("capital_node_id", 0)),
            f.get("faction_state", "normal"), int(f.get("rebuild_count", 0)),
            int(f.get("alive", 1)),
            json.dumps(f.get("diplomacy") or {}, ensure_ascii=False),
            int(f.get("alliance_id", 0) or 0),
            int(f["id"]),
        ))

    async def get_player(self, user_id: str, world_id: int) -> Optional[dict]:
        row = await self._fetchone(
            "SELECT * FROM strategy_players WHERE user_id=? AND world_id=?",
            (user_id, world_id))
        return self._decode("strategy_players", row)

    _PLAYER_COLS = ("coin", "grain", "iron", "recruit_token", "merit", "merit_season",
                    "honor_json", "auto_delegate", "daily_output_log_json", "notify_pref_json",
                    "tutorial_json", "achievements_json", "newcomer_json",
                    "rebuild_pay_log_json", "tavern_json", "faction_id", "last_seen_at")

    async def save_player(self, p: dict):
        await self.db.conn.execute("""
            UPDATE strategy_players SET coin=?, grain=?, iron=?, recruit_token=?, merit=?,
                merit_season=?, honor_json=?, auto_delegate=?, daily_output_log_json=?,
                notify_pref_json=?, tutorial_json=?, achievements_json=?, newcomer_json=?,
                rebuild_pay_log_json=?, tavern_json=?, faction_id=?, last_seen_at=?
            WHERE user_id=? AND world_id=?
        """, (
            int(p.get("coin", 0)), int(p.get("grain", 0)), int(p.get("iron", 0)),
            int(p.get("recruit_token", 0)), int(p.get("merit", 0)), int(p.get("merit_season", 0)),
            json.dumps(p.get("honor") or {}, ensure_ascii=False),
            int(p.get("auto_delegate", 1)),
            json.dumps(p.get("daily_output_log") or {}, ensure_ascii=False),
            json.dumps(p.get("notify_pref") or {}, ensure_ascii=False),
            json.dumps(p.get("tutorial") or {}, ensure_ascii=False),
            json.dumps(p.get("achievements") or {}, ensure_ascii=False),
            json.dumps(p.get("newcomer") or {}, ensure_ascii=False),
            json.dumps(p.get("rebuild_pay_log") or [], ensure_ascii=False),
            json.dumps(p.get("tavern") or {}, ensure_ascii=False),
            int(p.get("faction_id", 0)), int(p.get("last_seen_at", 0)),
            p["user_id"], int(p["world_id"]),
        ))

    async def get_char(self, char_id: int) -> Optional[dict]:
        row = await self._fetchone("SELECT * FROM strategy_characters WHERE id=?", (char_id,))
        return self._decode("strategy_characters", row)

    async def get_chars(self, owner_user_id: str = "", faction_id: int = 0,
                        world_id: int = 0, status: str = "") -> list[dict]:
        sql = "SELECT * FROM strategy_characters WHERE 1=1"
        params: list = []
        if world_id:
            sql += " AND world_id=?"
            params.append(world_id)
        if owner_user_id:
            sql += " AND owner_user_id=?"
            params.append(owner_user_id)
        if faction_id:
            sql += " AND faction_id=?"
            params.append(faction_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        rows = await self._fetchall(sql, tuple(params))
        return self._decode_all("strategy_characters", rows)

    async def save_char(self, c: dict):
        await self.db.conn.execute("""
            UPDATE strategy_characters SET world_id=?, faction_id=?, owner_user_id=?, name=?,
                role=?, rarity=?, profession=?, stats_json=?, traits_json=?, aptitude_json=?,
                exp=?, level=?, loyalty=?, injury=?, stamina=?, status=?, captor_faction=?,
                imprisoned_node_id=?, bonds_json=?, location_json=?,
                appointed_position=?, appointed_node_id=?, auction_cooldown_until=?
            WHERE id=?
        """, (
            int(c.get("world_id", 0)), int(c.get("faction_id", 0)), c.get("owner_user_id", ""),
            c.get("name", ""), c.get("role", "general"), c.get("rarity", "common"),
            c.get("profession", "sword"),
            json.dumps(c.get("stats") or {}, ensure_ascii=False),
            json.dumps(c.get("traits") or [], ensure_ascii=False),
            json.dumps(c.get("aptitude") or {}, ensure_ascii=False),
            int(c.get("exp", 0)), int(c.get("level", 1)), int(c.get("loyalty", 70)),
            int(c.get("injury", 0)), int(c.get("stamina", 100)), c.get("status", "idle"),
            int(c.get("captor_faction", 0)), int(c.get("imprisoned_node_id", 0)),
            json.dumps(c.get("bonds") or {}, ensure_ascii=False),
            json.dumps(c.get("location") or {}, ensure_ascii=False),
            str(c.get("appointed_position") or ""),
            int(c.get("appointed_node_id", 0) or 0),
            int(c.get("auction_cooldown_until", 0) or 0),
            int(c["id"]),
        ))

    async def insert_char(self, c: dict) -> int:
        # location_json 必须一起写入：否则流将/招募角色没有落点，
        # 拍卖会回退到 cands[0]、军师离间因找不到据点而放弃。
        cid = await self._execute("""
            INSERT INTO strategy_characters (world_id, faction_id, owner_user_id, name, role,
                rarity, profession, stats_json, traits_json, aptitude_json, exp, level,
                loyalty, injury, stamina, status, appointed_position, appointed_node_id,
                auction_cooldown_until, location_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(c.get("world_id", 0)), int(c.get("faction_id", 0)), c.get("owner_user_id", ""),
            c.get("name", ""), c.get("role", "general"), c.get("rarity", "common"),
            c.get("profession", "sword"),
            json.dumps(c.get("stats") or {}, ensure_ascii=False),
            json.dumps(c.get("traits") or [], ensure_ascii=False),
            json.dumps(c.get("aptitude") or {}, ensure_ascii=False),
            int(c.get("exp", 0)), int(c.get("level", 1)), int(c.get("loyalty", 70)),
            int(c.get("injury", 0)), int(c.get("stamina", 100)), c.get("status", "idle"),
            str(c.get("appointed_position") or ""),
            int(c.get("appointed_node_id", 0) or 0),
            int(c.get("auction_cooldown_until", 0) or 0),
            json.dumps(c.get("location") or {}, ensure_ascii=False),
        ))
        c["id"] = cid
        return cid

    async def get_army(self, army_id: int) -> Optional[dict]:
        row = await self._fetchone("SELECT * FROM strategy_armies WHERE id=?", (army_id,))
        return self._decode("strategy_armies", row)

    async def get_armies(self, world_id: int, faction_id: Optional[int] = None,
                         node_id: Optional[int] = None, owner_user_id: str = "",
                         states: tuple = ()) -> list[dict]:
        sql = "SELECT * FROM strategy_armies WHERE world_id=?"
        params: list = [world_id]
        if faction_id is not None:
            sql += " AND faction_id=?"
            params.append(faction_id)
        if node_id is not None:
            sql += " AND node_id=?"
            params.append(node_id)
        if owner_user_id:
            sql += " AND owner_user_id=?"
            params.append(owner_user_id)
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            params.extend(states)
        rows = await self._fetchall(sql, tuple(params))
        return self._decode_all("strategy_armies", rows)

    async def save_army(self, a: dict):
        await self.db.conn.execute("""
            UPDATE strategy_armies SET world_id=?, faction_id=?, owner_user_id=?, name=?,
                node_id=?, edge_pos=?, characters_json=?, troop_type=?, veteran=?, troops=?,
                grain=?, morale=?, state=?, path_json=?, arrive_tick=?, encircle_ticks=?,
                siege_engine=?, reorganize_until_tick=?, starve_ticks=?
            WHERE id=?
        """, (
            int(a.get("world_id", 0)), int(a.get("faction_id", 0)), a.get("owner_user_id", ""),
            a.get("name", ""), int(a.get("node_id", 0)), float(a.get("edge_pos", 0)),
            json.dumps(a.get("characters") or [], ensure_ascii=False),
            a.get("troop_type", "sword_shield"), int(a.get("veteran", 0)),
            int(a.get("troops", 0)), int(a.get("grain", 0)), int(a.get("morale", 100)),
            a.get("state", "idle"),
            json.dumps(a.get("path") or [], ensure_ascii=False),
            int(a.get("arrive_tick", 0)), int(a.get("encircle_ticks", 0)),
            int(a.get("siege_engine", 0)), int(a.get("reorganize_until_tick", 0)),
            int(a.get("starve_ticks", 0) or 0),
            int(a["id"]),
        ))

    async def insert_army(self, a: dict) -> int:
        aid = await self._execute("""
            INSERT INTO strategy_armies (world_id, faction_id, owner_user_id, name, node_id,
                characters_json, troop_type, veteran, troops, grain, morale, state)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(a.get("world_id", 0)), int(a.get("faction_id", 0)), a.get("owner_user_id", ""),
            a.get("name", ""), int(a.get("node_id", 0)),
            json.dumps(a.get("characters") or [], ensure_ascii=False),
            a.get("troop_type", "sword_shield"), int(a.get("veteran", 0)),
            int(a.get("troops", 0)), int(a.get("grain", 0)), int(a.get("morale", 100)),
            a.get("state", "idle"),
        ))
        a["id"] = aid
        return aid

    async def delete_army(self, army_id: int):
        await self.db.conn.execute("DELETE FROM strategy_armies WHERE id=?", (army_id,))

    async def get_tasks(self, world_id: int, character_id: int = 0,
                        status: str = "active") -> list[dict]:
        sql = "SELECT * FROM strategy_tasks WHERE world_id=?"
        params: list = [world_id]
        if character_id:
            sql += " AND character_id=?"
            params.append(character_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        rows = await self._fetchall(sql, tuple(params))
        return self._decode_all("strategy_tasks", rows)

    async def get_battles(self, world_id: int, limit: int = 30,
                          faction_id: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM strategy_battles WHERE world_id=?"
        params: list = [world_id]
        if faction_id is not None:
            sql += " AND (attacker_faction=? OR defender_faction=?)"
            params.extend([faction_id, faction_id])
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = await self._fetchall(sql, tuple(params))
        return self._decode_all("strategy_battles", rows)

    async def insert_battle(self, b: dict) -> int:
        result = b.get("result") or {}
        result = self._clip_battle_result(result)
        report = str(b.get("report_text", ""))
        limit = int((self.main_cfg().get("security") or {}).get(
            "result_max_bytes", 51200) or 51200)
        if len(report.encode("utf-8")) > limit:
            half = max(200, limit // 2 - 20)
            report = (report.encode("utf-8")[:half].decode("utf-8", "ignore")
                      + "\n…（战报过长已截断）…\n"
                      + report.encode("utf-8")[-half:].decode("utf-8", "ignore"))
        return await self._execute("""
            INSERT INTO strategy_battles (world_id, node_id, attacker_faction, defender_faction,
                kind, mode, state, round_no, map_seed, armies_snapshot_json, result_json,
                report_text, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(b.get("world_id", 0)), int(b.get("node_id", 0)),
            int(b.get("attacker_faction", 0)), int(b.get("defender_faction", 0)),
            b.get("kind", "field"), b.get("mode", "auto"), b.get("state", "done"),
            int(b.get("round_no", 0)), str(b.get("map_seed", "")),
            json.dumps(b.get("armies_snapshot") or [], ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
            report, int(b.get("created_at", _now())),
        ))

    def _clip_battle_result(self, result: dict) -> dict:
        """战报 result_json 体积上限（§18.4）；超出则保留摘要 + 首尾片段。"""
        limit = int((self.main_cfg().get("security") or {}).get(
            "result_max_bytes", 51200) or 51200)
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return {"truncated": True}
        if len(text.encode("utf-8")) <= limit:
            return result
        half = max(200, limit // 2 - 200)
        summary = {k: v for k, v in result.items()
                   if not isinstance(v, (list, dict))}
        summary["truncated"] = True
        summary["head"] = text[:half]
        summary["tail"] = text[-half:]
        return summary

    async def get_active_events(self, world_id: int, tick_no: int) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM strategy_events WHERE world_id=? AND start_tick<=? AND end_tick>=?",
            (world_id, tick_no, tick_no))
        return self._decode_all("strategy_events", rows)

    async def save_world(self, w: dict):
        await self.db.conn.execute("""
            UPDATE strategy_worlds SET season_no=?, template_id=?, started_at=?, ends_at=?,
                state=?, last_tick_at=?, tick_no=?, data_json=?
            WHERE id=?
        """, (
            int(w.get("season_no", 1)), w.get("template_id", "standard36"),
            int(w.get("started_at", 0)), int(w.get("ends_at", 0)), w.get("state", "active"),
            int(w.get("last_tick_at", 0)), int(w.get("tick_no", 0)),
            json.dumps(w.get("data") or {}, ensure_ascii=False), int(w["id"]),
        ))

    # ------------------------------------------------------------------
    # 世界生命周期（§10：建世界 / 幂等）
    # ------------------------------------------------------------------
    async def ensure_world(self) -> dict:
        """取活跃世界；不存在则按模板创建（含 NPC 势力与无主残军）。"""
        world = await self.get_world()
        if world:
            return world
        async with self._world_lock:
            world = await self.get_world()
            if world:
                return world
            return await self._create_world()

    async def _create_world(self, season_no: int = 0, template_id: str = "") -> dict:
        cfg = self.main_cfg()
        template_id = str(template_id or "").strip() or \
            cfg.get("world_template", "standard36")
        templates = (self.cfg.get("world") or {}).get("templates") or []
        tpl = next((t for t in templates if t.get("template_id") == template_id), None)
        if not tpl and templates:
            tpl = templates[0]
        if not tpl:
            raise StrategyError("世界模板缺失：config/strategy_world.json")
        now = _now()
        if int(season_no) <= 0:
            # C-M9：默认赛季号 = 历史最大 + 1（防世界重建后 season_no 撞号，
            # 导致赛季存档/英烈祠记录互相覆盖）
            row = await self._fetchone(
                "SELECT COALESCE(MAX(season_no), 0) + 1 AS n FROM strategy_worlds")
            season_no = int((row or {}).get("n", 1) or 1)
        season_days = int(cfg.get("season_days", 75))
        wid = await self._execute("""
            INSERT INTO strategy_worlds (season_no, template_id, started_at, ends_at,
                state, last_tick_at, tick_no, data_json)
            VALUES (?,?,?,?, 'active',?,0,'{}')
        """, (int(season_no), tpl.get("template_id", template_id), now,
              now + season_days * 86400, now))
        await self._commit()

        # --- 节点 ---
        ownerless = cfg.get("ownerless_garrison") or {}
        nodes: list[dict] = []
        for n in tpl.get("nodes") or []:
            ntype = n.get("type", "ruin")
            fortified = ntype in ("city", "pass", "branch")
            node = {
                "world_id": wid, "id": int(n["id"]), "name": n.get("name", ""),
                "type": ntype, "terrain": n.get("terrain", "plain"), "level": 1,
                "owner_faction": 0, "buildings": [],
                "defense": R.city_defense_max(1) if fortified else 0,
                "morale": 80, "stock": {}, "output": {},
                "pos_x": int(n.get("pos_x", 0)), "pos_y": int(n.get("pos_y", 0)),
                "adj": [int(x) for x in (n.get("adj") or [])],
                "siege_state": {},
                "npc_garrison": {"troops": int(ownerless.get(ntype, 800)), "morale": 80},
                "protect_until": 0, "contribution": {},
            }
            nodes.append(node)
        node_by_id = {n["id"]: n for n in nodes}

        # --- NPC 势力（§9.1：6~8 家，占 ~60% 节点）---
        npc_cfg = cfg.get("npc") or {}
        rng = random.Random(wid * 7919 + now % 1000)
        f_count = rng.randint(*npc_cfg.get("faction_count", [6, 8]))
        npc_names = self.cfg.get("npc_names") or {}
        pers_keys = list((npc_names.get("personalities") or {}).keys()) or [
            "expansive", "developer", "trader", "hermit", "demonic"]
        # 性格模板抽取（可重复，名字不重复）
        chosen = rng.choices(pers_keys, k=f_count)
        city_nodes = [n for n in nodes if n["type"] == "city"]
        other_nodes = [n for n in nodes if n["type"] != "city"]
        rng.shuffle(city_nodes)
        rng.shuffle(other_nodes)
        capitals = (city_nodes + other_nodes)[:f_count]
        used_names: set[str] = set()
        faction_ids: list[int] = []
        npc_persons: list[dict] = []
        for i, pers_key in enumerate(chosen):
            pers = dict((npc_names.get("personalities") or {}).get(pers_key) or {})
            pers["key"] = pers_key
            pool = list((npc_names.get("faction_names") or {}).get(pers_key) or [f"势力{i+1}"])
            pool = [p for p in pool if p not in used_names] or [f"{pers_key}·{i+1}"]
            fname = rng.choice(pool)
            used_names.add(fname)
            color = "#%06x" % rng.randint(0x333333, 0xFFFFFF)
            fid = await self._execute("""
                INSERT INTO strategy_factions (world_id, name, color, kind, leader_id,
                    personality_json, treasury_json, reputation, level, member_cap,
                    capital_node_id, faction_state, alive)
                VALUES (?,?,?,'npc','',?,?,0,1,10,?,'normal',1)
            """, (
                wid, fname, color,
                json.dumps(pers, ensure_ascii=False),
                json.dumps({"coin": rng.randint(8000, 20000),
                            "grain": rng.randint(5000, 15000), "iron": 0, "gold": 0}),
                int(capitals[i]["id"]),
            ))
            await self._commit()
            faction_ids.append(fid)
            # 势力主帅（NPC 武将，rare/epic）
            leader = R.generate_character(
                rng, rarity=rng.choice(["rare", "epic"]), role=R.ROLE_GENERAL,
                name_pools=self.cfg.get("names") or {})
            leader.update({"world_id": wid, "faction_id": fid, "owner_user_id": "",
                           "status": "idle", "loyalty": 100})
            await self.insert_char(leader)
            await self._commit()
            npc_persons.append({"id": int(fid), "personality": pers,
                                "name": fname})

        # NPC 之间的初始外交关系与好感（性格驱动，§9.2）
        await self.seed_npc_diplomacy(wid, npc_persons, rng)

        # --- 分配节点：首都 + BFS 扩张至 owned_node_ratio ---
        target_owned = int(len(nodes) * float(npc_cfg.get("owned_node_ratio", 0.6)))
        owned_count = 0
        frontier: list[tuple[int, int]] = []  # (node_id, faction_id)
        for fid, cap in zip(faction_ids, capitals):
            cap_node = node_by_id[int(cap["id"])]
            cap_node["owner_faction"] = fid
            cap_node["level"] = 2
            cap_node["defense"] = R.city_defense_max(2) if cap_node["type"] in (
                "city", "pass", "branch") else 0
            owned_count += 1
            for adj in cap_node["adj"]:
                frontier.append((adj, fid))
        rng.shuffle(frontier)
        while frontier and owned_count < target_owned:
            nid, fid = frontier.pop(0)
            node = node_by_id.get(nid)
            if not node or node["owner_faction"] != 0:
                continue
            node["owner_faction"] = fid
            owned_count += 1
            for adj in node["adj"]:
                if node_by_id.get(adj, {}).get("owner_faction", -1) == 0:
                    frontier.append((adj, fid))

        # --- NPC 势力驻军（比无主残军强一档）---
        for node in nodes:
            if node["owner_faction"] in faction_ids:
                base = int(ownerless.get(node["type"], 800))
                node["npc_garrison"] = {"troops": int(base * 1.2), "morale": 85}
            await self.db.conn.execute("""
                INSERT INTO strategy_nodes (id, world_id, name, type, terrain, level,
                    owner_faction, buildings_json, defense, morale, stock_json, output_json,
                    pos_x, pos_y, adj_json, siege_state_json, npc_garrison_json,
                    protect_until, contribution_json)
                VALUES (?,?,?,?,?,?,?,'[]',?,?,'{}','{}',?,?,?,'{}',?,0,'{}')
            """, (
                node["id"], wid, node["name"], node["type"], node["terrain"], node["level"],
                node["owner_faction"], node["defense"], node["morale"],
                node["pos_x"], node["pos_y"],
                json.dumps(node["adj"]),
                json.dumps(node["npc_garrison"]),
            ))
        await self._commit()
        logger.info(f"[strategy] 世界#{wid} 创建完成：{len(nodes)} 节点 / "
                    f"{len(faction_ids)} 个 NPC 势力 / NPC 占据 {owned_count} 节点")
        world = await self.get_world()
        return world

    # ------------------------------------------------------------------
    # 玩家入场（P0：个人势力 + 新手包）
    # ------------------------------------------------------------------
    async def _honor_from_previous_season(self, user_id: str,
                                          world: dict) -> dict:
        """读取最近一个历史世界的 honor 存档，生成新赛季可结转的荣誉。

        新赛季是全新的 world + strategy_players 行，荣誉只写在旧行上；
        入场时复制 total/spent 并重置赛季限购计数，才能真正“保留荣誉”。
        """
        row = await self._fetchone(
            "SELECT world_id, honor_json FROM strategy_players "
            "WHERE user_id=? AND world_id<>? ORDER BY world_id DESC LIMIT 1",
            (user_id, int(world["id"])))
        if not row:
            return {}
        old = _loads(row.get("honor_json"), {})
        if not isinstance(old, dict) or not old:
            return {}
        total = int(old.get("total", 0) or 0)
        spent = old.get("spent") or {}
        if not isinstance(spent, dict):
            spent = {}
        if total <= 0 and not spent:
            return {}
        return {
            "total": total,
            "spent": spent,
            "season_spent": {},
            "carried_from": {"world_id": int(row.get("world_id", 0) or 0),
                             "season_total": total},
        }

    async def enter_game(self, user_id: str, nickname: str = "",
                         referral: str = "", node_id: int = 0) -> dict:
        """玩家进入争霸玩法：建个人势力 + 新手包（§12 新手引导）。

        node_id：玩家自选的起始据点；必须是无主、非遗迹且未被匪寨占据的节点。
        referral：引荐人 user_id（追赶礼包引荐奖励用，§17）。
        """
        world = await self.ensure_world()
        wid = world["id"]
        p = await self.get_player(user_id, wid)
        if p:
            p["last_seen_at"] = _now()
            await self.save_player(p)
            # 旧存档自愈：补势力主成员行 / 补发从未领过的新手起始据点
            await self._repair_player_state(world, p)
            await self._commit()
            return await self.get_player(user_id, wid)
        if not int(node_id or 0):
            raise StrategyError("请先选择一座无主据点作为起始主城")
        node = await self.get_node(wid, int(node_id))
        if not node or int(node.get("owner_faction", 0)) != 0:
            raise StrategyError("所选据点已被占据或不存在")
        if node.get("type") == "ruin":
            raise StrategyError("遗迹不可作为起始据点")
        bandits = set(str(x) for x in
                      ((world.get("data") or {}).get("bandits") or {}).keys())
        if str(node["id"]) in bandits:
            raise StrategyError("该据点正被匪寨占据，请另选一处")
        cfg = self.main_cfg()
        pack = cfg.get("starter_pack") or {}
        rng = random.Random(int(hashlib.md5(
            f"{user_id}|{wid}".encode("utf-8")).hexdigest()[:8], 16))
        # 个人势力（P0 简化：kind='player'，多人势力 P1）
        color = "#%06x" % rng.randint(0x333333, 0xFFFFFF)
        fid = await self._execute("""
            INSERT INTO strategy_factions (world_id, name, color, kind, leader_id,
                personality_json, treasury_json, reputation, level, member_cap,
                capital_node_id, faction_state, alive, diplomacy_json)
            VALUES (?,?,?,'player',?,'{}','{}',0,1,10,0,'normal',1,'{}')
        """, (wid, f"{nickname or user_id}的个人势力", color, user_id))
        await self._commit()
        now = _now()
        newcomer = json.dumps({"referred_by": str(referral or "")},
                              ensure_ascii=False)
        await self._execute("""
            INSERT INTO strategy_players (user_id, world_id, faction_id, coin, grain, iron,
                recruit_token, merit, merit_season, auto_delegate, created_at, last_seen_at,
                tutorial_json, newcomer_json)
            VALUES (?,?,?,?,?,0,?,0,0,1,?,?,?,?)
        """, (user_id, wid, fid, int(pack.get("coin", 5000)), int(pack.get("grain", 2000)),
              int(pack.get("recruit_token", 2)), now, now,
              json.dumps({"guide_hidden": False}, ensure_ascii=False), newcomer))
        # 赛季荣誉结转：新世界的玩家行继承上一赛季累计荣誉与已消费额度
        carry_honor = await self._honor_from_previous_season(user_id, world)
        if carry_honor:
            await self._execute(
                "UPDATE strategy_players SET honor_json=? "
                "WHERE user_id=? AND world_id=?",
                (json.dumps(carry_honor, ensure_ascii=False), user_id, wid))
        # 新手角色：凡品武将 + 凡品内政官（§12.1）
        for spec in pack.get("characters") or [{"role": "general", "rarity": "common"},
                                               {"role": "official", "rarity": "common"}]:
            char = R.generate_character(
                rng, rarity=spec.get("rarity", "common"), role=spec.get("role", "general"),
                name_pools=self.cfg.get("names") or {})
            char.update({"world_id": wid, "faction_id": fid, "owner_user_id": user_id,
                         "loyalty": 90})
            await self.insert_char(char)
        # 势力主必须登记在成员名册，否则 _require_position / 带兵上限全部失效
        await self.upsert_member(fid, user_id, position="leader", joined_at=now)
        # 起始据点由玩家自选，但仍需二次校验并写入一次性标记
        player = await self.get_player(user_id, wid)
        faction = await self.get_faction(fid)
        if player and faction:
            await self._claim_starter_home(world, faction, player, int(node_id))
        await self._commit()
        logger.info(f"[strategy] 玩家 {user_id} 入场：个人势力#{fid}")
        return await self.get_player(user_id, wid)

    async def _starter_candidates(self, world: dict) -> list[dict]:
        """可选起始据点：无主、非遗迹、未被匪寨占据。"""
        bandits = set(str(x) for x in
                      ((world.get("data") or {}).get("bandits") or {}).keys())
        out = []
        for n in await self.get_nodes(world["id"]):
            if int(n.get("owner_faction", 0)) != 0:
                continue
            if n.get("type") == "ruin" or str(n["id"]) in bandits:
                continue
            out.append({
                "id": int(n["id"]), "name": n["name"], "type": n["type"],
                "terrain": n["terrain"], "level": int(n.get("level", 1)),
                "pos": [int(n.get("pos_x", 0)), int(n.get("pos_y", 0))],
            })
        out.sort(key=lambda x: (x["type"] != "city", x["level"] * -1, x["id"]))
        return out

    async def _claim_starter_home(self, world: dict, faction: dict, p: dict,
                                  node_id: int) -> int:
        """把玩家自选的起始据点设为主城，并写入一次性标记。"""
        scfg = self.main_cfg().get("starter_pack") or {}
        garrison = max(0, int(scfg.get("home_garrison", 300)))
        protect_days = max(1, int(
            (self.main_cfg().get("protection") or {}).get("newbie_days", 7)))
        bandits = set(str(x) for x in
                      ((world.get("data") or {}).get("bandits") or {}).keys())
        node = await self.get_node(world["id"], int(node_id))
        if not node or int(node.get("owner_faction", 0)) != 0:
            raise StrategyError("所选据点已被占据或不存在")
        if node.get("type") == "ruin":
            raise StrategyError("遗迹不可作为起始据点")
        if str(node["id"]) in bandits:
            raise StrategyError("该据点正被匪寨占据，请另选一处")
        now = _now()
        node["owner_faction"] = int(faction["id"])
        node["level"] = max(1, int(node.get("level", 1)))
        node["morale"] = max(80, int(node.get("morale", 80)))
        node["npc_garrison"] = {"troops": garrison, "morale": 90}
        node["siege_state"] = {}
        if node.get("type") in ("city", "pass", "branch"):
            node["defense"] = R.city_defense_max(int(node["level"]))
        node["protect_until"] = now + protect_days * 86400
        await self.save_node(node)
        faction["capital_node_id"] = int(node["id"])
        await self.save_faction(faction)
        tut = dict(p.get("tutorial") or {})
        tut["home_granted"] = True
        tut["home_node_id"] = int(node["id"])
        p["tutorial"] = tut
        await self.save_player(p)
        logger.info(f"[strategy] 玩家 {p.get('user_id')} 选定起始据点"
                    f"「{node['name']}」#{node['id']}（{node.get('type')}）")
        return int(node["id"])

    async def _grant_starter_home(self, world: dict, faction: dict, p: dict,
                                  tutorial: Optional[dict] = None) -> int:
        """给无地玩家分配一座起始据点（个人势力主城），并写入一次性标记。

        起始据点只用无主据点，优先分舵/坊市/灵谷田/灵脉矿；地图已满时
        退化为任意非遗迹无主节点。分配后 7 天新城保护，避免新手一入场被推平。
        """
        scfg = self.main_cfg().get("starter_pack") or {}
        prefer = [str(t) for t in (scfg.get("home_node_types") or
                                   ["branch", "market", "grain", "mine"])]
        garrison = max(0, int(scfg.get("home_garrison", 300)))
        protect_days = max(1, int(
            (self.main_cfg().get("protection") or {}).get("newbie_days", 7)))
        nodes = await self.get_nodes(world["id"])
        bandits = set(str(x) for x in
                      ((world.get("data") or {}).get("bandits") or {}).keys())
        rank = {t: i for i, t in enumerate(prefer)}
        cands = [n for n in nodes
                 if int(n.get("owner_faction", 0)) == 0
                 and str(n["id"]) not in bandits and n.get("type") in rank]
        if not cands:  # 地图拥挤时退化为任意非遗迹无主节点，避免新手死锁
            cands = [n for n in nodes
                     if int(n.get("owner_faction", 0)) == 0
                     and str(n["id"]) not in bandits and n.get("type") != "ruin"]
        if not cands:
            return 0
        cands.sort(key=lambda n: (rank.get(n.get("type"), len(rank)), int(n["id"])))
        node = await self.get_node(world["id"], int(cands[0]["id"]))
        if not node or int(node.get("owner_faction", 0)) != 0:
            return 0
        now = _now()
        node["owner_faction"] = int(faction["id"])
        node["level"] = max(1, int(node.get("level", 1)))
        node["morale"] = max(80, int(node.get("morale", 80)))
        node["npc_garrison"] = {"troops": garrison, "morale": 90}
        node["siege_state"] = {}
        if node.get("type") in ("city", "pass", "branch"):
            node["defense"] = R.city_defense_max(int(node["level"]))
        node["protect_until"] = now + protect_days * 86400
        await self.save_node(node)
        faction["capital_node_id"] = int(node["id"])
        await self.save_faction(faction)
        tut = dict(tutorial if tutorial is not None else (p.get("tutorial") or {}))
        tut["home_granted"] = True
        tut["home_node_id"] = int(node["id"])
        p["tutorial"] = tut
        await self.save_player(p)
        logger.info(f"[strategy] 玩家 {p.get('user_id')} 获得起始据点"
                    f"「{node['name']}」#{node['id']}（{node.get('type')}）")
        return int(node["id"])

    async def _repair_player_state(self, world: dict, p: dict) -> bool:
        """修复旧存档：补势力主成员行 + 给从未领过据点的新手补发起始据点。

        仅对「个人势力」（单成员）生效，且 home_granted 只会发放一次；
        因此赛季中后期被推平的玩家不会反复白嫖新据点，仍须走灵石重建。
        """
        f = await self.get_faction(int(p.get("faction_id") or 0))
        if not f or f.get("kind") != "player":
            return False
        changed = False
        members = await self.get_members(f["id"])
        if str(f.get("leader_id") or "") == p["user_id"] and \
                not any(m["user_id"] == p["user_id"] for m in members):
            await self.upsert_member(f["id"], p["user_id"], position="leader",
                                     joined_at=int(p.get("created_at") or _now()))
            changed = True
            members = await self.get_members(f["id"])
        tut = p.get("tutorial") or {}
        if not tut.get("home_granted"):
            owned = await self._fetchone(
                "SELECT COUNT(*) AS n FROM strategy_nodes "
                "WHERE world_id=? AND owner_faction=?",
                (world["id"], int(f["id"])))
            if int((owned or {}).get("n", 0)) == 0 and \
                    not int(f.get("capital_node_id") or 0):
                if len(members) <= 1 and \
                        await self._grant_starter_home(world, f, p, tut):
                    changed = True
            else:
                # 已有地盘/主城的老玩家只需打标，避免以后被推平时反复补发
                tut["home_granted"] = True
                tut["home_node_id"] = int(f.get("capital_node_id") or 0)
                p["tutorial"] = tut
                await self.save_player(p)
                changed = True
        if changed:
            await self._commit()
        return changed

    async def get_player_faction(self, user_id: str) -> Optional[dict]:
        world = await self.get_world()
        if not world:
            return None
        p = await self.get_player(user_id, world["id"])
        if not p or not p.get("faction_id"):
            return None
        return await self.get_faction(p["faction_id"])

    # ------------------------------------------------------------------
    # 酒馆招募（§6.1）
    # ------------------------------------------------------------------
    async def _recruit_quality_bonus(self, faction_id: int) -> float:
        """中郎将（角色官职）+ 酒馆分号提供的招募高品质概率加成。"""
        f = await self.get_faction(int(faction_id or 0))
        if not f:
            return 0.0
        host = await self._alliance_host(f) or f
        bonus = await self._faction_position_bonus(host)
        # 酒馆分号：本地招募稀有 +3%/级（§6.6，取本势力最高等级）
        best = 0
        for node in await self.get_nodes(int(f["world_id"])):
            if int(node.get("owner_faction") or 0) == int(f["id"]):
                best = max(best, self._building_level(node, "tavern_branch"))
        return float(bonus.get("recruit_quality", 0.0)) + 0.03 * min(5, best)

    async def _tavern(self, player: dict, world_id: int) -> dict:
        """取/刷新酒馆候选（4 小时一刷，手刷 300 币 ×3/日）。"""
        cfg = (self.main_cfg().get("recruit") or {})
        tavern = player.get("tavern") or {}
        now = _now()
        refresh_sec = int(cfg.get("refresh_hours", 4)) * 3600
        if not tavern.get("candidates") or now >= int(tavern.get("refresh_at", 0)):
            qb = await self._recruit_quality_bonus(int(player.get("faction_id") or 0))
            tavern = self._roll_tavern(player["user_id"], world_id, tavern, cfg, now,
                                       quality_bonus=qb)
            tavern["refresh_at"] = now + refresh_sec
            player["tavern"] = tavern
            await self.save_player(player)
            await self._commit()
        return tavern

    def _roll_tavern(self, user_id: str, world_id: int, old: dict, cfg: dict,
                     now: int, salt: int = 0, quality_bonus: float = 0.0,
                     role_force: str = "") -> dict:
        # salt=0：自动刷新按小时粒度稳定候选；手动刷新传精确时间戳，
        # 保证同小时内多次付费刷新得到不同候选
        rng = random.Random(int(hashlib.md5(
            f"{user_id}|{world_id}|{now // 3600}|{salt}".encode("utf-8")).hexdigest()[:8], 16))
        n = int(cfg.get("candidates", 5))
        role_keys = list((cfg.get("role_weights") or {}).keys()) or list(R.ROLES)
        role_w = [int((cfg.get("role_weights") or {}).get(k, 1)) for k in role_keys]
        if role_force in R.ROLES:
            # 举荐信：定向刷新为指定职责（候选池全部为该职责）
            role_keys, role_w = [role_force], [1]
        rar_keys = list((cfg.get("rarity_weights_normal") or {}).keys()) or ["common"]
        rar_w = [int((cfg.get("rarity_weights_normal") or {}).get(k, 0)) for k in rar_keys]
        if quality_bonus > 0:
            # 中郎将：高品质档位权重 ×(1+10%)（common 不变，避免抽到白身的概率被稀释）
            rar_w = [w if k == "common" else max(w + 1, int(round(w * (1 + quality_bonus))))
                     for k, w in zip(rar_keys, rar_w)]
        cands = []
        for _ in range(n):
            c = R.generate_character(
                rng,
                rarity=rng.choices(rar_keys, weights=rar_w)[0],
                role=rng.choices(role_keys, weights=role_w)[0],
                name_pools=self.cfg.get("names") or {})
            cands.append({k: c[k] for k in ("name", "role", "rarity", "profession",
                                            "stats", "traits", "aptitude")})
        if old.get("daily_refresh_date") == time.strftime("%Y%m%d", time.localtime(now)):
            daily = int(old.get("daily_refresh_used", 0))
        else:
            daily = 0
        return {"candidates": cands, "refreshed_at": now, "daily_refresh_date":
                time.strftime("%Y%m%d", time.localtime(now)), "daily_refresh_used": daily}

    async def refresh_tavern(self, player: dict, world_id: int, manual: bool = False) -> dict:
        cfg = self.main_cfg().get("recruit") or {}
        tavern = player.get("tavern") or {}
        if manual:
            used = int(tavern.get("daily_refresh_used", 0))
            limit = int(cfg.get("manual_refresh_daily", 3))
            if used >= limit:
                raise StrategyError(f"今日手动刷新次数已用完（{limit}次/日）")
            cost = int(cfg.get("manual_refresh_coin", 300))
            if player["coin"] < cost:
                raise StrategyError(f"铜钱不足：手动刷新需 {cost}")
            player["coin"] -= cost
            qb = await self._recruit_quality_bonus(int(player.get("faction_id") or 0))
            tavern = self._roll_tavern(player["user_id"], world_id, tavern, cfg,
                                       _now(), salt=_now(), quality_bonus=qb)
            tavern["daily_refresh_used"] = used + 1
            tavern["refresh_at"] = _now() + int(cfg.get("refresh_hours", 4)) * 3600
            player["tavern"] = tavern
            await self.save_player(player)
            await self._commit()
            return tavern
        return await self._tavern(player, world_id)

    async def recruit(self, player: dict, world_id: int, index: int,
                      premium: bool = False) -> dict:
        """招募酒馆候选（普通 800 币；高级用募将令，保底 40 抽必得 epic+）。"""
        cfg = self.main_cfg().get("recruit") or {}
        tavern = await self._tavern(player, world_id)
        cands = tavern.get("candidates") or []
        if not (0 <= int(index) < len(cands)):
            raise StrategyError("候选序号无效")
        cap_info = await self._char_cap_info(world_id, player)
        cap = int(cap_info["cap"])
        owned = await self._roster_count(player["user_id"], world_id)
        if owned >= cap:
            raise StrategyError(
                f"角色数量已达上限（{owned}/{cap}）"
                f"：当前据点 {cap_info['nodes']} 座，扩建据点可增加名额")
        rng = self._rng("recruit", player["user_id"], world_id, _now() // 60, index)
        if premium:
            if player["recruit_token"] < int(cfg.get("premium_cost_token", 1)):
                raise StrategyError("募将令不足")
            player["recruit_token"] -= 1
            pity = int((player.get("honor") or {}).get("pity", 0)) + 1
            honor = player.get("honor") or {}
            cand = dict(cands[index])
            if pity >= int(cfg.get("premium_pity", 40)):
                cand["rarity"] = rng.choice(["epic", "legend"])
                pity = 0
            elif cand["rarity"] in ("epic", "legend"):
                pity = 0
            honor["pity"] = pity
            player["honor"] = honor
        else:
            cost = int(cfg.get("normal_cost_coin", 800))
            if player["coin"] < cost:
                raise StrategyError(f"铜钱不足：普通招募需 {cost}")
            player["coin"] -= cost
            cand = dict(cands[index])
        char = {
            "name": cand["name"], "role": cand["role"], "rarity": cand["rarity"],
            "profession": cand["profession"], "stats": cand.get("stats") or {},
            "traits": cand.get("traits") or [], "aptitude": cand.get("aptitude") or {},
            "exp": 0, "level": 1, "loyalty": rng.randint(60, 80), "injury": 0,
            "stamina": 100, "status": "idle", "bonds": {},
            "world_id": world_id, "faction_id": player["faction_id"],
            "owner_user_id": player["user_id"],
        }
        await self.insert_char(char)
        # §4.5 知遇：招募者与新人建立知遇羁绊（取主魅力最高的空闲角色）
        recruiters = [c for c in await self.get_chars(
            owner_user_id=player["user_id"], world_id=world_id,
            status="idle") if int(c["id"]) != int(char["id"])]
        if recruiters:
            recruiter = max(
                recruiters, key=lambda c: int((c.get("stats") or {}).get("charm", 0)))
            char["recruited_by"] = int(recruiter["id"])
            R.form_zhiyu(recruiter, char)
            await self.save_char(recruiter)
            await self.save_char(char)
        cands.pop(int(index))
        tavern["candidates"] = cands
        player["tavern"] = tavern
        await self.save_player(player)
        await self._commit()
        return char

    async def dismiss_char(self, user_id: str, char_id: int) -> dict:
        """遣散空闲角色，腾出名册名额（官职需先卸任，军团中需先解散）。"""
        world, p = await self._require_player(user_id)
        c = await self._own_char(p, int(char_id))
        if c.get("status") in ("dead", "captive"):
            raise StrategyError("该角色已陨落/被俘，无法遣散")
        if c.get("status") != "idle":
            raise StrategyError(f"{c['name']} 当前状态无法遣散，请先结束任务/解散军团")
        if str(c.get("appointed_position") or ""):
            raise StrategyError("该角色仍有官职，请先卸任再遣散")
        for a in await self.get_armies(world["id"], owner_user_id=user_id):
            if int(char_id) in [int(x) for x in (a.get("characters") or [])]:
                raise StrategyError("该角色仍在军团中，请先解散军团")
        # 清理其他角色指向该角色的羁绊，避免面板显示悬空人物
        for other in await self.get_chars(owner_user_id=user_id,
                                          world_id=world["id"]):
            bonds = other.get("bonds") or {}
            if str(char_id) in bonds:
                bonds.pop(str(char_id), None)
                other["bonds"] = bonds
                await self.save_char(other)
        await self._execute(
            "DELETE FROM strategy_characters WHERE id=? AND world_id=? AND owner_user_id=?",
            (int(char_id), int(world["id"]), user_id))
        await self._commit()
        return {"ok": True, "msg": f"已遣散 {c['name']}，名册腾出 1 个名额"}

    # ------------------------------------------------------------------
    # 主玩法灵石注入（§10.4：日限额 + 超额转战功）
    # ------------------------------------------------------------------
    async def _inject_gold(self, p: dict, amount: int) -> tuple[int, int]:
        """给主玩法钱包加灵石；超日限额部分按比率转战功。返回 (实发gold, 转merit)。

        接收调用方的 strategy_players dict 并就地更新（daily_output_log/merit/
        merit_season），由调用方统一 save_player(p) 落库——避免函数内部重读的
        新日志被调用方手里的旧快照覆盖，导致灵石日限额彻底失效。
        """
        if amount <= 0 or not p:
            return 0, 0
        gcfg = self.main_cfg().get("gold_output") or {}
        daily_cap = int(gcfg.get("daily_gold_cap", 5_000_000))
        hard_cap = int(gcfg.get("max_daily_gold_cap", 20_000_000))
        rate = float(gcfg.get("overcap_to_merit_rate", 0.00002))
        user_id = p["user_id"]
        today = time.strftime("%Y%m%d")
        log = p.get("daily_output_log") or {}
        today_sent = int(log.get(today, 0))
        if today_sent >= hard_cap:
            extra_merit = int(amount * rate)
            if extra_merit > 0:
                p["merit"] = int(p.get("merit", 0)) + extra_merit
                p["merit_season"] = int(p.get("merit_season", 0)) + extra_merit
            return 0, extra_merit
        allow = min(amount, max(0, daily_cap - today_sent))
        overflow = amount - allow
        merit_gain = int(overflow * rate)
        if allow > 0:
            # 主玩法钱包：玩家锁防并发写
            async with _main_wallet_lock(user_id):
                player = await self.db.get_player_by_id(user_id)
                if player is None:
                    # 没有主玩法角色：不记日限额、不返回已发放额度
                    return 0, 0
                player.gold = int(player.gold or 0) + allow
                # tick 事务内绝不提前 commit：否则整段 tick 的原子性被破坏
                # （时钟已落库、后续步骤失败回滚失效）。
                await self.db.update_player(
                    player, commit=not self._in_tick_task())
            log[today] = today_sent + allow
            p["daily_output_log"] = log
        if merit_gain > 0:
            p["merit"] = int(p.get("merit", 0)) + merit_gain
            p["merit_season"] = int(p.get("merit_season", 0)) + merit_gain
        return allow, merit_gain

    # ------------------------------------------------------------------
    # 动作公共校验
    # ------------------------------------------------------------------
    async def _require_player(self, user_id: str) -> tuple[dict, dict]:
        world = await self.ensure_world()
        p = await self.get_player(user_id, world["id"])
        if not p:
            raise StrategyError("尚未入场，请先调用 enter_game")
        return world, p

    async def _owned_node(self, p: dict, node_id: int) -> dict:
        node = await self.get_node(p["world_id"], int(node_id))
        if not node:
            raise StrategyError("据点不存在")
        if node["owner_faction"] != p["faction_id"]:
            raise StrategyError(f"「{node['name']}」不是我方据点")
        return node

    async def _own_char(self, p: dict, char_id: int) -> dict:
        c = await self.get_char(int(char_id))
        if not c or c.get("owner_user_id") != p["user_id"]:
            raise StrategyError("角色不存在或不属于你")
        return c

    async def _own_army(self, p: dict, army_id: int) -> dict:
        a = await self.get_army(int(army_id))
        if not a or a.get("owner_user_id") != p["user_id"]:
            raise StrategyError("军团不存在或不属于你")
        return a

    @staticmethod
    def _is_reorganizing(army: dict, world: dict) -> bool:
        """溃败重整期判定（§7.7 battle.reorganize_ticks）。"""
        until = int((army or {}).get("reorganize_until_tick", 0) or 0)
        return until > 0 and int((world or {}).get("tick_no", 0) or 0) < until

    @staticmethod
    def _reorg_msg(army: dict, world: dict) -> str:
        left = max(0, int((army or {}).get("reorganize_until_tick", 0) or 0)
                   - int((world or {}).get("tick_no", 0) or 0))
        return f"军团溃败重整中（还需 {left} tick），暂不可出战"

    async def _army_leader(self, army: dict) -> Optional[dict]:
        """取军团中首位仍可领兵的角色（跳过已陨落/被俘/重伤）。"""
        for cid in army.get("characters") or []:
            c = await self.get_char(int(cid))
            if not c or c.get("status") in ("dead", "captive"):
                continue
            if not R.can_act(c):
                continue
            return c
        return None

    async def find_path(self, world_id: int, src: int, dst: int) -> Optional[list[int]]:
        """BFS 连通性（§5.6：行军必须沿通路）。"""
        if src == dst:
            return [dst]
        nodes = {n["id"]: n for n in await self.get_nodes(world_id)}
        if src not in nodes or dst not in nodes:
            return None
        from collections import deque
        q = deque([src])
        prev = {src: None}
        while q:
            cur = q.popleft()
            for nxt in nodes.get(cur, {}).get("adj") or []:
                if nxt in prev or nxt not in nodes:
                    continue
                prev[nxt] = cur
                if nxt == dst:
                    path = [dst]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])
                    return list(reversed(path))
                q.append(nxt)
        return None

    async def _governance_node_count(self, world_id: int, f: dict) -> int:
        """势力（含联盟成员势力）当前拥有的据点总数，用于官职开放档位。"""
        ids = await self._alliance_ids(int(world_id), f)
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        row = await self._fetchone(
            f"SELECT COUNT(*) AS c FROM strategy_nodes WHERE world_id=? "
            f"AND owner_faction IN ({marks})",
            (int(world_id), *sorted(int(x) for x in ids)))
        return int((row or {}).get("c", 0) or 0)

    async def _char_cap_info(self, world_id: int, p: dict) -> dict:
        """可招募角色上限：基础名额 + 本势力据点数量 × 每据点名额。

        以玩家自己的势力据点为准（联盟盟友的据点不计入），上限随据点
        扩张而增长，并在 ``character_cap_max`` 处封顶；被俘、陨落角色
        不占名册名额，避免战损后永久卡死招募。
        """
        rcfg = self.main_cfg().get("recruit") or {}
        base = int(rcfg.get("character_cap_base", 4))
        per_node = int(rcfg.get("character_cap_per_node", 2))
        hard_max = int(rcfg.get("character_cap_max", 30) or 0)
        f = await self.get_faction(int(p.get("faction_id") or 0))
        nodes = 0
        if f:
            row = await self._fetchone(
                "SELECT COUNT(*) AS c FROM strategy_nodes "
                "WHERE world_id=? AND owner_faction=?",
                (int(world_id), int(f["id"])))
            nodes = int((row or {}).get("c", 0) or 0)
        cap = max(1, base + per_node * max(0, nodes))
        if hard_max > 0:
            cap = min(cap, hard_max)
        return {"cap": cap, "nodes": nodes, "base": base,
                "per_node": per_node, "max": hard_max}

    async def _roster_count(self, user_id: str, world_id: int) -> int:
        """当前占用名册名额的角色数（不含陨落与被俘）。"""
        row = await self._fetchone(
            "SELECT COUNT(*) AS c FROM strategy_characters "
            "WHERE world_id=? AND owner_user_id=? AND status NOT IN ('dead','captive')",
            (int(world_id), user_id))
        return int((row or {}).get("c", 0) or 0)

    def _growth_cap_bonus(self) -> int:
        """任务/战斗历练可额外突破稀有度生成上限的六维总和。"""
        return int((self.main_cfg().get("recruit") or {})
                   .get("stat_growth_cap_bonus", R.DEFAULT_GROWTH_CAP_BONUS))

    async def _alliance_chars(self, world_id: int, f: dict) -> list[dict]:
        """本势力/联盟内的全部可用角色（官职任免与效果统计用）。"""
        ids = await self._alliance_ids(int(world_id), f)
        chars = await self.get_chars(world_id=int(world_id))
        return [c for c in chars
                if int(c.get("faction_id") or 0) in ids and
                c.get("status") not in ("dead", "captive")]

    async def _faction_position_bonus(self, f: Optional[dict]) -> dict:
        """角色级官职聚合出的势力加成（太尉攻/司徒产出/祭酒计略/中郎将招募）。"""
        if not f:
            return R.faction_position_bonus({})
        chars = await self._alliance_chars(int(f["world_id"]), f)
        positions: dict[str, list] = {}
        for c in chars:
            pos = str(c.get("appointed_position") or "")
            if pos:
                positions.setdefault(pos, []).append(c)
        return R.faction_position_bonus(positions)

    async def _leader_cap(self, leader: Optional[dict], faction: Optional[dict],
                          node_id: int = 0) -> int:
        """主将带兵上限：按其角色的官职决定（§4.5）。

        官职绑定角色而非玩家；无官职角色 1500。太守离开所辖据点出征时
        按辖外 1500 计算。
        """
        if not leader or not faction:
            return R.troop_cap("")
        if int(leader.get("faction_id") or 0) != int(faction.get("id") or 0):
            return R.troop_cap("")
        if str(faction.get("kind") or "") == "npc":
            return R.troop_cap("leader")
        pos = str(leader.get("appointed_position") or "")
        outside = (pos == "taishou" and int(leader.get("appointed_node_id") or 0)
                   != int(node_id or 0))
        cap = R.troop_cap(pos, is_taishou_outside=outside)
        # 兵营：本据点出征带兵上限 +5%/级（§6.6）
        if node_id:
            node = await self.get_node(int(faction.get("world_id") or 0), int(node_id))
            lvl = min(5, self._building_level(node, "barracks"))
            if lvl:
                cap = int(cap * (1 + 0.05 * lvl))
        return cap

    # ------------------------------------------------------------------
    # 任务（§6.2）
    # ------------------------------------------------------------------
    async def dispatch_task(self, user_id: str, char_id: int, node_id: int,
                            task_type: str, hours: int,
                            target_node_id: int = 0,
                            partner_char_id: int = 0) -> dict:
        world, p = await self._require_player(user_id)
        tcfg = self.main_cfg().get("task") or {}
        scfg = self.main_cfg().get("stamina") or {}
        if task_type == "quest":
            task_type = "seek"
        if task_type not in R.TASK_TYPES:
            raise StrategyError(f"暂不支持该任务：{task_type}")
        if int(hours) not in [int(h) for h in tcfg.get("hours_options", [1, 2, 4, 8])]:
            raise StrategyError("时长须为 1/2/4/8 小时")
        c = await self._own_char(p, char_id)
        if c["status"] != "idle":
            raise StrategyError(f"{c['name']} 状态为「{c['status']}」，无法派遣")
        if R.injury_state(c["injury"]) in ("dying", "dead"):
            raise StrategyError(f"{c['name']} 伤势过重")
        if scfg.get("enabled", True) and c["stamina"] < int(scfg.get("task_cost", 20)):
            raise StrategyError(f"{c['name']} 体力不足（需 {scfg.get('task_cost', 20)}）")
        # 双人协作（§6.1：协作任务 2 人 +15%）：两人都占用 status='task'
        partner = None
        if int(partner_char_id or 0):
            if int(partner_char_id) == int(char_id):
                raise StrategyError("协作角色不能与执行者是同一人")
            partner = await self._own_char(p, int(partner_char_id))
            if partner["status"] != "idle":
                raise StrategyError(
                    f"{partner['name']} 状态为「{partner['status']}」，无法协作")
            if R.injury_state(partner["injury"]) in ("dying", "dead"):
                raise StrategyError(f"{partner['name']} 伤势过重")
            if scfg.get("enabled", True) and \
                    partner["stamina"] < int(scfg.get("task_cost", 20)):
                raise StrategyError(
                    f"{partner['name']} 体力不足（需 {scfg.get('task_cost', 20)}）")
        ticks = max(1, int(hours) * int(tcfg.get("ticks_per_hour", 4)))
        target_id = int(target_node_id or node_id)
        supplied = 0
        army = None
        if task_type in ("raid", "scout"):
            # 劫掠/侦察：可指向敌方/无主据点（§6.2）
            node = await self.get_node(world["id"], target_id)
            if not node:
                raise StrategyError("目标据点不存在")
            if task_type == "raid":
                if int(node.get("owner_faction") or 0) == int(p["faction_id"]):
                    raise StrategyError("不能劫掠己方据点")
                f = await self.get_faction(int(p["faction_id"]))
                ids = await self._alliance_ids(int(world["id"]), f) if f else set()
                ids.add(int(p["faction_id"]))
                home = await self.get_nodes(world["id"])
                adj_ok = any(
                    int(n["id"]) in ids and int(node["id"]) in (n.get("adj") or [])
                    for n in home)
                if not adj_ok:
                    raise StrategyError("劫掠目标须与己方/盟友据点相邻")
        elif task_type == "supply":
            # 运粮：从玩家粮草扣，等量补给我方军团（§6.2）
            node = await self.get_node(world["id"], target_id)
            if not node:
                raise StrategyError("目标据点不存在")
            armies = [a for a in await self.get_armies(
                world["id"], faction_id=int(p["faction_id"]), node_id=int(node["id"]))
                if int(a.get("troops", 0)) > 0]
            if not armies:
                raise StrategyError("目标据点没有我方军团可补给")
            army = armies[0]
            far = bool(army.get("path")) and len(army.get("path") or []) > int(
                (self.main_cfg().get("army") or {}).get("far_supply_steps", 2))
            need = max(1, R.grain_upkeep(army, node.get("terrain", "plain"), far)) * ticks
            if int(p["grain"]) < need:
                raise StrategyError(f"粮草不足：运粮需 {need}，现有 {int(p['grain'])}")
            p["grain"] -= need
            army["grain"] = int(army.get("grain", 0)) + need
            supplied = need
            await self.save_player(p)
            await self.save_army(army)
        else:
            node = await self._owned_node(p, node_id)
        if R.spend_stamina(c, "task", bool(scfg.get("enabled", True))):
            pass
        c["status"] = "task"
        c["location"] = {"node_id": node["id"]}
        await self.save_char(c)
        if partner is not None:
            R.spend_stamina(partner, "task", bool(scfg.get("enabled", True)))
            partner["status"] = "task"
            partner["location"] = {"node_id": node["id"]}
            await self.save_char(partner)
        now = _now()
        tid = await self._execute("""
            INSERT INTO strategy_tasks (world_id, faction_id, character_id, node_id,
                task_type, start_at, end_at, payout_json, status)
            VALUES (?,?,?,?,?,?,?,?, 'active')
        """, (world["id"], p["faction_id"], c["id"], node["id"], task_type,
              now, now + int(hours) * 3600,
              json.dumps({"ticks": ticks,
                          "partner_char_id": int(partner["id"]) if partner else 0,
                          "supplied": supplied,
                          "target_node_id": int(node["id"])})))
        await self._commit()
        extra = f"，与 {partner['name']} 协作" if partner is not None else ""
        return {"ok": True,
                "msg": f"{c['name']} 开始在「{node['name']}」执行"
                       f"{R.TASK_NAMES.get(task_type, task_type)}（{hours}小时）{extra}",
                "task_id": tid}

    async def recall_task(self, user_id: str, task_id: int) -> dict:
        """提前召回：收益 ×50%（recall_loss）。"""
        world, p = await self._require_player(user_id)
        row = await self._fetchone(
            "SELECT * FROM strategy_tasks WHERE id=? AND world_id=?", (task_id, world["id"]))
        task = self._decode("strategy_tasks", row)
        if not task or task["status"] != "active":
            raise StrategyError("任务不存在或已结束")
        c = await self.get_char(task["character_id"])
        if not c or c.get("owner_user_id") != p["user_id"]:
            raise StrategyError("无权操作该任务")
        tcfg = self.main_cfg().get("task") or {}
        elapsed = max(0, _now() - task["start_at"])
        total = max(1, task["end_at"] - task["start_at"])
        full_ticks = int((task.get("payout") or {}).get("ticks", 4))
        got_ticks = max(0, int(full_ticks * elapsed / total *
                               (1 - float(tcfg.get("recall_loss", 0.5)))))
        # 刚派出就召回时可能结算 0 tick；不能再走 settle_task（其内部按
        # max(1, ticks) 兜底，会白送 1 tick 收益）
        payout = await self._pay_task(world, p, task, c, got_ticks) \
            if got_ticks > 0 else {"kind": "none", "amount": 0}
        task["status"] = "recalled"
        await self.db.conn.execute(
            "UPDATE strategy_tasks SET status='recalled' WHERE id=?", (task_id,))
        c["status"] = "idle"
        await self.save_char(c)
        # 双人协作：召回时同步释放搭档，避免永久占用
        partner_id = int((task.get("payout") or {}).get("partner_char_id") or 0)
        if partner_id:
            partner = await self.get_char(partner_id)
            if partner and partner.get("status") not in ("captive", "dead"):
                partner["status"] = "idle"
                await self.save_char(partner)
        await self._commit()
        return {"ok": True, "msg": f"召回成功（折损后结算 {got_ticks} tick）",
                "payout": payout}

    async def _pay_task(self, world: dict, p: dict, task: dict, c: dict,
                        ticks: int) -> dict:
        """按 tick 数结算任务收益入账（§6.2 产出映射）。"""
        cfg = self.main_cfg()
        tcfg = cfg.get("task") or {}
        base_map = dict(tcfg.get("base_per_tick") or {})
        base_map.setdefault("seek", base_map.pop("quest", 60))
        node = await self.get_node(world["id"], task["node_id"])
        rng = self._rng("task", task["id"], ticks)
        # 事件修正（灵雨 +20% 等）
        event_mult = await self._task_event_mult(world, task, node)
        amount = R.settle_task(
            rng, task["task_type"], c, float(base_map.get(task["task_type"], 50)),
            ticks, (node or {}).get("type", "ruin"),
            int((node or {}).get("morale", 80)), event_mult)
        amount = max(0, int(amount))
        # 双人协作：收益 ×1.15（§6.1）
        payout_meta = task.get("payout") or {}
        if int(payout_meta.get("partner_char_id") or 0):
            amount = int(amount * 1.15)
        # 任务小事件（§6.2 / task.mini_event_chance；概率直接传配置值）
        mini = R.mini_event_roll(rng, float(
            tcfg.get("mini_event_chance", R.MINI_EVENT_CHANCE)))
        if mini is not None:
            amount = max(0, int(amount * float(mini.get("mult", 1.0))))
        # 商路加成（§5.5）：trade 任务节点位于活跃商路上 → +30%
        if task["task_type"] == "trade" and node:
            trcfg = self.main_cfg().get("trade_route") or {}
            if int(node["id"]) in await self._trade_route_node_ids(world):
                amount = int(amount * (1 + float(trcfg.get("trade_task_bonus", 0.3))))
        # 劫掠（§6.2）：掠夺库存 + 民心↓ + 声望↓ + 反杀
        if task["task_type"] == "raid":
            out = await self._pay_task_raid(world, p, task, c, rng)
            out["event"] = mini
            await self._flush_task_wallet(p, task)
            return out
        out = {"kind": R.TASK_OUTPUT_RES.get(task["task_type"], "coin")}
        if mini is not None:
            out["event"] = mini
        kind = out["kind"]
        ach_calls: list[tuple[str, int]] = []
        if kind == "coin":
            p["coin"] += amount
        elif kind == "grain":
            p["grain"] += amount
            if task["task_type"] == "farm":
                ach_calls.append(("civil_farm_100k", amount))
        elif kind == "iron":
            p["iron"] = int(p.get("iron", 0)) + amount
            out["amount"] = amount
            if task["task_type"] == "mining":
                ach_calls.append(("civil_mine_100k", amount))
        elif kind == "token":
            tokens = max(0, int(amount // 300))  # 寻访：300 收益 = 1 募将令
            p["recruit_token"] += tokens
            out["amount"] = tokens
        elif kind == "merit":
            merit = max(0, int(amount // 10))
            # 魔潮期讨伐战功 ×2（hunt_merit_mult，§11.1）
            if task["task_type"] == "hunt":
                merit = int(merit * await self._hunt_merit_mult(world))
            merit = await self._settle_merit(p["user_id"], merit, "task")
            p["merit"] += merit
            p["merit_season"] += merit
            out["amount"] = merit
        elif kind == "heal":
            healed = await self._heal_chars_at_node(p, task["node_id"], amount)
            out["amount"] = healed
        elif kind == "build":
            if node:
                node["defense"] = min(R.city_defense_max(node["level"]),
                                      node["defense"] + amount * 2)
                await self.save_node(node)
            out["amount"] = amount * 2
            ach_calls.append(("civil_build_5", 1))
        elif kind == "train":
            drills = await self._drill_armies_at_node(p, task["node_id"], amount)
            out["amount"] = drills
        elif kind == "tech":
            # 研究任务产出势力科技点（结盟后记在盟主势力名下）
            f = await self._governance_faction(p)
            if f:
                tech = f.get("tech") or {}
                tech["points"] = int(tech.get("points", 0)) + amount
                f["tech"] = tech
                await self.save_faction(f)
            out["amount"] = amount
        elif kind == "intel":
            # 侦察任务：揭示任务据点及相邻据点的守军/军团情报
            revealed = await self._apply_scout_intel(world, task, ticks)
            out["amount"] = revealed
            # 侦察顺带俘获流将（task.scout_capture_chance）
            captured = await self._scout_maybe_capture(world, p, task, c, rng)
            if captured:
                out["captured"] = captured
        elif task["task_type"] == "supply":
            # 运粮：粮草已在派遣时扣除并注入军团
            out["amount"] = int(payout_meta.get("supplied", 0) or 0)
        else:  # 其他（loot 等）折半转币
            p["coin"] += amount // 2
            out["amount"] = amount // 2
        if kind in ("coin", "grain"):
            out["amount"] = amount
        # 布政/行医持续提升据点民心（§6.2 / governance.*_morale_per_tick）
        morale_gain = R.morale_task_gain(task["task_type"], ticks, self.main_cfg())
        if morale_gain and node:
            await self._bump_morale(node, morale_gain, reason=task["task_type"])
            await self.save_node(node)
            out["morale_gain"] = morale_gain
            out["morale"] = int(node.get("morale", 0))
        # 角色阅历：任务执行时间越长成长越多（可突破稀有度生成上限）
        exp_per_tick = int(tcfg.get("exp_per_tick", 5))
        logs = R.gain_exp(c, ticks * exp_per_tick,
                          self._growth_cap_bonus())
        c["location"] = {"node_id": task["node_id"]}
        await self.save_char(c)
        await self._flush_task_wallet(p, task)
        # 成就进度（须在 save_player 之后，避免覆盖 merit 写入）
        for akey, acount in ach_calls:
            await self._award_achievement(p["user_id"], akey, count=acount)
        out["exp_logs"] = logs
        return out

    async def _pay_task_raid(self, world: dict, p: dict, task: dict,
                             c: dict, rng: random.Random) -> dict:
        """劫掠结算（§6.2）：掠夺库存 → 民心 -8 → 声望降 → 反杀判定。"""
        cfg = self.main_cfg()
        tcfg = cfg.get("task") or {}
        node = await self.get_node(world["id"], int(task["node_id"]))
        out: dict = {"kind": "loot", "amount": 0}
        if not node:
            return out
        pct = float((cfg.get("siege") or {}).get("loot_stock_pct", 0.3))
        stock = node.get("stock") or {}
        gained = 0
        for key in ("coin", "grain", "iron"):
            take = int(int(stock.get(key, 0) or 0) * pct)
            if take <= 0:
                continue
            stock[key] = int(stock.get(key, 0) or 0) - take
            p[key] = int(p.get(key, 0) or 0) + take
            gained += take
        node["stock"] = stock
        await self._bump_morale(node, -8, reason="raid")
        await self.save_node(node)
        out["amount"] = gained
        out["morale"] = int(node.get("morale", 0))
        # 声望惩罚
        rep_cost = int(tcfg.get("raid_rep_cost", -8))
        if rep_cost:
            f = await self.get_faction(int(task.get("faction_id") or 0))
            if f:
                await self.add_reputation(f, rep_cost)
                out["reputation"] = rep_cost
        # 反杀：执行者受伤或被俘（task.raid_counterkill_chance）
        if rng.random() < float(tcfg.get("raid_counterkill_chance", 0.15)):
            if rng.random() < 0.4:
                # 统一走关押入口：校验营位、移出军团名册；位满则就地驱逐
                captor = int(node.get("owner_faction") or 0)
                got = await self._capture_char(c, captor, int(node["id"]))
                out["counterkill"] = "captured" if got else "evicted"
            else:
                dead = R.apply_injury(c, int((cfg.get("injury") or {}).get(
                    "recover_per_tick", 5)) * 4 + 10)
                if dead:
                    c["status"] = "dead"
                out["counterkill"] = "injured"
            await self.save_char(c)
        return out

    async def _scout_maybe_capture(self, world: dict, p: dict, task: dict,
                                   c: dict, rng: random.Random) -> str:
        """侦察成功按 scout_capture_chance 俘获目标据点流将（§6.2）。

        选择：转为**我方俘虏**（status='captive'，captor_faction=我方），
        保留后续劝降玩法，不直接招募。
        """
        tcfg = self.main_cfg().get("task") or {}
        chance = float(tcfg.get("scout_capture_chance", 0.08) or 0)
        if chance <= 0 or rng.random() >= chance:
            return ""
        node = await self.get_node(world["id"], int(task["node_id"]))
        if not node:
            return ""
        owner = int(node.get("owner_faction") or 0)
        if owner <= 0 or owner == int(task.get("faction_id") or 0):
            return ""
        rows = await self._fetchall(
            "SELECT * FROM strategy_characters WHERE world_id=? AND faction_id=? "
            "AND status='idle' ORDER BY RANDOM() LIMIT 1",
            (int(world["id"]), owner))
        if not rows:
            return ""
        victim = self._decode("strategy_characters", rows[0])
        if not await self._capture_char(
                victim, int(task.get("faction_id") or 0), int(node["id"])):
            return ""
        await self._notify(world, [p.get("user_id")], "event",
                           {"text": f"🕵 {c['name']} 侦察时擒获「{victim['name']}」"})
        return str(victim.get("name") or "")

    async def _apply_scout_intel(self, world: dict, task: dict, ticks: int) -> int:
        """侦察结算：点亮任务据点周边 2 跳范围的守军/驻军情报。

        默认战争迷雾只显示己方及 1 步邻接；侦察任务额外把 2 跳内的节点
        点亮，能提前看到更外侧的敌军与守军。有效期 = 任务时长
        （4 tick = 1h），至少 1 小时。
        """
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        origin = int(task["node_id"])
        if origin not in nodes:
            return 0
        ids = {origin}
        frontier = {origin}
        for _ in range(2):
            nxt: set[int] = set()
            for nid in frontier:
                for adj in nodes.get(nid, {}).get("adj") or []:
                    if int(adj) in nodes and int(adj) not in ids:
                        nxt.add(int(adj))
            ids.update(nxt)
            frontier = nxt
        data = world.get("data") or {}
        scouted = data.setdefault("scouted", {})
        fid = str(int(task.get("faction_id") or 0))
        bucket = scouted.setdefault(fid, {})
        now = _now()
        until = now + max(1, int(ticks) // 4) * 3600
        for nid in ids:
            bucket[str(nid)] = max(int(bucket.get(str(nid), 0) or 0), until)
        # 顺手清理过期项，避免 world.data 无限增长
        scouted[fid] = {k: int(v) for k, v in bucket.items() if int(v) > now}
        world["data"] = data
        await self.save_world(world)
        return len(ids)
    async def _task_event_mult(self, world: dict, task: dict, node: Optional[dict]) -> float:
        mult = 1.0
        res = R.TASK_OUTPUT_RES.get(str(task.get("task_type")), "")
        node_id = int((node or {}).get("id") or 0)
        for ev in await self.get_active_events(world["id"], world.get("tick_no", 0)):
            for eff in (ev.get("data") or {}).get("effects") or []:
                etype = eff.get("type")
                if etype == "global_output":
                    mult *= float(eff.get("mult", 1.0))
                elif etype == "node_output" and node_id and res and \
                        str(eff.get("resource", "")) == res and \
                        node_id in ((ev.get("target") or {}).get("node_ids") or []):
                    # 据点级资源事件同样作用于任务：矿脉枯竭削弱矿点采矿、
                    # 商队来访提升坊市贸易等，与被动产出保持同一套修正。
                    mult *= float(eff.get("mult", 1.0))
        return mult

    async def _hunt_merit_mult(self, world: dict) -> float:
        """魔潮等事件期间的讨伐（hunt）战功倍率（hunt_merit_mult）。"""
        mult = 1.0
        for ev in await self.get_active_events(world["id"], world.get("tick_no", 0)):
            for eff in (ev.get("data") or {}).get("effects") or []:
                if eff.get("type") == "hunt_merit_mult":
                    mult *= float(eff.get("mult", 1.0))
        return mult

    async def _heal_chars_at_node(self, p: dict, node_id: int, amount: int) -> int:
        healed = 0
        for c in await self.get_chars(owner_user_id=p["user_id"], world_id=p["world_id"]):
            if c["injury"] > 0 and (
                    c["status"] == "idle" or
                    (c.get("location") or {}).get("node_id") == node_id):
                c["injury"] = max(0, c["injury"] - max(1, amount // 40))
                await self.save_char(c)
                healed += 1
        return healed

    async def _drill_armies_at_node(self, p: dict, node_id: int, amount: int) -> int:
        n = 0
        for a in await self.get_armies(p["world_id"], owner_user_id=p["user_id"],
                                       node_id=node_id, states=("idle",)):
            a["veteran"] = min(R.VETERAN_MAX, a["veteran"] + max(1, amount // 200))
            await self.save_army(a)
            n += 1
        return n

    # ------------------------------------------------------------------
    # 军团（§5.5 编成 / §5.6 行军）
    # ------------------------------------------------------------------
    async def create_army(self, user_id: str, leader_char_id: int, member_ids: list,
                          node_id: int, troop_type: str, troops: int,
                          siege_engine: bool = False) -> dict:
        world, p = await self._require_player(user_id)
        acfg = self.main_cfg().get("army") or {}
        if troop_type not in R.TROOP_TYPES:
            raise StrategyError("兵种无效")
        troops = int(troops)
        if troops < int(acfg.get("min_troops", 100)):
            raise StrategyError(f"兵力至少 {acfg.get('min_troops', 100)}")
        node = await self._owned_node(p, node_id)
        faction = await self.get_faction(p["faction_id"])
        # 并发军团上限（§5.5：基础 2 支 + 势力等级/2，封顶 5；势力配置优先）
        level = max(1, int((faction or {}).get("level", 1) or 1))
        fcfg = self.main_cfg().get("faction") or {}
        per_levels = max(1, int(
            fcfg.get("concurrent_army_per_levels")
            or acfg.get("concurrent_per_faction_levels", 2)))
        concurrent = int(acfg.get("concurrent_base", 2)) + level // per_levels
        concurrent = min(5, max(1, concurrent))
        mine = await self.get_armies(world["id"], owner_user_id=p["user_id"])
        if len(mine) >= concurrent:
            raise StrategyError(f"军团数已达上限（{concurrent} 支）")
        leader = await self._own_char(p, leader_char_id)
        if leader["status"] != "idle":
            raise StrategyError(f"{leader['name']} 无法出征（{leader['status']}）")
        if leader["role"] == R.ROLE_OFFICIAL:
            raise StrategyError("内政官不能担任主将")
        if not R.can_act(leader):
            raise StrategyError(f"{leader['name']} 伤势过重，无法出征")
        members = [leader]
        seen = {int(leader["id"])}
        for mid in member_ids or []:
            # 低危：member_ids 去重（重复 id 会让同一角色两次入军/双重羁绊掷点）
            if int(mid) in seen:
                continue
            seen.add(int(mid))
            mc = await self._own_char(p, mid)
            if mc["status"] != "idle":
                raise StrategyError(f"{mc['name']} 无法出征（{mc['status']}）")
            if not R.can_act(mc):
                raise StrategyError(f"{mc['name']} 伤势过重，无法随军")
            members.append(mc)
        cap = await self._leader_cap(leader, faction, int(node["id"]))
        if troops > cap:
            raise StrategyError(f"超过主将带兵上限（{cap}）")
        # 招募成本（§7.5 兵种价目）；征兵动员 troop_cost_mult 折扣
        tt = R.TROOP_TYPES[troop_type]
        troop_mult = await self._faction_buff(
            int(p["faction_id"]), "troop_cost_mult")
        coin_cost = int(troops * tt["recruit_coin"] / 10 * troop_mult)
        grain_cost = int(troops * tt["recruit_grain"] / 10 * troop_mult)
        # 农政科技 L2：军团粮耗 -10%（与动员令乘区叠乘）
        grain_cost = int(grain_cost * float(R.tech_effects(
            (faction or {}).get("tech") or {}).get("grain_cost_mult", 1.0)))
        if siege_engine:
            # 兵学 L2：破城槌造价 -30%（R.tech_effects.ram_cost_mult）
            ram_mult = float(R.tech_effects(
                (faction or {}).get("tech") or {}).get("ram_cost_mult", 1.0))
            coin_cost += int(3000 * ram_mult)
        if p["coin"] < coin_cost or p["grain"] < grain_cost:
            raise StrategyError(f"资源不足：需铜钱 {coin_cost} / 粮草 {grain_cost}")
        p["coin"] -= coin_cost
        p["grain"] -= grain_cost
        # 携粮 = min(库存, 统率×50)；粮仓 +10%/级（§5.6 / §6.6）
        carry_cap = R.carry_grain_cap(leader)
        granary = min(5, self._building_level(node, "granary"))
        if granary:
            carry_cap = int(carry_cap * (1 + 0.1 * granary))
        carry = min(p["grain"], carry_cap)
        p["grain"] -= carry
        for mc in members:
            mc["status"] = "army"
            mc["location"] = {"node_id": node["id"]}
            await self.save_char(mc)
        army = {
            "world_id": world["id"], "faction_id": p["faction_id"],
            "owner_user_id": p["user_id"],
            "name": f"{leader['name']}军", "node_id": node["id"],
            "characters": [mc["id"] for mc in members],
            "troop_type": troop_type, "veteran": 0, "troops": troops,
            "grain": carry, "morale": 100, "state": "idle",
            "siege_engine": 1 if siege_engine else 0,
        }
        # 同道羁绊掷点（§4.5）
        rng = self._rng("bond", army["name"], _now() // 3600)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                R.maybe_form_tongdao(members[i], members[j], rng)
                await self.save_char(members[i])
                await self.save_char(members[j])
        await self.insert_army(army)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": f"编成「{army['name']}」（{tt['name']}×{troops}，携粮 {carry}）",
                "army_id": army["id"]}

    async def disband_army(self, user_id: str, army_id: int) -> dict:
        world, p = await self._require_player(user_id)
        a = await self._own_army(p, army_id)
        if a["state"] != "idle":
            raise StrategyError("军团行动期间无法解散（请先撤退）")
        node = await self.get_node(world["id"], a["node_id"])
        if not node or node["owner_faction"] != p["faction_id"]:
            raise StrategyError("只能在己方据点解散")
        p["grain"] += int(a.get("grain", 0))
        for cid in a.get("characters") or []:
            c = await self.get_char(cid)
            if c and c.get("status") not in ("dead", "captive"):
                # A-M9：已陨落/被俘角色不得重置为 idle（保持死亡与囚禁状态）
                c["status"] = "idle"
                await self.save_char(c)
        await self.delete_army(a["id"])
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": f"「{a['name']}」已解散，剩余粮草 {a.get('grain', 0)} 归还"}

    async def refill_army(self, user_id: str, army_id: int, troops: int) -> dict:
        """补员（§5.5：仅己方据点）。"""
        world, p = await self._require_player(user_id)
        a = await self._own_army(p, army_id)
        if self._is_reorganizing(a, world):
            raise StrategyError(self._reorg_msg(a, world))
        if a["state"] != "idle":
            raise StrategyError("军团行动中，无法补员")
        node = await self.get_node(world["id"], a["node_id"])
        if not node or node["owner_faction"] != p["faction_id"]:
            raise StrategyError("补员仅限己方据点")
        troops = int(troops)
        if troops <= 0:
            raise StrategyError("补员数量须为正")
        leader = await self._army_leader(a)
        if not leader:
            raise StrategyError("军团已无可用主将，请解散后重新编军")
        faction = await self.get_faction(a["faction_id"])
        cap = await self._leader_cap(leader, faction, int(a["node_id"]))
        if a["troops"] + troops > cap:
            raise StrategyError(f"超过带兵上限（{cap}）")
        tt = R.TROOP_TYPES.get(a["troop_type"], R.TROOP_TYPES["sword_shield"])
        troop_mult = await self._faction_buff(
            int(p["faction_id"]), "troop_cost_mult")
        coin_cost = int(troops * tt["recruit_coin"] / 10 * troop_mult)
        grain_cost = int(troops * tt["recruit_grain"] / 10 * troop_mult)
        grain_cost = int(grain_cost * float(R.tech_effects(
            (await self.get_faction(int(p["faction_id"])) or {}
             ).get("tech") or {}).get("grain_cost_mult", 1.0)))
        if p["coin"] < coin_cost or p["grain"] < grain_cost:
            raise StrategyError(f"资源不足：需铜钱 {coin_cost} / 粮草 {grain_cost}")
        p["coin"] -= coin_cost
        p["grain"] -= grain_cost
        a["troops"] += troops
        a["morale"] = min(100, a["morale"] + 5)
        await self.save_army(a)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": f"补员 {troops} 兵（现 {a['troops']}）"}

    async def drill_army(self, user_id: str, army_id: int) -> dict:
        """操练：铜钱 500 → 精锐值 +5（上限 R.VETERAN_MAX）。"""
        world, p = await self._require_player(user_id)
        a = await self._own_army(p, army_id)
        if a["state"] != "idle":
            raise StrategyError("军团行动中，无法操练")
        if int(a.get("troops", 0)) <= 0:
            raise StrategyError("军团已无兵力，请解散后重新编军")
        if not await self._army_leader(a):
            raise StrategyError("军团已无可用主将，请解散后重新编军")
        if a["veteran"] >= R.VETERAN_MAX:
            raise StrategyError("精锐值已满")
        if p["coin"] < 500:
            raise StrategyError("铜钱不足：操练需 500")
        p["coin"] -= 500
        a["veteran"] = min(R.VETERAN_MAX, a["veteran"] + 5)
        await self.save_army(a)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": f"操练完成，精锐值 {a['veteran']}"}

    async def march(self, user_id: str, army_id: int, target_node_id: int) -> dict:
        world, p = await self._require_player(user_id)
        a = await self._own_army(p, army_id)
        if self._is_reorganizing(a, world):
            raise StrategyError(self._reorg_msg(a, world))
        if a["state"] == "escort":
            raise StrategyError("护送运粮队期间无法出征（抵达或被截后自动归队）")
        if a["state"] not in ("idle",):
            raise StrategyError(f"军团状态「{a['state']}」无法出征")
        if int(a.get("troops", 0)) <= 0:
            raise StrategyError("军团已无兵力，请解散后重新编军")
        if not await self._army_leader(a):
            raise StrategyError("军团已无可用主将，请解散后重新编军")
        target = await self.get_node(world["id"], int(target_node_id))
        if not target:
            raise StrategyError("目标据点不存在")
        path = await self.find_path(world["id"], a["node_id"], target["id"])
        if not path:
            raise StrategyError("目标不连通（须沿通路行军）")
        if len(path) < 2:
            raise StrategyError("军团已在目标据点")
        now = _now()
        my_f = await self.get_faction(int(p["faction_id"]))
        my_host = await self._alliance_host(my_f) if my_f else None
        target_f = await self.get_faction(int(target["owner_faction"])) \
            if int(target["owner_faction"]) > 0 else None
        target_host = await self._alliance_host(target_f) if target_f else None
        if int(target["owner_faction"]) != int(p["faction_id"]) and my_host and \
                target_host and int(my_host["id"]) == int(target_host["id"]):
            raise StrategyError("目标属于同盟势力，不可进攻")
        if target["owner_faction"] not in (0, p["faction_id"]):
            if target.get("protect_until", 0) > now:
                raise StrategyError("目标处于保护期")
            # C-M3：同盟/停战/开战备战期禁止进攻（§6.3 宣战门禁）
            rel = R.relation_of((my_host or {}).get("diplomacy"),
                                int((target_host or {}).get("id") or
                                    target["owner_faction"]), now)
            if rel in (R.REL_ALLY, R.REL_TRUCE, "war_pending"):
                rel_name = {R.REL_ALLY: "同盟", R.REL_TRUCE: "停战",
                            "war_pending": "开战备战期"}.get(rel, rel)
                raise StrategyError(
                    f"与目标势力处于「{rel_name}」，不可进攻（须先解除关系）")
            tf = target_f
            if tf and tf.get("kind") == "player":
                # A4：created_at 此前未在 SELECT 列中，新手保护判定永远跳过
                np = await self._fetchone(
                    "SELECT newcomer_json, created_at FROM strategy_players "
                    "WHERE world_id=? AND faction_id=?",
                    (world["id"], target["owner_faction"]))
                newcom = _loads((np or {}).get("newcomer_json"), {})
                days = int((self.main_cfg().get("protection") or {}).get("newbie_days", 7))
                joined = int((np or {}).get("created_at", 0) or 0)
                if joined and now - joined < days * 86400 and not newcom.get("opt_out"):
                    raise StrategyError(f"目标玩家处于新手保护期（{days}天）")
        a["state"] = "march"
        a["path"] = path[1:]
        a["edge_pos"] = 0.0
        a["arrive_tick"] = world.get("tick_no", 0) + len(path)
        await self.save_army(a)
        await self._commit()
        hostile = target["owner_faction"] not in (0, p["faction_id"])
        return {"ok": True,
                "msg": f"「{a['name']}」出发 →「{target['name']}」"
                       f"（{len(path) - 1} 段路程，{'敌对' if hostile else '和平'}目标）"}

    async def retreat_army(self, user_id: str, army_id: int) -> dict:
        """撤退（A-M5 重写）：撤往最近己方据点；无路可退则就地解散。

        修复：不再产生无人消费的 "retreat" 死状态；已在己方据点驻守时
        直接返回；围城军撤走后清理孤儿 siege_state（A-M11）。
        """
        world, p = await self._require_player(user_id)
        a = await self._own_army(p, army_id)
        if a["state"] == "escort":
            raise StrategyError("护送运粮队期间无法撤退（抵达或被截后自动归队）")
        if a["state"] not in ("march", "siege", "idle"):
            raise StrategyError("当前状态无法撤退")
        was_siege = a["state"] == "siege"
        home = await self._nearest_own_node(world, p, a["node_id"])
        if home is None:
            # 无任何己方据点：与战败溃散同口径（_auto_retreat 就地解散归还角色）
            await self._auto_retreat(a)
            if was_siege:
                await self._clear_orphan_siege(
                    world, await self.get_node(world["id"], a["node_id"]))
            await self._commit()
            return {"ok": True, "msg": "无己方据点可退，军团已就地解散（角色归还）"}
        if home == a["node_id"] and a["state"] == "idle":
            return {"ok": True, "msg": "军团已驻守己方据点，无需撤退"}
        if home == a["node_id"]:
            a["state"] = "idle"
            a["path"] = []
            a["edge_pos"] = 0.0
            await self.save_army(a)
            msg = f"「{a['name']}」已就地转入驻守"
        else:
            path = await self.find_path(world["id"], a["node_id"], home)
            if not path:
                raise StrategyError("撤退路线不通（无己方据点可达）")
            a["state"] = "march"
            a["path"] = path[1:] if len(path) > 1 else []
            a["edge_pos"] = 0.0
            if not a["path"]:
                a["state"] = "idle"
            await self.save_army(a)
            msg = f"「{a['name']}」撤退中（{len(a['path'])} 段）"
        if was_siege:
            await self._clear_orphan_siege(
                world, await self.get_node(world["id"], a["node_id"]))
        await self._commit()
        return {"ok": True, "msg": msg}

    async def _clear_orphan_siege(self, world: dict, node: Optional[dict]):
        """A-M11：节点已无围城军团时清除悬挂的 siege_state。

        （否则 siege_state.ticks 悬挂：城防自修被阻、面板长期显示被围。）
        """
        if not node or not (node.get("siege_state") or {}):
            return
        left = await self.get_armies(world["id"], node_id=node["id"],
                                     states=("siege",))
        if not left:
            node["siege_state"] = {}
            await self.save_node(node)

    async def _nearest_own_node(self, world: dict, p: dict, node_id: int) -> Optional[int]:
        mine = await self._fetchall(
            "SELECT id FROM strategy_nodes WHERE world_id=? AND owner_faction=?",
            (world["id"], p["faction_id"]))
        if not mine:
            return None
        ids = [int(r["id"]) for r in mine]
        best, best_len = ids[0], 10 ** 9
        for nid in ids:
            path = await self.find_path(world["id"], node_id, nid)
            if path and len(path) < best_len:
                best, best_len = nid, len(path)
        return best

    async def set_siege_choice(self, user_id: str, army_id: int, choice: str) -> dict:
        """围城偏好：street_fight（巷战）/ surrender（受降，需 3 倍兵力威压）。"""
        if choice not in ("street_fight", "surrender"):
            raise StrategyError("choice 须为 street_fight 或 surrender")
        world, p = await self._require_player(user_id)
        a = await self._own_army(p, army_id)
        if a["state"] != "siege":
            raise StrategyError("军团未处于围城状态")
        node = await self.get_node(world["id"], a["node_id"])
        if not node:
            raise StrategyError("围城据点不存在")
        ss = node.get("siege_state") or {}
        if int(ss.get("attacker_faction") or 0) != int(p["faction_id"]):
            raise StrategyError("当前围城归属不是你方，无法设置围城策略")
        ss["choice"] = choice
        node["siege_state"] = ss
        await self.save_node(node)
        await self._commit()
        return {"ok": True, "msg": f"围城策略已设为「{'巷战' if choice == 'street_fight' else '受降'}」"}

    # ------------------------------------------------------------------
    # 民心直接提升：赈济（§8.6 势力治理 / 用户明确要求）
    # ------------------------------------------------------------------
    def _relief_log(self, p: dict) -> dict:
        """当日赈济计数（strategy_players.daily_output_log_json.relief）。"""
        today = time.strftime("%Y%m%d")
        log = p.get("daily_output_log") or {}
        relief = log.get("relief") or {}
        if not isinstance(relief, dict) or relief.get("date") != today:
            relief = {"date": today, "total": 0, "nodes": {}}
        if not isinstance(relief.get("nodes"), dict):
            relief["nodes"] = {}
        return relief

    def _relief_used(self, p: Optional[dict], node_id: int) -> int:
        if not p:
            return 0
        relief = self._relief_log(p)
        return int(relief["nodes"].get(str(int(node_id)), 0) or 0)

    async def govern_relief(self, user_id: str, node_id: int,
                            resource: str = "coin") -> dict:
        """赈济：花铜钱/粮草直接提升据点民心（每据点/每人每日限次）。"""
        world, p = await self._require_player(user_id)
        gcfg = self.main_cfg().get("governance") or {}
        node = await self._owned_node(p, node_id)
        resource = str(resource or "coin")
        if resource not in ("coin", "grain"):
            raise StrategyError("resource 须为 coin 或 grain")
        if int(node.get("morale", 0)) >= self._morale_cap(node):
            raise StrategyError(f"「{node['name']}」民心已满，无需赈济")
        relief = self._relief_log(p)
        per_player = max(1, int(gcfg.get("relief_daily_limit", 3)))
        per_node = max(1, int(gcfg.get("relief_node_daily_limit", 2)))
        if int(relief.get("total", 0)) >= per_player:
            raise StrategyError(f"今日赈济次数已用完（{per_player} 次/日）")
        used = int(relief["nodes"].get(str(int(node["id"])), 0) or 0)
        if used >= per_node:
            raise StrategyError(
                f"「{node['name']}」今日赈济已达上限（{per_node} 次/日）")
        if resource == "grain":
            cost = max(1, int(gcfg.get("relief_grain_cost", 800)))
            if int(p.get("grain", 0)) < cost:
                raise StrategyError(f"粮草不足：赈济需 {cost}")
            p["grain"] = int(p.get("grain", 0)) - cost
        else:
            cost = max(1, int(gcfg.get("relief_coin_cost", 3000)))
            if int(p.get("coin", 0)) < cost:
                raise StrategyError(f"铜钱不足：赈济需 {cost}")
            p["coin"] = int(p.get("coin", 0)) - cost
        gain = max(1, int(gcfg.get("relief_morale", 6)))
        morale = await self._bump_morale(node, gain, reason="relief")
        await self.save_node(node)
        relief["total"] = int(relief.get("total", 0)) + 1
        relief["nodes"][str(int(node["id"]))] = used + 1
        log = p.get("daily_output_log") or {}
        log["relief"] = relief
        p["daily_output_log"] = log
        await self.save_player(p)
        await self._commit()
        res_name = "粮草" if resource == "grain" else "铜钱"
        return {"ok": True, "morale": morale,
                "used": int(relief["total"]),
                "node_used": used + 1,
                "msg": f"「{node['name']}」赈济 {res_name} {cost}，民心 +{gain} → {morale}"
                       f"（今日 {relief['total']}/{per_player} 次）"}

    # ------------------------------------------------------------------
    # 建设 / 升级（§5.3 / §6.6）
    # ------------------------------------------------------------------
    #: 九种建筑，等级 1~5（``max`` 即等级上限）；效果按「级」线性叠加。
    BUILDINGS = {
        "manor": {"name": "城主府", "coin": 3000, "iron": 300, "max": 5,
                  "desc": "据点产出 +6%/级（主城另有主城加成）"},
        "barracks": {"name": "兵营", "coin": 2500, "iron": 250, "max": 5,
                     "desc": "本据点出征主将带兵上限 +5%/级"},
        "market_stall": {"name": "坊市摊", "coin": 1800, "iron": 100, "max": 5,
                         "desc": "本据点互市成交税 -1%/级"},
        "tavern_branch": {"name": "酒馆分号", "coin": 2200, "iron": 120, "max": 5,
                          "desc": "本地酒馆招募稀有概率 +3%/级"},
        "guard_array": {"name": "护山阵眼", "coin": 4000, "iron": 400, "max": 5,
                        "desc": "护山盾免战时长 +10%/级"},
        "arena": {"name": "擂将台", "coin": 3200, "iron": 300, "max": 5,
                  "desc": "擂台彩头 +10%/级，据点民心 +1/级"},
        "tower": {"name": "箭塔", "coin": 2000, "iron": 200, "max": 5,
                  "desc": "城防反击伤害 +20/级"},
        "infirmary": {"name": "医馆", "coin": 1500, "iron": 0, "max": 5,
                      "desc": "本据点角色伤势恢复 +5/级（1 级即 +10）"},
        "granary": {"name": "粮仓", "coin": 1200, "iron": 100, "max": 5,
                    "desc": "本据点出征军团携粮上限 +10%/级"},
    }

    @classmethod
    def _node_level_cap(cls) -> int:
        """据点等级上限：取建筑最高等级（§5.3：据点 1~5 级）。"""
        return max(int(spec.get("max", 1)) for spec in cls.BUILDINGS.values())

    @classmethod
    def _building_level(cls, node: Optional[dict], building_id: str) -> int:
        """建筑等级：兼容旧存档（同一 id 多条 = 多级）与新格式（level 字段）。"""
        if not node:
            return 0
        total = 0
        for b in node.get("buildings") or []:
            if isinstance(b, dict):
                if str(b.get("id")) == str(building_id):
                    total += max(1, int(b.get("level", 1) or 1))
            elif str(b) == str(building_id):
                total += 1
        return total

    def _buildings_level_map(self, node: Optional[dict]) -> dict[str, int]:
        out: dict[str, int] = {}
        for bid in self.BUILDINGS:
            lvl = self._building_level(node, bid)
            if lvl > 0:
                out[bid] = min(int(self.BUILDINGS[bid]["max"]), lvl)
        return out

    def _node_output_mult(self, node: dict, faction: Optional[dict] = None) -> float:
        """城主府产出加成 +（主城）capital_output_bonus（§6.6 / §8.7）。"""
        gcfg = self.main_cfg().get("governance") or {}
        mult = 1.0 + 0.06 * min(5, self._building_level(node, "manor"))
        if faction and int(faction.get("capital_node_id") or 0) == int(node["id"]):
            mult *= 1.0 + float(gcfg.get("capital_output_bonus", 0.1) or 0.0)
        return mult

    async def build(self, user_id: str, node_id: int, building_id: str) -> dict:
        world, p = await self._require_player(user_id)
        spec = self.BUILDINGS.get(building_id)
        if not spec:
            raise StrategyError("建筑不存在")
        node = await self._owned_node(p, node_id)
        if node["type"] not in ("city", "pass", "branch"):
            raise StrategyError("仅城池/关隘/分舵可建设")
        if self._building_level(node, building_id) > 0:
            raise StrategyError(f"{spec['name']}已建造，请使用「升级建筑」提升等级")
        if p["coin"] < spec["coin"] or p["iron"] < spec["iron"]:
            raise StrategyError(f"资源不足：需铜钱 {spec['coin']} / 精铁 {spec['iron']}")
        p["coin"] -= spec["coin"]
        p["iron"] -= spec["iron"]
        buildings = list(node.get("buildings") or [])
        buildings.append({"id": building_id, "level": 1, "built_at": _now(),
                          "by": p["user_id"]})
        node["buildings"] = buildings
        await self.save_node(node)
        await self.save_player(p)
        # §6.4 势力任务：兴业安邦
        await self._faction_task_progress(int(p["faction_id"]), "develop", 1)
        await self._commit()
        return {"ok": True, "level": 1,
                "msg": f"「{node['name']}」建成{spec['name']}（1 级：{spec['desc']}）"}

    async def upgrade_building(self, user_id: str, node_id: int,
                               building_id: str) -> dict:
        """建筑升级至 1~5 级；效果随等级线性增强（§6.6）。"""
        world, p = await self._require_player(user_id)
        spec = self.BUILDINGS.get(building_id)
        if not spec:
            raise StrategyError("建筑不存在")
        node = await self._owned_node(p, node_id)
        buildings = list(node.get("buildings") or [])
        entry = None
        for i, b in enumerate(buildings):
            bid = b.get("id") if isinstance(b, dict) else b
            if str(bid) != str(building_id):
                continue
            # 兼容旧存档 ["tower"]：就地升级为 {"id","level"} 结构
            if not isinstance(b, dict):
                b = {"id": str(bid), "level": 1}
                buildings[i] = b
            entry = b
            break
        if entry is None:
            raise StrategyError(f"尚未建造{spec['name']}")
        cur = max(1, int(entry.get("level", 1) or 1))
        cap = int(spec.get("max", 5))
        if cur >= cap:
            raise StrategyError(f"{spec['name']}已达等级上限（{cap} 级）")
        coin_cost = int(int(spec["coin"]) * 0.6 * cur)
        iron_cost = int(int(spec["iron"]) * 0.6 * cur)
        if p["coin"] < coin_cost or p["iron"] < iron_cost:
            raise StrategyError(
                f"资源不足：升级需铜钱 {coin_cost} / 精铁 {iron_cost}")
        p["coin"] -= coin_cost
        p["iron"] -= iron_cost
        entry["level"] = cur + 1
        node["buildings"] = buildings
        await self.save_node(node)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "level": cur + 1,
                "msg": f"「{node['name']}」{spec['name']}升至 {cur + 1} 级"}

    async def upgrade_node(self, user_id: str, node_id: int) -> dict:
        """据点升级（§5.3：1~5 级，上限读建筑最高等级）。"""
        world, p = await self._require_player(user_id)
        node = await self._owned_node(p, node_id)
        if node["type"] not in ("city", "pass"):
            raise StrategyError("仅城池/关隘可升级")
        cap = self._node_level_cap()
        if node["level"] >= cap:
            raise StrategyError(f"已达据点等级上限（{cap} 级）")
        nxt = node["level"] + 1
        coin_cost, iron_cost = 5000 * node["level"], 500 * node["level"]
        if p["coin"] < coin_cost or p["iron"] < iron_cost:
            raise StrategyError(f"资源不足：升级需铜钱 {coin_cost} / 精铁 {iron_cost}")
        p["coin"] -= coin_cost
        p["iron"] -= iron_cost
        node["level"] = nxt
        if node["defense"] > 0:
            node["defense"] = R.city_defense_max(nxt)
        await self.save_node(node)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": f"「{node['name']}」升至 {nxt} 级"}

    # --- 建筑效果接线（§6.6）：坊市摊 / 护山阵眼 / 擂将台 -----------------
    async def market_buy(self, user_id: str, order_id: int,
                         amount: int = 0) -> dict:
        """坊市摊：成交税 -1%/级（买入方返还等额铜钱，税池同步减少）。

        返税必须按「实际成交成本」计算：partial 买入、商队折扣、并发改单
        都会让成交额小于预读的 row×amount。这里只预读建筑等级，金额以
        `_market_buy` 返回的 cost 为准，避免超额返税。
        """
        world, p = await self._require_player(user_id)
        row = await self._fetchone(
            "SELECT node_id, status FROM strategy_market_orders "
            "WHERE id=? AND world_id=?",
            (int(order_id), world["id"]))
        lvl = 0
        if row and row.get("status") == "open":
            node = await self.get_node(world["id"], int(row["node_id"]))
            lvl = min(5, self._building_level(node, "market_stall"))
        res = await super().market_buy(user_id, order_id, amount)
        cost = int((res or {}).get("cost", 0) or 0)
        refund = int(round(cost * 0.01 * lvl)) if lvl > 0 and cost > 0 else 0
        if refund > 0:
            p2 = await self.get_player(user_id, world["id"])
            if p2:
                p2["coin"] = int(p2.get("coin", 0)) + refund
                await self.save_player(p2)
                await self._commit()
            res["msg"] = str(res.get("msg", "")) + f"（坊市摊减税 {refund}）"
            res["tax_refund"] = refund
        return res

    async def buy_shield(self, user_id: str, node_id: int) -> dict:
        """护山阵眼：护山盾免战时长 +10%/级。"""
        res = await super().buy_shield(user_id, node_id)
        world, p = await self._require_player(user_id)
        node = await self._owned_node(p, node_id)
        lvl = min(5, self._building_level(node, "guard_array"))
        if lvl > 0:
            hours = int((self.main_cfg().get("shield") or {}).get("hours", 12))
            extra = max(1, int(hours * 3600 * 0.1 * lvl))
            node["protect_until"] = int(node.get("protect_until", 0)) + extra
            data = world.get("data") or {}
            cd_map = data.get("shield_cd") or {}
            if str(node["id"]) in cd_map:
                cd_map[str(node["id"])] = int(cd_map[str(node["id"])]) + extra
                data["shield_cd"] = cd_map
                world["data"] = data
                await self.save_world(world)
            await self.save_node(node)
            await self._commit()
            res["msg"] = str(res.get("msg", "")) + f"（护山阵眼延长 {extra // 60} 分钟）"
        return res

    async def duel_arena_accept(self, user_id: str, duel_id: str,
                                char_id: int) -> dict:
        """擂将台：擂台彩头 +10%/级。"""
        res = await super().duel_arena_accept(user_id, duel_id, char_id)
        if not res.get("ok"):
            return res
        world, p = await self._require_player(user_id)
        data = world.get("data") or {}
        d = (data.get("duel_arena") or {}).get(str(duel_id)) or {}
        stake = int(d.get("stake", 0) or 0)
        if stake <= 0 or res.get("draw"):
            return res
        level = 0
        for node in await self.get_nodes(world["id"]):
            if int(node.get("owner_faction", 0)) == int(p["faction_id"]):
                level = max(level, min(5, self._building_level(node, "arena")))
        if level <= 0:
            return res
        bonus = int(round(stake * 0.1 * level *
                          float((self.main_cfg().get("duel") or {}).get(
                              "drum_tower_mult", 1.0) or 1.0)))
        c = await self.get_char(int(char_id))
        # 以对战结算返回的 winner_user 为准；旧实现用「武将名是否出现在
        # 战报文案」猜胜者，同名/改名/文案调整都会错发彩头。
        winner = str(res.get("winner_user") or "") if c else ""
        if winner and bonus > 0:
            wp = await self.get_player(winner, world["id"])
            if wp:
                wp["coin"] = int(wp.get("coin", 0)) + bonus
                await self.save_player(wp)
                await self._commit()
                res["msg"] = str(res.get("msg", "")) + f"（擂将台加彩 {bonus}）"
                res["arena_bonus"] = bonus
        return res

    # ------------------------------------------------------------------
    # 战功商店（§11.3）
    # ------------------------------------------------------------------
    #: 周限计数字段：快照展示时跨周自动归零（购买时仍由 buy_merit_shop 复核）
    _MERIT_SHOP_WEEK_KEYS = ("merit_shop_week", "merit_shop_week_iron",
                             "merit_shop_week_material")
    _MERIT_SHOP_MONTH_KEY = "merit_shop_month"

    def _honor_balance(self, honor: dict) -> int:
        """可支配荣誉点 = 累计获得 - 历史已消费，跨赛季结转。"""
        total = int((honor or {}).get("total", 0) or 0)
        spent = (honor or {}).get("spent") or {}
        if not isinstance(spent, dict):
            return max(0, total)
        used = 0
        for v in spent.values():
            if isinstance(v, (int, float)):
                used += int(v)
            elif isinstance(v, dict):
                used += int(v.get("amount", 0) or 0)
        return max(0, total - used)

    def _honor_view(self, p: dict) -> dict:
        """honor 快照视图：把上周/上月的限购计数归一成本周期。

        只影响下发数据，不写库——购买动作自己会判断并重置，避免每次刷新
        面板都产生一次 UPDATE。
        """
        honor = dict(p.get("honor") or {})
        week = time.strftime("%Y%W")
        for key in self._MERIT_SHOP_WEEK_KEYS:
            sw = dict(honor.get(key) or {})
            if sw.get("week") != week:
                sw = {"week": week, "used": 0}
            honor[key] = sw
        month = time.strftime("%Y%m")
        ms = dict(honor.get(self._MERIT_SHOP_MONTH_KEY) or {})
        if ms.get("month") != month:
            ms = {"month": month, "used": {}}
        if not isinstance(ms.get("used"), dict):
            ms["used"] = {}
        honor[self._MERIT_SHOP_MONTH_KEY] = ms
        season_spent = honor.get("season_spent") or {}
        honor["season_spent"] = season_spent if isinstance(season_spent, dict) else {}
        honor["balance"] = self._honor_balance(honor)
        return honor

    def _week_counter(self, honor: dict, key: str) -> dict:
        week = time.strftime("%Y%W")
        sw = dict(honor.get(key) or {})
        if sw.get("week") != week:
            sw = {"week": week, "used": 0}
        return sw

    def _month_counter(self, honor: dict) -> dict:
        month = time.strftime("%Y%m")
        ms = dict(honor.get(self._MERIT_SHOP_MONTH_KEY) or {})
        if ms.get("month") != month:
            ms = {"month": month, "used": {}}
        if not isinstance(ms.get("used"), dict):
            ms["used"] = {}
        return ms

    def _gold_inject_room(self, p: dict) -> int:
        """今日还能**完整**注入主玩法钱包的灵石额度（软/硬限额取小）。"""
        gcfg = self.main_cfg().get("gold_output") or {}
        hard_cap = int(gcfg.get("max_daily_gold_cap", 20_000_000))
        daily_cap = int(gcfg.get("daily_gold_cap", 5_000_000))
        cap = max(0, min(hard_cap, daily_cap))
        today = time.strftime("%Y%m%d")
        sent = int((p.get("daily_output_log") or {}).get(today, 0) or 0)
        return max(0, cap - sent), cap, sent

    async def _grant_main_equipment(self, user_id: str,
                                    quality: str = "") -> dict:
        """给主玩法角色发放一件当前境界附近的装备（装备副本）。"""
        if not self.config_manager:
            raise StrategyError("主玩法装备池未加载，暂时无法兑换")
        player = await self.db.get_player_by_id(user_id)
        if player is None:
            raise StrategyError("主玩法角色不存在，请先进入修仙")
        from ..core.equipment_drop import roll_equipment_drop
        from ..core.storage_ring_manager import StorageRingManager
        try:
            level_index = int(getattr(player, "level_index", 0) or 0)
        except (TypeError, ValueError):
            level_index = 0
        drop = roll_equipment_drop(self.config_manager, level_index)
        if not drop:
            raise StrategyError("当前境界的装备池为空，暂时无法兑换")
        q = str(quality or drop.get("quality") or "")
        srm = StorageRingManager(self.db, self.config_manager)
        ok, msg = await srm.grant_item(
            player, drop["name"], 1, quality=q, silent=False)
        if not ok:
            raise StrategyError(msg or "储物戒容量不足，无法领取装备")
        return {"name": drop["name"], "quality": q, "msg": msg}

    async def buy_merit_shop(self, user_id: str, item: str, char_id: int = 0,
                             role: str = "") -> dict:
        world, p = await self._require_player(user_id)
        scfg = self.main_cfg().get("merit_shop") or {}
        honor = p.get("honor") or {}
        if item == "gold_pack":
            spec = scfg.get("gold_pack") or {"merit": 500, "gold": 100000, "weekly_limit": 5}
            shop_week = self._week_counter(honor, "merit_shop_week")
            limit = max(1, int(spec.get("weekly_limit", 5)))
            if int(shop_week.get("used", 0)) >= limit:
                raise StrategyError("本周灵石包兑换次数已用完")
            cost = int(spec.get("merit", 500))
            if p["merit"] < cost:
                raise StrategyError(f"战功不足：需 {cost}")
            amount = int(spec.get("gold", 100000))
            # 主玩法角色不存在时，_inject_gold 无法落库却会照常扣战功，
            # 必须在扣点前拦截。
            if await self.db.get_player_by_id(user_id) is None:
                raise StrategyError("主玩法角色不存在，请先进入修仙后再兑换灵石")
            room, cap, sent = self._gold_inject_room(p)
            if room < amount:
                raise StrategyError(
                    f"今日灵石注入剩余额度不足（剩余 {room}，本次需 {amount}），"
                    f"本次未扣除战功，请明日再来或减少兑换")
            p["merit"] -= cost
            shop_week["used"] = int(shop_week.get("used", 0)) + 1
            honor["merit_shop_week"] = shop_week
            p["honor"] = honor
            got, merit_gain = await self._inject_gold(p, amount)
            await self.save_player(p)
            await self._commit()
            msg = (f"兑换成功：灵石 +{got}"
                   f"（本周第 {shop_week['used']}/{limit} 次）")
            if merit_gain > 0:
                msg += f"；超出每日限额部分折算战功 +{merit_gain}"
            return {"ok": True, "msg": msg}
        if item == "iron_pack":
            spec = scfg.get("iron_pack") or {"merit": 300, "iron": 500,
                                             "weekly_limit": 3}
            shop_week = self._week_counter(honor, "merit_shop_week_iron")
            limit = max(1, int(spec.get("weekly_limit", 3)))
            if int(shop_week.get("used", 0)) >= limit:
                raise StrategyError("本周精铁包兑换次数已用完")
            cost = int(spec.get("merit", 300))
            if p["merit"] < cost:
                raise StrategyError(f"战功不足：需 {cost}")
            gain = max(0, int(spec.get("iron", 500)))
            p["merit"] -= cost
            p["iron"] = int(p.get("iron", 0)) + gain
            shop_week["used"] = int(shop_week.get("used", 0)) + 1
            honor["merit_shop_week_iron"] = shop_week
            p["honor"] = honor
            await self.save_player(p)
            await self._commit()
            return {"ok": True,
                    "msg": f"兑换成功：精铁 +{gain}"
                           f"（本周第 {shop_week['used']}/{limit} 次）"}
        if item == "material_pack":
            spec = scfg.get("material_pack") or {
                "merit": 300, "coin": 8000, "grain": 8000, "iron": 400,
                "weekly_limit": 3}
            shop_week = self._week_counter(honor, "merit_shop_week_material")
            limit = max(1, int(spec.get("weekly_limit", 3)))
            if int(shop_week.get("used", 0)) >= limit:
                raise StrategyError("本周物资包兑换次数已用完")
            cost = int(spec.get("merit", 300))
            if p["merit"] < cost:
                raise StrategyError(f"战功不足：需 {cost}")
            gains = {
                "coin": max(0, int(spec.get("coin", 8000))),
                "grain": max(0, int(spec.get("grain", 8000))),
                "iron": max(0, int(spec.get("iron", 400))),
            }
            p["merit"] -= cost
            for k, v in gains.items():
                p[k] = int(p.get(k, 0) or 0) + v
            shop_week["used"] = int(shop_week.get("used", 0)) + 1
            honor["merit_shop_week_material"] = shop_week
            p["honor"] = honor
            await self.save_player(p)
            await self._commit()
            detail = "、".join(
                f"{ {'coin':'铜钱','grain':'粮草','iron':'精铁'}[k] } +{v}"
                for k, v in gains.items() if v > 0)
            return {"ok": True,
                    "msg": f"物资包兑换成功：{detail}"
                           f"（本周第 {shop_week['used']}/{limit} 次）"}
        if item == "equip_instance":
            spec = scfg.get("equip_instance") or {
                "merit": 3000, "monthly_limit": 1, "quality": "极佳"}
            ms = self._month_counter(honor)
            limit = max(1, int(spec.get("monthly_limit", 1)))
            used = int((ms.get("used") or {}).get(item, 0) or 0)
            if used >= limit:
                raise StrategyError("本月装备副本兑换次数已用完")
            cost = int(spec.get("merit", 3000))
            if p["merit"] < cost:
                raise StrategyError(f"战功不足：需 {cost}")
            got = await self._grant_main_equipment(
                user_id, str(spec.get("quality", "") or ""))
            p["merit"] -= cost
            ms["used"][item] = used + 1
            honor[self._MERIT_SHOP_MONTH_KEY] = ms
            p["honor"] = honor
            await self.save_player(p)
            await self._commit()
            q = f"（{got['quality']}）" if got.get("quality") else ""
            return {"ok": True,
                    "msg": f"兑换成功：获得装备「{got['name']}」{q}，"
                           f"已存入储物戒（本月 {used + 1}/{limit} 次）"}
        if item == "recommend_letter":
            spec = scfg.get("recommend_letter") or {"merit": 200}
            if role not in R.ROLES:
                raise StrategyError("举荐信须指定定向职责：武将 / 军师 / 内政官")
            cost = int(spec.get("merit", 200))
            if p["merit"] < cost:
                raise StrategyError(f"战功不足：需 {cost}")
            cfg = self.main_cfg().get("recruit") or {}
            p["merit"] -= cost
            tavern = self._roll_tavern(
                p["user_id"], int(world["id"]), p.get("tavern") or {}, cfg,
                _now(), salt=_now(), role_force=role)
            p["tavern"] = tavern
            await self.save_player(p)
            await self._commit()
            return {"ok": True,
                    "msg": f"举荐信已使用：酒馆候选全部定向为"
                           f"「{R.ROLE_NAMES.get(role, role)}」"}
        if item == "elixir":
            spec = scfg.get("elixir") or {"merit": 1500, "season_limit_per_char": 2}
            icfg = self.main_cfg().get("injury") or {}
            limit = max(1, int(spec.get(
                "season_limit_per_char", icfg.get("elixir_season_limit", 2))))
            if not char_id:
                raise StrategyError("须指定角色")
            c = await self._own_char(p, char_id)
            if c["injury"] <= 0:
                raise StrategyError(f"{c['name']} 没有伤势")
            used_map = honor.get("elixir_used") or {}
            used = int(used_map.get(str(char_id), 0))
            if used >= limit:
                raise StrategyError(f"{c['name']} 本赛季丹药次数已用完（{limit} 次）")
            cost = int(icfg.get("elixir_merit_cost", spec.get("merit", 1500)))
            if p["merit"] < cost:
                raise StrategyError(f"战功不足：需 {cost}")
            p["merit"] -= cost
            heal = int(icfg.get("elixir_heal", 40))
            c["injury"] = max(0, c["injury"] - heal)
            used_map[str(char_id)] = used + 1
            honor["elixir_used"] = used_map
            p["honor"] = honor
            await self.save_char(c)
            await self.save_player(p)
            await self._commit()
            state = R.INJURY_STATE_NAMES.get(R.injury_state(c["injury"]), "")
            return {"ok": True, "msg": f"{c['name']} 服用疗伤丹：伤势 -{heal}（现 {state}）"}
        raise StrategyError(f"未知商品：{item}")

    # ------------------------------------------------------------------
    # 荣誉商店（§11 赛季保留：荣誉点跨赛季结转）
    # ------------------------------------------------------------------
    async def buy_honor_shop(self, user_id: str, item: str,
                             role: str = "") -> dict:
        world, p = await self._require_player(user_id)
        scfg = (self.main_cfg().get("season") or {}).get("honor_shop") or {}
        spec = scfg.get(item)
        if not spec:
            raise StrategyError(f"未知荣誉商品：{item}")
        honor = p.get("honor") or {}
        cost = int(spec.get("honor", 0) or 0)
        balance = self._honor_balance(honor)
        if cost <= 0:
            raise StrategyError("荣誉商品价格配置错误")
        if balance < cost:
            raise StrategyError(
                f"荣誉点不足：需 {cost}，当前 {balance}（赛季结算后按战功发放）")
        season_spent = honor.get("season_spent") or {}
        if not isinstance(season_spent, dict):
            season_spent = {}
        limit = int(spec.get("season_limit", 0) or 0)
        used = int(season_spent.get(item, 0) or 0)
        if limit > 0 and used >= limit:
            raise StrategyError(f"本赛季该荣誉商品已兑换（{used}/{limit}）")

        if item == "gold_pack":
            amount = int(spec.get("gold", 100000))
            room, cap, sent = self._gold_inject_room(p)
            if room < amount:
                raise StrategyError(
                    f"今日灵石注入剩余额度不足（剩余 {room}，本次需 {amount}），"
                    f"本次未扣除荣誉点，请明日再来")
            got, merit_gain = await self._inject_gold(p, amount)
            msg = f"荣誉兑换成功：灵石 +{got}"
            if merit_gain > 0:
                msg += f"；超出每日限额部分折算战功 +{merit_gain}"
        elif item == "title_pack":
            title = str(spec.get("title", "") or "").strip()
            if not title:
                raise StrategyError("称号配置缺失")
            main_player = await self.db.get_player_by_id(user_id)
            if main_player is None:
                raise StrategyError("主玩法角色不存在，请先进入修仙")
            hooks = getattr(self, "_title_hooks", None)
            if hooks is None:
                from .title_hooks import TitleHooks
                hooks = self._title_hooks = TitleHooks(self.db)
            if hooks._tm().has_title(main_player, title):
                raise StrategyError(f"你已拥有称号「{title}」")
            tmsg = await hooks.grant_manual(main_player, title)
            if not tmsg:
                raise StrategyError(f"称号「{title}」授予失败")
            msg = f"荣誉兑换成功：{tmsg.strip()}"
        elif item == "boost_pack":
            mult = float(spec.get("output_mult", 1.2) or 1.2)
            days = max(1, int(spec.get("days", 7) or 7))
            honor["boost"] = {"mult": mult,
                              "until": _now() + days * 86400}
            msg = (f"荣誉兑换成功：战略产出 ×{mult:g}，"
                   f"持续 {days} 天")
        elif item == "material_pack":
            gains = {k: max(0, int(spec.get(k, 0) or 0))
                     for k in ("coin", "grain", "iron")}
            for k, v in gains.items():
                p[k] = int(p.get(k, 0) or 0) + v
            detail = "、".join(
                f"{ {'coin':'铜钱','grain':'粮草','iron':'精铁'}[k] } +{v}"
                for k, v in gains.items() if v > 0) or "资源"
            msg = f"荣誉兑换成功：{detail}"
        elif item == "avatar_frame":
            # v5 预留：头像框未接入主玩法时以称号/荣誉展示替代
            msg = "荣誉兑换成功：头像框已记录（待主玩法头像框上线后生效）"
        else:
            raise StrategyError(f"未知荣誉商品：{item}")

        spent = honor.get("spent") or {}
        if not isinstance(spent, dict):
            spent = {}
        spent[item] = int(spent.get(item, 0) or 0) + cost
        honor["spent"] = spent
        season_spent[item] = used + 1
        honor["season_spent"] = season_spent
        p["honor"] = honor
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": msg,
                "honor": self._honor_balance(honor)}

    # ------------------------------------------------------------------
    # tick 调度入口（§10.2：幂等 + 断线补跑 ≤8）
    # ------------------------------------------------------------------
    async def run_tick_catchup(self) -> list[dict]:
        """由 main.py 定时器周期调用：按 last_tick_at 计算欠账 tick 并补跑。"""
        if not self.enabled():
            return []
        await self.ensure_tables()
        world = await self.ensure_world()
        interval = max(1, self.tick_seconds())  # 防除零
        now = _now()
        last = int(world.get("last_tick_at") or 0)
        if last <= 0:
            world["last_tick_at"] = now
            await self.save_world(world)
            await self._commit()
            return []
        due = (now - last) // interval
        if due <= 0:
            return []
        max_catch = int(self.main_cfg().get("max_catchup_ticks", 8))
        full = min(due, max_catch)
        skipped = due - full
        results = []
        try:
            if skipped > 0:
                # 超出补跑上限：只结算产出（跳过 NPC/战斗），防滚雪球。
                # 但长时间停机（due 可达数千）时无限补跑会长期持锁把玩家
                # 动作全部挡在 423；这里再设一层硬上限，多余 tick 直接丢弃。
                out_cap = int(self.main_cfg().get("max_catchup_output_ticks", 192))
                out_cap = max(0, out_cap)
                if skipped > out_cap:
                    logger.warning(
                        "[strategy] 补跑产出 tick 数 %d 超过上限 %d，丢弃 %d 个",
                        skipped, out_cap, skipped - out_cap)
                    skipped = out_cap
                if skipped > 0:
                    res = await self.tick(output_only=True, repeat=skipped)
                    results.append(res)
            for _ in range(full):
                results.append(await self.tick())
        except StrategyBusyError as e:
            # 玩家动作持锁中：本轮跳过，下周期按 last_tick_at 自动补跑
            logger.info(f"[strategy] tick 繁忙跳过：{e}")
        return results

    async def tick(self, output_only: bool = False, repeat: int = 1) -> dict:
        """推进一个世界 tick（九步管线，§10.2）。

        A-M7：与玩家动作互斥——先取 _action_lock 再取 _world_lock（恒定锁序）；
        任一锁忙则直接 Busy 拒绝（本轮跳过，run_tick_catchup 下周期按
        last_tick_at 补跑），避免排队导致的双 tick 叠跑。
        """
        summary: dict = {"ok": True, "tick_no": 0, "battles": [], "events": [],
                         "gold_injected": 0, "output_only": output_only}
        if self._action_lock.locked() or self._world_lock.locked():
            raise StrategyBusyError("世界推进中，请稍后再试")
        async with self._action_lock:
            async with self._world_lock:
                for _ in range(max(1, int(repeat))):
                    summary = await self._tick_locked(output_only, summary)
        return summary

    async def _tick_locked(self, output_only: bool, prev: dict) -> dict:
        world = await self.get_world()
        if not world:
            raise StrategyError("世界不存在")
        world["tick_no"] = int(world.get("tick_no", 0)) + 1
        tick_no = world["tick_no"]
        summary = {"ok": True, "tick_no": tick_no, "battles": [], "events": [],
                   "gold_injected": 0, "output_only": output_only}
        # A-M6 时钟先落盘（事务内）：管线中途崩溃时整笔回滚，catchup
        # 会以同一 tick_no 完整重跑；不会再出现「时钟已推进 + 前半段已
        # 落库 + 后半段永久跳过」的半提交。autocommit 下单独 rollback
        # 是空操作，因此必须显式 BEGIN IMMEDIATE 包住整段 tick。
        try:
            await self.db.conn.execute("BEGIN IMMEDIATE")
        except Exception:  # noqa: BLE001
            await self.db.conn.rollback()
            await self.db.conn.execute("BEGIN IMMEDIATE")
        self._tick_task = asyncio.current_task()
        try:
            world["last_tick_at"] = _now()
            await self.save_world(world)
            # 1. 任务结算（到期任务入账，角色转 idle）
            if not output_only:
                await self._step_settle_tasks(world)
            else:
                await self._step_settle_tasks(world)  # 到期任务照常结算
            # 2. 据点产出分配（铜钱/粮/铁 + 主玩法灵石注入含日限额）
            gold_total = await self._step_distribute_output(world, output_only)
            summary["gold_injected"] = gold_total
            # 2b. 坊市流将竞拍：到期结算/补挂（补跑 tick 也执行，避免流将无限挂起）
            await self._step_wanderer_auctions(world)
            if not output_only:
                # 3. 围城推进
                battles = await self._step_sieges(world)
                summary["battles"].extend(battles)
                # 4. 行军推进 + 粮耗/断粮
                await self._step_march(world)
                # 5. 抵达触发会战 / 占领
                battles = await self._step_arrivals(world)
                summary["battles"].extend(battles)
                # 6. NPC AI（驻军恢复 / 扩张出征 / 魔潮）
                await self._step_npc_ai(world)
                # 7. 事件掷点（0~2 个/tick）
                ev_names = await self._step_events(world)
                summary["events"].extend(ev_names)
                # 7a. 势力事件（§10.3：角色出走/民心沸腾/哗变/细作/秘藏/名将迟暮）
                await self._step_faction_events(world)
                # 7b. 事件传染（瘟疫 spread：25%/tick 蔓延相邻据点）
                await self._step_event_spread(world)
                # 8. 伤势/体力恢复 + 城防自修 + 士气回归
                await self._step_recovery(world)
                # 8b. P1：俘虏流转 / 科技 / 治理（官职·俸禄）
                await self._step_captives(world)
                await self._step_captive_transfers(world)
                await self._step_tech(world)
                await self._step_governance(world)
                # 8b-2. 势力任务 / 动员令 / 低忠诚出走（§6.4 §18.4）
                await self._step_faction_tasks(world)
                await self._step_mobilizations(world)
                await self._step_loyalty_defect(world)
                # 8c. P2：hex 手操战役超时托管推进
                await self._step_manual_battles(world)
                # 8d. P3：互市 / 匪寨 / 商路 / 粮队
                await self._step_market(world)
                await self._step_market_maker(world)
                await self._step_bandits(world)
                await self._step_bandit_raids(world)
                await self._step_trade_routes(world)
                await self._step_convoys(world)
                # 8e. 赛季推进（阶段/霸权/合纵/结算，须放最后）
                await self._step_season(world)
                # 8f. 战报归档清理（§18.4 result_keep_days）
                await self._step_cleanup_battles(world)
                # 8g. 擂台过期清理（退还赌注，无交互也会回收）
                await self._sweep_duel_arena(world)
            # 9. 落盘世界时钟
            world["last_tick_at"] = _now()
            data = world.get("data") or {}
            recent = data.get("recent_ticks") or []
            recent.append({"tick_no": tick_no, "at": world["last_tick_at"],
                           "battles": len(summary["battles"]),
                           "events": summary["events"],
                           "gold": summary["gold_injected"],
                           "output_only": output_only})
            data["recent_ticks"] = recent[-12:]
            world["data"] = data
            await self.save_world(world)
            # 整段 tick 成功：恢复真实提交并一次性落盘
            self._tick_task = None
            await self._commit()
        except Exception as e:  # noqa: BLE001
            self._tick_task = None
            logger.exception(f"[strategy] tick#{tick_no} 异常: {e}")
            summary["ok"] = False
            summary["error"] = str(e)
            try:
                await self.db.conn.rollback()
            except Exception:  # noqa: BLE001
                logger.warning(f"[strategy] tick#{tick_no} 回滚失败", exc_info=True)
        return summary

    async def _step_cleanup_battles(self, world: dict):
        """§18.4 战报归档：超过 ``security.result_keep_days`` 的明细置为截断。"""
        days = int((self.main_cfg().get("security") or {}).get(
            "result_keep_days", 7) or 7)
        if days <= 0:
            return
        cutoff = _now() - days * 86400
        await self._execute(
            "UPDATE strategy_battles SET result_json='{\"truncated\": true, "
            "\"archived\": true}', report_text=substr(report_text, 1, 400) "
            "WHERE world_id=? AND created_at<? AND result_json NOT LIKE "
            "'%\"archived\": true%'", (world["id"], cutoff))
        await self._commit()

    # --- 步骤 1：任务结算 -------------------------------------------------
    async def _step_settle_tasks(self, world: dict):
        rows = await self._fetchall(
            "SELECT * FROM strategy_tasks WHERE world_id=? AND status='active' AND end_at<=?",
            (world["id"], _now()))
        for row in rows:
            task = self._decode("strategy_tasks", row)
            c = await self.get_char(task["character_id"])
            if not c:
                await self.db.conn.execute(
                    "UPDATE strategy_tasks SET status='orphan' WHERE id=?", (task["id"],))
                continue
            # 角色在任务期间已出走/被押运：任务作废，不发奖，也不要把状态
            # 强改回 idle（否则 wander 被覆盖、收益还会进原势力国库）。
            if str(c.get("status") or "") in ("wander", "transfer"):
                await self.db.conn.execute(
                    "UPDATE strategy_tasks SET status='cancelled' WHERE id=?",
                    (task["id"],))
                continue
            p = None
            if c.get("owner_user_id"):
                p = await self.get_player(c["owner_user_id"], world["id"])
            if p is None:
                # NPC 角色任务：收益进势力国库
                p = await self._npc_wallet(world, task["faction_id"])
            ticks = int((task.get("payout") or {}).get("ticks", 4))
            # 单任务用 SAVEPOINT 包裹：结算失败只回滚该任务，保留 active
            # 供下 tick 重试；此前吞掉异常仍置 done + idle，奖励永久丢失。
            # 仅在 tick 事务内使用（tick 外是 autocommit，且内部成就等
            # 辅助函数可能显式 commit，会提前释放 savepoint）。
            use_sp = self._in_tick_task()
            sp = f"strategy_task_{int(task['id'])}"
            if use_sp:
                await self.db.conn.execute(f"SAVEPOINT {sp}")
            try:
                if str(task["task_type"]).startswith("plot_"):
                    # 计略任务（§6.4）：走独立结算，不走 _pay_task
                    await self._settle_plot_task(world, task, c, p)
                else:
                    await self._pay_task(world, p, task, c, ticks)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[strategy] 任务#{task['id']}结算失败（保留待重试）: {e}")
                if use_sp:
                    await self.db.conn.execute(f"ROLLBACK TO {sp}")
                    await self.db.conn.execute(f"RELEASE {sp}")
                continue
            if use_sp:
                await self.db.conn.execute(f"RELEASE {sp}")
            # 瘟疫期执行任务的受伤判定（task_injury 效果，§11.1）
            if c.get("status") not in ("captive", "dead"):
                try:
                    await self._roll_task_injury(world, task, c)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[strategy] 任务#{task['id']}受伤判定失败: {e}")
            if c.get("status") in ("captive", "dead"):
                pass  # 被俘保持 captive；伤重不治保持 dead
            else:
                c["status"] = "idle"
            await self.save_char(c)
            # 双人协作：搭档同步结束占用（收益已在主执行者结算时 ×1.15）
            partner_id = int((task.get("payout") or {}).get("partner_char_id") or 0)
            if partner_id:
                partner = await self.get_char(partner_id)
                if partner and partner.get("status") not in ("captive", "dead"):
                    partner["status"] = "idle"
                    R.gain_exp(partner, max(1, int(ticks * 2)),
                               self._growth_cap_bonus())
                    await self.save_char(partner)
            await self.db.conn.execute(
                "UPDATE strategy_tasks SET status='done' WHERE id=?", (task["id"],))
        await self._commit()

    async def _roll_task_injury(self, world: dict, task: dict, c: dict):
        """task_injury 效果：任务结算时角色按概率受伤（瘟疫 5% 伤势 +10）。"""
        node_id = int(task.get("node_id", 0) or 0)
        if not node_id:
            return
        for ev in await self.get_active_events(world["id"], world.get("tick_no", 0)):
            if node_id not in ((ev.get("target") or {}).get("node_ids") or []):
                continue
            for eff in (ev.get("data") or {}).get("effects") or []:
                if eff.get("type") != "task_injury":
                    continue
                task_types = eff.get("task_types") or ["*"]
                if "*" not in task_types and \
                        str(task["task_type"]) not in task_types:
                    continue
                rng = self._rng("task_injury", ev["id"], task["id"], c["id"])
                if rng.random() < float(eff.get("chance", 0.05)):
                    if R.apply_injury(c, int(eff.get("injury", 10))):
                        logger.info(f"[strategy] 角色#{c['id']} 疫区任务伤重不治")
                    return

    async def _npc_wallet(self, world: dict, faction_id: int) -> dict:
        """NPC 势力任务收益的伪钱包（coin→国库，其余丢弃）。"""
        class _W(dict):
            pass
        w = _W(user_id="", world_id=world["id"], faction_id=faction_id, coin=0,
               grain=0, iron=0, recruit_token=0, merit=0, merit_season=0,
               honor={}, daily_output_log={}, notify_pref={}, tutorial={},
               achievements={}, newcomer={}, rebuild_pay_log=[], tavern={})
        w["_npc"] = True
        return w

    async def _flush_task_wallet(self, p: dict, task: dict) -> None:
        """任务收益落库：NPC 伪钱包转势力国库，玩家正常 save_player。

        ``_npc_wallet`` 的 user_id 为空，直接 ``save_player`` 不会写入任何行，
        导致 NPC 完成任务的 coin/grain/iron 收益凭空丢失。
        """
        if not p.get("_npc"):
            await self.save_player(p)
            return
        fid = int(p.get("faction_id") or task.get("faction_id") or 0)
        f = await self.get_faction(fid)
        if not f:
            return
        treasury = dict(f.get("treasury") or {})
        changed = False
        for key in ("coin", "grain", "iron"):
            amount = int(p.get(key, 0) or 0)
            if amount > 0:
                treasury[key] = int(treasury.get(key, 0) or 0) + amount
                changed = True
        if changed:
            f["treasury"] = treasury
            await self.save_faction(f)

    # --- 步骤 2：产出分配 -------------------------------------------------
    async def _step_distribute_output(self, world: dict, output_only: bool) -> int:
        cfg = self.main_cfg()
        per_tick = cfg.get("node_output_per_tick") or {}
        gcfg = cfg.get("gold_output") or {}
        gold_per_type = gcfg.get("per_tick_by_type") or {}
        diminish_after = int(gcfg.get("diminish_extra_nodes", 6))
        diminish_factor = float(gcfg.get("diminish_factor", 0.6))
        npc_bonus = float((cfg.get("npc") or {}).get("output_bonus", 1.2))
        nodes = await self.get_nodes(world["id"])
        # 事件修正表：node_id → {resource: mult}；global mult
        node_mult: dict[int, dict[str, float]] = {}
        global_mult = 1.0
        for ev in await self.get_active_events(world["id"], world.get("tick_no", 0)):
            for eff in (ev.get("data") or {}).get("effects") or []:
                if eff.get("type") == "global_output":
                    global_mult *= float(eff.get("mult", 1.0))
                elif eff.get("type") == "node_output":
                    for nid in (ev.get("target") or {}).get("node_ids") or []:
                        m = node_mult.setdefault(int(nid), {})
                        res = eff.get("resource", "coin")
                        m[res] = m.get(res, 1.0) * float(eff.get("mult", 1.0))
        gold_total = 0
        # --- 玩家个人势力 ---
        players = await self._fetchall(
            "SELECT * FROM strategy_players WHERE world_id=?", (world["id"],))
        for prow in players:
            p = self._decode("strategy_players", prow)
            f_nodes = sorted(
                [n for n in nodes if n["owner_faction"] == p["faction_id"] and
                 n["owner_faction"] != 0],
                key=lambda n: -(R.NODE_OUTPUT_VALUE.get(n["type"], 40)))
            if not f_nodes:
                continue
            # 势力国库 + 司徒（角色官职）据点产出加成；结盟后统一进入盟主国库
            f = await self.get_faction(p["faction_id"])
            f = await self._alliance_host(f) if f else None
            pos_bonus = await self._faction_position_bonus(f) if f else {}
            output_mult = 1.0 + float(pos_bonus.get("node_output", 0.0))
            # 农政科技 L1/L3：全据点产出 +4%/+8%
            output_mult += float(R.tech_effects(
                (f or {}).get("tech") or {}).get("output_bonus", 0.0))
            for idx, node in enumerate(f_nodes):
                base = per_tick.get(node["type"]) or {}
                mult = global_mult * (1 + 0.2 * (node["level"] - 1))
                # 赛季决战 +20% / 追赶礼包 ×1.5（§11/§17）/ 司徒加成
                mult *= self.player_output_mult(p) * self.season_output_mult(world) \
                    * output_mult * self._node_output_mult(node, f)
                # §9.6 谣言/破坏：计略产出减益
                mult *= self.plot_output_mult(node)
                nm = node_mult.get(node["id"], {})
                gain_coin = int(base.get("coin", 0) * mult * nm.get("coin", 1.0))
                gain_grain = int(base.get("grain", 0) * mult * nm.get("grain", 1.0))
                gain_iron = int(base.get("iron", 0) * mult * nm.get("iron", 1.0))
                p["coin"] += gain_coin
                p["grain"] += gain_grain
                p["iron"] += gain_iron
                # 主玩法灵石注入（超出 6 节点部分 ×0.6，日限额在 _inject_gold）
                if idx >= diminish_after:
                    mult *= diminish_factor
                gross = int(gold_per_type.get(node["type"], 0) * mult)
                personal = int(gross * float(gcfg.get("personal_share", 0.7)))
                if personal > 0:
                    # 补产出（output_only）照发，仍受日限额；p 就地更新，
                    # 循环尾统一 save_player 落库
                    got, _m = await self._inject_gold(p, personal)
                    gold_total += got
                if f:
                    treasury = f.get("treasury") or {}
                    treasury["gold"] = int(treasury.get("gold", 0)) + int(
                        gross * float(gcfg.get("treasury_share", 0.3)))
                    f["treasury"] = treasury
            await self.save_player(p)
            if f:
                await self.save_faction(f)
        # --- NPC 势力国库 ---
        npc_factions = await self._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' AND alive=1",
            (world["id"],))
        for frow in npc_factions:
            fid = int(frow["id"])
            f_nodes = [n for n in nodes if n["owner_faction"] == fid]
            if not f_nodes:
                continue
            f = await self.get_faction(fid)
            treasury = f.get("treasury") or {}
            coin_gain = 0
            grain_gain = 0
            for node in f_nodes:
                base = per_tick.get(node["type"]) or {}
                mult = global_mult * npc_bonus * (1 + 0.2 * (node["level"] - 1))
                mult *= self.season_output_mult(world) * self._node_output_mult(node, f)
                nm = node_mult.get(node["id"], {})
                coin_gain += int(base.get("coin", 0) * mult * nm.get("coin", 1.0))
                grain_gain += int(base.get("grain", 0) * mult * nm.get("grain", 1.0))
            treasury["coin"] = int(treasury.get("coin", 0)) + coin_gain
            treasury["grain"] = int(treasury.get("grain", 0)) + grain_gain
            f["treasury"] = treasury
            await self.save_faction(f)
        await self._commit()
        return gold_total

    async def _siege_relation_block(self, atk_fid: int,
                                    owner_fid: int) -> str:
        """围城外交门禁：返回阻断原因（空串表示允许继续围城）。

        与 ``send_army`` / ``_attack_node`` 的宣战门禁保持同口径：停战、
        同盟（含同一联盟）与开战备战期均不得继续推进围城，否则玩家可
        先签停战再让既有围城破城，绕过 ``check_truce_violation`` 惩罚。
        """
        atk_fid = int(atk_fid or 0)
        owner_fid = int(owner_fid or 0)
        if atk_fid <= 0 or owner_fid <= 0 or atk_fid == owner_fid:
            return ""
        af = await self.get_faction(atk_fid)
        df = await self.get_faction(owner_fid)
        if not af or not df:
            return ""
        ah = await self._alliance_host(af) or af
        dh = await self._alliance_host(df) or df
        if int(ah.get("id", 0) or 0) == int(dh.get("id", 0) or 0):
            return "同盟"
        rel = R.relation_of(ah.get("diplomacy") or {},
                            int(dh.get("id", 0) or 0), _now())
        if rel == R.REL_ALLY:
            return "同盟"
        if rel == R.REL_TRUCE:
            return "停战"
        if rel == "war_pending":
            return "开战备战期"
        return ""

    # --- 步骤 3：围城推进（§7.2）------------------------------------------
    async def _step_sieges(self, world: dict) -> list[str]:
        cfg = self.main_cfg().get("siege") or {}
        max_ticks = int(cfg.get("max_ticks", 20))
        reports: list[str] = []
        sieging = await self.get_armies(world["id"], states=("siege",))
        by_node: dict[int, list[dict]] = {}
        for a in sieging:
            by_node.setdefault(a["node_id"], []).append(a)
        for nid, siege_armies in by_node.items():
            node = await self.get_node(world["id"], nid)
            if not node:
                continue
            # 手操战役进行中：暂停自动围城结算（城防/破城不重复推进）
            if await self._active_manual_battle(world["id"], nid):
                continue
            ss = node.get("siege_state") or {}
            atk_fid = int(ss.get("attacker_faction") or 0)
            attackers = [a for a in siege_armies
                         if atk_fid > 0 and int(a["faction_id"]) == atk_fid]
            # 归属缺失/归属方已无围城军：由现存围城军重新起算，
            # 避免旧势力残军与当前攻方混算（§7.2：单节点唯一攻方）
            if not attackers:
                atk_fid = (int(siege_armies[0]["faction_id"])
                           if siege_armies else 0)
                attackers = [a for a in siege_armies
                             if int(a["faction_id"]) == atk_fid]
                ss = {}
            attacker_ids = {id(a) for a in attackers}
            stale = [a for a in siege_armies if id(a) not in attacker_ids]
            # 保护期（夺城免战 / 护山盾）：围城中途生效则攻方撤围
            if int(node.get("owner_faction", 0)) > 0 and \
                    int(node.get("protect_until", 0) or 0) > _now():
                node["siege_state"] = {}
                await self.save_node(node)
                for a in siege_armies:
                    await self._auto_retreat(a)
                reports.append(f"🛡「{node['name']}」处于保护期，攻方撤围")
                continue
            # 外交门禁：停战/同盟/备战期期间不推进围城（否则可先签停战
            # 再让既有围城破城，绕过撕约惩罚；或把盟友城池围到易主）
            block = await self._siege_relation_block(
                atk_fid, int(node.get("owner_faction", 0) or 0))
            if block:
                node["siege_state"] = {}
                await self.save_node(node)
                for a in siege_armies:
                    await self._auto_retreat(a)
                reports.append(
                    f"🕊「{node['name']}」与守方处于「{block}」，攻方撤围")
                continue
            # 非当前归属势力的围城军撤围：同一节点只允许一个攻方结算
            for a in stale:
                await self._auto_retreat(a)
            if stale:
                reports.append(
                    f"🔀「{node['name']}」围城归属已变更，"
                    f"{len(stale)} 支旧围城军撤围")
            if not ss:
                ss = {"ticks": 0, "attacker_faction": atk_fid,
                      "choice": "auto", "since": _now()}
            ss["attacker_faction"] = atk_fid
            ss["ticks"] = int(ss.get("ticks", 0)) + 1
            # 攻方掉兵：城防反击（箭塔+守军）
            towers = min(5, self._building_level(node, "tower"))
            garrison_troops = int((node.get("npc_garrison") or {}).get("troops", 0))
            shares = R.siege_counter_damage(garrison_troops, towers, len(attackers))
            for a, dmg in zip(attackers, shares):
                a["troops"] = max(0, a["troops"] - int(dmg))
                a["veteran"] = min(R.VETERAN_MAX,
                                   a["veteran"] + R.veteran_gain(False, siege=True) // 5)
            # 主将轮换伤势（每 3 tick +5）
            if ss["ticks"] % max(1, int(cfg.get("injury_every_ticks", 3))) == 0:
                for a in attackers:
                    cids = a.get("characters") or []
                    if cids:
                        c = await self.get_char(cids[0])
                        if c and R.apply_injury(c, int(cfg.get("injury_amount", 5))):
                            pass  # 陨落处理 P1（俘虏/死亡体系）
                        if c:
                            await self.save_char(c)
            # 城防损伤
            sim_attackers = [await self._army_sim(a) for a in attackers]
            dmg = R.siege_tick_damage([s for s in sim_attackers if s])
            node["defense"] = max(0, node["defense"] - dmg)
            node["morale"] = max(20, node["morale"] - 2)
            destroyed = [a for a in attackers if a["troops"] <= 0]
            if destroyed:
                # A1：反击全歼的覆没善后（镜像 _apply_battle_aftermath 覆没分支）：
                # 角色伤势 +40（可能陨落）、军团删除、节点所有者被俘判定。
                # 此前仅从内存列表过滤，DB 残留 troops=0/state=siege 的僵尸军团。
                for a in destroyed:
                    a["_destroyed"] = True
                    for cid in a.get("characters") or []:
                        c = await self.get_char(int(cid))
                        if not c:
                            continue
                        dead = R.apply_injury(c, 40)
                        c["status"] = "dead" if dead else "idle"
                        c["location"] = {"node_id": node["id"]}
                        await self.save_char(c)
                    await self.delete_army(a["id"])
                if int(node.get("owner_faction", 0)) > 0:
                    await self.roll_captures(world, node, destroyed, "wipeout",
                                             int(node.get("owner_faction", 0)))
            attackers = [a for a in attackers if a["troops"] > 0]
            if not attackers:
                node["siege_state"] = {}
                await self.save_node(node)
                continue
            if node["defense"] <= 0:
                report = await self._siege_breach(world, node, attackers)
                if report:
                    reports.append(report)
                node = await self.get_node(world["id"], nid) or node
                ss = node.get("siege_state") or {}
                # A2：破城易主（受降/巷战获胜）后，同势力围城军转入驻守；
                # 否则滞留 siege 态继续「围攻己方城」且 siege_state 悬挂
                for a in attackers:
                    if a["state"] == "siege" and not a.get("_destroyed") and \
                            int(a["faction_id"]) == int(node.get("owner_faction", 0)):
                        a["state"] = "idle"
                        a["path"] = []
            elif ss["ticks"] >= max_ticks:
                # 超时：攻方粮尽撤退（§7.4：主将伤势 +10）
                ss = {}
                node["siege_state"] = ss
                for a in attackers:
                    cids = a.get("characters") or []
                    if cids:
                        c = await self.get_char(int(cids[0]))
                        if c and c.get("status") not in ("dead", "captive"):
                            R.apply_injury(c, int(cfg.get("timeout_injury", 10)))
                            await self.save_char(c)
                    await self._auto_retreat(a)
                reports.append(f"⏳「{node['name']}」围城超时，攻方粮尽撤围")
            node["siege_state"] = ss
            for a in attackers:
                await self.save_army(a)
            await self.save_node(node)
        await self._commit()
        return reports

    async def _siege_breach(self, world: dict, node: dict, attackers: list[dict]) -> str:
        """城防归零：受降（3 倍威压）或巷战（§7.2）。"""
        cfg = self.main_cfg()
        scfg = cfg.get("siege") or {}
        ss = node.get("siege_state") or {}
        choice = ss.get("choice") or "auto"
        atk_fid = int(ss.get("attacker_faction") or 0)
        # 只结算当前围城归属势力的军团，避免多势力围城时由列表第一支
        # 旧势力残军决定受降/巷战/占城归属。
        attackers = [a for a in attackers
                     if atk_fid <= 0 or int(a["faction_id"]) == atk_fid]
        if not attackers:
            return ""
        if atk_fid <= 0:
            atk_fid = int(attackers[0]["faction_id"])
        atk_sim = [await self._army_sim(a) for a in attackers]
        atk_sim = [s for s in atk_sim if s]
        atk_buff = await self._faction_buff(atk_fid, "attack_bonus")
        if atk_buff:
            for s in atk_sim:
                s["pos_atk_mult"] = float(s.get("pos_atk_mult", 1.0)) * (1 + atk_buff)
        atk_power = R.siege_strength(atk_sim)
        garrison_troops = int((node.get("npc_garrison") or {}).get("troops", 0))
        def_armies = await self.get_armies(world["id"], node_id=node["id"],
                                           states=("idle",))
        def_armies = [a for a in def_armies
                      if a["faction_id"] != atk_fid]
        def_power = sum(a["troops"] for a in def_armies) / 100 + garrison_troops / 100
        if choice == "auto" and atk_power >= float(cfg.get("intimidate_ratio", 3.0)) * max(
                def_power, 0.1):
            choice = "surrender"
        if choice == "surrender":
            keep = float(scfg.get("surrender_troops_keep", 0.5))
            # 保留比例统一交给 _capture_node 结算一次；此前先按 keep 写回，
            # _capture_node 又砍半，实际只剩 keep×50%。
            await self._capture_node(world, node, atk_fid, garrison_keep=keep)
            note = node.get("_capture_note") or ""
            return (f"🏳️「{node['name']}」守军开城受降！"
                    f"（攻方兵力 {atk_power:.0f} ≥ 3 倍威压）"
                    + (f"\n{note}" if note else ""))
        # 巷战：攻 +30% / 守 -30%（street_fight_*）
        atk_bonus = float(scfg.get("street_fight_atk_bonus", 0.3))
        dfn_buff = await self._faction_buff(
            int(node.get("owner_faction", 0) or 0), "defense_bonus")
        defenders_sim = []
        for a in def_armies:
            s = await self._army_sim(a)
            if s:
                s["morale"] = int(s["morale"] * (1 - float(
                    scfg.get("street_fight_def_penalty", 0.3))))
                if dfn_buff:
                    s["def_terrain_mult"] = float(
                        s.get("def_terrain_mult", 1.0)) * (1 + dfn_buff)
                defenders_sim.append((a, s))
        gar = self._garrison_sim(node)
        if gar:
            gar["morale"] = int(gar["morale"] * 0.7)
            if dfn_buff:
                gar["def_terrain_mult"] = float(
                    gar.get("def_terrain_mult", 1.0)) * (1 + dfn_buff)
            defenders_sim.append((None, gar))
        for s in atk_sim:
            s["atk_terrain_mult"] = float(s.get("atk_terrain_mult", 1.0)) * (1 + atk_bonus)
        rng = self._rng("battle", world["id"], node["id"], world["tick_no"])
        result = R.simulate_battle(
            atk_sim, [s for _a, s in defenders_sim], rng,
            max_rounds=int((cfg.get("battle") or {}).get("max_rounds", 30)),
            night=R.is_night(time.localtime().tm_hour), defending_node=False)
        report = R.render_battle_report(result, title=f"巷战·{node['name']}")
        await self._persist_battle(world, node, atk_fid,
                                   node["owner_faction"], "street", result, report,
                                   atk_sim, [s for _a, s in defenders_sim])
        await self._apply_battle_aftermath(
            world, node, attackers, [a for a, _s in defenders_sim], result)
        if result["winner"] == "attacker":
            # 巷战破城：先易主（解放原关押俘虏、腾出关押位），再对败方
            # 军团角色掷点擒获——与手操终局路径顺序一致。若先 roll_captures，
            # 统计关押位时仍算旧囚，会误判满员并把守将"驱逐"到即将易主的城。
            freed = await self._capture_node(world, node, atk_fid) or []
            losers = [a for a, _s in defenders_sim
                      if a and (a.get("_destroyed") or a.get("_routed"))]
            if losers:
                await self.roll_captures(world, node, losers, "city_fall",
                                         atk_fid, street=True)
            report += f"\n🏴「{node['name']}」易主！"
            if freed:
                report += f"\n🕊 关押的俘虏获释：{'、'.join(freed)}"
            if node.get("_capture_note"):
                report += f"\n{node['_capture_note']}"
        else:
            node["defense"] = int(R.city_defense_max(node["level"]) * 0.2)
            node["siege_state"] = {}
            # 守方巷战获胜：必须立刻落库。调用方 _step_sieges 返回后会
            # 重新 get_node 覆盖内存对象，未保存会让城防/围城状态回滚，
            # 导致围城永久悬挂（城防归零却不再触发破城）且城防不自修。
            await self.save_node(node)
            # A-M10：战败幸存攻方的自动撤退已统一在 _apply_battle_aftermath 处理
        return report

    async def _clear_bandit_marker(self, world: dict, node: dict):
        """据点被玩家势力占领/重建后清除匪寨标记。

        ``world.data.bandits`` 是粮队劫掠（``_find_convoy_raider``）与
        匪寨 tick 的判定依据；不清除会让已易主的城池继续被当作匪寨：
        粮队经过即被无主“劫掠”，库存也继续被 tick 抽取。
        """
        data = world.get("data") or {}
        bandits = data.get("bandits") or {}
        if str(int(node["id"])) in bandits:
            bandits.pop(str(int(node["id"])), None)
            data["bandits"] = bandits
            world["data"] = data
            await self.save_world(world)
        lair = dict(node.get("lair") or {})
        if lair.get("bandit") or str(lair.get("source") or "") == "bandit_data":
            for k in ("bandit", "source", "last_raid_tick"):
                lair.pop(k, None)
            node["lair"] = lair

    async def _capture_node(self, world: dict, node: dict,
                            faction_id: int,
                            garrison_keep: float = 0.5) -> list[str]:
        """据点易主：驻军溃散、保护期、关押俘虏解放（返回获释名单）。

        ``garrison_keep``：易主后保留的守军比例（受降可配置）；只在这里
        结算一次，调用方不得预先缩放，避免重复减半。
        """
        prev_owner = int(node.get("owner_faction", 0))
        node["owner_faction"] = int(faction_id)
        node["siege_state"] = {}
        # 玩家占领后据点不再是匪寨：清除 data.bandits / lair 标记，防止
        # 新主的城继续劫掠商路粮队、继续被匪寨 tick 抽成。
        await self._clear_bandit_marker(world, node)
        node["morale"] = 50
        node["defense"] = int(R.city_defense_max(node["level"]) * 0.2) \
            if node["type"] in ("city", "pass", "branch") else 0
        cur_gar = node.get("npc_garrison") or {}
        cur_troops = int(cur_gar.get("troops", 0) or 0)
        keep = max(0.0, min(1.0, float(garrison_keep)))
        # 保留兵力不得超过原守军：此前 max(200, 残军×0.5) 会让少量残军
        # 被凭空补到 200，攻下方白得一支比原守军还多的驻军。
        node["npc_garrison"] = {
            "troops": int(cur_troops * keep) if cur_troops > 0 else 0,
            "morale": 60,
        }
        # 新占节点保护期（§17：168h 免战，防滚雪球）
        pcfg = self.main_cfg().get("protection") or {}
        if prev_owner != int(faction_id):
            node["protect_until"] = _now() + int(pcfg.get("new_node_hours", 168)) * 3600
        # 驻守/围城的敌方军团溃散处理：城破后不能留下仍处 siege 的
        # 旧围城军（否则会把新主的城继续围住，甚至绕过新主保护期）
        for a in await self.get_armies(world["id"], node_id=node["id"],
                                       states=("idle", "siege")):
            if a["faction_id"] != faction_id:
                a["_routed"] = True
                # 同一 tick 内已因战败进入重整期的军团，_routed/_rout_penalized
                # 不落库，重新 get_armies 后只剩 reorganize_until_tick；
                # 若再次走处罚分支会重复 30% 折损/主将伤势/擒获判定。
                # reorganize_ticks=0 时连 until 都没有，因此把「已在撤退
                # 途中（march + path）」也视为已处罚，避免二次折损。
                if self._is_reorganizing(a, world) or (
                        a.get("state") == "march" and a.get("path")):
                    a["_rout_penalized"] = True
                await self._auto_retreat(a, int(faction_id))
        notes: list[str] = []
        # §9.1 仇恨记忆：记录攻占者，驻军重生后优先敌对
        if prev_owner > 0 and prev_owner != int(faction_id):
            hatred = node.get("hatred") or {}
            hatred[str(int(faction_id))] = int(
                hatred.get(str(int(faction_id)), 0) or 0) + 20
            node["hatred"] = hatred
        # §8.7 主城陷落：旧势力失去主城标记；已无其他据点则进入「流亡」
        if prev_owner > 0 and prev_owner != int(faction_id):
            old_f = await self.get_faction(prev_owner)
            if old_f and int(old_f.get("capital_node_id") or 0) == int(node["id"]):
                old_f["capital_node_id"] = 0
                rest = await self._fetchone(
                    "SELECT COUNT(*) AS c FROM strategy_nodes "
                    "WHERE world_id=? AND owner_faction=? AND id!=?",
                    (world["id"], prev_owner, int(node["id"])))
                if int((rest or {}).get("c", 0) or 0) <= 0:
                    old_f["faction_state"] = "exiled"
                    notes.append(f"🏛 {old_f['name']} 主城陷落，势力进入流亡状态")
                else:
                    notes.append(f"🏛 {old_f['name']} 主城陷落，需尽快迁都")
                await self.save_faction(old_f)
        # §6.2 秘境首占：首次占领 ruin → 30% 掉装备/图纸（写战报 + 通知）
        if node["type"] == "ruin" and prev_owner != int(faction_id):
            out = node.get("output") or {}
            if not int(out.get("first_captured", 0) or 0):
                out["first_captured"] = 1
                node["output"] = out
                rng = self._rng("ruin_capture", world["id"], node["id"],
                                world["tick_no"])
                if rng.random() < 0.3:
                    notes.append(await self._ruin_first_capture_reward(
                        world, node, int(faction_id)))
        await self.save_node(node)
        node["_capture_note"] = "；".join(n for n in notes if n)
        # §6.4 势力任务：开疆拓土 / 守土安民
        await self._faction_task_progress(int(faction_id), "conquer", 1)
        if prev_owner:
            await self._faction_task_progress(int(prev_owner), "defend", 1)
        # 关押于此的俘虏解放（§7.9）
        return await self.liberate_captives_at_node(world, node, int(faction_id))

    async def _ruin_first_capture_reward(self, world: dict, node: dict,
                                         faction_id: int) -> str:
        """秘境首占掉落（§6.2）：优先主玩法装备，缺主玩法上下文时折为军资。"""
        owner = await self._fetchone(
            "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=? "
            "LIMIT 1", (world["id"], int(faction_id)))
        user_id = str((owner or {}).get("user_id") or "")
        if user_id:
            try:
                got = await self._grant_main_equipment(user_id)
                await self._notify(world, [user_id], "event",
                                   {"text": f"💎 首占秘境「{node['name']}」"
                                            f"获得装备「{got.get('name', '')}」"})
                return f"💎 首占秘境掉落装备「{got.get('name', '')}」"
            except Exception:  # noqa: BLE001 - 无主玩法上下文时降级
                pass
        p = await self._fetchone(
            "SELECT * FROM strategy_players WHERE world_id=? AND faction_id=? "
            "LIMIT 1", (world["id"], int(faction_id)))
        player = self._decode("strategy_players", p) if p else None
        if player:
            player["iron"] = int(player.get("iron", 0) or 0) + 300
            await self.save_player(player)
        if user_id:
            await self._notify(world, [user_id], "event",
                               {"text": f"💎 首占秘境「{node['name']}」获得图纸残卷"
                                        f"（精铁 +300）"})
        return "💎 首占秘境获得图纸残卷（精铁 +300）"

    async def _release_army_chars(self, army: dict):
        """军团解散时归还角色：保留陨落/被俘状态，其余恢复空闲。"""
        for cid in army.get("characters") or []:
            c = await self.get_char(int(cid))
            if not c or c.get("status") in ("dead", "captive"):
                continue
            c["status"] = "dead" if R.injury_state(
                c.get("injury", 0)) == "dead" else "idle"
            await self.save_char(c)

    async def _auto_retreat(self, army: dict, pursuer_faction: int = 0,
                            pursuer_traits: Optional[list] = None,
                            pursuer_cavalry: bool = False):
        """战败/超时/断粮自动撤往最近己方据点；无路可退或已无兵则解散。

        §7.4 溃败善后：溃逃军团额外折损 ``battle.rout_troops_pct``、士气压至
        ``battle.rout_morale``、主将按 ``rout_leader_injury_chance`` 负伤，并
        进入 ``reorganize_ticks`` tick 的重整期；追击方可用 ``R.pursuit_capture``
        截获溃逃主将（转 captive）。
        """
        bcfg = self.main_cfg().get("battle") or {}
        if army.get("_routed") and not army.get("_rout_penalized"):
            # 只罚一次：同一次溃败可能在会战/围城/追击多处调用 _auto_retreat
            army["_rout_penalized"] = True
            pct = float(bcfg.get("rout_troops_pct", 0.3) or 0.0)
            if pct > 0:
                army["troops"] = max(
                    0, int(int(army.get("troops", 0)) * (1 - min(0.95, pct))))
            morale_cap = int(bcfg.get("rout_morale", 30) or 30)
            army["morale"] = min(int(army.get("morale", 100) or 0), morale_cap)
            cids = [int(x) for x in (army.get("characters") or [])]
            rng = self._rng("rout", int(army.get("id", 0)), _now() // 60)
            # 主将负伤
            if cids and rng.random() < float(
                    bcfg.get("rout_leader_injury_chance", 0.3) or 0.0):
                leader = await self.get_char(cids[0])
                if leader and leader.get("status") not in ("dead", "captive"):
                    dead = R.apply_injury(
                        leader, int(bcfg.get("rout_leader_injury", 15) or 0))
                    if dead:
                        leader["status"] = "dead"
                    await self.save_char(leader)
            # 追击截获：仅在有明确追击方时判定
            if int(pursuer_faction or 0) > 0 and cids and \
                    bool(bcfg.get("pursuit_capture", True)):
                traits = list(pursuer_traits or [])
                pf = await self.get_faction(int(pursuer_faction))
                intercept = float(R.tech_effects(
                    (pf or {}).get("tech") or {}).get("intercept_bonus", 0.0))
                if R.pursuit_capture(
                        rng, cavalry=bool(pursuer_cavalry),
                        swift="swift" in traits,
                        base=0.25 + intercept):
                    leader = await self.get_char(cids[0])
                    if leader and leader.get("status") not in ("dead", "captive"):
                        # 走统一关押入口：校验关押位并移出军团名册；位满则
                        # 就地驱逐（忠诚 -5）。此前直接置 captive，后续
                        # roll_captures 会对其二次掷点并可能"释放"刚俘角色。
                        # 关押点必须是追击方自己的据点：战场节点通常是守方
                        # 的城，直接用它会把擒获的敌将关进敌方牢里。
                        pfid = int(pursuer_faction)
                        jail = int(army.get("node_id", 0) or 0)
                        jail_node = await self.get_node(
                            int(army["world_id"]), jail) if jail > 0 else None
                        if not jail_node or int(jail_node.get("owner_faction", 0)) != pfid:
                            jail = await self._nearest_own_node(
                                {"id": int(army["world_id"])},
                                {"faction_id": pfid}, int(army.get("node_id", 0) or 0))
                        if not jail:
                            logger.info("[strategy] 追击方无己方据点可关押 %s，跳过擒获",
                                        leader.get("name"))
                        else:
                            got = await self._capture_char(leader, pfid, int(jail))
                            if got:
                                army["_leader_captured"] = True
                            # 同步剔除本地名册，避免末尾 save_army 把已移出的
                            # 角色重新写回 characters_json。
                            army["characters"] = [
                                x for x in (army.get("characters") or [])
                                if int(x) != int(cids[0])]
            ticks = int(bcfg.get("reorganize_ticks", 0) or 0)
            if ticks > 0:
                world = await self.get_world()
                army["reorganize_until_tick"] = int(
                    (world or {}).get("tick_no", 0) or 0) + ticks
        if int(army.get("troops", 0)) <= 0:
            # 0 兵军团不能继续占用并发名额，也不能无限滞留地图
            await self._release_army_chars(army)
            await self.delete_army(army["id"])
            return
        home = await self._nearest_own_node(
            {"id": army["world_id"]}, {"faction_id": army["faction_id"]}, army["node_id"])
        if home is None:
            await self._release_army_chars(army)
            await self.delete_army(army["id"])
            return
        was_marching = army.get("state") == "march"
        path = await self.find_path(army["world_id"], army["node_id"], home)
        army["state"] = "march"
        army["path"] = (path or [home])[1:] if path else []
        if not was_marching:
            # 断粮撤退途中不要每 tick 清零进度，否则慢速军团永远走不回家
            army["edge_pos"] = 0.0
        if not army["path"]:
            army["state"] = "idle"
            army["edge_pos"] = 0.0
        await self.save_army(army)

    # --- 步骤 4：行军推进 + 粮耗（§5.6）------------------------------------
    async def _step_march(self, world: dict):
        marching = await self.get_armies(world["id"], states=("march",))
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        for a in marching:
            if not a["path"]:
                a["state"] = "idle"
                await self.save_army(a)
                continue
            nxt_id = a["path"][0]
            nxt = nodes.get(nxt_id)
            if not nxt:
                a["path"] = []
                a["state"] = "idle"
                await self.save_army(a)
                continue
            terrain = nxt.get("terrain", "plain")
            leader = None
            cids = a.get("characters") or []
            if cids:
                leader = await self.get_char(int(cids[0]))
            mods = R.trait_army_mods((leader or {}).get("traits"))
            a["edge_pos"] = float(a.get("edge_pos", 0)) + \
                R.march_speed(a, terrain) * float(mods.get("speed_mult", 1.0))
            cost = R.TERRAIN_MARCH_COST.get(terrain, 1.0)
            # 先推进再结算粮耗：断粮触发撤退时本 tick 的行军进度不会白费
            if a["edge_pos"] >= cost:
                a["edge_pos"] -= cost
                a["node_id"] = nxt_id
                a["path"] = a["path"][1:]
            # 粮耗（行军中每 tick；疾行 -10% / 动员令 / 农政科技 grain_cost_mult）
            faction = await self.get_faction(int(a["faction_id"]))
            tech_grain = float(R.tech_effects(
                (faction or {}).get("tech") or {}).get("grain_cost_mult", 1.0))
            far = len(a["path"]) > int((self.main_cfg().get("army") or {}).get(
                "far_supply_steps", 2))
            upkeep = R.grain_upkeep(a, terrain, far) * \
                float(mods.get("grain_mult", 1.0)) * tech_grain * \
                await self._faction_buff(int(a["faction_id"]), "grain_cost_mult")
            upkeep = max(0, int(round(upkeep)))
            acfg = self.main_cfg().get("army") or {}
            if a["grain"] < upkeep:
                a["grain"] = 0
                # 农政科技 L5「屯粮」：断粮抵抗 tick 数（期间只扣士气、不逃兵）
                resist = int(R.tech_effects(
                    (faction or {}).get("tech") or {}).get("starve_resist_ticks", 0))
                starve_n = int(a.get("starve_ticks", 0) or 0) + 1
                a["starve_ticks"] = starve_n
                a["morale"] = max(0, a["morale"] - int(acfg.get("starve_morale_per_tick", 20)))
                desert = 0 if starve_n <= resist else int(
                    a["troops"] * float(acfg.get("starve_desert_pct", 0.05)))
                a["troops"] = max(0, a["troops"] - desert)
                if a["morale"] <= 0 or a["troops"] <= 0:
                    a["starve_ticks"] = 0
                    await self._auto_retreat(a)
                    continue
            else:
                a["grain"] -= upkeep
                a["starve_ticks"] = 0
            await self.save_army(a)
        await self._commit()

    # --- 步骤 8：恢复（§4.6/§5.3）------------------------------------------
    async def _step_recovery(self, world: dict):
        icfg = self.main_cfg().get("injury") or {}
        scfg = self.main_cfg().get("stamina") or {}
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        infirmary_nodes: dict[int, int] = {}
        for nid, node in nodes.items():
            lvl = min(5, self._building_level(node, "infirmary"))
            if lvl > 0:
                infirmary_nodes[nid] = lvl
        # 角色恢复
        rows = await self._fetchall(
            "SELECT * FROM strategy_characters WHERE world_id=? AND status!='dead'",
            (world["id"],))
        taishou_nodes: set[int] = set()
        for row in rows:
            c = self._decode("strategy_characters", row)
            # 只有在本势力任上、且仍正常履职（未被俘/押运/出走）的太守才
            # 给辖地加成；否则被俘太守会继续给原城加成，甚至给已易主的
            # 敌方城市 +2 民心。
            if str(c.get("appointed_position") or "") == "taishou" and \
                    int(c.get("appointed_node_id") or 0) > 0 and \
                    c.get("status") not in ("dead", "captive", "transfer", "wander"):
                tnid = int(c.get("appointed_node_id") or 0)
                owner = nodes.get(tnid)
                if owner is not None and int(owner.get("owner_faction", 0)) == \
                        int(c.get("faction_id", 0) or 0):
                    taishou_nodes.add(tnid)
            per = int(icfg.get("recover_per_tick", 5))
            loc_node = int((c.get("location") or {}).get("node_id", 0) or 0)
            if loc_node in infirmary_nodes:
                per = int(icfg.get("infirmary_recover", 10)) + \
                    5 * (int(infirmary_nodes[loc_node]) - 1)
            if c["status"] == "idle":
                R.recover_injury(c, per)
            R.recover_stamina(c, int(scfg.get("recover_per_tick", 10))
                              if scfg.get("enabled", True) else 0)
            await self.save_char(c)
        # 据点：城防自修 + 士气回归 + 瘟疫类 DOT
        active = await self.get_active_events(world["id"], world.get("tick_no", 0))
        gcfg = self.main_cfg().get("governance") or {}
        tick_now = int(world.get("tick_no", 0) or 0)
        # 只保留本 tick 标记：既保证同 tick 去重，又避免 world.data 膨胀
        riot_map = {k: int(v) for k, v in
                    ((world.get("data") or {}).get("riot_tick_nodes") or {}).items()
                    if int(v or 0) == tick_now}
        for nid, node in nodes.items():
            if node["owner_faction"] != 0 and not node.get("siege_state"):
                if node["defense"] > 0:
                    node["defense"] = R.city_self_repair(node["defense"], node["level"])
                # 太守辖地：民心恢复 ×2（§4.5 官职表）；擂将台每级 +1（§6.6）
                step = 2 if nid in taishou_nodes else 1
                step += min(5, self._building_level(node, "arena"))
                node["morale"] = min(self._morale_cap(node), node["morale"] + step) \
                    if node["morale"] < 80 else max(80, node["morale"] - 1)
                # §8.6 哗变：民心 < riot_morale 每 tick 按 riot_chance 判定
                # 同一 tick 已被势力事件「哗变」命中的据点跳过，防双重结算
                if riot_map.get(str(nid)) == int(world.get("tick_no", 0) or 0):
                    pass
                else:
                    riot_rng = self._rng("riot", world["id"], nid,
                                         world.get("tick_no", 0))
                    if R.riot_check(riot_rng, int(node.get("morale", 0)), self.main_cfg()):
                        pct = float(gcfg.get("riot_troops_pct", 0.3) or 0)
                        gar = node.get("npc_garrison") or {}
                        if int(gar.get("troops", 0) or 0) > 0:
                            gar["troops"] = max(
                                0, int(int(gar["troops"]) * (1 - min(1.0, pct))))
                            node["npc_garrison"] = gar
                        await self._bump_morale(node, 5, reason="riot")
                        await self._riot_injure_char(world, node, riot_rng)
                        node["_riot_note"] = f"「{node['name']}」发生哗变"
                        riot_map[str(nid)] = int(world.get("tick_no", 0) or 0)
            for ev in active:
                for eff in (ev.get("data") or {}).get("effects") or []:
                    if eff.get("type") == "node_dot" and nid in (
                            (ev.get("target") or {}).get("node_ids") or []):
                        gar = node.get("npc_garrison") or {}
                        pct = float(eff.get("troops_pct", 0)) / 100
                        gar["troops"] = max(0, int(gar.get("troops", 0) * (1 + pct)))
                        node["npc_garrison"] = gar
                        node["morale"] = max(0, node["morale"] + int(eff.get("morale", 0)))
            # 己方军团驻守士气恢复
            await self.save_node(node)
        for a in await self.get_armies(world["id"], states=("idle",)):
            node = nodes.get(a["node_id"])
            if node and node["owner_faction"] == a["faction_id"]:
                a["morale"] = min(100, a["morale"] + 10)
            # §7.5 超编：兵力 > 统率×20 时每 tick 扣士气（与驻守恢复独立）
            leader = None
            cids = a.get("characters") or []
            if cids:
                leader = await self.get_char(int(cids[0]))
            penalty = R.overstaff_penalty(leader, int(a.get("troops", 0)),
                                          self.main_cfg())
            if penalty > 0:
                a["morale"] = max(0, a["morale"] - penalty)
            await self.save_army(a)
        data = world.get("data") or {}
        data["riot_tick_nodes"] = riot_map
        world["data"] = data
        await self._commit()

    async def _riot_injure_char(self, world: dict, node: dict,
                                rng: random.Random):
        """哗变时随机一名驻守角色受伤（§8.6）。"""
        rows = await self._fetchall(
            "SELECT * FROM strategy_characters WHERE world_id=? AND faction_id=? "
            "AND status='idle' LIMIT 50",
            (world["id"], int(node.get("owner_faction", 0) or 0)))
        if not rows:
            return
        c = self._decode("strategy_characters", rng.choice(rows))
        dead = R.apply_injury(c, 10)
        if dead:
            c["status"] = "dead"
        await self.save_char(c)

    # ------------------------------------------------------------------
    # 步骤 5：抵达 → 会战 / 占领 / 转入围城（§7.1）
    # ------------------------------------------------------------------
    async def _step_arrivals(self, world: dict) -> list[str]:
        reports: list[str] = []
        marching = await self.get_armies(world["id"], states=("march",))
        arrived: dict[tuple[int, int], list[dict]] = {}
        for a in marching:
            if a["path"]:
                continue
            arrived.setdefault((a["node_id"], a["faction_id"]), []).append(a)
        for (nid, _fid), attackers in arrived.items():
            node = await self.get_node(world["id"], nid)
            if not node:
                for a in attackers:
                    a["state"] = "idle"
                    await self.save_army(a)
                continue
            if node["owner_faction"] == attackers[0]["faction_id"]:
                for a in attackers:
                    a["state"] = "idle"
                    a["edge_pos"] = 0.0
                    await self.save_army(a)
                continue
            # 手操战役进行中：不得重复自动结算守军（守军已在手操战斗中被消耗），
            # 抵达援军暂退回最近己方据点，待手操结束后再行出击
            if await self._active_manual_battle(world["id"], node["id"]):
                for a in attackers:
                    await self._auto_retreat(a)
                reports.append(f"⏸「{node['name']}」手操会战进行中，"
                               f"途经军团暂退待命")
                continue
            report = await self._attack_node(world, node, attackers)
            if report:
                reports.append(report)
        await self._commit()
        return reports

    async def _attack_node(self, world: dict, node: dict, attackers: list[dict]) -> str:
        atk_fid = attackers[0]["faction_id"]
        atk_f = await self.get_faction(int(atk_fid))
        dfn_f = await self.get_faction(int(node.get("owner_faction", 0)))
        atk_host = await self._alliance_host(atk_f) if atk_f else None
        dfn_host = await self._alliance_host(dfn_f) if dfn_f else None
        if atk_host and dfn_host and int(atk_host["id"]) == int(dfn_host["id"]):
            for a in attackers:
                a["state"] = "idle"
                a["edge_pos"] = 0.0
                await self.save_army(a)
            return ""
        # 保护期复查：行军途中目标可能刚被他人夺取并获得 168h 免战，
        # 抵达时须重新判定，否则保护期形同虚设
        if int(node.get("owner_faction", 0)) > 0 and \
                int(node.get("protect_until", 0) or 0) > _now():
            for a in attackers:
                await self._auto_retreat(a)
            return f"🛡️「{node['name']}」处于保护期，攻方撤军"
        # 撕毁停战/背盟判定（§6.3）
        owner_fid = int(node.get("owner_faction", 0) or 0)
        # 外交关系按「联盟盟主势力」记账：成员国据点的归属方是其自身
        # faction_id，而 atk_host.diplomacy 的键是盟主 id。若不换成
        # dfn_host，结盟/停战关系全部查不到，每次攻城都会被误判成突袭。
        foe_fid = int(dfn_host["id"]) if dfn_host else owner_fid
        rel_before = (R.relation_of(atk_host.get("diplomacy") or {}, foe_fid, _now())
                      if atk_host and foe_fid > 0 else R.REL_NEUTRAL)
        await self.check_truce_violation(world, int(atk_fid), owner_fid)
        # 未宣战直接突袭：好感下降并引发 NPC 盟友/敌人的连锁表态
        if atk_host and dfn_host and owner_fid > 0 and \
                int(atk_host["id"]) != int(dfn_host["id"]) and \
                rel_before not in (R.REL_WAR, "war_pending",
                                   R.REL_TRUCE, R.REL_ALLY):
            await self._favor_on_war(atk_host, dfn_host, surprise=True)
        # 守方：驻守军团 + 守军（残军/征召兵）+ 正在围城的其他势力。
        # 与 intervene_siege 同口径：攻方同盟/同联盟的驻军不视为守方，
        # 否则会把盟友拉进自动会战（并给盟友误发 battle_open 通知）。
        atk_host_id = int((atk_host or {}).get("id") or 0)
        defenders: list[dict] = []
        for a in await self.get_armies(world["id"], node_id=node["id"],
                                       states=("idle", "siege")):
            if a["faction_id"] == atk_fid:
                continue
            if atk_host_id and int(a["faction_id"]) != owner_fid:
                af = await self.get_faction(int(a["faction_id"]))
                ah = await self._alliance_host(af) if af else None
                if ah and int(ah["id"]) == atk_host_id:
                    continue
            defenders.append(a)
        garrison_troops = int((node.get("npc_garrison") or {}).get("troops", 0))
        unowned = node["owner_faction"] == 0
        if not defenders and (garrison_troops <= 0 or unowned and garrison_troops <= 0):
            if unowned:
                await self._capture_node(world, node, atk_fid)
                note = node.get("_capture_note") or ""
                return (f"🚩「{node['name']}」守备空虚，被 "
                        f"{await self._faction_name(atk_fid)} 直接占领"
                        + (f"\n{note}" if note else ""))
            if node["defense"] > 0:
                for a in attackers:
                    await self._begin_siege(world, node, a)
                return f"⛺「{node['name']}」无兵驻守但城防尚存，攻方转入围城"
            await self._capture_node(world, node, atk_fid)
            note = node.get("_capture_note") or ""
            return (f"🚩「{node['name']}」被 {await self._faction_name(atk_fid)} 占领"
                    + (f"\n{note}" if note else ""))
        # 会战
        atk_sim, dfn_sim, gar_sim = [], [], None
        node_taishou = any(
            str(c.get("appointed_position") or "") == "taishou" and
            int(c.get("appointed_node_id") or 0) == int(node["id"]) and
            c.get("status") not in ("dead", "captive", "transfer", "wander")
            for c in await self.get_chars(
                world_id=world["id"], faction_id=int(node.get("owner_faction") or 0)))
        for a in attackers:
            s = await self._army_sim(a)
            if s:
                atk_sim.append(s)
        # §8.6 动员令：攻方攻击加成（兵学科技 atk_bonus 已在 _army_sim 内计入）
        atk_buff = await self._faction_buff(int(atk_fid), "attack_bonus")
        if atk_buff:
            for s in atk_sim:
                s["pos_atk_mult"] = float(s.get("pos_atk_mult", 1.0)) * (1 + atk_buff)
        # §8.6 动员令：守方城防加成
        dfn_buff = await self._faction_buff(
            int(node.get("owner_faction", 0) or 0), "defense_bonus")
        for a in defenders:
            s = await self._army_sim(a)
            if s:
                s["wall_mult"] = 1.0 + min(0.3, node["defense"] / R.city_defense_max(
                    max(1, node["level"])) * 0.3)
                s["wall_mult"] *= (1 + dfn_buff)
                if node_taishou:
                    s["wall_mult"] *= 1.10  # 太守辖地城防 +10%
                dfn_sim.append(s)
        if garrison_troops > 0:
            gar_sim = self._garrison_sim(node)
            if unowned:
                gar_sim["def_terrain_mult"] = 1.1  # 残军据险
            if node_taishou:
                gar_sim["wall_mult"] = 1.10
            dfn_sim.append(gar_sim)
        if not atk_sim:
            for a in attackers:
                await self._auto_retreat(a)
            return ""
        if not dfn_sim:
            if unowned or node["defense"] <= 0:
                await self._capture_node(world, node, atk_fid)
                note = node.get("_capture_note") or ""
                return (f"🚩「{node['name']}」被 {await self._faction_name(atk_fid)} 占领"
                        + (f"\n{note}" if note else ""))
            for a in attackers:
                await self._begin_siege(world, node, a)
            return ""
        cfg = self.main_cfg()
        rng = self._rng("battle", world["id"], node["id"], world["tick_no"],
                        *[a["id"] for a in attackers])
        result = R.simulate_battle(
            atk_sim, dfn_sim, rng,
            max_rounds=int((cfg.get("battle") or {}).get("max_rounds", 30)),
            night=R.is_night(time.localtime().tm_hour),
            defending_node=True, city_defense=node["defense"])
        report = R.render_battle_report(result, title=f"{node['name']}会战")
        await self._persist_battle(world, node, atk_fid, node["owner_faction"],
                                   "field", result, report, atk_sim, dfn_sim)
        await self._apply_battle_aftermath(world, node, attackers, defenders, result)
        if result["winner"] == "attacker":
            survivors = [a for a in attackers
                         if a.get("troops", 0) > 0 and not a.get("_routed")
                         and not a.get("_destroyed")]
            if not survivors:
                return report
            if unowned:
                await self._capture_node(world, node, atk_fid)
                report += f"\n🚩「{node['name']}」被 {await self._faction_name(atk_fid)} 占领！"
                if node.get("_capture_note"):
                    report += f"\n{node['_capture_note']}"
            elif node["defense"] > 0:
                # 全部幸存军团一起转入围城：只让第一支围城会让其余军团
                # 下 tick 再次触发会战（重复攻打同一座城）
                for s in survivors:
                    await self._begin_siege(world, node, s)
                report += f"\n⛺ 攻方获胜，「{node['name']}」城防 {node['defense']}，转入围城"
            else:
                await self._capture_node(world, node, atk_fid)
                report += f"\n🚩「{node['name']}」被 {await self._faction_name(atk_fid)} 占领！"
                if node.get("_capture_note"):
                    report += f"\n{node['_capture_note']}"
        else:
            # 守方胜：被歼灭/溃逃的攻方军团进入被俘判定（与围城反击
            # wipeout 同口径；否则自动野战守方永远擒不到攻方将领）
            lost_atk = [a for a in attackers
                        if a.get("_destroyed") or a.get("_routed")]
            dfn_fid = int(node.get("owner_faction", 0) or 0)
            if lost_atk and dfn_fid > 0:
                got = await self.roll_captures(
                    world, node, lost_atk, "wipeout", dfn_fid)
                if got:
                    report += f"\n⛓ 攻将被擒：{'、'.join(got)}"
            report += f"\n🛡️「{node['name']}」守住了攻势"
        return report

    async def _begin_siege(self, world: dict, node: dict, army: dict):
        army["state"] = "siege"
        army["edge_pos"] = 0.0
        army["path"] = []
        ss = node.get("siege_state") or {}
        # 围城易手（原攻方被击溃、新势力接管）：重置进度与归属，
        # 否则 plot 的「围城」判定会继续算在旧势力头上
        if ss and int(ss.get("attacker_faction") or 0) != int(army["faction_id"]):
            ss = {}
        fresh = not ss
        if fresh:
            ss = {"ticks": 0, "attacker_faction": army["faction_id"],
                  "choice": "auto", "since": _now()}
        node["siege_state"] = ss
        await self.save_army(army)
        await self.save_node(node)
        if fresh and int(node.get("owner_faction", 0)) > 0:
            # 己方被攻通知（§12.5 siege_alert，中档及以上）
            rows = await self._fetchall(
                "SELECT user_id FROM strategy_players "
                "WHERE world_id=? AND faction_id=?",
                (world["id"], int(node["owner_faction"])))
            await self._notify(world, [r["user_id"] for r in rows], "siege_alert",
                               {"node_id": int(node["id"]), "node_name": node["name"],
                                "attacker_faction": int(army["faction_id"])})

    async def _faction_name(self, faction_id: int) -> str:
        if faction_id == -1:
            return "魔潮大军"
        if faction_id == 0:
            return "无主守军"
        f = await self.get_faction(faction_id)
        return (f or {}).get("name", f"势力#{faction_id}")

    # ------------------------------------------------------------------
    # 战斗模拟辅助
    # ------------------------------------------------------------------
    async def _army_sim(self, army: dict) -> Optional[dict]:
        """strategy_armies 行 → simulate_battle 输入 dict。"""
        if int(army.get("troops", 0)) <= 0:
            return None
        leader = None
        for cid in army.get("characters") or []:
            c = await self.get_char(int(cid))
            if not c or c.get("status") in ("captive", "dead") or \
                    R.injury_state(c.get("injury", 0)) in ("dying", "dead"):
                continue  # C-M4：被俘/陨落者不领队，顺延下一位军中角色
            leader = c
            break
        f = await self.get_faction(int(army.get("faction_id") or 0))
        pos_bonus = await self._faction_position_bonus(f) if f else {}
        tech = R.tech_effects((f or {}).get("tech") or {})
        return {
            "id": int(army["id"]), "faction_id": int(army.get("faction_id", 0)),
            "troop_type": army.get("troop_type", "sword_shield"),
            "troops": int(army["troops"]), "morale": int(army.get("morale", 100)),
            "veteran": int(army.get("veteran", 0)), "leader": leader,
            "siege_engine": bool(army.get("siege_engine", 0)),
            "pos_atk_mult": 1.0 + float(pos_bonus.get("army_atk", 0.0))
            + float(tech.get("atk_bonus", 0.0))
            + float(tech.get("flank_bonus", 0.0)),
        }

    def _garrison_sim(self, node: dict) -> Optional[dict]:
        """据点守军（残军/征召兵）→ 伪军团（id = -node_id）。"""
        gar = node.get("npc_garrison") or {}
        troops = int(gar.get("troops", 0))
        if troops <= 0:
            return None
        return {
            "id": -int(node["id"]), "faction_id": int(node.get("owner_faction", 0)),
            "troop_type": "sword_shield", "troops": troops,
            "morale": int(gar.get("morale", 80)), "veteran": 0, "leader": None,
            "siege_engine": False,
        }

    async def _persist_battle(self, world: dict, node: dict, atk_fid: int,
                              dfn_fid: int, kind: str, result: dict, report: str,
                              atk_sim: list, dfn_sim: list):
        round_log = result.get("round_log") or []
        # §12.6 高光标记：单挑/伏兵/夜袭/以少胜多/合围
        result["highlights"] = self._battle_highlights(result)
        snapshot = [{"side": "atk", **{k: s.get(k) for k in
                                       ("id", "troop_type", "troops", "morale")}}
                    for s in atk_sim] + \
                   [{"side": "dfn", **{k: s.get(k) for k in
                                       ("id", "troop_type", "troops", "morale")}}
                    for s in dfn_sim]
        slim = {k: v for k, v in result.items() if k != "round_log"}
        slim["round_log"] = round_log[-10:]
        await self.insert_battle({
            "world_id": world["id"], "node_id": node["id"],
            "attacker_faction": atk_fid, "defender_faction": dfn_fid,
            "kind": kind, "mode": "auto", "state": "done",
            "round_no": int(result.get("rounds", 0)),
            "map_seed": f"{world['tick_no']}-{node['id']}",
            "armies_snapshot": snapshot, "result": slim,
            "report_text": report[:8000], "created_at": _now(),
        })

    def _battle_highlights(self, result: dict,
                           session: Optional[dict] = None) -> list[str]:
        """战报高光（§12.6）：单挑 / 伏兵 / 夜袭 / 合围 / 以少胜多。"""
        out: list[str] = []
        sess = session or {}
        if result.get("duel_log"):
            first = str(result["duel_log"][0])
            out.append(f"⭐ 单挑决胜：{first[:40]}")
        events = sess.get("events") or []
        if any(e.get("kind") == "ambush" for e in events):
            out.append("⭐ 伏兵突袭得手")
        if result.get("night") or sess.get("night"):
            out.append("⭐ 夜袭作战")
        if any(e.get("kind") == "encircle" for e in events):
            out.append("⭐ 合围破阵")
        try:
            atk = sum(int(a.get("troops_before", a.get("troops", 0)) or 0)
                      for a in (result.get("attackers") or []))
            dfn = sum(int(d.get("troops_before", d.get("troops", 0)) or 0)
                      for d in (result.get("defenders") or []))
            won = str(result.get("winner") or "")
            if won == "attacker" and 0 < atk <= dfn * 0.8:
                out.append("⭐ 以少胜多")
            elif won == "defender" and 0 < dfn <= atk * 0.8:
                out.append("⭐ 以少胜多")
        except (TypeError, ValueError):
            pass
        return out

    async def _pursuit_context(self, army: dict, attackers: list[dict],
                               defenders: list[dict]) -> tuple[int, list, bool]:
        """溃逃方 → 追击方上下文（势力/主将特技/是否飞剑队）。§7.4"""
        is_attacker = any(a is army for a in attackers)
        pool = defenders if is_attacker else attackers
        for other in pool or []:
            if not other or int(other.get("troops", 0) or 0) <= 0:
                continue
            # 己方（追击方）同样可能已溃逃：溃军不能再追击/截获敌将。
            if other.get("_routed") or other.get("_destroyed"):
                continue
            traits: list = []
            cids = other.get("characters") or []
            if cids:
                leader = await self.get_char(int(cids[0]))
                traits = list((leader or {}).get("traits") or [])
            return (int(other.get("faction_id", 0) or 0), traits,
                    str(other.get("troop_type") or "") == "flying_sword")
        return 0, [], False

    async def _army_members(self, army: dict) -> list[dict]:
        """军团角色列表（跳过已陨落/被俘）。"""
        out = []
        for cid in army.get("characters") or []:
            c = await self.get_char(int(cid))
            if c and c.get("status") not in ("dead", "captive"):
                out.append(c)
        return out

    async def _apply_battle_aftermath(self, world: dict, node: dict,
                                      attackers: list[dict], defenders: list[dict],
                                      result: dict,
                                      hold_atkers: bool = False):
        """把模拟结果写回军团/守军/角色（伤亡、溃逃、覆没、伤势、战功、精锐）。

        ``hold_atkers``：守方获胜时暂不把幸存攻方自动撤离。手操围城介入
        （kind='siege'）由调用方在原地恢复围城，需要残部留在城下；自动
        野战/巷战保持默认撤离，避免滞留敌节点反复触发会战。
        """
        cfg = self.main_cfg()
        bcfg = cfg.get("battle") or {}
        rows = {r["id"]: r for r in
                (result.get("attackers") or []) + (result.get("defenders") or [])}
        atk_ids = {id(a) for a in attackers}
        winner = result.get("winner")
        all_armies = list(attackers) + [d for d in defenders if d]
        # 预标记溃逃/覆没：_pursuit_context 依据 _routed/_destroyed 排除已
        # 失去战斗力的追击方，但主循环按「攻方 → 守方」顺序处理，结算攻方
        # 溃军时守方尚未轮到、标记仍为空，会把同样溃逃的守军当成追击方
        # （可截获敌将）。这里先从战果行统一预标记，与后续分支写回同一口径。
        for army in all_armies:
            row = rows.get(int(army["id"]))
            if not row:
                continue
            if int(row.get("troops_after", 0) or 0) <= 0:
                army["_destroyed"] = True
            elif row.get("routed"):
                army["_routed"] = True
        # 军团兵力/士气写回 + 溃逃/覆没善后
        for army in all_armies:
            row = rows.get(int(army["id"]))
            if not row:
                continue
            is_attacker = id(army) in atk_ids
            army["troops"] = int(row["troops_after"])
            # §4.5 羁绊士气：同道/义交/师徒给整队补士气（最高 +20）
            mates_bonus = R.bond_army_morale_bonus(
                [c for c in (await self._army_members(army))])
            army["morale"] = min(100, int(row["morale"]) + mates_bonus)
            army["veteran"] = min(R.VETERAN_MAX, int(army.get("veteran", 0))
                                  + R.veteran_gain(
                winner == ("attacker" if is_attacker else "defender")))
            # 全军团角色阅历：主将全额，随行角色按比例成长（可突破生成上限）
            cids = [int(x) for x in (army.get("characters") or [])]
            won = winner == ("attacker" if is_attacker else "defender")
            kills = int(result.get("dfn_losses" if is_attacker else "atk_losses", 0))
            exp_win = int(bcfg.get("exp_win_base", 25))
            exp_lose = int(bcfg.get("exp_lose_base", 10))
            exp_div = max(1, int(bcfg.get("exp_kill_divisor", 10)))
            member_ratio = float(bcfg.get("member_exp_ratio", 0.5))
            leader_exp = (kills // exp_div + exp_win) if won else exp_lose
            growth_cap = self._growth_cap_bonus()
            for idx, cid in enumerate(cids):
                cc = await self.get_char(cid)
                if not cc or cc.get("status") == "dead":
                    continue
                amount = leader_exp if idx == 0 else max(
                    1, int(round(leader_exp * member_ratio)))
                R.gain_exp(cc, amount, growth_cap)
                if idx == 0 and army.get("owner_user_id"):
                    # 战功（仅玩家军团主将结算一次）
                    merit = R.merit_from_battle(
                        kills // max(1, len([x for x in all_armies
                                             if (id(x) in atk_ids) == is_attacker])),
                        won, False,
                        float(bcfg.get("merit_per_kill", 0.05)),
                        int(bcfg.get("win_merit_base", 100)))
                    if merit > 0:
                        merit = await self._settle_merit(
                            army["owner_user_id"], merit, "battle")
                        pp = await self.get_player(army["owner_user_id"], world["id"])
                        if pp and merit > 0:
                            pp["merit"] += merit
                            pp["merit_season"] += merit
                            await self.save_player(pp)
                await self.save_char(cc)
            # §7.4 羁绊演化：共同作战累积 → 同道 / 义交 / 师徒
            mates = await self._army_members(army)
            for i in range(len(mates)):
                for j in range(i + 1, len(mates)):
                    R.bond_apply_battle(mates[i], mates[j], won)
                    await self.save_char(mates[i])
                    await self.save_char(mates[j])
            if army["troops"] <= 0:
                # 覆没：主将伤势 +40，军团解散（被俘 P1）
                army["_destroyed"] = True
                for cid in cids:
                    c = await self.get_char(int(cid))
                    if not c:
                        continue
                    dead = R.apply_injury(c, 40)
                    c["status"] = "dead" if dead else "idle"
                    c["location"] = {"node_id": node["id"]}
                    await self.save_char(c)
                await self.delete_army(army["id"])
            elif row.get("routed"):
                army["_routed"] = True
                # 主动撤退（do_retreat 已按 80% 结算并清零士气）只做撤离，
                # 不再套用溃败善后的兵力再扣 30% / 主将负伤 / 追击截获；
                # 否则玩家主动撤出实际只保留 56%，与「保留 80%」冲突。
                if row.get("voluntary_retreat"):
                    army["_rout_penalized"] = True
                await self.save_army(army)
                pf, traits, cav = await self._pursuit_context(
                    army, attackers, defenders)
                await self._auto_retreat(army, pf, traits, cav)
            else:
                await self.save_army(army)
                if is_attacker and winner == "defender" and not hold_atkers:
                    # A-M10：战败且未溃逃的幸存攻方自动撤退；否则滞留敌方
                    # 节点（march+空 path），下 tick _step_arrivals 再触发会战
                    # → 无限攻打循环。手操围城（hold_atkers）例外：由
                    # _finalize_manual_battle_inner 就地恢复围城。
                    pf, traits, cav = await self._pursuit_context(
                        army, attackers, defenders)
                    await self._auto_retreat(army, pf, traits, cav)
        # 主将险情（兵力 <20% 被集火）
        for ev in result.get("peril_events") or []:
            for army in all_armies:
                if int(army["id"]) == int(ev.get("army_id", 0)):
                    cids = army.get("characters") or []
                    if cids:
                        c = await self.get_char(int(cids[0]))
                        if c:
                            dead = R.apply_injury(c, int(ev.get("injury", 15)))
                            if dead:
                                c["status"] = "dead"
                            await self.save_char(c)
                    break
        # 守军写回
        gar_row = rows.get(-int(node["id"]))
        if gar_row:
            gar = node.get("npc_garrison") or {}
            gar["troops"] = int(gar_row["troops_after"])
            gar["morale"] = int(gar_row["morale"])
            node["npc_garrison"] = gar
            # §9.1 驻军被清空 → 预约重生（respawn_hours 随机）
            if int(gar["troops"]) <= 0 and int(node.get("owner_faction", 0) or 0) > 0:
                ncfg = cfg.get("npc") or {}
                hours = ncfg.get("respawn_hours") or [48, 96]
                try:
                    lo, hi = int(hours[0]), int(hours[-1])
                except (TypeError, ValueError, IndexError):
                    lo, hi = 48, 96
                rng = self._rng("respawn", world["id"], node["id"],
                                world.get("tick_no", 0))
                node["respawn_at"] = _now() + max(0, rng.randint(lo, hi)) * 3600
            await self.save_node(node)
        await self._commit()

    # ------------------------------------------------------------------
    # 步骤 6：NPC AI（§9.3）
    # ------------------------------------------------------------------
    async def _step_npc_ai(self, world: dict):
        cfg = self.main_cfg().get("npc") or {}
        fcfg = self._favor_cfg()
        await self.ensure_npc_diplomacy(world)
        threshold = float(cfg.get("aggression_threshold", 10))
        reserve = float(cfg.get("garrison_home_reserve", 0.3))
        now = _now()
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        factions = await self._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' AND alive=1",
            (world["id"],))
        for frow in factions:
            fid = int(frow["id"])
            f = await self.get_faction(fid)
            if not f:
                continue
            owned = [n for n in nodes.values() if n["owner_faction"] == fid]
            if not owned:
                f["alive"] = 0
                f["faction_state"] = "destroyed"
                await self.save_faction(f)
                continue
            # 驻军恢复：+2%/tick，上限 等级×2000
            own_power = 0.0
            for node in owned:
                gar = node.get("npc_garrison") or {}
                cap = int(node["level"]) * 2000
                troops = int(gar.get("troops", 0))
                # §9.1 驻军重生：被清空后到时重生，并继承/强化仇恨记忆
                respawn_at = int(node.get("respawn_at", 0) or 0)
                if respawn_at and respawn_at <= now:
                    if troops <= 0:
                        gar = {"troops": cap, "morale": 70}
                        node["npc_garrison"] = gar
                        troops = cap
                        if cfg.get("respawn_hatred", True):
                            hatred = node.get("hatred") or {}
                            node["hatred"] = {k: int(int(v or 0) * 1.2)
                                              for k, v in hatred.items()
                                              if int(v or 0) > 0}
                    node["respawn_at"] = 0
                elif respawn_at > now and troops <= 0:
                    # 重生倒计时中：不按 2% 慢速回兵
                    node["npc_garrison"] = gar
                    await self.save_node(node)
                    continue
                troops = min(cap, troops + max(1, int(cap * 0.02)))
                gar["troops"] = troops
                gar["morale"] = min(85, int(gar.get("morale", 80)) + 2)
                node["npc_garrison"] = gar
                # 仇恨记忆逐 tick 衰减（§9.1）
                hatred = node.get("hatred") or {}
                if hatred:
                    node["hatred"] = {
                        k: int(v or 0) - 1 for k, v in hatred.items()
                        if int(v or 0) > 1}
                own_power += troops / 100
                await self.save_node(node)
            # 出征决策：每 tick 每势力最多 2 路
            sorties = 0
            for node in owned:
                if sorties >= 2:
                    break
                gar = node.get("npc_garrison") or {}
                troops = int(gar.get("troops", 0))
                send = int(troops * (1 - reserve))
                if send < 300:
                    continue
                best_target, best_util = None, threshold
                f_dip = f.get("diplomacy") or {}
                for adj_id in node.get("adj") or []:
                    target = nodes.get(int(adj_id))
                    if not target or target["owner_faction"] == fid:
                        continue
                    if target.get("protect_until", 0) > _now():
                        continue
                    # 同盟国不征伐（§6.3）
                    tf_owner = int(target["owner_faction"])
                    rel = R.REL_NEUTRAL
                    if tf_owner > 0:
                        tfo = await self.get_faction(tf_owner)
                        if tfo and int(tfo.get("alliance_id") or 0) and \
                                int(tfo.get("alliance_id")) == int(f.get("alliance_id") or 0):
                            continue
                        rel = R.relation_of(f_dip, tf_owner, _now())
                        if rel == R.REL_ALLY:
                            continue
                        # 好感足够高时 NPC 不愿主动开战（交易/赠礼的回报）
                        if rel != R.REL_WAR and \
                                R.favor_aggression_mult(
                                    R.favor_of(f_dip, tf_owner), fcfg) <= 0:
                            continue
                    garrison_power = int((target.get("npc_garrison") or {}).get(
                        "troops", 0)) / 100
                    util = R.npc_utility(
                        target, f, int(own_power * 100), int(garrison_power * 100),
                        path_edges=1,
                        adjacent_to_enemy_core=(target["id"] ==
                                                int(f.get("capital_node_id", 0) or 0)))
                    # 赛季侵略性：决战 +30% / 合纵讨霸 ×1.5（§11）
                    util *= self.npc_aggression_mult(
                        world, fid, int(target["owner_faction"]))
                    if tf_owner > 0:
                        if rel == R.REL_WAR:
                            util *= float(fcfg.get("war_target_bonus", 1.35))
                        else:
                            util *= R.favor_aggression_mult(
                                R.favor_of(f_dip, tf_owner), fcfg)
                    if util > best_util:
                        best_util, best_target = util, target
                if not best_target:
                    continue
                army = await self._spawn_npc_army(world, f, node, best_target, send)
                if army:
                    gar["troops"] = troops - send
                    node["npc_garrison"] = gar
                    await self.save_node(node)
                    sorties += 1
            # §9.1 低兵/低士气：撤回本城保将（不做溃败惩罚）
            for a in await self.get_armies(
                    world["id"], faction_id=fid,
                    states=("idle", "march", "siege")):
                if int(a.get("troops", 0) or 0) < 300 or \
                        int(a.get("morale", 100) or 0) < 20:
                    await self._auto_retreat(a)
        # 魔潮军团（faction -1）：扑向最近的有人据点
        for a in await self.get_armies(world["id"], faction_id=-1, states=("idle",)):
            target = await self._nearest_enemy_node(world, a["node_id"], exclude=-1)
            if target:
                path = await self.find_path(world["id"], a["node_id"], target)
                if path and len(path) > 1:
                    a["state"] = "march"
                    a["path"] = path[1:]
                    a["grain"] = max(a["grain"], 9999)  # 魔潮就食于野
                    await self.save_army(a)
        await self._commit()

    async def _nearest_enemy_node(self, world: dict, src: int, exclude: int) -> Optional[int]:
        from collections import deque
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        q = deque([src])
        seen = {src}
        while q:
            cur = q.popleft()
            for nxt in nodes.get(cur, {}).get("adj") or []:
                if nxt in seen or nxt not in nodes:
                    continue
                seen.add(nxt)
                if nodes[nxt]["owner_faction"] != exclude:
                    return nxt
                q.append(nxt)
        return None

    async def _spawn_npc_army(self, world: dict, faction: dict, node: dict,
                              target: dict, troops: int) -> Optional[dict]:
        fid = int(faction["id"])
        pers = faction.get("personality") or {}
        troop_type = pers.get("troop_pref") or "sword_shield"
        # 主将：势力空闲武将，否则临时生成
        leader = None
        for c in await self.get_chars(faction_id=fid, world_id=world["id"], status="idle"):
            if c["role"] == R.ROLE_GENERAL:
                leader = c
                break
        rng = self._rng("npc_army", fid, node["id"], world["tick_no"])
        if not leader:
            leader = R.generate_character(rng, rarity=rng.choice(["fine", "rare"]),
                                          role=R.ROLE_GENERAL,
                                          name_pools=self.cfg.get("names") or {})
            leader.update({"world_id": world["id"], "faction_id": fid,
                           "owner_user_id": "", "loyalty": 100})
            await self.insert_char(leader)
        path = await self.find_path(world["id"], node["id"], target["id"])
        if not path or len(path) < 2:
            return None
        leader["status"] = "army"
        leader["location"] = {"node_id": node["id"]}
        await self.save_char(leader)
        army = {
            "world_id": world["id"], "faction_id": fid, "owner_user_id": "",
            "name": f"{faction.get('name', 'NPC')}·{leader['name']}军",
            "node_id": node["id"], "characters": [leader["id"]],
            "troop_type": troop_type, "veteran": rng.randint(0, 30),
            "troops": int(troops), "grain": int(troops * 2), "morale": 90,
            "state": "march",
        }
        await self.insert_army(army)
        army["path"] = path[1:]
        army["edge_pos"] = 0.0
        await self.save_army(army)
        return army

    # ------------------------------------------------------------------
    # 步骤 7：事件掷点（§11.1：0~2 个/tick）
    # ------------------------------------------------------------------
    async def _step_events(self, world: dict) -> list[str]:
        ecfg = self.cfg.get("events") or {}
        # 势力事件（scope=faction）由 strategy_faction_mixin._step_faction_events
        # 独立结算，这里只掷世界/区域事件，避免同一 tick 重复公告
        events = [e for e in (ecfg.get("events") or [])
                  if str(e.get("scope") or "region") != "faction"]
        if not events:
            return []
        rng = self._rng("events", world["id"], world["tick_no"])
        lo, hi = (ecfg.get("roll_per_tick") or [0, 2])
        rolls = rng.randint(int(lo), int(hi))
        data = world.get("data") or {}
        cd = data.get("event_cd") or {}
        tick_no = int(world["tick_no"])
        names: list[str] = []
        nodes = await self.get_nodes(world["id"])
        for _ in range(rolls):
            eligible = [e for e in events if tick_no >= int(cd.get(e["id"], 0))]
            if not eligible:
                break
            weights = [max(1, int(e.get("weight", 1))) for e in eligible]
            ev = rng.choices(eligible, weights=weights)[0]
            trigger = ev.get("trigger") or {}
            target: dict[str, Any] = {}
            if ev.get("scope") == "region":
                cands = nodes
                if trigger.get("node_types"):
                    cands = [n for n in cands if n["type"] in trigger["node_types"]]
                if trigger.get("node_terrain"):
                    cands = [n for n in cands if n["terrain"] in trigger["node_terrain"]]
                if not cands:
                    cd[ev["id"]] = tick_no + 8  # 短期重试
                    continue
                pick = rng.choice(cands)
                target = {"node_ids": [pick["id"]], "node_name": pick["name"]}
            duration = int(ev.get("duration_ticks", 8))
            await self._execute("""
                INSERT INTO strategy_events (world_id, event_id, scope, target_json,
                    start_at, end_at, start_tick, end_tick, data_json)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (world["id"], ev["id"], ev.get("scope", "region"),
                  json.dumps(target, ensure_ascii=False), _now(),
                  _now() + duration * self.tick_seconds(), tick_no,
                  tick_no + duration,
                  json.dumps({"name": ev.get("name", ev["id"]),
                              "effects": ev.get("effects") or []}, ensure_ascii=False)))
            cd[ev["id"]] = tick_no + duration + int(ev.get("cooldown_ticks", 32))
            names.append(ev.get("name", ev["id"]))
            # 即时效果
            await self._apply_event_onset(world, ev, target, rng)
        data["event_cd"] = cd
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return names

    async def _step_event_spread(self, world: dict):
        """瘟疫等事件的传染效果（spread）：每 tick 按概率蔓延至相邻据点。

        蔓延采用「扩充同一事件行的 target.node_ids」方式：
        node_dot 等持续效果按 node_ids 消费，天然覆盖新疫区；
        事件到期（end_tick）时新旧疫区一并结束。
        """
        tick_no = int(world.get("tick_no", 0))
        active = await self.get_active_events(world["id"], tick_no)
        spread_evs: list[tuple[dict, dict]] = []
        for ev in active:
            for eff in (ev.get("data") or {}).get("effects") or []:
                if eff.get("type") == "spread" and eff.get("to", "adjacent") == "adjacent":
                    spread_evs.append((ev, eff))
                    break
        if not spread_evs:
            return
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        # 同一事件已覆盖的节点集合（避免重复感染/跨行重复）
        infected: dict[str, set] = {}
        for ev in active:
            s = infected.setdefault(str(ev.get("event_id", "")), set())
            for nid in (ev.get("target") or {}).get("node_ids") or []:
                s.add(int(nid))
        dirty = False
        for ev, eff in spread_evs:
            target = ev.get("target") or {}
            node_ids = [int(x) for x in target.get("node_ids") or []]
            if not node_ids:
                continue
            chance = float(eff.get("chance", 0.25))
            rng = self._rng("spread", ev["id"], tick_no)
            covered = infected.setdefault(str(ev.get("event_id", "")), set())
            new_ids: list[int] = []
            for nid in node_ids:
                node = nodes.get(nid)
                if not node:
                    continue
                for adj_id in node.get("adj") or []:
                    adj_id = int(adj_id)
                    if adj_id in covered or adj_id in new_ids:
                        continue
                    if adj_id not in nodes:
                        continue
                    if rng.random() < chance:
                        new_ids.append(adj_id)
                        covered.add(adj_id)
            if not new_ids:
                continue
            target["node_ids"] = node_ids + new_ids
            first = nodes.get(target["node_ids"][0])
            if first:
                target["node_name"] = first["name"]
            ev["target"] = target
            await self._execute(
                "UPDATE strategy_events SET target_json=? WHERE id=?",
                (json.dumps(target, ensure_ascii=False), int(ev["id"])))
            dirty = True
            names = "、".join(nodes[x]["name"] for x in new_ids if x in nodes)
            logger.info(f"[strategy] 事件「{ev.get('event_id')}」蔓延至：{names}")
        if dirty:
            await self._commit()

    async def _apply_event_onset(self, world: dict, ev: dict, target: dict,
                                 rng: random.Random):
        """事件生效瞬间的一次性效果（洪涝粮损 / 魔潮生成）。"""
        for eff in ev.get("effects") or []:
            if eff.get("type") == "stock_loss":
                pct = float(eff.get("pct", 0.1))
                for nid in target.get("node_ids") or []:
                    node = await self.get_node(world["id"], int(nid))
                    if not node or node["owner_faction"] <= 0:
                        continue
                    f = await self.get_faction(node["owner_faction"])
                    if f and f.get("kind") == "npc":
                        treasury = f.get("treasury") or {}
                        loss = int(treasury.get("grain", 0) * pct)
                        treasury["grain"] = int(treasury.get("grain", 0)) - loss
                        f["treasury"] = treasury
                        await self.save_faction(f)
                    elif f and f.get("kind") == "player":
                        row = await self._fetchone(
                            "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
                            (world["id"], node["owner_faction"]))
                        if row:
                            p = await self.get_player(row["user_id"], world["id"])
                            loss = int(p["grain"] * pct)
                            p["grain"] -= loss
                            await self.save_player(p)
            elif eff.get("type") == "spawn_demon_armies":
                lo, hi = eff.get("count") or [2, 4]
                count = rng.randint(int(lo), int(hi))
                cands = [n for n in await self.get_nodes(world["id"])
                         if n["owner_faction"] == 0 and n["type"] in ("ruin", "mine")]
                rng.shuffle(cands)
                for node in cands[:count]:
                    demon = R.generate_character(
                        rng, rarity="rare", role=R.ROLE_GENERAL, profession="demon",
                        name_pools=self.cfg.get("names") or {})
                    demon.update({"world_id": world["id"], "faction_id": -1,
                                  "owner_user_id": "", "loyalty": 100,
                                  "status": "army"})
                    await self.insert_char(demon)
                    troops = rng.randint(800, 1500)
                    army = {
                        "world_id": world["id"], "faction_id": -1, "owner_user_id": "",
                        "name": f"魔潮·{demon['name']}", "node_id": node["id"],
                        "characters": [demon["id"]], "troop_type": "beast_horde",
                        "veteran": rng.randint(10, 40), "troops": troops,
                        "grain": 9999, "morale": 100, "state": "idle",
                    }
                    await self.insert_army(army)
        # P3 事件效果（流将/劫掠/会武，§11.1）
        await self._p3_event_onset(world, ev, target, rng)
        await self._commit()

    # ------------------------------------------------------------------
    # 面板快照（§12.2：战争迷雾过滤）
    # ------------------------------------------------------------------
    @staticmethod
    def _garrison_bucket(troops: int) -> str:
        for th, label in ((3000, "3000+"), (2000, "2000+"), (1000, "1000+"),
                          (500, "500+"), (100, "100+"), (1, "<100")):
            if troops >= th:
                return label
        return "0"

    async def get_wanderers_view(self, world: dict) -> list[dict]:
        """在野流将（兼容旧前端）：已统一改为坊市限时竞拍，不再直接挂价招募。"""
        return []

    async def get_state(self, user_id: str) -> dict:
        """网页面板全量状态（含战争迷雾过滤）。"""
        world = await self.ensure_world()
        wid = world["id"]
        p = await self.get_player(user_id, wid)
        if p and await self._repair_player_state(world, p):
            p = await self.get_player(user_id, wid)
        nodes = await self.get_nodes(wid)
        factions_rows = await self._fetchall(
            "SELECT * FROM strategy_factions WHERE world_id=?", (wid,))
        factions = self._decode_all("strategy_factions", factions_rows)
        node_count: dict[int, int] = {}
        for n in nodes:
            node_count[n["owner_faction"]] = node_count.get(n["owner_faction"], 0) + 1
        f_meta = {}
        for f in factions:
            f_meta[f["id"]] = {"id": f["id"], "name": f["name"],
                               "color": R.norm_color(f.get("color")),
                               "kind": f["kind"], "alive": f["alive"],
                               "reputation": f["reputation"], "level": f["level"],
                               "alliance_id": int(f.get("alliance_id") or 0),
                               "node_count": node_count.get(f["id"], 0)}
        f_meta[-1] = {"id": -1, "name": "魔潮", "color": "#7b1fa2", "kind": "demon",
                      "alive": 1, "reputation": -100, "level": 1,
                      "node_count": node_count.get(-1, 0)}
        f_meta[0] = {"id": 0, "name": "无主", "color": "#9e9e9e", "kind": "none",
                     "alive": 1, "reputation": 0, "level": 1,
                     "node_count": node_count.get(0, 0)}
        my_fid = p["faction_id"] if p else -999
        my_faction = await self.get_faction(int(my_fid)) if p else None
        if my_faction:
            my_host = await self._alliance_host(my_faction)
            my_dip = (my_host or my_faction).get("diplomacy") or {}
            for fid, meta in f_meta.items():
                if int(fid) > 0:
                    meta["favor"] = R.favor_of(my_dip, int(fid))
                    meta["favor_name"] = R.favor_name(meta["favor"])
        alliance_ids = await self._alliance_ids(wid, my_faction) \
            if my_faction else {int(my_fid)}
        my_nodes = {n["id"] for n in nodes if n["owner_faction"] in alliance_ids}
        visible_nodes = set(my_nodes)
        for n in nodes:
            if n["id"] in my_nodes:
                visible_nodes.update(n["adj"])
        now_ts = _now()
        raw_scouted = (world.get("data") or {}).get("scouted") or {}
        scouted: dict[int, int] = {}
        for fid in alliance_ids or {my_fid}:
            for k, v in (raw_scouted.get(str(int(fid))) or {}).items():
                until = int(v or 0)
                if until > now_ts and until > scouted.get(int(k), 0):
                    scouted[int(k)] = until
        out_nodes = []
        for n in nodes:
            gar = int((n.get("npc_garrison") or {}).get("troops", 0))
            visible = n["id"] in visible_nodes or n["id"] in scouted
            ss = n.get("siege_state") or {}
            out_nodes.append({
                "id": n["id"], "name": n["name"], "type": n["type"],
                "terrain": n["terrain"], "level": n["level"],
                "pos": [n["pos_x"], n["pos_y"]], "adj": n["adj"],
                "owner": f_meta.get(n["owner_faction"],
                                    {"id": n["owner_faction"], "name": "?",
                                     "color": "#888", "kind": "npc"}),
                "defense": n["defense"],
                "defense_max": R.city_defense_max(n["level"])
                if n["type"] in ("city", "pass", "branch") else 0,
                "morale": n["morale"],
                "morale_cap": self._morale_cap(n),
                "relief_used": self._relief_used(p, n["id"]) if p else 0,
                "respawn_at": int(n.get("respawn_at", 0) or 0),
                "lair": bool(n.get("lair")),
                "buildings": [
                    ({"id": str(b.get("id")),
                      "level": max(1, int(b.get("level", 1) or 1))}
                     if isinstance(b, dict) else {"id": str(b), "level": 1})
                    for b in (n.get("buildings") or [])],
                "siege": {"ticks": ss.get("ticks", 0), "choice": ss.get("choice"),
                          "attacker_faction": ss.get("attacker_faction", 0)} if ss else None,
                "garrison": gar if visible else None,
                "garrison_bucket": None if visible else self._garrison_bucket(gar),
                "scouted_until": scouted.get(n["id"], 0)
                if n["id"] not in visible_nodes else 0,
                "protected": n.get("protect_until", 0) > now_ts,
            })
        # 军团（迷雾：非可见据点上的敌军不展示）
        out_armies = []
        char_names: dict[int, str] = {}
        for c in await self.get_chars(world_id=wid):
            char_names[c["id"]] = c["name"]
        for a in await self.get_armies(wid):
            is_mine = a.get("owner_user_id") == user_id or \
                a["faction_id"] in alliance_ids
            node_visible = a["node_id"] in visible_nodes or a["node_id"] in scouted
            if not is_mine and not node_visible and a["state"] != "march":
                continue
            if not is_mine and a["state"] == "march" and not node_visible:
                continue
            fm = f_meta.get(a["faction_id"], {"name": "?", "color": "#888"})
            out_armies.append({
                "id": a["id"], "name": a["name"], "faction": fm["name"],
                "faction_color": fm["color"], "mine": is_mine,
                "node_id": a["node_id"], "state": a["state"],
                "path": a["path"], "troops": a["troops"] if is_mine or
                node_visible else None,
                "troop_type": a["troop_type"], "morale": a["morale"] if is_mine else None,
                "veteran": a["veteran"] if is_mine else None,
                "grain": a["grain"] if is_mine else None,
                "leader": char_names.get((a.get("characters") or [0])[0], ""),
            })
        state: dict[str, Any] = {
            "joined": bool(p),
            "world": {"id": wid, "season_no": world["season_no"],
                      "template_id": world["template_id"], "tick_no": world["tick_no"],
                      "last_tick_at": world["last_tick_at"],
                      "next_tick_at": world["last_tick_at"] + self.tick_seconds(),
                      "ends_at": world["ends_at"]},
            "nodes": out_nodes,
            "factions": sorted(f_meta.values(), key=lambda x: -x["node_count"]),
            "armies": out_armies,
            "events": [{"id": e["event_id"], "name": (e.get("data") or {}).get(
                "name", e["event_id"]), "target": e.get("target"),
                "end_tick": e["end_tick"]}
                for e in await self.get_active_events(wid, world["tick_no"])],
            "recent_ticks": (world.get("data") or {}).get("recent_ticks") or [],
            "season": await self.get_season_view(world),
            "bandits": await self.get_bandits_view(world),
            "trade_routes": await self.get_trade_routes_view(world),
            "trade_route_cfg": self.main_cfg().get("trade_route") or {},
            "convoy_cfg": self.main_cfg().get("convoy") or {},
            "wanderers": await self.get_wanderers_view(world),
            "wanderer_auctions": await self.get_wanderer_auctions_view(
                world, user_id),
            "petitions": [
                {"id": pet.get("id"), "name": pet.get("name"),
                 "color": R.norm_color(pet.get("color")), "creator": pet.get("creator"),
                 "cosigners": len(pet.get("cosigners") or []),
                 "expire_at": int(pet.get("expire_at", 0))}
                for pet in ((world.get("data") or {}).get("faction_petitions") or {}).values()
                if pet.get("state") == "open"
                and _now() <= int(pet.get("expire_at", 0))],
            "winds": [w.get("text", "") for w in
                      ((world.get("data") or {}).get("winds") or [])
                      if _now() >= int(w.get("visible_at", 0))][-10:],
            "start_candidates": (await self._starter_candidates(world)) if not p else [],
            "meta": self._frontend_meta(),
            "memorials": await self.get_memorials(wid),
            "transfers": await self.get_transfers_view(wid, p["faction_id"] if p else 0),
        }
        if p:
            state["player"] = await self._player_view(p, world, nodes)
            state["battles"] = await self._battle_list(wid, p["faction_id"])
            f_ext = await self._governance_faction(p)
            if f_ext:
                state["faction_ext"] = await self.get_faction_ext(world, f_ext)
        return state

    async def _task_preview(self, world: dict, task: dict, c: dict,
                            node: dict, partner_name: str = "") -> dict:
        """任务收益预览（进行中列表展示；不含 ±15% 随机波动与小事件）。"""
        tt = str(task.get("task_type") or "")
        payload = task.get("payout") or {}
        if tt.startswith("plot_"):
            return {"output_kind": "plot", "output_name": "计略效果",
                    "estimate": None, "note": "成功率判定"}
        kind = R.TASK_OUTPUT_RES.get(tt, "coin")
        out = {"output_kind": kind,
               "output_name": TASK_OUTPUT_TEXT.get(kind, kind),
               "estimate": None, "note": ""}
        if tt == "raid":
            out["note"] = "掠夺库存"
            return out
        if tt == "scout":
            out["note"] = "揭示守军情报"
            return out
        if tt == "supply":
            out["estimate"] = int(payload.get("supplied") or 0)
            out["note"] = "已注入军团"
            return out
        tcfg = self.main_cfg().get("task") or {}
        base_map = dict(tcfg.get("base_per_tick") or {})
        base_map.setdefault("seek", base_map.pop("quest", 60))
        ticks = max(1, int(payload.get("ticks") or 4))
        event_mult = await self._task_event_mult(world, task, node)
        amount = R.estimate_task(
            tt, c, float(base_map.get(tt, 50)), ticks,
            str(node.get("type") or "ruin"),
            int(node.get("morale", 80)), event_mult)
        if int(payload.get("partner_char_id") or 0):
            amount *= 1.15
        amount = max(0, int(amount))
        if kind == "token":
            out["estimate"] = amount // 300
        elif kind == "merit":
            out["estimate"] = amount // 10
        elif kind == "build":
            out["estimate"] = amount * 2
        elif kind in ("heal", "train", "intel"):
            out["note"] = "按据点单位结算"
        else:
            out["estimate"] = amount
        morale = R.morale_task_gain(tt, ticks, self.main_cfg())
        if morale:
            out["morale_gain"] = morale
        if partner_name:
            out["partner_name"] = partner_name
        return out

    async def _player_view(self, p: dict, world: dict, nodes: list[dict]) -> dict:
        node_names = {n["id"]: n["name"] for n in nodes}
        node_map = {int(n["id"]): n for n in nodes}
        char_names: dict[int, str] = {}
        for c0 in await self.get_chars(world_id=world["id"]):
            char_names[c0["id"]] = c0["name"]
        growth_cap_bonus = self._growth_cap_bonus()
        chars = []
        for c in await self.get_chars(owner_user_id=p["user_id"], world_id=world["id"]):
            chars.append({
                "id": c["id"], "name": c["name"], "role": c["role"],
                "role_name": R.ROLE_NAMES.get(c["role"], c["role"]),
                "rarity": c["rarity"],
                "rarity_name": R.RARITY_NAMES.get(c["rarity"], c["rarity"]),
                "profession": c["profession"],
                "profession_name": R.PROFESSION_NAMES.get(c["profession"], ""),
                "stats": c["stats"], "traits": c["traits"],
                "trait_names": [R.TRAIT_NAMES.get(t, t) for t in c["traits"]],
                "aptitude": c["aptitude"], "level": c["level"], "exp": c["exp"],
                "exp_next": R.exp_to_next(c["level"]),
                "stat_total": R.stat_total(c),
                "stat_cap": R.stat_growth_cap(c, growth_cap_bonus),
                "loyalty": c["loyalty"],
                "injury": c["injury"],
                "injury_state": R.injury_state(c["injury"]),
                "injury_name": R.INJURY_STATE_NAMES.get(R.injury_state(c["injury"]), ""),
                "stamina": c["stamina"], "status": c["status"],
                "appointed_position": str(c.get("appointed_position") or ""),
                "appointed_name": R.POSITION_NAMES.get(
                    str(c.get("appointed_position") or ""), "无官职"),
                "appointed_node_id": int(c.get("appointed_node_id") or 0),
                "appointed_node_name": node_names.get(
                    int(c.get("appointed_node_id") or 0), ""),
                "bonds": {str(k): v for k, v in (c.get("bonds") or {}).items()},
                "location": node_names.get(
                    int((c.get("location") or {}).get("node_id", 0) or 0), ""),
            })
        armies = []
        for a in await self.get_armies(world["id"], owner_user_id=p["user_id"]):
            armies.append({
                "id": a["id"], "name": a["name"], "node_id": a["node_id"],
                "node_name": node_names.get(a["node_id"], ""),
                "state": a["state"],
                "path": [node_names.get(x, str(x)) for x in a["path"]],
                "troop_type": a["troop_type"],
                "troop_name": R.TROOP_TYPES.get(a["troop_type"], {}).get("name", ""),
                "troops": a["troops"], "grain": a["grain"], "morale": a["morale"],
                "veteran": a["veteran"], "siege_engine": bool(a["siege_engine"]),
                "characters": [char_names.get(cid, f"#{cid}")
                               for cid in (a.get("characters") or [])],
                "character_ids": a.get("characters") or [],
            })
        tasks = []
        for t in await self.get_tasks(world["id"]):
            c = next((x for x in chars if x["id"] == t["character_id"]), None)
            if not c:
                continue
            tt = str(t["task_type"])
            if tt.startswith("plot_"):
                task_name = R.PLOT_NAMES.get(tt[len("plot_"):], tt)
            else:
                task_name = R.TASK_NAMES.get(tt, tt)
            node = node_map.get(int(t["node_id"] or 0)) or {}
            payload = t.get("payout") or {}
            partner_name = char_names.get(
                int(payload.get("partner_char_id") or 0), "")
            preview = await self._task_preview(world, t, c, node, partner_name)
            tasks.append({
                "id": t["id"], "char_id": t["character_id"], "char_name": c["name"],
                "task_type": tt, "task_name": task_name,
                "node_id": t["node_id"], "node_name": node_names.get(t["node_id"], ""),
                "end_at": t["end_at"], "remain_sec": max(0, t["end_at"] - _now()),
                **preview,
            })
        tavern = p.get("tavern") or {}
        # P1：势力详情（成员/国库/科技/外交，§4/§6.3/§8.4）
        # v72：结盟后保留自身势力，但治理/国库/科技/外交读取盟主势力
        f = await self.get_faction(p["faction_id"])
        host = await self._alliance_host(f) if f else None
        members = await self.get_members(host["id"]) if host else []
        faction_view = None
        if f and host:
            now = _now()
            relations = []
            seen_rel: set[int] = set()
            fcfg = ((self.main_cfg().get("diplomacy") or {}).get("favor") or {})
            for k, v in (host.get("diplomacy") or {}).items():
                try:
                    other_id = int(k)
                except (TypeError, ValueError):
                    continue
                rel = str(v.get("rel", "neutral"))
                until = int(v.get("until", 0) or 0)
                if rel.startswith("propose_") and until and until < now:
                    continue  # 过期提案不展示
                of = await self.get_faction(other_id)
                if not of:
                    continue
                seen_rel.add(other_id)
                favor = R.favor_of(host.get("diplomacy") or {}, other_id)
                relations.append({
                    "faction_id": other_id,
                    "name": (of or {}).get("name", "?"),
                    "kind": (of or {}).get("kind", ""),
                    "rel": rel, "until": until,
                    "favor": favor, "favor_name": R.favor_name(favor),
                    "favor_tier": R.favor_tier(favor),
                    "ally_gate": int(fcfg.get("ally_min", 60)),
                    "from": int(v.get("from", 0) or 0),
                    "indemnity": int(v.get("indemnity", 0) or 0)})
            # 尚未建交的 NPC 也展示（默认中立/0 好感），便于赠礼与初期结盟
            npc_rows = await self._fetchall(
                "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
                "AND alive=1", (world["id"],))
            for row in npc_rows:
                nid = int(row["id"])
                if nid in seen_rel:
                    continue
                of = await self.get_faction(nid)
                favor = R.favor_of(host.get("diplomacy") or {}, nid)
                relations.append({
                    "faction_id": nid, "name": (of or {}).get("name", "?"),
                    "kind": "npc", "rel": R.REL_NEUTRAL, "until": 0,
                    "favor": favor, "favor_name": R.favor_name(favor),
                    "favor_tier": R.favor_tier(favor),
                    "ally_gate": int(fcfg.get("ally_min", 60)),
                    "from": 0, "indemnity": 0})
            counts: dict[int, int] = {}
            for n in nodes:
                counts[int(n["owner_faction"])] = counts.get(int(n["owner_faction"]), 0) + 1
            allied = []
            for af in await self._alliance_factions(world["id"], host):
                allied.append({
                    "id": int(af["id"]), "name": af["name"],
                    "color": R.norm_color(af.get("color")),
                    "node_count": counts.get(int(af["id"]), 0),
                    "capital_node_id": int(af.get("capital_node_id") or 0),
                    "leader_id": str(af.get("leader_id") or ""),
                    "is_host": int(af["id"]) == int(host["id"]),
                })
            aid = int(f.get("alliance_id") or 0)
            pmap = {str(r["user_id"]): int(r["faction_id"]) for r in await self._fetchall(
                "SELECT user_id, faction_id FROM strategy_players WHERE world_id=?",
                (world["id"],))}
            # 角色级官职：开放档位按势力（含联盟）据点总数
            gov_ids = await self._alliance_ids(world["id"], f)
            gov_nodes = sum(1 for n in nodes
                            if int(n["owner_faction"]) in gov_ids)
            gov_chars = [c for c in await self.get_chars(world_id=world["id"])
                         if int(c.get("faction_id") or 0) in gov_ids and
                         c.get("status") not in ("dead", "captive")]
            avail = R.available_positions(gov_nodes)
            appointable = {
                pos: {
                    "name": R.POSITION_NAMES.get(pos, pos),
                    "cap": int(cap),
                    "used": sum(1 for c in gov_chars
                                if str(c.get("appointed_position") or "") == pos),
                    "effect": R.POSITION_EFFECT_TEXT.get(pos, ""),
                    "unlock_nodes": R.position_unlock_nodes(pos),
                    "troop_cap": R.POSITION_TROOP_CAP.get(pos, 1500),
                }
                for pos, cap in avail.items()}
            appointments = [
                {"char_id": c["id"], "name": c["name"],
                 "owner_user_id": c.get("owner_user_id", ""),
                 "position": str(c.get("appointed_position") or ""),
                 "position_name": R.POSITION_NAMES.get(
                     str(c.get("appointed_position") or ""), "无官职"),
                 "node_id": int(c.get("appointed_node_id") or 0),
                 "node_name": node_names.get(int(c.get("appointed_node_id") or 0), "")}
                for c in gov_chars if str(c.get("appointed_position") or "")]
            appoint_chars = [
                {"id": c["id"], "name": c["name"],
                 "owner_user_id": c.get("owner_user_id", ""),
                 "role_name": R.ROLE_NAMES.get(c.get("role", ""), c.get("role", "")),
                 "position": str(c.get("appointed_position") or ""),
                 "node_id": int(c.get("appointed_node_id") or 0)}
                for c in gov_chars]
            faction_view = {
                "id": f["id"], "name": f["name"], "kind": f["kind"],
                "level": f["level"], "reputation": f["reputation"],
                "capital_node_id": int(f.get("capital_node_id", 0) or 0),
                "node_count": counts.get(int(f["id"]), 0),
                "treasury": host.get("treasury") or {},
                "tech": host.get("tech") or {},
                "member_cap": int(f.get("member_cap", 10)),
                "members": [{"user_id": m["user_id"],
                             "position": m.get("position", ""),
                             "faction_id": pmap.get(str(m["user_id"]), int(f["id"])),
                             "contribution": int(m.get("contribution", 0) or 0)}
                            for m in members],
                "my_position": self._position_of(members, p["user_id"]),
                # 联盟视图：自身势力 id 不变，治理/盟主另给 governance_id
                "alliance_id": aid,
                "governance_id": int(host["id"]),
                "alliance_name": host["name"] if aid else "",
                "is_alliance_host": bool(aid) and int(host["id"]) == int(f["id"]),
                "allied_factions": allied if aid else [],
                "relations": relations,
                # 待审批的结盟申请（治理用，§8.4）
                "applications": sorted(
                    ((world.get("data") or {}).get("join_apps") or {})
                    .get(str(host["id"]), {}).keys()),
                # 角色级官职（v74）
                "governance_node_count": int(gov_nodes),
                "can_appoint": self._position_of(members, p["user_id"])
                in ("leader", "deputy"),
                "appointable": appointable,
                "appointments": appointments,
                "appoint_chars": appoint_chars,
            }
        cap_info = await self._char_cap_info(world["id"], p)
        roster_count = sum(
            1 for c in chars if c.get("status") not in ("dead", "captive"))
        return {
            "user_id": p["user_id"], "faction_id": p["faction_id"],
            "coin": p["coin"], "grain": p["grain"], "iron": p["iron"],
            "recruit_token": p["recruit_token"], "merit": p["merit"],
            "char_cap": int(cap_info["cap"]),
            "char_count": int(roster_count),
            "char_cap_nodes": int(cap_info["nodes"]),
            "char_cap_per_node": int(cap_info["per_node"]),
            "growth_cap_bonus": int(growth_cap_bonus),
            "honor": self._honor_view(p),
            "notify_pref": p.get("notify_pref") or {"level": 1},
            "chars": chars, "armies": armies, "tasks": tasks,
            "tavern": {"candidates": tavern.get("candidates") or [],
                      "refresh_at": tavern.get("refresh_at", 0),
                      "daily_refresh_used": int(tavern.get("daily_refresh_used", 0))},
            "intel": int(tavern.get("intel", 0) or 0),
            "achievements": p.get("achievements") or {},
            "newcomer": p.get("newcomer") or {},
            "tutorial": p.get("tutorial") or {},
            "faction": faction_view,
            "faction_invite": ((world.get("data") or {}).get("join_invites") or {})
            .get(p["user_id"]),
            "captives": await self.get_captives_view(world["id"], p["faction_id"]),
            "market_orders": await self.market_orders(p["user_id"]),
            "convoys": await self.get_convoys_view(p["user_id"]),
            "duels": await self.duel_arena_list(p["user_id"]),
        }

    async def _battle_list(self, world_id: int, faction_id: int,
                           limit: int = 20) -> list[dict]:
        out = []
        for b in await self.get_battles(world_id, limit=limit, faction_id=faction_id):
            res = b.get("result") or {}
            out.append({
                "id": b["id"], "node_id": b["node_id"], "kind": b["kind"],
                "mode": b.get("mode", "auto"), "state": b.get("state", "done"),
                "deadline_at": int(b.get("deadline_at", 0) or 0),
                "attacker_faction": b["attacker_faction"],
                "defender_faction": b["defender_faction"],
                "winner": res.get("winner"), "rounds": b["round_no"],
                "report_text": b["report_text"], "created_at": b["created_at"],
                "highlights": res.get("highlights") or [],
            })
        return out

    async def get_battle_report(self, user_id: str, battle_id: int) -> dict:
        world, p = await self._require_player(user_id)
        row = await self._fetchone(
            "SELECT * FROM strategy_battles WHERE id=? AND world_id=?",
            (battle_id, world["id"]))
        b = self._decode("strategy_battles", row)
        if not b:
            raise StrategyError("战报不存在")
        # A-M12：仅参战阵营可查详情（与 _battle_list 的 faction 过滤同口径，
        # 防越权遍历 battle_id 读取他方战损细节）
        if int(b.get("attacker_faction", 0)) != int(p["faction_id"]) and \
                int(b.get("defender_faction", 0)) != int(p["faction_id"]):
            raise StrategyError("无权查看该战报（仅参战方可见）")
        return {"id": b["id"], "node_id": b["node_id"], "kind": b["kind"],
                "winner": (b.get("result") or {}).get("winner"),
                "report_text": b["report_text"], "result": b.get("result"),
                "created_at": b["created_at"]}

    def _task_meta(self) -> dict:
        """任务选择提示：核心属性 / 最佳职责 / 产出 / 据点加成。"""
        node_bonus = {"govern": "仙城 ×1.5", "farm": "灵谷田 ×2.0",
                      "mining": "灵脉矿 ×2.0 / 关隘·遗迹 ×1.3",
                      "trade": "坊市 ×1.3", "seek": "分舵 ×1.4",
                      "hunt": "遗迹 ×1.3", "build": "仙城 ×1.2"}
        meta = {}
        for k in ("govern", "farm", "mining", "trade", "seek", "scout", "build",
                  "train", "hunt", "heal", "research"):
            best = R.TASK_BEST_ROLE.get(k)
            if best:
                role = R.ROLE_NAMES.get(best, best)
            elif k in ("trade", "build", "heal"):
                role = "内政官/军师"
            else:
                role = "任意"
            kind = R.TASK_OUTPUT_RES.get(k, "")
            meta[k] = {
                "name": R.TASK_NAMES.get(k, k),
                "stat": R.STAT_NAMES.get(R.TASK_PRIMARY_STAT.get(k, "admin"), ""),
                "role": role,
                "output_kind": kind,
                "output": TASK_OUTPUT_TEXT.get(kind, ""),
                "node": node_bonus.get(k, ""),
            }
        return meta

    def _frontend_meta(self) -> dict:
        cfg = self.main_cfg()
        return {
            "tick_seconds": self.tick_seconds(),
            "troop_types": {k: {"name": v["name"], "atk": v["atk"], "def": v["def"],
                                "move": v["move"], "range": v["range"],
                                "recruit_coin": v["recruit_coin"],
                                "recruit_grain": v["recruit_grain"],
                                "skill": v["skill"], "terrain": v["terrain"]}
                            for k, v in R.TROOP_TYPES.items()},
            "counter": {f"{a}>{b}": m for (a, b), m in R.COUNTER_MATRIX.items()},
            "buildings": {k: v for k, v in self.BUILDINGS.items()},
            "task_types": {k: R.TASK_NAMES.get(k, k) for k in
                           ("govern", "farm", "mining", "trade", "seek", "scout",
                            "build", "train", "hunt", "heal", "research")},
            "task_meta": self._task_meta(),
            "task_hours": (cfg.get("task") or {}).get("hours_options", [1, 2, 4, 8]),
            "rarity_names": dict(R.RARITY_NAMES),
            "role_names": dict(R.ROLE_NAMES),
            "profession_names": dict(R.PROFESSION_NAMES),
            "stat_names": dict(R.STAT_NAMES),
            "injury_names": dict(R.INJURY_STATE_NAMES),
            "node_output": cfg.get("node_output_per_tick") or {},
            "gold_output": {k: v for k, v in (cfg.get("gold_output") or {}).items()},
            "positions": {k or "none": {"name": v, "cap": R.POSITION_TROOP_CAP.get(k, 1500)}
                          for k, v in R.POSITION_NAMES.items()},
            "appointable_positions": {
                p: {"name": R.POSITION_NAMES.get(p, p),
                    "effect": R.POSITION_EFFECT_TEXT.get(p, ""),
                    "unlock_nodes": R.position_unlock_nodes(p),
                    "troop_cap": R.POSITION_TROOP_CAP.get(p, 1500)}
                for p in R.APPOINTABLE_POSITIONS},
            "position_unlocks": [
                {"nodes": int(nodes), "positions": dict(caps)}
                for nodes, caps in R.POSITION_UNLOCKS],
            # --- P1~P3 元数据 ---
            "plot_types": dict(R.PLOT_NAMES),
            "tech_lines": list(R.TECH_LINES),
            "tech": cfg.get("tech") or {},
            "achievements": {k: {"name": v.get("name", k),
                                 "desc": v.get("desc", ""),
                                 "merit": int(v.get("merit", 0))}
                             for k, v in (cfg.get("achievements") or {}).items()},
            "faction_cfg": cfg.get("faction") or {},
            "alliance_cfg": cfg.get("alliance") or {},
            "node_types": {"city": "仙城", "pass": "关隘", "branch": "分舵",
                           "market": "坊市", "grain": "良田", "mine": "矿脉",
                           "ruin": "遗迹"},
            "diplomacy_cfg": cfg.get("diplomacy") or {},
            "favor_cfg": (cfg.get("diplomacy") or {}).get("favor") or {},
            "captive_cfg": cfg.get("captive") or {},
            "duel_cfg": cfg.get("duel") or {},
            "hex_cfg": cfg.get("hex") or {},
            "market_cfg": cfg.get("market") or {},
            "wanderer_auction_cfg": cfg.get("wanderer_auction") or {},
            "season_cfg": cfg.get("season") or {},
            "shield_cfg": cfg.get("shield") or {},
            "rebuild_cfg": cfg.get("rebuild") or {},
            # 战功商店价目/限购与伤势配置：前端按配置渲染，避免文案写死
            "merit_shop": cfg.get("merit_shop") or {},
            "honor_shop": (cfg.get("season") or {}).get("honor_shop") or {},
            "injury_cfg": cfg.get("injury") or {},
            "governance_cfg": cfg.get("governance") or {},
        }

    # ------------------------------------------------------------------
    # 动作分发（REST → manager）
    # ------------------------------------------------------------------
    #: 需要审计的关键动作（§18.4）
    _AUDIT_ACTIONS = {
        "enter", "faction_create", "faction_quit", "faction_set_position",
        "treasury_withdraw", "rebuild_capital", "capital_relocate",
        "mobilization_issue", "transfer_captive", "transfer_node",
        "govern_relief", "upgrade_building", "create_army", "raid_bandit",
        "dispatch_plot", "market_list", "market_buy", "persuade",
    }

    async def handle_action(self, user_id: str, action: str,
                            params: Optional[dict] = None) -> dict:
        """动作统一入口（REST 漏斗）。

        A-M7：全程持 _action_lock 串行化玩家动作，防并发「读-改-写」互相
        覆盖丢更新（钱包/军团/互市等）；锁序恒为 _action_lock → _world_lock
        （tick/poll 同序，GM 重置赛季只拿 _world_lock，无死锁）。
        tick 持 _world_lock 期间照旧 Busy 拒绝（保持 REST 423 语义）。
        """
        # §18.4 指令限速（滑动 60s 窗口，超限直接拒绝）
        self._check_action_rate(user_id)
        if self._world_lock.locked():
            raise StrategyBusyError("世界推进中，请稍后再试")
        async with self._action_lock:
            if self._world_lock.locked():
                raise StrategyBusyError("世界推进中，请稍后再试")
            audit = action in self._AUDIT_ACTIONS
            detail = {k: v for k, v in (params or {}).items()
                      if k not in ("password", "token")}
            try:
                result = await self._handle_action(user_id, action, params)
            except Exception as exc:  # noqa: BLE001 - 失败也留痕后原样抛出
                # 动作中途抛错时共享连接里可能残留半更新（先 save_* 后校验
                # 失败）；必须显式回滚，否则会被下一次任意 _commit() 顺带
                # 提交，造成资源凭空增减。审计单独提交，避免被回滚吃掉。
                try:
                    await self.db.conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                if audit:
                    await self._audit(user_id, action,
                                      {**detail, "ok": False, "error": str(exc)})
                    try:
                        await self._commit()
                    except Exception:  # noqa: BLE001
                        pass
                raise
            if audit:
                await self._audit(user_id, action,
                                  {**detail, "ok": bool(result.get("ok", True))})
            return result

    async def _handle_action(self, user_id: str, action: str,
                             params: Optional[dict] = None) -> dict:
        params = params or {}
        # 总开关关闭时统一拒绝：此前只有 tick 停摆，REST/聊天仍可建世界、
        # 入场、调兵，配置形同虚设。
        if not self.enabled():
            raise StrategyError("仙域争霸未开启")
        try:
            if action == "enter":
                await self.enter_game(user_id, params.get("nickname", ""),
                                      str(params.get("referral", "")),
                                      int(params.get("node_id", 0) or 0))
                return {"ok": True, "msg": "入场成功，新手包已发放"}
            world, p = await self._require_player(user_id)
            wid = world["id"]
            if action == "refresh_tavern":
                self._ensure_joined(p)
                await self.refresh_tavern(p, wid, manual=bool(params.get("manual")))
                return {"ok": True, "msg": "酒馆已刷新"}
            if action == "recruit":
                char = await self.recruit(p, wid, int(params.get("index", 0)),
                                          premium=bool(params.get("premium")))
                return {"ok": True, "msg": f"招募成功：{char['name']}"
                                           f"（{R.RARITY_NAMES.get(char['rarity'])}）",
                        "char": char}
            if action == "dismiss_char":
                return await self.dismiss_char(user_id, int(params["char_id"]))
            if action == "dispatch_task":
                return await self.dispatch_task(
                    user_id, int(params["char_id"]), int(params["node_id"]),
                    str(params.get("task_type", "farm")), int(params.get("hours", 1)),
                    int(params.get("target_node_id", 0) or 0),
                    int(params.get("partner_char_id", 0) or 0))
            if action == "recall_task":
                return await self.recall_task(user_id, int(params["task_id"]))
            if action == "create_army":
                return await self.create_army(
                    user_id, int(params["leader_char_id"]),
                    [int(x) for x in (params.get("member_ids") or [])],
                    int(params["node_id"]), str(params.get("troop_type", "sword_shield")),
                    int(params.get("troops", 100)),
                    siege_engine=bool(params.get("siege_engine")))
            if action == "disband_army":
                return await self.disband_army(user_id, int(params["army_id"]))
            if action == "refill_army":
                return await self.refill_army(user_id, int(params["army_id"]),
                                              int(params.get("troops", 100)))
            if action == "drill_army":
                return await self.drill_army(user_id, int(params["army_id"]))
            if action == "march":
                return await self.march(user_id, int(params["army_id"]),
                                        int(params["node_id"]))
            if action == "retreat":
                return await self.retreat_army(user_id, int(params["army_id"]))
            if action == "siege_choice":
                return await self.set_siege_choice(user_id, int(params["army_id"]),
                                                   str(params.get("choice", "auto")))
            if action == "build":
                return await self.build(user_id, int(params["node_id"]),
                                        str(params["building_id"]))
            if action == "upgrade_building":
                return await self.upgrade_building(
                    user_id, int(params["node_id"]), str(params["building_id"]))
            if action == "upgrade_node":
                return await self.upgrade_node(user_id, int(params["node_id"]))
            if action == "govern_relief":
                return await self.govern_relief(
                    user_id, int(params["node_id"]),
                    str(params.get("resource", "coin")))
            if action == "buy_merit_shop":
                return await self.buy_merit_shop(
                    user_id, str(params["item"]),
                    int(params.get("char_id", 0)),
                    str(params.get("role", "") or ""))
            if action == "buy_honor_shop":
                return await self.buy_honor_shop(
                    user_id, str(params["item"]),
                    str(params.get("role", "") or ""))
            if action == "battle_report":
                return {"ok": True, "report": await self.get_battle_report(
                    user_id, int(params["battle_id"]))}
            # ---------------- P1：势力 / 外交 / 俘虏 / 计略 ----------------
            if action == "faction_create":
                return await self.create_faction(
                    user_id, str(params["name"]), str(params.get("color", "")))
            if action == "faction_cosign":
                return await self.faction_cosign(user_id, str(params["petition_id"]))
            if action == "faction_apply":
                return await self.faction_apply(user_id, int(params["faction_id"]))
            if action == "faction_approve":
                return await self.faction_approve(user_id, str(params["applicant_user"]))
            if action == "faction_reject":
                return await self.faction_reject(user_id, str(params["applicant_user"]))
            if action == "faction_invite":
                return await self.faction_invite(user_id, str(params["target_user"]))
            if action == "faction_invite_respond":
                return await self.faction_respond_invite(
                    user_id, bool(params.get("accept")))
            if action == "faction_quit":
                return await self.faction_quit(user_id)
            if action == "faction_set_position":
                return await self.faction_set_position(
                    user_id, str(params["target_user"]), str(params["position"]))
            if action == "faction_appoint_char":
                return await self.faction_appoint_char(
                    user_id, int(params["char_id"]),
                    str(params.get("position", "")),
                    int(params.get("node_id", 0) or 0))
            if action == "transfer_leader":
                return await self.transfer_leader(user_id, str(params["target_user"]))
            if action == "transfer_node":
                return await self.transfer_node(
                    user_id, int(params.get("node_id", 0)),
                    int(params.get("target_faction_id", 0)))
            if action == "treasury_deposit":
                return await self.treasury_deposit(
                    user_id, str(params["resource"]), int(params["amount"]))
            if action == "treasury_withdraw":
                return await self.treasury_withdraw(
                    user_id, str(params["resource"]), int(params["amount"]))
            if action == "tech_research":
                return await self.tech_research(
                    user_id, str(params["line"]),
                    int(params.get("char_id", 0) or 0))
            if action == "diplomacy_propose":
                return await self.diplomacy_propose(
                    user_id, int(params["target_fid"]), str(params["kind"]),
                    int(params.get("indemnity", 0)))
            if action == "diplomacy_gift":
                return await self.diplomacy_gift(
                    user_id, int(params["target_fid"]),
                    str(params.get("resource", "coin")),
                    int(params.get("amount", 0)))
            if action == "diplomacy_respond":
                return await self.diplomacy_respond(
                    user_id, int(params["from_fid"]), bool(params.get("accept")))
            if action == "rebuild_capital":
                return await self.rebuild_capital(user_id, int(params["node_id"]))
            # ---------------- v4.1.0 跨模块动作（C/D 实现，B 负责分发）---------
            for _name, _fn_name, _builder in (
                    ("faction_task_refresh", "faction_task_refresh",
                     lambda: (user_id,)),
                    ("faction_task_claim", "faction_task_claim",
                     lambda: (user_id, int(params["task_id"]))),
                    ("mobilization_issue", "mobilization_issue",
                     lambda: (user_id, str(params["kind"]))),
                    ("capital_relocate", "capital_relocate",
                     lambda: (user_id, int(params["node_id"]))),
                    ("rebuild_donate", "rebuild_donate",
                     lambda: (user_id, int(params["amount"]))),
                    ("transfer_captive", "transfer_captive",
                     lambda: (user_id, int(params["char_id"]),
                              int(params["to_node_id"]))),
                    ("arena_register", "arena_register",
                     lambda: (user_id, str(params.get("bracket", "")))),
            ):
                if action == _name:
                    fn = getattr(self, _fn_name, None)
                    if fn is None:
                        raise StrategyError(f"动作「{action}」暂未开放")
                    return await fn(*_builder())
            if action == "buy_shield":
                return await self.buy_shield(user_id, int(params["node_id"]))
            if action == "persuade":
                return await self.persuade_captive(
                    user_id, int(params["char_id"]),
                    int(params.get("persuader_char_id", 0)))
            if action == "release":
                return await self.release_captive(user_id, int(params["char_id"]))
            if action == "ransom_set":
                return await self.set_ransom(user_id, int(params["char_id"]),
                                             int(params["amount"]))
            if action == "ransom_pay":
                return await self.pay_ransom(user_id, int(params["char_id"]))
            if action == "dispatch_plot":
                return await self.dispatch_plot(
                    user_id, int(params["char_id"]), str(params["plot_type"]),
                    int(params["target_id"]))
            # ---------------- P2：hex 手操战役 / 擂台 ----------------
            if action == "intervene_siege":
                return await self.intervene_siege(
                    user_id, _as_int(params.get("node_id"), "node_id"))
            if action == "sortie":
                return await self.sortie(
                    user_id, _as_int(params.get("node_id"), "node_id"))
            if action == "battle_state":
                return await self.battle_state(
                    user_id, _as_int(params.get("battle_id"), "battle_id"))
            if action == "battle_deploy":
                return await self.battle_deploy(
                    user_id, _as_int(params.get("battle_id"), "battle_id"),
                    params.get("placements") or [])
            if action == "battle_auto_deploy":
                return await self.battle_auto_deploy(
                    user_id, _as_int(params.get("battle_id"), "battle_id"))
            if action == "battle_set_ambush":
                return await self.battle_set_ambush(
                    user_id, _as_int(params.get("battle_id"), "battle_id"),
                    _as_int(params.get("uid"), "uid"),
                    _as_int(params.get("q"), "q"),
                    _as_int(params.get("r"), "r"))
            if action == "battle_set_formation":
                return await self.battle_set_formation(
                    user_id, _as_int(params.get("battle_id"), "battle_id"),
                    _as_int(params.get("uid"), "uid"),
                    str(params.get("formation", "")))
            if action == "battle_start":
                return await self.battle_start(
                    user_id, _as_int(params.get("battle_id"), "battle_id"))
            if action == "battle_action":
                return await self.battle_action(
                    user_id, _as_int(params.get("battle_id"), "battle_id"),
                    str(params.get("act") or ""),
                    params.get("params") if isinstance(params.get("params"), dict) else {})
            if action == "battle_delegate":
                return await self.battle_delegate(
                    user_id, _as_int(params.get("battle_id"), "battle_id"),
                    bool(params.get("on", True)))
            if action == "battle_replay":
                return await self.get_battle_replay(
                    user_id, _as_int(params.get("battle_id"), "battle_id"))
            if action == "duel_arena_open":
                return await self.duel_arena_open(
                    user_id, int(params["char_id"]), int(params.get("stake", 100)))
            if action == "duel_arena_accept":
                return await self.duel_arena_accept(
                    user_id, str(params["duel_id"]), int(params["char_id"]))
            if action == "duel_arena_list":
                return {"ok": True, "duels": await self.duel_arena_list(user_id)}
            # ---------------- P3：互市 / 匪寨 / 粮队 / 赛季 ----------------
            if action == "market_list":
                return await self.market_list(
                    user_id, int(params["node_id"]), str(params["item_type"]),
                    int(params["amount"]), int(params["unit_price"]))
            if action == "market_buy":
                return await self.market_buy(user_id, int(params["order_id"]),
                                             int(params.get("amount", 0)))
            if action == "market_cancel":
                return await self.market_cancel(user_id, int(params["order_id"]))
            if action == "market_orders":
                return {"ok": True,
                        "orders": await self.market_orders(
                            user_id, int(params.get("node_id", 0)))}
            if action == "raid_bandit":
                return await self.raid_bandit(user_id, int(params["army_id"]),
                                              int(params.get("node_id", 0)))
            if action == "send_convoy":
                return await self.send_convoy(
                    user_id, int(params["from_node"]), int(params["to_node"]),
                    int(params["grain"]), int(params.get("escort_army_id", 0)))
            if action == "claim_catchup":
                return await self.claim_catchup(user_id)
            if action == "claim_carryover":
                return await self.claim_carryover(user_id, int(params["army_id"]))
            if action == "bid_wanderer":
                return await self.bid_wanderer(
                    user_id, int(params["auction_id"]),
                    int(params.get("amount", 0) or 0))
            if action == "cancel_wanderer_bid":
                return await self.cancel_wanderer_bid(
                    user_id, int(params["auction_id"]))
            if action == "recruit_wanderer":
                return await self.recruit_wanderer(user_id, int(params["char_id"]))
            if action == "set_notify_pref":
                return await self.set_notify_pref(user_id, int(params.get("level", 1)))
            if action == "set_guide":
                return await self.set_guide_visible(
                    user_id, bool(params.get("visible", True)))
            raise StrategyError(f"未知动作：{action}")
        except StrategyError:
            raise
        except KeyError as e:
            raise StrategyError(f"缺少参数：{e}") from e
        except (ValueError, TypeError) as e:
            # JSON 里传 null/对象时 int() 抛 TypeError（而非 ValueError），
            # 此前会穿透成 500 并回显内部异常文本。
            raise StrategyError(f"参数无效：{e}") from e

    @staticmethod
    def _ensure_joined(p: dict):
        if not p:
            raise StrategyError("尚未入场")





