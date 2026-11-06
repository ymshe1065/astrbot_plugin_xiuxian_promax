# -*- coding: utf-8 -*-
"""webroom 仙界（频道）系统。

- 主频道「原始仙界」固定为 ``room_main``，所有账号注册/登录后默认进入；
- 玩家可创建私人仙界，可选名称（留空自动命名为「仙界N」）与进入密码；
- 创建者可决定仙界是否在「仙界大厅」公开可见可搜索：
  * 公开：大厅可搜索，凭名称/UUID + 密码（如有）加入；
  * 私密：不出现在大厅，必须凭 UUID + 密码（如有）加入；
- 加入记录写入 ``webroom_channel_members``，已加入的玩家切回时不再验证密码；
- 数据与网页账号共用 ``accounts.db``，不修改游戏主库结构。
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from typing import Any

from . import accounts as accounts_store

MAIN_CHANNEL_ID = "room_main"
MAIN_CHANNEL_NAME = "原始仙界"

NAME_MAX = 20
PWD_MIN = 4
PWD_MAX = 64
MAX_OWNED = 5
MAX_MEMBERS = 500

# 名称只挡控制字符，中文 / 字母 / 数字 / 空格 / 常见符号都允许
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,%d}$" % NAME_MAX)


def init() -> None:
    """建表并写入主频道；重复调用安全。"""
    db = accounts_store._db()
    with accounts_store._lock:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS webroom_channels (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                owner_username TEXT NOT NULL DEFAULT '',
                password       TEXT NOT NULL DEFAULT '',
                is_public      INTEGER NOT NULL DEFAULT 1,
                seq            INTEGER NOT NULL DEFAULT 0,
                created        INTEGER NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS webroom_channel_members (
                channel_id TEXT NOT NULL,
                username   TEXT NOT NULL,
                joined     INTEGER NOT NULL,
                PRIMARY KEY (channel_id, username)
            )
            """
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_name "
            "ON webroom_channels(name)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_members_user "
            "ON webroom_channel_members(username, joined DESC)"
        )
        now = int(time.time())
        row = db.execute(
            "SELECT id FROM webroom_channels WHERE id = ?", (MAIN_CHANNEL_ID,)
        ).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO webroom_channels "
                "(id, name, owner_username, password, is_public, seq, created) "
                "VALUES (?,?,?,?,?,?,?)",
                (MAIN_CHANNEL_ID, MAIN_CHANNEL_NAME, "", "", 1, 0, now),
            )
        db.commit()


# ---------- 初始化兜底 ----------


def _ensure() -> None:
    """测试/热重载时可能绕过 start()，这里补一次建表。"""
    db = accounts_store._db()
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('webroom_channels', 'webroom_channel_members')"
    ).fetchall()
    if len(rows) < 2:
        init()


# ---------- 查询 ----------


def _row_of_channel(row: sqlite3.Row, username: str, member_count: int) -> dict[str, Any]:
    channel_id = str(row["id"])
    is_main = channel_id == MAIN_CHANNEL_ID
    is_mine = bool(username) and str(row["owner_username"] or "") == username
    joined = is_main or is_member(channel_id, username)
    return {
        "id": channel_id,
        "name": str(row["name"] or ""),
        "owner": str(row["owner_username"] or ""),
        "public": bool(row["is_public"]),
        "has_password": bool(row["password"]),
        "member_count": int(member_count or 0),
        "created": int(row["created"] or 0),
        "is_main": is_main,
        "is_mine": is_mine,
        "joined": bool(joined),
        # UUID 仅创建者可见：私密仙界需要靠它邀请，公开仙界无需暴露
        "uuid": channel_id if is_mine else "",
    }


