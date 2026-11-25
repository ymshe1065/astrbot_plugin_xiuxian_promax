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
  propose_war_out: "宣战提案（已发出）", propose_truce_out: "停战提案（已发出）",
  propose_ally_out: "结盟提案（已发出）", propose_peace_out: "和谈提案（已发出）",
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
let tickRefreshFor = 0;
let settleRefreshFor = 0;   // 任务预期结算时刻去重：同一时间戳只主动重取一次快照
let logState = { kind: "", items: [], hasMore: false, beforeId: 0, inited: false };
let busyRetries = 0;

// 倒计时统一走服务端时间基准（v4.6.5）：设备时钟偏差不再让倒计时提前归零/停滞。
// clock.js 未加载（老页面/缓存）时退化为本地时间，保证功能可用。
function srvNowSec() {
  return window.XiuXianClock ? XiuXianClock.nowSec()
    : Math.floor(Date.now() / 1000);
}
// 未入场玩家选择「先看看」后，轮询不再反复弹出遮罩；页面刷新后恢复提示。
let joinBrowse = false;

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

async function api(path, opts, silent) {
  const a = auth();
  if (!a || !a.token) { location.href = "../index.html"; throw new Error("未登录"); }
  let resp;
  try {
    resp = await fetch(path, Object.assign({
      headers: { "Authorization": "Bearer " + a.token, "Content-Type": "application/json" },
    }, opts || {}));
  } catch (e) {
    // 断网/服务器不可达：fetch 直接 reject，此前 doAction 静默吞掉，
    // 玩家只看到界面没反应。标记 network 供动作层提示（轮询保持安静）。
    const err = new Error("网络异常");
    err.network = true;
    throw err;
  }
  let data = null;
  try { data = await resp.json(); } catch (e) { /* ignore */ }
  if (resp.status === 401) { location.href = "../index.html"; throw new Error("登录已过期"); }
  if (resp.status === 423) {
    if (!silent) toast("⏳ 世界推进中，请稍后再试", "err");
    throw new Error("busy");
  }
  if (!resp.ok || (data && data.ok === false)) {
    const msg = (data && (data.error || data.msg)) || ("HTTP " + resp.status);
    if (!silent) toast("❌ " + msg, "err");
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
  } catch (e) {
    if (e && e.network) toast("🌐 网络异常，请检查连接后重试", "err");
    return null;
  }
}

let loadSeq = 0;   // 请求序号：20s 轮询与动作后的 load() 可能乱序返回
async function load() {
  const seq = ++loadSeq;
  try {
    const r = await api("/api/strategy/state");
    // 旧请求晚到：丢弃，避免把新状态覆盖回旧快照（界面回滚/重复操作）
    if (seq !== loadSeq) return;
    S = r.state;
    // 每次快照都顺手对齐服务端时间（20s 轮询即持续校准，无需额外请求）
    if (window.XiuXianClock) XiuXianClock.syncFrom(S);
    // 江湖纪事首屏：首次进入用快照填充；后续轮询只把新记录并到顶部
    if (Array.isArray(S.logs)) {
      if (!logState.inited) {
        logState.items = S.logs.slice();
        logState.hasMore = !!S.log_has_more;
        logState.inited = true;
      } else if (!logState.kind) {
        const seen = new Set((logState.items || []).map((x) => x.id));
        const fresh = S.logs.filter((x) => !seen.has(x.id));
        if (fresh.length) logState.items = fresh.concat(logState.items || []);
        if (S.log_has_more) logState.hasMore = true;
      }
    }
    busyRetries = 0;
    // 已入场 / 主动选择先看看时不遮罩；未入场默认弹出入场选择。
    document.getElementById("joinOverlay").hidden = !!S.joined || joinBrowse;
    if (!S.joined) renderJoinCandidates();
    renderAll();
    checkManualBattles();
  } catch (e) {
    // 世界推进期间 /api/strategy/state 会返回 423；此刻快照不可读。
    // 保留旧界面并短暂重试，避免 tick 边界或动作后的刷新被一次 423 吞掉，
    // 导致顶栏与势力面板长时间停在旧数值。
    if (String((e && e.message) || "") === "busy" && busyRetries < 3) {
      busyRetries += 1;
      setTimeout(load, 1200 * busyRetries);
    } else {
      busyRetries = 0;
    }
  }
  // 有手操战役时缩短轮询：部署窗口 90s、回合 30s，20s 轮询太容易错过。
  schedulePoll();
}

function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer);
  const liveCount = S ? liveManualBattles().length : 0;
  const ms = liveCount ? 5000 : POLL_MS;
  pollTimer = setTimeout(load, ms);
}

function initStrategyChannel() {
  try {
    if (typeof BroadcastChannel === "undefined") return;
    // 聊天室页面收到 WS 后转发 strategy.notify / battle.event；战略页不
    // 另开 WS（同账号第二条连接会把聊天室顶下线）。
    const bc = new BroadcastChannel("xiuxian_strategy");
    bc.onmessage = (ev) => {
      const m = ev.data || {};
      if (m.type === "strategy.delta") {
        if (!document.hidden) load();
      } else if (m.type === "strategy.notify") {
        onStrategyNotice(String(m.kind || ""), m.payload || {});
      } else if (m.type === "strategy.battle.event") {
        if (window.BattleView && BattleView.isOpen()) BattleView.refresh();
      }
    };
  } catch (e) { /* 不支持时战略页仍靠轮询兜底 */ }
}

// ---------------------------------------------------------------------------
// 浏览器通知（§12.5 F：首次进面板请求授权；手操战役轮询检测）
// ---------------------------------------------------------------------------
const NOTIFY_DISMISS_KEY = "xiuxian_notify_dismiss_v1";
let seenManualBattles = null;   // 上一轮轮询看到的进行中战役 id
const noticeSeen = new Map();   // 事件去重：同一场同一类提示短期内只弹一次
const lastTurnKey = new Map();  // battle_id → 当前行动方标识（回合提醒去重）

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

function battleOpenUrl(battleId) {
  return battleId
    ? ("/static/strategy/index.html?battle=" + encodeURIComponent(battleId))
    : "/static/strategy/index.html";
}

function notifyLocal(title, body, url, tag) {
  try {
    if (typeof Notification !== "undefined" && Notification.permission === "granted") {
      const n = new Notification(title, { body, tag: tag || title });
      if (url) n.onclick = () => {
        try { window.open(url, "_blank"); } catch (e) { /* 移动端忽略 */ }
      };
    }
  } catch (e) { /* 移动端可能抛错，忽略 */ }
}

function noticeKey(kind, pl) {
  const bid = Number(pl.battle_id || 0);
  if (kind === "battle_turn") {
    return kind + ":" + bid + ":" + (pl.round || 0) + ":" + (pl.uid || 0);
  }
  if (kind === "battle_open") return kind + ":" + bid + (pl.extended ? ":ext" : "");
  if (kind === "battle_result") return kind + ":" + bid;
  // v4.6.1：NPC 宣战 / 求和按势力去重，避免同一势力 60s 内重复弹窗
  if (kind === "war_declared" || kind === "peace_offer") {
    return kind + ":" + (pl.faction_id || pl.text || "");
  }
  return kind + ":" + (bid || pl.node_id || pl.text || "");
}

function shouldNotice(key) {
  const now = Date.now();
  const last = noticeSeen.get(key);
  if (last && now - last < 60000) return false;
  noticeSeen.set(key, now);
  if (noticeSeen.size > 80) {
    for (const [k, t] of noticeSeen) {
      if (now - t > 300000) noticeSeen.delete(k);
    }
  }
  return true;
}

function onStrategyNotice(kind, pl) {
  pl = pl || {};
  const key = noticeKey(kind, pl);
  if (!shouldNotice(key)) return;
  const where = pl.node_name || (pl.node_id ? "据点#" + pl.node_id : "战场");
  let text = "";
  if (kind === "battle_open") {
    text = "🔴 手操战役打响：" + where +
      (pl.extended ? "（备战已延长，请尽快进入）" : "（战报页可进入）");
  } else if (kind === "battle_turn") {
    text = "⏳ 轮到你行动：" + where +
      (pl.round ? "（第 " + pl.round + " 回合）" : "") + "，30 秒内未操作将自动托管";
  } else if (kind === "battle_result") {
    text = "🏁 战役结束：" + where + " · " +
      (pl.winner === "attacker" ? "攻方胜"
        : pl.winner === "defender" ? "守方胜" : "未分胜负");
  } else if (kind === "duel_invite") {
    text = "🎯 有敌将向你发起阵前单挑！进入手操战役应战";
  } else if (kind === "siege_alert") {
    text = "🔥 我方「" + where + "」正被围城！";
  } else if (kind === "war_declared") {
    // v4.6.1：NPC 自主宣战。payload 只有 faction_id/faction_name/goal，
    // 旧逻辑会直接显示原始 kind（war_declared）。
    text = "🔥「" + (pl.faction_name || "NPC 势力") +
      "」向你宣战！可在「天下」页查看威胁与战况";
  } else if (kind === "peace_offer") {
    text = "🕊「" + (pl.faction_name || "NPC 势力") +
      "」遣使求和，前往「势力/联盟」页外交回应";
  } else {
    text = "⚔️ 仙域争霸：" + (pl.text || kind);
  }
  toast(text);
  notifyLocal("⚔️ 仙域争霸", text.replace(/^[^\s]+\s/, ""),
    battleOpenUrl(pl.battle_id), "strategy-" + key);
  // 状态可能已变化（新战役/回合/终局/NPC 宣战求和）：拉一次快照刷新
  // 战报、顶部横幅与「天下」页。
  if (kind.startsWith("battle_") || kind === "war_declared" ||
      kind === "peace_offer") {
    setTimeout(() => { if (!document.hidden) load(); }, 150);
  }
}

function checkManualBattles() {
  const live = liveManualBattles();
  const ids = new Set(live.map((b) => b.id));
  for (const b of live) {
    const where = nodeById(b.node_id) ? nodeById(b.node_id).name : "据点#" + b.node_id;
    // 首次加载也要提示：此前 seenManualBattles=null 直接返回，玩家刷新
    // 页面后正在备战的手操战役反而没有任何提醒，只能等超时自动打完。
    if (!seenManualBattles || !seenManualBattles.has(b.id)) {
      onStrategyNotice("battle_open", {
        battle_id: b.id, node_id: b.node_id, node_name: where,
        deadline_at: b.deadline_at,
      });
    }
    // 轮到我行动（战略页没有 WS，靠 5s/轮询兜底；战场覆盖层已打开则由
    // battle.js 自己提示，不重复弹）。
    const watching = !!(window.BattleView && BattleView.isOpen());
    if (b.my_turn && !watching) {
      const turnKey = (b.turn_round || 0) + ":" + (b.current_actor || 0) +
        ":" + (b.turn_deadline || 0);
      if (lastTurnKey.get(b.id) !== turnKey) {
        lastTurnKey.set(b.id, turnKey);
        onStrategyNotice("battle_turn", {
          battle_id: b.id, node_id: b.node_id, node_name: where,
          round: b.turn_round || 0, uid: b.current_actor || 0,
          deadline_at: b.turn_deadline || b.deadline_at,
        });
      }
    } else if (!b.my_turn) {
      lastTurnKey.delete(b.id);
    }
  }
  for (const bid of Array.from(lastTurnKey.keys())) {
    if (!ids.has(bid)) lastTurnKey.delete(bid);
  }
  // 只靠轮询时也能发现「上一轮还在、这一轮已结束」的手操战役，补一条终局提示。
  if (seenManualBattles) {
    for (const bid of seenManualBattles) {
      if (ids.has(bid)) continue;
      const b = (S.battles || []).find((x) => x.id === bid);
      if (b && b.mode === "manual") {
        onStrategyNotice("battle_result", {
          battle_id: bid, node_id: b.node_id,
          node_name: nodeById(b.node_id) ? nodeById(b.node_id).name : "",
          winner: b.winner,
        });
      }
    }
  }
  seenManualBattles = ids;
}

