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
        if int(char_id or 0) <= 0:
            # 前端「细作营救」等快捷入口不指定军师：后端统一挑选智谋最高、
            # 且空闲/健康/体力足够的军师，避免固定取列表第一个（可能重伤）。
            best: Optional[dict] = None
            best_score = -1
            for mine in await self.get_chars(
                    owner_user_id=user_id, world_id=world["id"], status="idle"):
                if mine.get("role") != R.ROLE_STRATEGIST:
                    continue
                if R.injury_state(mine.get("injury", 0)) in ("dying", "dead"):
                    continue
                if int(mine.get("stamina", 0)) < PLOT_STAMINA_COST:
                    continue
                st = mine.get("stats") or {}
                score = int(st.get("wisdom", 0)) * 100 + int(st.get("charm", 0))
                if score > best_score:
                    best_score, best = score, mine
            if not best:
                raise StrategyError("没有可用的军师（需空闲、非濒死且体力充足）")
            char_id = int(best["id"])
        c = await self._own_char(p, char_id)
        if c.get("role") != R.ROLE_STRATEGIST:
            raise StrategyError(f"{c['name']} 不是军师，无法施展计略")
        if c.get("status") != "idle":
            raise StrategyError(
                f"{c['name']} 状态为「"
                f"{R.CHAR_STATUS_NAMES.get(c['status'], c['status'])}」，无法派遣")
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
            # v4.4.1：目标必须属于当前世界且仍在职——旧赛季残留角色/已出走
            # 角色 faction_id 为 0，派出去只会白扣铜钱并在结算时落空。
            if not tc or int(tc.get("world_id", 0) or 0) != int(world["id"]) \
                    or int(tc.get("faction_id", 0) or 0) <= 0 \
                    or int(tc.get("faction_id", 0)) == int(p["faction_id"]):
                raise StrategyError("离间目标须为敌方在职角色")
            if tc.get("status") in ("dead", "captive", "transfer"):
                # 俘虏/押运中角色不能走离间：否则可免费把人从敌方牢里
                # “劝”成流浪角色（跳过营救费用与关押位限制）。
                raise StrategyError("离间目标须为敌方在野/在职角色（俘虏请用营救）")
            target_fid = int(tc.get("faction_id", 0))
            cost_coin = int(pcfg.get("alienate_coin_cost", 500))
            if p["coin"] < cost_coin:
                raise StrategyError(f"铜钱不足：离间需 {cost_coin}")
            p["coin"] -= cost_coin
        elif plot_type == "rescue":
            tc = await self.get_char(target_id)
            # §7.9：关押中的俘虏 + 押运途中的俘虏都可劫走；
            # 跨赛季残留俘虏不属于当前世界，不能成为营救目标。
            if not tc or int(tc.get("world_id", 0) or 0) != int(world["id"]) \
                    or tc.get("status") not in ("captive", "transfer"):
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
        # 盟友/停战不可算计（与出征 _check_attack_target 的 _alliance_host 同口径）：
        # 只比 relation_of 会放过「已结盟但外交字典尚未写入 ALLY」的成员，
        # 同联盟玩家仍可对盟友城池造谣/纵火。
        if target_fid and f:
            my_host = await self._alliance_host(f) or f
            target_f = await self.get_faction(int(target_fid)) \
                if int(target_fid) > 0 else None
            target_host = await self._alliance_host(target_f) if target_f else None
            if target_host and my_host and \
                    int(my_host["id"]) == int(target_host["id"]):
                raise StrategyError("不能对同盟势力施展计略")
            rel = R.relation_of(
                (my_host or {}).get("diplomacy") or {},
                int((target_host or {}).get("id") or target_fid), _now())
            if rel in (R.REL_ALLY, R.REL_TRUCE):
                rel_name = "同盟" if rel == R.REL_ALLY else "停战"
                raise StrategyError(f"不能对{rel_name}势力施展计略")
        # 目标冷却：6 tick（谋略 L4 -2，最少 2）
        data = world.get("data") or {}
        cd_map = data.setdefault("plot_cd", {})
        cd_key = f"{p['faction_id']}:{plot_type}:{target_id}"
        tick_no = int(world.get("tick_no", 0))
        if int(cd_map.get(cd_key, 0)) > tick_no:
            raise StrategyError(f"该目标尚在计略冷却中"
                                f"（还剩 {int(cd_map[cd_key]) - tick_no} tick）")
        cd_ticks = max(2, int(pcfg.get("cooldown_ticks", 6)) -
                       int(R.tech_effects(
                           tech, tcfg=self.main_cfg().get("tech") or {}
                       ).get("plot_cd_reduce", 0)))
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
        tcfg = self.main_cfg().get("tech") or {}
        payload = task.get("payout") or {}
        plot = str(payload.get("plot") or task["task_type"][len("plot_"):])
        target = int(payload.get("target", 0))
        fid = int(task.get("faction_id", 0))
        f = await self.get_faction(fid)
        tech = (f or {}).get("tech") or {}
        tech_eff = R.tech_effects(tech, tcfg=tcfg)
        target_fid = int(task.get("target_faction", 0))
        out: dict[str, Any] = {"plot": plot, "success": False, "exposed": False,
                               "texts": []}
        # 换阵营保护：玩家带角色转投他势力后，旧势力的计略任务不应继续
        # 生效（否则执行者已在新阵营，效果仍打在旧势力的目标上）。
        if int(c.get("faction_id", 0) or 0) != fid:
            out["texts"].append("执行者已改换阵营，计略中止")
            return out
        rng = self._rng("plot", task["id"], plot, target)
        stats = c.get("stats") or {}
        wisdom_self = int(stats.get("wisdom", 50))
        wisdom_target = await self._faction_best_wisdom(world["id"], target_fid)
        bonus = float(tech_eff.get("plot_bonus", 0.0)) + \
            await self._faction_plot_bonus(f)
        # 神算：计略成功率 +15%（§4.9）
        bonus += float(R.personality_mods(
            c.get("personality")).get("scheme_rate_bonus", 0.0) or 0.0)
        # 谋略 L3：对带兵 ≥ 阈值的目标成功率 +3%（仅据点目标，按守军+驻军计）
        if str(PLOT_TARGET_KIND.get(plot) or "") == "node" and target > 0 and \
                float(tech_eff.get("plot_bonus_vs_big_army", 0.0) or 0.0) > 0:
            tnode = await self.get_node(world["id"], int(target))
            if tnode:
                tt = int(((tnode.get("npc_garrison") or {}).get("troops") or 0))
                for a in await self.get_armies(world["id"], node_id=int(target),
                                               states=("idle", "siege")):
                    tt += max(0, int(a.get("troops", 0) or 0))
                if tt >= int(tech_eff.get("big_army_threshold", 2000) or 2000):
                    bonus += float(tech_eff.get("plot_bonus_vs_big_army", 0.0))
        tf = await self.get_faction(target_fid) if target_fid else None
        # 谋略 L7 诡道：目标势力「凤雏」类效果——敌军计略成功率 -10%
        enemy_rate_mult = 1.0
        if tf:
            enemy_rate_mult = float(R.tech_effects(
                tf.get("tech") or {}, tcfg=tcfg).get("enemy_plot_rate_mult", 1.0) or 1.0)
        # 反间（谋略 L5：30% 反弹）；己方谋略 L5「反间防御」可抵消部分被反间概率
        countered = False
        if tf:
            cs = float(R.tech_effects(
                tf.get("tech") or {}, tcfg=tcfg).get("counter_spy", 0.0))
            cs = max(0.0, cs - float(tech_eff.get("counter_spy_defense", 0.0)))
            if cs > 0 and rng.random() < cs:
                countered = True
        if plot == "rescue":
            tc = await self.get_char(target)
            if not tc or int(tc.get("world_id", 0) or 0) != int(world["id"]) \
                    or tc.get("status") not in ("captive", "transfer"):
                out["texts"].append("营救目标已不在关押/押运中，行动取消")
                return out
            in_transit = tc.get("status") == "transfer"
            lead = int((tc.get("stats") or {}).get("leadership", 50))
            rate = R.rescue_success_rate(wisdom_self, wisdom_target, lead, bonus) * enemy_rate_mult
            success = (not countered) and rng.random() * 100 < rate
            out["success"] = success
            if success:
                if in_transit:
                    # 押运途中截获：删除押运单，俘虏直接脱身
                    await self._execute(
                        "DELETE FROM strategy_captive_transfers "
                        "WHERE char_id=? AND status='marching'", (int(tc["id"]),))
                await self._return_captive_home(tc)
                out["texts"].append(
                    f"🕊 营救成功！{tc['name']} "
                    f"{'在押运途中被劫走' if in_transit else '悄然脱身'}"
                    f"（成功率 {rate:.0f}%）")
            else:
                expose_chance = float((self.main_cfg().get("captive") or {}).get(
                    "rescue_expose_chance", 0.4))
                if countered or R.plot_expose(
                        rng, expose_chance,
                        float(tech_eff.get("expose_reduction", 0.0))):
                    out["exposed"] = True
                    got = await self._expose_executor(world, c, f, target_fid)
                    out["texts"].append(
                        f"营救失败且行迹败露，{c['name']} 被擒！" if got else
                        f"营救失败且行迹败露，{c['name']} 因敌营已满被就地驱逐")
                else:
                    out["texts"].append("营救失败，所幸未暴露行迹")
            return out
        # --- 通用计略 ---
        rate = R.plot_success_rate(wisdom_self, wisdom_target, bonus) * enemy_rate_mult
        success = (not countered) and rng.random() * 100 < rate
        out["success"] = success
        if countered:
            out["texts"].append("对方谋略科技「反间」生效，计略被识破反弹！")
        if not success:
            if countered or R.plot_expose(
                    rng, float(pcfg.get("expose_chance", 0.4)),
                    float(tech_eff.get("expose_reduction", 0.0))):
                out["exposed"] = True
                got = await self._expose_executor(world, c, f, target_fid)
                out["texts"].append(
                    (f"「{R.PLOT_NAMES.get(plot)}」失败，"
                     f"{c['name']} 身份暴露被擒（声望 -5）") if got else
                    (f"「{R.PLOT_NAMES.get(plot)}」失败，{c['name']} 身份暴露，"
                     f"但敌营已满被就地驱逐（声望 -5）"))
            else:
                out["texts"].append(f"「{R.PLOT_NAMES.get(plot)}」失败，无功而返")
            return out
        # --- 成功效果 ---
        # 计略生效有 2 tick 延迟：目标据点可能已被我方/盟友夺取。
        # 此时再落谣言/纵火/策应会打到自己人（烧自家粮仓、降自家城防），
        # 劝降也失去意义；统一在效果落点前复核归属。
        if str(PLOT_TARGET_KIND.get(plot) or "") == "node":
            tnode = await self.get_node(world["id"], int(target))
            if not tnode:
                out["success"] = False
                out["texts"].append("目标据点已不存在，计略落空")
                return out
            if int(tnode.get("owner_faction") or 0) in await self._friendly_ids(
                    int(world["id"]), fid):
                out["success"] = False
                out["texts"].append(
                    f"「{tnode['name']}」已归我方/盟友，计略中止")
                return out
        if plot == "alienate":
            tc = await self.get_char(target)
            # 复核目标状态：计略生效有 2 tick 延迟，期间目标可能已被俘、
            # 押运、陨落或已离间出走。旧逻辑只排除 dead，会把 captive /
            # transfer 的忠诚清零并“净化”囚禁标记，等于免费越狱。
            # v4.4.1：角色状态是 idle/task/army/research（march/siege 是
            # 军团状态），旧白名单只放过 idle，导致随军/任务/研究中的敌将
            # 明明在职却被判“已不在原势力任职”，离间必然落空。
            if not tc or int(tc.get("world_id", 0) or 0) != int(world["id"]) \
                    or tc.get("status") not in ("idle", "task", "army",
                                                "research"):
                out["texts"].append("离间目标已不在原势力任职，计略落空")
                return out
            if int(tc.get("faction_id") or 0) != target_fid:
                out["texts"].append("离间目标已转投他处，计略落空")
                return out
            lo, hi = pcfg.get("alienate_loyalty") or [-40, -20]
            drop = rng.randint(int(lo), int(hi))
            tc["loyalty"] = max(0, int(tc.get("loyalty", 70)) + drop)
            if tc["loyalty"] < 30:
                await self._detach_from_armies(tc)
                tc["status"] = "wander"
                tc["faction_id"] = 0
                tc["owner_user_id"] = ""
                tc["auction_cooldown_until"] = 0
                # 离心出走：清除囚禁标记（角色此前可能处于被俘状态），
                # 防 captor_faction/imprisoned_node_id 悬挂导致重复解放/迁移
                tc["captor_faction"] = 0
                tc["imprisoned_node_id"] = 0
                # 官职随出走一并解除（否则 taishou/都督仍在旧势力名册中）
                tc["appointed_position"] = ""
                tc["appointed_node_id"] = 0
                await self._clear_char_governors(world["id"], tc["id"])
                out["texts"].append(
                    f"离间大成！{tc['name']} 忠诚崩至 {tc['loyalty']}，挂印出走")
            else:
                out["texts"].append(
                    f"离间生效：{tc['name']} 忠诚 -{-drop}（现 {tc['loyalty']}）")
            await self.save_char(tc)
            await self._audit(str(c.get("owner_user_id", "") or ""),
                              "plot_alienate", {
                                  "char_id": int(tc["id"]),
                                  "loyalty": int(tc["loyalty"]),
                                  "drop": drop,
                                  "fled": tc["loyalty"] < 30})
        elif plot == "rumor":
            node = await self.get_node(world["id"], target)
            if node:
                delta = int(pcfg.get("rumor_morale", -15))
                # 谋略 L7 诡道：负面效果时长 +20%（谣言持续更久）
                ticks = max(1, int(int(pcfg.get("rumor_ticks", 3)) *
                                   float(tech_eff.get("plot_duration_mult",
                                                     1.0) or 1.0)))
                # §9/§15：驻守本城的太守可坐镇辟谣——民心动摇减半、
                # debuff 时长 -1 tick（与 _step_events 的谣言事件同口径）。
                gov = await self._valid_node_governor(node)
                owner_f = None
                owner_fid = int(node.get("owner_faction") or 0)
                if owner_fid > 0:
                    owner_f = await self.get_faction(owner_fid)
                mitigated = bool(
                    gov and await self._governor_onsite(node, gov)
                    and self._salary_governor_paid(owner_f, node["id"]))
                if mitigated:
                    delta = int(delta * 0.5)
                    ticks = max(1, ticks - 1)
                node["morale"] = max(0, int(node.get("morale", 80)) + delta)
                tick_no = int(world.get("tick_no", 0))
                # §6.3：产出 -20% 只落节点 output_json（plot_output_mult 读取），
                # 事件记录仅作展示。切勿再写 node_output 乘区，否则会与
                # plot_output_mult 叠乘成 0.8×0.8=0.64（-36%），偏离设计 -20%。
                pct = float(pcfg.get("rumor_output_pct", -0.2))
                out_d = node.get("output") or {}
                out_d["plot_debuff"] = {
                    "mult": max(0.1, 1.0 + pct),
                    "until": _now() + ticks * self.tick_seconds()}
                node["output"] = out_d
                await self.save_node(node)
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
                          {"type": "plot_effect", "resource": "coin",
                           "mult": 1 + pct},
                          {"type": "plot_effect", "resource": "grain",
                           "mult": 1 + pct}]},
                          ensure_ascii=False)))
                out["texts"].append(
                    f"谣言在「{node['name']}」蔓延：民心 {delta:+d}，"
                    f"产出 {pct * 100:+.0f}%（{ticks} tick"
                    + ("，太守坐镇影响减半" if mitigated else "") + "）")
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
            # 手操战役进行中：胜负未分，计略不得直接易主——否则战役还在打，
            # 据点已归攻方，终局的城防/俘虏/守军写回会与投降结算互相覆盖。
            if node and int(ss.get("attacker_faction", 0)) == fid and \
                    await self._active_manual_battle(int(world["id"]),
                                                     int(node["id"])):
                out["texts"].append(
                    f"🏳️「{node['name']}」守军仍在城头死战（手操战役进行中），"
                    f"劝降未果")
            elif node and int(ss.get("attacker_faction", 0)) == fid:
                # §10.6：劝降属无血开城，等级保留（by_force=False）
                await self._capture_node(world, node, fid, by_force=False)
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
                # v4.3.0：删除无读取方的 data.sabotage 死标记（此前文案
                # 「城门暗开 4 tick」暗示有后续效果，实际永不兑现）。
                out["texts"].append(
                    f"策应就绪：「{node['name']}」城防 -20%、守军士气挫败")
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

    def plot_output_mult(self, node: dict) -> float:
        """计略造成的据点产出乘区（§6.3 谣言 -20%）。

        供主管理器 `_tick_node_output` / 任务结算调用；读取
        ``node["output_debuff"]``（临时态）或持久化的
        ``node["output"]["plot_debuff"]``，过期自动失效。无减益返回 1.0。
        """
        if not node:
            return 1.0
        now = _now()
        mult = 1.0
        cands = []
        if node.get("output_debuff"):
            cands.append(node["output_debuff"])
        od = (node.get("output") or {}).get("plot_debuff")
        if od:
            cands.append(od)
        for d in cands:
            if isinstance(d, (int, float)):
                mult *= float(d)
                continue
            if not isinstance(d, dict):
                continue
            until = int(d.get("until", 0) or 0)
            if until and until <= now:
                continue
            mult *= float(d.get("mult", 1.0) or 1.0)
        return max(0.1, min(1.0, mult))

    async def _faction_best_wisdom(self, world_id: int, faction_id: int) -> int:
        if not faction_id or faction_id < 0:
            return 50
        best = 50
        for c in await self.get_chars(faction_id=faction_id, world_id=world_id):
            # v4.3.0：死者/濒死不参与谋略对抗——否则阵亡军师仍抬高防御
            # §7.2：被俘/押运中的军师在敌营大牢，同样不能为己方出谋划策
            if c.get("status") in ("dead", "captive", "transfer", "wander"):
                continue
            if R.injury_state(c.get("injury", 0)) in ("dying", "dead"):
                continue
            best = max(best, int((c.get("stats") or {}).get("wisdom", 0)))
        return best

    async def _expose_executor(self, world: dict, c: dict, f: Optional[dict],
                               target_fid: int) -> bool:
        """计略暴露：执行者被目标势力擒获 + 本方声望 -5。

        返回 True 表示成功关押；False 表示关押位已满、执行者被就地驱逐
        （调用方据此改写战报，不能一律写「被擒」）。
        """
        if f:
            await self.add_reputation(
                f, int((self.main_cfg().get("plot") or {}).get("expose_rep_cost", -5)))
        if target_fid and target_fid > 0 and int(c.get("faction_id", 0)) != target_fid:
            node_id = int((c.get("location") or {}).get("node_id", 0) or 0)
            # 俘虏必须关进「擒获方」的据点：此前直接用执行者当前所在地
            # （通常是其己方据点），会出现“被关在自己城里”，同势力成员
            # 可零成本营救，关押位统计也记在错误的节点上。
            jail = await self._nearest_own_node(
                world, {"faction_id": target_fid}, node_id,
                dwell_only=True) if node_id else None
            if not jail:
                return False
            c["status"] = "idle"  # 先脱离任务态再入囚
            return await self._capture_char(c, target_fid, int(jail))
        return False

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
        rows = await self._fetchall(
            "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
            (world["id"], owner))
        total = 0
        for row in rows:
            p = await self.get_player(row["user_id"], world["id"])
            if not p:
                continue
            loss = int(int(p.get("grain", 0)) * pct)
            p["grain"] = int(p.get("grain", 0)) - loss
            await self.save_player(p)
            total += loss
        return total
