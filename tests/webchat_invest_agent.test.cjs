// 仙市投资代理人：仓库落桌面板，不在聊天流里堆表单。
// 像素级布局靠浏览器手测，这里守结构契约：
// 入口、接口路径、请求字段、二次确认、面板刷新。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'webchat_room/static/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'webchat_room/static/index.html'), 'utf8');
const roomServer = fs.readFileSync(path.join(root, 'webchat_room/room_server.py'), 'utf8');
const mainPy = fs.readFileSync(path.join(root, 'main.py'), 'utf8');

test('仙市看板有独立页签与容器', () => {
  assert.match(html, /data-tab="invest"[^>]*>仙市</, '缺「仙市」页签');
  assert.match(html, /data-view="invest" id="viewInvest"/, '缺 viewInvest 容器');
});

test('行情卡片带代理人入口，切页即加载', () => {
  assert.match(app, /className = "mk-agent-entry"/, '行情卡底部缺代理人入口');
  assert.match(
    app,
    /agentBtn\.addEventListener\("click", \(\) => switchPanelTab\("invest"\)\)/,
    '入口要切到仙市页'
  );
  assert.match(app, /if \(tab === "invest"\) loadInvestAgentPanel\(\);/, '切页要拉面板数据');
});

test('四个接口路径与请求字段齐全', () => {
  assert.match(app, /fetch\("\/api\/invest\/agents",/, '缺面板 GET');
  assert.match(app, /investAgentAction\("\/api\/invest\/agents\/stop"/, '缺停止入口');
  assert.match(app, /investAgentAction\("\/api\/invest\/agents\/claim"\)/, '缺领取入口');
  assert.match(app, /investAgentAction\("\/api\/invest\/agents\/start",/, '缺雇佣入口');
  assert.match(app, /agent,\s*principal: Number\(investDraft\.principal\) \|\| 0,\s*duration: Number\(investDraft\.duration\) \|\| 0,/, 'start 请求字段不全');
  assert.match(roomServer, /add_get\("\/api\/invest\/agents"/, '路由未注册 GET');
  assert.match(roomServer, /add_post\("\/api\/invest\/agents\/start"/, '路由未注册 start');
  assert.match(roomServer, /add_post\("\/api\/invest\/agents\/stop"/, '路由未注册 stop');
  assert.match(roomServer, /add_post\("\/api\/invest\/agents\/claim"/, '路由未注册 claim');
});

test('停止委托必须二次确认并说明违约金', () => {
  assert.match(app, /window\.confirm\("确定停止？会按当前盘面清仓，并扣本金 1% 违约金。"\)/, '缺二次确认');
});

test('代理人卡片展示等级、经验条与等级收益', () => {
  assert.match(app, /class="ia-lv"/, '缺等级角标');
  assert.match(app, /class="ia-exp"><i style="width:/, '缺经验进度条');
  assert.match(app, /agent\.exp_in_level/, '经验文案未接快照字段');
  assert.match(app, /agent\.level_span/, '等级经验跨度未接快照字段');
  assert.match(app, /agent\.max_principal/, '本金上限未按等级展示');
  assert.match(app, /agent\.base_management_fee/, '降费后要能看出基础费率');
});

test('结算卡与历史展示本次经验', () => {
  assert.match(app, /item\.exp_delta \? " · 经验 "/, '待领取卡未展示经验增减');
  assert.match(app, /item\.exp_delta \? "　经验 "/, '历史记录未展示经验增减');
});

test('面板操作后整页刷新并保留滚动位置', () => {
  assert.match(app, /renderInvestAgentPanel\(data, true\)/, '操作成功后要用返回值重画');
  assert.match(app, /function renderInvestAgentPanel\(data, keepScroll = false\)/, 'render 要支持 keepScroll');
  assert.match(app, /if \(keepScroll && scroller\) scroller\.scrollTop = top;/, '重画后要还原滚动');
});

test('聊天端 /仙市 代理 已接到处理器', () => {
  assert.match(
    mainPy,
    /text\.startswith\(\("代理", "代理人", "投资代理", "agent"\)\)/,
    'main.py 未把「代理」转给 InvestAgentHandlers'
  );
  assert.match(
    mainPy,
    /self\.invest_agent_mgr\.settle_due\(\)/,
    '后台循环未推进代理人结算'
  );
});
