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
import secrets
import time
from typing import Any, Optional

from ..core import strategy_rules as R
from ..core import strategy_hex as H
from ..data.migration import (
    _ensure_strategy_v70, _ensure_strategy_v71, _ensure_strategy_v72,
    _ensure_strategy_v74, _ensure_strategy_v75, _ensure_strategy_v76,
    _ensure_strategy_v77, _ensure_strategy_v79, _ensure_strategy_v80,
    _ensure_strategy_v81, _ensure_strategy_v82, _ensure_strategy_v83,
    _ensure_strategy_v84, _ensure_strategy_world_log_v87,
)
from .strategy_errors import StrategyBusyError, StrategyError
from .strategy_faction_mixin import StrategyFactionMixin
from .strategy_captive_mixin import StrategyCaptiveMixin
from .strategy_plot_mixin import StrategyPlotMixin
from .strategy_battle_mixin import StrategyBattleMixin
from .strategy_p3_mixin import StrategyP3Mixin
from .strategy_threat_mixin import StrategyThreatMixin, _safe_int

logger = logging.getLogger("astrbot")

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")

#: 任务产出资源展示名（面板「进行中任务」与派遣下拉共用）
TASK_OUTPUT_TEXT = {
    k: R.RESOURCE_NAMES[k] for k in (
        "coin", "grain", "iron", "token", "intel", "build", "train",
        "merit", "heal", "tech", "grain_move", "loot", "plot")
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

class StrategyManager(StrategyThreatMixin, StrategyFactionMixin,
                      StrategyCaptiveMixin, StrategyPlotMixin,
                      StrategyBattleMixin, StrategyP3Mixin):
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
        # 当前处于「延迟提交事务」内的任务（v4.3.0 由 _tick_task 泛化）：
        # tick 大事务 / poll 轮询事务 / handle_action 动作事务共用此标记，
        # 任务内的 _commit() 只暂存，由包装层统一提交/回滚，避免
        # autocommit 下半提交与“伪回滚”。
        self._tx_task = None
        # v4.3.0：世界私密种子盐（world.data.secret，建世/首次加载时生成），
        # 混入所有 _rng 种子——此前种子全部由公开可算输入构成
        # （user_id/world_id/tick/时间桶），可离线复现酒馆候选与劫掠掷点。
        self._world_secret = ""
        # 最近活跃玩家（user_id → 秒级时间戳）：网页面板每次拉快照时刷新。
        # 仅内存、不落库：用于判断野战遭遇是否值得暂停 tick 开手操窗口；
        # 避免在 get_state 里写库与 tick 大事务抢同一连接提交。
        self._recent_players: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 通知分级推送（§12.5：strategy.notify，三档 notify_pref）
    # ------------------------------------------------------------------
    #: kind → 触发所需最低通知档位（0 仅手操窗口 / 1 +己方被攻 / 2 全部事件）
    _NOTIFY_MIN_LEVEL = {
        "battle_open": 0,
        "battle_turn": 0,
        "battle_result": 0,
        "duel_invite": 0,
        "siege_alert": 1,
        "trade_route_cut": 1,
        "convoy_arrived": 1,
        "convoy_raided": 1,
        "supply_arrived": 1,
        "supply_lost": 1,
        "supply_raided": 1,
        "wanderer_auction": 1,
        "bandit_raid": 1,
        "captive_transfer": 1,
        "captive_evicted": 1,
        "faction_task": 1,
        "faction_event": 1,
        # 计略结果（含执行者败露被擒）是玩家亲自派出的行动，默认档也须可见；
        # 否则玩家只能靠猜民心/忠诚变化得知结果。
        "plot_result": 1,
        # v4.6.1：NPC 自主宣战 / 求和提案，属于切身利害，默认档可见
        "war_declared": 1,
        "peace_offer": 1,
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
            # v4.3.0：全程持 _world_lock——此前与 tick/玩家动作并发时，旧世界
            # 可能在 ended 后仍被在途 tick 推进（读写已归档世界）；两个 GM
            # 并发开世界还会产生双 active。season_no 交由 _create_world 的
            # MAX+1 口径（C-M9），不再依赖 get_world 的陈旧读（跳号/撞号）。
            async with self._world_lock:
                world = await self.get_world()
                if world:
                    world["state"] = "ended"
                    await self.save_world(world)
                    await self._commit()
                new_world = await self._create_world(
                    season_no=0, template_id=template)
            return {"ok": True,
                    "msg": f"🌏 已开新世界 #{new_world['id']}"
                           f"（模板 {new_world['template_id']}，"
                           f"赛季 S{new_world.get('season_no', '?')}）"}
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
            tick_no = int(world["tick_no"])
            # v4.3.0 三项约束（与自然事件路径 _step_events 同口径）：
            # 1) 同事件去重：仍在进行中不得重复注入（效果叠加可放大到离谱）
            dup = await self._fetchone(
                "SELECT id FROM strategy_events WHERE world_id=? AND event_id=? "
                "AND end_tick>? LIMIT 1", (world["id"], ev["id"], tick_no))
            if dup:
                raise StrategyError(
                    f"事件「{ev.get('name', eid)}」仍在进行中（#{dup['id']}），"
                    f"到期后方可再次注入")
            target: dict = {}
            if ev.get("scope") == "region":
                # 2) node_types/node_terrain 过滤：此前固定 nodes[0]/只查 id，
                #    可把「洪涝」注到矿脉等不适用类型上错误生效
                trigger = ev.get("trigger") or {}
                nodes = await self.get_nodes(world["id"])
                cands = nodes
                if trigger.get("node_types"):
                    cands = [n for n in cands
                             if n["type"] in trigger["node_types"]]
                if trigger.get("node_terrain"):
                    cands = [n for n in cands
                             if n["terrain"] in trigger["node_terrain"]]
                node = None
                if len(args) > 1 and str(args[1]).lstrip("-").isdigit():
                    node = next((n for n in nodes
                                 if n["id"] == int(args[1])), None)
                    if not node:
                        raise StrategyError(f"据点不存在：{args[1]}")
                    if node not in cands:
                        raise StrategyError(
                            f"「{node['name']}」的类型/地形不适用该事件")
                elif cands:
                    # 3) 默认目标随机选取（rng 已混世界密钥），不再恒为 nodes[0]
                    node = self._rng("gm", world["id"], tick_no).choice(cands)
                if not node:
                    raise StrategyError(f"没有适用「{ev.get('name', eid)}」的据点")
                target = {"node_ids": [node["id"]], "node_name": node["name"]}
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
                              "effects": ev.get("effects") or [],
                              "gm": True}, ensure_ascii=False)))
            await self._apply_event_onset(
                world, ev, target, self._rng("gm", world["id"], tick_no))
            # 与自然路径同式写入冷却：GM 注入后自然掷点不会立刻叠一发同款
            data = world.get("data") or {}
            cd = data.get("event_cd") or {}
            cd[ev["id"]] = tick_no + duration + int(ev.get("cooldown_ticks", 32))
            data["event_cd"] = cd
            world["data"] = data
            await self.save_world(world)
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
            # v4.5.7：玩家势力无国库，补给直接进势力主钱包；NPC 仍入内部国库
            if str(f.get("kind") or "") == "player":
                world = await self.get_world()
                lp = await self._leader_player(int((world or {}).get("id") or 0), f)
                if not lp:
                    raise StrategyError("该势力没有可入账的势力主玩家")
                lp["coin"] = max(0, int(lp.get("coin", 0) or 0) + coin)
                lp["grain"] = max(0, int(lp.get("grain", 0) or 0) + grain)
                await self.save_player(lp)
                await self._commit()
                logger.info(f"[strategy][GM] 势力平衡：#{fid} 势力主铜钱{coin:+} 粮草{grain:+}")
                return {"ok": True,
                        "msg": f"⚖️ 已调整「{f['name']}」势力主钱包："
                               f"铜钱 {coin:+}、粮草 {grain:+}"
                               f"（现 {lp['coin']} / {lp['grain']}）"}
            treasury = f.get("treasury") or {}
            treasury["coin"] = max(0, int(treasury.get("coin", 0)) + coin)
            treasury["grain"] = max(0, int(treasury.get("grain", 0)) + grain)
            f["treasury"] = treasury
            await self.save_faction(f)
            await self._commit()
            logger.info(f"[strategy][GM] 势力平衡：#{fid} 铜钱{coin:+} 粮草{grain:+}")
            return {"ok": True,
                    "msg": f"⚖️ 已调整「{f['name']}」内部国库：铜钱 {coin:+}、粮草 {grain:+}"
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
        """确定性随机：同 seed 同结果（§7.3 可复现）。

        v4.3.0：混入世界服务端密钥（world.data.secret）——此前种子全部
        由公开值构成，玩家可离线重放掷点序列精确预判结果。
        """
        payload = "|".join(str(s) for s in (self._world_secret,) + seeds)
        h = hashlib.md5(payload.encode("utf-8")).hexdigest()
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
        """当前协程是否处于延迟提交事务内（tick/poll/handle_action 共用；
        口径与 :meth:`_commit` 一致）。"""
        return self._tx_task is not None and self._tx_task is asyncio.current_task()

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

    # ------------------------------------------------------------------
    # 江湖纪事（v4.7.0）：结构化行动日志
    # ------------------------------------------------------------------
    async def _log_action(self, world: dict, kind: str, action: str,
                          *, actor_faction: int = 0, actor_name: str = "",
                          target_faction: int = 0, target_name: str = "",
                          node_id: int = 0, node_name: str = "",
                          success: int = 0, summary: str = "",
                          detail: Optional[dict] = None,
                          visibility: str = "public",
                          publish_at: int = 0) -> int:
        """写入一条江湖纪事；任何异常都静默，日志不得影响主流程。"""
        try:
            wid = int((world or {}).get("id") or 0)
            text = str(summary or "").strip()
            if wid <= 0 or not text:
                return 0
            now = _now()
            return await self._execute(
                "INSERT INTO strategy_world_log (world_id, tick, created_at, "
                "publish_at, kind, action, actor_faction, actor_name, "
                "target_faction, target_name, node_id, node_name, success, "
                "summary, detail_json, visibility) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (wid, int((world or {}).get("tick_no", 0) or 0), now,
                 int(publish_at or now), str(kind or "event"),
                 str(action or ""), int(actor_faction or 0), str(actor_name or ""),
                 int(target_faction or 0), str(target_name or ""),
                 int(node_id or 0), str(node_name or ""),
                 1 if success else 0, text[:500],
                 json.dumps(detail or {}, ensure_ascii=False, default=str),
                 str(visibility or "public")))
        except Exception:  # noqa: BLE001 - 日志失败不影响玩法
            logger.debug("[strategy] 江湖纪事写入失败", exc_info=True)
            return 0

    async def get_world_logs(self, world_id: int,
                             viewer_fids: Optional[set[int]] = None,
                             kind: str = "", before_id: int = 0,
                             limit: int = 20) -> dict:
        """按可见性分页读取江湖纪事（public + 本方/盟友/被针对的 faction）。"""
        limit = max(1, min(100, int(limit or 20)))
        where = ["world_id=?", "publish_at<=?"]
        params: list = [int(world_id), _now()]
        if kind:
            where.append("kind=?")
            params.append(str(kind))
        if int(before_id or 0) > 0:
            where.append("id<?")
            params.append(int(before_id))
        fids = sorted({int(x) for x in (viewer_fids or set()) if int(x or 0) > 0})
        if fids:
            ph = ",".join("?" for _ in fids)
            where.append(
                "(visibility='public' OR (visibility='faction' AND "
                f"(actor_faction IN ({ph}) OR target_faction IN ({ph}))))")
            params.extend(fids)
            params.extend(fids)
        else:
            where.append("visibility='public'")
        params.append(limit + 1)
        rows = await self._fetchall(
            "SELECT * FROM strategy_world_log WHERE " + " AND ".join(where) +
            " ORDER BY id DESC LIMIT ?", tuple(params))
        has_more = len(rows) > limit
        items = []
        for row in rows[:limit]:
            item = self._decode("strategy_world_log", row) or {}
            icon, name = R.WORLD_LOG_KINDS.get(
                str(item.get("kind") or ""), ("📜", "纪事"))
            item["icon"] = icon
            item["kind_name"] = name
            items.append(item)
        return {"items": items, "has_more": has_more}

    async def get_world_logs_for_user(self, user_id: str, kind: str = "",
                                      before_id: int = 0,
                                      limit: int = 20) -> dict:
        """网页端入口：按玩家所属势力/联盟计算可见范围。"""
        world = await self.get_world()
        if not world:
            return {"items": [], "has_more": False}
        wid = int(world["id"])
        p = await self.get_player(str(user_id), wid)
        fids: set[int] = set()
        fid = int((p or {}).get("faction_id") or 0)
        if fid > 0:
            fids.add(fid)
            f = await self.get_faction(fid)
            if f:
                try:
                    fids |= await self._alliance_ids(wid, f)
                except Exception:  # noqa: BLE001 - 联盟解析失败只影响可见范围
                    pass
        return await self.get_world_logs(
            wid, fids, kind=kind, before_id=before_id, limit=limit)

    async def _step_world_log_cleanup(self, world: dict) -> int:
        """江湖纪事保留策略：按天数 + 条数上限回收（可在配置中调整）。"""
        cfg = self.main_cfg().get("world_log") or {}
        keep_days = max(0, int(cfg.get("keep_days", 14) or 0))
        keep_rows = max(0, int(cfg.get("keep_rows", 3000) or 0))
        wid = int(world["id"])
        removed = 0
        if keep_days > 0:
            removed += await self._exec_count(
                "DELETE FROM strategy_world_log WHERE world_id=? AND created_at<?",
                (wid, _now() - keep_days * 86400))
        if keep_rows > 0:
            removed += await self._exec_count(
                "DELETE FROM strategy_world_log WHERE world_id=? AND id NOT IN "
                "(SELECT id FROM strategy_world_log WHERE world_id=? "
                "ORDER BY id DESC LIMIT ?)", (wid, wid, keep_rows))
        return int(removed or 0)

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
        await _ensure_strategy_v79(self.db.conn)
        await _ensure_strategy_v80(self.db.conn)
        await _ensure_strategy_v81(self.db.conn)
        await _ensure_strategy_v82(self.db.conn)
        await _ensure_strategy_v83(self.db.conn)
        await _ensure_strategy_v84(self.db.conn)
        await _ensure_strategy_world_log_v87(self.db.conn)
        await self._commit()
        self._tables_ready = True

    # ------------------------------------------------------------------
    # 行编解码（JSON 列 ↔ dict）
    # ------------------------------------------------------------------
    _JSON_COLS = {
        "strategy_nodes": ("buildings_json", "stock_json", "output_json", "adj_json",
                           "siege_state_json", "npc_garrison_json", "contribution_json",
                           "hatred_json", "lair_json", "construction_json"),
        "strategy_factions": ("personality_json", "treasury_json", "tech_json",
                              "diplomacy_json", "salary_json"),
        "strategy_players": ("honor_json", "daily_output_log_json", "notify_pref_json",
                             "tutorial_json", "achievements_json", "newcomer_json",
                             "rebuild_pay_log_json", "tavern_json"),
        "strategy_characters": ("stats_json", "traits_json", "personality_json",
                                "aptitude_json", "bonds_json", "location_json"),
        "strategy_armies": ("characters_json", "path_json"),
        "strategy_coalitions": ("member_ids_json",),
        "strategy_tasks": ("payout_json",),
        "strategy_battles": ("terrain_json", "armies_snapshot_json", "result_json",
                             "session_json"),
        "strategy_events": ("target_json", "data_json"),
        "strategy_worlds": ("data_json",),
        "strategy_world_log": ("detail_json",),
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
                                    "armies_snapshot_json", "member_ids_json") else {}
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
        world = self._decode("strategy_worlds", row)
        if world is not None:
            await self._ensure_world_secret(world)
        return world

    async def _ensure_world_secret(self, world: dict) -> None:
        """加载/回填世界私密种子盐（v4.3.0，rng 防预测）。

        旧世界缺 secret 时补生成并落库（一次性迁移）；同时把值缓存到
        self._world_secret 供 _rng 混入。
        """
        data = world.get("data") or {}
        sec = str(data.get("secret") or "")
        if not sec:
            sec = secrets.token_hex(16)
            data["secret"] = sec
            world["data"] = data
            await self.save_world(world)
            await self._commit()
        self._world_secret = sec

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
                governor_char_id=?, buildings_json=?, defense=?, morale=?,
                stock_json=?, output_json=?,
                pos_x=?, pos_y=?, adj_json=?, siege_state_json=?, npc_garrison_json=?,
                protect_until=?, contribution_json=?, hatred_json=?, respawn_at=?,
                lair_json=?, potential=?, construction_json=?, develop_ready_at=?
            WHERE world_id=? AND id=?
        """, (
            node.get("name", ""), node.get("type", "ruin"), node.get("terrain", "plain"),
            int(node.get("level", 1)), int(node.get("owner_faction", 0)),
            int(node.get("governor_char_id", 0) or 0),
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
            int(R.node_potential(node)),
            json.dumps(node.get("construction") or {}, ensure_ascii=False),
            int(node.get("develop_ready_at", 0) or 0),
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
                diplomacy_json=?, alliance_id=?, salary_json=?
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
            json.dumps(f.get("salary") or {}, ensure_ascii=False),
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
                role=?, rarity=?, profession=?, stats_json=?, traits_json=?, personality_json=?,
                aptitude_json=?,
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
            json.dumps(c.get("personality") or {}, ensure_ascii=False),
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
                rarity, profession, stats_json, traits_json, personality_json, aptitude_json, exp, level,
                loyalty, injury, stamina, status, appointed_position, appointed_node_id,
                auction_cooldown_until, location_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(c.get("world_id", 0)), int(c.get("faction_id", 0)), c.get("owner_user_id", ""),
            c.get("name", ""), c.get("role", "general"), c.get("rarity", "common"),
            c.get("profession", "sword"),
            json.dumps(c.get("stats") or {}, ensure_ascii=False),
            json.dumps(c.get("traits") or [], ensure_ascii=False),
            json.dumps(c.get("personality") or {}, ensure_ascii=False),
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
                siege_engine=?, reorganize_until_tick=?, starve_ticks=?, coalition_id=?
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
            int(a.get("coalition_id", 0) or 0),
            int(a["id"]),
        ))

    async def insert_army(self, a: dict) -> int:
        aid = await self._execute("""
            INSERT INTO strategy_armies (world_id, faction_id, owner_user_id, name, node_id,
                characters_json, troop_type, veteran, troops, grain, morale, state,
                coalition_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            int(a.get("world_id", 0)), int(a.get("faction_id", 0)), a.get("owner_user_id", ""),
            a.get("name", ""), int(a.get("node_id", 0)),
            json.dumps(a.get("characters") or [], ensure_ascii=False),
            a.get("troop_type", "sword_shield"), int(a.get("veteran", 0)),
            int(a.get("troops", 0)), int(a.get("grain", 0)), int(a.get("morale", 100)),
            a.get("state", "idle"), int(a.get("coalition_id", 0) or 0),
        ))
        a["id"] = aid
        return aid

    async def delete_army(self, army_id: int):
        """删除军团；若其属于联军，先解散联军（避免悬挂成员）。"""
        row = await self._fetchone(
            "SELECT id, coalition_id FROM strategy_armies WHERE id=?", (int(army_id),))
        cid = int((row or {}).get("coalition_id") or 0)
        if cid > 0:
            await self._dissolve_coalition(cid, reason="军团解散/覆没")
        await self.db.conn.execute("DELETE FROM strategy_armies WHERE id=?", (army_id,))

    # ------------------------------------------------------------------
    # 联军（合军）：两支己方军团统一行动，单位仍独立结算
    # ------------------------------------------------------------------
    async def get_coalition(self, coalition_id: int) -> Optional[dict]:
        row = await self._fetchone(
            "SELECT * FROM strategy_coalitions WHERE id=?", (int(coalition_id),))
        return self._decode("strategy_coalitions", row)

    async def get_coalitions(self, world_id: int,
                             owner_user_id: str = "") -> list[dict]:
        sql = "SELECT * FROM strategy_coalitions WHERE world_id=?"
        params: list = [int(world_id)]
        if owner_user_id:
            sql += " AND owner_user_id=?"
            params.append(owner_user_id)
        return self._decode_all(
            "strategy_coalitions", await self._fetchall(sql, tuple(params)))

    async def save_coalition(self, c: dict):
        await self.db.conn.execute("""
            UPDATE strategy_coalitions SET world_id=?, faction_id=?,
                owner_user_id=?, node_id=?, target_node_id=?, leader_army_id=?,
                member_ids_json=?, status=?
            WHERE id=?
        """, (
            int(c.get("world_id", 0)), int(c.get("faction_id", 0)),
            str(c.get("owner_user_id") or ""), int(c.get("node_id", 0)),
            int(c.get("target_node_id", 0) or 0), int(c.get("leader_army_id", 0)),
            json.dumps(c.get("member_ids") or [], ensure_ascii=False),
            str(c.get("status") or "idle"), int(c["id"]),
        ))

    async def insert_coalition(self, c: dict) -> int:
        cid = await self._execute("""
            INSERT INTO strategy_coalitions (world_id, faction_id, owner_user_id,
                node_id, target_node_id, leader_army_id, member_ids_json, status,
                created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            int(c.get("world_id", 0)), int(c.get("faction_id", 0)),
            str(c.get("owner_user_id") or ""), int(c.get("node_id", 0)),
            int(c.get("target_node_id", 0) or 0), int(c.get("leader_army_id", 0)),
            json.dumps(c.get("member_ids") or [], ensure_ascii=False),
            str(c.get("status") or "idle"), int(c.get("created_at") or _now()),
        ))
        c["id"] = cid
        return cid

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

    def _gc_world_data(self, data: dict, tick_no: int) -> None:
        """清理 world.data 过期临时键，避免 JSON 无限膨胀。

        v4.3.0：plot_cd（按 tick）、persuade_cd（按时间）、已无读取方的
        sabotage 死标记、过期 winds；ransoms 已改由 ``_step_captives``
        按「仍在押 + 关押方一致」每 tick 回收。

        v4.6.1：同时自愈损坏的临时容器（非 dict 的 CD / 日限桶 / 赎金表、
        非数值的日限计数），避免旧档或人工改库让玩家动作永久抛错。
        """
        if not isinstance(data, dict):
            return
        now = _now()
        # 容器本身损坏（字符串/列表）时直接重置，否则后续 setdefault 后
        # 按 key 写入会抛 AttributeError，玩家动作永久失败到下一次人工改库。
        for key in ("plot_cd", "persuade_cd"):
            raw = data.get(key)
            if raw is not None and not isinstance(raw, dict):
                data[key] = {}
        plot_cd = data.get("plot_cd")
        if isinstance(plot_cd, dict):
            data["plot_cd"] = {k: _safe_int(v) for k, v in plot_cd.items()
                               if _safe_int(v) > int(tick_no)}
        pcd = data.get("persuade_cd")
        if isinstance(pcd, dict):
            data["persuade_cd"] = {k: _safe_int(v) for k, v in pcd.items()
                                   if _safe_int(v) > now}
        # sabotage 死标记：计略结算已不再写入，残留条目直接清空
        data.pop("sabotage", None)
        winds = data.get("winds")
        if isinstance(winds, list):
            try:
                wind_keep = int(self._npc_wind_cfg().get("keep", 40))
            except Exception:  # noqa: BLE001 - 配置异常时退回旧上限
                wind_keep = 40
            data["winds"] = [w for w in winds
                             if isinstance(w, dict)
                             and _safe_int(w.get("visible_at", 0)) + 7 * 86400 > now
                             ][-max(1, wind_keep):]
        elif winds is not None:
            # 旧档 / 人工改库可能把 winds 写成字符串或对象；直接重置为列表
            data["winds"] = []
        # 过期日限桶：只保留今天；计数非法时按 0 计（不阻塞劝降/奖赏）
        today = time.strftime("%Y%m%d", time.localtime(now))
        for key in ("persuade_daily", "loyalty_reward_daily"):
            daily = data.get(key)
            if daily is not None and not isinstance(daily, dict):
                data[key] = {}
                continue
            if not isinstance(daily, dict):
                continue
            for owner, bucket in list(daily.items()):
                if not isinstance(bucket, dict):
                    daily.pop(owner, None)
                    continue
                keep = {k: _safe_int(v, 0) for k, v in bucket.items()
                        if str(k) == today}
                if keep:
                    daily[owner] = keep
                else:
                    daily.pop(owner, None)
        # 赎金表：非 dict 的损坏值会在 set_ransom / pay_ransom 上抛错
        ransoms = data.get("ransoms")
        if ransoms is not None and not isinstance(ransoms, dict):
            data["ransoms"] = {}

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
        # v4.3.0：建世即生成私密种子盐（rng 防离线预测），随 data_json 落库
        world_secret = secrets.token_hex(16)
        self._world_secret = world_secret
        wid = await self._execute("""
            INSERT INTO strategy_worlds (season_no, template_id, started_at, ends_at,
                state, last_tick_at, tick_no, data_json)
            VALUES (?,?,?,?, 'active',?,0,?)
        """, (int(season_no), tpl.get("template_id", template_id), now,
              now + season_days * 86400, now,
              json.dumps({"secret": world_secret}, ensure_ascii=False)))
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
                # v81 所在地机制：潜力在地图生成时固定（模板可逐节点覆盖）
                "potential": int(n.get("potential") or
                                 R.NODE_POTENTIAL_BY_TYPE.get(ntype, 3)),
                "construction": {},
                "develop_ready_at": 0,
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
            # 主城默认 2 级，但不得超过据点潜力（§10.2；否则会出现
            # “潜力 1 · 当前 2 级”且守军上限按 2 级计算）。
            cap_node["level"] = min(2, R.node_potential(cap_node))
            cap_node["defense"] = R.city_defense_max(cap_node["level"]) \
                if cap_node["type"] in ("city", "pass", "branch") else 0
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
                    protect_until, contribution_json, potential, construction_json,
                    develop_ready_at)
                VALUES (?,?,?,?,?,?,?,'[]',?,?,'{}','{}',?,?,?,'{}',?,0,'{}',?,'{}',0)
            """, (
                node["id"], wid, node["name"], node["type"], node["terrain"], node["level"],
                node["owner_faction"], node["defense"], node["morale"],
                node["pos_x"], node["pos_y"],
                json.dumps(node["adj"]),
                json.dumps(node["npc_garrison"]),
                int(node["potential"]),
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
        # v4.3.0：昵称截断——过长昵称会撑爆势力名/面板
        nick = str(nickname or user_id).strip()[:16] or str(user_id)[:16]
        fid = await self._execute("""
            INSERT INTO strategy_factions (world_id, name, color, kind, leader_id,
                personality_json, treasury_json, reputation, level, member_cap,
                capital_node_id, faction_state, alive, diplomacy_json)
            VALUES (?,?,?,'player',?,'{}','{}',0,1,10,0,'normal',1,'{}')
        """, (wid, f"{nick}的个人势力", color, user_id))
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
                         "loyalty": 90,
                         # §3：新角色出生于起始据点（主城）
                         "location": {"node_id": int(node_id)}})
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
            (self.main_cfg().get("protection") or {}).get("newbie_days", 4)))
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
        退化为任意非遗迹无主节点。分配后 4 天新城保护，避免新手一入场被推平。
        """
        scfg = self.main_cfg().get("starter_pack") or {}
        prefer = [str(t) for t in (scfg.get("home_node_types") or
                                   ["branch", "market", "grain", "mine"])]
        garrison = max(0, int(scfg.get("home_garrison", 300)))
        protect_days = max(1, int(
            (self.main_cfg().get("protection") or {}).get("newbie_days", 4)))
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
        """修复旧存档：补势力主成员行 + 给从未领过据点的新手补发起始据点，
        并校正 idle 角色的非法所在地。

        补发/成员修复仅对「个人势力」（单成员）生效，且 home_granted
        只会发放一次；因此赛季中后期被推平的玩家不会反复白嫖新据点，
        仍须走灵石重建。所在地自愈对所有玩家势力生效。
        """
        f = await self.get_faction(int(p.get("faction_id") or 0))
        if not f:
            return False
        changed = False
        # 补发起始据点只针对个人势力；多人势力玩家仍继续执行下方所在地
        # 自愈（旧存档 idle 落在敌方/已删除节点的问题与势力类型无关）。
        if f.get("kind") == "player":
            members = await self.get_members(f["id"])
            if str(f.get("leader_id") or "") == p["user_id"] and \
                    not any(m["user_id"] == p["user_id"] for m in members):
                await self.upsert_member(f["id"], p["user_id"],
                                         position="leader",
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
        # §3 老存档自愈：空闲角色没有所在地、或所在地已删除/非法/敌方
        # 时，落到主城/最近己方（含联盟）可用据点，避免旧角色既不能派遣
        # 也不能编军、或以 idle 状态滞留敌城。只处理 idle，绝不改写
        # 军团/任务/俘虏/押运角色的状态与落点。
        home = int(f.get("capital_node_id") or 0)
        home_node = await self.get_node(world["id"], home) if home else None
        if not home_node or int(home_node.get("owner_faction") or 0) != int(f["id"]) \
                or str(home_node.get("type") or "") not in R.CHAR_DWELL_NODE_TYPES:
            home = await self._nearest_own_node(
                world, {"faction_id": int(f["id"])}, home, dwell_only=True) or 0
        ids = await self._alliance_ids(int(world["id"]), f) or set()
        ids.add(int(f["id"]))
        rows = await self._fetchall(
            "SELECT * FROM strategy_characters WHERE world_id=? "
            "AND owner_user_id=? AND status='idle'",
            (world["id"], p["user_id"]))
        for row in rows:
            c = self._decode("strategy_characters", row)
            cur = R.char_node_id(c)
            if cur > 0:
                node = await self.get_node(world["id"], cur)
                if node and int(node.get("owner_faction") or 0) in ids and \
                        str(node.get("type") or "") in R.CHAR_DWELL_NODE_TYPES:
                    continue
                dest = await self._char_fallback_location(
                    int(world["id"]), c, cur)
                if dest <= 0:
                    dest = home
            else:
                dest = home
            if cur == dest:
                continue
            c["location"] = ({"node_id": int(dest)} if dest > 0 else {})
            await self.save_char(c)
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
        # 联盟不共享经济：招募品质只吃本势力中郎将官职，不看盟主/盟友。
        bonus = await self._faction_position_bonus(f, include_alliance=False)
        # 酒馆分号：本地招募稀有 +3%/级（§6.6，取本势力最高等级）
        best = 0
        for node in await self.get_nodes(int(f["world_id"])):
            if int(node.get("owner_faction") or 0) == int(f["id"]):
                best = max(best, self._building_level(node, "tavern_branch"))
        # 善缘（good_karma）角色在册：每名 +5%，封顶 +15%（§4.9）
        # 陨落/被俘/押运中的角色不在名册内，不再提供招募品质加成
        chars = [c for c in await self.get_chars(
            faction_id=int(f["id"]), world_id=int(f["world_id"]))
            if c.get("status") not in ("dead", "captive", "transfer", "wander")]
        karma = R.personality_recruit_bonus(chars)
        return (float(bonus.get("recruit_quality", 0.0)) + 0.03 * min(5, best)
                + karma)

    async def _tavern(self, player: dict, world_id: int) -> dict:
        """取/刷新酒馆候选（4 小时一刷，手刷 300 币 ×3/日）。

        v4.3.0：仅到期刷新，删除「候选抽空自动重掷」——同一小时内种子
        相同，抽空重掷会原样复刻同一批候选，可反复招募相同角色无限供货。
        另以 roll_seq 递增作盐：即使到期刷新落在同一时间桶，候选也不同批。
        """
        cfg = (self.main_cfg().get("recruit") or {})
        tavern = player.get("tavern") or {}
        now = _now()
        refresh_sec = int(cfg.get("refresh_hours", 4)) * 3600
        if now >= int(tavern.get("refresh_at", 0)):
            qb = await self._recruit_quality_bonus(int(player.get("faction_id") or 0))
            seq = int(tavern.get("roll_seq", 0) or 0) + 1
            tavern = self._roll_tavern(player["user_id"], world_id, tavern, cfg, now,
                                       salt=seq, quality_bonus=qb)
            tavern["refresh_at"] = now + refresh_sec
            player["tavern"] = tavern
            await self.save_player(player)
            await self._commit()
        return tavern

    def _roll_tavern(self, user_id: str, world_id: int, old: dict, cfg: dict,
                     now: int, salt: int = 0, quality_bonus: float = 0.0,
                     role_force: str = "") -> dict:
        # salt=0：自动刷新按小时粒度稳定候选；手动刷新传精确时间戳，
        # 保证同小时内多次付费刷新得到不同候选。
        # v4.3.0：种子混入世界私密密钥——此前种子全为公开可算输入
        # （user_id/world_id/时间桶/salt），可离线复现候选序列定向刷传说将。
        rng = random.Random(int(hashlib.md5(
            f"{self._world_secret}|{user_id}|{world_id}|{now // 3600}|{salt}".encode("utf-8")).hexdigest()[:8], 16))
        n = int(cfg.get("candidates", 5))
        role_keys = list((cfg.get("role_weights") or {}).keys()) or list(R.ROLES)
        role_w = [int((cfg.get("role_weights") or {}).get(k, 1)) for k in role_keys]
        if role_force in R.ROLES:
            # 举荐信：定向刷新为指定职责（候选池全部为该职责）
            role_keys, role_w = [role_force], [1]
        rar_keys = list((cfg.get("rarity_weights_normal") or {}).keys()) or ["common"]
        # v4.3.5：支持小数权重（传世 0.1%）；旧配置的整数权重同样兼容。
        rar_w = [max(0.0, float((cfg.get("rarity_weights_normal") or {}).get(k, 0) or 0))
                 for k in rar_keys]
        if sum(rar_w) <= 0:
            rar_w = [1.0] * len(rar_keys)
        if quality_bonus > 0:
            # 中郎将/酒馆分号：高品质档位权重 ×(1+quality_bonus)（common 不变）。
            # 整数权重沿用旧版 +1 保底；小数权重（传世 0.1）按比例缩放，
            # 避免 +1 把 0.1 直接放大成 1.1。
            boost = 1 + max(0.0, float(quality_bonus))
            rar_w = [
                w if k == "common" else
                (max(w + 1, int(round(w * boost))) if w >= 1 else w * boost)
                for k, w in zip(rar_keys, rar_w)
            ]
        cands = []
        for _ in range(n):
            c = R.generate_character(
                rng,
                rarity=rng.choices(rar_keys, weights=rar_w)[0],
                role=rng.choices(role_keys, weights=role_w)[0],
                name_pools=self.cfg.get("names") or {},
                cfg=cfg)
            cands.append({k: c[k] for k in ("name", "role", "rarity", "profession",
                                            "stats", "traits", "personality", "aptitude")})
        if old.get("daily_refresh_date") == time.strftime("%Y%m%d", time.localtime(now)):
            daily = int(old.get("daily_refresh_used", 0))
        else:
            daily = 0
        return {"candidates": cands, "refreshed_at": now, "daily_refresh_date":
                time.strftime("%Y%m%d", time.localtime(now)), "daily_refresh_used": daily,
                # 刷新序号单调递增（自动刷新的去重盐，跨路径保留）
                "roll_seq": int(old.get("roll_seq", 0) or 0) + 1}

    async def refresh_tavern(self, player: dict, world_id: int, manual: bool = False) -> dict:
        cfg = self.main_cfg().get("recruit") or {}
        tavern = player.get("tavern") or {}
        if manual:
            # v4.3.0：每日计数先按日期校验——此前直接读残留计数，昨日
            # used>=limit 会把今日首刷也拦下（日期重置只发生在 _roll_tavern
            # 内部，而校验在其之前）；且跨日首刷后 used 会跳成旧值+1。
            today = time.strftime("%Y%m%d", time.localtime(_now()))
            used = int(tavern.get("daily_refresh_used", 0)) \
                if tavern.get("daily_refresh_date") == today else 0
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
                      premium: bool = False, expected_roll: int = 0,
                      expected_name: str = "") -> dict:
        """招募酒馆候选。

        v4.3.5 起货币由候选稀有度固定：
        - 凡品/良品/珍品：铜钱（价格按稀有度梯度）
        - 极品/传世：募将令（铜钱不可招募）

        v4.8.0 错位修复：候选列表会在 ``refresh_at`` 到期后于读取时**重掷**
        （``_tavern``）。此前 ``recruit`` 直接按序号取新区列表的候选人，
        若玩家看到的是旧列表，就会「点珍品武将、得良品内政官」。现在：
        - 本次读取触发了重掷 → 直接拒绝并提示重新选择（不扣费）；
        - 前端带来 ``roll``/``name`` 时，版本或候选身份不符同样拒绝。
        """
        cfg = self.main_cfg().get("recruit") or {}
        prev_seq = int((player.get("tavern") or {}).get("roll_seq", 0) or 0)
        tavern = await self._tavern(player, world_id)
        cur_seq = int(tavern.get("roll_seq", 0) or 0)
        if cur_seq != prev_seq:
            return {"ok": False, "refreshed": True,
                    "msg": "酒馆候选刚刚刷新，原候选已失效，请重新选择（本次未扣费）"}
        if int(expected_roll or 0) > 0 and int(expected_roll) != cur_seq:
            return {"ok": False, "refreshed": True,
                    "msg": "酒馆候选已刷新（页面数据过期），请重新选择（本次未扣费）"}
        cands = tavern.get("candidates") or []
        idx = int(index)
        if not (0 <= idx < len(cands)):
            raise StrategyError("候选序号无效")
        if expected_name and str(cands[idx].get("name") or "") != expected_name:
            return {"ok": False, "refreshed": True,
                    "msg": "候选已变化，请重新选择（本次未扣费）"}
        cap_info = await self._char_cap_info(world_id, player)
        cap = int(cap_info["cap"])
        owned = await self._roster_count(player["user_id"], world_id)
        if owned >= cap:
            raise StrategyError(
                f"角色数量已达上限（{owned}/{cap}）"
                f"：当前据点 {cap_info['nodes']} 座，扩建据点可增加名额")
        cand = dict(cands[idx])
        rarity = str(cand.get("rarity") or "common")
        token_only = {str(x) for x in (cfg.get("token_only_rarities")
                                       or ("epic", "legend"))}
        if rarity in token_only:
            if not premium:
                raise StrategyError(
                    f"{R.RARITY_NAMES.get(rarity, rarity)}仅可用募将令招募")
            token_costs = cfg.get("token_cost_by_rarity") or {}
            cost = max(1, int(token_costs.get(
                rarity, 25 if rarity == "legend" else 6)))
            held = int(player.get("recruit_token", 0) or 0)
            if held < cost:
                raise StrategyError(
                    f"募将令不足：{R.RARITY_NAMES.get(rarity, rarity)}招募需 "
                    f"{cost} 枚（持有 {held}）")
            player["recruit_token"] = held - cost
            currency = "token"
        else:
            if premium:
                raise StrategyError(
                    f"{R.RARITY_NAMES.get(rarity, rarity)}仅可用铜钱招募")
            coin_costs = cfg.get("coin_cost_by_rarity") or {}
            default_coin = {"common": 400, "fine": 800, "rare": 1600}
            cost = max(1, int(coin_costs.get(
                rarity, default_coin.get(rarity, 800))))
            held = int(player.get("coin", 0) or 0)
            if held < cost:
                raise StrategyError(
                    f"铜钱不足：{R.RARITY_NAMES.get(rarity, rarity)}招募需 {cost}")
            player["coin"] = held - cost
            currency = "coin"
        rng = self._rng("recruit", player["user_id"], world_id, _now() // 60, idx)
        # §3/§11：新招募角色出生在主城；主城失效则退到最近己方据点
        fid = int(player.get("faction_id") or 0)
        home = 0
        f = await self.get_faction(fid) if fid else None
        if f:
            home = int(f.get("capital_node_id") or 0)
        home_node = await self.get_node(world_id, home) if home else None
        if not home_node or int(home_node.get("owner_faction") or 0) != fid \
                or str(home_node.get("type") or "") not in R.CHAR_DWELL_NODE_TYPES:
            # §3：主城失效/不可停留时退最近可用己方据点
            home = await self._nearest_own_node(
                {"id": world_id}, {"faction_id": fid}, home,
                dwell_only=True) or 0
        char = {
            "name": cand["name"], "role": cand["role"], "rarity": cand["rarity"],
            "profession": cand["profession"], "stats": cand.get("stats") or {},
            "traits": cand.get("traits") or [],
            "personality": cand.get("personality") or {},
            "aptitude": cand.get("aptitude") or {},
            "exp": 0, "level": 1, "loyalty": rng.randint(60, 80), "injury": 0,
            "stamina": 100, "status": "idle", "bonds": {},
            "world_id": world_id, "faction_id": player["faction_id"],
            "owner_user_id": player["user_id"],
            "location": ({"node_id": int(home)} if home else {}),
        }
        await self.insert_char(char)
        char["recruit_currency"] = currency
        char["recruit_cost"] = cost
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
        cands.pop(idx)
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
        if await self._char_governor_node_id(int(world["id"]), int(c["id"])):
            raise StrategyError("该角色仍任据点太守，请先卸任再遣散")
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

    async def _friendly_owned_node(self, p: dict, node_id: int) -> dict:
        """己方/联盟据点（所在地机制 §3/§4/§10.3 的口径）。

        建造/升级/派遣/编军允许在盟友据点上进行（资源由操作者自付）；
        无主、敌方据点拒绝。v4.5.2 起遗迹转为常规可经营据点，不再特判。
        """
        node = await self.get_node(p["world_id"], int(node_id))
        if not node:
            raise StrategyError("据点不存在")
        # 仅拦「不可停留」的据点类型（如后续新增的特殊点位），遗迹已放行。
        if str(node.get("type") or "") not in R.CHAR_DWELL_NODE_TYPES:
            raise StrategyError("该据点不可作为人物落点/驻防/建设")
        owner = int(node.get("owner_faction") or 0)
        if owner <= 0:
            raise StrategyError(f"「{node['name']}」是无主据点，不可操作")
        f = await self.get_faction(int(p["faction_id"]))
        ids = await self._alliance_ids(int(p["world_id"]), f) if f else set()
        ids.add(int(p["faction_id"]))
        if owner not in ids:
            raise StrategyError(f"「{node['name']}」不是我方/盟友据点")
        return node

    async def _own_char(self, p: dict, char_id: int) -> dict:
        c = await self.get_char(int(char_id))
        if not c or c.get("owner_user_id") != p["user_id"]:
            raise StrategyError("角色不存在或不属于你")
        # v4.3.0：world_id 校验——此前旧赛季角色可被新世界派遣/编军
        # （跨赛季走私满级 legend，且不占新世界名册额度）
        if int(c.get("world_id", 0) or 0) != int(p["world_id"]):
            raise StrategyError("角色不存在或不属于你")
        return c

    async def _own_army(self, p: dict, army_id: int) -> dict:
        a = await self.get_army(int(army_id))
        if not a or a.get("owner_user_id") != p["user_id"]:
            raise StrategyError("军团不存在或不属于你")
        # v4.3.0：world_id 校验（与 _own_char 同理，堵跨赛季军团走私）
        if int(a.get("world_id", 0) or 0) != int(p["world_id"]):
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
        """取军团中首位仍能行动的角色（跳过已陨落/被俘/重伤/濒死）。

        用于出征、操练等“必须有人能行动”的门槛。带兵上限、携粮与断粮
        个性请用 :meth:`_army_nominal_leader`，二者口径不同：
        v4.5.5 起重伤角色可挂名主将，但行动仍需至少一名能行动角色。
        """
        for cid in army.get("characters") or []:
            c = await self.get_char(int(cid))
            if not c or c.get("status") in ("dead", "captive"):
                continue
            if not R.can_act(c):
                continue
            return c
        return None

    async def _army_nominal_leader(self, army: dict) -> Optional[dict]:
        """军团名义主将（名单首位仍存活者可挂名；重伤可任，濒死/陨落跳过）。

        v4.5.5：编军/换将允许重伤角色担任主将，因此带兵上限、携粮上限、
        断粮个性等必须按“名义主将”计算，不能按 `_army_leader()` 的
        “可行动主将”计算，否则会出现 3000 兵军团只能按 1500 补员的口径
        冲突。战斗模拟同样按名义主将（仅排除濒死/陨落）。
        """
        for cid in army.get("characters") or []:
            c = await self.get_char(int(cid))
            if not c or c.get("status") in ("dead", "captive"):
                continue
            if R.injury_state(c.get("injury", 0)) in ("dying", "dead"):
                continue
            return c
        return None

    async def _army_effective_cap(self, army: dict,
                                  faction: Optional[dict] = None
                                  ) -> tuple[Optional[dict], int]:
        """军团名义主将与其实时带兵上限。

        换将、官职任免/欠俸、太守变更或兵营升级后，回读时都会按当前
        名义主将重新计算；不要把这个值缓存在军团行里，否则换将后会出现
        “新主将 3000、军团仍按 1500 补员”的口径漂移。
        """
        leader = await self._army_nominal_leader(army)
        if not leader:
            return None, 0
        if faction is None:
            faction = await self.get_faction(int(army.get("faction_id") or 0))
        cap = await self._leader_cap(
            leader, faction, int(army.get("node_id") or 0))
        return leader, int(cap)

    async def _army_for_char(self, world_id: int, char_id: int,
                             cache: Optional[dict] = None) -> Optional[dict]:
        """查找角色当前所属军团（随军任务 / 任务结束恢复随军状态用）。"""
        cid = int(char_id)
        if isinstance(cache, dict):
            armies = cache.get("armies")
            if armies is None:
                armies = await self.get_armies(int(world_id))
                cache["armies"] = armies
        else:
            armies = await self.get_armies(int(world_id))
        for a in armies:
            if cid in {int(x) for x in (a.get("characters") or [])}:
                return a
        return None

    async def _restore_army_service(self, world_id: int, c: dict,
                                    army: Optional[dict] = None,
                                    cache: Optional[dict] = None) -> bool:
        """任务结束/中断后：若角色仍在军团，恢复为随军状态（调用方负责保存）。

        返回 True 表示已按军团驻地恢复；False 表示角色不属于任何军团，
        由调用方按普通空闲角色处理落点。
        """
        if not c or c.get("status") in (
                "dead", "captive", "transfer", "wander"):
            return False
        army = army or await self._army_for_char(
            int(world_id), int(c["id"]), cache)
        if not army:
            return False
        c["status"] = "army"
        nid = int(army.get("node_id") or 0)
        c["location"] = {"node_id": nid} if nid > 0 else {}
        return True

    async def _interrupt_char_tasks(self, world: dict, owner_user_id: str,
                                    char_ids: list[int]) -> list[dict]:
        """中断指定角色的在途任务（召回结算），返回被中断任务摘要。"""
        owner = str(owner_user_id or "")
        cids = [int(x) for x in (char_ids or [])]
        if not owner or not cids:
            return []
        marks = ",".join("?" * len(cids))
        rows = await self._fetchall(
            f"SELECT id, character_id FROM strategy_tasks WHERE world_id=? "
            f"AND status='active' AND character_id IN ({marks})",
            (int(world["id"]), *cids))
        out: list[dict] = []
        for row in rows:
            try:
                res = await self.recall_task(owner, int(row["id"]))
            except StrategyError:
                # 任务可能已在同 tick 结束/被取消：忽略，继续中断其余任务
                continue
            out.append({"task_id": int(row["id"]),
                        "char_id": int(row["character_id"]),
                        "msg": str(res.get("msg") or "")})
        return out

    async def _interrupt_army_tasks(self, world: dict, army: dict) -> list[dict]:
        """军团出军/改编制/解散前中断全军团任务，并把成员恢复为随军状态。"""
        cids = [int(x) for x in (army.get("characters") or [])]
        out = await self._interrupt_char_tasks(
            world, str(army.get("owner_user_id") or ""), cids)
        for cid in cids:
            c = await self.get_char(cid)
            if c and c.get("status") not in ("dead", "captive"):
                if await self._restore_army_service(
                        int(world["id"]), c, army=army):
                    await self.save_char(c)
        return out

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
        """当前占用名册名额的角色数（不含陨落/被俘/押运中的俘虏）。"""
        row = await self._fetchone(
            "SELECT COUNT(*) AS c FROM strategy_characters "
            "WHERE world_id=? AND owner_user_id=? "
            "AND status NOT IN ('dead','captive','transfer')",
            (int(world_id), user_id))
        return int((row or {}).get("c", 0) or 0)

    def _growth_cap_bonus(self) -> int:
        """任务/战斗历练可额外突破稀有度生成上限的六维总和。"""
        return int((self.main_cfg().get("recruit") or {})
                   .get("stat_growth_cap_bonus", R.DEFAULT_GROWTH_CAP_BONUS))

    async def _friendly_ids(self, world_id: int, faction_id: int,
                            cache: Optional[dict[int, set[int]]] = None) -> set[int]:
        """本势力 + 同联盟友方势力 id；用于道路/领土与协战判定。"""
        fid = int(faction_id or 0)
        if fid <= 0:
            return set()
        if cache is not None and fid in cache:
            return cache[fid]
        f = await self.get_faction(fid)
        ids = await self._alliance_ids(int(world_id), f) if f else {fid}
        ids = {int(x) for x in (ids or {fid})}
        ids.add(fid)
        if cache is not None:
            cache[fid] = ids
        return ids

    async def _factions_at_war(self, faction_a: int, faction_b: int) -> bool:
        """两势力是否处于交战（按联盟盟主外交口径，§5.5/§7）。"""
        a, b = int(faction_a or 0), int(faction_b or 0)
        if a <= 0 or b <= 0 or a == b:
            return False
        fa = await self.get_faction(a)
        fb = await self.get_faction(b)
        if not fa or not fb:
            return False
        ah = await self._alliance_host(fa) or fa
        bh = await self._alliance_host(fb) or fb
        if int(ah.get("id") or 0) == int(bh.get("id") or 0):
            return False
        return R.is_hostile(ah.get("diplomacy") or {}, int(bh["id"]), _now())

    async def _siege_owner_alive(self, world: dict, node: dict,
                                 owner_fid: int) -> bool:
        """当前围城方是否仍有存活围城军团镇守城下。"""
        owner_fid = int(owner_fid or 0)
        if owner_fid <= 0:
            return False
        ids = await self._friendly_ids(int(world["id"]), owner_fid)
        for a in await self.get_armies(
                int(world["id"]), node_id=int(node["id"]), states=("siege",)):
            if int(a.get("faction_id") or 0) in ids and \
                    int(a.get("troops", 0) or 0) > 0:
                return True
        return False

    async def _siege_side_ids(self, world: dict, node: dict,
                              owner_fid: int) -> set[int]:
        """围城参与方：原攻方及其盟友 + 与守方交战且不与攻方敌对的势力。

        v4.8.2：同一据点已有围城方时，不允许其他势力「抢归属」。与守方
        开战的第三方只能作为增援（破城方仍是原攻方），与攻方敌对的势力
        需先打赢城下营地战才能接管。
        """
        owner_fid = int(owner_fid or 0)
        if owner_fid <= 0:
            return set()
        ids = set(await self._friendly_ids(int(world["id"]), owner_fid))
        defender = int((node or {}).get("owner_faction") or 0)
        if defender <= 0:
            return ids
        for a in await self.get_armies(
                int(world["id"]), node_id=int(node["id"]), states=("siege",)):
            fid = int(a.get("faction_id") or 0)
            if fid <= 0 or fid in ids:
                continue
            if await self._factions_at_war(fid, owner_fid):
                continue  # 与围城方敌对：不算协战
            if await self._factions_at_war(fid, defender):
                ids.add(fid)
        return ids

    async def _territory_bonus(self, world: dict, node: dict, faction_id: int,
                               nodes: Optional[dict[int, dict]] = None,
                               cache: Optional[dict[int, set[int]]] = None) -> dict:
        """据点相邻友方领土带来的城防/本土作战加成（按当前归属动态计算）。"""
        fid = int(faction_id or 0)
        friendly = await self._friendly_ids(int(world["id"]), fid, cache)
        owner = int((node or {}).get("owner_faction") or 0)
        if owner <= 0 or owner not in friendly:
            return {"adjacent": 0, "defense_pct": 0.0, "combat_pct": 0.0,
                    "defense_mult": 1.0, "combat_mult": 1.0}
        if nodes is None:
            nodes = {n["id"]: n for n in await self.get_nodes(int(world["id"]))}
        count = R.adjacent_friendly_count(node, nodes, friendly)
        return R.territory_bonuses(count, self.main_cfg().get("territory") or {})

    async def _alliance_chars(self, world_id: int, f: dict) -> list[dict]:
        """本势力/联盟内的全部可用角色（官职任免与效果统计用）。"""
        ids = await self._alliance_ids(int(world_id), f)
        chars = await self.get_chars(world_id=int(world_id))
        return [c for c in chars
                if int(c.get("faction_id") or 0) in ids and
                c.get("status") not in ("dead", "captive", "transfer")]

    async def _faction_position_bonus(self, f: Optional[dict],
                                      include_alliance: bool = True) -> dict:
        """角色级官职聚合出的势力加成（太尉攻/司徒产出/祭酒计略/中郎将招募）。

        ``include_alliance`` 必须按效果类型区分：

        - **战斗/协战类**（太尉全军攻、祭酒计略/托管智谋）：``include_alliance=True``
          时在**本势力**加成之上叠加盟友官职，按协战口径共享；
        - **经济经营类**（司徒据点产出、中郎将招募品质）：无论是否结盟，
          都只统计**本势力**角色官职，盟友的司徒/中郎将永不跨势力。

        实现上先算本势力全量加成，再用联盟全量只覆盖协战三键
        （``army_atk / plot_bonus / ai_wisdom``），因此即使调用方漏传
        ``include_alliance=False``，经济键也不会被盟友污染。

        俸禄与国库始终按势力独立：每个角色只看**自己所属势力**的
        ``salary.paid_role_char_ids``，欠俸者不计入加成。
        """
        if not f:
            return R.faction_position_bonus({})
        world_id = int(f.get("world_id") or 0)

        async def _collect(by_id: dict[int, dict], chars: list[dict],
                           fallback: dict) -> dict[str, list]:
            positions: dict[str, list] = {}
            for c in chars:
                owner = by_id.get(int(c.get("faction_id") or 0)) or fallback
                pos = str(c.get("appointed_position") or "")
                if not self._salary_role_paid(owner, int(c["id"]), pos):
                    continue
                if pos:
                    positions.setdefault(pos, []).append(c)
            return positions

        own_chars = [c for c in await self.get_chars(
            faction_id=int(f["id"]), world_id=world_id)
            if c.get("status") not in ("dead", "captive", "transfer")]
        own_positions = await _collect({int(f["id"]): f}, own_chars, f)
        bonus = R.faction_position_bonus(own_positions)
        if not include_alliance:
            return bonus
        host = await self._alliance_host(f) or f
        if int(host.get("id") or 0) == int(f["id"]) and \
                not int(f.get("alliance_id") or 0):
            return bonus  # 未结盟：本势力口径即全部
        allied = await self._alliance_factions(world_id, host)
        by_id = {int(x["id"]): x for x in allied}
        all_positions = await _collect(
            by_id, await self._alliance_chars(world_id, host), host)
        all_bonus = R.faction_position_bonus(all_positions)
        # 联盟只共享协战类；经济键保留本势力口径。
        for key in ("army_atk", "plot_bonus", "ai_wisdom"):
            bonus[key] = all_bonus.get(key, bonus.get(key, 0))
        return bonus

    @staticmethod
    def _salary_role_state(f: Optional[dict], char_id: int,
                           position: str = "") -> str:
        """角色官职俸禄状态：``paid`` / ``pending`` / ``unpaid``。

        - ``paid``：上一次发薪已按**当前官职**足额支付；
        - ``pending``：任免发生在上一次发薪之后，尚未到下一次发薪 tick。
          这是正常等待状态，不是欠俸（v4.6.7 起与欠俸分开显示）；
        - ``unpaid``：上一次发薪时势力主钱包不足，真正欠俸。

        发薪结果同时写 ``paid_roles``（角色 → 官职），因此同一 tick 内
        「已付中郎将 → 晋升太尉」会按新官职判为 pending，需等下一次
        发薪后才生效，避免只付低价却拿高级官职加成。
        """
        state = (f or {}).get("salary") or {}
        cid = int(char_id)
        pos = str(position or "")
        if not state or not state.get("enabled"):
            return "paid"
        paid_roles = state.get("paid_roles")
        if isinstance(paid_roles, dict):
            paid_pos = str(paid_roles.get(str(cid)) or "")
            if paid_pos:
                # 旧档/异常值可能缺少官职，按成员命中兜底
                if pos and paid_pos != pos:
                    return "pending"
                return "paid"
        else:
            paid = state.get("paid_role_char_ids")
            if isinstance(paid, list) and cid in {int(x) for x in paid}:
                return "paid"
        unpaid = {int(x) for x in (state.get("unpaid_role_char_ids") or [])
                  if str(x).lstrip("-").isdigit()}
        if cid in unpaid:
            return "unpaid"
        return "pending"

    @staticmethod
    def _salary_role_paid(f: Optional[dict], char_id: int,
                          position: str = "") -> bool:
        """角色官职俸禄是否已支付；未支付则官职加成与官职带兵上限失效。

        ``pending``（任免后尚未发薪）与 ``unpaid``（钱包不足停发）都会
        让加成延后生效，但前端按 ``_salary_role_state`` 区分文案。
        """
        return StrategyManager._salary_role_state(f, char_id, position) == "paid"

    @staticmethod
    def _salary_governor_paid(f: Optional[dict], node_id: int) -> bool:
        """据点太守本 tick 俸禄是否已支付；未支付则太守加成暂停。"""
        state = (f or {}).get("salary") or {}
        unpaid = {int(x) for x in (state.get("unpaid_governor_node_ids") or [])
                  if str(x).lstrip("-").isdigit()}
        return int(node_id or 0) not in unpaid

    async def _char_governor_node_id(self, world_id: int, char_id: int) -> int:
        """角色当前担任太守的据点 id；0 表示未任太守。"""
        if int(char_id or 0) <= 0:
            return 0
        row = await self._fetchone(
            "SELECT id FROM strategy_nodes WHERE world_id=? AND governor_char_id=? "
            "ORDER BY id LIMIT 1",
            (int(world_id), int(char_id)))
        return int((row or {}).get("id", 0) or 0)

    async def _clear_char_governors(self, world_id: int, char_id: int) -> None:
        """角色离任/变更阵营时清除其全部据点太守标记（调用方负责 commit）。"""
        if int(char_id or 0) <= 0:
            return
        await self._execute(
            "UPDATE strategy_nodes SET governor_char_id=0 "
            "WHERE world_id=? AND governor_char_id=?",
            (int(world_id), int(char_id)))

    async def _valid_node_governor(self, node: Optional[dict]) -> Optional[dict]:
        """返回据点当前有效太守角色；死亡/被俘/押运/出走视为无效。"""
        gid = int((node or {}).get("governor_char_id") or 0)
        if gid <= 0:
            return None
        c = await self.get_char(gid)
        if not c or c.get("status") in ("dead", "captive", "transfer", "wander"):
            return None
        if R.injury_state(c.get("injury", 0)) == "dead":
            return None
        return c

    async def _leader_cap(self, leader: Optional[dict], faction: Optional[dict],
                          node_id: int = 0) -> int:
        """主将带兵上限：角色官职 + 据点太守叠加。

        - 角色官职决定基础上限（无官职 1500 / 中郎将 2000 / 司徒 2500 / 太尉 3000）；
        - 若该角色正是当前据点太守，则至少按太守 2000 计算；
        - 太守与官职可并存，取两者较高值，不再出现“辖外降为 1500”。
        """
        if not leader or not faction:
            return R.troop_cap("")
        if int(leader.get("faction_id") or 0) != int(faction.get("id") or 0):
            return R.troop_cap("")
        if str(faction.get("kind") or "") == "npc":
            return R.troop_cap("leader")
        # 角色官职俸禄未发 → 官职带兵上限失效（太守按据点单独判定）。
        # 联盟不共享国库：只看主将所属势力自己的发薪状态。
        pos_now = str(leader.get("appointed_position") or "")
        role_paid = self._salary_role_paid(faction, int(leader["id"]), pos_now)
        pos = pos_now if role_paid else ""
        cap = R.troop_cap(pos)
        # 兵营：本据点出征带兵上限 +5%/级（§6.6）；太守加成只作用于所辖据点。
        if node_id:
            node = await self.get_node(int(faction.get("world_id") or 0), int(node_id))
            if node and int(node.get("governor_char_id") or 0) == int(leader["id"]) \
                    and self._salary_governor_paid(faction, int(node_id)):
                cap = max(cap, int(R.TAISHOU_TROOP_CAP))
            lvl = min(5, self._building_level(node, "barracks"))
            if lvl:
                cap = int(cap * (1 + 0.05 * lvl))
        return cap

    # ------------------------------------------------------------------
    # 任务（§6.2）
    # ------------------------------------------------------------------
    async def _dispatch_supply(self, user_id: str, world: dict, p: dict,
                               node_id: int, target_army_id: int,
                               hours: int, char_id: int = 0) -> dict:
        """补粮（§6.2）：驻扎在己方/盟友据点内直接到账，无需押运角色。

        v4.6.7：驻扎军团（``state == 'idle'`` 且与运粮起点同一据点）直接
        由据点补粮，不再校验/占用空闲角色，也不生成补粮任务；只有行军/
        围城中的军团仍需挑选空闲角色押运，由补粮队追上交付。
        """
        tcfg = self.main_cfg().get("task") or {}
        hours = int(hours)
        if hours not in [int(h) for h in tcfg.get("hours_options", [1, 2, 4, 8])]:
            raise StrategyError("时长须为 1/2/4/8 小时")
        ticks = max(1, hours * int(tcfg.get("ticks_per_hour", 4)))
        army = await self._resolve_supply_army(
            p, world, int(node_id), int(target_army_id))
        origin = await self.get_node(world["id"], int(army["node_id"]))
        ids = await self._friendly_ids(int(world["id"]), int(p["faction_id"]))
        if origin and int(origin.get("owner_faction", 0) or 0) in ids and \
                str(origin.get("type") or "") in R.CHAR_DWELL_NODE_TYPES:
            node = origin
        else:
            src_id = await self._nearest_friendly_node(
                world, p, int(army["node_id"]), dwell_only=True)
            if src_id is None:
                raise StrategyError("没有可用的己方/盟友据点作为运粮起点")
            node = await self.get_node(world["id"], int(src_id))
            if not node:
                raise StrategyError("运粮起点不存在")
        cap = await self._army_grain_cap(army, node)
        room = max(0, cap - int(army.get("grain", 0) or 0))
        if room <= 0:
            raise StrategyError(
                f"「{army['name']}」携粮已满（{int(army.get('grain', 0))}/{cap}）")
        per_tick = await self._army_upkeep_amount(
            army, str(node.get("terrain") or "plain"),
            army.get("state") != "idle")
        need = min(max(1, per_tick) * ticks, room)
        if int(p["grain"]) < need:
            raise StrategyError(f"粮草不足：运粮需 {need}，现有 {int(p['grain'])}")
        # v4.6.7：驻扎在己方/盟友据点——军团就地入库，无需空闲人员
        if army.get("state") == "idle" and \
                int(army.get("node_id", 0) or 0) == int(node["id"]):
            p["grain"] = int(p["grain"]) - need
            army["grain"] = int(army.get("grain", 0) or 0) + need
            await self.save_army(army)
            await self.save_player(p)
            await self._commit()
            return {
                "ok": True, "task_id": 0,
                "msg": (f"🚚 已从「{node['name']}」直接补粮 {need} 给"
                        f"「{army['name']}」（现 {int(army['grain'])}/{cap}，"
                        f"无需押运角色）"),
            }
        # 行军/围城：仍需空闲角色从运粮起点押运，由补粮队追上交付
        if int(char_id or 0) <= 0:
            raise StrategyError("行军/围城中的军团需先选择押运角色")
        c = await self._own_char(p, int(char_id))
        if c["status"] == "army":
            raise StrategyError(
                f"{c['name']} 随军行动中，不能押运（请选择空闲角色）")
        if c["status"] != "idle":
            raise StrategyError(
                f"{c['name']} 状态为「"
                f"{R.CHAR_STATUS_NAMES.get(c['status'], c['status'])}」，无法押运")
        if R.injury_state(c["injury"]) in ("dying", "dead"):
            raise StrategyError(f"{c['name']} 伤势过重")
        scfg = self.main_cfg().get("stamina") or {}
        task_cost = int(R.STAMINA_COST.get("task", 20))
        if scfg.get("enabled", True) and int(c["stamina"]) < task_cost:
            raise StrategyError(f"{c['name']} 体力不足（需 {task_cost}）")
        if R.char_node_id(c) != int(node["id"]):
            raise StrategyError(f"押运角色需先传送到「{node['name']}」")
        ccfg = self.main_cfg().get("convoy") or {}
        max_active = int(ccfg.get("army_supply_max_active", 20))
        active = len((world.get("data") or {}).get("army_supplies") or [])
        if active >= max_active:
            raise StrategyError(
                f"补粮队队列已满（{active}/{max_active}），请等待送达")
        if not R.spend_stamina(c, "task", bool(scfg.get("enabled", True))):
            raise StrategyError(f"{c['name']} 体力不足（需 {task_cost}）")
        now = _now()
        c["status"] = "task"
        c["location"] = {"node_id": int(node["id"])}
        await self.save_char(c)
        p["grain"] = int(p["grain"]) - need
        await self.save_player(p)
        payload = {
            "ticks": ticks, "exp_ticks": ticks, "partner_char_id": 0,
            "supplied": need, "target_node_id": int(node["id"]),
            "origin_node_id": int(node["id"]), "army_id": int(army["id"]),
            "runner": 1, "delivered": 0,
        }
        tid = await self._execute("""
            INSERT INTO strategy_tasks (world_id, faction_id, character_id, node_id,
                task_type, start_at, end_at, payout_json, status)
            VALUES (?,?,?,?,?,?,?,?, 'active')
        """, (world["id"], p["faction_id"], c["id"], int(node["id"]), "supply",
              now, now + hours * 3600, json.dumps(payload, ensure_ascii=False)))
        pending = {
            "id": f"as{now}_{time.time_ns() % 100000000:08d}",
            "owner": p["user_id"], "faction": int(p["faction_id"]),
            "army_id": int(army["id"]), "task_id": int(tid),
            "node_id": int(node["id"]), "grain": need,
            "cap": cap, "created_tick": int(world.get("tick_no", 0) or 0),
            "wait_ticks": 0,
        }
        data = world.get("data") or {}
        supplies = data.get("army_supplies") or []
        supplies.append(pending)
        data["army_supplies"] = supplies
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return {
            "ok": True, "task_id": int(tid),
            "msg": (f"🚚 补粮队已出发，追赶「{army['name']}」（粮草 {need}，"
                    f"速度 {int(ccfg.get('army_supply_speed_edges', 4))} 段/tick）"),
        }

    async def dispatch_task(self, user_id: str, char_id: int, node_id: int,
                            task_type: str, hours: int,
                            target_node_id: int = 0,
                            partner_char_id: int = 0,
                            target_army_id: int = 0) -> dict:
        world, p = await self._require_player(user_id)
        tcfg = self.main_cfg().get("task") or {}
        scfg = self.main_cfg().get("stamina") or {}
        if task_type == "quest":
            task_type = "seek"
        if task_type not in R.TASK_TYPES:
            raise StrategyError(
                f"暂不支持该任务：{R.TASK_NAMES.get(task_type, task_type)}")
        # 运粮单独走 _dispatch_supply：驻扎军团无需押运角色，也不占人/不建任务
        if task_type == "supply":
            return await self._dispatch_supply(
                user_id, world, p, int(node_id or 0),
                int(target_army_id or 0), int(hours), int(char_id or 0))
        # §4.5：普通任务改为持续性（每满 1h 结算一次，直至主动停止）；
        # 侦察/劫掠保留固定时长；运粮沿用原补给队逻辑。
        sustained_types = {str(x) for x in
                           (tcfg.get("sustained_types") or R.TASK_LOCAL_TYPES)}
        fixed_types = {str(x) for x in
                       (tcfg.get("fixed_hours_types") or ("scout", "raid"))}
        sustained = (task_type in sustained_types
                     and task_type not in fixed_types
                     and task_type != "supply")
        ticks_per_hour = max(1, int(tcfg.get("ticks_per_hour", 4)))
        if not sustained and \
                int(hours) not in [int(h) for h in tcfg.get("hours_options", [1, 2, 4, 8])]:
            raise StrategyError("时长须为 1/2/4/8 小时")
        if sustained:
            # 持续任务的 hours 仅用于提示首个结算周期，实际按 1h 循环
            hours = 1
        c = await self._own_char(p, char_id)
        # v4.5.1：驻守军团的主将/随军将领可在当前据点执行本地任务；
        # 随军出征（非 idle）或远征任务仍拒绝。出军时由 march 自动中断。
        char_army = await self._army_for_char(world["id"], int(c["id"]))
        if c["status"] == "army":
            if not char_army or str(char_army.get("state") or "") != "idle":
                raise StrategyError(
                    f"{c['name']} 随军行动中，无法执行据点任务")
            if task_type not in R.TASK_LOCAL_TYPES:
                raise StrategyError(
                    f"{c['name']} 随军驻守中，只能执行当前据点的本地任务")
        elif c["status"] != "idle":
            raise StrategyError(
                f"{c['name']} 状态为「{R.CHAR_STATUS_NAMES.get(c['status'], c['status'])}」，无法派遣")
        if R.injury_state(c["injury"]) in ("dying", "dead"):
            raise StrategyError(f"{c['name']} 伤势过重")
        # v4.3.0：体力口径统一到 R.STAMINA_COST（此前预检读配置
        # stamina.task_cost、实扣用硬编码，配置改动会导致二者背离）
        task_cost = int(R.STAMINA_COST.get("task", 20))
        if scfg.get("enabled", True) and c["stamina"] < task_cost:
            raise StrategyError(f"{c['name']} 体力不足（需 {task_cost}）")
        # 双人协作（§6.1：协作任务 2 人 +15%）：两人都占用 status='task'
        partner = None
        partner_army = None
        if int(partner_char_id or 0):
            if int(partner_char_id) == int(char_id):
                raise StrategyError("协作角色不能与执行者是同一人")
            partner = await self._own_char(p, int(partner_char_id))
            partner_army = await self._army_for_char(
                world["id"], int(partner["id"]))
            if partner["status"] == "army":
                if not partner_army or str(partner_army.get("state") or "") != "idle":
                    raise StrategyError(
                        f"{partner['name']} 随军行动中，无法协作")
                if task_type not in R.TASK_LOCAL_TYPES:
                    raise StrategyError(
                        f"{partner['name']} 随军驻守中，只能执行本地任务")
            elif partner["status"] != "idle":
                raise StrategyError(
                    f"{partner['name']} 状态为「"
                    f"{R.CHAR_STATUS_NAMES.get(partner['status'], partner['status'])}」，"
                    f"无法协作")
            if R.injury_state(partner["injury"]) in ("dying", "dead"):
                raise StrategyError(f"{partner['name']} 伤势过重")
            if scfg.get("enabled", True) and partner["stamina"] < task_cost:
                raise StrategyError(
                    f"{partner['name']} 体力不足（需 {task_cost}）")
        ticks = max(1, int(hours) * int(tcfg.get("ticks_per_hour", 4)))
        target_id = int(target_node_id or 0)
        supplied = 0
        army = None
        supply_instant = False
        supply_msg = ""
        pending_supply: Optional[dict] = None
        if task_type in ("raid", "scout"):
            # §4.4：node_id 是出发地（己方据点），目标存 payload/目标字段。
            if target_id <= 0:
                raise StrategyError("请指定侦察/劫掠的目标据点")
            node = await self._friendly_owned_node(p, node_id)
            target = await self.get_node(world["id"], target_id)
            if not target:
                raise StrategyError("目标据点不存在")
            target_id = int(target["id"])
            if task_type == "raid":
                if int(target.get("owner_faction") or 0) == int(p["faction_id"]):
                    raise StrategyError("不能劫掠己方据点")
                # v4.3.0：复用 march 的全套目标校验（同盟/保护期/停战/
                # 新手保护）——此前 raid 绕过全部宣战门禁，可劫掠受护山盾、
                # 新手保护期甚至同盟势力的据点
                await self._check_attack_target(world, p, target, _now())
                f = await self.get_faction(int(p["faction_id"]))
                ids = await self._alliance_ids(int(world["id"]), f) if f else set()
                ids.add(int(p["faction_id"]))
                all_nodes = await self.get_nodes(world["id"])
                # v4.3.0：邻接校验修正——此前用节点 id 比对势力 id 集合
                # （int(n["id"]) in ids），真正毗邻的目标被误拒，而与
                # 「id 恰好等于某势力 id 的节点」相邻的目标被误放行
                adj_ok = any(
                    int(n.get("owner_faction") or 0) in ids
                    and int(target["id"]) in (n.get("adj") or [])
                    for n in all_nodes)
                if not adj_ok:
                    raise StrategyError("劫掠目标须与己方/盟友据点相邻")
        elif task_type == "supply":
            # 运粮已在函数入口分流到 _dispatch_supply（v4.6.7），此处不会命中。
            raise StrategyError("运粮请使用补粮入口")
        else:
            node = await self._friendly_owned_node(p, node_id)
        # §4：任务须“人在该据点”——本地任务=目标据点，远征任务=出发地
        if R.char_node_id(c) != int(node["id"]):
            raise StrategyError(f"需先传送到「{node['name']}」后再派遣")
        if char_army is not None and \
                int(char_army.get("node_id") or 0) != int(node["id"]):
            raise StrategyError(
                f"{c['name']} 所属军团驻地不在「{node['name']}」，无法在此执行任务")
        if partner is not None and R.char_node_id(partner) != int(node["id"]):
            raise StrategyError(
                f"协作角色需先传送到「{node['name']}」")
        if partner_army is not None and \
                int(partner_army.get("node_id") or 0) != int(node["id"]):
            raise StrategyError(
                f"{partner['name']} 所属军团驻地不在「{node['name']}」，无法在此协作")
        # v4.3.0：检查实扣返回值——此前 `if ...: pass` 吞掉 False（未扣成功），
        # 体力不足时任务照跑（整个动作在 handle_action 事务内，失败整体回滚）
        if not R.spend_stamina(c, "task", bool(scfg.get("enabled", True))):
            raise StrategyError(f"{c['name']} 体力不足（需 {task_cost}）")
        c["status"] = "task"
        c["location"] = {"node_id": node["id"]}
        await self.save_char(c)
        if partner is not None:
            if not R.spend_stamina(partner, "task", bool(scfg.get("enabled", True))):
                raise StrategyError(f"{partner['name']} 体力不足（需 {task_cost}）")
            partner["status"] = "task"
            partner["location"] = {"node_id": node["id"]}
            await self.save_char(partner)
        now = _now()
        # 侦察/劫掠：task.node_id 仍是目标（结算/情报/掠夺按目标据点算），
        # 人物所在地只记 origin_node_id（§4.4）。
        task_node_id = int(target_id) if task_type in ("raid", "scout") \
            else int(node["id"])
        payload: dict = {
            "ticks": ticks,
            "exp_ticks": 1 if supply_instant else ticks,
            "partner_char_id": int(partner["id"]) if partner else 0,
            "supplied": supplied,
            "target_node_id": int(target_id) if task_type in ("raid", "scout")
            else int(node["id"]),
            "origin_node_id": int(node["id"]),
            "army_id": int(army["id"]) if army else 0,
            "runner": 1 if pending_supply else 0,
            "delivered": 0,
        }
        if sustained:
            # 持续任务：无固定到期时间，每满 1h 结算一次
            end_at = 0
            payload.update({"sustained": 1, "ticks": ticks_per_hour,
                            "exp_ticks": ticks_per_hour,
                            "next_settle_at": now + 3600,
                            "settled_hours": 0})
        else:
            end_at = now if supply_instant else now + int(hours) * 3600
        tid = await self._execute("""
            INSERT INTO strategy_tasks (world_id, faction_id, character_id, node_id,
                task_type, start_at, end_at, payout_json, status)
            VALUES (?,?,?,?,?,?,?,?, 'active')
        """, (world["id"], p["faction_id"], c["id"], task_node_id, task_type,
              now, end_at, json.dumps(payload, ensure_ascii=False)))
        if pending_supply:
            pending_supply["task_id"] = int(tid)
            data = world.get("data") or {}
            supplies = data.get("army_supplies") or []
            supplies.append(pending_supply)
            data["army_supplies"] = supplies
            world["data"] = data
            await self.save_world(world)
        await self._commit()
        if task_type == "supply" and supply_msg:
            return {"ok": True, "msg": supply_msg, "task_id": tid}
        extra = f"，与 {partner['name']} 协作" if partner is not None else ""
        span = "每小时结算，可持续执行" if sustained else f"{hours}小时"
        return {"ok": True,
                "msg": f"{c['name']} 开始在「{node['name']}」执行"
                       f"{R.TASK_NAMES.get(task_type, task_type)}（{span}）{extra}",
                "task_id": tid}

    async def recall_task(self, user_id: str, task_id: int) -> dict:
        """主动停止任务（§4.5）。

        持续任务按未满 1h 的部分按比例结算（recall_loss 默认 0）；
        侦察/劫掠中途停止无收益；均释放搭档并回到出发地。
        """
        world, p = await self._require_player(user_id)
        row = await self._fetchone(
            "SELECT * FROM strategy_tasks WHERE id=? AND world_id=?", (task_id, world["id"]))
        task = self._decode("strategy_tasks", row)
        if not task or task["status"] != "active":
            raise StrategyError("任务不存在或已结束")
        c = await self.get_char(task["character_id"])
        if not c or c.get("owner_user_id") != p["user_id"]:
            raise StrategyError("无权操作该任务")
        payout_meta = task.get("payout") or {}
        if task["task_type"] == "supply" and int(payout_meta.get("runner") or 0) \
                and not int(payout_meta.get("delivered") or 0):
            refunded = await self._cancel_army_supply(world, task_id, refund=True)
            await self.db.conn.execute(
                "UPDATE strategy_tasks SET status='recalled' WHERE id=?", (task_id,))
            if c.get("status") not in ("captive", "dead", "transfer", "wander"):
                # 落点回出发地；易主/消失则退到最近己方据点（§4.4）
                origin = int(payout_meta.get("origin_node_id") or 0)
                fid0 = int(task.get("faction_id") or 0)
                dest = 0
                f0 = await self.get_faction(fid0) if fid0 else None
                ids0 = await self._alliance_ids(int(world["id"]), f0) \
                    if f0 else {fid0}
                onode = await self.get_node(world["id"], origin) if origin else None
                if onode and int(onode.get("owner_faction") or 0) in ids0 and \
                        str(onode.get("type") or "") in R.CHAR_DWELL_NODE_TYPES:
                    dest = origin
                if not dest:
                    dest = await self._char_fallback_location(
                        int(world["id"]), {"faction_id": fid0}, origin)
                if not await self._restore_army_service(int(world["id"]), c):
                    c["status"] = "idle"
                    c["location"] = {"node_id": int(dest)} if dest else {}
                await self.save_char(c)
                # 协作搭档同步释放，避免补粮队召回后永久卡在 task 态
                partner_id = int(payout_meta.get("partner_char_id") or 0)
                if partner_id:
                    partner = await self.get_char(partner_id)
                    if partner and partner.get("status") not in (
                            "captive", "dead", "transfer", "wander"):
                        if not await self._restore_army_service(
                                int(world["id"]), partner):
                            partner["status"] = "idle"
                            partner["location"] = ({"node_id": int(dest)}
                                                   if dest else {})
                        await self.save_char(partner)
            await self._commit()
            return {"ok": True,
                    "msg": f"已召回补粮队，未送达粮草 {refunded} 退回",
                    "payout": {"kind": "none", "amount": 0}}
        tcfg = self.main_cfg().get("task") or {}
        loss = float(tcfg.get("recall_loss", 0.0) or 0.0)
        ticks_per_hour = max(1, int(tcfg.get("ticks_per_hour", 4)))
        sustained = bool(int(payout_meta.get("sustained") or 0))
        if sustained:
            # §4.5 持续任务：按当前未满 1h 的部分按比例结算（召回损失默认 0）
            boundary = int(payout_meta.get("next_settle_at") or 0) - 3600
            if boundary <= 0:
                boundary = int(task["start_at"])
            partial = max(0, min(3600, _now() - boundary))
            got_ticks = int(round(ticks_per_hour * partial / 3600.0 * (1 - loss)))
        elif task["task_type"] in ("scout", "raid"):
            # 侦察/劫掠：中途停止无收益（§4.5）
            got_ticks = 0
        elif str(task["task_type"]).startswith("plot_"):
            # 计略任务（§6.4）：召回直接作废、不发通用任务产出，
            # 已投入的铜钱/精铁/体力不退（成本规则不变）。
            got_ticks = 0
        else:
            elapsed = max(0, _now() - task["start_at"])
            total = max(1, task["end_at"] - task["start_at"])
            full_ticks = int(payout_meta.get("ticks", 4))
            got_ticks = max(0, int(full_ticks * elapsed / total * (1 - loss)))
        # 刚派出就召回时可能结算 0 tick；不能再走 settle_task（其内部按
        # max(1, ticks) 兜底，会白送 1 tick 收益）
        payout = await self._pay_task(world, p, task, c, got_ticks) \
            if got_ticks > 0 else {"kind": "none", "amount": 0}
        task["status"] = "recalled"
        await self.db.conn.execute(
            "UPDATE strategy_tasks SET status='recalled' WHERE id=?", (task_id,))
        # v4.3.0：结算期间（_pay_task raid 反杀）角色可能已被俘/阵亡，
        # 不得强改 idle——否则俘虏「越狱」回可用池，且 DB 残留
        # idle+captor_faction>0 的脏数据（与 _step_settle_tasks 同守卫）
        # §4.4：远征任务落点回出发地（易主则退到最近己方据点）
        dest = int(task.get("node_id") or 0)
        if R.task_is_expedition(str(task["task_type"])):
            origin = int(payout_meta.get("origin_node_id") or 0)
            dest = 0
            fid0 = int(task.get("faction_id") or 0)
            f0 = await self.get_faction(fid0) if fid0 else None
            ids0 = await self._alliance_ids(int(world["id"]), f0) \
                if f0 else {fid0}
            onode = await self.get_node(world["id"], origin) if origin else None
            if onode and int(onode.get("owner_faction") or 0) in ids0 and \
                    str(onode.get("type") or "") in R.CHAR_DWELL_NODE_TYPES:
                dest = origin
            if not dest:
                dest = await self._char_fallback_location(
                    int(world["id"]), {"faction_id": fid0}, origin)
        elif str(task["task_type"]).startswith("plot_"):
            # 计略任务的 node_id 是目标据点（可为敌城/0），执行者全程
            # 未离开原所在地：召回不得按目标据点改写 location。
            dest = R.char_node_id(c)
        else:
            # 本地任务：据点执行期间可能已易主，落点须校验己方归属
            dest = await self._char_fallback_location(
                int(world["id"]), c, dest)
        if c.get("status") not in ("captive", "dead", "transfer", "wander"):
            if not await self._restore_army_service(int(world["id"]), c):
                c["status"] = "idle"
                c["location"] = {"node_id": int(dest)} if dest else {}
            await self.save_char(c)
        # 双人协作：召回时同步释放搭档，避免永久占用
        partner_id = int((task.get("payout") or {}).get("partner_char_id") or 0)
        if partner_id:
            partner = await self.get_char(partner_id)
            if partner and partner.get("status") not in (
                    "captive", "dead", "transfer", "wander"):
                if not await self._restore_army_service(
                        int(world["id"]), partner):
                    partner["status"] = "idle"
                    partner["location"] = ({"node_id": int(dest)}
                                           if dest else {})
                await self.save_char(partner)
        await self._commit()
        if str(task["task_type"]).startswith("plot_"):
            return {"ok": True,
                    "msg": "🕵️ 已终止计略（不结算任务产出，已投入成本不退）",
                    "payout": payout}
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
        # 商路加成（§5.5）：贸易任务节点位于本方/盟友商路 → 按商路数量与
        # 总长度递增（基础 + 每条 + 每边，封顶见 trade_route 配置）
        if task["task_type"] == "trade" and node:
            routes = await self._routes_for_faction(
                world, int(p.get("faction_id", 0) or 0))
            if any(int(node["id"]) in
                   {int(x) for x in (rt.get("path") or [])} for rt in routes):
                trcfg = self.main_cfg().get("trade_route") or {}
                bonus = R.trade_route_task_bonus(
                    routes,
                    base=float(trcfg.get("trade_task_bonus", 0.3)),
                    per_route=float(trcfg.get("task_bonus_per_route", 0.05)),
                    per_edge=float(trcfg.get("task_bonus_per_edge", 0.02)),
                    max_bonus=float(trcfg.get("task_bonus_max", 1.0)))
                amount = int(amount * (1 + bonus))
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
            # 研究任务产出本势力科技点（联盟成员各记各的，不并入盟主）
            f = await self._own_faction(p)
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
        # 立即补粮（驻扎军团，任务下一 tick 即结束）实际占用约 0 tick，
        # 按 exp_ticks（默认 1）结算，避免反复「8 小时补粮」刷经验。
        exp_per_tick = int(tcfg.get("exp_per_tick", 5))
        exp_ticks = max(0, int(payout_meta.get("exp_ticks", ticks) or 0))
        logs = R.gain_exp(c, exp_ticks * exp_per_tick,
                          self._growth_cap_bonus())
        # §4.4：远征任务落点记出发地，绝不写目标据点；本地任务留在任务据点。
        # 计略任务的 node_id 是目标据点，执行者并未离开原所在地，不得改写。
        # 被俘/陨落/押运/出走者不得改写 location（否则俘虏被“传送”出狱）。
        if not R.task_is_expedition(str(task["task_type"])) and \
                not str(task["task_type"]).startswith("plot_") and \
                c.get("status") not in ("captive", "dead", "transfer", "wander"):
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
            captor = int(node.get("owner_faction") or 0)
            # v4.3.0：无主据点（captor=0）无人可关押俘虏——此前会造出
            # captor_faction=0 的孤儿俘虏（无法劝降/收赎金，只能等关押位
            # 满被驱逐），此类反杀降级为受伤
            if captor > 0 and rng.random() < 0.4:
                # 统一走关押入口：校验营位、移出军团名册；位满则就地驱逐
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
        """侦察风险判定（§4.4：8% 被俘 / 无主目标改为击伤）。

        v4.3.5 所在地机制：此前的语义是「侦察顺手擒获敌方流将」，与设计
        §4.4「侦察 8% 被俘」冲突，现统一为**执行侦察的角色承担风险**：
        - 目标据点有主 → 被目标势力擒获，押在该据点大牢（战役可救）；
        - 目标无主 → 无人关押，改为被残军/匪寨击伤（可能陨落）。
        """
        tcfg = self.main_cfg().get("task") or {}
        chance = float(tcfg.get("scout_capture_chance", 0.08) or 0)
        if chance <= 0 or rng.random() >= chance:
            return ""
        node = await self.get_node(world["id"], int(task["node_id"]))
        if not node:
            return ""
        owner = int(node.get("owner_faction") or 0)
        my_fid = int(task.get("faction_id") or 0)
        if owner <= 0:
            # 无主据点不产生俘获：残军/匪寨击伤，可能陨落
            dead = R.apply_injury(
                c, int((self.main_cfg().get("injury") or {}).get(
                    "recover_per_tick", 5)) * 4 + 20)
            if dead:
                c["status"] = "dead"
            await self.save_char(c)
            text = (f"☠ {c['name']} 在「{node['name']}」侦察时遭残军伏击，"
                    + ("伤重陨落" if dead else "负伤而归"))
            if p.get("user_id"):
                await self._notify(world, [p["user_id"]], "event", {"text": text})
            return "伤重陨落" if dead else "遭残军击伤"
        if owner == my_fid:
            return ""
        # 被目标据点擒获：关押点取目标据点（§4.4）
        if not await self._capture_char(c, owner, int(node["id"])):
            return ""
        text = f"⛓ {c['name']} 侦察时被「{node['name']}」守军擒获"
        if p.get("user_id"):
            await self._notify(world, [p["user_id"]], "event", {"text": text})
        return f"{c.get('name', '')}被俘"

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
        # 谋略 L2「耳目」：扩展侦察范围（2 跳 → 3 跳，覆盖更多地形情报）
        hops = 2
        f = await self.get_faction(int(task.get("faction_id") or 0))
        if f:
            teff = R.tech_effects(f.get("tech") or {},
                                  tcfg=self.main_cfg().get("tech") or {})
            if teff.get("scout_terrain_reveal"):
                hops = 3
        for _ in range(hops):
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
        """疗伤任务：只治疗「人在任务据点」的角色（v4.3.0 node-scoped）。

        此前条件为 `status=='idle' or location==node_id`——全图任意 idle
        角色都被治疗，任务据点形同虚设（可远程隔空疗伤）；且死亡/被俘
        角色也会被治疗。无 location 的 idle 角色视为主城所在（新招募角色
        默认在主城），主城即任务据点时照常治疗。
        """
        healed = 0
        f = await self.get_faction(int(p.get("faction_id", 0) or 0))
        capital = int((f or {}).get("capital_node_id", 0) or 0)
        # 经营 L7 仁政：伤兵恢复 +30%
        heal_mult = float(R.tech_effects((f or {}).get("tech") or {},
                          tcfg=self.main_cfg().get("tech") or {}
                          ).get("heal_mult", 1.0) or 1.0)
        for c in await self.get_chars(owner_user_id=p["user_id"], world_id=p["world_id"]):
            if c["injury"] <= 0 or c["status"] in ("dead", "captive", "transfer"):
                continue
            loc = int((c.get("location") or {}).get("node_id", 0) or 0) or capital
            if loc == int(node_id):
                c["injury"] = max(0, c["injury"] - int(
                    max(1, amount // 40) * heal_mult))
                await self.save_char(c)
                healed += 1
        return healed

    async def _drill_armies_at_node(self, p: dict, node_id: int, amount: int) -> int:
        n = 0
        # 经营 L6 征募：练兵/操练效率 +20%
        f = await self.get_faction(int(p.get("faction_id", 0) or 0))
        train_mult = float(R.tech_effects((f or {}).get("tech") or {},
                           tcfg=self.main_cfg().get("tech") or {}
                           ).get("train_mult", 1.0) or 1.0)
        for a in await self.get_armies(p["world_id"], owner_user_id=p["user_id"],
                                       node_id=node_id, states=("idle",)):
            a["veteran"] = min(R.VETERAN_MAX, a["veteran"] + int(
                max(1, amount // 200) * train_mult))
            await self.save_army(a)
            n += 1
        return n

    # ------------------------------------------------------------------
    # 所在地（§3 显式传送）
    # ------------------------------------------------------------------
    async def move_char(self, user_id: str, char_id: int, node_id: int) -> dict:
        """显式传送：免费瞬移到己方/联盟据点。

        §3 规则：仅 ``status='idle'`` 可移动；目标须为己方/联盟据点
        （无主/敌方拒绝）；围城据点可进不可出（解围须派军团出击）。
        """
        world, p = await self._require_player(user_id)
        c = await self._own_char(p, char_id)
        if not R.can_move_char(c):
            raise StrategyError(
                f"{c['name']} 状态为「"
                f"{R.CHAR_STATUS_NAMES.get(c['status'], c['status'])}」，无法传送")
        if R.injury_state(c.get("injury", 0)) in ("dying", "dead"):
            raise StrategyError(f"{c['name']} 伤势过重，无法传送")
        node = await self.get_node(world["id"], int(node_id))
        if not node:
            raise StrategyError("目标据点不存在")
        if str(node.get("type") or "") not in R.CHAR_DWELL_NODE_TYPES:
            raise StrategyError("该据点不可作为人物落点")
        f = await self.get_faction(int(p["faction_id"]))
        ids = await self._alliance_ids(int(world["id"]), f) if f else set()
        ids.add(int(p["faction_id"]))
        target_owner = int(node.get("owner_faction") or 0)
        if target_owner <= 0 or target_owner not in ids:
            raise StrategyError(f"「{node['name']}」不是我方/盟友据点，无法传送")
        cur_id = R.char_node_id(c)
        if cur_id and cur_id != int(node["id"]):
            cur = await self.get_node(world["id"], cur_id)
            if cur and cur.get("siege_state"):
                raise StrategyError("围城据点不可直接离开，请派军团出击解围")
        c["location"] = {"node_id": int(node["id"])}
        await self.save_char(c)
        await self._commit()
        return {"ok": True,
                "msg": f"🚶 {c['name']} 已传送至「{node['name']}」",
                "node_id": int(node["id"])}

    # ------------------------------------------------------------------
    # 军团（§5.5 编成 / §5.6 行军）
    # ------------------------------------------------------------------
    async def _army_create_quote(self, p: dict, faction: Optional[dict],
                                 node: dict, leader: dict, troop_type: str,
                                 troops: int,
                                 siege_engine: bool = False) -> dict:
        """编军资源预算（``create_army`` / ``preview_army_cost`` 共用口径）。

        只做计算不落库：征兵铜钱/粮草、破城槌、自动携粮上限与库存差额
        都由这里统一给出，避免预览与正式结算两套公式漂移。
        """
        tt = R.TROOP_TYPES[troop_type]
        troop_mult = await self._faction_buff(
            int(p["faction_id"]), "troop_cost_mult")
        teff = R.tech_effects((faction or {}).get("tech") or {},
                              tcfg=self.main_cfg().get("tech") or {})
        # 经营 L6 征募：募兵铜钱/粮耗 -15%；农政 L2：军团粮耗 -10%
        recruit_mult = float(teff.get("recruit_cost_mult", 1.0) or 1.0)
        unit_coin = float(tt["recruit_coin"]) / 10 * troop_mult * recruit_mult
        unit_grain = (float(tt["recruit_grain"]) / 10 * troop_mult
                      * recruit_mult * float(teff.get("grain_cost_mult", 1.0)))
        # 与旧口径保持相同的乘法顺序与取整，避免预览/结算出现 1 点偏差
        recruit_coin = int(troops * tt["recruit_coin"] / 10
                           * troop_mult * recruit_mult)
        # 与旧口径保持两步取整：先算征兵价，再叠农政粮耗折扣
        recruit_grain = int(troops * tt["recruit_grain"] / 10
                            * troop_mult * recruit_mult)
        recruit_grain = int(
            recruit_grain * float(teff.get("grain_cost_mult", 1.0)))
        ram_cost = 0
        if siege_engine:
            # 兵学 L2：破城槌造价 -30%；带兵 ≥ 阈值时再叠加大军攻城槌效率
            ram_mult = float(teff.get("ram_cost_mult", 1.0))
            if troops >= int(teff.get("big_army_threshold", 2000) or 2000):
                ram_mult *= max(0.0, 1.0 - float(
                    teff.get("big_army_siege_bonus", 0.0) or 0.0))
            ram_cost = int(3000 * ram_mult)
        coin_cost = recruit_coin + ram_cost
        # 携粮 = min(库存, 统率×50)；粮仓 +10%/级（§5.6 / §6.6）
        carry_cap = R.carry_grain_cap(leader)
        granary = min(5, self._building_level(node, "granary"))
        if granary:
            carry_cap = int(carry_cap * (1 + 0.1 * granary))
        stock_coin = int(p["coin"])
        stock_grain = int(p["grain"])
        # 携粮按「扣除征兵粮后的库存」计算，与正式结算顺序一致
        carry = max(0, min(stock_grain - recruit_grain, carry_cap))
        return {
            "troop_type": troop_type,
            "troop_name": tt["name"],
            "troops": int(troops),
            "unit_coin": unit_coin,
            "unit_grain": unit_grain,
            "recruit_coin": recruit_coin,
            "recruit_grain": recruit_grain,
            "ram_cost": ram_cost,
            "coin_cost": coin_cost,
            "grain_cost": recruit_grain,
            "carry": carry,
            "carry_cap": carry_cap,
            "carry_short": max(0, carry_cap - carry),
            "total_coin": coin_cost,
            "total_grain": recruit_grain + carry,
            "stock_coin": stock_coin,
            "stock_grain": stock_grain,
            "granary_level": granary,
            "affordable": stock_coin >= coin_cost and stock_grain >= recruit_grain,
            "lacking_coin": max(0, coin_cost - stock_coin),
            "lacking_grain": max(0, recruit_grain - stock_grain),
        }

    async def preview_army_cost(self, user_id: str, leader_char_id: int,
                                node_id: int, troop_type: str, troops: int,
                                siege_engine: bool = False) -> dict:
        """编军预算预览（只读）：按主将/兵种/兵力返回可花费上限与预算明细。

        前端在编军弹窗输入时调用，提前暴露征兵价、破城槌、自动携粮和
        带兵上限，避免玩家按基础价估算后到结算才发现资源不足。
        """
        _world, p = await self._require_player(user_id)
        if troop_type not in R.TROOP_TYPES:
            raise StrategyError("兵种无效")
        troops = max(0, int(troops))
        node = await self._friendly_owned_node(p, node_id)
        leader = await self._own_char(p, leader_char_id)
        faction = await self.get_faction(p["faction_id"])
        acfg = self.main_cfg().get("army") or {}
        cap = await self._leader_cap(leader, faction, int(node["id"]))
        quote = await self._army_create_quote(
            p, faction, node, leader, troop_type, troops, siege_engine)
        # 当前资源最多可编多少兵（扣除破城槌固定铜钱后按单价折算，取整到百）
        budget_coin = max(0, quote["stock_coin"] - quote["ram_cost"])
        by_coin = (int(budget_coin / quote["unit_coin"])
                   if quote["unit_coin"] > 0 else cap)
        by_grain = (int(quote["stock_grain"] / quote["unit_grain"])
                    if quote["unit_grain"] > 0 else cap)
        affordable = max(0, min(cap, by_coin, by_grain))
        affordable = affordable // 100 * 100
        quote.update({
            "leader_name": leader.get("name", ""),
            "cap": int(cap),
            "min_troops": int(acfg.get("min_troops", 100)),
            "overstaff_limit": int(
                (leader.get("stats") or {}).get("leadership", 0)) * 20,
            "overstaffed": bool(R.is_overstaffed(leader, troops)),
            "affordable_troops": affordable,
        })
        return {"ok": True, "quote": quote}

    async def preview_army_cap(self, user_id: str, army_id: int,
                               leader_char_id: int = 0) -> dict:
        """换将弹窗只读预览：按候选主将回读该军团的实际带兵上限。

        与 ``handover_army`` 的硬校验同一口径（官职俸禄 / 据点太守 /
        兵营），并区分「官职基础上限」与含太守/兵营的实际上限，避免
        前端只按官职表估算后显示 1500 而误导玩家。
        """
        _world, p = await self._require_player(user_id)
        a = await self._own_army(p, army_id)
        faction = await self.get_faction(int(a.get("faction_id") or 0))
        if int(leader_char_id or 0) > 0:
            leader = await self._own_char(p, int(leader_char_id))
        else:
            leader = await self._army_nominal_leader(a)
        if not leader:
            raise StrategyError("军团已无可用主将")
        cap = await self._leader_cap(leader, faction, int(a.get("node_id") or 0))
        pos = str(leader.get("appointed_position") or "")
        pos_state = self._salary_role_state(faction, int(leader["id"]), pos)
        pos_paid = pos_state == "paid"
        troops = int(a.get("troops", 0) or 0)
        return {
            "ok": True, "army_id": int(a["id"]),
            "leader_id": int(leader["id"]),
            "leader_name": str(leader.get("name") or ""),
            "position": pos,
            "position_name": R.POSITION_NAMES.get(pos, "无官职"),
            "position_state": pos_state,
            "position_paid": bool(pos_paid),
            "base_cap": int(R.troop_cap(pos if pos_paid else "")),
            "troop_cap": int(cap), "troops": troops,
            "overstaff_limit": int(
                (leader.get("stats") or {}).get("leadership", 0) or 0) * 20,
            "overstaffed": bool(R.is_overstaffed(leader, troops)),
        }

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
        node = await self._friendly_owned_node(p, node_id)
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
            raise StrategyError(
                f"{leader['name']} 无法出征（"
                f"{R.CHAR_STATUS_NAMES.get(leader['status'], leader['status'])}）")
        # v4.5.1：主将可为内政官，也允许带伤上任（重伤可任，濒死强制静养）；
        # 随行仍须能行动。v4.5.5：明确拒绝濒死，避免“濒死主将”与静养规则冲突。
        if R.injury_state(leader.get("injury", 0)) in ("dying", "dead"):
            raise StrategyError(f"{leader['name']} 已陨落/濒死，无法出征")
        # §4.2 编军所在地：主将须已在本据点
        if R.char_node_id(leader) != int(node["id"]):
            raise StrategyError(f"需先传送到「{node['name']}」后编军")
        members = [leader]
        seen = {int(leader["id"])}
        # §5.5：1 主将 + 副将 0~2（含主将共最多 3 人）
        max_chars = max(1, int(acfg.get("max_chars", 3)))
        for mid in member_ids or []:
            # 低危：member_ids 去重（重复 id 会让同一角色两次入军/双重羁绊掷点）
            if int(mid) in seen:
                continue
            if len(members) >= max_chars:
                raise StrategyError(
                    f"军团最多 {max_chars} 人（主将 1 + 副将 {max_chars - 1}）")
            seen.add(int(mid))
            mc = await self._own_char(p, mid)
            if mc["status"] != "idle":
                raise StrategyError(
                    f"{mc['name']} 无法出征（"
                    f"{R.CHAR_STATUS_NAMES.get(mc['status'], mc['status'])}）")
            if not R.can_act(mc):
                raise StrategyError(f"{mc['name']} 伤势过重，无法随军")
            if R.char_node_id(mc) != int(node["id"]):
                raise StrategyError(
                    f"{mc['name']} 需先传送到「{node['name']}」后编军")
            members.append(mc)
        cap = await self._leader_cap(leader, faction, int(node["id"]))
        if troops > cap:
            raise StrategyError(f"超过主将带兵上限（{cap}）")
        # 招募成本（§7.5 兵种价目）；与预览 `preview_army_cost` 共用同一口径
        tt = R.TROOP_TYPES[troop_type]
        quote = await self._army_create_quote(
            p, faction, node, leader, troop_type, troops, siege_engine)
        coin_cost = int(quote["coin_cost"])
        grain_cost = int(quote["recruit_grain"])
        if p["coin"] < coin_cost or p["grain"] < grain_cost:
            raise StrategyError(
                f"资源不足：需铜钱 {coin_cost} / 粮草 {grain_cost}"
                f"（现有铜钱 {int(p['coin'])} / 粮草 {int(p['grain'])}）")
        p["coin"] -= coin_cost
        p["grain"] -= grain_cost
        # 携粮 = min(库存, 统率×50)；粮仓 +10%/级（§5.6 / §6.6）
        carry_cap = int(quote["carry_cap"])
        carry = min(p["grain"], carry_cap)
        p["grain"] -= carry
        # 上一条军团全灭留下的老兵值（50%）在新编军时继承
        data = world.get("data") or {}
        ashes = data.get("veteran_ashes") or {}
        carry_vet = int(ashes.pop(str(p["user_id"]), 0) or 0)
        if carry_vet:
            data["veteran_ashes"] = ashes
            world["data"] = data
        for mc in members:
            mc["status"] = "army"
            mc["location"] = {"node_id": node["id"]}
            await self.save_char(mc)
        army = {
            "world_id": world["id"], "faction_id": p["faction_id"],
            "owner_user_id": p["user_id"],
            "name": f"{leader['name']}军", "node_id": node["id"],
            "characters": [mc["id"] for mc in members],
            "troop_type": troop_type, "veteran": carry_vet,
            "troops": troops, "grain": carry, "morale": 100, "state": "idle",
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
        if carry_vet:
            await self.save_world(world)
        await self._commit()
        return {"ok": True, "msg": f"编成「{army['name']}」（{tt['name']}×{troops}，携粮 {carry}"
                                   + (f"，继承老兵 {carry_vet}" if carry_vet else "") + "）",
                "army_id": army["id"]}

    async def handover_army(self, user_id: str, army_id: int,
                            leader_char_id: int = 0,
                            member_ids: Optional[list] = None) -> dict:
        """军团换将交接（§5.5 编成扩展）：替换主将与随行，兵力/粮草/老兵不变。

        - 仅 ``state='idle'`` 且非溃败重整期的军团可交接；
        - 新主将/随行须为己方角色且位于该军团驻地（本军团现役成员可互相改任）；
        - ``member_ids`` 省略（``None``）表示保留现有随行（自动剔除新主将），
          传入列表则按列表重设随行（可传空列表清空）；
        - 主将可为内政官或带伤角色（重伤可任，濒死/陨落拒绝）；随行仍须能行动；换下的角色
          恢复空闲并留在驻地；带兵上限沿用官职/太守/兵营规则，现有兵力超过
          新主将上限时拒绝交接。
        """
        world, p = await self._require_player(user_id)
        a = await self._own_army(p, army_id)
        if self._is_reorganizing(a, world):
            raise StrategyError(self._reorg_msg(a, world))
        if a["state"] != "idle":
            raise StrategyError(
                f"军团状态「{R.ARMY_STATE_NAMES.get(a['state'], a['state'])}」，"
                "换将需先回到驻守状态")
        if int(a.get("troops", 0)) <= 0:
            raise StrategyError("军团已无兵力，请解散后重新编军")
        node = await self.get_node(world["id"], int(a["node_id"]))
        if not node:
            raise StrategyError("军团驻地不存在")
        node_id = int(node["id"])
        acfg = self.main_cfg().get("army") or {}
        max_chars = max(1, int(acfg.get("max_chars", 3)))
        faction = await self.get_faction(a["faction_id"])
        old_ids = [int(x) for x in (a.get("characters") or [])]
        old_chars: list[dict] = []
        for cid in old_ids:
            c = await self.get_char(cid)
            if c:
                old_chars.append(c)
        by_id = {int(c["id"]): c for c in old_chars}

        async def _pick(cid: int, *, as_leader: bool) -> dict:
            """校验换将候选人：本军团现役成员可直接改任，其余须空闲且在驻地。"""
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                raise StrategyError("角色参数无效")
            in_this = cid in by_id
            if in_this:
                c = by_id[cid]
            else:
                c = await self._own_char(p, cid)
            if c.get("status") in ("dead", "captive"):
                raise StrategyError(f"{c['name']} 已陨落/被俘，无法加入军团")
            if R.injury_state(c.get("injury", 0)) in ("dying", "dead"):
                raise StrategyError(f"{c['name']} 已陨落/濒死，无法加入军团")
            if not in_this and c.get("status") != "idle":
                raise StrategyError(
                    f"{c['name']} 当前状态为「"
                    f"{R.CHAR_STATUS_NAMES.get(str(c.get('status') or ''), c.get('status'))}」，"
                    "无法加入军团（可能已在其他军团或任务中）")
            # v4.5.1：主将可为内政官/带伤角色；随行仍须能行动
            if not as_leader and not R.can_act(c):
                raise StrategyError(f"{c['name']} 伤势过重，无法随军")
            if not in_this and R.char_node_id(c) != node_id:
                raise StrategyError(
                    f"{c['name']} 需先传送到「{node['name']}」后才能加入军团")
            return c

        # 新主将：未指定时沿用当前名义主将（重伤可任），不可用时取首位存活者
        if int(leader_char_id or 0) > 0:
            new_leader = await _pick(int(leader_char_id), as_leader=True)
        else:
            cur = await self._army_nominal_leader(a)
            if not cur:
                raise StrategyError("军团已无可用主将，请选择一名空闲武将担任主将")
            new_leader = await _pick(int(cur["id"]), as_leader=True)
        new_leader_id = int(new_leader["id"])

        # 带兵上限硬校验：只有新主将能带得动现有兵力才允许交接
        cap = await self._leader_cap(new_leader, faction, node_id)
        if int(a.get("troops", 0)) > cap:
            raise StrategyError(
                f"新主将带兵上限为 {cap}，低于军团当前 "
                f"{int(a.get('troops', 0))} 兵，无法交接"
                "（请改选带兵上限更高的主将，或解散后按低兵力重编）")

        # 交接会改变军团编制：先中断在途任务，避免任务角色状态悬空
        await self._interrupt_army_tasks(world, a)
        # 名义主将（若有主将已陨落/被俘，按名单顺延），用于提示与改名判定
        old_leader = await self._army_nominal_leader(a)

        new_ids: list[int] = [new_leader_id]
        new_chars: list[dict] = [new_leader]
        if member_ids is None:
            # 保留现有随行（old_ids[1:]），自动剔除被提拔为主将者
            for cid in old_ids[1:]:
                if cid == new_leader_id or cid in new_ids:
                    continue
                c = by_id.get(int(cid))
                if not c:
                    continue
                new_ids.append(int(cid))
                new_chars.append(c)
        else:
            if not isinstance(member_ids, (list, tuple)):
                raise StrategyError("随行角色参数无效")
            for mid in member_ids:
                try:
                    mid = int(mid)
                except (TypeError, ValueError):
                    raise StrategyError("随行角色参数无效")
                if mid in new_ids:
                    continue
                if len(new_ids) >= max_chars:
                    raise StrategyError(
                        f"军团最多 {max_chars} 人（主将 1 + 副将 {max_chars - 1}）")
                c = await _pick(mid, as_leader=False)
                new_ids.append(int(c["id"]))
                new_chars.append(c)

        # 任务召回会写回阅历 / 体力 / 伤势 / 落点；此后必须重读角色，
        # 否则用换将前的旧快照 save_char 会把召回结算整体覆盖掉。
        fresh_chars: dict[int, dict] = {}
        for cid in {*old_ids, *new_ids}:
            fc = await self.get_char(int(cid))
            if fc:
                fresh_chars[int(cid)] = fc
        new_leader = fresh_chars.get(new_leader_id, new_leader)
        new_chars = [fresh_chars[int(cid)] for cid in new_ids
                     if int(cid) in fresh_chars]

        # 换下角色：保留陨落/被俘状态，其余恢复空闲并留在驻地
        outgoing = [c for c in old_chars if int(c["id"]) not in new_ids]
        released: list[dict] = []
        for c in outgoing:
            c = fresh_chars.get(int(c["id"]), c)
            if c.get("status") in ("dead", "captive"):
                continue
            c["status"] = "idle"
            dest = await self._char_fallback_location(world["id"], c, node_id)
            c["location"] = {"node_id": int(dest)} if dest else {}
            await self.save_char(c)
            released.append(c)
        # 新阵容：随军状态 + 所在地同步为驻地
        for c in new_chars:
            if c.get("status") in ("dead", "captive"):
                continue
            c["status"] = "army"
            c["location"] = {"node_id": node_id}
            await self.save_char(c)

        a["characters"] = new_ids
        if not old_leader or int(old_leader["id"]) != new_leader_id:
            # 军团名默认跟随主将（与 create_army 的 "{主将名}军" 一致）
            a["name"] = f"{new_leader['name']}军"
        # 新阵容的同道羁绊掷点（与编军同口径；已有羁绊不重复生成）
        rng = self._rng("bond", a["name"], _now() // 3600)
        bondable = [c for c in new_chars
                    if c.get("status") not in ("dead", "captive")]
        for i in range(len(bondable)):
            for j in range(i + 1, len(bondable)):
                if R.maybe_form_tongdao(bondable[i], bondable[j], rng):
                    await self.save_char(bondable[i])
                    await self.save_char(bondable[j])
        await self.save_army(a)
        await self._commit()

        msg = (f"🔄「{a['name']}」换将完成：主将 {new_leader['name']}"
               f"（原 {old_leader['name'] if old_leader else '无'}）"
               f"，随行 {len(new_ids) - 1} 人")
        if released:
            names = "、".join(str(c.get("name") or "?") for c in released)
            msg += f"；{names} 已回归空闲"
        return {"ok": True, "msg": msg, "army_id": int(a["id"]),
                "characters": new_ids,
                "leader_id": new_leader_id,
                "leader_name": str(new_leader.get("name") or ""),
                "troop_cap": int(cap),
                "troops": int(a.get("troops", 0) or 0),
                "overstaff_limit": int(
                    (new_leader.get("stats") or {}).get("leadership", 0) or 0) * 20,
                "overstaffed": bool(R.is_overstaffed(
                    new_leader, int(a.get("troops", 0) or 0)))}

    async def disband_army(self, user_id: str, army_id: int) -> dict:
        world, p = await self._require_player(user_id)
        a = await self._own_army(p, army_id)
        if a["state"] != "idle":
            raise StrategyError("军团行动期间无法解散（请先撤退）")
        node = await self.get_node(world["id"], a["node_id"])
        if not node or node["owner_faction"] != p["faction_id"]:
            raise StrategyError("只能在己方据点解散")
        # 解散前中断在途任务，避免角色以 task 状态被改成 idle 后重复派遣
        await self._interrupt_army_tasks(world, a)
        # 中断任务会按召回口径把铜钱/粮草/战功写回玩家行。此处必须重读
        # 玩家，否则用中断前的旧快照 save_player 会把召回收益整体覆盖掉。
        p = await self.get_player(user_id, world["id"]) or p
        p["grain"] += int(a.get("grain", 0))
        for cid in a.get("characters") or []:
            c = await self.get_char(cid)
            if c and c.get("status") not in ("dead", "captive"):
                # A-M9：已陨落/被俘角色不得重置为 idle（保持死亡与囚禁状态）
                c["status"] = "idle"
                # §4.2/§4.4：解散后留在最后停靠据点；停靠点若已是
                # 敌方/无主（例如刚打下又丢掉），退回最近可用己方据点。
                dest = await self._char_fallback_location(
                    int(world["id"]), c, int(a.get("node_id") or 0))
                c["location"] = {"node_id": int(dest)} if dest else {}
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
        leader = await self._army_nominal_leader(a)
        if not leader:
            raise StrategyError("军团已无可用主将，请解散后重新编军")
        faction = await self.get_faction(a["faction_id"])
        cap = await self._leader_cap(leader, faction, int(a["node_id"]))
        if a["troops"] + troops > cap:
            raise StrategyError(
                f"超过带兵上限（当前 {int(a['troops'])} / 上限 {cap}）")
        tt = R.TROOP_TYPES.get(a["troop_type"], R.TROOP_TYPES["sword_shield"])
        troop_mult = await self._faction_buff(
            int(p["faction_id"]), "troop_cost_mult")
        coin_cost = int(troops * tt["recruit_coin"] / 10 * troop_mult)
        grain_cost = int(troops * tt["recruit_grain"] / 10 * troop_mult)
        teff = R.tech_effects((faction or {}).get("tech") or {},
                              tcfg=self.main_cfg().get("tech") or {})
        grain_cost = int(grain_cost * float(teff.get("grain_cost_mult", 1.0)))
        if p["coin"] < coin_cost or p["grain"] < grain_cost:
            raise StrategyError(f"资源不足：需铜钱 {coin_cost} / 粮草 {grain_cost}")
        p["coin"] -= coin_cost
        p["grain"] -= grain_cost
        a["troops"] += troops
        a["morale"] = min(R.army_morale_cap(a), a["morale"] + 5)
        await self.save_army(a)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": f"补员 {troops} 兵（现 {a['troops']} / 上限 {cap}）"}

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

    async def _check_attack_target(self, world: dict, p: dict, target: dict,
                                   now: int) -> None:
        """进攻性目标共用前置校验（march/raid 共用，v4.3.0 抽取）。

        校验项：同盟势力不可攻、保护期、同盟/停战/开战备战期（§6.3 宣战
        门禁）、目标玩家新手保护期。无主目标（owner_faction=0）仅同盟校验
        可能命中（target_host 为 None 自然跳过），其余按敌方势力才生效。
        """
        my_f = await self.get_faction(int(p["faction_id"]))
        my_host = await self._alliance_host(my_f) if my_f else None
        target_f = await self.get_faction(int(target["owner_faction"])) \
            if int(target["owner_faction"]) > 0 else None
        target_host = await self._alliance_host(target_f) if target_f else None
        if int(target["owner_faction"]) != int(p["faction_id"]) and my_host and \
                target_host and int(my_host["id"]) == int(target_host["id"]):
            same_alliance = bool(my_f and target_f and
                                 int(my_f.get("alliance_id") or 0) and
                                 int(my_f.get("alliance_id") or 0) ==
                                 int(target_f.get("alliance_id") or 0))
            raise StrategyError(
                "目标属于同联盟成员势力，不可进攻" if same_alliance
                else "目标属于同盟势力，不可进攻")
        if target["owner_faction"] not in (0, p["faction_id"]):
            # v4.3.2：保护期只属于正数势力。魔潮（-1）等 NPC 占位不应
            # 继承 24h 新城保护，否则玩家会被“魔潮城”卡 1 天无法收复。
            if int(target["owner_faction"]) > 0 and \
                    int(target.get("protect_until", 0) or 0) > now:
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
                days = int((self.main_cfg().get("protection") or {}).get("newbie_days", 4))
                joined = int((np or {}).get("created_at", 0) or 0)
                if joined and now - joined < days * 86400 and not newcom.get("opt_out"):
                    raise StrategyError(f"目标玩家处于新手保护期（{days}天）")

    async def march(self, user_id: str, army_id: int, target_node_id: int) -> dict:
        world, p = await self._require_player(user_id)
        a = await self._own_army(p, army_id)
        if int(a.get("coalition_id") or 0) > 0:
            # v4.8.0 合军：任一支成员下令，联军统一沿同一路线出征
            return await self._march_coalition(world, p, a, target_node_id)
        if self._is_reorganizing(a, world):
            raise StrategyError(self._reorg_msg(a, world))
        if a["state"] == "escort":
            raise StrategyError("护送运粮队期间无法出征（抵达或被截后自动归队）")
        if a["state"] not in ("idle",):
            raise StrategyError(
                f"军团状态「{R.ARMY_STATE_NAMES.get(a['state'], a['state'])}」无法出征")
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
        # v4.3.0：抽出 _check_attack_target 共用助手（raid 同口径复用），
        # 校验项不变：同盟不可攻 / 保护期 / 同盟·停战·开战备战 / 新手保护
        await self._check_attack_target(world, p, target, now)
        # 出军前中断全军团在途任务（驻守任务不随军出发），并恢复随军状态
        interrupted = await self._interrupt_army_tasks(world, a)
        a["state"] = "march"
        a["path"] = path[1:]
        a["edge_pos"] = 0.0
        a["arrive_tick"] = world.get("tick_no", 0) + len(path)
        await self.save_army(a)
        await self._commit()
        hostile = target["owner_faction"] not in (0, p["faction_id"])
        suffix = (f"；已中断 {len(interrupted)} 个随军任务" if interrupted else "")
        return {"ok": True,
                "msg": f"「{a['name']}」出发 →「{target['name']}」"
                       f"（{len(path) - 1} 段路程，{'敌对' if hostile else '和平'}目标）"
                       + suffix}

    async def retreat_army(self, user_id: str, army_id: int) -> dict:
        """撤退（A-M5 重写）：撤往最近己方据点；无路可退则就地解散。

        修复：不再产生无人消费的 "retreat" 死状态；已在己方据点驻守时
        直接返回；围城军撤走后清理孤儿 siege_state（A-M11）。
        """
        world, p = await self._require_player(user_id)
        a = await self._own_army(p, army_id)
        if int(a.get("coalition_id") or 0) > 0:
            # v4.8.0：联军统一撤退，避免两支军团拆散后失去协同
            return await self._retreat_coalition(world, p, a)
        if a["state"] == "escort":
            raise StrategyError("护送运粮队期间无法撤退（抵达或被截后自动归队）")
        if a["state"] not in ("march", "siege", "idle"):
            raise StrategyError("当前状态无法撤退")
        was_siege = a["state"] == "siege"
        # §3/§4.4：撤退落点只能在可用己方据点；无任何己方据点时
        # 走下方“无据点可退”分支就地解散（角色落点清空）。
        home = await self._nearest_own_node(
            world, p, a["node_id"], dwell_only=True)
        if home is None:
            # 无任何己方据点：与战败溃散同口径（_auto_retreat 就地解散归还角色）
            await self._interrupt_army_tasks(world, a)
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
            if a["state"] == "idle":
                # 驻守军团撤往他处同样视为出军：中断在途任务
                await self._interrupt_army_tasks(world, a)
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

    # ------------------------------------------------------------------
    # 联军合军（v4.8.0）
    # ------------------------------------------------------------------
    def _coalition_cfg(self) -> dict:
        return self.main_cfg().get("coalition") or {}

    async def _coalition_members(self, co: dict) -> list[dict]:
        out: list[dict] = []
        for aid in co.get("member_ids") or []:
            try:
                aid = int(aid)
            except (TypeError, ValueError):
                continue
            if aid <= 0:
                continue
            a = await self.get_army(aid)
            if a:
                out.append(a)
        return out

    async def _dissolve_coalition(self, coalition_id: int, reason: str = "",
                                  world: Optional[dict] = None) -> int:
        """解散联军：清空成员 coalition_id 并删行（幂等，不抛异常）。"""
        cid = int(coalition_id or 0)
        if cid <= 0:
            return 0
        co = await self.get_coalition(cid)
        if not co:
            return 0
        names: list[str] = []
        ids: list[int] = []
        for aid in co.get("member_ids") or []:
            try:
                aid = int(aid)
            except (TypeError, ValueError):
                continue
            if aid <= 0:
                continue
            ids.append(aid)
            a = await self.get_army(aid)
            if a:
                names.append(str(a.get("name") or f"#{aid}"))
        for aid in ids:
            await self.db.conn.execute(
                "UPDATE strategy_armies SET coalition_id=0 WHERE id=?", (aid,))
        await self.db.conn.execute(
            "DELETE FROM strategy_coalitions WHERE id=?", (cid,))
        if world is not None and names:
            await self._log_action(
                world, "coalition", "dissolve",
                actor_faction=int(co.get("faction_id") or 0),
                actor_name="、".join(names),
                node_id=int(co.get("node_id") or 0),
                success=1, detail={"reason": reason, "members": ids},
                summary=f"🛡️ 联军解散：{'、'.join(names)}"
                        + (f"（{reason}）" if reason else ""),
                visibility="faction")
        return len(ids)

    async def form_coalition(self, user_id: str, leader_army_id: int,
                             member_army_id: int) -> dict:
        """两支己方军团结为联军（驻守同据点；单位仍独立结算）。"""
        world, p = await self._require_player(user_id)
        if not bool((self._coalition_cfg() or {}).get("enabled", True)):
            raise StrategyError("合军玩法未开启")
        if int(leader_army_id) == int(member_army_id):
            raise StrategyError("请选择两支不同的军团")
        lead = await self._own_army(p, int(leader_army_id))
        mem = await self._own_army(p, int(member_army_id))
        for a, label in ((lead, "主将军团"), (mem, "协同军团")):
            if int(a.get("coalition_id") or 0) > 0:
                raise StrategyError(f"{label}已在联军中，请先解除合军")
            if int(a.get("faction_id") or 0) != int(p["faction_id"]):
                raise StrategyError(f"{label}与你不在同一势力")
            if self._is_reorganizing(a, world):
                raise StrategyError(f"「{a['name']}」{self._reorg_msg(a, world)}")
            if str(a.get("state") or "") != "idle":
                raise StrategyError(
                    f"「{a['name']}」状态为「"
                    f"{R.ARMY_STATE_NAMES.get(a['state'], a['state'])}」，"
                    "仅驻守军团可合军")
            if int(a.get("troops") or 0) <= 0:
                raise StrategyError(f"「{a['name']}」已无兵力")
            if not await self._army_leader(a):
                raise StrategyError(f"「{a['name']}」已无可用主将")
        if int(lead.get("node_id") or 0) != int(mem.get("node_id") or 0):
            raise StrategyError("两支军团需位于同一据点才能合军")
        node = await self.get_node(world["id"], int(lead["node_id"]))
        if not node:
            raise StrategyError("军团所在地据点不存在")
        active = await self.get_coalitions(world["id"], owner_user_id=p["user_id"])
        if active:
            raise StrategyError("你已有联军，请先解除现有合军再组建新的联军")
        co = {
            "world_id": world["id"], "faction_id": p["faction_id"],
            "owner_user_id": p["user_id"],
            "node_id": int(lead["node_id"]), "target_node_id": 0,
            "leader_army_id": int(lead["id"]),
            "member_ids": [int(lead["id"]), int(mem["id"])],
            "status": "idle", "created_at": _now(),
        }
        await self.insert_coalition(co)
        lead["coalition_id"] = int(co["id"])
        mem["coalition_id"] = int(co["id"])
        await self.save_army(lead)
        await self.save_army(mem)
        await self._commit()
        await self._log_action(
            world, "coalition", "form",
            actor_faction=int(p["faction_id"]),
            node_id=int(node["id"]), node_name=str(node.get("name") or ""),
            success=1,
            detail={"coalition_id": int(co["id"]),
                    "leader_army": int(lead["id"]),
                    "member_army": int(mem["id"])},
            summary=(f"🛡️「{lead['name']}」与「{mem['name']}」在"
                     f"「{node.get('name', '')}」合军，行动由联军统一调度"),
            visibility="faction")
        return {"ok": True, "coalition_id": int(co["id"]),
                "msg": (f"合军成功：「{lead['name']}」+「{mem['name']}」\n"
                        "两支军团仍各自独立结算兵力/携粮/士气，"
                        "行军与撤退将统一行动、同一 tick 抵达。")}

    async def disband_coalition(self, user_id: str, army_id: int) -> dict:
        """解除合军（行军途中不允许拆军，避免半途失联）。"""
        world, p = await self._require_player(user_id)
        a = await self._own_army(p, int(army_id))
        cid = int(a.get("coalition_id") or 0)
        if cid <= 0:
            raise StrategyError("该军团未处于合军状态")
        co = await self.get_coalition(cid)
        if not co:
            a["coalition_id"] = 0
            await self.save_army(a)
            await self._commit()
            return {"ok": True, "msg": "联军记录缺失，已自动解除合军"}
        if str(co.get("status") or "") == "march":
            raise StrategyError("联军统一行军途中无法解除合军（抵达或撤退后再解除）")
        names = [str(m.get("name") or "") for m in await self._coalition_members(co)]
        await self._dissolve_coalition(cid, reason="玩家解除合军", world=world)
        await self._commit()
        return {"ok": True,
                "msg": "联军已解除：" + "、".join(n for n in names if n)}

    async def _ensure_coalition_consistency(self, world: dict):
        """每 tick 校正联军：成员缺失/异地/状态异常时自动解散。"""
        allowed = {"idle", "march", "siege", "battle"}
        for co in await self.get_coalitions(world["id"]):
            ids = [int(x) for x in (co.get("member_ids") or [])]
            members = await self._coalition_members(co)
            if len(members) < 2 or len(members) != len(ids):
                await self._dissolve_coalition(
                    int(co["id"]), reason="成员缺失", world=world)
                continue
            nodes = {int(m.get("node_id") or 0) for m in members}
            states = {str(m.get("state") or "") for m in members}
            if len(nodes) > 1 or any(s not in allowed for s in states):
                await self._dissolve_coalition(
                    int(co["id"]), reason="成员所在地/状态不一致", world=world)
                continue
            status = ("march" if "march" in states else
                      "siege" if "siege" in states else "idle")
            node_id = next(iter(nodes))
            if str(co.get("status") or "idle") != status or \
                    int(co.get("node_id") or 0) != node_id:
                co["status"] = status
                co["node_id"] = node_id
                if status != "march":
                    co["target_node_id"] = 0
                await self.save_coalition(co)

    async def _march_coalition(self, world: dict, p: dict, a: dict,
                               target_node_id: int) -> dict:
        cid = int(a.get("coalition_id") or 0)
        co = await self.get_coalition(cid)
        if not co:
            a["coalition_id"] = 0
            await self.save_army(a)
            await self._commit()
            raise StrategyError("联军记录缺失，已自动解除合军，请重新出征")
        members = await self._coalition_members(co)
        if len(members) < 2:
            await self._dissolve_coalition(cid, reason="成员不足", world=world)
            await self._commit()
            raise StrategyError("联军成员不足，已自动解除合军")
        node_id = int(a.get("node_id") or 0)
        for m in members:
            if int(m.get("node_id") or 0) != node_id:
                await self._dissolve_coalition(
                    cid, reason="成员不在同一据点", world=world)
                await self._commit()
                raise StrategyError("联军成员已不在同一据点，自动解除合军")
            if self._is_reorganizing(m, world):
                raise StrategyError(f"「{m['name']}」{self._reorg_msg(m, world)}")
            if str(m.get("state") or "") != "idle":
                raise StrategyError(
                    f"「{m['name']}」状态为「"
                    f"{R.ARMY_STATE_NAMES.get(m['state'], m['state'])}」，"
                    "联军无法统一出征")
            if int(m.get("troops") or 0) <= 0:
                raise StrategyError(f"「{m['name']}」已无兵力，请先补员或解散")
            if not await self._army_leader(m):
                raise StrategyError(f"「{m['name']}」已无可用主将")
        target = await self.get_node(world["id"], int(target_node_id))
        if not target:
            raise StrategyError("目标据点不存在")
        path = await self.find_path(world["id"], node_id, int(target["id"]))
        if not path:
            raise StrategyError("目标不连通（须沿通路行军）")
        if len(path) < 2:
            raise StrategyError("联军已在目标据点")
        now = _now()
        await self._check_attack_target(world, p, target, now)
        interrupted = 0
        tick_no = int(world.get("tick_no", 0) or 0)
        for m in members:
            interrupted += len(await self._interrupt_army_tasks(world, m))
            m["state"] = "march"
            m["path"] = path[1:]
            m["edge_pos"] = 0.0
            m["arrive_tick"] = tick_no + len(path)
            m["coalition_id"] = cid
            await self.save_army(m)
        co["status"] = "march"
        co["node_id"] = node_id
        co["target_node_id"] = int(target["id"])
        await self.save_coalition(co)
        await self._commit()
        hostile = int(target.get("owner_faction") or 0) not in (0, p["faction_id"])
        await self._log_action(
            world, "coalition", "march",
            actor_faction=int(p["faction_id"]),
            target_faction=int(target.get("owner_faction") or 0),
            node_id=int(target["id"]), node_name=str(target.get("name") or ""),
            success=1,
            detail={"coalition_id": cid, "members": [int(m["id"]) for m in members],
                    "path_len": len(path) - 1},
            summary=(f"🛡️ 联军（{'、'.join(str(m['name']) for m in members)}）"
                     f"统一出征 →「{target.get('name', '')}」"),
            visibility="faction")
        suffix = f"；已中断 {interrupted} 个随军任务" if interrupted else ""
        return {"ok": True,
                "msg": f"联军出征：{' + '.join(str(m['name']) for m in members)}"
                       f" →「{target['name']}」（{len(path) - 1} 段路程，"
                       f"{'敌对' if hostile else '和平'}目标）" + suffix}

    async def _retreat_coalition(self, world: dict, p: dict, a: dict) -> dict:
        cid = int(a.get("coalition_id") or 0)
        co = await self.get_coalition(cid)
        members = await self._coalition_members(co) if co else []
        if len(members) < 2:
            if co:
                await self._dissolve_coalition(cid, reason="成员不足", world=world)
            a["coalition_id"] = 0
            await self.save_army(a)
            await self._commit()
            return await self.retreat_army(p["user_id"], int(a["id"]))
        for m in members:
            if str(m.get("state") or "") not in ("march", "siege", "idle"):
                raise StrategyError(
                    f"「{m['name']}」当前状态无法撤退")
        node_id = int(a.get("node_id") or 0)
        was_siege = any(m.get("state") == "siege" for m in members)
        home = await self._nearest_own_node(
            world, p, node_id, dwell_only=True)
        if home is None:
            for m in members:
                await self._interrupt_army_tasks(world, m)
                await self._auto_retreat(m)
            if was_siege:
                await self._clear_orphan_siege(
                    world, await self.get_node(world["id"], node_id))
            await self._commit()
            return {"ok": True,
                    "msg": "无己方据点可退，联军已就地解散（角色归还）"}
        if home == node_id and all(m.get("state") == "idle" for m in members):
            return {"ok": True, "msg": "联军已驻守己方据点，无需撤退"}
        path = await self.find_path(world["id"], node_id, home)
        if home != node_id and not path:
            raise StrategyError("撤退路线不通（无己方据点可达）")
        for m in members:
            if home == node_id:
                m["state"] = "idle"
                m["path"] = []
                m["edge_pos"] = 0.0
            else:
                if m.get("state") == "idle":
                    await self._interrupt_army_tasks(world, m)
                m["state"] = "march"
                m["path"] = path[1:] if len(path) > 1 else []
                m["edge_pos"] = 0.0
                m["arrive_tick"] = int(world.get("tick_no", 0) or 0) + len(path)
                if not m["path"]:
                    m["state"] = "idle"
            await self.save_army(m)
        if co:
            co["status"] = "idle" if home == node_id else "march"
            co["node_id"] = node_id
            co["target_node_id"] = 0 if home == node_id else int(home)
            await self.save_coalition(co)
        if was_siege:
            await self._clear_orphan_siege(
                world, await self.get_node(world["id"], node_id))
        await self._commit()
        names = "、".join(str(m.get("name") or "") for m in members)
        return {"ok": True,
                "msg": (f"联军（{names}）已就地转入驻守"
                        if home == node_id else
                        f"联军（{names}）统一撤退中（{max(0, len(path) - 1)} 段）")}

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

    async def _nearest_own_node(self, world: dict, p: dict, node_id: int,
                                dwell_only: bool = False) -> Optional[int]:
        mine = await self._fetchall(
            "SELECT id, type FROM strategy_nodes WHERE world_id=? AND owner_faction=?",
            (world["id"], p["faction_id"]))
        ids = [int(r["id"]) for r in mine
               if not dwell_only or str(r.get("type") or "")
               in R.CHAR_DWELL_NODE_TYPES]
        if not ids:
            return None
        best, best_len = ids[0], 10 ** 9
        for nid in ids:
            path = await self.find_path(world["id"], node_id, nid)
            if path and len(path) < best_len:
                best, best_len = nid, len(path)
        return best

    async def _nearest_friendly_node(self, world: dict, p: dict, node_id: int,
                                     dwell_only: bool = False) -> Optional[int]:
        """最近己方/盟友据点（v4.6.5：运粮起点允许落在盟友据点）。"""
        rows = await self._fetchall(
            "SELECT id, type, owner_faction FROM strategy_nodes WHERE world_id=?",
            (world["id"],))
        ids_f = await self._friendly_ids(
            int(world["id"]), int(p["faction_id"]))
        ids = [int(r["id"]) for r in rows
               if int(r.get("owner_faction") or 0) in ids_f
               and (not dwell_only or str(r.get("type") or "")
                    in R.CHAR_DWELL_NODE_TYPES)]
        if not ids:
            return None
        best, best_len = ids[0], 10 ** 9
        for nid in ids:
            path = await self.find_path(world["id"], node_id, nid)
            if path and len(path) < best_len:
                best, best_len = nid, len(path)
        return best

    async def _char_fallback_location(self, world_id: int, char: dict,
                                      from_node_id: int) -> int:
        """角色落点回退（§4.4）：``from`` 仍是己方/联盟据点则原地不动，
        否则退回最近己方据点；无己方据点返回 0（由调用方清空落点）。

        用于远征任务结束/出发地易主、军团覆没等“任务作废”场景，避免
        人物以 idle 状态滞留在敌方/无主据点。
        """
        fid = int((char or {}).get("faction_id") or 0)
        node_id = int(from_node_id or 0)
        if fid <= 0:
            return 0
        if node_id > 0:
            node = await self.get_node(int(world_id), node_id)
            ids = await self._friendly_ids(int(world_id), fid)
            if node and int(node.get("owner_faction") or 0) in ids and \
                    str(node.get("type") or "") in R.CHAR_DWELL_NODE_TYPES:
                return node_id
        return int(await self._nearest_own_node(
            {"id": int(world_id)}, {"faction_id": fid}, node_id,
            dwell_only=True) or 0)

    async def _governor_onsite(self, node: Optional[dict],
                               char: Optional[dict]) -> bool:
        """太守是否真正“驻守本城”（§6）。

        ``R.is_governor_resident`` 只按所在地/状态判定；远征任务（侦察/
        劫掠/运粮）与计略任务的 location 记在出发地，必须再排除，否则
        太守在外出勤仍吃城防 +10% / 民心 ×2 加成。
        """
        if not char or not R.is_governor_resident(node, char):
            return False
        if str(char.get("status") or "") != "task":
            return True
        row = await self._fetchone(
            "SELECT task_type FROM strategy_tasks WHERE character_id=? "
            "AND status='active' ORDER BY id DESC LIMIT 1", (int(char["id"]),))
        return str((row or {}).get("task_type") or "") in R.TASK_LOCAL_TYPES

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
    # 据点级太守任免（与角色官职相互独立，可同时担任）
    # ------------------------------------------------------------------
    async def appoint_taishou(self, user_id: str, node_id: int,
                              char_id: int) -> dict:
        """任命据点的太守；每个据点 1 名，同一角色只能辖 1 个据点。"""
        world, p = await self._require_player(user_id)
        f = await self._governance_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader", "deputy"))
        node = await self.get_node(int(world["id"]), int(node_id))
        if not node:
            raise StrategyError("据点不存在")
        ids = await self._alliance_ids(int(world["id"]), f)
        if int(node.get("owner_faction") or 0) not in ids:
            raise StrategyError("只能任命本势力/联盟据点的太守")
        c = await self.get_char(int(char_id))
        if not c or int(c.get("faction_id") or 0) not in ids:
            raise StrategyError("只能任命本势力/联盟内的角色")
        if c.get("status") in ("dead", "captive", "transfer", "wander") or \
                R.injury_state(c.get("injury", 0)) == "dead":
            raise StrategyError("该角色已陨落/被俘/押运/出走，无法任免太守")
        # 入盟观察期：太守也属于任命，沿用同一门禁。
        hours = int((self.main_cfg().get("security") or {}).get(
            "min_join_hours_for_position", 0) or 0)
        owner_uid = str(c.get("owner_user_id") or "")
        if hours > 0 and owner_uid and not await self._position_tenure_ok(
                int(f["id"]), owner_uid, hours):
            raise StrategyError(f"该角色主人入盟不足 {hours} 小时，"
                                "暂不可担任太守（观察期）")

        # 同一角色只能辖 1 个据点：调任时清空旧据点（兼容历史脏数据）。
        old_rows = await self._fetchall(
            "SELECT id, name FROM strategy_nodes WHERE world_id=? "
            "AND governor_char_id=? AND id<>?",
            (int(world["id"]), int(c["id"]), int(node["id"])))
        old_node_name = ""
        for old in old_rows:
            old_node = await self.get_node(int(world["id"]), int(old["id"]))
            if not old_node:
                continue
            if not old_node_name:
                old_node_name = str(old_node.get("name") or f"#{old['id']}")
            old_node["governor_char_id"] = 0
            await self.save_node(old_node)

        # 据点已有太守：直接替换，避免“先卸任再任命”的重复操作。
        prev_id = int(node.get("governor_char_id") or 0)
        prev_name = ""
        if prev_id and prev_id != int(c["id"]):
            prev = await self.get_char(prev_id)
            prev_name = str((prev or {}).get("name") or f"#{prev_id}")

        node["governor_char_id"] = int(c["id"])
        await self.save_node(node)
        await self._commit()
        msg = f"已任命 {c['name']} 为「{node['name']}」太守"
        if old_node_name:
            msg += f"（由「{old_node_name}」调任）"
        if prev_name:
            msg += f"；原太守 {prev_name} 已卸任"
        return {"ok": True, "msg": msg, "node_id": int(node["id"]),
                "char_id": int(c["id"]),
                "governor": {"char_id": int(c["id"]), "name": c["name"]}}

    async def dismiss_taishou(self, user_id: str, node_id: int) -> dict:
        """卸任据点太守。"""
        world, p = await self._require_player(user_id)
        f = await self._governance_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader", "deputy"))
        node = await self.get_node(int(world["id"]), int(node_id))
        if not node:
            raise StrategyError("据点不存在")
        ids = await self._alliance_ids(int(world["id"]), f)
        if int(node.get("owner_faction") or 0) not in ids:
            raise StrategyError("只能卸任本势力/联盟据点的太守")
        gid = int(node.get("governor_char_id") or 0)
        if gid <= 0:
            raise StrategyError("该据点当前没有太守")
        c = await self.get_char(gid)
        node["governor_char_id"] = 0
        await self.save_node(node)
        await self._commit()
        name = str((c or {}).get("name") or "")
        return {"ok": True,
                "msg": f"已卸任「{node['name']}」太守"
                       + (f"「{name}」" if name else ""),
                "node_id": int(node["id"])}

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
    def _node_level_cap(cls, node: Optional[dict] = None) -> int:
        """据点等级上限 = 据点潜力（§10.2：1~5 级，建世时固定）。

        旧存档节点没有 ``potential`` 字段时由 ``R.node_potential`` 按类型
        回填，因此兼容 v81 之前的存档。
        """
        return R.node_potential(node)

    def _build_open_types(self) -> tuple[str, ...]:
        """可建设/升级的据点类型（§10.3）：配置优先，默认全部可停留类型。"""
        cfg = self.main_cfg().get("city") or {}
        types = cfg.get("build_open_types")
        if isinstance(types, (list, tuple)) and types:
            return tuple(str(t) for t in types)
        return tuple(R.CHAR_DWELL_NODE_TYPES)

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
        node = await self._friendly_owned_node(p, node_id)
        if node["type"] not in self._build_open_types():
            raise StrategyError("该据点类型不可建设")
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
        node = await self._friendly_owned_node(p, node_id)
        if node["type"] not in self._build_open_types():
            raise StrategyError("该据点类型不可建设")
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
        """据点升级（§10.2：上限 = 据点潜力，阶梯费用 + 建造时间）。

        升级不再即时完成：扣资源后写入 ``node["construction"]``，由 tick 的
        ``_step_construction`` 到期结算（围城中暂停、易主作废）。
        """
        world, p = await self._require_player(user_id)
        node = await self._friendly_owned_node(p, node_id)
        if node["type"] not in self._build_open_types():
            raise StrategyError("该据点类型不可升级")
        cap = self._node_level_cap(node)
        level = max(1, int(node.get("level", 1) or 1))
        if level >= cap:
            raise StrategyError(f"已达据点潜力上限（{cap} 级）")
        if node.get("construction"):
            raise StrategyError("该据点正在建造中")
        if node.get("siege_state"):
            raise StrategyError("据点正在被围城，无法启动建造")
        coin_cost, iron_cost = R.node_upgrade_cost(level)
        if p["coin"] < coin_cost or p["iron"] < iron_cost:
            raise StrategyError(f"资源不足：升级需铜钱 {coin_cost} / 精铁 {iron_cost}")
        ticks = R.node_upgrade_ticks(level)
        now = _now()
        end_at = now + ticks * self.tick_seconds()
        p["coin"] -= coin_cost
        p["iron"] -= iron_cost
        node["construction"] = {
            "to_level": level + 1,
            "end_at": end_at,
            "end_tick": int(world.get("tick_no", 0) or 0) + ticks,
            "started_at": now,
            "by": p["user_id"],
        }
        await self.save_node(node)
        await self.save_player(p)
        await self._commit()
        eta = time.strftime("%m-%d %H:%M", time.localtime(end_at))
        return {"ok": True, "level": level,
                "construction": dict(node["construction"]),
                "msg": f"「{node['name']}」开始扩建至 {level + 1} 级"
                       f"（耗时 {ticks} tick，预计 {eta} 完成）"}

    async def _step_construction(self, world: dict):
        """据点建造结算（§10.2 / §10.6）：围城暂停，到期完成。

        - 围城中：顺延 1 tick（不推进、不取消），解围后自动续建；
        - 到期：``level = to_level``（不超过潜力）、城防重置为全额、清空在建；
        - ``output_only`` 补跑 tick 同样执行，避免长期停机后建造永久卡住。
        """
        now = _now()
        tick_no = int(world.get("tick_no", 0) or 0)
        tick_sec = self.tick_seconds()
        for node in await self.get_nodes(world["id"]):
            cons = node.get("construction") or {}
            if not cons:
                continue
            to_level = int(cons.get("to_level", 0) or 0)
            if to_level <= 0:
                node["construction"] = {}
                await self.save_node(node)
                continue
            if node.get("siege_state"):
                # §10.6：围城中暂停建造（顺延一个 tick），资源不退也不取消。
                # paused_tick 去重：即使调用方在同 tick 重复结算，也只顺延一次。
                if int(cons.get("paused_tick", -1) or -1) != tick_no:
                    if int(cons.get("end_tick", 0) or 0) > 0:
                        cons["end_tick"] = int(cons["end_tick"]) + 1
                    if int(cons.get("end_at", 0) or 0) > 0:
                        cons["end_at"] = int(cons["end_at"]) + tick_sec
                    cons["paused_tick"] = tick_no
                node["construction"] = cons
                await self.save_node(node)
                continue
            end_tick = int(cons.get("end_tick", 0) or 0)
            end_at = int(cons.get("end_at", 0) or 0)
            if end_tick > 0:
                finished = end_tick <= tick_no
            else:
                finished = end_at > 0 and end_at <= now
            if not finished:
                continue
            cap = self._node_level_cap(node)
            new_level = min(max(1, to_level), cap)
            node["level"] = new_level
            node["defense"] = R.city_defense_max(new_level)
            node["construction"] = {}
            await self.save_node(node)
            owner_fid = int(node.get("owner_faction") or 0)
            if owner_fid > 0:
                rows = await self._fetchall(
                    "SELECT user_id FROM strategy_players "
                    "WHERE world_id=? AND faction_id=?",
                    (world["id"], owner_fid))
                await self._notify(
                    world, [r.get("user_id") for r in rows], "event",
                    {"text": f"🏗「{node['name']}」扩建完成，升至 {new_level} 级"
                             f"（城防 {node['defense']}）"})
        await self._commit()

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
                f"{R.RESOURCE_NAMES.get(k, k)} +{v}"
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
            # v4.3.0：补写 refresh_at——此前返回的 tavern 缺该键，下一次
            # _tavern() 视同已到期立即重掷，定向候选瞬间被冲掉（200 战功白花）
            tavern["refresh_at"] = _now() + int(cfg.get("refresh_hours", 4)) * 3600
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
            # v4.3.0：与战功商店 gold_pack 同口径——主玩法角色不存在时
            # _inject_gold 无法落库，但下方仍会照扣荣誉点，须扣点前拦截
            if await self.db.get_player_by_id(user_id) is None:
                raise StrategyError("主玩法角色不存在，请先进入修仙后再兑换灵石")
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
                f"{R.RESOURCE_NAMES.get(k, k)} +{v}"
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
        self._tx_task = asyncio.current_task()
        try:
            world["last_tick_at"] = _now()
            await self.save_world(world)
            # 0. v4.5.7 一次性迁移：旧玩家势力国库余额退回成员钱包
            await self._settle_legacy_player_treasuries(world)
            # 1. 任务结算（到期任务入账，角色转 idle）
            if not output_only:
                await self._step_settle_tasks(world)
            else:
                await self._step_settle_tasks(world)  # 到期任务照常结算
            # 1b. 俸禄结算（角色官职 + 据点太守；国库不足则本 tick 对应增益停发）
            await self._step_salary(world)
            # 2. 据点产出分配（铜钱/粮/铁 + 主玩法灵石注入含日限额）
            gold_total = await self._step_distribute_output(world, output_only)
            summary["gold_injected"] = gold_total
            # 2b. 坊市流将竞拍：到期结算/补挂（补跑 tick 也执行，避免流将无限挂起）
            await self._step_wanderer_auctions(world)
            # 2c. 据点建造推进（§10.2）：补跑 tick 也执行，避免建造永久卡住
            await self._step_construction(world)
            if not output_only:
                # 3. 围城推进
                battles = await self._step_sieges(world)
                summary["battles"].extend(battles)
                # 4. 行军推进 + 粮耗/断粮
                await self._step_march(world)
                # 5. 抵达触发会战 / 占领
                battles = await self._step_arrivals(world)
                summary["battles"].extend(battles)
                # 5b. v4.6.1 战略外交层：威胁积累 / 档位 / 自主宣战 / 停战
                await self._step_npc_diplomacy(world)
                await self._step_npc_peace(world)
                # 6. NPC AI（扩张出征 / 魔潮；守军恢复统一在 _step_recovery）
                await self._step_npc_ai(world)
                # 6a. NPC 城池发展（§10.6）与自动赈济（§10.8）
                await self._step_npc_develop(world)
                await self._step_npc_economy(world)
                # 6b. 魔潮事件收尾：事件结束/无主残留清理，回收 -1 占位
                await self._step_demon_tide(world)
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
                # 8b-1. 忠诚自然恢复（先回血，再做出走判定）
                await self._step_loyalty_recovery(world)
                # 8b-2. 势力任务 / 动员令 / 低忠诚出走（§6.4 §18.4）
                await self._step_faction_tasks(world)
                await self._step_mobilizations(world)
                await self._step_loyalty_defect(world)
                # 8c. P2：hex 手操战役超时托管推进
                await self._step_manual_battles(world)
                # 8d. P3：互市 / 匪寨 / 商路 / 粮队
                await self._step_market(world)
                await self._step_market_maker(world)
                await self._step_market_offers(world)
                await self._step_bandits(world)
                await self._step_bandit_raids(world)
                await self._step_trade_routes(world)
                await self._step_convoys(world)
                # 8d-2. 军团补粮队追击（速度高于普通行军，追上即交付）
                await self._step_army_supplies(world)
                # 8e. 赛季推进（阶段/霸权/合纵/结算，须放最后）
                await self._step_season(world)
                # 8e-2. v4.6.1 风声：事件开始 / 结束的传闻播报
                await self._step_winds(world)
                # 8f. 战报归档清理（§18.4 result_keep_days）
                await self._step_cleanup_battles(world)
                # 8f-2. v4.7.0 江湖纪事保留策略（默认 14 天 / 3000 条）
                await self._step_world_log_cleanup(world)
                # 8g. 擂台过期清理（退还赌注，无交互也会回收）
                await self._sweep_duel_arena(world)
            # 9. 落盘世界时钟 + 临时键 GC
            world["last_tick_at"] = _now()
            data = world.get("data") or {}
            recent = data.get("recent_ticks") or []
            recent.append({"tick_no": tick_no, "at": world["last_tick_at"],
                           "battles": len(summary["battles"]),
                           "events": summary["events"],
                           "gold": summary["gold_injected"],
                           "output_only": output_only})
            data["recent_ticks"] = recent[-12:]
            self._gc_world_data(data, tick_no)
            world["data"] = data
            await self.save_world(world)
            # 整段 tick 成功：恢复真实提交并一次性落盘
            self._tx_task = None
            await self._commit()
        except Exception as e:  # noqa: BLE001
            self._tx_task = None
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
    async def _release_task_partner(self, task: dict,
                                    dest_node_id: Optional[int] = None) -> None:
        """释放协作搭档（任务结束/中断时调用，避免永久占用 task 状态）。

        ``dest_node_id`` 为 None 时沿用任务据点；显式传 0 表示无可用的
        己方据点，清空落点（避免搭档被留在敌方/无主据点）。
        """
        pid = int((task.get("payout") or {}).get("partner_char_id") or 0)
        if pid <= 0:
            return
        partner = await self.get_char(pid)
        if not partner or partner.get("status") in (
                "captive", "dead", "transfer", "wander"):
            return
        if await self._restore_army_service(
                int(task.get("world_id") or 0), partner):
            await self.save_char(partner)
            return
        partner["status"] = "idle"
        if dest_node_id is None:
            dest = int(task.get("node_id") or 0)
        else:
            dest = int(dest_node_id or 0)
        partner["location"] = {"node_id": dest} if dest > 0 else {}
        await self.save_char(partner)

    async def _release_task_partner_fallback(self, world_id: int,
                                             task: dict) -> None:
        """任务异常作废时释放搭档，落点按 §4.4 回退到最近己方据点。"""
        dest = await self._char_fallback_location(
            int(world_id), {"faction_id": int(task.get("faction_id") or 0)},
            int(task.get("node_id") or 0))
        await self._release_task_partner(task, dest)

    async def _step_settle_tasks(self, world: dict):
        now = _now()
        # end_at=0 的持续任务（§4.5）也须纳入本轮检查，由 payout 里的
        # next_settle_at 决定是否满 1h；不能沿用「end_at<=now」的到期语义。
        rows = await self._fetchall(
            "SELECT * FROM strategy_tasks WHERE world_id=? AND status='active' "
            "AND (end_at<=? OR end_at=0)",
            (world["id"], now))
        tcfg = self.main_cfg().get("task") or {}
        scfg = self.main_cfg().get("stamina") or {}
        stam_enabled = bool(scfg.get("enabled", True))
        ticks_per_hour = max(1, int(tcfg.get("ticks_per_hour", 4)))
        task_stamina = int(R.STAMINA_COST.get("task", 20))
        nodes_by_id: Optional[dict[int, dict]] = None
        friendly_cache: dict[int, set[int]] = {}
        for row in rows:
            task = self._decode("strategy_tasks", row)
            c = await self.get_char(task["character_id"])
            if not c:
                await self.db.conn.execute(
                    "UPDATE strategy_tasks SET status='orphan' WHERE id=?", (task["id"],))
                # 执行者记录已不存在：不能把协作搭档永久留在 task 态
                await self._release_task_partner_fallback(int(world["id"]), task)
                continue
            payout = task.get("payout") or {}
            if task["task_type"] == "supply" and int(payout.get("runner") or 0) \
                    and not int(payout.get("delivered") or 0):
                # 补粮队在途：任务时长只用于计算运粮量，实际以追上交付为
                # 结束条件；跑满 end_at 也不能提前释放押运角色。
                exists = any(int(sp.get("task_id", 0) or 0) == int(task["id"])
                             for sp in (world.get("data") or {}).get(
                                 "army_supplies") or [])
                if exists:
                    continue
                # 队列里已无对应补粮队（异常丢失）：关闭任务，避免角色卡死
                await self.db.conn.execute(
                    "UPDATE strategy_tasks SET status='cancelled' WHERE id=?",
                    (task["id"],))
                dest = await self._char_fallback_location(
                    int(world["id"]), c,
                    int(payout.get("origin_node_id") or task["node_id"]))
                if c.get("status") == "task":
                    if not await self._restore_army_service(
                            int(world["id"]), c):
                        c["status"] = "idle"
                        c["location"] = ({"node_id": int(dest)}
                                         if dest else {})
                    await self.save_char(c)
                await self._release_task_partner(task, dest)
                continue
            # 角色在任务期间已出走/被押运：任务作废，不发奖，也不要把状态
            # 强改回 idle（否则 wander 被覆盖、收益还会进原势力国库）。
            if str(c.get("status") or "") in ("wander", "transfer"):
                await self.db.conn.execute(
                    "UPDATE strategy_tasks SET status='cancelled' WHERE id=?",
                    (task["id"],))
                # 执行者出走/被押运：同步释放协作搭档，避免其永久卡在 task 态
                await self._release_task_partner_fallback(int(world["id"]), task)
                continue
            # §4.4 本地任务须始终在己方/联盟据点内执行：据点易主/消失后
            # 任务作废并把角色退回最近己方据点——既不能让 idle 角色滞留
            # 敌城，也不能继续用敌方据点结算产出/给敌方加民心。
            tt = str(task["task_type"])
            if not R.task_is_expedition(tt) and not tt.startswith("plot_"):
                if nodes_by_id is None:
                    nodes_by_id = {n["id"]: n for n in await self.get_nodes(world["id"])}
                tnode = nodes_by_id.get(int(task.get("node_id") or 0))
                fid0 = int(c.get("faction_id") or task.get("faction_id") or 0)
                ids0 = await self._friendly_ids(
                    int(world["id"]), fid0, friendly_cache)
                # 旧存档若仍有不可停留据点上的本地任务（含 build 加城防），
                # 一律作废并退回可用据点。
                if not tnode or int(tnode.get("owner_faction") or 0) not in ids0 \
                        or str(tnode.get("type") or "") \
                        not in R.CHAR_DWELL_NODE_TYPES:
                    await self.db.conn.execute(
                        "UPDATE strategy_tasks SET status='cancelled' WHERE id=?",
                        (task["id"],))
                    dest = await self._char_fallback_location(
                        int(world["id"]), c, int(task.get("node_id") or 0))
                    if str(c.get("status") or "") == "task":
                        if not await self._restore_army_service(
                                int(world["id"]), c):
                            c["status"] = "idle"
                            c["location"] = ({"node_id": int(dest)}
                                             if dest else {})
                        await self.save_char(c)
                    await self._release_task_partner(task, dest)
                    continue
            # --- 持续任务（§4.5 普通任务每满 1h 结算一次）----------------
            if bool(int(payout.get("sustained") or 0)) and \
                    not str(task["task_type"]).startswith("plot_"):
                if int(payout.get("next_settle_at") or 0) > now:
                    continue
                if str(c.get("status") or "") in (
                        "captive", "dead", "army", "wander", "transfer"):
                    # 执行者已不在任务态（被俘/陨落/随军/出走）：作废任务
                    await self.db.conn.execute(
                        "UPDATE strategy_tasks SET status='cancelled' WHERE id=?",
                        (task["id"],))
                    await self._release_task_partner(task)
                    continue
                # §4.7：派遣时已扣「首小时」体力（20），首个整点结算只发
                # 收益、不再重复扣；否则同一小时会被扣 40。首个整点若剩余
                # 体力不足以支撑下一小时，仍先结算已付出的这 1 小时收益。
                first_settle = int(payout.get("settled_hours") or 0) <= 0
                low_stamina = stam_enabled and \
                    int(c.get("stamina", 0)) < task_stamina
                if low_stamina and not first_settle:
                    # 体力耗尽：自动停止任务并释放角色（§4.5）
                    await self.db.conn.execute(
                        "UPDATE strategy_tasks SET status='done' WHERE id=?",
                        (task["id"],))
                    if not await self._restore_army_service(
                            int(world["id"]), c):
                        c["status"] = "idle"
                    await self.save_char(c)
                    await self._release_task_partner(task)
                    if c.get("owner_user_id"):
                        tname = R.TASK_NAMES.get(
                            task["task_type"], task["task_type"])
                        await self._notify(
                            world, [c["owner_user_id"]], "task",
                            {"text": f"😵 {c['name']} 体力耗尽，"
                                     f"{tname}任务已自动停止"})
                    continue
                p = None
                if c.get("owner_user_id"):
                    p = await self.get_player(c["owner_user_id"], world["id"])
                if p is None:
                    p = await self._npc_wallet(world, task["faction_id"])
                use_sp = self._in_tick_task()
                sp = f"strategy_task_{int(task['id'])}"
                if use_sp:
                    await self.db.conn.execute(f"SAVEPOINT {sp}")
                try:
                    await self._pay_task(world, p, task, c, ticks_per_hour)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[strategy] 任务#{task['id']}小时结算失败（保留待重试）: {e}")
                    if use_sp:
                        await self.db.conn.execute(f"ROLLBACK TO {sp}")
                        await self.db.conn.execute(f"RELEASE {sp}")
                    continue
                if use_sp:
                    await self.db.conn.execute(f"RELEASE {sp}")
                # 本小时体力：首小时已由派遣费支付，后续每小时再扣
                if not first_settle:
                    R.spend_stamina(c, "task", stam_enabled)
                payout["settled_hours"] = int(payout.get("settled_hours") or 0) + 1
                payout["ticks"] = ticks_per_hour
                payout["next_settle_at"] = \
                    int(payout.get("next_settle_at") or now) + 3600
                task["payout"] = payout
                await self.db.conn.execute(
                    "UPDATE strategy_tasks SET payout_json=? WHERE id=?",
                    (json.dumps(payout, ensure_ascii=False), int(task["id"])))
                # 瘟疫期执行任务的受伤判定（task_injury 效果，§11.1）
                if c.get("status") not in ("captive", "dead"):
                    try:
                        await self._roll_task_injury(world, task, c)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            f"[strategy] 任务#{task['id']}受伤判定失败: {e}")
                if c.get("status") in ("captive", "dead", "transfer", "wander"):
                    await self.db.conn.execute(
                        "UPDATE strategy_tasks SET status='cancelled' WHERE id=?",
                        (task["id"],))
                    await self.save_char(c)
                    await self._release_task_partner_fallback(
                        int(world["id"]), task)
                    continue
                # 持续任务保持 status='task'，不置 idle、不置 done
                c["status"] = "task"
                c["location"] = {"node_id": int(task["node_id"])}
                await self.save_char(c)
                # 双人协作：搭档同样按小时获得阅历，但不单独结算收益
                partner_id = int(payout.get("partner_char_id") or 0)
                if partner_id:
                    partner = await self.get_char(partner_id)
                    if partner and partner.get("status") not in (
                            "captive", "dead"):
                        R.gain_exp(partner, max(1, ticks_per_hour * 2),
                                   self._growth_cap_bonus())
                        await self.save_char(partner)
                if first_settle and low_stamina:
                    # 首小时已结算，剩余体力不足下一小时：立即收工，
                    # 不让角色以 task 状态空占名册再等一个整点。
                    await self.db.conn.execute(
                        "UPDATE strategy_tasks SET status='done' WHERE id=?",
                        (task["id"],))
                    if not await self._restore_army_service(
                            int(world["id"]), c):
                        c["status"] = "idle"
                    await self.save_char(c)
                    await self._release_task_partner(task)
                    if c.get("owner_user_id"):
                        tname = R.TASK_NAMES.get(
                            task["task_type"], task["task_type"])
                        await self._notify(
                            world, [c["owner_user_id"]], "task",
                            {"text": f"😵 {c['name']} 体力耗尽，"
                                     f"{tname}任务已自动停止"})
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
            plot_ret = None
            try:
                if str(task["task_type"]).startswith("plot_"):
                    # 计略任务（§6.4）：走独立结算，不走 _pay_task
                    plot_ret = await self._settle_plot_task(world, task, c, p)
                else:
                    plot_ret = None
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
            if c.get("status") in ("captive", "dead", "transfer", "wander"):
                pass  # 被俘/陨落/押运/出走：保持原状态，不落点
            elif await self._restore_army_service(int(world["id"]), c):
                pass  # 仍在军团：恢复随军状态，落点回到军团驻地
            else:
                c["status"] = "idle"
                # §4.4 落点：本地任务留任务据点；远征任务回出发地，
                # 出发地已易主/消失则退到最近己方据点。
                # 计略任务的 node_id 是目标据点（可为敌方/0），执行者
                # 全程未离开原所在地，不得按目标据点改写 location。
                dest = 0 if str(task["task_type"]).startswith("plot_") \
                    else int(task.get("node_id") or 0)
                if R.task_is_expedition(str(task["task_type"])):
                    origin = int((task.get("payout") or {}).get(
                        "origin_node_id") or 0)
                    dest = 0
                    fid0 = int(task.get("faction_id") or 0)
                    f0 = await self.get_faction(fid0) if fid0 else None
                    ids0 = await self._alliance_ids(int(world["id"]), f0) \
                        if f0 else {fid0}
                    onode = await self.get_node(world["id"], origin) \
                        if origin else None
                    if onode and int(onode.get("owner_faction") or 0) in ids0 and \
                            str(onode.get("type") or "") in R.CHAR_DWELL_NODE_TYPES:
                        dest = origin
                    if not dest:
                        dest = await self._char_fallback_location(
                            int(world["id"]), {"faction_id": fid0}, origin)
                if dest:
                    c["location"] = {"node_id": int(dest)}
            await self.save_char(c)
            # 计略结算结果回推给执行者主人：成功/失败/败露被擒此前只写进
            # ``_settle_plot_task`` 的返回值，调用方直接丢弃，玩家派出军师
            # 后 2 tick 完全收不到任何结果（只能自己猜民心/忠诚变化）。
            if plot_ret and plot_ret.get("texts"):
                owner = str(c.get("owner_user_id") or "")
                if owner:
                    await self._notify(
                        world, [owner], "plot_result",
                        {"text": f"🎴 {c.get('name', '军师')}："
                                 + "；".join(plot_ret["texts"])})
            # 双人协作：搭档同步结束占用（收益已在主执行者结算时 ×1.15）
            partner_id = int((task.get("payout") or {}).get("partner_char_id") or 0)
            if partner_id:
                partner = await self.get_char(partner_id)
                if partner and partner.get("status") not in ("captive", "dead"):
                    if not await self._restore_army_service(
                            int(world["id"]), partner):
                        partner["status"] = "idle"
                    R.gain_exp(partner, max(1, int(ticks * 2)),
                               self._growth_cap_bonus())
                    await self.save_char(partner)
            await self.db.conn.execute(
                "UPDATE strategy_tasks SET status='done' WHERE id=?", (task["id"],))
        await self._commit()

    async def _roll_task_injury(self, world: dict, task: dict, c: dict):
        """task_injury 效果：任务结算时角色按概率受伤（瘟疫 5% 伤势 +10）。"""
        # 计略任务的 node_id 是目标据点，执行者并不在该城，不参与当地瘟疫
        if str(task.get("task_type") or "").startswith("plot_"):
            return
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
    async def _settle_legacy_player_treasuries(self, world: dict) -> int:
        """v4.5.7 一次性迁移：旧玩家势力国库余额退回成员钱包。

        玩家势力已不再设国库；旧存档（含 v4.0.9~v4.3.3 联盟合库遗留）里的
        余额在此按势力人数均分到成员钱包（灵石走 ``_inject_gold`` 日限额），
        随后清空 ``treasury_json``。NPC 势力的内部模拟国库不受影响。
        """
        data = world.get("data") or {}
        if int(data.get("player_treasury_removed", 0) or 0) >= 456:
            return 0
        rows = await self._fetchall(
            "SELECT * FROM strategy_factions WHERE world_id=? "
            "AND kind='player' AND alive=1", (int(world["id"]),))
        settled = 0
        for f in self._decode_all("strategy_factions", rows):
            settled += await self._settle_faction_treasury(world, f)
        data["player_treasury_removed"] = 456
        world["data"] = data
        await self.save_world(world)
        if settled:
            logger.info(f"[strategy] v4.5.7 旧势力国库清算：{settled} 名玩家已入账")
        return settled

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
        # 商路自然产出加成：途经节点按经过商路数量与总长度递增（§5.5）
        route_out_bonus = await self.trade_route_output_bonus(world)
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
            # 产出/国库/科技均按本势力独立结算（联盟不共享资源）；
            # 司徒等经济类官职也只统计本势力，盟友官职不提供产出加成。
            f = await self.get_faction(p["faction_id"])
            pos_bonus = await self._faction_position_bonus(
                f, include_alliance=False) if f else {}
            # 农政科技：全局产出 + 资源/节点类型乘区
            # （L1 谷田粮草、L3 矿脉精铁、L4 坊市铜钱、L5 灵石/全资源）
            tech = R.tech_effects((f or {}).get("tech") or {},
                                  tcfg=cfg.get("tech") or {})
            output_mult = 1.0 + float(pos_bonus.get("node_output", 0.0))
            output_mult += float(tech.get("output_bonus", 0.0))
            res_mult = tech.get("resource_output_mult") or {}
            node_t_mult = tech.get("node_type_output_mult") or {}
            for idx, node in enumerate(f_nodes):
                base = per_tick.get(node["type"]) or {}
                mult = global_mult * (1 + 0.2 * (node["level"] - 1))
                # 赛季决战 +20% / 追赶礼包 ×1.5（§11/§17）/ 司徒加成
                mult *= self.player_output_mult(p) * self.season_output_mult(world) \
                    * output_mult * self._node_output_mult(node, f)
                # §9.6 谣言/破坏：计略产出减益
                mult *= self.plot_output_mult(node)
                nm = node_mult.get(node["id"], {})
                # 商路加成只作用于据点自然资源，不叠加到灵石注入
                route_mult = 1.0 + float(route_out_bonus.get(node["id"], 0.0))
                nt_mult = node_t_mult.get(str(node["type"])) or {}
                coin_m = float(res_mult.get("coin", 1.0)) * float(
                    nt_mult.get("coin", 1.0))
                grain_m = float(res_mult.get("grain", 1.0)) * float(
                    nt_mult.get("grain", 1.0))
                iron_m = float(res_mult.get("iron", 1.0)) * float(
                    nt_mult.get("iron", 1.0))
                gain_coin = int(base.get("coin", 0) * mult * route_mult *
                                nm.get("coin", 1.0) * coin_m)
                gain_grain = int(base.get("grain", 0) * mult * route_mult *
                                 nm.get("grain", 1.0) * grain_m)
                gain_iron = int(base.get("iron", 0) * mult * route_mult *
                                nm.get("iron", 1.0) * iron_m)
                p["coin"] += gain_coin
                p["grain"] += gain_grain
                p["iron"] += gain_iron
                # 主玩法灵石注入（超出 6 节点部分 ×0.6，日限额在 _inject_gold）
                if idx >= diminish_after:
                    mult *= diminish_factor
                gross = int(gold_per_type.get(node["type"], 0) * mult
                            * float(res_mult.get("gold", 1.0))
                            * float(nt_mult.get("gold", 1.0)))
                personal = int(gross * float(gcfg.get("personal_share", 0.7)))
                if personal > 0:
                    # 补产出（output_only）照发，仍受日限额；p 就地更新，
                    # 循环尾统一 save_player 落库
                    got, _m = await self._inject_gold(p, personal)
                    gold_total += got
                # v4.5.7：玩家势力无国库，原本沉淀在国库的 30% 灵石份额
                # 一并注入产出玩家钱包（仍受 _inject_gold 日限额约束）。
                rest = gross - personal
                if rest > 0:
                    got, _m = await self._inject_gold(p, rest)
                    gold_total += got
            await self.save_player(p)
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
            iron_gain = 0
            for node in f_nodes:
                base = per_tick.get(node["type"]) or {}
                mult = global_mult * npc_bonus * (1 + 0.2 * (node["level"] - 1))
                mult *= self.season_output_mult(world) * self._node_output_mult(node, f)
                # §9.6 谣言/破坏：计略产出减益（与玩家据点同口径；
                # 此前 NPC 分支漏乘，谣言对 NPC 国库产出完全不生效）
                mult *= self.plot_output_mult(node)
                # §10.8 NPC 民心产出乘数：0.5 + 民心/100×0.5（0→50%，100→100%）
                mult *= R.npc_output_morale_mult(int(node.get("morale", 80) or 0))
                nm = node_mult.get(node["id"], {})
                route_mult = 1.0 + float(route_out_bonus.get(node["id"], 0.0))
                coin_gain += int(base.get("coin", 0) * mult * route_mult *
                                 nm.get("coin", 1.0))
                grain_gain += int(base.get("grain", 0) * mult * route_mult *
                                  nm.get("grain", 1.0))
                iron_gain += int(base.get("iron", 0) * mult * route_mult *
                                 nm.get("iron", 1.0))
            treasury["coin"] = int(treasury.get("coin", 0)) + coin_gain
            treasury["grain"] = int(treasury.get("grain", 0)) + grain_gain
            treasury["iron"] = int(treasury.get("iron", 0) or 0) + iron_gain
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
            owner_fid = int(ss.get("attacker_faction") or 0)
            terrain = str(node.get("terrain") or "plain")
            atk_friendly = await self._siege_side_ids(
                world, node, owner_fid) if owner_fid > 0 else set()
            attackers = [a for a in siege_armies
                         if owner_fid > 0 and
                         int(a["faction_id"]) in atk_friendly]
            # v4.8.2：只有原围城方兵力被消灭后，围城归属才允许变更。
            if not attackers:
                had_owner = owner_fid > 0
                troops_by_fid: dict[int, int] = {}
                for a in siege_armies:
                    fid = int(a["faction_id"])
                    if int(a.get("troops", 0) or 0) <= 0:
                        continue
                    troops_by_fid[fid] = troops_by_fid.get(fid, 0) + \
                        int(a.get("troops", 0) or 0)
                owner_fid = max(troops_by_fid, key=troops_by_fid.get) \
                    if troops_by_fid else 0
                atk_friendly = await self._siege_side_ids(
                    world, node, owner_fid) if owner_fid > 0 else set()
                attackers = [a for a in siege_armies
                             if int(a["faction_id"]) in atk_friendly]
                ss = {}
                if had_owner and owner_fid > 0:
                    reports.append(
                        f"🏳「{node['name']}」原围城方已溃散，"
                        f"{await self._faction_name(owner_fid)} 接管围城")
            atk_fid = owner_fid
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
            # v4.8.2：同一据点不允许「抢围城归属」。与攻方敌对的围城军
            # 需在抵达时打城下营地战；此处仅排除结算，不再强制撤围回城。
            for a in stale:
                if await self._consume_army_upkeep(world, a, terrain, True):
                    await self._auto_retreat(a)
                else:
                    await self.save_army(a)
            if stale:
                reports.append(
                    f"⛔「{node['name']}」已有围城方，"
                    f"{len(stale)} 支敌对军团原地对峙（不计入围城结算）")
            if not ss:
                ss = {"ticks": 0, "attacker_faction": atk_fid,
                      "choice": "auto", "since": _now()}
            ss["attacker_faction"] = atk_fid
            ss["ticks"] = int(ss.get("ticks", 0)) + 1
            # §7.2：围城期间双方粮耗照常。守军（据点的 idle 军团）与攻方
            # 都按自身携粮结算；断粮军团当 tick 撤离，避免零粮无限围城。
            defender_armies = [a for a in await self.get_armies(
                world["id"], node_id=nid, states=("idle",))
                if int(a["faction_id"]) not in atk_friendly]
            for d in defender_armies:
                # 守军驻守本方/盟友据点，按“不远离己方”结算（far=False）；
                # 此前与攻方一样硬编码 far=True，守城方被多收 1.5 倍粮耗。
                if await self._consume_army_upkeep(world, d, terrain, False):
                    await self._auto_retreat(d)
                else:
                    await self.save_army(d)
            still_attackers = []
            for a in attackers:
                if await self._consume_army_upkeep(world, a, terrain, True):
                    await self._auto_retreat(a)
                else:
                    still_attackers.append(a)
            attackers = still_attackers
            if not attackers:
                node["siege_state"] = {}
                await self.save_node(node)
                continue
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
            # 城防损伤；相邻友方领土提供小幅度城防减伤
            sim_attackers = [await self._army_sim(a) for a in attackers]
            dmg = R.siege_tick_damage([s for s in sim_attackers if s])
            terr = await self._territory_bonus(
                world, node, int(node.get("owner_faction", 0) or 0))
            if float(terr.get("defense_mult", 1.0)) > 1.0:
                dmg = max(0, int(round(
                    dmg / float(terr["defense_mult"]))))
            node["defense"] = max(0, node["defense"] - dmg)
            node["morale"] = max(20, node["morale"] - 2)
            destroyed = [a for a in attackers if a["troops"] <= 0]
            if destroyed:
                # A1：反击全歼的覆没善后（镜像 _apply_battle_aftermath 覆没分支）：
                # 角色伤势 +40（可能陨落）、军团删除、节点所有者被俘判定。
                # 此前仅从内存列表过滤，DB 残留 troops=0/state=siege 的僵尸军团。
                for a in destroyed:
                    a["_destroyed"] = True
                    self._remember_veteran_ashes(
                        world, a.get("owner_user_id", ""),
                        int(a.get("veteran", 0) or 0))
                    for cid in a.get("characters") or []:
                        c = await self.get_char(int(cid))
                        if not c:
                            continue
                        dead = R.apply_injury(c, 40)
                        c["status"] = "dead" if dead else "idle"
                        # §4.4：围城军被反杀覆没时退回最近己方据点
                        dest = await self._char_fallback_location(
                            int(world["id"]), c, int(node["id"]))
                        c["location"] = {"node_id": int(dest)} if dest else {}
                        await self.save_char(c)
                    # §10.7：NPC 围城军覆没时残粮退回国库（此前直接删行，
                    # 残粮随军团蒸发；玩家军团/魔潮无国库，函数内部自动跳过）
                    await self._refund_npc_army_grain(a)
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
        # v4.8.2：清理「没有围城军却仍挂着 siege_state」的悬挂状态。
        # 旧实现只在有围城军的节点上遍历，攻方全灭后城防会被永久锁住。
        for n in await self.get_nodes(world["id"]):
            if not (n.get("siege_state") or {}):
                continue
            if await self._active_manual_battle(world["id"], int(n["id"])):
                continue
            left = await self.get_armies(world["id"], node_id=int(n["id"]),
                                         states=("siege",))
            if not left:
                n["siege_state"] = {}
                await self.save_node(n)
        await self._commit()
        return reports

    async def _siege_breach(self, world: dict, node: dict, attackers: list[dict]) -> str:
        """城防归零：受降（3 倍威压）或巷战（§7.2）。"""
        cfg = self.main_cfg()
        scfg = cfg.get("siege") or {}
        ss = node.get("siege_state") or {}
        choice = ss.get("choice") or "auto"
        atk_fid = int(ss.get("attacker_faction") or 0)
        # 只结算当前主攻势力 + 协战军团（同联盟友方 / 与守方开战且不与
        # 主攻方敌对的增援），避免多势力围城时由旧势力残军决定归属。
        # v4.8.2：与 _step_sieges 的围城参与方口径保持一致。
        atk_friendly = await self._siege_side_ids(
            world, node, atk_fid) if atk_fid > 0 else set()
        attackers = [a for a in attackers
                     if atk_fid <= 0 or
                     int(a["faction_id"]) in atk_friendly]
        if not attackers:
            return ""
        if atk_fid <= 0:
            atk_fid = int(attackers[0]["faction_id"])
            atk_friendly = await self._siege_side_ids(world, node, atk_fid)
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
                      if int(a["faction_id"]) not in atk_friendly]
        def_power = sum(a["troops"] for a in def_armies) / 100 + garrison_troops / 100
        if choice == "auto" and atk_power >= float(cfg.get("intimidate_ratio", 3.0)) * max(
                def_power, 0.1):
            choice = "surrender"
        if choice == "surrender":
            keep = float(scfg.get("surrender_troops_keep", 0.5))
            # 保留比例统一交给 _capture_node 结算一次；此前先按 keep 写回，
            # _capture_node 又砍半，实际只剩 keep×50%。
            # §10.6：受降属无血开城，等级保留（by_force=False）
            await self._capture_node(world, node, atk_fid, garrison_keep=keep,
                                     by_force=False)
            note = node.get("_capture_note") or ""
            return (f"🏳️「{node['name']}」守军开城受降！"
                    f"（攻方兵力 {atk_power:.0f} ≥ 3 倍威压）"
                    + (f"\n{note}" if note else ""))
        # 巷战：攻 +30% / 守 -30%（street_fight_*）
        atk_bonus = float(scfg.get("street_fight_atk_bonus", 0.3))
        dfn_buff = await self._faction_buff(
            int(node.get("owner_faction", 0) or 0), "defense_bonus")
        terr = await self._territory_bonus(
            world, node, int(node.get("owner_faction", 0) or 0))
        defenders_sim = []
        for a in def_armies:
            s = await self._army_sim(a)
            if s:
                s["morale"] = int(s["morale"] * (1 - float(
                    scfg.get("street_fight_def_penalty", 0.3))))
                if dfn_buff:
                    s["def_terrain_mult"] = float(
                        s.get("def_terrain_mult", 1.0)) * (1 + dfn_buff)
                s["territory_atk_mult"] = float(terr["combat_mult"])
                s["territory_def_mult"] = float(terr["combat_mult"]) * \
                    float(terr["defense_mult"])
                defenders_sim.append((a, s))
        gar = self._garrison_sim(node)
        if gar:
            gar["morale"] = int(gar["morale"] * 0.7)
            if dfn_buff:
                gar["def_terrain_mult"] = float(
                    gar.get("def_terrain_mult", 1.0)) * (1 + dfn_buff)
            gar["territory_atk_mult"] = float(terr["combat_mult"])
            gar["territory_def_mult"] = float(terr["combat_mult"]) * \
                float(terr["defense_mult"])
            defenders_sim.append((None, gar))
        for s in atk_sim:
            s["atk_terrain_mult"] = float(s.get("atk_terrain_mult", 1.0)) * (1 + atk_bonus)
        # v4.3.13：巷战按 wall 语境套用科技地形加成（攻方城墙伤害 / 守方城墙守军防御）
        await self._apply_tech_terrain(
            atk_sim + [s for _a, s in defenders_sim], "wall")
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
                            garrison_keep: float = 0.5,
                            by_force: bool = True) -> list[str]:
        """据点易主：驻军溃散、保护期、关押俘虏解放（返回获释名单）。

        ``garrison_keep``：易主后保留的守军比例（受降可配置）；只在这里
        结算一次，调用方不得预先缩放，避免重复减半。
        ``by_force``：易主方式（§10.6）。强攻破城 → 等级 -1（最低 1）；
        受降/劝降等无血开城 → 等级保留；两种方式都会作废在建工程。
        """
        # 兜底：灭亡势力不得通过任何调用路径重新占城（否则会出现
        # alive=0 却拥有节点的幽灵势力，NPC 轮转永不回收）。
        capture_fid = int(faction_id or 0)
        if capture_fid > 0:
            capture_f = await self.get_faction(capture_fid)
            if capture_f is not None and "alive" in capture_f and \
                    int(capture_f.get("alive") or 0) <= 0:
                logger.info(
                    f"[strategy] 已灭亡势力#{capture_fid} 试图占领节点"
                    f"{node.get('id')}，已拒绝")
                return []
        prev_owner = int(node.get("owner_faction", 0))
        node["owner_faction"] = int(faction_id)
        node["governor_char_id"] = 0  # 易主后太守自动卸任，由新主重新任命
        node["siege_state"] = {}
        # §10.6 易主联动：强攻破城 -1 级（最低 1）；受降保留等级
        if by_force and prev_owner > 0 and int(faction_id) > 0 and \
                int(faction_id) != prev_owner:
            node["level"] = max(1, int(node.get("level", 1) or 1) - 1)
        # 无论强攻还是受降：在建工程一律作废（资源不退）；NPC 发展稳定期
        # 由 develop_ready_at 重新起算（懒初始化由 NPC 发展代理完成）
        node["construction"] = {}
        node["develop_ready_at"] = 0
        # 玩家占领后据点不再是匪寨：清除 data.bandits / lair 标记，防止
        # 新主的城继续劫掠商路粮队、继续被匪寨 tick 抽成。
        await self._clear_bandit_marker(world, node)
        # v4.6.1：战果 / 龟缩统计 + 风声（据点易主）
        if int(faction_id) > 0 and prev_owner != int(faction_id):
            # 玩家联盟按盟主统计战果，避免打盟员据点不推进对盟主战争的 war_score
            prev_dip = prev_owner
            new_dip = int(faction_id)
            try:
                if prev_owner > 0:
                    pf = await self.get_faction(prev_owner)
                    pf = await self._alliance_host(pf) if pf else None
                    if pf:
                        prev_dip = int(pf["id"])
                nf = await self.get_faction(int(faction_id))
                nf = await self._alliance_host(nf) if nf else None
                if nf:
                    new_dip = int(nf["id"])
            except Exception:  # noqa: BLE001 - 战果统计失败不阻塞占城
                logger.warning("[strategy] 易主战果统计归属解析失败",
                               exc_info=True)
            self._note_threat_capture(
                world, prev_dip, new_dip, node.get("name", ""))
        node["morale"] = 50
        # 城防重置（§10.2：潜力放开后 mine/grain/market 也有城防，统一按
        # 类型判定；v4.5.2 遗迹同为可经营据点，按 20% 起步后靠自修/修筑成长）
        node["defense"] = int(R.city_defense_max(node["level"]) * 0.2)
        cur_gar = node.get("npc_garrison") or {}
        cur_troops = int(cur_gar.get("troops", 0) or 0)
        keep = max(0.0, min(1.0, float(garrison_keep)))
        # 保留兵力不得超过原守军，也不得超过新等级守军上限（§10.4）
        # 此前 max(200, 残军×0.5) 会让少量残军被凭空补到 200，攻下方
        # 白得一支比原守军还多的驻军。
        node["npc_garrison"] = {
            "troops": min(R.garrison_cap(node["level"]),
                          int(cur_troops * keep)) if cur_troops > 0 else 0,
            "morale": 60,
        }
        # 新占节点保护期（§17：24h 免战，防滚雪球）
        pcfg = self.main_cfg().get("protection") or {}
        if prev_owner != int(faction_id):
            if int(faction_id) > 0:
                node["protect_until"] = _now() + int(
                    pcfg.get("new_node_hours", 24)) * 3600
            else:
                # 魔潮（-1）与归还无主（0）不继承旧保护期：夺下即开放反攻
                node["protect_until"] = 0
        # 驻守/围城的敌方军团溃散处理：城破后不能留下仍处 siege 的
        # 旧围城军（否则会把新主的城继续围住，甚至绕过新主保护期）。
        # 同联盟协战军团不算敌方，转为驻守，避免盟友被当成败军击溃。
        new_friendly = await self._friendly_ids(int(world["id"]), int(faction_id))
        for a in await self.get_armies(world["id"], node_id=node["id"],
                                       states=("idle", "siege")):
            if int(a["faction_id"]) in new_friendly:
                if a.get("state") == "siege":
                    a["state"] = "idle"
                    a["path"] = []
                    a["edge_pos"] = 0.0
                    await self.save_army(a)
                continue
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
        # §6.2 秘境首占掉落：v4.5.2 起封存（``ruin_first_capture.enabled``
        # 默认 False）。遗迹改为常规据点、走每 tick 持续产出，不再发
        # 装备/图纸；这里保留完整逻辑线，方便后续版本按需重新启用。
        if self._ruin_first_capture_enabled() and node["type"] == "ruin" \
                and prev_owner != int(faction_id) and int(faction_id) > 0:
            out = node.get("output") or {}
            if not int(out.get("first_captured", 0) or 0):
                out["first_captured"] = 1
                node["output"] = out
                rng = self._rng("ruin_capture", world["id"], node["id"],
                                world["tick_no"])
                chance = float((self.main_cfg().get("ruin_first_capture") or {})
                               .get("chance", 0.3) or 0.3)
                chance = max(0.0, min(1.0, chance))
                if rng.random() < chance:
                    notes.append(await self._ruin_first_capture_reward(
                        world, node, int(faction_id)))
        await self.save_node(node)
        node["_capture_note"] = "；".join(n for n in notes if n)
        # §6.4 势力任务：开疆拓土 / 守土安民
        await self._faction_task_progress(int(faction_id), "conquer", 1)
        if prev_owner:
            await self._faction_task_progress(int(prev_owner), "defend", 1)
        # v4.7.0：据点易主写入江湖纪事
        if prev_owner != int(faction_id):
            try:
                nf = await self.get_faction(int(faction_id)) if int(faction_id) > 0 else None
                pf = await self.get_faction(prev_owner) if prev_owner > 0 else None
                nf_name = str((nf or {}).get("name") or
                              ("魔潮" if int(faction_id) == -1 else "无主"))
                pf_name = str((pf or {}).get("name") or
                              ("魔潮" if prev_owner == -1 else "无主"))
                await self._log_action(
                    world, "capture", "capture",
                    actor_faction=int(faction_id), actor_name=nf_name,
                    target_faction=prev_owner, target_name=pf_name,
                    node_id=int(node["id"]), node_name=str(node.get("name") or ""),
                    success=1,
                    summary=(f"🏯「{node.get('name', '')}」易主：{pf_name} → {nf_name}"
                             + ("（强攻破城）" if by_force else "（无血开城）")),
                    detail={"by_force": bool(by_force),
                            "prev_owner": prev_owner,
                            "new_owner": int(faction_id),
                            "level": int(node.get("level", 1) or 1)},
                    visibility="public")
            except Exception:  # noqa: BLE001 - 纪事失败不影响占城
                logger.debug("[strategy] 据点易主纪事写入失败", exc_info=True)
        # 关押于此的俘虏解放（§7.9）
        return await self.liberate_captives_at_node(world, node, int(faction_id))

    def _ruin_first_capture_enabled(self) -> bool:
        """遗迹首占装备/图纸掉落开关（§6.2）。

        v4.5.2 封存：装备与图纸暂不引入（主玩法没有装备图纸物品类型），
        遗迹改按常规据点持续产出。配置 ``ruin_first_capture.enabled``
        默认 False；改回 True 即恢复旧行为（含精铁兜底）。
        """
        spec = self.main_cfg().get("ruin_first_capture") or {}
        return bool(spec.get("enabled", False))

    async def _ruin_first_capture_reward(self, world: dict, node: dict,
                                         faction_id: int) -> str:
        """秘境首占掉落（§6.2）：优先主玩法装备，缺主玩法上下文时折为军资。

        .. note::
           已由 ``_ruin_first_capture_enabled`` 封存（默认不调用）。其中的
           「图纸残卷」只是精铁兜底文案，主玩法并没有装备图纸物品；重新
           启用前需先补齐真正的图纸道具。
        """
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

    def _remember_veteran_ashes(self, world: dict, owner_user_id: str,
                                veteran: int) -> int:
        """军团全灭后保留一半老兵值（§7.5「全灭减半」）。

        旧实现直接删除军团，``veteran`` 随行丢失，与前端指引「全灭减半」
        不符。这里按 owner 记在 ``world.data.veteran_ashes``，下次编军时
        继承；解散/主动重编仍然清零。
        """
        keep = int(int(veteran or 0) // 2)
        if not owner_user_id or keep <= 0:
            return 0
        data = world.get("data") or {}
        ashes = data.get("veteran_ashes") or {}
        if keep > int(ashes.get(str(owner_user_id), 0) or 0):
            ashes[str(owner_user_id)] = keep
        data["veteran_ashes"] = ashes
        world["data"] = data
        return keep

    async def _sync_army_chars_location(self, army: dict) -> None:
        """把随军角色所在地同步为军团 ``node_id``（§4.2）。

        行军/护送等直接改写 ``army.node_id`` 的路径必须调用，否则成员
        location 会与军团脱节（下车后仍停在旧据点，可异地派遣/编军）。
        """
        node_id = int(army.get("node_id") or 0)
        if node_id <= 0:
            return
        for cid in army.get("characters") or []:
            c = await self.get_char(int(cid))
            if not c or c.get("status") != "army":
                continue
            if R.char_node_id(c) == node_id:
                continue
            c["location"] = {"node_id": node_id}
            await self.save_char(c)

    async def _release_army_chars(self, army: dict):
        """军团解散时归还角色：保留陨落/被俘状态，其余恢复空闲。"""
        for cid in army.get("characters") or []:
            c = await self.get_char(int(cid))
            if not c or c.get("status") in ("dead", "captive"):
                continue
            c["status"] = "dead" if R.injury_state(
                c.get("injury", 0)) == "dead" else "idle"
            # §4.2：成员 location 随军团，解散后留在最后停靠据点；
            # §4.4：若最后停靠点已是敌方/无主（覆没于敌城），退回最近己方据点。
            dest = await self._char_fallback_location(
                int(army.get("world_id") or 0), c, int(army.get("node_id") or 0))
            c["location"] = {"node_id": int(dest)} if dest else {}
            await self.save_char(c)

    async def _refund_npc_army_grain(self, army: dict) -> int:
        """NPC 军团解散/全灭前把残粮退回所属势力国库（§10.7）。

        仅处理 ``faction_id > 0`` 且无玩家归属的军团；玩家军团与魔潮
        （faction=-1，无国库）不退还。退还后把 ``army.grain`` 置 0 防重复。
        """
        grain = max(0, int(army.get("grain", 0) or 0))
        fid = int(army.get("faction_id", 0) or 0)
        if grain <= 0 or fid <= 0 or str(army.get("owner_user_id") or ""):
            return 0
        f = await self.get_faction(fid)
        if not f or str(f.get("kind") or "") == "player":
            return 0
        self._treasury_add(f, grain=grain)
        await self.save_faction(f)
        army["grain"] = 0
        return grain

    async def _merge_idle_npc_army(self, world: dict, army: dict,
                                   node: dict) -> None:
        """把停在己方据点的 NPC 闲置军团并入驻军并解散。

        NPC 军团撤回本城后会长期以 ``idle`` 滞留：既不会归队也不会消失，
        也不会被再次出征复用（``_spawn_npc_army`` 只从驻军抽兵）。
        长局实测 1500 tick 可累积 60+ 支、3 万兵力的闲置军团，单 tick
        的遍历 / 士气写库开销随军团数线性膨胀。这里统一并入驻军：
        兵力按原地转入（不打折、不凭空增加），超过 ``garrison_cap`` 的
        部分不再参与自然增长，但既有守军不会被裁掉。
        """
        fid = int(army.get("faction_id", 0) or 0)
        if fid <= 0 or str(army.get("owner_user_id") or ""):
            return
        f = await self.get_faction(fid)
        if not f or str(f.get("kind") or "") == "player":
            return
        await self._interrupt_army_tasks(world, army)
        troops = max(0, int(army.get("troops", 0) or 0))
        if troops > 0:
            garrison = node.get("npc_garrison") or {}
            garrison["troops"] = int(
                garrison.get("troops", 0) or 0) + troops
            node["npc_garrison"] = garrison
            await self.save_node(node)
        await self._refund_npc_army_grain(army)
        await self._release_army_chars(army)
        await self.delete_army(int(army["id"]))

    async def _auto_retreat(self, army: dict, pursuer_faction: int = 0,
                            pursuer_traits: Optional[list] = None,
                            pursuer_cavalry: bool = False):
        """战败/超时/断粮自动撤往最近己方据点；无路可退或已无兵则解散。

        §7.4 溃败善后：溃逃军团额外折损 ``battle.rout_troops_pct``、士气压至
        ``battle.rout_morale``、主将按 ``rout_leader_injury_chance`` 负伤，并
        进入 ``reorganize_ticks`` tick 的重整期；追击方可用 ``R.pursuit_capture``
        截获溃逃主将（转 captive）。
        """
        # 溃败/断粮撤退同样视为出军：中断随军任务
        wid0 = int(army.get("world_id") or 0)
        if wid0 > 0:
            await self._interrupt_army_tasks({"id": wid0}, army)
        # v4.8.0：联军成员溃散/断粮撤退时解散联军，避免留下单支“半联军”
        cid0 = int(army.get("coalition_id") or 0)
        if cid0 > 0:
            await self._dissolve_coalition(cid0, reason="军团溃败/断粮，联军解散")
            army["coalition_id"] = 0
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
                    (pf or {}).get("tech") or {},
                    tcfg=self.main_cfg().get("tech") or {}
                ).get("intercept_bonus", 0.0))
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
                                {"faction_id": pfid},
                                int(army.get("node_id", 0) or 0),
                                dwell_only=True)
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
            await self._refund_npc_army_grain(army)
            await self._release_army_chars(army)
            await self.delete_army(army["id"])
            return
        home = await self._nearest_own_node(
            {"id": army["world_id"]}, {"faction_id": army["faction_id"]},
            army["node_id"], dwell_only=True)
        if home is None:
            await self._refund_npc_army_grain(army)
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

    # --- 军团补粮 / 运粮队（§6.2）-------------------------------------------
    async def _army_grain_cap(self, army: dict,
                              node: Optional[dict] = None) -> int:
        """军团携粮上限 = 主将统率×50，粮仓每级 +10%（与编军同口径）。"""
        leader = await self._army_nominal_leader(army)
        cap = R.carry_grain_cap(leader)
        if node is not None:
            lvl = min(5, self._building_level(node, "granary"))
            if lvl:
                cap = int(cap * (1 + 0.1 * lvl))
        return max(1, cap)

    async def _resolve_supply_army(self, p: dict, world: dict,
                                   node_id: int, army_id: int) -> dict:
        """运粮目标军团：显式 army_id 优先；未指定时仅允许该据点唯一军团。"""
        if int(army_id or 0):
            army = await self._own_army(p, int(army_id))
            if int(army.get("troops", 0) or 0) <= 0:
                raise StrategyError("目标军团已无兵力")
            return army
        armies = [a for a in await self.get_armies(
            world["id"], faction_id=int(p["faction_id"]),
            node_id=int(node_id)) if int(a.get("troops", 0) or 0) > 0]
        if not armies:
            raise StrategyError("目标据点没有我方军团可补给")
        if len(armies) > 1:
            names = "、".join(f"{a['name']}(#{a['id']})" for a in armies)
            raise StrategyError(
                f"该据点有多支军团，请指定要补给的军团：{names}")
        return armies[0]

    async def _finish_supply_task(self, world: dict, task_id: int,
                                  status: str = "done") -> None:
        """结束运粮任务并释放执行角色（供追上/失败/召回共用）。"""
        if int(task_id or 0) <= 0:
            return
        row = await self._fetchone(
            "SELECT * FROM strategy_tasks WHERE id=? AND world_id=?",
            (int(task_id), world["id"]))
        task = self._decode("strategy_tasks", row)
        if not task or task.get("status") != "active":
            return
        # 送达（done）时回写 delivered=1：任务展示的「补粮队在途/已注入」
        # 与召回判定都依赖该字段，此前只改 status，delivered 永远为 0。
        if status == "done":
            payout = task.get("payout") or {}
            payout["delivered"] = 1
            await self.db.conn.execute(
                "UPDATE strategy_tasks SET status=?, payout_json=? WHERE id=?",
                (status, json.dumps(payout, ensure_ascii=False), int(task_id)))
        else:
            await self.db.conn.execute(
                "UPDATE strategy_tasks SET status=? WHERE id=?", (status, int(task_id)))
        c = await self.get_char(int(task["character_id"]))
        payout = task.get("payout") or {}
        # §4.4 远征任务结束回出发地；出发地易主/消失则退最近己方据点。
        origin = int(payout.get("origin_node_id") or task.get("node_id") or 0)
        dest = 0
        if c:
            dest = await self._char_fallback_location(
                int(world["id"]), c, origin)
        if c and c.get("status") == "task":
            R.gain_exp(c, max(1, int((task.get("payout") or {}).get("ticks", 4)) * 2),
                       self._growth_cap_bonus())
            c["status"] = "idle"
            c["location"] = {"node_id": int(dest)} if dest else {}
            await self.save_char(c)
        partner_id = int((task.get("payout") or {}).get("partner_char_id") or 0)
        if partner_id:
            partner = await self.get_char(partner_id)
            if partner and partner.get("status") == "task":
                partner["status"] = "idle"
                pdest = await self._char_fallback_location(
                    int(world["id"]), partner, origin)
                partner["location"] = {"node_id": int(pdest)} if pdest else {}
                await self.save_char(partner)

    async def _refund_supply_grain(self, world: dict, sp: dict,
                                   amount: int) -> int:
        amount = max(0, int(amount or 0))
        if amount <= 0:
            return 0
        owner = str(sp.get("owner") or "")
        if not owner:
            return 0
        pp = await self.get_player(owner, world["id"])
        if not pp:
            return 0
        pp["grain"] = int(pp.get("grain", 0) or 0) + amount
        await self.save_player(pp)
        return amount

    async def _cancel_army_supply(self, world: dict, task_id: int,
                                  refund: bool = True) -> int:
        """取消某任务对应的补粮队；返回退还给玩家的粮草。"""
        data = world.get("data") or {}
        supplies = data.get("army_supplies") or []
        kept, refunded = [], 0
        for sp in supplies:
            if int(sp.get("task_id", 0) or 0) == int(task_id):
                if refund:
                    refunded += await self._refund_supply_grain(
                        world, sp, int(sp.get("grain", 0) or 0))
                continue
            kept.append(sp)
        if len(kept) != len(supplies):
            data["army_supplies"] = kept
            world["data"] = data
            await self.save_world(world)
        return refunded

    async def _deliver_army_supply(self, world: dict, sp: dict,
                                   army: dict) -> int:
        """把补粮队粮草送入军团（受携粮上限约束，多余退还玩家）。"""
        grain = int(sp.get("grain", 0) or 0)
        cap = int(sp.get("cap", 0) or 0) or await self._army_grain_cap(army)
        room = max(0, cap - int(army.get("grain", 0) or 0))
        moved = min(grain, room)
        if moved > 0:
            army["grain"] = int(army.get("grain", 0) or 0) + moved
            await self.save_army(army)
        if grain > moved:
            await self._refund_supply_grain(world, sp, grain - moved)
        await self._finish_supply_task(world, int(sp.get("task_id", 0) or 0), "done")
        owner = str(sp.get("owner") or "")
        if owner:
            await self._notify(
                world, [owner], "supply_arrived",
                {"army_id": int(army["id"]), "node_id": int(army["node_id"]),
                 "text": f"🚚 补粮队追上「{army['name']}」，交付粮草 {moved}"
                         + (f"（携粮已满，退还 {grain - moved}）"
                            if grain > moved else "")})
        return moved

    async def _step_army_supplies(self, world: dict):
        """补粮队追击行军军团（§6.2）。

        补粮队速度高于最快速行军军团（默认 4 段/tick，普通军团最高约
        3.07），从后方出发也能追上；抵达目标所在节点即交付。目标解散/
        覆没时剩余粮草退还玩家。途中可被匪寨/敌军劫掠（与运粮队同口径）。
        """
        data = world.get("data") or {}
        supplies = data.get("army_supplies") or []
        if not supplies:
            return
        ccfg = self.main_cfg().get("convoy") or {}
        speed = max(1, int(ccfg.get("army_supply_speed_edges", 4)))
        raid_pct = float(ccfg.get("raid_take_pct", 0.5))
        timeout = int(ccfg.get("army_supply_timeout_ticks", 672))
        tick_no = int(world.get("tick_no", 0) or 0)
        fn = await self._ally_or_self_fn(world)
        remaining = []
        for sp in supplies:
            army = await self.get_army(int(sp.get("army_id", 0) or 0))
            task_id = int(sp.get("task_id", 0) or 0)
            if not army or int(army.get("faction_id", 0) or 0) != int(
                    sp.get("faction", 0) or 0) or int(
                    army.get("troops", 0) or 0) <= 0:
                refunded = await self._refund_supply_grain(
                    world, sp, int(sp.get("grain", 0) or 0))
                await self._finish_supply_task(world, task_id, "cancelled")
                if sp.get("owner"):
                    await self._notify(
                        world, [sp["owner"]], "supply_lost",
                        {"text": f"🚚 补粮目标已不存在，粮草 {refunded} 已退回"})
                continue
            # 总时限兜底：即使路线可达，也避免玩家反复改道导致角色永久占用
            if tick_no - int(sp.get("created_tick", tick_no) or tick_no) > timeout:
                refunded = await self._refund_supply_grain(
                    world, sp, int(sp.get("grain", 0) or 0))
                await self._finish_supply_task(world, task_id, "cancelled")
                if sp.get("owner"):
                    await self._notify(
                        world, [sp["owner"]], "supply_lost",
                        {"text": f"🚚 补粮队超时未送达，粮草 {refunded} 已退回"})
                continue
            # 目标：行军军团追其终点；驻守/围城则追当前节点
            dest = int(army.get("node_id", 0) or 0)
            if army.get("state") == "march" and army.get("path"):
                dest = int(army["path"][-1])
            cur = int(sp.get("node_id", 0) or 0)
            # 已与军团同节点（且军团不在半格行军中）→ 交付
            at_army = cur == int(army.get("node_id", 0) or 0) and (
                army.get("state") != "march" or
                float(army.get("edge_pos", 0) or 0) <= 0)
            if at_army:
                await self._deliver_army_supply(world, sp, army)
                continue
            path = await self.find_path(world["id"], cur, dest)
            if not path or len(path) < 2:
                sp["wait_ticks"] = int(sp.get("wait_ticks", 0) or 0) + 1
                if sp["wait_ticks"] > timeout:
                    refunded = await self._refund_supply_grain(
                        world, sp, int(sp.get("grain", 0) or 0))
                    await self._finish_supply_task(world, task_id, "cancelled")
                    continue
                remaining.append(sp)
                continue
            step = min(speed, len(path) - 1)
            wiped = False
            for cell in path[1:step + 1]:
                raider = await self._find_convoy_raider(
                    world, int(cell), int(sp.get("faction", 0) or 0), fn)
                if not raider:
                    continue
                rng = self._rng("supply", sp["id"], cell)
                if not R.convoy_raid_success(
                        rng, int(ccfg.get("troops", 100)) * 2,
                        int(raider.get("troops", 0) or 0)):
                    continue
                take = min(int(sp.get("grain", 0) or 0),
                           max(1, int(int(sp.get("grain", 0) or 0) * raid_pct)))
                sp["grain"] = int(sp.get("grain", 0) or 0) - take
                await self._give_raid_spoils(world, raider, take)
                if sp.get("owner"):
                    await self._notify(
                        world, [sp["owner"]], "supply_raided",
                        {"text": f"🚚 补粮队遭劫，损失粮草 {take}"
                                 f"（剩余 {int(sp.get('grain', 0))}）"})
                if int(sp.get("grain", 0) or 0) <= 0:
                    await self._finish_supply_task(world, task_id, "cancelled")
                    wiped = True
                    break
            if wiped:
                continue
            # 本 tick 跨多段时若正好经过军团所在节点，立即视为追上
            step_nodes = [int(x) for x in path[1:step + 1]]
            if int(army.get("node_id", 0) or 0) in step_nodes and (
                    army.get("state") != "march" or
                    float(army.get("edge_pos", 0) or 0) <= 0):
                sp["node_id"] = int(army["node_id"])
                await self._deliver_army_supply(world, sp, army)
                continue
            sp["node_id"] = int(path[step])
            sp["wait_ticks"] = 0
            remaining.append(sp)
        data["army_supplies"] = remaining
        world["data"] = data
        await self.save_world(world)
        await self._commit()

    # --- 粮耗 / 断粮（§5.6，行军与围城共用）-------------------------------
    async def _army_upkeep_amount(self, army: dict, terrain: str,
                                  far: bool) -> int:
        """军团单 tick 粮耗：兵种基础 × 地形 × 距离 × 特技 × 科技 × 动员令。"""
        faction = await self.get_faction(int(army.get("faction_id") or 0))
        leader = None
        cids = army.get("characters") or []
        if cids:
            leader = await self.get_char(int(cids[0]))
        mods = R.trait_army_mods((leader or {}).get("traits"))
        pmods_grain = R.personality_mods((leader or {}).get("personality"))
        tech = R.tech_effects((faction or {}).get("tech") or {},
                              tcfg=self.main_cfg().get("tech") or {})
        # v4.3.13 军事 L4 精锐小队：粮耗可选折减（默认 1.0，未配置不生效）
        small_grain = 1.0
        if int(army.get("troops", 0) or 0) <= int(
                tech.get("small_army_threshold", 800) or 800):
            small_grain = float(tech.get("small_army_grain_mult", 1.0) or 1.0)
        upkeep = R.grain_upkeep(army, terrain, far) * \
            float(mods.get("grain_mult", 1.0)) * \
            float(pmods_grain.get("grain_mult", 1.0)) * \
            float(tech.get("grain_cost_mult", 1.0)) * small_grain * \
            await self._faction_buff(int(army.get("faction_id") or 0),
                                     "grain_cost_mult")
        return max(0, int(round(upkeep)))

    async def _consume_army_upkeep(self, world: dict, army: dict,
                                   terrain: str, far: bool,
                                   free: bool = False) -> bool:
        """结算军团本 tick 粮耗；断粮返回 True（调用方负责撤退/清理）。

        ``free=True``：友方道路 / 盟友据点线路免粮，只标记本 tick 已结算。
        同一 tick 只结算一次：围城断粮后 ``_auto_retreat`` 会把军团改成
        行军态，随后 ``_step_march`` 不应再次扣粮/重复逃散。
        """
        tick_no = int(world.get("tick_no", 0) or 0)
        if int(army.get("_upkeep_tick", -1)) == tick_no:
            return False
        army["_upkeep_tick"] = tick_no
        if free:
            army["starve_ticks"] = 0
            return False
        upkeep = await self._army_upkeep_amount(army, terrain, far)
        acfg = self.main_cfg().get("army") or {}
        if int(army.get("grain", 0) or 0) >= upkeep:
            army["grain"] = int(army.get("grain", 0) or 0) - upkeep
            army["starve_ticks"] = 0
            return False
        army["grain"] = 0
        faction = await self.get_faction(int(army.get("faction_id") or 0))
        tech = R.tech_effects((faction or {}).get("tech") or {},
                              tcfg=self.main_cfg().get("tech") or {})
        # 农政科技 L5「屯粮」：断粮抵抗 tick 数（期间只扣士气、不逃兵）
        resist = int(tech.get("starve_resist_ticks", 0))
        starve_n = int(army.get("starve_ticks", 0) or 0) + 1
        army["starve_ticks"] = starve_n
        # 军事 L6 军心：断粮士气下降 -20%
        morale_drop = int(int(acfg.get("starve_morale_per_tick", 20)) *
                          float(tech.get("morale_loss_mult", 1.0) or 1.0))
        # 嗜酒：断粮时士气额外 -10（§4.9）；按名义主将结算（重伤可挂名）
        leader = await self._army_nominal_leader(army)
        pmods = R.personality_mods((leader or {}).get("personality"))
        morale_drop += int(pmods.get("starve_morale_extra", 0) or 0)
        army["morale"] = max(
            0, int(army.get("morale", 100) or 0) - morale_drop)
        desert = 0 if starve_n <= resist else int(
            int(army.get("troops", 0) or 0) *
            float(acfg.get("starve_desert_pct", 0.05)))
        army["troops"] = max(0, int(army.get("troops", 0) or 0) - desert)
        if int(army.get("morale", 0) or 0) <= 0 or \
                int(army.get("troops", 0) or 0) <= 0:
            army["starve_ticks"] = 0
            return True
        return False

    # --- 步骤 4：行军推进 + 粮耗（§5.6）------------------------------------
    async def _march_metrics(self, world: dict, a: dict, nodes: dict,
                             friendly_cache: dict, tech_cache: dict,
                             road_mult: float, road_free: bool,
                             ally_free: bool, ally_speed: float) -> Optional[dict]:
        """单个行军军团本 tick 的推进参数。

        抽出口径供联军 lockstep 复用（取成员最小速度统一步进，避免
        一快一慢把联军拉扯成两支孤立军团）。
        """
        nxt_id = (a.get("path") or [None])[0]
        if not nxt_id:
            return None
        nxt = nodes.get(nxt_id)
        if not nxt:
            return None
        own_fid = int(a.get("faction_id") or 0)
        cur = nodes.get(int(a.get("node_id", 0) or 0))
        friendly = await self._friendly_ids(
            int(world["id"]), own_fid, friendly_cache)
        on_road = R.is_friendly_road(cur, nxt, friendly)
        terrain = nxt.get("terrain", "plain")
        leader = None
        cids = a.get("characters") or []
        if cids:
            leader = await self.get_char(int(cids[0]))
        mods = R.trait_army_mods((leader or {}).get("traits"))
        # 友方道路：相邻两个本方/盟友据点之间构成道路，额外加速。
        speed_mult = road_mult if on_road else 1.0
        # 同联盟额外行军加成（与道路加速叠乘）。
        if len(friendly) > 1:
            speed_mult *= 1.0 + ally_speed
        # v4.3.13 军事 L4 精锐小队：带兵 ≤ 阈值时行军机动 +1 边当量
        teff = tech_cache.get(own_fid)
        if teff is None:
            mf = await self.get_faction(own_fid) if own_fid > 0 else None
            teff = R.tech_effects((mf or {}).get("tech") or {},
                                  tcfg=self.main_cfg().get("tech") or {})
            tech_cache[own_fid] = teff
        a["move_bonus"] = (
            int(teff.get("small_army_move_bonus", 0) or 0)
            if int(a.get("troops", 0) or 0) <=
            int(teff.get("small_army_threshold", 800) or 800) else 0)
        advance = (R.march_speed(a, terrain) *
                   float(mods.get("speed_mult", 1.0)) * speed_mult)
        next_owner = int(nxt.get("owner_faction") or 0)
        free = (on_road and road_free) or (
            ally_free and next_owner in friendly and next_owner != own_fid)
        return {"nxt_id": nxt_id, "advance": float(advance),
                "terrain": terrain, "free": bool(free),
                "cost": float(R.TERRAIN_MARCH_COST.get(terrain, 1.0))}

    async def _step_march(self, world: dict):
        # v4.8.0：先校正联军（成员缺失/异地/状态异常自动解散）
        await self._ensure_coalition_consistency(world)
        marching = await self.get_armies(world["id"], states=("march",))
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        tcfg = self.main_cfg().get("territory") or {}
        acfg = self.main_cfg().get("alliance") or {}
        road_mult = max(1.0, float(tcfg.get("road_speed_mult", 1.5) or 1.5))
        road_free = bool(tcfg.get("road_free_upkeep", True))
        ally_free = bool(acfg.get("ally_route_free_upkeep", True))
        ally_speed = max(0.0, float(acfg.get("march_speed_bonus", 0.10) or 0))
        friendly_cache: dict[int, set[int]] = {}
        tech_cache: dict[int, dict] = {}
        metrics: dict[int, dict] = {}
        coalition_advance: dict[int, float] = {}
        for a in marching:
            mt = await self._march_metrics(
                world, a, nodes, friendly_cache, tech_cache,
                road_mult, road_free, ally_free, ally_speed)
            if mt is None:
                continue
            metrics[int(a["id"])] = mt
            cid = int(a.get("coalition_id") or 0)
            if cid > 0:
                coalition_advance[cid] = min(
                    coalition_advance.get(cid, 1e18), mt["advance"])
        for a in marching:
            if not a["path"]:
                a["state"] = "idle"
                await self.save_army(a)
                continue
            mt = metrics.get(int(a["id"]))
            if not mt:
                a["path"] = []
                a["state"] = "idle"
                await self.save_army(a)
                continue
            cid = int(a.get("coalition_id") or 0)
            advance = (coalition_advance.get(cid, mt["advance"])
                       if cid > 0 else mt["advance"])
            a["edge_pos"] = float(a.get("edge_pos", 0)) + advance
            cost = mt["cost"]
            nxt_id = mt["nxt_id"]
            # 先推进再结算粮耗：断粮触发撤退时本 tick 的行军进度不会白费
            if a["edge_pos"] >= cost:
                a["edge_pos"] -= cost
                a["node_id"] = nxt_id
                a["path"] = a["path"][1:]
                # §4.2：军团成员 location 随军团 node_id 同步（抵达/推进后）
                for cid_ch in a.get("characters") or []:
                    mc = await self.get_char(int(cid_ch))
                    if not mc or mc.get("status") != "army":
                        continue
                    if R.char_node_id(mc) == int(a["node_id"]):
                        continue
                    mc["location"] = {"node_id": int(a["node_id"])}
                    await self.save_char(mc)
            # 粮耗（行军中每 tick；疾行 -10% / 动员令 / 农政科技 grain_cost_mult）
            far = len(a["path"]) > int((self.main_cfg().get("army") or {}).get(
                "far_supply_steps", 2))
            starved = await self._consume_army_upkeep(
                world, a, mt["terrain"], far, free=mt["free"])
            if starved:
                if cid > 0:
                    # 联军断粮：整队撤回最近己方据点（避免一支断粮、另一支
                    # 孤军深入），随后解散联军，回城后可重新合军。
                    co = await self.get_coalition(cid)
                    mates = await self._coalition_members(co) if co else []
                    await self._dissolve_coalition(
                        cid, reason="断粮撤退，联军解散", world=world)
                    for m in mates:
                        if int(m["id"]) == int(a["id"]):
                            continue
                        m["coalition_id"] = 0
                        await self._auto_retreat(m)
                await self._auto_retreat(a)
                continue
            await self.save_army(a)
        # v4.8.0：推进结束同步联军状态（全员抵达 → 待命）
        await self._ensure_coalition_consistency(world)
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
        char_by_id: dict[int, dict] = {}
        # 落点安全网：城陷/易主后可能留下 idle/task/研究中的角色指向敌方、
        # 无主或已删除据点（各占领路径未必都走 roll_captures 的清理）。
        # 每 tick 校正一次，保证“人物永不落在敌方/无主/不可停留据点”的不变量，
        # 也让旧存档无需等玩家打开面板即可自愈。
        dwell_cache: dict[int, set[int]] = {}
        for row in rows:
            c = self._decode("strategy_characters", row)
            char_by_id[int(c["id"])] = c
            status = str(c.get("status") or "")
            loc_node = R.char_node_id(c)
            if loc_node <= 0 or status not in ("idle", "task", "research"):
                continue
            node = nodes.get(loc_node)
            fid = int(c.get("faction_id") or 0)
            ids = await self._friendly_ids(
                int(world["id"]), fid, dwell_cache) if fid > 0 else set()
            if node and str(node.get("type") or "") in R.CHAR_DWELL_NODE_TYPES \
                    and int(node.get("owner_faction") or 0) in ids:
                continue
            dest = await self._char_fallback_location(
                int(world["id"]), c, loc_node)
            c["location"] = {"node_id": int(dest)} if dest else {}
            await self.save_char(c)
        for c in char_by_id.values():
            per = int(icfg.get("recover_per_tick", 5))
            loc_node = int((c.get("location") or {}).get("node_id", 0) or 0)
            if loc_node in infirmary_nodes:
                per = int(icfg.get("infirmary_recover", 10)) + \
                    5 * (int(infirmary_nodes[loc_node]) - 1)
            if c["status"] == "idle":
                R.recover_injury(c, per)
            elif c["status"] == "captive":
                # v4.3.0：俘虏伤势半速恢复——此前 status='captive' 零恢复，
                # 长期关押的俘虏永远带伤（劝降/赎金价值受损，获释即废人）
                R.recover_injury(c, max(1, per // 2))
            R.recover_stamina(c, int(scfg.get("recover_per_tick", 10))
                              if scfg.get("enabled", True) else 0)
            await self.save_char(c)
        # 驻守角色按据点分组（民心魅力加成 / 守军驻守加成共用，避免重复查库）
        # §5：只有「空闲 / 本地任务」算驻守；侦察/劫掠/运粮（location 记
        # 出发地）与计略任务虽在同一据点，但人在外勤，不提供民心/守军/
        # 瘟疫联动加成。
        task_types: dict[int, str] = {}
        for trow in await self._fetchall(
                "SELECT character_id, task_type FROM strategy_tasks "
                "WHERE world_id=? AND status='active'", (world["id"],)):
            task_types[int(trow["character_id"])] = str(trow["task_type"])

        def _is_resident(c: dict) -> bool:
            if not R.is_resident_char(c):
                return False
            if str(c.get("status") or "") == "task":
                return task_types.get(int(c["id"]), "") in R.TASK_LOCAL_TYPES
            return True

        residents: dict[int, list[dict]] = {}
        for c in char_by_id.values():
            if not _is_resident(c):
                continue
            loc = R.char_node_id(c)
            if loc > 0:
                residents.setdefault(loc, []).append(c)
        # 据点级太守：校验角色仍存活且属于该据点所属势力/联盟；失效则清除。
        taishou_nodes: set[int] = set()
        ally_cache: dict[int, set[int]] = {}
        for nid, node in nodes.items():
            gid = int(node.get("governor_char_id") or 0)
            if gid <= 0:
                continue
            c = char_by_id.get(gid)
            if not c or c.get("status") in ("dead", "captive", "transfer", "wander") \
                    or R.injury_state(c.get("injury", 0)) == "dead":
                node["governor_char_id"] = 0
                continue
            owner_fid = int(node.get("owner_faction") or 0)
            if owner_fid <= 0:
                node["governor_char_id"] = 0
                continue
            if owner_fid not in ally_cache:
                owner_f = await self.get_faction(owner_fid)
                ally_cache[owner_fid] = await self._alliance_ids(
                    int(world["id"]), owner_f) if owner_f else {owner_fid}
            if int(c.get("faction_id") or 0) not in ally_cache[owner_fid]:
                node["governor_char_id"] = 0
                continue
            # §6：太守加成只在“驻守本城”时生效——出征/在外任务/被俘/押运
            # 期间暂停加成（不清除任命，回城后自动恢复）。
            if not (R.is_governor_resident(node, c) and _is_resident(c)):
                continue
            taishou_nodes.add(nid)
        # 据点归属势力/联盟缓存（判断 NPC 与驻守角色归属，避免逐节点查库）
        fcfg_by_owner: dict[int, dict] = {}
        tech_by_owner: dict[int, dict] = {}
        for node in nodes.values():
            owner_fid = int(node.get("owner_faction") or 0)
            if owner_fid <= 0 or owner_fid in fcfg_by_owner:
                continue
            owner_f = await self.get_faction(owner_fid)
            fcfg_by_owner[owner_fid] = owner_f or {}
            if owner_fid not in ally_cache:
                ally_cache[owner_fid] = await self._alliance_ids(
                    int(world["id"]), owner_f) if owner_f else {owner_fid}
        # 据点：城防自修 + 士气回归 + 守军增长 + 瘟疫类 DOT
        active = await self.get_active_events(world["id"], world.get("tick_no", 0))
        gcfg = self.main_cfg().get("governance") or {}
        garrison_cfg = self.main_cfg().get("garrison") or {}
        grow_pct = float(garrison_cfg.get("grow_pct", 0.02) or 0.0)
        tick_now = int(world.get("tick_no", 0) or 0)
        # 只保留本 tick 标记：既保证同 tick 去重，又避免 world.data 膨胀
        riot_map = {k: int(v) for k, v in
                    ((world.get("data") or {}).get("riot_tick_nodes") or {}).items()
                    if int(v or 0) == tick_now}
        for nid, node in nodes.items():
            if node["owner_faction"] != 0 and not node.get("siege_state"):
                owner_fid = int(node["owner_faction"])
                # 城防自修（§10.2：升级后 mine/grain/market 亦有城防）
                if node["defense"] > 0:
                    node["defense"] = R.city_self_repair(node["defense"], node["level"])
                ally_ids = ally_cache.get(owner_fid) or {owner_fid}
                guards = [c for c in residents.get(nid, [])
                          if int(c.get("faction_id") or 0) in ally_ids]
                is_taishou = nid in taishou_nodes and \
                    self._salary_governor_paid(fcfg_by_owner.get(owner_fid), nid)
                # v4.3.14 经营科技（征募/仁政）：按据点归属势力读科技
                teff = tech_by_owner.get(owner_fid)
                if teff is None:
                    teff = R.tech_effects(
                        (fcfg_by_owner.get(owner_fid) or {}).get("tech") or {},
                        tcfg=self.main_cfg().get("tech") or {})
                    tech_by_owner[owner_fid] = teff
                # §10.4 守军自然增长（玩家/NPC 统一）：基础 +2% 上限/tick；
                # NPC 再乘民心比例，玩家按驻守角色追加（武将/统率差异化，单城封顶 5%）。
                # 用本 tick 起始民心计算（民心恢复在其后），避免同一 tick 双重加速。
                gar = node.get("npc_garrison") or {}
                cap = R.garrison_cap(int(node.get("level", 1) or 1))
                troops = int(gar.get("troops", 0) or 0)
                if cap > 0 and troops < cap:
                    grow = R.garrison_grow(cap, grow_pct)
                    kind = str((fcfg_by_owner.get(owner_fid) or {}).get("kind") or "")
                    if kind == "npc":
                        mult = max(0.0, min(1.0, int(node.get("morale", 0) or 0) / 100.0))
                        grow = int(grow * mult)
                    else:
                        grow += int(cap * R.garrison_char_growth_pct(
                            guards, garrison_cfg))
                        # 经营 L6 征募：驻守守军增长 +0.5%/tick
                        grow += int(cap * float(
                            teff.get("garrison_grow_bonus", 0.0) or 0.0))
                    if grow > 0:
                        gar["troops"] = min(cap, troops + grow)
                        node["npc_garrison"] = gar
                # 民心自然恢复（§5/§6）：基础 1（太守驻守 → 2）+ 擂将台每级 1
                # + 魅力驻守加成 + 太守驻守固定 1；仍保持“<80 才恢复、≥80 回落”。
                step = 2 if is_taishou else 1
                step += min(5, self._building_level(node, "arena"))
                step += R.morale_garrison_bonus(guards)
                if is_taishou:
                    step += 1
                # 经营 L7 仁政：驻守据点民心恢复 +1/tick
                step += int(float(teff.get("morale_recover_bonus", 0.0) or 0.0))
                morale = int(node.get("morale", 0) or 0)
                if morale < 80:
                    node["morale"] = max(0, min(self._morale_cap(node),
                                                morale + step))
                else:
                    node["morale"] = max(80, min(self._morale_cap(node),
                                                 morale - 1))
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
                        # 只认据点归属方/盟友的驻守角色：滞留敌城的外来
                        # 角色不能替敌方提供瘟疫减伤/名医免疫。
                        owner_fid_ev = int(node.get("owner_faction") or 0)
                        if owner_fid_ev > 0:
                            ally_ids_ev = ally_cache.get(owner_fid_ev) or {owner_fid_ev}
                            guards = [gc for gc in residents.get(nid, [])
                                      if int(gc.get("faction_id") or 0) in ally_ids_ev]
                        else:
                            guards = residents.get(nid, [])
                        # §9 瘟疫联动：doctor 驻守 → 完全免疫；其余驻守角色
                        # （≥1 人）→ 负向效果（掉兵/掉民心）减半
                        if any("doctor" in (gc.get("traits") or [])
                               for gc in guards):
                            continue
                        scale = 0.5 if guards else 1.0
                        gar = node.get("npc_garrison") or {}
                        pct = float(eff.get("troops_pct", 0)) / 100 * scale
                        gar["troops"] = max(0, int(gar.get("troops", 0) * (1 + pct)))
                        node["npc_garrison"] = gar
                        morale_delta = int(eff.get("morale", 0))
                        if morale_delta < 0:
                            morale_delta = int(morale_delta * scale)
                        node["morale"] = max(0, node["morale"] + morale_delta)
            # 己方军团驻守士气恢复
            await self.save_node(node)
        for a in await self.get_armies(world["id"], states=("idle",)):
            node = nodes.get(a["node_id"])
            if node and node["owner_faction"] == a["faction_id"]:
                a["morale"] = min(R.army_morale_cap(a), a["morale"] + 10)
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
        """哗变时随机一名本城驻守角色受伤（§8.6）。

        此前只按 ``faction_id + status='idle'`` 抽人，会把同一势力但远在
        其他据点（甚至出征在外）的角色也判伤；哗变只应影响本城驻守者。
        """
        rows = await self._fetchall(
            "SELECT * FROM strategy_characters WHERE world_id=? AND faction_id=? "
            "AND status='idle'",
            (world["id"], int(node.get("owner_faction", 0) or 0)))
        rows = [r for r in rows
                if int((_loads(r.get("location_json"), {}) or {})
                       .get("node_id", 0) or 0) == int(node["id"])]
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
        host_cache: dict[int, int] = {}
        for a in marching:
            if a["path"]:
                continue
            fid = int(a.get("faction_id") or 0)
            if fid not in host_cache:
                f = await self.get_faction(fid) if fid > 0 else None
                host = await self._alliance_host(f) if f else None
                host_cache[fid] = int(host["id"]) if host else fid
            # 协战：同一联盟的军团同 tick 抵达同一目标时合并为一个攻方编组。
            arrived.setdefault((a["node_id"], host_cache[fid]), []).append(a)
        for (nid, _group_fid), attackers in arrived.items():
            node = await self.get_node(world["id"], nid)
            if not node:
                for a in attackers:
                    a["state"] = "idle"
                    await self.save_army(a)
                continue
            friendly = await self._friendly_ids(
                int(world["id"]), int(attackers[0].get("faction_id") or 0))
            if int(node.get("owner_faction") or 0) in friendly:
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
        # v4.8.0：抵达后同步联军状态（统一行军 → 待命/合围）
        await self._ensure_coalition_consistency(world)
        await self._commit()
        return reports

    async def _should_open_field_encounter(self, world: dict,
                                           attackers: list[dict],
                                           defenders: list[dict]) -> bool:
        """普通行军抵达的野战是否开手操窗口（§7.1/§7.3 野战遭遇）。

        仅在有玩家军团参与、且该玩家最近在线时暂停自动结算：
        - mode=auto  ：保持旧行为，tick 内自动模拟；
        - mode=pvp   ：攻守双方都有玩家军团才开；
        - mode=human ：任一方有玩家军团且最近在线（默认）。
        ``field_encounter_active_window`` 默认 1800s，避免 NPC 偷袭离线玩家
        时把节点和军团白白锁在 5 分钟手操窗口里。
        """
        hex_cfg = self.main_cfg().get("hex") or {}
        mode = str(hex_cfg.get("field_encounter_mode", "human") or "human").lower()
        if mode not in ("pvp", "human"):
            return False
        atk_owners = {str(a.get("owner_user_id") or "") for a in attackers}
        dfn_owners = {str(a.get("owner_user_id") or "") for a in defenders}
        atk_owners.discard("")
        dfn_owners.discard("")
        if mode == "pvp":
            if not atk_owners or not dfn_owners:
                return False
        elif not (atk_owners or dfn_owners):
            return False
        window = int(hex_cfg.get("field_encounter_active_window", 1800) or 0)
        if window <= 0:
            return True
        now = _now()
        for uid in atk_owners | dfn_owners:
            last = int(self._recent_players.get(uid, 0) or 0)
            if last and now - last <= window:
                return True
        return False

    async def _attack_node(self, world: dict, node: dict, attackers: list[dict]) -> str:
        # 协战归属：按参战总兵力选出主攻势力，城池/战利品归主攻方。
        troops_by_fid: dict[int, int] = {}
        for a in attackers:
            fid = int(a.get("faction_id") or 0)
            troops_by_fid[fid] = troops_by_fid.get(fid, 0) + \
                max(0, int(a.get("troops", 0) or 0))
        atk_fid = max(troops_by_fid, key=troops_by_fid.get) \
            if troops_by_fid else int(attackers[0]["faction_id"])
        atk_fids = {int(a.get("faction_id") or 0) for a in attackers}
        atk_f = await self.get_faction(int(atk_fid))
        dfn_f = await self.get_faction(int(node.get("owner_faction", 0)))
        # 已灭亡势力（外交层/崩溃清理）的在途军团不得继续占城：否则会留下
        # alive=0 的幽灵势力拥有节点，且永不进入后续 NPC 轮转。
        if atk_f is not None and "alive" in atk_f and \
                int(atk_f.get("alive") or 0) <= 0:
            for a in attackers:
                await self._auto_retreat(a)
            return ""
        atk_host = await self._alliance_host(atk_f) if atk_f else None
        dfn_host = await self._alliance_host(dfn_f) if dfn_f else None
        if atk_host and dfn_host and int(atk_host["id"]) == int(dfn_host["id"]):
            for a in attackers:
                a["state"] = "idle"
                a["edge_pos"] = 0.0
                await self.save_army(a)
            return ""
        # 保护期复查：行军途中目标可能刚被他人夺取并获得 24h 免战，
        # 抵达时须重新判定，否则保护期形同虚设
        if int(node.get("owner_faction", 0)) > 0 and \
                int(node.get("protect_until", 0) or 0) > _now():
            for a in attackers:
                await self._auto_retreat(a)
            return f"🛡️「{node['name']}」处于保护期，攻方撤军"
        # v4.8.2：围城归属保护——已有势力围城时，其他势力不能直接抢归属。
        #   友方 / 与守方开战 → 增援，破城方仍是原攻方；
        #   与攻方敌对 → 先打城下营地战，原攻方被消灭后才可能易主。
        ss = node.get("siege_state") or {}
        siege_owner = int(ss.get("attacker_faction") or 0)
        if siege_owner > 0 and siege_owner != atk_fid and \
                await self._siege_owner_alive(world, node, siege_owner):
            owner_ids = set(await self._friendly_ids(
                int(world["id"]), siege_owner))
            camp = [a for a in await self.get_armies(
                world["id"], node_id=int(node["id"]), states=("siege",))
                if int(a.get("faction_id") or 0) in owner_ids
                and int(a.get("troops", 0) or 0) > 0]
            defender_fid = int(node.get("owner_faction", 0) or 0)
            if await self._factions_at_war(atk_fid, siege_owner):
                camp_report = await self._siege_camp_battle(
                    world, node, attackers, camp, atk_fid, siege_owner)
                if await self._siege_owner_alive(world, node, siege_owner):
                    # 原围城方未被消灭：围城归属不变，挑战方按战败撤退
                    return camp_report
                node["siege_state"] = {}
                await self.save_node(node)
                survivors = [a for a in attackers
                             if int(a.get("troops", 0) or 0) > 0
                             and not a.get("_destroyed")
                             and not a.get("_routed")]
                if not survivors:
                    return camp_report + "\n🏳 原围城方被击溃，攻方亦无力再战"
                attackers = survivors
                atk_fids = {int(a.get("faction_id") or 0) for a in attackers}
                atk_fid = max(
                    atk_fids,
                    key=lambda f: sum(int(x.get("troops", 0) or 0)
                                      for x in attackers
                                      if int(x.get("faction_id") or 0) == f))
                atk_f = await self.get_faction(int(atk_fid))
                atk_host = await self._alliance_host(atk_f) if atk_f else None
                await self._log_action(
                    world, "battle", "siege_takeover",
                    actor_faction=int(atk_fid),
                    target_faction=int(siege_owner),
                    node_id=int(node["id"]),
                    node_name=str(node.get("name") or ""), success=1,
                    summary=(f"🏳「{node['name']}」城下营地战："
                             f"{await self._faction_name(int(atk_fid))} "
                             "击溃原围城方，围城归属空出"),
                    visibility="public")
            else:
                reinforce = (atk_fid in owner_ids or
                             await self._factions_at_war(atk_fid, defender_fid))
                if not reinforce:
                    for a in attackers:
                        a["state"] = "idle"
                        a["path"] = []
                        a["edge_pos"] = 0.0
                        await self.save_army(a)
                    return (f"⛔「{node['name']}」已由"
                            f"{await self._faction_name(siege_owner)}围城；"
                            "你既非其友方，也未与该城守方开战，无法加入围城")
                # 增援：破城方仍是原围城方，城防进度延续
                atk_fid = siege_owner
                atk_f = await self.get_faction(int(atk_fid))
                atk_host = await self._alliance_host(atk_f) if atk_f else None
                atk_fids = set(atk_fids) | owner_ids
                attackers = list(attackers) + camp
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
            if int(a["faction_id"]) in atk_fids:
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
                    await self._begin_siege(world, node, a, atk_fid)
                return f"⛺「{node['name']}」无兵驻守但城防尚存，攻方转入围城"
            await self._capture_node(world, node, atk_fid)
            note = node.get("_capture_note") or ""
            return (f"🚩「{node['name']}」被 {await self._faction_name(atk_fid)} 占领"
                    + (f"\n{note}" if note else ""))
        # 会战
        atk_sim, dfn_sim, gar_sim = [], [], None
        node_taishou = False
        gov = await self._valid_node_governor(node)
        if gov and owner_fid > 0:
            owner_f = await self.get_faction(owner_fid)
            ally_ids = await self._alliance_ids(world["id"], owner_f) \
                if owner_f else {owner_fid}
            # §6：城防 +10% 仅在太守“驻守本城”时生效（出征/在外任务/被俘
            # 期间暂停；_valid_node_governor 仍负责展示/清理口径）
            node_taishou = (int(gov.get("faction_id") or 0) in ally_ids
                            and await self._governor_onsite(node, gov)
                            and self._salary_governor_paid(owner_f, node["id"]))
        for a in attackers:
            s = await self._army_sim(a)
            if not s:
                continue
            # §8.6 动员令：按各协战势力分别叠攻方攻击加成
            atk_buff = await self._faction_buff(
                int(a.get("faction_id") or 0), "attack_bonus")
            if atk_buff:
                s["pos_atk_mult"] = float(
                    s.get("pos_atk_mult", 1.0)) * (1 + atk_buff)
            atk_sim.append(s)
        # §8.6 动员令 + 相邻友方领土：守方城防/本土作战加成
        dfn_buff = await self._faction_buff(
            int(node.get("owner_faction", 0) or 0), "defense_bonus")
        terr = await self._territory_bonus(world, node, owner_fid)
        for a in defenders:
            s = await self._army_sim(a)
            if s:
                s["wall_mult"] = 1.0 + min(0.3, node["defense"] / R.city_defense_max(
                    max(1, node["level"])) * 0.3)
                s["wall_mult"] *= (1 + dfn_buff) * float(terr["defense_mult"])
                if node_taishou:
                    s["wall_mult"] *= 1.10  # 太守辖地城防 +10%
                s["territory_atk_mult"] = float(terr["combat_mult"])
                s["territory_def_mult"] = float(terr["combat_mult"])
                dfn_sim.append(s)
        if garrison_troops > 0:
            gar_sim = self._garrison_sim(node)
            if unowned:
                gar_sim["def_terrain_mult"] = 1.1  # 残军据险
            if node_taishou:
                gar_sim["wall_mult"] = 1.10
            gar_sim["wall_mult"] = float(gar_sim.get("wall_mult", 1.0)) * \
                float(terr["defense_mult"])
            gar_sim["territory_atk_mult"] = float(terr["combat_mult"])
            gar_sim["territory_def_mult"] = float(terr["combat_mult"])
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
                await self._begin_siege(world, node, a, atk_fid)
            return ""
        # v4.3.13：自动会战按据点地形套用科技地形加成
        # （手操 hex 由 strategy_hex._sim_view 按逐格地形实时结算）
        await self._apply_tech_terrain(
            atk_sim + dfn_sim, self._auto_battle_terrain(node))
        # §7.1 野战遭遇：有玩家军团且最近在线时，改为手操会战而不是 tick 内
        # 直接模拟。这样玩家会收到 battle_open/回合通知，可部署/操作或托管；
        # 超时由 poll 的全托管模拟兜底，不会卡死。
        if await self._should_open_field_encounter(world, attackers, defenders):
            hex_cfg = self.main_cfg().get("hex") or {}
            prep = int(hex_cfg.get("field_encounter_prep_seconds",
                                   hex_cfg.get("field_prep_seconds", 300)))
            battle = await self._create_manual_battle(
                world, node, atk_sim, dfn_sim, attackers, defenders,
                mode="field", kind="field_encounter",
                prep_seconds=max(30, prep), wall_hp=0, initiator="",
                attacker_faction=atk_fid)
            return (f"⚔️「{node['name']}」野战遭遇，已开启手操会战！"
                    f"{max(0, int(battle['deadline_at']) - _now())} 秒内完成部署，"
                    f"超时自动结算")
        cfg = self.main_cfg()
        rng = self._rng("battle", world["id"], node["id"], world["tick_no"],
                        *[a["id"] for a in attackers])
        result = R.simulate_battle(
            atk_sim, dfn_sim, rng,
            max_rounds=int((cfg.get("battle") or {}).get("max_rounds", 30)),
            night=R.is_night(time.localtime().tm_hour),
            defending_node=True, city_defense=node["defense"])
        report = R.render_battle_report(result, title=f"{node['name']}会战")
        # 先写战果再归档战报：斩魔声望文案须在 _persist_battle 之前拼入，
        # 否则只会出现在即时返回串、归档战报缺失该行。_apply_battle_aftermath
        # 不改变据点归属，故先结算善后不影响 dfn_fid 与攻守快照。
        await self._apply_battle_aftermath(world, node, attackers, defenders, result)
        if result["winner"] == "attacker" and atk_fid > 0:
            # §6.3 斩魔声望：击败魔潮军团按势力声望配置加值
            slain = sum(1 for d in defenders if d and
                        int(d.get("faction_id", 0)) == -1 and
                        (d.get("_destroyed") or d.get("_routed")))
            if slain:
                gain = int((cfg.get("reputation") or {}).get(
                    "demon_slay_gain", 10)) * slain
                await self.add_reputation(atk_f, gain)
                report += f"\n🌑 击退魔潮军团 ×{slain}，声望 +{gain}"
        await self._persist_battle(world, node, atk_fid, node["owner_faction"],
                                   "field", result, report, atk_sim, dfn_sim)
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
                # 下 tick 再次触发会战（重复攻打同一座城）。协战统一以
                # 主攻势力登记围城归属，避免后到盟友改写攻方。
                for s in survivors:
                    await self._begin_siege(world, node, s, atk_fid)
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

    async def _siege_camp_battle(self, world: dict, node: dict,
                                 attackers: list[dict], camp: list[dict],
                                 atk_fid: int, owner_fid: int) -> str:
        """与围城方敌对：在城下先打一场营地战（决定围城归属是否易主）。

        仅当原攻方营地军被全歼/溃散后，调用方才会清空 ``siege_state``
        并让挑战方继续攻守。
        """
        atk_sim, dfn_sim = [], []
        for a in attackers:
            s = await self._army_sim(a)
            if s:
                atk_sim.append(s)
        for a in camp:
            s = await self._army_sim(a)
            if s:
                dfn_sim.append(s)
        if not atk_sim or not dfn_sim:
            return ""
        cfg = self.main_cfg()
        rng = self._rng("siege_camp", world["id"], node["id"],
                        world.get("tick_no", 0),
                        *[int(a["id"]) for a in attackers])
        result = R.simulate_battle(
            atk_sim, dfn_sim, rng,
            max_rounds=int((cfg.get("battle") or {}).get("max_rounds", 30)),
            night=R.is_night(time.localtime().tm_hour),
            defending_node=False)
        report = R.render_battle_report(result, title=f"{node['name']}城下营地战")
        await self._apply_battle_aftermath(world, node, attackers, camp, result)
        await self._persist_battle(world, node, atk_fid, owner_fid,
                                   "field", result, report, atk_sim, dfn_sim)
        await self._log_action(
            world, "battle", "siege_camp",
            actor_faction=int(atk_fid),
            target_faction=int(owner_fid),
            node_id=int(node["id"]),
            node_name=str(node.get("name") or ""),
            success=1 if result.get("winner") == "attacker" else 0,
            summary=(f"⚔️「{node['name']}」围城权争夺："
                     f"{await self._faction_name(int(atk_fid))} vs "
                     f"{await self._faction_name(int(owner_fid))}，"
                     f"{'挑战方' if result.get('winner') == 'attacker' else '原围城方'}胜"),
            visibility="public")
        return report

    async def _begin_siege(self, world: dict, node: dict, army: dict,
                           attacker_faction: int = 0):
        army["state"] = "siege"
        army["edge_pos"] = 0.0
        army["path"] = []
        ss = node.get("siege_state") or {}
        old_fid = int(ss.get("attacker_faction") or 0)
        new_fid = int(attacker_faction or army.get("faction_id") or 0)
        # v4.8.2：围城归属只在原攻方被消灭后才可变。
        # 友方增援 / 原攻方仍有驻军 → 沿用原攻方，围城进度不重置。
        if ss and old_fid != new_fid:
            friendly = await self._friendly_ids(int(world["id"]), old_fid)
            if new_fid in friendly or await self._siege_owner_alive(
                    world, node, old_fid):
                new_fid = old_fid
            else:
                ss = {}
        fresh = not ss
        if fresh:
            ss = {"ticks": 0, "attacker_faction": new_fid,
                  "choice": "auto", "since": _now()}
        node["siege_state"] = ss
        await self.save_army(army)
        await self.save_node(node)
        if fresh and int(node.get("owner_faction", 0)) > 0:
            # 己方被攻通知（§12.5 siege_alert，中档及以上）
            rows = await self._fetchall(
                "SELECT user_id FROM strategy_players "
                "WHERE world_id=? AND faction_id=?",
                (world["id"], int(node.get("owner_faction"))))
            await self._notify(world, [r["user_id"] for r in rows], "siege_alert",
                               {"node_id": int(node["id"]), "node_name": node["name"],
                                "attacker_faction": int(ss["attacker_faction"])})

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
        tech = R.tech_effects((f or {}).get("tech") or {},
                              tcfg=self.main_cfg().get("tech") or {})
        troops = int(army["troops"])
        big_thr = int(tech.get("big_army_threshold", 2000) or 2000)
        small_thr = int(tech.get("small_army_threshold", 800) or 800)
        big_atk = float(tech.get("big_army_atk_bonus", 0.0) or 0.0) \
            if troops >= big_thr else 0.0
        sim = {
            "id": int(army["id"]), "faction_id": int(army.get("faction_id", 0)),
            "troop_type": army.get("troop_type", "sword_shield"),
            "troops": troops, "morale": int(army.get("morale", 100)),
            "veteran": int(army.get("veteran", 0)), "leader": leader,
            "siege_engine": bool(army.get("siege_engine", 0)),
            "pos_atk_mult": 1.0 + float(pos_bonus.get("army_atk", 0.0))
            + float(tech.get("atk_bonus", 0.0))
            + float(tech.get("flank_bonus", 0.0)) + big_atk,
        }
        # v4.3.13 军事 L4 精锐小队：带兵 ≤ 阈值时机动 +1、先手 +N
        if troops <= small_thr:
            sim["move_bonus"] = int(tech.get("small_army_move_bonus", 0) or 0)
            sim["init_bonus"] = float(tech.get("small_army_init_bonus", 0.0) or 0.0)
        return sim

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

    @staticmethod
    def _auto_battle_terrain(node: dict) -> str:
        """自动会战无逐格地形：按据点大地图地形映射到 hex 地形语义词。"""
        return {"plain": "plain", "forest": "forest", "mountain": "highland",
                "river": "river", "swamp": "swamp"}.get(
                    str((node or {}).get("terrain") or "plain"), "plain")

    async def _apply_tech_terrain(self, sims: list, terrain: str) -> None:
        """自动会战：按战斗语境地形套用势力科技的地形攻/防加成。

        手操 hex 战役由 ``strategy_hex._sim_view`` 按逐格地形实时结算；
        自动会战没有逐格地形，这里用语境地形（野战=据点地形、攻城=wall）近似。
        """
        tcfg = self.main_cfg().get("tech") or {}
        cache: dict[int, dict] = {}
        for s in sims:
            if not isinstance(s, dict):
                continue
            fid = int(s.get("faction_id") or 0)
            if fid not in cache:
                f = await self.get_faction(fid) if fid > 0 else None
                cache[fid] = R.tech_effects((f or {}).get("tech") or {}, tcfg=tcfg)
            teff = cache[fid]
            atk = float((teff.get("terrain_atk_mult") or {}).get(terrain, 0.0))
            dfn = float((teff.get("terrain_def_mult") or {}).get(terrain, 0.0))
            if atk:
                s["atk_terrain_mult"] = float(
                    s.get("atk_terrain_mult", 1.0) or 1.0) * (1.0 + atk)
            if dfn:
                s["def_terrain_mult"] = float(
                    s.get("def_terrain_mult", 1.0) or 1.0) * (1.0 + dfn)

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
        data = world.get("data") or {}
        data["last_battle_tick"] = int(world.get("tick_no", 0) or 0)
        world["data"] = data
        await self.insert_battle({
            "world_id": world["id"], "node_id": node["id"],
            "attacker_faction": atk_fid, "defender_faction": dfn_fid,
            "kind": kind, "mode": "auto", "state": "done",
            "round_no": int(result.get("rounds", 0)),
            "map_seed": f"{world['tick_no']}-{node['id']}",
            "armies_snapshot": snapshot, "result": slim,
            "report_text": report[:8000], "created_at": _now(),
        })
        # v4.7.0：战斗结果写入江湖纪事
        try:
            af = await self.get_faction(int(atk_fid)) if int(atk_fid) > 0 else None
            df = await self.get_faction(int(dfn_fid)) if int(dfn_fid) > 0 else None
            atk_name = str((af or {}).get("name") or
                           ("魔潮" if int(atk_fid) == -1 else "无主"))
            dfn_name = str((df or {}).get("name") or
                           ("魔潮" if int(dfn_fid) == -1 else "无主"))
            winner = str(result.get("winner") or "")
            won_atk = winner == "attacker"

            def _loss(side: str) -> int:
                total = 0
                for row in result.get(side) or []:
                    before = int(row.get("troops_before",
                                         row.get("troops", 0)) or 0)
                    after = int(row.get("troops", 0) or 0)
                    total += max(0, before - after)
                return total

            kind_cn = "野战" if str(kind) == "field" else "会战"
            await self._log_action(
                world, "battle", str(kind),
                actor_faction=int(atk_fid), actor_name=atk_name,
                target_faction=int(dfn_fid), target_name=dfn_name,
                node_id=int(node["id"]), node_name=str(node.get("name") or ""),
                success=1 if won_atk else 0,
                summary=(f"⚔️「{node.get('name', '')}」{kind_cn}："
                         f"{atk_name} vs {dfn_name}，"
                         f"{atk_name if won_atk else dfn_name}胜"
                         f"（损失 {_loss('attackers')} / {_loss('defenders')}）"),
                detail={"winner": winner,
                        "rounds": int(result.get("rounds", 0) or 0),
                        "attacker_losses": _loss("attackers"),
                        "defender_losses": _loss("defenders"),
                        "highlights": result.get("highlights") or []},
                visibility="public")
        except Exception:  # noqa: BLE001 - 纪事失败不影响战报
            logger.debug("[strategy] 战斗纪事写入失败", exc_info=True)

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
            army["veteran"] = min(R.VETERAN_MAX, int(army.get("veteran", 0))
                                  + R.veteran_gain(
                winner == ("attacker" if is_attacker else "defender")))
            army["morale"] = min(R.army_morale_cap(army),
                                 int(row["morale"]) + mates_bonus)
            # v4.3.14 军事 L6 军心：本方获胜时全军士气 +N（击破敌军）
            if winner == ("attacker" if is_attacker else "defender"):
                wf = await self.get_faction(int(army.get("faction_id") or 0))
                army["morale"] = min(R.army_morale_cap(army), army["morale"] + int(
                    R.tech_effects((wf or {}).get("tech") or {},
                                   tcfg=self.main_cfg().get("tech") or {}
                                   ).get("kill_morale_bonus", 0) or 0))
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
                self._remember_veteran_ashes(
                    world, army.get("owner_user_id", ""),
                    int(army.get("veteran", 0) or 0))
                for cid in cids:
                    c = await self.get_char(int(cid))
                    if not c:
                        continue
                    dead = R.apply_injury(c, 40)
                    c["status"] = "dead" if dead else "idle"
                    # §4.4：覆没于敌方/无主据点时不得以 idle 状态留在原地
                    dest = await self._char_fallback_location(
                        int(world["id"]), c, int(node["id"]))
                    c["location"] = {"node_id": int(dest)} if dest else {}
                    await self.save_char(c)
                # §10.7：NPC 军团野战覆没时残粮退回国库（同围城覆没口径）
                await self._refund_npc_army_grain(army)
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
            # §10.4：取消 NPC 守军重生倒计时——被清空后由 _step_recovery
            # 的 +2% 上限/tick 自然恢复（旧存档 respawn_at 字段不再生效）。
            if int(node.get("respawn_at", 0) or 0):
                node["respawn_at"] = 0
            await self.save_node(node)
        await self._commit()

    # ------------------------------------------------------------------
    # NPC 财库 / 性格小工具（§10.6 / §10.7）
    # 仅服务 _step_npc_ai / _step_npc_develop / _step_npc_economy 及坊市改造
    # ------------------------------------------------------------------
    def _npc_economy_cfg(self) -> dict:
        """``npc.economy`` 配置块（缺省值兜底，兼容旧配置/旧存档）。"""
        ecfg = (self.main_cfg().get("npc") or {}).get("economy") or {}
        reserve = ecfg.get("treasury_reserve")
        if not isinstance(reserve, dict):
            reserve = {}
        sell_items = ecfg.get("npc_sell_items")
        offer_items = ecfg.get("npc_offer_items")
        return {
            "mobil_coin_per_100": float(ecfg.get("mobil_coin_per_100", 500)),
            "mobil_grain_per_100": float(ecfg.get("mobil_grain_per_100", 100)),
            "carry_grain_per_100": float(ecfg.get("carry_grain_per_100", 100)),
            "mobilization_cost_mult": float(
                ecfg.get("mobilization_cost_mult", 1.0)),
            "treasury_reserve": {
                "coin": int(reserve.get("coin", 5000)),
                "grain": int(reserve.get("grain", 2000)),
                "iron": int(reserve.get("iron", 500)),
            },
            "mobil_morale_per_100": float(ecfg.get("mobil_morale_per_100", 2)),
            "mobilize_morale_min": int(ecfg.get("mobilize_morale_min", 40)),
            "surplus_sell_pct": float(ecfg.get("surplus_sell_pct", 0.5)),
            "offer_reserve_pct": float(ecfg.get("offer_reserve_pct", 0.3)),
            "relief_enabled": bool(ecfg.get("relief_enabled", True)),
            "npc_sell_items": list(sell_items if isinstance(sell_items, list)
                                   else ["grain", "iron"]),
            "npc_offer_items": list(offer_items if isinstance(offer_items, list)
                                    else ["grain", "iron"]),
        }

    @staticmethod
    def _npc_personality_mult(f: dict, key: str, default: float = 1.0) -> float:
        """读取势力性格乘数（缺省 1.0；非法/非正值按缺省处理）。"""
        pers = (f or {}).get("personality") or {}
        try:
            val = float(pers.get(key, default))
        except (TypeError, ValueError):
            return float(default)
        return val if val > 0 else float(default)

    def _npc_treasury(self, f: dict) -> dict:
        """势力国库规范化副本（兼容旧存档缺键，不修改原 dict）。"""
        t = (f or {}).get("treasury") or {}
        return {
            "coin": max(0, int(t.get("coin", 0) or 0)),
            "grain": max(0, int(t.get("grain", 0) or 0)),
            "iron": max(0, int(t.get("iron", 0) or 0)),
            "gold": max(0, int(t.get("gold", 0) or 0)),
            "reserved_coin": max(0, int(t.get("reserved_coin", 0) or 0)),
        }

    def _npc_reserve(self, f: dict) -> dict:
        """势力守财底线 = ``treasury_reserve`` × 性格动员底线乘数（§10.7）。"""
        base = self._npc_economy_cfg()["treasury_reserve"]
        mult = self._npc_personality_mult(f, "mobil_reserve_mult")
        return {k: int(round(v * mult)) for k, v in base.items()}

    def _treasury_add(self, f: dict, **kw) -> dict:
        """国库入账（就地修改 ``f``，调用方负责 save_faction）。

        支持任意 treasury 键（coin/grain/iron/gold/reserved_coin），负值按 0 截断。
        """
        t = dict((f or {}).get("treasury") or {})
        for key, amount in kw.items():
            t[key] = max(0, int(t.get(key, 0) or 0) + int(amount or 0))
        f["treasury"] = t
        return t

    def _treasury_spend(self, f: dict, coin: int = 0, grain: int = 0,
                        iron: int = 0,
                        floor: Optional[dict] = None) -> bool:
        """国库扣费：余额不足或扣后低于守财底线返回 False（不改动国库）。

        ``floor=None`` 表示不设底线（赈济例外，§10.8）。
        """
        t = self._npc_treasury(f)
        costs = {"coin": max(0, int(coin or 0)),
                 "grain": max(0, int(grain or 0)),
                 "iron": max(0, int(iron or 0))}
        limit = floor or {}
        # 坊市收购冻结的铜钱不可被发展/动员/挂单等支出挪用
        frozen = int(t.get("reserved_coin", 0) or 0)
        for key, amount in costs.items():
            if amount <= 0:
                continue
            need = int(limit.get(key, 0) or 0)
            if key == "coin":
                need += frozen
            if t[key] - amount < need:
                return False
        for key, amount in costs.items():
            if amount > 0:
                t[key] -= amount
        f["treasury"] = t
        return True

    # ------------------------------------------------------------------
    # 步骤 6：NPC AI（§9.3）
    # ------------------------------------------------------------------
    async def _step_npc_ai(self, world: dict):
        cfg = self.main_cfg().get("npc") or {}
        fcfg = self._favor_cfg()
        await self.ensure_npc_diplomacy(world)
        # 旧档损坏的 pair/accum 字段先自愈（本方法可被单测单独调用）
        self._sanitize_npc_ai_state(world)
        threshold = float(cfg.get("aggression_threshold", 10))
        home_reserve = float(cfg.get("garrison_home_reserve", 0.3))
        ecfg = self._npc_economy_cfg()
        morale_min = int(ecfg["mobilize_morale_min"])
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        state = self._npc_ai_state(world)
        # 同一 tick 内目标势力的外交 / 盟主不会变化：按势力缓存，避免每个
        # 相邻节点重复 get_faction / _alliance_host（大图/多邻接时开销明显）。
        target_cache: dict[int, Optional[dict]] = {}
        host_cache: dict[int, Optional[dict]] = {}
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
                # 灭亡势力的在外军团必须同时清退：否则它可能在后续 tick
                # 攻下无主/敌方据点，让 alive=0 的势力重新拥有节点（幽灵
                # 势力），而 NPC 扫描只查 alive=1，永不复活——节点/霸权/
                # 威胁统计都会长期失真。
                siege_nodes: set[int] = set()
                for a in await self.get_armies(
                        world["id"], faction_id=fid):
                    if a.get("state") == "siege":
                        siege_nodes.add(int(a.get("node_id", 0) or 0))
                    await self._interrupt_army_tasks(world, a)
                    await self._refund_npc_army_grain(a)
                    await self._release_army_chars(a)
                    await self.delete_army(int(a["id"]))
                # 清退围城军后节点上的 siege_state 会悬挂：_step_sieges 只遍历
                # 现存围城军团，没有军团就永远轮不到该节点，城防自修 / 建造 /
                # 人物传送会一直被“围城中”阻塞。
                for nid in siege_nodes:
                    if nid > 0:
                        await self._clear_orphan_siege(
                            world, await self.get_node(world["id"], nid))
                self._prune_npc_ai_faction(world, fid)
                continue
            # 守军自然恢复统一由 _step_recovery 结算（§10.4 方案 A）；
            # 此处只统计现有守军战力，并做仇恨记忆逐 tick 衰减（§9.1）。
            own_power = 0.0
            for node in owned:
                gar = node.get("npc_garrison") or {}
                own_power += int(gar.get("troops", 0) or 0) / 100
                hatred = node.get("hatred") or {}
                if hatred:
                    node["hatred"] = {
                        k: int(v or 0) - 1 for k, v in hatred.items()
                        if int(v or 0) > 1}
                    await self.save_node(node)
            # §9.1 低兵/低士气：撤回本城保将（不做溃败惩罚）
            for a in await self.get_armies(
                    world["id"], faction_id=fid,
                    states=("idle", "march", "siege")):
                home_node = nodes.get(int(a.get("node_id", 0) or 0))
                at_home = bool(
                    home_node
                    and int(home_node.get("owner_faction", 0) or 0) == fid)
                if at_home and a.get("state") == "idle":
                    # 已在己方据点的闲置军团并入驻军：不归队也不会再被
                    # 出征复用，长局会无限堆积（见 _merge_idle_npc_army）。
                    await self._merge_idle_npc_army(world, a, home_node)
                    continue
                if int(a.get("troops", 0) or 0) < 300 or \
                        int(a.get("morale", 100) or 0) < 20:
                    await self._auto_retreat(a)
            # §10.7 守财底线：国库 coin/grain 不低于底线才出征（守军恢复免费）
            floor = self._npc_reserve(f)
            treasury = self._npc_treasury(f)
            if int(treasury["coin"]) < int(floor["coin"]) or \
                    int(treasury["grain"]) < int(floor["grain"]):
                continue
            # v4.6.1 战争狂热 / 威胁守家（§7.7 / §16.4）
            frenzy = self._npc_frenzy_state(world, fid)
            war_cfg = self._npc_war_cfg()
            threat_cfg = self._npc_threat_cfg()
            pairs_self = state.get("pairs", {}).get(str(fid)) or {}
            at_war = any(
                isinstance(p, dict) and p.get("phase") in ("war", "hostile")
                for p in pairs_self.values())
            active_wars = sum(
                1 for p in pairs_self.values()
                if isinstance(p, dict) and p.get("phase") == "war")
            # v4.6.1：战术层「不宣而战」必须先给战略层预留 1 个战争槽。
            # 否则 NPC 一开局就用突袭把 max_active_wars_per_npc 占满，
            # 威胁层永远走不到 hostile→war，自主宣战变成死代码。
            # 威胁层关闭时保持旧的突袭行为（见 _npc_tactical_new_war_allowed）。
            home = float(home_reserve) + (0.25 if at_war else 0.0)
            if frenzy["active"]:
                home = max(float(frenzy["home_reserve_min"]),
                           home + float(frenzy["home_reserve_delta"]))
            home = min(0.90, max(0.0, home))
            send_mult = float(frenzy["send_mult"])
            threshold_now = (float(frenzy["utility_threshold"])
                             if frenzy["active"] and
                             frenzy["utility_threshold"] is not None
                             else float(threshold))
            max_sorties = 2 + int(frenzy["sorties_bonus"])
            tick_no = int(world.get("tick_no", 0) or 0)
            node_morale_min = (int(frenzy["morale_min"])
                               if frenzy["morale_min"] is not None
                               else morale_min)
            # 出征决策：平时最多 2 路，战争狂热最多 4 路
            sorties = 0
            for node in owned:
                if sorties >= max_sorties:
                    break
                gar = node.get("npc_garrison") or {}
                troops = int(gar.get("troops", 0))
                send = int(troops * (1 - home) * send_mult)
                # §10.8 动员民心门槛：民心过低只允许小规模动员（≤ 守军上限 30%）
                if int(node.get("morale", 100) or 0) < node_morale_min:
                    send = min(send, int(R.garrison_cap(int(node["level"])) * 0.3))
                if send < 300:
                    continue
                best_target, best_util = None, threshold_now
                best_starts_war = False
                f_dip = f.get("diplomacy") or {}
                for adj_id in node.get("adj") or []:
                    target = nodes.get(int(adj_id))
                    if not target or target["owner_faction"] == fid:
                        continue
                    if int(target.get("owner_faction", 0) or 0) > 0 and \
                            int(target.get("protect_until", 0) or 0) > _now():
                        continue
                    # 同盟国不征伐（§6.3）
                    tf_owner = int(target["owner_faction"])
                    rel = R.REL_NEUTRAL
                    tfo = None
                    diplo_id = tf_owner
                    if tf_owner > 0:
                        if tf_owner in target_cache:
                            tfo = target_cache[tf_owner]
                        else:
                            tfo = await self.get_faction(tf_owner)
                            target_cache[tf_owner] = tfo
                        if tfo and int(tfo.get("alliance_id") or 0) and \
                                int(tfo.get("alliance_id")) == int(f.get("alliance_id") or 0):
                            continue
                        # 玩家联盟的外交由盟主代表：关系 / 停火 / 威胁倍率都按盟主算，
                        # 否则盟员外交副本与 NPC 字典各说各话（战和不同步）。
                        if tf_owner in host_cache:
                            host = host_cache[tf_owner]
                        else:
                            host = await self._alliance_host(tfo) if tfo else None
                            host_cache[tf_owner] = host
                        if host:
                            diplo_id = int(host["id"])
                    # 停火窗口内不主动进攻（§8.7）
                    if diplo_id > 0 and self._pair_ceasefire(
                            world, fid, diplo_id):
                        continue
                    # 停战状态机优先于外交真实时间：加速 tick / 补跑时
                    # 真实时间可能先过期，但 pair 仍应保持停战到 tick 到期。
                    tgt_pair = (state.get("pairs", {}).get(str(fid)) or {}
                                ).get(str(diplo_id)) or {}
                    if tgt_pair.get("phase") == "truce":
                        continue
                    if tf_owner > 0:
                        rel = R.relation_of(f_dip, diplo_id, _now())
                        # 同盟 / 停战 / 备战期都不主动进攻：停战刚签完
                        # 不能同一 tick 又出兵（否则立刻触发撕约惩罚）。
                        if rel in (R.REL_ALLY, R.REL_TRUCE, "war_pending"):
                            continue
                        # 好感足够高时 NPC 不愿主动开战（交易/赠礼的回报）
                        if rel != R.REL_WAR and \
                                R.favor_aggression_mult(
                                    R.favor_of(f_dip, diplo_id), fcfg) <= 0:
                            continue
                        # 战略层预留槽位：非正式战争目标不得再靠突袭开新战
                        if rel != R.REL_WAR and not \
                                self._npc_tactical_new_war_allowed(
                                    rel, active_wars):
                            continue
                    garrison_power = int((target.get("npc_garrison") or {}).get(
                        "troops", 0)) / 100
                    # §16.4 门口堆了多少兵：目标势力在本节点相邻处的守军
                    nearby = 0
                    for nb in target.get("adj") or []:
                        nb_node = nodes.get(int(nb))
                        if nb_node and int(nb_node.get(
                                "owner_faction", 0) or 0) == tf_owner:
                            nearby += int((nb_node.get("npc_garrison") or {})
                                          .get("troops", 0) or 0)
                    tgt_capital = int((tfo or {}).get(
                        "capital_node_id", 0) or 0)
                    adj_core = bool(
                        tgt_capital and tgt_capital in
                        [int(a) for a in target.get("adj") or []])
                    util = R.npc_utility(
                        target, f, int(own_power * 100), int(garrison_power * 100),
                        path_edges=1,
                        threat_nearby_power=int(nearby),
                        adjacent_to_enemy_core=adj_core)
                    # 侵略倍率 = 赛季/合纵 × 威胁 × 狂热，硬上限 aggression_cap
                    mult = self.npc_aggression_mult(world, fid, diplo_id)
                    if tf_owner > 0:
                        mult *= self._npc_threat_mult_for(world, fid, diplo_id)
                    if frenzy["active"]:
                        mult *= 1.0 + float(frenzy["aggression_bonus"])
                    mult = max(0.0, min(
                        mult, float(threat_cfg["aggression_cap"])))
                    util *= mult
                    if tf_owner > 0:
                        if rel == R.REL_WAR:
                            util *= float(fcfg.get("war_target_bonus", 1.35))
                            if frenzy["active"]:
                                util *= float(frenzy["target_bonus"])
                        else:
                            util *= R.favor_aggression_mult(
                                R.favor_of(f_dip, diplo_id), fcfg)
                    if util > best_util:
                        best_util, best_target = util, target
                        best_starts_war = bool(
                            tf_owner > 0 and rel != R.REL_WAR)
                if best_target:
                    army = await self._spawn_npc_army(
                        world, f, node, best_target, send,
                        cost_mult=float(frenzy["mobilization_cost_mult"]),
                        grain_mult=float(frenzy["grain_mult"]),
                        morale_min=(int(frenzy["morale_min"])
                                    if frenzy["morale_min"] is not None
                                    else None))
                    if army:
                        # 实际出兵量可能因民心门槛/国库截断小于 send，按实际扣减
                        used = min(troops, max(0, int(
                            army.get("troops", send) or 0)))
                        gar["troops"] = max(0, troops - used)
                        node["npc_garrison"] = gar
                        await self.save_node(node)
                        sorties += 1
                        if best_starts_war:
                            active_wars += 1
            # §7.8 非接壤霸主远征：仅 goal == hegemon，每 8 tick 至多一支
            if sorties < max_sorties:
                for tgt_key, wpair in pairs_self.items():
                    if not isinstance(wpair, dict) or \
                            wpair.get("phase") != "war" or \
                            str(wpair.get("goal") or "") != "hegemon":
                        continue
                    # 停火窗口内同样暂停远征（§8.7）
                    if self._pair_ceasefire(world, fid, int(tgt_key)):
                        continue
                    tgt_f = await self.get_faction(int(tgt_key))
                    if not tgt_f:
                        continue
                    exp = await self._npc_expedition_target(
                        world, {"nodes": nodes}, f, tgt_f)
                    if not exp:
                        continue
                    from_node = nodes.get(int(exp.get("_from_node") or 0))
                    if not from_node:
                        continue
                    egar = from_node.get("npc_garrison") or {}
                    etroops = int(egar.get("troops", 0) or 0)
                    esend = int(etroops * (1 - home) * send_mult)
                    if esend < int(war_cfg["expedition"]["min_troops"]):
                        continue
                    army = await self._spawn_npc_army(
                        world, f, from_node, exp, esend,
                        cost_mult=float(frenzy["mobilization_cost_mult"]),
                        grain_mult=float(
                            war_cfg["expedition"]["grain_mult"]),
                        morale_min=(int(frenzy["morale_min"])
                                    if frenzy["morale_min"] is not None
                                    else None))
                    if army:
                        used = min(etroops, max(0, int(
                            army.get("troops", esend) or 0)))
                        egar["troops"] = max(0, etroops - used)
                        from_node["npc_garrison"] = egar
                        await self.save_node(from_node)
                        wpair["last_expedition_tick"] = tick_no
                        sorties += 1
                    break
        # 魔潮军团（faction -1）：普通军团扑最近节点；猎潮军优先扑最强势力的据点
        data = world.get("data") or {}
        hunters = data.get("demon_hunters") or {}
        for a in await self.get_armies(world["id"], faction_id=-1, states=("idle",)):
            hunter = hunters.get(str(a["id"])) or {}
            target_fid = int(hunter.get("target_faction") or 0)
            target = None
            if target_fid > 0:
                target = await self._nearest_faction_node(
                    world, int(a["node_id"]), target_fid)
                if target is None:
                    # 目标势力已被灭/已无据点：下次生成前改追当前最强
                    new_fid, _pct = await self._strongest_faction(world)
                    if new_fid > 0:
                        hunter["target_faction"] = int(new_fid)
                        hunters[str(a["id"])] = hunter
                        data["demon_hunters"] = hunters
                        world["data"] = data
                        target = await self._nearest_faction_node(
                            world, int(a["node_id"]), int(new_fid))
            if target is None:
                target = await self._nearest_enemy_node(
                    world, a["node_id"], exclude=-1)
            if target:
                path = await self.find_path(world["id"], a["node_id"], target)
                if path and len(path) > 1:
                    a["state"] = "march"
                    a["path"] = path[1:]
                    a["grain"] = max(a["grain"], 9999)  # 魔潮就食于野
                    await self.save_army(a)
        await self._commit()

    async def _step_npc_develop(self, world: dict):
        """NPC 城池发展（§10.6）：稳定期 + 国库付费 + 建造时间 + 节奏上限。

        只负责“启动建造”：写入 ``node.construction`` 后由统一的
        ``_step_construction`` 到期结算（等级 +1 / 城防刷新）。
        """
        dcfg = (self.main_cfg().get("npc") or {}).get("develop") or {}
        now = _now()
        tick_no = int(world.get("tick_no", 0) or 0)
        interval = max(0, int(dcfg.get("interval_hours", 6))) * 3600
        max_building = max(1, int(dcfg.get("max_building", 1)))
        stabilize = list(dcfg.get("stabilize_hours") or [0, 8, 18, 32, 50])
        build_ticks = list(dcfg.get("build_ticks") or [0, 4, 6, 8, 10])
        data = world.get("data") or {}
        raw = data.get("npc_develop")
        progress = dict(raw) if isinstance(raw, dict) else {}
        nodes = await self.get_nodes(world["id"])
        by_id = {int(n["id"]): n for n in nodes}
        factions = await self._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "AND alive=1", (world["id"],))
        changed = False
        ally_cache: dict[int, set[int]] = {}
        for frow in factions:
            fid = int(frow["id"])
            f = await self.get_faction(fid)
            if not f:
                continue
            owned = [n for n in nodes if int(n.get("owner_faction", 0)) == fid]
            if not owned:
                continue
            bias = self._npc_personality_mult(f, "develop_bias")
            # ① 稳定期懒初始化：必须放在节奏/在建门槛之前执行——否则势力
            # 忙于建造或在间隔期内时，新占/刚完成据点的稳定期迟迟不起算，
            # 实际升级节奏被额外拖慢一整个 interval_hours（§10.6 四条件）。
            for node in owned:
                level = max(1, int(node.get("level", 1) or 1))
                if node.get("construction") or node.get("siege_state"):
                    continue
                if level >= R.node_potential(node):
                    continue
                if int(node.get("develop_ready_at", 0) or 0) > 0:
                    continue
                idx = level  # 目标等级 = level + 1，配置按目标等级索引
                hours = float(stabilize[idx]) if 0 <= idx < len(stabilize) \
                    else 8.0
                node["develop_ready_at"] = now + max(
                    0, int(hours * 3600 / max(0.1, bias)))
                await self.save_node(node)
                changed = True
            # ④ 节奏上限：两次启动间隔 ≥ interval_hours；同时在建 ≤ max_building
            rec = dict(progress.get(str(fid)) or {})
            last_start = int(rec.get("last_start_at", 0) or 0)
            if last_start > 0 and now - last_start < interval:
                continue
            building = [n for n in owned if (n.get("construction") or {})]
            if len(building) >= max_building:
                continue
            friendly = await self._friendly_ids(
                int(world["id"]), fid, ally_cache) or {fid}
            floor = self._npc_reserve(f)
            treasury = self._npc_treasury(f)
            cands: list[tuple[int, int, int, int, dict]] = []
            for node in owned:
                level = max(1, int(node.get("level", 1) or 1))
                potential = R.node_potential(node)
                if node.get("construction") or node.get("siege_state"):
                    continue
                if level >= potential:
                    continue
                ready_at = int(node.get("develop_ready_at", 0) or 0)
                if ready_at <= 0 or ready_at > now:
                    continue
                cost_coin, cost_iron = R.node_upgrade_cost(level)
                # ② 国库付费：候选必须“国库够”才进入排序（§10.6）。若把
                # 买不起的高价目标也留在候选里，排序后取第 1 个会直接 continue，
                # 同势力明明买得起的低价目标就永远等不到机会。
                free_coin = int(treasury["coin"]) - int(
                    treasury.get("reserved_coin", 0) or 0)
                if free_coin - cost_coin < int(floor["coin"]):
                    continue
                if int(treasury["iron"]) - cost_iron < int(floor["iron"]):
                    continue
                # 与敌接壤：相邻节点归属非本势力/非同盟且已有主
                border = 0
                for adj_id in node.get("adj") or []:
                    nb = by_id.get(int(adj_id))
                    if not nb:
                        continue
                    owner = int(nb.get("owner_faction", 0) or 0)
                    if owner > 0 and owner not in friendly:
                        border = 1
                        break
                cands.append((
                    potential, border,
                    int(R.NODE_OUTPUT_VALUE.get(node["type"], 40)), -level, node))
            if not cands:
                continue
            # 选城优先级（确定性）：潜力 ↓、接壤 ↓、产出值 ↓、等级 ↑
            cands.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
            node = cands[0][4]
            level = max(1, int(node.get("level", 1) or 1))
            target = level + 1
            cost_coin, cost_iron = R.node_upgrade_cost(level)
            if not self._treasury_spend(
                    f, coin=cost_coin, iron=cost_iron, floor=floor):
                continue
            idx = target - 1
            ticks = int(build_ticks[idx]) if 0 <= idx < len(build_ticks) \
                else R.node_upgrade_ticks(level)
            ticks = max(1, ticks)
            node["construction"] = {
                "to_level": target,
                "end_at": now + ticks * self.tick_seconds(),
                "end_tick": tick_no + ticks,
                "started_at": now,
                "by": f"npc:{fid}",
            }
            # 完成后由下次懒初始化按新等级重算稳定期
            node["develop_ready_at"] = 0
            progress[str(fid)] = {"last_start_at": now,
                                  "building_node_id": int(node["id"])}
            data["npc_develop"] = progress
            world["data"] = data
            await self.save_node(node)
            await self.save_faction(f)
            changed = True
        if changed:
            await self.save_world(world)
        await self._commit()

    async def _step_npc_economy(self, world: dict):
        """NPC 经济决策（§10.8）：自动赈济维稳。

        坊市侧的“需求驱动挂单/收购”由 ``_step_market_maker`` /
        ``_step_market_offers`` 按国库缺口执行，二者共用同一套国库口径。
        """
        ecfg = self._npc_economy_cfg()
        if not ecfg.get("relief_enabled", True):
            return
        gcfg = self.main_cfg().get("governance") or \
            (self.main_cfg().get("relief") or {})
        cost = max(1, int(gcfg.get("relief_coin_cost", 3000)))
        gain = max(1, int(gcfg.get("relief_morale", 6)))
        limit = max(1, int(gcfg.get("relief_daily_limit", 3)))
        now = _now()
        day = now // 86400
        data = world.get("data") or {}
        raw = data.get("npc_relief")
        log = dict(raw) if isinstance(raw, dict) else {}
        nodes = await self.get_nodes(world["id"])
        factions = await self._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "AND alive=1", (world["id"],))
        changed = False
        for frow in factions:
            fid = int(frow["id"])
            f = await self.get_faction(fid)
            if not f:
                continue
            owned = [n for n in nodes if int(n.get("owner_faction", 0)) == fid]
            if not owned:
                continue
            rec = dict(log.get(str(fid)) or {})
            if int(rec.get("day", 0) or 0) != day:
                rec = {"day": day, "used": 0}
            if int(rec.get("used", 0) or 0) >= limit:
                continue
            bias = self._npc_personality_mult(f, "relief_bias")
            # 性格驱动：赈济倾向越高，触发阈值越高（民心略低就赈济）
            threshold = max(30, min(70, 50 + (bias - 1.0) * 10))
            # 民心最低的据点优先，避免“救急不到”
            for node in sorted(owned, key=lambda n: int(n.get("morale", 0) or 0)):
                if int(rec["used"]) >= limit:
                    break
                if int(node.get("morale", 0) or 0) >= threshold:
                    continue
                # §10.8 赈济例外：允许低于守财底线，只要 coin 足够
                if not self._treasury_spend(f, coin=cost):
                    break
                await self._bump_morale(node, gain, reason="npc_relief")
                await self.save_node(node)
                rec["used"] = int(rec.get("used", 0)) + 1
                log[str(fid)] = rec
                changed = True
            await self.save_faction(f)
        data["npc_relief"] = log
        world["data"] = data
        if changed:
            await self.save_world(world)
        await self._commit()

    async def _strongest_faction(self, world: dict) -> tuple[int, float]:
        """当前世界最强势力的 (faction_id, 霸权占比)；无玩家/NPC 势力时返回 (0, 0)。"""
        scfg = self.main_cfg().get("season") or {}
        types_by_fid: dict[int, list[str]] = {}
        for n in await self.get_nodes(world["id"]):
            fid = int(n.get("owner_faction", 0) or 0)
            if fid > 0:
                types_by_fid.setdefault(fid, []).append(n["type"])
        shares = R.hegemony_share(types_by_fid, scfg.get("node_score"))
        if not shares:
            return 0, 0.0
        fid, pct = max(shares.items(), key=lambda kv: (kv[1], kv[0]))
        return int(fid), float(pct)

    async def _nearest_faction_node(self, world: dict, src: int,
                                    faction_id: int) -> Optional[int]:
        """BFS 找距 src 最近的指定势力据点（不含 src 自身）。"""
        from collections import deque
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        if int(nodes.get(src, {}).get("owner_faction", 0) or 0) == int(faction_id):
            return int(src)
        q = deque([int(src)])
        seen = {int(src)}
        while q:
            cur = q.popleft()
            for nxt in nodes.get(cur, {}).get("adj") or []:
                nxt = int(nxt)
                if nxt in seen or nxt not in nodes:
                    continue
                seen.add(nxt)
                if int(nodes[nxt].get("owner_faction", 0) or 0) == int(faction_id):
                    return nxt
                q.append(nxt)
        return None

    async def _spawn_demon_army(self, world: dict, node: dict, rng,
                                target_fid: int = 0) -> dict:
        """生成一支魔潮军团；``target_fid`` >0 时登记为猎潮军。"""
        demon = R.generate_character(
            rng, rarity="rare", role=R.ROLE_GENERAL, profession="demon",
            name_pools=self.cfg.get("names") or {})
        demon.update({"world_id": world["id"], "faction_id": -1,
                      "owner_user_id": "", "loyalty": 100,
                      "status": "army"})
        await self.insert_char(demon)
        troops = rng.randint(800, 1500)
        prefix = "魔潮猎潮" if int(target_fid) > 0 else "魔潮"
        army = {
            "world_id": world["id"], "faction_id": -1, "owner_user_id": "",
            "name": f"{prefix}·{demon['name']}", "node_id": node["id"],
            "characters": [demon["id"]], "troop_type": "beast_horde",
            "veteran": rng.randint(10, 40), "troops": troops,
            "grain": 9999, "morale": 100, "state": "idle",
        }
        await self.insert_army(army)
        if int(target_fid) > 0:
            data = world.get("data") or {}
            hunters = data.get("demon_hunters") or {}
            hunters[str(army["id"])] = {
                "target_faction": int(target_fid),
                "spawn_tick": int(world.get("tick_no", 0) or 0)}
            data["demon_hunters"] = hunters
            world["data"] = data
        return army

    async def _pick_demon_hunter_node(self, world: dict, target_fid: int,
                                      occupied: set[int], rng) -> Optional[dict]:
        """为猎潮军选一个靠近目标势力的无主/魔潮节点作为出生点。"""
        from collections import deque
        nodes_list = await self.get_nodes(world["id"])
        nodes = {n["id"]: n for n in nodes_list}
        cands = [n for n in nodes_list
                 if int(n.get("owner_faction", 0) or 0) in (0, -1)
                 and int(n["id"]) not in occupied]
        if not cands:
            # 全图已被瓜分时，退而求其次在第三方势力边境生成，保证反寡头
            # 机制在中后期仍能启动；若地图只剩目标一家则本次不生成。
            cands = [n for n in nodes_list
                     if int(n.get("owner_faction", 0) or 0) > 0
                     and int(n.get("owner_faction", 0) or 0) != int(target_fid)
                     and int(n["id"]) not in occupied]
        if not cands:
            return None
        targets = [n["id"] for n in nodes_list
                   if int(n.get("owner_faction", 0) or 0) == int(target_fid)]
        if not targets:
            return rng.choice(cands)
        # 多源 BFS：从目标势力所有据点同时扩散，取距目标最近的出生点
        dist = {int(t): 0 for t in targets}
        q = deque(int(t) for t in targets)
        while q:
            cur = q.popleft()
            for nxt in nodes.get(cur, {}).get("adj") or []:
                nxt = int(nxt)
                if nxt in dist or nxt not in nodes:
                    continue
                dist[nxt] = dist[cur] + 1
                q.append(nxt)
        reachable = [n for n in cands if int(n["id"]) in dist]
        pool = reachable or cands
        best = min((dist.get(int(n["id"]), 9999) for n in pool), default=0)
        nearest = [n for n in pool if dist.get(int(n["id"]), 9999) == best]
        # 优先遗迹/矿点，保持「边缘生成」观感
        typed = [n for n in nearest if n.get("type") in ("ruin", "mine")]
        return rng.choice(typed or nearest)

    async def _spawn_demon_hunter(self, world: dict, rng,
                                  target_fid: int,
                                  occupied: set[int]) -> Optional[dict]:
        node = await self._pick_demon_hunter_node(
            world, target_fid, occupied, rng)
        if not node:
            return None
        return await self._spawn_demon_army(
            world, node, rng, target_fid=int(target_fid))

    async def _maybe_spawn_demon_hunters(self, world: dict, tick_no: int,
                                         data: dict, rules: dict):
        """魔潮持续期间每隔 ``hunter_interval_ticks`` 额外生成猎潮军。"""
        interval = max(1, int(rules.get("hunter_interval_ticks", 8)))
        nxt = int(data.get("demon_next_hunter_tick", 0) or 0)
        if nxt <= 0:
            data["demon_next_hunter_tick"] = tick_no + interval
            world["data"] = data
            return
        if tick_no < nxt:
            return
        data["demon_next_hunter_tick"] = tick_no + interval
        armies = await self.get_armies(world["id"], faction_id=-1)
        cap = int(rules.get("max_armies", 8))
        if len(armies) >= cap:
            world["data"] = data
            return
        target_fid, _pct = await self._strongest_faction(world)
        if target_fid <= 0:
            world["data"] = data
            return
        rng = self._rng("demon_hunter", world["id"], tick_no)
        occupied = {int(a.get("node_id", 0)) for a in armies}
        count = min(max(1, int(rules.get("hunter_count", 1))),
                    cap - len(armies))
        for _ in range(count):
            army = await self._spawn_demon_hunter(
                world, rng, int(target_fid), occupied)
            if not army:
                break
            occupied.add(int(army["node_id"]))
        data["demon_hunters"] = data.get("demon_hunters") or {}
        world["data"] = data

    async def _step_demon_tide(self, world: dict):
        """魔潮收尾与保护期清理。

        - 魔潮打下据点后会正常占点（``owner_faction=-1``），但**不享受**
          新城/护山盾保护期：这里每 tick 清掉 -1 据点上的 ``protect_until``，
          玩家可以立即组织反攻；
        - 事件持续期间按 ``hunter_interval_ticks`` 额外生成猎潮军，目标为
          当前霸权最高的势力（反寡头机制）；
        - 事件到期后删除未阵亡的魔潮军团与临时魔修，防止跨事件堆积；
          已占领据点保留为魔潮领地，直到被玩家/NPC 夺回。
        """
        tick_no = int(world.get("tick_no", 0) or 0)
        data = world.get("data") or {}
        all_nodes = await self.get_nodes(world["id"])
        nodes = [n for n in all_nodes
                 if int(n.get("owner_faction", 0) or 0) == -1]
        cleared = 0
        for n in nodes:
            if int(n.get("protect_until", 0) or 0) != 0:
                n["protect_until"] = 0
                await self.save_node(n)
                cleared += 1
        active = any(str(ev.get("event_id") or "") == "demon_tide"
                     for ev in await self.get_active_events(world["id"], tick_no))
        armies = await self.get_armies(world["id"], faction_id=-1)
        # 清理已被消灭的猎潮军目标登记，防止 world.data 膨胀
        hunters = data.get("demon_hunters") or {}
        alive = {str(a["id"]) for a in armies}
        pruned = {k: v for k, v in hunters.items() if k in alive}
        if pruned != hunters:
            data["demon_hunters"] = pruned
            world["data"] = data
        if active:
            data["demon_tide_active"] = True
            world["data"] = data
            rules = (self.cfg.get("events") or {}).get("demon_tide") or {}
            await self._maybe_spawn_demon_hunters(
                world, tick_no, data, rules)
            if cleared:
                logger.info(f"[strategy] 魔潮：清除 {cleared} 个 -1 据点保护期")
            return
        # 无事件期间清理魔潮围城留下的悬挂状态（攻方军团可能已被删除，
        # 否则城池会一直显示「被围」且城防无法自修）
        for n in all_nodes:
            ss = n.get("siege_state") or {}
            if int(ss.get("attacker_faction", 0) or 0) == -1:
                if await self._active_manual_battle(world["id"], int(n["id"])):
                    continue
                n["siege_state"] = {}
                await self.save_node(n)
        if not armies:
            if data.get("demon_tide_active"):
                data["demon_tide_active"] = False
                world["data"] = data
                await self.save_world(world)
            return
        # 手操战役中的魔潮军团不能直接删除（否则 battle 会话引用悬空，
        # 玩家打一半单位消失）；等战斗结束后再回收。
        battle_hunters = {}
        deleted = 0
        for a in armies:
            if a.get("state") == "battle":
                hid = str(a["id"])
                if hid in hunters:
                    battle_hunters[hid] = hunters[hid]
                continue
            for cid in a.get("characters") or []:
                await self._execute(
                    "DELETE FROM strategy_characters WHERE id=? AND faction_id=-1 "
                    "AND status NOT IN ('captive','transfer')",
                    (int(cid),))
            await self.delete_army(a["id"])
            deleted += 1
        data["demon_tide_active"] = False
        data["demon_hunters"] = battle_hunters
        world["data"] = data
        await self.save_world(world)
        logger.info(f"[strategy] 魔潮收尾：清理 {deleted} 支军团；"
                    f"保留 {len(nodes)} 个魔潮据点")

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
                              target: dict, troops: int,
                              cost_mult: float = 1.0,
                              grain_mult: float = 1.0,
                              morale_min: Optional[int] = None) -> Optional[dict]:
        """NPC 出征建军团（§10.7）：动员费 + 招募粮耗 + 携粮 + 扣民心。

        费用/携粮均从势力国库真实扣减，扣后不得低于守财底线；国库不足
        直接返回 None（不组建）。魔潮（faction=-1）无国库，不走本函数。

        v4.6.1：``cost_mult`` / ``grain_mult`` / ``morale_min`` 供战争狂热
        与霸主远征覆盖默认值，缺省时行为与旧版一致。

        v4.8.1：兵种不再固定取势力 ``troop_pref``（多数 NPC 都是剑盾步），
        改为按主将六维 + 兵种适性挑选最合适兵种（``R.pick_troop_type``），
        势力偏好仅作接近时的小加成。
        """
        fid = int(faction["id"])
        pers = faction.get("personality") or {}
        preferred_troop = str(pers.get("troop_pref") or "")
        ecfg = self._npc_economy_cfg()
        # §10.8 动员民心门槛：民心过低只允许小规模（≤ 守军上限 30%）
        level = max(1, int(node.get("level", 1) or 1))
        troops = max(0, int(troops or 0))
        gate_morale = int(ecfg["mobilize_morale_min"]) if morale_min is None \
            else int(morale_min)
        if int(node.get("morale", 100) or 0) < gate_morale:
            troops = min(troops, max(1, int(R.garrison_cap(level) * 0.3)))
        if troops <= 0:
            return None
        rng = self._rng("npc_army", fid, node["id"], world["tick_no"])
        # 主将先定：兵种要按主将属性/适性挑选（无空闲武将则临时生成，
        # 但落库要等国库/路线校验通过，避免失败时留下孤儿角色）。
        leader = None
        for c in await self.get_chars(
                faction_id=fid, world_id=world["id"], status="idle"):
            if c["role"] == R.ROLE_GENERAL:
                leader = c
                break
        generated = False
        if not leader:
            leader = R.generate_character(
                rng, rarity=rng.choice(["fine", "rare"]),
                role=R.ROLE_GENERAL,
                name_pools=self.cfg.get("names") or {})
            leader.update({"world_id": world["id"], "faction_id": fid,
                           "owner_user_id": "", "loyalty": 100})
            generated = True
        troop_type = R.pick_troop_type(leader, preferred=preferred_troop)
        logger.debug(
            "[strategy] NPC 编军 %s：主将 %s 选 %s（势力偏好 %s）",
            faction.get("name", fid), leader.get("name", ""), troop_type,
            preferred_troop or "无")
        # 兵种价差：按实际编成兵种募兵价 / 剑盾价折算
        tt = R.TROOP_TYPES.get(troop_type) or {}
        base_tt = R.TROOP_TYPES.get("sword_shield") or {}
        base_coin = max(1.0, float(base_tt.get("recruit_coin", 50) or 50))
        ratio = float(tt.get("recruit_coin", base_coin) or base_coin) / base_coin
        coin_cost = max(1, int(round(
            troops / 100.0 * float(ecfg["mobil_coin_per_100"]) * ratio *
            float(ecfg["mobilization_cost_mult"]) * float(cost_mult))))
        grain_cost = max(1, int(round(
            troops / 100.0 * float(ecfg["mobil_grain_per_100"]))))
        floor = self._npc_reserve(faction)
        if not self._treasury_spend(
                faction, coin=coin_cost, grain=grain_cost, floor=floor):
            return None
        # 携粮从国库拨付（保留守财底线，避免把本金一次搬空）
        treasury = self._npc_treasury(faction)
        want_grain = max(1, int(round(
            troops * float(ecfg["carry_grain_per_100"]) / 100.0 *
            float(grain_mult))))
        carry = min(want_grain,
                    max(0, int(treasury["grain"]) - int(floor["grain"])))
        if carry > 0:
            self._treasury_spend(faction, grain=carry, floor=floor)
        path = await self.find_path(world["id"], node["id"], target["id"])
        if not path or len(path) < 2:
            # 无路可走：退回本 tick 已扣的动员费/携粮，避免资源凭空蒸发
            self._treasury_add(
                faction, coin=coin_cost, grain=grain_cost + carry)
            return None
        if generated:
            await self.insert_char(leader)
        leader["status"] = "army"
        leader["location"] = {"node_id": node["id"]}
        await self.save_char(leader)
        # §10.8 征兵消耗民心：每 100 兵扣 mobil_morale_per_100（抽丁）
        drain = int(round(
            troops / 100.0 * float(ecfg["mobil_morale_per_100"])))
        if drain > 0:
            await self._bump_morale(node, -drain, reason="npc_mobilize")
        await self.save_node(node)
        await self.save_faction(faction)
        army = {
            "world_id": world["id"], "faction_id": fid, "owner_user_id": "",
            "name": f"{faction.get('name', 'NPC')}·{leader['name']}军",
            "node_id": node["id"], "characters": [leader["id"]],
            "troop_type": troop_type, "veteran": rng.randint(0, 30),
            "troops": int(troops), "grain": int(carry), "morale": 90,
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
    def _event_weight(self, ev: dict, tick_no: int, data: dict,
                      ecfg: dict, main_demon: bool) -> int:
        """事件权重：魔潮接入和平期加成、每周保底与主玩法联动（§10.1）。"""
        weight = max(1, int(ev.get("weight", 1)))
        if str(ev.get("id") or "") != "demon_tide":
            return weight
        rules = ecfg.get("demon_tide") or {}
        last_battle = int(data.get("last_battle_tick", 0) or 0)
        peace_ticks = int(rules.get("peace_ticks", 96))
        if not last_battle or tick_no - last_battle >= peace_ticks:
            weight = int(weight * float(rules.get("peace_weight_mult", 2.0)))
        guarantee = int(rules.get("weekly_guarantee_ticks", 672))
        last_tide = int(data.get("demon_tide_last_tick", 0) or 0)
        overdue = (tick_no >= guarantee) if last_tide <= 0 else \
            (tick_no - last_tide >= guarantee)
        if overdue:
            weight *= 1000  # 每周保底：权重压制其余事件，避免一直抽不到
        if main_demon:
            weight = int(weight * float(
                rules.get("main_event_weight_mult", 2.0)))
        return max(1, weight)

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
        main_demon = False
        try:
            main_demon = str(await self.db.ext.get_system_config(
                "demon_invasion_active") or "") == "1"
        except Exception:  # noqa: BLE001 - 主玩法未初始化时按未开启处理
            main_demon = False
        for _ in range(rolls):
            eligible = [e for e in events if tick_no >= int(cd.get(e["id"], 0))]
            if not eligible:
                break
            weights = [self._event_weight(e, tick_no, data, ecfg, main_demon)
                       for e in eligible]
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
            effects = [dict(e) for e in (ev.get("effects") or [])]
            # §9 谣言类据点事件：驻守太守在场 → 民心动摇减半、debuff 时长
            # -1 tick（非据点事件 / 非谣言不受影响）
            if ev.get("scope") == "region" and target.get("node_ids"):
                rumor_effects = [
                    e for e in effects
                    if str(e.get("type")) in ("rumor", "node_morale")
                    and float(e.get("morale", e.get("delta", 0)) or 0) < 0]
                if rumor_effects:
                    rnode = await self.get_node(
                        world["id"], int(target["node_ids"][0]))
                    gov = await self._valid_node_governor(rnode) if rnode else None
                    owner_f = await self.get_faction(
                        int((rnode or {}).get("owner_faction") or 0)) if rnode else None
                    if rnode and gov and await self._governor_onsite(rnode, gov) \
                            and self._salary_governor_paid(owner_f, rnode["id"]):
                        duration = max(1, duration - 1)
                        for e in rumor_effects:
                            if e.get("morale") is not None:
                                e["morale"] = int(
                                    float(e["morale"]) * 0.5)
                            if e.get("delta") is not None:
                                e["delta"] = int(float(e["delta"]) * 0.5)
            await self._execute("""
                INSERT INTO strategy_events (world_id, event_id, scope, target_json,
                    start_at, end_at, start_tick, end_tick, data_json)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (world["id"], ev["id"], ev.get("scope", "region"),
                  json.dumps(target, ensure_ascii=False), _now(),
                  _now() + duration * self.tick_seconds(), tick_no,
                  tick_no + duration,
                  json.dumps({"name": ev.get("name", ev["id"]),
                              "effects": effects}, ensure_ascii=False)))
            cd[ev["id"]] = tick_no + duration + int(ev.get("cooldown_ticks", 32))
            names.append(ev.get("name", ev["id"]))
            # 即时效果（用调整后的 effects/duration，避免再次读到原始配置）
            ev_onset = dict(ev)
            ev_onset["effects"] = effects
            ev_onset["duration_ticks"] = duration
            await self._apply_event_onset(world, ev_onset, target, rng)
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
                    # v4.3.0：复用计略「焚粮」的 _burn_owner_grain——此前
                    # 玩家势力只 fetchone 烧「第一家」成员的粮，多人势力
                    # 的其他成员毫发无损（事件伤害取决于 DB 行序）
                    await self._burn_owner_grain(world, node, pct)
            elif eff.get("type") == "spawn_demon_armies":
                ecfg = self.cfg.get("events") or {}
                rules = ecfg.get("demon_tide") or {}
                existing = len(await self.get_armies(
                    world["id"], faction_id=-1))
                cap = int(rules.get("max_armies", 8))
                lo, hi = eff.get("count") or [2, 4]
                count = min(rng.randint(int(lo), int(hi)), max(0, cap - existing))
                try:
                    if str(await self.db.ext.get_system_config(
                            "demon_invasion_active") or "") == "1":
                        count = min(cap - existing,
                                    count + int(rules.get("main_event_extra", 1)))
                except Exception:  # noqa: BLE001
                    pass
                occupied = {int(a.get("node_id", 0)) for a in
                            await self.get_armies(world["id"], faction_id=-1)}
                cands = [n for n in await self.get_nodes(world["id"])
                         if n["owner_faction"] == 0 and n["type"] in ("ruin", "mine")
                         and int(n["id"]) not in occupied]
                rng.shuffle(cands)
                for node in cands[:max(0, count)]:
                    await self._spawn_demon_army(world, node, rng)
                data = world.get("data") or {}
                data["demon_tide_active"] = True
                data["demon_tide_last_tick"] = int(world.get("tick_no", 0) or 0)
                data["demon_next_hunter_tick"] = int(
                    world.get("tick_no", 0) or 0) + max(
                        1, int(rules.get("hunter_interval_ticks", 8)))
                world["data"] = data
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
        # 网页面板每 20s 拉一次快照：记录「最近活跃」用于野战手操窗口。
        # 只写内存，避免在读接口里与 tick 大事务抢同一连接提交。
        if p:
            now_seen = _now()
            self._recent_players[str(user_id)] = now_seen
            if len(self._recent_players) > 5000:
                cutoff = now_seen - 7200
                self._recent_players = {
                    k: v for k, v in self._recent_players.items() if v >= cutoff}
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
                               "node_count": node_count.get(f["id"], 0),
                               # v4.6.1：天下视图需要的关系 / 性格快照
                               "diplomacy": f.get("diplomacy") or {},
                               "personality": f.get("personality") or {},
                               "capital_node_id": int(
                                   f.get("capital_node_id", 0) or 0)}
        unpaid_gov_by_fid = {
            int(f["id"]): {int(x) for x in (((f.get("salary") or {}).get(
                "unpaid_governor_node_ids")) or [])}
            for f in factions}
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
        node_map = {n["id"]: n for n in nodes}
        territory_cfg = self.main_cfg().get("territory") or {}
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
        world_chars = await self.get_chars(world_id=wid)
        char_by_id = {int(c["id"]): c for c in world_chars}
        salary_per = (((self.main_cfg().get("faction") or {}).get("salary")
                       or {}).get("per_tick") or {})
        out_nodes = []
        # 驻守人物：所在地 == 据点 且状态为 idle/本地任务（§所在地机制）；
        # 远征（侦察/劫掠/运粮）与计略任务人在外勤，不算驻守。
        active_task_types = {
            int(r["character_id"]): str(r["task_type"])
            for r in await self._fetchall(
                "SELECT character_id, task_type FROM strategy_tasks "
                "WHERE world_id=? AND status='active'", (wid,))}
        residents_by_node: dict[int, list[dict]] = {}
        for c0 in world_chars:
            if not R.is_resident_char(c0):
                continue
            if str(c0.get("status") or "") == "task" and \
                    active_task_types.get(int(c0["id"])) not in R.TASK_LOCAL_TYPES:
                continue
            nid0 = R.char_node_id(c0)
            if nid0 > 0:
                residents_by_node.setdefault(nid0, []).append(c0)
        for n in nodes:
            gar = int((n.get("npc_garrison") or {}).get("troops", 0))
            visible = n["id"] in visible_nodes or n["id"] in scouted
            ss = n.get("siege_state") or {}
            gov_id = int(n.get("governor_char_id") or 0)
            gov = char_by_id.get(gov_id)
            gov_valid = bool(
                gov and gov.get("status") not in
                ("dead", "captive", "transfer", "wander") and
                R.injury_state(gov.get("injury", 0)) != "dead")
            gov_resident = bool(gov and await self._governor_onsite(n, gov))
            out_nodes.append({
                "id": n["id"], "name": n["name"], "type": n["type"],
                "terrain": n["terrain"], "level": n["level"],
                "pos": [n["pos_x"], n["pos_y"]], "adj": n["adj"],
                "owner": f_meta.get(n["owner_faction"],
                                    {"id": n["owner_faction"], "name": "?",
                                     "color": "#888", "kind": "npc"}),
                "defense": n["defense"],
                "defense_max": R.city_defense_max(n["level"]),
                "morale": n["morale"],
                "morale_cap": self._morale_cap(n),
                # v81 所在地/城镇等级：潜力与建造中（升级上限 = 潜力）
                "potential": R.node_potential(n),
                "construction": dict(n.get("construction") or {}) or None,
                "develop_ready_at": int(n.get("develop_ready_at", 0) or 0),
                "upgrade_cost": ({
                    "coin": R.node_upgrade_cost(n["level"])[0],
                    "iron": R.node_upgrade_cost(n["level"])[1],
                    "ticks": R.node_upgrade_ticks(n["level"])}
                    if int(n["level"]) < R.node_potential(n) else None),
                "residents": ([{"id": int(c0["id"]), "name": str(c0["name"]),
                                "owner_user_id": str(c0.get("owner_user_id") or ""),
                                "role": str(c0.get("role") or ""),
                                "role_name": R.ROLE_NAMES.get(
                                    str(c0.get("role") or ""), ""),
                                "status": str(c0.get("status") or ""),
                                "status_name": R.CHAR_STATUS_NAMES.get(
                                    str(c0.get("status") or ""), ""),
                                "is_governor": int(c0["id"]) == gov_id}
                               for c0 in residents_by_node.get(n["id"], [])]
                              if visible else []),
                "relief_used": self._relief_used(p, n["id"]) if p else 0,
                "governor": {
                    "char_id": gov_id,
                    "name": str((gov or {}).get("name") or ""),
                    "owner_user_id": str((gov or {}).get("owner_user_id") or ""),
                    "status": str((gov or {}).get("status") or ""),
                    "valid": gov_valid,
                    "resident": gov_resident,
                    "salary_paid": int(n["id"]) not in unpaid_gov_by_fid.get(
                        int(n.get("owner_faction") or 0), set()),
                    "salary_per_tick": int(salary_per.get("taishou", 0) or 0),
                } if gov_id else None,
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
                "protected": int(n.get("owner_faction", 0) or 0) > 0 and
                int(n.get("protect_until", 0) or 0) > now_ts,
                # 相邻友方领土加成（按观察者联盟计算）与友方道路连接
                "territory": (R.territory_bonuses(
                    R.adjacent_friendly_count(n, node_map, alliance_ids),
                    territory_cfg)
                    if int(n.get("owner_faction") or 0) in alliance_ids else None),
                "roads": ([int(x) for x in (n.get("adj") or [])
                           if int(x) in node_map and
                           int(node_map[int(x)].get("owner_faction") or 0)
                           in alliance_ids]
                          if int(n.get("owner_faction") or 0) in alliance_ids
                          else []),
            })
        # 军团（迷雾：非可见据点上的敌军不展示）
        out_armies = []
        army_count_by_fid: dict[int, int] = {}
        army_troops_by_fid: dict[int, int] = {}
        all_armies = await self.get_armies(wid)
        for a in all_armies:
            is_mine = a.get("owner_user_id") == user_id or \
                a["faction_id"] in alliance_ids
            node_visible = a["node_id"] in visible_nodes or a["node_id"] in scouted
            if not is_mine and not node_visible and a["state"] != "march":
                continue
            if not is_mine and a["state"] == "march" and not node_visible:
                continue
            fm = f_meta.get(a["faction_id"], {"name": "?", "color": "#888"})
            if is_mine or node_visible:
                afid = int(a["faction_id"])
                army_count_by_fid[afid] = army_count_by_fid.get(afid, 0) + 1
                army_troops_by_fid[afid] = army_troops_by_fid.get(afid, 0) + int(
                    a.get("troops", 0) or 0)
            on_road = False
            if is_mine and a.get("state") == "march" and a.get("path"):
                on_road = R.is_friendly_road(
                    node_map.get(int(a.get("node_id", 0) or 0)),
                    node_map.get(int(a["path"][0])), alliance_ids)
            # v4.3.0：敌军行军 path 只对自己可见——否则迷雾外/邻接敌军
            # 的完整路线可被前端直接读出，绕开战争迷雾。
            # 主将可能陨落/被俘：按名单顺延，不能盲取 characters[0]
            leader_name = ""
            for cid in a.get("characters") or []:
                lc = char_by_id.get(int(cid))
                if not lc or lc.get("status") in ("dead", "captive"):
                    continue
                if R.injury_state(lc.get("injury", 0)) in ("dying", "dead"):
                    continue
                leader_name = str(lc.get("name") or "")
                break
            out_armies.append({
                "id": a["id"], "name": a["name"], "faction": fm["name"],
                "faction_color": fm["color"], "mine": is_mine,
                "node_id": a["node_id"], "state": a["state"],
                "path": a["path"] if is_mine else [],
                "on_road": bool(on_road),
                "troops": a["troops"] if is_mine or
                node_visible else None,
                "troop_type": a["troop_type"], "morale": a["morale"] if is_mine else None,
                "veteran": a["veteran"] if is_mine else None,
                "grain": a["grain"] if is_mine else None,
                "leader": leader_name,
            })
        for fid, meta in f_meta.items():
            if int(fid) > 0:
                meta["armies"] = army_count_by_fid.get(int(fid), 0)
                meta["troops"] = army_troops_by_fid.get(int(fid), 0)
        # v4.6.1 天下视图：威胁榜 / 扩张倾向 / 对外关系 / 作战情况 / 风声
        winds_raw = (world.get("data") or {}).get("winds") or []
        npc_world = self._npc_world_view(world, f_meta, winds_raw)
        # 对外关系 / 性格只用于 npc_world 聚合，不随 factions 明细外发
        public_factions = [
            {k: v for k, v in meta.items()
             if k not in ("diplomacy", "personality")}
            for meta in f_meta.values()]
        state: dict[str, Any] = {
            "joined": bool(p),
            # v4.6.5：服务端时间基准，前端据此校准倒计时，
            # 避免玩家设备时钟偏差导致「下次推进」提前归零/停滞。
            "server_now": _now(),
            "world": {"id": wid, "season_no": world["season_no"],
                      "template_id": world["template_id"], "tick_no": world["tick_no"],
                      "last_tick_at": world["last_tick_at"],
                      "next_tick_at": world["last_tick_at"] + self.tick_seconds(),
                      "ends_at": world["ends_at"]},
            "nodes": out_nodes,
            "factions": sorted(public_factions, key=lambda x: -x["node_count"]),
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
            "winds": npc_world.get("winds") or [],
            "npc_world": npc_world,
            "start_candidates": (await self._starter_candidates(world)) if not p else [],
            "meta": self._frontend_meta(),
            "memorials": await self.get_memorials(wid),
            "transfers": await self.get_transfers_view(wid, p["faction_id"] if p else 0),
        }
        # v4.7.0：天下页「江湖纪事」首屏（按势力/联盟可见性过滤）
        viewer_fids: set[int] = set()
        if p and int(p.get("faction_id") or 0) > 0:
            viewer_fids.add(int(p["faction_id"]))
            wf = await self.get_faction(int(p["faction_id"]))
            if wf:
                try:
                    viewer_fids |= await self._alliance_ids(wid, wf)
                except Exception:  # noqa: BLE001 - 失败仅影响己方日志可见范围
                    pass
        log_page = await self.get_world_logs(wid, viewer_fids, limit=20)
        state["logs"] = log_page["items"]
        state["log_has_more"] = bool(log_page["has_more"])
        if p:
            state["player"] = await self._player_view(p, world, nodes)
            state["battles"] = await self._battle_list(
                wid, p["faction_id"], user_id=p["user_id"])
            f_ext = await self._governance_faction(p)
            own_f = await self.get_faction(int(p["faction_id"])) if p.get("faction_id") else None
            if f_ext:
                state["faction_ext"] = await self.get_faction_ext(
                    world, f_ext, own_f)
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
            out["note"] = ("补粮队在途" if int(payload.get("runner") or 0)
                           and not int(payload.get("delivered") or 0)
                           else "已注入军团")
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
        gov_node_by_char = {
            int(n.get("governor_char_id") or 0): n
            for n in nodes if int(n.get("governor_char_id") or 0) > 0}
        char_names: dict[int, str] = {}
        for c0 in await self.get_chars(world_id=world["id"]):
            char_names[c0["id"]] = c0["name"]
        growth_cap_bonus = self._growth_cap_bonus()
        lcfg = self._loyalty_cfg()
        reward_gain = max(1, int(lcfg.get("reward_gain", 5) or 5))
        reward_cost = max(0, int(lcfg.get("reward_cost_coin", 100) or 0))
        reward_cap = max(1, min(100, int(lcfg.get("reward_cap", 100) or 100)))
        reward_limit = max(0, int(lcfg.get("reward_daily_limit", 20) or 20))
        reward_used = self._loyalty_reward_used(world, p["user_id"])
        active_tasks = await self.get_tasks(world["id"])
        # 驻守口径（§5）：只有本地任务算驻守，远征/计略任务不算
        local_task_char_ids = {
            int(t["character_id"]) for t in active_tasks
            if str(t.get("task_type")) in R.TASK_LOCAL_TYPES}
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
                "personality": c.get("personality") or {},
                "personality_top_name": R.TOP_PERSONALITY_NAMES.get(
                    str((c.get("personality") or {}).get("top") or ""), ""),
                "personality_neg_name": R.NEGATIVE_PERSONALITY_NAMES.get(
                    str((c.get("personality") or {}).get("negative") or ""), ""),
                "personality_top_desc": R.TOP_PERSONALITY_DESC.get(
                    str((c.get("personality") or {}).get("top") or ""), ""),
                "personality_neg_desc": R.NEGATIVE_PERSONALITY_DESC.get(
                    str((c.get("personality") or {}).get("negative") or ""), ""),
                "aptitude": c["aptitude"], "level": c["level"], "exp": c["exp"],
                "exp_next": R.exp_to_next(c["level"]),
                "stat_total": R.stat_total(c),
                "stat_cap": R.stat_growth_cap(c, growth_cap_bonus),
                "loyalty": c["loyalty"],
                "can_reward": (
                    str(c.get("status") or "") not in
                    ("dead", "captive", "transfer", "wander")
                    and int(c["loyalty"]) < reward_cap),
                "reward_cost": reward_cost,
                "reward_gain": reward_gain,
                "injury": c["injury"],
                "injury_state": R.injury_state(c["injury"]),
                "injury_name": R.INJURY_STATE_NAMES.get(R.injury_state(c["injury"]), ""),
                "stamina": c["stamina"], "status": c["status"],
                "status_name": R.CHAR_STATUS_NAMES.get(
                    str(c.get("status") or ""), str(c.get("status") or "")),
                "resident": R.is_resident_char(c) and (
                    str(c.get("status") or "") != "task"
                    or int(c["id"]) in local_task_char_ids),
                "can_move": R.can_move_char(c),
                "location_node_id": int(
                    (c.get("location") or {}).get("node_id", 0) or 0),
                "appointed_position": str(c.get("appointed_position") or ""),
                "appointed_name": R.POSITION_NAMES.get(
                    str(c.get("appointed_position") or ""), "无官职"),
                "appointed_node_id": int(c.get("appointed_node_id") or 0),
                "appointed_node_name": node_names.get(
                    int(c.get("appointed_node_id") or 0), ""),
                "governor_node_id": int((gov_node_by_char.get(
                    int(c["id"])) or {}).get("id", 0) or 0),
                "governor_node_name": str((gov_node_by_char.get(
                    int(c["id"])) or {}).get("name", "") or ""),
                "bonds": {str(k): v for k, v in (c.get("bonds") or {}).items()},
                "location": node_names.get(
                    int((c.get("location") or {}).get("node_id", 0) or 0), ""),
                # 军团角色标记由下方 armies 循环回填（主将 / 随军）
                "army_role": "", "army_id": 0,
            })
        armies = []
        army_faction_cache: dict[int, Optional[dict]] = {}
        for a in await self.get_armies(world["id"], owner_user_id=p["user_id"]):
            afid = int(a.get("faction_id") or 0)
            if afid not in army_faction_cache:
                army_faction_cache[afid] = await self.get_faction(afid)
            leader, troop_cap = await self._army_effective_cap(
                a, army_faction_cache[afid])
            leader_pos = str(leader.get("appointed_position") or "") if leader else ""
            leader_paid = bool(
                leader and self._salary_role_paid(
                    army_faction_cache[afid], int(leader["id"]), leader_pos))
            leader_pay_state = (
                self._salary_role_state(
                    army_faction_cache[afid], int(leader["id"]), leader_pos)
                if leader else "paid")
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
                # 换将后上限/主将状态随名义主将实时刷新（升/降都对齐）
                "leader_id": int(leader["id"]) if leader else 0,
                "leader_name": str(leader.get("name") or "") if leader else "",
                "leader_position": str(
                    leader.get("appointed_position") or "") if leader else "",
                "leader_position_name": R.POSITION_NAMES.get(
                    str(leader.get("appointed_position") or ""), "无官职")
                if leader else "",
                "leader_position_paid": leader_paid,
                "leader_position_state": leader_pay_state,
                "troop_cap": int(troop_cap),
                "overstaff_limit": int(
                    (leader.get("stats") or {}).get("leadership", 0) or 0) * 20
                if leader else 0,
                "overstaffed": bool(R.is_overstaffed(
                    leader, int(a.get("troops", 0) or 0))) if leader else False,
                # v4.8.0 合军：联军归属与协同军团（成员仍独立结算）
                "coalition_id": int(a.get("coalition_id") or 0),
                "coalition_role": "", "coalition_partner": "",
                "coalition_status": "",
            })
        if any(int(av.get("coalition_id") or 0) > 0 for av in armies):
            co_by_id = {int(co["id"]): co for co in await self.get_coalitions(
                world["id"], owner_user_id=p["user_id"])}
            for av in armies:
                cid = int(av.get("coalition_id") or 0)
                co = co_by_id.get(cid)
                if not co:
                    continue
                partners = [
                    str(x.get("name") or "") for x in armies
                    if int(x.get("coalition_id") or 0) == cid
                    and int(x["id"]) != int(av["id"])]
                av["coalition_role"] = (
                    "leader" if int(co.get("leader_army_id") or 0) == int(av["id"])
                    else "member")
                av["coalition_partner"] = "、".join(n for n in partners if n)
                av["coalition_status"] = str(co.get("status") or "idle")
        # 角色卡与任务派遣列表共用：把军团主将标成「主将」，随行保持「随军」
        char_army_role: dict[int, tuple[str, int]] = {}
        for av in armies:
            lid = int(av.get("leader_id") or 0)
            for cid in av.get("character_ids") or []:
                char_army_role[int(cid)] = (
                    "leader" if int(cid) == lid else "member", int(av["id"]))
        for cv in chars:
            info = char_army_role.get(int(cv["id"]))
            if not info:
                continue
            cv["army_role"], cv["army_id"] = info
            if info[0] == "leader" and str(cv.get("status") or "") == "army":
                cv["status_name"] = "主将"
        # v4.6.6：持续任务的小时边界由 world tick 触发结算（_step_settle_tasks
        # 只在 tick 管线内执行），边界到 tick 之间最长会空转一个 tick 周期。
        # 这里把边界向上取整到首个不早于它的推进时刻再下发，前端据此倒计时，
        # 避免「已到点、tick 未到」期间任务倒计时长期停在 0s。
        tick_sec = max(1, self.tick_seconds())
        next_tick_at = int(world.get("last_tick_at") or 0) + tick_sec

        def _expect_settle(next_settle_at: int) -> int:
            if next_settle_at <= 0 or next_tick_at <= 0:
                return next_settle_at
            steps = max(0, (next_settle_at - next_tick_at + tick_sec - 1) // tick_sec)
            return next_tick_at + steps * tick_sec

        tasks = []
        for t in active_tasks:
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
                "started_at": int(t.get("start_at", 0) or 0),
                # 所在地机制：出发地 / 持续任务进度（普通任务每 h 结算）
                "origin_node_id": int(payload.get("origin_node_id") or 0),
                "origin_node_name": node_names.get(
                    int(payload.get("origin_node_id") or 0), ""),
                "expedition": bool(R.task_is_expedition(tt)),
                "sustained": bool(int(payload.get("sustained") or 0)),
                "next_settle_at": int(payload.get("next_settle_at") or 0),
                "expect_settle_at": _expect_settle(
                    int(payload.get("next_settle_at") or 0)),
                # v4.6.9：固定时长任务（计略/侦察/劫掠等）结算同样只在 world
                # tick 内执行，展示倒计时对齐到实际结算 tick，前端每秒刷新。
                "expect_end_at": _expect_settle(int(t["end_at"] or 0)),
                "settled_hours": int(payload.get("settled_hours") or 0),
                **preview,
            })
        tavern = p.get("tavern") or {}
        # P1：势力详情（成员/国库/科技/外交，§4/§6.3/§8.4）
        # 结盟后保留自身势力：名册/官职/外交读取盟主，国库/科技用本势力。
        f = await self.get_faction(p["faction_id"])
        host = await self._alliance_host(f) if f else None
        members = await self.get_members(host["id"]) if host else []
        # 本势力名册：科技/国库按势力独立结算，职称门禁也看自身名册
        # （联盟盟主调整共同名册不应影响成员势力主研究自家科技）。
        own_members = (members if f and host and int(host["id"]) == int(f["id"])
                       else await self.get_members(f["id"]) if f else [])
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
            alliance_factions = await self._alliance_factions(world["id"], host)
            af_by_id = {int(af["id"]): af for af in alliance_factions}
            allied = []
            for af in alliance_factions:
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
                         c.get("status") not in ("dead", "captive", "transfer")]
            gov_node_by_char = {
                int(n.get("governor_char_id") or 0): n
                for n in nodes
                if int(n.get("governor_char_id") or 0) > 0 and
                int(n.get("owner_faction") or 0) in gov_ids}
            avail = R.available_positions(gov_nodes)
            salary_cfg = (self.main_cfg().get("faction") or {}).get("salary") or {}
            salary_per = salary_cfg.get("per_tick") or {}
            own_salary = f.get("salary") or {}
            unpaid_gov_nodes: set[int] = set()
            for af in alliance_factions:
                unpaid_gov_nodes.update(
                    int(x) for x in (((af.get("salary") or {}).get(
                        "unpaid_governor_node_ids")) or []))
            appointable = {
                pos: {
                    "name": R.POSITION_NAMES.get(pos, pos),
                    "cap": int(cap),
                    "used": sum(1 for c in gov_chars
                                if str(c.get("appointed_position") or "") == pos),
                    "effect": R.POSITION_EFFECT_TEXT.get(pos, ""),
                    "unlock_nodes": R.position_unlock_nodes(pos),
                    "troop_cap": R.POSITION_TROOP_CAP.get(pos, 1500),
                    "salary_per_tick": int(salary_per.get(pos, 0) or 0),
                }
                for pos, cap in avail.items()}
            appointments = []
            for c in gov_chars:
                pos = str(c.get("appointed_position") or "")
                if not pos:
                    continue
                owner_f = af_by_id.get(int(c.get("faction_id") or 0)) or f
                appointments.append({
                    "char_id": c["id"], "name": c["name"],
                    "owner_user_id": c.get("owner_user_id", ""),
                    "position": pos,
                    "position_name": R.POSITION_NAMES.get(pos, "无官职"),
                    # v4.6.7：区分「待发薪（任免后等下一 tick）」与「欠俸（钱包不足）」
                    "salary_state": self._salary_role_state(
                        owner_f, int(c["id"]), pos),
                    "salary_paid": self._salary_role_paid(
                        owner_f, int(c["id"]), pos),
                    "salary_per_tick": int(salary_per.get(pos, 0) or 0)})
            appoint_chars = [
                {"id": c["id"], "name": c["name"],
                 "owner_user_id": c.get("owner_user_id", ""),
                 "role_name": R.ROLE_NAMES.get(c.get("role", ""), c.get("role", "")),
                 "status": str(c.get("status") or ""),
                 "position": str(c.get("appointed_position") or ""),
                 "governor_node_id": int((gov_node_by_char.get(
                     int(c["id"])) or {}).get("id", 0) or 0),
                 "governor_node_name": str((gov_node_by_char.get(
                     int(c["id"])) or {}).get("name", "") or ""),
                 "governor_salary_paid": int((gov_node_by_char.get(
                     int(c["id"])) or {}).get("id", 0) or 0) not in unpaid_gov_nodes}
                for c in gov_chars]
            faction_view = {
                "id": f["id"], "name": f["name"], "kind": f["kind"],
                "level": f["level"], "reputation": f["reputation"],
                "capital_node_id": int(f.get("capital_node_id", 0) or 0),
                "node_count": counts.get(int(f["id"]), 0),
                # v4.5.7：玩家势力无国库（开销统一走钱包）；科技仍各势力独立。
                "tech": f.get("tech") or {},
                # 联盟不共享国库：只展示本势力自身发薪结果。
                "salary": own_salary,
                "salary_summary": {
                    "due": int(own_salary.get("due", 0) or 0),
                    "paid": int(own_salary.get("paid", 0) or 0),
                    "short": int(own_salary.get("short", 0) or 0),
                    "unpaid_positions": sorted(
                        own_salary.get("unpaid_positions") or []),
                },
                "salary_per_tick": {str(k): int(v or 0)
                                    for k, v in salary_per.items()},
                "salary_enabled": bool(salary_cfg.get(
                    "enabled", (self.main_cfg().get("faction") or {}).get(
                        "salary_enabled", True))),
                "economy_shared": False,
                "member_cap": int(f.get("member_cap", 10)),
                "members": [{"user_id": m["user_id"],
                             "position": m.get("position", ""),
                             "faction_id": pmap.get(str(m["user_id"]), int(f["id"])),
                             "contribution": int(m.get("contribution", 0) or 0)}
                            for m in members],
                "my_position": self._position_of(members, p["user_id"]),
                # 本势力内的官职：科技/经济等「各势力独立」操作使用；
                # my_position 仍是联盟共同治理身份，两者不可混用。
                "own_position": self._position_of(own_members, p["user_id"]),
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
            1 for c in chars
            if c.get("status") not in ("dead", "captive", "transfer"))
        return {
            "user_id": p["user_id"], "faction_id": p["faction_id"],
            "coin": p["coin"], "grain": p["grain"], "iron": p["iron"],
            "recruit_token": p["recruit_token"], "merit": p["merit"],
            "char_cap": int(cap_info["cap"]),
            "char_count": int(roster_count),
            "char_cap_nodes": int(cap_info["nodes"]),
            "char_cap_per_node": int(cap_info["per_node"]),
            "growth_cap_bonus": int(growth_cap_bonus),
            "loyalty_reward": {
                "gain": reward_gain, "cost_coin": reward_cost,
                "cap": reward_cap, "daily_limit": reward_limit,
                "daily_used": reward_used,
                "daily_left": (max(0, reward_limit - reward_used)
                               if reward_limit > 0 else -1),
                "natural_gain_per_tick": int(
                    lcfg.get("natural_gain_per_tick", 1) or 0),
                "natural_cap": int(lcfg.get("natural_cap", 80) or 80),
                "appointed_bonus_per_tick": int(
                    lcfg.get("appointed_bonus_per_tick", 1) or 0),
                "appointed_natural_cap": int(
                    lcfg.get("appointed_natural_cap", 90) or 90),
            },
            "honor": self._honor_view(p),
            "notify_pref": p.get("notify_pref") or {"level": 1},
            "chars": chars, "armies": armies, "tasks": tasks,
            "tavern": {"candidates": tavern.get("candidates") or [],
                      "refresh_at": tavern.get("refresh_at", 0),
                      "roll_seq": int(tavern.get("roll_seq", 0) or 0),
                      "daily_refresh_used": int(tavern.get("daily_refresh_used", 0))},
            "intel": int(tavern.get("intel", 0) or 0),
            "achievements": p.get("achievements") or {},
            "newcomer": p.get("newcomer") or {},
            "tutorial": p.get("tutorial") or {},
            "faction": faction_view,
            "faction_invite": ((world.get("data") or {}).get("join_invites") or {})
            .get(p["user_id"]),
            "captives": await self.get_captives_view(
                world["id"], p["faction_id"], p["user_id"], world=world),
            "market_orders": await self.market_orders(p["user_id"]),
            "market_offers": await self.market_offers(p["user_id"]),
            "convoys": await self.get_convoys_view(p["user_id"]),
            "duels": await self.duel_arena_list(p["user_id"]),
        }

    @staticmethod
    def _battle_has_owner(battle: dict, user_id: str) -> bool:
        if not user_id:
            return False
        owners = (battle.get("session") or {}).get("owners") or {}
        return any(str(v.get("owner") or "") == str(user_id)
                   for v in owners.values())

    async def _battle_list(self, world_id: int, faction_id: int,
                           limit: int = 20, user_id: str = "") -> list[dict]:
        # 进行中的手操战役必须置顶且不受战报条数上限影响：世界 tick 一次
        # 可能产生几十场自动战报，若只取最新 limit 条，部署窗口内的手操
        # 战役可能被挤出战报页，玩家既收不到前端轮询提示也无法点击进入。
        # 先取全世界的进行中战役再按「参战势力或拥有参战军团」过滤：
        # 协防盟友据点的玩家势力号可能与攻守方都不同，只看 faction 会漏。
        active_rows = await self._fetchall(
            "SELECT * FROM strategy_battles WHERE world_id=? AND mode='manual' "
            "AND state IN ('deploy','battle') "
            "ORDER BY deadline_at ASC, id ASC", (int(world_id),))
        active = []
        for r in active_rows:
            b = self._decode("strategy_battles", r)
            if int(b.get("attacker_faction", 0)) == int(faction_id) or \
                    int(b.get("defender_faction", 0)) == int(faction_id) or \
                    self._battle_has_owner(b, user_id):
                active.append(b)
        active_ids = {int(b["id"]) for b in active}
        out = []
        for b in active:
            out.append(self._battle_view(b, user_id))
        done = 0
        for b in await self.get_battles(world_id, limit=max(int(limit), 1),
                                        faction_id=faction_id):
            if int(b["id"]) in active_ids:
                continue
            if done >= int(limit):
                break
            done += 1
            out.append(self._battle_view(b, user_id))
        return out

    def _battle_view(self, b: dict, user_id: str = "") -> dict:
        res = b.get("result") or {}
        sess = b.get("session") or {}
        live = str(b.get("mode")) == "manual" and \
            str(b.get("state")) in ("deploy", "battle")
        my_uids: set[int] = set()
        cur_uid = 0
        if live and user_id:
            for uid_s, v in (sess.get("owners") or {}).items():
                if str(v.get("owner") or "") != str(user_id):
                    continue
                try:
                    my_uids.add(int(uid_s))
                except (TypeError, ValueError):
                    continue
            if sess.get("phase") == "battle":
                cur = H.current_actor(sess)
                try:
                    cur_uid = int((cur or {}).get("uid", 0) or 0)
                except (TypeError, ValueError):
                    cur_uid = 0
        my_turn = bool(cur_uid and cur_uid in my_uids and
                       not (sess.get("delegate") or {}).get(str(cur_uid)))
        return {
            "id": b["id"], "node_id": b["node_id"], "kind": b["kind"],
            "mode": b.get("mode", "auto"), "state": b.get("state", "done"),
            "deadline_at": int(b.get("deadline_at", 0) or 0),
            "attacker_faction": b["attacker_faction"],
            "defender_faction": b["defender_faction"],
            "winner": res.get("winner"), "rounds": b["round_no"],
            "report_text": b["report_text"], "created_at": b["created_at"],
            "highlights": res.get("highlights") or [],
            # 手操战役的当前行动方/是否轮到我：战略页无 WS，靠轮询做
            # 回合提醒（存在手操战役时前端会缩短到 5s）；这些字段只对参战方下发。
            "current_actor": cur_uid or None,
            "my_turn": my_turn,
            "turn_round": int(sess.get("round", 0) or 0) if live else 0,
            "turn_deadline": int(sess.get("turn_deadline", 0) or 0) if live else 0,
        }

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
                int(b.get("defender_faction", 0)) != int(p["faction_id"]) and \
                not self._battle_has_owner(b, user_id):
            raise StrategyError("无权查看该战报（仅参战方可见）")
        return {"id": b["id"], "node_id": b["node_id"], "kind": b["kind"],
                "winner": (b.get("result") or {}).get("winner"),
                "report_text": b["report_text"], "result": b.get("result"),
                "created_at": b["created_at"]}

    def _task_meta(self) -> dict:
        """任务选择提示：核心属性 / 最佳职责 / 产出 / 据点加成。"""
        node_bonus = {"govern": "仙城 ×1.5", "farm": "灵谷田 ×2.0",
                      "mining": "灵脉矿 ×2.0 / 关隘 ×1.3",
                      "trade": "坊市 ×1.3", "seek": "分舵 ×1.4",
                      "build": "仙城 ×1.2"}
        meta = {}
        for k in ("govern", "farm", "mining", "trade", "seek", "scout", "build",
                  "train", "hunt", "raid", "heal", "research"):
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
                "category": ("expedition" if R.task_is_expedition(k)
                             else "local"),
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
                            "build", "train", "hunt", "raid", "heal", "research")},
            "task_meta": self._task_meta(),
            "task_hours": (cfg.get("task") or {}).get("hours_options", [1, 2, 4, 8]),
            "rarity_names": dict(R.RARITY_NAMES),
            "role_names": dict(R.ROLE_NAMES),
            "profession_names": dict(R.PROFESSION_NAMES),
            "trait_names": dict(R.TRAIT_NAMES),
            "trait_descs": dict(R.TRAIT_DESC),
            "personality_top_names": dict(R.TOP_PERSONALITY_NAMES),
            "personality_neg_names": dict(R.NEGATIVE_PERSONALITY_NAMES),
            "personality_top_descs": dict(R.TOP_PERSONALITY_DESC),
            "personality_neg_descs": dict(R.NEGATIVE_PERSONALITY_DESC),
            "world_log_kinds": {
                str(k): {"icon": v[0], "name": v[1]}
                for k, v in R.WORLD_LOG_KINDS.items()},
            "stat_names": dict(R.STAT_NAMES),
            "resource_names": dict(R.RESOURCE_NAMES),
            "tech_names": dict(R.TECH_NAMES),
            "char_status_names": dict(R.CHAR_STATUS_NAMES),
            "army_state_names": dict(R.ARMY_STATE_NAMES),
            "arena_bracket_names": dict(R.ARENA_BRACKET_NAMES),
            "injury_names": dict(R.INJURY_STATE_NAMES),
            "node_output": cfg.get("node_output_per_tick") or {},
            "gold_output": {k: v for k, v in (cfg.get("gold_output") or {}).items()},
            "positions": {k or "none": {"name": v, "cap": R.POSITION_TROOP_CAP.get(k, 1500)}
                          for k, v in R.POSITION_NAMES.items()},
            "appointable_positions": {
                p: {"name": R.POSITION_NAMES.get(p, p),
                    "effect": R.POSITION_EFFECT_TEXT.get(p, ""),
                    "unlock_nodes": R.position_unlock_nodes(p),
                    "troop_cap": R.POSITION_TROOP_CAP.get(p, 1500),
                    "salary_per_tick": int(((cfg.get("faction") or {}).get(
                        "salary") or {}).get("per_tick", {}).get(p, 0) or 0)}
                for p in R.APPOINTABLE_POSITIONS},
            "position_unlocks": [
                {"nodes": int(nodes), "positions": dict(caps)}
                for nodes, caps in R.POSITION_UNLOCKS],
            # --- P1~P3 元数据 ---
            "plot_types": dict(R.PLOT_NAMES),
            "tech_lines": list(R.TECH_LINES),
            "tech": cfg.get("tech") or {},
            "tech_tree": R.tech_tree_view(cfg.get("tech") or {}),
            "territory": cfg.get("territory") or {},
            "achievements": {k: {"name": v.get("name", k),
                                 "desc": v.get("desc", ""),
                                 "merit": int(v.get("merit", 0))}
                             for k, v in (cfg.get("achievements") or {}).items()},
            "faction_cfg": cfg.get("faction") or {},
            "alliance_cfg": cfg.get("alliance") or {},
            "node_types": {"city": "仙城", "pass": "关隘", "branch": "分舵",
                           "market": "坊市", "grain": "灵谷田", "mine": "灵脉矿",
                           "ruin": "遗迹"},
            "diplomacy_cfg": cfg.get("diplomacy") or {},
            "favor_cfg": (cfg.get("diplomacy") or {}).get("favor") or {},
            "captive_cfg": cfg.get("captive") or {},
            "duel_cfg": cfg.get("duel") or {},
            "hex_cfg": cfg.get("hex") or {},
            "recruit_cfg": cfg.get("recruit") or {},
            "coalition_cfg": cfg.get("coalition") or {},
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
            # v81 所在地机制：传送/驻守/潜力/守军/城镇升级
            "location_cfg": {
                "move_block_statuses": list(R.MOVE_BLOCK_STATUSES),
                "resident_statuses": list(R.RESIDENT_STATUSES),
                "dwell_node_types": list(R.CHAR_DWELL_NODE_TYPES),
                "local_task_types": list(R.TASK_LOCAL_TYPES),
                "expedition_task_types": list(R.TASK_EXPEDITION_TYPES),
            },
            "node_potential": dict(R.NODE_POTENTIAL_BY_TYPE),
            "garrison_cfg": cfg.get("garrison")
            or {"grow_pct": 0.02, "cap_pct": 0.05},
            "city_cfg": cfg.get("city")
            or {"upgrade_base_coin": 5000, "upgrade_base_iron": 500},
        }

    # ------------------------------------------------------------------
    # 动作分发（REST → manager）
    # ------------------------------------------------------------------
    #: 需要审计的关键动作（§18.4）
    _AUDIT_ACTIONS = {
        "enter", "faction_create", "faction_quit", "faction_set_position",
        "rebuild_capital", "capital_relocate",
        "mobilization_issue", "transfer_captive", "transfer_node",
        "govern_relief", "appoint_taishou", "dismiss_taishou",
        "upgrade_building", "create_army", "raid_bandit",
        "handover_army",
        # v4.8.0：酒馆招募（记录 roll_seq/候选身份，便于回溯错位）
        "recruit", "form_coalition", "disband_coalition",
        "dispatch_plot", "market_list", "market_buy", "persuade",
        # v4.3.0 补全：此前 11 个敏感动作落库无审计
        "faction_task_claim", "faction_task_refresh", "rebuild_donate",
        "arena_register", "diplomacy_propose", "diplomacy_respond",
        "release", "execute_captive", "sell_captive",
        "ransom_set", "ransom_pay", "market_cancel",
        "market_offer_accept", "market_offer_decline",
        "faction_invite",
        # v81 所在地机制：显式传送 / 大牢正式招募
        "move_char", "recruit_captive",
        # v4.5.8：忠诚奖赏
        "reward_char", "reward_all",
    }
    #: 只读预览动作：不占限速额度、不标活跃、不包事务（前端输入实时调用）
    _READONLY_ACTIONS = {"army_cost_preview", "army_cap_preview"}

    async def handle_action(self, user_id: str, action: str,
                            params: Optional[dict] = None) -> dict:
        """动作统一入口（REST 漏斗）。

        A-M7：全程持 _action_lock 串行化玩家动作，防并发「读-改-写」互相
        覆盖丢更新（钱包/军团/互市等）；锁序恒为 _action_lock → _world_lock
        （tick/poll 同序，GM 重置赛季只拿 _world_lock，无死锁）。
        tick 持 _world_lock 期间照旧 Busy 拒绝（保持 REST 423 语义）。
        """
        # 只读预览：不占限速额度、不标活跃、不开事务
        if action in self._READONLY_ACTIONS:
            if self._world_lock.locked():
                raise StrategyBusyError("世界推进中，请稍后再试")
            return await self._handle_action(user_id, action, params)
        # §18.4 指令限速（滑动 60s 窗口，超限直接拒绝）
        self._check_action_rate(user_id)
        # 玩家正在操作：记录活跃时间，供野战遭遇判断是否开手操窗口。
        self._recent_players[str(user_id)] = _now()
        if self._world_lock.locked():
            raise StrategyBusyError("世界推进中，请稍后再试")
        async with self._action_lock:
            if self._world_lock.locked():
                raise StrategyBusyError("世界推进中，请稍后再试")
            audit = action in self._AUDIT_ACTIONS
            detail = {k: v for k, v in (params or {}).items()
                      if k not in ("password", "token")}
            # v4.3.0：整个动作用 BEGIN IMMEDIATE 包住——autocommit 下每条
            # 语句即刻落盘，此前异常路径的 rollback() 是空操作，半更新会被
            # 后续任意提交「顺带」落库（如 recruit 先得角色后扣币，扣币失败
            # 角色却保留）。事务内所有 _commit() 经 _tx_task 标记延迟，
            # 成功统一提交、异常整体回滚；与 tick 同用数据层事务状态机，
            # 嵌套时自动降级 SAVEPOINT。
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
            except Exception:  # noqa: BLE001
                # 与数据层 _begin 同语义：残留事务先提交结束（而非回滚，
                # 否则会吞掉调用方此前未提交的合法写入），再开新事务
                await self.db.conn.commit()
                await self.db.conn.execute("BEGIN IMMEDIATE")
            prev_tx = self._tx_task
            self._tx_task = asyncio.current_task()
            try:
                result = await self._handle_action(user_id, action, params)
            except Exception as exc:  # noqa: BLE001 - 失败也留痕后原样抛出
                self._tx_task = prev_tx
                try:
                    await self.db.conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                # 审计在回滚后单独写入（autocommit 立即落盘），不被回滚吃掉
                if audit:
                    await self._audit(user_id, action,
                                      {**detail, "ok": False, "error": str(exc)})
                    try:
                        await self._commit()
                    except Exception:  # noqa: BLE001
                        pass
                raise
            self._tx_task = prev_tx
            try:
                await self.db.conn.commit()
            except Exception:  # noqa: BLE001
                # 提交失败视同动作未生效：回滚残留并报错，绝不返回「成功」
                try:
                    await self.db.conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                raise StrategyError("操作落库失败，请重试")
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
            if action == "army_cost_preview":
                return await self.preview_army_cost(
                    user_id, int(params.get("leader_char_id", 0) or 0),
                    int(params.get("node_id", 0) or 0),
                    str(params.get("troop_type", "sword_shield")),
                    int(params.get("troops", 0) or 0),
                    siege_engine=bool(params.get("siege_engine")))
            world, p = await self._require_player(user_id)
            wid = world["id"]
            if action == "refresh_tavern":
                self._ensure_joined(p)
                await self.refresh_tavern(p, wid, manual=bool(params.get("manual")))
                return {"ok": True, "msg": "酒馆已刷新"}
            if action == "recruit":
                char = await self.recruit(
                    p, wid, int(params.get("index", 0)),
                    premium=bool(params.get("premium")),
                    expected_roll=int(params.get("roll", 0) or 0),
                    expected_name=str(params.get("name") or ""))
                if not char.get("ok", True):
                    # v4.8.0：候选列表已在点击瞬间刷新 → 拒绝本次招募（不扣费）
                    return char
                unit = "募将令" if char.get("recruit_currency") == "token" else "铜钱"
                msg = (f"招募成功：{char['name']}"
                       f"（{R.RARITY_NAMES.get(char['rarity'])}·"
                       f"{R.ROLE_NAMES.get(char['role'], char['role'])}）")
                if int(char.get("recruit_cost", 0) or 0) > 0:
                    msg += f"，消耗 {int(char['recruit_cost'])} {unit}"
                return {"ok": True, "msg": msg, "char": char}
            if action == "form_coalition":
                return await self.form_coalition(
                    user_id, int(params.get("leader_army_id", 0) or 0),
                    int(params.get("member_army_id", 0) or 0))
            if action == "disband_coalition":
                return await self.disband_coalition(
                    user_id, int(params.get("army_id", 0) or 0))
            if action == "dismiss_char":
                return await self.dismiss_char(user_id, int(params["char_id"]))
            if action == "reward_char":
                return await self.reward_char(user_id, int(params["char_id"]))
            if action == "reward_all":
                return await self.reward_all_chars(user_id)
            if action == "move_char":
                return await self.move_char(
                    user_id, int(params["char_id"]),
                    _as_int(params.get("node_id"), "node_id"))
            if action == "recruit_captive":
                return await self.recruit_captive(
                    user_id, int(params["char_id"]),
                    int(params.get("node_id", 0) or 0))
            if action == "dispatch_task":
                return await self.dispatch_task(
                    user_id, int(params.get("char_id", 0) or 0),
                    int(params["node_id"]),
                    str(params.get("task_type", "farm")), int(params.get("hours", 1)),
                    int(params.get("target_node_id", 0) or 0),
                    int(params.get("partner_char_id", 0) or 0),
                    int(params.get("army_id", 0) or 0))
            if action == "recall_task":
                return await self.recall_task(user_id, int(params["task_id"]))
            if action == "create_army":
                return await self.create_army(
                    user_id, int(params["leader_char_id"]),
                    [int(x) for x in (params.get("member_ids") or [])],
                    int(params["node_id"]), str(params.get("troop_type", "sword_shield")),
                    int(params.get("troops", 100)),
                    siege_engine=bool(params.get("siege_engine")))
            if action == "army_cap_preview":
                return await self.preview_army_cap(
                    user_id, int(params["army_id"]),
                    int(params.get("leader_char_id", 0) or 0))
            if action == "disband_army":
                return await self.disband_army(user_id, int(params["army_id"]))
            if action == "handover_army":
                return await self.handover_army(
                    user_id, int(params["army_id"]),
                    int(params.get("leader_char_id", 0) or 0),
                    params.get("member_ids") if "member_ids" in params else None)
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
            if action == "appoint_taishou":
                return await self.appoint_taishou(
                    user_id, int(params["node_id"]), int(params["char_id"]))
            if action == "dismiss_taishou":
                return await self.dismiss_taishou(
                    user_id, int(params["node_id"]))
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
            if action == "tech_research":
                return await self.tech_research(
                    user_id, str(params["line"]),
                    int(params.get("char_id", 0) or 0),
                    str(params.get("node_id", "") or ""))
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
            if action == "execute_captive":
                return await self.execute_captive(
                    user_id, int(params["char_id"]))
            if action == "sell_captive":
                return await self.sell_captive(
                    user_id, int(params["char_id"]))
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
            if action == "market_offer_accept":
                return await self.market_offer_accept(
                    user_id, int(params["offer_id"]))
            if action == "market_offer_decline":
                return await self.market_offer_decline(
                    user_id, int(params["offer_id"]))
            if action == "raid_bandit":
                return await self.raid_bandit(user_id, int(params["army_id"]),
                                              int(params.get("node_id", 0)))
            if action == "send_convoy":
                return await self.send_convoy(
                    user_id, int(params["from_node"]), int(params["to_node"]),
                    int(params["grain"]), int(params.get("escort_army_id", 0)),
                    int(params.get("escort_troops", 0) or 0),
                    str(params.get("escort_troop_type") or ""))
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





