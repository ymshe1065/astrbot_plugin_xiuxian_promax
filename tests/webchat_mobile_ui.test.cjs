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
const vm = require('node:vm');

// 从 strategy.js 截取顶层函数：按大括号配平（这些函数内没有对象解构嵌套
// 到影响配平的程度；safeColor 含正则 {3} 之类不在此处截取）。
function extractStrategyFn(name) {
  const start = strategyJs.indexOf('function ' + name + '(');
  assert.ok(start >= 0, 'strategy.js 缺少函数 ' + name);
  const brace = strategyJs.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < strategyJs.length; i += 1) {
    const ch = strategyJs[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return strategyJs.slice(start, i + 1);
    }
  }
  throw new Error('无法截取函数 ' + name);
}

function renderWorldTab(state) {
  const ctx = {
    S: state,
    logState: { kind: "", items: [], hasMore: false },
    safeColor: (c) => (/^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(String(c || ''))
      ? String(c) : '#9e9e9e'),
    myFactionId: () => Number(((state || {}).player || {}).faction_id || -999),
  };
  vm.createContext(ctx);
  vm.runInContext(extractStrategyFn('esc') + '\n' +
    extractStrategyFn('logKindMeta') + '\n' +
    extractStrategyFn('logCardHtml') + '\n' +
    extractStrategyFn('tabWorld') + '\nthis.__out = tabWorld();', ctx);
  return ctx.__out;
}

test('天下页兼容旧风声字符串与缺失字段，且用户可控文本全部转义', () => {
  // 旧快照的 winds 是字符串数组；字段缺失时不能渲染出 undefined/NaN。
  const legacy = renderWorldTab({
    joined: false,
    winds: ['旧风声 <b>不应加粗</b>'],
    npc_world: {
      factions: [{
        id: 2,
        name: '<img src=x onerror=alert(1)>',
        color: 'javascript:alert(1)',
        public_threat: null,
        share: undefined,
        growth: null,
        tier: null,
        armies: undefined,
        is_npc: true,
        personality_name: '<b>侵略者</b>',
        aggression: null,
        threat_mult: undefined,
        relations: [{ target_name: '<script>x</script>', relation: 'war', h: null }],
        wars: [{ target_name: '<x>', goal_name: '<y>', war_score: undefined,
          exhaustion: null, frenzy_remaining: null, ceasefire_remaining: 2 }],
      }],
    },
  });
  assert.ok(legacy.includes('旧风声 &lt;b&gt;'), '旧字符串风声必须转义后展示');
  assert.ok(legacy.includes('&lt;img src=x onerror=alert(1)&gt;'), '势力名必须转义');
  assert.ok(legacy.includes('&lt;script&gt;x&lt;/script&gt;'), '关系目标名必须转义');
  assert.ok(legacy.includes('background:#9e9e9e'), '非法颜色必须回退灰点');
  assert.ok(!legacy.includes('undefined'), '缺失数值不能渲染为 undefined');
  assert.ok(!legacy.includes('NaN'), '缺失/非法数值不能渲染为 NaN');
  assert.ok(legacy.includes('停火中'), '停火剩余 tick 应正常展示');
  // relations/wars 是对象（旧结构）时按空列表处理，不能抛异常
  const odd = renderWorldTab({
    joined: false,
    npc_world: {
      winds: {},
      factions: [{ id: 3, name: '异常势力', is_npc: true, relations: {}, wars: {} }],
    },
  });
  assert.ok(odd.includes('异常势力'));
  assert.ok(odd.includes('暂无战事'));
});

test('俘虏处置按钮与 sell_enable / ransom_enable / 主副权限一致', () => {
  function renderButtons(state, c, cap, ctx) {
    const context = { S: state, esc: (s) => String(s === null || s === undefined ? '' : s) };
    vm.createContext(context);
    vm.runInContext(extractStrategyFn('captiveActionButtons'), context);
    context.__c = c;
    context.__cap = cap;
    context.__ctx = ctx;
    vm.runInContext('this.__out = captiveActionButtons(__c, __cap, __ctx);', context);
    return context.__out;
  }
  const c = { id: 7, ransom: 0 };
  const all = renderButtons(
    { meta: { captive_cfg: {} } }, c, { can_manage: true }, { kind: 'holding' });
  assert.ok(all.includes('data-act="sell-captive"'), '默认开启时应显示售卖');
  assert.ok(all.includes('data-act="ransom-set"'), '默认开启时应显示定价赎回');
  assert.ok(all.includes('data-act="execute-captive"'), '主/副应显示处决');

  const off = renderButtons(
    { meta: { captive_cfg: { sell_enable: false, ransom_enable: false } } },
    c, { can_manage: true }, { kind: 'holding' });
  assert.ok(!off.includes('data-act="sell-captive"'), 'sell_enable=false 不应显示售卖');
  assert.ok(!off.includes('data-act="ransom-set"'), 'ransom_enable=false 不应显示定价');
  assert.ok(off.includes('data-act="execute-captive"'), '处决不受售卖开关影响');

  const member = renderButtons(
    { meta: { captive_cfg: {} } }, c, { can_manage: false }, { kind: 'holding' });
  assert.ok(!member.includes('data-act="release"'), '非主/副不应显示释放');
  assert.ok(!member.includes('data-act="execute-captive"'), '非主/副不应显示处决');
  assert.ok(!member.includes('data-act="sell-captive"'), '非主/副不应显示售卖');
  assert.ok(member.includes('data-act="persuade"'), '普通成员仍可劝降');
  assert.match(strategyCss, /\.captive-detail \.kv\s*\{[^}]*flex-wrap:\s*wrap/s,
    '俘虏详情长字段在窄屏需换行，不能顶出弹层');
});

