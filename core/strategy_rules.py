# -*- coding: utf-8 -*-
"""「仙域争霸」规则引擎（设计方案 §4~§9 的纯函数实现）。

本模块不碰数据库、不碰 asyncio：所有函数接收 dict / 数值并返回结果，
随机性一律通过外部传入的 ``random.Random`` 注入，保证同 seed 同结果
（§7.3 可复现、§20 战斗模拟确定性测试的前提）。

数据库落盘、tick 调度、玩家写操作校验在 ``managers/strategy_manager.py``。
"""
from __future__ import annotations

import math
import random
import re
from typing import Any, Iterable, Optional

#: 势力颜色白名单：只接受 #RGB / #RRGGBB，避免任意字符串被写进前端
#: ``style`` / canvas ``fillStyle`` 造成存储型 XSS。
COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def norm_color(value: Any, fallback: str = "#888888") -> str:
    """归一化势力颜色：非法值回退 ``fallback``（不抛错）。"""
    v = str(value or "").strip()
    if COLOR_RE.match(v):
        return v
    fb = str(fallback or "#888888").strip()
    return fb if COLOR_RE.match(fb) else "#888888"

# ---------------------------------------------------------------------------
# 基础常量（§4 角色系统）
# ---------------------------------------------------------------------------

#: 六维属性（§4.2）
STAT_KEYS = ("leadership", "valor", "wisdom", "admin", "charm", "luck")
STAT_NAMES = {
    "leadership": "统率", "valor": "武勇", "wisdom": "智谋",
    "admin": "政务", "charm": "魅力", "luck": "气运",
}

#: 角色状态 / 军团状态中文名（提示与战报展示，避免直接暴露英文键）
CHAR_STATUS_NAMES = {
    "idle": "空闲", "task": "执行任务", "army": "随军",
    "research": "研究中",
    "captive": "被俘", "transfer": "押运中", "wander": "在野", "dead": "陨落",
}
ARMY_STATE_NAMES = {
    "idle": "驻守", "march": "行军", "siege": "围城",
    "retreat": "撤退", "escort": "护送粮队",
}

#: 资源 / 产出中文名（钱包、国库、坊市、任务产出、事件奖励共用）。
#: 注意钱包字段是 ``recruit_token``，任务产出键是 ``token``，两者都映射募将令。
RESOURCE_NAMES = {
    "coin": "铜钱", "grain": "粮草", "iron": "精铁",
    "recruit_token": "募将令", "token": "募将令", "intel": "情报",
    "merit": "战功", "merit_season": "赛季战功", "gold": "灵石",
    "build": "城防", "train": "精锐", "heal": "疗伤", "tech": "科技点",
    "grain_move": "粮草", "loot": "掠夺库存", "plot": "计略",
}

#: 三职责（§4.1）
ROLE_GENERAL = "general"      # 武将
ROLE_STRATEGIST = "strategist"  # 军师
ROLE_OFFICIAL = "official"    # 内政官
ROLES = (ROLE_GENERAL, ROLE_STRATEGIST, ROLE_OFFICIAL)
ROLE_NAMES = {ROLE_GENERAL: "武将", ROLE_STRATEGIST: "军师", ROLE_OFFICIAL: "内政官"}

#: 七职业（§4.3，沿用主玩法命名，数值独立）
PROFESSIONS = ("sword", "body", "spirit", "demon", "buddha", "ghost", "beast")
PROFESSION_NAMES = {
    "sword": "剑修", "body": "体修", "spirit": "灵修", "demon": "魔修",
    "buddha": "佛修", "ghost": "鬼修", "beast": "御兽",
}

#: 职业绑定 S 级兵种适性（§7.5）
PROFESSION_S_APTITUDE = {
    "sword": "flying_sword", "ghost": "flying_sword", "body": "heavy_armor",
    "spirit": "spell_corps", "demon": "beast_horde", "beast": "beast_horde",
    "buddha": "sword_shield",
}

#: 五稀有度（§4.4，六维总和区间）
RARITIES = ("common", "fine", "rare", "epic", "legend")
RARITY_NAMES = {
    "common": "凡品", "fine": "良品", "rare": "珍品", "epic": "极品", "legend": "传世",
}
#: 六维总和区间（v4.0.19 大幅下调：避免极品/传世角色多项属性直接顶到 100）。
RARITY_STAT_RANGE = {
    "common": (90, 120), "fine": (125, 160), "rare": (165, 200),
    "epic": (205, 240), "legend": (245, 280),
}

#: 单项属性硬上限（生成与成长共用）。100 上限会让主属性过早封顶，
#: 后期所有名将同质化；统一压到 90，保留长期养成空间。
STAT_HARD_CAP = 90

#: 职责六维权重（生成时按权重分配总和）
ROLE_STAT_WEIGHTS = {
    ROLE_GENERAL: {"leadership": 3, "valor": 3, "wisdom": 1, "admin": 1, "charm": 1, "luck": 1},
    ROLE_STRATEGIST: {"leadership": 1, "valor": 1, "wisdom": 3, "admin": 1, "charm": 2, "luck": 2},
    ROLE_OFFICIAL: {"leadership": 1, "valor": 1, "wisdom": 1, "admin": 3, "charm": 3, "luck": 1},
}

#: 角色官职 → 带兵上限（§4.5，v5 定案：带兵上限由主将官职决定）
#: ``taishou`` 仅为兼容旧存档保留；太守现在是据点级职务，不再进角色官职表。
POSITION_TROOP_CAP = {
    "leader": 3000, "taiwei": 3000, "situ": 2500, "jijiu": 2500,
    "zhonglang": 2000, "taishou": 2000, "": 1500,
}
POSITION_NAMES = {
    "leader": "势力主", "taiwei": "太尉", "situ": "司徒", "jijiu": "军师祭酒",
    "zhonglang": "中郎将", "taishou": "太守", "": "无官职",
}

#: 据点级太守：可与其他角色官职并存，每个据点最多 1 名。
TAISHOU_NAME = "太守"
TAISHOU_TROOP_CAP = 2000

#: 角色可任免官职（势力主/副主是玩家治理身份，不在角色官职内）。
#: 太守是据点级职务，走据点抽屉单独任免，不在此列表。
APPOINTABLE_POSITIONS = ("zhonglang", "jijiu", "situ", "taiwei")

#: 官职开放档位：未结盟按本势力据点数量；结盟后按联盟成员势力合计据点数量。
#: 名额为 -1 表示“每据点 1 名”，按当前据点数量动态计算。
POSITION_UNLOCKS = (
    (1, {"zhonglang": 1}),
    (2, {"jijiu": 1}),
    (3, {"situ": 1}),
    (4, {"taiwei": 1}),
    (6, {"zhonglang": 2, "jijiu": 2}),
)

#: 角色官职效果说明（前端展示 / 帮助文案）；太守效果见据点抽屉。
POSITION_EFFECT_TEXT = {
    "taiwei": "全军攻击 +3%",
    "situ": "本势力据点产出 +5%",
    "jijiu": "计略成功率 +10%，托管 AI 智谋 +5",
    "zhonglang": "招募高品质概率 +10%",
}


def position_unlock_nodes(position: str) -> int:
    """返回某官职首次开放所需据点数量；0 表示不可任免。"""
    for need, caps in POSITION_UNLOCKS:
        if position in caps:
            return int(need)
    return 0


def available_positions(node_count: int) -> dict[str, int]:
    """按势力据点数量返回当前可任免官职与名额。"""
    count = max(0, int(node_count or 0))
    out: dict[str, int] = {}
    for need, caps in POSITION_UNLOCKS:
        if count < int(need):
            continue
        for pos, cap in caps.items():
            if int(cap) < 0:
                out[pos] = max(1, count)
            else:
                out[pos] = max(int(out.get(pos, 0)), int(cap))
    return out

#: 特技词条（§4.4，按稀有度随机；v4.9 扩充正面特性池至 14 条，统一 2 字命名）
TRAIT_POOL = (
    "fire_master", "siege_expert", "farming", "doctor",
    "swift", "intimidate", "moneybag", "ironwall",
    "frugal", "still_water", "composed", "iron_body",
    "accumulate", "good_karma",
)
TRAIT_NAMES = {
    "fire_master": "火攻", "siege_expert": "守城", "farming": "屯田",
    "doctor": "名医", "swift": "疾行", "intimidate": "威压",
    "moneybag": "敛财", "ironwall": "铁壁",
    "frugal": "节俭", "still_water": "止水", "composed": "泰然",
    "iron_body": "铁骨", "accumulate": "厚积", "good_karma": "善缘",
}
#: 特技 id → 效果说明（与 ``trait_army_mods`` / 各调用点实际数值同口径，
#: 供网页端点击徽标时展示；文案改动需同步 docs/design 表）。
TRAIT_DESC = {
    "fire_master": "火攻伤害 +20%",
    "siege_expert": "城防战防御 +15%",
    "farming": "屯田任务产出 +25%",
    "doctor": "战后伤兵回收 8%，角色伤势 -1",
    "swift": "行军速度 +15%、行军粮耗 -10%、追击捕获 +10%",
    "intimidate": "开战时敌方军团士气 -5",
    "moneybag": "布政 / 贸易任务币产出 +15%",
    "ironwall": "守城城防 +20%",
    "frugal": "行军粮耗 -10%、俸禄 -15%",
    "still_water": "免疫「混乱」状态",
    "composed": "免疫「挑衅」状态",
    "iron_body": "战后伤势 -1",
    "accumulate": "阅历获取 +10%",
    "good_karma": "招募高品质概率 +5%（每名在册善缘角色 +5%，最高 +15%）",
}


def trait_army_mods(traits: list[str] | None) -> dict[str, float | int]:
    """特技 → 军团数值修正（§4.4 八条全部生效）。

    返回键固定包含契约约定的四项，并附带三项专用效果：
    - ``grain_mult``：行军粮耗乘区（swift ×0.9）
    - ``speed_mult``：行军速度乘区（swift ×1.15）
    - ``defense_mult``：守城战力乘区（siege_expert ×1.15、ironwall ×1.20，可叠乘）
    - ``attack_mult``：攻击乘区（当前无特技提供，保留 1.0 便于调用方统一相乘）
    - ``fire_mult``：火攻伤害乘区（fire_master ×1.2）
    - ``recover_pct``：战后伤兵回收比例（doctor 0.08）
    - ``injury_reduce``：战后角色伤势额外减免（doctor 1）
    """
    owned = set(traits or [])
    mods: dict[str, float | int] = {
        "grain_mult": 1.0, "speed_mult": 1.0,
        "defense_mult": 1.0, "attack_mult": 1.0,
        "fire_mult": 1.0, "recover_pct": 0.0, "injury_reduce": 0,
        "exp_mult": 1.0, "recruit_quality_bonus": 0.0,
    }
    if "swift" in owned:              # 疾行：机动 +15%、粮耗 -10%、追击捕获 +10%
        mods["speed_mult"] = 1.15
        mods["grain_mult"] = 0.9
    if "siege_expert" in owned:       # 守城专精：城防战防御 +15%
        mods["defense_mult"] = float(mods["defense_mult"]) * 1.15
    if "ironwall" in owned:           # 铁壁：守城城防 +20%
        mods["defense_mult"] = float(mods["defense_mult"]) * 1.20
    if "fire_master" in owned:        # 火攻大师：火攻伤害 +20%
        mods["fire_mult"] = 1.2
    if "doctor" in owned:             # 名医：战后伤兵回收 8%、伤势额外 -1
        mods["recover_pct"] = 0.08
        mods["injury_reduce"] = 1
    if "frugal" in owned:             # 节俭：行军粮耗 -10%
        mods["grain_mult"] = float(mods["grain_mult"]) * 0.9
    if "iron_body" in owned:          # 铁骨：战后伤势 -1
        mods["injury_reduce"] = int(mods["injury_reduce"]) + 1
    if "accumulate" in owned:         # 厚积：阅历获取 +10%
        mods["exp_mult"] = 1.1
    if "good_karma" in owned:         # 善缘：招募高品质 +5%
        mods["recruit_quality_bonus"] = 0.05
    return mods


# ---------------------------------------------------------------------------
# 个性（personality，§4.9）：顶级金 / 负面红，与特技 traits 并列
# ---------------------------------------------------------------------------

#: 顶级个性池（传世专属，固定 1 个）
TOP_PERSONALITIES = (
    "purple_qi", "domineer", "battle_will",
    "calculating", "clear_heart", "shrink_earth",
)
TOP_PERSONALITY_NAMES = {
    "purple_qi": "紫气", "domineer": "睥睨", "battle_will": "战意",
    "calculating": "神算", "clear_heart": "道心", "shrink_earth": "缩地",
}
TOP_PERSONALITY_DESC = {
    "purple_qi": "自势力军团攻击 +5%、防御 +5%",
    "domineer": "敌方军团攻击 -4%、防御 -4%",
    "battle_will": "自部队攻击 +10%、防御 +10%",
    "calculating": "计略成功率 +15%、异常持续时间 -50%",
    "clear_heart": "免疫全部异常状态",
    "shrink_earth": "机动 +1 hex、无视合围",
}

#: 负面个性池（任何角色最多 1 条，均为可接受惩罚）
NEGATIVE_PERSONALITIES = (
    "squander", "greedy", "arrogant", "timid", "drunkard",
    "paranoid", "reckless", "dejected", "frail",
)
NEGATIVE_PERSONALITY_NAMES = {
    "squander": "挥金", "greedy": "贪财", "arrogant": "倨傲",
    "timid": "畏怯", "drunkard": "嗜酒", "paranoid": "多疑",
    "reckless": "轻敌", "dejected": "消沉", "frail": "体弱",
}
NEGATIVE_PERSONALITY_DESC = {
    "squander": "行军粮耗 +10%",
    "greedy": "任务币产出 -10%",
    "arrogant": "兵力 <30% 时攻击/防御 -8%",
    "timid": "撤退/溃逃时额外损失兵力 +10%",
    "drunkard": "断粮时士气额外 -10",
    "paranoid": "陷入异常状态时间 +1 回合",
    "reckless": "被伏击时士气额外 -10",
    "dejected": "友军溃败时自身士气 -5",
    "frail": "战后伤势 +1",
}

#: 个性 id → 中文名 / 说明（顶级 + 负面合并）
PERSONALITY_NAMES = {**TOP_PERSONALITY_NAMES, **NEGATIVE_PERSONALITY_NAMES}
PERSONALITY_DESC = {**TOP_PERSONALITY_DESC, **NEGATIVE_PERSONALITY_DESC}

#: 稀有度 → 负面个性出现概率（%）；可被 config.recruit.neg_chance_by_rarity 覆盖
NEG_CHANCE_BY_RARITY = {
    "common": 40, "fine": 30, "rare": 15, "epic": 8, "legend": 5,
}
#: 稀有度 → 正面特性数量区间（min, max）
POSITIVE_TRAITS_BY_RARITY = {
    "common": (0, 1), "fine": (0, 1), "rare": (1, 1),
    "epic": (1, 2), "legend": (2, 2),
}


def roll_traits_for_rarity(rng: random.Random, rarity: str,
                           cfg: dict | None = None) -> list[str]:
    """按稀有度随机正面特性（凡品 0~1 → 传世固定 2）。"""
    table = POSITIVE_TRAITS_BY_RARITY.get(str(rarity or "common"), (0, 1))
    if isinstance(cfg, dict):
        custom = cfg.get("positive_traits_by_rarity") or {}
        if isinstance(custom.get(rarity), (list, tuple)) and len(custom[rarity]) == 2:
            table = tuple(custom[rarity])
    lo, hi = int(table[0]), int(table[1])
    n = rng.randint(lo, hi) if hi > lo else lo
    if n <= 0:
        return []
    return rng.sample(list(TRAIT_POOL), min(n, len(TRAIT_POOL)))


def roll_personality(rng: random.Random, rarity: str,
                     cfg: dict | None = None) -> dict:
    """按稀有度生成个性 ``{top, negative}``（可为空串）。

    - 传世固定 1 个顶级个性；
    - 负面最多 1 条，出现概率随稀有度递减（40%→5%）。
    """
    rarity = str(rarity or "common")
    top = rng.choice(TOP_PERSONALITIES) if rarity == "legend" else ""
    neg_chance = float(NEG_CHANCE_BY_RARITY.get(rarity, 40))
    if isinstance(cfg, dict):
        table = cfg.get("neg_chance_by_rarity") or {}
        if rarity in table:
            neg_chance = float(table.get(rarity, neg_chance))
    negative = ""
    if rng.random() * 100 < neg_chance:
        negative = rng.choice(NEGATIVE_PERSONALITIES)
    return {"top": top, "negative": negative}


def personality_mods(personality: dict | None) -> dict:
    """个性 → 数值修正（§4.9）。未拥有项返回中性值。"""
    p = personality or {}
    top = str(p.get("top") or "")
    neg = str(p.get("negative") or "")
    mods: dict = {
        "attack_mult": 1.0, "defense_mult": 1.0,
        "grain_mult": 1.0, "task_coin_mult": 1.0,
        "exp_mult": 1.0, "injury_delta": 0,
        "move_bonus": 0, "ignore_encircle": False,
        "scheme_rate_bonus": 0.0,
        "status_round_bonus": 0, "status_round_mult": 1.0,
        "understrength_attack_mult": 1.0, "understrength_defense_mult": 1.0,
        "ambush_morale_extra": 0, "ally_rout_morale": 0,
        "retreat_loss_mult": 1.0, "starve_morale_extra": 0,
        "salary_mult": 1.0,
    }
    # 顶级个性
    if top == "purple_qi":          # 紫气：自势力攻防 +5%
        mods["attack_mult"] *= 1.05
        mods["defense_mult"] *= 1.05
    elif top == "domineer":         # 睥睨：敌方攻防 -4%（以自身攻防 +4% 比值等价）
        mods["attack_mult"] *= 1.04
        mods["defense_mult"] *= 1.04
    elif top == "battle_will":      # 战意：自部队攻防 +10%
        mods["attack_mult"] *= 1.10
        mods["defense_mult"] *= 1.10
    elif top == "calculating":      # 神算：计略 +15%、异常时长 -50%
        mods["scheme_rate_bonus"] += 15.0
        mods["status_round_mult"] *= 0.5
    elif top == "shrink_earth":     # 缩地：机动 +1、无视合围
        mods["move_bonus"] += 1
        mods["ignore_encircle"] = True
    # 负面个性
    if neg == "squander":           # 挥金：粮耗 +10%
        mods["grain_mult"] *= 1.10
    elif neg == "greedy":           # 贪财：任务币 -10%、俸禄 +20%（对标三国志14 贪婪/强欲）
        mods["task_coin_mult"] *= 0.90
        mods["salary_mult"] *= 1.20
    elif neg == "arrogant":         # 倨傲：兵力 <30% 攻防 -8%
        mods["understrength_attack_mult"] *= 0.92
        mods["understrength_defense_mult"] *= 0.92
    elif neg == "timid":            # 畏怯：撤退额外损失兵力 +10%
        mods["retreat_loss_mult"] *= 1.10
    elif neg == "drunkard":         # 嗜酒：断粮士气额外 -10
        mods["starve_morale_extra"] = 10
    elif neg == "paranoid":         # 多疑：异常时长 +1 回合
        mods["status_round_bonus"] += 1
    elif neg == "reckless":         # 轻敌：被伏击士气额外 -10
        mods["ambush_morale_extra"] = 10
    elif neg == "dejected":         # 消沉：友军溃败自身士气 -5
        mods["ally_rout_morale"] = -5
    elif neg == "frail":            # 体弱：战后伤势 +1
        mods["injury_delta"] += 1
    return mods


def char_salary_mult(char: dict | None) -> float:
    """角色个性 / 特技 → 俸禄乘区。

    - 贪财（``greedy``，负面个性）：俸禄 ×1.20（对标三国志14 强欲）；
    - 节俭（``frugal``，正面特技）：俸禄 ×0.85（对标三国志14 寡欲）。
    势力科技「仁政」的 ``salary_mult`` 另行在发薪时叠乘，两者互不替代。
    """
    char = char or {}
    mult = float(personality_mods(char.get("personality"))
                 .get("salary_mult", 1.0) or 1.0)
    if "frugal" in set(char.get("traits") or []):
        mult *= 0.85
    return max(0.0, mult)


def status_immunity(leader: dict | None) -> set[str]:
    """角色（特技 + 个性）免疫的异常状态集合。"""
    leader = leader or {}
    traits = set(leader.get("traits") or [])
    top = str((leader.get("personality") or {}).get("top") or "")
    out: set[str] = set()
    if "still_water" in traits:       # 止水：免疫混乱
        out.add("confuse")
    if "composed" in traits:          # 泰然：免疫挑衅
        out.add("taunt")
    if top == "clear_heart":          # 道心：免疫全部异常
        out.update({"confuse", "taunt", "suppress", "burn"})
    return out


def personality_recruit_bonus(chars: Iterable[dict] | None) -> float:
    """善缘角色在册时提供的招募高品质加成（每名 +5%，封顶 +15%）。"""
    n = 0
    for c in (chars or []):
        if "good_karma" in ((c or {}).get("traits") or []):
            n += 1
    return min(0.15, 0.05 * n)


def _cfg_value(cfg: dict | None, section: str, key: str, default: Any) -> Any:
    """配置读取：既支持完整 config（嵌套 section），也支持直接传 section 子块。"""
    cfg = cfg or {}
    sub = cfg.get(section)
    if isinstance(sub, dict) and key in sub:
        return sub[key]
    if key in cfg:
        return cfg[key]
    return default


def morale_task_gain(task_type: str, ticks: int, cfg: dict) -> int:
    """govem/heal 任务的民心产出（§6.2；每 tick 固定增益）。

    ``govern`` 每 tick +``governance.govern_morale_per_tick``；
    ``heal`` 每 tick +``governance.heal_morale_per_tick``；其余任务 0。
    """
    n = max(0, int(ticks or 0))
    if n <= 0:
        return 0
    if task_type == "govern":
        per = float(_cfg_value(cfg, "governance", "govern_morale_per_tick", 1.5))
    elif task_type == "heal":
        per = float(_cfg_value(cfg, "governance", "heal_morale_per_tick", 1.0))
    else:
        return 0
    return max(0, int(round(per * n)))


