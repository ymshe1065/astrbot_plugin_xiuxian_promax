# managers/talisman_manager.py
"""符箓体系：符箓分类、效果推断、绘制、使用与跨系统加成。"""

import json
import random
import time
from typing import Dict, Iterator, List, Optional, Tuple, TYPE_CHECKING

from ..models import Player
from ..data.data_manager import DataBase
from ..core.talisman_rules import (
    TIER_POWER, TIER_LEVEL, TIER_GOLD, TIER_PRICE, CATEGORY_KEYWORDS,
    NON_TALISMAN_ITEMS, infer_category, build_profile,
    PROFICIENCY_TIERS, proficiency_level, proficiency_success, proficiency_gain,
)
from ..rank_rules import normalize_rank, rank_level

if TYPE_CHECKING:
    from ..config_manager import ConfigManager
    from ..core import StorageRingManager


# 允许送入战斗/跨系统的符箓效果键白名单。
# 旧实现是 7 键映射表，攻击/防御符写的是 *_multiplier，四个键一个都不在表里，
# 导致 42% 的符「能使用、不生效」。
TALISMAN_EFFECT_KEYS = {
    # 攻防倍率
    "physical_damage_multiplier", "magic_damage_multiplier",
    "physical_defense_multiplier", "magic_defense_multiplier",
    # 战斗基础
    "crit_rate_bonus", "crit_damage_bonus", "lifesteal_bonus",
    "counter_damage_bonus", "combo_rate_bonus", "dodge_bonus", "defense_bonus",
    # 破防 / 连击 / 追加攻击
    "sunder_chance", "sunder_pct", "sunder_turns",
    "extra_attack_chance", "extra_attack_ratio", "extra_attack_count",
    "crit_chain_chance", "crit_chain_ratio",
    # 控制
    "stun_chance", "stun_turns", "freeze_chance", "frost_chance", "frost_turns",
    "frost_atk_reduction", "curse_chance", "curse_defense_pct", "curse_turns",
    "burn_heal_reduction", "burn_chance", "burn_turns",
    # 跨系统（不进战斗面板，由各自系统读 get_active_bonus）
    "alchemy_success_bonus", "alchemy_quality_bonus", "tribulation_shield",
    "exploration_reward_multiplier", "rift_reward_multiplier",
    "drop_chance_bonus", "luck_bonus", "cultivation_multiplier",
    "lifespan_regen_per_minute", "heal_percent",
}

# 键名映射：符箓键 → 战斗 effects 键
TALISMAN_KEY_ALIAS = {
    "crit_rate_bonus": "crit_rate", "crit_damage_bonus": "crit_damage",
    "lifesteal_bonus": "lifesteal", "counter_damage_bonus": "counter_damage",
    "combo_rate_bonus": "combo_rate", "dodge_bonus": "dodge",
}

# 倍率语义：多个符取最高，且合并时是 1.0 + 增量（不是求和，否则会滑向乘算爆炸）
TALISMAN_MULTIPLIER_KEYS = {
    "physical_damage_multiplier", "magic_damage_multiplier",
    "physical_defense_multiplier", "magic_defense_multiplier",
}

# 控制类：多个控制符不该叠出 90% 定身，取最高概率
TALISMAN_MAX_KEYS = {
    "stun_chance", "stun_turns", "freeze_chance", "frost_chance", "curse_chance",
    "burn_chance", "curse_defense_pct", "frost_atk_reduction", "burn_heal_reduction",
}

# 不进战斗 effects 面板的键（各有专属读取方）
TALISMAN_NON_COMBAT_KEYS = {
    "alchemy_success_bonus", "alchemy_quality_bonus", "tribulation_shield",
    "exploration_reward_multiplier", "rift_reward_multiplier",
    "cultivation_multiplier", "lifespan_regen_per_minute", "heal_percent",
}

# 使用符箓时的品质档位（复用炼丹三档口径）
TALISMAN_QUALITY = (
    ("极品", 1.4, 10),
    ("上品", 1.2, 5),
    ("一般", 1.0, 0),
)


