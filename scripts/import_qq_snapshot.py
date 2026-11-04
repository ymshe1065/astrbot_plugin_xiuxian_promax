# -*- coding: utf-8 -*-
"""把旧 QQ 机器人数据快照合并进当前修仙主库（单向、只补不覆盖）。

设计前提：QQ 机器人已下线，旧数据只作为「可被网页账号绑定的存档」存在。

默认只导入玩家个人数据表（含 user_id 类列的表 + players），跳过 Boss/商店/
灵眼/拍卖周期/仙市行情等「全局运行状态」表——那些是旧实例的运行时状态，
导进来只会让当前实例立刻补刷一波过期活动。需要完整历史时加 --all。

- 按主键判重，只插入本地不存在的行，绝不覆盖现有 web_ 账号数据
- 快照的 db_info(version=58) 永远跳过，本地保持 v60
- 导入前用 SQLite backup API 做一致性备份到项目根 _backup_db_<时间戳>/
- 自动把指向已下线 napcat 群的 origin 清掉并停用对应自动代理

用法：
    python scripts/import_qq_snapshot.py --snapshot <快照.json>
    python scripts/import_qq_snapshot.py --snapshot <快照.json> --dry-run
    python scripts/import_qq_snapshot.py --snapshot <快照.json> --all
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

# 全局运行状态表：旧实例的「现在」，不属于任何玩家，默认不导
GLOBAL_STATE_TABLES = {
    "db_info",
    "system_config",
    "shop",
    "rifts",
    "spirit_eyes",
    "boss",
    "world_boss",
    "world_boss_raids",
    "battle_events",
    "auction_house_events",
    "auction_house_lots",
    "auction_house_messages",
    "sect_tournaments",
    "sect_wars",
    "sect_orders",
    "invest_prices",
    "invest_events",
    "invest_products",
    "bounty_tasks",
    "spar_requests",
    "dual_cultivation_requests",
    "pending_gifts",
    "master_relations",
}

# 这些表的行带平台 origin，QQ 机器人下线后不能再往旧群推送
ORIGIN_TABLES = ("auto_proxy", "sect_tournaments")


def default_db_path() -> Path:
    # scripts/ -> 插件根 -> 项目根
    root = Path(__file__).resolve().parents[2]
    return root / "astrbot" / "data" / "plugin_data" / "astrbot_plugin_xiuxian_promax" / "xiuxian_data_lite.db"


def backup_db(db: Path, root: Path) -> Path:
    dest_dir = root / f"_backup_db_{time.strftime('%Y%m%d_%H%M%S')}_pre_qq_import"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "xiuxian_data_lite.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def table_columns(con: sqlite3.Connection, table: str) -> tuple[list[str], list[str]]:
    """返回 (全部列, 主键列)。无主键时主键列 = 全部列，用于判重。"""
    rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    all_cols = [r[1] for r in rows]
    pk_cols = [r[1] for r in rows if r[5] > 0]
    return all_cols, pk_cols or all_cols


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True, help="快照 JSON 路径")
    ap.add_argument("--db", default="", help="目标主库路径，默认自动推断")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    ap.add_argument("--all", action="store_true", help="连全局运行状态表一起导入")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（不建议）")
    args = ap.parse_args()

    snap_path = Path(args.snapshot).expanduser().resolve()
    db = Path(args.db).expanduser().resolve() if args.db else default_db_path().resolve()
    root = Path(__file__).resolve().parents[2]
    if not snap_path.is_file():
        print(f"快照不存在: {snap_path}")
        return 1
    if not db.is_file():
        print(f"目标主库不存在: {db}")
        return 1

    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    tables: dict[str, list[dict]] = snap.get("tables", {})
    if not tables:
        print("快照里没有 tables")
        return 1
    print(f"快照: {snap_path.name}  ({snap.get('meta', {}).get('plugin_version')} / "
          f"db v{snap.get('meta', {}).get('db_version')} / {len(tables)} 表)")
    print(f"目标: {db}")
    print(f"模式: {'DRY-RUN' if args.dry_run else '写入'} / "
          f"{'全表' if args.all else '仅玩家数据'}")

    if not args.dry_run and not args.no_backup:
        bak = backup_db(db, root)
        print(f"已备份到: {bak}\n")

    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    try:
        local_tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        plan: list[tuple[str, list[dict], list[str], list[str]]] = []
        skipped: list[str] = []
        for name, rows in tables.items():
            if name == "db_info":
                skipped.append(f"{name}(版本号必须保持本地)")
                continue
            if not args.all and name in GLOBAL_STATE_TABLES:
                skipped.append(f"{name}(全局状态)")
                continue
            if name not in local_tables:
                skipped.append(f"{name}(本地无此表)")
                continue
            if not rows:
                continue
            local_cols, pk_cols = table_columns(con, name)
            snap_cols = list(rows[0].keys())
            unknown = [c for c in snap_cols if c not in local_cols]
            if unknown:
                print(f"  !! {name} 快照含本地不存在的列 {unknown}，跳过整表")
                skipped.append(f"{name}(列不兼容)")
                continue
            plan.append((name, rows, snap_cols, pk_cols))

        print(f"\n计划导入 {len(plan)} 张表；跳过 {len(skipped)} 张：")
        for s in skipped:
            print(f"  - {s}")

        total_new = 0
        stats: list[str] = []
        for name, rows, cols, pk_cols in plan:
            # 自增 id 表且本地已有数据：旧库的 id 1,2... 必然和本地撞，
            # 此时按「除 id 外的全部列」判重，并让 SQLite 重新分配 id。
            has_local = con.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone() is not None
            drop_id = pk_cols == ["id"] and has_local and "id" in cols
            key_cols = [c for c in cols if c != "id"] if drop_id else pk_cols
            insert_cols = [c for c in cols if c != "id"] if drop_id else cols
            quoted = ",".join('"' + c + '"' for c in key_cols)
            existing: set[tuple] = set()
            for r in con.execute(f'SELECT {quoted} FROM "{name}"'):
                existing.add(tuple(r))
            seen: set[tuple] = set(existing)
            deduped = []
            for r in rows:
                key = tuple(r.get(c) for c in key_cols)
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(r)
            if not deduped:
                stats.append(f"  {name:34s} +0 / {len(rows)}")
                continue
            if not args.dry_run:
                placeholders = ",".join("?" for _ in insert_cols)
                sql = (
                    f'INSERT OR IGNORE INTO "{name}" '
                    f'({",".join(chr(34) + c + chr(34) for c in insert_cols)}) '
                    f'VALUES ({placeholders})'
                )
                con.executemany(sql, [tuple(r.get(c) for c in insert_cols) for r in deduped])
            total_new += len(deduped)
            stats.append(f"  {name:34s} +{len(deduped)} / {len(rows)}" + ("  (id 重排)" if drop_id else ""))
        print("\n".join(stats))
        print(f"\n合计新增 {total_new} 行")

        if not args.dry_run:
            # 清掉指向已下线 QQ 群的平台 origin
            for t in ORIGIN_TABLES:
                if t not in local_tables:
                    continue
                cols, _ = table_columns(con, t)
                if "origin" not in cols:
                    continue
                if t == "auto_proxy" and "enabled" in cols:
                    cur = con.execute(
                        f'UPDATE "{t}" SET enabled = 0, origin = \'\' WHERE origin LIKE \'napcat:%\''
                    )
                else:
                    cur = con.execute(
                        f'UPDATE "{t}" SET origin = \'\' WHERE origin LIKE \'napcat:%\''
                    )
                if cur.rowcount:
                    print(f"  已停用/清理 {t} 的旧平台 origin: {cur.rowcount} 行")
            con.commit()

        con.close()

        if args.dry_run:
            print("\nDRY-RUN 结束，未改动数据库。")
            return 0

        # 校验
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        qq_players = con.execute(
            "SELECT user_id, level_index, cultivation_type FROM players "
            "WHERE user_id GLOB '[0-9]*' ORDER BY user_id"
        ).fetchall()
        ver = con.execute("SELECT version FROM db_info").fetchone()
        con.close()
        print(f"\n校验：db_info = v{ver[0]}；QQ 玩家 {len(qq_players)} 人")
        for uid, lv, ct in qq_players:
            print(f"  {uid}  lv{lv}  {ct}")
        return 0
    except Exception as e:
        print(f"导入失败: {e}")
        con.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