function liveManualBattles() {
  return (S && S.battles ? S.battles : []).filter((b) =>
    b.mode === "manual" && ["deploy", "battle"].includes(b.state));
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

// 持续任务的小时边界由服务端 world tick 触发结算（_step_settle_tasks 只在
// tick 管线内执行），边界到 tick 之间会空转一个 tick 周期。服务端下发
// expect_settle_at（已对齐到首个不早于边界、且不早于下一个推进的时刻）；
// 旧快照缺该字段时用 tick_seconds + next_tick_at 前端推算，避免倒计时
// 在 tick 到来前一直停在 0s。
function taskSettleAt(t) {
  const next = Number((t && t.next_settle_at) || 0);
  const expect = Number((t && t.expect_settle_at) || 0);
  if (expect > 0) return expect;
  const tickSec = Number(((S && S.meta) || {}).tick_seconds || 0);
  const nextTickAt = Number(((S && S.world) || {}).next_tick_at || 0);
  if (next > 0 && tickSec > 0 && nextTickAt > 0) {
    const steps = Math.max(0, Math.ceil((next - nextTickAt) / tickSec));
    return nextTickAt + steps * tickSec;
  }
  return next;
}

// 固定时长任务（计略 / 侦察 / 劫掠等）：用 .aucLeft 交给每秒刷新的
// tickCountdown 实时递减；服务端下发 expect_end_at（对齐实际结算 tick），
// 旧快照缺字段时回退 end_at，避免「切回页签才刷新一次」的假卡死。
function taskCountdownHtml(t) {
  const end = Number((t && (t.expect_end_at || t.end_at)) || 0);
  if (!end) return fmtRemain(Number((t && t.remain_sec) || 0));
  const left = Math.max(0, end - srvNowSec());
  return '<span class="aucLeft" data-expire="' + end + '">' +
    fmtRemain(left) + "</span>";
}

// ---------------------------------------------------------------------------
// 进行中手操战役：顶部常驻横幅 + 战报页红点
// 手操窗口只有 90 秒（野战 300 秒），toast 一闪而过很容易错过；这里把
// 「正在备战/轮到我」做成常驻条，并每秒刷新倒计时。
// ---------------------------------------------------------------------------
function earliestLiveBattle() {
  const live = liveManualBattles();
  live.sort((a, b) => {
    // 轮到我行动的战斗优先展示，其次按最早截止排序
    const mt = (b.my_turn ? 1 : 0) - (a.my_turn ? 1 : 0);
    if (mt) return mt;
    return Number(a.deadline_at || 0) - Number(b.deadline_at || 0);
  });
  return live[0] || null;
}

function renderBattleAlert() {
  const box = document.getElementById("battleAlert");
  const live = liveManualBattles();
  const tab = document.querySelector('.tab[data-tab="battles"]');
  if (tab) tab.classList.toggle("live", live.length > 0);
  if (!box) return;
  if (!live.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  const b = earliestLiveBattle();
  const node = nodeById(b.node_id);
  const where = node ? node.name : "据点#" + b.node_id;
  const myTurn = live.some((x) => x.my_turn);
  box.hidden = false;
  box.innerHTML =
    '<span class="battle-alert-dot">🔴</span>' +
    '<span class="battle-alert-text">' +
    (myTurn ? "轮到你行动 · " : "手操战役 · ") + esc(where) +
    (live.length > 1 ? "（另有 " + (live.length - 1) + " 场）" : "") +
    ' · <b id="battleAlertLeft">--</b></span>' +
    '<button class="btn small primary" id="battleAlertEnter" type="button">进入手操</button>';
  const btn = document.getElementById("battleAlertEnter");
  if (btn) btn.addEventListener("click", () => {
    if (window.BattleView) BattleView.open(b.id);
    else toast("战役模块未加载", "err");
  });
  updateBattleAlertLeft();
}

function updateBattleAlertLeft() {
  const el = document.getElementById("battleAlertLeft");
  if (!el || !S) return;
  const b = earliestLiveBattle();
  if (!b) { el.textContent = ""; return; }
  const left = Math.max(0, Number(b.deadline_at || 0) - srvNowSec());
  const prefix = b.my_turn ? "你的回合 · " : "";
  el.textContent = prefix + (b.state === "deploy" ? "部署剩余 " : "回合剩余 ") + left + "s";
}

function nodeById(id) { return (S.nodes || []).find((n) => n.id === id); }

// ---------------------------------------------------------------------------
// v81 所在地机制：传送 / 驻守 / 潜力 / 建造 / 持续任务
// 所有名称来自后端快照，拼接 HTML 前必须 esc()。
// ---------------------------------------------------------------------------
function tickSec() {
  return Math.max(1, Number((((S || {}).meta || {}).tick_seconds) || 900));
}

// 据点是否处于围城：后端只要存在 siege_state 就暂停建造/禁止离开，
// 首 tick 的 ticks 可能为 0，不能再用 ticks>0 判断（会漏掉 15 分钟）。
function nodeSieged(n) {
  return !!(n && n.siege);
}

function tickDurationText(ticks) {
  const t = Math.max(0, Number(ticks) || 0);
  return t + " tick（约 " + fmtRemain(t * tickSec()) + "）";
}

function nodeNameById(id) {
  const n = nodeById(Number(id) || 0);
  return n ? String(n.name || "") : "";
}

function charAtNode(c, nodeId) {
  return Number((c && c.location_node_id) || 0) === Number(nodeId || 0);
}

function charLocText(c) {
  const nm = nodeNameById(c && c.location_node_id);
  if (nm) return nm;
  if (c && c.location) return String(c.location);
  return "未定（按主城）";
}

function charStatusText(c) {
  if (!c) return "?";
  return c.status_name ||
    ((S.meta || {}).char_status_names || {})[c.status] || c.status || "?";
}

function idleHealthyChars(p) {
  return (((p || {}).chars) || []).filter((c) =>
    c.status === "idle" && c.injury_state !== "dying" && c.injury_state !== "dead");
}

function charsAtNode(p, nodeId) {
  return idleHealthyChars(p).filter((c) => charAtNode(c, nodeId));
}

// v4.5.1：主将可为带伤角色，因此独立于「可行动」筛选（只排除濒死/陨落）
function idleAliveChars(p) {
  return (((p || {}).chars) || []).filter((c) =>
    c.status === "idle" && c.injury_state !== "dying" &&
    c.injury_state !== "dead");
}

// 据点是否建有某建筑（快照 buildings 兼容字符串与 {id,level}）
function nodeHasBuilding(n, buildingId) {
  return (((n || {}).buildings) || []).some((b) => {
    const id = typeof b === "string" ? b : (b && b.id);
    const lv = typeof b === "string" ? 1 : Number((b && b.level) || 0);
    return String(id) === String(buildingId) && lv > 0;
  });
}

// 可停留据点类型以后端 meta 为准（CHAR_DWELL_NODE_TYPES，v4.5.2 起含遗迹）
// 后端实际下发在 meta.location_cfg.dwell_node_types；兼容旧的顶层字段。
function dwellTypes() {
  const meta = (S || {}).meta || {};
  const t = (meta.location_cfg || {}).dwell_node_types || meta.dwell_node_types;
  return Array.isArray(t) && t.length ? t : null;
}

function canDwellAt(n) {
  if (!n) return false;
  const t = dwellTypes();
  return t ? t.indexOf(String(n.type)) >= 0 : n.type !== "ruin";
}

// 传送目标：己方/联盟成员势力且可停留的据点
function moveTargets() {
  return friendlyNodes().filter(canDwellAt);
}

// 可摆擂 / 可应战的据点（建有擂将台）
function arenaNodes() {
  return friendlyNodes().filter((n) => nodeHasBuilding(n, "arena"));
}

function duelEligible(c) {
  return !!(c && c.role === "general" && c.status === "idle" &&
    c.injury_state === "healthy" &&
    Number((c.stats || {}).valor || 0) >= 75);
}

function charAtArena(c) {
  const n = nodeById(Number((c && c.location_node_id) || 0));
  return !!(n && n.owner && factionIsFriendly(n.owner.id) &&
    nodeHasBuilding(n, "arena"));
}

function potentialOf(n) {
  return Math.max(1, Number(n.potential ||
    (((S.meta || {}).node_potential || {})[n.type]) || 1));
}

function levelText(n) {
  const lv = Math.max(1, Number(n.level) || 1);
  return "潜力 " + potentialOf(n) + " · 当前 " + lv + " 级";
}

// 建造中：显示剩余 tick + 动态倒计时（.aucLeft 由 tickCountdown 每秒刷新）
function constructionKvHtml(n) {
  const c = n.construction;
  if (!c) return "";
  const tickNo = Number((((S || {}).world || {}).tick_no) || 0);
  const leftTicks = Math.max(0, Number(c.end_tick || 0) - tickNo);
  const endAt = Number(c.end_at || 0);
  const leftSec = Math.max(0, endAt - srvNowSec());
  const paused = nodeSieged(n);
  return kv("建造", "🚧 建造中：升至 " + Number(c.to_level || 0) + " 级 · 剩余 " +
    leftTicks + " tick" +
    (paused ? ' <span class="warn-text">围城中，建造暂停</span>' :
      ' · <span class="aucLeft" data-expire="' + endAt + '">' + fmtRemain(leftSec) + "</span>"));
}

function upgradeBoxHtml(n, mine) {
  if (!mine) return "";
  const lv = Math.max(1, Number(n.level) || 1);
  const pot = potentialOf(n);
  const siege = nodeSieged(n);
  let h = '<div class="upgrade-box">';
  if (n.construction) {
    h += '<div class="sub">🚧 建造中，完成后可继续升级。</div>' +
      '<div class="drawer-actions"><button class="btn small" disabled>建造中…</button></div>';
  } else if (lv >= pot) {
    h += '<div class="sub">已达据点潜力上限（' + pot + " 级）。</div>";
  } else {
    const cost = n.upgrade_cost || {};
    const coin = Number(cost.coin || 0);
    const iron = Number(cost.iron || 0);
    const ticks = Number(cost.ticks || 0);
    h += '<div class="sub">⬆️ 升级至 ' + (lv + 1) + " 级：🪙 " + fmtNum(coin) +
      " · ⛏️ " + fmtNum(iron) + " · 建造 " + tickDurationText(ticks) +
      (siege ? ' <span class="warn-text">围城中，建造暂停</span>' : "") + "</div>";
    h += '<div class="drawer-actions"><button class="btn small primary" data-act="upgrade-node" data-id="' +
      n.id + '"' + (siege ? " disabled" : "") + ">⬆️ 升级至 " + (lv + 1) + " 级</button></div>";
  }
  return h + "</div>";
}

// 驻守人物：residents 由后端按可见据点下发；传送按钮仅己方/联盟成员势力据点显示
function residentSectionHtml(n, p, canGovern) {
  const list = n.residents || [];
  if (!canGovern && !list.length) return "";
  let h = '<div class="resident-box"><div class="sub">🛡️ 驻守人物（' + list.length + "）</div>";
  if (!list.length) {
    h += '<div class="sub">暂无角色驻守；可从「人员」页把空闲角色传送至此。</div>';
  } else {
    h += '<div class="resident-list">';
    for (const r of list) {
      // 驻守列表里的角色本就在本据点，「传送到此」永远不会成立
      // （旧分支因 charAtNode 恒真而成为死代码），统一由下方下拉提供传送。
      h += '<div class="resident-row"><span class="rs-name">' + esc(r.name) + "</span>" +
        '<span class="tag">' + esc(r.role_name || "角色") + "</span>" +
        '<span class="sub">' + esc(r.status_name || "") + "</span>" +
        (r.is_governor ? '<span class="tag epic">本城太守</span>' : "") +
        "</div>";
    }
    h += "</div>";
  }
  if (canGovern) {
    const others = idleHealthyChars(p).filter((c) => c.can_move && !charAtNode(c, n.id));
    if (others.length) {
      h += '<div class="drawer-actions resident-move"><select id="moveSel_' + n.id + '">' +
        others.map((c) => '<option value="' + c.id + '">' + esc(c.name) + "（" +
          esc(c.role_name) + "·" + esc(charLocText(c)) + "）</option>").join("") +
        '</select><button class="btn small" data-act="move-char-sel" data-node="' +
        n.id + '">🚶 传送到此</button></div>';
    }
  }
  return h + "</div>";
}


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
  // 未入场且已收起遮罩时，顶栏保留一个「入场」入口，随时可回到选择主城。
  const joinOpen = document.getElementById("btnJoinOpen");
  if (joinOpen) joinOpen.hidden = !!S.joined;
}

function tickCountdown() {
  if (!S) return;
  const el = document.getElementById("tickCountdown");
  const left = Math.max(0, S.world.next_tick_at - srvNowSec());
  const m = String(Math.floor(left / 60)).padStart(2, "0");
  const s = String(left % 60).padStart(2, "0");
  el.textContent = "下次推进 " + m + ":" + s;
  // 兜底：WS 不可用或 tick 恰好在倒计时归零时推进，也要尽快重取快照，
  // 不能让顶栏 / 势力面板停在上一 tick 的资源与科技状态。
  const nextAt = Number((S.world || {}).next_tick_at || 0);
  if (left <= 0 && nextAt && tickRefreshFor !== nextAt) {
    tickRefreshFor = nextAt;
    load();
  }
  let expiredSettle = 0;
  document.querySelectorAll(".aucLeft").forEach((node) => {
    const exp = Number(node.dataset.expire || 0);
    const left = exp - srvNowSec();
    // 任务倒计时到点后服务端要等下一个 world tick 才结算，这里显示
    // 「结算中…」而不是一直停在 0s；到期瞬间主动重取一次快照。
    node.textContent = (left <= 0 && node.dataset.pending)
      ? node.dataset.pending : fmtRemain(left);
    if (node.dataset.settle && exp > 0 && left <= 0) expiredSettle = exp;
  });
  if (expiredSettle && settleRefreshFor !== expiredSettle) {
    settleRefreshFor = expiredSettle;
    load();
  }
  updateBattleAlertLeft();
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
  const width = Math.max(1, rect.width);
  const height = Math.max(1, rect.height);
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  // 容器尚未完成布局（宽/高为 0）时不要让 scale 变 0/NaN；
  // 后续 ResizeObserver 会再次调用并恢复正确比例。
  view.scale = Math.max(0.01,
    Math.min(width / WORLD_W, height / WORLD_H) * 0.94);
  view.ox = (width - WORLD_W * view.scale) / 2;
  view.oy = (height - WORLD_H * view.scale) / 2;
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
    if (nodeSieged(n)) {
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
// 特技 / 个性徽标：点击弹出详情
// ---------------------------------------------------------------------------
const TRAIT_KIND_LABELS = {
  trait: "特技", top: "顶级个性", negative: "负面个性", personality: "个性",
};

function traitDescOf(id, name) {
  const m = (S && S.meta) || {};
  const descs = m.trait_descs || {};
  if (id && descs[id]) return descs[id];
  // 兼容旧快照：只有中文名、没有 id 时按名称反查
  const names = m.trait_names || {};
  if (name) {
    for (const k of Object.keys(names)) {
      if (names[k] === name) return descs[k] || "";
    }
  }
  return "";
}

function traitChipHtml(name, desc, kind) {
  const k = kind || "trait";
  const label = TRAIT_KIND_LABELS[k] || "特技";
  const cls = k === "negative" ? " trait-chip-neg"
    : (k === "top" ? " trait-chip-top" : "");
  return '<button type="button" class="trait-chip' + cls +
    '" data-trait-kind="' + esc(label) +
    '" data-trait-name="' + esc(name) +
    '" data-trait-desc="' + esc(desc || "") +
    '" title="点击查看效果详情" aria-label="' +
    esc(name + "：点击查看效果详情") + '">' +
    esc(name) + '<span class="trait-chip-i" aria-hidden="true">ⓘ</span></button>';
}

function traitChips(ids, names) {
  const idList = Array.isArray(ids) ? ids : [];
  const nameList = Array.isArray(names) ? names : [];
  const m = (S && S.meta) || {};
  const nameMap = m.trait_names || {};
  const n = Math.max(idList.length, nameList.length);
  const out = [];
  for (let i = 0; i < n; i += 1) {
    const id = idList[i] ? String(idList[i]) : "";
    const name = nameList[i] || nameMap[id] || id;
    if (!name) continue;
    out.push(traitChipHtml(name, traitDescOf(id, name), "trait"));
  }
  return out.join("");
}

function personalityChipHtml(kind, id, name, desc) {
  const m = (S && S.meta) || {};
  const nameMap = kind === "top"
    ? (m.personality_top_names || {}) : (m.personality_neg_names || {});
  const descMap = kind === "top"
    ? (m.personality_top_descs || {}) : (m.personality_neg_descs || {});
  const label = name || nameMap[id] || id || "";
  if (!label) return "";
  return traitChipHtml(label, desc || descMap[id] || "", kind);
}


// ---------------------------------------------------------------------------
// 据点抽屉
// ---------------------------------------------------------------------------
// 据点类型特色：与 node_output_per_tick / task_node_mult 配置同口径。
// 产出数值从 meta 动态生成，避免配置调整后文案与实际不符。
const NODE_FEATURE = {
  city: "铜钱产出最高，可建全部建筑、城防最强；作为主城时产出额外 +10%",
  pass: "咽喉要地，守城享有城墙/箭塔优势；采矿任务 ×1.3",
  mine: "精铁主产地，采矿任务 ×2.0",
  grain: "粮草主产地，屯田任务 ×2.0",
  market: "坊市互市与商路端点，贸易任务 ×1.3",
  branch: "寻访任务 ×1.4；可建酒馆分号提升本地招募品质",
  ruin: "精铁向据点，采矿 ×1.5、讨伐 ×1.4；潜力 3，可建设升级",
};

function nodeFeatureText(n) {
  let text = NODE_FEATURE[n.type] || "常规据点";
  if (n.type === "city") {
    const bonus = Number((((S.meta || {}).governance_cfg) || {}).capital_output_bonus || 0.1);
    text = text.replace("+10%", "+" + Math.round(bonus * 100) + "%");
  }
  return text;
}

function nodeOutputText(n) {
  const meta = S.meta || {};
  const base = (meta.node_output || {})[n.type] || {};
  const names = meta.resource_names || {};
  const lv = Math.max(1, Number(n.level) || 1);
  const lvMult = 1 + 0.2 * (lv - 1);
  const parts = [];
  for (const key of ["coin", "grain", "iron"]) {
    const amount = Number(base[key] || 0);
    if (amount > 0) {
      parts.push((OUTPUT_ICON[key] || "") + " " + esc(names[key] || key) + " " +
        fmtNum(Math.round(amount * lvMult)));
    }
  }
  const goldBase = Number((((meta.gold_output || {}).per_tick_by_type) || {})[n.type] || 0);
  if (goldBase > 0) parts.push("💎 灵石 " + fmtNum(Math.round(goldBase * lvMult)));
  if (!parts.length) return "—";
  return parts.join(" · ") + ' <span class="sub">/ tick（Lv' + lv +
    " 基础值，等级每 +1 层 +20%；未计建筑/科技/势力/事件加成）</span>";
}

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
  const myF = p.faction || {};
  const allyIds = new Set([myFactionId()].concat(
    (myF.allied_factions || []).map((x) => Number(x.id))));
  const canGovern = !!(n.owner && allyIds.has(Number(n.owner.id)));
  let html = "";
  html += kv("类型", (TYPE_NAME[n.type] || n.type) + " · " + (TERRAIN_NAME[n.terrain] || n.terrain));
  html += kv("等级", levelText(n));
  html += kv("特色", esc(nodeFeatureText(n)));
  html += kv("产出", nodeOutputText(n));
  html += kv("归属", '<span style="color:' + (n.owner ? safeColor(n.owner.color) : "#999") + '">' + esc(n.owner ? n.owner.name : "无主") + "</span>");
  html += moraleBarHtml(n, mine);
  if (n.defense_max > 0) {
    const pct = Math.round(n.defense / n.defense_max * 100);
    html += kv("城防", n.defense + " / " + n.defense_max + ' <span class="sub">(' + pct + '%)</span>');
  }
  html += constructionKvHtml(n);
  if (n.territory && n.territory.adjacent > 0) {
    html += kv("相邻领土", "友方 " + n.territory.adjacent + " 个 · 城防 +" +
      Math.round((n.territory.defense_pct || 0) * 100) + "% · 本土作战 +" +
      Math.round((n.territory.combat_pct || 0) * 100) + "%");
  }
  if (n.roads && n.roads.length) {
    html += kv("友方道路", "连接 " + n.roads.length + " 个友方据点（行军加速、免粮）");
  }
  const gar = n.garrison !== null && n.garrison !== undefined ? n.garrison + " 兵" : (n.garrison_bucket ? "约 " + n.garrison_bucket : "无");
  html += kv("守军", gar);
  html += upgradeBoxHtml(n, canGovern);
  html += governorSectionHtml(n, canGovern, !!myF.can_appoint);
  const canDwell = canGovern && canDwellAt(n);
  html += residentSectionHtml(n, p, canDwell);
  // 大牢关押数量（§14：据点抽屉显示；容量与名册独立）
  const capView = p.captives || {};
  const jailSlots = Math.max(0, Number(capView.jail_slots ||
    ((S.meta || {}).captive_cfg || {}).jail_slots_per_node || 5));
  const jailCount = (capView.holding || []).filter(
    (c) => Number(c.node_id || 0) === Number(n.id)).length;
  if (canDwell && (mine || jailCount)) {
    html += kv("大牢", "⛓️ " + jailCount + " / " + jailSlots +
      ' <span class="sub">（独立于名册）</span>');
  }
  html += buildingSectionHtml(n, canGovern);
  if (nodeSieged(n)) html += kv("围城", "已围 " + Math.max(0, Number(n.siege.ticks || 0)) + " tick" + (n.siege.attacker_faction === myFactionId() ? "（我方发起）" : ""));
  if (n.protected) html += kv("保护", "🛡 保护期中");
  if (n.scouted_until) {
    const left = Math.max(0, n.scouted_until - srvNowSec());
    html += kv("情报", "🕵️ 侦察情报有效（剩 " + fmtRemain(left) + "）");
  }
  // 驻扎军团（可见的）；换将只对“自己的”驻守军团开放（盟友军团仅展示）
  const stationed = (S.armies || []).filter((a) => a.node_id === n.id && a.state !== "march");
  const ownArmyIds = new Set(((p.armies) || []).map((a) => Number(a.id)));
  if (stationed.length) {
    html += '<div class="kv"><span class="k">驻扎</span><span>' + stationed.map((a) =>
      esc(a.name) + (a.mine ? "（我方 " + a.troops + " 兵）" : "（" + a.troops + " 兵）") +
      (a.mine && a.state === "idle" && ownArmyIds.has(Number(a.id))
        ? ' <button class="btn small" onclick="openHandoverModal(' + a.id + ')">🔄 换将</button>'
        : "")).join("<br>") + "</span></div>";
  }
  html += '<div class="drawer-actions">';
  // 围城中的手操介入（§7.2）
  if (nodeSieged(n)) {
    if (n.siege.attacker_faction === myFactionId()) {
      html += '<button class="btn small primary" onclick="doAction(\'intervene_siege\',{node_id:' + n.id + '})">🎮 手操介入（发起攻城战）</button>';
    }
    if (mine) {
      html += '<button class="btn small danger" onclick="doAction(\'sortie\',{node_id:' + n.id + '})">⚔️ 出击（决战解围）</button>';
    }
  }
  if (canDwell) {
    html += '<button class="btn small" onclick="openArmyModal(' + n.id + ')">🛡️ 在此编军</button>';
    html += taskFormHtml(n, p);
    if (!mine) {
      html += '<span class="sub">（联盟成员势力据点：可在此编军/派遣/建造/升级，据点归属与产出仍归该势力）</span>';
    }
  } else if (!canGovern) {
    const idleArmies = (p.armies || []).filter((a) => a.state === "idle");
    for (const a of idleArmies) {
      html += '<button class="btn small primary" onclick="doAction(\'march\',{army_id:' + a.id + ",node_id:" + n.id + '})">⚔️ ' + esc(a.name) + " 出征至此</button>";
    }
    if (!idleArmies.length) html += '<span class="sub">（无可出征军团，先在己方据点编军）</span>';
  } else {
    html += '<span class="sub">（该据点不可驻扎/编军/派遣）</span>';
  }
  html += "</div>";
  const st = captureFormState(body);
  body.innerHTML = html;
  restoreFormState(body, st);
  // 任务类型切换：恢复选择后重新应用「时长/目标」显隐（20s 轮询不丢状态）
  body.querySelectorAll("select[data-task-sel]").forEach((sel) => {
    syncTaskForm(sel.dataset.taskSel);
  });
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

// 据点级太守：每个据点最多 1 名，可与角色官职并存
function governorSectionHtml(n, canGovern, canAppoint) {
  if (!canGovern) return "";
  const gov = n.governor || null;
  const valid = !!(gov && gov.valid);
  const resident = !!(gov && gov.resident);
  const govLabel = gov && gov.char_id
    ? (gov.name ? esc(gov.name) : "未知角色") +
      (valid ? "" : ' <span class="sub">（已失效）</span>') +
      (gov.char_id && valid
        ? (gov.salary_paid === false
          ? ' <span class="warn-text">欠俸（加成暂停）</span>'
          : (resident
            ? ' <span class="tag">驻守本城</span>'
            : ' <span class="warn-text">离城（加成暂停）</span>'))
        : "")
    : '<span class="sub">未任命</span>';
  let h = '<div class="kv"><span class="k">太守</span><span>' + govLabel + "</span></div>";
  if (!canAppoint) {
    const apptLabel = Number(((S.player || {}).faction || {}).alliance_id || 0)
      ? "盟主/副主" : "势力主/副主";
    return h + '<div class="sub">仅' + apptLabel + '可任免太守</div>';
  }
  const f = ((S.player || {}).faction || {});
  const blocked = new Set(["dead", "captive", "transfer", "wander"]);
  const chars = (f.appoint_chars || []).filter((c) =>
    !blocked.has(c.status) && (!gov || Number(c.id) !== Number(gov.char_id)));
  const opts = chars.map((c) =>
    '<option value="' + c.id + '">' + esc(c.name) + "（" + esc(c.role_name) +
    (c.governor_node_name ? "·现任" + esc(c.governor_node_name) + "太守" : "") +
    (c.position ? "·" + positionName(c.position) : "") + "）</option>").join("");
  h += '<div class="drawer-actions">';
  if (opts) {
    h += '<select id="govChar_' + n.id + '" style="max-width:190px">' + opts + "</select>" +
      '<button class="btn small primary" data-act="appoint-taishou" data-id="' + n.id + '">' +
      (gov && gov.char_id ? "更换太守" : "任命太守") + "</button>";
  } else if (!gov) {
    h += '<span class="sub">（暂无可任免角色）</span>';
  }
  if (gov && gov.char_id) {
    h += '<button class="btn small" data-act="dismiss-taishou" data-id="' + n.id + '">卸任</button>';
  }
  h += "</div>";
  if (valid) {
    h += '<div class="sub">' + (gov.salary_paid === false
      ? "太守俸禄 " + fmtNum(gov.salary_per_tick || 0) +
        " 铜钱/tick 未能从势力主钱包支付，本 tick 太守加成暂停；请势力主补充铜钱。"
      : resident
        ? "太守加成生效中：本据点城防 +10%，民心自然恢复基础 ×2，并额外 +1/tick；辖内带兵上限至少 2000。俸禄 " +
          fmtNum(gov.salary_per_tick || 0) + " 铜钱/tick。"
        : "太守已离城，城防 +10% 与民心恢复 ×2 暂停；把太守传送回本城后恢复。俸禄仍按任命照常支出。") + "</div>";
  }
  return h;
}

// 建筑：9 种目录（S.buildings 或 S.meta.buildings），支持建设与升级
// §10.3：所有可停留的己方据点均可建设（含矿田坊市，v4.5.2 起含遗迹）
function buildingSectionHtml(n, mine) {
  if (!canDwellAt(n)) return "";
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

// 侦察目标：任意非己方/联盟成员势力据点；劫掠目标：还需与己方/联盟成员势力据点相邻
function scoutTargetNodes() {
  return (S.nodes || []).filter((x) => !x.owner || !factionIsFriendly(x.owner.id));
}

function raidTargetNodes() {
  const adjFriendly = new Set();
  for (const f of friendlyNodes()) {
    for (const id of (f.adj || [])) adjFriendly.add(Number(id));
  }
  return (S.nodes || []).filter((x) => {
    if (x.owner && factionIsFriendly(x.owner.id)) return false;
    return adjFriendly.has(Number(x.id));
  });
}

function taskTargetLabel(n) {
  return (TYPE_ICON[n.type] || "") + n.name + "（" +
    (n.owner ? n.owner.name : "无主") + "）";
}

function targetOptionsHtml(list, emptyText) {
  if (!list.length) {
    return '<option value="0">' + esc(emptyText) + "</option>";
  }
  return list.map((x) => '<option value="' + x.id + '">' +
    esc(taskTargetLabel(x)) + "</option>").join("");
}

// v4.5.1：驻守军团（state=idle）的随军成员可在当前据点执行本地任务
function localArmyTaskCharsAtNode(p, nodeId) {
  const ids = new Set();
  for (const a of ((p || {}).armies || [])) {
    if (Number(a.node_id) === Number(nodeId) && a.state === "idle") {
      for (const id of (a.character_ids || [])) ids.add(Number(id));
    }
  }
  if (!ids.size) return [];
  return (((p || {}).chars) || []).filter((c) =>
    ids.has(Number(c.id)) && c.status === "army" &&
    c.injury_state !== "dying" && c.injury_state !== "dead");
}

function taskFormHtml(n, p) {
  const idle = charsAtNode(p, n.id);
  const armyChars = localArmyTaskCharsAtNode(p, n.id);
  const taskChars = idle.concat(armyChars);
  const allIdle = idleHealthyChars(p);
  const awayCount = Math.max(0, allIdle.length - idle.length);
  if (!taskChars.length) {
    return '<div class="task-box"><div class="sub">⚠️ 无驻守角色：先在「人员」页把空闲角色传送到此城（需传送）。</div>' +
      (awayCount > 0
        ? '<div class="sub">另有 ' + awayCount + " 名空闲角色不在本据点（需传送）。</div>"
        : "") + "</div>";
  }
  const charOpts = taskChars.map((c) => '<option value="' + c.id + '"' +
    (c.status === "army" ? ' data-army="1"' : "") + ">" + esc(c.name) +
    "（" + esc(c.role_name || "") + (c.status === "army"
      ? "·" + esc(charStatusText(c)) : "") + "）</option>").join("");
  const meta = S.meta.task_meta || {};
  const taskTypes = S.meta.task_types || {};
  const taskOpts = Object.entries(taskTypes).map(([t, name]) => {
    const m = meta[t] || {};
    const icon = OUTPUT_ICON[m.output_kind] || "";
    const label = m.name
      ? m.name + "·" + icon + (m.output || "产出") + "（" + (m.stat || "?") + "）"
      : name;
    const tip = [m.role ? "最佳职责：" + m.role : "", m.output ? "产出：" + m.output : "",
      m.node ? "据点加成：" + m.node : "",
      m.category === "expedition" ? "远征任务：需选择任务时长与目标" : "持续任务：每小时结算"].filter(Boolean).join(" · ");
    return '<option value="' + t + '" title="' + esc(tip) + '">' + esc(label) + "</option>";
  }).join("");
  const outputLegend = Object.entries(taskTypes).map(([t]) => {
    const m = meta[t] || {};
    return (m.name || t) + "→" + (OUTPUT_ICON[m.output_kind] || "") + (m.output || "?");
  }).join(" · ");
  const hourOpts = (S.meta.task_hours || [1, 2, 4, 8]).map((h) =>
    '<option value="' + h + '">' + h + " 小时</option>").join("");
  const uid = "task_" + n.id;
  return '<div class="task-box">' +
    '<div class="drawer-actions">' +
    '<select id="' + uid + '_c" style="max-width:110px">' + charOpts + "</select>" +
    '<select id="' + uid + '_t" data-task-sel="' + uid + '" style="max-width:130px" ' +
    "onchange=\"syncTaskForm('" + uid + "')\">" + taskOpts + "</select>" +
    '<select id="' + uid + '_h" style="max-width:80px" hidden>' + hourOpts + "</select>" +
    '<button class="btn small primary" onclick="dispatchFrom(\'' + uid + "'," + n.id + ')">📜 派遣</button></div>' +
    '<div class="task-continuous" id="' + uid + '_local">🕒 持续执行（每小时结算，可随时在「总览」停止）</div>' +
    '<div class="task-target" id="' + uid + '_scout" hidden><span class="sub">侦察目标（从本据点出发）：</span>' +
    '<select id="' + uid + '_ts">' + targetOptionsHtml(scoutTargetNodes(), "暂无可侦察目标") + "</select></div>" +
    '<div class="task-target" id="' + uid + '_raid" hidden><span class="sub">劫掠目标（从本据点出发，需相邻）：</span>' +
    '<select id="' + uid + '_tr">' + targetOptionsHtml(raidTargetNodes(), "暂无相邻可劫掠目标") + "</select></div>" +
    '<div class="sub">列出本据点空闲角色与驻守军团成员（主将 / 随军）；军团角色只能执行本地任务，军团出军会自动中断其任务。</div>' +
    '<div class="sub">括号内为任务核心属性；悬停可看最佳职责与据点加成</div>' +
    '<div class="sub">任务产出：' + esc(outputLegend) + "</div></div>";
}

// 任务类型切换：本地任务隐藏时长/目标；侦察/劫掠显示时长与对应目标
function syncTaskForm(uid) {
  const tsel = document.getElementById(uid + "_t");
  if (!tsel) return;
  const type = tsel.value;
  const cat = ((((S || {}).meta || {}).task_meta || {})[type] || {}).category;
  const expedition = cat === "expedition" || type === "scout" || type === "raid";
  const hsel = document.getElementById(uid + "_h");
  if (hsel) hsel.hidden = !expedition;
  const localHint = document.getElementById(uid + "_local");
  if (localHint) localHint.hidden = expedition;
  // 军团角色（主将/随军）只能执行本地任务：远征任务时禁用对应候选
  const csel = document.getElementById(uid + "_c");
  if (csel) {
    let firstEnabled = "";
    for (const opt of csel.options) {
      opt.disabled = expedition && opt.dataset.army === "1";
      if (!opt.disabled && !firstEnabled) firstEnabled = opt.value;
    }
    const cur = csel.selectedOptions[0];
    if (cur && cur.disabled) csel.value = firstEnabled;
  }
  const scoutBox = document.getElementById(uid + "_scout");
  if (scoutBox) scoutBox.hidden = type !== "scout";
  const raidBox = document.getElementById(uid + "_raid");
  if (raidBox) raidBox.hidden = type !== "raid";
}

function dispatchFrom(uid, nodeId) {
  const csel = document.getElementById(uid + "_c");
  const tsel = document.getElementById(uid + "_t");
  if (!csel || !tsel) return;
  const charId = Number(csel.value || 0);
  const taskType = tsel.value;
  if (!charId) { toast("请选择驻守角色", "err"); return; }
  const cat = ((((S || {}).meta || {}).task_meta || {})[taskType] || {}).category;
  const expedition = cat === "expedition" || taskType === "scout" || taskType === "raid";
  const copt = csel.selectedOptions[0];
  if (expedition && copt && copt.dataset.army === "1") {
    toast("军团角色（含主将/随军）只能执行当前据点的本地任务；远征任务请选择空闲角色", "err");
    return;
  }
  const params = { char_id: charId, node_id: nodeId, task_type: taskType, hours: 1 };
  if (expedition) {
    const targetSel = document.getElementById(
      uid + (taskType === "raid" ? "_tr" : "_ts"));
    const targetId = Number((targetSel || {}).value || 0);
    if (!targetId) {
      toast("请选择" + (taskType === "raid" ? "劫掠" : "侦察") + "目标据点", "err");
      return;
    }
    params.target_node_id = targetId;
    params.hours = Number((document.getElementById(uid + "_h") || {}).value || 1);
  }
  doAction("dispatch_task", params);
}

function taskPayoutText(t) {
  const icon = OUTPUT_ICON[t.output_kind] || "🎁";
  let s = icon + " " + esc(t.output_name || "产出");
  if (t.estimate !== null && t.estimate !== undefined) {
    s += " ≈" + fmtNum(t.estimate) + (t.sustained ? ' <span class="sub">/ 小时</span>' : "");
  }
  // 劫掠的 output_name 与 note 都是「掠夺库存」，不再重复括注
  if (t.note && t.note !== t.output_name) {
    s += ' <span class="sub">（' + esc(t.note) + "）</span>";
  }
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
function activateTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  renderTab();
}

document.getElementById("tabs").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".tab");
  if (!btn) return;
  activateTab(btn.dataset.tab);
});

function renderTab() {
  const el = document.getElementById("tabBody");
  // 「指引」「天下」不依赖入场态：未入场也能查阅玩法与公开情报
  const isGuide = activeTab === "guide";
  const isWorld = activeTab === "world";
  if ((!S || !S.joined) && !isGuide && !isWorld) {
    el.innerHTML = '<div class="empty">入场后展示</div>';
    return;
  }
  // 指引页已渲染时跳过重绘：20s 轮询不打断输入框焦点与光标（搜索词存于 guide.js state）
  if (isGuide && document.getElementById("guideSearch")) {
    if (typeof window.initGuide === "function") window.initGuide();
    return;
  }
  const fn = {
    world: tabWorld,
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
        : "当前地图暂无空位：可先发起联盟会签与道友共立门户，等待有据点释放后再入场。" },
    { title: "② 编成首支军团", done: (p.armies || []).length > 0,
      desc: "点击己方据点 →「在此编军」→ 选武将、剑盾兵 500，破城槌可选（攻城墙更有效）。" },
    { title: "③ 出征占地", done: nodes.length > 1,
      desc: "军团页点「选择目标出征」，再点地图上相邻的无主据点；守军约 800，建议兵力≥800。" },
    { title: "④ 派遣内政官", done: (p.tasks || []).length > 0,
      desc: "在己方据点抽屉底部，选内政官执行屯田/布政/挖矿，离线也会持续产出。" },
    { title: "⑤ 招贤纳士", done: (p.chars || []).length > 2,
      desc: "酒馆凡品/良品/珍品用铜钱招募，极品/传世用募将令招募。更多武将才能多线出征。" },
    { title: "⑥ 势力、联盟与官职",
      done: (f.members || []).length > 1 || (f.appointments || []).length > 0,
      desc: (f.appointments || []).length
        ? "已任免 " + f.appointments.length + " 名角色的官职；可随时在人员页调整，主将带兵上限按其官职计算。"
        : "人员页可任免角色官职：据点越多，可任免的官职档位越高；主将带兵上限按其角色官职计算。" },
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

// ---------------------------------------------------------------------------
// 江湖纪事（v4.7.0）：计略/战事/据点/外交/NPC/事件 行动历史
// ---------------------------------------------------------------------------
function logKindMeta(kind) {
  const kinds = ((S && S.meta) || {}).world_log_kinds || {};
  return kinds[kind] || { icon: "📜", name: "纪事" };
}

function logCardHtml() {
  const kinds = [["", "全部"]].concat(
    Object.entries(((S && S.meta) || {}).world_log_kinds || {})
      .map(([k, v]) => [k, v.name || k]));
  let h = '<div class="card" id="worldLogCard"><h3>📜 江湖纪事</h3>' +
    '<div class="sub">计略、战事、据点易主、外交与 NPC 行动按时间倒序；' +
    '仅显示你有权知晓的记录（未走漏的计略先只对当事方可见）。</div>' +
    '<div class="log-filter">' + kinds.map(([k, name]) =>
      '<button class="btn small' + (logState.kind === k ? " primary" : "") +
      '" data-act="logs-filter" data-kind="' + k + '">' + esc(name) +
      "</button>").join("") + "</div>";
  const items = logState.items || [];
  if (!items.length) {
    h += '<div class="sub">暂无记录。</div>';
  } else {
    h += '<div class="log-list">' + items.map((it) => {
      const meta = logKindMeta(it.kind);
      const when = it.created_at
        ? new Date(Number(it.created_at) * 1000).toLocaleString() : "";
      const detail = it.detail && Object.keys(it.detail).length
        ? JSON.stringify(it.detail) : "";
      return '<div class="log-row"' + (detail
        ? ' title="' + esc(detail) + '"' : "") + ">" +
        '<div class="log-meta">' + esc(meta.icon + " " + meta.name) +
        " · tick " + Number(it.tick || 0) +
        (when ? " · " + esc(when) : "") + "</div>" +
        '<div class="log-text">' + esc(it.summary || "") + "</div></div>";
    }).join("") + "</div>";
  }
  if (logState.hasMore) {
    h += '<div class="log-more"><button class="btn small" ' +
      'data-act="logs-more">加载更多</button></div>';
  }
  return h + "</div>";
}

async function loadWorldLogs(more) {
  const q = new URLSearchParams({ limit: "20" });
  if (logState.kind) q.set("kind", logState.kind);
  if (more && logState.beforeId) q.set("before_id", String(logState.beforeId));
  try {
    const r = await api("/api/strategy/logs?" + q.toString(), {}, true);
    if (!r || !r.ok) return;
    const rows = Array.isArray(r.logs) ? r.logs : [];
    logState.items = more ? (logState.items || []).concat(rows) : rows;
    logState.hasMore = !!r.has_more;
    if (rows.length) logState.beforeId = rows[rows.length - 1].id;
    logState.inited = true;
    refreshLogCard();
  } catch (e) { /* 网络抖动时保留旧列表 */ }
}

function refreshLogCard() {
  const box = document.getElementById("worldLogCard");
  if (!box) return;
  box.outerHTML = logCardHtml();
  bindTabEvents();
}

function tabWorld() {
  const w = (S && S.npc_world) || {};
  // 兼容旧快照：数组字段缺失/为对象时按空数组处理，数值缺省为 0，
  // 避免渲染出 "undefined" / "NaN" 或直接把整个「天下」页打崩。
  const asList = (v) => (Array.isArray(v) ? v : []);
  const num = (v, d) => {
    const n = Number(v);
    return (v === null || v === undefined || v === "" || isNaN(n)) ? d : n;
  };
  const list = asList(w.factions);
  const REL_CN = {
    war: "交战", war_pending: "备战", truce: "停战",
    ally: "同盟", neutral: "中立",
  };
  const TIER_CN = {
    calm: "无感", alert: "警觉", prepare: "戒备", hostile: "敌视", coalition: "公敌",
  };
  const tiers = (w.tiers && typeof w.tiers === "object" && !Array.isArray(w.tiers))
    ? w.tiers : {};
  const tierTxt = Object.entries(tiers)
    .map(([k, v]) => esc((TIER_CN[k] || k) + "≥" + num(v, 0))).join(" / ");
  let h = '<div class="card"><h3>🌐 天下大势</h3>' +
    '<div class="sub">威胁榜按「最敌视该势力的观察者」统计；宣战线 ' +
    esc(String(num(w.declare_gate, 60))) +
    '，停战线 ' + esc(String(num(w.peace_gate, 60))) +
    (tierTxt ? "。档位：" + tierTxt : "") + "</div>";
  if (!list.length) return h + '<div class="sub">暂无势力情报</div></div>';
  h += '<div class="tbl-wrap"><table class="tbl">' +
    "<tr><th>势力</th><th>威胁</th><th>档位</th><th>占比</th><th>扩张</th><th>军团</th></tr>";
  for (const f of list) {
    const crown = f.hegemon ? " 👑" : "";
    const growth = num(f.growth, 0);
    h += "<tr><td><span class='lg-dot' style='display:inline-block;background:" +
      safeColor(f.color) + "'></span> " + esc(f.name) +
      (f.id === myFactionId() ? "（我）" : "") + crown + "</td><td>" +
      esc(String(num(f.public_threat, 0))) + "</td><td>" + esc(f.tier || "无感") +
      "</td><td>" + esc(String(num(f.share, 0))) + "%</td><td>" +
      (growth >= 0 ? "+" : "") + esc(String(growth)) +
      "%</td><td>" + esc(String(num(f.armies, 0))) + "</td></tr>";
  }
  h += "</table></div></div>";
  const npcs = list.filter((f) => f.is_npc);
  h += '<div class="card"><h3>🏹 NPC 扩张倾向与作战</h3>';
  if (!npcs.length) h += '<div class="sub">暂无 NPC 势力</div>';
  for (const f of npcs) {
    h += '<div style="margin:8px 0;padding-top:6px;border-top:1px solid var(--line-soft)">' +
      "<b>" + esc(f.name) + "</b> " +
      '<span class="sub">' + esc(f.personality_name || f.personality || "") +
      " · 侵略 " + esc(String(num(f.aggression, 0))) +
      " · 侵略倍率 ×" + esc(String(num(f.threat_mult, 1))) +
      (f.hegemon ? " · 👑 霸主" : "") + "</span>";
    const rel = asList(f.relations).slice(0, 4);
    if (rel.length) {
      h += '<div class="sub">对外关系：' + rel.map((r) =>
        esc(r.target_name) + "（" + esc(REL_CN[r.relation] || r.relation) +
        "，敌意 " + esc(String(num(r.h, 0))) + "）").join("；") + "</div>";
    }
    const wars = asList(f.wars);
    if (wars.length) {
      h += '<div class="sub">作战：' + wars.map((x) => {
        let s = esc(x.target_name) + "（" + esc(x.goal_name) +
          "，战况 " + esc(String(num(x.war_score, 0))) +
          "，疲劳 " + esc(String(num(x.exhaustion, 0)));
        if (num(x.frenzy_remaining, 0) > 0) {
          s += "，狂热剩 " + esc(String(num(x.frenzy_remaining, 0))) + " tick";
        }
        if (num(x.ceasefire_remaining, 0) > 0) s += "，停火中";
        return s + "）";
      }).join("；") + "</div>";
    } else {
      h += '<div class="sub">作战：暂无战事</div>';
    }
    h += "</div>";
  }
  h += "</div>";
  h += logCardHtml();
  const windList = asList(w.winds);
  const winds = windList.length ? windList : asList(S && S.winds);
  h += '<div class="card"><h3>🌬️ 风声</h3>';
  if (!winds.length) h += '<div class="sub">近来江湖无事。</div>';
  else h += winds.slice().reverse().map((x) => {
    const t = typeof x === "string" ? x : ((x && x.text) || "");
    const k = (x && x.kind_name) ? "【" + x.kind_name + "】" : "";
    return '<div class="sub" style="padding:2px 0">🌬️ ' + esc(k + t) + "</div>";
  }).join("");
  h += "</div>";
  return h;
}

function tabOverview() {
  const p = S.player || {};
  let h = guideCard();
  // 科技与计略由势力页迁入总览，便于日常查看与操作
  if (p.faction) h += techCard(p.faction);
  h += plotCard(p);
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
    h += '<div class="tbl-wrap"><table class="tbl"><tr><th>角色</th><th>任务</th><th>收益</th><th>地点</th><th>进度</th><th></th></tr>' +
      tasks.map((t) => {
        const where = t.expedition
          ? esc(t.node_name || ("#" + t.node_id)) +
            (t.task_type !== "supply" && t.origin_node_name
              ? ' <span class="sub">（自 ' + esc(t.origin_node_name) + " 出发）</span>"
              : "")
          : esc(t.node_name || ("#" + t.node_id));
        let progress;
        if (t.sustained) {
          const now = srvNowSec();
          const next = taskSettleAt(t);
          const left = Math.max(0, next - now);
          const startedAt = Number(t.started_at || 0);
          const elapsedH = startedAt > 0
            ? Math.max(0, Math.floor((now - startedAt) / 3600))
            : Number(t.settled_hours || 0);
          progress = "已执行 " + elapsedH + " 小时 · 下次结算 " +
            '<span class="aucLeft" data-expire="' + next +
            '" data-settle="1" data-pending="结算中…">' +
            (left > 0 ? fmtRemain(left) : "结算中…") + "</span>";
        } else if (t.expedition) {
          progress = "剩余 " + taskCountdownHtml(t) +
            (t.task_type === "supply"
              ? ' <span class="sub">· 召回时未送达粮草退回</span>'
              : ' <span class="sub">· 中途停止无收益</span>');
        } else {
          progress = "剩余 " + taskCountdownHtml(t);
        }
        // 运粮队走 recall_task 会退回未送达粮草，不能按「无收益」提示
        const supply = t.task_type === "supply";
        const label = t.sustained ? "停止任务"
          : (supply ? "召回补粮队" : (t.expedition ? "停止（无收益）" : "召回"));
        const kind = t.sustained ? "sustained"
          : (supply ? "supply" : (t.expedition ? "expedition" : "normal"));
        return "<tr><td>" + esc(t.char_name) + "</td><td>" + esc(t.task_name) +
          "</td><td>" + taskPayoutText(t) + "</td><td>" + where +
          "</td><td>" + progress + '</td><td><button class="btn small" data-act="recall-task" data-id="' +
          t.id + '" data-kind="' + kind + '" data-name="' + esc(t.char_name) + '">' +
          label + "</button></td></tr>";
      }).join("") +
      '</table></div><div class="sub">持续任务每小时整点结算，可随时停止（未满 1 小时按比例结算）；侦察/劫掠须到期才结算，中途停止无收益；运粮队召回退回未送达粮草；远征任务落点始终是出发地。</div>';
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
  const atArena = charAtArena(c);
  const canDuel = duelEligible(c);
  const locNode = nodeById(Number(c.location_node_id || 0));
  const trapped = nodeSieged(locNode);
  const traitHtml = traitChips(c.traits, c.trait_names);
  const pTop = c.personality_top_name
    ? personalityChipHtml("top", (c.personality || {}).top,
      c.personality_top_name, c.personality_top_desc) : "";
  const pNeg = c.personality_neg_name
    ? personalityChipHtml("negative", (c.personality || {}).negative,
      c.personality_neg_name, c.personality_neg_desc) : "";
  return '<div class="card"><h3>' + esc(c.name) +
    ' <span class="tag ' + (["fine", "rare", "epic", "legend"].includes(c.rarity) ? c.rarity : "") + '">' + esc(c.rarity_name) + "</span>" +
    ' <span class="tag">' + esc(c.role_name) + "</span><span class='tag'>" + esc(c.profession_name) + "</span>" +
    " Lv" + c.level + "</h3>" +
    '<div class="sub">' + statGrid(c.stats) + "</div>" +
    (traitHtml ? '<div class="sub trait-line">特技：' + traitHtml + "</div>" : "") +
    (pTop ? '<div class="sub trait-line">🌟 个性：' + pTop + "</div>" : "") +
    (pNeg ? '<div class="sub trait-line">🔻 负面：' + pNeg + "</div>" : "") +
    (apt ? '<div class="sub">适性：' + esc(apt) + "</div>" : "") +
    (bonds ? '<div class="sub">羁绊：' + bonds + "</div>" : "") +
    '<div class="sub">阅历 ' + c.exp + "/" + c.exp_next +
    " · 成长 " + (c.stat_total || 0) + "/" + (c.stat_cap || 0) +
    " · 忠诚 " + c.loyalty +
    " · 状态 " + esc(charStatusText(c)) +
    (c.resident ? "（驻守）" : "") + "</div>" +
    (c.can_reward
      ? '<div class="drawer-actions"><button class="btn small" data-act="reward-char" data-id="' +
        c.id + '" data-name="' + esc(c.name) + '">🎁 奖赏（+' +
        Number(c.reward_gain || 5) + " 忠诚 / " + fmtNum(Number(c.reward_cost || 0)) +
        " 币）</button></div>"
      : "") +
    '<div class="char-loc">📍 所在地：' + esc(charLocText(c)) +
    (c.can_move && !trapped ? ' <span class="tag">可传送</span>' : "") +
    (trapped && c.status === "idle"
      ? ' <span class="warn-text">围城中，无法传送离开</span>' : "") + "</div>" +
    (c.appointed_position
      ? '<div class="sub">🏛 官职：' + esc(c.appointed_name || positionName(c.appointed_position)) + "</div>"
      : "") +
    (c.governor_node_name
      ? '<div class="sub">🛡 太守：' + esc(c.governor_node_name) +
        (charAtNode(c, c.governor_node_id) ? "（驻守本城）" : "（离城·加成暂停）") + "</div>"
      : "") +
    '<div class="sub">伤势 ' + esc(c.injury_name) + "(" + c.injury + ") · 体力 " + c.stamina + "</div>" +
    '<div class="bar ' + (c.injury >= 60 ? "bad" : c.injury >= 30 ? "warn" : "") + '"><i style="width:' + (100 - c.injury) + '%"></i></div>' +
    (c.status === "idle"
      ? '<div class="drawer-actions">' +
        (c.can_move
          ? '<button class="btn small" data-act="move-open" data-id="' + c.id +
            '" data-name="' + esc(c.name) + '">🚶 传送</button>'
          : "") +
        (canDuel && atArena
          ? '<button class="btn small" data-act="duel-open" data-id="' + c.id + '">🎯 摆擂（单挑赌注）</button>'
          : "") +
        (!c.appointed_position && !c.governor_node_id
          ? '<button class="btn small danger" data-act="dismiss-char" data-id="' + c.id +
            '" data-name="' + esc(c.name) + '">🗑 遣散（腾出名额）</button>'
          : "") +
        "</div>" +
        (canDuel && !atArena
          ? '<div class="sub">🎯 摆擂需先前往建有擂将台的己方/联盟成员势力据点。</div>'
          : "") +
        (!canDuel && c.role === "general"
          ? '<div class="sub">🎯 摆擂/应战需武勇 ≥75 且无伤。</div>'
          : "")
      : "") +
    "</div>";
}

function loyaltyRewardCard(p, chars) {
  const lr = p.loyalty_reward || {};
  const cost = Number(lr.cost_coin || 0);
  const gain = Number(lr.gain || 5);
  const cap = Number(lr.cap || 100);
  const eligible = (chars || []).filter((c) => c.can_reward);
  const total = cost * eligible.length;
  const limit = Number(lr.daily_limit || 0);
  const used = Number(lr.daily_used || 0);
  const leftText = limit > 0 ? Math.max(0, limit - used) + " / " + limit : "不限";
  const natural = Number(lr.natural_gain_per_tick || 0);
  const naturalCap = Number(lr.natural_cap || 0);
  const apptBonus = Number(lr.appointed_bonus_per_tick || 0);
  const apptCap = Number(lr.appointed_natural_cap || naturalCap);
  return '<div class="card"><h3>🎁 忠诚奖赏</h3>' +
    '<div class="sub">每次消耗 <b>' + fmtNum(cost) + '</b> 铜钱，忠诚 <b>+' + gain +
    '</b>（上限 ' + cap + '）。无官职角色每 tick 自然 +' + natural +
    '（上限 ' + naturalCap + '）；有官职（含太守）额外 +' + apptBonus +
    '（上限 ' + apptCap + '）。</div>' +
    '<div class="sub">今日次数 ' + leftText + ' · 待奖赏 ' + eligible.length +
    ' 人 · 一键预计 ' + fmtNum(total) + ' 铜钱</div>' +
    '<div class="drawer-actions"><button class="btn small primary" data-act="reward-all"' +
    (eligible.length ? "" : " disabled") + '>🎁 一键奖赏（' + eligible.length +
    ' 人）</button></div>' +
    '<div class="sub">忠诚低于 30 会在下一 tick 出走；奖赏可把忠诚拉回安全线，也可用于冲 100 上限。</div></div>';
}

function tabChars() {
  const p = S.player || {};
  const chars = (p.chars || []).filter((c) => !["dead", "captive"].includes(c.status));
  const dead = (p.chars || []).filter((c) => c.status === "dead");
  let h = '<div class="card"><h3>👥 名册 ' + (p.char_count || 0) + " / " + (p.char_cap || 0) + "</h3>" +
    '<div class="sub">按本势力据点数量提供名额：当前 ' + (p.char_cap_nodes || 0) +
    " 座据点，每座 +" + (p.char_cap_per_node || 0) + " 名；被俘/押运中/陨落角色不占名额。角色的六维总和可通过任务与战斗历练，最高成长到稀有度上限 +" +
    (p.growth_cap_bonus || 0) + "，满额后可遣散腾出名额。</div>" +
    '<div class="sub">📍 角色卡显示所在地；空闲角色可「传送到」己方/联盟成员势力据点。任务与编军只能使用位于该据点的角色。</div></div>';
  h += loyaltyRewardCard(p, chars);
  if (!chars.length && !dead.length) h += '<div class="empty">尚无角色，去酒馆招募吧</div>';
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
      "🪦 " + esc(c.name) + "（" + esc(c.rarity_name) + " " + esc(c.role_name) + "）").join("<br>") +
      "</div></div>";
  }
  // 官职任免与俘虏事务同属「人员」管理（原势力页迁入）
  if (p.faction) h += appointmentCard(p.faction);
  h += captivesCard(p);
  return h;
}

