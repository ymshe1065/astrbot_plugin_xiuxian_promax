# -*- coding: utf-8 -*-
"""webroom 平台适配器：把网页房间映射为 AstrBot 的「群聊」平台。

- 通过 register_platform_adapter 注册为 platform type "webroom"，
  后台配置 {"type": "webroom", "enable": true, ...} 即可实例化。
- 玩家身份: sender.user_id = "web_<玩家名>"（get_sender_id() 的返回值）
- 房间即群: session_id = room_id, group_id = room_id（get_group_id() 的返回值）
- UMO 形如 webroom:GroupMessage:room_main，与插件的定时世界播报格式天然匹配。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Coroutine
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.platform.register import (
    platform_cls_map,
    register_platform_adapter,
)

from . import room_server
from .webroom_event import WebroomMessageEvent

ADAPTER_NAME = "webroom"
ADAPTER_DESC = "修仙网页聊天室（路线B 自定义适配器）"
DEFAULT_CONFIG_TMPL = {
    "host": "127.0.0.1",
    "port": 6198,
}


class WebroomAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.metadata = PlatformMetadata(
            name=ADAPTER_NAME,
            description=ADAPTER_DESC,
            id=ADAPTER_NAME,
            support_proactive_message=True,
        )
        self._shutdown_event = asyncio.Event()
        room_server.set_adapter(self)

    def meta(self) -> PlatformMetadata:
        return self.metadata

    # ---------- 生命周期 ----------

    async def run(self) -> Coroutine[Any, Any, None]:
        host = str(self.config.get("host", "127.0.0.1"))
        port = int(self.config.get("port", 6198))
        await room_server.start(host, port)
        await self._shutdown_event.wait()

    async def terminate(self) -> None:
        self._shutdown_event.set()
        await room_server.stop()

    # ---------- 玩家消息 -> 流水线 ----------

    async def convert_message(
        self,
        room_id: str,
        player_id: str,
        player_name: str,
        text: str,
    ) -> AstrBotMessage:
        abm = AstrBotMessage()
        abm.self_id = ADAPTER_NAME
        abm.sender = MessageMember(user_id=player_id, nickname=player_name)
        abm.type = MessageType.GROUP_MESSAGE
        abm.session_id = room_id
        abm.message_id = uuid.uuid4().hex
        abm.group = Group(group_id=room_id, group_name=room_server.room_name(room_id))
        abm.message = [Plain(text)]
        abm.message_str = text
        abm.raw_message = {
            "room": room_id,
            "player": player_id,
            "player_name": player_name,
            "text": text,
        }
        abm.timestamp = int(time.time())
        return abm

    def create_event(self, message: AstrBotMessage) -> WebroomMessageEvent:
        event = WebroomMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
        )
        return event

    async def on_player_message(
        self,
        room_id: str,
        player_id: str,
        player_name: str,
        text: str,
    ) -> None:
        abm = await self.convert_message(room_id, player_id, player_name, text)
        self.commit_event(self.create_event(abm))

    # ---------- 世界事件播报（context.send_message 的落点） ----------

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ) -> None:
        """插件定时广播（Boss/秘境/拍卖/宗门/坊市）经此透传到网页房间。"""
        room_id = session.session_id
        texts: list[str] = []
        for comp in getattr(message_chain, "chain", []) or []:
            if isinstance(comp, Plain):
                texts.append(comp.text)
        text = "".join(texts).strip()
        if text:
            await room_server.broadcast_bot(room_id, text)
        await super().send_by_session(session, message_chain)


def is_registered() -> bool:
    return ADAPTER_NAME in platform_cls_map


def register() -> bool:
    """向 AstrBot 注册 webroom 平台类型。幂等，失败返回 False。"""
    if is_registered():
        logger.debug("[webroom] 平台适配器已注册，跳过")
        return True
    try:
        decorator = register_platform_adapter(
            ADAPTER_NAME,
            ADAPTER_DESC,
            default_config_tmpl=dict(DEFAULT_CONFIG_TMPL),
        )
        decorator(WebroomAdapter)
        logger.info("[webroom] 平台适配器注册成功")
        return True
    except Exception as e:
        logger.error(f"[webroom] 平台适配器注册失败: {e}")
        return False