def riot_check(rng: random.Random, morale: int, cfg: dict) -> bool:
    """民心低于 ``governance.riot_morale`` 时按 ``riot_chance`` 判定哗变（§8.6）。"""
    threshold = int(_cfg_value(cfg, "governance", "riot_morale", 20))
    if int(morale or 0) >= threshold:
        return False
    chance = float(_cfg_value(cfg, "governance", "riot_chance", 0.05))
    return rng.random() < max(0.0, min(1.0, chance))


def overstaff_penalty(leader: dict | None, troops: int, cfg: dict) -> int:
    """超编士气惩罚（§7.5）：兵力 > 统率×20 时每 tick 扣的士气点数。

    返回 0 表示不超编；否则按 ``army.overstaff_morale_penalty``（默认 10%）
    折算为整数士气点（至少 1 点）。
    """
    if not is_overstaffed(leader, troops):
        return 0
    pct = float(_cfg_value(cfg, "army", "overstaff_morale_penalty", 0.1))
    return max(1, int(round(pct * 100)))


def bandit_raid_ready(node: dict, tick_no: int, interval: int) -> bool:
    """匪寨周期劫掠判定（§9.1）：距上次劫掠 ≥ ``interval`` tick 才可再次出手。"""
    if not node:
        return False
    interval = max(1, int(interval or 1))
    lair = node.get("lair")
    if lair is None:
        lair = node.get("lair_json")
    if isinstance(lair, str):
        try:
            import json as _json
            lair = _json.loads(lair or "{}")
        except (ValueError, TypeError):
            lair = {}
    if not isinstance(lair, dict):
        lair = {}
    last = int(lair.get("last_raid_tick", 0) or 0)
    return int(tick_no or 0) - last >= interval


#: 任务小事件（§6.2：10% 触发，收益 0.6~1.6 倍）
MINI_EVENT_CHANCE = 0.10
MINI_EVENTS = (
    ("windfall", "意外之财：途中拾得前人遗落的储物袋", 1.4, 1.6),
    ("inspiration", "灵机一动：参悟功法，事半功倍", 1.2, 1.4),
    ("delay", "山路塌方，耽搁了半日行程", 0.6, 0.8),
    ("mishap", "工具折损，进度略有倒退", 0.85, 0.95),
)


def mini_event_roll(rng: random.Random,
                    chance: float = MINI_EVENT_CHANCE) -> Optional[dict[str, Any]]:
    """任务小事件掷骰：按 ``chance``（默认 10%）返回事件或 None。

    允许调用方传入配置概率（task.mini_event_chance），避免 >10% 的配置
    被旧实现静默忽略。
    """
    try:
        p = float(chance)
    except (TypeError, ValueError):
        p = MINI_EVENT_CHANCE
    if p <= 0 or rng.random() >= min(1.0, p):
        return None
    kind, text, lo, hi = rng.choices(
        MINI_EVENTS, weights=[3, 3, 2, 2], k=1)[0]
    return {"kind": kind, "text": text, "mult": round(rng.uniform(lo, hi), 3)}


def arena_bracket(merit: int) -> str:
    """会武分档（§11）：战功 <500 新秀 / <2000 宿将 / 其余宗师。"""
    value = max(0, int(merit or 0))
    if value < 500:
        return "novice"
    if value < 2000:
        return "veteran"
    return "master"

#: 会武档位中文名（前后端与提示语统一口径）
ARENA_BRACKET_NAMES = {"novice": "新秀", "veteran": "宿将", "master": "宗师"}

# ---------------------------------------------------------------------------
# 兵种（§7.5）
# ---------------------------------------------------------------------------

TROOP_TYPES: dict[str, dict[str, Any]] = {
    "sword_shield": {
        "name": "剑盾步", "atk": 100, "def": 100, "move": 3, "range": 0,
        "recruit_coin": 50, "recruit_grain": 20, "grain_per_tick": 10,
        "skill": "盾墙", "terrain": "全能",
    },
    "heavy_armor": {
        "name": "重甲(搬山甲)", "atk": 90, "def": 160, "move": 2, "range": 0,
        "recruit_coin": 125, "recruit_grain": 50, "grain_per_tick": 25,
        "skill": "搬山阵", "terrain": "沼泽禁入",
    },
    "spell_corps": {
        "name": "法修营", "atk": 120, "def": 60, "move": 2, "range": 3,
        "recruit_coin": 100, "recruit_grain": 40, "grain_per_tick": 15,
        "skill": "咒唱", "terrain": "高地射程+1",
    },
    "flying_sword": {
        "name": "飞剑队", "atk": 130, "def": 70, "move": 5, "range": 0,
        "recruit_coin": 150, "recruit_grain": 30, "grain_per_tick": 20,
        "skill": "剑阵冲锋", "terrain": "平原机动+1",
    },
    "beast_horde": {
        "name": "兽群", "atk": 110, "def": 90, "move": 4, "range": 0,
        "recruit_coin": 175, "recruit_grain": 60, "grain_per_tick": 30,
        "skill": "兽潮", "terrain": "森林/山地不减机动",
    },
}

#: 克制环（§7.5）：飞剑队→法修营/运粮队→兽群→重甲→剑盾步→飞剑队，×1.3
COUNTER_MATRIX: dict[tuple[str, str], float] = {
    ("flying_sword", "spell_corps"): 1.3,
    ("flying_sword", "supply"): 1.5,      # 对运粮队 ×1.5
    ("spell_corps", "beast_horde"): 1.3,
    ("spell_corps", "heavy_armor"): 1.2,  # 重甲受远程伤害 ×1.2
    ("beast_horde", "heavy_armor"): 1.3,
    ("heavy_armor", "sword_shield"): 1.3,
    ("sword_shield", "flying_sword"): 1.3,
    ("flying_sword", "heavy_armor"): 0.8,  # 飞剑短板：对重甲 ×0.8
}


def counter_factor(atk_type: str, def_type: str) -> float:
    return COUNTER_MATRIX.get((atk_type, def_type), 1.0)


#: 兵种适性加成（§7.5）
APTITUDE_BONUS = {"S": 0.10, "A": 0.05, "B": 0.0, "C": -0.05}


def aptitude_bonus(char: dict | None, troop_type: str) -> float:
    """角色领该兵种的攻防加成系数（S +10% / A +5% / B 0 / C -5%）。"""
    if not char:
        return 1.0
    grade = str((char.get("aptitude") or {}).get(troop_type, "B"))
    return 1.0 + APTITUDE_BONUS.get(grade, 0.0)


def has_aptitude_skill(char: dict | None, troop_type: str) -> bool:
    """S 级适性解锁兵种专属技能（每场 1 次）。"""
    if not char:
        return False
    return str((char.get("aptitude") or {}).get(troop_type, "B")) == "S"


#: 精锐值上限（§7.5 三阶：普通 <100 ≤ 精锐 <300 ≤ 御林；999 为硬上限）
VETERAN_MAX = 999


#: 老兵精锐三阶（§7.5）
def veteran_tier(veteran: int) -> tuple[str, float, int]:
    """返回 (阶位名, 攻防加成, 士气上限)。"""
    v = int(veteran or 0)
    if v >= 300:
        return ("御林", 0.10, 120)
    if v >= 100:
        return ("精锐", 0.05, 110)
    return ("普通", 0.0, 100)


def army_morale_cap(army: dict) -> int:
    """军团士气上限（随老兵阶位：普通 100 / 精锐 110 / 御林 120）。

    v4.3.2：``veteran_tier`` 一直返回士气上限，但驻守恢复/补员/战后写回
    都写死 100，导致精锐阶位的上限从未生效；战略层统一由本函数取值。
    战场（hex）临时士气增益仍允许到 120，以便「晨钟」等临时 buff 生效。
    """
    return veteran_tier(int((army or {}).get("veteran", 0) or 0))[2]


# ---------------------------------------------------------------------------
# 角色生成与成长（§4.4）
# ---------------------------------------------------------------------------

def roll_stats(rng: random.Random, rarity: str, role: str) -> dict[str, int]:
    """按稀有度总和区间与职责权重掷六维。

    v4.0.19 改为「按权重分配 + 单项硬上限 + 溢出再分配」：
    - 主属性不会再无脑顶到 100；
    - 单项封顶后，多余点数回填到次要属性，六维更均衡；
    - 总和始终落在稀有度区间内（除非区间本身低于 6×5 的下限）。
    """
    lo, hi = RARITY_STAT_RANGE.get(rarity, RARITY_STAT_RANGE["common"])
    target = rng.randint(lo, hi)
    weights = ROLE_STAT_WEIGHTS.get(role, ROLE_STAT_WEIGHTS[ROLE_GENERAL])
    keys = list(STAT_KEYS)
    # 先按职责权重生成带抖动的分配权重，避免每次都是同一个模板
    raw = {k: max(1.0, float(weights.get(k, 1)) * rng.uniform(0.9, 1.1))
           for k in keys}
    weight_sum = sum(raw.values()) or 1.0
    stats = {k: max(5, min(STAT_HARD_CAP,
                           int(round(target * raw[k] / weight_sum))))
             for k in keys}
    # 逐点回填/削减，保证总和精确落在 target，且不越过 [5, STAT_HARD_CAP]
    diff = target - sum(stats.values())
    while diff != 0:
        if diff > 0:
            candidates = [k for k in keys if stats[k] < STAT_HARD_CAP]
            if not candidates:
                break
            pick = max(candidates, key=lambda k: (raw[k], -STAT_KEYS.index(k)))
            stats[pick] += 1
            diff -= 1
        else:
            candidates = [k for k in keys if stats[k] > 5]
            if not candidates:
                break
            pick = min(candidates, key=lambda k: (raw[k], STAT_KEYS.index(k)))
            stats[pick] -= 1
            diff += 1
    return stats


def roll_aptitude(rng: random.Random, profession: str, rarity: str) -> dict[str, str]:
    """兵种适性：职业绑定 1 项 S；随机 1~2 项 A；其余 B/C。"""
    apt = {t: "C" if rng.random() < 0.25 else "B" for t in TROOP_TYPES}
    apt[PROFESSION_S_APTITUDE[profession]] = "S"
    candidates = [t for t in TROOP_TYPES if apt[t] != "S"]
    n_a = rng.randint(1, 2)
    for t in rng.sample(candidates, min(n_a, len(candidates))):
        apt[t] = "A"
    if rarity == "legend":
        # 传世：额外随机第 2 个 S（§4.4）
        rest = [t for t in TROOP_TYPES if apt[t] != "S"]
        if rest:
            apt[rng.choice(rest)] = "S"
    return apt


def generate_character(
    rng: random.Random,
    rarity: str | None = None,
    role: str | None = None,
    profession: str | None = None,
    name: str | None = None,
    name_pools: dict | None = None,
    traits: list[str] | None = None,
    personality: dict | None = None,
    cfg: dict | None = None,
) -> dict[str, Any]:
    """生成一名角色（未落库的 dict，字段与 strategy_characters 对齐）。"""
    rarity = rarity or rng.choice(["common", "common", "fine", "fine", "rare"])
    role = role or rng.choices(
        [ROLE_GENERAL, ROLE_STRATEGIST, ROLE_OFFICIAL], weights=[4, 3, 3]
    )[0]
    profession = profession or rng.choice(PROFESSIONS)
    if name is None:
        name = roll_name(rng, name_pools or {})
    if traits is None:
        traits = roll_traits_for_rarity(rng, rarity, cfg)
    if personality is None:
        personality = roll_personality(rng, rarity, cfg)
    return {
        "name": name,
        "role": role,
        "rarity": rarity,
        "profession": profession,
        "stats": roll_stats(rng, rarity, role),
        "traits": traits,
        "personality": personality,
        "aptitude": roll_aptitude(rng, profession, rarity),
        "exp": 0,
        "level": 1,
        "loyalty": rng.randint(60, 80),
        "injury": 0,
        "stamina": 100,
        "status": "idle",
        "bonds": {},
    }


def roll_name(rng: random.Random, pools: dict) -> str:
    """名字库：姓氏池 + 名池 + 道号池（§4.8）。"""
    surnames = pools.get("surnames") or ["叶", "萧", "楚", "林", "顾"]
    givens = pools.get("given_names") or ["无痕", "惊鸿", "长风", "落尘", "青冥"]
    titles = pools.get("dao_titles") or ["真人", "散人", "剑奴", "道人"]
    pattern = rng.randint(0, 3)
    surname = rng.choice(surnames)
    given = rng.choice(givens)
    if pattern == 0:
        return f"{surname}{given}"
    if pattern == 1:
        return f"{rng.choice(titles)}·{given}"
    if pattern == 2:
        return f"{given}{rng.choice(titles)}"
    return f"{surname}{given}子"


#: 升级阅历曲线：level n → n+1 需要 exp
def exp_to_next(level: int) -> int:
    return 100 + int(level) * 50


#: 等级硬上限：达到后阅历不再累积，防止无限升级与数值膨胀。
MAX_LEVEL = 60

#: 每次升级投入的成长点（按职责权重分配到六维，单项上限 STAT_HARD_CAP）。
LEVEL_GROWTH_POINTS = 2

#: 养成上限额外加成：角色可通过任务/战斗把六维总和练到
#: 「稀有度生成上限 + 该值」，让长期培养的角色不至于被同稀有度新品直接替换。
DEFAULT_GROWTH_CAP_BONUS = 30


def stat_total(char: dict | None) -> int:
    """角色六维总和。"""
    return sum(int(v or 0) for v in ((char or {}).get("stats") or {}).values())


def stat_growth_cap(char: dict | None, growth_cap_bonus: int = 0) -> int:
    """角色六维总和的养成上限（稀有度上限 + 额外历练上限）。"""
    rarity = str(((char or {}).get("rarity") or "common"))
    cap = RARITY_STAT_RANGE.get(rarity, RARITY_STAT_RANGE["common"])[1]
    return int(cap) + max(0, int(growth_cap_bonus or 0))


def _grow_stats(char: dict, points: int, growth_cap_bonus: int = 0) -> list[str]:
    """按职责权重给角色加成长点，返回本次提升的属性名列表。"""
    stats = char.setdefault("stats", {})
    total = sum(int(v or 0) for v in stats.values())
    cap = stat_growth_cap(char, growth_cap_bonus)
    weights = ROLE_STAT_WEIGHTS.get(str(char.get("role") or ""), {})
    order = sorted(STAT_KEYS, key=lambda k: (-weights.get(k, 1), STAT_KEYS.index(k)))
    gained: list[str] = []
    for _ in range(max(0, int(points))):
        if total >= cap:
            break
        for k in order:
            # 每个升级阶段优先把点数投给不同的主属性，避免全部堆在一项
            if k in gained:
                continue
            if int(stats.get(k) or 0) >= STAT_HARD_CAP:
                continue
            stats[k] = int(stats.get(k) or 0) + 1
            total += 1
            gained.append(k)
            break
    return [STAT_NAMES.get(k, k) for k in gained]


def gain_exp(char: dict, amount: int, rarity_cap_bonus: int = 0,
             *, max_level: int = MAX_LEVEL,
             growth_points: int = LEVEL_GROWTH_POINTS) -> list[str]:
    """加阅历并升级（六维小幅成长，总和上限=稀有度上限+历练加成）。

    ``rarity_cap_bonus`` 为额外历练上限，任务/战斗消耗越多成长越高；
    达到 ``max_level`` 后阅历封顶，不再无限升级。
    """
    logs: list[str] = []
    tmods = trait_army_mods(char.get("traits") or [])
    amount = int(round(max(0, int(amount)) * float(tmods.get("exp_mult", 1.0) or 1.0)))
    char["exp"] = int(char.get("exp") or 0) + max(0, int(amount))
    level = max(1, int(char.get("level") or 1))
    max_level = max(1, int(max_level))
    while level < max_level and char["exp"] >= exp_to_next(level):
        char["exp"] -= exp_to_next(level)
        level += 1
        gained = _grow_stats(char, growth_points, rarity_cap_bonus)
        suffix = f"（{'、'.join(gained)}+1）" if gained else "（六维已至成长上限）"
        logs.append(f"{char.get('name', '角色')} 升至 {level} 级{suffix}")
    char["level"] = level
    if level >= max_level:
        char["exp"] = min(char["exp"], max(0, exp_to_next(level) - 1))
    return logs


# ---------------------------------------------------------------------------
# 伤势与死亡（§4.6）
# ---------------------------------------------------------------------------

INJURY_LIGHT = 30
INJURY_HEAVY = 60
INJURY_DYING = 70

INJURY_STATES = ("healthy", "light", "heavy", "dying", "dead")
INJURY_STATE_NAMES = {
    "healthy": "健康", "light": "轻伤", "heavy": "重伤", "dying": "濒死", "dead": "陨落",
}
#: 属性折损（轻伤 -10% / 重伤 -25%）
INJURY_STAT_PENALTY = {"healthy": 0.0, "light": 0.10, "heavy": 0.25, "dying": 1.0, "dead": 1.0}


def injury_state(injury: int) -> str:
    v = int(injury or 0)
    if v >= 100:
        return "dead"
    if v >= INJURY_DYING:
        return "dying"
    if v >= INJURY_HEAVY:
        return "heavy"
    if v >= INJURY_LIGHT:
        return "light"
    return "healthy"


def apply_injury(char: dict, amount: int) -> bool:
    """累积伤势；濒死再受任何伤 → 陨落（返回 True 表示死亡）。"""
    amount = max(0, int(amount))
    if amount == 0:
        return False
    if injury_state(char.get("injury", 0)) in ("dying", "dead"):
        char["injury"] = 100
        char["status"] = "dead"
        return True
    char["injury"] = min(99, int(char.get("injury", 0)) + amount)
    return False


def effective_stats(char: dict) -> dict[str, int]:
    """伤势折损后的六维（§4.6：轻伤 -10%、重伤 -25%、濒死不可行动）。"""
    penalty = INJURY_STAT_PENALTY.get(injury_state(char.get("injury", 0)), 0.0)
    return {k: int(v * (1 - penalty)) for k, v in (char.get("stats") or {}).items()}


def can_act(char: dict) -> bool:
    """重伤不可出征、濒死强制静养（§4.6）。"""
    return injury_state(char.get("injury", 0)) in ("healthy", "light")


def can_dispatch(char: dict) -> bool:
    """派遣任务：重伤/濒死不可。"""
    return injury_state(char.get("injury", 0)) != "dead" and char.get("status") != "captive" and can_act(char)


def recover_injury(char: dict, per_tick: int = 5) -> None:
    """自然恢复 -5/tick（医庐/佛修 -10 由调用方传参）。"""
    if injury_state(char.get("injury", 0)) == "dead":
        return
    char["injury"] = max(0, int(char.get("injury", 0)) - per_tick)


# ---------------------------------------------------------------------------
# 体力（§4.7）
# ---------------------------------------------------------------------------

STAMINA_MAX = 100
STAMINA_RECOVER = 10
STAMINA_COST = {
    "task": 20, "battle": 10, "manual": 15, "stratagem": 30, "duel": 10,
}


def spend_stamina(char: dict, kind: str, enabled: bool = True) -> bool:
    """扣体力；不足返回 False（内政官己方据点生产任务减半在调用方处理）。"""
    if not enabled:
        return True
    cost = STAMINA_COST.get(kind, 0)
    if int(char.get("stamina", 0)) < cost:
        return False
    char["stamina"] = int(char.get("stamina", 0)) - cost
    return True


def recover_stamina(char: dict, per_tick: int = STAMINA_RECOVER) -> None:
    char["stamina"] = min(STAMINA_MAX, int(char.get("stamina", 0)) + per_tick)


# ---------------------------------------------------------------------------
# 官职 / 带兵上限（§4.5 / §7.5 编成规则）
# ---------------------------------------------------------------------------

def troop_cap(position: str = "", is_taishou_outside: bool = False) -> int:
    """主将角色官职对应的基础带兵上限。

    太守已改为据点级职务：是否在辖内由 ``StrategyManager._leader_cap``
    叠加 ``TAISHOU_TROOP_CAP``，不再通过这里把官职降为 1500。
    ``is_taishou_outside`` 仅为旧调用方兼容保留，实际不生效。
    """
    return POSITION_TROOP_CAP.get(position or "", 1500)


def is_overstaffed(leader: dict | None, troops: int) -> bool:
    """超编软惩罚：兵力 > 统率×20（士气恢复 -10%、托管 AI 降档）。"""
    if not leader:
        return False
    leadership = int((leader.get("stats") or {}).get("leadership", 0))
    return troops > leadership * 20


# ---------------------------------------------------------------------------
# 任务结算（§6.2）
# ---------------------------------------------------------------------------

TASK_TYPES = (
    "govern", "farm", "mining", "trade", "seek", "scout", "build",
    "train", "hunt", "raid", "heal", "supply", "research",
)
TASK_NAMES = {
    "govern": "布政", "farm": "屯田", "mining": "采矿", "trade": "贸易",
    "seek": "寻访", "scout": "侦察", "build": "修筑", "train": "练兵",
    "hunt": "讨伐", "raid": "劫掠", "heal": "行医", "supply": "运粮",
    "research": "研究",
}
#: 任务 → 主属性（§6.2）
TASK_PRIMARY_STAT = {
    "govern": "admin", "farm": "admin", "mining": "valor", "trade": "wisdom",
    "seek": "charm", "scout": "wisdom", "build": "admin", "train": "leadership",
    "hunt": "valor", "raid": "valor", "heal": "charm", "supply": "admin",
    "research": "wisdom",
}
#: 任务 → 最佳职责（适配 1.3，其余 1.0）
TASK_BEST_ROLE = {
    "govern": ROLE_OFFICIAL, "farm": ROLE_OFFICIAL, "mining": ROLE_GENERAL,
    "trade": None,  # 内政官/军师均可
    "seek": ROLE_OFFICIAL, "scout": ROLE_STRATEGIST, "build": None,
    "train": ROLE_GENERAL, "hunt": ROLE_GENERAL, "raid": ROLE_GENERAL,
    "heal": None, "supply": ROLE_OFFICIAL, "research": ROLE_STRATEGIST,
}
#: 任务 → 产出资源
TASK_OUTPUT_RES = {
    "govern": "coin", "farm": "grain", "mining": "iron", "trade": "coin",
    "seek": "token", "scout": "intel", "build": "build", "train": "train",
    "hunt": "merit", "raid": "loot", "heal": "heal", "supply": "grain_move",
    "research": "tech",
}