test('「天下」页签已注册且未入场可打开', () => {
  assert.match(strategyHtml, /data-tab="world"[^>]*>天下<\/button>/, '战略页需注册天下页签');
  assert.ok(strategyJs.includes('const isWorld = activeTab === "world"'),
    'renderTab 需识别天下页');
  assert.ok(strategyJs.includes('!isGuide && !isWorld'),
    '「天下」「指引」不依赖入场态，未入场也要能渲染');
  // 仅有 renderTab 白名单不够：入场遮罩是全屏 overlay，会把页签一起挡住。
  assert.match(strategyHtml, /id="btnJoinBrowse"/, '入场遮罩需提供「先看看」入口');
  assert.match(strategyHtml, /id="btnJoinOpen"/, '收起遮罩后需保留顶栏「入场」入口');
  assert.ok(strategyJs.includes('|| joinBrowse'), '轮询不能把用户已收起的遮罩反复弹回');
  assert.ok(strategyJs.includes('activateTab("world")'),
    '先看看应直接落到「天下」页');
  // 外交表不能把内部关系码直接展示给玩家（NPC 求和会写 propose_truce_out）。
  for (const rel of ['propose_truce_out', 'propose_ally_out', 'propose_war_out']) {
    assert.ok(strategyJs.includes(rel + ':'), '外交关系码 ' + rel + ' 需要中文文案');
  }
  assert.ok(strategyJs.includes('!r.rel.endsWith("_out")'),
    '已发出的提案不能显示「接受/拒绝」按钮（后端会拒绝）');
});

test('NPC 宣战 / 求和通知不会显示原始 kind', () => {
  assert.ok(strategyJs.includes('kind === "war_declared"'), '宣战通知需要专用文案');
  assert.ok(strategyJs.includes('kind === "peace_offer"'), '求和通知需要专用文案');
  assert.ok(strategyJs.includes('pl.faction_name || "NPC 势力"'),
    '通知文案应带势力名而不是 war_declared');
});

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

