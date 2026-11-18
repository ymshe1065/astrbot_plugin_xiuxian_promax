// 意见反馈：普通玩家与管理员共用一套列表，但管理员需要看到提交者，
// 且 200 条保留量必须能靠「加载更多」翻到，不能只显示最新 30 条。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '../webchat_room/static');
const js = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'app.css'), 'utf8');

test('反馈列表提供「加载更多」入口', () => {
  assert.match(html, /id="fbMore"/, '缺少加载更多按钮，管理员只能看到最新 30 条');
  assert.match(js, /const FB_PAGE = 30;/, '缺少分页大小常量');
  assert.match(js, /&before=\$\{before\}/, '翻页必须带 before 游标');
  assert.match(
    js,
    /\$\("fbMore"\)\.addEventListener\("click", \(\) => loadFeedback\(false\)\)/,
    '加载更多要追加下一页而不是重置列表'
  );
  assert.match(css, /\.fb-more\s*\{/, '加载更多按钮缺少样式');
});

test('管理员全量列表显示提交者账号', () => {
  assert.ok(
    js.includes('if (admin && it.username) snapBits.push(`提交者 ${it.username}`)'),
    '管理员看到的反馈必须带提交者账号，昵称可能重复'
  );
});

test('追加分页使用 insertAdjacentHTML，保留已加载内容', () => {
  assert.ok(
    js.includes('box.insertAdjacentHTML("beforeend", html)'),
    '加载更多应追加 DOM，不能覆盖已加载的反馈'
  );
});

test('连续点击「加载更多」不会重复追加同一页', () => {
  assert.ok(js.includes('if (!reset && S.feedbackAppending) return;'), '缺少追加中的并发守卫');
  assert.ok(js.includes('$("fbMore").disabled = true;'), '请求期间应禁用加载更多按钮');
  assert.ok(js.includes('if (seq !== S.feedbackSeq) return;'), '过期响应应被丢弃');
});
