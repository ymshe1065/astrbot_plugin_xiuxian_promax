# managers/alchemy_manager.py
"""炼丹系统管理器：品质、丹方学习、材料类别消耗与事务防并发。"""

import json
import random
import re
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from ..core.material_selection import select_recipe_materials
from ..data.data_manager import DataBase
from ..models import Player
from ..models_extended import UserStatus
from ..core.talisman_rules import (
    TRIBULATION_DEMAND, TRIBULATION_RATE, pill_rank_of, rank_from_level,
)
from ..core.active_effects import format_duration

if TYPE_CHECKING:
    from ..config_manager import ConfigManager
    from ..core import StorageRingManager


class AlchemyManager:
    """炼丹系统管理器（防并发版）"""

    QUALITY_ORDER = ["一般", "良好", "极佳", "大师"]
    # 批量炼丹单次指令上限：避免一次输入 all 时长时间占库与刷屏
    MAX_BATCH_CRAFTS = 100
    # config/pill_recipes.json 的 692 条 desc 全是占位文案，两种写法：
    # 「记载XX炼制方法的丹方。」与「记载XX炼制方法的丹方，学习后可用 /炼丹 炼制。」
    # 原逻辑只在 `not desc` 时才调 _generate_pill_desc，于是生成器成了死代码——
    # 丹方列表因此永远不显示「这炉丹做出来干什么」（F9 效果缺位）。只用前缀判定，
    # 因为后半句有没有「学习后可用 …」并不影响它是不是废话。
    _PLACEHOLDER_DESC_RE = re.compile(r"^记载.+?炼制方法的丹方")
    # items.json 里 443 条丹药的 description 全是这句，同样等于没写：
    # 「这是一件凡品丹药，蕴含着天地之力。」—— 它只说品阶，不说作用。
    _GENERIC_DESC_RE = re.compile(r"这是一件.+丹药|蕴含着天地之力")
    # _generate_pill_desc 也拿不到配置时的兜底；展示层据此显式写「效果未录入」。
    DESC_MISSING = "丹药效果"

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

        desc = str(recipe.get("desc") or "").strip()
        if self._PLACEHOLDER_DESC_RE.match(desc):
            desc = ""
        if not desc and self.config_manager:
            pill_config = self._get_pill_config_by_name(output_pill)
            if pill_config:
                desc = self._generate_pill_desc(pill_config)
        if not desc:
            desc = self.DESC_MISSING

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

    def _quality_cap(self, key: str = "effect_multiplier") -> float:
        """品质对效果的放大上界（一般 1.0 → 大师 1.6）。"""
        data = getattr(self.config_manager, "pill_quality_data", None) or {}
        qualities = data.get("qualities") if isinstance(data, dict) else None
        caps: List[float] = []
        for entry in (qualities or {}).values():
            if not isinstance(entry, dict):
                continue
            try:
                caps.append(float(entry.get(key, 1.0) or 1.0))
            except (TypeError, ValueError):
                continue
        return max(caps) if caps else 1.0

    def _generate_pill_desc(self, pill_config: Dict) -> str:
        """产出丹药的效果一句话，数值给到品质提升后的上界。

        品质倍率（一般 1.0 / 极佳 1.35 / 大师 1.6）只作用于数值型效果，
        所以直接写成区间——玩家只有看见「一般 500、大师 800」，
        「猛火博品质」这个选择才存在。区间上界取自 pill_quality.json，不硬编码。
        """
        rank = pill_config.get("rank", "")
        gain_cap = self._quality_cap("effect_multiplier")
        break_cap = self._quality_cap("breakthrough_multiplier")

        def span(value: float, cap: float, percent: bool = False) -> str:
            high = value * cap
            if percent:
                return f"{value:.0f}%~{high:.0f}%"
            return f"{int(round(value))}~{int(round(high))}"

        if pill_config.get("exp_gain"):
            return f"增加 {span(float(pill_config['exp_gain']), gain_cap)} 修为（{rank}修为丹）"
        if pill_config.get("breakthrough_bonus"):
            bonus = float(pill_config["breakthrough_bonus"]) * 100
            return f"提升 {span(bonus, break_cap, True)} 突破成功率（{rank}破境丹）"
        if pill_config.get("restore_hp_percent"):
            pct = float(pill_config["restore_hp_percent"]) * 100
            return f"恢复 {span(pct, gain_cap, True)} 最大气血"
        if pill_config.get("restore_qi_percent"):
            pct = float(pill_config["restore_qi_percent"]) * 100
            return f"恢复 {span(pct, gain_cap, True)} 最大灵气"
        desc = str(pill_config.get("description") or "").strip()
        if desc and not self._GENERIC_DESC_RE.search(desc):
            return desc
        # 走到这里说明数据层就没写效果（443 条丹药只有「这是一件X品丹药」）。
        # 返回空串，由 _normalize_recipe 统一落成 DESC_MISSING，让界面显式说「未录入」。
        return ""

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
    # 创建角色即学会的基础丹方：回春散·凡方(130)、聚灵散·凡方(131)
    INITIAL_LEARNED_RECIPE_IDS = (130, 131)

    def _default_recipe_ids(self) -> List[int]:
        for recipe_id in self.INITIAL_LEARNED_RECIPE_IDS:
            if recipe_id not in self.recipes:
                return sorted(self.recipes)[:3]
        return list(self.INITIAL_LEARNED_RECIPE_IDS)

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
        if int(ring_items.get(scroll_name, 0) or 0) > 0:
            left = int(ring_items.get(scroll_name, 0) or 0) - 1
            if left <= 0:
                ring_items.pop(scroll_name, None)
            else:
                ring_items[scroll_name] = left
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
                "tier": item.get("rank", "凡品"),
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

    def _items_snapshot(self, player: Player) -> Dict[str, int]:
        """储物戒快照（只读）。

        仓库物品不算数：炼丹只吃戒指里的材料，仓库里的必须先 /一键取出。
        """
        if self.storage_ring_manager:
            try:
                return self.storage_ring_manager.ring_items(player)
            except Exception:
                pass
        return dict(player.get_storage_ring_items())

    def _find_material_stacks(self, player: Player, spec: Dict) -> List[Tuple[str, int]]:
        stacks = []
        for item_name, count in self._items_snapshot(player).items():
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

    def _snapshot_available(self, player: Player) -> Dict[str, int]:
        available = {}
        for item_name, count in self._items_snapshot(player).items():
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if count > 0:
                available[item_name] = count
        return available

    def _plan_from_available(self, available: Dict[str, int], recipe: Dict):
        """在给定库存上模拟扣料，会直接消耗传入的 available 快照。"""
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

    def _plan_materials(self, player: Player, recipe: Dict):
        """模拟扣料：具名材料优先预留，避免被同类/同阶泛材料提前吃掉。"""
        return self._plan_from_available(self._snapshot_available(player), recipe)

    def max_craftable_count(self, player: Player, recipe: Dict, limit: Optional[int] = None) -> int:
        """当前材料/灵石最多能连续炼几次（默认按单次指令上限封顶）。"""
        cap = self.MAX_BATCH_CRAFTS if limit is None else max(0, int(limit))
        gold_cost = int(recipe.get("gold_cost", 0) or 0)
        if gold_cost > 0:
            cap = min(cap, max(0, int(player.gold) // gold_cost))
        if cap <= 0:
            return 0

        available = self._snapshot_available(player)
        count = 0
        while count < cap:
            ok, _, _ = self._plan_from_available(available, recipe)
            if not ok:
                break
            count += 1
        return count

    def _warehouse_note(self, player: Player, recipe: Dict) -> str:
        """仓库里有对应材料时提示先取出：否则玩家只看到"材料不足"，找不到东西。"""
        warehouse = {
            name: int(raw or 0)
            for name, raw in (player.get_warehouse_items() or {}).items()
            if int(raw or 0) > 0
        }
        if not warehouse:
            return ""
        hits: List[str] = []
        for spec in recipe.get("materials", []) or []:
            total = sum(count for _, count in self._iter_available_stacks(warehouse, spec))
            if total <= 0:
                continue
            label = spec.get("name") or f"{spec.get('tier', '')}{spec.get('category', '')}材料"
            hits.append(f"{label}×{total}")
        if not hits:
            return ""
        return f"\n　💡 仓库里有：{'、'.join(hits[:5])}，先 /一键取出 再炼。"

    def _consume_materials(self, player: Player, recipe: Dict) -> Tuple[bool, str, List[str]]:
        ok, missing, plan = self._plan_materials(player, recipe)
        if not ok:
            text = "❌ 材料不足！\n" + "\n".join(f"  · {m}" for m in missing)
            return False, text + self._warehouse_note(player, recipe), []

        ring_items = player.get_storage_ring_items()

        consumed = []
        for _, taken in plan:
            for item_name, take in taken:
                have = int(ring_items.get(item_name, 0) or 0)
                got = min(have, take)
                if got <= 0:
                    continue
                left = have - got
                if left <= 0:
                    ring_items.pop(item_name, None)
                else:
                    ring_items[item_name] = left
                consumed.append(f"{item_name}×{got}")
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

    def tribulation_hint(self, player: Player, output_pill: str, recipe: Dict = None) -> str:
        """开炉前的丹劫提示：手上已有够用的护劫符时返回空串（不打扰玩家）。

        丹劫是概率事件，但一旦落下且护盾不足就直接毁丹，所以在投料前提醒，
        而不是等丹药烧成灰之后再告诉玩家「护劫符可在 /符阁 购买」。
        """
        try:
            pill_rank = self._pill_rank_of(output_pill, recipe)
            demand = int(TRIBULATION_DEMAND.get(pill_rank, 0) or 0)
            trigger = int(TRIBULATION_RATE.get(pill_rank, 0) or 0)
        except Exception:
            return ""
        if demand <= 0 or trigger <= 0:
            return ""
        try:
            from .talisman_manager import TalismanManager
            has_talisman = TalismanManager.pick_shield_talisman(
                player, demand, self.config_manager
            ) is not None
            if not has_talisman:
                # 已激活的护劫符同样算「背包里备好了」
                has_talisman = int(
                    TalismanManager.get_active_max_bonus(player, "tribulation_shield") or 0
                ) >= demand
            best = int(TalismanManager.best_shield_available(player, self.config_manager) or 0)
        except Exception:
            return ""
        if has_talisman:
            return ""
        lines = [f"⚡ 丹劫预警：此丹为【{pill_rank}】，成丹有 {trigger}% 概率引劫（需护盾 ≥ {demand}）"]
        if best > 0:
            lines.append(f"   你手上最高护盾只有 {best}，劫雷落下丹药必毁")
        else:
            lines.append("   你身上没有护劫符，劫雷落下丹药必毁")
        lines.append(
            "   💡 护劫符图已常驻：/符阁 符图 → /购买 避劫符·符图（盾6）、混元符·符图（盾9）；"
            "成品符看 /符阁 符箓"
        )
        lines.append(
            "   💡 开炉前先 /使用 避劫符：状态期内同档丹劫全免，不必每颗丹现烧一张"
        )
        return "\n".join(lines)

    def _resolve_tribulation(self, player: Player, demand: int) -> Tuple[Optional[str], int, str]:
        """丹劫落地时的护劫判定，返回 (符名, 护盾, 状态说明)。

        1) 已有避劫状态且护盾够 → 直接免劫，不消耗任何符；
        2) 没有状态但储物戒有够用的护劫符 → 祭出一张，并把整段避劫状态挂上，
           所以后续同档丹劫都在状态里免掉，不再逐颗烧符；
        3) 都不满足 → (None, 0, "")，由调用方判定毁丹。
        """
        if demand <= 0:
            return None, 0, ""
        try:
            from .talisman_manager import TalismanManager
            state = TalismanManager.active_shield_state(player, demand)
            if state:
                return (
                    state["name"],
                    int(state["shield"]),
                    f"避劫状态尚余 {format_duration(state['remaining_minutes'])}",
                )
            pick = TalismanManager.pick_shield_talisman(player, demand, self.config_manager)
            if not pick:
                return None, 0, ""
            name = TalismanManager.consume_shield_talisman(player, self.config_manager, demand)
            granted = TalismanManager.grant_shield_state(
                player, name, self.config_manager
            ) if name else None
            if not granted:
                return None, 0, ""
            return (
                name,
                int(granted["shield"]),
                f"自此刻起 {format_duration(granted['minutes'])} 内丹劫皆可避",
            )
        except Exception:
            return None, 0, ""

    # ===== 五行/职业炼丹加成 =====
    def _pill_rank_of(self, output_pill: str, recipe: Dict = None) -> str:
        """丹药品阶：先查四张配置表，都查不到按丹方需求等级兜底。"""
        rank = pill_rank_of(output_pill, self.config_manager)
        if rank:
            return rank
        level_required = 0
        if isinstance(recipe, dict):
            try:
                level_required = int(recipe.get("level_required", 0) or 0)
            except (TypeError, ValueError):
                level_required = 0
        return rank_from_level(level_required)

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
        # 品质方向由 config/pill_quality.json 的 fire_modifiers 决定，
        # 这里只保留成功率加成与展示文案，避免出现两套互相矛盾的数值。
        "1": {"label": "猛火急攻", "rate_bonus": -8,
              "desc": "孤注一掷！成功率-8%，但极佳/大师概率提升"},
        "2": {"label": "文火慢煨", "rate_bonus": 6,
              "desc": "稳中求进！成功率+6%，但极佳/大师概率降低"},
        "3": {"label": "控火守中", "rate_bonus": 0,
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
        tribulation_hint: str = "",
        batch_count=None,
        projected: Optional[int] = None,
        materials=None,
    ) -> Tuple[bool, str]:
        state = {
            "pill_id": pill_id,
            "recipe": recipe_name,
            "expire": int(time.time()) + 180,
        }
        if materials:
            state["materials"] = materials
        if batch_count not in (None, 1):
            state["count"] = batch_count
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
        batch_note = self._batch_start_note(batch_count, projected)
        if batch_note:
            header.append(batch_note)
        if tribulation_hint:
            header.append("━━━━━━━━━━━━━━━")
            header.extend(tribulation_hint.splitlines())
        header.extend([
            "━━━━━━━━━━━━━━━",
            "正在炼制，请选择火候：",
            opts,
            "━━━━━━━━━━━━━━━",
            "💡 3分钟内发送 /火候 <编号> 掌控丹炉（超时按控火守中处理）",
        ])
        return True, "\n".join(header)

    @staticmethod
    def _batch_start_note(batch_count, projected: Optional[int]) -> str:
        if batch_count == "all":
            if projected:
                return f"🔥 批量炼制：开炉时消耗全部材料（当前最多可炼 {projected} 次）"
            return "🔥 批量炼制：开炉时消耗全部可用材料"
        if isinstance(batch_count, int) and batch_count > 1:
            note = f"🔥 批量炼制：连续炼丹 {batch_count} 次"
            if projected is not None and projected < batch_count:
                note += f"（当前材料/灵石仅够 {projected} 次）"
            return note
        return ""

    async def pop_fire_state(self, user_id: str, include_materials=False) -> tuple:
        empty = (None, None, None, None) if include_materials else (None, None, None)
        raw = await self.db.ext.get_system_config(self.FIRE_STATE_PREFIX + user_id)
        if not raw:
            return empty
        await self.db.ext.set_system_config(self.FIRE_STATE_PREFIX + user_id, "")
        try:
            state = json.loads(raw)
        except Exception:
            return empty
        choice = "3" if int(time.time()) > int(state.get("expire", 0)) else state.get("choice", "3")
        result = (state.get("pill_id"), choice, state.get("count"))
        return result + (state.get("materials"),) if include_materials else result

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
        batch_note = self._batch_start_note(state.get("count"), None)
        if batch_note:
            batch_note = "\n" + batch_note
        return True, f"🔥 你以「{opt['label']}」掌控丹炉！（{opt['desc']}）{batch_note}\n💡 现在发送 /炼丹 <同一ID> 开炉成丹！"

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
    async def get_available_recipes(self, user_id: str) -> Tuple[bool, str, list, list, list]:
        player = await self.db.get_player_by_id(user_id) if self.db else None
        if not player:
            return False, "❌ 你还未踏入修仙之路！", [], [], []

        learned = await self.ensure_learned_recipes(player)
        lines = ["📜 已学丹方列表", "━━━━━━━━━━━━━━━"]
        rows = []
        card_lines = []
        entries = []
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
            return True, "\n".join(lines), [], [], []

        held_items = self._items_snapshot(player)
        for recipe_id in sorted(learned_ids):
            recipe = self.recipes[recipe_id]
            level_required = int(recipe.get("level_required", 0) or 0)
            if player.level_index < level_required:
                status = f"需境界 {level_required}"
            else:
                status = "可炼制"
            success_rate = recipe.get("success_rate", 0)
            effect = str(recipe.get("desc") or "").strip()
            if not effect or effect == self.DESC_MISSING:
                # 443 条丹方的产出物在配置里压根没有效果定义。
                # 留白会被读成「这炉丹没用」，必须明写是配置缺失。
                effect = "⚠️ 效果未录入"
            card_lines.append(f"[{recipe_id}] {recipe['name']}（{status} · 成功率 {success_rate:.0f}%）")
            card_lines.append(f"   · 效果：{effect}")

            material_parts = []
            entry_materials = []
            materials_ok = True
            # 这一次开炉最多能连炼几次：材料与灵石的「够几次」取最小值
            cap = None
            for spec in self._iter_recipe_materials(recipe):
                have = self._material_count(player, spec)
                label = spec.get("name") or f"{spec.get('tier', '')}{spec.get('category', '')}"
                mark = "✓" if have >= spec["count"] else f"缺{spec['count'] - have}"
                material_parts.append(f"{label}x{spec['count']}({mark})")
                entry_materials.append({
                    "label": str(label), "need": int(spec["count"]), "have": int(have),
                })
                if spec["count"]:
                    rounds = int(have) // int(spec["count"])
                    cap = rounds if cap is None else min(cap, rounds)
                if have < spec["count"]:
                    materials_ok = False
                # 图片卡片逐行展示，标识改用文字，避免窄列挤压与 ✓ 字形缺失
                card_mark = "足够" if have >= spec["count"] else f"缺{spec['count'] - have}"
                card_lines.append(f"   · {label} ×{spec['count']}（拥有 {have}/{spec['count']} · {card_mark}）")
            gold_ok = True
            gold_cost = int(recipe.get("gold_cost") or 0)
            if gold_cost:
                mark = "✓" if player.gold >= gold_cost else f"缺{gold_cost - player.gold}"
                material_parts.append(f"灵石x{gold_cost}({mark})")
                gold_ok = player.gold >= gold_cost
                rounds = int(player.gold or 0) // gold_cost
                cap = rounds if cap is None else min(cap, rounds)
                card_mark = "足够" if player.gold >= gold_cost else f"缺{gold_cost - player.gold}"
                card_lines.append(f"   · 灵石 ×{gold_cost}（持有 {player.gold}/{gold_cost} · {card_mark}）")
            card_lines.append("")

            lines.append(f"[{recipe_id}] {recipe['name']}（{status}，成功率 {success_rate:.0f}%）")
            # 未录入时用 ⚠️ 起头，别再叠一个 ✨：那会读成「特效：效果未录入」。
            lines.append(f"   {effect}" if effect.startswith("⚠️") else f"   ✨ {effect}")
            if material_parts:
                lines.append("   材料：" + "、".join(material_parts))
            rows.append((
                f"[{recipe_id}] {recipe['name']}（{status} · 成功率{success_rate:.0f}%）",
                "材料：" + "、".join(material_parts) if material_parts else "无材料消耗",
            ))

            if player.level_index < level_required:
                state = "lock"
            elif not materials_ok or not gold_ok:
                state = "lack"
            else:
                state = "ok"
            output_pill = str(recipe.get("output_pill") or "")
            entries.append({
                "id": recipe_id,
                "name": str(recipe["name"]),
                "kind": "丹方",
                "rank": self._pill_rank_of(output_pill, recipe),
                "rate": int(round(float(success_rate or 0))),
                "state": state,
                "effect": effect,
                "materials": entry_materials,
                "cost": gold_cost,
                "gold": int(player.gold or 0),
                # 详情抽屉数量步进器的上限；None=算不出来（没材料也没工费），交给后端兜底
                "max": cap,
                "level": level_required,
                "level_now": int(player.level_index or 0),
                "held": int(held_items.get(output_pill, 0) or 0) if output_pill else 0,
                "output": output_pill,
                "unit": "颗",
                "source": "recipe",
                "action": "炼制",
            })
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("💡 /学习丹方 <ID或名称> 学习；/炼丹 <ID> [火候] [次数|all] 开始炼制")
        return True, "\n".join(lines), rows, card_lines, entries

    # ===== 炼制 =====
    @staticmethod
    def _normalize_batch_count(count) -> Optional[object]:
        """批量次数：None / 正整数 / "all"。"""
        if count is None:
            return None
        text = str(count).strip().lower()
        if text in ("all", "全部", "所有"):
            return "all"
        if text.startswith("x") or text.startswith("×"):
            text = text[1:]
        if text.endswith("次") or text.endswith("颗"):
            text = text[:-1]
        try:
            value = int(text)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    async def craft_pill(
        self,
        user_id: str,
        pill_id: int,
        fire_choice: Optional[str] = None,
        count=None,
        materials=None,
    ) -> Tuple[bool, str, Optional[Dict]]:
        """炼制丹药：首次投料进入火候选择，选好后再次 /炼丹 开炉。

        count 可选：正整数表示连续炼制次数，"all" 表示消耗当前全部材料。
        """
        explicit_fire = None
        if fire_choice is not None:
            mapping = {"猛火": "1", "猛火急攻": "1", "文火": "2", "文火慢煨": "2", "守中": "3", "控火守中": "3"}
            explicit_fire = mapping.get(str(fire_choice).strip(), str(fire_choice).strip())

        batch_count = self._normalize_batch_count(count)
        fire_pill_id, state_fire, state_count, state_materials = await self.pop_fire_state(user_id, include_materials=True)
        if materials is None and fire_pill_id == int(pill_id):
            materials = state_materials
        recipe = self.recipes.get(int(pill_id))
        if recipe:
            try:
                recipe = select_recipe_materials(recipe, materials, self._material_info)
            except ValueError as exc:
                return False, f"❌ {exc}", None
        if explicit_fire is not None:
            fire_choice = explicit_fire if explicit_fire in self.FIRE_OPTIONS else "3"
            fire_matched = True
        elif fire_pill_id is not None and int(fire_pill_id) == int(pill_id):
            fire_choice = state_fire if state_fire in self.FIRE_OPTIONS else "3"
            fire_matched = True
            if batch_count is None:
                batch_count = self._normalize_batch_count(state_count)
        else:
            fire_choice = "3"
            fire_matched = False

        if not fire_matched:
            player = await self.db.get_player_by_id(user_id)
            if not player:
                return False, "❌ 你还未踏入修仙之路！", None
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
            projected = None
            if batch_count == "all" or (isinstance(batch_count, int) and batch_count > 1):
                projected = self.max_craftable_count(player, recipe)
            return await self.start_fire_control(
                user_id,
                int(pill_id),
                recipe["name"],
                requirements,
                recipe.get("output_pill", ""),
                recipe.get("success_rate", 0),
                self.tribulation_hint(player, recipe.get("output_pill", ""), recipe),
                batch_count=batch_count,
                projected=projected,
                materials=materials,
            ) + (
                {"success": False, "pending_fire": True, "batch_count": batch_count},
            )

        if batch_count not in (None, 1):
            return await self.craft_pill_batch(user_id, int(pill_id), fire_choice, batch_count, materials)

        ok, msg, result_data = await self._craft_once(user_id, int(pill_id), fire_choice, materials)
        if result_data is not None:
            result_data = dict(result_data)
            success_count = 1 if result_data.get("success") else 0
            tribulation_count = 1 if result_data.get("tribulation") else 0
            result_data.update({
                "batch": False,
                "crafted": 1,
                "success_count": success_count,
                "perfect_count": 1 if result_data.get("perfect") else 0,
                "tribulation_count": tribulation_count,
                "failure_count": max(0, 1 - success_count - tribulation_count),
            })
        return ok, msg, result_data

    async def _craft_once(
        self, user_id: str, pill_id: int, fire_choice: str, materials=None
    ) -> Tuple[bool, str, Optional[Dict]]:
        """单次开炉：投料、判定成功率/品质/丹劫并写入丹药背包。"""
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

            try:
                recipe = select_recipe_materials(self.recipes[pill_id], materials, self._material_info)
            except ValueError as exc:
                await self.db.conn.rollback()
                return False, f"❌ {exc}", None
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
            # 仙缘【丹火纯青】：炼丹成功率永久加点（加算进最终成功率，仍受 95% 硬顶）
            fate_success = 0
            fate_double = 0.0
            try:
                from ..core.reincarnation_rules import permanent_effect

                fate_success = permanent_effect(player, "alchemy_success_bonus") * 100
                fate_double = permanent_effect(player, "alchemy_double_bonus")
            except Exception:
                fate_success = 0
                fate_double = 0.0
            final_success_rate = min(
                95,
                max(5, recipe["success_rate"] + level_bonus + furnace_bonus + sect_bonus
                    + companion_bonus + root_bonus.get("success_rate", 0) + rate_bonus
                    + talisman_success + int(round(fate_success))),
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
                pill_rank = self._pill_rank_of(output_pill, recipe)
                demand = int(TRIBULATION_DEMAND.get(pill_rank, 0) or 0)
                trigger = int(TRIBULATION_RATE.get(pill_rank, 0) or 0)
                # 仙缘【雷劫不侵】：按抗性折减丹劫所需护盾（最低 1，最高减 80%）
                if demand > 0:
                    try:
                        from ..core.reincarnation_rules import permanent_effect

                        resist = min(0.8, permanent_effect(player, "tribulation_resist_bonus"))
                        if resist > 0:
                            demand = max(1, int(round(demand * (1.0 - resist))))
                    except Exception:
                        pass
                # 丹劫挂在丹药品阶上，与丹方等级解耦：一张黄品引雷符救不了圣品丹。
                if demand > 0 and trigger > 0 and random.randint(1, 100) <= trigger:
                    used, shield, state_note = self._resolve_tribulation(player, demand)
                    if used:
                        tribulation_msg = (
                            f"\n⚡ 天降丹劫！你祭出【{used}】（护盾 {shield}），"
                            f"{state_note}，雷光淬丹，丹药品质得以保全！"
                        )
                    else:
                        pill_ruined = True
                        best = 0
                        try:
                            from .talisman_manager import TalismanManager
                            best = TalismanManager.best_shield_available(player, self.config_manager)
                        except Exception:
                            best = 0
                        tribulation_msg = (
                            f"\n💥 天降丹劫！本丹劫需护盾 ≥ {demand}，"
                            f"你最高只有 {best}，雷火焚炉，丹药在最后一刻化为灰烬..."
                        )

                extra_pills = 1
                double_hit = False
                if not pill_ruined:
                    pill_key = f"{output_pill}|{quality}"
                    inventory = player.get_pills_inventory()
                    # 仙缘【一炉双丹】：按概率额外多出一颗
                    if fate_double > 0 and random.random() < min(1.0, fate_double):
                        extra_pills = 2
                        double_hit = True
                    inventory[pill_key] = inventory.get(pill_key, 0) + extra_pills
                    player.set_pills_inventory(inventory)

                await self.db.update_player(player, commit=False)

                if pill_ruined:
                    # 精准点名能挡此劫的护劫符：凡/黄成品符阁直购，玄品以上走常驻符图
                    shield_hint = f"💡 此丹为{pill_rank}，丹劫需护盾 ≥ {demand}"
                    options = []
                    try:
                        from .talisman_manager import TalismanManager
                        options = TalismanManager.shield_talisman_options(demand, self.config_manager)
                    except Exception:
                        options = []
                    if options:
                        tal_name, tal_shield, tal_rank = options[0]
                        if tal_rank in ("凡品", "黄品"):
                            shield_hint += (
                                f"\n💡 可挡此劫的护劫符：【{tal_name}】(护盾 {tal_shield})，"
                                f"成品可在 /符阁 符箓 购买，或自行 /制符 {tal_name} 绘制"
                            )
                        else:
                            shield_hint += (
                                f"\n💡 可挡此劫的护劫符：【{tal_name}】({tal_rank}·护盾 {tal_shield})，"
                                f"符图常驻不限量：/购买 {tal_name}·符图，习得后即可 /制符 {tal_name} 绘制"
                            )
                    else:
                        shield_hint += "；护劫符可在 /符阁 购买，或自行 /制符 绘制"
                    if options:
                        shield_hint += (
                            f"\n💡 开炉前先 /使用 {options[0][0]} 挂上避劫状态："
                            f"状态期内同档丹劫全免，不必每颗丹现烧一张符"
                        )
                    msg = f"""
💔 丹劫毁丹
━━━━━━━━━━━━━━━

炼制【{output_pill}】本已成功，却引来天雷降劫...

消耗：{cost_str}
成功率：{final_success_rate}%
品质：{quality}
{tribulation_msg}

{shield_hint}
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
                double_note = "\n✨ 【一炉双丹】丹成之时炉火分焰，多出一颗！" if double_hit else ""
                msg = f"""
🎉 炼丹成功！
━━━━━━━━━━━━━━━

你成功炼制了【{output_pill}·{quality}】！{perfect_note}{double_note}
丹药已存入丹药背包（×{extra_pills}）

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
                    "double": double_hit,
                    "pill_count": extra_pills,
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

    async def craft_pill_batch(
        self, user_id: str, pill_id: int, fire_choice: str, requested, materials=None
    ) -> Tuple[bool, str, Optional[Dict]]:
        """连续炼丹：requested 为正整数或 "all"（消耗当前全部材料）。"""
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
        if not self._get_pill_config_by_name(recipe["output_pill"]):
            return False, f"❌ 丹方【{recipe['name']}】的目标丹药【{recipe['output_pill']}】配置缺失，请联系管理员。", None

        try:
            recipe = select_recipe_materials(recipe, materials, self._material_info)
        except ValueError as exc:
            return False, f"❌ {exc}", None
        projected = self.max_craftable_count(player, recipe)
        if projected <= 0:
            requirements, _ = self.format_recipe_requirements(player, recipe)
            return False, (
                f"❌ 材料或灵石不足，一次也炼不了【{recipe['name']}】\n"
                f"━━━━━━━━━━━━━━━\n"
                f"出炉：{recipe.get('output_pill', '')}\n"
                f"所需材料：\n{requirements}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"💡 补齐材料后再发送 /炼丹 {pill_id}"
                f"{self._warehouse_note(player, recipe)}"
            ), None

        notes: List[str] = []
        if requested == "all":
            planned = projected
            if projected >= self.MAX_BATCH_CRAFTS:
                notes.append(
                    f"💡 单次最多连炼 {self.MAX_BATCH_CRAFTS} 次，剩余材料可再发 /炼丹 {pill_id} all"
                )
        else:
            wanted = int(requested)
            planned = min(wanted, projected)
            if planned < wanted:
                if projected >= self.MAX_BATCH_CRAFTS:
                    notes.append(
                        f"💡 单次最多连炼 {self.MAX_BATCH_CRAFTS} 次，剩余材料可再发 /炼丹 {pill_id} all"
                    )
                else:
                    notes.append(f"💡 当前材料/灵石只够 {projected} 次，已按 {planned} 次炼制")

        results: List[Dict] = []
        stop_msg = ""
        for _ in range(planned):
            ok, msg, data = await self._craft_once(user_id, int(pill_id), fire_choice, materials)
            if not ok:
                stop_msg = msg
                break
            results.append(data or {})

        if not results:
            return False, stop_msg or "❌ 炼丹失败。", None

        success_count = sum(1 for item in results if item.get("success"))
        tribulation_count = sum(1 for item in results if item.get("tribulation"))
        normal_fail = len(results) - success_count - tribulation_count
        perfect_count = sum(1 for item in results if item.get("perfect"))

        quality_counter = Counter(
            item.get("quality") for item in results
            if item.get("success") and item.get("quality")
        )
        gold_total = sum(int(item.get("cost", 0) or 0) for item in results)
        material_totals: Dict[str, int] = {}
        for item in results:
            for entry in item.get("materials_consumed") or []:
                name, sep, amount = str(entry).rpartition("×")
                if not sep:
                    continue
                try:
                    material_totals[name] = material_totals.get(name, 0) + int(amount)
                except (TypeError, ValueError):
                    continue

        cost_parts: List[str] = []
        if gold_total > 0:
            cost_parts.append(f"灵石 -{gold_total:,}")
        cost_parts.extend(f"{name}×{amount}" for name, amount in material_totals.items())
        cost_str = "、".join(cost_parts) or "无"

        fire_label = self.FIRE_OPTIONS.get(fire_choice or "3", self.FIRE_OPTIONS["3"])["label"]
        title = "🔥 批量炼丹完成" if success_count else "💔 批量炼丹结束"
        lines = [
            title,
            "━━━━━━━━━━━━━━━",
            f"丹方：【{recipe['name']}】",
            f"火候：{fire_label}",
            f"投料：{len(results)} 次",
        ]
        outcome = []
        if success_count:
            total_pills = sum(int(item.get("pill_count", 1) or 1) for item in results if item.get("success"))
            extra = total_pills - success_count
            outcome.append(f"成丹 {total_pills} 颗" + (f"（一炉双丹 +{extra}）" if extra > 0 else ""))
        if normal_fail:
            outcome.append(f"炼废 {normal_fail} 炉")
        if tribulation_count:
            outcome.append(f"丹劫毁丹 {tribulation_count} 颗")
        if outcome:
            lines.append("结果：" + " ｜ ".join(outcome))
        if quality_counter:
            lines.append(
                "品质：" + "、".join(
                    f"{quality}×{quality_counter[quality]}"
                    for quality in self.QUALITY_ORDER
                    if quality_counter.get(quality)
                )
            )
        lines.append("━━━━━━━━━━━━━━━")
        lines.append(f"消耗：{cost_str}")
        if notes:
            lines.extend(notes)
        if stop_msg:
            lines.append(f"⚠️ {stop_msg.splitlines()[0]}")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append(f"💡 /丹药背包 查看丹药；材料足够时可再发 /炼丹 {pill_id} all")

        return True, "\n".join(lines), {
            "success": success_count > 0,
            "batch": True,
            "pill_name": recipe["output_pill"],
            "quality": None,
            "perfect": perfect_count > 0,
            "cost": gold_total,
            "materials_consumed": [f"{name}×{amount}" for name, amount in material_totals.items()],
            "requested": requested,
            "crafted": len(results),
            "success_count": success_count,
            "perfect_count": perfect_count,
            "tribulation_count": tribulation_count,
            "failure_count": normal_fail,
        }
