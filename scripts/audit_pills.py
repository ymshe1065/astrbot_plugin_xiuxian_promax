# -*- coding: utf-8 -*-
"""丹药效果审计：把「配置写了什么 / 代码真正消费了什么 / 描述承诺了什么」三者对一遍。

用法（插件根目录）：
    python scripts/audit_pills.py            # 全量报告
    python scripts/audit_pills.py --only 无缺丹   # 只看某个
    python scripts/audit_pills.py --brief        # 只列有问题的
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def _stub_astrbot():
    import types

    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    ev = types.ModuleType("astrbot.api.event")

    class _L:
        def __getattr__(self, _n):
            return lambda *a, **k: None

    api.logger = _L()
    api.AstrBotConfig = dict
    ev.AstrMessageEvent = object
    api.event = ev
    allm = types.ModuleType("astrbot.api.all")
    allm.At = object
    allm.Plain = object
    api.all = allm
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = ev
    sys.modules["astrbot.api.all"] = allm


_stub_astrbot()

PKG = ROOT.name
import importlib  # noqa: E402

importlib.import_module(f"{PKG}.data")
ConfigManager = importlib.import_module(f"{PKG}.config_manager").ConfigManager

# ===== 各分支真正会读的字段（改 Handler 时这里要跟着改）=====
CONSUMED = {
    "temporary": {
        "duration_minutes", "is_random", "effect_type", "subtype", "daily_limit",
        "cultivation_multiplier", "physical_damage_multiplier", "magic_damage_multiplier",
        "physical_defense_multiplier", "magic_defense_multiplier",
        "lifespan_cost_per_minute", "lifespan_regen_per_minute",
        "spiritual_qi_regen_per_minute", "blood_qi_regen_per_minute",
        "blood_qi_cost_per_minute", "breakthrough_bonus",
    },
    "permanent": {
        "effect_type", "subtype", "daily_limit",
        "physical_damage_gain", "magic_damage_gain", "physical_defense_gain",
        "magic_defense_gain", "mental_power_gain", "lifespan_gain",
        "max_spiritual_qi_gain", "max_blood_qi_gain",
        "base_attribute_limit_increase", "cultivation_multiplier",
        "death_protection_multiplier",
        "physical_damage_multiplier", "magic_damage_multiplier",
        "physical_defense_multiplier", "magic_defense_multiplier",
    },
    "instant": {
        "effect_type", "subtype", "daily_limit",
        "blood_qi_restore", "spiritual_qi_restore",
        "restore_hp_percent", "restore_qi_percent", "restore_mp_percent",
        "resets_permanent_pills", "reset_refund_ratio", "blocks_next_debuff",
    },
    "exp": {"effect_type", "subtype", "daily_limit", "exp_gain"},
    "resurrection": {"effect_type", "subtype", "daily_limit", "resurrects"},
    # 破境丹在突破流程里由 breakthrough_manager 读取，不经过 use_pill
    "breakthrough_only": {"breakthrough_bonus", "subtype", "effect_type"},
    "legacy": set(),  # 单独处理（读 effect.* 子键）
}

# 布尔型效果开关：不以 _gain/_multiplier 结尾，但要算效果字段
FLAG_KEYS = {"blocks_next_debuff", "resets_permanent_pills", "resurrects", "is_random"}

# 旧式 effect 丹药（items.json 里的 effect 字典）真正被读的键
LEGACY_KEYS = {
    "add_hp", "add_experience", "add_max_hp", "add_spiritual_power",
    "add_mental_power", "add_attack", "add_defense", "add_gold",
    "add_breakthrough_bonus",
}

# 命中这些后缀/前缀就算「效果字段」，没被消费就是静默失效
EFFECT_KEY_RE = re.compile(
    r"(_gain|_multiplier|_restore|_percent|_per_minute|_bonus|^add_|_increase|_reduce|_shield)$"
)

META_KEYS = {
    "id", "name", "description", "rank", "subtype", "required_level_index",
    "price", "shop_weight", "volume", "type", "effect_type", "daily_limit",
    "effect", "resurrects", "duration_minutes", "legacy",
}

# 描述词 → 配置字段候选
DESC_ATTRS = {
    "物伤": ("physical_damage_gain", "physical_damage_multiplier", "add_attack"),
    "物理伤害": ("physical_damage_gain", "physical_damage_multiplier", "add_attack"),
    "法伤": ("magic_damage_gain", "magic_damage_multiplier", "add_spiritual_power"),
    "法术伤害": ("magic_damage_gain", "magic_damage_multiplier", "add_spiritual_power"),
    "物防": ("physical_defense_gain", "physical_defense_multiplier", "add_defense"),
    "物理防御": ("physical_defense_gain", "physical_defense_multiplier", "add_defense"),
    "法防": ("magic_defense_gain", "magic_defense_multiplier"),
    "法术防御": ("magic_defense_gain", "magic_defense_multiplier"),
    "精神": ("mental_power_gain", "add_mental_power"),
    "寿命": ("lifespan_gain", "lifespan_regen_per_minute", "lifespan_cost_per_minute"),
    "修为": ("exp_gain", "add_experience"),
    "修炼速度": ("cultivation_multiplier",),
    "突破": ("breakthrough_bonus", "add_breakthrough_bonus", "death_protection_multiplier"),
    "灵气": (
        "spiritual_qi_restore", "spiritual_qi_regen_per_minute", "max_spiritual_qi_gain",
        "restore_qi_percent", "add_hp", "add_max_hp",
    ),
    "气血": (
        "blood_qi_restore", "blood_qi_regen_per_minute", "max_blood_qi_gain",
        "restore_hp_percent", "add_hp", "add_max_hp",
    ),
    "灵石": ("add_gold", "reset_refund_ratio", "price"),
}

VAGUE_DESC = re.compile(r"蕴含着天地之力|这是一件.*丹药")


def pick_branch(data: dict) -> str:
    subtype = data.get("subtype", "")
    if subtype == "breakthrough":
        return "breakthrough_only"
    if subtype == "exp":
        return "exp"
    if subtype == "resurrection":
        return "resurrection"
    if data.get("effect") and not data.get("effect_type"):
        return "legacy"
    et = data.get("effect_type", "instant")
    return et if et in ("temporary", "permanent", "instant") else f"unknown:{et}"


def effect_keys_of(data: dict) -> list[str]:
    keys = []
    for key in data:
        if key in META_KEYS:
            continue
        if EFFECT_KEY_RE.search(key) or key.startswith("add_") or key in FLAG_KEYS:
            keys.append(key)
    if isinstance(data.get("effect"), dict):
        keys.extend(f"effect.{k}" for k in data["effect"])
    return keys


def describe_mismatch(data: dict, branch: str) -> list[str]:
    desc = str(data.get("description") or "")
    if not desc or VAGUE_DESC.search(desc):
        return []
    # 修为丹/复活丹/破境丹的描述多是修辞（“凝聚灵气增强修为”），只认主效果
    if branch == "exp":
        return [] if data.get("exp_gain") else ["修为丹没有 exp_gain"]
    if branch in ("resurrection", "breakthrough_only"):
        return []

    flat = json.dumps(data, ensure_ascii=False)
    issues = []
    for word, fields in DESC_ATTRS.items():
        if word not in desc:
            continue
        if not any(f in flat for f in fields):
            issues.append(f"描述提到「{word}」但配置无对应字段")
    # 描述里出现「所有属性」这种总括词，要求四项以上齐全
    if "所有属性" in desc or "全属性" in desc:
        need = ["physical_damage", "magic_damage", "physical_defense", "magic_defense"]
        missing = [n for n in need if n not in flat]
        if missing:
            issues.append(f"描述称「所有属性」，但缺 {'/'.join(missing)}")
    return issues


def describe_number_mismatch(data: dict) -> list[str]:
    """描述里写死的数值 / 百分比 / 时长，跟配置对一对。"""
    desc = str(data.get("description") or "")
    if not desc or VAGUE_DESC.search(desc):
        return []
    issues = []

    # 持续时长：持续 60 分钟 / 持续 2 小时
    m = re.search(r"持续\s*(\d+)\s*(分钟|小时)", desc)
    if m and "duration_minutes" in data:
        want = int(m.group(1)) * (60 if m.group(2) == "小时" else 1)
        got = int(data.get("duration_minutes") or 0)
        if want != got:
            issues.append(f"描述持续 {want} 分钟，配置 duration_minutes={got}")

    # 修炼速度 +50%
    m = re.search(r"(降低|减少|下降)?\s*修炼速度\s*([+\-]?\d+(?:\.\d+)?)\s*%", desc)
    if m and "cultivation_multiplier" in data:
        want = float(m.group(2)) / 100
        if m.group(1):
            want = -abs(want)
        got = float(data.get("cultivation_multiplier") or 0)
        if abs(want - got) > 1e-6:
            issues.append(f"描述修炼速度 {want:+.0%}，配置 {got:+.0%}")

    # 所有属性 +400：四项增减益必须同值
    if "所有属性" in desc or "全属性" in desc:
        m = re.search(r"所有属性\s*([+\-]\d+)", desc) or re.search(r"全属性\s*([+\-]\d+)", desc)
        if m:
            want = int(m.group(1))
            got = {
                "物伤": data.get("physical_damage_gain"),
                "法伤": data.get("magic_damage_gain"),
                "物防": data.get("physical_defense_gain"),
                "法防": data.get("magic_defense_gain"),
                "精神": data.get("mental_power_gain"),
            }
            if "精神力+" in desc:  # 描述已单独说明精神力数值，不再要求五项同值
                got.pop("精神", None)
            diff = {k: v for k, v in got.items() if v is not None and int(v) != want}
            if diff:
                detail = "、".join(f"{k}{v:+d}" for k, v in diff.items())
                issues.append(f"描述称所有属性 {want:+d}，实际 {detail}")

    # 「每分钟恢复寿命+50」这类无时限描述，交给时长规则即可，不重复报
    return issues


def audit(only: str | None = None) -> list[dict]:
    cm = ConfigManager(ROOT)
    try:
        cult_cap = float(cm.pill_quality_data.get("temporary_effect_cap", 3.0))
    except Exception:
        cult_cap = 3.0
    temp_cap = 0.5  # 攻防倍率每项封顶 +50%（calculate_pill_attribute_effects 的 caps）
    pills: dict[str, dict] = {}
    for source, table in (
        ("pills.json", cm.pills_data),
        ("exp_pills.json", cm.exp_pills_data),
        ("utility_pills.json", cm.utility_pills_data),
    ):
        for name, data in table.items():
            if name and name not in pills:
                pills[name] = {**data, "_source": source}
    for data in cm.items_data.values():
        if data.get("type") != "丹药":
            continue
        name = data.get("name")
        if name and name not in pills:
            pills[name] = {**data, "_source": "items.json"}

    rows = []
    for name, data in sorted(pills.items()):
        if only and only not in name:
            continue
        branch = pick_branch(data)
        consumed = CONSUMED.get(branch, set())
        keys = effect_keys_of(data)
        dead = [k for k in keys if not k.startswith("effect.") and k not in consumed]
        if branch == "legacy":
            dead += [
                k for k in keys
                if k.startswith("effect.") and k.split(".", 1)[1] not in LEGACY_KEYS
            ]
        desc_issues = describe_mismatch(data, branch)
        num_issues = describe_number_mismatch(data)

        # 机制截断：配置数值再高，也会被系统上限压回去，实际效果小于配置
        capped = []
        if branch == "temporary":
            for key in (
                "physical_damage_multiplier", "magic_damage_multiplier",
                "physical_defense_multiplier", "magic_defense_multiplier",
            ):
                try:
                    value = float(data.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if value > temp_cap:
                    capped.append(f"{key}={value:+.0%}，实际封顶 +{temp_cap:.0%}")
            try:
                cult = float(data.get("cultivation_multiplier") or 0)
            except (TypeError, ValueError):
                cult = 0.0
            if cult > cult_cap - 1:
                capped.append(f"cultivation_multiplier={cult:+.0%}，实际封顶 +{cult_cap - 1:.0%}")

        tags = []
        if branch.startswith("unknown"):
            tags.append("分支未知→服用直接报错")
        if dead:
            tags.append("字段不被消费")
        if desc_issues or num_issues:
            tags.append("描述不符")
        if not keys and branch not in ("exp", "resurrection", "breakthrough_only"):
            tags.append("无可识别效果字段")

        rows.append({
            "name": name,
            "source": data.get("_source"),
            "branch": branch,
            "desc": data.get("description") or "",
            "keys": keys,
            "dead": dead,
            "capped": capped,
            "desc_issues": desc_issues + num_issues,
            "tags": tags,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--caps", action="store_true", help="同时列出被系统上限压低的丹药")
    args = ap.parse_args()

    rows = audit(args.only)
    bad = [r for r in rows if r["tags"]]
    capped = [r for r in rows if r.get("capped")]
    print(f"共 {len(rows)} 种丹药，其中 {len(bad)} 种存在疑问，{len(capped)} 种被系统上限压低\n")
    target = bad if args.brief else rows
    if args.caps:
        target = [r for r in rows if r["tags"] or r.get("capped")]
    for r in target:
        print(f"【{r['name']}】{r['source']} · {r['branch']} · {'/'.join(r['tags'])}")
        if not args.brief:
            print(f"   描述：{r['desc'][:70]}")
            print(f"   效果字段：{', '.join(r['keys']) or '（无）'}")
        for d in r["dead"]:
            print(f"   ✗ 不被消费：{d}")
        for d in r["desc_issues"]:
            print(f"   ✗ {d}")
        for d in r.get("capped") or []:
            print(f"   ⚠ 被上限压低：{d}")
    if capped and not args.caps:
        print(f"\n（另有 {len(capped)} 种丹药的数值会被系统上限压低，用 --caps 查看）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
