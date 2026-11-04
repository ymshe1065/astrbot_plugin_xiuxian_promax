# 网页聊天室路线 B 实现方案：自建聊天室 + 自定义平台适配器

> 分支：`feat/web-chatroom` · 2026-11-03
> 前置：阶段 0 实测已完成（见《网页聊天室路线A方案.md》与工作日志），路线 A（原生 WebChat）因**身份串号**（所有网页用户 sender_id 均为 `astrbot`）与**无跨客户端实时推送**判定不可行。

---

## 一、目标与非目标

**目标**
1. 多名玩家在网页房间内实时聊天（WebSocket，毫秒级互见）；
2. 修仙插件**零改动**：`get_sender_id()` 拿到真实玩家名、`get_group_id()` 拿到房间 ID、世界事件广播（Boss/秘境/拍卖/宗门/坊市）原样透传到网页；
3. 与 QQ 端**共用同一套玩家存档**（同一个 SQLite），同指令、同玩法。

**非目标（本期不做）**
- 图片/语音/文件等富媒体消息（第一版纯文本 + 轻量 markdown，留扩展点）；
- QQ 消息与网页房间的互通（两边是两个独立"群"）；
- 聊天室账号与 QQ 账号绑定（网页玩家是独立玩家池）。

---

## 二、总体架构

```
浏览器（玩家A / 玩家B / …）
   │  HTTP :6198 静态页 + 注册/登录
   │  WebSocket :6198/ws/{room_id}
   ▼
┌─────────────────────────────────────────────────┐
│ 房间服务 room_server（插件内置，FastAPI+WS）      │
│  · 玩家账号/房间/在线连接管理                     │
│  · 消息持久化（room_messages 表）                 │
│  · 收到玩家消息 → 交给适配器 commit_event          │
└──────────────┬──────────────────────────────────┘
               │ 构造 AstrBotMessage（群消息）
               ▼
┌─────────────────────────────────────────────────┐
│ 自定义平台适配器 webroom（插件内注册）             │
│  · convert_message: sender=玩家名, group_id=房间  │
│  · send(): event.send() → WS 广播回房间所有人     │
│  · send_by_session(): 世界事件广播 → 同样广播房间  │
└──────────────┬──────────────────────────────────┘
               │ AstrBot 流水线（指令解析/唤醒词）
               ▼
┌─────────────────────────────────────────────────┐
│ 修仙插件 main.py（零改动）                        │
│  300+ @filter.command handler / 定时任务 / 广播   │
└─────────────────────────────────────────────────┘
```

**一条消息的完整旅程（玩家发 `/签到`）：**
1. 玩家 A 在网页发 `/签到` → WS 到 room_server；
2. room_server 落库（聊天记录）、构造 `AstrBotMessage` 交给适配器 `commit_event`；
3. AstrBot 流水线解析指令 → 调修仙插件 handler；
4. handler 里 `event.get_sender_id()` = `玩家A的网页用户名`，存档/查库一切如常；
5. handler `yield` 的消息 → `WebroomEvent.send()` → 适配器查找房间在线连接 → **WS 广播给房间所有人**（不是只回 A）；
6. 每天定时任务里的世界播报 `context.send_message("webroom:GroupMessage:{room_id}", ...)` → 走 `send_by_session()` → 同样广播房间。

---

## 三、可行性依据（已核实 AstrBot 4.29.4 源码）

| # | 机制 | 源码位置 | 结论 |
|---|------|----------|------|
| 1 | 插件先于平台加载 | `core/core_lifecycle.py`：`plugin_manager.reload()`(L267) → `platform_manager.initialize()`(L296) | ✅ 插件可提前注册适配器 |
| 2 | 适配器实例化只查全局注册表 | `core/platform/manager.py` L236：`platform_cls_map[platform_config["type"]]`，内置 `case` 分支只是负责 import | ✅ 插件内调用 `register_platform_adapter("webroom", ...)` 即可被配置实例化 |
| 3 | 身份取值来源 | `get_sender_id()` ← `message_obj.sender.user_id`；`get_group_id()` ← `message_obj.group_id` | ✅ 适配器构造消息时直接注入，插件无感 |
| 4 | UMO 广播格式 | `MessageType.GROUP_MESSAGE = "GroupMessage"`；插件广播 `f"{platform_name}:GroupMessage:{group_id}"` | ✅ 适配器名 + session_id 设计与广播格式天然吻合 |

**UMO 设计（本方案的核心映射表）：**

| 概念 | 值 | 说明 |
|------|-----|------|
| 平台名/适配器 ID | `webroom` | UMO 第一段 |
| 消息类型 | `GroupMessage` | UMO 第二段，房间即群 |
| session_id | `{room_id}` | UMO 第三段 = 房间 ID（如 `room_main`） |
| 完整 UMO | `webroom:GroupMessage:room_main` | 与插件 10+ 处世界播报格式完全匹配 |
| sender.user_id | 网页玩家用户名 | `get_sender_id()` 的返回值 |
| message_obj.group_id | 房间 ID | `get_group_id()` 的返回值 → `_check_access` 白名单直接可用 |

---