def role_fit_mult(char: dict, task_type: str) -> float:
    best = TASK_BEST_ROLE.get(task_type)
    if best is None:
        return 1.15 if char.get("role") in (ROLE_OFFICIAL, ROLE_STRATEGIST) else 1.0
    return 1.3 if char.get("role") == best else 1.0


def task_node_mult(task_type: str, node_type: str) -> float:
    """据点类型系数（§6.2：仙城布政 ×1.5、谷田屯田 ×2、灵脉矿采矿 ×2…）。

    v4.5.2：遗迹改为可经营据点后，用「采矿 ×1.5 + 讨伐 ×1.4」保留辨识度
    （低于灵脉矿采矿 ×2.0，高于关隘采矿 ×1.3）。
    """
    table = {
        ("govern", "city"): 1.5,
        ("farm", "grain"): 2.0,
        ("mining", "mine"): 2.0,
        ("mining", "pass"): 1.3,
        ("mining", "ruin"): 1.5,
        ("trade", "market"): 1.3,
        ("seek", "branch"): 1.4,
        ("hunt", "ruin"): 1.4,
        ("build", "city"): 1.2,
    }
    return table.get((task_type, node_type), 1.0)


def task_yield_multiplier(
    task_type: str,
    char: dict,
    node_type: str,
    node_morale: int = 100,
    event_mult: float = 1.0,
) -> float:
    """任务收益倍率（不含随机波动）：
    (1+主属性/200) × 职责适配 × 特技 × 据点类型 × 民心系数 × 事件修正
    """
    stats = effective_stats(char)
    primary = TASK_PRIMARY_STAT.get(task_type, "admin")
    stat_val = int(stats.get(primary, 0))
    trait_mult = 1.0
    traits = char.get("traits") or []
    if task_type == "farm" and "farming" in traits:
        trait_mult += 0.25
    if task_type in ("govern", "trade") and "moneybag" in traits:
        trait_mult += 0.15
    morale_mult = 0.5 + max(0, min(100, node_morale)) / 100 * 0.5
    pmods = personality_mods(char.get("personality"))
    coin_mult = float(pmods["task_coin_mult"]) if task_type in ("govern", "trade") else 1.0
    return (
        (1 + stat_val / 200)
        * role_fit_mult(char, task_type)
        * trait_mult
        * task_node_mult(task_type, node_type)
        * morale_mult
        * event_mult
        * coin_mult
    )


def estimate_task(
    task_type: str,
    char: dict,
    base_per_tick: float,
    ticks: int,
    node_type: str,
    node_morale: int = 100,
    event_mult: float = 1.0,
) -> float:
    """任务预期收益（§6.2 去掉 ±15% 随机波动，供面板预览）。"""
    per_tick = base_per_tick * task_yield_multiplier(
        task_type, char, node_type, node_morale, event_mult)
    return per_tick * max(1, int(ticks))


def settle_task(
    rng: random.Random,
    task_type: str,
    char: dict,
    base_per_tick: float,
    ticks: int,
    node_type: str,
    node_morale: int = 100,
    event_mult: float = 1.0,
) -> float:
    """结算公式（§6.2）：
    收益 = 基础值 × (1+主属性/200) × 职责适配 × 特技 × 据点类型 × 民心系数 × 随机波动 × 事件修正
    """
    return estimate_task(
        task_type, char, base_per_tick, ticks, node_type,
        node_morale, event_mult) * rng.uniform(0.85, 1.15)


# ---------------------------------------------------------------------------
# 六边形工具（§7.3 odd-q 偏移坐标 / §7.4 ZOC）
# ---------------------------------------------------------------------------

def oddq_to_cube(q: int, r: int) -> tuple[int, int, int]:
    """odd-q 偏移坐标 → cube 坐标（奇数列下移半格）。"""
    x = q
    z = r - (q - (q & 1)) // 2
    y = -x - z
    return (x, y, z)


def hex_distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    ax, ay, az = oddq_to_cube(*a)
    bx, by, bz = oddq_to_cube(*b)
    return max(abs(ax - bx), abs(ay - by), abs(az - bz))


def hex_neighbors(q: int, r: int) -> list[tuple[int, int]]:
    """odd-q 六邻格（redblobgames 标准方向表）。"""
    if q % 2 == 0:
        dirs = [(+1, 0), (+1, -1), (0, -1), (-1, -1), (-1, 0), (0, +1)]
    else:
        dirs = [(+1, +1), (+1, 0), (0, -1), (-1, 0), (-1, +1), (0, +1)]
    return [(q + dq, r + dr) for dq, dr in dirs]


def zoc_tiles(hex_pos: tuple[int, int]) -> set[tuple[int, int]]:
    """军团的 ZOC = 相邻 6 hex（§7.4）。"""
    return set(hex_neighbors(*hex_pos))


#: 夹击 / 合围系数（§7.4）
FLANK_FACTOR = 1.15
ENCIRCLE_FACTOR = 1.3


def flank_factor(adjacent_enemies: int) -> float:
    """受击时邻格敌军数：1 → ×1.0；2 → 夹击 ×1.15；≥3 → 合围 ×1.3。"""
    n = int(adjacent_enemies)
    if n >= 3:
        return ENCIRCLE_FACTOR
    if n == 2:
        return FLANK_FACTOR
    return 1.0


# ---------------------------------------------------------------------------
# 夜袭（§7.6，现实 22:00~6:00）
# ---------------------------------------------------------------------------

def is_night(hour: int) -> bool:
    return hour >= 22 or hour < 6


# ---------------------------------------------------------------------------
# 会战自动模拟（§7.6 战斗数值框架；P0 抽象回合制，hex 手操为 P2）
# ---------------------------------------------------------------------------

#: 单轮伤亡上限（占守方现有兵力比例），防止一轮蒸发
MAX_ROUND_CASUALTY_PCT = 0.30
BASE_LETHALITY = 0.055


def _leader_stat(army: dict, key: str) -> int:
    leader = army.get("leader") or {}
    return int((leader.get("stats") or {}).get(key, 0))


def attack_power(army: dict, flank_mult: float = 1.0, night: bool = False) -> float:
    """攻击 = (兵种攻 × 兵力^0.8) × (1+武勇/150) × 士气系数 × 职业 × 适性 × 精锐 × 地形 × 克制 × 包抄。

    克制与地形在伤害结算处按目标另行乘入（与目标相关），此处计算与目标无关的部分。
    """
    troop = TROOP_TYPES.get(army.get("troop_type", "sword_shield"), TROOP_TYPES["sword_shield"])
    troops = max(1.0, float(army.get("troops", 0)))
    valor = _leader_stat(army, "valor")
    morale = float(army.get("morale", 100))
    morale_mult = 0.5 + morale / 200
    prof_mult = 1.05 if (army.get("leader") or {}).get("profession") == "sword" else 1.0
    if night and (army.get("leader") or {}).get("profession") == "demon":
        prof_mult *= 1.1  # 夜袭魔修（先手在 initiative 里体现，这里给少量攻击加成）
    apt = aptitude_bonus(army.get("leader"), army.get("troop_type", ""))
    _, vet_bonus, _ = veteran_tier(army.get("veteran", 0))
    terrain_mult = float(army.get("atk_terrain_mult", 1.0))
    position_mult = float(army.get("pos_atk_mult", 1.0))
    # 本土作战：在相邻友方据点构成的本势力/联盟领土内作战时加成
    territory_mult = float(army.get("territory_atk_mult", 1.0))
    pmods = personality_mods((army.get("leader") or {}).get("personality"))
    p_attack = float(pmods.get("attack_mult", 1.0) or 1.0)
    start_troops = int(army.get("start_troops", 0) or 0)
    if start_troops > 0 and troops < start_troops * 0.3:
        p_attack *= float(pmods.get("understrength_attack_mult", 1.0) or 1.0)
    return (
        troop["atk"] * (troops ** 0.8) * (1 + valor / 150)
        * morale_mult * prof_mult * apt * (1 + vet_bonus) * terrain_mult
        * position_mult * flank_mult * territory_mult * p_attack
    )


def defense_power(army: dict, defending_node: bool = False) -> float:
    """防御 = (兵种防 × 兵力^0.5) × (1+统率/150) × 适性 × 精锐 × 地形 × 阵型 × 城墙。"""
    troop = TROOP_TYPES.get(army.get("troop_type", "sword_shield"), TROOP_TYPES["sword_shield"])
    troops = max(1.0, float(army.get("troops", 0)))
    leadership = _leader_stat(army, "leadership")
    leader = army.get("leader") or {}
    # hex 视图（_sim_view）用 defending_node 字段标记「当前处于守城/守墙」，
    # 与自动会战的显式参数同口径；否则体修在攻城战里只吃到 ×1.1 而非 ×1.3。
    is_defending_node = bool(defending_node or army.get("defending_node"))
    apt = aptitude_bonus(leader, army.get("troop_type", ""))
    _, vet_bonus, _ = veteran_tier(army.get("veteran", 0))
    terrain_mult = float(army.get("def_terrain_mult", 1.0))
    prof_mult = 1.0
    if leader.get("profession") == "body":
        prof_mult = 1.3 if is_defending_node else 1.1  # 体修守城/阵地 ×1.3（§4.3）
    wall_mult = float(army.get("wall_mult", 1.0)) if is_defending_node else 1.0
    # 特技守城加成（§4.4）：以「是否处于守方/守城」为准，避免误加进攻方
    is_garrison = bool(is_defending_node or army.get("defending_node"))
    trait_mult = float(trait_army_mods(leader.get("traits") or [])["defense_mult"]) \
        if is_garrison else 1.0
    # 本土作战：友方领土内防守时的额外防御加成
    territory_mult = float(army.get("territory_def_mult", 1.0))
    pmods = personality_mods((leader or {}).get("personality"))
    p_def = float(pmods.get("defense_mult", 1.0) or 1.0)
    start_troops = int(army.get("start_troops", 0) or 0)
    if start_troops > 0 and troops < start_troops * 0.3:
        p_def *= float(pmods.get("understrength_defense_mult", 1.0) or 1.0)
    return (
        troop["def"] * math.sqrt(troops) * (1 + leadership / 150)
        * apt * (1 + vet_bonus) * terrain_mult * prof_mult * wall_mult
        * trait_mult * territory_mult * p_def
    )


def damage_curve(atk: float, dfn: float, defender_troops: int) -> int:
    """攻防比减伤曲线：减伤 = def/(def+atk)，软上限 60%；单轮伤亡 ≤30% 兵力。

    f(R)=2R/(1+R)（R=atk/def）单调有界：均势时 1.0、碾压时趋近 2.0，
    等价于减伤不低于 ~33%、不高于 60% 的软上限曲线。

    v4.3.0：只要攻击有效且守方仍有兵，伤亡保底 1——此前 int() 截断会让
    ≤18 兵的小队对普攻/反击/拦截完全免疫（近战永生，30 回合耗尽判守方胜）。
    """
    if atk <= 0 or defender_troops <= 0:
        return 0
    ratio = atk / max(1.0, dfn)
    f = 2 * ratio / (1 + ratio)
    casualties = defender_troops * BASE_LETHALITY * f
    dmg = int(min(casualties, defender_troops * MAX_ROUND_CASUALTY_PCT))
    return max(1, dmg)


def battle_initiative(army: dict, rng: random.Random, night: bool = False) -> float:
    """先手序 = 主将武勇 + 职业修正 + d10（§7.3）；夜袭魔修 +20（§7.6）。"""
    leader = army.get("leader") or {}
    valor = int((leader.get("stats") or {}).get("valor", 0))
    bonus = 0.0
    if night and leader.get("profession") == "demon":
        bonus += 20
    if leader.get("profession") in ("beast", "ghost"):
        bonus += 5
    # v4.3.13：军事 L4「精锐小队」先手 +5（调用方按带兵阈值写入 init_bonus）
    return valor + bonus + float(army.get("init_bonus", 0) or 0) + rng.uniform(0, 10)


def simulate_battle(
    attackers: list[dict],
    defenders: list[dict],
    rng: random.Random,
    max_rounds: int = 30,
    night: bool = False,
    defending_node: bool = False,
    city_defense: int = 0,
) -> dict[str, Any]:
    """自动会战模拟（P0 文字战报；hex 手操为 P2）。

    输入 army dict::

        {"id", "faction_id", "troop_type", "troops", "morale", "veteran",
         "leader": char|None, "siege_engine": bool, "def_terrain_mult", ...}

    返回 result dict：胜方、逐轮摘要（≤50KB 由调用方截断）、伤亡、
    溃逃名单、主将险情/被俘事件（概率判定留给调用方，这里只掷骰记录）。
    """
    atk = [dict(a) for a in attackers if a.get("troops", 0) > 0]
    dfn = [dict(a) for a in defenders if a.get("troops", 0) > 0]
    atk_start = {a["id"]: a["troops"] for a in atk}
    dfn_start = {a["id"]: a["troops"] for a in dfn}
    # 倨傲（兵力 <30%）等个性需要起始兵力基准
    for _a in atk + dfn:
        _a.setdefault("start_troops", int(_a.get("troops", 0) or 0))
    atk_total0 = sum(atk_start.values()) or 1
    dfn_total0 = sum(dfn_start.values()) or 1

    rounds: list[dict[str, Any]] = []
    routed_atk: list[Any] = []
    routed_dfn: list[Any] = []
    peril_events: list[dict[str, Any]] = []  # 兵力<20% 主将险情
    winner = None
    round_no = 0

    # 威压特技：敌方士气初始 -5（§4.4）
    for side, foe in ((atk, dfn), (dfn, atk)):
        if any("intimidate" in ((a.get("leader") or {}).get("traits") or []) for a in side):
            for a in foe:
                a["morale"] = max(0, int(a.get("morale", 100)) - 5)

    for round_no in range(1, max_rounds + 1):
        # 夹击/合围只在「同一回合内」有效：每回合开始清空上回合集火目标，
        # 否则上回合残留的 _focus 会让本回合首个攻击者凭空获得夹击系数。
        for _a in atk + dfn:
            _a.pop("_focus", None)
        order = sorted(atk + dfn, key=lambda a: -battle_initiative(a, rng, night))
        round_log: list[str] = []
        for army in order:
            if army.get("troops", 0) <= 0 or army.get("routed"):
                continue
            is_attacker = army in atk
            foes = [x for x in (dfn if is_attacker else atk)
                     if x.get("troops", 0) > 0 and not x.get("routed")]
            if not foes:
                break
            # 集火：打兵力最少的敌军（托管 AI 基础集火，§7.6）
            target = min(foes, key=lambda x: x.get("troops", 0))
            # 包抄系数：同一轮内攻击同一目标的友军数近似邻格敌军数（抽象化 §7.4）
            flank_n = sum(
                1 for x in (atk if is_attacker else dfn)
                if x is not army and not x.get("routed") and x.get("troops", 0) > 0
                and x.get("_focus") == target.get("id")
            )
            army["_focus"] = target.get("id")
            f_mult = flank_factor(min(flank_n + 1, 3))
            atk_pow = attack_power(army, f_mult, night)
            counter = counter_factor(
                army.get("troop_type", ""), target.get("troop_type", "")
            )
            dfn_pow = defense_power(target, defending_node=defending_node and target in dfn)
            dmg = damage_curve(atk_pow * counter, dfn_pow, target.get("troops", 0))
            target["troops"] = max(0, int(target.get("troops", 0)) - dmg)
            # 士气：受创按伤亡比例掉
            loss_pct = dmg / max(1, dmg + target["troops"])
            target["morale"] = max(0, int(target.get("morale", 100)) - loss_pct * 40)
            if f_mult >= ENCIRCLE_FACTOR:
                target["morale"] = max(0, target["morale"] - 5)  # 合围士气 -5
            elif f_mult >= FLANK_FACTOR:
                target["morale"] = max(0, target["morale"] - 2)  # 夹击士气 -2
            # 主将险情：兵力 <20% 被集火 10% 伤势 +15（§7.6，掷骰记录，落库在 manager）
            if target.get("troops", 0) > 0:
                start = (dfn_start if target in dfn else atk_start).get(target["id"], 1)
                if target["troops"] < start * 0.2 and rng.random() < 0.10:
                    peril_events.append({"army_id": target["id"], "injury": 15, "round": round_no})
            # 溃逃判定：士气 <30（§7.6）
            if target.get("morale", 0) < 30 and target.get("troops", 0) > 0:
                if rng.random() < (30 - target["morale"]) / 30 * 0.5 + 0.25:
                    target["routed"] = True
                    (routed_dfn if target in dfn else routed_atk).append(target["id"])
                    round_log.append(
                        f"{(target.get('leader') or {}).get('name', '无主军团')} 所部士气崩溃，溃逃！"
                    )
            elif dmg > 0 and round_no % 3 == 1:
                round_log.append(
                    f"{(army.get('leader') or {}).get('name', '军团')} 的"
                    f"{TROOP_TYPES.get(army.get('troop_type', ''), {}).get('name', '部队')}"
                    f"重创 {(target.get('leader') or {}).get('name', '敌军')}"
                    f"（-{dmg} 兵）"
                )
            # 反击伤害（守方对攻城方按城防反击在 siege 里另算）。
            # 必须在溃逃判定之后结算：本击直接打崩士气而溃逃的目标不再反击，
            # 与 hex 引擎 _apply_damage → routed → _resolve_attack 口径一致。
            # 该块不掷骰，移到溃逃判定后不改变随机数消耗顺序。
            if target.get("troops", 0) > 0 and not target.get("routed"):
                back = damage_curve(
                    attack_power(target, 1.0, night) * 0.5,
                    defense_power(army),
                    army.get("troops", 0),
                )
                army["troops"] = max(0, int(army.get("troops", 0)) - back)
                army["morale"] = max(0, int(army.get("morale", 100)) - 2)

        # 佛修治疗：每回合为己方全体治疗兵力 2%（§4.3）。
        # 必须放在回合内所有攻击结算之后，且每军团每回合只结算一次；
        # 此前写在「单次攻击」循环内，佛修一方每回合会被治疗 N 次（N=出手数）。
        for side_armies, side_start in ((atk, atk_start), (dfn, dfn_start)):
            for side_army in side_armies:
                if (side_army.get("leader") or {}).get("profession") != "buddha":
                    continue
                if side_army.get("troops", 0) <= 0 or side_army.get("routed"):
                    continue
                cap = int(side_start.get(side_army["id"], side_army["troops"]))
                heal = min(int(side_army["troops"] * 0.02),
                           cap - side_army["troops"])
                if heal > 0:
                    side_army["troops"] += heal

        rounds.append({"round": round_no, "log": round_log[:8]})

        # 结束判定：一方总兵力 <30% 或全部溃逃（§7.3）
        atk_total = sum(a.get("troops", 0) for a in atk if not a.get("routed"))
        dfn_total = sum(a.get("troops", 0) for a in dfn if not a.get("routed"))
        atk_alive = any(a.get("troops", 0) > 0 and not a.get("routed") for a in atk)
        dfn_alive = any(a.get("troops", 0) > 0 and not a.get("routed") for a in dfn)
        if not atk_alive or atk_total < atk_total0 * 0.3:
            winner = "defender"
            break
        if not dfn_alive or dfn_total < dfn_total0 * 0.3:
            winner = "attacker"
            break

    if winner is None:
        # 打满回合：守方守住（攻方未能击溃）
        winner = "defender"

    def _side_result(armies: list[dict], start: dict, routed: list) -> list[dict]:
        out = []
        for a in armies:
            out.append({
                "id": a["id"],
                "faction_id": a.get("faction_id"),
                "troops_before": start.get(a["id"], 0),
                "troops_after": int(a.get("troops", 0)),
                "morale": int(a.get("morale", 0)),
                "routed": bool(a.get("routed")) or a["id"] in routed,
                "leader_id": (a.get("leader") or {}).get("id"),
                "leader_name": (a.get("leader") or {}).get("name"),
                "troop_type": a.get("troop_type"),
                "veteran": int(a.get("veteran", 0)),
            })
        return out

    return {
        "winner": winner,
        "rounds": round_no,
        "round_log": rounds,
        "attackers": _side_result(atk, atk_start, routed_atk),
        "defenders": _side_result(dfn, dfn_start, routed_dfn),
        "routed_attacker_ids": routed_atk,
        "routed_defender_ids": routed_dfn,
        "peril_events": peril_events,
        "atk_losses": sum(
            atk_start.get(a["id"], 0) - a.get("troops", 0) for a in atk
        ),
        "dfn_losses": sum(
            dfn_start.get(a["id"], 0) - a.get("troops", 0) for a in dfn
        ),
    }


# ---------------------------------------------------------------------------
# 围城态推进（§7.2）
# ---------------------------------------------------------------------------

#: 城防伤害缩放：标定「1500 兵 + 破城槌 ≈ 4 tick 破 1 级城」（§15）
SIEGE_DAMAGE_SCALE = 5.5


def city_defense_max(level: int) -> int:
    return 1000 * max(1, int(level))


def siege_strength(armies: Iterable[dict]) -> float:
    """攻方攻城强度 = Σ(兵力/100 × 器械(1~3) × 武将系数(0.8~1.5))。"""
    total = 0.0
    for a in armies:
        troops = max(0, int(a.get("troops", 0)))
        if troops <= 0:
            continue
        engine = 3.0 if a.get("siege_engine") else 1.0
        valor = _leader_stat(a, "valor")
        leader_coef = max(0.8, min(1.5, 0.8 + valor / 200))
        total += troops / 100 * engine * leader_coef
    return total


def siege_tick_damage(armies: Iterable[dict]) -> int:
    """每 tick 城防损伤（含标定系数）。"""
    return int(siege_strength(armies) * SIEGE_DAMAGE_SCALE)


