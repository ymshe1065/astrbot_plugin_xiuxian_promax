# -*- coding: utf-8 -*-
"""「仙域争霸」NPC 威胁值与战略外交 Mixin（设计文档 §4~§8）。

覆盖：
- 威胁快照（体量 / 接壤 / 扩张 / 军力）与累计敌意 ``H``；
- 势力对状态机（calm/alert/prepare/hostile/war/truce/cooldown）；
- NPC 自主宣战（含自杀宣战、非接壤霸主远征）；
- 战争疲劳 ``E``、NPC→NPC / NPC→玩家停战；
- 战争狂热参数、龟缩判定。

持久状态统一写 ``strategy_worlds.data_json`` 的 ``npc_ai`` / ``threat``；
不新增数据库表。所有随机都走 ``self._rng(...)`` 保证同 tick 可重放。
"""
from __future__ import annotations

import json
import time
from typing import Optional

from ..core import strategy_rules as R

#: 势力对持久 ``phase``（frenzy / weary / ceasefire 为派生状态）
PHASES = ("calm", "alert", "prepare", "hostile", "war", "truce", "cooldown")
#: 由 ``H`` 驱动的档位状态
TIER_PHASES = ("calm", "alert", "prepare", "hostile")
#: 档位进入线（与 ``npc.threat.tiers`` 缺省值一致）
TIER_ENTRY = {"alert": 20, "prepare": 40, "hostile": 60}
#: 档位滞回：退出线 = 进入线 − 5
TIER_HYSTERESIS = 5
TIER_CN = {"calm": "无感", "alert": "警觉", "prepare": "戒备",
           "hostile": "敌视", "coalition": "公敌"}
GOAL_CN = {"border": "边境战争", "hegemon": "讨逆战争",
           "revenge": "复仇战争", "opportunity": "机会战争"}


def _now() -> int:
    return int(time.time())


def _merge(defaults: dict, raw: dict | None) -> dict:
    """浅合并 + 嵌套 dict 一层深合并（缺省值兜底，兼容旧配置）。"""
    out = dict(defaults)
    for key, value in (raw or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = dict(out[key])
            merged.update(value)
            out[key] = merged
        else:
            out[key] = value
    return out


def _num(value, default: float) -> float:
    """取数值：``None`` / 非法值回退 ``default``。

    不能写成 ``value or default``：战争顺风度为 0、平均民心为 0 都是合法
    极端值（一方被打崩 / 民心归零），按 falsy 兜底会错误当成 50 / 100。
    """
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)


def _safe_int(value, default: int = 0) -> int:
    """旧档自愈：非法 / NaN / inf / 超大整数回退 ``default``。"""
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)
    if out != out or out in (float("inf"), float("-inf")):
        return int(default)
    return int(out)


def _safe_float(value, default: float = 0.0) -> float:
    """旧档自愈：非法 / NaN / inf / 超大整数回退 ``default``。"""
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    if out != out or out in (float("inf"), float("-inf")):
        return float(default)
    return out


#: 势力对里应为整数的字段（损坏类型自愈时按 ``_default_pair`` 回退）
_INT_PAIR_KEYS = (
    "hostile_since_tick", "war_start_tick", "frenzy_until_tick",
    "no_declare_until_tick", "truce_until_tick", "ceasefire_until_tick",
    "last_offer_tick", "last_declare_tick", "last_capture_tick",
    "last_warning_tick", "last_expedition_tick", "captures_by_me",
    "captures_by_target", "war_start_troops", "war_start_nodes",
)
#: 势力对里应为 0~100 浮点的字段
_FLOAT_PAIR_KEYS = ("war_score", "exhaustion")


