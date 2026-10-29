"""普通功法生活加成与功法掉落规则。"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Dict

from .item_rules import ItemRules

if TYPE_CHECKING:
    from ..models import Player


LIFE_EFFECT_KEYS = ("gold_bonus", "drop_bonus", "cooldown_reduction", "luck_bonus")
LIFE_EFFECT_CAPS = {
    "gold_bonus": 0.50,
    "drop_bonus": 0.50,
    "cooldown_reduction": 0.35,
    "luck_bonus": 50,
}
LIFE_EFFECT_LABELS = {
    "gold_bonus": "灵石收益",
    "drop_bonus": "掉落几率",
    "cooldown_reduction": "冷却缩减",
    "luck_bonus": "气运",
    "exp_multiplier": "修炼效率",
}

_RANK_DROP_WEIGHT = {"凡品": 100, "黄品": 35, "玄品": 10, "圣品": 2, "仙品": 1}


class TechniqueBonusManager:
    """汇总已装备普通功法的生活加成，并提供统一掉落入口。"""

    @staticmethod
    def _item_config(config_manager, name: str) -> Dict:
        if not config_manager or not name:
            return {}
        try:
            cfg = config_manager.get_item_config(name)
        except Exception:
            cfg = None
        return cfg if isinstance(cfg, dict) else {}

    @classmethod
    def collect_life_effects(cls, player, config_manager) -> Dict[str, float]:
        """仅普通功法提供生活加成；主修心法专注战斗。"""
        result = {key: 0.0 for key in LIFE_EFFECT_KEYS}
        if not player or not config_manager:
            return result

        seen = set()
        for name in player.get_techniques_list():
            if not name or name in seen:
                continue
            seen.add(name)
            cfg = cls._item_config(config_manager, name)
            if not cfg or ItemRules.normalize_equipment_type(cfg) != "technique":
                continue
            effects = cfg.get("life_effects")
            if isinstance(effects, dict):
                for key in LIFE_EFFECT_KEYS:
                    try:
                        result[key] += float(effects.get(key, 0) or 0)
                    except (TypeError, ValueError):
                        continue
            # 兼容旧配置：普通功法的 exp_multiplier 仍视为修炼加成
            try:
                result["exp_multiplier"] = result.get("exp_multiplier", 0.0) + float(
                    cfg.get("exp_multiplier", 0) or 0
                )
            except (TypeError, ValueError):
                pass

        for key, cap in LIFE_EFFECT_CAPS.items():
            result[key] = max(0.0, min(cap, result[key]))
        result["exp_multiplier"] = max(0.0, min(0.80, result.get("exp_multiplier", 0.0)))
        return result

    @classmethod
    def gold_multiplier(cls, player, config_manager) -> float:
        return 1.0 + cls.collect_life_effects(player, config_manager).get("gold_bonus", 0.0)

    @classmethod
    def apply_gold_bonus(cls, player, amount: int, config_manager) -> int:
        try:
            value = int(amount)
        except (TypeError, ValueError):
            return 0
        if value <= 0:
            return value
        return int(value * cls.gold_multiplier(player, config_manager))

    @classmethod
    def cooldown_reduction(cls, player, config_manager) -> float:
        return cls.collect_life_effects(player, config_manager).get("cooldown_reduction", 0.0)

    @classmethod
    def effective_cooldown(cls, base_seconds: int, player, config_manager) -> int:
        try:
            base = max(0, int(base_seconds))
        except (TypeError, ValueError):
            return 0
        return max(1, int(base * (1.0 - cls.cooldown_reduction(player, config_manager))))

    @classmethod
    def drop_chance_bonus(cls, player, config_manager) -> float:
        """返回百分点加成，例如 0.05 表示 +5 个百分点。"""
        return cls.collect_life_effects(player, config_manager).get("drop_bonus", 0.0) * 100.0

    @classmethod
    def luck_bonus(cls, player, config_manager) -> int:
        return int(round(cls.collect_life_effects(player, config_manager).get("luck_bonus", 0.0)))

    @classmethod
    def cultivation_bonus(cls, player, config_manager) -> float:
        return cls.collect_life_effects(player, config_manager).get("exp_multiplier", 0.0)

    @classmethod
    def _weighted_choice(cls, pool) -> str:
        if not pool:
            return ""
        total = sum(max(1, item[1]) for item in pool)
        roll = random.randint(1, total)
        upto = 0
        for name, weight in pool:
            upto += max(1, weight)
            if roll <= upto:
                return name
        return pool[-1][0]

    @classmethod
    def roll_technique_drop(
        cls,
        player,
        config_manager,
        base_chance: float = 5.0,
        allow_main: bool = False,
        main_ratio: float = 0.0,
    ) -> str:
        """按境界与权重随机产出一本功法；返回空串表示未掉落。"""
        if not player or not config_manager:
            return ""
        chance = max(0.0, min(95.0, float(base_chance) + cls.drop_chance_bonus(player, config_manager)))
        if random.random() * 100.0 >= chance:
            return ""

        max_level = max(0, int(getattr(player, "level_index", 0) or 0)) + 3
        ordinary, main_pool = [], []
        sources = []
        for attr in ("techniques_data", "items_data"):
            data = getattr(config_manager, attr, {}) or {}
            if isinstance(data, dict):
                sources.extend(data.values())
        for cfg in sources:
            if not isinstance(cfg, dict) or not cfg.get("name"):
                continue
            item_type = ItemRules.normalize_equipment_type(cfg)
            if item_type not in ("technique", "main_technique"):
                continue
            if ItemRules.get_required_level(cfg) > max_level:
                continue
            name = cfg["name"]
            weight = int(cfg.get("drop_weight") or _RANK_DROP_WEIGHT.get(cfg.get("rank", ""), 20))
            if item_type == "main_technique":
                main_pool.append((name, weight))
            else:
                ordinary.append((name, weight))

        use_main = bool(allow_main and main_pool and random.random() < max(0.0, min(1.0, main_ratio)))
        return cls._weighted_choice(main_pool if use_main else ordinary)


__all__ = [
    "TechniqueBonusManager",
    "LIFE_EFFECT_CAPS",
    "LIFE_EFFECT_KEYS",
    "LIFE_EFFECT_LABELS",
]
