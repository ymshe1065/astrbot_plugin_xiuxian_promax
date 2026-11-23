# 仙域争霸 · 缺口补全实施契约（v4.1.0 / DB v77）

> **历史契约说明（2026-11-23 复核更新）**：本文件 §0~§4 是 v4.1.0（DB v77 / 221 tests）时期的并行开发契约，其中多数“未接线 / 待补”条目已在 v4.2~v4.6 落地。**当前实际状态、已修复项与待确认缺口统一见文末 §5**（基线：插件 v4.6.4 / DB v86 / `tests/test_strategy_*.py` 659 passed）。

> 本文件是并行开发的**唯一接口契约**。所有 subagent 必须遵守：
> 1. 只改自己「文件所有权」内的文件；需要他人文件配合时，按本契约约定调用，**不要自行修改他人文件**。
> 2. 新增配置项一律已在 `config/strategy_config.json` 就绪，**不要改该文件**。
> 3. 新增 DB 列/表已由 v77 迁移就绪（`data/migration.py`），**不要改该文件**。
> 4. 完成后必须跑通：`python3 -m pytest tests/test_strategy_*.py -q`。
> 5. 输出中文，代码注释保持现有风格（`# §x.y 标题`）。

---

## 0. 基线

| 项 | 值 |
|---|---|
| 插件版本 | v4.0.24（本次升 v4.1.0） |
| 数据库 | v76 → **v77**（已迁移） |
| 设计文档 | `docs/design/仙域争霸战略玩法设计方案.md`（下称「设计稿」） |
| 现有测试 | 221 passed |

### v77 已就绪的 schema（禁止再改）

| 表/列 | 用途 |
|---|---|
| `strategy_nodes.hatred_json` | NPC 仇恨记忆 `{"<faction_id>": ticks}` |
| `strategy_nodes.respawn_at` | NPC 驻军重生时间戳（0=无需） |
| `strategy_nodes.lair_json` | 匪寨状态 `{"last_raid_tick": n}` |
| `strategy_factions.last_capital_move_at` | 迁都冷却起点 |
| `strategy_factions.rebuild_fund` | 流亡重建合力资金 |
| `strategy_players.last_action_at` | 限速 |
| `strategy_faction_tasks` | 势力任务（id/world_id/faction_id/task_type/title/node_id/target_node/assigned_user/assigned_char/progress/target/reward_json/status/start_at/end_at） |
| `strategy_mobilizations` | 动员令（id/world_id/faction_id/issued_by/kind/buff_json/start_at/end_at/status） |
| `strategy_audit` | 审计（id/world_id/user_id/action/detail_json/created_at） |
| `strategy_captive_transfers` | 俘虏押运（id/world_id/char_id/faction_id/from_node/to_node/depart_at/arrive_at/status） |

### 已就绪但「未接线」的配置（直接用，不要新增同名键）

`army.concurrent_per_faction_levels`、`army.overstaff_morale_penalty`、
`battle.pursuit_capture`、`battle.rout_troops_pct`、`battle.rout_leader_injury_chance`、
`battle.rout_leader_injury`、`battle.reorganize_ticks`、
`battle.night_buddha_morale`、`battle.night_raid_loot_bonus`、
`npc.respawn_hours`、`npc.respawn_hatred`、`bandit.raid_interval_ticks`、
`market.cross_faction_market_only`、`market.items`（已含 `intel`）、
`captive.jail_slots_per_node`、`captive.defect_loyalty`、`captive.defect_rep_cost`、
`faction.capital_relocate_cd_days`、`task.mini_event_chance`、
`task.scout_capture_chance`、`task.raid_rep_cost`、`task.raid_counterkill_chance`、
`convoy.discount_pct`、`duel.drum_tower_mult`。

### 本次新增配置块

`governance.{relief_*, govern_morale_per_tick, heal_morale_per_tick, riot_*, capital_output_bonus, loyalty_defect, faction_task_slots}`、
`faction_task.*`、`mobilization.*`、`security.*`。

---

## 1. 文件所有权（硬约束）

| Agent | 拥有文件 | 说明 |
|---|---|---|
| A 规则层 | `core/strategy_rules.py`、`core/strategy_hex.py`、`config/strategy_events.json`、`tests/test_strategy_rules.py`、`tests/test_strategy_hex.py` | 纯规则，不碰 DB |
| B 主管理器 | `managers/strategy_manager.py`、`tests/test_strategy_manager.py` | tick 管线、任务、建筑、行军、NPC、动作分发 |
| C 势力 | `managers/strategy_faction_mixin.py`、`tests/test_strategy_faction_gaps.py`（新建） | 势力任务/动员令/迁都/科技/安全 |
| D P3·俘虏·计略 | `managers/strategy_p3_mixin.py`、`managers/strategy_captive_mixin.py`、`managers/strategy_plot_mixin.py`、`tests/test_strategy_p3_gaps.py`（新建） | 互市/匪寨/赛季/俘虏/计略 |
| E 前端指引 | `webchat_room/static/strategy/guide.js`、`guide.css`、`guide-data.js`、`index.html` | 全玩法文档 + 搜索 |
| F 服务端 | `webchat_room/room_server.py`、`tests/test_strategy_api.py` | WS 协议、REST 端点、静态路由 |
| G 前端视觉 | `webchat_room/static/strategy/strategy.js`、`battle.js`、`strategy.css` | 名将堂展示、观战/高光/分享、移动端手势、guide tab 注册 |

