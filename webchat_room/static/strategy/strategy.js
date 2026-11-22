/* 仙域争霸 · 战略大地图前端（P0）
 * 依赖同源聊天室的登录态：localStorage["xiuxian_auth_v1"] = {user, token}
 */
"use strict";

const AUTH_KEY = "xiuxian_auth_v1";
const POLL_MS = 20000;
const WORLD_W = 1200, WORLD_H = 780;

const TYPE_ICON = { city: "🏯", pass: "⛰️", mine: "⛏️", grain: "🌾", market: "🏪", branch: "🏘️", ruin: "🗿" };
const TYPE_NAME = { city: "仙城", pass: "关隘", mine: "灵脉矿", grain: "灵谷田", market: "坊市", branch: "分舵", ruin: "遗迹" };
const TERRAIN_NAME = { plain: "平原", forest: "森林", mountain: "山地", river: "河流", swamp: "沼泽" };
const STATE_NAME = { idle: "驻守", march: "行军", siege: "围城", retreat: "撤退", escort: "护送粮队" };

function vetTierName(v) {
  const n = Number(v) || 0;
  if (n >= 300) return "御林";
  if (n >= 100) return "精锐";
  return "普通";
}
// 任务产出图标（后端 task_meta.output_kind / 进行中任务 output_kind）
const OUTPUT_ICON = {
  coin: "💰", grain: "🌾", iron: "⛏️", token: "🎟️", merit: "🎖️",
  tech: "🔬", build: "🛡️", train: "⚔️", heal: "💊", intel: "🕵️",
  grain_move: "🌾", loot: "💎", plot: "🎴",
};
// 羁绊（core/strategy_rules.BOND_NAMES 前端投影）
const BOND_NAMES = { tongdao: "同道", zhiyu: "知遇", yijiao: "义交", shitu: "师徒", suyuan: "宿怨" };
// 外交关系
const REL_NAMES = {
  neutral: "中立", war: "交战中", war_pending: "备战", ally: "同盟",
  truce: "停战", peace: "和谈中", propose_war: "宣战提案", propose_truce: "停战提案",
  propose_ally: "结盟提案", propose_peace: "和谈提案",
};
// 计略目标类型：char=角色ID / node=据点 / held=我方被俘角色
const PLOT_TARGET = {
  alienate: "char", rumor: "node", arson: "node",
  persuade_plot: "node", sabotage: "node", rescue: "held",
};

let S = null;            // 世界快照
let selectedNode = null; // 选中据点 id
let marchArmyId = null;  // 出征目标选择模式
let activeTab = "overview";
let lastRenderedTab = null;
let pollTimer = null;

// ---------------------------------------------------------------------------
// 基础工具
// ---------------------------------------------------------------------------
function auth() {
  try { return JSON.parse(localStorage.getItem(AUTH_KEY) || "null"); } catch (e) { return null; }
}

function toast(msg, kind) {
  const wrap = document.getElementById("toastWrap");
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? 6000 : 3500);
}

async function api(path, opts) {
  const a = auth();
  if (!a || !a.token) { location.href = "../index.html"; throw new Error("未登录"); }
  const resp = await fetch(path, Object.assign({
    headers: { "Authorization": "Bearer " + a.token, "Content-Type": "application/json" },
  }, opts || {}));
  let data = null;
  try { data = await resp.json(); } catch (e) { /* ignore */ }
  if (resp.status === 401) { location.href = "../index.html"; throw new Error("登录已过期"); }
  if (resp.status === 423) { toast("⏳ 世界推进中，请稍后再试", "err"); throw new Error("busy"); }
  if (!resp.ok || (data && data.ok === false)) {
    const msg = (data && (data.error || data.msg)) || ("HTTP " + resp.status);
    toast("❌ " + msg, "err");
    throw new Error(msg);
  }
  return data;
}

async function doAction(action, params) {
  try {
    const r = await api("/api/strategy/action", {
      method: "POST", body: JSON.stringify({ action, params: params || {} }),
    });
    if (r && r.msg) toast(r.msg, r.ok === false ? "err" : "ok");
    await load();
    return r;
  } catch (e) { return null; }
}

let loadSeq = 0;   // 请求序号：20s 轮询与动作后的 load() 可能乱序返回
async function load() {
  const seq = ++loadSeq;
  try {
    const r = await api("/api/strategy/state");
    // 旧请求晚到：丢弃，避免把新状态覆盖回旧快照（界面回滚/重复操作）
    if (seq !== loadSeq) return;
    S = r.state;
    document.getElementById("joinOverlay").hidden = !!S.joined;
    if (!S.joined) renderJoinCandidates();
    renderAll();
    checkManualBattles();
  } catch (e) { /* toast 已弹 */ }
}

// ---------------------------------------------------------------------------
// 浏览器通知（§12.5 F：首次进面板请求授权；手操战役轮询检测）
// ---------------------------------------------------------------------------
const NOTIFY_DISMISS_KEY = "xiuxian_notify_dismiss_v1";
let seenManualBattles = null;  // null = 尚未初始化（首载不弹，避免刷屏）

function requestNotifyPermission() {
  if (typeof Notification === "undefined") { toast("当前浏览器不支持通知", "err"); return; }
  Notification.requestPermission().then((perm) => {
    if (perm === "granted") {
      toast("🔔 通知已开启");
      document.getElementById("notifyBar").hidden = true;
      renderTab();
    } else toast("未获得通知授权", "err");
  });
}

function notifyLocal(title, body) {
  try {
    if (typeof Notification !== "undefined" && Notification.permission === "granted") {
      new Notification(title, { body, tag: title });
    }
  } catch (e) { /* 移动端可能抛错，忽略 */ }
}

function checkManualBattles() {
  const live = (S.battles || []).filter((b) =>
    b.mode === "manual" && ["deploy", "battle"].includes(b.state));
  const ids = new Set(live.map((b) => b.id));
  if (seenManualBattles === null) { seenManualBattles = ids; return; }
  for (const b of live) {
    if (!seenManualBattles.has(b.id)) {
      const node = nodeById(b.node_id);
      const where = node ? node.name : "据点#" + b.node_id;
      toast("🔴 手操战役打响：" + where + "（战报页可进入）");
      notifyLocal("⚔️ 仙域争霸 · 手操战役", where + " 战役开始，点击战报页进入手操！");
      if (activeTab === "battles") renderTab();
    }
  }
  seenManualBattles = ids;
}

function fmtNum(n) {
  if (n === null || n === undefined) return "?";
  return Number(n).toLocaleString("zh-CN");
}

function fmtRemain(sec) {
  sec = Math.max(0, Number(sec) || 0);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return h + "h" + (m > 0 ? m + "m" : "");
  if (m > 0) return m + "m" + (s > 0 ? s + "s" : "");
  return s + "s";
}

function nodeById(id) { return (S.nodes || []).find((n) => n.id === id); }

function myFactionId() { return S && S.player ? S.player.faction_id : -999; }

// ---------------------------------------------------------------------------
// 顶栏 / 倒计时
// ---------------------------------------------------------------------------
function renderTop() {
  const p = S.player || {};
  document.getElementById("resCoin").textContent = fmtNum(p.coin);
  document.getElementById("resGrain").textContent = fmtNum(p.grain);
  document.getElementById("resIron").textContent = fmtNum(p.iron);
  document.getElementById("resToken").textContent = fmtNum(p.recruit_token);
  document.getElementById("resMerit").textContent = fmtNum(p.merit);
  document.getElementById("tickNo").textContent =
    "第 " + S.world.tick_no + " tick · " + (S.world.template_id || "");
}

function tickCountdown() {
  if (!S) return;
  const el = document.getElementById("tickCountdown");
  const left = Math.max(0, S.world.next_tick_at - Math.floor(Date.now() / 1000));
  const m = String(Math.floor(left / 60)).padStart(2, "0");
  const s = String(left % 60).padStart(2, "0");
  el.textContent = "下次推进 " + m + ":" + s;
  document.querySelectorAll(".aucLeft").forEach((node) => {
    const exp = Number(node.dataset.expire || 0);
    node.textContent = fmtRemain(exp - Math.floor(Date.now() / 1000));
  });
}

// ---------------------------------------------------------------------------
// Canvas 大地图
// ---------------------------------------------------------------------------
const canvas = document.getElementById("mapCanvas");
const ctx = canvas.getContext("2d");
let view = { scale: 1, ox: 0, oy: 0 };

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  view.scale = Math.min(rect.width / WORLD_W, rect.height / WORLD_H) * 0.94;
  view.ox = (rect.width - WORLD_W * view.scale) / 2;
  view.oy = (rect.height - WORLD_H * view.scale) / 2;
  drawMap();
}

function toScreen(x, y) { return [view.ox + x * view.scale, view.oy + y * view.scale]; }
function toWorld(sx, sy) { return [(sx - view.ox) / view.scale, (sy - view.oy) / view.scale]; }

function drawMap() {
  if (!S) return;
  const w = canvas.width / (window.devicePixelRatio || 1);
  const h = canvas.height / (window.devicePixelRatio || 1);
  ctx.clearRect(0, 0, w, h);
  // 边
  ctx.strokeStyle = "#243044";
  ctx.lineWidth = 1.4;
  const seen = new Set();
  for (const n of S.nodes) {
    for (const adj of n.adj || []) {
      const key = Math.min(n.id, adj) + "-" + Math.max(n.id, adj);
      if (seen.has(key)) continue;
      seen.add(key);
      const t = nodeById(adj);
      if (!t) continue;
      const [x1, y1] = toScreen(n.pos[0], n.pos[1]);
      const [x2, y2] = toScreen(t.pos[0], t.pos[1]);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    }
  }
  // 行军箭头
  for (const a of S.armies || []) {
    if (a.state !== "march" || !a.path || !a.path.length) continue;
    const from = nodeById(a.node_id), to = nodeById(a.path[0]);
    if (!from || !to) continue;
    const [x1, y1] = toScreen(from.pos[0], from.pos[1]);
    const [x2, y2] = toScreen(to.pos[0], to.pos[1]);
    ctx.strokeStyle = a.faction_color || "#888";
    ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    ctx.setLineDash([]);
  }
  // 节点
  for (const n of S.nodes) {
    const [x, y] = toScreen(n.pos[0], n.pos[1]);
    const r = (n.type === "city" ? 15 : n.type === "pass" ? 12 : 10) * Math.max(0.8, view.scale);
    const color = safeColor(n.owner && n.owner.color);
    if (selectedNode === n.id) {
      ctx.strokeStyle = "#e3b341"; ctx.lineWidth = 2.5;
      ctx.beginPath(); ctx.arc(x, y, r + 5, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.fillStyle = "#0d1117";
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = color; ctx.lineWidth = 2.4;
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.stroke();
    ctx.font = (r * 0.95) + "px serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(TYPE_ICON[n.type] || "•", x, y + 0.5);
    // 名称 + 等级
    ctx.font = "11px sans-serif";
    ctx.fillStyle = "#c6d0dc";
    ctx.fillText(n.name + (n.level > 1 ? " Lv" + n.level : ""), x, y + r + 11);
    // 守军
    const gar = n.garrison !== null && n.garrison !== undefined
      ? String(n.garrison) : (n.garrison_bucket && n.garrison_bucket !== "0" ? n.garrison_bucket : "");
    if (gar && gar !== "0") {
      ctx.fillStyle = "#8b98a9"; ctx.font = "10px sans-serif";
      ctx.fillText("⚔" + gar, x, y - r - 7);
    }
    if (n.siege && n.siege.ticks > 0) {
      ctx.fillStyle = "#f85149"; ctx.font = "12px sans-serif";
      ctx.fillText("🔥", x + r + 5, y - r);
    }
    if (n.protected) {
      ctx.fillStyle = "#58a6ff"; ctx.font = "10px sans-serif";
      ctx.fillText("🛡", x - r - 6, y - r);
    }
  }
  // 军团标记
  for (const a of S.armies || []) {
    const n = nodeById(a.node_id);
    if (!n) continue;
    const [x, y] = toScreen(n.pos[0], n.pos[1]);
    ctx.fillStyle = a.faction_color || "#888";
    ctx.beginPath();
    ctx.moveTo(x + 12, y + 8); ctx.lineTo(x + 20, y + 8); ctx.lineTo(x + 16, y + 15);
    ctx.closePath(); ctx.fill();
    if (Number(a.veteran) >= 300) {
      ctx.strokeStyle = "#e3b341"; ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.moveTo(x + 12, y + 8); ctx.lineTo(x + 20, y + 8); ctx.lineTo(x + 16, y + 15);
      ctx.closePath(); ctx.stroke();
    }
    if (a.troops !== null && a.troops !== undefined) {
      ctx.font = "10px sans-serif"; ctx.textAlign = "left";
      ctx.fillText(String(a.troops), x + 22, y + 14);
      ctx.textAlign = "center";
    }
  }
}

canvas.addEventListener("click", (ev) => {
  if (!S) return;
  const rect = canvas.getBoundingClientRect();
  const [wx, wy] = toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
  let hit = null, bestD = 26 / view.scale;
  for (const n of S.nodes) {
    const d = Math.hypot(n.pos[0] - wx, n.pos[1] - wy);
    if (d < bestD) { bestD = d; hit = n; }
  }
  if (!hit) return;
  if (marchArmyId) {
    const id = marchArmyId; marchArmyId = null;
    canvas.style.cursor = "crosshair";
    doAction("march", { army_id: id, node_id: hit.id });
    return;
  }
  selectedNode = hit.id;
  renderDrawer();
  drawMap();
});

// ---------------------------------------------------------------------------
// 图例
// ---------------------------------------------------------------------------
function renderLegend() {
  const el = document.getElementById("legendList");
  const cnt = document.getElementById("legendCount");
  if (!el) return;
  const rows = (S.factions || []).filter((f) => f.node_count > 0 || f.id === myFactionId());
  el.innerHTML = rows.map((f) =>
    '<div class="lg-item"><span class="lg-dot" style="background:' + safeColor(f.color) + '"></span>' +
    esc(f.name) + "（" + f.node_count + "）</div>").join("");
  if (cnt) cnt.textContent = "(" + rows.length + ")";
}

// ---------------------------------------------------------------------------
// 图例：可收起 + 可拖动（手机端默认收起，避免遮挡地图）
// ---------------------------------------------------------------------------
const LEGEND_KEY = "xiuxian_strategy_legend_collapsed_v1";

function applyLegendCollapsed(collapsed) {
  const el = document.getElementById("mapLegend");
  const btn = document.getElementById("legendToggle");
  if (!el) return;
  el.classList.toggle("collapsed", !!collapsed);
  if (btn) btn.textContent = collapsed ? "展开" : "收起";
}

function initLegend() {
  const el = document.getElementById("mapLegend");
  const head = document.getElementById("legendHead");
  const btn = document.getElementById("legendToggle");
  if (!el || !head) return;
  let saved = null;
  try { saved = localStorage.getItem(LEGEND_KEY); } catch (e) { /* ignore */ }
  applyLegendCollapsed(saved === null ? window.innerWidth <= 860 : saved === "1");
  if (btn) {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const collapsed = !el.classList.contains("collapsed");
      applyLegendCollapsed(collapsed);
      try { localStorage.setItem(LEGEND_KEY, collapsed ? "1" : "0"); } catch (e) { /* ignore */ }
    });
  }
  let drag = null;
  head.addEventListener("pointerdown", (ev) => {
    if (ev.target.closest("button")) return;
    const rect = el.getBoundingClientRect();
    drag = { dx: ev.clientX - rect.left, dy: ev.clientY - rect.top };
    el.classList.add("dragging");
    try { head.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
  });
  head.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const pane = el.parentElement.getBoundingClientRect();
    const rect = el.getBoundingClientRect();
    const x = Math.min(Math.max(4, ev.clientX - drag.dx - pane.left), pane.width - rect.width - 4);
    const y = Math.min(Math.max(4, ev.clientY - drag.dy - pane.top), pane.height - rect.height - 4);
    el.style.left = x + "px";
    el.style.top = y + "px";
    el.style.right = "auto";
  });
  const end = () => { drag = null; el.classList.remove("dragging"); };
  head.addEventListener("pointerup", end);
  head.addEventListener("pointercancel", end);
  window.addEventListener("resize", () => {
    if (!el.style.left) return;
    const pane = el.parentElement.getBoundingClientRect();
    const rect = el.getBoundingClientRect();
    el.style.left = Math.min(parseFloat(el.style.left) || 4,
      Math.max(4, pane.width - rect.width - 4)) + "px";
    el.style.top = Math.min(parseFloat(el.style.top) || 4,
      Math.max(4, pane.height - rect.height - 4)) + "px";
  });
}

function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// 势力颜色只允许 #RGB/#RRGGBB：服务端已归一化，此处再做一次白名单，
// 防止旧数据库/其它写入路径把任意字符串拼进 style 属性。
function safeColor(c) {
  const v = String(c === null || c === undefined ? "" : c).trim();
  return /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(v) ? v : "#9e9e9e";
}

