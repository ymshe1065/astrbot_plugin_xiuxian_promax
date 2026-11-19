# utils/newbie_guide.py
"""《新手引导》文档解析：人机共用一份内容源。

- 给 AI：``utils/knowledge_retriever.py`` 直接按标题分块注入；
- 给玩家：``/新手指南`` 输出文本或长图（复用 help 页的双栏渲染器）。

文档：``docs/player/新手引导.md``。改文档即改内容，图片按指纹自动重绘。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from astrbot.api import logger
except ImportError:  # 允许脱离 AstrBot 跑离线测试/生成脚本
    import logging

    logger = logging.getLogger("xiuxian.newbie_guide")

__all__ = [
    "NEWBIE_DOC_PATH",
    "NEWBIE_PAGE_KEY",
    "load_newbie_raw",
    "newbie_sections",
    "newbie_text",
    "newbie_title",
    "ensure_newbie_page",
    "register_newbie_page",
]

NEWBIE_DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "player" / "新手引导.md"
NEWBIE_PAGE_KEY = "newbie"
NEWBIE_TITLE = "模拟修仙 · 新手引导"
NEWBIE_FOOTER = "仙途漫漫 · /新手指南 随时查阅 · /chat <问题> 直接问 AI 向导"

# 这一节是写给 AI 的引导约束，不对玩家展示（AI 检索时仍会读到）
AI_ONLY_SECTIONS: Tuple[str, ...] = ("给 AI 的引导要求",)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")

_cache: Dict[str, Any] = {"mtime": None, "raw": "", "sections": [], "text": ""}


def _front_matter_body(raw: str) -> str:
    if not raw.startswith("---"):
        return raw
    end = raw.find("\n---", 3)
    if end < 0:
        return raw
    return raw[end + 4 :].lstrip("\n")


def load_newbie_raw(path: Optional[Path] = None) -> str:
    """读取原始文档（按 mtime 缓存）。"""
    target = Path(path) if path else NEWBIE_DOC_PATH
    try:
        mtime = target.stat().st_mtime
    except OSError:
        logger.warning(f"【新手指南】文档缺失：{target}")
        return ""
    if path is None and _cache.get("mtime") == mtime and _cache.get("raw"):
        return str(_cache["raw"])
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"【新手指南】文档读取失败 {target.name}: {e}")
        return ""
    if path is None:
        _cache.update({"mtime": mtime, "raw": raw, "sections": [], "text": ""})
    return raw


def _table_row_to_line(line: str) -> str:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    cells = [c for c in cells if c]
    if not cells:
        return ""
    if len(cells) == 1:
        return cells[0]
    return " ｜ ".join(cells)


_INLINE_MD_RE = re.compile(r"\*\*|__|`|\[|\]")


def _clean_inline(text: str) -> str:
    """去掉 Markdown 强调/代码记号：图片渲染不了，文本里也是噪音。

    刻意不压缩空白——代码块里的指令说明靠空格对齐。
    """
    return _INLINE_MD_RE.sub("", str(text or "")).rstrip()


def _split_sections(body: str) -> List[Dict[str, Any]]:
    """按 ``##`` 切成渲染器用的 sections；``###`` 降级为小节标题行。"""
    sections: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {"name": "开始之前", "lines": []}
    in_fence = False
    skipped = False
    started = False  # 首个 ## 之前的引言不进图（是给维护者看的元信息）

    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            if line.strip() and started and not skipped:
                current["lines"].append(_clean_inline(line))
            continue

        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            title = match.group(2).strip()
            if level == 1:
                continue  # 文档主标题，交给页面 title
            if level == 2:
                started = True
                skipped = any(k in title for k in AI_ONLY_SECTIONS)
                if not skipped:
                    current = {"name": title, "lines": []}
                    sections.append(current)
                continue
            if not skipped and started and title:
                current["lines"].append(f"▸ {_clean_inline(title)}")
            continue

        if skipped or not started:
            continue
        if _HR_RE.match(line):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("|") and _TABLE_SEP_RE.match(stripped):
            continue  # 表格分隔行
        if stripped.startswith("|"):
            text = _table_row_to_line(stripped)
            if text:
                current["lines"].append(_clean_inline(text))
            continue
        if stripped.startswith(">"):
            text = stripped.lstrip(">").strip()
            if text:
                current["lines"].append(_clean_inline(text))
            continue
        if stripped.startswith(("- ", "* ")):
            current["lines"].append(f"· {_clean_inline(stripped[2:].strip())}")
            continue
        current["lines"].append(_clean_inline(stripped))

    return [s for s in sections if s.get("lines")]


def newbie_sections(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """长图/卡片用的 sections（``[{"name":…, "lines":[…]}]``）。"""
    if path is None and _cache.get("sections"):
        return list(_cache["sections"])
    raw = load_newbie_raw(path)
    if not raw:
        return []
    sections = _split_sections(_front_matter_body(raw))
    if path is None:
        _cache["sections"] = sections
    return sections


def newbie_title() -> str:
    return NEWBIE_TITLE


def newbie_text(max_chars: int = 6000, path: Optional[Path] = None) -> str:
    """纯文本版（图片不可用时的降级输出）。"""
    if path is None and _cache.get("text"):
        text = str(_cache["text"])
        return text[:max_chars]
    sections = newbie_sections(path)
    if not sections:
        return ""
    lines = [NEWBIE_TITLE, "具体数值以游戏内显示为准，本文只讲顺序与思路。", "━" * 15]
    for sec in sections:
        lines.append("")
        lines.append(f"【{sec['name']}】")
        lines.extend(f"  {ln}" for ln in sec.get("lines", []))
    lines.extend(["", NEWBIE_FOOTER])
    text = "\n".join(lines)
    if path is None:
        _cache["text"] = text
    return text[:max_chars]


def register_newbie_page() -> bool:
    """把新手引导注册为固定页（幂等），供 static_image_generator 缓存与重绘。"""
    sections = newbie_sections()
    if not sections:
        return False
    try:
        from .static_image_generator import register_page

        return bool(
            register_page(
                NEWBIE_PAGE_KEY,
                {
                    "title": NEWBIE_TITLE,
                    "subtitle": "🌱 第一天照着做，就能跑通核心循环",
                    "footer": NEWBIE_FOOTER,
                    "background": "info",
                    "renderer": "help_columns",
                    "width": 1600,
                    "sections": sections,
                    "text_builder": lambda: newbie_text(),
                },
            )
        )
    except Exception as e:
        logger.warning(f"【新手指南】固定页注册失败（将降级文本）: {e}")
        return False


async def ensure_newbie_page(allow_generate: Optional[bool] = None) -> Optional[str]:
    """取新手引导长图；未注册/生成失败返回 None，由调用方降级文本。"""
    try:
        from .static_image_generator import ensure_page

        register_newbie_page()
        return await ensure_page(NEWBIE_PAGE_KEY, allow_generate=allow_generate)
    except Exception as e:
        logger.warning(f"【新手指南】长图获取失败，降级为文本: {e}")
        return None
