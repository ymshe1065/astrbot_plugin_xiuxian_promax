# -*- coding: utf-8 -*-
"""webroom 房间服务：aiohttp + WebSocket

- 运行在 AstrBot 现有 asyncio 事件循环内（不另起线程）
- 房间维护在线连接表；机器人回复/世界播报广播给房间所有人
- 私聊通道：带 req_id 的查询回复只回给请求者（看板数据不进公屏）
- 玩家消息交给适配器构造 AstrBotMessage 后 commit_event 进流水线
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from . import accounts as accounts_store
from . import commands as commands_catalog

if TYPE_CHECKING:
    from .webroom_adapter import WebroomAdapter

MEDIA_DIR = Path(get_astrbot_data_path()) / "webroom" / "imgs"
AVATAR_DIR = Path(get_astrbot_data_path()) / "webroom" / "avatars"
STATIC_DIR = Path(__file__).parent / "static"

# 全服唯一聊天室：不做房间区分，所有账号都进这里
ROOM_ID = "room_main"

# 内置头像（static/img/avatars/<name>.svg），前端展示同一套
PRESET_AVATARS = (
    "taiji",
    "sword",
    "cloud",
    "moon",
    "lotus",
    "bamboo",
    "cauldron",
    "fish",
    "star",
)

_AVATAR_FILE_RE = re.compile(r"^[a-f0-9]{16}\.png$")
_AVATAR_B64_MAX = 3 * 1024 * 1024   # data URL 上限（约 2.2MB 原图）
_AVATAR_RAW_MAX = 2 * 1024 * 1024   # 解码后上限
_AVATAR_SIDE = 160                  # 统一缩放边长

# room_id -> {ws: {"player_id": str, "player_name": str}}
_rooms: dict[str, dict[web.WebSocketResponse, dict[str, str]]] = {}
_adapter: WebroomAdapter | None = None
_runner: web.AppRunner | None = None
_site: web.TCPSite | None = None


# ---------- 图片落盘 ----------


def save_image_b64(data_b64: str) -> str:
    """落盘机器人回复中的图片，返回可访问的 URL 路径。"""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.jpg"
    path = MEDIA_DIR / filename
    path.write_bytes(base64.b64decode(data_b64))
    return f"/media/{filename}"


def set_adapter(adapter: WebroomAdapter) -> None:
    global _adapter
    _adapter = adapter


def room_name(room_id: str) -> str:
    return "修仙聊天室"


def online_players(room_id: str) -> list[str]:
    return [p["nick"] for p in online_detail(room_id)]


def online_detail(room_id: str) -> list[dict[str, Any]]:
    """在线名单（按账号去重，同一账号多开只算一个）。"""
    conns = _rooms.get(room_id) or {}
    seen: dict[str, dict[str, Any]] = {}
    for info in conns.values():
        username = str(info.get("username") or "")
        if not username or username in seen:
            continue
        seen[username] = {
            "username": username,
            "nick": info["player_name"],
            "avatar": info.get("avatar", ""),
        }
    return sorted(seen.values(), key=lambda p: p["nick"])


async def broadcast_presence(room_id: str) -> None:
    """在线名单变化时推给房间所有人，前端据此刷新列表和计数。"""
    await broadcast(
        room_id,
        {
            "type": "presence",
            "room": room_id,
            "players": online_detail(room_id),
            "ts": int(time.time()),
        },
    )


# ---------- 生命周期 ----------


@web.middleware
async def _cache_control(request: web.Request, handler):
    """静态资源走协商缓存：ETag 命中返回 304，改动后立刻拉新的。

    不设 Cache-Control 时浏览器会启发式强缓存，改完代码用户刷新也看不到变化。
    """
    resp = await handler(request)
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


async def start(host: str, port: int) -> None:
    global _runner, _site
    if _site is not None:
        return
    # client_max_size 放宽到 4MB：头像以 base64 上传，默认 1MB 不够
    app = web.Application(middlewares=[_cache_control], client_max_size=4 * 1024 * 1024)
    accounts_store.init()
    app.router.add_get("/", _index)
    app.router.add_get("/api/commands", _api_commands)
    app.router.add_get("/api/health", _api_health)
    app.router.add_post("/api/register", _api_register)
    app.router.add_post("/api/login", _api_login)
    app.router.add_get("/api/me", _api_me)
    app.router.add_post("/api/nick", _api_nick)
    app.router.add_post("/api/password", _api_password)
    app.router.add_post("/api/avatar", _api_avatar)
    app.router.add_get("/api/bind/status", _api_bind_status)
    app.router.add_post("/api/bind", _api_bind)
    app.router.add_post("/api/bind/unbind", _api_bind_unbind)
    app.router.add_get("/ws", _ws_handler)
    # 旧链接 /ws/{room} 仍然可用，房间号已忽略
    app.router.add_get("/ws/{room_id}", _ws_handler)
    app.router.add_get("/media/{filename}", _media)
    app.router.add_get("/avatar/{filename}", _avatar_file)
    app.router.add_get("/cards/{filename}", _card_image)
    if STATIC_DIR.is_dir():
        app.router.add_static("/static/", STATIC_DIR, name="static")

    _runner = web.AppRunner(app, access_log=None)
    await _runner.setup()
    _site = web.TCPSite(_runner, host, port)
    await _site.start()
    logger.info(f"[webroom] 房间服务已启动: http://{host}:{port}/")


async def stop() -> None:
    global _runner, _site
    for conns in _rooms.values():
        for ws in list(conns):
            await ws.close(code=1001, message=b"server shutdown")
    _rooms.clear()
    if _runner is not None:
        await _runner.cleanup()
    _runner = None
    _site = None
    logger.info("[webroom] 房间服务已停止")


# ---------- 发送 ----------


async def broadcast(room_id: str, payload: dict[str, Any]) -> int:
    """向房间所有在线连接广播 JSON，返回成功送达的连接数。"""
    conns = _rooms.get(room_id)
    if not conns:
        return 0
    sent = 0
    dead: list[web.WebSocketResponse] = []
    for ws in list(conns):
        try:
            await ws.send_json(payload)
            sent += 1
        except Exception:
            dead.append(ws)
    for killed in dead:
        _drop(room_id, killed)
    return sent


def _find_ws(room_id: str, player_id: str) -> web.WebSocketResponse | None:
    for ws, info in (_rooms.get(room_id) or {}).items():
        if info["player_id"] == player_id:
            return ws
    return None


async def send_to_player(room_id: str, player_id: str, payload: dict[str, Any]) -> bool:
    """定向发送给某个玩家（看板/私聊回复）。玩家已离线则返回 False。"""
    ws = _find_ws(room_id, player_id)
    if ws is None:
        return False
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        _drop(room_id, ws)
        return False


async def send_to_ws(ws: web.WebSocketResponse, payload: dict[str, Any]) -> bool:
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        return False


async def broadcast_bot(room_id: str, text: str) -> None:
    """世界事件播报入口（定时 Boss/秘境/拍卖等），广播给房间所有人。"""
    if not text:
        return
    await broadcast_bot_payload(room_id, {"text": text, "images": []})


async def broadcast_bot_payload(room_id: str, payload: dict) -> None:
    """机器人公开回复（广播）。"""
    text = str(payload.get("text", "") or "")
    images = [u for u in (payload.get("images") or []) if u]
    cards = payload.get("cards") or []
    if not text and not images and not cards:
        return
    await broadcast(
        room_id,
        {
            "type": "bot",
            "room": room_id,
            "text": text,
            "images": images,
            "cards": cards,
            "ts": int(time.time()),
        },
    )


async def broadcast_broadcast(room_id: str, payload: dict) -> int:
    """系统播报（世界事件）——前端渲染成置顶横幅。"""
    text = str(payload.get("text", "") or "")
    if not text:
        return 0
    return await broadcast(
        room_id,
        {
            "type": "broadcast",
            "room": room_id,
            "text": text,
            "ts": int(time.time()),
        },
    )


def _drop(room_id: str, ws: web.WebSocketResponse) -> None:
    conns = _rooms.get(room_id)
    if conns is not None:
        conns.pop(ws, None)
        if not conns:
            _rooms.pop(room_id, None)


# ---------- HTTP 资源 ----------


_INDEX_HTML = STATIC_DIR / "index.html"


def _asset_ver() -> str:
    """静态资源版本号取三者 mtime 最大值，任一文件改动即失效旧缓存。"""
    latest = 0.0
    for name in ("index.html", "app.css", "app.js"):
        path = STATIC_DIR / name
        if path.exists():
            latest = max(latest, path.stat().st_mtime)
    return str(int(latest))


async def _index(_request: web.Request) -> web.Response:
    try:
        html = _INDEX_HTML.read_text(encoding="utf-8").replace(
            "__ASSET_VER__", _asset_ver()
        )
    except OSError:
        return web.FileResponse(_INDEX_HTML)
    return web.Response(
        text=html,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


async def _api_commands(_request: web.Request) -> web.Response:
    return web.json_response(commands_catalog.all_commands())


async def _api_health(_request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "adapter": _adapter is not None,
            "accounts": accounts_store.count(),
            "rooms": {rid: online_players(rid) for rid in _rooms},
        }
    )


# ---------- 账号 API ----------


async def _read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _bearer(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.query.get("token", "").strip()


def _token_user(request: web.Request) -> dict | None:
    username = accounts_store.parse_token(_bearer(request))
    if not username:
        return None
    return accounts_store.get(username)


# ---------- QQ 存档绑定（单向：网页账号 -> QQ 号存档） ----------


def _xiuxian_db_path() -> Path:
    return (
        Path(get_astrbot_data_path())
        / "plugin_data"
        / "astrbot_plugin_xiuxian_promax"
        / "xiuxian_data_lite.db"
    )


def _player_save_of(key: str) -> dict | None:
    """只读查主库，看这个存档 key（QQ 号 / web_<账号名>）是否已有角色。

    绑定接口用它确认 QQ 号真的有旧存档，避免填错号时静默造出空角色；
    状态接口用它回显“你现在实际在用哪份存档”。
    """
    if not key:
        return None
    path = _xiuxian_db_path()
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT level_index, cultivation_type, experience FROM players "
                "WHERE user_id = ?",
                (key,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "level_index": row[0],
        "cultivation_type": row[1] or "",
        "experience": row[2],
    }


def _sync_player_key(username: str, key: str) -> None:
    """绑定/解绑后，已在线的连接立即换存档 key，不必刷新页面。"""
    for info in (_rooms.get(ROOM_ID) or {}).values():
        if info.get("username") == username:
            info["player_id"] = key


async def _api_register(request: web.Request) -> web.Response:
    data = await _read_json(request)
    ok, msg = accounts_store.register(
        str(data.get("username", "")),
        str(data.get("password", "")),
        str(data.get("nick", "")),
    )
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    user = accounts_store.get(str(data.get("username", "")))
    assert user is not None
    return web.json_response(
        {
            "ok": True,
            "token": accounts_store.make_token(user["username"]),
            "username": user["username"],
            "nick": user["nick"],
            "avatar": user.get("avatar", ""),
        }
    )


async def _api_login(request: web.Request) -> web.Response:
    data = await _read_json(request)
    ok, msg, user = accounts_store.verify(
        str(data.get("username", "")), str(data.get("password", ""))
    )
    if not ok or user is None:
        return web.json_response({"ok": False, "error": msg}, status=401)
    return web.json_response(
        {
            "ok": True,
            "token": accounts_store.make_token(user["username"]),
            "username": user["username"],
            "nick": user["nick"],
            "avatar": user.get("avatar", ""),
        }
    )


async def _api_me(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    return web.json_response({"ok": True, **user})


async def _api_nick(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    data = await _read_json(request)
    ok, msg = accounts_store.set_nick(user["username"], str(data.get("nick", "")))
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    fresh = accounts_store.get(user["username"]) or user
    # 在线连接同步新昵称，之后发消息立即用新名字
    for info in (_rooms.get(ROOM_ID) or {}).values():
        if info.get("username") == user["username"]:
            info["player_name"] = fresh["nick"]
    await broadcast(
        ROOM_ID,
        {
            "type": "sys",
            "text": f"{user['nick']} 改名为 {fresh['nick']}",
            "ts": int(time.time()),
        },
    )
    await broadcast_presence(ROOM_ID)
    return web.json_response(
        {
            "ok": True,
            "username": fresh["username"],
            "nick": fresh["nick"],
            "avatar": fresh.get("avatar", ""),
        }
    )


async def _api_password(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    data = await _read_json(request)
    ok, msg = accounts_store.set_password(
        user["username"], str(data.get("old", "")), str(data.get("new", ""))
    )
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    return web.json_response({"ok": True, "message": msg})


async def _api_bind_status(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    qq = str(user.get("qq") or "")
    key = accounts_store.player_key_of(user)
    save, web_save = await asyncio.gather(
        asyncio.to_thread(_player_save_of, key),
        asyncio.to_thread(_player_save_of, f"web_{user['username']}"),
    )
    return web.json_response(
        {
            "ok": True,
            "qq": qq,
            "player_key": key,
            "save": save,
            # 网页独立存档：解绑后回到的那份，让用户知道绑过去会“看不见”它
            "web_save": web_save,
        }
    )


async def _api_bind(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    data = await _read_json(request)
    qq = str(data.get("qq", "")).strip()
    # 先卡格式，否则填错会得到“没有找到存档”，看不出是格式问题
    if not accounts_store.QQ_RE.match(qq):
        return web.json_response(
            {"ok": False, "error": "QQ 号格式不对，请填 5-12 位数字"}, status=400
        )
    # 只允许绑到确实存在的旧存档，避免填错号时静默创建空角色
    save, web_save = await asyncio.gather(
        asyncio.to_thread(_player_save_of, qq),
        asyncio.to_thread(_player_save_of, f"web_{user['username']}"),
    )
    if save is None:
        return web.json_response(
            {"ok": False, "error": "没有找到这个 QQ 号的旧存档，请确认号码"}, status=400
        )
    ok, msg = accounts_store.bind_qq(user["username"], qq)
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    key = accounts_store.player_key_of({"username": user["username"], "player_key": qq})
    _sync_player_key(user["username"], key)
    return web.json_response(
        {
            "ok": True,
            "qq": qq,
            "player_key": key,
            "save": save,
            "web_save": web_save,
            "message": msg,
        }
    )


async def _api_bind_unbind(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    ok, msg = accounts_store.unbind_qq(user["username"])
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    key = accounts_store.player_key_of({"username": user["username"], "player_key": ""})
    _sync_player_key(user["username"], key)
    return web.json_response(
        {"ok": True, "qq": "", "player_key": key, "save": None, "message": msg}
    )


# ---------- 头像 ----------


def _avatar_filename(username: str) -> str:
    """按账号派生固定文件名：同一账号换图不堆垃圾文件。"""
    return hashlib.sha256(username.encode("utf-8")).hexdigest()[:16] + ".png"


def _render_avatar(raw: bytes, dest: Path) -> None:
    """裁成正方形并缩放，顺便把图片格式统一成 PNG。

    重新编码是有意为之：既能挡住伪装成图片的其它内容，也能抹掉 EXIF。
    """
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(raw))
    # 挡住 PNG 炸弹：体积小但解压后巨大
    if img.width * img.height > 40_000_000:
        raise ValueError("图片像素过大")
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    # 居中裁正方形，避免窄长图缩完变形
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((_AVATAR_SIDE, _AVATAR_SIDE), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="PNG", optimize=True)


async def _api_avatar(request: web.Request) -> web.Response:
    """设置头像。

    - {"preset": "moon"}  用内置头像
    - {"image": "data:image/png;base64,..."}  上传图片
    - {}                  恢复默认（昵称首字）
    """
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    data = await _read_json(request)
    preset = str(data.get("preset", "")).strip()
    image = data.get("image")

    if preset:
        if preset not in PRESET_AVATARS:
            return web.json_response({"ok": False, "error": "未知的内置头像"}, status=400)
        value = f"/static/img/avatars/{preset}.svg"
    elif isinstance(image, str) and image.strip():
        blob = image.strip()
        if len(blob) > _AVATAR_B64_MAX:
            return web.json_response({"ok": False, "error": "图片太大了，换张小点的（约 2MB 内）"}, status=400)
        b64 = blob.split(",", 1)[1] if blob.startswith("data:") and "," in blob else blob
        try:
            raw = base64.b64decode(b64, validate=False)
        except Exception:
            return web.json_response({"ok": False, "error": "图片数据损坏"}, status=400)
        if len(raw) > _AVATAR_RAW_MAX:
            return web.json_response({"ok": False, "error": "图片太大了，换张小点的（约 2MB 内）"}, status=400)
        dest = AVATAR_DIR / _avatar_filename(user["username"])
        try:
            await asyncio.to_thread(_render_avatar, raw, dest)
        except Exception:
            logger.exception("[webroom] 头像处理失败")
            return web.json_response({"ok": False, "error": "这张图读不出来，换一张试试"}, status=400)
        value = f"/avatar/{dest.name}?v={int(time.time() * 1000)}"
    else:
        value = ""

    ok, msg = accounts_store.set_avatar(user["username"], value)
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)

    for info in (_rooms.get(ROOM_ID) or {}).values():
        if info.get("username") == user["username"]:
            info["avatar"] = value
    await broadcast_presence(ROOM_ID)
    return web.json_response({"ok": True, "avatar": value})


async def _avatar_file(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    if not _AVATAR_FILE_RE.match(filename):
        return web.json_response({"error": "非法路径"}, status=400)
    path = AVATAR_DIR / filename
    if not path.is_file():
        return web.json_response({"error": "文件不存在"}, status=404)
    # 走协商缓存：URL 里虽然带了 ?v=，手动覆盖同名文件时也能立刻生效
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


def _safe_name(filename: str) -> bool:
    return bool(filename) and "/" not in filename and "\\" not in filename and ".." not in filename

async def _media(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    if not _safe_name(filename):
        return web.json_response({"error": "非法路径"}, status=400)
    path = MEDIA_DIR / filename
    if not path.is_file():
        return web.json_response({"error": "文件不存在"}, status=404)
    return web.FileResponse(path)


def _cards_dir() -> Path | None:
    try:
        from ..utils.image_generator import CARD_TEMP_DIR

        return Path(CARD_TEMP_DIR)
    except Exception:
        try:
            from utils.image_generator import CARD_TEMP_DIR  # type: ignore

            return Path(CARD_TEMP_DIR)
        except Exception:
            return None


async def _card_image(request: web.Request) -> web.Response:
    """卡片原图兜底访问（原生渲染不可用时的降级路径）。"""
    filename = request.match_info["filename"]
    base = _cards_dir()
    if not _safe_name(filename) or base is None:
        return web.json_response({"error": "非法路径"}, status=400)
    path = base / filename
    if not path.is_file():
        return web.json_response({"error": "文件不存在"}, status=404)
    return web.FileResponse(path)


# ---------- WebSocket ----------


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    # 房间固定，登录态靠 token 判定
    token = request.query.get("token", "").strip()
    username = accounts_store.parse_token(token)
    user = accounts_store.get(username) if username else None
    if user is None:
        return web.json_response({"error": "未登录或登录已过期"}, status=401)

    room_id = ROOM_ID
    player_name = user["nick"]

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)

    # 顶号：同一账号的旧连接踢下线（换设备/重开页面）
    conns = _rooms.setdefault(room_id, {})
    for old_ws, info in list(conns.items()):
        if info.get("username") == user["username"]:
            try:
                await old_ws.close(code=4001, message=b"replaced by new login")
            except Exception:
                pass
            _drop(room_id, old_ws)
    conns = _rooms.setdefault(room_id, {})
    # 绑定了 QQ 就用 QQ 号当存档 key（与旧机器人的 players.user_id 对齐）
    player_id = accounts_store.player_key_of(user)
    conns[ws] = {
        "player_id": player_id,
        "player_name": player_name,
        "username": user["username"],
        "avatar": user.get("avatar", "") or "",
    }

    await broadcast(
        room_id,
        {"type": "sys", "text": f"{player_name} 进入了房间", "ts": int(time.time())},
    )
    await broadcast_presence(room_id)
    await ws.send_json(
        {
            "type": "hello",
            "room": room_id,
            "player": player_name,
            "nick": player_name,
            "username": user["username"],
            "avatar": user.get("avatar", "") or "",
            "online": online_detail(room_id),
            "prefetch": commands_catalog.PANEL_PREFETCH,
        }
    )
    logger.info(f"[webroom] {player_name} 进入 {room_id}，在线 {len(conns)} 人")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await _handle_client_message(room_id, ws, msg.data)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        info = conns.get(ws)
        _drop(room_id, ws)
        if info:
            await broadcast(
                room_id,
                {"type": "sys", "text": f"{info['player_name']} 离开了房间", "ts": int(time.time())},
            )
            await broadcast_presence(room_id)
            logger.info(f"[webroom] {info['player_name']} 离开 {room_id}")
    return ws


async def _handle_client_message(room_id: str, ws: web.WebSocketResponse, raw: str) -> None:
    try:
        data = json.loads(raw)
    except Exception:
        await send_to_ws(ws, {"type": "error", "text": "消息格式错误"})
        return
    if not isinstance(data, dict):
        await send_to_ws(ws, {"type": "error", "text": "消息格式错误"})
        return

    action = data.get("action")
    if action == "ping":
        await send_to_ws(ws, {"type": "pong"})
        return
    if action == "online":
        await send_to_ws(
            ws, {"type": "presence", "players": online_detail(room_id)}
        )
        return
    if action not in ("msg", "cmd"):
        await send_to_ws(ws, {"type": "error", "text": f"未知 action: {action}"})
        return

    text = str(data.get("text", "")).strip()
    if not text:
        return
    if len(text) > 2000:
        await send_to_ws(ws, {"type": "error", "text": "消息过长（>2000 字）"})
        return

    info = _rooms.get(room_id, {}).get(ws)
    if info is None:
        await send_to_ws(ws, {"type": "error", "text": "尚未完成连接握手"})
        return

    private = action == "cmd" or bool(data.get("private"))
    req_id = str(data.get("req_id") or "").strip()[:64]
    # 看板请求：前端要原生渲染，后端优先返回结构化卡片
    want_card = bool(data.get("card"))

    if not private:
        # 公屏消息：广播给房间（含发送者自己）
        payload = {
            "type": "chat",
            "room": room_id,
            "from": info["player_name"],
            "username": info.get("username", ""),
            "avatar": info.get("avatar", ""),
            "text": text,
            "ts": int(time.time()),
        }
        conns = _rooms.get(room_id, {})
        dead = []
        for client_ws in list(conns):
            try:
                await client_ws.send_json(payload)
            except Exception:
                dead.append(client_ws)
        for killed in dead:
            _drop(room_id, killed)

    if _adapter is None:
        if private:
            await send_to_ws(
                ws,
                {"type": "reply", "req_id": req_id, "text": "机器人适配器未就绪", "error": True},
            )
        else:
            await send_to_ws(ws, {"type": "error", "text": "机器人适配器未就绪"})
        return

    try:
        await _adapter.on_player_message(
            room_id,
            info["player_id"],
            info["player_name"],
            text,
            req_id=req_id or None,
            private=private,
            want_card=want_card,
        )
    except Exception:
        logger.exception("[webroom] 提交玩家消息到流水线失败")
        if private:
            await send_to_ws(
                ws,
                {"type": "reply", "req_id": req_id, "text": "提交失败，请稍后再试", "error": True},
            )
