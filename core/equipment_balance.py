"""装备与境界共用的数值规则。百分比一律存小数，概率特效存百分点。"""
from .item_rules import ItemRules

QUALITY = {"一般": 1.0, "良好": 1.05, "极佳": 1.10, "大师": 1.15}
# 与 ItemRules.RANK_REQ_LEVEL 保持同一张解锁表，避免两套品阶各自推算。
RANK_LEVEL = ItemRules.RANK_REQ_LEVEL
SLOT_BUDGET = {"weapon": (.18, 0), "head": (0, .06), "body": (0, .12),
               "gloves": (.06, 0), "boots": (0, .04), "ring1": (.04, 0), "amulet": (.02, .04)}
ATTRIBUTE_LABELS = {"physical_damage": "物伤", "magic_damage": "法伤", "physical_defense": "物防",
                    "magic_defense": "法防", "mental_power": "精神力"}
RATE_CAPS = {"physical_damage": .40, "magic_damage": .40, "physical_defense": .35,
             "magic_defense": .35, "mental_power": .30}

# 装备特效上限：每件装备单独计算后再汇总，避免单件堆满。
GEAR_EFFECT_CAPS = {
    "crit_rate": 25,
    "lifesteal": 30,
    "counter_damage": 20,
    "combo_rate": 20,
    "dodge": 5,
    "crit_damage": .40,
    "physical_damage_multiplier": .10,
    "magic_damage_multiplier": .10,
    "defense_bonus": 0,
    "extra_attack_chance": 30,
    "extra_attack_ratio": 80,
    "extra_attack_count": 2,
    "extra_attack_second_ratio": 100,
    "extra_attack_can_crit": 1,
    "extra_attack_can_proc": 1,
    "armor_penetration": 30,
    "sunder_chance": 40,
    "sunder_pct": 15,
    "sunder_turns": 5,
    "crit_chain_chance": 30,
    "crit_chain_ratio": 60,
    "crit_chain_can_crit": 1,
    "crit_defense_ignore": 30,
    "combo_extra_hit_ratio": 60,
    "combo_can_crit": 1,
    "combo_lifesteal_pct": 30,
    "stun_chance": 20,
    "stun_turns": 2,
    "stun_extend_chance": 30,
    "stun_resist_pierce": 60,
    "burn_chance": 35,
    "burn_turns": 5,
    "burn_damage_pct": 10,
    "burn_heal_reduction": 50,
    "burn_detonate": 1,
    "frost_chance": 30,
    "frost_atk_reduction": 30,
    "frost_turns": 5,
    "frost_dodge_reduction": 10,
    "freeze_chance": 40,
    "curse_chance": 30,
    "curse_defense_pct": 25,
    "curse_turns": 5,
    "curse_damage_reduction": 30,
    "status_resist": 50,
}

GLOBAL_EFFECT_CAPS = {
    "crit_rate": 100,
    "lifesteal": 100,
    "counter_damage": 100,
    "combo_rate": 30,
    "dodge": 20,
    "crit_damage": 3.5,
    "physical_damage_multiplier": 1.5,
    "magic_damage_multiplier": 1.5,
    "extra_attack_chance": 50,
    "extra_attack_ratio": 100,
    "extra_attack_count": 2,
    "extra_attack_second_ratio": 100,
    "extra_attack_can_crit": 1,
    "extra_attack_can_proc": 1,
    "armor_penetration": 50,
    "sunder_chance": 60,
    "sunder_pct": 25,
    "sunder_turns": 5,
    "crit_chain_chance": 40,
    "crit_chain_ratio": 100,
    "crit_chain_can_crit": 1,
    "crit_defense_ignore": 50,
    "combo_extra_hit_ratio": 80,
    "combo_can_crit": 1,
    "combo_lifesteal_pct": 50,
    "stun_chance": 30,
    "stun_turns": 2,
    "stun_extend_chance": 50,
    "stun_resist_pierce": 80,
    "burn_chance": 50,
    "burn_turns": 5,
    "burn_damage_pct": 15,
    "burn_heal_reduction": 80,
    "burn_detonate": 1,
    "frost_chance": 40,
    "frost_atk_reduction": 50,
    "frost_turns": 5,
    "frost_dodge_reduction": 20,
    "freeze_chance": 50,
    "curse_chance": 40,
    "curse_defense_pct": 35,
    "curse_turns": 5,
    "curse_damage_reduction": 50,
    "status_resist": 80,
}

# 效果品质缩放：回合、段数与布尔里程碑不缩放。
_EFFECT_INT_KEYS = {
    "lifesteal", "crit_rate", "combo_rate", "counter_damage", "dodge", "status_resist",
}
_EFFECT_BOOL_KEYS = {
    "extra_attack_can_crit", "extra_attack_can_proc", "burn_detonate",
    "crit_chain_can_crit", "combo_can_crit",
}
_EFFECT_TURN_KEYS = {
    "extra_attack_count", "stun_turns", "burn_turns", "frost_turns", "curse_turns", "sunder_turns",
}


