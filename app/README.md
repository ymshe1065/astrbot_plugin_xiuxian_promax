# 肥鱼修仙 - 手机端 App（WebView 壳）

基于 [Capacitor](https://capacitorjs.com) 的 WebView 壳 App，加载已部署的网页版修仙聊天室，包装成可安装的原生应用（Android + iOS）。

## 原理

```
┌─────────────────────────────┐
│  原生 App 壳（Capacitor）     │
│  ┌───────────────────────┐  │
│  │  WebView 全屏加载        │  │
│  │  http://8.130.86.71:8888/│ │  ← 已部署的网页版
│  └───────────────────────┘  │
└─────────────────────────────┘
```

前端零改动、后端零改动，只套一层壳。

## 目录结构

```
app/
├── capacitor.config.json      # 核心配置：线上地址、应用名、包名、明文放行
├── package.json
├── www/                        # 壳的兜底页面（正常会被重定向到线上地址）
├── android/                    # Android 原生工程
├── ios/                        # iOS 原生工程（需 Mac 打包）
├── _gen_icons.py               # 图标生成脚本（一次性）
├── _gen_splash.py              # 启动屏生成脚本（一次性）
└── ../.github/workflows/build-android.yml  # 云端打包工作流（备用）
```

---

## 如何修改线上地址（重要）

线上地址在 **`app/capacitor.config.json`** 里的 `server.url` 字段：

```json
{
  "server": {
    "url": "http://8.130.86.71:8888/",   // ← 改这里，就是换后端地址
    "cleartext": true,
    "androidScheme": "https"
  }
}
```

**改完之后必须重新打包才会生效**（改配置文件不会自动更新已装好的 App）：

1. 改 `capacitor.config.json` 的 `server.url`
2. 重新打包（见下方「本机打包」）
3. 卸载旧 App，安装新 apk

> 其他关键配置：`appName`（应用名）、`appId`（包名）、`server.cleartext`（明文 HTTP 放行，地址是 http:// 时必须为 true）。

---

## 如何打包

### 方式 A：本机命令行打包（推荐，已配置好环境）

本机已装好命令行 Android SDK（`C:/Android/Sdk`）和 JDK，签名也已配置，直接跑：

```bash
cd app/android
./gradlew assembleRelease
```

产物位置（固定不变）：

```
app/android/app/build/outputs/apk/release/app-release.apk
```

> 首次构建会下载 Gradle 发行版和依赖，需几分钟；之后增量构建很快（约 1 分钟）。

### 方式 B：云端打包（GitHub Actions，本机无需装 SDK）

1. 推代码到 GitHub
2. 仓库 → Actions → "Build Android APK" → Run workflow
3. 构建完成后在 Artifacts 下载 `feiyu-xiuxian-android`（内含 `app-debug.apk`）

> 注意：workflow 文件必须存在于仓库**默认分支（main）**上才会出现在 Actions 页面。
> 云端打包拿不到本机的 keystore，产出的是 debug 签名包，**不能覆盖安装 release 包**。

---

## 签名（重要，别丢）

签名凭据在 `app/android/keystore.properties`，密钥库本体是 `app/android/app/feiyu-release.jks`。

```properties
storeFile=app/feiyu-release.jks
storePassword=feiyuxiuxian2026
keyAlias=feiyu
keyPassword=feiyuxiuxian2026
```

- **这份签名必须长期固定**：应用内更新的前提是「新包能覆盖老包」，签名一变，用户就只能卸载重装。
- `debug` 与 `release` 共用同一份签名，本机测试包可以直接覆盖正式包。
- 两个文件都随仓库提交，换机器 clone 下来就能打包；单独备份一份 keystore 更稳妥。

> 历史包袱：最早的「状态栏修复」包（versionCode 1）是用 Android Debug 签名打的，从它升级必须**卸载一次**；
> 之后所有的包都用 `feiyu` 签名，可以一直覆盖升级。

---

## 版本号规则

| 字段 | 规则 |
| --- | --- |
| `versionName` | 测试期走 `0.x`，**从 `0.1` 起**，每发一个包 +0.1（0.1 → 0.2 → 0.3 …）；正式对外发布才转 `1.0.0` |
| `versionCode` | **只增不减**，每次发版 +1。应用内更新靠它判断新旧，Android 也不允许覆盖安装更小的值 |

所以会出现「versionName 看着小、versionCode 一直在涨」的情况，这是故意的：
版本名面向用户表达阶段（0.x = 测试版），versionCode 服务安装与升级判断，二者解耦。

---

## 应用内更新

页面本身走远程 URL，改 `webchat_room/static/` 即刻生效，不需要更新 App。
只有原生层（Java、权限、AndroidManifest）改了才必须换 APK —— 这条链路服务的就是它。

**流程**

```
App 启动 → GET /api/app/version → 比对 versionCode
        → 有新版本：弹窗（版本号 / 体积 / 更新说明）
        → 站内下载到 cacheDir/updates/ → 校验 sha256 → 拉起系统安装器
        → 用户点「安装」完成升级
```

**发版三步**

1. 改 `app/android/app/build.gradle` 里的 `versionCode`（必须 +1）和 `versionName`（测试期 +0.1）
2. `cd app/android && ./gradlew assembleRelease`
3. 回到插件根目录执行：

```bash
python scripts/release_apk.py --note "本次更新了什么" --note "可以写多条"
```

脚本会读 gradle 里的版本号、复制 APK 到 `<AstrBot 数据目录>/webroom/apk/`、
算出 size 与 sha256，并重写 `webroom/app_release.json`。之后旧版 App 启动就会收到更新提示。

**清单字段**

```json
{
  "versionCode": 3,              // 与客户端比对，必须比旧包大
  "versionName": "0.1",
  "file": "feiyu-0.1.apk",
  "size": 5351842,
  "sha256": "…",                 // 下载完校验，对不上就丢弃
  "changelog": ["修复状态栏遮挡"],
  "minVersionCode": 0            // 低于此版本强制更新；0 = 只提示可稍后
}
```

**几个约束**

- Android 不允许第三方应用静默安装，最后一步「安装」必须用户点确认；
- 首次更新时系统会引导开启「安装未知应用」权限，之后不再询问；
- 分发用 HTTP 明文，所以下载走 App 内 `HttpURLConnection`，不用系统的 `DownloadManager`
  （后者跑在系统进程里，明文策略与 App 的 `usesCleartextTraffic` 不一致）；
- `/api/app/version` 与 `/download/*` 由 `webchat_room/room_server.py` 提供，
  部署时记得把 `webroom/apk/` 和 `app_release.json` 一起放到服务器的 AstrBot 数据目录。

---

## iOS 打包（需 Mac）

```bash
cd app
npm install
npx cap sync ios
npx cap open ios       # 打开 Xcode
# 在 Xcode 里选真机/模拟器运行，或 Archive 上架
```

> ⚠️ 注意：iOS 上架 App Store 要求 HTTPS。当前后端是 `http://`，只适合 TestFlight 内测/自用分发。要上架需先把后端改成 HTTPS 域名。

---

## 已实现的功能

- ✅ WebView 全屏加载线上网页版
- ✅ 应用名「肥鱼修仙」+ 二次元头像图标（复用 `whale_logo.png`）
- ✅ Android 明文流量放行（`usesCleartextTraffic`）
- ✅ 状态栏 / 导航栏 / 刘海 insets 适配（Android 15 强制 edge-to-edge 后的必需处理）
- ✅ 返回键：优先回退网页历史，到顶才退出
- ✅ 启动屏：浅蓝底 + 头像居中
- ✅ 登录态：WebView 自动持久化 Cookie，无需额外处理
- ✅ 固定 release 签名 + 应用内更新（站内下载 APK、sha256 校验、拉起系统安装器）
