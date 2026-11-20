# -*- coding: utf-8 -*-
"""生成「仙域争霸」大地图世界模板 config/strategy_world.json。

按设计方案 §5.1/§5.2：
- 36 节点，7 类节点占比：仙城 4 / 关隘 5 / 灵脉矿 8 / 灵谷田 8 / 坊市 4 / 门派别院 3 / 野外遗迹 4；
- 6 类地形：平原 / 森林 / 山地 / 河流 / 沼泽（大地图行军修正见 §5.4）；
- 抖动 6×6 网格铺点，边 = 网格邻接 + 少量斜边，最后并查集补边保证全图连通；
- 固定种子，可复现；每次运行覆盖写出同一份模板。

用法：python3 scripts/gen_strategy_world.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "config" / "strategy_world.json"

SEED = 20261119
COLS, ROWS = 6, 6
W, H = 1200, 780

# 节点类型（key, 数量）：占比按设计方案 §5.2（仙城 3~5 / 关隘 4~6 / 矿·田 6~8 / 坊市 3~4 / 别院 2~3 / 遗迹 2~4）
TYPE_POOL = (
    ("city", 4), ("pass", 5), ("mine", 8), ("grain", 8),
    ("market", 4), ("branch", 3), ("ruin", 4),
)

CITY_NAMES = ["凌霄仙城", "碧落仙城", "紫霄仙城", "太虚仙城", "琼华仙城"]
PASS_NAMES = ["剑门关", "落雁隘", "镇岳关", "断云关", "天枢隘", "拒马隘"]
MINE_NAMES = [
    "凝霜灵脉", "赤铜灵脉", "玄晶灵脉", "流火灵脉", "沉银灵脉",
    "紫髓灵脉", "寒铁灵脉", "曜石林", "碧血矿洞",
]
GRAIN_NAMES = [
    "青禾灵谷田", "白露灵谷田", "金穗灵谷田", "云梦灵谷田", "栖霞灵谷田",
    "沐雨灵谷田", "丰衍灵谷田", "南亩灵谷田", "北原灵谷田",
]
MARKET_NAMES = ["云集市", "落星坊", "百草坊市", "琳琅坊市", "聚宝坊"]
BRANCH_NAMES = ["丹鼎别院", "御剑别院", "万兽别院", "天机别院"]
RUIN_NAMES = ["上古剑冢", "仙人遗府", "荒古祭坛", "沉舟遗迹", "青铜古殿"]

NAMES_BY_TYPE = {
    "city": CITY_NAMES,
    "pass": PASS_NAMES,
    "mine": MINE_NAMES,
    "grain": GRAIN_NAMES,
    "market": MARKET_NAMES,
    "branch": BRANCH_NAMES,
    "ruin": RUIN_NAMES,
}

TYPE_NAMES = {
    "city": "仙城", "pass": "关隘", "mine": "灵脉矿", "grain": "灵谷田",
    "market": "坊市", "branch": "门派别院", "ruin": "野外遗迹",
}

TERRAIN_NAMES = {
    "plain": "平原", "forest": "森林", "mountain": "山地",
    "river": "河流", "swamp": "沼泽",
}


class DSU:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.p[ra] = rb
        return True


def build_nodes(rng: random.Random) -> list[dict]:
    cells = [(c, r) for r in range(ROWS) for c in range(COLS)]
    rng.shuffle(cells)
    nodes: list[dict] = []
    for i, (c, r) in enumerate(cells):
        x = 110 + c * ((W - 220) // (COLS - 1)) + rng.randint(-46, 46)
        y = 90 + r * ((H - 180) // (ROWS - 1)) + rng.randint(-30, 30)
        nodes.append({"id": i + 1, "grid": [c, r], "pos_x": x, "pos_y": y})
    nodes.sort(key=lambda n: n["id"])
    return nodes


def assign_types(nodes: list[dict], rng: random.Random) -> None:
    cx, cy = W / 2, H / 2
    # 仙城靠中央
    by_center = sorted(nodes, key=lambda n: (n["pos_x"] - cx) ** 2 + (n["pos_y"] - cy) ** 2)
    taken: set[int] = set()
    type_queue: list[str] = []
    for key, count in TYPE_POOL:
        type_queue.extend([key] * count)

    for node in by_center[:4]:
        node["type"] = "city"
        taken.add(node["id"])

    # 关隘放在边缘带（距中心较远的环上）
    edge_nodes = [n for n in by_center if n["id"] not in taken]
    edge_nodes_far = sorted(
        edge_nodes,
        key=lambda n: -((n["pos_x"] - cx) ** 2 + (n["pos_y"] - cy) ** 2),
    )
    for node in edge_nodes_far[:5]:
        node["type"] = "pass"
        taken.add(node["id"])

    rest = [n for n in nodes if n["id"] not in taken]
    rng.shuffle(rest)
    remaining = [t for t in type_queue if t not in ("city", "pass")]
    for node, t in zip(rest, remaining):
        node["type"] = t
        taken.add(node["id"])


def assign_terrain(nodes: list[dict], rng: random.Random) -> None:
    # 关隘多山地；谷田多平原；遗迹多森林
    forced = {"pass": "mountain"}
    pool = ["plain"] * 16 + ["forest"] * 8 + ["mountain"] * 6 + ["river"] * 3 + ["swamp"] * 2
    rng.shuffle(pool)
    idx = 0
    for node in nodes:
        t = node["type"]
        if t in forced:
            node["terrain"] = forced[t]
            continue
        if t == "grain" and rng.random() < 0.7:
            node["terrain"] = "plain"
            continue
        if t == "city" and rng.random() < 0.8:
            node["terrain"] = "plain"
            continue
        while idx < len(pool) and pool[idx] == "mountain" and t in ("market", "grain"):
            idx += 1
        node["terrain"] = pool[idx] if idx < len(pool) else "plain"
        idx += 1


def build_edges(nodes: list[dict], rng: random.Random) -> dict[int, set[int]]:
    by_grid = {(n["grid"][0], n["grid"][1]): n["id"] for n in nodes}
    adj: dict[int, set[int]] = {n["id"]: set() for n in nodes}

    def link(a: int, b: int) -> None:
        adj[a].add(b)
        adj[b].add(a)

    for (c, r), nid in by_grid.items():
        # 右邻 / 下邻
        for dc, dr, prob in ((1, 0, 0.92), (0, 1, 0.92), (1, 1, 0.22), (1, -1, 0.22)):
            nb = by_grid.get((c + dc, r + dr))
            if nb is not None and rng.random() < prob:
                link(nid, nb)

    # 连通性兜底：把各分量用最近点对接起来
    dsu = DSU(len(nodes) + 1)
    for a, nbrs in adj.items():
        for b in nbrs:
            dsu.union(a, b)
    pos = {n["id"]: (n["pos_x"], n["pos_y"]) for n in nodes}

    def dist2(a: int, b: int) -> float:
        return (pos[a][0] - pos[b][0]) ** 2 + (pos[a][1] - pos[b][1]) ** 2

    while True:
        groups: dict[int, list[int]] = {}
        for n in nodes:
            groups.setdefault(dsu.find(n["id"]), []).append(n["id"])
        if len(groups) <= 1:
            break
        keys = list(groups)
        best = None
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                for a in groups[keys[i]]:
                    for b in groups[keys[j]]:
                        d = dist2(a, b)
                        if best is None or d < best[0]:
                            best = (d, a, b)
        _, a, b = best
        link(a, b)
        dsu.union(a, b)

    # 每节点至少 2 条边（避免独苗咽喉过多），孤岛补最近邻
    for n in nodes:
        while len(adj[n["id"]]) < 2:
            cand = sorted((x for x in pos if x != n["id"]), key=lambda x: dist2(n["id"], x))
            linked = False
            for c in cand:
                if c not in adj[n["id"]] and len(adj[c]) < 6:
                    link(n["id"], c)
                    linked = True
                    break
            if not linked:
                break
    return adj


def assign_names(nodes: list[dict]) -> None:
    used: dict[str, int] = {}
    for node in nodes:
        t = node["type"]
        pool = NAMES_BY_TYPE[t]
        i = used.get(t, 0)
        used[t] = i + 1
        node["name"] = pool[i % len(pool)] if i < len(pool) else f"{pool[i % len(pool)]}·{i // len(pool) + 1}号"


def main() -> None:
    rng = random.Random(SEED)
    nodes = build_nodes(rng)
    assign_types(nodes, rng)
    assign_terrain(nodes, rng)
    adj = build_edges(nodes, rng)
    assign_names(nodes)

    out_nodes = []
    for n in sorted(nodes, key=lambda x: x["id"]):
        out_nodes.append({
            "id": n["id"],
            "name": n["name"],
            "type": n["type"],
            "type_name": TYPE_NAMES[n["type"]],
            "terrain": n["terrain"],
            "terrain_name": TERRAIN_NAMES[n["terrain"]],
            "pos_x": n["pos_x"],
            "pos_y": n["pos_y"],
            "adj": sorted(adj[n["id"]]),
        })

    doc = {
        "templates": [
            {
                "template_id": "standard36",
                "name": "九州仙域·三十六洞天",
                "width": W,
                "height": H,
                "nodes": out_nodes,
            }
        ],
        "meta": {
            "seed": SEED,
            "generated_by": "scripts/gen_strategy_world.py",
            "type_names": TYPE_NAMES,
            "terrain_names": TERRAIN_NAMES,
        },
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    edge_count = sum(len(n["adj"]) for n in out_nodes) // 2
    print(f"written {OUT}: {len(out_nodes)} nodes, {edge_count} edges")
    from collections import Counter
    print("types:", dict(Counter(n["type"] for n in out_nodes)))
    print("terrain:", dict(Counter(n["terrain"] for n in out_nodes)))


if __name__ == "__main__":
    main()
