# -*- coding: utf-8 -*-
"""webroom 房间服务：aiohttp + WebSocket

- 运行在 AstrBot 现有 asyncio 事件循环内（不另起线程）
- 房间维护在线连接表；机器人回复/世界播报广播给房间所有人
- 私聊通道：带 req_id 的查询回复只回给请求者（看板数据不进公屏）
- 玩家消息交给适配器构造 AstrBotMessage 后 commit_event 进流水线
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from . import accounts as accounts_store
from . import commands as commands_catalog
from .arg_options import build_arg_options

if TYPE_CHECKING:
    from .webroom_adapter import WebroomAdapter
    from ..main import XiuXianPlugin

MEDIA_DIR = Path(get_astrbot_data_path()) / "webroom" / "imgs"
STATIC_DIR = Path(__file__).parent / "static"

# 全服唯一聊天室：不做房间区分，所有账号都进这里
ROOM_ID = "room_main"

# room_id -> {ws: {"player_id": str, "player_name": str}}
_rooms: dict[str, dict[web.WebSocketResponse, dict[str, str]]] = {}
_adapter: WebroomAdapter | None = None
_plugin: "XiuXianPlugin | None" = None
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


def set_plugin(plugin: "XiuXianPlugin") -> None:
    """注入插件实例，供 /api/arg_options 读取玩家真实存档。"""
    global _plugin
    _plugin = plugin


def room_name(room_id: str) -> str:
    return "修仙聊天室"


def online_players(room_id: str) -> list[str]:
    conns = _rooms.get(room_id, {})
    return sorted({info["player_name"] for info in conns.values()})


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
    app = web.Application(middlewares=[_cache_control])
    accounts_store.init()
    app.router.add_get("/", _index)
    app.router.add_get("/api/commands", _api_commands)
    app.router.add_get("/api/arg_options", _api_arg_options)
    app.router.add_get("/api/health", _api_health)
    app.router.add_post("/api/register", _api_register)
    app.router.add_post("/api/login", _api_login)
    app.router.add_get("/api/me", _api_me)
    app.router.add_post("/api/nick", _api_nick)
    app.router.add_post("/api/password", _api_password)
    app.router.add_get("/ws", _ws_handler)
    # 旧链接 /ws/{room} 仍然可用，房间号已忽略
    app.router.add_get("/ws/{room_id}", _ws_handler)
    app.router.add_get("/media/{filename}", _media)
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


async def _api_arg_options(request: web.Request) -> web.Response:
    """按指令 + 已填参数返回下一段的可点候选（储物戒物品、职业、宗门成员等）。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    cmd = str(request.query.get("cmd", "") or "")
    values = [str(v) for v in request.query.getall("value", []) if str(v).strip()]
    raw_values = str(request.query.get("values", "") or "").strip()
    if raw_values:
        try:
            parsed = json.loads(raw_values)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            values = [str(v) for v in parsed if str(v).strip()]

    user_id = f"web_{user['username']}"
    try:
        data = await build_arg_options(_plugin, user_id, cmd, values)
    except Exception as exc:
        logger.exception("[webroom] 读取参数候选失败")
        return web.json_response({"ok": False, "error": f"候选读取失败：{exc}"}, status=500)
    return web.json_response(data)


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
    return web.json_response({"ok": True, "username": fresh["username"], "nick": fresh["nick"]})


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
    player_id = f"web_{user['username']}"
    conns[ws] = {
        "player_id": player_id,
        "player_name": player_name,
        "username": user["username"],
    }

    await broadcast(
        room_id,
        {"type": "sys", "text": f"{player_name} 进入了房间", "ts": int(time.time())},
    )
    await ws.send_json(
        {
            "type": "hello",
            "room": room_id,
            "player": player_name,
            "nick": player_name,
            "username": user["username"],
            "online": online_players(room_id),
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
        await send_to_ws(ws, {"type": "online", "players": online_players(room_id)})
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
