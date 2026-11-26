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
from collections import OrderedDict, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from . import accounts as accounts_store
from . import channels as channels_store
from . import commands as commands_catalog
from . import dm_store
from . import stickers as stickers_catalog
from .arg_options import build_arg_options
from .proxy_panel import proxy_snapshot, toggle_proxy
from .fate_shop_panel import fate_shop_buy, fate_shop_exchange, fate_shop_snapshot
from .strategy_panel import (
    StrategyBusyError,
    StrategyError,
    strategy_action,
    strategy_logs,
    strategy_state,
)
from .invest_agent_panel import (
    invest_agent_claim,
    invest_agent_snapshot,
    invest_agent_start,
    invest_agent_stop,
)
from .visibility import is_panel_command

if TYPE_CHECKING:
    from .webroom_adapter import WebroomAdapter
    from ..main import XiuXianPlugin

MEDIA_DIR = Path(get_astrbot_data_path()) / "webroom" / "imgs"
AVATAR_DIR = Path(get_astrbot_data_path()) / "webroom" / "avatars"
STATIC_DIR = Path(__file__).parent / "static"

# 主频道「原始仙界」：注册/登录后的默认落点，也是旧版单房间的兼容 id
ROOM_ID = channels_store.MAIN_CHANNEL_ID

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

# 储物戒指纹：player_key -> 最近一次已同步给前端的储物戒状态哈希。
# 指令执行后比较一次，只有真的变了才推送 bag_changed，避免每条消息都重渲染卡片。
_BAG_SIG_LIMIT = 2000
_bag_sigs: "OrderedDict[str, str]" = OrderedDict()
# 丹药背包指纹：与储物戒分开算——服用丹药不动储物戒，储物戒增减也不代表丹药变了。
# 两边各推各的事件，前端只重画对应那一页。
_PILL_SIG_LIMIT = 2000
_pill_sigs: "OrderedDict[str, str]" = OrderedDict()
_bag_tasks: set[asyncio.Task] = set()

# 最近发出的公屏消息：撤回时校验发送者，避免撤回别人的消息。
# 只保存在内存里，重启后历史消息不能再撤回（与聊天记录不落库的策略一致）。
_RECENT_MSG_LIMIT = 800
_recent_msgs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

# 频道最近消息：切换频道时回放，避免切过去只有空白。
# 同样只在内存里，不落库；私聊/看板回复不进入历史。
_CHANNEL_HISTORY_LIMIT = 60
_channel_history: dict[str, deque[dict[str, Any]]] = {}


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
    info = channels_store.get(room_id) if room_id else None
    if info and info.get("name"):
        return str(info["name"])
    return "修仙聊天室"


def _remember_history(room_id: str, payload: dict[str, Any]) -> None:
    """记录频道最近消息，供切换/重连时回放。"""
    if not room_id or not payload:
        return
    box = _channel_history.setdefault(
        room_id, deque(maxlen=_CHANNEL_HISTORY_LIMIT)
    )
    box.append(payload)


def _history_for(room_id: str) -> list[dict[str, Any]]:
    return list(_channel_history.get(room_id) or ())


def _history_drop(room_id: str, msg_id: str) -> None:
    box = _channel_history.get(room_id)
    if not box or not msg_id:
        return
    kept = [item for item in box if item.get("msg_id") != msg_id]
    box.clear()
    box.extend(kept)


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


def room_members(room_id: str) -> list[dict[str, Any]]:
    """房间成员名单：进过这个房间的账号，带在线/离线标记。

    主频道显示所有进过聊天室的账号；私人仙界只显示已加入该仙界的成员，
    避免把其他仙界的人列进来。在线的人排前面，同段内按最近进过房间的时间倒序。
    """
    conns = _rooms.get(room_id) or {}
    live: dict[str, dict[str, Any]] = {}
    for info in conns.values():
        username = str(info.get("username") or "")
        if username and username not in live:
            live[username] = info

    # 私人仙界按成员表过滤；主频道保持「所有来过的人」
    if room_id == ROOM_ID:
        base = accounts_store.list_room_members()
    else:
        base = channels_store.member_profiles(room_id)

    members: list[dict[str, Any]] = []
    listed: set[str] = set()
    for m in base:
        username = m["username"]
        listed.add(username)
        info = live.get(username) or {}
        members.append(
            {
                "username": username,
                "nick": info.get("player_name") or m["nick"] or username,
                "avatar": info.get("avatar") or m["avatar"] or "",
                "online": bool(info),
                "seen": int(m.get("seen") or 0),
            }
        )
    # 在线但成员表里还没有的（老库刚补列、或写库失败），也要出现在名单里
    for username, info in live.items():
        if username in listed:
            continue
        listed.add(username)
        members.append(
            {
                "username": username,
                "nick": info.get("player_name") or username,
                "avatar": info.get("avatar") or "",
                "online": True,
                "seen": 0,
            }
        )
    members.sort(key=lambda p: (0 if p["online"] else 1, -int(p.get("seen") or 0)))
    return members


async def broadcast_presence(room_id: str) -> None:
    """成员名单变化时推给房间所有人，前端据此刷新列表和计数。"""
    await broadcast(
        room_id,
        {
            "type": "presence",
            "room": room_id,
            "players": room_members(room_id),
            "ts": int(time.time()),
        },
    )


# ---------- 生命周期 ----------


@web.middleware
async def _cache_control(request: web.Request, handler):
    """静态资源走协商缓存：ETag 命中返回 304，改动后立刻拉新的。

    不设 Cache-Control 时浏览器会启发式强缓存，改完代码用户刷新也看不到变化。
    页面、脚本与样式额外使用 no-store，避免旧 HTML/JS 组合导致新功能「看起来没生效」。
    """
    resp = await handler(request)
    if request.path.startswith("/static/"):
        if request.path.endswith((".html", ".js", ".css")):
            resp.headers["Cache-Control"] = "no-store, max-age=0"
        else:
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif request.path.startswith("/api/"):
        # 接口结果跟当前存档强相关（如 /api/equip_context 的槽位占用），
        # 被浏览器/WebView 缓存会让替换弹窗看起来「没生效」。
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


async def start(host: str, port: int) -> None:
    global _runner, _site
    if _site is not None:
        return
    # client_max_size 放宽到 4MB：头像以 base64 上传，默认 1MB 不够
    app = web.Application(middlewares=[_cache_control], client_max_size=4 * 1024 * 1024)
    accounts_store.init()
    channels_store.init()
    dm_store.init()
    app.router.add_get("/", _index)
    app.router.add_get("/api/commands", _api_commands)
    app.router.add_get("/api/stickers", _api_stickers)
    app.router.add_get("/api/arg_options", _api_arg_options)
    app.router.add_get("/api/equip_context", _api_equip_context)
    app.router.add_get("/api/proxy", _api_proxy)
    app.router.add_get("/api/market/candles", _api_market_candles)
    app.router.add_get("/api/invest/agents", _api_invest_agents)
    app.router.add_post("/api/invest/agents/start", _api_invest_agents_start)
    app.router.add_post("/api/invest/agents/stop", _api_invest_agents_stop)
    app.router.add_post("/api/invest/agents/claim", _api_invest_agents_claim)
    app.router.add_post("/api/proxy/toggle", _api_proxy_toggle)
    app.router.add_get("/api/fate_shop", _api_fate_shop)
    app.router.add_post("/api/fate_shop/buy", _api_fate_shop_buy)
    app.router.add_post("/api/fate_shop/exchange", _api_fate_shop_exchange)
    app.router.add_get("/api/strategy/state", _api_strategy_state)
    app.router.add_get("/api/strategy/logs", _api_strategy_logs)
    app.router.add_post("/api/strategy/action", _api_strategy_action)
    app.router.add_get("/api/strategy/memorial", _api_strategy_memorial)
    app.router.add_get("/api/strategy/guide", _api_strategy_guide)
    app.router.add_get("/api/settings", _api_settings)
    app.router.add_post("/api/settings", _api_settings)
    app.router.add_get("/api/notices", _api_notices)
    app.router.add_post("/api/notices/read", _api_notices_read)
    app.router.add_post("/api/notices/clear", _api_notices_clear)
    app.router.add_get("/api/feedback", _api_feedback)
    app.router.add_post("/api/feedback", _api_feedback)
    app.router.add_post("/api/feedback/status", _api_feedback_status)
    app.router.add_get("/api/health", _api_health)
    app.router.add_get("/api/server_time", _api_server_time)
    app.router.add_get("/api/app/version", _api_app_version)
    app.router.add_get("/download/{name}", _api_apk)
    app.router.add_post("/api/register", _api_register)
    app.router.add_post("/api/login", _api_login)
    app.router.add_get("/api/me", _api_me)
    app.router.add_post("/api/nick", _api_nick)
    app.router.add_post("/api/password", _api_password)
    app.router.add_post("/api/avatar", _api_avatar)
    app.router.add_get("/api/bind/status", _api_bind_status)
    app.router.add_post("/api/bind", _api_bind)
    app.router.add_post("/api/bind/cancel", _api_bind_cancel)
    app.router.add_post("/api/bind/unbind", _api_bind_unbind)
    app.router.add_get("/api/channels", _api_channels)
    app.router.add_post("/api/channels/create", _api_channels_create)
    app.router.add_post("/api/channels/join", _api_channels_join)
    app.router.add_post("/api/channels/update", _api_channels_update)
    app.router.add_get("/api/dm/conversations", _api_dm_conversations)
    app.router.add_get("/api/dm/history", _api_dm_history)
    app.router.add_get("/api/dm/blocks", _api_dm_blocks)
    app.router.add_post("/api/dm/send", _api_dm_send)
    app.router.add_post("/api/dm/read", _api_dm_read)
    app.router.add_post("/api/dm/block", _api_dm_block)
    app.router.add_post("/api/dm/unblock", _api_dm_unblock)
    app.router.add_get("/ws", _ws_handler)
    # 旧链接 /ws/{room} 仍然可用，房间号已忽略
    app.router.add_get("/ws/{room_id}", _ws_handler)
    app.router.add_get("/media/{filename}", _media)
    app.router.add_get("/avatar/{filename}", _avatar_file)
    app.router.add_get("/cards/{filename}", _card_image)
    if STATIC_DIR.is_dir():
        app.router.add_static("/static/", STATIC_DIR, name="static")

    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
    except BaseException:
        # 端口冲突/启动取消时释放 runner，不留下阻止下次启动的伪运行状态。
        await runner.cleanup()
        raise
    _runner, _site = runner, site
    # 私聊历史保留 30 天（§12.7）：启动即清一次，之后每 24h 定期清理
    try:
        removed = dm_store.prune()
        if removed:
            logger.info(f"[webroom] 已清理 {removed} 条过期私聊消息")
    except Exception:
        logger.debug("[webroom] 启动私聊清理失败", exc_info=True)

    async def _dm_prune_loop() -> None:
        while True:
            await asyncio.sleep(86400)
            try:
                removed = dm_store.prune()
                if removed:
                    logger.info(f"[webroom] 已清理 {removed} 条过期私聊消息")
            except Exception:
                logger.debug("[webroom] 私聊定期清理失败", exc_info=True)

    try:
        prune_task = asyncio.create_task(_dm_prune_loop())
        _bag_tasks.add(prune_task)
        prune_task.add_done_callback(_bag_tasks.discard)
    except RuntimeError:
        pass
    # 仙域争霸：世界 tick 推进 → strategy.delta（不侵入 manager 的 tick 管线）
    try:
        delta_task = asyncio.create_task(_strategy_delta_loop())
        _bag_tasks.add(delta_task)
        delta_task.add_done_callback(_bag_tasks.discard)
    except RuntimeError:
        pass
    logger.info(f"[webroom] 房间服务已启动: http://{host}:{port}/")


