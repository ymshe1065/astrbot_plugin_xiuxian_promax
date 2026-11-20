# -*- coding: utf-8 -*-
"""「仙域争霸」P1 势力联机 Mixin（设计方案 §8）。

覆盖：势力创建（会签）/ 申请·邀请·审批 / 退盟 CD / 官职任免 /
国库存取 / 势力科技 / 外交（宣战·停战·结盟·和谈）/ 声望 /
治理（继任·僵尸势力崩溃）/ 主城重建 / 护山盾。

作为 StrategyManager 的 mixin 使用，依赖主类的
cfg/main_cfg/_fetchall/_execute/_commit/get_*/save_* 等方法。
临时态（会签/申请/退盟CD/护山盾CD）统一存 strategy_worlds.data_json。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

from ..core import strategy_rules as R
from .strategy_errors import StrategyError

logger = logging.getLogger("astrbot")


def _now() -> int:
    return int(time.time())


POSITION_CAPS_DEFAULT = {"leader": 1, "deputy": 1, "situ": 1, "taiwei": 1,
                         "jijiu": 2, "zhonglangjiang": 2}
POSITION_CN = {"leader": "主公", "deputy": "副主", "situ": "司徒", "taiwei": "太尉",
               "jijiu": "祭酒", "zhonglangjiang": "中郎将"}
#: 可参与决策（科技/外交/国库）的官职
OFFICER_POSITIONS = ("leader", "deputy", "situ", "taiwei", "jijiu")


class StrategyFactionMixin:
    """P1 势力联机（§8）。"""

    # ------------------------------------------------------------------
    # 成员名册（strategy_members）
    # ------------------------------------------------------------------
    async def get_members(self, faction_id: int) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM strategy_members WHERE faction_id=?", (int(faction_id),))
        return [dict(r) for r in rows]

    async def get_member(self, faction_id: int, user_id: str) -> Optional[dict]:
        row = await self._fetchone(
            "SELECT * FROM strategy_members WHERE faction_id=? AND user_id=?",
            (int(faction_id), user_id))
        return dict(row) if row else None

    async def upsert_member(self, faction_id: int, user_id: str,
                            position: Optional[str] = None,
                            contribution: Optional[int] = None,
                            joined_at: Optional[int] = None):
        m = await self.get_member(faction_id, user_id)
        if m:
            sets, params = [], []
            if position is not None:
                sets.append("position=?")
                params.append(position)
            if contribution is not None:
                sets.append("contribution=?")
                params.append(int(contribution))
            if sets:
                params.extend([int(faction_id), user_id])
                await self._execute(
                    f"UPDATE strategy_members SET {','.join(sets)} "
                    "WHERE faction_id=? AND user_id=?", tuple(params))
            return m
        await self._execute(
            "INSERT INTO strategy_members (faction_id, user_id, position, "
            "contribution, joined_at) VALUES (?,?,?,?,?)",
            (int(faction_id), user_id, position or "",
             int(contribution or 0), int(joined_at or _now())))
        return await self.get_member(faction_id, user_id)

    async def delete_member(self, faction_id: int, user_id: str):
        await self._execute(
            "DELETE FROM strategy_members WHERE faction_id=? AND user_id=?",
            (int(faction_id), user_id))

    async def add_contribution(self, faction_id: int, user_id: str, amount: int):
        m = await self.get_member(faction_id, user_id)
        if m:
            await self.upsert_member(
                faction_id, user_id,
                contribution=int(m.get("contribution", 0)) + int(amount))

    def _position_of(self, members: list[dict], user_id: str) -> str:
        for m in members:
            if m["user_id"] == user_id:
                return m.get("position") or ""
        return ""

    async def _require_position(self, f: dict, user_id: str,
                                allowed: tuple = ("leader",)) -> str:
        members = await self.get_members(f["id"])
        pos = self._position_of(members, user_id)
        if pos not in allowed:
            names = "/".join(POSITION_CN.get(x, x) for x in allowed)
            raise StrategyError(f"仅 {names} 可执行此操作")
        return pos

    # ------------------------------------------------------------------
    # 势力等级 / 成员上限
    # ------------------------------------------------------------------
    def _faction_level(self, node_count: int) -> int:
        thresholds = (self.main_cfg().get("faction") or {}).get(
            "level_nodes") or [0, 2, 4, 6, 9, 12, 15, 19, 23, 28]
        lvl = 1
        for i, t in enumerate(thresholds):
            if node_count >= int(t):
                lvl = i + 1
        return lvl

    def _member_cap(self, f: dict) -> int:
        fcfg = self.main_cfg().get("faction") or {}
        return int(fcfg.get("member_cap_base", 10)) + \
            int(fcfg.get("member_cap_per_level", 3)) * (int(f.get("level", 1)) - 1)

    # ------------------------------------------------------------------
    # 创建势力（会签制，§8.1）
    # ------------------------------------------------------------------
    async def create_faction(self, user_id: str, name: str, color: str = "") -> dict:
        """发起建势力会签：垫付 10000 铜钱，72h 内集齐 3 人（含发起人）。"""
        world, p = await self._require_player(user_id)
        fcfg = self.main_cfg().get("faction") or {}
        name = str(name or "").strip()
        if not (2 <= len(name) <= 12):
            raise StrategyError("势力名须为 2~12 字")
        f = await self.get_faction(p["faction_id"])
        members = await self.get_members(p["faction_id"])
        if f and f.get("kind") != "player" or len(members) > 1:
            raise StrategyError("已在多人势力中，无法另立门户（请先退盟）")
        # 重名检查
        dup = await self._fetchone(
            "SELECT id FROM strategy_factions WHERE world_id=? AND name=? AND alive=1",
            (world["id"], name))
        if dup:
            raise StrategyError("势力名已被占用")
        data = world.get("data") or {}
        pets = data.get("faction_petitions") or {}
        for pet in pets.values():
            if pet.get("creator") == user_id and pet.get("state") == "open":
                raise StrategyError("已有进行中的会签，请先等待或撤销")
        cost = int(fcfg.get("create_cost_coin", 10000))
        if p["coin"] < cost:
            raise StrategyError(f"铜钱不足：发起会签需垫付 {cost}")
        p["coin"] -= cost
        pid = f"pet{_now()}_{user_id[-6:]}"
        pets[pid] = {
            "id": pid, "name": name,
            "color": color or "#%06x" % random.Random(pid).randint(0x333333, 0xFFFFFF),
            "creator": user_id, "created_at": _now(), "state": "open",
            "expire_at": _now() + int(fcfg.get("cosign_expire_hours", 72)) * 3600,
            "cosigners": [], "escrow": cost,
        }
        data["faction_petitions"] = pets
        world["data"] = data
        await self.save_world(world)
        await self.save_player(p)
        await self._commit()
        need = int(fcfg.get("cosign_required", 3))
        return {"ok": True, "petition_id": pid,
                "msg": f"会签已发起：「{name}」还需 {need - 1} 人联署"
                       f"（{int(fcfg.get('cosign_expire_hours', 72))}h 内有效，已垫付 {cost} 铜钱）"}

    async def faction_cosign(self, user_id: str, petition_id: str) -> dict:
        """联署建势力会签；集齐人数即刻立派。"""
        world, p = await self._require_player(user_id)
        fcfg = self.main_cfg().get("faction") or {}
        data = world.get("data") or {}
        pets = data.get("faction_petitions") or {}
        pet = pets.get(str(petition_id))
        if not pet or pet.get("state") != "open":
            raise StrategyError("会签不存在或已结束")
        if _now() > int(pet.get("expire_at", 0)):
            raise StrategyError("会签已过期")
        if pet["creator"] == user_id or user_id in pet.get("cosigners") or []:
            raise StrategyError("已联署过该会签")
        members = await self.get_members(p["faction_id"])
        if len(members) > 1:
            raise StrategyError("已在多人势力中，无法联署")
        need = int(fcfg.get("cosign_required", 3))
        pet.setdefault("cosigners", []).append(user_id)
        if 1 + len(pet["cosigners"]) >= need:
            pet["state"] = "created"
            pets[petition_id] = pet
            data["faction_petitions"] = pets
            world["data"] = data
            await self.save_world(world)
            await self._commit()
            fid = await self._do_create_faction(world, pet)
            fname = pet["name"]
            return {"ok": True, "faction_id": fid,
                    "msg": f"🎉 势力「{fname}」正式建立！（会签 {need} 人达成）"}
        pets[petition_id] = pet
        data["faction_petitions"] = pets
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return {"ok": True,
                "msg": f"联署成功：「{pet['name']}」还差 {need - 1 - len(pet['cosigners'])} 人"}

    async def _do_create_faction(self, world: dict, pet: dict) -> int:
        """会签达成：建势力行 + 迁入发起人与联署人。"""
        fid = await self._execute("""
            INSERT INTO strategy_factions (world_id, name, color, kind, leader_id,
                personality_json, treasury_json, reputation, level, member_cap,
                capital_node_id, faction_state, alive)
            VALUES (?,?,?,'player',?,'{}','{}',0,1,?,0,'normal',1)
        """, (world["id"], pet["name"], pet.get("color") or "#888888",
              pet["creator"],
              int((self.main_cfg().get("faction") or {}).get("member_cap_base", 10))))
        await self._commit()
        new_f = await self.get_faction(fid)
        escrow = int(pet.get("escrow", 0))
        if new_f and escrow > 0:
            treasury = new_f.get("treasury") or {}
            treasury["coin"] = int(treasury.get("coin", 0)) + escrow
            new_f["treasury"] = treasury
            await self.save_faction(new_f)
        for uid in [pet["creator"]] + list(pet.get("cosigners") or []):
            p = await self.get_player(uid, world["id"])
            if p:
                await self._join_faction(world, p, fid,
                                         position="leader" if uid == pet["creator"] else "")
        await self.upsert_member(fid, pet["creator"], position="leader")
        await self._commit()
        logger.info(f"[strategy] 势力#{fid}「{pet['name']}」经会签建立")
        return fid

    # ------------------------------------------------------------------
    # 入盟 / 退盟
    # ------------------------------------------------------------------
    async def _join_faction(self, world: dict, p: dict, fid: int, position: str = ""):
        """把玩家（及其角色/军团）迁入目标势力；旧个人势力若无成员则封存。"""
        old_fid = int(p.get("faction_id", 0))
        if old_fid == fid:
            return
        await self._execute(
            "UPDATE strategy_characters SET faction_id=? WHERE world_id=? AND owner_user_id=?",
            (fid, world["id"], p["user_id"]))
        await self._execute(
            "UPDATE strategy_armies SET faction_id=? WHERE world_id=? AND owner_user_id=?",
            (fid, world["id"], p["user_id"]))
        p["faction_id"] = fid
        await self.save_player(p)
        await self.upsert_member(fid, p["user_id"], position=position,
                                 joined_at=_now())
        if old_fid and old_fid != fid:
            await self.delete_member(old_fid, p["user_id"])
            old = await self.get_faction(old_fid)
            if old and old.get("kind") == "player":
                rest = await self.get_members(old_fid)
                if not rest:
                    old["alive"] = 0
                    old["faction_state"] = "merged"
                    await self.save_faction(old)
        await self._commit()

    async def _ensure_personal_faction(self, world: dict, p: dict,
                                       nickname: str = "") -> int:
        """为玩家（重）建个人势力（P0 语义：散修也持有个人势力壳）。"""
        rng = random.Random(int(hashlib_md5(f"{p['user_id']}|{world['id']}|{_now()}"), 16))
        color = "#%06x" % rng.randint(0x333333, 0xFFFFFF)
        fid = await self._execute("""
            INSERT INTO strategy_factions (world_id, name, color, kind, leader_id,
                personality_json, treasury_json, reputation, level, member_cap,
                capital_node_id, faction_state, alive)
            VALUES (?,?,?,'player',?,'{}','{}',0,1,10,0,'normal',1)
        """, (world["id"], f"{nickname or p['user_id']}的个人势力", color, p["user_id"]))
        await self._commit()
        return fid

    async def faction_apply(self, user_id: str, faction_id: int) -> dict:
        """申请入盟（存入 world.data.join_apps，等待领袖/副主审批）。"""
        world, p = await self._require_player(user_id)
        fid = int(faction_id)
        f = await self.get_faction(fid)
        if not f or not int(f.get("alive", 0)) or f.get("kind") == "npc":
            raise StrategyError("目标势力不存在或不可加入")
        members = await self.get_members(fid)
        if len(members) >= self._member_cap(f):
            raise StrategyError(f"「{f['name']}」成员已满（{self._member_cap(f)}）")
        my_members = await self.get_members(p["faction_id"])
        if len(my_members) > 1:
            raise StrategyError("已在多人势力中，请先退盟")
        data = world.get("data") or {}
        quit_cd = int((data.get("quit_cd") or {}).get(user_id, 0))
        if quit_cd > _now():
            hours = (quit_cd - _now() + 3599) // 3600
            raise StrategyError(f"退盟冷却中（还剩 {hours} 小时）")
        apps = data.setdefault("join_apps", {}).setdefault(str(fid), {})
        if user_id in apps:
            raise StrategyError("已递交过申请，请等待审批")
        apps[user_id] = _now()
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return {"ok": True, "msg": f"已向「{f['name']}」递交入盟申请"}

    async def faction_invite(self, user_id: str, target_user: str) -> dict:
        """领袖/副主邀请玩家入盟。"""
        world, p = await self._require_player(user_id)
        f = await self.get_faction(p["faction_id"])
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader", "deputy"))
        target = await self.get_player(str(target_user), world["id"])
        if not target:
            raise StrategyError("对方尚未入场")
        t_members = await self.get_members(target["faction_id"])
        if len(t_members) > 1:
            raise StrategyError("对方已在多人势力中")
        members = await self.get_members(f["id"])
        if len(members) >= self._member_cap(f):
            raise StrategyError("势力成员已满")
        data = world.get("data") or {}
        invites = data.setdefault("join_invites", {})
        invites[str(target_user)] = {"fid": int(f["id"]), "by": user_id, "at": _now()}
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return {"ok": True, "msg": f"已邀请 {target_user} 加入「{f['name']}」"}

    async def faction_respond_invite(self, user_id: str, accept: bool) -> dict:
        world, p = await self._require_player(user_id)
        data = world.get("data") or {}
        inv = (data.get("join_invites") or {}).get(user_id)
        if not inv:
            raise StrategyError("没有待处理的邀请")
        if not accept:
            data["join_invites"].pop(user_id, None)
            world["data"] = data
            await self.save_world(world)
            await self._commit()
            return {"ok": True, "msg": "已拒绝邀请"}
        f = await self.get_faction(int(inv["fid"]))
        if not f or not int(f.get("alive", 0)):
            raise StrategyError("邀请方势力已不存在")
        members = await self.get_members(f["id"])
        if len(members) >= self._member_cap(f):
            raise StrategyError("对方势力成员已满")
        data["join_invites"].pop(user_id, None)
        world["data"] = data
        await self.save_world(world)
        await self._join_faction(world, p, int(f["id"]))
        await self._commit()
        return {"ok": True, "msg": f"已加入「{f['name']}」"}

    async def faction_approve(self, user_id: str, applicant_user: str) -> dict:
        world, p = await self._require_player(user_id)
        f = await self.get_faction(p["faction_id"])
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader", "deputy"))
        data = world.get("data") or {}
        apps = (data.get("join_apps") or {}).get(str(f["id"])) or {}
        if applicant_user not in apps:
            raise StrategyError("该玩家没有待审批的申请")
        target = await self.get_player(str(applicant_user), world["id"])
        if not target:
            raise StrategyError("申请人不存在")
        members = await self.get_members(f["id"])
        if len(members) >= self._member_cap(f):
            raise StrategyError("势力成员已满")
        apps.pop(applicant_user, None)
        data["join_apps"][str(f["id"])] = apps
        world["data"] = data
        await self.save_world(world)
        await self._join_faction(world, target, int(f["id"]))
        await self._commit()
        return {"ok": True, "msg": f"已批准 {applicant_user} 入盟"}

    async def faction_reject(self, user_id: str, applicant_user: str) -> dict:
        world, p = await self._require_player(user_id)
        f = await self.get_faction(p["faction_id"])
        if not f:
            raise StrategyError("当前不在势力中")
        await self._require_position(f, user_id, ("leader", "deputy"))
        data = world.get("data") or {}
        apps = (data.get("join_apps") or {}).get(str(f["id"])) or {}
        if applicant_user not in apps:
            raise StrategyError("该申请不存在")
        apps.pop(applicant_user, None)
        data["join_apps"][str(f["id"])] = apps
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return {"ok": True, "msg": f"已拒绝 {applicant_user} 的申请"}

    async def faction_quit(self, user_id: str) -> dict:
        """退盟：回归个人势力（散修），24h 内不可再入盟。"""
        world, p = await self._require_player(user_id)
        fcfg = self.main_cfg().get("faction") or {}
        fid = int(p["faction_id"])
        f = await self.get_faction(fid)
        members = await self.get_members(fid)
        if not f or len(members) <= 1:
            raise StrategyError("当前不在多人势力中")
        pos = self._position_of(members, user_id)
        if pos == "leader" and len(members) > 1:
            succ = R.successor_of(members, user_id)
            if not succ:
                raise StrategyError("请先移交主公之位")
            await self.upsert_member(fid, succ["user_id"], position="leader")
            f["leader_id"] = succ["user_id"]
            await self.save_faction(f)
        data = world.get("data") or {}
        cd_hours = int(fcfg.get("quit_cooldown_hours", 24))
        data.setdefault("quit_cd", {})[user_id] = _now() + cd_hours * 3600
        world["data"] = data
        await self.save_world(world)
        personal = await self._ensure_personal_faction(world, p)
        await self._join_faction(world, p, personal, position="leader")
        await self.upsert_member(personal, user_id, position="leader")
        await self._commit()
        return {"ok": True,
                "msg": f"已退出「{f['name']}」，成为散修（{cd_hours}h 内不可再入盟）"}

    async def faction_set_position(self, user_id: str, target_user: str,
                                   position: str) -> dict:
        """官职任免（§4.5）：主公/副主操作；名额按配置。"""
        world, p = await self._require_player(user_id)
        f = await self.get_faction(p["faction_id"])
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader", "deputy"))
        caps = dict(POSITION_CAPS_DEFAULT)
        caps.update((self.main_cfg().get("faction") or {}).get("positions") or {})
        position = str(position or "").strip()
        if position not in caps or position == "leader":
            raise StrategyError("官职无效（主公之位请走移交）")
        members = await self.get_members(f["id"])
        target = next((m for m in members if m["user_id"] == str(target_user)), None)
        if not target:
            raise StrategyError("目标不是本势力成员")
        holders = [m for m in members if (m.get("position") or "") == position
                   and m["user_id"] != str(target_user)]
        if len(holders) >= int(caps.get(position, 1)):
            raise StrategyError(f"{POSITION_CN.get(position, position)}名额已满"
                                f"（{caps.get(position, 1)}）")
        if target.get("position") == "leader":
            raise StrategyError("不能变更主公官职（请走移交）")
        await self.upsert_member(f["id"], str(target_user), position=position)
        await self._commit()
        cn = POSITION_CN.get(position, position)
        if not position:
            cn = "白身"
        return {"ok": True, "msg": f"已任命 {target_user} 为「{cn}」"}

    async def transfer_leader(self, user_id: str, target_user: str) -> dict:
        world, p = await self._require_player(user_id)
        f = await self.get_faction(p["faction_id"])
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader",))
        members = await self.get_members(f["id"])
        target = next((m for m in members if m["user_id"] == str(target_user)), None)
        if not target:
            raise StrategyError("目标不是本势力成员")
        await self.upsert_member(f["id"], str(target_user), position="leader")
        await self.upsert_member(f["id"], user_id, position="deputy")
        f["leader_id"] = str(target_user)
        await self.save_faction(f)
        await self._commit()
        return {"ok": True, "msg": f"主公之位已移交 {target_user}，你转任副主"}

    # ------------------------------------------------------------------
    # 国库（§8.4）
    # ------------------------------------------------------------------
    async def treasury_deposit(self, user_id: str, resource: str, amount: int) -> dict:
        world, p = await self._require_player(user_id)
        resource = str(resource)
        amount = int(amount)
        if resource not in ("coin", "grain", "iron") or amount <= 0:
            raise StrategyError("仅支持存入 coin/grain/iron 且数量为正")
        if int(p.get(resource, 0)) < amount:
            raise StrategyError(f"{resource} 不足")
        f = await self.get_faction(p["faction_id"])
        if not f:
            raise StrategyError("势力不存在")
        p[resource] -= amount
        treasury = f.get("treasury") or {}
        treasury[resource] = int(treasury.get(resource, 0)) + amount
        f["treasury"] = treasury
        await self.save_faction(f)
        await self.save_player(p)
        await self.add_contribution(f["id"], user_id, max(1, amount // 10))
        await self._commit()
        return {"ok": True, "msg": f"存入国库 {resource} ×{amount}"}

    async def treasury_withdraw(self, user_id: str, resource: str, amount: int) -> dict:
        world, p = await self._require_player(user_id)
        resource = str(resource)
        amount = int(amount)
        if resource not in ("coin", "grain", "iron") or amount <= 0:
            raise StrategyError("仅支持提取 coin/grain/iron 且数量为正")
        f = await self.get_faction(p["faction_id"])
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        pos = await self._require_position(f, user_id, ("leader", "deputy"))
        treasury = f.get("treasury") or {}
        have = int(treasury.get(resource, 0))
        if have < amount:
            raise StrategyError(f"国库 {resource} 不足（现有 {have}）")
        if pos == "deputy":
            pct = float((self.main_cfg().get("faction") or {}).get(
                "treasury_deputy_pct", 0.3))
            if amount > int(have * pct):
                raise StrategyError(f"副主单次提取上限为国库的 {int(pct * 100)}%"
                                    f"（{int(have * pct)}）")
        treasury[resource] = have - amount
        f["treasury"] = treasury
        p[resource] = int(p.get(resource, 0)) + amount
        await self.save_faction(f)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": f"从国库提取 {resource} ×{amount}"}

    # ------------------------------------------------------------------
    # 势力科技（§8.4）
    # ------------------------------------------------------------------
    async def tech_research(self, user_id: str, line: str) -> dict:
        world, p = await self._require_player(user_id)
        tcfg = self.main_cfg().get("tech") or {}
        line = str(line)
        if line not in (tcfg.get("lines") or list(R.TECH_LINES)):
            raise StrategyError("科技线无效（military/agri/stratagem）")
        f = await self.get_faction(p["faction_id"])
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, OFFICER_POSITIONS)
        tech = f.get("tech") or {}
        if tech.get("pending"):
            pend = tech["pending"]
            raise StrategyError(f"已有研究进行中（{pend.get('line')}，"
                                f"剩余 {(int(pend.get('finish_at', 0)) - _now() + 59) // 60} 分钟）")
        lvl = R.tech_level(tech, line)
        max_lvl = int(tcfg.get("levels", 5))
        if lvl >= max_lvl:
            raise StrategyError(f"{line} 已满级（L{max_lvl}）")
        cost_coin = int((tcfg.get("cost_coin") or [500] * 5)[lvl])
        cost_iron = int((tcfg.get("cost_iron") or [500] * 5)[lvl])
        treasury = f.get("treasury") or {}
        if int(treasury.get("coin", 0)) < cost_coin or int(treasury.get("iron", 0)) < cost_iron:
            raise StrategyError(f"国库不足：研究 L{lvl + 1} 需铜钱 {cost_coin} / 精铁 {cost_iron}")
        # 耗时：基础小时 × 主属性加速（取本势力在任文官最高智谋/政务）
        base_hours = float((tcfg.get("base_hours_per_level") or [8, 16, 24, 32, 40])[lvl])
        stat = await self._best_research_stat(f["id"], world["id"])
        hours = R.tech_research_hours(base_hours, stat,
                                      float(tcfg.get("stat_speed_divisor", 10)),
                                      float(tcfg.get("stat_speed_pct", 0.05)))
        treasury["coin"] = int(treasury.get("coin", 0)) - cost_coin
        treasury["iron"] = int(treasury.get("iron", 0)) - cost_iron
        f["treasury"] = treasury
        tech["pending"] = {"line": line, "by": user_id,
                           "finish_at": _now() + int(hours * 3600)}
        f["tech"] = tech
        await self.save_faction(f)
        await self._commit()
        return {"ok": True,
                "msg": f"开始研究 {line} L{lvl + 1}（{hours:.1f}h，"
                       f"扣国库铜钱 {cost_coin}/精铁 {cost_iron}）"}

    async def _best_research_stat(self, faction_id: int, world_id: int) -> int:
        best = 50
        for c in await self.get_chars(faction_id=faction_id, world_id=world_id):
            st = c.get("stats") or {}
            best = max(best, int(st.get("wisdom", 0)), int(st.get("admin", 0)))
        return best

    async def _step_tech(self, world: dict):
        """tick：到期的科技研究完成（等级 +1）。"""
        rows = await self._fetchall(
            "SELECT * FROM strategy_factions WHERE world_id=? AND alive=1", (world["id"],))
        changed = False
        for row in rows:
            f = self._decode("strategy_factions", row)
            tech = f.get("tech") or {}
            pend = tech.get("pending") or {}
            if pend and int(pend.get("finish_at", 0)) <= _now():
                line = str(pend.get("line", ""))
                if line in R.TECH_LINES:
                    tech[line] = R.tech_level(tech, line) + 1
                    tech.pop("pending", None)
                    f["tech"] = tech
                    await self.save_faction(f)
                    changed = True
                    logger.info(f"[strategy] 势力#{f['id']} 科技 {line} → L{tech[line]}")
        if changed:
            await self._commit()

    # ------------------------------------------------------------------
    # 外交（§8.3）与声望（§8.2）
    # ------------------------------------------------------------------
    async def add_reputation(self, f: dict, delta: int) -> int:
        rep = max(-100, min(100, int(f.get("reputation", 0)) + int(delta)))
        f["reputation"] = rep
        await self.save_faction(f)
        return rep

    def _set_relation(self, f_dip: dict, other_id: int, rel: str,
                      until: int = 0, war_at: int = 0) -> dict:
        entry = f_dip.setdefault(str(int(other_id)), {})
        entry.update({"rel": rel, "until": int(until or 0),
                      "war_at": int(war_at or 0)})
        return f_dip

    async def _alive_faction_ids(self, world: dict) -> set:
        """C-M2：当前存续势力 id 集合（字符串键，与 diplomacy dict 对齐）。"""
        rows = await self._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND alive=1",
            (world["id"],))
        return {str(int(r["id"])) for r in rows}

    @staticmethod
    def _filter_dip(dip: dict, alive_ids: set) -> dict:
        """剔除已覆灭势力的外交记录（结盟计数等仅看存续势力）。"""
        return {k: v for k, v in (dip or {}).items() if str(k) in alive_ids}

    async def diplomacy_propose(self, user_id: str, target_fid: int, kind: str,
                                indemnity: int = 0) -> dict:
        """外交提案：war（宣战，立即进入备战）/ truce / ally / peace（和谈赔款）。"""
        world, p = await self._require_player(user_id)
        dcfg = self.main_cfg().get("diplomacy") or {}
        kind = str(kind)
        if kind not in ("war", "truce", "ally", "peace"):
            raise StrategyError("外交类型无效（war/truce/ally/peace）")
        f = await self.get_faction(p["faction_id"])
        tf = await self.get_faction(int(target_fid))
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        if not tf or not int(tf.get("alive", 0)) or int(tf["id"]) == int(f["id"]):
            raise StrategyError("目标势力无效")
        now = _now()
        f_dip = f.get("diplomacy") or {}
        t_dip = tf.get("diplomacy") or {}
        cur = R.relation_of(f_dip, tf["id"], now)
        if kind == "war":
            if cur in ("war", "war_pending"):
                raise StrategyError("双方已处于战争状态")
            if cur == "truce":
                # 撕毁停战直接宣战：声望 -15
                await self.add_reputation(f, int(dcfg.get("truce_break_rep", -15)))
            if cur == "ally":
                await self.add_reputation(f, int(
                    (self.main_cfg().get("reputation") or {}).get("betray_ally_cost", -20)))
            prep = int(dcfg.get("war_prep_hours", 24)) * 3600
            prep = int(prep * float(R.reputation_effects(f.get("reputation", 0)).get(
                "war_prep_mult", 1.0)))
            self._set_relation(f_dip, tf["id"], R.REL_WAR, war_at=now + prep)
            self._set_relation(t_dip, f["id"], R.REL_WAR, war_at=now + prep)
            f["diplomacy"] = f_dip
            tf["diplomacy"] = t_dip
            await self.save_faction(f)
            await self.save_faction(tf)
            await self._commit()
            return {"ok": True,
                    "msg": f"⚔️ 已向「{tf['name']}」宣战！备战期 {prep // 3600} 小时后全面开战"}
        if kind == "peace":
            if cur not in ("war", "war_pending"):
                raise StrategyError("仅在战争状态下可提出和谈")
            cap = int(int((f.get("treasury") or {}).get("coin", 0)) *
                      float(dcfg.get("peace_indemnity_cap_pct", 0.2)))
            indemnity = int(indemnity)
            if indemnity < 0 or indemnity > cap:
                raise StrategyError(f"赔款上限为国库铜钱的 20%（{cap}）")
        if kind == "ally":
            cap_ally = int(dcfg.get("ally_max", 3))
            # C-M2：仅统计存续势力的同盟位（已覆灭势力的 ally 记录不再占坑）
            alive_ids = await self._alive_faction_ids(world)
            if not R.can_ally_count(self._filter_dip(f_dip, alive_ids), now, cap_ally) or \
                    not R.can_ally_count(self._filter_dip(t_dip, alive_ids), now, cap_ally):
                raise StrategyError(f"任一方同盟数已达上限（{cap_ally}）")
            if cur == "war" or cur == "war_pending":
                raise StrategyError("交战中无法结盟（请先和谈）")
        if kind == "truce":
            if cur not in ("war", "war_pending"):
                raise StrategyError("仅在战争状态下可提出停战")
        # --- 提案（对方为玩家 → 等待回应；NPC → 立即掷点）---
        if tf.get("kind") == "npc":
            accepted = self._npc_diplomacy_roll(world, f, tf, kind, indemnity, dcfg)
            if accepted:
                await self._apply_diplomacy_deal(f, tf, kind, indemnity, dcfg)
                return {"ok": True, "msg": f"「{tf['name']}」接受了{self._kind_cn(kind)}"}
            return {"ok": True, "msg": f"「{tf['name']}」拒绝了{self._kind_cn(kind)}"}
        expire = now + 48 * 3600
        self._set_relation(t_dip, f["id"], f"propose_{kind}", until=expire)
        t_dip[str(f["id"])]["from"] = int(f["id"])
        t_dip[str(f["id"])]["indemnity"] = int(indemnity)
        self._set_relation(f_dip, tf["id"], f"propose_{kind}_out", until=expire)
        f["diplomacy"] = f_dip
        tf["diplomacy"] = t_dip
        await self.save_faction(f)
        await self.save_faction(tf)
        await self._commit()
        return {"ok": True,
                "msg": f"{self._kind_cn(kind)}提案已发往「{tf['name']}」（48h 内有效）"}

    @staticmethod
    def _kind_cn(kind: str) -> str:
        return {"war": "宣战", "truce": "停战", "ally": "结盟", "peace": "和谈"}.get(
            kind, kind)

    def _npc_diplomacy_roll(self, world: dict, f: dict, tf: dict, kind: str,
                            indemnity: int, dcfg: dict) -> bool:
        rng = self._rng("diplomacy", f["id"], tf["id"], kind, _now() // 3600)
        if kind == "truce":
            return rng.random() < 0.7
        if kind == "ally":
            charm = 50
            base = float(dcfg.get("ally_base_chance", 40))
            chance = R.ally_accept_chance(
                charm, int(f.get("reputation", 0)), int(tf.get("reputation", 0)),
                base=base, charm_div=float(dcfg.get("ally_charm_divisor", 4)),
                rep_div=float(dcfg.get("ally_rep_divisor", 2)))
            return rng.random() * 100 < chance
        if kind == "peace":
            cap = max(1, int(int((f.get("treasury") or {}).get("coin", 0)) *
                             float(dcfg.get("peace_indemnity_cap_pct", 0.2))))
            chance = min(0.9, 0.3 + 0.6 * int(indemnity) / cap)
            return rng.random() < chance
        return False

    async def _apply_diplomacy_deal(self, f: dict, tf: dict, kind: str,
                                    indemnity: int, dcfg: dict):
        now = _now()
        f_dip = f.get("diplomacy") or {}
        t_dip = tf.get("diplomacy") or {}
        if kind == "truce" or kind == "peace":
            until = now + int(dcfg.get("truce_hours", 48)) * 3600
            self._set_relation(f_dip, tf["id"], R.REL_TRUCE, until=until)
            self._set_relation(t_dip, f["id"], R.REL_TRUCE, until=until)
            if kind == "peace" and int(indemnity) > 0:
                pay = min(int(indemnity), int((f.get("treasury") or {}).get("coin", 0)))
                ft = f.get("treasury") or {}
                tt = tf.get("treasury") or {}
                ft["coin"] = int(ft.get("coin", 0)) - pay
                tt["coin"] = int(tt.get("coin", 0)) + pay
                f["treasury"] = ft
                tf["treasury"] = tt
        elif kind == "ally":
            self._set_relation(f_dip, tf["id"], R.REL_ALLY)
            self._set_relation(t_dip, f["id"], R.REL_ALLY)
        f["diplomacy"] = f_dip
        tf["diplomacy"] = t_dip
        await self.save_faction(f)
        await self.save_faction(tf)
        await self._commit()
        if kind == "ally":
            for uid in (f.get("leader_id"), tf.get("leader_id")):
                if uid:
                    await self._award_achievement(uid, "social_ally")

    async def diplomacy_respond(self, user_id: str, from_fid: int,
                                accept: bool) -> dict:
        world, p = await self._require_player(user_id)
        dcfg = self.main_cfg().get("diplomacy") or {}
        f = await self.get_faction(p["faction_id"])
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, OFFICER_POSITIONS)
        f_dip = f.get("diplomacy") or {}
        entry = f_dip.get(str(int(from_fid))) or {}
        rel = str(entry.get("rel", ""))
        if not rel.startswith("propose_") or rel.endswith("_out") \
                or int(entry.get("from", 0)) != int(from_fid):
            raise StrategyError("没有来自该势力的待决提案")
        if int(entry.get("until", 0)) and _now() > int(entry["until"]):
            raise StrategyError("提案已过期")
        tf = await self.get_faction(int(from_fid))
        if not tf or not int(tf.get("alive", 0)):
            raise StrategyError("提案方势力已消亡")
        kind = rel[len("propose_"):]
        if not accept:
            self._clear_proposals(f, tf)
            await self.save_faction(f)
            await self.save_faction(tf)
            await self._commit()
            return {"ok": True, "msg": f"已拒绝「{tf['name']}」的{self._kind_cn(kind)}提案"}
        if kind == "ally":
            cap_ally = int(dcfg.get("ally_max", 3))
            if not R.can_ally_count(f.get("diplomacy") or {}, _now(), cap_ally) or \
                    not R.can_ally_count(tf.get("diplomacy") or {}, _now(), cap_ally):
                raise StrategyError("任一方同盟数已达上限")
        self._clear_proposals(f, tf)
        await self._apply_diplomacy_deal(
            f, tf, kind, int(entry.get("indemnity", 0)), dcfg)
        return {"ok": True,
                "msg": f"已接受「{tf['name']}」的{self._kind_cn(kind)}提案"}

    def _clear_proposals(self, f: dict, tf: dict):
        f_dip = f.get("diplomacy") or {}
        t_dip = tf.get("diplomacy") or {}
        f_dip.pop(str(tf["id"]), None)
        t_dip.pop(str(f["id"]), None)
        f["diplomacy"] = f_dip
        tf["diplomacy"] = t_dip

    async def check_truce_violation(self, world: dict, atk_fid: int,
                                    dfn_fid: int) -> bool:
        """进攻前调用：停战/同盟期间动手 → 撕毁声望惩罚并转入战争。返回是否发生撕毁。"""
        if not atk_fid or not dfn_fid or atk_fid == dfn_fid or dfn_fid <= 0:
            return False
        f = await self.get_faction(atk_fid)
        tf = await self.get_faction(dfn_fid)
        if not f or not tf:
            return False
        now = _now()
        rel = R.relation_of(f.get("diplomacy") or {}, dfn_fid, now)
        dcfg = self.main_cfg().get("diplomacy") or {}
        if rel == R.REL_TRUCE:
            await self.add_reputation(f, int(dcfg.get("truce_break_rep", -15)))
        elif rel == R.REL_ALLY:
            await self.add_reputation(f, int(
                (self.main_cfg().get("reputation") or {}).get("betray_ally_cost", -20)))
        else:
            return False
        f_dip = f.setdefault("diplomacy", {})
        tf_dip = tf.setdefault("diplomacy", {})
        self._set_relation(f_dip, dfn_fid, R.REL_WAR, war_at=now)
        self._set_relation(tf_dip, atk_fid, R.REL_WAR, war_at=now)
        await self.save_faction(f)
        await self.save_faction(tf)
        await self._commit()
        logger.info(f"[strategy] 势力#{atk_fid} 撕毁条约进攻 #{dfn_fid}，转入战争")
        return True

    # ------------------------------------------------------------------
    # 治理（§8.6）：继任 / 僵尸势力崩溃 / 过期会签退款
    # ------------------------------------------------------------------
    async def _step_governance(self, world: dict):
        gcfg = self.main_cfg().get("governance") or {}
        now = _now()
        # 过期外交提案清理（propose_* 残留条目，避免面板/关系判定长期背负脏数据）
        for row in await self._fetchall(
                "SELECT id FROM strategy_factions WHERE world_id=? AND alive=1",
                (world["id"],)):
            fx = await self.get_faction(int(row["id"]))
            if not fx:
                continue
            dip = fx.get("diplomacy") or {}
            changed = False
            for k, v in list(dip.items()):
                rel = str((v or {}).get("rel", ""))
                until = int((v or {}).get("until", 0) or 0)
                if rel.startswith("propose_") and until and now > until:
                    dip.pop(k, None)
                    changed = True
            if changed:
                fx["diplomacy"] = dip
                await self.save_faction(fx)
        data = world.get("data") or {}
        dirty = False
        # 过期会签退款
        pets = data.get("faction_petitions") or {}
        for pid, pet in list(pets.items()):
            if pet.get("state") == "open" and now > int(pet.get("expire_at", 0)):
                pet["state"] = "expired"
                cp = await self.get_player(pet["creator"], world["id"])
                if cp:
                    cp["coin"] = int(cp.get("coin", 0)) + int(pet.get("escrow", 0))
                    await self.save_player(cp)
                dirty = True
        if dirty:
            data["faction_petitions"] = pets
            world["data"] = data
            await self.save_world(world)
            await self._commit()
        # 玩家势力治理
        nodes = await self.get_nodes(world["id"])
        node_count: dict[int, int] = {}
        for n in nodes:
            node_count[n["owner_faction"]] = node_count.get(n["owner_faction"], 0) + 1
        rows = await self._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='player' AND alive=1",
            (world["id"],))
        absent_days = int(gcfg.get("leader_absent_days", 7))
        zombie_days = int(gcfg.get("zombie_days", 30))
        for row in rows:
            fid = int(row["id"])
            f = await self.get_faction(fid)
            if not f:
                continue
            members = await self.get_members(fid)
            # 势力等级/上限刷新
            lvl = self._faction_level(node_count.get(fid, 0))
            cap = self._member_cap({**f, "level": lvl})
            if int(f.get("level", 1)) != lvl or int(f.get("member_cap", 10)) != cap:
                f["level"] = lvl
                f["member_cap"] = cap
                await self.save_faction(f)
            if not members:
                continue
            player_rows = {m["user_id"]: await self.get_player(m["user_id"], world["id"])
                           for m in members}
            leader_user = str(f.get("leader_id") or "")
            # 领袖缺席继任
            lp = player_rows.get(leader_user)
            if lp and len(members) > 1 and \
                    now - int(lp.get("last_seen_at", 0) or 0) > absent_days * 86400:
                succ = R.successor_of(members, leader_user)
                if succ and succ["user_id"] != leader_user:
                    await self.upsert_member(fid, succ["user_id"], position="leader")
                    await self.upsert_member(fid, leader_user, position="deputy")
                    f["leader_id"] = succ["user_id"]
                    await self.save_faction(f)
                    logger.info(f"[strategy] 势力#{fid} 领袖缺席 {absent_days} 天，"
                                f"{succ['user_id']} 继任")
            # 僵尸势力崩溃
            seen = [int((player_rows.get(m["user_id"]) or {}).get("last_seen_at", 0) or 0)
                    for m in members]
            if seen and max(seen) and now - max(seen) > zombie_days * 86400:
                await self._collapse_faction(world, f, members, nodes, gcfg)

    async def _collapse_faction(self, world: dict, f: dict, members: list[dict],
                                nodes: list[dict], gcfg: dict):
        """僵尸势力崩溃：国库按贡献清算、30% 据点沦为匪寨、成员变散修。"""
        fid = int(f["id"])
        contributions = {m["user_id"]: int(m.get("contribution", 0)) for m in members}
        shares = R.treasury_split(f.get("treasury") or {}, contributions)
        for uid, share in shares.items():
            p = await self.get_player(uid, world["id"])
            if p:
                for k in ("coin", "grain", "iron"):
                    p[k] = int(p.get(k, 0)) + int(share.get(k, 0))
                await self.save_player(p)
        owned = [n for n in nodes if n["owner_faction"] == fid]
        rng = self._rng("collapse", fid, world["tick_no"])
        n_bandit = int(len(owned) * float(gcfg.get("collapse_bandit_chance", 0.3)))
        data = world.get("data") or {}
        bandits = data.setdefault("bandits", {})
        for node in rng.sample(owned, min(n_bandit, len(owned))):
            node["owner_faction"] = 0
            gar = node.get("npc_garrison") or {}
            node["npc_garrison"] = {"troops": max(800, int(gar.get("troops", 0) * 1.5)),
                                    "morale": 90}
            bandits[str(node["id"])] = {"spawned_at": _now(), "origin": "collapse",
                                        "until": _now() + 30 * 86400}
            await self.save_node(node)
        for node in owned:
            if node["owner_faction"] == fid:
                node["owner_faction"] = 0
                await self.save_node(node)
        f["alive"] = 0
        f["faction_state"] = "collapsed"
        f["treasury"] = {}
        await self.save_faction(f)
        for m in members:
            p = await self.get_player(m["user_id"], world["id"])
            if p:
                personal = await self._ensure_personal_faction(world, p)
                await self._join_faction(world, p, personal, position="leader")
            await self.delete_member(fid, m["user_id"])
        data["bandits"] = bandits
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        logger.info(f"[strategy] 势力#{fid}「{f['name']}」僵尸崩溃："
                    f"{n_bandit} 处据点沦为匪寨")

    # ------------------------------------------------------------------
    # 主城重建（§8.7）与护山盾（§8.5）
    # ------------------------------------------------------------------
    async def _deduct_main_gold(self, user_id: str, amount: int) -> bool:
        """从主玩法钱包扣灵石（不足返回 False）。"""
        try:
            from ..handlers.utils import get_player_lock
            lock = get_player_lock(user_id)
        except Exception:  # noqa: BLE001
            lock = asyncio.Lock()
        async with lock:
            player = await self.db.get_player_by_id(user_id)
            if player is None or int(player.gold or 0) < int(amount):
                return False
            player.gold = int(player.gold or 0) - int(amount)
            await self.db.update_player(player)
        return True

    async def rebuild_capital(self, user_id: str, node_id: int) -> dict:
        """主城重建：主玩法灵石付费（300 万 ×1.5^n），新首都 7 天保护。"""
        world, p = await self._require_player(user_id)
        rcfg = self.main_cfg().get("rebuild") or {}
        f = await self.get_faction(p["faction_id"])
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader",))
        node = await self._owned_node(p, node_id)
        if node["type"] not in ("city", "pass", "branch"):
            raise StrategyError("新主城须为城池/关隘/分舵")
        cost = R.rebuild_gold_cost(int(rcfg.get("base_gold_cost", 3_000_000)),
                                   int(f.get("rebuild_count", 0)),
                                   float(rcfg.get("cost_growth", 0.5)))
        if not await self._deduct_main_gold(user_id, cost):
            raise StrategyError(f"主玩法灵石不足：重建需 {cost:,}")
        f["capital_node_id"] = int(node["id"])
        f["rebuild_count"] = int(f.get("rebuild_count", 0)) + 1
        await self.save_faction(f)
        node["protect_until"] = _now() + int(rcfg.get("protect_days", 7)) * 86400
        node["level"] = max(int(node.get("level", 1)),
                            int(rcfg.get("new_capital_level", 1)))
        await self.save_node(node)
        log = p.get("rebuild_pay_log") or []
        log.append({"at": _now(), "cost": cost, "node_id": int(node["id"])})
        p["rebuild_pay_log"] = log[-20:]
        await self.save_player(p)
        await self._commit()
        return {"ok": True,
                "msg": f"🏯 主城迁至「{node['name']}」（灵石 -{cost:,}，"
                       f"{int(rcfg.get('protect_days', 7))} 天保护期）"}

    async def buy_shield(self, user_id: str, node_id: int) -> dict:
        """护山盾：800 战功 → 据点 12h 免战；同据点 72h CD。"""
        world, p = await self._require_player(user_id)
        scfg = self.main_cfg().get("shield") or {}
        node = await self._owned_node(p, node_id)
        cost = int(scfg.get("merit_cost", 800))
        hours = int(scfg.get("hours", 12))
        cd_hours = int(scfg.get("cooldown_hours", 72))
        data = world.get("data") or {}
        cd_map = data.setdefault("shield_cd", {})
        until = int(cd_map.get(str(node["id"]), 0))
        if until > _now():
            raise StrategyError(f"该据点护山盾冷却中（还剩 {(until - _now() + 3599) // 3600}h）")
        if node.get("protect_until", 0) > _now():
            raise StrategyError("据点已处于保护期")
        if int(p.get("merit", 0)) < cost:
            raise StrategyError(f"战功不足：护山盾需 {cost}")
        p["merit"] -= cost
        node["protect_until"] = _now() + hours * 3600
        cd_map[str(node["id"])] = _now() + cd_hours * 3600
        data["shield_cd"] = cd_map
        world["data"] = data
        await self.save_world(world)
        await self.save_node(node)
        await self.save_player(p)
        await self._commit()
        return {"ok": True, "msg": f"🛡「{node['name']}」护山盾已展开（{hours}h 免战）"}


def hashlib_md5(s: str) -> str:
    import hashlib
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]