def siege_counter_damage(garrison_troops: int, towers: int, attacker_count: int) -> list[int]:
    """城防反击：(箭塔+守军强度)×60%，多军团分摊（§7.2）。返回每个攻方军团的掉兵。"""
    raw = (max(0, garrison_troops) / 100 + max(0, towers) * 20) * 0.6
    n = max(1, int(attacker_count))
    share = int(raw / n)
    return [share] * n


def city_self_repair(defense: int, level: int) -> int:
    """和平期城防 +2%/tick 自修，上限 70%（§5.3，需修筑任务补满）。"""
    cap = int(city_defense_max(level) * 0.7)
    if defense >= cap:
        return defense
    return min(cap, defense + int(city_defense_max(level) * 0.02))


# ---------------------------------------------------------------------------
# 行军 / 补给 / 断粮（§5.6）
# ---------------------------------------------------------------------------

#: 大地图地形行军耗时系数（边数权重）
TERRAIN_MARCH_COST = {
    "plain": 1.0, "forest": 2.0, "mountain": 2.0, "river": 3.0, "swamp": 2.0,
}


def march_speed(army: dict, terrain: str) -> float:
    """每 tick 可推进的「边当量」：基础 2 边 ÷ 地形系数（机动强的兵种更快）。

    疾行特技（swift）行军速度 +15%（§4.4）。
    """
    troop = TROOP_TYPES.get(army.get("troop_type", "sword_shield"), TROOP_TYPES["sword_shield"])
    base = 1.0 + troop["move"] / 3  # 机动 3 → 2.0；机动 5 → 2.67；机动 2 → 1.67
    cost = TERRAIN_MARCH_COST.get(terrain, 1.0)
    swift_mult = float(trait_army_mods(
        ((army.get("leader") or {}).get("traits")) or [])["speed_mult"])
    # v4.3.13：军事 L4「精锐小队」机动 +1 边当量（调用方按带兵阈值写入 move_bonus）
    return base / cost * swift_mult + float(army.get("move_bonus", 0) or 0)


def grain_upkeep(army: dict, terrain: str, far_from_home: bool) -> int:
    """行军粮耗：兵力×0.5/百兵/tick × 地形系数；离家 2 步以上 ×1.5（§5.6）。

    疾行特技（swift）粮耗 -10%（§4.4）。
    """
    troop = TROOP_TYPES.get(army.get("troop_type", "sword_shield"), TROOP_TYPES["sword_shield"])
    base = troop["grain_per_tick"] * max(0, int(army.get("troops", 0))) / 100
    mult = TERRAIN_MARCH_COST.get(terrain, 1.0) ** 0.5
    if far_from_home:
        mult *= 1.5
    mult *= float(trait_army_mods(
        ((army.get("leader") or {}).get("traits")) or [])["grain_mult"])
    return max(1, int(base * mult))


def carry_grain_cap(leader: dict | None) -> int:
    """携粮 = min(库存, 统率×50)（§5.6）。"""
    leadership = int(((leader or {}).get("stats") or {}).get("leadership", 0))
    return leadership * 50


# ---------------------------------------------------------------------------
# 友方道路 / 相邻领土加成（v4.3.4）
# ---------------------------------------------------------------------------

def friendly_owner(faction_id: int, friendly_ids: Iterable[int]) -> bool:
    """据点归属是否属于本势力/同联盟友方（无主 0 与魔潮 -1 不算）。"""
    try:
        fid = int(faction_id or 0)
    except (TypeError, ValueError):
        return False
    return fid > 0 and fid in {int(x) for x in friendly_ids}


def is_friendly_road(from_node: dict | None, to_node: dict | None,
                     friendly_ids: Iterable[int]) -> bool:
    """道路：相邻两个据点都归本势力/同联盟友方所有。"""
    return bool(from_node and to_node and
                friendly_owner(int(from_node.get("owner_faction") or 0),
                               friendly_ids) and
                friendly_owner(int(to_node.get("owner_faction") or 0),
                               friendly_ids))


def adjacent_friendly_count(node: dict | None, nodes: dict[int, dict],
                            friendly_ids: Iterable[int]) -> int:
    """某据点相邻的友方据点数量（不含自身）。"""
    if not node:
        return 0
    ids = {int(x) for x in friendly_ids}
    count = 0
    for nid in (node.get("adj") or []):
        nxt = nodes.get(int(nid))
        if nxt and int(nxt.get("owner_faction") or 0) in ids:
            count += 1
    return count


def territory_bonuses(adjacent_count: int, cfg: dict | None = None) -> dict:
    """相邻友方领土加成：小幅度城防 + 本土作战攻防。"""
    cfg = cfg or {}
    n = max(0, int(adjacent_count or 0))
    per_def = max(0.0, float(cfg.get("defense_bonus_per_adjacent", 0.04) or 0))
    cap_def = max(0.0, float(cfg.get("defense_bonus_cap", 0.20) or 0))
    per_combat = max(0.0, float(
        cfg.get("home_combat_bonus_per_adjacent", 0.03) or 0))
    cap_combat = max(0.0, float(cfg.get("home_combat_bonus_cap", 0.15) or 0))
    defense_pct = min(cap_def, per_def * n)
    combat_pct = min(cap_combat, per_combat * n)
    return {
        "adjacent": n,
        "defense_pct": defense_pct,
        "combat_pct": combat_pct,
        "defense_mult": 1.0 + defense_pct,
        "combat_mult": 1.0 + combat_pct,
    }


# ---------------------------------------------------------------------------
# NPC 决策评分（§9.3）
# ---------------------------------------------------------------------------

#: 节点产出价值折算币（output_score 用）
NODE_OUTPUT_VALUE = {
    "city": 120, "pass": 40, "mine": 120, "grain": 90,
    "market": 60, "branch": 50, "ruin": 40,
}


def npc_utility(
    node: dict,
    faction: dict,
    own_power: int,
    garrison_power: int,
    path_edges: int,
    threat_nearby_power: int = 0,
    on_trade_route: bool = False,
    adjacent_to_enemy_core: bool = False,
    would_encircle: bool = False,
    cuts_enemy_route: bool = False,
) -> float:
    """utility(T) = (output + strategic − distance) × aggression/100 − threat（§9.3）。"""
    aggression = float((faction.get("personality") or {}).get("aggression", 50))
    econ_w = float((faction.get("personality") or {}).get("econ_weight", 1.0))
    output_score = NODE_OUTPUT_VALUE.get(node.get("type", ""), 40) * econ_w
    strategic = 0.0
    if node.get("type") == "pass":
        strategic += 30
    if on_trade_route:
        strategic += 20
    if adjacent_to_enemy_core:
        strategic += 50
    if would_encircle:
        strategic += 40
    if cuts_enemy_route:
        strategic += 25
    threat = 0.0
    if own_power > 0:
        threat += garrison_power / own_power * 50
    threat += int(node.get("defense", 0)) / 1000 * 20
    if own_power > 0:
        threat += threat_nearby_power / own_power * 30
    distance_cost = path_edges * 5
    return (output_score + strategic - distance_cost) * aggression / 100 - threat


# ---------------------------------------------------------------------------
# 俘虏（§7.9）与劝降
# ---------------------------------------------------------------------------

CAPTURE_CHANCE = {"duel": 0.60, "city_fall": 0.40, "wipeout": 0.20}


def capture_check(kind: str, rng: random.Random, street_fight: bool = False,
                  bonus: float = 0.0) -> bool:
    """被俘判定：单挑 60% ＞ 城陷 40% ＞ 覆没 20%；巷战 +10%（§7.9）。

    ``bonus``：俘获方科技「俘获」加成（军事/谋略线，默认 0）。
    """
    chance = CAPTURE_CHANCE.get(kind, 0.2)
    if kind == "city_fall" and street_fight:
        chance += 0.10
    chance += float(bonus)
    return rng.random() < chance


def persuade_rate(captive_loyalty: int, persuader: dict | None, reputation: int = 0, disloyal: bool = False) -> float:
    """劝降成功率 = (100−忠诚)% × 0.9 + (魅力+智谋)/4 %，钳制 5%~90%；声望档 ±10%。

    离心状态（忠诚归零）固定 80%。
    """
    if disloyal or int(captive_loyalty) <= 0:
        return 0.80
    rate = (100 - int(captive_loyalty)) / 100 * 0.9
    if persuader:
        stats = persuader.get("stats") or {}
        rate += (int(stats.get("charm", 0)) + int(stats.get("wisdom", 0))) / 4 / 100
    if reputation >= 60:
        rate += 0.10
    elif reputation <= -21:
        # 贪婪（-59~-21）与恶名（≤-60）同档 -10%（v4.3.0 合并冗余分支，
        # 原 <=-60 与 <=-21 返回值相同，前者为死代码）
        rate -= 0.10
    return max(0.05, min(0.90, rate))


#: 俘虏处置默认值（可被 config.strategy_config.captive 覆盖）
CAPTIVE_EXECUTE_MERIT = {
    "common": 40, "fine": 70, "rare": 110, "epic": 170, "legend": 260,
}
CAPTIVE_SELL_COIN = {
    "common": 900, "fine": 1800, "rare": 3600, "epic": 7200, "legend": 15000,
}


def captive_execute_merit(char: dict, ccfg: dict | None = None) -> int:
    """处决俘虏获得的战功（按稀有度底价 + 特技数加成）。"""
    cfg = ccfg or {}
    rarity = str((char or {}).get("rarity") or "common")
    table = cfg.get("execute_merit_by_rarity") or {}
    base = int(table.get(rarity, CAPTIVE_EXECUTE_MERIT.get(
        rarity, CAPTIVE_EXECUTE_MERIT["common"])) or 0)
    traits = len((char or {}).get("traits") or [])
    bonus = traits * int(cfg.get("execute_merit_per_trait", 15) or 0)
    return max(0, base + bonus)


def captive_sell_value(char: dict, ccfg: dict | None = None) -> int:
    """售卖俘虏的铜钱收益（稀有度底价 + 六维总和 + 特技数）。"""
    cfg = ccfg or {}
    rarity = str((char or {}).get("rarity") or "common")
    table = cfg.get("sell_coin_by_rarity") or {}
    base = int(table.get(rarity, CAPTIVE_SELL_COIN.get(
        rarity, CAPTIVE_SELL_COIN["common"])) or 0)
    total = sum(int(v or 0) for v in ((char or {}).get("stats") or {}).values())
    value = base + total * int(cfg.get("sell_coin_per_stat", 10) or 0)
    value += len((char or {}).get("traits") or []) * int(
        cfg.get("sell_coin_per_trait", 500) or 0)
    floor = max(0, int(cfg.get("sell_min_coin", 100) or 0))
    return int(math.ceil(max(floor, value) / 10.0) * 10)


# ---------------------------------------------------------------------------
# 单挑（斗法，§7.8）
# ---------------------------------------------------------------------------

def duel_power(char: dict, rng: random.Random) -> float:
    """战力 = 武勇 × (1±气运5%) × 职业克制 × 伤势折损。"""
    stats = effective_stats(char)
    luck = int(stats.get("luck", 0))
    prof_mult = 1.1 if char.get("profession") == "sword" else 1.0
    return int(stats.get("valor", 0)) * (1 + rng.uniform(-0.05, 0.05) * (1 + luck / 200)) * prof_mult


def duel(char_a: dict, char_b: dict, rng: random.Random) -> dict[str, Any]:
    """三局两胜斗法。返回 {winner, rounds:[(pa,pb)], draw}。"""
    rounds = []
    wins_a = wins_b = 0
    for _ in range(3):
        pa, pb = duel_power(char_a, rng), duel_power(char_b, rng)
        rounds.append((round(pa, 1), round(pb, 1)))
        if pa > pb:
            wins_a += 1
        elif pb > pa:
            wins_b += 1
        if wins_a >= 2 or wins_b >= 2:
            break
    if wins_a == wins_b:
        return {"winner": None, "draw": True, "rounds": rounds}
    return {
        "winner": char_a.get("id") if wins_a > wins_b else char_b.get("id"),
        "draw": False,
        "rounds": rounds,
    }


def can_challenge_duel(char: dict) -> bool:
    """单挑资格：武将职责且武勇 ≥75，轻伤不可（§7.8）。"""
    if char.get("role") != ROLE_GENERAL:
        return False
    if injury_state(char.get("injury", 0)) != "healthy":
        return False
    return int((char.get("stats") or {}).get("valor", 0)) >= 75


# ---------------------------------------------------------------------------
# 羁绊（§4.5，P0 基础：同道/知遇；完整演化 P3）
# ---------------------------------------------------------------------------

BOND_TYPES = ("tongdao", "zhiyu", "yijiao", "shitu", "suyuan")
BOND_NAMES = {
    "tongdao": "同道", "zhiyu": "知遇", "yijiao": "义交",
    "shitu": "师徒", "suyuan": "宿怨",
}


def bond_morale_bonus(char_a: dict, char_b: dict) -> int:
    """同军团士气加成：同道 +3、义交 +8（§4.5）。"""
    bonds = (char_a.get("bonds") or {}).get(str(char_b.get("id"))) or {}
    kind = bonds.get("type")
    if kind == "yijiao":
        return 8
    if kind == "tongdao":
        return 3
    return 0


def maybe_form_tongdao(char_a: dict, char_b: dict, rng: random.Random) -> bool:
    """同职业同军团初始概率生成同道羁绊。"""
    if char_a.get("profession") != char_b.get("profession"):
        return False
    a_bonds = char_a.setdefault("bonds", {})
    b_bonds = char_b.setdefault("bonds", {})
    key_a, key_b = str(char_b.get("id")), str(char_a.get("id"))
    if key_a in a_bonds or key_b in b_bonds:
        return False
    if rng.random() < 0.35:
        a_bonds[key_a] = {"type": "tongdao", "level": 1}
        b_bonds[key_b] = {"type": "tongdao", "level": 1}
        return True
    return False


def form_zhiyu(recruiter: dict, recruited: dict) -> None:
    """知遇：A 寻访招募到 B，双方忠诚 +10（§4.5）。"""
    recruiter.setdefault("bonds", {})[str(recruited.get("id"))] = {"type": "zhiyu", "level": 1}
    recruited.setdefault("bonds", {})[str(recruiter.get("id"))] = {"type": "zhiyu", "level": 1}
    recruiter["loyalty"] = min(100, int(recruiter.get("loyalty", 70)) + 10)
    recruited["loyalty"] = min(100, int(recruited.get("loyalty", 70)) + 10)


# ---------------------------------------------------------------------------
# 战功 / 精锐值（§7.5）
# ---------------------------------------------------------------------------

def veteran_gain(winner: bool, siege: bool = False) -> int:
    """老兵值：每场战役 +10~30（胜方高档）、参与围城 +5/次。"""
    if siege:
        return 5
    return 25 if winner else 12


def merit_from_battle(kills: int, winner: bool, manual_win: bool = False, merit_per_kill: float = 0.05, win_base: int = 100) -> int:
    """战功 = 击杀×0.05 + 胜利基础 100；手操获胜 ×1.5（§7.2）。"""
    merit = int(kills * merit_per_kill) + (win_base if winner else 0)
    if winner and manual_win:
        merit = int(merit * 1.5)
    return merit


# ---------------------------------------------------------------------------
# 战报文本渲染（P0 文字版战报，§12.2）
# ---------------------------------------------------------------------------

def render_battle_report(result: dict[str, Any], title: str = "会战") -> str:
    """把 simulate_battle 结果渲染成聊天室/面板可读的文字战报。"""
    lines = [f"⚔️【{title}】历经 {result.get('rounds', 0)} 回合"]
    winner = result.get("winner")
    for r in (result.get("round_log") or [])[-6:]:
        for text in r.get("log", [])[:3]:
            lines.append(f"  · 第{r['round']}回合：{text}")
    for side_key, side_name in (("attackers", "攻方"), ("defenders", "守方")):
        for a in result.get(side_key, []):
            state = "溃逃" if a.get("routed") else ("全灭" if a["troops_after"] <= 0 else "存留")
            leader = a.get("leader_name") or "无主军团"
            troop_name = TROOP_TYPES.get(a.get("troop_type", ""), {}).get("name", "部队")
            lines.append(
                f"  {side_name}·{leader}（{troop_name}）："
                f"{a['troops_before']} → {a['troops_after']} 兵【{state}】"
            )
    verdict = "攻方胜" if winner == "attacker" else "守方胜"
    lines.append(f"🏁 结果：{verdict}（攻方损 {result.get('atk_losses', 0)} / 守方损 {result.get('dfn_losses', 0)}）")
    return "\n".join(lines)


# ===========================================================================
# 以下为 P1~P3 扩展（势力联机 / 外交 / 科技 / 计略 / 赛季 / 互市 / 追赶）
# ===========================================================================

# ---------------------------------------------------------------------------
# 声望档位（§8.2）
# ---------------------------------------------------------------------------

REPUTATION_RANKS = (
    ("righteous", "仁义", 60, 100),
    ("neutral", "中立", -20, 59),
    ("greedy", "贪婪", -59, -21),
    ("infamous", "恶名", -100, -60),
)


def reputation_rank(reputation: int) -> str:
    """声望档位：仁义(+60~100)/中立(-20~59)/贪婪(-59~-21)/恶名(-100~-60)。"""
    rep = max(-100, min(100, int(reputation or 0)))
    for key, _name, lo, hi in REPUTATION_RANKS:
        if lo <= rep <= hi:
            return key
    return "neutral"


def reputation_effects(reputation: int) -> dict[str, Any]:
    """档位效果：招募品质偏移 / 劝降修正 / NPC 侵略加成 / 备战减半。"""
    rank = reputation_rank(reputation)
    if rank == "righteous":
        return {"recruit_shift": 0.10, "persuade_bonus": 0.10,
                "npc_aggression": 0.0, "war_prep_mult": 1.0, "npc_trade_offer": True}
    if rank == "greedy":
        return {"recruit_shift": 0.0, "persuade_bonus": -0.10,
                "npc_aggression": 0.0, "war_prep_mult": 1.0, "raid_bonus": 0.10}
    if rank == "infamous":
        return {"recruit_shift": -0.10, "persuade_bonus": -0.10,
                "npc_aggression": 0.2, "war_prep_mult": 0.5, "raid_bonus": 0.0}
    return {"recruit_shift": 0.0, "persuade_bonus": 0.0,
            "npc_aggression": 0.0, "war_prep_mult": 1.0}


# ---------------------------------------------------------------------------
# 外交（§8.3）：关系存于 strategy_factions.diplomacy_json
#   { "<other_fid>": {"rel": "ally"|"truce"|"war"|"neutral", "until": ts,
#                     "war_at": ts(备战到期=开战时刻), "score": int} }
# ---------------------------------------------------------------------------

REL_NEUTRAL = "neutral"
REL_TRUCE = "truce"
REL_ALLY = "ally"
REL_WAR = "war"


def relation_of(diplomacy: dict | None, other_id: int, now: int = 0) -> str:
    """取当前关系（过期的停战/备战自动回落）。

    外交提案在 ``diplomacy`` 里以 ``rel="propose_*"`` 表示，属于**临时展示
    视图**：真实关系保存在条目的 ``prev`` 快照中。这里透视 ``prev``，
    避免"提个案就把战争/停战/同盟状态抹掉"（例如停战期提案即可绕过门禁）。
    """
    entry = (diplomacy or {}).get(str(int(other_id))) or {}
    rel = str(entry.get("rel", REL_NEUTRAL))
    if rel.startswith("propose_"):
        prev = entry.get("prev")
        if isinstance(prev, dict):
            return relation_of({str(int(other_id)): prev}, other_id, now)
        return REL_NEUTRAL
    until = int(entry.get("until", 0) or 0)
    if rel == REL_TRUCE and until and now and now >= until:
        return REL_NEUTRAL
    if rel == REL_WAR:
        war_at = int(entry.get("war_at", 0) or 0)
        if war_at and now and now < war_at:
            return "war_pending"  # 备战期（尚未全面开战，不可攻击）
    return rel


def is_hostile(diplomacy: dict | None, other_id: int, now: int = 0) -> bool:
    return relation_of(diplomacy, other_id, now) == REL_WAR


def can_ally_count(diplomacy: dict | None, now: int = 0, cap: int = 3) -> bool:
    """结盟上限（默认 3 盟）。"""
    n = sum(1 for k, v in (diplomacy or {}).items()
            if str(v.get("rel", "")) == REL_ALLY)
    return n < cap


def ally_accept_chance(charm: int, rep_self: int, rep_target: int,
                       base: float = 40.0, charm_div: float = 4.0,
                       rep_div: float = 2.0, favor: int = 0,
                       favor_div: float = 2.0) -> float:
    """结盟谈判成功率% = 基础 40 + 发起者魅力/4 + (双方声望档修正)/2
    + 对方好感/2，钳 5~95。"""
    rank_bonus = {"righteous": 15, "neutral": 0, "greedy": -10, "infamous": -25}
    score = (base + float(charm) / charm_div
             + (rank_bonus.get(reputation_rank(rep_self), 0)
                + rank_bonus.get(reputation_rank(rep_target), 0)) / rep_div
             + float(favor) / max(1.0, float(favor_div)))
    return max(5.0, min(95.0, score))


# ---------------------------------------------------------------------------
# 好感度（NPC 外交，§8.3/§9.2）
#   存放在 diplomacy entry 的 "score" 字段（与玩家外交共用结构），
#   对 NPC 势力即「该势力对玩家的好感」。范围默认 -100~100。
# ---------------------------------------------------------------------------

FAVOR_MIN, FAVOR_MAX = -100, 100

#: 好感档位（key, 名称, 下限, 上限），与前端展示顺序一致
FAVOR_TIERS = (
    ("hostile", "敌对", -100, -60),
    ("cold", "冷淡", -59, -20),
    ("neutral", "中立", -19, 19),
    ("friendly", "友善", 20, 49),
    ("close", "亲密", 50, 79),
    ("trusted", "挚友", 80, 100),
)
FAVOR_TIER_NAMES = {k: name for k, name, _lo, _hi in FAVOR_TIERS}
#: 各外交行为对好感度的准入门槛键（war 不设门槛）
FAVOR_GATE_KEYS = {
    "ally": "ally_min", "truce": "truce_min", "peace": "peace_min",
}


