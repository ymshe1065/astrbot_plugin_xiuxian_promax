# managers/talisman_manager.py
"""符箓体系：符箓分类、效果推断、绘制、使用与跨系统加成。"""

import json
import random
import time
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..models import Player
from ..data.data_manager import DataBase

if TYPE_CHECKING:
    from ..config_manager import ConfigManager
    from ..core import StorageRingManager


class TalismanManager:
    """符箓管理器（符箓师专属扩展）"""

    TIER_POWER = {
        "凡品": 0.15, "黄阶": 0.25, "灵品": 0.3, "玄阶": 0.4,
        "珍品": 0.45, "地阶": 0.55, "圣品": 0.6, "天阶": 0.8,
        "帝品": 0.85, "道品": 0.9, "仙品": 1.0, "神品": 1.1, "混元先天": 1.2,
    }
    TIER_LEVEL = {
        "凡品": 0, "黄阶": 0, "灵品": 0, "玄阶": 3, "珍品": 3,
        "地阶": 9, "圣品": 9, "天阶": 18, "帝品": 18, "道品": 24,
        "仙品": 30, "神品": 36, "混元先天": 42,
    }
    TIER_GOLD = {
        "凡品": 200, "黄阶": 300, "灵品": 500, "玄阶": 900,
        "珍品": 1500, "地阶": 3000, "圣品": 5000, "天阶": 12000,
        "帝品": 16000, "道品": 22000, "仙品": 50000, "神品": 80000, "混元先天": 120000,
    }
    TIER_PRICE = {
        "凡品": 500, "黄阶": 800, "灵品": 1500, "玄阶": 2600,
        "珍品": 4000, "地阶": 9000, "圣品": 14000, "天阶": 35000,
        "帝品": 45000, "道品": 60000, "仙品": 120000, "神品": 200000, "混元先天": 300000,
    }

    CATEGORY_KEYWORDS = [
        ("炼丹", ("引雷", "避劫", "稳魂", "聚灵")),
        ("探索", ("寻宝", "遁地", "星辰")),
        ("回复", ("回春",)),
        ("攻击", ("焚天", "紫电", "诛仙", "破军", "噬魂", "青锋", "九天", "烈焰", "雷霆", "雷", "火")),
        ("防御", ("山河", "长生", "太极", "八卦", "两仪", "四象", "乾坤", "混元", "无极", "太虚", "金刚")),
        ("控制", ("寒霜", "幽冥", "九幽", "寂灭", "混沌", "冰", "霜")),
        ("增益", ("鸿蒙", "造化", "万古")),
    ]

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
        recipe = self._recipe_config(name)
        if recipe.get("category"):
            return recipe["category"]
        for category, keywords in self.CATEGORY_KEYWORDS:
            if any(keyword in name for keyword in keywords):
                return category
        return "增益"

    def get_talisman_profile(self, name: str) -> Dict:
        """根据符名、品级与配置推断效果。"""
        item = self._item_data(name)
        rank = str(item.get("rank") or self._recipe_config(name).get("rank") or "黄阶")
        power = float(self.TIER_POWER.get(rank, 0.3))
        category = self._infer_category(name)
        is_array = "阵" in str(name)
        if is_array:
            power *= 1.2
        duration = 30 if is_array else 15
        effects: Dict[str, float] = {}
        desc_parts: List[str] = []

        if category == "攻击":
            mult = round(0.15 + power * 0.5, 2)
            effects["physical_damage_multiplier"] = mult
            effects["magic_damage_multiplier"] = mult
            effects["crit_rate_bonus"] = int(8 + power * 15)
            desc_parts.append(f"物伤/法伤+{mult:.0%}，暴击+{effects['crit_rate_bonus']}%")
        elif category == "防御":
            mult = round(0.2 + power * 0.6, 2)
            effects["physical_defense_multiplier"] = mult
            effects["magic_defense_multiplier"] = mult
            desc_parts.append(f"双防+{mult:.0%}")
        elif category == "控制":
            effects["dodge_bonus"] = int(8 + power * 20)
            effects["counter_damage_bonus"] = int(5 + power * 15)
            effects["lifesteal_bonus"] = int(3 + power * 10)
            desc_parts.append(
                f"闪避+{effects['dodge_bonus']}%，反击+{effects['counter_damage_bonus']}%，吸血+{effects['lifesteal_bonus']}%"
            )
        elif category == "探索":
            mult = round(0.15 + power * 0.4, 2)
            effects["exploration_reward_multiplier"] = mult
            effects["rift_reward_multiplier"] = mult
            effects["drop_chance_bonus"] = int(5 + power * 20)
            effects["dodge_bonus"] = int(3 + power * 8)
            effects["luck_bonus"] = int(2 + power * 8)
            desc_parts.append(
                f"历练/秘境收益+{mult:.0%}，掉落概率+{effects['drop_chance_bonus']}%，"
                f"闪避+{effects['dodge_bonus']}%，气运+{effects['luck_bonus']}"
            )
        elif category == "炼丹":
            effects["alchemy_success_bonus"] = int(6 + power * 15)
            effects["alchemy_quality_bonus"] = int(3 + power * 12)
            if any(keyword in name for keyword in ("引雷", "避劫")):
                effects["tribulation_shield"] = 1
                desc_parts.append("免疫一次丹劫毁丹")
            desc_parts.append(
                f"炼丹成功率+{effects['alchemy_success_bonus']}%，高品质率+{effects['alchemy_quality_bonus']}%"
            )
        elif category == "回复":
            effects["heal_percent"] = round(0.25 + power * 0.2, 2)
            desc_parts.append(f"立即恢复 {effects['heal_percent']:.0%} 气血/灵气")
        else:
            mult = round(0.1 + power * 0.3, 2)
            effects["cultivation_multiplier"] = mult
            effects["lifespan_regen_per_minute"] = int(1 + power * 3)
            effects["crit_rate_bonus"] = int(2 + power * 5)
            effects["luck_bonus"] = int(1 + power * 6)
            desc_parts.append(
                f"修炼速度+{mult:.0%}，寿命回复+{effects['lifespan_regen_per_minute']}/分钟，"
                f"气运+{effects['luck_bonus']}"
            )

        return {
            "name": name,
            "rank": rank,
            "category": category,
            "duration": duration,
            "effects": effects,
            "description": "；".join(desc_parts) if desc_parts else "特殊效果",
        }

    # ===== 配方 =====
    def _auto_materials(self, rank: str) -> List[Dict]:
        if rank in ("凡品", "黄阶", "灵品"):
            tier = "黄阶"
            return [
                {"category": "灵植", "tier": tier, "count": 2},
                {"category": "矿材", "tier": tier, "count": 1},
            ]
        if rank in ("玄阶", "珍品"):
            return [
                {"category": "灵植", "tier": "玄阶", "count": 2},
                {"category": "矿材", "tier": "玄阶", "count": 1},
            ]
        if rank in ("地阶", "圣品"):
            return [
                {"category": "灵植", "tier": "地阶", "count": 3},
                {"category": "矿材", "tier": "地阶", "count": 2},
            ]
        return [
            {"category": "灵植", "tier": "天阶", "count": 4},
            {"category": "矿材", "tier": "天阶", "count": 3},
        ]

    def get_recipe(self, name: str) -> Optional[Dict]:
        recipe = self._recipe_config(name)
        if recipe:
            return dict(recipe)
        item = self._item_data(name)
        if not item or item.get("type") != "符箓":
            return None
        rank = item.get("rank", "黄阶")
        return {
            "id": f"auto_{name}",
            "name": name,
            "category": self._infer_category(name),
            "rank": rank,
            "level_required": self.TIER_LEVEL.get(rank, 0),
            "success_rate": 80 if self.TIER_LEVEL.get(rank, 0) < 9 else 68,
            "gold_cost": self.TIER_GOLD.get(rank, 900),
            "materials": self._auto_materials(rank),
            "price": self.TIER_PRICE.get(rank, 2000),
            "shop_weight": 30,
            "desc": "按符箓品级推演出的绘制配方。",
        }

    def list_talismans(self, player: Player) -> str:
        lines = ["🧿 符箓体系", "━━━━━━━━━━━━━━━"]
        recipes = getattr(self.config_manager, "talisman_recipes_data", {}) or {}
        by_category: Dict[str, List[Tuple[str, Dict]]] = {}
        for name, recipe in recipes.items():
            category = recipe.get("category") or self._infer_category(name)
            by_category.setdefault(category, []).append((name, recipe))

        owned = player.get_storage_ring_items()
        for category in ["攻击", "防御", "控制", "回复", "炼丹", "探索", "增益"]:
            entries = by_category.get(category)
            if not entries:
                continue
            lines.append(f"\n【{category}】")
            for name, recipe in sorted(entries, key=lambda item: item[0]):
                profile = self.get_talisman_profile(name)
                count = owned.get(name, 0)
                lines.append(
                    f"  · {name}（{profile['rank']}，持有{count}）\n"
                    f"    {profile['description']}｜持续{profile['duration']}分钟"
                )
        all_talismans = [
            name for name, item in (getattr(self.config_manager, "items_data", {}) or {}).items()
            if isinstance(item, dict) and item.get("type") == "符箓"
        ]
        others = max(0, len(all_talismans) - len(recipes))
        if others:
            lines.append(f"\n另有 {others} 种已有符箓，可直接 /制符 <符名> 按品级自动配方绘制。")
        lines.append("\n💡 /制符 <符名> [数量] 绘制；/使用符箓 <符名> [数量] 使用")
        lines.append("💡 任意符箓均可 /使用符箓，效果按符名与品级自动推演")
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

        level_required = int(recipe.get("level_required", 0) or 0)
        if player.level_index < level_required:
            return False, f"❌ 绘制【{name}】需要达到境界等级 {level_required}。"

        is_master = player.life_skill == "符箓师"
        gold_cost = int(recipe.get("gold_cost", 0) or 0)
        if is_master:
            gold_cost = int(gold_cost * 0.8)
        gold_cost *= count
        if player.gold < gold_cost:
            return False, f"❌ 灵石不足！绘制【{name}】x{count} 需要 {gold_cost:,} 灵石。"

        volume = int(self._item_data(name).get("volume", 1) or 1)
        bonus = 1 if is_master and random.randint(1, 100) <= 20 else 0
        original_items = dict(player.get_storage_ring_items())

        # 逐次扣料，保证 count 份消耗正确
        if count == 1:
            material_ok, material_msg, consumed = self._consume_materials(player, recipe)
        else:
            consumed_all = []
            for _ in range(count):
                ok, msg, used = self._consume_materials(player, recipe)
                if not ok:
                    player.set_storage_ring_items(original_items)
                    return False, msg
                consumed_all.extend(used)
            material_ok, material_msg, consumed = True, "", consumed_all
        if not material_ok:
            player.set_storage_ring_items(original_items)
            return False, material_msg

        # 扣除符材后再检查容量，避免满背包时误判
        if self.storage_ring_manager and self.storage_ring_manager.get_available_slots(player) < volume * (count + bonus):
            player.set_storage_ring_items(original_items)
            return False, "❌ 储物戒空间不足，绘制失败（符材未消耗）。"

        player.gold -= gold_cost
        await self.db.update_player(player)

        success_rate = float(recipe.get("success_rate", 75))
        if is_master:
            success_rate += 10
        success_rate = max(5, min(98, success_rate))
        success_count = 0
        for _ in range(count):
            if random.randint(1, 100) <= success_rate:
                success_count += 1

        if success_count <= 0:
            cost_text = "、".join(consumed) if consumed else "无"
            return True, (
                f"💥 绘制失败\n━━━━━━━━━━━━━━━\n"
                f"符纸承载不住灵力，化为灰烬。\n消耗：{cost_text}\n成功率：{success_rate:.0f}%\n"
                f"━━━━━━━━━━━━━━━"
            )

        final_count = success_count + bonus
        ok, msg = await self.storage_ring_manager.store_item(player, name, final_count, silent=True)
        if not ok:
            player.gold += gold_cost
            await self.db.update_player(player)
            return False, f"❌ 符箓存入储物戒失败：{msg}（灵石已退回，符材已消耗）"

        cost_text = "、".join(consumed) if consumed else "无"
        bonus_text = "\n🌟 符箓师心得触发，额外多绘制 1 张！" if bonus else ""
        return True, (
            f"🖌️ 绘制成功！\n━━━━━━━━━━━━━━━\n"
            f"获得【{name}】x{final_count}{bonus_text}\n"
            f"消耗：{cost_text}、灵石 -{gold_cost:,}\n"
            f"成功率：{success_rate:.0f}%\n"
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
        if self._item_data(name).get("type") != "符箓":
            return False, f"❌ 【{name}】不是符箓。"

        profile = self.get_talisman_profile(name)
        effects = profile["effects"]

        if "heal_percent" in effects:
            percent = min(1.0, float(effects["heal_percent"]) * count)
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
            return True, f"✨ 使用【{name}】x{count}\n━━━━━━━━━━━━━━━\n{result}\n━━━━━━━━━━━━━━━"

        if "physical_defense_multiplier" in effects:
            base_def = (
                int(getattr(player, "physical_defense", 0) or 0)
                + int(getattr(player, "magic_defense", 0) or 0)
                + max(1, int(player.experience) // 50)
            )
            def_mult = max(
                float(effects.get("physical_defense_multiplier", 0) or 0),
                float(effects.get("magic_defense_multiplier", 0) or 0),
            )
            effects["defense_bonus"] = int(base_def * def_mult)
            profile["description"] += f"（另获得护体值+{effects['defense_bonus']:,}）"

        now = int(time.time())
        duration_minutes = int(profile["duration"]) * count
        duration = duration_minutes * 60
        active = [
            effect for effect in player.get_active_pill_effects()
            if effect.get("talisman_name") != name
        ]
        active.append({
            "pill_name": name,
            "talisman_name": name,
            "source": "talisman",
            "subtype": "talisman",
            "category": profile["category"],
            "quality": "一般",
            "start_time": now,
            "expiry_time": now + duration,
            "duration_minutes": duration_minutes,
            "last_tick_time": now,
            **effects,
        })
        player.set_active_pill_effects(active)
        items[name] = have - count
        if items[name] <= 0:
            del items[name]
        player.set_storage_ring_items(items)
        await self.db.update_player(player)
        return True, (
            f"✨ 使用【{name}】x{count} 成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"类别：{profile['category']}\n"
            f"效果：{profile['description']}\n"
            f"持续：{duration_minutes}分钟\n"
            f"━━━━━━━━━━━━━━━"
        )

    # ===== 跨系统加成 =====
    @staticmethod
    def get_active_bonus(player: Player, key: str) -> float:
        total = 0.0
        now = int(time.time())
        for effect in player.get_active_pill_effects():
            if effect.get("source") != "talisman":
                continue
            expiry = effect.get("expiry_time", 0)
            if expiry > 0 and now >= expiry:
                continue
            try:
                total += float(effect.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
        return total

    @staticmethod
    def collect_active_combat_effects(player: Player) -> Dict[str, float]:
        keys = {
            "crit_rate_bonus": "crit_rate",
            "crit_damage_bonus": "crit_damage",
            "lifesteal_bonus": "lifesteal",
            "counter_damage_bonus": "counter_damage",
            "combo_rate_bonus": "combo_rate",
            "dodge_bonus": "dodge",
            "defense_bonus": "defense_bonus",
        }
        result = {}
        for source_key, target_key in keys.items():
            value = TalismanManager.get_active_bonus(player, source_key)
            if value:
                result[target_key] = result.get(target_key, 0) + value
        return result
