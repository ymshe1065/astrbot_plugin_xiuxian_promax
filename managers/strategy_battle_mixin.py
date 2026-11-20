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
        await self._execute(
            "UPDATE strategy_battles SET state=?, round_no=?, deadline_at=?, "
            "session_json=?, result_json=?, report_text=? WHERE id=?",
            (battle.get("state", "deploy"), int(sess.get("round", 0)),
             int(battle.get("deadline_at", 0)),
             json.dumps(sess, ensure_ascii=False, default=str),
             json.dumps(battle.get("result") or {}, ensure_ascii=False),
             battle.get("report_text", "")[:8000], int(battle["id"])))

    def _battle_rng(self, battle: dict) -> random.Random:
        """恢复/新建战役专属 rng（状态存 session['_rng_state']）。"""
        sess = battle.get("session") or {}
        rng = random.Random(str(battle.get("map_seed") or battle["id"]))
        st = sess.get("_rng_state")
        if st and len(st) == 3:
            try:
                rng.setstate((int(st[0]), tuple(int(x) for x in st[1]), float(st[2])))
            except (ValueError, TypeError):
                pass
        return rng

    @staticmethod
    def _save_rng(battle: dict, rng: random.Random):
        st = rng.getstate()
        (battle.get("session") or {})["_rng_state"] = [st[0], list(st[1]), st[2]]

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
        my_siege = [a for a in await self.get_armies(
            world["id"], faction_id=p["faction_id"], node_id=node["id"],
            states=("siege",)) if a["troops"] > 0]
        if not my_siege:
            raise StrategyError("你没有正在围困该据点的军团")
        atk_sims, dfn_sims = [], []
        for a in my_siege:
            s = await self._army_sim(a)
            if s:
                atk_sims.append(s)
        dfn_armies = [a for a in await self.get_armies(
            world["id"], node_id=node["id"], states=("idle",))
            if a["faction_id"] != p["faction_id"] and a["troops"] > 0]
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
        atk_armies = [a for a in await self.get_armies(
            world["id"], node_id=node["id"], states=("siege",)) if a["troops"] > 0]
        if not atk_armies:
            raise StrategyError("围城军团已不存在")
        my_def = [a for a in await self.get_armies(
            world["id"], faction_id=p["faction_id"], node_id=node["id"],
            states=("idle",)) if a["troops"] > 0]
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
            wall_hp=0, initiator=user_id)
        return {"ok": True, "battle_id": battle["id"],
                "msg": f"🛡 已从「{node['name']}」出击！"
                       f"{int(battle['deadline_at']) - _now()} 秒内完成部署"}

    async def _create_manual_battle(self, world: dict, node: dict,
                                    atk_sims: list, dfn_sims: list,
                                    atk_armies: list, dfn_armies: list,
                                    mode: str, kind: str, prep_seconds: int,
                                    wall_hp: int, initiator: str) -> dict:
        hex_cfg = (self.main_cfg().get("hex") or {})
        now = _now()
        # 先插行拿 battle_id（map_seed = hash(battle_id)，§7.3）
        bid = await self.insert_battle({
            "world_id": world["id"], "node_id": node["id"],
            "attacker_faction": int(atk_armies[0]["faction_id"]),
            "defender_faction": int(node.get("owner_faction", 0) or
                                    (dfn_armies[0]["faction_id"] if dfn_armies else 0)),
            "kind": kind, "mode": "manual", "state": "deploy",
            "round_no": 0, "map_seed": "", "armies_snapshot": [],
            "result": {}, "report_text": "", "created_at": now,
        })
        map_seed = f"hex-{bid}-{node['id']}"
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
            "attacker_faction": int(atk_armies[0]["faction_id"]),
            "defender_faction": int(node.get("owner_faction", 0) or 0),
            "kind": kind, "mode": "manual", "state": "deploy",
            "map_seed": map_seed, "session": session,
            "armies_snapshot": [{"side": "atk", **{k: s.get(k) for k in
                                 ("id", "troop_type", "troops", "morale")}}
                                for s in atk_sims] +
                               [{"side": "dfn", **{k: s.get(k) for k in
                                 ("id", "troop_type", "troops", "morale")}}
                                for s in dfn_sims],
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
        units = []
        for u in sess.get("units") or []:
            is_mine = str(u["uid"]) in owners and \
                owners[str(u["uid"])].get("owner") == user_id
            ambush_hidden = any(
                amb.get("uid") == u["uid"] and not amb.get("revealed")
                for amb in (sess.get("ambushes") or []))
            pos = u.get("pos")
            if ambush_hidden and not is_mine:
                pos = None
            units.append({
                "uid": u["uid"], "side": u["side"], "mine": is_mine,
                "army_id": u.get("army_id", 0),
                "leader": ((u.get("leader") or {}).get("name") or
                           ("守军" if u.get("army_id", 0) < 0 else "无将")),
                "leader_id": (u.get("leader") or {}).get("id"),
                "troop_type": u.get("troop_type"), "troops": u.get("troops"),
                "start_troops": u.get("start_troops"), "morale": u.get("morale"),
                "mana": u.get("mana"), "pos": pos,
                "moved": u.get("moved"), "acted": u.get("acted"),
                "routed": u.get("routed"), "defending": u.get("defending"),
                "formation": u.get("formation"),
                "siege_engine": u.get("siege_engine"),
                "statuses": (sess.get("statuses") or {}).get(str(u["uid"]), {}),
                "skills": H.unit_skills(sess, u) if pos else [],
                "delegate": bool((sess.get("delegate") or {}).get(str(u["uid"]))),
            })
        cur = H.current_actor(sess) if sess.get("phase") == "battle" else None
        out = {
            "battle_id": int(battle["id"]), "node_id": int(battle["node_id"]),
            "kind": battle["kind"], "state": battle["state"],
            "phase": sess.get("phase"), "round": sess.get("round", 0),
            "mode": sess.get("mode"), "size": sess.get("size"),
            "night": sess.get("night"),
            "terrain": sess.get("terrain") if not sess.get("fog") else None,
            "wall_hp": sess.get("wall_hp", 0),
            "deploy_zones": sess.get("deploy_zones"),
            "units": units, "my_uids": my_uids,
            "current_actor": (cur or {}).get("uid"),
            "turn_deadline": sess.get("turn_deadline", 0),
            "deadline_at": int(battle.get("deadline_at", 0)),
            "duel": sess.get("duel"),
            "events": (sess.get("events") or [])[-40:],
            "round_seconds": sess.get("round_seconds", 30),
            "winner": sess.get("winner"),
        }
        if battle["state"] == "done":
            out["result"] = battle.get("result") or {}
            out["report_text"] = battle.get("report_text", "")
        return out

    async def get_battle_replay(self, user_id: str, battle_id: int) -> dict:
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        sess = battle.get("session") or {}
        snap = battle.get("armies_snapshot") or []
        return {
            "battle_id": int(battle["id"]), "seed": battle.get("map_seed", ""),
            "mode": sess.get("mode", battle["kind"]),
            "units_a": [s for s in snap if s.get("side") == "atk"],
            "units_b": [s for s in snap if s.get("side") == "dfn"],
            "commands": sess.get("commands") or [],
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
        for pl in placements or []:
            uid = int(pl.get("uid", 0))
            if owners.get(str(uid), {}).get("owner") != user_id:
                raise StrategyError("只能部署自己的军团")
            ok, reason = H.valid_deploy(sess, uid, int(pl["q"]), int(pl["r"]))
            if not ok:
                raise StrategyError(f"部署位置无效：{reason}")
            H.place_unit(sess, uid, int(pl["q"]), int(pl["r"]))
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

    async def battle_auto_deploy(self, user_id: str, battle_id: int) -> dict:
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        sess = battle.get("session") or {}
        if sess.get("phase") != "deploy":
            raise StrategyError("已过部署阶段")
        rng = self._battle_rng(battle)
        # 只自动部署该玩家未落位的单位
        owners = sess.get("owners") or {}
        mine = [int(uid) for uid, v in owners.items() if v.get("owner") == user_id]
        missing = [u for u in sess["units"]
                   if u["uid"] in mine and not u.get("pos")]
        if not missing:
            raise StrategyError("你的军团均已部署")
        H.auto_deploy(sess, rng)
        self._save_rng(battle, rng)
        await self.update_battle_session(battle)
        await self._commit()
        return {"ok": True, "msg": f"已自动部署 {len(missing)} 支军团"}

    async def battle_start(self, user_id: str, battle_id: int) -> dict:
        world, p, battle = await self._require_battle_player(user_id, battle_id)
        sess = battle.get("session") or {}
        if sess.get("phase") != "deploy":
            raise StrategyError("战役已开始")
        if not H.all_deployed(sess):
            raise StrategyError("尚有军团未部署（可调用自动部署）")
        rng = self._battle_rng(battle)
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
            if owners.get(t_uid, {}).get("owner") != user_id and \
                    not (sess.get("delegate") or {}).get(t_uid):
                raise StrategyError("你不是被挑战方")
            if params.get("accept"):
                out = H.resolve_duel(sess, rng, duel_cfg)
                await self._handle_duel_outcome(world, battle, sess, out)
            else:
                out = H.refuse_duel(sess)
            self._save_rng(battle, rng)
            sess["turn_deadline"] = _now() + int(sess.get("round_seconds", 30))
            battle["deadline_at"] = sess["turn_deadline"]
            await self.update_battle_session(battle)
            await self._commit()
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
            path = [[int(s[0]), int(s[1])] for s in (params.get("path") or [])]
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
        elif action == "attack":
            target = int(params.get("target", 0))
            atk = H.do_attack(sess, uid, target, rng)
            if not atk.get("ok"):
                raise StrategyError(f"攻击失败：{atk.get('reason')}")
            res["attack"] = atk
        elif action == "skill":
            sk = H.do_skill(sess, uid, str(params.get("skill", "")), rng,
                            target_uid=params.get("target_uid"),
                            target_hex=params.get("target_hex"))
            if not sk.get("ok"):
                raise StrategyError(f"技能失败：{sk.get('reason')}")
            res["skill"] = sk
        elif action == "defend":
            res["defend"] = H.do_defend(sess, uid)
        elif action == "retreat":
            res["retreat"] = H.do_retreat(sess, uid, rng)
        elif action == "duel_challenge":
            target = int(params.get("target", 0))
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
        await self.update_battle_session(battle)
        await self._commit()
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

    async def _handle_duel_outcome(self, world: dict, battle: dict, sess: dict,
                                   out: dict):
        """单挑结果落库：斩落 → 角色死亡；被俘 60% 掷点；羁绊 +1。"""
        if not out.get("ok"):
            return
        if out.get("executed"):
            loser_uid = out.get("loser_uid")
            unit = H.get_unit(sess, int(loser_uid)) if loser_uid else None
            leader = (unit or {}).get("leader") or {}
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
                    killer_uid = out.get("winner_uid")
                    ku = H.get_unit(sess, int(killer_uid)) if killer_uid else None
                    kowner = ((sess.get("owners") or {}).get(
                        str(killer_uid), {}) or {}).get("owner")
                    if kowner and (ku or {}).get("leader", {}).get("rarity") == "legend":
                        await self._award_achievement(kowner, "legend_slay_legend")
        elif out.get("loser_injury"):
            loser_uid = out.get("loser_uid")
            unit = H.get_unit(sess, int(loser_uid)) if loser_uid else None
            leader = (unit or {}).get("leader") or {}
            if leader.get("id"):
                c = await self.get_char(int(leader["id"]))
                if c:
                    injury = int(out["loser_injury"]) + \
                        (20 if out.get("extra_injury") else 0)
                    R.apply_injury(c, injury)
                    leader["injury"] = c["injury"]
                    await self.save_char(c)
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
        await self._commit()

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

    async def _advance_manual_battle(self, world: dict, battle: dict):
        sess = battle.get("session") or {}
        now = _now()
        node = await self.get_node(world["id"], int(battle["node_id"]))
        if not node:
            battle["state"] = "done"
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
            rng = self._battle_rng(battle)
            tech_bonus = await self._tech_wisdom_bonus(world, battle)
            guard = 0
            # 超时单位由 AI 代行，直至轮到未超时的手操单位或终局
            while sess.get("phase") == "battle" and guard < 500:
                guard += 1
                if battle.get("_finished"):
                    break
                cur = H.current_actor(sess)
                if not cur:
                    if H.round_complete(sess):
                        H.begin_round(sess, rng)
                        winner = H.check_end(sess)
                        if winner:
                            sess["winner"] = winner
                            battle["_finished"] = H.finish(sess)
                            break
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
            out[fid] = int(R.tech_effects((f or {}).get("tech") or {}).get(
                "ai_wisdom_bonus", 0))
        return out

    # ------------------------------------------------------------------
    # 终局：写回军团/守军 + 占领 + 掠夺 + 被俘
    # ------------------------------------------------------------------
    async def _finalize_manual_battle(self, world: dict, battle: dict, node: dict):
        sess = battle.get("session") or {}
        result = battle.get("_finished") or sess.get("result") or {}
        report = H.render_hex_report(result, title=f"{node['name']}·手操会战")
        battle["state"] = "done"
        battle["result"] = result
        battle["report_text"] = report
        battle["deadline_at"] = 0
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
        atk_sim = [await self._army_sim(a) for a in atk_armies]
        dfn_sim = [await self._army_sim(a) for a in dfn_armies]
        gar = self._garrison_sim(node)
        if gar:
            dfn_sim.append(gar)
        # 恢复军团状态标记（battle → 原状态由善后逻辑接管）
        for a in atk_armies + dfn_armies:
            if a.get("state") == "battle":
                a["state"] = "idle"
        await self._apply_battle_aftermath(
            world, node, atk_armies, dfn_armies, result,
            [s for s in atk_sim if s], [s for s in dfn_sim if s])
        winner = result.get("winner")
        atk_fid = int(battle.get("attacker_faction", 0))
        kind = battle.get("kind", "field")
        texts = [report]
        # 手操获胜奖励：战功 ×1.5（补 0.5）+ 掠夺 +10%
        if winner == "attacker":
            for a in atk_armies:
                if a.get("owner_user_id") and a.get("troops", 0) > 0:
                    pp = await self.get_player(a["owner_user_id"], world["id"])
                    if pp:
                        extra = int(result.get("dfn_losses", 0) * 0.05 * 0.5) + \
                            int((self.main_cfg().get("battle") or {}).get(
                                "win_merit_base", 100) * 0.5)
                        pp["merit"] += extra
                        pp["merit_season"] += extra
                        await self.save_player(pp)
                    await self._award_achievement(a["owner_user_id"],
                                                  "conquest_first_manual")
        if kind == "siege" and winner == "attacker":
            # 城破：占领 + 掠夺库存 10% + 被俘判定 + 解放关押俘虏
            await self._capture_node(world, node, atk_fid)
            node = await self.get_node(world["id"], node["id"]) or node
            loot = await self._loot_node(world, node, atk_fid,
                                         bonus=float((self.main_cfg().get(
                                             "siege") or {}).get(
                                             "manual_win_loot_bonus", 0.1)))
            if loot:
                texts.append(f"💰 攻方掠得铜钱 {loot.get('coin', 0)} / "
                             f"粮草 {loot.get('grain', 0)}")
            losers = [a for a in dfn_armies if a.get("_destroyed") or a.get("_routed")]
            captured = []
            if losers:
                captured = await self.roll_captures(
                    world, node, losers, "city_fall", atk_fid, street=False)
            # 单挑中被擒的将领
            for pc in sess.get("_pending_captures") or []:
                c = await self.get_char(pc["char_id"])
                if c and c.get("status") != "dead":
                    await self._capture_char(c, pc["captor_fid"], node["id"])
                    if c["name"] not in captured:
                        captured.append(c["name"])
            freed = await self.liberate_captives_at_node(world, node, atk_fid)
            if captured:
                texts.append(f"⛓ 守将被擒：{'、'.join(captured)}")
            if freed:
                texts.append(f"🕊 关押的俘虏获释：{'、'.join(freed)}")
            texts.append(f"🏴「{node['name']}」城破易主！")
        elif kind == "siege":
            # 守方击退介入：攻方残部回到围城状态
            for a in atk_armies:
                fresh = await self.get_army(a["id"])
                if fresh and fresh.get("troops", 0) > 0 and \
                        fresh.get("state") == "idle" and not fresh.get("_routed"):
                    fresh["state"] = "siege"
                    await self.save_army(fresh)
            texts.append(f"🛡「{node['name']}」守住了这轮猛攻")
        else:
            # 出击（field）：守方胜 → 围城瓦解
            if winner == "defender":
                ss = node.get("siege_state") or {}
                if ss:
                    node["siege_state"] = {}
                    await self.save_node(node)
                texts.append(f"🎉 出击大捷！「{node['name']}」之围遂解")
            else:
                texts.append(f"⚠️ 出击失利，「{node['name']}」围城继续")
        for pc in ([] if kind == "siege" else sess.get("_pending_captures") or []):
            c = await self.get_char(pc["char_id"])
            if c and c.get("status") != "captive" and c.get("status") != "dead":
                await self._capture_char(c, pc["captor_fid"], node["id"])
                texts.append(f"⛓ 斗法被擒：{c['name']}")
        sess.pop("_pending_captures", None)
        battle["report_text"] = "\n".join(texts)[:8000]
        await self._execute(
            "UPDATE strategy_battles SET state='done', round_no=?, result_json=?, "
            "report_text=?, deadline_at=0, session_json=? WHERE id=?",
            (int(result.get("rounds", 0)),
             json.dumps({k: v for k, v in result.items()}, ensure_ascii=False,
                        default=str)[:200000],
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
        pct = float(scfg.get("loot_stock_pct", 0.3)) + float(bonus)
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
        data = world.get("data") or {}
        arena = data.setdefault("duel_arena", {})
        cid = f"duel{_now()}_{user_id[-4:]}"
        arena[cid] = {"challenger": user_id, "char_id": int(c["id"]),
                      "char_name": c["name"], "stake": stake, "state": "open",
                      "created_at": _now(), "expire_at": _now() + 3600}
        # 过期清理
        for k in [k for k, v in arena.items()
                  if v.get("state", "open") == "open" and
                  _now() > int(v.get("expire_at", 0))]:
            old = arena.pop(k)
            op = await self.get_player(old["challenger"], world["id"])
            if op:
                op["coin"] = int(op.get("coin", 0)) + int(old.get("stake", 0))
                await self.save_player(op)
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
        if not challenger_char:
            raise StrategyError("擂主角色已不存在")
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
                "rounds": result.get("rounds"), "draw": bool(result.get("draw"))}

    async def duel_arena_list(self, user_id: str) -> list[dict]:
        world, p = await self._require_player(user_id)
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
