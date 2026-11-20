# -*- coding: utf-8 -*-
"""webroom 私聊（DM）存储（设计方案 §12.7，通用能力、非争霸专属）。

- 会话以双方用户名的字典序拼接为 id（``userA|userB``），双方共享同一会话行；
- 消息落 ``dm_messages``（含已读标记），历史保留 30 天，过期由 :func:`prune` 清理；
- 拉黑：A 拉黑 B 后，B 发给 A 的私聊不落库不投递，B 侧仅收到「发送失败」
  （不泄露被拉黑状态）；
- 数据与网页账号共用 ``accounts.db``，属 webchat_room 自有表，
  **不计入** strategy 11 表（§13）。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import accounts as accounts_store

#: 单条私聊长度上限（与公屏同标准）
CONTENT_MAX = 2000
#: 历史保留天数（§12.7）
RETENTION_DAYS = 30
#: 会话列表返回上限
CONVO_LIMIT = 200
#: 预览截断长度
PREVIEW_LEN = 50


def init() -> None:
    """建表；重复调用安全。"""
    db = accounts_store._db()
    with accounts_store._lock:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS dm_conversations (
                id         TEXT PRIMARY KEY,
                user_a     TEXT NOT NULL,
                user_b     TEXT NOT NULL,
                preview    TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS dm_messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                convo_id  TEXT NOT NULL,
                sender    TEXT NOT NULL,
                recipient TEXT NOT NULL,
                content   TEXT NOT NULL DEFAULT '',
                created   INTEGER NOT NULL DEFAULT 0,
                read      INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS dm_blocks (
                blocker TEXT NOT NULL,
                blocked TEXT NOT NULL,
                created INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (blocker, blocked)
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dm_convo_a "
            "ON dm_conversations(user_a, updated_at DESC)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dm_convo_b "
            "ON dm_conversations(user_b, updated_at DESC)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dm_msgs_convo "
            "ON dm_messages(convo_id, id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dm_msgs_unread "
            "ON dm_messages(recipient, read)"
        )
        db.commit()


def _ensure() -> None:
    """测试/热重载时可能绕过 start()，这里补一次建表。"""
    db = accounts_store._db()
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('dm_conversations', 'dm_messages', 'dm_blocks')"
    ).fetchall()
    if len(rows) < 3:
        init()


# ---------- 会话 ----------


def convo_id(user_a: str, user_b: str) -> str:
    """双方用户名按字典序拼接，保证 A→B 与 B→A 命中同一会话。"""
    a = str(user_a or "").strip()
    b = str(user_b or "").strip()
    lo, hi = (a, b) if a <= b else (b, a)
    return f"{lo}|{hi}"


def _row_of_message(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "convo_id": str(row["convo_id"] or ""),
        "sender": str(row["sender"] or ""),
        "recipient": str(row["recipient"] or ""),
        "content": str(row["content"] or ""),
        "created": int(row["created"] or 0),
        "read": bool(row["read"]),
    }


def send(sender: str, recipient: str,
         content: str) -> tuple[bool, str, dict[str, Any] | None]:
    """发送私聊。返回 (ok, err, message)。

    被拉黑时消息不落库不投递，仅回复「发送失败」（不泄露拉黑状态）。
    """
    _ensure()
    sender = str(sender or "").strip()
    recipient = str(recipient or "").strip()
    content = str(content or "").strip()
    if not sender:
        return False, "请先登录", None
    if not recipient:
        return False, "请选择私聊对象", None
    if sender == recipient:
        return False, "不能给自己发私聊", None
    if not content:
        return False, "消息不能为空", None
    if len(content) > CONTENT_MAX:
        return False, f"消息过长（>{CONTENT_MAX} 字）", None
    if accounts_store.get(recipient) is None:
        return False, "对方账号不存在", None
    if is_blocked(recipient, sender):
        return False, "消息发送失败", None

    cid = convo_id(sender, recipient)
    now = int(time.time())
    preview = content[:PREVIEW_LEN]
    db = accounts_store._db()
    with accounts_store._lock:
        cur = db.execute(
            "INSERT INTO dm_messages "
            "(convo_id, sender, recipient, content, created, read) "
            "VALUES (?,?,?,?,?,0)",
            (cid, sender, recipient, content, now),
        )
        msg_id = int(cur.lastrowid or 0)
        db.execute(
            "INSERT INTO dm_conversations (id, user_a, user_b, preview, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET preview=excluded.preview, "
            "updated_at=excluded.updated_at",
            (cid, *sorted((sender, recipient)), preview, now),
        )
        db.commit()
    return True, "", {
        "id": msg_id, "convo_id": cid, "sender": sender,
        "recipient": recipient, "content": content,
        "created": now, "read": False,
    }


def history(username: str, other: str, limit: int = 50,
            before: int = 0) -> list[dict[str, Any]]:
    """与某人的私聊历史（按 id 升序返回，便于前端顺序渲染）。"""
    _ensure()
    username = str(username or "").strip()
    other = str(other or "").strip()
    if not username or not other:
        return []
    limit = max(1, min(int(limit or 50), 200))
    cid = convo_id(username, other)
    sql = "SELECT * FROM dm_messages WHERE convo_id = ?"
    args: list[Any] = [cid]
    if before:
        sql += " AND id < ?"
        args.append(int(before))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = accounts_store._db().execute(sql, args).fetchall()
    return [_row_of_message(r) for r in reversed(rows)]


def conversations(username: str) -> list[dict[str, Any]]:
    """我的会话列表（按最近活跃倒序），含对方资料 / 预览 / 未读数。"""
    _ensure()
    username = str(username or "").strip()
    if not username:
        return []
    rows = accounts_store._db().execute(
        """
        SELECT c.*, u.nick AS peer_nick, u.avatar AS peer_avatar,
               u.room_seen AS peer_seen,
               (SELECT COUNT(*) FROM dm_messages m
                 WHERE m.convo_id = c.id AND m.recipient = ? AND m.read = 0) AS unread
        FROM dm_conversations c
        LEFT JOIN users u
               ON u.username = CASE WHEN c.user_a = ? THEN c.user_b ELSE c.user_a END
        WHERE c.user_a = ? OR c.user_b = ?
        ORDER BY c.updated_at DESC
        LIMIT ?
        """,
        (username, username, username, username, CONVO_LIMIT),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        user_a = str(row["user_a"] or "")
        user_b = str(row["user_b"] or "")
        peer = user_b if user_a == username else user_a
        out.append({
            "convo_id": str(row["id"] or ""),
            "peer": peer,
            "peer_nick": str(row["peer_nick"] or peer),
            "peer_avatar": str(row["peer_avatar"] or ""),
            "peer_seen": int(row["peer_seen"] or 0),
            "preview": str(row["preview"] or ""),
            "updated_at": int(row["updated_at"] or 0),
            "unread": int(row["unread"] or 0),
        })
    return out


def mark_read(username: str, other: str) -> int:
    """把某人发给我的私聊全部标为已读，返回本次标读数。"""
    _ensure()
    username = str(username or "").strip()
    other = str(other or "").strip()
    if not username or not other:
        return 0
    cid = convo_id(username, other)
    db = accounts_store._db()
    with accounts_store._lock:
        cur = db.execute(
            "UPDATE dm_messages SET read = 1 "
            "WHERE convo_id = ? AND recipient = ? AND read = 0",
            (cid, username),
        )
        db.commit()
    return int(cur.rowcount or 0)


def unread_total(username: str) -> int:
    """我的全部未读私聊数（侧栏角标）。"""
    _ensure()
    username = str(username or "").strip()
    if not username:
        return 0
    row = accounts_store._db().execute(
        "SELECT COUNT(*) AS c FROM dm_messages WHERE recipient = ? AND read = 0",
        (username,),
    ).fetchone()
    return int(row["c"]) if row else 0


# ---------- 拉黑 ----------


def block(username: str, target: str) -> tuple[bool, str]:
    _ensure()
    username = str(username or "").strip()
    target = str(target or "").strip()
    if not username or not target:
        return False, "参数缺失"
    if username == target:
        return False, "不能拉黑自己"
    if accounts_store.get(target) is None:
        return False, "对方账号不存在"
    db = accounts_store._db()
    with accounts_store._lock:
        db.execute(
            "INSERT OR IGNORE INTO dm_blocks (blocker, blocked, created) "
            "VALUES (?,?,?)",
            (username, target, int(time.time())),
        )
        db.commit()
    return True, "已拉黑，对方消息将不再投递"


def unblock(username: str, target: str) -> tuple[bool, str]:
    _ensure()
    username = str(username or "").strip()
    target = str(target or "").strip()
    if not username or not target:
        return False, "参数缺失"
    db = accounts_store._db()
    with accounts_store._lock:
        db.execute(
            "DELETE FROM dm_blocks WHERE blocker = ? AND blocked = ?",
            (username, target),
        )
        db.commit()
    return True, "已解除拉黑"


def is_blocked(blocker: str, blocked: str) -> bool:
    """blocker 是否拉黑了 blocked。"""
    _ensure()
    blocker = str(blocker or "").strip()
    blocked = str(blocked or "").strip()
    if not blocker or not blocked:
        return False
    row = accounts_store._db().execute(
        "SELECT 1 FROM dm_blocks WHERE blocker = ? AND blocked = ?",
        (blocker, blocked),
    ).fetchone()
    return row is not None


def blocks(username: str) -> list[dict[str, Any]]:
    """我的拉黑名单（含对方昵称，供设置页渲染）。"""
    _ensure()
    username = str(username or "").strip()
    if not username:
        return []
    rows = accounts_store._db().execute(
        """
        SELECT b.blocked AS username, b.created AS created, u.nick AS nick
        FROM dm_blocks b
        LEFT JOIN users u ON u.username = b.blocked
        WHERE b.blocker = ?
        ORDER BY b.created DESC
        """,
        (username,),
    ).fetchall()
    return [{
        "username": str(r["username"] or ""),
        "nick": str(r["nick"] or r["username"] or ""),
        "created": int(r["created"] or 0),
    } for r in rows]


# ---------- 清理 ----------


def prune(days: int = RETENTION_DAYS) -> int:
    """删除超过保留期的消息与随之变空的会话，返回删除消息数。"""
    _ensure()
    cutoff = int(time.time()) - max(1, int(days)) * 86400
    db = accounts_store._db()
    with accounts_store._lock:
        cur = db.execute("DELETE FROM dm_messages WHERE created < ?", (cutoff,))
        removed = int(cur.rowcount or 0)
        db.execute(
            "DELETE FROM dm_conversations WHERE id NOT IN "
            "(SELECT DISTINCT convo_id FROM dm_messages)"
        )
        db.commit()
    return removed
