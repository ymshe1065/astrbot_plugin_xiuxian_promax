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
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from . import accounts as accounts_store
from . import commands as commands_catalog
from .arg_options import build_arg_options
from .proxy_panel import proxy_snapshot, toggle_proxy

if TYPE_CHECKING:
    from .webroom_adapter import WebroomAdapter
    from ..main import XiuXianPlugin

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
_plugin: "XiuXianPlugin | None" = None
_runner: web.AppRunner | None = None
_site: web.TCPSite | None = None

# 最近发出的公屏消息：撤回时校验发送者，避免撤回别人的消息。
# 只保存在内存里，重启后历史消息不能再撤回（与聊天记录不落库的策略一致）。
_RECENT_MSG_LIMIT = 800
_recent_msgs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


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
    app.router.add_get("/api/arg_options", _api_arg_options)
    app.router.add_get("/api/equip_context", _api_equip_context)
    app.router.add_get("/api/proxy", _api_proxy)
    app.router.add_post("/api/proxy/toggle", _api_proxy_toggle)
    app.router.add_get("/api/settings", _api_settings)
    app.router.add_post("/api/settings", _api_settings)
    app.router.add_get("/api/notices", _api_notices)
    app.router.add_post("/api/notices/read", _api_notices_read)
    app.router.add_post("/api/notices/clear", _api_notices_clear)
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
    _recent_msgs.clear()
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
    out = {
        "type": "bot",
        "room": room_id,
        "text": text,
        "images": images,
        "cards": cards,
        "ts": int(time.time()),
    }
    # 透传可选呈现字段：消息分类、归属、可折叠详情等由事件层决定
    for key in (
        "options",
        "personal",
        "channel",
        "audience",
        "actor",
        "req_id",
        "title",
        "summary",
        "detail",
        "detail_collapsed",
    ):
        if key in payload:
            out[key] = payload[key]
    await broadcast(room_id, out)


def resolve_audience(
    text: str, settings: dict[str, Any] | None = None, panel: bool = False
) -> str:
    """计算一条手动消息的受众。

    只有看板请求（我的信息/储物戒）或用户主动设为「仅自己」时才私密；
    其余手动指令默认公屏公开。
    """
    if panel:
        return "self"
    conf = settings or {}
    if str(conf.get("manual_visibility", "public")) == "self":
        return "self"
    return "public"


def _summary_of(text: str, limit: int = 120) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return str(text or "")[:limit]


def _find_user_ws(room_id: str, username: str) -> list[web.WebSocketResponse]:
    return [
        ws
        for ws, info in (_rooms.get(room_id) or {}).items()
        if info.get("username") == username
    ]


async def push_to_username(room_id: str, username: str, payload: dict[str, Any]) -> int:
    """推送给某个网页账号的全部在线连接，返回成功数。"""
    sent = 0
    for ws in _find_user_ws(room_id, username):
        if await send_to_ws(ws, payload):
            sent += 1
        else:
            _drop(room_id, ws)
    return sent


async def notify_player(
    room_id: str,
    player_key: str,
    text: str,
    title: str = "",
    category: str = "auto_proxy",
    summary: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
) -> bool:
    """写入并推送一条个人消息。

    - 默认只进个人消息历史，不广播；
    - 用户把 auto_visibility 设为公开摘要/全文时，才额外广播；
    - 找不到网页账号（例如纯 QQ 玩家）返回 False，由调用方回退到旧通道。
    """
    username = accounts_store.username_of_player_key(player_key)
    clean = str(text or "").strip()
    if not username or not clean:
        return False

    summary = str(summary or "").strip() or _summary_of(clean)
    detail = str(detail or "").strip() or clean
    notice_id = accounts_store.add_notice(
        username,
        player_key=player_key,
        channel="auto",
        category=category,
        title=title or "自动代理",
        summary=summary,
        detail=detail,
        payload=payload or {},
    )
    notice = {
        "id": notice_id,
        "channel": "auto",
        "category": category,
        "title": title or "自动代理",
        "summary": summary,
        "detail": detail,
        "payload": payload or {},
        "created": int(time.time()),
        "is_read": False,
    }
    await push_to_username(
        room_id,
        username,
        {
            "type": "notice",
            "notice": notice,
            "unread": accounts_store.count_unread(username),
        },
    )

    settings = accounts_store.get_settings(username)
    visibility = str(settings.get("auto_visibility", "self"))
    if visibility in ("public_summary", "public_full"):
        public_detail = detail if visibility == "public_full" else ""
        await broadcast_bot_payload(
            room_id,
            {
                "text": summary if visibility == "public_summary" else clean,
                "channel": "auto",
                "audience": "public",
                "actor": username,
                "title": title or "自动代理",
                "summary": summary,
                "detail": public_detail,
                "detail_collapsed": visibility != "public_full",
            },
        )
    return True


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


