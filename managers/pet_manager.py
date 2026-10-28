# managers/pet_manager.py
import random
import time
from typing import Tuple, Dict, Optional, List
from ..data import DataBase
from ..models import Player
from ..models_extended import UserStatus


def _build_pet_types(base_types: Dict[str, dict], evolutions: Dict[str, list]) -> Dict[str, dict]:
    """按进化链生成所有灵兽阶段，阶段数据继承基础品种并放大。"""
    result: Dict[str, dict] = {}
    for base_name, base_info in base_types.items():
        chain = evolutions.get(base_name) or [{"name": base_name, "level": 1}]
        for stage_index, stage in enumerate(chain):
            stat_mult = float(stage.get("stat_mult", 1.0) or 1.0)
            effect_mult = float(stage.get("effect_mult", 1.0) or 1.0)
            info = dict(base_info)
            for key in ("hp_ratio", "atk_ratio", "def_ratio"):
                info[key] = float(base_info[key]) * stat_mult
            for key in ("hp_base", "hp_growth", "atk_base", "atk_growth", "def_base", "def_growth"):
                info[key] = int(round(float(base_info[key]) * stat_mult))
            info["val"] = float(base_info["val"]) * effect_mult
            info["guard_rate"] = min(
                0.6,
                float(base_info.get("guard_rate", 0.15)) + float(stage.get("guard_bonus", 0.0) or 0.0),
            )
            info["effect_mult"] = effect_mult
            info["desc"] = stage.get("desc", base_info.get("desc", ""))
            info["name"] = stage["name"]
            info["base_type"] = base_name
            info["stage"] = stage_index
            info["evolution_level"] = int(stage.get("level", 1) or 1)
            info["next_evolution_level"] = (
                int(chain[stage_index + 1]["level"]) if stage_index + 1 < len(chain) else None
            )
            info["is_base"] = stage_index == 0
            result[stage["name"]] = info
    return result