def clamp_favor(score: Any, lo: int = FAVOR_MIN, hi: int = FAVOR_MAX) -> int:
    return max(int(lo), min(int(hi), int(score or 0)))


def favor_of(diplomacy: dict | None, other_id: int) -> int:
    """读取对某势力的好感度（缺省 0）。"""
    entry = (diplomacy or {}).get(str(int(other_id))) or {}
    return clamp_favor(entry.get("score", 0))


def favor_tier(score: int) -> str:
    val = clamp_favor(score)
    for key, _name, lo, hi in FAVOR_TIERS:
        if lo <= val <= hi:
            return key
    return "neutral"


def favor_name(score: int) -> str:
    return FAVOR_TIER_NAMES.get(favor_tier(score), "中立")


def favor_gate(kind: str, score: int, fcfg: dict | None = None) -> Optional[int]:
    """返回该外交行为所需最低好感；None 表示无门槛。"""
    key = FAVOR_GATE_KEYS.get(str(kind))
    if not key:
        return None
    default = {"ally_min": 60, "truce_min": -50, "peace_min": -50}[key]
    return int((fcfg or {}).get(key, default))


def favor_gate_ok(kind: str, score: int, fcfg: dict | None = None) -> bool:
    need = favor_gate(kind, score, fcfg)
    return need is None or int(score) >= int(need)


