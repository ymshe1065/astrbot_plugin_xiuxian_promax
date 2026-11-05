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
  const AUTH_KEY = "xiuxian_auth_v1";
  const ROOM = "room_main"; // 全服唯一聊天室

  /* ================= 状态 ================= */
  const S = {
    ws: null,
    me: "",                  // 昵称（显示名）
    username: "",            // 账号（玩家标识 web_<username>）
    token: "",
    room: ROOM,
    authMode: "login",
    seq: 0,
    pending: new Map(),      // req_id -> { cmd, panel }
    cards: new Map(),        // card id -> card
    panelCard: { status: null, bag: null },
    lastCmd: null,           // 用于刷新
    reconnectTimer: null,
    manualClose: false,
    connected: false,
    cmdCatalog: null,
    argSpec: null,           // 带参指令规格，来自 /api/commands
    unread: { status: 0, bag: 0 },
  };

  /* ================= 账号 ================= */
  function loadAuth() {
    try {
      return JSON.parse(localStorage.getItem(AUTH_KEY) || "null");
    } catch {
      return null;
    }
  }

  function saveAuth(a) {
    localStorage.setItem(AUTH_KEY, JSON.stringify(a));
  }

  function cacheKey() {
    return `${CACHE_KEY}_${S.username || "anon"}`;
  }

  /** 名字 → 稳定的头像底色 */
  function avatarColor(name) {
    const palette = ["#b5654a", "#7a8b5a", "#5d7f9e", "#9b6b9e", "#a8843f", "#4f8b7d", "#8b6b4a", "#6b7fa8"];
    let h = 0;
    for (const ch of String(name || "我")) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return palette[h % palette.length];
  }

  /** 取首字做头像 */
  function initial(name) {
    const s = String(name || "我").trim();
    return s ? Array.from(s)[0].toUpperCase() : "我";
  }

  function updateMeUI() {
    const n = S.me || "我";
    $("meNick").textContent = n;
    $("meDot").textContent = initial(n);
    $("meDot").style.background = avatarColor(n);
    $("accNickShow").textContent = n;
    $("accUser").textContent = "@" + (S.username || "");
    $("accDot").textContent = initial(n);
    $("accDot").style.background = avatarColor(n);
    $("accNick").value = n;
  }

  function setAuthMode(mode) {
    S.authMode = mode;
    document.querySelectorAll(".auth-tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.mode === mode);
    });
    $("nickField").hidden = mode !== "register";
    $("btnAuth").textContent = mode === "register" ? "注 册 并 进 入" : "登 入";
    $("inPwd").setAttribute("autocomplete", mode === "register" ? "new-password" : "current-password");
    $("loginErr").textContent = "";
  }

  async function submitAuth() {
    const username = $("inUser").value.trim();
    const password = $("inPwd").value;
    const nick = $("inNick").value.trim();
    const fail = (msg) => ($("loginErr").textContent = msg);

    if (!username) return fail("请先报上道号");
    if (!password) return fail("请填写密码");
    if (S.authMode === "register" && password.length < 6) return fail("密码至少 6 位");

    $("btnAuth").disabled = true;
    try {
      const path = S.authMode === "register" ? "/api/register" : "/api/login";
      const r = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password, nick }),
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) return fail(d.error || "登录失败，稍后再试");
      saveAuth({ token: d.token, username: d.username, nick: d.nick });
      enterRoom(d);
    } catch {
      fail("网络异常，稍后再试");
    } finally {
      $("btnAuth").disabled = false;
    }
  }

  function enterRoom(auth) {
    S.token = auth.token;
    S.username = auth.username;
    S.me = auth.nick || auth.username;
    S.room = ROOM;
    $("loginErr").textContent = "";
    $("login").hidden = true;
    $("login").style.display = "none";
    $("app").hidden = false;
    updateMeUI();
    connect();
    loadCatalog();
    setupViewport();
    replayCache();
  }

  function logout(msg = "") {
    S.manualClose = true;
    if (S.ws) {
      try { S.ws.close(); } catch { /* ignore */ }
      S.ws = null;
    }
    clearTimeout(S.reconnectTimer);
    localStorage.removeItem(AUTH_KEY);
    localStorage.removeItem(cacheKey());
    S.me = "";
    S.username = "";
    S.token = "";
    S.connected = false;
    $("accMask").hidden = true;
    $("app").hidden = true;
    $("login").hidden = false;
    $("login").style.display = "";
    $("inPwd").value = "";
    setAuthMode("login");
    $("loginErr").textContent = msg;
  }

  /** token 是否还有效（网络异常时按有效处理，避免误踢） */
  async function sessionAlive() {
    try {
      const r = await fetch("/api/me", { headers: { Authorization: "Bearer " + S.token } });
      return r.ok;
    } catch {
      return true;
    }
  }

  function openAccount() {
    updateMeUI();
    $("accErr").textContent = "";
    $("accOld").value = "";
    $("accNew").value = "";
    $("accMask").hidden = false;
  }

  async function saveNick() {
    const nick = $("accNick").value.trim();
    if (!nick) return ($("accErr").textContent = "昵称不能为空");
    if (nick === S.me) return ($("accErr").textContent = "昵称没有变化");
    try {
      const r = await fetch("/api/nick", {
        method: "POST",
        headers: { Authorization: "Bearer " + S.token, "Content-Type": "application/json" },
        body: JSON.stringify({ nick }),
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) return ($("accErr").textContent = d.error || "保存失败");
      S.me = d.nick;
      updateMeUI();
      $("accMask").hidden = true;
      toast("昵称已更新");
    } catch {
      $("accErr").textContent = "网络异常，稍后再试";
    }
  }

  async function savePwd() {
    const oldPwd = $("accOld").value;
    const newPwd = $("accNew").value;
    if (!oldPwd || !newPwd) return ($("accErr").textContent = "请填写原密码和新密码");
    if (newPwd.length < 6) return ($("accErr").textContent = "新密码至少 6 位");
    try {
      const r = await fetch("/api/password", {
        method: "POST",
        headers: { Authorization: "Bearer " + S.token, "Content-Type": "application/json" },
        body: JSON.stringify({ old: oldPwd, new: newPwd }),
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) return ($("accErr").textContent = d.error || "修改失败");
      $("accOld").value = "";
      $("accNew").value = "";
      $("accErr").textContent = "";
      toast("密码已修改");
    } catch {
      $("accErr").textContent = "网络异常，稍后再试";
    }
  }

  /* ================= 连接 ================= */
  let failCount = 0;

  function connect() {
    S.manualClose = false;
    setConn("连接中…", false);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws?token=${encodeURIComponent(S.token)}`;
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
      failCount = 0;
      setConn("已连接", true);
      refreshOnline();
      // 连接就绪后再拉看板（首次连接与断线重连都走这里）
      loadPanel("status");
    };
    ws.onclose = async (e) => {
      S.connected = false;
      setConn("已断开", false);
      if (S.manualClose) return;
      if (e.code === 4001) {
        addSys("账号在其他地方登录，本连接已断开");
        return;
      }
      failCount += 1;
      // 连续失败先确认登录态，过期就直接回登录页，不要无限重连
      if (failCount >= 2 && !(await sessionAlive())) {
        logout("登录已过期，请重新登录");
        return;
      }
      addSys("连接断开，正在重连…");
      scheduleReconnect();
    };
    ws.onerror = () => setConn("连接异常", false);
    ws.onmessage = onMessage;
  }

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

  function setOnline(list) {
    $("onlineTag").textContent = `在线 ${(list || []).length}`;
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
        if (m.nick) { S.me = m.nick; updateMeUI(); }
        if (m.username) S.username = m.username;
        setOnline(m.online);
        addSys(`欢迎 ${S.me} 来到修仙聊天室`);
        refreshOnline();
        break;
      case "chat":
        // 用账号判断是不是自己，昵称可以随时改
        addChat(m.from, m.text, m.ts, m.username ? m.username === S.username : m.from === S.me);
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
        setOnline(m.players);
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
    const row = document.createElement("div");
    row.className = "msg-row" + (isMe ? " right" : "");
    row.innerHTML =
      `<div class="avatar" style="background:${avatarColor(from)}">${esc(initial(from))}</div>` +
      `<div class="msg-col">` +
      `<div class="who">${esc(from)}<span class="time">${fmtTime(ts)}</span></div>` +
      `<div class="bubble">${escapeWithMarks(text)}</div>` +
      `</div>`;
    appendNode(row);
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
    const row = document.createElement("div");
    row.className = "msg-row bot-row";
    const tone = classifyTone(text);
    if (tone) row.classList.add(tone);

    const col = document.createElement("div");
    col.className = "msg-col";
    let html = `<div class="who bot-who">仙人<span class="time">${fmtTime(ts)}</span></div>`;
    html += `<div class="bubble">`;
    if (text) html += escapeWithMarks(text);
    (images || []).forEach((u) => {
      html += `<img src="${esc(u)}" data-full="${esc(u)}" alt="">`;
    });
    html += `</div>`;
    col.innerHTML = html;

    row.innerHTML = `<div class="avatar bot-avatar">☯</div>`;
    row.appendChild(col);
    bindImages(row);
    appendNode(row, { force: true });
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
    // 卡片正文/页脚里已成按钮的命令，避免和卡脚动作重复
    const tmplCmds = card._tmplCmds || (card._tmplCmds = new Set());
    if (card.footer || actions.length || card.raw) {
      const foot = document.createElement("div");
      foot.className = "card-foot";
      if (card.footer) {
        String(card.footer)
          .split("\n")
          .forEach((ln) => {
            const tmpl = parseTmplLine(ln);
            if (tmpl) {
              tmpl.items.forEach((it) => tmplCmds.add(it.cmd));
              foot.appendChild(renderTmplLine(tmpl));
              return;
            }
            const t = document.createElement("div");
            t.innerHTML = valueHtml(ln);
            foot.appendChild(t);
          });
      }
      const rest = actions.filter((a) => !tmplCmds.has(a.cmd)).slice(0, 8);
      if (rest.length) {
        const bar = document.createElement("div");
        bar.className = "actions";
        rest.forEach((a) => {
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
        // 「指令：/行情 <代码>　/买入股 <代码> <手>」被解析成键值对，这里还原成按钮行
        const tmpl = parseTmplLine(String(v ?? ""));
        if (tmpl) {
          tmpl.items.forEach((it) => {
            if (!card._tmplCmds) card._tmplCmds = new Set();
            card._tmplCmds.add(it.cmd);
          });
          const wrap = document.createElement("div");
          wrap.className = "card-row tmpl-row";
          const kEl = document.createElement("div");
          kEl.className = "k";
          kEl.textContent = k;
          wrap.appendChild(kEl);
          wrap.appendChild(renderTmplLine(tmpl));
          list.appendChild(wrap);
          return;
        }
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
        // 「指令：/行情 <代码>　/买入股 <代码> <手>」这类模板行拆成可点按钮
        const tmpl = parseTmplLine(ln);
        if (tmpl) {
          box.appendChild(renderTmplLine(tmpl));
          return;
        }
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

  /**
   * 指令模板行解析：`指令：/行情 <代码>　/买入股 <代码> <手>`
   * 命令间常用全角空格分隔，且可能已带具体代码（/买入股 S01 <手数>）。
   * 至少两个命令才算模板行，否则交给普通文本渲染。
   */
  const TMPL_CMD_RE = /[/／]([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]{1,11})/g;
  function parseTmplLine(line) {
    const hits = [...String(line).matchAll(TMPL_CMD_RE)];
    if (hits.length < 2) return null;
    const items = hits.map((h, i) => {
      const from = h.index + h[0].length;
      const to = i + 1 < hits.length ? hits[i + 1].index : line.length;
      const args = line.slice(from, to).replace(/[\s\u3000]+/g, " ").trim();
      return { cmd: `/${h[1]}`, label: h[1], args };
    });
    const note = line
      .slice(0, hits[0].index)
      .replace(/^[·•\s\u3000]+/, "")
      .replace(/[：:，,。\s\u3000]+$/, "")
      .trim();
    return { items, note };
  }

  function renderTmplLine(tmpl) {
    const box = document.createElement("div");
    box.className = "tmpl-line";
    if (tmpl.note) {
      const n = document.createElement("span");
      n.className = "tmpl-note";
      n.textContent = tmpl.note;
      box.appendChild(n);
    }
    tmpl.items.forEach((it) => {
      const b = document.createElement("button");
      b.className = "tmpl-btn";
      b.textContent = it.label;
      b.addEventListener("click", () => {
        // 行里已带具体代码（/买入股 S01）→ 直接开对应动作并预填
        const code = (it.args.match(/[SF]\d{2,3}/) || [])[0];
        if (code && /行情|买入股|卖出股|申购|赎回/.test(it.label)) {
          openEntitySheet(code, it.cmd);
          return;
        }
        dispatchCommand(it.cmd);
      });
      box.appendChild(b);
    });
    return box;
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
    // 含股票/基金代码的文本走实体链接化，点代码可直接发起操作
    if (/[SF]\d{2,3}/.test(s)) return linkifyEntities(s);
    return highlightNumbers(s);
  }

  /** 把文本里的 S01 / F01 这类代码变成可点实体（中文后无 \b，用显式边界） */
  const ENTITY_RE = /(^|[^A-Za-z0-9_])([SF]\d{2,3})(?![A-Za-z0-9_])/g;
  function linkifyEntities(text) {
    const raw = String(text ?? "");
    let out = "";
    let last = 0;
    let m;
    ENTITY_RE.lastIndex = 0;
    while ((m = ENTITY_RE.exec(raw)) !== null) {
      const code = m[2];
      const start = m.index + m[1].length;
      out += esc(raw.slice(last, start));
      out += `<button class="entity-chip" data-code="${esc(code)}">${esc(code)}</button>`;
      last = start + code.length;
    }
    out += esc(raw.slice(last));
    return out;
  }

  /** 点卡片里的 S01/F01 → 弹层预填代码，可在对应动作间切换 */
  function openEntitySheet(code, cmd = "") {
    const isFund = /^F/i.test(code);
    const actions = isFund
      ? [
          { label: "行情", cmd: "/行情", hint: "基金代码", example: code },
          { label: "申购", cmd: "/申购", hint: "基金代码 金额", example: `${code} 10000` },
          { label: "赎回", cmd: "/赎回", hint: "基金代码 份额", example: `${code} 100` },
        ]
      : [
          { label: "行情", cmd: "/行情", hint: "股票代码", example: code },
          { label: "买入股", cmd: "/买入股", hint: "股票代码 手数（1 手 = 100 股）", example: `${code} 2` },
          { label: "卖出股", cmd: "/卖出股", hint: "股票代码 手数", example: `${code} 1` },
        ];
    const cur = actions.find((a) => a.cmd === cmd) || actions[1];
    openSheet(cur.label, `${cur.hint}，例：${cur.example}`, cur.cmd, code, {
      example: cur.example,
      actions,
    });
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
      ["详情", `/物品信息 ${name}`],
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
        if (needArg) {
          // 物品菜单点「赠予」：先选对象，选完自动把当前物品带进第二段
          openSheet("赠予 物品", "选择赠予对象", "/赠予", "", {
            seed: { index: 1, value: name },
          });
        } else dispatchCommand(cmd);
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
  function dispatchCommand(cmd, opts = {}) {
    if (!cmd) return;
    const text = cmd.startsWith("/") ? cmd : `/${cmd}`;

    // 带参指令先补参数，别把半截命令发出去换一句报错
    if (!opts.noCheck) {
      const miss = missingArgs(text);
      if (miss) {
        openSheet(miss.name, miss.hint, `/${miss.name}`, miss.preset, { example: miss.example });
        return;
      }
    }

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

  /** 命令是否缺必填参数；缺则返回弹层所需信息 */
  function missingArgs(text) {
    const spec = S.argSpec || {};
    const parts = text.trim().split(/\s+/);
    const name = parts[0].replace(/^\//, "");
    const rule = spec[name];
    if (!rule) return null;
    const have = parts.length - 1;
    const need = rule.min || 1;
    if (have >= need) return null;
    return {
      name,
      hint: rule.example ? `${rule.hint}，例：${rule.example}` : rule.hint,
      preset: have ? parts.slice(1).join(" ") : "",
      example: rule.example || "",
    };
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
      S.argSpec = S.cmdCatalog.arg_spec || {};
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
            if (it.arg) {
              const rule = (S.argSpec || {})[it.cmd.replace(/^\//, "")];
              openSheet(it.label, rule ? `${rule.hint}，例：${rule.example}` : it.arg, it.cmd);
            } else {
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

  /* ================= 参数弹层 =================
   * 分两段：
   * 1) 后端 /api/arg_options 给出当前字段的真实候选（储物戒装备、职业、宗门成员…），
   *    点候选即写入该字段并自动进入下一段；
   * 2) 没有候选的字段（金额、数量等）保留手动输入。
   */
  let sheetCtx = null;
  let sheetSeq = 0;
  const SHEET_PICK_LIMIT = 12;

  function normalizeCmd(cmd) {
    const text = String(cmd || "").trim();
    if (!text) return "";
    return text.startsWith("/") ? text : `/${text}`;
  }

  function sheetApi() {
    return (S.cmdCatalog && S.cmdCatalog.arg_spec) || S.argSpec || {};
  }

  function openSheet(title, hint, cmd, preset = "", opts = {}) {
    const { example = "", actions = [], seed = null } = opts || {};
    const cmdText = normalizeCmd(cmd);
    const presetValues = String(preset || "").trim()
      ? String(preset).trim().split(/\s+/).filter(Boolean)
      : [];
    sheetCtx = {
      title,
      hint: hint || "",
      cmd: cmdText,
      name: cmdText.replace(/^\//, ""),
      values: presetValues,
      chosen: presetValues.map((v) => ({ value: v, label: v })),
      data: null,
      example,
      actions,
      seed,
      expanded: false,
      filter: "",
    };
    sheetSeq += 1;
    $("sheetTitle").textContent = title;
    $("sheetHint").textContent = hint ? `填写：${hint}` : "";
    $("sheetInput").value = "";
    $("sheetFilter").value = "";
    $("sheetInput").style.display = "";
    $("sheetChosen").innerHTML = "";
    $("sheetChosen").hidden = true;
    $("sheetPicks").hidden = true;
    $("sheetOptList").innerHTML = "";
    $("sheetMore").hidden = true;
    $("sheetSkip").hidden = true;
    $("sheetExample").hidden = !example;
    $("sheetExample").onclick = () => {
      $("sheetInput").value = example;
      $("sheetInput").focus();
      $("sheetInput").setSelectionRange(example.length, example.length);
    };

    // 动作切换：点卡片实体后可换 行情/买入/卖出，已填代码保留
    const acts = $("sheetActs");
    acts.innerHTML = "";
    if (actions.length) {
      actions.forEach((a) => {
        const b = document.createElement("button");
        b.className = "act-chip" + (normalizeCmd(a.cmd) === cmdText ? " on" : "");
        b.textContent = a.label;
        b.addEventListener("click", () => {
          openSheet(a.label, `${a.hint}，例：${a.example}`, a.cmd, preset, {
            example: a.example,
            actions,
          });
        });
        acts.appendChild(b);
      });
      acts.hidden = false;
    } else {
      acts.hidden = true;
    }

    $("sheetMask").hidden = false;
    loadSheetField();
  }

  function closeSheet() {
    sheetSeq += 1;
    $("sheetMask").hidden = true;
    sheetCtx = null;
    $("sheetInput").blur();
  }

  async function loadSheetField() {
    const ctx = sheetCtx;
    if (!ctx) return;
    const seq = ++sheetSeq;
    $("sheetInput").value = "";
    $("sheetInput").placeholder = "加载可选内容…";
    try {
      const qs = new URLSearchParams({
        cmd: ctx.cmd,
        values: JSON.stringify(ctx.values),
      });
      const r = await fetch(`/api/arg_options?${qs.toString()}`, {
        headers: { Authorization: "Bearer " + S.token },
      });
      const data = await r.json().catch(() => null);
      if (seq !== sheetSeq || sheetCtx !== ctx) return;
      if (!data || data.ok === false) {
        if (data && data.reason === "no_options") {
          applyManualSheet(ctx, data);
        } else {
          applyManualSheet(ctx, data || {});
          $("sheetHint").textContent = (data && data.error) || "候选加载失败，可直接手动输入";
        }
        return;
      }
      ctx.data = data;

      // 从物品菜单点「赠予」时，选完对象后自动补上物品
      if (ctx.seed && !ctx.seed.used && ctx.seed.index === ctx.values.length && ctx.seed.value) {
        ctx.seed.used = true;
        ctx.values.push(ctx.seed.value);
        ctx.chosen.push({ value: ctx.seed.value, label: ctx.seed.value });
        await loadSheetField();
        return;
      }

      if (data.done) {
        renderSheetDone(ctx, data);
        return;
      }
      renderSheetField(ctx, data);
    } catch {
      if (seq !== sheetSeq || sheetCtx !== ctx) return;
      applyManualSheet(ctx, {});
      $("sheetHint").textContent = "网络异常，可直接手动输入";
    }
  }

  function renderChosen(ctx) {
    const box = $("sheetChosen");
    box.innerHTML = "";
    if (!ctx.chosen.length) {
      box.hidden = true;
      return;
    }
    ctx.chosen.forEach((item, idx) => {
      const chip = document.createElement("button");
      chip.className = "chosen-chip";
      chip.innerHTML = `<span>${esc(item.label)}</span><b>×</b>`;
      chip.title = "点此撤销该参数及其后的选择";
      chip.addEventListener("click", () => {
        ctx.values = ctx.values.slice(0, idx);
        ctx.chosen = ctx.chosen.slice(0, idx);
        ctx.expanded = false;
        ctx.filter = "";
        $("sheetFilter").value = "";
        loadSheetField();
      });
      box.appendChild(chip);
    });
    box.hidden = false;
  }

  function renderSheetField(ctx, data) {
    renderChosen(ctx);
    const field = data.field || {};
    const options = data.options || [];
    const label = field.label || data.hint || ctx.hint || "参数";
    $("sheetHint").textContent = `第 ${data.index + 1} 步：${label}` + (data.message ? ` · ${data.message}` : "");
    $("sheetInput").placeholder = options.length ? `也可手动输入${label}` : label;
    $("sheetInput").style.display = "";
    $("sheetSkip").hidden = !field.optional;
    // 全局示例只在第一段/纯手动指令出现，避免把「炼气丹 5」塞进数量字段
    const showExample = !options.length && !!ctx.example && Number(data.index || 0) === 0;
    $("sheetExample").hidden = !showExample;
    const isLast = !!field.key && Number(data.index || 0) + 1 >= Number(data.total || 1);
    $("sheetOk").textContent = field.optional || isLast ? "发 送" : "下 一 步";

    const picks = $("sheetPicks");
    if (!options.length) {
      picks.hidden = true;
      $("sheetPickCount").textContent = "";
      $("sheetMore").hidden = true;
      if (data.message) $("sheetHint").textContent += `（${data.message}）`;
      return;
    }
    picks.hidden = false;
    $("sheetPickLabel").textContent = label;
    drawSheetOptions(ctx);
  }

  function drawSheetOptions(ctx) {
    const data = ctx.data || {};
    const all = data.options || [];
    const kw = String(ctx.filter || "").trim().toLowerCase();
    const list = kw
      ? all.filter((o) =>
          `${o.label || ""} ${o.value || ""} ${o.desc || ""} ${o.group || ""}`.toLowerCase().includes(kw)
        )
      : all;
    const shown = ctx.expanded ? list : list.slice(0, SHEET_PICK_LIMIT);

    $("sheetPickCount").textContent = kw ? `${shown.length}/${all.length} 项` : `共 ${all.length} 项`;
    const box = $("sheetOptList");
    box.innerHTML = "";
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "picks-empty";
      empty.textContent = kw ? "没有匹配的候选，可直接手动输入" : (data.message || "暂无可选内容，可手动输入");
      box.appendChild(empty);
      $("sheetMore").hidden = true;
      return;
    }
    shown.forEach((opt) => {
      const b = document.createElement("button");
      b.className = "pick-item";
      b.innerHTML =
        `<span class="pick-main"><span class="pick-label">${esc(opt.label || opt.value)}</span>` +
        (opt.desc ? `<span class="pick-desc">${esc(opt.desc)}</span>` : "") +
        `</span><span class="pick-go">›</span>`;
      b.addEventListener("click", () => pickSheetOption(opt));
      box.appendChild(b);
    });
    const more = $("sheetMore");
    more.hidden = list.length <= SHEET_PICK_LIMIT;
    more.textContent = ctx.expanded ? "收起" : `展开全部 ${list.length} 条 ›`;
    more.onclick = () => {
      ctx.expanded = !ctx.expanded;
      drawSheetOptions(ctx);
    };
  }

  function applyManualSheet(ctx, data) {
    renderChosen(ctx);
    const field = (data && data.field) || {};
    const label = field.label || ctx.hint || "参数";
    $("sheetHint").textContent = data.message ? `${label} · ${data.message}` : (ctx.hint ? `填写：${ctx.hint}` : label);
    $("sheetInput").style.display = "";
    $("sheetInput").placeholder = label;
    $("sheetPicks").hidden = true;
    $("sheetMore").hidden = true;
    $("sheetSkip").hidden = true;
    const showExample = !!ctx.example && Number((data && data.index) || 0) === 0;
    $("sheetExample").hidden = !showExample;
    $("sheetOk").textContent = "发 送";
  }

  function renderSheetDone(ctx, data) {
    renderChosen(ctx);
    $("sheetHint").textContent = data.message || "参数已填完，可以发送";
    $("sheetInput").style.display = "none";
    $("sheetPicks").hidden = true;
    $("sheetMore").hidden = true;
    $("sheetSkip").hidden = true;
    $("sheetExample").hidden = true;
    $("sheetOk").textContent = "发 送";
  }

  function pickSheetOption(opt) {
    const ctx = sheetCtx;
    if (!ctx) return;
    ctx.values.push(String(opt.value));
    ctx.chosen.push({ value: String(opt.value), label: opt.label || opt.value });
    ctx.expanded = false;
    ctx.filter = "";
    $("sheetFilter").value = "";
    loadSheetField().then(() => {
      if (sheetCtx === ctx && ctx.data && ctx.data.done) submitSheet({ auto: true });
    });
  }

  function submitSheet(opts = {}) {
    const ctx = sheetCtx;
    if (!ctx) return;
    const data = ctx.data || {};
    const field = data.field || null;

    if (!data.done) {
      const val = $("sheetInput").value.trim();
      if (val) {
        ctx.values.push(val);
        ctx.chosen.push({ value: val, label: val });
        ctx.expanded = false;
        ctx.filter = "";
        $("sheetFilter").value = "";
        // 有字段计划时继续拉下一段；没有计划（纯手动指令）直接发送
        if (field) {
          loadSheetField().then(() => {
            if (sheetCtx === ctx && ctx.data && ctx.data.done) submitSheet({ auto: true });
          });
          return;
        }
      } else if (field && !field.optional) {
        toast(`请选择或填写：${field.label || "参数"}`);
        return;
      } else if (!field) {
        toast("请填写内容");
        return;
      }
    }

    const need = Number(data.min || (sheetApi()[ctx.name] || {}).min || 1);
    if (ctx.values.length < need) {
      toast("参数还没填完");
      return;
    }
    const text = `${ctx.cmd} ${ctx.values.join(" ")}`.trim();
    closeSheet();
    closeDrawer();
    dispatchCommand(text, { noCheck: true });
    if (opts.auto) toast("已按所选参数发送");
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
    options.slice(0, 12).forEach((o) => {
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

  /* ================= 本地缓存（最近消息，按账号隔离） ================= */
  function cachePush(evt) {
    try {
      const key = cacheKey();
      const arr = JSON.parse(localStorage.getItem(key) || "[]");
      arr.push({ ...evt, room: S.room, at: Date.now() });
      while (arr.length > CACHE_MAX) arr.shift();
      localStorage.setItem(key, JSON.stringify(arr));
    } catch {
      /* 忽略配额错误 */
    }
  }

  function replayCache() {
    if (!S.username) return;
    try {
      const arr = JSON.parse(localStorage.getItem(cacheKey()) || "[]");
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

  /* ================= 主题 =================
   * xian 宣纸玉简（默认） / fish 大肥鱼·浅蓝鱼缸
   * 只切 data-theme，视觉全交给 CSS 变量；这里只管文案、图标和持久化。
   * 大肥鱼的 logo 不写在文案里，交给 CSS 背景图（brand/mark 留空即可）。
   */
  const THEME_KEY = "xiuxian_theme_v1";
  const FAVICON = {
    xian: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='16' r='13' fill='%23a33b2a'/%3E%3C/svg%3E",
    fish: "/static/img/whale_favicon.png",
  };
  const THEMES = {
    xian: {
      name: "宣纸玉简",
      next: "大肥鱼",
      meta: "#f4eee1",
      brand: "☯",
      mark: "☯",
      sub: "聚灵谷 · 同道共聚一室",
      btn: "",
    },
    fish: {
      name: "大肥鱼 · 浅蓝鱼缸",
      next: "宣纸玉简",
      meta: "#eef4ff",
      brand: "",
      mark: "",
      sub: "一缸浅水 · 一起摸鱼吃饭",
      btn: "☯",
    },
  };
  let theme = "xian";

  function applyTheme(name, persist) {
    theme = THEMES[name] ? name : "xian";
    const t = THEMES[theme];
    document.documentElement.setAttribute("data-theme", theme);
    document.body.dataset.theme = theme;
    if ($("metaTheme")) $("metaTheme").setAttribute("content", t.meta);
    if ($("brandMark")) $("brandMark").textContent = t.brand;
    if ($("loginMark")) $("loginMark").textContent = t.mark;
    if ($("loginSub")) $("loginSub").textContent = t.sub;
    if ($("favicon")) $("favicon").setAttribute("href", FAVICON[theme]);
    if ($("btnTheme")) {
      $("btnTheme").textContent = t.btn;
      $("btnTheme").title = "切换到" + t.next;
    }
    if ($("loginTheme")) $("loginTheme").textContent = "换个皮肤 · " + t.next;
    document.querySelectorAll(".theme-opt").forEach((el) => {
      el.classList.toggle("on", el.dataset.themeVal === theme);
    });
    if (persist) {
      try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* 隐私模式 */ }
    }
  }

  function toggleTheme() {
    applyTheme(theme === "xian" ? "fish" : "xian", true);
  }

  function initTheme() {
    let start = "xian";
    try {
      const saved = localStorage.getItem(THEME_KEY);
      if (saved === "fish" || saved === "xian") start = saved;
    } catch (e) { /* ignore */ }
    applyTheme(start, false);
  }

  /* ================= 事件绑定 ================= */
  function bindEvents() {
    // 登录 / 注册
    document.querySelectorAll(".auth-tab").forEach((t) => {
      t.addEventListener("click", () => setAuthMode(t.dataset.mode));
    });
    $("btnAuth").addEventListener("click", submitAuth);
    [$("inUser"), $("inPwd"), $("inNick")].forEach((el) => {
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter") submitAuth();
      });
    });

    // 账号设置
    $("btnMe").addEventListener("click", openAccount);
    $("accClose").addEventListener("click", () => ($("accMask").hidden = true));
    $("accMask").addEventListener("click", (e) => {
      if (e.target === $("accMask")) $("accMask").hidden = true;
    });
    $("accSaveNick").addEventListener("click", saveNick);
    $("accSavePwd").addEventListener("click", savePwd);
    $("accLogout").addEventListener("click", () => logout("已退出登录"));

    // 主题切换：顶栏快捷按钮 / 登录页小字 / 账号设置里的色块
    $("btnTheme").addEventListener("click", toggleTheme);
    $("loginTheme").addEventListener("click", toggleTheme);
    document.querySelectorAll(".theme-opt").forEach((el) => {
      el.addEventListener("click", () => applyTheme(el.dataset.themeVal, true));
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
    $("sheetOk").addEventListener("click", () => submitSheet());
    $("sheetSkip").addEventListener("click", () => submitSheet());
    $("sheetFilter").addEventListener("input", (e) => {
      if (!sheetCtx) return;
      sheetCtx.filter = e.target.value;
      drawSheetOptions(sheetCtx);
    });
    $("sheetInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submitSheet();
      }
    });

    // 卡片内实体（S01/F01）：点一下直接带出代码
    document.addEventListener("click", (e) => {
      const chip = e.target.closest && e.target.closest(".entity-chip");
      if (chip && chip.dataset.code) openEntitySheet(chip.dataset.code);
    });

    // 播报横幅
    $("bannerClose").addEventListener("click", () => ($("banner").hidden = true));

    // 图片查看器
    bindViewer();

    // 页面回前台：自动重连
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && !S.connected && !S.manualClose && S.token) {
        connect();
      }
    });

    // 全局快捷键：Esc 关闭浮层
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!$("viewer").hidden) return closeViewer();
      if (!$("sheetMask").hidden) return closeSheet();
      if (!$("accMask").hidden) return ($("accMask").hidden = true);
      closeDrawer();
    });
  }

  /* ================= 启动 ================= */
  bindEvents();
  initTheme();

  // 有登录态就自动进入，否则停在登录页
  (async () => {
    setAuthMode("login");
    const saved = loadAuth();
    if (!saved || !saved.token) return;
    try {
      const r = await fetch("/api/me", {
        headers: { Authorization: "Bearer " + saved.token },
      });
      if (!r.ok) {
        localStorage.removeItem(AUTH_KEY);
        return;
      }
      const me = await r.json();
      enterRoom({ token: saved.token, username: me.username, nick: me.nick });
    } catch {
      /* 服务未就绪时留在登录页 */
    }
  })();

  // ?u=账号 预填道号（书签用）
  const qs = new URLSearchParams(location.search);
  if (qs.get("u")) $("inUser").value = qs.get("u");

  window.addEventListener("resize", () => {
    if (!isMobile()) {
      $("panelPane").classList.remove("open");
      $("drawerMask").hidden = true;
      $("app").style.height = "";
    }
  });

  // 暴露少量调试接口
  window.__xiuxian = {
    sendCmd,
    dispatchCommand,
    openViewer,
    setTheme: applyTheme,
    getTheme: () => theme,
  };
})();
