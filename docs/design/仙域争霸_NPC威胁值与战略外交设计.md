# 仙域争霸 · NPC 威胁值与战略外交设计

> 状态：**已实施（插件 v4.6.1 / 游戏 v8.1.0 / 主库 DB v86）**。
> 基线：插件 v4.5.7 / 游戏 v8.0.3 / 主库 DB v84（设计起点）。
> 关联文档：`docs/design/仙域争霸战略玩法设计方案.md`（§9 NPC 势力与 AI、§9.4 合纵制衡）。
> 本文记录「威胁值 → 战略敌意 → 自主开战 / 停战」的完整算法设计，作为后续实现的唯一口径。
> 实施清单（新增代码 / 对接现有代码）见 §19；风声机制见 §20。

---

## 0. 拍板结论（先看这里）

| # | 结论 | 落地含义 |
|---|---|---|
| 1 | **不接壤时，累计威胁值受减益** | 无接壤时 `adj_factor` 降低、累计速度变慢、自然衰减更快；威胁不会瞬间归零，但很难单独靠“体量大”触发战争 |
| 2 | **寡头霸主例外，非接壤也要能开战** | 目标霸权占比 ≥ 40%（或已触发合纵）时，去掉大部分非接壤减益，并给予“公敌地板”；允许跨区远征宣战 |
| 3 | **允许玩家龟缩玩法** | 不扩张、不主动攻击、边境防御高的玩家获得 `turtle` 减益；NPC 可以袭扰，但正式宣战概率显著降低 |
| 4 | **允许 NPC 自杀宣战** | 不设硬性兵力门槛；兵力劣势只降低宣战概率，不禁止。代价由 NPC 自己承担 |
| 5 | **宣战后出兵倾向大幅提高** | 新增 `war_frenzy` 状态：出兵路数、出兵比例、目标权重、侵略倍率显著上升，但仍受国库 / 民心 / 守家底线约束 |

### 0.1 实现落点（先看：新增 vs 对接）

| 类别 | 文件 | 说明 |
|---|---|---|
| **新增代码** | `managers/strategy_threat_mixin.py` | `StrategyThreatMixin`：威胁快照 / 累计敌意 / 势力对状态机 / 自主宣战 / 停战 / 狂热 / 风声 / 天下视图 |
| **新增规则** | `core/strategy_rules.py`（文件末尾） | 威胁相关纯函数（`threat_*` / `per_day_to_per_tick`） |
| **新增配置** | `config/strategy_config.json` | `npc.threat` / `npc.war` / `npc.peace` / `npc.wind` 四段 |
| **新增前端** | `webchat_room/static/strategy/index.html`、`strategy.js` | 「天下」标签页与 `tabWorld()` |
| **新增测试** | `tests/test_strategy_threat.py` | 规则纯函数 + 管理器集成用例 |
| **对接现有** | `managers/strategy_manager.py` | mixin 组合、tick 管线、`_step_npc_ai`、`_spawn_npc_army`、`_capture_node`、`_gc_world_data`、`get_state` |
| **对接现有** | `managers/strategy_faction_mixin.py` | 停战接受率、外交提案 / 应答同步、拒绝求和清空停火 |
| **不改表结构** | `strategy_worlds.data_json` | 全部状态落 `world.data.npc_ai / threat / winds`，数据库仍为 **v86** |

> 详细逐项清单见 §19；每处改造的“为什么改、改了什么”见对应章节。

---

## 1. 现状核对

### 1.1 当前 NPC 自主程度

**结论：有“自动经营”和“局部侵略”，但没有“战略层威胁感知与自主外交”。**

| 维度 | 当前实现 | 关键位置 | 评价 |
|---|---|---|---|
| 城池发展 | 自动启动升级，受稳定期、国库底线、建造间隔、同时在建筑上限约束 | `managers/strategy_manager.py:7627` `_step_npc_develop` | ✅ 自主 |
| 经济经营 | 自动赈济维稳；坊市做市、收购按国库缺口执行 | `managers/strategy_manager.py:7758` `_step_npc_economy`；`managers/strategy_p3_mixin.py:587`、`:675` | ✅ 自主 |
| 军事动员 | 按国库、民心、守家比例自动出兵，每势力每 tick 最多 2 路 | `managers/strategy_manager.py:7481` `_step_npc_ai`；`:8065` `_spawn_npc_army` | ✅ 自主 |
| 战术目标 | 对**相邻**节点跑 `npc_utility`，按性格侵略性、好感、赛季 / 合纵倍率选目标 | `core/strategy_rules.py:1563`；`managers/strategy_manager.py:7569` | ✅ 局部自主 |
| 战略外交 | 只在开局按性格预置关系；之后不会自主宣战、结盟、停战 | `managers/strategy_faction_mixin.py:1429` `seed_npc_diplomacy` | ❌ 缺失 |
| 跨区协同 | 只有一个全局合纵标量：霸权 > 40% 时所有 NPC 对其侵略 ×1.5 | `managers/strategy_p3_mixin.py:1870` `npc_aggression_mult` | ⚠️ 粗糙 |

### 1.2 四个关键缺口

| 缺口 | 说明 | 影响 |
|---|---|---|
| 局部威胁参数未生效 | `npc_utility` 有 `threat_nearby_power` 参数，但调用处没有传值，恒为 0 | AI 看不到“门口堆了多少兵” |
| 核心城判定写反 | `adjacent_to_enemy_core=(target["id"] == f.capital_node_id)` 比较的是目标节点与**自己**首都 | 该战略加分几乎恒为 False |
| 仇恨记忆只写不读 | 攻城时 `node.hatred += 20`，每 tick 衰减 1，但 AI 选目标时没有使用 | 灭国仇恨不会转化为战略行为 |
| 合纵一刀切 | 只认全图第一势力，没有“谁威胁我、威胁多大、打不打得起”的分层判断 | 无法形成远近亲疏、均势外交 |

> 本次设计不推翻现有 `_step_npc_ai`，而是在其前面增加“战略威胁层”，并把威胁作为战术评分的倍率输入。

---

## 2. 设计目标与非目标

### 2.1 目标

| 目标 | 说明 |
|---|---|
| 世界更“活” | NPC 之间、NPC 对玩家会根据体量、接壤、扩张、军力自主形成敌友判断 |
| 反霸权 | 大势力会自然成为众矢之的，但反制是渐进的，不是瞬间围殴 |
| 保留龟缩 | 小势力、防守型玩家可以靠不扩张、筑防、外交活下去 |
| 可解释 | 玩家能看到“为什么被打”：体量、接壤、扩张、军力四项拆解 |
| 可调参 | 所有阈值、权重、概率都在 `config/strategy_config.json`，联调期可热调 |
| 无库表迁移 | 全部为派生数据 + `world.data` 缓存，旧存档可直接运行 |

### 2.2 非目标

| 不做 | 原因 |
|---|---|
| 复杂附庸 / 傀儡 / 联姻 | 初版只做威胁、宣战、停战、合纵 |
| 精确经济战 / 封锁 | 现有商路、坊市已足够，不在本期 |
| 玩家可手动编辑 NPC 外交 | 保持 NPC 自主性，只保留赠礼 / 通商 / 外交提案影响 |
| 以威胁值直接决定胜负 | 威胁只影响意愿，战斗仍走现有会战 / 围城 / 补给 |
| 每次 tick 全量重算所有历史 | 用累计状态 + 轻量快照，避免性能与存档膨胀 |

---

## 3. 术语与核心模型

### 3.1 术语

| 术语 | 定义 |
|---|---|
| 观察者 `O` | 正在评估威胁的势力（NPC 或玩家势力） |
| 目标 `T` | 被评估的势力（NPC 或玩家势力） |
| 即时压力 `P[O][T]` | 当前时刻由体量、接壤、扩张、军力算出的 0~100 压力 |
| 累计敌意 `H[O][T]` | 带记忆的 0~100 累计威胁，决定外交档位与宣战意愿 |
| 接壤边 | 一条边两端分别属于 `O` 与 `T` |
| 接壤压力 `B[O][T]` | 目标势力与观察者接壤的据点权重和 |
| 边境暴露度 `R[O][T]` | 观察者边境节点中，面向目标势力的比例 |
| 霸权占比 `S[T]` | 目标势力占全图已占节点权重的比例 |
| 寡头霸主 | `S[T] ≥ hegemon_share_threshold` 或合纵激活时的第一势力 |
| 战争狂热 `war_frenzy` | 宣战后短时间内的全军出击状态 |
| 战争疲劳 `E` | 0~100，决定 NPC 何时想停战 |
| `tick` | 世界推进的最小时间单位，默认 `tick_minutes = 15`，即 1 tick = 15 分钟 |
| `ticks_per_hour` / `ticks_per_day` | `3600 // tick_seconds` = 4；`86400 // tick_seconds` = 96 |
| `*_tick` 字段 | 持久状态中的时间一律用**绝对 tick 序号**，不使用真实时间戳，保证确定性与可重放 |

### 3.2 两层模型

```text
第一层：即时压力 P
  体量 + 接壤 + 扩张 + 军力
  ↓ 性格 / 关系 / 好感 / 声望修正
  ↓ 非接壤减益
  ↓ 霸主公敌地板

第二层：累计敌意 H
  H(t) = H(t-1) + (P_eff - H(t-1)) × alpha
  ↓
  分档：无感 / 警觉 / 戒备 / 敌视 / 公敌
  ↓
  战略行为：警告 / 增兵 / 宣战 / 合纵 / 求和
```

- `P` 是“现在有多危险”。
- `H` 是“我心里积累了多少敌意”。
- 两者分离的好处：边境摩擦可以慢慢升级，也可以慢慢降温；不会因为一次边界变化瞬间开战或瞬间和解。

### 3.3 战略敌意 vs 战术可行性

这是本设计最重要的分层：

| 层 | 决定什么 | 输入 | 输出 |
|---|---|---|---|
| 战略层 | 想不想打、愿不愿意承担战争 | `H`、性格、关系、好感、霸权 | 档位、宣战概率、停战意愿 |
| 战术层 | 打不打得动、打哪里划算 | `npc_utility`、距离、守军、城防、补给 | 是否出兵、目标节点 |

**威胁高不等于无脑出兵。** 一个 90 威胁但国力只有对方 20% 的 NPC，可以“宣战”（自杀宣战），但出兵仍会因为 `npc_utility` 低而碰壁；这正是用户允许的“自杀宣战”语义。

---

## 4. 数据模型

### 4.1 派生快照（每 tick 可重算）

