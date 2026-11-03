# -*- coding: utf-8 -*-
"""webroom 房间服务：aiohttp + WebSocket

- 运行在 AstrBot 现有 asyncio 事件循环内（不另起线程）
- 每个房间维护 在线连接表，机器人回复/世界播报广播给房间所有人
- 玩家消息交给适配器构造 AstrBotMessage 后 commit_event 进流水线
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

if TYPE_CHECKING:
    from .webroom_adapter import WebroomAdapter

MEDIA_DIR = Path(get_astrbot_data_path()) / "webroom" / "imgs"

# room_id -> {ws: {"player_id": str, "player_name": str}}
_rooms: dict[str, dict[web.WebSocketResponse, dict[str, str]]] = {}
_adapter: WebroomAdapter | None = None
_runner: web.AppRunner | None = None
_site: web.TCPSite | None = None


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
    return f"房间 {room_id}"


def online_players(room_id: str) -> list[str]:
    conns = _rooms.get(room_id, {})
    return sorted({info["player_name"] for info in conns.values()})


async def start(host: str, port: int) -> None:
    global _runner, _site
    if _site is not None:
        return
    app = web.Application()
    app.router.add_get("/", _index)
    app.router.add_get("/ws/{room_id}", _ws_handler)
    app.router.add_get("/media/{filename}", _media)

    _runner = web.AppRunner(app, access_log=None)
    await _runner.setup()
    _site = web.TCPSite(_runner, host, port)
    await _site.start()
    logger.info(f"[webroom] 房间服务已启动: ws://{host}:{port}/ws/{{room_id}}")


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
    for ws in dead:
        _drop(room_id, ws)
    return sent


async def broadcast_bot(room_id: str, text: str) -> None:
    """机器人回复 / 世界事件播报入口（纯文本）。"""
    if not text:
        return
    await broadcast_bot_payload(room_id, {"text": text, "images": []})


async def broadcast_bot_payload(room_id: str, payload: dict) -> None:
    """机器人回复入口（文本 + 图片）。"""
    text = str(payload.get("text", "") or "")
    images = [u for u in (payload.get("images") or []) if u]
    if not text and not images:
        return
    await broadcast(
        room_id,
        {
            "type": "bot",
            "room": room_id,
            "text": text,
            "images": images,
            "ts": int(time.time()),
        },
    )


def _drop(room_id: str, ws: web.WebSocketResponse) -> None:
    conns = _rooms.get(room_id)
    if conns is not None:
        conns.pop(ws, None)
        if not conns:
            _rooms.pop(room_id, None)


async def _media(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    # 只允许纯文件名，防止路径穿越
    if "/" in filename or "\\" in filename or ".." in filename:
        return web.json_response({"error": "非法路径"}, status=400)
    path = MEDIA_DIR / filename
    if not path.is_file():
        return web.json_response({"error": "文件不存在"}, status=404)
    return web.FileResponse(path)


async def _index(_request: web.Request) -> web.Response:    return web.json_response(
        {
            "service": "webroom",
            "rooms": {rid: online_players(rid) for rid in _rooms},
            "usage": "ws://<host>:<port>/ws/<room_id>?player=<玩家名>",
        }
    )


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    room_id = request.match_info["room_id"].strip()
    player_name = request.query.get("player", "").strip()
    if not room_id or not player_name:
        return web.json_response({"error": "缺少 room_id 或 player 参数"}, status=400)

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    # 顶号：同名玩家旧连接踢下线
    conns = _rooms.setdefault(room_id, {})
    for old_ws, info in list(conns.items()):
        if info["player_name"] == player_name:
            try:
                await old_ws.close(code=4001, message=b"replaced by new login")
            except Exception:
                pass
            _drop(room_id, old_ws)
    conns = _rooms.setdefault(room_id, {})
    player_id = f"web_{player_name}"
    conns[ws] = {"player_id": player_id, "player_name": player_name}

    await broadcast(room_id, {"type": "sys", "text": f"{player_name} 进入了房间", "ts": int(time.time())})
    await ws.send_json(
        {"type": "hello", "room": room_id, "player": player_name, "online": online_players(room_id)}
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
        await ws.send_json({"type": "error", "text": "消息格式错误"})
        return

    action = data.get("action")
    if action == "ping":
        await ws.send_json({"type": "pong"})
        return
    if action == "online":
        await ws.send_json({"type": "online", "players": online_players(room_id)})
        return
    if action != "msg":
        await ws.send_json({"type": "error", "text": f"未知 action: {action}"})
        return

    text = str(data.get("text", "")).strip()
    if not text:
        return
    if len(text) > 2000:
        await ws.send_json({"type": "error", "text": "消息过长（>2000 字）"})
        return

    info = _rooms.get(room_id, {}).get(ws)
    if info is None:
        await ws.send_json({"type": "error", "text": "尚未完成连接握手"})
        return

    # 玩家消息先广播给房间（含发送者自己），再进机器人流水线
    payload = {
        "type": "chat",
        "room": room_id,
        "from": info["player_name"],
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
    for client_ws in dead:
        _drop(room_id, client_ws)

    if _adapter is not None:
        try:
            await _adapter.on_player_message(room_id, info["player_id"], info["player_name"], text)
        except Exception:
            logger.exception("[webroom] 提交玩家消息到流水线失败")
    else:
        await ws.send_json({"type": "error", "text": "机器人适配器未就绪"})
