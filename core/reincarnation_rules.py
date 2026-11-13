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

# 上一世飞升时的灵根：道体重塑「保底灵根」的基准，按用户存 system_config。
PREV_ROOT_KEY = "rebirth_prev_root_{uid}"

# 本轮已经抽了几批灵根（首次抽取算第 1 批，每次重抽 +1），按用户存 system_config
ROOT_ROLL_KEY = "rebirth_root_roll_{uid}"

# 每隔多少批给一次「比上一世高一档」。其余批次只保「与上一世同档」。
# 免费重抽正好 5 次，所以第 5 批（最后一次免费）一定是升档批。
ROOT_TIER_GUARANTEE_EVERY = 5


def prev_root_key(user_id: str) -> str:
    return PREV_ROOT_KEY.format(uid=user_id)


def root_roll_key(user_id: str) -> str:
    return ROOT_ROLL_KEY.format(uid=user_id)

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
# group = 商店面板的分组
#
# 定价口径：数值型全部无上限（cap=0），玩家愿意肝就一直变强；价格按「和现有项同档」定，
# 40~120 点为主，结构性解锁（槽位/品阶/档位）200~400。
# 只有系统本身容纳不下的项才留硬顶（封印位 2、灵兽栏 6、装备品阶 1 阶…），
# 否则会出现「无限封印位 / 无限品阶」这种没法结算的状态。