```json
{
  "tick": 1234,
  "weight": {"3": 12.0, "7": 19.6},
  "share": {"3": 0.24, "7": 0.40},
  "border": {"3": {"7": 9.0}, "7": {"3": 9.0}},
  "border_nodes": {"3": [12, 18], "7": [23, 29]},
  "exposed": {"3": {"7": 2}, "7": {"3": 3}},
  "near_troops": {"3": {"7": 4200}, "7": {"3": 3800}},
  "growth": {"3": 0.20, "7": 0.00},
  "force": {"3": {"7": 0.62}, "7": {"3": 0.55}},
  "inst": {"3": {"7": 61.2}, "7": {"3": 58.4}},
  "accum": {"3": {"7": 55.1}, "7": {"3": 47.8}},
  "tier": {"3": {"7": "prepare"}, "7": {"3": "prepare"}},
  "public": {"3": 12.0, "7": 64.0}
}
```

`inst` / `accum` / `tier` 是核心；其余是计算中间量与 UI 拆解依据。

> 天下威胁榜展示用 `public[T] = max_O H[O][T]`（“最敌视 T 的观察者”），避免平均后大国威胁被稀释。
>
> 内存中所有势力键统一用 `int`，仅在写入 `world.data` / JSON 时转 `str`；读取时用 `to_int_key()` 归一。

### 4.2 持久状态（写入 `world.data`）

```json
{
  "npc_ai": {
    "tick": 1234,
    "accum": {"3": {"7": 55.1}},
    "pairs": {
      "3": {
        "7": {
          "phase": "hostile",
          "hostile_since_tick": 1180,
          "war_start_tick": 0,
          "frenzy_until_tick": 0,
          "no_declare_until_tick": 0,
          "truce_until_tick": 0,
          "ceasefire_until_tick": 0,
          "goal": "",
          "war_score": 50,
          "exhaustion": 0,
          "last_offer_tick": 0,
          "last_declare_tick": 0,
          "last_capture_tick": 0,
          "captures_by_me": 0,
          "captures_by_target": 0,
          "war_start_troops": 0,
          "war_start_nodes": 0
        }
      }
    },
    "declarations": {"day": 12, "count": 2},
    "stats": {
      "3": {"captures_7d": [1100, 1150], "attacks_7d": [1160]}
    }
  },
  "threat": {
    "tick": 1234,
    "inst": {"3": {"7": 61.2}},
    "accum": {"3": {"7": 55.1}},
    "public": {"7": 64.0}
  }
}
```

- `pairs` 只保留 `alive` 势力之间的条目，势力灭亡后清理。
- `pairs` 是**有向**的：`A→B` 与 `B→A` 各存一份，`war_score` / `exhaustion` 是各自视角；宣战、停战、狂热同步写双方，保证 `target_E` / `target_war_score` 可读。
- `stats` 用于龟缩判定与复仇判定，保留最近 7 天（672 tick）。
- `declarations` 用于全服宣战频率上限；`day = tick // ticks_per_day`，不依赖真实日期。

### 4.3 外交状态机

| 状态 | 类型 | 进入条件 | 主要行为 | 退出条件 |
|---|---|---|---|---|
| `calm` | 持久 `phase` | `H < 20` | 正常经营、扩张 | `H ≥ 20` |
| `alert` | 持久 `phase` | `20 ≤ H < 40` | 侦察、边境轻微增兵 | `H < 15` 或 `H ≥ 40` |
| `prepare` | 持久 `phase` | `40 ≤ H < 60` | 提高守家比例、暂停贸易 / 结盟、寻找盟友 | `H < 35` 或 `H ≥ 60` |
| `hostile` | 持久 `phase` | `H ≥ 60` | 发出“战争阴云”警告、集结、可宣战；`H ≥ 80` 时 UI 标注“公敌” | `H < 55` 或进入战争 |
| `war` | 持久 `phase` | 正式宣战 | 正常战争 | 停战 / 灭亡 |
| `truce` | 持久 `phase` | 停战协议 | 停止攻击 | 停战到期 |
| `cooldown` | 持久 `phase` | 停战到期后 | 禁止再次宣战 | `no_declare_until_tick` 到期 |
| `frenzy` | 派生状态 | `tick < frenzy_until_tick` 且 `phase == war` | 全军出击、出兵路数与比例提升 | 时间结束 / 国库或民心崩溃 |
| `weary` | 派生状态 | `E ≥ peace_gate` 且 `phase == war` | 降低进攻频率、寻求停战 | 停战 / 疲劳回落 |
| `ceasefire` | 派生状态 | `phase == war` 且 `tick < ceasefire_until_tick` | 暂停进攻、等待答复 | 接受 / 拒绝 / 到期 |

> 状态机按“势力对”维护，不是按势力维护。一个 NPC 可以同时对 A 处于 `war`、对 B 处于 `hostile`。

**迁移实现要点**（`_update_pair_phases`）：

| 迁移 | 触发 | 必做写入 |
|---|---|---|
| 任意档位进入 `hostile` | `H` 由 `<60` 变为 `≥60` | `hostile_since_tick = tick`；清空 `last_offer_tick` |
| `hostile` 档位回落 | `H` 跌破退出线（带 5 点滞回） | `hostile_since_tick = 0`，取消已排队宣战 |
| `→war` | `_declare_npc_war` | `phase=war`、`war_start_tick`、`last_declare_tick`、`frenzy_until_tick` |
| `war→truce` | 停战生效 | `phase=truce`、`truce_until_tick`、`no_declare_until_tick` |
| `truce→cooldown` | `tick ≥ truce_until_tick` | `phase=cooldown`，直到 `no_declare_until_tick` |
| `cooldown→calm/alert/prepare/hostile` | 冷却结束 | 按当前 `H` 重算档位 |
| `war→war`（狂热 / 疲惫 / 停火） | 派生条件成立 | **不写 `phase`**，只更新 `frenzy_until_tick` / `exhaustion` / `ceasefire_until_tick` |

> **滞回设计**：进入线与退出线相差 5 点（如 `hostile` 进 60 / 出 55），避免 `H` 在阈值附近抖动导致状态和警告反复触发。
>
> **档位不覆盖战争状态**：`_update_pair_phases` 只在 `phase ∈ {calm, alert, prepare, hostile}` 时按 `H` 改写；`war / truce / cooldown` 期间只更新 `H`，不改 `phase`。

**狂热与疲惫不作为持久 `phase`**：`frenzy` / `weary` / `ceasefire` 都由时间字段或 `E` 派生，`phase` 只保留 `calm/alert/prepare/hostile/war/truce/cooldown` 七个真实状态，减少状态爆炸与旧存档兼容问题。

---

## 5. 威胁值算法

### 5.1 输入

| 输入 | 来源 | 用途 |
|---|---|---|
| 据点类型 / 等级 | `strategy_nodes` | `V(n)` 权重 |
| 据点归属 | `strategy_nodes.owner_faction` | 体量、接壤 |
| 邻接表 | `strategy_nodes.adj` | 接壤边、边境节点 |
| 守军 / 城防 | `npc_garrison`、`defense` | 军力压力 |
| 野战军团 | `strategy_armies` | 军力压力 |
| 性格 | `faction.personality` | 敏感度、战争倾向、和平倾向 |
| 外交关系 | `faction.diplomacy` | 同盟 / 停战 / 战争衰减 |
| 好感 | `diplomacy[target].score` | 好感衰减 |
| 声望 | `faction.reputation` | 恶名 / 侠名修正 |
| 霸权 / 合纵 | `world.data.coalition` | 霸主例外 |
| 近期扩张 / 攻击 | `world.data.npc_ai.stats` | 扩张项、龟缩判定、复仇判定 |

### 5.2 据点权重 `V(n)`

```text
V(n) = node_score[n.type] × (1 + level_bonus × (n.level - 1))
```

- `node_score` 复用 `config/strategy_config.json` 的 `season.node_score`：仙城 3、关隘 2、其他 1。
- `level_bonus` 默认 0.10，即 5 级城权重为 `3 × 1.4 = 4.2`。
- 不建议另造一套“城权重”，避免与赛季霸权口径不一致。

### 5.3 体量 `S_T`

```text
total_weight = Σ V(n)  for all owned nodes
S_T = Σ V(n) for n.owner == T / max(1, total_weight)
```

- 用“占全图已占权重的比例”，而不是裸数量。
- 这样 20 个矿点不会比 5 座仙城更吓人；同时天然与合纵口径一致。

### 5.4 接壤压力 `B_OT` 与边境暴露度 `R_OT`

```text
B_OT = Σ V(u)
       for each edge (u, v)
       where u.owner == T, v.owner == O, v ∈ u.adj

Bn_OT = min(1, B_OT / border_norm)

border_nodes(O) = { v | v.owner == O 且 ∃u∈v.adj, u.owner != O }
exposed_nodes(O,T) = { v ∈ border_nodes(O) | ∃u∈v.adj, u.owner == T }
R_OT = |exposed_nodes(O,T)| / max(1, |border_nodes(O)|)

border_OT = 0.6 × Bn_OT + 0.4 × R_OT
```

- `B_OT` 直接对应“目标势力有多少城压在我边境上”。
- `R_OT` 补足“我这个势力有多少边境线正对着它”。
- 两者加权后，能让“小势力被大国贴脸”的体感明显，但不会让远方的巨无霸凭空变成边境威胁。

### 5.5 扩张速度 `G_T`

```text
ΔS_T = S_T(tick) - S_T(tick - growth_window_ticks)
G_T = clamp(ΔS_T / growth_norm, 0, 1)
```

- 默认 `growth_window_ticks = 96`（1 天），`growth_norm = 0.10`。
- 一天吃掉全图 10% 权重 → `G = 1`。
- 新世界开局前 1 天没有历史快照，`G = 0`，避免开局误判。
- 轻量实现：`world.data.npc_ai.stats[fid].share_history` 每天存一个点，保留 7 个点即可。

### 5.6 边境军力 `F_OT`

```text
near_troops(T,O) = Σ troops at nodes owned by T
                   within force_hops of any O-owned node

own_troops(O) = Σ 守军 + 野战军团兵力

F_OT = clamp(
    (near_troops(T,O) / max(1, own_troops(O))) / force_norm,
    0, 1
)
```

- 默认 `force_hops = 2`，`force_norm = 2.0`。
- 敌方门口兵力等于己方总兵力 → `F = 0.5`；四倍 → `F = 2.0`，钳到 1。
- 只看边境附近，不看全图，避免“远方大国总兵力”把整个地图都变成威胁。

### 5.7 即时压力 `P`

