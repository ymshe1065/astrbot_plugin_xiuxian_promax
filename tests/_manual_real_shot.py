# -*- coding: utf-8 -*-
"""真实链路页面验收：/丹药背包 从「命令方法」一路走到浏览器。

与 _manual_list_shot.py 的区别：
  那边是「手工构造卡片 → 直接广播」，只验前端；
  这边走真实链路：XiuXianPlugin.handle_show_pills（main.py 真实方法体）
    → @require_whitelist 按平台名把位图开关关掉（is_webroom_platform）
    → PillHandler.handle_show_pills（真实 player_required + 真实 db）
    → render_web_card → sidecar
    → load_card_for_image 读回 payload
    → broadcast_bot_payload → 浏览器渲染。

同时跑一条 platform="qq" 的对照：断言同一条命令在 QQ 侧仍然是纯文本。

跑法：python tests/_manual_real_shot.py [pill|fate|title] [--no-browser]
产物：_grid_shot/real_<key>.png
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PLUG = _HERE.parent
ROOT = PLUG.parent
sys.path.insert(0, str(PLUG))
sys.path.insert(0, str(PLUG / "scripts"))
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "astrbot"))  # shot.py 在这里
os.environ["XIU_BASE"] = "http://127.0.0.1:6219"
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

import dev_webroom  # noqa: E402

import types  # noqa: E402

dev_webroom._install_astrbot_stubs()
_mod = types.ModuleType
# 让 astrbot / astrbot.api 变成真正的 package，否则导入 astrbot.api.star 会报
# "not a package"。桩只用于本脚本，不污染 unittest。
_pkg_root = sys.modules["astrbot"]
_pkg_root.__path__ = []
_api = sys.modules["astrbot.api"]
_api.__path__ = []
_api.AstrBotConfig = dict


class _Star:
    def __init__(self, context=None):
        self.context = context


class _StarTools:
    @staticmethod
    def get_data_dir(name):
        d = Path(tempfile.gettempdir()) / "astrbot_webroom_dev" / name
        d.mkdir(parents=True, exist_ok=True)
        return d


_star = _mod("astrbot.api.star")
_star.Context = object
_star.Star = _Star
_star.StarTools = _StarTools
_api.star = _star
sys.modules["astrbot.api.star"] = _star


COMMAND_REGISTRY: list[tuple[tuple, callable]] = []
"""main.py 里 @filter.command(...) 的 (参数, 方法)。

