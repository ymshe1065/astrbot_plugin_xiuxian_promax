# managers/luck_manager.py
"""气运系统：统一计算玩家气运，并提供各系统的气运加成换算。"""

import json
import time
from typing import Dict

from ..models import Player

__all__ = ["LuckManager"]


class LuckManager:
    """气运来源：本体 + 最高品阶称号 + 炼器/装备词条 + 临时符箓。"""

    TALISMAN_SOURCE = "talisman"

    @staticmethod
    def _bonus_data(player: Player) -> Dict:
        try:
            data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @classmethod
    def get_base_luck(cls, player: Player) -> int:
        try:
            return max(0, int(getattr(player, "luck", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def get_title_luck(cls, player: Player) -> int:
        """仅最高品阶称号的气运生效，与称号系统规则一致。"""
        from .title_data import TITLES

        owned = cls._bonus_data(player).get("titles", {})
        if not isinstance(owned, dict) or not owned:
            return 0
        title_map = {t["name"]: t for t in TITLES}
        best = None
        for name in owned:
            title = title_map.get(name)
            if title and (best is None or title["tier"] > best["tier"]):
                best = title
        if not best:
            return 0
        try:
            return max(0, int(best["stats"].get("luck", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def get_affix_luck(cls, player: Player) -> int:
        """炼器/洗练词条与已装备实例词条提供的气运。"""
        total = 0
        for affix in cls._bonus_data(player).get("affixes", []) or []:
            if not isinstance(affix, dict) or affix.get("key") != "luck":
                continue
            try:
                total += int(float(affix.get("value", 0) or 0))
            except (TypeError, ValueError):
                continue

        equipped_ids = set()
        for value in (player.get_equipped_instances() or {}).values():
            try:
                equipped_ids.add(int(value))
            except (TypeError, ValueError):
                continue
        if equipped_ids:
            for inst in player.get_equipment_instances():
                if not isinstance(inst, dict):
                    continue
                try:
                    inst_id = int(inst.get("id", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if inst_id not in equipped_ids:
                    continue
                for affix in inst.get("affixes", []) or []:
                    if not isinstance(affix, dict) or affix.get("key") != "luck":
                        continue
                    try:
                        total += int(float(affix.get("value", 0) or 0))
                    except (TypeError, ValueError):
                        continue
        return max(0, total)

    @classmethod
    def get_talisman_luck(cls, player: Player) -> int:
        """临时符箓提供的气运（过期自动失效）。"""
        now = int(time.time())
        total = 0.0
        for effect in player.get_active_pill_effects():
            if not isinstance(effect, dict) or effect.get("source") != cls.TALISMAN_SOURCE:
                continue
            expiry = int(effect.get("expiry_time", 0) or 0)
            if expiry > 0 and now >= expiry:
                continue
            try:
                total += float(effect.get("luck_bonus", effect.get("luck", 0)) or 0)
            except (TypeError, ValueError):
                continue
        return max(0, int(round(total)))

    @classmethod
    def get_total_luck(cls, player: Player) -> int:
        return (
            cls.get_base_luck(player)
            + cls.get_title_luck(player)
            + cls.get_affix_luck(player)
            + cls.get_talisman_luck(player)
        )

    @classmethod
    def get_breakdown(cls, player: Player) -> Dict[str, int]:
        base = cls.get_base_luck(player)
        title = cls.get_title_luck(player)
        affix = cls.get_affix_luck(player)
        talisman = cls.get_talisman_luck(player)
        return {
            "base": base,
            "title": title,
            "affix": affix,
            "talisman": talisman,
            "total": base + title + affix + talisman,
        }

    @classmethod
    def add_luck(cls, player: Player, amount: int) -> int:
        """增减本体气运，不低于 0；返回变化后的本体气运。"""
        try:
            delta = int(amount or 0)
        except (TypeError, ValueError):
            delta = 0
        player.luck = max(0, cls.get_base_luck(player) + delta)
        return player.luck

    @classmethod
    def percent_bonus(cls, player: Player, per_point: float = 0.5, cap: float = 20.0) -> float:
        """把气运换算为百分比加成，例如 0.5% / 点、上限 20%。"""
        return min(float(cap), cls.get_total_luck(player) * float(per_point))
