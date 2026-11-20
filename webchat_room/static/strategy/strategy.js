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
const STATE_NAME = { idle: "驻守", march: "行军", siege: "围城", retreat: "撤退" };
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
    const msg = (data && data.error) || ("HTTP " + resp.status);
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

async function load() {
  try {
    const r = await api("/api/strategy/state");
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
    const color = (n.owner && n.owner.color) || "#9e9e9e";
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
    '<div class="lg-item"><span class="lg-dot" style="background:' + f.color + '"></span>' +
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

// ---------------------------------------------------------------------------
// 据点抽屉
// ---------------------------------------------------------------------------
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
  html += kv("归属", '<span style="color:' + (n.owner ? n.owner.color : "#999") + '">' + esc(n.owner ? n.owner.name : "无主") + "</span>");
  html += kv("民心", n.morale + " / 100");
  if (n.defense_max > 0) {
    const pct = Math.round(n.defense / n.defense_max * 100);
    html += kv("城防", n.defense + " / " + n.defense_max + ' <span class="sub">(' + pct + '%)</span>');
  }
  const gar = n.garrison !== null && n.garrison !== undefined ? n.garrison + " 兵" : (n.garrison_bucket ? "约 " + n.garrison_bucket : "无");
  html += kv("守军", gar);
  if (n.buildings && n.buildings.length) html += kv("建筑", n.buildings.map((b) => (S.meta.buildings[b] || {}).name || b).join("、"));
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
    html += buildSelectHtml(n);
    html += taskFormHtml(n, p);
  } else {
    const idleArmies = (p.armies || []).filter((a) => a.state === "idle");
    for (const a of idleArmies) {
      html += '<button class="btn small primary" onclick="doAction(\'march\',{army_id:' + a.id + ",node_id:" + n.id + '})">⚔️ ' + esc(a.name) + " 出征至此</button>";
    }
    if (!idleArmies.length) html += '<span class="sub">（无可出征军团，先在己方据点编军）</span>';
  }
  html += "</div>";
  body.innerHTML = html;
}

function kv(k, v) { return '<div class="kv"><span class="k">' + k + "</span><span>" + v + "</span></div>"; }

function buildSelectHtml(n) {
  if (!["city", "pass", "branch"].includes(n.type)) return "";
  const opts = Object.entries(S.meta.buildings).map(([id, b]) =>
    '<option value="' + id + '">' + b.name + "（" + b.coin + "币" + (b.iron ? "/" + b.iron + "铁" : "") + "）</option>").join("");
  return '<select id="buildSel_' + n.id + '" style="max-width:150px">' + opts + "</select>" +
    '<button class="btn small" onclick="doAction(\'build\',{node_id:' + n.id + ",building_id:document.getElementById('buildSel_" + n.id + "').value})" + '">🏗️ 建设</button>';
}

function taskFormHtml(n, p) {
  const idle = (p.chars || []).filter((c) => c.status === "idle" && c.injury_state !== "dying" && c.injury_state !== "dead");
  if (!idle.length) return '<span class="sub">（无空闲角色可派遣任务）</span>';
  const charOpts = idle.map((c) => '<option value="' + c.id + '">' + esc(c.name) + "（" + c.role_name + "）</option>").join("");
  const meta = S.meta.task_meta || {};
  const taskOpts = Object.entries(S.meta.task_types).map(([t, name]) => {
    const m = meta[t] || {};
    const label = m.name ? m.name + "（" + (m.stat || "?") + "）" : name;
    const tip = [m.role ? "最佳职责：" + m.role : "", m.output ? "产出：" + m.output : "",
      m.node ? "据点加成：" + m.node : ""].filter(Boolean).join(" · ");
    return '<option value="' + t + '" title="' + esc(tip) + '">' + esc(label) + "</option>";
  }).join("");
  const hourOpts = (S.meta.task_hours || [1, 2, 4, 8]).map((h) => '<option value="' + h + '">' + h + "h</option>").join("");
  const uid = "task_" + n.id;
  return '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:6px;width:100%">' +
    '<select id="' + uid + '_c" style="max-width:110px">' + charOpts + "</select>" +
    '<select id="' + uid + '_t" style="max-width:120px">' + taskOpts + "</select>" +
    '<select id="' + uid + '_h" style="max-width:60px">' + hourOpts + "</select>" +
    '<button class="btn small" onclick="dispatchFrom(\'' + uid + "'," + n.id + ')">📜 派遣</button></div>' +
    '<div class="sub" style="width:100%">括号内为任务核心属性；悬停可看最佳职责、产出与据点加成</div>';
}