def effective_effects(config, quality: str = "一般") -> dict:
    """返回装备效果；仅对显式开启 effect_quality_scaling 的装备应用品质倍率。"""
    if not isinstance(config, dict):
        return {}
    raw = config.get("effects")
    if not isinstance(raw, dict):
        return {}
    try:
        mult = float(QUALITY.get(quality or "一般", 1.0))
    except (TypeError, ValueError):
        mult = 1.0
    scaling = bool(config.get("effect_quality_scaling"))
    result = {}
    for key, value in raw.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if scaling and key not in _EFFECT_TURN_KEYS and key not in _EFFECT_BOOL_KEYS:
            if key == "crit_damage":
                number = round(number * mult, 3)
            elif key in _EFFECT_INT_KEYS:
                number = int(round(number * mult))
            else:
                number = round(number * mult, 1)
        else:
            number = int(number) if key in _EFFECT_TURN_KEYS or key in _EFFECT_BOOL_KEYS else number
        result[key] = number
    return result


def realm_attributes(player, config_manager=None):
    """固定境界模板，不随囤积修为/穿戴装备/临时状态变化。

    沿用境界配置的修为门槛作为尺度，不使用玩家实际修为。
    职业只影响属性分配与小幅攻防差异。基础零防职业也有最低防御。
    """
    levels = getattr(config_manager, "level_data", None) or []
    level = max(0, int(player.level_index))
    if levels:
        level = min(level, len(levels) - 1)
        threshold = max(0, levels[level].get("exp_needed", 0))
        growth = sum(x.get("breakthrough_physical_damage_gain", 0) + x.get("breakthrough_magic_damage_gain", 0)
                     for x in levels[:level + 1])
        scale = max(160, 155 + growth + threshold // 10)
    else:
        scale = int(160 * 1.25 ** min(level, 35))
    attack_factor, defense_factor, physical_share = {
        "灵修": (1.0, 1.0, .2), "体修": (.95, 1.25, .9), "剑修": (1.15, .85, .95),
        "魔修": (1.10, .9, .5), "佛修": (.85, 1.35, .5), "鬼修": (1.0, .9, .2),
        "御兽": (.95, 1.0, .5),
    }.get(player.cultivation_type, (1.0, 1.0, .2))
    attack = int(scale * attack_factor)
    defense = int(scale * .35 * defense_factor)
    return {"physical_damage": int(attack * physical_share),
            "magic_damage": attack - int(attack * physical_share),
            "physical_defense": defense // 2, "magic_defense": defense - defense // 2,
            "mental_power": max(50, scale // 5), "max_hp": scale * 8,
            "max_mp": scale * 10, "defense_scale": scale}


def equipment_rates(config):
    """显式百分比优先；旧配置也按部位预算转化，禁止固定数值绕过。"""
    if isinstance(config.get("equipment_rates"), dict):
        return {key: min(cap, max(0, float(config["equipment_rates"].get(key, 0))))
                for key, cap in RATE_CAPS.items()}
    slot = ItemRules.get_equip_slot(config)
    attack, defense = SLOT_BUDGET.get(slot, (0, 0))
    # 物伤/法伤各自基于对应境界基础，不能将两者合并后二次放大。
    return {"physical_damage": attack, "magic_damage": attack,
            "physical_defense": defense, "magic_defense": defense}


def effective_rates(config, player_level, quality="一般"):
    req = ItemRules.get_required_level(config)
    # 每落后三个境界索引衰减20%，最低保留20%，升级装备仍有意义。
    gap = max(0, int(player_level) - req) // 3
    efficiency = max(.2, .8 ** gap)
    return {key: value * QUALITY.get(quality, 1.0) * efficiency
            for key, value in equipment_rates(config).items()}


def equipment_price(config):
    """按解锁境界与部位预算定价；同门槛同预算价格一致。"""
    level = ItemRules.get_required_level(config)
    rates = equipment_rates(config)
    budget = max(rates.get("physical_damage", 0), rates.get("magic_damage", 0))
    budget += max(rates.get("physical_defense", 0), rates.get("magic_defense", 0))
    budget += rates.get("mental_power", 0) * .5
    # 初始武器约600灵石；高境界逐级增长，基础部件价格低于武器。
    return max(50, round(600 * (1.45 ** level) * budget / .18 / 10) * 10)


def special_equipment_price(reference_price: int, multiplier: float = 0.9) -> int:
    """新系装备定价：同品阶灵纹同部位价格 × 0.9，取整到 10 且不低于 100。"""
    try:
        price = float(reference_price) * float(multiplier)
    except (TypeError, ValueError):
        price = 0
    return max(100, int(round(price / 10.0) * 10))
