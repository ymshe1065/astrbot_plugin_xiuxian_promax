# -*- coding: utf-8 -*-
"""WebroomMessageEvent：网页房间消息事件。

- send()：插件 handler yield 的回复 → 广播给房间全部在线玩家
- send_streaming()：收集完整文本/图片后一次性广播
- 图片组件落盘到 data/webroom/imgs，通过 /media/<file> 提供访问
"""
from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import BaseMessageComponent, Image, Plain
from astrbot.core.message.message_event_result import MessageChain as CoreMessageChain

from . import room_server


async def _chain_to_payload(message: MessageChain | CoreMessageChain | None) -> dict:
    """把消息链解析为 {text, images} 结构。"""
    result = {"text": "", "images": []}
    if message is None:
        return result
    for comp in getattr(message, "chain", None) or []:
        if not isinstance(comp, BaseMessageComponent):
            continue
        if isinstance(comp, Plain):
            result["text"] += comp.text
        elif isinstance(comp, Image):
            try:
                data_b64 = await comp.convert_to_base64()
                url = room_server.save_image_b64(data_b64)
                result["images"].append(url)
            except Exception:
                result["images"].append("")
    return result


class WebroomMessageEvent(AstrMessageEvent):
    async def send(self, message: MessageChain | None) -> None:
        if message is not None:
            payload = await _chain_to_payload(message)
            if payload["text"] or payload["images"]:
                await room_server.broadcast_bot_payload(self.session.session_id, payload)
        await super().send(message if message is not None else MessageChain([]))

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        text_parts: list[str] = []
        images: list[str] = []
        async for chain in generator:
            payload = await _chain_to_payload(chain)
            if payload["text"]:
                text_parts.append(payload["text"])
            images.extend(u for u in payload["images"] if u)
        if text_parts or images:
            await room_server.broadcast_bot_payload(
                self.session.session_id, {"text": "".join(text_parts), "images": images}
            )
