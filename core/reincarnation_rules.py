# core/reincarnation_rules.py
"""飞升 / 弃道重修共用的永久账本与仙缘商店目录。

背景：仙缘商店卖的是「跨世永久」的东西，但旧实现里【大道馈赠】只改了
``player.mental_power`` 这类当世字段，而转生会用 ``generate_new_player_stats()``
把整组属性覆盖成新号基础值，于是 500 仙缘点买的加成会在下一次转生时静默清零。

本模块把「买到的层数」统一记进 ``player.permanent_bonus``：

* ``global_cultivation_multiplier``  修炼加速（闭关/历练修为）
* ``newlife_trips``                  转世资粮（转生后按镖银趟数发放启动灵石）
* ``dao_gift_times``                 道基淬炼（全基础属性百分比）
* ``root_reroll_stock``              灵根洗炼（额外重抽额度）
* ``breakthrough_bonus``             破境感悟（突破成功率加点）
* ``adventure_bonus``                历练心得（历练修为/灵石）
* ``rift_item_bonus``                采药心得（秘境材料掉率加点）
* ``rift_equip_bonus``               寻宝直觉（秘境装备掉率乘区）

账本只增不减，所以飞升 N 次就重放 N 次，不会重复叠加。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

# ---------- 灵根重抽 ----------

# 每一轮转生（飞升 / 弃道重修）自带的免费重抽次数，用完后消耗洗炼额度
FREE_ROOT_REROLLS = 5

# 上一世飞升时的灵根：道体重塑「必出高一档灵根」的基准，按用户存 system_config。
PREV_ROOT_KEY = "rebirth_prev_root_{uid}"


def prev_root_key(user_id: str) -> str:
    return PREV_ROOT_KEY.format(uid=user_id)

# ---------- 道基淬炼（原「大道馈赠」） ----------

# 每层把新号基础属性整体放大的比例；百分比会随境界一起成长，不会像固定 +100 那样被稀释
DAO_GIFT_PCT_PER_STACK = 0.05

# 旧存档的扁平加成字段，仅用于迁移计数（不再按扁平值重放）
DAO_GIFT_LEGACY_KEYS = (
    "mental_power",
    "max_spiritual_qi",
    "max_blood_qi",
    "magic_damage",
    "physical_damage",
    "magic_defense",
    "physical_defense",
)

# 转世资粮：每层在转生后发放「上一世巅峰境界 N 趟镖银」
# 24 趟 ≈ 0.5 天镖银（lv35 一天约 48 趟），换算成「时间」与【修炼加速】同档，
# 低境界同样约 0.6 天收入，因此不需要随境界调价。
NEWLIFE_ESCORT_TRIPS_PER_STACK = 24.0

# 飞升后的临时状态：道体未塑，等玩家重选职业 + 灵根。
# 用玩家行上的 cultivation_type 作为标记，可以随飞升事务一起提交，中断不会留下半状态。
NEWLIFE_MARKER = "待重塑"
NEWLIFE_ROOT_MARKER = "未定灵根"


# ---------- 商店目录 ----------
# key  = 账本字段
# step = 每买一层写入账本的数量（额度类为 1）
# cap  = 上限层数（0 表示无上限）
SHOP_ITEMS: Dict[int, Dict[str, Any]] = {
    1: {
        "name": "修炼加速",
        "key": "global_cultivation_multiplier",
        "cost": 100,
        "step": 0.10,
        "cap": 0,
        "unit": "10%",
        "desc": "闭关与历练修为 +10%（乘算，可无限叠加）",
        "tier": "推进",
    },
    2: {
        "name": "转世资粮",
        "key": "newlife_trips",
        "cost": 40,
        "step": NEWLIFE_ESCORT_TRIPS_PER_STACK,
        "cap": 8,
        "unit": "24 趟镖银",
        "desc": "每次转生到账「上一世巅峰境界 24 趟镖银」的启动灵石（约 0.5 天镖银）",
        "tier": "资源",
    },
    3: {
        "name": "道基淬炼",
        "key": "dao_gift_times",
        "cost": 100,
        "step": 1,
        "cap": 8,
        "unit": "5% 全属性",
        "desc": "新号基础属性 +5%（跨世重放，随境界一起放大）",
        "tier": "战力",
    },
    4: {
        "name": "灵根洗炼",
        "key": "root_reroll_stock",
        "cost": 25,
        "step": 1,
        "cap": 20,
        "unit": "1 次重抽",
        "desc": "额外灵根重抽额度（免费次数用完后消耗，跨世保留）",
        "tier": "便利",
    },
    5: {
        "name": "破境感悟",
        "key": "breakthrough_bonus",
        "cost": 80,
        "step": 0.005,
        "cap": 6,
        "unit": "0.5 点",
        "desc": "突破基础成功率 +0.5 个百分点（同时抬高破境丹上限）",
        "tier": "概率",
    },
    6: {
        "name": "历练心得",
        "key": "adventure_bonus",
        "cost": 110,
        "step": 0.12,
        "cap": 8,
        "unit": "12%",
        "desc": "历练修为与灵石收益 +12%（乘算）",
        "tier": "推进",
    },
    7: {
        "name": "采药心得",
        "key": "rift_item_bonus",
        "cost": 30,
        "step": 0.04,
        "cap": 6,
        "unit": "4 点",
        "desc": "秘境材料掉率 +4 个百分点（总掉率按 100 封顶）",
        "tier": "资源",
    },
    8: {
        "name": "寻宝直觉",
        "key": "rift_equip_bonus",
        "cost": 40,
        "step": 0.12,
        "cap": 6,
        "unit": "12%",
        "desc": "秘境装备掉率 +12%（乘算）",
        "tier": "资源",
    },
}


def item_by_key(key: str) -> Optional[Dict[str, Any]]:
    for item in SHOP_ITEMS.values():
        if item["key"] == key:
            return item
    return None


# ---------- 账本读写 ----------


def parse_bonus(player: Any) -> Dict[str, Any]:
    """读取 permanent_bonus；脏数据一律当空账本，避免转生流程被一条坏 JSON 卡死。"""
    raw = getattr(player, "permanent_bonus", "") or ""
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def ledger_value(bonus: Dict[str, Any], key: str) -> float:
    return max(0.0, _as_float((bonus or {}).get(key)))


def item_owned(bonus: Dict[str, Any], item_id: int) -> int:
    """已购层数（额度类按整数次计）。"""
    item = SHOP_ITEMS.get(item_id)
    if not item:
        return 0
    if item_id == 3:
        return dao_gift_times(bonus)
    step = float(item["step"]) or 1.0
    return int(ledger_value(bonus, item["key"]) / step + 1e-9)


def item_at_cap(bonus: Dict[str, Any], item_id: int) -> bool:
    item = SHOP_ITEMS.get(item_id)
    if not item or not item.get("cap"):
        return False
    return item_owned(bonus, item_id) >= int(item["cap"])


def add_item(bonus: Dict[str, Any], item_id: int) -> int:
    """买一层：写入账本并返回新的已购层数。"""
    item = SHOP_ITEMS[item_id]
    owned = item_owned(bonus, item_id)
    if item.get("cap") and owned >= int(item["cap"]):
        return owned
    if item_id == 3:
        # 道基淬炼按「层数」记账，顺手清掉旧存档的扁平账本，避免两套口径并存
        bonus["dao_gift_times"] = owned + 1
        bonus.pop("dao_gift", None)
        return owned + 1
    key = item["key"]
    bonus[key] = round(ledger_value(bonus, key) + float(item["step"]), 6)
    return owned + 1


# ---------- 道基淬炼 ----------


def dao_gift_times(bonus: Dict[str, Any]) -> int:
    """已购层数；兼容旧存档的扁平账本（按最小一项折算）。"""
    raw = (bonus or {}).get("dao_gift_times")
    if raw is not None:
        return max(0, int(_as_float(raw)))
    legacy = (bonus or {}).get("dao_gift")
    if isinstance(legacy, dict) and legacy:
        # 旧账本按 DAO_GIFT_PCT_PER_STACK 折算不到，直接按「买过一次」计
        return 1
    return 0


def dao_gift_pct(bonus: Dict[str, Any]) -> float:
    item = SHOP_ITEMS[3]
    times = min(dao_gift_times(bonus), int(item["cap"]))
    return times * DAO_GIFT_PCT_PER_STACK


def permanent_stats_mult(player: Any) -> float:
    """道基淬炼的全属性乘区（1.0 表示没有）。"""
    return 1.0 + dao_gift_pct(parse_bonus(player))


def scale_base_stats(player: Any, ratio: float) -> None:
    """把当前基础属性整体乘以 ratio（用于转生重放与当场购买）。"""
    if abs(ratio - 1.0) < 1e-9:
        return
    for key in DAO_GIFT_LEGACY_KEYS:
        try:
            current = int(getattr(player, key, 0) or 0)
        except (TypeError, ValueError):
            continue
        setattr(player, key, max(0, int(round(current * ratio))))


def grant_dao_gift(player: Any, bonus: Dict[str, Any]) -> None:
    """当场购买一层：立即放大现有属性，并记账供下次转生重放。"""
    add_item(bonus, 3)
    scale_base_stats(player, 1.0 + DAO_GIFT_PCT_PER_STACK)


def apply_dao_gift(player: Any, bonus: Dict[str, Any]) -> None:
    """转生重置后按账本重放（必须在基础属性生成之后调用）。"""
    scale_base_stats(player, permanent_stats_mult_from_bonus(bonus))


def permanent_stats_mult_from_bonus(bonus: Dict[str, Any]) -> float:
    return 1.0 + dao_gift_pct(bonus)


def format_dao_gift(bonus: Dict[str, Any]) -> str:
    times = dao_gift_times(bonus)
    if not times:
        return ""
    return f"道基淬炼 {times} 层（全属性 +{round(dao_gift_pct(bonus) * 100)}%）"


# ---------- 各项加成的读取口 ----------


def permanent_cultivation_mult(player: Any) -> float:
    """修炼加速的加算系数（0.10 表示 +10%）。"""
    return ledger_value(parse_bonus(player), "global_cultivation_multiplier")


def permanent_breakthrough_bonus(player: Any) -> float:
    """破境感悟：突破成功率的加点（0.005 = 0.5 个百分点）。"""
    return ledger_value(parse_bonus(player), "breakthrough_bonus")


def permanent_adventure_bonus(player: Any) -> float:
    return ledger_value(parse_bonus(player), "adventure_bonus")


def permanent_rift_item_bonus(player: Any) -> float:
    return ledger_value(parse_bonus(player), "rift_item_bonus")


def permanent_rift_equip_bonus(player: Any) -> float:
    return ledger_value(parse_bonus(player), "rift_equip_bonus")


def root_reroll_stock(player: Any) -> int:
    return int(ledger_value(parse_bonus(player), "root_reroll_stock"))


def consume_root_reroll(bonus: Dict[str, Any]) -> int:
    """消耗一次洗炼额度，返回剩余库存。"""
    stock = max(0, int(ledger_value(bonus, "root_reroll_stock")))
    if stock <= 0:
        return 0
    bonus["root_reroll_stock"] = float(stock - 1)
    return stock - 1


def newlife_trips(player: Any) -> float:
    return ledger_value(parse_bonus(player), "newlife_trips")


def escort_gold_base(level_index: int) -> int:
    """该境界「一趟镖银」的名义值（复用押镖基准，避免另造一条经济曲线）。"""
    try:
        from ..managers.escort_manager import EscortManager

        return int(EscortManager.calculate_escort_gold_base(int(level_index or 0)))
    except Exception:
        return 0


def newlife_gold(player: Any, peak_level: Optional[int] = None) -> int:
    """转生到账的启动灵石：账本层数 × 每层趟数（按上一世巅峰境界折算）。"""
    trips = newlife_trips(player)
    if trips <= 0:
        return 0
    level = int(peak_level if peak_level is not None else getattr(player, "level_index", 0) or 0)
    base = escort_gold_base(level)
    if base <= 0:
        return 0
    return int(base * trips)


def format_reroll_budget(free_left: int, stock: int) -> str:
    """灵根重抽预算文案，文本与网页端共用。"""
    return (
        f"🌀 本轮免费重抽剩余 {free_left}/{FREE_ROOT_REROLLS} 次"
        f" · 洗炼额度 {stock} 次"
    )


def format_permanent_summary(bonus: Dict[str, Any]) -> str:
    """仙缘商店顶部的「已购」摘要（空账本返回空串）。"""
    parts = []
    for item_id in sorted(SHOP_ITEMS):
        item = SHOP_ITEMS[item_id]
        owned = item_owned(bonus, item_id)
        if owned <= 0:
            continue
        if item_id == 3:
            parts.append(format_dao_gift(bonus))
        elif item_id == 4:
            parts.append(f"灵根洗炼 {owned} 次")
        elif item_id == 2:
            parts.append(f"转世资粮 {owned} 层（{owned * NEWLIFE_ESCORT_TRIPS_PER_STACK:g} 趟镖银）")
        else:
            parts.append(f"{item['name']} {owned} 层")
    return " · ".join(p for p in parts if p)