// ---------------------------------------------------------------------------
// 据点抽屉
// ---------------------------------------------------------------------------
// 20 秒轮询会整块重绘 tabBody / drawerBody：先快照输入值，重绘后回填，
// 避免玩家正在填写的编军/建设/搜索表单被清空（含光标丢失）。
function captureFormState(root) {
  const st = {};
  if (!root || !root.querySelectorAll) return st;
  root.querySelectorAll("input,select,textarea").forEach((el) => {
    if (!el.id) return;
    st[el.id] = (el.type === "checkbox" || el.type === "radio") ? !!el.checked : el.value;
  });
  return st;
}

function restoreFormState(root, st) {
  if (!root || !st) return;
  Object.keys(st).forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === "checkbox" || el.type === "radio") el.checked = !!st[id];
    else el.value = st[id];
  });
}

function renderDrawer() {
  const drawer = document.getElementById("nodeDrawer");
  const n = nodeById(selectedNode);
  if (!n) { drawer.hidden = true; return; }
  drawer.hidden = false;
  document.getElementById("drawerTitle").textContent =
    (TYPE_ICON[n.type] || "") + " " + n.name;
  const body = document.getElementById("drawerBody");
  const mine = n.owner && n.owner.id === myFactionId();
  const p = S.player || {};
  let html = "";
  html += kv("类型", (TYPE_NAME[n.type] || n.type) + " · " + (TERRAIN_NAME[n.terrain] || n.terrain) + " · Lv" + n.level);
  html += kv("归属", '<span style="color:' + (n.owner ? safeColor(n.owner.color) : "#999") + '">' + esc(n.owner ? n.owner.name : "无主") + "</span>");
  html += moraleBarHtml(n, mine);
  if (n.defense_max > 0) {
    const pct = Math.round(n.defense / n.defense_max * 100);
    html += kv("城防", n.defense + " / " + n.defense_max + ' <span class="sub">(' + pct + '%)</span>');
  }
  const gar = n.garrison !== null && n.garrison !== undefined ? n.garrison + " 兵" : (n.garrison_bucket ? "约 " + n.garrison_bucket : "无");
  html += kv("守军", gar);
  if (n.respawn_at) {
    const left = Math.max(0, Number(n.respawn_at) - Math.floor(Date.now() / 1000));
    if (left > 0) html += kv("重生", "👹 匪寨驻军约 " + fmtRemain(left) + " 后重生");
  }
  html += buildingSectionHtml(n, mine);
  if (n.siege && n.siege.ticks > 0) html += kv("围城", "已围 " + n.siege.ticks + " tick" + (n.siege.attacker_faction === myFactionId() ? "（我方发起）" : ""));
  if (n.protected) html += kv("保护", "🛡 保护期中");
  if (n.scouted_until) {
    const left = Math.max(0, n.scouted_until - Math.floor(Date.now() / 1000));
    html += kv("情报", "🕵️ 侦察情报有效（剩 " + fmtRemain(left) + "）");
  }
  // 驻扎军团（可见的）
  const stationed = (S.armies || []).filter((a) => a.node_id === n.id && a.state !== "march");
  if (stationed.length) {
    html += '<div class="kv"><span class="k">驻扎</span><span>' + stationed.map((a) =>
      esc(a.name) + (a.mine ? "（我方 " + a.troops + " 兵）" : "（" + a.troops + " 兵）")).join("<br>") + "</span></div>";
  }
  html += '<div class="drawer-actions">';
  // 围城中的手操介入（§7.2）
  if (n.siege && n.siege.ticks > 0) {
    if (n.siege.attacker_faction === myFactionId()) {
      html += '<button class="btn small primary" onclick="doAction(\'intervene_siege\',{node_id:' + n.id + '})">🎮 手操介入（发起攻城战）</button>';
    }
    if (mine) {
      html += '<button class="btn small danger" onclick="doAction(\'sortie\',{node_id:' + n.id + '})">⚔️ 出击（决战解围）</button>';
    }
  }
  if (mine) {
    html += '<button class="btn small" onclick="openArmyModal(' + n.id + ')">🛡️ 在此编军</button>';
    if (["city", "pass"].includes(n.type)) html += '<button class="btn small" onclick="doAction(\'upgrade_node\',{node_id:' + n.id + '})">⬆️ 升级（5000币/500铁起）</button>';
    html += taskFormHtml(n, p);
  } else {
    const idleArmies = (p.armies || []).filter((a) => a.state === "idle");
    for (const a of idleArmies) {
      html += '<button class="btn small primary" onclick="doAction(\'march\',{army_id:' + a.id + ",node_id:" + n.id + '})">⚔️ ' + esc(a.name) + " 出征至此</button>";
    }
    if (!idleArmies.length) html += '<span class="sub">（无可出征军团，先在己方据点编军）</span>';
  }
  html += "</div>";
  const st = captureFormState(body);
  body.innerHTML = html;
  restoreFormState(body, st);
}

function kv(k, v) { return '<div class="kv"><span class="k">' + k + "</span><span>" + v + "</span></div>"; }

// 民心进度条 + 赈济（§8.6，用户重点）
function moraleBarHtml(n, mine) {
  const cap = Math.max(1, Number(n.morale_cap || 100));
  const morale = Math.max(0, Math.min(cap, Number(n.morale || 0)));
  const cls = morale < 20 ? "bad" : morale < 50 ? "warn" : "";
  let h = '<div class="kv morale-kv"><span class="k">民心</span><span class="morale-wrap">' +
    '<span>' + morale + " / " + cap + "</span>" +
    '<span class="bar ' + cls + '"><i style="width:' + Math.round(morale / cap * 100) + '%"></i></span></span></div>';
  if (!mine) return h;
  const gcfg = ((S.meta || {}).governance_cfg) || {};
  const perNode = Math.max(1, Number(gcfg.relief_node_daily_limit || 2));
  const coinCost = Math.max(1, Number(gcfg.relief_coin_cost || 3000));
  const grainCost = Math.max(1, Number(gcfg.relief_grain_cost || 800));
  const gain = Math.max(1, Number(gcfg.relief_morale || 6));
  const used = Math.max(0, Number(n.relief_used || 0));
  const left = Math.max(0, perNode - used);
  const full = morale >= cap;
  const dis = (left <= 0 || full) ? " disabled" : "";
  h += '<div class="relief-box"><div class="sub">🎁 赈济：本据点今日剩余 <b>' + left + " / " + perNode +
    " 次</b>" + (full ? "（民心已满）" : left <= 0 ? "（今日已达上限）" : "") + "</div>" +
    '<div class="drawer-actions">' +
    '<button class="btn small primary" data-act="relief" data-id="' + n.id + '" data-res="coin"' + dis +
    ">🪙 赈济铜钱（" + fmtNum(coinCost) + "，+" + gain + " 民心）</button>" +
    '<button class="btn small" data-act="relief" data-id="' + n.id + '" data-res="grain"' + dis +
    ">🌾 赈济粮草（" + fmtNum(grainCost) + "，+" + gain + " 民心）</button>" +
    "</div></div>";
  return h;
}

// 建筑：9 种目录（S.buildings 或 S.meta.buildings），支持建设与升级
function buildingSectionHtml(n, mine) {
  if (!["city", "pass", "branch"].includes(n.type)) return "";
  const metaCatalog = (S.meta || {}).buildings || {};
  const ownCatalog = S.buildings || {};
  const catalog = Object.keys(ownCatalog).length ? ownCatalog : metaCatalog;
  const ids = Object.keys(catalog);
  if (!ids.length) return "";
  const levels = {};
  for (const b of (n.buildings || [])) {
    const bid = typeof b === "string" ? b : (b && b.id);
    if (!bid) continue;
    const lv = (b && typeof b === "object" && Number(b.level)) ? Number(b.level) : 1;
    levels[bid] = Math.max(levels[bid] || 0, Math.max(1, lv));
  }
  const built = Object.keys(levels);
  let h = '<div class="building-box"><div class="sub">🏗️ 建筑（' + ids.length + " 种，最高 5 级）</div>";
  if (built.length) {
    h += '<div class="tbl-wrap"><table class="tbl"><tr><th>建筑</th><th>等级</th><th>效果</th><th></th></tr>';
    for (const bid of built) {
      const spec = catalog[bid] || {};
      const maxLv = Number(spec.max || 5);
      const lv = Math.min(maxLv, levels[bid]);
      h += '<tr><td>' + esc(spec.name || bid) + "</td><td>Lv " + lv + " / " + maxLv +
        '</td><td class="sub">' + esc(spec.desc || "") + "</td><td>" +
        (mine && lv < maxLv
          ? '<button class="btn small" data-act="upgrade-building" data-id="' + n.id +
            '" data-building="' + esc(bid) + '">升级</button>'
          : '<span class="sub">' + (lv >= maxLv ? "满级" : "-") + "</span>") + "</td></tr>";
    }
    h += "</table></div>";
  } else {
    h += '<div class="sub">尚未建造任何建筑</div>';
  }
  if (mine) {
    const avail = ids.filter((id) => !levels[id]);
    if (avail.length) {
      h += '<div class="form-row building-build"><select id="buildSel_' + n.id + '" style="max-width:150px">' +
        avail.map((id) => {
          const spec = catalog[id] || {};
          return '<option value="' + esc(id) + '">' + esc(spec.name || id) + "（" +
            fmtNum(spec.coin || 0) + "币" + (spec.iron ? "/" + fmtNum(spec.iron) + "铁" : "") + "）</option>";
        }).join("") +
        '</select><button class="btn small" data-act="build-building" data-id="' + n.id + '">🏗️ 建设</button></div>';
    }
  }
  return h + "</div>";
}

function taskFormHtml(n, p) {
  const idle = (p.chars || []).filter((c) => c.status === "idle" && c.injury_state !== "dying" && c.injury_state !== "dead");
  if (!idle.length) return '<span class="sub">（无空闲角色可派遣任务）</span>';
  const charOpts = idle.map((c) => '<option value="' + c.id + '">' + esc(c.name) + "（" + c.role_name + "）</option>").join("");
  const meta = S.meta.task_meta || {};
  const taskOpts = Object.entries(S.meta.task_types).map(([t, name]) => {
    const m = meta[t] || {};
    const icon = OUTPUT_ICON[m.output_kind] || "";
    const label = m.name
      ? m.name + "·" + icon + (m.output || "产出") + "（" + (m.stat || "?") + "）"
      : name;
    const tip = [m.role ? "最佳职责：" + m.role : "", m.output ? "产出：" + m.output : "",
      m.node ? "据点加成：" + m.node : ""].filter(Boolean).join(" · ");
    return '<option value="' + t + '" title="' + esc(tip) + '">' + esc(label) + "</option>";
  }).join("");
  const outputLegend = Object.entries(S.meta.task_types).map(([t]) => {
    const m = meta[t] || {};
    return (m.name || t) + "→" + (OUTPUT_ICON[m.output_kind] || "") + (m.output || "?");
  }).join(" · ");
  const hourOpts = (S.meta.task_hours || [1, 2, 4, 8]).map((h) => '<option value="' + h + '">' + h + "h</option>").join("");
  const uid = "task_" + n.id;
  return '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:6px;width:100%">' +
    '<select id="' + uid + '_c" style="max-width:110px">' + charOpts + "</select>" +
    '<select id="' + uid + '_t" style="max-width:120px">' + taskOpts + "</select>" +
    '<select id="' + uid + '_h" style="max-width:60px">' + hourOpts + "</select>" +
    '<button class="btn small" onclick="dispatchFrom(\'' + uid + "'," + n.id + ')">📜 派遣</button></div>' +
    '<div class="sub" style="width:100%">括号内为任务核心属性；悬停可看最佳职责与据点加成</div>' +
    '<div class="sub" style="width:100%">任务产出：' + esc(outputLegend) + "</div>";
}

function dispatchFrom(uid, nodeId) {
  doAction("dispatch_task", {
    char_id: Number(document.getElementById(uid + "_c").value),
    node_id: nodeId,
    task_type: document.getElementById(uid + "_t").value,
    hours: Number(document.getElementById(uid + "_h").value),
  });
}

function taskPayoutText(t) {
  const icon = OUTPUT_ICON[t.output_kind] || "🎁";
  let s = icon + " " + esc(t.output_name || "产出");
  if (t.estimate !== null && t.estimate !== undefined) s += " ≈" + fmtNum(t.estimate);
  if (t.note) s += ' <span class="sub">（' + esc(t.note) + "）</span>";
  if (t.morale_gain) s += ' <span class="sub">· 民心 +' + t.morale_gain + "</span>";
  if (t.partner_name) s += ' <span class="sub">· 与 ' + esc(t.partner_name) + " 协作</span>";
  return s;
}

document.getElementById("drawerClose").addEventListener("click", () => {
  selectedNode = null;
  document.getElementById("nodeDrawer").hidden = true;
  drawMap();
});

// ---------------------------------------------------------------------------
// 右侧标签页
// ---------------------------------------------------------------------------
document.getElementById("tabs").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".tab");
  if (!btn) return;
  activeTab = btn.dataset.tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === btn));
  renderTab();
});

function renderTab() {
  const el = document.getElementById("tabBody");
  // 「指引」不依赖入场态：未入场也能查全部玩法文档（guide.js 提供 tabGuide/initGuide）
  const isGuide = activeTab === "guide";
  if ((!S || !S.joined) && !isGuide) {
    el.innerHTML = '<div class="empty">入场后展示</div>';
    return;
  }
  // 指引页已渲染时跳过重绘：20s 轮询不打断输入框焦点与光标（搜索词存于 guide.js state）
  if (isGuide && document.getElementById("guideSearch")) {
    if (typeof window.initGuide === "function") window.initGuide();
    return;
  }
  const fn = {
    overview: tabOverview, chars: tabChars, armies: tabArmies,
    faction: tabFaction, tavern: tabTavern, market: tabMarket,
    battles: tabBattles, shop: tabShop, honor: tabHonor, settings: tabSettings,
    guide: (window.tabGuide || (() => "")),
  }[activeTab];
  // 仅在同一标签页内重绘时回填输入值；切换标签不复用上一页的旧值
  const sameTab = lastRenderedTab === activeTab;
  const st = sameTab ? captureFormState(el) : null;
  el.innerHTML = fn ? fn() : "";
  if (sameTab) restoreFormState(el, st);
  lastRenderedTab = activeTab;
  bindTabEvents();
  // 指引页：事件全部委托在 #tabBody 上，重复调用不会重复绑定；
  // 搜索词保存在 guide.js 的 state 中，20s 轮询重绘后仍会回填。
  if (isGuide && typeof window.initGuide === "function") window.initGuide();
}

// ---------------------------------------------------------------------------
// 战报高光（§12.6）：后端 result.highlights 可能是英文码或中文短句，统一成中文
// ---------------------------------------------------------------------------
const HIGHLIGHT_NAMES = {
  duel_win: "单挑获胜", duel: "单挑决胜",
  encircle: "完成合围", night_raid: "夜袭得手",
  night: "夜袭作战", underdog: "以少胜多", ambush: "伏兵突袭",
};

// 战报列表不含 result.highlights 时，按需静默拉取详情并缓存（仅前 12 场）
const battleHlCache = {};
const battleHlPending = {};
function hydrateBattleHighlights(list) {
  const need = (list || []).filter((b) => b && b.state === "done" &&
    battleHlCache[b.id] === undefined && !battleHlPending[b.id] &&
    !battleHighlights(b).length).slice(0, 12);
  if (!need.length) return;
  const a = auth();
  if (!a || !a.token) return;
  need.forEach((b) => { battleHlPending[b.id] = true; });
  let left = need.length;
  need.forEach((b) => {
    fetch("/api/strategy/action", {
      method: "POST",
      headers: { "Authorization": "Bearer " + a.token, "Content-Type": "application/json" },
      body: JSON.stringify({ action: "battle_report", params: { battle_id: b.id } }),
    }).then((r) => (r.ok ? r.json() : null)).then((d) => {
      const res = (d && d.report && d.report.result) || {};
      battleHlCache[b.id] = ((res.highlights || []).map(highlightText).filter(Boolean));
    }).catch(() => { battleHlCache[b.id] = []; }).then(() => {
      delete battleHlPending[b.id];
      if (--left === 0 && activeTab === "battles") renderTab();
    });
  });
}

function highlightText(h) {
  const s = String(h === null || h === undefined ? "" : h).trim().replace(/^⭐\s*/, "");
  if (!s) return "";
  return HIGHLIGHT_NAMES[s] || s;
}

function battleHighlights(b) {
  if (b && Object.prototype.hasOwnProperty.call(battleHlCache, b.id) &&
      battleHlCache[b.id].length) {
    return battleHlCache[b.id];
  }
  const src = (b && (b.highlights || (b.result || {}).highlights)) || [];
  let raw = Array.isArray(src) ? src.slice() : [];
  // 兼容只落库 report_text 的旧战报：首行以 ⭐ 标记高光
  if (!raw.length && b && b.report_text) {
    const line = String(b.report_text).split("\n").find((x) => x.includes("⭐"));
    if (line) raw = line.split("⭐").map((x) => x.trim()).filter(Boolean);
  }
  return raw.map(highlightText).filter(Boolean);
}

