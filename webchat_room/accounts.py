# -*- coding: utf-8 -*-
"""webroom 账号系统。

- SQLite 存储，密码 PBKDF2-SHA256 加盐哈希，明文绝不落盘
- token 用 HMAC 自签（含过期时间），服务重启后仍有效，不需要 Redis
- 账号名 = 插件侧的玩家标识（sender_id = web_<账号名>），改昵称不影响存档
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
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
NICK_MAX = 16
PWD_MIN = 6
PWD_MAX = 64

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
                last_login INTEGER NOT NULL DEFAULT 0
            )
            """
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
    if not token or "." not in token:
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
        "SELECT username, nick, created, last_login FROM users WHERE username = ?",
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
        "SELECT username, nick, pwd FROM users WHERE username = ?", (username,)
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
    return True, "登录成功", {"username": row["username"], "nick": row["nick"]}


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