```text
P_raw = 100 × (
    w_size   × S_T
  + w_border × border_OT
  + w_growth × G_T
  + w_force  × F_OT
)
```

默认权重：

| 分项 | 权重 | 理由 |
|---|---|---|
| 体量 | 0.30 | 大势力天然被忌惮 |
| 接壤 | 0.40 | 直接影响边境安全，权重最高 |
| 扩张 | 0.15 | 惩罚闪电扩张，但不惩罚正常发育 |
| 军力 | 0.15 | 惩罚门口堆兵，鼓励守军分散 |

### 5.8 修正项

```text
sens        = sensitivity[O.personality.key]       # 性格敏感度
rel_damp    = relation_damp[relation_of(O, T)]     # 同盟 / 停战 / 中立
favor       = favor_of(O.diplomacy, T)
favor_damp  = clamp(1 - max(0, favor) / 200, 0.5, 1.0)
rep_damp    = 0.95 if T.reputation >= 60 else 1.00
              # 恶名（≤ -60）不在此重复加成：现有
              # reputation.infamous_npc_aggression 已在 npc_aggression_mult 里 +0.2

P_mod = P_raw × sens × rel_damp × favor_damp × rep_damp
```

> 恶名走既有 `reputation.infamous_npc_aggression`（默认 0.2，作用在侵略倍率上），侠名走新的 `rep_damp = 0.95`（作用在威胁压力上）。两者刻意分开，避免恶名玩家被双重惩罚、数值叠爆。

**性格敏感度**与“侵略性”分开：

> 性格键取自 `faction.personality["key"]`（`config/strategy_npc_names.json` 的 `personalities`）；下表的「侵略性」即该文件的 `aggression`，本文不重复配置，只新增 `sensitivity`。

| 性格 | 侵略性 | 威胁敏感度 | 设计意图 |
|---|---|---|---|
| 扩张型 | 85 | 1.25 | 既好战又警觉，容易先发制人 |
| 魔道型 | 95 | 1.40 | 对强者敏感，倾向以战养战 |
| 经营型 | 30 | 0.85 | 更愿意筑城、结盟 |
| 贸易型 | 20 | 0.70 | 倾向花钱买平安、通商 |
| 隐世型 | 10 | 0.60 | 只要不贴脸就装看不见 |

**关系衰减**：

| 关系 | `rel_damp` | 说明 |
|---|---|---|
| 同盟 | 0.05 | 几乎不视为威胁 |
| 停战 | 0.25 | 仍会警惕，但不会主动开战 |
| 中立 | 1.00 | 正常计算 |
| 战争 | 1.00 | 战争状态另由 `war` 逻辑处理 |

**好感衰减**：好感 0 → 1.0；好感 100 → 0.5。高好感不能完全免疫威胁，但能显著降低敌意。

### 5.9 非接壤减益与霸主例外

```text
hegemon = (S_T >= hegemon_share_threshold)                    # 默认 0.40
          或 (coalition.active 且 coalition.fid == T)

adj_factor = 1.00   if B_OT > 0
             0.70   if B_OT == 0 且 hegemon
             0.30   if B_OT == 0 且非 hegemon

P_eff = P_mod × adj_factor
```

**公敌地板**：

```text
floor_raw = public_base + public_span × clamp(
    (S_T - hegemon_share_threshold) / (1 - hegemon_share_threshold), 0, 1
)

if hegemon:
    if rel(O, T) in ("ally", "truce"):
        # 同盟 / 停战不吃公敌地板，只保留很低的基础警惕
        floor = floor_raw × rel_damp × favor_damp × rep_damp
    else:
        # 普通观察者：地板保底到“宣战线 + margin”，确保非接壤也能积累敌意
        floor = max(
            floor_raw × favor_damp × rep_damp,
            declare_gate + public_floor_margin
        )
    P_eff = max(P_eff, floor)
```

默认 `public_base = 60`、`public_span = 40`、`hegemon_share_threshold = 0.40`、`public_floor_margin = 5`。

- 目标占 40% → 地板 ≈ 65（`declare_gate = 60` + margin），非接壤也能持续积累。
- 目标占 60% → 地板 ≈ 73。
- 目标占 80% → 地板 ≈ 87。

> **性格不参与地板计算**：性格只影响 `P` 的原始计算、`alpha` 积累速度、宣战概率与出兵意愿。这样“寡头必被针对”是硬规则，“谁先动手、多快动手”才体现性格差异。否则隐世型（sens 0.60）会被地板压成 `60 × 0.6 = 36`，永远到不了宣战线，等于霸主免疫。

这样远方的 NPC 即使不接壤，也会对寡头霸主产生足够敌意；但只有霸主才有这个待遇，普通大势力仍受非接壤减益。

### 5.10 累计敌意 `H`

```text
if P_eff >= H:
    alpha = alpha_up_adj              if B_OT > 0
            alpha_up_hegemon_nonadj   if B_OT == 0 且 hegemon
            alpha_up_nonadj           otherwise
else:
    alpha = alpha_down

H = clamp(H + (P_eff - H) × alpha, 0, 100)
```

默认：

| 参数 | 值 | 含义 |
|---|---|---|
| `alpha_up_adj` | 0.020 | 接壤时约 35 tick 半衰到目标值（约 9 小时） |
| `alpha_up_nonadj` | 0.005 | 非接壤时上升极慢，约 139 tick 半衰（约 35 小时） |
| `alpha_up_hegemon_nonadj` | 0.012 | 霸主非接壤也较慢，但明显快于普通非接壤 |
| `alpha_down` | 0.015 | 威胁下降时约 46 tick 半衰（约 11.5 小时） |

**非接壤减益的三重效果**：

1. `P_eff` 乘 0.30，均衡值只有接壤时的三成。
2. `alpha_up` 从 0.020 降到 0.005，积累速度只有四分之一。
3. 如果边境丢失，`H` 会以 `alpha_down` 向较低的目标值回落，不会瞬间清零。

**霸主例外**：

- `adj_factor` 从 0.30 提高到 0.70。
- `alpha_up` 从 0.005 提高到 0.012。
- 额外获得 `public_floor`，保证非接壤也能达到宣战门槛。

### 5.11 玩家龟缩修正

**龟缩判定**（全部满足）：

```text
turtle_player =
    目标为玩家势力
    且 stats.captures_7d 为空
    且 stats.attacks_7d 为空
    且 边境节点平均城防 >= turtle_defense_threshold
    且 目标据点权重占比 < hegemon_share_threshold
```

默认 `turtle_defense_threshold = 1500`。

**修正**：

```text
P_eff  *= turtle_threat_damp          # 默认 0.60
p_day  *= turtle_declare_mult         # 默认 0.50
priority -= 20                        # 目标排序降权
```

**龟缩的边界**：

| 情况 | 是否仍会被正式宣战 |
|---|---|
| 不扩张、不攻击、边境高防、非霸主 | 概率显著降低，但仍有小概率 |
| 玩家是霸主（≥40%） | 龟缩失效，按霸主处理 |
| 玩家近期主动攻击过别人 | 龟缩失效，按复仇 / 机会主义处理 |
| 玩家与霸主结盟 | 龟缩减益减半，NPC 会视为“霸权阵营” |
| NPC 为魔道型且处于绝望 | 龟缩减益减半，仍可能铤而走险 |

> 允许龟缩，不等于绝对安全。龟缩玩家仍可能遭遇边境袭扰、资源被劫、外交施压；但不会轻易被全图围殴。

### 5.12 完整伪代码

```python
def update_threat(world, nodes, factions, armies, stats, cfg):
    snap = build_snapshot(nodes, factions, armies, stats, cfg)
    declare_gate = main_cfg["npc"]["war"]["declare_gate"]

    for o in alive_factions:
        for t in alive_factions:
            if o.id == t.id:
                continue
            if t.id not in snap.accum[o.id]:
                snap.accum[o.id][t.id] = 0.0

            p_raw = 100 * (
                cfg.weights["size"] * snap.share[t.id]
                + cfg.weights["border"] * snap.border[o.id][t.id]
                + cfg.weights["growth"] * snap.growth[t.id]
                + cfg.weights["force"] * snap.force[o.id][t.id]
            )

            p_mod = (
                p_raw
                * cfg.sensitivity[o.personality_key]
                * cfg.relation_damp[rel(o, t)]
                * cfg.favor_damp(favor(o, t))
                * cfg.reputation_damp(t)
            )

            hegemon = (
                snap.share[t.id] >= cfg.hegemon_share_threshold
                or (world.coalition.active and world.coalition.fid == t.id)
            )

            if snap.border[o.id][t.id] > 0:
                adj = 1.0
            elif hegemon:
                adj = cfg.hegemon_nonadj_factor
            else:
                adj = cfg.nonadj_factor

            p_eff = p_mod * adj

            if hegemon:
                floor_raw = cfg.public_base + cfg.public_span * clamp(
                    (snap.share[t.id] - cfg.hegemon_share_threshold)
                    / (1 - cfg.hegemon_share_threshold), 0, 1
                )
                if rel(o, t) in (R.REL_ALLY, R.REL_TRUCE):
                    floor = (
                        floor_raw
                        * cfg.relation_damp[rel(o, t)]
                        * cfg.favor_damp(favor(o, t))
                        * cfg.reputation_damp(t)
                    )
                else:
                    floor = max(
                        floor_raw * cfg.favor_damp(favor(o, t))
                        * cfg.reputation_damp(t),
                        declare_gate + cfg.public_floor_margin,
                    )
                p_eff = max(p_eff, floor)

            if is_turtle(t, snap, stats, cfg):
                p_eff *= cfg.turtle.threat_damp

            h = snap.accum[o.id][t.id]
            if p_eff >= h:
                if snap.border[o.id][t.id] > 0:
                    alpha = cfg.alpha_up_adj
                elif hegemon:
                    alpha = cfg.alpha_up_hegemon_nonadj
                else:
                    alpha = cfg.alpha_up_nonadj
            else:
                alpha = cfg.alpha_down

            h = clamp(h + (p_eff - h) * alpha, 0, 100)
            snap.accum[o.id][t.id] = h
```

> `favor_damp(favor)` 由 `favor_damp_floor` 派生：`clamp(1 - max(0, favor)/200, floor, 1.0)`。
> `reputation_damp(t)`：`t.reputation >= 60` 时返回 `1 - righteous_penalty`，否则 `1.0`（恶名不在此处理）。
> `is_turtle()` 的完整条件见 §5.11；`cfg` 在此指 `main_cfg["npc"]["threat"]`。
> `o.personality_key = (o.get("personality") or {}).get("key", "developer")`。

---