_SHOP_DEFS = (
    # (id, group, 名称, 账本字段, 价格, 每层写入, 上限, 单位, 说明)
    # ---- 修为与产出 ----
    (1, "修为", "修炼加速", "global_cultivation_multiplier", 100, 0.10, 0, "10%",
     "闭关与历练修为 +10%（乘算）"),
    (6, "修为", "历练心得", "adventure_bonus", 110, 0.12, 0, "12%",
     "历练修为与灵石 +12%（乘算）"),
    (9, "修为", "灵眼共鸣", "spirit_eye_bonus", 70, 0.10, 0, "10%",
     "灵眼收取的修为 +10%"),
    (10, "修为", "洞天造化", "blessed_land_bonus", 60, 0.10, 0, "10%",
     "洞天福地的修为与灵石产出 +10%"),
    (11, "修为", "双修心得", "dual_cultivation_bonus", 60, 0.10, 0, "10%",
     "双修获得的修为 +10%"),
    (12, "修为", "丹道精研", "pill_exp_bonus", 80, 0.08, 0, "8%",
     "修为丹提供的修为 +8%"),
    (13, "修为", "灵田丰饶", "spirit_farm_bonus", 50, 0.10, 0, "10%",
     "灵田每次收获的灵石与修为 +10%"),
    (14, "修为", "悬赏通缉", "bounty_bonus", 60, 0.10, 0, "10%",
     "悬赏任务奖励 +10%"),
    # ---- 战斗属性 ----
    (3, "战斗", "道基淬炼", "dao_gift_times", 100, 1, 0, "5% 全属性",
     "新号基础属性 +5%（跨世重放，随境界放大）"),
    (15, "战斗", "先天杀伐", "crit_rate_bonus", 60, 0.01, 0, "1 点",
     "暴击率 +1 个百分点"),
    (16, "战斗", "破军之势", "crit_damage_bonus", 50, 0.06, 0, "6%",
     "暴击伤害 +6%"),
    (17, "战斗", "疾风连击", "combo_rate_bonus", 60, 0.01, 0, "1 点",
     "连击率 +1 个百分点"),
    (18, "战斗", "血煞之体", "lifesteal_bonus", 70, 0.005, 0, "0.5 点",
     "吸血 +0.5 个百分点"),
    (19, "战斗", "不动明王", "counter_bonus", 50, 0.01, 0, "1 点",
     "反击率 +1 个百分点"),
    (20, "战斗", "踏雪无痕", "dodge_bonus", 60, 0.008, 0, "0.8 点",
     "闪避 +0.8 个百分点"),
    (21, "战斗", "五行亲和", "element_resonance_bonus", 90, 0.08, 0, "8%",
     "五行共鸣的所有加成 +8%"),
    (22, "战斗", "御兽同心", "pet_guard_bonus", 50, 0.02, 0, "2 点",
     "灵兽守护触发率 +2 个百分点"),
    # ---- 概率 ----
    (5, "概率", "破境感悟", "breakthrough_bonus", 80, 0.005, 0, "0.5 点",
     "突破基础成功率 +0.5 个百分点（同时抬高破境丹上限）"),
    (23, "概率", "丹火纯青", "alchemy_success_bonus", 70, 0.02, 0, "2 点",
     "炼丹成功率 +2 个百分点"),
    (24, "概率", "一炉双丹", "alchemy_double_bonus", 120, 0.03, 0, "3 点",
     "炼丹双倍产出概率 +3 个百分点"),
    (25, "概率", "百炼成钢", "forge_success_bonus", 70, 0.02, 0, "2 点",
     "锻造成功率 +2 个百分点"),
    (26, "概率", "天工开物", "forge_affix_bonus", 250, 1, 3, "1 档",
     "锻造附魔词条数值 +15%/档（最多 3 档）"),
    (27, "概率", "雷劫不侵", "tribulation_resist_bonus", 90, 0.05, 0, "5%",
     "天劫与魔潮的伤害抗性 +5%"),
    (28, "概率", "秘境老手", "rift_score_bonus", 60, 0.05, 0, "5%",
     "秘境通关结算的灵石与修为 +5%"),
    # ---- 掉落 ----
    (7, "掉落", "采药心得", "rift_item_bonus", 30, 0.04, 0, "4 点",
     "秘境材料掉率 +4 个百分点（总掉率按 100 封顶）"),
    (8, "掉落", "寻宝直觉", "rift_equip_bonus", 40, 0.12, 0, "12%",
     "秘境装备掉率 +12%（乘算）"),
    (29, "掉落", "天机感应", "rare_drop_bonus", 100, 0.05, 0, "5%",
     "所有稀有物品掉率 +5%（乘算）"),
    (30, "掉落", "功法天授", "technique_drop_bonus", 80, 0.06, 0, "6%",
     "功法秘籍掉率 +6%（乘算）"),
    (31, "掉落", "灵材辨识", "material_quality_bonus", 150, 1, 3, "1 档",
     "材料判定品阶 +1 档"),
    (32, "掉落", "炼器宗师", "equipment_tier_bonus", 400, 1, 1, "1 阶",
     "秘境装备品阶上限 +1 阶（只可一次）"),
    (33, "掉落", "灵眼扩容", "spirit_eye_cap_bonus", 80, 2, 0, "2 小时",
     "灵眼单次收取的时长上限 +2 小时（原本 24 小时）"),
    # ---- 资源 ----
    (2, "资源", "转世资粮", "newlife_trips", 40, NEWLIFE_ESCORT_TRIPS_PER_STACK, 0, "24 趟镖银",
     "每次转生到账「上一世巅峰境界 24 趟镖银」的启动灵石"),
    (34, "资源", "财源广进", "gold_bonus_permanent", 100, 0.10, 0, "10%",
     "所有灵石收益 +10%（乘算）"),
    # ---- 便利与容量 ----
    (4, "便利", "灵根洗炼", "root_reroll_stock", 25, 1, 0, "1 次",
     "额外灵根重抽额度（免费次数用完后消耗）"),
    (35, "便利", "芥子纳须", "storage_slots_bonus", 40, 4, 0, "4 格",
     "储物戒容量 +4 格"),
    (36, "便利", "藏经阁", "warehouse_slots_bonus", 40, 10, 0, "10 格",
     "洞天仓库容量 +10 格"),
    (37, "便利", "拍卖贵宾", "auction_fee_bonus", 90, 0.10, 0, "10%",
     "拍卖行手续费与上架费 -10%（按原价折扣，最多免费）"),
    (38, "便利", "天生道种", "free_root_rerolls", 50, 1, 0, "1 次",
     "每轮免费灵根重抽 +1 次"),
    (39, "便利", "御兽栏", "pet_slot_bonus", 120, 1, 6, "1 位",
     "可携带灵兽 +1 只（最多 6 只）"),
    (40, "便利", "灵兽潜能", "pet_level_cap_bonus", 80, 5, 0, "5 级",
     "灵兽等级上限 +5"),
    (41, "便利", "纳物铭文", "ring_slots_bonus", 60, 6, 0, "6 格",
     "储物戒容量 +6 格（与芥子纳须叠加）"),
    (42, "便利", "悟道蒲团", "seclusion_bonus", 70, 0.08, 0, "8%",
     "闭关打坐的修为收益 +8%（与修炼加速相乘）"),
    (52, "便利", "身外化身", "avatar_slots", 60, 1, 4, "1 尊",
     "多一个活动分身，可同时再挂一项活动（阶梯价 60/120/240/480，最多 4 尊）"),
    # ---- 跨世 ----
    (43, "跨世", "灵根天眷", "root_tier_bonus", 300, 1, 3, "1 档",
     "每世首批灵根的保底档位 +1 档（最高档封顶）"),
    (44, "跨世", "万世传承", "heirloom_slot_bonus", 300, 1, 2, "1 档",
     "解封传家宝的境界门槛每档降低 3 级（原需筑基）"),
    (45, "跨世", "宿慧觉醒", "start_level_bonus", 200, 2, 0, "2 级",
     "转生开局直接 +2 级（不超过上一世巅峰）"),
    (46, "跨世", "一脉相承", "keep_life_skill", 250, 1, 1, "1 个",
     "转生时保留生活职业（只可一次）"),
    (47, "跨世", "前世余荫", "keep_gold_pct", 120, 0.03, 0, "3%",
     "转生时额外保留上一世 3% 的灵石"),
    # ---- 荣誉 ----
    (48, "荣誉", "仙缘铭牌", "rank_badge", 200, 1, 1, "1 枚",
     "排行榜与个人信息展示仙缘铭牌（纯展示）"),
    (49, "荣誉", "飞升图鉴", "ascension_codex", 300, 1, 1, "1 部",
     "记录每一世的灵根与巅峰境界（纯收藏）"),
    (50, "荣誉", "洞天外观", "cosmetic_skin", 100, 1, 1, "1 款",
     "洞天福地专属外观（纯展示）"),
    (51, "荣誉", "仙缘称号", "fate_title", 150, 1, 3, "1 阶",
     "专属称号【仙缘·命主】，三阶递进（纯展示 + 1% 全属性/阶）"),
)

