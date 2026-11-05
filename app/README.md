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
└── ../.github/workflows/build-android.yml  # 云端打包工作流
```

## 关键配置

`capacitor.config.json` 里这几项最重要：

| 字段 | 值 | 说明 |
|------|-----|------|
| `appId` | `com.feiyuxiuxian.app` | 应用包名（上架需唯一） |
| `appName` | `肥鱼修仙` | 图标下的应用名 |
| `server.url` | `http://8.130.86.71:8888/` | **线上网页地址，改了这里就是换后端** |
| `server.cleartext` | `true` | 允许明文 HTTP（Android 9+ 需要） |

> 要改线上地址：只需改 `capacitor.config.json` 的 `server.url`，然后重新 `npx cap sync` + 打包。

## 如何打包

### 方式 A：云端打包（推荐，本机无需装 Android SDK）

1. 把项目推到 GitHub（`app/` 目录和 `.github/workflows/` 一起推上去）
2. GitHub 仓库 → Actions → "Build Android APK" → Run workflow
3. 构建完成后，在 Artifacts 下载 `feiyu-xiuxian-android`（里面是 `app-debug.apk`）
4. 把 apk 发到手机安装即可

### 方式 B：本机打包（需装 Android Studio + SDK）

```bash
cd app
npm install
npx cap sync android
npx cap open android    # 打开 Android Studio
# 在 Android Studio 里 Build > Build APK(s)
```

## iOS 打包（需 Mac）

```bash
cd app
npm install
npx cap sync ios
npx cap open ios       # 打开 Xcode
# 在 Xcode 里选真机/模拟器运行，或 Archive 上架
```

> ⚠️ 注意：iOS 上架 App Store 要求 HTTPS。当前后端是 `http://`，只适合 TestFlight 内测/自用分发。要上架需先把后端改成 HTTPS 域名。

## 已实现的功能

- ✅ WebView 全屏加载线上网页版
- ✅ 应用名「肥鱼修仙」+ 二次元头像图标（复用 `whale_logo.png`）
- ✅ Android 明文流量放行（`usesCleartextTraffic`）
- ✅ 返回键：优先回退网页历史，到顶才退出
- ✅ 启动屏：浅蓝底 + 头像居中
- ✅ 登录态：WebView 自动持久化 Cookie，无需额外处理
