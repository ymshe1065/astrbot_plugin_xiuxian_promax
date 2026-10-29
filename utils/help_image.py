# utils/help_image.py
"""帮助页专用双栏长图渲染器：信息不截断，按主题分栏，降低单栏高度。"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageEnhance

from .image_generator import (
    BG_DIR,
    CARD_TEMP_DIR,
    HAS_PIL,
    INK_ACCENT,
    INK_BG_TOP,
    INK_DIVIDER,
    INK_FOOTER,
    INK_GOLD,
    INK_GOLD_DIM,
    INK_PANEL,
    INK_PANEL_EDGE,
    INK_SECTION,
    INK_TEXT,
    _cover_resize,
    _draw_mixed,
    _load_font,
    _mixed_width,
    _wrap_mixed,
)

ROW_H = 40
SECTION_HEAD_H = 50
SECTION_GAP = 24
COL_GAP = 56


def _section_height(section: Dict[str, Any], content_w: int, f_text) -> int:
    height = SECTION_HEAD_H
    for line in section.get("lines", []):
        height += ROW_H * max(1, len(_wrap_mixed(str(line), f_text, content_w, 24)))
    return height + SECTION_GAP


def _split_sections(sections: List[Dict[str, Any]], content_w: int, f_text):
    """在 section 边界把内容分成左右两栏，尽量平衡高度。"""
    if not sections:
        return [], []
    heights = [_section_height(section, content_w, f_text) for section in sections]
    total = sum(heights)
    best_index = 1
    best_delta = None
    left = 0
    for index in range(1, len(sections)):
        left += heights[index - 1]
        delta = abs(total - 2 * left)
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_index = index
    return sections[:best_index], sections[best_index:]


def _draw_column(canvas, draw, sections, x, y, content_w, f_head, f_text):
    for section in sections:
        name = str(section.get("name", ""))
        _draw_mixed(canvas, (x, y + 2), name, f_head, 24, INK_SECTION)
        draw = ImageDraw.Draw(canvas)
        name_w = _mixed_width(name, f_head, 24)
        draw.line(
            [(x + name_w + 18, y + 24), (x + content_w, y + 24)],
            fill=INK_DIVIDER + (170,), width=1,
        )
        y += SECTION_HEAD_H
        for line in section.get("lines", []):
            for wrapped in _wrap_mixed(str(line), f_text, content_w, 24):
                _draw_mixed(canvas, (x + 4, y), wrapped, f_text, 24, INK_TEXT)
                draw = ImageDraw.Draw(canvas)
                y += ROW_H
        y += SECTION_GAP
    return y


def render_help_page(
    title: str,
    subtitle: str,
    sections: List[Dict[str, Any]],
    footer: str = "",
    width: int = 1600,
) -> Optional[str]:
    if not HAS_PIL or not sections:
        return None
    try:
        pad = 44
        inner = 30
        outer = pad + inner
        content_w = (width - outer * 2 - COL_GAP) // 2
        f_title = _load_font(54, "heavy")
        f_sub = _load_font(26, "text")
        f_head = _load_font(29, "bold")
        f_text = _load_font(24, "semi")
        f_foot = _load_font(24, "text")

        left, right = _split_sections(sections, content_w, f_text)
        left_h = sum(_section_height(s, content_w, f_text) for s in left)
        right_h = sum(_section_height(s, content_w, f_text) for s in right)
        body_h = max(left_h, right_h, 200)
        header_h = 170
        footer_h = 74 if footer else 34
        height = header_h + body_h + outer + footer_h + pad

        bg_path = BG_DIR / "info.jpg"
        if not bg_path.exists():
            bg_path = BG_DIR / "default.jpg"
        if not bg_path.exists():
            return None
        bg = Image.open(bg_path).convert("RGB")
        canvas = _cover_resize(bg, width, height).convert("RGBA")
        canvas = ImageEnhance.Brightness(canvas).enhance(0.45)
        canvas = ImageEnhance.Color(canvas).enhance(0.85).convert("RGBA")

        panel = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        pdraw = ImageDraw.Draw(panel)
        pdraw.rounded_rectangle(
            [pad, header_h - 28, width - pad, height - footer_h],
            radius=28, fill=INK_PANEL,
            outline=INK_PANEL_EDGE, width=3,
        )
        canvas = Image.alpha_composite(canvas, panel)
        draw = ImageDraw.Draw(canvas)

        title_w = _mixed_width(title, f_title, 40)
        _draw_mixed(canvas, ((width - title_w) / 2, 34), title, f_title, 40, INK_GOLD)
        draw = ImageDraw.Draw(canvas)
        if subtitle:
            sub_w = _mixed_width(subtitle, f_sub, 22)
            _draw_mixed(canvas, ((width - sub_w) / 2, 104), subtitle, f_sub, 22, INK_GOLD_DIM)
            draw = ImageDraw.Draw(canvas)

        x_left = outer
        x_right = outer + content_w + COL_GAP
        y = header_h + 18
        _draw_column(canvas, draw, left, x_left, y, content_w, f_head, f_text)
        _draw_column(canvas, draw, right, x_right, y, content_w, f_head, f_text)
        draw = ImageDraw.Draw(canvas)
        draw.line(
            [(x_right - COL_GAP // 2, y), (x_right - COL_GAP // 2, height - footer_h - 20)],
            fill=INK_ACCENT + (80,), width=1,
        )

        if footer:
            foot_w = _mixed_width(footer, f_foot, 22)
            _draw_mixed(
                canvas, ((width - foot_w) / 2, height - footer_h + 12),
                footer, f_foot, 22, INK_FOOTER,
            )

        CARD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        out_path = CARD_TEMP_DIR / f"help_{uuid.uuid4().hex[:12]}.jpg"
        canvas.convert("RGB").save(str(out_path), format="JPEG", quality=92)
        return str(out_path)
    except Exception:
        return None


async def render_help_page_async(*args, **kwargs) -> Optional[str]:
    import asyncio
    return await asyncio.to_thread(render_help_page, *args, **kwargs)