function guideSteps() {
  const p = S.player || {};
  const f = p.faction || {};
  const nodes = myNodes();
  return [
    { title: "① 认领主城", done: nodes.length > 0,
      desc: nodes.length
        ? "已获得起始据点。点地图上的己方据点，抽屉里可查看详情、编军和派遣任务。"
        : "当前地图暂无空位：先在「势力」页申请加入玩家势力，或发起会签与道友共立门户。" },
    { title: "② 编成首支军团", done: (p.armies || []).length > 0,
      desc: "点击己方据点 →「在此编军」→ 选武将、剑盾兵 500，破城槌可选（攻城墙更有效）。" },
    { title: "③ 出征占地", done: nodes.length > 1,
      desc: "军团页点「选择目标出征」，再点地图上相邻的无主据点；守军约 800，建议兵力≥800。" },
    { title: "④ 派遣内政官", done: (p.tasks || []).length > 0,
      desc: "在己方据点抽屉底部，选内政官执行屯田/布政/挖矿，离线也会持续产出。" },
    { title: "⑤ 招贤纳士", done: (p.chars || []).length > 2,
      desc: "酒馆普通招募 800 铜钱；募将令高级招募 40 抽保底极品。更多武将才能多线出征。" },
    { title: "⑥ 势力与官职",
      done: (f.members || []).length > 1 || (f.appointments || []).length > 0,
      desc: (f.appointments || []).length
        ? "已任免 " + f.appointments.length + " 名角色的官职；可随时在势力页调整，主将带兵上限按其官职计算。"
        : "势力页可任免角色官职：据点越多，可任免的官职档位越高；主将带兵上限按其角色官职计算。" },
  ];
}

function guideCard() {
  const tut = (S.player || {}).tutorial || {};
  if (tut.guide_hidden) {
    return '<div class="guide-reopen"><button class="btn small" data-act="guide-show">📖 显示新手指引</button></div>';
  }
  const steps = guideSteps();
  const done = steps.filter((s) => s.done).length;
  let h = '<div class="card guide-card"><div class="guide-head"><h3>📖 新手指引</h3>' +
    '<span class="guide-progress">进度 ' + done + " / " + steps.length + "</span></div>" +
    '<div class="sub" style="margin:4px 0 6px">按顺序经营即可起步；随时可收起，设置页可重新开启。</div>';
  for (const s of steps) {
    h += '<div class="guide-step' + (s.done ? " done" : "") + '">' +
      '<span class="gs-mark">' + (s.done ? "✅" : "○") + "</span><span>" +
      '<span class="gs-title">' + s.title + '</span><br><span class="gs-desc">' + s.desc + "</span></span></div>";
  }
  h += '<div class="drawer-actions"><button class="btn small" data-act="guide-hide">暂时收起引导</button></div></div>';
  return h;
}

function tabOverview() {
  let h = guideCard();
  h += '<div class="card"><h3>🌏 世界局势</h3>';
  h += '<table class="tbl"><tr><th>势力</th><th>类型</th><th>据点</th><th>声望</th></tr>';
  for (const f of S.factions || []) {
    if (f.node_count <= 0 && f.id !== myFactionId()) continue;
    h += "<tr><td><span class='lg-dot' style='display:inline-block;background:" + safeColor(f.color) + "'></span> " + esc(f.name) +
      (f.id === myFactionId() ? "（我）" : "") + "</td><td>" +
      ({ npc: "NPC", player: "玩家", demon: "魔潮", none: "无主" }[f.kind] || f.kind) +
      "</td><td>" + f.node_count + "</td><td>" + f.reputation + "</td></tr>";
  }
  h += "</table></div>";
  const tasks = (S.player || {}).tasks || [];
  h += '<div class="card"><h3>📜 进行中任务</h3>';
  if (tasks.length) {
    h += '<table class="tbl"><tr><th>角色</th><th>任务</th><th>收益</th><th>据点</th><th>剩余</th><th></th></tr>' +
      tasks.map((t) => "<tr><td>" + esc(t.char_name) + "</td><td>" + esc(t.task_name) +
        "</td><td>" + taskPayoutText(t) +
        "</td><td>" + esc(t.node_name) + "</td><td>" + fmtRemain(t.remain_sec) +
        '</td><td><button class="btn small" data-act="recall-task" data-id="' + t.id +
        '" data-name="' + esc(t.char_name) + '">召回</button></td></tr>').join("") +
      "</table><div class='sub'>收益为满期预期值（不含 ±15% 随机波动与小事件）；随时可召回，已进行部分按比例结算，但收益额外损失 50%</div>";
  } else {
    h += '<span class="sub">暂无进行中的任务（在己方据点抽屉底部派遣）</span>';
  }
  h += "</div>";
  const evs = S.events || [];
  h += '<div class="card"><h3>🎲 进行中的事件</h3>' + (evs.length
    ? evs.map((e) => "· " + esc(e.name) + (e.target && e.target.node_name ? "（" + esc(e.target.node_name) + "）" : "（全境）") + " 剩 " + (e.end_tick - S.world.tick_no) + " tick").join("<br>")
    : '<span class="sub">天下太平</span>') + "</div>";
  const ticks = (S.recent_ticks || []).slice(-8).reverse();
  h += '<div class="card"><h3>🕰️ 最近推进</h3>' + (ticks.length
    ? '<table class="tbl"><tr><th>tick</th><th>战报</th><th>事件</th><th>灵石注入</th></tr>' +
      ticks.map((t) => "<tr><td>" + t.tick_no + (t.output_only ? "*" : "") + "</td><td>" + (t.battles || 0) +
        "</td><td>" + esc((t.events || []).join("、") || "-") + "</td><td>" + fmtNum(t.gold || 0) + "</td></tr>").join("") +
      "</table><div class='sub'>* 为离线补产出（跳过战斗/AI）</div>"
    : '<span class="sub">尚无记录</span>') + "</div>";
  return h;
}

function statGrid(stats) {
  const names = S.meta.stat_names;
  return Object.entries(names).map(([k, name]) => name + " " + (stats[k] || 0)).join(" · ");
}

function charCard(c) {
  const apt = Object.entries(c.aptitude || {}).map(([t, g]) => (S.meta.troop_types[t] || {}).name + ":" + g).join(" ");
  const bonds = Object.entries(c.bonds || {}).map(([cid, b]) => {
    const other = ((S.player || {}).chars || []).find((x) => String(x.id) === String(cid));
    const type = (b && b.type) || "";
    return (BOND_NAMES[type] || type || "?") + "·" + esc(other ? other.name : "#" + cid);
  }).join("、");
  return '<div class="card"><h3>' + esc(c.name) +
    ' <span class="tag ' + (["fine", "rare", "epic", "legend"].includes(c.rarity) ? c.rarity : "") + '">' + c.rarity_name + "</span>" +
    ' <span class="tag">' + c.role_name + "</span><span class='tag'>" + c.profession_name + "</span>" +
    " Lv" + c.level + "</h3>" +
    '<div class="sub">' + statGrid(c.stats) + "</div>" +
    (c.trait_names && c.trait_names.length ? '<div class="sub">特技：' + c.trait_names.join("、") + "</div>" : "") +
    (apt ? '<div class="sub">适性：' + esc(apt) + "</div>" : "") +
    (bonds ? '<div class="sub">羁绊：' + bonds + "</div>" : "") +
    '<div class="sub">阅历 ' + c.exp + "/" + c.exp_next +
    " · 成长 " + (c.stat_total || 0) + "/" + (c.stat_cap || 0) +
    " · 忠诚 " + c.loyalty +
    " · 状态 " + ({ idle: "空闲", task: "执行任务", army: "随军", captive: "被俘", wander: "在野", dead: "陨落" }[c.status] || c.status) +
    (c.location ? " · 位于 " + esc(c.location) : "") + "</div>" +
    (c.appointed_position
      ? '<div class="sub">🏛 官职：' + esc(c.appointed_name || positionName(c.appointed_position)) +
        (c.appointed_node_name ? "（辖 " + esc(c.appointed_node_name) + "）" : "") + "</div>"
      : "") +
    '<div class="sub">伤势 ' + c.injury_name + "(" + c.injury + ") · 体力 " + c.stamina + "</div>" +
    '<div class="bar ' + (c.injury >= 60 ? "bad" : c.injury >= 30 ? "warn" : "") + '"><i style="width:' + (100 - c.injury) + '%"></i></div>' +
    (c.status === "idle"
      ? '<div class="drawer-actions">' +
        (c.role !== "official"
          ? '<button class="btn small" data-act="duel-open" data-id="' + c.id + '">🎯 摆擂（单挑赌注）</button>'
          : "") +
        (!c.appointed_position
          ? '<button class="btn small danger" data-act="dismiss-char" data-id="' + c.id +
            '" data-name="' + esc(c.name) + '">🗑 遣散（腾出名额）</button>'
          : "") +
        "</div>"
      : "") +
    "</div>";
}

function tabChars() {
  const p = S.player || {};
  const chars = (p.chars || []).filter((c) => !["dead", "captive"].includes(c.status));
  const dead = (p.chars || []).filter((c) => c.status === "dead");
  let h = '<div class="card"><h3>👥 名册 ' + (p.char_count || 0) + " / " + (p.char_cap || 0) + "</h3>" +
    '<div class="sub">按本势力据点数量提供名额：当前 ' + (p.char_cap_nodes || 0) +
    " 座据点，每座 +" + (p.char_cap_per_node || 0) + " 名；被俘/陨落角色不占名额。角色的六维总和可通过任务与战斗历练，最高成长到稀有度上限 +" +
    (p.growth_cap_bonus || 0) + "，满额后可遣散腾出名额。</div></div>";
  if (!chars.length && !dead.length) return h + '<div class="empty">尚无角色，去酒馆招募吧</div>';
  const sections = [
    ["⚔️ 武将", chars.filter((c) => c.role === "general")],
    ["🪶 军师", chars.filter((c) => c.role === "strategist")],
    ["📜 内政官", chars.filter((c) => c.role === "official")],
  ];
  for (const [title, list] of sections) {
    if (!list.length) continue;
    h += '<div class="section-title">' + title + "（" + list.length + "）</div>";
    h += list.map(charCard).join("");
  }
  if (dead.length) {
    h += '<div class="section-title">🕯️ 英烈祠（' + dead.length + "）</div>";
    h += '<div class="card"><div class="sub">' + dead.map((c) =>
      "🪦 " + esc(c.name) + "（" + c.rarity_name + " " + c.role_name + "）").join("<br>") +
      "</div></div>";
  }
  return h;
}

function tabArmies() {
  const p = S.player || {};
  let h = '<div class="card"><h3>🛡️ 我的军团</h3><div class="sub">上限 ' +
    ((S.meta.positions || {})["none"] || {}).cap + " 兵/无官职主将 · 出征沿通路行军，粮尽则士气崩溃</div></div>";
  const armies = p.armies || [];
  if (!armies.length) return h + '<div class="empty">尚无军团：在己方据点抽屉中「在此编军」</div>';
  for (const a of armies) {
    h += '<div class="card"><h3>' + esc(a.name) + ' <span class="tag">' + a.troop_name + "</span>" +
      (a.siege_engine ? ' <span class="tag epic">破城槌</span>' : "") + "</h3>" +
      '<div class="sub">兵力 ' + fmtNum(a.troops) + " · 士气 " + a.morale + " · 精锐 " +
      a.veteran + "（" + vetTierName(a.veteran) + "） · 携粮 " + fmtNum(a.grain) + "</div>" +
      '<div class="sub">主将/随行：' + a.characters.map(esc).join("、") + "</div>" +
      '<div class="sub">状态：' + (STATE_NAME[a.state] || a.state) + " @ " + esc(a.node_name) +
      (a.path && a.path.length ? " → " + a.path.map(esc).join(" → ") : "") + "</div>" +
      '<div class="drawer-actions">' + armyButtons(a) + "</div></div>";
  }
  return h;
}

function armyButtons(a) {
  let b = "";
  if (a.state === "idle") {
    b += '<button class="btn small primary" data-act="march-pick" data-id="' + a.id + '">⚔️ 选择目标出征</button>';
    b += '<button class="btn small" onclick="refillPrompt(' + a.id + ')">➕ 补员</button>';
    b += '<button class="btn small" onclick="openSupplyModal(' + a.id + ')">🚚 补粮</button>';
    b += '<button class="btn small" onclick="doAction(\'drill_army\',{army_id:' + a.id + '})">🎯 操练(500币)</button>';
    b += '<button class="btn small danger" onclick="doAction(\'disband_army\',{army_id:' + a.id + '})">解散</button>';
  } else if (a.state === "siege") {
    b += '<button class="btn small" onclick="openSupplyModal(' + a.id + ')">🚚 补粮</button>';
    b += '<button class="btn small" onclick="doAction(\'siege_choice\',{army_id:' + a.id + ",choice:'street_fight'})" + '">巷战</button>';
    b += '<button class="btn small" onclick="doAction(\'siege_choice\',{army_id:' + a.id + ",choice:'surrender'})" + '">受降(需3倍兵力)</button>';
    b += '<button class="btn small danger" onclick="doAction(\'retreat\',{army_id:' + a.id + '})">撤围</button>';
  } else if (a.state === "escort") {
    b += '<span class="sub">护送运粮队中，抵达或被截后自动归队</span>';
  } else {
    b += '<button class="btn small" onclick="openSupplyModal(' + a.id + ')">🚚 补粮</button>';
    b += '<button class="btn small danger" onclick="doAction(\'retreat\',{army_id:' + a.id + '})">🏳️ 撤退</button>';
  }
  return b;
}

let supplyArmyId = 0;

function openSupplyModal(armyId) {
  const p = S.player || {};
  const army = (p.armies || []).find((a) => a.id === armyId);
  if (!army) return;
  const idle = (p.chars || []).filter((c) => c.status === "idle" &&
    c.injury_state !== "dying" && c.injury_state !== "dead");
  if (!idle.length) { toast("没有空闲角色可押运", "err"); return; }
  supplyArmyId = armyId;
  document.getElementById("supplyArmyHint").textContent =
    "目标：" + army.name + "（" + (STATE_NAME[army.state] || army.state) + " · " +
    army.node_name + " · 携粮 " + fmtNum(army.grain) + "）" +
    (army.state === "idle" ? "，驻扎中，交付立即完成"
      : "，补粮队速度高于普通行军，追上后交付（角色占用至交付或超时）");
  document.getElementById("supplyChar").innerHTML = idle.map((c) =>
    '<option value="' + c.id + '">' + esc(c.name) + "（" + c.role_name + "）</option>").join("");
  const hours = (S.meta.task_hours || [1, 2, 4, 8]);
  document.getElementById("supplyHours").innerHTML = hours.map((h) =>
    '<option value="' + h + '">' + h + "小时</option>").join("");
  document.getElementById("supplyCost").textContent =
    "运粮量 = 军团单 tick 粮耗 × 所选时长对应 tick（受携粮上限：统率×50，粮仓 +10%/级）；" +
    "「折算时长」只决定运粮量，行军/围城军团由补粮队追上即交付，不代表角色占用时长。";
  document.getElementById("supplyOverlay").hidden = false;
}

document.getElementById("supplyCancel").addEventListener("click", () => {
  document.getElementById("supplyOverlay").hidden = true;
});
document.getElementById("supplyConfirm").addEventListener("click", () => {
  const army = (S.player.armies || []).find((a) => a.id === supplyArmyId);
  doAction("dispatch_task", {
    char_id: Number(document.getElementById("supplyChar").value),
    node_id: army ? army.node_id : 0,
    task_type: "supply",
    hours: Number(document.getElementById("supplyHours").value),
    army_id: supplyArmyId,
  }).then(() => { document.getElementById("supplyOverlay").hidden = true; });
});

function refillPrompt(armyId) {
  const n = prompt("补员数量（100 的倍数，需己方据点）：", "100");
  if (n) doAction("refill_army", { army_id: armyId, troops: Number(n) });
}

function tabTavern() {
  const p = S.player || {};
  const t = p.tavern || {};
  let h = '<div class="card"><h3>🍶 招贤酒馆</h3><div class="sub">名册 ' +
    (p.char_count || 0) + " / " + (p.char_cap || 0) + "（据点 " +
    (p.char_cap_nodes || 0) + " 座 × 每座 +" + (p.char_cap_per_node || 0) +
    "）；普通招募 800 铜钱；募将令招募必得良品以上，40 抽保底极品+（当前保底进度 " +
    ((p.honor || {}).pity || 0) + "/40）</div>" +
    '<div class="drawer-actions"><button class="btn small" id="btnTavernRefresh">🔄 手动刷新(300币 ' +
    (t.daily_refresh_used || 0) + "/3)</button></div></div>";
  const cands = t.candidates || [];
  if (!cands.length) return h + '<div class="empty">酒过三巡，客官请刷新</div>';
  cands.forEach((c, i) => {
    h += '<div class="card"><h3>' + esc(c.name) + ' <span class="tag ' + c.rarity + '">' +
      ((S.meta.rarity_names || {})[c.rarity] || c.rarity) + "</span><span class='tag'>" +
      ((S.meta.role_names || {})[c.role] || c.role) + "</span><span class='tag'>" +
      ((S.meta.profession_names || {})[c.profession] || c.profession) + "</span></h3>" +
      '<div class="sub">' + statGrid(c.stats || {}) + "</div>" +
      (c.traits && c.traits.length ? '<div class="sub">特技：' + c.traits.join("、") + "</div>" : "") +
      '<div class="drawer-actions">' +
      '<button class="btn small" data-act="recruit" data-i="' + i + '">招募(800币)</button>' +
      '<button class="btn small primary" data-act="recruit-premium" data-i="' + i + '">募将令招募(' + (p.recruit_token || 0) + ")</button>" +
      "</div></div>";
  });
  return h;
}

