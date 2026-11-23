/*!
 * 前端时间基准（v4.6.5）
 *
 * 玩家设备时钟偏差（快/慢几分钟甚至几小时）会让「服务端时间戳 - 本地时间」
 * 算出的倒计时提前归零或长时间停滞（典型现象：顶栏「下次推进 00:00」不动）。
 * 这里以服务端时间为基准维护一个偏移量，所有倒计时统一用 nowSec() 计算。
 *
 * 校准来源：
 *   1) GET /api/server_time（页面加载时 + 每 10 分钟兜底刷新）；
 *   2) 任意接口载荷里的 server_now（如 /api/strategy/state，随 20s 轮询持续对齐）。
 * 校准失败时偏移量保持为 0，退化为本地时间，不影响功能。
 */
(function (global) {
  "use strict";

  var REFRESH_MS = 600000;   // 10 分钟兜底校准
  var offsetMs = 0;          // 服务端时间 - 本地时间
  var syncedAt = 0;          // 最近一次对齐时的本地时间戳

  function apply(serverSec) {
    var s = Number(serverSec || 0);
    if (!isFinite(s) || s <= 0) return false;
    offsetMs = s * 1000 - Date.now();
    syncedAt = Date.now();
    return true;
  }

  // 从接口载荷顺手对齐：兼容 {server_now} / {server_time} / {now}
  // 以及 {state: {server_now}}（战略快照把 server_now 放在 state 内）。
  function syncFrom(payload) {
    if (!payload) return false;
    var st = payload.state || {};
    return apply(payload.server_now || payload.server_time || payload.now ||
                 st.server_now || st.server_time);
  }

  function refresh() {
    try {
      return fetch("/api/server_time", {
        cache: "no-store",
        credentials: "same-origin",
      }).then(function (r) {
        return r && r.ok ? r.json() : null;
      }).then(function (d) {
        if (d) apply(d.now || d.server_now);
      }).catch(function () { /* 离线：沿用当前偏移 */ });
    } catch (e) {
      return null;
    }
  }

  function nowMs() { return Date.now() + offsetMs; }
  function nowSec() { return Math.floor(nowMs() / 1000); }

  global.XiuXianClock = {
    syncFrom: syncFrom,
    refresh: refresh,
    nowMs: nowMs,
    nowSec: nowSec,
    offsetMs: function () { return offsetMs; },
    syncedAt: function () { return syncedAt; },
  };

  refresh();
  setInterval(refresh, REFRESH_MS);
})(window);
