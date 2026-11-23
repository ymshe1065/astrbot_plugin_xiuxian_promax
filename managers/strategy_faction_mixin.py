# -*- coding: utf-8 -*-
"""「仙域争霸」P1 势力联机 Mixin（设计方案 §8）。

术语：势力 = 玩家个人势力；联盟 = 多个个人势力结盟后的集体。
覆盖：联盟会签 / 申请·邀请·审批 / 退盟 CD / 官职任免 /
本势力国库存取 / 本势力科技 / 联盟外交（宣战·停战·结盟·和谈）/ 声望 /
治理（继任·僵尸势力崩溃）/ 本势力主城重建 / 护山盾。

v4.1.0 补全（§6.4/§8.7/§10.3/§18）：联盟/势力任务（刷新·进度·领取）/
动员令 / 势力事件（六种 effect）/ 科技指派角色 / 迁都 /
重建合力（rebuild_fund）/ 入盟观察期（官职·战功）/ 低忠诚出走。

作为 StrategyManager 的 mixin 使用，依赖主类的
cfg/main_cfg/_fetchall/_execute/_commit/get_*/save_* 等方法。
临时态（会签/申请/退盟CD/护山盾CD）统一存 strategy_worlds.data_json。
"""
from __future__ import annotations

import asyncio
import json
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
POSITION_CN = {"leader": "势力主", "deputy": "副主", "situ": "司徒", "taiwei": "太尉",
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

    async def _own_faction_members(self, world_id: int,
                                   faction_id: int) -> list[dict]:
        """本势力真实成员（排除联盟共同治理名册里的盟友）。

        ``strategy_members`` 对盟主势力还保存着盟友玩家（position=deputy），
        直接拿它做「领袖缺席 / 僵尸势力」判定会把整个联盟当成一个势力：
        盟主永远不会僵尸化，崩溃时还会把盟友玩家迁出本势力。
        """
        rows = await self._fetchall(
            "SELECT m.* FROM strategy_members m "
            "JOIN strategy_players p ON p.world_id=? AND p.user_id=m.user_id "
            "WHERE m.faction_id=? AND p.faction_id=?",
            (int(world_id), int(faction_id), int(faction_id)))
        return [dict(r) for r in rows]

    async def get_member(self, faction_id: int, user_id: str) -> Optional[dict]:
        row = await self._fetchone(
            "SELECT * FROM strategy_members WHERE faction_id=? AND user_id=?",
            (int(faction_id), user_id))
        return dict(row) if row else None

    async def upsert_member(self, faction_id: int, user_id: str,
                            position: Optional[str] = None,
                            contribution: Optional[int] = None,
                            joined_at: Optional[int] = None,
                            record_join: bool = True):
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
        # §18.4 入盟留痕：供 _settle_merit 判定「24h 内转投」的多开嫌疑。
        # record_join=False 用于崩溃/被动迁移等非自愿入盟（v4.3.0），
        # 否则成员会在毫不知情的情况下背上 24h 双势力记录、战功被清零。
        if record_join:
            await self._record_faction_join(faction_id, user_id)
        return await self.get_member(faction_id, user_id)

    async def _record_faction_join(self, faction_id: int, user_id: str) -> None:
        """记录一次真实的入盟（仅当玩家当前 faction_id 就是该势力）。

        联盟名册登记（玩家仍在自身势力）不计入，避免结盟误判多开。
        """
        try:
            f = await self.get_faction(int(faction_id))
            if not f:
                return
            p = await self.get_player(str(user_id), int(f["world_id"]))
            if not p or int(p.get("faction_id") or 0) != int(faction_id):
                return
            await self._execute(
                "INSERT INTO strategy_audit (world_id, user_id, action, "
                "detail_json, created_at) VALUES (?,?,?,?,?)",
                (int(f["world_id"]), str(user_id), "faction_join",
                 json.dumps({"faction_id": int(faction_id)},
                            ensure_ascii=False), _now()))
        except Exception:  # noqa: BLE001 - 留痕失败不影响入盟
            logger.debug("[strategy] 入盟留痕失败", exc_info=True)

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

    @staticmethod
    def _position_cn(f: dict, pos: str) -> str:
        """治理身份的中文名：联盟盟主势力称「盟主」，普通势力称「势力主」。

        势力 = 玩家个人势力；多个势力结盟后，治理名册挂在盟主势力上，
        因此只有在 ``alliance_id == 自身 id`` 时 leader 才是盟主。
        """
        if pos == "leader":
            return "盟主" if int(f.get("alliance_id") or 0) == int(f.get("id") or 0) \
                else "势力主"
        return POSITION_CN.get(pos, pos)

    async def _require_position(self, f: dict, user_id: str,
                                allowed: tuple = ("leader",)) -> str:
        members = await self.get_members(f["id"])
        pos = self._position_of(members, user_id)
        if pos not in allowed:
            names = "/".join(self._position_cn(f, x) for x in allowed)
            raise StrategyError(f"仅 {names} 可执行此操作")
        return pos

    # ------------------------------------------------------------------
    # 联盟（v72）：保留各势力据点与独立经济；仅外交/官职/名册共同治理
    # ------------------------------------------------------------------
    async def _alliance_host(self, f: Optional[dict]) -> Optional[dict]:
        """返回联盟盟主势力；未结盟时返回自身。"""
        if not f:
            return f
        aid = int(f.get("alliance_id") or 0)
        if not aid or aid == int(f["id"]):
            return f
        host = await self.get_faction(aid)
        if host and int(host.get("alive", 0)):
            return host
        return f

    async def _alliance_factions(self, world_id: int, f: dict) -> list[dict]:
        """联盟内所有存续势力（盟主在首位）。"""
        host = await self._alliance_host(f)
        if not host:
            return []
        rows = await self._fetchall(
            "SELECT * FROM strategy_factions WHERE world_id=? AND alive=1 "
            "AND (id=? OR alliance_id=?)",
            (int(world_id), int(host["id"]), int(host["id"])))
        fs = self._decode_all("strategy_factions", rows)
        fs.sort(key=lambda x: (int(x["id"]) != int(host["id"]), int(x["id"])))
        return fs

    async def _alliance_ids(self, world_id: int, f: dict) -> set[int]:
        return {int(x["id"]) for x in await self._alliance_factions(world_id, f)}

    async def _governance_faction(self, p: dict) -> Optional[dict]:
        """玩家参与共同治理的势力：结盟后为盟主势力，否则为自身势力。

        v4.3.4：联盟只共同治理外交 / 官职 / 名册；国库与科技改为
        **各势力各自占有**，相关逻辑应使用 ``_own_faction``。
        """
        f = await self.get_faction(int(p.get("faction_id") or 0))
        return await self._alliance_host(f) if f else None

    async def _own_faction(self, p: dict) -> Optional[dict]:
        """玩家自己的势力（联盟内仍为本势力，不替换为盟主）。"""
        return await self.get_faction(int(p.get("faction_id") or 0))

    async def _sync_alliance_meta(self, host: dict):
        """把盟主的共同外交同步给成员势力；国库/科技各势力独立，不同步。"""
        if not host:
            return
        for m in await self._alliance_factions(int(host["world_id"]), host):
            if int(m["id"]) == int(host["id"]):
                continue
            m["diplomacy"] = dict(host.get("diplomacy") or {})
            await self.save_faction(m)

    async def _restore_alliance_diplomacy(self, world: dict, member: dict,
                                          host: dict) -> dict:
        """退盟时恢复成员入盟前的外交字典。

        恢复快照后，再补上联盟存续期间新增的战争/停战关系，避免成员
        通过退盟单方面脱离联盟战争（免费撕约）。快照存于 ``world.data``，
        不随 ``save_faction`` 的固定列落库。
        """
        data = world.get("data") or {}
        backup = data.get("alliance_dip_backup")
        if not isinstance(backup, dict):
            backup = {}
        snap = backup.pop(str(int(member["id"])), None)
        data["alliance_dip_backup"] = backup
        world["data"] = data
        restored: dict = {}
        if isinstance(snap, dict) and isinstance(snap.get("dip"), dict):
            for k, v in snap["dip"].items():
                restored[str(k)] = dict(v) if isinstance(v, dict) else v
        for k, v in (host.get("diplomacy") or {}).items():
            rel = str((v or {}).get("rel") or "")
            if rel in (R.REL_WAR, R.REL_TRUCE) and str(k) not in restored:
                restored[str(k)] = dict(v) if isinstance(v, dict) else v
        await self.save_world(world)
        return restored

    async def _add_faction_to_alliance(self, world: dict, member: dict,
                                       host: dict, leader_position: str = "deputy"):
        """把成员势力并入联盟：保留据点、国库与科技，仅合并外交并登记名册。"""
        if not member or not host:
            raise StrategyError("势力不存在")
        member = await self.get_faction(int(member["id"])) or member
        host = await self.get_faction(int(host["id"])) or host
        if int(member["id"]) == int(host["id"]):
            host["alliance_id"] = int(host["id"])
            await self.save_faction(host)
            return
        if member.get("kind") != "player" or host.get("kind") != "player":
            raise StrategyError("仅玩家势力之间可以结盟")
        cur_aid = int(member.get("alliance_id") or 0)
        if cur_aid and cur_aid != int(host["id"]):
            raise StrategyError(f"「{member['name']}」已在其他联盟中")
        # 入盟前备份成员原有外交关系：退盟/联盟解散时恢复，避免
        # 「结盟覆盖、退盟清空」导致成员此前签下的停战/好感凭空消失
        # （等于免费撕约，且 NPC 好感日限记录一并丢失）。
        data = world.get("data") or {}
        backup = data.get("alliance_dip_backup")
        if not isinstance(backup, dict):
            backup = {}
        backup[str(int(member["id"]))] = {
            "dip": dict(member.get("diplomacy") or {}), "at": _now()}
        data["alliance_dip_backup"] = backup
        world["data"] = data
        await self.save_world(world)
        # 国库与科技保持各势力独立：据点、资源、研究进度都不搬迁。
        host["alliance_id"] = int(host["id"])
        member["alliance_id"] = int(host["id"])
        # 联盟共同外交：以盟主政策为主，但成员已有的双边记录（尤其
        # 停战/战争/好感）并入盟主字典，避免结盟即抹除既有条约。
        h_dip = dict(host.get("diplomacy") or {})
        for k, v in (member.get("diplomacy") or {}).items():
            h_dip.setdefault(k, v)
        host["diplomacy"] = h_dip
        member["diplomacy"] = dict(h_dip)
        world["data"] = data
        await self.save_world(world)
        await self.save_faction(host)
        await self.save_faction(member)
        # 盟主名册 = 联盟共同治理名册；保留成员原势力名册不变
        rows = await self._fetchall(
            "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
            (world["id"], int(member["id"])))
        for row in rows:
            uid = str(row["user_id"])
            if await self.get_member(int(host["id"]), uid):
                continue
            pos = leader_position if uid == str(member.get("leader_id") or "") else ""
            # 联盟名册的 joined_at 必须继承玩家在成员势力中的真实入盟时间，
            # 否则每次结盟都重置为 now，§18.4 的 72h 战功观察期会把联盟
            # 成员的战功全部 ×0（盟主/官员除外，普通成员必中）。
            own = await self.get_member(int(member["id"]), uid) or {}
            joined = int(own.get("joined_at", 0) or 0) or None
            await self.upsert_member(int(host["id"]), uid, position=pos,
                                     joined_at=joined)
        await self._sync_alliance_meta(host)
        logger.info(f"[strategy] 势力#{member['id']}「{member['name']}」并入"
                    f"联盟#{host['id']}「{host['name']}」")

    async def _remove_faction_from_alliance(self, world: dict, member: dict) -> dict:
        """退出联盟：保留自己的据点、国库与科技，仅恢复共同外交。"""
        member = await self.get_faction(int(member["id"])) or member
        if not int(member.get("alliance_id") or 0):
            raise StrategyError("当前不在联盟中")
        host = await self._alliance_host(member)
        factions = await self._alliance_factions(world["id"], host)
        ids = {int(x["id"]) for x in factions}
        if int(member["id"]) == int(host["id"]):
            await self._dissolve_alliance(world, host, factions, ids)
            return {"ok": True,
                    "msg": f"联盟「{host['name']}」已解散，各成员势力保留现有据点"}
        rows = await self._fetchall(
            "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
            (world["id"], int(member["id"])))
        for row in rows:
            await self.delete_member(int(host["id"]), str(row["user_id"]))
        member["alliance_id"] = 0
        # 恢复入盟前的外交关系，并保留联盟存续期间新增的战争/停战，
        # 防止「退盟即脱战/免费撕约」。
        member["diplomacy"] = await self._restore_alliance_diplomacy(
            world, member, host)
        await self.save_faction(member)
        rest = [x for x in factions if int(x["id"]) != int(member["id"])]
        if len(rest) <= 1:
            host["alliance_id"] = 0
            await self.save_faction(host)
        logger.info(f"[strategy] 势力#{member['id']}「{member['name']}」退出联盟"
                    f"「{host['name']}」，保留据点与独立国库/科技")
        return {"ok": True,
                "msg": f"已退出联盟「{host['name']}」，现有据点、国库与科技全部保留"}

    async def _dissolve_alliance(self, world: dict, host: dict,
                                 factions: list[dict], ids: set[int]):
        for f in factions:
            if int(f["id"]) == int(host["id"]):
                continue
            rows = await self._fetchall(
                "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
                (world["id"], int(f["id"])))
            for row in rows:
                await self.delete_member(int(host["id"]), str(row["user_id"]))
            f["alliance_id"] = 0
            f["diplomacy"] = await self._restore_alliance_diplomacy(world, f, host)
            await self.save_faction(f)
        host["alliance_id"] = 0
        await self.save_faction(host)

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
        """发起联盟会签：垫付 10000 铜钱，72h 内集齐 3 个玩家势力（含发起人势力）。"""
        world, p = await self._require_player(user_id)
        fcfg = self.main_cfg().get("faction") or {}
        name = str(name or "").strip()
        if not (2 <= len(name) <= 12):
            raise StrategyError("联盟名须为 2~12 字")
        f = await self.get_faction(p["faction_id"])
        members = await self.get_members(p["faction_id"])
        if f and f.get("kind") != "player" or len(members) > 1:
            raise StrategyError("已在多人势力中，无法另立门户（请先退盟）")
        if f and int(f.get("alliance_id") or 0):
            raise StrategyError("已在联盟中，请先退出联盟")
        if self._position_of(members, user_id) != "leader":
            raise StrategyError("仅势力主可发起联盟会签")
        # 重名检查
        dup = await self._fetchone(
            "SELECT id FROM strategy_factions WHERE world_id=? AND name=? AND alive=1",
            (world["id"], name))
        if dup:
            raise StrategyError("联盟名已被占用")
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
        fallback_color = "#%06x" % random.Random(pid).randint(0x333333, 0xFFFFFF)
        pets[pid] = {
            "id": pid, "name": name,
            # 颜色只接受 #RGB/#RRGGBB：任意字符串会被前端拼进 style，
            # 形成存储型 XSS（如 "red\"></span><img onerror=...>"）。
            "color": R.norm_color(color, fallback_color),
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
                "msg": f"📜 联盟会签已发起：「{name}」还需 {need - 1} 个玩家势力联署"
                       f"（{int(fcfg.get('cosign_expire_hours', 72))}h 内有效，已垫付 {cost} 铜钱；"
                       f"成立后你的势力为盟主）"}

    async def faction_cosign(self, user_id: str, petition_id: str) -> dict:
        """联署联盟会签；集齐 3 个玩家势力即刻结盟。"""
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
        mf = await self.get_faction(p["faction_id"])
        if mf and int(mf.get("alliance_id") or 0):
            raise StrategyError("已在联盟中，无法联署")
        if self._position_of(members, user_id) != "leader":
            raise StrategyError("仅势力主可联署联盟会签")
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
                    "msg": f"🎉 联盟「{fname}」正式成立！"
                           f"发起人势力为盟主，联署势力为联盟成员"
                           f"（会签 {need} 个势力达成）"}
        pets[petition_id] = pet
        data["faction_petitions"] = pets
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return {"ok": True,
                "msg": f"联署成功：「{pet['name']}」还差 "
                       f"{need - 1 - len(pet['cosigners'])} 个玩家势力"}

    async def _do_create_faction(self, world: dict, pet: dict) -> int:
        """会签达成：以发起人的个人势力为盟主，联署人的个人势力作为联盟成员并入。"""
        creator = await self.get_player(pet["creator"], world["id"])
        if not creator:
            raise StrategyError("发起人已离开世界")
        host = await self.get_faction(int(creator["faction_id"]))
        if not host or host.get("kind") != "player":
            raise StrategyError("发起人没有可用的个人势力")
        host["name"] = pet["name"]
        host["color"] = R.norm_color(pet.get("color"), "#888888")
        host["leader_id"] = pet["creator"]
        host["alliance_id"] = int(host["id"])
        host["faction_state"] = "normal"
        host["alive"] = 1
        escrow = int(pet.get("escrow", 0))
        if escrow > 0:
            treasury = host.get("treasury") or {}
            treasury["coin"] = int(treasury.get("coin", 0)) + escrow
            host["treasury"] = treasury
        await self.save_faction(host)
        for uid in list(pet.get("cosigners") or []):
            p = await self.get_player(uid, world["id"])
            if not p:
                continue
            mf = await self.get_faction(int(p["faction_id"]))
            if not mf or int(mf["id"]) == int(host["id"]):
                continue
            await self._add_faction_to_alliance(world, mf, host)
        await self.upsert_member(int(host["id"]), pet["creator"], position="leader")
        await self._sync_alliance_meta(host)
        await self._commit()
        logger.info(f"[strategy] 联盟#{host['id']}「{pet['name']}」经会签建立")
        return int(host["id"])

    # ------------------------------------------------------------------
    # 联盟加入 / 退出（保留势力据点）
    # ------------------------------------------------------------------
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

    async def _move_player_to_faction(self, world: dict, p: dict, fid: int):
        """把玩家角色/军团迁入指定势力，并登记为该势力势力主。"""
        # 被俘/押运中的角色不得被入盟/崩溃等流程改写阵营：其 faction_id
        # 仍代表原势力，改写会让俘虏归属错乱（甚至直接脱离关押）。
        # 太守是据点级职务，随角色换阵营一并卸任，避免跨势力残留。
        await self._execute(
            "UPDATE strategy_nodes SET governor_char_id=0 WHERE world_id=? "
            "AND governor_char_id IN (SELECT id FROM strategy_characters "
            "WHERE world_id=? AND owner_user_id=? AND status NOT IN "
            "('captive','transfer') AND captor_faction<=0)",
            (world["id"], world["id"], p["user_id"]))
        await self._execute(
            "UPDATE strategy_characters SET faction_id=?, appointed_position='', "
            "appointed_node_id=0 WHERE world_id=? AND owner_user_id=? "
            "AND status NOT IN ('captive','transfer') AND captor_faction<=0",
            (int(fid), world["id"], p["user_id"]))
        await self._execute(
            "UPDATE strategy_armies SET faction_id=? WHERE world_id=? AND owner_user_id=?",
            (int(fid), world["id"], p["user_id"]))
        p["faction_id"] = int(fid)
        await self.save_player(p)
        # 强制迁移（势力崩溃→个人势力）不留入盟痕迹：这不是玩家主动转投，
        # 记入 faction_join 会让 _settle_merit 的多开检测误判（24h 内双势力）
        await self.upsert_member(int(fid), p["user_id"], position="leader",
                                 record_join=False)

    async def faction_apply(self, user_id: str, faction_id: int) -> dict:
        """以势力主身份申请加入联盟（保留自身据点，等待对方盟主/副主审批）。"""
        world, p = await self._require_player(user_id)
        my_f = await self.get_faction(int(p["faction_id"]))
        my_members = await self.get_members(int(p["faction_id"]))
        if not my_f or my_f.get("kind") != "player":
            raise StrategyError("当前没有可结盟的玩家势力")
        if self._position_of(my_members, user_id) != "leader":
            raise StrategyError("仅势力主可申请加入联盟")
        if int(my_f.get("alliance_id") or 0):
            raise StrategyError("当前势力已在联盟中，请先退出联盟")
        target = await self.get_faction(int(faction_id))
        if not target or not int(target.get("alive", 0)) or target.get("kind") != "player":
            raise StrategyError("目标势力不存在或不可加入联盟")
        host = await self._alliance_host(target)
        if int(host["id"]) == int(my_f["id"]):
            raise StrategyError("不能加入自己的联盟")
        if int(host.get("alliance_id") or 0) and \
                int(host["alliance_id"]) != int(host["id"]):
            host = await self._alliance_host(host)
        cap = int((self.main_cfg().get("alliance") or {}).get("max_factions", 3))
        if len(await self._alliance_factions(world["id"], host)) >= cap:
            raise StrategyError(
                f"「{host['name']}」联盟成员势力已达上限（{cap} 个）")
        data = world.get("data") or {}
        quit_cd = int((data.get("quit_cd") or {}).get(user_id, 0))
        if quit_cd > _now():
            hours = (quit_cd - _now() + 3599) // 3600
            raise StrategyError(f"退盟冷却中（还剩 {hours} 小时）")
        apps = data.setdefault("join_apps", {}).setdefault(str(host["id"]), {})
        if user_id in apps:
            raise StrategyError("已递交过申请，请等待审批")
        apps[user_id] = _now()
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return {"ok": True,
                "msg": f"已向联盟「{host['name']}」递交入盟申请"}

    async def faction_invite(self, user_id: str, target_user: str) -> dict:
        """盟主/副主邀请其他势力主加入联盟。"""
        world, p = await self._require_player(user_id)
        f = await self._governance_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader", "deputy"))
        target = await self.get_player(str(target_user), world["id"])
        if not target:
            raise StrategyError("对方尚未入场")
        tf = await self.get_faction(int(target["faction_id"]))
        t_members = await self.get_members(int(target["faction_id"]))
        if not tf or tf.get("kind") != "player":
            raise StrategyError("对方没有可结盟的玩家势力")
        if self._position_of(t_members, str(target_user)) != "leader":
            raise StrategyError("对方不是势力主，无法代表势力加入联盟")
        if int(tf.get("alliance_id") or 0):
            raise StrategyError("对方势力已在联盟中")
        if int(tf["id"]) == int(f["id"]):
            raise StrategyError("不能邀请自己")
        cap = int((self.main_cfg().get("alliance") or {}).get("max_factions", 3))
        if len(await self._alliance_factions(world["id"], f)) >= cap:
            raise StrategyError(f"联盟成员势力已达上限（{cap} 个）")
        data = world.get("data") or {}
        invites = data.setdefault("join_invites", {})
        invites[str(target_user)] = {"fid": int(f["id"]), "by": user_id, "at": _now()}
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        return {"ok": True, "msg": f"已邀请 {target_user} 的势力加入联盟「{f['name']}」"}

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
        host = await self.get_faction(int(inv["fid"]))
        if not host or not int(host.get("alive", 0)):
            raise StrategyError("邀请方联盟已不存在")
        host = await self._alliance_host(host)
        my_f = await self.get_faction(int(p["faction_id"]))
        my_members = await self.get_members(int(p["faction_id"]))
        if not my_f or my_f.get("kind") != "player":
            raise StrategyError("当前没有可结盟的玩家势力")
        if self._position_of(my_members, user_id) != "leader":
            raise StrategyError("仅势力主可接受入盟邀请")
        if int(my_f.get("alliance_id") or 0):
            raise StrategyError("当前势力已在联盟中")
        cap = int((self.main_cfg().get("alliance") or {}).get("max_factions", 3))
        if len(await self._alliance_factions(world["id"], host)) >= cap:
            raise StrategyError("对方联盟成员势力已达上限")
        # 与申请/退盟同口径的冷却校验：此前接受邀请可绕过 24h 退盟冷却，
        # 退盟后立刻借邀请重新入盟。
        quit_cd = int((data.get("quit_cd") or {}).get(user_id, 0))
        if quit_cd > _now():
            hours = (quit_cd - _now() + 3599) // 3600
            raise StrategyError(f"退盟冷却中（还剩 {hours} 小时）")
        data["join_invites"].pop(user_id, None)
        world["data"] = data
        await self.save_world(world)
        await self._add_faction_to_alliance(world, my_f, host)
        await self._commit()
        return {"ok": True, "msg": f"已加入联盟「{host['name']}」，自身据点保持不变"}

    async def faction_approve(self, user_id: str, applicant_user: str) -> dict:
        world, p = await self._require_player(user_id)
        f = await self._governance_faction(p)
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
        my_f = await self.get_faction(int(target["faction_id"]))
        my_members = await self.get_members(int(target["faction_id"]))
        if not my_f or my_f.get("kind") != "player":
            raise StrategyError("申请人没有可结盟的玩家势力")
        if self._position_of(my_members, str(applicant_user)) != "leader":
            raise StrategyError("申请人已不是势力主")
        if int(my_f.get("alliance_id") or 0):
            raise StrategyError("申请人势力已在联盟中")
        cap = int((self.main_cfg().get("alliance") or {}).get("max_factions", 3))
        if len(await self._alliance_factions(world["id"], f)) >= cap:
            raise StrategyError(f"联盟成员势力已达上限（{cap} 个）")
        # 审批路径同样受退盟冷却约束（申请人退盟后立即另投他盟）
        quit_cd = int((data.get("quit_cd") or {}).get(str(applicant_user), 0))
        if quit_cd > _now():
            hours = (quit_cd - _now() + 3599) // 3600
            raise StrategyError(f"申请人退盟冷却中（还剩 {hours} 小时）")
        apps.pop(applicant_user, None)
        data["join_apps"][str(f["id"])] = apps
        world["data"] = data
        await self.save_world(world)
        await self._add_faction_to_alliance(world, my_f, f)
        await self._commit()
        return {"ok": True, "msg": f"已批准「{my_f['name']}」加入联盟"}

    async def faction_reject(self, user_id: str, applicant_user: str) -> dict:
        world, p = await self._require_player(user_id)
        f = await self._governance_faction(p)
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
        """退出联盟：势力保留自己当前全部据点；未结盟的多人势力仍按旧逻辑散伙。"""
        world, p = await self._require_player(user_id)
        fcfg = self.main_cfg().get("faction") or {}
        fid = int(p["faction_id"])
        f = await self.get_faction(fid)
        members = await self.get_members(fid)
        if not f:
            raise StrategyError("当前不在势力中")
        data = world.get("data") or {}
        cd_hours = int(fcfg.get("quit_cooldown_hours", 24))

        def _mark_cd():
            data.setdefault("quit_cd", {})[user_id] = _now() + cd_hours * 3600
            world["data"] = data
            return self.save_world(world)

        aid = int(f.get("alliance_id") or 0)
        if aid:
            if self._position_of(members, user_id) != "leader":
                raise StrategyError("仅势力主可代表势力退出联盟")
            await _mark_cd()
            if aid == int(f["id"]):
                factions = await self._alliance_factions(world["id"], f)
                ids = {int(x["id"]) for x in factions}
                await self._dissolve_alliance(world, f, factions, ids)
                await self._commit()
                return {"ok": True,
                        "msg": f"联盟「{f['name']}」已解散，各成员势力保留现有据点"}
            await self._remove_faction_from_alliance(world, f)
            await self._commit()
            return {"ok": True,
                    "msg": f"已退出联盟，势力「{f['name']}」现有据点全部保留"
                           f"（{cd_hours}h 内不可再结盟）"}
        # 旧版多人势力（非联盟）：个人退伙后重建个人势力
        if len(members) <= 1:
            raise StrategyError("当前不在多人势力或联盟中")
        pos = self._position_of(members, user_id)
        if pos == "leader":
            succ = R.successor_of(members, user_id)
            if not succ:
                raise StrategyError("请先移交势力主之位")
            await self.upsert_member(fid, succ["user_id"], position="leader")
            f["leader_id"] = succ["user_id"]
            await self.save_faction(f)
        await _mark_cd()
        personal = await self._ensure_personal_faction(world, p)
        await self._move_player_to_faction(world, p, personal)
        await self.delete_member(fid, user_id)
        await self._commit()
        return {"ok": True,
                "msg": f"已退出「{f['name']}」，重建个人势力"
                       f"（{cd_hours}h 内不可再结盟）"}

    async def faction_set_position(self, user_id: str, target_user: str,
                                   position: str) -> dict:
        """官职任免（§4.5）：联盟盟主/副主可任免联盟名册成员。"""
        world, p = await self._require_player(user_id)
        f = await self._governance_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader", "deputy"))
        caps = dict(POSITION_CAPS_DEFAULT)
        caps.update((self.main_cfg().get("faction") or {}).get("positions") or {})
        position = str(position or "").strip()
        # 空字符串 = 卸任（返回白身）；此前直接判为无效，官职只能改任不能卸
        if position and (position not in caps or position == "leader"):
            raise StrategyError("官职无效（势力主之位请走移交）")
        members = await self.get_members(f["id"])
        target = next((m for m in members if m["user_id"] == str(target_user)), None)
        if not target:
            raise StrategyError("目标不是本势力/联盟成员")
        holders = [m for m in members if (m.get("position") or "") == position
                   and m["user_id"] != str(target_user)]
        if len(holders) >= int(caps.get(position, 1)):
            raise StrategyError(f"{POSITION_CN.get(position, position)}名额已满"
                                f"（{caps.get(position, 1)}）")
        if target.get("position") == "leader":
            raise StrategyError("不能变更势力主身份（请走移交）")
        # §18.4 观察期：入盟不足 min_join_hours_for_position 不可任官
        hours = int((self.main_cfg().get("security") or {}).get(
            "min_join_hours_for_position", 0) or 0)
        if position and hours > 0 and not await self._position_tenure_ok(
                int(f["id"]), str(target_user), hours):
            raise StrategyError(f"入盟不足 {hours} 小时，暂不可担任官职（观察期）")
        await self.upsert_member(f["id"], str(target_user), position=position)
        await self._commit()
        cn = POSITION_CN.get(position, position)
        if not position:
            cn = "白身"
        return {"ok": True, "msg": f"已任命 {target_user} 为「{cn}」"}

    async def faction_appoint_char(self, user_id: str, char_id: int,
                                   position: str = "", node_id: int = 0) -> dict:
        """角色级官职任免（§4.5）：官职随角色，开放档位随势力据点数量。

        太守已改为据点级职务，不再走角色官职接口；``node_id`` 仅为旧前端兼容。
        """
        world, p = await self._require_player(user_id)
        f = await self._governance_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader", "deputy"))
        c = await self.get_char(int(char_id))
        ids = await self._alliance_ids(int(world["id"]), f)
        if not c or int(c.get("faction_id") or 0) not in ids:
            raise StrategyError("只能任免本势力/联盟内的角色")
        if c.get("status") in ("dead", "captive", "transfer") or \
                R.injury_state(c.get("injury", 0)) == "dead":
            raise StrategyError("该角色已陨落/被俘/押运中，无法任免")
        # §18.4 观察期：角色主人入盟不足 min_join_hours_for_position 不可任官
        hours = int((self.main_cfg().get("security") or {}).get(
            "min_join_hours_for_position", 0) or 0)
        owner_uid = str(c.get("owner_user_id") or "")
        if position and hours > 0 and owner_uid and \
                not await self._position_tenure_ok(int(f["id"]), owner_uid, hours):
            raise StrategyError(f"该角色主人入盟不足 {hours} 小时，"
                                "暂不可担任官职（观察期）")
        position = str(position or "").strip()
        if position == "taishou":
            raise StrategyError("太守已改为据点级职务，请在据点抽屉任免")
        if position and position not in R.APPOINTABLE_POSITIONS:
            raise StrategyError(
                "官职无效（势力主/副主是玩家治理身份，不属于角色官职）")
        node_count = await self._governance_node_count(int(world["id"]), f)
        avail = R.available_positions(node_count)
        if position and position not in avail:
            need = R.position_unlock_nodes(position)
            raise StrategyError(
                f"据点不足：{R.POSITION_NAMES.get(position, position)}需 {need} 座据点"
                f"（当前 {node_count} 座）")
        if position:
            holders = [x for x in await self._alliance_chars(int(world["id"]), f)
                       if str(x.get("appointed_position") or "") == position
                       and int(x["id"]) != int(c["id"])]
            cap = int(avail.get(position, 1))
            if len(holders) >= cap:
                raise StrategyError(
                    f"{R.POSITION_NAMES.get(position, position)}名额已满（{cap}）")
        # 角色官职与据点太守相互独立；历史字段 appointed_node_id 不再承载太守。
        c["appointed_position"] = position
        c["appointed_node_id"] = 0
        await self.save_char(c)
        await self._commit()
        if not position:
            return {"ok": True, "msg": f"{c['name']} 已卸任官职"}
        return {"ok": True,
                "msg": f"已任命 {c['name']} 为「"
                       f"{R.POSITION_NAMES.get(position, position)}」"}

    async def transfer_leader(self, user_id: str, target_user: str) -> dict:
        world, p = await self._require_player(user_id)
        # 只移交玩家自身势力的势力主（联盟盟主势力移交即联盟盟主移交）
        f = await self.get_faction(int(p["faction_id"]))
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader",))
        members = await self.get_members(f["id"])
        target = next((m for m in members if m["user_id"] == str(target_user)), None)
        if not target:
            raise StrategyError("目标不是本势力成员")
        tp = await self.get_player(str(target_user), world["id"])
        if not tp or int(tp.get("faction_id") or 0) != int(f["id"]):
            raise StrategyError(
                "仅可在自身势力内部移交势力主（联盟成员势力请各自移交）")
        await self.upsert_member(f["id"], str(target_user), position="leader")
        await self.upsert_member(f["id"], user_id, position="deputy")
        f["leader_id"] = str(target_user)
        await self.save_faction(f)
        await self._commit()
        is_host = int(f.get("alliance_id") or 0) == int(f["id"])
        title = "盟主" if is_host else "势力主"
        return {"ok": True, "msg": f"{title}之位已移交 {target_user}，你转任副主"}

    async def transfer_node(self, user_id: str, node_id: int,
                            target_faction_id: int) -> dict:
        """联盟内部交接据点归属（保留守军/建筑，仅换所属势力）。

        权限：盟主/副主可调度全联盟据点；成员势力主/副主只能把
        **本势力自己的据点**交给盟友，不能反向把盟友据点划给自己。
        此前统一按盟主名册校验，任何成员国主（默认副主）都能随意
        鲸吞盟友领土。
        """
        world, p = await self._require_player(user_id)
        own = await self.get_faction(int(p.get("faction_id") or 0))
        if not own or own.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        host = await self._alliance_host(own)
        ids = await self._alliance_ids(world["id"], own)
        node = await self.get_node(world["id"], int(node_id))
        if not node:
            raise StrategyError("据点不存在")
        old_owner_id = int(node.get("owner_faction", 0))
        target = await self.get_faction(int(target_faction_id))
        if not target or int(target.get("alive", 0)) != 1:
            raise StrategyError("目标势力不存在")
        if old_owner_id not in ids or int(target["id"]) not in ids:
            raise StrategyError("只能在本联盟成员势力之间交接据点")
        if old_owner_id == int(target["id"]):
            raise StrategyError("该据点已属于目标势力")
        f = host or own
        if int(f["id"]) == int(own["id"]):
            # 盟主势力：可调度联盟内任意据点
            await self._require_position(f, user_id, ("leader", "deputy"))
        else:
            # 成员势力：仅可交出本势力据点，且目标不能是自己以外的
            # “反向接收”（统一禁止，避免变相吞并）
            await self._require_position(own, user_id, ("leader", "deputy"))
            if old_owner_id != int(own["id"]):
                raise StrategyError("仅联盟盟主可跨势力交接据点")
        old_owner = await self.get_faction(old_owner_id)
        node["owner_faction"] = int(target["id"])
        node["governor_char_id"] = 0  # 交接后由新归属势力重新任命太守
        node["siege_state"] = {}
        await self.save_node(node)
        if old_owner and int(old_owner.get("capital_node_id") or 0) == int(node["id"]):
            left = await self._fetchone(
                "SELECT id FROM strategy_nodes WHERE world_id=? AND owner_faction=? "
                "AND id<>? AND type<>'ruin' ORDER BY (type IN ('city','pass','branch')) DESC, id LIMIT 1",
                (world["id"], old_owner_id, int(node["id"])))
            new_cap = int((left or {}).get("id", 0) or 0)
            old_owner["capital_node_id"] = new_cap
            if new_cap <= 0:
                # 交出最后一座据点：必须与城破同口径进入流亡，否则旧势力
                # 会停留在 normal 且 capital=0，可绕过流亡校验在地图任意
                # 无主节点“重建主城”。
                old_owner["faction_state"] = "exiled"
            elif str(old_owner.get("faction_state") or "") == "exiled":
                old_owner["faction_state"] = "normal"
            await self.save_faction(old_owner)
        if not int(target.get("capital_node_id") or 0):
            target["capital_node_id"] = int(node["id"])
            await self.save_faction(target)
        await self._commit()
        return {"ok": True,
                "msg": f"「{node['name']}」已由「{(old_owner or {}).get('name', '?')}」"
                       f"交接给「{target['name']}」"}

    # ------------------------------------------------------------------
    # 国库（§8.4）
    # ------------------------------------------------------------------
    async def treasury_deposit(self, user_id: str, resource: str, amount: int) -> dict:
        world, p = await self._require_player(user_id)
        resource = str(resource)
        amount = int(amount)
        if resource not in ("coin", "grain", "iron") or amount <= 0:
            raise StrategyError("仅支持存入铜钱/粮草/精铁且数量为正")
        if int(p.get(resource, 0)) < amount:
            raise StrategyError(
                f"{R.RESOURCE_NAMES.get(resource, resource)} 不足")
        f = await self._own_faction(p)
        gov = await self._governance_faction(p) or f
        if not f:
            raise StrategyError("势力不存在")
        p[resource] -= amount
        treasury = f.get("treasury") or {}
        treasury[resource] = int(treasury.get(resource, 0)) + amount
        f["treasury"] = treasury
        await self.save_faction(f)
        await self.save_player(p)
        await self.add_contribution(f["id"], user_id, max(1, amount // 10))
        # §6.4 势力任务：共襄国库（共同治理名册下的任务仍由盟主推进）
        await self._faction_task_progress(gov["id"], "donate", amount)
        await self._commit()
        return {"ok": True,
                "msg": f"存入本势力国库 {R.RESOURCE_NAMES.get(resource, resource)}"
                       f" ×{amount}"}

    async def treasury_withdraw(self, user_id: str, resource: str, amount: int) -> dict:
        world, p = await self._require_player(user_id)
        resource = str(resource)
        amount = int(amount)
        if resource not in ("coin", "grain", "iron") or amount <= 0:
            raise StrategyError("仅支持提取铜钱/粮草/精铁且数量为正")
        f = await self._own_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        pos = await self._require_position(f, user_id, ("leader", "deputy"))
        treasury = f.get("treasury") or {}
        have = int(treasury.get(resource, 0))
        if have < amount:
            raise StrategyError(
                f"国库 {R.RESOURCE_NAMES.get(resource, resource)} "
                f"不足（现有 {have}）")
        if pos == "deputy":
            pct = float((self.main_cfg().get("faction") or {}).get(
                "treasury_deputy_pct", 0.3))
            # 「单次上限 30%」若不限次数等于没有上限：副主可以连续提取
            # 30%/21%/15%…把国库抽干。这里按「每日累计」计算额度。
            day = _now() // 86400
            data = world.get("data") or {}
            draws = data.get("treasury_draw")
            if not isinstance(draws, dict):
                draws = {}
            rec = draws.get(str(int(f["id"]))) or {}
            used = int(rec.get("used", 0) or 0) if int(rec.get("day", 0) or 0) == day else 0
            base = have + used
            cap_total = int(base * pct)
            if amount + used > cap_total:
                raise StrategyError(
                    f"副主今日累计提取上限为国库的 {int(pct * 100)}%"
                    f"（本日剩余 {max(0, cap_total - used)}）")
            draws[str(int(f["id"]))] = {"day": day, "used": used + amount}
            data["treasury_draw"] = draws
            world["data"] = data
            await self.save_world(world)
        treasury[resource] = have - amount
        f["treasury"] = treasury
        p[resource] = int(p.get(resource, 0)) + amount
        await self.save_faction(f)
        await self.save_player(p)
        await self._commit()
        return {"ok": True,
                "msg": f"从本势力国库提取 {R.RESOURCE_NAMES.get(resource, resource)}"
                       f" ×{amount}"}

    # ------------------------------------------------------------------
    # 势力科技（§8.4）
    # ------------------------------------------------------------------
    async def tech_research(self, user_id: str, line: str,
                            char_id: int = 0, node_id: str = "") -> dict:
        world, p = await self._require_player(user_id)
        tcfg = self.main_cfg().get("tech") or {}
        line = str(line)
        if line not in (tcfg.get("lines") or list(R.TECH_LINES)):
            raise StrategyError(
                "科技线无效（" + "/".join(R.TECH_NAMES.get(x, x)
                                          for x in R.TECH_LINES) + "）")
        f = await self._own_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        # v4.5.0：科技的国库费用与进度都只作用于本势力，官职校验也必须
        # 用本势力名册。此前校验联盟盟主名册（gov），盟主调整联盟名册把
        # 成员势力主降为白身后，成员即使仍是自家势力主也无法研究自家
        # 科技、花自家国库，表现为「结盟后拿不到本势力国库资源」。
        await self._require_position(f, user_id, OFFICER_POSITIONS)
        tech = f.get("tech") or {}
        if tech.get("pending"):
            pend = tech["pending"]
            pend_name = (pend.get("node_name") or
                         R.TECH_NAMES.get(pend.get("line"), pend.get("line")))
            raise StrategyError(
                f"已有研究进行中（{pend_name}，"
                f"剩余 {(int(pend.get('finish_at', 0)) - _now() + 59) // 60} 分钟）")
        # 主脉/支脉统一解析：前置、等级上限、费用与基础耗时
        target = R.tech_research_target(tech, line, str(node_id or ""), tcfg)
        if target.get("error"):
            raise StrategyError(str(target["error"]))
        node = target["node"]
        node_name = str(node.get("name") or node.get("id") or "")
        kind = str(target.get("kind") or "spine")
        lvl = int(target.get("level", 0) or 0)
        cost_coin = int(target.get("cost_coin", 0) or 0)
        cost_iron = int(target.get("cost_iron", 0) or 0)
        base_hours = float(target.get("base_hours", 8) or 8)
        treasury = f.get("treasury") or {}
        have_coin = int(treasury.get("coin", 0) or 0)
        have_iron = int(treasury.get("iron", 0) or 0)
        if have_coin < cost_coin or have_iron < cost_iron:
            # 研究固定从势力国库扣费（个人钱包不参与）；报错同时给出当前余额，
            # 避免玩家把顶栏个人铜钱/精铁当成国库余额而误判为数值判断出错。
            raise StrategyError(
                f"国库不足：研究 L{lvl + 1}（{node_name}）需铜钱 {cost_coin} / 精铁 {cost_iron}，"
                f"当前国库铜钱 {have_coin} / 精铁 {have_iron}"
                "（研究从势力国库扣费，个人资源请先「捐入国库」）")
        # §8.4 指派 1 名角色「研究」：占用其时间，智谋/政务加速
        # 科技为本势力独立进度，只允许本势力角色主研。
        c = await self._pick_research_char(world["id"], f, int(char_id or 0),
                                           {int(f["id"])})
        stat = max(int((c.get("stats") or {}).get("wisdom", 0)),
                   int((c.get("stats") or {}).get("admin", 0)))
        # 耗时：基础小时 × 主属性加速（以主研角色的智谋/政务计算）
        hours = R.tech_research_hours(base_hours, stat,
                                      float(tcfg.get("stat_speed_divisor", 10)),
                                      float(tcfg.get("stat_speed_pct", 0.05)))
        # 研究任务产出的科技点可抵扣研究时长（最多抵 50%，1 点 = 1 分钟）
        points = max(0, int(tech.get("points", 0) or 0))
        max_cut_min = int(base_hours * 60 * 0.5)
        cut_min = min(points, max_cut_min)
        if cut_min > 0:
            hours = max(0.5, hours - cut_min / 60.0)
            tech["points"] = points - cut_min
        treasury["coin"] = int(treasury.get("coin", 0)) - cost_coin
        treasury["iron"] = int(treasury.get("iron", 0)) - cost_iron
        f["treasury"] = treasury
        tech["pending"] = {"line": line, "node_id": str(node.get("id") or ""),
                           "node_name": node_name, "kind": kind,
                           "by": user_id,
                           "finish_at": _now() + int(hours * 3600),
                           "char_id": int(c["id"]), "char_name": c.get("name", ""),
                           "exp": int(tcfg.get("research_exp", 60)) + 20 * lvl}
        f["tech"] = tech
        c["status"] = "research"
        await self.save_char(c)
        await self.save_faction(f)
        await self._commit()
        await self._audit(user_id, "tech_research",
                          {"faction_id": int(f["id"]), "line": line,
                           "node_id": str(node.get("id") or ""), "kind": kind,
                           "char_id": int(c["id"]), "hours": round(hours, 2)})
        return {"ok": True,
                "msg": f"开始研究 {node_name} L{lvl + 1}"
                       f"（{hours:.1f}h，主研 {c['name']}，"
                       f"扣本势力国库铜钱 {cost_coin}/精铁 {cost_iron}"
                       + (f"，科技点抵扣 {cut_min} 分钟" if cut_min > 0 else "")
                       + "）"}

    async def _pick_research_char(self, world_id: int, f: dict,
                                  char_id: int = 0,
                                  faction_ids: Optional[set[int]] = None) -> dict:
        """§8.4 研究角色：显式指派优先，否则自动挑智谋最高且 idle 者。"""
        ids = faction_ids or await self._alliance_ids(world_id, f) or {int(f["id"])}
        if int(char_id or 0) > 0:
            c = await self.get_char(int(char_id))
            if not c or int(c.get("faction_id") or 0) not in ids:
                raise StrategyError("只能指派本势力/联盟内的角色研究")
            if c.get("status") in ("dead", "captive", "army", "task", "wander") or \
                    R.injury_state(c.get("injury", 0)) == "dead":
                raise StrategyError(
                    f"{c['name']} 当前无法研究（"
                    f"{R.CHAR_STATUS_NAMES.get(c.get('status'), c.get('status'))}）")
            if c.get("status") not in ("idle", "research"):
                raise StrategyError(f"{c['name']} 正在执行其他任务，无法研究")
            return c
        best: Optional[tuple[int, dict]] = None
        for fid in ids:
            for c in await self.get_chars(faction_id=int(fid), world_id=world_id,
                                          status="idle"):
                if R.injury_state(c.get("injury", 0)) == "dead":
                    continue
                st = c.get("stats") or {}
                score = max(int(st.get("wisdom", 0)), int(st.get("admin", 0)))
                if best is None or score > best[0]:
                    best = (score, c)
        if best is None:
            raise StrategyError("暂无可指派的空闲角色（研究需 idle 状态角色）")
        return best[1]

    async def _best_research_stat(self, f: dict, world_id: int) -> int:
        best = 50
        ids = await self._alliance_ids(world_id, f)
        for fid in ids or {int(f["id"])}:
            for c in await self.get_chars(faction_id=int(fid), world_id=world_id):
                st = c.get("stats") or {}
                best = max(best, int(st.get("wisdom", 0)), int(st.get("admin", 0)))
        return best

    async def _step_tech(self, world: dict):
        """tick：到期的科技研究完成（主脉 +1 / 支脉 +1，并同步给联盟成员）。"""
        rows = await self._fetchall(
            "SELECT * FROM strategy_factions WHERE world_id=? AND alive=1", (world["id"],))
        # v4.3.0：结算与入口同口径钳制到配置上限（防 levels>5 时无限研究）
        tcfg = self.main_cfg().get("tech") or {}
        max_lvl = R.tech_line_max(tcfg)
        node_map = R.tech_node_map(tcfg)
        changed = False
        for row in rows:
            f = self._decode("strategy_factions", row)
            # 科技为各势力独立进度：联盟成员也在此结算自己的研究。
            tech = f.get("tech") or {}
            pend = tech.get("pending") or {}
            if pend and int(pend.get("finish_at", 0)) <= _now():
                line = str(pend.get("line", ""))
                if line in R.TECH_LINES:
                    node_id = str(pend.get("node_id") or "")
                    node = node_map.get(node_id) if node_id else None
                    kind = str(pend.get("kind") or
                               (node.get("kind") if node else "spine"))
                    if node and kind == "branch":
                        nodes = R.tech_nodes(tech)
                        cap = int(node.get("max") or
                                  tcfg.get("branch_max_level") or 5)
                        new_lv = min(max(1, cap),
                                     int(nodes.get(node_id, 0) or 0) + 1)
                        nodes[node_id] = new_lv
                        tech["nodes"] = nodes
                        label = str(node.get("name") or
                                    pend.get("node_name") or node_id)
                    else:
                        # 旧 pending（无 node_id）与主脉走同一分支
                        tech[line] = R.tech_level(tech, line, max_lvl) + 1
                        new_lv = tech[line]
                        label = R.TECH_NAMES.get(line, line)
                    tech.pop("pending", None)
                    f["tech"] = tech
                    await self.save_faction(f)
                    changed = True
                    # §8.4 研究完成：主研角色回到 idle 并结算经验
                    cid = int(pend.get("char_id", 0) or 0)
                    if cid:
                        c = await self.get_char(cid)
                        if c:
                            if c.get("status") == "research":
                                c["status"] = "idle"
                            c["exp"] = int(c.get("exp", 0) or 0) + \
                                int(pend.get("exp", 0) or 0)
                            await self.save_char(c)
                    await self._notify_faction_event(
                        world, int(f["id"]),
                        {"event": "tech_done", "line": line,
                         "node_id": node_id,
                         "text": f"科技 {label} 研究完成（L{new_lv}）"
                                 + (f"，{pend.get('char_name', '')} 归队"
                                    if pend.get("char_name") else "")})
                    logger.info(f"[strategy] 势力#{f['id']} 科技 {label} → L{new_lv}")
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
        key = str(int(other_id))
        entry = f_dip.setdefault(key, {})
        if rel.startswith("propose_"):
            # 提案只是临时视图：先把既有关系（含 score/war_at/until）完整
            # 备份到 prev，接受/拒绝/过期时再还原，不能直接覆盖或 pop。
            if not isinstance(entry.get("prev"), dict):
                entry["prev"] = {k: v for k, v in entry.items() if k != "prev"}
        else:
            entry.pop("prev", None)
        entry.update({"rel": rel, "until": int(until or 0),
                      "war_at": int(war_at or 0)})
        return f_dip

    @staticmethod
    def _restore_proposal(entry: dict) -> Optional[dict]:
        """把提案条目还原为提案前快照；返回 None 表示应删除该条目。

        窗口期内产生的好感/赠礼计数要保留（它们写入的是当前条目），
        否则玩家在 48h 内送礼提升的好感会随提案结束一起消失。
        """
        prev = entry.get("prev") if isinstance(entry, dict) else None
        if not isinstance(prev, dict):
            return None
        restored = dict(prev)
        for k in ("score", "gift_day", "gift_gain", "trade_day", "trade_gain"):
            if k in entry:
                restored[k] = entry[k]
        return restored

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

    # ------------------------------------------------------------------
    # 好感度（NPC 外交，§8.3/§9.2）
    #   diplomacy entry["score"] 即好感度；进入/退出战争、赠礼、通商、
    #   结盟/停战/和谈、撕毁条约、以及 NPC 盟友/敌人的反应都会增减。
    # ------------------------------------------------------------------
    def _favor_cfg(self) -> dict:
        dcfg = self.main_cfg().get("diplomacy") or {}
        return dcfg.get("favor") or {}

    @staticmethod
    def _bump_favor_entry(dip: dict, other_id: int, delta: int,
                          fcfg: dict) -> int:
        lo = int(fcfg.get("min", R.FAVOR_MIN))
        hi = int(fcfg.get("max", R.FAVOR_MAX))
        entry = dip.setdefault(str(int(other_id)), {"rel": R.REL_NEUTRAL})
        entry["score"] = max(lo, min(hi, int(entry.get("score", 0) or 0) +
                                     int(delta)))
        return int(entry["score"])

    def _apply_favor_delta(self, f: dict, tf: dict, delta: int,
                           fcfg: Optional[dict] = None) -> int:
        """双向修改两势力好感（就地写入 f/tf，由调用方统一落盘）。"""
        if not delta:
            return R.favor_of(f.get("diplomacy") or {}, tf["id"])
        cfg = fcfg or self._favor_cfg()
        if not cfg.get("enabled", True):
            return R.favor_of(f.get("diplomacy") or {}, tf["id"])
        f_dip = f.setdefault("diplomacy", {})
        t_dip = tf.setdefault("diplomacy", {})
        score = self._bump_favor_entry(f_dip, tf["id"], delta, cfg)
        self._bump_favor_entry(t_dip, f["id"], delta, cfg)
        return score

    async def _add_favor(self, fid: int, other_fid: int, delta: int,
                         reason: str = "") -> int:
        """独立地给两个势力加好感（自动落到联盟盟主势力）。"""
        if int(fid) <= 0 or int(other_fid) <= 0 or int(fid) == int(other_fid):
            return 0
        cfg = self._favor_cfg()
        if not cfg.get("enabled", True) or not delta:
            return 0
        f = await self._alliance_host(await self.get_faction(int(fid)))
        tf = await self._alliance_host(await self.get_faction(int(other_fid)))
        if not f or not tf or int(f["id"]) == int(tf["id"]):
            return 0
        score = self._apply_favor_delta(f, tf, int(delta), cfg)
        await self.save_faction(f)
        await self.save_faction(tf)
        await self._sync_alliance_meta(f)
        await self._sync_alliance_meta(tf)
        await self._commit()
        if reason:
            logger.info(f"[strategy] 好感 {f['id']}→{tf['id']} "
                        f"{int(delta):+d} → {score}（{reason}）")
        return score

    async def _favor_third_party(self, world_id: int, actor_f: dict,
                                 target_f: dict, kind: str,
                                 fcfg: Optional[dict] = None):
        """第三方反应：与目标结盟者厌恶、与目标交恶者叫好。"""
        cfg = fcfg or self._favor_cfg()
        if not cfg.get("enabled", True):
            return
        ally_cost = int(cfg.get("third_party_ally_cost", -10))
        enemy_gain = int(cfg.get("third_party_enemy_gain", 8))
        ally_min = int(cfg.get("ally_min", 60))
        war_favor = int(cfg.get("npc_war_favor", -50))
        if kind not in ("war", "ally"):
            return
        rows = await self._fetchall(
            "SELECT * FROM strategy_factions WHERE world_id=? AND alive=1 "
            "AND kind='npc'", (int(world_id),))
        changed = False
        for row in rows:
            c = self._decode("strategy_factions", row)
            c = await self._alliance_host(c)
            if int(c["id"]) in (int(actor_f["id"]), int(target_f["id"])):
                continue
            rel_target = R.relation_of(c.get("diplomacy") or {},
                                       int(target_f["id"]), _now())
            favor_target = R.favor_of(c.get("diplomacy") or {},
                                      int(target_f["id"]))
            delta = 0
            if kind == "war":
                if rel_target == R.REL_ALLY or favor_target >= ally_min:
                    delta = ally_cost
                elif rel_target == R.REL_WAR or favor_target <= war_favor:
                    delta = enemy_gain
            elif kind == "ally" and (rel_target == R.REL_WAR or
                                     favor_target <= war_favor):
                delta = ally_cost
            if not delta:
                continue
            actor = await self._alliance_host(
                await self.get_faction(int(actor_f["id"])))
            if not actor:
                continue
            self._apply_favor_delta(actor, c, int(delta), cfg)
            await self.save_faction(c)
            await self.save_faction(actor)
            await self._sync_alliance_meta(actor)
            changed = True
        if changed:
            await self._commit()

    async def _favor_on_war(self, actor_f: dict, target_f: dict,
                            surprise: bool = False):
        """宣战 / 突袭：双方好感下降，NPC 盟友与敌人按立场表态。"""
        cfg = self._favor_cfg()
        if not cfg.get("enabled", True):
            return
        actor = await self._alliance_host(
            await self.get_faction(int(actor_f["id"])))
        target = await self._alliance_host(
            await self.get_faction(int(target_f["id"])))
        if not actor or not target or int(actor["id"]) == int(target["id"]):
            return
        key = "surprise_attack_cost" if surprise else "war_declare_cost"
        delta = int(cfg.get(key, -20))
        if surprise:
            # 未宣战突袭直接进入战争态，避免连续进攻反复扣好感
            self._set_relation(actor.setdefault("diplomacy", {}),
                               int(target["id"]), R.REL_WAR, war_at=_now())
            self._set_relation(target.setdefault("diplomacy", {}),
                               int(actor["id"]), R.REL_WAR, war_at=_now())
        self._apply_favor_delta(actor, target, delta, cfg)
        await self.save_faction(actor)
        await self.save_faction(target)
        await self._sync_alliance_meta(actor)
        await self._sync_alliance_meta(target)
        await self._commit()
        await self._favor_third_party(int(actor["world_id"]), actor,
                                      target, "war", cfg)

    async def _step_favor_decay(self, world: dict):
        """每日好感向 0 缓慢回落（热情与仇恨都会随时间淡化）。"""
        cfg = self._favor_cfg()
        per_day = int(cfg.get("decay_per_day", 0) or 0)
        if not cfg.get("enabled", True) or per_day <= 0:
            return
        day = _now() // 86400
        data = world.get("data") or {}
        if int(data.get("favor_decay_day", 0) or 0) >= day:
            return
        data["favor_decay_day"] = day
        world["data"] = data
        rows = await self._fetchall(
            "SELECT * FROM strategy_factions WHERE world_id=? AND alive=1 "
            "AND kind='npc'", (int(world["id"]),))
        changed = False
        for row in rows:
            f = self._decode("strategy_factions", row)
            dip = f.get("diplomacy") or {}
            dirty = False
            for v in dip.values():
                if not isinstance(v, dict):
                    continue
                score = int(v.get("score", 0) or 0)
                if score > 0:
                    v["score"] = max(0, score - per_day)
                elif score < 0:
                    v["score"] = min(0, score + per_day)
                else:
                    continue
                dirty = changed = True
            if dirty:
                f["diplomacy"] = dip
                await self.save_faction(f)
        if changed:
            await self.save_world(world)
            await self._commit()

    async def seed_npc_diplomacy(self, world_id: int, factions: list[dict],
                                 rng: random.Random):
        """按性格给 NPC 之间预置外交关系与初始好感（只补缺失的双边记录）。"""
        cfg = self._favor_cfg()
        ids = [int(x["id"]) for x in factions]
        if len(ids) < 2:
            return
        marks = ",".join("?" for _ in ids)
        rows = await self._fetchall(
            f"SELECT id, diplomacy_json FROM strategy_factions WHERE id IN ({marks})",
            tuple(ids))
        dips: dict[int, dict] = {}
        for row in rows:
            try:
                dips[int(row["id"])] = json.loads(row["diplomacy_json"] or "{}")
            except (TypeError, ValueError):
                dips[int(row["id"])] = {}
        changed = False
        for i in range(len(factions)):
            for j in range(i + 1, len(factions)):
                a, b = factions[i], factions[j]
                aid, bid = int(a["id"]), int(b["id"])
                da = dips.setdefault(aid, {})
                db = dips.setdefault(bid, {})
                if da.get(str(bid)) or db.get(str(aid)):
                    continue
                rel, favor = R.npc_seed_relation(
                    rng, a.get("personality"), b.get("personality"), cfg)
                entry = {"rel": rel, "score": int(favor)}
                if rel == R.REL_WAR:
                    entry["war_at"] = 0  # NPC 之间直接进入战争
                da[str(bid)] = dict(entry)
                db[str(aid)] = dict(entry)
                changed = True
        if not changed:
            return
        for fid, dip in dips.items():
            await self._execute(
                "UPDATE strategy_factions SET diplomacy_json=? WHERE id=?",
                (json.dumps(dip, ensure_ascii=False), int(fid)))
        await self._commit()
        logger.info(f"[strategy] 世界#{world_id} NPC 外交预置完成"
                    f"（{len(factions)} 家）")

    async def ensure_npc_diplomacy(self, world: dict):
        """旧存档补齐：为缺失的 NPC 双边关系补种（不覆盖已有外交）。"""
        rows = await self._fetchall(
            "SELECT id, personality_json, diplomacy_json FROM strategy_factions "
            "WHERE world_id=? AND kind='npc' AND alive=1", (int(world["id"]),))
        if len(rows) < 2:
            return
        factions = []
        for row in rows:
            try:
                pers = json.loads(row["personality_json"] or "{}")
            except (TypeError, ValueError):
                pers = {}
            factions.append({"id": int(row["id"]), "personality": pers})
        rng = self._rng("npc_diplomacy_seed", int(world["id"]))
        await self.seed_npc_diplomacy(int(world["id"]), factions, rng)


    async def diplomacy_propose(self, user_id: str, target_fid: int, kind: str,
                                indemnity: int = 0) -> dict:
        """外交提案：war（宣战，立即进入备战）/ truce / ally / peace（和谈赔款）。"""
        world, p = await self._require_player(user_id)
        dcfg = self.main_cfg().get("diplomacy") or {}
        kind = str(kind)
        if kind not in ("war", "truce", "ally", "peace"):
            raise StrategyError("外交类型无效（war/truce/ally/peace）")
        f = await self._governance_faction(p)
        tf = await self.get_faction(int(target_fid))
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        if not tf or not int(tf.get("alive", 0)):
            raise StrategyError("目标势力无效")
        tf = await self._alliance_host(tf)
        if int(tf["id"]) == int(f["id"]):
            raise StrategyError("目标势力无效")
        # 外交代表权属于官职（主公/副主/司徒/太尉/祭酒），普通成员不得
        # 代表势力宣战或提案（此前完全无职位校验）。
        await self._require_position(f, user_id, OFFICER_POSITIONS)
        now = _now()
        f_dip = f.get("diplomacy") or {}
        t_dip = tf.get("diplomacy") or {}
        cur = R.relation_of(f_dip, tf["id"], now)
        fcfg = dcfg.get("favor") or {}
        is_npc = str(tf.get("kind") or "") == "npc"
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
            # 宣战/背盟：双方好感下降，NPC 盟友与敌人按立场表态
            self._apply_favor_delta(f, tf, int(fcfg.get("war_declare_cost", -25)), fcfg)
            await self.save_faction(f)
            await self.save_faction(tf)
            await self._sync_alliance_meta(f)
            await self._sync_alliance_meta(tf)
            await self._commit()
            await self._favor_third_party(world["id"], f, tf, "war", fcfg)
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
        # 对 NPC 的外交行为设有好感门槛（宣战无门槛）
        if is_npc and kind in ("ally", "truce", "peace"):
            need = R.favor_gate(kind, 0, fcfg)
            favor = R.favor_of(f_dip, tf["id"])
            if need is not None and favor < need:
                raise StrategyError(
                    f"「{tf['name']}」对你好感不足（{favor}，"
                    f"{self._kind_cn(kind)}需 {need}，当前{R.favor_name(favor)}）"
                    f"，可先赠礼或与其通商提升好感")
        # --- 提案（对方为玩家 → 等待回应；NPC → 立即掷点）---
        if tf.get("kind") == "npc":
            accepted = self._npc_diplomacy_roll(world, f, tf, kind, indemnity, dcfg)
            if accepted:
                await self._apply_diplomacy_deal(f, tf, kind, indemnity, dcfg)
                return {"ok": True, "msg": f"「{tf['name']}」接受了{self._kind_cn(kind)}"}
            return {"ok": True, "msg": f"「{tf['name']}」拒绝了{self._kind_cn(kind)}"}
        # 已有待决提案时不再覆盖：否则会叠加 prev 快照并丢失原关系
        for dip, other in ((f_dip, tf["id"]), (t_dip, f["id"])):
            if str((dip.get(str(int(other))) or {}).get("rel", "")).startswith(
                    "propose_"):
                raise StrategyError("已有待回应的外交提案，请等待对方答复或过期")
        expire = now + 48 * 3600
        self._set_relation(t_dip, f["id"], f"propose_{kind}", until=expire)
        t_dip[str(f["id"])]["from"] = int(f["id"])
        t_dip[str(f["id"])]["indemnity"] = int(indemnity)
        self._set_relation(f_dip, tf["id"], f"propose_{kind}_out", until=expire)
        f["diplomacy"] = f_dip
        tf["diplomacy"] = t_dip
        await self.save_faction(f)
        await self.save_faction(tf)
        await self._sync_alliance_meta(f)
        await self._sync_alliance_meta(tf)
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
        fcfg = dcfg.get("favor") or {}
        favor = R.favor_of(tf.get("diplomacy") or {}, f["id"])
        if kind == "truce":
            return rng.random() < max(0.35, min(0.95, 0.7 + favor / 250.0))
        if kind == "ally":
            charm = 50
            base = float(dcfg.get("ally_base_chance", 40))
            chance = R.ally_accept_chance(
                charm, int(f.get("reputation", 0)), int(tf.get("reputation", 0)),
                base=base, charm_div=float(dcfg.get("ally_charm_divisor", 4)),
                rep_div=float(dcfg.get("ally_rep_divisor", 2)),
                favor=favor,
                favor_div=float(fcfg.get("ally_favor_divisor", 2)))
            return rng.random() * 100 < chance
        if kind == "peace":
            cap = max(1, int(int((f.get("treasury") or {}).get("coin", 0)) *
                             float(dcfg.get("peace_indemnity_cap_pct", 0.2))))
            chance = min(0.95, max(0.1, 0.3 + 0.6 * int(indemnity) / cap
                                   + favor / 300.0))
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
        elif kind == "war":
            prep = int(dcfg.get("war_prep_hours", 24)) * 3600
            self._set_relation(f_dip, tf["id"], R.REL_WAR, war_at=now + prep)
            self._set_relation(t_dip, f["id"], R.REL_WAR, war_at=now + prep)
        fcfg = dcfg.get("favor") or {}
        favor_key = {"war": "war_declare_cost", "truce": "truce_sign_gain",
                     "peace": "peace_sign_gain", "ally": "ally_sign_gain"}.get(kind)
        if favor_key:
            self._apply_favor_delta(f, tf, int(fcfg.get(favor_key, 0)), fcfg)
        f["diplomacy"] = f_dip
        tf["diplomacy"] = t_dip
        await self.save_faction(f)
        await self.save_faction(tf)
        await self._sync_alliance_meta(f)
        await self._sync_alliance_meta(tf)
        await self._commit()
        if kind in ("war", "ally"):
            await self._favor_third_party(int(f["world_id"]), f, tf, kind, fcfg)
        if kind == "ally":
            for uid in (f.get("leader_id"), tf.get("leader_id")):
                if uid:
                    await self._award_achievement(uid, "social_ally")

    async def diplomacy_gift(self, user_id: str, target_fid: int,
                             resource: str = "coin", amount: int = 0) -> dict:
        """向 NPC 势力赠礼：按价值换算好感，每日有上限。"""
        world, p = await self._require_player(user_id)
        fcfg = self._favor_cfg()
        if not fcfg.get("enabled", True):
            raise StrategyError("当前世界未开放好感度系统")
        f = await self._governance_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, OFFICER_POSITIONS)
        tf = await self.get_faction(int(target_fid))
        if not tf or not int(tf.get("alive", 0)):
            raise StrategyError("目标势力无效")
        if str(tf.get("kind") or "") != "npc":
            raise StrategyError("赠礼仅可对 NPC 势力发起")
        tf = await self._alliance_host(tf)
        resource = str(resource)
        amount = int(amount)
        if resource not in ("coin", "grain", "iron"):
            raise StrategyError("仅可赠送铜钱/粮草/精铁")
        if amount <= 0:
            raise StrategyError("赠礼数量须为正")
        if int(p.get(resource, 0)) < amount:
            raise StrategyError(
                f"{R.RESOURCE_NAMES.get(resource, resource)} "
                f"不足（现有 {int(p.get(resource, 0))}）")
        values = fcfg.get("gift_resource_value") or {}
        value = amount * float(values.get(resource, 1))
        points = R.favor_gift_points(value, fcfg)
        if points <= 0:
            per = int(fcfg.get("gift_coin_per_point", 800))
            raise StrategyError(f"赠礼价值过低（约 {per} 币 = 1 点好感）")
        f_dip = f.setdefault("diplomacy", {})
        entry = f_dip.setdefault(str(int(tf["id"])),
                                 {"rel": R.REL_NEUTRAL})
        day = _now() // 86400
        if int(entry.get("gift_day", 0) or 0) != day:
            entry["gift_day"] = day
            entry["gift_gain"] = 0
        cap = max(0, int(fcfg.get("gift_daily_cap", 40)))
        gained = int(entry.get("gift_gain", 0) or 0)
        if gained >= cap:
            raise StrategyError(f"今日对「{tf['name']}」的赠礼好感已达上限（{cap}）")
        points = min(points, cap - gained)
        entry["gift_gain"] = gained + points
        p[resource] = int(p.get(resource, 0)) - amount
        score = self._apply_favor_delta(f, tf, points, fcfg)
        await self.save_player(p)
        await self.save_faction(f)
        await self.save_faction(tf)
        await self._sync_alliance_meta(f)
        await self._sync_alliance_meta(tf)
        await self._commit()
        return {"ok": True,
                "favor": int(score), "favor_name": R.favor_name(score),
                "msg": f"🎁 向「{tf['name']}」赠送 "
                       f"{R.RESOURCE_NAMES.get(resource, resource)} ×{amount}，"
                       f"好感 +{points}（现为 {score}·{R.favor_name(score)}）"}

    async def add_trade_favor(self, fid: int, seller_fid: int) -> int:
        """与 NPC 势力通商（坊市成交）后提升好感，每日有上限。"""
        fcfg = self._favor_cfg()
        if not fcfg.get("enabled", True):
            return 0
        f = await self._alliance_host(await self.get_faction(int(fid)))
        tf = await self.get_faction(int(seller_fid))
        if not f or not tf or int(tf["id"]) == int(f["id"]):
            return 0
        if str(tf.get("kind") or "") != "npc" or not int(tf.get("alive", 0)):
            return 0
        tf = await self._alliance_host(tf)
        f_dip = f.setdefault("diplomacy", {})
        entry = f_dip.setdefault(str(int(tf["id"])),
                                 {"rel": R.REL_NEUTRAL})
        day = _now() // 86400
        if int(entry.get("trade_day", 0) or 0) != day:
            entry["trade_day"] = day
            entry["trade_gain"] = 0
        cap = max(0, int(fcfg.get("trade_daily_cap", 10)))
        gained = int(entry.get("trade_gain", 0) or 0)
        if gained >= cap:
            return 0
        points = min(max(1, int(fcfg.get("trade_gain", 1))), cap - gained)
        entry["trade_gain"] = gained + points
        score = self._apply_favor_delta(f, tf, points, fcfg)
        await self.save_faction(f)
        await self.save_faction(tf)
        await self._sync_alliance_meta(f)
        await self._sync_alliance_meta(tf)
        await self._commit()
        return int(score)

    async def diplomacy_respond(self, user_id: str, from_fid: int,
                                accept: bool) -> dict:
        world, p = await self._require_player(user_id)
        dcfg = self.main_cfg().get("diplomacy") or {}
        f = await self._governance_faction(p)
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
        tf = await self._alliance_host(tf)
        kind = rel[len("propose_"):]
        if not accept:
            self._clear_proposals(f, tf)
            await self.save_faction(f)
            await self.save_faction(tf)
            await self._sync_alliance_meta(f)
            await self._sync_alliance_meta(tf)
            await self._commit()
            return {"ok": True, "msg": f"已拒绝「{tf['name']}」的{self._kind_cn(kind)}提案"}
        if kind == "ally":
            cap_ally = int(dcfg.get("ally_max", 3))
            if not R.can_ally_count(f.get("diplomacy") or {}, _now(), cap_ally) or \
                    not R.can_ally_count(tf.get("diplomacy") or {}, _now(), cap_ally):
                raise StrategyError("任一方同盟数已达上限")
        self._clear_proposals(f, tf)
        # _apply_diplomacy_deal(f=付款方, tf=收款方)：提案方是赔款支付方，
        # 应答方是收款方。此前参数颠倒，会从接受方国库扣钱给提案方。
        await self._apply_diplomacy_deal(
            tf, f, kind, int(entry.get("indemnity", 0)), dcfg)
        return {"ok": True,
                "msg": f"已接受「{tf['name']}」的{self._kind_cn(kind)}提案"}

    def _clear_proposals(self, f: dict, tf: dict):
        # 结束后还原提案前的关系快照（而非整条 pop），否则停战/同盟与
        # 好感 score 会在提案期间或应答后凭空消失。
        for holder, other_id in ((f, tf["id"]), (tf, f["id"])):
            dip = holder.get("diplomacy") or {}
            key = str(int(other_id))
            entry = dip.get(key)
            if not isinstance(entry, dict):
                continue
            restored = self._restore_proposal(entry)
            if restored:
                dip[key] = restored
            else:
                dip.pop(key, None)
            holder["diplomacy"] = dip

    async def check_truce_violation(self, world: dict, atk_fid: int,
                                    dfn_fid: int) -> bool:
        """进攻前调用：停战/同盟期间动手 → 撕毁声望惩罚并转入战争。返回是否发生撕毁。"""
        if not atk_fid or not dfn_fid or atk_fid == dfn_fid or dfn_fid <= 0:
            return False
        f = await self.get_faction(atk_fid)
        tf = await self.get_faction(dfn_fid)
        if not f or not tf:
            return False
        f = await self._alliance_host(f)
        tf = await self._alliance_host(tf)
        if int(f["id"]) == int(tf["id"]):
            return False
        atk_fid, dfn_fid = int(f["id"]), int(tf["id"])
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
        fcfg = dcfg.get("favor") or {}
        self._apply_favor_delta(f, tf, int(fcfg.get("treaty_break_cost", -30)), fcfg)
        await self.save_faction(f)
        await self.save_faction(tf)
        await self._sync_alliance_meta(f)
        await self._sync_alliance_meta(tf)
        await self._commit()
        await self._favor_third_party(int(world["id"]), f, tf, "war", fcfg)
        logger.info(f"[strategy] 势力#{atk_fid} 撕毁条约进攻 #{dfn_fid}，转入战争")
        return True

    # ------------------------------------------------------------------
    # 俸禄（角色官职 + 据点太守）：每 tick 从国库扣铜钱
    # ------------------------------------------------------------------
    async def _step_salary(self, world: dict):
        """结算角色官职 / 据点太守俸禄；国库不足时停发对应增益。

        - 联盟不共享国库：角色官职俸禄由**角色所属势力**国库发放；
        - 据点太守俸禄由**据点归属势力**国库发放；
        - 按 ``faction.salary.priority`` 依次支付，付不起的条目记入
          ``salary.unpaid_*``，本 tick 对应官职/太守加成一并暂停；
        - 研究科技「仁政」的 ``salary_mult`` 可降低本势力俸禄。
        """
        fcfg = self.main_cfg().get("faction") or {}
        scfg = fcfg.get("salary") or {}
        enabled = bool(scfg.get("enabled", fcfg.get("salary_from_treasury", True)))
        # 默认值与 config/strategy_config.json 保持一致；旧配置缺字段时仍可运行。
        per = {"taishou": 10, "zhonglang": 15, "jijiu": 25,
               "situ": 35, "taiwei": 50}
        for k, v in (scfg.get("per_tick") or {}).items():
            per[str(k)] = max(0, int(v or 0))
        rows = await self._fetchall(
            "SELECT id FROM strategy_factions WHERE world_id=? "
            "AND kind='player' AND alive=1", (world["id"],))
        if not rows:
            return
        factions: dict[int, dict] = {}
        for row in rows:
            f = await self.get_faction(int(row["id"]))
            if f:
                factions[int(f["id"])] = f
        if not enabled or not per:
            # 关闭俸禄后清空历史停发状态，避免旧 unpaid 列表继续屏蔽加成。
            for f in factions.values():
                if f.get("salary"):
                    f["salary"] = {}
                    await self.save_faction(f)
            await self._commit()
            return
        priority = [str(x) for x in
                    (scfg.get("priority") or
                     ("taiwei", "situ", "jijiu", "zhonglang", "taishou"))]
        order = {pos: idx for idx, pos in enumerate(priority)}
        tick_no = int(world.get("tick_no", 0) or 0)
        tcfg = self.main_cfg().get("tech") or {}
        # 角色官职俸禄：各势力只为本势力角色发薪（联盟官职共同治理，
        # 但国库/科技等资源按势力独立，绝不从盟主国库代付）。
        role_items: dict[int, list[dict]] = {}
        for fid, f in factions.items():
            for c in await self._alliance_chars(int(world["id"]), f):
                if int(c.get("faction_id") or 0) != fid:
                    continue
                pos = str(c.get("appointed_position") or "")
                amount = int(per.get(pos, 0))
                if pos in R.APPOINTABLE_POSITIONS:
                    role_items.setdefault(fid, []).append({
                        "kind": "role", "position": pos,
                        "char_id": int(c["id"]), "char_name": str(c.get("name") or ""),
                        "node_id": 0, "node_name": "", "amount": amount,
                        "char_salary_mult": R.char_salary_mult(c),
                    })
        # 据点太守俸禄：据点归属势力发。
        gov_items: dict[int, list[dict]] = {}
        ally_cache: dict[int, set[int]] = {}
        for node in await self.get_nodes(int(world["id"])):
            owner_fid = int(node.get("owner_faction") or 0)
            if owner_fid <= 0 or owner_fid not in factions:
                continue
            gov = await self._valid_node_governor(node)
            if not gov:
                continue
            if owner_fid not in ally_cache:
                ally_cache[owner_fid] = await self._alliance_ids(
                    int(world["id"]), factions[owner_fid])
            if int(gov.get("faction_id") or 0) not in ally_cache[owner_fid]:
                continue
            amount = int(per.get("taishou", 0))
            if amount <= 0:
                continue
            gov_items.setdefault(owner_fid, []).append({
                "kind": "governor", "position": "taishou",
                "char_id": int(gov["id"]), "char_name": str(gov.get("name") or ""),
                "node_id": int(node["id"]), "node_name": str(node.get("name") or ""),
                "amount": amount,
                "char_salary_mult": R.char_salary_mult(gov),
            })
        for fid, f in factions.items():
            old_state = f.get("salary") or {}
            if not role_items.get(fid) and not gov_items.get(fid) and not old_state:
                continue  # 无任官且无历史状态：不必每 tick 写库
            tech = R.tech_effects(f.get("tech") or {}, tcfg=tcfg)
            salary_mult = max(0.0, float(tech.get("salary_mult", 1.0) or 1.0))
            items = list(role_items.get(fid, [])) + list(gov_items.get(fid, []))
            for it in items:
                # 势力科技「仁政」× 角色个性/特技（贪财↑ / 节俭↓）
                it["amount"] = max(0, int(round(
                    int(it["amount"]) * salary_mult *
                    float(it.get("char_salary_mult", 1.0) or 1.0))))
            items.sort(key=lambda it: (
                order.get(str(it.get("position")), len(priority)),
                int(it.get("char_id") or 0),
                int(it.get("node_id") or 0)))
            treasury = dict(f.get("treasury") or {})
            balance = max(0, int(treasury.get("coin", 0) or 0))
            paid_total = 0
            due_total = 0
            for it in items:
                amount = int(it.get("amount", 0))
                due_total += amount
                if amount <= 0:
                    it["paid"] = True
                    continue
                if balance >= amount:
                    balance -= amount
                    paid_total += amount
                    it["paid"] = True
                else:
                    it["paid"] = False
            treasury["coin"] = balance
            f["treasury"] = treasury
            unpaid = [it for it in items if not it.get("paid")]
            state = {
                "tick": tick_no,
                "due": due_total,
                "paid": paid_total,
                "short": max(0, due_total - paid_total),
                "enabled": True,
                "items": items,
                "unpaid_positions": sorted({str(it["position"]) for it in unpaid}),
                "paid_role_char_ids": sorted({
                    int(it["char_id"]) for it in items
                    if it.get("kind") == "role" and it.get("paid")}),
                "unpaid_role_char_ids": sorted({
                    int(it["char_id"]) for it in unpaid
                    if it.get("kind") == "role"}),
                "unpaid_governor_node_ids": sorted({
                    int(it["node_id"]) for it in unpaid
                    if it.get("kind") == "governor"}),
                "unpaid_governor_char_ids": sorted({
                    int(it["char_id"]) for it in unpaid
                    if it.get("kind") == "governor"}),
            }
            f["salary"] = state
            await self.save_faction(f)
            if state["short"] > 0 and not int(old_state.get("short", 0) or 0):
                names = "、".join(
                    R.POSITION_NAMES.get(it["position"], it["position"])
                    + ("·" + it["node_name"] if it.get("node_name") else "")
                    for it in unpaid)
                logger.info(
                    f"[strategy] 势力#{fid} 国库不足：应付俸禄 {due_total}，"
                    f"已付 {paid_total}，停发 {names or '部分条目'}")
        await self._commit()

    # ------------------------------------------------------------------
    # 治理（§8.6）：继任 / 僵尸势力崩溃 / 过期会签退款
    # ------------------------------------------------------------------
    async def _step_governance(self, world: dict):
        gcfg = self.main_cfg().get("governance") or {}
        now = _now()
        await self._step_favor_decay(world)
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
                if not isinstance(v, dict):
                    continue
                rel = str(v.get("rel", ""))
                until = int(v.get("until", 0) or 0)
                if rel.startswith("propose_") and until and now > until:
                    restored = self._restore_proposal(v)
                    if restored:
                        dip[k] = restored
                    else:
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
            members = await self._own_faction_members(world["id"], fid)
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
                # 国库灵石按贡献分配回主玩法钱包（走日限额/超额转战功），
                # 否则崩溃时 treasury.gold 会随 f["treasury"]={} 直接蒸发。
                gold_share = int(share.get("gold", 0) or 0)
                if gold_share > 0:
                    await self._inject_gold(p, gold_share)
                await self.save_player(p)
        owned = [n for n in nodes if n["owner_faction"] == fid]
        rng = self._rng("collapse", fid, world["tick_no"])
        n_bandit = int(len(owned) * float(gcfg.get("collapse_bandit_chance", 0.3)))
        data = world.get("data") or {}
        bandits = data.setdefault("bandits", {})
        for node in rng.sample(owned, min(n_bandit, len(owned))):
            node["owner_faction"] = 0
            node["governor_char_id"] = 0
            gar = node.get("npc_garrison") or {}
            node["npc_garrison"] = {"troops": max(800, int(gar.get("troops", 0) * 1.5)),
                                    "morale": 90}
            bandits[str(node["id"])] = {"spawned_at": _now(), "origin": "collapse",
                                        "until": _now() + 30 * 86400}
            await self.save_node(node)
        for node in owned:
            if node["owner_faction"] == fid:
                node["owner_faction"] = 0
                node["governor_char_id"] = 0
                await self.save_node(node)
        f["alive"] = 0
        f["faction_state"] = "collapsed"
        f["treasury"] = {}
        await self.save_faction(f)
        if int(f.get("alliance_id") or 0):
            try:
                await self._remove_faction_from_alliance(world, f)
            except StrategyError:
                pass
        # 崩溃势力的研究进度随 pending 一起作废：主研角色必须回到 idle，
        # 否则 tech 已不会结算（alive=0 不在 _step_tech 扫描范围），角色
        # 会永久卡在 status='research' 无法派遣/编军。
        for c in await self.get_chars(world_id=world["id"], status="research"):
            if int(c.get("faction_id") or 0) == fid:
                c["status"] = "idle"
                await self.save_char(c)
        for m in members:
            p = await self.get_player(m["user_id"], world["id"])
            if p:
                personal = await self._ensure_personal_faction(world, p)
                await self._move_player_to_faction(world, p, personal)
            await self.delete_member(fid, m["user_id"])
        # 崩溃势力的大牢：据点已全部转为无主，俘虏不能继续留在原节点
        # 或保持 captor_faction 指向已亡势力（无人能劝降/收赎/解放）。
        # 统一释放归乡（含押运中），并清掉旧赎金报价。
        ransoms = data.get("ransoms")
        if not isinstance(ransoms, dict):
            ransoms = {}
        for c in await self.get_chars(world_id=world["id"]):
            if int(c.get("captor_faction") or 0) != fid:
                continue
            if str(c.get("status") or "") not in ("captive", "transfer"):
                continue
            if str(c.get("status") or "") == "transfer":
                await self._execute(
                    "UPDATE strategy_captive_transfers SET status='released' "
                    "WHERE world_id=? AND char_id=? AND status='marching'",
                    (int(world["id"]), int(c["id"])))
            ransoms.pop(str(int(c["id"])), None)
            await self._return_captive_home(c)
        data["ransoms"] = ransoms
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
        """主城重建：主玩法灵石付费（300 万 ×1.5^n），新首都 4 天保护。

        流亡势力（faction_state=exiled）可选任意无主城池/关隘/分舵直接重建，
        且任一成员均可发起；尚有主城时只允许势力主迁都到己方据点。
        """
        world, p = await self._require_player(user_id)
        rcfg = self.main_cfg().get("rebuild") or {}
        f = await self.get_faction(p["faction_id"])
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        node = await self.get_node(world["id"], int(node_id))
        if not node:
            raise StrategyError("据点不存在")
        if node["type"] not in ("city", "pass", "branch"):
            raise StrategyError("新主城须为城池/关隘/分舵")
        owned = int(node.get("owner_faction", 0)) == int(f["id"])
        # 流亡态是重建的唯一合法入口：faction_state 由城破/据点清零统一写入，
        # 仅凭 capital_node_id==0 判定会把「主城已陷但仍持有其他据点」的势力
        # 也当成流亡，使其可在任意无主节点远程重建（绕过迁都）。
        exiled = str(f.get("faction_state") or "") == "exiled"
        if not exiled and int(f.get("capital_node_id") or 0) <= 0:
            # 旧存档自愈：无主城且名下已无任何「可作主城」据点，等价于流亡
            # （新逻辑会在城破时写入 exiled；旧档可能停留在 normal）。
            # v4.5.2：遗迹虽已常规据化，但仍不能担任主城，故不计入此处。
            rest = await self._fetchone(
                "SELECT COUNT(*) AS n FROM strategy_nodes "
                "WHERE world_id=? AND owner_faction=? AND type<>'ruin'",
                (int(world["id"]), int(f["id"])))
            if int((rest or {}).get("n", 0) or 0) <= 0:
                exiled = True
                f["faction_state"] = "exiled"
                await self.save_faction(f)
        if owned:
            # 迁都仅势力主可发起
            await self._require_position(f, user_id, ("leader",))
        else:
            if not exiled:
                raise StrategyError(
                    "只有流亡势力（已失去全部据点）才可在无主节点重建主城；"
                    "迁都请使用迁都指令")
            # §8.7：流亡势力任一成员均可发起重建
            members = await self.get_members(int(f["id"]))
            if not any(str(m.get("user_id")) == str(user_id) for m in members):
                raise StrategyError("仅本势力成员可发起重建")
        if not owned:
            if int(node.get("owner_faction", 0)) != 0:
                raise StrategyError("目标据点不属于无主势力")
            # 保护期/围城中的节点不可作为重建点：否则可用 300 万灵石
            # 强插一个 4 天免战新城，直接中止敌方正在推进的围城。
            if int(node.get("protect_until", 0) or 0) > _now():
                raise StrategyError("目标据点处于保护期，无法在此重建主城")
            if node.get("siege_state"):
                raise StrategyError("目标据点正在被围困，无法在此重建主城")
        cost = R.rebuild_gold_cost(int(rcfg.get("base_gold_cost", 3_000_000)),
                                   int(f.get("rebuild_count", 0)),
                                   float(rcfg.get("cost_growth", 0.5)))
        # §8.7 重建合力：先扣 rebuild_fund（以灵石计价），国库铜钱按
        # ``rebuild.coin_per_gold`` 折算抵充，不足部分由发起人补主玩法灵石；
        # 成功后合力资金清零。v4.3.4 起国库/合力均归本势力，不读取盟主账本。
        gf = f
        treasury = dict(gf.get("treasury") or {})
        fund = max(0, int(gf.get("rebuild_fund") or 0))
        # 国库铜钱按 rebuild.coin_per_gold 折算灵石（默认 1000:1）
        coin_per_gold = max(1, int(rcfg.get("coin_per_gold", 1000) or 1000))
        # 国库里沉淀的灵石（据点产出的 treasury_share）同样是重建资金，
        # 否则这部分灵石只进不出，成为永久黑洞。
        treasury_gold = max(0, int(treasury.get("gold", 0) or 0))
        coin_gold = max(0, int(treasury.get("coin", 0) or 0)) // coin_per_gold
        covered = min(int(cost), fund + treasury_gold + coin_gold)
        gold_need = int(cost) - covered
        use_fund = min(fund, covered)
        rest = covered - use_fund
        use_tgold = min(treasury_gold, rest)
        use_coin = (rest - use_tgold) * coin_per_gold
        if gold_need > 0 and not await self._deduct_main_gold(user_id, gold_need):
            raise StrategyError(
                f"重建资金不足：还需灵石 {gold_need:,}"
                f"（国库+合力已抵 {covered:,}/{int(cost):,}）")
        if use_tgold > 0:
            treasury["gold"] = treasury_gold - int(use_tgold)
            gf["treasury"] = treasury
        if use_coin > 0:
            treasury["coin"] = int(treasury.get("coin", 0)) - int(use_coin)
            gf["treasury"] = treasury
        gf["rebuild_fund"] = 0
        if int(gf["id"]) != int(f["id"]):
            await self.save_faction(gf)
        if not owned:
            # 流亡重建：无主残军自动散尽，据点归入本势力后再设为主城
            node["owner_faction"] = int(f["id"])
            node["npc_garrison"] = {"troops": 0, "morale": 80}
            node["siege_state"] = {}
            await self._clear_bandit_marker(world, node)
            if node["type"] in ("city", "pass", "branch"):
                node["defense"] = R.city_defense_max(max(1, int(node.get("level", 1))))
        f["capital_node_id"] = int(node["id"])
        f["rebuild_count"] = int(f.get("rebuild_count", 0)) + 1
        f["faction_state"] = "normal"
        await self.save_faction(f)
        # 重建同样占用迁都 CD：避免重建后立刻再迁都绕过 7 天限制
        await self._execute(
            "UPDATE strategy_factions SET rebuild_fund=0, last_capital_move_at=? "
            "WHERE id=?", (_now(), int(gf["id"])))
        node["protect_until"] = _now() + int(rcfg.get("protect_days", 4)) * 86400
        node["level"] = max(int(node.get("level", 1)),
                            int(rcfg.get("new_capital_level", 1)))
        await self.save_node(node)
        log = p.get("rebuild_pay_log") or []
        log.append({"at": _now(), "cost": int(cost), "node_id": int(node["id"]),
                    "from_fund": int(use_fund),
                    "from_treasury_gold": int(use_tgold),
                    "from_treasury": int(use_coin),
                    "from_gold": int(gold_need)})
        p["rebuild_pay_log"] = log[-20:]
        await self.save_player(p)
        await self._commit()
        await self._audit(user_id, "rebuild_capital",
                          {"faction_id": int(f["id"]), "node_id": int(node["id"]),
                           "cost": int(cost), "from_fund": int(use_fund),
                           "from_treasury_gold": int(use_tgold),
                           "from_treasury": int(use_coin),
                           "from_gold": int(gold_need)})
        parts = []
        if use_fund:
            parts.append(f"合力 -{use_fund:,}")
        if use_tgold:
            parts.append(f"国库灵石 -{int(use_tgold):,}")
        if use_coin:
            parts.append(f"国库 -{use_coin:,}")
        if gold_need:
            parts.append(f"灵石 -{gold_need:,}")
        return {"ok": True,
                "msg": f"🏯 主城迁至「{node['name']}」（{'、'.join(parts) or '无需付费'}，"
                       f"{int(rcfg.get('protect_days', 4))} 天保护期）"}

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

    # ==================================================================
    # v4.1.0 势力缺口补全（设计稿 §6.4 / §8.4 / §8.6 / §8.7 / §10.3 / §18）
    # ==================================================================
    # ------------------------------------------------------------------
    # 通知助手（势力全员；失败静默）
    # ------------------------------------------------------------------
    async def _notify_faction_event(self, world: dict, faction_id: int,
                                    payload: dict) -> None:
        """向某势力全体成员推送一条事件通知（§12.5）。"""
        try:
            rows = await self._fetchall(
                "SELECT user_id FROM strategy_players WHERE world_id=? AND faction_id=?",
                (int(world["id"]), int(faction_id)))
            uids = [str(r["user_id"]) for r in rows]
            if uids:
                await self._notify(world, uids, "event", payload)
        except Exception:  # noqa: BLE001 - 通知失败不影响主流程
            logger.debug("[strategy] 势力通知失败", exc_info=True)

    # ------------------------------------------------------------------
    # 势力任务（§6.4：高层发布集体目标，按贡献发放战功）
    # ------------------------------------------------------------------
    def _faction_task_cfg(self) -> dict:
        return self.main_cfg().get("faction_task") or {}

    @staticmethod
    def _faction_task_label(f: dict) -> str:
        """任务挂在盟主势力名下：结盟后统一称「联盟任务」。"""
        return "联盟任务" if int((f or {}).get("alliance_id") or 0) else "势力任务"

    def _faction_task_slots(self) -> int:
        return max(0, int((self.main_cfg().get("governance") or {}).get(
            "faction_task_slots", 3)))

    async def get_faction_tasks(self, faction_id: int,
                                status: str = "") -> list[dict]:
        sql = ("SELECT * FROM strategy_faction_tasks WHERE faction_id=?")
        params: list = [int(faction_id)]
        if status:
            sql += " AND status=?"
            params.append(str(status))
        sql += " ORDER BY id DESC"
        return await self._fetchall(sql, tuple(params))

    @staticmethod
    def _task_reward(task: dict) -> dict:
        try:
            return json.loads(task.get("reward_json") or "{}") or {}
        except (TypeError, ValueError):
            return {}

    async def _generate_faction_tasks(self, world: dict, f: dict,
                                      count: Optional[int] = None,
                                      exclude: Optional[set] = None) -> list[dict]:
        """按 slots 随机生成势力/联盟任务（status=open，end_at=now+duration_hours）。

        ``exclude``：已在进行中的任务类型，生成时优先跳过，避免同一势力
        同时出现两条同类型任务、一次进度回填把多条一起填满。
        """
        tcfg = self._faction_task_cfg()
        types = tcfg.get("types") or {}
        if not types:
            return []
        slots = self._faction_task_slots() if count is None else max(0, int(count))
        if slots <= 0:
            return []
        now = _now()
        end_at = now + int(tcfg.get("duration_hours", 24)) * 3600
        rng = self._rng("faction_task", int(f["id"]),
                        int(world.get("tick_no", 0)), now)
        keys = list(types.keys())
        skip = {str(x) for x in (exclude or set())}
        fresh = [k for k in keys if k not in skip]
        pool = fresh or keys
        rng.shuffle(pool)
        out: list[dict] = []
        for i in range(slots):
            # 先轮一遍不重复的类型；任务槽位多于类型数时才允许重复
            key = pool[i % len(pool)]
            spec = types.get(key) or {}
            title = str(spec.get("name") or key)
            target = max(1, int(spec.get("target", 1) or 1))
            reward = {"merit": max(0, int(spec.get("reward_merit", 0) or 0))}
            tid = await self._execute(
                "INSERT INTO strategy_faction_tasks (world_id, faction_id, task_type, "
                "title, progress, target, reward_json, status, start_at, end_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (int(world["id"]), int(f["id"]), key, title, 0, target,
                 json.dumps(reward, ensure_ascii=False), "open", now, end_at))
            out.append({"id": int(tid), "faction_id": int(f["id"]),
                        "task_type": key, "title": title, "progress": 0,
                        "target": target, "reward": reward, "status": "open",
                        "start_at": now, "end_at": end_at})
        return out

    async def faction_task_refresh(self, user_id: str) -> dict:
        """手动刷新势力/联盟任务（势力主/盟主/副主/祭酒，每 refresh_hours 一次）。"""
        world, p = await self._require_player(user_id)
        f = await self._governance_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader", "deputy", "jijiu"))
        label = self._faction_task_label(f)
        tcfg = self._faction_task_cfg()
        hours = int(tcfg.get("refresh_hours", 12) or 0)
        now = _now()
        data = world.get("data") or {}
        cd = data.get("faction_task_refresh") or {}
        last = int(cd.get(str(f["id"]), 0) or 0)
        if hours > 0 and last and now - last < hours * 3600:
            left = (hours * 3600 - (now - last) + 3599) // 3600
            raise StrategyError(f"{label}刷新冷却中（还剩 {left} 小时）")
        slots = self._faction_task_slots()
        open_tasks = await self.get_faction_tasks(int(f["id"]), "open")
        if len(open_tasks) >= slots:
            raise StrategyError(
                f"当前已有 {len(open_tasks)} 条进行中的{label}，请先完成再刷新")
        tasks = await self._generate_faction_tasks(
            world, f, slots - len(open_tasks),
            exclude={t.get("task_type") for t in open_tasks})
        cd[str(f["id"])] = now
        data["faction_task_refresh"] = cd
        world["data"] = data
        await self.save_world(world)
        await self._commit()
        await self._audit(user_id, "faction_task_refresh",
                          {"faction_id": int(f["id"]), "count": len(tasks),
                           "manual": True})
        return {"ok": True, "tasks": open_tasks + tasks,
                "msg": f"📜 {label}已刷新（新增 {len(tasks)} 条，"
                       f"{int(tcfg.get('duration_hours', 24))}h 内有效）"}

    async def faction_task_claim(self, user_id: str, task_id: int) -> dict:
        """领取已达标的势力/联盟任务奖励（战功，受 §18.4 观察期限制）。"""
        world, p = await self._require_player(user_id)
        f = await self._governance_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        label = self._faction_task_label(f)
        row = await self._fetchone(
            "SELECT * FROM strategy_faction_tasks WHERE id=? AND world_id=?",
            (int(task_id), int(world["id"])))
        if not row:
            raise StrategyError(f"{label}不存在")
        task = dict(row)
        if int(task.get("faction_id", 0)) != int(f["id"]):
            raise StrategyError(f"该{label}不属于你的势力/联盟")
        if str(task.get("status")) != "open":
            raise StrategyError(f"该{label}已完成或已过期")
        if int(task.get("progress", 0) or 0) < int(task.get("target", 1) or 1):
            raise StrategyError(
                f"任务进度不足（{int(task.get('progress', 0))}/"
                f"{int(task.get('target', 1))}）")
        reward = self._task_reward(task)
        raw_merit = max(0, int(reward.get("merit", 0) or 0))
        merit = await self._settle_merit(user_id, raw_merit, "faction_task")
        p["merit"] = int(p.get("merit", 0)) + int(merit)
        p["merit_season"] = int(p.get("merit_season", 0)) + int(merit)
        await self.save_player(p)
        # 乐观锁：仅 open 状态可领取；并发双击/多端领取只有一次成功
        n = await self._exec_count(
            "UPDATE strategy_faction_tasks SET status='claimed', assigned_user=? "
            "WHERE id=? AND status='open'", (str(user_id), int(task_id)))
        if n != 1:
            await self.db.conn.rollback()
            raise StrategyError(f"该{label}已被领取，请刷新后重试")
        await self.add_contribution(int(f["id"]), user_id,
                                    max(1, int(merit) // 5))
        await self._commit()
        await self._audit(user_id, "faction_task_claim",
                          {"faction_id": int(f["id"]), "task_id": int(task_id),
                           "task_type": task.get("task_type"),
                           "merit": int(merit), "raw_merit": int(raw_merit)})
        note = "" if int(merit) == int(raw_merit) else \
            "（观察期内战功不计，详见审计）"
        return {"ok": True, "reward": {"merit": int(merit)},
                "msg": f"🎖「{task.get('title', '')}」已完成，"
                       f"战功 +{int(merit)}{note}"}

    async def _faction_task_progress(self, faction_id: int, task_type: str,
                                     amount: int = 1) -> int:
        """回填势力/联盟任务进度（供 B 在 tick/战斗/捐献事件中调用）。

        进度上限为 target；返回受影响的任务行数。**不提交事务**，
        由调用方的 tick/动作统一 ``_commit``。
        """
        try:
            fid = int(faction_id)
        except (TypeError, ValueError):
            return 0
        if fid <= 0 or not task_type:
            return 0
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return 0
        if amount == 0:
            return 0
        # 联盟语义：任务挂在盟主势力名下，玩家所属成员势力自动上溯
        try:
            f = await self.get_faction(fid)
            if f:
                host = await self._alliance_host(f)
                if host:
                    fid = int(host["id"])
        except Exception:  # noqa: BLE001 - 上溯失败按原势力记录
            pass
        return await self._exec_count(
            "UPDATE strategy_faction_tasks "
            "SET progress=MIN(target, MAX(0, progress+?)) "
            "WHERE faction_id=? AND task_type=? AND status='open'",
            (int(amount), fid, str(task_type)))

    async def _step_faction_tasks(self, world: dict):
        """tick：过期任务置 expired，并按 refresh_hours 自动补刷新。"""
        tcfg = self._faction_task_cfg()
        now = _now()
        await self._execute(
            "UPDATE strategy_faction_tasks SET status='expired' "
            "WHERE world_id=? AND status='open' AND end_at>0 AND end_at<=?",
            (int(world["id"]), now))
        slots = self._faction_task_slots()
        if slots <= 0:
            await self._commit()
            return
        hours = int(tcfg.get("refresh_hours", 12) or 0)
        data = world.get("data") or {}
        cd = data.get("faction_task_refresh") or {}
        rows = await self._fetchall(
            "SELECT * FROM strategy_factions WHERE world_id=? AND kind='player' "
            "AND alive=1", (int(world["id"]),))
        for row in rows:
            f = self._decode("strategy_factions", row)
            aid = int(f.get("alliance_id") or 0)
            if aid and aid != int(f["id"]):
                continue  # 联盟任务只在盟主势力推进
            cnt = await self._fetchone(
                "SELECT COUNT(*) AS n FROM strategy_faction_tasks "
                "WHERE faction_id=? AND status='open'", (int(f["id"]),))
            open_n = int((cnt or {}).get("n", 0) or 0)
            if open_n >= slots:
                continue
            last = int(cd.get(str(f["id"]), 0) or 0)
            if hours > 0 and last and now - last < hours * 3600:
                continue
            open_rows = await self.get_faction_tasks(int(f["id"]), "open")
            await self._generate_faction_tasks(
                world, f, slots - open_n,
                exclude={t.get("task_type") for t in open_rows})
            cd[str(f["id"])] = now
        data["faction_task_refresh"] = cd
        world["data"] = data
        await self.save_world(world)
        await self._commit()

    # ------------------------------------------------------------------
    # 动员令（§6.4：全军攻击/防御 +5%，24h；覆盖为 recruit/march/defend/attack）
    # ------------------------------------------------------------------
    async def mobilization_issue(self, user_id: str, kind: str) -> dict:
        world, p = await self._require_player(user_id)
        mcfg = self.main_cfg().get("mobilization") or {}
        kinds = mcfg.get("kinds") or {}
        kind = str(kind or "")
        if kind not in kinds:
            raise StrategyError("动员令类型无效（recruit/march/defend/attack）")
        f = await self._governance_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader", "deputy", "jijiu"))
        in_alliance = bool(int(f.get("alliance_id") or 0))
        label = "联盟动员令" if in_alliance else "动员令"
        now = _now()
        cd_hours = int(mcfg.get("cooldown_hours", 24) or 0)
        last_row = await self._fetchone(
            "SELECT MAX(start_at) AS t FROM strategy_mobilizations "
            "WHERE faction_id=?", (int(f["id"]),))
        last = int((last_row or {}).get("t", 0) or 0)
        if cd_hours > 0 and last and now - last < cd_hours * 3600:
            left = (cd_hours * 3600 - (now - last) + 3599) // 3600
            raise StrategyError(f"{label}冷却中（还剩 {left} 小时）")
        cost = max(0, int(mcfg.get("cost_merit", 60) or 0))
        if int(p.get("merit", 0)) < cost:
            raise StrategyError(f"战功不足：{label}需 {cost}")
        p["merit"] = int(p.get("merit", 0)) - cost
        await self.save_player(p)
        spec = kinds.get(kind) or {}
        buff = {"effect": str(spec.get("effect") or ""),
                "value": float(spec.get("value", 1) or 1)}
        hours = max(1, int(mcfg.get("hours", 12) or 12))
        end_at = now + hours * 3600
        await self._execute(
            "UPDATE strategy_mobilizations SET status='expired' "
            "WHERE faction_id=? AND status='active'", (int(f["id"]),))
        mid = await self._execute(
            "INSERT INTO strategy_mobilizations (world_id, faction_id, issued_by, "
            "kind, buff_json, start_at, end_at, status) "
            "VALUES (?,?,?,?,?,?,?,'active')",
            (int(world["id"]), int(f["id"]), str(user_id), kind,
             json.dumps(buff, ensure_ascii=False), now, end_at))
        await self._commit()
        await self._audit(user_id, "mobilization_issue",
                          {"faction_id": int(f["id"]), "kind": kind,
                           "buff": buff, "cost_merit": cost,
                           "mobilization_id": int(mid)})
        name = str(spec.get("name") or kind)
        prefix = f"联盟「{f['name']}」" if in_alliance else f"「{f['name']}」"
        await self._notify_faction_event(
            world, int(f["id"]),
            {"event": "mobilization", "kind": kind, "name": name,
             "ends_at": end_at,
             "text": f"📣{prefix}发布{name}（{hours}h，"
                     f"{buff['effect']} {buff['value']}）"})
        return {"ok": True, "ends_at": end_at, "cost_merit": cost,
                "msg": f"📣 已发布「{name}」，持续 {hours}h（战功 -{cost}）"}

    async def _step_mobilizations(self, world: dict):
        """tick：过期动员令置 expired。"""
        n = await self._exec_count(
            "UPDATE strategy_mobilizations SET status='expired' "
            "WHERE world_id=? AND status='active' AND end_at<=?",
            (int(world["id"]), _now()))
        if n:
            await self._commit()

    # ------------------------------------------------------------------
    # 势力事件（§10.3：角色出走 / 民心沸腾 / 哗变 / 细作 / 秘藏 / 名将迟暮）
    # ------------------------------------------------------------------
    async def _step_faction_events(self, world: dict) -> list[str]:
        """tick：掷点 roll_per_tick 次，触发 0~N 个 scope=faction 事件并即时结算。

        v4.3.0：此前掷点结果只作「是否触发」用（<=0 跳过，否则恒触发 1 个），
        配置 [0,2] 的"2"永远无法兑现；现按掷点值循环触发，冷却天然防同 tick 重复。
        """
        ecfg = self.cfg.get("events") or {}
        events = [e for e in (ecfg.get("events") or [])
                  if str(e.get("scope") or "") == "faction"]
        if not events:
            return []
        tick_no = int(world.get("tick_no", 0))
        rng = self._rng("faction_events", int(world["id"]), tick_no)
        lo, hi = (ecfg.get("roll_per_tick") or [0, 2])
        n = rng.randint(int(lo), int(hi))
        fired: list[str] = []
        if n <= 0:
            return fired
        data = world.get("data") or {}
        # 独立 cooldown 键：避免与主类 _step_events 的全 scope 掷点互相抢占
        cd = data.get("faction_event_cd") or {}
        touched = False
        for _ in range(n):
            eligible = [e for e in events
                        if tick_no >= int(cd.get(e["id"], 0) or 0)]
            if not eligible:
                break
            weights = [max(1, int(e.get("weight", 1) or 1)) for e in eligible]
            ev = rng.choices(eligible, weights=weights)[0]
            rows = await self._fetchall(
                "SELECT id FROM strategy_factions WHERE world_id=? AND kind='player' "
                "AND alive=1", (int(world["id"]),))
            cands = [int(r["id"]) for r in rows]
            if not cands:
                cd[ev["id"]] = tick_no + int(ev.get("cooldown_ticks", 96) or 0)
                touched = True
                continue
            fid = rng.choice(cands)
            f = await self.get_faction(fid)
            if not f:
                continue
            await self._apply_faction_event(world, f, ev, rng)
            cd[ev["id"]] = tick_no + int(ev.get("cooldown_ticks", 96) or 0)
            touched = True
            fired.append(str(ev.get("name") or ev["id"]))
        if touched:
            data["faction_event_cd"] = cd
            world["data"] = data
            await self.save_world(world)
            await self._commit()
        return fired

    async def _apply_faction_event(self, world: dict, f: dict, ev: dict,
                                   rng: random.Random) -> dict:
        """结算一个势力事件的全部 effects，并写审计 + 势力公告。"""
        msgs: list[str] = []
        for eff in ev.get("effects") or []:
            try:
                msg = await self._faction_event_effect(world, f, eff, rng)
            except Exception as e:  # noqa: BLE001 - 单个效果失败不拖垮 tick
                logger.warning(f"[strategy] 势力事件 {ev.get('id')} 效果失败: {e}")
                msg = ""
            if msg:
                msgs.append(msg)
        text = "；".join(msgs)
        await self._audit(str(f.get("leader_id") or ""), "faction_event",
                          {"faction_id": int(f["id"]), "event": ev.get("id"),
                           "name": ev.get("name", ev.get("id")), "text": text})
        if text:
            await self._notify_faction_event(
                world, int(f["id"]),
                {"event": ev.get("id"), "name": ev.get("name", ev.get("id")),
                 "text": f"⚡「{f['name']}」{ev.get('name', '')}：{text}"})
        return {"ok": True, "text": text}

    async def _faction_event_effect(self, world: dict, f: dict, eff: dict,
                                    rng: random.Random) -> str:
        etype = str(eff.get("type") or "")
        wid = int(world["id"])
        fid = int(f["id"])
        nodes = [n for n in await self.get_nodes(wid)
                 if int(n.get("owner_faction", 0)) == fid]
        chars = [c for c in await self.get_chars(faction_id=fid, world_id=wid)
                 if c.get("status") not in ("dead", "captive", "wander", "transfer")]
        if etype == "char_leave":
            below = int(eff.get("loyalty_below", 30) or 30)
            cands = [c for c in chars if int(c.get("loyalty", 70)) < below]
            if not cands:
                return ""
            c = rng.choice(cands)
            if rng.random() >= float(eff.get("leave_chance", 0.5) or 0.5):
                return (f"「{c['name']}」心生去意，但暂未离队"
                        f"（忠诚 {int(c.get('loyalty', 0))}）")
            owner = str(c.get("owner_user_id") or "")
            # 先移出军团名册：否则在途军团仍带「已出走」武将出战/结算
            await self._detach_from_armies(c)
            c["status"] = "wander"
            c["faction_id"] = 0
            c["owner_user_id"] = ""
            c["appointed_position"] = ""
            c["appointed_node_id"] = 0
            c["captor_faction"] = 0
            c["imprisoned_node_id"] = 0
            c["auction_cooldown_until"] = 0
            c["loyalty"] = max(40, int(c.get("loyalty", 40)))
            await self._clear_char_governors(world["id"], c["id"])
            await self.save_char(c)
            if owner:
                await self._notify(world, [owner], "event",
                                   {"event": "char_leave",
                                    "text": f"「{c['name']}」忠诚过低，已挂印出走"})
            return f"「{c['name']}」忠诚过低，挂印出走"
        if etype == "morale":
            delta = int(eff.get("delta", 8) or 0)
            if not nodes or not delta:
                return ""
            for n in nodes:
                await self._bump_morale(n, delta, "faction_event")
                await self.save_node(n)
            return f"全境民心 {delta:+d}（{len(nodes)} 据点）"
        if etype == "riot":
            if not nodes:
                return ""
            # 与本 tick 的「低民心哗变」（_step_recovery）互斥：同一据点
            # 一个 tick 只允许一次哗变，避免事件与民意双重扣兵/扣库。
            tick_no = int(world.get("tick_no", 0) or 0)
            data = world.get("data") or {}
            riot_map = data.get("riot_tick_nodes") or {}
            pool = [n for n in nodes if riot_map.get(str(n["id"])) != tick_no]
            if not pool:
                return ""
            n = rng.choice(pool)
            riot_map[str(n["id"])] = tick_no
            data["riot_tick_nodes"] = riot_map
            world["data"] = data
            pct = float(eff.get("troops_pct", 0.2) or 0.2)
            gar = n.get("npc_garrison") or {}
            loss = 0
            if int(gar.get("troops", 0) or 0) > 0:
                loss = max(1, int(int(gar["troops"]) * pct))
                gar["troops"] = max(0, int(gar["troops"]) - loss)
                n["npc_garrison"] = gar
            # 仓库同时被哄抢（按同比例损耗库存）
            stock = dict(n.get("stock") or {})
            stolen: list[str] = []
            for k in ("coin", "grain", "iron"):
                have = int(stock.get(k, 0) or 0)
                if have <= 0:
                    continue
                cut = int(have * pct)
                if cut <= 0:
                    continue
                stock[k] = have - cut
                stolen.append(f"{R.RESOURCE_NAMES.get(k, k)}-{cut}")
            if stolen:
                n["stock"] = stock
            await self._bump_morale(n, int(eff.get("morale_delta", 5) or 0), "riot")
            await self.save_node(n)
            text = f"「{n['name']}」哗变，驻军 -{loss}"
            if stolen:
                text += "，库存 " + "、".join(stolen)
            if chars and rng.random() < float(eff.get("injury_chance", 0.3) or 0.3):
                c = rng.choice(chars)
                c["injury"] = min(100, int(c.get("injury", 0) or 0) +
                                  int(eff.get("injury", 10) or 10))
                await self.save_char(c)
                text += f"，{c['name']} 受伤"
            return text
        if etype == "spy":
            if not nodes:
                return ""
            n = rng.choice(nodes)
            stock = n.get("stock") or {}
            gar = n.get("npc_garrison") or {}
            intel = (f"「{n['name']}」库存 铜钱{int(stock.get('coin', 0) or 0)}/"
                     f"粮{int(stock.get('grain', 0) or 0)}/"
                     f"铁{int(stock.get('iron', 0) or 0)}，"
                     f"驻军{int(gar.get('troops', 0) or 0)}")
            loss = max(0, int(eff.get("merit_loss", 0) or 0))
            if loss:
                leader = await self.get_player(str(f.get("leader_id") or ""), wid)
                if leader:
                    leader["merit"] = max(0, int(leader.get("merit", 0)) - loss)
                    await self.save_player(leader)
            return f"细作潜入，{intel} 已泄露" + (f"（战功 -{loss}）" if loss else "")
        if etype == "treasure":
            if rng.random() > float(eff.get("chance", 1.0) or 0.0):
                return "秘藏线索中断，一无所获"
            rewards = eff.get("rewards") or {}
            treasury = f.get("treasury") or {}
            got = []
            for k, v in rewards.items():
                amount = int(v or 0)
                if amount <= 0:
                    continue
                treasury[k] = int(treasury.get(k, 0) or 0) + amount
                got.append(f"{R.RESOURCE_NAMES.get(k, k)}+{amount}")
            if not got:
                return ""
            f["treasury"] = treasury
            await self.save_faction(f)
            return "挖出秘藏：" + "、".join(got)
        if etype == "char_aging":
            rarity = str(eff.get("rarity") or "legend")
            cands = [c for c in chars if str(c.get("rarity") or "") == rarity]
            if not cands:
                return ""
            c = rng.choice(cands)
            stats = dict(c.get("stats") or {})
            if stats:
                key = rng.choice(list(stats.keys()))
                drop = rng.randint(1, 3)
                stats[key] = max(1, int(stats.get(key, 0) or 0) - drop)
                c["stats"] = stats
            else:
                key, drop = "stats", 0
            c["loyalty"] = max(0, int(c.get("loyalty", 70) or 0) +
                               int(eff.get("loyalty_delta", -5) or 0))
            pack = max(0, int(eff.get("exp_pack", 0) or 0))
            if pack > 0:
                c["exp"] = int(c.get("exp", 0) or 0) + pack
            await self.save_char(c)
            return (f"名将迟暮：「{c['name']}」"
                    f"{R.STAT_NAMES.get(key, key)} -{drop}"
                    f"（忠诚 {c['loyalty']}）")
        return ""

    # ------------------------------------------------------------------
    # 治理：低忠诚出走（§8.6）
    # ------------------------------------------------------------------
    async def _step_loyalty_defect(self, world: dict):
        """tick：忠诚低于 governance.loyalty_defect 的角色出走（转 wander）。"""
        gcfg = self.main_cfg().get("governance") or {}
        thr = int(gcfg.get("loyalty_defect", 30) or 0)
        if thr <= 0:
            return
        rows = await self._fetchall(
            "SELECT * FROM strategy_characters WHERE world_id=? AND faction_id>0 "
            "AND status NOT IN ('dead','captive','wander','transfer')",
            (int(world["id"]),))
        changed = False
        for row in rows:
            c = self._decode("strategy_characters", row)
            if int(c.get("loyalty", 70) or 0) >= thr:
                continue
            fid = int(c.get("faction_id") or 0)
            f = await self.get_faction(fid)
            if not f or str(f.get("kind")) != "player":
                continue  # NPC 角色不走忠诚出走
            owner = str(c.get("owner_user_id") or "")
            old_loyalty = int(c.get("loyalty", 0) or 0)
            await self._detach_from_armies(c)
            c["status"] = "wander"
            c["faction_id"] = 0
            c["owner_user_id"] = ""
            c["appointed_position"] = ""
            c["appointed_node_id"] = 0
            c["captor_faction"] = 0
            c["imprisoned_node_id"] = 0
            c["auction_cooldown_until"] = 0
            await self._clear_char_governors(world["id"], c["id"])
            await self.save_char(c)
            changed = True
            payload = {"event": "char_defect",
                       "text": f"「{c['name']}」忠诚跌至 {old_loyalty}，已离队出走"}
            targets = [u for u in (owner, str(f.get("leader_id") or "")) if u]
            if targets:
                await self._notify(world, targets, "event", payload)
            await self._audit(owner or str(f.get("leader_id") or ""), "char_defect",
                              {"char_id": int(c["id"]), "faction_id": fid,
                               "loyalty": old_loyalty, "threshold": thr})
        if changed:
            await self._commit()

    # ------------------------------------------------------------------
    # 迁都（§8.7：CD faction.capital_relocate_cd_days，扣国库）
    # ------------------------------------------------------------------
    async def capital_relocate(self, user_id: str, node_id: int) -> dict:
        world, p = await self._require_player(user_id)
        f = await self.get_faction(int(p["faction_id"]))
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        await self._require_position(f, user_id, ("leader",))
        old_cap = int(f.get("capital_node_id") or 0)
        node = await self.get_node(int(world["id"]), int(node_id))
        if not node:
            raise StrategyError("据点不存在")
        if node["type"] not in ("city", "pass", "branch"):
            raise StrategyError("新主城须为城池/关隘/分舵")
        if int(node.get("owner_faction", 0)) != int(f["id"]):
            raise StrategyError("只能迁都至我方据点")
        if int(f.get("capital_node_id") or 0) == int(node["id"]):
            raise StrategyError("该据点已是主城")
        now = _now()
        fcfg = self.main_cfg().get("faction") or {}
        cd_days = int(fcfg.get("capital_relocate_cd_days", 7) or 0)
        raw = await self._fetchone(
            "SELECT last_capital_move_at FROM strategy_factions WHERE id=?",
            (int(f["id"]),))
        last = int((raw or {}).get("last_capital_move_at", 0) or 0)
        cd_until = last + cd_days * 86400 if cd_days > 0 else 0
        if cd_until and now < cd_until:
            left = (cd_until - now + 3599) // 3600
            raise StrategyError(f"迁都冷却中（还剩 {left} 小时）")
        rcfg = self.main_cfg().get("rebuild") or {}
        cost = int(int(rcfg.get("base_gold_cost", 3_000_000)) * 0.1)
        treasury = dict(f.get("treasury") or {})
        if int(treasury.get("coin", 0) or 0) < cost:
            raise StrategyError(
                f"国库铜钱不足：迁都需 {cost:,}（现有 "
                f"{int(treasury.get('coin', 0) or 0):,}）")
        treasury["coin"] = int(treasury.get("coin", 0)) - cost
        f["treasury"] = treasury
        f["capital_node_id"] = int(node["id"])
        # 迁都成功即脱离流亡态（否则 faction_state 永远停留在 exiled）
        f["faction_state"] = "normal"
        await self.save_faction(f)
        await self._execute(
            "UPDATE strategy_factions SET last_capital_move_at=?, treasury_json=? "
            "WHERE id=?",
            (now, json.dumps(treasury, ensure_ascii=False), int(f["id"])))
        await self._commit()
        await self._audit(user_id, "capital_relocate",
                          {"faction_id": int(f["id"]), "from": old_cap,
                           "node_id": int(node["id"]), "cost": cost,
                           "cooldown_days": cd_days})
        await self._notify_faction_event(
            world, int(f["id"]),
            {"event": "capital_relocate",
             "text": f"🏯「{f['name']}」迁都至「{node['name']}」"
                     f"（国库铜钱 -{cost:,}）"})
        return {"ok": True, "capital_node_id": int(node["id"]),
                "cooldown_until": now + cd_days * 86400,
                "msg": f"🏯 已迁都至「{node['name']}」（国库 -{cost:,} 铜钱，"
                       f"{cd_days} 天内不可再迁）"}

    # ------------------------------------------------------------------
    # 重建合力（§8.7：多人凑款，成功后清零）
    # ------------------------------------------------------------------
    async def rebuild_donate(self, user_id: str, amount: int) -> dict:
        """成员向本势力 rebuild_fund 捐铜钱（用于主城重建，成功后清零）。"""
        world, p = await self._require_player(user_id)
        f = await self._own_faction(p)
        if not f or f.get("kind") == "npc":
            raise StrategyError("当前不在玩家势力中")
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            raise StrategyError("捐款数额无效") from None
        if amount <= 0:
            raise StrategyError("捐款数额须为正")
        # §8.7 重建消耗主玩法灵石：合力资金以「灵石」计价，盟友可共同凑款
        if not await self._deduct_main_gold(user_id, amount):
            raise StrategyError(f"灵石不足（需 {amount:,}）")
        cur = await self._fetchone(
            "SELECT rebuild_fund FROM strategy_factions WHERE id=?",
            (int(f["id"]),))
        fund = int((cur or {}).get("rebuild_fund", 0) or 0) + amount
        await self._execute(
            "UPDATE strategy_factions SET rebuild_fund=? WHERE id=?",
            (fund, int(f["id"])))
        await self.add_contribution(int(f["id"]), user_id, max(1, amount // 1000))
        await self._commit()
        await self._audit(user_id, "rebuild_donate",
                          {"faction_id": int(f["id"]), "amount": amount,
                           "fund": fund})
        return {"ok": True, "fund": fund,
                "msg": f"💰 已向重建合力捐入灵石 {amount:,}（当前合力 "
                       f"{fund:,}）"}

    # ------------------------------------------------------------------
    # 安全（§18.4：观察期 / 多开战功 ×0）
    # ------------------------------------------------------------------
    async def _position_tenure_ok(self, faction_id: int, user_id: str,
                                  hours: int) -> bool:
        """官职观察期校验；创派元勋（与最早成员同期入盟）豁免。"""
        if int(hours) <= 0:
            return True
        members = await self.get_members(int(faction_id))
        target = next((m for m in members if m["user_id"] == str(user_id)), None)
        if not target:
            return True
        if self._is_joined_long_enough(target, int(hours)):
            return True
        joined = int(target.get("joined_at", 0) or 0)
        oldest = min((int(m.get("joined_at", 0) or 0) for m in members
                      if int(m.get("joined_at", 0) or 0) > 0), default=0)
        # 2h 内与最早成员先后入盟 → 同批创派，不设观察期
        return bool(oldest and joined and joined - oldest <= 2 * 3600)

    async def _settle_merit(self, user_id: str, amount: int,
                            reason: str = "") -> int:
        """战功入账前门禁：返回实发战功（观察期/多开嫌疑 ×0）。

        调用方拿到返回值后再自行累加 ``merit/merit_season`` 并 save_player。
        """
        try:
            amount = int(amount or 0)
        except (TypeError, ValueError):
            return 0
        if amount <= 0:
            return 0
        sec = self.main_cfg().get("security") or {}
        world_id = await self._current_world_id()
        if world_id <= 0:
            return amount
        p = await self.get_player(str(user_id), world_id)
        member = None
        if p:
            # 联盟语义下以「共同治理势力（盟主）名册」为准
            gov = await self._governance_faction(p)
            if gov:
                member = await self.get_member(int(gov["id"]), str(user_id))
        hours = int(sec.get("min_join_hours_for_merit", 0) or 0)
        if hours > 0 and member and not self._is_joined_long_enough(member, hours):
            await self._audit(user_id, "merit_zero",
                              {"rule": "min_join_hours_for_merit",
                               "hours": hours, "amount": amount,
                               "source": reason})
            return 0
        if bool(sec.get("multi_open_merit_zero", True)):
            rows = await self._fetchall(
                "SELECT detail_json FROM strategy_audit WHERE world_id=? AND "
                "user_id=? AND action='faction_join' AND created_at>=?",
                (world_id, str(user_id), _now() - 86400))
            fids: set[int] = set()
            for r in rows:
                try:
                    detail = json.loads(r.get("detail_json") or "{}") or {}
                    fids.add(int(detail.get("faction_id", 0) or 0))
                except (TypeError, ValueError):
                    continue
            fids.discard(0)
            if len(fids) >= 2:
                await self._audit(user_id, "merit_zero",
                                  {"rule": "multi_open_24h_switch",
                                   "factions": sorted(fids),
                                   "amount": amount, "source": reason})
                return 0
        return amount

    # ------------------------------------------------------------------
    # 面板视图（§2.4 faction_ext，供 B 的 get_state 调用）
    # ------------------------------------------------------------------
    async def get_faction_ext(self, world: dict, f: dict,
                              own_f: Optional[dict] = None) -> dict:
        """势力扩展视图：任务是联盟共享，主城/重建/迁都 CD 归本势力。

        v4.5.5：结盟后「联盟任务 / 动员令」挂在盟主势力名下；但重建合力、
        主城与迁都冷却属于**本势力**。此前整份视图都按盟主生成，导致成员
        势力主看到盟主的重建合力与冷却，做出与后端（本势力账本）不一致的
        判断。``own_f`` 由调用方传入本势力；缺省时按 ``f`` 自身读取。
        """
        original = f
        own = own_f or await self.get_faction(
            int(original.get("id") or 0)) or original
        host = await self._alliance_host(f) or f
        if host is not f and int(host.get("id", 0)) != int(f.get("id", 0)):
            f = host
        tasks = await self.get_faction_tasks(int(f["id"]), "open")
        task_view = [{"id": int(t["id"]), "title": t.get("title", ""),
                      "task_type": t.get("task_type", ""),
                      "progress": int(t.get("progress", 0) or 0),
                      "target": int(t.get("target", 1) or 1),
                      "status": t.get("status", "open"),
                      "reward": self._task_reward(t),
                      "end_at": int(t.get("end_at", 0) or 0)}
                     for t in tasks]
        mob = await self._fetchone(
            "SELECT * FROM strategy_mobilizations WHERE faction_id=? AND "
            "status='active' AND end_at>? ORDER BY id DESC LIMIT 1",
            (int(f["id"]), _now()))
        mob_view = None
        if mob:
            try:
                buff = json.loads(mob.get("buff_json") or "{}") or {}
            except (TypeError, ValueError):
                buff = {}
            spec = ((self.main_cfg().get("mobilization") or {}).get("kinds")
                    or {}).get(str(mob.get("kind")), {})
            mob_view = {"kind": mob.get("kind"),
                        "name": spec.get("name", mob.get("kind")),
                        "effect": buff.get("effect"), "value": buff.get("value"),
                        "ends_at": int(mob.get("end_at", 0) or 0)}
        raw = await self._fetchone(
            "SELECT rebuild_fund, last_capital_move_at FROM strategy_factions "
            "WHERE id=?", (int(own["id"]),)) or {}
        cd_days = int((self.main_cfg().get("faction") or {}).get(
            "capital_relocate_cd_days", 7) or 0)
        last = int(raw.get("last_capital_move_at", 0) or 0)
        return {
            "tasks": task_view,
            "mobilization": mob_view,
            "own_faction_id": int(own["id"]),
            "rebuild_fund": int(raw.get("rebuild_fund", 0) or 0),
            "capital": {
                "node_id": int(own.get("capital_node_id") or 0),
                "bonus_pct": float((self.main_cfg().get("governance") or {}).get(
                    "capital_output_bonus", 0.1) or 0),
            },
            "relocate_cd_until": (last + cd_days * 86400) if cd_days else 0,
        }


def hashlib_md5(s: str) -> str:
    import hashlib
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]