function tabBattles() {
  const all = S.battles || [];
  if (!all.length) return '<div class="empty">尚无战报</div>';
  // §12.6 高光战报置顶（同组内保持后端原顺序：新→旧）
  const bs = all.map((b) => ({ b, hl: battleHighlights(b) }))
    .sort((x, y) => (y.hl.length ? 1 : 0) - (x.hl.length ? 1 : 0));
  // 列表缺 highlights 字段时后台补拉详情（会缓存，拉全后自动重排一次）
  hydrateBattleHighlights(all);
  return bs.map(({ b, hl }) => {
    const when = new Date(b.created_at * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const node = nodeById(b.node_id);
    const manualLive = b.mode === "manual" && ["deploy", "battle"].includes(b.state);
    const hlTag = hl.length ? ' <span class="tag star">⭐ 高光</span>' : "";
    const hlLine = hl.length
      ? '<div class="hl-line">⭐ ' + hl.map((t) => '<span class="hl-item">' + esc(t) + "</span>").join("") + "</div>"
      : "";
    if (manualLive) {
      const left = Math.max(0, Number(b.deadline_at || 0) - Math.floor(Date.now() / 1000));
      return '<div class="card live"><h3>🔴 ' +
        ({ field: "会战", street: "巷战", siege: "攻城战" }[b.kind] || "战役") +
        " · " + esc(node ? node.name : "据点#" + b.node_id) +
        ' <span class="tag epic">' + (b.state === "deploy" ? "部署中" : "手操进行中") + "</span>" + hlTag + "</h3>" +
        '<div class="sub">' + (b.state === "deploy" ? "部署剩余 " + left + " 秒" : "回合限时 " + left + " 秒") + "</div>" +
        hlLine +
        '<div class="drawer-actions"><button class="btn small primary" data-act="enter-battle" data-id="' + b.id + '">⚔️ 进入手操</button>' +
        '<button class="btn small" data-act="spectate-battle" data-id="' + b.id + '">👁 观战（只读）</button></div></div>';
    }
    const win = b.winner === "attacker" ? "攻方胜" : b.winner === "defender" ? "守方胜" : "未分胜负";
    return '<div class="card' + (hl.length ? " highlight" : "") + '"><h3>' +
      ({ field: "⚔️", street: "🏙️", siege: "🔥" }[b.kind] || "⚔️") +
      " " + esc(node ? node.name : "据点#" + b.node_id) + " · " + win +
      (b.mode === "manual" ? ' <span class="tag">手操</span>' : "") + hlTag +
      ' <span class="sub">' + when + " · " + b.rounds + " 回合</span></h3>" +
      hlLine +
      '<details><summary class="sub">展开战报</summary><div class="report-text">' + esc(b.report_text) + "</div></details>" +
      '<div class="drawer-actions">' +
      '<button class="btn small" data-act="enter-battle" data-id="' + b.id + '">🎞️ 复盘/回放</button>' +
      '<button class="btn small" data-act="spectate-battle" data-id="' + b.id + '">👁 观战（只读）</button>' +
      "</div></div>";
  }).join("");
}

function tabShop() {
  const p = S.player || {};
  const meta = S.meta || {};
  const shop = meta.merit_shop || {};
  const injury = meta.injury_cfg || {};
  const goldSpec = Object.assign({ merit: 500, gold: 100000, weekly_limit: 5 }, shop.gold_pack || {});
  const ironSpec = Object.assign({ merit: 300, iron: 500, weekly_limit: 3 }, shop.iron_pack || {});
  const materialSpec = Object.assign({ merit: 300, coin: 8000, grain: 8000, iron: 400, weekly_limit: 3 }, shop.material_pack || {});
  const equipSpec = Object.assign({ merit: 3000, monthly_limit: 1, quality: "极佳" }, shop.equip_instance || {});
  const letterSpec = Object.assign({ merit: 200 }, shop.recommend_letter || {});
  const elixirSpec = Object.assign({ merit: 1500, season_limit_per_char: 2 }, shop.elixir || {});
  const elixirCost = Number(injury.elixir_merit_cost || elixirSpec.merit || 1500);
  const elixirHeal = Number(injury.elixir_heal || 40);
  const elixirLimit = Number(elixirSpec.season_limit_per_char || injury.elixir_season_limit || 2);
  const honor = p.honor || {};
  const week = honor.merit_shop_week || {};
  const ironWeek = honor.merit_shop_week_iron || {};
  const materialWeek = honor.merit_shop_week_material || {};
  const month = honor.merit_shop_month || {};
  const monthUsed = month.used || {};
  const elixirUsed = honor.elixir_used || {};
  const injured = (p.chars || []).filter((c) => c.injury > 0);
  let h = '<div class="card"><h3>🏅 我的战功</h3><div class="sub">当前 <b>' + fmtNum(p.merit) +
    '</b>，可在本页兑换战略资源；赛季结算后折算为荣誉点。</div></div>';
  h += '<div class="card"><h3>💰 灵石包</h3><div class="sub">' + goldSpec.merit + " 战功 → " +
    fmtNum(goldSpec.gold) + " 灵石（注入主玩法钱包，计入每日限额；达硬上限时拒绝兑换且不扣战功）<br>本周已兑 " +
    (Number(week.used) || 0) + "/" + goldSpec.weekly_limit + "</div>" +
    '<div class="drawer-actions"><button class="btn small primary" data-act="buy-gold">兑换灵石包</button></div></div>';
  h += '<div class="card"><h3>⛏️ 精铁包</h3><div class="sub">' + ironSpec.merit + " 战功 → " +
    fmtNum(ironSpec.iron) + " 精铁（建筑/科技通用，无灵脉矿也能补缺口）<br>本周已兑 " +
    (Number(ironWeek.used) || 0) + "/" + ironSpec.weekly_limit + "</div>" +
    '<div class="drawer-actions"><button class="btn small primary" data-act="buy-iron">兑换精铁包</button></div></div>';
  h += '<div class="card"><h3>📦 物资包</h3><div class="sub">' + materialSpec.merit + " 战功 → 铜钱 +" +
    fmtNum(materialSpec.coin) + "、粮草 +" + fmtNum(materialSpec.grain) + "、精铁 +" +
    fmtNum(materialSpec.iron) + "<br>本周已兑 " + (Number(materialWeek.used) || 0) + "/" +
    materialSpec.weekly_limit + "</div>" +
    '<div class="drawer-actions"><button class="btn small primary" data-act="buy-material">兑换物资包</button></div></div>';
  h += '<div class="card"><h3>📜 举荐信</h3><div class="sub">' + letterSpec.merit +
    " 战功 → 定向刷新酒馆候选职责（本次全部为该职责）</div>" +
    '<div class="form-row"><select id="recommendRole">' +
    Object.keys(meta.role_names || { general: "武将", strategist: "军师", official: "内政官" })
      .map((k) => '<option value="' + k + '">' + esc((meta.role_names || {})[k] || k) + "</option>").join("") +
    '</select><button class="btn small primary" data-act="buy-recommend">使用举荐信</button></div></div>';
  h += '<div class="card"><h3>🗡️ 装备副本</h3><div class="sub">' + equipSpec.merit +
    " 战功 → 主玩法随机装备（" + esc(equipSpec.quality || "极佳") +
    "，按当前境界掉落，存入储物戒）<br>本月已兑 " +
    (Number(monthUsed.equip_instance) || 0) + "/" + equipSpec.monthly_limit + "</div>" +
    '<div class="drawer-actions"><button class="btn small primary" data-act="buy-equip">兑换装备</button></div></div>';
  h += '<div class="card"><h3>💊 疗伤丹</h3><div class="sub">' + elixirCost + " 战功 → 指定角色伤势 -" +
    elixirHeal + "（每角色每赛季 " + elixirLimit + " 次）</div>";
  if (injured.length) {
    h += '<div class="form-row"><select id="elixirChar">' + injured.map((c) =>
      '<option value="' + c.id + '">' + esc(c.name) + "（" + c.injury_name + " " + c.injury + "，已用 " + (elixirUsed[String(c.id)] || 0) + "/" + elixirLimit + "）</option>").join("") +
      '</select><button class="btn small primary" data-act="buy-elixir">服用</button></div>';
  } else {
    h += '<div class="sub">（当前无受伤角色）</div>';
  }
  h += "</div>";
  const goldDailyCap = Number((meta.gold_output || {}).daily_gold_cap || 5000000);
  h += '<div class="card"><h3>📖 战功来源</h3><div class="sub">会战击杀 ×0.05 + 胜利 100（手操获胜 ×1.5）；讨伐匪寨 100~200（魔潮期 ×2）；成就册解锁；据点灵石产出超过每日限额（' + fmtNum(goldDailyCap) + '）的部分自动折算战功</div></div>';
  return h;
}

// ---------------------------------------------------------------------------
// 势力（P1 §4/§6.3/§8）
// ---------------------------------------------------------------------------
function myNodes() {
  return (S.nodes || []).filter((n) => n.owner && n.owner.id === myFactionId());
}

function friendlyNodes() {
  const myFid = myFactionId();
  const me = (S.factions || []).find((f) => f.id === myFid) || {};
  const aid = Number(me.alliance_id || 0);
  return (S.nodes || []).filter((n) => {
    const oid = n.owner ? n.owner.id : 0;
    if (oid === myFid) return true;
    return aid > 0 && n.owner && Number(n.owner.alliance_id || 0) === aid;
  });
}

function positionName(pos) {
  return ((S.meta.positions || {})[pos || "none"] || {}).name || "无官职";
}

function appointmentCard(f) {
  const appPos = f.appointable || {};
  const posKeys = Object.keys(appPos);
  const govCount = Number(f.governance_node_count || 0);
  let h = '<div class="card"><h3>🏛 角色官职任免</h3>' +
    '<div class="sub">官职绑定具体角色，主将带兵上限按其官职计算；可任免档位随本势力（含联盟）据点数量开放。当前据点 <b>' +
    govCount + '</b> 座。</div>';
  if (posKeys.length) {
    h += '<div class="tbl-wrap"><table class="tbl"><tr><th>官职</th><th>名额</th><th>带兵上限</th><th>势力效果</th></tr>';
    for (const k of posKeys) {
      const v = appPos[k];
      h += '<tr><td>' + esc(v.name) + '</td><td>' + v.used + " / " + v.cap +
        '</td><td>' + fmtNum(v.troop_cap) + '</td><td class="sub">' + esc(v.effect || "-") + '</td></tr>';
    }
    h += "</table></div>";
  } else {
    h += '<div class="sub">尚未开放角色官职：占领 1 座据点后开放中郎将。</div>';
  }
  const unlocks = (S.meta.position_unlocks || []).filter((u) => Number(u.nodes) > govCount);
  if (unlocks.length) {
    h += '<div class="sub" style="margin-top:6px">后续开放：' + unlocks.map((u) =>
      u.nodes + " 据点 → " + Object.keys(u.positions || {}).map((k) =>
        ((S.meta.appointable_positions || {})[k] || {}).name || k).join("、")).join("；") + "</div>";
  }
  const apps = f.appointments || [];
  if (apps.length) {
    h += '<div class="tbl-wrap" style="margin-top:8px"><table class="tbl"><tr><th>角色</th><th>官职</th><th>所辖据点</th></tr>' +
      apps.map((a) => "<tr><td>" + esc(a.name) + ' <span class="sub">(' + esc(a.owner_user_id) +
        ")</span></td><td>" + esc(a.position_name) + "</td><td>" +
        (a.node_name ? esc(a.node_name) : "-") + "</td></tr>").join("") + "</table></div>";
  }
  if (f.can_appoint) {
    const chars = f.appoint_chars || [];
    if (chars.length && posKeys.length) {
      const allyIds = new Set([f.id].concat((f.allied_factions || []).map((x) => x.id)));
      const govNodes = (S.nodes || []).filter((n) =>
        n.owner && allyIds.has(n.owner.id) && n.type !== "ruin");
      h += '<div class="form-row" style="margin-top:8px"><select id="appointChar" style="max-width:170px">' +
        chars.map((c) => '<option value="' + c.id + '">' + esc(c.name) + "（" + esc(c.role_name) +
          (c.position ? "·" + positionName(c.position) : "") + "）</option>").join("") +
        '</select><select id="appointPos" style="max-width:150px"><option value="">（卸任）</option>' +
        posKeys.map((k) => '<option value="' + k + '">' + esc(appPos[k].name) +
          "（" + appPos[k].used + "/" + appPos[k].cap + "）</option>").join("") +
        '</select><select id="appointNode" style="max-width:150px">' +
        govNodes.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
        '</select><button class="btn small primary" data-act="appoint-char">任命 / 卸任</button></div>' +
        '<div class="sub">任免「太守」时须指定所辖据点；其他官职会忽略据点选项。角色离任后带兵上限立即回落。</div>';
    } else if (!chars.length) {
      h += '<div class="sub">（暂无角色可任免）</div>';
    }
  } else {
    h += '<div class="sub">（仅势力主/副主可任免角色官职）</div>';
  }
  return h + "</div>";
}

function tabFaction() {
  const p = S.player || {};
  const f = p.faction;
  if (!f) return factionNoHome(p);
  let h = "";
  const inAlliance = !!f.alliance_id;
  const myAllianceName = f.alliance_name || f.name;
  // 官员可参与联盟/势力共同治理
  const canGovern = ["leader", "deputy", "situ", "taiwei", "jijiu"].includes(f.my_position);
  const canAppoint = ["leader", "deputy"].includes(f.my_position);
  const isLeader = f.my_position === "leader";
  // --- 概况 ---
  h += '<div class="card"><h3><span class="lg-dot" style="display:inline-block;background:' +
    safeColor(((S.factions || []).find((x) => x.id === f.id) || {}).color) + '"></span> ' + esc(f.name) +
    ' <span class="tag">' + (f.kind === "player" ? "玩家势力" : f.kind) + "</span>" +
    (inAlliance ? ' <span class="tag">联盟：' + esc(myAllianceName) + "</span>" : "") + "</h3>" +
    '<div class="sub">等级 ' + f.level + " · 声望 " + f.reputation + " · 我的治理身份 " +
    positionName(f.my_position) + "（带兵上限由主将该角色官职决定）</div>" +
    kv((inAlliance ? "联盟国库" : "国库"), "🪙 " + fmtNum(f.treasury.coin || 0) + " · 🌾 " + fmtNum(f.treasury.grain || 0) +
      " · ⛏️ " + fmtNum(f.treasury.iron || 0) + " · 💎 " + fmtNum(f.treasury.gold || 0)) +
    kv("成员", f.members.length + " / " + f.member_cap + (inAlliance ? "（联盟共同名册）" : "")) +
    kv("本势力据点", (f.node_count || 0) + " 座") +
    kv("主城", esc((nodeById(f.capital_node_id) || {}).name || "已陷落")) + "</div>";
  // --- 联盟概况与据点交接 ---
  if (inAlliance) {
    const allied = f.allied_factions || [];
    const allyIds = new Set(allied.map((x) => x.id));
    h += '<div class="card"><h3>🤝 联盟「' + esc(myAllianceName) + '」</h3>' +
      '<div class="sub">结盟后各势力保留自身据点，政治 / 国库 / 科技 / 外交由联盟共同治理；盟主：' +
      esc((allied.find((x) => x.is_host) || {}).name || "?") + "</div>";
    h += '<div class="tbl-wrap"><table class="tbl"><tr><th>势力</th><th>据点</th><th>主城</th><th>身份</th></tr>';
    for (const a of allied) {
      h += "<tr><td>" + esc(a.name) + (a.id === f.id ? "（我）" : "") + "</td><td>" +
        a.node_count + "</td><td>" + esc((nodeById(a.capital_node_id) || {}).name || "-") +
        "</td><td>" + (a.is_host ? "盟主" : "成员") + "</td></tr>";
    }
    h += "</table></div>";
    if (canGovern && allied.length > 1) {
      const allyNodes = (S.nodes || []).filter((n) => n.owner && allyIds.has(n.owner.id) && n.type !== "ruin");
      h += '<div class="sub" style="margin-top:8px">据点交接：把本联盟内某座据点改归另一成员势力（守军与建筑保留）</div>' +
        '<div class="form-row alliance-transfer"><select id="transferNode" style="max-width:170px">' +
        allyNodes.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "（" + esc((n.owner || {}).name || "?") + "）</option>").join("") +
        '</select><select id="transferTarget" style="max-width:140px">' +
        allied.map((a) => '<option value="' + a.id + '">' + esc(a.name) + "</option>").join("") +
        '</select><button class="btn small primary" data-act="transfer-node">🔁 交接据点</button></div>';
    }
    h += "</div>";
  }
  // --- 结盟（未结盟时） ---
  if (!inAlliance) {
    if (p.faction_invite) {
      h += '<div class="card live"><h3>💌 结盟邀请</h3><div class="sub">势力 #' +
        p.faction_invite.fid + ' 邀请你的势力结盟</div><div class="form-row">' +
        '<button class="btn small primary" data-act="invite-respond" data-accept="1">接受</button>' +
        '<button class="btn small" data-act="invite-respond" data-accept="0">婉拒</button></div></div>';
    }
    const others = (S.factions || []).filter(
      (x) => x.kind === "player" && x.alive && x.id !== f.id && !x.alliance_id);
    h += '<div class="card"><h3>🤝 结盟</h3>' +
      '<div class="sub">结盟后双方保留各自势力与据点，政治 / 国库 / 科技 / 外交共同治理</div>';
    if (isLeader && others.length) {
      for (const x of others) {
        h += '<div class="form-row"><span class="lg-dot" style="display:inline-block;background:' +
          safeColor(x.color) + '"></span> ' + esc(x.name) + "（声望 " + x.reputation +
          " · 据点 " + x.node_count + "）" +
          '<button class="btn small" data-act="faction-apply" data-id="' + x.id +
          '">申请结盟</button></div>';
      }
    } else if (!others.length) {
      h += '<div class="sub">（暂无可结盟的其他玩家势力）</div>';
    } else {
      h += '<div class="sub">（仅势力主可发起结盟申请）</div>';
    }
    if (canGovern) {
      h += '<div class="form-row"><input id="inviteUser" placeholder="对方玩家 ID" style="max-width:160px">' +
        '<button class="btn small primary" data-act="faction-invite">💌 邀请结盟</button></div>';
    }
    h += "</div>";
  }
  // --- 治理（官员可操作） ---
  if (canGovern && (f.applications || []).length) {
    h += '<div class="card"><h3>📥 结盟申请</h3>' + f.applications.map((u) =>
      '<div class="form-row"><span class="sub">' + esc(u) + "</span>" +
      '<button class="btn small primary" data-act="approve" data-u="' + esc(u) + '">批准结盟</button>' +
      '<button class="btn small" data-act="reject" data-u="' + esc(u) + '">拒绝</button></div>').join("") + "</div>";
  }
  // --- 角色官职（v74：绑定角色，按据点解锁） ---
  h += appointmentCard(f);
  // --- 成员贡献榜（玩家治理身份：主公/副主） ---
  h += '<div class="card"><h3>👥 成员贡献榜</h3><div class="sub">这里的「治理身份」决定谁可管理国库/科技/外交，与角色官职相互独立。</div>' +
    '<table class="tbl"><tr><th>成员</th><th>治理身份</th><th>贡献</th>' +
    (canAppoint ? "<th>设置副主</th>" : "") + "</tr>";
  for (const m of [...f.members].sort((a, b) => b.contribution - a.contribution)) {
    const pname = m.position === "leader" ? "主公" : (m.position === "deputy" ? "副主" : "成员");
    h += "<tr><td>" + esc(m.user_id) + (m.user_id === p.user_id ? "（我）" : "") +
      "</td><td>" + pname + "</td><td>" + fmtNum(m.contribution) + "</td>";
    if (canAppoint) {
      if (m.position === "leader") {
        h += "<td>-</td>";
      } else {
        h += '<td><select id="pos_' + m.user_id.replace(/[^a-zA-Z0-9_-]/g, "_") + '" style="max-width:90px">' +
          '<option value=""' + ((m.position || "") === "" ? " selected" : "") + ">成员</option>" +
          '<option value="deputy"' + ((m.position || "") === "deputy" ? " selected" : "") + ">副主</option>" +
          '</select> <button class="btn small" data-act="set-position" data-u="' + esc(m.user_id) + '">设置</button></td>';
      }
    } else h += "<td>-</td>";
    h += "</tr>";
  }
  h += "</table>";
  if (f.my_position === "leader" && f.members.length > 1) {
    const ownMembers = f.members.filter((m) => m.user_id !== p.user_id &&
      m.faction_id === (f.governance_id || f.id));
    if (ownMembers.length) {
      h += '<div class="form-row"><select id="transferSel" style="max-width:150px">' +
        ownMembers.map((m) =>
          '<option value="' + esc(m.user_id) + '">' + esc(m.user_id) + "</option>").join("") +
        '</select><button class="btn small danger" data-act="transfer-leader">👑 ' +
        (inAlliance ? "移交盟主" : "禅让势力主") + "</button></div>";
    }
  }
  h += '<div class="form-row"><select id="depositRes" style="max-width:80px">' +
    '<option value="coin">铜钱</option><option value="grain">粮草</option><option value="iron">精铁</option></select>' +
    '<input id="depositAmt" type="number" min="1" placeholder="数量" style="max-width:90px">' +
    '<button class="btn small" data-act="deposit">📥 捐入国库</button>';
  if (canAppoint) {
    h += '<select id="withdrawRes" style="max-width:80px">' +
      '<option value="coin">铜钱</option><option value="grain">粮草</option><option value="iron">精铁</option></select>' +
      '<input id="withdrawAmt" type="number" min="1" placeholder="支取" style="max-width:90px">' +
      '<button class="btn small" data-act="withdraw">📤 国库支取</button>';
  }
  h += "</div></div>";
  // --- 势力任务 / 动员令（S.faction_ext）---
  const ext = S.faction_ext || {};
  h += '<div class="card"><h3>📜 势力任务</h3>' +
    '<div class="sub">每 12 小时刷 3 条，24 小时内完成；成员共同推进后可领取战功</div>';
  const tasks = ext.tasks;
  if (!Array.isArray(tasks)) {
    h += '<div class="sub">（势力任务数据暂未下发）</div>';
  } else if (!tasks.length) {
    h += '<div class="sub">（暂无进行中的势力任务）</div>';
  } else {
    const nowSec = Math.floor(Date.now() / 1000);
    h += '<table class="tbl"><tr><th>任务</th><th>进度</th><th>奖励</th><th>剩余</th><th></th></tr>';
    for (const t of tasks) {
      const done = Number(t.progress || 0) >= Number(t.target || 1);
      const left = Math.max(0, Number(t.end_at || 0) - nowSec);
      const merit = Number((t.reward || {}).merit || 0);
      h += "<tr><td>" + esc(t.title || t.task_type || "?") + "</td><td>" +
        Number(t.progress || 0) + " / " + Number(t.target || 1) + "</td><td>+" +
        merit + " 战功</td><td>" + (left ? fmtRemain(left) : "已到期") + "</td><td>" +
        (done
          ? '<button class="btn small primary" data-act="task-claim" data-id="' + t.id + '">领取</button>'
          : '<span class="sub">推进中</span>') + "</td></tr>";
    }
    h += "</table>";
  }
  if (["leader", "deputy", "jijiu"].includes(f.my_position)) {
    h += '<div class="form-row"><button class="btn small" data-act="task-refresh">🔄 刷新势力任务</button>' +
      '<span class="sub">主公 / 副主 / 祭酒 · 冷却 12 小时</span></div>';
  } else {
    h += '<div class="sub">（仅主公 / 副主 / 祭酒可手动刷新）</div>';
  }
  h += "</div>";
  h += '<div class="card"><h3>📣 动员令</h3>' +
    '<div class="sub">消耗 60 战功，持续 12 小时，冷却 24 小时；四选一全军 buff</div>';
  const mob = ext.mobilization;
  if (mob === undefined) {
    h += '<div class="sub">（动员令状态暂未下发）</div>';
  } else if (mob) {
    const mobLeft = Math.max(0, Number(mob.ends_at || 0) - Math.floor(Date.now() / 1000));
    h += '<div class="sub">当前生效：' + esc(mob.name || mob.kind || "?") +
      "（剩余 " + (mobLeft ? fmtRemain(mobLeft) : "即将结束") + "）</div>";
  } else {
    h += '<div class="sub">（当前无生效动员令）</div>';
  }
  if (["leader", "deputy", "jijiu"].includes(f.my_position)) {
    const MOB_KINDS = [
      ["recruit", "征兵动员"], ["march", "行军动员"],
      ["defend", "守土动员"], ["attack", "征伐动员"],
    ];
    h += '<div class="form-row"><select id="mobKind" style="max-width:130px">' +
      MOB_KINDS.map(([k, n]) => '<option value="' + k + '">' + n + "</option>").join("") +
      '</select><button class="btn small primary" data-act="mob-issue">发布动员令</button>' +
      '<span class="sub">我的战功 ' + fmtNum(p.merit || 0) + "</span></div>";
  } else {
    h += '<div class="sub">（仅主公 / 副主 / 祭酒可发布）</div>';
  }
  h += "</div>";
  // --- 外交矩阵 ---
  h += '<div class="card"><h3>🤝 外交</h3>';
  const favorCfg = ((S.meta || {}).favor_cfg) || {};
  const allyMin = Number(favorCfg.ally_min || 60);
  h += '<div class="sub">与 NPC 势力存在好感度：赠礼、通商、停战/和谈、共同敌人等会提升；宣战、背盟、突袭会降低。好感 ≥ ' +
    allyMin + " 才能尝试结盟。好感会随时间缓慢回落。</div>";
  const rels = f.relations || [];
  if (rels.length) {
    h += '<table class="tbl"><tr><th>势力</th><th>关系</th><th>好感</th><th>操作</th></tr>';
    for (const r of rels) {
      let ops = "-";
      if (r.rel.startsWith("propose_") && r.from !== f.id && canGovern) {
        ops = '<button class="btn small primary" data-act="dip-respond" data-id="' + r.faction_id + '" data-accept="1">接受</button> ' +
          '<button class="btn small" data-act="dip-respond" data-id="' + r.faction_id + '" data-accept="0">拒绝</button>';
      }
      const favor = Number(r.favor || 0);
      const tier = r.favor_name || "中立";
      const favorColor = favor >= allyMin ? "#2e7d32" : (favor <= -20 ? "#c62828" : "#666");
      h += "<tr><td>" + esc(r.name) + (r.kind === "npc" ? ' <span class="tag">NPC</span>' : "") +
        "</td><td>" + (REL_NAMES[r.rel] || r.rel) +
        (r.indemnity ? "（赔款 " + fmtNum(r.indemnity) + "）" : "") +
        '</td><td style="color:' + favorColor + '">' + favor + " · " + tier +
        '</td><td>' + ops + "</td></tr>";
    }
    h += "</table>";
  }
  if (canGovern) {
    const others = (S.factions || []).filter((x) => x.alive && x.id !== f.id &&
      (x.kind === "player" || x.kind === "npc"));
    h += '<div class="form-row"><select id="dipTarget" style="max-width:150px">' +
      others.map((x) => '<option value="' + x.id + '">' +
        (x.kind === "npc" ? "🏯 " : "") + esc(x.name) +
        (x.kind === "npc" ? "（好感 " + Number(x.favor || 0) + "）" : "") +
        "</option>").join("") +
      '</select><select id="dipKind" style="max-width:100px">' +
      '<option value="ally">结盟提案</option><option value="truce">停战提案</option>' +
      '<option value="peace">和谈（赔款）</option><option value="war">宣战</option></select>' +
      '<input id="dipIndemnity" type="number" min="0" placeholder="赔款(和谈)" style="max-width:100px">' +
      '<button class="btn small primary" data-act="dip-propose">发起</button></div>';
    const npcs = others.filter((x) => x.kind === "npc");
    if (npcs.length) {
      h += '<div class="form-row"><span class="sub">🎁 赠礼：</span>' +
        '<select id="dipGiftTarget" style="max-width:150px">' +
        npcs.map((x) => '<option value="' + x.id + '">' + esc(x.name) +
          "（好感 " + Number(x.favor || 0) + "）</option>").join("") +
        '</select><select id="dipGiftRes" style="max-width:80px">' +
        '<option value="coin">铜钱</option><option value="grain">粮草</option>' +
        '<option value="iron">精铁</option></select>' +
        '<input id="dipGiftAmt" type="number" min="1" placeholder="数量" style="max-width:100px">' +
        '<button class="btn small" data-act="dip-gift">赠送</button>' +
        '<span class="sub">约 ' + Number(favorCfg.gift_coin_per_point || 800) +
        " 币 = 1 好感，每日上限 " + Number(favorCfg.gift_daily_cap || 40) + "</span></div>";
    }
  } else h += '<div class="sub">（仅势力主/副主/司徒/太尉/祭酒可发起外交）</div>';
  h += "</div>";
  // --- 科技树 ---
  h += '<div class="card"><h3>🔬 势力科技</h3>' +
    '<div class="sub">科技点 ' + fmtNum((f.tech || {}).points || 0) +
    '（研究任务产出；每次开研究可抵扣最多 50% 时长，1 点 = 1 分钟）</div>' +
    '<table class="tbl"><tr><th>科技线</th><th>等级</th><th></th></tr>';
  const techNames = { military: "军事（攻伐加成）", agri: "农耕（产出加成）", stratagem: "谋略（计略加成）" };
  for (const line of S.meta.tech_lines || ["military", "agri", "stratagem"]) {
    const lv = Number((f.tech || {})[line] || 0);
    h += "<tr><td>" + (techNames[line] || line) + '</td><td><span class="tag">' + lv +
      " / 5</span></td><td>" + (lv < 5 && canGovern
        ? '<button class="btn small" data-act="tech" data-line="' + line + '">研究（国库出资）</button>'
        : lv >= 5 ? '<span class="sub">已满级</span>' : '<span class="sub">-</span>') + "</td></tr>";
  }
  h += "</table></div>";
  // --- 计略 ---
  const strategists = (p.chars || []).filter((c) => c.role === "strategist" && c.status === "idle" && !["dying", "dead"].includes(c.injury_state));
  h += '<div class="card"><h3>🕵️ 计略派遣</h3>';
  if (strategists.length) {
    const enemyNodes = (S.nodes || []).filter((n) => !(n.owner && n.owner.id === myFactionId()));
    h += '<div class="form-row"><select id="plotChar" style="max-width:110px">' +
      strategists.map((c) => '<option value="' + c.id + '">' + esc(c.name) + "</option>").join("") +
      '</select><select id="plotType" style="max-width:110px">' +
      Object.entries(S.meta.plot_types || {}).map(([k, v]) => '<option value="' + k + '">' + v + "</option>").join("") +
      '</select><select id="plotTarget" style="max-width:150px">' +
      enemyNodes.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
      '</select><button class="btn small primary" data-act="dispatch-plot">施展</button></div>' +
      '<div class="sub">造谣/纵火/劝降/策应以据点为目标；离间需敌方角色 ID（下方按钮输入）；营救在俘虏页操作；成功率 35~85%，失败可能走漏风声</div>' +
      '<div class="form-row"><button class="btn small" data-act="alienate">🗣️ 离间（输入敌方角色 ID）</button></div>';
  } else h += '<div class="sub">（无空闲军师，无法施展计略）</div>';
  if ((S.winds || []).length) {
    h += '<div class="sub" style="margin-top:6px">🌬️ 风声：' + S.winds.map(esc).join("<br>🌬️ ") + "</div>";
  }
  h += "</div>";
  // --- 俘虏 ---
  const cap = p.captives || { holding: [], held: [] };
  const myJailNodes = myNodes().filter((n) => ["city", "pass", "branch"].includes(n.type));
  h += '<div class="card"><h3>⛓️ 俘虏</h3>';
  if (cap.holding.length) {
    h += '<div class="sub">我方关押：</div><table class="tbl"><tr><th>俘虏</th><th>忠诚</th><th>操作</th></tr>';
    for (const c of cap.holding) {
      const destOpts = myJailNodes.filter((n) => n.id !== Number(c.node_id || 0));
      h += "<tr><td>" + esc(c.name) + ' <span class="tag ' + c.rarity + '">' +
        ((S.meta.rarity_names || {})[c.rarity] || c.rarity) + "</span>" +
        (c.node_id ? ' <span class="sub">@' + esc((nodeById(c.node_id) || {}).name || ("#" + c.node_id)) + "</span>" : "") +
        "</td><td>" +
        (c.disloyal ? '<span style="color:#f85149">离心!</span> ' : "") + c.loyalty + "</td><td>" +
        '<button class="btn small primary" data-act="persuade" data-id="' + c.id + '">劝降</button> ' +
        '<button class="btn small" data-act="ransom-set" data-id="' + c.id + '">定价赎回</button> ' +
        '<button class="btn small" data-act="release" data-id="' + c.id + '">释放</button>';
      if (destOpts.length) {
        h += ' <select id="xfer_' + c.id + '" style="max-width:100px">' +
          destOpts.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
          '</select><button class="btn small" data-act="transfer-captive" data-id="' + c.id +
          '">🔒 押运</button>';
      }
      h += "</td></tr>";
    }
    h += "</table>";
  }
  const transfers = S.transfers;
  if (Array.isArray(transfers) && transfers.length) {
    h += '<div class="sub" style="margin-top:6px">押运途中：</div>' + transfers.map((t) =>
      '<div class="sub">🔒 ' + esc(t.name || ("#" + t.char_id)) + " → " +
      esc((nodeById(t.to_node) || {}).name || ("#" + t.to_node)) +
      "（约 " + (t.hours_left != null ? t.hours_left + "h" : "?") + "）</div>").join("");
  } else if (transfers === undefined && cap.holding.length) {
    h += '<div class="sub">（押运队列数据暂未下发）</div>';
  }
  if (cap.held.length) {
    const freeGen = (p.chars || []).filter((c) => c.role === "general" && c.status === "idle");
    h += '<div class="sub">我方被俘：</div>' + cap.held.map((c) =>
      '<div class="form-row">🪖 ' + esc(c.name) + "（忠诚 " + c.loyalty + "，关押于据点#" + c.node_id + "）" +
      '<button class="btn small primary" data-act="ransom-pay" data-id="' + c.id + '">💰 支付赎金</button>' +
      (freeGen.length ? '<button class="btn small" data-act="rescue-plot" data-id="' + c.id + '">🕵️ 细作营救</button>' : "") +
      "</div>").join("");
  }
  if (!cap.holding.length && !cap.held.length) h += '<div class="sub">（无俘虏事务）</div>';
  h += "</div>";
  // --- 主城防御 ---
  h += '<div class="card"><h3>🏯 主城与防御</h3>';
  const cities = myNodes().filter((n) => ["city", "pass"].includes(n.type));
  if (cities.length) {
    h += '<div class="form-row"><select id="shieldNode" style="max-width:130px">' +
      cities.map((n) => '<option value="' + n.id + '">' + esc(n.name) + (n.protected ? "（已护盾）" : "") + "</option>").join("") +
      '</select><button class="btn small" data-act="buy-shield">🛡️ 购买保护罩</button></div>';
  }
  if (f.capital_node_id) {
    const relocateNodes = myNodes().filter((n) =>
      ["city", "pass", "branch"].includes(n.type) && n.id !== f.capital_node_id);
    const cdUntil = Number(ext.relocate_cd_until || 0);
    const cdLeft = Math.max(0, cdUntil - Math.floor(Date.now() / 1000));
    if (isLeader && relocateNodes.length) {
      h += '<div class="form-row"><select id="relocateNode" style="max-width:150px">' +
        relocateNodes.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
        '</select><button class="btn small" data-act="capital-relocate"' +
        (cdLeft > 0 ? " disabled" : "") + ">🏯 迁都</button>" +
        (cdLeft > 0
          ? '<span class="sub">冷却中 ' + fmtRemain(cdLeft) + "</span>"
          : '<span class="sub">扣国库铜钱 · 仅主公</span>') + "</div>";
    } else if (isLeader && !relocateNodes.length) {
      h += '<div class="sub">（无可迁都目标：需另有城池 / 关隘 / 分舵）</div>';
    } else if (!isLeader) {
      h += '<div class="sub">（仅主公可迁都）</div>';
    }
  }
  if (!f.capital_node_id) {
    const free = (S.nodes || []).filter((n) => !n.owner && ["city", "pass", "branch"].includes(n.type));
    const fund = ext.rebuild_fund;
    if (fund !== undefined) {
      h += '<div class="sub">重建合力：' + fmtNum(fund) + " 灵石</div>";
    } else {
      h += '<div class="sub">（重建合力数据暂未下发）</div>';
    }
    if (free.length) {
      h += '<div class="form-row"><select id="rebuildNode" style="max-width:150px">' +
        free.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
        '</select><button class="btn small danger" data-act="rebuild-capital">🔥 重建主城（花灵石，残军散尽）</button></div>';
    } else {
      h += '<div class="sub">（已无无主城池/关隘/分舵可供重建；可申请加入其他玩家势力）</div>';
    }
    h += '<div class="form-row"><input id="rebuildDonateAmt" type="number" min="1" placeholder="灵石数量" style="max-width:110px">' +
      '<button class="btn small" data-act="rebuild-donate">💰 重建捐献</button>' +
      '<span class="sub">成员可共同凑款</span></div>';
  }
  h += '<div class="form-row"><button class="btn small danger" data-act="faction-quit">' +
    (inAlliance ? (f.is_alliance_host ? "💔 解散联盟" : "🚪 退出联盟") : "🚪 退出势力") +
    "</button></div></div>";
  return h;
}

