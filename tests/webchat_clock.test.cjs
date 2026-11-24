// 前端时间基准回归（v4.6.5）
// 现象：玩家设备时钟偏快（实测 +2.5h）时，顶栏「下次推进」用
// Date.now() 与服务器 next_tick_at 相减恒为负，被 Math.max(0, …) 夹成 00:00
// 后一直不动。这里锁定 clock.js 的校准逻辑与三处前端接线。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '../webchat_room/static');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');
const clockSrc = read('clock.js');
const strategyJs = read('strategy/strategy.js');
const battleJs = read('strategy/battle.js');
const appJs = read('app.js');
const indexHtml = read('index.html');
const strategyHtml = read('strategy/index.html');

// 启动 clock.js：可控本地时间 + 可控 /api/server_time 响应
function bootClock(localNowMs, serverTimeResp) {
  const state = { now: localNowMs };
  const sandbox = {
    setInterval: () => 0,
    fetch: async () => ({
      ok: true,
      json: async () => serverTimeResp || { ok: true, now: 0 },
    }),
  };
  sandbox.window = sandbox;
  sandbox.Date = { now: () => state.now };
  vm.createContext(sandbox);
  vm.runInContext(clockSrc, sandbox);
  return { clock: sandbox.XiuXianClock, state };
}

test('clock.js：设备时钟快 2.5 小时也不会让倒计时提前归零', () => {
  const serverNow = 1795451092;               // 服务器当前 epoch 秒
  const { clock, state } = bootClock(1795460070000); // 本地快了 8978s
  clock.syncFrom({ server_now: serverNow });
  assert.equal(clock.nowSec(), serverNow);
  assert.equal(clock.offsetMs(), (serverNow * 1000) - 1795460070000);

  // 时间继续走 60s：仍锚在服务端时间轴，剩余时间照常递减
  state.now += 60000;
  const nextAt = serverNow + 900;
  assert.equal(clock.nowSec(), serverNow + 60);
  assert.equal(Math.max(0, nextAt - clock.nowSec()), 840);
});

test('clock.js：从 state.server_now（战略快照）也能对齐', () => {
  const { clock } = bootClock(1795460070000);
  clock.syncFrom({ state: { server_now: 1795451092 } });
  assert.equal(clock.nowSec(), 1795451092);
});

test('clock.js：非法/缺失基准时退化为本地时间，不影响功能', () => {
  const { clock } = bootClock(1795460070000);
  clock.syncFrom(null);
  clock.syncFrom({});
  clock.syncFrom({ server_now: 0 });
  clock.syncFrom({ server_now: "bad" });
  assert.equal(clock.offsetMs(), 0);
  assert.equal(clock.nowSec(), Math.floor(1795460070000 / 1000));
});

test('clock.js：refresh() 从 /api/server_time 校准服务端时间', async () => {
  const { clock } = bootClock(1795460070000, { ok: true, now: 1795451092 });
  await clock.refresh();
  assert.equal(clock.nowSec(), 1795451092);
  assert.ok(clock.syncedAt() > 0);
});

test('页面接入：两个页面都先加载 clock.js', () => {
  assert.match(indexHtml, /<script src="\/static\/clock\.js"><\/script>/);
  assert.match(strategyHtml, /<script src="\/static\/clock\.js"><\/script>/);
  // 必须在业务脚本之前加载
  assert.ok(indexHtml.indexOf('clock.js') < indexHtml.indexOf('app.js'));
  assert.ok(strategyHtml.indexOf('clock.js') < strategyHtml.indexOf('battle.js'));
});

test('strategy.js：顶栏推进/建造/拍卖等倒计时统一走 srvNowSec()', () => {
  assert.match(strategyJs, /function srvNowSec\(\)/);
  assert.match(
    strategyJs,
    /function tickCountdown\(\)\s*\{[\s\S]*?S\.world\.next_tick_at - srvNowSec\(\)/);
  // 快照载入即校准
  assert.match(strategyJs, /XiuXianClock\.syncFrom\(S\)/);
  // 除 srvNowSec() 内部兜底外，不再直接用本地秒戳做服务端时间计算
  const raw = strategyJs.match(/Math\.floor\(Date\.now\(\) \/ 1000\)/g) || [];
  assert.equal(raw.length, 1, '仅允许 srvNowSec() 内的本地时间兜底');
});

test('strategy.js：持续任务倒计时对齐 world tick，到点显示结算中而非停在 0s', () => {
  // 服务端下发 expect_settle_at（小时边界向上取整到推进时刻）
  assert.match(strategyJs, /function taskSettleAt\(t\)/);
  assert.match(strategyJs, /t\.expect_settle_at/);
  assert.match(strategyJs, /S\.meta[\s\S]{0,80}tick_seconds/);
  // 任务行使用对齐后的时刻，并带「结算中…」占位
  assert.match(
    strategyJs,
    /data-expire="' \+ next \+[\s\S]{0,120}data-settle="1" data-pending="结算中…"/);
  // 每秒刷新时过期不再固定写 0s，并主动重取一次快照
  assert.match(
    strategyJs,
    /left <= 0 && node\.dataset\.pending[\s\S]{0,400}settleRefreshFor !== expiredSettle/);
});

test('battle.js / app.js：手操倒计时与休整判断也走服务端时间', () => {
  assert.match(battleJs, /function srvNowSec\(\)/);
  assert.match(
    battleJs,
    /function updateTimer\(\)\s*\{[\s\S]*?Number\(dl \|\| 0\) - srvNowSec\(\)/);
  assert.match(appJs, /const srvNowSec = \(\) =>/);
  assert.match(appJs, /Number\(cfg\.close_until \|\| 0\) > srvNowSec\(\)/);
});
