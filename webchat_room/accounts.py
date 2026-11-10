# -*- coding: utf-8 -*-
"""webroom 账号系统。

- SQLite 存储，密码 PBKDF2-SHA256 加盐哈希，明文绝不落盘
- token 用 HMAC 自签（含过期时间），服务重启后仍有效，不需要 Redis
- 账号名默认就是插件侧的玩家标识（sender_id = web_<账号名>），改昵称不影响存档
- 绑定 QQ 号后存档标识改为该 QQ 号（players.user_id），与旧机器人数据对齐
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

STORE_DIR = Path(get_astrbot_data_path()) / "webroom"
DB_PATH = STORE_DIR / "accounts.db"
SECRET_PATH = STORE_DIR / "secret.key"

TOKEN_TTL = 7 * 24 * 3600  # 7 天
PBKDF2_ROUNDS = 200_000

USERNAME_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9_]{2,16}$")
QQ_RE = re.compile(r"^[1-9]\d{4,11}$")
NICK_MAX = 16
PWD_MIN = 6
PWD_MAX = 64
AVATAR_MAX = 200

# 网页端消息可见性默认值。查询看板（我的信息/储物戒）固定私密，其余动作默认公开。
DEFAULT_SETTINGS: dict[str, str] = {
    "manual_visibility": "public",       # public / self
    "panel_visibility": "self",          # self / public（保留，默认看板仅自己）
    "auto_visibility": "self",           # self / public_summary / public_full
    "battle_detail_visibility": "public",  # public / self
    "battle_detail_default": "collapsed",  # collapsed / expanded
    "view_filter": "all",                # all / self（只看自己的消息）
}

_SETTING_VALUES: dict[str, set[str]] = {
    "manual_visibility": {"public", "self"},
    "panel_visibility": {"self", "public"},
    "auto_visibility": {"self", "public_summary", "public_full"},
    "battle_detail_visibility": {"public", "self"},
    "battle_detail_default": {"collapsed", "expanded"},
    "view_filter": {"all", "self"},
}

NOTICE_KEEP = 200       # 每个账号最多保留的通知条数
NOTICE_TTL = 30 * 24 * 3600  # 通知最长保留 30 天

# 意见反馈：类型、内容长度、提交频率与保留量
FEEDBACK_KINDS: dict[str, str] = {
    "bug": "BUG 反馈",
    "suggestion": "玩法建议",
    "balance": "数值平衡",
    "other": "其他",
}
FEEDBACK_STATUS: dict[int, str] = {0: "待处理", 1: "已处理", 2: "已忽略"}
FEEDBACK_CONTENT_MIN = 5
FEEDBACK_CONTENT_MAX = 1000
FEEDBACK_CONTACT_MAX = 64
FEEDBACK_COOLDOWN = 60        # 同一账号两次提交的最短间隔（秒）
FEEDBACK_DAILY_LIMIT = 10     # 同一账号 24 小时内最多提交条数
FEEDBACK_KEEP = 200           # 每个账号最多保留的反馈条数

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_secret: bytes = b""


# ---------- 初始化 ----------


def init() -> None:
    """建库建表、准备签名密钥。重复调用安全。"""
    global _conn, _secret
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    if not SECRET_PATH.exists():
        SECRET_PATH.write_bytes(secrets.token_bytes(32))
        try:  # 尽量收紧权限（Windows 上是 no-op）
            os.chmod(SECRET_PATH, 0o600)
        except OSError:
            pass
    _secret = SECRET_PATH.read_bytes()

    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username   TEXT PRIMARY KEY,
                nick       TEXT NOT NULL,
                pwd        TEXT NOT NULL,
                created    INTEGER NOT NULL,
                last_login INTEGER NOT NULL DEFAULT 0,
                avatar     TEXT NOT NULL DEFAULT '',
                qq         TEXT NOT NULL DEFAULT '',
                player_key TEXT NOT NULL DEFAULT '',
                settings   TEXT NOT NULL DEFAULT '{}',
                room_seen  INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # 老库补列（头像为空 = 用昵称首字）
        cols = {row["name"] for row in _conn.execute("PRAGMA table_info(users)")}
        if "avatar" not in cols:
            _conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT NOT NULL DEFAULT ''")
        if "qq" not in cols:
            _conn.execute("ALTER TABLE users ADD COLUMN qq TEXT NOT NULL DEFAULT ''")
        if "player_key" not in cols:
            _conn.execute("ALTER TABLE users ADD COLUMN player_key TEXT NOT NULL DEFAULT ''")
        if "settings" not in cols:
            _conn.execute("ALTER TABLE users ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'")
        # 进过房间的时间：0 = 从没进过，房间成员列表据此区分在线/离线
        if "room_seen" not in cols:
            _conn.execute("ALTER TABLE users ADD COLUMN room_seen INTEGER NOT NULL DEFAULT 0")
        # 一个 QQ 号只能被一个网页账号绑定（空串不参与唯一约束）
        _conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_qq "
            "ON users(qq) WHERE qq <> ''"
        )
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS webroom_notices (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT NOT NULL,
                player_key  TEXT NOT NULL DEFAULT '',
                channel     TEXT NOT NULL DEFAULT 'auto',
                category    TEXT NOT NULL DEFAULT 'auto_proxy',
                title       TEXT NOT NULL DEFAULT '',
                summary     TEXT NOT NULL DEFAULT '',
                detail      TEXT NOT NULL DEFAULT '',
                payload     TEXT NOT NULL DEFAULT '{}',
                created     INTEGER NOT NULL,
                is_read     INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notices_user_time "
            "ON webroom_notices(username, created DESC)"
        )
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS webroom_feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT NOT NULL,
                player_key  TEXT NOT NULL DEFAULT '',
                kind        TEXT NOT NULL DEFAULT 'other',
                content     TEXT NOT NULL,
                contact     TEXT NOT NULL DEFAULT '',
                snapshot    TEXT NOT NULL DEFAULT '{}',
                status      INTEGER NOT NULL DEFAULT 0,
                admin_note  TEXT NOT NULL DEFAULT '',
                created     INTEGER NOT NULL,
                updated     INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_user_time "
            "ON webroom_feedback(username, id DESC)"
        )
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_status_time "
            "ON webroom_feedback(status, id DESC)"
        )
        _conn.commit()


def _db() -> sqlite3.Connection:
    if _conn is None:
        init()
    assert _conn is not None
    return _conn


# ---------- 密码 ----------


def _hash_pwd(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def _check_pwd(password: str, stored: str) -> bool:
    try:
        _, rounds, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------- token ----------


def _sign(payload: str) -> str:
    mac = hmac.new(_secret, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def make_token(username: str) -> str:
    exp = int(time.time()) + TOKEN_TTL
    payload = base64.urlsafe_b64encode(f"{username}|{exp}".encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload)}"


def parse_token(token: str) -> str | None:
    """校验 token，返回账号名；无效/过期返回 None。"""
    if not token or not token.isascii() or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        username, exp = raw.rsplit("|", 1)
        if int(exp) < time.time():
            return None
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return username or None


# ---------- 账号操作 ----------


def _norm(username: str) -> str:
    return (username or "").strip()


def get(username: str) -> dict[str, Any] | None:
    row = _db().execute(
        "SELECT username, nick, avatar, created, last_login, qq, player_key "
        "FROM users WHERE username = ?",
        (_norm(username),),
    ).fetchone()
    return dict(row) if row else None


def register(username: str, password: str, nick: str = "") -> tuple[bool, str]:
    """注册。成功返回 (True, 提示语)，失败返回 (False, 原因)。"""
    username = _norm(username)
    nick = (nick or "").strip() or username
    if not USERNAME_RE.match(username):
        return False, "账号需 2-16 位，仅支持中文、字母、数字、下划线"
    if len(password) < PWD_MIN or len(password) > PWD_MAX:
        return False, f"密码需 {PWD_MIN}-{PWD_MAX} 位"
    if len(nick) > NICK_MAX:
        return False, f"昵称最长 {NICK_MAX} 个字"
    with _lock:
        db = _db()
        exists = db.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        if exists:
            return False, "该道号已被占，换一个吧"
        db.execute(
            "INSERT INTO users (username, nick, pwd, created, last_login) VALUES (?,?,?,?,?)",
            (username, nick, _hash_pwd(password), int(time.time()), 0),
        )
        db.commit()
    return True, "注册成功"


def verify(username: str, password: str) -> tuple[bool, str, dict[str, Any] | None]:
    """校验密码。返回 (ok, 提示语, 用户信息)。"""
    username = _norm(username)
    row = _db().execute(
        "SELECT username, nick, avatar, pwd, qq, player_key FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if row is None:
        return False, "账号不存在，先去注册", None
    if not _check_pwd(password, row["pwd"]):
        return False, "密码不对，再想想", None
    with _lock:
        db = _db()
        db.execute(
            "UPDATE users SET last_login = ? WHERE username = ?",
            (int(time.time()), username),
        )
        db.commit()
    return (
        True,
        "登录成功",
        {
            "username": row["username"],
            "nick": row["nick"],
            "avatar": row["avatar"] or "",
            "qq": row["qq"] or "",
            "player_key": row["player_key"] or "",
        },
    )


def set_nick(username: str, nick: str) -> tuple[bool, str]:
    nick = (nick or "").strip()
    if not nick:
        return False, "昵称不能为空"
    if len(nick) > NICK_MAX:
        return False, f"昵称最长 {NICK_MAX} 个字"
    with _lock:
        db = _db()
        cur = db.execute("UPDATE users SET nick = ? WHERE username = ?", (nick, _norm(username)))
        db.commit()
        if cur.rowcount == 0:
            return False, "账号不存在"
    return True, "昵称已更新"


def set_avatar(username: str, avatar: str) -> tuple[bool, str]:
    """设置头像：空串 = 回到昵称首字；其余为可访问的图片 URL。"""
    avatar = (avatar or "").strip()
    if len(avatar) > AVATAR_MAX:
        return False, "头像标识过长"
    if avatar and not avatar.startswith("/"):
        return False, "头像标识非法"
    with _lock:
        db = _db()
        cur = db.execute(
            "UPDATE users SET avatar = ? WHERE username = ?", (avatar, _norm(username))
        )
        db.commit()
        if cur.rowcount == 0:
            return False, "账号不存在"
    return True, "头像已更新"


def set_password(username: str, old: str, new: str) -> tuple[bool, str]:
    ok, msg, _ = verify(username, old)
    if not ok:
        return False, msg
    if len(new) < PWD_MIN or len(new) > PWD_MAX:
        return False, f"新密码需 {PWD_MIN}-{PWD_MAX} 位"
    with _lock:
        db = _db()
        db.execute("UPDATE users SET pwd = ? WHERE username = ?", (_hash_pwd(new), _norm(username)))
        db.commit()
    return True, "密码已修改"


def count() -> int:
    row = _db().execute("SELECT COUNT(*) AS c FROM users").fetchone()
    return int(row["c"]) if row else 0


# ---------- 房间成员（进过房间的账号） ----------


def touch_room(username: str) -> None:
    """记录账号最近一次进入房间的时间；进过房间的人才会出现在成员列表里。"""
    name = _norm(username)
    if not name:
        return
    conn = _db()
    conn.execute("UPDATE users SET room_seen = ? WHERE username = ?", (int(time.time()), name))
    conn.commit()


def list_room_members() -> list[dict[str, Any]]:
    """所有进过房间的账号，最近进过的排前面。"""
    rows = _db().execute(
        "SELECT username, nick, avatar, room_seen FROM users "
        "WHERE room_seen > 0 ORDER BY room_seen DESC, nick ASC"
    ).fetchall()
    return [
        {
            "username": row["username"],
            "nick": row["nick"] or row["username"],
            "avatar": row["avatar"] or "",
            "seen": int(row["room_seen"] or 0),
        }
        for row in rows
    ]


# ---------- QQ 存档绑定（单向：网页账号 -> QQ 号存档） ----------


def player_key_of(user: dict[str, Any]) -> str:
    """网页账号实际使用的存档 key。

    未绑定时是 web_<账号名>（兼容现有存档）；绑定后直接是 QQ 号，
    于是和旧 QQ 机器人的 players.user_id 完全对齐，业务层无需任何改动。
    """
    key = str((user or {}).get("player_key") or "").strip()
    return key or f"web_{(user or {}).get('username', '')}"


def bind_qq(username: str, qq: str) -> tuple[bool, str]:
    """把网页账号的存档指针指向某个 QQ 号。不搬数据、不覆盖任何存档。"""
    qq = (qq or "").strip()
    if not QQ_RE.match(qq):
        return False, "QQ 号格式不对，请填 5-12 位数字"
    username = _norm(username)
    with _lock:
        db = _db()
        if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone() is None:
            return False, "账号不存在"
        taken = db.execute(
            "SELECT username FROM users WHERE qq = ? AND username <> ?", (qq, username)
        ).fetchone()
        if taken is not None:
            return False, f"该 QQ 号已被「{taken['username']}」绑定"
        db.execute(
            "UPDATE users SET qq = ?, player_key = ? WHERE username = ?",
            (qq, qq, username),
        )
        db.commit()
    return True, "绑定成功"


def unbind_qq(username: str) -> tuple[bool, str]:
    """解除绑定：回到 web_<账号名> 那份独立存档，QQ 存档原样保留。"""
    username = _norm(username)
    with _lock:
        db = _db()
        cur = db.execute(
            "UPDATE users SET qq = '', player_key = '' WHERE username = ?", (username,)
        )
        db.commit()
        if cur.rowcount == 0:
            return False, "账号不存在"
    return True, "已解除绑定"


# ---------- 消息可见性设置 ----------


def get_settings(username: str) -> dict[str, str]:
    """读取消息设置，缺失字段用默认值补齐。"""
    row = _db().execute(
        "SELECT settings FROM users WHERE username = ?", (_norm(username),)
    ).fetchone()
    raw: dict[str, Any] = {}
    if row and row["settings"]:
        try:
            parsed = json.loads(row["settings"])
            if isinstance(parsed, dict):
                raw = parsed
        except (TypeError, ValueError):
            raw = {}
    out = dict(DEFAULT_SETTINGS)
    for key, value in raw.items():
        if key in _SETTING_VALUES and isinstance(value, str) and value in _SETTING_VALUES[key]:
            out[key] = value
    return out


def set_settings(
    username: str, patch: dict[str, Any]
) -> tuple[bool, str, dict[str, str]]:
    """只更新已知设置项，未知键忽略，非法值拒绝。"""
    current = get_settings(username)
    if not isinstance(patch, dict):
        return False, "配置格式错误", current
    for key, value in patch.items():
        if key not in _SETTING_VALUES:
            continue
        if not isinstance(value, str) or value not in _SETTING_VALUES[key]:
            return False, f"不支持的配置：{key}={value}", current
        current[key] = value
    with _lock:
        db = _db()
        cur = db.execute(
            "UPDATE users SET settings = ? WHERE username = ?",
            (json.dumps(current, ensure_ascii=False), _norm(username)),
        )
        db.commit()
        if cur.rowcount == 0:
            return False, "账号不存在", current
    return True, "设置已保存", current


def username_of_player_key(player_key: str) -> str:
    """把插件侧 player_id（web_名字 / QQ号）解析成网页账号名。"""
    key = str(player_key or "").strip()
    if not key:
        return ""
    db = _db()
    if key.startswith("web_"):
        name = key[4:]
        row = db.execute(
            "SELECT username FROM users WHERE username = ?", (name,)
        ).fetchone()
        if row:
            return str(row["username"])
    row = db.execute(
        "SELECT username FROM users WHERE player_key = ? LIMIT 1", (key,)
    ).fetchone()
    return str(row["username"]) if row else ""


# ---------- 个人消息历史 ----------


def _notice_row(row: sqlite3.Row) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        parsed = json.loads(row["payload"] or "{}")
        if isinstance(parsed, dict):
            payload = parsed
    except (TypeError, ValueError):
        payload = {}
    return {
        "id": int(row["id"]),
        "channel": row["channel"],
        "category": row["category"],
        "title": row["title"],
        "summary": row["summary"],
        "detail": row["detail"],
        "payload": payload,
        "created": int(row["created"]),
        "is_read": bool(row["is_read"]),
    }


def add_notice(
    username: str,
    player_key: str = "",
    channel: str = "auto",
    category: str = "auto_proxy",
    title: str = "",
    summary: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
) -> int:
    """写入个人消息，并做条数/时间清理；返回新消息 id。"""
    username = _norm(username)
    now = int(time.time())
    blob = json.dumps(payload or {}, ensure_ascii=False)
    with _lock:
        db = _db()
        cur = db.execute(
            "INSERT INTO webroom_notices "
            "(username, player_key, channel, category, title, summary, detail, payload, created, is_read) "
            "VALUES (?,?,?,?,?,?,?,?,?,0)",
            (
                username,
                str(player_key or ""),
                str(channel or "auto"),
                str(category or "auto_proxy"),
                str(title or ""),
                str(summary or ""),
                str(detail or ""),
                blob,
                now,
            ),
        )
        notice_id = int(cur.lastrowid)
        db.execute(
            "DELETE FROM webroom_notices WHERE username = ? AND created < ?",
            (username, now - NOTICE_TTL),
        )
        db.execute(
            "DELETE FROM webroom_notices WHERE username = ? AND id NOT IN ("
            "SELECT id FROM webroom_notices WHERE username = ? ORDER BY id DESC LIMIT ?)",
            (username, username, NOTICE_KEEP),
        )
        db.commit()
    return notice_id


def count_unread(username: str) -> int:
    row = _db().execute(
        "SELECT COUNT(*) AS c FROM webroom_notices WHERE username = ? AND is_read = 0",
        (_norm(username),),
    ).fetchone()
    return int(row["c"]) if row else 0


def list_notices(
    username: str, limit: int = 50, before: int = 0, unread_only: bool = False
) -> tuple[list[dict[str, Any]], int]:
    """按时间倒序返回通知，附带未读数。"""
    username = _norm(username)
    limit = max(1, min(int(limit or 50), 100))
    before = int(before or 0)
    sql = "SELECT * FROM webroom_notices WHERE username = ?"
    args: list[Any] = [username]
    # SQLite INTEGER 只有 64 位；比最大 ID 更大的游标等价于第一页。
    if 0 < before <= 2**63 - 1:
        sql += " AND id < ?"
        args.append(int(before))
    if unread_only:
        sql += " AND is_read = 0"
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = _db().execute(sql, args).fetchall()
    return [_notice_row(row) for row in rows], count_unread(username)


def mark_notices_read(
    username: str, ids: list[int] | None = None, all_read: bool = False
) -> int:
    username = _norm(username)
    with _lock:
        db = _db()
        if all_read:
            cur = db.execute(
                "UPDATE webroom_notices SET is_read = 1 WHERE username = ? AND is_read = 0",
                (username,),
            )
        else:
            clean = set()
            for value in ids or []:
                if isinstance(value, bool) or not isinstance(value, (int, str)):
                    continue
                raw = str(value)
                if len(raw) <= 19 and raw.isascii() and raw.isdecimal():
                    item_id = int(raw)
                    if 0 < item_id <= 2**63 - 1:
                        clean.add(item_id)
            if not clean:
                return 0
            marks = ",".join("?" for _ in clean)
            cur = db.execute(
                f"UPDATE webroom_notices SET is_read = 1 "
                f"WHERE username = ? AND is_read = 0 AND id IN ({marks})",
                [username, *clean],
            )
        db.commit()
        return int(cur.rowcount or 0)


def clear_read_notices(username: str) -> int:
    username = _norm(username)
    with _lock:
        db = _db()
        cur = db.execute(
            "DELETE FROM webroom_notices WHERE username = ? AND is_read = 1",
            (username,),
        )
        db.commit()
        return int(cur.rowcount or 0)


# ---------- 意见反馈 ----------


def _feedback_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        snapshot = json.loads(row["snapshot"] or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    status = int(row["status"] or 0)
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "player_key": row["player_key"],
        "kind": row["kind"],
        "kind_label": FEEDBACK_KINDS.get(row["kind"], FEEDBACK_KINDS["other"]),
        "content": row["content"],
        "contact": row["contact"],
        "snapshot": snapshot,
        "status": status,
        "status_label": FEEDBACK_STATUS.get(status, FEEDBACK_STATUS[0]),
        "admin_note": row["admin_note"],
        "created": int(row["created"]),
        "updated": int(row["updated"]),
    }


def add_feedback(
    username: str,
    player_key: str = "",
    kind: str = "other",
    content: str = "",
    contact: str = "",
    snapshot: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """写入一条反馈；带 60 秒冷却与每日上限，超长内容按上限截断。"""
    username = _norm(username)
    if not username:
        return False, "请先登录", None
    kind = str(kind or "").strip()
    if kind not in FEEDBACK_KINDS:
        return False, "请选择反馈类型", None
    content = str(content or "").strip()
    if len(content) < FEEDBACK_CONTENT_MIN:
        return False, f"反馈内容至少写 {FEEDBACK_CONTENT_MIN} 个字", None
    content = content[:FEEDBACK_CONTENT_MAX]
    contact = str(contact or "").strip()[:FEEDBACK_CONTACT_MAX]
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    now = int(time.time())
    blob = json.dumps(snapshot, ensure_ascii=False)

    with _lock:
        db = _db()
        last = db.execute(
            "SELECT created FROM webroom_feedback WHERE username = ? "
            "ORDER BY id DESC LIMIT 1",
            (username,),
        ).fetchone()
        if last is not None:
            wait = FEEDBACK_COOLDOWN - (now - int(last["created"] or 0))
            if wait > 0:
                return False, f"提交太快了，请 {wait} 秒后再试", None
        recent = db.execute(
            "SELECT COUNT(*) AS c FROM webroom_feedback "
            "WHERE username = ? AND created >= ?",
            (username, now - 86400),
        ).fetchone()
        if recent is not None and int(recent["c"] or 0) >= FEEDBACK_DAILY_LIMIT:
            return False, "今天的反馈数量已达上限，请明天再来", None
        cur = db.execute(
            "INSERT INTO webroom_feedback "
            "(username, player_key, kind, content, contact, snapshot, status, admin_note, created, updated) "
            "VALUES (?,?,?,?,?,?,0,'',?,0)",
            (username, str(player_key or ""), kind, content, contact, blob, now),
        )
        feedback_id = int(cur.lastrowid)
        db.execute(
            "DELETE FROM webroom_feedback WHERE username = ? AND id NOT IN ("
            "SELECT id FROM webroom_feedback WHERE username = ? ORDER BY id DESC LIMIT ?)",
            (username, username, FEEDBACK_KEEP),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM webroom_feedback WHERE id = ?", (feedback_id,)
        ).fetchone()

    if row is None:
        return False, "反馈保存失败，请稍后再试", None
    return True, "反馈已提交，感谢你的建议", _feedback_row(row)


def list_feedback(
    username: str | None = None,
    status: int | None = None,
    limit: int = 50,
    before: int = 0,
) -> list[dict[str, Any]]:
    """按时间倒序返回反馈；username 为空表示管理端全量查询。"""
    limit = max(1, min(int(limit or 50), 100))
    before = int(before or 0)
    sql = "SELECT * FROM webroom_feedback WHERE 1=1"
    args: list[Any] = []
    if username:
        sql += " AND username = ?"
        args.append(_norm(username))
    if status is not None:
        sql += " AND status = ?"
        args.append(int(status))
    if 0 < before <= 2**63 - 1:
        sql += " AND id < ?"
        args.append(int(before))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = _db().execute(sql, args).fetchall()
    return [_feedback_row(row) for row in rows]


def count_feedback(status: int | None = None) -> int:
    sql = "SELECT COUNT(*) AS c FROM webroom_feedback"
    args: list[Any] = []
    if status is not None:
        sql += " WHERE status = ?"
        args.append(int(status))
    row = _db().execute(sql, args).fetchone()
    return int(row["c"] or 0) if row else 0


def update_feedback_status(
    feedback_id: int, status: int, admin_note: str = ""
) -> tuple[bool, str, dict[str, Any] | None]:
    """管理员把反馈标记为已处理(1)或已忽略(2)，可附一句备注。"""
    try:
        feedback_id = int(feedback_id)
        status = int(status)
    except (TypeError, ValueError):
        return False, "反馈编号或状态不正确", None
    if feedback_id <= 0:
        return False, "反馈编号不正确", None
    if status not in (1, 2):
        return False, "只能标记为已处理或已忽略", None
    note = str(admin_note or "").strip()[:200]
    now = int(time.time())
    with _lock:
        db = _db()
        cur = db.execute(
            "UPDATE webroom_feedback SET status = ?, admin_note = ?, updated = ? "
            "WHERE id = ?",
            (status, note, now, feedback_id),
        )
        db.commit()
        if int(cur.rowcount or 0) <= 0:
            return False, "反馈不存在", None
        row = db.execute(
            "SELECT * FROM webroom_feedback WHERE id = ?", (feedback_id,)
        ).fetchone()
    if row is None:
        return False, "反馈不存在", None
    return True, "已更新处理状态", _feedback_row(row)