function factionNoHome(p) {
  let h = '<div class="card"><h3>🏳️ 你还没有势力</h3><div class="sub">散修可独自经营，也可会签立派或与其他势力结盟</div></div>';
  if (p.faction_invite) {
    h += '<div class="card live"><h3>💌 结盟邀请</h3><div class="sub">势力 #' + p.faction_invite.fid +
      " 邀请你结盟</div><div class=\"form-row\">" +
      '<button class="btn small primary" data-act="invite-respond" data-accept="1">接受</button>' +
      '<button class="btn small" data-act="invite-respond" data-accept="0">婉拒</button></div></div>';
  }
  const pets = S.petitions || [];
  if (pets.length) {
    h += '<div class="card"><h3>✍️ 进行中的会签</h3>';
    for (const pet of pets) {
      const left = Math.max(0, pet.expire_at - Math.floor(Date.now() / 1000));
      h += '<div class="form-row"><span class="lg-dot" style="display:inline-block;background:' + safeColor(pet.color) + '"></span> ' +
        esc(pet.name) + "（" + pet.cosigners + "/3 签 · 剩 " + Math.round(left / 3600) + "h）" +
        '<button class="btn small primary" data-act="cosign" data-id="' + esc(pet.id) + '">会签（垫付会费）</button></div>';
    }
    h += "</div>";
  }
  h += '<div class="card"><h3>🎌 发起会签立派</h3><div class="form-row">' +
    '<input id="newFactionName" placeholder="势力名（2~8字）" style="max-width:140px">' +
    '<input id="newFactionColor" type="color" value="#58a6ff" style="max-width:50px">' +
    '<button class="btn small primary" data-act="faction-create">发起（垫付 10000 铜钱，3 人成派）</button></div></div>';
  const playerFs = (S.factions || []).filter((x) => x.kind === "player" && x.alive);
  if (playerFs.length) {
    h += '<div class="card"><h3>🏰 玩家势力</h3>';
    for (const x of playerFs) {
      h += '<div class="form-row"><span class="lg-dot" style="display:inline-block;background:' + safeColor(x.color) + '"></span> ' +
        esc(x.name) + "（声望 " + x.reputation + " · 据点 " + x.node_count + "）" +
        '<button class="btn small" data-act="faction-apply" data-id="' + x.id + '">申请结盟</button></div>';
    }
    h += "</div>";
  }
  return h;
}