function dispatchFrom(uid, nodeId) {
  doAction("dispatch_task", {
    char_id: Number(document.getElementById(uid + "_c").value),
    node_id: nodeId,
    task_type: document.getElementById(uid + "_t").value,
    hours: Number(document.getElementById(uid + "_h").value),
  });
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
  if (!S || !S.joined) { el.innerHTML = '<div class="empty">入场后展示</div>'; return; }
  const fn = {
    overview: tabOverview, chars: tabChars, armies: tabArmies,
    faction: tabFaction, tavern: tabTavern, market: tabMarket,
    battles: tabBattles, shop: tabShop, honor: tabHonor, settings: tabSettings,
  }[activeTab];
  el.innerHTML = fn ? fn() : "";
  bindTabEvents();
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
    { title: "⑥ 势力与官职", done: (f.members || []).length > 1,
      desc: "势力页可任免角色官职：据点越多，可任免的官职档位越高；主将带兵上限按其角色官职计算。" },
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
    h += "<tr><td><span class='lg-dot' style='display:inline-block;background:" + f.color + "'></span> " + esc(f.name) +
      (f.id === myFactionId() ? "（我）" : "") + "</td><td>" +
      ({ npc: "NPC", player: "玩家", demon: "魔潮", none: "无主" }[f.kind] || f.kind) +
      "</td><td>" + f.node_count + "</td><td>" + f.reputation + "</td></tr>";
  }
  h += "</table></div>";
  const tasks = (S.player || {}).tasks || [];
  h += '<div class="card"><h3>📜 进行中任务</h3>';
  if (tasks.length) {
    h += '<table class="tbl"><tr><th>角色</th><th>任务</th><th>据点</th><th>剩余</th><th></th></tr>' +
      tasks.map((t) => "<tr><td>" + esc(t.char_name) + "</td><td>" + esc(t.task_name) +
        "</td><td>" + esc(t.node_name) + "</td><td>" + fmtRemain(t.remain_sec) +
        '</td><td><button class="btn small" data-act="recall-task" data-id="' + t.id +
        '" data-name="' + esc(t.char_name) + '">召回</button></td></tr>').join("") +
      "</table><div class='sub'>随时可召回；已进行部分按比例结算，但收益额外损失 50%</div>";
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
    '<div class="sub">阅历 ' + c.exp + "/" + c.exp_next + " · 忠诚 " + c.loyalty +
    " · 状态 " + ({ idle: "空闲", task: "执行任务", army: "随军", captive: "被俘", wander: "在野", dead: "陨落" }[c.status] || c.status) +
    (c.location ? " · 位于 " + esc(c.location) : "") + "</div>" +
    (c.appointed_position
      ? '<div class="sub">🏛 官职：' + esc(c.appointed_name || positionName(c.appointed_position)) +
        (c.appointed_node_name ? "（辖 " + esc(c.appointed_node_name) + "）" : "") + "</div>"
      : "") +
    '<div class="sub">伤势 ' + c.injury_name + "(" + c.injury + ") · 体力 " + c.stamina + "</div>" +
    '<div class="bar ' + (c.injury >= 60 ? "bad" : c.injury >= 30 ? "warn" : "") + '"><i style="width:' + (100 - c.injury) + '%"></i></div>' +
    (c.status === "idle" && c.role !== "official"
      ? '<div class="drawer-actions"><button class="btn small" data-act="duel-open" data-id="' + c.id + '">🎯 摆擂（单挑赌注）</button></div>'
      : "") +
    "</div>";
}