def _count_members(channel_id: str) -> int:
    _ensure()
    row = accounts_store._db().execute(
        "SELECT COUNT(*) AS c FROM webroom_channel_members WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    return int(row["c"]) if row else 0


def get(channel_id: str) -> dict[str, Any] | None:
    _ensure()
    channel_id = str(channel_id or "").strip()
    if not channel_id:
        return None
    row = accounts_store._db().execute(
        "SELECT * FROM webroom_channels WHERE id = ?", (channel_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_of_channel(row, "", _count_members(channel_id))


def describe(channel_id: str, username: str) -> dict[str, Any] | None:
    """返回带当前玩家视角（是否加入/是否本人创建）的频道信息。"""
    _ensure()
    channel_id = str(channel_id or "").strip()
    if not channel_id:
        return None
    row = accounts_store._db().execute(
        "SELECT * FROM webroom_channels WHERE id = ?", (channel_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_of_channel(row, str(username or ""), _count_members(channel_id))


def get_by_name(name: str) -> dict[str, Any] | None:
    _ensure()
    name = str(name or "").strip()
    if not name:
        return None
    row = accounts_store._db().execute(
        "SELECT * FROM webroom_channels WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        return None
    return _row_of_channel(row, "", _count_members(str(row["id"])))


def is_member(channel_id: str, username: str) -> bool:
    _ensure()
    channel_id = str(channel_id or "").strip()
    username = str(username or "").strip()
    if not channel_id or not username:
        return False
    if channel_id == MAIN_CHANNEL_ID:
        return True
    row = accounts_store._db().execute(
        "SELECT 1 FROM webroom_channel_members WHERE channel_id = ? AND username = ?",
        (channel_id, username),
    ).fetchone()
    return row is not None


def can_access(channel_id: str, username: str) -> bool:
    return describe(channel_id, username) is not None and is_member(channel_id, username)


def member_profiles(channel_id: str) -> list[dict[str, Any]]:
    """频道成员资料（含离线），按加入时间倒序，供成员名单渲染。"""
    _ensure()
    channel_id = str(channel_id or "").strip()
    if not channel_id:
        return []
    rows = accounts_store._db().execute(
        """
        SELECT m.username AS username, m.joined AS joined,
               u.nick AS nick, u.avatar AS avatar, u.room_seen AS room_seen
        FROM webroom_channel_members m
        LEFT JOIN users u ON u.username = m.username
        WHERE m.channel_id = ?
        ORDER BY m.joined DESC
        """,
        (channel_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        username = str(row["username"] or "")
        if not username:
            continue
        out.append(
            {
                "username": username,
                "nick": str(row["nick"] or username),
                "avatar": str(row["avatar"] or ""),
                "seen": max(int(row["joined"] or 0), int(row["room_seen"] or 0)),
            }
        )
    return out


def joined_channels(username: str) -> list[dict[str, Any]]:
    """主频道固定排第一，其余按加入时间倒序。"""
    _ensure()
    username = str(username or "").strip()
    db = accounts_store._db()
    out: list[dict[str, Any]] = []
    main_row = db.execute(
        "SELECT * FROM webroom_channels WHERE id = ?", (MAIN_CHANNEL_ID,)
    ).fetchone()
    if main_row is not None:
        out.append(
            _row_of_channel(main_row, username, _count_members(MAIN_CHANNEL_ID))
        )
    rows = db.execute(
        """
        SELECT c.*, m.joined AS member_joined
        FROM webroom_channel_members m
        JOIN webroom_channels c ON c.id = m.channel_id
        WHERE m.username = ? AND c.id <> ?
        ORDER BY m.joined DESC
        """,
        (username, MAIN_CHANNEL_ID),
    ).fetchall()
    for row in rows:
        out.append(
            _row_of_channel(row, username, _count_members(str(row["id"])))
        )
    return out


def public_channels(username: str = "", query: str = "", limit: int = 100) -> list[dict[str, Any]]:
    """仙界大厅：只返回公开频道（不含主频道），按创建时间倒序。"""
    _ensure()
    db = accounts_store._db()
    sql = (
        "SELECT * FROM webroom_channels "
        "WHERE is_public = 1 AND id <> ?"
    )
    args: list[Any] = [MAIN_CHANNEL_ID]
    query = str(query or "").strip()
    if query:
        sql += " AND name LIKE ?"
        args.append(f"%{query}%")
    sql += " ORDER BY created DESC LIMIT ?"
    args.append(max(1, min(int(limit or 100), 200)))
    rows = db.execute(sql, args).fetchall()
    return [
        _row_of_channel(row, str(username or ""), _count_members(str(row["id"])))
        for row in rows
    ]


# ---------- 创建 / 加入 / 管理 ----------


def _clean_name(name: str) -> str:
    return str(name or "").strip()


def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        return f"仙界名称需 1-{NAME_MAX} 个字，不能包含控制字符"
    return ""


def _validate_password(password: str) -> str:
    if password and not (PWD_MIN <= len(password) <= PWD_MAX):
        return f"进入密码需 {PWD_MIN}-{PWD_MAX} 位，或留空不设密码"
    return ""


def _next_seq(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT MAX(seq) AS s FROM webroom_channels").fetchone()
    return int(row["s"] or 0) + 1


def create(
    username: str, name: str = "", password: str = "", is_public: bool = True
) -> tuple[bool, str, dict[str, Any] | None]:
    _ensure()
    username = str(username or "").strip()
    if not username:
        return False, "请先登录", None
    name = _clean_name(name)
    password = str(password or "").strip()
    err = _validate_password(password)
    if err:
        return False, err, None
    if name:
        err = _validate_name(name)
        if err:
            return False, err, None

    db = accounts_store._db()
    with accounts_store._lock:
        owned = db.execute(
            "SELECT COUNT(*) AS c FROM webroom_channels WHERE owner_username = ?",
            (username,),
        ).fetchone()
        if owned and int(owned["c"]) >= MAX_OWNED:
            return False, f"每位道友最多创建 {MAX_OWNED} 个仙界", None

        seq = _next_seq(db)
        if not name:
            name = f"仙界{seq}"
            while db.execute(
                "SELECT 1 FROM webroom_channels WHERE name = ?", (name,)
            ).fetchone():
                seq += 1
                name = f"仙界{seq}"
        else:
            exists = db.execute(
                "SELECT 1 FROM webroom_channels WHERE name = ?", (name,)
            ).fetchone()
            if exists:
                return False, "这个仙界名称已被占用，换一个吧", None

        channel_id = uuid.uuid4().hex
        now = int(time.time())
        try:
            db.execute(
                "INSERT INTO webroom_channels "
                "(id, name, owner_username, password, is_public, seq, created) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    channel_id,
                    name,
                    username,
                    accounts_store._hash_pwd(password) if password else "",
                    1 if is_public else 0,
                    seq,
                    now,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO webroom_channel_members "
                "(channel_id, username, joined) VALUES (?,?,?)",
                (channel_id, username, now),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return False, "这个仙界名称已被占用，换一个吧", None

    return True, "仙界创建成功", describe(channel_id, username)


def join(
    username: str, identifier: str = "", password: str = ""
) -> tuple[bool, str, dict[str, Any] | None]:
    """加入仙界。

    identifier 可为 UUID 或公开仙界名称；私密仙界只接受 UUID。
    已加入的玩家直接返回成功，不再校验密码。
    """
    _ensure()
    username = str(username or "").strip()
    identifier = str(identifier or "").strip()
    password = str(password or "").strip()
    if not username:
        return False, "请先登录", None
    if not identifier:
        return False, "请输入仙界 UUID 或名称", None
    if identifier in (MAIN_CHANNEL_ID, MAIN_CHANNEL_NAME):
        return True, "已在原始仙界", describe(MAIN_CHANNEL_ID, username)

    db = accounts_store._db()
    row = db.execute(
        "SELECT * FROM webroom_channels WHERE id = ?", (identifier,)
    ).fetchone()
    if row is None:
        row = db.execute(
            "SELECT * FROM webroom_channels WHERE name = ?", (identifier,)
        ).fetchone()
        if row is not None and not bool(row["is_public"]):
            # 已加入的成员仍可按名称回来，不受“私密必须 UUID”限制
            if is_member(str(row["id"]), username):
                return True, "已加入该仙界", describe(str(row["id"]), username)
            return False, "该仙界不公开，请向创建者索取 UUID", None
    if row is None:
        return False, "没有找到这个仙界，请检查 UUID 或名称", None

    channel_id = str(row["id"])
    if is_member(channel_id, username):
        return True, "已加入该仙界", describe(channel_id, username)

    stored_pwd = str(row["password"] or "")
    if stored_pwd and not accounts_store._check_pwd(password, stored_pwd):
        return False, "进入密码不对，再想想" if password else "该仙界需要密码", None

    members = _count_members(channel_id)
    if members >= MAX_MEMBERS:
        return False, "该仙界人数已满", None

    with accounts_store._lock:
        db.execute(
            "INSERT OR IGNORE INTO webroom_channel_members "
            "(channel_id, username, joined) VALUES (?,?,?)",
            (channel_id, username, int(time.time())),
        )
        db.commit()
    return True, "已加入仙界", describe(channel_id, username)


def update(
    username: str,
    channel_id: str,
    *,
    name: str | None = None,
    is_public: bool | None = None,
    password: str | None = None,
    clear_password: bool = False,
) -> tuple[bool, str, dict[str, Any] | None]:
    """创建者修改仙界名称 / 公开状态 / 密码。"""
    _ensure()
    username = str(username or "").strip()
    channel_id = str(channel_id or "").strip()
    if channel_id == MAIN_CHANNEL_ID:
        return False, "原始仙界不可修改", None
    db = accounts_store._db()
    row = db.execute(
        "SELECT * FROM webroom_channels WHERE id = ?", (channel_id,)
    ).fetchone()
    if row is None:
        return False, "仙界不存在", None
    if str(row["owner_username"] or "") != username:
        return False, "只有创建者可以管理这个仙界", None

    sets: list[str] = []
    args: list[Any] = []
    if name is not None:
        name = _clean_name(name)
        err = _validate_name(name)
        if err:
            return False, err, None
        if name != str(row["name"]):
            exists = db.execute(
                "SELECT 1 FROM webroom_channels WHERE name = ? AND id <> ?",
                (name, channel_id),
            ).fetchone()
            if exists:
                return False, "这个仙界名称已被占用，换一个吧", None
            sets.append("name = ?")
            args.append(name)
    if is_public is not None:
        sets.append("is_public = ?")
        args.append(1 if is_public else 0)
    if clear_password:
        sets.append("password = ?")
        args.append("")
    elif password is not None and str(password).strip():
        pwd = str(password).strip()
        err = _validate_password(pwd)
        if err:
            return False, err, None
        sets.append("password = ?")
        args.append(accounts_store._hash_pwd(pwd))

    if not sets:
        return True, "没有需要修改的内容", describe(channel_id, username)

    args.append(channel_id)
    with accounts_store._lock:
        try:
            db.execute(
                f"UPDATE webroom_channels SET {', '.join(sets)} WHERE id = ?",
                args,
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return False, "这个仙界名称已被占用，换一个吧", None
    return True, "仙界设置已更新", describe(channel_id, username)
