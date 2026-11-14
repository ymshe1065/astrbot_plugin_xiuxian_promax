"""仙缘商店定价配平：以【修炼加速】为锚，把 52 项逐项折算成同一把尺子。

锚（用户定的基准）
------------------
【修炼加速】100 点 = 闭关修为 **+10%**。

参考玩家（lv34 → 混元先天，突破需求 35e9）在**满配**下的速度：
  闭关 0.25%/h × 满配 20 倍 ≈ 5%/h
  历练 0.75%/h × 满配 6.7 倍 ≈ 5%/h
两者各占一个活动槽、可以同时挂，所以「总修为速度」≈ 10%/h。
【修炼加速】`global_cultivation_multiplier` **同时乘在闭关和历练上**
（core/cultivation_manager.py:505 与 managers/adventure_manager.py:630），
所以它吃满 100% 总速度：**+10% = 10 个百分点 = 100 点**
⇒ **1% 总修为速度 = 10 点**；1% 战力按同一价格折算（战力与进度同权，玩家对战力有直接体感）。

其余来源在 lv34 的量级（都按代码里的公式实算，见下方常量）：
  灵眼 3 万/h、洞天封顶 10 万/h、灵田 12.5 万/h、悬赏约 1.5 千/h、修为丹 3450 万/颗 × 10 颗/天
  → 相对 35e9 的突破需求都是千分之几到十万分之几，所以挂在这些系统上的百分比商品
    按真实收益只值 10 点上下（低于 10 点按下限收，避免出现「白送」）。
  双修一次 = 对方总修为的 10%，1 小时冷却 → 有效权重按总速度的一半算。

不在本表内的三类
----------------
* **掉落组**：已在 v3.45.6 按秘境材料/装备的金币产出单独配平过，本次不动。
* **跨世 / 荣誉**：一次性结构与纯展示，没有可叠加的数值，价格由「满层 1~3 次飞升」定，不动。
* **结构解锁**（御兽栏 / 身外化身 / 天工开物 / 灵材辨识 / 炼器宗师）：硬顶本身决定价值，只做边界检查。

用法
----
    python scripts/fate_shop_balance.py           # 重写 docs/仙缘商店定价表.md
    python scripts/fate_shop_balance.py --check   # 校验表与 _SHOP_DEFS 是否同步
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES_FILE = ROOT / "core" / "reincarnation_rules.py"
DOC_FILE = ROOT / "docs" / "仙缘商店定价表.md"

MARK_BEGIN = "<!-- FATE_SHOP_BALANCE_AUTO_BEGIN -->"
MARK_END = "<!-- FATE_SHOP_BALANCE_AUTO_END -->"

# ---------- 模型常量（改数值必须连同文档一起重算） ----------

ANCHOR_ID = 1               # 修炼加速
ANCHOR_PRICE = 100          # 锚价
ANCHOR_EFFECT = 0.10        # +10%
ANCHOR_SCOPE = 1.0          # 修炼加速同时吃闭关与历练，覆盖 100% 总速度
SECLUSION_SHARE = 0.5       # 闭关在总修为速度里的占比（满配下闭关/历练同速）
ANCHOR_PP = ANCHOR_EFFECT * ANCHOR_SCOPE         # 10 个百分点
POINT_PER_PP = ANCHOR_PRICE / ANCHOR_PP          # 10 点 / 1% 总速度
FLOOR_PRICE = 10            # 数值项下限：收益再小也不白送

NEED_LV34 = 35_000_000_000  # config/level_config.json[34].exp_needed

# 各来源在 lv34 每小时的实际产出（折成「占总修为速度的比例」用）
SECLUSION_HOURLY = NEED_LV34 * 0.0025 * 20     # 满配闭关 ≈ 5%/h
ADVENTURE_HOURLY = NEED_LV34 * 0.0075 * 6.7    # 满配历练 ≈ 5%/h
TOTAL_HOURLY = SECLUSION_HOURLY + ADVENTURE_HOURLY

SPIRIT_EYE_HOURLY = 30_000                     # 极品灵眼，spirit_eye_manager 上限
BLESSED_LAND_HOURLY = 100_000                  # 洞天福地 max_exp_per_hour 封顶
SPIRIT_FARM_HOURLY = 12_500                    # 40 格 × 37500 修为 / 12h 茬
BOUNTY_HOURLY = 1_500                          # 模板 2500 修为 × 等级 2.86 × 一天几次
EXP_PILL_HOURLY = 34_500_000 * 10 / 24         # 顶配修为丹 3450 万 × 一天 10 颗
DUAL_CULT_HOURLY = TOTAL_HOURLY * 1.0          # 双修 1 小时一次、约等于「闭关+历练」合计

GOLD_PER_HOUR = 405_000 * 48 / 24              # 押镖 81 万灵石/小时

# 每层效果里「来源占比」的算法：效果 × 该来源占总速度的比例
SHARES = {
    "闭关": SECLUSION_SHARE,
    "历练": ADVENTURE_HOURLY / TOTAL_HOURLY,
    "灵眼": SPIRIT_EYE_HOURLY / TOTAL_HOURLY,
    "洞天": BLESSED_LAND_HOURLY / TOTAL_HOURLY,
    "灵田": SPIRIT_FARM_HOURLY / TOTAL_HOURLY,
    "悬赏": BOUNTY_HOURLY / TOTAL_HOURLY,
    "丹药": EXP_PILL_HOURLY / TOTAL_HOURLY,
    "双修": DUAL_CULT_HOURLY / TOTAL_HOURLY,
}


def _round5(value: float) -> int:
    return int(round(value / 5.0)) * 5


def _progress_price(source: str, effect: float) -> int:
    """修为类：效果 × 来源占比 → 总速度 pp → 点数。"""
    pp = effect * SHARES[source]
    return max(FLOOR_PRICE, _round5(pp * POINT_PER_PP))


def _power_price(power_pct: float) -> int:
    """战斗类：战力 % 与修为速度为同一价格（入参是百分点，例如 5.0 表示 +5%）。"""
    return max(FLOOR_PRICE, _round5(power_pct / 100.0 * POINT_PER_PP))


# ---------- 逐项定价表 ----------
# (编号, 分类, 每层效果文字, 建议价, 依据)
# 分类：修为 / 战斗 / 概率 / 资源 / 便利 / 结构 / 不动

def build_rows() -> list:
    rows = []

    def add(item_id, kind, effect, price, reason):
        rows.append({
            "id": item_id, "kind": kind, "effect": effect,
            "price": int(price), "reason": reason,
        })

    # ---- 修为：按「效果 × 来源占比」折算 ----
    add(1, "修为", "闭关与历练修为 +10%", ANCHOR_PRICE, "锚：闭关与历练各 +10% = +10% 总速度")
    add(6, "修为", "历练修为/灵石 +12%", _progress_price("历练", 0.12), "只作用历练那一半速度")
    add(11, "修为", "双修修为 +10%", _progress_price("双修", 0.10),
        "双修按对方总修为的 10% 结算，机制偏强 → 限购 5 层兜底")
    add(9, "修为", "灵眼收取修为 +10%", _progress_price("灵眼", 0.10), "灵眼 3 万/时，占总量万分之一")
    add(10, "修为", "洞天修为/灵石 +10%", _progress_price("洞天", 0.10), "洞天封顶 10 万/时")
    add(12, "修为", "修为丹修为 +8%", _progress_price("丹药", 0.08), "丹药约占总量 0.4%")
    add(13, "修为", "灵田灵石/修为 +10%", _progress_price("灵田", 0.10), "灵田 12.5 万/时")
    add(14, "修为", "悬赏奖励 +10%", _progress_price("悬赏", 0.10), "悬赏修为几乎可忽略")

    # ---- 战斗：战力 % × 20 点 ----
    add(3, "战斗", "新号基础属性 +5%", _power_price(5.0), "组锚：+5% 全属性 ≈ +5% 战力")
    add(15, "战斗", "暴击率 +1 点", _power_price(0.5), "基础暴击约 10%、1.5 倍伤害 → DPS +0.5%")
    add(16, "战斗", "暴击伤害 +6%", _power_price(0.6), "1.5→1.56，按 10% 暴击率摊薄")
    add(17, "战斗", "连击率 +1 点", _power_price(1.0), "连击直接多一次普攻")
    add(18, "战斗", "吸血 +0.5 点", _power_price(0.4), "续航，按半个生存档")
    add(19, "战斗", "反击率 +1 点", _power_price(0.5), "半次普攻")
    add(20, "战斗", "闪避 +0.8 点", _power_price(0.8), "等效生存 +0.8%")
    add(21, "战斗", "五行共鸣加成 +8%", _power_price(2.4), "共鸣总加成约 +30% × 8%")
    add(22, "战斗", "灵兽守护触发 +2 点", _power_price(1.0), "减伤触发，按 1% 生存")

    # ---- 概率：省下的突破/重炼时间 ----
    add(5, "概率", "突破成功率 +0.5 点", 30, "混元突破 20 次期望省 1 次 ≈ 20h 进度")
    add(23, "概率", "炼丹成功率 +2 点", 10, "省重炼材料与时间，量级很小")
    add(24, "概率", "炼丹双倍概率 +3 点", 10, "丹药产出 +3%")
    add(25, "概率", "锻造成功率 +2 点", 10, "省重铸材料")
    add(26, "概率", "锻造词条 +15%/档", 20, "战力 +2%/档，最多 3 档")
    add(27, "概率", "天劫抗性 +5%", 30, "少掉境界的保险，非收益")
    add(28, "概率", "秘境结算 +5%", 10, "秘境修为/灵石只占零头")

    # ---- 资源：灵石不直接换进度，按「满层 1~3 次飞升」的绝对价 ----
    add(2, "资源", "转世资粮 24 趟镖银", 40, "0.5 天镖银，已按时间校准")
    add(34, "资源", "灵石收益 +10%", 30, "满级每天多 194 万 ≈ 3 趟镖；灵石不直接换进度，按便利档收")

    # ---- 便利：容量/次数，按「一次功能 = 25 点上下」 ----
    add(4, "便利", "灵根重抽 +1 次", 25, "基准额度的单价")
    add(38, "便利", "免费重抽 +1 次", 25, "同上")
    add(37, "便利", "拍卖手续费 -10%", 30, "省手续费，量级小")
    add(35, "便利", "储物戒 +4 格", 25, "容量类统一档")
    add(36, "便利", "仓库 +10 格", 25, "容量类统一档")
    add(41, "便利", "储物戒 +6 格", 30, "容量类统一档")
    add(40, "便利", "灵兽等级上限 +5", 40, "影响灵兽战力上限")
    add(42, "便利", "闭关收益 +8%", _progress_price("闭关", 0.08), "只作用闭关那一半速度")

    # ---- 结构解锁：硬顶决定价值，只做边界检查 ----
    add(39, "结构", "可携带灵兽 +1（6 位）", 120, "满层 720 点 ≈ 2 次飞升")
    add(52, "结构", "活动槽位 +1（4 尊）", 60, "阶梯价 60/120/240/480 不变")
    return rows


def load_shop_defs() -> dict:
    """直接 exec 规则文件读 _SHOP_DEFS。

    这个模块只依赖 json / typing，没有 astrbot 依赖，所以可以脱离插件包单独跑；
    用 exec 而不是 ast.literal_eval，是因为表里有 ``NEWLIFE_ESCORT_TRIPS_PER_STACK`` 这种常量引用。
    """
    ns: dict = {"__name__": "reincarnation_rules_standalone", "__file__": str(RULES_FILE)}
    exec(compile(RULES_FILE.read_text(encoding="utf-8"), str(RULES_FILE), "exec"), ns)
    data = ns["_SHOP_DEFS"]
    return {
        int(row[0]): {
            "group": row[1], "name": row[2], "key": row[3],
            "cost": int(row[4]), "step": row[5], "cap": int(row[6]),
            "unit": row[7],
        }
        for row in data
    }


def _shop_groups() -> tuple:
    ns: dict = {"__name__": "reincarnation_rules_standalone", "__file__": str(RULES_FILE)}
    exec(compile(RULES_FILE.read_text(encoding="utf-8"), str(RULES_FILE), "exec"), ns)
    return ns["SHOP_GROUPS"]


def build_doc() -> str:
    shop = load_shop_defs()
    rows = build_rows()
    by_id = {r["id"]: r for r in rows}

    lines = [
        f"参考玩家：lv34 → 混元先天，突破需求 {NEED_LV34:,}；"
        f"满配总修为速度 ≈ {TOTAL_HOURLY / NEED_LV34 * 100:.1f}%/h",
        "",
        f"锚：{shop[ANCHOR_ID]['name']} {ANCHOR_PRICE} 点 = 闭关与历练各 +10% = +{ANCHOR_PP * 100:.0f}% 总速度"
        f" ⇒ **1% 总速度 = {POINT_PER_PP / 100:.0f} 点**（战力同价）",
        "",
        "| 编号 | 商品 | 分组 | 每层效果 | 现值 | 建议价 | 依据 |",
        "| ---: | --- | --- | --- | ---: | ---: | --- |",
    ]
    group_order = [name for name, _ in _shop_groups()]
    for item_id in sorted(by_id, key=lambda i: (
        group_order.index(shop[i]["group"]) if shop.get(i) and shop[i]["group"] in group_order else 99, i
    )):
        item = shop.get(item_id)
        if not item:
            continue
        row = by_id[item_id]
        mark = "" if row["price"] == item["cost"] else " ⚠️"
        lines.append(
            f"| {item_id} | {item['name']} | {item['group']} | {row['effect']} | "
            f"{item['cost']}{mark} | {row['price']} | {row['reason']} |"
        )

    untouched = [
        (i, item) for i, item in sorted(shop.items())
        if i not in by_id and item["group"] not in ("跨世", "荣誉")
    ]
    lines += [
        "",
        "**不动（掉落组已单独配平）**",
        "",
        "| 编号 | 商品 | 每层 | 现值 |",
        "| ---: | --- | --- | ---: |",
    ]
    for i, item in untouched:
        lines.append(f"| {i} | {item['name']} | {item['unit']} | {item['cost']} |")

    lines += [
        "",
        "**不动（一次性 / 纯展示，价格由「满层 1~3 次飞升」定）**",
        "",
        "| 编号 | 商品 | 分组 | 现值 | 上限 |",
        "| ---: | --- | --- | ---: | ---: |",
    ]
    for i, item in sorted(shop.items()):
        if i in by_id or item["group"] not in ("跨世", "荣誉"):
            continue
        seen = set()
        if i in seen:
            continue
        lines.append(f"| {i} | {item['name']} | {item['group']} | {item['cost']} | {item['cap'] or '—'} |")
    return "\n".join(lines)


def diff_report() -> list:
    """建议价与现值不一致的项。"""
    shop = load_shop_defs()
    out = []
    for row in build_rows():
        item = shop.get(row["id"])
        if item and item["cost"] != row["price"]:
            out.append((row["id"], item["name"], item["cost"], row["price"]))
    return out


def main() -> int:
    check = "--check" in sys.argv
    table = build_doc()
    block = f"{MARK_BEGIN}\n{table}\n{MARK_END}"

    if check:
        ok = DOC_FILE.exists() and block in DOC_FILE.read_text(encoding="utf-8")
        changes = diff_report()
        if not ok:
            print("仙缘商店定价表已过期，请运行 python scripts/fate_shop_balance.py")
        if changes:
            print("以下商品价格与模型不一致：")
            for i, name, old, new in changes:
                print(f"  {i:>2} {name}: {old} → {new}")
        return 0 if (ok and not changes) else 1

    if DOC_FILE.exists():
        current = DOC_FILE.read_text(encoding="utf-8")
        if MARK_BEGIN in current and MARK_END in current:
            start = current.index(MARK_BEGIN)
            end = current.index(MARK_END) + len(MARK_END)
            new = current[:start] + block + current[end:]
        else:
            new = current + "\n" + block + "\n"
    else:
        new = (
            "# 仙缘商店定价表\n\n"
            "> 由 `scripts/fate_shop_balance.py` 生成，改价请改 `core/reincarnation_rules.py` 的 `_SHOP_DEFS` 后重跑。\n\n"
            f"{block}\n"
        )
    DOC_FILE.write_text(new, encoding="utf-8")
    print("已生成", DOC_FILE)
    for i, name, old, new_price in diff_report():
        print(f"  待改 {i:>2} {name}: {old} → {new_price}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
