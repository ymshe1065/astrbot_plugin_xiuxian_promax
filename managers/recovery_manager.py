# managers/recovery_manager.py
"""惰性气血/灵气恢复：不启动定时任务，在玩家下一条指令时结算。"""
import json
import time
from typing import Optional

from ..models import Player
from ..models_extended import UserStatus


class RecoveryManager:
    NATURAL_INTERVAL = 300
    NATURAL_RATE = 0.01
    REST_INTERVAL = 60
    REST_RATE = 0.05
    INJURED_HP_RATIO = 0.20

    def __init__(self, db, config_manager=None):
        self.db = db
        self.config_manager = config_manager

    async def _get_cd(self, user_id):
        cd = await self.db.ext.get_user_cd(user_id)
        if not cd:
            await self.db.ext.create_user_cd(user_id)
            cd = await self.db.ext.get_user_cd(user_id)
        return cd

    @staticmethod
    def _extra(cd) -> dict:
        try:
            data = json.loads(cd.extra_data or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def get_bonuses(self, player: Player) -> dict:
        result = {
            "natural_hp_regen_bonus": 0.0,
            "natural_qi_regen_bonus": 0.0,
            "rest_hp_regen_bonus": 0.0,
            "rest_qi_regen_bonus": 0.0,
            "natural_mp_regen_bonus": 0.0,
            "rest_mp_regen_bonus": 0.0,
        }
        # 宗门疗养殿
        try:
            if getattr(player, "sect_id", 0):
                sect = await self.db.ext.get_sect_by_id(player.sect_id)
                if sect:
                    buildings = json.loads(getattr(sect, "buildings", "{}") or "{}")
                    level = int((buildings or {}).get("疗养殿", 0) or 0)
                    if level:
                        result["natural_hp_regen_bonus"] += 0.10 * level
                        result["natural_qi_regen_bonus"] += 0.10 * level
                        result["rest_hp_regen_bonus"] += 0.05 * level
                        result["rest_qi_regen_bonus"] += 0.05 * level
        except Exception:
            pass

        # 洞府家具
        try:
            async with self.db.conn.execute(
                "SELECT furniture FROM blessed_lands WHERE user_id = ?", (player.user_id,)
            ) as cursor:
                row = await cursor.fetchone()
            furniture = json.loads(row[0]) if row and row[0] else []
            if isinstance(furniture, list):
                if "回春榻" in furniture:
                    result["natural_hp_regen_bonus"] += 0.15
                    result["rest_hp_regen_bonus"] += 0.10
                if "聚灵池" in furniture:
                    result["natural_qi_regen_bonus"] += 0.15
                    result["rest_qi_regen_bonus"] += 0.10
        except Exception:
            pass

        # 功法/装备 effects
        if self.config_manager is not None:
            try:
                items_data = getattr(self.config_manager, "items_data", {}) or {}
                techniques_data = getattr(self.config_manager, "techniques_data", {}) or {}
                weapons_data = getattr(self.config_manager, "weapons_data", {}) or {}
                names = list(player.get_techniques_list()) if hasattr(player, "get_techniques_list") else []
                if getattr(player, "main_technique", ""):
                    names.append(player.main_technique)
                try:
                    from ..core.equipment_manager import EquipmentManager
                    manager = EquipmentManager(None, self.config_manager)
                    for name, _inst in manager.get_gear_slots(player).values():
                        if name:
                            names.append(name)
                except Exception:
                    if getattr(player, "weapon", ""):
                        names.append(player.weapon)
                    if getattr(player, "armor", ""):
                        names.append(player.armor)
                aliases = {
                    "natural_mp_regen_bonus": "natural_qi_regen_bonus",
                    "rest_mp_regen_bonus": "rest_qi_regen_bonus",
                }
                for name in dict.fromkeys(names):
                    data = techniques_data.get(name) or items_data.get(name) or weapons_data.get(name) or {}
                    effects = data.get("effects") or {}
                    for key, value in effects.items():
                        target = aliases.get(key, key)
                        if target in result:
                            try:
                                result[target] += float(value or 0)
                            except (TypeError, ValueError):
                                pass
            except Exception:
                pass
        return result

    async def apply_lazy_recovery(self, player: Player, now: Optional[int] = None) -> Player:
        # 没有配置管理器时无法可靠计算境界气血上限；宁可不结算，
        # 也不能用回退模板把玩家当前气血压到错误上限。
        if self.config_manager is None:
            return player
        now = int(now or time.time())
        cd = await self._get_cd(player.user_id)
        extra = self._extra(cd)

        from .combat_manager import CombatManager
        stats = CombatManager.build_player_combat_stats(
            player, self.config_manager, db=self.db
        )
        max_hp = max(1, int(stats.max_hp))
        max_mp = max(0, int(stats.max_mp))
        bonuses = await self.get_bonuses(player)

        hp = int(player.hp or 0)
        mp = int(player.spiritual_qi or 0)
        if hp <= 0:
            hp = max_hp

        # 自然恢复
        last_natural = int(extra.get("last_natural_regen") or now)
        intervals = max(0, (now - last_natural) // self.NATURAL_INTERVAL)
        if intervals:
            hp = min(max_hp, hp + int(max_hp * self.NATURAL_RATE * intervals * (1 + bonuses["natural_hp_regen_bonus"])))
            mp = min(max_mp, mp + int(max_mp * self.NATURAL_RATE * intervals * (1 + bonuses["natural_qi_regen_bonus"])))
            extra["last_natural_regen"] = last_natural + intervals * self.NATURAL_INTERVAL
        elif not extra.get("last_natural_regen"):
            extra["last_natural_regen"] = now

        # 调息：只结算完整分钟
        if int(cd.type) == int(UserStatus.RESTING):
            last_rest = int(extra.get("rest_last_tick") or extra.get("resting_since") or now)
            minutes = max(0, (now - last_rest) // self.REST_INTERVAL)
            if minutes:
                hp = min(max_hp, hp + int(max_hp * self.REST_RATE * minutes * (1 + bonuses["rest_hp_regen_bonus"])))
                mp = min(max_mp, mp + int(max_mp * self.REST_RATE * minutes * (1 + bonuses["rest_qi_regen_bonus"])))
                extra["rest_last_tick"] = last_rest + minutes * self.REST_INTERVAL
            if hp >= max_hp and mp >= max_mp:
                extra.pop("rest_last_tick", None)
                extra.pop("resting_since", None)
                await self.db.ext.set_user_busy(player.user_id, UserStatus.IDLE, 0, extra_data=extra)
            else:
                await self.db.conn.execute(
                    "UPDATE user_cd SET extra_data = ? WHERE user_id = ?",
                    (json.dumps(extra, ensure_ascii=False), player.user_id),
                )
        else:
            await self.db.conn.execute(
                "UPDATE user_cd SET extra_data = ? WHERE user_id = ?",
                (json.dumps(extra, ensure_ascii=False), player.user_id),
            )

        player.hp = min(max_hp, max(1, hp))
        player.spiritual_qi = min(max_mp, max(0, mp))
        player.mp = player.spiritual_qi
        await self.db.update_player_fields(
            player.user_id, hp=player.hp, spiritual_qi=player.spiritual_qi, mp=player.mp
        )
        return player

    async def is_injured(self, player: Player) -> bool:
        from .combat_manager import CombatManager
        stats = CombatManager.build_player_combat_stats(
            player, self.config_manager, db=self.db
        )
        return stats.max_hp > 0 and (int(stats.hp) / int(stats.max_hp)) <= self.INJURED_HP_RATIO