// ---------------------------------------------------------------------------
// 坊市（P3 §5.5：互市/商路/粮队/匪寨/流将）
// ---------------------------------------------------------------------------
const ITEM_NAMES = { grain: "粮草", iron: "精铁", recruit_token: "募将令", intel: "情报" };

function tabMarket() {
  const p = S.player || {};
  let h = "";
  // --- 流将战功暗标（§11 天降流将）---
  const auctions = S.wanderer_auctions || [];
  h += '<div class="card"><h3>🎴 流将暗标（战功）</h3>';
  if (auctions.length) {
    for (const a of auctions) {
      const own = Number(a.my_bid || 0);
      const quotaText = a.quota_limit > 0
        ? '名额 ' + a.quota_used + '/' + a.quota_limit +
          '（剩余 ' + a.quota_left + '）'
        : '名额不限';
      h += '<div class="card thin"><h3>' + esc(a.name) +
        ' <span class="tag ' + a.rarity + '">' + esc(a.rarity_name || a.rarity) + "</span>" +
        '<span class="tag">' + esc(a.role_name || "") + "</span></h3>" +
        '<div class="sub">' + statGrid(a.stats || {}) + " · 流落 " + esc(a.node_name || "?") +
        ' · 剩余 <span class="aucLeft" data-expire="' + a.expire_at + '">' +
        fmtRemain(a.expire_at - Math.floor(Date.now() / 1000)) + "</span></div>" +
        ((a.trait_names || []).length
          ? '<div class="sub">特技：' + a.trait_names.map(esc).join("、") + "</div>" : "") +
        '<div class="sub">底价 ' + fmtNum(a.start_price) + " 战功 · 你的暗标 " +
        (own > 0 ? fmtNum(own) + "（仅你可见）" : "未出价") +
        " · " + (a.bidder_count > 0 ? a.bidder_count + " 人已出价" : "暂无人出价") + "</div>" +
        '<div class="sub">' + esc(quotaText) + " · 坊市归属：" +
        esc(a.controller_name || "无主") +
        (a.access === "priority" && a.priority_until
          ? '（优先期剩余 <span class="aucLeft" data-expire="' + a.priority_until + '">' +
            fmtRemain(a.priority_until - Math.floor(Date.now() / 1000)) + "</span>）"
          : "") + "</div>" +
        (a.block_reason
          ? '<div class="sub">🔒 ' + esc(a.block_reason) + "</div>"
          : (a.can_bid
            ? '<div class="form-row"><input id="aid_' + a.id + '" type="number" min="' +
              a.min_next + '" value="' + (own > 0 ? a.min_next : a.min_next) +
              '" style="max-width:130px">' +
              '<button class="btn small primary" data-act="bid-wanderer" data-id="' +
              a.id + '">' + (own > 0 ? "改价" : "提交暗标") + "</button>" +
              (own > 0
                ? '<button class="btn small" data-act="cancel-wanderer-bid" data-id="' +
                  a.id + '">撤标</button>'
                : "") + "</div>" +
              '<div class="sub">暗标仅自己可见，到期统一开标；落标全额退还战功。</div>'
            : "")) +
        "</div>";
    }
  } else {
    h += '<div class="sub">（暂无流将挂拍；世界事件「天降流将」开启时，极品/传世流将会在坊市挂出暗标）</div>';
  }
  h += "</div>";
  // --- 挂单 ---
  const orders = p.market_orders || [];
  h += '<div class="card"><h3>🏪 互市挂单</h3>';
  if (orders.length) {
    h += '<table class="tbl"><tr><th>据点</th><th>商品</th><th>数量</th><th>单价</th><th>卖家</th><th></th></tr>';
    for (const o of orders) {
      const mineOrder = o.seller_faction === myFactionId();
      h += "<tr><td>" + esc((nodeById(o.node_id) || {}).name || "#" + o.node_id) +
        "</td><td>" + (o.item_name || ((S.meta || {}).resource_names || {})[o.item_type] ||
          ITEM_NAMES[o.item_type] || o.item_type) + "</td><td>" + fmtNum(o.amount) +
        "</td><td>" + fmtNum(o.unit_price) + "</td><td>" +
        (mineOrder ? "我方" : "势力#" + o.seller_faction) + "</td><td>" +
        (mineOrder
          ? '<button class="btn small" data-act="market-cancel" data-id="' + o.order_id + '">撤单</button>'
          : '<button class="btn small primary" data-act="market-buy" data-id="' + o.order_id + '" data-max="' + o.amount + '">买入</button>') +
        "</td></tr>";
    }
    h += "</table>";
  } else h += '<div class="sub">（当前无挂单）</div>';
  const mkNodes = myNodes().filter((n) => ["market", "city"].includes(n.type));
  const mkItems = ((S.meta || {}).market_cfg || {}).items || Object.keys(ITEM_NAMES);
  if (mkNodes.length) {
    h += '<div class="form-row"><select id="mkNode" style="max-width:110px">' +
      mkNodes.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
      '</select><select id="mkItem" style="max-width:90px">' +
      mkItems.map((k) => '<option value="' + k + '">' +
        (((S.meta || {}).resource_names || {})[k] || ITEM_NAMES[k] || k) + "</option>").join("") +
      '</select><input id="mkAmount" type="number" min="1" placeholder="数量" style="max-width:80px">' +
      '<input id="mkPrice" type="number" min="1" placeholder="单价(币)" style="max-width:90px">' +
      '<button class="btn small primary" data-act="market-list">挂售</button></div>';
  } else h += '<div class="sub">（占据坊市/仙城后可挂售）</div>';
  h += "</div>";
  // --- 商路 ---
  const routes = S.trade_routes || [];
  const trCfg = S.trade_route_cfg || {};
  const trCoin = fmtNum(trCfg.coin_per_tick != null ? trCfg.coin_per_tick : 20);
  const trBonus = Math.round(Number(
    trCfg.trade_task_bonus != null ? trCfg.trade_task_bonus : 0.3) * 100);
  h += '<div class="card"><h3>🛤️ 商路</h3>' +
    '<div class="sub">自动开通：己方/盟友据点全程连通两个坊市或仙城。每条商路每 tick +' +
    trCoin + " 币（归起点所属势力），途经节点的贸易任务 +" + trBonus +
    "%；被敌方切断会收到提醒。</div>";
  if (routes.length) {
    h += routes.map((r) => '<div class="sub">• ' + esc(r.a_name) + " ⇄ " + esc(r.b_name) +
      "（途经 " + esc((r.path_names || []).join(" → ")) + "）</div>").join("");
  } else {
    h += '<div class="sub">（暂无商路：占领并连通两个坊市/仙城即可自动开通）</div>';
  }
  h += "</div>";
  // --- 运粮队 ---
  const convoyCfg = S.convoy_cfg || {};
  const baseGuard = fmtNum(convoyCfg.troops != null ? convoyCfg.troops : 100);
  h += '<div class="card"><h3>🚚 运粮队</h3>' +
    '<div class="sub">粮队自带护卫 ' + baseGuard +
    " 兵，可另派空闲军团护送（护送期间军团随队移动、不可出征/解散）。</div>";
  const convoys = p.convoys || [];
  if (convoys.length) {
    h += convoys.map((c) => {
      if (c.kind === "army_supply") {
        return '<div class="sub">🚚 补粮队 · ' + fmtNum(c.grain) + " 粮 · 追击 " +
          esc(c.to || "?") + "（现于 " + esc(c.cur_node || "?") + " · " +
          esc(c.progress || "追击中") + "）</div>";
      }
      return '<div class="sub">🌾 ' + fmtNum(c.grain) + " 粮 · 护送 " +
        fmtNum(c.escort_troops || 0) + " 兵 · " + esc(c.from || "?") + " → " +
        esc(c.to || "?") + "（现于 " + esc(c.cur_node || "?") + " · 进度 " +
        esc(c.progress || "?") + "）</div>";
    }).join("");
  } else h += '<div class="sub">（无在途粮队；粮队沿本方/盟友通路自动行进，可能被匪寨/敌军劫掠）</div>';
  const srcs = myNodes();
  const dests = friendlyNodes();
  if (srcs.length && dests.length > 1) {
    const escorts = (p.armies || []).filter((a) => a.state === "idle");
    h += '<div class="form-row"><select id="cvFrom" style="max-width:110px">' +
      srcs.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
      '</select>→<select id="cvTo" style="max-width:130px">' +
      dests.map((n) => '<option value="' + n.id + '">' + esc(n.name) +
        (n.owner && n.owner.id !== myFactionId() ? "（盟友）" : "") + "</option>").join("") +
      '</select><input id="cvGrain" type="number" min="1" placeholder="粮草" style="max-width:80px">' +
      '<select id="cvEscort" style="max-width:150px"><option value="0">无护送</option>' +
      escorts.map((a) => '<option value="' + a.id + '">' + esc(a.name) +
        "（" + esc(a.node_name || "?") + "）</option>").join("") +
      '</select><button class="btn small primary" data-act="send-convoy">发粮队</button></div>';
  } else {
    h += '<div class="sub">（需至少 2 个己方/盟友据点才能派出运粮队）</div>';
  }
  h += "</div>";
  // --- 匪寨 ---
  const bandits = S.bandits || [];
  if (bandits.length) {
    h += '<div class="card"><h3>👹 匪寨 / 妖巢</h3>';
    const raiders = (p.armies || []).filter((a) => a.state === "idle");
    for (const b of bandits) {
      h += '<div class="form-row">' + (b.type === "demon" ? "🐲" : "👹") + " " + esc(b.name) +
        "（" + esc((nodeById(b.node_id) || {}).name || "") + " · 兵力约 " + fmtNum(b.troops) + "）" +
        (raiders.length
          ? '<select id="raid_' + b.node_id + '" style="max-width:120px">' +
            raiders.map((a) => '<option value="' + a.id + '">' + esc(a.name) + "</option>").join("") +
            '</select><button class="btn small danger" data-act="raid" data-node="' + b.node_id + '">讨伐</button>'
          : '<span class="sub">（无待命军团）</span>') + "</div>";
    }
    h += "</div>";
  }
  // --- 流将 ---
  // 已统一为上方「流将暗标」卡片，旧的一口价招募入口不再提供
  return h;
}

