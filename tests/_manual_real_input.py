"""真实输入链路验收：页面里打字 → 服务 → 插件方法 → 推回页面。

`_manual_real_shot.py` 已经覆盖了「handler 产出什么」，但它把卡片**手工 broadcast**
出去，页面输入框那一环是空的。这个脚本把最后一环补齐：

    Chromium 输入框打字（#inMsg）→ 点发送（#btnSend）
      → room_server 收到 /api/send（真实 HTTP + WS 鉴权）
      → Router.on_player_message(room_id, "web_真实演示", ...)  ← 真实适配器回调签名
      → COMMAND_REGISTRY 查命令（来自 main.py 真实 @filter.command）
      → main.py 里的真实方法体（@require_whitelist 按平台名真去关位图开关）
      → handler + sqlite → render_web_card 写 sidecar
      → load_card_for_image 读回 → room_server 广播
      → 页面真的把卡片画出来

用法：
    python tests/_manual_real_input.py pill|sect|fate|market|black|title
    python tests/_manual_real_input.py pill --no-browser   # 只验输入链路，不开浏览器
"""

import asyncio
import inspect
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLUG = TESTS.parent
ROOT = PLUG.parent
for p in (str(TESTS), str(PLUG), str(ROOT), str(ROOT / "astrbot")):
    if p not in sys.path:
        sys.path.insert(0, p)

import _manual_real_shot as R  # noqa: E402  （它负责装 astrbot 桩、导 main）

room_server = R.room_server
accounts = R.accounts
load_card_for_image = R.load_card_for_image

ACCOUNT = "真实演示"
PASSWORD = "test123456"
NICK = "真实演示"
PLAYER_ID = f"web_{ACCOUNT}"
PORT = 6221


# ---------- 数据 ----------

SCENES = {
    "pill": "丹药背包",
    "sect": "宗门商店",
    "fate": "仙缘商店",
    "fate_ex": "仙缘兑换 灵石 10",
    "market": "坊市",
    "black": "黑市",
    "title": "称号图鉴",
}


async def make_player(db, cm):
    """在建玩家时就把各场景要的数据一次备齐，六条命令共用。"""
    P = R.Player
    p = P(user_id=PLAYER_ID, user_name=NICK, gold=240000, level_index=22)
    await db.create_player(p)
    p = await db.get_player_by_id(PLAYER_ID)

    inv = {}
    names = [v.get("name") for v in cm.pills_data.values()][:5]
    names += [v.get("name") for v in cm.exp_pills_data.values()][:4]
    for i, n in enumerate(n for n in names if n):
        for q in ["一般", "良好", "极佳"][: 1 + i % 3]:
            inv[f"{n}|{q}"] = 1 + i % 6
    p.set_pills_inventory(inv)

    p.fate_points = 186
    p.sect_contribution = 8600
    p.title = "初入仙途"
    # 已收集称号记在 permanent_bonus["titles"]（TitleManager._load_owned 读这里）
    try:
        bonus = json.loads(p.permanent_bonus) if p.permanent_bonus else {}
    except Exception:
        bonus = {}
    if not isinstance(bonus, dict):
        bonus = {}
    bonus["titles"] = {t: time.time() for t in ("初入仙途", "筑基之姿", "结丹有望")}
    p.permanent_bonus = json.dumps(bonus, ensure_ascii=False)
    await db.update_player(p)
    return await db.get_player_by_id(PLAYER_ID)


