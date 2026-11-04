#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# 本地真实链路启停脚本：AstrBot 4.29.x + 本插件 webroom 平台
#
# 运行时目录结构（固定路径，首次由人工/脚本准备）：
#   $ASTRBOT_RUNTIME/python   独立 CPython 3.12
#   $ASTRBOT_RUNTIME/venv     AstrBot 运行环境
#   $ASTRBOT_RUNTIME/data     AstrBot 数据目录（配置/插件/存档/日志）
#
# 用法：
#   scripts/astrbot_local.sh start|stop|restart|status|logs
# 环境变量：
#   ASTRBOT_RUNTIME          默认 ~/.astrbot-runtime
#   ASTRBOT_DASHBOARD_PORT   默认 6199
set -uo pipefail

RUNTIME="${ASTRBOT_RUNTIME:-$HOME/.astrbot-runtime}"
DASH_PORT="${ASTRBOT_DASHBOARD_PORT:-6199}"
ROOM_PORT="${ASTRBOT_ROOM_PORT:-6198}"
LOG="$RUNTIME/astrbot.log"
ASTROBOT="$RUNTIME/venv/bin/astrbot"

pids_on_port() { lsof -ti ":$1" -sTCP:LISTEN 2>/dev/null; }

status() {
  local d r
  d="$(pids_on_port "$DASH_PORT")"
  r="$(pids_on_port "$ROOM_PORT")"
  if [[ -n "$d" ]]; then
    echo "AstrBot   : 运行中 (PID $d)  面板 http://127.0.0.1:$DASH_PORT"
  else
    echo "AstrBot   : 未运行"
  fi
  if [[ -n "$r" ]]; then
    echo "聊天室    : 运行中 (PID $r)  http://127.0.0.1:$ROOM_PORT/"
  else
    echo "聊天室    : 未运行"
  fi
}

start() {
  if [[ ! -x "$ASTROBOT" ]]; then
    echo "未找到 $ASTROBOT，请先准备运行时（见 docs 路线B 4.6/4.7）" >&2
    return 1
  fi
  if [[ -n "$(pids_on_port "$DASH_PORT")" ]]; then
    echo "AstrBot 已在运行，跳过启动"
    status
    return 0
  fi
  # AstrBot 以 cwd 作为根目录，必须 cd 到 runtime
  ( cd "$RUNTIME" && nohup "$ASTROBOT" run -p "$DASH_PORT" >> "$LOG" 2>&1 & )
  for _ in $(seq 1 40); do
    sleep 1
    if [[ -n "$(pids_on_port "$DASH_PORT")" ]]; then
      echo "已启动，日志：$LOG"
      status
      return 0
    fi
  done
  echo "启动超时，请查看 $LOG" >&2
  return 1
}

stop() {
  local p
  for p in $(pids_on_port "$DASH_PORT"); do
    kill "$p" && echo "已停止 AstrBot (PID $p)"
  done
  for _ in $(seq 1 20); do
    [[ -z "$(pids_on_port "$DASH_PORT")" ]] && break
    sleep 1
  done
  for p in $(pids_on_port "$ROOM_PORT"); do
    kill "$p" && echo "已停止残留聊天室服务 (PID $p)"
  done
  [[ -z "$(pids_on_port "$DASH_PORT")" ]] || { echo "仍在运行，可 kill -9 强制结束" >&2; return 1; }
}

case "${1:-status}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  logs)    tail -n "${2:-60}" "$LOG" ;;
  *)
    echo "用法: $0 {start|stop|restart|status|logs [行数]}" >&2
    exit 1
    ;;
esac