// ---------------------------------------------------------------------------
// 成就 / 赛季（P3 §6.5/§17）
// ---------------------------------------------------------------------------
function tabHonor() {
  const p = S.player || {};
  const season = S.season || {};
  let h = "";
  // --- 赛季 ---
  const dayLeft = Math.max(0, (season.season_days || 75) - (season.day || 0));
  h += '<div class="card"><h3>📅 第 ' + (season.season_no || 1) + " 赛季</h3>" +
    kv("进度", "第 " + (season.day || 0) + " / " + (season.season_days || 75) + " 天（剩 " + dayLeft + " 天）") +
    kv("阶段", ({ early: "初期", mid: "中期", late: "末期", final: "结算" }[season.phase] || season.phase || "-")) +
    ((season.coalition && season.coalition.active)
      ? kv("合纵", "⚠️ 玩家霸权过盛，NPC 联合反制中") : "") +
    ((season.history || []).length
      ? '<details><summary class="sub">历史赛季</summary>' + season.history.map((s) =>
        '<div class="sub">S' + s.season_no + "：" + (s.scores || []).map((x) => esc(x.name || "?") + " " + fmtNum(x.score || 0)).join(" · ") + "</div>").join("") +
        "</details>" : "") + "</div>";
  // --- 荣誉商店（赛季保留）---
  const honor = p.honor || {};
  const honorShop = S.meta.honor_shop || {};
  const honorBalance = Number(honor.balance !== undefined
    ? honor.balance
    : Math.max(0, Number(honor.total || 0) - Object.values(honor.spent || {}).reduce(
      (a, b) => a + (b && typeof b === "object" ? Number(b.amount || 0) : Number(b || 0)), 0)));
  const seasonSpent = honor.season_spent || {};
  const HONOR_UI = {
    gold_pack: ["💰 主玩法灵石礼包", (v) => "灵石 +" + fmtNum(v.gold || 100000)],
    title_pack: ["🏅 赛季称号", (v) => "称号「" + (v.title || "赛季霸主") + "」"],
    boost_pack: ["⚡ 开局加成", (v) => "战略产出 ×" + (v.output_mult || 1.2) + "，持续 " + (v.days || 7) + " 天"],
    material_pack: ["📦 战略物资礼包", (v) => "铜钱 +" + fmtNum(v.coin || 0) + "、粮草 +" + fmtNum(v.grain || 0) + "、精铁 +" + fmtNum(v.iron || 0)],
  };
  const hk = Object.keys(honorShop);
  if (hk.length) {
    h += '<div class="card"><h3>🏅 荣誉商店（可支配 ' + fmtNum(honorBalance) + '）</h3>' +
      '<div class="sub">荣誉点由赛季结算按战功发放，跨赛季结转；每件商品有独立赛季限购。</div>';
    for (const k of hk) {
      const v = honorShop[k] || {};
      const ui = HONOR_UI[k] || [k, () => ""];
      const cost = Number(v.honor || 0);
      const limit = Number(v.season_limit || 0);
      const used = Number(seasonSpent[k] || 0);
      const reached = limit > 0 && used >= limit;
      const poor = honorBalance < cost;
      h += '<div class="card thin"><h3>' + esc(ui[0]) + ' <span class="tag">' + fmtNum(cost) + ' 荣誉</span></h3>' +
        '<div class="sub">' + esc(ui[1](v)) + (limit > 0 ? " · 本赛季 " + used + "/" + limit : " · 不限购") + "</div>" +
        '<div class="drawer-actions"><button class="btn small primary" data-act="buy-honor" data-item="' + esc(k) + '"' +
        (reached || poor ? " disabled" : "") + ">" + (reached ? "本赛季已兑完" : poor ? "荣誉点不足" : "兑换") + "</button></div></div>";
    }
    const boost = honor.boost || {};
    if (Number(boost.until || 0) > Math.floor(Date.now() / 1000)) {
      h += '<div class="sub">⚡ 当前开局加成：产出 ×' + (boost.mult || 1.2) + "，剩余 " +
        fmtRemain(Number(boost.until) - Math.floor(Date.now() / 1000)) + "</div>";
    }
    h += "</div>";
  }
  // --- 新手福利 ---
  h += '<div class="card"><h3>🎁 入场福利</h3>';
  h += '<div class="form-row"><button class="btn small primary" data-act="claim-catchup"' +
    ((p.newcomer || {}).catchup_claimed ? " disabled" : "") + ">📦 追赶礼包（中期入场一次性资源包）</button>" +
    ((p.newcomer || {}).catchup_claimed ? '<span class="sub">已领取</span>' : "") + "</div>";
  const idleArmies = (p.armies || []).filter((a) => a.state === "idle");
  if (idleArmies.length) {
    h += '<div class="form-row"><select id="carryArmy" style="max-width:130px">' +
      idleArmies.map((a) => '<option value="' + a.id + '">' + esc(a.name) + "</option>").join("") +
      '</select><button class="btn small" data-act="claim-carryover">🎖️ 御林老兵结转（上赛季 veteran 50%）</button></div>';
  }
  h += "</div>";
  // --- 成就册 ---
  const got = p.achievements || {};
  const defs = S.meta.achievements || {};
  const keys = Object.keys(defs);
  const doneN = keys.filter((k) => got[k]).length;
  h += '<div class="card"><h3>🏆 成就册（' + doneN + " / " + keys.length + "）</h3>";
  if (keys.length) {
    h += '<table class="tbl"><tr><th>成就</th><th>说明</th><th>战功</th><th></th></tr>';
    for (const k of keys) {
      const a = defs[k];
      h += "<tr><td>" + esc(a.name) + "</td><td class='sub'>" + esc(a.desc) + "</td><td>+" + a.merit +
        "</td><td>" + (got[k] ? "✅" : "…") + "</td></tr>";
    }
    h += "</table>";
  } else h += '<div class="sub">（暂无成就配置）</div>';
  h += "</div>";
  // --- 擂台 ---
  h += '<div class="card"><h3>🎯 演武擂台</h3><div class="sub">摆擂押铜钱，全服应战；胜者通吃（1 小时有效，平局各自收回）</div>';
  const duels = p.duels || [];
  if (duels.length) {
    for (const d of duels) {
      h += '<div class="form-row">' + (d.mine ? "🔵 我的擂台：" : "🔴 ") + esc(d.challenger || "?") + " 的 " +
        esc(d.char_name || "?") + "（赌注 " + fmtNum(d.stake) + " 铜钱）" +
        (d.mine ? "" : '<button class="btn small primary" data-act="duel-accept" data-id="' + esc(d.duel_id) + '">应战</button>') + "</div>";
    }
  } else h += '<div class="sub">（当前无人摆擂；在府邸页武将卡片可摆擂）</div>';
  h += "</div>";
  // --- 仙盟会武（按战功分档报名）---
  const merit = Number(p.merit || 0);
  const myBracket = merit < 500 ? "novice" : (merit < 2000 ? "veteran" : "master");
  const BRACKET_NAMES = { novice: "新秀", veteran: "宿将", master: "宗师" };
  const arenaFee = Number(((S.meta || {}).duel_cfg || {}).arena_stake_min || 100);
  h += '<div class="card"><h3>🥁 仙盟会武</h3>' +
    '<div class="sub">按战功分档：新秀（&lt;500）/ 宿将（500~1999）/ 宗师（≥2000）；不可跨档</div>' +
    kv("我的档位", (BRACKET_NAMES[myBracket] || myBracket) + "（战功 " + fmtNum(merit) + "）") +
    '<div class="form-row"><select id="arenaBracket" style="max-width:120px">' +
    Object.entries(BRACKET_NAMES).map(([k, n]) =>
      '<option value="' + k + '"' + (k === myBracket ? " selected" : "") + ">" + n + "</option>").join("") +
    '</select><button class="btn small primary" data-act="arena-register">报名会武</button>' +
    '<span class="sub">报名费 ' + fmtNum(arenaFee) + " 铜钱</span></div></div>";
  // --- 名将堂 / 英烈祠（§11 赛季陈列，最多各 20 条）---
  const mem = memorialData();
  h += memorialCard("🏛️ 名将堂", mem.hall, true);
  h += memorialCard("🕯️ 英烈祠", mem.shrine, false);
  return h;
}

const RARITY_TAG_CLASS = {
  common: "", fine: "fine", rare: "rare", epic: "epic", legend: "legend",
  凡品: "", 良品: "fine", 稀有: "rare", 极品: "epic", 传世: "legend",
};

// 名将堂数据：优先随 get_state 下发，缺失时按需拉 /api/strategy/memorial（只读）
let memorialCache = null; // null=未加载；false=加载失败；object=已加载
function memorialData() {
  const src = S.memorials;
  if (src && (Array.isArray(src.hall) || Array.isArray(src.shrine))) return src;
  if (memorialCache === null) {
    memorialCache = false;
    api("/api/strategy/memorial").then((r) => {
      memorialCache = {
        hall: (r && r.hall) || [],
        shrine: (r && r.shrine) || [],
        season_no: (r && r.season_no) || (S.season || {}).season_no,
      };
      if (activeTab === "honor") renderTab();
    }).catch(() => { /* 加载失败保持空态 */ });
  }
  return (memorialCache && typeof memorialCache === "object") ? memorialCache : {};
}

function memorialRow(m) {
  m = m || {};
  const cls = RARITY_TAG_CLASS[m.rarity] || "";
  const rname = ((S.meta || {}).rarity_names || {})[m.rarity] || m.rarity || "";
  const achs = (m.achievements || []).map((x) =>
    typeof x === "string" ? x : ((x && (x.name || x.title)) || "")).filter(Boolean);
  return '<div class="memorial-row">' +
    '<div class="mr-head"><span class="mr-name">' + esc(m.name || "无名") + "</span>" +
    (rname ? '<span class="tag ' + cls + '">' + esc(rname) + "</span>" : "") +
    '<span class="mr-meta">' + esc(m.faction || "在野") + " · S" + esc(m.season || "?") +
    " · 战功 " + fmtNum(m.merit || 0) + "</span></div>" +
    (achs.length ? '<div class="mr-ach">' + achs.map(esc).join(" · ") + "</div>" : "") +
    "</div>";
}

function memorialCard(title, list, open) {
  const arr = (list || []).slice(0, 20);
  let h = '<details class="card memorial-card"' + (open ? " open" : "") + ">" +
    "<summary>" + title + ' <span class="sub">（' + arr.length + "）</span></summary>";
  h += arr.length ? arr.map(memorialRow).join("")
    : '<div class="sub memorial-empty">本赛季尚无记录</div>';
  return h + "</details>";
}

// ---------------------------------------------------------------------------
// 设置（通知分级 §12.5）
// ---------------------------------------------------------------------------
function tabSettings() {
  const p = S.player || {};
  const pref = p.notify_pref || {};
  const level = Number(pref.level !== undefined ? pref.level : 1);
  const hidden = !!((p.tutorial || {}).guide_hidden);
  let h = '<div class="card"><h3>📖 新手指引</h3><div class="sub">当前状态：' +
    (hidden ? "已收起" : "已开启") +
    '。收起后总览页只保留一个小按钮，不会打扰日常操作。</div>' +
    '<div class="drawer-actions"><button class="btn small ' + (hidden ? "primary" : "") +
    '" data-act="' + (hidden ? "guide-show" : "guide-hide") + '">' +
    (hidden ? "开启新手指引" : "收起新手指引") + "</button></div></div>";
  const opts = [
    [0, "仅手操窗口", "手操战役/单挑邀请（强烈建议保留）"],
    [1, "手操 + 己方被攻", "围城警报等（默认）"],
    [2, "全部事件", "含世界事件播报"],
  ];
  h += '<div class="card"><h3>🔔 通知档位</h3><div class="sub">控制服务端 WS 推送与聊天室横幅的通知密度</div><div class="form-col">';
  for (const [v, name, desc] of opts) {
    h += '<label class="radio-item"><input type="radio" name="notifyLevel" value="' + v + '"' +
      (level === v ? " checked" : "") + "> " + name + ' <span class="sub">' + desc + "</span></label>";
  }
  h += '</div><div class="drawer-actions"><button class="btn small primary" data-act="save-notify">保存</button></div></div>';
  const perm = typeof Notification !== "undefined" ? Notification.permission : "unsupported";
  h += '<div class="card"><h3>🌐 浏览器通知</h3><div class="sub">当前状态：' +
    ({ granted: "✅ 已授权", denied: "❌ 已拒绝（需在浏览器设置中恢复）", default: "未授权", unsupported: "浏览器不支持" }[perm] || perm) +
    "</div>" + (perm === "default"
      ? '<div class="drawer-actions"><button class="btn small" data-act="notify-perm">请求授权</button></div>' : "") +
    "</div>";
  return h;
}