## 6. 威胁分档与行为

| 威胁 `H` | 档位 | 侵略倍率 | 宣战概率/日（基础） | 主要行为 |
|---|---|---|---|---|
| 0~19 | 无感 | ×1.00~1.15 | 0 | 正常经营、扩张 |
| 20~39 | 警觉 | ×1.16~1.31 | 0 | 边境侦察、轻微增兵、好感自然下降 |
| 40~59 | 戒备 | ×1.32~1.47 | 0 | 暂停通商 / 结盟；守家比例提高；寻找盟友 |
| 60~79 | 敌视 | ×1.48~1.63 | 6%~17% | 发“战争阴云”警告；可正式宣战 |
| 80~100 | 公敌 | ×1.64~1.80 | 18%~40% | 合纵；多 NPC 协同；可跨区远征 |

> 「宣战概率/日（基础）」只含 `declare_base_per_day + declare_span_per_day × ratio^1.5`；实际还会乘 `goal_mult`、`personality_war_bias`、龟缩 / 自杀倍率，见 §7.4。

**侵略倍率**：

```text
threat_mult = 1 + aggression_gain × H / 100
aggression_gain = 0.80
```

- `H = 60` → ×1.48。
- `H = 80` → ×1.64。
- `H = 100` → ×1.80。
- 与赛季决战、合纵旧倍率叠乘时，总倍率硬上限 2.20，避免数值爆炸。

---

## 7. NPC 开战逻辑

### 7.1 宣战类型

| 类型 | 触发 | 是否允许非接壤 | 目标偏好 |
|---|---|---|---|
| `border` 边境战争 | 接壤且 `H` 高 | ❌ | 接壤的高价值节点 |
| `hegemon` 讨逆战争 | 目标是寡头霸主 / 合纵目标 | ✅ | 最近的目标节点、首都方向 |
| `revenge` 复仇战争 | 目标近期攻击过自己或盟友，或 `hatred` 高 | ❌（仅可接壤；目标为霸主时按 `hegemon` 处理） | 攻击者边境、薄弱节点 |
| `opportunity` 机会主义 | 目标正与别人交战、守备空虚 | ❌ | 低守军、高产出节点 |

> **V1 实现范围**：`_choose_goal` 目前只产出 `border`（接壤）与 `hegemon`（非接壤寡头），即本次需求覆盖的“边境战争 + 讨逆远征”。`revenge` / `opportunity` 的 `goal_mult` 配置与文案已预留，判定条件列入后续迭代，不影响现有威胁 / 宣战 / 停战链路。

### 7.2 开战时机

| 时间点 | 事件 | 行为 |
|---|---|---|
| T0 | 双方首次接壤 | `H` 开始积累，进入 `alert` |
| T0 + 数小时 | `H ≥ 40` | 进入 `prepare`，提高守家比例、暂停贸易 |
| T0 + 数小时~数天 | `H ≥ 60` | 进入 `hostile`，向玩家发“战争阴云”警告 |
| `hostile` 持续 ≥ 8 tick | 满足 `hostile_hold_ticks` | 开始每 tick 掷宣战 |
| 宣战成功 | 写入 `REL_WAR` | 进入 `war` + `war_frenzy` |
| 宣战后 48 tick | `frenzy` 结束 | 转入正常 `war` |
| 战争疲劳 ≥ 60 | 进入 `weary` | 降低进攻频率，准备停战 |
| 停战 | `REL_TRUCE` | 进入 `truce` → `cooldown` |

**关键约束**：

- 刚接壤不会立刻宣战，必须 `hostile_hold_ticks` 持续敌视。
- 玩家在 `hostile` 阶段收到预警，有时间送礼、结盟、让地、筑防。
- 宣战后不会“秒和”，至少 `min_war_ticks` 后才评估停战。

### 7.3 目标选择

每个 NPC 每 tick 只选**一个**最高优先级目标掷宣战，避免同一势力一 tick 向多国开战。

```text
priority = H
if B_OT > 0:                     priority += 15
if hegemon:                      priority += 25
if revenge:                      priority += 15
if target_at_war_with_others:    priority += 10
if power_ratio < 0.60:           priority += 10
if target is turtle:             priority -= 20
priority -= 10 × max(0, active_wars - 1)
```

`power_ratio`：

```text
power_ratio = own_border_power / max(1, target_border_power)
```

只比较边境附近军力，不比较全图，避免“远方大军”影响局部判断。

### 7.4 宣战概率

```text
ratio = clamp((H - declare_gate) / (100 - declare_gate), 0, 1)

p_day = declare_base_per_day + declare_span_per_day × ratio^1.5
p_day *= goal_mult
p_day *= personality_war_bias

if power_ratio < min_power_ratio:
    if allow_suicide_declare:
        p_day *= suicide_declare_mult
    else:
        p_day = 0

if target is turtle:
    p_day *= turtle_declare_mult

p_day = min(p_day, max_p_day)
p_tick = 1 - (1 - p_day)^(1 / ticks_per_day)
```

默认：

| 参数 | 值 |
|---|---|
| `declare_gate` | 60 |
| `declare_base_per_day` | 0.06 |
| `declare_span_per_day` | 0.34 |
| `max_p_day` | 0.55 |
| `min_power_ratio` | 0.35 |
| `suicide_declare_mult` | 0.55 |
| `turtle_declare_mult` | 0.50 |

`goal_mult`：

| 类型 | 倍率 |
|---|---|
| `border` | 1.00 |
| `hegemon` | 1.30 |
| `revenge` | 1.50 |
| `opportunity` | 0.80 |

`personality_war_bias`：

| 性格 | 倍率 |
|---|---|
| 扩张型 | 1.20 |
| 魔道型 | 1.40 |
| 经营型 | 0.70 |
| 贸易型 | 0.50 |
| 隐世型 | 0.30 |

### 7.5 宣战前置校验

```text
can_declare(O, T):
    1.  O、T 均存活，且 O != T
    2.  T 不是 O 的同盟、停战对象、已交战对象
    3.  tick >= pair.no_declare_until_tick
    3b. pair.last_declare_tick == 0 或
        tick - pair.last_declare_tick >= declare_cooldown_ticks
    4.  T 不在新手保护期（节点保护期不阻止宣战，只阻止战术层选择该节点）
    5.  H >= declare_gate
    6.  pair.phase == "hostile" 且持续 >= hostile_hold_ticks
    7.  O 的 active_wars < max_active_wars_per_npc
    8.  当日宣战数 < max_declarations_per_day
    9.  本 tick 全服宣战数 < max_declarations_per_tick
    10. 满足以下任一：
        a. B_OT > 0
        b. hegemon 且存在可通行路径
    11. 若 B_OT == 0 且非 hegemon：拒绝
    12. 若 B_OT == 0 且 hegemon：
        路径边数 <= cfg.expedition.max_edges
```

### 7.6 宣战执行

```python
async def _declare_npc_war(world, observer, target, pair, goal):
    dcfg = main_cfg["diplomacy"]

    # 1) 建立正式战争关系
    await self._apply_diplomacy_deal(observer, target, "war", 0, dcfg)

    # 2) NPC 战争默认立即开打，避免 war_pending 与 AI 不一致
    #    （_apply_diplomacy_deal 默认写 war_at = now + diplomacy.war_prep_hours）
    for side, other in ((observer, target), (target, observer)):
        dip = side.setdefault("diplomacy", {})
        dip.setdefault(str(other["id"]), {})["war_at"] = 0

    tick = world["tick_no"]
    ticks_per_hour = 3600 // self.tick_seconds()
    frenzy_hours = main_cfg["npc"]["war"]["frenzy"]["hours"]
    pair.update({
        "phase": "war",
        "war_start_tick": tick,
        "last_declare_tick": tick,
        "frenzy_until_tick": tick + frenzy_hours * ticks_per_hour,
        "goal": goal,
        "war_score": 50,
        "exhaustion": 0,
        "last_capture_tick": tick,
        "captures_by_me": 0,
        "captures_by_target": 0,
        "war_start_troops": snapshot_troops(observer),
        "war_start_nodes": count_nodes(observer),
    })

    # 3) 全服公告 / 玩家预警 / 审计
    await self._notify(world, target_player_ids, "war_declared", {...})
    await self._audit(...)
    await self.save_faction(observer)
    await self.save_faction(target)
```

**宣战文案示例**：

- 边境战争：`⚔️「霸刀门」以边境争端为由，向「青木仙庐」宣战！`
- 讨逆战争：`⚔️「血祭教」响应合纵檄文，向霸主「凌霄阁」宣战！`
- 复仇战争：`⚔️「守成谷」为雪前耻，向「裂天刀宗」宣战！`

### 7.7 战后狂热 `war_frenzy`

宣战后 `frenzy_hours`（默认 12 小时 / 48 tick）内，NPC 进入全军出击状态。

| 参数 | 平时 | 狂热 | 配置键 |
|---|---|---|---|
| 每 tick 出兵路数 | 2 | 4 | `frenzy.sorties_bonus = 2` |
| 守家比例 | 0.30 | 0.15（最低 0.10） | `frenzy.home_reserve_delta / home_reserve_min` |
| 出兵比例 | `1 - home_reserve` | 额外 ×1.15 | `frenzy.send_mult` |
| utility 门槛 | 10 | 4 | `frenzy.utility_threshold` |
| 对战争目标权重 | ×1.0 | ×1.30 | `frenzy.target_bonus` |
| 侵略倍率 | ×1.0 | ×1.30（叠乘） | `frenzy.aggression_bonus` |
| 征兵民心门槛 | 40 | 25 | `frenzy.morale_min` |
| 动员费 | 1.0 | 0.90 | `frenzy.mobilization_cost_mult` |
| 携粮 | 1.0 | 1.50 | `frenzy.grain_mult` |

**狂热提前结束条件**：

- 国库 coin 或 grain 低于守财底线。
- 平均民心 < 20。
- 可用兵力 < 300。
- 目标已被灭 / 已停战。

狂热结束后回到正常 `war`，但战争目标仍保留优先级加成（建议 +15%）。

> 最终侵略倍率 = `npc_aggression_mult`（赛季 / 合纵）× `threat_mult`（§6）× `(1 + frenzy.aggression_bonus)`，再 `clamp(..., 0, threat.aggression_cap)`。狂热只是其中一个乘区，不改变现有赛季 / 合纵逻辑。

### 7.8 非接壤霸主远征

只有 `goal == "hegemon"` 的战争允许非接壤出兵。以下 `cfg` 指 `main_cfg["npc"]["war"]`。