# ---------- 撤回 ----------


def _remember_msg(
    room_id: str, msg_id: str, info: dict[str, str], text: str, ts: int
) -> None:
    """记录最近消息的发送者，供撤回鉴权。"""
    _recent_msgs[msg_id] = {
        "room": room_id,
        "username": info.get("username", ""),
        "nick": info.get("player_name", ""),
        "text": text,
        "ts": ts,
    }
    while len(_recent_msgs) > _RECENT_MSG_LIMIT:
        _recent_msgs.popitem(last=False)


async def _handle_recall(
    room_id: str, ws: web.WebSocketResponse, data: dict[str, Any]
) -> None:
    info = _rooms.get(room_id, {}).get(ws)
    if info is None:
        await send_to_ws(ws, {"type": "recall_fail", "text": "尚未完成连接握手"})
        return

    msg_id = str(data.get("msg_id") or "").strip()[:64]
    record = _recent_msgs.get(msg_id) if msg_id else None
    if not record or record.get("room") != room_id:
        await send_to_ws(
            ws, {"type": "recall_fail", "msg_id": msg_id, "text": "消息已过期，无法撤回"}
        )
        return
    if record.get("username") != info.get("username"):
        await send_to_ws(
            ws, {"type": "recall_fail", "msg_id": msg_id, "text": "只能撤回自己的消息"}
        )
        return

    _recent_msgs.pop(msg_id, None)
    await broadcast(
        room_id,
        {
            "type": "recall",
            "room": room_id,
            "msg_id": msg_id,
            "username": record.get("username", ""),
            "nick": record.get("nick", ""),
            "ts": int(time.time()),
        },
    )


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

    # 绑定了 QQ 的账号实际使用 QQ 号存档，候选必须从同一份存档读取
    user_id = accounts_store.player_key_of(user)
    try:
        data = await build_arg_options(_plugin, user_id, cmd, values)
    except Exception as exc:
        logger.exception("[webroom] 读取参数候选失败")
        return web.json_response({"ok": False, "error": f"候选读取失败：{exc}"}, status=500)
    return web.json_response(data)