> `data/migration.py`、`config/strategy_config.json`、`README.md`、`metadata.yaml`、`handlers/misc_handler.py`、设计稿：由主 Agent 统一收尾，**任何人不要改**。

---

## 2. 跨文件接口（必须严格一致）

### 2.1 B 提供给 C/D/G 的公共方法

```python
async def _audit(self, user_id: str, action: str, detail: dict | None = None) -> None
    # 写 strategy_audit；保留 security.audit_keep_days 天；异常静默不影响主流程

async def _bump_morale(self, node: dict, delta: int, reason: str = "") -> int
    # 民心增减（0~100 clamp），返回新值；node 由调用方 save_node

def _is_joined_long_enough(self, member_or_player: dict, hours: int) -> bool
    # 基于 strategy_members.joined_at

async def _faction_buff(self, faction_id: int, effect: str) -> float
    # 读取生效中的动员令，返回乘区（无则 1.0；defense_bonus/attack_bonus 返回加成值 0.0~）
```

### 2.2 A 提供的纯规则函数（B/C/D 调用）

```python
# core/strategy_rules.py
def morale_task_gain(task_type: str, ticks: int, cfg: dict) -> int
def riot_check(rng, morale: int, cfg: dict) -> bool
def pursuit_capture(rng, cavalry: bool = False, swift: bool = False,
                    base: float = 0.25) -> bool          # 已存在，签名勿改
def trait_army_mods(traits: list[str]) -> dict            # {"grain_mult","speed_mult","defense_mult","attack_mult"}
def overstaff_penalty(leader, troops, cfg) -> int          # 士气惩罚值（0 表示不超编）
def bandit_raid_ready(node: dict, tick_no: int, interval: int) -> bool
def mini_event_roll(rng) -> dict | None                   # {"kind","text","mult"}
def arena_bracket(merit: int) -> str                      # "novice"/"veteran"/"master"
```

### 2.3 新增动作名（REST `POST /api/strategy/action {"action": ...}`）

| action | 参数 | 实现方 | 返回 |
|---|---|---|---|
| `govern_relief` | `node_id`, `resource`=`coin`/`grain` | B | `{ok,msg,morale}` |
| `upgrade_building` | `node_id`, `building_id` | B | `{ok,msg,level}` |
| `dispatch_task` | 追加 `target_node_id`, `partner_char_id` | B | 同现有 |
| `faction_task_refresh` | — | C | `{ok,tasks:[...]}` |
| `faction_task_claim` | `task_id` | C | `{ok,msg,reward}` |
| `mobilization_issue` | `kind` | C | `{ok,msg,ends_at}` |
| `capital_relocate` | `node_id` | C | `{ok,msg,cooldown_until}` |
| `rebuild_donate` | `amount` | C | `{ok,msg,fund}` |
| `tech_research` | 现有参数 + 可选 `char_id` | C | 同现有 |
| `transfer_captive` | `char_id`, `to_node_id` | D | `{ok,msg,arrive_at}` |
| `arena_register` | `bracket` | D | `{ok,msg}` |
| `market_list` | 现有 + `item` 允许 `intel` | D | 同现有 |
| `guide_search` | `q`（可选，纯前端不调用后端） | — | — |

> 所有失败一律 `raise StrategyError("中文原因")`，由 REST 层转 400。

### 2.4 `get_state` 新增字段（B/C/D 提供，G 消费）

```jsonc
{
  "faction_ext": {
    "tasks": [{"id","title","task_type","progress","target","status","reward"}],
    "mobilization": {"kind","name","ends_at"} | null,
    "rebuild_fund": 0,
    "capital": {"node_id","bonus_pct"},
    "relocate_cd_until": 0
  },
  "nodes": [ /* 已有；追加 */ {"morale","morale_cap":100,"relief_used":0,"respawn_at":0,"lair":true} ],
  "memorials": {"hall": [...], "shrine": [...], "season_no": 1},
  "transfers": [{"char_id","tiny","to_node","arrive_at"}]
}
```

---

## 3. 各 Agent 具体任务

### Agent A — 规则层（`core/*`）