```text
if pair.goal == "hegemon" and B_OT == 0:
    target_node = nearest_faction_node(world, from_node, target.id)
    path = find_path(world, from_node, target_node)
    if path and len(path) - 1 <= cfg.expedition.max_edges:
        spawn_npc_army(
            observer, from_node, target_node,
            troops=send,
            grain_mult=cfg.expedition.grain_mult
        )
```

默认：

| 参数 | 值 |
|---|---|
| `expedition.max_edges` | 10 |
| `expedition.min_troops` | 800 |
| `expedition.grain_mult` | 1.50 |
| `expedition.cooldown_ticks` | 8 |

- 路径太长则不宣战，或宣战后等待边境接近。
- 远征军按现有 `_step_march` 规则消耗粮草；断粮会掉士气、逃兵，这是对跨区霸权的自然惩罚。
- 同一 NPC 每 8 tick 最多派一支远征军，避免全图撒兵。

### 7.9 自杀宣战

用户已拍板：**允许 NPC 自杀宣战**。

| 条件 | 处理 |
|---|---|
| `power_ratio >= min_power_ratio` | 正常概率 |
| `power_ratio < min_power_ratio` | 不禁止；`p_day *= suicide_declare_mult`（默认 0.55） |
| NPC 仅剩 ≤ 2 城，或首都已失 | `p_day *= desperation_mult`（默认 0.90），更可能鱼死网破 |
| 魔道型 / 扩张型 | `personality_war_bias` 已提高，天然更容易自杀宣战 |
| 玩家龟缩 | 仍降低概率，但不会完全免疫 |

**设计意图**：

- 世界不是精算机器，弱小势力也会因恐惧、仇恨、疯狂而误判。
- 自杀宣战会让 NPC 更快灭亡，这本身是玩家扩张的机会。
- 但通过 `max_active_wars_per_npc`、全服宣战上限、冷却，避免所有弱者同时送死。

### 7.10 宣战上限与冷却

| 参数 | 默认 | 作用 |
|---|---|---|
| `max_active_wars_per_npc` | 2 | 一个 NPC 同时最多两场战争 |
| `max_declarations_per_tick` | 1 | 全服每 tick 最多一场新战争 |
| `max_declarations_per_day` | 4 | 全服每天最多四场新战争 |
| `declare_cooldown_ticks` | 96 | 同一对势力两次宣战最短间隔（1 天） |
| `post_war_cooldown_ticks` | 96 | 停战后至少 1 天不得再宣战 |
| `hostile_hold_ticks` | 8 | 敌视状态至少持续 2 小时才可宣战 |
| `warning_cooldown_ticks` | 48 | “战争阴云”警告最短重复间隔 |

### 7.11 开战伪代码

```python
async def _step_npc_diplomacy(world):
    snap = await self._threat_snapshot(world)
    cfg = main_cfg["npc"]["war"]
    tick = world["tick_no"]
    day = tick // ticks_per_day          # 世界内“天”，避免依赖真实时钟

    # 1) 更新累计敌意与档位
    self._update_threat_accum(world, snap, tick)
    self._update_pair_phases(world, snap, tick)

    # 2) 清理过期状态
    self._expire_war_states(world, snap, tick)

    declared_today = world.data["npc_ai"]["declarations"]
    if declared_today["day"] != day:
        declared_today = {"day": day, "count": 0}
    declared_this_tick = 0

    for observer in npc_factions:
        pairs = world.data["npc_ai"]["pairs"].get(str(observer.id), {})
        active_wars = count_active_wars(pairs)
        if active_wars >= cfg.max_active_wars_per_npc:
            continue

        candidates = []
        for target in alive_factions:
            if target.id == observer.id:
                continue
            pair = pairs.get(str(target.id)) or default_pair()
            if pair.phase != "hostile":
                continue
            if tick - pair.hostile_since_tick < cfg.hostile_hold_ticks:
                continue

            h = snap.accum[observer.id][target.id]
            if h < cfg.declare_gate:
                continue
            if not can_declare(observer, target, pair, snap, tick):
                continue

            goal = choose_goal(observer, target, pair, snap)
            priority = compute_priority(observer, target, pair, snap, goal)
            candidates.append((priority, target, pair, goal, h))

        if not candidates:
            continue

        candidates.sort(key=lambda x: -x[0])
        _, target, pair, goal, h = candidates[0]

        p_day = declare_chance(h, goal, observer, target, pair, snap)
        p_tick = 1 - (1 - p_day) ** (1 / ticks_per_day)
        rng = self._rng("npc_declare", tick, observer["id"], target["id"])

        if rng.random() < p_tick:
            if declared_this_tick >= cfg.max_declarations_per_tick:
                continue
            if declared_today["count"] >= cfg.max_declarations_per_day:
                continue
            await self._declare_npc_war(world, observer, target, pair, goal)
            declared_this_tick += 1
            declared_today["count"] += 1

    world.data["npc_ai"]["declarations"] = declared_today
    await self._commit()
```

---

## 8. NPC 停战逻辑

### 8.1 战争疲劳 `E`

每 tick 对每场战争更新 `E`（0~100）：

```text
troop_loss      = 1 - current_troops / max(1, war_start_troops)
treasury_stress = max(0, (reserve_coin - coin) / max(1, reserve_coin))
morale_stress   = max(0, (50 - avg_morale) / 50)
no_progress     = min(1, (tick - last_capture_tick) / no_progress_norm)
third_party     = clamp(max(0, max_other_H - H_target) / 50, 0, 1)
lost_nodes      = min(3, nodes_lost_since_war_start) / 3
lost_capital    = 1 if capital 已失 else 0

E = 100 × (
      0.35 × troop_loss
    + 0.15 × treasury_stress
    + 0.10 × morale_stress
    + 0.15 × no_progress
    + 0.10 × third_party
    + 0.15 × lost_nodes
) + 25 × lost_capital

E = clamp(E - 0.20 × max(0, war_score - 50), 0, 100)
```

默认 `no_progress_norm = 96`（1 天没有新占领即视为停滞）。

**各项含义**：

| 分项 | 权重 | 说明 |
|---|---|---|
| 兵力损失 | 0.35 | 打光了自然想停 |
| 国库压力 | 0.15 | 打穷了自然想停 |
| 民心压力 | 0.10 | 抽丁太狠会想停 |
| 无进展 | 0.15 | 僵持越久越厌战 |
| 第三方威胁 | 0.10 | 背后出现更大威胁时，想先停战 |
| 丢失据点 | 0.15 | 丢城越多越想停 |
| 首都失守 | +25 | 大幅提高求和意愿 |
| 战争顺风 | -0.20×(war_score-50) | 打得顺则降低求和意愿 |

### 8.2 战争顺风度 `war_score`

```text
war_score = clamp(
    50
    + 10 × captures_by_me
    - 10 × captures_by_target
    + 20 × (是否攻占目标首都)
    - 5 × own_armies_wiped
    + 5 × enemy_armies_wiped,
    0, 100
)
```

- `0~30`：明显劣势。
- `30~60`：僵持。
- `60~100`：明显优势。

V1 可先只统计据点占领，战损与军团击破后续再接。

### 8.3 停战时机

| 条件 | 处理 |
|---|---|
| 战争时长 < `min_war_ticks`（24 tick / 6h） | 不评估停战，避免秒和 |
| `tick < frenzy_until_tick`（狂热期） | 不评估停战，让狂热打完；`E` 仍照常累积 |
| `E ≥ peace_gate`（默认 60） | 进入 `weary` 派生状态，开始按概率求和 |
| 战争时长 ≥ `max_war_ticks`（672 tick / 7 天） | 强制进入求和流程 |
| 首都失守 / 仅剩 ≤ 2 城 | 大幅提高求和概率 |
| 第三方威胁 `max_other_H ≥ H_target + 15` | 大幅提高求和概率 |
| 国库连续 24 tick 低于守财底线 | 提高求和概率 |
| `war_score ≥ 60` 且 `E < 80` | 降低求和概率（V1 不要求赔款） |
| 目标龟缩且久攻不下 | 提高求和概率 |

### 8.4 停战概率

```text
ratio = clamp((E - peace_gate) / (100 - peace_gate), 0, 1)

p_day = peace_base_per_day + peace_span_per_day × ratio^1.2
p_day *= personality_peace_bias

if war_score >= 60:
    p_day *= 0.50

p_day = min(p_day, max_peace_p_day)
p_tick = 1 - (1 - p_day)^(1 / ticks_per_day)
```

默认：

| 参数 | 值 |
|---|---|
| `peace_gate` | 60 |
| `peace_base_per_day` | 0.08 |
| `peace_span_per_day` | 0.42 |
| `max_peace_p_day` | 0.60 |
| `min_war_ticks` | 24 |
| `max_war_ticks` | 672 |

`personality_peace_bias`：

| 性格 | 倍率 |
|---|---|
| 隐世型 | 1.30 |
| 贸易型 | 1.50 |
| 经营型 | 1.10 |
| 扩张型 | 0.70 |
| 魔道型 | 0.50 |

### 8.5 NPC → NPC 停战

```text
1. 发起方计算 E，达到 peace_gate 后按概率触发。
2. 目标方计算接受概率：
   accept = 0.15
          + 0.55 × target_E / 100
          + 0.15 × favor / 100
          - 0.25 × (attacker_war_score - 50) / 50
          - 0.10 × (target_war_score - 50) / 50
   accept = clamp(accept, 0.05, 0.95)
3. 接受 → _apply_diplomacy_deal(initiator, target, "truce", 0, dcfg)
4. 拒绝 → 继续战争，双方 E 继续上升，下次再评估。
```

### 8.6 NPC → 玩家停战

玩家需要可操作的停战入口，流程：

```text
1. NPC 进入 weary，且 E >= peace_gate。
2. 向玩家势力写入外交提案（沿用现有 propose_* 契约，保留 prev 关系快照）：
   注意：diplomacy 条目的 until 是**真实时间戳**（diplomacy_respond 直接比较 _now()）；
   pair 内部状态才用 tick。
   now = _now()
   expire = now + ceasefire_hours * 3600
   self._set_relation(t_dip, npc_fid, "propose_truce", until=expire)
   t_dip[str(npc_fid)]["from"] = int(npc_fid)        # diplomacy_respond 必读
   self._set_relation(f_dip, player_fid, "propose_truce_out", until=expire)
3. 同时设置停火标记（不改变 pair.phase）：
   pair.ceasefire_until_tick = tick + ceasefire_hours * ticks_per_hour
   - 暂停对玩家的进攻性出兵
   - 保留防御性驻军
4. 玩家通过现有 diplomacy_respond 接受 / 拒绝（无需职位改动，沿用 OFFICER_POSITIONS 校验）。
5. 接受 → _apply_diplomacy_deal(npc, player, "truce", 0, dcfg)，停战 truce_hours；
   拒绝 / 过期 → 停火标记失效，战争恢复。
6. 每次提案间隔 >= offer_cooldown_ticks（默认 96 tick / 1 天）。
```