async def stop() -> None:
    global _runner, _site, _strategy_last_tick, _strategy_last_world
    if _site is not None:
        await _site.stop()
    # close 会触发握手协程的 finally 并删除房间，必须先拍下所有连接。
    sockets = [ws for conns in _rooms.values() for ws in conns]
    if sockets:
        await asyncio.gather(
            *(ws.close(code=1001, message=b"server shutdown") for ws in sockets),
            return_exceptions=True,
        )
    tasks = list(_bag_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _bag_tasks.clear()
    _bag_sigs.clear()
    _pill_sigs.clear()
    _dm_rate.clear()
    _rooms.clear()
    _recent_msgs.clear()
    _channel_history.clear()
    _strategy_subs.clear()
    _strategy_last_tick = 0
    _strategy_last_world = 0
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


def _find_ws_anywhere(room_id: str, player_id: str) -> tuple[str, web.WebSocketResponse] | None:
    """优先在当前频道找，找不到再跨频道找。

    玩家在等待看板回复时可能已经切到别的仙界，私密回复不应因此丢失。
    """
    ws = _find_ws(room_id, player_id)
    if ws is not None:
        return room_id, ws
    for rid, conns in _rooms.items():
        for other_ws, info in conns.items():
            if info.get("player_id") == player_id:
                return rid, other_ws
    return None


async def send_to_player(room_id: str, player_id: str, payload: dict[str, Any]) -> bool:
    """定向发送给某个玩家（看板/私聊回复）。玩家已离线则返回 False。"""
    found = _find_ws_anywhere(room_id, player_id)
    if found is None:
        return False
    rid, ws = found
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        _drop(rid, ws)
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
    _remember_history(room_id, out)
    await broadcast(room_id, out)


def resolve_audience(
    text: str, settings: dict[str, Any] | None = None, panel: bool = False
) -> str:
    """计算一条手动消息的受众。

    看板指令（我的信息/储物戒）固定私密：既看前端 panel 标记，也按指令名兜底，
    以兼容旧版前端和玩家手动输入。用户主动设为「仅自己」时同样私密；
    其余手动指令默认公屏公开。
    """
    if panel:
        return "self"
    if is_panel_command(text):
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


def _find_user_ws(room_id: str, username: str) -> list[tuple[str, web.WebSocketResponse]]:
    """返回 (频道, 连接) 列表；room_id 为空时搜索全部频道。"""
    rooms = _rooms if not room_id else {room_id: _rooms.get(room_id) or {}}
    found: list[tuple[str, web.WebSocketResponse]] = []
    for rid, conns in rooms.items():
        for ws, info in conns.items():
            if info.get("username") == username:
                found.append((rid, ws))
    return found


async def push_to_username(room_id: str, username: str, payload: dict[str, Any]) -> int:
    """推送给某个网页账号的全部在线连接，返回成功数。

    优先在指定频道推送；找不到时跨频道兜底，保证切换仙界后个人消息不丢。
    """
    targets = _find_user_ws(room_id, username)
    if not targets:
        targets = _find_user_ws("", username)
    sent = 0
    for rid, ws in targets:
        if await send_to_ws(ws, payload):
            sent += 1
        else:
            _drop(rid, ws)
    return sent


async def _bag_check(room_id: str, player_key: str, username: str) -> None:
    """比对储物戒与丹药背包指纹，变化时通知该玩家的所有网页连接刷新看板。

    两块数据各有各的存储（物品栏 vs 丹药背包），所以分别算指纹、分别推送：
    吃一颗丹药只会推 pillbag_changed，不会连带重画储物戒。
    """
    plugin = _plugin
    if plugin is None or not player_key or not username:
        return
    db = getattr(plugin, "db", None)
    if db is None:
        return
    try:
        player = await db.get_player_by_id(player_key)
    except Exception:
        return
    if player is None:
        return
    sig = _storage_signature(player)
    old = _bag_sigs.get(player_key)
    _bag_sigs[player_key] = sig
    _bag_sigs.move_to_end(player_key)
    while len(_bag_sigs) > _BAG_SIG_LIMIT:
        _bag_sigs.popitem(last=False)
    if old is not None and old != sig:
        await push_to_username(
            room_id, username, {"type": "bag_changed", "ts": int(time.time())}
        )

    pill_sig = _pill_signature(player)
    pill_old = _pill_sigs.get(player_key)
    _pill_sigs[player_key] = pill_sig
    _pill_sigs.move_to_end(player_key)
    while len(_pill_sigs) > _PILL_SIG_LIMIT:
        _pill_sigs.popitem(last=False)
    if pill_old is not None and pill_old != pill_sig:
        await push_to_username(
            room_id, username, {"type": "pillbag_changed", "ts": int(time.time())}
        )


def schedule_bag_check(
    room_id: str, player_key: str, username: str, delay: float = 0.2
) -> None:
    """异步补一次储物戒检查。

    指令回复往往在处理器写库前就已 yield，直接同步比对可能拿到中间态；
    延迟一小会儿再查，既能拿到最终存档，也不会拖慢聊天回复。
    """
    if _plugin is None or not player_key or not username:
        return

    async def _run() -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await _bag_check(room_id, player_key, username)
        except Exception:
            logger.debug("[webroom] 储物戒变更检查失败", exc_info=True)

    try:
        task = asyncio.create_task(_run())
    except RuntimeError:
        return
    _bag_tasks.add(task)
    task.add_done_callback(_bag_tasks.discard)


def _storage_signature(player: Any) -> str:
    """储物戒展示状态的轻量指纹：物品、装备实例、容量与已装备标记。"""

    def _counts(getter: Any) -> dict[str, int]:
        try:
            data = getter() or {}
        except Exception:
            return {}
        out: dict[str, int] = {}
        for name, value in data.items():
            try:
                amount = int(value or 0)
            except (TypeError, ValueError):
                amount = 0
            if amount > 0:
                out[str(name)] = amount
        return out

    instances: list[list[Any]] = []
    try:
        for inst in player.get_equipment_instances() or []:
            if str(inst.get("location") or "") not in ("storage", "overflow"):
                continue
            instances.append(
                [
                    int(inst.get("id") or 0),
                    str(inst.get("name") or ""),
                    str(inst.get("quality") or ""),
                    1 if inst.get("affixes") else 0,
                    str(inst.get("location") or ""),
                ]
            )
    except Exception:
        instances = []
    instances.sort()

    enchant = 0
    try:
        bonus = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
        enchant = int(bonus.get("storage_enchant", 0) or 0)
    except Exception:
        enchant = 0

    payload = {
        "ring": str(getattr(player, "storage_ring", "") or ""),
        "enchant": enchant,
        "items": _counts(player.get_storage_ring_items),
        "overflow": _counts(player.get_overflow_items),
        "instances": instances,
        "techniques": list(player.get_techniques_list() or []),
        "main": str(getattr(player, "main_technique", "") or ""),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _pill_signature(player: Any) -> str:
    """丹药背包展示状态的轻量指纹：丹药名|品质 → 数量。

    丹药存在 get_pills_inventory() 里，与储物戒的物品栏是两套存储，
    所以必须单独算一份——否则吃了丹药储物戒指纹不变，看板就不会刷新。
    """
    try:
        data = player.get_pills_inventory() or {}
    except Exception:
        return ""
    out: dict[str, int] = {}
    for key, value in data.items():
        try:
            amount = int(value or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount > 0:
            out[str(key)] = amount
    raw = json.dumps(out, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


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
    # 自动代理等后台动作也可能往储物戒里塞东西：顺手比对一次，界面开着就自动刷新
    schedule_bag_check(room_id, player_key, username, delay=0.4)

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


async def notify_strategy(player_key: str, kind: str,
                          payload: dict[str, Any] | None = None) -> int:
    """仙域争霸 WS 通知（§12.5 strategy.notify）：手操窗口/己方被攻/单挑邀请。

    只推给该玩家的在线网页连接（跨频道兜底）；纯 QQ 玩家返回 0。
    通知分级过滤在 StrategyManager._notify 内完成，这里只管投递。
    """
    username = accounts_store.username_of_player_key(player_key)
    if not username:
        return 0
    return await push_to_username("", username, {
        "type": "strategy.notify",
        "kind": str(kind or ""),
        "payload": payload or {},
        "ts": int(time.time()),
    })


# ---------- 仙域争霸 WS 协议补全（§12.5 / v4.1.0） ----------
#
# 现有 strategy.notify（按玩家定向、经 manager._notify 分级过滤）保持不变；
# 这里补三条通道：
#   strategy.delta       世界 tick 推进后推给「订阅了策略面板」的连接，前端收到即刷新；
#   strategy.battle.event手操回合推进后推给该战役的参与者（含 REST 与 WS 两条入口）；
#   strategy.<上行指令>   客户端手操指令转调 manager.handle_action，回 strategy.battle.ack。
# REST 轮询照旧可用，WS 只是更低延迟的增量通道。

# 订阅表：最近拉过快照/提交过争霸动作的网页账号 → 最近活跃时间。
# 只给这些人推 delta，避免每 tick 打扰纯聊天用户；6 小时不活跃自动过期。
_STRATEGY_SUB_TTL = 6 * 3600
_STRATEGY_SUB_LIMIT = 2000
_STRATEGY_DELTA_POLL = 10.0
_strategy_subs: dict[str, int] = {}
_strategy_last_tick = 0
_strategy_last_world = 0

# 可经 WS 上行的手操指令 → (action, act)
_STRATEGY_WS_KINDS = ("deploy", "move", "skill", "duel", "retreat", "end_turn")


def subscribe_strategy(username: str) -> None:
    """登记策略面板订阅（拉快照/提交动作/发 WS 手操指令时调用）。"""
    name = str(username or "").strip()
    if not name:
        return
    now = int(time.time())
    _strategy_subs[name] = now
    if len(_strategy_subs) > _STRATEGY_SUB_LIMIT:
        dead = [u for u, ts in _strategy_subs.items()
                if now - int(ts or 0) > _STRATEGY_SUB_TTL]
        for u in dead:
            _strategy_subs.pop(u, None)
        while len(_strategy_subs) > _STRATEGY_SUB_LIMIT:
            _strategy_subs.pop(next(iter(_strategy_subs)), None)


async def notify_strategy_delta(tick: int, ts: int | None = None) -> int:
    """世界 tick 推进广播（strategy.delta）：只推给策略面板订阅者。"""
    try:
        tick_no = int(tick or 0)
    except (TypeError, ValueError):
        return 0
    if tick_no <= 0:
        return 0
    now = int(time.time())
    payload = {"type": "strategy.delta", "tick": tick_no,
               "ts": int(ts or now)}
    sent = 0
    for username, seen in list(_strategy_subs.items()):
        if now - int(seen or 0) > _STRATEGY_SUB_TTL:
            _strategy_subs.pop(username, None)
            continue
        sent += await push_to_username("", username, payload)
    return sent


async def _maybe_strategy_delta(tick: int, world_id: int = 0) -> int:
    """tick 号变化时才广播，避免轮询把同一个 tick 重复推给前端。

    赛季重置会新建世界、tick_no 从 0 重新计数；若只比较 tick 号会因
    ``tick_no <= _strategy_last_tick`` 而整季静默。这里同时记录 world_id，
    世界变了就重置基线。
    """
    global _strategy_last_tick, _strategy_last_world
    try:
        tick_no = int(tick or 0)
        wid = int(world_id or 0)
    except (TypeError, ValueError):
        return 0
    if wid and wid != _strategy_last_world:
        _strategy_last_world = wid
        _strategy_last_tick = 0
    if _strategy_last_tick <= 0:
        # 首次只对齐基线，不补推历史 tick
        _strategy_last_tick = tick_no
        return 0
    if tick_no <= _strategy_last_tick:
        return 0
    _strategy_last_tick = tick_no
    return await notify_strategy_delta(tick_no)


async def _strategy_delta_loop() -> None:
    """后台对账：每 10s 看一次世界 tick，有推进就给订阅者推 strategy.delta。

    tick 调度在 main.py（manager 侧无广播钩子），这里用一次轻量读取代替代
    回调，既不侵入 manager，也能在补跑多个 tick 后只推最终 tick 号。
    """
    while True:
        await asyncio.sleep(_STRATEGY_DELTA_POLL)
        plugin = _plugin
        mgr = getattr(plugin, "strategy_mgr", None) if plugin is not None else None
        if mgr is None:
            continue
        try:
            if callable(getattr(mgr, "enabled", None)) and not mgr.enabled():
                continue
            world = await mgr.get_world()
            if world:
                await _maybe_strategy_delta(int(world.get("tick_no") or 0),
                                            int(world.get("id") or 0))
        except Exception:
            logger.debug("[webroom] strategy.delta 对账失败", exc_info=True)


async def _strategy_battle_owners(battle_id: int) -> list[str]:
    """取手操战役参与者（player_key 列表），用于 battle.event 定向推送。"""
    plugin = _plugin
    mgr = getattr(plugin, "strategy_mgr", None) if plugin is not None else None
    if mgr is None:
        return []
    try:
        battle = await mgr.get_battle(int(battle_id))
    except Exception:
        return []
    owners = ((battle or {}).get("session") or {}).get("owners") or {}
    initiator = str(((battle or {}).get("session") or {}).get("initiator") or "")
    out: list[str] = []
    seen: set[str] = set()
    # 发起人可能没有自有军团（代势力介入），不在 owners 里；漏掉会让其
    # 收不到回合推进/战报 WS 推送，只能靠 3s 轮询。与 _battle_humans 同口径。
    if initiator:
        seen.add(initiator)
        out.append(initiator)
    for v in owners.values():
        uid = str((v or {}).get("owner") or "")
        if uid and uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


async def _mask_battle_current_actor(battle_id: int, viewer_uid: str,
                                     current_actor) -> Any:
    """按 viewer 遮罩 current_actor：敌方未揭示伏兵不得通过 WS 泄露 uid。"""
    if current_actor is None:
        return None
    plugin = _plugin
    mgr = getattr(plugin, "strategy_mgr", None) if plugin is not None else None
    if mgr is None:
        return current_actor
    try:
        battle = await mgr.get_battle(int(battle_id))
    except Exception:
        return None
    sess = (battle or {}).get("session") or {}
    owners = sess.get("owners") or {}
    try:
        cur_uid = int(current_actor)
    except (TypeError, ValueError):
        return current_actor
    own = (owners.get(str(cur_uid)) or {}).get("owner", "")
    if own == viewer_uid:
        return cur_uid
    # 与 battle_state 同口径：未揭示敌方伏兵对对手隐藏 current_actor
    for amb in (sess.get("ambushes") or []):
        if int(amb.get("uid", 0) or 0) == cur_uid and not amb.get("revealed"):
            return None
    return cur_uid


async def notify_strategy_battle(battle_id: int, result: dict | None = None,
                                 user_ids: list[str] | None = None) -> int:
    """手操回合推进广播（strategy.battle.event）。

    REST 与 WS 两条入口都会调用：同一回合所有参与者（含只挂 REST 轮询的
    对手）都能立刻看到新日志，观战者为只读不在此列。
    """
    try:
        bid = int(battle_id or 0)
    except (TypeError, ValueError):
        return 0
    if bid <= 0:
        return 0
    res = result if isinstance(result, dict) else {}
    # 非回合推进类动作（auto_deploy/delegate/set_ambush/set_formation/start）
    # 没有 round/events/result：不推空事件，否则前端战场视图会被清屏。
    if res.get("round") is None and not res.get("events") and not res.get("result"):
        return 0
    ids = list(user_ids) if user_ids else await _strategy_battle_owners(bid)
    raw_actor = res.get("current_actor")
    sent = 0
    for uid in ids:
        username = accounts_store.username_of_player_key(uid)
        if not username:
            continue
        actor = await _mask_battle_current_actor(bid, uid, raw_actor)
        payload = {
            "type": "strategy.battle.event",
            "battle_id": bid,
            "round": res.get("round"),
            "log": list(res.get("events") or [])[-8:],
            "act": str(res.get("action") or ""),
            "current_actor": actor,
            "finished": bool(res.get("result")),
            "ts": int(time.time()),
        }
        sent += await push_to_username("", username, payload)
    return sent


async def _handle_strategy_ws(room_id: str, ws: web.WebSocketResponse,
                              action: str, data: dict[str, Any]) -> None:
    """WS 上行手操指令 → manager.handle_action(...)，回 strategy.battle.ack。

    鉴权沿用连接握手时的网页账号（player_id 即存档 key），不新开旁路；
    限速/世界锁/审计仍在 manager.handle_action 内统一执行。
    """
    info = _rooms.get(room_id, {}).get(ws) or {}
    username = str(info.get("username") or "")
    req = str(data.get("req_id") or data.get("req") or "").strip()[:64]
    if not username:
        await send_to_ws(ws, {"type": "strategy.error", "req": req,
                              "msg": "尚未完成连接握手"})
        return
    plugin = _plugin
    mgr = getattr(plugin, "strategy_mgr", None) if plugin is not None else None
    if mgr is None:
        await send_to_ws(ws, {"type": "strategy.error", "req": req,
                              "msg": "插件尚未就绪"})
        return

    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    try:
        battle_id = int(data.get("battle_id") or payload.get("battle_id") or 0)
    except (TypeError, ValueError):
        await send_to_ws(ws, {"type": "strategy.error", "req": req,
                              "msg": "battle_id 无效"})
        return
    user_id = str(info.get("player_id") or "") or \
        accounts_store.player_key_of({"username": username})
    subscribe_strategy(username)

    kind = action[len("strategy."):]
    if kind == "subscribe":
        # 显式订阅策略通道（前端可选用；REST 拉快照/动作也会自动订阅）
        await send_to_ws(ws, {"type": "strategy.subscribed",
                              "req": req,
                              "tick": int(_strategy_last_tick or 0),
                              "ts": int(time.time())})
        return
    if kind not in _STRATEGY_WS_KINDS:
        await send_to_ws(ws, {"type": "strategy.error", "req": req,
                              "msg": f"未知指令：{action[:32]}"})
        return

    try:
        if kind == "deploy":
            act = "deploy"
            result = await mgr.handle_action(user_id, "battle_deploy", {
                "battle_id": battle_id,
                "placements": payload.get("placements") or [],
            })
        elif kind == "duel":
            # 有 accept 视为应战/拒绝，否则视为发起单挑
            act = "duel_respond" if ("accept" in payload or payload.get("respond")) \
                else "duel_challenge"
            result = await mgr.handle_action(user_id, "battle_action", {
                "battle_id": battle_id, "act": act, "params": payload})
        else:
            act = kind
            result = await mgr.handle_action(user_id, "battle_action", {
                "battle_id": battle_id, "act": act, "params": payload})
    except StrategyBusyError:
        await send_to_ws(ws, {"type": "strategy.error", "req": req,
                              "msg": "世界推进中，请稍后再试"})
        return
    except StrategyError as exc:
        await send_to_ws(ws, {"type": "strategy.error", "req": req,
                              "msg": str(exc)})
        return
    except Exception:  # noqa: BLE001
        logger.exception(f"[webroom] 争霸 WS 指令 {action} 失败")
        await send_to_ws(ws, {"type": "strategy.error", "req": req,
                              "msg": "操作失败，请稍后重试"})
        return

    if not isinstance(result, dict):
        result = {"ok": True}
    await send_to_ws(ws, {
        "type": "strategy.battle.ack",
        "req": req,
        "req_id": req,
        "action": action,
        "act": act,
        "battle_id": battle_id,
        "result": result,
        "ts": int(time.time()),
    })
    if "round" in result or result.get("result"):
        try:
            await notify_strategy_battle(battle_id, result)
        except Exception:
            logger.debug("[webroom] 手操回合广播失败", exc_info=True)


async def broadcast_broadcast(room_id: str, payload: dict) -> int:
    """系统播报（世界事件）——前端渲染成置顶横幅。"""
    text = str(payload.get("text", "") or "")
    if not text:
        return 0
    out = {
        "type": "broadcast",
        "room": room_id,
        "text": text,
        "ts": int(time.time()),
    }
    _remember_history(room_id, out)
    return await broadcast(room_id, out)


def _drop(room_id: str, ws: web.WebSocketResponse) -> None:
    conns = _rooms.get(room_id)
    if conns is not None:
        conns.pop(ws, None)
        if not conns:
            _rooms.pop(room_id, None)


def _each_conn_of(username: str) -> list[tuple[str, web.WebSocketResponse, dict[str, Any]]]:
    """返回某账号当前所有连接（正常只有一条，但保留多开兼容）。"""
    out: list[tuple[str, web.WebSocketResponse, dict[str, Any]]] = []
    for rid, conns in _rooms.items():
        for ws, info in conns.items():
            if info.get("username") == username:
                out.append((rid, ws, info))
    return out


async def _broadcast_user_rooms(username: str, payload: dict[str, Any]) -> None:
    """把系统提示/在线名单刷新到该账号所在的频道。"""
    rooms = {rid for rid, _ws, _info in _each_conn_of(username)}
    for rid in rooms:
        await broadcast(rid, payload)
        await broadcast_presence(rid)


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
    _history_drop(room_id, msg_id)
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
    """静态资源版本号取三个文件的内容摘要，内容不变才允许复用缓存。

    用内容而不是 mtime：部署工具保留旧时间戳时，也能正确刷新浏览器缓存。
    """
    digest = hashlib.sha1()
    for name in ("index.html", "app.css", "app.js"):
        path = STATIC_DIR / name
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:12]


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
        headers={"Cache-Control": "no-store, max-age=0"},
    )


async def _api_commands(_request: web.Request) -> web.Response:
    return web.json_response(commands_catalog.all_commands())


async def _api_stickers(_request: web.Request) -> web.Response:
    """内置表情包目录：图片随插件静态资源提供，无需登录即可取目录。"""
    return web.json_response({"ok": True, "groups": stickers_catalog.groups()})


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

        # 同名装备可能有多件（同品质重复掉落、或不同品质各一件）。
        # 储物戒里还有未穿戴的副本时，点「装备」应当继续走替换流程，
        # 不能因为同名装备已经穿在身上就直接回「已装备」——那正是玩家
        # 想换上更好品质副本时「点了没反应」的原因。
        spare_copy = False
        if item_type in ItemRules.GEAR_TYPES:
            instance_manager = getattr(manager, "instance_manager", None)
            if instance_manager is not None:
                try:
                    spare_copy = bool(instance_manager.list_instances(player, "storage", item_name))
                except Exception:
                    spare_copy = False

        def _choice(value: str, label: str, desc: str = "") -> dict[str, str]:
            return {"value": value, "label": label, "desc": desc}

        if item_type in ItemRules.GEAR_TYPES:
            if item_type == "ring":
                ring_slots = [
                    ("戒指1", "ring1", slots.get("ring1", ("", None))[0]),
                    ("戒指2", "ring2", slots.get("ring2", ("", None))[0]),
                ]
                same = [label for label, _key, name in ring_slots if name == item_name]
                if same and not spare_copy:
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
            if slot and current_name == item_name and not spare_copy:
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
                hint = (
                    f"【{item_name}】已装备（栏位 {techniques.index(item_name) + 1}），"
                    "同名功法无法重复装备"
                )
                try:
                    spare = int((player.get_storage_ring_items() or {}).get(item_name, 0) or 0)
                except Exception:
                    spare = 0
                if spare > 0:
                    hint += f"；储物戒中的 {spare} 本为额外副本，可回收或赠予"
                result["hint"] = hint
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


async def _api_fate_shop(request: web.Request) -> web.Response:
    """仙缘商店面板：八组商品 + 已购层数 + 灵石兑换额度。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    user_id = accounts_store.player_key_of(user)
    try:
        data = await fate_shop_snapshot(_plugin, user_id)
    except Exception as exc:
        logger.exception("[webroom] 读取仙缘商店失败")
        return web.json_response({"ok": False, "error": f"读取失败：{exc}"}, status=500)
    return web.json_response(data)


async def _api_fate_shop_buy(request: web.Request) -> web.Response:
    """买一层：点卡片即升级。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    body = await _read_json(request)
    if not body:
        return web.json_response({"ok": False, "error": "请求格式不对"}, status=400)
    try:
        item_id = int(body.get("item_id") or 0)
    except (TypeError, ValueError):
        item_id = 0
    if item_id <= 0:
        return web.json_response({"ok": False, "error": "缺少 item_id"}, status=400)

    user_id = accounts_store.player_key_of(user)
    try:
        data = await fate_shop_buy(_plugin, user_id, item_id)
    except Exception as exc:
        logger.exception("[webroom] 仙缘商店购买失败")
        return web.json_response({"ok": False, "error": f"购买失败：{exc}"}, status=500)
    return web.json_response(data)


async def _api_fate_shop_exchange(request: web.Request) -> web.Response:
    """仙缘祭祀（1000 万灵石 / 次，按日期·气运·功德小概率成功）。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    body = await _read_json(request) or {}
    try:
        points = int(body.get("points") or 0)
    except (TypeError, ValueError):
        points = 0

    user_id = accounts_store.player_key_of(user)
    try:
        data = await fate_shop_exchange(_plugin, user_id, points)
    except Exception as exc:
        logger.exception("[webroom] 灵石兑换失败")
        return web.json_response({"ok": False, "error": f"兑换失败：{exc}"}, status=500)
    return web.json_response(data)


def _strategy_enabled() -> bool:
    """仙域争霸总开关：关闭后 REST/WS 入口统一拒绝（此前仅 tick 停摆）。"""
    plugin = _plugin
    mgr = getattr(plugin, "strategy_mgr", None) if plugin is not None else None
    if mgr is None:
        return False
    fn = getattr(mgr, "enabled", None)
    if not callable(fn):
        return True
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        # 开关状态读取失败时按关闭处理，避免异常状态下继续放行
        # REST/WS 写操作（fail-open 会绕过 strategy.enabled 配置）。
        logger.exception("[webroom] 读取仙域争霸开关失败，按未开启处理")
        return False


async def _api_strategy_logs(request: web.Request) -> web.Response:
    """仙域争霸「江湖纪事」：按势力/联盟可见性分页返回行动日志。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)
    if not _strategy_enabled():
        return web.json_response({"ok": False, "error": "仙域争霸未开启"}, status=400)
    user_id = accounts_store.player_key_of(user)
    try:
        limit = max(1, min(100, int(request.query.get("limit", 20) or 20)))
        data = await strategy_logs(
            _plugin, user_id,
            kind=str(request.query.get("kind") or ""),
            before_id=int(request.query.get("before_id", 0) or 0),
            limit=limit)
    except StrategyError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        logger.exception("[webroom] 读取江湖纪事失败")
        return web.json_response({"ok": False, "error": "读取失败，请稍后重试"},
                                 status=500)
    return web.json_response({
        "ok": True,
        "logs": data.get("items") or [],
        "has_more": bool(data.get("has_more")),
    })


async def _api_strategy_state(request: web.Request) -> web.Response:
    """仙域争霸：世界快照（大地图 + 玩家面板，含战争迷雾过滤）。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)
    if not _strategy_enabled():
        return web.json_response({"ok": False, "error": "仙域争霸未开启"}, status=400)

    user_id = accounts_store.player_key_of(user)
    try:
        data = await strategy_state(_plugin, user_id)
    except StrategyBusyError:
        return web.json_response({"ok": False, "error": "世界推进中，请稍后再试"},
                                 status=423)
    except StrategyError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        logger.exception("[webroom] 读取争霸快照失败")
        return web.json_response({"ok": False, "error": "读取失败，请稍后重试"},
                                 status=500)
    # 策略面板活跃订阅 + 顺手对账 tick（轮询也能拿到 delta）
    subscribe_strategy(str(user.get("username") or ""))
    world = (data or {}).get("world") or {}
    try:
        await _maybe_strategy_delta(
            int(world.get("tick_no") or 0), int(world.get("id") or 0))
    except Exception:
        logger.debug("[webroom] strategy.delta 对账失败", exc_info=True)
    return web.json_response({"ok": True, "state": data})


async def _api_strategy_action(request: web.Request) -> web.Response:
    """仙域争霸：统一动作分发（入场/招募/遣散/任务/编军/行军/围城/建设/商店）。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)
    if not _strategy_enabled():
        return web.json_response({"ok": False, "error": "仙域争霸未开启"}, status=400)

    body = await _read_json(request)
    if not body:
        return web.json_response({"ok": False, "error": "请求格式不对"}, status=400)
    action = str(body.get("action") or "").strip()
    if not action:
        return web.json_response({"ok": False, "error": "缺少 action"}, status=400)
    # v4.3.0：action 长度上限——防异常超长字符串打日志/审计膨胀
    if len(action) > 64:
        return web.json_response({"ok": False, "error": "action 无效"}, status=400)
    params = body.get("params") if isinstance(body.get("params"), dict) else {}

    user_id = accounts_store.player_key_of(user)
    try:
        result = await strategy_action(
            _plugin, user_id, action, params,
            nickname=str(user.get("nick") or user.get("username") or ""))
    except StrategyBusyError:
        return web.json_response({"ok": False, "error": "世界推进中，请稍后再试"},
                                 status=423)
    except StrategyError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        logger.exception(f"[webroom] 争霸动作 {action} 失败")
        return web.json_response({"ok": False, "error": "操作失败，请稍后重试"},
                                 status=500)
    if not isinstance(result, dict):
        result = {"ok": True}
    result.setdefault("ok", True)
    # 策略面板活跃订阅（用于 tick 增量推送）
    subscribe_strategy(str(user.get("username") or ""))
    # 手操战役：回合推进在 REST 内同步完成 → 同步广播给全部参与者
    if action in _STRATEGY_REST_BATTLE_ACTIONS:
        try:
            await notify_strategy_battle(int(params.get("battle_id") or 0), result)
        except Exception:
            logger.debug("[webroom] 手操回合广播失败", exc_info=True)
    return web.json_response(result)


