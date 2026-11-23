// 网页聊天室手机端回归：顶栏挤压、私聊发送键、折叠开关、争霸入口与战略页 hidden 兜底。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '../webchat_room/static');
const css = fs.readFileSync(path.join(root, 'app.css'), 'utf8');
const js = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const strategyCss = fs.readFileSync(path.join(root, 'strategy/strategy.css'), 'utf8');
const strategyJs = fs.readFileSync(path.join(root, 'strategy/strategy.js'), 'utf8');
const strategyHtml = fs.readFileSync(path.join(root, 'strategy/index.html'), 'utf8');

test('移动端顶栏控件不会被 flex 压缩或换行', () => {
  assert.ok(
    css.includes('.topbar-right { flex: 0 0 auto; gap: 3px; min-width: 0; }'),
    '顶栏右侧必须固定宽度，否则 320/360px 下会把账号按钮挤出屏幕'
  );
  assert.match(css, /\.room-tag\s*\{[^}]*white-space:\s*nowrap/s, '在线人数标签必须禁止换行');
  assert.match(css, /\.icon-btn\s*\{[^}]*flex:\s*0 0 auto/s, '图标按钮必须禁止 flex 压缩');
  assert.match(css, /\.fold-all-btn\s*\{[^}]*white-space:\s*nowrap/s, '折叠按钮文字不能竖排');
  assert.match(css, /#btnTheme\s*\{\s*display:\s*none/, '移动端主题按钮应让位给争霸入口');
});

test('私聊发送键不会被 .btn-primary 的 width:100% 撑满输入行', () => {
  assert.match(css, /\.dm-send\s*\{[^}]*width:\s*auto/s, '私聊发送键必须覆盖 .btn-primary 的 width:100%');
  assert.match(css, /\.dm-input-row input\s*\{[^}]*min-width:\s*0/s, '私聊输入框必须允许收缩');
  assert.match(css, /\.dm-input-row input\s*\{\s*font-size:\s*16px/s, '移动端私聊输入框用 16px 防 iOS 聚焦缩放');
});

test('折叠开关同时作用于气泡与卡片消息', () => {
  assert.ok(
    js.includes('box.querySelectorAll(".card.card-foldable")'),
    '全局折叠/展开不能只处理气泡，战报与自动代理详情卡片也要同步'
  );
});

test('移动端底部导航再次点击同一项可以收起看板', () => {
  assert.ok(
    js.includes('if (wasActive && $("panelPane").classList.contains("open"))'),
    '底部导航需要支持二次点击收起，而不是只能点遮罩'
  );
});

test('仙域争霸入口带文字标签，移动端可当前页打开', () => {
  assert.match(html, /id="btnStrategy"[\s\S]*?class="strategy-label">争霸<\/span>/, '顶栏入口必须显示「争霸」文字');
  assert.match(html, /id="accStrategy"/, '账号设置里需要第二个带文字的争霸入口');
  assert.ok(js.includes('const openStrategy = (e) =>'), '移动端 WebView 忽略 target=_blank 时需要回退为当前页跳转');
});

test('战略页面必须用 [hidden] 兜底 display:flex 的弹层', () => {
  assert.match(
    strategyCss,
    /\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}/,
    '缺少 hidden 兜底时战报回放/入场遮罩会全部叠在屏幕上'
  );
  assert.match(strategyCss, /\.btn\s*\{[^}]*white-space:\s*nowrap/s, '战略页按钮文字不能竖排');
});

test('矮屏登录页可以滚动，卡片顶部不会被裁掉', () => {
  assert.match(css, /\.screen\s*\{[^}]*overflow-y:\s*auto/s, 'iPhone SE / 横屏下登录卡片高于视口，必须允许滚动');
  assert.match(css, /\.login-card\s*\{[^}]*margin:\s*auto/s, '滚动容器内用 margin:auto 居中，超高时才会贴顶而不是被裁');
});

test('战略页横屏时地图 / 战役画布不会撑破侧栏', () => {
  assert.match(strategyCss, /\.map-pane\s*\{[^}]*min-height:\s*0/s, 'canvas 固有比例会把 46vh 的地图区撑高，必须允许 flex 收缩');
  assert.match(strategyCss, /\.battle-board\s*\{[^}]*min-height:\s*0/s, '战役画布同样需要 min-height:0，否则横屏下把右侧操作区挤出屏幕');
});

test('战略页据点抽屉与弹层不会超出视口', () => {
  assert.match(
    strategyCss,
    /\.node-drawer\s*\{[^}]*max-height:\s*min\(70vh,\s*calc\(100%\s*-\s*20px\)\)/s,
    '据点抽屉必须限制在地图区域内，长内容不能把顶部顶出屏幕'
  );
  assert.match(
    strategyCss,
    /\.overlay-card\s*\{[^}]*max-height:\s*calc\(100dvh\s*-\s*24px\)[^}]*overflow-y:\s*auto/s,
    '横屏 / 矮屏下入场、编军、回放弹窗需要自身滚动，按钮不能被裁出视口'
  );
});

test('切换非总览页签不会撑破侧栏并导致地图据点点击偏移', () => {
  assert.match(
    strategyCss,
    /\.side-pane\s*\{[^}]*min-width:\s*0/s,
    '侧栏必须允许收缩，否则势力页宽表格会压缩地图，canvas 命中坐标随之偏移'
  );
  assert.match(
    strategyCss,
    /\.tab-body\s*\{[^}]*overflow-x:\s*auto[^}]*min-width:\s*0/s,
    '页签内容应在自身区域内横向滚动，不能把侧栏撑宽'
  );
  assert.ok(
    strategyJs.includes('new ResizeObserver(() => resizeCanvas())'),
    '地图容器尺寸变化后必须重算 canvas view，否则切页后点击据点会落到错误坐标'
  );
});

