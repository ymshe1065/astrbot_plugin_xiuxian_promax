# -*- coding: utf-8 -*-
"""临时验收：本轮新网格化的 6 个长清单（丹药背包 / 宗门商店 / 仙缘商店 / 坊市 / 黑市 / 称号图鉴）。

跑法：python tests/_manual_list_shot.py [pill|sect|fate|market|black|title]
产物：_grid_shot/list_<key>.png 与 list_<key>_详情.png

复用 _manual_grid_shot 的 astrbot 桩与 Chromium/CDP 封装，只是场景不同。
不是 unittest 用例（下划线开头，discover 不收）。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

# _manual_grid_shot 在模块级按 sys.argv[1] 选场景，导入前先把参数挪开
_ARGV = sys.argv[:]
sys.argv = [sys.argv[0]]
import _manual_grid_shot as G  # noqa: E402
sys.argv = _ARGV

PKG = G.PKG
CM = G.CM
Player = G.Player
OUT = G.OUT
room_server = G.room_server
accounts = G.accounts
shot = G.shot
Imp = G._imp


def _player(**kw):
    p = Player(user_id="u1", user_name="云无月", gold=240000, level_index=22)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def build_pill_card():
    from astrbot_plugin_xiuxian_promax.core.pill_manager import PillManager
    mgr = PillManager(G._FakeDB(_player()), CM)
    player = _player()
    inv = {}
    names = [v.get("name") for v in CM.pills_data.values()][:6]
    names += [v.get("name") for v in CM.exp_pills_data.values()][:6]
    qualities = ["一般", "良好", "极佳", "大师"]
    for i, n in enumerate(names):
        if not n:
            continue
        for q in qualities[: 1 + i % 3]:
            inv[f"{n}|{q}"] = 1 + i % 7
    player.set_pills_inventory(inv)
    entries = mgr.build_pill_bag_entries(player)
    print(f"[丹药背包] entries={len(entries)} 品质={sorted({e['kind'] for e in entries})}")
    return {
        "kind": "bag",
        "title": "云无月 的丹药背包",
        "subtitle": f"共 {len(entries)} 格（按品质分开）",
        "sections": [{
            "name": "丹药",
            "view": "grid",
            "entries": entries,
            "facet": {"label": "品质", "okLabel": "可用"},
        }],
        "footer": "/服用丹药 <名> [品质] [数量]",
        "ts": int(time.time()),
    }


def build_sect_card():
    from astrbot_plugin_xiuxian_promax.handlers.sect_shop_handler import SectShopHandler
    from astrbot_plugin_xiuxian_promax.core.shop_manager import ShopManager

    shop = ShopManager({}, CM)
    handler = SectShopHandler.__new__(SectShopHandler)
    handler.shop_manager = shop
    handler.config = {}
    handler.sect_mgr = None

    def _discounted(sect, base):
        return max(1, int(base * 0.94))

    handler._discounted_price = _discounted
    items = []
    for v in CM.items_data.values():
        if not isinstance(v, dict) or int(v.get("price") or 0) <= 0:
            continue
        items.append({
            "name": v.get("name"), "type": v.get("type") or "material",
            "rank": v.get("rank", "凡品"), "price": int(v.get("price") or 0),
            "stock": 3 if len(items) % 2 else 0, "data": dict(v),
        })
        if len(items) >= 5:
            break

    class _Sect:
        sect_name = "青云宗"

    player = _player()
    player.sect_contribution = 8600
    entries = handler._build_entries(_Sect(), items, player)
    print(f"[宗门商店] entries={len(entries)}")
    return {
        "kind": "shop",
        "title": "🏪 青云宗 · 宗门商店",
        "subtitle": "你的贡献 8,600 · 12 分钟后刷新 · 商堂 3 级折扣",
        "sections": [{
            "name": "在售",
            "view": "grid",
            "entries": entries,
            "facet": {"label": "", "okLabel": "可兑"},
        }],
        "footer": "点格子兑换 · /宗门兑换 <编号>",
        "ts": int(time.time()),
    }


def build_fate_card():
    from astrbot_plugin_xiuxian_promax.core.reincarnation_rules import shop_entries
    entries = shop_entries({
        "global_cultivation_multiplier": 0.4,
        "avatar_slots": 2,
        "equipment_tier_bonus": 1,
        "pet_slot_bonus": 2,
        "titles": {},
    })
    print(f"[仙缘商店] entries={len(entries)}")
    return {
        "kind": "shop",
        "title": "🌟 仙缘商店",
        "subtitle": "当前仙缘点 186 · 完美转生 3 次 · 弃道重修 1 次",
        "sections": [{
            "name": "商品",
            "view": "grid",
            "entries": entries,
            "facet": {"label": "分组", "okLabel": "可兑"},
        }],
        "footer": "点格子兑换 · /仙缘兑换 <编号>",
        "ts": int(time.time()),
    }


async def build_market_card():
    from astrbot_plugin_xiuxian_promax.managers.auction_manager import AuctionManager

    class _Ring:
        def __init__(self):
            self.config_manager = CM

    class _DB:
        async def get_player_by_id(self, uid):
            return Player(user_id=uid, user_name="卖家甲", gold=0, level_index=5)

    mgr = AuctionManager(_DB(), _Ring(), {})
    pool = []
    for v in CM.items_data.values():
        if isinstance(v, dict) and v.get("type") in ("材料", "丹药", "符箓", "丹方") and v.get("name"):
            pool.append(v)
    pool = pool[:12]
    items = []
    for i, v in enumerate(pool):
        items.append({
            "id": 100 + i,
            "item_name": v.get("name"),
            "base_item_name": v.get("name"),
            "count": 1 + i % 4,
            "price": 800 * (i + 1),
            "item_quality": "" if v.get("type") != "丹药" else "良好",
            "item_kind": "pill" if v.get("type") == "丹药" else "item",
            "seller_id": "u9",
        })
    entries = await mgr.build_market_entries(items)
    print(f"[坊市] entries={len(entries)}")
    return {
        "kind": "shop",
        "title": "🏪 玩家交易坊市",
        "subtitle": f"最近 {len(entries)} 件 · 交易税 5%",
        "sections": [{
            "name": "最新上架",
            "view": "grid",
            "entries": entries,
            "facet": {"label": "", "okLabel": "可买"},
        }],
        "footer": "/坊市购买 <编号>",
        "ts": int(time.time()),
    }


def build_black_card():
    from astrbot_plugin_xiuxian_promax.managers.gamble_manager import GambleManager
    mgr = GambleManager(None, CM, None)
    entries = mgr.build_black_market_entries()
    print(f"[黑市] entries={len(entries)}")
    return {
        "kind": "shop",
        "title": "🕶️ 黑市",
        "subtitle": f"{len(entries)} 件在售 · 每小时刷新",
        "sections": [{
            "name": "在售",
            "view": "grid",
            "entries": entries,
            "facet": {"label": "", "okLabel": "在售"},
        }],
        "footer": "点格子购买 · /黑市购买 <编号> · 真假自负",
        "ts": int(time.time()),
    }


def build_title_card():
    from astrbot_plugin_xiuxian_promax.managers.title_manager import TitleManager
    mgr = TitleManager(None)
    owned = {}
    for i, t in enumerate(G._imp("managers.title_data").TITLES):
        if i % 4 == 0:
            owned[t["name"]] = 1700000000 + i
    player = _player()
    player.permanent_bonus = json.dumps({"titles": owned})
    entries = mgr.build_collection_entries(player)
    collected = sum(1 for e in entries if e["state"] == "ok")
    print(f"[称号图鉴] entries={len(entries)} 已收集={collected}")
    return {
        "kind": "info",
        "title": "📜 称号图鉴",
        "subtitle": f"已收集 {collected}/{len(entries)} · 加成自动按最高品阶生效",
        "sections": [{
            "name": "称号",
            "view": "grid",
            "entries": entries,
            "facet": {"label": "收集", "okLabel": "已收集"},
        }],
        "footer": "点格子佩戴 · /佩戴称号 <名>",
        "ts": int(time.time()),
    }


_KEYS = {
    "pill": ("/丹药背包", "list_pill", build_pill_card),
    "sect": ("/宗门商店", "list_sect", build_sect_card),
    "fate": ("/仙缘商店", "list_fate", build_fate_card),
    "market": ("/坊市", "list_market", build_market_card),
    "black": ("/黑市", "list_black", build_black_card),
    "title": ("/称号图鉴", "list_title", build_title_card),
}

_KEY = sys.argv[1] if len(sys.argv) > 1 else "pill"
CMD, TAG, BUILDER = _KEYS[_KEY]


class Adapter:
    seq = 0

    async def on_player_message(self, room_id, player_id, player_name, text,
                                req_id=None, private=False, want_card=False, **kw):
        cmd = (text or "").strip()
        if not cmd.startswith(CMD):
            await room_server.broadcast_bot_payload(
                room_id, {"text": f"（演示）忽略 {cmd}", "ts": int(time.time() * 1000)}
            )
            return
        Adapter.seq += 1
        card = dict(CARD, id=f"listdemo{Adapter.seq}", ts=int(time.time() * 1000) + Adapter.seq)
        print(f"[adapter] {cmd} -> {card['title']}", flush=True)
        await room_server.broadcast_bot_payload(
            room_id, {"text": "", "cards": [card], "ts": card["ts"]}
        )


CARD = None


async def send_scene(c):
    return await c.js(
        "(()=>{const t=document.getElementById('inMsg');"
        f"t.value={json.dumps(CMD)};"
        "t.dispatchEvent(new Event('input',{bubbles:true}));"
        "document.getElementById('btnSend').click();"
        "return t.value})()"
    )


_CDP = None


async def last(expr):
    c = _CDP
    return await c.js(
        f"(()=>{{const C=[...document.querySelectorAll('.card')].pop();if(!C)return '-';return {expr}}})()"
    )


async def main():
    global CARD
    CARD = await BUILDER() if asyncio.iscoroutinefunction(BUILDER) else BUILDER()

    room_server.set_adapter(Adapter())
    await room_server.start("127.0.0.1", 6217)
    accounts.register("网格演示", "test123456", "网格演示")
    print("listening", flush=True)

    auth = {"token": accounts.make_token("网格演示"), "username": "网格演示", "nick": "网格演示"}
    proc = shot.start_chrome(1440, 900)
    try:
        shot.wait_devtools()
        page = shot.open_page(shot.URL_BASE)
        await asyncio.sleep(1.2)
        async with shot.CDP(page["webSocketDebuggerUrl"]) as c:
            global _CDP
            _CDP = c
            await c.cmd("Page.enable")
            await c.cmd("Runtime.enable")
            auth_js = json.dumps(json.dumps(auth, ensure_ascii=False))
            await c.js(f"localStorage.clear();localStorage.setItem('xiuxian_auth_v1', {auth_js})")
            await c.cmd("Page.addScriptToEvaluateOnNewDocument", source=G.HOOK_JS)
            await c.cmd("Page.reload")
            await asyncio.sleep(3.2)
            await send_scene(c)
            await asyncio.sleep(2.0)
            print("卡片数:", await c.js("document.querySelectorAll('.card').length"))
            print("页面错误:", await c.js("JSON.stringify(window.__errs||[])"))
            print("标题:", await last("C.querySelector('.card-title').textContent"))
            print("品阶条:", await last(
                "[...C.querySelectorAll('.rchip')].map(b=>b.innerText.replace(/\\n/g,'')).join('|')"))
            print("筛选:", await last(
                "[...C.querySelectorAll('.eg-facets .fchip')].map(b=>b.textContent).join('/')"))
            print("汇总:", await last("(()=>{const s=C.querySelector('.eg-sum');return s?s.innerText:'-'})()"))
            print("格子:", await last("C.querySelectorAll('.gcell').length"))
            print("列数:", await last(
                "(()=>{const g=C.querySelector('.eg-grid');"
                "return g?getComputedStyle(g).gridTemplateColumns.split(' ').length:'-'})()"))
            print("格尺寸:", await last(
                "(()=>{const e=C.querySelector('.gcell');if(!e)return '-';"
                "const r=e.getBoundingClientRect();return Math.round(r.width)+'x'+Math.round(r.height)})()"))
            print("首格文字:", await last(
                "(()=>{const e=C.querySelector('.gcell');return e?e.innerText.replace(/\\n/g,' / '):'-'})()"))
            await c.shot(str(OUT / f"{TAG}.png"))

            before = await c.js("document.querySelectorAll('.msg').length")
            await c.js("(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                       "const e=[...C.querySelectorAll('.gcell')].find(x=>x.offsetParent!==null);if(e)e.click()})()")
            await asyncio.sleep(0.9)
            print("详情打开:", await c.js("!!document.querySelector('.ed-drawer.on')"))
            print("详情标题:", await c.js(
                "(()=>{const t=document.querySelector('.ed-drawer .ed-title');return t?t.textContent:'-'})()"))
            print("chips:", await c.js(
                "[...document.querySelectorAll('.ed-drawer .ed-chip')].map(x=>x.textContent).join(' | ')"))
            print("小节:", await c.js(
                "[...document.querySelectorAll('.ed-drawer .ed-sec h4')].map(h=>h.textContent).join('/')"))
            print("正文:", await c.js(
                "(()=>{const b=document.querySelector('.ed-drawer .ed-body');return b?b.innerText.replace(/\\n/g,' / ').slice(0,220):'-'})()"))
            print("主按钮:", await c.js(
                "(()=>{const b=document.querySelector('.ed-drawer .primary');"
                "return b?(b.textContent+(b.disabled?'(禁用)':'')):'-'})()"))
            print("新增消息:", await c.js("document.querySelectorAll('.msg').length") - before)
            await c.shot(str(OUT / f"{TAG}_详情.png"))
            # 点主按钮：抓 ws 帧，确认指令拼得对
            await c.js("(()=>{const P=WebSocket.prototype;const raw=P.send;window.__s=[];"
                       "P.send=function(d){try{window.__s.push(String(d).slice(0,160))}catch(e){};"
                       "return raw.call(this,d)}})()")
            await c.js("(()=>{const b=document.querySelector('.ed-drawer .primary');if(b&&!b.disabled)b.click()})()")
            await asyncio.sleep(0.8)
            print("ws 帧:", await c.js("JSON.stringify(window.__s||[])"))

            # 手机视口
            await c.cmd("Emulation.setDeviceMetricsOverride", width=390, height=844,
                        deviceScaleFactor=2, mobile=True)
            await c.cmd("Page.addScriptToEvaluateOnNewDocument", source=G.HOOK_TOUCH_JS)
            await c.cmd("Page.reload")
            await asyncio.sleep(3.4)
            if not await c.js("document.querySelectorAll('.card').length"):
                await send_scene(c)
                await asyncio.sleep(2.2)
            print("[手机] 格子:", await last("C.querySelectorAll('.gcell').length"))
            print("[手机] 列数:", await last(
                "(()=>{const g=C.querySelector('.eg-grid');"
                "return g?getComputedStyle(g).gridTemplateColumns.split(' ').length:'-'})()"))
            print("[手机] 横向溢出:", await last(
                "(()=>{const w=C.querySelector('.eg-wrap');"
                "return w?w.scrollWidth+'/'+w.clientWidth:'-'})()"))
            await c.shot(str(OUT / f"{TAG}_手机.png"))
            await c.js("(()=>{const C=[...document.querySelectorAll('.card')].pop();"
                       "const e=[...C.querySelectorAll('.gcell')].find(x=>x.offsetParent!==null);if(e)e.click()})()")
            await asyncio.sleep(0.9)
            print("[手机] 详情高:", await c.js(
                "(()=>{const d=document.querySelector('.ed-drawer');"
                "return d?Math.round(d.getBoundingClientRect().height)+' top='+"
                "Math.round(d.getBoundingClientRect().top):'-'})()"))
            await c.shot(str(OUT / f"{TAG}_手机详情.png"))
    finally:
        proc.terminate()
        await room_server.stop()


if __name__ == "__main__":
    asyncio.run(main())
