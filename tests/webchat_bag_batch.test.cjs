// 储物戒多选批量操作：条目键与批量指令生成
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../webchat_room/static/app.js'), 'utf8');

function load() {
  const ctx = vm.createContext({});
  for (const name of ['bagEntryKey', 'bagRecycleCommand', 'bagGiftCommands']) {
    const start = source.indexOf(`  function ${name}(`);
    assert.ok(start >= 0, `missing ${name}`);
    const end = source.indexOf('\n  }', start) + 4;
    vm.runInContext(source.slice(start, end), ctx);
  }
  return ctx;
}

test('同名装备按品质区分，堆叠物品只使用名称', () => {
  const { bagEntryKey } = load();
  assert.equal(bagEntryKey({ name: '青锋剑', quality: '极佳' }), '青锋剑|极佳');
  assert.equal(bagEntryKey({ name: '精铁', quality: '' }), '精铁');
});

test('批量回收使用完整数量并保留品质', () => {
  const { bagRecycleCommand } = load();
  assert.equal(bagRecycleCommand({ name: '青锋剑', quality: '大师', count: 3 }), '/回收 青锋剑|大师 3');
  assert.equal(bagRecycleCommand({ name: '精铁', count: 0 }), '/回收 精铁 1');
});

test('装备实例逐件生成赠予请求，堆叠物品一次提交全部数量', () => {
  const { bagGiftCommands } = load();
  assert.equal(
    JSON.stringify(bagGiftCommands({ name: '青锋剑', quality: '良好', count: 2, kind: '装备' }, 'web_a')),
    JSON.stringify(['/赠予 web_a 青锋剑|良好 1', '/赠予 web_a 青锋剑|良好 1'])
  );
  assert.equal(
    JSON.stringify(bagGiftCommands({ name: '精铁', count: 12, kind: '材料' }, 'web_a')),
    JSON.stringify(['/赠予 web_a 精铁 12'])
  );
});
