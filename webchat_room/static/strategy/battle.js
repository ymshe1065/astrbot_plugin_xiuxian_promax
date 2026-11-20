/* 仙域争霸 · hex 手操战役视图（P2 §7，设计方案 v5）
 * 依赖 strategy.js 的 auth()/api()/toast()/esc()；挂载 window.BattleView。
 * 坐标：odd-q 偏移（奇数列下移半格），与 core/strategy_hex.py 一致。
 */
"use strict";

(function () {
  const POLL_MS = 3000;

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

  let bs = null;              // battle_state 快照
  let battleId = 0;
  let pollTimer = null;
  let tickTimer = null;
  let selectedUid = null;     // 选中的我方单位
  let mode = "select";        // select | deploy | move | attack | skill | ambush
  let skillId = "";
  let movePlan = null;        // {uid, cells: Map("q,r"->{q,r,cost,prev})}
  let hoverHex = null;
  let delegated = false;

  const canvas = document.getElementById("hexCanvas");
  const ctx = canvas.getContext("2d");
  let view = { size: 26, ox: 0, oy: 0 };

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
    let mp = Number(troop.move || 3);
    if ((unit.statuses || {}).suppress) mp *= 0.5;  // 落石/箭雨压制：机动减半
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
    while (cur) { path.unshift([cur.q, cur.r]); cur = cur.prev; }
    return path;
  }

  // 敌方 ZOC 提示格（近似：敌单位 6 邻格，去河流/己方占位）
  function enemyZocTiles() {
    const tiles = new Set();
    for (const u of bs.units || []) {
      if (u.mine || !u.pos || u.troops <= 0 || u.routed) continue;
      if (terrainAt(u.pos[0], u.pos[1]) === "river") continue;
      for (const [q, r] of neighbors(u.pos[0], u.pos[1])) {
        if (!inBounds(q, r)) continue;
        if (terrainAt(q, r) === "river") continue;
        const occ = unitAt(q, r);
        if (occ && occ.mine) continue;  // 友军通道
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
    canvas.width = box.width * dpr;
    canvas.height = box.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const [w, h] = bs.size;
    const needW = w * 1.5 + 0.5, needH = h * Math.sqrt(3) + Math.sqrt(3) / 2;
    view.size = Math.max(14, Math.min(
      (box.width - 24) / needW, (box.height - 40) / needH));
    view.ox = (box.width - needW * view.size) / 2 + view.size;
    view.oy = (box.height - needH * view.size) / 2 + view.size * Math.sqrt(3) / 2;
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
    const actor = (bs.units || []).find((u) => u.uid === bs.current_actor);
    document.getElementById("battleActor").textContent = actor
      ? (actor.mine ? "▶ 轮到你的「" + actor.leader + "」行动"
                    : "⏳ 等待「" + actor.leader + "」行动…")
      : "";
    document.getElementById("btnDelegate").textContent =
      delegated ? "🤖 已托管（点击取消）" : "🤖 托管";
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
    if (bs.state === "done" || bs.phase === "done") {
      el.innerHTML = '<button class="btn small primary" id="btnViewReport">📜 查看战报</button>' +
        '<button class="btn small" id="btnViewReplay">🎞️ 回放</button>';
      bindDoneButtons();
      return;
    }
    if (bs.phase === "deploy") {
      const left = (bs.units || []).filter((u) => u.mine && !u.pos && u.troops > 0);
      let h = '<div class="sub">点击左侧未部署军团 → 点击蓝色部署区落位</div>';
      h += '<div class="ba-btns">';
      h += '<button class="btn small" id="btnAutoDeploy">⚡ 自动部署</button>';
      h += '<button class="btn small primary" id="btnStartBattle"' +
        (left.length ? " disabled" : "") + ">🥁 全军开战</button>";
      h += "</div>";
      if (selectedUid) {
        h += '<div class="ba-btns"><button class="btn small" id="btnAmbush">🌲 设伏（森林格）</button></div>';
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
        (cur.moved ? "（已移动）" : "") + (cur.acted ? "（已行动）" : "") + "</div>";
      h += '<div class="ba-btns">';
      if (!cur.moved && !cur.acted) {
        h += '<button class="btn small primary" id="btnMove">👣 移动</button>';
      }
      if (!cur.acted) {
        h += '<button class="btn small danger" id="btnAttack">⚔️ 攻击</button>';
        h += '<button class="btn small" id="btnDefend">🛡️ 坚守</button>';
        h += '<button class="btn small" id="btnRetreat">🏳️ 溃退</button>';
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
    return act("battle_action", { act: battleAct, params: params || {} });
  }

  async function refresh() {
    try {
      const r = await api("/api/strategy/action", {
        method: "POST",
        body: JSON.stringify({ action: "battle_state",
                               params: { battle_id: battleId } }),
      });
      bs = r;
      delegated = (bs.units || []).some((u) => u.mine && u.delegate);
      renderTop(); renderUnits(); renderActions(); renderLog(); draw();
      checkDuel();
      if (bs.state === "done") { showReport(); stopPoll(true); }
    } catch (e) { /* api 已弹 toast */ }
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
    const dl = bs.phase === "deploy" ? bs.deadline_at : bs.turn_deadline;
    const left = Math.max(0, Number(dl || 0) - Math.floor(Date.now() / 1000));
    el.textContent = (bs.phase === "deploy" ? "部署剩余 " : "回合剩余 ") + left + "s";
    el.classList.toggle("urgent", left <= 10);
  }

  // -------------------------------------------------------------------------
  // Canvas 交互
  // -------------------------------------------------------------------------
  canvas.addEventListener("mousemove", (ev) => {
    if (!bs || !bs.terrain) return;
    const rect = canvas.getBoundingClientRect();
    const [q, r] = pixelToHex(ev.clientX - rect.left, ev.clientY - rect.top);
    const changed = !hoverHex || hoverHex[0] !== q || hoverHex[1] !== r;
    hoverHex = inBounds(q, r) ? [q, r] : null;
    if (changed) draw();
  });

  canvas.addEventListener("click", (ev) => {
    if (!bs || bs.state === "done") return;
    const rect = canvas.getBoundingClientRect();
    const [q, r] = pixelToHex(ev.clientX - rect.left, ev.clientY - rect.top);
    if (!inBounds(q, r)) return;
    const clicked = unitAt(q, r);

    if (bs.phase === "deploy") {
      if (mode === "ambush" && selectedUid) {
        act("battle_set_ambush", { uid: selectedUid, q, r });
        mode = "select"; setHint("");
        return;
      }
      if (clicked && clicked.mine && !clicked.pos) return;
      // 选中未部署单位
      const unplaced = myUnits().filter((u) => !u.pos);
      if (clicked && clicked.mine) { selectedUid = clicked.uid; renderUnits(); renderActions(); draw(); return; }
      if (unplaced.length) {
        const uid = selectedUid && unplaced.some((u) => u.uid === selectedUid)
          ? selectedUid : unplaced[0].uid;
        act("battle_deploy", { placements: [{ uid, q, r }] });
      }
      return;
    }
    if (bs.phase !== "battle") return;

    if (mode === "move" && movePlan && !clicked) {
      const path = pathTo(movePlan.cells, q, r);
      if (!path.length) { toast("该格不可达", "err"); return; }
      const uid = movePlan.uid;
      movePlan = null; mode = "select"; setHint("");
      act2("move", { path });
      return;
    }
    if (mode === "attack" && clicked && !clicked.mine) {
      act2("attack", { target: clicked.uid });
      return;
    }
    if (mode === "duel" && clicked && !clicked.mine) {
      act2("duel_challenge", { target: clicked.uid });
      return;
    }
    if (mode === "skill" && skillId) {
      const info = SKILL_INFO[skillId] || {};
      if (info.target === "unit" && (!clicked || clicked.mine)) {
        toast("请选择敌方单位", "err"); return;
      }
      if (info.target === "friendly" && (!clicked || !clicked.mine)) {
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
  function showReport() {
    if (!bs || bs.state !== "done" || !bs.result) return;
    const res = bs.result;
    const winText = res.winner === "attacker" ? "攻方胜" :
      res.winner === "defender" ? "守方胜" : "平局收场";
    const lines = [
      '<div class="report-head">🏁 ' + winText + " · " + (bs.round || 0) + " 回合</div>",
      '<div class="report-text">' + esc(bs.report_text || "").replace(/\n/g, "<br>") + "</div>",
      '<div class="form-actions"><button class="btn small" id="btnCopyReport">📋 复制战报分享</button></div>',
    ];
    document.getElementById("battleLog").innerHTML = lines.join("");
    const cp = document.getElementById("btnCopyReport");
    if (cp) cp.addEventListener("click", () => copyReport());
  }

  function copyReport() {
    const text = "【仙域争霸战报】" + (bs.report_text || "");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        () => toast("战报已复制，去聊天室分享吧"), () => toast("复制失败", "err"));
    } else { toast("当前浏览器不支持复制", "err"); }
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
    // 回放步进 = 战况事件流（commands 为服务器指令流水，事件文本更适合叙事回放）
    const evs = bs && bs.events ? bs.events : [];
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
  async function open(id) {
    battleId = Number(id);
    selectedUid = null; mode = "select"; movePlan = null; bs = null;
    document.getElementById("battleOverlay").hidden = false;
    document.getElementById("duelOverlay").hidden = true;
    document.getElementById("replayOverlay").hidden = true;
    setHint("加载战场…");
    await refresh();
    resize();
    stopPoll(true);
    pollTimer = setInterval(refresh, POLL_MS);
    tickTimer = setInterval(updateTimer, 1000);
    updateTimer();
  }

  function close() {
    document.getElementById("battleOverlay").hidden = true;
    stopPoll(false);
    bs = null;
    if (window.onBattleClosed) window.onBattleClosed();
  }

  function isOpen() {
    return bs !== null && !document.getElementById("battleOverlay").hidden;
  }

  window.addEventListener("resize", () => { if (isOpen()) resize(); });

  window.BattleView = { open, close, isOpen, refresh };
})();
