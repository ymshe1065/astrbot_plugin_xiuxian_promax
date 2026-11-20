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
    _ensure_strategy_v74, _ensure_strategy_v75,
)
from .strategy_errors import StrategyBusyError, StrategyError
from .strategy_faction_mixin import StrategyFactionMixin
from .strategy_captive_mixin import StrategyCaptiveMixin
from .strategy_plot_mixin import StrategyPlotMixin
from .strategy_battle_mixin import StrategyBattleMixin
from .strategy_p3_mixin import StrategyP3Mixin

logger = logging.getLogger("astrbot")

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")


def _now() -> int:
    return int(time.time())


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

    # ------------------------------------------------------------------
    # 通知分级推送（§12.5：strategy.notify，三档 notify_pref）
    # ------------------------------------------------------------------
    #: kind → 触发所需最低通知档位（0 仅手操窗口 / 1 +己方被攻 / 2 全部事件）
    _NOTIFY_MIN_LEVEL = {
        "battle_open": 0,
        "duel_invite": 0,
        "siege_alert": 1,
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

    async def _commit(self):
        await self.db.conn.commit()

    async def ensure_tables(self):
        """确保 strategy_* 表存在（测试库/旧库兜底，IF NOT EXISTS 幂等；就绪后跳过）。"""
        if getattr(self, "_tables_ready", False):
            return
        await _ensure_strategy_v70(self.db.conn)
        await _ensure_strategy_v71(self.db.conn)
        await _ensure_strategy_v72(self.db.conn)
        await _ensure_strategy_v74(self.db.conn)
        await _ensure_strategy_v75(self.db.conn)
        await self._commit()
        self._tables_ready = True

    # ------------------------------------------------------------------
    # 行编解码（JSON 列 ↔ dict）
    # ------------------------------------------------------------------
    _JSON_COLS = {
        "strategy_nodes": ("buildings_json", "stock_json", "output_json", "adj_json",
                           "siege_state_json", "npc_garrison_json", "contribution_json"),
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
                protect_until=?, contribution_json=?
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
            f.get("name", ""), f.get("color", "#888888"), f.get("kind", "npc"),
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
                appointed_position=?, appointed_node_id=?
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
            int(c["id"]),
        ))

    async def insert_char(self, c: dict) -> int:
        cid = await self._execute("""
            INSERT INTO strategy_characters (world_id, faction_id, owner_user_id, name, role,
                rarity, profession, stats_json, traits_json, aptitude_json, exp, level,
                loyalty, injury, stamina, status, appointed_position, appointed_node_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                siege_engine=?, reorganize_until_tick=?
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
            json.dumps(b.get("result") or {}, ensure_ascii=False),
            b.get("report_text", ""), int(b.get("created_at", _now())),
        ))

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
        for i, pers_key in enumerate(chosen):
            pers = dict((npc_names.get("personalities") or {}).get(pers_key) or {})
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
        """中郎将（角色官职）提供的招募高品质概率加成。"""
        f = await self.get_faction(int(faction_id or 0))
        if not f:
            return 0.0
        host = await self._alliance_host(f) or f
        bonus = await self._faction_position_bonus(host)
        return float(bonus.get("recruit_quality", 0.0))

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
                     now: int, salt: int = 0, quality_bonus: float = 0.0) -> dict:
        # salt=0：自动刷新按小时粒度稳定候选；手动刷新传精确时间戳，
        # 保证同小时内多次付费刷新得到不同候选
        rng = random.Random(int(hashlib.md5(
            f"{user_id}|{world_id}|{now // 3600}|{salt}".encode("utf-8")).hexdigest()[:8], 16))
        n = int(cfg.get("candidates", 5))
        role_keys = list((cfg.get("role_weights") or {}).keys()) or list(R.ROLES)
        role_w = [int((cfg.get("role_weights") or {}).get(k, 1)) for k in role_keys]
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
        cap = int(cfg.get("character_cap_base", 10)) + \
            int(cfg.get("character_cap_per_level", 3)) * 0  # P0：势力等级恒 1
        owned = await self.get_chars(owner_user_id=player["user_id"], world_id=world_id)
        if len(owned) >= cap:
            raise StrategyError(f"角色数量已达上限（{cap}）")
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
        cands.pop(int(index))
        tavern["candidates"] = cands
        player["tavern"] = tavern
        await self.save_player(player)
        await self._commit()
        return char

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
                if player is not None:
                    player.gold = int(player.gold or 0) + allow
                    await self.db.update_player(player)
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
        return R.troop_cap(pos, is_taishou_outside=outside)

    # ------------------------------------------------------------------
    # 任务（§6.2）
    # ------------------------------------------------------------------
    async def dispatch_task(self, user_id: str, char_id: int, node_id: int,
                            task_type: str, hours: int) -> dict:
        world, p = await self._require_player(user_id)
        tcfg = self.main_cfg().get("task") or {}
        scfg = self.main_cfg().get("stamina") or {}
        if task_type == "quest":
            task_type = "seek"
        if task_type not in R.TASK_TYPES or task_type in ("raid", "supply"):
            raise StrategyError(f"P0 暂不支持该任务：{task_type}")
        if int(hours) not in [int(h) for h in tcfg.get("hours_options", [1, 2, 4, 8])]:
            raise StrategyError("时长须为 1/2/4/8 小时")
        c = await self._own_char(p, char_id)
        if c["status"] != "idle":
            raise StrategyError(f"{c['name']} 状态为「{c['status']}」，无法派遣")
        if R.injury_state(c["injury"]) in ("dying", "dead"):
            raise StrategyError(f"{c['name']} 伤势过重")
        if scfg.get("enabled", True) and c["stamina"] < int(scfg.get("task_cost", 20)):
            raise StrategyError(f"{c['name']} 体力不足（需 {scfg.get('task_cost', 20)}）")
        node = await self._owned_node(p, node_id)
        if R.spend_stamina(c, "task", bool(scfg.get("enabled", True))):
            pass
        c["status"] = "task"
        c["location"] = {"node_id": node["id"]}
        await self.save_char(c)
        now = _now()
        ticks = max(1, int(hours) * int(tcfg.get("ticks_per_hour", 4)))
        tid = await self._execute("""
            INSERT INTO strategy_tasks (world_id, faction_id, character_id, node_id,
                task_type, start_at, end_at, payout_json, status)
            VALUES (?,?,?,?,?,?,?,?, 'active')
        """, (world["id"], p["faction_id"], c["id"], node["id"], task_type,
              now, now + int(hours) * 3600, json.dumps({"ticks": ticks})))
        await self._commit()
        return {"ok": True, "msg": f"{c['name']} 开始在「{node['name']}」执行"
                                   f"{R.TASK_NAMES.get(task_type, task_type)}（{hours}小时）",
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
        # 商路加成（§5.5）：trade 任务节点位于活跃商路上 → +30%
        if task["task_type"] == "trade" and node:
            trcfg = self.main_cfg().get("trade_route") or {}
            if int(node["id"]) in await self._trade_route_node_ids(world):
                amount = int(amount * (1 + float(trcfg.get("trade_task_bonus", 0.3))))
        out = {"kind": R.TASK_OUTPUT_RES.get(task["task_type"], "coin")}
        kind = out["kind"]
        ach_calls: list[tuple[str, int]] = []
        if kind == "coin":
            p["coin"] += amount
        elif kind == "grain":
            p["grain"] += amount
            if task["task_type"] == "farm":
                ach_calls.append(("civil_farm_100k", amount))
        elif kind == "token":
            tokens = max(0, int(amount // 300))  # 寻访：300 收益 = 1 募将令
            p["recruit_token"] += tokens
            out["amount"] = tokens
        elif kind == "merit":
            merit = max(0, int(amount // 10))
            # 魔潮期讨伐战功 ×2（hunt_merit_mult，§11.1）
            if task["task_type"] == "hunt":
                merit = int(merit * await self._hunt_merit_mult(world))
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
        else:  # loot / grain_move → P0 折半转币
            p["coin"] += amount // 2
            out["amount"] = amount // 2
        if kind in ("coin", "grain"):
            out["amount"] = amount
        # 角色阅历
        logs = R.gain_exp(c, ticks * 5)
        c["location"] = {"node_id": task["node_id"]}
        await self.save_char(c)
        await self.save_player(p)
        # 成就进度（须在 save_player 之后，避免覆盖 merit 写入）
        for akey, acount in ach_calls:
            await self._award_achievement(p["user_id"], akey, count=acount)
        out["exp_logs"] = logs
        return out

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
        for ev in await self.get_active_events(world["id"], world.get("tick_no", 0)):
            for eff in (ev.get("data") or {}).get("effects") or []:
                if eff.get("type") == "global_output":
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
        # 并发军团上限（§5.5：基础 2 支）
        concurrent = int(acfg.get("concurrent_base", 2))
        mine = await self.get_armies(world["id"], owner_user_id=p["user_id"])
        if len(mine) >= concurrent:
            raise StrategyError(f"军团数已达上限（{concurrent} 支）")
        leader = await self._own_char(p, leader_char_id)
        if leader["status"] != "idle":
            raise StrategyError(f"{leader['name']} 无法出征（{leader['status']}）")
        if leader["role"] == R.ROLE_OFFICIAL:
            raise StrategyError("内政官不能担任主将")
        if R.injury_state(leader["injury"]) in ("dying", "dead"):
            raise StrategyError(f"{leader['name']} 伤势过重")
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
            members.append(mc)
        faction = await self.get_faction(p["faction_id"])
        cap = await self._leader_cap(leader, faction, int(node["id"]))
        if troops > cap:
            raise StrategyError(f"超过主将带兵上限（{cap}）")
        # 招募成本（§7.5 兵种价目）
        tt = R.TROOP_TYPES[troop_type]
        coin_cost = int(troops * tt["recruit_coin"] / 10)
        grain_cost = int(troops * tt["recruit_grain"] / 10)
        if siege_engine:
            coin_cost += 3000
        if p["coin"] < coin_cost or p["grain"] < grain_cost:
            raise StrategyError(f"资源不足：需铜钱 {coin_cost} / 粮草 {grain_cost}")
        p["coin"] -= coin_cost
        p["grain"] -= grain_cost
        # 携粮 = min(库存, 统率×50)（§5.6）
        carry = min(p["grain"], R.carry_grain_cap(leader))
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
        if a["state"] != "idle":
            raise StrategyError("军团行动中，无法补员")
        node = await self.get_node(world["id"], a["node_id"])
        if not node or node["owner_faction"] != p["faction_id"]:
            raise StrategyError("补员仅限己方据点")
        troops = int(troops)
        if troops <= 0:
            raise StrategyError("补员数量须为正")
        leader = await self.get_char((a.get("characters") or [0])[0])
        faction = await self.get_faction(a["faction_id"])
        cap = await self._leader_cap(leader, faction, int(a["node_id"]))
        if a["troops"] + troops > cap:
            raise StrategyError(f"超过带兵上限（{cap}）")
        tt = R.TROOP_TYPES.get(a["troop_type"], R.TROOP_TYPES["sword_shield"])
        coin_cost = int(troops * tt["recruit_coin"] / 10)
        grain_cost = int(troops * tt["recruit_grain"] / 10)
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
        if a["state"] not in ("idle",):
            raise StrategyError(f"军团状态「{a['state']}」无法出征")
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
        ss["choice"] = choice
        node["siege_state"] = ss
        await self.save_node(node)
        await self._commit()
        return {"ok": True, "msg": f"围城策略已设为「{'巷战' if choice == 'street_fight' else '受降'}」"}

    # ------------------------------------------------------------------
    # 建设 / 升级（§5.3）
    # ------------------------------------------------------------------
    BUILDINGS = {
        "tower": {"name": "箭塔", "coin": 2000, "iron": 200, "max": 3,
                  "desc": "城防反击伤害 +20/座"},
        "infirmary": {"name": "医馆", "coin": 1500, "iron": 0, "max": 1,
                      "desc": "本据点角色伤势恢复 +10/tick"},
        "granary": {"name": "粮仓", "coin": 1200, "iron": 100, "max": 1,
                    "desc": "本据点出征军团携粮上限 +50%"},
    }

    async def build(self, user_id: str, node_id: int, building_id: str) -> dict:
        world, p = await self._require_player(user_id)
        spec = self.BUILDINGS.get(building_id)
        if not spec:
            raise StrategyError("建筑不存在")
        node = await self._owned_node(p, node_id)
        if node["type"] not in ("city", "pass", "branch"):
            raise StrategyError("仅城池/关隘/分舵可建设")
        owned = [b for b in (node.get("buildings") or [])
                 if (b.get("id") if isinstance(b, dict) else b) == building_id]
        if len(owned) >= spec["max"]:
            raise StrategyError(f"{spec['name']}已达上限（{spec['max']}）")
        if p["coin"] < spec["coin"] or p["iron"] < spec["iron"]:
            raise StrategyError(f"资源不足：需铜钱 {spec['coin']} / 精铁 {spec['iron']}")
        p["coin"] -= spec["coin"]
        p["iron"] -= spec["iron"]
        buildings = node.get("buildings") or []
        buildings.append({"id": building_id, "built_at": _now(),
                          "by": p["user_id"]})
        node["buildings"] = buildings
        await self.save_node(node)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": f"「{node['name']}」建成{spec['name']}"}

    async def upgrade_node(self, user_id: str, node_id: int) -> dict:
        """据点升级（P0：等级上限 3；城防上限 1000×级，产出 +20%/级）。"""
        world, p = await self._require_player(user_id)
        node = await self._owned_node(p, node_id)
        if node["type"] not in ("city", "pass"):
            raise StrategyError("仅城池/关隘可升级")
        if node["level"] >= 3:
            raise StrategyError("已达 P0 等级上限（3 级）")
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

    # ------------------------------------------------------------------
    # 战功商店（§11.3）
    # ------------------------------------------------------------------
    async def buy_merit_shop(self, user_id: str, item: str, char_id: int = 0) -> dict:
        world, p = await self._require_player(user_id)
        scfg = self.main_cfg().get("merit_shop") or {}
        honor = p.get("honor") or {}
        if item == "gold_pack":
            spec = scfg.get("gold_pack") or {"merit": 500, "gold": 100000, "weekly_limit": 5}
            week = time.strftime("%Y%W")
            shop_week = honor.get("merit_shop_week") or {}
            if shop_week.get("week") != week:
                shop_week = {"week": week, "used": 0}
            if shop_week["used"] >= int(spec.get("weekly_limit", 5)):
                raise StrategyError("本周灵石包兑换次数已用完")
            if p["merit"] < int(spec["merit"]):
                raise StrategyError(f"战功不足：需 {spec['merit']}")
            p["merit"] -= int(spec["merit"])
            shop_week["used"] += 1
            honor["merit_shop_week"] = shop_week
            p["honor"] = honor
            got, _ = await self._inject_gold(p, int(spec["gold"]))
            await self.save_player(p)
            await self._commit()
            return {"ok": True, "msg": f"兑换成功：灵石 +{got}（本周第 {shop_week['used']} 次）"}
        if item == "elixir":
            spec = scfg.get("elixir") or {"merit": 1500, "season_limit_per_char": 2}
            if not char_id:
                raise StrategyError("须指定角色")
            c = await self._own_char(p, char_id)
            if c["injury"] <= 0:
                raise StrategyError(f"{c['name']} 没有伤势")
            used_map = honor.get("elixir_used") or {}
            used = int(used_map.get(str(char_id), 0))
            if used >= int(spec.get("season_limit_per_char", 2)):
                raise StrategyError(f"{c['name']} 本赛季丹药次数已用完（2 次）")
            cost = int((self.main_cfg().get("injury") or {}).get(
                "elixir_merit_cost", spec.get("merit", 1500)))
            if p["merit"] < cost:
                raise StrategyError(f"战功不足：需 {cost}")
            p["merit"] -= cost
            heal = int((self.main_cfg().get("injury") or {}).get("elixir_heal", 40))
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
                # 超出补跑上限：只结算产出（跳过 NPC/战斗），防滚雪球
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
        # A-M6 时钟先落盘：管线中途崩溃时 catchup 按 last_tick_at 判定，
        # 本 tick 产出宁可丢一拍，也不会以同一 tick_no 重入导致双份产出
        world["last_tick_at"] = _now()
        await self.save_world(world)
        await self._commit()
        try:
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
                # 7b. 事件传染（瘟疫 spread：25%/tick 蔓延相邻据点）
                await self._step_event_spread(world)
                # 8. 伤势/体力恢复 + 城防自修 + 士气回归
                await self._step_recovery(world)
                # 8b. P1：俘虏流转 / 科技 / 治理（官职·俸禄）
                await self._step_captives(world)
                await self._step_tech(world)
                await self._step_governance(world)
                # 8c. P2：hex 手操战役超时托管推进
                await self._step_manual_battles(world)
                # 8d. P3：互市 / 匪寨 / 商路 / 粮队
                await self._step_market(world)
                await self._step_market_maker(world)
                await self._step_bandits(world)
                await self._step_trade_routes(world)
                await self._step_convoys(world)
                # 8e. 赛季推进（阶段/霸权/合纵/结算，须放最后）
                await self._step_season(world)
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
            await self._commit()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[strategy] tick#{tick_no} 异常: {e}")
            summary["ok"] = False
            summary["error"] = str(e)
            await self.db.conn.rollback()
        return summary

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
            p = None
            if c.get("owner_user_id"):
                p = await self.get_player(c["owner_user_id"], world["id"])
            if p is None:
                # NPC 角色任务：收益进势力国库
                p = await self._npc_wallet(world, task["faction_id"])
            ticks = int((task.get("payout") or {}).get("ticks", 4))
            try:
                if str(task["task_type"]).startswith("plot_"):
                    # 计略任务（§6.4）：走独立结算，不走 _pay_task
                    await self._settle_plot_task(world, task, c, p)
                else:
                    await self._pay_task(world, p, task, c, ticks)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[strategy] 任务#{task['id']}结算失败: {e}")
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
            for idx, node in enumerate(f_nodes):
                base = per_tick.get(node["type"]) or {}
                mult = global_mult * (1 + 0.2 * (node["level"] - 1))
                # 赛季决战 +20% / 追赶礼包 ×1.5（§11/§17）/ 司徒加成
                mult *= self.player_output_mult(p) * self.season_output_mult(world) \
                    * output_mult
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
                mult *= self.season_output_mult(world)
                nm = node_mult.get(node["id"], {})
                coin_gain += int(base.get("coin", 0) * mult * nm.get("coin", 1.0))
                grain_gain += int(base.get("grain", 0) * mult * nm.get("grain", 1.0))
            treasury["coin"] = int(treasury.get("coin", 0)) + coin_gain
            treasury["grain"] = int(treasury.get("grain", 0)) + grain_gain
            f["treasury"] = treasury
            await self.save_faction(f)
        await self._commit()
        return gold_total

    # --- 步骤 3：围城推进（§7.2）------------------------------------------
    async def _step_sieges(self, world: dict) -> list[str]:
        cfg = self.main_cfg().get("siege") or {}
        max_ticks = int(cfg.get("max_ticks", 20))
        reports: list[str] = []
        sieging = await self.get_armies(world["id"], states=("siege",))
        by_node: dict[int, list[dict]] = {}
        for a in sieging:
            by_node.setdefault(a["node_id"], []).append(a)
        for nid, attackers in by_node.items():
            node = await self.get_node(world["id"], nid)
            if not node:
                continue
            ss = node.get("siege_state") or {}
            ss["ticks"] = int(ss.get("ticks", 0)) + 1
            # 攻方掉兵：城防反击（箭塔+守军）
            towers = len([b for b in (node.get("buildings") or [])
                          if (b.get("id") if isinstance(b, dict) else b) == "tower"])
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
                # 超时：攻方自动撤退
                ss = {}
                node["siege_state"] = ss
                for a in attackers:
                    await self._auto_retreat(a)
                reports.append(f"⏳「{node['name']}」围城超时，攻方撤围")
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
        atk_sim = [await self._army_sim(a) for a in attackers]
        atk_sim = [s for s in atk_sim if s]
        atk_power = R.siege_strength(atk_sim)
        garrison_troops = int((node.get("npc_garrison") or {}).get("troops", 0))
        def_armies = await self.get_armies(world["id"], node_id=node["id"],
                                           states=("idle",))
        def_armies = [a for a in def_armies
                      if a["faction_id"] != attackers[0]["faction_id"]]
        def_power = sum(a["troops"] for a in def_armies) / 100 + garrison_troops / 100
        if choice == "auto" and atk_power >= float(cfg.get("intimidate_ratio", 3.0)) * max(
                def_power, 0.1):
            choice = "surrender"
        if choice == "surrender":
            keep = float(scfg.get("surrender_troops_keep", 0.5))
            node["npc_garrison"] = {"troops": int(garrison_troops * keep), "morale": 60}
            await self._capture_node(world, node, attackers[0]["faction_id"])
            return (f"🏳️「{node['name']}」守军开城受降！"
                    f"（攻方兵力 {atk_power:.0f} ≥ 3 倍威压）")
        # 巷战：攻 +30% / 守 -30%（street_fight_*）
        atk_bonus = float(scfg.get("street_fight_atk_bonus", 0.3))
        defenders_sim = []
        for a in def_armies:
            s = await self._army_sim(a)
            if s:
                s["morale"] = int(s["morale"] * (1 - float(
                    scfg.get("street_fight_def_penalty", 0.3))))
                defenders_sim.append((a, s))
        gar = self._garrison_sim(node)
        if gar:
            gar["morale"] = int(gar["morale"] * 0.7)
            defenders_sim.append((None, gar))
        for s in atk_sim:
            s["atk_terrain_mult"] = float(s.get("atk_terrain_mult", 1.0)) * (1 + atk_bonus)
        rng = self._rng("battle", world["id"], node["id"], world["tick_no"])
        result = R.simulate_battle(
            atk_sim, [s for _a, s in defenders_sim], rng,
            max_rounds=int((cfg.get("battle") or {}).get("max_rounds", 30)),
            night=R.is_night(time.localtime().tm_hour), defending_node=False)
        report = R.render_battle_report(result, title=f"巷战·{node['name']}")
        await self._persist_battle(world, node, attackers[0]["faction_id"],
                                   node["owner_faction"], "street", result, report,
                                   atk_sim, [s for _a, s in defenders_sim])
        await self._apply_battle_aftermath(
            world, node, attackers, [a for a, _s in defenders_sim], result,
            atk_sim, [s for _a, s in defenders_sim])
        if result["winner"] == "attacker":
            # 巷战破城：败方被歼/溃军团角色被俘判定（street +10%，§7.9）
            losers = [a for a, _s in defenders_sim
                      if a and (a.get("_destroyed") or a.get("_routed"))]
            if losers:
                await self.roll_captures(world, node, losers, "city_fall",
                                         int(attackers[0]["faction_id"]), street=True)
            await self._capture_node(world, node, attackers[0]["faction_id"])
            report += f"\n🏴「{node['name']}」易主！"
        else:
            node["defense"] = int(R.city_defense_max(node["level"]) * 0.2)
            node["siege_state"] = {}
            # A-M10：战败幸存攻方的自动撤退已统一在 _apply_battle_aftermath 处理
        return report

    async def _capture_node(self, world: dict, node: dict, faction_id: int):
        prev_owner = int(node.get("owner_faction", 0))
        node["owner_faction"] = int(faction_id)
        node["siege_state"] = {}
        node["morale"] = 50
        node["defense"] = int(R.city_defense_max(node["level"]) * 0.2) \
            if node["type"] in ("city", "pass", "branch") else 0
        node["npc_garrison"] = {"troops": max(
            200, int((node.get("npc_garrison") or {}).get("troops", 0) * 0.5)),
            "morale": 60} if node.get("npc_garrison", {}).get("troops") else \
            {"troops": 0, "morale": 60}
        # 新占节点保护期（§17：168h 免战，防滚雪球）
        pcfg = self.main_cfg().get("protection") or {}
        if prev_owner != int(faction_id):
            node["protect_until"] = _now() + int(pcfg.get("new_node_hours", 168)) * 3600
        # 驻守的敌方军团溃散处理
        for a in await self.get_armies(world["id"], node_id=node["id"], states=("idle",)):
            if a["faction_id"] != faction_id:
                await self._auto_retreat(a)
        await self.save_node(node)
        # 关押于此的俘虏解放（§7.9）
        await self.liberate_captives_at_node(world, node, int(faction_id))

    async def _auto_retreat(self, army: dict):
        """战败/超时自动撤往最近己方据点；无路可退则解散（角色归还）。"""
        home = await self._nearest_own_node(
            {"id": army["world_id"]}, {"faction_id": army["faction_id"]}, army["node_id"])
        if home is None:
            for cid in army.get("characters") or []:
                c = await self.get_char(cid)
                if c:
                    c["status"] = "idle" if R.injury_state(c["injury"]) != "dead" else "dead"
                    await self.save_char(c)
            await self.delete_army(army["id"])
            return
        path = await self.find_path(army["world_id"], army["node_id"], home)
        army["state"] = "march"
        army["path"] = (path or [home])[1:] if path else []
        army["edge_pos"] = 0.0
        if not army["path"]:
            army["state"] = "idle"
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
            a["edge_pos"] = float(a.get("edge_pos", 0)) + R.march_speed(a, terrain)
            cost = R.TERRAIN_MARCH_COST.get(terrain, 1.0)
            # 粮耗（行军中每 tick）
            far = len(a["path"]) > int((self.main_cfg().get("army") or {}).get(
                "far_supply_steps", 2))
            upkeep = R.grain_upkeep(a, terrain, far)
            acfg = self.main_cfg().get("army") or {}
            if a["grain"] < upkeep:
                a["grain"] = 0
                a["morale"] = max(0, a["morale"] - int(acfg.get("starve_morale_per_tick", 20)))
                desert = int(a["troops"] * float(acfg.get("starve_desert_pct", 0.05)))
                a["troops"] = max(0, a["troops"] - desert)
                if a["morale"] <= 0 or a["troops"] <= 0:
                    await self._auto_retreat(a)
                    continue
            else:
                a["grain"] -= upkeep
            if a["edge_pos"] >= cost:
                a["edge_pos"] -= cost
                a["node_id"] = nxt_id
                a["path"] = a["path"][1:]
            await self.save_army(a)
        await self._commit()

    # --- 步骤 8：恢复（§4.6/§5.3）------------------------------------------
    async def _step_recovery(self, world: dict):
        icfg = self.main_cfg().get("injury") or {}
        scfg = self.main_cfg().get("stamina") or {}
        nodes = {n["id"]: n for n in await self.get_nodes(world["id"])}
        infirmary_nodes = set()
        for nid, node in nodes.items():
            if any((b.get("id") if isinstance(b, dict) else b) == "infirmary"
                   for b in (node.get("buildings") or [])):
                infirmary_nodes.add(nid)
        # 角色恢复
        rows = await self._fetchall(
            "SELECT * FROM strategy_characters WHERE world_id=? AND status!='dead'",
            (world["id"],))
        taishou_nodes: set[int] = set()
        for row in rows:
            c = self._decode("strategy_characters", row)
            if str(c.get("appointed_position") or "") == "taishou" and \
                    int(c.get("appointed_node_id") or 0) > 0:
                taishou_nodes.add(int(c.get("appointed_node_id") or 0))
            per = int(icfg.get("recover_per_tick", 5))
            loc_node = int((c.get("location") or {}).get("node_id", 0) or 0)
            if loc_node in infirmary_nodes:
                per = int(icfg.get("infirmary_recover", 10))
            if c["status"] == "idle":
                R.recover_injury(c, per)
            R.recover_stamina(c, int(scfg.get("recover_per_tick", 10))
                              if scfg.get("enabled", True) else 0)
            await self.save_char(c)
        # 据点：城防自修 + 士气回归 + 瘟疫类 DOT
        active = await self.get_active_events(world["id"], world.get("tick_no", 0))
        for nid, node in nodes.items():
            if node["owner_faction"] != 0 and not node.get("siege_state"):
                if node["defense"] > 0:
                    node["defense"] = R.city_self_repair(node["defense"], node["level"])
                # 太守辖地：民心恢复 ×2（§4.5 官职表）
                step = 2 if nid in taishou_nodes else 1
                node["morale"] = min(100, node["morale"] + step) \
                    if node["morale"] < 80 else max(80, node["morale"] - 1)
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
                await self.save_army(a)
        await self._commit()

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
        # 撕毁停战/背盟判定（§6.3）
        await self.check_truce_violation(world, int(atk_fid),
                                         int(node.get("owner_faction", 0)))
        # 守方：驻守军团 + 守军（残军/征召兵）+ 正在围城的其他势力
        defenders: list[dict] = []
        for a in await self.get_armies(world["id"], node_id=node["id"],
                                       states=("idle", "siege")):
            if a["faction_id"] != atk_fid:
                defenders.append(a)
        garrison_troops = int((node.get("npc_garrison") or {}).get("troops", 0))
        unowned = node["owner_faction"] == 0
        if not defenders and (garrison_troops <= 0 or unowned and garrison_troops <= 0):
            if unowned:
                await self._capture_node(world, node, atk_fid)
                return f"🚩「{node['name']}」守备空虚，被 {await self._faction_name(atk_fid)} 直接占领"
            if node["defense"] > 0:
                await self._begin_siege(world, node, attackers[0])
                return f"⛺「{node['name']}」无兵驻守但城防尚存，攻方转入围城"
            await self._capture_node(world, node, atk_fid)
            return f"🚩「{node['name']}」被 {await self._faction_name(atk_fid)} 占领"
        # 会战
        atk_sim, dfn_sim, gar_sim = [], [], None
        node_taishou = any(
            str(c.get("appointed_position") or "") == "taishou" and
            int(c.get("appointed_node_id") or 0) == int(node["id"])
            for c in await self.get_chars(
                world_id=world["id"], faction_id=int(node.get("owner_faction") or 0)))
        for a in attackers:
            s = await self._army_sim(a)
            if s:
                atk_sim.append(s)
        for a in defenders:
            s = await self._army_sim(a)
            if s:
                s["wall_mult"] = 1.0 + min(0.3, node["defense"] / R.city_defense_max(
                    max(1, node["level"])) * 0.3)
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
                return f"🚩「{node['name']}」被 {await self._faction_name(atk_fid)} 占领"
            await self._begin_siege(world, node, attackers[0])
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
            elif node["defense"] > 0:
                await self._begin_siege(world, node, survivors[0])
                report += f"\n⛺ 攻方获胜，「{node['name']}」城防 {node['defense']}，转入围城"
            else:
                await self._capture_node(world, node, atk_fid)
                report += f"\n🚩「{node['name']}」被 {await self._faction_name(atk_fid)} 占领！"
        else:
            report += f"\n🛡️「{node['name']}」守住了攻势"
        return report

    async def _begin_siege(self, world: dict, node: dict, army: dict):
        army["state"] = "siege"
        army["edge_pos"] = 0.0
        army["path"] = []
        ss = node.get("siege_state") or {}
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
        return {
            "id": int(army["id"]), "faction_id": int(army.get("faction_id", 0)),
            "troop_type": army.get("troop_type", "sword_shield"),
            "troops": int(army["troops"]), "morale": int(army.get("morale", 100)),
            "veteran": int(army.get("veteran", 0)), "leader": leader,
            "siege_engine": bool(army.get("siege_engine", 0)),
            "pos_atk_mult": 1.0 + float(pos_bonus.get("army_atk", 0.0)),
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

    async def _apply_battle_aftermath(self, world: dict, node: dict,
                                      attackers: list[dict], defenders: list[dict],
                                      result: dict, atk_sim: list = None,
                                      dfn_sim: list = None):
        """把模拟结果写回军团/守军/角色（伤亡、溃逃、覆没、伤势、战功、精锐）。"""
        cfg = self.main_cfg()
        bcfg = cfg.get("battle") or {}
        rows = {r["id"]: r for r in
                (result.get("attackers") or []) + (result.get("defenders") or [])}
        atk_ids = {id(a) for a in attackers}
        winner = result.get("winner")
        all_armies = list(attackers) + [d for d in defenders if d]
        # 军团兵力/士气写回 + 溃逃/覆没善后
        for army in all_armies:
            row = rows.get(int(army["id"]))
            if not row:
                continue
            is_attacker = id(army) in atk_ids
            army["troops"] = int(row["troops_after"])
            army["morale"] = int(row["morale"])
            army["veteran"] = min(R.VETERAN_MAX, int(army.get("veteran", 0))
                                  + R.veteran_gain(
                winner == ("attacker" if is_attacker else "defender")))
            # 主将阅历
            cids = army.get("characters") or []
            leader = await self.get_char(int(cids[0])) if cids else None
            won = winner == ("attacker" if is_attacker else "defender")
            if leader:
                kills = int(result.get("dfn_losses" if is_attacker else "atk_losses", 0))
                R.gain_exp(leader, (kills // 10 + 25) if won else 10)
                # 战功（仅玩家军团）
                if army.get("owner_user_id"):
                    merit = R.merit_from_battle(
                        kills // max(1, len([x for x in all_armies
                                             if (id(x) in atk_ids) == is_attacker])),
                        won, False,
                        float(bcfg.get("merit_per_kill", 0.05)),
                        int(bcfg.get("win_merit_base", 100)))
                    if merit > 0:
                        pp = await self.get_player(army["owner_user_id"], world["id"])
                        if pp:
                            pp["merit"] += merit
                            pp["merit_season"] += merit
                            await self.save_player(pp)
                await self.save_char(leader)
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
                await self.save_army(army)
                await self._auto_retreat(army)
            else:
                await self.save_army(army)
                if is_attacker and winner == "defender":
                    # A-M10：战败且未溃逃的幸存攻方自动撤退；否则滞留敌方
                    # 节点（march+空 path），下 tick _step_arrivals 再触发会战
                    # → 无限攻打循环
                    await self._auto_retreat(army)
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
            await self.save_node(node)
        await self._commit()

    # ------------------------------------------------------------------
    # 步骤 6：NPC AI（§9.3）
    # ------------------------------------------------------------------
    async def _step_npc_ai(self, world: dict):
        cfg = self.main_cfg().get("npc") or {}
        threshold = float(cfg.get("aggression_threshold", 10))
        reserve = float(cfg.get("garrison_home_reserve", 0.3))
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
                troops = min(cap, troops + max(1, int(cap * 0.02)))
                gar["troops"] = troops
                gar["morale"] = min(85, int(gar.get("morale", 80)) + 2)
                node["npc_garrison"] = gar
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
                    if tf_owner > 0:
                        tfo = await self.get_faction(tf_owner)
                        if tfo and int(tfo.get("alliance_id") or 0) and \
                                int(tfo.get("alliance_id")) == int(f.get("alliance_id") or 0):
                            continue
                        if R.relation_of(f_dip, tf_owner, _now()) == R.REL_ALLY:
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
        events = ecfg.get("events") or []
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
            f_meta[f["id"]] = {"id": f["id"], "name": f["name"], "color": f["color"],
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
                "buildings": [b.get("id") if isinstance(b, dict) else b
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
            "wanderers": await self.get_wanderers_view(world),
            "wanderer_auctions": await self.get_wanderer_auctions_view(
                world, user_id),
            "petitions": [
                {"id": pet.get("id"), "name": pet.get("name"),
                 "color": pet.get("color"), "creator": pet.get("creator"),
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
        }
        if p:
            state["player"] = await self._player_view(p, world, nodes)
            state["battles"] = await self._battle_list(wid, p["faction_id"])
        return state

    async def _player_view(self, p: dict, world: dict, nodes: list[dict]) -> dict:
        node_names = {n["id"]: n["name"] for n in nodes}
        char_names: dict[int, str] = {}
        for c0 in await self.get_chars(world_id=world["id"]):
            char_names[c0["id"]] = c0["name"]
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
                "exp_next": R.exp_to_next(c["level"]), "loyalty": c["loyalty"],
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
            tasks.append({
                "id": t["id"], "char_id": t["character_id"], "char_name": c["name"],
                "task_type": t["task_type"],
                "task_name": R.TASK_NAMES.get(t["task_type"], t["task_type"]),
                "node_id": t["node_id"], "node_name": node_names.get(t["node_id"], ""),
                "end_at": t["end_at"], "remain_sec": max(0, t["end_at"] - _now()),
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
                relations.append({
                    "faction_id": other_id,
                    "name": (of or {}).get("name", "?"),
                    "rel": rel, "until": until,
                    "from": int(v.get("from", 0) or 0),
                    "indemnity": int(v.get("indemnity", 0) or 0)})
            counts: dict[int, int] = {}
            for n in nodes:
                counts[int(n["owner_faction"])] = counts.get(int(n["owner_faction"]), 0) + 1
            allied = []
            for af in await self._alliance_factions(world["id"], host):
                allied.append({
                    "id": int(af["id"]), "name": af["name"], "color": af["color"],
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
        return {
            "user_id": p["user_id"], "faction_id": p["faction_id"],
            "coin": p["coin"], "grain": p["grain"], "iron": p["iron"],
            "recruit_token": p["recruit_token"], "merit": p["merit"],
            "honor": p.get("honor") or {},
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
        out_name = {"coin": "铜钱", "grain": "粮草", "token": "募将令",
                    "intel": "情报", "build": "城防", "train": "精锐",
                    "merit": "战功", "heal": "疗伤", "tech": "科技点"}
        node_bonus = {"govern": "仙城 ×1.5", "farm": "灵谷田 ×2.0",
                      "trade": "坊市 ×1.3", "seek": "分舵 ×1.4",
                      "hunt": "遗迹 ×1.3", "build": "仙城 ×1.2"}
        meta = {}
        for k in ("govern", "farm", "trade", "seek", "scout", "build",
                  "train", "hunt", "heal", "research"):
            best = R.TASK_BEST_ROLE.get(k)
            if best:
                role = R.ROLE_NAMES.get(best, best)
            elif k in ("trade", "build", "heal"):
                role = "内政官/军师"
            else:
                role = "任意"
            meta[k] = {
                "name": R.TASK_NAMES.get(k, k),
                "stat": R.STAT_NAMES.get(R.TASK_PRIMARY_STAT.get(k, "admin"), ""),
                "role": role,
                "output": out_name.get(R.TASK_OUTPUT_RES.get(k, ""), ""),
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
                           ("govern", "farm", "trade", "seek", "scout", "build",
                            "train", "hunt", "heal", "research")},
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
            "captive_cfg": cfg.get("captive") or {},
            "duel_cfg": cfg.get("duel") or {},
            "hex_cfg": cfg.get("hex") or {},
            "market_cfg": cfg.get("market") or {},
            "wanderer_auction_cfg": cfg.get("wanderer_auction") or {},
            "season_cfg": cfg.get("season") or {},
            "shield_cfg": cfg.get("shield") or {},
            "rebuild_cfg": cfg.get("rebuild") or {},
        }

    # ------------------------------------------------------------------
    # 动作分发（REST → manager）
    # ------------------------------------------------------------------
    async def handle_action(self, user_id: str, action: str,
                            params: Optional[dict] = None) -> dict:
        """动作统一入口（REST 漏斗）。

        A-M7：全程持 _action_lock 串行化玩家动作，防并发「读-改-写」互相
        覆盖丢更新（钱包/军团/互市等）；锁序恒为 _action_lock → _world_lock
        （tick/poll 同序，GM 重置赛季只拿 _world_lock，无死锁）。
        tick 持 _world_lock 期间照旧 Busy 拒绝（保持 REST 423 语义）。
        """
        if self._world_lock.locked():
            raise StrategyBusyError("世界推进中，请稍后再试")
        async with self._action_lock:
            if self._world_lock.locked():
                raise StrategyBusyError("世界推进中，请稍后再试")
            return await self._handle_action(user_id, action, params)

    async def _handle_action(self, user_id: str, action: str,
                             params: Optional[dict] = None) -> dict:
        params = params or {}
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
            if action == "dispatch_task":
                return await self.dispatch_task(
                    user_id, int(params["char_id"]), int(params["node_id"]),
                    str(params.get("task_type", "farm")), int(params.get("hours", 1)))
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
            if action == "upgrade_node":
                return await self.upgrade_node(user_id, int(params["node_id"]))
            if action == "buy_merit_shop":
                return await self.buy_merit_shop(user_id, str(params["item"]),
                                                 int(params.get("char_id", 0)))
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
                return await self.tech_research(user_id, str(params["line"]))
            if action == "diplomacy_propose":
                return await self.diplomacy_propose(
                    user_id, int(params["target_fid"]), str(params["kind"]),
                    int(params.get("indemnity", 0)))
            if action == "diplomacy_respond":
                return await self.diplomacy_respond(
                    user_id, int(params["from_fid"]), bool(params.get("accept")))
            if action == "rebuild_capital":
                return await self.rebuild_capital(user_id, int(params["node_id"]))
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
                return await self.intervene_siege(user_id, int(params["node_id"]))
            if action == "sortie":
                return await self.sortie(user_id, int(params["node_id"]))
            if action == "battle_state":
                return await self.battle_state(user_id, int(params["battle_id"]))
            if action == "battle_deploy":
                return await self.battle_deploy(
                    user_id, int(params["battle_id"]),
                    params.get("placements") or [])
            if action == "battle_auto_deploy":
                return await self.battle_auto_deploy(
                    user_id, int(params["battle_id"]))
            if action == "battle_set_ambush":
                return await self.battle_set_ambush(
                    user_id, int(params["battle_id"]), int(params["uid"]),
                    int(params["q"]), int(params["r"]))
            if action == "battle_start":
                return await self.battle_start(user_id, int(params["battle_id"]))
            if action == "battle_action":
                return await self.battle_action(
                    user_id, int(params["battle_id"]), str(params["act"]),
                    params.get("params") or {})
            if action == "battle_delegate":
                return await self.battle_delegate(
                    user_id, int(params["battle_id"]), bool(params.get("on", True)))
            if action == "battle_replay":
                return await self.get_battle_replay(
                    user_id, int(params["battle_id"]))
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
                    int(params.get("amount", 0) or 0),
                    buyout=bool(params.get("buyout")))
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
        except ValueError as e:
            raise StrategyError(f"参数无效：{e}") from e

    @staticmethod
    def _ensure_joined(p: dict):
        if not p:
            raise StrategyError("尚未入场")





