# data/default_configs.py

SECT_CONFIG = {
    "create_cost": 10000,
    "create_level_required": 3, # 筑基
}

BOSS_CONFIG = {
    "spawn_interval": 3600,
    "levels": [
        {"name": "练气", "level_index": 0, "hp_mult": 1.0, "atk_mult": 1.0, "reward_mult": 1.0},
        {"name": "筑基", "level_index": 3, "hp_mult": 1.5, "atk_mult": 1.2, "reward_mult": 1.5},
        {"name": "金丹", "level_index": 6, "hp_mult": 2.0, "atk_mult": 1.5, "reward_mult": 2.0},
        {"name": "元婴", "level_index": 9, "hp_mult": 2.5, "atk_mult": 1.8, "reward_mult": 2.5},
        {"name": "化神", "level_index": 12, "hp_mult": 3.0, "atk_mult": 2.0, "reward_mult": 3.0},
        {"name": "炼虚", "level_index": 15, "hp_mult": 4.0, "atk_mult": 2.5, "reward_mult": 4.0},
        {"name": "合体", "level_index": 18, "hp_mult": 5.0, "atk_mult": 3.0, "reward_mult": 5.0},
        {"name": "大乘", "level_index": 21, "hp_mult": 6.0, "atk_mult": 3.5, "reward_mult": 6.0},
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
}

RIFT_CONFIG = {
    "default_duration": 1800, # 30分钟
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
