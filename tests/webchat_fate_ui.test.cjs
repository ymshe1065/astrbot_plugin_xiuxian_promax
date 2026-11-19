// 仙缘商店看板：/仙缘商店 不该再把 52 件商品画成格子墙糊进聊天流，
// 而是落到右侧「仙缘」页——按分组的小卡片 + 一排层数晶块。
//
// 具体像素只能靠 CDP 手测（tests/_manual_real_input.py 的 fate 场景），
// 这里守住结构契约：归位映射、点卡片先弹菜单、晶块槽位算法、换不了时的按钮文案。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(
  path.join(__dirname, '../webchat_room/static/app.js'),
  'utf8'
);

test('/仙缘商店 归位到右侧看板', () => {
  assert.match(
    source,
    /"\/仙缘商店": "fate"/,
    '缺这条映射，命令回复会当普通卡片塞进聊天流'
  );
  assert.match(
    source,
    /if \(ctx\.panel === "fate"\) \{\s*switchPanelTab\("fate"\);\s*return;/,
    '看板数据走 /api/fate_shop，指令回复要丢弃而不是渲染成卡片'
  );
});

test('点卡片先弹菜单，不直接扣仙缘点', () => {
  assert.match(
    source,
    /card\.addEventListener\("click", \(e\) => openFateMenu\(item, card, e\)\)/,
    '卡片点击必须进菜单，误触就花掉仙缘点代价太大'
  );
  assert.match(
    source,
    /ctxMenuButton\("购买一层 · " \+ item\.cost \+ " 点"/,
    '菜单里要有明确的「购买一层 · N 点」确认项'
  );
  assert.match(
    source,
    /if \(item\.maxed\) \{[\s\S]{0,220}已买满，不能再买/,
    '已满的商品不给购买项，避免点出无效请求'
  );
});

test('层数晶块：有限层用 cap，无上限随层数增长', () => {
  const body = source.slice(
    source.indexOf('function fatePips'),
    source.indexOf('function fateCard')
  );
  assert.ok(body, '找不到 fatePips');
  assert.match(body, /cap\s*\?\s*Math\.min\(cap, 6\)/, '有上限时按 cap 画槽，最多 6 个');
  assert.match(
    body,
    /Math\.min\(6, Math\.max\(3, owned \+ 1\)\)/,
    '无上限时至少 3 个槽、随已买层数增长到 6 个'
  );
  assert.match(body, /i < owned \? "on" : ""/, '买过的层要点亮');
  assert.match(body, /fp-more/, '超出可视槽位时补 +N 角标');
});

test('购买后整页重画要保住滚动位置', () => {
  assert.match(
    source,
    /renderFatePanel\(data, true\)/,
    '买一层会重建 DOM，不传 keepScroll 就弹回顶部'
  );
  assert.match(
    source,
    /const top = keepScroll && scroller \? scroller\.scrollTop : 0;/,
    '重画前要记下滚动位置'
  );
  assert.match(
    source,
    /if \(keepScroll && scroller\) scroller\.scrollTop = top;/,
    '重画后要还原滚动位置'
  );
});

test('换不了点数时说清原因', () => {
  assert.match(
    source,
    /"\u672c\u8f6e\u5df2\u79ef\u6ee1"/,
    '额度用完要提示已积满'
  );
  assert.match(source, /"\u7075\u77f3\u4e0d\u8db3"/, '灵石不够要提示灵石不足');
  assert.doesNotMatch(
    source,
    /exBtn\.textContent = "换 " \+ \(ex\.affordable \|\| 0\) \+ " 点"/,
    '别再出现「换 0 点」这种看不懂的文案'
  );
});