SHOP_ITEMS: Dict[int, Dict[str, Any]] = {
    item_id: {
        "name": name,
        "key": key,
        "cost": cost,
        "step": step,
        "cap": cap,
        "unit": unit,
        "desc": desc,
        "group": group,
    }
    for item_id, group, name, key, cost, step, cap, unit, desc in _SHOP_DEFS
}

# 面板分组顺序（组名, 一句话说明）
SHOP_GROUPS: tuple = (
    ("修为", "修为与各类产出的永久提升"),
    ("战斗", "面板属性与战斗触发"),
    ("概率", "突破 / 炼丹 / 锻造 / 天劫"),
    ("掉落", "材料 / 装备 / 功法 / 稀有"),
    ("资源", "灵石与转生启动资金"),
    ("便利", "容量 / 次数 / 上限"),
    ("跨世", "转生时继承的东西"),
    ("荣誉", "纯展示的收藏与铭牌"),
)


def items_in_group(group: str) -> list:
    """某分组的商品 id（按表中顺序）。"""
    return [i for i, item in SHOP_ITEMS.items() if item.get("group") == group]


def item_by_key(key: str) -> Optional[Dict[str, Any]]:
    for item in SHOP_ITEMS.values():
        if item["key"] == key:
            return item
    return None


def shop_price(item_id: int, owned: int = 0) -> int:
    """下一份的价钱。

    绝大多数商品是固定价；【身外化身】走阶梯价（60/120/240/480），
    所以价格一律从这里取，别直接读 ``item["cost"]``。
    """
    if int(item_id) == 52:
        from .avatar_rules import avatar_price_at

        # 买满 4 尊后返回 0（调用方按「已满」展示，购买前会先被 item_at_cap 拦住）
        return int(avatar_price_at(int(owned or 0)) or 0)
    item = SHOP_ITEMS.get(int(item_id))
    return int(item["cost"]) if item else 0


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
    """道基淬炼的全属性加成（无上限，层数 × 5%）。"""
    return dao_gift_times(bonus) * DAO_GIFT_PCT_PER_STACK


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


