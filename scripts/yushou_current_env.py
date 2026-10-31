"""当前线上环境的御兽强度：当前基础系数 + 指定玩家真实宠物数据。

只读 SQLite，不修改游戏数据。装备/丹药未计入。
"""
from __future__ import annotations

import importlib.util
import json
import random
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = Path(r"C:/Users/sym01/.astrbot_launcher/instances/6cdc9f28-1665-4b0b-a7d8-43eec6bcc7bd/core/data/plugin_data/astrbot_plugin_xiuxian_promax/xiuxian_data_lite.db")
spec = importlib.util.spec_from_file_location("cm_current", ROOT / "managers" / "combat_manager.py")
combat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(combat)
CM = combat.CombatManager
CombatStats = combat.CombatStats
PetCombatState = combat.PetCombatState

YUSHOU_USER = "1610049038"
CURRENT_FACTORS = {
    "灵修": (1.02, 1.00, 0.20), "体修": (1.02, 1.08, 0.90),
    "剑修": (1.05, 0.85, 0.95), "魔修": (1.03, 0.90, 0.50),
    "佛修": (1.02, 1.15, 0.50), "鬼修": (1.04, 0.88, 0.20),
    "御兽": (1.06, 1.05, 0.50),
}
CLASS_PASSIVES = {
    "灵修": {"magic": 1.04}, "体修": {"hp": 1.06, "def": 1.04},
    "剑修": {"crit": 2.0, "phys": 1.03},
    "魔修": {"lifesteal": 4.0, "phys": 1.02, "magic": 1.02},
    "佛修": {"hp": 1.03, "def": 1.06},
    "鬼修": {"magic": 1.04, "pen": 3.0},
    "御兽": {},
}
CLASSES = list(CURRENT_FACTORS)
# 当前线上宠物类型 -> (基础类型, 进化倍率)
PET_TYPE = {
    "太古青龙": ("青龙幼崽", 2.2, "dragon", 0.14, 0.10),
    "吞金貔貅": ("噬金鼠", 2.2, "gold", 0.10, 0.04),
    "冰川巨兽": ("冰晶兽", 2.2, "shield", 0.08, 0.15),
    "炎灵狐": ("火灵狐", 1.6, "burn", 0.16, 0.05),
    "寻宝金鼠": ("噬金鼠", 1.6, "gold", 0.10, 0.04),
    "噬金鼠": ("噬金鼠", 1.0, "gold", 0.10, 0.04),
}


def level_scale(idx=18):
    levels = json.loads((ROOT / "config" / "level_config.json").read_text(encoding="utf-8"))
    threshold = levels[idx]["exp_needed"]
    growth = sum(x.get("breakthrough_physical_damage_gain", 0) + x.get("breakthrough_magic_damage_gain", 0) for x in levels[:idx + 1])
    return max(160, 155 + growth + threshold // 10)


def stats(cls):
    scale = level_scale()
    a, d, share = CURRENT_FACTORS[cls]
    atk = int(scale * a)
    defense = int(scale * 0.35 * d)
    p = CLASS_PASSIVES.get(cls, {})
    hp = int(scale * 8 * p.get("hp", 1.0))
    defense = int(defense * p.get("def", 1.0))
    pa, ma = int(atk * share * p.get("phys", 1.0)), int((atk - int(atk * share)) * p.get("magic", 1.0))
    pd, md = defense // 2, defense - defense // 2
    effects = {"crit_damage": 1.5, "crit_rate": p.get("crit", 0.0),
               "lifesteal": p.get("lifesteal", 0.0), "armor_penetration": p.get("pen", 0.0)}
    return CombatStats(
        user_id=cls, name=cls, hp=hp, max_hp=hp,
        mp=scale * 10, max_mp=scale * 10, atk=pa + ma, defense=defense,
        physical_atk=pa, magic_atk=ma, physical_defense=pd, magic_defense=md,
        defense_scale=scale, effects=effects,
    )


def live_pets(user_id):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM player_pets WHERE user_id = ? ORDER BY id", (user_id,)
    )]
    con.close()
    return rows


