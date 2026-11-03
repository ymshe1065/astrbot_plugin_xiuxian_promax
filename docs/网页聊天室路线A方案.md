# 网页聊天室方案（路线 A：复用 AstrBot WebChat 平台）

> 分支：`feat/web-chatroom`
> 目标：把 QQ 群里的修仙机器人搬到网页端，解决刷屏问题，同时保留「多人实时群聊」体验。
> 原则：**优先复用**，尽量少改插件游戏逻辑。

---

## 1. 背景与目标

### 1.1 现状
- 项目 `astrbot_plugin_xiuxian_promax` 是基于 **AstrBot** 框架的 Star 插件，功能完整（300+ handler，修仙/炼丹/符箓/秘境/基金股票/宗门/拍卖/宠物等）。
- 当前跑在 QQ 群，机器人消息刷屏，干扰了原本的聊天用途。
- 数据用 SQLite 持久化，玩家身份用 `event.get_sender_id()` 区分。

### 1.2 目标
- 提供网页端聊天室，玩家在网页里聊天 + 玩修仙游戏。
- 多人能进同一个「房间」实时群聊，机器人也在房间里响应。
- 游戏逻辑、数据、指令**尽量不改**。

---

## 2. 关键结论：AstrBot 自带 WebChat 平台

AstrBot 原生内置了 **WebChat** 平台适配器（源码见 AstrBot 仓库
`astrbot/core/platform/sources/webchat/` 下的 `webchat_adapter.py` /
`webchat_event.py` / `webchat_queue_mgr.py`），以及 Vue3 前端
（`dashboard/src/components/chat/`）。

能力：
- 浏览器内聊天 UI（流式 SSE/WebSocket、图片、语音、文件、markdown 渲染）。
- FastAPI 后端 + `WebChatAdapter` 把网页消息接入 AstrBot 的 `AstrMessageEvent` 消息总线。
- 意味着：**只要把 WebChat 平台开启，你的修仙插件理论上无需改游戏逻辑即可在网页里被调用**。

---

## 3. 路线 A 的适配点分析（已核对插件源码）

以下是插件里依赖「平台/群聊」概念、需要在 WebChat 下验证或适配的关键点。

### 3.1 玩家身份：`event.get_sender_id()`
- 插件大量用 `str(event.get_sender_id())` 作为玩家唯一 ID（存库、查玩家、白名单、Boss 管理）。
- **WebChat 下返回什么？** 需要实测确认它能否稳定区分不同网页用户。
- 若 WebChat 的 sender_id 是会话级而非用户级，则多人同会话会出现「身份串号」风险 → 这是路线 A 能否成立的第一道关卡。

### 3.2 群组判定：`event.get_group_id()` / `_check_access`
- `_check_access` 用 `event.get_group_id()` 做白名单过滤；白名单为空则放行（默认全开放）。
- WebChat 下 `get_group_id()` 大概率返回空或会话 ID。
  - 若为空 → 白名单失效（等于全开放），对网页场景**可接受**（网页本身可自行做访问控制）。
  - 若要按「房间」隔离，需把 WebChat 的会话 ID 映射成 group_id。

### 3.3 定时广播：`{platform_name}:GroupMessage:{group_id}`
- 插件有大量 `context.send_message(f"{platform_name}:GroupMessage:{group_id}", ...)` 的定时播报
  （Boss 刷新、秘境、拍卖会、宗门事件、坊市 NPC 等，约 10+ 处，见 main.py 830/1057/1079/1110/1166/1254 行附近）。
- 这些用 `context.platform_manager.get_insts()` 遍历「所有平台实例」，并拼 UMO 字符串发群。
- **WebChat 适配风险点**：WebChat 平台的 UMO 格式、以及 `get_insts()` 是否包含 WebChat 实例，都需要实测。
  - 若 WebChat 不在 `get_insts()` 里，则这些广播不会出现在网页端 → 网页玩家会「漏掉」世界事件。
  - 若 UMO 格式不匹配，广播会静默失败。

### 3.4 消息来源标记：`unified_msg_origin`
- 插件在 1580、3145 行用 `getattr(event, "unified_msg_origin", "")` 记录消息来源（可能用于跨平台玩家关联）。
- WebChat 下该字段的取值需要确认，避免和 QQ 来源混淆。

---

## 4. 实施步骤（分阶段，先验证后开发）

### 阶段 0：可行性验证（约 0.5~1 天，零代码）
1. 在 AstrBot 后台开启 WebChat 平台，确认网页聊天可用。
2. 用两个浏览器/两个账号进入**同一个会话**，验证：
   - 能否互相看到对方消息（即「多人实时群聊」是否成立）。
   - `get_sender_id()` 是否能区分两个用户。
   - `get_group_id()` 返回什么。
3. 触发一次世界事件（如 Boss 刷新），观察广播是否会出现在网页端（验证 3.3 的风险点）。

**决策点**：
- 若「多人同会话 + sender_id 可区分」成立 → 继续路线 A 落地。
- 若不成立 → 转入路线 B（自建聊天室 + 消息桥），见 `网页聊天室路线B方案.md`（待写）。

### 阶段 1：身份与群组适配（若阶段 0 发现不匹配）
- 若 WebChat sender_id 不能区分用户，在插件内增加一层「WebChat 用户 → 稳定玩家 ID」的映射
  （例如读取 WebChat 事件里的用户标识字段，或在插件侧维护 nickname → user_id 的绑定表）。
- 若需要按房间隔离，把 WebChat 会话 ID 映射为 `get_group_id()` 的返回值。

### 阶段 2：广播适配（若阶段 0 发现广播缺失）
- 为定时广播增加「WebChat 目标」：
  - 方案 A：让 `get_insts()` 能返回 WebChat 实例（可能只需在 AstrBot 侧配置，或插件侧兼容 WebChat 的 UMO 格式）。
  - 方案 B：插件侧单独维护一个「WebChat 广播队列」，把世界事件推送到网页端（复用 `webchat_queue_mgr` 或自建推送通道）。

### 阶段 3：网页端体验增强（可选，视需求）
- 白名单/访问控制：网页聊天室需要登录或邀请码（WebChat 是否有该能力需确认）。
- 房间化 UI：把「群聊房间」概念做成网页里的频道/房间列表。

---

## 5. 需要向用户/环境确认的问题清单

- [ ] AstrBot 版本号（WebChat 能力随版本变化）。
- [ ] WebChat 是否已在当前部署中开启、入口 URL 是什么。
- [ ] 网页端是否需要登录/权限控制，还是公开访问。
- [ ] 是否接受「网页端和 QQ 群共用同一份玩家数据」，还是网页端要独立数据。

---

## 6. 风险与备选

| 风险 | 影响 | 缓解 |
|------|------|------|
| WebChat 不支持多人同会话 | 路线 A 不成立 | 转路线 B（自建聊天室 + 消息桥） |
| WebChat sender_id 不区分用户 | 玩家数据串号 | 插件侧加用户映射层（阶段 1） |
| 世界事件广播不进网页 | 网页玩家漏事件 | 广播适配（阶段 2） |
| WebChat 版本与插件不兼容 | 需要升级 AstrBot 或降级插件 | 锁定 AstrBot 版本并回归测试 |

---

## 7. 结论

路线 A 是最低成本路径，核心动作是**阶段 0 的实测验证**。在确认「多人同会话 + 身份可区分」之前，
不建议写任何适配代码。验证通过后，改造成本集中在「身份映射」和「广播适配」两处，游戏逻辑基本不动。