FATE_TITLE_PCT_PER_STACK = 0.01


def fate_title_pct(bonus: Dict[str, Any]) -> float:
    """【仙缘称号】每阶 +1% 全属性（纯展示之外的那一点实在好处）。"""
    return max(0, int(ledger_value(bonus, "fate_title"))) * FATE_TITLE_PCT_PER_STACK


def permanent_stats_mult_from_bonus(bonus: Dict[str, Any]) -> float:
    return (1.0 + dao_gift_pct(bonus)) * (1.0 + fate_title_pct(bonus))


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


def permanent_effect(player: Any, key: str) -> float:
    """任意仙缘账本字段的读数（51 项商品共用这一个入口）。

    返回的是「累计数值」而不是层数：比如 crit_rate_bonus 买 3 层返回 0.03。
    各消费点自己决定怎么用（加算 / 乘算 / 取整），不要在这里做口径转换。
    """
    return ledger_value(parse_bonus(player), key)


def permanent_effect_from_bonus(bonus: Dict[str, Any], key: str) -> float:
    return ledger_value(bonus, key)


def clamp01(value: float, upper: float = 1.0) -> float:
    """概率类叠加的自然封顶（商品无上限，但概率不能超过 upper）。"""
    return max(0.0, min(float(upper), float(value)))


# ---------- 灵石兑换仙缘点 ----------

# 1 点 = 1000 万灵石；每世最多换 100 点（= 1 亿灵石）
GOLD_PER_FATE_POINT = 10_000_000
FATE_EXCHANGE_MAX = 100
FATE_EXCHANGE_KEY = "fate_gold_exchange_{uid}"


def fate_exchange_key(user_id: str) -> str:
    return FATE_EXCHANGE_KEY.format(uid=user_id)


