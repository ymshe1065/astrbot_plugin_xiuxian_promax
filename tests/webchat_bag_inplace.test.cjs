// 储物戒无感刷新：刷新时复用网格节点，别把筛选/滚动打回初始状态
//
// 行为只能靠 CDP 手测（见 README v3.45.2 条目），这里守住三条契约，
// 防止以后有人把 setPanelCard 改回 view.innerHTML = "" 又把状态清掉。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../webchat_room/static/app.js'), 'utf8');

test('网格带稳定标记并暴露就地重绘入口', () => {
  assert.match(source, /wrap\.dataset\.role = "bag-grid"/, '网格要有 data-role，刷新时靠它找回');
  assert.match(source, /wrap\.__repaint = \(nextItems\)/, '网格要暴露 __repaint 才能原地换数据');
  assert.match(source, /wrap\.__items = items/, '要把当前条目挂在节点上给复用方读取');
});

test('筛选状态提升到模块级，重绘后不丢', () => {
  assert.match(source, /const bagFilter = \{ kind: "", quality: "" \}/, '种类/品质筛选要跨刷新保留');
  assert.match(source, /const state = bagFilter/, 'renderItemGrid 要直接引用模块级筛选状态');
  assert.doesNotMatch(source, /const state = \{ kind: "", quality: "" \}/, '不该再有局部筛选状态');
});

test('储物戒刷新复用旧网格，其余走整体重建', () => {
  assert.match(source, /oldGrid\.__repaint\(newGrid\.__items \|\| \[\]\)/, '要用新卡片的条目重绘旧网格');
  assert.match(source, /newGrid\.replaceWith\(oldGrid\)/, '旧网格节点要挪进新卡片');
  assert.match(source, /oldCard\.replaceWith\(fresh\)/, '卡片外壳仍整体替换');
  assert.match(source, /view\.appendChild\(fresh\)/, '没有旧网格时回退整体重建');
});
