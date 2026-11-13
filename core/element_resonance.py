"""五行共鸣：以主修心法确定的五行属性为基准，结算同属性装备/功法与灵根加成。

设计口径：
- 主属性优先取主修心法的 element；未装备心法时兼容回退到当前武器。
- 已穿戴的 8 个装备部位与 3 本普通功法，凡五行与主属性一致即计入“同属性件数”。
- 同属性灵根额外提供一份灵根共鸣加成；多灵根按拥有属性数量分摊。
- 所有加成在统一战斗面板入口 `build_player_combat_stats` 中结算，
  因此 PvP、Boss、世界 Boss、团本、讨伐等入口共用同一结果。
"""

from collections import defaultdict
from typing import Dict, List, Optional, Set

from .item_rules import ItemRules

__all__ = ["ElementResonance"]

ELEMENTS = ("金", "木", "水", "火", "土")

# 变体灵根/属性统一归入五行：雷风归木、冰暗归水、光归火。
ELEMENT_ALIASES = {
    "雷": "木",
    "风": "木",
    "闪": "木",
    "冰": "水",
    "寒": "水",
    "暗": "水",
    "幽": "水",
    "光": "火",
    "阳": "火",
    "炎": "火",
    "焰": "火",
}

# 每件同属性装备/功法的固定加成。
PER_MATCH_BONUS = {
    "金": {"physical_atk_pct": 0.010, "crit_rate": 0.3},
    "木": {"hp_pct": 0.012, "lifesteal": 0.3},
    "水": {"magic_atk_pct": 0.010, "magic_def_pct": 0.008},
    "火": {"physical_atk_pct": 0.004, "magic_atk_pct": 0.004, "crit_damage": 0.010},
    "土": {"physical_def_pct": 0.010, "magic_def_pct": 0.010, "counter_damage": 0.3},
}

# 同属性灵根的额外加成（单灵根 100%，多灵根按属性数量分摊）。
ROOT_BONUS = {
    "金": {"physical_atk_pct": 0.030, "crit_rate": 1.0},
    "木": {"hp_pct": 0.040, "lifesteal": 1.0},
    "水": {"magic_atk_pct": 0.030, "magic_def_pct": 0.020},
    "火": {"physical_atk_pct": 0.0125, "magic_atk_pct": 0.0125, "crit_damage": 0.030},
    "土": {"physical_def_pct": 0.030, "magic_def_pct": 0.030, "counter_damage": 1.0},
}

# 防止多件同属性装备叠加过高。
CAPS = {
    "physical_atk_pct": 0.20,
    "magic_atk_pct": 0.20,
    "hp_pct": 0.20,
    "physical_def_pct": 0.20,
    "magic_def_pct": 0.20,
    "crit_rate": 10.0,
    "crit_damage": 0.30,
    "lifesteal": 10.0,
    "counter_damage": 10.0,
}

EFFECT_KEYS = ("crit_rate", "crit_damage", "lifesteal", "counter_damage")
PCT_KEYS = (
    "physical_atk_pct",
    "magic_atk_pct",
    "hp_pct",
    "physical_def_pct",
    "magic_def_pct",
)