// 角色官职俸禄三态（v4.6.7）：
//   paid    上一次发薪已按当前官职发放
//   pending 任免发生在上一次发薪之后，等下一 tick 发薪（不是欠俸）
//   unpaid  上一次发薪因势力主钱包不足而停发
// 旧快照没有 salary_state 时按 salary_paid 回退，保持兼容。
function salaryStateOf(obj) {
  const st = String((obj && obj.salary_state) || "");
  if (st) return st;
  return obj && obj.salary_paid ? "paid" : "unpaid";
}

function salaryStateText(state, shortText) {
  if (state === "pending") return '<span class="sub">待发薪·下个 tick 生效</span>';
  if (state === "unpaid") return '<span class="warn-text">' + shortText + "</span>";
  return "";
}

function armyRosterText(a) {
  const ids = (a.character_ids || []).map(Number);
  const names = a.characters || [];
  const leaderId = Number(a.leader_id || 0);
  const leaderName = a.leader_name ||
    (ids.length && Number(ids[0]) === leaderId ? (names[0] || "") : "");
  const members = ids.map((id, i) => ({ id: id, name: names[i] || ("#" + id) }))
    .filter((x) => x.id !== leaderId);
  const payState = a.leader_position_state ||
    (a.leader_position_paid === false ? "unpaid" : "paid");
  const payNote = payState === "pending" ? "·待发薪，下个 tick 生效"
    : (payState === "unpaid" ? "·俸禄未发，上限暂停" : "");
  let s = "主将：" + (leaderName
    ? esc(leaderName) + (a.leader_position_name
      ? "（" + esc(a.leader_position_name) +
        (a.leader_position ? payNote : "") + "）"
      : "")
    : "无可用主将");
  if (members.length) s += " · 随行：" + members.map((x) => esc(x.name)).join("、");
  return s;
}

