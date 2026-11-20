# -*- coding: utf-8 -*-
"""「仙域争霸」P1 计略体系 Mixin（设计方案 §6.3）。

六计略：离间 / 散布谣言 / 纵火 / 劝降（围城） / 策应 / 营救。
军师专属，走 strategy_tasks 通道（task_type='plot_*'），
到期由 _step_settle_tasks 路由至 _settle_plot_task 结算。

冷却/风声/策应标记等临时态存 strategy_worlds.data_json。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from ..core import strategy_rules as R
from .strategy_errors import StrategyError

logger = logging.getLogger("astrbot")


def _now() -> int:
    return int(time.time())


PLOT_STAMINA_COST = 30
#: 各计略的目标类型：char=角色 id / node=据点 id / captive=被俘角色 id
PLOT_TARGET_KIND = {
    "alienate": "char", "rumor": "node", "arson": "node",
    "persuade_plot": "node", "sabotage": "node", "rescue": "captive",
}


class StrategyPlotMixin:
    """P1 计略（§6.3）。"""

    # ------------------------------------------------------------------
    # 派遣
    # ------------------------------------------------------------------
    async def dispatch_plot(self, user_id: str, char_id: int, plot_type: str,
                            target_id: int) -> dict:
        world, p = await self._require_player(user_id)
        pcfg = self.main_cfg().get("plot") or {}
        ccfg = self.main_cfg().get("captive") or {}
        plot_type = str(plot_type)
        if plot_type not in (pcfg.get("types") or list(R.PLOT_TYPES)):
            raise StrategyError(f"计略类型无效：{plot_type}（{R.PLOT_NAMES}）")
        c = await self._own_char(p, char_id)
        if c.get("role") != R.ROLE_STRATEGIST:
            raise StrategyError(f"{c['name']} 不是军师，无法施展计略")
        if c.get("status") != "idle":
            raise StrategyError(f"{c['name']} 状态为「{c['status']}」，无法派遣")
        if R.injury_state(c.get("injury", 0)) in ("dying", "dead"):
            raise StrategyError(f"{c['name']} 伤势过重")
        if int(c.get("stamina", 0)) < PLOT_STAMINA_COST:
            raise StrategyError(f"{c['name']} 体力不足（需 {PLOT_STAMINA_COST}）")
        f = await self.get_faction(p["faction_id"])
        tech = (f or {}).get("tech") or {}
        target_id = int(target_id)
        target_fid = 0
        # --- 目标校验 + 费用 ---
        if plot_type == "alienate":
            tc = await self.get_char(target_id)
            if not tc or int(tc.get("faction_id", 0)) == int(p["faction_id"]):
                raise StrategyError("离间目标须为敌方角色")
            target_fid = int(tc.get("faction_id", 0))
            cost_coin = int(pcfg.get("alienate_coin_cost", 500))
            if p["coin"] < cost_coin:
                raise StrategyError(f"铜钱不足：离间需 {cost_coin}")
            p["coin"] -= cost_coin
        elif plot_type == "rescue":
            tc = await self.get_char(target_id)
            if not tc or tc.get("status") != "captive":
                raise StrategyError("营救目标不是俘虏")
            if (tc.get("location") or {}).get("home_owner") != user_id and \
                    int((tc.get("location") or {}).get("home_faction", 0)) != \
                    int(p["faction_id"]):
                raise StrategyError("只能营救本势力的人")
            target_fid = int(tc.get("captor_faction", 0))
            cost_coin = int(ccfg.get("rescue_coin", 800))
            if p["coin"] < cost_coin:
                raise StrategyError(f"铜钱不足：营救需 {cost_coin}")
            p["coin"] -= cost_coin
        else:
            node = await self.get_node(world["id"], target_id)
            if not node:
                raise StrategyError("目标据点不存在")
            if int(node.get("owner_faction", 0)) == int(p["faction_id"]):
                raise StrategyError("不能对本方据点施展计略")
            target_fid = int(node.get("owner_faction", 0))
            if plot_type == "persuade_plot":
                ss = node.get("siege_state") or {}
                if int(ss.get("attacker_faction", 0)) != int(p["faction_id"]):
                    raise StrategyError("劝降仅对我方正在围城的据点有效")
            if plot_type == "arson":
                cost_iron = int(pcfg.get("arson_iron_cost", 200))
                if p["iron"] < cost_iron:
                    raise StrategyError(f"精铁不足：纵火需 {cost_iron}")
                p["iron"] -= cost_iron
        # 盟友不可算计
        if target_fid and f:
            rel = R.relation_of(f.get("diplomacy") or {}, target_fid, _now())
            if rel == R.REL_ALLY:
                raise StrategyError("不能对盟友施展计略")
        # 目标冷却：6 tick（谋略 L4 -2，最少 2）
        data = world.get("data") or {}
        cd_map = data.setdefault("plot_cd", {})
        cd_key = f"{p['faction_id']}:{plot_type}:{target_id}"
        tick_no = int(world.get("tick_no", 0))
        if int(cd_map.get(cd_key, 0)) > tick_no:
            raise StrategyError(f"该目标尚在计略冷却中"
                                f"（还剩 {int(cd_map[cd_key]) - tick_no} tick）")
        cd_ticks = max(2, int(pcfg.get("cooldown_ticks", 6)) -
                       int(R.tech_effects(tech).get("plot_cd_reduce", 0)))
        cd_map[cd_key] = tick_no + cd_ticks
        # --- 落任务行 ---
        if plot_type == "rescue":
            lo, hi = ccfg.get("rescue_ticks") or [4, 8]
            ticks = self._rng("rescue_ticks", c["id"], target_id,
                              tick_no).randint(int(lo), int(hi))
        else:
            ticks = 2
        end_at = _now() + ticks * self.tick_seconds()
        c["status"] = "task"
        c["stamina"] = int(c.get("stamina", 100)) - PLOT_STAMINA_COST
        await self.save_char(c)
        tid = await self._execute("""
            INSERT INTO strategy_tasks (world_id, faction_id, character_id, node_id,
                task_type, start_at, end_at, payout_json, status, target_faction)
            VALUES (?,?,?,?,?,?,?,'{}','active',?)
        """, (
            world["id"], int(p["faction_id"]), int(c["id"]),
            int(target_id) if PLOT_TARGET_KIND[plot_type] != "char" else 0,
            f"plot_{plot_type}", _now(), end_at, target_fid))
        # payout_json 单独写入（含计略参数）
        await self._execute(
            "UPDATE strategy_tasks SET payout_json=? WHERE id=?",
            (json.dumps({"plot": plot_type, "target": target_id,
                         "ticks": ticks}, ensure_ascii=False), tid))
        data["plot_cd"] = cd_map
        world["data"] = data
        await self.save_world(world)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "task_id": tid,
                "msg": f"🎴 {c['name']} 开始施展「{R.PLOT_NAMES.get(plot_type)}」"
                       f"（{ticks} tick 后见分晓）"}

    # ------------------------------------------------------------------
    # 结算（挂入 _step_settle_tasks）
    # ------------------------------------------------------------------
    async def _settle_plot_task(self, world: dict, task: dict, c: dict,
                                p: Optional[dict]) -> dict:
        """计略任务到期结算。返回 {plot, success, exposed, texts}。"""
        pcfg = self.main_cfg().get("plot") or {}
        payload = task.get("payout") or {}
        plot = str(payload.get("plot") or task["task_type"][len("plot_"):])
        target = int(payload.get("target", 0))
        fid = int(task.get("faction_id", 0))
        f = await self.get_faction(fid)
        tech = (f or {}).get("tech") or {}
        target_fid = int(task.get("target_faction", 0))
        out: dict[str, Any] = {"plot": plot, "success": False, "exposed": False,
                               "texts": []}
        rng = self._rng("plot", task["id"], plot, target)
        stats = c.get("stats") or {}
        wisdom_self = int(stats.get("wisdom", 50))
        wisdom_target = await self._faction_best_wisdom(world["id"], target_fid)
        bonus = float(R.tech_effects(tech).get("plot_bonus", 0.0)) + \
            await self._faction_plot_bonus(f)
        tf = await self.get_faction(target_fid) if target_fid else None
        # 反间（谋略 L5：30% 反弹）
        countered = False
        if tf:
            cs = float(R.tech_effects(tf.get("tech") or {}).get("counter_spy", 0.0))
            if cs > 0 and rng.random() < cs:
                countered = True
        if plot == "rescue":
            tc = await self.get_char(target)
            if not tc or tc.get("status") != "captive":
                out["texts"].append("营救目标已不在关押中，行动取消")
                return out
            lead = int((tc.get("stats") or {}).get("leadership", 50))
            rate = R.rescue_success_rate(wisdom_self, wisdom_target, lead, bonus)
            success = (not countered) and rng.random() * 100 < rate
            out["success"] = success
            if success:
                await self._return_captive_home(tc)
                out["texts"].append(f"🕊 营救成功！{tc['name']} 悄然脱身"
                                    f"（成功率 {rate:.0f}%）")
            else:
                expose_chance = float((self.main_cfg().get("captive") or {}).get(
                    "rescue_expose_chance", 0.4))
                if countered or R.plot_expose(
                        rng, expose_chance,
                        float(R.tech_effects(tech).get("expose_reduction", 0.0))):
                    out["exposed"] = True
                    await self._expose_executor(world, c, f, target_fid)
                    out["texts"].append(f"营救失败且行迹败露，{c['name']} 被擒！")
                else:
                    out["texts"].append("营救失败，所幸未暴露行迹")
            return out
        # --- 通用计略 ---
        rate = R.plot_success_rate(wisdom_self, wisdom_target, bonus)
        success = (not countered) and rng.random() * 100 < rate
        out["success"] = success
        if countered:
            out["texts"].append("对方谋略科技「反间」生效，计略被识破反弹！")
        if not success:
            if countered or R.plot_expose(
                    rng, float(pcfg.get("expose_chance", 0.4)),
                    float(R.tech_effects(tech).get("expose_reduction", 0.0))):
                out["exposed"] = True
                await self._expose_executor(world, c, f, target_fid)
                out["texts"].append(f"「{R.PLOT_NAMES.get(plot)}」失败，"
                                    f"{c['name']} 身份暴露被擒（声望 -5）")
            else:
                out["texts"].append(f"「{R.PLOT_NAMES.get(plot)}」失败，无功而返")
            return out
        # --- 成功效果 ---
        if plot == "alienate":
            tc = await self.get_char(target)
            if tc and tc.get("status") not in ("dead",):
                lo, hi = pcfg.get("alienate_loyalty") or [-40, -20]
                drop = rng.randint(int(lo), int(hi))
                tc["loyalty"] = max(0, int(tc.get("loyalty", 70)) + drop)
                if tc["loyalty"] < 30:
                    tc["status"] = "wander"
                    tc["faction_id"] = 0
                    tc["owner_user_id"] = ""
                    # 离心出走：清除囚禁标记（角色此前可能处于被俘状态），
                    # 防 captor_faction/imprisoned_node_id 悬挂导致重复解放/迁移
                    tc["captor_faction"] = 0
                    tc["imprisoned_node_id"] = 0
                    out["texts"].append(
                        f"离间大成！{tc['name']} 忠诚崩至 {tc['loyalty']}，挂印出走")
                else:
                    out["texts"].append(
                        f"离间生效：{tc['name']} 忠诚 -{-drop}（现 {tc['loyalty']}）")
                await self.save_char(tc)
        elif plot == "rumor":
            node = await self.get_node(world["id"], target)
            if node:
                node["morale"] = max(0, int(node.get("morale", 80)) +
                                     int(pcfg.get("rumor_morale", -15)))
                await self.save_node(node)
                ticks = int(pcfg.get("rumor_ticks", 3))
                tick_no = int(world.get("tick_no", 0))
                await self._execute("""
                    INSERT INTO strategy_events (world_id, event_id, scope, target_json,
                        start_at, end_at, start_tick, end_tick, data_json)
                    VALUES (?,'rumor','region',?,?,?,?,?,?)
                """, (world["id"],
                      json.dumps({"node_ids": [node["id"]], "node_name": node["name"]},
                                 ensure_ascii=False),
                      _now(), _now() + ticks * self.tick_seconds(), tick_no,
                      tick_no + ticks,
                      json.dumps({"name": "谣言四起", "effects": [
                          {"type": "node_output", "resource": "coin",
                           "mult": 1 + float(pcfg.get("rumor_output_pct", -0.2))},
                          {"type": "node_output", "resource": "grain",
                           "mult": 1 + float(pcfg.get("rumor_output_pct", -0.2))}]},
                          ensure_ascii=False)))
                out["texts"].append(
                    f"谣言在「{node['name']}」蔓延：民心 -15，产出 -20%（{ticks} tick）")
        elif plot == "arson":
            node = await self.get_node(world["id"], target)
            if node:
                lo, hi = pcfg.get("arson_burn_pct") or [0.1, 0.3]
                pct = rng.uniform(float(lo), float(hi))
                burned = await self._burn_owner_grain(world, node, pct)
                out["texts"].append(
                    f"🔥「{node['name']}」粮仓走水，焚毁存粮 {burned}（{pct * 100:.0f}%）")
        elif plot == "persuade_plot":
            node = await self.get_node(world["id"], target)
            ss = (node or {}).get("siege_state") or {}
            if node and int(ss.get("attacker_faction", 0)) == fid:
                await self._capture_node(world, node, fid)
                out["texts"].append(f"🏳️ 围城中的「{node['name']}」守将开城投降！")
                await self.liberate_captives_at_node(world, node, fid)
            else:
                out["texts"].append("围城已解，劝降扑空")
        elif plot == "sabotage":
            node = await self.get_node(world["id"], target)
            if node:
                node["defense"] = int(int(node.get("defense", 0)) * 0.8)
                gar = node.get("npc_garrison") or {}
                gar["morale"] = max(0, int(gar.get("morale", 80)) +
                                    int(pcfg.get("sabotage_morale", -10)))
                node["npc_garrison"] = gar
                await self.save_node(node)
                data = world.get("data") or {}
                sab = data.setdefault("sabotage", {})
                sab[str(node["id"])] = {"by": fid,
                                        "until_tick": int(world.get("tick_no", 0)) + 4}
                world["data"] = data
                await self.save_world(world)
                out["texts"].append(
                    f"策应就绪：「{node['name']}」城防 -20%、守军士气挫败，城门暗开 4 tick")
        # 成功 → 24h 后风声走漏（记录，供面板叙事）
        if out["success"]:
            data = world.get("data") or {}
            winds = data.setdefault("winds", [])
            winds.append({"visible_at": _now() + int(pcfg.get("wind_delay_hours", 24))
                          * 3600,
                          "text": f"有风声传出：{R.PLOT_NAMES.get(plot)}之计"
                                  f"似出自 {f.get('name', '?')} 手笔"})
            data["winds"] = winds[-30:]
            world["data"] = data
            await self.save_world(world)
        await self._commit()
        return out

    async def _faction_best_wisdom(self, world_id: int, faction_id: int) -> int:
        if not faction_id or faction_id < 0:
            return 50
        best = 50
        for c in await self.get_chars(faction_id=faction_id, world_id=world_id):
            best = max(best, int((c.get("stats") or {}).get("wisdom", 0)))
        return best

    async def _expose_executor(self, world: dict, c: dict, f: Optional[dict],
                               target_fid: int):
        """计略暴露：执行者被目标势力擒获 + 本方声望 -5。"""
        if f:
            await self.add_reputation(
                f, int((self.main_cfg().get("plot") or {}).get("expose_rep_cost", -5)))
        if target_fid and target_fid > 0 and int(c.get("faction_id", 0)) != target_fid:
            node_id = int((c.get("location") or {}).get("node_id", 0) or 0)
            c["status"] = "idle"  # 先脱离任务态再入囚
            await self._capture_char(c, target_fid, node_id)

    async def _burn_owner_grain(self, world: dict, node: dict, pct: float) -> int:
        owner = int(node.get("owner_faction", 0))
        if owner <= 0:
            return 0
        f = await self.get_faction(owner)
        if not f:
            return 0
        if f.get("kind") == "npc":
            treasury = f.get("treasury") or {}
            loss = int(int(treasury.get("grain", 0)) * pct)
            treasury["grain"] = int(treasury.get("grain", 0)) - loss
            f["treasury"] = treasury
            await self.save_faction(f)
            return loss
        row = await self._fetchone(
            "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
            (world["id"], owner))
        if not row:
            return 0
        p = await self.get_player(row["user_id"], world["id"])
        if not p:
            return 0
        loss = int(int(p.get("grain", 0)) * pct)
        p["grain"] = int(p.get("grain", 0)) - loss
        await self.save_player(p)
        return loss
