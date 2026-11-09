# utils/list_card.py
"""发现型列表（秘境 / 历练）的卡片转换。

这两处列表条目多（秘境 45 档、历练 60 条），一张平铺列表滚动到底也选不出
该去哪个。这里把后端算好的分组数据拆成两份：

- 图片 sections：只画「推荐的前 N 条 + 折叠汇总」，给 QQ 端；
- payload：完整的分组、筛选标签与每行指令，写进 sidecar 给网页端原生渲染，
  网页端因此可以筛产物、展开折叠、点一行直接进入。

两边共用同一份排序结果，不会出现「图里第一」和「网页里第一」不一致。
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple


def pick_recommend(rows: List[dict], facets: Sequence[str], n: int) -> List[dict]:
    """按产物轮转挑推荐位：每类先各取最好的，再回头取第二好的。

    直接按收益取前 n 条会得到 6 个同类选项（高境界路线往往清一色「极高风险、
    全产物」），等于没推荐。轮转保证推荐位覆盖不同产物，玩家一眼能选出「我要刷什么」。
    """
    if n <= 0:
        return []
    buckets: Dict[str, List[dict]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("facet") or "全产物"), []).append(row)
    picked: List[dict] = []
    depth = 0
    while len(picked) < n:
        added = False
        for facet in facets:
            pile = buckets.get(facet) or []
            if depth < len(pile):
                picked.append(pile[depth])
                added = True
                if len(picked) >= n:
                    break
        if not added:
            break
        depth += 1
    return picked


def to_card_parts(
    data: dict,
    kind: str,
    top_n: int = 8,
    note: str = "",
) -> Tuple[List[Dict], Dict]:
    """把 build_rift_list / build_route_list 的结果转成 (sections, payload)。"""
    sections: List[Dict] = []
    for group in data.get("groups") or []:
        items = group.get("items") or []
        if not items:
            continue
        name = str(group.get("name") or "")
        if group.get("collapsed"):
            sections.append({"name": name, "lines": [f"共 {len(items)} 条，此处不展开"]})
            continue
        # 可出发/可进入段只画推荐位：这一段的完整清单交给网页端筛选，
        # 图片里塞 30 条既看不清也滑不动。
        picked = [it for it in items if it.get("recommend")] or items
        lines = [str(it.get("line") or it.get("title") or "") for it in picked[:top_n]]
        rest = len(items) - len(lines)
        if rest > 0:
            lines.append(f"…另有 {rest} 条，可用筛选指令查看")
        sections.append({"name": name, "lines": lines})

    if note:
        sections.append({"name": "说明", "lines": [note]})

    payload: Dict = {
        "kind": kind,
        "facets": data.get("facets") or [],
        "groups": [
            {
                "key": g.get("key") or "",
                "name": g.get("name") or "",
                "collapsed": bool(g.get("collapsed")),
                "items": [
                    {
                        "title": it.get("title") or "",
                        "badge": it.get("badge") or "",
                        "cmd": it.get("cmd") or "",
                        "facet": it.get("facet") or "",
                        "rank": it.get("rank") or 0,
                        "recommend": bool(it.get("recommend")),
                        "rows": it.get("rows") or [],
                    }
                    for it in (g.get("items") or [])
                ],
            }
            for g in (data.get("groups") or [])
        ],
    }
    return sections, payload