function tabArmies() {
  const p = S.player || {};
  let h = '<div class="card"><h3>🛡️ 我的军团</h3><div class="sub">带兵上限按当前主将的官职 / 据点太守 / 兵营实时计算（无官职 1500 ~ 势力主·太尉 3000）；换将后立即按新主将刷新。出征沿通路行军，粮尽则士气崩溃。</div></div>';
  const armies = p.armies || [];
  if (!armies.length) return h + '<div class="empty">尚无军团：在己方/联盟成员势力据点抽屉中「在此编军」</div>';
  for (const a of armies) {
    const cap = Number(a.troop_cap || 0);
    const overCap = cap > 0 && Number(a.troops || 0) > cap;
    const capText = cap > 0
      ? " / 上限 " + fmtNum(cap) + (overCap ? " ⚠超编" : "")
      : "（无可用主将）";
    h += '<div class="card"><h3>' + esc(a.name) + ' <span class="tag">' + esc(a.troop_name) + "</span>" +
      (a.siege_engine ? ' <span class="tag epic">破城槌</span>' : "") + "</h3>" +
      '<div class="sub">兵力 ' + fmtNum(a.troops) + capText + " · 士气 " + a.morale + " · 精锐 " +
      a.veteran + "（" + vetTierName(a.veteran) + "） · 携粮 " + fmtNum(a.grain) +
      (a.overstaffed ? " · <span class=\"warn-text\">统率超编（阈值 " + fmtNum(a.overstaff_limit) + "）</span>" : "") + "</div>" +
      '<div class="sub">' + armyRosterText(a) + "</div>" +
      '<div class="sub">驻地：' + esc(a.node_name || ("#" + a.node_id)) +
      " · 状态：" + esc(STATE_NAME[a.state] || a.state) +
      (a.path && a.path.length ? " → " + a.path.map(esc).join(" → ") : "") +
      (a.on_road ? " · 🛣 友方道路（加速 / 免粮）" : "") + "</div>" +
      '<div class="drawer-actions">' + armyButtons(a) + "</div></div>";
  }
  return h;
}

