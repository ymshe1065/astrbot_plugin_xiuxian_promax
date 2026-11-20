/* 仙域争霸 · 玩法指引（Agent E）
 * 依赖：guide-data.js（window.STRATEGY_GUIDE）、strategy.js 提供的 .tab-body 容器。
 * 由 strategy.js 的 renderTab 映射 guide: tabGuide 调用；未入场也能查看（不依赖 S）。
 * 纯原生 JS，无外部依赖；事件全部委托在 #tabBody 上，重绘不会重复绑定。
 */
"use strict";

(function () {
  var DATA = window.STRATEGY_GUIDE || { version: "", categories: [], entries: [] };
  var ENTRIES = DATA.entries || [];
  var CATS = DATA.categories || [];
  var HOT = ["民心", "围城", "俘虏", "互市"];   // 空结果推荐词（用户最常问的四个）
  var ALL = "全部";

  // 视图状态：查询词、当前分类、已展开条目（重绘后保持展开）
  var state = { q: "", cat: ALL, open: {} };
  var bound = false;
  var urlApplied = false;

  // -------------------------------------------------------------------------
  // 小工具
  // -------------------------------------------------------------------------
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function escRe(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  // 只把白名单标签当作 HTML：正文里的裸「<20%」「<500」不能被当成
  // 标签起始吞掉后续文本（旧实现 `/<[^>]*>/` 会一路吃到下一个 ">"）。
  var TAG_SPLIT = /(<\/?(?:p|br|strong|b|em|i|u|code|pre|ul|ol|li|span|div|h[1-6]|mark|table|thead|tbody|tr|td|th|small|blockquote|hr)(?:\s[^<>]*)?\/?>)/gi;

  function stripTags(html) { return String(html || "").replace(TAG_SPLIT, " "); }

  // 多关键词：空格（含全角）分隔，全部命中才算匹配（AND）
  function tokens() {
    return state.q.split(/[ \u3000]+/).map(function (t) { return t.trim(); })
      .filter(function (t) { return t.length > 0; });
  }

  function haystack(e) {
    return [e.title, (e.keywords || []).join(" "), e.summary, stripTags(e.body), e.cat].join(" ");
  }

  function matched() {
    var ts = tokens();
    return ENTRIES.filter(function (e) {
      if (state.cat !== ALL && e.cat !== state.cat) return false;
      if (!ts.length) return true;
      var hay = haystack(e).toLowerCase();
      return ts.every(function (t) { return hay.indexOf(t.toLowerCase()) >= 0; });
    });
  }

  // 纯文本高亮：在「未转义原文」上匹配，逐段转义后再包 <mark>，
  // 避免先 esc 得到 &lt; / &amp; 后正则命中实体导致标签残缺。
  function hlText(s, ts) {
    var raw = String(s === null || s === undefined ? "" : s);
    if (!ts.length) return esc(raw);
    var pat = ts.slice().sort(function (a, b) { return b.length - a.length; })
      .map(function (t) { return escRe(t); }).join("|");
    if (!pat) return esc(raw);
    var re;
    try { re = new RegExp("(" + pat + ")", "gi"); } catch (e) { return esc(raw); }
    var out = "", last = 0, m;
    while ((m = re.exec(raw)) !== null) {
      out += esc(raw.slice(last, m.index)) + "<mark>" + esc(m[1]) + "</mark>";
      last = m.index + m[1].length;
      if (m.index === re.lastIndex) re.lastIndex++;  // 零宽匹配保护
    }
    out += esc(raw.slice(last));
    return out;
  }

  // HTML 高亮：只切分白名单标签，标签外文本走 hlText 转义；
  // 裸 "<" 不会被识别为标签，从而正常显示为 < 且不吞掉搜索语料。
  function hlHtml(html, ts) {
    if (!ts.length) return html;
    return String(html).split(TAG_SPLIT).map(function (seg, i) {
      return i % 2 === 1 ? seg : hlText(seg, ts);
    }).join("");
  }

  // -------------------------------------------------------------------------
  // 渲染
  // -------------------------------------------------------------------------
  function catHtml() {
    var ts = tokens();
    var pool = ENTRIES.filter(function (e) {
      if (!ts.length) return true;
      var hay = haystack(e).toLowerCase();
      return ts.every(function (t) { return hay.indexOf(t.toLowerCase()) >= 0; });
    });
    var h = catBtn(ALL, pool.length, false);
    CATS.forEach(function (c) {
      var n = pool.filter(function (e) { return e.cat === c; }).length;
      h += catBtn(c, n, true);
    });
    return h;
  }

  function catBtn(name, n, showCount) {
    var active = state.cat === name ? " active" : "";
    var label = name === ALL ? "🗂 " + ALL : name;
    return '<button class="guide-cat' + active + '" type="button" data-gcat="' + esc(name) + '">' +
      '<span class="gc-name">' + esc(label) + "</span>" +
      (showCount ? '<span class="gc-num">' + n + "</span>" : '<span class="gc-num">' + n + "</span>") +
      "</button>";
  }

  function kwChips(e, ts, limit) {
    var kws = (e.keywords || []).slice(0, limit || 4);
    return kws.map(function (k) {
      return '<span class="guide-kw">' + hlText(k, ts) + "</span>";
    }).join("");
  }

  function itemHtml(e, ts, forceOpen) {
    var isOpen = !!state.open[e.id] || !!forceOpen;
    var h = '<details class="guide-item" data-gid="' + esc(e.id) + '"' + (isOpen ? " open" : "") + ">";
    h += '<summary class="guide-sum">' +
      '<span class="guide-arrow">▸</span>' +
      '<span class="guide-title">' + hlText(e.title, ts) + "</span>" +
      '<span class="guide-tag">' + esc(e.cat) + "</span>" +
      '<span class="guide-brief">' + hlText(e.summary, ts) + "</span>" +
      '<span class="guide-kws">' + kwChips(e, ts, 4) + "</span>" +
      "</summary>";
    h += '<div class="guide-body">' + hlHtml(e.body, ts);
    if ((e.steps || []).length) {
      h += '<div class="guide-steps"><h4>📋 操作步骤</h4><ol>' +
        e.steps.map(function (s) { return "<li>" + hlText(s, ts) + "</li>"; }).join("") +
        "</ol></div>";
    }
    if ((e.tips || []).length) {
      h += '<div class="guide-tips"><h4>💡 小技巧</h4><ul>' +
        e.tips.map(function (s) { return "<li>" + hlText(s, ts) + "</li>"; }).join("") +
        "</ul></div>";
    }
    h += '<div class="guide-foot">适用版本 <code>' + esc(e.patch || DATA.version || "-") + "</code>" +
      ' · 全部关键词：' + (e.keywords || []).map(function (k) {
        return '<button class="guide-kwbtn" type="button" data-gkw="' + esc(k) + '">' + esc(k) + "</button>";
      }).join("") +
      "</div></div></details>";
    return h;
  }

  function emptyHtml() {
    var ts = tokens();
    var h = '<div class="guide-empty">' +
      "<h3>😶 没有找到相关条目</h3>" +
      "<p>试试更短的关键词，或用下面的热门词：</p>" +
      '<div class="guide-hots">' +
      HOT.slice(0, 4).map(function (k) {
        return '<button class="btn small" type="button" data-ghot="' + esc(k) + '">' + esc(k) + "</button>";
      }).join("") + "</div>";
    if (ts.length) {
      h += '<p class="sub">当前检索：' + ts.map(function (t) { return "「" + esc(t) + "」"; }).join(" + ") + "</p>";
    }
    h += "</div>";
    return h;
  }

  function listHtml() {
    var ts = tokens();
    var res = matched();
    if (!res.length) return emptyHtml();
    // 只命中一条时自动展开，省一次点击；其余保持折叠
    var only = res.length === 1;
    return res.map(function (e) { return itemHtml(e, ts, only); }).join("");
  }

  function countText() {
    var ts = tokens();
    var n = matched().length;
    if (!ts.length) return "共 " + n + " 条指引";
    return "命中 " + n + " / " + ENTRIES.length + " 条";
  }

  // -------------------------------------------------------------------------
  // 对外：tab 渲染
  // -------------------------------------------------------------------------
  function tabGuide() {
    ensureBound();
    var h = '<div class="guide-wrap">';
    h += '<div class="guide-search">' +
      '<span class="gs-icon">🔍</span>' +
      '<input id="guideSearch" type="text" autocomplete="off" spellcheck="false" ' +
      'placeholder="搜索玩法：民心 / 围城 / 俘虏 / 互市（空格分隔多关键词）" ' +
      'value="' + esc(state.q) + '">' +
      '<span class="guide-count" id="guideCount">' + esc(countText()) + "</span>" +
      '<button class="btn small" id="guideClear" type="button">清空</button>' +
      "</div>";
    h += '<div class="guide-tip-line sub">支持多关键词组合（空格 AND），匹配标题 / 关键词 / 摘要 / 正文；' +
      "按 <code>Esc</code> 清空。数值口径：<code>" + esc(DATA.version || "v4.1.0") + "</code>（" +
      esc(DATA.updated || "") + "）。</div>";
    h += '<div class="guide-layout">' +
      '<aside class="guide-cats" id="guideCats">' + catHtml() + "</aside>" +
      '<div class="guide-list" id="guideList">' + listHtml() + "</div>" +
      "</div></div>";
    return h;
  }

  // -------------------------------------------------------------------------
  // 交互
  // -------------------------------------------------------------------------
  function inputEl() { return document.getElementById("guideSearch"); }

  function refresh() {
    var list = document.getElementById("guideList");
    if (!list) return;
    list.innerHTML = listHtml();
    var cats = document.getElementById("guideCats");
    if (cats) cats.innerHTML = catHtml();
    var count = document.getElementById("guideCount");
    if (count) count.textContent = countText();
  }

  function setQuery(q) {
    state.q = String(q || "");
    var input = inputEl();
    if (input) input.value = state.q;
    refresh();
    if (input) input.focus();
  }

  function onInput(ev) {
    var t = ev.target;
    if (!t || t.id !== "guideSearch") return;
    state.q = t.value || "";
    refresh();
  }

  function onClick(ev) {
    var t = ev.target;
    if (!t || !t.closest) return;
    var hit = t.closest("[data-gcat],[data-ghot],[data-gkw],#guideClear");
    if (!hit) return;
    if (hit.id === "guideClear") {
      setQuery("");
      return;
    }
    if (hit.dataset.gcat !== undefined) {
      state.cat = hit.dataset.gcat;
      refresh();
      return;
    }
    // 关键词按钮位于 <details> 内部，阻止冒泡避免误触折叠
    ev.preventDefault();
    ev.stopPropagation();
    if (hit.dataset.ghot) setQuery(hit.dataset.ghot);
    else if (hit.dataset.gkw) setQuery(hit.dataset.gkw);
  }

  function onToggle(ev) {
    var t = ev.target;
    if (!t || !t.classList || !t.classList.contains("guide-item")) return;
    state.open[t.dataset.gid] = !!t.open;
  }

  function onKey(ev) {
    if (ev.key !== "Escape" && ev.key !== "Esc") return;
    if (!inputEl()) return;
    setQuery("");
  }

  function ensureBound() {
    if (bound) return;
    var body = document.getElementById("tabBody");
    if (!body) return;
    bound = true;
    body.addEventListener("input", onInput);
    body.addEventListener("click", onClick);
    body.addEventListener("toggle", onToggle, true);  // toggle 不冒泡，需捕获
    document.addEventListener("keydown", onKey);
    // 兜底：strategy.js 的 renderTab 在「未入场」时会提前返回（门禁），
    // 这里在 tab 点击后再检查一次，确保未入场也能打开指引。
    // 若 strategy.js 已注册 guide: tabGuide 并放开未入场渲染，则 #guideSearch 已存在，直接跳过。
    var tabs = document.getElementById("tabs");
    if (tabs) {
      tabs.addEventListener("click", function (ev) {
        var btn = ev.target && ev.target.closest ? ev.target.closest(".tab") : null;
        if (!btn || btn.dataset.tab !== "guide") return;
        setTimeout(function () {
          var el = document.getElementById("tabBody");
          if (!el || document.getElementById("guideSearch")) return;
          el.innerHTML = tabGuide();
        }, 0);
      });
    }
    // 未入场时 strategy.js 每次轮询重绘都会把 tabBody 覆盖成「入场后展示」，
    // 用观察器在「指引 tab 仍激活但内容被冲掉」时补回，避免内容 20 秒闪一次。
    if (window.MutationObserver) {
      var mo = new MutationObserver(function () {
        var active = document.querySelector('.tab.active[data-tab="guide"]');
        if (!active || document.getElementById("guideSearch")) return;
        var el = document.getElementById("tabBody");
        if (el) el.innerHTML = tabGuide();
      });
      mo.observe(body, { childList: true });
    }
  }

  // URL 直达：?guide=围城 自动填入并切到指引页
  function urlQuery() {
    try {
      var m = new RegExp("[?&]guide=([^&#]*)").exec(location.search || "");
      return m ? decodeURIComponent(m[1].replace(/\+/g, " ")) : "";
    } catch (e) { return ""; }
  }

  function applyUrlParam() {
    if (urlApplied) return;
    var q = urlQuery();
    if (!q) return;
    urlApplied = true;
    state.q = q;
    var btn = document.querySelector('.tab[data-tab="guide"]');
    if (btn && !btn.classList.contains("active")) {
      btn.click();          // 交给 strategy.js 的 tab 事件切换并重绘
      return;
    }
    // 已是指引页：直接重绘一次让输入框与结果同步
    if (document.getElementById("tabBody") && typeof window.renderTab === "function") {
      window.renderTab();
    }
  }

  function initGuide() {
    ensureBound();
    applyUrlParam();
  }

  // 全局暴露（strategy.js 的 renderTab 映射 guide: tabGuide）
  window.tabGuide = tabGuide;
  window.initGuide = initGuide;
  // 兼容需要主动查询的调用方
  window.guideSearch = setQuery;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initGuide);
  } else {
    initGuide();
  }
})();