## 四、组件设计

### 4.1 自定义适配器 `webroom_adapter.py`（插件内新增）

```python
# astrbot_plugin_xiuxian_promax/webchat_room/webroom_adapter.py
@register_platform_adapter(
    "webroom", "修仙网页聊天室",
    default_config_tmpl={"host": "127.0.0.1", "port": 6198, "rooms": ["room_main"]},
)
class WebroomAdapter(Platform):
    # 构造签名与内置适配器一致: (platform_config, platform_settings, event_queue)
    # run(): 启动房间服务(FastAPI+WS, 独立端口 6198)
    # convert_message(): 构造 AstrBotMessage
    #   abm.type = MessageType.GROUP_MESSAGE
    #   abm.session_id = room_id          # UMO 第三段
    #   abm.group_id = room_id            # get_group_id()
    #   abm.sender = MessageMember(玩家名, 玩家名)   # get_sender_id()
    # send(): event.send() 的落点 → 查房间在线 WS → 广播
    # send_by_session(): 世界播报落点 → 同样广播 + 离线落库
```

要点：
- `send()` 与 `send_by_session()` 都广播给**房间全部在线连接**（与 QQ 群语义一致）；
- 机器人回复同时落到 room_messages 表，离线玩家上线可拉历史；
- `terminate()` 关闭 WS 服务。

### 4.2 房间服务 `room_server.py` + 前端 `static/index.html`

- **协议**（WS JSON）：
  - 上行：`{"action":"msg","text":"..."} / {"action":"join","room":"..."} / {"action":"history","before":ts}`
  - 下行：`{"type":"chat","from":"玩家A","text":"..."} / {"type":"bot","text":"..."} / {"type":"sys","text":"..."} / {"type":"online",[...]}`
- **账号**：第一版 = 用户名 + 密码（PBKDF2），存 `webroom_users` 表；注册即建修仙存档（玩家首次发指令时插件自动建档，无需迁移）。
- **在线管理**：`room_id → {ws: player_name}` 字典；断线踢出；重复登录顶号。
- **前端**：单页 HTML + 原生 JS（无构建链），房间侧栏 + 消息流 + 输入框；bot 消息与玩家消息样式区分；历史分页拉取。

### 4.3 数据表（聊天室自有，与插件存档库分离）

- `webroom_users(username, pwd_hash, created_at)`
- `webroom_rooms(room_id, name, whitelist)` —— whitelist 对接插件 `_check_access` 的群白名单
- `webroom_messages(id, room_id, sender, kind(player/bot/sys), text, ts)`

### 4.4 插件侧改动清单（最小化）

| 文件 | 改动 |
|------|------|
| `main.py` | `on_load` 中调用 `webchat_room.bootstrap()`（注册适配器 + 启服务），约 5 行 |
| `webchat_room/` | 新增目录：adapter / room_server / room_store / static |
| 其余 300+ handler | **零改动** |
| 白名单 | 后台配置里把房间 ID 加进群白名单（或 `_check_access` 空则放行的现有逻辑直接兼容） |

### 4.5 启用方式

后台「平台适配器」新增 `webroom` 类型实例（WebUI 会因 `config_metadata` 渲染表单：host/port/rooms），启用即生效；QQ/OneBot 照常并行运行。

### 4.6 本地预览（无需 AstrBot）

只想调前端（账号登录、在线列表、消息气泡、看板、卡片渲染）时，不必安装完整 AstrBot：

```bash
python3 -m venv .venv
.venv/bin/pip install "aiohttp>=3.9"

.venv/bin/python scripts/dev_webroom.py            # 仅房间服务，浏览器开 http://127.0.0.1:6198/
.venv/bin/python scripts/dev_webroom.py --demo     # 附带演示机器人回合，并自动创建演示账号
.venv/bin/python scripts/dev_webroom.py --port 6200 --host 0.0.0.0
```

`--demo` 启动时会自动创建（已存在则跳过）演示账号 **`demo / demo123456`**（昵称「演示道友」），
在登录页直接用即可；不加 `--demo` 则在页面自行注册。

`scripts/dev_webroom.py` 用轻量桩替掉 `astrbot.api.logger` 与 `get_astrbot_data_path`，
因此**不加载修仙插件、也不产生真实机器人回复**；真实链路仍需在 AstrBot 后台启用 `webroom` 平台实例。

> 房间服务自 v3 起改成**账号制**：单一主聊天室，前端不再填房间号；WebSocket 走 `/ws?token=<登录 token>`。

| 依赖 | 用途 | 说明 |
|------|------|------|
| `aiohttp` | room_server 的 HTTP + WebSocket | 已写入 `requirements.txt`；AstrBot 自身也依赖它 |
| `astrbot` | 适配器 `webroom_adapter.py` | 需 Python 3.10+，仅真实链路需要 |
| `aiosqlite` | 插件存档库 | AstrBot 环境自带 |

### 4.7 真实链路本地运行（AstrBot + webroom）

前端预览只能验证 UI；要看**真实机器人回复**（指令解析、存档、图片渲染），需要完整 AstrBot。

