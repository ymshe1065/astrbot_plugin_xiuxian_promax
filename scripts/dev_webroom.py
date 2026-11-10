# -*- coding: utf-8 -*-
"""webroom 前端本地预览启动器（开发用，不参与插件运行）。

作用：不依赖完整 AstrBot，只用 aiohttp 把 webchat_room/room_server 拉起来，
方便单独调试 static/ 前端（账号登录、在线列表、消息气泡、看板、卡片渲染）。

用法::

    .venv/bin/python scripts/dev_webroom.py                # 仅房间服务
    .venv/bin/python scripts/dev_webroom.py --demo          # 附带演示机器人回复
    .venv/bin/python scripts/dev_webroom.py --port 6200     # 换端口

说明：本脚本用轻量桩替代 astrbot 的 logger 与数据目录，因此**不会**加载修仙插件、
也不会产生真实机器人回复（加 --demo 时由桩适配器回一条演示文本）。
真实链路请用 AstrBot 后台「平台适配」新增 type=webroom 实例。

房间服务已改为**账号制**（单一主聊天室，无房间号）：首次打开页面先在登录页注册账号，
或直接用 --demo 自动创建并打印的演示账号登录。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_astrbot_stubs() -> None:
    """用最小桩替换 room_server 依赖的 astrbot 组件。"""

    class _Logger:
        def _log(self, level: str, msg: str) -> None:
            print(f"[{level}] {msg}", flush=True)

        def debug(self, msg, *a, **k):
            pass

        def info(self, msg, *a, **k):
            self._log("INFO", msg % a if a else msg)

        def warning(self, msg, *a, **k):
            self._log("WARN", msg % a if a else msg)

        def error(self, msg, *a, **k):
            self._log("ERROR", msg % a if a else msg)

        def exception(self, msg, *a, **k):
            self._log("ERROR", msg % a if a else msg)

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    # 核心模块（core/__init__）会导入 AstrBotConfig，桩必须补齐，
    # 否则本桩被其他测试先装入 sys.modules 时会导致后续导入失败。
    api.AstrBotConfig = dict
    astrbot.api = api

    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")

    data_dir = Path(tempfile.gettempdir()) / "astrbot_webroom_dev"
    data_dir.mkdir(parents=True, exist_ok=True)
    astrbot_path.get_astrbot_data_path = lambda: str(data_dir)

    utils.astrbot_path = astrbot_path
    core.utils = utils
    astrbot.core = core

    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
    sys.modules.setdefault("astrbot.core", core)
    sys.modules.setdefault("astrbot.core.utils", utils)
    sys.modules.setdefault("astrbot.core.utils.astrbot_path", astrbot_path)


class _DemoAdapter:
    """演示用桩适配器：只实现 room_server 会调用到的 on_player_message。"""

    def __init__(self) -> None:
        self._count = 0

    async def on_player_message(
        self,
        room_id: str,
        player_id: str,
        player_name: str,
        text: str,
        req_id: str | None = None,
        private: bool = False,
        want_card: bool = False,
    ) -> None:
        from webchat_room import room_server

        self._count += 1
        await asyncio.sleep(0.15)
        await room_server.broadcast_bot(
            room_id,
            f"【演示机器人】{player_name} 的第 {self._count} 条消息：「{text}」\n"
            f"（本地预览桩，真实回复需在 AstrBot 里启用 webroom 平台）",
        )


async def _main(args: argparse.Namespace) -> None:
    _install_astrbot_stubs()

    from webchat_room import accounts, room_server

    if args.demo:
        room_server.set_adapter(_DemoAdapter())

    await room_server.start(args.host, args.port)

    demo_user = "demo"
    demo_pwd = "demo123456"
    if args.demo:
        # 已存在会返回失败，属于正常情况（本地预览要能反复启动）
        accounts.register(demo_user, demo_pwd, "演示道友")
        my = accounts.get(demo_user) or {}
        demo_pwd_note = f"{demo_user} / {demo_pwd}"
        if my.get("nick"):
            demo_pwd_note += f"（昵称：{my['nick']}）"
    else:
        demo_pwd_note = "页面自行注册（登录页「注册」标签）"

    print("=" * 60, flush=True)
    print("  修仙网页聊天室（本地预览）", flush=True)
    print(f"  打开浏览器访问: http://{args.host}:{args.port}/", flush=True)
    print(f"  演示账号     : {demo_pwd_note}", flush=True)
    print(f"  WebSocket    : ws://{args.host}:{args.port}/ws?token=<登录返回的 token>", flush=True)
    print(f"  演示机器人   : {'开启' if args.demo else '关闭（加 --demo 开启）'}", flush=True)
    print("  Ctrl+C 退出", flush=True)
    print("=" * 60, flush=True)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await room_server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="webroom 前端本地预览")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6198)
    parser.add_argument("--demo", action="store_true", help="启用演示机器人回合")
    args = parser.parse_args()

    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        print("\n已退出", flush=True)


if __name__ == "__main__":
    main()