test('遗迹可停留判定读取后端 meta.location_cfg.dwell_node_types', () => {
  // 后端 _frontend_meta 把 dwell_node_types 下发在 meta.location_cfg 下；
  // 前端若只读 meta.dwell_node_types 会退回“ruin 不可停留”的旧逻辑，
  // 导致遗迹能任命太守却无法传送人物/编军/派遣。
  assert.ok(
    strategyJs.includes('(meta.location_cfg || {}).dwell_node_types'),
    'dwellTypes 必须读取 meta.location_cfg.dwell_node_types'
  );
  assert.ok(
    strategyJs.includes('meta.dwell_node_types'),
    '同时保留顶层字段兼容旧后端'
  );
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
    strategyJs.includes('军团角色（含主将/随军）只能执行当前据点的本地任务'),
    '远征任务需拦截军团角色'
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

test('任免后的「待发薪」与真正「欠俸」分开显示', () => {
  assert.ok(
    strategyJs.includes('function salaryStateOf(') &&
      strategyJs.includes('function salaryStateText('),
    '战略页需要 salary_state 三态解析函数'
  );
  assert.ok(
    strategyJs.includes('salary_state') && strategyJs.includes('待发薪·下个 tick 生效'),
    '任免后未到发薪 tick 应显示「待发薪」，不能误报「欠俸」'
  );
  assert.match(
    strategyJs,
    /const st = salaryStateOf\(a\);[\s\S]{0,200}欠俸·加成暂停/,
    '俸禄表需按 paid/pending/unpaid 三态渲染'
  );
  assert.ok(
    strategyJs.includes('leader_position_state') &&
      strategyJs.includes('position_state'),
    '军团主将与换将预览也要区分待发薪/欠俸'
  );
});

test('驻扎友方据点补粮无需押运角色', () => {
  assert.ok(
    strategyHtml.includes('id="supplyCharRow"'),
    '补粮弹窗需要可隐藏的押运角色行'
  );
  assert.match(
    strategyJs,
    /const instant = army\.state === "idle" && originFriendly;/,
    '需与后端同口径判断驻扎友方据点'
  );
  assert.ok(
    strategyJs.includes('charRow.hidden = instant') &&
      strategyJs.includes('char_id: instant ? 0 :'),
    '就地补粮时应隐藏押运下拉并提交 char_id=0'
  );
  assert.ok(
    strategyJs.includes('无需押运角色'),
    '提示文案需说明驻扎据点补粮无需押运角色'
  );
});

test('特技 / 个性徽标可点击查看详细加成', () => {
  assert.ok(strategyHtml.includes('id="traitOverlay"'),
    '战略页需要特技详情小弹窗容器');
  assert.ok(strategyHtml.includes('id="traitClose"') &&
    strategyHtml.includes('id="traitDesc"'),
    '弹窗需要关闭按钮与效果说明节点');
  assert.ok(strategyJs.includes('function traitChips(') &&
    strategyJs.includes('function personalityChipHtml('),
    '角色卡/酒馆/俘虏/流将需要统一的徽标渲染函数');
  assert.ok(strategyJs.includes('data-trait-desc') &&
    strategyJs.includes('data-trait-kind'),
    '徽标需携带效果说明与类型，供点击弹窗读取');
  assert.ok(strategyJs.includes('function initTraitPopup(') &&
    strategyJs.includes('closest(".trait-chip")'),
    '弹窗需用事件委托，轮询重绘后仍可点击');
  assert.ok(strategyJs.includes('m.trait_descs') &&
    strategyJs.includes('personality_top_descs'),
    '前端需读取后端 meta 下发的特技/个性说明');
});

test('战略页移动端使用动态视口并避免输入框聚焦缩放', () => {
  assert.match(strategyCss, /body\s*\{[^}]*height:\s*100dvh/s,
    '移动端地址栏收放时不能继续用固定 100vh');
  assert.match(
    strategyCss,
    /@media \(max-width:\s*860px\)[\s\S]*?select,\s*input\[type="number"\][^{]*\{[^}]*font-size:\s*16px/s,
    'iOS 聚焦 13px 输入框会自动放大页面，窄屏需改为 16px'
  );
  assert.match(strategyCss, /\.res-bar\s*\{[^}]*overflow-x:\s*auto/s,
    '中等宽度桌面资源条应自身横向滚动，不能把右侧按钮挤出屏幕'
  );
  assert.match(strategyCss,
    /orientation:\s*landscape\)\s*and\s*\(max-height:\s*540px\)/,
    '横屏手机需要单独恢复左右分栏，避免地图/战场被压扁');
});

test('固定时长任务（计略/侦察/劫掠）倒计时每秒刷新', () => {
  assert.ok(
    strategyJs.includes('function taskCountdownHtml('),
    '固定时长任务需要统一的实时倒计时渲染函数'
  );
  assert.ok(
    strategyJs.includes('t.expect_end_at || t.end_at'),
    '倒计时终点优先用对齐结算 tick 的 expect_end_at，并兼容旧快照 end_at'
  );
  assert.match(
    strategyJs,
    /progress = "剩余 " \+ taskCountdownHtml\(t\)/,
    '计略/侦察等任务必须使用可每秒刷新的 .aucLeft 倒计时'
  );
  assert.ok(
    !strategyJs.includes('progress = "剩余 " + fmtRemain(t.remain_sec)'),
    '不得再用快照静态 remain_sec（只有切页签才更新，表现为卡住）'
  );
  assert.ok(
    strategyJs.includes('data-expire="\' + end + \'"'),
    '实时倒计时要带 data-expire，交给 tickCountdown 每秒刷新'
  );
});

test('天下页提供江湖纪事（筛选/分页/可见性说明）', () => {
  assert.ok(
    strategyJs.includes('function logCardHtml(') &&
      strategyJs.includes('江湖纪事'),
    '天下页需要「江湖纪事」行动历史卡片'
  );
  assert.ok(
    strategyJs.includes('data-act="logs-filter"') &&
      strategyJs.includes('data-act="logs-more"'),
    '纪事需要类别筛选与加载更多'
  );
  assert.ok(
    strategyJs.includes('/api/strategy/logs'),
    '纪事分页需走独立接口，不能只靠 20s 快照'
  );
  assert.ok(
    strategyJs.includes('world_log_kinds') &&
      strategyJs.includes('logKindMeta'),
    '类别图标/名称需读取服务端 meta，避免前后端两套口径'
  );
  assert.ok(
    strategyJs.includes('未走漏的计略先只对当事方可见'),
    '界面需提示纪事按可见性过滤'
  );
});