真实 astrbot 靠这张表做命令路由；我们的桩原本把它丢了，导致「玩家输入命令」
这条路没法验。这里记下来，_manual_real_input.py 就能复刻一次真实派发。
"""


class _Filter:
    """@filter.command(...) 做元数据绑定（记进 COMMAND_REGISTRY），方法原样返回。"""

    def command(self, *a, **k):
        def deco(fn):
            COMMAND_REGISTRY.append((a, fn))
            return fn
        return deco

    def __getattr__(self, _name):
        def deco(*a, **k):
            def wrap(fn):
                return fn
            return wrap
        return deco


_ev = _mod("astrbot.api.event")
_ev.AstrMessageEvent = object
_ev.filter = _Filter()
_api.event = _ev
_ev.__path__ = []
sys.modules["astrbot.api.event"] = _ev
_all = _mod("astrbot.api.all")
_all.At = object
_all.Plain = object
_api.all = _all
sys.modules["astrbot.api.all"] = _all

_core = sys.modules["astrbot.core"]
_core.__path__ = []
for _name in ("astrbot.core.star", "astrbot.core.star.filter"):
    _m = _mod(_name)
    _m.__path__ = []
    sys.modules[_name] = _m
_cmd = _mod("astrbot.core.star.filter.command")
_cmd.GreedyStr = str
sys.modules["astrbot.core.star.filter.command"] = _cmd

PKG = "astrbot_plugin_xiuxian_promax"
OUT = ROOT / "_grid_shot"
OUT.mkdir(exist_ok=True)


def _imp(p):
    return __import__(f"{PKG}.{p}", fromlist=["x"])


CM = _imp("config_manager").ConfigManager(PLUG)
DB_CLS = _imp("data").DataBase
Player = _imp("models").Player
room_server = _imp("webchat_room.room_server")
accounts = _imp("webchat_room.accounts")
load_card_for_image = _imp("webchat_room.card_sidecar").load_card_for_image
main_mod = _imp("main")


class RealEvent:
    """够 require_whitelist / player_required 用的事件。

    平台名是这轮改动唯一的开关输入，所以它是可切的：
    "webroom" → 位图开关关掉、走网格；"qq" → 保持原样。
    """

    def __init__(self, uid, platform="webroom", text="", name="云无月"):
        self._uid = uid
        self._platform = platform
        self._text = text
        self._name = name
        self.plain = []
        self.images = []

    def get_platform_name(self):
        return self._platform

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return self._name

    def get_message_str(self):
        return self._text

    def get_group_id(self):
        return "room_main"

    def plain_result(self, text):
        self.plain.append(text)
        return text

    def image_result(self, path):
        self.images.append(path)
        return path

    async def send(self, _msg):
        return None


async def make_player(db):
    p = Player(user_id="u_real", user_name="云无月", gold=240000, level_index=22)
    await db.create_player(p)
    p = await db.get_player_by_id("u_real")
    inv = {}
    names = [v.get("name") for v in CM.pills_data.values()][:5]
    names += [v.get("name") for v in CM.exp_pills_data.values()][:4]
    qualities = ["一般", "良好", "极佳"]
    for i, n in enumerate(n for n in names if n):
        for q in qualities[: 1 + i % 3]:
            inv[f"{n}|{q}"] = 1 + i % 6
    p.set_pills_inventory(inv)
    p.fate_points = 186
    p.sect_contribution = 8600
    await db.update_player(p)
    return await db.get_player_by_id("u_real")


def _pill_manager():
    return _imp("core.pill_manager").PillManager  # noqa: F841


def bind_plugin(db, cm):
    """不走 __init__（它要 AstrBot 上下文并会起十几个定时任务），只塞方法体需要的属性。"""
    plug = main_mod.XiuXianPlugin.__new__(main_mod.XiuXianPlugin)
    plug.db = db
    plug.config = {}
    plug.config_manager = cm
    plug.whitelist_groups = []          # _check_access 直接放行
    plug.pill_handler = _imp("handlers.pill_handler").PillHandler(db, cm)
    _ReincMgr = _imp("managers.reincarnation_manager").ReincarnationManager
    plug.reinc_handlers = _imp("handlers.reincarnation_handlers").ReincarnationHandlers(
        db, _ReincMgr(db, cm, None)
    )
    plug.title_mgr = _imp("managers.title_manager").TitleManager(db)
    return plug


async def run_cmd(plug, event, method):
    """执行 main.py 里的真实方法体，返回 (texts, image_paths)。"""
    out = []
    async for r in getattr(plug, method)(event):
        out.append(r)
    return out


async def probe(plug, method, platform, label):
    ev = RealEvent("u_real", platform=platform, text=label)
    await run_cmd(plug, ev, method)
    print(f"  [{platform}] yields={len(ev.plain) + len(ev.images)} "
          f"text={len(ev.plain)} image={len(ev.images)}", flush=True)
    if ev.plain:
        head = ev.plain[0].splitlines()[0] if ev.plain[0] else ""
        print(f"  [{platform}] 文本首行: {head[:60]}", flush=True)
    if ev.images:
        p = Path(ev.images[0])
        print(f"  [{platform}] 卡片: {p.name} jpg存在={p.exists()}", flush=True)
    return ev


def card_from(path):
    card = load_card_for_image(str(path))
    if card:
        card["ts"] = int(time.time() * 1000)
        card["owner"] = "云无月"
    return card


async def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "pill"
    no_browser = "--no-browser" in sys.argv
    scen = {
        "pill": ("handle_show_pills", "/丹药背包"),
        "fate": ("handle_fate_shop", "/仙缘商店"),
        "title": ("handle_title_collection", "/称号图鉴"),
    }[key]
    method, cmd = scen

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    Path(path).unlink(missing_ok=True)
    db = DB_CLS(path)
    await db.connect()
    await _imp("data").MigrationManager(db.conn, CM).migrate()
    await db.ext.ensure_system_config_table()
    player = await make_player(db)
    plug = bind_plugin(db, CM)
    print(f"[{cmd}] 玩家 {player.user_name} 丹药 {len(player.get_pills_inventory())} 种", flush=True)

    print("== 真实链路 · 网页端 ==", flush=True)
    web_ev = await probe(plug, method, "webroom", cmd)
    print("== 真实链路 · QQ 端对照 ==", flush=True)
    qq_ev = await probe(plug, method, "qq", cmd)

    if web_ev.images and not web_ev.plain:
        print("  ✅ 网页端只出卡片、无纯文本", flush=True)
    if qq_ev.plain and not qq_ev.images:
        print("  ✅ QQ 端只出文本、无卡片", flush=True)

    if not web_ev.images:
        print("  ❌ 网页端没有产出卡片，后续截图跳过", flush=True)
        await db.close()
        Path(path).unlink(missing_ok=True)
        return

    card = card_from(web_ev.images[0])
    print(f"  sidecar 读回: title={card and card.get('title')} "
          f"sections={len(card.get('sections', [])) if card else 0}", flush=True)
    if not card:
        await db.close()
        Path(path).unlink(missing_ok=True)
        return
    sec = (card.get("sections") or [{}])[0]
    print(f"  view={sec.get('view')} entries={len(sec.get('entries') or [])}", flush=True)

    if no_browser:
        await db.close()
        Path(path).unlink(missing_ok=True)
        return

    import shot

    class Adapter:
        """本脚本直接调 handler，玩家在页面里发的消息一律不理，避免刷占位文本。"""

        async def on_player_message(self, room_id, player_id, player_name, text,
                                    req_id=None, private=False, want_card=False, **kw):
            return None

    room_server.set_adapter(Adapter())
    await room_server.start("127.0.0.1", 6219)
    accounts.register("真实演示", "test123456", "真实演示")
    auth = {"token": accounts.make_token("真实演示"),
            "username": "真实演示", "nick": "真实演示"}
    proc = shot.start_chrome(1440, 900)
    try:
        shot.wait_devtools()
        page = shot.open_page(shot.URL_BASE)
        await asyncio.sleep(1.2)
        async with shot.CDP(page["webSocketDebuggerUrl"]) as c:
            await c.cmd("Page.enable")
            await c.cmd("Runtime.enable")
            auth_js = json.dumps(json.dumps(auth, ensure_ascii=False))
            await c.js(f"localStorage.clear();localStorage.setItem('xiuxian_auth_v1', {auth_js})")
            await c.cmd("Page.reload")
            await asyncio.sleep(3.0)
            await room_server.broadcast_bot_payload(
                "room_main",
                {"text": "", "cards": [card], "ts": int(time.time() * 1000),
                 "type": "bot", "channel": "manual", "audience": "public",
                 "actor": "云无月"},
            )
            await asyncio.sleep(1.6)
            print("卡片数:", await c.js("document.querySelectorAll('.card').length"), flush=True)
            print("页面错误:", await c.js("JSON.stringify(window.__errs||[])"))
            print("标题:", await c.js(
                "(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                "return C?C.querySelector('.card-title').textContent:'-'})()"))
            print("格子:", await c.js(
                "(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                "return C?C.querySelectorAll('.gcell').length:'-'})()"))
            print("筛选条:", await c.js(
                "(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                "return [...C.querySelectorAll('.eg-facets')].map(r=>"
                "r.innerText.replace(/\\n/g,' ')).join(' || ')||'-'})()"))
            print("汇总:", await c.js(
                "(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                "const s=C.querySelector('.eg-sum');return s?s.innerText:'-'})()"))
            await c.shot(str(OUT / f"real_{key}.png"))
            await c.js("(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                       "const e=[...C.querySelectorAll('.gcell')].find(x=>x.offsetParent!==null);"
                       "if(e)e.click()})()")
            await asyncio.sleep(0.8)
            print("详情:", await c.js(
                "(()=>{const d=document.querySelector('.ed-drawer .ed-title');"
                "return d?d.textContent:'-'})()"))
            await c.shot(str(OUT / f"real_{key}_详情.png"))
    finally:
        proc.terminate()
        await room_server.stop()
        await db.close()
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