1. **特技 8 条全部生效**（`trait_army_mods` + 各调用点数值）：`fire_master` 火攻伤害 +20%；`siege_expert` 守城战力 +15%；`doctor` 战后伤兵回收 8%、角色伤势额外 -1；`swift` 行军速度 +15%、粮耗 -10%、追击捕获 +10%；`ironwall` 守城城防 +20%。
2. **夜袭补全**（`strategy_hex.py`）：守方侦察精度减半（生成时守方视野/伏兵位减少）；佛修「晨钟」首回合士气 +`battle.night_buddha_morale`；魔修夜袭劫掠 +`battle.night_raid_loot_bonus`。
3. **兵种专属技能对齐设计稿 §5.4**：修正「咒唱」等偏差（按设计稿数值）。
4. **羁绊**：补齐 `yijiao`（义交）判定 `maybe_form_yijiao`、宿怨结怨 `form_suyuan` 触发条件、师徒 `maybe_form_shitu` 共享战斗计数口径；提供 `bond_apply_battle(a, b, won)` 一次完成「同道/义交/宿怨/师徒/知遇」演化，供 B 调用。
5. **势力事件**（`config/strategy_events.json`）：新增 `scope: "faction"` 的 6 个事件（角色出走/民心沸腾/哗变/细作/秘藏/名将迟暮），字段与现有事件一致，并补 `effects` 类型：`char_leave`/`morale`/`riot`/`spy`/`treasure`/`char_aging`。**不要**改已有事件。
6. **规则工具**：实现 §2.2 全部函数；`morale_task_gain`：`govern` 每 tick +`governance.govern_morale_per_tick`，`heal` 每 tick +`governance.heal_morale_per_tick`，其余 0。
7. 单位/竞技：`arena_bracket` 按战功分档（<500 novice / <2000 veteran / master）。
8. 为以上新增 15+ 条单测。

### Agent B — 主管理器（`managers/strategy_manager.py`）

1. **建筑体系补全到设计稿 §6.6 的 9 种**，等级上限 1~5：
   `manor`(城主府：产出 +6%/级)、`barracks`(兵营：带兵上限 +5%/级)、`market_stall`(坊市摊：交易税 -1%/级)、`tavern_branch`(酒馆分号：招募稀有 +3%/级)、`guard_array`(护山阵眼：护山盾强度 +10%/级)、`arena`(擂将台：擂台奖励 +10%/级、民心 +1/级) + 现有 `tower`/`infirmary`/`granary`。
   - `upgrade_node` 的 3 级上限改为读 `BUILDINGS[...]["max"]`（5）。
   - 所有效果必须**真实接线**：产出（`_tick_node_output`）、带兵上限（`troop_cap`）、交易税（`market_buy` 侧读取）、护山盾（`buy_shield`）、招募（`refresh_tavern`）、擂台（`duel_arena_*`）。
2. **主城链路修复**：
   - `_capture_node`：若失守节点是旧势力主城 → `capital_node_id=0` 且 `faction_state='exiled'`（无其他城时）。
   - 主城产出 +`governance.capital_output_bonus`。
   - `_auto_retreat`：撤退扣 `battle.rout_troops_pct` 兵、主将按 `battle.rout_leader_injury_chance` 受伤、`reorganize_until_tick = tick + battle.reorganize_ticks`；并用 `R.pursuit_capture` 实现飞剑队追击截获（被截获角色转 captive）。
3. **任务系统**：
   - 解禁 `raid`/`supply`：`raid` 目标为相邻敌方/无主据点，掠夺库存 `siege.loot_stock_pct`、民心 -8、声望 `task.raid_rep_cost`、`raid_counterkill_chance` 反杀；`supply` 为己方军团补粮（粮从玩家扣）。
   - `scout` 允许任意节点（`target_node_id`），且 `scout_capture_chance` 触发俘获流将。
   - `partner_char_id` 协作：两人同时占用、收益 ×1.15。
   - `mini_event_chance` 小事件接入 `_pay_task`（用 `R.mini_event_roll`）。
   - `govern`/`heal` 产出民心（`R.morale_task_gain`）。
4. **民心直接提升（用户重点）**：
   - 新增 `govern_relief`：花 `governance.relief_coin_cost` 铜钱（或 `relief_grain_cost` 粮）→ 民心 +`relief_morale`，每据点每日 `relief_node_daily_limit`，每人每日 `relief_daily_limit`（用 `strategy_players.daily_output_log_json` 记）。
   - `govern`/`heal` 任务持续加民心。
   - 民心 <`riot_morale` 每 tick `riot_chance` 哗变（`R.riot_check`）：驻军 -`riot_troops_pct`、民心 +5、随机角色受伤。
   - `get_state` 暴露 `morale/relief_used`。
