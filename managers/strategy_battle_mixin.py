# -*- coding: utf-8 -*-
"""「仙域争霸」P2 hex 手操战役 Mixin（设计方案 §7.3/§7.8/§15）。

覆盖：围城介入（intervene）/ 守方出击（sortie）→ strategy_battles
（mode='manual'，state='deploy'→'battle'→'done'，session_json 存
core.strategy_hex 会话）；部署/开局/指令/托管/超时 AI 接管；
终局复用 _apply_battle_aftermath + _capture_node + roll_captures；
和平斗法赌注局；seed+指令流回放。

随机数：session["_rng_state"] 持久化 random.Random 状态，
保证直播操作与 replay() 重建的判定序列一致。
"""
from __future__ import annotations

import json
import logging
import random
import secrets
import time
from typing import Any, Optional

from ..core import strategy_hex as H
from ..core import strategy_rules as R
from .strategy_errors import StrategyError

logger = logging.getLogger("astrbot")


def _now() -> int:
    return int(time.time())


class StrategyBattleMixin:
    """P2 hex 手操战役（§7.3）。"""

    # ------------------------------------------------------------------
    # 战役行读写
    # ------------------------------------------------------------------
    async def get_battle(self, battle_id: int) -> Optional[dict]:
        row = await self._fetchone(
            "SELECT * FROM strategy_battles WHERE id=?", (int(battle_id),))
        return self._decode("strategy_battles", row)

    async def update_battle_session(self, battle: dict):
        sess = battle.get("session") or {}
        done = str(battle.get("state") or "") == "done"
        await self._execute(
            "UPDATE strategy_battles SET state=?, round_no=?, deadline_at=?, "
            "session_json=?, result_json=?, report_text=? WHERE id=?",
            (battle.get("state", "deploy"), int(sess.get("round", 0)),
             0 if done else int(battle.get("deadline_at", 0)),
             json.dumps(sess, ensure_ascii=False, default=str),
             json.dumps(self._clip_battle_result(battle.get("result") or {}),
                        ensure_ascii=False, default=str),
             battle.get("report_text", "")[:8000], int(battle["id"])))

    def _battle_rng(self, battle: dict) -> random.Random:
        """恢复/新建战役专属 rng（状态存 session['_rng_state']）。"""
        sess = battle.get("session") or {}
        rng = random.Random(str(battle.get("map_seed") or battle["id"]))
        st = sess.get("_rng_state")
        if st and len(st) == 3:
            try:
                # 第三元素 gauss_next 常为 None（未用过 gauss 时），不可 float()
                rng.setstate((int(st[0]), tuple(int(x) for x in st[1]),
                              None if st[2] is None else float(st[2])))
            except (ValueError, TypeError) as e:
                logger.warning(f"[strategy] 战役#{battle.get('id')} rng 状态恢复"
                               f"失败（随机流将从种子重来）: {e}")
        return rng

    @staticmethod
    def _save_rng(battle: dict, rng: random.Random):
        st = rng.getstate()
        sess = battle.get("session")
        if not isinstance(sess, dict):
            # B1 加固：session 缺失/为 None 时建字典挂回 battle；
            # 不能用 `or {}`（空字典 falsy 会把状态写进临时对象丢失）
            sess = {}
            battle["session"] = sess
        sess["_rng_state"] = [st[0], list(st[1]), st[2]]

    async def _require_battle_player(self, user_id: str, battle_id: int) -> tuple:
        world, p = await self._require_player(user_id)
        battle = await self.get_battle(battle_id)
        if not battle:
            raise StrategyError("战役不存在")
        if int(battle["world_id"]) != int(world["id"]):
            raise StrategyError("战役不属于当前世界")
        sess = battle.get("session") or {}
        owners = sess.get("owners") or {}
        involved = any(v.get("owner") == user_id for v in owners.values())
        if not involved and int(p["faction_id"]) not in (
                int(battle.get("attacker_faction", 0)),
                int(battle.get("defender_faction", 0))):
            raise StrategyError("你与此战役无关")
        return world, p, battle

    @staticmethod
    def _owns_battle_unit(battle: dict, user_id: str) -> bool:
        """该玩家是否在战役中拥有军团（旁观同势力玩家不算参战方）。"""
        sess = battle.get("session") or {}
        owners = sess.get("owners") or {}
        return any(v.get("owner") == user_id for v in owners.values())

    async def _require_battle_participant(self, user_id: str,
                                          battle_id: int) -> tuple:
        """开战 / 布阵等参战操作：必须是发起人或拥有参战军团。"""
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        sess = battle.get("session") or {}
        initiator = str(sess.get("initiator") or "")
        if initiator != user_id and not self._owns_battle_unit(battle, user_id):
            raise StrategyError("只有参战方可执行该操作")
        return world, p, battle

    # ------------------------------------------------------------------
    # 发起：围城介入 / 守方出击
    # ------------------------------------------------------------------
    async def _active_manual_battle(self, world_id: int, node_id: int) -> Optional[dict]:
        row = await self._fetchone(
            "SELECT * FROM strategy_battles WHERE world_id=? AND node_id=? "
            "AND mode='manual' AND state IN ('deploy','battle') LIMIT 1",
            (world_id, int(node_id)))
        return self._decode("strategy_battles", row) if row else None

    async def intervene_siege(self, user_id: str, node_id: int) -> dict:
        """攻方手操介入进行中的围城（§7.2：接管自动结算）。"""
        world, p = await self._require_player(user_id)
        node = await self.get_node(world["id"], int(node_id))
        if not node:
            raise StrategyError("据点不存在")
        if await self._active_manual_battle(world["id"], node["id"]):
            raise StrategyError("该据点已有手操战役进行中")
        ss = node.get("siege_state") or {}
        if int(ss.get("attacker_faction") or 0) != int(p["faction_id"]):
            raise StrategyError("当前围城归属不是你方，无法介入")
        if int(node.get("protect_until", 0) or 0) > _now():
            raise StrategyError("该据点处于保护期，无法手操攻城")
        block = await self._siege_relation_block(
            int(p["faction_id"]), int(node.get("owner_faction", 0) or 0))
        if block:
            raise StrategyError(f"与守方处于「{block}」，无法攻城")
        my_siege = [a for a in await self.get_armies(
            world["id"], faction_id=p["faction_id"], node_id=node["id"],
            states=("siege",))
            if a["troops"] > 0 and not self._is_reorganizing(a, world)]
        if not my_siege:
            raise StrategyError("你没有正在围困该据点的军团")
        atk_sims, dfn_sims = [], []
        for a in my_siege:
            s = await self._army_sim(a)
            if s:
                atk_sims.append(s)
        dfn_armies = []
        my_f = await self.get_faction(int(p["faction_id"]))
        my_alliance = int((my_f or {}).get("alliance_id") or 0)
        for a in await self.get_armies(
                world["id"], node_id=node["id"], states=("idle",)):
            if a["faction_id"] == p["faction_id"] or a["troops"] <= 0:
                continue
            # 盟友驻军不视为守方（否则会被拉进战斗并收到 battle_open）
            if my_alliance:
                af = await self.get_faction(int(a["faction_id"]))
                if int((af or {}).get("alliance_id") or 0) == my_alliance:
                    continue
            dfn_armies.append(a)
        for a in dfn_armies:
            s = await self._army_sim(a)
            if s:
                dfn_sims.append(s)
        gar = self._garrison_sim(node)
        if gar:
            dfn_sims.append(gar)
        if not dfn_sims:
            raise StrategyError("守方无兵可战（将按受降流程处理）")
        battle = await self._create_manual_battle(
            world, node, atk_sims, dfn_sims, my_siege, dfn_armies,
            mode="siege", kind="siege",
            prep_seconds=int((self.main_cfg().get("hex") or {}).get(
                "siege_prep_seconds", 90)),
            wall_hp=int(node.get("defense", 0)), initiator=user_id)
        return {"ok": True, "battle_id": battle["id"],
                "msg": f"⚔️ 已介入「{node['name']}」攻城战！"
                       f"{int(battle['deadline_at']) - _now()} 秒内完成部署"}

    async def sortie(self, user_id: str, node_id: int) -> dict:
        """守方出击：被围据点的驻守军团主动野战（§7.2）。"""
        world, p = await self._require_player(user_id)
        node = await self.get_node(world["id"], int(node_id))
        if not node or int(node.get("owner_faction", 0)) != int(p["faction_id"]):
            raise StrategyError("只能从我方据点出击")
        if await self._active_manual_battle(world["id"], node["id"]):
            raise StrategyError("该据点已有手操战役进行中")
        ss = node.get("siege_state") or {}
        if not ss:
            raise StrategyError("据点未被围困，无需出击")
        block = await self._siege_relation_block(
            int(ss.get("attacker_faction") or 0),
            int(node.get("owner_faction", 0) or 0))
        if block:
            raise StrategyError(f"与围城方处于「{block}」，无需出击")
        # 只拉「当前围城归属势力」的军团：此前把所有势力的 siege 军团合并，
        # 围城易手后旧势力残军会被拉进战斗，攻方胜时还会按列表第一支误判
        # 占城/战利品归属（§7.2 围城唯一攻方）。
        atk_fid = int(ss.get("attacker_faction") or 0)
        atk_armies = [a for a in await self.get_armies(
            world["id"], node_id=node["id"], states=("siege",))
            if a["troops"] > 0
            and (not atk_fid or int(a["faction_id"]) == atk_fid)]
        if not atk_armies:
            raise StrategyError("围城军团已不存在")
        my_def = [a for a in await self.get_armies(
            world["id"], faction_id=p["faction_id"], node_id=node["id"],
            states=("idle",))
            if a["troops"] > 0 and not self._is_reorganizing(a, world)]
        if not my_def:
            raise StrategyError("据点内没有可出击的驻守军团")
        atk_sims, dfn_sims = [], []
        for a in atk_armies:
            s = await self._army_sim(a)
            if s:
                atk_sims.append(s)
        for a in my_def:
            s = await self._army_sim(a)
            if s:
                dfn_sims.append(s)
        gar = self._garrison_sim(node)
        if gar:
            dfn_sims.append(gar)
        battle = await self._create_manual_battle(
            world, node, atk_sims, dfn_sims, atk_armies, my_def,
            mode="field", kind="field",
            prep_seconds=int((self.main_cfg().get("hex") or {}).get(
                "field_prep_seconds", 300)),
            wall_hp=0, initiator=user_id, attacker_faction=atk_fid)
        return {"ok": True, "battle_id": battle["id"],
                "msg": f"🛡 已从「{node['name']}」出击！"
                       f"{int(battle['deadline_at']) - _now()} 秒内完成部署"}

    async def _create_manual_battle(self, world: dict, node: dict,
                                    atk_sims: list, dfn_sims: list,
                                    atk_armies: list, dfn_armies: list,
                                    mode: str, kind: str, prep_seconds: int,
                                    wall_hp: int, initiator: str,
                                    attacker_faction: int = 0) -> dict:
        hex_cfg = dict(self.main_cfg().get("hex") or {})
        # §7.6 battle 子配置（夜袭/包围/溃逃等）传入 hex 引擎
        battle_cfg = dict(self.main_cfg().get("battle") or {})
        hex_cfg["battle"] = battle_cfg
        for k, v in battle_cfg.items():
            hex_cfg.setdefault(k, v)
        now = _now()
        # 攻方势力由调用方指定（出击时取 siege_state.attacker_faction），
        # 避免多势力围城时用「列表第一支」误判归属
        atk_fid = int(attacker_faction or
                      (atk_armies[0]["faction_id"] if atk_armies else 0))
        dfn_fid = int(node.get("owner_faction", 0) or
                      (dfn_armies[0]["faction_id"] if dfn_armies else 0))
        # 先插行拿 battle_id（map_seed 含随机盐，§7.3）
        bid = await self.insert_battle({
            "world_id": world["id"], "node_id": node["id"],
            "attacker_faction": atk_fid,
            "defender_faction": dfn_fid,
            "kind": kind, "mode": "manual", "state": "deploy",
            "round_no": 0, "map_seed": "", "armies_snapshot": [],
            "result": {}, "report_text": "", "created_at": now,
        })
        # 服务端私有随机盐：battle_id/node_id 公开可推，若直接作为种子，
        # 玩家可离线复现整条随机流预知先手与伤害浮动（§7.3 公平性）。
        map_seed = f"hex-{bid}-{node['id']}-{secrets.token_hex(8)}"
        mi = H.generate_map(map_seed, mode, hex_cfg)
        session = H.new_session(mi, mode, map_seed,
                                night=R.is_night(time.localtime().tm_hour),
                                hex_cfg=hex_cfg)
        owners: dict[str, dict] = {}
        army_by_id = {int(a["id"]): a for a in list(atk_armies) + list(dfn_armies)}
        for sim in atk_sims:
            u = H.add_unit(session, sim, "a")
            army = army_by_id.get(int(sim["id"]))
            owners[str(u["uid"])] = {"army_id": int(sim["id"]), "side": "a",
                                     "owner": (army or {}).get("owner_user_id", "")}
        for sim in dfn_sims:
            u = H.add_unit(session, sim, "b")
            army = army_by_id.get(int(sim["id"]))
            owners[str(u["uid"])] = {"army_id": int(sim["id"]), "side": "b",
                                     "owner": (army or {}).get("owner_user_id", "")}
        session["owners"] = owners
        session["wall_hp"] = int(wall_hp)
        session["battle_cfg"] = battle_cfg
        session["node_id"] = int(node["id"])
        session["delegate"] = {}       # uid(str) → bool
        session["turn_deadline"] = 0
        session["initiator"] = initiator
        # 参战军团转入 battle 态（脱离围城/驻守管线）
        for a in list(atk_armies) + list(dfn_armies):
            a["_prev_state"] = a.get("state", "idle")
            a["state"] = "battle"
            await self.save_army(a)
        battle = {
            "id": bid, "world_id": world["id"], "node_id": node["id"],
            "attacker_faction": atk_fid,
            "defender_faction": dfn_fid,
            "kind": kind, "mode": "manual", "state": "deploy",
            "map_seed": map_seed, "session": session,
            # 快照存完整 army_sim（含 leader/veteran/faction_id/siege_engine），
            # 保证 replay() 用 snapshot 重建的单位与实时 add_unit 完全一致
            "armies_snapshot": [{"side": "atk", **s} for s in atk_sims] +
                               [{"side": "dfn", **s} for s in dfn_sims],
            "deadline_at": now + int(prep_seconds),
            "result": {}, "report_text": "", "created_at": now,
        }
        await self._execute(
            "UPDATE strategy_battles SET map_seed=?, armies_snapshot_json=?, "
            "deadline_at=? WHERE id=?",
            (map_seed, json.dumps(battle["armies_snapshot"], ensure_ascii=False),
             battle["deadline_at"], bid))
        await self.update_battle_session(battle)
        await self._commit()
        logger.info(f"[strategy] 手操战役#{bid} 建立：{node['name']} {mode} "
                    f"（{len(atk_sims)}v{len(dfn_sims)}）")
        # 手操窗口通知（§12.5 battle_open，低档即推）：所有有人军团的拥有者
        humans = [v.get("owner") for v in owners.values() if v.get("owner")]
        await self._notify(world, humans, "battle_open",
                           {"battle_id": bid, "node_id": int(node["id"]),
                            "node_name": node["name"], "mode": mode,
                            "deadline_at": battle["deadline_at"]})
        return battle

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    async def battle_state(self, user_id: str, battle_id: int) -> dict:
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        sess = battle.get("session") or {}
        owners = sess.get("owners") or {}
        my_uids = [int(uid) for uid, v in owners.items() if v.get("owner") == user_id]
        cur = H.current_actor(sess) if sess.get("phase") == "battle" else None
        cur_uid = int((cur or {}).get("uid", 0) or 0)
        units = []
        cur_hidden_enemy = False
        for u in sess.get("units") or []:
            is_mine = str(u["uid"]) in owners and \
                owners[str(u["uid"])].get("owner") == user_id
            ambush_hidden = any(
                amb.get("uid") == u["uid"] and not amb.get("revealed")
                for amb in (sess.get("ambushes") or []))
            # 未触发的敌方伏兵整支不下发：否则即使隐藏坐标，也能从
            # 单位数量/兵力/状态推断出林中有伏兵（§15 设伏保密）。
            # 轮到该伏兵行动时同样不下发（其主人 is_mine 仍可见，前端
            # 不会丢失行动单位）；仅对对手隐藏，并把 current_actor 一并遮蔽。
            if ambush_hidden and not is_mine:
                if int(u["uid"]) == cur_uid:
                    cur_hidden_enemy = True
                continue
            units.append({
                "uid": u["uid"], "side": u["side"], "mine": is_mine,
                "army_id": u.get("army_id", 0),
                "leader": ("兽群" if u.get("temp") else
                           ((u.get("leader") or {}).get("name") or
                            ("守军" if u.get("army_id", 0) < 0 else "无将"))),
                "leader_id": (u.get("leader") or {}).get("id"),
                "troop_type": u.get("troop_type"), "troops": u.get("troops"),
                "start_troops": u.get("start_troops"), "morale": u.get("morale"),
                # 灵力/技能属于对手不可见信息：只下发自己的军团
                "mana": u.get("mana") if is_mine else None, "pos": u.get("pos"),
                "moved": u.get("moved"), "acted": u.get("acted"),
                "routed": u.get("routed"), "defending": u.get("defending"),
                "formation": u.get("formation"),
                "siege_engine": u.get("siege_engine"),
                "temp": bool(u.get("temp")),
                # 剑修「突破 ZOC」每场 1 次；前端据此显示突破移动按钮，
                # 避免后端已支持但玩家无从触发。
                "can_zoc_break": bool(
                    is_mine and not u.get("zoc_break_used")
                    and ((u.get("leader") or {}).get("profession") == "sword")
                    and int(u.get("troops", 0) or 0) > 0
                    and not u.get("routed")),
                "statuses": (sess.get("statuses") or {}).get(str(u["uid"]), {}),
                "skills": (H.unit_skills(sess, u) if u.get("pos") else [])
                          if is_mine else [],
                "delegate": bool((sess.get("delegate") or {}).get(str(u["uid"]))),
            })
        # §12.6 观战模式：非参战同势力玩家只读且**延迟 1 回合**（防报点）。
        spectator = not my_uids
        snap_round = 0
        wall_hp = sess.get("wall_hp", 0)
        if spectator:
            units, snap_round, snap_wall = self._spectator_units(sess)
            cur = None
            # 战斗阶段城防也延迟 1 回合；部署阶段尚无快照，保留实时值。
            if sess.get("phase") == "battle":
                wall_hp = snap_wall
        out = {
            "battle_id": int(battle["id"]), "node_id": int(battle["node_id"]),
            "kind": battle["kind"], "state": battle["state"],
            "phase": sess.get("phase"), "round": sess.get("round", 0),
            "mode": sess.get("mode"), "size": sess.get("size"),
            "night": sess.get("night"),
            "terrain": sess.get("terrain"),
            "temp_terrain": {} if spectator else (sess.get("temp_terrain") or {}),
            "wall_hp": wall_hp,
            "deploy_zones": sess.get("deploy_zones"),
            "units": units, "my_uids": my_uids,
            "spectator": spectator, "spectator_round": snap_round,
            "current_actor": None if cur_hidden_enemy else (cur or {}).get("uid"),
            "turn_deadline": 0 if spectator else sess.get("turn_deadline", 0),
            # 战斗阶段观战者不得看到当前行动方的实时剩余时间：部署阶段
            # 的 deadline_at 是公开的布阵截止时间，可保留。
            "deadline_at": (0 if spectator and sess.get("phase") == "battle"
                            else int(battle.get("deadline_at", 0))),
            "duel": None if spectator else sess.get("duel"),
            "events": [e for e in (sess.get("events") or [])
                       if not spectator or int(e.get("round", 0) or 0) <= snap_round][-80:],
            "round_seconds": sess.get("round_seconds", 30),
            "winner": sess.get("winner"),
        }
        if battle["state"] == "done":
            out["result"] = battle.get("result") or {}
            out["report_text"] = battle.get("report_text", "")
        return out

    @staticmethod
    def _spectator_units(sess: dict) -> tuple[list[dict], int, int]:
        """观战视图：取最近一条「上一回合」快照（§12.6 延迟 1 回合）。

        返回 ``(units, snap_round, wall_hp)``；城防与单位同口径延迟，
        避免观战者通过实时城防推断本回合破城槌进度。
        """
        log = sess.get("spectator_log") or []
        if not log:
            return [], 0, 0
        snap = log[-1]
        snap_round = int(snap.get("round", 0) or 0)
        snap_wall = int(snap.get("wall_hp", 0) or 0)
        units = []
        # 兜底：旧快照可能已写入未触发伏兵，按当前 revealed 状态过滤
        hidden = H.hidden_ambush_uids(sess)
        for u in snap.get("units") or []:
            try:
                if int(u.get("uid", 0)) in hidden:
                    continue
            except (TypeError, ValueError):
                continue
            d = dict(u)
            d.update({
                "mine": False, "mana": None,
                # 观战者不掌握行动/托管细节
                "moved": True, "acted": True, "defending": False,
                "statuses": {}, "skills": [], "delegate": False,
                "leader_id": None,
            })
            units.append(d)
        return units, snap_round, snap_wall

    async def get_battle_replay(self, user_id: str, battle_id: int) -> dict:
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        if battle.get("state") != "done":
            # 赛中 replay 会泄露 map_seed（battle_state 特意隐藏）、未触发
            # 伏兵坐标与全部指令流 → 可离线预知后续所有掷骰，仅限终局后开放
            raise StrategyError("战役结束后才能获取回放")
        sess = battle.get("session") or {}
        snap = battle.get("armies_snapshot") or []
        return {
            "battle_id": int(battle["id"]), "seed": battle.get("map_seed", ""),
            "mode": sess.get("mode", battle["kind"]),
            "units_a": [s for s in snap if s.get("side") == "atk"],
            "units_b": [s for s in snap if s.get("side") == "dfn"],
            "commands": sess.get("commands") or [],
            "events": sess.get("events") or [],
            # 关键事件（被 events 尾部窗口截断的前中期高光）
            "key_events": sess.get("key_events") or [],
            "wall_hp": sess.get("wall_hp", 0),
            "result": battle.get("result") or {},
            "report_text": battle.get("report_text", ""),
        }

    # ------------------------------------------------------------------
    # 部署阶段
    # ------------------------------------------------------------------
    async def battle_deploy(self, user_id: str, battle_id: int,
                            placements: list) -> dict:
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        sess = battle.get("session") or {}
        if sess.get("phase") != "deploy":
            raise StrategyError("已过部署阶段")
        owners = sess.get("owners") or {}
        placed = 0
        if not isinstance(placements, (list, tuple)):
            raise StrategyError("部署参数无效（需为数组）")
        for pl in placements:
            if not isinstance(pl, dict) or "q" not in pl or "r" not in pl:
                raise StrategyError("部署参数无效（需含 uid/q/r）")
            try:
                uid, pq, pr = int(pl.get("uid", 0)), int(pl["q"]), int(pl["r"])
            except (TypeError, ValueError):
                raise StrategyError("部署参数无效（uid/q/r 须为整数）")
            if owners.get(str(uid), {}).get("owner") != user_id:
                raise StrategyError("只能部署自己的军团")
            ok, reason = H.valid_deploy(sess, uid, pq, pr)
            if not ok:
                raise StrategyError(f"部署位置无效：{reason}")
            H.place_unit(sess, uid, pq, pr)
            placed += 1
        await self.update_battle_session(battle)
        await self._commit()
        return {"ok": True, "placed": placed, "msg": f"已部署 {placed} 支军团"}

    async def battle_set_ambush(self, user_id: str, battle_id: int, uid: int,
                                q: int, r: int) -> dict:
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        sess = battle.get("session") or {}
        if sess.get("phase") != "deploy":
            raise StrategyError("设伏仅限部署阶段")
        owners = sess.get("owners") or {}
        if owners.get(str(int(uid)), {}).get("owner") != user_id:
            raise StrategyError("只能为自己的军团设伏")
        ok, reason = H.set_ambush(sess, int(uid), int(q), int(r))
        if not ok:
            raise StrategyError(f"设伏失败：{reason}")
        await self.update_battle_session(battle)
        await self._commit()
        return {"ok": True, "msg": f"伏兵已埋设于 ({q},{r})"}

    async def battle_set_formation(self, user_id: str, battle_id: int,
                                   uid: int, formation: str) -> dict:
        """部署阶段选择阵型（锋矢攻 +10% / 鹤翼 / 方圆防 +15% / 雁行远程 +10%）。"""
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        sess = battle.get("session") or {}
        if sess.get("phase") != "deploy":
            raise StrategyError("阵型只能在部署阶段调整")
        owners = sess.get("owners") or {}
        if owners.get(str(int(uid)), {}).get("owner") != user_id:
            raise StrategyError("只能调整自己的军团阵型")
        formation = str(formation or "")
        if formation not in H.FORMATIONS:
            raise StrategyError("未知阵型")
        unit = H.get_unit(sess, int(uid))
        if not unit:
            raise StrategyError("军团不存在")
        H.set_formation(sess, int(uid), formation)
        await self.update_battle_session(battle)
        await self._commit()
        name = H.FORMATION_NAMES.get(formation, formation or "无阵型")
        return {"ok": True, "msg": f"「{H.leader_name(unit)}」已切换为{name}"}

    async def battle_auto_deploy(self, user_id: str, battle_id: int) -> dict:
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        sess = battle.get("session") or {}
        if sess.get("phase") != "deploy":
            raise StrategyError("已过部署阶段")
        rng = self._battle_rng(battle)
        # 只自动部署该玩家未落位的单位（不越权代摆敌方/盟友军团）
        owners = sess.get("owners") or {}
        mine = [int(uid) for uid, v in owners.items() if v.get("owner") == user_id]
        missing = [u for u in sess["units"]
                   if u["uid"] in mine and not u.get("pos")]
        if not missing:
            raise StrategyError("你的军团均已部署")
        H.auto_deploy(sess, rng, uids={int(u["uid"]) for u in missing})
        self._save_rng(battle, rng)
        await self.update_battle_session(battle)
        await self._commit()
        return {"ok": True, "msg": f"已自动部署 {len(missing)} 支军团"}

    async def battle_start(self, user_id: str, battle_id: int) -> dict:
        world, p, battle = await self._require_battle_participant(user_id, battle_id)
        sess = battle.get("session") or {}
        if sess.get("phase") != "deploy":
            raise StrategyError("战役已开始")
        rng = self._battle_rng(battle)
        # NPC 守军等无主单位没有手操 owner，开战时由系统代为部署
        owners = sess.get("owners") or {}
        npc_missing = [u for u in sess.get("units") or [] if not u.get("pos")
                       and not (owners.get(str(u["uid"])) or {}).get("owner")]
        if npc_missing:
            H.auto_deploy(sess, rng, uids={int(u["uid"]) for u in npc_missing})
        if not H.all_deployed(sess):
            raise StrategyError("尚有军团未部署（可调用自动部署）")
        H.start_battle(sess, rng, wall_hp=int(sess.get("wall_hp", 0)))
        sess["turn_deadline"] = _now() + int(sess.get("round_seconds", 30))
        battle["state"] = "battle"
        battle["deadline_at"] = sess["turn_deadline"]
        self._save_rng(battle, rng)
        await self.update_battle_session(battle)
        await self._commit()
        return {"ok": True, "msg": f"🥁 第 1 回合开始！先手：单位 "
                                   f"{(H.current_actor(sess) or {}).get('uid')}"}

    async def battle_delegate(self, user_id: str, battle_id: int,
                              on: bool = True) -> dict:
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        sess = battle.get("session") or {}
        if battle.get("state") == "done":
            raise StrategyError("战役已结束")
        if not self._owns_battle_unit(battle, user_id):
            raise StrategyError("你在本战役中没有可托管的军团")
        owners = sess.get("owners") or {}
        dele = sess.setdefault("delegate", {})
        n = 0
        for uid, v in owners.items():
            if v.get("owner") == user_id:
                dele[uid] = bool(on)
                n += 1
        await self.update_battle_session(battle)
        await self._commit()
        return {"ok": True,
                "msg": f"已{'开启' if on else '关闭'}托管（{n} 支军团由军师代行）"}

    # ------------------------------------------------------------------
    # 战斗指令
    # ------------------------------------------------------------------
    async def battle_action(self, user_id: str, battle_id: int, action: str,
                            params: Optional[dict] = None) -> dict:
        params = params or {}
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        sess = battle.get("session") or {}
        if battle.get("state") != "battle" or sess.get("phase") != "battle":
            raise StrategyError("战役不在战斗阶段")
        owners = sess.get("owners") or {}
        duel_cfg = self.main_cfg().get("duel") or {}
        rng = self._battle_rng(battle)
        action = str(action)

        # 单挑应战/拒绝（不占用行动权）
        if action == "duel_respond":
            duel = sess.get("duel")
            if not duel or duel.get("state") != "pending":
                raise StrategyError("没有待决的单挑")
            t_uid = str(duel.get("target_uid"))
            # 仅被挑战方本人可应答（托管旁路会让任意相关玩家代押生死斗）；
            # 托管/离线单位由 tick 超时自动拒绝
            if owners.get(t_uid, {}).get("owner") != user_id:
                raise StrategyError("你不是被挑战方")
            deadline = int(duel.get("deadline", 0) or 0)
            if deadline and _now() > deadline:
                raise StrategyError("单挑应答已超时")
            if params.get("accept"):
                out = H.resolve_duel(sess, rng, duel_cfg)
                await self._handle_duel_outcome(world, battle, sess, out)
                # 斩将/擒将会把败方军团置为溃败，可能直接打空一方。必须
                # 当场判终局并落库，否则要等下一次 tick 才结束（期间军团
                # 卡在 battle 态、围城管线继续暂停）。
                winner = H.check_end(sess)
                if winner:
                    sess["winner"] = winner
                    battle["_finished"] = H.finish(sess)
            else:
                out = H.refuse_duel(sess)
            self._save_rng(battle, rng)
            if battle.get("_finished"):
                await self._persist_battle_action(world, battle)
            else:
                await self.update_battle_session(battle)
                await self._commit()
            # 15s 窗口内对象已溃/亡时 resolve_duel 会中止（ok=False）。此时
            # 单挑已清空且挑战次数已消耗，必须明确告知玩家，不能静默成功。
            if params.get("accept") and not out.get("ok"):
                return {"ok": False, "action": "duel_respond",
                        "msg": f"单挑中止：{out.get('reason') or '对象已失去战斗力'}",
                        "out": out}
            return {"ok": True, "action": "duel_respond", "out": out}

        cur = H.current_actor(sess)
        if not cur:
            raise StrategyError("本回合已无待行动单位")
        uid = int(cur["uid"])
        if owners.get(str(uid), {}).get("owner") != user_id:
            raise StrategyError("当前不是你的单位行动")
        if (sess.get("delegate") or {}).get(str(uid)):
            raise StrategyError("该单位已托管（先关闭托管再手操）")

        res: dict[str, Any] = {"ok": True, "action": action}
        if action == "move":
            try:
                raw_path = params.get("path") or []
                if not isinstance(raw_path, (list, tuple)):
                    raise TypeError
                path = [[int(s[0]), int(s[1])] for s in raw_path]
            except (TypeError, ValueError, IndexError, KeyError):
                raise StrategyError("移动路径无效（需为 [[q,r], ...]）")
            if not path:
                raise StrategyError("path 不能为空")
            leader = cur.get("leader") or {}
            can_break = bool(params.get("break")) and \
                leader.get("profession") == "sword"
            mv = H.do_move(sess, uid, path, rng, use_zoc_break=can_break)
            res["move"] = mv
            if not mv.get("ok") and mv.get("end", 0) == 0:
                self._save_rng(battle, rng)
                await self.update_battle_session(battle)
                await self._commit()
                raise StrategyError(f"移动失败：{mv.get('reason')}")
            # §7.3 移动→攻击/技能/防御/撤退：移动不消耗行动权，
            # 同回合可继续攻击或显式 end_turn（与 AI/前端按钮语义一致）
            self._after_actions(battle, sess, rng)
            self._save_rng(battle, rng)
            await self._persist_battle_action(world, battle)
            return self._action_tail(battle, sess, res)
        elif action == "attack":
            if H.is_confused(sess, cur):
                # 惑敌：敌我不分，由引擎按确定性伪随机选目标（与 AI 同规则）
                atk = H.do_confused_attack(sess, uid, rng)
            else:
                # 先解析目标再校验挑衅：此前挑衅分支里直接 int(params["target"])，
                # 客户端传非数字（"abc"/对象/数组）会抛未捕获的 ValueError/
                # TypeError → REST 500「操作失败」，而不是 400 指令错误。
                try:
                    target = int(params.get("target", 0) or 0)
                except (TypeError, ValueError):
                    raise StrategyError("攻击目标无效")
                taunt_by = H.active_taunter(sess, cur)
                if taunt_by:
                    tu = H.get_unit(sess, int(taunt_by))
                    if tu and tu["troops"] > 0 and not tu["routed"] and \
                            target != int(taunt_by):
                        raise StrategyError("已被挑衅：只能攻击挑衅者")
                atk = H.do_attack(sess, uid, target, rng)
            if not atk.get("ok"):
                raise StrategyError(f"攻击失败：{atk.get('reason')}")
            res["attack"] = atk
        elif action == "skill":
            if H.is_confused(sess, cur):
                raise StrategyError("惑敌：本回合无法施放技能")
            try:
                t_uid = params.get("target_uid")
                t_uid = int(t_uid) if t_uid not in (None, "", 0, "0") else None
            except (TypeError, ValueError):
                raise StrategyError("技能目标无效")
            t_hex = params.get("target_hex")
            if t_hex is not None:
                try:
                    if not isinstance(t_hex, (list, tuple)) or len(t_hex) != 2:
                        raise TypeError
                    t_hex = [int(t_hex[0]), int(t_hex[1])]
                except (TypeError, ValueError, IndexError):
                    raise StrategyError("技能坐标无效")
            sk = H.do_skill(sess, uid, str(params.get("skill", "")), rng,
                            target_uid=t_uid, target_hex=t_hex)
            if not sk.get("ok"):
                raise StrategyError(f"技能失败：{sk.get('reason')}")
            res["skill"] = sk
        elif action == "defend":
            res["defend"] = H.do_defend(sess, uid)
        elif action == "retreat":
            res["retreat"] = H.do_retreat(sess, uid, rng)
        elif action == "duel_challenge":
            try:
                target = int(params.get("target", 0) or 0)
            except (TypeError, ValueError):
                raise StrategyError("单挑目标无效")
            ok, reason = H.can_challenge(sess, uid, target, duel_cfg)
            if not ok:
                raise StrategyError(f"无法单挑：{reason}")
            deadline = _now() + int(duel_cfg.get("respond_seconds",
                                                duel_cfg.get("duel_respond_seconds", 15)))
            H.challenge_duel(sess, uid, target, deadline_at=deadline)
            res["duel"] = sess.get("duel")
            self._save_rng(battle, rng)
            await self.update_battle_session(battle)
            await self._commit()
            # 单挑邀请通知（§12.5 duel_invite，低档即推）
            t_owner = (owners.get(str(target)) or {}).get("owner", "")
            await self._notify(world, [t_owner], "duel_invite",
                               {"battle_id": int(battle["id"]),
                                "from_uid": uid, "target_uid": target,
                                "deadline_at": deadline})
            return res
        elif action == "end_turn":
            pass
        else:
            raise StrategyError(f"未知指令：{action}")

        H.end_actor_turn(sess, uid)
        self._after_actions(battle, sess, rng)
        self._save_rng(battle, rng)
        await self._persist_battle_action(world, battle)
        return self._action_tail(battle, sess, res)

    async def _sync_hex_injury_heals(self, sess: dict):
        """把引擎记录的治疗结果写回 strategy_characters（伤势 -N 落库）。"""
        heals = sess.pop("_injury_heals", None)
        if not heals:
            return
        for cid_s, new_val in list(heals.items()):
            try:
                c = await self.get_char(int(cid_s))
            except (TypeError, ValueError):
                continue
            if c and c.get("status") != "dead" and \
                    int(c.get("injury", 0) or 0) > int(new_val):
                c["injury"] = int(new_val)
                await self.save_char(c)

    async def _release_battle_armies(self, world: dict, battle: dict) -> int:
        """兜底释放参战军团：把仍处于 battle 态的军团恢复 idle。

        仅用于无法走正常善后流程的异常终局（据点已不存在、终局落库失败），
        否则军团会永远卡在 battle 态，既不围城也不受正常指令驱动。
        """
        sess = battle.get("session") or {}
        released = 0
        for v in (sess.get("owners") or {}).values():
            try:
                aid = int(v.get("army_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if aid <= 0:
                continue
            a = await self.get_army(aid)
            if a and a.get("state") == "battle":
                a["state"] = "idle"
                await self.save_army(a)
                released += 1
        if released:
            await self._commit()
            logger.info(f"[strategy] 手操战役#{battle.get('id')} 释放 "
                        f"{released} 支滞留军团（异常终局）")
        return released

    async def _persist_battle_action(self, world: dict, battle: dict):
        """手操指令后落库；若本次行动已触发终局 → 立即走终局落库流程。

        （修复：此前击杀终局仅置内存 _finished 标记，无人调用
        _finalize_manual_battle，战役永久卡在 state=battle/phase=done，
        参战军团卡 battle 态、据点管线阻塞。）
        """
        await self._sync_hex_injury_heals(battle.get("session") or {})
        if battle.get("_finished"):
            node = await self.get_node(world["id"], int(battle["node_id"]))
            if node:
                await self._finalize_manual_battle(world, battle, node)
                return
            battle["state"] = "done"
            battle["result"] = battle.get("_finished") or {}
            # 据点已消失：无法占领/掠夺，但必须把军团从 battle 态释放
            await self._release_battle_armies(world, battle)
        await self.update_battle_session(battle)
        await self._commit()

    @staticmethod
    def _action_tail(battle: dict, sess: dict, res: dict) -> dict:
        res["current_actor"] = (H.current_actor(sess) or {}).get("uid")
        res["round"] = sess.get("round")
        res["events"] = (sess.get("events") or [])[-6:]
        if battle.get("state") == "done":
            res["result"] = battle.get("result") or {}
            res["report_text"] = battle.get("report_text", "")
        return res

    def _after_actions(self, battle: dict, sess: dict, rng: random.Random):
        """行动后推进：回合完结 → 新回合；终局 → finish。"""
        winner = H.check_end(sess)
        if winner:
            sess["winner"] = winner
            result = H.finish(sess)
            battle["_finished"] = result
            return
        if H.round_complete(sess):
            H.begin_round(sess, rng)
            winner = H.check_end(sess)
            if winner:
                sess["winner"] = winner
                battle["_finished"] = H.finish(sess)
                return
        sess["turn_deadline"] = _now() + int(sess.get("round_seconds", 30))
        battle["deadline_at"] = sess["turn_deadline"]

    async def _bump_duel_bond(self, cid_a: int, cid_b: int):
        """单挑平局：双方羁绊 +1 级（无羁绊则建立同道，最高 5 级）。"""
        a = await self.get_char(int(cid_a))
        b = await self.get_char(int(cid_b))
        if not a or not b or a.get("status") == "dead" or b.get("status") == "dead":
            return
        # 宿怨是「仇敌」关系，平局惺惺相惜不应把它升级成更高宿怨
        old_ab = (a.get("bonds") or {}).get(str(b["id"])) or {}
        old_bb = (b.get("bonds") or {}).get(str(a["id"])) or {}
        if old_ab.get("type") == "suyuan" or old_bb.get("type") == "suyuan":
            return
        ab = a.setdefault("bonds", {}).setdefault(
            str(b["id"]), {"type": "tongdao", "level": 0, "battles": 0})
        bb = b.setdefault("bonds", {}).setdefault(
            str(a["id"]), {"type": "tongdao", "level": 0, "battles": 0})
        lv = min(5, max(1, int(ab.get("level", 0)) + 1))
        ab["level"] = lv
        bb["level"] = lv
        ab.setdefault("type", "tongdao")
        bb.setdefault("type", "tongdao")
        await self.save_char(a)
        await self.save_char(b)

    async def _handle_duel_outcome(self, world: dict, battle: dict, sess: dict,
                                   out: dict):
        """单挑结果落库：斩落/伤重 → 角色库；被俘掷点；平局羁绊 +1。

        伤重/斩落/被俘后同步把战场单位置为溃败——主将失去战斗力即军团
        溃败（§7.6），否则被斩的军团还能继续移动、攻击、施法。
        """
        if not out.get("ok"):
            return
        owners = sess.get("owners") or {}
        loser_uid = out.get("loser_uid")
        lunit = H.get_unit(sess, int(loser_uid)) if loser_uid else None
        leader = (lunit or {}).get("leader") or {}

        def _rout_unit():
            if lunit:
                lunit["troops"] = 0
                lunit["routed"] = True
                lunit["morale"] = 0
                routed[0] = True

        routed = [False]

        if out.get("executed"):
            if leader.get("id"):
                c = await self.get_char(int(leader["id"]))
                if c:
                    c["status"] = "dead"
                    c["injury"] = max(int(c.get("injury", 0)), 100)
                    all_chars = await self.get_chars(world_id=world["id"])
                    legacy = R.legacy_on_death(c, all_chars)
                    for av in (legacy.get("avengers") or []) + \
                            ([legacy["heir"]] if legacy.get("heir") else []):
                        await self.save_char(av)
                    await self.save_char(c)
            _rout_unit()
            # 成就归击杀方，但判定对象是「被斩者」是否传世名将
            killer_uid = out.get("winner_uid")
            kowner = (owners.get(str(killer_uid), {}) or {}).get("owner")
            if kowner and leader.get("rarity") == "legend":
                await self._award_achievement(kowner, "legend_slay_legend")
        elif out.get("loser_injury"):
            if leader.get("id"):
                c = await self.get_char(int(leader["id"]))
                if c:
                    injury = int(out["loser_injury"]) + \
                        (20 if out.get("extra_injury") else 0)
                    if R.apply_injury(c, injury):
                        # 伤势达到陨落阈值：与 roll_captures/战后善后同口径
                        c["status"] = "dead"
                        _rout_unit()
                    leader["injury"] = int(c["injury"])
                    await self.save_char(c)

        # 平局羁绊 +1（engine 返回 bond_up，此前无人落库）
        bond_up = out.get("bond_up") or []
        if len(bond_up) >= 2 and bond_up[0] and bond_up[1]:
            await self._bump_duel_bond(int(bond_up[0]), int(bond_up[1]))

        hint = out.get("capture_hint") or {}
        if hint.get("loser_leader_id"):
            rng = random.Random(f"duelcap-{battle['id']}-{hint['loser_leader_id']}")
            pct = float((self.main_cfg().get("captive") or {}).get(
                "duel_capture_pct", 60)) / 100
            if rng.random() < pct:
                c = await self.get_char(int(hint["loser_leader_id"]))
                if c and c.get("status") != "dead":
                    # 存入 session（随 session_json 持久化，终局时落库）
                    sess.setdefault("_pending_captures", []).append(
                        {"char_id": int(c["id"]),
                         "captor_fid": int(hint.get("captor_faction", 0))})
                    # 主将被擒 → 军团溃败（不再行动/施法）
                    _rout_unit()
        # 把 manager 侧战果回填进刚记录的 duel_resolve 指令，保证 replay()
        # 用同一指令流重建时，败将所部的溃败状态与实时一致（§7.3）。
        if routed[0] and sess.get("commands"):
            last = sess["commands"][-1]
            if last.get("type") == "duel_resolve":
                last["apply"] = {"rout_uid": int(loser_uid or 0), "rout": True}
        # 不在此处 commit：由调用方（duel_respond 分支）随 session 落库同一事务提交

    # ------------------------------------------------------------------
    # tick：超时托管 / 部署超时 / 终局落库
    # ------------------------------------------------------------------
    async def _step_manual_battles(self, world: dict):
        rows = await self._fetchall(
            "SELECT * FROM strategy_battles WHERE world_id=? AND mode='manual' "
            "AND state IN ('deploy','battle')", (world["id"],))
        for row in rows:
            battle = self._decode("strategy_battles", row)
            try:
                await self._advance_manual_battle(world, battle)
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[strategy] 手操战役#{battle['id']} 推进异常: {e}")
        await self._commit()

    async def poll_manual_battles(self):
        """秒级轮询推进手操战役（独立于 15 分钟世界 tick）。

        round_seconds=30 的超时托管、部署超时、单挑超时均为秒级节奏，
        仅挂世界 tick 会拖延至多 tick_minutes 分钟。main.py 每 10s 调用。
        锁序恒为 _action_lock → _world_lock；忙则本轮跳过（不排队积压）。
        """
        if not self.enabled() or self._action_lock.locked() or \
                self._world_lock.locked():
            return
        async with self._action_lock:
            if self._world_lock.locked():
                return
            async with self._world_lock:
                world = await self.get_world()
                if world:
                    await self._step_manual_battles(world)

    async def _advance_manual_battle(self, world: dict, battle: dict):
        sess = battle.get("session") or {}
        now = _now()
        node = await self.get_node(world["id"], int(battle["node_id"]))
        if not node:
            battle["state"] = "done"
            # 据点被其他战役/事件销毁：释放参战军团，避免永久 battle 态
            await self._release_battle_armies(world, battle)
            await self.update_battle_session(battle)
            return
        # 单挑超时 → 自动拒绝
        duel = sess.get("duel")
        if duel and duel.get("state") == "pending" and \
                now > int(duel.get("deadline", 0)):
            H.refuse_duel(sess)
        if battle["state"] == "deploy":
            if now < int(battle.get("deadline_at", 0)):
                return
            # 部署超时：全托管瞬时模拟
            rng = self._battle_rng(battle)
            tech_bonus = await self._tech_wisdom_bonus(world, battle)
            result = H.ai_auto_battle(sess, rng, tech_bonus)
            self._save_rng(battle, rng)
            battle["_finished"] = result
        elif battle["state"] == "battle":
            if sess.get("phase") == "done":
                # 终局续办：此前异常/崩溃导致 phase 已 done 但未走终局落库。
                # 必须给 _finished 一个非空哨兵：空 dict 为假值，会跳过
                # finalize 而把 state=battle/phase=done 永久留在库里。
                battle["_finished"] = sess.get("result") or {
                    "winner": sess.get("winner") or "defender"}
            else:
                rng = self._battle_rng(battle)
                tech_bonus = await self._tech_wisdom_bonus(world, battle)
                guard = 0
                # 兜底上限按「回合数 × 每回合单位数」动态计算：固定 500 在
                # 多军团长战役中可能截断推进，下一 tick 续推又会与回合计时错位。
                guard_max = max(64, int(sess.get("max_rounds", 30)) *
                                (len(sess.get("units") or []) + 4))
                # 超时单位由 AI 代行，直至轮到未超时的手操单位或终局
                while sess.get("phase") == "battle" and guard < guard_max:
                    guard += 1
                    if battle.get("_finished"):
                        break
                    cur = H.current_actor(sess)
                    if not cur:
                        # 先判终局再开新回合：打满 max_rounds 时不得让
                        # 第 N+1 回合的合围/灼烧结算抢先跑一遍。
                        winner = H.check_end(sess)
                        if winner:
                            sess["winner"] = winner
                            battle["_finished"] = H.finish(sess)
                            break
                        H.begin_round(sess, rng)
                        continue
                    uid = int(cur["uid"])
                    owner = (sess.get("owners") or {}).get(str(uid), {}).get("owner")
                    delegated = bool((sess.get("delegate") or {}).get(str(uid)))
                    timed_out = now > int(sess.get("turn_deadline", 0))
                    if owner and not delegated and not timed_out:
                        break  # 等待手操
                    H.ai_decide(sess, uid, rng,
                                tech_bonus.get(int(cur.get("faction_id", 0)), 0))
                    H.end_actor_turn(sess, uid)
                    winner = H.check_end(sess)
                    if winner:
                        sess["winner"] = winner
                        battle["_finished"] = H.finish(sess)
                        break
                    if H.round_complete(sess):
                        H.begin_round(sess, rng)
                        winner = H.check_end(sess)
                        if winner:
                            sess["winner"] = winner
                            battle["_finished"] = H.finish(sess)
                            break
                    # 每次行动权交接都重置回合计时（逐单位超时，§7.3）
                    sess["turn_deadline"] = _now() + int(sess.get("round_seconds", 30))
                self._save_rng(battle, rng)
                battle["deadline_at"] = sess.get("turn_deadline", 0)
        if battle.get("_finished"):
            await self._finalize_manual_battle(world, battle, node)
        else:
            await self.update_battle_session(battle)

    async def _tech_wisdom_bonus(self, world: dict, battle: dict) -> dict:
        out = {}
        for fid in (int(battle.get("attacker_faction", 0)),
                    int(battle.get("defender_faction", 0))):
            if fid <= 0 or fid in out:
                continue
            f = await self.get_faction(fid)
            tech = int(R.tech_effects((f or {}).get("tech") or {}).get(
                "ai_wisdom_bonus", 0))
            # 祭酒（角色官职）托管智谋 +5，与科技叠加
            pos = await self._faction_position_bonus(f) if f else {}
            out[fid] = tech + int((pos or {}).get("ai_wisdom", 0) or 0)
        return out

    # ------------------------------------------------------------------
    # 终局：写回军团/守军 + 占领 + 掠夺 + 被俘
    # ------------------------------------------------------------------
    async def _finalize_manual_battle(self, world: dict, battle: dict, node: dict):
        """终局落库外层兜底：异常时释放军团并留下可诊断战报。

        inner 开头已用原子闸门防重复结算；一旦善后中途抛异常，DB 行已经是
        done，后续 tick 不会再选中该战役。若不在这里兜底，参战军团会永久
        卡在 battle 态（既不围城也不受指令驱动），且战报丢失。
        """
        try:
            await self._finalize_manual_battle_inner(world, battle, node)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                f"[strategy] 手操战役#{battle.get('id')} 终局结算异常: {e}")
            await self._release_battle_armies(world, battle)
            note = ("⚠️ 本场结算过程出现异常，部分伤亡/战利品可能未落库；"
                    "参战军团已释放，请联系管理员核查。")
            battle["state"] = "done"
            battle["report_text"] = (
                str(battle.get("report_text") or "") + "\n" + note)[:8000]
            await self._execute(
                "UPDATE strategy_battles SET state='done', deadline_at=0, "
                "report_text=? WHERE id=?",
                (battle["report_text"], int(battle["id"])))
            await self._commit()

    async def _finalize_manual_battle_inner(self, world: dict, battle: dict, node: dict):
        sess = battle.get("session") or {}
        # 幂等闸门：只有把状态从 deploy/battle 原子推进到 done 的那一次调用
        # 才执行善后。中途异常/进程重启后的续办直接返回，避免战功、掠夺、
        # 俘虏判定被重复结算（_apply_battle_aftermath 等内部各自 commit）。
        claimed = await self._exec_count(
            "UPDATE strategy_battles SET state='done', deadline_at=0 "
            "WHERE id=? AND state IN ('deploy','battle')",
            (int(battle["id"]),))
        if not claimed:
            battle["state"] = "done"
            return
        await self._commit()
        # 治疗术等引擎侧伤势减免先写回角色表（须早于善后伤势结算）
        await self._sync_hex_injury_heals(sess)
        result = battle.get("_finished") or sess.get("result") or {}
        # 单挑被擒（60%）只登记在 _pending_captures，角色此刻仍 idle；
        # 终局 roll_captures 若再对同一角色掷点会概率叠加（城陷可达 76%），
        # 关押位满时还会重复驱逐。这里先收集待处理角色，掷点时跳过。
        pending_ids: set[int] = set()
        for pc in sess.get("_pending_captures") or []:
            try:
                pending_ids.add(int(pc.get("char_id", 0) or 0))
            except (TypeError, ValueError, AttributeError):
                continue
        # 恢复路径（phase 已 done 但 result 丢失/缺 attackers/defenders）：
        # _apply_battle_aftermath 找不到对应 row 会直接 continue，不会落库，
        # 参战军团将永久卡在 battle 态。先显式释放，避免僵尸军团。
        if not result.get("attackers") and not result.get("defenders"):
            await self._release_battle_armies(world, battle)
        try:
            result["highlights"] = self._battle_highlights(result, sess)
        except Exception:  # noqa: BLE001 - 高光标记失败不影响结算
            result["highlights"] = []
        report = H.render_hex_report(result, title=f"{node['name']}·手操会战")
        battle["state"] = "done"
        battle["result"] = result
        battle["report_text"] = report
        battle["deadline_at"] = 0
        # 破城槌墙伤写回据点城防（siege 模式）：wall_hp 初值 = 开战时城防，
        # 手操期间自动围城管线暂停（参战军为 battle 态），城防不会被别处改动
        if sess.get("mode") == "siege":
            wall_now = max(0, int(sess.get("wall_hp", 0) or 0))
            if wall_now < int(node.get("defense", 0) or 0):
                node["defense"] = wall_now
                await self.save_node(node)
        owners = sess.get("owners") or {}
        atk_armies, dfn_armies = [], []
        for uid_s, v in owners.items():
            aid = int(v.get("army_id", 0))
            if aid <= 0:
                continue
            a = await self.get_army(aid)
            if not a:
                continue
            (atk_armies if v.get("side") == "a" else dfn_armies).append(a)
        # 恢复军团状态标记（battle → 原状态由善后逻辑接管）
        for a in atk_armies + dfn_armies:
            if a.get("state") == "battle":
                a["state"] = "idle"
        kind = battle.get("kind", "field")
        # 手操围城介入失败时，幸存攻方残部留在城下由下方 siege 分支恢复围城；
        # 若此处按自动野战口径撤离，恢复分支将永远找不到驻扎军团。
        await self._apply_battle_aftermath(
            world, node, atk_armies, dfn_armies, result,
            hold_atkers=(kind == "siege"))
        winner = result.get("winner")
        atk_fid = int(battle.get("attacker_faction", 0))
        texts = [report]
        if result.get("highlights"):
            texts.insert(0, "  ".join(result["highlights"]))
        # 手操获胜奖励：战功 ×manual_win_merit_mult（默认 1.5，即补 0.5）
        # 多军团时按攻击方军团数均摊，避免"每支军团各领一次"导致成倍膨胀
        mult = float((self.main_cfg().get("siege") or {}).get(
            "manual_win_merit_mult", 1.5))
        base_bcfg = self.main_cfg().get("battle") or {}
        n_atk = max(1, len(atk_armies))
        per_army_base = int(
            int(result.get("dfn_losses", 0)) / n_atk *
            float(base_bcfg.get("merit_per_kill", 0.05))) + \
            int(base_bcfg.get("win_merit_base", 100))
        extra = int(per_army_base * max(0.0, mult - 1.0))
        if winner == "attacker":
            for a in atk_armies:
                if a.get("owner_user_id") and a.get("troops", 0) > 0:
                    pp = await self.get_player(a["owner_user_id"], world["id"])
                    if pp:
                        pp["merit"] += extra
                        pp["merit_season"] += extra
                        await self.save_player(pp)
                    await self._award_achievement(a["owner_user_id"],
                                                  "conquest_first_manual")
        if kind == "siege" and winner == "attacker":
            # 城破：占领（内含关押俘虏解放）+ 掠夺库存 10% + 被俘判定
            freed = await self._capture_node(world, node, atk_fid) or []
            capture_note = node.get("_capture_note")
            node = await self.get_node(world["id"], node["id"]) or node
            if capture_note:
                texts.append(str(capture_note))
            # 掠夺加成：手操获胜 + 夜战魔修劫掠（§7.6，finish 已算好
            # loot_bonus）；此前只取 manual_win_loot_bonus，夜袭 +30% 失效。
            loot_bonus = float((self.main_cfg().get("siege") or {}).get(
                "manual_win_loot_bonus", 0.1)) + \
                float(result.get("loot_bonus") or 0.0)
            loot = await self._loot_node(world, node, atk_fid,
                                         bonus=max(0.0, loot_bonus))
            if loot:
                texts.append(f"💰 攻方掠得铜钱 {loot.get('coin', 0)} / "
                             f"粮草 {loot.get('grain', 0)}")
            losers = [a for a in dfn_armies if a.get("_destroyed") or a.get("_routed")]
            captured = []
            if losers:
                captured = await self.roll_captures(
                    world, node, losers, "city_fall", atk_fid, street=False,
                    skip_ids=pending_ids)
            # 单挑中被擒的将领统一在分支后处理（见下方 _pending_captures）
            if captured:
                texts.append(f"⛓ 守将被擒：{'、'.join(captured)}")
            if freed:
                texts.append(f"🕊 关押的俘虏获释：{'、'.join(freed)}")
            texts.append(f"🏴「{node['name']}」城破易主！")
        elif kind == "siege":
            # 守方击退介入：攻方主力虽败退，幸存军团仍留在城下继续围城
            # （否则围城线断裂、siege_state 悬挂且城防自修被永久阻塞）
            restored = 0
            for a in atk_armies:
                if a.get("_destroyed") or a.get("_routed"):
                    continue
                fresh = await self.get_army(a["id"])
                # 只有仍驻扎在本节点的残部才能恢复围城（行军途中的军团
                # 强行置 siege 会造成 node_id/path 与围城态互相矛盾）
                if fresh and int(fresh.get("troops", 0) or 0) > 0 and \
                        int(fresh.get("node_id") or 0) == int(node["id"]) and \
                        not (fresh.get("path") or []):
                    await self._begin_siege(world, node, fresh)
                    restored += 1
            # 全军覆没/溃逃：清除悬挂围城态
            await self._clear_orphan_siege(world, node)
            # 守方胜：被歼灭/溃逃的攻方军团同样进入被俘判定（与自动围城
            # 反击 wipeout 口径一致，否则守方永远擒不到攻方将领）
            lost_atk = [a for a in atk_armies
                        if a.get("_destroyed") or a.get("_routed")]
            if lost_atk:
                dfn_fid = int(battle.get("defender_faction", 0)
                              or node.get("owner_faction", 0) or 0)
                if dfn_fid > 0:
                    got = await self.roll_captures(
                        world, node, lost_atk, "wipeout", dfn_fid,
                        skip_ids=pending_ids)
                    if got:
                        texts.append(f"⛓ 攻将被擒：{'、'.join(got)}")
            texts.append(f"🛡「{node['name']}」守住了这轮猛攻"
                         + (f"（{restored} 支残部继续围城）" if restored else ""))
        else:
            # 出击（field）：守方胜 → 围城瓦解
            if winner == "defender":
                ss = node.get("siege_state") or {}
                if ss:
                    node["siege_state"] = {}
                    await self.save_node(node)
                lost_atk = [a for a in atk_armies
                            if a.get("_destroyed") or a.get("_routed")]
                if lost_atk:
                    dfn_fid = int(battle.get("defender_faction", 0)
                                  or node.get("owner_faction", 0) or 0)
                    if dfn_fid > 0:
                        got = await self.roll_captures(
                            world, node, lost_atk, "wipeout", dfn_fid,
                            skip_ids=pending_ids)
                        if got:
                            texts.append(f"⛓ 攻将被擒：{'、'.join(got)}")
                texts.append(f"🎉 出击大捷！「{node['name']}」之围遂解")
            else:
                # 攻方胜：幸存围城军恢复 siege 态并补齐 siege_state，
                # 否则军团滞留 idle、围城计时悬挂——无法继续围城且节点自修被阻
                for a in atk_armies:
                    if a.get("_destroyed") or a.get("_routed"):
                        continue
                    fresh = await self.get_army(a["id"])
                    if fresh and fresh.get("troops", 0) > 0 and \
                            int(fresh.get("node_id") or 0) == int(node["id"]) and \
                            not (fresh.get("path") or []):
                        await self._begin_siege(world, node, fresh)
                # 兜底：若幸存军团均因 node_id/path 不匹配无法重建围城，
                # 清除悬挂 siege_state（该函数自身幂等，有围城军时不动）
                await self._clear_orphan_siege(world, node)
                texts.append(f"⚠️ 出击失利，「{node['name']}」围城继续")
        # 单挑被擒统一落库（field 的攻方胜与 siege 守方胜都需要）
        for pc in sess.get("_pending_captures") or []:
            c = await self.get_char(pc["char_id"])
            if c and c.get("status") not in ("captive", "dead"):
                captor_fid = int(pc.get("captor_fid") or 0)
                # 关押节点必须属于擒获方：field 出击攻方胜时战场节点仍归
                # 守方，若直接关在本节点会把俘虏塞进敌方自己的城。
                jail_id = int(node["id"])
                if captor_fid > 0 and \
                        int(node.get("owner_faction", 0) or 0) != captor_fid:
                    home = await self._nearest_own_node(
                        world, {"faction_id": captor_fid}, int(node["id"]))
                    if home:
                        jail_id = int(home)
                got = await self._capture_char(c, captor_fid, jail_id)
                texts.append(f"⛓ 斗法被擒：{c['name']}" if got
                             else f"⛓ {c['name']} 斗法落败，但敌营已满，被就地驱逐")
        sess.pop("_pending_captures", None)
        battle["report_text"] = "\n".join(texts)[:8000]
        await self._execute(
            "UPDATE strategy_battles SET state='done', round_no=?, result_json=?, "
            "report_text=?, deadline_at=0, session_json=? WHERE id=?",
            (int(result.get("rounds", 0)),
             # 安全截断：按字符硬切可能产生非法 JSON，_decode 会回退 {}，
             # 导致整场战报/结果丢失。复用已有的摘要+首尾片段方案。
             json.dumps(self._clip_battle_result(result), ensure_ascii=False,
                        default=str),
             battle["report_text"],
             json.dumps(sess, ensure_ascii=False, default=str),
             int(battle["id"])))
        await self._commit()
        logger.info(f"[strategy] 手操战役#{battle['id']} 结束：{winner} 胜 "
                    f"（{node['name']} {kind}）")

    async def _loot_node(self, world: dict, node: dict, atk_fid: int,
                         bonus: float = 0.0) -> dict:
        """攻城掠夺：守方库存 30% + 手操加成 10%，入攻方（国库/玩家）。"""
        scfg = self.main_cfg().get("siege") or {}
        # 比例必须封顶：夜间劫掠/手操加成配置过大时 pct 可能 >1，
        # 库存会被 clamp 到 0，但玩家仍按 pct 拿走超过库存的资源。
        pct = min(1.0, max(0.0, float(scfg.get("loot_stock_pct", 0.3)) +
                             float(bonus)))
        stock = node.get("stock") or {}
        loot = {k: int(int(v or 0) * pct) for k, v in stock.items()}
        if loot:
            node["stock"] = {k: max(0, int(v or 0) - loot.get(k, 0))
                             for k, v in stock.items()}
            await self.save_node(node)
        total = sum(loot.values())
        if total <= 0:
            # 无库存记录时按据点类型给象征性掠夺（玩家资源池）
            base = {"city": 3000, "pass": 1200, "mine": 1500, "grain": 800,
                    "market": 1000, "branch": 800, "ruin": 400}.get(node["type"], 400)
            loot = {"coin": int(base * pct)}
        f = await self.get_faction(atk_fid)
        if f:
            if f.get("kind") == "npc":
                treasury = f.get("treasury") or {}
                for k, v in loot.items():
                    treasury[k] = int(treasury.get(k, 0)) + int(v)
                f["treasury"] = treasury
                await self.save_faction(f)
            else:
                rows = await self._fetchall(
                    "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
                    (world["id"], atk_fid))
                n = max(1, len(rows))
                for row in rows:
                    pp = await self.get_player(row["user_id"], world["id"])
                    if pp:
                        for k, v in loot.items():
                            if k in ("coin", "grain", "iron"):
                                pp[k] = int(pp.get(k, 0)) + int(v) // n
                        await self.save_player(pp)
        await self._commit()
        return loot

    # ------------------------------------------------------------------
    # 和平斗法赌注局（坊市擂台，§7.8）
    # ------------------------------------------------------------------
    async def _sweep_duel_arena(self, world: dict) -> int:
        """清理过期擂台（退还赌注）与 1h 前的 done 记录。

        此前只在 open 时顺带清理，玩家只 list/accept 时过期赌注会一直悬挂。
        """
        data = world.get("data") or {}
        arena = data.get("duel_arena") or {}
        now = _now()
        removed = 0
        for k in [k for k, v in arena.items()
                  if (v.get("state", "open") == "open"
                      and now > int(v.get("expire_at", 0)))
                  or (v.get("state") == "done"
                      and now - int(v.get("created_at", 0)) > 3600)]:
            old = arena.pop(k)
            removed += 1
            if old.get("state", "open") != "open":
                continue
            op = await self.get_player(old["challenger"], world["id"])
            if op:
                op["coin"] = int(op.get("coin", 0)) + int(old.get("stake", 0))
                await self.save_player(op)
        if removed:
            data["duel_arena"] = arena
            world["data"] = data
            await self.save_world(world)
        return removed

    async def duel_arena_open(self, user_id: str, char_id: int, stake: int) -> dict:
        world, p = await self._require_player(user_id)
        dcfg = self.main_cfg().get("duel") or {}
        c = await self._own_char(p, char_id)
        if c.get("status") != "idle":
            raise StrategyError(f"{c['name']} 并非空闲")
        if not R.can_challenge_duel(c):
            raise StrategyError("需健康且武勇 ≥75 的武将方可摆擂")
        stake = int(stake)
        lo = int(dcfg.get("arena_stake_min", 100))
        hi = int(dcfg.get("arena_stake_max", 100000))
        if not (lo <= stake <= hi):
            raise StrategyError(f"赌注须在 {lo}~{hi} 铜钱之间")
        if p["coin"] < stake:
            raise StrategyError(f"铜钱不足：赌注 {stake}")
        p["coin"] -= stake
        await self._sweep_duel_arena(world)
        data = world.get("data") or {}
        arena = data.setdefault("duel_arena", {})
        now = _now()
        # 同一秒内同一玩家可以重复摆擂（rollback 后重试/连点）：cid 必须唯一，
        # 否则新擂会覆盖旧擂，旧擂赌注被吞且无法退还。
        base_cid = f"duel{now}_{user_id[-4:]}"
        cid = base_cid
        seq = 1
        while cid in arena:
            seq += 1
            cid = f"{base_cid}_{seq}"
        arena[cid] = {"challenger": user_id, "char_id": int(c["id"]),
                      "char_name": c["name"], "stake": stake, "state": "open",
                      "created_at": now, "expire_at": now + 3600}
        data["duel_arena"] = arena
        world["data"] = data
        await self.save_world(world)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "duel_id": cid,
                "msg": f"🥁 {c['name']} 摆下擂台（赌注 {stake} 铜钱，1h 内有效）"}

    async def duel_arena_accept(self, user_id: str, duel_id: str,
                                char_id: int) -> dict:
        world, p = await self._require_player(user_id)
        await self._sweep_duel_arena(world)
        data = world.get("data") or {}
        arena = data.get("duel_arena") or {}
        d = arena.get(str(duel_id))
        if not d or (d.get("state") or "open") != "open":
            raise StrategyError("擂台不存在或已结束")
        if _now() > int(d.get("expire_at", 0)):
            raise StrategyError("擂台已过期")
        if d["challenger"] == user_id:
            raise StrategyError("不能应自己的擂")
        stake = int(d.get("stake", 0))
        if p["coin"] < stake:
            raise StrategyError(f"铜钱不足：跟注 {stake}")
        c = await self._own_char(p, char_id)
        if c.get("status") != "idle" or not R.can_challenge_duel(c):
            raise StrategyError("应战需健康且武勇 ≥75 的空闲武将")
        challenger_char = await self.get_char(int(d["char_id"]))
        if not challenger_char or challenger_char.get("status") != "idle" \
                or not R.can_challenge_duel(challenger_char):
            # 擂主已被俘/陨落/出征：擂台失效，立即关闭并退还擂主赌注，
            # 否则应战方会与一个「不存在的武将」结算赌局。
            arena.pop(str(duel_id), None)
            data["duel_arena"] = arena
            world["data"] = data
            cp = await self.get_player(d["challenger"], world["id"])
            if cp:
                cp["coin"] = int(cp.get("coin", 0)) + stake
                await self.save_player(cp)
            await self.save_world(world)
            await self._commit()
            raise StrategyError("擂主已无法应战，擂台已关闭并退还赌注")
        p["coin"] -= stake
        d["state"] = "done"
        d["acceptor"] = user_id
        rng = self._rng("arena", duel_id, user_id, _now() // 60)
        result = R.duel(challenger_char, c, rng)
        pot = stake * 2
        winner_user = ""
        if result.get("draw"):
            p["coin"] += stake
            cp = await self.get_player(d["challenger"], world["id"])
            if cp:
                cp["coin"] = int(cp.get("coin", 0)) + stake
                await self.save_player(cp)
            msg = "⚖️ 斗法战平，各自收回赌注"
        elif int(result["winner"]) == int(challenger_char["id"]):
            cp = await self.get_player(d["challenger"], world["id"])
            if cp:
                cp["coin"] = int(cp.get("coin", 0)) + pot
                await self.save_player(cp)
            msg = f"🏆 {challenger_char['name']} 技高一筹，赢得彩头 {pot} 铜钱"
            winner_user = d["challenger"]
        else:
            p["coin"] += pot
            msg = f"🏆 {c['name']} 技高一筹，赢得彩头 {pot} 铜钱"
            winner_user = user_id
        if winner_user:
            # 仙盟会武事件期间：胜场计数，5 胜夺魁
            if await self._tournament_win(world, winner_user):
                msg += "（仙盟会武夺魁！成就「会武夺冠」达成）"
        arena[str(duel_id)] = d
        data["duel_arena"] = arena
        world["data"] = data
        await self.save_world(world)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": msg,
                "rounds": result.get("rounds"), "draw": bool(result.get("draw")),
                "winner_user": winner_user}

    async def duel_arena_list(self, user_id: str) -> list[dict]:
        world, p = await self._require_player(user_id)
        await self._sweep_duel_arena(world)
        data = world.get("data") or {}
        arena = data.get("duel_arena") or {}
        out = []
        for k, v in arena.items():
            if v.get("state", "open") != "open" or _now() > int(v.get("expire_at", 0)):
                continue
            out.append({"duel_id": k, "challenger": v["challenger"],
                        "char_name": v.get("char_name", ""),
                        "stake": v.get("stake", 0),
                        "expire_at": v.get("expire_at", 0),
                        "mine": v["challenger"] == user_id})
        return out