class TalismanManager:
    """符箓管理器（符箓师专属扩展）"""

    TIER_POWER = TIER_POWER
    TIER_LEVEL = TIER_LEVEL
    TIER_GOLD = TIER_GOLD
    TIER_PRICE = TIER_PRICE
    CATEGORY_KEYWORDS = CATEGORY_KEYWORDS

    def __init__(self, db: DataBase, config_manager: "ConfigManager", storage_ring_manager: "StorageRingManager" = None):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_manager = storage_ring_manager

    # ===== 配置与效果推断 =====
    def _item_data(self, name: str) -> Dict:
        if not self.config_manager:
            return {}
        item = (getattr(self.config_manager, "items_data", {}) or {}).get(name)
        return item if isinstance(item, dict) else {}

    def _recipe_config(self, name: str) -> Dict:
        if not self.config_manager:
            return {}
        return (getattr(self.config_manager, "talisman_recipes_data", {}) or {}).get(name, {})

    def _infer_category(self, name: str) -> str:
        return infer_category(name, self._recipe_config(name).get("category", ""))

    def get_talisman_profile(self, name: str) -> Dict:
        """根据符名、品级与配置推演效果；配方写了 effects 就以配方为准。"""
        item = self._item_data(name)
        recipe = self._recipe_config(name)
        rank = normalize_rank(item.get("rank") or recipe.get("rank"), "凡品")
        category = self._infer_category(name)
        raw_effects = recipe.get("effects")
        return build_profile(
            name, rank, category,
            effects=raw_effects if isinstance(raw_effects, dict) else None,
            duration=recipe.get("duration"),
            description=recipe.get("desc", ""),
        )

    # ===== 配方 =====
    # 材料目录只有四档，按境界把符箓品阶归入最近的材料档。
    MATERIAL_TIER_COUNTS = {"凡品": 2, "黄品": 2, "玄品": 3, "圣品": 4}

    @classmethod
    def _material_tier(cls, rank: str) -> str:
        level = rank_level(rank)
        if level >= 18:
            return "圣品"
        if level >= 9:
            return "玄品"
        if level >= 3:
            return "黄品"
        return "凡品"

    def _auto_materials(self, rank: str) -> List[Dict]:
        tier = self._material_tier(normalize_rank(rank, "凡品"))
        return [
            {"category": "灵植", "tier": tier, "count": self.MATERIAL_TIER_COUNTS[tier]},
            {"category": "矿材", "tier": tier, "count": max(1, self.MATERIAL_TIER_COUNTS[tier] - 1)},
        ]

    def get_recipe(self, name: str) -> Optional[Dict]:
        if name in NON_TALISMAN_ITEMS:
            return None
        recipe = self._recipe_config(name)
        if recipe:
            return dict(recipe)
        item = self._item_data(name)
        if not item or item.get("type") != "符箓":
            return None
        rank = normalize_rank(item.get("rank"), "凡品")
        return {
            "id": f"auto_{name}",
            "name": name,
            "category": self._infer_category(name),
            "rank": rank,
            "level_required": item.get("required_level_index", self.TIER_LEVEL.get(rank, 0)),
            "success_rate": 80 if self.TIER_LEVEL.get(rank, 0) < 9 else 68,
            "gold_cost": self.TIER_GOLD.get(rank, 900),
            "materials": self._auto_materials(rank),
            "price": self.TIER_PRICE.get(rank, 2000),
            "shop_weight": 30,
            "desc": "按符箓品级推演出的绘制配方。",
            "legacy": True,
        }

    # ===== 符图习得 =====
    def get_learned_recipes(self, player: Player) -> set:
        raw = getattr(player, "talisman_recipes", "")
        if isinstance(raw, (list, tuple, set)):
            data = list(raw)
        else:
            try:
                data = json.loads(raw) if raw else []
            except (TypeError, ValueError, json.JSONDecodeError):
                data = []
        if not isinstance(data, list):
            return set()
        return {str(item) for item in data}

    def is_recipe_learned(self, player: Player, name: str, recipe: Optional[Dict] = None) -> bool:
        """基础配方人人可画；legacy 自动配方继续放行，避免老存档能力缩水。"""
        recipe = recipe or self.get_recipe(name) or {}
        if recipe.get("basic") or recipe.get("legacy"):
            return True
        if str(recipe.get("id", "")).startswith("auto_"):
            return True
        return name in self.get_learned_recipes(player)

    async def learn_recipe(self, player: Player, name: str, commit: bool = True) -> Tuple[bool, str]:
        """习得符图（符阁购买后调用）。"""
        recipe = self._recipe_config(name)
        if not recipe:
            return False, f"❌ 没有【{name}】的符图。"
        if self.is_recipe_learned(player, name, recipe):
            return False, f"❌ 你已掌握【{name}】的绘制方法。"
        learned = sorted(self.get_learned_recipes(player) | {name})
        player.talisman_recipes = json.dumps(learned, ensure_ascii=False)
        if commit:
            await self.db.update_player(player)
        return True, f"📜 你已参悟【{name}】符图，可以 /制符 {name} 绘制了。"

    # ===== 熟练度 =====
    def get_proficiency_points(self, player: Player) -> int:
        try:
            return max(0, int(getattr(player, "talisman_exp", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def get_proficiency(self, player: Player) -> Dict:
        points = self.get_proficiency_points(player)
        level = proficiency_level(points)
        tier = dict(PROFICIENCY_TIERS[level])
        tier["level"] = level
        tier["points"] = points
        if level + 1 < len(PROFICIENCY_TIERS):
            tier["next_points"] = int(PROFICIENCY_TIERS[level + 1]["points"])
        else:
            tier["next_points"] = int(PROFICIENCY_TIERS[-1]["points"])
        return tier

    def _add_proficiency(self, player: Player, points: int):
        if points <= 0:
            return
        player.talisman_exp = self.get_proficiency_points(player) + int(points)

    def _roll_quality(self, extra_rate: float) -> Tuple[str, float, int]:
        """极品/上品率随熟练度额外产出加成提升，返回 (档位, 倍率, 加时分钟)。"""
        bonus = max(0.0, float(extra_rate or 0)) * 0.5
        roll = random.randint(1, 100)
        if roll <= 3 + bonus:
            return TALISMAN_QUALITY[0]
        if roll <= 12 + bonus:
            return TALISMAN_QUALITY[1]
        return TALISMAN_QUALITY[2]

    # ===== 列表 =====
    def list_talismans(self, player: Player) -> str:
        lines = ["🧿 符箓体系", "━━━━━━━━━━━━━━━"]
        recipes = getattr(self.config_manager, "talisman_recipes_data", {}) or {}
        by_category: Dict[str, List[Tuple[str, Dict]]] = {}
        for name, recipe in recipes.items():
            category = recipe.get("category") or self._infer_category(name)
            by_category.setdefault(category, []).append((name, recipe))

        owned = player.get_storage_ring_items()
        learned = self.get_learned_recipes(player)
        rank_order = {"凡品": 0, "黄品": 1, "玄品": 2, "圣品": 3}
        category_order = ["攻击", "防御", "控制", "回复", "炼丹", "探索", "增益", "遁术"]
        for category in category_order + [c for c in by_category if c not in category_order]:
            entries = by_category.get(category)
            if not entries:
                continue
            lines.append(f"\n【{category}】")
            entries.sort(key=lambda item: (rank_order.get(normalize_rank(item[1].get("rank"), "凡品"), 9), item[0]))
            for name, recipe in entries:
                profile = self.get_talisman_profile(name)
                count = owned.get(name, 0)
                can_craft = recipe.get("basic") or recipe.get("legacy") or name in learned
                tag = "" if can_craft else "（符图未习得）"
                lines.append(
                    f"  · {name}（{profile['rank']}，持有{count}）{tag}\n"
                    f"    {profile['description']}｜持续{profile['duration']}分钟"
                )
        prof = self.get_proficiency(player)
        lines.append(
            f"\n🖌️ 熟练度：{prof['name']}（{prof['points']}/{prof['next_points']}）"
            f"｜成功率+{prof['success']:.1f}%｜额外产出+{prof['extra']:.0f}%"
        )
        lines.append("\n💡 /制符 <符名> [数量] 绘制；/使用符箓 <符名> [数量] 使用")
        lines.append("💡 /符阁 符箓 买成品符，/符阁 符图 买玄品以上符图")
        lines.append("💡 /符箓配方 查看全部配方长图（材料、灵石、成功率）")
        return "\n".join(lines)

    # ===== 配方总览 =====
    RECIPE_CATEGORY_ORDER = ["攻击", "防御", "控制", "回复", "炼丹", "探索", "增益", "遁术"]

    @staticmethod
    def _material_label(spec: Dict) -> str:
        count = int(spec.get("count", 0) or 0)
        if spec.get("name"):
            return f"{spec['name']}×{count}"
        return f"{spec.get('tier', '')}{spec.get('category', '')}×{count}"

    @staticmethod
    def _recipe_rank(recipe: Dict) -> str:
        return normalize_rank(recipe.get("rank"), "凡品")

    def _recipe_summary(self, recipe: Dict) -> str:
        materials = "、".join(
            self._material_label(spec) for spec in (recipe.get("materials") or []) if isinstance(spec, dict)
        ) or "无需材料"
        level = int(recipe.get("level_required", 0) or 0)
        gold = int(recipe.get("gold_cost", 0) or 0)
        rate = float(recipe.get("success_rate", 75) or 75)
        return f"{self._recipe_rank(recipe)} Lv{level}｜{gold:,}灵石｜{materials}｜{rate:.0f}%"

    def iter_recipes(self) -> List[Tuple[str, Dict]]:
        """枚举正式符表（config 配方，34 张），不含 legacy 拼凑符。"""
        entries: List[Tuple[str, Dict]] = []
        for name, recipe in (getattr(self.config_manager, "talisman_recipes_data", {}) or {}).items():
            if name in NON_TALISMAN_ITEMS or recipe.get("legacy"):
                continue
            data = dict(recipe)
            data["category"] = data.get("category") or self._infer_category(name)
            entries.append((name, data))
        order = {category: index for index, category in enumerate(self.RECIPE_CATEGORY_ORDER)}
        entries.sort(key=lambda item: (
            order.get(item[1].get("category"), len(order)),
            rank_level(self._recipe_rank(item[1])),
            int(item[1].get("level_required", 0) or 0),
            item[0],
        ))
        return entries

    def build_recipe_page(self, width: int = 1280) -> Dict:
        """构建配方长图的图片页配置（供固定页生成器渲染与缓存）。"""
        entries = self.iter_recipes()
        grouped: Dict[str, List[Tuple[str, Dict]]] = {category: [] for category in self.RECIPE_CATEGORY_ORDER}
        for name, recipe in entries:
            grouped.setdefault(recipe.get("category", "增益"), []).append((name, recipe))

        sections: List[Dict] = [{
            "name": "制符说明",
            "lines": [
                "仅【符箓师】可绘制符箓；绘制消耗灵石与对应品阶材料。",
                "符箓师加成：成功率 +10%、灵石消耗 -20%、20% 概率额外产出 1 张。",
                "表中成功率为基础值，实际再叠加符箓师加成与熟练度加成。",
                "材料按「类别 + 品阶」匹配：灵植 / 矿材 / 妖兽 / 灵液，可从丹阁材料、灵田、历练与秘境获取。",
                "基础符免符图；标注「需符图」的需在 /符阁 符图 习得后方可绘制。",
                "格式：符名｜品阶 境界｜灵石｜材料｜成功率。",
                "/制符 <符名> [数量] 绘制；/使用符箓 <符名> [数量] 使用；/符箓 查看分类与持有数量。",
            ],
        }]
        for category in self.RECIPE_CATEGORY_ORDER:
            items = grouped.get(category) or []
            if not items:
                continue
            sections.append({
                "name": f"{category}符箓 · {len(items)}种",
                "lines": [
                    f"· {name}｜{self._recipe_summary(recipe)}"
                    + ("" if recipe.get("basic") else "｜需符图")
                    for name, recipe in items
                ],
            })
        return {
            "title": "符箓配方总览",
            "subtitle": f"共 {len(entries)} 种可绘制符箓 · 材料按类别与品阶匹配",
            "footer": "💡 /制符 <符名> [数量] 绘制 · /使用符箓 <符名> [数量] 使用",
            "background": "craft",
            "width": width,
            "sections": sections,
        }

    def format_recipe_text(self) -> str:
        """图片不可用时的配方文本降级。"""
        entries = self.iter_recipes()
        lines = [
            f"🧿 符箓配方总览（共 {len(entries)} 种）",
            "━━━━━━━━━━━━━━━",
            "仅【符箓师】可绘制；材料按「类别 + 品阶」匹配。",
            "格式：符名（品阶 境界｜灵石｜材料｜成功率）。",
        ]
        current_category = None
        for name, recipe in entries:
            category = recipe.get("category", "增益")
            if category != current_category:
                current_category = category
                lines.append(f"\n【{category}】")
            lines.append(f"  · {name}（{self._recipe_summary(recipe)}）")
        lines.append("\n💡 /制符 <符名> [数量] 绘制；/使用符箓 <符名> [数量] 使用")
        return "\n".join(lines)

    # ===== 材料 =====
    def _material_info(self, name: str) -> Dict:
        try:
            info = self.config_manager.get_material_info(name)
        except Exception:
            info = {}
        if info:
            return info
        return {}

    def _material_stacks(self, items: Dict, spec: Dict) -> List[Tuple[str, int]]:
        stacks = []
        for item_name, count in items.items():
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            if spec.get("name"):
                if item_name == spec["name"]:
                    stacks.append((item_name, count))
                continue
            info = self._material_info(item_name)
            if spec.get("category") and info.get("category") != spec["category"]:
                continue
            if spec.get("tier") and info.get("tier") != spec["tier"]:
                continue
            stacks.append((item_name, count))
        return stacks

    def _consume_materials(self, player: Player, recipe: Dict) -> Tuple[bool, str, List[str]]:
        items = player.get_storage_ring_items()
        specs = recipe.get("materials", []) or []
        missing = []
        for spec in specs:
            count = sum(value for _, value in self._material_stacks(items, spec))
            if count < int(spec.get("count", 0) or 0):
                label = spec.get("name") or f"{spec.get('tier', '')}{spec.get('category', '')}"
                missing.append(f"{label}（需要{spec.get('count')}，拥有{count}）")
        if missing:
            return False, "❌ 符材不足！\n" + "\n".join(f"  · {m}" for m in missing), []

        consumed = []
        for spec in specs:
            remaining = int(spec.get("count", 0) or 0)
            for item_name, have in self._material_stacks(items, spec):
                if remaining <= 0:
                    break
                take = min(remaining, have)
                left = int(items.get(item_name, 0)) - take
                if left <= 0:
                    del items[item_name]
                else:
                    items[item_name] = left
                remaining -= take
                consumed.append(f"{item_name}×{take}")
        player.set_storage_ring_items(items)
        return True, "", consumed

    # ===== 绘制 =====
    async def craft_talisman(self, player: Player, name: str, count: int = 1) -> Tuple[bool, str]:
        count = max(1, min(100, int(count or 1)))
        recipe = self.get_recipe(name)
        if not recipe:
            return False, f"❌ 没有找到符箓【{name}】的绘制方法。发送 /符箓 查看可绘制符箓。"
        if player.life_skill != "符箓师":
            return False, "❌ 只有【符箓师】才能绘制符箓。发送 /选择生活职业 符箓师 转职。"
        if not self.storage_ring_manager:
            return False, "❌ 系统异常：储物戒管理器未加载。"
        if not self.is_recipe_learned(player, name, recipe):
            return False, f"❌ 尚未学会【{name}】的绘制方法，可到 /符阁 符图 购买符图。"

        level_required = int(recipe.get("level_required", 0) or 0)
        if player.level_index < level_required:
            return False, f"❌ 绘制【{name}】需要达到境界等级 {level_required}。"

        rank = normalize_rank(recipe.get("rank"), "凡品")
        volume = int(self._item_data(name).get("volume", 1) or 1)
        if self.storage_ring_manager.get_available_slots(player) < volume:
            return False, "❌ 储物戒空间不足，绘制失败（符材未消耗）。"

        prof = self.get_proficiency(player)
        extra_rate = float(prof.get("extra", 0) or 0)
        discount = float(prof.get("cost_discount", 0) or 0)
        is_master = player.life_skill == "符箓师"
        base_gold = int(recipe.get("gold_cost", 0) or 0)
        if is_master:
            base_gold = int(base_gold * 0.8)
        unit_gold = int(round(base_gold * (1 - discount / 100.0)))
        success_rate = proficiency_success(recipe.get("success_rate", 75), rank, prof["points"])
        if is_master:
            success_rate = min(99.0, success_rate + 10)

        made = 0
        failed = 0
        attempted = 0
        gold_spent = 0
        consumed_all: List[str] = []

        # 逐张结算：扣料 → 扣灵石 → 判成功，材料耗尽即停并回报已完成张数
        for _ in range(count):
            if player.gold < unit_gold:
                break
            if self.storage_ring_manager.get_available_slots(player) < volume:
                break
            ok, msg, consumed = self._consume_materials(player, recipe)
            if not ok:
                if attempted == 0:
                    return False, msg
                break
            attempted += 1
            player.gold -= unit_gold
            gold_spent += unit_gold
            consumed_all.extend(consumed)
            if random.randint(1, 100) <= success_rate:
                made += 1
                if extra_rate > 0 and random.randint(1, 100) <= extra_rate:
                    made += 1
            else:
                failed += 1

        if attempted == 0:
            if player.gold < unit_gold:
                return False, f"❌ 灵石不足！绘制【{name}】需要 {unit_gold:,} 灵石（你有 {player.gold:,}）。"
            if self.storage_ring_manager.get_available_slots(player) < volume:
                return False, "❌ 储物戒空间不足，绘制失败（符材未消耗）。"
            return False, "❌ 符材不足，无法绘制。"

        # 熟练点：成功按张数×品阶系数，失败按 50% 给，否则新手永远练不起来
        gained = proficiency_gain(rank, made, True) + proficiency_gain(rank, failed, False)
        gate_before = proficiency_level(prof["points"])
        self._add_proficiency(player, gained)
        gate_after = proficiency_level(self.get_proficiency_points(player))
        await self.db.update_player(player)

        cost_text = "、".join(consumed_all) if consumed_all else "无"
        prof_after = self.get_proficiency(player)
        progress = (
            f"\n🖌️ 熟练度 {prof_after['name']}（{prof_after['points']}/{prof_after['next_points']}）"
            f"，本次 +{gained}"
        )
        if gate_after > gate_before:
            progress += f"\n🌟 熟练度提升至【{prof_after['name']}】！"

        if made <= 0:
            return True, (
                f"💥 绘制失败\n━━━━━━━━━━━━━━━\n"
                f"符纸承载不住灵力，化为灰烬。\n"
                f"消耗：{cost_text}、灵石 -{gold_spent:,}\n"
                f"成功率：{success_rate:.0f}%{progress}\n"
                f"━━━━━━━━━━━━━━━"
            )

        ok, msg = await self.storage_ring_manager.store_item(player, name, made, silent=True)
        if not ok:
            player.gold += gold_spent
            await self.db.update_player(player)
            return False, f"❌ 符箓存入储物戒失败：{msg}（灵石已退回，符材已消耗）"

        note = ""
        if attempted > made:
            note = f"\n（共尝试 {attempted} 次，成功 {made} 张，失败 {attempted - made} 次）"
        return True, (
            f"🖌️ 绘制成功！\n━━━━━━━━━━━━━━━\n"
            f"获得【{name}】x{made}{note}\n"
            f"消耗：{cost_text}、灵石 -{gold_spent:,}\n"
            f"成功率：{success_rate:.0f}%{progress}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 /使用符箓 {name} [数量] 使用"
        )

    # ===== 使用 =====
    async def use_talisman(self, player: Player, name: str, count: int = 1) -> Tuple[bool, str]:
        count = max(1, min(20, int(count or 1)))
        items = player.get_storage_ring_items()
        have = int(items.get(name, 0) or 0)
        if have < count:
            return False, f"❌ 你的储物戒中没有足够的【{name}】（拥有 {have}，需要 {count}）。"
        if name in NON_TALISMAN_ITEMS:
            return False, f"❌ 【{name}】需使用 /激活化身，不能作为符箓消耗。"
        if self._item_data(name).get("type") != "符箓":
            return False, f"❌ 【{name}】不是符箓。"

        recipe = self._recipe_config(name)
        rank = normalize_rank(self._item_data(name).get("rank") or recipe.get("rank"), "凡品")
        required = int(recipe.get("level_required", self.TIER_LEVEL.get(rank, 0)) or 0)
        if player.level_index < required:
            return False, f"❌ 使用【{name}】需要达到境界等级 {required}。"

        profile = self.get_talisman_profile(name)
        prof = self.get_proficiency(player)
        quality_name, quality_mult, quality_minutes = self._roll_quality(prof.get("extra", 0))
        scale = quality_mult * count
        effects = profile["effects"]

        if "heal_percent" in effects:
            percent = min(1.0, float(effects["heal_percent"]) * scale)
            if player.cultivation_type == "体修":
                old = player.blood_qi
                player.blood_qi = min(player.max_blood_qi, player.blood_qi + int(player.max_blood_qi * percent))
                healed = player.blood_qi - old
                result = f"恢复气血 {healed:,}"
            else:
                old = player.spiritual_qi
                player.spiritual_qi = min(player.max_spiritual_qi, player.spiritual_qi + int(player.max_spiritual_qi * percent))
                healed = player.spiritual_qi - old
                result = f"恢复灵气 {healed:,}"
            items[name] = have - count
            if items[name] <= 0:
                del items[name]
            player.set_storage_ring_items(items)
            await self.db.update_player(player)
            return True, (
                f"✨ 使用【{name}】x{count}（{quality_name}）\n"
                f"━━━━━━━━━━━━━━━\n{result}\n━━━━━━━━━━━━━━━"
            )

        now = int(time.time())
        duration_minutes = int(profile["duration"]) * count + quality_minutes
        duration = duration_minutes * 60
        active = [
            effect for effect in player.get_active_pill_effects()
            if effect.get("talisman_name") != name
        ]
        scaled = {}
        for key, value in effects.items():
            try:
                scaled[key] = round(float(value) * quality_mult, 4)
            except (TypeError, ValueError):
                continue
        active.append({
            "pill_name": name,
            "talisman_name": name,
            "source": "talisman",
            "subtype": "talisman",
            "category": profile["category"],
            "quality": quality_name,
            "start_time": now,
            "expiry_time": now + duration,
            "duration_minutes": duration_minutes,
            "last_tick_time": now,
            **scaled,
        })
        player.set_active_pill_effects(active)
        items[name] = have - count
        if items[name] <= 0:
            del items[name]
        player.set_storage_ring_items(items)
        await self.db.update_player(player)
        return True, (
            f"✨ 使用【{name}】x{count} 成功！（{quality_name}）\n"
            f"━━━━━━━━━━━━━━━\n"
            f"类别：{profile['category']}\n"
            f"效果：{profile['description']}\n"
            f"持续：{duration_minutes}分钟\n"
            f"━━━━━━━━━━━━━━━"
        )

    @staticmethod
    def consume_active_charge(player: Player, key: str) -> bool:
        """消耗一层指定符箓效果；用于避劫符等一次性护盾。"""
        effects = player.get_active_pill_effects()
        changed = False
        for effect in effects:
            if effect.get("source") != "talisman":
                continue
            try:
                value = int(effect.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            if value <= 1:
                effects = [e for e in effects if e is not effect]
            else:
                effect[key] = value - 1
            changed = True
            break
        if changed:
            player.set_active_pill_effects(effects)
        return changed

    @staticmethod
    def pick_shield_talisman(player: Player, demand: int, config_manager=None) -> Optional[Tuple[str, int]]:
        """从储物戒挑一张「够用且护盾最小」的护劫符；一张符只挡一次丹劫。"""
        if demand <= 0:
            return None
        manager = TalismanManager(None, config_manager, None)
        best: Optional[Tuple[str, int]] = None
        for name, raw_count in (player.get_storage_ring_items() or {}).items():
            try:
                if int(raw_count or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            profile = manager.get_talisman_profile(name)
            if profile.get("category") != "炼丹":
                continue
            shield = int(profile.get("effects", {}).get("tribulation_shield", 0) or 0)
            if shield >= demand and (best is None or shield < best[1]):
                best = (name, shield)
        return best

    @staticmethod
    def best_shield_available(player: Player, config_manager=None) -> int:
        """玩家手上（含已激活）能拿出的最高护盾，用于丹劫失败文案。"""
        manager = TalismanManager(None, config_manager, None)
        best = 0
        for name, raw_count in (player.get_storage_ring_items() or {}).items():
            try:
                if int(raw_count or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            profile = manager.get_talisman_profile(name)
            if profile.get("category") != "炼丹":
                continue
            best = max(best, int(profile.get("effects", {}).get("tribulation_shield", 0) or 0))
        try:
            best = max(best, int(TalismanManager.get_active_max_bonus(player, "tribulation_shield")))
        except Exception:
            pass
        return best

    @staticmethod
    def consume_shield_talisman(player: Player, config_manager=None, demand: int = 1) -> Optional[str]:
        """消耗储物戒中最省的护劫符，返回符名；库存不足返回 None。"""
        pick = TalismanManager.pick_shield_talisman(player, demand, config_manager)
        if not pick:
            return None
        name = pick[0]
        items = player.get_storage_ring_items()
        left = int(items.get(name, 0) or 0) - 1
        if left <= 0:
            items.pop(name, None)
        else:
            items[name] = left
        player.set_storage_ring_items(items)
        return name

    @staticmethod
    def consume_active_shield(player: Player, demand: int) -> Optional[int]:
        """消耗已激活符箓的护劫层：整张护盾一次用掉，不按层数复用。"""
        if demand <= 0:
            return None
        now = int(time.time())
        effects = player.get_active_pill_effects()
        best_index = None
        best_shield = None
        for index, effect in enumerate(effects):
            if effect.get("source") != "talisman":
                continue
            expiry = int(effect.get("expiry_time", 0) or 0)
            if expiry and now >= expiry:
                continue
            try:
                shield = int(effect.get("tribulation_shield", 0) or 0)
            except (TypeError, ValueError):
                continue
            if shield >= demand and (best_shield is None or shield < best_shield):
                best_index, best_shield = index, shield
        if best_index is None:
            return None
        effects.pop(best_index)
        player.set_active_pill_effects(effects)
        return best_shield

    # ===== 跨系统加成 =====
    @staticmethod
    def iter_active_effects(player: Player) -> Iterator[Dict]:
        now = int(time.time())
        for effect in player.get_active_pill_effects():
            if effect.get("source") != "talisman":
                continue
            expiry = effect.get("expiry_time", 0)
            try:
                expiry = int(expiry or 0)
            except (TypeError, ValueError):
                expiry = 0
            if expiry > 0 and now >= expiry:
                continue
            yield effect

    @staticmethod
    def get_active_bonus(player: Player, key: str) -> float:
        total = 0.0
        for effect in TalismanManager.iter_active_effects(player):
            try:
                total += float(effect.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
        return total

    @staticmethod
    def get_active_max_bonus(player: Player, key: str) -> float:
        best = 0.0
        for effect in TalismanManager.iter_active_effects(player):
            try:
                best = max(best, float(effect.get(key, 0) or 0))
            except (TypeError, ValueError):
                continue
        return best

    @staticmethod
    def collect_active_combat_effects(player: Player) -> Dict[str, float]:
        """收集生效中的符箓战斗效果。

        倍率类与控制类取最高，其余求和（交给 clamp_effects 封顶）。
        返回的是「增量」：倍率 0.33 表示 ×1.33，合并方负责加基准 1.0。
        """
        sums: Dict[str, float] = {}
        maxes: Dict[str, float] = {}
        for effect in TalismanManager.iter_active_effects(player):
            for key, raw in effect.items():
                if key not in TALISMAN_EFFECT_KEYS:
                    continue
                if key in TALISMAN_NON_COMBAT_KEYS:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                target = TALISMAN_KEY_ALIAS.get(key, key)
                if target in TALISMAN_MAX_KEYS or target in TALISMAN_MULTIPLIER_KEYS:
                    maxes[target] = max(maxes.get(target, 0.0), value)
                else:
                    sums[target] = sums.get(target, 0.0) + value
        result = dict(sums)
        result.update(maxes)
        return result

    @staticmethod
    def merge_into_combat_effects(effects: Dict, player: Player) -> Dict:
        """把符箓效果按语义合并进战斗 effects（倍率取 max(基准, 1+增量)）。"""
        for key, value in TalismanManager.collect_active_combat_effects(player).items():
            if key in TALISMAN_MULTIPLIER_KEYS:
                try:
                    base = float(effects.get(key, 1.0) or 1.0)
                except (TypeError, ValueError):
                    base = 1.0
                effects[key] = max(base, 1.0 + float(value))
            elif key in TALISMAN_MAX_KEYS:
                try:
                    old = float(effects.get(key, 0) or 0)
                except (TypeError, ValueError):
                    old = 0.0
                effects[key] = max(old, float(value))
            else:
                try:
                    old = float(effects.get(key, 0) or 0)
                except (TypeError, ValueError):
                    old = 0.0
                effects[key] = old + float(value)
        return effects
