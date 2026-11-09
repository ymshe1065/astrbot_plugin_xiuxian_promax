# -*- coding: utf-8 -*-
"""卡片结构化 sidecar 读取。

插件的信息卡片由 Pillow 直接绘制，落盘时同时写同名 .json（见 utils/image_generator.py）。
网页端拿到 Image 组件的本地 path 后，读同名 sidecar 即可原生渲染，不必再看位图。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
# 单卡片 sidecar 上限，防止读到异常大文件
_MAX_BYTES = 512 * 1024

_ITEM_KV = re.compile(r"^([^\s：:]{1,14})\s*[：:]\s*(.+)$")
# 商品首行必须带价格或库存数字：`聚灵散 - 246灵石(8.2折) · 库存3`
# 只看「 - 」会把「等级 1 - 10」这类普通行误判成商品
_ITEM_PRICE = re.compile(r"\d[\d,]*\s*灵石|库存\s*\d+")
# 旧卡片把五行 / 常驻标签拼在物品名后面（`玄铁右戒[金]`），这些标签不属于名字，
# 留着会让网页端复制出的 /购买 参数永远匹配不到货架条目。
_DISPLAY_SUFFIX = re.compile(r"(?:\s*\[(?:金|木|水|火|土|常驻)\])+\s*$")


def _strip_display_suffix(text: str) -> str:
    """去掉物品名末尾的展示标签（五行 / 常驻），返回可购买的名称。"""
    return _DISPLAY_SUFFIX.sub("", str(text or "").strip()).strip()


def _looks_like_item_head(line: str) -> bool:
    """商品/条目首行：聚灵散·凡 - 246灵石(8.2折) · 库存3"""
    s = line.strip()
    if not s or len(s) > 100:
        return False
    if _ITEM_KV.match(s):
        return False
    if not _ITEM_PRICE.search(s):
        return False
    return (" - " in s) or ("库存" in s)


def _section_rank(name: str) -> str:
    """分区名 `皇品区` → `皇品`；不是「X品区」形态返回空串。"""
    s = (name or "").strip()
    if s.endswith("区") and s[:-1].endswith("品"):
        return s[:-1]
    return ""


def _promote_lines_to_items(section: dict[str, Any]) -> None:
    """把「每条目占多行的纯文本清单」还原成条目数组。

    插件的商店/背包类卡片把商品塞进 lines（一条商品 = 1 行标题 + N 行属性），
    网页端逐行平铺可读性极差，这里提升成 items 列表。

    百宝阁按品阶分区上架，单区通常只有 1~3 件，所以不能用「条目数」做门槛，
    改成看覆盖率：只要条目吃下了大部分行，就说明这个分区确实是清单。
    """
    lines = [ln.strip() for ln in (section.get("lines") or []) if ln.strip()]
    if not lines:
        return
    items: list[dict[str, Any]] = []
    loose: list[str] = []
    cur: dict[str, Any] | None = None
    for ln in lines:
        if _looks_like_item_head(ln):
            title, meta = ln, ""
            if " - " in ln:
                title, meta = ln.split(" - ", 1)
            title = _strip_display_suffix(title)
            cur = {"name": title, "title": title, "meta": meta.strip(), "rows": [], "lines": []}
            items.append(cur)
            continue
        if cur is None:
            # 清单之前的抬头行（如分区说明）保留为分区正文
            loose.append(ln)
            continue
        m = _ITEM_KV.match(ln)
        if m:
            cur["rows"].append([m.group(1), m.group(2).strip()])
        else:
            cur["lines"].append(ln)
    if not items:
        return
    covered = sum(1 + len(it["rows"]) + len(it["lines"]) for it in items)
    if covered < max(3, len(lines) * 0.6):
        return
    _dedupe_rank(section, items)
    _hoist_shared_lines(section, items)
    section["items"] = items
    section["lines"] = loose


def _hoist_shared_lines(section: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """每件商品都写一遍的说明（如「同属性加法叠加…」）提到区块尾部，只留一份。

    货架上的装备共用同一套结算规则，逐件重复既占高度又淹没差异；
    只搬运在多数条目里字面完全相同的行，单件独有的说明仍留在条目内。
    """
    if len(items) < 2:
        return
    counts: dict[str, int] = {}
    order: list[str] = []
    for it in items:
        for ln in it.get("lines") or []:
            key = str(ln).strip()
            if not key:
                continue
            if key not in counts:
                counts[key] = 0
                order.append(key)
            counts[key] += 1
    need = max(2, math.ceil(len(items) * 0.6))
    shared = [k for k in order if counts[k] >= need]
    if not shared:
        return
    drop = set(shared)
    for it in items:
        it["lines"] = [ln for ln in (it.get("lines") or []) if str(ln).strip() not in drop]
    section["note"] = shared


def _dedupe_rank(section: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """分区名已写品阶，删掉重复的「品级」行，但标题保留完整物品名。

    卡片标题就是 `/购买 <名称>` 的参数，品阶前缀是物品名的一部分
    （`帝品·玄铁右戒`），去掉后玩家照抄会买不到，所以只精简属性行、不改名。
    """
    rank = _section_rank(section.get("name") or "")
    if not rank:
        return
    for it in items:
        it["rows"] = [
            row for row in it.get("rows") or []
            if not (str(row[0]).strip() == "品级" and str(row[1]).strip() == rank)
        ]



def _normalize_groups(raw: Any) -> list[dict[str, Any]]:
    """读取「发现型列表」的分组数据（秘境/历练）。

    这类卡片的正文是给 QQ 端看的精简图，网页端要的是可筛选、可折叠、
    每行带指令的完整结构，所以单独走 groups 而不是 sections。
    """
    if not isinstance(raw, list):
        return []
    groups: list[dict[str, Any]] = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        items: list[dict[str, Any]] = []
        for it in g.get("items") or []:
            if not isinstance(it, dict):
                continue
            rows = []
            for row in it.get("rows") or []:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    rows.append([str(row[0]), str(row[1])])
            items.append(
                {
                    "title": str(it.get("title") or ""),
                    "badge": str(it.get("badge") or ""),
                    "meta": str(it.get("meta") or ""),
                    "cmd": str(it.get("cmd") or ""),
                    "facet": str(it.get("facet") or ""),
                    "value": str(it.get("value") or ""),
                    "hint": str(it.get("hint") or ""),
                    "recommend": bool(it.get("recommend")),
                    # 未解锁行：网页端据此显示「需 XX」，不给可点按钮
                    "locked": bool(it.get("locked")),
                    "lock_text": str(it.get("lock_text") or ""),
                    "rows": rows,
                }
            )
        if not items:
            continue
        groups.append(
            {
                "key": str(g.get("key") or ""),
                "name": str(g.get("name") or ""),
                "collapsed": bool(g.get("collapsed")),
                "items": items,
            }
        )
    return groups


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
    if not isinstance(sections, list):
        sections = []
    groups = _normalize_groups(data.get("groups"))
    if not sections and not groups:
        return None
    normalized = {
        "id": str(data.get("id") or p.stem),
        "kind": str(data.get("kind") or data.get("bg") or "card"),
        "title": str(data.get("title") or ""),
        "subtitle": str(data.get("subtitle") or ""),
        "footer": str(data.get("footer") or ""),
        "sections": [],
    }
    if groups:
        facets = [
            {"key": str(f.get("key") or ""), "label": str(f.get("label") or ""), "count": f.get("count") or 0}
            for f in (data.get("facets") or [])
            if isinstance(f, dict) and f.get("key")
        ]
        normalized["groups"] = groups
        normalized["facets"] = facets
        normalized["sections"] = []
        return normalized
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