5. **NPC**：驻军被清 → `respawn_at = now + rand(respawn_hours)`；tick 到时重生并按 `hatred_json` 提升 20%；仇恨记忆衰减；低兵时撤退保将。
6. **军团**：`create_army` 并发 = `concurrent_base + level // concurrent_per_faction_levels`（封顶 5）；`R.overstaff_penalty` 每 tick 扣士气。
7. **安全**：`handle_action` 入口限速（`security.action_rate_per_min`，内存滑动窗口）；关键动作 `_audit`；`result_json` 落库前 >`security.result_max_bytes` 截断；`security.result_keep_days` 归档清理。
8. **势力 buff 钩子**：`_faction_buff` 实现并接到行军粮耗/城防/攻击。
9. **秘境首占**：占领 `ruin` 首次 → 30% 掉装备/图纸（写入战报 + 通知）。⚠️ **v4.5.2 起整体封存**：`ruin_first_capture.enabled` 默认 false，遗迹改为常规可经营据点持续产出；恢复前须先补齐真正的装备图纸物品（原「图纸残卷」仅为精铁兜底文案）。
10. 更新 `tests/test_strategy_manager.py`，新增 ≥12 条用例；原有断言如需调整请说明理由。

### Agent C — 势力（`managers/strategy_faction_mixin.py`）

1. **势力任务（设计稿 §6.4）**：`faction_task_refresh`（每 `faction_task.refresh_hours` 生成 `governance.faction_task_slots` 条）、`faction_task_claim`；进度由 B 的 tick 事件回填（B 只负责调用 `self._faction_task_progress(...)`，请在本文件实现该方法并对 B 暴露）。
2. **动员令**：`mobilization_issue`，消耗 `mobilization.cost_merit`，冷却 `cooldown_hours`，写 `strategy_mobilizations`；`_faction_buff` 读取（若 B 已实现则改为委托）。
3. **势力事件（§10.3）**：tick 中按 `strategy_events.json` 的 `scope=faction` 事件随机触发并结算六种效果；民心 <20 的哗变由 B 处理，此处只处理「民心沸腾/细作/秘藏/名将迟暮/角色出走」。
4. **科技指派角色**：`tech_research` 支持 `char_id`，占用该角色 `status='research'` 至完成；未指派则自动挑智谋最高空闲角色；无可用角色报错。
5. **迁都**：`capital_relocate`（同势力城池/关隘，`faction.capital_relocate_cd_days` 冷却，扣势力主钱包）。
6. **重建合力**：`rebuild_donate` 向 `rebuild_fund` 捐资源；`rebuild_capital` 消耗 = `rebuild_fund` + 发起人灵石（v4.5.7 起玩家势力无国库）。
7. **安全**：入盟 72h 内不得担任官职、战功结算 ×0（`security.min_join_hours_*`）；`appoint_position`/`_settle_merit` 接入。
8. **治理**：低忠诚（<`governance.loyalty_defect`）角色出走（转 `wander`，通知）。
9. 新增 `tests/test_strategy_faction_gaps.py` ≥8 条。

### Agent D — P3 / 俘虏 / 计略

1. **互市**：`market_list` 支持 `intel`；`cross_faction_market_only=true` 时校验「非敌对才可跨势力成交」；挂单限 `city` 类节点。
2. **俘虏**：
   - 捕获时校验 `jail_slots_per_node`（满则改判「驱逐」）。
   - `persuade_captive` 支持「离心」：忠诚 ≤ `defect_loyalty` 后任意势力可 80% 劝降（消耗声望 `defect_rep_cost`）。**现口径**：仅「非关押方」的离心俘虏固定 80%；关押方自己仍按 `persuade_rate` 公式 + 军师祭酒加成结算；恶名（声望 ≤ -60）时不可劝降。详见 §5.4。
   - 濒死（`injury_state == dying`）不可劝降。
   - `transfer_captive`：写 `strategy_captive_transfers`，`rescue_ticks` 行程；行程中被原势力 `rescue` 可截获（沿用 `rescue` 动作）。
3. **匪寨周期劫掠**：`raid_interval_ticks` 驱动，每 N tick 掠夺相邻玩家据点库存/民心，并发通知。
4. **赛季/会武**：会武按 `arena_bracket` 分档报名（`arena_register`），擂台奖励接 `duel.drum_tower_mult`。
5. **商队 7 折**：我方商队经过的坊市 `market_buy` 价格 ×(1-`convoy.discount_pct`)。
6. **名将堂/英烈祠读取**：提供 `get_memorials(world_id)` 返回 `{"hall":[...],"shrine":[...]}`（B 的 `get_state` 会调用它）。
7. **计略**：`rumor` 对民心/产出的减益接入 B 的产出计算（提供 `plot_output_mult(node)`）；补齐「反间计」忠诚 -`alienate_loyalty` 的实际生效点。
8. 新增 `tests/test_strategy_p3_gaps.py` ≥8 条。

### Agent E — 前端「指引」文档 + 搜索（用户重点）