def wire(plug, db):
    """把六条命令各自依赖的 handler/manager 按 main.py 的真实构造方式挂上。

    main.py 的 __init__ 会起十几个定时任务，不能直接跑；但它挂的每个组件
    都是独立可构造的，这里照抄构造参数即可。
    """
    ring = R._imp("core.storage_ring_manager").StorageRingManager(db, R.CM)
    shop = R._imp("handlers.shop_handler").ShopHandler(db, plug.config, R.CM)
    sect_mgr = R._imp("managers.sect_manager").SectManager(db, R.CM)
    plug.storage_ring_mgr = ring
    plug.sect_mgr = sect_mgr
    plug.sect_shop_handler = R._imp("handlers.sect_shop_handler").SectShopHandler(
        db, shop.shop_manager, ring, plug.config, sect_mgr
    )
    gamble_mgr = R._imp("managers.gamble_manager").GambleManager(db, R.CM, ring)
    plug.gamble_mgr = gamble_mgr
    plug.gamble_handlers = R._imp("handlers.gamble_handlers").GambleHandlers(db, gamble_mgr)
    auction_mgr = R._imp("managers.auction_manager").AuctionManager(db, ring, plug.config)
    plug.auction_mgr = auction_mgr
    plug.auction_handlers = R._imp("handlers.auction_handlers").AuctionHandlers(db, auction_mgr)
    # 页面登录成功后会自己发一条 /我的信息 拉看板
    plug.player_handler = R._imp("handlers.player_handler").PlayerHandler(
        db, plug.config, R.CM
    )
    return auction_mgr, sect_mgr


async def prepare_scene(key, db, player, sect_mgr, auction_mgr):
    """只有宗门/坊市需要额外造场景数据，其余命令用通用玩家即可。"""
    if key == "sect":
        ok, msg = await sect_mgr.create_sect(PLAYER_ID, "青云宗")
        print(f"  建宗: {ok} {msg}", flush=True)
        player = await db.get_player_by_id(PLAYER_ID)
        await db.update_player(player)
    elif key == "market":
        inv = player.get_pills_inventory()
        n = 0
        for pill_key, cnt in list(inv.items())[:6]:
            name, _, quality = pill_key.partition("|")
            ok, msg = await auction_mgr.list_item(
                player, name, 1, 600 + 120 * n, quality or ""
            )
            if ok:
                n += 1
            else:
                print(f"  挂单失败 {pill_key}: {msg}", flush=True)
        print(f"  坊市挂单: {n} 件", flush=True)
    elif key == "fate_ex":
        # 灵石换点要够钱：建号时只有 24 万，这里补到 1.2 亿（10 点 = 1000 万/点）
        player.gold = 120_000_000
        await db.update_player(player)
        player = await db.get_player_by_id(PLAYER_ID)
        print(f"  备好灵石: {int(player.gold):,}", flush=True)
    return player


# ---------- 命令路由 ----------

def build_router_table():
    """"命令名 → main.py 里的真实方法"，直接从 @filter.command 里取，不手写。"""
    table = {}
    for args, fn in R.COMMAND_REGISTRY:
        for a in args:
            if isinstance(a, str) and a:
                table.setdefault(a, fn)
    return table


ROUTES = build_router_table()


def resolve(text):
    body = (text or "").strip().lstrip("/／").strip()
    if not body:
        return None, ""
    head, _, rest = body.partition(" ")
    return ROUTES.get(head), rest.strip()


