# core/jewel_manager.py
"""珠宝体系：品质、词条、镶嵌孔位、拆卸与战斗加成。

珠宝只存放在玩家身上的独立「珠囊」中（``player.jewel_inventory``），
不占储物戒格数；镶嵌后词条写入装备实例的 ``sockets`` 列表。
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional, Tuple

from ..models import Player
from .item_rules import ItemRules

__all__ = ["JewelManager", "JewelRules"]

ATTACK_EQUIP_TYPES = {"weapon", "gloves", "ring", "amulet", "accessory"}
DEFENSE_EQUIP_TYPES = {"armor", "body", "head", "boots"}

# 品质基础孔位：品质越高，出厂孔位越多
SOCKET_BASE_BY_QUALITY = {"一般": 1, "良好": 2, "极佳": 3, "大师": 4}
MAX_SOCKETS = 6
# 扩孔消耗强化石：2、4、6、8……线性增长
EXPAND_STONE_BASE = 2
EXPAND_STONE_STEP = 2
# 拆卸珠宝的灵石费用（按珠宝品质）
UNSOCKET_COST = {"一般": 2000, "良好": 6000, "极佳": 15000, "大师": 40000}

RATE_ATTRIBUTE_KEYS = {
    "physical_damage", "magic_damage", "physical_defense", "magic_defense",
    "mental_power",
}

PERCENT_POINT_KEYS = {
    "crit_rate", "lifesteal", "counter_damage", "combo_rate", "dodge",
    "status_resist", "extra_attack_chance", "extra_attack_ratio",
    "extra_attack_second_ratio", "armor_penetration", "sunder_chance",
    "sunder_pct", "crit_chain_chance", "crit_chain_ratio", "crit_defense_ignore",
    "combo_extra_hit_ratio", "combo_lifesteal_pct", "stun_chance",
    "stun_extend_chance", "stun_resist_pierce", "burn_chance", "burn_damage_pct",
    "burn_heal_reduction", "frost_chance", "frost_atk_reduction",
    "frost_dodge_reduction", "freeze_chance", "curse_chance", "curse_defense_pct",
    "curse_damage_reduction", "natural_hp_regen_bonus", "natural_qi_regen_bonus",
    "rest_hp_regen_bonus", "rest_qi_regen_bonus",
}

AFFIX_LABELS = {
    "crit_rate": "暴击率", "crit_damage": "暴击伤害", "lifesteal": "吸血",
    "counter_damage": "反伤", "combo_rate": "连击率", "dodge": "闪避",
    "status_resist": "状态抗性", "extra_attack_chance": "快击概率",
    "armor_penetration": "无视防御", "sunder_chance": "破防概率",
    "stun_chance": "麻痹概率", "burn_chance": "灼烧概率",
    "burn_damage_pct": "灼烧伤害", "frost_chance": "寒霜概率",
    "frost_atk_reduction": "寒霜减攻", "freeze_chance": "冻结概率",
    "curse_chance": "诅咒概率", "curse_defense_pct": "诅咒减防",
    "physical_damage": "物伤", "magic_damage": "法伤",
    "physical_defense": "物防", "magic_defense": "法防",
    "mental_power": "精神力",
}

DEFAULT_QUALITIES = {
    "一般": {"weight": 58, "multiplier": 1.0, "rank": "黄品"},
    "良好": {"weight": 27, "multiplier": 1.40, "rank": "玄品"},
    "极佳": {"weight": 12, "multiplier": 1.90, "rank": "圣品"},
    "大师": {"weight": 3, "multiplier": 2.60, "rank": "帝品"},
}

# 配置缺失时的最小可用模板（正常从 config/jewels.json 读取）
DEFAULT_TEMPLATES = [
    {
        "id": "war_crystal", "name": "破军晶", "category": "attack", "price": 9000,
        "description": "晶棱如兵锋，直接增幅双攻。",
        "affixes": [
            {"key": "physical_damage", "value": 0.018, "scale": "rate"},
            {"key": "magic_damage", "value": 0.018, "scale": "rate"},
        ],
    },
    {
        "id": "turtle_jade", "name": "玄龟玉", "category": "defense", "price": 6500,
        "description": "玄龟背纹盘踞，双防俱增。",
        "affixes": [
            {"key": "physical_defense", "value": 0.022, "scale": "rate"},
            {"key": "magic_defense", "value": 0.022, "scale": "rate"},
        ],
    },
]


class JewelRules:
    """纯规则函数，供展示层与测试直接调用。"""

    @staticmethod
    def equipment_category(equipment_type: str) -> str:
        equip_type = ItemRules.normalize_equipment_type({"type": equipment_type})
        if equip_type in ATTACK_EQUIP_TYPES:
            return "attack"
        if equip_type in DEFENSE_EQUIP_TYPES:
            return "defense"
        return ""

    @staticmethod
    def category_label(category: str) -> str:
        return {"attack": "攻击", "defense": "防御"}.get(category, "通用")

    @staticmethod
    def socket_base(quality: str) -> int:
        return int(SOCKET_BASE_BY_QUALITY.get(quality or "一般", 1))

    @staticmethod
    def socket_capacity(instance: dict) -> int:
        base = JewelRules.socket_base((instance or {}).get("quality", "一般"))
        try:
            extra = int((instance or {}).get("socket_extra", 0) or 0)
        except (TypeError, ValueError):
            extra = 0
        return max(0, min(MAX_SOCKETS, base + max(0, extra)))

    @staticmethod
    def next_expand_cost(instance: dict) -> Optional[int]:
        """返回下一次扩孔所需强化石；已到统一上限时返回 None。"""
        capacity = JewelRules.socket_capacity(instance)
        if capacity >= MAX_SOCKETS:
            return None
        try:
            extra = int((instance or {}).get("socket_extra", 0) or 0)
        except (TypeError, ValueError):
            extra = 0
        return EXPAND_STONE_BASE + EXPAND_STONE_STEP * max(0, extra)

    @staticmethod
    def format_affix(key: str, value, scale: str = "") -> str:
        label = AFFIX_LABELS.get(key, key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return f"{label}+{value}"
        if scale == "rate" or key in RATE_ATTRIBUTE_KEYS:
            return f"{label}{number:+.1%}"
        if scale == "flat":
            return f"{label}{number:+.0f}"
        if key == "crit_damage":
            return f"{label}{number:+.1%}"
        if key in PERCENT_POINT_KEYS:
            return f"{label}{number:+g}%"
        return f"{label}{number:+g}"


class JewelManager:
    def __init__(self, db=None, config_manager=None, storage_ring_manager=None):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_manager = storage_ring_manager

    # ===== 配置 =====
    def _config(self) -> dict:
        data = getattr(self.config_manager, "jewels_data", {}) or {}
        return data if isinstance(data, dict) else {}

    def qualities(self) -> Dict[str, dict]:
        raw = self._config().get("qualities")
        return raw if isinstance(raw, dict) and raw else dict(DEFAULT_QUALITIES)

    def templates(self) -> List[dict]:
        raw = self._config().get("templates")
        if isinstance(raw, list) and raw:
            return [x for x in raw if isinstance(x, dict) and x.get("name")]
        return list(DEFAULT_TEMPLATES)

    def template_by_id(self, template_id: str) -> Optional[dict]:
        for template in self.templates():
            if str(template.get("id") or "") == str(template_id):
                return template
        return None

    def template_by_name(self, name: str) -> Optional[dict]:
        for template in self.templates():
            if template.get("name") == name:
                return template
        return None

    def _quality_weight_table(self) -> Tuple[List[str], List[int]]:
        qualities = self.qualities()
        names = list(qualities)
        weights = [max(0, int((qualities[q] or {}).get("weight", 0) or 0)) for q in names]
        if not any(weights):
            weights = [1] * len(names)
        return names, weights

    def quality_multiplier(self, quality: str) -> float:
        data = self.qualities().get(quality or "一般") or {}
        try:
            return float(data.get("multiplier", 1.0) or 1.0)
        except (TypeError, ValueError):
            return 1.0

    def quality_rank(self, quality: str) -> str:
        data = self.qualities().get(quality or "一般") or {}
        return str(data.get("rank") or "黄品")

    # ===== 生成 =====
    def roll_jewel(
        self,
        quality: Optional[str] = None,
        category: Optional[str] = None,
        template: Optional[dict] = None,
        quality_weights: Optional[dict] = None,
        rng=None,
    ) -> dict:
        rng = rng or random
        qualities = self.qualities()
        if quality not in qualities:
            if isinstance(quality_weights, dict) and quality_weights:
                names = [q for q in qualities if quality_weights.get(q, 0) > 0]
                weights = [quality_weights.get(q, 0) for q in names]
                quality = rng.choices(names, weights=weights, k=1)[0] if names else None
            if quality not in qualities:
                names, weights = self._quality_weight_table()
                quality = rng.choices(names, weights=weights, k=1)[0]
        if template is None:
            pool = [
                x for x in self.templates()
                if not category or str(x.get("category") or "attack") == category
            ] or self.templates()
            template = rng.choice(pool)
        template = dict(template)
        mult = self.quality_multiplier(quality)
        affixes = []
        for affix in template.get("affixes") or []:
            if not isinstance(affix, dict) or not affix.get("key"):
                continue
            scale = str(affix.get("scale") or "percent")
            try:
                base = float(affix.get("value", 0) or 0)
            except (TypeError, ValueError):
                continue
            if scale == "rate":
                value = round(base * mult, 4)
            elif scale == "flat":
                value = int(round(base * mult))
            else:
                value = round(base * mult, 1)
            if value:
                affixes.append({"key": affix["key"], "value": value, "scale": scale})
        try:
            price = int(round(float(template.get("price", 0) or 0) * mult))
        except (TypeError, ValueError):
            price = 0
        return {
            "id": 0,
            "name": template.get("name") or "无名珠宝",
            "template_id": template.get("id") or template.get("name") or "",
            "category": str(template.get("category") or "attack"),
            "quality": quality,
            "rank": self.quality_rank(quality),
            "price": max(1, price),
            "description": template.get("description", ""),
            "affixes": affixes,
        }

    # ===== 珠囊 =====
    @staticmethod
    def get_inventory(player: Player) -> List[dict]:
        return [x for x in player.get_jewel_inventory() if isinstance(x, dict)]

    @staticmethod
    def save_inventory(player: Player, jewels: List[dict]) -> None:
        player.set_jewel_inventory(jewels)

    def add_jewel(self, player: Player, jewel: dict) -> dict:
        inventory = self.get_inventory(player)
        next_id = 1
        for item in inventory:
            try:
                next_id = max(next_id, int(item.get("id", 0) or 0) + 1)
            except (TypeError, ValueError):
                continue
        gem = dict(jewel)
        gem["id"] = next_id
        gem.setdefault("affixes", [])
        inventory.append(gem)
        self.save_inventory(player, inventory)
        return gem

    def get_jewel(self, player: Player, jewel_id) -> Optional[dict]:
        try:
            jewel_id = int(jewel_id)
        except (TypeError, ValueError):
            return None
        for gem in self.get_inventory(player):
            if int(gem.get("id", 0) or 0) == jewel_id:
                return gem
        return None

    def remove_jewel(self, player: Player, jewel_id) -> Optional[dict]:
        try:
            jewel_id = int(jewel_id)
        except (TypeError, ValueError):
            return None
        inventory = self.get_inventory(player)
        for index, gem in enumerate(inventory):
            if int(gem.get("id", 0) or 0) == jewel_id:
                inventory.pop(index)
                self.save_inventory(player, inventory)
                return gem
        return None

    # ===== 镶嵌 / 拆卸 / 扩孔 =====
    def instance_category(self, instance: dict) -> str:
        inst = instance or {}
        equip_type = str(inst.get("type") or "")
        if not equip_type and self.config_manager:
            cfg = (getattr(self.config_manager, "items_data", {}) or {}).get(inst.get("name", ""))
            if not cfg:
                cfg = (getattr(self.config_manager, "weapons_data", {}) or {}).get(inst.get("name", ""))
            equip_type = ItemRules.normalize_equipment_type(cfg)
        return JewelRules.equipment_category(equip_type)

    def sockets_of(self, instance: dict) -> List[dict]:
        raw = (instance or {}).get("sockets")
        return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []

    def can_socket(self, player: Player, instance: dict, jewel: dict) -> Tuple[bool, str]:
        if not instance:
            return False, "❌ 未找到该装备实例。"
        equip_category = self.instance_category(instance)
        if not equip_category:
            return False, "❌ 该物品不是可镶嵌的装备。"
        if str(jewel.get("category") or "") != equip_category:
            need = "攻击类装备" if jewel.get("category") == "attack" else "防御类装备"
            return False, f"❌ 【{jewel.get('name')}】只能镶嵌在{need}上。"
        sockets = self.sockets_of(instance)
        if len(sockets) >= JewelRules.socket_capacity(instance):
            return False, "❌ 该装备孔位已满，可使用 /扩孔 消耗强化石扩展。"
        if any(int(x.get("id", 0) or 0) == int(jewel.get("id", 0) or 0) for x in sockets):
            return False, "❌ 这枚珠宝已经镶嵌在该装备上了。"
        return True, ""

    def socket_gem_into_instance(self, player: Player, instance: dict, jewel: dict) -> Tuple[bool, str]:
        ok, msg = self.can_socket(player, instance, jewel)
        if not ok:
            return False, msg
        sockets = self.sockets_of(instance)
        sockets.append(dict(jewel))
        instance["sockets"] = sockets
        self.remove_jewel(player, jewel.get("id"))
        return True, (
            f"✅ 已将【{jewel.get('name')}·{jewel.get('quality')}】镶嵌到"
            f"【{instance.get('name')}】第 {len(sockets)} 孔。"
        )

    def unsocket_gem(self, player: Player, instance: dict, socket_index: int) -> Tuple[bool, str, int]:
        sockets = self.sockets_of(instance)
        if socket_index < 1 or socket_index > len(sockets):
            return False, f"❌ 该装备只有 {len(sockets)} 个已镶嵌孔位。", 0
        gem = sockets.pop(socket_index - 1)
        instance["sockets"] = sockets
        cost = int(UNSOCKET_COST.get(str(gem.get("quality") or "一般"), 2000))
        gem = dict(gem)
        gem.pop("id", None)
        new_gem = self.add_jewel(player, gem)
        return True, (
            f"✅ 已从【{instance.get('name')}】拆下【{gem.get('name')}·{gem.get('quality')}】"
            f"（珠囊编号 #{new_gem['id']}）。"
        ), cost

    def expand_instance_socket(self, instance: dict) -> Tuple[bool, str, Optional[int]]:
        cost = JewelRules.next_expand_cost(instance)
        if cost is None:
            return False, f"❌ 该装备孔位已达统一上限 {MAX_SOCKETS} 孔。", None
        try:
            extra = int((instance or {}).get("socket_extra", 0) or 0)
        except (TypeError, ValueError):
            extra = 0
        instance["socket_extra"] = extra + 1
        return True, (
            f"✅ 扩孔成功！【{instance.get('name')}】当前孔位 "
            f"{JewelRules.socket_capacity(instance)}/{MAX_SOCKETS}。"
        ), cost

    # ===== 战斗加成 =====
    def instance_bonuses(self, instance: dict) -> Tuple[Dict[str, float], Dict[str, float]]:
        rates: Dict[str, float] = {}
        effects: Dict[str, float] = {}
        for gem in self.sockets_of(instance):
            for affix in gem.get("affixes") or []:
                if not isinstance(affix, dict):
                    continue
                key = str(affix.get("key") or "")
                if not key:
                    continue
                try:
                    value = float(affix.get("value", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if not value:
                    continue
                if key in RATE_ATTRIBUTE_KEYS:
                    rates[key] = rates.get(key, 0.0) + value
                else:
                    effects[key] = effects.get(key, 0.0) + value
        return rates, effects

    def equipped_bonuses(self, player: Player) -> Tuple[Dict[str, float], Dict[str, float]]:
        """汇总当前穿戴装备上的全部珠宝词条。"""
        rates: Dict[str, float] = {}
        effects: Dict[str, float] = {}
        mapping = player.get_equipped_instances() if player else {}
        if not mapping:
            return rates, effects
        from .item_instance_manager import ItemInstanceManager

        manager = ItemInstanceManager(self.db, self.config_manager)
        for instance_id in mapping.values():
            try:
                instance = manager.get_instance(player, instance_id)
            except Exception:
                instance = None
            if not instance:
                continue
            gem_rates, gem_effects = self.instance_bonuses(instance)
            for key, value in gem_rates.items():
                rates[key] = rates.get(key, 0.0) + value
            for key, value in gem_effects.items():
                effects[key] = effects.get(key, 0.0) + value
        return rates, effects

    def describe_jewel(self, jewel: dict) -> str:
        parts = [
            JewelRules.format_affix(a.get("key", ""), a.get("value", 0), a.get("scale", ""))
            for a in (jewel or {}).get("affixes") or []
            if isinstance(a, dict)
        ]
        return "、".join(parts) or "无词条"

    def describe_sockets(self, instance: dict) -> str:
        capacity = JewelRules.socket_capacity(instance)
        sockets = self.sockets_of(instance)
        if not sockets:
            return f"孔位 0/{capacity}（空）"
        gems = "、".join(
            f"{gem.get('name')}·{gem.get('quality')}" for gem in sockets
        )
        return f"孔位 {len(sockets)}/{capacity}：{gems}"