async def _api_equip_context(request: web.Request) -> web.Response:
    """物品快捷菜单的装备上下文：是否可装备、槽位占用与需要怎么替换。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    item_name = str(request.query.get("item", "") or "").strip()
    if item_name:
        try:
            from ..core.item_display import strip_item_tag

            item_name = strip_item_tag(item_name)
        except Exception:
            pass
    if not item_name:
        return web.json_response({"ok": False, "error": "缺少物品名称"}, status=400)

    user_id = accounts_store.player_key_of(user)
    try:
        player = await _plugin.db.get_player_by_id(user_id)
    except Exception as exc:
        logger.exception("[webroom] 读取装备上下文失败")
        return web.json_response({"ok": False, "error": f"读取失败：{exc}"}, status=500)
    if player is None:
        return web.json_response({"ok": False, "error": "尚未创建角色，请先 /我要修仙"})

    try:
        from ..core.item_rules import ItemRules

        cm = _plugin.config_manager
        config = (
            cm.items_data.get(item_name)
            or cm.weapons_data.get(item_name)
            or (getattr(cm, "techniques_data", {}) or {}).get(item_name)
        )
        if not isinstance(config, dict):
            return web.json_response(
                {"ok": True, "item": item_name, "equippable": False, "type": "", "needs_choice": False,
                 "choices": [], "already_equipped": False, "hint": "未找到该物品的配置"}
            )

        item_type = ItemRules.normalize_equipment_type(config)
        if item_type not in ItemRules.EQUIPPABLE_TYPES:
            return web.json_response(
                {"ok": True, "item": item_name, "equippable": False, "type": item_type, "needs_choice": False,
                 "choices": [], "already_equipped": False, "hint": "该物品不是可装备类型"}
            )

        result: dict[str, Any] = {
            "ok": True,
            "item": item_name,
            "equippable": True,
            "type": item_type,
            "needs_choice": False,
            "choices": [],
            "already_equipped": False,
            "hint": "",
        }
        manager = _plugin.equipment_handler.equipment_manager
        slots = manager.get_gear_slots(player)

        def _choice(value: str, label: str, desc: str = "") -> dict[str, str]:
            return {"value": value, "label": label, "desc": desc}

        if item_type in ItemRules.GEAR_TYPES:
            if item_type == "ring":
                ring_slots = [
                    ("戒指1", "ring1", slots.get("ring1", ("", None))[0]),
                    ("戒指2", "ring2", slots.get("ring2", ("", None))[0]),
                ]
                same = [label for label, _key, name in ring_slots if name == item_name]
                if same:
                    result["already_equipped"] = True
                    result["hint"] = f"【{item_name}】已装备在{same[0]}"
                    return web.json_response(result)
                occupied = [(label, name) for label, _key, name in ring_slots if name]
                if len(occupied) >= 2:
                    result["needs_choice"] = True
                    result["choices"] = [
                        _choice(label, f"{label}：替换【{name}】", "两枚戒指都已占用")
                        for label, name in occupied
                    ]
                    result["hint"] = "两枚戒指均已装备，请选择要替换的槽位"
                else:
                    free = next(label for label, _key, name in ring_slots if not name)
                    result["hint"] = f"将装备到{free}（有空闲槽位，直接装备）"
                return web.json_response(result)

            slot = ItemRules.SLOT_BY_TYPE.get(item_type, "")
            slot = "armor" if slot == "body" else slot
            current_name = (slots.get(slot, ("", None))[0] if slot else "") or ""
            if slot and current_name == item_name:
                result["already_equipped"] = True
                result["hint"] = f"【{item_name}】已装备在{ItemRules.SLOT_LABELS.get(slot, slot)}"
                return web.json_response(result)
            if current_name:
                slot_label = ItemRules.SLOT_LABELS.get(slot, slot)
                result["needs_choice"] = True
                result["choices"] = [
                    _choice(slot_label, f"{slot_label}：替换【{current_name}】", "该部位已有装备")
                ]
                result["hint"] = f"{slot_label}已装备【{current_name}】，确认后替换"
            else:
                result["hint"] = f"将装备到{ItemRules.SLOT_LABELS.get(slot, slot)}（槽位空闲）"
            return web.json_response(result)

        if item_type == "main_technique":
            current = str(getattr(player, "main_technique", "") or "")
            if current == item_name:
                result["already_equipped"] = True
                result["hint"] = f"【{item_name}】已是当前主修心法"
            elif current:
                result["needs_choice"] = True
                result["choices"] = [
                    _choice("", f"心法：替换【{current}】", "主修心法已有装备，确认后替换")
                ]
                result["hint"] = f"当前主修心法为【{current}】，确认后替换"
            else:
                result["hint"] = "将装备为主修心法"
            return web.json_response(result)

        if item_type == "technique":
            techniques = [str(name) for name in player.get_techniques_list() if str(name)]
            if item_name in techniques:
                result["already_equipped"] = True
                result["hint"] = f"【{item_name}】已装备（栏位 {techniques.index(item_name) + 1}）"
                return web.json_response(result)
            if len(techniques) >= 3:
                result["needs_choice"] = True
                result["choices"] = [
                    _choice(str(index), f"栏位 {index}：替换【{name}】", "功法栏已满")
                    for index, name in enumerate(techniques[:3], 1)
                ]
                result["hint"] = "功法栏已满（3/3），请选择要替换的栏位"
            else:
                result["hint"] = f"将装备到空余栏位（当前 {len(techniques)}/3）"
            return web.json_response(result)
    except Exception as exc:
        logger.exception("[webroom] 解析装备上下文失败")
        return web.json_response({"ok": False, "error": f"解析失败：{exc}"}, status=500)

    return web.json_response(result)


async def _api_proxy(request: web.Request) -> web.Response:
    """自动代理面板：读取五项代理的布尔状态与详情。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    user_id = accounts_store.player_key_of(user)
    try:
        data = await proxy_snapshot(_plugin, user_id)
    except Exception as exc:
        logger.exception("[webroom] 读取自动代理状态失败")
        return web.json_response({"ok": False, "error": f"读取失败：{exc}"}, status=500)
    return web.json_response(data)


async def _api_proxy_toggle(request: web.Request) -> web.Response:
    """切换单项代理；task=all 时一键开启/关闭全部。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    body = await _read_json(request)
    if not body:
        return web.json_response({"ok": False, "error": "请求格式不对"}, status=400)

    task = str(body.get("task") or "").strip()
    enabled = bool(body.get("enabled"))
    try:
        rift_id = int(body.get("rift_id") or 0)
    except (TypeError, ValueError):
        rift_id = 0

    user_id = accounts_store.player_key_of(user)
    origin = f"webroom:GroupMessage:{ROOM_ID}"
    try:
        data = await toggle_proxy(
            _plugin,
            user_id,
            task,
            enabled,
            rift_id=rift_id,
            origin=origin,
        )
    except Exception as exc:
        logger.exception("[webroom] 切换自动代理失败")
        return web.json_response({"ok": False, "error": f"操作失败：{exc}"}, status=500)

    if data.get("ok") is False:
        return web.json_response(data, status=400)
    return web.json_response(data)


async def _api_settings(request: web.Request) -> web.Response:
    """读取/保存网页端消息可见性设置。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if request.method == "GET":
        return web.json_response(
            {"ok": True, "settings": accounts_store.get_settings(user["username"])}
        )
    body = await _read_json(request)
    ok, msg, settings = accounts_store.set_settings(user["username"], body)
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    return web.json_response({"ok": True, "message": msg, "settings": settings})


