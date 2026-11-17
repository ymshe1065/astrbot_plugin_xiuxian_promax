// 指令联想的纯函数回归：token 提取、匹配排序、Tab 补全替换。
// 直接从 app.js 抽取函数在 vm 里跑，不依赖浏览器与服务端。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../webchat_room/static/app.js'), 'utf8');

// vm 里创建的数组/对象原型与宿主不同，先转成普通值再比较
const plain = (value) => JSON.parse(JSON.stringify(value));

function setup() {
  const ctx = vm.createContext({});
  for (const name of ['cmdSuggestToken', 'cmdSuggestOnlyExact', 'matchCommandSuggest', 'completeCommandText']) {
    const start = source.indexOf(`  function ${name}(`);
    assert.ok(start >= 0, `app.js 缺少函数 ${name}`);
    const end = source.indexOf('\n  }', start) + 4;
    assert.ok(end > start, `无法定位函数 ${name} 的结尾`);
    vm.runInContext(source.slice(start, end), ctx);
  }
  return ctx;
}

const ITEMS = [
  { cmd: '/储物戒', label: '储物戒', desc: '查看储物戒', group: '物品' },
  { cmd: '/储物戒扩容', label: '储物戒扩容', desc: '', group: '物品' },
  { cmd: '/批量回收', label: '批量回收', desc: '回收储物戒物品', group: '物品' },
  { cmd: '/签到', label: '签到', desc: '', group: '日常' },
];
const ALIASES = { bag: '/储物戒', 背包: '/储物戒' };

test('只对行首 / 后的第一个 token 联想', () => {
  const { cmdSuggestToken } = setup();
  assert.equal(cmdSuggestToken('/储'), '储');
  assert.equal(cmdSuggestToken('/'), '');
  assert.equal(cmdSuggestToken('/储物 3'), '储物');
  assert.equal(cmdSuggestToken('你好'), null);
  assert.equal(cmdSuggestToken(''), null);
});

test('精确命中但还有更长指令时继续联想，只有唯一完整指令才收起', () => {
  const { cmdSuggestOnlyExact } = setup();
  // /拍卖会 是完整指令，但还有 /拍卖会报名、/拍卖会出价，不能收起
  const auction = [
    { cmd: '/拍卖会', label: '拍卖会', desc: '', group: '交易' },
    { cmd: '/拍卖会报名', label: '拍卖会报名', desc: '', group: '交易' },
    { cmd: '/拍卖会出价', label: '拍卖会出价', desc: '', group: '交易' },
  ];
  assert.equal(cmdSuggestOnlyExact(auction, '拍卖会'), false);
  assert.equal(cmdSuggestOnlyExact(ITEMS, '储物戒'), false);
  // /签到 没有更长候选，收起
  assert.equal(cmdSuggestOnlyExact(ITEMS, '签到'), true);
  // 半截输入与别名都不收起
  assert.equal(cmdSuggestOnlyExact(ITEMS, '储物'), false);
  assert.equal(cmdSuggestOnlyExact(ITEMS, 'bag'), false);
  assert.equal(cmdSuggestOnlyExact(ITEMS, ''), false);
});

test('正式指令精确匹配排第一，前缀次之', () => {
  const { matchCommandSuggest } = setup();
  const exact = matchCommandSuggest(ITEMS, ALIASES, '储物戒', []);
  assert.equal(exact[0].cmd, '/储物戒');
  const prefix = matchCommandSuggest(ITEMS, ALIASES, '储物', []);
  assert.deepEqual(plain(prefix.map((it) => it.cmd)).slice(0, 2), ['/储物戒', '/储物戒扩容']);
});

test('别名精确与前缀都能命中并补全为正式指令', () => {
  const { matchCommandSuggest } = setup();
  assert.equal(matchCommandSuggest(ITEMS, ALIASES, 'bag', [])[0].cmd, '/储物戒');
  assert.equal(matchCommandSuggest(ITEMS, ALIASES, '背包', [])[0].cmd, '/储物戒');
  assert.equal(matchCommandSuggest(ITEMS, ALIASES, 'ba', [])[0].cmd, '/储物戒');
});

test('按钮文案关键词作为兜底命中', () => {
  const { matchCommandSuggest } = setup();
  const hit = matchCommandSuggest(ITEMS, ALIASES, '回收', []);
  assert.deepEqual(plain(hit.map((it) => it.cmd)), ['/批量回收']);
});

test('无匹配返回空，最多返回 10 条', () => {
  const { matchCommandSuggest } = setup();
  assert.equal(matchCommandSuggest(ITEMS, ALIASES, '不存在', []).length, 0);
  const many = Array.from({ length: 30 }, (_, i) => ({
    cmd: `/测试指令${i}`, label: `测试指令${i}`, desc: '', group: '测试',
  }));
  assert.equal(matchCommandSuggest(many, {}, '测试', []).length, 10);
});

test('空查询把最近使用置顶，其余按目录顺序且不重复', () => {
  const { matchCommandSuggest } = setup();
  const list = plain(matchCommandSuggest(ITEMS, ALIASES, '', ['/签到', '/储物戒']));
  assert.deepEqual(list.slice(0, 2).map((it) => it.cmd), ['/签到', '/储物戒']);
  assert.equal(list[0].recent, true);
  assert.equal(new Set(list.map((it) => it.cmd)).size, list.length);
  assert.deepEqual(list.slice(2).map((it) => it.cmd), ['/储物戒扩容', '/批量回收']);
});

test('同分时最近使用优先', () => {
  const { matchCommandSuggest } = setup();
  const items = [
    { cmd: '/测试甲', label: '测试甲', desc: '', group: '测试' },
    { cmd: '/测试乙', label: '测试乙', desc: '', group: '测试' },
  ];
  const list = plain(matchCommandSuggest(items, {}, '测试', ['/测试乙']));
  assert.equal(list[0].cmd, '/测试乙');
});

test('Tab 补全替换第一个 token 并保留后面的参数', () => {
  const { completeCommandText } = setup();
  assert.deepEqual(plain(completeCommandText('/储', '/储物戒')), { text: '/储物戒 ', caret: 5 });
  assert.deepEqual(plain(completeCommandText('/储物 3', '/储物戒')), { text: '/储物戒 3', caret: 6 });
  assert.deepEqual(plain(completeCommandText('普通消息', '/储物戒')), { text: '普通消息', caret: 4 });
});
