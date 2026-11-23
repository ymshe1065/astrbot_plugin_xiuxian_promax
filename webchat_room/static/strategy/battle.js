/* 仙域争霸 · hex 手操战役视图（P2 §7，设计方案 v5）
 * 依赖 strategy.js 的 auth()/api()/toast()/esc()；挂载 window.BattleView。
 * 坐标：odd-q 偏移（奇数列下移半格），与 core/strategy_hex.py 一致。
 */
"use strict";

(function () {
  const POLL_MS = 3000;

  // 服务端时间基准（v4.6.5）：与 strategy.js 同口径，避免设备时钟偏差导致
  // 部署/回合倒计时提前归零或停滞。
  function srvNowSec() {
    return window.XiuXianClock ? XiuXianClock.nowSec()
      : Math.floor(Date.now() / 1000);
  }

  // 地形配色（与 strategy_hex.TERRAIN_* 对应）
  const TERRAIN_COLOR = {
    plain: "#1c2733", forest: "#17351f", highland: "#3a3320",
    river: "#122c47", swamp: "#22303a", wall: "#4a4a58",
    gate: "#6b5a2e", moat: "#14324a",
  };
  const TERRAIN_NAME = {
    plain: "平原", forest: "森林", highland: "高地", river: "河流",
    swamp: "沼泽", wall: "城墙", gate: "城门", moat: "护城河",
  };
  const MOVE_COST = {
    plain: 1, forest: 2, highland: 2, river: 3, swamp: 2,
    wall: 99, gate: 2, moat: 3,
  };
  // 技能元信息（core/strategy_hex.SKILLS 的前端投影）
  const SKILL_INFO = {
    fire: { name: "火攻", mana: 40, target: "hex", range: 3 },
    flood: { name: "水淹", mana: 50, target: "hex", range: 2 },
    confuse: { name: "惑敌", mana: 35, target: "unit", range: 4 },
    taunt: { name: "挑衅", mana: 25, target: "unit", range: 3 },
    bulwark: { name: "坚守阵", mana: 30, target: "self", range: 0 },
    heal: { name: "治疗术", mana: 30, target: "friendly", range: 2 },
    beastrush: { name: "兽群突袭", mana: 45, target: "empty", range: 2 },
    barrage: { name: "落石/箭雨", mana: 35, target: "hex", range: 3 },
    ram: { name: "破城槌撞击", mana: 0, target: "wall", range: 0 },
    combo: { name: "合击技", mana: 60, target: "unit", range: 2 },
    troop_special: { name: "兵种专属", mana: 0, target: "auto", range: 0 },
  };
  // 阵型（core/strategy_hex.FORMATIONS 的前端投影）
  const FORMATIONS = [
    ["", "无阵型"], ["fengshi", "锋矢阵（攻 +10%）"],
    ["heyi", "鹤翼阵（包抄）"], ["fangyuan", "方圆阵（防 +15%）"],
    ["yanhang", "雁行阵（远程 +10%）"],
  ];
  const FORMATION_NAMES = Object.fromEntries(FORMATIONS);

  let bs = null;              // battle_state 快照
  let refreshSeq = 0;         // 轮询请求序号：旧响应不得覆盖新快照
  let battleId = 0;
  let pollTimer = null;
  let tickTimer = null;
  let selectedUid = null;     // 选中的我方单位
  let mode = "select";        // select | deploy | move | attack | skill | ambush
  let skillId = "";
  let movePlan = null;        // {uid, cells: Map("q,r"->{q,r,cost,prev})}
  let hoverHex = null;
  let delegated = false;
  let lastActor = null;       // 上一帧的行动单位（换人时清理移动/技能选择）
  let spectator = false;      // §12.6 观战（只读，只轮询不提交）

  const canvas = document.getElementById("hexCanvas");
  const ctx = canvas.getContext("2d");
  // base* 为自适应尺寸；zoom/pan 由移动端手势与滚轮叠加，不改变六边形数据
  let view = {
    size: 26, ox: 0, oy: 0,
    baseSize: 26, baseOx: 0, baseOy: 0,
    zoom: 1, panX: 0, panY: 0, vw: 0, vh: 0,
  };

  // -------------------------------------------------------------------------
  // 几何（odd-q flat-top）
  // -------------------------------------------------------------------------
  function hexToPixel(q, r) {
    return [view.ox + view.size * 1.5 * q,
            view.oy + view.size * Math.sqrt(3) * (r + 0.5 * (q & 1))];
  }

  function cubeRound(x, y, z) {
    let rx = Math.round(x), ry = Math.round(y), rz = Math.round(z);
    const dx = Math.abs(rx - x), dy = Math.abs(ry - y), dz = Math.abs(rz - z);
    if (dx > dy && dx > dz) rx = -ry - rz;
    else if (dy > dz) ry = -rx - rz;
    else rz = -rx - ry;
    return [rx, ry, rz];
  }

  function pixelToHex(px, py) {
    const s = view.size;
    const x = (px - view.ox) / s, y = (py - view.oy) / s;
    const qf = (2 / 3 * x) / 1;
    const rf = (-1 / 3 * x + Math.sqrt(3) / 3 * y) / 1;
    // axial(flat-top odd-q 的分数近似) → cube round → odd-q
    const [cx, , cz] = cubeRound(qf, -qf - rf, rf);
    return [cx, cz + (cx - (cx & 1)) / 2];
  }

  function neighbors(q, r) {
    const dirs = q % 2 === 0
      ? [[+1, 0], [+1, -1], [0, -1], [-1, -1], [-1, 0], [0, +1]]
      : [[+1, +1], [+1, 0], [0, -1], [-1, 0], [-1, +1], [0, +1]];
    return dirs.map(([dq, dr]) => [q + dq, r + dr]);
  }

  function hexDist(a, b) {
    const ac = oddqToCube(a[0], a[1]), bc = oddqToCube(b[0], b[1]);
    return Math.max(Math.abs(ac[0] - bc[0]), Math.abs(ac[1] - bc[1]),
                    Math.abs(ac[2] - bc[2]));
  }

  function oddqToCube(q, r) {
    const x = q, z = r - ((q - (q & 1)) / 2), y = -x - z;
    return [x, y, z];
  }

  // -------------------------------------------------------------------------
  // 战场数据访问
  // -------------------------------------------------------------------------
  function terrainAt(q, r) {
    if (!bs || !bs.terrain) return "plain";
    // B17：水淹临时沼泽（temp_terrain: {"q,r": 到期回合}，与服务端 eff_terrain 一致）
    const tt = bs.temp_terrain || {};
    const k = q + "," + r;
    if (k in tt && Number(tt[k]) >= Number(bs.round || 0)) return "swamp";
    return bs.terrain[k] || "plain";
  }

  function inBounds(q, r) {
    return bs && q >= 0 && r >= 0 && q < bs.size[0] && r < bs.size[1];
  }

  function unitAt(q, r) {
    // B6：服务端 pos 契约为 [q, r] 数组（非 {q, r} 对象）
    return (bs.units || []).find((u) => u.pos && u.pos[0] === q && u.pos[1] === r
      && u.troops > 0 && !u.routed);
  }

  function myUnits() {
    return (bs.units || []).filter((u) => u.mine && u.troops > 0);
  }

  function passable(q, r, troopType) {
    if (!inBounds(q, r)) return false;
    const t = terrainAt(q, r);
    if (t === "wall") return false;
    if (t === "swamp" && troopType === "heavy_armor") return false;
    return true;
  }

  // Dijkstra 可达域（近似服务端规则；ZOC 截停由服务端裁决）
  function computeReachable(unit) {
    const meta = (typeof S !== "undefined" && S && S.meta) || {};
    const troop = (meta.troop_types || {})[unit.troop_type] || {};
    // 机动点优先取服务端 move_points（含缩地/精锐小队机动 +1、压制减半、
    // 剑阵冲锋 +2）；缺失时回退到兵种基础值 + 本地状态推算（旧快照兼容）。
    let mp = Number(unit.move_points);
    if (!Number.isFinite(mp) || mp <= 0) {
      mp = Number(troop.move || 3);
      // 落石/箭雨压制：下一回合起生效（与服务端 _status_active 同口径）
      const st = unit.statuses || {};
      if (st.suppress && Number(st.suppress_from ?? -1) < Number(bs.round ?? 0)) mp *= 0.5;
      // 剑阵冲锋：3 回合机动 +2（与服务端 move_points 同口径）
      if (Number(st.charge || 0) > 0) mp += 2;
    }
    const start = unit.pos;   // B6：pos 为 [q, r] 数组
    const sq = start[0], sr = start[1];
    const cells = new Map();
    cells.set(sq + "," + sr, { q: sq, r: sr, cost: 0, prev: null });
    const queue = [cells.get(sq + "," + sr)];
    while (queue.length) {
      queue.sort((a, b) => a.cost - b.cost);
      const cur = queue.shift();
      for (const [nq, nr] of neighbors(cur.q, cur.r)) {
        const key = nq + "," + nr;
        if (!passable(nq, nr, unit.troop_type)) continue;
        if (unitAt(nq, nr)) continue;  // 占位格不可通行
        // B17：与服务端 check_move_path 同口径的兵种地形消耗
        const t = terrainAt(nq, nr);
        let step = MOVE_COST[t] || 1;
        if (unit.troop_type === "beast_horde" && t === "forest") step = 1;
        if (unit.troop_type === "flying_sword" && t === "plain") {
          step = Math.max(0.5, step - 0.5);
        }
        const cost = cur.cost + step;
        if (cost > mp + 1e-6) continue;
        const old = cells.get(key);
        if (old && old.cost <= cost) continue;
        const cell = { q: nq, r: nr, cost, prev: cur };
        cells.set(key, cell);
        queue.push(cell);
      }
    }
    cells.delete(sq + "," + sr);
    return cells;
  }

  function pathTo(cells, q, r) {
    const path = [];
    let cur = cells.get(q + "," + r);
    // cells 的根节点是寻路起点（prev=null），不属于移动步骤。
    // 服务端 check_move_path 要求 path[0] 与单位当前站位相邻，
    // 带上起点会被判成「第1步不相邻」导致移动必然失败。
    while (cur) { path.unshift([cur.q, cur.r]); cur = cur.prev; }
    if (path.length) path.shift();
    return path;
  }

  // 当前操作/选中单位的阵营（用于区分敌我，不能只看 u.mine：
  // 同势力其他玩家的军团 mine=false 但并非敌军）
  function mySide() {
    // 优先取本方（mine）单位的阵营：对手回合 current_actor 是敌方，
    // 若直接取它会把本方 ZOC 当成红色敌方 ZOC 绘出。观战视图没有
    // mine 单位时仍回退到当前行动方，保持原有只读展示。
    const own = (bs.units || []).find((u) => u.mine && u.side);
    if (own) return own.side;
    const cur = (bs.units || []).find((u) => u.uid === bs.current_actor);
    if (cur) return cur.side;
    const sel = (bs.units || []).find((u) => u.uid === selectedUid);
    return sel ? sel.side : null;
  }

  // 敌方 ZOC 提示格（近似：敌方单位 6 邻格，去河流/友军占位）
  function enemyZocTiles() {
    const tiles = new Set();
    const side = mySide();
    for (const u of bs.units || []) {
      if (!u.pos || u.troops <= 0 || u.routed) continue;
      if (side ? u.side === side : u.mine) continue;
      if (terrainAt(u.pos[0], u.pos[1]) === "river") continue;
      for (const [q, r] of neighbors(u.pos[0], u.pos[1])) {
        if (!inBounds(q, r)) continue;
        if (terrainAt(q, r) === "river") continue;
        const occ = unitAt(q, r);
        if (occ && (side ? occ.side === side : occ.mine)) continue;  // 友军通道
        tiles.add(q + "," + r);
      }
    }
    return tiles;
  }

  function attackRangeOf(unit) {
    // B17：射程取自 meta.troop_types.range（法修营 3、高地 +1；近战为 1）。
    // 旧 TROOP_RANGE={bow,siege} 是永远匹配不上的死键。
    const meta = (typeof S !== "undefined" && S && S.meta) || {};
    const troop = (meta.troop_types || {})[unit.troop_type] || {};
    let r = Number(troop.range || 0);
    // 咒唱：3 回合全军射程 +1（与服务端 attack_range 的 range_buff 同口径）
    const st = unit.statuses || {};
    if (r > 0 && Number(st.range_buff || 0) > 0) r += 1;
    if (r > 0 && unit.pos) {
      const t = terrainAt(unit.pos[0], unit.pos[1]);
      if (t === "highland" || t === "wall" || t === "gate") r += 1;
    }
    return Math.max(1, r);
  }

  // -------------------------------------------------------------------------
  // 渲染
  // -------------------------------------------------------------------------
  function resize() {
    if (!bs) return;
    const dpr = window.devicePixelRatio || 1;
    const box = canvas.parentElement.getBoundingClientRect();
    view.vw = box.width;
    view.vh = box.height;
    canvas.width = box.width * dpr;
    canvas.height = box.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const [w, h] = bs.size;
    const needW = w * 1.5 + 0.5, needH = h * Math.sqrt(3) + Math.sqrt(3) / 2;
    view.baseSize = Math.max(14, Math.min(
      (box.width - 24) / needW, (box.height - 40) / needH));
    view.baseOx = (box.width - needW * view.baseSize) / 2 + view.baseSize;
    view.baseOy = (box.height - needH * view.baseSize) / 2 + view.baseSize * Math.sqrt(3) / 2;
    applyView();
    draw();
  }

  // 把基础布局 + 缩放/平移合成为实际绘制参数（以视口中心为缩放锚点）
  function applyView() {
    const cx = view.vw / 2, cy = view.vh / 2;
    view.size = view.baseSize * view.zoom;
    view.ox = cx + (view.baseOx - cx) * view.zoom + view.panX;
    view.oy = cy + (view.baseOy - cy) * view.zoom + view.panY;
  }

  function setZoom(z) {
    view.zoom = Math.max(0.5, Math.min(3, Number(z) || 1));
    applyView();
    draw();
  }

  function resetView() {
    view.zoom = 1; view.panX = 0; view.panY = 0;
    applyView();
    draw();
  }

  function drawHex(cx, cy, size, fill, stroke, lw) {
    ctx.beginPath();
    for (let i = 0; i < 6; i++) {
      const ang = Math.PI / 180 * (60 * i);
      const px = cx + size * Math.cos(ang), py = cy + size * Math.sin(ang);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.closePath();
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = lw || 1; ctx.stroke(); }
  }

  function draw() {
    if (!bs) return;
    const box = canvas.parentElement.getBoundingClientRect();
    ctx.clearRect(0, 0, box.width, box.height);
    const [w, h] = bs.size;
    const zoc = bs.phase === "battle" ? enemyZocTiles() : new Set();
    const reach = mode === "move" && movePlan ? movePlan.cells : null;
    const dzA = ((bs.deploy_zones || {}).a || []);
    const dzB = ((bs.deploy_zones || {}).b || []);
    const dzSet = new Set((bs.phase === "deploy" ? dzA.concat(dzB) : [])
      .map((p) => p[0] + "," + p[1]));

    for (let q = 0; q < w; q++) {
      for (let r = 0; r < h; r++) {
        const [cx, cy] = hexToPixel(q, r);
        const t = bs.terrain ? terrainAt(q, r) : "plain";
        drawHex(cx, cy, view.size - 1, TERRAIN_COLOR[t] || "#1c2733",
                "#0a0f16", 1);
        if (!bs.terrain) continue;  // 战争迷雾：只画网格
        if (dzSet.has(q + "," + r)) {
          drawHex(cx, cy, view.size - 3, "rgba(88,166,255,0.10)",
                  "rgba(88,166,255,0.4)", 1);
        }
        if (zoc.has(q + "," + r)) {
          drawHex(cx, cy, view.size - 4, null, "rgba(248,81,73,0.35)", 1.2);
        }
        if (reach && reach.has(q + "," + r)) {
          drawHex(cx, cy, view.size - 4, "rgba(63,185,80,0.18)",
                  "rgba(63,185,80,0.6)", 1.4);
        }
        if (hoverHex && hoverHex[0] === q && hoverHex[1] === r) {
          drawHex(cx, cy, view.size - 2, null, "#e3b341", 2);
        }
      }
    }
    // 城墙 HP（攻城战）
    if (bs.mode === "siege" && bs.wall_hp > 0) {
      ctx.fillStyle = "#c6d0dc"; ctx.font = "12px sans-serif";
      ctx.textAlign = "left";
      ctx.fillText("🧱 城墙 " + bs.wall_hp, 8, 16);
    }
    // 单位
    for (const u of bs.units || []) {
      if (!u.pos || u.troops <= 0) continue;
      const [cx, cy] = hexToPixel(u.pos[0], u.pos[1]);
      const color = u.side === "a" ? "#58a6ff" : "#f85149";
      const radius = view.size * 0.55;
      ctx.beginPath(); ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.fillStyle = u.routed ? "#3a3a3a" : (u.mine ? "#132a44" : "#3a1520");
      ctx.fill();
      ctx.strokeStyle = color; ctx.lineWidth = u.uid === selectedUid ? 3 : 2;
      ctx.stroke();
      if (u.uid === bs.current_actor) {
        ctx.beginPath();
        ctx.arc(cx, cy, radius + 4, 0, Math.PI * 2);
        ctx.strokeStyle = "#e3b341"; ctx.lineWidth = 2;
        ctx.setLineDash([4, 3]); ctx.stroke(); ctx.setLineDash([]);
      }
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.font = (radius * 0.8) + "px serif";
      ctx.fillStyle = "#fff";
      const icon = { sword_shield: "🗡️", heavy_armor: "🛡️", flying_sword: "⚔️",
                     spell_corps: "🔮", beast_horde: "🐺" }[u.troop_type] || "⚔️";
      ctx.fillText(u.routed ? "💨" : icon, cx, cy - 1);
      ctx.font = "9px sans-serif";
      ctx.fillStyle = color;
      ctx.fillText(String(u.troops), cx, cy + radius * 0.62);
      // 状态角标
      let badges = "";
      if (u.defending) badges += "🛡";
      if (u.delegate) badges += "🤖";
      const st = u.statuses || {};
      if (st.burn) badges += "🔥";
      if (st.confuse) badges += "😵";
      if (st.suppress) badges += "🪨";
      if (st.bulwark) badges += "⛰️";
      if (badges) {
        ctx.font = "9px sans-serif"; ctx.textAlign = "left";
        ctx.fillText(badges, cx + radius * 0.5, cy - radius * 0.7);
      }
      // 血条（士气）
      const bw = radius * 1.6;
      ctx.fillStyle = "#21262d";
      ctx.fillRect(cx - bw / 2, cy + radius + 2, bw, 3);
      ctx.fillStyle = u.morale > 60 ? "#3fb950" : u.morale > 30 ? "#d29922" : "#f85149";
      ctx.fillRect(cx - bw / 2, cy + radius + 2, bw * Math.max(0, u.morale) / 100, 3);
    }
    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
  }

  // -------------------------------------------------------------------------
  // 侧栏 / 顶栏
  // -------------------------------------------------------------------------
  function renderTop() {
    document.getElementById("battleTitle").textContent =
      (bs.mode === "siege" ? "🔥 攻城战" : bs.mode === "pass" ? "⛰️ 关隘战" : "⚔️ 会战") +
      (bs.night ? " · 🌙 夜战" : "");
    document.getElementById("battleRound").textContent =
      bs.phase === "battle" ? "第 " + bs.round + " 回合" :
      bs.phase === "deploy" ? "部署阶段" : "已结束";
    const ph = document.getElementById("battlePhase");
    ph.textContent = { deploy: "部署", battle: "战斗", done: "结束" }[bs.phase] || bs.phase;
    const actorEl = document.getElementById("battleActor");
    const delegateBtn = document.getElementById("btnDelegate");
    if (isSpectator()) {
      // §12.6 观战：顶部明确只读，隐藏托管
      actorEl.textContent = "👁 观战中（只读）";
      actorEl.classList.add("spectating");
      if (delegateBtn) delegateBtn.style.display = "none";
    } else {
      const actor = (bs.units || []).find((u) => u.uid === bs.current_actor);
      actorEl.textContent = actor
        ? (actor.mine ? "▶ 轮到你的「" + actor.leader + "」行动"
                      : "⏳ 等待「" + actor.leader + "」行动…")
        : "";
      actorEl.classList.remove("spectating");
      if (delegateBtn) delegateBtn.style.display = "";
      delegateBtn.textContent =
        delegated ? "🤖 已托管（点击取消）" : "🤖 托管";
    }
  }

  function isSpectator() {
    return spectator || !!(bs && bs.spectator);
  }

  function renderUnits() {
    const el = document.getElementById("battleUnits");
    const rows = (bs.units || []).filter((u) => u.troops > 0)
      .sort((a, b) => (b.mine - a.mine) || (a.side < b.side ? -1 : 1));
    el.innerHTML = rows.map((u) => {
      const st = u.statuses || {};
      const marks = [u.defending ? "🛡" : "", u.delegate ? "🤖" : "",
                     st.burn ? "🔥" : "", st.confuse ? "😵" : "",
                     st.suppress ? "🪨" : "", u.routed ? "💨溃逃" : ""].join("");
      return '<div class="bu-row' + (u.mine ? " me" : "") +
        (u.uid === selectedUid ? " sel" : "") + '" data-uid="' + u.uid + '">' +
        '<span class="bu-dot" style="background:' + (u.side === "a" ? "#58a6ff" : "#f85149") + '"></span>' +
        '<span class="bu-name">' + esc(u.leader) + (u.mine ? "（我）" : "") + "</span>" +
        '<span class="bu-troops">' + u.troops + " 兵</span>" +
        '<span class="bu-marks">' + marks + "</span></div>";
    }).join("") || '<div class="sub">战场空空如也</div>';
    el.querySelectorAll(".bu-row").forEach((row) => {
      row.addEventListener("click", () => {
        selectedUid = Number(row.dataset.uid);
        mode = "select"; movePlan = null;
        renderUnits(); renderActions(); draw();
      });
    });
  }

  function renderActions() {
    const el = document.getElementById("battleActions");
    if (isSpectator()) {
      // 观战/只读：隐藏部署与全部操作，只保留战报与回放
      let h = '<div class="sub spectate-note">👁 观战中（只读）：部署与操作已隐藏，战况自动推进' +
        (bs.round ? "（延迟 1 回合）" : "") + "</div>";
      if (bs.state === "done" || bs.phase === "done") {
        h += '<div class="ba-btns"><button class="btn small primary" id="btnViewReport">📜 查看战报</button>' +
          '<button class="btn small" id="btnViewReplay">🎞️ 回放</button></div>';
      }
      el.innerHTML = h;
      bindDoneButtons();
      return;
    }
    if (bs.state === "done" || bs.phase === "done") {
      el.innerHTML = '<button class="btn small primary" id="btnViewReport">📜 查看战报</button>' +
        '<button class="btn small" id="btnViewReplay">🎞️ 回放</button>';
      bindDoneButtons();
      return;
    }
    if (bs.phase === "deploy") {
      // 只按「自己的军团」判断是否可开战：服务端 battle_start 会把所有
      // 未落位单位（含未到场的盟友/敌方/守军）自动部署并托管，避免某位
      // 参战者挂机导致所有人都点不动开战按钮、最后被部署超时 AI 打完。
      const left = (bs.units || []).filter(
        (u) => u.mine && !u.pos && u.troops > 0);
      let h = '<div class="sub">点击左侧未部署军团 → 点击蓝色部署区落位；未部署的盟友/守军开战时自动布置</div>';
      h += '<div class="ba-btns">';
      h += '<button class="btn small" id="btnAutoDeploy">⚡ 自动部署</button>';
      h += '<button class="btn small primary" id="btnStartBattle"' +
        (left.length ? " disabled" : "") + ">🥁 全军开战</button>";
      h += "</div>";
      if (selectedUid) {
        h += '<div class="ba-btns"><button class="btn small" id="btnAmbush">🌲 设伏（森林格）</button></div>';
      }
      const picked = (bs.units || []).find((u) => u.uid === selectedUid && u.mine);
      if (picked) {
        h += '<div class="ba-btns"><label class="sub">阵型：' +
          '<select id="selFormation">' + FORMATIONS.map(([v, n]) =>
            '<option value="' + v + '"' +
            ((picked.formation || "") === v ? " selected" : "") + ">" + n +
            "</option>").join("") + "</select></label></div>";
      }
      el.innerHTML = h;
      const ad = document.getElementById("btnAutoDeploy");
      if (ad) ad.addEventListener("click", () => act("battle_auto_deploy", {}));
      const sb = document.getElementById("btnStartBattle");
      if (sb) sb.addEventListener("click", () => act("battle_start", {}));
      const ab = document.getElementById("btnAmbush");
      if (ab) ab.addEventListener("click", () => {
        mode = "ambush";
        setHint("点击一个森林格埋设伏兵（敌军接近时触发先手）");
      });
      const fs = document.getElementById("selFormation");
      if (fs) fs.addEventListener("change", () => {
        act("battle_set_formation", { uid: selectedUid, formation: fs.value });
      });
      return;
    }
    // 战斗阶段
    const me = (bs.units || []).find((u) => u.uid === selectedUid);
    const isMyTurn = bs.current_actor &&
      ((bs.units || []).find((u) => u.uid === bs.current_actor) || {}).mine;
    let h = "";
    if (isMyTurn) {
      const cur = (bs.units || []).find((u) => u.uid === bs.current_actor);
      h += '<div class="sub">行动中：' + esc(cur.leader) +
        (cur.formation ? " · " + (FORMATION_NAMES[cur.formation] || cur.formation) : "") +
        (cur.moved ? "（已移动）" : "") + (cur.acted ? "（已行动）" : "") + "</div>";
      h += '<div class="ba-btns">';
      if (!cur.moved && !cur.acted) {
        h += '<button class="btn small primary" id="btnMove">👣 移动</button>';
        if (cur.can_zoc_break) {
          h += '<button class="btn small" id="btnMoveBreak">🗡 突破 ZOC（每场 1 次）</button>';
        }
      }
      if (!cur.acted) {
        h += '<button class="btn small danger" id="btnAttack">⚔️ 攻击</button>';
        h += '<button class="btn small" id="btnDefend">🛡️ 坚守</button>';
        h += '<button class="btn small" id="btnRetreat">🏳️ 撤退</button>';
        h += '<button class="btn small" id="btnDuel">🎯 单挑</button>';
        for (const sid of cur.skills || []) {
          const info = SKILL_INFO[sid] || { name: sid, mana: 0 };
          const dis = (cur.mana || 0) < info.mana ? " disabled" : "";
          h += '<button class="btn small magic" data-skill="' + sid + '"' + dis + ">" +
            info.name + (info.mana ? "(" + info.mana + "气)" : "") + "</button>";
        }
      }
      h += '<button class="btn small" id="btnEndTurn">⏭️ 结束行动</button>';
      h += "</div>";
    } else {
      h += '<div class="sub">⏳ 等待对方行动…</div>';
    }
    if (me && me.uid !== bs.current_actor) {
      h += '<div class="sub">已选：' + esc(me.leader) + " · 士气 " + me.morale +
        " · 法力 " + (me.mana || 0) + "</div>";
    }
    el.innerHTML = h;
    const bm = document.getElementById("btnMove");
    if (bm) bm.addEventListener("click", () => {
      const cur = (bs.units || []).find((u) => u.uid === bs.current_actor);
      movePlan = { uid: cur.uid, cells: computeReachable(cur) };
      mode = "move";
      setHint("绿色为可达格；点击落点移动（进入敌 ZOC 会被截停）");
      draw();
    });
    const bmb = document.getElementById("btnMoveBreak");
    if (bmb) bmb.addEventListener("click", () => {
      const cur = (bs.units || []).find((u) => u.uid === bs.current_actor);
      movePlan = { uid: cur.uid, cells: computeReachable(cur), zocBreak: true };
      mode = "move";
      setHint("突破移动：首次进入敌 ZOC 不再截停（每场 1 次）；点击落点");
      draw();
    });
    const ba = document.getElementById("btnAttack");
    if (ba) ba.addEventListener("click", () => {
      mode = "attack";
      setHint("点击射程内的敌军发起攻击");
      draw();
    });
    const bd = document.getElementById("btnDefend");
    if (bd) bd.addEventListener("click", () => act2("defend", {}));
    const br = document.getElementById("btnRetreat");
    if (br) br.addEventListener("click", () => act2("retreat", {}));
    const bdu = document.getElementById("btnDuel");
    if (bdu) bdu.addEventListener("click", () => {
      mode = "duel";
      setHint("点击相邻敌将发起单挑（胜者 60% 擒斩败者）");
      draw();
    });
    const bet = document.getElementById("btnEndTurn");
    if (bet) bet.addEventListener("click", () => act2("end_turn", {}));
    el.querySelectorAll("[data-skill]").forEach((btn) => {
      btn.addEventListener("click", () => {
        skillId = btn.dataset.skill;
        const info = SKILL_INFO[skillId] || {};
        if (info.target === "self" || info.target === "auto") {
          act2("skill", { skill: skillId });
          return;
        }
        mode = "skill";
        setHint("「" + info.name + "」：点击" +
          ({ hex: "目标格（射程 " + info.range + "）", unit: "敌方单位",
             friendly: "友方单位", empty: "空地点", wall: "城墙格" }[info.target] || "目标"));
        draw();
      });
    });
  }

  function bindDoneButtons() {
    const vr = document.getElementById("btnViewReport");
    if (vr) vr.addEventListener("click", showReport);
    const rp = document.getElementById("btnViewReplay");
    if (rp) rp.addEventListener("click", openReplay);
  }

  function renderLog() {
    const el = document.getElementById("battleLog");
    const evs = (bs.events || []).slice(-30);
    el.innerHTML = evs.map((e) =>
      '<div class="bl-row' + (e.kind === "duel" ? " duel" : "") + '">' +
      (e.round ? '<span class="bl-r">R' + e.round + "</span>" : "") +
      esc(e.text || "") + "</div>").join("") || '<div class="sub">尚无战况</div>';
    el.scrollTop = el.scrollHeight;
  }

  function setHint(text) {
    document.getElementById("battleHint").textContent = text || "";
  }

  // -------------------------------------------------------------------------
  // 动作
  // -------------------------------------------------------------------------
  async function act(action, params) {
    if (isSpectator()) {
      toast("👁 观战模式为只读，无法提交操作", "err");
      return null;
    }
    try {
      const r = await api("/api/strategy/action", {
        method: "POST",
        body: JSON.stringify({
          action, params: Object.assign({ battle_id: battleId }, params || {}) }),
      });
      if (r && r.msg) toast(r.msg, r.ok === false ? "err" : "ok");
      await refresh();
      return r;
    } catch (e) { return null; }
  }

  // battle_action 族：action 名放 params.act（后端 params["act"] 分发）
  async function act2(battleAct, params) {
    mode = "select"; movePlan = null; setHint("");
    params = params || {};
    // v4.3.0：回合类指令附带渲染时的 current_actor，服务端校验回合未切换；
    // 防止网络延迟下旧指令误吞己方下一单位的行动权（超时托管后尤其危险）
    const turnActs = ["move", "attack", "skill", "defend", "retreat",
                      "end_turn", "duel_challenge"];
    if (turnActs.indexOf(battleAct) >= 0 && bs && bs.current_actor) {
      params.expected_uid = bs.current_actor;
    }
    return act("battle_action", { act: battleAct, params: params });
  }

  async function refresh() {
    const seq = ++refreshSeq;
    try {
      const r = await api("/api/strategy/action", {
        method: "POST",
        body: JSON.stringify({ action: "battle_state",
                               params: { battle_id: battleId } }),
      });
      // 轮询/操作触发的多次刷新可能乱序返回，旧快照会覆盖新状态
      if (seq !== refreshSeq) return true;
      bs = r;
      // 观战：URL/入口指定，或后端判定为同势力旁观（my_uids 为空）
      if (r && r.spectator) spectator = true;
      const actorNow = bs.current_actor || null;
      if (actorNow !== lastActor) {
        // 回合超时/换人：清空上一单位的技能与可达路径，
        // 否则旧路径会以"当前行动单位"名义发出 move，误移动其他军团
        mode = "select"; movePlan = null; skillId = "";
        lastActor = actorNow; setHint("");
        // 战场页在后台标签时仍会 3s 轮询：轮到己方单位就发浏览器通知，
        // 避免玩家切去聊天/地图后每回合 30 秒超时托管。
        if (actorNow && (bs.my_uids || []).includes(actorNow) && document.hidden) {
          if (typeof notifyLocal === "function") {
            notifyLocal("⚔️ 轮到你行动",
              "手操战役第 " + (bs.round || 0) + " 回合，30 秒内未操作将自动托管",
              null, "battle-turn-" + battleId + "-" + (bs.round || 0));
          }
        }
      }
      delegated = (bs.units || []).some((u) => u.mine && u.delegate);
      renderTop(); renderUnits(); renderActions(); renderLog(); draw();
      checkDuel();
      if (bs.state === "done") { showReport(); stopPoll(false); }
      return true;
    } catch (e) { /* api 已弹 toast */ return false; }
  }

  function checkDuel() {
    const d = bs.duel;
    if (!d || d.state !== "pending") {
      document.getElementById("duelOverlay").hidden = true;
      return;
    }
    const isTarget = (bs.my_uids || []).includes(d.target_uid);
    document.getElementById("duelOverlay").hidden = !isTarget;
    if (isTarget) {
      const from = (bs.units || []).find((u) => u.uid === d.challenger_uid);
      document.getElementById("duelPrompt").textContent =
        "敌将「" + ((from || {}).leader || "?") + "」向你发起阵前单挑！应战获胜可 60% 擒斩敌将。";
    }
  }

  function stopPoll(keepTick) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (!keepTick && tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  }

  function updateTimer() {
    const el = document.getElementById("battleTimer");
    if (!bs) return;
    if (isSpectator()) {
      el.textContent = "👁 观战";
      el.classList.remove("urgent");
      return;
    }
    const dl = bs.phase === "deploy" ? bs.deadline_at : bs.turn_deadline;
    const left = Math.max(0, Number(dl || 0) - srvNowSec());
    el.textContent = (bs.phase === "deploy" ? "部署剩余 " : "回合剩余 ") + left + "s";
    el.classList.toggle("urgent", left <= 10);
  }

  // -------------------------------------------------------------------------
  // Canvas 交互
  // -------------------------------------------------------------------------
  // 移动端手势：单指拖拽平移 / 双指 pinch 缩放（0.5x~3x）/ 双击复位；
  // 桌面端滚轮缩放，普通点击选中逻辑保持不变。
  const gesture = {
    pointers: new Map(), dragging: false, moved: 0,
    lastX: 0, lastY: 0, pinch: null,
    suppressClick: false, lastTapAt: 0, lastTapX: 0, lastTapY: 0,
  };

  canvas.addEventListener("pointerdown", (ev) => {
    if (!bs) return;
    gesture.pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    gesture.suppressClick = false;
    if (gesture.pointers.size === 1) {
      gesture.dragging = true;
      gesture.moved = 0;
      gesture.lastX = ev.clientX;
      gesture.lastY = ev.clientY;
      try { canvas.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
      if (ev.pointerType !== "mouse") {
        const now = Date.now();
        if (now - gesture.lastTapAt < 320 &&
            bs.phase === "battle" && mode === "select" &&
            Math.hypot(ev.clientX - gesture.lastTapX, ev.clientY - gesture.lastTapY) < 30) {
          resetView();
          gesture.lastTapAt = 0;
          gesture.suppressClick = true;
        } else {
          gesture.lastTapAt = now;
          gesture.lastTapX = ev.clientX;
          gesture.lastTapY = ev.clientY;
        }
      }
    } else if (gesture.pointers.size === 2) {
      const ps = Array.from(gesture.pointers.values());
      gesture.pinch = {
        dist: Math.max(1, Math.hypot(ps[0].x - ps[1].x, ps[0].y - ps[1].y)),
        zoom: view.zoom,
      };
      gesture.dragging = false;
      gesture.suppressClick = true;
    }
  });

  canvas.addEventListener("pointermove", (ev) => {
    if (!bs) return;
    if (gesture.pointers.has(ev.pointerId)) {
      gesture.pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    }
    if (gesture.pointers.size >= 2 && gesture.pinch) {
      const ps = Array.from(gesture.pointers.values()).slice(0, 2);
      const dist = Math.max(1, Math.hypot(ps[0].x - ps[1].x, ps[0].y - ps[1].y));
      setZoom(gesture.pinch.zoom * dist / gesture.pinch.dist);
      return;
    }
    if (!gesture.dragging || gesture.pointers.size !== 1) return;
    const dx = ev.clientX - gesture.lastX;
    const dy = ev.clientY - gesture.lastY;
    if (!dx && !dy) return;
    gesture.lastX = ev.clientX;
    gesture.lastY = ev.clientY;
    gesture.moved += Math.abs(dx) + Math.abs(dy);
    if (gesture.moved > 6) gesture.suppressClick = true;
    view.panX += dx;
    view.panY += dy;
    applyView();
    draw();
  });

  const endPointer = (ev) => {
    gesture.pointers.delete(ev.pointerId);
    if (gesture.pointers.size < 2) gesture.pinch = null;
    if (gesture.pointers.size === 0) gesture.dragging = false;
  };
  canvas.addEventListener("pointerup", endPointer);
  canvas.addEventListener("pointercancel", endPointer);

  canvas.addEventListener("wheel", (ev) => {
    if (!bs) return;
    ev.preventDefault();
    setZoom(view.zoom * (ev.deltaY < 0 ? 1.12 : 1 / 1.12));
  }, { passive: false });

  canvas.addEventListener("mousemove", (ev) => {
    if (!bs || !bs.terrain || gesture.dragging) return;
    const rect = canvas.getBoundingClientRect();
    const [q, r] = pixelToHex(ev.clientX - rect.left, ev.clientY - rect.top);
    const changed = !hoverHex || hoverHex[0] !== q || hoverHex[1] !== r;
    hoverHex = inBounds(q, r) ? [q, r] : null;
    if (changed) draw();
  });

  canvas.addEventListener("click", (ev) => {
    if (gesture.suppressClick) { gesture.suppressClick = false; return; }
    if (!bs || bs.state === "done") return;
    const rect = canvas.getBoundingClientRect();
    const [q, r] = pixelToHex(ev.clientX - rect.left, ev.clientY - rect.top);
    if (!inBounds(q, r)) return;
    const clicked = unitAt(q, r);

    if (isSpectator()) {
      // 观战只允许选中查看，不触发任何操作
      if (clicked) { selectedUid = clicked.uid; renderUnits(); draw(); }
      return;
    }

    if (bs.phase === "deploy") {
      if (mode === "ambush" && selectedUid) {
        act("battle_set_ambush", { uid: selectedUid, q, r });
        mode = "select"; setHint("");
        return;
      }
      // 选中未部署单位
      const unplaced = myUnits().filter((u) => !u.pos);
      if (clicked && clicked.mine) { selectedUid = clicked.uid; renderUnits(); renderActions(); draw(); return; }
      if (unplaced.length) {
        const uid = selectedUid && unplaced.some((u) => u.uid === selectedUid)
          ? selectedUid : unplaced[0].uid;
        // 自动挑中下一支未部署军团时同步高亮，避免左侧仍选中已部署单位、
        // 实际落位的却是另一支军团（静默改选）。
        selectedUid = uid;
        act("battle_deploy", { placements: [{ uid, q, r }] });
      }
      return;
    }
    if (bs.phase !== "battle") return;

    if (mode === "move" && movePlan && movePlan.uid === bs.current_actor && !clicked) {
      const path = pathTo(movePlan.cells, q, r);
      if (!path.length) { toast("该格不可达", "err"); return; }
      const uid = movePlan.uid;
      const zocBreak = !!movePlan.zocBreak;
      movePlan = null; mode = "select"; setHint("");
      act2("move", zocBreak ? { path, break: true } : { path });
      return;
    }
    const my = mySide();
    const isFoe = (u) => !!u && !!my && u.side !== my;
    if (mode === "attack" && isFoe(clicked)) {
      act2("attack", { target: clicked.uid });
      return;
    }
    if (mode === "duel" && isFoe(clicked)) {
      act2("duel_challenge", { target: clicked.uid });
      return;
    }
    if (mode === "skill" && skillId) {
      const info = SKILL_INFO[skillId] || {};
      if (info.target === "unit" && !isFoe(clicked)) {
        toast("请选择敌方单位", "err"); return;
      }
      if (info.target === "friendly" && (!clicked || !my || clicked.side !== my)) {
        toast("请选择友方单位", "err"); return;
      }
      if (info.target === "empty" && clicked) {
        toast("请选择空地点", "err"); return;
      }
      const params = { skill: skillId };
      if (info.target === "unit" || info.target === "friendly") params.target_uid = clicked.uid;
      else params.target_hex = [q, r];
      skillId = "";
      act2("skill", params);
      return;
    }
    // 默认：选中单位
    if (clicked) {
      selectedUid = clicked.uid;
      mode = "select"; movePlan = null;
      renderUnits(); renderActions(); draw();
    }
  });

  // -------------------------------------------------------------------------
  // 战报 / 回放
  // -------------------------------------------------------------------------
  function hlText(h) {
    const s = String(h === null || h === undefined ? "" : h).trim().replace(/^⭐\s*/, "");
    if (!s) return "";
    const names = {
      duel_win: "单挑获胜", duel: "单挑决胜", encircle: "完成合围",
      night_raid: "夜袭得手", night: "夜袭作战", underdog: "以少胜多", ambush: "伏兵突袭",
    };
    return names[s] || s;
  }

  function highlightsOf(res) {
    const raw = (res && res.highlights) || [];
    return (Array.isArray(raw) ? raw : []).map(hlText).filter(Boolean);
  }

  function showReport() {
    if (!bs || bs.state !== "done" || !bs.result) return;
    const res = bs.result;
    const winText = res.winner === "attacker" ? "攻方胜" :
      res.winner === "defender" ? "守方胜" : "平局收场";
    const hl = highlightsOf(res);
    const lines = [
      '<div class="report-head">🏁 ' + winText + " · " + (bs.round || 0) + " 回合</div>",
      hl.length ? '<div class="hl-report">' + hl.map((t) =>
        '<div class="hl-item">⭐ ' + esc(t) + "</div>").join("") + "</div>" : "",
      '<div class="report-text">' + esc(bs.report_text || "").replace(/\n/g, "<br>") + "</div>",
      '<div class="form-actions"><button class="btn small" id="btnCopyReport">📋 复制战报</button>' +
        '<button class="btn small primary" id="btnShareCard">📤 分享</button></div>',
    ];
    document.getElementById("battleLog").innerHTML = lines.join("");
    const cp = document.getElementById("btnCopyReport");
    if (cp) cp.addEventListener("click", () => copyReport());
    const sc = document.getElementById("btnShareCard");
    if (sc) sc.addEventListener("click", openShareCard);
  }

  function copyPlain(text, okMsg) {
    const done = () => toast(okMsg || "已复制");
    const fail = () => toast("复制失败", "err");
    const legacy = () => {
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand("copy");
        ta.remove();
        ok ? done() : fail();
      } catch (e) { fail(); }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, legacy);
    } else legacy();
  }

  function copyReport() {
    const text = "【仙域争霸战报】" + (bs.report_text || "");
    copyPlain(text, "战报已复制，去聊天室分享吧");
  }

  // -------------------------------------------------------------------------
  // 分享卡片（Canvas 720×960，无外部依赖）
  // -------------------------------------------------------------------------
  function shareModel() {
    const res = bs.result || {};
    const S0 = (typeof S !== "undefined" && S) || {};
    const node = ((S0.nodes || []).find((n) => n.id === bs.node_id)) || {};
    const facName = (fid) => {
      const f = ((S0.factions || []).find((x) => x.id === fid)) || {};
      return f.name || (fid ? "势力#" + fid : "无主");
    };
    const atk = res.attackers || [];
    const dfn = res.defenders || [];
    const uniq = (arr) => Array.from(new Set(arr.map((x) => x.faction_id).filter(Boolean)));
    const atkName = uniq(atk).map(facName).join("、") || "攻方";
    const dfnName = uniq(dfn).map(facName).join("、") || "守方";
    const season = (S0.season || {}).season_no || 1;
    const tick = (S0.world || {}).tick_no || 0;
    return {
      node: node.name || ("据点#" + bs.node_id),
      winText: res.winner === "attacker" ? "攻方胜" :
        res.winner === "defender" ? "守方胜" : "平局收场",
      winner: res.winner || "",
      rounds: bs.round || res.rounds || 0,
      atkName: atkName, dfnName: dfnName,
      atkLoss: Number(res.atk_losses || 0), dfnLoss: Number(res.dfn_losses || 0),
      atk: atk, dfn: dfn,
      highlights: highlightsOf(res),
      season: season, tick: tick,
      when: new Date().toLocaleString("zh-CN", { hour12: false }),
    };
  }

  function shareText(m) {
    const lines = [
      "⚔️【仙域争霸·战报】" + m.node,
      "🏁 " + m.winText + " · " + m.rounds + " 回合",
      "🔵 攻方：" + m.atkName + "（损 " + m.atkLoss + "）",
      "🔴 守方：" + m.dfnName + "（损 " + m.dfnLoss + "）",
    ];
    if (m.highlights.length) lines.push("⭐ 高光：" + m.highlights.join(" / "));
    lines.push("📅 第 " + m.season + " 赛季 · 第 " + m.tick + " tick");
    lines.push("🕰 " + m.when);
    return lines.join("\n");
  }

  function drawShareCard(cv, m) {
    const g = cv.getContext("2d");
    const W = cv.width, H = cv.height;
    const FONT = '"PingFang SC","Microsoft YaHei",sans-serif';
    const bg = g.createLinearGradient(0, 0, 0, H);
    bg.addColorStop(0, "#0d1117");
    bg.addColorStop(1, "#1b2330");
    g.fillStyle = bg;
    g.fillRect(0, 0, W, H);
    g.strokeStyle = "#e3b341";
    g.lineWidth = 4;
    g.strokeRect(18, 18, W - 36, H - 36);

    g.textAlign = "center";
    g.textBaseline = "top";
    g.fillStyle = "#e3b341";
    g.font = "bold 26px " + FONT;
    g.fillText("⚔️ 仙域争霸 · 战报", W / 2, 54);
    g.fillStyle = "#d7dee8";
    g.font = "bold 34px " + FONT;
    g.fillText(m.node, W / 2, 100);
    g.strokeStyle = "#2d3646";
    g.lineWidth = 1.5;
    g.beginPath(); g.moveTo(64, 156); g.lineTo(W - 64, 156); g.stroke();

    g.fillStyle = m.winner === "attacker" ? "#58a6ff" :
      m.winner === "defender" ? "#f85149" : "#e3b341";
    g.font = "bold 50px " + FONT;
    g.fillText(m.winText, W / 2, 184);
    g.fillStyle = "#8b98a9";
    g.font = "20px " + FONT;
    g.fillText(m.rounds + " 回合 · 第 " + m.season + " 赛季", W / 2, 246);

    // 双方势力 + 兵力损失
    function side(y, label, name, units, loss, color) {
      g.textAlign = "left";
      g.fillStyle = color;
      g.font = "bold 22px " + FONT;
      g.fillText(label, 64, y);
      g.fillStyle = "#d7dee8";
      g.font = "bold 24px " + FONT;
      g.fillText(name.slice(0, 14), 150, y - 2);
      let yy = y + 44;
      g.font = "18px " + FONT;
      units.slice(0, 3).forEach((u) => {
        const troop = ((S && S.meta && S.meta.troop_types[u.troop_type]) || {}).name || "部队";
        g.fillStyle = "#8b98a9";
        g.fillText((u.leader_name || "无主军团") + " · " + troop + "  " +
          (u.troops_before || 0) + " → " + (u.troops_after || 0), 82, yy, W - 164);
        yy += 26;
      });
      g.fillStyle = "#f85149";
      g.font = "bold 20px " + FONT;
      g.fillText("兵力损失 " + loss, 82, yy + 6);
      return yy + 48;
    }

    let y = side(286, "🔵 攻方", m.atkName, m.atk, m.atkLoss, "#58a6ff");
    y = side(y + 18, "🔴 守方", m.dfnName, m.dfn, m.dfnLoss, "#f85149");

    // 高光
    g.textAlign = "left";
    g.fillStyle = "#e3b341";
    g.font = "bold 22px " + FONT;
    g.fillText("⭐ 本场高光", 64, y + 10);
    let hy = y + 48;
    g.font = "20px " + FONT;
    if (m.highlights.length) {
      m.highlights.slice(0, 4).forEach((t) => {
        g.fillStyle = "#ffe9b0";
        g.fillText("· " + t, 82, hy, W - 164);
        hy += 28;
      });
    } else {
      g.fillStyle = "#8b98a9";
      g.fillText("· 本场无特殊高光", 82, hy);
      hy += 28;
    }

    g.textAlign = "center";
    g.fillStyle = "#8b98a9";
    g.font = "16px " + FONT;
    g.fillText("第 " + m.season + " 赛季 · 第 " + m.tick + " tick", W / 2, H - 104);
    g.fillText("生成于 " + m.when, W / 2, H - 76);
    g.fillStyle = "#2d3646";
    g.fillText("仙域争霸 · 战略大地图", W / 2, H - 48);
  }

  function ensureShareOverlay() {
    let ov = document.getElementById("shareOverlay");
    if (ov) return ov;
    ov = document.createElement("div");
    ov.id = "shareOverlay";
    ov.className = "overlay";
    ov.hidden = true;
    ov.innerHTML = '<div class="overlay-card share-card">' +
      "<h2>📤 分享战报</h2>" +
      '<div class="share-canvas-wrap"><canvas id="shareCanvas" width="720" height="960"></canvas></div>' +
      '<div class="form-actions">' +
      '<button class="btn small" id="shareDownload" type="button">⬇️ 下载图片</button>' +
      '<button class="btn small primary" id="shareCopy" type="button">📋 复制文本</button>' +
      '<button class="btn small" id="shareClose" type="button">关闭</button>' +
      "</div></div>";
    (document.getElementById("battleOverlay") || document.body).appendChild(ov);
    ov.querySelector("#shareClose").addEventListener("click", () => { ov.hidden = true; });
    ov.querySelector("#shareDownload").addEventListener("click", () => {
      try {
        const a = document.createElement("a");
        a.download = "仙域争霸战报_" + battleId + ".png";
        a.href = document.getElementById("shareCanvas").toDataURL("image/png");
        a.click();
        toast("⬇️ 分享卡片已下载");
      } catch (e) { toast("下载失败，请尝试截图", "err"); }
    });
    ov.querySelector("#shareCopy").addEventListener("click", () => {
      copyPlain(shareText(shareModel()), "📋 战报文本已复制");
    });
    ov.addEventListener("click", (ev) => { if (ev.target === ov) ov.hidden = true; });
    return ov;
  }

  function openShareCard() {
    if (!bs || !bs.result) return;
    const ov = ensureShareOverlay();
    const cv = document.getElementById("shareCanvas");
    drawShareCard(cv, shareModel());
    ov.hidden = false;
  }

  let replay = null, replayIdx = 0;

  async function openReplay() {
    try {
      const r = await api("/api/strategy/action", {
        method: "POST",
        body: JSON.stringify({ action: "battle_replay",
                               params: { battle_id: battleId } }),
      });
      replay = r; replayIdx = 0;
      document.getElementById("replayOverlay").hidden = false;
      renderReplay();
    } catch (e) { /* toast 已弹 */ }
  }

  function renderReplay() {
    if (!replay) return;
    // 回放步进 = 终局战况事件流（battle_state.events 仅尾部 80 条）。
    // 服务端 events 只保留最近 400 条；更早的关键事件（开局/单挑/伏兵/
    // 合围）由 key_events 补回，避免回放从战役中段才开始。
    let evs = replay.events || [];
    const firstRound = evs.length ? Number(evs[0].round || 0) : 0;
    const early = (replay.key_events || []).filter(
      (e) => Number(e.round || 0) < firstRound);
    if (early.length) evs = early.concat(evs);
    const total = evs.length;
    replayIdx = Math.max(0, Math.min(replayIdx, total));
    document.getElementById("replayStep").textContent = replayIdx + " / " + total;
    const head = '<div class="sub">双方：' +
      (replay.units_a || []).map((u) => "🔵" + u.troop_type + "×" + u.troops).join(" ") +
      " vs " +
      (replay.units_b || []).map((u) => "🔴" + u.troop_type + "×" + u.troops).join(" ") +
      "</div><hr>";
    const shown = evs.slice(0, replayIdx).map((e) =>
      '<div class="bl-row">' + (e.round ? '<span class="bl-r">R' + e.round + "</span>" : "") +
      esc(e.text || "") + "</div>").join("");
    document.getElementById("replayBody").innerHTML = head + (shown || '<div class="sub">（无事件记录）</div>') +
      '<div class="report-text" style="margin-top:8px">' +
      esc(replay.report_text || "").replace(/\n/g, "<br>") + "</div>";
  }

  document.getElementById("replayPrev").addEventListener("click", () => {
    replayIdx--; renderReplay();
  });
  document.getElementById("replayNext").addEventListener("click", () => {
    replayIdx++; renderReplay();
  });
  document.getElementById("replayClose").addEventListener("click", () => {
    document.getElementById("replayOverlay").hidden = true;
  });
  document.getElementById("replayShare").addEventListener("click", copyReport);
  document.getElementById("duelAccept").addEventListener("click", () => {
    document.getElementById("duelOverlay").hidden = true;
    act2("duel_respond", { accept: true });
  });
  document.getElementById("duelRefuse").addEventListener("click", () => {
    document.getElementById("duelOverlay").hidden = true;
    act2("duel_respond", { accept: false });
  });
  document.getElementById("btnDelegate").addEventListener("click", async () => {
    await act("battle_delegate", { on: !delegated });
  });
  document.getElementById("btnBattleClose").addEventListener("click", () => close());

  // -------------------------------------------------------------------------
  // 生命周期
  // -------------------------------------------------------------------------
  async function open(id, opts) {
    battleId = Number(id);
    spectator = !!(opts && opts.spectator);
    selectedUid = null; mode = "select"; movePlan = null; bs = null;
    lastActor = null;
    view.zoom = 1; view.panX = 0; view.panY = 0;
    document.getElementById("battleOverlay").hidden = false;
    document.getElementById("duelOverlay").hidden = true;
    document.getElementById("replayOverlay").hidden = true;
    const so = document.getElementById("shareOverlay");
    if (so) so.hidden = true;
    setHint(spectator ? "加载观战视图…" : "加载战场…");
    const ok = await refresh();
    resize();
    stopPoll(false);
    if (!ok) {
      // 战场加载失败（战役不存在/无权限/球队列断开）：不要启动轮询，
      // 否则会每 3 秒重复报错刷 toast。
      setHint("战场加载失败，请关闭后重试");
      return;
    }
    pollTimer = setInterval(refresh, POLL_MS);
    tickTimer = setInterval(updateTimer, 1000);
    updateTimer();
  }

  function close() {
    document.getElementById("battleOverlay").hidden = true;
    stopPoll(false);
    bs = null;
    spectator = false;
    if (window.onBattleClosed) window.onBattleClosed();
  }

  function isOpen() {
    return bs !== null && !document.getElementById("battleOverlay").hidden;
  }

  window.addEventListener("resize", () => { if (isOpen()) resize(); });

  window.BattleView = { open, close, isOpen, refresh };
})();