**提案文案**：

- 劣势方：`🕊️「守成谷」遣使求和：愿休兵 48 小时，共御外敌。`
- 僵持方：`🕊️「坊商会」提议停战：再打下去只会两败俱伤。`
- 优势方：`🕊️「霸刀门」愿意停战：交出边境三城，便可保全宗庙。`（V1 不做割地，可先白和）

**V1 建议**：NPC → 玩家只做白和 / 停战，不做赔款与割地，避免与现有 `_apply_diplomacy_deal` 的付款方向语义冲突。赔款、割地放到 P3。

> `ceasefire` 期间，`_step_npc_ai` 必须跳过针对该目标的进攻性出兵；防御性驻军、对其他目标的行动不受影响。

### 8.7 停战条款与战后冷却

| 条款 | V1 | 说明 |
|---|---|---|
| 白和 | ✅ | 双方停战 48h |
| 赔款 | ❌（P3） | 需明确付款方，现有接口按“提案方付款”设计 |
| 割地 | ❌（P3） | 需要新的节点转移流程 |
| 停战时长 | 48h | 复用 `diplomacy.truce_hours` |
| 战后冷却 | 96 tick（1 天） | `no_declare_until_tick` |
| 疲劳恢复 | 每天 -20 | 停战后 `E` 逐步回落 |
| 复仇窗口 | 7 天 | 停战后短期内再次宣战概率提高 20% |

### 8.8 停战伪代码

```python
async def _step_npc_peace(world):
    snap = await self._threat_snapshot(world)
    tick = world["tick_no"]
    dcfg = main_cfg["diplomacy"]

    for observer in npc_factions:
        for target in alive_factions:
            pair = get_pair(observer, target)
            if pair.phase != "war":
                continue

            # 疲劳与顺风每 tick 都更新（狂热期也累积），只是狂热期不评估求和
            update_exhaustion(pair, observer, target, snap, tick)
            update_war_score(pair, world)

            if tick < pair.frenzy_until_tick:      # frenzy 派生状态
                continue
            if tick - pair.war_start_tick < cfg.min_war_ticks:
                continue

            if not should_seek_peace(pair, observer, target, snap, tick):
                continue

            p_day = peace_chance(pair, observer, target)
            p_tick = 1 - (1 - p_day) ** (1 / ticks_per_day)
            rng = self._rng("npc_peace", tick, observer["id"], target["id"])
            if rng.random() >= p_tick:
                continue

            if target.kind == "npc":
                if rng.random() < peace_accept_chance(pair, target):
                    truce_until_tick = tick + dcfg.truce_hours * ticks_per_hour
                    await self._apply_diplomacy_deal(
                        observer, target, "truce", 0, dcfg)
                    pair.phase = "truce"
                    pair.truce_until_tick = truce_until_tick
                    pair.no_declare_until_tick = max(
                        truce_until_tick, tick + cfg.post_war_cooldown_ticks)
                else:
                    pair.last_offer_tick = tick
            else:
                await self._npc_send_truce_offer(world, observer, target, pair)
                pair.ceasefire_until_tick = tick + cfg.ceasefire_hours * ticks_per_hour
                pair.last_offer_tick = tick
```

---

## 9. 与现有系统整合

| 现有系统 | 处理方式 |
|---|---|
| `npc_utility` | 威胁作为 `util` 乘数；修正 `threat_nearby_power` 与 `adjacent_to_enemy_core` |
| `npc_aggression_mult` | 保留赛季决战倍率；合纵旧 ×1.5 逐步降为 1.0，由威胁倍率接管 |
| `favor_aggression_mult` | 用 `favor_eff = favor - 0.4 × H` 传入，让威胁侵蚀好感带来的和平 |
| `hegemony_share` / `coalition` | 保留事件 / UI / 讨逆任务；威胁体量项复用同一权重口径 |
| `seed_npc_diplomacy` | 保留开局预置；后续自主关系由威胁状态机接管 |
| `hatred` | 目前只写不读；升级为复仇判定输入，或作为威胁第 5 分项 |
| `_favor_on_war` | 宣战时继续调用，触发第三方 NPC 表态 |
| `check_truce_violation` | 保留；NPC 自主宣战走正式关系，不触发“突袭”惩罚 |
| 新手保护 | 宣战前置检查必须排除 `protection.newbie_days` 内的玩家 |
| 节点保护期 | 宣战可以，但 AI 不会选择 `protect_until > now` 的节点 |
| 赛季阶段 | `season.final_aggression_bonus`（默认 +0.3）继续作用于 `npc_aggression_mult`；威胁倍率独立相乘，总上限 `threat.aggression_cap` |
| 联盟 | 同盟关系 `rel_damp=0.05`；后续按联盟块计算体量与威胁 |
| 合纵倍率 | `season.hegemony_npc_aggression_mult`（默认 1.5）保留为事件期加成；P2 起逐步降为 1.0，由公敌地板接管 |

**与合纵的关系**：

- 合纵事件、檄文、讨逆任务继续由 `coalition_state` 驱动。
- 合纵不再只靠 `npc_aggression_mult` 的全局 ×1.5；改为：
  - 霸主目标获得 `public_floor`（保底 `declare_gate + margin`）。
  - 接壤 NPC 自然获得高威胁。
  - 非接壤 NPC 也能靠地板跨过宣战线，但出兵仍要过战术层（路径 ≤ `expedition.max_edges`）。
- 合纵从“全图统一倍率”变成“每个 NPC 根据自己的威胁感知决定是否参战”，讨逆任务与 UI 不变。

---

## 10. 配置设计

建议全部挂在 `config/strategy_config.json` 的 `npc.threat` / `npc.war` / `npc.peace` 下，旧存档缺省时使用代码内默认值（`npc.*` 已有 `aggression_threshold` 等键，互不冲突）。

```json
{
  "npc": {
    "threat": {
      "enabled": true,
      "eval_interval_ticks": 1,
      "level_bonus": 0.10,
      "weights": {
        "size": 0.30,
        "border": 0.40,
        "growth": 0.15,
        "force": 0.15
      },
      "border_norm": 8,
      "growth_window_ticks": 96,
      "growth_norm": 0.10,
      "force_hops": 2,
      "force_norm": 2.0,
      "sensitivity": {
        "expansive": 1.25,
        "developer": 0.85,
        "trader": 0.70,
        "hermit": 0.60,
        "demonic": 1.40
      },
      "relation_damp": {
        "ally": 0.05,
        "truce": 0.25,
        "neutral": 1.0,
        "war": 1.0
      },
      "favor_damp_floor": 0.5,
      "reputation": {
        "righteous_penalty": 0.05
      },
      "nonadj_factor": 0.30,
      "hegemon_nonadj_factor": 0.70,
      "hegemon_share_threshold": 0.40,
      "public_base": 60,
      "public_span": 40,
      "public_floor_margin": 5,
      "alpha_up_adj": 0.020,
      "alpha_up_nonadj": 0.005,
      "alpha_up_hegemon_nonadj": 0.012,
      "alpha_down": 0.015,
      "tiers": {
        "alert": 20,
        "prepare": 40,
        "hostile": 60,
        "coalition": 80
      },
      "aggression_gain": 0.80,
      "aggression_cap": 2.20,
      "turtle": {
        "enabled": true,
        "defense_threshold": 1500,
        "threat_damp": 0.60,
        "declare_mult": 0.50,
        "priority_penalty": 20,
        "exclude_hegemon": true
      }
    },
    "war": {
      "declare_gate": 60,
      "hostile_hold_ticks": 8,
      "declare_base_per_day": 0.06,
      "declare_span_per_day": 0.34,
      "max_p_day": 0.55,
      "min_power_ratio": 0.35,
      "allow_suicide_declare": true,
      "suicide_declare_mult": 0.55,
      "desperation_mult": 0.90,
      "max_active_wars_per_npc": 2,
      "max_declarations_per_tick": 1,
      "max_declarations_per_day": 4,
      "declare_cooldown_ticks": 96,
      "post_war_cooldown_ticks": 96,
      "warning_cooldown_ticks": 48,
      "personality_war_bias": {
        "expansive": 1.20,
        "developer": 0.70,
        "trader": 0.50,
        "hermit": 0.30,
        "demonic": 1.40
      },
      "goal_mult": {
        "border": 1.00,
        "hegemon": 1.30,
        "revenge": 1.50,
        "opportunity": 0.80
      },
      "frenzy": {
        "hours": 12,
        "sorties_bonus": 2,
        "home_reserve_delta": -0.15,
        "home_reserve_min": 0.10,
        "send_mult": 1.15,
        "utility_threshold": 4,
        "target_bonus": 1.30,
        "aggression_bonus": 0.30,
        "morale_min": 25,
        "mobilization_cost_mult": 0.90,
        "grain_mult": 1.50
      },
      "expedition": {
        "max_edges": 10,
        "min_troops": 800,
        "grain_mult": 1.50,
        "cooldown_ticks": 8
      }
    },
    "peace": {
      "min_war_ticks": 24,
      "max_war_ticks": 672,
      "peace_gate": 60,
      "peace_base_per_day": 0.08,
      "peace_span_per_day": 0.42,
      "max_peace_p_day": 0.60,
      "no_progress_norm": 96,
      "ceasefire_hours": 24,
      "offer_cooldown_ticks": 96,
      "post_war_cooldown_ticks": 96,
      "personality_peace_bias": {
        "expansive": 0.70,
        "developer": 1.10,
        "trader": 1.50,
        "hermit": 1.30,
        "demonic": 0.50
      }
    }
  }
}
```

---

## 11. 数据落点与兼容

### 11.1 写入位置

| 数据 | 位置 | 说明 |
|---|---|---|
| 累计敌意 | `world.data["npc_ai"]["accum"]` | 每 tick 更新 |
| 势力对状态 | `world.data["npc_ai"]["pairs"]` | 战争、停战、冷却 |
| 宣战计数 | `world.data["npc_ai"]["declarations"]` | 全服频率限制 |
| 近期扩张 / 攻击 | `world.data["npc_ai"]["stats"]` | 龟缩 / 复仇判定 |
| 威胁快照 | `world.data["threat"]` | UI / 调试 / 外交展示 |

