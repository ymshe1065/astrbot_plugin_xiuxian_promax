import json
from pathlib import Path
from typing import List, Dict, Any, Optional

from astrbot.api import logger
from .rank_rules import RANK_PRICE, normalize_rank
from .data.default_configs import (
    SECT_CONFIG, BOSS_CONFIG, RIFT_CONFIG, ALCHEMY_CONFIG, AUCTION_HOUSE_CONFIG,
)

class ConfigManager:
    """配置管理器，加载境界、物品、武器和丹药配置"""

    def __init__(self, base_dir: Path):
        self._base_dir = base_dir
        self.level_data: List[dict] = []  
        self.body_level_data: List[dict] = []  
        self.items_data: Dict[str, dict] = {}  
        self.weapons_data: Dict[str, dict] = {}  
        self.pills_data: Dict[str, dict] = {}  
        self.exp_pills_data: Dict[str, dict] = {}  
        self.utility_pills_data: Dict[str, dict] = {}  
        self.storage_rings_data: Dict[str, dict] = {}  
        self.techniques_data: Dict[str, dict] = {} # 新增：功法数据
        
        self.sect_config: Dict[str, Any] = {}
        self.boss_config: Dict[str, Any] = {}
        self.rift_config: Dict[str, Any] = {}
        self.alchemy_config: Dict[str, Any] = {}
        self.pill_quality_data: Dict[str, Any] = {}
        self.pill_recipes_data: Dict[int, dict] = {}
        self.material_catalog: Dict[str, dict] = {}
        self.talisman_recipes_data: Dict[str, dict] = {}
        self.auction_house_config: Dict[str, Any] = {}
        
        self._load_all()

    def get_level_data(self, cultivation_type: str = "灵修") -> List[dict]:
        if cultivation_type == "体修":
            return self.body_level_data
        return self.level_data

    def _load_json_data(self, file_path: Path) -> List[dict]:
        if not file_path.exists():
            return []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载 {file_path} 失败: {e}")
            return []
            
    def _load_config_with_default(self, file_path: Path, default_config: Dict) -> Dict:
        if not file_path.exists():
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(default_config, f, ensure_ascii=False, indent=2)
                return default_config
            except Exception as e:
                return default_config
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            return default_config

    def _load_items_data(self, file_path: Path) -> Dict[str, dict]:
        if not file_path.exists():
            return {}
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {item.get("name", ""): item for item in data if isinstance(item, dict) and item.get("name")}
                elif isinstance(data, dict):
                    items_dict = {}
                    for item_id, item_data in data.items():
                        if isinstance(item_data, dict) and item_data.get("name"):
                            if "id" not in item_data: item_data["id"] = item_id
                            name = item_data["name"]
                            if name in items_dict:
                                logger.warning(
                                    f"物品配置存在同名条目：{name}（{items_dict[name].get('id')} 与 "
                                    f"{item_data.get('id')}），后者将覆盖前者，请检查 config/items.json"
                                )
                            items_dict[name] = item_data
                    return items_dict
                return {}
        except Exception as e:
            logger.error(f"加载物品数据失败: {e}")
            return {}

    def _merge_new_equipment(self):
        """把新系装备合并进 items_data；旧 items.json 保持不变，实现新老分离。"""
        data = getattr(self, "new_equipment_data", {}) or {}
        merged = 0
        for name, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            if name in self.items_data:
                logger.warning(f"新装备与旧 items.json 同名：{name}，已保留旧配置")
                continue
            self.items_data[name] = cfg
            merged += 1
        logger.info(f"新系装备加载完成：{merged} 件")

    def _merge_special_equipment(self):
        """把特殊装备合并进 items_data；同名时以 items.json 为准并告警。"""
        data = getattr(self, "special_equipment_data", {}) or {}
        merged = 0
        for name, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            if name in self.items_data:
                logger.warning(f"特殊装备与 items.json 同名：{name}，已保留 items.json 配置")
                continue
            self.items_data[name] = cfg
            merged += 1
        logger.info(f"特殊手套/戒指装备加载完成：{merged} 件")

    def _load_all(self):
        config_dir = self._base_dir / "config"
        self.level_data = self._load_json_data(config_dir / "level_config.json")
        self.body_level_data = self._load_json_data(config_dir / "body_level_config.json")
        self.items_data = self._load_items_data(config_dir / "items.json")
        # 新系装备与旧 items.json 分离维护；旧装备保持原 50/50 配置。
        self.new_equipment_data = self._load_items_data(config_dir / "items_v2.json")
        self._merge_new_equipment()
        # 特殊手套/戒指（120 件）单独维护，加载后合并进统一物品目录。
        self.special_equipment_data = self._load_items_data(config_dir / "special_equipment.json")
        self._merge_special_equipment()
        self.weapons_data = self._load_items_data(config_dir / "weapons.json")
        self.pills_data = self._load_items_data(config_dir / "pills.json")
        self.exp_pills_data = self._load_items_data(config_dir / "exp_pills.json")
        self.utility_pills_data = self._load_items_data(config_dir / "utility_pills.json")
        self.storage_rings_data = self._load_items_data(config_dir / "storage_rings.json")
        
        # 加载功法
        self.techniques_data = self._load_items_data(config_dir / "techniques.json")
        
        self.sect_config = self._load_config_with_default(config_dir / "sect_config.json", SECT_CONFIG)
        self.boss_config = self._load_config_with_default(config_dir / "boss_config.json", BOSS_CONFIG)
        self.rift_config = self._load_config_with_default(config_dir / "rift_config.json", RIFT_CONFIG)
        self.alchemy_config = self._load_config_with_default(config_dir / "alchemy_config.json", ALCHEMY_CONFIG)
        self.alchemy_recipes = self._load_items_data(config_dir / "alchemy_recipes.json")
        self.game_config = self._load_config_with_default(config_dir / "game_config.json", {})
        self.auction_house_config = self._load_config_with_default(
            config_dir / "auction_house_config.json", AUCTION_HOUSE_CONFIG
        )
        self.pill_quality_data = self._load_config_with_default(
            config_dir / "pill_quality.json",
            {"qualities": {"一般": {"order": 0, "effect_multiplier": 1.0, "breakthrough_multiplier": 1.0, "resurrection_keep": 0.5, "weight": 55}}},
        )
        raw_recipes = self._load_json_data(config_dir / "pill_recipes.json")
        self.pill_recipes_data = {}
        for recipe in raw_recipes:
            if isinstance(recipe, dict) and recipe.get("id") is not None:
                try:
                    self.pill_recipes_data[int(recipe["id"])] = recipe
                except (TypeError, ValueError):
                    continue
        raw_material_catalog = self._load_config_with_default(config_dir / "material_catalog.json", {"materials": []})
        self.material_catalog = {}
        for material in raw_material_catalog.get("materials", []):
            if isinstance(material, dict) and material.get("name"):
                self.material_catalog[material["name"]] = material

        raw_talisman_recipes = self._load_json_data(config_dir / "talisman_recipes.json")
        self.talisman_recipes_data = {}
        for recipe in raw_talisman_recipes:
            if isinstance(recipe, dict) and recipe.get("name"):
                self.talisman_recipes_data[recipe["name"]] = recipe

        self._normalize_ranks()
        self._inject_recipe_scrolls()
        self._inject_material_packages()
        self._inject_talisman_items()
        self._pill_names_cache = None
        self._annotate_elements()
        logger.info("配置管理器初始化完成")

    def _normalize_ranks(self):
        """把配置中的历史品阶名称统一为 rank_rules 的标准名称。

        配置文件可能来自旧版本自定义，加载时统一归一化，避免两套名称
        在运行时重新混用；不写回文件。
        """
        item_sources = (
            self.items_data, self.weapons_data, self.pills_data,
            self.exp_pills_data, self.utility_pills_data,
            self.storage_rings_data, self.techniques_data,
        )
        for data in item_sources:
            for cfg in data.values():
                if isinstance(cfg, dict) and cfg.get("rank"):
                    cfg["rank"] = normalize_rank(cfg["rank"])
        for recipe in self.talisman_recipes_data.values():
            if isinstance(recipe, dict) and recipe.get("rank"):
                recipe["rank"] = normalize_rank(recipe["rank"])
        for recipe in self.pill_recipes_data.values():
            if not isinstance(recipe, dict):
                continue
            for spec in recipe.get("materials") or []:
                if isinstance(spec, dict) and spec.get("tier"):
                    spec["tier"] = normalize_rank(spec["tier"])
        for material in self.material_catalog.values():
            if isinstance(material, dict) and material.get("tier"):
                material["tier"] = normalize_rank(material["tier"])

    # ===== 五行属性标注 =====
    # 名称关键词 → 五行（含变体属性映射：雷属木、风属木、冰属水、光属火、暗属水）
    ELEMENT_KEYWORDS = [
        ("金", "金"), ("庚", "金"), ("锐", "金"), ("锋", "金"),
        ("木", "木"), ("青", "木"), ("雷", "木"), ("风", "木"), ("闪", "木"), ("生生", "木"),
        ("水", "水"), ("冰", "水"), ("寒", "水"), ("暗", "水"), ("幽", "水"), ("壬", "水"),
        ("火", "火"), ("炎", "火"), ("焰", "火"), ("光", "火"), ("阳", "火"), ("丙", "火"),
        ("土", "土"), ("山", "土"), ("岩", "土"), ("厚", "土"), ("戊", "土"),
    ]

    def _infer_element(self, name: str) -> str:
        for keyword, element in self.ELEMENT_KEYWORDS:
            if keyword in name:
                return element
        return ""

    def _annotate_elements(self):
        """加载配置后按名称关键词为武器/防具/功法/心法标注五行属性（仅运行时，不写回文件）。
        配置里显式写了 element 字段的优先保留。"""
        self._element_index = {}
        annotated = 0
        for data in (self.weapons_data, self.items_data, self.techniques_data):
            for name, cfg in data.items():
                if not isinstance(cfg, dict):
                    continue
                element = cfg.get("element") or self._infer_element(name)
                if element:
                    cfg["element"] = element
                    self._element_index[name] = element
                    annotated += 1
        logger.info(f"五行属性标注完成：{annotated} 件物品/功法获得属性")

    def get_item_element(self, item_name: str) -> str:
        """获取物品/功法的五行属性（未标注返回空串）"""
        return self._element_index.get(item_name, "")

    def _inject_recipe_scrolls(self):
        """将未学习丹方作为可交易、可堆叠的“丹方”物品注入物品目录。"""
        for recipe in self.pill_recipes_data.values():
            name = self.get_recipe_scroll_name(recipe)
            if name in self.items_data:
                continue
            self.items_data[name] = {
                "id": f"recipe_scroll_{recipe.get('id')}",
                "name": name,
                "type": "丹方",
                "rank": "灵品",
                "price": int(recipe.get("price") or 2000),
                "shop_weight": int(recipe.get("shop_weight") or 10),
                "volume": 1,
                "recipe_id": int(recipe.get("id")),
                "description": recipe.get("desc") or f"记载{recipe.get('output_pill', '丹药')}炼制方法的丹方，使用 /学习丹方 学习。",
            }

    def _inject_talisman_items(self):
        """将新增符箓注入物品目录（已有符箓沿用原配置）。"""
        for name, recipe in self.talisman_recipes_data.items():
            if name in self.items_data:
                continue
            self.items_data[name] = {
                "id": f"talisman_{recipe.get('id', name)}",
                "name": name,
                "type": "符箓",
                "rank": normalize_rank(recipe.get("rank"), "凡品"),
                "price": int(recipe.get("price") or 1000),
                "shop_weight": int(recipe.get("shop_weight") or 20),
                "volume": 1,
                "description": recipe.get("desc") or "一张蕴含天地之力的符箓。",
            }

    def _inject_material_packages(self):
        """注入材料包：购买后随机开出同类别同品级材料。"""
        tier_price = {"凡品": 90, "黄品": 360, "玄品": 1500, "圣品": 6000}
        categories = ["灵植", "矿材", "妖兽", "灵液"]
        for category in categories:
            for tier, price in tier_price.items():
                name = f"{category}包·{tier}"
                if name in self.items_data:
                    continue
                self.items_data[name] = {
                    "id": f"material_package_{category}_{tier}",
                    "name": name,
                    "type": "材料包",
                    "rank": tier,
                    "price": price,
                    "shop_weight": 600 if tier in ("凡品", "黄品") else 200,
                    "volume": 1,
                    "package_category": category,
                    "package_tier": tier,
                    "description": f"打开后随机获得 1-3 个{tier}{category}材料。",
                }

    @staticmethod
    def get_recipe_scroll_name(recipe: dict) -> str:
        name = str(recipe.get("name") or recipe.get("output_pill") or "丹方")
        return f"《{name}》" if not name.startswith("《") else name

    def get_recipe_by_id(self, recipe_id) -> Optional[Dict[str, Any]]:
        try:
            return self.pill_recipes_data.get(int(recipe_id))
        except (TypeError, ValueError):
            return None

    def get_recipe_by_scroll(self, item_name: str) -> Optional[Dict[str, Any]]:
        item = self.items_data.get(item_name)
        if not item or item.get("type") != "丹方":
            return None
        return self.get_recipe_by_id(item.get("recipe_id"))

    def get_pill_quality(self, quality: str) -> Dict[str, Any]:
        qualities = self.pill_quality_data.get("qualities", {}) if isinstance(self.pill_quality_data, dict) else {}
        return qualities.get(quality or "", {})

    def get_pill_quality_names(self) -> List[str]:
        qualities = self.pill_quality_data.get("qualities", {}) if isinstance(self.pill_quality_data, dict) else {}
        return [name for name, _ in sorted(qualities.items(), key=lambda kv: int(kv[1].get("order", 0)))]

    def get_material_info(self, item_name: str) -> Dict[str, Any]:
        return self.material_catalog.get(item_name, {})

    def is_recipe_scroll(self, item_name: str) -> bool:
        return self.get_recipe_by_scroll(item_name) is not None

    def get_item_config(self, item_name: str) -> Optional[Dict[str, Any]]:
        """统一物品目录查询：同名物品只认一个配置来源。"""
        return (
            self.items_data.get(item_name)
            or self.weapons_data.get(item_name)
            or self.techniques_data.get(item_name)
            or self.pills_data.get(item_name)
            or self.exp_pills_data.get(item_name)
            or self.utility_pills_data.get(item_name)
        )

    def get_item_price(self, item_name: str) -> int:
        """统一价格入口：配置价优先，缺失时按品级回退。"""
        cfg = self.get_item_config(item_name) or {}
        if not cfg:
            cfg = self.get_material_info(item_name)
        try:
            price = int(cfg.get("price", 0) or 0)
        except (TypeError, ValueError):
            price = 0
        if price > 0:
            return price
        rank = normalize_rank(cfg.get("rank"), "凡品")
        return int(RANK_PRICE.get(rank, 1000))

    def is_pill(self, item_name: str) -> bool:
        # 兼容“丹药名|品质”形式的背包 key，避免品质后缀让丹药被识别成普通物品
        base_name = str(item_name or "").split("|", 1)[0]
        if base_name in self.pills_data or base_name in self.exp_pills_data or base_name in self.utility_pills_data:
            return True
        item_config = self.items_data.get(base_name)
        if item_config and item_config.get("type") == "丹药":
            return True
        return False
    
    def get_all_pill_names(self) -> set:
        if self._pill_names_cache is not None:
            return self._pill_names_cache
        pill_names = set()
        pill_names.update(self.pills_data.keys())
        pill_names.update(self.exp_pills_data.keys())
        pill_names.update(self.utility_pills_data.keys())
        for name, item in self.items_data.items():
            if isinstance(item, dict) and item.get("type") == "丹药":
                pill_names.add(name)
        self._pill_names_cache = pill_names
        return pill_names
    
    def invalidate_cache(self):
        self._pill_names_cache = None