class PetManager:
    """灵兽管理器"""

    BASE_PET_TYPES = {
        "火灵狐": {
            "desc": "攻击时附带灼烧，增加主人暴击率",
            "attr": "crit_rate", "val": 10,
            "mechanic": "burn",
            "hp_ratio": 0.12, "hp_base": 120, "hp_growth": 20,
            "atk_ratio": 0.16, "atk_base": 18, "atk_growth": 4,
            "def_ratio": 0.05, "def_base": 6, "def_growth": 1,
            "guard_rate": 0.08,
        },
        "冰晶兽": {
            "desc": "极寒护盾，增加主人防御力，并替主人承受更多伤害",
            "attr": "defense", "val": 50,
            "mechanic": "shield",
            "hp_ratio": 0.22, "hp_base": 220, "hp_growth": 34,
            "atk_ratio": 0.08, "atk_base": 10, "atk_growth": 3,
            "def_ratio": 0.15, "def_base": 18, "def_growth": 3,
            "guard_rate": 0.18,
        },
        "噬金鼠": {
            "desc": "嗅觉敏锐，增加日常灵石掉落",
            "attr": "gold_bonus", "val": 0.1,
            "mechanic": "gold",
            "hp_ratio": 0.10, "hp_base": 100, "hp_growth": 16,
            "atk_ratio": 0.10, "atk_base": 14, "atk_growth": 4,
            "def_ratio": 0.04, "def_base": 4, "def_growth": 1,
            "guard_rate": 0.05,
        },
        "青龙幼崽": {
            "desc": "神兽血脉，全面增加主人属性，协战时反哺主人",
            "attr": "all", "val": 20,
            "mechanic": "dragon",
            "hp_ratio": 0.16, "hp_base": 170, "hp_growth": 28,
            "atk_ratio": 0.14, "atk_base": 22, "atk_growth": 5,
            "def_ratio": 0.10, "def_base": 12, "def_growth": 2,
            "guard_rate": 0.12,
        },
    }

    # 灵兽进化链：Lv.20 第一次进化，Lv.40 第二次进化，无需材料。
    PET_EVOLUTIONS = {
        "火灵狐": [
            {"name": "火灵狐", "level": 1, "stat_mult": 1.0, "effect_mult": 1.0, "guard_bonus": 0.0},
            {
                "name": "炎灵狐", "level": 20, "stat_mult": 1.6, "effect_mult": 1.5,
                "guard_bonus": 0.04, "desc": "灵火凝形，灼烧与暴击大幅提升",
            },
            {
                "name": "九尾天狐", "level": 40, "stat_mult": 2.2, "effect_mult": 2.0,
                "guard_bonus": 0.08, "desc": "九尾狐火焚天，本体数值与暴击加成再次飞跃",
            },
        ],
        "冰晶兽": [
            {"name": "冰晶兽", "level": 1, "stat_mult": 1.0, "effect_mult": 1.0, "guard_bonus": 0.0},
            {
                "name": "玄冰巨兽", "level": 20, "stat_mult": 1.6, "effect_mult": 1.5,
                "guard_bonus": 0.04, "desc": "玄冰护体，防御、反伤与护主能力大幅提升",
            },
            {
                "name": "冰川巨兽", "level": 40, "stat_mult": 2.2, "effect_mult": 2.0,
                "guard_bonus": 0.08, "desc": "冰川之躯，防御、反伤与护主能力再次飞跃",
            },
        ],
        "噬金鼠": [
            {"name": "噬金鼠", "level": 1, "stat_mult": 1.0, "effect_mult": 1.0, "guard_bonus": 0.0},
            {
                "name": "寻宝金鼠", "level": 20, "stat_mult": 1.6, "effect_mult": 1.5,
                "guard_bonus": 0.04, "desc": "寻宝血脉觉醒，灵石收益显著提升",
            },
            {
                "name": "吞金貔貅", "level": 40, "stat_mult": 2.2, "effect_mult": 2.0,
                "guard_bonus": 0.08, "desc": "貔貅吞金纳财，灵石收益飞跃式提升",
            },
        ],
        "青龙幼崽": [
            {"name": "青龙幼崽", "level": 1, "stat_mult": 1.0, "effect_mult": 1.0, "guard_bonus": 0.0},
            {
                "name": "青龙", "level": 20, "stat_mult": 1.6, "effect_mult": 1.5,
                "guard_bonus": 0.04, "desc": "青龙觉醒，暴击、吸血与龙息反哺大幅提升",
            },
            {
                "name": "太古青龙", "level": 40, "stat_mult": 2.2, "effect_mult": 2.0,
                "guard_bonus": 0.08, "desc": "太古青龙血脉，暴击、吸血与龙息反哺达到巅峰",
            },
        ],
    }

    PET_TYPES = _build_pet_types(BASE_PET_TYPES, PET_EVOLUTIONS)

    MAX_PET_LEVEL = 50
    REGEN_PER_MIN = 0.02
    # 主人调息时灵兽同步按调息速率恢复精力（与玩家 /调息 的 5%/分钟一致）
    REST_REGEN_PER_MIN = 0.05
    REVIVE_BASE_SECONDS = 3600
    REVIVE_PER_LEVEL_SECONDS = 60
    EXP_SHARE_YUSOU = 0.30
    EXP_SHARE_OTHER = 0.10
    # 每级属性/被动效果统一加强倍率（1.5 = 提升 50%）
    PET_LEVEL_BONUS_MULT = 1.5
    PET_EFFECT_SCALE = 0.1 * PET_LEVEL_BONUS_MULT
    GOLD_BONUS_PER_LEVEL = 0.005 * PET_LEVEL_BONUS_MULT

    # 历练/秘境归来的灵兽奇遇：空槽越多，触发概率越高
    PET_ENCOUNTER_BASE_CHANCE = 0.50
    PET_ENCOUNTER_PER_EMPTY_SLOT = 0.15
    PET_ENCOUNTER_MAX_CHANCE = 0.95

    def __init__(self, db: DataBase, config_manager=None):
        self.db = db
        self.config_manager = config_manager

    @staticmethod
    def get_realm_tier(level_index: int) -> int:
        idx = max(0, int(level_index or 0))
        if idx >= 31:
            return idx - 23
        if idx < 10:
            return 0
        return 1 + (idx - 10) // 3

    def get_pet_slots(self, player: Player) -> int:
        # 非御兽职业固定 1 个灵兽槽；御兽随大境界提升（炼气2、筑基3、金丹4……）。
        if getattr(player, "cultivation_type", "") == "御兽":
            return 2 + self.get_realm_tier(player.level_index)
        return 1

    @staticmethod
    def stage_for_level(level: int) -> int:
        lv = max(1, int(level or 1))
        if lv >= 40:
            return 2
        if lv >= 20:
            return 1
        return 0

    def get_pet_info(self, pet: Optional[Dict]) -> Dict:
        if not pet:
            return self.PET_TYPES["火灵狐"]
        return self.PET_TYPES.get(pet.get("pet_type")) or self.PET_TYPES["火灵狐"]

    def get_pet_info_for_level(self, pet_type: str, level: int) -> Dict:
        """按等级解析当前应处于的进化阶段，未落库时也能正确计算。"""
        info = self.PET_TYPES.get(pet_type) or self.PET_TYPES["火灵狐"]
        desired = self.stage_for_level(level)
        if desired <= int(info.get("stage", 0)):
            return info
        chain = self.PET_EVOLUTIONS.get(info.get("base_type")) or []
        if not chain:
            return info
        target = chain[min(desired, len(chain) - 1)]
        return self.PET_TYPES.get(target["name"], info)

    def get_base_type(self, pet_type: str) -> str:
        info = self.PET_TYPES.get(pet_type)
        return info.get("base_type", pet_type) if info else pet_type

    @staticmethod
    def _evolved_pet_name(old_name: str, old_type: str, new_type: str) -> str:
        old_name = (old_name or "").strip()
        if not old_name or old_name == old_type:
            return new_type
        prefix = f"{old_type} "
        if old_name.startswith(prefix):
            suffix = old_name[len(prefix):]
            if suffix.isdigit():
                return f"{new_type} {suffix}"
        return old_name

    async def ensure_pet_evolution(
        self,
        pet: Optional[Dict],
        commit: bool = False,
        level: Optional[int] = None,
    ) -> Optional[Dict]:
        """达到 Lv.20 / Lv.40 时自动进化，不消耗材料。"""
        if not pet:
            return None
        info = self.PET_TYPES.get(pet.get("pet_type"))
        if not info:
            return None
        lv = max(1, int(level if level is not None else pet.get("level") or 1))
        desired = self.stage_for_level(lv)
        if desired <= int(info.get("stage", 0)):
            return None
        chain = self.PET_EVOLUTIONS.get(info.get("base_type")) or []
        if not chain:
            return None
        desired = min(desired, len(chain) - 1)
        target = chain[desired]
        old_type = pet.get("pet_type")
        new_type = target["name"]
        new_name = self._evolved_pet_name(pet.get("pet_name", ""), old_type, new_type)
        pet["pet_type"] = new_type
        pet["pet_name"] = new_name
        if pet.get("id"):
            await self.db.conn.execute(
                "UPDATE player_pets SET pet_type = ?, pet_name = ? WHERE id = ?",
                (new_type, new_name, pet["id"]),
            )
            if commit:
                await self.db.conn.commit()
        return {
            "from": old_type,
            "to": new_type,
            "level": lv,
            "stage": desired,
            "pet_name": new_name,
        }

    async def _query_pets(self, user_id: str) -> List[Dict]:
        async with self.db.conn.execute(
            "SELECT * FROM player_pets WHERE user_id = ? ORDER BY slot ASC, id ASC", (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_pets(
        self,
        user_id: str,
        apply_regen: bool = True,
        sync_exp: bool = True,
        commit: bool = True,
    ) -> List[Dict]:
        exp_changed = False
        if sync_exp:
            exp_changed = await self.sync_pet_exp(user_id)
        pets = await self._query_pets(user_id)
        if not pets:
            return []
        evolved = False
        for pet in pets:
            if await self.ensure_pet_evolution(pet, commit=False):
                evolved = True
        if apply_regen:
            regen_rate, regen_bonus = await self._regen_context(user_id)
            for pet in pets:
                await self.refresh_pet(
                    pet,
                    commit=commit,
                    regen_rate=regen_rate,
                    regen_bonus=regen_bonus,
                )
        if commit and (apply_regen or evolved or exp_changed):
            await self.db.conn.commit()
        return pets

    async def _regen_context(self, user_id: str) -> Tuple[float, float]:
        """灵兽精力恢复速率：主人调息时用调息速率，否则用自然恢复速率。"""
        resting = False
        try:
            cd = await self.db.ext.get_user_cd(user_id)
            resting = bool(cd and int(cd.type) == int(UserStatus.RESTING))
        except Exception:
            pass

        rate = self.REST_REGEN_PER_MIN if resting else self.REGEN_PER_MIN
        bonus = 0.0
        if self.config_manager is not None:
            try:
                from .recovery_manager import RecoveryManager

                player = await self.db.get_player_by_id(user_id)
                if player:
                    bonuses = await RecoveryManager(
                        self.db, self.config_manager
                    ).get_bonuses(player)
                    key = "rest_hp_regen_bonus" if resting else "natural_hp_regen_bonus"
                    bonus = float(bonuses.get(key, 0.0) or 0.0)
            except Exception:
                pass
        return rate, bonus

    async def apply_regen(
        self,
        user_id: str,
        regen_rate: Optional[float] = None,
        regen_bonus: float = 0.0,
        now: Optional[int] = None,
    ) -> None:
        """按指定速率结算一次灵兽精力恢复（供调息/自然恢复统一调用）。"""
        pets = await self._query_pets(user_id)
        if not pets:
            return
        for pet in pets:
            await self.refresh_pet(
                pet,
                commit=True,
                now=now,
                regen_rate=regen_rate,
                regen_bonus=regen_bonus,
            )
        await self.db.conn.commit()

    async def get_pet(
        self,
        user_id: str,
        apply_regen: bool = True,
        sync_exp: Optional[bool] = None,
    ) -> Optional[Dict]:
        if sync_exp is None:
            sync_exp = apply_regen
        pets = await self.get_pets(user_id, apply_regen=apply_regen, sync_exp=sync_exp, commit=apply_regen)
        if not pets:
            return None
        for pet in pets:
            if int(pet.get("is_main") or 0) == 1:
                return pet
        return pets[0]

    async def get_all_pets(self) -> Dict[str, Dict]:
        pets = {}
        async with self.db.conn.execute(
            "SELECT * FROM player_pets ORDER BY user_id, is_main DESC, slot ASC, id ASC"
        ) as cursor:
            async for row in cursor:
                pet = dict(row)
                pets.setdefault(pet["user_id"], pet)
        return pets

    async def get_all_pets_grouped(self) -> Dict[str, List[Dict]]:
        """按玩家分组返回全部灵兽，供名片/战力榜统计辅助灵兽。"""
        result: Dict[str, List[Dict]] = {}
        async with self.db.conn.execute(
            "SELECT * FROM player_pets ORDER BY user_id, is_main DESC, slot ASC, id ASC"
        ) as cursor:
            async for row in cursor:
                pet = dict(row)
                result.setdefault(pet["user_id"], []).append(pet)
        return result

    def summarize_lineup(
        self,
        player: Player,
        pets: List[Dict],
        owner_hp: int,
        owner_atk: int,
        owner_def: int,
        cultivation_type: str,
    ):
        """非战斗展示用：返回 (主人攻击, 灵兽合计攻击, 主战特效, 防御加成)。"""
        if not pets:
            return int(owner_atk), 0, {}, 0
        slots = max(1, self.get_pet_slots(player))
        main = next((p for p in pets if int(p.get("is_main") or 0) == 1), None)
        ordered = ([main] if main else []) + [p for p in pets if p is not main]
        ordered = ordered[:slots]

        states = []
        for pet in ordered:
            if int(pet.get("dead_until") or 0) > int(time.time()):
                continue
            stats = self.get_pet_stats(pet, owner_hp, owner_atk, owner_def)
            states.append((pet, stats))
        if not states:
            return int(owner_atk), 0, {}, 0

        pet_atk_mult = 1.0
        if cultivation_type == "御兽":
            count = len(states)
            pet_atk_mult = 1 + 0.05 * (count - 1)
            owner_atk = int(owner_atk * (1 + 0.03 * (count - 1)))

        total_pet_atk = sum(int(stats["atk"] * pet_atk_mult) for _, stats in states)
        main_pet = next((pet for pet, _ in states if int(pet.get("is_main") or 0) == 1), states[0][0])
        effects = self.get_pet_combat_effects(main_pet)
        defense_bonus = int(effects.get("defense_bonus", 0) or 0)
        return int(owner_atk), total_pet_atk, effects, defense_bonus

    async def get_max_pet_level(self, user_id: str) -> int:
        async with self.db.conn.execute(
            "SELECT MAX(level) FROM player_pets WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row and row[0] else 0

    async def sync_pet_exp(self, user_id: str, player: Optional[Player] = None) -> bool:
        if player is not None:
            experience = int(player.experience or 0)
            cultivation_type = player.cultivation_type
        else:
            async with self.db.conn.execute(
                "SELECT experience, cultivation_type FROM players WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return False
            experience = int(row[0] or 0)
            cultivation_type = row[1]

        key = f"pet_exp_ck_{user_id}"
        checkpoint_raw = await self.db.ext.get_system_config(key)
        if checkpoint_raw is None or not str(checkpoint_raw).isdigit():
            await self.db.ext.set_system_config(key, str(experience))
            return False

        checkpoint = int(checkpoint_raw)
        delta = experience - checkpoint
        if delta <= 0:
            if delta < 0:
                await self.db.ext.set_system_config(key, str(experience))
            return False

        pets = await self._query_pets(user_id)
        if not pets:
            await self.db.ext.set_system_config(key, str(experience))
            return False

        eligible = [p for p in pets if int(p.get("level") or 1) < self.MAX_PET_LEVEL]
        share = self.EXP_SHARE_YUSOU if cultivation_type == "御兽" else self.EXP_SHARE_OTHER
        gain = int(delta * share)
        if gain <= 0 or not eligible:
            await self.db.ext.set_system_config(key, str(experience))
            return False

        # 全体灵兽各获得等量经验，不再按灵兽数量平分。
        for pet in eligible:
            level = int(pet.get("level") or 1)
            exp = int(pet.get("exp") or 0) + gain
            while level < self.MAX_PET_LEVEL and exp >= level * 500:
                exp -= level * 500
                level += 1
            if level >= self.MAX_PET_LEVEL:
                exp = 0
            await self.ensure_pet_evolution(pet, commit=False, level=level)
            await self.db.conn.execute(
                "UPDATE player_pets SET level = ?, exp = ? WHERE id = ?",
                (level, exp, pet["id"]),
            )
        await self.db.ext.set_system_config(key, str(experience))
        return True

    async def _persist_pet(self, pet: Dict):
        if pet.get("id"):
            await self.db.conn.execute(
                "UPDATE player_pets SET max_hp = ?, hp = ?, last_regen_time = ?, dead_until = ? WHERE id = ?",
                (
                    int(pet.get("max_hp") or 0),
                    int(pet.get("hp") or 0),
                    int(pet.get("last_regen_time") or 0),
                    int(pet.get("dead_until") or 0),
                    pet["id"],
                ),
            )
        else:
            await self.db.conn.execute(
                "UPDATE player_pets SET max_hp = ?, hp = ?, last_regen_time = ?, dead_until = ? WHERE user_id = ?",
                (
                    int(pet.get("max_hp") or 0),
                    int(pet.get("hp") or 0),
                    int(pet.get("last_regen_time") or 0),
                    int(pet.get("dead_until") or 0),
                    pet["user_id"],
                ),
            )
        await self.db.conn.commit()

    def _flat_stats(self, level: int, pet_type: str) -> Dict[str, int]:
        info = self.get_pet_info_for_level(pet_type, level)
        growth = level * self.PET_LEVEL_BONUS_MULT
        return {
            "max_hp": max(1, int(info["hp_base"] + info["hp_growth"] * growth)),
            "atk": max(1, int(info["atk_base"] + info["atk_growth"] * growth)),
            "defense": max(0, int(info["def_base"] + info["def_growth"] * growth)),
        }

    @staticmethod
    def _owner_realm_stats(player) -> tuple:
        """灵兽成长只跟随境界基础，不跟囤积修为或穿戴装备。"""
        from ..core.equipment_balance import realm_attributes
        base = realm_attributes(player)
        return base["max_hp"], base["physical_damage"] + base["magic_damage"]

    def get_pet_stats(
        self,
        pet: Dict,
        owner_hp: int = 0,
        owner_atk: int = 0,
        owner_def: int = 0,
    ) -> Dict[str, int]:
        level = max(1, int(pet.get("level") or 1))
        info = self.get_pet_info_for_level(pet.get("pet_type"), level)
        growth = level * self.PET_LEVEL_BONUS_MULT
        max_hp = int(owner_hp * info["hp_ratio"]) + info["hp_base"] + int(info["hp_growth"] * growth)
        atk = int(owner_atk * info["atk_ratio"]) + info["atk_base"] + int(info["atk_growth"] * growth)
        defense = int(owner_def * info["def_ratio"]) + info["def_base"] + int(info["def_growth"] * growth)
        return {
            "max_hp": max(1, max_hp),
            "atk": max(1, atk),
            "defense": max(0, defense),
        }

    def revive_seconds(self, level: int, revive_mult: float = 1.0) -> int:
        base = self.REVIVE_BASE_SECONDS + max(1, int(level)) * self.REVIVE_PER_LEVEL_SECONDS
        return max(60, int(base * revive_mult))

    async def sync_pet_stats(
        self,
        pet: Dict,
        owner_hp: int = 0,
        owner_atk: int = 0,
        owner_def: int = 0,
        commit: bool = True,
    ) -> Dict:
        stats = self.get_pet_stats(pet, owner_hp, owner_atk, owner_def)
        old_max = int(pet.get("max_hp") or 0)
        old_hp = int(pet.get("hp") or 0)
        last_regen = int(pet.get("last_regen_time") or 0)
        new_max = stats["max_hp"]
        new_hp = min(old_hp, new_max)
        if old_max <= 0 and last_regen <= 0 and int(pet.get("dead_until") or 0) <= int(time.time()):
            new_hp = new_max
        if new_max != old_max or new_hp != old_hp:
            pet["max_hp"] = new_max
            pet["hp"] = new_hp
            if commit:
                await self._persist_pet(pet)
        return pet

    async def refresh_pet(
        self,
        pet: Dict,
        commit: bool = True,
        now: Optional[int] = None,
        regen_rate: Optional[float] = None,
        regen_bonus: float = 0.0,
    ) -> Dict:
        now = int(now or time.time())
        rate = self.REGEN_PER_MIN if regen_rate is None else float(regen_rate)
        level = max(1, int(pet.get("level") or 1))
        max_hp = int(pet.get("max_hp") or 0)
        changed = False
        if max_hp <= 0:
            max_hp = self._flat_stats(level, pet.get("pet_type"))["max_hp"]
            pet["max_hp"] = max_hp
            changed = True

        dead_until = int(pet.get("dead_until") or 0)
        if dead_until > now:
            if int(pet.get("hp") or 0) != 0:
                pet["hp"] = 0
                changed = True
            if changed and commit:
                await self._persist_pet(pet)
            return pet

        if dead_until and dead_until <= now:
            pet["hp"] = max(1, max_hp // 2)
            pet["dead_until"] = 0
            pet["last_regen_time"] = now
            if commit:
                await self._persist_pet(pet)
            return pet

        hp = int(pet.get("hp") or 0)
        last = int(pet.get("last_regen_time") or 0)
        if last <= 0:
            pet["hp"] = max_hp
            pet["last_regen_time"] = now
            if commit:
                await self._persist_pet(pet)
            return pet

        if hp < max_hp:
            elapsed = now - last
            if elapsed >= 60:
                regen = max(
                    1,
                    int(max_hp * rate * (1 + float(regen_bonus or 0.0)) * (elapsed // 60)),
                )
                pet["hp"] = min(max_hp, hp + regen)
                pet["last_regen_time"] = now - (elapsed % 60)
                changed = True

        if changed and commit:
            await self._persist_pet(pet)
        return pet

    def _select_pet(self, pets: List[Dict], selector: str = "") -> Tuple[Optional[Dict], str]:
        if not pets:
            return None, "❌ 你还没有灵兽，请先使用 /孵化灵兽 契约。"
        selector = (selector or "").strip()
        if not selector:
            for pet in pets:
                if int(pet.get("is_main") or 0) == 1:
                    return pet, ""
            return pets[0], ""
        if selector.isdigit():
            index = int(selector)
            if 1 <= index <= len(pets):
                return pets[index - 1], ""
            return None, f"❌ 没有第 {index} 只灵兽，请发送 /我的灵兽 查看。"
        for pet in pets:
            if pet.get("pet_name") == selector:
                return pet, ""
        for pet in pets:
            if pet.get("pet_type") == selector:
                return pet, ""
        for pet in pets:
            if self.get_base_type(pet.get("pet_type")) == selector:
                return pet, ""
        return None, f"❌ 没有名为【{selector}】的灵兽。"

    async def adopt_pet(self, player: Player, pet_name: str) -> Tuple[bool, str]:
        if pet_name not in self.BASE_PET_TYPES:
            return False, f"❌ 未知的灵兽种类！可用灵兽：{', '.join(self.BASE_PET_TYPES.keys())}"

        await self.sync_pet_exp(player.user_id, player)
        pets = await self._query_pets(player.user_id)
        slots = self.get_pet_slots(player)
        if len(pets) >= slots:
            return False, (
                f"❌ 灵兽槽位已满（{len(pets)}/{slots}）。\n"
                f"💡 非御兽职业固定 1 个灵兽槽；御兽槽位随大境界提升。"
            )

        used_slots = {int(p.get("slot") or 0) for p in pets}
        slot = next((i for i in range(1, slots + 1) if i not in used_slots), None)
        if slot is None:
            return False, "❌ 灵兽槽位数据异常，请先整理灵兽栏后再试。"

        is_first_yusou = (
            getattr(player, "cultivation_type", "") == "御兽" and not pets
        )
        cost = 0 if is_first_yusou else 100000
        if player.gold < cost:
            return False, f"❌ 捕捉【{pet_name}】需要 {cost:,} 灵石。"

        player.gold -= cost
        await self.db.update_player(player)

        used_names = {p.get("pet_name") for p in pets}
        same_type = sum(1 for p in pets if self.get_base_type(p.get("pet_type")) == pet_name)
        suffix = same_type + 1
        display_name = f"{pet_name} {suffix}"
        while display_name in used_names:
            suffix += 1
            display_name = f"{pet_name} {suffix}"

        level = 1
        owner_hp, owner_atk = self._owner_realm_stats(player)
        stats = self.get_pet_stats(
            {"level": level, "pet_type": pet_name}, owner_hp, owner_atk, 0
        )
        now = int(time.time())
        is_main = 1 if not any(int(p.get("is_main") or 0) == 1 for p in pets) else 0
        await self.db.conn.execute(
            "INSERT INTO player_pets "
            "(user_id, pet_name, pet_type, level, exp, max_hp, hp, last_regen_time, dead_until, slot, is_main) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?)",
            (
                player.user_id, display_name, pet_name, level,
                stats["max_hp"], stats["max_hp"], now, slot, is_main,
            ),
        )
        await self.db.conn.commit()

        pet_info = self.PET_TYPES[pet_name]
        chain = self.PET_EVOLUTIONS.get(pet_name, [])
        evolution_line = (
            f"\n进化：{' → '.join(stage['name'] for stage in chain)}（Lv20 / Lv40 自动进化）"
            if len(chain) > 1 else ""
        )
        free_note = "\n🎁 御兽职业首次契约免费！" if is_first_yusou else ""
        return True, (
            f"🎉 恭喜！你成功契约了【{display_name}】（槽位 {slot}/{slots}）！{free_note}\n"
            f"特性：{pet_info['desc']}{evolution_line}\n"
            f"精力：{stats['max_hp']} · 攻击：{stats['atk']} · 防御：{stats['defense']}\n"
            f"💡 灵兽每回合都会协战，精力耗尽后会倒下，需等待较长时间复活。"
        )

    async def grant_pet(
        self,
        player: Player,
        pet_name: str,
        level: int = 1,
        free: bool = True,
        commit: bool = True,
    ) -> Tuple[bool, str, Optional[Dict]]:
        """直接授予灵兽（奇遇/活动用），默认免费，占用当前空槽。"""
        if pet_name not in self.BASE_PET_TYPES:
            return False, f"❌ 未知的灵兽种类：{pet_name}", None

        pets = await self._query_pets(player.user_id)
        slots = self.get_pet_slots(player)
        if len(pets) >= slots:
            return False, f"❌ 灵兽槽位已满（{len(pets)}/{slots}）。", None

        cost = 0 if free else 100000
        if player.gold < cost:
            return False, f"❌ 捕捉【{pet_name}】需要 {cost:,} 灵石。", None

        used_slots = {int(p.get("slot") or 0) for p in pets}
        slot = next((i for i in range(1, slots + 1) if i not in used_slots), None)
        if slot is None:
            return False, "❌ 灵兽槽位数据异常，请先整理灵兽栏后再试。", None

        level = max(1, min(self.MAX_PET_LEVEL, int(level or 1)))
        stage = self.stage_for_level(level)
        chain = self.PET_EVOLUTIONS.get(pet_name) or []
        stage_info = chain[min(stage, len(chain) - 1)] if chain else {"name": pet_name}
        pet_type = stage_info["name"]

        used_names = {p.get("pet_name") for p in pets}
        same_type = sum(1 for p in pets if self.get_base_type(p.get("pet_type")) == pet_name)
        suffix = same_type + 1
        display_name = f"{pet_type} {suffix}"
        while display_name in used_names:
            suffix += 1
            display_name = f"{pet_type} {suffix}"

        owner_hp, owner_atk = self._owner_realm_stats(player)
        stats = self.get_pet_stats(
            {"level": level, "pet_type": pet_type}, owner_hp, owner_atk, 0
        )
        now = int(time.time())
        is_main = 1 if not any(int(p.get("is_main") or 0) == 1 for p in pets) else 0

        if cost:
            player.gold -= cost
            await self.db.update_player(player, commit=commit)
        await self.db.conn.execute(
            "INSERT INTO player_pets "
            "(user_id, pet_name, pet_type, level, exp, max_hp, hp, last_regen_time, dead_until, slot, is_main) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?)",
            (
                player.user_id, display_name, pet_type, level,
                stats["max_hp"], stats["max_hp"], now, slot, is_main,
            ),
        )
        if commit:
            await self.db.conn.commit()

        info = {
            "pet_name": display_name,
            "pet_type": pet_type,
            "base_type": pet_name,
            "stage": stage,
            "level": level,
            "slot": slot,
            "slots": slots,
            "is_main": bool(is_main),
            "free": bool(free),
            "stats": stats,
        }
        return True, f"🎁 你获得了【{display_name}】（Lv.{level}）！", info

    def pet_encounter_chance(self, empty_slots: int) -> float:
        empty = max(0, int(empty_slots))
        return min(
            self.PET_ENCOUNTER_MAX_CHANCE,
            self.PET_ENCOUNTER_BASE_CHANCE + self.PET_ENCOUNTER_PER_EMPTY_SLOT * empty,
        )

    async def try_exploration_encounter(
        self,
        player: Player,
        source: str = "历练途中",
        commit: bool = True,
    ) -> Optional[Dict]:
        """历练/秘境结算时判定灵兽奇遇：有空槽才触发，空槽越多概率越高。"""
        if not player:
            return None
        pets = await self._query_pets(player.user_id)
        slots = self.get_pet_slots(player)
        empty = slots - len(pets)
        if empty <= 0:
            return None
        chance = self.pet_encounter_chance(empty)
        if random.random() >= chance:
            return None

        pet_type = random.choice(list(self.BASE_PET_TYPES.keys()))
        level = max(1, int(getattr(player, "level_index", 0) or 0) // 5)
        ok, _, info = await self.grant_pet(
            player, pet_type, level=level, free=True, commit=commit
        )
        if not ok or not info:
            return None
        result = dict(info)
        result.update({"source": source, "empty_before": empty, "chance": chance})
        return result

    @staticmethod
    def format_exploration_encounter(info: Optional[Dict]) -> str:
        if not info:
            return ""
        source = info.get("source", "探索")
        main_note = "，并自动设为主战灵兽" if info.get("is_main") else ""
        return (
            f"\n\n🐾【灵兽奇遇】\n"
            f"你在{source}遇到一只【{info['pet_name']}】，它主动与你缔结了契约{main_note}！\n"
            f"等级：Lv.{info['level']} · 槽位 {info['slot']}/{info['slots']}\n"
            f"💡 本次契约免费，发送 /我的灵兽 查看"
        )

    async def set_main_pet(self, user_id: str, pet_id: int) -> Tuple[bool, str]:
        pets = await self._query_pets(user_id)
        target = next((p for p in pets if int(p.get("id") or 0) == int(pet_id)), None)
        if not target:
            return False, "❌ 找不到该灵兽。"
        await self.db.conn.execute("UPDATE player_pets SET is_main = 0 WHERE user_id = ?", (user_id,))
        await self.db.conn.execute(
            "UPDATE player_pets SET is_main = 1 WHERE id = ? AND user_id = ?", (pet_id, user_id)
        )
        await self.db.conn.commit()
        return True, f"✅ 【{target['pet_name']}】已设为出战主战灵兽。"

    async def rename_pet(self, user_id: str, pet_id: int, new_name: str) -> Tuple[bool, str]:
        new_name = (new_name or "").strip()
        if not new_name:
            return False, "❌ 新名字不能为空。"
        if len(new_name) > 16:
            return False, "❌ 名字最多 16 个字符。"
        pets = await self._query_pets(user_id)
        target = next((p for p in pets if int(p.get("id") or 0) == int(pet_id)), None)
        if not target:
            return False, "❌ 找不到该灵兽。"
        if any(p.get("pet_name") == new_name and int(p.get("id") or 0) != int(pet_id) for p in pets):
            return False, "❌ 你已经有一只叫这个名字的灵兽了。"
        old_name = target["pet_name"]
        await self.db.conn.execute(
            "UPDATE player_pets SET pet_name = ? WHERE id = ? AND user_id = ?",
            (new_name, pet_id, user_id),
        )
        await self.db.conn.commit()
        return True, f"✅ 【{old_name}】已更名为【{new_name}】。"

    async def _promote_main_if_needed(self, user_id: str, removed_id: int):
        pets = await self._query_pets(user_id)
        if not pets or any(int(p.get("is_main") or 0) == 1 for p in pets):
            return
        await self.db.conn.execute(
            "UPDATE player_pets SET is_main = 1 WHERE id = ?", (pets[0]["id"],)
        )

    async def release_pet(self, player: Player, selector: str = "") -> Tuple[bool, str]:
        pets = await self.get_pets(player.user_id)
        if len(pets) > 1 and not (selector or "").strip():
            return False, "❌ 你有多只灵兽，请指定要放生的灵兽（序号或名字）。"
        pet, error = self._select_pet(pets, selector)
        if not pet:
            return False, error

        pet_name = pet["pet_name"]
        await self.db.conn.execute("DELETE FROM player_pets WHERE id = ?", (pet["id"],))
        await self._promote_main_if_needed(player.user_id, pet["id"])
        await self.db.conn.commit()
        return True, f"🕊️ 你已解开契约，将【{pet_name}】放归山林。\n当前灵兽栏剩余 {max(0, len(pets) - 1)} 只。"

    async def devour_pet(self, player: Player, selector: str = "") -> Tuple[bool, str]:
        pets = await self.get_pets(player.user_id)
        if len(pets) > 1 and not (selector or "").strip():
            return False, "❌ 你有多只灵兽，请指定要吞噬的灵兽（序号或名字）。"
        pet, error = self._select_pet(pets, selector)
        if not pet:
            return False, error

        pet_name = pet["pet_name"]
        level = pet["level"]
        refund_gold = level * 500
        refund_exp = level * 250

        player.gold += refund_gold
        player.experience += refund_exp

        await self.db.conn.execute("DELETE FROM player_pets WHERE id = ?", (pet["id"],))
        await self._promote_main_if_needed(player.user_id, pet["id"])
        await self.db.update_player(player)
        await self.db.conn.commit()

        return True, (
            f"🩸 你运转魔功，强行吞噬了【{pet_name}】！\n"
            f"炼化获得：{refund_gold:,} 灵石，{refund_exp:,} 修为。\n"
            f"（灵兽栏剩余 {max(0, len(pets) - 1)} 只）"
        )

    async def feed_pet(
        self, player: Player, selector: str = "", cost_mult: float = 1.0
    ) -> Tuple[bool, str]:
        pets = await self.get_pets(player.user_id)
        pet, error = self._select_pet(pets, selector)
        if not pet:
            return False, error

        now = int(time.time())
        dead_until = int(pet.get("dead_until") or 0)
        if dead_until > now:
            remain = dead_until - now
            return False, f"❌ 【{pet['pet_name']}】正在复活中，还需 {remain // 60} 分 {remain % 60} 秒，暂时无法喂养。"

        if pet["level"] >= self.MAX_PET_LEVEL:
            return False, f"❌ 【{pet['pet_name']}】已经满级（Lv.{self.MAX_PET_LEVEL}）。"

        if getattr(player, "cultivation_type", "") == "御兽":
            cost_mult *= 0.8

        cost_gold = int(pet["level"] * 1000 * cost_mult)

        if player.gold < cost_gold:
            return False, f"❌ 灵石不足，喂养需要 {cost_gold:,} 灵石。"

        player.gold -= cost_gold

        new_exp = pet["exp"] + 100
        new_level = pet["level"]
        while new_level < self.MAX_PET_LEVEL and new_exp >= new_level * 500:
            new_exp -= new_level * 500
            new_level += 1
        if new_level >= self.MAX_PET_LEVEL:
            new_exp = 0

        evolution = await self.ensure_pet_evolution(pet, commit=False, level=new_level)
        owner_hp, owner_atk = self._owner_realm_stats(player)
        stats = self.get_pet_stats(
            {"level": new_level, "pet_type": pet["pet_type"]}, owner_hp, owner_atk, 0
        )
        old_max = int(pet.get("max_hp") or 0)
        new_hp = min(
            stats["max_hp"],
            max(0, int(pet.get("hp") or 0)) + max(0, stats["max_hp"] - old_max) + max(1, stats["max_hp"] // 20),
        )

        await self.db.conn.execute(
            "UPDATE player_pets SET level = ?, exp = ?, max_hp = ?, hp = ?, last_regen_time = ? WHERE id = ?",
            (new_level, new_exp, stats["max_hp"], new_hp, now, pet["id"]),
        )
        await self.db.update_player(player)
        await self.db.conn.commit()

        discount_note = "\n🐾 御兽职业喂养消耗 -20%" if getattr(player, "cultivation_type", "") == "御兽" else ""
        evolve_note = ""
        if evolution:
            evolve_note = (
                f"\n✨ 境界突破！【{evolution['from']}】进化为【{evolution['to']}】，"
                f"本体数值与加成效果大幅提升！"
            )
        return True, (
            f"✨ 喂养【{pet['pet_name']}】成功！{discount_note}{evolve_note}\n"
            f"消耗灵石：{cost_gold:,}（不消耗修为）\n"
            f"剩余灵石：{player.gold:,}\n"
            f"灵兽当前等级：Lv.{new_level} (经验：{new_exp}/{new_level * 500})\n"
            f"当前精力：{new_hp}/{stats['max_hp']} · 攻击：{stats['atk']} · 防御：{stats['defense']}"
        )

    def get_pet_combat_effects(self, pet: Optional[Dict]) -> dict:
        if not pet:
            return {}

        if int(pet.get("dead_until") or 0) > int(time.time()):
            return {}

        level = max(1, int(pet.get("level") or 1))
        pet_info = self.get_pet_info_for_level(pet.get("pet_type"), level)
        if not pet_info:
            return {}

        base_val = pet_info["val"] * level * self.PET_EFFECT_SCALE

        effects = {}
        if pet_info["attr"] == "crit_rate":
            effects["crit_rate"] = base_val
        elif pet_info["attr"] == "defense":
            effects["defense_bonus"] = base_val
            effects["counter_damage"] = min(
                100.0,
                level * 0.2 * self.PET_LEVEL_BONUS_MULT * float(pet_info.get("effect_mult", 1.0)),
            )
        elif pet_info["attr"] == "all":
            effects["crit_rate"] = base_val
            effects["lifesteal"] = base_val / 2

        return effects

    def get_life_bonus_from_pets(self, pets: Optional[List[Dict]], attr: str = "gold_bonus") -> float:
        """统计所有存活灵兽的生活类加成，同类效果按每只灵兽累加。"""
        total = 0.0
        now = int(time.time())
        for pet in pets or []:
            if not pet:
                continue
            if int(pet.get("dead_until") or 0) > now:
                continue
            level = max(1, int(pet.get("level") or 1))
            pet_info = self.get_pet_info_for_level(pet.get("pet_type"), level)
            if not pet_info or pet_info.get("attr") != attr:
                continue
            if attr == "gold_bonus":
                per_level = self.GOLD_BONUS_PER_LEVEL * float(pet_info.get("effect_mult", 1.0))
                total += float(pet_info["val"]) + max(0, level - 1) * per_level
        return total

    async def get_life_bonus(self, user_id: str, attr: str = "gold_bonus") -> float:
        pets = await self._query_pets(user_id)
        return self.get_life_bonus_from_pets(pets, attr)

    def get_gold_bonus(self, pet: Optional[Dict]) -> float:
        return self.get_life_bonus_from_pets([pet] if pet else [], "gold_bonus")

    async def build_pet_combat_state(
        self,
        pet: Dict,
        owner_hp: int,
        owner_atk: int,
        owner_def: int = 0,
        commit: bool = True,
        revive_mult: float = 1.0,
    ):
        now = int(time.time())
        if int(pet.get("dead_until") or 0) > now:
            return None

        await self.ensure_pet_evolution(pet, commit=commit)
        stats = self.get_pet_stats(pet, owner_hp, owner_atk, owner_def)
        max_hp = stats["max_hp"]
        hp = int(pet.get("hp") or 0)
        if hp <= 0:
            if int(pet.get("dead_until") or 0) > 0:
                return None
            hp = max_hp

        hp = min(hp, max_hp)
        if int(pet.get("max_hp") or 0) != max_hp or int(pet.get("hp") or 0) != hp:
            pet["max_hp"] = max_hp
            pet["hp"] = hp
            if commit:
                await self._persist_pet(pet)

        from .combat_manager import PetCombatState

        info = self.PET_TYPES.get(pet.get("pet_type")) or self.PET_TYPES["火灵狐"]
        return PetCombatState(
            pet_id=int(pet.get("id") or 0),
            user_id=pet["user_id"],
            name=pet["pet_name"],
            pet_type=pet["pet_type"],
            level=max(1, int(pet.get("level") or 1)),
            hp=hp,
            max_hp=max_hp,
            atk=stats["atk"],
            defense=stats["defense"],
            guard_rate=float(info.get("guard_rate", 0.15)),
            mechanic=info.get("mechanic", ""),
            revive_mult=revive_mult,
        )

    async def build_pet_lineup(
        self,
        user_id: str,
        owner_hp: int,
        owner_atk: int,
        owner_def: int,
        cultivation_type: str,
        persist: bool = True,
    ):
        pets = await self.get_pets(
            user_id, apply_regen=persist, sync_exp=persist, commit=persist
        )
        if not pets:
            return None, [], {}, owner_atk

        # 出战数量不得超过当前境界槽位；超过时优先保留主战灵兽。
        player = await self.db.get_player_by_id(user_id)
        if player:
            slots = max(1, self.get_pet_slots(player))
            if len(pets) > slots:
                main_pet = next((p for p in pets if int(p.get("is_main") or 0) == 1), None)
                ordered = ([main_pet] if main_pet else []) + [
                    p for p in pets if p is not main_pet
                ]
                pets = ordered[:slots]

        revive_mult = 0.7 if cultivation_type == "御兽" else 1.0
        states = []
        for pet in pets:
            state = await self.build_pet_combat_state(
                pet, owner_hp, owner_atk, owner_def,
                commit=persist, revive_mult=revive_mult,
            )
            if state:
                states.append((pet, state))
        if not states:
            return None, [], {}, owner_atk

        pet_bonus = 0.0
        try:
            from .social_manager import SocialManager
            pet_bonus = await SocialManager(self.db).companion_bonus_async(user_id, "pet")
        except Exception:
            pet_bonus = 0.0
        if pet_bonus > 0:
            for _, state in states:
                state.max_hp = max(1, int(state.max_hp * (1 + pet_bonus)))
                state.hp = min(state.max_hp, max(1, int(state.hp * (1 + pet_bonus))))
                state.atk = max(1, int(state.atk * (1 + pet_bonus)))
                state.defense = max(0, int(state.defense * (1 + pet_bonus)))

        if cultivation_type == "御兽":
            count = len(states)
            pet_damage_mult = 1 + 0.05 * (count - 1)
            owner_atk_mult = 1 + 0.03 * (count - 1)
            for _, state in states:
                state.atk = int(state.atk * pet_damage_mult)
            owner_atk = int(owner_atk * owner_atk_mult)

        main_index = 0
        for index, (pet, _) in enumerate(states):
            if int(pet.get("is_main") or 0) == 1:
                main_index = index
                break
        effects = self.get_pet_combat_effects(states[main_index][0])
        main_state = states[main_index][1]
        support_states = [state for index, (_, state) in enumerate(states) if index != main_index]
        return main_state, support_states, effects, owner_atk

    async def save_pet_combat_state(self, state, commit: bool = True):
        if not state:
            return
        now = int(time.time())
        if state.hp <= 0:
            hp = 0
            dead_until = now + self.revive_seconds(
                state.level, getattr(state, "revive_mult", 1.0)
            )
        else:
            hp = min(int(state.max_hp), max(0, int(state.hp)))
            dead_until = 0
        pet_id = int(getattr(state, "pet_id", 0) or 0)
        if pet_id:
            await self.db.conn.execute(
                "UPDATE player_pets SET max_hp = ?, hp = ?, dead_until = ?, last_regen_time = ? WHERE id = ?",
                (int(state.max_hp), hp, dead_until, now, pet_id),
            )
        else:
            await self.db.conn.execute(
                "UPDATE player_pets SET max_hp = ?, hp = ?, dead_until = ?, last_regen_time = ? WHERE user_id = ?",
                (int(state.max_hp), hp, dead_until, now, state.user_id),
            )
        if commit:
            await self.db.conn.commit()
