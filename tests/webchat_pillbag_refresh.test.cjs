// 丹药背包自动刷新：服用/炼丹/买卖改了丹药数量后，右侧看板要自己重画，
// 不能等玩家切页或手动再敲一次 /丹药背包。
//
// 这条链路是「后端推 pillbag_changed → 前端标脏 + 就地重取」，行为只能靠
// CDP 手测（见 tests/_manual_real_input.py 的 pill 场景），这里守住契约，
// 防止以后有人把事件分支或脏标记删掉，又退化成「吃掉一颗，页面还写着 ×2」。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(
  path.join(__dirname, '../webchat_room/static/app.js'),
  'utf8'
);

test('pillbag_changed 落到丹药背包的标脏与重取', () => {
  assert.match(source, /case "pillbag_changed":/, '缺这条事件，服用后看板永远不动');
  assert.match(
    source,
    /case "pillbag_changed":[\s\S]{0,240}markPillBagDirty\(\)[\s\S]{0,80}refreshPillBagSoon\(\)/,
    '收到推送要标脏并触发刷新'
  );
});

test('刷新只在丹药背包页激活时发请求', () => {
  assert.match(
    source,
    /function pillBagTabActive\(\)[\s\S]{0,200}dataset\.tab === "pillbag"/,
    '要能判断当前是否停在丹药背包页'
  );
  assert.match(
    source,
    /function refreshPillBagSoon[\s\S]{0,400}if \(!pillBagTabActive\(\)\) return;/,
    '不在丹药背包页就只标脏，别白发请求'
  );
  assert.match(
    source,
    /panelPending\("pillbag"\) && retry < 5/,
    '已有请求在路上时要重试，避免拿到变更前的卡片'
  );
});

test('切页时脏标记参与判重，渲染后清脏', () => {
  assert.match(
    source,
    /S\.pillBagDirty \|\| !S\.panelCard\.pillbag \|\| Date\.now\(\) - pillBagLoadedAt > 3000/,
    '脏标记、无卡片、过期三种情况都要重拉'
  );
  assert.match(
    source,
    /if \(which === "pillbag"\) \{\s*S\.pillBagDirty = false;/,
    '渲染成功后要清脏，否则每次切页都重复请求'
  );
});

test('换连接后旧 socket 的迟到事件不能踩掉在线状态', () => {
  // 旧连接的 onclose 把 S.connected 置 false 之后，所有推送驱动的刷新
  // 都会被「未连接」守卫静默吞掉——这正是「看板不自动刷新」的真凶。
  assert.match(source, /const stale = \(\) => S\.ws !== ws;/, '要有当前连接的判定');
  assert.match(
    source,
    /ws\.onopen = \(\) => \{\s*if \(stale\(\)\) return;/,
    '旧连接的 onopen 不该再拉看板'
  );
  assert.match(
    source,
    /ws\.onclose = async \(e\) => \{\s*if \(stale\(\)\) return;/,
    '旧连接的 onclose 不该把新连接标成断开'
  );
  assert.match(
    source,
    /ws\.onmessage = \(ev\) => \{[\s\S]{0,160}if \(stale\(\)\) return;/,
    '旧连接残留的消息不该再渲染'
  );
});
