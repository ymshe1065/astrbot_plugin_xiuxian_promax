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
                            street: bool = False) -> list[str]:
        """败方军团角色被俘判定。kind: city_fall/wipeout/duel。返回被俘者名单。"""
        rng = self._rng("capture", world["id"], node["id"], world.get("tick_no", 0),
                        captor_fid, *[a["id"] for a in loser_armies])
        captured: list[str] = []
        all_chars = await self.get_chars(world_id=world["id"])
        for army in loser_armies:
            for cid in army.get("characters") or []:
                c = await self.get_char(int(cid))
                if not c or c.get("status") == "dead":
                    continue
                if R.capture_check(kind, rng, street_fight=street):
                    await self._capture_char(c, captor_fid, node["id"])
                    captured.append(c["name"])
                else:
                    dead = R.apply_injury(c, 40)
                    if dead:
                        c["status"] = "dead"
                        legacy = R.legacy_on_death(c, all_chars)
                        for av in legacy.get("avengers") or []:
                            await self.save_char(av)
                        if legacy.get("heir"):
                            await self.save_char(legacy["heir"])
                    await self.save_char(c)
        if kind == "city_fall" and not captured and loser_armies:
            # 未触发被俘：主将伤势 +40（兜底叙事一致性）
            for army in loser_armies[:1]:
                cids = army.get("characters") or []
                if cids:
                    c = await self.get_char(int(cids[0]))
                    if c and c.get("status") not in ("dead", "captive"):
                        R.apply_injury(c, 40)
                        await self.save_char(c)
        await self._commit()
        if captured:
            logger.info(f"[strategy] 「{node.get('name')}」被俘 {len(captured)} 人："
                        f"{'、'.join(captured)}（ captor={captor_fid} kind={kind}）")
        return captured

    async def _capture_char(self, c: dict, captor_fid: int, node_id: int):
        loc = c.get("location") or {}
        loc.update({"node_id": int(node_id),
                    "home_faction": int(c.get("faction_id", 0) or 0),
                    "home_owner": c.get("owner_user_id", "")})
        c["location"] = loc
        c["status"] = "captive"
        c["captor_faction"] = int(captor_fid)
        c["imprisoned_node_id"] = int(node_id)
        await self.save_char(c)

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
        if int(c.get("captor_faction", 0)) != int(p["faction_id"]):
            raise StrategyError("该俘虏不在我方关押之下")
        # 说服者：指定或自动取魅力+智谋最高的空闲角色
        persuader = None
        if persuader_char_id:
            persuader = await self._own_char(p, persuader_char_id)
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
        data = world.get("data") or {}
        cd_map = data.setdefault("persuade_cd", {})
        if int(cd_map.get(str(c["id"]), 0)) > _now():
            hours = (int(cd_map[str(c["id"])]) - _now() + 3599) // 3600
            raise StrategyError(f"劝降冷却中（还剩 {hours}h）")
        daily = data.setdefault("persuade_daily", {}).setdefault(
            str(p["faction_id"]), {})
        today = time.strftime("%Y%m%d")
        limit = int(ccfg.get("persuade_daily_limit", 3))
        if int(daily.get(today, 0)) >= limit:
            raise StrategyError(f"今日劝降次数已用完（{limit} 次/日）")
        f = await self.get_faction(p["faction_id"])
        bonus = await self._faction_plot_bonus(f)
        rate = R.persuade_rate(int(c.get("loyalty", 70)), persuader,
                               int((f or {}).get("reputation", 0)),
                               disloyal=int(c.get("loyalty", 70)) <= 0) + bonus
        rate = max(0.05, min(0.95, rate))
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
            return {"ok": True, "result": "fail",
                    "msg": f"{c['name']} 不为所动（成功率 {rate * 100:.0f}%），忠诚微降"}
        # 成功入仕
        home_f = int((c.get("location") or {}).get("home_faction", 0) or 0)
        c["status"] = "idle"
        c["faction_id"] = int(p["faction_id"])
        c["owner_user_id"] = user_id
        c["loyalty"] = 40
        c["captor_faction"] = 0
        c["imprisoned_node_id"] = 0
        c["location"] = {"node_id": int((c.get("location") or {}).get("node_id", 0))}
        await self.save_char(c)
        if home_f and home_f != int(p["faction_id"]):
            of = await self.get_faction(home_f)
            if of:
                await self.add_reputation(
                    of, int(ccfg.get("defect_rep_cost", -3)))
        if c.get("rarity") in ("epic", "legend"):
            await self._award_achievement(user_id, "social_persuade_epic")
        await self._commit()
        return {"ok": True, "result": "success",
                "msg": f"🎉 {c['name']} 归顺！（忠诚 40，原势力声望 -3）"}

    async def _faction_plot_bonus(self, f: Optional[dict]) -> float:
        """势力官职（祭酒）对计略/劝降的加成合计。"""
        if not f:
            return 0.0
        members = await self.get_members(f["id"])
        positions: dict[str, list] = {}
        for m in members:
            pos = m.get("position") or ""
            if pos:
                positions.setdefault(pos, []).append(m["user_id"])
        return float(R.faction_position_bonus(positions).get("plot_bonus", 0.0))

    async def release_captive(self, user_id: str, char_id: int) -> dict:
        """无条件释放：声望 +3、与原势力外交 +5。"""
        world, p = await self._require_player(user_id)
        ccfg = self.main_cfg().get("captive") or {}
        c = await self.get_char(int(char_id))
        if not c or c.get("status") != "captive":
            raise StrategyError("目标不是俘虏")
        if int(c.get("captor_faction", 0)) != int(p["faction_id"]):
            raise StrategyError("该俘虏不在我方关押之下")
        f = await self.get_faction(p["faction_id"])
        if f:
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

    async def _return_captive_home(self, c: dict):
        loc = c.get("location") or {}
        home_f = int(loc.get("home_faction", 0) or 0)
        home_owner = loc.get("home_owner", "") or ""
        home_faction = await self.get_faction(home_f) if home_f else None
        c["status"] = "idle"
        c["captor_faction"] = 0
        c["imprisoned_node_id"] = 0
        if home_faction and int(home_faction.get("alive", 1)):
            c["faction_id"] = home_f
            c["owner_user_id"] = home_owner
        else:
            c["faction_id"] = 0
            c["owner_user_id"] = ""
            c["status"] = "wander"
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
        if int(p.get("coin", 0)) < amount:
            raise StrategyError(f"铜钱不足：赎金 {amount}")
        p["coin"] -= amount
        captor_fid = int(r.get("captor_fid", 0))
        cf = await self.get_faction(captor_fid)
        if cf:
            if cf.get("kind") == "npc":
                treasury = cf.get("treasury") or {}
                treasury["coin"] = int(treasury.get("coin", 0)) + amount
                cf["treasury"] = treasury
                await self.save_faction(cf)
            else:
                # 玩家势力：入国库
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
            home_owner = (c.get("location") or {}).get("home_owner", "")
            if home_owner:
                hp = await self.get_player(home_owner, world["id"])
                if hp:
                    hp["merit"] = int(hp.get("merit", 0)) + merit
                    hp["merit_season"] = int(hp.get("merit_season", 0)) + merit
                    await self.save_player(hp)
            # 解放方参战玩家奖励
            rows = await self._fetchall(
                "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
                (world["id"], int(liberator_fid)))
            for row in rows:
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
        decay = int((self.main_cfg().get("captive") or {}).get(
            "loyalty_decay_per_tick", 1))
        if decay <= 0:
            return
        changed = False
        for c in await self.get_chars(world_id=world["id"], status="captive"):
            if int(c.get("loyalty", 0)) > 0:
                c["loyalty"] = max(0, int(c["loyalty"]) - decay)
                await self.save_char(c)
                changed = True
        if changed:
            await self._commit()

    async def get_captives_view(self, world_id: int, faction_id: int) -> dict:
        """面板：我方关押的俘虏 + 我方被俘的角色。"""
        holding, held = [], []
        for c in await self.get_chars(world_id=world_id, status="captive"):
            item = {"id": c["id"], "name": c["name"], "rarity": c["rarity"],
                    "role": c["role"], "loyalty": c["loyalty"],
                    "node_id": c.get("imprisoned_node_id", 0),
                    "home_owner": (c.get("location") or {}).get("home_owner", ""),
                    "disloyal": int(c.get("loyalty", 0)) <= 0}
            if int(c.get("captor_faction", 0)) == int(faction_id):
                holding.append(item)
            elif int((c.get("location") or {}).get("home_faction", 0) or 0) \
                    == int(faction_id):
                held.append(item)
        return {"holding": holding, "held": held}
