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
        state.setdefault("accum", {})
        state.setdefault("pairs", {})
        state.setdefault("stats", {})
        state.setdefault("declarations", {"day": 0, "count": 0})
        data["npc_ai"] = state
        return state

    def _pair(self, state: dict, observer_id: int, target_id: int) -> dict:
        pairs = state.setdefault("pairs", {})
        row = pairs.setdefault(str(int(observer_id)), {})
        pair = row.setdefault(str(int(target_id)), self._default_pair())
        for key, value in self._default_pair().items():
            pair.setdefault(key, value)
        return pair

    def _pair_of(self, world: dict, observer_id: int, target_id: int) -> dict:
        return self._pair(self._npc_ai_state(world), observer_id, target_id)

    def _npc_h(self, world: dict, observer_id: int, target_id: int) -> float:
        accum = self._npc_ai_state(world).get("accum") or {}
        try:
            return float((accum.get(str(int(observer_id))) or {})
                         .get(str(int(target_id)), 0.0) or 0.0)
        except (TypeError, ValueError):
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
                       and int(h.get("tick", 0) or 0) >= tick - 7 * tpd]
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
        stat_window = 7 * tpd
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
        for obs, row in snap["inst"].items():
            out = accum.setdefault(str(obs), {})
            for tgt, p_eff in row.items():
                prev = float(out.get(str(tgt), 0.0) or 0.0)
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
        war_cfg = self._npc_war_cfg()
        for obs in factions:
            for tgt in factions:
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
                    entry = TIER_ENTRY.get(pair["phase"], 0)
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
        war_cfg = self._npc_war_cfg()
        now = _now()
        for obs in factions:
            for tgt in factions:
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
                        morale = float(snap["avg_morale"].get(obs, 100) or 100)
                        if (int(treasury["coin"]) < int(reserve["coin"])
                                or int(treasury["grain"]) < int(
                                    reserve["grain"])
                                or troops < int(frenzy["min_troops"])
                                or morale < float(frenzy["morale_break"])):
                            pair["frenzy_until_tick"] = tick
                elif phase == "truce":
                    expire_tick = int(pair.get("truce_until_tick", 0) or 0)
                    if (expire_tick and tick >= expire_tick) or \
                            (not expire_tick and rel != R.REL_TRUCE):
                        pair["phase"] = "cooldown"
                        pair["no_declare_until_tick"] = max(
                            int(pair.get("no_declare_until_tick", 0) or 0),
                            tick + int(war_cfg["post_war_cooldown_ticks"]))
                elif phase == "cooldown":
                    if tick >= int(pair.get("no_declare_until_tick", 0) or 0):
                        pair["phase"] = "calm"
                        pair["hostile_since_tick"] = 0
                elif phase in TIER_PHASES and rel in (R.REL_WAR,
                                                      "war_pending"):
                    pair.update({
                        "phase": "war",
                        "war_start_tick": tick,
                        "last_capture_tick": tick,
                        "war_score": 50,
                        "exhaustion": 0.0,
                        "goal": pair.get("goal") or "border",
                        "war_start_troops": int(snap["troops"].get(obs, 0) or 0),
                        "war_start_nodes": int(
                            snap["node_count"].get(obs, 0) or 0),
                    })
                ceasefire = int(pair.get("ceasefire_until_tick", 0) or 0)
                if ceasefire and tick >= ceasefire:
                    pair["ceasefire_until_tick"] = 0

    # ------------------------------------------------------------------
    # 宣战
    # ------------------------------------------------------------------
    async def _step_npc_diplomacy(self, world: dict) -> None:
        """威胁收敛 + 档位推进 + NPC 自主宣战（§7.11）。"""
        cfg = self._npc_threat_cfg()
        if not cfg.get("enabled", True):
            return
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
        if not isinstance(declarations, dict) or \
                int(declarations.get("day", -1) or -1) != day:
            declarations = {"day": day, "count": 0}
        declared_this_tick = 0
        factions = snap["factions"]
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
        owned = [n for n in snap["nodes"].values()
                 if int(n.get("owner_faction", 0) or 0) == int(target["id"])]
        if owned and all(int(n.get("protect_until", 0) or 0) > now
                         for n in owned):
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
        frenzy_ticks = int(float(war_cfg["frenzy"]["hours"]) * tph)
        state = self._npc_ai_state(world)
        for obs_id, tgt_id, me in (
                (observer["id"], target["id"], observer),
                (target["id"], observer["id"], target)):
            row = self._pair(state, obs_id, tgt_id)
            row.update({
                "phase": "war",
                "war_start_tick": tick,
                "last_declare_tick": tick,
                "frenzy_until_tick": tick + frenzy_ticks,
                "goal": goal,
                "war_score": 50,
                "exhaustion": 0.0,
                "last_capture_tick": tick,
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
            rows = await self._fetchall(
                "SELECT user_id FROM strategy_players "
                "WHERE world_id=? AND faction_id=?",
                (world["id"], int(target["id"])))
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
        """非接壤霸主远征目标（§7.8）：返回可直达的目标节点。"""
        cfg = self._npc_war_cfg()["expedition"]
        tick = int(world.get("tick_no", 0) or 0)
        state = self._npc_ai_state(world)
        pair = self._pair(state, observer["id"], target["id"])
        if tick - int(pair.get("last_expedition_tick", 0) or 0) < \
                int(cfg["cooldown_ticks"]):
            return None
        own_nodes = [n for n in snap["nodes"].values()
                     if int(n.get("owner_faction", 0) or 0) ==
                     int(observer["id"])
                     and int((n.get("npc_garrison") or {})
                             .get("troops", 0) or 0) >= int(
                                 cfg["min_troops"])]
        if not own_nodes:
            return None
        best: Optional[dict] = None
        best_len = int(cfg["max_edges"]) + 1
        for node in own_nodes:
            dest_id = await self._nearest_faction_node(
                world, int(node["id"]), int(target["id"]))
            if not dest_id:
                continue
            path = await self.find_path(world["id"], int(node["id"]), dest_id)
            if not path or len(path) - 1 > int(cfg["max_edges"]):
                continue
            if len(path) - 1 < best_len:
                best_len = len(path) - 1
                best = dict(snap["nodes"].get(dest_id) or {})
                best.setdefault("id", dest_id)
                best["_from_node"] = int(node["id"])
        return best

    # ------------------------------------------------------------------
    # 停战
    # ------------------------------------------------------------------
    async def _step_npc_peace(self, world: dict) -> None:
        """战争疲劳、NPC→NPC 停战、NPC→玩家求和（§8.8）。"""
        cfg = self._npc_peace_cfg()
        dcfg = self.main_cfg().get("diplomacy") or {}
        tick = int(world.get("tick_no", 0) or 0)
        tpd = self._ticks_per_day()
        tph = self._ticks_per_hour()
        snap = await self._threat_snapshot(world)
        state = self._npc_ai_state(world)
        factions = snap["factions"]
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
                    float(pair.get("war_score", 50) or 50))
                p_tick = R.per_day_to_per_tick(p_day, tpd)
                rng = self._rng("npc_peace", tick, observer_id, target_id)
                if rng.random() >= p_tick:
                    continue
                if str(target.get("kind") or "") == "npc":
                    reverse = self._pair(state, target_id, observer_id)
                    favor = R.favor_of(target.get("diplomacy"), observer_id)
                    accept = R.threat_peace_accept(
                        float(reverse.get("exhaustion", 0) or 0), favor,
                        float(pair.get("war_score", 50) or 50),
                        float(reverse.get("war_score", 50) or 50),
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
                    if tick - int(pair.get("last_offer_tick", 0) or 0) < \
                            int(cfg["offer_cooldown_ticks"]):
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
        morale = float(snap["avg_morale"].get(obs_id, 100) or 100)
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
            "SELECT user_id FROM strategy_players "
            "WHERE world_id=? AND faction_id=?",
            (world["id"], int(target["id"])))
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
        """据点易主：更新战争战果 / 龟缩统计，并写一条风声。"""
        prev_owner, new_owner = int(prev_owner or 0), int(new_owner or 0)
        if prev_owner <= 0 or new_owner <= 0 or prev_owner == new_owner:
            return
        tick = int(world.get("tick_no", 0) or 0)
        state = self._npc_ai_state(world)
        pair = self._pair(state, new_owner, prev_owner)
        if pair.get("phase") == "war":
            pair["captures_by_me"] = int(
                pair.get("captures_by_me", 0) or 0) + 1
            pair["last_capture_tick"] = tick
        reverse = self._pair(state, prev_owner, new_owner)
        if reverse.get("phase") == "war":
            reverse["captures_by_target"] = int(
                reverse.get("captures_by_target", 0) or 0) + 1
        window = 7 * self._ticks_per_day()
        for fid, key in ((new_owner, "captures_7d"),
                         (prev_owner, "attacks_7d")):
            stat = state["stats"].setdefault(str(fid), {})
            items = [int(x) for x in (stat.get(key) or [])
                     if int(x or 0) >= tick - window]
            items.append(tick)
            stat[key] = items[-32:]
        self._add_wind(
            world,
            f"🏴「{node_name or '边镇'}」易主，归入新主旗下。",
            "battle")

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
        state = data.get("npc_ai") or {}
        threat = data.get("threat") or {}
        accum = threat.get("accum") or {}
        public = threat.get("public") or {}
        rows = []
        for fid, meta in (f_meta or {}).items():
            fid = int(fid)
            if fid <= 0 or not isinstance(meta, dict):
                continue
            pair_row = state.get("pairs", {}).get(str(fid)) or {}
            wars = []
            for tgt, pair in pair_row.items():
                if not isinstance(pair, dict) or pair.get("phase") != "war":
                    continue
                tgt = int(tgt)
                wars.append({
                    "target": tgt,
                    "target_name": (f_meta.get(tgt) or {}).get("name", "?"),
                    "goal": pair.get("goal") or "",
                    "goal_name": GOAL_CN.get(pair.get("goal") or "", "战争"),
                    "war_score": round(float(
                        pair.get("war_score", 50) or 50), 1),
                    "exhaustion": round(float(
                        pair.get("exhaustion", 0) or 0), 1),
                    "frenzy_remaining": max(0, int(
                        pair.get("frenzy_until_tick", 0) or 0) - tick),
                    "ceasefire_remaining": max(0, int(
                        pair.get("ceasefire_until_tick", 0) or 0) - tick),
                    "weary": float(pair.get("exhaustion", 0) or 0) >= float(
                        peace_cfg["peace_gate"]),
                })
            stats = state.get("stats", {}).get(str(fid)) or {}
            history = stats.get("share_history") or []
            share = float(history[-1].get("share", 0.0) or 0.0) \
                if history else 0.0
            recent_growth = 0.0
            if len(history) >= 2:
                recent_growth = float(history[-1].get("share", 0) or 0) - \
                    float(history[-2].get("share", 0) or 0)
            public_h = float(public.get(str(fid), 0.0) or 0.0)
            threat_to = {int(t): round(float(v or 0), 1)
                         for t, v in (accum.get(str(fid)) or {}).items()}
            relations = []
            for tgt, h in sorted(threat_to.items(),
                                 key=lambda kv: -kv[1])[:6]:
                if tgt == fid:
                    continue
                relations.append({
                    "target": tgt,
                    "target_name": (f_meta.get(tgt) or {}).get("name", "?"),
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
                    public_h, float(cfg["aggression_gain"]),
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
        for wind in (winds if winds is not None
                     else (data.get("winds") or [])):
            if not isinstance(wind, dict):
                if str(wind or "").strip():
                    wind_rows.append({"text": str(wind), "kind": "plot",
                                      "kind_name": "流言", "tick": 0})
                continue
            if int(wind.get("visible_at", 0) or 0) > visible_at:
                continue
            wind_rows.append({
                "text": str(wind.get("text") or ""),
                "kind": str(wind.get("kind") or "plot"),
                "kind_name": (wind_cfg["kinds"] or {}).get(
                    str(wind.get("kind") or "plot"), "流言"),
                "tick": int(wind.get("tick", 0) or 0),
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