def favor_gift_points(value: float, fcfg: dict | None = None) -> int:
    """赠礼价值 → 好感点数：每 N 币 1 点，单次受 max 点限制。"""
    cfg = fcfg or {}
    per = max(1.0, float(cfg.get("gift_coin_per_point", 800)))
    cap = max(1, int(cfg.get("gift_max_points", 30)))
    return max(0, min(cap, int(float(value) // per)))


def favor_aggression_mult(score: int, fcfg: dict | None = None) -> float:
    """好感对 NPC 进攻意愿的修正：好感越高越不愿动手。"""
    cfg = fcfg or {}
    peace = float(cfg.get("npc_peace_favor", 50))
    val = clamp_favor(score)
    if val >= peace:
        return 0.0
    # -100 → ×1.25，50 → ×0.6
    return max(0.55, min(1.25, 1.05 - val / 250.0))


def npc_seed_relation(rng: random.Random, pers_a: dict | None,
                      pers_b: dict | None,
                      fcfg: dict | None = None) -> tuple[str, int]:
    """按双方性格预置 NPC 之间的关系与初始好感。"""
    cfg = fcfg or {}
    seed = cfg.get("seed") or {}
    a = pers_a or {}
    b = pers_b or {}
    aggr = (float(a.get("aggression", 50)) + float(b.get("aggression", 50))) / 2.0
    demonic = "demonic" in str(b.get("key", "")).lower() or \
        "demonic" in str(a.get("key", "")).lower() or \
        "魔" in str(b.get("name", "")) or "血" in str(b.get("name", ""))
    ally_p = float(seed.get("ally_base", 0.22)) * max(0.15, 1 - aggr / 120.0)
    war_p = float(seed.get("war_base", 0.08)) + aggr / 250.0
    if demonic:
        ally_p *= float(seed.get("demonic_ally_mult", 0.15))
        war_p += float(seed.get("demonic_war_bonus", 0.12))
    roll = rng.random()
    if roll < war_p:
        lo, hi = seed.get("war_favor", [-85, -55])
        return REL_WAR, rng.randint(int(lo), int(hi))
    if roll < war_p + ally_p:
        lo, hi = seed.get("ally_favor", [55, 80])
        return REL_ALLY, rng.randint(int(lo), int(hi))
    lo, hi = seed.get("neutral_favor", [-15, 20])
    return REL_NEUTRAL, rng.randint(int(lo), int(hi))


# ---------------------------------------------------------------------------
# 势力科技（§8.4）：tech_json = {"military": lvl, "agri": lvl, "stratagem": lvl}
# ---------------------------------------------------------------------------

TECH_LINES = ("military", "agri", "stratagem")
TECH_NAMES = {"military": "军事", "agri": "经营", "stratagem": "谋略"}

#: 科技默认效果补丁表：line → level → patch（逐级累计）。
#: 语义：标量键「覆盖」（后级覆盖前级，用于累计值）；
#:       ``terrain_*_mult`` 为「加成增量」，逐级相加（L5 全地形 +3% 叠加在 L1/L3 之上）；
#:       ``resource_output_mult`` / ``node_type_output_mult`` 为「乘区」，逐级相乘。
#: 管理员可在 config ``tech.effects_values`` 按 (line, level) 覆盖本表；缺省用这里。
TECH_EFFECT_PATCHES: dict[str, dict[int, dict[str, Any]]] = {
    "military": {
        1: {"atk_bonus": 0.02, "terrain_atk_mult": {"plain": 0.03},
            "terrain_def_mult": {"plain": 0.03}},
        2: {"atk_bonus": 0.04, "ram_cost_mult": 0.7,
            "big_army_siege_bonus": 0.05},
        3: {"atk_bonus": 0.06, "terrain_atk_mult": {"highland": 0.08},
            "terrain_def_mult": {"forest": 0.10, "river": 0.10, "swamp": 0.10}},
        4: {"intercept_bonus": 0.25,
            "terrain_def_mult": {"wall": 0.10, "gate": 0.10},
            "small_army_move_bonus": 1, "small_army_init_bonus": 5.0},
        5: {"flank_bonus": 0.05,
            "terrain_atk_mult": {t: 0.03 for t in
                                 ("plain", "forest", "highland", "river", "swamp")},
            "big_army_atk_bonus": 0.05},
        6: {"morale_loss_mult": 0.8, "kill_morale_bonus": 10},
        7: {"ignore_zoc": True, "immune_surround": True,
            "duel_win_morale_bonus": 5, "duel_injury_bonus": 0.10,
            "capture_bonus": 0.30, "no_capture": True},
    },
    "agri": {
        1: {"output_bonus": 0.04,
            "node_type_output_mult": {"grain": {"grain": 1.10}}},
        2: {"grain_cost_mult": 0.9,
            "resource_output_mult": {"grain": 1.10}},
        3: {"output_bonus": 0.08,
            "node_type_output_mult": {"mine": {"iron": 1.15}}},
        4: {"convoy_move_bonus": 1, "convoy_loss_mult": 0.5,
            "node_type_output_mult": {"market": {"coin": 1.10}}},
        5: {"starve_resist_ticks": 2,
            "resource_output_mult": {"gold": 1.05, "coin": 1.02,
                                     "grain": 1.02, "iron": 1.02}},
        6: {"recruit_cost_mult": 0.85, "train_mult": 1.20,
            "garrison_grow_bonus": 0.005},
        7: {"heal_mult": 1.30, "salary_mult": 0.80,
            "morale_recover_bonus": 1.0},
    },
    "stratagem": {
        1: {"plot_bonus": 0.04},
        2: {"scout_precision": True, "scout_terrain_reveal": True},
        3: {"ai_wisdom_bonus": 5, "plot_bonus_vs_big_army": 0.03},
        4: {"plot_cd_reduce": 2, "expose_reduction": 0.15},
        5: {"counter_spy": 0.30, "counter_spy_defense": 0.15},
        6: {"fire_damage_mult": 1.30, "fire_def_mult": 0.70},
        7: {"plot_duration_mult": 1.20, "enemy_plot_rate_mult": 0.90},
    },
}


#: 支脉默认参数（配置缺省时回退）；管理员可在 config ``tech`` 块覆盖。
TECH_BRANCH_DEFAULTS: dict[str, Any] = {
    "branch_max_level": 5,
    "branch_cost_coin": [400, 800, 1600, 3200, 6400],
    "branch_cost_iron": [300, 600, 1200, 2400, 4800],
    "branch_base_hours_per_level": [4, 8, 12, 18, 24],
}

#: 三线科技树默认定义。
#: - ``kind="spine"``：主脉节点，固定 1 级；数值仍由 ``TECH_EFFECT_PATCHES``
#:   按 ``spine`` 序号提供（避免同一份数值维护两处）。
#: - ``kind="branch"``：支脉节点，可升 ``max`` 级；``effects`` 为**每级增量**：
#:   ``mode="add"`` 时标量/地形逐级相加，资源乘区按 ``(1+值)^级`` 叠乘；
#:   ``mode="mul"`` 时标量/资源乘区按 ``值^级`` 叠乘。
TECH_TREE: dict[str, list[dict[str, Any]]] = {
    "military": [
        {"id": "mil_s1", "name": "军制", "kind": "spine", "spine": 1, "parent": None,
         "desc": "全兵种攻击 +2%；平原攻防 +3%"},
        {"id": "mil_b_plain", "name": "平原战法", "kind": "branch", "parent": "mil_s1",
         "max": 5, "mode": "add", "desc": "平原攻防", "per_level_text": "+2%",
         "effects": {"terrain_atk_mult": {"plain": 0.02},
                     "terrain_def_mult": {"plain": 0.02}}},
        {"id": "mil_s2", "name": "攻城", "kind": "spine", "spine": 2, "parent": "mil_s1",
         "desc": "破城槌造价 -30%；城墙伤害 +10%；带兵≥2000 攻城槌 +5%"},
        {"id": "mil_b_siege", "name": "攻城术", "kind": "branch", "parent": "mil_s2",
         "max": 5, "mode": "add", "desc": "城墙/城门伤害", "per_level_text": "+2%",
         "effects": {"terrain_atk_mult": {"wall": 0.02, "gate": 0.02}}},
        {"id": "mil_b_ram", "name": "器械术", "kind": "branch", "parent": "mil_s2",
         "max": 3, "mode": "mul", "desc": "破城槌造价", "per_level_text": "-5%",
         "effects": {"ram_cost_mult": 0.95}},
        {"id": "mil_s3", "name": "地利", "kind": "spine", "spine": 3, "parent": "mil_s2",
         "desc": "全兵种攻击累计 +6%；高地攻 +8%、森林防 +10%、涉水减免"},
        {"id": "mil_b_forest", "name": "林地战法", "kind": "branch", "parent": "mil_s3",
         "max": 5, "mode": "add", "desc": "林地防御", "per_level_text": "+5%",
         "effects": {"terrain_def_mult": {"forest": 0.05}}},
        {"id": "mil_b_highland", "name": "山地战法", "kind": "branch", "parent": "mil_s3",
         "max": 5, "mode": "add", "desc": "高地攻击", "per_level_text": "+5%",
         "effects": {"terrain_atk_mult": {"highland": 0.05}}},
        {"id": "mil_b_wade", "name": "涉水适应", "kind": "branch", "parent": "mil_s3",
         "max": 3, "mode": "add", "desc": "河/沼泽防御", "per_level_text": "+4%",
         "effects": {"terrain_def_mult": {"river": 0.04, "swamp": 0.04}}},
        {"id": "mil_s4", "name": "守备", "kind": "spine", "spine": 4, "parent": "mil_s3",
         "desc": "ZOC 拦截 +25%；城墙/关隘守军防御 +10%；带兵≤800 机动 +1、先手 +5"},
        {"id": "mil_b_wall", "name": "城墙守备", "kind": "branch", "parent": "mil_s4",
         "max": 5, "mode": "add", "desc": "城墙防御", "per_level_text": "+3%",
         "effects": {"terrain_def_mult": {"wall": 0.03}}},
        {"id": "mil_b_gate", "name": "关隘守备", "kind": "branch", "parent": "mil_s4",
         "max": 5, "mode": "add", "desc": "关隘防御", "per_level_text": "+3%",
         "effects": {"terrain_def_mult": {"gate": 0.03}}},
        {"id": "mil_s5", "name": "军势", "kind": "spine", "spine": 5, "parent": "mil_s4",
         "desc": "包抄 +5%；全地形攻 +3%；带兵≥3000 攻 +5%"},
        {"id": "mil_b_big", "name": "大军团", "kind": "branch", "parent": "mil_s5",
         "max": 5, "mode": "add", "desc": "大军攻击", "per_level_text": "+1%",
         "effects": {"big_army_atk_bonus": 0.01}},
        {"id": "mil_b_flank", "name": "包抄战术", "kind": "branch", "parent": "mil_s5",
         "max": 5, "mode": "add", "desc": "包抄系数", "per_level_text": "+1%",
         "effects": {"flank_bonus": 0.01}},
        {"id": "mil_s6", "name": "军心", "kind": "spine", "spine": 6, "parent": "mil_s5",
         "desc": "全军士气下降 -20%；击破敌军士气 +10"},
        {"id": "mil_b_morale", "name": "士气整训", "kind": "branch", "parent": "mil_s6",
         "max": 5, "mode": "mul", "desc": "士气下降", "per_level_text": "-2%",
         "effects": {"morale_loss_mult": 0.98}},
        {"id": "mil_s7", "name": "无双", "kind": "spine", "spine": 7, "parent": "mil_s6",
         "desc": "无视 ZOC、免疫包围；单挑获胜士气 +5、致伤 +10%；俘获 +30%、己方不被俘"},
        {"id": "mil_b_duel", "name": "单挑精研", "kind": "branch", "parent": "mil_s7",
         "max": 5, "mode": "add", "desc": "单挑致伤", "per_level_text": "+2%",
         "effects": {"duel_injury_bonus": 0.02}},
        {"id": "mil_b_capture", "name": "俘获精研", "kind": "branch", "parent": "mil_s7",
         "max": 5, "mode": "add", "desc": "俘获率", "per_level_text": "+2%",
         "effects": {"capture_bonus": 0.02}},
    ],
    "agri": [
        {"id": "agr_s1", "name": "劝农", "kind": "spine", "spine": 1, "parent": None,
         "desc": "全据点产出 +4%；谷田粮草 +10%"},
        {"id": "agr_b_grain", "name": "谷田精耕", "kind": "branch", "parent": "agr_s1",
         "max": 5, "mode": "mul", "desc": "谷田粮草", "per_level_text": "+4%",
         "effects": {"node_type_output_mult": {"grain": {"grain": 1.04}}}},
        {"id": "agr_b_reclaim", "name": "垦荒", "kind": "branch", "parent": "agr_s1",
         "max": 5, "mode": "add", "desc": "全据点产出", "per_level_text": "+1%",
         "effects": {"output_bonus": 0.01}},
        {"id": "agr_s2", "name": "漕运", "kind": "spine", "spine": 2, "parent": "agr_s1",
         "desc": "军团粮耗 -10%；全势力粮草 +10%"},
        {"id": "agr_b_convoy", "name": "漕运精研", "kind": "branch", "parent": "agr_s2",
         "max": 5, "mode": "mul", "desc": "军团粮耗", "per_level_text": "-3%",
         "effects": {"grain_cost_mult": 0.97}},
        {"id": "agr_s3", "name": "冶矿", "kind": "spine", "spine": 3, "parent": "agr_s2",
         "desc": "全据点产出累计 +8%；矿脉精铁 +15%"},
        {"id": "agr_b_mine", "name": "矿脉精炼", "kind": "branch", "parent": "agr_s3",
         "max": 5, "mode": "mul", "desc": "矿脉精铁", "per_level_text": "+4%",
         "effects": {"node_type_output_mult": {"mine": {"iron": 1.04}}}},
        {"id": "agr_s4", "name": "商路", "kind": "spine", "spine": 4, "parent": "agr_s3",
         "desc": "运粮机动 +1、被劫 -50%；坊市铜钱 +10%"},
        {"id": "agr_b_market", "name": "坊市通商", "kind": "branch", "parent": "agr_s4",
         "max": 5, "mode": "mul", "desc": "坊市铜钱", "per_level_text": "+4%",
         "effects": {"node_type_output_mult": {"market": {"coin": 1.04}}}},
        {"id": "agr_b_escort", "name": "商队护卫", "kind": "branch", "parent": "agr_s4",
         "max": 3, "mode": "mul", "desc": "被劫损失", "per_level_text": "-10%",
         "effects": {"convoy_loss_mult": 0.90}},
        {"id": "agr_s5", "name": "屯田", "kind": "spine", "spine": 5, "parent": "agr_s4",
         "desc": "断粮抵抗 +2 tick；灵石 +5%；全资源再 +2%"},
        {"id": "agr_b_starve", "name": "屯田精研", "kind": "branch", "parent": "agr_s5",
         "max": 3, "mode": "add", "desc": "断粮抵抗", "per_level_text": "+1 tick",
         "effects": {"starve_resist_ticks": 1}},
        {"id": "agr_b_gold", "name": "灵石采掘", "kind": "branch", "parent": "agr_s5",
         "max": 5, "mode": "mul", "desc": "灵石产出", "per_level_text": "+2%",
         "effects": {"resource_output_mult": {"gold": 1.02}}},
        {"id": "agr_s6", "name": "征募", "kind": "spine", "spine": 6, "parent": "agr_s5",
         "desc": "募兵铜钱粮耗 -15%；练兵效率 +20%；守军增长 +0.5%/tick"},
        {"id": "agr_b_recruit", "name": "募兵精研", "kind": "branch", "parent": "agr_s6",
         "max": 5, "mode": "mul", "desc": "募兵成本", "per_level_text": "-3%",
         "effects": {"recruit_cost_mult": 0.97}},
        {"id": "agr_b_train", "name": "练兵精研", "kind": "branch", "parent": "agr_s6",
         "max": 5, "mode": "add", "desc": "练兵效率", "per_level_text": "+4%",
         "effects": {"train_mult": 0.04}},
        {"id": "agr_s7", "name": "仁政", "kind": "spine", "spine": 7, "parent": "agr_s6",
         "desc": "伤兵恢复 +30%；角色俸禄 -20%；民心恢复 +1/tick"},
        {"id": "agr_b_heal", "name": "医养", "kind": "branch", "parent": "agr_s7",
         "max": 5, "mode": "add", "desc": "伤兵恢复", "per_level_text": "+4%",
         "effects": {"heal_mult": 0.04}},
        {"id": "agr_b_salary", "name": "廉政", "kind": "branch", "parent": "agr_s7",
         "max": 3, "mode": "mul", "desc": "角色俸禄", "per_level_text": "-5%",
         "effects": {"salary_mult": 0.95}},
        {"id": "agr_b_morale", "name": "民心", "kind": "branch", "parent": "agr_s7",
         "max": 5, "mode": "add", "desc": "民心恢复", "per_level_text": "+0.3/tick",
         "effects": {"morale_recover_bonus": 0.3}},
    ],
    "stratagem": [
        {"id": "str_s1", "name": "离间", "kind": "spine", "spine": 1, "parent": None,
         "desc": "计略成功率 +4%"},
        {"id": "str_b_plot", "name": "离间精研", "kind": "branch", "parent": "str_s1",
         "max": 5, "mode": "add", "desc": "计略成功率", "per_level_text": "+1%",
         "effects": {"plot_bonus": 0.01}},
        {"id": "str_s2", "name": "耳目", "kind": "spine", "spine": 2, "parent": "str_s1",
         "desc": "侦察精度提升；侦察范围 +1"},
        {"id": "str_b_intel", "name": "情报网", "kind": "branch", "parent": "str_s2",
         "max": 5, "mode": "add", "desc": "计略暴露率", "per_level_text": "-3%",
         "effects": {"expose_reduction": 0.03}},
        {"id": "str_s3", "name": "军略", "kind": "spine", "spine": 3, "parent": "str_s2",
         "desc": "托管 AI 智谋 +5；对带兵≥2000 目标计略 +3%"},
        {"id": "str_b_ai", "name": "军师团", "kind": "branch", "parent": "str_s3",
         "max": 5, "mode": "add", "desc": "托管智谋", "per_level_text": "+1",
         "effects": {"ai_wisdom_bonus": 1}},
        {"id": "str_b_bigplot", "name": "大军计略", "kind": "branch", "parent": "str_s3",
         "max": 5, "mode": "add", "desc": "对大军计略", "per_level_text": "+1%",
         "effects": {"plot_bonus_vs_big_army": 0.01}},
        {"id": "str_s4", "name": "鬼谋", "kind": "spine", "spine": 4, "parent": "str_s3",
         "desc": "计略冷却 -2 tick；暴露率 -15%"},
        {"id": "str_b_cd", "name": "疾谋", "kind": "branch", "parent": "str_s4",
         "max": 3, "mode": "add", "desc": "计略冷却", "per_level_text": "-1 tick",
         "effects": {"plot_cd_reduce": 1}},
        {"id": "str_b_hide", "name": "隐匿", "kind": "branch", "parent": "str_s4",
         "max": 5, "mode": "add", "desc": "计略暴露率", "per_level_text": "-3%",
         "effects": {"expose_reduction": 0.03}},
        {"id": "str_s5", "name": "反间", "kind": "spine", "spine": 5, "parent": "str_s4",
         "desc": "解锁反间 30%；己方被反间概率 -15%"},
        {"id": "str_b_counter", "name": "反间精研", "kind": "branch", "parent": "str_s5",
         "max": 5, "mode": "add", "desc": "反间成功率", "per_level_text": "+3%",
         "effects": {"counter_spy": 0.03}},
        {"id": "str_b_defense", "name": "防谍", "kind": "branch", "parent": "str_s5",
         "max": 5, "mode": "add", "desc": "被反间概率", "per_level_text": "-3%",
         "effects": {"counter_spy_defense": 0.03}},
        {"id": "str_s6", "name": "火攻", "kind": "spine", "spine": 6, "parent": "str_s5",
         "desc": "火攻威力 +30%；己方受火伤 -30%"},
        {"id": "str_b_fire", "name": "火攻精研", "kind": "branch", "parent": "str_s6",
         "max": 5, "mode": "add", "desc": "火攻威力", "per_level_text": "+6%",
         "effects": {"fire_damage_mult": 0.06}},
        {"id": "str_b_firedef", "name": "火防", "kind": "branch", "parent": "str_s6",
         "max": 5, "mode": "mul", "desc": "受火伤", "per_level_text": "-4%",
         "effects": {"fire_def_mult": 0.96}},
        {"id": "str_s7", "name": "诡道", "kind": "spine", "spine": 7, "parent": "str_s6",
         "desc": "己方计略负面时长 +20%；敌军计略成功率 -10%"},
        {"id": "str_b_duration", "name": "诡道精研", "kind": "branch", "parent": "str_s7",
         "max": 5, "mode": "add", "desc": "计略负面时长", "per_level_text": "+4%",
         "effects": {"plot_duration_mult": 0.04}},
        {"id": "str_b_enemy", "name": "疑兵", "kind": "branch", "parent": "str_s7",
         "max": 5, "mode": "mul", "desc": "敌军计略成功率", "per_level_text": "-2%",
         "effects": {"enemy_plot_rate_mult": 0.98}},
    ],
}


def tech_level(tech_json: dict | None, line: str, max_lvl: int = 5) -> int:
    """科技等级（钳制到 [0, max_lvl]）。

    v4.3.0：上限改为参数化——此前硬编码 min(5,…)，而入口满级判定读配置
    ``tech.levels``；一旦管理员配置 levels>5，等级永远到不了上限，
    势力可无限重复研究（照扣国库、无新增效果）。调用方须把配置上限传入。
    """
    cap = max(1, int(max_lvl or 5))
    return max(0, min(cap, int((tech_json or {}).get(line, 0) or 0)))


def _tech_cfg(tcfg: dict | None) -> dict:
    return tcfg if isinstance(tcfg, dict) else {}


def tech_line_max(tcfg: dict | None = None) -> int:
    """主脉等级上限：优先 config ``levels``，否则取默认树主脉节数。"""
    cfg = _tech_cfg(tcfg)
    try:
        lv = int(cfg.get("levels") or 0)
    except (TypeError, ValueError):
        lv = 0
    if lv > 0:
        return lv
    return max((int(n.get("spine", 0) or 0) for n in TECH_TREE.get("military", [])),
               default=7) or 7


def tech_tree(tcfg: dict | None = None) -> dict[str, list[dict]]:
    """三线节点树；config ``tech.tree`` 按**节点 id** 合并覆盖默认 ``TECH_TREE``。

    v4.5.5：此前配置某条线会整线替换默认节点，管理员只改一个节点就会让
    该线其它主脉/支脉消失。现在按 id 覆盖：同 id 的字段合并，未列出的默认
    节点保留，新 id 追加到该线末尾；空列表表示不启用覆盖。
    """
    cfg = _tech_cfg(tcfg)
    override = cfg.get("tree") if isinstance(cfg.get("tree"), dict) else {}
    out: dict[str, list[dict]] = {}
    for line in TECH_LINES:
        defaults = [dict(n) for n in TECH_TREE.get(line, [])
                    if isinstance(n, dict)]
        nodes = override.get(line)
        if not isinstance(nodes, list):
            out[line] = defaults
            continue
        merged: dict[str, dict] = {}
        order: list[str] = []
        for n in defaults:
            nid = str(n.get("id") or "")
            if nid:
                merged[nid] = n
                order.append(nid)
        extras: list[dict] = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            nid = str(n.get("id") or "")
            if nid and nid in merged:
                merged[nid].update(n)
            elif nid:
                merged[nid] = dict(n)
                order.append(nid)
            else:
                extras.append(dict(n))
        out[line] = [merged[i] for i in order if i in merged] + extras
    return out


def tech_node_map(tcfg: dict | None = None) -> dict[str, dict]:
    """节点索引：node_id → 节点定义（自动补 ``line`` 字段）。"""
    out: dict[str, dict] = {}
    for line, nodes in tech_tree(tcfg).items():
        for node in nodes:
            n = dict(node)
            n["line"] = line
            nid = str(n.get("id") or "")
            if nid:
                out[nid] = n
    return out


def tech_nodes(tech_json: dict | None) -> dict[str, int]:
    """规整 ``tech_json["nodes"]`` 支脉等级（只保留正整数）。"""
    raw = (tech_json or {}).get("nodes")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            lv = int(v or 0)
        except (TypeError, ValueError):
            lv = 0
        if lv > 0:
            out[str(k)] = lv
    return out


def tech_spine_nodes(line: str, tcfg: dict | None = None) -> list[dict]:
    return [n for n in tech_tree(tcfg).get(str(line), [])
            if str(n.get("kind") or "") == "spine"]


def tech_node_level(tech_json: dict | None, node_id: str,
                    tcfg: dict | None = None) -> int:
    """单节点等级：主脉返回 0/1，支脉返回 0~max。"""
    node = tech_node_map(tcfg).get(str(node_id))
    if not node:
        return 0
    line = str(node.get("line") or "")
    if str(node.get("kind") or "") == "spine":
        idx = int(node.get("spine", 0) or 0)
        return 1 if tech_level(tech_json, line, tech_line_max(tcfg)) >= idx else 0
    lv = int(tech_nodes(tech_json).get(str(node_id), 0) or 0)
    cap = int(node.get("max") or _tech_cfg(tcfg).get("branch_max_level") or 5)
    return max(0, min(max(1, cap), lv))


def tech_node_unlocked(tech_json: dict | None, node: dict,
                       tcfg: dict | None = None) -> bool:
    """节点前置是否满足：主脉需前一节；支脉需父节点已达 1 级/已完成。"""
    parent_id = node.get("parent")
    if not parent_id:
        return True
    parent = tech_node_map(tcfg).get(str(parent_id))
    if not parent:
        return True
    if str(parent.get("kind") or "") == "spine":
        idx = int(parent.get("spine", 0) or 0)
        return tech_level(tech_json, str(parent.get("line") or ""),
                          tech_line_max(tcfg)) >= idx
    return tech_node_level(tech_json, str(parent.get("id") or ""), tcfg) >= 1


def tech_node_state(tech_json: dict | None, node_id: str,
                    tcfg: dict | None = None) -> dict | None:
    """节点状态（供研究入口与前端展示）。"""
    node = tech_node_map(tcfg).get(str(node_id))
    if not node:
        return None
    loop_id = str(node.get("id") or "")
    kind = str(node.get("kind") or "spine")
    level = tech_node_level(tech_json, loop_id, tcfg)
    max_level = 1 if kind == "spine" else int(
        node.get("max") or _tech_cfg(tcfg).get("branch_max_level") or 5)
    unlocked = tech_node_unlocked(tech_json, node, tcfg)
    can_research = unlocked and level < max_level
    reason = ""
    if not unlocked:
        parent = tech_node_map(tcfg).get(str(node.get("parent") or ""))
        reason = f"需先完成 {parent.get('name')}" if parent else "前置未完成"
    elif level >= max_level:
        reason = "已满级" if kind == "branch" else "已研究"
    return {
        "id": loop_id, "name": str(node.get("name") or loop_id),
        "kind": kind, "line": str(node.get("line") or ""),
        "level": level, "max": max_level,
        "unlocked": unlocked, "can_research": can_research, "reason": reason,
        "parent": node.get("parent"), "spine": node.get("spine"),
    }


def _pick_curve(arr: Any, idx: int, default: list) -> int:
    vals = list(arr) if isinstance(arr, (list, tuple)) and arr else list(default)
    if not vals:
        vals = list(default)
    try:
        pos = max(0, min(int(idx), len(vals) - 1))
        return int(vals[pos])
    except (TypeError, ValueError):
        return int(default[0])


def tech_research_target(tech_json: dict | None, line: str, node_id: str = "",
                         tcfg: dict | None = None) -> dict:
    """解析研究目标（主脉/支脉统一出口）。

    返回 ``{"error": ...}`` 或目标节点、当前等级、费用与基础耗时。
    """
    cfg = _tech_cfg(tcfg)
    line = str(line or "")
    if line not in TECH_LINES:
        return {"error": "科技线无效（" +
                         "/".join(TECH_NAMES.get(x, x) for x in TECH_LINES) + "）"}
    max_lvl = tech_line_max(tcfg)
    spine_lv = tech_level(tech_json, line, max_lvl)
    node_map = tech_node_map(tcfg)
    if not node_id:
        target = None
        for n in tech_spine_nodes(line, tcfg):
            if int(n.get("spine", 0) or 0) == spine_lv + 1:
                target = n
                break
        if target is None:
            return {"error": f"{TECH_NAMES.get(line, line)} 已满级（L{max_lvl}）"}
    else:
        target = node_map.get(str(node_id))
        if not target or str(target.get("line") or "") != line:
            return {"error": "科技节点无效"}

    kind = str(target.get("kind") or "spine")
    if kind == "spine":
        idx = int(target.get("spine", 0) or 0)
        if spine_lv >= idx:
            return {"error": f"{target.get('name', idx)} 已研究"}
        if spine_lv != idx - 1:
            return {"error": f"需先完成 {TECH_NAMES.get(line, line)} 前置主脉"}
        level = spine_lv
        max_level = 1
        coin_arr = cfg.get("cost_coin") or [500, 1000, 2000, 3500, 6000, 8000, 10000]
        iron_arr = cfg.get("cost_iron") or coin_arr
        hour_arr = cfg.get("base_hours_per_level") or [8, 16, 24, 32, 40, 48, 56]
        coin_def, iron_def, hour_def = coin_arr, iron_arr, hour_arr
    else:
        if not tech_node_unlocked(tech_json, target, tcfg):
            parent = node_map.get(str(target.get("parent") or ""))
            return {"error": f"需先完成 {parent.get('name') if parent else '前置节点'}"}
        level = tech_node_level(tech_json, str(target.get("id") or ""), tcfg)
        max_level = int(target.get("max") or cfg.get("branch_max_level")
                        or TECH_BRANCH_DEFAULTS["branch_max_level"])
        if level >= max_level:
            return {"error": f"{target.get('name', '')} 已满级（L{max_level}）"}
        coin_def = cfg.get("branch_cost_coin") or TECH_BRANCH_DEFAULTS["branch_cost_coin"]
        iron_def = cfg.get("branch_cost_iron") or TECH_BRANCH_DEFAULTS["branch_cost_iron"]
        hour_def = (cfg.get("branch_base_hours_per_level")
                    or TECH_BRANCH_DEFAULTS["branch_base_hours_per_level"])

    return {
        "node": target, "kind": kind, "line": line,
        "level": level, "target_level": level + 1, "max_level": max_level,
        "cost_coin": _pick_curve(coin_def, level, coin_def),
        "cost_iron": _pick_curve(iron_def, level, iron_def),
        "base_hours": _pick_curve(hour_def, level, hour_def),
    }


def tech_tree_view(tcfg: dict | None = None) -> dict[str, list[dict]]:
    """前端节点树视图（可 JSON 序列化，含解锁子节点名称）。"""
    out: dict[str, list[dict]] = {}
    for line, nodes in tech_tree(tcfg).items():
        unlocks: dict[str, list[str]] = {}
        names: dict[str, str] = {}
        for n in nodes:
            nid = str(n.get("id") or "")
            names[nid] = str(n.get("name") or nid)
            if str(n.get("kind") or "") == "branch":
                unlocks.setdefault(str(n.get("parent") or ""), []).append(
                    str(n.get("name") or n.get("id") or ""))
        view: list[dict] = []
        for n in nodes:
            kind = str(n.get("kind") or "spine")
            nid = str(n.get("id") or "")
            parent = str(n.get("parent") or "")
            item = {
                "id": nid, "name": str(n.get("name") or nid), "kind": kind,
                "parent": n.get("parent"), "parent_name": names.get(parent, ""),
                "spine": n.get("spine"),
                "mode": str(n.get("mode") or "add"),
                "desc": str(n.get("desc") or ""),
                "per_level_text": str(n.get("per_level_text") or ""),
                "max": 1 if kind == "spine" else int(n.get("max") or 5),
                "unlocks": unlocks.get(nid, []) if kind == "spine" else [],
            }
            view.append(item)
        out[line] = view
    return out


def _apply_branch_effect(out: dict, key: str, val: Any, lvl: int,
                         mode: str = "add") -> None:
    """把支脉「每级增量 × 等级」并入效果字典。"""
    lvl = max(0, int(lvl or 0))
    if lvl <= 0:
        return
    mode = str(mode or "add")
    if key in ("terrain_atk_mult", "terrain_def_mult"):
        dst = out.setdefault(key, {})
        for t, v in (val or {}).items():
            dst[t] = float(dst.get(t, 0.0)) + float(v) * lvl
        return
    if key in ("resource_output_mult", "node_type_output_mult"):
        dst = out.setdefault(key, {})
        for k, v in (val or {}).items():
            if isinstance(v, dict):
                sub = dst.setdefault(k, {})
                for r, mv in v.items():
                    factor = (float(mv) ** lvl) if mode == "mul" \
                        else ((1.0 + float(mv)) ** lvl)
                    sub[r] = float(sub.get(r, 1.0)) * factor
            else:
                factor = (float(v) ** lvl) if mode == "mul" \
                    else ((1.0 + float(v)) ** lvl)
                dst[k] = float(dst.get(k, 1.0)) * factor
        return
    if isinstance(val, bool):
        if val:
            out[key] = True
        return
    if isinstance(val, (int, float)):
        if mode == "mul":
            out[key] = float(out.get(key, 1.0)) * (float(val) ** lvl)
        else:
            cur = out.get(key, 0.0)
            new = float(cur) + float(val) * lvl
            out[key] = int(round(new)) if isinstance(cur, int) and \
                float(val).is_integer() else new
        return
    out[key] = val


def _apply_tech_patch(out: dict, patch: dict | None) -> None:
    """把单级补丁并入效果字典（地形相加 / 资源相乘 / 标量覆盖）。"""
    for key, val in (patch or {}).items():
        if key in ("terrain_atk_mult", "terrain_def_mult"):
            dst = out.setdefault(key, {})
            for t, v in (val or {}).items():
                dst[t] = float(dst.get(t, 0.0)) + float(v)
        elif key in ("resource_output_mult", "node_type_output_mult"):
            dst = out.setdefault(key, {})
            for k, v in (val or {}).items():
                if isinstance(v, dict):
                    sub = dst.setdefault(k, {})
                    for r, mv in v.items():
                        sub[r] = float(sub.get(r, 1.0)) * float(mv)
                else:
                    dst[k] = float(dst.get(k, 1.0)) * float(v)
        else:
            out[key] = val


def tech_effects(tech_json: dict | None, max_lvl: int = 5,
                 tcfg: dict | None = None) -> dict[str, Any]:
    """把三线等级折算成数值效果字典（调用方按需取键）。

    返回键只增不改，向后兼容旧调用方。新增地形/带兵/资源三类效果：
    - ``terrain_atk_mult`` / ``terrain_def_mult``：按 hex 地形加成的增量（相加）。
    - ``big_army_*`` / ``small_army_*``：按带兵阈值触发（阈值见返回值）。
    - ``resource_output_mult`` / ``node_type_output_mult``：产出乘区（相乘）。
    ``tcfg`` 为 config ``tech`` 配置块，可选；缺省用内置 ``TECH_EFFECT_PATCHES``。
    """
    if isinstance(tcfg, dict) and tcfg.get("levels"):
        max_lvl = max(1, int(tcfg.get("levels") or max_lvl))
    mil = tech_level(tech_json, "military", max_lvl)
    agri = tech_level(tech_json, "agri", max_lvl)
    strat = tech_level(tech_json, "stratagem", max_lvl)
    tcfg = tcfg if isinstance(tcfg, dict) else {}
    out: dict[str, Any] = {
        # —— 既有键（保持默认值，旧调用方无感）——
        "atk_bonus": 0.0, "ram_cost_mult": 1.0,
        "intercept_bonus": 0.0, "flank_bonus": 0.0,
        "output_bonus": 0.0, "grain_cost_mult": 1.0,
        "convoy_move_bonus": 0, "convoy_loss_mult": 1.0,
        "starve_resist_ticks": 0,
        "plot_bonus": 0.0, "scout_precision": False, "ai_wisdom_bonus": 0,
        "plot_cd_reduce": 0, "expose_reduction": 0.0, "counter_spy": 0.0,
        # —— 新增：地形轴 ——
        "terrain_atk_mult": {}, "terrain_def_mult": {},
        # —— 新增：带兵轴 ——
        "big_army_atk_bonus": 0.0, "big_army_siege_bonus": 0.0,
        "small_army_move_bonus": 0, "small_army_init_bonus": 0.0,
        "small_army_grain_mult": 1.0,
        # —— 新增：资源轴 ——
        "resource_output_mult": {}, "node_type_output_mult": {},
        # —— 新增：谋略联动 ——
        "scout_terrain_reveal": False, "plot_bonus_vs_big_army": 0.0,
        "counter_spy_defense": 0.0,
        # —— 新增（v4.3.14，三国志个性转化）：士气 / 无双 / 征募 / 医养 / 火攻 / 诡道 ——
        "morale_loss_mult": 1.0, "kill_morale_bonus": 0,
        "ignore_zoc": False, "immune_surround": False,
        "duel_win_morale_bonus": 0, "duel_injury_bonus": 0.0,
        "capture_bonus": 0.0, "no_capture": False,
        "recruit_cost_mult": 1.0, "train_mult": 1.0,
        "heal_mult": 1.0, "salary_mult": 1.0,
        "garrison_grow_bonus": 0.0, "morale_recover_bonus": 0.0,
        "fire_damage_mult": 1.0, "fire_def_mult": 1.0,
        "plot_duration_mult": 1.0, "enemy_plot_rate_mult": 1.0,
    }
    overrides = tcfg.get("effects_values") or {}
    for line, lvl in (("military", mil), ("agri", agri),
                      ("stratagem", strat)):
        default = TECH_EFFECT_PATCHES.get(line) or {}
        cfg_line = overrides.get(line) if isinstance(overrides, dict) else None
        for level in range(1, int(lvl) + 1):
            patch = dict(default.get(level) or {})
            if isinstance(cfg_line, dict):
                cfg_patch = cfg_line.get(str(level))
                if isinstance(cfg_patch, dict):
                    patch.update(cfg_patch)  # 配置按级覆盖默认补丁
            _apply_tech_patch(out, patch)
    # 支脉：按节点等级叠加（每级增量；主脉前置未满足的支脉不生效，防改档）
    node_map = tech_node_map(tcfg)
    for node_id, raw_lv in tech_nodes(tech_json).items():
        node = node_map.get(node_id)
        if not node or str(node.get("kind") or "") != "branch":
            continue
        if not tech_node_unlocked(tech_json, node, tcfg):
            continue
        lvl = int(raw_lv)
        cap = int(node.get("max") or tcfg.get("branch_max_level") or 5)
        lvl = max(0, min(max(1, cap), lvl))
        mode = str(node.get("mode") or "add")
        for key, val in (node.get("effects") or {}).items():
            _apply_branch_effect(out, str(key), val, lvl, mode)
    # 带兵阈值配置（非补丁，单独读取）
    out["big_army_threshold"] = int(tcfg.get("big_army_threshold", 2000) or 2000)
    out["small_army_threshold"] = int(tcfg.get("small_army_threshold", 800) or 800)
    return out


def tech_research_hours(base_hours: int, stat: int, divisor: float = 10.0,
                        pct: float = 0.05) -> float:
    """研究耗时：每 10 点主属性（智谋/政务取高）-5%。"""
    steps = int(stat) // max(1, int(divisor))  # 防除零
    return max(1.0, float(base_hours) * (1 - steps * pct))


# ---------------------------------------------------------------------------
# 计略（§6.3）
# ---------------------------------------------------------------------------

PLOT_TYPES = ("alienate", "rumor", "arson", "persuade_plot", "sabotage", "rescue")
PLOT_NAMES = {
    "alienate": "离间", "rumor": "散布谣言", "arson": "纵火",
    "persuade_plot": "劝降", "sabotage": "策应", "rescue": "营救",
}

#: 「江湖纪事」日志类别：kind → (图标, 名称)。前端筛选与条目徽标共用，
#: 服务端通过 meta / logs 接口下发，避免前后端两套口径。
WORLD_LOG_KINDS = {
    "plot": ("🎴", "计略"),
    "battle": ("⚔️", "战事"),
    "capture": ("🏯", "据点"),
    "diplomacy": ("🤝", "外交"),
    "npc": ("🏹", "NPC"),
    "event": ("🎲", "异象"),
    "season": ("🏆", "赛季"),
    "captive": ("⛓️", "俘虏"),
    "coalition": ("🛡️", "合军"),
}


def plot_success_rate(wisdom_self: int, wisdom_target: int,
                      bonus: float = 0.0, clamp: tuple = (10.0, 85.0)) -> float:
    """成功率% = 35 + (己智谋 − 敌最高智谋)/4 + 祭酒/科技加成，钳 10~85。"""
    rate = 35.0 + (int(wisdom_self) - int(wisdom_target)) / 4.0 + float(bonus) * 100
    return max(float(clamp[0]), min(float(clamp[1]), rate))


def plot_expose(rng: random.Random, base: float = 0.4,
                reduction: float = 0.0) -> bool:
    """计略失败暴露判定：40%（谋略 L4 -15%）。"""
    return rng.random() < max(0.0, float(base) - float(reduction))


def rescue_success_rate(wisdom_self: int, wisdom_target: int,
                        captive_leadership: int, bonus: float = 0.0) -> float:
    """营救：计略公式 + 俘虏统率修正（统率/10 %）。"""
    return plot_success_rate(wisdom_self, wisdom_target,
                             bonus + int(captive_leadership) / 10 / 100)


# ---------------------------------------------------------------------------
# 羁绊网完整演化（§4.5）
# ---------------------------------------------------------------------------

def bond_get(char: dict, other_id: Any) -> dict:
    return (char.get("bonds") or {}).get(str(other_id)) or {}


def bond_set_both(a: dict, b: dict, kind: str, level: int = 1) -> None:
    a.setdefault("bonds", {})[str(b.get("id"))] = {"type": kind, "level": level,
                                                   "battles": 0}
    b.setdefault("bonds", {})[str(a.get("id"))] = {"type": kind, "level": level,
                                                   "battles": 0}


def bond_battle_progress(a: dict, b: dict) -> None:
    """共同经历一场战役：battles +1；≥5 场且为同道 → 升级为义交（§4.5）。"""
    ab = a.setdefault("bonds", {}).setdefault(str(b.get("id")),
                                               {"type": "tongdao", "level": 1, "battles": 0})
    bb = b.setdefault("bonds", {}).setdefault(str(a.get("id")),
                                              {"type": "tongdao", "level": 1, "battles": 0})
    ab["battles"] = int(ab.get("battles", 0)) + 1
    bb["battles"] = int(bb.get("battles", 0)) + 1
    if ab.get("type") == "tongdao" and ab["battles"] >= 5:
        ab.update({"type": "yijiao", "level": 1})
        bb.update({"type": "yijiao", "level": 1})


def bond_battles(a: dict, b: dict) -> int:
    """双方「共同经历战役」计数（取两侧较大值，兼容旧数据单边缺字段）。"""
    return max(int(bond_get(a, b.get("id")).get("battles", 0) or 0),
               int(bond_get(b, a.get("id")).get("battles", 0) or 0))


def maybe_form_yijiao(a: dict, b: dict, rng: random.Random) -> bool:
    """义交判定（§4.5）：共同经历 ≥5 场战役，或互救成功后按 50% 结义。

    已成为师徒/宿怨的组合不再演化；返回是否本次结成义交。
    """
    current = bond_get(a, b.get("id")).get("type")
    if current in ("shitu", "suyuan"):
        return False
    if current == "yijiao":
        return False
    battles = bond_battles(a, b)
    rescued = bool(bond_get(a, b.get("id")).get("rescued")
                   or bond_get(b, a.get("id")).get("rescued"))
    if battles < 5 and not rescued:
        return False
    if battles < 5 and rescued and rng.random() >= 0.5:
        return False
    bond_set_both(a, b, "yijiao", 1)
    return True


def maybe_form_shitu(senior: dict, junior: dict, shared_battles: int | None = None,
                     allow_upgrade: bool = False) -> bool:
    """师徒：极品+ 与凡/良品同军团 ≥10 场（§4.5）。

    ``shared_battles`` 传 None 时自动取双方羁绊里的共享战斗计数，
    与 ``bond_apply_battle`` 的口径保持一致；``allow_upgrade`` 允许由
    同道升格为师徒（义交/宿怨不降格）。
    """
    if senior.get("rarity") not in ("epic", "legend"):
        return False
    if junior.get("rarity") not in ("common", "fine"):
        return False
    shared = bond_battles(senior, junior) if shared_battles is None \
        else int(shared_battles)
    if shared < 10:
        return False
    current = bond_get(senior, junior.get("id")).get("type")
    if current and not (allow_upgrade and current in ("tongdao", "yijiao")):
        return False
    bond_set_both(senior, junior, "shitu", 1)
    return True


def form_suyuan(a: dict, b: dict) -> None:
    """宿怨：单挑击败对方 / 击杀其义交。"""
    bond_set_both(a, b, "suyuan", 1)


def maybe_form_suyuan(a: dict, b: dict, duel_defeated: bool = False,
                      killed_bond: bool = False) -> bool:
    """宿怨触发条件封装（§4.5）：单挑击败 / 击杀对方义交才算结怨。

    普通并肩作战不会结怨；返回是否新结宿怨。
    """
    if not (duel_defeated or killed_bond):
        return False
    if bond_get(a, b.get("id")).get("type") == "suyuan":
        return False
    form_suyuan(a, b)
    return True


def bond_apply_battle(a: dict, b: dict, won: bool) -> dict[str, Any]:
    """一次完成战斗后羁绊演化（供 managers 调用，§4.5）。

    顺序：同道积累（battles +1）→ 义交 → 师徒 → 宿怨 → 知遇。
    ``won`` 表示 a 所在阵营是否获胜；宿怨仅在被标记为「单挑落败/义交被杀」
    时触发（读 ``duel_lost_to`` / ``killed_bond_of`` 标记，避免同袍反目）。
    返回本次新结成的羁绊类型集合，便于落库/通知。
    """
    out: dict[str, Any] = {"tongdao": False, "yijiao": False,
                           "shitu": False, "suyuan": False, "zhiyu": False}
    if not a or not b or str(a.get("id")) == str(b.get("id")):
        return out
    prev = bond_get(a, b.get("id")).get("type")
    # 知遇：招募关系在战后首次补登记（优先于同道，保持「一人一羁绊」）
    recruited = a.get("recruited_by") == b.get("id") or \
        b.get("recruited_by") == a.get("id")
    if recruited and not prev:
        form_zhiyu(a, b)
        out["zhiyu"] = True
        out["won"] = bool(won)
        return out
    bond_battle_progress(a, b)
    now_type = bond_get(a, b.get("id")).get("type")
    if prev not in ("tongdao", "yijiao") and now_type == "tongdao":
        out["tongdao"] = True
    if prev != "yijiao" and now_type == "yijiao":
        out["yijiao"] = True
    # 义交：≥5 战或互救；用确定性 rng 保证同输入同结果（回放/测试可复现）
    rng = random.Random(f"yijiao:{a.get('id')}:{b.get('id')}:{bond_battles(a, b)}")
    if not out["yijiao"] and maybe_form_yijiao(a, b, rng):
        out["yijiao"] = True
    # 师徒：按稀有度自动判定长幼（同道可升格，义交/宿怨不降格）
    shared = bond_battles(a, b)
    if maybe_form_shitu(a, b, shared, allow_upgrade=True) or \
            maybe_form_shitu(b, a, shared, allow_upgrade=True):
        out["shitu"] = True
    # 宿怨：单挑落败 / 义交被杀的显式标记
    duel_lost = a.get("duel_lost_to") == b.get("id") or b.get("duel_lost_to") == a.get("id")
    killed_bond = a.get("killed_bond_of") == b.get("id") or \
        b.get("killed_bond_of") == a.get("id")
    if maybe_form_suyuan(a, b, duel_defeated=duel_lost, killed_bond=killed_bond):
        out["suyuan"] = True
    out["won"] = bool(won)
    return out


def bond_army_morale_bonus(members: list[dict]) -> int:
    """整队羁绊士气加成（§4.5）：逐对累加同道 +3 / 义交 +8 / 师徒 +5，封顶 +20。"""
    total = 0
    seen: set[int] = set()
    chars = [c for c in (members or []) if c and c.get("id") is not None]
    for i, a in enumerate(chars):
        for b in chars[i + 1:]:
            key = id(a) * 1000003 + id(b)
            if key in seen:
                continue
            seen.add(key)
            bonus = bond_morale_bonus_full(a, b) or bond_morale_bonus_full(b, a)
            total += int(bonus)
    return max(0, min(20, total))


def doctor_aftermath(leader: dict | None, lost_troops: int,
                     injury: int = 0) -> dict[str, int]:
    """名医特技战后结算（§4.4）：伤兵回收 8%、角色伤势额外 -1。"""
    mods = trait_army_mods(((leader or {}).get("traits")) or [])
    pmods = personality_mods((leader or {}).get("personality"))
    lost = max(0, int(lost_troops or 0))
    recovered = int(lost * float(mods["recover_pct"]))
    new_injury = max(0, int(injury or 0) - int(mods["injury_reduce"])
                     + int(pmods["injury_delta"]))
    return {"recovered": recovered, "injury": new_injury}


def suyuan_attack_bonus(a: dict, b: dict) -> float:
    """宿怨同战场：双方攻击 +10%。"""
    return 1.10 if bond_get(a, b.get("id")).get("type") == "suyuan" else 1.0


def bond_morale_bonus_full(a: dict, b: dict) -> int:
    """同军团士气：同道 +3 / 义交 +8 / 师徒 +5。"""
    kind = bond_get(a, b.get("id")).get("type")
    return {"tongdao": 3, "yijiao": 8, "shitu": 5}.get(kind, 0)


def junior_exp_mult(junior: dict, master_id: Any) -> float:
    """师徒：徒弟阅历 +20%。"""
    return 1.2 if bond_get(junior, master_id).get("type") == "shitu" else 1.0


def legacy_on_death(dead: dict, all_chars: list[dict]) -> dict[str, Any]:
    """陨落遗产（§4.6）：义交忠诚 -20 进雪耻；师徒徒弟继承 1 条特技。

    返回 {"avengers": [char...], "heir": char|None, "inherited_trait": str|None}
    """
    avengers: list[dict] = []
    heir = None
    inherited = None
    for c in all_chars:
        if c.get("id") == dead.get("id"):
            continue
        b = bond_get(c, dead.get("id"))
        if b.get("type") == "yijiao":
            c["loyalty"] = max(0, int(c.get("loyalty", 70)) - 20)
            c.setdefault("vengeance", {"target_faction": None, "active": True})
            avengers.append(c)
        elif b.get("type") == "shitu" and heir is None:
            traits = dead.get("traits") or []
            if traits:
                inherited = traits[0]
                c_traits = list(c.get("traits") or [])
                if inherited not in c_traits:
                    c_traits.append(inherited)
                c["traits"] = c_traits
            heir = c
    return {"avengers": avengers, "heir": heir, "inherited_trait": inherited}


# ---------------------------------------------------------------------------
# 官职效果（§4.5 官职表）
# ---------------------------------------------------------------------------

POSITION_EFFECTS = {
    "taiwei": {"army_atk": 0.03},
    "situ": {"node_output": 0.05},
    "jijiu": {"plot_bonus": 0.10, "ai_wisdom": 5},
    "zhonglang": {"recruit_quality": 0.10},
}


def faction_position_bonus(positions: dict[str, list]) -> dict[str, float]:
    """按在任官职聚合势力级加成（太尉全军攻+3% 等；祭酒按人数叠半）。"""
    out = {"army_atk": 0.0, "node_output": 0.0, "plot_bonus": 0.0,
           "recruit_quality": 0.0, "ai_wisdom": 0}
    for pos, holders in (positions or {}).items():
        eff = POSITION_EFFECTS.get(pos)
        if not eff or not holders:
            continue
        for k, v in eff.items():
            out[k] = out.get(k, 0) + v
    return out


# ---------------------------------------------------------------------------
# 主城重建（§8.7）与护山盾（§8.5）
# ---------------------------------------------------------------------------

def rebuild_gold_cost(base: int, rebuild_count: int, growth: float = 0.5) -> int:
    """重建灵石：首次 base（300 万），本赛季每再重建 +50%（复利）。"""
    return int(float(base) * (1 + growth) ** max(0, int(rebuild_count)))


# ---------------------------------------------------------------------------
# 合纵制衡（§9.4）与赛季（§11）
# ---------------------------------------------------------------------------

SEASON_NODE_SCORE = {"city": 3, "pass": 2, "mine": 1, "grain": 1,
                     "market": 1, "branch": 1, "ruin": 1}


def hegemony_share(node_types_by_faction: dict[int, list[str]],
                   score_table: dict[str, int] | None = None) -> dict[int, float]:
    """霸权值占比：占点加权（仙城3/关隘2/其余1）/ 全图总权重。"""
    table = score_table or SEASON_NODE_SCORE
    weighted = {fid: sum(table.get(t, 1) for t in types)
                for fid, types in node_types_by_faction.items()}
    total = sum(weighted.values())
    if total <= 0:
        return {fid: 0.0 for fid in weighted}
    return {fid: weighted[fid] / total for fid in weighted}


def coalition_state(hegemony_pct: float, current: bool,
                    trigger: float = 0.4, release: float = 0.3) -> bool:
    """合纵迟滞判定：>40% 触发；<30% 解除；其间维持现状。"""
    if hegemony_pct > trigger:
        return True
    if hegemony_pct < release:
        return False
    return current


def season_phase(day: int, season_days: int = 75, mid_day: int = 31,
                 late_day: int = 56) -> str:
    """赛季阶段：early / mid / late / final（末 3 天决战时刻）。"""
    if day >= season_days - 3:
        return "final"
    if day >= late_day:
        return "late"
    if day >= mid_day:
        return "mid"
    return "early"


def honor_from_merit(merit: int, rate: float = 1.0) -> int:
    """赛季结算：战功 → 荣誉点（1:1 起）。"""
    return int(max(0, int(merit)) * float(rate))


def veteran_carryover(veteran: int, pct: float = 0.5) -> int:
    """御林军团老兵值结转（≥300 才结转，50%）。"""
    v = int(veteran or 0)
    return int(v * pct) if v >= 300 else 0


# ---------------------------------------------------------------------------
# 坊市互市（§5.5）
# ---------------------------------------------------------------------------

def market_tax(amount: int, pct: float = 0.05) -> int:
    """交易税（销毁）：成交额 5%。"""
    return int(int(amount) * float(pct))


def npc_maker_price(base_price: float, rng: random.Random,
                    spread_pct: float = 0.15, is_sell: bool = True) -> int:
    """NPC 做市挂单价：基准 ±15%（卖单偏高、买单偏低，提供价格锚点）。"""
    factor = 1 + rng.uniform(0, spread_pct) if is_sell else 1 - rng.uniform(0, spread_pct)
    return max(1, int(base_price * factor))


def market_price_band(base_price: float, band_pct: float = 0.5,
                      min_price: int = 1) -> tuple[int, int, int]:
    """玩家挂单价允许区间：市场基准价 ±band_pct。

    返回 ``(最低价, 最高价, 基准价)``。低于 1 的价格没有意义，统一抬到
    ``min_price``；基准价缺失（<=0）时返回 ``(min_price, 0, 0)`` 表示不限制。
    """
    base = int(round(float(base_price or 0)))
    if base <= 0:
        return max(1, int(min_price)), 0, 0
    band = max(0.0, min(1.0, float(band_pct)))
    lo = max(max(1, int(min_price)), int(base * (1.0 - band)))
    hi = max(lo, int(base * (1.0 + band)))
    return lo, hi, base


def npc_offer_price(list_price: float, rng: random.Random,
                    spread_pct: float = 0.15) -> int:
    """NPC 收购报价：在玩家挂单价基础上上下浮动（默认 ±15%）。"""
    spread = max(0.0, float(spread_pct))
    factor = 1.0 + rng.uniform(-spread, spread)
    return max(1, int(round(float(list_price) * factor)))


# ---------------------------------------------------------------------------
# 商路（§5.5）与合围断粮（§5.6）
# ---------------------------------------------------------------------------

def find_trade_routes(adjacency: dict[int, list[int]], node_type: dict[int, str],
                      owner: dict[int, int], is_ally_or_self) -> list[dict]:
    """自动商路：两端为坊市（或坊市-仙城），全程同一势力/盟友节点连通。

    is_ally_or_self(fid_a, fid_b) -> bool 由调用方注入外交判定。
    返回 [{"a": id, "b": id, "path": [...]}]（去重）。
    """
    endpoints = [nid for nid, t in node_type.items() if t in ("market", "city")]
    found: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for start in endpoints:
        # BFS：只走「与起点 owner 同盟/同属」的节点
        base_owner = owner.get(start, 0)
        if base_owner <= 0:
            continue
        visited = {start}
        queue = [(start, [start])]
        while queue:
            cur, path = queue.pop(0)
            for nxt in adjacency.get(cur, []):
                if nxt in visited:
                    continue
                nxt_owner = owner.get(nxt, 0)
                if nxt_owner <= 0 or not is_ally_or_self(base_owner, nxt_owner):
                    continue
                visited.add(nxt)
                new_path = path + [nxt]
                if node_type.get(nxt) in ("market", "city") and nxt != start:
                    pair = (min(start, nxt), max(start, nxt))
                    if pair not in seen:
                        seen.add(pair)
                        found.append({"a": pair[0], "b": pair[1],
                                      "path": new_path,
                                      "owner": base_owner})
                queue.append((nxt, new_path))
    return found


def trade_route_income(route: dict, coin_per_node: int = 20) -> int:
    """商路每 tick 收益：途经据点数量 × 单点收益。

    ``path`` 含两端与全部中间节点；路径缺失（旧存档/异常）时按最小
    2 个端点计，避免商路因字段缺失而零收益。
    """
    try:
        count = len(route.get("path") or [])
    except TypeError:
        count = 0
    if count <= 0:
        count = 2
    return max(0, int(coin_per_node)) * int(count)


def trade_route_task_bonus(routes: list[dict], base: float = 0.3,
                           per_route: float = 0.05, per_edge: float = 0.02,
                           max_bonus: float = 1.0) -> float:
    """商路网络对贸易任务的加成。

    商路数量越多、总长度（边数）越大，加成越高，并受 ``max_bonus`` 封顶。
    无商路时返回 0（调用方只在节点位于商路时使用）。
    """
    if not routes:
        return 0.0
    count = len(routes)
    edges = sum(max(0, len(r.get("path") or []) - 1) for r in routes)
    bonus = float(base) + float(per_route) * count + float(per_edge) * edges
    return max(0.0, min(float(max_bonus), bonus))


def trade_route_output_bonus(routes: list[dict], base: float = 0.0,
                             per_route: float = 0.02, per_edge: float = 0.01,
                             max_bonus: float = 0.4) -> dict[int, float]:
    """商路途经据点的自然产出加成。

    同一节点被越多商路经过、且这些商路越长，加成越高（单节点封顶
    ``max_bonus``）。返回 ``{node_id: bonus_ratio}``。
    """
    route_count: dict[int, int] = {}
    route_edges: dict[int, int] = {}
    for r in routes:
        path = [int(x) for x in (r.get("path") or [])]
        if not path:
            continue
        edges = max(0, len(path) - 1)
        for nid in path:
            route_count[nid] = route_count.get(nid, 0) + 1
            route_edges[nid] = route_edges.get(nid, 0) + edges
    return {
        nid: max(0.0, min(float(max_bonus),
                          float(base) + float(per_route) * count +
                          float(per_edge) * route_edges.get(nid, 0)))
        for nid, count in route_count.items()
    }


def is_node_encircled(node_id: int, adjacency: dict[int, list[int]],
                      owner: dict[int, int], is_hostile_fn) -> bool:
    """据点合围断粮判定：所有邻接节点被敌对占据/封锁。"""
    owner_self = owner.get(node_id, 0)
    neighbors = adjacency.get(node_id, [])
    if not neighbors:
        return False
    for nid in neighbors:
        n_owner = owner.get(nid, 0)
        if n_owner == owner_self or not is_hostile_fn(owner_self, n_owner):
            return False
    return True


# ---------------------------------------------------------------------------
# 运粮队劫掠 / 追击截获（§5.6 / §7.6）
# ---------------------------------------------------------------------------

def convoy_raid_success(rng: random.Random, escort_troops: int,
                        raider_troops: int, base: float = 0.5) -> bool:
    """劫粮：兵力比修正的成功率（raider/(raider+escort) 围绕 base）。"""
    ratio = raider_troops / max(1, raider_troops + escort_troops)
    chance = max(0.05, min(0.95, base + (ratio - 0.5)))
    return rng.random() < chance


def pursuit_capture(rng: random.Random, cavalry: bool = False,
                    swift: bool = False, base: float = 0.25) -> bool:
    """溃逃追击截获：基础 25%；飞剑队（快速机动·劫粮追击）+15%；
    疾行特技（swift）+10%（§4.4 / §7.6）。"""
    chance = float(base) + (0.15 if cavalry else 0.0) + (0.10 if swift else 0.0)
    return rng.random() < max(0.0, min(0.95, chance))


# ---------------------------------------------------------------------------
# 追赶礼包（§17）
# ---------------------------------------------------------------------------

def catchup_scale(day_of_season: int, season_days: int = 75,
                  mid_mult: float = 1.5) -> float:
    """追赶礼包缩放：赛季进度线性插值 1.0 → 中期 mid_mult → 末期回落 1.2。"""
    if season_days <= 0:
        return 1.0
    progress = max(0.0, min(1.0, day_of_season / season_days))
    if progress <= 0.5:
        return 1.0 + (mid_mult - 1.0) * (progress / 0.5)
    return mid_mult + (1.2 - mid_mult) * ((progress - 0.5) / 0.5)


def scale_pack(pack: dict[str, int], mult: float) -> dict[str, int]:
    return {k: int(v * mult) for k, v in pack.items() if isinstance(v, (int, float))}


# ---------------------------------------------------------------------------
# 势力治理（§8.6）
# ---------------------------------------------------------------------------

def successor_of(members: list[dict], leader_user: str) -> Optional[dict]:
    """领袖继任：副主顺位 → 贡献最高成员。members 需含 position/contribution。"""
    deputies = [m for m in members if m.get("position") == "deputy"
                and m.get("user_id") != leader_user]
    if deputies:
        return max(deputies, key=lambda m: int(m.get("contribution", 0)))
    rest = [m for m in members if m.get("user_id") != leader_user]
    if not rest:
        return None
    return max(rest, key=lambda m: int(m.get("contribution", 0)))


def treasury_split(treasury: dict[str, int],
                   contributions: dict[str, int]) -> dict[str, dict[str, int]]:
    """旧档国库清算工具：按贡献比例分给成员（玩家势力已无国库，仅兼容）。"""
    total_c = sum(max(0, int(v)) for v in contributions.values())
    out: dict[str, dict[str, int]] = {}
    if total_c <= 0:
        return out
    for uid, c in contributions.items():
        share = max(0, int(c)) / total_c
        out[uid] = {k: int(int(v) * share) for k, v in treasury.items()}
    return out


# ---------------------------------------------------------------------------
# 成就（§6.5）：判定纯函数（进度存取在 manager）
# ---------------------------------------------------------------------------

def achievement_unlocked(progress: dict, key: str, need: Any) -> bool:
    """通用阈值判定：need 为 int（计数型）或 None（一次性标记型）。"""
    cur = progress.get(key)
    if need is None:
        return bool(cur)
    return int(cur or 0) >= int(need)


# ---------------------------------------------------------------------------
# 坊市流将暗标（§11「天降流将」：极品/传世流亡角色，密封战功竞价）
# ---------------------------------------------------------------------------

def wanderer_auction_price(char: dict, acfg: dict,
                           season_progress: float = 0.0,
                           world_mult: float = 1.0) -> int:
    """流将底价（战功）：稀有度底价 + 超出保底六维的成长 + 特技附加。

    v4.0.19 起竞拍货币改为战功，并加入赛季进度/世界战功水位修正，
    避免后期战功膨胀后仍以极低底价扫货。
    """
    rarity = str((char or {}).get("rarity") or "epic")
    start = acfg.get("start_price") or {}
    floors = acfg.get("quality_floor") or {}
    steps = acfg.get("quality_step") or {}
    # 低稀有度流亡角色按低档兜底定价，避免凡品/良品起拍过高直接流拍。
    fallback = {
        "common": (80, 90, 2),
        "fine": (150, 125, 2),
        "rare": (260, 165, 2),
        "epic": (420, 205, 2),
        "legend": (1000, 245, 3),
    }
    fb_base, fb_floor, fb_step = fallback.get(rarity, fallback["epic"])
    base = int(start.get(rarity, fb_base))
    floor = int(floors.get(rarity, fb_floor))
    step = int(steps.get(rarity, fb_step))
    total = sum(int(v or 0) for v in ((char or {}).get("stats") or {}).values())
    price = base + max(0, total - floor) * step
    price += len((char or {}).get("traits") or []) * int(
        acfg.get("trait_bonus", 80))
    progress = max(0.0, min(1.0, float(season_progress or 0.0)))
    season_mult = 1.0 + progress * float(
        acfg.get("season_price_growth", 0.5))
    total_mult = season_mult * max(1.0, float(world_mult or 1.0))
    total_mult = max(1.0, min(float(acfg.get("max_price_mult", 2.5)),
                              total_mult))
    price = int(math.ceil(price * total_mult / 10.0) * 10)
    return max(int(acfg.get("min_price", 50)), int(price))


def wanderer_min_bid(start_price: int, current_price: int = 0,
                     has_bidder: bool = False, acfg: dict | None = None,
                     own_bid: int = 0) -> int:
    """暗标最低出价：不低于底价；已有自己的暗标时给出建议加价。"""
    acfg = acfg or {}
    floor = max(1, int(start_price))
    own = max(0, int(own_bid or 0))
    if own <= 0:
        return floor
    inc = max(int(acfg.get("min_increment_abs", 20)),
              int(own * float(acfg.get("min_increment_pct", 0.05))))
    return max(floor, own + max(1, inc))


def wanderer_quota_limit(acfg: dict, rarity: str) -> int:
    """单赛季每人可竞得的该稀有度流将数量（0/负数表示不限）。"""
    quota = (acfg or {}).get("season_quota") or {}
    try:
        return int(quota.get(rarity, 3))
    except (TypeError, ValueError):
        return 3


# ---------------------------------------------------------------------------
# 所在地 / 城镇等级 / 守军 / 民心（v4.3.5 所在地机制）
# ---------------------------------------------------------------------------

#: 据点潜力默认映射：地图生成时按类型写入，模板可用 potential 覆盖。
#: v4.5.2：遗迹由「潜力 1 的纯事件点」改为「潜力 3 的可经营据点」，与
#: 坊市同级、低于关隘/仙城，产出靠升级与城主府放大。
NODE_POTENTIAL_BY_TYPE = {
    "city": 5, "branch": 5, "pass": 4, "market": 3,
    "mine": 2, "grain": 2, "ruin": 3,
}

#: 可被人物驻守/管理/抓捕的据点类型。
#: v4.5.2：遗迹转为常规据点，允许落点/驻防/编军/派遣/关押/太守。
CHAR_DWELL_NODE_TYPES = ("city", "pass", "branch", "market", "mine", "grain",
                         "ruin")

#: 不可传送的角色状态（仅 idle 可移动）。
MOVE_BLOCK_STATUSES = ("task", "army", "captive", "transfer", "wander", "dead")

#: 本地任务：角色必须已位于目标据点。
TASK_LOCAL_TYPES = ("govern", "farm", "mining", "trade", "build", "train",
                    "heal", "seek", "quest", "research", "hunt")

#: 远征任务：落点记出发地，目标只存在任务 payload。
TASK_EXPEDITION_TYPES = ("scout", "raid", "supply")

#: 视为“驻守”的角色状态（空闲 / 本地任务）。
RESIDENT_STATUSES = ("idle", "task")

#: 非驻守角色状态（出征、被俘、押运、出走、陨落）。
NON_RESIDENT_STATUSES = ("army", "captive", "transfer", "wander", "dead")


def node_potential(node: dict | None) -> int:
    """据点潜力（1~5）：显式 potential 优先，否则按类型映射。"""
    node = node or {}
    try:
        val = int(node.get("potential") or 0)
    except (TypeError, ValueError):
        val = 0
    if val <= 0:
        val = NODE_POTENTIAL_BY_TYPE.get(str(node.get("type") or ""), 3)
    return max(1, min(5, val))


def node_upgrade_cost(level: int) -> tuple[int, int]:
    """据点升级费用（阶梯递增）：(铜钱, 精铁) = (5000×L², 500×L²)。"""
    lv = max(1, int(level or 1))
    return 5000 * lv * lv, 500 * lv * lv


def node_upgrade_ticks(level: int) -> int:
    """据点升级建造时间（tick）：2×(当前等级+1) → 4/6/8/10。"""
    return 2 * (max(1, int(level or 1)) + 1)


def garrison_cap(level: int) -> int:
    """守军上限 = 2000 × 城镇等级（玩家/NPC 统一口径）。"""
    return 2000 * max(1, int(level or 1))


def garrison_grow(cap: int, pct: float) -> int:
    """守军单 tick 增长量：至少 1，且不超过上限；``pct<=0`` 表示关闭增长。"""
    cap = max(0, int(cap or 0))
    pct = max(0.0, float(pct or 0.0))
    if cap <= 0 or pct <= 0:
        return 0
    return max(1, int(cap * pct))


def garrison_char_growth_pct(chars: Iterable[dict],
                             cfg: dict | None = None) -> float:
    """玩家驻守角色带来的守军恢复加成（占上限比例，单城封顶 5%）。

    每名驻守角色 +``garrison.char_grow_pct``（默认 1%）；武将额外
    +``general_bonus_pct``，统率 ≥ 80 的武将再 +``leadership_bonus_pct``
    （默认武将最高 +2%/人）；单城封顶 ``garrison.cap_pct``。
    调用方需自行过滤“驻守在本据点”的角色。
    """
    cfg = cfg or {}
    per = max(0.0, float(cfg.get("char_grow_pct", 0.01) or 0.0))
    general = max(0.0, float(cfg.get("general_bonus_pct", 0.005) or 0.0))
    leadership_bonus = max(0.0, float(
        cfg.get("leadership_bonus_pct", 0.01) or 0.0))
    cap = float(cfg.get("cap_pct", 0.05) or 0.0)
    total = 0.0
    for c in chars or []:
        total += per
        if str((c or {}).get("role") or "") == ROLE_GENERAL:
            total += general
            try:
                leadership = int(((c or {}).get("stats") or {}).get(
                    "leadership", 0) or 0)
            except (TypeError, ValueError):
                leadership = 0
            if leadership >= 80:
                total += leadership_bonus
    if cap > 0:
        total = min(cap, total)
    return total


def npc_output_morale_mult(morale: int) -> float:
    """NPC 据點产出民心乘数：0.5 + 民心/100 × 0.5（0→50%，100→100%）。"""
    m = max(0, min(100, int(morale or 0)))
    return 0.5 + m / 100.0 * 0.5


def morale_garrison_bonus(chars: Iterable[dict], per_cap: int = 4,
                          total_cap: int = 6) -> int:
    """魅力驻守加成：每名驻守角色 floor(魅力/25)，单角色封顶 4，单城封顶 6。

    调用方需自行过滤“驻守在本据点”的角色。
    """
    total = 0
    for c in chars or []:
        try:
            charm = int(((c or {}).get("stats") or {}).get("charm", 0) or 0)
        except (TypeError, ValueError):
            charm = 0
        total += min(max(1, int(per_cap)), max(0, charm // 25))
    return min(max(0, int(total_cap)), total)


def is_resident_char(char: dict | None) -> bool:
    """角色是否处于“驻守”状态（空闲 / 本地任务）。"""
    return str((char or {}).get("status") or "") in RESIDENT_STATUSES


def char_node_id(char: dict | None) -> int:
    """角色所在地据点 id（0 表示无落点）。"""
    try:
        return int((((char or {}).get("location") or {}).get("node_id", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def is_governor_resident(node: dict | None, char: dict | None) -> bool:
    """太守是否驻守本城：任命匹配 + 状态非出征/被俘 + 所在地为本城。"""
    node = node or {}
    char = char or {}
    if int(char.get("id") or 0) <= 0 or \
            int(node.get("governor_char_id") or 0) != int(char.get("id") or 0):
        return False
    status = str(char.get("status") or "")
    if status in NON_RESIDENT_STATUSES:
        return False
    return char_node_id(char) == int(node.get("id") or 0)


def task_is_expedition(task_type: str) -> bool:
    """是否远征类任务（侦察/劫掠/运粮：落点记出发地）。"""
    return str(task_type or "") in TASK_EXPEDITION_TYPES


def can_move_char(char: dict | None) -> bool:
    """是否允许传送（仅 idle 且未陨落/被俘）。"""
    return str((char or {}).get("status") or "") == "idle"


# ---------------------------------------------------------------------------
# NPC 威胁值与战略外交（设计文档 §5~§8）
#
# 全部为纯函数：输入已算好的数值 / 快照，返回压力、档位、概率。
# 时间一律用绝对 tick（默认 1 tick = 15 分钟），不读真实时钟。
# ---------------------------------------------------------------------------

def clamp01(value: float) -> float:
    """钳到 [0, 1]。"""
    return max(0.0, min(1.0, float(value or 0.0)))


def threat_node_value(node: dict | None, node_score: dict | None,
                      level_bonus: float = 0.10) -> float:
    """据点权重 ``V(n) = node_score[type] × (1 + level_bonus × (level - 1))``。"""
    n = node or {}
    score = float((node_score or {}).get(str(n.get("type") or ""), 1) or 1)
    level = max(1, int(n.get("level", 1) or 1))
    return score * (1.0 + float(level_bonus) * (level - 1))


def threat_share_by_faction(nodes: Iterable[dict] | None,
                            node_score: dict | None,
                            level_bonus: float = 0.10) -> dict[int, float]:
    """体量占比 ``S_T``：目标占全图已占节点权重的比例（§5.3）。"""
    total = 0.0
    owned: dict[int, float] = {}
    for n in nodes or []:
        fid = int(n.get("owner_faction", 0) or 0)
        if fid <= 0:
            continue
        value = threat_node_value(n, node_score, level_bonus)
        total += value
        owned[fid] = owned.get(fid, 0.0) + value
    if total <= 0:
        return {}
    return {fid: val / total for fid, val in owned.items()}


def threat_border_metrics(nodes_by_id: dict, obs_fid: int, tgt_fid: int,
                          node_score: dict | None,
                          level_bonus: float = 0.10,
                          border_norm: float = 8.0) -> dict:
    """接壤压力 ``B_OT`` 与边境暴露度 ``R_OT``（§5.4）。

    返回 ``{"raw": B_OT, "norm": Bn_OT, "exposed": R_OT, "border": 0~1}``。
    """
    obs_fid, tgt_fid = int(obs_fid or 0), int(tgt_fid or 0)
    raw = 0.0
    if obs_fid and tgt_fid and obs_fid != tgt_fid:
        for uid, u in nodes_by_id.items():
            if int(u.get("owner_faction", 0) or 0) != tgt_fid:
                continue
            for vid in u.get("adj") or []:
                v = nodes_by_id.get(int(vid))
                if v and int(v.get("owner_faction", 0) or 0) == obs_fid:
                    raw += threat_node_value(u, node_score, level_bonus)
                    break
    border_nodes = []
    for vid, v in nodes_by_id.items():
        if int(v.get("owner_faction", 0) or 0) != obs_fid:
            continue
        for uid in v.get("adj") or []:
            u = nodes_by_id.get(int(uid))
            if u and int(u.get("owner_faction", 0) or 0) != obs_fid:
                border_nodes.append(int(vid))
                break
    exposed = 0
    for vid in border_nodes:
        v = nodes_by_id.get(vid) or {}
        for uid in v.get("adj") or []:
            u = nodes_by_id.get(int(uid))
            if u and int(u.get("owner_faction", 0) or 0) == tgt_fid:
                exposed += 1
                break
    norm = clamp01(raw / max(1.0, float(border_norm)))
    exposed_ratio = exposed / max(1, len(border_nodes))
    return {"raw": raw, "norm": norm, "exposed": exposed_ratio,
            "border": clamp01(0.6 * norm + 0.4 * exposed_ratio)}


def threat_growth(share_now: float, share_past: float,
                  growth_norm: float = 0.10) -> float:
    """扩张速度 ``G_T``（§5.5）。"""
    delta = float(share_now or 0.0) - float(share_past or 0.0)
    return clamp01(delta / max(1e-6, float(growth_norm)))


def threat_force_ratio(near_troops: float, own_troops: float,
                       force_norm: float = 2.0) -> float:
    """边境军力 ``F_OT``（§5.6）。"""
    ratio = float(near_troops or 0.0) / max(1.0, float(own_troops or 0.0))
    return clamp01(ratio / max(1e-6, float(force_norm)))


def threat_pressure(size: float, border: float, growth: float, force: float,
                    weights: dict | None = None) -> float:
    """即时压力 ``P_raw``（§5.7），返回 0~100。"""
    w = weights or {}
    return 100.0 * (
        float(w.get("size", 0.30)) * clamp01(size)
        + float(w.get("border", 0.40)) * clamp01(border)
        + float(w.get("growth", 0.15)) * clamp01(growth)
        + float(w.get("force", 0.15)) * clamp01(force)
    )


def threat_favor_damp(favor: int, floor: float = 0.5) -> float:
    """好感衰减：0 → 1.0，100 → 0.5，下限 ``floor``（§5.8）。"""
    return max(float(floor), min(1.0, 1.0 - max(0, int(favor or 0)) / 200.0))


def threat_reputation_damp(reputation: int, righteous_penalty: float = 0.05) -> float:
    """侠名修正：声望 ≥ 60 时威胁 ×(1 − penalty)；恶名交给既有机制。"""
    if int(reputation or 0) >= 60:
        return max(0.0, 1.0 - float(righteous_penalty))
    return 1.0


def threat_adjacency_factor(border_raw: float, hegemon: bool,
                            nonadj_factor: float = 0.30,
                            hegemon_nonadj_factor: float = 0.70) -> float:
    """非接壤减益与霸主例外（§5.9）。"""
    if float(border_raw or 0.0) > 0:
        return 1.0
    return float(hegemon_nonadj_factor if hegemon else nonadj_factor)


def threat_public_floor(share: float, hegemon_threshold: float = 0.40,
                        public_base: float = 60.0,
                        public_span: float = 40.0) -> float:
    """公敌地板原始值 ``floor_raw``（§5.9）。"""
    span = max(1e-6, 1.0 - float(hegemon_threshold))
    return float(public_base) + float(public_span) * clamp01(
        (float(share or 0.0) - float(hegemon_threshold)) / span)


def threat_alpha(p_eff: float, h: float, border_raw: float, hegemon: bool,
                 alpha_up_adj: float = 0.020,
                 alpha_up_nonadj: float = 0.005,
                 alpha_up_hegemon_nonadj: float = 0.012,
                 alpha_down: float = 0.015) -> float:
    """累计敌意的收敛系数（§5.10）。"""
    if float(p_eff) >= float(h):
        if float(border_raw or 0) > 0:
            return float(alpha_up_adj)
        if hegemon:
            return float(alpha_up_hegemon_nonadj)
        return float(alpha_up_nonadj)
    return float(alpha_down)


def threat_accumulate(h: float, p_eff: float, alpha: float) -> float:
    """``H += (P_eff − H) × alpha``，钳 0~100。"""
    return max(0.0, min(100.0, float(h) + (float(p_eff) - float(h)) * float(alpha)))


def threat_mult(h: float, aggression_gain: float = 0.80,
                cap: float = 2.20) -> float:
    """威胁侵略倍率 ``1 + gain × H/100``，硬上限 ``cap``（§6）。"""
    return min(float(cap), 1.0 + float(aggression_gain) * clamp01(
        float(h or 0.0) / 100.0))


def threat_tier(h: float, tiers: dict | None = None) -> str:
    """威胁档位：``calm / alert / prepare / hostile / coalition``（§6）。"""
    t = tiers or {}
    h = float(h or 0.0)
    if h >= float(t.get("coalition", 80)):
        return "coalition"
    if h >= float(t.get("hostile", 60)):
        return "hostile"
    if h >= float(t.get("prepare", 40)):
        return "prepare"
    if h >= float(t.get("alert", 20)):
        return "alert"
    return "calm"


def threat_is_turtle(stats: dict | None, border_avg_defense: float, share: float,
                     is_player: bool, hegemon: bool,
                     cfg: dict | None = None) -> bool:
    """龟缩判定（§5.11）。全部满足才算龟缩。"""
    c = cfg or {}
    if not bool(c.get("enabled", True)):
        return False
    if not is_player:
        return False
    if hegemon and bool(c.get("exclude_hegemon", True)):
        return False
    st = stats or {}
    if st.get("captures_7d") or st.get("attacks_7d"):
        return False
    if float(border_avg_defense or 0.0) < float(c.get("defense_threshold", 1500)):
        return False
    return float(share or 0.0) < float(c.get("hegemon_share_threshold", 0.40))


def threat_priority(h: float, border_raw: float, hegemon: bool,
                    revenge: bool = False, target_at_war: bool = False,
                    power_ratio: float = 1.0, turtle: bool = False,
                    active_wars: int = 0,
                    turtle_penalty: float = 20.0) -> float:
    """宣战目标优先级（§7.3）。"""
    priority = float(h or 0.0)
    if float(border_raw or 0) > 0:
        priority += 15
    if hegemon:
        priority += 25
    if revenge:
        priority += 15
    if target_at_war:
        priority += 10
    if float(power_ratio or 0.0) < 0.60:
        priority += 10
    if turtle:
        priority -= float(turtle_penalty)
    priority -= 10.0 * max(0, int(active_wars or 0) - 1)
    return priority


def threat_declare_chance(h: float, declare_gate: float = 60.0,
                          declare_base_per_day: float = 0.06,
                          declare_span_per_day: float = 0.34,
                          max_p_day: float = 0.55,
                          goal_mult: float = 1.0,
                          personality_bias: float = 1.0,
                          power_ratio: float = 1.0,
                          min_power_ratio: float = 0.35,
                          allow_suicide_declare: bool = True,
                          suicide_declare_mult: float = 0.55,
                          turtle: bool = False,
                          turtle_declare_mult: float = 0.50) -> float:
    """每日宣战概率 ``p_day``（§7.4）。"""
    gate = float(declare_gate)
    ratio = clamp01((float(h or 0.0) - gate) / max(1e-6, 100.0 - gate))
    p_day = float(declare_base_per_day) + float(declare_span_per_day) * (
        ratio ** 1.5)
    p_day *= float(goal_mult) * float(personality_bias)
    if float(power_ratio or 0.0) < float(min_power_ratio):
        if not allow_suicide_declare:
            return 0.0
        p_day *= float(suicide_declare_mult)
    if turtle:
        p_day *= float(turtle_declare_mult)
    return max(0.0, min(float(max_p_day), p_day))


def per_day_to_per_tick(p_day: float, ticks_per_day: int = 96) -> float:
    """把「每日概率」折算为「每 tick 概率」（§7.4 / §8.4）。"""
    p = clamp01(p_day)
    tpd = max(1, int(ticks_per_day or 96))
    return 1.0 - (1.0 - p) ** (1.0 / tpd)


def threat_war_score(captures_by_me: int, captures_by_target: int,
                     capital_taken: bool = False) -> float:
    """战争顺风度 ``war_score``（§8.2，V1 只统计据点占领）。"""
    score = (50.0 + 10.0 * int(captures_by_me or 0)
             - 10.0 * int(captures_by_target or 0)
             + (20.0 if capital_taken else 0.0))
    return max(0.0, min(100.0, score))


def threat_exhaustion(troop_loss: float, treasury_stress: float,
                      morale_stress: float, no_progress: float,
                      third_party: float, lost_nodes: float,
                      lost_capital: bool, war_score: float) -> float:
    """战争疲劳 ``E``（§8.1），返回 0~100。"""
    e = 100.0 * (
        0.35 * clamp01(troop_loss)
        + 0.15 * clamp01(treasury_stress)
        + 0.10 * clamp01(morale_stress)
        + 0.15 * clamp01(no_progress)
        + 0.10 * clamp01(third_party)
        + 0.15 * clamp01(lost_nodes)
    ) + (25.0 if lost_capital else 0.0)
    e -= 0.20 * max(0.0, float(war_score or 0.0) - 50.0)
    return max(0.0, min(100.0, e))


def threat_peace_chance(exhaustion: float, peace_gate: float = 60.0,
                        peace_base_per_day: float = 0.08,
                        peace_span_per_day: float = 0.42,
                        max_peace_p_day: float = 0.60,
                        personality_bias: float = 1.0,
                        war_score: float = 50.0) -> float:
    """每日求和概率 ``p_day``（§8.4）。"""
    gate = float(peace_gate)
    ratio = clamp01((float(exhaustion or 0.0) - gate) / max(1e-6, 100.0 - gate))
    p_day = float(peace_base_per_day) + float(peace_span_per_day) * (
        ratio ** 1.2)
    p_day *= float(personality_bias)
    if float(war_score or 0.0) >= 60:
        p_day *= 0.50
    return max(0.0, min(float(max_peace_p_day), p_day))


def threat_peace_accept(target_exhaustion: float, favor: int = 0,
                        attacker_war_score: float = 50.0,
                        target_war_score: float = 50.0,
                        accept_min: float = 0.05,
                        accept_max: float = 0.95) -> float:
    """受战方接受停战的概率（§8.5）。"""
    accept = (0.15
              + 0.55 * clamp01(float(target_exhaustion or 0.0) / 100.0)
              + 0.15 * clamp01(max(0, int(favor or 0)) / 100.0)
              - 0.25 * (float(attacker_war_score or 50.0) - 50.0) / 50.0
              - 0.10 * (float(target_war_score or 50.0) - 50.0) / 50.0)
    return max(float(accept_min), min(float(accept_max), accept))


def threat_relation_key(rel: str) -> str:
    """外交关系 → ``relation_damp`` 配置键（战争与中立同档）。"""
    rel = str(rel or REL_NEUTRAL)
    if rel == REL_ALLY:
        return "ally"
    if rel == REL_TRUCE:
        return "truce"
    if rel == REL_WAR:
        return "war"
    return "neutral"
