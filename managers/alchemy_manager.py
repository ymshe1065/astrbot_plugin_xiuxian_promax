# managers/alchemy_manager.py
"""炼丹系统管理器：品质、丹方学习、材料类别消耗与事务防并发。"""

import json
import random
import time
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..data.data_manager import DataBase
from ..models import Player
from ..models_extended import UserStatus

if TYPE_CHECKING:
    from ..config_manager import ConfigManager
    from ..core import StorageRingManager


class AlchemyManager:
    """炼丹系统管理器（防并发版）"""

    QUALITY_ORDER = ["一般", "良好", "极佳", "大师"]

    def __init__(self, db: DataBase, config_manager: "ConfigManager" = None, storage_ring_manager: "StorageRingManager" = None):
        self.db = db
        self.config_manager = config_manager
        self._social = None
        self.storage_ring_manager = storage_ring_manager
        self.config = config_manager.alchemy_config if config_manager else {}

        raw_recipes = {}
        if config_manager:
            raw_recipes = getattr(config_manager, "pill_recipes_data", None) or getattr(config_manager, "alchemy_recipes", None) or {}

        self.recipes: Dict[int, Dict] = {}
        if isinstance(raw_recipes, dict):
            iterable = raw_recipes.items()
        else:
            iterable = ((recipe.get("id"), recipe) for recipe in raw_recipes if isinstance(recipe, dict))
        for key, recipe in iterable:
            if not isinstance(recipe, dict):
                continue
            try:
                recipe_id = int(recipe.get("id", key))
            except (TypeError, ValueError):
                continue
            self.recipes[recipe_id] = self._normalize_recipe(recipe_id, recipe)

    def _normalize_recipe(self, recipe_id: int, recipe: Dict) -> Dict:
        name = recipe.get("name") or recipe.get("output_pill") or f"丹药{recipe_id}"
        output_pill = recipe.get("output_pill") or recipe.get("output") or name

        desc = recipe.get("desc")
        if not desc and self.config_manager:
            pill_config = self._get_pill_config_by_name(output_pill)
            if pill_config:
                desc = self._generate_pill_desc(pill_config)
        if not desc:
            desc = "丹药效果"

        materials = recipe.get("materials", recipe.get("cost", []))
        gold_cost = recipe.get("gold_cost", 0)
        if isinstance(materials, dict) and "灵石" in materials:
            gold_cost = materials.get("灵石", gold_cost)
            materials = {k: v for k, v in materials.items() if k != "灵石"}

        return {
            "id": recipe.get("id", recipe_id),
            "name": name,
            "output_pill": output_pill,
            "level_required": int(recipe.get("level_required", recipe.get("level", 0)) or 0),
            "materials": materials,
            "gold_cost": int(gold_cost or 0),
            "success_rate": float(recipe.get("success_rate", recipe.get("success", 50)) or 50),
            "price": int(recipe.get("price", 0) or 0),
            "shop_weight": int(recipe.get("shop_weight", 10) or 10),
            "desc": desc,
        }

    def _generate_pill_desc(self, pill_config: Dict) -> str:
        rank = pill_config.get("rank", "")
        if pill_config.get("exp_gain"):
            return f"增加{pill_config['exp_gain']}修为（{rank}修为丹）"
        if pill_config.get("breakthrough_bonus"):
            bonus = int(pill_config["breakthrough_bonus"] * 100)
            return f"提升{bonus}%突破成功率（{rank}破境丹）"
        if pill_config.get("description"):
            return pill_config["description"]
        return f"{rank}丹药"

    def _get_pill_config_by_name(self, name: str) -> Optional[Dict]:
        if not self.config_manager:
            return None
        for attr in ("exp_pills_data", "utility_pills_data", "pills_data"):
            data = getattr(self.config_manager, attr, {}) or {}
            if name in data:
                return data[name]
        item = (getattr(self.config_manager, "items_data", {}) or {}).get(name)
        if isinstance(item, dict) and item.get("type") == "丹药":
            return item
        return None

    # ===== 丹方学习 =====
    def _default_recipe_ids(self) -> List[int]:
        return sorted(self.recipes)[:3]

    def get_learned_recipe_ids(self, player: Player) -> List[int]:
        try:
            raw = json.loads(player.learned_recipes) if getattr(player, "learned_recipes", "") else []
        except Exception:
            raw = []
        if not isinstance(raw, list):
            raw = []
        learned = []
        for value in raw:
            try:
                learned.append(int(value))
            except (TypeError, ValueError):
                continue
        return learned

    async def ensure_learned_recipes(self, player: Player, commit: bool = True) -> List[int]:
        learned = self.get_learned_recipe_ids(player)
        if not learned:
            learned = self._default_recipe_ids()
            player.learned_recipes = json.dumps(learned, ensure_ascii=False)
            try:
                await self.db.update_player(player, commit=commit)
            except Exception:
                pass
        return learned

    def get_recipe_by_identifier(self, identifier: str) -> Optional[Dict]:
        text = str(identifier or "").strip()
        if not text:
            return None
        try:
            recipe = self.recipes.get(int(text))
            if recipe:
                return recipe
        except (TypeError, ValueError):
            pass
        clean = text.strip("《》")
        for recipe in self.recipes.values():
            if clean in (recipe.get("name"), recipe.get("output_pill")):
                return recipe
        for recipe in self.recipes.values():
            if self.config_manager and self.config_manager.get_recipe_scroll_name(recipe) == text:
                return recipe
        return None

    async def learn_recipe(self, player: Player, identifier: str) -> Tuple[bool, str]:
        recipe = self.get_recipe_by_identifier(identifier)
        if not recipe:
            return False, f"❌ 未找到丹方【{identifier}】。发送 /丹方 查看已学丹方；/丹阁 丹方 查看可购买丹方。"

        learned = await self.ensure_learned_recipes(player)
        recipe_id = int(recipe.get("id"))
        if recipe_id in learned:
            return False, f"❌ 你已经学会【{recipe.get('name')}】了。"

        scroll_name = self.config_manager.get_recipe_scroll_name(recipe) if self.config_manager else f"《{recipe.get('name')}》"
        ring_items = player.get_storage_ring_items()
        if ring_items.get(scroll_name, 0) > 0:
            if ring_items[scroll_name] <= 1:
                del ring_items[scroll_name]
            else:
                ring_items[scroll_name] -= 1
            player.set_storage_ring_items(ring_items)
            cost_note = f"消耗丹方卷轴【{scroll_name}】x1"
        else:
            price = int(recipe.get("price", 0) or 0)
            if price <= 0:
                return False, f"❌ 你的储物戒中没有【{scroll_name}】，请先到 /丹阁 丹方 购买。"
            if player.gold < price:
                return False, f"❌ 学习【{recipe.get('name')}】需要 {price:,} 灵石，或先从丹方阁购买卷轴。"
            player.gold -= price
            cost_note = f"直接参悟，花费 {price:,} 灵石"

        learned.append(recipe_id)
        player.learned_recipes = json.dumps(sorted(set(learned)), ensure_ascii=False)
        await self.db.update_player(player)
        return True, (
            f"📜 学习成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"已掌握丹方：【{recipe.get('name')}】\n"
            f"可炼制：{recipe.get('output_pill')}\n"
            f"{cost_note}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 使用 /炼丹 {recipe_id} 开始炼制"
        )

    # ===== 材料 =====
    def _infer_material_category(self, name: str) -> str:
        if any(s in name for s in ["草", "花", "芝", "参", "藤", "叶", "果"]):
            return "灵植"
        if any(s in name for s in ["液", "泉", "露", "血"]):
            return "灵液"
        if any(s in name for s in ["骨", "皮", "筋", "角", "鳞", "爪", "羽"]):
            return "妖兽"
        if any(s in name for s in ["石", "铁", "沙", "晶", "矿", "金", "银", "铜"]):
            return "矿材"
        return "特殊"

    def _material_info(self, name: str) -> Dict:
        if not self.config_manager:
            return {}
        try:
            info = self.config_manager.get_material_info(name)
        except Exception:
            info = {}
        if info:
            return info
        item = (getattr(self.config_manager, "items_data", {}) or {}).get(name)
        if isinstance(item, dict) and item.get("type") == "材料":
            return {
                "name": name,
                "category": self._infer_material_category(name),
                "tier": item.get("rank", "黄阶"),
            }
        return {}

    def _iter_recipe_materials(self, recipe: Dict):
        materials = recipe.get("materials", {})
        if isinstance(materials, dict):
            for name, count in materials.items():
                yield {"name": str(name), "count": int(count or 0)}
            return
        if isinstance(materials, list):
            for material in materials:
                if isinstance(material, dict):
                    yield {
                        "name": material.get("name"),
                        "category": material.get("category"),
                        "tier": material.get("tier"),
                        "count": int(material.get("count", 0) or 0),
                    }

    def _find_material_stacks(self, player: Player, spec: Dict) -> List[Tuple[str, int]]:
        stacks = []
        for item_name, count in player.get_storage_ring_items().items():
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
            if not info:
                continue
            if spec.get("category") and info.get("category") != spec["category"]:
                continue
            if spec.get("tier") and info.get("tier") != spec["tier"]:
                continue
            stacks.append((item_name, count))
        return stacks

    def _material_count(self, player: Player, spec: Dict) -> int:
        stacks = self._find_material_stacks(player, spec)
        if stacks:
            return sum(count for _, count in stacks)
        if spec.get("name") and self.storage_ring_manager:
            try:
                return int(self.storage_ring_manager.get_item_count(player, spec["name"]))
            except Exception:
                return 0
        return 0

    def _iter_available_stacks(self, available: Dict[str, int], spec: Dict):
        """在可用材料快照中查找匹配堆叠。"""
        for item_name, count in list(available.items()):
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            if spec.get("name"):
                if item_name == spec["name"]:
                    yield item_name, count
                continue
            info = self._material_info(item_name)
            if not info:
                continue
            if spec.get("category") and info.get("category") != spec["category"]:
                continue
            if spec.get("tier") and info.get("tier") != spec["tier"]:
                continue
            yield item_name, count

    def _plan_materials(self, player: Player, recipe: Dict):
        """模拟扣料：具名材料优先预留，避免被同类/同阶泛材料提前吃掉。"""
        available = {}
        for item_name, count in player.get_storage_ring_items().items():
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if count > 0:
                available[item_name] = count

        specs = list(self._iter_recipe_materials(recipe))
        ordered = sorted(specs, key=lambda item: 0 if item.get("name") else 1)
        plan = []
        missing = []
        for spec in ordered:
            need = int(spec.get("count", 0) or 0)
            taken = []
            for item_name, have in self._iter_available_stacks(available, spec):
                if need <= 0:
                    break
                take = min(need, have)
                left = available[item_name] - take
                if left <= 0:
                    del available[item_name]
                else:
                    available[item_name] = left
                need -= take
                taken.append((item_name, take))
            if need > 0:
                label = spec.get("name") or f"{spec.get('tier', '')}{spec.get('category', '')}材料"
                missing.append(f"{label}（缺{need}）")
            plan.append((spec, taken))
        return not missing, missing, plan

    def _consume_materials(self, player: Player, recipe: Dict) -> Tuple[bool, str, List[str]]:
        ok, missing, plan = self._plan_materials(player, recipe)
        if not ok:
            return False, "❌ 材料不足！\n" + "\n".join(f"  · {m}" for m in missing), []

        ring_items = player.get_storage_ring_items()
        consumed = []
        for _, taken in plan:
            for item_name, take in taken:
                left = int(ring_items.get(item_name, 0) or 0) - take
                if left <= 0:
                    ring_items.pop(item_name, None)
                else:
                    ring_items[item_name] = left
                consumed.append(f"{item_name}×{take}")
        player.set_storage_ring_items(ring_items)
        return True, "", consumed

    def format_recipe_requirements(self, player: Player, recipe: Dict) -> Tuple[str, bool]:
        """列出丹方所需材料、灵石及当前持有情况；返回 (文本, 是否齐全)。"""
        _, missing, plan = self._plan_materials(player, recipe)
        missing_labels = "；".join(missing)
        parts = []
        ready = not missing
        for spec, taken in plan:
            need = int(spec.get("count", 0) or 0)
            have = sum(take for _, take in taken)
            label = spec.get("name") or f"{spec.get('tier', '')}{spec.get('category', '')}材料"
            if have >= need:
                status = f"现有{have} ✓"
            else:
                status = f"现有{have}，缺{need - have}"
            parts.append(f"  · {label} x{need}（{status}）")
        gold_cost = int(recipe.get("gold_cost", 0) or 0)
        if gold_cost > 0:
            if player.gold >= gold_cost:
                status = f"现有{player.gold:,} ✓"
            else:
                status = f"现有{player.gold:,}，缺{gold_cost - player.gold:,}"
                ready = False
            parts.append(f"  · 灵石 x{gold_cost:,}（{status}）")
        text = "\n".join(parts) if parts else "  · 无"
        if missing_labels:
            text += f"\n  ⚠️ 材料冲突/不足：{missing_labels}"
        return text, ready

    # ===== 五行/职业炼丹加成 =====
    def get_alchemy_bonus(self, player: Player) -> Dict:
        root = str(getattr(player, "spiritual_root", "") or "").replace("灵根", "")
        variant_map = {"雷": "木", "风": "木", "冰": "水", "暗": "水", "光": "火"}
        elements = []
        for char in root:
            mapped = variant_map.get(char, char)
            if mapped in "金木水火土":
                elements.append(mapped)
        total = max(1, len(elements))
        wood_share = elements.count("木") / total
        fire_share = elements.count("火") / total

        quality_cfg = {}
        try:
            quality_cfg = self.config_manager.pill_quality_data.get("root_bonus", {})
        except Exception:
            quality_cfg = {}
        wood_cfg = quality_cfg.get("木", {})
        fire_cfg = quality_cfg.get("火", {})

        success_bonus = min(float(wood_cfg.get("cap", 10) or 10), float(wood_cfg.get("success_rate", 5) or 5) * wood_share)
        perfect_bonus = min(float(fire_cfg.get("cap", 20) or 20), float(fire_cfg.get("perfect_rate", 15) or 15) * fire_share)

        if getattr(player, "life_skill", "") == "炼丹师":
            success_bonus += 10

        return {
            "success_rate": success_bonus,
            "perfect_rate": perfect_bonus,
            "fire_tolerance": 1 if fire_share >= 0.5 else 0,
            "root": root,
        }

    # ===== 丹炉 =====
    def _get_bonus_data(self, player: Player) -> dict:
        try:
            data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get_furnace_level(self, player: Player) -> int:
        """丹炉等级（0-5），每级炼丹成功率 +3%"""
        return int(self._get_bonus_data(player).get("alchemy_furnace", 0))

    def get_furnace_bonus_rate(self, player: Player) -> int:
        return self.get_furnace_level(player) * 3

    async def upgrade_furnace(self, player: Player) -> Tuple[bool, str]:
        level = self.get_furnace_level(player)
        if level >= 5:
            return False, "❌ 你的丹炉已达最高等级【五品玄天炉】。"

        cost = 20000 * (level + 1)
        if player.gold < cost:
            return False, f"❌ 灵石不足！升级丹炉到 {level + 1} 级需要 {cost:,} 灵石。"

        player.gold -= cost
        data = self._get_bonus_data(player)
        data["alchemy_furnace"] = level + 1
        player.permanent_bonus = json.dumps(data, ensure_ascii=False)
        await self.db.update_player(player)

        furnace_names = ["凡铁炉", "青石炉", "紫金炉", "灵纹炉", "八卦炉", "五品玄天炉"]
        return True, (
            f"🔥 丹炉升级成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{furnace_names[level]} → 【{furnace_names[level + 1]}】\n"
            f"炼丹成功率加成：+{level * 3}% → +{(level + 1) * 3}%\n"
            f"花费：{cost:,} 灵石"
        )

    # ===== 火候小游戏 =====
    FIRE_STATE_PREFIX = "firecontrol_"
    FIRE_OPTIONS = {
        "1": {"label": "猛火急攻", "rate_bonus": -8, "perfect_bonus": 8,
              "desc": "孤注一掷！成功率-8%，但极品概率大增"},
        "2": {"label": "文火慢煨", "rate_bonus": 6, "perfect_bonus": -6,
              "desc": "稳中求进！成功率+6%，难出极品"},
        "3": {"label": "控火守中", "rate_bonus": 0, "perfect_bonus": 0,
              "desc": "不偏不倚，一切如常"},
    }

    async def start_fire_control(
        self,
        user_id: str,
        pill_id: int,
        recipe_name: str,
        requirements: str = "",
        output_pill: str = "",
        success_rate: float = 0,
    ) -> Tuple[bool, str]:
        state = {"pill_id": pill_id, "recipe": recipe_name, "expire": int(time.time()) + 180}
        await self.db.ext.set_system_config(
            self.FIRE_STATE_PREFIX + user_id, json.dumps(state, ensure_ascii=False)
        )
        opts = "\n".join(f"  [{k}] {v['label']} —— {v['desc']}" for k, v in self.FIRE_OPTIONS.items())
        header = [f"🔥 药材入炉，丹火升腾！", "━━━━━━━━━━━━━━━", f"丹方：【{recipe_name}】"]
        if output_pill:
            header.append(f"出炉：{output_pill}")
        header.append("出炉所需材料：")
        header.append(requirements or "  · 无")
        if success_rate:
            header.append(f"基础成功率：{success_rate:.0f}%")
        header.extend([
            "━━━━━━━━━━━━━━━",
            "正在炼制，请选择火候：",
            opts,
            "━━━━━━━━━━━━━━━",
            "💡 3分钟内发送 /火候 <编号> 掌控丹炉（超时按控火守中处理）",
        ])
        return True, "\n".join(header)

    async def pop_fire_state(self, user_id: str) -> Tuple[Optional[int], Optional[str]]:
        raw = await self.db.ext.get_system_config(self.FIRE_STATE_PREFIX + user_id)
        if not raw:
            return None, None
        await self.db.ext.set_system_config(self.FIRE_STATE_PREFIX + user_id, "")
        try:
            state = json.loads(raw)
        except Exception:
            return None, None
        if int(time.time()) > int(state.get("expire", 0)):
            return state.get("pill_id"), "3"
        return state.get("pill_id"), state.get("choice", "3")

    async def set_fire_choice(self, user_id: str, choice: str) -> Tuple[bool, str]:
        raw = await self.db.ext.get_system_config(self.FIRE_STATE_PREFIX + user_id)
        if not raw:
            return False, "❌ 你当前没有正在炼制的丹药。先发送 /炼丹 <ID>。"
        try:
            state = json.loads(raw)
        except Exception:
            return False, "❌ 火候数据异常，请重新 /炼丹。"
        if choice not in self.FIRE_OPTIONS:
            return False, "❌ 请输入 1-3 的编号选择火候。"
        if int(time.time()) > int(state.get("expire", 0)):
            await self.db.ext.set_system_config(self.FIRE_STATE_PREFIX + user_id, "")
            return False, "⌛ 炉火已熄（超时），请重新 /炼丹。"
        state["choice"] = choice
        await self.db.ext.set_system_config(
            self.FIRE_STATE_PREFIX + user_id, json.dumps(state, ensure_ascii=False)
        )
        opt = self.FIRE_OPTIONS[choice]
        return True, f"🔥 你以「{opt['label']}」掌控丹炉！（{opt['desc']}）\n💡 现在发送 /炼丹 <同一ID> 开炉成丹！"

    # ===== 品质 =====
    def _quality_weights(self, player: Player, fire_choice: str, furnace_level: int, sect_level: int) -> Dict[str, float]:
        try:
            config = self.config_manager.pill_quality_data
        except Exception:
            config = {}
        qualities = config.get("qualities", {})
        weights = {name: float(info.get("weight", 0) or 0) for name, info in qualities.items()}
        if not weights:
            weights = {"一般": 55, "良好": 30, "极佳": 12, "大师": 3}

        fire_mods = config.get("fire_modifiers", {}).get(str(fire_choice), {})
        furnace_mods = config.get("furnace_per_level", {})
        sect_mods = config.get("sect_per_level", {})
        for quality in weights:
            weights[quality] += float(fire_mods.get(quality, 0) or 0)
            weights[quality] += float(furnace_mods.get(quality, 0) or 0) * furnace_level
            weights[quality] += float(sect_mods.get(quality, 0) or 0) * sect_level

        root_bonus = self.get_alchemy_bonus(player)
        perfect = float(root_bonus.get("perfect_rate", 0) or 0)
        try:
            from .talisman_manager import TalismanManager
            perfect += float(TalismanManager.get_active_bonus(player, "alchemy_quality_bonus"))
        except Exception:
            pass
        if perfect:
            weights["极佳"] = weights.get("极佳", 0) + perfect * 0.7
            weights["大师"] = weights.get("大师", 0) + perfect * 0.3

        for quality in weights:
            weights[quality] = max(1.0, min(100.0, weights[quality]))
        return weights

    def _roll_quality(self, player: Player, fire_choice: str, furnace_level: int, sect_level: int) -> str:
        weights = self._quality_weights(player, fire_choice, furnace_level, sect_level)
        total = sum(weights.values())
        roll = random.uniform(0, total)
        cumulative = 0.0
        for quality in self.QUALITY_ORDER:
            cumulative += weights.get(quality, 0)
            if roll <= cumulative:
                return quality
        return "一般"

    # ===== 丹方列表 =====
    async def get_available_recipes(self, user_id: str) -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id) if self.db else None
        if not player:
            return False, "❌ 你还未踏入修仙之路！"

        learned = await self.ensure_learned_recipes(player)
        lines = ["📜 已学丹方列表", "━━━━━━━━━━━━━━━"]
        # 这里只列出已学习丹方；未学习丹方请到 /丹阁 丹方 购买卷轴后学习。
        learned_ids = []
        for value in learned:
            try:
                recipe_id = int(value)
            except (TypeError, ValueError):
                continue
            if recipe_id in self.recipes and recipe_id not in learned_ids:
                learned_ids.append(recipe_id)

        if not learned_ids:
            lines.append("你还没有学习任何丹方。")
            lines.append("💡 前往 /丹阁 丹方 购买丹方卷轴，再使用 /学习丹方。")
            return True, "\n".join(lines)

        for recipe_id in sorted(learned_ids):
            recipe = self.recipes[recipe_id]
            level_required = int(recipe.get("level_required", 0) or 0)
            if player.level_index < level_required:
                status = f"需境界 {level_required}"
            else:
                status = "可炼制"

            material_parts = []
            for spec in self._iter_recipe_materials(recipe):
                have = self._material_count(player, spec)
                label = spec.get("name") or f"{spec.get('tier', '')}{spec.get('category', '')}"
                mark = "✓" if have >= spec["count"] else f"缺{spec['count'] - have}"
                material_parts.append(f"{label}x{spec['count']}({mark})")
            if recipe.get("gold_cost"):
                mark = "✓" if player.gold >= recipe["gold_cost"] else f"缺{recipe['gold_cost'] - player.gold}"
                material_parts.append(f"灵石x{recipe['gold_cost']}({mark})")

            lines.append(f"[{recipe_id}] {recipe['name']}（{status}，成功率 {recipe.get('success_rate', 0):.0f}%）")
            if material_parts:
                lines.append("   材料：" + "、".join(material_parts))
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("💡 /学习丹方 <ID或名称> 学习；/炼丹 <ID> 开始炼制")
        return True, "\n".join(lines)

    # ===== 炼制 =====
    async def craft_pill(self, user_id: str, pill_id: int, fire_choice: Optional[str] = None) -> Tuple[bool, str, Optional[Dict]]:
        """炼制丹药：首次投料进入火候选择，选好后再次 /炼丹 开炉。"""
        explicit_fire = None
        if fire_choice is not None:
            mapping = {"猛火": "1", "猛火急攻": "1", "文火": "2", "文火慢煨": "2", "守中": "3", "控火守中": "3"}
            explicit_fire = mapping.get(str(fire_choice).strip(), str(fire_choice).strip())

        fire_pill_id, state_fire = await self.pop_fire_state(user_id)
        if explicit_fire is not None:
            fire_choice = explicit_fire if explicit_fire in self.FIRE_OPTIONS else "3"
            fire_matched = True
        elif fire_pill_id is not None and int(fire_pill_id) == int(pill_id):
            fire_choice = state_fire if state_fire in self.FIRE_OPTIONS else "3"
            fire_matched = True
        else:
            fire_choice = "3"
            fire_matched = False

        if not fire_matched:
            player = await self.db.get_player_by_id(user_id)
            if not player:
                return False, "❌ 你还未踏入修仙之路！", None
            recipe = self.recipes.get(int(pill_id))
            if not recipe:
                return False, "❌ 无效的丹药ID！", None
            learned = await self.ensure_learned_recipes(player)
            if int(pill_id) not in learned:
                return False, f"❌ 你还没有学会【{recipe['name']}】，请先 /学习丹方 {pill_id}。", None
            if player.level_index < recipe["level_required"]:
                return False, f"❌ 炼制{recipe['name']}需要达到境界等级 {recipe['level_required']}！", None
            requirements, ready = self.format_recipe_requirements(player, recipe)
            if not ready:
                return False, (
                    f"❌ 材料或灵石不足，暂时无法炼制【{recipe['name']}】\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"出炉：{recipe.get('output_pill', '')}\n"
                    f"所需材料：\n{requirements}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"💡 补齐材料后再发送 /炼丹 {pill_id}；可用 /丹药配方 查看已学丹方"
                ), None
            return await self.start_fire_control(
                user_id,
                int(pill_id),
                recipe["name"],
                requirements,
                recipe.get("output_pill", ""),
                recipe.get("success_rate", 0),
            ) + (
                {"success": False, "pending_fire": True},
            )

        fire_cfg = self.FIRE_OPTIONS.get(fire_choice or "3", self.FIRE_OPTIONS["3"])

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(user_id)
            if not player:
                await self.db.conn.rollback()
                return False, "❌ 你还未踏入修仙之路！", None

            if pill_id not in self.recipes:
                await self.db.conn.rollback()
                return False, "❌ 无效的丹药ID！", None

            recipe = self.recipes[pill_id]
            learned = await self.ensure_learned_recipes(player, commit=False)
            if int(pill_id) not in learned:
                await self.db.conn.rollback()
                return False, f"❌ 你还没有学会【{recipe['name']}】，请先 /学习丹方 {pill_id}。", None

            if player.level_index < recipe["level_required"]:
                await self.db.conn.rollback()
                return False, f"❌ 炼制{recipe['name']}需要达到境界等级 {recipe['level_required']}！", None

            output_pill = recipe["output_pill"]
            if not self._get_pill_config_by_name(output_pill):
                await self.db.conn.rollback()
                return False, f"❌ 丹方【{recipe['name']}】的目标丹药【{output_pill}】配置缺失，请联系管理员。", None

            gold_cost = int(recipe.get("gold_cost", 0) or 0)
            if player.gold < gold_cost:
                await self.db.conn.rollback()
                return False, f"❌ 灵石不足！炼制【{recipe['name']}】需要 {gold_cost:,} 灵石。", None

            material_ok, material_msg, consumed_materials = self._consume_materials(player, recipe)
            if not material_ok:
                await self.db.conn.rollback()
                return False, material_msg, None

            player.gold -= gold_cost

            # 成功率
            furnace_level = self.get_furnace_level(player)
            furnace_bonus = furnace_level * 3
            level_bonus = min(20, (player.level_index - recipe["level_required"]) * 2)
            sect_level = 0
            try:
                if player.sect_id:
                    sect = await self.db.ext.get_sect_by_id(player.sect_id)
                    if sect:
                        sect_level = int(json.loads(getattr(sect, "buildings", "{}") or "{}").get("炼丹房", 0))
            except Exception:
                sect_level = 0
            sect_bonus = sect_level * 2

            companion_bonus = 0
            try:
                if self._social is None:
                    from .social_manager import SocialManager
                    self._social = SocialManager(self.db)
                companion_bonus = int(await self._social.companion_bonus_async(user_id, "alchemy") * 100)
            except Exception:
                companion_bonus = 0

            root_bonus = self.get_alchemy_bonus(player)
            rate_bonus = fire_cfg["rate_bonus"]
            if root_bonus.get("fire_tolerance") and rate_bonus < 0:
                rate_bonus += 4
            talisman_success = 0
            try:
                from .talisman_manager import TalismanManager
                talisman_success = int(TalismanManager.get_active_bonus(player, "alchemy_success_bonus"))
            except Exception:
                talisman_success = 0
            final_success_rate = min(
                95,
                max(5, recipe["success_rate"] + level_bonus + furnace_bonus + sect_bonus
                    + companion_bonus + root_bonus.get("success_rate", 0) + rate_bonus
                    + talisman_success),
            )
            is_success = random.randint(1, 100) <= final_success_rate
            quality = self._roll_quality(player, fire_choice, furnace_level, sect_level) if is_success else ""
            perfect = quality in ("极佳", "大师")

            cost_lines = []
            if gold_cost > 0:
                cost_lines.append(f"灵石 -{gold_cost}")
            cost_lines.extend(consumed_materials)
            cost_str = "、".join(cost_lines) if cost_lines else "无"

            if is_success:
                tribulation_msg = ""
                pill_ruined = False
                if recipe["level_required"] >= 6 and random.randint(1, 100) <= 25:
                    ring_items = player.get_storage_ring_items()
                    if self.storage_ring_manager and ring_items.get("引雷符", 0) >= 1:
                        if ring_items["引雷符"] <= 1:
                            del ring_items["引雷符"]
                        else:
                            ring_items["引雷符"] -= 1
                        player.set_storage_ring_items(ring_items)
                        tribulation_msg = ("\n⚡ 天降丹劫！你祭出【引雷符】引雷入体，"
                                           "雷光淬丹，丹药品质得以保全！（消耗引雷符x1）")
                    else:
                        try:
                            from .talisman_manager import TalismanManager
                            shield = TalismanManager.get_active_bonus(player, "tribulation_shield")
                        except Exception:
                            shield = 0
                        if shield > 0:
                            TalismanManager.consume_active_charge(player, "tribulation_shield")
                            tribulation_msg = ("\n⚡ 天降丹劫！避劫符自行燃烧，"
                                               "替你遮蔽天机，丹药安然出炉！（消耗避劫符护盾）")
                        else:
                            pill_ruined = True
                            tribulation_msg = ("\n💥 天降丹劫！你没有【引雷符】护身，"
                                               "雷火焚炉，丹药在最后一刻化为灰烬...")

                if not pill_ruined:
                    pill_key = f"{output_pill}|{quality}"
                    inventory = player.get_pills_inventory()
                    inventory[pill_key] = inventory.get(pill_key, 0) + 1
                    player.set_pills_inventory(inventory)

                await self.db.update_player(player, commit=False)

                if pill_ruined:
                    msg = f"""
💔 丹劫毁丹
━━━━━━━━━━━━━━━

炼制【{output_pill}】本已成功，却引来天雷降劫...

消耗：{cost_str}
成功率：{final_success_rate}%
品质：{quality}
{tribulation_msg}

💡 高阶丹药请随身携带【引雷符】（丹阁/符箓师可获取）
                    """.strip()
                    await self.db.conn.commit()
                    return True, msg, {
                        "success": False,
                        "pill_name": output_pill,
                        "quality": quality,
                        "cost": gold_cost,
                        "materials_consumed": consumed_materials,
                        "tribulation": True,
                    }

                perfect_note = f"\n🌟 火候与灵根共鸣，此丹品质达到【{quality}】！" if perfect else ""
                msg = f"""
🎉 炼丹成功！
━━━━━━━━━━━━━━━

你成功炼制了【{output_pill}·{quality}】！{perfect_note}
丹药已存入丹药背包

消耗：{cost_str}
成功率：{final_success_rate}%
火候：{fire_cfg['label']}
{tribulation_msg}

💡 使用 /服用丹药 {output_pill} {quality} 可服用此丹药
💡 使用 /丹药背包 查看所有丹药
                """.strip()
                result_data = {
                    "success": True,
                    "pill_name": output_pill,
                    "pill_key": f"{output_pill}|{quality}",
                    "quality": quality,
                    "cost": gold_cost,
                    "materials_consumed": consumed_materials,
                    "perfect": perfect,
                }
            else:
                await self.db.update_player(player, commit=False)
                msg = f"""
💔 炼丹失败
━━━━━━━━━━━━━━━

炼制【{recipe['name']}】失败了...

材料已消耗
消耗：{cost_str}
成功率：{final_success_rate}%

再接再厉！
                """.strip()
                result_data = {
                    "success": False,
                    "pill_name": recipe["output_pill"],
                    "cost": gold_cost,
                    "materials_consumed": consumed_materials,
                }

            await self.db.conn.commit()
            return True, msg, result_data

        except Exception:
            await self.db.conn.rollback()
            raise