async def _api_notices(request: web.Request) -> web.Response:
    """分页拉取个人消息历史与未读数。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    try:
        before = int(request.query.get("before") or 0)
        limit = int(request.query.get("limit") or 50)
    except (TypeError, ValueError):
        before, limit = 0, 50
    items, unread = accounts_store.list_notices(
        user["username"],
        limit=limit,
        before=before,
        unread_only=request.query.get("unread_only") in ("1", "true", "yes"),
    )
    return web.json_response({"ok": True, "items": items, "unread": unread})


async def _api_notices_read(request: web.Request) -> web.Response:
    """标记个人消息已读；body 传 {all:true} 或 {ids:[...]}。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    body = await _read_json(request)
    ids = body.get("ids") if isinstance(body.get("ids"), list) else []
    changed = accounts_store.mark_notices_read(
        user["username"], ids=ids, all_read=bool(body.get("all"))
    )
    items, unread = accounts_store.list_notices(user["username"])
    return web.json_response(
        {"ok": True, "changed": changed, "items": items, "unread": unread}
    )


async def _api_notices_clear(request: web.Request) -> web.Response:
    """清空已读的个人消息。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    removed = accounts_store.clear_read_notices(user["username"])
    items, unread = accounts_store.list_notices(user["username"])
    return web.json_response(
        {"ok": True, "removed": removed, "items": items, "unread": unread}
    )


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
            "settings": accounts_store.get_settings(user["username"]),
            "unread": accounts_store.count_unread(user["username"]),
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
    if action == "recall":
        await _handle_recall(room_id, ws, data)
        return
    if action in ("notices", "notices_read", "notices_clear"):
        info = _rooms.get(room_id, {}).get(ws)
        if info is None:
            await send_to_ws(ws, {"type": "error", "text": "尚未完成连接握手"})
            return
        username = str(info.get("username") or "")
        if action == "notices":
            items, unread = accounts_store.list_notices(
                username,
                limit=int(data.get("limit") or 50),
                before=int(data.get("before") or 0),
                unread_only=bool(data.get("unread_only")),
            )
            await send_to_ws(
                ws, {"type": "notices", "items": items, "unread": unread}
            )
        elif action == "notices_read":
            ids = data.get("ids") if isinstance(data.get("ids"), list) else []
            accounts_store.mark_notices_read(
                username, ids=ids, all_read=bool(data.get("all"))
            )
            await send_to_ws(
                ws,
                {
                    "type": "notices",
                    "items": accounts_store.list_notices(username)[0],
                    "unread": accounts_store.count_unread(username),
                },
            )
        else:
            accounts_store.clear_read_notices(username)
            await send_to_ws(
                ws,
                {
                    "type": "notices",
                    "items": accounts_store.list_notices(username)[0],
                    "unread": accounts_store.count_unread(username),
                },
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

    req_id = str(data.get("req_id") or "").strip()[:64]
    # 看板请求：前端要原生渲染，后端优先返回结构化卡片
    want_card = bool(data.get("card"))
    panel = bool(data.get("panel"))
    settings = accounts_store.get_settings(str(info.get("username") or ""))

    if action == "cmd":
        audience = resolve_audience(text, settings, panel=panel)
    else:
        audience = "self" if bool(data.get("private")) else "public"
    private = audience == "self"

    if panel:
        # 看板请求不生成聊天回显，结果只进看板
        pass
    elif private:
        # 仅自己可见的指令：回显只发给发送者本人
        await send_to_ws(
            ws,
            {
                "type": "chat",
                "room": room_id,
                "msg_id": uuid.uuid4().hex[:16],
                "from": info["player_name"],
                "username": info.get("username", ""),
                "avatar": info.get("avatar", ""),
                "text": text,
                "is_cmd": action == "cmd",
                "audience": audience,
                "ts": int(time.time()),
            },
        )
    else:
        # 公屏消息：广播给房间（含发送者自己），附带消息 id 供撤回
        ts = int(time.time())
        msg_id = uuid.uuid4().hex[:16]
        _remember_msg(room_id, msg_id, info, text, ts)
        payload = {
            "type": "chat",
            "room": room_id,
            "msg_id": msg_id,
            "from": info["player_name"],
            "username": info.get("username", ""),
            "avatar": info.get("avatar", ""),
            "text": text,
            "is_cmd": action == "cmd",
            "audience": audience,
            "ts": ts,
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
            audience=audience,
            panel=panel,
            username=str(info.get("username") or ""),
        )
    except Exception:
        logger.exception("[webroom] 提交玩家消息到流水线失败")
        if private:
            await send_to_ws(
                ws,
                {"type": "reply", "req_id": req_id, "text": "提交失败，请稍后再试", "error": True},
            )