class ElementResonance:
    """五行共鸣计算器。"""

    @classmethod
    def normalize(cls, element: Optional[str]) -> str:
        """把配置中的五行/变体属性统一为金木水火土。"""
        value = str(element or "").strip()
        if value in ELEMENTS:
            return value
        if value in ELEMENT_ALIASES:
            return ELEMENT_ALIASES[value]
        for alias, mapped in ELEMENT_ALIASES.items():
            if alias in value:
                return mapped
        return ""

    @classmethod
    def infer_from_name(cls, name: str) -> str:
        """名称兜底推断；配置显式 element 仍是第一优先级。"""
        text = str(name or "")
        if not text:
            return ""
        for element in ELEMENTS:
            if element in text:
                return element
        for alias, mapped in ELEMENT_ALIASES.items():
            if alias in text:
                return mapped
        return ""

    @classmethod
    def item_element(cls, name: str, config_manager) -> str:
        """读取物品/功法的五行，优先运行时标注，其次配置字段，最后名称推断。"""
        if not name:
            return ""
        if config_manager is not None:
            getter = getattr(config_manager, "get_item_element", None)
            if callable(getter):
                try:
                    explicit = cls.normalize(getter(name))
                    if explicit:
                        return explicit
                except Exception:
                    pass
            for attr in ("items_data", "weapons_data", "techniques_data"):
                data = getattr(config_manager, attr, None) or {}
                cfg = data.get(name) if isinstance(data, dict) else None
                if isinstance(cfg, dict):
                    explicit = cls.normalize(cfg.get("element"))
                    if explicit:
                        return explicit
        return cls.infer_from_name(name)

    @classmethod
    def _equipped_slot_name(cls, player, slot: str) -> str:
        """读取某个槽位当前实际穿戴的装备名，兼容旧 weapon/armor 字段。"""
        mapping = player.get_equipped_instances() if hasattr(player, "get_equipped_instances") else {}
        instances = player.get_equipment_instances() if hasattr(player, "get_equipment_instances") else []
        by_id = {}
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            try:
                by_id[int(inst.get("id", 0) or 0)] = inst
            except (TypeError, ValueError):
                continue
        try:
            inst = by_id.get(int(mapping.get(slot, 0) or 0))
        except (TypeError, ValueError):
            inst = None
        if inst and inst.get("name"):
            return str(inst["name"])
        if slot in ("weapon", "armor"):
            return str(getattr(player, slot, "") or "")
        return ""

    @classmethod
    def equipped_names(cls, player) -> List[str]:
        """返回参与共鸣的装备与普通功法名称。主修心法只定主属性，不重复计数。"""
        names: List[str] = []
        for slot in ItemRules.SLOT_LABELS:
            name = cls._equipped_slot_name(player, slot)
            if name:
                names.append(name)
        getter = getattr(player, "get_techniques_list", None)
        if callable(getter):
            try:
                names.extend(str(name) for name in getter() if name)
            except Exception:
                pass
        return names

    @classmethod
    def root_elements(cls, spiritual_root: str) -> Set[str]:
        """解析灵根包含的五行；雷/风→木、冰/暗→水、光→火。"""
        text = str(spiritual_root or "").replace("灵根", "")
        # “阴阳”是灵根类型，不应把其中的“阳”误判为火属性。
        text = text.replace("阴阳", "")
        # 融合/混沌灵根包罗五行，视为同时拥有五种属性（灵根加成按 1/5 分摊）。
        if "融合" in text or "混沌" in text:
            return set(ELEMENTS)
        found: Set[str] = set()
        for element in ELEMENTS:
            if element in text:
                found.add(element)
        for alias, mapped in ELEMENT_ALIASES.items():
            if alias in text:
                found.add(mapped)
        return found

    @classmethod
    def resolve_main_element(cls, player, config_manager=None) -> str:
        """主属性：主修心法优先，未装备心法时回退到当前武器。"""
        main = cls.item_element(getattr(player, "main_technique", ""), config_manager)
        if main:
            return main
        return cls.item_element(cls._equipped_slot_name(player, "weapon"), config_manager)

    @classmethod
    def compute(cls, player, config_manager=None, element: Optional[str] = None) -> Dict:
        """计算五行共鸣结果。

        `element` 显式传入时以其为准（空串表示禁用共鸣）；传 None 时按心法/武器解析。
        """
        main_element = cls.normalize(element) if element is not None else cls.resolve_main_element(
            player, config_manager
        )
        empty = {
            "main_element": "",
            "match_count": 0,
            "matched_items": [],
            "root_match": False,
            "root_share": 0.0,
        }
        empty.update({key: 0.0 for key in PCT_KEYS})
        empty.update({key: 0.0 for key in EFFECT_KEYS})
        empty["effects"] = {key: 0.0 for key in EFFECT_KEYS}
        if not main_element:
            return empty

        matched: List[str] = []
        for name in cls.equipped_names(player):
            if cls.item_element(name, config_manager) == main_element:
                matched.append(name)

        root_set = cls.root_elements(getattr(player, "spiritual_root", ""))
        root_match = main_element in root_set
        root_share = (1.0 / len(root_set)) if root_match and root_set else 0.0

        totals: Dict[str, float] = defaultdict(float)
        for _ in matched:
            for key, value in PER_MATCH_BONUS.get(main_element, {}).items():
                totals[key] += float(value)
        if root_match:
            for key, value in ROOT_BONUS.get(main_element, {}).items():
                totals[key] += float(value) * root_share

        for key in CAPS:
            totals[key] = min(float(totals.get(key, 0.0) or 0.0), float(CAPS[key]))

        # 仙缘【五行亲和】：共鸣的所有加成按账本整体放大（加算系数，无上限）
        try:
            from .reincarnation_rules import permanent_effect

            resonance_bonus = permanent_effect(player, "element_resonance_bonus")
            if resonance_bonus:
                for key in list(totals.keys()):
                    totals[key] = float(totals[key]) * (1.0 + resonance_bonus)
        except Exception:
            pass

        result = {
            "main_element": main_element,
            "match_count": len(matched),
            "matched_items": matched,
            "root_match": root_match,
            "root_share": root_share,
        }
        result.update({key: float(totals.get(key, 0.0) or 0.0) for key in PCT_KEYS})
        result.update({key: float(totals.get(key, 0.0) or 0.0) for key in EFFECT_KEYS})
        result["effects"] = {key: float(totals.get(key, 0.0) or 0.0) for key in EFFECT_KEYS}
        return result

    @classmethod
    def describe(cls, resonance: Dict) -> str:
        """给管理面板/卡片用的一行摘要。"""
        if not resonance or not resonance.get("main_element"):
            return "无"
        parts = [f"{resonance['main_element']}属性"]
        count = int(resonance.get("match_count", 0) or 0)
        parts.append(f"同属性{count}件" if count else "无同属性装备")
        if resonance.get("root_match"):
            parts.append(f"灵根共鸣{float(resonance.get('root_share', 0) or 0):.0%}")
        else:
            parts.append("灵根未共鸣")
        return " · ".join(parts)

    @classmethod
    def bonus_text(cls, resonance: Dict) -> str:
        """给卡片用的加成摘要。"""
        if not resonance or not resonance.get("main_element"):
            return ""
        parts = []
        if resonance.get("physical_atk_pct"):
            parts.append(f"物攻+{resonance['physical_atk_pct']:.1%}")
        if resonance.get("magic_atk_pct"):
            parts.append(f"法攻+{resonance['magic_atk_pct']:.1%}")
        if resonance.get("hp_pct"):
            parts.append(f"气血+{resonance['hp_pct']:.1%}")
        if resonance.get("physical_def_pct"):
            parts.append(f"物防+{resonance['physical_def_pct']:.1%}")
        if resonance.get("magic_def_pct"):
            parts.append(f"法防+{resonance['magic_def_pct']:.1%}")
        if resonance.get("crit_rate"):
            parts.append(f"暴击+{resonance['crit_rate']:.1f}%")
        if resonance.get("crit_damage"):
            parts.append(f"暴伤+{resonance['crit_damage']:.1%}")
        if resonance.get("lifesteal"):
            parts.append(f"吸血+{resonance['lifesteal']:.1f}%")
        if resonance.get("counter_damage"):
            parts.append(f"反伤+{resonance['counter_damage']:.1f}%")
        return " · ".join(parts)
