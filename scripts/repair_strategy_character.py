#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仙域争霸角色数据修正工具（运维用，本地/服务器均可运行）。

用途：修正因线上 bug 产生的角色数据（例如酒馆候选错位导致「点珍品武将、
得良品内政官」），按目标稀有度/职责重掷六维、特技与兵种适性，保留姓名、
职业、个性、忠诚与所在地，并写入 ``strategy_audit`` 留痕。

示例::

    python3 scripts/repair_strategy_character.py \
        --db /opt/xiuxian/runtime/data/plugin_data/astrbot_plugin_xiuxian_promax/xiuxian_data_lite.db \
        --user-id 1610049038 --char-id 107 \
        --rarity rare --role general --reason "酒馆候选错位补偿"

默认只打印修改预览（--dry-run），确认后加 ``--apply`` 落库。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "config" / "strategy_config.json"
RULES_PATH = ROOT / "core" / "strategy_rules.py"


def _load_rules():
    spec = importlib.util.spec_from_file_location("strategy_rules", RULES_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["strategy_rules"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser(description="仙域争霸角色修正（稀有度/职责/六维重掷）")
    ap.add_argument("--db", required=True, help="xiuxian_data_lite.db 路径")
    ap.add_argument("--user-id", required=True, help="角色归属玩家 user_id")
    ap.add_argument("--char-id", type=int, required=True, help="strategy_characters.id")
    ap.add_argument("--rarity", default="rare",
                    help="目标稀有度：common/fine/rare/epic/legend")
    ap.add_argument("--role", default="general",
                    help="目标职责：general/strategist/official")
    ap.add_argument("--reason", default="数据修正", help="写入审计的原因")
    ap.add_argument("--seed", default="", help="重掷随机种子（默认按角色+日期）")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    args = ap.parse_args()

    R = _load_rules()
    cfg_all = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    recruit_cfg = cfg_all.get("recruit") or {}

    conn = sqlite3.connect(args.db, timeout=10)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM strategy_characters WHERE id=?", (int(args.char_id),)).fetchone()
    if not row:
        print(f"❌ 角色 {args.char_id} 不存在")
        return 2
    c = dict(row)
    if str(c.get("owner_user_id") or "") != str(args.user_id):
        print(f"❌ 角色 {args.char_id} 归属 {c.get('owner_user_id')!r}，"
              f"与 --user-id {args.user_id!r} 不符")
        return 2
    if args.rarity not in R.RARITY_NAMES:
        print(f"❌ 稀有度无效：{args.rarity}")
        return 2
    if args.role not in (R.ROLE_GENERAL, R.ROLE_STRATEGIST, R.ROLE_OFFICIAL):
        print(f"❌ 职责无效：{args.role}")
        return 2

    seed = args.seed or f"{args.char_id}|{time.strftime('%Y%m%d')}|{args.reason}"
    rng = random.Random(int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:8], 16))

    before = {
        "rarity": c["rarity"], "role": c["role"], "stats": json.loads(
            c["stats_json"] or "{}"), "traits": json.loads(c["traits_json"] or "[]"),
        "aptitude": json.loads(c["aptitude_json"] or "{}"),
    }
    new_stats = R.roll_stats(rng, args.rarity, args.role)
    new_traits = R.roll_traits_for_rarity(rng, args.rarity, recruit_cfg)
    new_apt = R.roll_aptitude(rng, c["profession"], args.rarity)

    print("=== 修改预览 ===")
    print(f"角色：#{args.char_id} {c['name']}（{c.get('profession')}）")
    print(f"品质/职责：{before['rarity']}/{before['role']} → {args.rarity}/{args.role}")
    print(f"六维：{before['stats']} → {new_stats}")
    print(f"特技：{before['traits']} → {new_traits}")
    print(f"适性：{before['aptitude']} → {new_apt}")

    if not args.apply:
        print("\n（dry-run：未写库；确认无误后加 --apply）")
        return 0

    detail = {
        "tool": "repair_strategy_character",
        "reason": args.reason,
        "seed": seed,
        "before": before,
        "after": {"rarity": args.rarity, "role": args.role,
                  "stats": new_stats, "traits": new_traits,
                  "aptitude": new_apt},
    }
    with conn:  # BEGIN IMMEDIATE + 提交/回滚
        conn.execute(
            "UPDATE strategy_characters SET rarity=?, role=?, stats_json=?, "
            "traits_json=?, aptitude_json=? WHERE id=? AND owner_user_id=?",
            (args.rarity, args.role, json.dumps(new_stats, ensure_ascii=False),
             json.dumps(new_traits, ensure_ascii=False),
             json.dumps(new_apt, ensure_ascii=False),
             int(args.char_id), str(args.user_id)))
        conn.execute(
            "INSERT INTO strategy_audit (world_id, user_id, action, detail_json, "
            "created_at) VALUES (?,?,?,?,?)",
            (int(c.get("world_id") or 0), str(args.user_id), "gm_fix_char",
             json.dumps(detail, ensure_ascii=False), int(time.time())))
    conn.close()
    print("\n✅ 已写入数据库，并记录 strategy_audit(action=gm_fix_char)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
