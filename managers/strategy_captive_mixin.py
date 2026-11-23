# -*- coding: utf-8 -*-
"""「仙域争霸」P1 俘虏体系 Mixin（设计方案 §7.9）。

覆盖：被俘掷点（城陷 40%/巷战 +10%/覆没 20%/单挑 60%）、劝降、
释放、赎金设定与支付、随据点解放、关押忠诚衰减。

临时态（劝降 CD / 日限 / 赎金）存 strategy_worlds.data_json；
俘虏本体状态存 strategy_characters（status='captive' +
captor_faction + imprisoned_node_id）。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..core import strategy_rules as R
from .strategy_errors import StrategyError

logger = logging.getLogger("astrbot")


def _now() -> int:
    return int(time.time())


class StrategyCaptiveMixin:
    """P1 俘虏体系（§7.9）。"""

    # ------------------------------------------------------------------
    # 被俘掷点（战斗善后调用）
    # ------------------------------------------------------------------
    async def roll_captures(self, world: dict, node: dict, loser_armies: list[dict],
                            kind: str, captor_fid: int,
                            street: bool = False,
                            skip_ids: Optional[set[int]] = None) -> list[str]:
        """败方军团角色被俘判定。kind: city_fall/wipeout/duel。返回被俘者名单。

        skip_ids: 已由其他路径（如单挑 _pending_captures）确定要处理的角色，
        跳过二次掷点，避免同一角色在终局被重复判定/重复驱逐。
        """
        rng = self._rng("capture", world["id"], node["id"], world.get("tick_no", 0),
                        captor_fid, *[a["id"] for a in loser_armies])
        # v4.3.14 军事 L6/谋略「俘获」：俘获方被俘率加成；「血路/强运」：己方不被俘
        captor_f = await self.get_faction(int(captor_fid))
        capture_bonus = float(R.tech_effects((captor_f or {}).get("tech") or {},
                              tcfg=self.main_cfg().get("tech") or {}
                              ).get("capture_bonus", 0.0) or 0.0)
        vt_cache: dict[int, dict] = {}
        captured: list[str] = []
        evicted: list[str] = []
        evicted_home_owners: set[str] = set()
        all_chars = await self.get_chars(world_id=world["id"])
        for army in loser_armies:
            # 覆没军团的角色已在 _apply_battle_aftermath 统一 +40 伤势：
            # city_fall 若再补一次会叠加成 +80，可直接打成濒死/陨落。
            already_injured = kind == "wipeout" or (
                kind == "city_fall" and army.get("_destroyed"))
            for cid in army.get("characters") or []:
                if skip_ids:
                    try:
                        if int(cid) in skip_ids:
                            continue
                    except (TypeError, ValueError):
                        pass
                c = await self.get_char(int(cid))
                # 已被追击截获（_auto_retreat）或此前已入狱的角色不得二次判定：
                # 否则关押位满时会把刚俘的角色改回 idle（凭空释放）。
                if not c or c.get("status") in ("dead", "captive"):
                    continue
                # 军事 L7 无双「血路/强运」：己方角色不会被俘（仍走下方负伤分支）
                vfid = int(c.get("faction_id") or 0)
                vtech = vt_cache.get(vfid)
                if vtech is None:
                    vf = await self.get_faction(vfid) if vfid > 0 else None
                    vtech = R.tech_effects((vf or {}).get("tech") or {},
                                           tcfg=self.main_cfg().get("tech") or {})
                    vt_cache[vfid] = vtech
                no_cap = bool(vtech.get("no_capture", False))
                if not no_cap and R.capture_check(
                        kind, rng, street_fight=street, bonus=capture_bonus):
                    # §7.9：关押位满员 → 就地驱逐（不计入被俘名单）
                    if await self._capture_char(c, captor_fid, node["id"]):
                        captured.append(c["name"])
                    else:
                        evicted.append(c["name"])
                        ho = str((c.get("owner_user_id") or
                                  ((c.get("location") or {}).get("home_owner")
                                   or "")))
                        if ho:
                            evicted_home_owners.add(ho)
                else:
                    # 覆没分支（_apply_battle_aftermath / 围城反击）已统一给
                    # 全军团角色 +40 伤势；此处对 wipeout / 覆没的 city_fall
                    # 再加一次会叠加成 +80 直接把人打成陨落。
                    if already_injured:
                        await self.save_char(c)
                        continue
                    dead = R.apply_injury(c, 40)
                    if dead:
                        c["status"] = "dead"
                        legacy = R.legacy_on_death(c, all_chars)
                        for av in legacy.get("avengers") or []:
                            await self.save_char(av)
                        if legacy.get("heir"):
                            await self.save_char(legacy["heir"])
                    await self.save_char(c)
        # §7.1：城陷时抓捕池 = “城内的所有人”（守军军团已在上方覆盖）。
        # 追加 location 指向本城且处于 idle/task 的角色（含驻守太守），
        # 严格按 location 判定，绝不抓其他据点的空闲角色。
        if kind == "city_fall":
            handled: set[int] = set()
            for army in loser_armies:
                for cid in army.get("characters") or []:
                    try:
                        handled.add(int(cid))
                    except (TypeError, ValueError):
                        pass
            if skip_ids:
                for x in skip_ids:
                    try:
                        handled.add(int(x))
                    except (TypeError, ValueError):
                        pass
            captor_f = await self.get_faction(int(captor_fid))
            captor_ids = await self._alliance_ids(int(world["id"]), captor_f) \
                if captor_f else {int(captor_fid)}
            captor_ids.add(int(captor_fid))
            for cc in all_chars:
                cid = int(cc["id"])
                if cid in handled:
                    continue
                if str(cc.get("status") or "") not in ("idle", "task"):
                    continue
                if R.char_node_id(cc) != int(node["id"]):
                    continue
                if int(cc.get("faction_id") or 0) in captor_ids:
                    continue  # 攻方/盟友自己人不得误抓
                vfid2 = int(cc.get("faction_id") or 0)
                vtech2 = vt_cache.get(vfid2)
                if vtech2 is None:
                    vf2 = await self.get_faction(vfid2) if vfid2 > 0 else None
                    vtech2 = R.tech_effects((vf2 or {}).get("tech") or {},
                                            tcfg=self.main_cfg().get("tech") or {})
                    vt_cache[vfid2] = vtech2
                if not vtech2.get("no_capture", False) and R.capture_check(
                        kind, rng, street_fight=street, bonus=capture_bonus):
                    if await self._capture_char(cc, captor_fid, node["id"]):
                        captured.append(cc["name"])
                    else:
                        evicted.append(cc["name"])
                        ho = str((cc.get("owner_user_id") or
                                  ((cc.get("location") or {}).get("home_owner")
                                   or "")))
                        if ho:
                            evicted_home_owners.add(ho)
                else:
                    # 未被俘的城内 idle/本地任务角色不能留在已沦陷的敌城：
                    # 落点退回本人最近可用据点（原势力已无据点则清空），
                    # 与 §3“人物永不落在敌方/无主/遗迹”一致。
                    dest = await self._char_fallback_location(
                        int(world["id"]), cc, int(node["id"]))
                    cc["location"] = {"node_id": int(dest)} if dest else {}
                    await self.save_char(cc)
        await self._commit()
        if captured:
            logger.info(f"[strategy] 「{node.get('name')}」被俘 {len(captured)} 人："
                        f"{'、'.join(captured)}（ captor={captor_fid} kind={kind}）")
        if evicted:
            logger.info(f"[strategy] 「{node.get('name')}」关押位已满，"
                        f"就地驱逐 {'、'.join(evicted)}")
            rows = await self._fetchall(
                "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
                (world["id"], int(captor_fid)))
            await self._notify(
                world, [r["user_id"] for r in rows], "captive_evicted",
                {"node_id": int(node["id"]),
                 "text": f"「{node.get('name')}」关押位已满，"
                         f"{'、'.join(evicted)} 被就地驱逐（返回原势力，忠诚 -5）"})
            # v4.3.0：同步通知原主——此前只通知关押方，角色驱逐归来时原主无感知。
            home_owners = evicted_home_owners - {r["user_id"] for r in rows}
            if home_owners:
                await self._notify(
                    world, list(home_owners), "captive_evicted_home",
                    {"node_id": int(node["id"]),
                     "text": f"「{node.get('name')}」关押位已满，"
                             f"{'、'.join(evicted)} 就地驱逐归来（忠诚 -5）"})
        return captured

    async def _detach_from_armies(self, c: dict) -> None:
        """把角色从所有军团名册移除（被俘/驱逐后不可随军、不可领队）。"""
        cid = int(c["id"])
        wid = int(c.get("world_id", 0) or 0)
        if not wid:
            return
        for a in await self.get_armies(wid):
            cids = [int(x) for x in (a.get("characters") or [])]
            if cid in cids:
                cids.remove(cid)
                a["characters"] = cids
                await self.save_army(a)

    async def _capture_char(self, c: dict, captor_fid: int, node_id: int) -> bool:
        """关押角色；目标节点关押位已满则「就地驱逐」（§7.9）。

        返回 True 表示成功关押为俘虏；False 表示关押位满、角色被就地
        驱逐回原势力（status=idle、忠诚 -5），不视为被俘。
        """
        # v4.3.0：无合法擒获方/关押点时拒绝入狱——captor_faction=0 的
        # 孤儿俘虏无法被任何势力劝降/收赎/解放，只会永久占用名册与牢位
        captor_fid = int(captor_fid or 0)
        node_id = int(node_id or 0)
        if captor_fid <= 0 or node_id <= 0:
            logger.info(f"[strategy] 拒绝无效关押 {c.get('name')}"
                        f"（captor={captor_fid} node={node_id}）")
            return False
        wid = int(c.get("world_id", 0) or 0)
        # §3：遗迹不可作为关押/落点；攻守战场在遗迹时改关到擒获方最近的
        # 可用据点，避免俘虏被塞进遗迹（后续招募/押运都会被拒）。
        jail_node = await self.get_node(wid, node_id) if wid else None
        if not jail_node or str(jail_node.get("type") or "") \
                not in R.CHAR_DWELL_NODE_TYPES:
            alt = await self._nearest_own_node(
                {"id": wid}, {"faction_id": captor_fid}, node_id,
                dwell_only=True) if wid else None
            if not alt:
                logger.info(f"[strategy] 擒获方无可用关押据点，跳过关押 "
                            f"{c.get('name')}")
                return False
            node_id = int(alt)
        ccfg = self.main_cfg().get("captive") or {}
        slots = max(0, int(ccfg.get("jail_slots_per_node", 5)))
        wid = int(c.get("world_id", 0) or 0)
        # 重新被擒：上一次关押遗留的赎金报价已失效（收款方/关押点可能
        # 已变），必须先清理，避免支付到错误势力或旧价复用。
        await self._clear_ransom(wid, int(c["id"]))
        await self._detach_from_armies(c)
        # §7.1：被俘者同时从进行中的任务摘除（否则俘虏仍挂着 status='task'
        # 的任务行，持续任务会反复结算/卡住名册名额）
        if wid:
            # 协作任务：执行者被俘时先把搭档释放回 idle，否则任务行作废后
            # 搭档既无任务行可结算、又不能移动/派遣，永久卡在 task 态。
            for trow in await self._fetchall(
                    "SELECT * FROM strategy_tasks WHERE world_id=? "
                    "AND character_id=? AND status='active'",
                    (wid, int(c["id"]))):
                await self._release_task_partner_fallback(
                    wid, self._decode("strategy_tasks", trow))
            await self.db.conn.execute(
                "UPDATE strategy_tasks SET status='cancelled' "
                "WHERE world_id=? AND character_id=? AND status='active'",
                (wid, int(c["id"])))
        used = 0
        if slots > 0:
            row = await self._fetchone(
                "SELECT COUNT(*) AS n FROM strategy_characters "
                "WHERE world_id=? AND status='captive' AND imprisoned_node_id=?",
                (wid, int(node_id)))
            used = int((row or {}).get("n", 0) or 0)
        loc = c.get("location") or {}
        prev_node = int(loc.get("node_id", 0) or 0)
        loc.pop("transfer_to", None)
        loc.update({"node_id": int(node_id),
                    "home_faction": int(c.get("faction_id", 0) or 0),
                    "home_owner": c.get("owner_user_id", "")})
        c["location"] = loc
        if slots > 0 and used >= slots:
            # 就地驱逐回原势力：落点须为己方/联盟据点。此前沿用擒获方节点，
            # 城破后该节点已是敌方城，角色等于被“释放”进敌营；原势力已无
            # 据点（流亡）时也不能停在敌城，改为无落点（回城/传送时再定位）。
            home_fid = int(c.get("faction_id", 0) or 0)
            dest = 0
            if home_fid > 0 and wid:
                dest = int(await self._char_fallback_location(
                    wid, {"faction_id": home_fid}, int(prev_node)) or 0)
                if dest > 0:
                    dnode = await self.get_node(wid, int(dest))
                    if not dnode or str(dnode.get("type") or "") \
                            not in R.CHAR_DWELL_NODE_TYPES:
                        # 遗迹等不可作为人物落点（§3）：改取最近可用己方据点
                        dest = int(await self._nearest_own_node(
                            {"id": wid}, {"faction_id": home_fid},
                            int(prev_node), dwell_only=True) or 0)
            loc["node_id"] = dest
            c["location"] = loc
            c["status"] = "idle"
            c["captor_faction"] = 0
            c["imprisoned_node_id"] = 0
            c["loyalty"] = max(0, int(c.get("loyalty", 70)) - 5)
            await self.save_char(c)
            logger.info(f"[strategy] 节点 {node_id} 关押位已满（{used}/{slots}），"
                        f"{c['name']} 就地驱逐（忠诚 -5）")
            return False
        c["status"] = "captive"
        c["captor_faction"] = int(captor_fid)
        c["imprisoned_node_id"] = int(node_id)
        # 被俘即失去官职与据点太守：否则被俘太守仍在原城享受任命加成。
        c["appointed_position"] = ""
        c["appointed_node_id"] = 0
        await self._clear_char_governors(wid, c["id"])
        await self.save_char(c)
        logger.debug(f"[strategy] {c['name']} 被关押于节点 {node_id}（{used + 1}/{slots}）")
        return True

    # ------------------------------------------------------------------
    # 劝降 / 释放 / 赎金 / 解放
    # ------------------------------------------------------------------
    async def persuade_captive(self, user_id: str, char_id: int,
                               persuader_char_id: int = 0) -> dict:
        world, p = await self._require_player(user_id)
        ccfg = self.main_cfg().get("captive") or {}
        c = await self.get_char(int(char_id))
        if not c or c.get("status") != "captive":
            raise StrategyError("目标不是俘虏")
        # §7.9：濒死俘虏不可劝降（须先救治）
        if R.injury_state(c.get("injury", 0)) in ("dying", "dead"):
            raise StrategyError(f"{c['name']} 伤势濒死，须先救治方可劝降")
        captor_fid = int(c.get("captor_faction", 0))
        fid = int(p["faction_id"])
        # §7.9 离心：忠诚 ≤ defect_loyalty 后「任意势力」均可劝降（固定 80%）；
        # 关押方本身仍按忠诚公式结算（低忠诚时同样接近必成）
        defect_threshold = int(ccfg.get("defect_loyalty", 0))
        loyalty = int(c.get("loyalty", 70))
        defect_eligible = loyalty <= defect_threshold
        defect = defect_eligible and captor_fid != fid
        if captor_fid != fid and not defect_eligible:
            raise StrategyError("该俘虏不在我方关押之下（尚未离心，仅关押方可劝降）")
        # 说服者：指定或自动取魅力+智谋最高的空闲角色
        persuader = None
        if persuader_char_id:
            persuader = await self._own_char(p, persuader_char_id)
            if persuader.get("status") != "idle":
                raise StrategyError(
                    f"{persuader['name']} 状态为「"
                    f"{R.CHAR_STATUS_NAMES.get(persuader['status'], persuader['status'])}」，"
                    f"无法劝降")
        else:
            best = -1
            for mine in await self.get_chars(owner_user_id=user_id,
                                             world_id=world["id"], status="idle"):
                st = mine.get("stats") or {}
                score = int(st.get("charm", 0)) + int(st.get("wisdom", 0))
                if score > best and R.injury_state(mine.get("injury", 0)) not in (
                        "dying", "dead"):
                    best, persuader = score, mine
        if not persuader:
            raise StrategyError("没有可用的说服者（需空闲且非濒死角色）")
        if R.injury_state(persuader.get("injury", 0)) in ("dying", "dead"):
            raise StrategyError(f"{persuader['name']} 伤势过重，无法劝降")
        # 劝降成功会新增一名角色：先校验名册上限，避免绕过招募限制
        cap_info = await self._char_cap_info(world["id"], p)
        if await self._roster_count(user_id, world["id"]) >= int(cap_info["cap"]):
            raise StrategyError(
                f"角色数量已达上限（{cap_info['cap']}）："
                f"扩建据点或遣散空闲角色后再劝降")
        data = world.get("data") or {}
        cd_map = data.setdefault("persuade_cd", {})
        if int(cd_map.get(str(c["id"]), 0)) > _now():
            hours = (int(cd_map[str(c["id"])]) - _now() + 3599) // 3600
            raise StrategyError(f"劝降冷却中（还剩 {hours}h）")
        daily = data.setdefault("persuade_daily", {}).setdefault(
            str(fid), {})
        today = time.strftime("%Y%m%d")
        limit = int(ccfg.get("persuade_daily_limit", 3))
        if int(daily.get(today, 0)) >= limit:
            raise StrategyError(f"今日劝降次数已用完（{limit} 次/日）")
        f = await self.get_faction(fid)
        rep = int((f or {}).get("reputation", 0))
        # §7.9：恶名（声誉 ≤ -60）不可对离心俘虏劝降
        if defect and rep <= -60:
            raise StrategyError("恶名昭彰，离心者亦不愿归顺（声望须高于 -60）")
        if defect:
            rate = 0.80
        else:
            bonus = await self._faction_plot_bonus(f)
            rate = R.persuade_rate(loyalty, persuader, rep,
                                   disloyal=loyalty <= 0) + bonus
            # v4.3.0：与配置 persuade_clamp / 规则层统一为 5%~90%
            # （此前加成后再钳 0.95，可突破设计 90% 上限）
            lo_hi = ccfg.get("persuade_clamp") or [5, 90]
            lo = float(lo_hi[0]) / 100.0 if len(lo_hi) >= 1 else 0.05
            hi = float(lo_hi[1]) / 100.0 if len(lo_hi) >= 2 else 0.90
            rate = max(lo, min(hi, rate))
        rng = self._rng("persuade", c["id"], user_id, _now() // 60)
        success = rng.random() < rate
        # CD 与日限：无论成败都消耗
        cd_map[str(c["id"])] = _now() + int(ccfg.get("persuade_cd_hours", 24)) * 3600
        daily[today] = int(daily.get(today, 0)) + 1
        data["persuade_cd"] = cd_map
        data["persuade_daily"] = daily
        world["data"] = data
        await self.save_world(world)
        if not success:
            c["loyalty"] = max(0, int(c.get("loyalty", 70)) - 5)
            await self.save_char(c)
            await self._commit()
            await self._audit(user_id, "persuade_captive", {
                "char_id": int(c["id"]), "captor_faction": captor_fid,
                "mode": "defect" if defect else "normal",
                "result": "fail", "rate": round(rate, 3)})
            return {"ok": True, "result": "fail",
                    "msg": f"{c['name']} 不为所动（成功率 {rate * 100:.0f}%），忠诚微降"
                           f"（也可用「招募俘虏」直接入册，需空闲名册槽位）"}
        # 成功入仕：迁往己方最近据点（不可继续站在敌营关押点）
        home_f = int((c.get("location") or {}).get("home_faction", 0) or 0)
        jail_node = int(c.get("imprisoned_node_id", 0)
                        or ((c.get("location") or {}).get("node_id", 0) or 0))
        dest = await self._nearest_own_node(
            world, p, jail_node, dwell_only=True) or 0
        c["status"] = "idle"
        c["faction_id"] = fid
        c["owner_user_id"] = user_id
        c["captor_faction"] = 0
        c["imprisoned_node_id"] = 0
        # 归顺/离心后原官职与据点太守失效，否则会继续吃旧势力加成。
        c["appointed_position"] = ""
        c["appointed_node_id"] = 0
        await self._clear_char_governors(int(world["id"]), c["id"])
        loc = c.get("location") or {}
        loc.pop("transfer_to", None)
        c["location"] = {"node_id": int(dest) if dest else 0}
        # §4.5 知遇：先结羁绊（form_zhiyu 会 +10 忠诚），再强制重置为 40
        # ——设计口径「归顺忠诚 40」优先于知遇加成。
        if persuader and int(persuader.get("id", 0)) != int(c["id"]):
            c["recruited_by"] = int(persuader["id"])
            R.form_zhiyu(persuader, c)
            await self.save_char(persuader)
        c["loyalty"] = 40
        # 归顺即不再是俘虏：旧赎金条目必须清掉，避免原关押方继续收款
        await self._clear_ransom(int(world["id"]), int(c["id"]))
        await self.save_char(c)
        rep_cost = 0
        if defect:
            # §7.9 离心：消耗劝降方声望（defect_rep_cost 负值即扣）
            rep_cost = int(ccfg.get("defect_rep_cost", -3))
            if f:
                await self.add_reputation(f, rep_cost)
        elif home_f and home_f != fid:
            of = await self.get_faction(home_f)
            if of:
                rep_cost = int(ccfg.get("defect_rep_cost", -3))
                await self.add_reputation(of, rep_cost)
        if c.get("rarity") in ("epic", "legend"):
            await self._award_achievement(user_id, "social_persuade_epic")
        await self._commit()
        await self._audit(user_id, "persuade_captive", {
            "char_id": int(c["id"]), "captor_faction": captor_fid,
            "mode": "defect" if defect else "normal",
            "result": "success", "rate": round(rate, 3),
            "rep_cost": rep_cost})
        if defect:
            return {"ok": True, "result": "success",
                    "msg": f"🎉 {c['name']} 离心归顺！（忠诚 40，"
                           f"我方声望 {rep_cost}）"}
        return {"ok": True, "result": "success",
                "msg": f"🎉 {c['name']} 归顺！（忠诚 40，原势力声望 -3）"}

    async def recruit_captive(self, user_id: str, char_id: int,
                              node_id: int = 0) -> dict:
        """大牢正式招募（§7.3）：占用空闲名册槽位，把俘虏转为己方角色。

        与 ``persuade_captive``（降忠诚/建羁绊，成功后仍按旧交互直接归顺）
        分开：本动作不看劝降成功率，但严格要求**有空闲名册槽位**。
        可招降口径沿用劝降：我方关押的俘虏，或已离心（忠诚 ≤
        defect_loyalty）的任意俘虏。
        """
        world, p = await self._require_player(user_id)
        ccfg = self.main_cfg().get("captive") or {}
        c = await self.get_char(int(char_id))
        if not c or c.get("status") != "captive":
            raise StrategyError("目标不是俘虏")
        if R.injury_state(c.get("injury", 0)) in ("dying", "dead"):
            raise StrategyError(f"{c['name']} 伤势濒死，须先救治方可招募")
        fid = int(p["faction_id"])
        captor_fid = int(c.get("captor_faction", 0))
        defect_threshold = int(ccfg.get("defect_loyalty", 0))
        defect = (captor_fid != fid
                  and int(c.get("loyalty", 70)) <= defect_threshold)
        if captor_fid != fid and not defect:
            raise StrategyError("该俘虏不在我方关押之下（尚未离心，无法招募）")
        # 名册槽位（大牢容量与名册容量相互独立，入册才占名额）
        cap_info = await self._char_cap_info(world["id"], p)
        if await self._roster_count(user_id, world["id"]) >= int(cap_info["cap"]):
            raise StrategyError("名册已满，请先扩建据点或遣散角色")
        # 落点：指定己方据点，否则由关押点/主城就近取己方据点
        f = await self.get_faction(fid)
        # 与 persuade_captive 同口径：恶名势力不能招揽离心俘虏
        if defect and int((f or {}).get("reputation", 0)) <= -60:
            raise StrategyError("恶名昭彰，离心者亦不愿归顺（声望须高于 -60）")
        jail_node = int(c.get("imprisoned_node_id", 0)
                        or ((c.get("location") or {}).get("node_id", 0) or 0))
        dest = 0
        if int(node_id or 0):
            dest_node = await self.get_node(world["id"], int(node_id))
            if not dest_node:
                raise StrategyError("目标据点不存在")
            if int(dest_node.get("owner_faction") or 0) != fid:
                raise StrategyError(f"「{dest_node['name']}」不是我方据点")
            if str(dest_node.get("type") or "") not in R.CHAR_DWELL_NODE_TYPES:
                raise StrategyError("遗迹不可作为人物落点")
            dest = int(dest_node["id"])
        if not dest:
            dest = await self._nearest_own_node(
                world, {"faction_id": fid}, jail_node, dwell_only=True) or 0
        if not dest:
            capital = int((f or {}).get("capital_node_id", 0) or 0)
            dest = await self._nearest_own_node(
                world, {"faction_id": fid}, capital, dwell_only=True) or 0
        if not dest:
            raise StrategyError("我方尚无可用据点，无法安置新角色")
        home_f = int((c.get("location") or {}).get("home_faction", 0) or 0)
        c["status"] = "idle"
        c["faction_id"] = fid
        c["owner_user_id"] = user_id
        c["captor_faction"] = 0
        c["imprisoned_node_id"] = 0
        # 归顺后原官职/太守失效，否则会继续吃旧势力加成
        c["appointed_position"] = ""
        c["appointed_node_id"] = 0
        await self._clear_char_governors(int(world["id"]), c["id"])
        # location 只保留落点，清空 home_faction/home_owner/transfer_to
        c["location"] = {"node_id": int(dest)}
        # 忠诚口径与劝降归顺一致（40）
        c["loyalty"] = 40
        if defect and f:
            await self.add_reputation(f, int(ccfg.get("defect_rep_cost", -3)))
        elif home_f and home_f != fid:
            of = await self.get_faction(home_f)
            if of:
                await self.add_reputation(of, int(ccfg.get("defect_rep_cost", -3)))
        # 入册后不再是俘虏：清除旧赎金，避免原关押方继续“收取”已归顺者
        await self._clear_ransom(int(world["id"]), int(c["id"]))
        await self.save_char(c)
        await self._commit()
        dest_node = await self.get_node(world["id"], int(dest))
        dest_name = (dest_node or {}).get("name") or f"#{int(dest)}"
        logger.info(f"[strategy] {user_id} 从大牢招募 {c['name']}"
                    f"（node={dest}）")
        return {"ok": True, "char_id": int(c["id"]),
                "msg": f"📜 {c['name']} 已入我方名册（忠诚 40，驻地"
                       f"「{dest_name}」）"}

    async def _faction_plot_bonus(self, f: Optional[dict]) -> float:
        """势力角色官职（祭酒）对计略/劝降的加成合计。"""
        if not f:
            return 0.0
        host = await self._alliance_host(f) or f
        bonus = await self._faction_position_bonus(host)
        return float(bonus.get("plot_bonus", 0.0))

    async def release_captive(self, user_id: str, char_id: int) -> dict:
        """无条件释放：声望 +3、与原势力外交 +5。

        v4.3.0：仅势力主/副主可释放——此前任意成员可刷声望与外交分。
        """
        world, p = await self._require_player(user_id)
        ccfg = self.main_cfg().get("captive") or {}
        c = await self.get_char(int(char_id))
        if not c or c.get("status") != "captive":
            raise StrategyError("目标不是俘虏")
        if int(c.get("captor_faction", 0)) != int(p["faction_id"]):
            raise StrategyError("该俘虏不在我方关押之下")
        f = await self.get_faction(p["faction_id"])
        if not f:
            raise StrategyError("势力不存在")
        await self._require_position(f, user_id, ("leader", "deputy", "host"))
        await self.add_reputation(f, int(ccfg.get("release_rep_gain", 3)))
        home_f = int((c.get("location") or {}).get("home_faction", 0) or 0)
        if home_f and home_f != int(p["faction_id"]):
            await self._diplomacy_score_add(p["faction_id"], home_f,
                                            int(ccfg.get("release_diplomacy_gain", 5)))
        await self._return_captive_home(c)
        await self._commit()
        return {"ok": True,
                "msg": f"🕊 已释放 {c['name']}（声望 +{int(ccfg.get('release_rep_gain', 3))}，"
                       f"外交关系改善）"}

    async def _diplomacy_score_add(self, fid_a: int, fid_b: int, score: int):
        fa = await self.get_faction(fid_a)
        fb = await self.get_faction(fid_b)
        if fa:
            dip = fa.get("diplomacy") or {}
            entry = dip.setdefault(str(fid_b), {"rel": R.REL_NEUTRAL})
            entry["score"] = int(entry.get("score", 0)) + int(score)
            fa["diplomacy"] = dip
            await self.save_faction(fa)
        if fb:
            dip = fb.get("diplomacy") or {}
            entry = dip.setdefault(str(fid_a), {"rel": R.REL_NEUTRAL})
            entry["score"] = int(entry.get("score", 0)) + int(score)
            fb["diplomacy"] = dip
            await self.save_faction(fb)

    async def _clear_ransom(self, world_id: int, char_id: int) -> None:
        """清除某角色的赎金条目。

        释放 / 入册 / 转押 / 据点解放 / 势力崩溃后，旧报价的收款方与
        关押方已失效；不清理会让同一角色下次被俘时复用旧赎金，并让
        ``world.data.ransoms`` 随历史俘虏无限膨胀。
        """
        wid = int(world_id or 0)
        cid = int(char_id or 0)
        if wid <= 0 or cid <= 0:
            return
        w = await self.get_world()
        if not w or int(w.get("id") or 0) != wid:
            row = await self._fetchone(
                "SELECT * FROM strategy_worlds WHERE id=?", (wid,))
            w = self._decode("strategy_worlds", row)
        if not w:
            return
        data = w.get("data") or {}
        ransoms = data.get("ransoms")
        if not isinstance(ransoms, dict) or str(cid) not in ransoms:
            return
        ransoms.pop(str(cid), None)
        data["ransoms"] = ransoms
        w["data"] = data
        await self.save_world(w)

    async def _return_captive_home(self, c: dict):
        loc = c.get("location") or {}
        loc.pop("transfer_to", None)
        home_f = int(loc.get("home_faction", 0) or 0)
        home_owner = loc.get("home_owner", "") or ""
        # v4.3.0：优先查原主当前所属势力——快照 home_faction 可能已过时
        # （原主已退盟/入他盟/势力崩溃），按旧快照会把人送回错误阵营。
        wid = int(c.get("world_id", 0) or 0)
        # 释放即失效赎金：旧报价不得在下次被俘时复用
        await self._clear_ransom(wid, int(c["id"]))
        if home_owner and wid:
            owner_p = await self.get_player(home_owner, wid)
            if owner_p and int(owner_p.get("faction_id", 0) or 0) > 0:
                cur_fid = int(owner_p["faction_id"])
                cur_f = await self.get_faction(cur_fid)
                if cur_f and int(cur_f.get("alive", 1)):
                    home_f = cur_fid
        home_faction = await self.get_faction(home_f) if home_f else None
        c["status"] = "idle"
        c["captor_faction"] = 0
        c["imprisoned_node_id"] = 0
        # 释放后必须脱离敌营：此前 location.node_id 仍指向关押节点，
        # 角色虽已"回家"却站在别人城里，且官职/太守残留继续吃加成。
        c["appointed_position"] = ""
        c["appointed_node_id"] = 0
        await self._clear_char_governors(wid, c["id"])
        if home_faction and int(home_faction.get("alive", 1)):
            c["faction_id"] = home_f
            c["owner_user_id"] = home_owner
            cur = int(loc.get("node_id", 0) or 0)
            # §3/§4.4：落点须为己方/联盟可用据点；原势力已只剩遗迹时
            # 不能再把释放者丢回遗迹/敌营，确实无处可去则清空落点
            # （之后由进场自愈或传送重新定位）。
            dest = await self._char_fallback_location(
                wid, {"faction_id": home_f}, cur)
            loc["node_id"] = int(dest) if dest > 0 else 0
        else:
            c["faction_id"] = 0
            c["owner_user_id"] = ""
            c["status"] = "wander"
            c["auction_cooldown_until"] = 0
            loc["node_id"] = 0
        c["location"] = loc
        c["loyalty"] = max(int(c.get("loyalty", 70)), 60)
        await self.save_char(c)

    async def set_ransom(self, user_id: str, char_id: int, amount: int) -> dict:
        world, p = await self._require_player(user_id)
        ccfg = self.main_cfg().get("captive") or {}
        if not ccfg.get("ransom_enable", True):
            raise StrategyError("赎金功能未开启")
        c = await self.get_char(int(char_id))
        if not c or c.get("status") != "captive":
            raise StrategyError("目标不是俘虏")
        if int(c.get("captor_faction", 0)) != int(p["faction_id"]):
            raise StrategyError("该俘虏不在我方关押之下")
        amount = int(amount)
        if amount <= 0 or amount > 10_000_000:
            raise StrategyError("赎金须为 1~1000 万铜钱")
        data = world.get("data") or {}
        ransoms = data.setdefault("ransoms", {})
        ransoms[str(c["id"])] = {"amount": amount,
                                 "captor_fid": int(p["faction_id"]), "at": _now()}
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return {"ok": True, "msg": f"已为 {c['name']} 设定赎金 {amount} 铜钱"}

    async def pay_ransom(self, user_id: str, char_id: int) -> dict:
        world, p = await self._require_player(user_id)
        c = await self.get_char(int(char_id))
        if not c or c.get("status") != "captive":
            raise StrategyError("目标不是俘虏")
        loc = c.get("location") or {}
        if loc.get("home_owner") != user_id:
            raise StrategyError("只能赎回属于自己的角色")
        data = world.get("data") or {}
        r = (data.get("ransoms") or {}).get(str(c["id"]))
        if not r:
            raise StrategyError("对方未设定赎金（可等待劝降冷却或武力解放）")
        amount = int(r.get("amount", 0))
        if int(r.get("captor_fid", 0)) != int(c.get("captor_faction", 0)) or \
                int(c.get("captor_faction", 0)) <= 0:
            # 俘虏已被转押/解放/被第三方重新擒获，旧赎金条目的收款方
            # 已失效；继续支付会把铜钱打给错误势力。
            (data.get("ransoms") or {}).pop(str(c["id"]), None)
            world["data"] = data
            await self.save_world(world)
            await self._commit()
            raise StrategyError("赎金信息已失效（俘虏已转押或被重新擒获），"
                                "请等待对方重新设定")
        if int(p.get("coin", 0)) < amount:
            raise StrategyError(f"铜钱不足：赎金 {amount}")
        p["coin"] -= amount
        captor_fid = int(r.get("captor_fid", 0))
        cf = await self.get_faction(captor_fid)
        if not cf or not int(cf.get("alive", 0)):
            # 关押势力已消亡：没人能接收赎金。直接让俘虏脱身，避免
            # 玩家付了钱却打进已覆灭势力的国库（永久卡死）。
            data.get("ransoms", {}).pop(str(c["id"]), None)
            world["data"] = data
            p["coin"] += amount  # 未实际支付
            await self.save_world(world)
            await self._return_captive_home(c)
            await self.save_player(p)
            await self._commit()
            return {"ok": True,
                    "msg": f"关押势力已消亡，{c['name']} 自行脱身归来（未扣赎金）"}
        treasury = cf.get("treasury") or {}
        treasury["coin"] = int(treasury.get("coin", 0)) + amount
        cf["treasury"] = treasury
        await self.save_faction(cf)
        data["ransoms"].pop(str(c["id"]), None)
        world["data"] = data
        await self.save_world(world)
        await self._return_captive_home(c)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": f"💰 支付赎金 {amount}，{c['name']} 平安归来"}

    async def liberate_captives_at_node(self, world: dict, node: dict,
                                        liberator_fid: int) -> list[str]:
        """据点易主：关押于此的俘虏全部解放（解放方获战功+成就）。"""
        ccfg = self.main_cfg().get("captive") or {}
        freed: list[str] = []
        for c in await self.get_chars(world_id=world["id"], status="captive"):
            if int(c.get("imprisoned_node_id", 0)) != int(node["id"]):
                continue
            if int(c.get("captor_faction", 0)) == int(liberator_fid):
                continue  # 自己关的自己带走（迁往新关押地：保持 captive，随势力）
            merit = int(ccfg.get("liberate_merit", 50))
            # C-M5：战功归解放方（§7.9「解放者获战功 +50/人」），发给解放
            # 势力全体玩家；此前误发给被俘者原主（home_owner）
            rows = await self._fetchall(
                "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
                (world["id"], int(liberator_fid)))
            for row in rows:
                lp = await self.get_player(row["user_id"], world["id"])
                if lp:
                    gain = await self._settle_merit(
                        row["user_id"], merit, "liberate")
                    if gain > 0:
                        lp["merit"] = int(lp.get("merit", 0)) + gain
                        lp["merit_season"] = int(lp.get("merit_season", 0)) + gain
                        await self.save_player(lp)
                await self._award_achievement(row["user_id"], "social_liberate")
            await self._return_captive_home(c)
            freed.append(c["name"])
        if freed:
            await self._commit()
            logger.info(f"[strategy] 「{node.get('name')}」解放俘虏：{'、'.join(freed)}")
        return freed

    # ------------------------------------------------------------------
    # tick：关押忠诚衰减（§7.9：-1/tick，归零离心）
    # ------------------------------------------------------------------
    async def _step_captives(self, world: dict):
        changed = False
        # 赎金条目 GC：只保留仍在押（captive/transfer）且有擒获方的角色。
        # 释放/入册/据点解放/势力崩溃/角色陨落遗留的旧报价在这里统一清掉，
        # 防止旧赎金被复用、world.data 随历史俘虏无限膨胀。
        data = world.get("data") or {}
        ransoms = data.get("ransoms")
        if isinstance(ransoms, dict) and ransoms:
            rows = await self._fetchall(
                "SELECT id, captor_faction FROM strategy_characters WHERE world_id=? "
                "AND status IN ('captive','transfer') AND captor_faction>0",
                (world["id"],))
            valid = {str(int(r["id"])): int(r["captor_faction"] or 0)
                     for r in rows}
            stale = []
            for k, v in list(ransoms.items()):
                fid = valid.get(str(k))
                # 旧报价：角色已不在押，或关押方已变（转押/被第三方重新擒获）
                # 时收款方失效，必须清掉，避免旧价复用或赎金打给错误势力。
                if fid is None or not isinstance(v, dict) or \
                        int(v.get("captor_fid") or 0) != fid:
                    stale.append(k)
            if stale:
                for k in stale:
                    ransoms.pop(k, None)
                data["ransoms"] = ransoms
                world["data"] = data
                changed = True
        decay = int((self.main_cfg().get("captive") or {}).get(
            "loyalty_decay_per_tick", 1))
        if decay > 0:
            for c in await self.get_chars(world_id=world["id"], status="captive"):
                if int(c.get("loyalty", 0)) > 0:
                    c["loyalty"] = max(0, int(c["loyalty"]) - decay)
                    await self.save_char(c)
                    changed = True
        if changed:
            if data.get("ransoms") is not None:
                await self.save_world(world)
            await self._commit()

    # ------------------------------------------------------------------
    # 押运（§7.9：关押地可转移，走陆路可被截获）
    # ------------------------------------------------------------------
    async def transfer_captive(self, user_id: str, char_id: int,
                               to_node_id: int) -> dict:
        """把俘虏押往本势力另一据点：行程 rescue_ticks 小时，途中可被营救截获。"""
        world, p = await self._require_player(user_id)
        ccfg = self.main_cfg().get("captive") or {}
        c = await self.get_char(int(char_id))
        if not c or c.get("status") != "captive":
            raise StrategyError("目标不是俘虏（押运中或已归顺者不可再押运）")
        if int(c.get("captor_faction", 0)) != int(p["faction_id"]):
            raise StrategyError("该俘虏不在我方关押之下")
        to_node = await self.get_node(world["id"], int(to_node_id))
        if not to_node:
            raise StrategyError("目标据点不存在")
        if int(to_node["owner_faction"]) != int(p["faction_id"]):
            raise StrategyError(f"「{to_node['name']}」不是我方据点，无法押运")
        if str(to_node.get("type") or "") not in R.CHAR_DWELL_NODE_TYPES:
            raise StrategyError("遗迹不可作为关押据点")
        from_node = int(c.get("imprisoned_node_id", 0) or 0)
        if from_node and from_node == int(to_node["id"]):
            raise StrategyError("该俘虏已关押在目标据点")
        slots = max(1, int(ccfg.get("jail_slots_per_node", 5)))
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM strategy_characters WHERE world_id=? "
            "AND status='captive' AND imprisoned_node_id=?",
            (world["id"], int(to_node["id"])))
        if int((row or {}).get("n", 0) or 0) >= slots:
            raise StrategyError(f"「{to_node['name']}」关押位已满（{slots}）")
        live = await self._fetchone(
            "SELECT id FROM strategy_captive_transfers WHERE world_id=? AND char_id=? "
            "AND status='marching'", (world["id"], int(c["id"])))
        if live:
            raise StrategyError("该俘虏已在押运途中")
        lo, hi = ccfg.get("rescue_ticks") or [4, 8]
        rng = self._rng("transfer", world["id"], c["id"], to_node["id"],
                        world.get("tick_no", 0))
        ticks = max(1, rng.randint(int(lo), int(hi)))
        now = _now()
        # v4.3.0：用 tick_seconds() 而非硬编码 3600——加速/调试世界下
        # 押运会与真实 tick 漂移，计略营救同用 tick_seconds。
        arrive_at = now + ticks * self.tick_seconds()
        tid = await self._execute("""
            INSERT INTO strategy_captive_transfers (world_id, char_id, faction_id,
                from_node, to_node, depart_at, arrive_at, status)
            VALUES (?,?,?,?,?,?,?,'marching')
        """, (world["id"], int(c["id"]), int(p["faction_id"]), from_node,
              int(to_node["id"]), now, arrive_at))
        c["status"] = "transfer"
        c["imprisoned_node_id"] = 0
        c["captor_faction"] = int(p["faction_id"])
        loc = c.get("location") or {}
        loc["transfer_to"] = int(to_node["id"])
        c["location"] = loc
        await self.save_char(c)
        await self._commit()
        await self._audit(user_id, "transfer_captive", {
            "char_id": int(c["id"]), "from_node": from_node,
            "to_node": int(to_node["id"]), "ticks": ticks})
        return {"ok": True, "transfer_id": tid, "arrive_at": arrive_at,
                "msg": f"🔒 {c['name']} 已押运前往「{to_node['name']}」"
                       f"（约 {ticks} tick 抵达，途中可被劫囚）"}

    async def _step_captive_transfers(self, world: dict) -> int:
        """tick：推进押运队列；抵达入新关押地，目标易主则自动释放。"""
        rows = await self._fetchall(
            "SELECT * FROM strategy_captive_transfers WHERE world_id=? "
            "AND status='marching'", (world["id"],))
        if not rows:
            return 0
        ccfg = self.main_cfg().get("captive") or {}
        slots = max(1, int(ccfg.get("jail_slots_per_node", 5)))
        now = _now()
        moved = 0
        for row in rows:
            c = await self.get_char(int(row["char_id"]))
            if not c or c.get("status") != "transfer":
                await self._execute(
                    "UPDATE strategy_captive_transfers SET status='void' WHERE id=?",
                    (int(row["id"]),))
                continue
            if now < int(row["arrive_at"] or 0):
                continue
            fid = int(row["faction_id"])
            node = await self.get_node(world["id"], int(row["to_node"]))
            if not node or int(node["owner_faction"]) != fid:
                # 目的地中途易主 → 押送队溃散，俘虏自动释放
                await self._execute(
                    "UPDATE strategy_captive_transfers SET status='released' "
                    "WHERE id=?", (int(row["id"]),))
                await self._return_captive_home(c)
                await self._notify(
                    world, [(c.get("location") or {}).get("home_owner", "")],
                    "captive_transfer",
                    {"char_id": int(c["id"]),
                     "text": f"{c['name']} 的押送队因「{node['name'] if node else '目的地'}"
                             f"」易主而溃散，已自行归乡"})
                moved += 1
                continue
            cnt = await self._fetchone(
                "SELECT COUNT(*) AS n FROM strategy_characters WHERE world_id=? "
                "AND status='captive' AND imprisoned_node_id=?",
                (world["id"], int(node["id"])))
            if int((cnt or {}).get("n", 0) or 0) >= slots:
                # 新关押位已满：就地释放（避免突破关押上限）
                await self._execute(
                    "UPDATE strategy_captive_transfers SET status='released' "
                    "WHERE id=?", (int(row["id"]),))
                await self._return_captive_home(c)
                moved += 1
                continue
            c["status"] = "captive"
            c["captor_faction"] = fid
            c["imprisoned_node_id"] = int(node["id"])
            loc = c.get("location") or {}
            loc["node_id"] = int(node["id"])
            loc.pop("transfer_to", None)
            c["location"] = loc
            await self.save_char(c)
            await self._execute(
                "UPDATE strategy_captive_transfers SET status='arrived' WHERE id=?",
                (int(row["id"]),))
            await self._notify(
                world, [(c.get("location") or {}).get("home_owner", "")],
                "captive_transfer",
                {"char_id": int(c["id"]),
                 "text": f"{c['name']} 已被押送至「{node['name']}」"})
            moved += 1
        if moved:
            await self._commit()
        return moved

    async def get_transfers_view(self, world_id: int,
                                 faction_id: int = 0) -> list[dict]:
        """押运队列视图（§2.4 get_state.transfers）。"""
        rows = await self._fetchall(
            "SELECT * FROM strategy_captive_transfers WHERE world_id=? "
            "AND status='marching' ORDER BY arrive_at ASC", (world_id,))
        now = _now()
        out = []
        for r in rows:
            if faction_id and int(r["faction_id"]) != int(faction_id):
                continue
            c = await self.get_char(int(r["char_id"]))
            if not c:
                continue
            out.append({"char_id": int(r["char_id"]), "name": c["name"],
                        "from_node": int(r["from_node"]),
                        "to_node": int(r["to_node"]),
                        "arrive_at": int(r["arrive_at"]),
                        "hours_left": max(0, (int(r["arrive_at"]) - now + 3599) // 3600)})
        return out

    async def get_captives_view(self, world_id: int, faction_id: int) -> dict:
        """面板：我方关押的俘虏 + 我方被俘的角色。"""
        ccfg = self.main_cfg().get("captive") or {}
        jail_slots = max(0, int(ccfg.get("jail_slots_per_node", 5)))
        defect_threshold = int(ccfg.get("defect_loyalty", 0))
        holding, held, recruitable = [], [], []
        owner_ids: set[str] = set()
        if int(faction_id or 0) > 0:
            # 「我方被俘」按角色原主判断：玩家转投/势力崩溃重建个人势力后，
            # 角色的 home_faction 快照仍是旧势力，只按快照会让俘虏从面板消失。
            for r in await self._fetchall(
                    "SELECT user_id FROM strategy_players WHERE world_id=? "
                    "AND faction_id=?", (int(world_id), int(faction_id))):
                owner_ids.add(str(r["user_id"]))
        for c in await self.get_chars(world_id=world_id, status="captive"):
            disloyal = int(c.get("loyalty", 0)) <= defect_threshold
            is_ours = int(c.get("captor_faction", 0)) == int(faction_id)
            home = c.get("location") or {}
            home_owner = str(home.get("home_owner") or "")
            item = {"id": c["id"], "name": c["name"], "rarity": c["rarity"],
                    "role": c["role"], "loyalty": c["loyalty"],
                    "node_id": c.get("imprisoned_node_id", 0),
                    "home_owner": home_owner,
                    "disloyal": disloyal,
                    # 我方关押或已离心：可用「招募俘虏」入册（需空闲槽位）
                    "can_recruit": bool(is_ours or disloyal)}
            if is_ours:
                holding.append(item)
            elif int(home.get("home_faction", 0) or 0) == int(faction_id) or \
                    (home_owner and home_owner in owner_ids):
                held.append(item)
            elif disloyal:
                # §7.3 离心俘虏：任意势力均可招降，单独列出避免与“我方被俘”混淆
                recruitable.append(item)
        return {"holding": holding, "held": held,
                "recruitable": recruitable, "jail_slots": jail_slots}
