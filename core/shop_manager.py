# core/shop_manager.py

import math
import random
import time
import json
from typing import List, Dict, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from ..config_manager import ConfigManager
from ..models import Item
from ..rank_rules import RANK_NAMES, level_to_rank, normalize_rank
from .item_rules import ItemRules
from .realm_rules import format_applicable_realm

class ShopManager:
    """商店管理器，负责商店物品生成、刷新和购买"""

    RANK_BAND_TTL = 300
    # 百宝阁分区排序：装备 > 符箓 > 丹药 > 功法 > 材料
    TYPE_ORDER = {
        "weapon": 0, "armor": 0, "head": 0, "body": 0, "gloves": 0,
        "boots": 0, "ring": 0, "amulet": 0, "accessory": 0,
        "符箓": 1,
        "pill": 2, "exp_pill": 2, "utility_pill": 2,
        "inventory_pill": 2, "legacy_pill": 2,
        "main_technique": 3, "technique": 3,
        "material": 4, "material_package": 4, "recipe": 4,
    }

    def __init__(self, config: AstrBotConfig, config_manager: ConfigManager):
        self.config = config
        self.config_manager = config_manager
        self._rank_band: Optional[Dict] = None
        self._rank_band_at: int = 0

    # ===== 品阶区间 =====

    @staticmethod
    def rank_index(rank: str) -> int:
        try:
            return RANK_NAMES.index(normalize_rank(rank))
        except ValueError:
            return 0

    def resolve_rank_band(self, players, force: bool = False) -> Dict:
        """按全服最低/最高/平均境界计算刷新品阶区间。

        峰值取“平均品阶 × 50% + 区间中点 × 50%”，并向中段集中。
        """
        now = int(time.time())
        if not force and self._rank_band and now - self._rank_band_at < self.RANK_BAND_TTL:
            return self._rank_band

        levels = []
        for player in players or []:
            try:
                levels.append(max(0, int(getattr(player, "level_index", 0) or 0)))
            except (TypeError, ValueError):
                continue
        if not levels:
            # 空服默认凡品~玄品，峰值玄品
            min_idx, max_idx, center = 0, 2, 2.0
            avg_idx = 2
        else:
            min_idx = self.rank_index(level_to_rank(min(levels)))
            max_idx = self.rank_index(level_to_rank(max(levels)))
            avg_idx = self.rank_index(level_to_rank(round(sum(levels) / len(levels))))
            mid_idx = (min_idx + max_idx) / 2.0
            center = avg_idx * 0.5 + mid_idx * 0.5
        if max_idx < min_idx:
            min_idx, max_idx = max_idx, min_idx
        sigma = max(1.0, (max_idx - min_idx) / 3.0)
        weights = {}
        for index in range(min_idx, max_idx + 1):
            weights[index] = max(0.15, math.exp(-((index - center) ** 2) / (2 * sigma ** 2)))
        band = {
            "min": min_idx, "max": max_idx, "center": center,
            "avg": avg_idx, "weights": weights, "created_at": now,
        }
        self._rank_band = band
        self._rank_band_at = now
        return band

    def filter_by_rank_band(self, items: List[Dict]) -> List[Dict]:
        """按品阶区间过滤装备/符箓，并把区间权重乘到商店权重上。"""
        band = self._rank_band
        if not band or not band.get("weights"):
            return items
        result = []
        for item in items or []:
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            is_gear = ItemRules.normalize_equipment_type(data) in ItemRules.GEAR_TYPES
            is_talisman = data.get("type") == "符箓" or item.get("type") == "符箓"
            if not (is_gear or is_talisman):
                result.append(item)
                continue
            index = self.rank_index(item.get("rank") or data.get("rank") or "凡品")
            factor = band["weights"].get(index)
            if factor is None:
                continue
            clone = dict(item)
            try:
                base_weight = int(
                    (data or {}).get("shop_weight") or item.get("weight") or 100
                )
            except (TypeError, ValueError):
                base_weight = 100
            clone["weight"] = max(1, int(round(base_weight * factor)))
            result.append(clone)
        return result

    # ===== 百宝阁排序与分区 =====

    def _type_order(self, item: Dict) -> int:
        return self.TYPE_ORDER.get(str(item.get("type") or ""), 5)

    def sort_pavilion_items(self, items: List[Dict]) -> List[Dict]:
        """按 品阶 -> 类型 -> 价格 -> 名称 排序，刷新时落库。"""
        return sorted(
            items or [],
            key=lambda item: (
                self.rank_index(item.get("rank") or "凡品"),
                self._type_order(item),
                int(item.get("original_price") or item.get("price") or 0),
                str(item.get("name") or ""),
            ),
        )

    def group_items_by_rank(self, items: List[Dict]) -> List[Tuple[str, List[Dict]]]:
        """按品阶顺序分组，跳过空区。"""
        groups: Dict[str, List[Dict]] = {}
        for item in items or []:
            rank = normalize_rank(item.get("rank"), "凡品")
            groups.setdefault(rank, []).append(item)
        return [
            (rank, groups[rank])
            for rank in RANK_NAMES
            if groups.get(rank)
        ]

    def _cfg(self, key: str, default):
        """读取配置：优先新 schema 的 VALUES 层，兼容旧根键。"""
        values = self.config.get("VALUES", {}) or {}
        return values.get(key, self.config.get(key, default))

    def is_gear_entry(self, item: Dict) -> bool:
        """判断商店/交易条目是否为随机品质装备。"""
        if not isinstance(item, dict):
            return False
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        return ItemRules.normalize_equipment_type(data) in ItemRules.GEAR_TYPES

    def roll_gear_quality(self) -> str:
        from .item_instance_manager import ItemInstanceManager
        return ItemInstanceManager.roll_quality()

    def ensure_equipment_quality(self, items: List[Dict]) -> bool:
        """为旧货架补齐装备品质；品质一旦落库，展示与购买保持一致。"""
        changed = False
        for item in items or []:
            if not self.is_gear_entry(item) or item.get("quality"):
                continue
            item["quality"] = self.roll_gear_quality()
            changed = True
        return changed

    def resolve_current_item_data(self, item: Dict) -> Optional[Dict]:
        """按缓存物品的 id/名称查找当前配置，避免继续使用旧效果快照。"""
        if not isinstance(item, dict):
            return None
        data = item.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        target_id = str(data.get("id") or item.get("id") or "")
        target_name = str(item.get("name") or data.get("name") or "")
        sources = []
        for attr in (
            "items_data", "weapons_data", "pills_data",
            "exp_pills_data", "utility_pills_data",
        ):
            source = getattr(self.config_manager, attr, None)
            if isinstance(source, dict):
                sources.extend(source.values())
        # 先全量匹配 id，再退回名称：避免先遍历到的同名旧条目抢在正确 id 之前。
        for current in sources:
            if not isinstance(current, dict):
                continue
            current_id = str(current.get("id") or "")
            if target_id and current_id and current_id == target_id:
                return dict(current)
        for current in sources:
            if not isinstance(current, dict):
                continue
            current_name = str(current.get("name") or "")
            if target_name and current_name == target_name:
                return dict(current)
        return None

    def refresh_item_data(self, items: List[Dict]) -> bool:
        """用当前配置回填商店缓存；同步基础价与折扣价，保留库存和折扣。"""
        changed = False
        for item in items or []:
            if not isinstance(item, dict):
                continue
            current = self.resolve_current_item_data(item)
            if not current:
                continue
            old = item.get("data")
            if old != current:
                item["data"] = current
                changed = True
            if current.get("name") and item.get("name") != current["name"]:
                item["name"] = current["name"]
                changed = True
            if current.get("rank") and item.get("rank") != current["rank"]:
                item["rank"] = current["rank"]
                changed = True
            try:
                base_price = int(current.get("price", 0) or 0)
            except (TypeError, ValueError):
                base_price = 0
            if base_price <= 0:
                continue
            try:
                discount = float(item.get("discount", 1.0) or 1.0)
            except (TypeError, ValueError):
                discount = 1.0
            if item.get("original_price") != base_price:
                item["original_price"] = base_price
                changed = True
            display_price = int(base_price * discount)
            if item.get("price") != display_price:
                item["price"] = display_price
                changed = True
        return changed

    def _format_required_level(self, level_index: int) -> str:
        """同时展示灵修/体修的需求境界名称"""
        names = []
        if 0 <= level_index < len(self.config_manager.level_data):
            name = self.config_manager.level_data[level_index].get("level_name", "")
            if name:
                names.append(name)
        if 0 <= level_index < len(self.config_manager.body_level_data):
            name = self.config_manager.body_level_data[level_index].get("level_name", "")
            if name and name not in names:
                names.append(name)
        if not names:
            return "未知境界"
        return " / ".join(names)

    def _format_applicable_realm(self, item_data: Optional[Dict], required: Optional[int] = None) -> str:
        """按品阶区间展示「适用境界」，例如 金丹期初期-金丹期后期。"""
        data = item_data if isinstance(item_data, dict) else {}
        if required is None:
            required = ItemRules.get_required_level(data)
        rank = str(data.get("rank", "") or "")
        return format_applicable_realm(
            required,
            getattr(self.config_manager, "level_data", []),
            getattr(self.config_manager, "body_level_data", []),
            rank=rank,
        )

    def _get_all_shop_items(self) -> List[Dict]:
        """获取所有可以在商店出售的物品"""
        all_items = []

        # 添加武器
        for weapon in self.config_manager.weapons_data.values():
            if weapon.get('shop_weight', 0) > 0 and weapon.get('price', 0) > 0:
                all_items.append({
                    'id': weapon['id'],
                    'name': weapon['name'],
                    'type': 'weapon',
                    'price': weapon['price'],
                    'weight': weapon['shop_weight'],
                    'rank': weapon.get('rank', '凡品'),
                    'data': weapon
                })

        # 添加物品（防具、心法、功法）
        for item in self.config_manager.items_data.values():
            if item.get('shop_weight', 0) > 0 and item.get('price', 0) > 0:
                all_items.append({
                    'id': item.get('id', item['name']),
                    'name': item['name'],
                    'type': item['type'],
                    'price': item['price'],
                    'weight': item['shop_weight'],
                    'rank': item.get('rank', '凡品'),
                    'data': item
                })

        # 独立功法池（主修心法 + 普通生活功法）
        for item in (getattr(self.config_manager, "techniques_data", {}) or {}).values():
            if item.get('shop_weight', 0) > 0 and item.get('price', 0) > 0:
                all_items.append({
                    'id': item.get('id', item['name']),
                    'name': item['name'],
                    'type': ItemRules.normalize_shop_type(item),
                    'price': item['price'],
                    'weight': item['shop_weight'],
                    'rank': item.get('rank', '凡品'),
                    'data': item
                })

        # 添加破境丹
        for pill in self.config_manager.pills_data.values():
            if pill.get('shop_weight', 0) > 0 and pill.get('price', 0) > 0:
                all_items.append({
                    'id': pill['id'],
                    'name': pill['name'],
                    'type': 'pill',
                    'price': pill['price'],
                    'weight': pill['shop_weight'],
                    'rank': pill.get('rank', '凡品'),
                    'data': pill
                })

        # 添加修为丹
        for pill in self.config_manager.exp_pills_data.values():
            if pill.get('shop_weight', 0) > 0 and pill.get('price', 0) > 0:
                all_items.append({
                    'id': pill['id'],
                    'name': pill['name'],
                    'type': 'exp_pill',
                    'price': pill['price'],
                    'weight': pill['shop_weight'],
                    'rank': pill.get('rank', '凡品'),
                    'data': pill
                })

        # 添加功能丹
        for pill in self.config_manager.utility_pills_data.values():
            if pill.get('shop_weight', 0) > 0 and pill.get('price', 0) > 0:
                all_items.append({
                    'id': pill['id'],
                    'name': pill['name'],
                    'type': 'utility_pill',
                    'price': pill['price'],
                    'weight': pill['shop_weight'],
                    'rank': pill.get('rank', '凡品'),
                    'data': pill
                })

        return all_items

    def _weighted_random_choice(self, items: List[Dict], count: int) -> List[Dict]:
        """基于权重的随机选择（不重复）"""
        if len(items) <= count:
            return items.copy()

        selected = []
        available_items = items.copy()

        for _ in range(count):
            if not available_items:
                break

            # 计算总权重
            total_weight = sum(item['weight'] for item in available_items)
            if total_weight == 0:
                # 如果所有权重都是0，则随机选择
                choice = random.choice(available_items)
            else:
                # 基于权重选择
                rand = random.uniform(0, total_weight)
                cumulative = 0
                choice = available_items[0]
                for item in available_items:
                    cumulative += item['weight']
                    if rand <= cumulative:
                        choice = item
                        break

            selected.append(choice)
            available_items.remove(choice)

        return selected

    def _calculate_stock(self, weight: int, item_data: Optional[dict] = None) -> int:
        """根据权重计算库存数量

        权重越高，物品越常见，库存越多
        权重越低，物品越稀有，库存越少（最少为1）
        物品配置 stock_cap 可限制单次刷新库存，适合高频出现的消耗品。

        Args:
            weight: 物品的商店权重
            item_data: 物品原始配置，可包含 stock_cap

        Returns:
            库存数量（最小为1）
        """
        # 获取库存计算基数，默认100
        stock_divisor = self._cfg("SHOP_STOCK_DIVISOR", 100)

        # 装备类每次刷新只上架 1 件，避免同名实例过多
        if isinstance(item_data, dict) and ItemRules.normalize_equipment_type(item_data) in ItemRules.GEAR_TYPES:
            return 1

        # 库存 = 权重 / 基数，向上取整，最小为1
        stock = max(1, (weight + stock_divisor - 1) // stock_divisor)
        if isinstance(item_data, dict):
            try:
                cap = int(item_data.get("stock_cap") or 0)
            except (TypeError, ValueError):
                cap = 0
            if cap > 0:
                stock = min(stock, cap)

        return stock

    def ensure_items_have_stock(self, shop_items: List[Dict]) -> bool:
        """确保已有商店物品列表包含库存字段（用于兼容旧数据）

        Args:
            shop_items: 商店物品列表

        Returns:
            是否发生了修改
        """
        updated = False
        for item in shop_items:
            stock = item.get('stock')
            if stock is None:
                data = item.get('data', {})
                weight = 0
                if isinstance(data, dict):
                    weight = data.get('shop_weight') or data.get('weight') or 0
                item['stock'] = self._calculate_stock(weight, data)
                updated = True
            elif stock < 0:
                item['stock'] = 0
                updated = True
            elif self.is_gear_entry(item) and stock > 1:
                # 旧货架装备库存收敛为 1；已售罄保持 0
                item['stock'] = 1
                updated = True
        return updated

    def generate_shop_items(self, count: int) -> List[Dict]:
        """生成商店物品列表

        Args:
            count: 要生成的物品数量

        Returns:
            商店物品列表，每个物品包含 id, name, type, price, discount, final_price, stock
        """
        all_items = self._get_all_shop_items()
        if not all_items:
            logger.warning("没有可用的商店物品")
            return []

        # 随机选择物品
        selected_items = self._weighted_random_choice(all_items, count)

        # 获取折扣配置
        discount_min = self._cfg("SHOP_DISCOUNT_MIN", 0.8)
        discount_max = self._cfg("SHOP_DISCOUNT_MAX", 1.2)

        # 生成商店物品
        shop_items = []
        for item in selected_items:
            # 随机折扣
            discount = random.uniform(discount_min, discount_max)
            final_price = int(item['price'] * discount)

            # 计算库存（基于权重）
            stock = self._calculate_stock(item['weight'], item.get('data'))

            shop_items.append({
                'id': item['id'],
                'name': item['name'],
                'type': item['type'],
                'rank': item['rank'],
                'original_price': item['price'],
                'discount': discount,
                'price': final_price,
                'stock': stock,
                'data': item['data']
            })

        return shop_items

    def should_refresh_shop(self, last_refresh_time: int, refresh_hours: int = None) -> bool:
        """检查是否需要刷新"""
        if refresh_hours is None:
            refresh_hours = self._cfg("SHOP_REFRESH_HOURS", 6)
        if refresh_hours <= 0:
            return False
        return (int(time.time()) - last_refresh_time) >= (refresh_hours * 3600)

    def generate_pavilion_items(self, item_getter, count: int) -> List[Dict]:
        """生成阁楼物品列表（带库存和折扣）

        item_getter 返回的条目若带 always_available=True，则视为常驻商品：
        每次刷新都会全部上架，count 仅控制额外随机商品的数量。
        """
        base_items = item_getter(count * 2)  # 获取更多以便随机选择
        normalized = [
            {'weight': i.get('data', {}).get('shop_weight', 100), **i}
            for i in base_items
        ]
        permanent = [i for i in normalized if i.get('always_available')]
        rotatable = [i for i in normalized if not i.get('always_available')]
        if permanent:
            selected = list(permanent)
            if rotatable and count > 0:
                selected.extend(self._weighted_random_choice(rotatable, min(count, len(rotatable))))
        else:
            selected = self._weighted_random_choice(normalized, count)
        discount_min = self._cfg("SHOP_DISCOUNT_MIN", 0.8)
        discount_max = self._cfg("SHOP_DISCOUNT_MAX", 1.2)
        result = []
        for item in selected:
            discount = random.uniform(discount_min, discount_max)
            stock = self._calculate_stock(item.get('weight', 100), item.get('data'))
            result.append({
                'name': item['name'], 'type': item['type'], 'rank': item['rank'],
                'original_price': item['price'], 'discount': discount,
                'price': int(item['price'] * discount), 'stock': stock, 'data': item.get('data', {}),
                'always_available': bool(item.get('always_available')), 'rare': bool(item.get('rare')),
                'quality': self.roll_gear_quality() if self.is_gear_entry(item) else '',
            })
        return self.sort_pavilion_items(result)

    def get_pills_for_display(self, count: int) -> List[Dict]:
        """获取丹药列表用于丹阁展示"""
        all_pills = []
        for pill in self.config_manager.pills_data.values():
            if pill.get('price', 0) > 0:
                all_pills.append({'name': pill['name'], 'type': 'pill', 'price': pill['price'], 'rank': pill.get('rank', '凡品'), 'data': pill})
        for pill in self.config_manager.exp_pills_data.values():
            if pill.get('price', 0) > 0:
                all_pills.append({'name': pill['name'], 'type': 'exp_pill', 'price': pill['price'], 'rank': pill.get('rank', '凡品'), 'data': pill})
        for pill in self.config_manager.utility_pills_data.values():
            if pill.get('price', 0) > 0:
                all_pills.append({'name': pill['name'], 'type': 'utility_pill', 'price': pill['price'], 'rank': pill.get('rank', '凡品'), 'data': pill})
        return all_pills

    def _is_breakthrough_recipe(self, item: Dict) -> bool:
        """判断丹方卷轴对应的丹药是否为突破所需（常驻丹方）"""
        recipe = None
        recipe_id = item.get('recipe_id')
        if recipe_id is not None:
            recipe = self.config_manager.get_recipe_by_id(recipe_id)
        if not recipe:
            recipe = self.config_manager.get_recipe_by_scroll(item.get('name', ''))
        if not recipe:
            return False
        output_pill = recipe.get('output_pill') or recipe.get('output')
        pill = self.config_manager.pills_data.get(output_pill) if output_pill else None
        if not isinstance(pill, dict):
            return False
        return pill.get('subtype') == 'breakthrough' or pill.get('type') == 'breakthrough'

    def get_recipes_for_display(self, count: int) -> List[Dict]:
        """获取丹方卷轴用于丹方阁展示

        突破丹方常驻提供（always_available=True），其余稀有丹方参与随机刷新。
        """
        recipes = []
        for item in self.config_manager.items_data.values():
            if item.get('type') == '丹方' and item.get('price', 0) > 0:
                permanent = self._is_breakthrough_recipe(item)
                recipes.append({
                    'name': item['name'], 'type': 'recipe', 'price': item['price'],
                    'rank': item.get('rank', '灵品'), 'data': item,
                    'always_available': permanent, 'rare': not permanent,
                })
        return recipes

    def get_materials_for_display(self, count: int) -> List[Dict]:
        """获取材料用于材料阁展示；价格缺失时按品级补齐。"""
        tier_price = {'凡品': 10, '黄品': 30, '玄品': 120, '灵品': 200, '地品': 500, '天品': 900, '皇品': 1500, '圣品': 2000, '帝品': 3000, '道品': 4000, '仙品': 5000, '混元先天': 8000}
        materials = []
        for item in self.config_manager.items_data.values():
            raw_type = item.get('type')
            if raw_type not in ('材料', '材料包'):
                continue
            rank = normalize_rank(item.get('rank'), '凡品')
            price = int(item.get('price') or tier_price.get(rank, 50))
            item_type = 'material_package' if raw_type == '材料包' else 'material'
            materials.append({
                'name': item['name'], 'type': item_type, 'price': price,
                'rank': rank, 'data': {**item, 'price': price},
            })
        return materials

    # ===== 材料包 =====
    PACKAGE_OPEN_MIN = 1
    PACKAGE_OPEN_MAX = 3

    def get_package_material_pool(self, category: str, tier: str) -> List[Tuple[str, int]]:
        """返回材料包可开出的 (材料名, 权重)；只取物品目录中真实存在的材料。

        权重沿用材料的 shop_weight；若同类别同品级全部为 0（如仅由杂交产出的灵草），
        则统一按 1 参与随机，避免材料包开出空奖。
        """
        catalog = getattr(self.config_manager, "material_catalog", {}) or {}
        items = getattr(self.config_manager, "items_data", {}) or {}
        pool: List[Tuple[str, int]] = []
        for name, material in catalog.items():
            if not isinstance(material, dict):
                continue
            if material.get("category") != category or material.get("tier") != tier:
                continue
            item_data = items.get(name)
            if not isinstance(item_data, dict) or item_data.get("type") != "材料":
                continue
            try:
                weight = int(item_data.get("shop_weight") or 0)
            except (TypeError, ValueError):
                weight = 0
            if weight > 0:
                pool.append((name, weight))
        if not pool:
            for name, material in catalog.items():
                if not isinstance(material, dict):
                    continue
                if material.get("category") != category or material.get("tier") != tier:
                    continue
                item_data = items.get(name)
                if isinstance(item_data, dict) and item_data.get("type") == "材料":
                    pool.append((name, 1))
        return pool

    def open_material_package(self, item: Dict) -> List[Tuple[str, int]]:
        """开启一个材料包，返回按数量降序的 [(材料名, 数量)]；无可用材料时返回空列表。"""
        data = item.get("data") or {}
        category = str(data.get("package_category") or "")
        tier = str(data.get("package_tier") or item.get("rank") or "")
        pool = self.get_package_material_pool(category, tier)
        if not pool:
            return []
        total_weight = sum(weight for _, weight in pool)
        if total_weight <= 0:
            return []
        gained: Dict[str, int] = {}
        for _ in range(random.randint(self.PACKAGE_OPEN_MIN, self.PACKAGE_OPEN_MAX)):
            roll = random.uniform(0, total_weight)
            cumulative = 0
            picked = pool[-1][0]
            for name, weight in pool:
                cumulative += weight
                if roll <= cumulative:
                    picked = name
                    break
            gained[picked] = gained.get(picked, 0) + 1
        return sorted(gained.items(), key=lambda kv: (-kv[1], kv[0]))

    def get_weapons_for_display(self, count: int) -> List[Dict]:
        """获取武器列表用于器阁展示"""
        all_weapons = []
        for weapon in self.config_manager.weapons_data.values():
            if weapon.get('price', 0) > 0:
                all_weapons.append({'name': weapon['name'], 'type': 'weapon', 'price': weapon['price'], 'rank': weapon.get('rank', '凡品'), 'data': weapon})
        for item in self.config_manager.items_data.values():
            if item.get("price", 0) > 0 and ItemRules.normalize_equipment_type(item) in ItemRules.GEAR_TYPES:
                all_weapons.append({"name": item["name"], "type": ItemRules.normalize_shop_type(item),
                                    "price": item["price"], "rank": item.get("rank", "凡品"), "data": item})
        return self.filter_by_rank_band(all_weapons)

    def get_all_items_for_display(self, count: int) -> List[Dict]:
        """获取所有物品用于百宝阁展示"""
        all_items = []
        for weapon in self.config_manager.weapons_data.values():
            if weapon.get('price', 0) > 0:
                all_items.append({'name': weapon['name'], 'type': 'weapon', 'price': weapon['price'], 'rank': weapon.get('rank', '凡品'), 'data': weapon})
        for item in self.config_manager.items_data.values():
            if item.get('price', 0) > 0:
                # 丹方/材料包由丹阁分区单独出售，不混入百宝阁
                if item.get('type') in ('丹方', '材料包'):
                    continue
                # 符箓保留独立类型，避免在商店/物品信息中显示成材料
                item_type = "符箓" if item.get('type') == '符箓' else self._map_legacy_item_type(item)
                all_items.append({'name': item['name'], 'type': item_type, 'price': item['price'], 'rank': item.get('rank', '凡品'), 'data': item})
        for item in (getattr(self.config_manager, "techniques_data", {}) or {}).values():
            if item.get('price', 0) > 0:
                all_items.append({
                    'name': item['name'],
                    'type': ItemRules.normalize_shop_type(item),
                    'price': item['price'],
                    'rank': item.get('rank', '凡品'),
                    'data': item,
                })
        for pill in self.config_manager.pills_data.values():
            if pill.get('price', 0) > 0:
                all_items.append({'name': pill['name'], 'type': 'pill', 'price': pill['price'], 'rank': pill.get('rank', '凡品'), 'data': pill})
        for pill in self.config_manager.exp_pills_data.values():
            if pill.get('price', 0) > 0:
                all_items.append({'name': pill['name'], 'type': 'exp_pill', 'price': pill['price'], 'rank': pill.get('rank', '凡品'), 'data': pill})
        for pill in self.config_manager.utility_pills_data.values():
            if pill.get('price', 0) > 0:
                all_items.append({'name': pill['name'], 'type': 'utility_pill', 'price': pill['price'], 'rank': pill.get('rank', '凡品'), 'data': pill})
        return self.filter_by_rank_band(all_items)

    def _map_legacy_item_type(self, item: dict) -> str:
        """将旧格式物品类型映射到新格式

        Args:
            item: 物品配置字典

        Returns:
            映射后的类型字符串
        """
        return ItemRules.normalize_shop_type(item)

    def find_item_by_name(self, name: str) -> Optional[Dict]:
        """根据名称查找物品；覆盖商店、功法、丹药、材料、储物戒与符箓配方。"""
        def _wrap(data: dict, item_type: str, price=None, rank=None):
            return {
                'name': data.get('name') or name,
                'type': item_type,
                'price': int(data.get('price', 0) or 0) if price is None else price,
                'rank': rank or data.get('rank', '凡品'),
                'data': data,
            }

        for weapon in self.config_manager.weapons_data.values():
            if weapon.get('name') == name:
                return _wrap(weapon, 'weapon')
        for item in self.config_manager.items_data.values():
            if item.get('name') == name:
                if item.get('type') == '符箓':
                    item_type = '符箓'
                elif item.get('type') == '材料':
                    item_type = 'material'
                else:
                    item_type = self._map_legacy_item_type(item)
                return _wrap(item, item_type)
        for item in (getattr(self.config_manager, "techniques_data", {}) or {}).values():
            if item.get('name') == name:
                return _wrap(item, ItemRules.normalize_shop_type(item))
        for pill in self.config_manager.pills_data.values():
            if pill.get('name') == name:
                return _wrap(pill, 'pill')
        for pill in self.config_manager.exp_pills_data.values():
            if pill.get('name') == name:
                return _wrap(pill, 'exp_pill')
        for pill in self.config_manager.utility_pills_data.values():
            if pill.get('name') == name:
                return _wrap(pill, 'utility_pill')
        # 材料目录兜底（少数材料只存在于材料目录）
        material = (getattr(self.config_manager, "material_catalog", {}) or {}).get(name)
        if isinstance(material, dict):
            data = {
                **material,
                'name': name,
                'type': '材料',
                'rank': material.get('tier', material.get('rank', '凡品')),
                'price': material.get('price', 0),
                'description': material.get('description') or f"{material.get('tier', '')}{material.get('category', '')}材料。",
            }
            return _wrap(data, 'material')
        # 储物戒
        ring = (getattr(self.config_manager, "storage_rings_data", {}) or {}).get(name)
        if isinstance(ring, dict):
            return _wrap(ring, 'storage_ring')
        # 符箓配方兜底（部分配方未注入物品目录）
        recipe = (getattr(self.config_manager, "talisman_recipes_data", {}) or {}).get(name)
        if isinstance(recipe, dict):
            data = {
                **recipe,
                'name': name,
                'type': '符箓',
                'rank': normalize_rank(recipe.get('rank'), '凡品'),
                'price': recipe.get('price', 0),
                'description': recipe.get('desc') or f"{recipe.get('category', '')}符箓，可绘制使用。",
            }
            return _wrap(data, '符箓')
        return None

    def format_pavilion_display(self, pavilion_name: str, items: List[Dict], refresh_hours: int = 6, last_refresh: int = 0) -> str:
        """格式化阁楼展示信息"""
        if not items:
            return f"{pavilion_name}暂无物品出售"

        type_label_map = {
            'weapon': '武器', 'armor': '防具', 'head': '头饰', 'body': '甲胄',
            'gloves': '手套', 'boots': '靴子', 'ring': '戒指', 'amulet': '护符',
            'main_technique': '心法', 'technique': '功法',
            'pill': '破境丹', 'exp_pill': '修为丹', 'utility_pill': '功能丹',
            'inventory_pill': '丹药', 'legacy_pill': '丹药', 'recipe': '丹方',
            'material': '材料', 'material_package': '材料包', 'accessory': '饰品', '符箓': '符箓'
        }

        lines = [f"=== {pavilion_name} ===\n"]
        display_index = 0
        groups = self.group_items_by_rank(items) if pavilion_name == "百宝阁" else [("", items)]
        for rank, group in groups:
            if rank:
                lines.append(f"\n【{rank}区】\n")
            for item in group:
                stock = item.get('stock', 0)
                if stock <= 0:
                    continue
                display_index += 1
                type_label = type_label_map.get(item['type'], '物品')
                discount_text = ""
                if item.get('discount', 1.0) < 1.0:
                    discount_text = f" [{round(item['discount'] * 10, 1)}折]"
                elif item.get('discount', 1.0) > 1.0:
                    discount_text = f" [+{int((item['discount'] - 1.0) * 100)}%]"
                stock_text = f"库存紧张:{stock}" if stock <= 3 else f"库存:{stock}"
                permanent_text = " [常驻]" if item.get('always_available') else ""

                # 获取物品效果描述
                effect_desc = '；'.join(self.get_trade_details(item))
                effect_line = f"\n   效果: {effect_desc}" if effect_desc else ""

                lines.append(f"{display_index}. [{item['rank']}] {item['name']} ({type_label}){permanent_text}{discount_text}\n   价格: {item['price']} 灵石 {stock_text}{effect_line}\n")

        if refresh_hours > 0 and last_refresh:
            remaining = (last_refresh + refresh_hours * 3600) - int(time.time())
            if remaining > 0:
                lines.append(f"\n下次刷新: {remaining // 3600}小时{(remaining % 3600) // 60}分钟后")
        lines.append(f"\n提示: 使用 '购买 [物品名]' 购买物品")
        return "".join(lines)

    def _get_item_effect_short(self, item: Dict) -> str:
        """获取物品效果的简短描述"""
        data = self.resolve_current_item_data(item) or item.get('data', {})
        item_type = item.get('type', '')
        effects = []
        
        # 武器/装备属性
        if item_type in ItemRules.GEAR_TYPES:
            if data.get('physical_damage', 0) > 0:
                effects.append(f"物伤+{data['physical_damage']}")
            if data.get('magic_damage', 0) > 0:
                effects.append(f"法伤+{data['magic_damage']}")
            if data.get('physical_defense', 0) > 0:
                effects.append(f"物防+{data['physical_defense']}")
            if data.get('magic_defense', 0) > 0:
                effects.append(f"法防+{data['magic_defense']}")
            if data.get('mental_power', 0) > 0:
                effects.append(f"精神力+{data['mental_power']}")
        
        # 功法属性
        elif item_type in ['main_technique', 'technique', '功法']:
            if data.get('exp_multiplier', 0) > 0:
                effects.append(f"修炼效率+{int(data['exp_multiplier']*100)}%")
            if data.get('physical_damage', 0) > 0:
                effects.append(f"物伤+{data['physical_damage']}")
            if data.get('magic_damage', 0) > 0:
                effects.append(f"法伤+{data['magic_damage']}")
        
        # 丹药效果
        elif item_type in ['pill', 'exp_pill', 'utility_pill', 'inventory_pill', 'legacy_pill']:
            # 尝试从 effect 字段获取
            effect_data = data.get('effect', {})
            if isinstance(effect_data, dict):
                if effect_data.get('add_experience', 0) > 0:
                    effects.append(f"修为+{effect_data['add_experience']}")
                if effect_data.get('add_hp', 0) > 0:
                    effects.append(f"气血+{effect_data['add_hp']}")
                if effect_data.get('add_max_hp', 0) > 0:
                    effects.append(f"气血上限+{effect_data['add_max_hp']}")
                if effect_data.get('add_attack', 0) > 0:
                    effects.append(f"攻击+{effect_data['add_attack']}")
                if effect_data.get('add_defense', 0) > 0:
                    effects.append(f"防御+{effect_data['add_defense']}")
            
            # 破境丹特殊处理
            if data.get('subtype') == 'breakthrough':
                bonus = data.get('breakthrough_bonus', 0)
                if bonus > 0:
                    effects.append(f"突破成功率+{int(bonus*100)}%")
            
            # 修为丹
            if data.get('exp_boost', 0) > 0:
                effects.append(f"修为+{data['exp_boost']}")
        
        # 材料
        elif item_type == 'material':
            if data.get('description'):
                return data['description'][:20]
        elif item_type == 'material_package':
            return data.get('description', '打开后获得随机材料')[:25]
        
        # 如果有描述字段，优先使用
        if not effects and data.get('description'):
            desc = data['description']
            return desc[:25] + "..." if len(desc) > 25 else desc
        
        return ", ".join(effects[:3]) if effects else ""

    def get_trade_details(self, item: Dict, instance: dict = None) -> list:
        """图片与文本共用完整数值说明，不截断属性或名称。"""
        data = item.get("data") or item
        detail_type = item.get("type") or data.get("type")
        if not detail_type:
            detail_type = "exp_pill" if "exp_gain" in data else "utility_pill" if "effect_type" in data else ""
        if detail_type not in ("pill", "exp_pill", "utility_pill"):
            detail_type = "符箓" if data.get("type") == "符箓" else ItemRules.normalize_shop_type(data)
        normalized = {**item, "type": detail_type,
                      "rank": item.get("rank", data.get("rank", "凡品")), "price": item.get("price", 0), "data": data}
        lines = [f"品级: {normalized['rank']}"] + self.get_item_details(normalized).splitlines()[3:]
        is_gear = ItemRules.normalize_equipment_type(data) in ItemRules.GEAR_TYPES
        if is_gear:
            from .equipment_manager import EquipmentManager
            from .equipment_balance import QUALITY
            manager = EquipmentManager(None, self.config_manager)
            quality = str((instance or {}).get("quality") or item.get("quality") or "")
            display_instance = instance if instance else ({"quality": quality} if quality else None)
            parsed = manager.parse_item_from_name(item["name"], {item["name"]: data}, instance=display_instance)
            lines = [line for line in lines if not line.startswith(("属性:", "基础属性:", "品质:", "品质浮动:", "需求境界:", "适用境界:"))]
            attr_text = (parsed.get_attribute_display() or "").strip()
            has_panel_rates = bool(attr_text)
            attr_text = attr_text or "无面板主属性（仅提供特殊效果）"
            if quality:
                mult = QUALITY.get(quality, 1.0)
                lines.append("属性: " + attr_text)
                scale_label = "主属性" if has_panel_rates else "特殊效果"
                lines.append(f"品质: {quality}（{scale_label}×{mult:.2f}）")
            else:
                lines.append("基础属性: " + attr_text)
                if has_panel_rates:
                    lines.append("品质浮动: 一般/良好/极佳/大师（主属性×1.00/1.05/1.10/1.15，按实例品质结算）")
                else:
                    lines.append("品质浮动: 一般/良好/极佳/大师（特殊效果×1.00/1.05/1.10/1.15，按实例品质结算）")
            if has_panel_rates:
                lines.append("同属性加法叠加；每落后3级效率×80%，最低20%")
            slot = ItemRules.get_equip_slot(data)
            label = "戒指（两个槽位）" if slot == "ring1" else ItemRules.SLOT_LABELS.get("armor" if slot == "body" else slot, slot)
            lines.append(f"部位: {label} · 适用境界: {self._format_applicable_realm(data)}")
        labels = {"physical_damage": "物伤", "magic_damage": "法伤", "physical_defense": "物防",
                  "magic_defense": "法防", "mental_power": "精神力", "luck": "气运", "crit_rate": "暴击率", "crit_damage": "暴击伤害", "lifesteal": "吸血", "counter_damage": "反伤",
                  "combo_rate": "连击率", "defense_bonus": "额外防御", "physical_damage_multiplier": "物伤倍率", "magic_damage_multiplier": "法伤倍率"}
        # 装备特效已由 get_item_details 按品质缩放展示，避免重复且显示未缩放的原始键名
        if not is_gear:
            for key, value in data.get("effects", {}).items():
                value_text = (f"{value:+g}%" if key in ("crit_rate", "lifesteal", "counter_damage", "combo_rate", "dodge", "status_resist")
                              else f"{value:+.1%}" if ItemRules.is_rate_key(key) else f"{value:+g}")
                lines.append(f"特效: {labels.get(key, key)} {value_text}")
        try:
            exp_mult = float(data.get("exp_multiplier", 0) or 0)
        except (TypeError, ValueError):
            exp_mult = 0.0
        if exp_mult > 0:
            lines.append(f"生活: 修炼效率 {exp_mult:+.1%}")
        from .technique_bonus import LIFE_EFFECT_LABELS
        for key, value in (data.get("life_effects") or {}).items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number <= 0:
                continue
            value_text = f"{number:+.0f}" if key == "luck_bonus" else f"{number:+.1%}"
            lines.append(f"生活: {LIFE_EFFECT_LABELS.get(key, key)} {value_text}")
        for affix in (instance or {}).get("affixes", []) or []:
            key, value = affix.get("key", ""), affix.get("value", 0)
            value_text = (f"{value:+g}%" if key in ("crit_rate", "lifesteal", "counter_damage", "combo_rate", "dodge", "status_resist")
                          else f"{value:+.1%}" if ItemRules.is_rate_key(key) else f"{value:+g}")
            lines.append(f"词条: {affix.get('name') or labels.get(key, key)} {value_text}")
        return lines

    def get_item_details(self, item_data: Dict) -> str:
        """获取物品详细信息

        Args:
            item_data: 物品数据字典

        Returns:
            物品详细描述
        """
        item_type = item_data.get('type', '')
        data = self.resolve_current_item_data(item_data) or item_data.get('data', {})

        details = [f"名称: {item_data['name']}"]
        details.append(f"品级: {item_data['rank']}")
        try:
            price = int(item_data.get('price', 0) or 0)
        except (TypeError, ValueError):
            price = 0
        details.append(f"价格: {price:,} 灵石" if price > 0 else "价格: —")
        if data.get('element'):
            details.append(f"五行: {data['element']}")

        description = data.get('description')
        if description:
            details.append(f"描述: {description}")

        # 武器/防具/饰品属性
        if item_type in ItemRules.GEAR_TYPES:
            from .equipment_balance import equipment_rates, effective_effects, ATTRIBUTE_LABELS
            from .equipment_manager import EquipmentManager
            attrs = [f"境界基础{ATTRIBUTE_LABELS[k]}+{v:.1%}" for k, v in equipment_rates(data).items() if v]

            if attrs:
                details.append(f"基础属性: {', '.join(attrs)}")
                details.append("品质浮动: 一般/良好/极佳/大师（主属性×1.00/1.05/1.10/1.15，按实例品质结算）")
            quality = item_data.get("quality") or "一般"
            effect_lines = []
            for key, value in effective_effects(data, quality).items():
                label = EquipmentManager.EFFECT_LABELS.get(key, key)
                effect_lines.append(f"{label} {EquipmentManager.format_bonus_value(key, value)}")
            if effect_lines:
                quality_text = f"（{quality}品质）" if quality else ""
                details.append(f"特殊效果{quality_text}: " + "，".join(effect_lines))
            details.append(f"适用境界: {self._format_applicable_realm(data)}")

        elif item_type == '符箓':
            from .talisman_rules import NON_TALISMAN_ITEMS, build_profile
            if item_data['name'] in NON_TALISMAN_ITEMS:
                details.append("用途：使用 /激活化身 可激活 30 天闭关收益加成")
                return "\n".join(details)
            recipe = (getattr(self.config_manager, "talisman_recipes_data", {}) or {}).get(item_data['name'], {})
            profile = build_profile(item_data['name'], normalize_rank(item_data.get('rank'), '凡品'), recipe.get('category', ''))
            details.append(f"类别：{profile['category']}")
            details.append(f"效果：{profile['description']}")
            details.append(f"持续：{profile['duration']}分钟")
            required = recipe.get("level_required")
            if required is None:
                required = data.get("required_level_index")
            if required is not None:
                details.append(f"适用境界: {self._format_applicable_realm(data, int(required))}")

        elif item_type == 'material_package':
            category = data.get('package_category', '材料')
            tier = data.get('package_tier', '')
            details.append(f"开启：随机获得 1-3 个{tier}{category}材料")

        # 心法/功法
        elif item_type in ['main_technique', 'technique']:
            attrs = []
            if data.get('exp_multiplier', 0) > 0:
                attrs.append(f"修炼效率+{data['exp_multiplier']:.1%}")
            if data.get('spiritual_qi', 0) > 0:
                attrs.append(f"灵气+{data['spiritual_qi']}")
            if data.get('magic_damage', 0) > 0:
                attrs.append(f"法伤+{data['magic_damage']}")
            if data.get('physical_damage', 0) > 0:
                attrs.append(f"物伤+{data['physical_damage']}")
            if data.get('magic_defense', 0) > 0:
                attrs.append(f"法防+{data['magic_defense']}")
            if data.get('physical_defense', 0) > 0:
                attrs.append(f"物防+{data['physical_defense']}")
            if data.get('mental_power', 0) > 0:
                attrs.append(f"精神力+{data['mental_power']}")
            from .technique_bonus import LIFE_EFFECT_LABELS
            effect_labels = {
                "crit_rate": "暴击率", "crit_damage": "暴击伤害", "lifesteal": "吸血",
                "counter_damage": "反伤", "combo_rate": "连击率", "dodge": "闪避",
                "defense_bonus": "额外防御", "physical_damage_multiplier": "物伤倍率",
                "magic_damage_multiplier": "法伤倍率",
            }
            for key, value in (data.get("effects") or {}).items():
                label = effect_labels.get(key, key)
                if key in ("crit_rate", "lifesteal", "counter_damage", "combo_rate", "dodge"):
                    attrs.append(f"{label}+{value:g}%")
                elif key.endswith("_multiplier"):
                    attrs.append(f"{label}+{float(value):.1%}")
                else:
                    attrs.append(f"{label}+{value:g}")
            for key, value in (data.get("life_effects") or {}).items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if number <= 0:
                    continue
                attrs.append(
                    f"{LIFE_EFFECT_LABELS.get(key, key)}+{int(number)}"
                    if key == "luck_bonus"
                    else f"{LIFE_EFFECT_LABELS.get(key, key)}+{number:.1%}"
                )
            if attrs:
                details.append(f"效果: {', '.join(attrs)}")
            details.append(f"适用境界: {self._format_applicable_realm(data)}")

        elif item_type == 'storage_ring':
            if data.get('capacity') is not None:
                details.append(f"容量: {int(data.get('capacity', 0) or 0)} 格")
            details.append(f"适用境界: {self._format_applicable_realm(data)}")
            details.append("获取：基础储物戒初始自带；高级储物戒商店/百宝阁购买")

        elif item_type == 'material':
            category = data.get('category') or data.get('subtype')
            tier = data.get('tier') or item_data.get('rank')
            if category or tier:
                details.append(f"类别: {category or '材料'}" + (f" · {tier}" if tier else ""))

        # 丹药类
        elif item_type in ['pill', 'exp_pill', 'utility_pill', 'inventory_pill', 'legacy_pill']:
            if 'required_level_index' in data and data['required_level_index'] > 0:
                details.append(f"适用境界: {self._format_applicable_realm(data)}")

            subtype = data.get('subtype', '')
            effect_desc = []

            if item_type == 'pill' and subtype == 'breakthrough':
                bonus = data.get('breakthrough_bonus', 0)
                max_rate = data.get('max_success_rate', 1.0)
                target = data.get('target_level_index')
                if target is not None:
                    level_name = self._format_required_level(target)
                    effect_desc.append(f"目标境界: {level_name}")
                effect_desc.append(f"突破成功率+{int(bonus * 100)}%，最高可达 {int(max_rate * 100)}%")

            elif item_type == 'exp_pill':
                exp_gain = data.get('exp_gain', 0)
                effect_desc.append(f"立即获得修为：+{exp_gain}")

            elif item_type == 'utility_pill' or data.get('effect_type') in ('temporary', 'permanent'):
                effect_type = data.get('effect_type', '')
                if item_type == 'utility_pill' and subtype == 'resurrection':
                    effect_desc.append("死亡时自动复活（属性减半）")
                elif effect_type == 'temporary':
                    duration = data.get('duration_minutes', 0)
                    mult = data.get('cultivation_multiplier', 0)
                    if mult > 0:
                        effect_desc.append(f"修炼速度{ItemRules.format_bonus_rate(mult, cap=None)} 持续 {duration} 分钟")
                    if data.get('physical_damage_multiplier'):
                        effect_desc.append(f"物伤{ItemRules.format_bonus_rate(data['physical_damage_multiplier'])}")
                    if data.get('magic_damage_multiplier'):
                        effect_desc.append(f"法伤{ItemRules.format_bonus_rate(data['magic_damage_multiplier'])}")
                    if data.get('physical_defense_multiplier'):
                        effect_desc.append(f"物防{ItemRules.format_bonus_rate(data['physical_defense_multiplier'])}")
                    if data.get('magic_defense_multiplier'):
                        effect_desc.append(f"法防{ItemRules.format_bonus_rate(data['magic_defense_multiplier'])}")
                    if data.get('duration_minutes') and not any(
                        data.get(key) for key in (
                            'cultivation_multiplier', 'physical_damage_multiplier',
                            'magic_damage_multiplier', 'physical_defense_multiplier',
                            'magic_defense_multiplier',
                        )
                    ):
                        effect_desc.append(f"持续 {duration} 分钟")
                elif effect_type == 'permanent':
                    gains = []
                    for attr_key, label in [
                        ('physical_damage_gain', '物伤'),
                        ('magic_damage_gain', '法伤'),
                        ('physical_defense_gain', '物防'),
                        ('magic_defense_gain', '法防'),
                        ('mental_power_gain', '精神力')
                    ]:
                        value = data.get(attr_key)
                        if value:
                            sign = "+" if value > 0 else ""
                            gains.append(f"{label}{sign}{value}")
                    if gains:
                        effect_desc.append("永久增益：" + "，".join(gains))
                if data.get('resets_permanent_pills'):
                    refund_ratio = data.get('reset_refund_ratio', 0)
                    hint = "重置所有永久丹药增益"
                    if refund_ratio:
                        hint += f"，返还售价的{int(refund_ratio*100)}%"
                    effect_desc.append(hint)
                if data.get('blocks_next_debuff'):
                    effect_desc.append("获得定魂护盾，抵消下一次负面状态")

            elif item_type in ('legacy_pill', 'inventory_pill') and data.get('effect'):
                effect_data = data.get('effect', {})
                for key, label in [
                    ('add_hp', '恢复气血'),
                    ('add_experience', '增加修为'),
                    ('add_max_hp', '提升上限'),
                    ('add_attack', '物伤变化'),
                    ('add_defense', '物防变化'),
                    ('add_spiritual_power', '法伤变化'),
                    ('add_mental_power', '精神力变化'),
                    ('add_gold', '灵石变化'),
                ]:
                    value = effect_data.get(key)
                    if value:
                        sign = "+" if value > 0 else ""
                        effect_desc.append(f"{label}{sign}{value}")
                if effect_data.get('add_breakthrough_bonus'):
                    effect_desc.append(f"突破成功率+{int(effect_data['add_breakthrough_bonus']*100)}%（1小时）")

            if effect_desc:
                details.append("效果: " + "；".join(effect_desc))

        return "\n".join(details)
