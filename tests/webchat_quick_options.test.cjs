// Run actual frontend functions in an isolated DOM stub; no browser/server required.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../webchat_room/static/app.js'), 'utf8');
function setup() {
  const box = { hidden: true, children: [], appendChild(el) { this.children.push(el); },
    set innerHTML(value) { this.children = []; } };
  const sent = [];
  const ctx = vm.createContext({
    S: { username: 'me', pending: new Map(), cards: new Map(), ws: { readyState: 1, send: m => sent.push(m) } },
    $: () => box,
    document: { createElement: () => ({ addEventListener() {} }) },
    addBotText() {}, addCardMessage() {}, addChat() {}, addSys() {}, scrollBottom() {},
    markBagDirty() {}, toast() {}, addError() {}, panelOfCmd: () => '', reqId: () => 'r1', setTimeout() {},
  });
  for (const name of ['showQuickOptions', 'addBotMessage', 'applyChannelHistory', 'handleReply', 'sendChat', 'sendCmd', 'submitSheet', 'pickSheetOption']) {
    const start = source.indexOf(`  function ${name}(`);
    assert.ok(start >= 0);
    const end = source.indexOf('\n  }', start) + 4;
    vm.runInContext(source.slice(start, end), ctx);
  }
  return { ctx, box, sent };
}
const options = [{ label: '青云宗', cmd: '/加入宗门 青云宗' }];

test('宗门候选显示后，无候选的新回复会清空', () => {
  const { ctx, box } = setup();
  ctx.addBotMessage({ actor: 'me', text: '宗门列表', options });
  assert.equal(box.hidden, false);
  assert.equal(box.children.length, 1);
  ctx.addBotMessage({ actor: 'me', text: '已加入宗门' });
  assert.equal(box.hidden, true);
  assert.equal(box.children.length, 0);
});
test('刷新后的历史宗门列表不能恢复输入提示', () => {
  const { ctx, box } = setup();
  ctx.applyChannelHistory([{ type: 'bot', actor: 'me', text: '宗门列表', options }]);
  assert.equal(box.hidden, true);
  assert.equal(box.children.length, 0);
});
test('他人与自动代理消息不修改当前候选', () => {
  const { ctx, box } = setup();
  ctx.addBotMessage({ actor: 'other', options });
  assert.equal(box.hidden, true);
  ctx.addBotMessage({ actor: 'me', options });
  ctx.addBotMessage({ actor: 'other', options: [] });
  ctx.addBotMessage({ actor: 'me', channel: 'auto', options: [] });
  assert.equal(box.hidden, false);
  assert.equal(box.children.length, 1);
});
test('普通发送和指令发送清除候选，后台看板刷新保留候选', () => {
  const { ctx, box } = setup();
  ctx.showQuickOptions(options);
  ctx.sendChat('/宗门信息');
  assert.equal(box.hidden, true);
  ctx.showQuickOptions(options);
  ctx.sendCmd('/储物戒', { panel: 'bag' });
  assert.equal(box.hidden, false);
  ctx.sendCmd('/宗门信息');
  assert.equal(box.hidden, true);
});
test('定向无候选回复及错误回复清除旧提示', () => {
  const { ctx, box } = setup();
  ctx.handleReply({ req_id: 'r', text: '宗门列表', options });
  assert.equal(box.hidden, false);
  ctx.handleReply({ req_id: 's', text: '失败', error: true });
  assert.equal(box.hidden, true);
});

test('跳过火候后手动输入 1 次会使用显式数量，不误选火候', async () => {
  const { ctx } = setup();
  const input = { value: '1' };
  ctx.$ = () => input;
  ctx.sheetCtx = { name: '炼丹', cmd: '/炼丹', values: ['1', '-'], chosen: [],
    data: { field: { key: 'count', optional: true } } };
  ctx.loadSheetField = async () => { ctx.sheetCtx.data = { field: { key: 'materials', optional: true } }; };
  ctx.submitSheet();
  await Promise.resolve();
  assert.equal(ctx.sheetCtx.values.at(-1), 'x1');
});
test('材料可留空发送，也可连续点选多个类别自动填参', async () => {
  const { ctx } = setup();
  const input = { value: '' };
  ctx.$ = () => input;
  ctx.sheetCtx = { name: '炼丹', cmd: '/炼丹', values: ['1', '-', '-'], chosen: [],
    data: { min: 1, field: { key: 'materials', optional: true } } };
  ctx.loadSheetField = async () => {};
  ctx.pickSheetOption({ value: '圣品灵植=幽冥草', label: '幽冥草' });
  await Promise.resolve();
  ctx.pickSheetOption({ value: '圣品矿材=圣矿', label: '圣矿' });
  await Promise.resolve();
  let sent = '';
  ctx.sheetApi = () => ({});
  ctx.closeSheet = ctx.closeDrawer = () => {};
  ctx.dispatchCommand = text => { sent = text; };
  ctx.submitSheet();
  assert.equal(sent, '/炼丹 1 - - 圣品灵植=幽冥草 圣品矿材=圣矿');
  ctx.sheetCtx.values = ['1', '-', '-'];
  ctx.submitSheet();
  assert.equal(sent, '/炼丹 1 - -');
});