def wants_args(fn) -> bool:
    """handler 是否是「带参命令」（self, event, ...）。

    @require_whitelist 用 functools.wraps 包了一层，signature 会顺着
    __wrapped__ 摸回原函数，所以数一下位置参数个数就够了。
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = [
        p for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 3


def split_args(fn, rest: str) -> list:
    """按真 astrbot 的规则把命令行尾巴拆成实参。

    两种形态：
    - ``GreedyStr``：整段原样给一个形参（``/服用丹药 凝气丹 一般 1``）；
    - 普通 ``str`` 形参：按空格逐个拆（``/仙缘兑换 灵石 10`` →
      ``item_id="灵石", count="10"``），多出来的词并进最后一个形参。

    桩以前一律把整段塞给第一个形参，于是「两个形参」的命令（仙缘兑换）
    永远只收到 ``"灵石 10"`` 这一个字符串，被当成品名报错——
    是桩失真，不是产品 bug。
    """
    if not rest:
        # 形参没有默认值时，真 astrbot 会传空串（GreedyStr 尤其如此），
        # 否则 `/飞升` 这类无参命令会 TypeError: missing 'args'
        try:
            params = [
                p for p in inspect.signature(fn).parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            extra = params[2:]
        except (TypeError, ValueError):
            return []
        if extra and extra[0].default is inspect.Parameter.empty:
            return [""]
        return []
    try:
        params = [
            p for p in inspect.signature(fn).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        return [rest]
    extra = params[2:]  # 去掉 self / event
    if not extra:
        return []

    ann = extra[0].annotation
    ann_name = getattr(ann, "__name__", "") or str(ann)
    if "GreedyStr" in ann_name:
        return [rest]

    tokens = rest.split()
    if len(tokens) <= len(extra):
        return tokens
    head = tokens[: len(extra) - 1]
    return head + [" ".join(tokens[len(extra) - 1:])]


def result_to_payload(ev):
    """把一次方法调用的产出转成 room_server 能广播的 payload。"""
    cards = []
    for p in ev.images:
        card = load_card_for_image(str(p))
        if card:
            card["ts"] = int(time.time() * 1000)
            cards.append(card)
    return {
        "text": "\n".join(ev.plain),
        "images": [str(p) for p in ev.images],
        "cards": cards,
        "ts": int(time.time() * 1000),
        "type": "bot",
        "actor": "天道",
    }


# ---------- 适配器 ----------

class Router:
    """替身适配器：签名与 WebroomAdapter.on_player_message 一致，收到消息就真派发。

    真实 astrbot 由 pipeline 完成这一步（按 @filter.command 匹配、构造事件、
    把 MessageEventResult 送回平台）。这里没装 astrbot 本体，所以手工复刻，
    但**方法体、装饰器、开关、handler、sqlite、sidecar 全是真的**，
    唯一替身是事件对象（WebroomMessageEvent 依赖 astrbot 的 AstrMessageEvent 基类）。
    """

    def __init__(self, plug):
        self.plug = plug
        self.log = []
        self.last_text = ""

    async def on_player_message(self, room_id, player_id, player_name, text,
                                req_id=None, private=False, want_card=False,
                                panel=False, audience="public", **kw):
        fn, rest = resolve(text)
        if fn is None:
            self.log.append((text, "未注册命令", 0))
            await room_server.broadcast_bot_payload(
                room_id, {"text": f"没有这条指令：{text}", "ts": int(time.time() * 1000)}
            )
            return
        ev = R.RealEvent(player_id, platform="webroom", text=text, name=player_name)
        # 真实事件层（webroom_event._deliver）靠这两点决定「回给谁、带不带 req_id」：
        # 看板/私密请求一律走定向 reply，前端 settlePending(req_id) 才拿得到 ctx.panel，
        # 卡片才会归位到右侧看板；少了 req_id 就会退化成聊天流里的普通卡片。
        ev.req_id = req_id or None
        ev.panel = bool(panel)
        private = bool(private or panel)
        try:
            # 方法是从类体上取的未绑定函数，要显式传 self。
            # 带参命令按真 astrbot 的规则拆参：GreedyStr 吃整段，普通形参按空格拆。
            argv = [self.plug, ev]
            argv.extend(split_args(fn, rest))
            async for _ in fn(*argv):
                pass
            payload = result_to_payload(ev)
        except Exception:
            import traceback
            traceback.print_exc()
            raise
        detail = []
        for card in payload["cards"]:
            for sec in card.get("sections") or []:
                if sec.get("view") == "grid":
                    names = [e.get("name") for e in (sec.get("entries") or [])]
                    detail.append(f"{sec.get('name')}×{len(names)}:{names}")
        self.log.append((text, fn.__name__, len(payload["cards"]), detail))
        self.last_text = payload.get("text", "")
        payload["req_id"] = req_id or ""
        if private:
            # 与 webroom_event._deliver 一致：看板结果只回给本人，绝不走公屏
            payload["type"] = "reply"
            payload["audience"] = "self"
            await room_server.send_to_player(room_id, player_id, payload)
        else:
            await room_server.broadcast_bot_payload(room_id, payload)
        # 真实事件层在 _deliver 末尾会补一次看板变更检查（储物戒 / 丹药背包各算
        # 一份指纹，变了就推 bag_changed / pillbag_changed）。桩绕过了事件层，
        # 这里照做，否则「服用后看板自动刷新」这条链路根本没被触发。
        room_server.schedule_bag_check(room_id, player_id, ACCOUNT)


# ---------- 浏览器侧 ----------

LOGIN_JS = """
(async () => {
  const set = (id, v) => {
    const el = document.getElementById(id);
    el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
  };
  // 冷启动时 app.js 可能还没绑定点击事件，先等按钮就位再点
  for (let i = 0; i < 50 && !document.getElementById('btnAuth'); i++) {
    await new Promise(r => setTimeout(r, 100));
  }
  set('inUser', %s);
  set('inPwd', %s);
  document.getElementById('btnAuth').click();
  // 登录后还要等 WS 握手 + 首屏 /我的信息，轮询到主界面出现为止
  for (let i = 0; i < 100; i++) {
    const app = document.getElementById('app');
    if (app && !app.hidden) return true;
    await new Promise(r => setTimeout(r, 100));
  }
  return false;
})()
"""

SEND_JS = """
(() => {
  const el = document.getElementById('inMsg');
  el.value = %s;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  document.getElementById('btnSend').click();
  return el.value;
})()
"""

COUNT_JS = """
(() => {
  const cc = document.querySelectorAll('.card');
  const C = cc[cc.length - 1];
  if (!C) return JSON.stringify({cards: cc.length});
  const f = [...C.querySelectorAll('.eg-facets')].map(r => r.innerText.replace(/\\n/g, ' '));
  const s = C.querySelector('.eg-sum');
  return JSON.stringify({
    cards: cc.length,
    title: (C.querySelector('.card-title') || {}).textContent || '',
    cells: C.querySelectorAll('.gcell').length,
    facets: f,
    sum: s ? s.innerText : '',
  });
})()
"""


# /丹药背包 走右侧看板（v3.45.19 的 pillbag 页），不在聊天流里出 .gcell 卡片，
# 所以它要另外一套统计：看 #viewPillBag 里的 .icell 与筛选条。
PANEL_JS = """
(() => {
  const v = document.getElementById('viewPillBag');
  if (!v) return JSON.stringify({panel: 'missing'});
  const cells = v.querySelectorAll('.icell');
  const tab = document.querySelector('.panel-tab.active');
  return JSON.stringify({
    panel: 'pillbag',
    tab: tab ? tab.textContent.trim() : '',
    cells: cells.length,
    first: cells.length ? cells[0].innerText.replace(/\\n+/g, ' ') : '',
    filters: [...v.querySelectorAll('.item-filters .fchip')].map(x => x.textContent.trim()),
    tools: [...v.querySelectorAll('.item-filters .fitem, .item-filters button')]
      .map(x => x.textContent.trim()).slice(0, 8),
  });
})()
"""


# /仙缘商店 同样只看板：数据来自 /api/fate_shop，看板里是一组组小卡片 + 层数晶块
FATE_JS = """
(() => {
  const v = document.getElementById('viewFate');
  if (!v) return JSON.stringify({panel: 'missing'});
  const tab = document.querySelector('.panel-tab.active');
  const cards = [...v.querySelectorAll('.fate-card')];
  const first = cards[0];
  const pips = first ? [...first.querySelectorAll('.fp-pips > i')] : [];
  return JSON.stringify({
    panel: 'fate',
    tab: tab ? tab.textContent.trim() : '',
    groups: v.querySelectorAll('.fate-group').length,
    cards: cards.length,
    pips: pips.length,
    pipsOn: pips.filter(x => x.classList.contains('on')).length,
    first: first ? first.innerText.replace(/\\n+/g, ' ') : '',
    points: (v.querySelector('.fate-points') || {}).innerText || '',
  });
})()
"""


async def run(key, no_browser):
    cmd = SCENES[key]
    fd, dbpath = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    Path(dbpath).unlink(missing_ok=True)

    DB = R.DB_CLS
    db = DB(dbpath)
    await db.connect()
    await R._imp("data").MigrationManager(db.conn, R.CM).migrate()
    await db.ext.ensure_system_config_table()
    player = await make_player(db, R.CM)
    plug = R.bind_plugin(db, R.CM)
    am, sm = wire(plug, db)
    player = await prepare_scene(key, db, player, sm, am)
    print(f"[{cmd}] 玩家 {PLAYER_ID} 已建；已注册命令 {len(ROUTES)} 条", flush=True)

    router = Router(plug)
    room_server.set_adapter(router)
    # 看板变更推送（bag_changed / pillbag_changed）要读存档算指纹，
    # 生产环境 main.py 的 initialize 里会注入；这里同样补上。
    room_server.set_plugin(plug)

    # 记录后端到底推了哪些事件，便于验证「服用后自动刷新」这条链路
    pushed = []
    _orig_push = room_server.push_to_username

    async def _spy_push(room_id, username, payload):
        n = await _orig_push(room_id, username, payload)
        pushed.append((payload.get("type"), n))
        return n

    room_server.push_to_username = _spy_push

    # 先离线跑一遍，确认这条命令能被解析（不开服务也能看到）
    fn, _ = resolve(cmd)
    print(f"  路由: {cmd} -> {fn.__name__ if fn else '未找到'}", flush=True)
    if fn is None:
        await db.close()
        Path(dbpath).unlink(missing_ok=True)
        return

    if no_browser:
        await router.on_player_message("room_main", PLAYER_ID, NICK, f"/{cmd}")
        print("  离线派发结果:", router.log, flush=True)
        print("  回复文本:", (router.last_text or "(空)").replace("\n", " / ")[:160], flush=True)
        await db.close()
        Path(dbpath).unlink(missing_ok=True)
        return

    import shot

    await room_server.start("127.0.0.1", PORT)
    shot.URL_BASE = f"http://127.0.0.1:{PORT}/"
    accounts.register(ACCOUNT, PASSWORD, NICK)
    # 视口可用环境变量覆盖：手机端（SHOT_W=390 SHOT_H=844）才能暴露
    # 「菜单贴着屏幕底边 → 按钮被裁掉」这类只在窄屏出现的问题
    proc = shot.start_chrome(
        int(os.environ.get("SHOT_W", 1440)), int(os.environ.get("SHOT_H", 900))
    )
    try:
        shot.wait_devtools()
        page = shot.open_page(shot.URL_BASE)
        await asyncio.sleep(1.2)
        async with shot.CDP(page["webSocketDebuggerUrl"]) as c:
            await c.cmd("Page.enable")
            await c.cmd("Runtime.enable")
            ok = False
            for attempt in range(3):
                # LOGIN_JS 是 async IIFE，必须等 Promise，否则拿到的是 undefined
                ok = await c.js(
                    LOGIN_JS % (json.dumps(ACCOUNT), json.dumps(PASSWORD)),
                    await_promise=True,
                )
                if ok:
                    break
                await asyncio.sleep(1.0)
            print(f"  登录: {'进入主界面' if ok else '失败'}", flush=True)
            await asyncio.sleep(1.5)
            await c.js(SEND_JS % json.dumps(f"/{cmd}"))
            await asyncio.sleep(2.2)
            stat_js = {"pill": PANEL_JS, "fate": FATE_JS}.get(key, COUNT_JS)
            stat = json.loads(await c.js(stat_js) or "{}")
            print("  页面:", stat, flush=True)
            if key == "pill":
                # 验收：看板页签得是「丹药背包」，且格子真的画出来了
                good = stat.get("tab") == "丹药背包" and stat.get("cells", 0) > 0
                print(f"  看板归位: {'✅' if good else '❌'} "
                      f"{stat.get('cells', 0)} 格", flush=True)
            if key == "fate":
                # 验收：52 件商品按 8 组进看板，聊天流里不该再有格子墙
                good = (
                    stat.get("tab") == "仙缘"
                    and stat.get("cards", 0) > 0
                    and stat.get("groups", 0) > 0
                )
                print(f"  看板归位: {'✅' if good else '❌'} "
                      f"{stat.get('groups', 0)} 组 / {stat.get('cards', 0)} 张卡片", flush=True)
                print(f"  晶块: {stat.get('pipsOn', 0)}/{stat.get('pips', 0)} 已点亮", flush=True)
                # 点第一张卡片：菜单里必须同时有说明和「购买一层」入口
                await c.js("(()=>{const v=document.getElementById('viewFate');"
                           "const e=v&&[...v.querySelectorAll('.fate-card')]"
                           ".find(x=>x.getClientRects().length);"
                           "if(e)e.click()})()")
                await asyncio.sleep(0.8)
                print("  卡片菜单:", await c.js(
                    "(()=>{const ms=[...document.querySelectorAll('div')]"
                    ".filter(x=>x.style&&x.style.zIndex==='70'&&x.getClientRects().length);"
                    "const m=ms[ms.length-1];"
                    "if(!m)return '-';"
                    "const r=m.getBoundingClientRect();"
                    "return JSON.stringify({text:m.innerText.replace(/\\n+/g,' / '),"
                    "btns:m.querySelectorAll('button').length,"
                    "top:Math.round(r.top),h:Math.round(r.height),"
                    "vw:innerWidth,vh:innerHeight,"
                    "inView:r.top>=0&&r.bottom<=innerHeight})})()"), flush=True)
            print("  服务端派发:", router.log, flush=True)
            # 防回归：页面汇总的「共 N 条」必须等于后端给出的条目数。
            # v3.45.22 的 bug（默认落凡品）就是这两个数对不上——宗门 5 件只画 2 格。
            m = re.search(r"共\s*(\d+)\s*条", stat.get("sum", ""))
            back = re.search(r"×(\d+):", (router.log[-1][3] or [""])[0]) if router.log else None
            if m and back:
                ok_sum = int(m.group(1)) == int(back.group(1))
                print(f"  总数一致: {'✅' if ok_sum else '❌'} "
                      f"页面 {m.group(1)} / 后端 {back.group(1)}", flush=True)
            print("  页面错误:", await c.js("JSON.stringify(window.__errs||[])"))
            await c.shot(str(R.OUT / f"input_{key}.png"))
            if key == "pill":
                # 看板格子点了是弹操作菜单（服用/详情/回收），不是详情抽屉
                await c.js("(()=>{const v=document.getElementById('viewPillBag');"
                           "const e=v&&[...v.querySelectorAll('.icell')]"
                           ".find(x=>x.getClientRects().length);"
                           "if(e)e.click()})()")
                await asyncio.sleep(0.8)
                # 菜单是 position:fixed，offsetParent 恒为 null，只能用 getClientRects 判在不在；
                # 顺带避开「选择器里再套引号」导致 JS 截串的问题，直接比 style.zIndex
                print("  格子菜单:", await c.js(
                    "(()=>{const ms=[...document.querySelectorAll('div')]"
                    ".filter(x=>x.style&&x.style.zIndex==='70'&&x.getClientRects().length);"
                    "const m=ms[ms.length-1];"
                    "return m?m.innerText.replace(/\\n+/g,' / '):'-'})()"), flush=True)

                # 服丹后看板要自己刷新。这里由桩直接提交命令（等价于玩家在聊天框
                # 敲了一条 /服用丹药），浏览器全程停在丹药背包页不切走：
                # 命令回复照旧进聊天流，看板只靠后端推的 pillbag_changed 自己重画。
                before = stat.get("first", "")
                await router.on_player_message(
                    "room_main", PLAYER_ID, NICK, "/服用丹药 凝气丹 一般 1"
                )
                await asyncio.sleep(2.5)
                after = json.loads(await c.js(PANEL_JS) or "{}")
                used = [r for r in router.log if r[1] == "handle_use_pill"]
                print(f"  服用: {used[-1][1] if used else '未执行'}", flush=True)
                lines = (router.last_text or "").splitlines()
                print(f"  回复: {lines[0][:60] if lines else '(卡片)'}", flush=True)
                p_after = await db.get_player_by_id(PLAYER_ID)
                inv = {
                    k: v for k, v in (p_after.get_pills_inventory() or {}).items()
                    if "凝气丹" in k
                }
                print(f"  存档丹药: {inv}", flush=True)
                print(f"  后端推送: {pushed}", flush=True)
                print(f"  服用前: {before}", flush=True)
                print(f"  服用后: {after.get('first', '')}", flush=True)
                refreshed = after.get("first", "") != before
                print(f"  自动刷新: {'✅' if refreshed else '❌ 页面没变'}", flush=True)
            elif key == "fate":
                # 点一张还买得起的商品：应当弹「说明 + 购买一层」菜单，而不是点一下就扣点
                await c.js("(()=>{const v=document.getElementById('viewFate');"
                           "const e=v&&[...v.querySelectorAll('.fate-card:not(.maxed)')]"
                           ".find(x=>x.getClientRects().length);"
                           "if(e)e.click()})()")
                await asyncio.sleep(0.7)
                menu_js = (
                    "(()=>{const ms=[...document.querySelectorAll('div')]"
                    ".filter(x=>x.style&&x.style.zIndex==='70'&&x.getClientRects().length);"
                    "const m=ms[ms.length-1];"
                    "return m?m.innerText.replace(/\\n+/g,' / '):'-'})()"
                )
                print("  商品菜单:", await c.js(menu_js), flush=True)
                await c.shot(str(R.OUT / f"input_{key}_菜单.png"))
                # 菜单确认购买一层：晶块要跟着多点亮一颗
                await c.js("(()=>{const ms=[...document.querySelectorAll('div')]"
                           ".filter(x=>x.style&&x.style.zIndex==='70'&&x.getClientRects().length);"
                           "const m=ms[ms.length-1];const b=m&&m.querySelector('button');"
                           "if(b)b.click()})()")
                await asyncio.sleep(2.0)
                after = json.loads(await c.js(FATE_JS) or "{}")
                print(f"  购买前: {stat.get('first', '')} 晶块 {stat.get('pipsOn', 0)}/{stat.get('pips', 0)}",
                      flush=True)
                print(f"  购买后: {after.get('first', '')} 晶块 {after.get('pipsOn', 0)}/{after.get('pips', 0)}",
                      flush=True)
                bought = after.get("pipsOn", 0) > stat.get("pipsOn", 0)
                print(f"  晶块增加: {'✅' if bought else '❌ 没变'}", flush=True)
            else:
                await c.js("(()=>{const cc=document.querySelectorAll('.card');"
                           "const C=cc[cc.length-1];if(!C)return;"
                           "const e=[...C.querySelectorAll('.gcell')].find(x=>x.offsetParent!==null);"
                           "if(e)e.click()})()")
                await asyncio.sleep(0.8)
                print("  详情:", await c.js(
                    "(()=>{const d=document.querySelector('.ed-drawer .ed-title');"
                    "return d?d.textContent:'-'})()"))
            await c.shot(str(R.OUT / f"input_{key}_详情.png"))
    finally:
        proc.terminate()
        await room_server.stop()
        await db.close()
        Path(dbpath).unlink(missing_ok=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    which = args[0] if args else "pill"
    asyncio.run(run(which, "--no-browser" in sys.argv))