function armyButtons(a) {
  let b = "";
  if (a.state === "idle") {
    b += '<button class="btn small primary" data-act="march-pick" data-id="' + a.id + '">⚔️ 选择目标出征</button>';
    const at = nodeById(a.node_id);
    const atOwn = !!(at && at.owner && Number(at.owner.id) === myFactionId());
    if (atOwn) {
      b += '<button class="btn small" onclick="refillPrompt(' + a.id + ')">➕ 补员</button>';
    }
    b += '<button class="btn small" onclick="openHandoverModal(' + a.id + ')">🔄 换将</button>';
    b += '<button class="btn small" onclick="openSupplyModal(' + a.id + ')">🚚 补粮</button>';
    b += '<button class="btn small" onclick="doAction(\'drill_army\',{army_id:' + a.id + '})">🎯 操练(500币)</button>';
    if (atOwn) {
      b += '<button class="btn small danger" onclick="doAction(\'disband_army\',{army_id:' + a.id + '})">解散</button>';
    } else {
      b += '<span class="sub">（驻扎联盟成员势力/外地据点：补员、解散需回到己方据点）</span>';
    }
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

function nearestFriendlyNodeId(fromId) {
  // 与后端 _nearest_friendly_node(dwell_only=True) 同口径：按据点 id 升序遍历，
  // 取最短路径；己方与盟友据点均可作为补粮起点，只保留可停留据点。
  const own = friendlyNodes().filter(canDwellAt)
    .slice().sort((a, b) => Number(a.id) - Number(b.id));
  if (!own.length) return 0;
  const start = Number(fromId || 0);
  const dist = {};
  if (start) {
    dist[start] = 0;
    const q = [start];
    while (q.length) {
      const cur = q.shift();
      for (const nxt of ((nodeById(cur) || {}).adj || [])) {
        const id = Number(nxt);
        if (dist[id] !== undefined) continue;
        dist[id] = dist[cur] + 1;
        q.push(id);
      }
    }
  }
  // 与后端 _nearest_friendly_node 同口径：最短路径优先，同距离取据点 id 较小者
  let best = 0, bestD = Infinity;
  for (const n of own) {
    const d = dist[n.id];
    if (d !== undefined && d < bestD) { best = Number(n.id); bestD = d; }
  }
  return best || Number(own[0].id);
}

let supplyArmyId = 0;

function openSupplyModal(armyId) {
  const p = S.player || {};
  const army = (p.armies || []).find((a) => a.id === armyId);
  if (!army) return;
  const at = nodeById(army.node_id);
  const originFriendly = !!(at && at.owner && factionIsFriendly(at.owner.id) &&
    canDwellAt(at));
  const instant = army.state === "idle" && originFriendly;
  // 后端运粮起点：军团当前据点是己方/盟友就原地出发，否则取最近友方据点。
  // 此处必须同口径计算，否则下拉会列出异地角色，提交后才报“需先传送”。
  const originId = originFriendly ? Number(army.node_id) : nearestFriendlyNodeId(army.node_id);
  const originNode = nodeById(originId);
  const idle = originId ? charsAtNode(p, originId) : [];
  // v4.6.7：驻扎在己方/盟友据点时由军团就地补粮，无需空闲角色押运
  if (!instant && !idle.length) {
    toast(originNode
      ? "补粮起点「" + originNode.name + "」无空闲角色，请先传送"
      : "没有可用己方/盟友据点作为补粮起点", "err");
    return;
  }
  supplyArmyId = armyId;
  const charRow = document.getElementById("supplyCharRow");
  if (charRow) charRow.hidden = instant;
  document.getElementById("supplyArmyHint").textContent =
    "目标：" + army.name + "（" + (STATE_NAME[army.state] || army.state) + " · " +
    army.node_name + " · 携粮 " + fmtNum(army.grain) + "）" +
    (instant
      ? "，驻扎在己方/盟友据点，直接从据点补粮、立即到账（无需押运角色）"
      : "，补粮队速度高于普通行军，追上后交付（角色占用至交付或超时）") +
    (instant ? "" : "；出发地：" + (originNode ? originNode.name : "最近友方据点") +
      "，押运角色须位于出发地。");
  document.getElementById("supplyChar").innerHTML = idle.map((c) =>
    '<option value="' + c.id + '">' + esc(c.name) + "（" + esc(c.role_name) + "·" +
    esc(charLocText(c)) + "）</option>").join("");
  const hours = (S.meta.task_hours || [1, 2, 4, 8]);
  document.getElementById("supplyHours").innerHTML = hours.map((h) =>
    '<option value="' + h + '">' + h + "小时</option>").join("");
  document.getElementById("supplyCost").textContent =
    "运粮量 = 军团单 tick 粮耗 × 所选时长对应 tick（受携粮上限：统率×50，粮仓 +10%/级）；" +
    (instant
      ? "驻扎军团直接由据点入库，立即到账，不消耗角色体力。"
      : "「折算时长」只决定运粮量，行军/围城军团由补粮队追上即交付，不代表角色占用时长。");
  const confirmBtn = document.getElementById("supplyConfirm");
  if (confirmBtn) confirmBtn.textContent = instant ? "立即补粮" : "出发";
  document.getElementById("supplyOverlay").hidden = false;
}

document.getElementById("supplyCancel").addEventListener("click", () => {
  document.getElementById("supplyOverlay").hidden = true;
});
document.getElementById("supplyConfirm").addEventListener("click", () => {
  const army = (S.player.armies || []).find((a) => a.id === supplyArmyId);
  const at = army ? nodeById(army.node_id) : null;
  const instant = !!(army && army.state === "idle" && at && at.owner &&
    factionIsFriendly(at.owner.id) && canDwellAt(at));
  doAction("dispatch_task", {
    // 驻扎友方据点：后端就地补粮，无需押运角色（char_id=0）
    char_id: instant ? 0 : Number(document.getElementById("supplyChar").value || 0),
    node_id: army ? army.node_id : 0,
    task_type: "supply",
    hours: Number(document.getElementById("supplyHours").value),
    army_id: supplyArmyId,
  }).then(() => { document.getElementById("supplyOverlay").hidden = true; });
});

function refillPrompt(armyId) {
  const army = ((S.player || {}).armies || []).find((a) => Number(a.id) === Number(armyId)) || {};
  const cap = Number(army.troop_cap || 0);
  const cur = Number(army.troops || 0);
  const room = cap > 0 ? Math.max(0, cap - cur) : 0;
  const hint = cap > 0
    ? "补员数量（100 的倍数，需己方据点；当前 " + fmtNum(cur) + " / 上限 " +
      fmtNum(cap) + "，最多可补 " + fmtNum(room) + "）："
    : "补员数量（100 的倍数，需己方据点）：";
  if (room <= 0 && cap > 0) {
    toast("「" + (army.name || "军团") + "」已达带兵上限 " + fmtNum(cap) +
      "，换将/任官/建兵营可提升上限", "err");
    return;
  }
  const def = room >= 100 ? String(Math.min(100, Math.floor(room / 100) * 100)) : "100";
  const n = prompt(hint, def);
  if (n) doAction("refill_army", { army_id: armyId, troops: Number(n) });
}

// ---------------------------------------------------------------------------
// 军团换将交接：空闲武将可替换主将/随行；现役成员也可互相改任
// ---------------------------------------------------------------------------
let handoverArmyId = 0;
let handoverPool = [];
let handoverRoster = new Set();
let handoverCurrentLeader = 0;

function handoverLevelLabel(c) {
  if (Number(c.id) === Number(handoverCurrentLeader)) return "现任主将";
  if (handoverRoster.has(Number(c.id))) return "随行中";
  return "空闲·" + (c.location || "驻地");
}

function handoverMemberOptions(leaderId, selected) {
  return handoverPool
    .filter((c) => Number(c.id) !== Number(leaderId) && handoverCanFollow(c))
    .map((c) => {
      const id = Number(c.id);
      return '<option value="' + id + '"' + (selected.has(id) ? " selected" : "") + ">" +
        esc(c.name) + "（" + esc(c.role_name) + "·" + esc(handoverLevelLabel(c)) + "）</option>";
    }).join("");
}

function handoverAlive(c) {
  return c && c.status !== "dead" && c.status !== "captive" &&
    c.injury_state !== "dying" && c.injury_state !== "dead";
}

function handoverCanFollow(c) {
  // 随行仍需能行动：健康/轻伤；主将允许带伤或内政官
  return handoverAlive(c) &&
    (c.injury_state === "healthy" || c.injury_state === "light");
}

let handoverCapSeq = 0;
let handoverCapTimer = null;

function handoverArmyById() {
  return ((S.player || {}).armies || []).find(
    (x) => Number(x.id) === Number(handoverArmyId));
}

function renderHandoverCost(cap, label, count) {
  const army = handoverArmyById();
  const el = document.getElementById("handoverCost");
  if (!army || !el) return;
  const over = Number(army.troops || 0) > Number(cap || 0);
  el.textContent =
    "当前兵力 " + fmtNum(army.troops) + " · 携粮 " + fmtNum(army.grain) +
    "；新主将 " + (label ? label : "") + "带兵上限 " + fmtNum(cap) +
    (over ? " ⚠（当前兵力超出该主将实际上限，交接会被拒绝）" : "") +
    "；军团最多 3 人（当前 " + count + " 人）。交接后换下的角色回归空闲并留在驻地。";
}

// 先本地给出官职基础上限（即时反馈），再向后端要含太守/兵营/欠俸的实际上限。
function updateHandoverCost() {
  const army = handoverArmyById();
  const leaderSel = document.getElementById("handoverLeader");
  const memberSel = document.getElementById("handoverMembers");
  if (!army || !leaderSel || !memberSel) return;
  const count = memberSel.selectedOptions.length + 1;
  const leader = handoverPool.find(
    (c) => Number(c.id) === Number(leaderSel.value)) || {};
  const pos = String(leader.appointed_position || "none") || "none";
  const baseCap = Number((((S.meta || {}).positions || {})[pos] || {}).cap || 1500);
  renderHandoverCost(baseCap, "「" + (leader.name || "?") + "」官职基础", count);
  const seq = ++handoverCapSeq;
  if (handoverCapTimer) clearTimeout(handoverCapTimer);
  handoverCapTimer = setTimeout(() => refreshHandoverCap(seq), 180);
}

async function refreshHandoverCap(seq) {
  const army = handoverArmyById();
  const leaderSel = document.getElementById("handoverLeader");
  const memberSel = document.getElementById("handoverMembers");
  if (!army || !leaderSel || !memberSel) return;
  const leaderId = Number(leaderSel.value || 0);
  if (!leaderId) return;
  try {
    const r = await api("/api/strategy/action", {
      method: "POST",
      body: JSON.stringify({ action: "army_cap_preview",
        params: { army_id: army.id, leader_char_id: leaderId } }),
    }, true);
    if (seq !== handoverCapSeq) return;
    const overlay = document.getElementById("handoverOverlay");
    if (!overlay || overlay.hidden || !r || !r.ok) return;
    const count = memberSel.selectedOptions.length + 1;
    const payState = r.position_state ||
      (r.position_paid === false ? "unpaid" : "paid");
    const payNote = payState === "pending" ? "·待发薪"
      : (payState === "unpaid" ? "·俸禄未发" : "");
    const label = "「" + (r.leader_name || "新主将") + "」（" +
      (r.position_name || "无官职") + payNote + "）实际";
    renderHandoverCost(r.troop_cap, label, count);
  } catch (e) {
    // 预览失败保留官职基础估算；正式交接仍由后端硬校验
  }
}

function openHandoverModal(armyId) {
  const p = S.player || {};
  const army = (p.armies || []).find((x) => Number(x.id) === Number(armyId));
  if (!army) {
    toast("只能对自己所属的驻守军团换将", "err");
    return;
  }
  if (army.state !== "idle") {
    toast("军团行动期间无法换将，请先驻守/撤退", "err");
    return;
  }
  const nodeId = Number(army.node_id || 0);
  const nodeName = army.node_name || nodeNameById(nodeId) || ("#" + nodeId);
  const roster = new Set((army.character_ids || []).map(Number));
  const byId = new Map(((p.chars || [])).map((c) => [Number(c.id), c]));
  const current = (army.character_ids || []).map(
    (id) => byId.get(Number(id))).filter(Boolean);
  const idleHere = idleAliveChars(p).filter(
    (c) => charAtNode(c, nodeId) && !roster.has(Number(c.id)) && handoverAlive(c));
  // 现役成员（status=army）也可改任主将/随行；外部候选必须是本地空闲角色
  handoverPool = current.filter(handoverAlive).concat(idleHere);
  // 主将允许内政官与带伤角色；随行在 handoverMemberOptions 内单独筛
  const leaders = handoverPool;
  if (!leaders.length) {
    toast("「" + nodeName + "」暂无可用角色，请先传送空闲角色至此", "err");
    return;
  }
  handoverArmyId = Number(army.id);
  handoverRoster = roster;
  // 主将可能因陨落/被俘顺延，优先用后端给出的名义主将 id
  handoverCurrentLeader = Number(
    army.leader_id || (army.character_ids || [])[0] || 0);
  const curLeaderId = handoverCurrentLeader;
  const selectedLeader = leaders.find((c) => Number(c.id) === curLeaderId) || leaders[0];
  const memberSel = document.getElementById("handoverMembers");
  const defaultMembers = new Set(
    current.slice(1).filter(handoverCanFollow)
      .map((c) => Number(c.id)).filter((id) => id !== Number(selectedLeader.id)));
  memberSel.innerHTML = handoverMemberOptions(selectedLeader.id, defaultMembers);
  document.getElementById("handoverLeader").innerHTML = leaders.map((c) =>
    '<option value="' + c.id + '"' +
    (Number(c.id) === Number(selectedLeader.id) ? " selected" : "") + ">" +
    esc(c.name) + "（" + esc(c.role_name) + " 武勇" +
    ((c.stats || {}).valor || 0) +
    (c.injury_state && c.injury_state !== "healthy"
      ? "·" + esc(c.injury_name || c.injury_state) : "") +
    "·" + esc(handoverLevelLabel(c)) + "）</option>").join("");
  document.getElementById("handoverHint").textContent =
    "军团：" + army.name + "（" + fmtNum(army.troops) + " 兵 · " +
    (STATE_NAME[army.state] || army.state) + "） · 驻地：" + nodeName +
    "。仅列出驻地空闲角色与现役成员；主将可为内政官或重伤角色，随行仍须能行动（健康/轻伤），" +
    "最多 2 人；新主将实际带兵上限（含官职俸禄/太守/兵营）须不低于当前兵力，否则无法交接。" +
    "重伤主将出征/操练需至少 1 名健康或轻伤随行。";
  updateHandoverCost();
  document.getElementById("handoverOverlay").hidden = false;
}

document.getElementById("handoverLeader").addEventListener("change", () => {
  const leaderId = Number(document.getElementById("handoverLeader").value);
  const sel = new Set(Array.from(
    document.getElementById("handoverMembers").selectedOptions).map((o) => Number(o.value)));
  document.getElementById("handoverMembers").innerHTML =
    handoverMemberOptions(leaderId, sel);
  updateHandoverCost();
});
document.getElementById("handoverMembers").addEventListener("change", updateHandoverCost);
document.getElementById("handoverCancel").addEventListener("click", () => {
  document.getElementById("handoverOverlay").hidden = true;
});
document.getElementById("handoverConfirm").addEventListener("click", () => {
  const leaderId = Number(document.getElementById("handoverLeader").value);
  if (!leaderId) { toast("请选择新主将", "err"); return; }
  const members = Array.from(
    document.getElementById("handoverMembers").selectedOptions)
    .map((o) => Number(o.value)).filter((id) => id !== leaderId);
  doAction("handover_army", {
    army_id: handoverArmyId,
    leader_char_id: leaderId,
    member_ids: members,
  }).then((r) => {
    if (r && r.ok !== false) document.getElementById("handoverOverlay").hidden = true;
  });
});

// ---------------------------------------------------------------------------
// 角色传送弹窗（§3：免费瞬移，仅己方/联盟成员势力据点；围城据点可进不可出）
// ---------------------------------------------------------------------------
let moveCharId = 0;

function openMoveModal(charId, charName) {
  const c = ((S.player || {}).chars || []).find(
    (x) => Number(x.id) === Number(charId));
  if (!c) return;
  if (!c.can_move) {
    toast((c.name || charName || "该角色") + " 当前状态不可传送", "err");
    return;
  }
  const cur = nodeById(Number(c.location_node_id || 0));
  if (nodeSieged(cur)) {
    toast("所在据点正在被围城，无法传送离开；请派军团出击解围", "err");
    return;
  }
  // 排除当前所在地：选择「原地传送」没有意义，且易被误认为换防成功
  const targets = moveTargets().filter(
    (n) => Number(n.id) !== Number(c.location_node_id));
  if (!targets.length) {
    toast("暂无其他可传送的己方/联盟成员势力据点", "err");
    return;
  }
  moveCharId = Number(c.id);
  document.getElementById("moveCharHint").textContent =
    "角色：" + (c.name || charName || "") + "（当前：" + charLocText(c) + "）";
  document.getElementById("moveNodeSel").innerHTML = targets.map((n) =>
    '<option value="' + n.id + '"' +
    (Number(n.id) === Number(c.location_node_id) ? " selected" : "") + ">" +
    esc(n.name) + "（" + esc(TYPE_NAME[n.type] || n.type) + " · Lv" + n.level + "）</option>").join("");
  document.getElementById("moveOverlay").hidden = false;
}

document.getElementById("moveCancel").addEventListener("click", () => {
  document.getElementById("moveOverlay").hidden = true;
});
document.getElementById("moveConfirm").addEventListener("click", () => {
  const sel = document.getElementById("moveNodeSel");
  const nodeId = Number((sel || {}).value || 0);
  if (!moveCharId || !nodeId) { toast("请选择目标据点", "err"); return; }
  doAction("move_char", { char_id: moveCharId, node_id: nodeId }).then(() => {
    document.getElementById("moveOverlay").hidden = true;
  });
});

function tabTavern() {
  const p = S.player || {};
  const t = p.tavern || {};
  const rc = (S.meta || {}).recruit_cfg || {};
  const coinCosts = Object.assign(
    { common: 400, fine: 800, rare: 1600 }, rc.coin_cost_by_rarity || {});
  const tokenCosts = Object.assign(
    { epic: 6, legend: 25 }, rc.token_cost_by_rarity || {});
  const tokenOnly = new Set(rc.token_only_rarities || ["epic", "legend"]);
  const manualCost = Number(rc.manual_refresh_coin != null ? rc.manual_refresh_coin : 300);
  const manualLimit = Number(rc.manual_refresh_daily != null ? rc.manual_refresh_daily : 3);
  const refreshHours = Number(rc.refresh_hours != null ? rc.refresh_hours : 4);
  let h = '<div class="card"><h3>🍶 招贤酒馆</h3><div class="sub">名册 ' +
    (p.char_count || 0) + " / " + (p.char_cap || 0) + "（据点 " +
    (p.char_cap_nodes || 0) + " 座 × 每座 +" + (p.char_cap_per_node || 0) +
    "）；每 " + refreshHours + " 小时刷 5 名候选。凡品/良品/珍品用铜钱招募，极品/传世仅可用募将令招募。</div>" +
    '<div class="drawer-actions"><button class="btn small" id="btnTavernRefresh">🔄 手动刷新(' +
    fmtNum(manualCost) + '币 ' + (t.daily_refresh_used || 0) + "/" + manualLimit +
    ')</button></div></div>';
  const cands = t.candidates || [];
  if (!cands.length) return h + '<div class="empty">酒过三巡，客官请刷新</div>';
  cands.forEach((c, i) => {
    const isToken = tokenOnly.has(c.rarity);
    const coinCost = Number(coinCosts[c.rarity] || 0);
    const tokenCost = Number(tokenCosts[c.rarity] || 0);
    const heldToken = Number(p.recruit_token || 0);
    let action = "";
    if (isToken) {
      const lack = heldToken < tokenCost;
      action = '<button class="btn small primary" data-act="recruit-premium" data-i="' +
        i + '">募将令招募（' + fmtNum(tokenCost) + ' 枚 / 持有 ' +
        fmtNum(heldToken) + '）</button>' +
        (lack ? '<span class="sub">募将令不足</span>' : "");
    } else {
      const lack = Number(p.coin || 0) < coinCost;
      action = '<button class="btn small" data-act="recruit" data-i="' + i +
        '">招募（' + fmtNum(coinCost) + ' 币）</button>' +
        (lack ? '<span class="sub">铜钱不足</span>' : "");
    }
    const candTraits = traitChips(c.traits, c.trait_names);
    const candTop = personalityChipHtml("top",
      (c.personality || {}).top, "", "");
    const candNeg = personalityChipHtml("negative",
      (c.personality || {}).negative, "", "");
    h += '<div class="card"><h3>' + esc(c.name) + ' <span class="tag ' + c.rarity + '">' +
      ((S.meta.rarity_names || {})[c.rarity] || c.rarity) + "</span><span class='tag'>" +
      ((S.meta.role_names || {})[c.role] || c.role) + "</span><span class='tag'>" +
      ((S.meta.profession_names || {})[c.profession] || c.profession) + "</span></h3>" +
      '<div class="sub">' + statGrid(c.stats || {}) + "</div>" +
      (candTraits ? '<div class="sub trait-line">特技：' + candTraits + "</div>" : "") +
      ((candTop || candNeg)
        ? '<div class="sub trait-line">个性：' + candTop + candNeg + "</div>" : "") +
      '<div class="drawer-actions">' + action + "</div></div>";
  });
  return h;
}

function tabBattles() {
  const all = S.battles || [];
  if (!all.length) return '<div class="empty">尚无战报</div>';
  // §12.6 高光战报置顶（同组内保持后端原顺序：新→旧）
  const bs = all.map((b) => ({
    b, hl: battleHighlights(b),
    live: b.mode === "manual" && ["deploy", "battle"].includes(b.state),
  })).sort((x, y) => {
    // 进行中的手操战役永远置顶：高光排序只能影响已结束的战报。
    if (x.live !== y.live) return x.live ? -1 : 1;
    return (y.hl.length ? 1 : 0) - (x.hl.length ? 1 : 0);
  });
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
      const left = Math.max(0, Number(b.deadline_at || 0) - srvNowSec());
      return '<div class="card live"><h3>🔴 ' +
        ({ field: "会战", field_encounter: "野战", street: "巷战", siege: "攻城战" }[b.kind] || "战役") +
        " · " + esc(node ? node.name : "据点#" + b.node_id) +
        ' <span class="tag epic">' + (b.state === "deploy" ? "部署中" : "手操进行中") + "</span>" + hlTag + "</h3>" +
        '<div class="sub">' + (b.state === "deploy" ? "部署剩余 " + left + " 秒" : "回合限时 " + left + " 秒") + "</div>" +
        hlLine +
        '<div class="drawer-actions"><button class="btn small primary" data-act="enter-battle" data-id="' + b.id + '">⚔️ 进入手操</button>' +
        '<button class="btn small" data-act="spectate-battle" data-id="' + b.id + '">👁 观战（只读）</button></div></div>';
    }
    const win = b.winner === "attacker" ? "攻方胜" : b.winner === "defender" ? "守方胜" : "未分胜负";
    return '<div class="card' + (hl.length ? " highlight" : "") + '"><h3>' +
      ({ field: "⚔️", field_encounter: "⚔️", street: "🏙️", siege: "🔥" }[b.kind] || "⚔️") +
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

function factionIsFriendly(fid) {
  const myFid = myFactionId();
  if (Number(fid) === Number(myFid)) return true;
  const me = (S.factions || []).find((f) => f.id === myFid) || {};
  const aid = Number(me.alliance_id || 0);
  const f = (S.factions || []).find((x) => Number(x.id) === Number(fid));
  return !!f && aid > 0 && Number(f.alliance_id || 0) === aid;
}

function myTradeRoutes(routes) {
  return (routes || []).filter((r) => {
    if (r.owner) return factionIsFriendly(r.owner);
    // 兼容旧版视图：无 owner 时按路径节点归属判断
    return (r.path || []).every((nid) => {
      const n = nodeById(nid);
      return n && n.owner && factionIsFriendly(n.owner.id);
    });
  });
}

function tradeRouteBonusSummary(routes) {
  const cfg = S.trade_route_cfg || {};
  const count = routes.length;
  const edges = routes.reduce((s, r) => s +
    Number(r.edge_count != null ? r.edge_count : Math.max(0, (r.path || []).length - 1)), 0);
  const taskBase = Number(cfg.trade_task_bonus != null ? cfg.trade_task_bonus : 0.3);
  const taskPerRoute = Number(cfg.task_bonus_per_route != null ? cfg.task_bonus_per_route : 0.05);
  const taskPerEdge = Number(cfg.task_bonus_per_edge != null ? cfg.task_bonus_per_edge : 0.02);
  const taskMax = Number(cfg.task_bonus_max != null ? cfg.task_bonus_max : 1.0);
  const task = count
    ? Math.max(0, Math.min(taskMax, taskBase + taskPerRoute * count + taskPerEdge * edges))
    : 0;
  const outBase = Number(cfg.node_output_bonus_base != null ? cfg.node_output_bonus_base : 0);
  const outPerRoute = Number(cfg.node_output_bonus_per_route != null ? cfg.node_output_bonus_per_route : 0.02);
  const outPerEdge = Number(cfg.node_output_bonus_per_edge != null ? cfg.node_output_bonus_per_edge : 0.01);
  const outMax = Number(cfg.node_output_bonus_max != null ? cfg.node_output_bonus_max : 0.4);
  const cnt = {}, eds = {};
  for (const r of routes) {
    const path = r.path || [];
    const e = Math.max(0, path.length - 1);
    for (const nid of path) {
      cnt[nid] = (cnt[nid] || 0) + 1;
      eds[nid] = (eds[nid] || 0) + e;
    }
  }
  let outMaxNode = 0;
  for (const nid of Object.keys(cnt)) {
    const v = Math.min(outMax, outBase + outPerRoute * cnt[nid] + outPerEdge * eds[nid]);
    if (v > outMaxNode) outMaxNode = v;
  }
  return { count, edges, task, outMaxNode };
}

function positionName(pos) {
  return ((S.meta.positions || {})[pos || "none"] || {}).name || "无官职";
}

// 势力 = 玩家个人势力（首领称势力主）；联盟 = 多势力结盟后的集体（首领称盟主）。
function positionTitle(pos, inAlliance) {
  if (pos === "leader") return inAlliance ? "盟主" : "势力主";
  return positionName(pos);
}

function factionNameById(fid) {
  const f = ((S || {}).factions || []).find((x) => Number(x.id) === Number(fid));
  return f ? f.name : "势力 #" + fid;
}

// ---------------------------------------------------------------------------
// 总览：势力科技（三线主脉 + 支脉树）与计略派遣
// ---------------------------------------------------------------------------
const TECH_FOLD = {};
let TECH_FOLD_INIT = false;

function parseTechPer(text) {
  const m = String(text || "").match(/^\s*([+-]?\d+(?:\.\d+)?)(.*)$/);
  if (!m) return null;
  return { per: Number(m[1]), unit: m[2] || "" };
}

function fmtTechTotal(perInfo, lv, mode) {
  if (!perInfo) return "";
  let v;
  if (mode === "mul" && perInfo.unit === "%") {
    v = ((1 + perInfo.per / 100) ** Number(lv || 0) - 1) * 100;
  } else {
    v = perInfo.per * Number(lv || 0);
  }
  const sign = v > 0 ? "+" : "";
  const num = Math.abs(v - Math.round(v)) > 0.001
    ? v.toFixed(1) : String(Math.round(v));
  return sign + num + perInfo.unit;
}

function techCurve(cfg, key, fallback) {
  const arr = cfg && Array.isArray(cfg[key]) && cfg[key].length ? cfg[key] : fallback;
  return arr;
}

function techCurveAt(arr, idx) {
  if (!Array.isArray(arr) || !arr.length) return 0;
  return Number(arr[Math.min(Math.max(0, Number(idx) || 0), arr.length - 1)] || 0);
}

function techCard(f) {
  if (!f) return "";
  // 科技按本势力独立结算：职称门禁用 own_position（自身势力名册），
  // 不能用 my_position（联盟共同治理名册，盟主可改动）。
  const techPos = f.own_position != null ? f.own_position : f.my_position;
  const canGovern = ["leader", "deputy", "situ", "taiwei", "jijiu"].includes(techPos);
  // v4.5.7：研究费用直接从个人钱包扣除，这里列出下一级费用与钱包余额。
  const techCfg = S.meta.tech || {};
  const maxTechLv = Math.max(1, Number(techCfg.levels || 7));
  const coinCurve = techCurve(techCfg, "cost_coin", [500, 1000, 2000, 3500, 6000, 8000, 10000]);
  const ironCurve = techCurve(techCfg, "cost_iron", [500, 1000, 2000, 3500, 6000, 8000, 10000]);
  const hourCurve = techCurve(techCfg, "base_hours_per_level", [8, 16, 24, 32, 40, 48, 56]);
  const bCoinCurve = techCurve(techCfg, "branch_cost_coin", [400, 800, 1600, 3200, 6400]);
  const bIronCurve = techCurve(techCfg, "branch_cost_iron", [300, 600, 1200, 2400, 4800]);
  const bHourCurve = techCurve(techCfg, "branch_base_hours_per_level", [4, 8, 12, 18, 24]);
  const tech = f.tech || {};
  const nodeLv = tech.nodes || {};
  const wallet = S.player || {};
  const techPend = tech.pending || null;
  const pendId = techPend ? String(techPend.node_id || "") : "";
  const techNames = { military: "军事", agri: "经营", stratagem: "谋略" };
  const tree = S.meta.tech_tree || {};
  const lines = S.meta.tech_lines || ["military", "agri", "stratagem"];

  // 首次渲染默认展开：有研究中的线 > 主脉最高的线；其余折叠。
  if (!TECH_FOLD_INIT) {
    let bestLine = lines[0];
    let bestLv = -1;
    for (const line of lines) {
      const lv = Number(tech[line] || 0);
      if (lv > bestLv) { bestLv = lv; bestLine = line; }
    }
    const openLine = (techPend && techPend.line) || bestLine;
    for (const line of lines) TECH_FOLD[line] = line === openLine;
    TECH_FOLD_INIT = true;
  }

  let h = '<div class="card tech-card"><h3>🔬 本势力科技</h3>' +
    '<div class="sub">科技点 ' + fmtNum(tech.points || 0) +
    '（研究任务产出；每次开研究可抵扣最多 50% 时长，1 点 = 1 分钟）</div>' +
    '<div class="sub">研究费用从<b>个人钱包</b>扣除（当前 🪙' + fmtNum(wallet.coin || 0) +
    ' · ⛏️' + fmtNum(wallet.iron || 0) + '）；联盟不共享科技，费用由发起研究的玩家承担</div>';
  if (techPend) {
    const pendName = techPend.node_name ||
      techNames[techPend.line] || techPend.line || "?";
    h += '<div class="sub tech-pending-line">⏳ 研究中：' + esc(pendName) + '，剩余 ' +
      fmtRemain(Number(techPend.finish_at || 0) - srvNowSec()) + '</div>';
  }

  for (const line of lines) {
    const lineNodes = Array.isArray(tree[line]) ? tree[line] : [];
    const spineNodes = lineNodes.filter((n) => n.kind === "spine");
    const branchNodes = lineNodes.filter((n) => n.kind === "branch");
    const spineLv = Number(tech[line] || 0);
    const effList = (techCfg.effects || {})[line] || [];
    const invested = branchNodes.filter((n) => Number(nodeLv[n.id] || 0) > 0).length;
    const open = !!TECH_FOLD[line];
    h += '<div class="tech-line">';
    h += '<button class="tech-line-head" data-act="tech-fold" data-line="' + line + '">' +
      '<span class="tech-caret">' + (open ? "▾" : "▸") + '</span>' +
      '<span class="tech-line-name">' + (techNames[line] || line) + '</span>' +
      '<span class="tag">主脉 L' + spineLv + ' / ' + maxTechLv + '</span>' +
      '<span class="tag">支脉 ' + invested + ' / ' + branchNodes.length + '</span>' +
      '</button>';
    if (!open) { h += '</div>'; continue; }

    h += '<div class="tech-line-body">';
    for (const sp of spineNodes) {
      const idx = Number(sp.spine || 0);
      const done = spineLv >= idx;
      const ready = spineLv === idx - 1;
      const isPend = !!pendId && pendId === sp.id;
      const state = isPend ? "researching" : done ? "done" : ready ? "ready" : "locked";
      const costCoin = techCurveAt(coinCurve, spineLv);
      const costIron = techCurveAt(ironCurve, spineLv);
      const costHour = techCurveAt(hourCurve, spineLv);
      const enough = Number(wallet.coin || 0) >= costCoin &&
        Number(wallet.iron || 0) >= costIron;
      const curEff = done ? (effList[idx - 1] || sp.desc || "") : "";
      const nextEff = !done ? (effList[idx - 1] || sp.desc || "") : "";
      let act;
      if (isPend) act = '<span class="sub tech-state">研究进行中</span>';
      else if (done) act = '<span class="sub tech-state done">✅ 已研究</span>';
      else if (!ready) act = '<span class="sub tech-state">🔒 ' + esc(sp.parent_name ? "需先完成 " + sp.parent_name : "前置未完成") + '</span>';
      else if (!canGovern) act = '<span class="sub tech-state">-</span>';
      else if (techPend) act = '<span class="sub tech-state">研究进行中</span>';
      else if (!enough) act = '<span class="sub tech-state bad">铜钱/精铁不足</span>';
      else act = '<button class="btn small" data-act="tech" data-line="' + line +
        '" data-node="' + sp.id + '">研究</button>';
      h += '<div class="tech-spine-row">' +
        '<div class="tech-node spine ' + state + '">' +
          '<div class="tech-node-head"><span class="tech-idx">' + idx + '</span>' +
          '<b>' + esc(sp.name) + '</b>' +
          '<span class="tag">L' + (done ? idx : idx - 1) + '/' + maxTechLv + '</span>' +
          (isPend ? '<span class="tag tech-pend">研究中</span>' : '') + '</div>' +
          '<div class="tech-eff">' + (done
            ? esc(curEff)
            : (nextEff ? '<span class="sub">下一级：</span>' + esc(nextEff) : '')) + '</div>' +
          (done ? '' : '<div class="tech-cost">🪙' + fmtNum(costCoin) +
            ' · ⛏️' + fmtNum(costIron) + ' · ⏱' + fmtNum(costHour) + 'h</div>') +
          '<div class="tech-act">' + act + '</div>' +
          ((sp.unlocks || []).length
            ? '<div class="tech-unlock">解锁：' + sp.unlocks.map(esc).join(" / ") + '</div>'
            : '') +
        '</div>';

      const children = branchNodes.filter((b) => b.parent === sp.id);
      h += '<div class="tech-branch-list">';
      for (const b of children) {
        const blv = Number(nodeLv[b.id] || 0);
        const bmax = Number(b.max || 5);
        const unlocked = spineLv >= idx;
        const maxed = blv >= bmax;
        const isBPend = !!pendId && pendId === b.id;
        const bState = isBPend ? "researching" : !unlocked ? "locked"
          : maxed ? "maxed" : blv > 0 ? "done" : "ready";
        const per = parseTechPer(b.per_level_text);
        const curText = fmtTechTotal(per, blv, b.mode);
        const nextText = fmtTechTotal(per, blv + 1, b.mode);
        const bCoin = techCurveAt(bCoinCurve, blv);
        const bIron = techCurveAt(bIronCurve, blv);
        const bHour = techCurveAt(bHourCurve, blv);
        const bEnough = Number(wallet.coin || 0) >= bCoin &&
          Number(wallet.iron || 0) >= bIron;
        let bAct;
        if (isBPend) bAct = '<span class="sub tech-state">研究进行中</span>';
        else if (!unlocked) bAct = '<span class="sub tech-state">🔒 ' + esc(b.parent_name ? "需先完成 " + b.parent_name : "前置未完成") + '</span>';
        else if (maxed) bAct = '<span class="sub tech-state maxed">已满级</span>';
        else if (!canGovern) bAct = '<span class="sub tech-state">-</span>';
        else if (techPend) bAct = '<span class="sub tech-state">研究进行中</span>';
        else if (!bEnough) bAct = '<span class="sub tech-state bad">铜钱/精铁不足</span>';
        else bAct = '<button class="btn small" data-act="tech" data-line="' + line +
          '" data-node="' + b.id + '">研究</button>';
        h += '<div class="tech-node branch ' + bState + '">' +
          '<div class="tech-node-head"><b>' + esc(b.name) + '</b>' +
          '<span class="tag">L' + blv + '/' + bmax + '</span>' +
          (isBPend ? '<span class="tag tech-pend">研究中</span>' : '') + '</div>' +
          '<div class="tech-eff">' + esc(b.desc) + ' ' + curText +
          (!maxed ? ' <span class="sub">→ ' + nextText + '</span>' : '') + '</div>' +
          (!maxed && unlocked
            ? '<div class="tech-cost">🪙' + fmtNum(bCoin) + ' · ⛏️' + fmtNum(bIron) +
              ' · ⏱' + fmtNum(bHour) + 'h</div>'
            : '') +
          '<div class="tech-act">' + bAct + '</div>' +
          '</div>';
      }
      h += '</div></div>';
    }
    h += '</div></div>';
  }
  return h + "</div>";
}

function plotCard(p) {
  const strategists = (p.chars || []).filter((c) =>
    c.role === "strategist" && c.status === "idle" &&
    !["dying", "dead"].includes(c.injury_state));
  let h = '<div class="card"><h3>🕵️ 计略派遣</h3>';
  if (strategists.length) {
    const enemyNodes = (S.nodes || []).filter((n) =>
      !(n.owner && factionIsFriendly(n.owner.id)));
    h += '<div class="form-row"><select id="plotChar" style="max-width:110px">' +
      strategists.map((c) => '<option value="' + c.id + '">' + esc(c.name) + "</option>").join("") +
      '</select><select id="plotType" style="max-width:110px">' +
      Object.entries(S.meta.plot_types || {}).map(([k, v]) => '<option value="' + k + '">' + v + "</option>").join("") +
      '</select><select id="plotTarget" style="max-width:150px">' +
      enemyNodes.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
      '</select><button class="btn small primary" data-act="dispatch-plot">施展</button></div>' +
      '<div class="sub">造谣/纵火/劝降/策应以据点为目标；离间需敌方角色 ID（下方按钮输入）；营救在「人员」页俘虏区操作；成功率 35~85%，失败可能走漏风声</div>' +
      '<div class="form-row"><button class="btn small" data-act="alienate">🗣️ 离间（输入敌方角色 ID）</button></div>';
  } else h += '<div class="sub">（无空闲军师，无法施展计略）</div>';
  if ((S.winds || []).length) {
    h += '<div class="sub" style="margin-top:6px">🌬️ 风声：' + S.winds.map((w) => {
      const t = typeof w === "string" ? w : ((w && w.text) || "");
      const k = (w && w.kind_name) ? "【" + w.kind_name + "】" : "";
      return esc(k + t);
    }).join("<br>🌬️ ") + "</div>";
  }
  return h + "</div>";
}

function appointmentCard(f) {
  const appPos = f.appointable || {};
  const posKeys = Object.keys(appPos);
  const govCount = Number(f.governance_node_count || 0);
  const inAlliance = !!Number(f.alliance_id || 0);
  const appointLabel = inAlliance ? "盟主/副主" : "势力主/副主";
  const scopeLabel = inAlliance ? "联盟成员势力据点总数" : "本势力据点数量";
  let h = '<div class="card"><h3>🏛 角色官职任免</h3>' +
    '<div class="sub">官职绑定具体角色，主将带兵上限按其官职计算；可任免档位按' +
    scopeLabel + '开放。当前据点 <b>' + govCount + '</b> 座。</div>';
  if (posKeys.length) {
    h += '<div class="tbl-wrap"><table class="tbl"><tr><th>官职</th><th>名额</th><th>带兵上限</th><th>俸禄/tick</th><th>势力效果</th></tr>';
    for (const k of posKeys) {
      const v = appPos[k];
      h += '<tr><td>' + esc(v.name) + '</td><td>' + v.used + " / " + v.cap +
        '</td><td>' + fmtNum(v.troop_cap) + '</td><td>🪙 ' + fmtNum(v.salary_per_tick || 0) +
        '</td><td class="sub">' + esc(v.effect || "-") + '</td></tr>';
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
    h += '<div class="tbl-wrap" style="margin-top:8px"><table class="tbl"><tr><th>角色</th><th>官职</th><th>俸禄</th></tr>' +
      apps.map((a) => {
        const st = salaryStateOf(a);
        const pay = st === "paid" ? "🪙 " + fmtNum(a.salary_per_tick || 0)
          : salaryStateText(st, "欠俸·加成暂停");
        return "<tr><td>" + esc(a.name) + ' <span class="sub">(' + esc(a.owner_user_id) +
          ")</span></td><td>" + esc(a.position_name) + "</td><td>" + pay + "</td></tr>";
      }).join("") +
      "</table></div>";
  }
  if (f.can_appoint) {
    const chars = f.appoint_chars || [];
    if (chars.length && posKeys.length) {
      h += '<div class="form-row" style="margin-top:8px"><select id="appointChar" style="max-width:170px">' +
        chars.map((c) => '<option value="' + c.id + '">' + esc(c.name) + "（" + esc(c.role_name) +
          (c.position ? "·" + positionName(c.position) : "") + "）</option>").join("") +
        '</select><select id="appointPos" style="max-width:150px"><option value="">（卸任）</option>' +
        posKeys.map((k) => '<option value="' + k + '">' + esc(appPos[k].name) +
          "（" + appPos[k].used + "/" + appPos[k].cap + "）</option>").join("") +
        '</select><button class="btn small primary" data-act="appoint-char">任命 / 卸任</button></div>' +
        '<div class="sub">角色官职与据点太守相互独立，可同时担任；太守请在据点抽屉任免。</div>';
    } else if (!chars.length) {
      h += '<div class="sub">（暂无角色可任免）</div>';
    }
  } else {
    h += '<div class="sub">（仅 ' + appointLabel +
      ' 可任免角色官职；据点太守也由' + appointLabel + '任免）</div>';
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
  // 官员可参与联盟共同治理；科技/迁都仍是各势力独立操作。
  const canGovern = ["leader", "deputy", "situ", "taiwei", "jijiu"].includes(f.my_position);
  const canAppoint = ["leader", "deputy"].includes(f.my_position);
  // 迁都/移交按本势力名册（own_position）判定；
  // 联盟共同治理名册（my_position）只用于外交、联盟官职与任务。
  const ownPosition = f.own_position != null ? f.own_position : f.my_position;
  const isOwnLeader = ownPosition === "leader";
  const isHostFaction = inAlliance && !!f.is_alliance_host;
  const isHostLeader = isHostFaction && f.my_position === "leader";
  // --- 概况：个人势力与联盟分开表述 ---
  h += '<div class="card"><h3><span class="lg-dot" style="display:inline-block;background:' +
    safeColor(((S.factions || []).find((x) => x.id === f.id) || {}).color) + '"></span> ' + esc(f.name) +
    ' <span class="tag">' + (f.kind === "player" ? "个人势力" : f.kind) + "</span>" +
    (inAlliance ? ' <span class="tag">' + (isHostFaction ? "盟主势力" : "联盟成员势力") + "</span>" : "") +
    "</h3>" +
    '<div class="sub">势力等级 ' + f.level + " · 势力声望 " + f.reputation + " · 我的势力身份 " +
    positionTitle(ownPosition, false) +
    (inAlliance ? " · 我的联盟身份 " + positionTitle(f.my_position, true) : "") +
    "（角色带兵上限由主将官职决定）</div>" +
    (() => {
      const ss = f.salary_summary || {};
      if (!Number(ss.due || 0)) return "";
      const names = (ss.unpaid_positions || []).map((k) => positionTitle(k, false)).join("、");
      return kv("官职俸禄", "🪙 " + fmtNum(ss.paid || 0) + " / " + fmtNum(ss.due || 0) +
        " 每 tick（势力主钱包支付）" + (Number(ss.short || 0) > 0
          ? ' · <span class="warn-text">欠俸 ' + fmtNum(ss.short) +
            (names ? "（" + esc(names) + "）" : "") + "，对应加成暂停</span>"
          : " · 全额发放"));
    })() +
    (inAlliance ? kv("联盟分工", "联盟共同治理外交 / 官职 / 名册 / 任务；" +
      "科技、经济、据点与主城仍归各成员势力独立") : "") +
    (inAlliance
      ? kv("联盟成员", f.members.length + " 人 · " +
        (f.allied_factions || []).length + " 个成员势力（共同名册）")
      : kv("势力成员", f.members.length + " / " + f.member_cap)) +
    kv("本势力据点", (f.node_count || 0) + " 座") +
    kv("本势力主城", esc((nodeById(f.capital_node_id) || {}).name || "已陷落")) + "</div>";
  // --- 联盟概况与据点交接 ---
  if (inAlliance) {
    const allied = f.allied_factions || [];
    const allyIds = new Set(allied.map((x) => x.id));
    h += '<div class="card"><h3>🤝 联盟「' + esc(myAllianceName) + '」</h3>' +
      '<div class="sub">联盟名即盟主势力名（会签时填写）；结盟后各成员势力保留据点、资源与科技。联盟共同治理外交 / 官职 / 任务，并开放协战、额外行军加成与友方道路免粮。盟主势力：' +
      esc((allied.find((x) => x.is_host) || {}).name || "?") + "</div>";
    h += '<div class="tbl-wrap"><table class="tbl"><tr><th>成员势力</th><th>据点</th><th>主城</th><th>联盟身份</th></tr>';
    for (const a of allied) {
      h += "<tr><td>" + esc(a.name) + (a.id === f.id ? "（我）" : "") + "</td><td>" +
        a.node_count + "</td><td>" + esc((nodeById(a.capital_node_id) || {}).name || "-") +
        "</td><td>" + (a.is_host ? "盟主势力" : "成员势力") + "</td></tr>";
    }
    h += "</table></div>";
    if (canGovern && allied.length > 1) {
      const allyNodes = (S.nodes || []).filter((n) => n.owner && allyIds.has(n.owner.id));
      h += '<div class="sub" style="margin-top:8px">据点交接：把本联盟内某座据点改归另一成员势力（守军与建筑保留）</div>' +
        '<div class="form-row alliance-transfer"><select id="transferNode" style="max-width:170px">' +
        allyNodes.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "（" + esc((n.owner || {}).name || "?") + "）</option>").join("") +
        '</select><select id="transferTarget" style="max-width:140px">' +
        allied.map((a) => '<option value="' + a.id + '">' + esc(a.name) + "</option>").join("") +
        '</select><button class="btn small primary" data-act="transfer-node">🔁 交接据点</button></div>';
    }
    h += "</div>";
  }
  // --- 加入联盟（未结盟时） ---
  if (!inAlliance) {
    if (p.faction_invite) {
      h += '<div class="card live"><h3>💌 联盟邀请</h3><div class="sub">联盟「' +
        esc(factionNameById(p.faction_invite.fid)) +
        '」邀请你的势力加入</div><div class="form-row">' +
        '<button class="btn small primary" data-act="invite-respond" data-accept="1">接受</button>' +
        '<button class="btn small" data-act="invite-respond" data-accept="0">婉拒</button></div></div>';
    }
    const others = (S.factions || []).filter(
      (x) => x.kind === "player" && x.alive && x.id !== f.id && !x.alliance_id);
    h += '<div class="card"><h3>🤝 加入联盟</h3>' +
      '<div class="sub">加入后双方保留各自势力、据点、资源与科技；联盟共同治理外交 / 官职 / 名册 / 任务，经济与科技仍各势力独立</div>';
    if (isOwnLeader && others.length) {
      for (const x of others) {
        h += '<div class="form-row"><span class="lg-dot" style="display:inline-block;background:' +
          safeColor(x.color) + '"></span> ' + esc(x.name) + "（声望 " + x.reputation +
          " · 据点 " + x.node_count + "）" +
          '<button class="btn small" data-act="faction-apply" data-id="' + x.id +
          '">申请加入联盟</button></div>';
      }
    } else if (!others.length) {
      h += '<div class="sub">（暂无可结盟的其他玩家势力）</div>';
    } else {
      h += '<div class="sub">（仅势力主可代表本势力申请加入联盟）</div>';
    }
    if (canGovern) {
      h += '<div class="form-row"><input id="inviteUser" placeholder="对方玩家 ID" style="max-width:160px">' +
        '<button class="btn small primary" data-act="faction-invite">💌 邀请结盟</button></div>';
    }
    h += "</div>";
  }
  // --- 治理（官员可操作） ---
  if (canGovern && (f.applications || []).length) {
    h += '<div class="card"><h3>📥 入盟申请</h3>' + f.applications.map((u) =>
      '<div class="form-row"><span class="sub">' + esc(u) + "</span>" +
      '<button class="btn small primary" data-act="approve" data-u="' + esc(u) + '">批准入盟</button>' +
      '<button class="btn small" data-act="reject" data-u="' + esc(u) + '">拒绝</button></div>').join("") + "</div>";
  }
  // --- 角色官职（v74：绑定角色，按据点解锁）已迁移至「人员」页 ---
  // --- 成员贡献榜（联盟看共同名册；未结盟看本势力名册） ---
  h += '<div class="card"><h3>👥 ' + (inAlliance ? "联盟成员贡献榜" : "势力成员贡献榜") +
    '</h3><div class="sub">' +
    (inAlliance
      ? "这里是联盟共同治理名册：治理身份决定联盟外交与官职；科技/经济仍按各成员势力独立，由本势力势力主与副主操作。"
      : "这里展示本势力成员：势力主与副主管理科技/经济；联盟外交与官职需结盟后另行治理。") +
    "</div>" +
    '<table class="tbl"><tr><th>成员</th><th>治理身份</th><th>贡献</th>' +
    (canAppoint ? "<th>设置" + (inAlliance ? "联盟副主" : "副主") + "</th>" : "") + "</tr>";
  for (const m of [...f.members].sort((a, b) => b.contribution - a.contribution)) {
    const pname = m.position === "leader" ? (inAlliance ? "盟主" : "势力主")
      : (m.position === "deputy" ? "副主" : "成员");
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
  if (isOwnLeader && f.members.length > 1) {
    const ownMembers = f.members.filter((m) => m.user_id !== p.user_id &&
      m.faction_id === f.id);
    if (ownMembers.length) {
      h += '<div class="form-row"><select id="transferSel" style="max-width:150px">' +
        ownMembers.map((m) =>
          '<option value="' + esc(m.user_id) + '">' + esc(m.user_id) + "</option>").join("") +
        '</select><button class="btn small danger" data-act="transfer-leader">👑 ' +
        (isHostLeader ? "移交盟主" : "移交势力主") + "</button></div>";
    }
  }
  h += "</div></div>";
  // --- 势力/联盟任务 / 动员令（S.faction_ext）---
  const ext = S.faction_ext || {};
  // 任务与动员令都挂在盟主势力名下：结盟后统一称「联盟任务 / 联盟动员令」。
  const taskLabel = inAlliance ? "联盟任务" : "势力任务";
  const govLabel = inAlliance ? "盟主 / 副主 / 祭酒" : "势力主 / 副主 / 祭酒";
  h += '<div class="card"><h3>📜 ' + taskLabel + "</h3>" +
    '<div class="sub">' + (inAlliance
      ? "由盟主势力统一刷新，全联盟成员共同推进；任务进度与奖励按联盟共享"
      : "每 12 小时刷 3 条，24 小时内完成；势力成员共同推进后可领取战功") + "</div>";
  const tasks = ext.tasks;
  if (!Array.isArray(tasks)) {
    h += '<div class="sub">（' + taskLabel + "数据暂未下发）</div>";
  } else if (!tasks.length) {
    h += '<div class="sub">（暂无进行中的' + taskLabel + "）</div>";
  } else {
    const nowSec = srvNowSec();
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
    h += '<div class="form-row"><button class="btn small" data-act="task-refresh">🔄 刷新' +
      taskLabel + '</button><span class="sub">' + govLabel +
      ' · 冷却 12 小时</span></div>';
  } else {
    h += '<div class="sub">（仅 ' + govLabel + ' 可手动刷新）</div>';
  }
  h += "</div>";
  const mobLabel = inAlliance ? "联盟动员令" : "动员令";
  h += '<div class="card"><h3>📣 ' + mobLabel + "</h3>" +
    '<div class="sub">消耗 60 战功，持续 12 小时，冷却 24 小时；四选一全军 buff，' +
    (inAlliance ? "全联盟共享" : "本势力共享") + "</div>";
  const mob = ext.mobilization;
  if (mob === undefined) {
    h += '<div class="sub">（' + mobLabel + "状态暂未下发）</div>";
  } else if (mob) {
    const mobLeft = Math.max(0, Number(mob.ends_at || 0) - srvNowSec());
    h += '<div class="sub">当前生效：' + esc(mob.name || mob.kind || "?") +
      "（剩余 " + (mobLeft ? fmtRemain(mobLeft) : "即将结束") + "）</div>";
  } else {
    h += '<div class="sub">（当前无生效' + mobLabel + "）</div>";
  }
  if (["leader", "deputy", "jijiu"].includes(f.my_position)) {
    const MOB_KINDS = [
      ["recruit", "征兵动员"], ["march", "行军动员"],
      ["defend", "守土动员"], ["attack", "征伐动员"],
    ];
    h += '<div class="form-row"><select id="mobKind" style="max-width:130px">' +
      MOB_KINDS.map(([k, n]) => '<option value="' + k + '">' + n + "</option>").join("") +
      '</select><button class="btn small primary" data-act="mob-issue">发布' + mobLabel + '</button>' +
      '<span class="sub">我的战功 ' + fmtNum(p.merit || 0) + "</span></div>";
  } else {
    h += '<div class="sub">（仅 ' + govLabel + ' 可发布）</div>';
  }
  h += "</div>";
  // --- 外交矩阵（联盟外交读取盟主势力，未结盟时为本势力外交） ---
  h += '<div class="card"><h3>🤝 ' + (inAlliance ? "联盟外交" : "势力外交") + "</h3>";
  const favorCfg = ((S.meta || {}).favor_cfg) || {};
  const allyMin = Number(favorCfg.ally_min || 60);
  h += '<div class="sub">' + (inAlliance
    ? "联盟外交由盟主势力统一对外，各成员势力共享同一组外交关系。"
    : "本势力外交独立对外；结盟后改由联盟统一治理。") +
    "与 NPC 势力存在好感度：赠礼、通商、停战/和谈、共同敌人等会提升；宣战、背盟、突袭会降低。好感 ≥ " +
    allyMin + " 才能尝试外交结盟。好感会随时间缓慢回落。</div>";
  const rels = f.relations || [];
  if (rels.length) {
    h += '<table class="tbl"><tr><th>对象势力</th><th>关系</th><th>好感</th><th>操作</th></tr>';
    for (const r of rels) {
      let ops = "-";
      if (r.rel.startsWith("propose_") && !r.rel.endsWith("_out") &&
          r.from !== f.id && canGovern) {
        ops = '<button class="btn small primary" data-act="dip-respond" data-id="' + r.faction_id + '" data-accept="1">接受</button> ' +
          '<button class="btn small" data-act="dip-respond" data-id="' + r.faction_id + '" data-accept="0">拒绝</button>';
      }
      const favor = Number(r.favor || 0);
      const tier = r.favor_name || "中立";
      const favorColor = favor >= allyMin ? "#2e7d32" : (favor <= -20 ? "#c62828" : "#666");
      h += "<tr><td>" + esc(r.name) + (r.kind === "npc" ? ' <span class="tag">NPC</span>' : "") +
        "</td><td>" + esc(REL_NAMES[r.rel] || r.rel) +
        (r.indemnity ? "（赔款 " + fmtNum(r.indemnity) + "）" : "") +
        '</td><td style="color:' + favorColor + '">' + favor + " · " + esc(tier) +
        '</td><td>' + ops + "</td></tr>";
    }
    h += "</table>";
  }
  if (canGovern) {
    const myAlliance = Number(f.alliance_id || 0);
    const others = (S.factions || []).filter((x) => x.alive && x.id !== f.id &&
      (x.kind === "player" || x.kind === "npc") &&
      // 同联盟成员势力之间不出现外交提案（后端也会拒绝）
      !(myAlliance && Number(x.alliance_id || 0) === myAlliance));
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
  } else {
    h += '<div class="sub">（仅 ' + (inAlliance
      ? "盟主/副主/司徒/太尉/祭酒" : "势力主/副主/司徒/太尉/祭酒") +
      " 可发起" + (inAlliance ? "联盟外交" : "外交") + "）</div>";
  }
  h += "</div>";
  // --- 科技树与计略派遣已迁移至「总览」页 ---
  // --- 俘虏（前端已迁移至「人员」页） ---
  // --- 本势力主城与防御（联盟成员势力各自独立） ---
  h += '<div class="card"><h3>🏯 本势力主城与防御</h3>';
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
    const cdLeft = Math.max(0, cdUntil - srvNowSec());
    if (isOwnLeader && relocateNodes.length) {
      h += '<div class="form-row"><select id="relocateNode" style="max-width:150px">' +
        relocateNodes.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
        '</select><button class="btn small" data-act="capital-relocate"' +
        (cdLeft > 0 ? " disabled" : "") + ">🏯 迁都</button>" +
        (cdLeft > 0
          ? '<span class="sub">冷却中 ' + fmtRemain(cdLeft) + "</span>"
          : '<span class="sub">扣个人铜钱 · 仅势力主</span>') + "</div>";
    } else if (isOwnLeader && !relocateNodes.length) {
      h += '<div class="sub">（无可迁都目标：需另有城池 / 关隘 / 分舵）</div>';
    } else if (!isOwnLeader) {
      h += '<div class="sub">（仅势力主可迁都；联盟成员势力各自独立）</div>';
    }
  }
  // 重建合力：v4.5.7 起玩家势力无国库，成员随时可向本势力重建合力捐灵石，
  // 用于主城陷落后的重建；同时推进「共襄重建」势力任务（不要求主城已陷落）。
  const rebuildFund = ext.rebuild_fund;
  h += '<div class="form-row"><span class="sub">重建合力：' +
    (rebuildFund !== undefined ? fmtNum(rebuildFund) + " 灵石" : "数据暂未下发") +
    '</span></div>';
  h += '<div class="form-row"><input id="rebuildDonateAmt" type="number" min="1" placeholder="灵石数量" style="max-width:110px">' +
    '<button class="btn small" data-act="rebuild-donate">💰 重建捐献</button>' +
    '<span class="sub">成员可共同凑款 · 主城陷落后用于重建</span></div>';
  if (!f.capital_node_id) {
    const free = (S.nodes || []).filter((n) => !n.owner && ["city", "pass", "branch"].includes(n.type));
    if (free.length) {
      h += '<div class="form-row"><select id="rebuildNode" style="max-width:150px">' +
        free.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
        '</select><button class="btn small danger" data-act="rebuild-capital">🔥 重建主城（花灵石，残军散尽）</button></div>';
    } else {
      h += '<div class="sub">（已无无主城池/关隘/分舵可供重建；可申请加入其他玩家创建的联盟）</div>';
    }
  }
  h += '<div class="form-row"><button class="btn small danger" data-act="faction-quit">' +
    (inAlliance ? (f.is_alliance_host ? "💔 解散联盟" : "🚪 退出联盟") : "🚪 退出势力") +
    "</button></div></div>";
  return h;
}

// ---------------------------------------------------------------------------
// 人员：俘虏事务（原势力页迁入）
// ---------------------------------------------------------------------------
function captiveFind(charId) {
  const cap = (S.player || {}).captives || {};
  const groups = [
    ["holding", cap.holding || []],
    ["held", cap.held || []],
    ["recruitable", cap.recruitable || []],
  ];
  for (const [kind, list] of groups) {
    const c = list.find((x) => Number(x.id) === Number(charId));
    if (c) return { c, kind };
  }
  return null;
}

function captiveSummary(c) {
  const st = c.stats || {};
  const total = Number(c.stat_total || 0) ||
    Object.keys(st).reduce((sum, k) => sum + Number(st[k] || 0), 0);
  const traits = (c.trait_names || []).slice(0, 2).join("、");
  return "Lv" + (c.level || 1) + " · 六维 " + total +
    (c.stat_cap ? "/" + c.stat_cap : "") +
    (traits ? " · 特技 " + traits : "") +
    " · " + (c.injury_name || ("伤势 " + (c.injury || 0)));
}

function captiveRateText(c) {
  if (c.persuade_rate === null || c.persuade_rate === undefined) return "";
  return "劝降约 " + Math.round(Number(c.persuade_rate) * 100) + "%";
}

function captiveCdText(c) {
  const left = Number(c.persuade_cd_left || 0);
  if (left <= 0) return "";
  return "劝降冷却 " + (left >= 3600
    ? Math.ceil(left / 3600) + "h" : Math.ceil(left / 60) + "m");
}

function captiveActionButtons(c, cap, ctx) {
  const ctx0 = ctx || {};
  const modal = !!ctx0.modal;
  const kind = ctx0.kind || "";
  const sfx = modal ? "m_" : "";
  const canManage = !!cap.can_manage;
  // 与后端配置/权限保持一致：sell_enable=false 时后端会拒绝售卖，
  // 前端不再展示按钮；ransom_enable 只控「定价」，已设赎金仍可支付。
  const ccfg = ((S || {}).meta || {}).captive_cfg || {};
  const sellOn = ccfg.sell_enable !== false;
  const ransomOn = ccfg.ransom_enable !== false;
  let h = "";
  if (!modal) {
    h += '<button class="btn small" data-act="captive-detail" data-id="' +
      c.id + '">👁 详情</button> ';
  }
  if (kind === "holding") {
    h += '<button class="btn small primary" data-act="persuade" data-id="' +
      c.id + '">劝降</button> ';
    if (ransomOn) {
      h += '<button class="btn small" data-act="ransom-set" data-id="' +
        c.id + '">' + (Number(c.ransom || 0) > 0 ? "改价赎回" : "定价赎回") +
        "</button> ";
    }
    if (canManage) {
      h += '<button class="btn small" data-act="release" data-id="' + c.id +
        '">🕊 释放</button> ';
      h += '<button class="btn small danger" data-act="execute-captive" data-id="' +
        c.id + '">🗡 处决</button> ';
      if (sellOn) {
        h += '<button class="btn small" data-act="sell-captive" data-id="' +
          c.id + '">💰 售卖</button> ';
      }
    }
  } else if (kind === "held") {
    h += '<button class="btn small primary" data-act="ransom-pay" data-id="' +
      c.id + '">💰 支付赎金</button> ';
    if (ctx0.freeStrat) {
      h += '<button class="btn small" data-act="rescue-plot" data-id="' +
        c.id + '">🕵️ 细作营救</button> ';
    }
  }
  if (c.can_recruit) {
    const nodes = (ctx0.recruitNodes || []);
    if (nodes.length) {
      h += '<select id="recruitNode_' + sfx + c.id +
        '" style="max-width:110px">' +
        nodes.map((n) => '<option value="' + n.id + '"' +
          (Number(n.id) === Number(c.node_id) ? " selected" : "") + ">" +
          esc(n.name) + "</option>").join("") + "</select>";
    }
    h += '<button class="btn small primary" data-act="recruit-captive" data-id="' +
      c.id + '"' + (modal ? ' data-modal="1"' : "") +
      (ctx0.rosterFull ? " disabled" : "") + ">📜 招募入册</button> ";
    if (ctx0.rosterFull) h += '<span class="sub">名册已满，请扩建/遣散</span>';
  }
  if (kind === "holding" && !modal && (ctx0.destOpts || []).length) {
    h += '<select id="xfer_' + c.id + '" style="max-width:100px">' +
      ctx0.destOpts.map((n) => '<option value="' + n.id + '">' +
        esc(n.name) + "</option>").join("") +
      '</select><button class="btn small" data-act="transfer-captive" data-id="' +
      c.id + '">🔒 押运</button>';
  }
  return h;
}

function openCaptiveDetail(charId) {
  const found = captiveFind(charId);
  if (!found) return;
  const c = found.c;
  const cap = (S.player || {}).captives || {};
  const kind = found.kind;
  const m = S.meta || {};
  const ccfg = m.captive_cfg || {};
  const rosterFull = Number((S.player || {}).char_cap || 0) > 0 &&
    Number((S.player || {}).char_count || 0) >=
    Number((S.player || {}).char_cap || 0);
  const freeStrat = ((S.player || {}).chars || []).some((x) =>
    x.role === "strategist" && x.status === "idle" &&
    !["dying", "dead"].includes(x.injury_state));
  const recruitNodes = myNodes().filter(canDwellAt);
  const rarityName = c.rarity_name ||
    ((m.rarity_names || {})[c.rarity] || c.rarity || "");
  const apt = Object.entries(c.aptitude || {}).map(([t, g]) =>
    ((m.troop_types || {})[t] || {}).name + ":" + g).join(" · ");
  const rate = captiveRateText(c);
  const cd = captiveCdText(c);
  const traitHtml = traitChips(c.traits, c.trait_names);
  const statNames = m.stat_names || {};
  const statTotal = Number(c.stat_total || 0);
  const executeMerit = Number(c.execute_merit || 0);
  const sellCoin = Number(c.sell_coin || 0);
  let h = '<div class="captive-head"><h3>' + esc(c.name) + "</h3>" +
    '<span class="tag ' + esc(c.rarity || "") + '">' + esc(rarityName) + "</span>" +
    '<span class="tag">' + esc(c.role_name || c.role || "") + "</span>" +
    (c.profession_name
      ? '<span class="tag">' + esc(c.profession_name) + "</span>" : "") +
    "</div>";
  h += '<div class="kv"><span class="k">等级</span><span>Lv' + (c.level || 1) +
    " · 阅历 " + (c.exp || 0) + "/" + (c.exp_next || 0) +
    " · 成长 " + statTotal + "/" + (c.stat_cap || "?") + "</span></div>";
  h += '<div class="stat-strip">' + Object.entries(statNames).map(([k, name]) =>
    "<span>" + esc(name) + " <b>" + Number((c.stats || {})[k] || 0) +
    "</b></span>").join("") + "</div>";
  h += '<div class="kv"><span class="k">特技</span><span class="trait-cell">' +
    (traitHtml || "无") + "</span></div>";
  if (c.personality_top_name) {
    h += '<div class="kv"><span class="k">个性</span><span class="trait-cell">🌟 ' +
      personalityChipHtml("top", (c.personality || {}).top,
        c.personality_top_name, c.personality_top_desc) + "</span></div>";
  }
  if (c.personality_neg_name) {
    h += '<div class="kv"><span class="k">负面</span><span class="trait-cell">🔻 ' +
      personalityChipHtml("negative", (c.personality || {}).negative,
        c.personality_neg_name, c.personality_neg_desc) + "</span></div>";
  }
  if (apt) h += '<div class="kv"><span class="k">适性</span><span>' + esc(apt) + "</span></div>";
  h += '<div class="kv"><span class="k">伤势</span><span>' +
    esc(c.injury_name || "") + "（" + Number(c.injury || 0) + "） · 体力 " +
    Number(c.stamina || 0) + "</span></div>";
  h += '<div class="kv"><span class="k">忠诚</span><span>' + Number(c.loyalty || 0) +
    (c.disloyal ? ' <span style="color:#f85149">离心</span>' : "") +
    (rate ? " · " + esc(rate) : "") +
    (cd ? " · " + esc(cd) : "") + "</span></div>";
  h += '<div class="kv"><span class="k">原属</span><span>' +
    esc(c.home_faction_name || "无") +
    (c.home_owner ? " · 原主 " + esc(c.home_owner) : "") + "</span></div>";
  h += '<div class="kv"><span class="k">关押</span><span>' +
    esc(c.node_name || nodeNameById(c.node_id) || ("据点#" + c.node_id)) +
    "</span></div>";
  if (Number(c.ransom || 0) > 0) {
    h += '<div class="kv"><span class="k">赎金</span><span>' +
      fmtNum(Number(c.ransom)) + " 铜钱</span></div>";
  }
  h += '<div class="kv"><span class="k">处置</span><span>处决：战功 +' +
    fmtNum(executeMerit) + " · 同牢威慑 " +
    Number(ccfg.execute_deterrence || 0) + " 点 / 人 · 外交 " +
    Number(ccfg.execute_diplomacy_penalty || 0) + "；" +
    (ccfg.sell_enable === false ? "售卖：未开启" : "售卖：铜钱 +" + fmtNum(sellCoin)) +
    "；释放：声望 +" +
    Number(ccfg.release_rep_gain || 3) + " / 外交 +" +
    Number(ccfg.release_diplomacy_gain || 5) + "</span></div>";
  if (c.can_recruit) {
    h += '<div class="sub" style="margin-top:6px">📜 可招募入册（占用 1 个名册名额）' +
      (rosterFull ? "；当前名册已满" : "") + "。</div>";
  }
  h += '<div class="drawer-actions">' +
    captiveActionButtons(c, cap, {
      modal: true, kind, rosterFull, recruitNodes, freeStrat,
    }) + "</div>";
  document.getElementById("captiveDetailTitle").textContent = "⛓️ 俘虏详情";
  document.getElementById("captiveDetailBody").innerHTML = h;
  document.getElementById("captiveOverlay").hidden = false;
  bindTabEvents();
}

function captivesCard(p) {
  const cap = p.captives || { holding: [], held: [], jail_slots: 0 };
  const myJailNodes = myNodes().filter(canDwellAt);
  const recruitNodes = myNodes().filter(canDwellAt);
  const ccfg = ((S.meta || {}).captive_cfg || {});
  const jailSlots = Math.max(0, Number(cap.jail_slots || ccfg.jail_slots_per_node || 5));
  const rosterCount = Number(p.char_count || 0);
  const rosterCap = Number(p.char_cap || 0);
  const rosterFull = rosterCap > 0 && rosterCount >= rosterCap;
  const canManage = !!cap.can_manage;
  let h = '<div class="card"><h3>⛓️ 俘虏</h3>' +
    '<div class="sub">大牢：每据点 ' + jailSlots + " 格（独立于名册，不占角色名额） · 名册：" +
    rosterCount + " / " + rosterCap + (rosterFull ? "（已满，需扩建/遣散）" : "") + "</div>";
  if (cap.holding.length) {
    h += '<div class="sub">我方关押（点击名字查看六维、特技与处置收益）：</div>' +
      '<div class="tbl-wrap"><table class="tbl"><tr><th>俘虏</th><th>概况</th><th>忠诚 / 劝降</th><th>操作</th></tr>';
    for (const c of cap.holding) {
      const destOpts = myJailNodes.filter((n) => n.id !== Number(c.node_id || 0));
      const rate = captiveRateText(c);
      const cd = captiveCdText(c);
      h += "<tr><td>" +
        '<span class="captive-link" data-act="captive-detail" data-id="' + c.id + '">' +
        esc(c.name) + "</span>" +
        ' <span class="tag ' + esc(c.rarity || "") + '">' +
        esc(c.rarity_name || (S.meta.rarity_names || {})[c.rarity] || c.rarity) + "</span>" +
        (c.node_id ? ' <span class="sub">@' + esc(c.node_name ||
          (nodeById(c.node_id) || {}).name || ("#" + c.node_id)) + "</span>" : "") +
        "</td><td><span class=\"sub\">" + esc(captiveSummary(c)) + "</span></td><td>" +
        (c.disloyal ? '<span style="color:#f85149">离心!</span> ' : "") +
        Number(c.loyalty || 0) +
        (rate ? '<span class="sub"> · ' + esc(rate) + "</span>" : "") +
        (cd ? '<span class="sub"> · ' + esc(cd) + "</span>" : "") +
        "</td><td>" +
        captiveActionButtons(c, cap, {
          kind: "holding", recruitNodes, rosterFull, destOpts,
        }) +
        "</td></tr>";
    }
    h += "</table></div>";
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
    // 营救必须由军师执行（不是武将）；筛掉重伤/濒死，具体人选交给
    // 后端按智谋自动挑选，避免固定取列表第一个导致成功率被白白拉低。
    const freeStrat = (p.chars || []).some((c) =>
      c.role === "strategist" && c.status === "idle" &&
      !["dying", "dead"].includes(c.injury_state));
    h += '<div class="sub">我方被俘 / 可招募离心俘虏：</div>' + cap.held.map((c) => {
      return '<div class="form-row"><span class="captive-link" data-act="captive-detail" data-id="' +
        c.id + '">🪖 ' + esc(c.name) + "</span>" +
        '<span class="sub">（忠诚 ' + Number(c.loyalty || 0) + "，关押于 " +
        esc(c.node_name || nodeNameById(c.node_id) || ("据点#" + c.node_id)) +
        (Number(c.ransom || 0) > 0 ? "，赎金 " + fmtNum(Number(c.ransom)) + " 币" : "") +
        "）</span>" +
        captiveActionButtons(c, cap, {
          kind: "held", recruitNodes, rosterFull, freeStrat,
        }) + "</div>";
    }).join("");
  }
  if ((cap.recruitable || []).length) {
    h += '<div class="sub">可招降的离心俘虏（其他势力关押）：</div>' +
      cap.recruitable.map((c) => {
        return '<div class="form-row"><span class="captive-link" data-act="captive-detail" data-id="' +
          c.id + '">🪖 ' + esc(c.name) + "</span>" +
          '<span class="sub">（忠诚 ' + Number(c.loyalty || 0) +
          "，关押于 " +
          esc(c.node_name || nodeNameById(c.node_id) || ("据点#" + c.node_id)) +
          "）</span>" +
          captiveActionButtons(c, cap, {
            kind: "recruitable", recruitNodes, rosterFull,
          }) + "</div>";
      }).join("");
  }
  if (!cap.holding.length && !cap.held.length && !(cap.recruitable || []).length) {
    h += '<div class="sub">（无俘虏事务）</div>';
  }
  if (!canManage) {
    h += '<div class="sub">（释放 / 处决 / 售卖仅势力主或副主可执行）</div>';
  }
  h += '<div class="sub">「招募入册」会把大牢俘虏转为己方角色并占用 1 个名册名额；名册满时需先扩建据点或遣散角色。</div>';
  return h + "</div>";
}

function factionNoHome(p) {
  let h = '<div class="card"><h3>🏳️ 你还没有个人势力</h3>' +
    '<div class="sub">散修可独自经营；也可发起联盟会签，或加入其他玩家创建的联盟</div></div>';
  if (p.faction_invite) {
    h += '<div class="card live"><h3>💌 联盟邀请</h3><div class="sub">联盟「' +
      esc(factionNameById(p.faction_invite.fid)) +
      '」邀请你的势力加入</div><div class="form-row">' +
      '<button class="btn small primary" data-act="invite-respond" data-accept="1">接受</button>' +
      '<button class="btn small" data-act="invite-respond" data-accept="0">婉拒</button></div></div>';
  }
  const pets = S.petitions || [];
  if (pets.length) {
    h += '<div class="card"><h3>✍️ 进行中的联盟会签</h3>';
    for (const pet of pets) {
      const left = Math.max(0, pet.expire_at - srvNowSec());
      h += '<div class="form-row"><span class="lg-dot" style="display:inline-block;background:' + safeColor(pet.color) + '"></span> ' +
        esc(pet.name) + "（" + pet.cosigners + "/3 个势力 · 剩 " + Math.round(left / 3600) + "h）" +
        '<button class="btn small primary" data-act="cosign" data-id="' + esc(pet.id) + '">会签（垫付会费）</button></div>';
    }
    h += "</div>";
  }
  h += '<div class="card"><h3>🎌 发起联盟会签</h3>' +
    '<div class="sub">以你的个人势力为盟主，联署势力作为联盟成员；3 个势力会签即成立</div>' +
    '<div class="form-row">' +
    '<input id="newFactionName" placeholder="联盟名（2~12字）" style="max-width:140px">' +
    '<input id="newFactionColor" type="color" value="#58a6ff" style="max-width:50px">' +
    '<button class="btn small primary" data-act="faction-create">发起（垫付 10000 铜钱，3 个势力成盟）</button></div></div>';
  const playerFs = (S.factions || []).filter((x) => x.kind === "player" && x.alive);
  if (playerFs.length) {
    h += '<div class="card"><h3>🏰 可结盟的玩家势力</h3>';
    for (const x of playerFs) {
      h += '<div class="form-row"><span class="lg-dot" style="display:inline-block;background:' + safeColor(x.color) + '"></span> ' +
        esc(x.name) + "（声望 " + x.reputation + " · 据点 " + x.node_count + "）" +
        '<button class="btn small" data-act="faction-apply" data-id="' + x.id + '">申请加入联盟</button></div>';
    }
    h += "</div>";
  }
  return h;
}

// ---------------------------------------------------------------------------
// 坊市（P3 §5.5：互市/商路/粮队/匪寨/流将）
// ---------------------------------------------------------------------------
const ITEM_NAMES = { grain: "粮草", iron: "精铁", recruit_token: "募将令", intel: "情报" };

function marketItemName(key) {
  return ((S.meta || {}).resource_names || {})[key] || ITEM_NAMES[key] || key;
}

function marketFactionName(fid) {
  const f = ((S || {}).factions || []).find((x) => Number(x.id) === Number(fid));
  return f ? f.name : "势力#" + fid;
}

function marketHeld(item) {
  const p = (S && S.player) || {};
  if (item === "intel") return Number(p.intel || 0);
  return Number(p[item] || 0);
}

function marketBand(item) {
  const mcfg = (S.meta || {}).market_cfg || {};
  const base = Number((mcfg.base_prices || {})[item] || 0);
  const band = Number(mcfg.player_price_band_pct != null ? mcfg.player_price_band_pct : 0.5);
  if (!base) return { lo: 1, hi: 0, base: 0, band: band };
  const lo = Math.max(1, Math.floor(base * (1 - band)));
  const hi = Math.max(lo, Math.floor(base * (1 + band)));
  return { lo, hi, base, band };
}

function updateConvoyEscortCost() {
  const el = document.getElementById("cvEscortCost");
  if (!el) return;
  const typeSel = document.getElementById("cvEscortType");
  const troopsInput = document.getElementById("cvEscortTroops");
  const tt = ((S.meta || {}).troop_types || {})[typeSel ? typeSel.value : ""] || {};
  const troops = Math.max(0, Number((troopsInput || {}).value || 0));
  if (!troops) {
    el.textContent = "额外护军按编军机制计费（铜钱+粮草），留空或填 0 表示不额外征召。";
    return;
  }
  const coin = Math.floor(troops * Number(tt.recruit_coin || 0) / 10);
  const grain = Math.floor(troops * Number(tt.recruit_grain || 0) / 10);
  el.textContent = "额外护军预计消耗：铜钱 " + fmtNum(coin) + " · 粮草 " +
    fmtNum(grain) + "（按编军机制，实际以结算时势力折扣为准）";
}

function updateMarketBandHint() {
  const sel = document.getElementById("mkItem");
  if (!sel) return;
  const band = marketBand(sel.value);
  const price = document.getElementById("mkPrice");
  if (price) {
    price.min = band.lo || 1;
    if (band.hi) price.max = band.hi;
    else price.removeAttribute("max");
  }
  const el = document.getElementById("mkRange");
  if (!el) return;
  el.textContent = band.hi
    ? "市场价 " + band.base + " · 可定价 " + band.lo + "~" + band.hi +
      "（±" + Math.round(band.band * 100) + "%）"
    : "该资源未配置市场价，可自由定价";
}

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
        fmtRemain(a.expire_at - srvNowSec()) + "</span></div>" +
        ((a.trait_names || []).length
          ? '<div class="sub trait-line">特技：' +
            traitChips(a.traits, a.trait_names) + "</div>" : "") +
        ((((a.personality || {}).top) || ((a.personality || {}).negative))
          ? '<div class="sub trait-line">个性：' +
            personalityChipHtml("top", (a.personality || {}).top, "", "") +
            personalityChipHtml("negative", (a.personality || {}).negative, "", "") +
            "</div>" : "") +
        '<div class="sub">底价 ' + fmtNum(a.start_price) + " 战功 · 你的暗标 " +
        (own > 0 ? fmtNum(own) + "（仅你可见）" : "未出价") +
        " · " + (a.bidder_count > 0 ? a.bidder_count + " 人已出价" : "暂无人出价") + "</div>" +
        '<div class="sub">' + esc(quotaText) + " · 坊市归属：" +
        esc(a.controller_name || "无主") +
        (a.access === "priority" && a.priority_until
          ? '（优先期剩余 <span class="aucLeft" data-expire="' + a.priority_until + '">' +
            fmtRemain(a.priority_until - srvNowSec()) + "</span>）"
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
  // --- 我的挂单 + NPC 收购报价 ---
  const orders = p.market_orders || [];
  const myOrders = orders.filter((o) => o.mine);
  const offers = p.market_offers || [];
  const nowSec = srvNowSec();
  h += '<div class="card"><h3>📦 我的挂单与 NPC 报价</h3>';
  if (myOrders.length) {
    h += '<table class="tbl"><tr><th>据点</th><th>商品</th><th>数量</th><th>单价</th><th>剩余</th><th></th></tr>';
    for (const o of myOrders) {
      h += "<tr><td>" + esc((nodeById(o.node_id) || {}).name || "#" + o.node_id) +
        "</td><td>" + esc(marketItemName(o.item_type)) + "</td><td>" + fmtNum(o.amount) +
        "</td><td>" + fmtNum(o.unit_price) + '</td><td><span class="aucLeft" data-expire="' +
        o.expires_at + '">' + fmtRemain(Number(o.expires_at || 0) - nowSec) + "</span></td><td>" +
        '<button class="btn small" data-act="market-cancel" data-id="' + o.order_id + '">撤单</button>' +
        "</td></tr>";
    }
    h += "</table>";
  } else {
    h += '<div class="sub">（暂无我的挂单）</div>';
  }
  if (offers.length) {
    h += '<div class="sub" style="margin-top:8px">NPC 收购报价（接受即成交，卖家承担交易税）：</div>' +
      '<table class="tbl"><tr><th>买家</th><th>商品</th><th>数量</th><th>单价</th><th>实收</th><th>剩余</th><th></th></tr>';
    for (const f of offers) {
      h += "<tr><td>" + esc(f.buyer_name || "坊市行商") +
        "</td><td>" + esc(marketItemName(f.item_type)) +
        "</td><td>" + fmtNum(f.amount) + "</td><td>" + fmtNum(f.unit_price) +
        "</td><td>" + fmtNum(f.payout) + ' <span class="sub">(税 ' + fmtNum(f.tax) + ")</span>" +
        '</td><td><span class="aucLeft" data-expire="' + f.expires_at + '">' +
        fmtRemain(Number(f.expires_at || 0) - nowSec) + "</span></td><td>" +
        '<button class="btn small primary" data-act="market-offer-accept" data-id="' + f.offer_id + '">接受</button> ' +
        '<button class="btn small" data-act="market-offer-decline" data-id="' + f.offer_id + '">拒绝</button>' +
        "</td></tr>";
    }
    h += "</table>";
  } else {
    h += '<div class="sub">（暂无 NPC 收购报价；挂单后行商会不定期发来带波动的报价）</div>';
  }
  h += "</div>";
  // --- 全服在售挂单 ---
  h += '<div class="card"><h3>🏪 互市挂单</h3>';
  if (orders.length) {
    const crossOnly = ((S.meta || {}).market_cfg || {}).cross_faction_market_only !== false;
    h += '<table class="tbl"><tr><th>据点</th><th>商品</th><th>数量</th><th>单价</th><th>剩余</th><th>卖家</th><th></th></tr>';
    for (const o of orders) {
      const seller = o.npc
        ? "行商·" + marketFactionName(o.seller_faction)
        : (o.mine ? "我" : marketFactionName(o.seller_faction));
      const sameFaction = !o.npc && !o.mine &&
        Number(o.seller_faction) === Number(myFactionId());
      h += "<tr><td>" + esc((nodeById(o.node_id) || {}).name || "#" + o.node_id) +
        "</td><td>" + esc(marketItemName(o.item_type)) + "</td><td>" + fmtNum(o.amount) +
        "</td><td>" + fmtNum(o.unit_price) + '</td><td><span class="aucLeft" data-expire="' +
        o.expires_at + '">' + fmtRemain(Number(o.expires_at || 0) - nowSec) + "</span></td><td>" +
        esc(seller) + "</td><td>" +
        (o.mine
          ? '<button class="btn small" data-act="market-cancel" data-id="' + o.order_id + '">撤单</button>'
          : (crossOnly && sameFaction
            ? '<span class="sub">同势力不可购买</span>'
            : '<button class="btn small primary" data-act="market-buy" data-id="' + o.order_id + '" data-max="' + o.amount + '">买入</button>')) +
        "</td></tr>";
    }
    h += "</table>";
  } else h += '<div class="sub">（当前无挂单）</div>';
  h += "</div>";
  // --- 资源上架 ---
  const mkNodes = (S.nodes || []).filter((n) => ["market", "city"].includes(n.type));
  const mkItems = ((S.meta || {}).market_cfg || {}).items || Object.keys(ITEM_NAMES);
  h += '<div class="card"><h3>📤 资源上架</h3>';
  if (mkNodes.length) {
    h += '<div class="sub">选择仙城或坊市挂单（交战势力坊市不可挂）；定价只能落在市场价的一定波动范围内，' +
      "挂单到期自动下架并退回资源。</div>" +
      '<div class="form-row"><select id="mkNode" style="max-width:150px">' +
      mkNodes.map((n) => '<option value="' + n.id + '">' + esc(n.name) +
        (n.owner ? "（" + esc(n.owner.name || "势力#" + n.owner.id) + "）" : "（无主）") +
        "</option>").join("") +
      '</select><select id="mkItem" style="max-width:90px">' +
      mkItems.map((k) => '<option value="' + k + '">' + esc(marketItemName(k)) +
        "（持有 " + fmtNum(marketHeld(k)) + "）</option>").join("") +
      '</select><input id="mkAmount" type="number" min="1" placeholder="数量" style="max-width:80px">' +
      '<input id="mkPrice" type="number" min="1" placeholder="单价(币)" style="max-width:90px">' +
      '<button class="btn small primary" data-act="market-list">挂售</button></div>' +
      '<div class="sub" id="mkRange"></div>';
  } else h += '<div class="sub">（世界内没有仙城/坊市，暂时无法挂售）</div>';
  h += "</div>";
  // --- 商路 ---
  const allRoutes = S.trade_routes || [];
  const routes = myTradeRoutes(allRoutes);
  const trCfg = S.trade_route_cfg || {};
  const trCoinNum = Number(trCfg.coin_per_node != null ? trCfg.coin_per_node
    : (trCfg.coin_per_tick != null ? trCfg.coin_per_tick : 20));
  const trCoin = fmtNum(trCoinNum);
  const sum = tradeRouteBonusSummary(routes);
  const taskPct = Math.round(sum.task * 100);
  const outPct = Math.round(sum.outMaxNode * 100);
  h += '<div class="card"><h3>🛤️ 商路</h3>' +
    '<div class="sub">自动开通：己方/联盟成员势力据点全程连通两个坊市或仙城。每条商路每 tick +' +
    trCoin + " 币 × 途经据点数量（含两端与中间节点，路径越长收益越高，归起点所属势力）。" +
    "商路数量与总长度越大，贸易任务与途经据点自然产出加成越高。</div>" +
    '<div class="sub">当前本方/联盟成员势力：<b>' + sum.count + '</b> 条商路 · 总长 <b>' +
    sum.edges + '</b> 段 → 贸易任务 <b>+' + taskPct + '%</b>，途经据点自然产出最高 <b>+' +
    outPct + '%</b></div>';
  if (routes.length) {
    h += routes.map((r) => {
      const nodeCount = (r.path || []).length || 2;
      return '<div class="sub">• ' + esc(r.a_name) + " ⇄ " + esc(r.b_name) +
        "（" + (r.edge_count != null ? r.edge_count : Math.max(0, nodeCount - 1)) +
        " 段 · 收益 +" + fmtNum(trCoinNum * nodeCount) +
        " 币 · " + esc((r.path_names || []).join(" → ")) + "）</div>";
    }).join("");
  } else {
    h += '<div class="sub">（暂无商路：占领并连通两个坊市/仙城即可自动开通）</div>';
  }
  h += "</div>";
  // --- 运粮队 ---
  const convoyCfg = S.convoy_cfg || {};
  const baseGuard = fmtNum(convoyCfg.troops != null ? convoyCfg.troops : 100);
  const escortTypeDefault = convoyCfg.escort_troop_type || "sword_shield";
  h += '<div class="card"><h3>🚚 运粮队</h3>' +
    '<div class="sub">粮队自带护卫 ' + baseGuard +
    " 兵；可另派空闲军团护送（护送期间军团随队移动、不可出征/解散），" +
    "也可额外指定护送军队人数，按编军机制消耗铜钱/粮草，抵达或被截后不返还。</div>";
  const convoys = p.convoys || [];
  if (convoys.length) {
    h += convoys.map((c) => {
      if (c.kind === "army_supply") {
        return '<div class="sub">🚚 补粮队 · ' + fmtNum(c.grain) + " 粮 · 追击 " +
          esc(c.to || "?") + "（现于 " + esc(c.cur_node || "?") + " · " +
          esc(c.progress || "追击中") + "）</div>";
      }
      return '<div class="sub">🌾 ' + fmtNum(c.grain) + " 粮 · 护送 " +
        fmtNum(c.escort_troops || 0) + " 兵" +
        (c.escort_paid_troops ? "（含征召 " + fmtNum(c.escort_paid_troops) + "）" : "") +
        " · " + esc(c.from || "?") + " → " +
        esc(c.to || "?") + "（现于 " + esc(c.cur_node || "?") + " · 进度 " +
        esc(c.progress || "?") + "）</div>";
    }).join("");
  } else h += '<div class="sub">（无在途粮队；粮队沿本方/联盟成员势力通路自动行进，可能被匪寨/敌军劫掠）</div>';
  const srcs = myNodes();
  const dests = friendlyNodes();
  if (srcs.length && dests.length > 1) {
    const escorts = (p.armies || []).filter((a) => a.state === "idle");
    const troopTypes = S.meta.troop_types || {};
    h += '<div class="form-row"><select id="cvFrom" style="max-width:110px">' +
      srcs.map((n) => '<option value="' + n.id + '">' + esc(n.name) + "</option>").join("") +
      '</select>→<select id="cvTo" style="max-width:130px">' +
      dests.map((n) => '<option value="' + n.id + '">' + esc(n.name) +
        (n.owner && n.owner.id !== myFactionId() ? "（联盟成员势力）" : "") + "</option>").join("") +
      '</select><input id="cvGrain" type="number" min="1" placeholder="粮草" style="max-width:80px">' +
      '<select id="cvEscort" style="max-width:150px"><option value="0">无军团护送</option>' +
      escorts.map((a) => '<option value="' + a.id + '">' + esc(a.name) +
        "（" + esc(a.node_name || "?") + "）</option>").join("") +
      '</select><button class="btn small primary" data-act="send-convoy">发粮队</button></div>' +
      '<div class="form-row"><span class="sub">额外护军：</span>' +
      '<select id="cvEscortType" style="max-width:130px">' +
      Object.entries(troopTypes).map(([k, t]) => '<option value="' + k + '"' +
        (k === escortTypeDefault ? " selected" : "") + ">" + esc(t.name) + "</option>").join("") +
      '</select><input id="cvEscortTroops" type="number" min="0" step="100" value="0" ' +
      'placeholder="护军人数" style="max-width:100px"></div>' +
      '<div class="sub" id="cvEscortCost"></div>';
  } else {
    h += '<div class="sub">（需至少 2 个己方/联盟成员势力据点才能派出运粮队）</div>';
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
    if (Number(boost.until || 0) > srvNowSec()) {
      h += '<div class="sub">⚡ 当前开局加成：产出 ×' + (boost.mult || 1.2) + "，剩余 " +
        fmtRemain(Number(boost.until) - srvNowSec()) + "</div>";
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
  h += '<div class="card"><h3>🎯 演武擂台</h3><div class="sub">摆擂押铜钱，全服应战；胜者通吃（1 小时有效，平局各自收回）。摆擂/应战都要求武将位于建有擂将台的据点。</div>';
  const duels = p.duels || [];
  if (duels.length) {
    const arenaGens = ((p.chars || []).filter(duelEligible));
    for (const d of duels) {
      const arenaNodeId = Number(d.node_id || 0);
      const arenaName = arenaNodeId
        ? (nodeNameById(arenaNodeId) || ("据点#" + arenaNodeId))
        : "未知据点";
      h += '<div class="form-row duel-row">' + (d.mine ? "🔵 我的擂台：" : "🔴 ") +
        esc(d.challenger || "?") + " 的 " + esc(d.char_name || "?") +
        "（赌注 " + fmtNum(d.stake) + " 铜钱）" +
        '<span class="duel-node">📍 ' + esc(arenaName) + "</span>";
      if (!d.mine) {
        const here = arenaNodeId
          ? arenaGens.filter((g) => charAtNode(g, arenaNodeId))
          : arenaGens;
        if (here.length) {
          h += '<select id="duelChar_' + esc(d.duel_id) + '" style="max-width:130px">' +
            here.map((g) => '<option value="' + g.id + '">' + esc(g.name) +
              "（武勇 " + Number((g.stats || {}).valor || 0) + "）</option>").join("") +
            '</select><button class="btn small primary" data-act="duel-accept" data-id="' +
            esc(d.duel_id) + '">应战</button>';
        } else {
          const movable = arenaGens.filter((g) => g.can_move &&
            (!arenaNodeId || !charAtNode(g, arenaNodeId)));
          h += '<span class="sub">先传送到「' + esc(arenaName) + "」</span>";
          if (movable.length && arenaNodeId) {
            h += '<button class="btn small" data-act="duel-move" data-id="' + esc(d.duel_id) +
              '" data-node="' + arenaNodeId + '">🚶 传送武将至此</button>';
          } else {
            h += '<span class="sub">（无可应战的空闲武将：需武将、无伤、武勇 ≥75）</span>';
          }
        }
      }
      h += "</div>";
    }
    h += '<div class="sub">应战武将必须与擂台处于同一据点；擂台据点由摆擂者所在地决定。</div>';
  } else h += '<div class="sub">（当前无人摆擂；在「人员」页武将卡片可摆擂，需位于有擂将台的据点）</div>';
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
      if (act === "logs-filter") {
        logState.kind = btn.dataset.kind || "";
        logState.beforeId = 0;
        logState.items = [];
        loadWorldLogs(false);
        return;
      }
      if (act === "logs-more") {
        loadWorldLogs(true);
        return;
      }
      if (act === "captive-detail") {
        openCaptiveDetail(id);
        return;
      }
      if (act === "execute-captive") {
        if (confirm("确认处决该俘虏？将获得战功并威慑同牢俘虏，但会与原势力交恶（不可撤销）。")) {
          doAction("execute_captive", { char_id: id });
        }
        return;
      }
      if (act === "sell-captive") {
        if (confirm("确认将该俘虏卖往坊市？可获得铜钱，但角色将转为在野流将（不可撤销）。")) {
          doAction("sell_captive", { char_id: id });
        }
        return;
      }
      if (act === "recruit") doAction("recruit", { index: Number(btn.dataset.i) });
      else if (act === "recruit-premium") doAction("recruit", { index: Number(btn.dataset.i), premium: true });
      else if (act === "dismiss-char") {
        const name = btn.dataset.name || "该角色";
        if (confirm("确认遣散 " + name + "？该操作不可撤销，将腾出 1 个名册名额。")) {
          doAction("dismiss_char", { char_id: Number(btn.dataset.id) });
        }
      }
      else if (act === "reward-char") {
        const lr = ((S.player || {}).loyalty_reward) || {};
        const name = btn.dataset.name || "该角色";
        if (confirm("奖赏 " + name + "？消耗 " + Number(lr.cost_coin || 0) +
          " 铜钱，忠诚 +" + Number(lr.gain || 5) + "。")) {
          doAction("reward_char", { char_id: Number(btn.dataset.id) });
        }
      }
      else if (act === "reward-all") {
        const lr = ((S.player || {}).loyalty_reward) || {};
        const elig = (((S.player || {}).chars) || []).filter((c) => c.can_reward);
        if (!elig.length) {
          toast("没有需要奖赏的角色", "err");
          return;
        }
        if (confirm("一键奖赏 " + elig.length + " 名角色？预计消耗 " +
          fmtNum(Number(lr.cost_coin || 0) * elig.length) +
          " 铜钱（受每日次数与余额限制，按实际结算）。")) {
          doAction("reward_all", {});
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
        const c = ((S.player || {}).chars || []).find((x) => Number(x.id) === id);
        if (c && !charAtArena(c)) {
          toast("需先传送到建有擂将台的己方/联盟成员势力据点再摆擂", "err");
          return;
        }
        if (c && !duelEligible(c)) {
          toast("摆擂需武将、无伤且武勇 ≥75", "err");
          return;
        }
        const dc = (S.meta || {}).duel_cfg || {};
        const lo = Number(dc.arena_stake_min || 100);
        const hi = Number(dc.arena_stake_max || 100000);
        const stake = prompt("摆擂赌注（铜钱，" + lo + "~" + hi + "）：", String(lo));
        if (stake) doAction("duel_arena_open", { char_id: id, stake: Number(stake) });
      } else if (act === "duel-accept") {
        const sel = document.getElementById("duelChar_" + btn.dataset.id);
        const cid = Number(sel ? sel.value : 0);
        if (!cid) { toast("没有位于擂台据点的空闲武将可应战", "err"); return; }
        doAction("duel_arena_accept", { duel_id: btn.dataset.id, char_id: cid });
      } else if (act === "duel-move") {
        const nodeId = Number(btn.dataset.node || 0);
        const gens = ((S.player || {}).chars || []).filter((c) =>
          duelEligible(c) && c.can_move && !charAtNode(c, nodeId));
        if (!gens.length) { toast("没有可传送到擂台据点的空闲武将", "err"); return; }
        let pick = gens[0];
        if (gens.length > 1) {
          const sel = prompt("选择要传送到擂台的武将（输入序号）：\n" +
            gens.map((c, i) => (i + 1) + ". " + c.name + "（武勇 " +
              Number((c.stats || {}).valor || 0) + "）").join("\n"), "1");
          if (!sel) return;
          pick = gens[Number(sel) - 1];
          if (!pick) return;
        }
        doAction("move_char", { char_id: pick.id, node_id: nodeId });
      }
      // --- P1 势力治理 ---
      else if (act === "approve") doAction("faction_approve", { applicant_user: btn.dataset.u });
      else if (act === "reject") doAction("faction_reject", { applicant_user: btn.dataset.u });
      else if (act === "recall-task") {
        const name = btn.dataset.name || "该角色";
        const kind = btn.dataset.kind || "normal";
        const msg = kind === "sustained"
          ? "停止 " + name + " 的持续任务？已满整小时的收益已结算，未满 1 小时部分按比例结算。"
          : kind === "supply"
            ? "召回 " + name + " 的补粮队？未送达的粮草将退回。"
            : kind === "expedition"
              ? "停止 " + name + " 的远征任务？中途停止不结算任何收益。"
              : "召回 " + name + "？已进行部分按比例结算。";
        if (confirm(msg)) {
          doAction("recall_task", { task_id: Number(btn.dataset.id) });
        }
      } else if (act === "move-open") {
        openMoveModal(id, btn.dataset.name || "");
      } else if (act === "recruit-captive") {
        const modal = btn.dataset.modal === "1";
        const sel = document.getElementById(
          "recruitNode_" + (modal ? "m_" : "") + id);
        doAction("recruit_captive", {
          char_id: id,
          node_id: Number(sel ? sel.value : 0),
        });
      } else if (act === "set-position") {
        const sel = document.getElementById("pos_" + btn.dataset.u.replace(/[^a-zA-Z0-9_-]/g, "_"));
        doAction("faction_set_position", { target_user: btn.dataset.u, position: sel ? sel.value : "" });
      } else if (act === "appoint-char") {
        const charSel = document.getElementById("appointChar");
        const posSel = document.getElementById("appointPos");
        if (!charSel) return;
        doAction("faction_appoint_char", {
          char_id: Number(charSel.value),
          position: posSel ? posSel.value : "",
        });
      } else if (act === "transfer-leader") {
        const sel = document.getElementById("transferSel");
        const pf = (S.player || {}).faction || {};
        const leaderTitle = pf.is_alliance_host ? "盟主" : "势力主";
        if (sel && confirm("移交后你将失去" + leaderTitle + "权限，确认？")) {
          doAction("transfer_leader", { target_user: sel.value });
        }
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
      } else if (act === "tech-fold") {
        const line = btn.dataset.line;
        TECH_FOLD[line] = !TECH_FOLD[line];
        renderTab();
      } else if (act === "tech") doAction("tech_research", {
        line: btn.dataset.line,
        node_id: btn.dataset.node || "",
      });
      else if (act === "dispatch-plot") {
        const ptype = (document.getElementById("plotType") || {}).value || "";
        if (PLOT_TARGET[ptype] === "char") { toast("该计略目标为角色，请用下方专用按钮", "err"); return; }
        doAction("dispatch_plot", {
          char_id: Number((document.getElementById("plotChar") || {}).value || 0),
          plot_type: ptype,
          target_id: Number((document.getElementById("plotTarget") || {}).value || 0),
        });
      } else if (act === "alienate") {
        const cid = prompt("离间：输入敌方角色 ID（可在战报/人员中查看）：");
        if (cid) doAction("dispatch_plot", {
          char_id: Number((document.getElementById("plotChar") || {}).value || 0),
          plot_type: "alienate", target_id: Number(cid),
        });
      } else if (act === "rescue-plot") {
        const st = ((S.player || {}).chars || []).filter((c) =>
          c.role === "strategist" && c.status === "idle" &&
          !["dying", "dead"].includes(c.injury_state));
        if (!st.length) { toast("没有可用军师执行营救", "err"); return; }
        // char_id=0：由后端自动选智谋最高的健康军师
        doAction("dispatch_plot", { char_id: 0, plot_type: "rescue", target_id: id });
      }
      // --- 俘虏 ---
      else if (act === "persuade") {
        // 不指定说服者：由后端自动选择魅力+智谋最高的空闲角色。
        // 旧逻辑固定取列表第一个（可能重伤/低属性），会平白拉低成功率。
        doAction("persuade", { char_id: id, persuader_char_id: 0 });
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
        if (confirm("迁都将扣个人铜钱并进入冷却，确认？")) {
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
        if (!name) { toast("请输入联盟名", "err"); return; }
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
      else if (act === "market-offer-accept") {
        if (confirm("接受该 NPC 收购报价？接受后立即成交，并扣除交易税。")) {
          doAction("market_offer_accept", { offer_id: id });
        }
      } else if (act === "market-offer-decline") {
        doAction("market_offer_decline", { offer_id: id });
      } else if (act === "market-list") {
        const item = (document.getElementById("mkItem") || {}).value || "grain";
        const amount = Number((document.getElementById("mkAmount") || {}).value || 0);
        const price = Number((document.getElementById("mkPrice") || {}).value || 0);
        const band = marketBand(item);
        const held = marketHeld(item);
        if (!amount || amount <= 0) { toast("请输入挂售数量", "err"); return; }
        if (amount > held) { toast("持有不足（现有 " + held + "）", "err"); return; }
        if (!price || price <= 0) { toast("请输入单价", "err"); return; }
        if (band.hi && (price < band.lo || price > band.hi)) {
          toast("定价须在市场价 " + band.lo + "~" + band.hi + " 之间", "err");
          return;
        }
        doAction("market_list", {
          node_id: Number((document.getElementById("mkNode") || {}).value || 0),
          item_type: item, amount: amount, unit_price: price,
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
          escort_troops: Number((document.getElementById("cvEscortTroops") || {}).value || 0),
          escort_troop_type: (document.getElementById("cvEscortType") || {}).value || "",
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
  const mkItemSel = document.getElementById("mkItem");
  if (mkItemSel) {
    mkItemSel.addEventListener("change", updateMarketBandHint);
    updateMarketBandHint();
  }
  const cvTroops = document.getElementById("cvEscortTroops");
  const cvType = document.getElementById("cvEscortType");
  if (cvTroops || cvType) {
    [cvTroops, cvType].forEach((el) => {
      if (!el) return;
      el.addEventListener("change", updateConvoyEscortCost);
      el.addEventListener("input", updateConvoyEscortCost);
    });
    updateConvoyEscortCost();
  }
}

// ---------------------------------------------------------------------------
// 编军弹窗
// ---------------------------------------------------------------------------
let armyModalNode = 0;

function openArmyModal(nodeId) {
  const p = S.player || {};
  const n = nodeById(nodeId);
  const nodeName = n ? n.name : ("#" + nodeId);
  const idle = charsAtNode(p, nodeId);
  const allIdle = idleAliveChars(p);
  // v4.5.1：主将可为内政官或重伤角色（濒死强制静养）；随行仍须能行动
  const leaders = allIdle.filter((c) => charAtNode(c, nodeId));
  if (!leaders.length) {
    toast("「" + nodeName + "」无驻守角色：先到「人员」页把角色传送至此城再编军", "err");
    return;
  }
  armyModalNode = nodeId;
  const hint = document.getElementById("armyHint");
  if (hint) {
    hint.textContent = "仅列出位于「" + nodeName + "」的空闲角色；其他角色请先在「人员」页传送。" +
      (allIdle.length > leaders.length ? "（另有 " + (allIdle.length - leaders.length) + " 名空闲角色不在本据点）" : "") +
      "（主将可为内政官/重伤角色，濒死不可；随行须健康或轻伤；主将 1 + 副将最多 2 人；" +
      "重伤主将出征/操练需至少 1 名健康或轻伤随行）" +
      (n && n.owner && n.owner.id !== myFactionId()
        ? "（联盟成员势力据点：编成后需回己方据点才能解散/补员）" : "");
  }
  document.getElementById("armyLeader").innerHTML = leaders.map((c) =>
    '<option value="' + c.id + '">' + esc(c.name) + "（" + esc(c.role_name) + " 武勇" +
    ((c.stats || {}).valor || 0) +
    (c.injury_state && c.injury_state !== "healthy"
      ? "·" + esc(c.injury_name || c.injury_state) : "") + "）</option>").join("");
  document.getElementById("armyMembers").innerHTML = idle
    .filter((c) => c.injury_state === "healthy" || c.injury_state === "light")
    .map((c) =>
      '<option value="' + c.id + '">' + esc(c.name) + "（" + esc(c.role_name) + "）</option>").join("");
  document.getElementById("armyTroopType").innerHTML = Object.entries(S.meta.troop_types).map(([k, t]) =>
    '<option value="' + k + '">' + t.name + "（攻" + t.atk + "/防" + t.def + "/机动" + t.move + " · " + t.skill + "）</option>").join("");
  document.getElementById("armyOverlay").hidden = false;
  updateArmyCost();
}

let armyCostTimer = null;
let armyCostSeq = 0;

// 编军预算：先用兵种基础价立即给出估算，再向后端要精确口径
// （势力动员令/科技折扣、破城槌折扣、自动携粮上限、带兵上限）。
function updateArmyCost() {
  const el = document.getElementById("armyCost");
  if (!el) return;
  const typeSel = document.getElementById("armyTroopType");
  const tt = ((S.meta || {}).troop_types || {})[typeSel ? typeSel.value : ""] || {};
  const troops = Math.max(0, Number((document.getElementById("armyTroops") || {}).value || 0));
  const engine = !!(document.getElementById("armyEngine") || {}).checked;
  const coin = Math.floor(troops * Number(tt.recruit_coin || 0) / 10) + (engine ? 3000 : 0);
  const grain = Math.floor(troops * Number(tt.recruit_grain || 0) / 10);
  el.classList.remove("warn");
  el.textContent = "预计消耗：铜钱 " + fmtNum(coin) + " · 粮草 " + fmtNum(grain) +
    (engine ? "（含破城槌 3000）" : "") + " · 核算折扣中…";
  const seq = ++armyCostSeq;
  if (armyCostTimer) clearTimeout(armyCostTimer);
  armyCostTimer = setTimeout(() => refreshArmyCost(seq), 300);
}

async function refreshArmyCost(seq) {
  if (seq !== armyCostSeq) return;
  const overlay = document.getElementById("armyOverlay");
  if (!overlay || overlay.hidden) return;
  const nodeId = Number(armyModalNode || 0);
  const leaderSel = document.getElementById("armyLeader");
  const leaderId = Number((leaderSel || {}).value || 0);
  if (!nodeId || !leaderId) return;
  const params = {
    node_id: nodeId,
    leader_char_id: leaderId,
    troop_type: document.getElementById("armyTroopType").value,
    troops: Number(document.getElementById("armyTroops").value) || 0,
    siege_engine: !!(document.getElementById("armyEngine") || {}).checked,
  };
  try {
    const r = await api("/api/strategy/action", {
      method: "POST", body: JSON.stringify({ action: "army_cost_preview", params }),
    }, true);
    if (seq !== armyCostSeq) return;
    if ((document.getElementById("armyOverlay") || {}).hidden) return;
    renderArmyQuote(r && r.quote);
  } catch (e) {
    // 预览失败（世界推进/网络抖动）保留基础估算，不影响正式编军
  }
}

function renderArmyQuote(q) {
  const el = document.getElementById("armyCost");
  if (!el || !q || !q.troop_name) return;
  const lines = [];
  lines.push("预计消耗：铜钱 <b>" + fmtNum(q.coin_cost) + "</b> · 粮草 <b>" +
    fmtNum(q.recruit_grain) + "</b>" +
    (q.ram_cost ? "（含破城槌 " + fmtNum(q.ram_cost) + " 铜钱）" : ""));
  lines.push("自动携粮 " + fmtNum(q.carry) + " / 上限 " + fmtNum(q.carry_cap) +
    " · 合计扣粮 " + fmtNum(q.total_grain));
  lines.push("现有铜钱 " + fmtNum(q.stock_coin) + " / 粮草 " + fmtNum(q.stock_grain) +
    " · 主将「" + esc(q.leader_name || "") + "」带兵上限 " + fmtNum(q.cap));
  const warn = [];
  if (q.troops < Number(q.min_troops || 0)) {
    warn.push("兵力至少 " + fmtNum(q.min_troops));
  }
  if (q.troops > Number(q.cap || 0)) {
    warn.push("超过主将带兵上限 " + fmtNum(q.cap));
  }
  if (q.overstaffed) {
    warn.push("超编（统率×20 = " + fmtNum(q.overstaff_limit) + "），士气恢复 -10%");
  }
  if (Number(q.lacking_coin) > 0 || Number(q.lacking_grain) > 0) {
    const lack = [];
    if (Number(q.lacking_coin) > 0) lack.push("铜钱 " + fmtNum(q.lacking_coin));
    if (Number(q.lacking_grain) > 0) lack.push("粮草 " + fmtNum(q.lacking_grain));
    warn.push("资源不足，还差 " + lack.join(" / "));
  } else if (q.affordable_troops != null && q.troops > 0 &&
             Number(q.affordable_troops) < Math.min(q.troops, Number(q.cap || q.troops))) {
    warn.push("按现有资源最多可编 " + fmtNum(q.affordable_troops) + " 兵");
  }
  if (warn.length) {
    lines.push('<span class="cost-warn">⚠ ' + warn.join("；") + "</span>");
  }
  el.innerHTML = lines.join("<br>");
  el.classList.toggle("warn", warn.length > 0);
}

["armyTroopType", "armyTroops", "armyEngine", "armyLeader"].forEach((id) => {
  const node = document.getElementById(id);
  if (!node) return;
  node.addEventListener("change", updateArmyCost);
  node.addEventListener("input", updateArmyCost);
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
  }).then((r) => {
    // 仅在编成成功时关闭弹窗：资源不足等失败会保留输入，方便调整兵力
    if (r) document.getElementById("armyOverlay").hidden = true;
  });
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

document.getElementById("btnJoinBrowse").addEventListener("click", () => {
  // 未入场也能先查阅「天下」公开情报与「指引」；轮询不再反复弹遮罩。
  joinBrowse = true;
  document.getElementById("joinOverlay").hidden = true;
  activateTab("world");
});

document.getElementById("btnJoinOpen").addEventListener("click", () => {
  joinBrowse = false;
  document.getElementById("joinOverlay").hidden = false;
  renderJoinCandidates();
});

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
// 侧栏切页/表格宽度变化会改变地图容器尺寸，但不会触发 window.resize；
// 不重算 view 时 canvas 仍是旧比例，点击据点会偏移甚至落到地图外。
if (window.ResizeObserver && canvas.parentElement) {
  new ResizeObserver(() => resizeCanvas()).observe(canvas.parentElement);
}

function renderAll() {
  renderTop();
  renderLegend();
  drawMap();
  renderDrawer();
  renderTab();
  renderBattleAlert();
}

// ---------------------------------------------------------------------------
// 据点抽屉：赈济 / 建筑（事件委托，抽屉重绘后依然有效）
// ---------------------------------------------------------------------------
// 俘虏详情弹窗：点击遮罩/关闭按钮隐藏；点处置按钮后也立即收起旧快照，
// 等 doAction 刷新后的状态重新渲染。
function initCaptiveDetail() {
  const overlay = document.getElementById("captiveOverlay");
  if (!overlay) return;
  const hide = () => { overlay.hidden = true; };
  const close = document.getElementById("captiveDetailClose");
  if (close) close.addEventListener("click", hide);
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay) { hide(); return; }
    if (ev.target.closest && ev.target.closest("[data-act]")) hide();
  });
}

// 特技 / 个性详情弹窗：徽标由 traitChips / personalityChipHtml 生成，
// 这里统一做事件委托，20s 轮询重绘（页面、抽屉、弹窗）后无需重新绑定。
function initTraitPopup() {
  const overlay = document.getElementById("traitOverlay");
  if (!overlay) return;
  const hide = () => { overlay.hidden = true; };
  ["traitClose", "traitOk"].forEach((id) => {
    const btn = document.getElementById(id);
    if (btn) btn.addEventListener("click", hide);
  });
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay) hide();
  });
  document.addEventListener("click", (ev) => {
    const chip = ev.target && ev.target.closest
      ? ev.target.closest(".trait-chip") : null;
    if (!chip) return;
    ev.preventDefault();
    const kind = chip.getAttribute("data-trait-kind") || "特技";
    const name = chip.getAttribute("data-trait-name") || "";
    const desc = chip.getAttribute("data-trait-desc") || "";
    document.getElementById("traitKind").textContent = kind;
    document.getElementById("traitTitle").textContent = kind + "详情";
    document.getElementById("traitName").textContent = name;
    document.getElementById("traitDesc").textContent = desc || "暂无详细说明";
    overlay.hidden = false;
    const ok = document.getElementById("traitOk");
    if (ok) ok.focus({ preventScroll: true });
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !overlay.hidden) hide();
  });
}

function initDrawerEvents() {
  const drawer = document.getElementById("nodeDrawer");
  if (!drawer) return;  drawer.addEventListener("click", (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest("[data-act]") : null;
    if (!btn) return;
    const act = btn.dataset.act;
    const idNum = Number(btn.dataset.id || 0);
    // data-node 优先：驻守列表的「传送到此」按钮 data-id 是角色 id，
    // 不能与据点 id 混用。
    const nodeId = Number(btn.dataset.node || idNum || 0);
    if (!nodeId) return;
    if (act === "relief") {
      doAction("govern_relief", { node_id: nodeId, resource: btn.dataset.res || "coin" });
    } else if (act === "upgrade-node") {
      doAction("upgrade_node", { node_id: nodeId });
    } else if (act === "move-char") {
      doAction("move_char", { char_id: idNum, node_id: nodeId });
    } else if (act === "move-char-sel") {
      const sel = document.getElementById("moveSel_" + nodeId);
      if (!sel || !sel.value) { toast("请选择要传送的角色", "err"); return; }
      doAction("move_char", { char_id: Number(sel.value), node_id: nodeId });
    } else if (act === "appoint-taishou") {
      const sel = document.getElementById("govChar_" + nodeId);
      if (!sel || !sel.value) return;
      doAction("appoint_taishou", { node_id: nodeId, char_id: Number(sel.value) });
    } else if (act === "dismiss-taishou") {
      if (confirm("确认卸任该据点太守？")) {
        doAction("dismiss_taishou", { node_id: nodeId });
      }
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
  initCaptiveDetail();
  initTraitPopup();
  resizeCanvas();
  await load();
  openBattleFromUrl();
  initStrategyChannel();
  setInterval(tickCountdown, 1000);
  // 页面重新可见时立即刷新
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) load();
  });
  // 战役覆盖层关闭后回到大地图并刷新（不再重置 seenManualBattles，
  // 否则每次关战场都会把手操开启提示重新播一遍）
  window.onBattleClosed = () => { load(); };
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
