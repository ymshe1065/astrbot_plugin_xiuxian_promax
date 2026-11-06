# -*- coding: utf-8 -*-
"""WebroomMessageEvent：网页房间消息事件。

发送策略：
- private（看板/查询请求）→ 只回给请求者，带 req_id，前端按 id 归位到看板
- 公开 → 广播给房间所有人（聊天气泡）
- 文本长回复 → 解析成结构化卡片（保留 raw 原文可折叠查看）
- 图片组件若命中卡片 sidecar → 直接发结构化数据，不再传 276KB 位图
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import BaseMessageComponent, Image, Plain
from astrbot.core.message.message_event_result import MessageChain as CoreMessageChain

from . import accounts as accounts_store
from . import room_server
from .card_sidecar import load_card_for_image
from .text_struct import extract_options, parse_battle, parse_text
from .visibility import is_personal_command

# 动作反馈（签到/购买/闭关…）以状态符号开头，保持气泡形态更自然
_ACTION_HEAD = re.compile(r"^\s*[✅❌🎉⚠✨💥🔔🈵🈲]")
_ACTION_MAX_LEN = 320


def _is_action_feedback(text: str) -> bool:
    return len(text) <= _ACTION_MAX_LEN and bool(_ACTION_HEAD.match(text))


async def _chain_to_payload(message: MessageChain | CoreMessageChain | None) -> dict:
    """把消息链解析为 {text, images, cards} 结构。"""
    result: dict = {"text": "", "images": [], "cards": []}
    if message is None:
        return result
    for comp in getattr(message, "chain", None) or []:
        if not isinstance(comp, BaseMessageComponent):
            continue
        if isinstance(comp, Plain):
            result["text"] += comp.text
        elif isinstance(comp, Image):
            card = load_card_for_image(getattr(comp, "path", None))
            if card is not None:
                # 卡片本质是数据：发结构化字段，附带原图地址供「查看原图」
                name = Path(comp.path).name if comp.path else ""
                card["image"] = f"/cards/{name}" if name else ""
                result["cards"].append(card)
                continue
            try:
                data_b64 = await comp.convert_to_base64()
                url = room_server.save_image_b64(data_b64)
                if url:
                    result["images"].append(url)
            except Exception:
                continue
    return result


class WebroomMessageEvent(AstrMessageEvent):
    # 由适配器 create_event 注入
    req_id: str | None = None
    private: bool = False
    want_card: bool = False
    audience: str = "public"
    panel: bool = False
    username: str = ""

    def _room_id(self) -> str:
        return self.session.session_id

    def _player_id(self) -> str:
        try:
            return str(self.message_obj.sender.user_id)
        except Exception:
            return ""

    def _message_text(self) -> str:
        """取触发本次回复的原始消息，用于判断是否为个人信息查询。"""
        try:
            return self.get_message_str() or ""
        except Exception:
            return getattr(self, "message_str", "") or ""

    async def _deliver(self, payload: dict) -> None:
        raw_text = payload.get("text", "") or ""
        text = raw_text
        images = [u for u in payload.get("images", []) if u]
        cards = list(payload.get("cards", []))
        personal = is_personal_command(self._message_text())

        # 受众：看板强制私密；手动指令默认公开；用户设为仅自己时私密
        audience = (
            "self" if (self.private or self.panel or self.audience == "self") else "public"
        )
        settings: dict[str, str] = {}
        if self.username:
            try:
                settings = accounts_store.get_settings(self.username)
            except Exception:
                settings = {}

        # 战报优先：长文本逐回合日志默认折成「结果 + 展开战报」
        if text and not _is_action_feedback(text):
            battle = parse_battle(text)
            if battle is not None:
                battle["folded"] = (
                    str(settings.get("battle_detail_default", "collapsed")) != "expanded"
                )
                # 战报详情默认所有人可展开；设为「仅自己」时，公屏只留结果
                if (
                    audience != "self"
                    and str(settings.get("battle_detail_visibility", "public")) == "self"
                ):
                    battle["detail"] = ""
                    battle["detail_hidden"] = True
                battle["owner"] = self.username
                cards.append(battle)
                text = ""
            else:
                parsed = parse_text(text)
                if parsed is not None:
                    parsed["raw"] = text
                    parsed["owner"] = self.username
                    cards.append(parsed)
                    text = ""

        # 个人信息查询：结果卡片在聊天流里默认收起，点「展开详情」再看。
        if personal:
            for card in cards:
                if isinstance(card, dict):
                    card["folded"] = True
                    card["owner"] = self.username

        if not text and not images and not cards:
            return

        room_id = self._room_id()
        payload_out = {
            "type": "reply" if audience == "self" else "bot",
            "room": room_id,
            "text": text,
            "images": images,
            "cards": cards,
            "options": extract_options(raw_text),
            "personal": personal,
            "channel": "manual",
            "audience": audience,
            "actor": self.username,
            "req_id": self.req_id or "",
            "detail_collapsed": bool(personal),
            "ts": int(time.time()),
        }

        if audience == "self":
            # 私密/看板结果只回给本人；玩家已离线时宁可丢弃，也不回退到公屏
            await room_server.send_to_player(room_id, self._player_id(), payload_out)
            return

        await room_server.broadcast_bot_payload(room_id, payload_out)

    async def send(self, message: MessageChain | None) -> None:
        if message is not None:
            await self._deliver(await _chain_to_payload(message))
        await super().send(message if message is not None else MessageChain([]))

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        text_parts: list[str] = []
        images: list[str] = []
        cards: list[dict] = []
        async for chain in generator:
            payload = await _chain_to_payload(chain)
            if payload["text"]:
                text_parts.append(payload["text"])
            images.extend(u for u in payload["images"] if u)
            cards.extend(payload["cards"])
        await self._deliver(
            {"text": "".join(text_parts), "images": images, "cards": cards}
        )
