# managers/bounty_manager.py
"""通缉令系统：每日刷新、全境界可选的随机强敌挑战。

玩法概览：
- 每日为每位修士生成一张通缉榜，榜单境界围绕玩家当前境界上下浮动；
- 每个通缉目标都会随机搭配姓名、境界、五行、功法、装备与主动绝技；
- 同一时间只能接取一道通缉令，接取后可查看完整数值；
- 每日最多挑战 3 次，胜负都会消耗次数；
- 胜利后按目标境界发放灵石、修为、材料、装备与功法掉落；
- 可缴纳罚款放弃已接取的通缉令，重新选择目标。
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from astrbot.api import logger

from ..core.equipment_balance import (
    effective_effects,
    equipment_rates,
    realm_attributes,
)
from ..core.equipment_drop import DEFAULT_SLOT_WEIGHTS, build_equipment_pool
from ..core.item_rules import ItemRules
from ..data import DataBase
from ..models import Player
from ..rank_rules import level_to_rank, normalize_rank
from .task_penalty import calc_abandon_penalty, charge_abandon_penalty

if TYPE_CHECKING:
    from ..core import StorageRingManager

__all__ = ["BountyManager"]


class BountyManager:
    """通缉令管理器（每日刷新 · 随机强敌 · 挑战制）"""

    BOARD_SIZE = 5
    MAX_DAILY_ATTEMPTS = 3
    BOARD_CACHE_SECONDS = 600
    # 当前版本所有能产生悬赏进度的活动标签，保留用于旧配置校验与兼容
    TRIGGERABLE_TAGS = {
        "adventure_scout",
        "adventure_roam",
        "adventure_hunt",
        "adventure_peril",
        "rift",
        "escort",
        "craft",
        "spar",
        "pvp",
        "tide",
        "boss_damage",
        "harvest",
        "sect",
    }
    CONFIG_FILE = Path(__file__).resolve().parents[1] / "config" / "bounty_templates.json"
    ADVENTURE_CONFIG_FILE = Path(__file__).resolve().parents[1] / "config" / "adventure_config.json"
    DEFAULT_CONFIG = {
        "difficulties": {
            "easy": {"name": "F级", "stone_scale": 1.0, "exp_scale": 1.0, "min_level": 0}
        },
        "templates": [
            {
                "id": 1,
                "name": "击退妖兽",
                "difficulty": "easy",
                "category": "巡山",
                "progress_tags": ["adventure_scout"],
                "min_target": 3,
                "max_target": 5,
                "time_limit": 3600,
                "reward": {"stone": 300, "exp": 2500},
                "item_table": "hunt",
                "description": "驱逐骚扰山门的妖兽。",
            }
        ],
        "item_tables": {
            "hunt": [
                {"name": "灵兽毛皮", "weight": 40, "min": 1, "max": 3},
                {"name": "妖兽精血", "weight": 30, "min": 1, "max": 2},
                {"name": "玄铁", "weight": 30, "min": 1, "max": 2},
            ]
        },
    }

    # 敌人流派：决定姓名风格、主修方向与基础属性倾向
    ARCHETYPES: Dict[str, dict] = {
        "魔修": {
            "titles": ["血手人屠", "噬魂老鬼", "炼尸道人", "千面魔君", "阴煞修罗", "焚心邪僧"],
            "given": ["厉无咎", "苏九阴", "陆沉舟", "殷破军", "夜孤寒", "鬼见愁"],
            "element": ["水", "火", "木", ""],
            "desc": "修炼邪法的魔道修士，所过之处生灵涂炭。",
        },
        "剑修": {
            "titles": ["夺命判官", "白衣剑煞", "孤鸿剑客", "断水狂生", "无相剑痴", "追魂剑影"],
            "given": ["顾长风", "叶孤城", "谢无衣", "沈青锋", "楚寒山", "秦照影"],
            "element": ["金", "木", ""],
            "desc": "以杀证道的剑修，出剑必见血。",
        },
        "体修": {
            "titles": ["黑风寨主", "铁臂金刚", "撼山魔猿", "铜皮恶霸", "镇岳狂徒", "不坏凶僧"],
            "given": ["石破天", "鲁铁山", "熊震岳", "武奎", "铁木真", "金不换"],
            "element": ["土", "金", ""],
            "desc": "横练肉身的体修狂徒，一力降十会。",
        },
        "灵修": {
            "titles": ["玉面毒狐", "云梦妖姬", "玄阴圣女", "焚天妖女", "碧眼妖尊", "九尾灵魅"],
            "given": ["苏媚儿", "白浅浅", "狐九娘", "月无瑕", "柳如烟", "花弄影"],
            "element": ["火", "水", "木", ""],
            "desc": "操纵天地灵气的灵修，法术诡谲难测。",
        },
        "鬼修": {
            "titles": ["笑面阎罗", "幽冥鬼将", "白骨夫人", "黄泉引路人", "无头煞神", "阴司判官"],
            "given": ["陆判", "孟婆", "黑无常", "白无常", "聂小倩", "宁采臣"],
            "element": ["水", "土", ""],
            "desc": "游走阴阳两界的鬼修，手段阴狠。",
        },
        "御兽": {
            "titles": ["万兽尊者", "驭虫老怪", "蛊毒巫祝", "兽王猎户", "百妖之主", "蛇蝎夫人"],
            "given": ["公冶长", "苗疆客", "蛊真人", "兽王", "阿蛮", "毒娘子"],
            "element": ["木", "土", "火", ""],
            "desc": "驱使妖兽蛊虫的邪修，身边毒物环伺。",
        },
    }

    QUALITY_WEIGHTS = {"一般": 55, "良好": 30, "极佳": 12, "大师": 3}
    PROFILE_ATTACK = {
        "physical": (0.82, 0.18),
        "magic": (0.18, 0.82),
        "hybrid": (0.50, 0.50),
        "guardian": (0.40, 0.60),
    }
    MATERIAL_TABLES = {
        "low": [
            {"name": "洗练石", "weight": 32, "min": 1, "max": 2},
            {"name": "轮回骨", "weight": 28, "min": 1, "max": 3},
            {"name": "无极骨", "weight": 22, "min": 1, "max": 2},
            {"name": "噬魂铁", "weight": 18, "min": 1, "max": 2},
        ],
        "mid": [
            {"name": "洗练石", "weight": 20, "min": 1, "max": 3},
            {"name": "星辰铁", "weight": 20, "min": 2, "max": 4},
            {"name": "九天骨", "weight": 18, "min": 1, "max": 2},
            {"name": "混沌皮", "weight": 16, "min": 1, "max": 2},
            {"name": "寂灭液", "weight": 10, "min": 1, "max": 2},
            {"name": "秘境精华", "weight": 8, "min": 1, "max": 1},
        ],
        "high": [
            {"name": "洗练石", "weight": 18, "min": 2, "max": 4},
            {"name": "八卦日曜晶皮", "weight": 16, "min": 1, "max": 2},
            {"name": "诛仙龙骨铁", "weight": 16, "min": 1, "max": 2},
            {"name": "寂灭液", "weight": 12, "min": 1, "max": 2},
            {"name": "秘境精华", "weight": 10, "min": 1, "max": 2},
            {"name": "四象皮", "weight": 1, "min": 1, "max": 1},
            {"name": "混元铁", "weight": 1, "min": 1, "max": 1},
        ],
        "legend": [
            {"name": "洗练石", "weight": 15, "min": 2, "max": 4},
            {"name": "寂灭液", "weight": 12, "min": 2, "max": 3},
            {"name": "秘境精华", "weight": 10, "min": 1, "max": 2},
            {"name": "八卦日曜晶皮", "weight": 10, "min": 1, "max": 2},
            {"name": "诛仙龙骨铁", "weight": 10, "min": 1, "max": 2},
            {"name": "四象皮", "weight": 1, "min": 1, "max": 1},
            {"name": "混元铁", "weight": 1, "min": 1, "max": 1},
            {"name": "太初道莲", "weight": 1, "min": 1, "max": 1},
            {"name": "太初道金", "weight": 1, "min": 1, "max": 1},
            {"name": "太古真龙鳞", "weight": 1, "min": 1, "max": 1},
            {"name": "太初源液", "weight": 1, "min": 1, "max": 1},
        ],
    }

    def __init__(
        self,
        db: DataBase,
        storage_ring_manager: Optional["StorageRingManager"] = None,
        config_manager=None,
    ):
        self.db = db
        self.storage_ring_manager = storage_ring_manager
        self.config_manager = config_manager
        self._bounty_cache: Dict[str, Dict] = {}
        self._daily_table_ready = False
        self.difficulties: Dict[str, dict] = {}
        self.templates_by_id: Dict[int, dict] = {}
        self.templates_by_diff: Dict[str, List[dict]] = {}
        self.item_tables: Dict[str, List[dict]] = {}
        self.adventure_tag_meta: Dict[str, Dict[str, int]] = {}
        self.reload_config()

    # ------------------------------------------------------------------
    # 旧配置加载：保留给历史模板校验与兼容，不再参与每日通缉榜生成
    # ------------------------------------------------------------------

    def reload_config(self):
        config = self._load_config_file()
        self.difficulties = config.get("difficulties", self.DEFAULT_CONFIG["difficulties"])
        self.item_tables = config.get("item_tables", self.DEFAULT_CONFIG["item_tables"])
        self.templates_by_id = {}
        self.templates_by_diff = {}
        for tpl_copy in self._sanitize_templates(config.get("templates", [])):
            self.templates_by_id[tpl_copy["id"]] = tpl_copy
            self.templates_by_diff.setdefault(tpl_copy["difficulty"], []).append(tpl_copy)
        self._bounty_cache.clear()
        logger.info(f"通缉令配置加载完成：{len(self.templates_by_id)} 条历史模板")
        self._load_adventure_meta()

    def _sanitize_templates(self, templates: list) -> List[dict]:
        """过滤冲突或残缺的模板，避免列表展示后无法接取/推进。"""
        cleaned: List[dict] = []
        seen_ids = set()
        for raw in templates:
            try:
                tpl = dict(raw)
                template_id = int(tpl["id"])
                name = str(tpl.get("name", "")).strip()
                difficulty = str(tpl.get("difficulty", "")).strip()
                if not name or difficulty not in self.difficulties:
                    logger.warning(f"悬赏模板 {template_id} 名称或难度无效，已跳过")
                    continue
                if template_id in seen_ids:
                    logger.warning(f"悬赏模板 ID 重复：{template_id}，已跳过后者")
                    continue

                min_target = max(1, int(tpl.get("min_target", 1)))
                max_target = max(min_target, int(tpl.get("max_target", min_target)))
                progress_tags = [
                    str(tag).strip().lower()
                    for tag in tpl.get("progress_tags", [])
                    if str(tag).strip()
                ]
                if not progress_tags:
                    logger.warning(f"悬赏模板 {template_id} 缺少可推进标签，已跳过")
                    continue
                unknown_tags = set(progress_tags) - self.TRIGGERABLE_TAGS
                if unknown_tags:
                    logger.warning(
                        f"悬赏模板 {template_id} 含未知进度标签 {sorted(unknown_tags)}，"
                        "该标签不会产生进度"
                    )

                item_table = str(tpl.get("item_table", "gather") or "gather")
                if item_table not in self.item_tables:
                    logger.warning(
                        f"悬赏模板 {template_id} 的奖励表 {item_table} 不存在，回退为 gather"
                    )
                    item_table = "gather"

                tpl.update(
                    {
                        "id": template_id,
                        "name": name,
                        "difficulty": difficulty,
                        "min_target": min_target,
                        "max_target": max_target,
                        "progress_tags": progress_tags,
                        "item_table": item_table,
                        "weight": max(1, int(tpl.get("weight", 1))),
                        "time_limit": max(60, int(tpl.get("time_limit", 3600))),
                    }
                )
                seen_ids.add(template_id)
                cleaned.append(tpl)
            except Exception as exc:
                logger.warning(f"悬赏模板配置异常，已跳过：{exc}")
        return cleaned

    def _load_config_file(self) -> dict:
        if self.CONFIG_FILE.exists():
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.error(f"加载 bounty_templates.json 失败，将使用默认配置: {exc}")
        return self.DEFAULT_CONFIG

    def _load_adventure_meta(self):
        self.adventure_tag_meta = {}
        if not self.ADVENTURE_CONFIG_FILE.exists():
            return
        try:
            with open(self.ADVENTURE_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for route in data.get("routes", []):
                tag = str(route.get("bounty_tag", "")).lower()
                if not tag:
                    continue
                self.adventure_tag_meta[tag] = {
                    "duration": int(route.get("duration", 3600)),
                    "fatigue": int(route.get("fatigue_cooldown", 0)),
                }
        except Exception as exc:
            logger.warning(f"加载冒险路线配置失败，将使用默认时限: {exc}")

    def _get_difficulty_plan(self, level_index: int) -> List[str]:
        """按配置中的 min_level 解锁难度（保留旧测试与兼容入口）。"""
        plan = [
            diff
            for diff, cfg in self.difficulties.items()
            if level_index >= int(cfg.get("min_level", 0) or 0)
        ]
        plan.sort(key=lambda diff: int(self.difficulties[diff].get("min_level", 0) or 0))
        return plan

    # ------------------------------------------------------------------
    # 每日通缉榜
    # ------------------------------------------------------------------

    @staticmethod
    def _day_key(now: Optional[int] = None) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))

    @staticmethod
    def _day_end_ts(now: Optional[int] = None) -> int:
        lt = time.localtime(now if now is not None else time.time())
        return int(
            time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1)
            )
        )

    def _max_level(self) -> int:
        levels = getattr(self.config_manager, "level_data", None) or []
        return max(0, len(levels) - 1) or 35

    def _level_name(self, level_index: int, cultivation_type: str = "灵修") -> str:
        try:
            levels = self.config_manager.get_level_data(cultivation_type) or []
        except Exception:
            levels = getattr(self.config_manager, "level_data", None) or []
        if 0 <= level_index < len(levels):
            return str(levels[level_index].get("level_name") or f"境界{level_index}")
        return f"境界{level_index}"

    def _board_target_levels(self, level_index: int) -> List[int]:
        """围绕玩家境界生成 5 个不同目标境界，覆盖低阶到越阶挑战。"""
        max_level = self._max_level()
        base = max(0, min(max_level, int(level_index or 0)))
        offsets = [-6, -3, 0, 3, 6]
        levels: List[int] = []
        for offset in offsets:
            value = max(0, min(max_level, base + offset))
            if value not in levels:
                levels.append(value)
        fill_offsets = [1, -1, 2, -2, 4, -4, 5, -5, 7, -7, 8, -8]
        while len(levels) < self.BOARD_SIZE:
            added = False
            for offset in fill_offsets:
                value = max(0, min(max_level, base + offset))
                if value not in levels:
                    levels.append(value)
                    added = True
                    break
            if not added:
                break
        return sorted(levels)[: self.BOARD_SIZE]

    def _board_cache_key(self, player: Player) -> str:
        return f"{player.user_id}:{self._day_key()}:{int(player.level_index or 0)}"

    def _generate_daily_board(self, player: Player) -> List[dict]:
        key = self._board_cache_key(player)
        cached = self._bounty_cache.get(key)
        now = int(time.time())
        if cached and int(cached.get("expire_time", 0)) > now:
            return cached["bounties"]

        day = self._day_key()
        rng = random.Random(f"{day}:{player.user_id}:{int(player.level_index or 0)}")
        levels = self._board_target_levels(player.level_index)
        bounties = [
            self._generate_bounty(rng, idx, level, player)
            for idx, level in enumerate(levels, start=1)
        ]
        self._bounty_cache[key] = {
            "bounties": bounties,
            "expire_time": now + self.BOARD_CACHE_SECONDS,
        }
        return bounties

    def _weighted_pick(self, rng: random.Random, entries: list, weight_key: str = "weight"):
        if not entries:
            return None
        weights = [max(1, int(item.get(weight_key, 1) or 1)) for item in entries]
        return rng.choices(entries, weights=weights, k=1)[0]

    def _technique_pool(self, target_level: int) -> Dict[str, List[dict]]:
        """按目标境界筛出可搭配的功法，返回 main/technique 两组。"""
        pool: Dict[str, List[dict]] = {"main_technique": [], "technique": []}
        seen = set()
        sources = []
        for attr in ("techniques_data", "items_data"):
            data = getattr(self.config_manager, attr, {}) or {}
            if isinstance(data, dict):
                sources.append(data)
        for source in sources:
            for name, cfg in source.items():
                if not isinstance(cfg, dict) or name in seen:
                    continue
                item_type = ItemRules.normalize_equipment_type(cfg)
                if item_type not in pool:
                    continue
                seen.add(name)
                try:
                    required = int(ItemRules.get_required_level(cfg) or 0)
                except Exception:
                    required = 0
                if required > target_level + 3:
                    continue
                pool[item_type].append(
                    {
                        "name": name,
                        "config": cfg,
                        "weight": max(
                            1,
                            int(cfg.get("drop_weight") or cfg.get("shop_weight") or 1),
                        ),
                    }
                )
        return pool

    def _pick_techniques(
        self, rng: random.Random, target_level: int
    ) -> Tuple[Optional[dict], List[dict]]:
        pool = self._technique_pool(target_level)
        main_pool = list(pool.get("main_technique") or [])
        normal_pool = list(pool.get("technique") or [])
        main = self._weighted_pick(rng, main_pool)
        normals: List[dict] = []
        pick_count = rng.randint(1, 3)
        for _ in range(pick_count):
            if not normal_pool:
                break
            picked = self._weighted_pick(rng, normal_pool)
            if not picked:
                break
            normals.append(picked)
            normal_pool = [item for item in normal_pool if item["name"] != picked["name"]]
        return main, normals

    def _equipment_pool_for_level(self, target_level: int) -> Dict[str, List[dict]]:
        raw_pool = build_equipment_pool(self.config_manager)
        result: Dict[str, List[dict]] = {}
        for slot, entries in raw_pool.items():
            filtered = []
            for entry in entries:
                cfg = entry.get("config") or {}
                try:
                    required = int(ItemRules.get_required_level(cfg) or 0)
                except Exception:
                    required = 0
                if required <= target_level + 1:
                    filtered.append(entry)
            if filtered:
                result[slot] = filtered
        return result

    def _roll_quality(self, rng: random.Random) -> str:
        qualities = list(self.QUALITY_WEIGHTS)
        weights = [self.QUALITY_WEIGHTS[q] for q in qualities]
        return rng.choices(qualities, weights=weights, k=1)[0]

    def _pick_equipment(
        self, rng: random.Random, target_level: int
    ) -> List[dict]:
        pool = self._equipment_pool_for_level(target_level)
        available_slots = [slot for slot, entries in pool.items() if entries]
        if not available_slots:
            return []
        slot_weights = {
            slot: int(DEFAULT_SLOT_WEIGHTS.get(slot, 10) or 0)
            for slot in available_slots
        }
        pick_count = min(len(available_slots), rng.randint(3, 5))
        picked_slots = rng.choices(
            available_slots,
            weights=[max(1, slot_weights.get(slot, 1)) for slot in available_slots],
            k=pick_count,
        )
        # 去重后不足则补齐
        unique_slots: List[str] = []
        for slot in picked_slots:
            if slot not in unique_slots:
                unique_slots.append(slot)
        for slot in available_slots:
            if len(unique_slots) >= pick_count:
                break
            if slot not in unique_slots:
                unique_slots.append(slot)

        equipment: List[dict] = []
        for slot in unique_slots:
            weighted_entries = [
                {
                    "name": item["name"],
                    "config": item["config"],
                    "weight": int(
                        item["config"].get("drop_weight")
                        or item["config"].get("shop_weight")
                        or 1
                    ),
                }
                for item in pool.get(slot, [])
            ]
            entry = self._weighted_pick(rng, weighted_entries, "weight")
            if not entry:
                continue
            cfg = entry.get("config") or {}
            quality = self._roll_quality(rng)
            equipment.append(
                {
                    "slot": slot,
                    "slot_name": {
                        "body": "甲胄",
                        "ring": "戒指",
                    }.get(slot, ItemRules.SLOT_LABELS.get(slot, slot)),
                    "name": entry["name"],
                    "quality": quality,
                    "rank": normalize_rank(cfg.get("rank", ""), level_to_rank(target_level)),
                    "effects": effective_effects(cfg, quality),
                    "rates": equipment_rates(cfg),
                }
            )
        return equipment

    def _generate_bounty(
        self, rng: random.Random, bounty_id: int, target_level: int, player: Player
    ) -> dict:
        """生成一条随机通缉目标，返回可直接落库/展示的完整快照。"""
        archetype_key = rng.choice(list(self.ARCHETYPES))
        archetype = self.ARCHETYPES[archetype_key]
        title = rng.choice(archetype["titles"])
        given = rng.choice(archetype["given"])
        enemy_name = f"{title}·{given}"

        temp = Player(
            user_id=f"bounty_template_{bounty_id}",
            level_index=target_level,
            cultivation_type=archetype_key,
        )
        base = realm_attributes(temp, self.config_manager)
        base_phys = max(1, int(base.get("physical_damage", 0) or 0))
        base_magic = max(1, int(base.get("magic_damage", 0) or 0))
        base_pdef = max(1, int(base.get("physical_defense", 0) or 0))
        base_mdef = max(1, int(base.get("magic_defense", 0) or 0))
        base_hp = max(1, int(base.get("max_hp", 1) or 1))
        defense_scale = max(1, int(base.get("defense_scale", 1) or 1))
        base_mp = max(1000, int(base.get("max_mp", base_hp // 2) or base_hp // 2))

        main_tech, normal_techs = self._pick_techniques(rng, target_level)
        equipment = self._pick_equipment(rng, target_level)

        profile = rng.choice(list(self.PROFILE_ATTACK))
        phys_share, magic_share = self.PROFILE_ATTACK[profile]
        hp_mult = rng.uniform(0.92, 1.12)
        atk_mult = rng.uniform(0.88, 1.04)
        def_mult = rng.uniform(0.88, 1.08)

        effects: Dict[str, float] = {
            "crit_rate": 0.0,
            "crit_damage": 1.5,
            "lifesteal": 0.0,
            "counter_damage": 0.0,
            "defense_bonus": 0.0,
            "combo_rate": 0.0,
            "dodge": 0.0,
        }
        element_counts: Dict[str, int] = {}

        all_techs = [main_tech] + normal_techs
        for tech in all_techs:
            if not tech:
                continue
            cfg = tech.get("config") or {}
            element = str(cfg.get("element") or "").strip()
            if element:
                element_counts[element] = element_counts.get(element, 0) + 1
            for key, value in (cfg.get("effects") or {}).items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                effects[key] = effects.get(key, 0) + number

        phys_rate = 0.0
        magic_rate = 0.0
        pdef_rate = 0.0
        mdef_rate = 0.0
        for gear in equipment:
            for key, value in (gear.get("effects") or {}).items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                effects[key] = effects.get(key, 0) + number
            rates = gear.get("rates") or {}
            phys_rate += float(rates.get("physical_damage", 0) or 0)
            magic_rate += float(rates.get("magic_damage", 0) or 0)
            pdef_rate += float(rates.get("physical_defense", 0) or 0)
            mdef_rate += float(rates.get("magic_defense", 0) or 0)

        from .combat_manager import CombatManager

        effects = CombatManager.clamp_effects(
            effects, skip=("crit_rate", "lifesteal", "counter_damage")
        )
        overflow_mult = CombatManager.normalize_overflow_effects(effects)
        if overflow_mult != 1.0:
            atk_mult *= overflow_mult

        physical_atk = int(base_phys * phys_share * atk_mult) + int(base_phys * phys_rate)
        magic_atk = int(base_magic * magic_share * atk_mult) + int(base_magic * magic_rate)
        max_hp = max(1, int(base_hp * hp_mult))
        physical_def = int(base_pdef * def_mult) + int(base_pdef * pdef_rate)
        magic_def = int(base_mdef * def_mult) + int(base_mdef * mdef_rate)
        atk = max(1, physical_atk + magic_atk)
        defense = max(0, physical_def + magic_def)

        element = ""
        if element_counts:
            element = max(element_counts, key=element_counts.get)
        elif archetype.get("element"):
            element = rng.choice([e for e in archetype["element"] if e] or [""])

        active_skill = {}
        if main_tech:
            active_skill = dict((main_tech.get("config") or {}).get("active_skill") or {})

        diff = int(target_level) - int(player.level_index or 0)
        if diff >= 5:
            danger = "高危越阶"
        elif diff >= 1:
            danger = "越阶挑战"
        elif diff >= -1:
            danger = "同境劲敌"
        else:
            danger = "低阶目标"

        risk_mult = 1.0 + min(0.5, max(0, diff) * 0.10)
        reward_exp = int(
            (1800 + target_level * 900 + target_level * target_level * 80)
            * risk_mult
            * rng.uniform(0.92, 1.08)
        )
        reward_stone = int(
            (200 + target_level * 150 + target_level * target_level * 15)
            * risk_mult
            * rng.uniform(0.92, 1.08)
        )
        drop_table = self._drop_table_for_level(target_level)
        equipment_chance = min(75, 45 + max(0, diff) * 3)
        technique_chance = min(22, 8 + max(0, diff) * 2)

        return {
            "id": bounty_id,
            "name": enemy_name,
            "title": title,
            "archetype": archetype_key,
            "level_index": int(target_level),
            "realm_name": self._level_name(target_level, archetype_key),
            "element": element,
            "danger": danger,
            "description": archetype.get("desc", ""),
            "hp": max_hp,
            "max_hp": max_hp,
            "mp": base_mp,
            "max_mp": base_mp,
            "atk": atk,
            "defense": defense,
            "physical_atk": physical_atk,
            "magic_atk": magic_atk,
            "physical_defense": physical_def,
            "magic_defense": magic_def,
            "defense_scale": defense_scale,
            "crit_rate": float(effects.get("crit_rate", 0) or 0),
            "crit_damage": float(effects.get("crit_damage", 1.5) or 1.5),
            "lifesteal": float(effects.get("lifesteal", 0) or 0),
            "counter_damage": float(effects.get("counter_damage", 0) or 0),
            "status_resist": float(effects.get("status_resist", 0) or 0),
            "effects": effects,
            "main_technique": main_tech["name"] if main_tech else "",
            "techniques": [tech["name"] for tech in normal_techs],
            "main_technique_effects": (main_tech.get("config") or {}).get("effects", {})
            if main_tech
            else {},
            "technique_details": [
                {
                    "name": tech["name"],
                    "rank": normalize_rank(
                        (tech.get("config") or {}).get("rank", ""), ""
                    ),
                    "element": (tech.get("config") or {}).get("element", ""),
                    "effects": dict((tech.get("config") or {}).get("effects") or {}),
                }
                for tech in normal_techs
            ],
            "equipment": [
                {
                    "slot": gear["slot"],
                    "slot_name": gear["slot_name"],
                    "name": gear["name"],
                    "quality": gear["quality"],
                    "rank": gear["rank"],
                }
                for gear in equipment
            ],
            "active_skill": active_skill,
            "drop_table": drop_table,
            "equipment_chance": equipment_chance,
            "technique_chance": technique_chance,
            "reward": {
                "stone": max(1, reward_stone),
                "exp": max(1, reward_exp),
            },
        }

    @staticmethod
    def _drop_table_for_level(level_index: int) -> str:
        if level_index < 10:
            return "low"
        if level_index < 18:
            return "mid"
        if level_index < 26:
            return "high"
        return "legend"

    # ------------------------------------------------------------------
    # 每日挑战次数
    # ------------------------------------------------------------------

    async def _ensure_daily_table(self):
        if self._daily_table_ready:
            return
        await self.db.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bounty_daily (
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )
            """
        )
        await self.db.conn.commit()
        self._daily_table_ready = True

    async def get_daily_attempts(self, player: Player) -> int:
        """返回玩家今日已使用的挑战次数。"""
        try:
            await self._ensure_daily_table()
            async with self.db.conn.execute(
                "SELECT attempts FROM bounty_daily WHERE user_id = ? AND day = ?",
                (str(player.user_id), self._day_key()),
            ) as cursor:
                row = await cursor.fetchone()
            return max(0, int(row[0] or 0)) if row else 0
        except Exception as exc:
            logger.warning(f"读取通缉令每日次数失败: {exc}")
            return 0

    async def remaining_attempts(self, player: Player) -> int:
        used = await self.get_daily_attempts(player)
        return max(0, self.MAX_DAILY_ATTEMPTS - used)

    async def _consume_daily_attempt(self, user_id: str) -> bool:
        await self._ensure_daily_table()
        cursor = await self.db.conn.execute(
            """
            INSERT INTO bounty_daily (user_id, day, attempts)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, day) DO UPDATE SET attempts = attempts + 1
            WHERE attempts < ?
            """,
            (str(user_id), self._day_key(), self.MAX_DAILY_ATTEMPTS),
        )
        await self.db.conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 列表 / 接取 / 详情
    # ------------------------------------------------------------------

    async def get_bounty_list(self, player: Player) -> List[dict]:
        """获取今日通缉榜（按日期 + 玩家 + 境界确定性生成）。"""
        return self._generate_daily_board(player)

    async def accept_bounty(self, player: Player, bounty_id: int) -> Tuple[bool, str]:
        if bounty_id <= 0:
            return False, "无效的通缉令编号。"

        if await self.remaining_attempts(player) <= 0:
            return False, (
                f"❌ 今日挑战次数已用尽（{self.MAX_DAILY_ATTEMPTS}/{self.MAX_DAILY_ATTEMPTS}）。\n"
                "💡 明日刷新后可继续挑战。"
            )

        board = self._generate_daily_board(player)
        bounty = next((b for b in board if int(b.get("id", 0)) == int(bounty_id)), None)
        if not bounty:
            return False, "⚠️ 通缉榜已刷新，请先发送 /悬赏令 重新查看后再接取。"

        now = int(time.time())
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            active = await self.db.ext.get_active_bounty(player.user_id)
            if active:
                if int(active.get("expire_time") or 0) <= now:
                    await self.db.conn.execute(
                        "UPDATE bounty_tasks SET status = 3 WHERE user_id = ? AND status = 1",
                        (player.user_id,),
                    )
                else:
                    await self.db.conn.rollback()
                    return False, (
                        f"你已接取通缉令：{active['bounty_name']}\n"
                        "💡 使用 /悬赏状态 查看详情，/挑战悬赏 开始战斗。\n"
                        "💡 如需更换目标，可使用 /放弃悬赏 缴纳罚款后放弃。"
                    )

            expire_time = self._day_end_ts(now)
            snapshot = dict(bounty)
            snapshot["accepted_at"] = now
            snapshot["expire_time"] = expire_time
            await self.db.conn.execute(
                """
                INSERT INTO bounty_tasks (
                    user_id, bounty_id, bounty_name, target_type,
                    target_count, current_progress, rewards,
                    start_time, expire_time, status
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 1)
                """,
                (
                    player.user_id,
                    int(bounty["id"]),
                    str(bounty["name"]),
                    "challenge",
                    1,
                    json.dumps(snapshot, ensure_ascii=False),
                    now,
                    expire_time,
                ),
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        return True, (
            f"🎯 已接取通缉令！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"目标：{bounty['name']}（{bounty['realm_name']}·{bounty['danger']}）\n"
            f"境界：{bounty['realm_name']} · {bounty['element'] or '无'}属性\n"
            f"悬赏：{bounty['reward']['stone']:,} 灵石 + {bounty['reward']['exp']:,} 修为\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 /悬赏状态 查看功法、装备与技能详情\n"
            f"💡 /挑战悬赏 开始战斗（今日剩余 "
            f"{await self.remaining_attempts(player)}/{self.MAX_DAILY_ATTEMPTS} 次）"
        )

    async def check_bounty_status(self, player: Player) -> Tuple[bool, str]:
        active = await self.db.ext.get_active_bounty(player.user_id)
        if not active:
            remaining = await self.remaining_attempts(player)
            return False, (
                "📜 你当前没有接取通缉令。\n"
                "━━━━━━━━━━━━━━━\n"
                f"今日剩余挑战次数：{remaining}/{self.MAX_DAILY_ATTEMPTS}\n"
                "💡 /悬赏令 查看今日通缉榜\n"
                "💡 /接取悬赏 <编号> 接取目标"
            )

        now = int(time.time())
        if now > int(active.get("expire_time") or 0):
            await self.db.conn.execute(
                "UPDATE bounty_tasks SET status = 3 WHERE user_id = ? AND status = 1",
                (player.user_id,),
            )
            await self.db.conn.commit()
            return False, "💨 该通缉令已过期，今日通缉榜已刷新。"

        try:
            snapshot = json.loads(active.get("rewards") or "{}")
        except Exception:
            snapshot = {}
        if not snapshot or "reward" not in snapshot:
            return False, "❌ 通缉令数据损坏，请放弃后重新接取。"

        remaining = await self.remaining_attempts(player)
        remaining_time = max(0, int(active.get("expire_time") or 0) - now)
        hours, rem = divmod(remaining_time, 3600)
        minutes = rem // 60

        effect_parts = []
        if float(snapshot.get("crit_rate", 0) or 0) > 0:
            effect_parts.append(f"会心 {float(snapshot['crit_rate']):.1f}%")
        if float(snapshot.get("crit_damage", 1.5) or 1.5) > 1.5:
            effect_parts.append(f"暴伤 {float(snapshot['crit_damage']):.0%}")
        if float(snapshot.get("lifesteal", 0) or 0) > 0:
            effect_parts.append(f"吸血 {float(snapshot['lifesteal']):.1f}%")
        if float(snapshot.get("counter_damage", 0) or 0) > 0:
            effect_parts.append(f"反伤 {float(snapshot['counter_damage']):.1f}%")
        status_resist = float(snapshot.get("status_resist", 0) or 0)
        if status_resist > 0:
            effect_parts.append(f"抗性 {status_resist:.0f}%")

        lines = [
            "📜 通缉令详情",
            "━━━━━━━━━━━━━━━",
            f"👹 目标：{snapshot.get('name', active['bounty_name'])}",
            f"🏷️ 类型：{snapshot.get('archetype', '未知')} · "
            f"{snapshot.get('element') or '无'}属性 · {snapshot.get('danger', '未知')}",
            f"🧬 境界：{snapshot.get('realm_name', '未知')}（等级 {snapshot.get('level_index', 0)}）",
            f"📖 {snapshot.get('description', '')}",
            "",
            "⚔️ 属性面板",
            f"  ❤️ 气血 {int(snapshot.get('max_hp', 0)):,} | "
            f"🌀 灵气 {int(snapshot.get('max_mp', 0)):,}",
            f"  🗡️ 物攻 {int(snapshot.get('physical_atk', 0)):,} | "
            f"✨ 法攻 {int(snapshot.get('magic_atk', 0)):,}",
            f"  🛡️ 物防 {int(snapshot.get('physical_defense', 0)):,} | "
            f"🔮 法防 {int(snapshot.get('magic_defense', 0)):,}",
        ]
        if effect_parts:
            lines.append("  " + " | ".join(effect_parts))

        lines.append("")
        lines.append("📚 功法搭配")
        main_tech = snapshot.get("main_technique") or "无"
        lines.append(f"  · 主修：{main_tech}")
        if snapshot.get("techniques"):
            lines.append(f"  · 辅修：{'、'.join(snapshot.get('techniques') or [])}")
        else:
            lines.append("  · 辅修：无")

        lines.append("")
        lines.append("🛡️ 装备搭配")
        equipment = snapshot.get("equipment") or []
        if equipment:
            for gear in equipment:
                lines.append(
                    f"  · {gear.get('slot_name', '装备')}：{gear.get('name', '未知')}"
                    f"（{gear.get('quality', '一般')}·{gear.get('rank', '未知')}）"
                )
        else:
            lines.append("  · 赤手空拳")

        skill = snapshot.get("active_skill") or {}
        lines.append("")
        if skill:
            from ..core.technique_skill import skill_effect_text, skill_type_label

            lines.append(
                f"⚔️ 绝技：{skill.get('name', '未知绝技')}"
                f"（{skill_type_label(skill)}）"
            )
            lines.append(
                f"  · 第 {int(skill.get('trigger_round', 2) or 2)} 回合起施展，"
                f"消耗 {int(skill.get('mp_cost', 0) or 0):,} 灵气"
            )
            lines.append(f"  · 效果：{skill_effect_text(skill)}")
        else:
            lines.append("⚔️ 绝技：无")

        reward = snapshot.get("reward") or {}
        lines.extend(
            [
                "",
                "🎁 悬赏奖励",
                f"  · 灵石 +{int(reward.get('stone', 0)):,}",
                f"  · 修为 +{int(reward.get('exp', 0)):,}",
                f"  · 材料掉落：{self._drop_table_label(snapshot.get('drop_table', 'low'))}",
                f"  · 装备掉率：{int(snapshot.get('equipment_chance', 0) or 0)}%",
                f"  · 功法掉率：{int(snapshot.get('technique_chance', 0) or 0)}%",
                "",
                f"⏳ 剩余时间：{hours} 小时 {minutes} 分钟",
                f"🎯 今日剩余挑战次数：{remaining}/{self.MAX_DAILY_ATTEMPTS}",
                "━━━━━━━━━━━━━━━",
                "💡 /挑战悬赏 开始战斗",
                "💡 /放弃悬赏 缴纳罚款并放弃目标",
            ]
        )
        return True, "\n".join(lines)

    @staticmethod
    def _drop_table_label(table: str) -> str:
        return {
            "low": "低阶材料（洗练石/轮回骨等）",
            "mid": "中阶材料（星辰铁/九天骨等）",
            "high": "高阶材料（八卦日曜晶皮/诛仙龙骨铁等）",
            "legend": "传说材料（太初道莲/太古真龙鳞等）",
        }.get(str(table or "low"), "随机材料")

    async def complete_bounty(self, player: Player) -> Tuple[bool, str]:
        """兼容旧指令：挑战制通缉令不再需要手动完成。"""
        active = await self.db.ext.get_active_bounty(player.user_id)
        if not active:
            return False, "你当前没有接取通缉令。\n💡 使用 /悬赏令 查看今日通缉榜。"
        return False, (
            "📜 通缉令已改为战斗挑战制。\n"
            "━━━━━━━━━━━━━━━\n"
            f"当前目标：{active['bounty_name']}\n"
            "💡 直接发送 /挑战悬赏 开始战斗，胜利后自动结算奖励，无需手动完成。"
        )

    async def abandon_bounty(self, player: Player) -> Tuple[bool, str]:
        active = await self.db.ext.get_active_bounty(player.user_id)
        if not active:
            return False, "你当前没有接取通缉令。"

        try:
            snapshot = json.loads(active.get("rewards") or "{}")
        except Exception:
            snapshot = {}
        reward = snapshot.get("reward") or {}
        cost, currency = calc_abandon_penalty(
            int(reward.get("stone", 0) or 0),
            int(reward.get("spiritual_qi", 0) or 0),
        )
        currency_label = "灵气" if currency == "qi" else "灵石"

        fresh = await self.db.get_player_by_id(player.user_id)
        if not fresh:
            return False, "❌ 玩家数据不存在。"

        ok, msg = await charge_abandon_penalty(fresh, self.db, cost, currency)
        if not ok:
            return False, msg

        await self.db.ext.cancel_bounty(player.user_id)
        return True, (
            f"已放弃通缉令：{active['bounty_name']}\n"
            f"缴纳罚款：{cost:,} {currency_label}\n"
            "💡 可重新发送 /悬赏令 选择其他目标（今日挑战次数不返还）。"
        )

    # ------------------------------------------------------------------
    # 战斗与奖励
    # ------------------------------------------------------------------

    async def _build_player_stats(self, player: Player):
        from .combat_manager import CombatManager
        from .pet_manager import PetManager

        impart = None
        try:
            impart = await self.db.ext.get_impart_info(player.user_id)
        except Exception:
            impart = None
        stats = CombatManager.build_player_combat_stats(
            player,
            self.config_manager,
            db=self.db,
            hp_buff=impart.impart_hp_per if impart else 0.0,
            mp_buff=impart.impart_mp_per if impart else 0.0,
            atk_buff=impart.impart_atk_per if impart else 0.0,
            crit_rate=int((impart.impart_know_per if impart else 0.0) * 100),
            crit_damage_bonus=impart.impart_burst_per if impart else 0.0,
        )
        try:
            pet_mgr = PetManager(self.db)
            main_pet, support_pets, pet_effects, owner_atk = await pet_mgr.build_pet_lineup(
                player.user_id,
                stats.max_hp,
                stats.atk,
                stats.defense,
                player.cultivation_type,
                persist=False,
            )
            CombatManager.apply_pet_lineup(
                stats, main_pet, support_pets, pet_effects, owner_atk
            )
        except Exception:
            logger.warning("通缉令战斗灵兽协战构建失败", exc_info=True)
        return stats

    @staticmethod
    def _build_enemy_stats(snapshot: dict):
        from .combat_manager import CombatStats

        return CombatStats(
            user_id=f"bounty_{snapshot.get('id', 0)}_{snapshot.get('name', 'target')}",
            name=str(snapshot.get("name") or "通缉目标"),
            hp=max(1, int(snapshot.get("max_hp", 1) or 1)),
            max_hp=max(1, int(snapshot.get("max_hp", 1) or 1)),
            mp=max(0, int(snapshot.get("max_mp", 0) or 0)),
            max_mp=max(0, int(snapshot.get("max_mp", 0) or 0)),
            atk=max(1, int(snapshot.get("atk", 1) or 1)),
            defense=max(0, int(snapshot.get("defense", 0) or 0)),
            crit_rate=0,
            exp=0,
            effects=dict(snapshot.get("effects") or {}),
            element=str(snapshot.get("element") or ""),
            physical_atk=max(0, int(snapshot.get("physical_atk", 0) or 0)),
            magic_atk=max(0, int(snapshot.get("magic_atk", 0) or 0)),
            physical_defense=max(0, int(snapshot.get("physical_defense", 0) or 0)),
            magic_defense=max(0, int(snapshot.get("magic_defense", 0) or 0)),
            defense_scale=max(0, int(snapshot.get("defense_scale", 0) or 0)),
            status_resist=float(snapshot.get("status_resist", 0) or 0),
            active_skill=dict(snapshot.get("active_skill") or {}),
        )

    def _apply_reward_bonuses(
        self,
        player: Player,
        snapshot: dict,
        stone: int,
        exp: int,
        pet_gold_bonus: float = 0.0,
    ) -> Tuple[int, int]:
        try:
            from .title_manager import TitleManager

            exp = int(exp * (1 + TitleManager.get_exp_bonus(player)))
        except Exception:
            pass
        try:
            from ..core.technique_bonus import TechniqueBonusManager

            stone = TechniqueBonusManager.apply_gold_bonus(
                player, stone, self.config_manager
            )
        except Exception:
            pass
        pet_gold_bonus = max(0.0, float(pet_gold_bonus or 0.0))
        if pet_gold_bonus:
            stone = int(stone * (1 + pet_gold_bonus))
        # 仙缘【悬赏通缉】：悬赏奖励（灵石 + 修为）永久加成
        try:
            from ..core.reincarnation_rules import permanent_effect

            bounty_bonus = permanent_effect(player, "bounty_bonus")
            if bounty_bonus:
                stone = int(stone * (1.0 + bounty_bonus))
                exp = int(exp * (1.0 + bounty_bonus))
        except Exception:
            pass
        return max(0, stone), max(0, exp)

    def _roll_material(self, table_key: str) -> Optional[Tuple[str, int]]:
        table = self.MATERIAL_TABLES.get(table_key) or self.MATERIAL_TABLES["low"]
        total = sum(max(1, int(item.get("weight", 1))) for item in table)
        roll = random.randint(1, total)
        current = 0
        for item in table:
            current += max(1, int(item.get("weight", 1)))
            if roll <= current:
                low = int(item.get("min", 1))
                high = max(low, int(item.get("max", low)))
                return str(item.get("name", "")), random.randint(low, high)
        return None

    async def _grant_drops(
        self, player: Player, snapshot: dict
    ) -> Tuple[List[str], List[str]]:
        """按目标境界发放材料/装备/功法，返回 (成功列表, 失败列表)。"""
        got: List[str] = []
        lost: List[str] = []
        if not self.storage_ring_manager:
            return got, lost

        table_key = str(snapshot.get("drop_table") or "low")
        roll_count = 1 + (1 if int(snapshot.get("level_index", 0) or 0) >= 15 else 0)
        roll_count += 1 if int(snapshot.get("level_index", 0) or 0) >= 28 else 0
        drops: List[Tuple[str, int, str]] = []
        for _ in range(roll_count):
            material = self._roll_material(table_key)
            if material:
                drops.append((material[0], material[1], ""))

        # 装备掉落
        if random.randint(1, 100) <= int(snapshot.get("equipment_chance", 0) or 0):
            try:
                from ..core.equipment_drop import roll_equipment_drop

                equipment = roll_equipment_drop(
                    self.config_manager,
                    int(snapshot.get("level_index", 0) or 0),
                    up_rank_weight=1.0,
                )
                if equipment:
                    drops.append(
                        (
                            str(equipment.get("name", "")),
                            1,
                            str(equipment.get("quality", "") or ""),
                        )
                    )
            except Exception as exc:
                logger.warning(f"通缉令装备掉落判定失败: {exc}")

        # 功法掉落：按目标境界过滤，避免低阶通缉掉落超阶功法
        if random.randint(1, 100) <= int(snapshot.get("technique_chance", 0) or 0):
            try:
                import copy

                from ..core.technique_bonus import TechniqueBonusManager

                drop_player = copy.copy(player)
                drop_player.level_index = max(0, int(snapshot.get("level_index", 0) or 0))
                tech_name = TechniqueBonusManager.roll_technique_drop(
                    drop_player,
                    self.config_manager,
                    base_chance=100.0,
                    allow_main=False,
                )
                if tech_name:
                    drops.append((tech_name, 1, ""))
            except Exception as exc:
                logger.warning(f"通缉令功法掉落判定失败: {exc}")

        for name, count, quality in drops:
            if not name:
                continue
            label = f"{name}（{quality}）" if quality else name
            try:
                success, _ = await self.storage_ring_manager.grant_item(
                    player, name, count, quality=quality, silent=True
                )
            except Exception as exc:
                logger.warning(f"通缉令掉落发放失败 {label}x{count}: {exc}")
                success = False
            if success:
                got.append(f"{label} x{count}")
            else:
                lost.append(f"{label} x{count}")
        return got, lost

    async def challenge_bounty(self, player: Player) -> Tuple[bool, str]:
        now = int(time.time())
        active = await self.db.ext.get_active_bounty(player.user_id)
        if not active:
            return False, (
                "📜 你当前没有接取通缉令。\n"
                "💡 /悬赏令 查看今日通缉榜，/接取悬赏 <编号> 接取目标。"
            )
        if now > int(active.get("expire_time") or 0):
            await self.db.conn.execute(
                "UPDATE bounty_tasks SET status = 3 WHERE user_id = ? AND status = 1",
                (player.user_id,),
            )
            await self.db.conn.commit()
            return False, "💨 该通缉令已过期，今日通缉榜已刷新。"

        try:
            snapshot = json.loads(active.get("rewards") or "{}")
        except Exception:
            snapshot = {}
        if not snapshot or "reward" not in snapshot:
            return False, "❌ 通缉令数据损坏，请放弃后重新接取。"

        if not await self._consume_daily_attempt(player.user_id):
            return False, (
                f"❌ 今日挑战次数已用尽（{self.MAX_DAILY_ATTEMPTS}/{self.MAX_DAILY_ATTEMPTS}）。\n"
                "💡 明日刷新后可继续挑战。"
            )
        remaining = await self.remaining_attempts(player)

        from .combat_manager import CombatManager

        try:
            player_stats = await self._build_player_stats(player)
            enemy_stats = self._build_enemy_stats(snapshot)
        except Exception:
            logger.exception("通缉令战斗面板构建失败")
            return False, "❌ 战斗准备失败，请稍后再试。"

        # 记录灵兽金币加成，供奖励结算复用
        pet_gold_bonus = 0.0
        try:
            from .pet_manager import PetManager

            pet_gold_bonus = await PetManager(self.db).get_life_bonus(
                player.user_id, "gold_bonus"
            )
        except Exception:
            pet_gold_bonus = 0.0

        result = CombatManager.player_vs_player(player_stats, enemy_stats, combat_type=2)
        win = str(result.get("winner") or "") == str(player.user_id)
        rounds = int(result.get("rounds", 0) or 0)
        player_hp = max(1, int(result.get("player1_real_hp", 1) or 1))
        player_mp = max(0, int(result.get("player1_final_mp", 0) or 0))
        # 爬塔中的玩家：战斗内吸血不能绕过每层回血额度
        from .treasure_map_manager import TreasureMapManager

        player_hp = await TreasureMapManager.clamp_combat_hp(self.db, player.user_id, player_hp)
        enemy_hp = max(0, int(result.get("player2_real_hp", 0) or 0))
        enemy_max_hp = max(1, int(snapshot.get("max_hp", 1) or 1))

        # 战后气血/灵气写回，与其它战斗入口保持一致
        try:
            await self.db.update_player_fields(
                player.user_id,
                hp=player_hp,
                mp=player_mp,
                spiritual_qi=player_mp,
            )
            player.hp = player_hp
            player.spiritual_qi = player_mp
            player.mp = player_mp
        except Exception as exc:
            logger.warning(f"通缉令战后气血写回失败: {exc}")

        drop_lines: List[str] = []
        lost_lines: List[str] = []
        reward = snapshot.get("reward") or {}
        stone = int(reward.get("stone", 0) or 0)
        exp = int(reward.get("exp", 0) or 0)

        if win:
            stone, exp = self._apply_reward_bonuses(
                player, snapshot, stone, exp, pet_gold_bonus
            )
            MAX_VALUE = 2**63 - 1
            player.gold = min(player.gold + stone, MAX_VALUE)
            player.experience = min(player.experience + exp, MAX_VALUE)
            await self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                await self.db.conn.execute(
                    "UPDATE bounty_tasks SET status = 2 WHERE user_id = ? AND status = 1",
                    (player.user_id,),
                )
                await self.db.conn.execute(
                    "UPDATE players SET gold = ?, experience = ? WHERE user_id = ?",
                    (player.gold, player.experience, player.user_id),
                )
                await self.db.conn.commit()
            except Exception:
                await self.db.conn.rollback()
                raise
            drop_lines, lost_lines = await self._grant_drops(player, snapshot)

        lines = [
            f"⚔️ 通缉令战斗 · {snapshot.get('name', active['bounty_name'])}",
            "━━━━━━━━━━━━━━━",
            f"结果：{'✅ 缉拿成功' if win else '❌ 挑战失败'}",
            f"境界：{snapshot.get('realm_name', '未知')} · "
            f"{snapshot.get('element') or '无'}属性 · {snapshot.get('danger', '未知')}",
            f"战斗回合：{rounds}",
            f"你的剩余气血：{player_hp:,} | 剩余灵气：{player_mp:,}",
            f"目标剩余气血：{enemy_hp:,} / {enemy_max_hp:,}",
            f"今日剩余挑战次数：{remaining}/{self.MAX_DAILY_ATTEMPTS}",
        ]

        combat_log = result.get("combat_log") or []
        if combat_log:
            lines.append("")
            lines.append("📜 战斗详情：")
            lines.extend(str(line) for line in combat_log)

        lines.append("━━━━━━━━━━━━━━━")
        if win:
            lines.extend(
                [
                    "🎁 悬赏结算",
                    f"  💰 灵石 +{stone:,}",
                    f"  🧘 修为 +{exp:,}",
                ]
            )
            if drop_lines:
                lines.append("📦 获得物品：")
                lines.extend(f"  · {item}" for item in drop_lines)
            if lost_lines:
                lines.append("⚠️ 以下物品因储物戒已满未能发放：")
                lines.extend(f"  · {item}" for item in lost_lines)
            lines.append(f"📜 通缉令【{snapshot.get('name', active['bounty_name'])}】已结案。")
            lines.append("💡 可继续 /悬赏令 选择新的挑战目标。")
        else:
            if remaining <= 0:
                lines.append("💤 今日挑战次数已用尽，明日再来雪耻。")
            else:
                lines.append("💡 可发送 /挑战悬赏 再次挑战，或 /放弃悬赏 缴纳罚款更换目标。")

        return win, "\n".join(lines)

    async def _roll_bounty_items(
        self, player: Player, table_name: str
    ) -> List[Tuple[str, int]]:
        """兼容旧接口：按旧悬赏配置表抽取一次物品。"""
        dropped_items: List[Tuple[str, int]] = []
        drop_table = self.item_tables.get(table_name, self.item_tables.get("gather", []))
        if not drop_table or random.randint(1, 100) > 70:
            return dropped_items
        total_weight = sum(item["weight"] for item in drop_table)
        roll = random.randint(1, total_weight)
        upto = 0
        chosen = drop_table[0]
        for item in drop_table:
            upto += item["weight"]
            if roll <= upto:
                chosen = item
                break
        count = random.randint(chosen["min"], chosen["max"])
        dropped_items.append((chosen["name"], count))
        return dropped_items

    async def add_bounty_progress(
        self, player: Player, activity_tag: str, count: int = 1
    ) -> Tuple[bool, str]:
        """兼容旧调用：战斗挑战制不再由历练/秘境等日常行为推进进度。"""
        return False, ""

    async def check_and_expire_bounties(self) -> int:
        now = int(time.time())
        cursor = await self.db.conn.execute(
            "UPDATE bounty_tasks SET status = 3 WHERE status = 1 AND expire_time < ?",
            (now,),
        )
        await self.db.conn.commit()
        return cursor.rowcount
