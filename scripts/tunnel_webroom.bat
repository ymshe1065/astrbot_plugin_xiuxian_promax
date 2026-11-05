@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

set PORT=%~1
if "%PORT%"=="" set PORT=6198

set CLOUDFLARED=%ProgramFiles(x86)%\cloudflared\cloudflared.exe
if not exist "%CLOUDFLARED%" set CLOUDFLARED=cloudflared

echo ============================================================
echo   Cloudflare 隧道 -^> http://127.0.0.1:%PORT%
echo.
echo   稍等 5-10 秒，下面会打印一行
echo     https://xxxx-xxxx-xxxx.trycloudflare.com
echo   把那行网址复制发给朋友即可（对方不用装任何东西）。
echo.
echo   关闭本窗口 = 立刻断开。
echo   前提：AstrBot 必须正在运行，且网页聊天室监听在 %PORT%。
echo ============================================================
echo.

"%CLOUDFLARED%" tunnel --url http://127.0.0.1:%PORT% --no-autoupdate

echo.
echo 隧道已结束。
pause
