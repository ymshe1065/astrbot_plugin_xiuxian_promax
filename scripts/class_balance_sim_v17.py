"""职业平衡模拟 v3：按报告调整基础系数、被动和 PvP 回合数。"""
from __future__ import annotations

import importlib.util
import json
import random
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "combat_manager_v3", ROOT / "managers" / "combat_manager.py"
)
combat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(combat)
CM = combat.CombatManager
CombatStats = combat.CombatStats

CLASSES = ["灵修", "体修", "剑修", "魔修", "佛修", "鬼修", "御兽"]
# v17：鬼修小降攻击，御兽基础防御补回 0.02
FACTORS = {
    "灵修": (1.02, 1.00, 0.20),
    "体修": (1.02, 1.08, 0.90),
    "剑修": (1.05, 0.85, 0.95),
    "魔修": (1.03, 0.90, 0.50),
    "佛修": (1.02, 1.15, 0.50),
    "鬼修": (1.04, 0.88, 0.20),
    "御兽": (1.00, 1.00, 0.50),
}
PASSIVES = {
    "灵修": {"mp_mult": 1.08, "magic_mult": 1.04},
    "体修": {"hp_mult": 1.06, "def_mult": 1.04},
    "剑修": {"crit": 2.0, "phys_mult": 1.03},
    "魔修": {"lifesteal": 4.0, "phys_mult": 1.02, "magic_mult": 1.02},
    "佛修": {"hp_mult": 1.03, "def_mult": 1.06},
    # 鬼修低防，靠咒印法伤/穿透，不用被动补防御
    "鬼修": {"magic_mult": 1.04, "armor_pen": 3.0},
    "御兽": {"phys_mult": 1.04, "magic_mult": 1.04, "def_mult": 1.02},
}


