"""丹阁丹方（带分区参数）真实链路验收。

`_manual_real_input.py` 的 Router 只把 `fn(plug, ev)` 两个参数传下去，
带分区的命令（/丹阁 丹方、/丹阁 突破丹方）走到 handler 时 args 恒为 ""，
只会吐出分区菜单。这里把 resolve() 出来的 rest 一起传进去，其余环节
（真实 room_server HTTP/WS、真实 main.py 方法体、真实 sidecar、真实 Chromium）
和那个脚本完全一致。

用法：
    python tests/_manual_real_pavilion.py 丹方
    python tests/_manual_real_pavilion.py 突破丹方
    python tests/_manual_real_pavilion.py 丹方 --no-browser
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import _manual_real_input as H  # noqa: E402

R = H.R
room_server = H.room_server
PORT = 6223


class ArgRouter(H.Router):
    """把命令尾部参数一并交给 main.py 的方法。"""

    async def on_player_message(self, room_id, player_id, player_name, text,
                                req_id=None, private=False, want_card=False, **kw):
        fn, rest = H.resolve(text)
        if fn is None:
            self.log.append((text, "未注册命令", 0))
            await room_server.broadcast_bot_payload(
                room_id, {"text": f"没有这条指令：{text}", "ts": int(time.time() * 1000)}
            )
            return
        ev = R.RealEvent(player_id, platform="webroom", text=text, name=player_name)
        # 带分区参数的方法收 args，不带的（/器阁、/百宝阁）只收 event
        takes_args = len(inspect.signature(fn).parameters) >= 3
        stream = fn(self.plug, ev, rest) if takes_args else fn(self.plug, ev)
        async for _ in stream:
            pass
        payload = H.result_to_payload(ev)
        detail = []
        for card in payload["cards"]:
            for sec in card.get("sections") or []:
                if sec.get("view") == "grid":
                    names = [e.get("name") for e in (sec.get("entries") or [])]
                    detail.append(f"{sec.get('name')}×{len(names)}:{names}")
        self.log.append((text, fn.__name__, len(payload["cards"]), detail))
        await room_server.broadcast_bot_payload(room_id, payload)


DETAIL_JS = """
(() => {
  const d = document.querySelector('.ed-drawer.on');
  if (!d) return '-';
  return d.innerText.replace(/\\n+/g, ' | ');
})()
"""

FIRST_CELL_JS = """
(() => {
  const cc = document.querySelectorAll('.card');
  const C = cc[cc.length - 1];
  if (!C) return false;
  const e = [...C.querySelectorAll('.gcell')].find(x => x.offsetParent !== null);
  if (!e) return false;
  e.click();
  return true;
})()
"""


async def run(section, no_browser):
    # 丹阁的四个分区要补前缀；"符阁 符图" / "器阁" / "百宝阁" 这种本身就是完整命令
    sections = {"丹药", "丹方", "突破丹方", "材料"}
    cmd = f"丹阁 {section}" if section in sections else section
    fd, dbpath = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    Path(dbpath).unlink(missing_ok=True)

    db = R.DB_CLS(dbpath)
    await db.connect()
    await R._imp("data").MigrationManager(db.conn, R.CM).migrate()
    await db.ext.ensure_system_config_table()
    player = await H.make_player(db, R.CM)
    plug = R.bind_plugin(db, R.CM)
    H.wire(plug, db)
    # H.wire 里的 ShopHandler 只是局部变量（六条命令都走别处），
    # /丹阁 需要它挂在插件上，否则 main.py 的 handle_pill_pavilion 找不到 self.shop_handler。
    plug.shop_handler = R._imp("handlers.shop_handler").ShopHandler(db, plug.config, R.CM)
    plug.pill_handler = R._imp("handlers.pill_handler").PillHandler(db, R.CM)
    print(f"[{cmd}] 玩家 {H.PLAYER_ID} 已建；已注册命令 {len(H.ROUTES)} 条", flush=True)

    router = ArgRouter(plug)
    room_server.set_adapter(router)

    if no_browser:
        await router.on_player_message("room_main", H.PLAYER_ID, H.NICK, f"/{cmd}")
        print("  离线派发:", router.log, flush=True)
        await db.close()
        Path(dbpath).unlink(missing_ok=True)
        return

    import shot

    await room_server.start("127.0.0.1", PORT)
    shot.URL_BASE = f"http://127.0.0.1:{PORT}/"
    H.accounts.register(H.ACCOUNT, H.PASSWORD, H.NICK)
    proc = shot.start_chrome(1440, 900)
    tag = section.replace(" ", "_")
    try:
        shot.wait_devtools()
        page = shot.open_page(shot.URL_BASE)
        await asyncio.sleep(1.2)
        async with shot.CDP(page["webSocketDebuggerUrl"]) as c:
            await c.cmd("Page.enable")
            await c.cmd("Runtime.enable")
            ok = await c.js(H.LOGIN_JS % (json.dumps(H.ACCOUNT), json.dumps(H.PASSWORD)))
            print("  登录:", "进入主界面" if ok else "失败", flush=True)
            await asyncio.sleep(1.5)
            await c.js(H.SEND_JS % json.dumps(f"/{cmd}"))
            await asyncio.sleep(2.2)
            stat = json.loads(await c.js(H.COUNT_JS) or "{}")
            print("  页面:", stat, flush=True)
            print("  服务端派发:", router.log, flush=True)
            m = re.search(r"共\s*(\d+)\s*条", stat.get("sum", ""))
            back = re.search(r"×(\d+):", (router.log[-1][3] or [""])[0]) if router.log else None
            if m and back:
                same = int(m.group(1)) == int(back.group(1))
                print(f"  总数一致: {'✅' if same else '❌'} 页面 {m.group(1)} / 后端 {back.group(1)}",
                      flush=True)
            print("  页面错误:", await c.js("JSON.stringify(window.__errs||[])"))
            await c.shot(str(R.OUT / f"recipe_{tag}.png"))

            clicked = await c.js(FIRST_CELL_JS)
            await asyncio.sleep(0.8)
            detail = await c.js(DETAIL_JS)
            print("  点开第一格:", clicked, flush=True)
            print("  详情抽屉:", detail, flush=True)
            await c.shot(str(R.OUT / f"recipe_{tag}_详情.png"))

            # 点完这一格后页面里的「已发指令」与格子计数
            print("  最后一条我的消息:", await c.js(
                "(()=>{const rows=[...document.querySelectorAll('.msg-row.right')];"
                "const last=rows[rows.length-1];return last?last.innerText.replace(/\\n+/g,' '):'-'})()"),
                flush=True)
            print("  第一格计数:", await c.js(
                "(()=>{const e=[...document.querySelectorAll('.gcell')].find(x=>x.offsetParent!==null);"
                "if(!e)return '-';const v=e.querySelector('.gc');"
                "return (v?v.textContent:'-')+' / '+(e.classList.contains('used')?'已用完':'在')})()"),
                flush=True)

            # 长按 600ms 应该开详情（快捷格的第二条入口）
            print("  长按派发:", await c.js(
                "(()=>{const e=[...document.querySelectorAll('.gcell')].find(x=>x.offsetParent!==null);"
                "if(!e)return 'no-cell';"
                "e.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerType:'touch'}));"
                "return 'pointerdown'})()"), flush=True)
            await asyncio.sleep(0.9)
            print("  长按后详情:", (await c.js(DETAIL_JS) or "-")[:90], flush=True)
            print("  页面错误:", await c.js("JSON.stringify(window.__errs||[])"))
            await c.js("(()=>{const e=[...document.querySelectorAll('.gcell')].find(x=>x.offsetParent!==null);"
                       "if(e)e.dispatchEvent(new PointerEvent('pointerup',{bubbles:true}));"
                       "const d=document.querySelector('.ed-drawer.on .ed-close');if(d)d.click()})()")

            # 详情里的下单按钮文案就是即将发出去的命令（entry.action + 数量）
            print("  下单按钮:", await c.js(
                "(()=>{const b=document.querySelector('.ed-drawer.on .ed-foot .primary');"
                "return b?b.textContent.trim():'-'})()"), flush=True)
            print("  详情分节:", await c.js(
                "(()=>{const d=document.querySelector('.ed-drawer.on');"
                "return d?JSON.stringify([...d.querySelectorAll('.ed-sec h4')].map(x=>x.textContent)):'-'})()"),
                flush=True)

            # 点主按钮：这一步会走 dispatchCommand → WS 发送 → 服务端真执行。
            # 只要按钮文案是「查看」而不是「服用」，或者点完输入框里没出现指令，
            # 就说明「格子里点不出服用」。
            before = await c.js("document.querySelectorAll('.msg-row').length")
            await c.js("(()=>{const b=document.querySelector('.ed-drawer.on .ed-foot .primary');"
                       "if(b)b.click()})()")
            await asyncio.sleep(1.6)
            sent = await c.js(
                "(()=>{const rows=[...document.querySelectorAll('.msg-row.right')];"
                "const last=rows[rows.length-1];return last?last.innerText.trim():'-'})()")
            print("  点按钮后发出:", sent, flush=True)
            print("  消息数:", before, "->",
                  await c.js("document.querySelectorAll('.msg-row').length"), flush=True)
            await c.shot(str(R.OUT / f"recipe_{tag}_服用.png"))
    finally:
        proc.terminate()
        await room_server.stop()
        await db.close()
        Path(dbpath).unlink(missing_ok=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    raw = args[0] if args else "丹方"
    # 传 "符阁|符图" 这种带竖线的，就是整条命令；否则默认补「丹阁」前缀
    asyncio.run(run(raw.replace("|", " "), "--no-browser" in sys.argv))