**固定路径约定**（已在本机落地，实测占用 ≈ 706 MB）：

```
~/.astrbot-runtime/
├── python/   独立 CPython 3.12.16（免编译，24 MB 包 → 73 MB）
├── venv/     AstrBot 4.29.4 运行环境（633 MB，含 65 个依赖）
└── data/     AstrBot 根数据目录
    ├── cmd_config.json                 # platform 段需含 webroom 实例
    ├── config/astrbot_plugin_xiuxian_promax_config.json
    ├── plugins/astrbot_plugin_xiuxian_promax -> 本仓库（软链，改代码即时生效）
    ├── plugin_data/astrbot_plugin_xiuxian_promax/   # 玩家存档 sqlite
    └── webroom/imgs/                   # 机器人回复的图片
```

**必备配置**

`data/cmd_config.json` 的 `platform` 段：

```json
[{ "id": "webroom", "type": "webroom", "enable": true, "host": "127.0.0.1", "port": 6198 }]
```

`data/config/astrbot_plugin_xiuxian_promax_config.json` 的 `ACCESS_CONTROL`（房间 ID 即群号，不填则世界播报会跳过）：

```json
{ "WHITELIST_GROUPS": ["room_main"], "BROADCAST_GROUPS": ["room_main"] }
```

**启停**

```bash
scripts/astrbot_local.sh start     # 后台启动，等端口就绪
scripts/astrbot_local.sh status    # 面板 6199 / 聊天室 6198 状态
scripts/astrbot_local.sh logs 80   # 看日志
scripts/astrbot_local.sh stop
```

> `scripts/astrbot_local.sh` 依赖 `lsof` 与 `venv/bin`，**仅适用于 Linux / macOS**。
> Windows 下等价命令：`$env:PYTHONPATH=""; cd <运行时目录>; astrbot run`（后台运行，改代码后需重启）。

浏览器打开 <http://127.0.0.1:6198/>，先在**登录页注册账号**（或登录已有账号）即可进入主聊天室；
前端不再填房间号，服务端固定使用 `room_main`。
面板端口以 `ASTRBOT_DASHBOARD_PORT` 为准（脚本默认 6199，本机实测环境为 6185）；
初始账号 `astrbot`，初始密码见首次启动日志，可用 `astrbot password` 重置。

**已实测通过的链路**：`/修仙帮助`（返回帮助图，落盘 `data/webroom/imgs` 后经 `/media/<file>` 访问）→ `/我要修仙 灵修` → `/选择灵根 1` → `/签到` → `/我的信息`，均为真实的插件逻辑与存档写库。



---

## 五、里程碑

| 阶段 | 内容 | 验收标准 |
|------|------|----------|
| **M1 核心链路**（先行） | 适配器骨架 + 极简 WS echo 服务 + 构造 AstrBotMessage | 两个 ws 客户端连同一房间，A 发 `/帮助`，两人都收到插件回复；`get_sender_id()` 返回各自用户名 |
| **M2 聊天室 UI** | 注册登录、多房间、历史记录、在线列表 | 浏览器多人互聊无刷新可见；世界播报（手动触发一次测试广播）进网页 |
| **M3 权限与存档** | 房间白名单对接、重复登录顶号、玩家首次指令建档 | 白名单外房间指令被拒；玩家数据落 SQLite 与 QQ 端同构 |
| **M4 打磨** | 图片消息(降级为链接)、@提醒、掉线重连、静态资源缓存 | 长跑 24h 无泄漏；断线自动重连恢复上下文 |

**M1 是风险收敛点**：只验证「插件注册适配器 → 消息进流水线 → 广播回房间」这一条链，半天内可出结果；不通过则方案降级为「独立进程 + OpenAPI 轮询」（备选，体验差）。

---

## 六、风险与对策

| 风险 | 等级 | 对策 |
|------|------|------|
| `register_platform_adapter` 是 core 内部 API，AstrBot 升级可能变动 | 中 | bootstrap 里做 try/except 兼容层，注册失败降级为日志告警而非崩溃；锁定 AstrBot 4.29.x |
| 适配器构造函数签名/Platform 基类行为随版本变化 | 低 | 与内置 webchat_adapter 保持同构（已逐行对照 4.29.4） |
| 定时任务广播时房间无人在线 | 低 | send_by_session 无人在线时仅落库（离线消息，上线拉取）——与 webchat 的 proactive 处理同构 |
| WS 服务与 AstrBot 事件循环耦合 | 中 | 房间服务跑在同一 asyncio loop（uvicorn 注入现有 loop），不另起线程，避免跨线程操作 event_queue |
| 两人同时发指令导致插件并发写 SQLite | 中 | 插件本身已在 QQ 群多人场景下运行过，`aiosqlite` 串行队列天然支持；必要时加房间级指令锁 |
| 玩家名即 sender_id 与 QQ 号混淆 | 低 | 网页玩家名加前缀（如 `web_阿蓝`）入库隔离，排行榜可按前缀分榜 |

---

## 七、下一步

按 M1 开工：新建 `webchat_room/` 目录，实现适配器骨架 + WS echo + `/帮助` 指令打通，验收后提交。
