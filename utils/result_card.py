# utils/result_card.py
"""通用长文本结果卡：把原本很长的文字结果渲染成单张图片。"""
from __future__ import annotations

from typing import Dict, List, Optional


def text_to_sections(text: str, section_name: str = "详情") -> List[Dict]:
    """保留原始文字信息，只去掉纯分隔线并转成卡片 section。"""
    lines = []
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if set(line.strip()) <= {"━", "-", "—", "="}:
            continue
        lines.append(line)
    return [{"name": section_name, "lines": lines}] if lines else []


async def render_result_card(
    title: str,
    text: str = "",
    *,
    background: str = "default",
    subtitle: str = "",
    footer: str = "",
    sections: Optional[List[Dict]] = None,
    enabled_key: str = "CARD_ADVENTURE",
) -> Optional[str]:
    """渲染结果图；图片关闭或失败返回 None，由调用方回退文本。"""
    try:
        from .image_generator import card_enabled, render_card_async

        if not card_enabled(enabled_key):
            return None
        card_sections = sections if sections is not None else text_to_sections(text)
        if not card_sections:
            return None
        return await render_card_async(
            background, title, subtitle, sections=card_sections, footer=footer
        )
    except Exception:
        return None