1. 新建 `guide-data.js`：结构化全玩法文档（**从设计稿提炼，中文，面向新手**），覆盖：入场/主城、据点与建筑、角色与招募、任务、军团与行军、围城、hex 战役、单挑、势力/联盟/官职、钱包/科技/外交/计略、俘虏、经济与商店、互市、匪寨/商路/运粮、赛季与成就、通知与设置。每条含 `id/title/category/keywords/body(HTML 字符串)/steps[]/tips[]`。**≥40 条**，每条正文 ≥80 字。
2. 新建 `guide.js`：暴露全局 `function tabGuide()` 供 `strategy.js` 的 `renderTab` 调用；包含：
   - 顶部搜索框（input，实时过滤，支持多关键词空格分隔、拼音首字母不做要求）；
   - 分类侧栏/标签（可折叠）；
   - 结果高亮 `<mark>`；
   - 空结果提示 + 热门词推荐（「民心」「围城」「俘虏」「互市」…）；
   - 不依赖登录态（未入场也能看）；
   - 支持 URL `?guide=围城` 直达；
   - 移动端单列自适应。
3. 新建 `guide.css`（暗色，与 `strategy.css` 变量一致）。
4. `index.html`：新增 tab 按钮 `data-tab="guide"`，并在 `strategy.js` 之前引入 `guide-data.js`、`guide.js`（`guide.css` 一并引入）。
5. 内容必须与**当前代码实现**一致（若某功能未实现，写「规划中」并标注）。绝不可编造数值——数值取 `config/strategy_config.json`。

### Agent F — 服务端（`webchat_room/room_server.py`）

1. **WS 协议补全**：在现有 `strategy.notify` 之外，新增
   - `strategy.delta`：tick 结束后向所有在线策略连接推送 `{type,tick,ts}`（前端收到即刷新）；
   - `strategy.battle.event`：手操回合推进推送 `{battle_id, round, log}`；
   - `strategy.deploy|move|skill|duel|retreat|end_turn`：允许客户端发送，转调 `manager.handle_action("battle_*")` 并回 `strategy.battle.ack`；失败回 `strategy.error`。
   保持向后兼容：REST 轮询仍可用。
2. **REST**：`GET /api/strategy/guide` 返回指引索引（读 `guide-data.js` 的 JSON 版或后端静态清单，二选一，简单优先：直接返回 `{"sections":[...]}` 由后端常量维护）；`GET /api/strategy/memorial` 返回 `get_memorials`。
3. **静态路由**：确保 `/static/strategy/guide.js|guide.css|guide-data.js` 可访问（现有静态目录应已覆盖，验证即可）。
4. 更新 `tests/test_strategy_api.py`：WS 消息与两个新端点各 2 条用例。

### Agent G — 前端视觉/交互（`strategy.js`/`battle.js`/`strategy.css`）

1. `renderTab` 注册 `guide: tabGuide`（**只加这一行映射 + 让 guide 在未入场时也渲染**）。
2. **名将堂/英烈祠**：势力页或成就页新增展示区，读 `S.memorials`。
3. **观战模式**：战报详情可进入只读 hex 视图（`battle.js` 增加 `spectator=true`：隐藏操作栏、显示「观战中」、轮询只拉状态）。
4. **高光标记**：战报中「单挑胜/合围/夜袭/以少胜多」打 `⭐ 高光` 标签并置顶。
5. **分享卡片**：`分享` 按钮生成一张 Canvas 卡片（战果/双方/损失/高光），支持「保存图片」与「复制文本」。
6. **移动端 hex 手势**：`battle.js` 支持 touch 双指缩放 + 单指拖拽。
7. 样式保持一致，不引入外部依赖。

---

## 4. 验收

| 项 | 标准 |
|---|---|
| 单测 | `python3 -m pytest tests/test_strategy_*.py -q` 全绿，且总数 ≥ 221 + 40 |
| 语法 | `python3 -m compileall managers core webchat_room` 无错 |
| 指引 | 未入场可打开「指引」，搜索「民心」能命中「赈济/布政」条目 |
| 前端 | 浏览器控制台无报错；`node --check` 通过（如有 node） |

---

## 5. v4.6.1 复核与缺口增量记录（v4.6.4 / DB v86）

> 本节由 2026-11-23 对 commit `b39a9801` 的 5 轮复核增量维护。**§0~§4 是历史契约，不是当前任务清单**；新增缺口只追加到本节，补全方案须经策划/用户确认后再实施。
>
> 工作区同时包含并行的 v4.6.2（遗迹可停留）与 v4.6.3（军团上限实时同步）改动；本节只记录 v4.6.1 审计相关结论，不回滚并行工作。

### 5.1 基线与验收

| 项 | 值 |
|---|---|
| 插件 / 游戏 / DB | v4.6.4 / v8.1.0 / v86（本轮无 schema 变更） |
| 策略回归 | `tests/test_strategy_*.py` **659 passed** |
| 前端回归 | `tests/webchat_mobile_ui.test.cjs` **22 passed**；`node --check` 通过 |
| 长局压测 | 600 tick 自然局 0 不变量错误；`world.data` 约 40 KB；单 tick 约 28~31 ms（7~8 势力 / 36 节点） |
| 终审结论 | 未发现阻断正常游玩的 P1；剩余为 1 项 P2 平衡口径与若干 P3 深化项 |

### 5.2 本轮已修复（审计发现，已落地）

