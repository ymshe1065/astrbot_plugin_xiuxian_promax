/* 修仙聊天室 · 前端主逻辑
 * - WebSocket 通信（公屏 chat / 私聊 reply）
 * - 结构化卡片原生渲染（替代 276KB 位图）
 * - 看板：状态 / 储物戒 / 指令格；移动端为底部抽屉
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const isMobile = () => window.matchMedia("(max-width: 900px)").matches;
  const CACHE_KEY = "xiuxian_room_cache_v1";
  const CACHE_MAX = 80;

  /* ================= 状态 ================= */
  const S = {
    ws: null,
    me: "",
    room: "",
    seq: 0,
    pending: new Map(),      // req_id -> { cmd, panel }
    cards: new Map(),        // card id -> card
    panelCard: { status: null, bag: null },
    lastCmd: null,           // 用于刷新
    reconnectTimer: null,
    manualClose: false,
    connected: false,
    cmdCatalog: null,
    unread: { status: 0, bag: 0 },
  };

  /* ================= 工具 ================= */
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fmtTime = (ts) => {
    const d = ts ? new Date(ts * 1000) : new Date();
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };

  /** 数值高亮：把文本里的数字包一层 span */
  function highlightNumbers(text) {
    return esc(text).replace(/(\d[\d,]*(?:\.\d+)?%?)/g, '<span class="num">$1</span>');
  }

  function toast(text, ms = 1800) {
    const el = $("toast");
    el.textContent = text;
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(() => (el.hidden = true), ms);
  }

  function reqId() {
    S.seq += 1;
    return `r${Date.now().toString(36)}${S.seq}`;
  }

  /* ================= 连接 ================= */
  function join() {
    const name = $("inName").value.trim();
    const room = $("inRoom").value.trim() || "room_main";
    if (!name) {
      $("loginErr").textContent = "请先报上道号";
      return;
    }
    localStorage.setItem("xiuxian_room_name", name);
    localStorage.setItem("xiuxian_room_id", room);
    S.me = name;
    S.room = room;
    $("loginErr").textContent = "";
    $("login").hidden = true;
    $("login").style.display = "none";
    $("app").hidden = false;
    $("roomTag").textContent = room;
    connect();
    loadCatalog();
    setupViewport();
    replayCache();
  }

  function connect() {
    S.manualClose = false;
    setConn("连接中…", false);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws/${encodeURIComponent(S.room)}?player=${encodeURIComponent(S.me)}`;
    let ws;
    try {
      ws = new WebSocket(url);
    } catch {
      setConn("连接失败", false);
      scheduleReconnect();
      return;
    }
    S.ws = ws;
    ws.onopen = () => {
      S.connected = true;
      setConn("已连接", true);
      refreshOnline();
      // 连接就绪后再拉看板（首次连接与断线重连都走这里）
      loadPanel("status");
    };
    ws.onclose = (e) => {
      S.connected = false;
      setConn("已断开", false);
      if (S.manualClose) return;
      if (e.code === 4001) {
        addSys("账号在其他地方登录，本连接已断开");
        return;
      }
      addSys("连接断开，正在重连…");
      scheduleReconnect();
    };
    ws.onerror = () => setConn("连接异常", false);
    ws.onmessage = onMessage;
  }

  function scheduleReconnect() {
    clearTimeout(S.reconnectTimer);
    S.reconnectTimer = setTimeout(() => {
      if (!S.manualClose) connect();
    }, 3000);
  }

  function setConn(text, on) {
    const el = $("connState");
    el.textContent = text;
    el.className = "conn" + (on ? " on" : "");
  }

  function refreshOnline() {
    if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ action: "online" }));
  }

  /* ================= 发送 ================= */
  function sendChat(text) {
    if (!S.ws || S.ws.readyState !== 1) {
      toast("尚未连接");
      return false;
    }
    S.ws.send(JSON.stringify({ action: "msg", text }));
    return true;
  }

  /**
   * 发指令并等待定向回复
   * @param {string} text 指令原文
   * @param {{panel?: 'status'|'bag', card?: boolean, silent?: boolean}} opts
   */
  function sendCmd(text, opts = {}) {
    if (!S.ws || S.ws.readyState !== 1) {
      toast("尚未连接，请稍候");
      return;
    }
    const id = reqId();
    S.pending.set(id, { cmd: text, panel: opts.panel || null, card: !!opts.card });
    S.ws.send(
      JSON.stringify({
        action: "cmd",
        text,
        req_id: id,
        card: !!opts.card || !!opts.panel,
        private: true,
      })
    );
    if (!opts.silent && !opts.panel) {
      addChat(S.me, text, null, true);
    }
    // 兜底：20 秒未收到回复则丢弃等待态
    setTimeout(() => {
      if (S.pending.has(id)) {
        S.pending.delete(id);
        if (opts.panel) renderPanelEmpty(opts.panel, "响应超时，请重试");
      }
    }, 20000);
  }

  /* ================= 消息处理 ================= */
  function onMessage(ev) {
    let m;
    try {
      m = JSON.parse(ev.data);
    } catch {
      return;
    }
    switch (m.type) {
      case "hello":
        addSys(`欢迎 ${S.me}，本室在线 ${(m.online || []).length} 人`);
        refreshOnline();
        break;
      case "chat":
        addChat(m.from, m.text, m.ts, m.from === S.me);
        break;
      case "bot":
        addBotMessage(m);
        break;
      case "reply":
        handleReply(m);
        break;
      case "broadcast":
        showBanner(m.text);
        break;
      case "sys":
        addSys(m.text);
        refreshOnline();
        break;
      case "error":
        addError(m.text);
        break;
      case "online":
        break;
      case "pong":
        break;
      default:
        break;
    }
  }

  function handleReply(m) {
    const ctx = S.pending.get(m.req_id);
    S.pending.delete(m.req_id);

    if (m.error && !m.cards?.length) {
      if (ctx?.panel) renderPanelEmpty(ctx.panel, m.text || "请求失败");
      else addError(m.text || "请求失败");
      return;
    }

    // 收集卡片
    const cards = (m.cards || []).filter(Boolean);
    cards.forEach((c) => S.cards.set(c.id, c));

    // 面板请求：结果归位到看板，不进聊天流
    if (ctx?.panel) {
      if (cards.length) setPanelCard(ctx.panel, cards[0]);
      else if (m.text) renderPanelText(ctx.panel, m.text);
      else renderPanelEmpty(ctx.panel, "没有数据");
      return;
    }

    // 其他回复：进聊天流
    if (m.text) addBotText(m.text, m.ts, m.options);
    cards.forEach((c) => addCardMessage(c));
    (m.images || []).forEach((u) => u && addBotText("", m.ts, null, [u]));
    if (m.options?.length) showQuickOptions(m.options);
  }

  /* ================= 渲染：消息 ================= */
  function scrollBottom(force = false) {
    const box = $("msgs");
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 140;
    if (force || nearBottom) box.scrollTop = box.scrollHeight;
  }

  function appendNode(el, opts = {}) {
    const box = $("msgs");
    box.appendChild(el);
    while (box.children.length > 240) box.firstChild.remove();
    scrollBottom(opts.force);
  }

  function addChat(from, text, ts, isMe) {
    const div = document.createElement("div");
    div.className = "msg " + (isMe ? "me" : "player");
    div.innerHTML = isMe
      ? `${escapeWithMarks(text)}<div class="time-line">${fmtTime(ts)}</div>`
      : `<div class="who">${esc(from)}<span class="time">${fmtTime(ts)}</span></div>${escapeWithMarks(text)}`;
    appendNode(div);
    cachePush({ t: "chat", from, text, ts, me: isMe });
  }

  function addSys(text) {
    const div = document.createElement("div");
    div.className = "msg sys";
    div.textContent = text;
    appendNode(div);
  }

  function addError(text) {
    const div = document.createElement("div");
    div.className = "msg error";
    div.textContent = text;
    appendNode(div, { force: true });
  }

  function addBotText(text, ts = null, options = null, images = null) {
    const div = document.createElement("div");
    div.className = "msg bot";
    const tone = classifyTone(text);
    if (tone) div.classList.add(tone);
    let html = `<div class="who">仙人<span class="time">${fmtTime(ts)}</span></div>`;
    if (text) html += escapeWithMarks(text);
    (images || []).forEach((u) => {
      html += `<img src="${esc(u)}" data-full="${esc(u)}" alt="">`;
    });
    div.innerHTML = html;
    bindImages(div);
    appendNode(div, { force: true });
    if (text) cachePush({ t: "bot", text, ts });
    if (options?.length) showQuickOptions(options);
  }

  function addCardMessage(card) {
    const div = document.createElement("div");
    div.className = "msg card-msg";
    div.appendChild(renderCard(card));
    appendNode(div, { force: true });
  }

  function addBotMessage(m) {
    const cards = m.cards || [];
    if (cards.length) {
      cards.forEach((c) => addCardMessage(c));
      if (m.text) addBotText(m.text, m.ts);
    } else if (m.text) {
      addBotText(m.text, m.ts, m.options);
    }
    (m.images || []).forEach((u) => u && addBotText("", m.ts, null, [u]));
    if (m.options?.length) showQuickOptions(m.options);
  }

  /** 动作反馈的语气判定 */
  function classifyTone(text) {
    if (!text) return "";
    if (/^[✅✨🎉]|成功/.test(text)) return "action-ok";
    if (/^[❌⚠️]|失败|不足|无法/.test(text)) return "action-bad";
    return "";
  }

  /** 轻量 markdown：**粗体**、`代码` */
  function escapeWithMarks(text) {
    return esc(text)
      .replace(/\*\*([^*\n]{1,40})\*\*/g, "<b>$1</b>")
      .replace(/`([^`\n]{1,60})`/g, '<span class="md-code">$1</span>');
  }

  function bindImages(root) {
    root.querySelectorAll("img[data-full]").forEach((img) => {
      img.addEventListener("click", () => openViewer(img.dataset.full));
    });
  }

  /* ================= 卡片渲染 ================= */
  function renderCard(card) {
    const wrap = document.createElement("div");
    wrap.className = "card";
    wrap.dataset.cardId = card.id;

    const head = document.createElement("div");
    head.className = "card-head";
    head.innerHTML =
      `<div class="card-title">${esc(card.title || "信息")}</div>` +
      (card.subtitle ? `<div class="card-sub">${esc(card.subtitle)}</div>` : "");
    wrap.appendChild(head);

    const body = document.createElement("div");
    body.className = "card-body";

    // 1) 列表型（秘境/排行/Boss）
    if (Array.isArray(card.items) && card.items.length) {
      body.appendChild(renderItems(card.items, card));
    }

    // 2) 分区型（带条目提升的分区单独走列表渲染）
    const sections = (card.sections || []).filter(
      (s) => (s.rows || []).length || (s.lines || []).length || (s.items || []).length
    );
    sections.forEach((sec) => {
      if (Array.isArray(sec.items) && sec.items.length) {
        const box = document.createElement("div");
        box.className = "card-section";
        if (sec.name) {
          const n = document.createElement("div");
          n.className = "card-section-name";
          n.textContent = sec.name;
          box.appendChild(n);
        }
        box.appendChild(renderItems(sec.items));
        body.appendChild(box);
      } else {
        body.appendChild(renderSection(sec, card));
      }
    });

    if (body.childElementCount) wrap.appendChild(body);

    // 3) 卡脚：提示 + 动作
    const actions = card.actions || [];
    if (card.footer || actions.length || card.raw) {
      const foot = document.createElement("div");
      foot.className = "card-foot";
      if (card.footer) {
        const t = document.createElement("div");
        t.innerHTML = highlightNumbers(card.footer);
        foot.appendChild(t);
      }
      if (actions.length) {
        const bar = document.createElement("div");
        bar.className = "actions";
        actions.slice(0, 8).forEach((a) => {
          const b = document.createElement("button");
          b.className = "act-btn";
          b.textContent = a.label || a.cmd;
          b.addEventListener("click", () => dispatchCommand(a.cmd));
          bar.appendChild(b);
        });
        foot.appendChild(bar);
      }
      if (card.raw) {
        const tog = document.createElement("button");
        tog.className = "card-raw-toggle";
        tog.textContent = "查看原文";
        const raw = document.createElement("div");
        raw.className = "card-raw";
        raw.textContent = card.raw;
        tog.addEventListener("click", () => {
          raw.classList.toggle("open");
          tog.textContent = raw.classList.contains("open") ? "收起原文" : "查看原文";
        });
        foot.appendChild(tog);
        foot.appendChild(raw);
      }
      if (card.image) {
        const b = document.createElement("button");
        b.className = "card-raw-toggle";
        b.style.marginLeft = "10px";
        b.textContent = "原图";
        b.addEventListener("click", () => openViewer(card.image));
        foot.appendChild(b);
      }
      wrap.appendChild(foot);
    }
    return wrap;
  }

  function renderSection(sec, card) {
    const box = document.createElement("div");
    box.className = "card-section";
    if (sec.name) {
      const n = document.createElement("div");
      n.className = "card-section-name";
      n.textContent = sec.name;
      box.appendChild(n);
    }
    const rows = sec.rows || [];
    const name = sec.name || "";
    // 按分区名精确判断，避免把普通键值误渲染成槽位
    const isBag = /物品清单|物品栏/.test(name) || (card.kind === "bag" && /物品/.test(name));
    const isSlots = /装备栏|装备槽/.test(name);

    if (isBag) {
      box.appendChild(renderChips(rows));
    } else if (isSlots) {
      box.appendChild(renderSlots(rows));
    } else if (rows.length) {
      const list = document.createElement("div");
      list.className = "card-rows";
      rows.forEach(([k, v]) => {
        const row = document.createElement("div");
        row.className = "card-row";
        row.innerHTML = `<div class="k">${esc(k)}</div><div class="v">${rowValueHtml(k, v)}</div>`;
        list.appendChild(row);
      });
      box.appendChild(list);
    }
    const lines = sec.lines || [];
    const bulletLines = lines.filter((l) => /^[·•]\s*\S/.test(l));
    // 以 · 开头的行多为「导航/选项」，带指令的直接做成可点条目
    if (bulletLines.length && bulletLines.length >= lines.length * 0.6) {
      const list = document.createElement("div");
      list.className = "bullet-list";
      bulletLines.forEach((ln) => {
        const text = ln.replace(/^[·•]\s*/, "");
        const cmd = extractLineCmd(text);
        const item = document.createElement(cmd ? "button" : "div");
        item.className = "bullet-item" + (cmd ? " link" : "");
        item.innerHTML = `${esc(text)}` + (cmd ? '<span class="bullet-go">›</span>' : "");
        if (cmd) item.addEventListener("click", () => dispatchCommand(cmd));
        list.appendChild(item);
      });
      box.appendChild(list);
    }
    lines
      .filter((l) => !bulletLines.includes(l))
      .forEach((ln) => {
        const d = document.createElement("div");
        d.className = "card-line";
        d.innerHTML = valueHtml(ln);
        box.appendChild(d);
      });
    return box;
  }

  /** 从「/丹阁 丹药：成品丹药」中取出可直接发送的指令 */
  function extractLineCmd(text) {
    const m = text.match(/\/([^\s：:，。]+(?:\s+[^\s：:，。]+)?)/);
    if (!m) return null;
    const cmd = m[0].trim();
    if (/<|>/.test(cmd)) return null;
    return cmd;
  }

  /** 进度条：修为 0/500、气血 1280/1280 这类比直接显示数字直观 */
  function rowValueHtml(key, val) {
    const s = String(val ?? "");
    const m = s.match(/^([\d,]+)\s*\/\s*([\d,]+)$/);
    if (m && /修为|经验|气血|灵气|进度|容量|空间/.test(key)) {
      const cur = parseInt(m[1].replace(/,/g, ""), 10);
      const max = parseInt(m[2].replace(/,/g, ""), 10);
      if (max > 0 && cur >= 0) {
        const pct = Math.max(0, Math.min(100, (cur / max) * 100));
        return (
          `<div class="prog"><div class="prog-bar"><i style="width:${pct.toFixed(1)}%"></i></div>` +
          `<span class="prog-txt">${esc(s)}</span></div>`
        );
      }
    }
    return valueHtml(s);
  }

  function valueHtml(v) {
    const s = String(v ?? "");
    if (!s || s === "—" || s === "-") return '<span class="muted">—</span>';
    return highlightNumbers(s);
  }

  /** 物品芯片 */
  function renderChips(rows) {
    const box = document.createElement("div");
    box.className = "chips";
    if (!rows.length || (rows.length === 1 && /空空如也/.test(rows[0][0]))) {
      const e = document.createElement("div");
      e.className = "chip empty";
      e.textContent = "空空如也 · 去坊市逛逛？";
      box.appendChild(e);
      return box;
    }
    rows.forEach(([name, cnt]) => {
      const chip = document.createElement("button");
      chip.className = "chip";
      const m = String(cnt).match(/x?\s*(\d+)/i);
      chip.innerHTML = `<span>${esc(name)}</span>` + (m ? `<span class="cnt">×${m[1]}</span>` : "");
      chip.addEventListener("click", (e) => openItemMenu(name, e.currentTarget));
      box.appendChild(chip);
    });
    return box;
  }

  /** 装备槽位 */
  function renderSlots(rows) {
    const grid = document.createElement("div");
    grid.className = "slot-grid";
    rows.forEach(([slot, val]) => {
      const empty = !val || val === "—" || val === "-" || val === "空";
      const d = document.createElement("div");
      d.className = "slot" + (empty ? "" : " filled");
      d.innerHTML = `<div class="slot-name">${esc(slot)}</div><div class="slot-val">${empty ? "空" : esc(val)}</div>`;
      grid.appendChild(d);
    });
    return grid;
  }

  /** 列表型卡片（秘境/排行/商店货架） */
  function renderItems(items, card) {
    const box = document.createElement("div");
    box.className = "card-items";
    const buyable = card && card.kind === "shop";
    const LIMIT = 6; // 超过 6 条折叠，避免一张卡片占满屏
    let expanded = items.length <= LIMIT;

    const build = (list) => {
      list.forEach((it) => {
        const row = document.createElement("div");
        row.className = "card-item";
        const badgeText = it.meta && /^#\d+$/.test(it.meta) ? it.meta.slice(1) : it.meta || "";
        const rank = badgeText ? parseInt(badgeText, 10) : 0;
        let html = '<div class="card-item-head">';
        if (badgeText && /^\d+$/.test(badgeText)) {
          const cls = rank === 1 ? " top1" : rank === 2 ? " top2" : rank === 3 ? " top3" : "";
          html += `<span class="card-item-badge${cls}">${esc(badgeText)}</span>`;
        }
        html += `<span class="card-item-title">${esc(it.title || "")}</span>`;
        if (it.meta && !/^#?\d+$/.test(it.meta)) html += `<span class="card-item-meta">${esc(it.meta)}</span>`;
        html += "</div>";
        const rows = it.rows || [];
        if (rows.length) {
          // 说明类属性竖排（商店描述），奖励/要求这类短属性横排更省空间
          const vertical =
            rows.length >= 2 &&
            (rows.some(([k]) => /描述|效果|说明|简介|加成/.test(k)) || rows.length >= 4);
          if (vertical) {
            html += '<div class="card-item-kv">';
            rows.forEach(([k, v]) => {
              html += `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`;
            });
            html += "</div>";
          } else {
            html += '<div class="card-item-rows">';
            rows.forEach(([k, v]) => {
              html += `<span class="p">${esc(k)}<b>${esc(v)}</b></span>`;
            });
            html += "</div>";
          }
        }
        (it.lines || []).forEach((l) => {
          html += `<div class="card-line">${esc(l)}</div>`;
        });
        row.innerHTML = html;
        if (buyable && it.title) {
          row.classList.add("buyable");
          row.addEventListener("click", () => openSheet(`购买 ${it.title}`, "数量（默认 1）", "/购买", it.title));
        }
        box.appendChild(row);
      });
    };

    const paint = () => {
      box.innerHTML = "";
      build(expanded ? items : items.slice(0, 5));
      if (!expanded) {
        const more = document.createElement("button");
        more.className = "items-more";
        more.textContent = `展开全部 ${items.length} 条 ›`;
        more.addEventListener("click", () => {
          expanded = true;
          paint();
        });
        box.appendChild(more);
      }
    };
    paint();
    return box;
  }

  /* ---- 物品操作菜单 ---- */
  let ctxMenu = null;
  function closeCtxMenu() {
    if (ctxMenu) {
      ctxMenu.remove();
      ctxMenu = null;
    }
  }
  function openItemMenu(name, anchor) {
    closeCtxMenu();
    const menu = document.createElement("div");
    menu.style.cssText =
      "position:fixed;z-index:70;background:#fff;border:1px solid #e5dbcb;border-radius:10px;" +
      "box-shadow:0 8px 24px rgba(60,45,25,.18);padding:5px;min-width:132px;font-size:13.5px";
    const ops = [
      ["装备", `/装备 ${name}`],
      ["取出", `/取出 ${name}`],
      ["回收", `/回收 ${name}`],
      ["赠予", `/赠予 `, true],
    ];
    ops.forEach(([label, cmd, needArg]) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.style.cssText =
        "display:block;width:100%;text-align:left;padding:8px 12px;border-radius:7px;color:#3a3128";
      b.addEventListener("mouseenter", () => (b.style.background = "#f5efe4"));
      b.addEventListener("mouseleave", () => (b.style.background = ""));
      b.addEventListener("click", () => {
        closeCtxMenu();
        if (needArg) openSheet(label + " 物品", `${label}给谁？`, cmd, name);
        else dispatchCommand(cmd);
      });
      menu.appendChild(b);
    });
    document.body.appendChild(menu);
    const r = anchor.getBoundingClientRect();
    const top = Math.min(r.bottom + 6, window.innerHeight - menu.offsetHeight - 12);
    const left = Math.min(r.left, window.innerWidth - menu.offsetWidth - 12);
    menu.style.top = `${Math.max(10, top)}px`;
    menu.style.left = `${Math.max(10, left)}px`;
    ctxMenu = menu;
    setTimeout(() => document.addEventListener("click", closeCtxMenu, { once: true }), 0);
  }

  /* ================= 指令分发 ================= */
  function dispatchCommand(cmd) {
    if (!cmd) return;
    const text = cmd.startsWith("/") ? cmd : `/${cmd}`;
    // 看板常驻类指令：结果只进对应面板（注意中文后没有 \b 词边界）
    if (/^\/我的信息(?:\s|$)/.test(text)) {
      sendCmd(text, { panel: "status", card: true, silent: true });
      switchPanelTab("status");
      return;
    }
    if (/^\/储物戒(?:\s|$)/.test(text)) {
      sendCmd(text, { panel: "bag", card: true, silent: true });
      switchPanelTab("bag");
      return;
    }
    sendCmd(text, { card: true });
  }

  /* ================= 面板 ================= */
  function loadPanel(which) {
    if (which === "status") sendCmd("/我的信息", { panel: "status", card: true, silent: true });
    if (which === "bag") sendCmd("/储物戒", { panel: "bag", card: true, silent: true });
  }

  function panelView(which) {
    return which === "status" ? $("viewStatus") : $("viewBag");
  }

  function setPanelCard(which, card) {
    S.cards.set(card.id, card);
    S.panelCard[which] = card.id;
    const view = panelView(which);
    view.innerHTML = "";
    const head = document.createElement("div");
    head.className = "panel-head";
    head.innerHTML = `<span class="ph-title">${which === "status" ? "道体状态" : "储物戒"}</span>`;
    const btn = document.createElement("button");
    btn.className = "ph-refresh";
    btn.textContent = "刷新";
    btn.addEventListener("click", () => loadPanel(which));
    head.appendChild(btn);
    view.appendChild(head);
    view.appendChild(renderCard(card));
  }

  function renderPanelText(which, text) {
    const view = panelView(which);
    view.innerHTML = "";
    const d = document.createElement("div");
    d.className = "panel-empty";
    d.style.whiteSpace = "pre-wrap";
    d.style.textAlign = "left";
    d.textContent = text;
    view.appendChild(d);
  }

  function renderPanelEmpty(which, msg) {
    const view = panelView(which);
    view.innerHTML = `<div class="panel-empty"><span class="big">${which === "status" ? "☯" : "🎒"}</span>${
      esc(msg) || "暂无数据"
    }<br><button>点击查询</button></div>`;
    view.querySelector("button").addEventListener("click", () => loadPanel(which));
  }

  function switchPanelTab(tab) {
    document.querySelectorAll(".panel-tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === tab);
    });
    document.querySelectorAll(".panel-view").forEach((v) => {
      v.classList.toggle("active", v.dataset.view === tab);
    });
    if (tab === "cmds") renderCommandGrid();
    // 面板还没数据时按需拉取（开面板才查，避免无谓的卡片渲染）
    if ((tab === "status" || tab === "bag") && !S.panelCard[tab] && S.connected) {
      loadPanel(tab);
    }
    if (isMobile()) openDrawer();
  }

  /* ================= 指令格 ================= */
  async function loadCatalog() {
    try {
      const r = await fetch("/api/commands");
      S.cmdCatalog = await r.json();
      renderCommandGrid();
    } catch {
      /* 忽略：离线时指令格为空 */
    }
  }

  function renderCommandGrid() {
    const view = $("viewCmds");
    if (!S.cmdCatalog) {
      view.innerHTML = '<div class="panel-empty">指令加载中…</div>';
      return;
    }
    if (view.dataset.rendered === "1") return;
    view.dataset.rendered = "1";
    view.innerHTML = "";

    const search = document.createElement("input");
    search.className = "cmd-search";
    search.placeholder = "搜索指令…（如 秘境 / 突破）";
    view.appendChild(search);

    const list = document.createElement("div");
    view.appendChild(list);

    const draw = (kw = "") => {
      list.innerHTML = "";
      const q = kw.trim().toLowerCase();
      S.cmdCatalog.categories.forEach((cat) => {
        const items = cat.items.filter(
          (it) => !q || it.label.toLowerCase().includes(q) || it.cmd.toLowerCase().includes(q)
        );
        if (!items.length) return;
        const group = document.createElement("div");
        group.className = "cmd-group";
        group.innerHTML = `<div class="cmd-group-head"><span>${cat.icon || ""}</span>${esc(cat.name)}</div>`;
        const grid = document.createElement("div");
        grid.className = "cmd-grid";
        items.forEach((it) => {
          const b = document.createElement("button");
          b.className = "cmd-btn" + (it.tone === "gold" ? " gold" : "");
          b.innerHTML = `<span>${esc(it.label)}</span>` + (it.arg ? `<span class="arg-hint">需填参</span>` : "");
          b.addEventListener("click", () => {
            if (it.arg) openSheet(it.label, it.arg, it.cmd);
            else {
              dispatchCommand(it.cmd);
              closeDrawer();
            }
          });
          grid.appendChild(b);
        });
        group.appendChild(grid);
        list.appendChild(group);
      });
      if (!list.childElementCount) {
        list.innerHTML = '<div class="panel-empty">没有匹配的指令</div>';
      }
    };
    search.addEventListener("input", () => draw(search.value));
    draw();
  }

  /* ================= 参数弹层 ================= */
  let sheetCtx = null;
  function openSheet(title, hint, cmd, preset = "") {
    sheetCtx = { cmd, preset };
    $("sheetTitle").textContent = title;
    $("sheetHint").textContent = hint ? `填写：${hint}` : "";
    $("sheetInput").value = preset ? `${preset} ` : "";
    $("sheetMask").hidden = false;
    setTimeout(() => {
      $("sheetInput").focus();
      const v = $("sheetInput").value;
      $("sheetInput").setSelectionRange(v.length, v.length);
    }, 60);
  }

  function closeSheet() {
    $("sheetMask").hidden = true;
    sheetCtx = null;
    $("sheetInput").blur();
  }

  function submitSheet() {
    if (!sheetCtx) return;
    const val = $("sheetInput").value.trim();
    if (!val) {
      toast("请填写内容");
      return;
    }
    const text = `${sheetCtx.cmd} ${val}`;
    closeSheet();
    closeDrawer();
    dispatchCommand(text);
  }

  /* ================= 播报横幅 ================= */
  let bannerTimer = null;
  function showBanner(text) {
    if (!text) return;
    const el = $("banner");
    $("bannerText").textContent = text;
    el.hidden = false;
    clearTimeout(bannerTimer);
    bannerTimer = setTimeout(() => (el.hidden = true), 20000);
    addSys("📢 " + text.replace(/\n/g, " "));
  }

  /* ================= 快捷选项 ================= */
  function showQuickOptions(options) {
    const box = $("quickOptions");
    box.innerHTML = "";
    options.slice(0, 6).forEach((o) => {
      const b = document.createElement("button");
      b.className = "qopt";
      b.textContent = o.label;
      b.addEventListener("click", () => {
        sendChat(o.cmd);
        box.hidden = true;
      });
      box.appendChild(b);
    });
    box.hidden = false;
  }

  /* ================= 移动端抽屉 ================= */
  function openDrawer() {
    if (!isMobile()) return;
    $("panelPane").classList.add("open");
    $("drawerMask").hidden = false;
  }
  function closeDrawer() {
    if (!isMobile()) return;
    $("panelPane").classList.remove("open");
    $("drawerMask").hidden = true;
  }

  /* ================= 图片查看器（双指缩放） ================= */
  const V = { scale: 1, x: 0, y: 0, dragging: false, lastX: 0, lastY: 0, pinch: 0 };
  function openViewer(src) {
    const img = $("viewerImg");
    V.scale = 1;
    V.x = 0;
    V.y = 0;
    img.onload = () => applyViewer();
    img.src = src;
    $("viewer").hidden = false;
  }
  function closeViewer() {
    $("viewer").hidden = true;
    $("viewerImg").src = "";
  }
  function applyViewer() {
    const img = $("viewerImg");
    const nw = img.naturalWidth || 0;
    const nh = img.naturalHeight || 0;
    const fit = nw && nh ? Math.min(window.innerWidth / nw, window.innerHeight / nh, 1) : 1;
    img.style.transform = `translate(${V.x}px, ${V.y}px) scale(${fit * V.scale})`;
  }

  function bindViewer() {
    const el = $("viewer");
    const img = $("viewerImg");
    el.addEventListener("click", (e) => {
      if (e.target === el) closeViewer();
    });
    $("viewerClose").addEventListener("click", closeViewer);
    img.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      V.scale = V.scale > 1.5 ? 1 : 2.6;
      V.x = 0;
      V.y = 0;
      applyViewer();
    });
    el.addEventListener("wheel", (e) => {
      e.preventDefault();
      V.scale = Math.min(6, Math.max(0.6, V.scale * (e.deltaY < 0 ? 1.12 : 0.89)));
      applyViewer();
    }, { passive: false });

    el.addEventListener("touchstart", (e) => {
      if (e.touches.length === 2) {
        V.pinch = dist2(e.touches);
      } else if (e.touches.length === 1) {
        V.dragging = true;
        V.lastX = e.touches[0].clientX;
        V.lastY = e.touches[0].clientY;
      }
    }, { passive: true });

    el.addEventListener("touchmove", (e) => {
      if (e.touches.length === 2) {
        e.preventDefault();
        const d = dist2(e.touches);
        if (V.pinch > 0) {
          V.scale = Math.min(6, Math.max(0.6, V.scale * (d / V.pinch)));
          V.pinch = d;
          applyViewer();
        }
      } else if (V.dragging && e.touches.length === 1 && V.scale > 1) {
        e.preventDefault();
        V.x += e.touches[0].clientX - V.lastX;
        V.y += e.touches[0].clientY - V.lastY;
        V.lastX = e.touches[0].clientX;
        V.lastY = e.touches[0].clientY;
        applyViewer();
      }
    }, { passive: false });

    el.addEventListener("touchend", () => {
      V.dragging = false;
      V.pinch = 0;
    }, { passive: true });
  }
  function dist2(t) {
    const dx = t[0].clientX - t[1].clientX;
    const dy = t[0].clientY - t[1].clientY;
    return Math.hypot(dx, dy);
  }

  /* ================= 本地缓存（最近消息） ================= */
  function cachePush(evt) {
    try {
      const arr = JSON.parse(localStorage.getItem(CACHE_KEY) || "[]");
      arr.push({ ...evt, room: S.room, at: Date.now() });
      while (arr.length > CACHE_MAX) arr.shift();
      localStorage.setItem(CACHE_KEY, JSON.stringify(arr));
    } catch {
      /* 忽略配额错误 */
    }
  }

  function replayCache() {
    try {
      const arr = JSON.parse(localStorage.getItem(CACHE_KEY) || "[]");
      const recent = arr.filter((e) => e.room === S.room && Date.now() - e.at < 12 * 3600 * 1000).slice(-40);
      if (!recent.length) return;
      addSys("—— 以下是最近的记录 ——");
      recent.forEach((e) => {
        if (e.t === "chat") addChat(e.from, e.text, Math.floor(e.at / 1000), e.me);
        else if (e.t === "bot") addBotText(e.text, Math.floor(e.at / 1000));
      });
      addSys("—— 记录结束 ——");
    } catch {
      /* 忽略 */
    }
  }

  /* ================= 视口适配（软键盘） ================= */
  function setupViewport() {
    const vv = window.visualViewport;
    const app = $("app");
    const fit = () => {
      // 占位提示按屏幕宽度切换，窄屏不截断
      $("inMsg").placeholder = isMobile()
        ? "发消息或 /指令…"
        : "输入消息或指令，如 /我的信息（Enter 发送，Shift+Enter 换行）";
      if (!isMobile() || !vv) {
        app.style.height = "";
        return;
      }
      app.style.height = `${vv.height}px`;
      window.scrollTo(0, 0);
    };
    if (vv) {
      vv.addEventListener("resize", fit);
      vv.addEventListener("scroll", fit);
    }
    window.addEventListener("orientationchange", () => setTimeout(fit, 260));
    fit();
  }

  /* ================= 事件绑定 ================= */
  function bindEvents() {
    $("inName").value = localStorage.getItem("xiuxian_room_name") || "";
    $("inRoom").value = localStorage.getItem("xiuxian_room_id") || "room_main";
    $("btnJoin").addEventListener("click", join);
    $("inName").addEventListener("keydown", (e) => {
      if (e.key === "Enter") $("inRoom").focus();
    });
    $("inRoom").addEventListener("keydown", (e) => {
      if (e.key === "Enter") join();
    });

    const input = $("inMsg");
    const autosize = () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 132) + "px";
    };
    input.addEventListener("input", autosize);

    const submit = () => {
      const text = input.value.trim();
      if (!text) return;
      // 中文输入法组合中不发送
      if (input.dataset.composing === "1") return;
      if (text.startsWith("/")) dispatchCommand(text);
      else if (sendChat(text)) {
        /* 公屏消息由服务端回显 */
      }
      input.value = "";
      autosize();
      $("quickOptions").hidden = true;
    };

    input.addEventListener("compositionstart", () => (input.dataset.composing = "1"));
    input.addEventListener("compositionend", () => {
      input.dataset.composing = "0";
      autosize();
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submit();
      }
    });
    $("btnSend").addEventListener("click", submit);

    $("btnQuick").addEventListener("click", () => {
      switchPanelTab("cmds");
      if (isMobile()) openDrawer();
    });

    $("btnTogglePanel").addEventListener("click", () => {
      document.querySelector(".main").classList.toggle("panel-hidden");
    });

    // 面板 Tab
    document.querySelectorAll(".panel-tab").forEach((tab) => {
      tab.addEventListener("click", () => switchPanelTab(tab.dataset.tab));
    });

    // 移动端底部导航
    document.querySelectorAll(".mnav").forEach((btn) => {
      btn.addEventListener("click", () => {
        const nav = btn.dataset.nav;
        document.querySelectorAll(".mnav").forEach((b) => b.classList.toggle("active", b === btn));
        if (nav === "chat") {
          closeDrawer();
          return;
        }
        if (isMobile()) {
          switchPanelTab(nav);
          openDrawer();
        } else {
          switchPanelTab(nav);
        }
      });
    });

    $("drawerMask").addEventListener("click", closeDrawer);

    // 弹层
    $("sheetClose").addEventListener("click", closeSheet);
    $("sheetMask").addEventListener("click", (e) => {
      if (e.target === $("sheetMask")) closeSheet();
    });
    $("sheetOk").addEventListener("click", submitSheet);
    $("sheetInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submitSheet();
      }
    });

    // 播报横幅
    $("bannerClose").addEventListener("click", () => ($("banner").hidden = true));

    // 图片查看器
    bindViewer();

    // 页面回前台：自动重连
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && !S.connected && !S.manualClose && S.me) {
        connect();
      }
    });

    // 全局快捷键：Esc 关闭浮层
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!$("viewer").hidden) return closeViewer();
      if (!$("sheetMask").hidden) return closeSheet();
      closeDrawer();
    });
  }

  /* ================= 启动 ================= */
  bindEvents();

  // 支持 ?name=道号&room=房间 直接进入（书签/分享链接）
  const qs = new URLSearchParams(location.search);
  const autoName = qs.get("name") || qs.get("player");
  if (autoName) {
    $("inName").value = autoName;
    if (qs.get("room")) $("inRoom").value = qs.get("room");
    join();
  }

  window.addEventListener("resize", () => {
    if (!isMobile()) {
      $("panelPane").classList.remove("open");
      $("drawerMask").hidden = true;
      $("app").style.height = "";
    }
  });

  // 暴露少量调试接口
  window.__xiuxian = { sendCmd, dispatchCommand, openViewer };
})();