function bindTabEvents() {
  document.querySelectorAll("[data-act]").forEach((btn) => {
    // 幂等绑定：切换 tab 时 renderTab 会对仍存在的抽屉按钮重复绑定，
    // 导致一次点击触发多次动作。已绑过的节点跳过。
    if (btn.dataset.boundAct === "1") return;
    btn.dataset.boundAct = "1";
    btn.addEventListener("click", () => {
      const act = btn.dataset.act;
      const id = Number(btn.dataset.id);
      if (act === "recruit") doAction("recruit", { index: Number(btn.dataset.i) });
      else if (act === "recruit-premium") doAction("recruit", { index: Number(btn.dataset.i), premium: true });
      else if (act === "dismiss-char") {
        const name = btn.dataset.name || "该角色";
        if (confirm("确认遣散 " + name + "？该操作不可撤销，将腾出 1 个名册名额。")) {
          doAction("dismiss_char", { char_id: Number(btn.dataset.id) });
        }
      }
      else if (act === "buy-gold") doAction("buy_merit_shop", { item: "gold_pack" });
      else if (act === "buy-iron") doAction("buy_merit_shop", { item: "iron_pack" });
      else if (act === "buy-material") doAction("buy_merit_shop", { item: "material_pack" });
      else if (act === "buy-equip") {
        if (confirm("确认消耗 " + Number(((S.meta || {}).merit_shop || {}).equip_instance?.merit || 3000) +
          " 战功兑换一件随机装备？")) doAction("buy_merit_shop", { item: "equip_instance" });
      } else if (act === "buy-recommend") {
        const sel = document.getElementById("recommendRole");
        doAction("buy_merit_shop", { item: "recommend_letter", role: sel ? sel.value : "" });
      } else if (act === "buy-honor") {
        doAction("buy_honor_shop", { item: btn.dataset.item || "" });
      } else if (act === "buy-elixir") {
        const sel = document.getElementById("elixirChar");
        if (sel) doAction("buy_merit_shop", { item: "elixir", char_id: Number(sel.value) });
      } else if (act === "march-pick") {
        marchArmyId = Number(btn.dataset.id);
        canvas.style.cursor = "pointer";
        toast("🗺️ 已选中军团，点击地图上的目标据点出征");
      }
      // --- P2 战役 ---
      else if (act === "enter-battle") {
        if (window.BattleView) BattleView.open(id);
        else toast("战役模块未加载", "err");
      } else if (act === "spectate-battle") {
        // §12.6 只读观战：隐藏部署/操作栏，只轮询不提交
        if (window.BattleView) BattleView.open(id, { spectator: true });
        else toast("战役模块未加载", "err");
      } else if (act === "duel-open") {
        const dc = (S.meta || {}).duel_cfg || {};
        const lo = Number(dc.arena_stake_min || 100);
        const hi = Number(dc.arena_stake_max || 100000);
        const stake = prompt("摆擂赌注（铜钱，" + lo + "~" + hi + "）：", String(lo));
        if (stake) doAction("duel_arena_open", { char_id: id, stake: Number(stake) });
      } else if (act === "duel-accept") {
        const gens = ((S.player || {}).chars || []).filter((c) => c.role === "general" && c.status === "idle");
        if (!gens.length) { toast("没有空闲武将可应战", "err"); return; }
        const pick = prompt("派出哪位武将应战？输入序号：\n" +
          gens.map((c, i) => (i + 1) + ". " + c.name + "（武勇 " + ((c.stats || {}).valor || 0) + "）").join("\n"), "1");
        const gi = Number(pick) - 1;
        if (pick && gens[gi]) doAction("duel_arena_accept", { duel_id: btn.dataset.id, char_id: gens[gi].id });
      }
      // --- P1 势力治理 ---
      else if (act === "approve") doAction("faction_approve", { applicant_user: btn.dataset.u });
      else if (act === "reject") doAction("faction_reject", { applicant_user: btn.dataset.u });
      else if (act === "recall-task") {
        const name = btn.dataset.name || "该角色";
        if (confirm("召回 " + name + "？已进行部分按比例结算，且收益额外损失 50%。")) {
          doAction("recall_task", { task_id: Number(btn.dataset.id) });
        }
      } else if (act === "set-position") {
        const sel = document.getElementById("pos_" + btn.dataset.u.replace(/[^a-zA-Z0-9_-]/g, "_"));
        doAction("faction_set_position", { target_user: btn.dataset.u, position: sel ? sel.value : "" });
      } else if (act === "appoint-char") {
        const charSel = document.getElementById("appointChar");
        const posSel = document.getElementById("appointPos");
        const nodeSel = document.getElementById("appointNode");
        if (!charSel) return;
        doAction("faction_appoint_char", {
          char_id: Number(charSel.value),
          position: posSel ? posSel.value : "",
          node_id: nodeSel ? Number(nodeSel.value) : 0,
        });
      } else if (act === "transfer-leader") {
        const sel = document.getElementById("transferSel");
        if (sel && confirm("禅让后你将失去势力主权限，确认？")) {
          doAction("transfer_leader", { target_user: sel.value });
        }
      } else if (act === "deposit") {
        const amt = Number((document.getElementById("depositAmt") || {}).value || 0);
        const res = (document.getElementById("depositRes") || {}).value || "coin";
        if (amt > 0) doAction("treasury_deposit", { resource: res, amount: amt });
        else toast("请输入数量", "err");
      } else if (act === "withdraw") {
        const amt = Number((document.getElementById("withdrawAmt") || {}).value || 0);
        const res = (document.getElementById("withdrawRes") || {}).value || "coin";
        if (amt > 0) doAction("treasury_withdraw", { resource: res, amount: amt });
        else toast("请输入支取数量", "err");
      } else if (act === "task-refresh") {
        doAction("faction_task_refresh", {});
      } else if (act === "task-claim") {
        doAction("faction_task_claim", { task_id: id });
      } else if (act === "mob-issue") {
        const kind = (document.getElementById("mobKind") || {}).value || "";
        if (!kind) { toast("请选择动员令类型", "err"); return; }
        if (confirm("发布动员令将消耗 60 战功，确认？")) {
          doAction("mobilization_issue", { kind });
        }
      } else if (act === "dip-respond") {
        doAction("diplomacy_respond", { from_fid: id, accept: btn.dataset.accept === "1" });
      } else if (act === "dip-propose") {
        doAction("diplomacy_propose", {
          target_fid: Number((document.getElementById("dipTarget") || {}).value || 0),
          kind: (document.getElementById("dipKind") || {}).value || "ally",
          indemnity: Number((document.getElementById("dipIndemnity") || {}).value || 0),
        });
      } else if (act === "dip-gift") {
        const gt = Number((document.getElementById("dipGiftTarget") || {}).value || 0);
        const gres = (document.getElementById("dipGiftRes") || {}).value || "coin";
        const gamt = Number((document.getElementById("dipGiftAmt") || {}).value || 0);
        if (!gt || gamt <= 0) { toast("请选择目标并填写赠礼数量", "err"); return; }
        doAction("diplomacy_gift", { target_fid: gt, resource: gres, amount: gamt });
      } else if (act === "tech") doAction("tech_research", { line: btn.dataset.line });
      else if (act === "dispatch-plot") {
        const ptype = (document.getElementById("plotType") || {}).value || "";
        if (PLOT_TARGET[ptype] === "char") { toast("该计略目标为角色，请用下方专用按钮", "err"); return; }
        doAction("dispatch_plot", {
          char_id: Number((document.getElementById("plotChar") || {}).value || 0),
          plot_type: ptype,
          target_id: Number((document.getElementById("plotTarget") || {}).value || 0),
        });
      } else if (act === "alienate") {
        const cid = prompt("离间：输入敌方角色 ID（可在战报/府邸中查看）：");
        if (cid) doAction("dispatch_plot", {
          char_id: Number((document.getElementById("plotChar") || {}).value || 0),
          plot_type: "alienate", target_id: Number(cid),
        });
      } else if (act === "rescue-plot") {
        const st = ((S.player || {}).chars || []).filter((c) => c.role === "strategist" && c.status === "idle");
        if (!st.length) { toast("没有空闲军师执行营救", "err"); return; }
        doAction("dispatch_plot", { char_id: st[0].id, plot_type: "rescue", target_id: id });
      }
      // --- 俘虏 ---
      else if (act === "persuade") {
        const gens = ((S.player || {}).chars || []).filter((c) => c.role !== "official" && c.status === "idle");
        const pid = gens.length ? gens[0].id : 0;
        doAction("persuade", { char_id: id, persuader_char_id: pid });
      } else if (act === "ransom-set") {
        const amt = prompt("设置赎金（铜钱，对方支付后放人）：", "5000");
        if (amt) doAction("ransom_set", { char_id: id, amount: Number(amt) });
      } else if (act === "ransom-pay") doAction("ransom_pay", { char_id: id });
      else if (act === "release") {
        if (confirm("无条件释放该俘虏？")) doAction("release", { char_id: id });
      } else if (act === "transfer-captive") {
        const sel = document.getElementById("xfer_" + id);
        const toNode = Number(sel ? sel.value : 0);
        if (!toNode) { toast("请选择押运目标据点", "err"); return; }
        if (confirm("押运途中可被截获，确认押送？")) {
          doAction("transfer_captive", { char_id: id, to_node_id: toNode });
        }
      }
      // --- 主城/退盟/立派 ---
      else if (act === "buy-shield") {
        doAction("buy_shield", { node_id: Number((document.getElementById("shieldNode") || {}).value || 0) });
      } else if (act === "capital-relocate") {
        const nid = Number((document.getElementById("relocateNode") || {}).value || 0);
        if (!nid) { toast("请选择新主城", "err"); return; }
        if (confirm("迁都将扣国库铜钱并进入冷却，确认？")) {
          doAction("capital_relocate", { node_id: nid });
        }
      } else if (act === "rebuild-capital") {
        const nid = Number((document.getElementById("rebuildNode") || {}).value || 0);
        if (confirm("重建主城将消耗灵石（主玩法钱包扣费），确认选定该无主节点？")) {
          doAction("rebuild_capital", { node_id: nid });
        }
      } else if (act === "rebuild-donate") {
        const amt = Number((document.getElementById("rebuildDonateAmt") || {}).value || 0);
        if (amt > 0) doAction("rebuild_donate", { amount: amt });
        else toast("请输入捐献灵石数量", "err");
      } else if (act === "arena-register") {
        const bracket = (document.getElementById("arenaBracket") || {}).value || "";
        if (!bracket) { toast("请选择会武档位", "err"); return; }
        doAction("arena_register", { bracket });
      } else if (act === "faction-quit") {
        const fac = (S.player || {}).faction || {};
        const msg = fac.alliance_id
          ? (fac.is_alliance_host
            ? "解散联盟后各成员势力保留现有据点，确认？"
            : "退出联盟后你的势力与全部据点保留，确认？")
          : "退出势力将有冷却期，确认？";
        if (confirm(msg)) doAction("faction_quit", {});
      } else if (act === "transfer-node") {
        const nid = Number((document.getElementById("transferNode") || {}).value || 0);
        const tfid = Number((document.getElementById("transferTarget") || {}).value || 0);
        if (!nid || !tfid) { toast("请选择据点与目标势力", "err"); return; }
        if (confirm("确认在本联盟内交接该据点归属？")) {
          doAction("transfer_node", { node_id: nid, target_faction_id: tfid });
        }
      } else if (act === "invite-respond") {
        doAction("faction_invite_respond", { accept: btn.dataset.accept === "1" });
      } else if (act === "cosign") doAction("faction_cosign", { petition_id: btn.dataset.id });
      else if (act === "faction-create") {
        const name = ((document.getElementById("newFactionName") || {}).value || "").trim();
        const color = (document.getElementById("newFactionColor") || {}).value || "";
        if (!name) { toast("请输入势力名", "err"); return; }
        doAction("faction_create", { name, color });
      } else if (act === "faction-apply") doAction("faction_apply", { faction_id: id });
      else if (act === "faction-invite") {
        const u = ((document.getElementById("inviteUser") || {}).value || "").trim();
        if (!u) { toast("请输入对方玩家 ID", "err"); return; }
        doAction("faction_invite", { target_user: u });
      }
      // --- P3 坊市 ---
      else if (act === "market-buy") {
        const amt = prompt("买入数量（≤" + btn.dataset.max + "）：", btn.dataset.max);
        if (amt) doAction("market_buy", { order_id: id, amount: Number(amt) });
      } else if (act === "market-cancel") doAction("market_cancel", { order_id: id });
      else if (act === "market-list") {
        doAction("market_list", {
          node_id: Number((document.getElementById("mkNode") || {}).value || 0),
          item_type: (document.getElementById("mkItem") || {}).value || "grain",
          amount: Number((document.getElementById("mkAmount") || {}).value || 0),
          unit_price: Number((document.getElementById("mkPrice") || {}).value || 0),
        });
      } else if (act === "send-convoy") {
        const from = Number((document.getElementById("cvFrom") || {}).value || 0);
        const to = Number((document.getElementById("cvTo") || {}).value || 0);
        const grain = Number((document.getElementById("cvGrain") || {}).value || 0);
        if (!grain || grain <= 0) { toast("请输入运粮数量", "err"); return; }
        if (!from || !to || from === to) { toast("起点与终点需为不同据点", "err"); return; }
        doAction("send_convoy", {
          from_node: from, to_node: to, grain: grain,
          escort_army_id: Number((document.getElementById("cvEscort") || {}).value || 0),
        });
      } else if (act === "raid") {
        const sel = document.getElementById("raid_" + btn.dataset.node);
        doAction("raid_bandit", { army_id: Number(sel ? sel.value : 0), node_id: Number(btn.dataset.node) });
      } else if (act === "bid-wanderer") {
        const inp = document.getElementById("aid_" + id);
        const amount = Number((inp || {}).value || 0);
        if (!amount || amount <= 0) { toast("请输入暗标战功数", "err"); return; }
        doAction("bid_wanderer", { auction_id: id, amount: amount });
      } else if (act === "cancel-wanderer-bid") {
        if (!confirm("确认撤标并退还已托管战功？")) return;
        doAction("cancel_wanderer_bid", { auction_id: id });
      }
      // --- 赛季 / 设置 ---
      else if (act === "claim-catchup") doAction("claim_catchup", {});
      else if (act === "claim-carryover") {
        doAction("claim_carryover", { army_id: Number((document.getElementById("carryArmy") || {}).value || 0) });
      } else if (act === "save-notify") {
        const checked = document.querySelector('input[name="notifyLevel"]:checked');
        doAction("set_notify_pref", { level: Number(checked ? checked.value : 1) });
      } else if (act === "guide-hide") doAction("set_guide", { visible: false });
      else if (act === "guide-show") doAction("set_guide", { visible: true });
      else if (act === "notify-perm") requestNotifyPermission();
    });
  });
  const rf = document.getElementById("btnTavernRefresh");
  if (rf) rf.addEventListener("click", () => doAction("refresh_tavern", { manual: true }));
}

// ---------------------------------------------------------------------------
// 编军弹窗
// ---------------------------------------------------------------------------
let armyModalNode = 0;

function openArmyModal(nodeId) {
  const p = S.player || {};
  const idle = (p.chars || []).filter((c) => c.status === "idle" && c.injury_state !== "dying" && c.injury_state !== "dead");
  const leaders = idle.filter((c) => c.role !== "official");
  if (!leaders.length) { toast("没有可出征的武将/军师（内政官不能当主将）", "err"); return; }
  armyModalNode = nodeId;
  document.getElementById("armyLeader").innerHTML = leaders.map((c) =>
    '<option value="' + c.id + '">' + esc(c.name) + "（" + c.role_name + " 武勇" + ((c.stats || {}).valor || 0) + "）</option>").join("");
  document.getElementById("armyMembers").innerHTML = idle.map((c) =>
    '<option value="' + c.id + '">' + esc(c.name) + "（" + c.role_name + "）</option>").join("");
  document.getElementById("armyTroopType").innerHTML = Object.entries(S.meta.troop_types).map(([k, t]) =>
    '<option value="' + k + '">' + t.name + "（攻" + t.atk + "/防" + t.def + "/机动" + t.move + " · " + t.skill + "）</option>").join("");
  document.getElementById("armyOverlay").hidden = false;
  updateArmyCost();
}

function updateArmyCost() {
  const tt = S.meta.troop_types[document.getElementById("armyTroopType").value] || {};
  const troops = Number(document.getElementById("armyTroops").value) || 0;
  const engine = document.getElementById("armyEngine").checked;
  const coin = Math.floor(troops * (tt.recruit_coin || 0) / 10) + (engine ? 3000 : 0);
  const grain = Math.floor(troops * (tt.recruit_grain || 0) / 10);
  document.getElementById("armyCost").textContent =
    "预计消耗：铜钱 " + fmtNum(coin) + " · 粮草 " + fmtNum(grain) + "（另自动携粮 = 主将统率×50）";
}

["armyTroopType", "armyTroops", "armyEngine"].forEach((id) => {
  document.getElementById(id).addEventListener("change", updateArmyCost);
  document.getElementById(id).addEventListener("input", updateArmyCost);
});
document.getElementById("armyCancel").addEventListener("click", () => {
  document.getElementById("armyOverlay").hidden = true;
});
document.getElementById("armyConfirm").addEventListener("click", () => {
  const members = Array.from(document.getElementById("armyMembers").selectedOptions)
    .map((o) => Number(o.value))
    .filter((id) => id !== Number(document.getElementById("armyLeader").value));
  doAction("create_army", {
    leader_char_id: Number(document.getElementById("armyLeader").value),
    member_ids: members,
    node_id: armyModalNode,
    troop_type: document.getElementById("armyTroopType").value,
    troops: Number(document.getElementById("armyTroops").value) || 100,
    siege_engine: document.getElementById("armyEngine").checked,
  }).then(() => { document.getElementById("armyOverlay").hidden = true; });
});

// ---------------------------------------------------------------------------
// 入场 / 刷新 / 启动
// ---------------------------------------------------------------------------
function renderJoinCandidates() {
  const sel = document.getElementById("joinNode");
  const btn = document.getElementById("btnJoin");
  if (!sel) return;
  const list = (S && S.start_candidates) || [];
  if (!list.length) {
    sel.innerHTML = '<option value="">暂无可选据点（可能均已被占据）</option>';
    if (btn) btn.disabled = true;
    return;
  }
  const prev = sel.value;
  const typeNames = ((S.meta || {}).node_types) || {};
  sel.innerHTML = list.map((n) => {
    const tn = typeNames[n.type] || n.type;
    return '<option value="' + n.id + '">' + esc(n.name) + "（" + esc(tn) +
      " · Lv" + n.level + "）</option>";
  }).join("");
  if (prev && list.some((n) => String(n.id) === prev)) sel.value = prev;
  if (btn) btn.disabled = false;
}

document.getElementById("btnJoin").addEventListener("click", async () => {
  const nodeId = Number((document.getElementById("joinNode") || {}).value || 0);
  if (!nodeId) { toast("请先选择起始据点", "err"); return; }
  const btn = document.getElementById("btnJoin");
  if (btn) btn.disabled = true;
  try {
    await doAction("enter", { node_id: nodeId });
  } finally {
    // 失败（如灵石不足/据点被抢）后必须恢复按钮，否则最长等到下一次
    // 轮询重建 DOM 前都无法重试。
    if (btn) btn.disabled = false;
  }
});
document.getElementById("btnRefresh").addEventListener("click", load);
window.addEventListener("resize", resizeCanvas);

function renderAll() {
  renderTop();
  renderLegend();
  drawMap();
  renderDrawer();
  renderTab();
}

// ---------------------------------------------------------------------------
// 据点抽屉：赈济 / 建筑（事件委托，抽屉重绘后依然有效）
// ---------------------------------------------------------------------------
function initDrawerEvents() {
  const drawer = document.getElementById("nodeDrawer");
  if (!drawer) return;
  drawer.addEventListener("click", (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest("[data-act]") : null;
    if (!btn) return;
    const act = btn.dataset.act;
    const nodeId = Number(btn.dataset.id || btn.dataset.node || 0);
    if (!nodeId) return;
    if (act === "relief") {
      doAction("govern_relief", { node_id: nodeId, resource: btn.dataset.res || "coin" });
    } else if (act === "upgrade-building") {
      doAction("upgrade_building", { node_id: nodeId, building_id: btn.dataset.building || "" });
    } else if (act === "build-building") {
      const sel = document.getElementById("buildSel_" + nodeId);
      if (!sel) return;
      doAction("build", { node_id: nodeId, building_id: sel.value });
    }
  });
}

// 战报链接直达：?battle=ID[&spectate=1]
function openBattleFromUrl() {
  try {
    const m = (/[?&]battle=(\d+)/).exec(location.search || "");
    if (!m || !window.BattleView) return;
    const spectator = /[?&]spectate=1/.test(location.search || "");
    BattleView.open(Number(m[1]), { spectator: spectator });
  } catch (e) { /* 忽略非法参数 */ }
}

// 供 guide.js 的 URL 直达（?guide=xxx）调用
window.renderTab = renderTab;

(async function init() {
  initLegend();
  initDrawerEvents();
  resizeCanvas();
  await load();
  openBattleFromUrl();
  setInterval(tickCountdown, 1000);
  pollTimer = setInterval(load, POLL_MS);
  // 页面重新可见时立即刷新
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) load();
  });
  // 战役覆盖层关闭后回到大地图并刷新
  window.onBattleClosed = () => { seenManualBattles = null; load(); };
  // 通知授权引导条（§12.5 F）：未授权且未点过「以后再说」时展示
  const bar = document.getElementById("notifyBar");
  if (bar && typeof Notification !== "undefined" && Notification.permission === "default" &&
      !localStorage.getItem(NOTIFY_DISMISS_KEY)) {
    bar.hidden = false;
  }
  const be = document.getElementById("btnNotifyEnable");
  if (be) be.addEventListener("click", requestNotifyPermission);
  const bl = document.getElementById("btnNotifyLater");
  if (bl) bl.addEventListener("click", () => {
    localStorage.setItem(NOTIFY_DISMISS_KEY, "1");
    bar.hidden = true;
  });
})();