| 编号 | 问题 | 修复要点 | 回归 |
|---|---|---|---|
| FIX-01 | 开局第 0 天每日宣战上限被 `or -1` 重置，首日可超过 4 场 | `declarations.day` 按合法 0 判断 | `test_declaration_day_zero_keeps_daily_counter` |
| FIX-02 | 种子战争占满单 NPC 战争槽，自主宣战永远无槽可宣 | 种子战争最多占 `max_active_wars_per_npc-1`；战术突袭最多再占 1 个槽（`< cap-1` 才允许），避免一次突袭占满全部槽位 | `test_seed_wars_leave_slot_for_autonomous_declaration`、`test_tactical_attack_reserves_slot_for_declaration` |
| FIX-03 | 灭亡势力 pair/accum/stats 残留、幽灵战争、狂热不结束 | 按存活外交主体裁剪 + 灭亡当 tick 立即清理 | `test_dead_faction_pairs_cleaned_and_hidden` |
| FIX-04 | 停战/冷却/备战/撕约状态机错位；备战期被当成正式战争 | `truce/cooldown` 遇 `REL_WAR` 立即回战争；`war_pending` 不建 war pair；新战争重置旧战果/求和/狂热 | 多项 threat 回归 |
| FIX-05 | 玩家联盟被宣战后盟主 `war_at=0`、成员仍 24h 备战；战果按盟员而非盟主记账 | 宣战后再次 `_sync_alliance_meta`；易主战果按联盟盟主解析；通知全体成员 | `test_alliance_declaration_resets_member_war_at` 等 |
| FIX-06 | 龟缩统计把被攻占方记成主动攻击者；占无主不记扩张 | 只给攻占方记 `captures_7d/attacks_7d`；无主易主不写风声 | `test_capture_marks_attacker_not_victim` |
| FIX-07 | 新手保护误用节点保护期；节点保护被当成新玩家保护 | 改为 `strategy_players.created_at + newcomer.opt_out`，联盟按全体判断；节点保护仍由战术层拦截 | `test_can_declare_uses_player_join_time_not_node_shield` |
| FIX-08 | `war_score=0` / 平均民心 0 被 falsy 兜底成 50 / 100 | 统一 `_num/_safe_float`，仅 None 回退 | `test_zero_morale_and_zero_war_score_not_coerced` |
| FIX-09 | 旧档损坏 `declarations/pair/accum/winds/CD/日限桶/赎金`、超大整数导致 tick 永久回滚 | 初始化清洗 + GC 自愈 + 捕获 `OverflowError` | `test_corrupt_npc_ai_fields_self_heal_and_tick_advances` 等 |
| FIX-10 | 闲置 NPC 军团无限堆积（1500 tick 68 支 / 3 万兵），单 tick 上百 ms | 己方 idle NPC 军团并入驻军解散；灭亡势力军团清退 | `test_idle_low_troop_npc_army_merges_into_home_garrison` |
| FIX-11 | 灭亡势力围城军清退后 `siege_state` 悬挂，阻塞城防自修/建造/传送 | 删军后清理孤儿围城状态 | `test_dead_npc_siege_army_clears_orphan_siege_state` |
| FIX-12 | 处决/售卖时 `add_reputation` 用旧 `f` 覆盖刚写入的 `diplomacy_json`，外交惩罚单边丢失 | 先声望、后外交 | 处决/售卖双方外交断言 |
| FIX-13 | 和谈赔款上限按操作者钱包、接受后可能只支付一部分 | 上限与付款统一按势力主钱包；余额不足整笔失败 | `test_peace_indemnity_cap_uses_leader_wallet` 等 |
| FIX-14 | 重建捐款/迁都的灵石在动作事务中提前 commit，失败无法回滚 | `update_player(commit=not _in_tick_task())` | `test_rebuild_donate_main_gold_rolls_back_on_failure` |
| FIX-15 | 科技可指派濒死角色；转投角色旧研究完成会清掉新研究状态 | 显式/自动均排除 `dying/dead`；仅同势力释放 research | `test_research_rejects_dying_char` 等 |
| FIX-16 | v85 把押运/在野/NPC/死亡角色忠诚也置满 | 仅玩家势力非 `captive/transfer/dead/wander` 且 `captor_faction<=0` | `test_char_loyalty_full_backfill_v85` |
| FIX-17 | 未入场时「天下」「指引」被入场遮罩挡住；NPC 宣战/求和通知显示原始 kind | 新增「先看看天下/指引」与顶栏「入场」；补中文通知；聊天室横幅同步 | mobile test 断言 |
| FIX-18 | 天下页遇旧字符串风声/缺失字段崩溃；死亡势力挤占关系前 6 | `asList/num` 归一化；先过滤存活再排序 | `test_world_view_relations_prioritize_live_targets` 等 |
| FIX-19 | 俘虏详情劝降率在 `defect_loyalty>0` 时预览 80%、实际按公式 | 关押方按公式 + 祭酒；仅非关押方离心固定 80% | `test_captive_preview_rate_matches_defect_formula` |
| FIX-20 | 俘虏售卖/定价按钮未按 `sell_enable/ransom_enable` 隐藏 | 前端读取 `meta.captive_cfg` | mobile test 断言 |