### 11.2 旧存档兼容

- 缺少 `npc_ai` / `threat` 时，按空状态初始化。
- 缺少配置项时，使用代码内默认值。
- 旧存档中已有 `REL_WAR` 但没有 `pair` 状态的，首次进入时补建：
  - `phase = "war"`
  - `war_start_tick = tick`
  - `frenzy_until_tick = 0`
  - `truce_until_tick = 0`、`no_declare_until_tick = 0`
  - `exhaustion = 0`、`war_score = 50`
  - `last_capture_tick = tick`（避免刚补建就被判“无进展”）
- 不新增数据库表，不改 `strategy_factions` / `strategy_nodes` 结构。

### 11.3 性能

- 36 节点 × 8~10 势力，`O(N × F²)` 约几千次运算，每 tick 毫秒级。
- 累计状态只在内存中更新，随 `world.data` 一次落库。
- 如果未来地图扩大到 100+ 节点，可把 `eval_interval_ticks` 调到 4，并用邻接表缓存。
- 地域军力统计只做 1~2 跳 BFS，避免全图最短路。

---

## 12. UI 与可观测性

### 12.1 玩家可见

| 视图 | 内容 |
|---|---|
| 天下威胁榜 | 各势力 `public` 值、排名、霸权占比 |
| 对我敌意 | 每个 NPC 对玩家的 `H`、档位、主要来源 |
| 战争状态 | 当前交战方、战争目标、`war_score`、`E`、狂热倒计时 |
| 战争阴云 | `hostile` 档预警：“X 正在集结，目标可能是你” |
| 停战提案 | 接受 / 拒绝按钮，剩余有效期 |
| 威胁拆解 | 体量 / 接壤 / 扩张 / 军力四项条形图，不暴露精确公式 |

### 12.2 调试日志

```text
[strategy] threat O#3→T#7 P=71.2 H=62.1 tier=hostile border=9.0 size=0.40
[strategy] NPC#3 对 #7 宣战 goal=hegemon H=78.2 power_ratio=0.42
[strategy] NPC#3 pair#7 进入 frenzy until=...
[strategy] NPC#3 pair#7 E=64.2 war_score=38 → 求和
[strategy] NPC#3 对 #7 停战，truce_until=...
```

### 12.3 前端落点

| 文件 | 改动 |
|---|---|
| `managers/strategy_manager.py` `get_state` | 新增 `npc_world`（威胁榜 / 扩张倾向 / 对外关系 / 作战情况 / 风声）；`winds` 改为对象数组（`text/kind/kind_name/tick`） |
| `webchat_room/static/strategy/index.html` | 新增「天下」标签页 |
| `webchat_room/static/strategy/strategy.js` | `tabWorld()` 渲染威胁榜、NPC 扩张倾向、作战与风声；总览风声兼容对象数组 |
| `docs/player/xiuxian_knowledge_base.md` | 增加“威胁与战争阴云 / 天下页”说明 |
| `README.md` | 按版本记录更新 |

---

## 13. 边界情况与风险

| 风险 | 表现 | 对策 |
|---|---|---|
| 群殴致死 | 所有 NPC 同时宣战，霸主瞬间出局 | 全服每 tick 1 场、每天 4 场、每 NPC 最多 2 场、冷却 1 天 |
| 死亡螺旋 | 被围殴 → 丢城 → 更弱 → 更多人来打 | 停战后 1 天冷却；求和概率随疲劳上升；龟缩减益 |
| 威胁失控 | 体量项让所有大势力都被针对 | 非接壤 0.30 减益；只有霸主有公敌地板 |
| 霸主例外过强 | 霸主被全图跨区围殴 | 远征路径 ≤10 边、远征冷却、补给消耗、宣战上限 |
| NPC 自杀过多 | 弱国排队送死 | 自杀倍率 0.55、每 NPC 战争上限、全服宣战上限 |
| 玩家龟缩无敌 | 龟缩玩家永远没人打 | 龟缩只降低概率；霸主、近期攻击、结盟霸主会失效 |
| 停战太频繁 | 战争打不起来 | `min_war_ticks=24`、`peace_gate=60`、停战后冷却 |
| 数值爆炸 | 威胁 × 赛季 × 合纵叠乘过高 | 总侵略倍率硬上限 2.20 |
| 旧存档异常 | 缺字段导致 KeyError | 全部 `.get` + 默认值；首次补建 pair |
| 性能 | 每 tick 全量重算 | 36 节点规模可忽略；可降频到 4 tick |
| 确定性问题 | 同一 tick 重跑结果不同 | 所有随机用 `self._rng("npc_declare" / "npc_peace", tick, o, t)` 固定种子 |
| 数据膨胀 | `world.data` 越来越大 | 只保留存活势力对；灭亡 / 停战过期条目清理 |

---

## 14. 测试计划

| 测试 | 断言 |
|---|---|
| 体量单调性 | 目标占城越多，`S_T` 越高，`P` 越高 |
| 接壤单调性 | 同样体量下，接壤边越多，`P` 越高 |
| 非接壤减益 | 无接壤时 `P_eff ≤ 0.35 × P_mod`（霸主除外） |
| 累计速度 | 非接壤 `H` 上升速度显著慢于接壤 |
| 霸主例外 | 霸主无接壤时 `H` 仍能达到 `declare_gate` |
| 霸主地板 | 非同盟 / 非停战的观察者对霸主 `P_eff ≥ declare_gate + public_floor_margin` |
| 地板对同盟无效 | 与霸主同盟 / 停战时地板按 `rel_damp` 大幅衰减 |
| 龟缩判定 | 不扩张 + 高防 + 非霸主 → `H` 降低、宣战概率减半 |
| 龟缩失效 | 霸主 / 近期攻击 / 结盟霸主时龟缩不生效 |
| 自杀宣战 | `power_ratio < 0.35` 时仍可能宣战，但概率降低 |
| 狂热参数 | 宣战后 `sorties` +2（最多 4）、`utility_threshold=4`、`send ×1.15`、`home_reserve` 至少 −0.15 且不低于 0.10 |
| 宣战上限 | 每 tick 最多 1 场、每天最多 4 场、每 NPC 最多 2 场 |
| 宣战冷却 | 停战后 `no_declare_until_tick` 内不再宣战 |
| 非接壤霸主远征 | 路径 ≤10 边才派远征军；无路径不宣战 |
| 战争疲劳 | 兵力损失 / 国库见底 / 丢城提高 `E` |
| 停战概率 | `E` 越高，求和概率越高；顺风时降低 |
| 停战接受 | 目标越疲劳越容易接受；攻击方越顺风越难被接受 |
| 玩家停战提案 | 写入 `propose_truce`，接受后进入 48h 停战 |
| 停火状态 | 提案期间 NPC 暂停进攻，拒绝 / 过期后恢复 |
| 旧存档 | 缺 `npc_ai` / 配置时正常初始化 |
| 确定性 | 同 world/tick/seed 下宣战结果一致 |
| 性能 | 36 节点 × 10 势力单 tick < 10ms |

---

## 15. 分阶段落地

| 阶段 | 内容 | 风险 | 验收 |
|---|---|---|---|
| **P0 只读** | 纯函数 + `_threat_snapshot` + `world.data.threat` + UI 展示 + 单测；不改变 AI | 低 | 威胁榜 / 拆解条正常，数值分布合理 |
| **P1 战术接入** | `util` 叠威胁倍率；修复 `threat_nearby_power` / `adjacent_to_enemy_core`；高威胁提高守家比例 | 低 | NPC 更倾向攻击高威胁目标，但仍只在相邻用兵 |
| **P2 开战** | `_step_npc_diplomacy` + `war_frenzy` + 警告 + 全服上限 + 自杀宣战 + 霸主远征 | 中 | 能看到 NPC 自主宣战，且不会被瞬间围殴 |
| **P3 停战** | 战争疲劳 + 求和 + 玩家停战提案 + 停火 + 战后冷却 | 中 | 长战争会自然收束，龟缩玩家能等到和平 |
| **P4 深化** | NPC 自主结盟、联盟块威胁、赔款 / 割地、仇恨并入、跨区协同 | 高 | 均势外交成型 |

**建议先做 P0 + P1**，用真实 tick 数据校准 `border_norm`、`growth_norm`、`alpha_*`、`declare_gate`，再开 P2。不要一上来就同时开宣战和停战。

---

## 16. 函数清单

### 16.1 `core/strategy_rules.py`（纯函数，易测试）

| 函数 | 作用 |
|---|---|
| `node_threat_value(node, score_table, level_bonus)` | `V(n)` |
| `threat_size_share(nodes, target_fid, ...)` | `S_T` |
| `threat_border_pressure(nodes, observer_fid, target_fid, ...)` | `B_OT`、`R_OT` |
| `threat_growth(history, target_fid, ...)` | `G_T` |
| `threat_force_ratio(nodes, armies, observer_fid, target_fid, ...)` | `F_OT` |
| `threat_pressure(...)` | `P_raw` / `P_mod` |
| `threat_adjacency_factor(...)` | 非接壤 / 霸主系数 |
| `threat_accumulate(old_h, p_eff, ...)` | `H` |
| `threat_tier(h, tiers)` | 档位 |
| `threat_war_priority(...)` | 宣战目标排序 |
| `threat_declare_chance(...)` | 宣战概率 |
| `threat_exhaustion(...)` | 战争疲劳 |
| `threat_peace_chance(...)` | 停战概率 |
| `threat_peace_accept_chance(...)` | 停战接受概率 |

### 16.2 `managers/strategy_manager.py`

| 函数 | 作用 |
|---|---|
| `_threat_snapshot(world)` | 构建派生快照（体量 / 边境 / 扩张 / 军力 / 霸主 / 龟缩），并按窗口写入 `stats.share_history` |
| `_update_threat_accum(world, snap, tick)` | 只做 `H` 的累计与衰减（§5.10） |
| `_update_pair_phases(world, snap, tick)` | 按档位改写 `phase` 与 `hostile_since_tick`（§4.3，带滞回） |
| `_expire_war_states(world, snap, tick)` | `truce→cooldown`、清理过期 `ceasefire` / `pair` |
| `_step_npc_diplomacy(world)` | 每 tick 收敛威胁、推进档位，并执行自主宣战（威胁更新已并入此步，不再单列 `_step_npc_threat`） |
| `_step_npc_peace(world)` | 战争疲劳与求和 |
| `_declare_npc_war(...)` | 宣战执行（立即开战 + 狂热） |
| `_npc_send_truce_offer(...)` | 向玩家发停战提案 |
| `_npc_frenzy_state(world, faction_id)` | 狂热参数（实现名；设计稿曾称 `_npc_war_frenzy_params`） |
| `_npc_expedition_target(...)` | 非接壤霸主远征目标 |
| `_npc_world_view(world, f_meta, winds)` | 「天下」页聚合成交（威胁榜 / 扩张 / 关系 / 作战 / 风声） |