class StrategyThreatMixin:
    """NPC 威胁值 / 自主宣战 / 停战 / 狂热 / 风声。"""

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _npc_threat_cfg(self) -> dict:
        raw = (self.main_cfg().get("npc") or {}).get("threat") or {}
        return _merge({
            "enabled": True,
            "eval_interval_ticks": 1,
            "level_bonus": 0.10,
            "weights": {"size": 0.30, "border": 0.40,
                        "growth": 0.15, "force": 0.15},
            "border_norm": 8.0,
            "growth_window_ticks": 96,
            "growth_norm": 0.10,
            "force_hops": 2,
            "force_norm": 2.0,
            "sensitivity": {"expansive": 1.25, "developer": 0.85,
                            "trader": 0.70, "hermit": 0.60, "demonic": 1.40},
            "relation_damp": {"ally": 0.05, "truce": 0.25,
                              "neutral": 1.0, "war": 1.0},
            "favor_damp_floor": 0.5,
            "reputation": {"righteous_penalty": 0.05},
            "nonadj_factor": 0.30,
            "hegemon_nonadj_factor": 0.70,
            "hegemon_share_threshold": 0.40,
            "public_base": 60.0,
            "public_span": 40.0,
            "public_floor_margin": 5.0,
            "alpha_up_adj": 0.020,
            "alpha_up_nonadj": 0.005,
            "alpha_up_hegemon_nonadj": 0.012,
            "alpha_down": 0.015,
            "tiers": {"alert": 20, "prepare": 40, "hostile": 60,
                      "coalition": 80},
            "aggression_gain": 0.80,
            "aggression_cap": 2.20,
            "turtle": {"enabled": True, "defense_threshold": 1500,
                       "threat_damp": 0.60, "declare_mult": 0.50,
                       "priority_penalty": 20,
                       "exclude_hegemon": True, "window_ticks": 672},
        }, raw)

    def _npc_war_cfg(self) -> dict:
        raw = (self.main_cfg().get("npc") or {}).get("war") or {}
        return _merge({
            "declare_gate": 60,
            "hostile_hold_ticks": 8,
            "declare_base_per_day": 0.06,
            "declare_span_per_day": 0.34,
            "max_p_day": 0.55,
            "min_power_ratio": 0.35,
            "allow_suicide_declare": True,
            "suicide_declare_mult": 0.55,
            "desperation_mult": 0.90,
            "desperation_nodes": 2,
            "max_active_wars_per_npc": 2,
            "max_declarations_per_tick": 1,
            "max_declarations_per_day": 4,
            "declare_cooldown_ticks": 96,
            "post_war_cooldown_ticks": 96,
            "warning_cooldown_ticks": 48,
            "personality_war_bias": {"expansive": 1.20, "developer": 0.70,
                                     "trader": 0.50, "hermit": 0.30,
                                     "demonic": 1.40},
            "goal_mult": {"border": 1.0, "hegemon": 1.30,
                          "revenge": 1.50, "opportunity": 0.80},
            "frenzy": {
                "hours": 12, "sorties_bonus": 2,
                "home_reserve_delta": -0.15, "home_reserve_min": 0.10,
                "send_mult": 1.15, "utility_threshold": 4,
                "target_bonus": 1.30, "aggression_bonus": 0.30,
                "morale_min": 25, "mobilization_cost_mult": 0.90,
                "grain_mult": 1.50, "min_troops": 300, "morale_break": 20,
            },
            "expedition": {"max_edges": 10, "min_troops": 800,
                           "grain_mult": 1.50, "cooldown_ticks": 8},
        }, raw)

    def _npc_peace_cfg(self) -> dict:
        raw = (self.main_cfg().get("npc") or {}).get("peace") or {}
        return _merge({
            "min_war_ticks": 24,
            "max_war_ticks": 672,
            "peace_gate": 60,
            "peace_base_per_day": 0.08,
            "peace_span_per_day": 0.42,
            "max_peace_p_day": 0.60,
            "no_progress_norm": 96,
            "ceasefire_hours": 24,
            "offer_cooldown_ticks": 96,
            "post_war_cooldown_ticks": 96,
            "accept_min": 0.05,
            "accept_max": 0.95,
            "personality_peace_bias": {"expansive": 0.70, "developer": 1.10,
                                       "trader": 1.50, "hermit": 1.30,
                                       "demonic": 0.50},
        }, raw)

    def _npc_wind_cfg(self) -> dict:
        raw = (self.main_cfg().get("npc") or {}).get("wind") or {}
        return _merge({
            "enabled": True,
            "keep": 40,
            "visible_hours": 72,
            "panel_limit": 14,
            "default_delay_hours": 0,
            "kinds": {"war": "战云", "peace": "休兵", "battle": "战报",
                      "event": "异象", "season": "天时", "plot": "流言"},
        }, raw)

    def _ticks_per_day(self) -> int:
        return max(1, 86400 // max(1, self.tick_seconds()))

    def _ticks_per_hour(self) -> int:
        return max(1, 3600 // max(1, self.tick_seconds()))

    @staticmethod
    def _personality_key(faction: dict | None) -> str:
        key = str(((faction or {}).get("personality") or {}).get("key") or "")
        return key if key else "developer"

    # ------------------------------------------------------------------
    # 持久状态
    # ------------------------------------------------------------------
    @staticmethod
    def _default_pair() -> dict:
        return {
            "phase": "calm",
            "hostile_since_tick": 0,
            "war_start_tick": 0,
            "frenzy_until_tick": 0,
            "no_declare_until_tick": 0,
            "truce_until_tick": 0,
            "ceasefire_until_tick": 0,
            "goal": "",
            "war_score": 50,
            "exhaustion": 0.0,
            "last_offer_tick": 0,
            "last_declare_tick": 0,
            "last_capture_tick": 0,
            "last_warning_tick": 0,
            "last_expedition_tick": 0,
            "captures_by_me": 0,
            "captures_by_target": 0,
            "war_start_troops": 0,
            "war_start_nodes": 0,
        }

    def _npc_ai_state(self, world: dict) -> dict:
        """取（并按需初始化）``world.data.npc_ai``。"""
        data = world.setdefault("data", {})
        state = data.get("npc_ai")
        if not isinstance(state, dict):
            state = {}
        for key, default in (("accum", {}), ("pairs", {}), ("stats", {}),
                             ("declarations", {"day": 0, "count": 0})):
            if not isinstance(state.get(key), type(default)):
                state[key] = default
        data["npc_ai"] = state
        return state

    def _sanitize_npc_ai_state(self, world: dict) -> dict:
        """旧档 / 损坏字段自愈（每 tick 威胁层入口调用一次）。

        历史版本或人工改库可能留下 ``{"count": "NaN"}``、``truce_until_tick``
        为字符串、``accum`` 值为 ``"abc"`` 等；这些字段会在 ``_step_npc_*``
        里被 ``int()`` / ``float()`` 直接转换，一旦抛错整段 tick 回滚，且每
        tick 都在同一处失败，世界永久卡死。这里统一清洗为合法类型：
        - 非 dict 的 pair / accum / stats 行丢弃或重建；
        - 数值字段非法回退默认值，``H``/``war_score``/``exhaustion`` 钳到 0~100；
        - ``declarations.day/count`` 强制整数。
        """
        state = self._npc_ai_state(world)
        defaults = self._default_pair()
        # 每日宣战计数
        dec = state.get("declarations")
        if isinstance(dec, dict):
            dec["day"] = _safe_int(dec.get("day"), 0)
            dec["count"] = max(0, _safe_int(dec.get("count"), 0))
        # 累计敌意
        accum = state.get("accum")
        if isinstance(accum, dict):
            for okey in list(accum.keys()):
                row = accum.get(okey)
                if not isinstance(row, dict):
                    accum.pop(okey, None)
                    continue
                for tkey in list(row.keys()):
                    raw = row.get(tkey)
                    if isinstance(raw, bool):
                        row.pop(tkey, None)
                        continue
                    try:
                        h = float(raw)
                    except (TypeError, ValueError, OverflowError):
                        row.pop(tkey, None)
                        continue
                    if h != h or h in (float("inf"), float("-inf")):
                        row.pop(tkey, None)
                        continue
                    row[tkey] = max(0.0, min(100.0, h))
        # 势力对状态
        pairs = state.get("pairs")
        if isinstance(pairs, dict):
            for okey in list(pairs.keys()):
                row = pairs.get(okey)
                if not isinstance(row, dict):
                    pairs.pop(okey, None)
                    continue
                for tkey in list(row.keys()):
                    pair = row.get(tkey)
                    if not isinstance(pair, dict):
                        row[tkey] = dict(defaults)
                        continue
                    for key in _INT_PAIR_KEYS:
                        pair[key] = _safe_int(pair.get(key), defaults[key])
                    for key in _FLOAT_PAIR_KEYS:
                        # 战况 / 疲劳是 0~100 的归一化值
                        pair[key] = max(0.0, min(100.0, _safe_float(
                            pair.get(key), defaults[key])))
                    phase = str(pair.get("phase") or "")
                    if phase not in PHASES:
                        pair["phase"] = "calm"
                    goal = pair.get("goal")
                    if not isinstance(goal, str) or goal not in (
                            "", "border", "hegemon", "revenge",
                            "opportunity"):
                        pair["goal"] = ""
        # 龟缩统计 / 体量历史
        stats = state.get("stats")
        if isinstance(stats, dict):
            for fkey in list(stats.keys()):
                stat = stats.get(fkey)
                if not isinstance(stat, dict):
                    stats[fkey] = {}
                    stat = stats[fkey]
                for key in ("captures_7d", "attacks_7d"):
                    vals = stat.get(key)
                    if isinstance(vals, list):
                        cleaned = []
                        for value in vals:
                            tick_value = _safe_int(value, -1)
                            if tick_value >= 0:
                                cleaned.append(tick_value)
                        stat[key] = cleaned[-32:]
                    elif vals is not None:
                        stat[key] = []
                history = stat.get("share_history")
                if isinstance(history, list):
                    stat["share_history"] = [
                        {"tick": _safe_int(h.get("tick"), 0),
                         "share": max(0.0, min(1.0, _safe_float(
                             h.get("share"), 0.0)))}
                        for h in history if isinstance(h, dict)][-8:]
                elif history is not None:
                    stat["share_history"] = []
        return state

    def _pair(self, state: dict, observer_id: int, target_id: int) -> dict:
        pairs = state.setdefault("pairs", {})
        okey, tkey = str(int(observer_id)), str(int(target_id))
        row = pairs.get(okey)
        if not isinstance(row, dict):
            row = pairs[okey] = {}
        defaults = self._default_pair()
        pair = row.get(tkey)
        if not isinstance(pair, dict):
            pair = row[tkey] = dict(defaults)
        for key, value in defaults.items():
            pair.setdefault(key, value)
        return pair

    def _pair_of(self, world: dict, observer_id: int, target_id: int) -> dict:
        return self._pair(self._npc_ai_state(world), observer_id, target_id)

    def _prune_npc_ai_faction(self, world: dict, faction_id: int) -> None:
        """立即清掉某势力的 pair / accum / stats（灭亡当 tick 即生效）。

        ``_step_npc_diplomacy`` 的清理发生在 ``_step_npc_ai`` 判定灭亡之前，
        若不在这里补一次，灭亡当 tick 结束时 ``world.data`` 仍会残留该势力
        的势力对与敌意，直到下一 tick 才自愈。
        """
        state = self._npc_ai_state(world)
        fid_key = str(int(faction_id))
        pairs = state.get("pairs")
        if isinstance(pairs, dict):
            pairs.pop(fid_key, None)
            for row in pairs.values():
                if isinstance(row, dict):
                    row.pop(fid_key, None)
        accum = state.get("accum")
        if isinstance(accum, dict):
            accum.pop(fid_key, None)
            for row in accum.values():
                if isinstance(row, dict):
                    row.pop(fid_key, None)
        stats = state.get("stats")
        if isinstance(stats, dict):
            stats.pop(fid_key, None)
        # data.threat 是同一 tick 早些时候生成的派生快照：灭亡当 tick 也清掉，
        # 避免 tick 结束落盘的 world.data 仍带死亡势力份额/威胁。
        threat = (world.get("data") or {}).get("threat")
        if isinstance(threat, dict):
            for key in ("accum", "inst", "split", "tier"):
                rows = threat.get(key)
                if not isinstance(rows, dict):
                    continue
                rows.pop(fid_key, None)
                for row in rows.values():
                    if isinstance(row, dict):
                        row.pop(fid_key, None)
            for key in ("share", "public"):
                rows = threat.get(key)
                if isinstance(rows, dict):
                    rows.pop(fid_key, None)

    def _npc_h(self, world: dict, observer_id: int, target_id: int) -> float:
        accum = self._npc_ai_state(world).get("accum") or {}
        try:
            return float((accum.get(str(int(observer_id))) or {})
                         .get(str(int(target_id)), 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return 0.0

    # ------------------------------------------------------------------
    # 风声
    # ------------------------------------------------------------------
    def _add_wind(self, world: dict, text: str, kind: str = "plot",
                  delay_hours: float = 0) -> None:
        """写入一条风声（短期传闻日志，复用 ``world.data.winds``）。"""
        cfg = self._npc_wind_cfg()
        if not cfg.get("enabled", True):
            return
        text = str(text or "").strip()
        if not text:
            return
        data = world.setdefault("data", {})
        winds = data.get("winds")
        if not isinstance(winds, list):
            winds = []
        try:
            delay = float(delay_hours)
        except (TypeError, ValueError):
            delay = 0.0
        winds.append({
            "visible_at": _now() + int(delay * 3600),
            "text": text,
            "kind": str(kind or "plot"),
            "tick": int(world.get("tick_no", 0) or 0),
        })
        data["winds"] = winds[-max(1, int(cfg.get("keep", 40))):]

    async def _step_winds(self, world: dict) -> None:
        """事件开始 / 结束的传闻播报（按事件行 id 对比上一 tick）。"""
        cfg = self._npc_wind_cfg()
        if not cfg.get("enabled", True):
            return
        tick = int(world.get("tick_no", 0) or 0)
        data = world.setdefault("data", {})
        try:
            active = await self.get_active_events(world["id"], tick)
        except Exception:  # noqa: BLE001 - 风声不阻塞主 tick
            return
        current: dict[str, dict] = {}
        for ev in active:
            name = str(((ev.get("data") or {}).get("name")
                        or ev.get("event_id") or "异象"))
            current[str(ev.get("id"))] = {"name": name,
                                          "event_id": ev.get("event_id")}
        previous = data.get("wind_events")
        first_run = not isinstance(previous, dict)
        if first_run:
            previous = {}
        for row_id, info in current.items():
            if row_id not in previous and not first_run:
                self._add_wind(
                    world,
                    f"「{info['name']}」之兆忽现，江湖坊间议论纷纷。",
                    "event")
        for row_id, info in previous.items():
            if row_id not in current:
                self._add_wind(
                    world,
                    f"「{info.get('name') or '异象'}」之兆渐息，"
                    f"各地重归安宁。",
                    "event")
        data["wind_events"] = current

    # ------------------------------------------------------------------
    # 威胁快照
    # ------------------------------------------------------------------
    async def _threat_snapshot(self, world: dict) -> dict:
        """构建派生快照（§4.1）：share/border/growth/force/inst/tier。"""
        cfg = self._npc_threat_cfg()
        tick = int(world.get("tick_no", 0) or 0)
        tpd = self._ticks_per_day()
        state = self._npc_ai_state(world)
        nodes_list = await self.get_nodes(world["id"])
        nodes = {int(n["id"]): n for n in nodes_list}
        rows = await self._fetchall(
            "SELECT * FROM strategy_factions WHERE world_id=? AND alive=1",
            (world["id"],))
        factions = {int(f["id"]): f
                    for f in self._decode_all("strategy_factions", rows)}
        # 外交主体：NPC + 玩家盟主。联盟成员的外交由盟主代表，
        # 否则同一场战争会同时写在盟主与成员身上，造成 war_at / pair 不对称。
        diplo_factions: dict[int, dict] = {}
        for fid, f in factions.items():
            aid = int(f.get("alliance_id") or 0)
            if aid and aid != fid and aid in factions:
                continue
            diplo_factions[fid] = f
        node_score = (self.main_cfg().get("season") or {}).get("node_score") or {}
        level_bonus = float(cfg["level_bonus"])
        armies = await self.get_armies(world["id"])

        share = R.threat_share_by_faction(nodes_list, node_score, level_bonus)

        # 势力军力（守军 + 野战军团）
        troops: dict[int, int] = {}
        for node in nodes_list:
            fid = int(node.get("owner_faction", 0) or 0)
            if fid <= 0:
                continue
            troops[fid] = troops.get(fid, 0) + int(
                (node.get("npc_garrison") or {}).get("troops", 0) or 0)
        for army in armies:
            fid = int(army.get("faction_id", 0) or 0)
            if fid <= 0:
                continue
            troops[fid] = troops.get(fid, 0) + int(army.get("troops", 0) or 0)

        # 势力据点数量 / 平均民心
        node_count: dict[int, int] = {}
        morale_sum: dict[int, float] = {}
        for node in nodes_list:
            fid = int(node.get("owner_faction", 0) or 0)
            if fid <= 0:
                continue
            node_count[fid] = node_count.get(fid, 0) + 1
            morale_sum[fid] = morale_sum.get(fid, 0.0) + float(
                node.get("morale", 100) or 0)
        avg_morale = {fid: morale_sum.get(fid, 0.0) / max(1, count)
                      for fid, count in node_count.items()}

        # 扩张速度：每 growth_window_ticks 存一个体量快照
        window = max(1, int(cfg["growth_window_ticks"]))
        growth: dict[int, float] = {}
        for fid in factions:
            stat = state["stats"].setdefault(str(fid), {})
            history = stat.get("share_history")
            if not isinstance(history, list):
                history = []
            history = [h for h in history
                       if isinstance(h, dict)
                       and tick - 7 * tpd <= int(h.get("tick", 0) or 0)
                       <= tick]
            past = float(share.get(fid, 0.0))
            for point in history:
                if int(point.get("tick", 0) or 0) <= tick - window:
                    past = float(point.get("share", 0.0) or 0.0)
            last_tick = int(history[-1].get("tick", 0) or 0) if history \
                else -window
            if tick - last_tick >= window:
                history.append({"tick": tick,
                                "share": float(share.get(fid, 0.0))})
            stat["share_history"] = history[-8:]
            growth[fid] = R.threat_growth(
                share.get(fid, 0.0), past, float(cfg["growth_norm"]))

        # 接壤 / 边境暴露度
        border: dict[int, dict[int, float]] = {}
        border_raw: dict[int, dict[int, float]] = {}
        exposed: dict[int, dict[int, float]] = {}
        for obs in factions:
            border[obs], border_raw[obs], exposed[obs] = {}, {}, {}
            for tgt in factions:
                if obs == tgt:
                    continue
                metrics = R.threat_border_metrics(
                    nodes, obs, tgt, node_score, level_bonus,
                    float(cfg["border_norm"]))
                border_raw[obs][tgt] = metrics["raw"]
                exposed[obs][tgt] = metrics["exposed"]
                border[obs][tgt] = metrics["border"]

        # 边境军力：以 O 的据点为源做 1~force_hops 跳 BFS
        hops = max(1, int(cfg["force_hops"]))
        near_troops: dict[int, dict[int, int]] = {}
        force: dict[int, dict[int, float]] = {}
        power_ratio: dict[int, dict[int, float]] = {}
        for obs in factions:
            near_troops[obs], force[obs], power_ratio[obs] = {}, {}, {}
            sources = [nid for nid, n in nodes.items()
                       if int(n.get("owner_faction", 0) or 0) == obs]
            reached: dict[int, int] = {}
            frontier = set(sources)
            seen = set(sources)
            for _ in range(hops):
                nxt: set[int] = set()
                for nid in frontier:
                    for adj in nodes.get(nid, {}).get("adj") or []:
                        adj = int(adj)
                        if adj in seen or adj not in nodes:
                            continue
                        seen.add(adj)
                        nxt.add(adj)
                        owner = int(nodes[adj].get("owner_faction", 0) or 0)
                        if owner > 0 and owner != obs:
                            reached[owner] = reached.get(owner, 0) + int(
                                (nodes[adj].get("npc_garrison") or {})
                                .get("troops", 0) or 0)
                frontier = nxt
                if not frontier:
                    break
            own_border = 0
            tgt_border: dict[int, int] = {}
            for node in nodes.values():
                owner = int(node.get("owner_faction", 0) or 0)
                if owner <= 0:
                    continue
                if not any(int(a) in nodes and int(
                        nodes[int(a)].get("owner_faction", 0) or 0) != owner
                        for a in node.get("adj") or []):
                    continue
                power = int((node.get("npc_garrison") or {})
                            .get("troops", 0) or 0)
                if owner == obs:
                    own_border += power
                else:
                    tgt_border[owner] = tgt_border.get(owner, 0) + power
            for tgt in factions:
                if obs == tgt:
                    continue
                near = reached.get(tgt, 0)
                near_troops[obs][tgt] = near
                force[obs][tgt] = R.threat_force_ratio(
                    near, troops.get(obs, 0), float(cfg["force_norm"]))
                power_ratio[obs][tgt] = own_border / max(
                    1, tgt_border.get(tgt, 0))

        # 霸主 / 合纵
        coalition = (world.get("data") or {}).get("coalition") or {}
        coal_fid = int(coalition.get("fid", 0) or 0) if coalition.get(
            "active") else 0
        threshold = float(cfg["hegemon_share_threshold"])
        hegemon = {fid: (share.get(fid, 0.0) >= threshold or fid == coal_fid)
                   for fid in factions}

        # 龟缩判定（仅玩家势力）
        border_def_sum: dict[int, float] = {}
        border_def_cnt: dict[int, int] = {}
        for node in nodes.values():
            owner = int(node.get("owner_faction", 0) or 0)
            faction = factions.get(owner)
            if not faction or faction.get("kind") != "player":
                continue
            if not any(int(a) in nodes and int(
                    nodes[int(a)].get("owner_faction", 0) or 0) != owner
                    for a in node.get("adj") or []):
                continue
            border_def_sum[owner] = border_def_sum.get(owner, 0.0) + float(
                node.get("defense", 0) or 0)
            border_def_cnt[owner] = border_def_cnt.get(owner, 0) + 1
        turtle: dict[int, bool] = {}
        stat_window = max(1, _safe_int(
            (cfg.get("turtle") or {}).get("window_ticks"), 7 * tpd))
        for fid, faction in factions.items():
            avg_def = border_def_sum.get(fid, 0.0) / max(
                1, border_def_cnt.get(fid, 1))
            tcfg = dict(cfg["turtle"])
            # 霸主占比线来自 threat 顶层，龟缩判定也需使用同一口径
            tcfg.setdefault("hegemon_share_threshold", threshold)
            stat = state["stats"].get(str(fid)) or {}
            recent = dict(stat)
            for key in ("captures_7d", "attacks_7d"):
                vals = stat.get(key)
                if isinstance(vals, list):
                    recent[key] = [x for x in vals
                                   if int(x or 0) >= tick - stat_window]
            turtle[fid] = R.threat_is_turtle(
                recent, avg_def, share.get(fid, 0.0),
                faction.get("kind") == "player",
                hegemon.get(fid, False), tcfg)

        # 即时压力 P_eff
        inst: dict[int, dict[int, float]] = {}
        split: dict[int, dict[int, dict]] = {}
        tier: dict[int, dict[int, str]] = {}
        accum = {int(o): row for o, row in
                 ((o, v) for o, v in (state.get("accum") or {}).items())
                 if str(o).lstrip("-").isdigit()}
        accum = {int(o): (row if isinstance(row, dict) else {})
                 for o, row in accum.items()}
        declare_gate = float(self._npc_war_cfg()["declare_gate"])
        for obs in factions:
            inst[obs], split[obs], tier[obs] = {}, {}, {}
            o_f = factions[obs]
            sens = float(cfg["sensitivity"].get(
                self._personality_key(o_f), 0.85))
            rep_damp = R.threat_reputation_damp(
                int(o_f.get("reputation", 0) or 0),
                float((cfg.get("reputation") or {}).get(
                    "righteous_penalty", 0.05)))
            for tgt in factions:
                if obs == tgt:
                    continue
                rel_key = R.threat_relation_key(R.relation_of(
                    o_f.get("diplomacy"), tgt, _now()))
                rel_damp = float(cfg["relation_damp"].get(rel_key, 1.0))
                favor_damp = R.threat_favor_damp(
                    R.favor_of(o_f.get("diplomacy"), tgt),
                    float(cfg["favor_damp_floor"]))
                p_raw = R.threat_pressure(
                    share.get(tgt, 0.0), border[obs][tgt],
                    growth.get(tgt, 0.0), force[obs][tgt], cfg["weights"])
                p_mod = p_raw * sens * rel_damp * favor_damp * rep_damp
                adj = R.threat_adjacency_factor(
                    border_raw[obs][tgt], hegemon.get(tgt, False),
                    float(cfg["nonadj_factor"]),
                    float(cfg["hegemon_nonadj_factor"]))
                p_eff = p_mod * adj
                if hegemon.get(tgt, False):
                    floor_raw = R.threat_public_floor(
                        share.get(tgt, 0.0), threshold,
                        float(cfg["public_base"]), float(cfg["public_span"]))
                    if rel_key in ("ally", "truce"):
                        floor = floor_raw * rel_damp * favor_damp * rep_damp
                    else:
                        floor = max(
                            floor_raw * favor_damp * rep_damp,
                            declare_gate + float(cfg["public_floor_margin"]))
                    p_eff = max(p_eff, floor)
                if turtle.get(tgt):
                    p_eff *= float(cfg["turtle"]["threat_damp"])
                inst[obs][tgt] = p_eff
                split[obs][tgt] = {
                    "size": share.get(tgt, 0.0),
                    "border": border[obs][tgt],
                    "growth": growth.get(tgt, 0.0),
                    "force": force[obs][tgt],
                }
                tier[obs][tgt] = R.threat_tier(
                    float((accum.get(obs) or {}).get(str(tgt), 0.0) or 0.0),
                    cfg["tiers"])

        return {
            "tick": tick,
            "cfg": cfg,
            "factions": factions,
            "diplo_factions": diplo_factions,
            "nodes": nodes,
            "share": share,
            "node_count": node_count,
            "troops": troops,
            "avg_morale": avg_morale,
            "growth": growth,
            "border": border,
            "border_raw": border_raw,
            "exposed": exposed,
            "near_troops": near_troops,
            "force": force,
            "power_ratio": power_ratio,
            "hegemon": hegemon,
            "turtle": turtle,
            "inst": inst,
            "split": split,
            "tier": tier,
            "accum": {obs: {int(k): float(v or 0.0)
                            for k, v in (row or {}).items()}
                      for obs, row in accum.items()},
        }

    def _update_threat_accum(self, world: dict, snap: dict, tick: int) -> None:
        """累计敌意 ``H`` 收敛（§5.10），并回写 ``world.data.threat``。"""
        cfg = snap["cfg"]
        state = self._npc_ai_state(world)
        accum = state.setdefault("accum", {})
        for okey in list(accum.keys()):
            if not isinstance(accum.get(okey), dict):
                accum.pop(okey, None)
        for obs, row in snap["inst"].items():
            out = accum.setdefault(str(obs), {})
            for tgt, p_eff in row.items():
                prev = _safe_float(out.get(str(tgt)), 0.0)
                alpha = R.threat_alpha(
                    p_eff, prev, snap["border_raw"][obs][tgt],
                    snap["hegemon"].get(tgt, False),
                    float(cfg["alpha_up_adj"]),
                    float(cfg["alpha_up_nonadj"]),
                    float(cfg["alpha_up_hegemon_nonadj"]),
                    float(cfg["alpha_down"]))
                out[str(tgt)] = round(
                    R.threat_accumulate(prev, p_eff, alpha), 2)
                snap["tier"][obs][tgt] = R.threat_tier(
                    out[str(tgt)], cfg["tiers"])
        # 只保留存活势力的累计敌意：灭亡势力的 H 若残留，会污染
        # third_party（战争疲劳）与天下视图的对外关系。
        alive = set(snap["factions"])
        # 灭亡势力的龟缩统计 / 体量历史同样清理（长赛季反复灭国时不残留）
        stats = state.get("stats")
        if isinstance(stats, dict):
            for skey in list(stats.keys()):
                try:
                    sid = int(skey)
                except (TypeError, ValueError):
                    stats.pop(skey, None)
                    continue
                if sid not in alive:
                    stats.pop(skey, None)
        for okey in list(accum.keys()):
            try:
                oid = int(okey)
            except (TypeError, ValueError):
                accum.pop(okey, None)
                continue
            row = accum.get(okey)
            if oid not in alive or not isinstance(row, dict):
                accum.pop(okey, None)
                continue
            for tkey in list(row.keys()):
                try:
                    tid = int(tkey)
                except (TypeError, ValueError):
                    row.pop(tkey, None)
                    continue
                if tid not in alive or tid == oid:
                    row.pop(tkey, None)
        snap["accum"] = {
            int(o): {int(k): float(v or 0.0) for k, v in (row or {}).items()}
            for o, row in accum.items()}
        public: dict[int, float] = {}
        for tgt in snap["factions"]:
            public[tgt] = max(
                [float((accum.get(str(o)) or {}).get(str(tgt), 0.0) or 0.0)
                 for o in snap["factions"] if o != tgt] or [0.0])
        snap["public"] = public
        data = world.setdefault("data", {})
        data["threat"] = {
            "tick": int(tick),
            "share": {str(fid): round(float(v), 4)
                      for fid, v in (snap.get("share") or {}).items()},
            "inst": {str(o): {str(t): round(float(v), 2)
                              for t, v in row.items()}
                     for o, row in snap["inst"].items()},
            "accum": {str(o): {str(t): round(float(v), 2)
                               for t, v in row.items()}
                      for o, row in snap["accum"].items()},
            "tier": {str(o): {str(t): v for t, v in row.items()}
                     for o, row in snap["tier"].items()},
            "split": {str(o): {str(t): {k: round(float(x), 3)
                                        for k, x in parts.items()}
                               for t, parts in row.items()}
                      for o, row in snap["split"].items()},
            "public": {str(t): round(float(v), 2) for t, v in public.items()},
        }

    def _update_pair_phases(self, world: dict, snap: dict, tick: int) -> None:
        """按 ``H`` 改写档位 `phase`（带 5 点滞回，§4.3）。"""
        state = self._npc_ai_state(world)
        tiers_cfg = snap["cfg"]["tiers"]
        factions = snap["factions"]
        diplo = snap.get("diplo_factions") or factions
        war_cfg = self._npc_war_cfg()
        for obs in diplo:
            for tgt in diplo:
                if obs == tgt:
                    continue
                pair = self._pair(state, obs, tgt)
                if pair["phase"] not in TIER_PHASES:
                    continue
                h = float(snap["accum"].get(obs, {}).get(tgt, 0.0) or 0.0)
                target_phase = R.threat_tier(h, tiers_cfg)
                if target_phase == "coalition":
                    target_phase = "hostile"
                order = list(TIER_PHASES)
                cur_idx = order.index(pair["phase"])
                new_idx = order.index(target_phase)
                if new_idx < cur_idx:
                    # 退出线跟随配置（默认 20/40/60），不要写死旧常量
                    entry = float(tiers_cfg.get(
                        pair["phase"], TIER_ENTRY.get(pair["phase"], 0)))
                    if h >= entry - TIER_HYSTERESIS:
                        continue
                if target_phase == pair["phase"]:
                    continue
                pair["phase"] = target_phase
                if target_phase == "hostile":
                    pair["hostile_since_tick"] = tick
                    pair["last_offer_tick"] = 0
                    last_warn = int(pair.get("last_warning_tick", 0) or 0)
                    if tick - last_warn >= int(
                            war_cfg["warning_cooldown_ticks"]):
                        pair["last_warning_tick"] = tick
                        if factions[tgt].get("kind") == "player":
                            self._add_wind(
                                world,
                                f"「{factions[obs].get('name')}」厉兵秣马，"
                                f"兵锋似指「{factions[tgt].get('name')}」。",
                                "war")
                        else:
                            self._add_wind(
                                world,
                                f"「{factions[obs].get('name')}」与"
                                f"「{factions[tgt].get('name')}」之间的关系"
                                f"骤然紧张。",
                                "war")
                elif target_phase == "calm":
                    pair["hostile_since_tick"] = 0
        state["tick"] = tick

    def _expire_war_states(self, world: dict, snap: dict, tick: int) -> None:
        """停战到期、冷却到期、狂热提前结束、关系与 pair 对齐。"""
        state = self._npc_ai_state(world)
        factions = snap["factions"]
        diplo = snap.get("diplo_factions") or factions
        war_cfg = self._npc_war_cfg()
        now = _now()

        def enter_war(pair: dict) -> None:
            """按正式战争初始化势力对状态（供外部写入 REL_WAR 时对齐）。"""
            pair.update({
                "phase": "war",
                "war_start_tick": tick,
                "last_capture_tick": tick,
                "war_score": 50,
                "exhaustion": 0.0,
                "goal": "border",
                "frenzy_until_tick": 0,
                "last_offer_tick": 0,
                "ceasefire_until_tick": 0,
                "captures_by_me": 0,
                "captures_by_target": 0,
                "war_start_troops": int(snap["troops"].get(obs, 0) or 0),
                "war_start_nodes": int(
                    snap["node_count"].get(obs, 0) or 0),
            })

        for obs in diplo:
            for tgt in diplo:
                if obs == tgt:
                    continue
                pair = self._pair(state, obs, tgt)
                rel = R.relation_of(factions[obs].get("diplomacy"), tgt, now)
                phase = pair["phase"]
                if phase == "war":
                    if rel not in (R.REL_WAR, "war_pending"):
                        pair["phase"] = "cooldown"
                        pair["no_declare_until_tick"] = max(
                            int(pair.get("no_declare_until_tick", 0) or 0),
                            tick + int(war_cfg["post_war_cooldown_ticks"]))
                        continue
                    if tick < int(pair.get("frenzy_until_tick", 0) or 0):
                        f = factions[obs]
                        frenzy = war_cfg["frenzy"]
                        treasury = self._npc_treasury(f)
                        reserve = self._npc_reserve(f)
                        troops = int(snap["troops"].get(obs, 0) or 0)
                        morale = _num(snap["avg_morale"].get(obs), 100)
                        if (int(treasury["coin"]) < int(reserve["coin"])
                                or int(treasury["grain"]) < int(
                                    reserve["grain"])
                                or troops < int(frenzy["min_troops"])
                                or morale < float(frenzy["morale_break"])):
                            pair["frenzy_until_tick"] = tick
                elif phase == "truce":
                    if rel == R.REL_WAR:
                        # 停战期被撕毁 / 玩家重新宣战：关系是权威，立即回到战争态
                        enter_war(pair)
                        continue
                    expire_tick = int(pair.get("truce_until_tick", 0) or 0)
                    if (expire_tick and tick >= expire_tick) or \
                            (not expire_tick and rel != R.REL_TRUCE):
                        pair["phase"] = "cooldown"
                        pair["no_declare_until_tick"] = max(
                            int(pair.get("no_declare_until_tick", 0) or 0),
                            tick + int(war_cfg["post_war_cooldown_ticks"]))
                elif phase == "cooldown":
                    if rel == R.REL_WAR:
                        # 冷却期内被玩家宣战（既有玩法允许撕约/直接宣战）
                        enter_war(pair)
                    elif tick >= int(
                            pair.get("no_declare_until_tick", 0) or 0):
                        pair["phase"] = "calm"
                        pair["hostile_since_tick"] = 0
                elif phase in TIER_PHASES and rel == R.REL_WAR:
                    # 备战期（war_pending）不建战争态：war_at 到期后关系才变 war
                    enter_war(pair)
                ceasefire = int(pair.get("ceasefire_until_tick", 0) or 0)
                if ceasefire and tick >= ceasefire:
                    pair["ceasefire_until_tick"] = 0

        # 只保留存活外交主体之间的 pair（成员势力 / 已灭亡势力清理）
        alive_keys = {str(fid) for fid in diplo}
        pairs = state.get("pairs")
        if isinstance(pairs, dict):
            for okey in list(pairs.keys()):
                if str(okey) not in alive_keys:
                    pairs.pop(okey, None)
                    continue
                row = pairs.get(okey)
                if not isinstance(row, dict):
                    pairs.pop(okey, None)
                    continue
                for tkey in list(row.keys()):
                    if str(tkey) not in alive_keys or str(tkey) == str(okey):
                        row.pop(tkey, None)

    # ------------------------------------------------------------------
    # 宣战
    # ------------------------------------------------------------------
    async def _step_npc_diplomacy(self, world: dict) -> None:
        """威胁收敛 + 档位推进 + NPC 自主宣战（§7.11）。"""
        cfg = self._npc_threat_cfg()
        if not cfg.get("enabled", True):
            return
        # 旧档 / 损坏字段先自愈，避免 int()/float() 抛错导致 tick 永久回滚
        self._sanitize_npc_ai_state(world)
        tick = int(world.get("tick_no", 0) or 0)
        snap = await self._threat_snapshot(world)
        self._update_threat_accum(world, snap, tick)
        self._update_pair_phases(world, snap, tick)
        self._expire_war_states(world, snap, tick)

        war_cfg = self._npc_war_cfg()
        state = self._npc_ai_state(world)
        tpd = self._ticks_per_day()
        day = tick // tpd
        declarations = state.get("declarations")
        # 注意不能写 ``day or -1``：开局第 0 天是合法值，会被当成空值而
        # 每 tick 重置，导致首日宣战上限（max_declarations_per_day）失效。
        try:
            stored_day = int(declarations.get("day", -1)) \
                if isinstance(declarations, dict) else -1
        except (TypeError, ValueError):
            stored_day = -1
        if stored_day != day:
            declarations = {"day": day, "count": 0}
        declared_this_tick = 0
        factions = snap.get("diplo_factions") or snap["factions"]
        npc_ids = [fid for fid, f in factions.items()
                   if str(f.get("kind") or "") == "npc"]
        gate = float(war_cfg["declare_gate"])

        for observer_id in npc_ids:
            observer = factions[observer_id]
            pairs = state["pairs"].setdefault(str(observer_id), {})
            active_wars = sum(
                1 for other in factions if other != observer_id
                and (pairs.get(str(other)) or {}).get("phase") == "war")
            if active_wars >= int(war_cfg["max_active_wars_per_npc"]):
                continue
            candidates = []
            for target_id in factions:
                if target_id == observer_id:
                    continue
                target = factions[target_id]
                pair = self._pair(state, observer_id, target_id)
                if pair["phase"] != "hostile":
                    continue
                if tick - int(pair.get("hostile_since_tick", 0) or 0) < \
                        int(war_cfg["hostile_hold_ticks"]):
                    continue
                h = float(snap["accum"].get(observer_id, {})
                          .get(target_id, 0.0) or 0.0)
                if h < gate:
                    continue
                if not await self._can_declare(
                        world, snap, observer, target, pair, tick):
                    continue
                goal = await self._choose_goal(
                    world, snap, observer, target, pair)
                if not goal:
                    continue
                priority = R.threat_priority(
                    h, snap["border_raw"][observer_id][target_id],
                    snap["hegemon"].get(target_id, False),
                    power_ratio=snap["power_ratio"][observer_id][target_id],
                    turtle=snap["turtle"].get(target_id, False),
                    active_wars=active_wars,
                    turtle_penalty=float(cfg["turtle"]["priority_penalty"]))
                candidates.append((priority, target_id, pair, goal))
            if not candidates:
                continue
            candidates.sort(key=lambda item: -item[0])
            _, target_id, pair, goal = candidates[0]
            target = factions[target_id]
            h = float(snap["accum"].get(observer_id, {})
                      .get(target_id, 0.0) or 0.0)
            power_ratio = float(
                snap["power_ratio"][observer_id][target_id] or 0.0)
            key = self._personality_key(observer)
            p_day = R.threat_declare_chance(
                h, gate,
                float(war_cfg["declare_base_per_day"]),
                float(war_cfg["declare_span_per_day"]),
                float(war_cfg["max_p_day"]),
                float((war_cfg["goal_mult"] or {}).get(goal, 1.0)),
                float((war_cfg["personality_war_bias"] or {}).get(key, 1.0)),
                power_ratio,
                float(war_cfg["min_power_ratio"]),
                bool(war_cfg["allow_suicide_declare"]),
                float(war_cfg["suicide_declare_mult"]),
                snap["turtle"].get(target_id, False),
                float(cfg["turtle"]["declare_mult"]))
            if int(snap["node_count"].get(observer_id, 0) or 0) <= \
                    int(war_cfg["desperation_nodes"]):
                p_day = min(float(war_cfg["max_p_day"]),
                            p_day * float(war_cfg["desperation_mult"]))
            p_tick = R.per_day_to_per_tick(p_day, tpd)
            rng = self._rng("npc_declare", tick, observer_id, target_id)
            if rng.random() >= p_tick:
                continue
            if declared_this_tick >= int(
                    war_cfg["max_declarations_per_tick"]):
                continue
            if int(declarations.get("count", 0) or 0) >= int(
                    war_cfg["max_declarations_per_day"]):
                continue
            await self._declare_npc_war(
                world, snap, observer, target, pair, goal)
            declared_this_tick += 1
            declarations["count"] = int(declarations.get("count", 0) or 0) + 1

        state["declarations"] = declarations
        await self.save_world(world)
        await self._commit()

    async def _target_newbie_protected(self, world: dict, target: dict) -> bool:
        """宣战目标是否处于新手保护期（§7.5 第 4 条 / §9）。

        与 ``_check_attack_target`` 口径一致：以 ``strategy_players.created_at``
        为准，``newcomer.opt_out`` 可主动放弃。玩家联盟按盟主 + 成员全体判断，
        避免 NPC 绕过新手保护去打盟内新玩家。

        节点保护期（``protect_until``）不阻止宣战，只阻止战术层选该节点。
        """
        if str(target.get("kind") or "") != "player":
            return False
        days = int((self.main_cfg().get("protection") or {}).get(
            "newbie_days", 4) or 0)
        if days <= 0:
            return False
        rows = await self._fetchall(
            "SELECT p.created_at, p.newcomer_json FROM strategy_players p "
            "JOIN strategy_factions f ON f.world_id=p.world_id "
            "AND f.id=p.faction_id "
            "WHERE p.world_id=? AND (f.id=? OR f.alliance_id=?)",
            (int(world["id"]), int(target["id"]), int(target["id"])))
        now = _now()
        for row in rows or []:
            joined = int(row.get("created_at", 0) or 0)
            if not joined or now - joined >= days * 86400:
                continue
            try:
                newcom = json.loads(row.get("newcomer_json") or "{}")
            except (TypeError, ValueError):
                newcom = {}
            if not isinstance(newcom, dict):
                newcom = {}
            if not newcom.get("opt_out"):
                return True
        return False

    async def _can_declare(self, world: dict, snap: dict, observer: dict,
                           target: dict, pair: dict, tick: int) -> bool:
        """宣战前置校验（§7.5）。"""
        war_cfg = self._npc_war_cfg()
        now = _now()
        rel = R.relation_of(observer.get("diplomacy"), target["id"], now)
        if rel in (R.REL_ALLY, R.REL_TRUCE, R.REL_WAR, "war_pending"):
            return False
        if int(target.get("alive", 0) or 0) <= 0:
            return False
        if tick < int(pair.get("no_declare_until_tick", 0) or 0):
            return False
        last = int(pair.get("last_declare_tick", 0) or 0)
        if last and tick - last < int(war_cfg["declare_cooldown_ticks"]):
            return False
        if await self._target_newbie_protected(world, target):
            return False
        border_raw = float(snap["border_raw"][observer["id"]][target["id"]])
        hegemon = bool(snap["hegemon"].get(target["id"]))
        if border_raw <= 0:
            if not hegemon:
                return False
            exp = await self._npc_expedition_target(
                world, snap, observer, target)
            if not exp:
                return False
        return True

    async def _choose_goal(self, world: dict, snap: dict, observer: dict,
                           target: dict, pair: dict) -> str:
        """宣战目标类型（§7.1）。"""
        if snap["border_raw"][observer["id"]][target["id"]] > 0:
            return "border"
        if snap["hegemon"].get(target["id"]):
            return "hegemon"
        return ""

    async def _declare_npc_war(self, world: dict, snap: dict, observer: dict,
                               target: dict, pair: dict, goal: str) -> None:
        """执行宣战（§7.6）：立即开战 + 进入战争狂热。"""
        dcfg = self.main_cfg().get("diplomacy") or {}
        war_cfg = self._npc_war_cfg()
        tick = int(world.get("tick_no", 0) or 0)
        tph = self._ticks_per_hour()
        await self._apply_diplomacy_deal(observer, target, "war", 0, dcfg)
        for side, other in ((observer, target), (target, observer)):
            dip = side.setdefault("diplomacy", {})
            dip.setdefault(str(int(other["id"])), {})["war_at"] = 0
        # 玩家联盟：上面的 war_at=0 只改了盟主字典，需再同步一次到成员，
        # 否则盟友看到的是 24h 备战期，被打了却暂时无法反击。
        await self._sync_alliance_meta(target)
        frenzy_ticks = int(float(war_cfg["frenzy"]["hours"]) * tph)
        state = self._npc_ai_state(world)
        # 防守方不继承进攻方的 hegemon 目标，避免反方向误触发跨区远征
        reverse_goal = "border" if float(
            snap["border_raw"][target["id"]][observer["id"]]) > 0 else ""
        for obs_id, tgt_id, me, row_goal in (
                (observer["id"], target["id"], observer, goal),
                (target["id"], observer["id"], target, reverse_goal)):
            row = self._pair(state, obs_id, tgt_id)
            row.update({
                "phase": "war",
                "war_start_tick": tick,
                "last_declare_tick": tick,
                "frenzy_until_tick": tick + frenzy_ticks,
                "goal": row_goal,
                "war_score": 50,
                "exhaustion": 0.0,
                "last_capture_tick": tick,
                "last_offer_tick": 0,
                "captures_by_me": 0,
                "captures_by_target": 0,
                "ceasefire_until_tick": 0,
                "truce_until_tick": 0,
                "war_start_troops": int(snap["troops"].get(me["id"], 0) or 0),
                "war_start_nodes": int(
                    snap["node_count"].get(me["id"], 0) or 0),
            })
        self._add_wind(
            world,
            f"⚔️「{observer.get('name')}」以"
            f"{GOAL_CN.get(goal, '兵锋')}为名，"
            f"向「{target.get('name')}」宣战！",
            "war")
        if str(target.get("kind") or "") == "player":
            # 玩家联盟：通知盟主与全体成员（成员玩家 faction_id 仍是成员势力）
            rows = await self._fetchall(
                "SELECT user_id FROM strategy_players WHERE world_id=? "
                "AND faction_id IN (SELECT id FROM strategy_factions "
                "WHERE world_id=? AND (id=? OR alliance_id=?))",
                (world["id"], world["id"], int(target["id"]),
                 int(target["id"])))
            await self._notify(
                world, [r["user_id"] for r in rows], "war_declared",
                {"faction_id": int(observer["id"]),
                 "faction_name": observer.get("name"),
                 "goal": goal})
        await self.save_faction(observer)
        await self.save_faction(target)
        await self.save_world(world)

    # ------------------------------------------------------------------
    # 远征
    # ------------------------------------------------------------------
    async def _npc_expedition_target(self, world: dict, snap: dict,
                                     observer: dict,
                                     target: dict) -> Optional[dict]:
        """非接壤霸主远征目标（§7.8）：返回可直达的目标节点。

        直接在 ``snap["nodes"]`` 上做一次有界 BFS（最近目标据点 = 最短路），
        不再对每个己方据点重复 ``get_nodes`` + ``find_path``，避免每 tick
        成倍的关系库查询。
        """
        cfg = self._npc_war_cfg()["expedition"]
        tick = int(world.get("tick_no", 0) or 0)
        state = self._npc_ai_state(world)
        pair = self._pair(state, observer["id"], target["id"])
        if tick - int(pair.get("last_expedition_tick", 0) or 0) < \
                int(cfg["cooldown_ticks"]):
            return None
        nodes = snap.get("nodes") or {}
        target_id = int(target["id"])
        max_edges = int(cfg["max_edges"])
        now = _now()
        own_nodes = [n for n in nodes.values()
                     if int(n.get("owner_faction", 0) or 0) ==
                     int(observer["id"])
                     and int((n.get("npc_garrison") or {})
                             .get("troops", 0) or 0) >= int(
                                 cfg["min_troops"])]
        if not own_nodes:
            return None
        from collections import deque
        best: Optional[dict] = None
        best_len = max_edges + 1
        for node in own_nodes:
            src = int(node["id"])
            q = deque([(src, 0)])
            seen = {src}
            found, found_len = 0, best_len
            while q:
                cur, dist = q.popleft()
                if dist >= max_edges or dist + 1 >= found_len:
                    continue
                for nxt in nodes.get(cur, {}).get("adj") or []:
                    nxt = int(nxt)
                    if nxt in seen or nxt not in nodes:
                        continue
                    nxt_node = nodes[nxt]
                    if int(nxt_node.get("owner_faction", 0) or 0) == \
                            target_id and int(
                                nxt_node.get("protect_until", 0) or 0) <= now:
                        found, found_len = nxt, dist + 1
                        break
                    seen.add(nxt)
                    q.append((nxt, dist + 1))
                if found:
                    break
            if found and found_len < best_len:
                best_len = found_len
                best = dict(nodes.get(found) or {})
                best.setdefault("id", found)
                best["_from_node"] = src
        return best

    # ------------------------------------------------------------------
    # 停战
    # ------------------------------------------------------------------
    async def _step_npc_peace(self, world: dict) -> None:
        """战争疲劳、NPC→NPC 停战、NPC→玩家求和（§8.8）。"""
        cfg = self._npc_peace_cfg()
        dcfg = self.main_cfg().get("diplomacy") or {}
        self._sanitize_npc_ai_state(world)
        tick = int(world.get("tick_no", 0) or 0)
        tpd = self._ticks_per_day()
        tph = self._ticks_per_hour()
        snap = await self._threat_snapshot(world)
        state = self._npc_ai_state(world)
        factions = snap.get("diplo_factions") or snap["factions"]
        npc_ids = [fid for fid, f in factions.items()
                   if str(f.get("kind") or "") == "npc"]
        for observer_id in npc_ids:
            observer = factions[observer_id]
            for target_id in factions:
                if target_id == observer_id:
                    continue
                target = factions[target_id]
                pair = self._pair(state, observer_id, target_id)
                if pair["phase"] != "war":
                    continue
                self._update_war_state(world, snap, observer, target, pair)
                if tick < int(pair.get("frenzy_until_tick", 0) or 0):
                    continue
                elapsed = tick - int(pair.get("war_start_tick", 0) or 0)
                forced = elapsed >= int(cfg["max_war_ticks"])
                if elapsed < int(cfg["min_war_ticks"]):
                    continue
                if not forced and float(pair.get("exhaustion", 0) or 0) < \
                        float(cfg["peace_gate"]):
                    continue
                key = self._personality_key(observer)
                p_day = R.threat_peace_chance(
                    float(pair.get("exhaustion", 0) or 0),
                    float(cfg["peace_gate"]),
                    float(cfg["peace_base_per_day"]),
                    float(cfg["peace_span_per_day"]),
                    float(cfg["max_peace_p_day"]),
                    float((cfg["personality_peace_bias"] or {}).get(key, 1.0)),
                    _num(pair.get("war_score"), 50))
                p_tick = R.per_day_to_per_tick(p_day, tpd)
                rng = self._rng("npc_peace", tick, observer_id, target_id)
                if rng.random() >= p_tick:
                    continue
                if str(target.get("kind") or "") == "npc":
                    reverse = self._pair(state, target_id, observer_id)
                    favor = R.favor_of(target.get("diplomacy"), observer_id)
                    accept = R.threat_peace_accept(
                        float(reverse.get("exhaustion", 0) or 0), favor,
                        _num(pair.get("war_score"), 50),
                        _num(reverse.get("war_score"), 50),
                        float(cfg["accept_min"]),
                        float(cfg["accept_max"]))
                    if rng.random() < accept:
                        truce_ticks = int(
                            float(dcfg.get("truce_hours", 48)) * tph)
                        await self._apply_diplomacy_deal(
                            observer, target, "truce", 0, dcfg)
                        for obs_id, tgt_id in (
                                (observer_id, target_id),
                                (target_id, observer_id)):
                            row = self._pair(state, obs_id, tgt_id)
                            row.update({
                                "phase": "truce",
                                "truce_until_tick": tick + truce_ticks,
                                "no_declare_until_tick": max(
                                    tick + truce_ticks,
                                    tick + int(
                                        cfg["post_war_cooldown_ticks"])),
                                "ceasefire_until_tick": 0,
                                "exhaustion": 0.0,
                                "war_score": 50,
                                "goal": "",
                            })
                        self._add_wind(
                            world,
                            f"🕊️「{observer.get('name')}」与"
                            f"「{target.get('name')}」罢兵休战，"
                            f"边境重归平静。",
                            "peace")
                    else:
                        pair["last_offer_tick"] = tick
                else:
                    last_offer = int(pair.get("last_offer_tick", 0) or 0)
                    # 0 表示本场战争还没提过和：不应被当成“tick 0 提过”
                    if last_offer and tick - last_offer < int(
                            cfg["offer_cooldown_ticks"]):
                        continue
                    await self._npc_send_truce_offer(
                        world, observer, target, pair)
                    pair["ceasefire_until_tick"] = tick + int(
                        float(cfg["ceasefire_hours"]) * tph)
                    pair["last_offer_tick"] = tick
        await self.save_world(world)
        await self._commit()

    def _update_war_state(self, world: dict, snap: dict, observer: dict,
                          target: dict, pair: dict) -> None:
        """更新 ``war_score`` 与 ``exhaustion``（§8.1 / §8.2）。"""
        cfg = self._npc_peace_cfg()
        tick = int(world.get("tick_no", 0) or 0)
        state = self._npc_ai_state(world)
        obs_id, tgt_id = int(observer["id"]), int(target["id"])
        current_troops = float(snap["troops"].get(obs_id, 0) or 0)
        start_troops = float(pair.get("war_start_troops", 0) or 0)
        if start_troops <= 0:
            start_troops = current_troops
            pair["war_start_troops"] = int(current_troops)
        troop_loss = 1.0 - current_troops / max(1.0, start_troops)
        treasury = self._npc_treasury(observer)
        reserve = self._npc_reserve(observer)
        treasury_stress = max(
            0.0, (float(reserve["coin"]) - float(treasury["coin"]))
            / max(1.0, float(reserve["coin"])))
        morale = _num(snap["avg_morale"].get(obs_id), 100)
        morale_stress = max(0.0, (50.0 - morale) / 50.0)
        no_progress = min(1.0, (tick - int(
            pair.get("last_capture_tick", 0) or 0))
            / max(1.0, float(cfg["no_progress_norm"])))
        accum = snap["accum"].get(obs_id, {})
        h_target = float(accum.get(tgt_id, 0.0) or 0.0)
        others = [float(v or 0.0) for k, v in accum.items() if k != tgt_id]
        third_party = R.clamp01((max(others or [0.0]) - h_target) / 50.0)
        start_nodes = int(pair.get("war_start_nodes", 0) or 0)
        cur_nodes = int(snap["node_count"].get(obs_id, 0) or 0)
        if start_nodes <= 0:
            start_nodes = cur_nodes
            pair["war_start_nodes"] = cur_nodes
        lost_nodes = min(3, max(0, start_nodes - cur_nodes)) / 3.0
        capital_id = int(observer.get("capital_node_id", 0) or 0)
        lost_capital = bool(
            capital_id and int((snap["nodes"].get(capital_id) or {})
                               .get("owner_faction", 0) or 0) != obs_id)
        tgt_capital = int(target.get("capital_node_id", 0) or 0)
        capital_taken = bool(
            tgt_capital and int((snap["nodes"].get(tgt_capital) or {})
                                .get("owner_faction", 0) or 0) == obs_id)
        war_score = R.threat_war_score(
            int(pair.get("captures_by_me", 0) or 0),
            int(pair.get("captures_by_target", 0) or 0),
            capital_taken)
        pair["war_score"] = round(war_score, 1)
        pair["exhaustion"] = round(R.threat_exhaustion(
            troop_loss, treasury_stress, morale_stress, no_progress,
            third_party, lost_nodes, lost_capital, war_score), 1)
        state["tick"] = tick

    async def _npc_send_truce_offer(self, world: dict, observer: dict,
                                    target: dict, pair: dict) -> None:
        """NPC → 玩家停战提案（§8.6，沿用 propose_* 契约）。"""
        cfg = self._npc_peace_cfg()
        expire = _now() + int(float(cfg["ceasefire_hours"]) * 3600)
        f_dip = observer.setdefault("diplomacy", {})
        t_dip = target.setdefault("diplomacy", {})
        self._set_relation(
            t_dip, observer["id"], "propose_truce", until=expire)
        entry = t_dip.setdefault(str(int(observer["id"])), {})
        entry["from"] = int(observer["id"])
        entry["indemnity"] = 0
        self._set_relation(
            f_dip, target["id"], "propose_truce_out", until=expire)
        await self.save_faction(observer)
        await self.save_faction(target)
        await self._sync_alliance_meta(observer)
        await self._sync_alliance_meta(target)
        await self._commit()
        self._add_wind(
            world,
            f"🕊️「{observer.get('name')}」遣使向"
            f"「{target.get('name')}」求和，愿休兵止戈。",
            "peace")
        rows = await self._fetchall(
            "SELECT user_id FROM strategy_players WHERE world_id=? "
            "AND faction_id IN (SELECT id FROM strategy_factions "
            "WHERE world_id=? AND (id=? OR alliance_id=?))",
            (world["id"], world["id"], int(target["id"]),
             int(target["id"])))
        await self._notify(
            world, [r["user_id"] for r in rows], "peace_offer",
            {"faction_id": int(observer["id"]),
             "faction_name": observer.get("name")})

    async def _sync_npc_pair_after_truce(self, world: dict, f: dict,
                                         tf: dict) -> None:
        """停战生效后同步双方 pair（由外交应答 / 提案接受调用）。"""
        if str(f.get("kind") or "") != "npc" and \
                str(tf.get("kind") or "") != "npc":
            return
        cfg = self._npc_war_cfg()
        dcfg = self.main_cfg().get("diplomacy") or {}
        tick = int(world.get("tick_no", 0) or 0)
        tph = self._ticks_per_hour()
        truce_ticks = int(float(dcfg.get("truce_hours", 48)) * tph)
        state = self._npc_ai_state(world)
        for obs_id, tgt_id in ((int(f["id"]), int(tf["id"])),
                               (int(tf["id"]), int(f["id"]))):
            pair = self._pair(state, obs_id, tgt_id)
            pair.update({
                "phase": "truce",
                "truce_until_tick": tick + truce_ticks,
                "no_declare_until_tick": max(
                    tick + truce_ticks,
                    tick + int(cfg["post_war_cooldown_ticks"])),
                "ceasefire_until_tick": 0,
                "exhaustion": 0.0,
                "war_score": 50,
                "goal": "",
            })
        await self.save_world(world)

    # ------------------------------------------------------------------
    # 记录与查询
    # ------------------------------------------------------------------
    def _note_threat_capture(self, world: dict, prev_owner: int,
                             new_owner: int, node_name: str = "") -> None:
        """据点易主：更新战争战果 / 龟缩统计，并写一条风声。

        ``prev_owner == 0``（占无主/匪寨）也记入攻占方 ``captures_7d``，
        否则玩家可以靠不断占空地规避龟缩判定。
        """
        prev_owner, new_owner = int(prev_owner or 0), int(new_owner or 0)
        if new_owner <= 0 or prev_owner == new_owner:
            return
        tick = int(world.get("tick_no", 0) or 0)
        state = self._npc_ai_state(world)
        if prev_owner > 0:
            pair = self._pair(state, new_owner, prev_owner)
            if pair.get("phase") == "war":
                pair["captures_by_me"] = _safe_int(
                    pair.get("captures_by_me")) + 1
                pair["captures_by_target"] = _safe_int(
                    pair.get("captures_by_target"))
                pair["last_capture_tick"] = tick
            reverse = self._pair(state, prev_owner, new_owner)
            if reverse.get("phase") == "war":
                reverse["captures_by_target"] = _safe_int(
                    reverse.get("captures_by_target")) + 1
                reverse["captures_by_me"] = _safe_int(
                    reverse.get("captures_by_me"))
            # 战果即时反映到双方 war_score（NPC 方下一 tick 会再按首都修正）
            for side in (pair, reverse):
                if side.get("phase") == "war":
                    side["war_score"] = round(R.threat_war_score(
                        _safe_int(side.get("captures_by_me")),
                        _safe_int(side.get("captures_by_target"))), 1)
        window = max(1, _safe_int(
            (self._npc_threat_cfg().get("turtle") or {}).get("window_ticks"),
            7 * self._ticks_per_day()))
        # 只给攻占方记「主动攻击」；被攻占方不是攻击者，不能因此失去龟缩资格
        stats = state["stats"]
        stat = stats.get(str(new_owner))
        if not isinstance(stat, dict):
            stat = stats[str(new_owner)] = {}
        for key in ("captures_7d", "attacks_7d"):
            raw_items = stat.get(key)
            items = []
            for x in (raw_items if isinstance(raw_items, list) else []):
                value = _safe_int(x, -1)
                if value >= 0 and value >= tick - window:
                    items.append(value)
            items.append(tick)
            stat[key] = items[-32:]
        if prev_owner > 0:
            self._add_wind(
                world,
                f"🏴「{node_name or '边镇'}」易主，归入新主旗下。",
                "battle")

    def _npc_tactical_new_war_allowed(self, relation: str,
                                      active_wars: int) -> bool:
        """战术层能否对非战争目标「不宣而战」（v4.6.1 槽位预留）。

        威胁外交层启用时，``max_active_wars_per_npc`` 至少预留 1 个槽给
        正式宣战；否则 NPC 开局用突袭占满所有槽位，威胁层永远走不到
        ``hostile → war``，自主宣战不会发生。威胁层关闭时保持旧行为。
        """
        if str(relation or "") == R.REL_WAR:
            return True
        threat_cfg = self._npc_threat_cfg()
        if not bool(threat_cfg.get("enabled", True)):
            return True
        war_cfg = self._npc_war_cfg()
        cap = max(0, int(war_cfg["max_active_wars_per_npc"]) - 1)
        return int(active_wars) < cap

    def _npc_frenzy_state(self, world: dict, faction_id: int) -> dict:
        """当前势力的狂热参数（取所有交战对中最长的狂热期）。"""
        cfg = self._npc_war_cfg()["frenzy"]
        tick = int(world.get("tick_no", 0) or 0)
        state = self._npc_ai_state(world)
        until = 0
        for pair in (state["pairs"].get(str(int(faction_id))) or {}).values():
            if not isinstance(pair, dict) or pair.get("phase") != "war":
                continue
            until = max(until, int(pair.get("frenzy_until_tick", 0) or 0))
        active = until > tick
        return {
            "active": active,
            "until_tick": until,
            "remaining_ticks": max(0, until - tick),
            "sorties_bonus": int(cfg["sorties_bonus"]) if active else 0,
            "home_reserve_delta": float(cfg["home_reserve_delta"])
            if active else 0.0,
            "home_reserve_min": float(cfg["home_reserve_min"]),
            "send_mult": float(cfg["send_mult"]) if active else 1.0,
            "utility_threshold": float(cfg["utility_threshold"])
            if active else None,
            "target_bonus": float(cfg["target_bonus"]) if active else 1.0,
            "aggression_bonus": float(cfg["aggression_bonus"])
            if active else 0.0,
            "morale_min": int(cfg["morale_min"]) if active else None,
            "mobilization_cost_mult": float(cfg["mobilization_cost_mult"])
            if active else 1.0,
            "grain_mult": float(cfg["grain_mult"]) if active else 1.0,
        }

    def _npc_threat_mult_for(self, world: dict, observer_id: int,
                             target_id: int) -> float:
        """威胁侵略倍率（受 ``threat.aggression_cap`` 约束）。"""
        cfg = self._npc_threat_cfg()
        h = self._npc_h(world, observer_id, target_id)
        return R.threat_mult(h, float(cfg["aggression_gain"]),
                             float(cfg["aggression_cap"]))

    def _pair_ceasefire(self, world: dict, observer_id: int,
                        target_id: int) -> bool:
        """是否处于对某目标的停火窗口（暂停进攻性出兵）。"""
        tick = int(world.get("tick_no", 0) or 0)
        state = self._npc_ai_state(world)
        pair = (state["pairs"].get(str(int(observer_id))) or {}).get(
            str(int(target_id))) or {}
        return tick < int(pair.get("ceasefire_until_tick", 0) or 0)

    # ------------------------------------------------------------------
    # 前端：天下视图
    # ------------------------------------------------------------------
    def _npc_world_view(self, world: dict, f_meta: dict,
                        winds: list | None = None) -> dict:
        """「天下」页数据：威胁榜 / 扩张倾向 / 对外关系 / 作战情况。"""
        cfg = self._npc_threat_cfg()
        war_cfg = self._npc_war_cfg()
        peace_cfg = self._npc_peace_cfg()
        wind_cfg = self._npc_wind_cfg()
        tick = int(world.get("tick_no", 0) or 0)
        data = world.get("data") or {}
        state = data.get("npc_ai")
        if not isinstance(state, dict):
            state = {}
        threat = data.get("threat")
        if not isinstance(threat, dict):
            threat = {}
        f_meta = f_meta or {}
        accum = threat.get("accum") or {}
        public = threat.get("public") or {}
        share_now = threat.get("share") or {}
        rows = []
        for fid, meta in f_meta.items():
            fid = int(fid)
            if fid <= 0 or not isinstance(meta, dict):
                continue
            # 已灭亡势力不再出现在天下榜 / 作战列表
            if int(meta.get("alive", 1) or 0) <= 0:
                continue
            aid = int(meta.get("alliance_id") or 0)
            host_meta = f_meta.get(aid) or {}
            is_member = bool(aid and aid != fid and int(
                host_meta.get("alive", 0) or 0) > 0)
            pair_row = {} if is_member else (
                state.get("pairs", {}).get(str(fid)) or {})
            if not isinstance(pair_row, dict):
                pair_row = {}
            wars = []
            for tgt, pair in pair_row.items():
                if not isinstance(pair, dict) or pair.get("phase") != "war":
                    continue
                try:
                    tgt = int(tgt)
                except (TypeError, ValueError):
                    continue
                tgt_meta = f_meta.get(tgt) or {}
                if tgt not in f_meta or int(
                        tgt_meta.get("alive", 1) or 0) <= 0:
                    continue
                wars.append({
                    "target": tgt,
                    "target_name": tgt_meta.get("name", "?"),
                    "goal": pair.get("goal") or "",
                    "goal_name": GOAL_CN.get(pair.get("goal") or "", "战争"),
                    "war_score": round(_num(pair.get("war_score"), 50), 1),
                    "exhaustion": round(_num(pair.get("exhaustion"), 0), 1),
                    "frenzy_remaining": max(0, _safe_int(
                        pair.get("frenzy_until_tick", 0)) - tick),
                    "ceasefire_remaining": max(0, _safe_int(
                        pair.get("ceasefire_until_tick", 0)) - tick),
                    "weary": _num(pair.get("exhaustion"), 0) >= float(
                        peace_cfg["peace_gate"]),
                })
            stats = state.get("stats", {}).get(str(fid)) or {}
            if not isinstance(stats, dict):
                stats = {}
            history = [h for h in (stats.get("share_history") or [])
                       if isinstance(h, dict)
                       and _safe_int(h.get("tick", 0)) <= tick]
            share = _num(share_now.get(str(fid)), 0) \
                if str(fid) in share_now else (
                    _num(history[-1].get("share"), 0) if history else 0.0)
            recent_growth = 0.0
            if len(history) >= 2:
                recent_growth = _num(history[-1].get("share"), 0) - \
                    _num(history[-2].get("share"), 0)
            public_h = _num(public.get(str(fid)), 0)
            threat_to = {}
            for t, v in (accum.get(str(fid)) or {}).items():
                try:
                    threat_to[int(t)] = round(_num(v, 0), 1)
                except (TypeError, ValueError):
                    continue
            own_h = max(threat_to.values() or [0])
            relations = []
            live_threat = []
            for tgt, h in threat_to.items():
                if tgt == fid or tgt not in f_meta:
                    continue
                tgt_meta = f_meta.get(tgt) or {}
                if int(tgt_meta.get("alive", 1) or 0) <= 0:
                    continue
                live_threat.append((tgt, h))
            for tgt, h in sorted(live_threat, key=lambda kv: -kv[1])[:6]:
                tgt_meta = f_meta.get(tgt) or {}
                relations.append({
                    "target": tgt,
                    "target_name": tgt_meta.get("name", "?"),
                    "relation": R.relation_of(
                        meta.get("diplomacy") or {}, tgt, _now()),
                    "h": h,
                })
            personality = meta.get("personality") or {}
            key = self._personality_key(meta)
            rows.append({
                "id": fid,
                "name": meta.get("name", "?"),
                "color": meta.get("color", "#888888"),
                "kind": meta.get("kind", "npc"),
                "node_count": int(meta.get("node_count", 0) or 0),
                "share": round(share * 100, 1),
                "growth": round(recent_growth * 100, 2),
                "public_threat": round(public_h, 1),
                "tier": TIER_CN.get(
                    R.threat_tier(public_h, cfg["tiers"]), "无感"),
                "personality": key,
                "personality_name": personality.get("name") or key,
                "aggression": int(personality.get("aggression", 50) or 50),
                "threat_mult": round(R.threat_mult(
                    own_h, float(cfg["aggression_gain"]),
                    float(cfg["aggression_cap"])), 2),
                "hegemon": bool(
                    share >= float(cfg["hegemon_share_threshold"])),
                "wars": wars,
                "war_count": len(wars),
                "relations": relations,
                "armies": int(meta.get("armies", 0) or 0),
                "troops": int(meta.get("troops", 0) or 0),
                "is_npc": str(meta.get("kind") or "") == "npc",
            })
        rows.sort(key=lambda item: -item["public_threat"])
        visible_at = _now()
        wind_rows = []
        raw_winds = winds if winds is not None else (data.get("winds") or [])
        if not isinstance(raw_winds, list):
            raw_winds = []
        for wind in raw_winds:
            if not isinstance(wind, dict):
                if str(wind or "").strip():
                    wind_rows.append({"text": str(wind), "kind": "plot",
                                      "kind_name": "流言", "tick": 0})
                continue
            if _safe_int(wind.get("visible_at", 0)) > visible_at:
                continue
            wind_rows.append({
                "text": str(wind.get("text") or ""),
                "kind": str(wind.get("kind") or "plot"),
                "kind_name": (wind_cfg["kinds"] or {}).get(
                    str(wind.get("kind") or "plot"), "流言"),
                "tick": _safe_int(wind.get("tick", 0)),
            })
        wind_rows = wind_rows[-max(1, int(wind_cfg["panel_limit"])):]
        return {
            "tick": tick,
            "tiers": cfg["tiers"],
            "declare_gate": float(war_cfg["declare_gate"]),
            "peace_gate": float(peace_cfg["peace_gate"]),
            "factions": rows,
            "winds": wind_rows,
        }