# 会推进手操回合（或结束战役）的 REST action：处理完立刻广播 battle.event
_STRATEGY_REST_BATTLE_ACTIONS = {
    "battle_action", "battle_start", "battle_auto_deploy",
    "battle_delegate", "battle_set_ambush", "battle_set_formation",
}

# 指引目录索引（§12.5 / Agent F）：与 static/strategy/guide-data.js 的
# id/title/cat/keywords 保持一致，正文仍由前端 data 文件渲染，后端只提供
# 轻量索引，避免解析 JS 带来的启动开销与耦合。
_STRATEGY_GUIDE_INDEX: tuple[dict[str, Any], ...] = (
    {"id": "enter", "title": "入场与主城选择", "cat": "入门与界面",
     "keywords": ["入场", "入场遮罩", "主城", "起始据点", "新手包", "300 守军", "新城保护"]},
    {"id": "ui-overview", "title": "界面总览与操作路径", "cat": "入门与界面",
     "keywords": ["界面", "页签", "总览", "人员", "府邸", "军团", "势力", "联盟", "酒馆", "坊市", "战报", "商店", "成就", "设置", "指引", "据点抽屉"]},
    {"id": "tick", "title": "回合（tick）与离线推进", "cat": "入门与界面",
     "keywords": ["tick", "回合", "15 分钟", "96 tick", "离线", "补跑", "倒计时", "世界锁"]},
    {"id": "catchup", "title": "新手六步与追赶礼包", "cat": "入门与界面",
     "keywords": ["新手引导", "六步", "追赶礼包", "举荐", "产出加成", "150%", "14 天"]},
    {"id": "morale", "title": "民心（产出与哗变的核心）", "cat": "据点与建筑",
     "keywords": ["民心", "士气", "赈济", "布政", "行医", "哗变", "产出系数", "叛乱", "民心上限"]},
    {"id": "node-types", "title": "七类据点与产出", "cat": "据点与建筑",
     "keywords": ["据点", "仙城", "关隘", "灵脉矿", "灵谷田", "坊市", "分舵", "遗迹", "产出", "无主残军", "威慑"]},
    {"id": "node-upgrade", "title": "据点等级、潜力与建造时间", "cat": "据点与建筑",
     "keywords": ["据点等级", "潜力", "升级", "建造中", "城防", "1000", "自修", "阶梯费用", "围城暂停"]},
    {"id": "garrison-grow", "title": "守军自然增长与驻守加成", "cat": "据点与建筑",
     "keywords": ["守军", "自然增长", "驻守", "2%", "上限", "2000", "武将", "统率", "围城"]},
    {"id": "buildings", "title": "九种建筑与真实效果", "cat": "据点与建筑",
     "keywords": ["建筑", "城主府", "兵营", "坊市摊", "酒馆分号", "护山阵眼", "擂将台", "箭塔", "医馆", "粮仓", "造价"]},
    {"id": "protection", "title": "新城保护与护山盾", "cat": "据点与建筑",
     "keywords": ["保护期", "新城保护", "24 小时", "1 天", "96 小时", "4 天", "护山盾", "免战", "800 战功", "12 小时", "冷却 72 小时"]},
    {"id": "location-move", "title": "所在地与免费传送", "cat": "角色与招募",
     "keywords": ["所在地", "传送", "驻守", "空闲", "己方据点", "盟友据点", "围城", "遗迹", "名册"]},
    {"id": "roles", "title": "三类角色：武将 / 军师 / 内政官", "cat": "角色与招募",
     "keywords": ["角色", "武将", "军师", "内政官", "职责", "主将", "副将", "单挑资格"]},
    {"id": "stats", "title": "六维属性与稀有度", "cat": "角色与招募",
     "keywords": ["六维", "统率", "武勇", "智谋", "政务", "魅力", "气运", "稀有度", "凡品", "良品", "珍品", "极品", "传世", "90 上限"]},
    {"id": "tavern", "title": "酒馆招募与稀有度定价", "cat": "角色与招募",
     "keywords": ["酒馆", "招募", "候选", "普通招募", "募将令", "极品", "传世", "铜钱", "角色上限"]},
    {"id": "army-coalition", "title": "联军合军与统一行动", "cat": "军团与行军",
     "keywords": ["合军", "联军", "协同作战", "统一行军", "统一行动", "解除合军", "同据点", "两支部队"]},
    {"id": "army-siege-owner", "title": "围城归属保护与增援/营地战", "cat": "军团与行军",
     "keywords": ["围城", "攻城", "围城归属", "增援", "营地战", "抢围城", "守方", "攻方", "第三方", "围城进度", "接管", "对峙"]},
    {"id": "army-troop-fit", "title": "兵种与武将属性/适性适配", "cat": "军团与行军",
     "keywords": ["兵种", "主属性", "统率", "武勇", "智谋", "魅力", "适性", "S", "A", "飞剑", "重甲", "法修", "兽群", "剑盾", "NPC 兵种"]},
    {"id": "stamina-injury", "title": "体力、伤势与死亡", "cat": "角色与招募",
     "keywords": ["体力", "100", "恢复", "伤势", "轻伤", "重伤", "濒死", "陨落", "续命灵丹", "医馆"]},
    {"id": "loyalty-reward", "title": "忠诚自然恢复与奖赏", "cat": "角色与招募",
     "keywords": ["忠诚", "自然恢复", "每 tick", "奖赏", "一键奖赏", "铜钱", "官职", "100", "30", "出走"]},
    {"id": "task-basics", "title": "任务派遣：持续执行与远征", "cat": "任务与产出",
     "keywords": ["任务", "派遣", "持续", "每小时结算", "停止", "侦察", "劫掠", "远征", "体力 20", "协作", "随军", "驻守", "出军中断"]},
    {"id": "task-govern-heal", "title": "布政与行医（稳定涨民心）", "cat": "任务与产出",
     "keywords": ["布政", "行医", "民心", "1.5", "1.0", "治疗", "伤兵", "内政官", "佛修"]},
    {"id": "task-gather", "title": "屯田、采矿、贸易与修筑", "cat": "任务与产出",
     "keywords": ["屯田", "采矿", "贸易", "修筑", "粮草", "精铁", "铜钱", "商路", "灵脉矿", "灵谷田"]},
    {"id": "task-special", "title": "寻访、侦察、讨伐、劫掠与运粮", "cat": "任务与产出",
     "keywords": ["寻访", "侦察", "讨伐", "劫掠", "运粮", "募将令", "被俘", "声望", "反杀", "8%"]},
    {"id": "army-create", "title": "编军、兵种与带兵上限", "cat": "军团与行军",
     "keywords": ["编军", "军团", "主将", "兵种", "剑盾步", "重甲", "法修营", "飞剑队", "兽群", "破城槌", "带兵上限", "并发", "换将", "交接", "更换主将", "资源预算", "消耗", "携粮", "资源不足"]},
    {"id": "army-supply", "title": "粮耗、补给与断粮", "cat": "军团与行军",
     "keywords": ["粮耗", "补给", "断粮", "携粮", "运粮", "补粮", "补粮队", "1.5 倍", "士气", "逃散", "长途"]},
    {"id": "army-road-territory", "title": "友方道路与相邻领土加成", "cat": "军团与行军",
     "keywords": ["道路", "友方道路", "领土", "相邻", "行军加速", "免粮", "城防", "本土作战", "联盟"]},
    {"id": "army-refit", "title": "补兵、操练与老兵精锐", "cat": "军团与行军",
     "keywords": ["补兵", "操练", "老兵", "精锐", "御林", "100", "300", "士气上限", "全灭"]},
    {"id": "siege-auto", "title": "围城：自动推进的攻城战", "cat": "围城与攻城",
     "keywords": ["围城", "攻城", "城防", "20 tick", "破城槌", "反击", "伤势", "自动推进"]},
    {"id": "siege-manual", "title": "手操介入、巷战与受降", "cat": "围城与攻城",
     "keywords": ["手操", "督战", "90 秒", "巷战", "投降", "攻克", "1.5 倍", "备战"]},
    {"id": "encircle-node", "title": "据点合围与断粮迫降", "cat": "围城与攻城",
     "keywords": ["合围", "断粮", "迫降", "4 tick", "库存", "士气", "围而不打"]},
    {"id": "hex-board", "title": "hex 棋盘、部署与阵型", "cat": "hex 战役",
     "keywords": ["hex", "六边形", "棋盘", "13×9", "15×11", "9×11", "部署", "阵型", "备战", "30 秒"]},
    {"id": "hex-zoc", "title": "ZOC、夹击与完全合围", "cat": "hex 战役",
     "keywords": ["ZOC", "控制区", "夹击", "合围", "1.15", "1.3", "拦截", "撤退", "断粮"]},
    {"id": "hex-skills", "title": "战术技能、灵力与兵种专属", "cat": "hex 战役",
     "keywords": ["技能", "灵力", "火攻", "水淹", "惑敌", "挑衅", "坚守阵", "治疗术", "兽群突袭", "落石", "合击"]},
    {"id": "hex-night", "title": "夜袭与设伏", "cat": "hex 战役",
     "keywords": ["夜袭", "22:00", "6:00", "魔修", "佛修", "晨钟", "设伏", "森林", "先手"]},
    {"id": "duel", "title": "单挑（斗法）规则", "cat": "单挑与擂台",
     "keywords": ["单挑", "斗法", "武勇 75", "三局两胜", "15 秒", "士气", "伤势", "阵前斩落", "60%"]},
    {"id": "arena", "title": "演武擂台与会武分档", "cat": "单挑与擂台",
     "keywords": ["擂台", "摆擂", "应战", "赌注", "100", "100000", "会武", "分档", "彩头"]},
    {"id": "faction-create", "title": "个人势力与联盟会签", "cat": "势力与联盟",
     "keywords": ["势力", "个人势力", "联盟", "会签", "盟主", "成员势力",
                  "10000 铜钱", "3 个势力", "72 小时", "势力等级"]},
    {"id": "faction-alliance", "title": "联盟会签与据点交接", "cat": "势力与联盟",
     "keywords": ["联盟", "会签", "盟主", "3 个势力", "成员势力",
                  "据点交接", "退盟", "经济独立", "副主"]},
    {"id": "reputation", "title": "声望档位与外交四态", "cat": "势力与联盟",
     "keywords": ["声望", "仁义", "中立", "贪婪", "恶名", "停战", "结盟", "宣战", "备战 24 小时", "48 小时"]},
    {"id": "positions", "title": "角色官职与据点太守", "cat": "官职与经济",
     "keywords": ["官职", "势力主", "盟主", "太尉", "司徒", "军师祭酒", "中郎将",
                  "太守", "据点太守", "带兵上限", "任命"]},
    {"id": "treasury", "title": "势力经济、俸禄与钱包", "cat": "官职与经济",
     "keywords": ["玩家钱包", "俸禄", "势力主钱包", "研究费用", "迁都",
                  "赔款", "赎金", "10~50 铜钱", "太守俸禄", "欠俸"]},
    {"id": "faction-task", "title": "联盟任务与动员令", "cat": "官职与经济",
     "keywords": ["联盟任务", "势力任务", "共襄重建", "开疆拓土", "守土安民",
                  "兴业安邦", "剿匪安境", "动员令", "60 战功", "12 小时"]},
    {"id": "tech", "title": "势力科技树（三线主脉 + 支脉）", "cat": "科技与外交",
     "keywords": ["科技", "科技树", "主脉", "支脉", "分支", "军事", "经营", "谋略",
                  "林地", "山地", "7 级", "连点", "精铁", "研究", "指派角色"]},
    {"id": "diplomacy", "title": "外交提案与 NPC 好感度", "cat": "科技与外交",
     "keywords": ["外交", "联盟外交", "好感度", "赠礼", "与 NPC 结盟", "停战",
                  "和谈", "宣战", "60", "800 铜钱", "魅力"]},
    {"id": "plot", "title": "六种计略与效果", "cat": "计略",
     "keywords": ["计略", "离间", "谣言", "纵火", "劝降", "策应", "营救", "军师", "体力 30"]},
    {"id": "plot-risk", "title": "计略成功率、暴露与反间", "cat": "计略",
     "keywords": ["成功率", "35%", "85%", "暴露", "40%", "声望", "冷却 6 tick", "风声", "反间"]},
    {"id": "capture", "title": "被俘的三档概率与关押", "cat": "俘虏",
     "keywords": ["俘虏", "被俘", "60%", "40%", "20%", "关押", "5 个", "忠诚衰减", "巷战"]},
    {"id": "persuade", "title": "劝降、离心与忠诚", "cat": "俘虏",
     "keywords": ["劝降", "离心", "忠诚", "80%", "24 小时", "每日 3 次", "知遇", "声望"]},
    {"id": "captive-rescue", "title": "解救、赎回与细作营救", "cat": "俘虏",
     "keywords": ["解救", "营救", "赎回", "释放", "800 铜钱", "4~8 tick", "50 战功", "押运", "截获"]},
    {"id": "captive-handling", "title": "俘虏详情与处决 / 售卖 / 释放", "cat": "俘虏",
     "keywords": ["俘虏详情", "处决", "售卖", "释放", "战功", "铜钱", "声望", "外交",
                  "威慑", "流将", "六维", "特技", "劝降率"]},
    {"id": "resources", "title": "资源体系与灵石产出", "cat": "经济与商店",
     "keywords": ["资源", "铜钱", "粮草", "精铁", "募将令", "战功", "灵石", "日限额", "边际递减", "隔离"]},
    {"id": "merit-shop", "title": "战功商店与荣誉商店", "cat": "经济与商店",
     "keywords": ["战功商店", "荣誉商店", "灵石包", "续命灵丹", "护山盾", "举荐信", "装备", "限购"]},
    {"id": "auction", "title": "流将暗标竞拍", "cat": "经济与商店",
     "keywords": ["流将", "竞拍", "暗标", "战功", "12 小时", "起拍价", "赛季限购", "防狙击"]},
    {"id": "market", "title": "坊市互市与交易税", "cat": "互市",
     "keywords": ["互市", "坊市", "挂单", "交易税", "5%", "情报", "20 单", "48 小时", "NPC 收购", "报价", "定价区间", "跨势力"]},
    {"id": "trade-route", "title": "商路开通与收益", "cat": "互市",
     "keywords": ["商路", "20 币", "据点数量", "路径长度", "贸易", "30%", "切断", "商队", "7 折"]},
    {"id": "bandit", "title": "匪寨、妖巢与周期劫掠", "cat": "匪寨与商路",
     "keywords": ["匪寨", "妖巢", "劫掠", "每周 2~4", "72 小时", "800~1500", "8 tick", "战功", "募将令"]},
    {"id": "convoy", "title": "运粮队、护送与劫粮", "cat": "匪寨与商路",
     "keywords": ["运粮", "运粮队", "护送", "劫粮", "50%", "100 兵", "农政 L4", "截获"]},
    {"id": "demon-tide", "title": "魔潮爆发", "cat": "世界事件",
     "keywords": ["魔潮", "魔潮军团", "事件", "讨伐", "声望", "保护期", "每周保底"]},
    {"id": "season", "title": "赛季时间线、合纵与结算", "cat": "赛季与成就",
     "keywords": ["赛季", "75 天", "中期", "后期", "决战", "合纵", "40%", "结算", "荣誉", "重置"]},
    {"id": "achievements", "title": "成就册、名将堂与散修玩法", "cat": "赛季与成就",
     "keywords": ["成就", "战功", "名将堂", "英烈祠", "散修", "首破坚城", "以少胜多", "会武夺冠"]},
    {"id": "notify", "title": "通知分级与设置项", "cat": "通知与设置",
     "keywords": ["通知", "浏览器通知", "授权", "三档", "手操窗口", "设置", "自动托管", "移动端"]},
    {"id": "security", "title": "服务器权威、限速与观察期", "cat": "安全与规则",
     "keywords": ["安全", "限速", "90 次", "服务器权威", "观察期", "72 小时", "多开", "审计", "战报保留"]},
    {"id": "fair-play", "title": "公平游戏与数据说明", "cat": "安全与规则",
     "keywords": ["公平", "报点", "观战", "延迟", "违规", "数据", "隐私", "建议"]},
)

