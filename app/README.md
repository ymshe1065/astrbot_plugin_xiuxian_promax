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

本机已装好命令行 Android SDK（`C:/Android/Sdk`）和 JDK 21，直接跑：

```bash
cd app/android
export ANDROID_HOME="/c/Android/Sdk"
export JAVA_HOME="/c/use/jdk/jdk21"     # 必须 JDK 21（Capacitor 8 要求，17 会报错）
./gradlew assembleDebug --no-daemon
```

产物位置（固定不变）：

```
app/android/app/build/outputs/apk/debug/app-debug.apk
```

> 首次构建会下载 Gradle 发行版和依赖，需几分钟；之后增量构建很快（约 1 分钟）。

### 方式 B：云端打包（GitHub Actions，本机无需装 SDK）

1. 推代码到 GitHub
2. 仓库 → Actions → "Build Android APK" → Run workflow
3. 构建完成后在 Artifacts 下载 `feiyu-xiuxian-android`（内含 `app-debug.apk`）

> 注意：workflow 文件必须存在于仓库**默认分支（main）**上才会出现在 Actions 页面。

---

## debug 版本说明（重要）

当前产出的是 **debug 版本**，特点与限制：

| 项目 | debug 版 | release 版 |
|------|---------|-----------|
| 能否安装使用 | ✅ 能 | ✅ 能 |
| 性能/体积 | 未优化，偏大（约 6MB） | 混淆+压缩，更小更快 |
| 代码混淆保护 | ❌ 无 | ✅ 有（R8 混淆） |
| 是否签名 | 用 debug 签名（可安装） | 需正式签名 |
| 适用场景 | **自用/内测/开发调试** | 正式分发、上架商店 |

**结论：现在这个 debug 包自己装着玩、给群友内测完全够用。** 如果以后要正式对外分发或上架，需要出 release 签名版（需配置签名密钥），可另行处理。

出 release 版的命令（需先配签名，见「release 打包」小节）：

```bash
./gradlew assembleRelease
```

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
- ✅ 返回键：优先回退网页历史，到顶才退出
- ✅ 启动屏：浅蓝底 + 头像居中
- ✅ 登录态：WebView 自动持久化 Cookie，无需额外处理
