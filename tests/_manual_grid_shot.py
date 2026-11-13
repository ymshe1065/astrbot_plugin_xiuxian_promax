# -*- coding: utf-8 -*-
"""临时验收：丹方 / 符箓书 / 丹阁 的网格化显示（真实管理器产物 + 真实前端）。

跑法：python tests/_manual_grid_shot.py [recipe|talisman|shop|book]
产物：_grid_shot/*.png（默认写到插件根目录下的 _grid_shot/）

不是 unittest 用例：它要起真实 room_server + Chromium 打 CDP 截图，
所以文件名以下划线开头，`unittest discover -p "test_*.py"` 不会收它。
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 两种放置方式都认：留在仓库根的临时脚本 / 收进 tests/ 的常驻工具
PLUG = _HERE if (_HERE / "metadata.yaml").exists() else _HERE.parent
ROOT = PLUG.parent
sys.path.insert(0, str(PLUG))
sys.path.insert(0, str(PLUG / "scripts"))
sys.path.insert(0, str(ROOT / "astrbot"))
os.environ["XIU_BASE"] = "http://127.0.0.1:6217"
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)

import dev_webroom  # noqa: E402

dev_webroom._install_astrbot_stubs()
_api = sys.modules["astrbot.api"]
_api.AstrBotConfig = dict
_ev = __import__("types").ModuleType("astrbot.api.event")
_ev.AstrMessageEvent = object
_api.event = _ev
sys.modules.setdefault("astrbot.api.event", _ev)
_all = __import__("types").ModuleType("astrbot.api.all")
_all.At = object
_all.Plain = object
_api.all = _all
sys.modules.setdefault("astrbot.api.all", _all)
sys.path.insert(0, str(ROOT))

import shot  # noqa: E402
from astrbot_plugin_xiuxian_promax.webchat_room import accounts, room_server  # noqa: E402

PKG = "astrbot_plugin_xiuxian_promax"
OUT = ROOT / "_grid_shot"
OUT.mkdir(exist_ok=True)


def _imp(p):
    return __import__(f"{PKG}.{p}", fromlist=["x"])


CM = _imp("config_manager").ConfigManager(PLUG)
Player = _imp("models").Player


class _FakeDB:
    def __init__(self, player):
        self.player = player

    async def get_player_by_id(self, uid):
        return self.player if uid == self.player.user_id else None

    async def update_player(self, player, commit=True):
        return True


class _FakeRing:
    """够用的储物戒替身：材料给足，格位给足。"""

    STACK = 50

    def __init__(self, owned):
        self.owned = owned

    def get_available_slots(self, player):
        return 80

    def room_for(self, owned, available):
        return max(0, int(available) * self.STACK)

    def get_item_count(self, player, name):
        return int(self.owned.get(name, 0))

    def ring_items(self, player):
        return dict(self.owned)


def _make_player(recipe_ids, gold=24000, level=6, owned=None):
    return Player(
        user_id="u1", user_name="云无月", gold=gold, level_index=level,
        learned_recipes=json.dumps(recipe_ids),
    )


def _catalog_owned(buckets, count=20):
    """按「品阶×类别」给材料：炼丹/制符的库存判定走 catalog 的真实材料名，
    乱编名字（如「凡品灵植」）只会得到 0，全都变成缺料。"""
    catalog = json.loads((PLUG / "config" / "material_catalog.json").read_text(encoding="utf-8"))
    rows = catalog["materials"] if isinstance(catalog, dict) else catalog
    owned, seen = {}, {}
    for row in rows:
        key = (row.get("tier"), row.get("category"))
        if key not in buckets:
            continue
        if seen.get(key, 0) >= 6:
            continue
        owned[row["name"]] = count
        seen[key] = seen.get(key, 0) + 1
    return owned


async def build_recipe_card():
    recipes = json.loads((PLUG / "config" / "pill_recipes.json").read_text(encoding="utf-8"))
    ids = [r["id"] for r in recipes][:18]
    player = _make_player(ids, gold=24000, level=6)
    owned = _catalog_owned({
        ("凡品", "灵植"), ("凡品", "妖兽"), ("凡品", "矿材"), ("凡品", "灵液"),
        ("黄品", "灵植"), ("黄品", "妖兽"), ("黄品", "矿材"), ("黄品", "灵液"),
        ("灵品", "灵植"), ("灵品", "妖兽"), ("灵品", "矿材"), ("灵品", "灵液"),
        ("地品", "灵植"), ("地品", "妖兽"), ("地品", "矿材"), ("地品", "灵液"),
    })
    mgr = _imp("managers.alchemy_manager").AlchemyManager(
        _FakeDB(player), CM, _FakeRing(owned)
    )
    ok, _msg, rows, card_lines, entries = await mgr.get_available_recipes("u1")
    print(f"[丹方] ok={ok} rows={len(rows)} entries={len(entries)}")
    craftable = sum(1 for e in entries if e["state"] == "ok")
    return {
        "kind": "craft",
        "title": "云无月 的已学丹方",
        "subtitle": f"共 {len(rows)} 个配方 · {craftable} 个可炼",
        "sections": [{
            "name": "已学丹方",
            "view": "grid",
            "lines": card_lines,
            "entries": entries,
        }],
        "footer": "💡 /学习丹方 <ID或名称> · /炼丹 <ID> [火候] [次数|all]",
        "ts": int(time.time()),
    }


def build_talisman_card():
    ring = _FakeRing(_catalog_owned({
        ("凡品", "灵植"), ("凡品", "妖兽"), ("凡品", "矿材"), ("凡品", "灵液"),
        ("黄品", "灵植"), ("黄品", "妖兽"), ("黄品", "矿材"), ("黄品", "灵液"),
    }, count=30))
    mgr = _imp("managers.talisman_manager").TalismanManager(None, CM, ring)
    player = _make_player([], gold=9000, level=8)
    player.life_skill = "符箓师"
    msg, card_lines, craftable, entries = mgr.build_craft_book(player)
    print(f"[符箓书] lines={len(card_lines)} entries={len(entries)} craftable={craftable}")
    if not entries:
        print(msg[:500])
    return {
        "kind": "craft",
        "title": "云无月 的符箓书",
        "subtitle": f"可画 {craftable} 种 · 熟练度 初学",
        "sections": [{
            "name": "已掌握符箓",
            "view": "grid",
            "lines": card_lines,
            "entries": entries,
        }],
        "footer": "💡 /制符 <符名> [数量|all] 绘制 · /符箓 查看图鉴",
        "ts": int(time.time()),
    }


def build_book_card():
    """符箓配方总览：35 张、两段分区、材料「有多少」未知的那种。"""
    mgr = _imp("managers.talisman_manager").TalismanManager(None, CM, _FakeRing({}))
    spec = mgr.build_recipe_page()
    sections = spec.get("web_sections") or spec.get("sections") or []
    n = sum(len(s.get("entries") or []) for s in sections)
    print(f"[符箓配方] sections={len(sections)} entries={n}")
    return {
        "kind": "craft",
        "title": spec["title"],
        "subtitle": spec["subtitle"],
        "sections": sections,
        "footer": spec.get("footer", ""),
        "ts": int(time.time()),
    }


def build_shop_card():
    from_shop = _imp("core.shop_manager").ShopManager({}, CM)
    # 用真实条目：丹方名是《回春散·凡方》这种卷轴名，手抄会抄错
    scrolls = [
        v for v in CM.items_data.values()
        if isinstance(v, dict) and v.get("type") == "丹方" and int(v.get("price") or 0) > 0
    ]
    scrolls.sort(key=lambda v: int(v.get("price") or 0))
    picks = scrolls[:6] + scrolls[len(scrolls) // 2: len(scrolls) // 2 + 3] + scrolls[-3:]
    items = []
    for idx, src in enumerate(picks):
        items.append({
            "name": src["name"],
            "type": "recipe",
            "rank": src.get("rank", "凡品"),
            "price": int(src.get("price") or 0),
            "stock": 3 if idx % 3 == 0 else 1 + idx % 4,
            "discount": (0.9, 0.95, 1.0, 0.88)[idx % 4],
            "data": dict(src),
        })
    entries = from_shop.build_pavilion_entries(items)
    print(f"[丹阁] entries={len(entries)} ranks={sorted({e['rank'] for e in entries})}")
    return {
        "kind": "shop",
        "title": "丹阁 · 丹方区",
        "subtitle": f"共 {len(entries)} 种在售",
        "sections": [{"name": "在售清单", "view": "grid", "lines": [], "entries": entries}],
        "footer": "购买 <物品名> [数量] · 售罄商品不显示",
        "ts": int(time.time()),
    }


CARDS = {}


class Adapter:
    seq = 0

    async def on_player_message(self, room_id, player_id, player_name, text,
                                req_id=None, private=False, want_card=False, **kw):
        cmd = (text or "").strip()
        base = next((v for k, v in CARDS.items() if cmd.startswith(k)), None)
        if base is None:
            # 登录后前端会自动发 /我的信息 之类的指令，别让它挤进卡片列表
            await room_server.broadcast_bot_payload(
                room_id, {"text": f"（演示）忽略 {cmd}", "ts": int(time.time() * 1000)}
            )
            return
        Adapter.seq += 1
        card = dict(base, id=f"griddemo{Adapter.seq}", ts=int(time.time() * 1000) + Adapter.seq)
        print(f"[adapter] {cmd} -> {card['title']} entries="
              f"{sum(len(s.get('entries') or []) for s in card['sections'])}", flush=True)
        await room_server.broadcast_bot_payload(
            room_id, {"text": "", "cards": [card], "ts": card["ts"]}
        )


HOOK_JS = r"""
window.__errs = [];
window.__sent = [];
window.__wsEv = [];
window.addEventListener('error', (e) => window.__errs.push(String(e.message)));
window.addEventListener('unhandledrejection', (e) => window.__errs.push('rej:' + String(e.reason)));
(() => {
  const P = WebSocket.prototype;
  const send = P.send;
  P.send = function (d) {
    window.__wsEv.push('send:' + this.readyState);
    window.__sent.push(String(d).slice(0, 120));
    return send.call(this, d);
  };
  const A = P.addEventListener;
  P.addEventListener = function (t, f) {
    if (t === 'open' || t === 'close' || t === 'error') {
      return A.call(this, t, (ev) => {
        window.__wsEv.push(t + ':' + this.readyState);
        return f.call(this, ev);
      });
    }
    return A.call(this, t, f);
  };
})();
"""


# 桌面 Chrome 模拟不了 «(hover: hover)» 这个能力位（CDP 的 setEmulatedMedia 对它无效），
# 只能在页面脚本之前把 matchMedia 打桩，用来跑触屏分支的代码路径
HOOK_TOUCH_JS = r"""
(() => {
  const raw = window.matchMedia.bind(window);
  window.matchMedia = (q) => {
    const s = String(q);
    if (s.includes('hover') || s.includes('pointer')) {
      return {
        media: s, matches: s.includes('pointer: fine') ? false : false,
        onchange: null, addListener() {}, removeListener() {},
        addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; },
      };
    }
    return raw(q);
  };
})();
"""


async def send_scene(c):
    """从浏览器自己的输入框发指令：另开连接发会被「同账号两处在线」抢走回复。"""
    return await c.js(f"(()=>{{const t=document.getElementById('inMsg');"
                      f"t.value={json.dumps(SCENE)};"
                      f"const v1=t.value;"
                      f"t.dispatchEvent(new Event('input',{{bubbles:true}}));"
                      f"document.getElementById('btnSend').click();"
                      f"return JSON.stringify({{v1,v2:t.value,mask:document.getElementById('sheetMask').hidden,tag:t.tagName,dis:t.disabled}})}})()")


async def last_card(c, expr):
    """只在最后一张卡片里查询：命令是叠加的，querySelector 会命中最早那张。"""
    return await c.js(f"(()=>{{const C=[...document.querySelectorAll('.card')].pop();"
                      f"if(!C)return '-';return {expr}}})()")


async def run_page(c, label):
    print(f"--- {label} ---")
    print("卡片:", await c.js("document.querySelectorAll('.card').length"))
    print("卡片清单:", await c.js(
        "[...document.querySelectorAll('.card')].map(c=>("
        "(c.querySelector('.card-title')||{}).textContent+'/'+c.querySelectorAll('.eg-wrap').length)"
        ").join(' | ')"))
    print("本卡分区:", await last_card(c, "document.querySelectorAll('.eg-wrap').length"))
    print("品阶条:", await last_card(c,
        "[...C.querySelectorAll('.rchip')].map(b=>b.innerText.replace(/\\n/g,'')).join('|')"))
    print("筛选:", await last_card(c,
        "[...C.querySelectorAll('.eg-facets .fchip')].map(b=>b.textContent).join('/')"))
    print("汇总:", await last_card(c,
        "(()=>{const s=C.querySelector('.eg-sum');return s?s.innerText:'-'})()"))
    print("格子数:", await last_card(c, "C.querySelectorAll('.gcell').length"))
    print("列数:", await last_card(c,
        "(()=>{const g=C.querySelector('.eg-grid');"
        "return g?getComputedStyle(g).gridTemplateColumns.split(' ').length:'-'})()"))
    print("格尺寸:", await last_card(c,
        "(()=>{const e=C.querySelector('.gcell');if(!e)return '-';"
        "const r=e.getBoundingClientRect();return Math.round(r.width)+'x'+Math.round(r.height)})()"))
    print("可见格子:", await last_card(c,
        "[...C.querySelectorAll('.gcell')].filter(e=>e.offsetParent!==null).length"))
    print("品阶条折行:", await last_card(c,
        "(()=>{const b=C.querySelector('.eg-rankbar');if(!b)return '-';"
        "const r=b.getBoundingClientRect();"
        "return Math.round(r.height)+'px/'+(b.scrollWidth>b.clientWidth+1?'横滑':'折行')})()"))
    print("⋯ 显示:", await last_card(c,
        "(()=>{const m=C.querySelector('.gcell .more');return m?getComputedStyle(m).display:'-'})()"))


async def open_first_detail(c):
    await c.js("(()=>{const C=[...document.querySelectorAll('.card')].pop();C.querySelector('.gcell').click()})()")
    await asyncio.sleep(0.8)
    print("详情打开:", await c.js("!!document.querySelector('.ed-drawer.on')"))
    print("详情标题:", await c.js(
        "(()=>{const t=document.querySelector('.ed-drawer .ed-title');return t?t.textContent:'-'})()"))
    print("详情小节:", await c.js(
        "[...document.querySelectorAll('.ed-drawer .ed-sec h4')].map(h=>h.textContent).join('/')"))
    print("材料行:", await c.js(
        "[...document.querySelectorAll('.ed-drawer .ed-mat-row')]"
        ".map(r=>r.innerText.replace(/\\n/g,' ')).join(' | ')"))
    print("主按钮:", await c.js(
        "(()=>{const b=document.querySelector('.ed-drawer .primary');"
        "return b?(b.textContent+(b.disabled?'(禁用)':'')):'-'})()"))
    # 数量步进器：默认 1、「最多 N 次」提示、以及「全部 N」快捷
    print("数量控件:", await c.js(
        "(()=>{const d=document.querySelector('.ed-drawer');const q=d.querySelector('.ed-qty');"
        "return q?JSON.stringify({num:d.querySelector('.qnum').value,"
        "hint:d.querySelector('.qhint').textContent,"
        "all:(d.querySelector('.qall')||{}).textContent||'-',"
        "minus:d.querySelector('.qminus').disabled,"
        "plus:d.querySelector('.qplus').disabled}):'无'})()"))
    print("数量控件高:", await c.js(
        "(()=>{const d=document.querySelector('.ed-drawer');const q=d.querySelector('.ed-qty');"
        "if(!q)return '-';const f=d.querySelector('.ed-foot');"
        "return Math.round(q.getBoundingClientRect().height)+' / foot '+Math.round(f.getBoundingClientRect().height)"
        "+' / 抽屉 '+Math.round(d.getBoundingClientRect().height)})()"))
    print("消息数:", await c.js("document.querySelectorAll('.msg').length"))


async def probe_quantity(c):
    """把数量步进器点一圈（加减 / 全部 / 手输），最后点主按钮抓 ws 帧。"""
    await c.js("(()=>{const d=document.querySelector('.ed-drawer');"
               "d.querySelector('.qplus').click();d.querySelector('.qplus').click()})()")
    await asyncio.sleep(0.3)
    print("加两次后:", await c.js(
        "(()=>{const d=document.querySelector('.ed-drawer');"
        "return d.querySelector('.qnum').value+' / '+d.querySelector('.primary').textContent})()"))
    await c.js("(()=>{const d=document.querySelector('.ed-drawer');const a=d.querySelector('.qall');if(a)a.click()})()")
    await asyncio.sleep(0.3)
    print("点全部后:", await c.js(
        "(()=>{const d=document.querySelector('.ed-drawer');"
        "return d.querySelector('.qnum').value+' / '+d.querySelector('.primary').textContent"
        "+' / plus禁用='+d.querySelector('.qplus').disabled})()"))
    await c.js("(()=>{const d=document.querySelector('.ed-drawer');"
               "const q=d.querySelector('.qnum');q.value='2';q.dispatchEvent(new Event('change'))})()")
    await asyncio.sleep(0.3)
    print("手输 2 后:", await c.js(
        "(()=>{const d=document.querySelector('.ed-drawer');"
        "return d.querySelector('.qnum').value+' / '+d.querySelector('.primary').textContent})()"))
    # 点主按钮：抓 ws 帧，确认数量真的拼进了指令
    await c.js(
        "(()=>{const P=WebSocket.prototype;const raw=P.send;window.__sent2=[];"
        "P.send=function(d){try{window.__sent2.push(String(d).slice(0,140))}catch(e){};"
        "return raw.call(this,d)}})()")
    await c.js("(()=>{document.querySelector('.ed-drawer .primary').click()})()")
    await asyncio.sleep(0.8)
    print("ws 帧:", await c.js("JSON.stringify(window.__sent2||[])"))
    print("抽屉已关:", await c.js("!document.querySelector('.ed-drawer.on')"))



_KEY = sys.argv[1] if len(sys.argv) > 1 else "recipe"
# 发送文本带上分区参数：/丹阁 在指令表里 min=1，光发命令会先弹参数层
SCENE, TAG = {
    "recipe": ("/丹方", "1_丹方"),
    "talisman": ("/符箓书", "2_符箓书"),
    "shop": ("/丹阁 丹药", "3_丹阁"),
    "book": ("/符箓配方", "4_符箓配方"),
}[_KEY]


async def main():
    CARDS["/丹方"] = await build_recipe_card()
    CARDS["/符箓书"] = build_talisman_card()
    CARDS["/丹阁"] = build_shop_card()
    CARDS["/符箓配方"] = build_book_card()

    room_server.set_adapter(Adapter())
    await room_server.start("127.0.0.1", 6217)
    accounts.register("网格演示", "test123456", "网格演示")
    print("listening", flush=True)

    auth = {
        "token": accounts.make_token("网格演示"),
        "username": "网格演示",
        "nick": "网格演示",
    }
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
            # 钩子必须抢在 app.js 之前装，否则页面一刷新就丢
            await c.cmd("Page.addScriptToEvaluateOnNewDocument", source=HOOK_JS)
            await c.cmd("Page.reload")
            await asyncio.sleep(3.2)

            print("自检:", await c.js(
                "JSON.stringify({inMsg:!!document.getElementById('inMsg'),"
                "scripts:[...document.scripts].length,"
                "wsEv:window.__wsEv||[],sent:(window.__sent||[]).length,"
                "mask:document.getElementById('sheetMask').hidden,"
                "title:document.getElementById('sheetTitle').textContent})"))
            # 从浏览器自己的输入框发指令：另开连接发会被「同账号两处在线」抢走回复
            print("发送:", await send_scene(c))
            await asyncio.sleep(0.6)
            print("发送后输入框:", await c.js("document.getElementById('inMsg').value"))
            print("ws帧:", await c.js("JSON.stringify(window.__sent||[])"))
            print("提示:", await c.js(
                "(()=>{const t=document.getElementById('toast');"
                "return t&&!t.hidden?t.innerText:'-'})()"))
            await asyncio.sleep(1.6)
            print("输入框:", await c.js("document.getElementById('inMsg').value"))
            print("消息数:", await c.js("document.querySelectorAll('.msg').length"))
            print("最后消息:", await c.js(
                "(()=>{const a=[...document.querySelectorAll('.msg')];"
                "const m=a[a.length-1];return m?m.innerText.slice(0,120):'-'})()"))
            print("页面错误:", await c.js("JSON.stringify(window.__errs||[])"))
            await run_page(c, TAG)
            await c.shot(str(OUT / f"{TAG}.png"))

            before = await c.js("document.querySelectorAll('.msg').length")
            await open_first_detail(c)
            print("点格子后新增消息:", await c.js("document.querySelectorAll('.msg').length") - before)
            await c.shot(str(OUT / f"{TAG}_详情.png"))
            await probe_quantity(c)
            # probe_quantity 点主按钮已经把抽屉关了，这里再兜一次
            await c.js("(()=>{const b=document.querySelector('.ed-drawer .ed-close');if(b)b.click()})()")
            await asyncio.sleep(0.5)

            # 未达境界的条目：抽屉里不该出现数量控件（反正是点不动的）
            await c.js("(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                       "const b=[...C.querySelectorAll('.rchip')].find(x=>x.textContent.includes('圣'));if(b)b.click()})()")
            await asyncio.sleep(0.5)
            await c.js("(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                       "const cell=[...C.querySelectorAll('.gcell')].find(x=>x.querySelector('.st.lock'));if(cell)cell.click()})()")
            await asyncio.sleep(0.7)
            print("锁定条目:", await c.js(
                "(()=>{const d=document.querySelector('.ed-drawer');if(!d)return '无抽屉';"
                "return (d.querySelector('.ed-qty')?'有数量控件(错)':'无数量控件')"
                "+' / '+d.querySelector('.primary').textContent})()"))
            await c.js("(()=>{const d=document.querySelector('.ed-drawer');if(d)d.querySelector('.ed-close').click()})()")
            await asyncio.sleep(0.4)

            if SCENE == "/丹方":
                # 切品阶页签：取「全部」之后的第一个档，别假定这号人一定有黄品
                print("切档目标:", await last_card(c,
                    "[...C.querySelectorAll('.rchip')][2].innerText.replace(/\\n/g,'')"))
                await c.js("(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                           "const b=[...C.querySelectorAll('.rchip')][2];if(b)b.click()})()")
                await asyncio.sleep(0.6)
                print("切档后:", await last_card(c,
                    "C.querySelectorAll('.gcell').length+' 格 / '+C.querySelector('.eg-sum').innerText"))
                await c.shot(str(OUT / "4_切品阶.png"))

                # 「全部」分档纵览
                await c.js("(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                           "const b=[...C.querySelectorAll('.rchip')].find(x=>x.textContent.includes('全部'));if(b)b.click()})()")
                await asyncio.sleep(0.6)
                print("全部模式分组:", await last_card(c, "C.querySelectorAll('.eg-sec').length"))
                print("档位标题:", await last_card(c,
                    "[...C.querySelectorAll('.eg-sec')].map(h=>h.innerText.replace(/\\n/g,' ')).join(' / ')"))
                await c.shot(str(OUT / "5_全部纵览.png"))

                # 状态筛选：可炼制
                await c.js("(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                           "const b=[...C.querySelectorAll('.eg-facets .fchip')].find(x=>x.textContent==='可炼制');if(b)b.click()})()")
                await asyncio.sleep(0.6)
                print("筛可炼制后:", await last_card(c,
                    "(()=>{const all=[...C.querySelectorAll('.gcell')];"
                    "const vis=all.filter(e=>e.offsetParent!==null);"
                    "return vis.length+' 可见 / '+all.length+' 总 / '+C.querySelector('.eg-sum').innerText})()"))
                await c.shot(str(OUT / "6_筛可炼制.png"))
                await c.js("(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                           "C.querySelector('.eg-facets .fchip').click()})()")
                await asyncio.sleep(0.4)

            # 手机视口：先切「能力」（hover:none + 粗指针）再重载，
            # HAS_HOVER 是脚本加载时算死的，不重载测不到触屏分支
            await c.cmd("Emulation.setDeviceMetricsOverride", width=390, height=844,
                        deviceScaleFactor=2, mobile=True)
            await c.cmd("Emulation.setEmulatedMedia", media="", features=[
                {"name": "hover", "value": "none"},
                {"name": "pointer", "value": "coarse"},
            ])
            await c.cmd("Page.addScriptToEvaluateOnNewDocument", source=HOOK_TOUCH_JS)
            await c.cmd("Page.reload")
            await asyncio.sleep(3.4)
            print("触屏能力:", await c.js(
                "JSON.stringify({hover:matchMedia('(hover: hover)').matches,"
                "fine:matchMedia('(pointer: fine)').matches,"
                "has:matchMedia('(hover: hover) and (pointer: fine)').matches,"
                "cards:document.querySelectorAll('.card').length})"))
            # 重载会把历史里的卡片补回来，有了就别再发第二条
            if not await c.js("document.querySelectorAll('.card').length"):
                await send_scene(c)
                await asyncio.sleep(2.2)
            await run_page(c, f"{TAG} mobile")
            await c.shot(str(OUT / f"{TAG}_手机.png"))
            await c.js("(()=>{const C=[...document.querySelectorAll('.card')].pop();C.querySelector('.gcell').click()})()")
            await asyncio.sleep(0.9)
            print("手机详情:", await c.js(
                "(()=>{const d=document.querySelector('.ed-drawer');"
                "return d?Math.round(d.getBoundingClientRect().height)+'px top='+"
                "Math.round(d.getBoundingClientRect().top):'-'})()"))
            print("手机数量控件:", await c.js(
                "(()=>{const d=document.querySelector('.ed-drawer');const q=d.querySelector('.ed-qty');"
                "if(!q)return '无';const f=d.querySelector('.ed-foot');"
                "return Math.round(q.scrollWidth)+'/'+Math.round(q.clientWidth)"
                "+' 溢出='+(q.scrollWidth>q.clientWidth+1)"
                "+' | 按钮宽 '+Math.round(d.querySelector('.primary').getBoundingClientRect().width)"
                "+' | foot '+Math.round(f.getBoundingClientRect().height)})()"))
            await c.shot(str(OUT / f"{TAG}_手机详情.png"))
    finally:
        proc.terminate()
        await room_server.stop()


if __name__ == "__main__":
    asyncio.run(main())