function tabChars() {
  const chars = ((S.player || {}).chars || []).filter((c) => c.status !== "dead");
  const dead = ((S.player || {}).chars || []).filter((c) => c.status === "dead");
  if (!chars.length && !dead.length) return '<div class="empty">尚无角色，去酒馆招募吧</div>';
  let h = "";
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
      '<div class="sub">兵力 ' + fmtNum(a.troops) + " · 士气 " + a.morale + " · 精锐 " + a.veteran +
      " · 携粮 " + fmtNum(a.grain) + "</div>" +
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
    b += '<button class="btn small" onclick="doAction(\'drill_army\',{army_id:' + a.id + '})">🎯 操练(500币)</button>';
    b += '<button class="btn small danger" onclick="doAction(\'disband_army\',{army_id:' + a.id + '})">解散</button>';
  } else if (a.state === "siege") {
    b += '<button class="btn small" onclick="doAction(\'siege_choice\',{army_id:' + a.id + ",choice:'street_fight'})" + '">巷战</button>';
    b += '<button class="btn small" onclick="doAction(\'siege_choice\',{army_id:' + a.id + ",choice:'surrender'})" + '">受降(需3倍兵力)</button>';
    b += '<button class="btn small danger" onclick="doAction(\'retreat\',{army_id:' + a.id + '})">撤围</button>';
  } else {
    b += '<button class="btn small danger" onclick="doAction(\'retreat\',{army_id:' + a.id + '})">🏳️ 撤退</button>';
  }
  return b;
}

function refillPrompt(armyId) {
  const n = prompt("补员数量（100 的倍数，需己方据点）：", "100");
  if (n) doAction("refill_army", { army_id: armyId, troops: Number(n) });
}

