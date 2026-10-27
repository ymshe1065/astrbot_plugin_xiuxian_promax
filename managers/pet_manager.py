# managers/pet_manager.py
import time
from typing import Tuple, Dict, Optional, List
from ..data import DataBase
from ..models import Player

class PetManager:
    """灵兽管理器"""

    PET_TYPES = {
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

    MAX_PET_LEVEL = 50
    REGEN_PER_MIN = 0.02
    REVIVE_BASE_SECONDS = 3600
    REVIVE_PER_LEVEL_SECONDS = 60
    EXP_SHARE_YUSOU = 0.30
    EXP_SHARE_OTHER = 0.10

    def __init__(self, db: DataBase):
        self.db = db

    @staticmethod
    def get_realm_tier(level_index: int) -> int:
        idx = max(0, int(level_index or 0))
        if idx >= 31:
            return idx - 23
        if idx < 10:
            return 0
        return 1 + (idx - 10) // 3

    def get_pet_slots(self, player: Player) -> int:
        slots = 1 + self.get_realm_tier(player.level_index)
        if getattr(player, "cultivation_type", "") == "御兽":
            slots += 1
        return slots

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
        if sync_exp:
            await self.sync_pet_exp(user_id)
        pets = await self._query_pets(user_id)
        if not pets:
            return []
        if apply_regen:
            for pet in pets:
                await self.refresh_pet(pet, commit=False)
            if commit:
                await self.db.conn.commit()
        return pets

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

    async def get_max_pet_level(self, user_id: str) -> int:
        async with self.db.conn.execute(
            "SELECT MAX(level) FROM player_pets WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row and row[0] else 0

    async def sync_pet_exp(self, user_id: str, player: Optional[Player] = None):
        if player is not None:
            experience = int(player.experience or 0)
            cultivation_type = player.cultivation_type
        else:
            async with self.db.conn.execute(
                "SELECT experience, cultivation_type FROM players WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return
            experience = int(row[0] or 0)
            cultivation_type = row[1]

        key = f"pet_exp_ck_{user_id}"
        checkpoint_raw = await self.db.ext.get_system_config(key)
        if checkpoint_raw is None or not str(checkpoint_raw).isdigit():
            await self.db.ext.set_system_config(key, str(experience))
            return

        checkpoint = int(checkpoint_raw)
        delta = experience - checkpoint
        if delta <= 0:
            if delta < 0:
                await self.db.ext.set_system_config(key, str(experience))
            return

        pets = await self._query_pets(user_id)
        if not pets:
            await self.db.ext.set_system_config(key, str(experience))
            return

        eligible = [p for p in pets if int(p.get("level") or 1) < self.MAX_PET_LEVEL]
        share = self.EXP_SHARE_YUSOU if cultivation_type == "御兽" else self.EXP_SHARE_OTHER
        total = int(delta * share)
        if total <= 0 or not eligible:
            await self.db.ext.set_system_config(key, str(experience))
            return

        base = total // len(eligible)
        remainder = total % len(eligible)
        for index, pet in enumerate(eligible):
            gain = base + (1 if index < remainder else 0)
            if gain <= 0:
                continue
            level = int(pet.get("level") or 1)
            exp = int(pet.get("exp") or 0) + gain
            while level < self.MAX_PET_LEVEL and exp >= level * 500:
                exp -= level * 500
                level += 1
            if level >= self.MAX_PET_LEVEL:
                exp = 0
            await self.db.conn.execute(
                "UPDATE player_pets SET level = ?, exp = ? WHERE id = ?",
                (level, exp, pet["id"]),
            )
        await self.db.ext.set_system_config(key, str(experience))

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
        info = self.PET_TYPES.get(pet_type) or self.PET_TYPES["火灵狐"]
        return {
            "max_hp": max(1, int(info["hp_base"] + info["hp_growth"] * level)),
            "atk": max(1, int(info["atk_base"] + info["atk_growth"] * level)),
            "defense": max(0, int(info["def_base"] + info["def_growth"] * level)),
        }

    def get_pet_stats(
        self,
        pet: Dict,
        owner_hp: int = 0,
        owner_atk: int = 0,
        owner_def: int = 0,
    ) -> Dict[str, int]:
        info = self.PET_TYPES.get(pet.get("pet_type")) or self.PET_TYPES["火灵狐"]
        level = max(1, int(pet.get("level") or 1))
        max_hp = int(owner_hp * info["hp_ratio"]) + info["hp_base"] + info["hp_growth"] * level
        atk = int(owner_atk * info["atk_ratio"]) + info["atk_base"] + info["atk_growth"] * level
        defense = int(owner_def * info["def_ratio"]) + info["def_base"] + info["def_growth"] * level
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

    async def refresh_pet(self, pet: Dict, commit: bool = True) -> Dict:
        now = int(time.time())
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
                regen = max(1, int(max_hp * self.REGEN_PER_MIN * (elapsed // 60)))
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
        return None, f"❌ 没有名为【{selector}】的灵兽。"

    async def adopt_pet(self, player: Player, pet_name: str) -> Tuple[bool, str]:
        if pet_name not in self.PET_TYPES:
            return False, f"❌ 未知的灵兽种类！可用灵兽：{', '.join(self.PET_TYPES.keys())}"

        await self.sync_pet_exp(player.user_id, player)
        pets = await self._query_pets(player.user_id)
        slots = self.get_pet_slots(player)
        if len(pets) >= slots:
            return False, (
                f"❌ 灵兽槽位已满（{len(pets)}/{slots}）。\n"
                f"💡 提升大境界可解锁更多槽位；御兽职业额外 +1 槽位。"
            )

        used_slots = {int(p.get("slot") or 0) for p in pets}
        slot = next(i for i in range(1, slots + 1) if i not in used_slots)

        is_first_yusou = (
            getattr(player, "cultivation_type", "") == "御兽" and not pets
        )
        cost = 0 if is_first_yusou else 100000
        if player.gold < cost:
            return False, f"❌ 捕捉【{pet_name}】需要 {cost:,} 灵石。"

        player.gold -= cost
        await self.db.update_player(player)

        used_names = {p.get("pet_name") for p in pets}
        same_type = sum(1 for p in pets if p.get("pet_type") == pet_name)
        suffix = same_type + 1
        display_name = f"{pet_name} {suffix}"
        while display_name in used_names:
            suffix += 1
            display_name = f"{pet_name} {suffix}"

        level = 1
        owner_hp = max(1, int(player.hp or 0), player.experience // 2)
        owner_atk = max(1, int(player.atk or 0), player.experience // 10)
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
        free_note = "\n🎁 御兽职业首次契约免费！" if is_first_yusou else ""
        return True, (
            f"🎉 恭喜！你成功契约了【{display_name}】（槽位 {slot}/{slots}）！{free_note}\n"
            f"特性：{pet_info['desc']}\n"
            f"精力：{stats['max_hp']} · 攻击：{stats['atk']} · 防御：{stats['defense']}\n"
            f"💡 灵兽每回合都会协战，精力耗尽后会倒下，需等待较长时间复活。"
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
        cost_exp = int(pet["level"] * 500 * cost_mult)

        if player.gold < cost_gold:
            return False, f"❌ 灵石不足，喂养需要 {cost_gold:,} 灵石。"
        if player.experience < cost_exp:
            return False, f"❌ 修为不足，喂养需要 {cost_exp:,} 修为（灵兽会吞噬你的修为）。"

        player.gold -= cost_gold
        player.experience -= cost_exp

        new_exp = pet["exp"] + 100
        new_level = pet["level"]
        while new_level < self.MAX_PET_LEVEL and new_exp >= new_level * 500:
            new_exp -= new_level * 500
            new_level += 1
        if new_level >= self.MAX_PET_LEVEL:
            new_exp = 0

        owner_hp = max(1, int(player.hp or 0), player.experience // 2)
        owner_atk = max(1, int(player.atk or 0), player.experience // 10)
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
        return True, (
            f"✨ 喂养【{pet['pet_name']}】成功！{discount_note}\n"
            f"消耗灵石：{cost_gold:,}\n"
            f"剩余灵石：{player.gold:,}\n"
            f"灵兽当前等级：Lv.{new_level} (经验：{new_exp}/{new_level * 500})\n"
            f"当前精力：{new_hp}/{stats['max_hp']} · 攻击：{stats['atk']} · 防御：{stats['defense']}"
        )

    def get_pet_combat_effects(self, pet: Optional[Dict]) -> dict:
        if not pet:
            return {}

        if int(pet.get("dead_until") or 0) > int(time.time()):
            return {}

        pet_info = self.PET_TYPES.get(pet["pet_type"])
        if not pet_info:
            return {}

        level = max(1, int(pet.get("level") or 1))
        base_val = pet_info["val"] * level * 0.1

        effects = {}
        if pet_info["attr"] == "crit_rate":
            effects["crit_rate"] = base_val
        elif pet_info["attr"] == "defense":
            effects["defense_bonus"] = base_val
            effects["counter_damage"] = min(30.0, level * 0.2)
        elif pet_info["attr"] == "all":
            effects["crit_rate"] = base_val
            effects["lifesteal"] = base_val / 2

        return effects

    def get_gold_bonus(self, pet: Optional[Dict]) -> float:
        if not pet:
            return 0.0
        if int(pet.get("dead_until") or 0) > int(time.time()):
            return 0.0
        pet_info = self.PET_TYPES.get(pet.get("pet_type"))
        if not pet_info or pet_info["attr"] != "gold_bonus":
            return 0.0
        level = max(1, int(pet.get("level") or 1))
        return pet_info["val"] + max(0, level - 1) * 0.005

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
