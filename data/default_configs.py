# data/default_configs.py

SECT_CONFIG = {
    "create_cost": 10000,
    "create_level_required": 10, # 筑基
}

BOSS_CONFIG = {
    "spawn_interval": 3600,
    "world_boss": {
        "min_level": 3,
        "cooldown": 300,
        "damage_floor_pct": 0.5,
        "damage_cap_pct": 20.0,
        "expire_hours": 24,
    },
    "raid": {
        "enabled": True,
        "auto_spawn_interval": 21600,
        "expire_hours": 48,
        "cooldown": 600,
        "damage_floor_pct": 0.3,
        "damage_cap_pct": 8.0,
        "min_players": 2,
    },
    "small_event": {
        "enabled": True,
        "interval_min_hours": 3,
        "interval_max_hours": 4,
        "active_start_hour": 10,
        "active_end_hour": 24,
        "timezone": "Asia/Shanghai",
        "cooldown": 180,
        "damage_floor_pct": 1.0,
        "damage_cap_pct": 12.0,
        "expire_hours": 2,
        "escape_reward_pct": 0.5,
    },
    "weekly_boss": {
        "enabled": True,
        "refresh_weekday": 1,
        "refresh_hour": 0,
        "refresh_minute": 5,
        "duration_days": 7,
        "entry_close_minutes": 5,
        "min_level": 13,
        "max_players": 8,
        "attempts_per_week": 3,
        "baseline_mode": "server_p75",
        "baseline_level": 22,
        "baseline_atk_scale": 1.39,
        "baseline_def_scale": 1.30,
        "hp_unit": 40.0,
        "hp_exponent": 0.65,
        "solo_hp_mult": 1.3,
        "solo_damage_mult": 1.25,
        "soft_enrage_round": 16,
        "hard_enrage_round": 20,
        "soft_enrage_mult": 1.5,
        "normalize_ratio": 0.15,
        "normalize_min": 0.85,
        "normalize_max": 1.15,
        "prepare_timeout_seconds": 7200,
        "confirm_timeout_seconds": 90,
        "pill_slots_prepare": 6,
        "pill_slots_combat": 6,
        "pill_buff_caps": {
            "attack": 0.6,
            "defense": 0.6,
            "crit_rate": 30,
            "crit_damage": 0.6,
            "lifesteal": 20,
            "damage_reduction": 0.3,
        },
        "rewards": {
            "stone_pool": 2000000,
            "legend_rolls_base": 2,
            "legend_rolls_share": 8,
            "equipment_rolls_base": 1,
            "exclusive_material": "天外陨核",
        },
    },
}

RIFT_CONFIG = {
    "default_duration": 1800, # 30分钟
    # 装备品阶基准 = min(玩家等级, 秘境门槛 + 该余量)：低门槛本不掉当前阶装备
    "equipment_level_cap_bonus": 2,
    "equipment_drop_chances": {"1": 6, "2": 9, "3": 12, "4": 12, "5": 12},
    "equipment_up_rank_weight": {"1": 1.0, "2": 0.7, "3": 0.5, "4": 0.35, "5": 0.25},
    "equipment_slot_weights": {
        "weapon": 16, "armor": 16, "head": 12, "boots": 12,
        "amulet": 12, "gloves": 16, "ring": 16,
    },
    "rifts": [],
}

# 秘境清单（15 处 × 3 难度）体量大且需要与掉落表严格对应，直接以随包下发的
# config/rift_config.json 为准，避免在这里维护一份会走样的副本。
# 老存档的 rift_config.json 没有 rifts 键时，RiftManager 就靠这里补全。
try:
    import json as _json
    import os as _os

    _RIFT_FILE = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "config", "rift_config.json",
    )
    if _os.path.exists(_RIFT_FILE):
        with open(_RIFT_FILE, "r", encoding="utf-8") as _f:
            _bundled_rifts = _json.load(_f).get("rifts")
        if _bundled_rifts:
            RIFT_CONFIG["rifts"] = _bundled_rifts
except Exception:
    pass

ALCHEMY_CONFIG = {
    "recipes": {
        "1": {
            "name": "聚气丹",
            "level_required": 0,
            "materials": {"灵草": 3, "灵石": 100},
            "success_rate": 80,
            "effect": {"type": "exp", "value": 1000},
            "desc": "增加1000修为"
        },
        "2": {
            "name": "筑基丹",
            "level_required": 2,
            "materials": {"灵草": 5, "灵石": 500},
            "success_rate": 60,
            "effect": {"type": "exp", "value": 5000},
            "desc": "增加5000修为"
        },
        "3": {
            "name": "金丹",
            "level_required": 5,
            "materials": {"灵草": 10, "灵石": 2000},
            "success_rate": 40,
            "effect": {"type": "exp", "value": 20000},
            "desc": "增加20000修为"
        },
        "4": {
            "name": "回春丹",
            "level_required": 1,
            "materials": {"灵草": 2, "灵石": 200},
            "success_rate": 70,
            "effect": {"type": "hp_restore", "value": 50},
            "desc": "恢复50%气血"
        },
        "5": {
            "name": "聚灵丹",
            "level_required": 1,
            "materials": {"灵草": 2, "灵石": 200},
            "success_rate": 70,
            "effect": {"type": "mp_restore", "value": 50},
            "desc": "恢复50%真元"
        },
    }
}

AUCTION_HOUSE_CONFIG = {
    # 拍品品阶曲线固定：凡品 → 混元先天，越靠后越稀有；
    # 不使用玩家或全服平均境界做动态缩放。
    "lot_blueprint": [
        {"category": "equipment", "slot": "gloves", "rank": "凡品", "tier": 1, "count": 1,
         "quality_weights": {"一般": 85, "良好": 15, "极佳": 0, "大师": 0}},
        {"category": "talisman", "rank": "黄品", "tier": 2, "count": 2},
        {"category": "material", "rank": "玄品", "tier": 3, "count": 2},
        {"category": "equipment", "slot": "ring", "rank": "灵品", "tier": 3, "count": 1,
         "quality_weights": {"一般": 45, "良好": 45, "极佳": 10, "大师": 0}},
        {"category": "equipment", "slot": "body", "rank": "地品", "tier": 3, "count": 1,
         "quality_weights": {"一般": 20, "良好": 55, "极佳": 24, "大师": 1}},
        {"category": "equipment", "slot": "boots", "rank": "天品", "tier": 4, "count": 1,
         "quality_weights": {"一般": 0, "良好": 45, "极佳": 45, "大师": 10}},
        {"category": "equipment", "slot": "amulet", "rank": "皇品", "tier": 4, "count": 1,
         "quality_weights": {"一般": 0, "良好": 25, "极佳": 55, "大师": 20}},
        {"category": "main_technique", "rank": "圣品", "tier": 4, "count": 1},
        {"category": "equipment", "slot": "head", "rank": "仙品", "tier": 5, "count": 1,
         "quality_weights": {"一般": 0, "良好": 5, "极佳": 45, "大师": 50}},
        {"category": "equipment", "slot": "ring", "rank": "混元先天", "tier": 5, "count": 1,
         "quality_weights": {"一般": 0, "良好": 0, "极佳": 30, "大师": 70}},
    ],
    "start_price_rate": 1.0,
    "start_price_fluctuation": 0.0,
    "affix_counts": {"一般": 0, "良好": 1, "极佳": 2, "大师": 3},
    "guaranteed_categories": ["main_technique"],
    "space_stone_name": "空间石",
    "space_stone_count": 2,
    "space_stone_chance": 0.8,
    "space_stone_extra_chance": 0.5,
}