### 16.3 `managers/strategy_faction_mixin.py`

| 函数 | 作用 |
|---|---|
| `_npc_diplomacy_roll`（改造） | 停战 / 和谈接受率纳入 `E` 与 `war_score` |
| `_npc_send_truce_offer`（实现在 `strategy_threat_mixin.py`，复用 `_set_relation`） | 写入 `propose_truce` / `propose_truce_out` 与 `from` 字段 |
| `_sync_npc_pair_after_truce`（实现在 `strategy_threat_mixin.py`） | 停战后同步双方 `phase` / `truce_until_tick` / `no_declare_until_tick` |

### 16.4 `managers/strategy_manager.py::_step_npc_ai`（改造）

| 改造点 | 说明 |
|---|---|
| `threat_nearby_power` | 传入真实边境敌军兵力 |
| `adjacent_to_enemy_core` | 改为判断目标是否邻接目标势力首都 |
| `sorties` | 引入 `war_frenzy`，最多 4 路 |
| `home_reserve` | 先按威胁 +0.25，再叠加狂热 -0.15，最低 0.10 |
| `send` | 狂热 ×1.15 |
| `best_util` | 高威胁目标加成；狂热降到 4 |
| `war_target` | 战争目标权重 ×1.3 |
| `expedition` | 非接壤霸主远征分支 |

---

## 17. 待定项

| 项 | 建议 |
|---|---|
| 威胁值是否公开精确数字 | 只公开档位与分项条，避免玩家过度精算 |
| 玩家能否查看 NPC 对别人的威胁 | 可以看“天下威胁榜”，不看具体势力对 |
| NPC 是否自主结盟 | P4 再做；V1 只做威胁→宣战→停战 |
| 赔款 / 割地 | P3 再做；V1 白和 |
| 联盟块威胁 | P4 再做；V1 先按单势力计算，避免复杂度 |
| 威胁是否跨赛季继承 | 不继承；新赛季重置 `H`，只保留荣誉 |
| 龟缩判定窗口 | 7 天；可配置 |
| 自杀宣战上限 | 每 NPC 同时 2 场；全服每天 4 场 |
| 霸主远征 | 路径 ≤10 边；后续可接“借道”机制 |

---

## 18. 一句话总结

> **把“谁强、谁挨着我、谁在扩张、谁在门口堆兵”量化为有向累计威胁 `H`；接壤加得快、不接壤加得慢且衰减快，寡头霸主例外；威胁决定 NPC 想不想打，`npc_utility` 决定打不打得动；宣战后进入短期狂热大幅提高出兵，打累了再按战争疲劳主动求和。**

---

## 19. 实现清单（新增代码 vs 对接现有代码）

### 19.1 新增代码

| 文件 | 内容 |
|---|---|
| `managers/strategy_threat_mixin.py` | **新增** `StrategyThreatMixin`，承载威胁快照、累计敌意 `H`、势力对状态机、自主宣战、战争疲劳与停战、狂热参数、龟缩判定、风声写入与「天下」视图 |
| `core/strategy_rules.py`（文件末尾追加） | **新增**纯函数：`threat_node_value` / `threat_share_by_faction` / `threat_border_metrics` / `threat_growth` / `threat_force_ratio` / `threat_pressure` / `threat_favor_damp` / `threat_reputation_damp` / `threat_adjacency_factor` / `threat_public_floor` / `threat_alpha` / `threat_accumulate` / `threat_mult` / `threat_tier` / `threat_is_turtle` / `threat_priority` / `threat_declare_chance` / `per_day_to_per_tick` / `threat_war_score` / `threat_exhaustion` / `threat_peace_chance` / `threat_peace_accept` / `threat_relation_key` |
| `config/strategy_config.json` | **新增** `npc.threat` / `npc.war` / `npc.peace` / `npc.wind` 四段配置；缺省值由 `_merge` 兜底，旧配置不写也能跑 |
| `webchat_room/static/strategy/index.html` | **新增**「天下」标签页入口 |
| `webchat_room/static/strategy/strategy.js` | **新增** `tabWorld()`；总览风声兼容对象数组（旧字符串数组仍可渲染） |
| `tests/test_strategy_threat.py` | **新增**规则纯函数 + 管理器集成测试（威胁落盘 / 势力对 / 停战同步 / 风声 / 前端视图） |

### 19.2 对接现有代码

| 文件 / 函数 | 改造 |
|---|---|
| `managers/strategy_manager.py` 类定义 | `StrategyThreatMixin` 作为第一基类组合进 `StrategyManager` |
| `managers/strategy_manager.py` `_NOTIFY_MIN_LEVEL` | 新增 `war_declared` / `peace_offer`（默认档 1，玩家切身利害可见） |
| `managers/strategy_manager.py` `_tick_locked` | 在 `_step_arrivals` 后、`_step_npc_ai` 前插入 `_step_npc_diplomacy` / `_step_npc_peace`；在 `_step_season` 后插入 `_step_winds` |
| `managers/strategy_manager.py` `_step_npc_ai` | 传入真实 `threat_nearby_power`；修正 `adjacent_to_enemy_core` 为目标节点邻接**目标势力**首都；叠乘 `threat_mult` 与狂热倍率并套 `aggression_cap`；路数 2→4、守家比例按威胁 / 狂热调整、出兵量 ×`send_mult`；停火窗口跳过进攻；新增非接壤霸主远征分支 |
| `managers/strategy_manager.py` `_spawn_npc_army` | 新增可选参数 `cost_mult` / `grain_mult` / `morale_min`，缺省行为不变；狂热时传动员费 / 携粮 / 民心门槛覆盖值 |
| `managers/strategy_manager.py` `_capture_node` | 易主时调用 `_note_threat_capture`，累计战果 / 龟缩统计并写「战报」风声 |
| `managers/strategy_manager.py` `_gc_world_data` | 风声清理改用 `npc.wind.keep`，保留 `kind/tick` 结构 |
| `managers/strategy_manager.py` `get_state` | 新增 `npc_world` 视图；`winds` 由字符串数组改为对象数组 |
| `managers/strategy_faction_mixin.py` `_npc_diplomacy_roll` | 停战接受率纳入战争疲劳 `E` 与双方 `war_score`（`threat_peace_accept`），保留 0.20 下限 |
| `managers/strategy_faction_mixin.py` `diplomacy_propose` / `diplomacy_respond` | 立即接受停战 / 和谈、玩家应答接受停战后调用 `_sync_npc_pair_after_truce`；拒绝求和时清空 `ceasefire_until_tick` |
| `managers/strategy_manager.py` `get_state` 上游数据 | `f_meta` 增补 `diplomacy` / `personality` / `capital_node_id`；按可见军团统计 `armies/troops`；对外 `factions` 明细过滤 `diplomacy/personality` 防泄漏与膨胀 |

### 19.3 持久化与兼容

| 项 | 口径 |
|---|---|
| 数据库 | **不新增表、不改表结构**；全部状态写入 `strategy_worlds.data_json` |
| `world.data.npc_ai` | `accum`（有向 `H`）/ `pairs`（有向双份状态）/ `stats`（7 天战果与体量历史）/ `declarations`（每日宣战额度） |
| `world.data.threat` | 最近一次快照的 `inst/accum/tier/split/public`，供前端与调试读取 |
| `world.data.winds` | 复用既有风声数组，新增 `kind/tick` 字段；旧 `{visible_at,text}` 条目仍可读取 |
| 旧存档 | 缺 `npc_ai/threat/pairs` 时按需初始化；所有读取走 `.get` + 默认值 |

---

## 20. 风声机制

### 20.1 定位

风声是**微缩、短期、传闻口吻**的天下日志：只播报“最近发生了什么”，不做精确战报，也不替代战报 / 事件列表。前端在「天下」页与「总览」计略卡片各展示一份。

### 20.2 数据结构

```json
{
  "visible_at": 1795428143,
  "text": "⚔️「狂沙战殿」以边境战争为名，向「血祭教」宣战！",
  "kind": "war",
  "tick": 1234
}
```

| 字段 | 说明 |
|---|---|
| `visible_at` | 真实时间戳；未到时间不展示（支持延迟播报） |
| `text` | 传闻正文，含 emoji 与势力名 |
| `kind` | `war` / `peace` / `battle` / `event` / `season` / `plot` |
| `tick` | 写入时的世界 tick，便于排查与前端排序 |

### 20.3 写入点

| 时机 | `kind` | 文案示例 |
|---|---|---|
| NPC 被判定进入 `hostile`（首次，带警告冷却） | `war` | 「A」厉兵秣马，兵锋似指「B」。 |
| NPC 正式宣战 | `war` | ⚔️「A」以边境战争为名，向「B」宣战！ |
| NPC 向玩家遣使求和 | `peace` | 🕊️「A」遣使向「B」求和，愿休兵止戈。 |
| NPC↔NPC 停战达成 | `peace` | 🕊️「A」与「B」罢兵休战，边境重归平静。 |
| 据点易主 | `battle` | 🏴「落霞城」易主，归入新主旗下。 |
| 事件开始 / 结束（对比上一 tick 活跃事件行） | `event` | 「血月」之兆忽现 / 渐息。 |
| 既有计略流言 | `plot` | 沿用原 `world.data.winds` 写入 |

> 事件风声首次接入时只记录当前活跃事件基线，不补发历史，避免升级瞬间刷屏。

### 20.4 展示与清理

| 项 | 默认 |
|---|---|
| `npc.wind.enabled` | `true` |
| `npc.wind.keep` | 40 条（`world.data.winds` 上限） |
| `npc.wind.panel_limit` | 14 条（「天下」页展示） |
| `npc.wind.visible_hours` | 72 小时（配置预留；GC 仍按 7 天兜底） |
| `npc.wind.default_delay_hours` | 0（预留延迟播报） |
| GC | `_gc_world_data` 删除 `visible_at + 7 天 < now` 的条目，并按 `keep` 截断 |

### 20.5 前端

- 「天下」页按时间**倒序**展示，带 `【战云】/【休兵】/【战报】/【异象】/【流言】` 前缀。
- 「总览」页计略卡片保留风声摘要，兼容旧字符串数组与新的对象数组。
- 风声不提玩家精确兵力、资源与具体战损，避免绕过战争迷雾。