test('窄屏下的 Toast 与 App 更新弹窗不会超出视口', () => {
  assert.match(
    strategyCss,
    /\.toast-wrap\s*\{[^}]*max-width:\s*min\(360px,\s*calc\(100vw\s*-\s*32px\)\)/s,
    '战略页 Toast 固定 360px 会在 320px 屏幕顶出左侧'
  );
  assert.match(strategyCss, /\.toast\s*\{[^}]*word-break:\s*break-word/s, '长错误文本需要断行');
  assert.match(
    css,
    /\.upd-sheet\s*\{[^}]*max-height:\s*calc\(100dvh\s*-\s*24px\)[^}]*overflow-y:\s*auto/s,
    '横屏下更新弹窗内容需要内部滚动'
  );
  assert.match(css, /\.toast\s*\{[^}]*word-break:\s*break-word/s, '聊天室 Toast 也需要为长文本断行');
});

test('坊市界面具备资源上架、NPC 收购报价与到期提示', () => {
  assert.ok(
    strategyJs.includes('id="mkNode"') && strategyJs.includes('id="mkPrice"'),
    '坊市必须提供资源上架入口（据点/资源/数量/单价）'
  );
  assert.ok(
    strategyJs.includes('market_offer_accept') &&
      strategyJs.includes('market_offer_decline'),
    'NPC 收购报价需要接受/拒绝操作'
  );
  assert.ok(
    strategyJs.includes('player_price_band_pct') && strategyJs.includes('mkRange'),
    '上架单价必须展示并校验市场价波动区间'
  );
  assert.ok(
    strategyJs.includes('data-expire="') && strategyJs.includes('aucLeft'),
    '挂单与报价需要展示剩余有效期'
  );
});

test('人员页承载官职/俘虏，科技与计略迁入总览', () => {
  assert.ok(
    strategyHtml.includes('data-tab="chars" type="button">人员'),
    '「府邸」页签需更名为「人员」'
  );
  assert.ok(
    strategyJs.includes('function tabChars()') &&
      strategyJs.includes('appointmentCard(p.faction)') &&
      strategyJs.includes('captivesCard(p)'),
    '人员页需包含角色名册、官职任免与俘虏事务'
  );
  assert.ok(
    strategyJs.includes('function tabOverview()') &&
      strategyJs.includes('techCard(p.faction)') &&
      strategyJs.includes('plotCard(p)'),
    '科技与计略入口需迁入总览页'
  );
});

test('商路加成与运粮队额外护军具备前端入口', () => {
  assert.ok(
    strategyJs.includes('tradeRouteBonusSummary') &&
      strategyJs.includes('node_output_bonus_per_route'),
    '商路面板需按路线数量/长度展示贸易与据点产出加成'
  );
  assert.ok(
    strategyJs.includes('id="cvEscortTroops"') &&
      strategyJs.includes('id="cvEscortType"') &&
      strategyJs.includes('updateConvoyEscortCost'),
    '运粮队需支持指定额外护送人数/兵种并预览花费'
  );
});

test('军团换将交接具备前端入口与弹窗', () => {
  assert.match(strategyHtml, /id="handoverOverlay"[\s\S]*?id="handoverLeader"/,
    '换将弹窗需要主将选择与确认入口');
  assert.ok(
    strategyJs.includes('function openHandoverModal') &&
      strategyJs.includes('handover_army') &&
      strategyJs.includes('onclick="openHandoverModal('),
    '驻守军团卡片需提供「换将」按钮并提交 handover_army 动作'
  );
});

test('驻守军团成员可执行本地任务且出军自动中断', () => {
  assert.ok(
    strategyJs.includes('localArmyTaskCharsAtNode') &&
      strategyJs.includes('data-army="1"'),
    '任务下拉需列出驻守军团成员并标记「随军」'
  );
  assert.ok(
    strategyJs.includes('随军角色只能执行当前据点的本地任务'),
    '远征任务需拦截随军角色'
  );
});

test('手操战役提示不会被独立战略页漏掉', () => {
  assert.ok(
    js.includes('postStrategyBroadcast(m)') && js.includes('strategy.battle.event'),
    '聊天室收到 WS 后必须把争霸通知/回合事件转发给独立战略页标签'
  );
  assert.ok(
    strategyJs.includes('BroadcastChannel("xiuxian_strategy")'),
    '战略页需要监听聊天室转发的 strategy.notify / battle.event'
  );
  assert.ok(
    strategyJs.includes('function onStrategyNotice(') &&
      strategyJs.includes('battle_turn') && strategyJs.includes('battle_result'),
    '战略页必须处理手操开启/轮到行动/终局三类提示'
  );
  assert.match(
    strategyCss,
    /\.battle-alert\s*\{[^}]*display:\s*flex/s,
    '进行中手操战役需要常驻顶部横幅，不能只靠一闪而过的 toast'
  );
  assert.ok(
    strategyHtml.includes('id="battleAlert"'),
    '战略页 DOM 需要手操战役常驻横幅容器'
  );
  assert.ok(
    strategyJs.includes('function schedulePoll()') &&
      strategyJs.includes('liveCount ? 5000 : POLL_MS'),
    '存在手操战役时轮询需缩短到 5s，20s 间隔会错过 90s 部署/30s 回合窗口'
  );
  assert.ok(
    strategyJs.includes('if (x.live !== y.live) return x.live ? -1 : 1;'),
    '战报页高光排序不得把进行中的手操战役压到已结束战报下面'
  );
  assert.ok(
    strategyJs.includes('field_encounter: "野战"'),
    '普通行军抵达触发的野战需要在战报页显示为「野战」'
  );
});