function tabTavern() {
  const p = S.player || {};
  const t = p.tavern || {};
  let h = '<div class="card"><h3>🍶 招贤酒馆</h3><div class="sub">普通招募 800 铜钱；募将令招募必得良品以上，40 抽保底极品+（当前保底进度 ' +
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
  const bs = S.battles || [];
  if (!bs.length) return '<div class="empty">尚无战报</div>';
  return bs.map((b) => {
    const when = new Date(b.created_at * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const node = nodeById(b.node_id);
    const manualLive = b.mode === "manual" && ["deploy", "battle"].includes(b.state);
    if (manualLive) {
      const left = Math.max(0, Number(b.deadline_at || 0) - Math.floor(Date.now() / 1000));
      return '<div class="card live"><h3>🔴 ' +
        ({ field: "会战", street: "巷战", siege: "攻城战" }[b.kind] || "战役") +
        " · " + esc(node ? node.name : "据点#" + b.node_id) +
        ' <span class="tag epic">' + (b.state === "deploy" ? "部署中" : "手操进行中") + "</span></h3>" +
        '<div class="sub">' + (b.state === "deploy" ? "部署剩余 " + left + " 秒" : "回合限时 " + left + " 秒") + "</div>" +
        '<div class="drawer-actions"><button class="btn small primary" data-act="enter-battle" data-id="' + b.id + '">⚔️ 进入手操</button></div></div>';
    }
    const win = b.winner === "attacker" ? "攻方胜" : b.winner === "defender" ? "守方胜" : "未分胜负";
    return '<div class="card"><h3>' + ({ field: "⚔️", street: "🏙️", siege: "🔥" }[b.kind] || "⚔️") +
      " " + esc(node ? node.name : "据点#" + b.node_id) + " · " + win +
      (b.mode === "manual" ? ' <span class="tag">手操</span>' : "") +
      ' <span class="sub">' + when + " · " + b.rounds + " 回合</span></h3>" +
      '<details><summary class="sub">展开战报</summary><div class="report-text">' + esc(b.report_text) + "</div></details>" +
      '<div class="drawer-actions"><button class="btn small" data-act="enter-battle" data-id="' + b.id + '">🎞️ 复盘/回放</button></div></div>';
  }).join("");
}

function tabShop() {
  const p = S.player || {};
  const week = ((p.honor || {}).merit_shop_week || {});
  const elixirUsed = (p.honor || {}).elixir_used || {};
  const injured = (p.chars || []).filter((c) => c.injury > 0);
  let h = '<div class="card"><h3>💰 灵石包</h3><div class="sub">500 战功 → 100,000 灵石（注入主玩法钱包，计入每日限额）<br>本周已兑 ' +
    (week.used || 0) + "/5</div>" +
    '<div class="drawer-actions"><button class="btn small primary" data-act="buy-gold">兑换灵石包</button></div></div>';
  h += '<div class="card"><h3>💊 疗伤丹</h3><div class="sub">1500 战功 → 指定角色伤势 -40（每角色每赛季 2 次）</div>';
  if (injured.length) {
    h += '<div class="form-row"><select id="elixirChar">' + injured.map((c) =>
      '<option value="' + c.id + '">' + esc(c.name) + "（" + c.injury_name + " " + c.injury + "，已用 " + (elixirUsed[String(c.id)] || 0) + "/2）</option>").join("") +
      '</select><button class="btn small primary" data-act="buy-elixir">服用</button></div>';
  } else {
    h += '<div class="sub">（当前无受伤角色）</div>';
  }
  h += "</div>";
  h += '<div class="card"><h3>📖 战功来源</h3><div class="sub">会战击杀 ×0.05 + 胜利 100；据点产出灵石超过每日限额（500 万）的部分自动折算战功；讨伐任务</div></div>';
  return h;
}

// ---------------------------------------------------------------------------
// 势力（P1 §4/§6.3/§8）
// ---------------------------------------------------------------------------
function myNodes() {
  return (S.nodes || []).filter((n) => n.owner && n.owner.id === myFactionId());
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
    ((S.factions || []).find((x) => x.id === f.id) || {}).color + '"></span> ' + esc(f.name) +
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
          x.color + '"></span> ' + esc(x.name) + "（声望 " + x.reputation +
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
    '<button class="btn small" data-act="deposit">📥 捐入国库</button></div></div>';
  // --- 外交矩阵 ---
  h += '<div class="card"><h3>🤝 外交</h3>';
  const rels = f.relations || [];
  if (rels.length) {
    h += '<table class="tbl"><tr><th>势力</th><th>关系</th><th>操作</th></tr>';
    for (const r of rels) {
      let ops = "-";
      if (r.rel.startsWith("propose_") && r.from !== f.id && canGovern) {
        ops = '<button class="btn small primary" data-act="dip-respond" data-id="' + r.faction_id + '" data-accept="1">接受</button> ' +
          '<button class="btn small" data-act="dip-respond" data-id="' + r.faction_id + '" data-accept="0">拒绝</button>';
      }
      h += "<tr><td>" + esc(r.name) + "</td><td>" + (REL_NAMES[r.rel] || r.rel) +
        (r.indemnity ? "（赔款 " + fmtNum(r.indemnity) + "）" : "") + "</td><td>" + ops + "</td></tr>";
    }
    h += "</table>";
  }
  if (canGovern) {
    const others = (S.factions || []).filter((x) => x.kind === "player" && x.alive && x.id !== f.id);
    h += '<div class="form-row"><select id="dipTarget" style="max-width:120px">' +
      others.map((x) => '<option value="' + x.id + '">' + esc(x.name) + "</option>").join("") +
      '</select><select id="dipKind" style="max-width:100px">' +
      '<option value="ally">结盟提案</option><option value="truce">停战提案</option>' +
      '<option value="peace">和谈（赔款）</option><option value="war">宣战</option></select>' +
      '<input id="dipIndemnity" type="number" min="0" placeholder="赔款(和谈)" style="max-width:100px">' +
      '<button class="btn small primary" data-act="dip-propose">发起</button></div>';
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
  h += '<div class="card"><h3>⛓️ 俘虏</h3>';
  if (cap.holding.length) {
    h += '<div class="sub">我方关押：</div><table class="tbl"><tr><th>俘虏</th><th>忠诚</th><th>操作</th></tr>';
    for (const c of cap.holding) {
      h += "<tr><td>" + esc(c.name) + ' <span class="tag ' + c.rarity + '">' +
        ((S.meta.rarity_names || {})[c.rarity] || c.rarity) + "</span></td><td>" +
        (c.disloyal ? '<span style="color:#f85149">离心!</span> ' : "") + c.loyalty + "</td><td>" +
        '<button class="btn small primary" data-act="persuade" data-id="' + c.id + '">劝降</button> ' +
        '<button class="btn small" data-act="ransom-set" data-id="' + c.id + '">定价赎回</button> ' +
        '<button class="btn small" data-act="release" data-id="' + c.id + '">释放</button></td></tr>';
    }
    h += "</table>";
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
  if (!f.capital_node_id) {
    const free = (S.nodes || []).filter((n) => !n.owner && ["city", "pass", "branch"].includes(n.type));
    if (free.length) {
      h += '<div class="form-row"><select id="rebuildNode" style="max-width:150px">' +
        free.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
        '</select><button class="btn small danger" data-act="rebuild-capital">🔥 重建主城（花灵石，残军散尽）</button></div>';
    } else {
      h += '<div class="sub">（已无无主城池/关隘/分舵可供重建；可申请加入其他玩家势力）</div>';
    }
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
      h += '<div class="form-row"><span class="lg-dot" style="display:inline-block;background:' + pet.color + '"></span> ' +
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
      h += '<div class="form-row"><span class="lg-dot" style="display:inline-block;background:' + x.color + '"></span> ' +
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
const ITEM_NAMES = { grain: "粮草", iron: "精铁", recruit_token: "募将令" };

function tabMarket() {
  const p = S.player || {};
  let h = "";
  // --- 流将限时竞拍（§11 天降流将）---
  const auctions = S.wanderer_auctions || [];
  h += '<div class="card"><h3>🎴 流将竞拍（限时）</h3>';
  if (auctions.length) {
    for (const a of auctions) {
      h += '<div class="card thin"><h3>' + esc(a.name) +
        ' <span class="tag ' + a.rarity + '">' + esc(a.rarity_name || a.rarity) + "</span>" +
        '<span class="tag">' + esc(a.role_name || "") + "</span></h3>" +
        '<div class="sub">' + statGrid(a.stats || {}) + " · 流落 " + esc(a.node_name || "?") +
        ' · 剩余 <span class="aucLeft" data-expire="' + a.expire_at + '">' +
        fmtRemain(a.expire_at - Math.floor(Date.now() / 1000)) + "</span></div>" +
        ((a.trait_names || []).length
          ? '<div class="sub">特技：' + a.trait_names.map(esc).join("、") + "</div>" : "") +
        '<div class="sub">起拍 ' + fmtNum(a.start_price) + " · 当前 " +
        (a.current_price > 0
          ? fmtNum(a.current_price) + "（" + esc(a.bidder_label || "") + "）"
          : "无人出价") +
        " · 一口价 " + fmtNum(a.buyout_price) + "</div>" +
        (a.bidder_is_me
          ? '<div class="sub">✅ 你正领先；被超价后托管铜钱自动退还</div>'
          : '<div class="form-row"><input id="aid_' + a.id + '" type="number" min="' + a.min_next +
            '" value="' + a.min_next + '" style="max-width:110px">' +
            '<button class="btn small primary" data-act="bid-wanderer" data-id="' + a.id +
            '" data-buyout="0">出价</button>' +
            '<button class="btn small" data-act="bid-wanderer" data-id="' + a.id +
            '" data-buyout="1">一口价 ' + fmtNum(a.buyout_price) + "</button></div>") +
        "</div>";
    }
  } else {
    h += '<div class="sub">（暂无流将挂拍；世界事件「天降流将」开启时，极品/传世流将会在坊市限时竞拍）</div>';
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
        "</td><td>" + (ITEM_NAMES[o.item_type] || o.item_type) + "</td><td>" + fmtNum(o.amount) +
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
  if (mkNodes.length) {
    h += '<div class="form-row"><select id="mkNode" style="max-width:110px">' +
      mkNodes.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
      '</select><select id="mkItem" style="max-width:90px">' +
      Object.entries(ITEM_NAMES).map(([k, v]) => '<option value="' + k + '">' + v + "</option>").join("") +
      '</select><input id="mkAmount" type="number" min="1" placeholder="数量" style="max-width:80px">' +
      '<input id="mkPrice" type="number" min="1" placeholder="单价(币)" style="max-width:90px">' +
      '<button class="btn small primary" data-act="market-list">挂售</button></div>';
  } else h += '<div class="sub">（占据坊市/仙城后可挂售）</div>';
  h += "</div>";
  // --- 商路 ---
  const routes = S.trade_routes || [];
  if (routes.length) {
    h += '<div class="card"><h3>🛤️ 商路</h3><div class="sub">' + routes.map((r) =>
      esc(r.a_name) + " ⇄ " + esc(r.b_name) + "（途经 " + (r.path || []).length + " 节点）").join("<br>") +
      "</div></div>";
  }
  // --- 运粮队 ---
  h += '<div class="card"><h3>🚚 运粮队</h3>';
  const convoys = p.convoys || [];
  if (convoys.length) {
    h += convoys.map((c) => '<div class="sub">🌾 ' + fmtNum(c.grain) + " 粮 · 护送 " +
      fmtNum(c.escort_troops || 0) + " 兵 · " + esc((nodeById(c.from) || {}).name || "?") + " → " +
      esc((nodeById(c.to) || {}).name || "?") + "（现于 " + esc((nodeById(c.cur_node) || {}).name || "?") + "）</div>").join("");
  } else h += '<div class="sub">（无在途粮队；粮队沿商路自动行进，可能被匪寨劫掠）</div>';
  const srcs = myNodes();
  if (srcs.length > 1) {
    const escorts = (p.armies || []).filter((a) => a.state === "idle");
    h += '<div class="form-row"><select id="cvFrom" style="max-width:110px">' +
      srcs.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
      '</select>→<select id="cvTo" style="max-width:110px">' +
      srcs.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
      '</select><input id="cvGrain" type="number" min="1" placeholder="粮草" style="max-width:80px">' +
      '<select id="cvEscort" style="max-width:120px"><option value="0">无护送</option>' +
      escorts.map((a) => '<option value="' + a.id + '">' + esc(a.name) + "</option>").join("") +
      '</select><button class="btn small primary" data-act="send-convoy">发粮队</button></div>';
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
  // 已统一为上方「流将竞拍」卡片，旧的一口价招募入口不再提供
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
  h += '<div class="card"><h3>🎯 演武擂台</h3><div class="sub">摆擂押战功，全服应战；胜者通吃（每赛季限次）</div>';
  const duels = p.duels || [];
  if (duels.length) {
    for (const d of duels) {
      h += '<div class="form-row">' + (d.mine ? "🔵 我的擂台：" : "🔴 ") + esc(d.challenger || "?") + " 的 " +
        esc(d.char_name || "?") + "（赌注 " + fmtNum(d.stake) + " 战功）" +
        (d.mine ? "" : '<button class="btn small primary" data-act="duel-accept" data-id="' + esc(d.duel_id) + '">应战</button>') + "</div>";
    }
  } else h += '<div class="sub">（当前无人摆擂；在府邸页武将卡片可摆擂）</div>';
  h += "</div>";
  return h;
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
    btn.addEventListener("click", () => {
      const act = btn.dataset.act;
      const id = Number(btn.dataset.id);
      if (act === "recruit") doAction("recruit", { index: Number(btn.dataset.i) });
      else if (act === "recruit-premium") doAction("recruit", { index: Number(btn.dataset.i), premium: true });
      else if (act === "buy-gold") doAction("buy_merit_shop", { item: "gold_pack" });
      else if (act === "buy-elixir") {
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
      } else if (act === "duel-open") {
        const stake = prompt("摆擂赌注（战功，≥100）：", "100");
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
      } else if (act === "dip-respond") {
        doAction("diplomacy_respond", { from_fid: id, accept: btn.dataset.accept === "1" });
      } else if (act === "dip-propose") {
        doAction("diplomacy_propose", {
          target_fid: Number((document.getElementById("dipTarget") || {}).value || 0),
          kind: (document.getElementById("dipKind") || {}).value || "ally",
          indemnity: Number((document.getElementById("dipIndemnity") || {}).value || 0),
        });
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
      }
      // --- 主城/退盟/立派 ---
      else if (act === "buy-shield") {
        doAction("buy_shield", { node_id: Number((document.getElementById("shieldNode") || {}).value || 0) });
      } else if (act === "rebuild-capital") {
        const nid = Number((document.getElementById("rebuildNode") || {}).value || 0);
        if (confirm("重建主城将消耗灵石（主玩法钱包扣费），确认选定该无主节点？")) {
          doAction("rebuild_capital", { node_id: nid });
        }
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
        doAction("send_convoy", {
          from_node: Number((document.getElementById("cvFrom") || {}).value || 0),
          to_node: Number((document.getElementById("cvTo") || {}).value || 0),
          grain: Number((document.getElementById("cvGrain") || {}).value || 0),
          escort_army_id: Number((document.getElementById("cvEscort") || {}).value || 0),
        });
      } else if (act === "raid") {
        const sel = document.getElementById("raid_" + btn.dataset.node);
        doAction("raid_bandit", { army_id: Number(sel ? sel.value : 0), node_id: Number(btn.dataset.node) });
      } else if (act === "bid-wanderer") {
        const inp = document.getElementById("aid_" + id);
        const instant = btn.dataset.buyout === "1";
        const amount = instant ? 0 : Number((inp || {}).value || 0);
        if (!instant && !amount) { toast("请输入出价", "err"); return; }
        if (instant && !confirm("确认按一口价直接竞得该流将？")) return;
        doAction("bid_wanderer", { auction_id: id, amount: amount, buyout: instant });
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
  await doAction("enter", { node_id: nodeId });
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

(async function init() {
  initLegend();
  resizeCanvas();
  await load();
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