_STRATEGY_GUIDE_CATS: tuple[str, ...] = tuple(
    dict.fromkeys(e["cat"] for e in _STRATEGY_GUIDE_INDEX))


def _strategy_guide_tokens(q: str) -> list[str]:
    """搜索词切分：空格分隔多关键词（AND 语义），统一小写。"""
    return [t for t in str(q or "").lower().split() if t]


def _strategy_guide_hit(entry: dict[str, Any], tokens: list[str]) -> bool:
    if not tokens:
        return True
    blob = " ".join([
        str(entry.get("title") or ""),
        str(entry.get("cat") or ""),
        " ".join(str(k) for k in (entry.get("keywords") or [])),
    ]).lower()
    return all(t in blob for t in tokens)


async def _api_strategy_memorial(request: web.Request) -> web.Response:
    """仙域争霸：名将堂 / 英烈祠（§11）。未入场返回空列表而非报错。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)
    mgr = getattr(_plugin, "strategy_mgr", None)
    if mgr is None:
        return web.json_response({"ok": False, "error": "争霸玩法未启用"},
                                 status=503)
    if not _strategy_enabled():
        return web.json_response({"ok": False, "error": "仙域争霸未开启"}, status=400)

    empty = {"ok": True, "hall": [], "shrine": [], "season_no": 0}
    user_id = accounts_store.player_key_of(user)
    try:
        world = await mgr.get_world()
        if not world:
            return web.json_response(empty)
        wid = int(world.get("id") or 0)
        season_no = int(world.get("season_no") or 0)
        # 未入场：不拦 200，只回空榜（前端可直接渲染「暂无记录」）
        get_player = getattr(mgr, "get_player", None)
        if callable(get_player):
            player = await get_player(user_id, wid)
            if not player:
                return web.json_response({
                    "ok": True, "hall": [], "shrine": [],
                    "season_no": season_no or 1, "joined": False,
                })
        data = await mgr.get_memorials(wid)
    except Exception:  # noqa: BLE001
        logger.exception("[webroom] 读取名将堂失败")
        return web.json_response({"ok": False, "error": "读取失败，请稍后重试"},
                                 status=500)

    data = data if isinstance(data, dict) else {}
    hall = data.get("hall") if isinstance(data.get("hall"), list) else []
    shrine = data.get("shrine") if isinstance(data.get("shrine"), list) else []
    return web.json_response({
        "ok": True,
        "hall": hall,
        "shrine": shrine,
        "season_no": int(data.get("season_no") or season_no or 1),
        "joined": True,
    })


async def _api_strategy_guide(request: web.Request) -> web.Response:
    """仙域争霸：指引目录索引（20 分类 / 60 条），支持 ?q= 过滤。

    只回轻量索引 ``{id,title,cat,keywords}``，正文由前端 guide-data.js 渲染；
    索引是后端常量，进程内即可回答，不需要登录态以外的任何存档前提。
    """
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)

    q = str(request.query.get("q") or "").strip()[:64]
    tokens = _strategy_guide_tokens(q)
    items = [dict(e) for e in _STRATEGY_GUIDE_INDEX
             if _strategy_guide_hit(e, tokens)]
    return web.json_response({
        "ok": True,
        "q": q,
        "total": len(_STRATEGY_GUIDE_INDEX),
        "count": len(items),
        "categories": list(_STRATEGY_GUIDE_CATS),
        "items": items,
        # 契约 §3 兼容字段：sections 与 items 同源
        "sections": items,
    })


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
    adv_mode = str(body.get("adv_mode") or "").strip()
    adv_route = str(body.get("adv_route") or "").strip()
    adv_route2 = str(body.get("adv_route2") or "").strip()

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
            adv_mode=adv_mode,
            adv_route=adv_route,
            adv_route2=adv_route2,
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


# ---------- 意见反馈 API ----------


def _feedback_admin(user: dict | None) -> bool:
    """网页账号是否为反馈管理员：只认绑定 QQ 命中 BOSS_ADMINS。

    不能用账号名兜底：BOSS_ADMINS 通常是纯数字 QQ，开放注册下
    玩家注册同名账号就能冒充管理员查看/处理全部反馈。
    """
    if not user:
        return False
    admins = []
    if _plugin is not None:
        admins = [str(a) for a in (getattr(_plugin, "boss_admins", None) or [])]
    if not admins:
        return False
    qq = str(user.get("qq") or "")
    return bool(qq and qq in admins)


async def _feedback_snapshot(user: dict, body: dict) -> dict[str, Any]:
    """提交时自动附带账号与角色快照，方便管理员定位问题。"""
    player_key = accounts_store.player_key_of(user)
    snapshot: dict[str, Any] = {
        "nick": str(user.get("nick") or ""),
        "username": str(user.get("username") or ""),
        "player_key": player_key,
        "client_version": str(body.get("client_version") or "")[:40],
        "channel": str(body.get("channel") or "")[:64],
        "page": str(body.get("page") or "")[:120],
        "ua": str(body.get("ua") or "")[:200],
        "time": int(time.time()),
    }
    if _plugin is not None:
        try:
            player = await _plugin.db.get_player_by_id(player_key)
        except Exception:
            player = None
        if player is not None:
            try:
                snapshot["realm"] = player.get_level(_plugin.config_manager)
            except Exception:
                snapshot["realm"] = ""
            snapshot["cultivation_type"] = str(
                getattr(player, "cultivation_type", "") or ""
            )
            snapshot["level_index"] = int(getattr(player, "level_index", 0) or 0)
            snapshot["experience"] = int(getattr(player, "experience", 0) or 0)
    return snapshot


def _feedback_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def _api_feedback(request: web.Request) -> web.Response:
    """提交意见反馈；GET 拉取我的反馈（管理员可 scope=all 看全量）。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    is_admin = _feedback_admin(user)

    if request.method == "GET":
        scope = str(request.query.get("scope") or "mine")
        if scope == "all" and not is_admin:
            return web.json_response(
                {"ok": False, "error": "只有管理员可以查看全部反馈"}, status=403
            )
        limit = _feedback_int(request.query.get("limit"), 30)
        before = _feedback_int(request.query.get("before"), 0)
        status_raw = str(request.query.get("status") or "")
        status = int(status_raw) if status_raw in ("0", "1", "2") else None
        items = accounts_store.list_feedback(
            None if scope == "all" else user["username"],
            status=status,
            limit=limit,
            before=before,
        )
        return web.json_response(
            {
                "ok": True,
                "items": items,
                "is_admin": is_admin,
                "pending": accounts_store.count_feedback(0) if is_admin else 0,
                "kinds": accounts_store.FEEDBACK_KINDS,
            }
        )

    body = await _read_json(request)
    snapshot = await _feedback_snapshot(user, body)
    ok, msg, item = accounts_store.add_feedback(
        user["username"],
        player_key=str(snapshot.get("player_key") or ""),
        kind=str(body.get("kind") or ""),
        content=str(body.get("content") or ""),
        contact=str(body.get("contact") or ""),
        snapshot=snapshot,
    )
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    return web.json_response({"ok": True, "message": msg, "item": item})


