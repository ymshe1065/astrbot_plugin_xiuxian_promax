# utils/auction_ai.py
"""拍卖会 AI 播报：只生成氛围文案，价格与拍品事实由插件模板保证。"""

import asyncio
import re
from typing import Optional

from astrbot.api import logger

__all__ = ["AuctionAIBroadcaster"]


class AuctionAIBroadcaster:
    """可选 LLM 播报器；任何异常都回退到固定模板。"""

    SCENE_HINTS = {
        "reminder": "用一句仙侠世界拍卖行掌柜的口吻，提醒修士们拍卖会即将召开。",
        "start": "用一句仙侠世界拍卖师的口吻，宣布一件稀世拍品登场。",
        "bid": "用一句简短喝彩，渲染拍卖会竞价激烈的气氛。",
        "result": "用一句仙侠世界拍卖师落槌或宣布流拍的口吻。",
        "summary": "用一句仙侠世界拍卖行收场的口吻，感谢修士们参与。",
    }

    def __init__(self, context, enabled: bool = True, bid_enabled: bool = False,
                 timeout: int = 8, max_chars: int = 300, provider_id: str = ""):
        self.context = context
        self.enabled = bool(enabled)
        self.bid_enabled = bool(bid_enabled)
        try:
            self.timeout = max(2, int(timeout or 8))
        except (TypeError, ValueError):
            self.timeout = 8
        try:
            self.max_chars = max(20, int(max_chars or 300))
        except (TypeError, ValueError):
            self.max_chars = 300
        self.provider_id = str(provider_id or "").strip()

    async def compose(self, kind: str, payload: dict, fallback: str,
                      umo: Optional[str] = None) -> str:
        if not self.enabled:
            return fallback
        if kind == "bid" and not self.bid_enabled:
            return fallback
        if not self.context or not umo:
            return fallback

        try:
            provider_id = self.provider_id
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                return fallback
            prompt = self._build_prompt(kind, payload)
            resp = await asyncio.wait_for(
                self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt),
                timeout=self.timeout,
            )
            flavor = self._clean(getattr(resp, "completion_text", "") or "")
            if not flavor:
                return fallback
            flavor = flavor[: self.max_chars]
            facts = str((payload or {}).get("facts") or "").strip()
            if not facts:
                return fallback
            return f"{flavor}\n{facts}"
        except asyncio.TimeoutError:
            logger.warning("【拍卖会】AI 播报超时，已降级为模板")
            return fallback
        except Exception as e:
            logger.warning(f"【拍卖会】AI 播报失败，已降级为模板: {e}")
            return fallback

    def _build_prompt(self, kind: str, payload: dict) -> str:
        hint = self.SCENE_HINTS.get(kind, "用一句简短的仙侠世界播报口吻。")
        facts = str((payload or {}).get("facts") or "").strip()
        return (
            "你是修仙世界【万宝阁拍卖行】的首席拍卖师。\n"
            f"{hint}\n"
            "要求：\n"
            "1. 只写 1 句，不超过 60 个汉字。\n"
            "2. 不要写任何数字、价格、时间、物品名或玩家名，这些由系统另行展示。\n"
            "3. 不要使用 Markdown、列表、引号或表情符号。\n"
            "4. 不要重复下面的事实内容，只写氛围。\n"
            f"【事实参考】\n{facts}\n"
            "【你的氛围文案】"
        )

    @staticmethod
    def _clean(text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r"^[\s\"'“”‘’`*#>-]+", "", text)
        text = re.sub(r"[\s\"'“”‘’`*#]+$", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
