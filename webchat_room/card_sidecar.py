# -*- coding: utf-8 -*-
"""卡片结构化 sidecar 读取。

插件的信息卡片由 Pillow 直接绘制，落盘时同时写同名 .json（见 utils/image_generator.py）。
网页端拿到 Image 组件的本地 path 后，读同名 sidecar 即可原生渲染，不必再看位图。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
# 单卡片 sidecar 上限，防止读到异常大文件
_MAX_BYTES = 512 * 1024

_ITEM_KV = re.compile(r"^([^\s：:]{1,14})\s*[：:]\s*(.+)$")


def _looks_like_item_head(line: str) -> bool:
    """商品/条目首行：聚灵散·凡 - 246灵石(8.2折) · 库存3"""
    s = line.strip()
    if not s or len(s) > 80:
        return False
    if _ITEM_KV.match(s):
        return False
    return (" - " in s) or ("灵石" in s) or ("库存" in s)


def _promote_lines_to_items(section: dict[str, Any]) -> None:
    """把「每条目占多行的纯文本清单」还原成条目数组。

    插件的商店/背包类卡片把商品塞进 lines（一条商品 = 1 行标题 + N 行属性），
    网页端逐行平铺可读性极差，这里提升成 items 列表。
    """
    lines = [ln.strip() for ln in (section.get("lines") or []) if ln.strip()]
    if len(lines) < 8:
        return
    items: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for ln in lines:
        if _looks_like_item_head(ln):
            title, meta = ln, ""
            if " - " in ln:
                title, meta = ln.split(" - ", 1)
            cur = {"title": title.strip(), "meta": meta.strip(), "rows": [], "lines": []}
            items.append(cur)
            continue
        if cur is None:
            continue
        m = _ITEM_KV.match(ln)
        if m:
            cur["rows"].append([m.group(1), m.group(2).strip()])
        else:
            cur["lines"].append(ln)
    if len(items) >= 3:
        section["items"] = items
        section["lines"] = []


def load_card_for_image(path: str | None) -> dict[str, Any] | None:
    """按图片路径读同名 sidecar；不存在或损坏返回 None。"""
    if not path:
        return None
    try:
        p = Path(path)
    except Exception:
        return None
    if p.suffix.lower() not in _IMAGE_SUFFIXES:
        return None
    sidecar = p.with_suffix(".json")
    try:
        if not sidecar.is_file() or sidecar.stat().st_size > _MAX_BYTES:
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        return None
    normalized = {
        "id": str(data.get("id") or p.stem),
        "kind": str(data.get("bg") or "card"),
        "title": str(data.get("title") or ""),
        "subtitle": str(data.get("subtitle") or ""),
        "footer": str(data.get("footer") or ""),
        "sections": [],
    }
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        rows = []
        for row in sec.get("rows") or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                rows.append([str(row[0]), str(row[1])])
        lines = [str(x) for x in (sec.get("lines") or []) if str(x).strip()]
        name = str(sec.get("name") or "")
        if not rows and not lines and not name:
            continue
        section = {"name": name, "rows": rows, "lines": lines}
        _promote_lines_to_items(section)
        # 调用方预先拆好的结构化条目（储物戒），原样透传给网页
        entries = [
            it for it in (sec.get("entries") or [])
            if isinstance(it, dict) and it.get("name")
        ]
        if entries:
            section["entries"] = entries
        normalized["sections"].append(section)
    if not normalized["sections"]:
        return None
    return normalized
