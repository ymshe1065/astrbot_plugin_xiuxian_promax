# data/default_configs.py

SECT_CONFIG = {
    "create_cost": 10000,
    "create_level_required": 10, # 筑基
}

BOSS_CONFIG = {
    "spawn_interval": 3600,
    "levels": [
        {"name": "练气", "level_index": 0, "hp_mult": 1.0, "atk_mult": 1.0, "reward_mult": 1.0},
        {"name": "筑基", "level_index": 10, "hp_mult": 1.5, "atk_mult": 1.2, "reward_mult": 1.5},
        {"name": "金丹", "level_index": 13, "hp_mult": 2.0, "atk_mult": 1.5, "reward_mult": 2.0},
        {"name": "元婴", "level_index": 16, "hp_mult": 2.5, "atk_mult": 1.8, "reward_mult": 2.5},
        {"name": "化神", "level_index": 19, "hp_mult": 3.0, "atk_mult": 2.0, "reward_mult": 3.0},
        {"name": "炼虚", "level_index": 22, "hp_mult": 4.0, "atk_mult": 2.5, "reward_mult": 4.0},
        {"name": "合体", "level_index": 25, "hp_mult": 5.0, "atk_mult": 3.0, "reward_mult": 5.0},
        {"name": "大乘", "level_index": 28, "hp_mult": 6.0, "atk_mult": 3.5, "reward_mult": 6.0},
    ],
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
}

RIFT_CONFIG = {
    "default_duration": 1800, # 30分钟
    "equipment_drop_chances": {"1": 6, "2": 10, "3": 15, "4": 18, "5": 22},
    "equipment_slot_weights": {
        "weapon": 16, "armor": 16, "head": 12, "boots": 12,
        "amulet": 12, "gloves": 16, "ring": 16,
    },
    "rifts": [
        {"id": 1, "name": "青云秘境", "level": 2, "exp_range": [100, 500], "gold_range": [50, 200]},
        {"id": 2, "name": "幽冥鬼域", "level": 5, "exp_range": [500, 2000], "gold_range": [200, 800]},
        {"id": 3, "name": "太古遗迹", "level": 10, "exp_range": [5000, 10000], "gold_range": [1000, 5000]},
    ]
}

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
    "lot_blueprint": [
        {"category": "equipment", "slot": "gloves", "tier": 2, "count": 1,
         "quality_weights": {"一般": 30, "良好": 50, "极佳": 20, "大师": 0}},
        {"category": "talisman", "tier": 2, "count": 2},
        {"category": "technique", "tier": 2, "count": 1},
        {"category": "equipment", "slot": "ring", "tier": 3, "count": 1,
         "quality_weights": {"一般": 0, "良好": 35, "极佳": 50, "大师": 15}},
        {"category": "pill", "tier": 3, "count": 1,
         "quality_weights": {"一般": 0, "良好": 45, "极佳": 45, "大师": 10}},
        {"category": "recipe", "tier": 3, "count": 1},
        {"category": "talisman", "tier": 3, "count": 2},
        {"category": "main_technique", "tier": 4, "count": 1},
        {"category": "equipment", "tier": 4, "count": 1,
         "quality_weights": {"一般": 0, "良好": 10, "极佳": 60, "大师": 30}},
        {"category": "pill", "tier": 5, "count": 1,
         "quality_weights": {"一般": 0, "良好": 0, "极佳": 45, "大师": 55}},
    ],
    "start_price_rate": 0.2,
    "start_price_fluctuation": 0.2,
    "affix_counts": {"一般": 0, "良好": 1, "极佳": 2, "大师": 3},
    "guaranteed_categories": ["main_technique"],
    "space_stone_name": "空间石",
    "space_stone_count": 2,
    "space_stone_chance": 0.8,
    "space_stone_extra_chance": 0.5,
}