### 5.3 待确认缺口（未实施，需策划/用户拍板）

| 编号 | 优先级 | 缺口 / 现状 | 影响 | 建议方案（待确认） |
|---|---|---|---|---|
| GAP-01 | **P2** | 正式宣战线 `declare_gate=60` 在无霸权自然长局中较难达到：600 tick 三次实跑仅 1 次宣战；2400 tick 0 次，max H≈47、最高占比 0.324（< 0.40 霸权线） | 「天下」页长期只显示警觉/戒备，自主宣战存在感弱 | A) 下调 gate 至 50~55；B) 提高非霸权压力/alpha；C) 接受“只有霸权出现才正式宣战”的稀有设定。需先定玩法目标 |
| GAP-02 | **P2** | 战争狂热守家比例：设计稿 §7.7 表为 0.30→0.15，§16.4 为“威胁 +0.25 再狂热 -0.15”，代码按 §16.4 得 0.40 | 狂热单路出兵比例约 69%，略低于平时 70%；总量靠 2→4 路提升，与“守家下调”文案不符 | A) 采用 §7.7：威胁 +0.25 只用于非战争档；B) 保留现状并修正文档/文案。需平衡确认 |
| GAP-03 | **P2** | `max_war_ticks=672` 只“强制进入求和流程”，仍按 `p_day` 掷点；实测 3/8 战争超过 15 游戏日 | 长期纸面战争占用战争槽，影响自主宣战与玩法节奏 | forced 时提高 `p_day` 下限或直接白和；需确认“最长战争时长” |
| GAP-04 | **P2** | 玩家联盟未按“联盟块”聚合威胁/体量/接壤/军力/疲劳；`diplo_factions` 只统一外交主体 | 多人联盟可能很难被正式宣战，战争疲劳只反映盟主自身损失 | P4：按联盟块聚合 share/border/force/node_count/avg_morale/troops；需评估性能 |
| GAP-05 | **P2** | 100+ 势力时 `npc_ai.pairs/accum` 与 `threat.inst/split/tier` 为 O(F²)，99 势力 `world.data` 5.19 MB、威胁三步骤约 400ms | 大服/多玩家扩展性差 | 裁剪无消费方的 `inst/split/tier`、零值 pair/accum；按需懒建；需压测验收 |
| GAP-06 | P3 | `active_wars` 只计 `phase=war`，不计 `war_pending`；守方被多方宣战/种子战争/突袭可短期超过“单 NPC 同时 2 场” | 备战到期后可能短时 3 场 | 明确“上限只约束主动宣战”或把 `war_pending`/受战也计入 |
| GAP-07 | P3 | 闲置 NPC 军团并入驻军后 `npc_garrison` 可超 `garrison_cap` | 无机制惩罚（防御按实际兵力），但语义越界 | 归队时按上限截断或折算；需定“残军损耗”口径 |
| GAP-08 | P3 | `max_active_wars_per_npc<=1` 时战术突袭槽为 0，NPC 只能靠正式宣战 | 极小众配置下攻击性骤降 | 若允许该配置，单独设计保底槽；不建议上限设 1 |
| GAP-09 | P3 | 灭亡 NPC 势力的 idle 角色仍留在 `strategy_characters`（`faction_id` 指向死亡势力、落点空） | 仅占库，不参与玩法 | 批量转 `wander` 或按赛季清理 |
| GAP-10 | P3 | 非关押方 + 离心 + 恶名（声望 ≤ -60）的俘虏详情仍预览 80%，实际 `persuade_captive` 拒绝 | 预览与实际不一致 | 预览补恶名判定 |
| GAP-11 | P3 | `npc.threat.enabled=false` 时 `data.threat` 不生成，天下页 share/威胁全 0（仍有势力名与风声） | 关闭威胁层后页面像“坏数据” | 回退按据点占比计算，或页面显示“威胁层未启用” |
| GAP-12 | P3 | 旧存档可能已有超过新口径的种子战争/已被旧 v85 置满的 NPC 忠诚；v85 不会重跑 | 旧档自主宣战仍可能被占槽；NPC 忠诚无法无损回滚 | 可做一次性运行时归一（带版本标记）；v85 历史数据在文档说明 |
| GAP-13 | P3 | 战术层仍可“不宣而战”，只是最多占 1 槽；正式宣战与突袭两套入口并存 | 玩家可能先被突袭、后收到宣战；语义略有重叠 | 若要“先宣战后开战”，收紧战术层只打 `REL_WAR`；需评估对现有挑战性的影响 |
| GAP-14 | P3 | 威胁深化未接线：`favor_eff = favor - 0.4×H`、龟缩“与霸主结盟/魔道绝望”边界、`target_at_war`/`revenge` 优先级、`eval_interval_ticks` 降频、边境 B 按“每边”还是“每城”、防守方是否也进入狂热 | 威胁精度/性能/语义与设计稿有出入 | 分批接入；先做 `eval_interval_ticks` 与 `target_at_war`，复仇留 P4 |
| GAP-15 | P3 | 无国库残留：赎金设价人离队后仍收款；势力事件秘藏/劫粮在无势力主时写 `treasury`；NPC 作为付款方可能部分赔款 | 极端分支经济归属不清 | 明确“设价人离队→回退势力主/失效”；无主时跳过并告警；NPC 赔款足额或失败 |
| GAP-16 | P3 | `loyalty.natural_min_loyalty` 与 `governance.loyalty_defect` 独立配置，可能出现 20~29 忠诚“不恢复也不出走” | 配置错位时角色卡死 | 恢复下限取 `min(natural_min_loyalty, loyalty_defect)` 或配置校验 |
| GAP-17 | P3 | 旧科技线不存在时 pending 不清理，角色可能永久 `research`；`reward_all_chars` 未再校验 `faction_id` | 旧档/异常数据卡角色或跨势力奖赏 | 清理无效 pending 并释放角色；奖赏补 faction 过滤 |
| GAP-18 | P3 | 指引/帮助缺口：`guide-data.js` 无「天下/威胁/风声」独立条目（搜索“威胁”“天下”0 命中），`/修仙帮助` 原未提天下/忠诚（本轮已补 2 行），前后端 guide 索引标题/关键词仍有漂移 | 新玩法可发现性不足 | 新增 1 条「天下大势与 NPC 威胁」并同步后端索引；统一索引口径 |
| GAP-19 | P3 | 隐私/展示口径：`get_captives_view.home_owner` 下发原始账号 key；天下「军团」列未入场/迷雾外恒 0；`relations` 公开精确 H；`get_state.winds` 与 `npc_world.winds` 双份 | 隐私/迷雾/载荷 | 原主改显示势力名/昵称或仅原势力可见；军团列标注“可见”或改全局；关系是否模糊化待定；winds 可只保留一份 |
| GAP-20 | P3 | 科技研究前端无 `char_id` 选择器（后端自动挑智谋/政务最高）；押运 `arrived/released/void` 行长期保留；指令数 README 279 vs metadata 278 | 与指引“可指派角色”略有出入；表缓慢增长；文档口径不一 | 前端加角色选择；押运记录按赛季/时间清理；统一指令数口径 |