async def _api_feedback_status(request: web.Request) -> web.Response:
    """管理员标记反馈为已处理(1)或已忽略(2)。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if not _feedback_admin(user):
        return web.json_response(
            {"ok": False, "error": "只有管理员可以处理反馈"}, status=403
        )
    body = await _read_json(request)
    ok, msg, item = accounts_store.update_feedback_status(
        _feedback_int(body.get("id"), 0),
        _feedback_int(body.get("status"), 0),
        str(body.get("note") or ""),
    )
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    return web.json_response(
        {
            "ok": True,
            "message": msg,
            "item": item,
            "pending": accounts_store.count_feedback(0),
        }
    )


# ---------- 仙界（频道）API ----------


async def _api_channels(request: web.Request) -> web.Response:
    """我的仙界 + 仙界大厅（仅公开频道）。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    username = user["username"]
    query = str(request.query.get("q", "") or "")
    try:
        joined = channels_store.joined_channels(username)
        hall = channels_store.public_channels(username, query=query)
    except Exception as exc:
        logger.exception("[webroom] 读取仙界列表失败")
        return web.json_response({"ok": False, "error": f"读取失败：{exc}"}, status=500)
    return web.json_response({"ok": True, "joined": joined, "public": hall})


async def _api_channels_create(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    body = await _read_json(request)
    ok, msg, channel = channels_store.create(
        user["username"],
        name=str(body.get("name", "") or ""),
        password=str(body.get("password", "") or ""),
        is_public=bool(body.get("public", True)),
    )
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    return web.json_response({"ok": True, "message": msg, "channel": channel})


async def _api_channels_join(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    body = await _read_json(request)
    identifier = str(
        body.get("channel_id")
        or body.get("uuid")
        or body.get("name")
        or ""
    )
    ok, msg, channel = channels_store.join(
        user["username"],
        identifier=identifier,
        password=str(body.get("password", "") or ""),
    )
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    # 新成员计入频道名单（离线），让已在频道里的道友立刻看到
    if channel:
        await broadcast_presence(str(channel["id"]))
    return web.json_response({"ok": True, "message": msg, "channel": channel})


async def _api_channels_update(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    body = await _read_json(request)
    channel_id = str(body.get("channel_id", "") or "")
    name = body.get("name") if "name" in body else None
    is_public = bool(body.get("public")) if "public" in body else None
    password = body.get("password") if "password" in body else None
    ok, msg, channel = channels_store.update(
        user["username"],
        channel_id,
        name=str(name) if name is not None else None,
        is_public=is_public,
        password=str(password) if password is not None else None,
        clear_password=bool(body.get("clear_password")),
    )
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    return web.json_response({"ok": True, "message": msg, "channel": channel})


# ---------- 私聊（DM）API（§12.7，通用能力） ----------

#: username -> 最近 1 秒内的发送时间戳（私聊限速与公屏同标准，§18：>2 条/秒丢弃）
_dm_rate: dict[str, deque] = {}


def _dm_rate_ok(username: str) -> bool:
    now = time.monotonic()
    dq = _dm_rate.get(username)
    if dq is None:
        dq = _dm_rate[username] = deque()
    while dq and now - dq[0] > 1.0:
        dq.popleft()
    if len(dq) >= 2:
        return False
    dq.append(now)
    return True


async def _api_dm_conversations(request: web.Request) -> web.Response:
    """我的私聊会话列表（含对方资料 / 预览 / 未读数）。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    try:
        items = dm_store.conversations(user["username"])
        unread = dm_store.unread_total(user["username"])
    except Exception as exc:
        logger.exception("[webroom] 读取私聊会话失败")
        return web.json_response({"ok": False, "error": f"读取失败：{exc}"}, status=500)
    return web.json_response({"ok": True, "items": items, "unread": unread})


async def _api_dm_history(request: web.Request) -> web.Response:
    """与某人的私聊历史：GET /api/dm/history?with=xxx&limit=50&before=0"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    other = str(request.query.get("with", "") or "").strip()
    if not other:
        return web.json_response({"ok": False, "error": "缺少对方账号"}, status=400)
    try:
        limit = int(request.query.get("limit") or 50)
        before = int(request.query.get("before") or 0)
    except (TypeError, ValueError, OverflowError):
        return web.json_response({"ok": False, "error": "分页参数必须为整数"}, status=400)
    try:
        items = dm_store.history(user["username"], other, limit=limit, before=before)
    except Exception as exc:
        logger.exception("[webroom] 读取私聊历史失败")
        return web.json_response({"ok": False, "error": f"读取失败：{exc}"}, status=500)
    return web.json_response({"ok": True, "items": items, "with": other})


async def _api_dm_send(request: web.Request) -> web.Response:
    """REST 发送私聊（WS dm.send 的兜底通道），成功后实时推给收件人。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    body = await _read_json(request)
    to = str(body.get("to", "") or "").strip()
    text = str(body.get("text", "") or "").strip()
    if not _dm_rate_ok(user["username"]):
        return web.json_response(
            {"ok": False, "error": "发送太快了，歇一歇（>2 条/秒）"}, status=429)
    ok, err, msg = dm_store.send(user["username"], to, text)
    if not ok:
        return web.json_response({"ok": False, "error": err}, status=400)
    await push_to_username("", to, {"type": "dm.new", "message": msg})
    return web.json_response({"ok": True, "message": msg})


async def _api_dm_read(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    body = await _read_json(request)
    other = str(body.get("with", "") or "").strip()
    if not other:
        return web.json_response({"ok": False, "error": "缺少对方账号"}, status=400)
    dm_store.mark_read(user["username"], other)
    # 已读回执推给对方（前端显示「已读」）
    await push_to_username("", other, {
        "type": "dm.read", "by": user["username"],
        "convo_id": dm_store.convo_id(user["username"], other)})
    return web.json_response({"ok": True, "unread": dm_store.unread_total(user["username"])})


async def _api_dm_block(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    body = await _read_json(request)
    ok, msg = dm_store.block(user["username"], str(body.get("target", "") or ""))
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    return web.json_response({"ok": True, "message": msg,
                              "items": dm_store.blocks(user["username"])})


async def _api_dm_unblock(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    body = await _read_json(request)
    ok, msg = dm_store.unblock(user["username"], str(body.get("target", "") or ""))
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    return web.json_response({"ok": True, "message": msg,
                              "items": dm_store.blocks(user["username"])})


async def _api_dm_blocks(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    return web.json_response({"ok": True, "items": dm_store.blocks(user["username"])})


async def _api_market_candles(request: web.Request) -> web.Response:
    """仙市标的 K 线：GET /api/market/candles?code=S01&period=day&limit=60

    日线由逐盘收盘价合成（开=上一日收盘、高/低=盘内极值、收=末盘），
    周/月再聚合。前端拿不到数据时退回卡片里的「近 8 盘」文本明细。
    """
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    code = str(request.query.get("code", "") or "").strip().upper()
    if not code:
        return web.json_response({"ok": False, "error": "缺少标的代码"}, status=400)
    period = str(request.query.get("period", "day") or "day").strip().lower()
    limit = str(request.query.get("limit", "60") or "60")
    manager = getattr(_plugin, "invest_mgr", None)
    if manager is None:
        return web.json_response({"ok": False, "error": "仙市尚未就绪"}, status=503)
    try:
        data = await manager.get_candles(code, period, limit)
    except Exception as exc:
        logger.exception("[webroom] 读取 K 线失败")
        return web.json_response({"ok": False, "error": f"读取失败：{exc}"}, status=500)
    if not data:
        return web.json_response({"ok": False, "error": f"仙市中不存在代码「{code}」"}, status=404)
    return web.json_response({"ok": True, **data})


async def _api_invest_agents(request: web.Request) -> web.Response:
    """代理人面板：三个 NPC + 当前委托 + 待领取 + 历史。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    user_id = accounts_store.player_key_of(user)
    try:
        data = await invest_agent_snapshot(_plugin, user_id)
    except Exception as exc:
        logger.exception("[webroom] 读取投资代理人失败")
        return web.json_response({"ok": False, "error": f"读取失败：{exc}"}, status=500)
    return web.json_response(data)


async def _api_invest_agents_start(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    body = await _read_json(request)
    if not body:
        return web.json_response({"ok": False, "error": "请求格式不对"}, status=400)
    agent_key = str(body.get("agent") or body.get("agent_key") or "").strip()
    if not agent_key:
        return web.json_response({"ok": False, "error": "缺少代理人"}, status=400)
    try:
        principal = int(body.get("principal") or 0)
        duration = int(body.get("duration") or 0)
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "本金和期限必须是整数"}, status=400)

    user_id = accounts_store.player_key_of(user)
    try:
        data = await invest_agent_start(_plugin, user_id, agent_key, principal, duration)
    except Exception as exc:
        logger.exception("[webroom] 雇佣投资代理人失败")
        return web.json_response({"ok": False, "error": f"委托失败：{exc}"}, status=500)
    return web.json_response(data)


async def _api_invest_agents_stop(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    user_id = accounts_store.player_key_of(user)
    try:
        data = await invest_agent_stop(_plugin, user_id)
    except Exception as exc:
        logger.exception("[webroom] 停止投资代理人失败")
        return web.json_response({"ok": False, "error": f"停止失败：{exc}"}, status=500)
    return web.json_response(data)


async def _api_invest_agents_claim(request: web.Request) -> web.Response:
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    if _plugin is None:
        return web.json_response({"ok": False, "error": "插件尚未就绪"}, status=503)

    user_id = accounts_store.player_key_of(user)
    try:
        data = await invest_agent_claim(_plugin, user_id)
    except Exception as exc:
        logger.exception("[webroom] 领取投资代理人结算失败")
        return web.json_response({"ok": False, "error": f"领取失败：{exc}"}, status=500)
    return web.json_response(data)


async def _api_health(_request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "adapter": _adapter is not None,
            "accounts": accounts_store.count(),
            # 静态资源版本：排查「改完页面没变化」时，和浏览器里 app.js?v= 对一下即可
            "asset": _asset_ver(),
            "rooms": {rid: online_players(rid) for rid in _rooms},
        }
    )


async def _api_server_time(_request: web.Request) -> web.Response:
    """服务端时间基准（v4.6.5）：前端倒计时兜底，避免设备时钟偏差导致冻结。"""
    return web.json_response({"ok": True, "now": int(time.time())})


# ---------- App 版本与安装包 ----------
#
# 前端页面本身走远程 URL，改 webchat_room/static 即刻生效，不需要「推送」。
# 只有原生层（Java / 权限 / 清单）变了才必须重装 APK，这条链路服务于那一种情况：
#   /api/app/version 读清单 → App 内下载 /download/xxx.apk → 拉起系统安装器


def _apk_dir() -> Path:
    """APK 存放目录，与 imgs / avatars 同级放在 AstrBot 数据目录下。"""
    return Path(get_astrbot_data_path()) / "webroom" / "apk"


def _release_manifest_path() -> Path:
    """版本清单：手工维护一个 JSON，比把版本号写进代码更好改。"""
    return Path(get_astrbot_data_path()) / "webroom" / "app_release.json"


async def _api_app_version(_request: web.Request) -> web.Response:
    """返回最新 App 版本；没配清单就当「没发过版」，客户端静默跳过。"""
    path = _release_manifest_path()
    if not path.is_file():
        return web.json_response({"ok": False, "error": "尚未发布版本"}, status=404)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[webroom] app_release.json 解析失败")
        return web.json_response({"ok": False, "error": "版本清单损坏"}, status=500)
    if not isinstance(raw, dict):
        return web.json_response({"ok": False, "error": "版本清单格式错误"}, status=500)

    try:
        version_code = int(raw.get("versionCode") or 0)
    except (TypeError, ValueError):
        version_code = 0
    file_name = str(raw.get("file") or "").strip()
    if version_code <= 0 or not _safe_name(file_name):
        return web.json_response({"ok": False, "error": "版本清单缺少 versionCode/file"}, status=500)

    apk = _apk_dir() / file_name
    if not apk.is_file():
        # 清单写了但包没上传：回 404 让客户端别弹「更新」把用户引到死链
        logger.warning(f"[webroom] 清单指向的安装包不存在: {apk}")
        return web.json_response({"ok": False, "error": "安装包未上传"}, status=404)

    changelog = raw.get("changelog")
    if isinstance(changelog, str):
        changelog = [changelog]
    if not isinstance(changelog, list):
        changelog = []
    try:
        min_version = int(raw.get("minVersionCode") or 0)
    except (TypeError, ValueError):
        min_version = 0
    published = str(raw.get("publishedAt") or "")

    return web.json_response(
        {
            "ok": True,
            "versionCode": version_code,
            "versionName": str(raw.get("versionName") or ""),
            "size": apk.stat().st_size,
            "url": f"/download/{file_name}",
            "sha256": str(raw.get("sha256") or ""),
            "changelog": [str(x) for x in changelog][:12],
            # 低于这个版本必须更新才能继续用；0 表示只提示不强推
            "minVersionCode": min_version,
            "publishedAt": published,
        }
    )


async def _api_apk(request: web.Request) -> web.Response:
    """分发 APK。FileResponse 自带 Range 支持，断点续传与进度条都依赖它。"""
    name = request.match_info["name"]
    if not _safe_name(name) or not name.lower().endswith(".apk"):
        return web.json_response({"error": "非法路径"}, status=400)
    path = _apk_dir() / name
    if not path.is_file():
        return web.json_response({"error": "文件不存在"}, status=404)
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "application/vnd.android.package-archive",
            "Content-Disposition": f'attachment; filename="{name}"',
            # 安装包按文件名带版本，内容不会原地变，允许长缓存省流量
            "Cache-Control": "public, max-age=86400",
        },
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
    for _rid, _ws, info in _each_conn_of(username):
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
    for _rid, _ws, info in _each_conn_of(user["username"]):
        info["player_name"] = fresh["nick"]
    await _broadcast_user_rooms(
        user["username"],
        {
            "type": "sys",
            "text": f"{user['nick']} 改名为 {fresh['nick']}",
            "ts": int(time.time()),
        },
    )
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
    pending = accounts_store.pending_bind_request(user["username"])
    return web.json_response(
        {
            "ok": True,
            "qq": qq,
            "player_key": key,
            "save": save,
            # 网页独立存档：解绑后回到的那份，让用户知道绑过去会“看不见”它
            "web_save": web_save,
            "pending": pending,
        }
    )


async def _api_bind(request: web.Request) -> web.Response:
    """提交 QQ 绑定申请（须超管在群内批准后才改 player_key）。

    v4.3.0：堵住「注册网页号 → POST /api/bind → 立刻接管受害者 QQ 存档
    → WS strategy_admin 过 _check_boss_admin」的接管链。
    """
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
    ok, msg, rid = accounts_store.submit_bind_request(user["username"], qq)
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    logger.info(
        f"[webroom] 绑定申请 #{rid}：{user['username']} → QQ {qq}"
    )
    return web.json_response(
        {
            "ok": True,
            "pending": True,
            "request_id": rid,
            "qq": qq,
            "save": save,
            "web_save": web_save,
            "message": msg,
        }
    )


async def _api_bind_cancel(request: web.Request) -> web.Response:
    """用户取消自己的待批绑定申请。"""
    user = _token_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "登录已过期"}, status=401)
    data = await _read_json(request) or {}
    rid = int(data.get("request_id") or 0)
    ok, msg = accounts_store.cancel_bind_request(user["username"], rid)
    if not ok:
        return web.json_response({"ok": False, "error": msg}, status=400)
    return web.json_response({"ok": True, "message": msg})


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

    rooms: set[str] = set()
    for rid, _ws, info in _each_conn_of(user["username"]):
        info["avatar"] = value
        rooms.add(rid)
    for rid in rooms:
        await broadcast_presence(rid)
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
    # 登录态靠 token 判定；channel 仅作重连恢复，服务端会再校验成员身份
    token = request.query.get("token", "").strip()
    username = accounts_store.parse_token(token)
    user = accounts_store.get(username) if username else None
    if user is None:
        return web.json_response({"error": "未登录或登录已过期"}, status=401)

    requested = request.query.get("channel", "").strip()
    room_id = ROOM_ID
    if requested and channels_store.can_access(requested, user["username"]):
        room_id = requested
    player_name = user["nick"]

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)

    # 顶号：同一账号的旧连接踢下线（换设备/重开页面）
    old_rooms: set[str] = set()
    for old_room, old_ws, _info in _each_conn_of(user["username"]):
        try:
            await old_ws.close(code=4001, message=b"replaced by new login")
        except Exception:
            pass
        _drop(old_room, old_ws)
        old_rooms.add(old_room)

    conns = _rooms.setdefault(room_id, {})
    # 绑定了 QQ 就用 QQ 号当存档 key（与旧机器人的 players.user_id 对齐）
    player_id = accounts_store.player_key_of(user)
    conns[ws] = {
        "player_id": player_id,
        "player_name": player_name,
        "username": user["username"],
        "avatar": user.get("avatar", "") or "",
        "room": room_id,
    }
    # 记一笔「来过房间」，离线后仍留在成员名单里
    accounts_store.touch_room(user["username"])

    await broadcast_presence(room_id)
    for rid in old_rooms - {room_id}:
        # 旧频道里这个人已经下线，名单需要刷新
        await broadcast_presence(rid)
    await ws.send_json(
        {
            "type": "hello",
            "room": room_id,
            "channel": channels_store.describe(room_id, user["username"]),
            "player": player_name,
            "nick": player_name,
            "username": user["username"],
            "avatar": user.get("avatar", "") or "",
            "online": room_members(room_id),
            "history": _history_for(room_id),
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
        info = None
        # 连接可能已经切换过频道，按 info 记录的实际频道清理
        for rid, conns_now in list(_rooms.items()):
            if ws in conns_now:
                info = conns_now.get(ws)
                _drop(rid, ws)
                if info:
                    # 离开不发系统提示，只刷新成员名单（人变成离线）
                    await broadcast_presence(rid)
                    logger.info(f"[webroom] {info['player_name']} 离开 {rid}")
                break
    return ws


async def _handle_channel_switch(
    room_id: str, ws: web.WebSocketResponse, data: dict[str, Any]
) -> None:
    """切换当前频道：只移动这条连接，并把目标频道最近消息回放给本人。"""
    info = _rooms.get(room_id, {}).get(ws)
    if info is None:
        await send_to_ws(ws, {"type": "channel_error", "text": "尚未完成连接握手"})
        return
    target = str(
        data.get("channel_id") or data.get("channel") or data.get("uuid") or ""
    ).strip()
    if not target:
        await send_to_ws(ws, {"type": "channel_error", "text": "缺少频道 ID"})
        return
    username = str(info.get("username") or "")
    if not channels_store.is_member(target, username):
        await send_to_ws(
            ws, {"type": "channel_error", "text": "尚未加入该仙界，请先在大厅或 UUID 入口加入"}
        )
        return
    desc = channels_store.describe(target, username)
    if desc is None:
        await send_to_ws(ws, {"type": "channel_error", "text": "仙界不存在"})
        return

    if target != room_id:
        # 先离开旧频道，再进入新频道；该连接在内存表里挪位置
        _drop(room_id, ws)
        _rooms.setdefault(target, {})[ws] = info
        info["room"] = target
        # 进出只刷新成员名单，不发「进入/离开房间」群提示
        accounts_store.touch_room(username)
        await broadcast_presence(room_id)

    # 先给切换者回执+历史，再刷新新频道成员名单
    await send_to_ws(
        ws,
        {
            "type": "channel_switched",
            "room": target,
            "channel": desc,
            "history": _history_for(target),
        },
    )
    if target != room_id:
        await broadcast_presence(target)


async def _broadcast_sticker(
    room_id: str, info: dict[str, str], sticker: dict[str, str]
) -> None:
    """广播一条纯表情消息（不走机器人流水线，避免表情触发指令解析）。"""
    ts = int(time.time())
    msg_id = uuid.uuid4().hex[:16]
    text = f"[表情:{sticker.get('label', '')}]"
    _remember_msg(room_id, msg_id, info, text, ts)
    payload = {
        "type": "chat",
        "room": room_id,
        "msg_id": msg_id,
        "from": info["player_name"],
        "username": info.get("username", ""),
        "avatar": info.get("avatar", ""),
        "text": "",
        "sticker": {
            "id": sticker.get("id", ""),
            "label": sticker.get("label", ""),
            "url": sticker.get("url", ""),
        },
        "is_cmd": False,
        "audience": "public",
        "ts": ts,
    }
    _remember_history(room_id, payload)
    conns = _rooms.get(room_id, {})
    dead: list[web.WebSocketResponse] = []
    for client_ws in list(conns):
        try:
            await client_ws.send_json(payload)
        except Exception:
            dead.append(client_ws)
    for killed in dead:
        _drop(room_id, killed)


async def _handle_dm_action(room_id: str, ws: web.WebSocketResponse,
                            action: str, data: dict) -> None:
    """WS 私聊消息族：dm.send / dm.history / dm.list / dm.read / dm.block / dm.unblock。

    鉴权复用连接握手的网页账号；限速与公屏同标准（§18：>2 条/秒丢弃并警告）。
    """
    info = _rooms.get(room_id, {}).get(ws)
    if info is None:
        await send_to_ws(ws, {"type": "error", "text": "尚未完成连接握手"})
        return
    username = str(info.get("username") or "")
    if not username:
        await send_to_ws(ws, {"type": "error", "text": "请先登录"})
        return
    req_id = str(data.get("req_id") or "").strip()[:64]
    try:
        if action == "dm.send":
            if not _dm_rate_ok(username):
                await send_to_ws(ws, {"type": "dm.error", "req_id": req_id,
                                      "text": "发送太快了，歇一歇（>2 条/秒）"})
                return
            to = str(data.get("to") or "").strip()
            text = str(data.get("text") or "").strip()
            ok, err, msg = dm_store.send(username, to, text)
            if not ok:
                await send_to_ws(ws, {"type": "dm.error", "req_id": req_id,
                                      "text": err})
                return
            await send_to_ws(ws, {"type": "dm.sent", "req_id": req_id,
                                  "message": msg})
            # 实时投递给收件人（跨频道兜底搜索）
            await push_to_username("", to, {"type": "dm.new", "message": msg})
        elif action == "dm.history":
            other = str(data.get("with") or "").strip()
            items = dm_store.history(username, other,
                                     limit=int(data.get("limit") or 50),
                                     before=int(data.get("before") or 0))
            await send_to_ws(ws, {"type": "dm.history", "req_id": req_id,
                                  "with": other, "items": items})
        elif action == "dm.list":
            await send_to_ws(ws, {
                "type": "dm.list", "req_id": req_id,
                "items": dm_store.conversations(username),
                "unread": dm_store.unread_total(username)})
        elif action == "dm.read":
            other = str(data.get("with") or "").strip()
            dm_store.mark_read(username, other)
            # 已读回执推给对方
            await push_to_username("", other, {
                "type": "dm.read", "by": username,
                "convo_id": dm_store.convo_id(username, other)})
            await send_to_ws(ws, {
                "type": "dm.read_ok", "req_id": req_id, "with": other,
                "unread": dm_store.unread_total(username)})
        elif action in ("dm.block", "dm.unblock"):
            target = str(data.get("target") or "").strip()
            if action == "dm.block":
                ok, msg = dm_store.block(username, target)
            else:
                ok, msg = dm_store.unblock(username, target)
            await send_to_ws(ws, {
                "type": "dm.block_ok", "req_id": req_id, "ok": ok, "text": msg,
                "target": target, "blocked": action == "dm.block",
                "items": dm_store.blocks(username)})
        else:
            await send_to_ws(ws, {"type": "error", "text": f"未知 action: {action}"})
    except (TypeError, ValueError, OverflowError):
        await send_to_ws(ws, {"type": "dm.error", "req_id": req_id,
                              "text": "参数无效"})
    except Exception:
        logger.exception("[webroom] 私聊处理失败")
        await send_to_ws(ws, {"type": "dm.error", "req_id": req_id,
                              "text": "私聊处理失败，请稍后再试"})


async def _handle_client_message(room_id: str, ws: web.WebSocketResponse, raw: str) -> None:
    try:
        data = json.loads(raw)
    except Exception:
        await send_to_ws(ws, {"type": "error", "text": "消息格式错误"})
        return
    if not isinstance(data, dict):
        await send_to_ws(ws, {"type": "error", "text": "消息格式错误"})
        return

    # 连接可能切换过频道：以内存表中的实际频道为准，避免继续用旧 room_id
    for rid, conns in _rooms.items():
        if ws in conns:
            room_id = str(conns[ws].get("room") or rid)
            break

    action = data.get("action")
    # 仙域争霸上行以 {"type":"strategy.xxx"} 标识（§12.5），兼容旧 action 字段
    if not action and str(data.get("type") or "").startswith("strategy."):
        action = data.get("type")
    if action == "ping":
        await send_to_ws(ws, {"type": "pong"})
        return
    if action == "online":
        await send_to_ws(
            ws, {"type": "presence", "players": room_members(room_id)}
        )
        return
    if action in ("switch", "switch_channel"):
        await _handle_channel_switch(room_id, ws, data)
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
            try:
                limit = int(data.get("limit") or 50)
                before = int(data.get("before") or 0)
            except (TypeError, ValueError, OverflowError):
                await send_to_ws(ws, {"type": "error", "text": "消息分页参数必须为整数"})
                return
            items, unread = accounts_store.list_notices(
                username,
                limit=limit,
                before=before,
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
    if str(action or "").startswith("strategy."):
        await _handle_strategy_ws(room_id, ws, str(action), data)
        return
    if str(action or "").startswith("dm."):
        await _handle_dm_action(room_id, ws, str(action), data)
        return
    if action not in ("msg", "cmd"):
        await send_to_ws(ws, {"type": "error", "text": f"未知 action: {action}"})
        return

    text = str(data.get("text", "")).strip()
    # 纯表情消息：客户端只提交 id，服务端按白名单解析图片，避免任意 URL 注入
    sticker = stickers_catalog.get(str(data.get("sticker") or "")) if not text else None
    if not text and sticker is None:
        return
    if len(text) > 2000:
        await send_to_ws(ws, {"type": "error", "text": "消息过长（>2000 字）"})
        return

    info = _rooms.get(room_id, {}).get(ws)
    if info is None:
        await send_to_ws(ws, {"type": "error", "text": "尚未完成连接握手"})
        return

    if sticker is not None and action == "msg":
        await _broadcast_sticker(room_id, info, sticker)
        return
    if action == "msg" and text:
        # 兼容贴吧快捷写法 #(滑稽)：前端未加载目录时也能按名称发送
        shortcut = re.fullmatch(r"#\((.{1,12})\)", text)
        if shortcut:
            shortcut_sticker = stickers_catalog.find_by_label(shortcut.group(1))
            if shortcut_sticker is not None:
                await _broadcast_sticker(room_id, info, shortcut_sticker)
                return
    if not text:
        return

    req_id = str(data.get("req_id") or "").strip()[:64]
    # 看板请求：前端要原生渲染，后端优先返回结构化卡片
    want_card = bool(data.get("card"))
    # panel 以指令名为准兜底：旧版前端只发 private、玩家手动输入也不带标记
    panel = bool(data.get("panel")) or (
        action == "cmd" and is_panel_command(text)
    )
    if panel:
        want_card = True
    settings = accounts_store.get_settings(str(info.get("username") or ""))

    if action == "cmd":
        audience = resolve_audience(text, settings, panel=panel)
        # 兼容升级前的旧版前端：当时所有 cmd 都固定带 private:true，保持其私密语义
        if audience == "public" and bool(data.get("private")):
            audience = "self"
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
        _remember_history(room_id, payload)
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
            # AstrBot 会话仍固定主频道：白名单、自动代理与宗门事件等逻辑不变；
            # 实际回复频道由 channel_id 单独传递，由事件层按频道广播。
            ROOM_ID,
            info["player_id"],
            info["player_name"],
            text,
            req_id=req_id or None,
            private=private,
            want_card=want_card,
            audience=audience,
            panel=panel,
            username=str(info.get("username") or ""),
            channel_id=room_id,
        )
    except Exception:
        logger.exception("[webroom] 提交玩家消息到流水线失败")
        if private:
            await send_to_ws(
                ws,
                {"type": "reply", "req_id": req_id, "text": "提交失败，请稍后再试", "error": True},
            )