def pet_state(row, owner):
    base, mult, mech, atk_ratio, def_ratio = PET_TYPE.get(row["pet_type"], ("火灵狐", 1.0, "burn", 0.16, 0.05))
    level = int(row["level"])
    growth = level * 1.5
    atk_base = {"青龙幼崽": 22, "噬金鼠": 14, "冰晶兽": 10, "火灵狐": 18}[base]
    atk_growth = {"青龙幼崽": 5, "噬金鼠": 4, "冰晶兽": 3, "火灵狐": 4}[base]
    def_base = {"青龙幼崽": 12, "噬金鼠": 4, "冰晶兽": 18, "火灵狐": 6}[base]
    def_growth = {"青龙幼崽": 2, "噬金鼠": 1, "冰晶兽": 3, "火灵狐": 1}[base]
    atk = int(owner.atk * atk_ratio * mult) + int(atk_base * mult + atk_growth * mult * growth)
    defense = int(owner.defense * def_ratio * mult) + int(def_base * mult + def_growth * mult * growth)
    return PetCombatState(
        user_id=YUSHOU_USER, name=row["pet_name"], pet_type=row["pet_type"], level=level,
        hp=int(row["max_hp"]), max_hp=int(row["max_hp"]),
        atk=max(1, atk), defense=max(0, defense),
        guard_rate=0.12 + (0.04 if level >= 20 else 0) + (0.04 if level >= 40 else 0),
        mechanic=mech,
    )


def yushou(pet_count=5):
    hero = stats("御兽")
    all_pets = live_pets(YUSHOU_USER)
    if pet_count > 0:
        # 主战优先，按实际 5 只宠物取前 N 只
        chosen = all_pets[:pet_count]
        states = [pet_state(r, hero) for r in chosen]
        if not states:
            return hero
        # v4：多宠不再给主人加攻击；主战宠100%，辅助宠20%由战斗层处理。
        for p in states:
            p.atk = int(p.atk * 1.04)
            p.defense = int(p.defense * 1.02)
        hero.pet = states[0]
        hero.support_pets = states[1:]
    return hero


def duel(p1, p2, rounds=5):
    r = 0
    while p1.hp > 0 and p2.hp > 0 and r < rounds:
        r += 1
        dmg, _, heal = CM._take_turn(p1, p2, pvp=True)
        p2.hp = max(0, p2.hp - dmg)
        p1.hp = min(p1.max_hp, p1.hp + heal)
        if p2.hp <= 0 or p1.hp <= 0:
            break
        dmg, _, heal = CM._take_turn(p2, p1, pvp=True)
        p1.hp = max(0, p1.hp - dmg)
        p2.hp = min(p2.max_hp, p2.hp + heal)
    if p1.hp <= 0 and p2.hp <= 0:
        return "平局"
    if p2.hp <= 0:
        return p1.user_id
    if p1.hp <= 0:
        return p2.user_id
    r1, r2 = p1.hp / p1.max_hp, p2.hp / p2.max_hp
    return p1.user_id if r1 > r2 else p2.user_id if r2 > r1 else "平局"


def pvp_count(pet_count, fights=300):
    random.seed(20261031)
    wins = {}
    for other in CLASSES:
        if other == "御兽":
            continue
        w = 0.0
        for i in range(fights):
            y = yushou(pet_count)
            o = stats(other)
            if i % 2:
                result = duel(o, y)
            else:
                result = duel(y, o)
            if result == "御兽":
                w += 1
            elif result == "平局":
                w += 0.5
        wins[other] = w / fights
    return wins


def pve(pet_count):
    hero = yushou(pet_count)
    dummy = CombatStats(
        user_id="dummy", name="木桩", hp=10**15, max_hp=10**15, mp=0, max_mp=0, atk=0, defense=0,
        physical_atk=0, magic_atk=0,
        physical_defense=int(level_scale() * 0.35), magic_defense=int(level_scale() * 0.35),
        defense_scale=level_scale(), effects={"crit_damage": 1.5},
    )
    start = dummy.hp
    res = CM.player_vs_boss(hero, dummy)
    return int((start - res["boss_final_hp"]) / max(1, res["rounds"]))


if __name__ == "__main__":
    pets = live_pets(YUSHOU_USER)
    print("宠物：", [(p["pet_name"], p["level"], p["max_hp"]) for p in pets])
    for n in (0, 1, 3, 5):
        row = pvp_count(n, 150)
        avg = sum(row.values()) / len(row) if row else 0
        print(f"{n}宠 PvP平均 {avg * 100:.1f}% | " + " ".join(f"{k}:{v * 100:.0f}%" for k, v in row.items()))
        print(f"  PvE DPS {pve(n):,}")
