/* 修仙聊天室 · 前端主逻辑
 * - WebSocket 通信（公屏 chat / 私聊 reply）
 * - 结构化卡片原生渲染（替代 276KB 位图）
 * - 看板：状态 / 储物戒 / 指令格；移动端为底部抽屉
 */
(() => {
  "use strict";

  // 启动时打印自身版本（= index.html 里的 ?v=mtime），便于判断「改了却没生效」
  try {
    const src = document.currentScript && document.currentScript.src;
    const ver = src ? new URL(src).searchParams.get("v") : "";
    console.info(`[webroom] app.js v=${ver || "?"} loaded`);
  } catch (_) {
    /* 老浏览器忽略 */
  }

  const $ = (id) => document.getElementById(id);
  const isMobile = () => window.matchMedia("(max-width: 900px)").matches;
  const CACHE_KEY = "xiuxian_room_cache_v1";
  const CACHE_MAX = 80;
  // 长消息折叠：字数或行数任一超标就折起来
  const FOLD_KEY = "xiuxian_fold_all_v1"; // 顶栏「全部折叠」的开关状态
  const CMD_FOLD_KEY = "xiuxian_cmd_fold_v1"; // 指令分类的折叠状态（会话级临时保存）
  const FOLD_CHARS = 120;
  const FOLD_LINES = 7;
  const AUTH_KEY = "xiuxian_auth_v1";
  const ROOM = "room_main"; // 主频道「原始仙界」
  const BOT_NAME = "大肥鱼"; // 机器人显示名（聊天气泡里的落款）
  // 内置头像，与后端 PRESET_AVATARS 保持一致
  const PRESETS = ["taiji", "sword", "cloud", "moon", "lotus", "bamboo", "cauldron", "fish", "star"];

  /* ================= 状态 ================= */
  const S = {
    ws: null,
    me: "",                  // 昵称（显示名）
    username: "",            // 账号（玩家标识 web_<username>）
    avatar: "",              // 头像 URL（空 = 昵称首字）
    online: [],              // 在线名单 [{username, nick, avatar}]
    token: "",
    room: ROOM,
    channel: null,           // 当前仙界 { id, name, public, owner, is_mine, uuid... }
    channels: { joined: [], public: [] },
    channelSearch: "",
    channelManageId: "",     // 正在展开管理的仙界 id
    switchPending: "",
    authMode: "login",
    seq: 0,
    pending: new Map(),      // req_id -> { cmd, panel }
    cards: new Map(),        // card id -> card
    panelCard: { status: null, bag: null },
    bagDirty: true,          // 储物戒在别处发生变更，切回看板时需要重新拉取
    lastCmd: null,           // 用于刷新
    reconnectTimer: null,
    manualClose: false,
    connected: false,
    cmdCatalog: null,
    argSpec: null,           // 带参指令规格，来自 /api/commands
    unread: { status: 0, bag: 0 },
    msgSeq: 0,               // 本地消息临时 id（指令回显等不上公屏的消息）
    settings: {              // 服务端下发的消息可见性配置
      manual_visibility: "public",
      panel_visibility: "self",
      auto_visibility: "self",
      battle_detail_visibility: "public",
      battle_detail_default: "collapsed",
      view_filter: "all",
    },
    unreadNotices: 0,        // 个人消息未读数
    notices: [],             // 个人消息历史
    noticesOpen: false,
    feedbackScope: "mine",   // 意见反馈：mine / all（管理员）
    feedbackAdmin: false,    // 当前账号是否反馈管理员
    stickers: [],            // 内置表情包 [{id,label,url}]
    stickerLoaded: false,    // 是否已拉取表情目录
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

  /** 头像内联样式：有图用背景图，没图用色彩 + 首字 */
  function avatarStyle(name, avatar) {
    // 注意用单引号：这串会作为 style="..." 的值写进 HTML，双引号会提前闭合属性
    if (avatar) return `background-image:url('${esc(avatar)}')`;
    return `background:${avatarColor(name)}`;
  }

  function avatarHtml(name, avatar, cls = "") {
    const inner = avatar ? "" : esc(initial(name));
    return `<div class="avatar ${cls}${avatar ? " has-img" : ""}" style="${avatarStyle(name, avatar)}">${inner}</div>`;
  }

  /** 把已有的头像元素（顶栏/账号弹层）刷成新头像 */
  function paintAvatar(el, name, avatar) {
    if (!el) return;
    if (avatar) {
      el.classList.add("has-img");
      el.textContent = "";
      el.style.background = "";
      el.style.backgroundImage = `url("${avatar}")`;
    } else {
      el.classList.remove("has-img");
      el.style.backgroundImage = "";
      el.textContent = initial(name);
      el.style.background = avatarColor(name);
    }
  }

  function updateMeUI() {
    const n = S.me || "我";
    $("meNick").textContent = n;
    paintAvatar($("meDot"), n, S.avatar);
    $("accNickShow").textContent = n;
    $("accUser").textContent = "@" + (S.username || "");
    paintAvatar($("accDot"), n, S.avatar);
    $("accNick").value = n;
    paintAvatarPick();
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
      saveAuth({ token: d.token, username: d.username, nick: d.nick, avatar: d.avatar || "" });
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
    S.avatar = auth.avatar || "";
    // 注册/登录后默认进入原始仙界，不继承上次页面留在的私人频道
    S.room = ROOM;
    S.channel = null;
    S.channels = { joined: [], public: [] };
    S.channelManageId = "";
    S.switchPending = "";
    $("loginErr").textContent = "";
    $("login").hidden = true;
    $("login").style.display = "none";
    $("app").hidden = false;
    updateMeUI();
    updateChannelUI();
    connect();
    loadCatalog();
    setupViewport();
    refreshChannelData();
    checkAppUpdate();
  }

  /* ================= App 内更新 =================
   * 页面本身走远程 URL（capacitor.config.json 的 server.url），改本目录的
   * static 即刻生效，不存在「推送页面」这回事。这里只处理另一种情况：
   * 原生层（Java / 权限 / 清单）改了，必须装新 APK。
   *
   * 流程：查 /api/app/version → 站内下载 → 交系统安装器。
   * 浏览器里不触发（没有安装包可装），旧 APK 没有 AppUpdater 插件也会静默跳过。
   */
  const UPD_SNOOZE_KEY = "xiuxian_app_update_snooze_v1";
  const UPD_SNOOZE_MS = 12 * 3600 * 1000;   // 点「稍后」后 12 小时内不再打扰

  let updRel = null;    // 服务端下发的版本信息
  let updForce = false; // 强制更新：不给「稍后」
  let updBusy = false;  // 下载/安装中，防重复点

  function capPlugin(name) {
    const C = window.Capacitor;
    if (!C) return null;
    try {
      if (typeof C.registerPlugin === "function") return C.registerPlugin(name);
    } catch (_) {
      /* 老版本 Capacitor 走下面的兜底 */
    }
    return (C.Plugins && C.Plugins[name]) || null;
  }

  function isNativeApp() {
    const C = window.Capacitor;
    return !!(C && typeof C.isNativePlatform === "function" && C.isNativePlatform());
  }

  function fmtMB(n) {
    const v = Number(n) || 0;
    if (!v) return "";
    return v >= 1048576 ? (v / 1048576).toFixed(1) + " MB" : Math.round(v / 1024) + " KB";
  }

  async function checkAppUpdate() {
    if (!isNativeApp()) return;
    const app = capPlugin("App");
    const updater = capPlugin("AppUpdater");
    if (!app || !updater) return; // 旧包没带这个插件，什么都不做

    let info = null;
    try {
      info = await app.getInfo();
    } catch (_) {
      return;
    }
    const curCode = parseInt((info && info.build) || "", 10) || 0;
    if (!curCode) return;

    let rel = null;
    try {
      const r = await fetch("/api/app/version", { cache: "no-store" });
      if (!r.ok) return; // 服务端没配清单 = 没发过版
      rel = await r.json();
    } catch (_) {
      return;
    }
    if (!rel || !rel.ok || !rel.versionCode) return;
    if (Number(rel.versionCode) <= curCode) return; // 已是最新

    const force = Number(rel.minVersionCode || 0) > curCode;
    if (!force) {
      const snoozedAt = Number(localStorage.getItem(UPD_SNOOZE_KEY) || 0);
      if (snoozedAt && Date.now() - snoozedAt < UPD_SNOOZE_MS) return;
    }
    showUpdateDialog(rel, curCode, force);
  }

  function showUpdateDialog(rel, curCode, force) {
    updRel = rel;
    updForce = force;
    updBusy = false;

    $("updTitle").textContent = force ? "需要更新后才能继续" : "发现新版本";
    $("updBadge").textContent = force ? "必需" : "NEW";
    $("updName").textContent = `肥鱼修仙 ${rel.versionName || ""}`.trim();

    const size = fmtMB(rel.size);
    $("updSub").textContent =
      `当前 v${curCode} → 最新 v${rel.versionCode}` + (size ? ` · ${size}` : "");

    const log = $("updLog");
    log.innerHTML = "";
    (rel.changelog || []).forEach((line) => {
      const li = document.createElement("li");
      li.textContent = String(line);
      log.appendChild(li);
    });

    $("updProg").hidden = true;
    $("updProgBar").style.width = "0%";
    $("updProgTip").textContent = "准备下载…";
    $("updLater").hidden = force;
    $("updLater").textContent = "稍后";
    $("updClose").hidden = force;
    $("updNow").disabled = false;
    $("updNow").textContent = "立即更新";
    $("updMask").hidden = false;
  }

  function closeUpdateDialog() {
    if (updBusy) return;
    $("updMask").hidden = true;
    if (!updForce) {
      // 只对非强制更新记「稍后」，避免强制更新被无限推迟
      try { localStorage.setItem(UPD_SNOOZE_KEY, String(Date.now())); } catch (_) { /* ignore */ }
    }
  }

  async function startUpdate() {
    if (updBusy || !updRel) return;
    const updater = capPlugin("AppUpdater");
    if (!updater) return;

    updBusy = true;
    const btn = $("updNow");
    btn.disabled = true;
    btn.textContent = "下载中…";
    $("updLater").hidden = true;
    $("updClose").hidden = true;
    $("updProg").hidden = false;
    $("updProgBar").style.width = "0%";
    $("updProgTip").textContent = "正在下载安装包…";

    let listener = null;
    try {
      listener = await updater.addListener("progress", (d) => {
        const pct = Math.max(0, Math.min(100, Number(d && d.percent) || 0));
        $("updProgBar").style.width = pct + "%";
        $("updProgTip").textContent =
          `已下载 ${pct}%` + (d && d.total ? `（${fmtMB(d.received)} / ${fmtMB(d.total)}）` : "");
      });
    } catch (_) {
      /* 拿不到进度事件也不影响主流程 */
    }

    try {
      const fileName = String(updRel.url || "").split("/").pop() || "update.apk";
      const res = await updater.download({
        url: location.origin + updRel.url,
        fileName,
        sha256: updRel.sha256 || "",
      });
      $("updProgBar").style.width = "100%";
      $("updProgTip").textContent = "下载完成，正在调起安装…";
      await updater.install({ path: res.path });

      $("updProgTip").textContent = "已交给系统安装，按提示点「安装」即可";
      btn.textContent = "安装";
      btn.disabled = false;
      $("updLater").hidden = false;
      $("updLater").textContent = "关闭";
      updBusy = false;
    } catch (err) {
      const code = (err && (err.code || err.message)) || "";
      $("updProgTip").textContent =
        code === "NEED_PERMISSION"
          ? "请允许本应用「安装未知应用」，然后回来再点一次「立即更新」"
          : "更新失败：" + (err && (err.message || err.code) || "未知错误");
      btn.textContent = "重试";
      btn.disabled = false;
      $("updLater").hidden = false;
      $("updLater").textContent = "关闭";
      updBusy = false;
    } finally {
      if (listener && typeof listener.remove === "function") {
        try { await listener.remove(); } catch (_) { /* ignore */ }
      }
    }
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
    S.avatar = "";
    S.online = [];
    S.token = "";
    S.connected = false;
    S.channel = null;
    S.channels = { joined: [], public: [] };
    S.channelManageId = "";
    S.switchPending = "";
    $("accMask").hidden = true;
    $("inboxMask").hidden = true;
    $("privacyMask").hidden = true;
    $("channelMask").hidden = true;
    $("fbMask").hidden = true;
    S.feedbackScope = "mine";
    S.feedbackAdmin = false;
    S.noticesOpen = false;
    S.notices = [];
    S.unreadNotices = 0;
    setUnreadBadge(0);
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
    $("avatarTip").textContent = "点选内置头像，或上传一张自己的（自动裁成方形）";
    loadBindStatus();
    $("accMask").hidden = false;
  }

  /* ---------- 头像 ---------- */
  function paintAvatarPick() {
    const box = $("avatarPick");
    if (!box) return;
    const cur = S.avatar || "";
    let html = `<button class="av-opt${cur ? "" : " active"}" data-val="" type="button"
      title="默认（昵称首字）" style="background:${avatarColor(S.me)}">${esc(initial(S.me))}</button>`;
    PRESETS.forEach((name) => {
      const url = `/static/img/avatars/${name}.svg`;
      html += `<button class="av-opt${cur === url ? " active" : ""}" data-val="${esc(url)}"
        type="button" style="background-image:url('${esc(url)}')" title="内置头像"></button>`;
    });
    html += `<button class="av-opt av-upload" data-upload="1" type="button" title="上传图片">＋</button>`;
    box.innerHTML = html;
  }

  async function saveAvatar(payload) {
    const tip = (t) => ($("avatarTip").textContent = t);
    try {
      const r = await fetch("/api/avatar", {
        method: "POST",
        headers: { Authorization: "Bearer " + S.token, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) return tip(d.error || "头像保存失败");
      S.avatar = d.avatar || "";
      updateMeUI();
      tip(S.avatar ? "头像已更换" : "已恢复默认头像");
      toast("头像已更新");
    } catch {
      tip("网络异常，稍后再试");
    }
  }

  async function onAvatarFile(ev) {
    const file = ev.target.files && ev.target.files[0];
    ev.target.value = ""; // 允许重复选同一张
    if (!file) return;
    const tip = (t) => ($("avatarTip").textContent = t);
    if (!/^image\//.test(file.type)) return tip("请选一张图片");
    if (file.size > 2 * 1024 * 1024) return tip("图片太大了，选 2MB 以内的");
    tip("正在处理图片…");
    const dataUrl = await new Promise((resolve) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result || ""));
      fr.onerror = () => resolve("");
      fr.readAsDataURL(file);
    });
    if (!dataUrl) return tip("这张图读不出来，换一张");
    await saveAvatar({ image: dataUrl });
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

  /* ---------- QQ 存档绑定 ---------- */
  async function apiBind(path, body) {
    try {
      const r = await fetch(path, {
        method: "POST",
        headers: { Authorization: "Bearer " + S.token, "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      return await r.json().catch(() => ({}));
    } catch {
      return { ok: false, error: "网络异常，稍后再试" };
    }
  }

  async function loadBindStatus() {
    const state = $("bindState");
    const tip = $("bindTip");
    const input = $("bindQq");
    const bindBtn = $("btnBind");
    const unbindBtn = $("btnUnbind");
    try {
      const r = await fetch("/api/bind/status", {
        headers: { Authorization: "Bearer " + S.token },
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) {
        state.textContent = "绑定状态读取失败";
        return;
      }
      if (d.qq) {
        state.textContent = "已绑定 QQ " + d.qq;
        state.classList.add("on");
        input.value = d.qq;
        input.disabled = true;
        bindBtn.hidden = true;
        unbindBtn.hidden = false;
        const base = d.save
          ? "已找到旧存档：境界 " + d.save.level_index + " · " +
            (d.save.cultivation_type || "修士") + "。现在发消息即使用这份存档。"
          : "该 QQ 号暂无存档数据。";
        tip.textContent = base + (d.web_save ? " 网页原存档已保留，解绑即可回到它。" : "");
      } else {
        state.textContent = "未绑定 · 使用网页独立存档";
        state.classList.remove("on");
        input.value = "";
        input.disabled = false;
        bindBtn.hidden = false;
        unbindBtn.hidden = true;
        const mine = d.web_save
          ? "当前网页存档：境界 " + d.web_save.level_index + " · " +
            (d.web_save.cultivation_type || "修士") + "。"
          : "";
        tip.textContent = mine +
          "填旧机器人的 QQ 号即可接着玩；网页当前存档会保留，解绑可回到它。";
      }
    } catch {
      state.textContent = "网络异常，稍后再试";
    }
  }

  async function bindQq() {
    const qq = $("bindQq").value.trim();
    if (!/^[1-9]\d{4,11}$/.test(qq)) {
      $("bindTip").textContent = "请填 5-12 位数字 QQ 号";
      return;
    }
    $("btnBind").disabled = true;
    $("bindTip").textContent = "正在查找旧存档…";
    const d = await apiBind("/api/bind", { qq });
    $("btnBind").disabled = false;
    if (!d.ok) {
      $("bindTip").textContent = d.error || "绑定失败";
      return;
    }
    await loadBindStatus();
    toast(d.web_save ? "已切到 QQ 存档，网页原存档已保留" : "已绑定旧存档");
  }

  /* ================= 消息隐私与个人消息 ================= */
  function applySettings(settings) {
    if (!settings || typeof settings !== "object") return;
    S.settings = { ...S.settings, ...settings };
    applyViewFilter();
    paintPrivacy();
  }

  /** 「只看自己」时隐藏其他人的聊天与回复；系统提示/世界播报始终可见 */
  function applyViewFilter() {
    const box = $("msgs");
    if (!box) return;
    const onlySelf = S.settings.view_filter === "self";
    box.querySelectorAll(".msg-row[data-owner], .card-msg[data-owner]").forEach((el) => {
      const owner = el.dataset.owner || "";
      const hide = onlySelf && !!owner && owner !== S.username;
      el.style.display = hide ? "none" : "";
    });
  }

  function paintPrivacy() {
    document.querySelectorAll("#privacyMask .seg").forEach((seg) => {
      const key = seg.dataset.key;
      seg.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("on", (S.settings[key] || "") === b.dataset.val);
      });
    });
  }

  function openPrivacy() {
    $("accMask").hidden = true;
    $("privacyMask").hidden = false;
    paintPrivacy();
  }

  async function savePrivacy() {
    const patch = {};
    document.querySelectorAll("#privacyMask .seg").forEach((seg) => {
      const on = seg.querySelector("button.on");
      if (on) patch[seg.dataset.key] = on.dataset.val;
    });
    const err = $("privacyErr");
    err.textContent = "";
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { Authorization: "Bearer " + S.token, "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) return (err.textContent = d.error || "保存失败");
      applySettings(d.settings || patch);
      $("privacyMask").hidden = true;
      toast("消息隐私配置已保存");
    } catch {
      err.textContent = "网络异常，稍后再试";
    }
  }

  /* ================= 意见反馈 ================= */
  const FB_KIND_LABEL = {
    bug: "BUG 反馈",
    suggestion: "玩法建议",
    balance: "数值平衡",
    other: "其他",
  };
  const FB_STATUS_LABEL = { 0: "待处理", 1: "已处理", 2: "已忽略" };

  const fbDay = (ts) => {
    const d = ts ? new Date(ts * 1000) : new Date();
    return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(
      d.getMinutes(),
    ).padStart(2, "0")}`;
  };

  function fbKind() {
    const on = document.querySelector("#fbKind button.on");
    return on ? on.dataset.val : "bug";
  }

  function openFeedback() {
    $("accMask").hidden = true;
    $("fbMask").hidden = false;
    $("fbErr").textContent = "";
    $("fbCount").textContent = String($("fbContent").value.length);
    loadFeedback();
  }

  function feedbackSnapshotLine(item) {
    const s = item.snapshot || {};
    const bits = [];
    if (s.realm) bits.push(s.realm);
    if (s.nick) bits.push(s.nick);
    if (s.channel) bits.push(s.channel);
    return bits.join(" · ");
  }

  function renderFeedback(items) {
    const box = $("fbList");
    if (!box) return;
    if (!items.length) {
      box.innerHTML = '<div class="fb-empty">还没有反馈记录。</div>';
      return;
    }
    const admin = S.feedbackAdmin && S.feedbackScope === "all";
    box.innerHTML = items
      .map((it) => {
        const st = Number(it.status) || 0;
        const snap = feedbackSnapshotLine(it);
        const acts = admin
          ? '<div class="fb-acts">' +
            (st === 0
              ? `<button class="btn-ghost small" data-fb-done="${it.id}" type="button">标记已处理</button>` +
                `<button class="btn-ghost small" data-fb-ignore="${it.id}" type="button">忽略</button>`
              : "") +
            "</div>"
          : "";
        return (
          '<div class="fb-item">' +
          '<div class="fb-top">' +
          `<span class="fb-kind">${esc(it.kind_label || FB_KIND_LABEL[it.kind] || "其他")}</span>` +
          `<span class="fb-status st-${st}">${esc(it.status_label || FB_STATUS_LABEL[st] || "待处理")}</span>` +
          `<span class="fb-time">${esc(fbDay(it.created))}</span>` +
          "</div>" +
          `<div class="fb-text">${esc(it.content || "")}</div>` +
          (snap ? `<div class="fb-snap">${esc(snap)}</div>` : "") +
          (it.contact ? `<div class="fb-snap">联系方式：${esc(it.contact)}</div>` : "") +
          (it.admin_note ? `<div class="fb-note">处理备注：${esc(it.admin_note)}</div>` : "") +
          acts +
          "</div>"
        );
      })
      .join("");
  }

  async function loadFeedback() {
    const box = $("fbList");
    if (!box) return;
    box.innerHTML = '<div class="fb-empty">加载中…</div>';
    const scope = S.feedbackScope === "all" ? "all" : "mine";
    try {
      const r = await fetch(`/api/feedback?scope=${scope}&limit=30`, {
        headers: { Authorization: "Bearer " + S.token },
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) {
        box.innerHTML = `<div class="fb-empty">${esc(d.error || "读取失败")}</div>`;
        return;
      }
      S.feedbackAdmin = !!d.is_admin;
      $("fbAdminToggle").hidden = !d.is_admin;
      $("fbAdminToggle").textContent = scope === "all" ? "查看我的" : "查看全部";
      $("fbListTitle").textContent =
        scope === "all" ? `全部反馈${d.pending ? `（待处理 ${d.pending}）` : ""}` : "我的反馈";
      renderFeedback(Array.isArray(d.items) ? d.items : []);
    } catch {
      box.innerHTML = '<div class="fb-empty">网络异常，稍后再试</div>';
    }
  }

  async function submitFeedback() {
    const err = $("fbErr");
    err.textContent = "";
    const content = $("fbContent").value.trim();
    if (content.length < 5) {
      err.textContent = "反馈内容至少写 5 个字";
      return;
    }
    const btn = $("fbSubmit");
    btn.disabled = true;
    try {
      const r = await fetch("/api/feedback", {
        method: "POST",
        headers: { Authorization: "Bearer " + S.token, "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: fbKind(),
          content,
          contact: $("fbContact").value.trim(),
          channel: S.channel && S.channel.name ? S.channel.name : "",
          client_version: "web",
          page: `${location.pathname}${location.hash || ""}`,
          ua: navigator.userAgent || "",
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) return (err.textContent = d.error || "提交失败");
      $("fbContent").value = "";
      $("fbContact").value = "";
      $("fbCount").textContent = "0";
      S.feedbackScope = "mine";
      toast(d.message || "反馈已提交，感谢你的建议");
      loadFeedback();
    } catch {
      err.textContent = "网络异常，稍后再试";
    } finally {
      btn.disabled = false;
    }
  }

  async function setFeedbackStatus(id, status) {
    try {
      const r = await fetch("/api/feedback/status", {
        method: "POST",
        headers: { Authorization: "Bearer " + S.token, "Content-Type": "application/json" },
        body: JSON.stringify({ id, status }),
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) return toast(d.error || "操作失败");
      toast(d.message || "已更新");
      loadFeedback();
    } catch {
      toast("网络异常，稍后再试");
    }
  }

  /* ---------- 顶栏个人消息 ---------- */
  function setUnreadBadge(n) {
    S.unreadNotices = Math.max(0, Number(n) || 0);
    const b = $("msgBadge");
    if (b) {
      b.hidden = S.unreadNotices <= 0;
      b.textContent = S.unreadNotices > 99 ? "99+" : String(S.unreadNotices);
    }
    const u = $("inboxUnread");
    if (u) u.textContent = S.unreadNotices > 0 ? `${S.unreadNotices} 条未读` : "";
  }

  function sendNotices(action = "notices", extra = {}) {
    if (!S.ws || S.ws.readyState !== 1) return;
    S.ws.send(JSON.stringify({ action, ...extra }));
  }

  function openInbox() {
    $("inboxMask").hidden = false;
    S.noticesOpen = true;
    renderNotices();
    sendNotices("notices", { limit: 60 });
  }

  function closeInbox() {
    $("inboxMask").hidden = true;
    S.noticesOpen = false;
  }

  function applyNotices(items, unread) {
    S.notices = Array.isArray(items) ? items : [];
    setUnreadBadge(unread || 0);
    renderNotices();
    // 打开收件箱即视为已读；服务端返回更新后的列表
    if (S.noticesOpen && S.unreadNotices > 0) sendNotices("notices_read", { all: true });
  }

  function renderNotices() {
    const box = $("inboxList");
    if (!box) return;
    if (!S.notices.length) {
      box.innerHTML =
        '<div class="inbox-empty">还没有个人消息。<br>自动代理、战报结算等通知会收进这里。</div>';
      return;
    }
    box.innerHTML = S.notices
      .map((n) => {
        const hasDetail = !!n.detail && n.detail !== n.summary;
        return (
          `<div class="inbox-item${n.is_read ? "" : " unread"}" data-id="${n.id}">` +
          `<div class="inbox-top">` +
          `<span class="inbox-title">${esc(n.title || "通知")}</span>` +
          `<span class="inbox-time">${fmtTime(n.created)}</span>` +
          `</div>` +
          `<div class="inbox-summary">${esc(n.summary || "")}</div>` +
          (hasDetail
            ? '<button class="inbox-toggle" type="button">展开详情</button>' +
              `<div class="inbox-detail">${esc(n.detail)}</div>`
            : "") +
          `</div>`
        );
      })
      .join("");
  }

  function onNotice(m) {
    const n = m.notice || {};
    S.notices.unshift(n);
    if (S.noticesOpen) {
      renderNotices();
      sendNotices("notices_read", { ids: [n.id] });
    } else {
      toast(`收到个人消息：${n.title || "通知"}`);
    }
    setUnreadBadge(m.unread ?? S.unreadNotices + 1);
  }

  async function unbindQq() {
    if (!confirm("解除后会回到网页自己的存档，QQ 存档仍会保留。确定吗？")) return;
    const d = await apiBind("/api/bind/unbind", {});
    if (!d.ok) {
      $("bindTip").textContent = d.error || "解绑失败";
      return;
    }
    await loadBindStatus();
    toast("已解除绑定");
  }

  /* ================= 连接 ================= */
  let failCount = 0;

  function connect() {
    S.manualClose = false;
    setConn("连接中…", false);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    let url = `${proto}://${location.host}/ws?token=${encodeURIComponent(S.token)}`;
    // 断线重连时尽量回到原频道；服务端会再次校验成员身份，不合法则落回原始仙界
    if (S.channel && S.channel.id) {
      url += `&channel=${encodeURIComponent(S.channel.id)}`;
    }
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

  /** 成员名单兼容两种格式：纯昵称数组（旧） / 带 online 标记的对象数组 */
  function normalizeOnline(list) {
    return (list || [])
      .map((p) =>
        typeof p === "string"
          ? { username: "", nick: p, avatar: "", online: true, seen: 0 }
          : {
              username: String(p.username || ""),
              nick: String(p.nick || p.username || ""),
              avatar: String(p.avatar || ""),
              // 老服务端没有 online 字段，一律当在线
              online: p.online !== false,
              seen: Number(p.seen || 0),
            }
      )
      .filter((p) => p.nick);
  }

  function setOnline(list) {
    S.online = normalizeOnline(list);
    const total = S.online.length;
    const on = S.online.filter((p) => p.online).length;
    // 有离线成员时显示「在线 3/12」，全员在线时保持原来的「在线 3」
    $("onlineTag").textContent =
      total > on ? `在线 ${on}/${total}` : `在线 ${on}`;
    if (!$("onlineMask").hidden) renderOnline();
  }

  function isMe(p) {
    return !!S.username && p.username === S.username;
  }

  function renderOnline() {
    const box = $("onlineList");
    const total = S.online.length;
    const online = S.online.filter((p) => p.online);
    const offline = S.online.filter((p) => !p.online);
    $("onlineCount").textContent = `${online.length}/${total}`;
    if (!total) {
      box.innerHTML = `<div class="online-empty">还没有人来过这间屋子</div>`;
      return;
    }
    // 自己排最前，其余维持服务端顺序（最近进过房间的在前）
    const sortMe = (list) =>
      [...list].sort((a, b) => (isMe(b) ? 1 : 0) - (isMe(a) ? 1 : 0));
    const rowHtml = (p) =>
      `<div class="online-row${p.online ? "" : " off"}">` +
      avatarHtml(p.nick, p.avatar, "online-avatar") +
      `<div class="online-info">` +
      `<div class="online-nick">${esc(p.nick)}${isMe(p) ? '<span class="online-me">我</span>' : ""}</div>` +
      `<div class="online-user">@${esc(p.username || p.nick)}</div>` +
      `</div>` +
      `<span class="online-dot${p.online ? "" : " off"}"></span>` +
      `</div>`;
    const groupHtml = (label, list) =>
      list.length
        ? `<div class="online-group">${label} ${list.length}</div>` +
          sortMe(list).map(rowHtml).join("")
        : "";
    box.innerHTML = groupHtml("在线", online) + groupHtml("离线", offline);
  }

  function openOnline() {
    $("onlineMask").hidden = false;
    renderOnline();
    refreshOnline();
  }

  /* ================= 仙界频道 ================= */
  let channelPwdCtx = null;

  function currentChannel() {
    return S.channel || {
      id: ROOM,
      name: "原始仙界",
      public: true,
      owner: "",
      is_main: true,
      is_mine: false,
      joined: true,
      has_password: false,
      member_count: 0,
    };
  }

  function channelMeta(ch) {
    if (!ch) return "";
    if (ch.is_main) return "所有道友的公共仙界 · 世界播报落点";
    const parts = [];
    parts.push(ch.is_mine ? "我创建的" : (ch.owner ? `创建者 @${ch.owner}` : "私人仙界"));
    parts.push(`${Number(ch.member_count || 0)} 人`);
    parts.push(ch.public ? "大厅公开" : "私密 UUID");
    parts.push(ch.has_password ? "有密码" : "无密码");
    return parts.join(" · ");
  }

  function updateChannelUI() {
    const ch = currentChannel();
    const name = ch.name || "原始仙界";
    if ($("channelBtnName")) $("channelBtnName").textContent = name;
    if ($("channelCurrentName")) $("channelCurrentName").textContent = name;
    if ($("channelCurrentBadge")) {
      $("channelCurrentBadge").textContent = ch.is_main
        ? "主频道"
        : (ch.is_mine ? "我创建的" : "私人仙界");
    }
    if ($("channelCurrentMeta")) $("channelCurrentMeta").textContent = channelMeta(ch);

    const uuidBox = $("channelCurrentUuid");
    if (!uuidBox) return;
    if (ch.is_mine && !ch.is_main && (ch.uuid || ch.id)) {
      const uuid = ch.uuid || ch.id;
      uuidBox.hidden = false;
      uuidBox.innerHTML =
        `<span>UUID</span><code title="${esc(uuid)}">${esc(uuid)}</code>` +
        `<button class="channel-uuid-copy" type="button" data-copy="${esc(uuid)}">复制</button>`;
    } else {
      uuidBox.hidden = true;
      uuidBox.innerHTML = "";
    }
  }

  function resetChatView() {
    const box = $("msgs");
    if (!box) return;
    if (contentRO) {
      Array.from(box.children).forEach((el) => contentRO.unobserve(el));
    }
    box.innerHTML = "";
    // 切房间/重连会丢掉等待态；同时唤醒批量队列，避免空等超时
    for (const id of Array.from(S.pending.keys())) settlePending(id, false);
    const quick = $("quickOptions");
    if (quick) quick.hidden = true;
    pinnedBottom = true;
    refreshScrollBottomBtn();
  }

  function applyChannelHistory(items) {
    if (!Array.isArray(items) || !items.length) return;
    items.forEach((m) => {
      if (!m || !m.type) return;
      if (m.type === "chat" && (m.text || m.sticker)) {
        addChat(
          m.from || "",
          m.text || "",
          m.ts,
          m.username ? m.username === S.username : m.from === S.me,
          m.avatar || "",
          {
            id: m.msg_id || "",
            owner: m.username || "",
            cmd: !!m.is_cmd,
            audience: m.audience || "public",
            persist: false,
            sticker: m.sticker || null,
          }
        );
      } else if (m.type === "bot") {
        addBotMessage(m, { persist: false });
      } else if (m.type === "broadcast" && m.text) {
        addSys("📢 " + String(m.text).replace(/\n/g, " "));
      }
    });
    scrollBottom();
  }

  function applyChannelState(ch) {
    if (!ch || !ch.id) return;
    S.channel = ch;
    S.room = ch.id;
    updateChannelUI();
  }

  function channelById(id) {
    return (S.channels.joined || []).find((c) => c && c.id === id) || null;
  }

  async function refreshChannelData() {
    if (!S.token) return;
    try {
      const r = await fetch("/api/channels", {
        headers: { Authorization: "Bearer " + S.token },
      });
      const d = await r.json().catch(() => null);
      if (!d || !d.ok) return;
      S.channels = {
        joined: Array.isArray(d.joined) ? d.joined : [],
        public: Array.isArray(d.public) ? d.public : [],
      };
      const cur = S.channels.joined.find((c) => c && c.id === S.room);
      if (cur) applyChannelState(cur);
      else updateChannelUI();
      if (!$("channelMask").hidden) renderChannelMenu();
    } catch {
      /* 列表拉取失败不影响聊天 */
    }
  }

  function openChannelMenu() {
    S.channelManageId = "";
    channelPwdCtx = null;
    $("channelPwdForm").hidden = true;
    $("channelCreateForm").hidden = true;
    $("channelUuidForm").hidden = true;
    $("channelCreateErr").textContent = "";
    $("channelUuidErr").textContent = "";
    $("channelMask").hidden = false;
    renderChannelMenu();
    refreshChannelData();
  }

  function closeChannelMenu() {
    $("channelMask").hidden = true;
    channelPwdCtx = null;
    $("channelPwdForm").hidden = true;
  }

  function renderChannelMenu() {
    const ch = currentChannel();
    updateChannelUI();

    const joined = S.channels.joined.length ? S.channels.joined : [ch];
    $("channelJoinedList").innerHTML = joined.map((item) => {
      const cur = item.id === S.room;
      const manageOpen = !!item.is_mine && S.channelManageId === item.id;
      return (
        `<div class="chan-item${cur ? " on" : ""}">` +
        `<button class="chan-main" type="button" data-chan-switch="${esc(item.id)}">` +
        `<span class="chan-name">${esc(item.name || "未命名仙界")}</span>` +
        `<span class="chan-meta">${esc(channelMeta(item))}</span>` +
        `</button>` +
        `<div class="chan-btns">` +
        (cur
          ? `<span class="chan-cur">当前</span>`
          : `<button class="chan-btn" type="button" data-chan-switch="${esc(item.id)}">进入</button>`) +
        (item.is_mine
          ? `<button class="chan-btn" type="button" data-chan-manage="${esc(item.id)}">${manageOpen ? "收起" : "管理"}</button>`
          : "") +
        `</div></div>` +
        (manageOpen ? channelManageHtml(item) : "")
      );
    }).join("") || `<div class="channel-empty">还没有加入任何仙界</div>`;

    const query = S.channelSearch.trim().toLowerCase();
    const hall = S.channels.public.filter(
      (item) => !query || String(item.name || "").toLowerCase().includes(query)
    );
    $("channelHallList").innerHTML = hall.length
      ? hall.map((item) => {
          const joinedHere = (S.channels.joined || []).some((c) => c && c.id === item.id);
          const cur = item.id === S.room;
          return (
            `<div class="chan-item${cur ? " on" : ""}">` +
            `<div class="chan-main">` +
            `<span class="chan-name">${esc(item.name || "未命名仙界")}</span>` +
            `<span class="chan-meta">${esc(channelMeta(item))}</span>` +
            `</div>` +
            `<div class="chan-btns">` +
            (cur
              ? `<span class="chan-cur">当前</span>`
              : joinedHere
                ? `<button class="chan-btn" type="button" data-chan-switch="${esc(item.id)}">进入</button>`
                : `<button class="chan-btn" type="button" data-chan-join="${esc(item.id)}" data-need-pwd="${item.has_password ? "1" : ""}">加入</button>`) +
            `</div></div>`
          );
        }).join("")
      : `<div class="channel-empty">${query ? "没有找到匹配的公开仙界" : "大厅里还没有公开仙界，去创建一个吧"}</div>`;

    $("channelTip").textContent =
      "公开仙界会出现在大厅，所有人可搜索加入；关闭公开后不再显示，需要把 UUID 和密码发给朋友才能进入。已加入的仙界不受影响，可随时切换。";
  }

  function channelManageHtml(ch) {
    const uuid = ch.uuid || ch.id;
    return (
      `<div class="chan-manage" data-manage-box="${esc(ch.id)}">` +
      `<label>仙界名称</label>` +
      `<input type="text" data-mg-name maxlength="20" value="${esc(ch.name || "")}">` +
      `<label>进入密码（留空表示不修改）</label>` +
      `<input type="password" data-mg-pwd maxlength="64" autocomplete="new-password" placeholder="${ch.has_password ? "已有密码，填新密码可替换" : "当前未设密码"}">` +
      `<label class="channel-check"><input type="checkbox" data-mg-public ${ch.public ? "checked" : ""}> 在大厅公开可见可搜索</label>` +
      `<div class="channel-current-uuid"><span>UUID</span><code title="${esc(uuid)}">${esc(uuid)}</code>` +
      `<button class="channel-uuid-copy" type="button" data-copy="${esc(uuid)}">复制</button></div>` +
      `<div class="chan-manage-acts">` +
      `<button class="chan-btn" type="button" data-mg-save="${esc(ch.id)}">保存设置</button>` +
      `<button class="chan-btn danger" type="button" data-mg-clear="${esc(ch.id)}"${ch.has_password ? "" : " disabled"}>清除密码</button>` +
      `</div></div>`
    );
  }

  function requestSwitchChannel(id) {
    if (!id || id === S.room) {
      closeChannelMenu();
      return;
    }
    if (!S.ws || S.ws.readyState !== 1) {
      const ch = channelById(id);
      if (ch) applyChannelState(ch);
      closeChannelMenu();
      toast("已切换，重连后进入该仙界");
      return;
    }
    S.switchPending = id;
    S.ws.send(JSON.stringify({ action: "switch", channel_id: id }));
    toast("正在切换仙界…", 1200);
  }

  async function createChannel() {
    const name = $("channelName").value.trim();
    const password = $("channelPwd").value.trim();
    const isPublic = $("channelPublic").checked;
    const err = (msg) => ($("channelCreateErr").textContent = msg);
    err("");
    if (password && password.length < 4) return err("进入密码至少 4 位，或留空不设密码");

    const btn = $("channelCreateOk");
    btn.disabled = true;
    try {
      const r = await fetch("/api/channels/create", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + S.token,
        },
        body: JSON.stringify({ name, password, public: isPublic }),
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) return err(d.error || "创建失败，稍后再试");
      $("channelName").value = "";
      $("channelPwd").value = "";
      $("channelCreateForm").hidden = true;
      toast(d.message || "仙界创建成功");
      await refreshChannelData();
      requestSwitchChannel(d.channel.id);
    } catch {
      err("网络异常，稍后再试");
    } finally {
      btn.disabled = false;
    }
  }

  async function joinChannel(identifier, password = "") {
    const err = (msg) => {
      $("channelUuidErr").textContent = msg;
      $("channelPwdErr").textContent = msg;
    };
    err("");
    try {
      const r = await fetch("/api/channels/join", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + S.token,
        },
        body: JSON.stringify({ channel_id: identifier, password }),
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) return err(d.error || "加入失败");
      $("channelUuid").value = "";
      $("channelUuidPwd").value = "";
      $("channelUuidForm").hidden = true;
      $("channelPwdForm").hidden = true;
      channelPwdCtx = null;
      toast(d.message || "已加入仙界");
      await refreshChannelData();
      requestSwitchChannel(d.channel.id);
    } catch {
      err("网络异常，稍后再试");
    }
  }

  async function saveChannelManage(channelId, { clearPassword = false } = {}) {
    const box = document.querySelector(`.chan-manage[data-manage-box="${channelId}"]`);
    if (!box) return;
    const name = box.querySelector("[data-mg-name]")?.value.trim() || "";
    const pwdEl = box.querySelector("[data-mg-pwd]");
    const pwd = pwdEl ? pwdEl.value.trim() : "";
    const isPublic = !!box.querySelector("[data-mg-public]")?.checked;
    const body = { channel_id: channelId, name, public: isPublic };
    if (pwd) body.password = pwd;
    if (clearPassword) body.clear_password = true;

    try {
      const r = await fetch("/api/channels/update", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + S.token,
        },
        body: JSON.stringify(body),
      });
      const d = await r.json().catch(() => ({}));
      if (!d.ok) {
        toast(d.error || "保存失败");
        return;
      }
      toast(d.message || "仙界设置已更新");
      if (d.channel && d.channel.id === S.room) applyChannelState(d.channel);
      await refreshChannelData();
    } catch {
      toast("网络异常，保存失败");
    }
  }

  function askJoinPassword(identifier) {
    channelPwdCtx = { identifier };
    $("channelPwdTitle").textContent = "该仙界需要密码";
    $("channelPwdInput").value = "";
    $("channelPwdErr").textContent = "";
    $("channelPwdForm").hidden = false;
    $("channelPwdInput").focus();
  }

  async function copyText(text) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      toast("UUID 已复制");
    } catch {
      const el = document.createElement("textarea");
      el.value = text;
      el.style.position = "fixed";
      el.style.opacity = "0";
      document.body.appendChild(el);
      el.select();
      try {
        document.execCommand("copy");
        toast("UUID 已复制");
      } catch {
        toast("复制失败，请手动选择 UUID");
      }
      el.remove();
    }
  }

  /* ================= 发送 ================= */
  function sendChat(text) {
    if (!S.ws || S.ws.readyState !== 1) {
      toast("尚未连接");
      return false;
    }
    showQuickOptions([]);
    S.ws.send(JSON.stringify({ action: "msg", text }));
    return true;
  }

  /* ================= 内置表情包 ================= */
  async function loadStickers() {
    if (S.stickerLoaded) return;
    S.stickerLoaded = true;
    const grid = $("stickerGrid");
    if (grid) grid.innerHTML = `<div class="sticker-empty">表情加载中…</div>`;
    try {
      const r = await fetch("/api/stickers");
      const d = await r.json();
      const groups = Array.isArray(d?.groups) ? d.groups : [];
      S.stickers = groups.flatMap((g) =>
        (Array.isArray(g?.stickers) ? g.stickers : []).filter((s) => s && s.id && s.url)
      );
      renderStickerGrid();
    } catch {
      S.stickerLoaded = false;
      if (grid) grid.innerHTML = `<div class="sticker-empty">表情加载失败，点这里重试</div>`;
    }
  }

  function renderStickerGrid() {
    const grid = $("stickerGrid");
    if (!grid) return;
    if (!S.stickers.length) {
      grid.innerHTML = `<div class="sticker-empty">暂无内置表情</div>`;
      return;
    }
    grid.innerHTML = S.stickers
      .map(
        (s) =>
          `<button type="button" class="sticker-item" data-sticker="${esc(s.id)}" ` +
          `title="${esc(s.label || "")}" aria-label="${esc(s.label || "表情")}">` +
          `<img src="${esc(s.url)}" alt="${esc(s.label || "")}" loading="lazy" draggable="false"></button>`
      )
      .join("");
  }

  function openStickerPanel(show) {
    const panel = $("stickerPanel");
    if (!panel) return;
    const next = show === undefined ? panel.hidden : !!show;
    panel.hidden = !next;
    $("btnSticker").classList.toggle("on", next);
    if (next) {
      $("quickOptions").hidden = true;
      loadStickers();
      // 打开面板时让聊天区继续贴底，避免表情把消息顶出视野
      scrollBottom();
    }
  }

  function sendSticker(id) {
    if (!id) return;
    if (!S.ws || S.ws.readyState !== 1) {
      toast("尚未连接");
      return;
    }
    S.ws.send(JSON.stringify({ action: "msg", sticker: id }));
    openStickerPanel(false);
  }

  /** 输入 #(滑稽) 这类贴吧快捷写法时，直接按表情发送 */
  function stickerByShortcut(text) {
    const m = /^#\((.{1,12})\)$/.exec(String(text || "").trim());
    if (!m) return null;
    return S.stickers.find((s) => s.label === m[1]) || null;
  }

  /** 撤回消息：公屏消息交给服务端广播，本地指令回显直接移除 */
  function recallMessage(msg, edit) {
    if (!msg || !msg.id) return;
    const row = findMsgRow(msg.id);
    if (row) row.dataset.recalled = "1";
    if (edit) fillComposer(msg.text);
    if (msg.local) {
      removeChatMessage(msg.id);
      addSys("你撤回了一条消息");
      if (!edit) toast("已撤回");
      return;
    }
    if (!S.ws || S.ws.readyState !== 1) {
      if (row) delete row.dataset.recalled;
      toast("尚未连接，无法撤回");
      return;
    }
    S.ws.send(JSON.stringify({ action: "recall", msg_id: msg.id }));
    if (!edit) toast("正在撤回…");
  }

  /** 把待编辑内容放回输入框，光标停在末尾 */
  function fillComposer(text) {
    const input = $("inMsg");
    input.value = String(text ?? "");
    input.dispatchEvent(new Event("input"));
    input.focus();
    try {
      const end = input.value.length;
      input.setSelectionRange(end, end);
    } catch {
      /* 部分浏览器不支持，忽略 */
    }
  }

  /** 固定进看板的指令：/我的信息、/储物戒（与后端 visibility.PANEL_COMMANDS 对齐） */
  const PANEL_CMDS = { "/我的信息": "status", "/储物戒": "bag" };

  function panelOfCmd(text) {
    const name = String(text || "").trim().split(/\s+/)[0];
    return PANEL_CMDS[name] || "";
  }

  /** 结束一次请求等待：删除等待态并唤醒批量指令的串行队列 */
  function settlePending(id, ok) {
    const ctx = id ? S.pending.get(id) : null;
    if (!ctx) return null;
    S.pending.delete(id);
    if (typeof ctx.waiter === "function") ctx.waiter(!!ok);
    return ctx;
  }

  /**
   * 发指令并等待定向回复
   * @param {string} text 指令原文
   * @param {{panel?: 'status'|'bag', card?: boolean, silent?: boolean, awaitReply?: boolean}} opts
   *   awaitReply 为真时返回 Promise<boolean>，供批量指令逐条串行发送
   */
  function sendCmd(text, opts = {}) {
    let settle = null;
    const wait = opts.awaitReply
      ? new Promise((resolve) => {
          settle = resolve;
        })
      : null;
    if (!S.ws || S.ws.readyState !== 1) {
      toast("尚未连接，请稍候");
      if (settle) settle(false);
      return wait;
    }
    // 看板指令无论从哪里触发都按 panel 请求处理，避免被当成公屏指令广播
    const panel = opts.panel || panelOfCmd(text);
    if (!panel) showQuickOptions([]);
    const id = reqId();
    S.pending.set(id, {
      cmd: text,
      panel: panel || null,
      card: !!opts.card,
      waiter: settle,
    });
    S.ws.send(
      JSON.stringify({
        action: "cmd",
        text,
        req_id: id,
        card: !!opts.card || !!panel,
        panel: !!panel,
        // 兼容未升级的服务端：看板请求仍按私密处理
        private: !!panel,
      })
    );
    // 公屏指令的聊天回显由服务端广播；私密/看板请求不产生聊天流回显
    // 兜底：20 秒未收到回复则丢弃等待态
    setTimeout(() => {
      if (S.pending.has(id)) {
        settlePending(id, false);
        if (panel) renderPanelEmpty(panel, "响应超时，请重试");
      }
    }, 20000);
    return wait;
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
      case "hello": {
        const switchedUser = !!S.username && !!m.username && S.username !== m.username;
        if (m.nick) { S.me = m.nick; }
        if (m.username) S.username = m.username;
        if (typeof m.avatar === "string") S.avatar = m.avatar;
        // 重连/换号后看板必须重新取：防止展示上一个账号或过期的储物戒
        S.bagDirty = true;
        if (switchedUser) {
          S.panelCard = { status: null, bag: null };
          const statusView = $("viewStatus");
          const bagView = $("viewBag");
          if (statusView) statusView.innerHTML = "";
          if (bagView) bagView.innerHTML = "";
        }
        applySettings(m.settings);
        setUnreadBadge(m.unread || 0);
        updateMeUI();
        applyChannelState(m.channel);
        setOnline(m.online);
        resetChatView();
        if (Array.isArray(m.history) && m.history.length) {
          applyChannelHistory(m.history);
        } else {
          replayCache();
        }
        addSys(`欢迎 ${S.me} 来到「${currentChannel().name}」`);
        refreshOnline();
        refreshChannelData();
        break;
      }
      case "chat":
        // 用账号判断是不是自己，昵称可以随时改
        addChat(
          m.from,
          m.text,
          m.ts,
          m.username ? m.username === S.username : m.from === S.me,
          m.avatar || "",
          {
            id: m.msg_id || "",
            owner: m.username || "",
            cmd: !!m.is_cmd,
            audience: m.audience || "public",
            sticker: m.sticker || null,
          }
        );
        break;
      case "recall":
        applyRecall(m);
        break;
      case "recall_fail":
        addError(m.text || "撤回失败");
        break;
      case "channel_switched":
        S.switchPending = "";
        applyChannelState(m.channel);
        resetChatView();
        if (Array.isArray(m.history) && m.history.length) {
          applyChannelHistory(m.history);
        } else {
          replayCache();
        }
        refreshOnline();
        refreshChannelData();
        closeChannelMenu();
        toast(`已进入「${currentChannel().name}」`);
        break;
      case "channel_error":
        S.switchPending = "";
        addError(m.text || "切换仙界失败");
        toast(m.text || "切换仙界失败");
        break;
      case "bot":
        if (m.req_id && S.pending.has(m.req_id)) {
          settlePending(m.req_id, !/^\s*❌/.test(String(m.text || "")));
        }
        if (m.actor && m.actor === S.username) markBagDirty();
        addBotMessage(m);
        break;
      case "reply":
        handleReply(m);
        break;
      case "bag_changed":
        // 后端确认储物戒内容有变：界面正开着就立即刷新，否则等切回来再取
        markBagDirty();
        refreshBagSoon();
        break;
      case "notice":
        onNotice(m);
        break;
      case "notices":
        applyNotices(m.items, m.unread);
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
      case "presence":
      case "online":
        setOnline(m.players);
        // 别的设备改了昵称/头像时，自己也跟着刷
        {
          const mine = (m.players || []).find((p) => p && p.username && p.username === S.username);
          if (mine) {
            const changed =
              (typeof mine.avatar === "string" && mine.avatar !== S.avatar) ||
              (mine.nick && mine.nick !== S.me);
            if (changed) {
              if (typeof mine.avatar === "string") S.avatar = mine.avatar;
              if (mine.nick) S.me = mine.nick;
              updateMeUI();
            }
          }
        }
        break;
      case "pong":
        break;
      default:
        break;
    }
  }

  function handleReply(m) {
    // 业务失败通常返回普通文本（「❌ …」），批量操作按文本判断成功与否
    const ok = !m.error && !/^\s*❌/.test(String(m.text || ""));
    const ctx = settlePending(m.req_id, ok);

    if (m.error && !m.cards?.length) {
      if (!ctx?.panel) showQuickOptions([]);
      if (ctx?.panel) renderPanelEmpty(ctx.panel, m.text || "请求失败");
      else addError(m.text || "请求失败");
      return;
    }

    // 收集卡片
    const cards = (m.cards || []).filter(Boolean);
    cards.forEach((c) => {
      if (!c.owner) c.owner = S.username;
      S.cards.set(c.id, c);
    });

    // 迟到的看板回复（本地等待态已超时/丢失）不要落进公屏，按卡片类型归位
    if (!ctx && m.req_id && cards.length) {
      const inferred = { info: "status", bag: "bag" }[cards[0].kind] || "";
      if (inferred) {
        setPanelCard(inferred, cards[0]);
        return;
      }
    }

    // 面板请求：结果归位到看板，不进聊天流
    if (ctx?.panel) {
      if (cards.length) {
        setPanelCard(ctx.panel, cards[0]);
      } else if (m.text) {
        renderPanelText(ctx.panel, m.text);
        // 没有卡片、退化成文本时也算已同步，避免每次切页重复请求
        if (ctx.panel === "bag") {
          S.bagDirty = false;
          bagLoadedAt = Date.now();
        }
      } else {
        renderPanelEmpty(ctx.panel, "没有数据");
      }
      return;
    }

    showQuickOptions(m.options || []);

    // 自己的指令已经执行：储物戒可能被改动，先打标记；
    // 后端若确认有变会随后推 bag_changed，界面开着时再精确刷新一次。
    markBagDirty();

    // 其他回复：进聊天流（仅自己可见）
    if (m.text) {
      addBotText(m.text, m.ts, m.options, null, {
        forceFold: !!m.personal,
        owner: S.username,
      });
    }
    cards.forEach((c) => addCardMessage(c, S.username));
    (m.images || []).forEach((u) => u && addBotText("", m.ts, null, [u], { owner: S.username }));
  }

  /* ================= 渲染：消息 ================= */
  // 用户视角是否停留在最新消息处：只有贴底时才自动跟随新消息
  // 容差取小值：离开底部一点点（8px）就视为正在翻阅历史，立即停止跟随并露出箭头
  const FOLLOW_SLACK = 8;
  let pinnedBottom = true;
  let smoothScrollUntil = 0;
  let lastScrollTop = 0;
  let touchStartY = 0;
  let contentRO = null;

  function distanceToBottom(box = $("msgs")) {
    if (!box) return 0;
    return Math.max(0, box.scrollHeight - box.scrollTop - box.clientHeight);
  }

  /** 内容是否已经超出视口、具备滚动条件 */
  function canScroll(box = $("msgs")) {
    if (!box) return false;
    return box.scrollHeight - box.clientHeight > FOLLOW_SLACK;
  }

  /** 不在底部时显示小箭头；已贴底则隐藏 */
  function refreshScrollBottomBtn() {
    const btn = $("btnScrollBottom");
    if (!btn) return;
    btn.hidden = distanceToBottom() <= FOLLOW_SLACK;
  }

  /** 立即标记「用户已离开底部」，不等 scroll 事件（滚轮/触摸先到、消息后到也不抢视角） */
  function markAwayFromBottom() {
    const box = $("msgs");
    if (!box || !canScroll(box)) return;
    smoothScrollUntil = 0;
    pinnedBottom = false;
  }

  /**
   * 滚动到最新消息
   * @param {{smooth?: boolean}} opts smooth=true 供按钮点击使用，自动跟随时保持瞬时以免动画排队
   */
  function scrollBottom(opts = {}) {
    const box = $("msgs");
    if (!box) return;
    pinnedBottom = true;
    if (opts.smooth) {
      // 平滑滚动的时长与距离有关，不能用固定延时判断结束：
      // 逐帧检查是否真正到达底部；用户中途上滑时 markAwayFromBottom 会把标记清零并停止检查。
      smoothScrollUntil = Date.now() + 5000; // 安全上限，避免异常情况下无限轮询
      box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
      refreshScrollBottomBtn();
      const settle = () => {
        if (!smoothScrollUntil) return;
        const arrived = distanceToBottom(box) <= FOLLOW_SLACK;
        if (arrived || Date.now() > smoothScrollUntil) {
          smoothScrollUntil = 0;
          pinnedBottom = arrived;
          refreshScrollBottomBtn();
          return;
        }
        requestAnimationFrame(settle);
      };
      requestAnimationFrame(settle);
      return;
    }
    // 自动跟随要即时生效：临时关掉 CSS 平滑滚动，避免连续消息排队动画
    const prev = box.style.scrollBehavior;
    box.style.scrollBehavior = "auto";
    box.scrollTop = box.scrollHeight;
    box.style.scrollBehavior = prev;
    lastScrollTop = box.scrollTop;
    refreshScrollBottomBtn();
  }

  /** 监听单条消息的高度变化（图片加载、卡片展开等），贴底时继续跟随 */
  function watchContentSize(el) {
    if (!el || typeof ResizeObserver === "undefined") return;
    if (!contentRO) {
      contentRO = new ResizeObserver(() => {
        if (pinnedBottom) scrollBottom();
        else refreshScrollBottomBtn();
      });
    }
    contentRO.observe(el);
  }

  function appendNode(el, opts = {}) {
    const box = $("msgs");
    const follow = pinnedBottom || !!opts.force;
    box.appendChild(el);
    watchContentSize(el);
    el.querySelectorAll(".bubble.foldable").forEach(setFoldLabel); // 全折状态下新消息也显示「展开」
    while (box.children.length > 240) {
      const first = box.firstChild;
      if (contentRO) contentRO.unobserve(first);
      first.remove();
    }
    // 新内容追加前用户就在底部才跟随；否则保持阅读位置并露出回到底部的箭头
    if (follow) scrollBottom();
    else refreshScrollBottomBtn();
  }

  function nextLocalMsgId() {
    S.msgSeq += 1;
    return `m${Date.now().toString(36)}${S.msgSeq}`;
  }

  function findMsgRow(id) {
    if (!id) return null;
    return Array.from($("msgs").querySelectorAll(".msg-row[data-msg-id]")).find(
      (r) => r.dataset.msgId === id
    ) || null;
  }

  /** 撤回广播：所有在线端同步移除气泡，并留一条系统提示 */
  function applyRecall(m) {
    const id = m && m.msg_id;
    if (!id) return;
    removeChatMessage(id);
    const mine = !!m.username && m.username === S.username;
    addSys(mine ? "你撤回了一条消息" : `${m.nick || m.from || "对方"} 撤回了一条消息`);
  }

  function removeChatMessage(id) {
    const row = findMsgRow(id);
    if (row) {
      if (contentRO) contentRO.unobserve(row);
      row.remove();
    }
    cacheDropMsg(id);
  }

  /**
   * @param {object} meta 消息元信息：{ id, local, persist }
   *   id      公屏消息用服务端 msg_id，本地回显则自动生成
   *   local   仅本端可见（指令回显），撤回不需要通知服务端
   *   persist 是否写入本地缓存（回放历史时传 false，避免重复入栈）
   */
  function addChat(from, text, ts, isMe, avatar = "", meta = {}) {
    const id = meta.id || nextLocalMsgId();
    const owner = meta.owner !== undefined ? meta.owner : (isMe ? S.username : "");
    const sticker = meta.sticker || null;
    const row = document.createElement("div");
    row.className = "msg-row" + (isMe ? " right" : "") + (meta.cmd ? " cmd-row" : "");
    row.dataset.msgId = id;
    row.dataset.msgLocal = meta.local ? "1" : "";
    row.dataset.owner = owner || "";
    row.innerHTML =
      avatarHtml(from, avatar || (isMe ? S.avatar : "")) +
      `<div class="msg-col">` +
      `<div class="who">${esc(from)}<span class="time">${fmtTime(ts)}</span></div>` +
      bubbleHtml(text, null, sticker ? " sticker-bubble" : "", false, sticker) +
      `</div>`;

    if (isMe) {
      const col = row.querySelector(".msg-col");
      const more = document.createElement("button");
      more.type = "button";
      more.className = "msg-more";
      more.title = "消息操作";
      more.setAttribute("aria-label", "消息操作");
      more.textContent = "⋯";
      more.addEventListener("click", (e) => {
        e.stopPropagation();
        openMsgMenu(more, { id, text, local: !!meta.local, sticker }, e);
      });
      col.appendChild(more);
      row.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        openMsgMenu(row, { id, text, local: !!meta.local, sticker });
      });
    }

    appendNode(row);
    bindImages(row);
    applyViewFilter();
    if (meta.persist !== false) {
      cachePush({
        t: "chat", id, from, text, ts, me: isMe, avatar,
        owner, cmd: !!meta.cmd, local: !!meta.local, sticker,
      });
    }
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
    appendNode(div);
  }

  function addBotText(text, ts = null, options = null, images = null, opts = {}) {
    const row = document.createElement("div");
    row.className = "msg-row bot-row";
    row.dataset.owner = opts.owner || "";
    row.dataset.audience = opts.audience || "";
    const tone = classifyTone(text);
    if (tone) row.classList.add(tone);

    const col = document.createElement("div");
    col.className = "msg-col";
    let html = `<div class="who bot-who">${BOT_NAME}<span class="time">${fmtTime(ts)}</span></div>`;
    // 动作反馈/报错保持原样；单行短提示也不必折，避免把「未学会」藏起来
    const forceFold = Boolean(opts.forceFold) && !tone && (text.length >= 30 || text.includes("\n"));
    html += bubbleHtml(text, images, "", forceFold);
    col.innerHTML = html;

    row.innerHTML = `<div class="avatar bot-avatar"></div>`;
    row.appendChild(col);
    bindImages(row);
    appendNode(row);
    applyViewFilter();
    if (text && opts.persist !== false) {
      cachePush({ t: "bot", text, ts, owner: opts.owner || "" });
    }
  }

  function addCardMessage(card, owner = "") {
    const div = document.createElement("div");
    div.className = "msg card-msg";
    div.dataset.owner = card.owner || owner || "";
    div.appendChild(renderCard(card));
    appendNode(div);
    applyViewFilter();
  }

  function addBotMessage(m, opts = {}) {
    const cards = m.cards || [];
    const owner = m.actor || "";
    const audience = m.audience || "public";
    const persist = opts.persist !== false;
    // 历史回放、他人的指令和自动代理消息不能改变当前输入提示。
    if (persist && owner === S.username && m.channel !== "auto") {
      showQuickOptions(m.options || []);
    }

    // 自动代理的公开消息：摘要常驻，详情按配置折叠
    if (m.channel === "auto" && m.title) {
      if (m.detail) {
        addCardMessage(
          {
            id: `auto_${m.ts || Date.now()}`,
            kind: "notice",
            title: m.title,
            summary: m.summary || m.text || "",
            detail: m.detail,
            folded: m.detail_collapsed !== false,
            owner,
          },
          owner
        );
        return;
      }
      if (m.text) addBotText(m.text, m.ts, m.options, null, { owner, audience, persist });
      (m.images || []).forEach((u) => u && addBotText("", m.ts, null, [u], { owner, audience, persist }));
      return;
    }

    if (cards.length) {
      cards.forEach((c) => addCardMessage(c, owner));
      if (m.text) {
        addBotText(m.text, m.ts, null, null, {
          forceFold: !!m.personal,
          owner,
          audience,
          persist,
        });
      }
    } else if (m.text) {
      addBotText(m.text, m.ts, m.options, null, {
        forceFold: !!m.personal,
        owner,
        audience,
        persist,
      });
    }
    (m.images || []).forEach((u) => u && addBotText("", m.ts, null, [u], { owner, audience, persist }));
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

  /* ================= 消息折叠 ================= */
  /** 长文本才值得折：字数或行数任一超标 */
  function isFoldable(text) {
    return !!text && (text.length >= FOLD_CHARS || text.split("\n").length >= FOLD_LINES);
  }

  /** 统一构造气泡：长文本自动带 .b-body 与展开按钮；forceFold 用于个人信息消息 */
  function bubbleHtml(text, images, extraCls = "", forceFold = false, sticker = null) {
    const stickerHtml =
      sticker && sticker.url
        ? `<img class="sticker-img" src="${esc(sticker.url)}" ` +
          `data-full="${esc(sticker.url)}" alt="${esc(sticker.label || "表情")}" ` +
          `title="${esc(sticker.label || "表情")}" draggable="false">`
        : "";
    const body =
      (text ? escapeWithMarks(text) : "") +
      stickerHtml +
      (images || []).map((u) => `<img src="${esc(u)}" data-full="${esc(u)}" alt="">`).join("");
    if (!text && stickerHtml) {
      return `<div class="bubble sticker-bubble${extraCls}">${body}</div>`;
    }
    if (!isFoldable(text) && !(forceFold && text)) {
      return `<div class="bubble${extraCls}">${body}</div>`;
    }
    return (
      `<div class="bubble foldable folded${extraCls}">` +
      `<div class="b-body">${body}</div>` +
      `<button class="fold-btn" type="button">展开全文` +
      `<span class="n"> · ${text.length} 字</span></button>` +
      `</div>`
    );
  }

  /** 按钮文案随「单条是否展开」和「全局是否全折」变化 */
  function setFoldLabel(bubble) {
    const btn = bubble.querySelector(".fold-btn");
    if (!btn || !btn.firstChild) return;
    const open = !bubble.classList.contains("folded");
    const all = $("msgs").classList.contains("fold-all");
    btn.firstChild.textContent = open ? "收起" : all ? "展开" : "展开全文";
    const n = btn.querySelector(".n");
    if (n) n.style.display = open ? "none" : ""; // 展开后不必再报字数
    btn.title = open ? "收起这条" : "展开这条";
  }

  function toggleFold(bubble, open) {
    const box = $("msgs");
    const stick = pinnedBottom || distanceToBottom(box) <= FOLLOW_SLACK;
    bubble.classList.toggle("folded", !open);
    bubble.classList.toggle("open", open);
    setFoldLabel(bubble);
    if (stick) scrollBottom(); // 原本贴底就继续贴底
    else refreshScrollBottomBtn();
  }

  /** 全局折叠 / 展开；remember=false 用于启动时回放上次状态 */
  function applyFoldAll(on, remember = true) {
    const box = $("msgs");
    const stick = pinnedBottom || distanceToBottom(box) <= FOLLOW_SLACK;
    box.classList.toggle("fold-all", on);
    box.querySelectorAll(".bubble.foldable").forEach((b) => {
      b.classList.toggle("folded", on);
      b.classList.toggle("open", !on);
      setFoldLabel(b);
    });
    const btn = $("btnFold");
    btn.classList.toggle("on", on);
    btn.querySelector(".ft").textContent = on ? "展开" : "折叠";
    btn.title = on ? "展开全部消息" : "折叠全部消息";
    if (stick) scrollBottom();
    else refreshScrollBottomBtn();
    if (remember) {
      try {
        localStorage.setItem(FOLD_KEY, on ? "1" : "0");
      } catch {
        /* 隐私模式忽略 */
      }
    }
  }

  function bindImages(root) {
    root.querySelectorAll("img[data-full]").forEach((img) => {
      img.addEventListener("click", () => openViewer(img.dataset.full));
      // 图片撑开高度后，仍停在底部就继续跟随
      img.addEventListener("load", () => {
        if (pinnedBottom) scrollBottom();
      });
    });
  }

  /* ================= 卡片渲染 ================= */
  function renderCard(card, opts = {}) {
    const wrap = document.createElement("div");
    wrap.className = "card";
    wrap.dataset.cardId = card.id;
    // 个人信息卡片在聊天流默认折叠；看板渲染时传 expanded 保持展开
    const foldedByDefault = Boolean(card.folded) && !opts.expanded;

    const head = document.createElement("div");
    head.className = "card-head";
    head.innerHTML =
      `<div class="card-title">${esc(card.title || "信息")}</div>` +
      (card.subtitle ? `<div class="card-sub">${esc(card.subtitle)}</div>` : "");
    // 战报/自动代理摘要：折叠时也要能看到结果
    if (card.summary) {
      const sum = document.createElement("div");
      sum.className = "card-summary";
      sum.textContent = card.summary;
      head.appendChild(sum);
    }
    wrap.appendChild(head);

    const body = document.createElement("div");
    body.className = "card-body";

    // 0) /行情 单标的：顶部挂 K 线，下面的「近 8 盘」文本明细照旧
    if (card.quote && card.quote.code) {
      body.appendChild(renderQuoteChart(card.quote));
    }

    // 0.5) 战报 / 长详情：默认折叠，展开后看逐回合全文
    if (card.detail !== undefined && card.detail !== null) {
      if (card.detail_hidden) {
        const hint = document.createElement("div");
        hint.className = "detail-hidden";
        hint.textContent = "战报详情仅本人可查看";
        body.appendChild(hint);
      } else if (card.detail) {
        const pre = document.createElement("div");
        pre.className = "detail-block";
        pre.textContent = card.detail;
        body.appendChild(pre);
      }
    }

    // 1) 列表型（秘境/排行/Boss）
    if (Array.isArray(card.groups) && card.groups.length) {
      body.appendChild(renderDiscover(card));
    }

    if (Array.isArray(card.items) && card.items.length) {
      body.appendChild(renderItems(card.items, card));
    }

    // 1.5) 仙市行情：风声 + 行情表（不走通用分区渲染）
    if (card.kind === "market" && card.market) {
      body.appendChild(renderMarket(card.market));
    }

    // 2) 分区型（带条目提升的分区单独走列表渲染）
    const sections = (card.sections || []).filter(
      (s) =>
        (s.rows || []).length || (s.lines || []).length ||
        (s.items || []).length || (s.note || []).length
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
        // 每件都重复的说明已提到分区尾部，只写一次
        (sec.note || []).forEach((ln) => {
          const d = document.createElement("div");
          d.className = "card-note";
          d.textContent = ln;
          box.appendChild(d);
        });
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

    // 折叠开关：默认只露标题，展开后显示正文、卡脚与操作
    if (foldedByDefault && wrap.querySelector(".card-body, .card-foot")) {
      const isBattle = card.kind === "battle";
      const openText = isBattle ? "展开战报" : "展开详情";
      const closeText = isBattle ? "收起战报" : "收起详情";
      wrap.classList.add("card-foldable", "folded");
      const bar = document.createElement("div");
      bar.className = "card-fold-bar";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "card-fold-btn";
      btn.textContent = openText;
      btn.setAttribute("aria-expanded", "false");
      btn.addEventListener("click", () => {
        const open = wrap.classList.contains("folded");
        wrap.classList.toggle("folded", !open);
        btn.textContent = open ? closeText : openText;
        btn.setAttribute("aria-expanded", String(open));
      });
      bar.appendChild(btn);
      wrap.appendChild(bar);
    }
    return wrap;
  }

  /* ---- 仙市行情：风声 + 真表格 ----
   * 插件用空格拼出的伪表格一换行就错位，这里按行数据重新排版。 */
  function marketPctClass(txt) {
    if (/^▲/.test(txt)) return "up";
    if (/^▼/.test(txt)) return "down";
    return "flat";
  }

  function renderMarketTable(g, rows) {
    const box = document.createElement("div");
    box.className = "mk-block";
    const name = document.createElement("div");
    name.className = "mk-block-name";
    name.textContent = g.name;
    box.appendChild(name);

    const table = document.createElement("table");
    table.className = "mk-table";
    // 固定列宽：名称列吃剩余空间，其余按内容给固定宽度才不会挤成均分
    const widths = [44, 0, 64, ...g.cols.map(() => 58), ...(g.key === "fund" ? [36] : [])];
    const colgroup = document.createElement("colgroup");
    widths.forEach((w) => {
      const col = document.createElement("col");
      if (w) col.style.width = w + "px";
      colgroup.appendChild(col);
    });
    table.appendChild(colgroup);

    const thead = document.createElement("thead");
    const htr = document.createElement("tr");
    ["代码", "名称", g.price, ...g.cols, ...(g.key === "fund" ? ["风险"] : [])].forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      tr.title = `${r.code} ${r.name} · 点击看行情`;
      const code = document.createElement("td");
      code.className = "mk-code";
      code.textContent = r.code;
      tr.appendChild(code);
      const nm = document.createElement("td");
      nm.className = "mk-name";
      nm.textContent = r.name;
      tr.appendChild(nm);
      const price = document.createElement("td");
      price.className = "mk-num";
      price.textContent = r.price;
      tr.appendChild(price);
      g.cols.forEach((_, i) => {
        const raw = (r.pcts || [])[i] || "—";
        const td = document.createElement("td");
        td.className = `mk-pct ${marketPctClass(raw)}`;
        td.textContent = raw;
        tr.appendChild(td);
      });
      if (g.key === "fund") {
        const risk = document.createElement("td");
        risk.className = "mk-risk";
        risk.textContent = "★".repeat(r.risk || 0) || "—";
        tr.appendChild(risk);
      }
      tr.addEventListener("click", () => openEntitySheet(r.code));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);
    return box;
  }

  function renderMarket(m) {
    const box = document.createElement("div");
    box.className = "mk";

    if (!m.closed && typeof m.progress === "number") {
      const bar = document.createElement("div");
      bar.className = "mk-progress";
      const fill = document.createElement("i");
      const pct = Math.round(Math.min(1, Math.max(0, m.progress)) * 100);
      fill.style.width = `${pct}%`;
      bar.appendChild(fill);
      box.appendChild(bar);
    }

    (m.hints || []).forEach((h) => {
      const row = document.createElement("div");
      row.className = "mk-hint";
      const label = document.createElement("div");
      label.className = "mk-hint-label";
      label.textContent = h.hour ? `${h.label} · ${h.hour}` : h.label;
      row.appendChild(label);
      if (h.title) {
        const t = document.createElement("div");
        t.className = "mk-hint-title";
        t.innerHTML = valueHtml(h.title);
        row.appendChild(t);
      }
      if (h.detail) {
        const d = document.createElement("div");
        d.className = "mk-hint-detail";
        d.textContent = h.detail;
        row.appendChild(d);
      }
      box.appendChild(row);
    });

    const groups = [
      { key: "stock", name: "股票", price: "现价", cols: m.stock_cols || ["本盘", "今日", "近5盘"] },
      { key: "fund", name: "基金", price: "净值", cols: m.fund_cols || ["本盘", "今日"] },
    ];
    groups.forEach((g) => {
      const rows = (m.rows || []).filter((r) => r.kind === g.key);
      if (rows.length) box.appendChild(renderMarketTable(g, rows));
    });
    return box;
  }

  // ---- 单标的 K 线：日/周/月都由逐盘收盘价合成，基金画净值折线 ----
  const KL_PERIODS = [["day", "日K"], ["week", "周K"], ["month", "月K"]];
  const KL_NS = "http://www.w3.org/2000/svg";
  const KL_H = 168;
  // 同一标的的 K 线在 60 秒内复用：聊天记录回放时会反复渲染同一张卡片
  const KL_CACHE = new Map();
  const KL_TTL = 60 * 1000;

  function klSvg(data, width, onHover) {
    const bars = data.candles || [];
    const isFund = data.kind === "fund";
    const line = isFund || bars.length < 8;
    const fx = isFund ? 4 : 2;
    const PL = 6, PR = 44, PT = 10, PB = 16;
    const iw = Math.max(60, width - PL - PR);
    const ih = KL_H - PT - PB;
    const hi = Math.max(...bars.map((b) => b.h));
    const lo = Math.min(...bars.map((b) => b.l));
    const span = hi - lo || Math.max(Math.abs(hi) * 0.02, 0.0001);
    const Y = (v) => PT + ((hi - v) / span) * ih;
    const step = iw / Math.max(1, bars.length);
    const bw = Math.max(1.2, Math.min(8, step * 0.62));
    const ns = (tag, attrs) => {
      const el = document.createElementNS(KL_NS, tag);
      Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
      return el;
    };
    const svg = ns("svg", {
      viewBox: `0 0 ${width} ${KL_H}`,
      width: width,
      height: KL_H,
      class: "kl-svg",
    });

    // 网格与右侧价格轴（三档：高 / 中 / 低）
    [hi, (hi + lo) / 2, lo].forEach((v) => {
      const y = Y(v);
      svg.appendChild(ns("line", { x1: PL, y1: y, x2: PL + iw, y2: y, class: "kl-grid" }));
      const t = ns("text", { x: PL + iw + 5, y: y + 3.5, class: "kl-axis" });
      t.textContent = v.toFixed(fx);
      svg.appendChild(t);
    });

    // 底部时间轴：只标首尾两根，窄栏也不会挤成一团
    const firstBar = bars[0], lastBar = bars[bars.length - 1];
    if (firstBar && lastBar) {
      const t1 = ns("text", { x: PL, y: KL_H - 4, class: "kl-axis" });
      t1.textContent = firstBar.date;
      const t2 = ns("text", { x: PL + iw, y: KL_H - 4, class: "kl-axis end" });
      t2.textContent = lastBar.date;
      svg.append(t1, t2);
    }

    if (line) {
      // 基金净值 / 历史不足 8 根：折线，避免画成一片十字星
      const pts = bars.map((b, i) => [PL + step * i + step / 2, Y(b.c)]);
      svg.appendChild(
        ns("polyline", {
          points: pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" "),
          class: "kl-line",
        })
      );
      pts.forEach(([x, y], i) => {
        if (pts.length > 40 && i % 2) return;
        svg.appendChild(ns("circle", { cx: x, cy: y, r: 1.8, class: "kl-dot" }));
      });
    } else {
      bars.forEach((b, i) => {
        const x = PL + step * i + step / 2;
        const up = b.c >= b.o;
        const cls = up ? "kl-up" : "kl-down";
        const yO = Y(b.o), yC = Y(b.c);
        svg.appendChild(ns("line", { x1: x, y1: Y(b.h), x2: x, y2: Y(b.l), class: cls }));
        svg.appendChild(
          ns("rect", {
            x: (x - bw / 2).toFixed(2),
            y: Math.min(yO, yC).toFixed(2),
            width: bw.toFixed(2),
            height: Math.max(1, Math.abs(yC - yO)).toFixed(2),
            // 上涨空心、下跌实心：颜色之外再多一重编码，色弱也读得出
            class: up ? `${cls} hollow` : cls,
          })
        );
      });
    }

    // 悬停：十字线 + 顶部读数（不跟随鼠标，窄栏也不会被裁掉）
    const cross = ns("line", { x1: 0, y1: PT, x2: 0, y2: PT + ih, class: "kl-cross" });
    cross.style.opacity = 0;
    svg.appendChild(cross);
    const hit = ns("rect", { x: PL, y: PT, width: iw, height: ih, fill: "transparent" });
    svg.appendChild(hit);

    const fmt = (v) => Number(v).toFixed(fx);
    const move = (ev) => {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const x = ((ev.clientX - rect.left) / rect.width) * width;
      const idx = Math.max(0, Math.min(bars.length - 1, Math.floor((x - PL) / step)));
      const b = bars[idx];
      const cx = PL + step * idx + step / 2;
      cross.setAttribute("x1", cx);
      cross.setAttribute("x2", cx);
      cross.style.opacity = 0.9;
      const chg = `${b.chg >= 0 ? "+" : ""}${(b.chg * 100).toFixed(2)}%`;
      const tags = (b.tags || []).join(" · ");
      onHover(
        `${b.date}　开 ${fmt(b.o)}　高 ${fmt(b.h)}　低 ${fmt(b.l)}　收 ${fmt(b.c)}　${chg}` +
          (b.n < 16 && data.period === "day" ? "　（当盘未收）" : "") +
          (tags ? `　${tags}` : "")
      );
    };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerdown", move);
    svg.addEventListener("pointerleave", () => {
      cross.style.opacity = 0;
      onHover("");
    });
    return svg;
  }

  /** /行情 卡片顶部挂 K 线：先给骨架，数据异步补；接口不可用时静默留白，不影响下方文本明细。 */
  function renderQuoteChart(quote) {
    const box = document.createElement("div");
    box.className = "kl";

    const bar = document.createElement("div");
    bar.className = "kl-bar";
    const chips = document.createElement("div");
    chips.className = "kl-chips";
    const legend = document.createElement("div");
    legend.className = "kl-legend";
    bar.append(chips, legend);

    const area = document.createElement("div");
    area.className = "kl-area";
    const tip = document.createElement("div");
    tip.className = "kl-tip";
    box.append(bar, area, tip);

    let period = "day";
    let data = null;
    let drawnW = 0;
    const cache = new Map();

    const paintChips = () =>
      [...chips.children].forEach((c) => c.classList.toggle("on", c.dataset.p === period));

    KL_PERIODS.forEach(([p, label]) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "kl-chip";
      b.dataset.p = p;
      b.textContent = label;
      b.addEventListener("click", () => {
        if (period === p) return;
        period = p;
        paintChips();
        load();
      });
      chips.appendChild(b);
    });
    paintChips();

    const showTip = (text) => {
      tip.textContent = text || "";
    };

    function paint() {
      if (!data) return;
      const w = Math.floor(area.clientWidth || 0);
      if (w < 160) return;
      drawnW = w;
      area.replaceChildren(klSvg(data, w, showTip));
    }

    function render(payload) {
      data = payload;
      const bars = payload.candles || [];
      const isFund = payload.kind === "fund";
      legend.textContent = `近 ${bars.length} 根${isFund ? "净值" : ""}${
        payload.enough ? "" : "（历史太短，折线显示）"
      }`;
      if (!bars.length) {
        area.innerHTML = `<div class="kl-empty">还没有历史行情，攒几盘再看</div>`;
        return;
      }
      drawnW = 0;
      paint();
    }

    async function load() {
      const p = period;
      if (cache.has(p)) {
        render(cache.get(p));
        return;
      }
      const ck = `${quote.code}|${p}`;
      const hit = KL_CACHE.get(ck);
      if (hit && Date.now() - hit.ts < KL_TTL) {
        cache.set(p, hit.data);
        render(hit.data);
        return;
      }
      area.innerHTML = `<div class="kl-empty">K 线加载中…</div>`;
      try {
        const r = await fetch(
          `/api/market/candles?code=${encodeURIComponent(quote.code)}&period=${p}&limit=60`,
          { headers: { Authorization: "Bearer " + S.token } }
        );
        const payload = await r.json();
        if (!payload || !payload.ok) throw new Error((payload && payload.error) || "读取失败");
        KL_CACHE.set(ck, { ts: Date.now(), data: payload });
        cache.set(p, payload);
        if (p === period) render(payload);
      } catch (e) {
        area.innerHTML = `<div class="kl-empty">K 线暂不可用：${esc(
          (e && e.message) || "读取失败"
        )}</div>`;
      }
    }

    // 卡片刚插入时宽度还是 0，用 ResizeObserver 等布局稳定后再画（也管住窗口缩放）
    if (typeof ResizeObserver === "function") {
      const ro = new ResizeObserver(() => {
        const w = Math.floor(area.clientWidth || 0);
        if (w >= 160 && Math.abs(w - drawnW) > 8) paint();
      });
      ro.observe(area);
    }
    load();
    return box;
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
      // 结构化条目优先；老卡片没有 entries 时回退到从展示名解析
      const entries = Array.isArray(sec.entries) ? sec.entries.filter((it) => it && it.name) : [];
      box.appendChild(entries.length ? renderItemGrid(entries) : renderChips(rows));
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
    let bulletsRendered = false;
    if (bulletLines.length && bulletLines.length >= lines.length * 0.6) {
      bulletsRendered = true;
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
      // 只有真的渲染成条目列表时才跳过这些行，否则「· 风声标题」会被整行吞掉
      .filter((l) => !(bulletsRendered && bulletLines.includes(l)))
      .forEach((ln) => {
        // 「指令：/行情 <代码>　/买入股 <代码> <手>」这类模板行拆成可点按钮
        const tmpl = parseTmplLine(ln);
        if (tmpl) {
          box.appendChild(renderTmplLine(tmpl));
          return;
        }
        const d = document.createElement("div");
        const isBullet = /^[·•]\s*\S/.test(ln);
        // 行首全角缩进＝上一条的补充说明（插件里只有风声这类正文这么写）
        const isDetail = !isBullet && /^\u3000+\S/.test(ln);
        d.className = "card-line" + (isBullet ? " bullet" : "") + (isDetail ? " detail" : "");
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
      const total = m ? Number(m[1]) : 1;
      chip.innerHTML = `<span>${esc(name)}</span>` + (m ? `<span class="cnt">×${m[1]}</span>` : "");
      chip.addEventListener("click", (e) => openItemMenu(name, e.currentTarget, total, null, e));
      box.appendChild(chip);
    });
    return box;
  }

  /** 展示名「无极骨（材料·妖兽 | 玄品）」→ 指令参数「无极骨」 */
  function bareItemName(label) {
    const raw = String(label ?? "").trim();
    const stripped = raw
      .replace(
        /[（(](?:材料|丹药|丹方|符箓|装备|功法)(?:·[^（）()|]+)?(?:\s*\|[^（）()]+)?[）)](?:·[^（）()·]+)?\s*$/,
        ""
      )
      .trim();
    return stripped || raw;
  }

  /* ---- 物品网格：名称 / 品阶·品质 / 数量 三行 ---- */
  const KIND_COLORS = {
    材料: "#c08334", 丹药: "#4f9d6b", 丹方: "#8a6fd0",
    符箓: "#c9a227", 装备: "#3f7fc9", 功法: "#b5567a",
  };
  const KIND_ORDER = ["材料", "丹药", "丹方", "符箓", "装备", "功法"];
  const QUALITY_ORDER = ["一般", "良好", "极佳", "大师"];
  const ELEMENT_COLORS = {
    金: "#c8a24a", 木: "#5fa564", 水: "#4a86c8", 火: "#cf5f45", 土: "#a8825a",
  };

  let itemTipEl = null;
  let itemTipTimer = 0;

  function hideItemTip() {
    clearTimeout(itemTipTimer);
    if (itemTipEl) {
      itemTipEl.remove();
      itemTipEl = null;
    }
  }

  // 浮层用 fixed 定位，容器一滚就会脱位，捕获阶段收掉
  window.addEventListener("scroll", hideItemTip, true);

  /** 悬停浮层：只读结构化字段，不发请求 */
  function showItemTip(entry, anchor) {
    hideItemTip();
    const tip = document.createElement("div");
    tip.className = "item-tip";
    const rows = [
      ["种类", [entry.kind, entry.sub].filter(Boolean).join("·")],
      ["品阶", entry.rank],
      ["品质", entry.quality],
      ["五行", entry.element],
      ["数量", `×${entry.count}`],
      ["状态", entry.equipped],
    ].filter(([, v]) => v);
    if (entry.affix_count) rows.push(["词条", `${entry.affix_count} 条`]);
    tip.innerHTML =
      `<div class="tip-name">${esc(entry.name)}</div>` +
      rows
        .map(([k, v]) => `<div class="tip-row"><span>${k}</span><b>${esc(v)}</b></div>`)
        .join("");
    document.body.appendChild(tip);
    const r = anchor.getBoundingClientRect();
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, window.innerWidth - w - 8));
    let top = r.top - h - 8;
    if (top < 8) top = r.bottom + 8;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
    itemTipEl = tip;
  }

  function bindItemTip(el, entry) {
    el.addEventListener("mouseenter", () => {
      clearTimeout(itemTipTimer);
      itemTipTimer = setTimeout(() => showItemTip(entry, el), 140);
    });
    el.addEventListener("mouseleave", hideItemTip);
    el.addEventListener("click", hideItemTip);
  }

  /** 储物戒多选状态：跨卡片刷新保留，条目消失时自动剔除 */
  const bagSelection = { on: false, keys: new Set(), busy: false };

  /** 多选与指令共用的条目键：同名装备按品质区分 */
  function bagEntryKey(it) {
    if (!it) return "";
    const name = String(it.name || "");
    const quality = String(it.quality || "");
    return quality ? `${name}|${quality}` : name;
  }

  function bagRecycleCommand(it) {
    const count = Math.max(1, Number(it.count) || 1);
    return `/回收 ${bagEntryKey(it)} ${count}`;
  }

  /** 批量赠予拆成单条指令；装备实例一次只能赠 1 件 */
  function bagGiftCommands(it, target) {
    const key = bagEntryKey(it);
    const count = Math.max(1, Number(it.count) || 1);
    if (it.kind === "装备") {
      return Array.from({ length: count }, () => `/赠予 ${target} ${key} 1`);
    }
    return [`/赠予 ${target} ${key} ${count}`];
  }

  /** 储物戒网格：结构化条目 + 种类/品质筛选 + 多选批量回收/赠予 */
  function renderItemGrid(items) {
    // 只保留当前仍存在的勾选，避免把刚回收完的物品继续算进批量操作
    const validKeys = new Set(items.map(bagEntryKey));
    for (const key of Array.from(bagSelection.keys)) {
      if (!validKeys.has(key)) bagSelection.keys.delete(key);
    }

    const wrap = document.createElement("div");
    wrap.className = "item-grid-wrap";

    const bulk = document.createElement("div");
    bulk.className = "bag-bulk";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "bulk-toggle";
    toggle.title = "开启后点击物品即可勾选，支持批量回收/赠予";
    const tools = document.createElement("div");
    tools.className = "bulk-tools";
    const countEl = document.createElement("span");
    countEl.className = "bulk-count";
    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = "bulk-btn";
    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "bulk-btn";
    clearBtn.textContent = "清空";
    const recycleBtn = document.createElement("button");
    recycleBtn.type = "button";
    recycleBtn.className = "bulk-btn primary";
    recycleBtn.textContent = "批量回收";
    const giftBtn = document.createElement("button");
    giftBtn.type = "button";
    giftBtn.className = "bulk-btn primary";
    giftBtn.textContent = "批量赠予";
    tools.append(countEl, allBtn, clearBtn, recycleBtn, giftBtn);
    bulk.append(toggle, tools);
    wrap.appendChild(bulk);

    const filters = document.createElement("div");
    filters.className = "item-filters";
    const grid = document.createElement("div");
    grid.className = "chip-grid";
    const empty = document.createElement("div");
    empty.className = "item-empty";
    empty.textContent = "没有符合筛选的物品";
    empty.hidden = true;

    const state = { kind: "", quality: "" };
    let visible = items.slice();
    const kinds = KIND_ORDER.filter((k) => items.some((it) => it.kind === k));
    const qualitiesOf = (kind) =>
      QUALITY_ORDER.filter((q) => items.some((it) => it.quality === q && (!kind || it.kind === kind)));

    const chipRow = (label, options, key, onChange) => {
      const row = document.createElement("div");
      row.className = "filter-row";
      const tag = document.createElement("span");
      tag.className = "filter-tag";
      tag.textContent = label;
      row.appendChild(tag);
      ["", ...options].forEach((opt) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "fchip" + (state[key] === opt ? " on" : "");
        b.dataset.val = opt;
        b.textContent = opt || "全部";
        b.addEventListener("click", () => {
          if (state[key] === opt) return;
          state[key] = opt;
          row.querySelectorAll(".fchip").forEach((x) => x.classList.toggle("on", x.dataset.val === opt));
          onChange(opt);
        });
        row.appendChild(b);
      });
      return row;
    };

    /** 品质选项跟着种类走：材料/丹方没有品质，那一行就该消失 */
    const buildFilters = () => {
      filters.innerHTML = "";
      if (kinds.length > 1) {
        filters.appendChild(
          chipRow("种类", kinds, "kind", () => {
            if (state.quality && !qualitiesOf(state.kind).includes(state.quality)) state.quality = "";
            buildFilters();
            paint();
          })
        );
      }
      const qs = qualitiesOf(state.kind);
      if (qs.length > 1) filters.appendChild(chipRow("品质", qs, "quality", paint));
      filters.hidden = filters.childElementCount === 0;
    };
    buildFilters();
    wrap.appendChild(filters);

    const selectedItems = () => items.filter((it) => bagSelection.keys.has(bagEntryKey(it)));

    function updateBulk() {
      const picked = selectedItems();
      const visibleKeys = visible.map(bagEntryKey);
      const allOn = visibleKeys.length > 0 && visibleKeys.every((k) => bagSelection.keys.has(k));
      toggle.classList.toggle("on", bagSelection.on);
      toggle.textContent = bagSelection.on ? "✓ 多选" : "☑ 多选";
      toggle.setAttribute("aria-pressed", String(bagSelection.on));
      toggle.disabled = bagSelection.busy;
      tools.hidden = !bagSelection.on;
      countEl.textContent = `已选 ${picked.length}`;
      allBtn.textContent = allOn ? "取消全选" : "全选";
      allBtn.disabled = bagSelection.busy || !visibleKeys.length;
      clearBtn.disabled = bagSelection.busy || !picked.length;
      recycleBtn.disabled = bagSelection.busy || !picked.length;
      giftBtn.disabled = bagSelection.busy || !picked.length;
    }

    toggle.addEventListener("click", () => {
      if (bagSelection.busy) return;
      hideItemTip();
      bagSelection.on = !bagSelection.on;
      bagSelection.keys.clear();
      paint();
    });
    allBtn.addEventListener("click", () => {
      if (bagSelection.busy) return;
      const visibleKeys = visible.map(bagEntryKey);
      const allOn = visibleKeys.length > 0 && visibleKeys.every((k) => bagSelection.keys.has(k));
      visibleKeys.forEach((k) => (allOn ? bagSelection.keys.delete(k) : bagSelection.keys.add(k)));
      paint();
    });
    clearBtn.addEventListener("click", () => {
      if (bagSelection.busy) return;
      bagSelection.keys.clear();
      paint();
    });

    /** 批量指令逐条串行发送，避免并发写存档时互相覆盖数量/装备实例 */
    async function runBatch(commands, label) {
      if (bagSelection.busy || !commands.length) return;
      bagSelection.busy = true;
      updateBulk();
      let ok = 0;
      let fail = 0;
      try {
        for (const cmd of commands) {
          const passed = await sendCmd(cmd, { card: true, awaitReply: true });
          if (passed) ok += 1;
          else fail += 1;
          updateBulk();
        }
      } finally {
        bagSelection.busy = false;
        // 批量完成后退出勾选模式，避免下一次刷新残留已处理的选择
        bagSelection.on = false;
        bagSelection.keys.clear();
        updateBulk();
      }
      toast(`${label}：成功 ${ok} 项${fail ? `，失败 ${fail} 项` : ""}`);
      S.bagDirty = true;
      refreshBagSoon(80);
    }

    recycleBtn.addEventListener("click", () => {
      const picked = selectedItems();
      if (!picked.length) {
        toast("请先勾选要回收的物品");
        return;
      }
      const commands = picked.map((it) => bagRecycleCommand(it));
      void runBatch(commands, "批量回收");
    });

    giftBtn.addEventListener("click", async () => {
      const picked = selectedItems();
      if (!picked.length) {
        toast("请先勾选要赠予的物品");
        return;
      }
      if (bagSelection.busy) return;
      let data = null;
      try {
        const qs = new URLSearchParams({ cmd: "/赠予", values: "[]" });
        const r = await fetch(`/api/arg_options?${qs.toString()}`, {
          headers: { Authorization: "Bearer " + S.token },
        });
        data = await r.json().catch(() => null);
      } catch {
        data = null;
      }
      if (!data || data.ok === false) {
        toast((data && data.error) || "读取赠予对象失败");
        return;
      }
      const options = Array.isArray(data.options) ? data.options : [];
      if (!options.length) {
        toast(data.message || "暂无可赠予的对象");
        return;
      }
      openSheet("批量赠予", "选择赠予对象", "/赠予", "", {
        choices: options,
        onPick: (value) => {
          const commands = picked.flatMap((it) => bagGiftCommands(it, value));
          void runBatch(commands, "批量赠予");
        },
      });
    });

    const cell = (it) => {
      const b = document.createElement("button");
      b.type = "button";
      const key = bagEntryKey(it);
      const checked = bagSelection.keys.has(key);
      b.className =
        "icell" +
        (bagSelection.on ? " is-select" : "") +
        (bagSelection.on && checked ? " is-checked" : "");
      b.style.setProperty("--kd", KIND_COLORS[it.kind] || "transparent");
      const meta = [it.rank, it.quality, it.equipped].filter(Boolean).join("·") || it.kind || "—";
      b.innerHTML =
        `<span class="ic-name">${esc(it.name)}</span>` +
        (it.element
          ? `<span class="ic-el" style="background:${ELEMENT_COLORS[it.element] || "#8f8f8f"}"></span>`
          : "") +
        `<span class="ic-meta">${esc(meta)}</span>` +
        `<span class="ic-cnt">×${it.count}</span>` +
        (bagSelection.on ? `<span class="ic-check">${checked ? "✓" : ""}</span>` : "");
      b.classList.toggle("is-equipped", !!it.equipped);
      b.title = bagSelection.on
        ? `${checked ? "取消勾选" : "勾选"} ${it.name}`
        : [it.name, it.rank, it.quality, it.element, it.equipped].filter(Boolean).join(" · ");
      if (bagSelection.on) {
        b.addEventListener("click", () => {
          if (bagSelection.keys.has(key)) bagSelection.keys.delete(key);
          else bagSelection.keys.add(key);
          paint();
        });
      } else {
        b.addEventListener("click", (e) => openItemMenu(it.name, e.currentTarget, it.count, it, e));
      }
      bindItemTip(b, it);
      return b;
    };

    function paint() {
      hideItemTip();
      grid.innerHTML = "";
      visible = items.filter(
        (it) => (!state.kind || it.kind === state.kind) && (!state.quality || it.quality === state.quality)
      );
      visible.forEach((it) => grid.appendChild(cell(it)));
      empty.hidden = visible.length > 0;
      updateBulk();
    }

    paint();
    wrap.appendChild(grid);
    wrap.appendChild(empty);
    return wrap;
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

  /** 发现型列表：秘境 / 历练 —— 筛选标签 + 分组折叠 + 每行直接进入 */
  function renderDiscover(card) {
    const box = document.createElement("div");
    box.className = "discover";
    const facets = (card.facets || []).filter((f) => f && f.key);
    const groups = (card.groups || []).filter((g) => g && (g.items || []).length);
    const goText = card.kind === "route_list" ? "出发" : "进入";
    let facet = (facets[0] && facets[0].key) || "all";
    const openState = {};
    groups.forEach((g) => {
      openState[g.key] = !g.collapsed;
    });

    const bar = document.createElement("div");
    bar.className = "discover-facets";
    const listBox = document.createElement("div");
    listBox.className = "discover-groups";

    // facet=all 是「推荐」，只留后端标出的推荐位；「全部」才是整段。
    // 但推荐位只从「可进/可走」段里挑，所以 soon/locked 段始终给全量：
    // 它们默认折叠，只显示一行汇总，留着玩家才知道「还差多少能解锁」。
    // 否则默认视角下这两段会整段消失，进度感就没了。
    const pick = (items, group) => {
      if (facet === "all") {
        return group === "open" ? items.filter((it) => it.recommend) : items;
      }
      if (facet === "全部") return items;
      return items.filter((it) => it.facet === facet);
    };

    const paint = () => {
      bar.innerHTML = "";
      facets.forEach((f) => {
        const b = document.createElement("button");
        b.className = "discover-chip" + (f.key === facet ? " on" : "");
        b.innerHTML = `${esc(f.label)}<span>${esc(String(f.count || 0))}</span>`;
        b.addEventListener("click", () => {
          if (facet === f.key) return;
          facet = f.key;
          // 切换筛选时把原本展开的分组保持展开，避免点完像没反应
          paint();
        });
        bar.appendChild(b);
      });

      listBox.innerHTML = "";
      let shown = 0;
      groups.forEach((g) => {
        const items = pick(g.items || [], g.key);
        if (!items.length) return;
        shown += items.length;
        const sec = document.createElement("div");
        sec.className = "discover-group" + (openState[g.key] ? " open" : "");

        const head = document.createElement("button");
        head.className = "discover-head";
        head.innerHTML =
          `<span>${esc(g.name || "")}</span>` +
          `<i class="discover-count">${items.length}</i>` +
          `<i class="discover-caret">›</i>`;
        head.addEventListener("click", () => {
          openState[g.key] = !openState[g.key];
          paint();
        });
        sec.appendChild(head);

        if (openState[g.key]) {
          const list = document.createElement("div");
          list.className = "discover-items";
          items.forEach((it) => list.appendChild(discoverRow(it, goText)));
          sec.appendChild(list);
        }
        listBox.appendChild(sec);
      });

      if (!shown) {
        const empty = document.createElement("div");
        empty.className = "discover-empty";
        empty.textContent = "这一分类下暂时没有可去的目标";
        listBox.appendChild(empty);
      }
    };

    paint();
    box.appendChild(bar);
    box.appendChild(listBox);
    return box;
  }

  /** 发现型列表的单个条目：点一下直接执行对应指令 */
  function discoverRow(it, goText) {
    const row = document.createElement("button");
    row.className =
      "discover-item" + (it.badge ? " best" : "") + (it.locked ? " locked" : "");
    let html = '<div class="di-main"><div class="di-title">' + esc(it.title || "");
    if (it.badge) html += `<i class="di-badge">${esc(it.badge)}</i>`;
    html += "</div>";
    const rows = (it.rows || []).filter((r) => r && r.length >= 2);
    if (rows.length) {
      html += '<div class="di-rows">';
      rows.forEach(([k, v]) => {
        html += `<span>${esc(k)}<b>${esc(v)}</b></span>`;
      });
      html += "</div>";
    }
    html += "</div>";
    if (it.locked) {
      // 未解锁：不给按钮，换成还需要什么，点了也不会白跑一趟
      html += `<span class="di-lock">${esc(it.lock_text || "未解锁")}</span>`;
    } else if (it.cmd) {
      html += `<span class="di-go">${esc(goText)}</span>`;
    }
    row.innerHTML = html;
    if (!it.locked && it.cmd) {
      row.addEventListener("click", () => dispatchCommand(it.cmd));
    } else {
      row.disabled = true;
    }
    return row;
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
        const buyName = it.name || it.title;
        if (buyable && buyName) {
          row.classList.add("buyable");
          row.addEventListener("click", () => openSheet(`购买 ${buyName}`, "数量（默认 1）", "/购买", buyName));
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

  /* ---- 浮层操作菜单 ---- */
  let ctxMenu = null;
  let ctxMenuDocHandler = null;

  function closeCtxMenu() {
    if (ctxMenuDocHandler) {
      document.removeEventListener("click", ctxMenuDocHandler);
      ctxMenuDocHandler = null;
    }
    if (ctxMenu) {
      ctxMenu.remove();
      ctxMenu = null;
    }
  }

  function mountCtxMenu(menu, anchor, sourceEvent = null) {
    // 旧菜单的 document 监听必须先注销，否则本次点击冒泡到 document 时
    // 会把刚打开的新菜单立刻关掉（表现为第二次点击同一物品没反应）
    closeCtxMenu();
    document.body.appendChild(menu);
    const r = anchor.getBoundingClientRect();
    const top = Math.min(r.bottom + 6, window.innerHeight - menu.offsetHeight - 12);
    const left = Math.min(r.left, window.innerWidth - menu.offsetWidth - 12);
    menu.style.top = `${Math.max(10, top)}px`;
    menu.style.left = `${Math.max(10, left)}px`;
    ctxMenu = menu;
    // 同步注册监听，但忽略触发打开菜单的那一次点击；换成 setTimeout 延后注册
    // 在部分移动端浏览器上可能早于同一次触摸产生的 click，仍会“打开即关闭”
    ctxMenuDocHandler = (e) => {
      if (e === sourceEvent) return;
      if (menu.contains(e.target)) return;
      closeCtxMenu();
    };
    document.addEventListener("click", ctxMenuDocHandler);
  }

  function ctxMenuButton(label, onClick) {
    const b = document.createElement("button");
    b.textContent = label;
    b.style.cssText =
      "display:block;width:100%;text-align:left;padding:8px 12px;border-radius:7px;color:#3a3128";
    b.addEventListener("mouseenter", () => (b.style.background = "#f5efe4"));
    b.addEventListener("mouseleave", () => (b.style.background = ""));
    b.addEventListener("click", () => {
      closeCtxMenu();
      onClick();
    });
    return b;
  }

  function newCtxMenu() {
    closeCtxMenu();
    const menu = document.createElement("div");
    menu.style.cssText =
      "position:fixed;z-index:70;background:#fff;border:1px solid #e5dbcb;border-radius:10px;" +
      "box-shadow:0 8px 24px rgba(60,45,25,.18);padding:5px;min-width:132px;font-size:13.5px";
    return menu;
  }

  /** 物品展示标签里的种类：装备 / 功法 / 其他 */
  function itemMenuTag(label) {
    const m = String(label || "").match(/[（(](装备|功法)(?:·([^（）()|]+))?/);
    if (!m) return { kind: "", sub: "" };
    return { kind: m[1], sub: String(m[2] || "").trim() };
  }

  function openItemMenu(name, anchor, total = 1, entry = null, sourceEvent = null) {
    const rawLabel = String(name || "").trim();
    name = bareItemName(rawLabel);
    // 结构化条目直接给种类；老路径从展示名括号里猜
    const tag = entry && entry.kind ? { kind: entry.kind, sub: entry.sub || "" } : itemMenuTag(rawLabel);
    const equippable = tag.kind === "装备" || tag.kind === "功法";
    const isGear = tag.kind === "装备";
    const count = Math.max(1, Number(total) || 1);
    // 同名物品可能有多个品质：回收/赠予要指名品质，装备要指名第几件
    const key = entry && entry.quality ? `${name}|${entry.quality}` : name;
    const menu = newCtxMenu();
    const ops = [];
    if (equippable) ops.push(["装备", () => quickEquip(name, entry)]);
    ops.push(["详情", () => dispatchCommand(`/物品信息 ${name}`)]);
    ops.push(["回收", () => recycleItem(key, count)]);
    ops.push(["赠予", () => giftItem(key, count, isGear)]);
    ops.forEach(([label, onClick]) => menu.appendChild(ctxMenuButton(label, onClick)));
    mountCtxMenu(menu, anchor, sourceEvent);
  }

  /** 回收：弹窗选择数量，默认当前全部数量 */
  function recycleItem(name, total) {
    const count = String(Math.max(1, Number(total) || 1));
    openSheet("回收物品", "确认要回收的数量", "/回收", name, {
      defaults: { count, 1: count },
    });
  }

  /** 赠予：先选对象 → 物品已带入 → 数量默认全部（装备实例默认 1） */
  function giftItem(name, total, isGear) {
    const count = String(isGear ? 1 : Math.max(1, Number(total) || 1));
    openSheet("赠予 物品", "选择赠予对象", "/赠予", "", {
      seed: { index: 1, value: name },
      defaults: { count, 2: count },
    });
  }

  /** 装备：先查槽位占用，必要时弹层选择替换 */
  async function quickEquip(name, entry = null) {
    let data = null;
    try {
      const r = await fetch(`/api/equip_context?item=${encodeURIComponent(name)}`, {
        headers: { Authorization: "Bearer " + S.token },
        // 槽位占用随穿戴变化，缓存住旧结果会让「替换弹窗」看起来失效
        cache: "no-store",
      });
      data = await r.json().catch(() => null);
    } catch {
      data = null;
    }
    if (!data || data.ok === false) {
      toast((data && data.error) || "装备信息读取失败");
      return;
    }
    if (!data.equippable) {
      toast(data.hint || "该物品不可装备");
      return;
    }
    if (data.already_equipped) {
      toast(data.hint || "该物品已装备");
      return;
    }
    const choices = Array.isArray(data.choices) ? data.choices : [];
    // 同名同品质可能有多件，替换时也要带上「第几件」，否则会装上另一件
    const pick = entry && entry.equip_index > 1 ? ` ${entry.equip_index}` : "";
    if (data.needs_choice && choices.length) {
      openChoiceSheet("装备替换", data.hint || "请选择要替换的栏位", choices, (value) => {
        dispatchCommand(`/装备 ${name}${pick} ${value}`.trim());
      });
      return;
    }
    if (data.hint) toast(data.hint);
    dispatchCommand(`/装备 ${name}${pick}`);
  }

  /** 复用参数弹层展示自定义候选项 */
  function openChoiceSheet(title, hint, choices, onPick) {
    openSheet(title, hint, "/装备", "", { choices, onPick });
  }

  /** 自己消息的操作菜单：撤回 / 撤回后带回输入框重新编辑 */
  function openMsgMenu(anchor, msg, sourceEvent = null) {
    const menu = newCtxMenu();
    menu.appendChild(ctxMenuButton("撤回", () => recallMessage(msg, false)));
    if (!msg.sticker) {
      menu.appendChild(ctxMenuButton("重新编辑", () => recallMessage(msg, true)));
    }
    mountCtxMenu(menu, anchor, sourceEvent);
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
    const need = rule.min ?? 1;
    if (have >= need) return null;
    return {
      name,
      hint: rule.example ? `${rule.hint}，例：${rule.example}` : rule.hint,
      preset: have ? parts.slice(1).join(" ") : "",
      example: rule.example || "",
    };
  }

  /* ================= 面板 ================= */
  /** 储物戒是否已过期：后台变更时只打标记，等玩家切回储物戒界面再刷新 */
  let bagRefreshTimer = 0;
  let bagLoadedAt = 0; // 最近一次成功渲染储物戒卡片的时间

  function markBagDirty() {
    S.bagDirty = true;
  }

  /** 当前看板是否停在储物戒页 */
  function bagTabActive() {
    const tab = document.querySelector(".panel-tab.active");
    return !!tab && tab.dataset.tab === "bag";
  }

  /** 合并短时间内的多次刷新请求，避免连发指令时重复渲染卡片 */
  function refreshBagSoon(delay = 220, retry = 0) {
    if (!S.connected) return;
    clearTimeout(bagRefreshTimer);
    bagRefreshTimer = setTimeout(() => {
      bagRefreshTimer = 0;
      if (!bagTabActive()) return;
      // 已有请求在路上：等它落地后再补一次，避免这张卡生成于变更之前
      if (panelPending("bag") && retry < 5) {
        refreshBagSoon(400, retry + 1);
        return;
      }
      loadPanel("bag");
    }, delay);
  }

  function loadPanel(which) {
    if (panelPending(which)) return;
    if (which === "status") sendCmd("/我的信息", { panel: "status", card: true, silent: true });
    if (which === "bag") sendCmd("/储物戒", { panel: "bag", card: true, silent: true });
  }

  /** 看板是否已有同类请求在路上，避免切页/推送触发重复渲染 */
  function panelPending(which) {
    for (const p of S.pending.values()) {
      if (p && p.panel === which) return true;
    }
    return false;
  }

  function panelView(which) {
    return which === "status" ? $("viewStatus") : $("viewBag");
  }

  function setPanelCard(which, card) {
    S.cards.set(card.id, card);
    S.panelCard[which] = card.id;
    if (which === "bag") {
      S.bagDirty = false;
      bagLoadedAt = Date.now();
    }
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
    view.appendChild(renderCard(card, { expanded: true }));
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
    if (tab === "proxy") loadProxyPanel();
    // 面板还没数据、或期间储物戒发生过变更时重新拉取：
    // 不在储物戒界面时不急着渲染卡片，回到这里再取最新状态。
    if (tab === "status" && !S.panelCard.status && S.connected) {
      loadPanel("status");
    }
    if (tab === "bag" && S.connected) {
      // 切回储物戒页一律取最新：变更标记、卡片过期或距上次渲染超过 3 秒都重拉，
      // 这样连 QQ 端改动的存档也会在切页时同步过来。
      if (S.bagDirty || !S.panelCard.bag || Date.now() - bagLoadedAt > 3000) {
        loadPanel("bag");
      }
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

    const searchWrap = document.createElement("div");
    searchWrap.className = "search-field cmd-search-field";

    const search = document.createElement("input");
    search.className = "cmd-search";
    search.placeholder = "搜索指令…（如 秘境 / 突破）";

    const searchClear = document.createElement("button");
    searchClear.type = "button";
    searchClear.className = "search-clear";
    searchClear.title = "清除";
    searchClear.setAttribute("aria-label", "清除搜索内容");
    searchClear.innerHTML =
      '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
      '<path d="M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6"></path></svg>';

    searchWrap.appendChild(search);
    searchWrap.appendChild(searchClear);
    view.appendChild(searchWrap);

    const list = document.createElement("div");
    view.appendChild(list);

    const readFold = () => {
      try {
        const saved = JSON.parse(sessionStorage.getItem(CMD_FOLD_KEY) || "{}");
        return saved && typeof saved === "object" ? saved : {};
      } catch {
        return {};
      }
    };
    const saveFold = (state) => {
      try {
        sessionStorage.setItem(CMD_FOLD_KEY, JSON.stringify(state));
      } catch {
        /* 隐私模式 / 存储不可用时忽略 */
      }
    };

    const draw = (kw = "") => {
      list.innerHTML = "";
      const q = kw.trim().toLowerCase();
      const folded = readFold();
      // 搜索时临时展开全部结果，清空搜索后恢复用户的折叠状态
      const searching = Boolean(q);
      S.cmdCatalog.categories.forEach((cat) => {
        const items = cat.items.filter(
          (it) => !q || it.label.toLowerCase().includes(q) || it.cmd.toLowerCase().includes(q)
        );
        if (!items.length) return;
        const isFolded = !searching && Boolean(folded[cat.id]);
        const group = document.createElement("div");
        group.className = "cmd-group" + (isFolded ? " folded" : "");
        group.dataset.cat = cat.id;

        const head = document.createElement("button");
        head.type = "button";
        head.className = "cmd-group-head";
        head.setAttribute("aria-expanded", String(!isFolded));
        head.title = isFolded ? "展开该分类" : "折叠该分类";
        head.innerHTML =
          `<span class="cmd-group-icon">${cat.icon || ""}</span>` +
          `<span class="cmd-group-name">${esc(cat.name)}</span>` +
          `<span class="cmd-group-count">${items.length}</span>` +
          `<span class="cmd-group-arrow" aria-hidden="true">▾</span>`;
        head.addEventListener("click", () => {
          const next = !group.classList.contains("folded");
          group.classList.toggle("folded", next);
          head.setAttribute("aria-expanded", String(!next));
          head.title = next ? "展开该分类" : "折叠该分类";
          const state = readFold();
          if (next) state[cat.id] = true;
          else delete state[cat.id];
          saveFold(state);
        });
        group.appendChild(head);

        const grid = document.createElement("div");
        grid.className = "cmd-grid";
        items.forEach((it) => {
          const b = document.createElement("button");
          const rule = (S.argSpec || {})[it.cmd.replace(/^\//, "")];
          // 按钮带 arg，或后端登记过参数规格，都应先开弹层：
          // 这样丹阁/符阁这类「必须选分区」的指令不会再把半截命令直接发出去。
          const needSheet = Boolean(it.arg || rule);
          b.className = "cmd-btn" + (it.tone === "gold" ? " gold" : "");
          b.innerHTML = `<span>${esc(it.label)}</span>` + (needSheet ? `<span class="arg-hint">需填参</span>` : "");
          b.addEventListener("click", () => {
            if (needSheet) {
              const hint = rule
                ? `${rule.hint}${rule.example ? `，例：${rule.example}` : ""}`
                : it.arg || "请选择参数";
              openSheet(it.label, hint, it.cmd);
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

  /* ================= 自动代理面板 =================
   * 顶部总开关一键开启/关闭全部；每项一个滑动开关，状态与 /代理状态 同源。
   * 开启仍走 AutoProxyManager 原有校验，不另写一套规则。
   */
  let proxyBusy = false;

  async function loadProxyPanel() {
    const view = $("viewProxy");
    if (!view) return;
    view.innerHTML = '<div class="panel-empty">自动代理加载中…</div>';
    try {
      const r = await fetch("/api/proxy", {
        headers: { Authorization: "Bearer " + S.token },
      });
      const data = await r.json().catch(() => null);
      if (!data) throw new Error("bad response");
      if (data.ok === false) {
        view.innerHTML = `<div class="panel-empty">${esc(data.error || "自动代理加载失败")}<br><button>重试</button></div>`;
        view.querySelector("button").addEventListener("click", loadProxyPanel);
        return;
      }
      renderProxyPanel(data);
    } catch {
      view.innerHTML = '<div class="panel-empty">网络异常，自动代理加载失败<br><button>重试</button></div>';
      view.querySelector("button").addEventListener("click", loadProxyPanel);
    }
  }

  function proxySwitch(on, task) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "switch" + (on ? " on" : "");
    b.setAttribute("role", "switch");
    b.setAttribute("aria-checked", on ? "true" : "false");
    b.title = on ? "点击关闭" : "点击开启";
    b.dataset.task = task;
    b.dataset.on = on ? "1" : "0";
    b.innerHTML = "<span></span>";
    b.addEventListener("click", () => {
      if (proxyBusy) return;
      toggleProxy(task, b.dataset.on !== "1");
    });
    return b;
  }

  function renderProxyPanel(data) {
    const view = $("viewProxy");
    view.innerHTML = "";

    const head = document.createElement("div");
    head.className = "panel-head";
    head.innerHTML = '<span class="ph-title">自动代理</span>';
    const refresh = document.createElement("button");
    refresh.className = "ph-refresh";
    refresh.textContent = "刷新";
    refresh.addEventListener("click", loadProxyPanel);
    head.appendChild(refresh);
    view.appendChild(head);

    if (!data.has_player) {
      const empty = document.createElement("div");
      empty.className = "panel-empty";
      empty.innerHTML = `<span class="big">🤖</span>${esc(data.message || "尚未创建角色")}<br><button>去创建角色</button>`;
      empty.querySelector("button").addEventListener("click", () => {
        closeDrawer();
        dispatchCommand("/我要修仙");
      });
      view.appendChild(empty);
      return;
    }

    const master = document.createElement("div");
    master.className = "proxy-master" + (data.enabled ? " on" : "");
    master.innerHTML =
      '<span class="proxy-master-icon">🤖</span>' +
      '<div class="proxy-master-text"><b>自动代理总开关</b><small>' +
      (data.enabled
        ? "代理运行中 · 一键关闭全部"
        : "一键开启灵田 / 施肥 / 秘境（无可用秘境时开启押镖）") +
      "</small></div>";
    master.appendChild(proxySwitch(data.enabled, "all"));
    view.appendChild(master);

    if (data.rifts && data.rifts.length) {
      const sub = document.createElement("div");
      sub.className = "proxy-sub";
      const label = document.createElement("label");
      label.textContent = "秘境目标";
      label.setAttribute("for", "proxyRiftSelect");
      const sel = document.createElement("select");
      sel.id = "proxyRiftSelect";
      sel.className = "proxy-select";
      data.rifts.forEach((rift) => {
        const opt = document.createElement("option");
        opt.value = rift.id;
        opt.textContent = `${rift.name}（ID:${rift.id}）`;
        if (Number(rift.id) === Number(data.rift_id)) opt.selected = true;
        sel.appendChild(opt);
      });
      const riftTask = (data.tasks || []).find((task) => task.key === "rift");
      if (riftTask && riftTask.enabled) {
        sel.disabled = true;
        sel.title = "秘境代理运行中，如需换本请先关闭再开启";
      }
      sub.appendChild(label);
      sub.appendChild(sel);
      view.appendChild(sub);
    }

    const list = document.createElement("div");
    list.className = "proxy-list";
    (data.tasks || []).forEach((task) => {
      const row = document.createElement("div");
      row.className = "proxy-row" + (task.enabled ? " on" : "");
      const main = document.createElement("div");
      main.className = "proxy-row-main";
      main.innerHTML =
        '<div class="proxy-row-title"><span class="proxy-row-name">' +
        esc(task.name) +
        "</span>" +
        (task.enabled ? '<span class="proxy-badge">运行中</span>' : "") +
        "</div>" +
        `<div class="proxy-row-desc">${esc(task.detail || task.desc || "")}</div>`;
      row.appendChild(main);
      const sw = proxySwitch(task.enabled, task.key);
      if (task.available === false) sw.disabled = true;
      row.appendChild(sw);
      list.appendChild(row);
    });
    view.appendChild(list);

    const tip = document.createElement("div");
    tip.className = "proxy-tip";
    tip.textContent =
      "灵田 / 施肥需要先开垦灵田；秘境只列出当前境界可进入的本；押镖与宗门任务互斥，开启其中一个会关闭另一个。";
    view.appendChild(tip);
  }

  async function toggleProxy(task, enabled) {
    if (proxyBusy) return;
    const view = $("viewProxy");
    proxyBusy = true;
    if (view) {
      view.classList.add("busy");
      view.querySelectorAll(".switch").forEach((s) => (s.disabled = true));
    }
    try {
      const body = { task, enabled };
      if (task === "all" || task === "rift") {
        const sel = $("proxyRiftSelect");
        if (sel) body.rift_id = Number(sel.value) || 0;
      }
      const r = await fetch("/api/proxy/toggle", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + S.token,
        },
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => null);
      if (!data) throw new Error("bad response");
      if (data.ok === false) {
        toast(data.error || "操作失败");
        await loadProxyPanel();
        return;
      }
      renderProxyPanel(data);
      if (data.message) toast(data.message);
    } catch {
      toast("网络异常，操作失败");
      await loadProxyPanel();
    } finally {
      proxyBusy = false;
      if (view) view.classList.remove("busy");
    }
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
    const { example = "", actions = [], seed = null, defaults = {}, choices = [], onPick = null } = opts || {};
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
      defaults,
      choices,
      onPick,
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
    if (choices.length) {
      sheetCtx.data = {
        index: 0,
        total: 1,
        field: { label: "选择" },
        options: choices,
        done: false,
        message: hint || "",
      };
      renderSheetField(sheetCtx, sheetCtx.data);
      $("sheetInput").style.display = "none";
      $("sheetOk").textContent = "取 消";
      return;
    }
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

  /** 取当前字段的预填默认值 */
  function sheetDefaultValue(ctx, data) {
    const defaults = ctx.defaults || {};
    const index = Number((data && data.index) || 0);
    const key = (data && data.field && data.field.key) || "";
    let value = defaults[key];
    if (value === undefined) value = defaults[index];
    if (value === undefined) value = defaults[String(index)];
    return value === undefined || value === null || String(value) === "" ? "" : String(value);
  }

  /** 给手动输入字段预填默认值（如回收/赠予的数量） */
  function applySheetDefault(ctx, data) {
    const value = sheetDefaultValue(ctx, data);
    if (value) $("sheetInput").value = value;
  }

  function renderSheetField(ctx, data) {
    renderChosen(ctx);
    const field = data.field || {};
    const options = data.options || [];
    // 自定义候选项（如装备替换）不提供手动输入，选择即执行
    if (ctx.onPick) {
      renderCustomChoices(ctx, data);
      return;
    }
    const defaultText = sheetDefaultValue(ctx, data);
    let label = field.label || data.hint || ctx.hint || "参数";
    if (defaultText && field.key === "count") label = `数量（默认 ${defaultText}，可修改）`;
    const stepText =
      field.optional && Number(data.total || 1) > 1
        ? `${label}（选完点「发送」）`
        : `第 ${data.index + 1} 步：${label}`;
    $("sheetHint").textContent = stepText + (data.message ? ` · ${data.message}` : "");
    $("sheetInput").placeholder = options.length ? `也可手动输入${label}` : label;
    $("sheetInput").style.display = "";
    applySheetDefault(ctx, data);
    $("sheetSkip").hidden = !field.optional;
    $("sheetSkip").textContent = field.skip_value ? "留空，下一步" : "留空发送";
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

  function renderCustomChoices(ctx, data) {
    const options = data.options || [];
    $("sheetHint").textContent = data.message || ctx.hint || "请选择";
    $("sheetPickLabel").textContent = (data.field && data.field.label) || "选择";
    $("sheetInput").style.display = "none";
    $("sheetSkip").hidden = true;
    $("sheetExample").hidden = true;
    $("sheetPicks").hidden = !options.length;
    $("sheetMore").hidden = true;
    $("sheetOk").textContent = "取 消";
    if (options.length) drawSheetOptions(ctx);
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
    applySheetDefault(ctx, data || {});
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
    if (ctx.onPick) {
      const onPick = ctx.onPick;
      const value = String(opt.value ?? "");
      closeSheet();
      onPick(value, opt);
      return;
    }
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
    if (ctx.onPick) {
      closeSheet();
      return;
    }
    const data = ctx.data || {};
    const field = data.field || null;

    if (!data.done) {
      let val = $("sheetInput").value.trim();
      // 数量显式加 x，避免跳过火候后把 1/2/3 当成旧式火候编号。
      if (ctx.name === "炼丹" && field?.key === "count" && /^\d+$/.test(val)) val = `x${val}`;
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

    const fallbackMin = (sheetApi()[ctx.name] || {}).min;
    const need = Number(data.min ?? fallbackMin ?? 1);
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
    box.hidden = !options.length;
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

  /** 撤回后同步清掉本地缓存，刷新页面不再“复活” */
  function cacheDropMsg(id) {
    if (!id) return;
    try {
      const key = cacheKey();
      const arr = JSON.parse(localStorage.getItem(key) || "[]");
      const next = arr.filter((e) => !(e && e.t === "chat" && e.id === id));
      if (next.length !== arr.length) localStorage.setItem(key, JSON.stringify(next));
    } catch {
      /* 忽略 */
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
        if (e.t === "chat") {
          addChat(e.from, e.text, Math.floor(e.at / 1000), e.me, e.avatar || "", {
            id: e.id || "",
            owner: e.owner,
            cmd: !!e.cmd,
            local: e.local !== undefined ? !!e.local : !e.id,
            persist: false,
            sticker: e.sticker || null,
          });
        } else if (e.t === "bot") addBotText(e.text, Math.floor(e.at / 1000), null, null, { owner: e.owner || "" });
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
   * fish 大肥鱼·浅蓝鱼缸（默认） / xian 宣纸玉简
   * 只切 data-theme，视觉全交给 CSS 变量；这里只管文案、图标和持久化。
   * 大肥鱼的 logo 不写在文案里，交给 CSS 背景图（brand/mark 留空即可）。
   */
  const THEME_KEY = "xiuxian_theme_v2";
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
  let theme = "fish";

  function applyTheme(name, persist) {
    theme = THEMES[name] ? name : "fish";
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
    let start = "fish";
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

    // App 更新
    $("updNow").addEventListener("click", startUpdate);
    $("updLater").addEventListener("click", closeUpdateDialog);
    $("updClose").addEventListener("click", closeUpdateDialog);
    $("accSaveNick").addEventListener("click", saveNick);
    $("accSavePwd").addEventListener("click", savePwd);
    $("btnBind").addEventListener("click", bindQq);
    $("btnUnbind").addEventListener("click", unbindQq);
    $("bindQq").addEventListener("keydown", (e) => {
      if (e.key === "Enter") bindQq();
    });
    $("accLogout").addEventListener("click", () => logout("已退出登录"));

    // 消息隐私配置（账号设置内跳转）
    $("openPrivacy").addEventListener("click", openPrivacy);
    $("privacyClose").addEventListener("click", () => ($("privacyMask").hidden = true));
    $("privacyMask").addEventListener("click", (e) => {
      if (e.target === $("privacyMask")) $("privacyMask").hidden = true;
    });
    $("privacySave").addEventListener("click", savePrivacy);
    $("privacyMask").addEventListener("click", (e) => {
      const btn = e.target.closest && e.target.closest(".seg button");
      if (!btn) return;
      btn.parentElement.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
    });

    // 意见反馈
    $("openFeedback").addEventListener("click", openFeedback);
    $("fbClose").addEventListener("click", () => ($("fbMask").hidden = true));
    $("fbMask").addEventListener("click", (e) => {
      if (e.target === $("fbMask")) $("fbMask").hidden = true;
    });
    $("fbKind").addEventListener("click", (e) => {
      const btn = e.target.closest && e.target.closest("button");
      if (!btn) return;
      $("fbKind").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
    });
    $("fbContent").addEventListener("input", () => {
      $("fbCount").textContent = String($("fbContent").value.length);
    });
    $("fbSubmit").addEventListener("click", submitFeedback);
    $("fbAdminToggle").addEventListener("click", () => {
      S.feedbackScope = S.feedbackScope === "all" ? "mine" : "all";
      loadFeedback();
    });
    $("fbList").addEventListener("click", (e) => {
      const done = e.target.closest && e.target.closest("[data-fb-done]");
      if (done) return setFeedbackStatus(Number(done.dataset.fbDone), 1);
      const ignore = e.target.closest && e.target.closest("[data-fb-ignore]");
      if (ignore) return setFeedbackStatus(Number(ignore.dataset.fbIgnore), 2);
    });

    // 顶栏个人消息
    $("btnInbox").addEventListener("click", openInbox);
    $("inboxClose").addEventListener("click", closeInbox);
    $("inboxMask").addEventListener("click", (e) => {
      if (e.target === $("inboxMask")) closeInbox();
    });
    $("inboxRead").addEventListener("click", () => sendNotices("notices_read", { all: true }));
    $("inboxClear").addEventListener("click", () => sendNotices("notices_clear"));
    $("inboxList").addEventListener("click", (e) => {
      const btn = e.target.closest && e.target.closest(".inbox-toggle");
      if (!btn) return;
      const detail = btn.nextElementSibling;
      if (!detail) return;
      const open = detail.classList.toggle("open");
      btn.textContent = open ? "收起详情" : "展开详情";
    });

    // 头像：内置可点选，也可上传
    $("avatarPick").addEventListener("click", (e) => {
      const btn = e.target.closest(".av-opt");
      if (!btn) return;
      if (btn.dataset.upload) {
        $("avatarFile").click();
        return;
      }
      const val = btn.dataset.val || "";
      if (val === (S.avatar || "")) return;
      saveAvatar(val ? { preset: val.split("/").pop().replace(/\.svg$/, "") } : {});
    });
    $("avatarFile").addEventListener("change", onAvatarFile);

    // 在线名单
    $("onlineTag").addEventListener("click", openOnline);
    $("onlineClose").addEventListener("click", () => ($("onlineMask").hidden = true));
    $("onlineMask").addEventListener("click", (e) => {
      if (e.target === $("onlineMask")) $("onlineMask").hidden = true;
    });

    // 仙界频道
    $("btnChannel").addEventListener("click", openChannelMenu);
    $("channelClose").addEventListener("click", closeChannelMenu);
    $("channelMask").addEventListener("click", (e) => {
      if (e.target === $("channelMask")) closeChannelMenu();
    });
    $("channelCreateToggle").addEventListener("click", () => {
      const form = $("channelCreateForm");
      form.hidden = !form.hidden;
      $("channelUuidForm").hidden = true;
      $("channelPwdForm").hidden = true;
      $("channelCreateErr").textContent = "";
      if (!form.hidden) $("channelName").focus();
    });
    $("channelUuidToggle").addEventListener("click", () => {
      const form = $("channelUuidForm");
      form.hidden = !form.hidden;
      $("channelCreateForm").hidden = true;
      $("channelPwdForm").hidden = true;
      $("channelUuidErr").textContent = "";
      if (!form.hidden) $("channelUuid").focus();
    });
    $("channelCreateOk").addEventListener("click", createChannel);
    $("channelUuidOk").addEventListener("click", () => {
      const uuid = $("channelUuid").value.trim();
      if (!uuid) {
        $("channelUuidErr").textContent = "请先填写频道 UUID";
        return;
      }
      joinChannel(uuid, $("channelUuidPwd").value.trim());
    });
    $("channelPwdCancel").addEventListener("click", () => {
      channelPwdCtx = null;
      $("channelPwdForm").hidden = true;
    });
    $("channelPwdOk").addEventListener("click", () => {
      if (!channelPwdCtx) return;
      const pwd = $("channelPwdInput").value.trim();
      if (!pwd) {
        $("channelPwdErr").textContent = "请输入进入密码";
        return;
      }
      joinChannel(channelPwdCtx.identifier, pwd);
    });
    ["channelName", "channelPwd", "channelUuidPwd", "channelPwdInput"].forEach((id) => {
      $(id).addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        if (id === "channelName" || id === "channelPwd") createChannel();
        else if (id === "channelUuidPwd") $("channelUuidOk").click();
        else $("channelPwdOk").click();
      });
    });
    $("channelSearch").addEventListener("input", (e) => {
      S.channelSearch = e.target.value || "";
      renderChannelMenu();
    });
    $("channelDrawer").addEventListener("click", (e) => {
      const copy = e.target.closest("[data-copy]");
      if (copy) {
        copyText(copy.dataset.copy || "");
        return;
      }
      const sw = e.target.closest("[data-chan-switch]");
      if (sw) {
        requestSwitchChannel(sw.dataset.chanSwitch || "");
        return;
      }
      const join = e.target.closest("[data-chan-join]");
      if (join) {
        const id = join.dataset.chanJoin || "";
        if (join.dataset.needPwd === "1") askJoinPassword(id);
        else joinChannel(id);
        return;
      }
      const manage = e.target.closest("[data-chan-manage]");
      if (manage) {
        const id = manage.dataset.chanManage || "";
        S.channelManageId = S.channelManageId === id ? "" : id;
        renderChannelMenu();
        return;
      }
      const save = e.target.closest("[data-mg-save]");
      if (save) {
        saveChannelManage(save.dataset.mgSave || "");
        return;
      }
      const clear = e.target.closest("[data-mg-clear]");
      if (clear) {
        saveChannelManage(clear.dataset.mgClear || "", { clearPassword: true });
      }
    });

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
      // 贴吧快捷写法 #(滑稽)：直接按表情发送，不进入指令/机器人流程
      const shortcut = stickerByShortcut(text);
      if (shortcut) {
        sendSticker(shortcut.id);
      } else if (text.startsWith("/")) dispatchCommand(text);
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

    // 内置表情包：点按钮展开面板，点表情直接发送
    $("btnSticker").addEventListener("click", () => {
      openStickerPanel($("stickerPanel").hidden);
    });
    $("stickerClose").addEventListener("click", () => openStickerPanel(false));
    $("stickerGrid").addEventListener("click", (e) => {
      const item = e.target.closest && e.target.closest("[data-sticker]");
      if (item) {
        sendSticker(item.dataset.sticker || "");
        return;
      }
      if (e.target.closest && e.target.closest(".sticker-empty") && !S.stickerLoaded) {
        loadStickers();
      }
    });

    $("btnTogglePanel").addEventListener("click", () => {
      document.querySelector(".main").classList.toggle("panel-hidden");
    });

    // 消息折叠：单条展开 / 收起（事件委托，新消息不必重复绑定）
    $("msgs").addEventListener("click", (e) => {
      const btn = e.target.closest && e.target.closest(".fold-btn");
      if (!btn) return;
      const bubble = btn.closest(".bubble");
      if (bubble) toggleFold(bubble, bubble.classList.contains("folded"));
    });

    // 聊天流滚动：贴底时后续新消息自动跟随；离开底部则显示「回到最新」箭头
    const msgs = $("msgs");
    msgs.addEventListener(
      "scroll",
      () => {
        const top = msgs.scrollTop;
        const goingUp = top < lastScrollTop - 1;
        lastScrollTop = top;
        // 平滑回底途中用户又往上滑：立即交还控制权
        if (goingUp) smoothScrollUntil = 0;
        if (Date.now() < smoothScrollUntil && !goingUp) {
          pinnedBottom = true; // 正在执行「回到最新」，期间视为贴底
          return;
        }
        pinnedBottom = distanceToBottom(msgs) <= FOLLOW_SLACK;
        refreshScrollBottomBtn();
      },
      { passive: true }
    );

    // 滚轮/触摸一开始往上，就立即解除贴底，避免紧随其后的新消息把视角抢回底部
    msgs.addEventListener(
      "wheel",
      (e) => {
        if (e.deltaY < 0) markAwayFromBottom();
      },
      { passive: true }
    );
    msgs.addEventListener(
      "touchstart",
      (e) => {
        const t = e.touches && e.touches[0];
        touchStartY = t ? t.clientY : 0;
        smoothScrollUntil = 0;
      },
      { passive: true }
    );
    msgs.addEventListener(
      "touchmove",
      (e) => {
        const t = e.touches && e.touches[0];
        // 手指向下拖 = 查看更早的记录
        if (t && t.clientY > touchStartY + 4) markAwayFromBottom();
      },
      { passive: true }
    );

    $("btnScrollBottom").addEventListener("click", () => scrollBottom({ smooth: true }));
    refreshScrollBottomBtn();

    // 顶栏一键折叠 / 展开，状态记在本地
    $("btnFold").addEventListener("click", () => {
      applyFoldAll(!$("msgs").classList.contains("fold-all"));
    });
    try {
      applyFoldAll(localStorage.getItem(FOLD_KEY) === "1", false);
    } catch {
      applyFoldAll(false, false);
    }

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
    $("sheetSkip").addEventListener("click", () => {
      const field = sheetCtx?.data?.field;
      if (field?.skip_value) {
        const skipLabel =
          field.key === "fire" ? "火候：默认"
            : field.key === "pill" ? "进入战中"
              : "数量：默认";
        pickSheetOption({ value: field.skip_value, label: skipLabel });
      } else {
        $("sheetInput").value = "";
        submitSheet();
      }
    });
    $("sheetFilter").addEventListener("input", (e) => {
      if (!sheetCtx) return;
      sheetCtx.filter = e.target.value;
      drawSheetOptions(sheetCtx);
    });

    // 搜索框一键清除：事件委托覆盖参数筛选与指令搜索（含动态生成的输入框）
    const clearSearch = (btn) => {
      const field = btn.closest(".search-field");
      const input = field && field.querySelector("input");
      if (!input) return;
      if (input.value) {
        input.value = "";
        // 手动派发 input，让筛选/搜索逻辑与用户手动清空保持一致
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      try {
        input.focus({ preventScroll: true });
      } catch {
        input.focus();
      }
    };
    document.addEventListener("pointerdown", (e) => {
      const btn = e.target.closest && e.target.closest(".search-clear");
      if (!btn) return;
      // 不抢输入框焦点，移动端点击清除时软键盘不会收起
      e.preventDefault();
      // 触屏在 pointerdown 阶段就清除，避免部分浏览器抑制后续 click
      if (e.pointerType && e.pointerType !== "mouse") clearSearch(btn);
    });
    document.addEventListener("click", (e) => {
      const btn = e.target.closest && e.target.closest(".search-clear");
      if (btn) clearSearch(btn);
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
      if (!$("stickerPanel").hidden) return openStickerPanel(false);
      if (!$("sheetMask").hidden) return closeSheet();
      if (!$("inboxMask").hidden) return closeInbox();
      if (!$("privacyMask").hidden) return ($("privacyMask").hidden = true);
      if (!$("channelMask").hidden) return closeChannelMenu();
      if (!$("accMask").hidden) return ($("accMask").hidden = true);
      closeDrawer();
    });
  }

  /* ================= 启动 ================= */
  bindEvents();
  initTheme();
  loadStickers();

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
    getAvatar: () => S.avatar,
    setAvatar: saveAvatar,
    getOnline: () => S.online,
    openOnline,
    openInbox,
    openPrivacy,
    openChannelMenu,
    getChannel: () => currentChannel(),
    getChannels: () => S.channels,
    getSettings: () => S.settings,
  };
})();