def level_scale(level_index: int = 18) -> int:
    levels = json.loads((ROOT / "config" / "level_config.json").read_text(encoding="utf-8"))
    level_index = max(0, min(level_index, len(levels) - 1))
    threshold = max(0, levels[level_index].get("exp_needed", 0))
    growth = sum(
        x.get("breakthrough_physical_damage_gain", 0)
        + x.get("breakthrough_magic_damage_gain", 0)
        for x in levels[: level_index + 1]
    )
    return max(160, 155 + growth + threshold // 10)


def make_stats(cls: str, level_index: int = 18, passive: bool = True) -> CombatStats:
    scale = level_scale(level_index)
    attack_factor, defense_factor, physical_share = FACTORS[cls]
    attack = int(scale * attack_factor)
    defense = int(scale * 0.35 * defense_factor)
    physical_atk = int(attack * physical_share)
    magic_atk = attack - physical_atk
    physical_def = defense // 2
    magic_def = defense - physical_def
    max_hp = scale * 8
    max_mp = scale * 10
    effects = {
        "crit_rate": 0.0, "crit_damage": 1.5, "lifesteal": 0.0,
        "counter_damage": 0.0, "armor_penetration": 0.0,
        "physical_damage_multiplier": 1.0, "magic_damage_multiplier": 1.0,
    }
    if passive:
        p = PASSIVES[cls]
        max_mp = int(max_mp * p.get("mp_mult", 1.0))
        max_hp = int(max_hp * p.get("hp_mult", 1.0))
        physical_def = int(physical_def * p.get("def_mult", 1.0))
        magic_def = int(magic_def * p.get("def_mult", 1.0))
        physical_atk = int(physical_atk * p.get("phys_mult", 1.0))
        magic_atk = int(magic_atk * p.get("magic_mult", 1.0))
        effects["crit_rate"] += p.get("crit", 0.0)
        effects["lifesteal"] += p.get("lifesteal", 0.0)
        effects["armor_penetration"] += p.get("armor_pen", 0.0)
        effects["physical_damage_multiplier"] *= p.get("phys_mult", 1.0)
        effects["magic_damage_multiplier"] *= p.get("magic_mult", 1.0)
    return CombatStats(
        user_id=cls, name=cls, hp=max_hp, max_hp=max_hp, mp=max_mp, max_mp=max_mp,
        atk=physical_atk + magic_atk, defense=physical_def + magic_def,
        physical_atk=physical_atk, magic_atk=magic_atk,
        physical_defense=physical_def, magic_defense=magic_def,
        defense_scale=scale, effects=effects,
    )


def duel(p1: CombatStats, p2: CombatStats, max_rounds: int = 10) -> str:
    """与 player_vs_player 同口径，但允许配置回合数；不处理反伤。"""
    rounds = 0
    while p1.hp > 0 and p2.hp > 0 and rounds < max_rounds:
        rounds += 1
        dmg, _, heal = CM._take_turn(p1, p2, pvp=True)
        p2.hp = max(0, p2.hp - dmg)
        p1.hp = min(p1.max_hp, p1.hp + heal)
        if p1.hp <= 0 or p2.hp <= 0:
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
    r1 = p1.hp / max(1, p1.max_hp)
    r2 = p2.hp / max(1, p2.max_hp)
    if r1 > r2:
        return p1.user_id
    if r2 > r1:
        return p2.user_id
    return "平局"


def pvp_matrix(fights: int = 300, rounds: int = 10, passive: bool = True):
    random.seed(20261031)
    result = {}
    for a in CLASSES:
        result[a] = {}
        for b in CLASSES:
            wins = 0.0
            for i in range(fights):
                # 交换先后手，消除“先手方固定”的偏差。
                if i % 2:
                    p1, p2 = make_stats(b, passive=passive), make_stats(a, passive=passive)
                else:
                    p1, p2 = make_stats(a, passive=passive), make_stats(b, passive=passive)
                winner = duel(p1, p2, rounds)
                if winner == a:
                    wins += 1
                elif winner == "平局":
                    wins += 0.5
            result[a][b] = wins / fights
    return result


def pve_damage(cls: str, rounds: int = 60, passive: bool = True):
    scale = level_scale(18)
    p = make_stats(cls, passive=passive)
    dummy = CombatStats(
        user_id="dummy", name="木桩", hp=10**15, max_hp=10**15,
        mp=0, max_mp=0, atk=0, defense=0,
        physical_atk=0, magic_atk=0,
        physical_defense=int(scale * 0.35), magic_defense=int(scale * 0.35),
        defense_scale=scale, effects={"crit_damage": 1.5},
    )
    start = dummy.hp
    result = CM.player_vs_boss(p, dummy)
    dealt = start - result["boss_final_hp"]
    dps = dealt / max(1, result["rounds"])
    sample = scale
    p_reduce = 1 - CM.apply_damage_reduction(sample, p.physical_defense, p.defense_scale) / sample
    m_reduce = 1 - CM.apply_damage_reduction(sample, p.magic_defense, p.defense_scale) / sample
    ehp = p.max_hp / max(0.01, 1 - (p_reduce + m_reduce) / 2)
    return dps, ehp


def main():
    report = {}
    for passive in (False, True):
        label = "v17被动" if passive else "v17基础"
        matrix = pvp_matrix(fights=300, rounds=10, passive=passive)
        overall = {
            a: statistics.mean(matrix[a][b] for b in CLASSES if b != a)
            for a in CLASSES
        }
        rows = []
        for a in CLASSES:
            dps, ehp = pve_damage(a, passive=passive)
            rows.append((a, dps, ehp))
        max_dps = max(x[1] for x in rows)
        max_ehp = max(x[2] for x in rows)
        report[label] = {
            "pvp_winrate": overall,
            "pve": [
                {"class": a, "dps": dps, "ehp": ehp,
                 "dps_vs_max": dps / max_dps - 1, "ehp_vs_max": ehp / max_ehp - 1}
                for a, dps, ehp in rows
            ],
        }
        print(f"\n=== {label} · 10回合 PvP ===")
        for a, v in sorted(overall.items(), key=lambda kv: -kv[1]):
            print(f"{a}: {v * 100:.1f}%")
        print(f"=== {label} · PvE ===")
        for a, dps, ehp in sorted(rows, key=lambda x: -x[1]):
            print(f"{a}: DPS {dps:,.0f} ({(dps / max_dps - 1) * 100:+.1f}%) "
                  f"EHP {ehp:,.0f} ({(ehp / max_ehp - 1) * 100:+.1f}%)")
    (ROOT / "scripts" / "class_balance_report_v17.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()