def fate_exchange_points(gold: int) -> int:
    """给定灵石能换多少点（向下取整）。"""
    if gold <= 0:
        return 0
    return int(gold // GOLD_PER_FATE_POINT)


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


# ---------- 跨世商品 ----------


def keep_gold_ratio(bonus: Dict[str, Any]) -> float:
    """【前世余荫】转生时额外保留的上一世灵石比例。

    商品本身不封顶，但功能上最多只能把「全部灵石」带过来，所以硬顶 90%：
    留 10% 的落差，飞升才不至于彻底没有压力。
    """
    return min(0.90, ledger_value(bonus, "keep_gold_pct"))


def keeps_life_skill(bonus: Dict[str, Any]) -> bool:
    """【一脉相承】是否买过（买过一次终身保留生活职业）。"""
    return ledger_value(bonus, "keep_life_skill") >= 1


def start_level_gift(bonus: Dict[str, Any]) -> int:
    """【宿慧觉醒】转生开局直接送的境界等级。"""
    return int(ledger_value(bonus, "start_level_bonus"))


def heirloom_unseal_level(bonus: Dict[str, Any]) -> int:
    """【万世传承】把解封传家宝的境界门槛每档降 3 级（最低 0）。"""
    stacks = int(ledger_value(bonus, "heirloom_slot_bonus"))
    return max(0, 10 - 3 * stacks)


def apply_start_level(player: Any, bonus: Dict[str, Any], peak_level: int, level_data) -> int:
    """按【宿慧觉醒】把开局境界直接顶上去，并补发对应的突破属性。

    只加「等级 + 突破成长」，不给修为：否则等于白送一次突破消耗。
    返回实际提升的级数。
    """
    gift = start_level_gift(bonus)
    if gift <= 0 or not level_data:
        return 0
    target = min(int(peak_level or 0), gift, len(level_data) - 1)
    gained = 0
    for idx in range(1, target + 1):
        row = level_data[idx] or {}
        player.level_index = idx
        try:
            player.lifespan = int(player.lifespan or 0) + int(row.get("breakthrough_lifespan_gain", 0) or 0)
            player.physical_damage = int(player.physical_damage or 0) + int(row.get("breakthrough_physical_damage_gain", 0) or 0)
            player.magic_damage = int(player.magic_damage or 0) + int(row.get("breakthrough_magic_damage_gain", 0) or 0)
            player.physical_defense = int(player.physical_defense or 0) + int(row.get("breakthrough_physical_defense_gain", 0) or 0)
            player.magic_defense = int(player.magic_defense or 0) + int(row.get("breakthrough_magic_defense_gain", 0) or 0)
            player.mental_power = int(player.mental_power or 0) + int(row.get("breakthrough_mental_power_gain", 0) or 0)
            if getattr(player, "cultivation_type", "") == "体修":
                player.max_blood_qi = int(player.max_blood_qi or 0) + int(row.get("breakthrough_blood_qi_gain", 0) or 0)
                player.blood_qi = player.max_blood_qi
            else:
                player.max_spiritual_qi = int(player.max_spiritual_qi or 0) + int(row.get("breakthrough_spiritual_qi_gain", 0) or 0)
                player.spiritual_qi = player.max_spiritual_qi
        except (TypeError, ValueError):
            continue
        gained = idx
    return gained


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


def free_root_rerolls(player: Any) -> int:
    """本轮免费灵根重抽次数：基础 5 次 + 商店【天生道种】的永久加次。"""
    return FREE_ROOT_REROLLS + int(ledger_value(parse_bonus(player), "free_root_rerolls"))


def format_reroll_budget(free_left: int, stock: int, total: int = FREE_ROOT_REROLLS) -> str:
    """灵根重抽预算文案，文本与网页端共用。"""
    return (
        f"🌀 本轮免费重抽剩余 {free_left}/{total} 次"
        f" · 洗炼额度 {stock} 次"
    )


def format_permanent_summary(bonus: Dict[str, Any], detail: bool = False) -> str:
    """仙缘加成的摘要：默认按分组计数，detail=True 时逐项列出（空账本返回空串）。

    商品扩到 51 项后逐项拼接会撑爆飞升结算页，所以默认只给「修为 5 项 · 战斗 3 项」。
    """
    owned_ids = [i for i in SHOP_ITEMS if item_owned(bonus, i) > 0]
    if not owned_ids:
        return ""
    if detail:
        parts = []
        for item_id in owned_ids:
            item = SHOP_ITEMS[item_id]
            n = item_owned(bonus, item_id)
            if item_id == 3:
                parts.append(format_dao_gift(bonus))
            elif item_id == 2:
                parts.append(f"转世资粮 {n} 层（{n * NEWLIFE_ESCORT_TRIPS_PER_STACK:g} 趟镖银）")
            else:
                parts.append(f"{item['name']} {n} 层")
        return " · ".join(p for p in parts if p)
    grouped: Dict[str, int] = {}
    for item_id in owned_ids:
        group = SHOP_ITEMS[item_id].get("group") or "其他"
        grouped[group] = grouped.get(group, 0) + 1
    order = [g for g, _ in SHOP_GROUPS] + [g for g in grouped if g not in dict(SHOP_GROUPS)]
    return " · ".join(f"{g} {grouped[g]} 项" for g in order if g in grouped)