### 5.4 历史契约口径更正

| 位置 | 旧口径 | 现口径 |
|---|---|---|
| §3 Agent D 俘虏 | “任意势力可 80% 劝降” | 仅“非关押方”的离心俘虏固定 80%；关押方按 `persuade_rate` 公式 + 军师祭酒加成；恶名（声望 ≤ -60）拒绝 |
| §3 Agent E 指引覆盖 | “国库/科技/外交/计略” | v4.5.7 起玩家势力无国库，改为“钱包/科技/外交/计略”；俸禄、研发、迁都、赔款均走势力主钱包 |
| §3 Agent C 迁都/重建 | “扣国库 / 国库 + rebuild_fund” | 迁都扣势力主钱包；重建 = `rebuild_fund` + 发起人灵石（v4.5.7 起） |
| §0 基线 | v4.1.0 / DB v77 / 221 tests | v4.6.4 / DB v86 / 659 strategy tests（§0~§4 仅作历史参考） |
| v85 迁移 | “全部存量武将忠诚置满（俘虏除外）” | 仅玩家势力非 `captive/transfer/dead/wander` 且 `captor_faction<=0`；NPC/在野/押运保持原值 |
| v86 迁移 | “进行中任务更名” | 实际同步 `task_type='donate'` 的所有状态行（含历史 `claimed`） |

### 5.5 建议的补全顺序（均需确认后实施）

| 阶段 | 内容 | 目的 |
|---|---|---|
| 第 1 批（低风险） | GAP-10、GAP-11、GAP-16、GAP-17、GAP-19（隐私/双份载荷） | 修正预览/关闭态/配置联动，不改玩法节奏 |
| 第 2 批（平衡拍板） | GAP-01、GAP-02、GAP-03、GAP-06、GAP-13 | 决定“宣战是否常见、狂热是否更凶、战争是否必须结束、突袭是否保留” |
| 第 3 批（规模化） | GAP-04、GAP-05、GAP-07、GAP-09、GAP-12 | 联盟块聚合、100+ 势力性能、旧档归一 |
| 第 4 批（内容补全） | GAP-14、GAP-15、GAP-18、GAP-20 | 威胁深化、经济边界、指引/隐私/清理 |

> 以上均为“发现记录”，不代表已排期。请先对 GAP-01 / GAP-02 / GAP-03 / GAP-13 给出玩法口径，再确定补全方案。
