# -*- coding: utf-8 -*-
"""「仙域争霸」六边形会战引擎（设计方案 §7.3~§7.8，P2）。

纯逻辑模块：不碰数据库、不碰 asyncio。会战状态是一个可 JSON 序列化的
``session`` dict（落库到 strategy_battles.session_json），所有操作通过
本模块函数变更 session 并追加指令到 ``session["commands"]``——
seed + 指令序列可完整重建战报回放（§7.3 可复现）。

随机性一律通过外部传入 ``random.Random``（由 seed 派生），保证同 seed
同指令序列 → 同结果（§20 战斗模拟确定性）。

坐标系：odd-q 偏移坐标（奇数列下移半格），距离/邻接复用
``core.strategy_rules`` 的 cube 换算。
"""
from __future__ import annotations

import random
from typing import Any, Optional

from . import strategy_rules as R

# ---------------------------------------------------------------------------
# 地形（§7.3 地图生成）
# ---------------------------------------------------------------------------

T_PLAIN = "plain"
T_FOREST = "forest"
T_HIGHLAND = "highland"
T_RIVER = "river"
T_SWAMP = "swamp"
T_WALL = "wall"       # 攻城图城墙弧
T_GATE = "gate"       # 城门（可通行）
T_MOAT = "moat"       # 护城河

TERRAIN_NAMES = {
    T_PLAIN: "平原", T_FOREST: "森林", T_HIGHLAND: "高地", T_RIVER: "河流",
    T_SWAMP: "沼泽", T_WALL: "城墙", T_GATE: "城门", T_MOAT: "护城河",
}

#: 移动消耗（§7.3；重甲禁沼泽在 passable 里单独处理）
TERRAIN_MOVE_COST = {
    T_PLAIN: 1, T_FOREST: 2, T_HIGHLAND: 2, T_RIVER: 3,
    T_SWAMP: 2, T_WALL: 99, T_GATE: 2, T_MOAT: 3,
}

#: 地形战斗系数（攻/防）——森林隐蔽 +25% 防、高地 +15% 攻 +10% 防、
#: 河流涉水 -20% 防、沼泽 -10% 防、城墙 ×2.5 防（§5.3/§7.3 城墙弧线）
TERRAIN_ATK_MULT = {
    T_PLAIN: 1.0, T_FOREST: 0.95, T_HIGHLAND: 1.15, T_RIVER: 0.9,
    T_SWAMP: 0.9, T_WALL: 1.0, T_GATE: 1.0, T_MOAT: 0.9,
}
TERRAIN_DEF_MULT = {
    T_PLAIN: 1.0, T_FOREST: 1.25, T_HIGHLAND: 1.10, T_RIVER: 0.8,
    T_SWAMP: 0.9, T_WALL: 2.5, T_GATE: 1.8, T_MOAT: 0.9,
}

#: 阵型（§7.3 部署阶段）：锋矢攻+10% / 鹤翼包抄判定+1方向 / 方圆防+15% / 雁行远程伤+10%
FORMATIONS = ("", "fengshi", "heyi", "fangyuan", "yanhang")
FORMATION_NAMES = {
    "": "无阵型", "fengshi": "锋矢阵", "heyi": "鹤翼阵",
    "fangyuan": "方圆阵", "yanhang": "雁行阵",
}
FORMATION_ATK = {"fengshi": 1.10}
FORMATION_DEF = {"fangyuan": 1.15}
FORMATION_RANGED = {"yanhang": 1.10}

#: 会战模式 → 尺寸（§7.3；config hex.field_size 等可覆盖）
MODE_SIZE = {
    "field": (13, 9),
    "siege": (15, 11),
    "pass": (9, 11),
}

MANA_MAX = 100
MANA_PER_ROUND = 15
MANA_ON_HIT = 5

#: 灵力技能表（§7.7；数值细节由 config skills 组注入，这里是结构默认）
SKILLS: dict[str, dict[str, Any]] = {
    "fire":      {"name": "火攻", "mana": 40, "professions": ("spirit", "demon"),
                  "roles": ("strategist",), "target": "hex", "range": 3,
                  "burn_rounds": 2, "forest_mult": 1.5},
    "flood":     {"name": "水淹", "mana": 50, "professions": ("spirit",),
                  "roles": ("strategist",), "target": "hex", "range": 2,
                  "need_river": True, "swamp_rounds": 3},
    "confuse":   {"name": "惑敌", "mana": 35, "professions": ("ghost",),
                  "roles": (), "target": "unit", "range": 4},
    "taunt":     {"name": "挑衅", "mana": 25, "professions": ("sword",),
                  "roles": (), "target": "unit", "range": 3},
    "bulwark":   {"name": "坚守阵", "mana": 30, "professions": ("body",),
                  "roles": (), "target": "self", "def_bonus": 0.4, "rounds": 3},
    "heal":      {"name": "治疗术", "mana": 30, "professions": ("buddha",),
                  "roles": (), "target": "friendly", "range": 2,
                  "heal_pct": 0.05, "injury_heal": 5},
    "beastrush": {"name": "兽群突袭", "mana": 45, "professions": ("beast",),
                  "roles": (), "target": "empty", "range": 2,
                  "summon_troops": 500, "rounds": 3},
    "barrage":   {"name": "落石/箭雨", "mana": 35, "professions": (),
                  "roles": (), "target": "hex", "range": 3,
                  "need_highland": True},
    "ram":       {"name": "破城槌撞击", "mana": 0, "professions": (),
                  "roles": (), "target": "wall", "range": 0, "wall_mult": 3.0},
    "combo":     {"name": "合击技", "mana": 60, "professions": (),
                  "roles": (), "target": "unit", "range": 2,
                  "need_bond": "sworn", "damage_mult": 1.8, "morale_hit": 10},
}


# ---------------------------------------------------------------------------
# session 构造与地图生成（§7.3）
# ---------------------------------------------------------------------------

def _key(q: int, r: int) -> str:
    return f"{q},{r}"


# ---------------------------------------------------------------------------
# 控制/减益状态的「生效回合」判定（§7.7）
#
# 技能在目标行动前施放时，若直接读 statuses 会让同一回合立刻生效，相当于
# 多赚一回合（施放回合 + 下一回合）。这里统一约定：控制/减益自「下一回合」
# 起生效——施放回合用 *_from 记录来源回合，只有 round > *_from 才算生效。
# ---------------------------------------------------------------------------

def _status_active(session: dict, unit: dict, key: str) -> bool:
    st = (session.get("statuses") or {}).get(str(unit.get("uid"))) or {}
    if not st.get(key):
        return False
    return int(st.get(key + "_from", -1)) < int(session.get("round", 0))


def is_confused(session: dict, unit: dict) -> bool:
    """目标是否处于惑敌（下一回合起生效）。"""
    return _status_active(session, unit, "confuse")


def is_suppressed(session: dict, unit: dict) -> bool:
    """目标是否被落石/箭雨压制（下一回合起机动减半）。"""
    return _status_active(session, unit, "suppress")


def active_taunter(session: dict, unit: dict) -> Optional[int]:
    """返回强制挑衅者 uid（下一回合起生效；挑衅者已失去战斗力则失效）。"""
    if not _status_active(session, unit, "taunt_by"):
        return None
    st = (session.get("statuses") or {}).get(str(unit.get("uid"))) or {}
    tu = get_unit(session, int(st.get("taunt_by", 0)))
    if tu and tu["troops"] > 0 and not tu["routed"]:
        return int(tu["uid"])
    return None


def _rotate(q: int, r: int, w: int, h: int) -> tuple[int, int]:
    """180° 旋转对称映射（部署区公平性校验用）。"""
    return (w - 1 - q, h - 1 - r)


def terrain_at(session: dict, q: int, r: int) -> str:
    return (session.get("terrain") or {}).get(_key(q, r), T_PLAIN)


def in_bounds(session: dict, q: int, r: int) -> bool:
    w, h = session["size"]
    return 0 <= q < w and 0 <= r < h


def passable(session: dict, q: int, r: int, troop_type: str = "") -> bool:
    if not in_bounds(session, q, r):
        return False
    t = eff_terrain(session, q, r)  # 含水淹临时沼泽（重甲禁入一切沼泽，§7.5）
    if t == T_WALL:
        return False
    if t == T_SWAMP and troop_type == "heavy_armor":
        return False  # 重甲沼泽禁入（§7.5）
    return True


def move_points(unit: dict, session: dict) -> float:
    """机动点 = 兵种 move（飞剑平原适性由路径消耗 -0.5 体现；兽群森林不减）。"""
    troop = R.TROOP_TYPES.get(unit.get("troop_type", "sword_shield"),
                              R.TROOP_TYPES["sword_shield"])
    mp = float(troop.get("move", 3))
    if is_suppressed(session, unit):
        mp *= 0.5  # 落石/箭雨压制（下一回合起生效）
    st = (session.get("statuses") or {}).get(str(unit.get("uid"))) or {}
    if int(st.get("charge", 0) or 0) > 0:
        mp += 2  # 剑阵冲锋：3 回合机动 +2（§7.5）
    return mp


def _generate_terrain_once(rng: random.Random, mode: str, w: int, h: int,
                           hex_cfg: dict | None) -> dict[str, str]:
    """单次地形掷骰：高地 1~2 处靠中央、森林 15~20%、沼泽 ≤8%、
    河流战场 1 条渡河线；攻城图追加城墙弧 + 护城河 + 城门。"""
    hex_cfg = hex_cfg or {}
    terrain: dict[str, str] = {}
    for q in range(w):
        for r in range(h):
            terrain[_key(q, r)] = T_PLAIN

    # 森林斑块（15%~20%，簇状生长）
    f_lo, f_hi = hex_cfg.get("forest_pct") or [0.15, 0.20]
    forest_target = int(w * h * rng.uniform(f_lo, f_hi))
    forest_count = 0
    seeds = [(rng.randrange(w), rng.randrange(h)) for _ in range(4)]
    guard = 0
    while forest_count < forest_target and guard < forest_target * 10:
        guard += 1
        cq, cr = rng.choice(seeds)
        q, r = cq + rng.randint(-2, 2), cr + rng.randint(-2, 2)
        if not (0 <= q < w and 0 <= r < h):
            continue
        k = _key(q, r)
        if terrain[k] == T_PLAIN:
            terrain[k] = T_FOREST
            forest_count += 1

    # 沼泽 ≤8%（低洼靠森林）
    swamp_target = int(w * h * rng.uniform(0.02, float(hex_cfg.get("swamp_max_pct", 0.08))))
    placed = 0
    guard = 0
    while placed < swamp_target and guard < swamp_target * 10 + 10:
        guard += 1
        q, r = rng.randrange(w), rng.randrange(h)
        if terrain[_key(q, r)] == T_PLAIN:
            terrain[_key(q, r)] = T_SWAMP
            placed += 1

    # 高地 1~2 处（靠中央随机，小丘状 2~4 格）
    hl_lo, hl_hi = hex_cfg.get("highland_count") or [1, 2]
    for _ in range(rng.randint(hl_lo, hl_hi)):
        cq = rng.randint(int(w * 0.3), int(w * 0.7) - 1)
        cr = rng.randint(int(h * 0.3), max(int(h * 0.3) + 1, int(h * 0.7) - 1))
        size = rng.randint(2, 4)
        terrain[_key(cq, cr)] = T_HIGHLAND
        frontier = [(cq, cr)]
        for _i in range(size - 1):
            if not frontier:
                break
            bq, br = rng.choice(frontier)
            nb = [x for x in R.hex_neighbors(bq, br)
                  if 0 <= x[0] < w and 0 <= x[1] < h and terrain[_key(*x)] == T_PLAIN]
            if nb:
                nq, nr = rng.choice(nb)
                terrain[_key(nq, nr)] = T_HIGHLAND
                frontier.append((nq, nr))

    # 河流：1 条纵向渡河线（蜿蜒）；攻城图的城墙/护城河在对称性
    # 校验通过后统一放置（generate_map → _place_siege_structures），
    # 避免非对称人工结构干扰自然地形评分。
    if mode != "siege":
        river_q = rng.randint(int(w * 0.35), int(w * 0.65))
        drift = 0
        river_cells: list[tuple[int, int]] = []
        for r in range(h):
            drift = max(-2, min(2, drift + rng.randint(-1, 1)))
            q = river_q + drift
            if 0 <= q < w and terrain[_key(q, r)] in (T_PLAIN, T_SWAMP):
                terrain[_key(q, r)] = T_RIVER
                river_cells.append((q, r))
        # 渡河线：一处浅滩（可通行的平原桥位）
        if river_cells:
            fq, fr = rng.choice(river_cells)
            terrain[_key(fq, fr)] = T_PLAIN

    return terrain


def symmetry_score(terrain: dict[str, str], w: int, h: int,
                   ignorable: tuple[str, ...] = (T_WALL, T_GATE, T_MOAT)) -> float:
    """180° 旋转不对称度：非对称格对数 / 总格对数（城墙/门/壕不计入，
    它们本身按中轴对称生成）。"""
    total = 0
    mismatch = 0
    for q in range(w):
        for r in range(h):
            rq, rr = _rotate(q, r, w, h)
            if (q, r) >= (rq, rr):
                continue
            a, b = terrain.get(_key(q, r), T_PLAIN), terrain.get(_key(rq, rr), T_PLAIN)
            if a in ignorable and b in ignorable:
                continue
            total += 1
            if a != b:
                mismatch += 1
    if total == 0:
        return 0.0
    return mismatch / total


def _force_symmetry(terrain: dict[str, str], w: int, h: int) -> dict[str, str]:
    """兜底：把左半侧复制到旋转对称位，保证不对称度为 0。"""
    out = dict(terrain)
    for q in range(w):
        for r in range(h):
            rq, rr = _rotate(q, r, w, h)
            if (q, r) < (rq, rr):
                out[_key(rq, rr)] = out.get(_key(q, r), T_PLAIN)
    return out


def _place_siege_structures(terrain: dict[str, str], w: int, h: int) -> None:
    """攻城图人工结构（对称性评分后放置）：纵向城墙 + 双格城门 + 护城河。"""
    wall_q = int(w * 0.66)
    for r in range(h):
        terrain[_key(wall_q, r)] = T_WALL
    gate_r = h // 2
    terrain[_key(wall_q, gate_r)] = T_GATE
    if gate_r - 1 >= 0:
        terrain[_key(wall_q, gate_r - 1)] = T_GATE  # 双格门，避免独木桥
    for r in range(h):
        terrain[_key(wall_q - 1, r)] = T_MOAT


def generate_map(seed: str, mode: str = "field", hex_cfg: dict | None = None,
                 size: tuple[int, int] | None = None) -> dict[str, Any]:
    """服务端地图生成（seed = hash(battle_id)，§7.3）。

    不对称度 > tolerance 重掷 seed（最多 12 次），仍失败则强制镜像兜底；
    攻城图城墙/护城河在对称处理后放置（人工结构不参与评分）。
    返回 {"size", "terrain", "deploy_a", "deploy_b"}。
    """
    hex_cfg = hex_cfg or {}
    w, h = size or tuple(hex_cfg.get(f"{mode}_size") or MODE_SIZE.get(mode, (13, 9)))
    tol = float(hex_cfg.get("symmetry_tolerance", 0.10))
    terrain: dict[str, str] = {}
    for attempt in range(12):
        rng = random.Random(f"{seed}#{attempt}")
        terrain = _generate_terrain_once(rng, mode, w, h, hex_cfg)
        if symmetry_score(terrain, w, h) <= tol:
            break
    else:
        terrain = _force_symmetry(terrain, w, h)
    if mode == "siege":
        _place_siege_structures(terrain, w, h)
    depth = int(hex_cfg.get("deploy_depth", 3))
    deploy_a, deploy_b = deploy_zones(w, h, depth)
    return {"size": [w, h], "terrain": terrain,
            "deploy_a": deploy_a, "deploy_b": deploy_b}


def deploy_zones(w: int, h: int, depth: int = 3) -> tuple[list[list[int]], list[list[int]]]:
    """双方部署区：左右各 depth 列（旋转对称天然成立）。"""
    za = [[q, r] for q in range(min(depth, w)) for r in range(h)]
    zb = [[q, r] for q in range(max(0, w - depth), w) for r in range(h)]
    return za, zb


# ---------------------------------------------------------------------------
# session / unit 生命周期
# ---------------------------------------------------------------------------

def _cfg_num(cfg: dict | None, key: str, default: float,
             section: str = "battle") -> float:
    """会战配置读取：支持 cfg 直接给键、或完整 config 的 ``battle``/``hex`` 子块。"""
    cfg = cfg or {}
    for block in (cfg, cfg.get(section), cfg.get("hex")):
        if isinstance(block, dict) and key in block:
            try:
                return float(block[key])
            except (TypeError, ValueError):
                break
    return float(default)


def new_session(map_info: dict[str, Any], mode: str, seed: str,
                night: bool = False, hex_cfg: dict | None = None) -> dict[str, Any]:
    """创建会战 session（部署阶段）。"""
    hex_cfg = hex_cfg or {}
    return {
        "mode": mode,
        "seed": seed,
        "size": list(map_info["size"]),
        "terrain": dict(map_info["terrain"]),
        "temp_terrain": {},          # 水淹临时沼泽：{"q,r": expire_round}
        "deploy_zones": {"a": map_info["deploy_a"], "b": map_info["deploy_b"]},
        "units": [],
        "next_uid": 1,
        "phase": "deploy",           # deploy → battle → done
        "round": 0,
        "night": bool(night),
        "night_scout_mult": _cfg_num(hex_cfg, "night_scout_precision", 0.5),
        "night_buddha_morale": _cfg_num(hex_cfg, "night_buddha_morale", 5),
        "night_raid_loot_bonus": _cfg_num(hex_cfg, "night_raid_loot_bonus", 0.3),
        "night_buddha_applied": False,
        "order": [],                 # 本回合先手序 [uid...]
        "acted": [],                 # 本回合已行动 uid
        "statuses": {},              # uid → {burn: n, confuse: n, taunt_by: uid, bulwark: n, suppress: n, defending: bool}
        "ambushes": [],              # [{uid, q, r, revealed}]
        "ambush_max": int(hex_cfg.get("ambush_max", 2)),  # 每方设伏上限
        "duel": None,                # {challenger_uid, target_uid, state, deadline}
        "duel_counts": {},           # uid(str) → 本回合已发起单挑次数
        "duel_target_counts": {},    # uid(str) → 本场累计被挑战次数（v4.3.0）
        "duel_log": [],
        "commands": [],              # 回放指令流
        "events": [],                # 战斗叙事（round, text, kind）
        "winner": None,
        "encircle_rounds": {},       # uid → 被完全合围连续回合数
        "max_rounds": int(hex_cfg.get("max_manual_rounds", 30)),
        "round_seconds": int(hex_cfg.get("round_seconds", 30)),
        "result": None,
        "_rng_state": None,
    }


def make_unit(army_sim: dict, side: str, uid: int | None = None,
              formation: str = "") -> dict[str, Any]:
    """把 simulate_battle 输入格式的 army dict 转成 hex 战场单位。

    army_sim: {"id", "faction_id", "troop_type", "troops", "morale",
               "veteran", "leader", "siege_engine", ...}
    """
    leader = army_sim.get("leader") or None
    return {
        "uid": uid,
        "army_id": int(army_sim.get("id", 0)),
        "side": side,                # "a" 攻方 / "b" 守方
        "faction_id": int(army_sim.get("faction_id", 0)),
        "troop_type": army_sim.get("troop_type", "sword_shield"),
        "troops": int(army_sim.get("troops", 0)),
        "start_troops": int(army_sim.get("troops", 0)),
        "morale": int(army_sim.get("morale", 100)),
        "veteran": int(army_sim.get("veteran", 0)),
        "leader": leader,
        "siege_engine": bool(army_sim.get("siege_engine")),
        "pos_atk_mult": float(army_sim.get("pos_atk_mult", 1.0)),
        "pos": None,                 # [q, r]
        "mana": 0,
        "formation": formation,
        "zoc_break_used": False,
        "moved": False,
        "acted": False,
        "routed": False,
        "voluntary_retreat": False,  # 主动撤退（非溃败）：善后不得二次扣减
        "temp": False,               # 兽群突袭召唤物
        "temp_expire": 0,
        "defending": False,
        "injury_pool": int(army_sim.get("troops", 0)) * 0.2,  # 伤兵池（治疗上限）
    }


def add_unit(session: dict, army_sim: dict, side: str, formation: str = "") -> dict:
    unit = make_unit(army_sim, side, session["next_uid"], formation)
    session["next_uid"] += 1
    session["units"].append(unit)
    return unit


def get_unit(session: dict, uid: int) -> Optional[dict]:
    for u in session["units"]:
        if u["uid"] == int(uid):
            return u
    return None


def alive_units(session: dict, side: str | None = None) -> list[dict]:
    return [u for u in session["units"]
            if u["troops"] > 0 and not u["routed"]
            and (side is None or u["side"] == side)]


def foes_of(session: dict, unit: dict) -> list[dict]:
    return alive_units(session, "b" if unit["side"] == "a" else "a")


def unit_at(session: dict, q: int, r: int) -> Optional[dict]:
    for u in session["units"]:
        if u["pos"] and u["pos"][0] == q and u["pos"][1] == r \
                and u["troops"] > 0 and not u["routed"]:
            return u
    return None


def eff_terrain(session: dict, q: int, r: int) -> str:
    """含水淹临时沼泽的地形。"""
    k = _key(q, r)
    temp = session.get("temp_terrain") or {}
    if k in temp and int(temp[k]) >= int(session.get("round", 0)):
        return T_SWAMP
    return terrain_at(session, q, r)


# ---------------------------------------------------------------------------
# 部署（§7.3 部署阶段）
# ---------------------------------------------------------------------------

def valid_deploy(session: dict, uid: int, q: int, r: int) -> tuple[bool, str]:
    unit = get_unit(session, uid)
    if not unit:
        return False, "单位不存在"
    if session["phase"] != "deploy":
        return False, "不在部署阶段"
    if unit["pos"]:
        return False, "该军团已部署"
    zone = session["deploy_zones"].get(unit["side"]) or []
    if [q, r] not in zone and [int(q), int(r)] not in zone:
        return False, "只能部署在己方部署区"
    if not passable(session, q, r, unit["troop_type"]):
        return False, "该地形不可部署"
    if unit_at(session, q, r):
        return False, "该格已有军团"
    return True, ""


def place_unit(session: dict, uid: int, q: int, r: int) -> None:
    unit = get_unit(session, uid)
    unit["pos"] = [int(q), int(r)]
    _record(session, {"type": "deploy", "uid": uid, "q": q, "r": r,
                      "formation": unit.get("formation", "")})


def set_formation(session: dict, uid: int, formation: str) -> None:
    unit = get_unit(session, uid)
    if formation in FORMATIONS:
        unit["formation"] = formation
        _record(session, {"type": "formation", "uid": uid, "formation": formation})


def auto_deploy(session: dict, rng: random.Random,
                uids: Optional[set] = None) -> None:
    """托管部署：按军师智谋自动摆位 + 选阵型（§7.3）。

    智谋 ≥80：前排肉盾（重甲/剑盾）+ 后排远程错落 + 针对性阵型；
    智谋 ≥50：两列横排；其余：一列站满。
    uids 非 None 时仅部署指定 uid 的单位（玩家自助托管不越权代摆敌方）。
    """
    for side in ("a", "b"):
        units = [u for u in session["units"] if u["side"] == side and not u["pos"]
                 and (uids is None or u["uid"] in uids)]
        if not units:
            continue
        wisdom = max((int(((u.get("leader") or {}).get("stats") or {}).get("wisdom", 40))
                      for u in units), default=40)
        zone = [list(z) for z in (session["deploy_zones"].get(side) or [])]
        rng.shuffle(zone)
        # 按列组织：靠前（对敌方向）列给肉盾
        if side == "a":
            cols = sorted({z[0] for z in zone})
            front_cols = cols[-2:] if wisdom >= 80 else cols[-1:]
        else:
            cols = sorted({z[0] for z in zone}, reverse=True)
            front_cols = cols[-2:] if wisdom >= 80 else cols[-1:]
        melee = [u for u in units if R.TROOP_TYPES.get(u["troop_type"], {}).get("range", 0) == 0]
        ranged = [u for u in units if R.TROOP_TYPES.get(u["troop_type"], {}).get("range", 0) > 0]
        # 已手动落位/敌方先部署的单位占格必须排除，否则自动部署会叠格
        front_cells = [z for z in zone if z[0] in front_cols
                       and passable(session, z[0], z[1])
                       and not unit_at(session, z[0], z[1])]
        back_cells = [z for z in zone if z[0] not in front_cols
                      and passable(session, z[0], z[1])
                      and not unit_at(session, z[0], z[1])]
        rng.shuffle(front_cells)
        rng.shuffle(back_cells)
        for u in melee:
            if front_cells:
                u["pos"] = front_cells.pop()
            elif back_cells:
                u["pos"] = back_cells.pop()
        for u in ranged:
            if back_cells:
                u["pos"] = back_cells.pop()
            elif front_cells:
                u["pos"] = front_cells.pop()
        # 兜底：仍没位置的塞任意空格
        rest = [z for z in zone if passable(session, z[0], z[1]) and not unit_at(session, z[0], z[1])]
        for u in units:
            if not u["pos"] and rest:
                u["pos"] = rest.pop()
        # 部署区已满（军团数 > 可用格）：退化为可通行空格，避免残留
        # pos=None 的单位开战后不可被攻击。优先己方半场，其次全图，
        # 但始终排除敌方部署区（防止越权摆进对手出生点）。
        left = [u for u in units if not u["pos"]]
        if left:
            w, h = session["size"]
            foe_side = "b" if side == "a" else "a"
            foe_zone = {tuple(z) for z in (session["deploy_zones"].get(foe_side) or [])}
            all_cells = [(q, r) for q in range(w) for r in range(h)]
            rng.shuffle(all_cells)
            own_half = [c for c in all_cells if c not in foe_zone]
            for u in left:
                for q, r in own_half:
                    if passable(session, q, r, u["troop_type"]) and \
                            not unit_at(session, q, r):
                        u["pos"] = [q, r]
                        break
        # 阵型选择
        for u in units:
            if wisdom >= 80:
                if u["troop_type"] in ("heavy_armor", "sword_shield"):
                    u["formation"] = "fangyuan"
                elif u["troop_type"] == "flying_sword":
                    u["formation"] = "fengshi"
                elif R.TROOP_TYPES.get(u["troop_type"], {}).get("range", 0) > 0:
                    u["formation"] = "yanhang"
                else:
                    u["formation"] = "heyi"
            elif wisdom >= 50:
                u["formation"] = "fengshi" if u["troop_type"] == "flying_sword" else ""
    _record(session, {"type": "auto_deploy",
                      "uids": sorted(uids) if uids is not None else None})


def all_deployed(session: dict) -> bool:
    return all(u["pos"] for u in session["units"] if u["troops"] > 0)


# ---------------------------------------------------------------------------
# 设伏（§15 森林格 ≤2 处/场；侦察排雷由 manager 传 scouted）
# ---------------------------------------------------------------------------

def scout_precision(session: dict, side: str) -> float:
    """夜间侦察精度（§7.6 夜袭）：守方（b）减半，攻方不受影响。"""
    if not session.get("night"):
        return 1.0
    if side == "b":
        return max(0.0, min(1.0, float(session.get("night_scout_mult", 0.5))))
    return 1.0


def ambush_limit(session: dict, side: str) -> int:
    """每方设伏上限；夜间守方侦察精度下降 → 可用伏兵位按精度折算（至少 1）。"""
    base = int(session.get("ambush_max", 2) or 2)
    precision = scout_precision(session, side)
    if precision >= 1.0:
        return base
    return max(1, int(base * precision))


def night_buddha_morale_bonus(session: dict) -> int:
    """夜间防守首回合「晨钟」士气加成（§7.6）：佛修阵容 +N，无佛修则 0。"""
    if not session.get("night"):
        return 0
    bonus = int(session.get("night_buddha_morale", 5) or 0)
    if bonus <= 0:
        return 0
    if not any(u["side"] == "b" and (u.get("leader") or {}).get("profession") == "buddha"
               for u in session["units"] if u["troops"] > 0 and not u["routed"]):
        return 0
    return bonus


def apply_night_buddha_morale(session: dict) -> int:
    """首回合给守方全军加晨钟士气（同一场只结算一次）。"""
    if session.get("night_buddha_applied"):
        return 0
    session["night_buddha_applied"] = True
    if int(session.get("round", 0)) != 1:
        return 0
    bonus = night_buddha_morale_bonus(session)
    if bonus <= 0:
        return 0
    for u in session["units"]:
        if u["side"] == "b" and u["troops"] > 0 and not u["routed"]:
            u["morale"] = min(120, int(u["morale"]) + bonus)
    _log(session, f"🌙 晨钟暮鼓：守方佛修诵经，全军士气 +{bonus}", "night")
    return bonus


def night_loot_bonus(session: dict) -> float:
    """夜间魔修劫掠加成（§7.6）：攻方含魔修时返回 ``night_raid_loot_bonus``。"""
    if not session.get("night"):
        return 0.0
    has_demon = any(u["side"] == "a" and (u.get("leader") or {}).get("profession") == "demon"
                    for u in session["units"] if u["troops"] > 0 and not u["routed"])
    if not has_demon:
        return 0.0
    return float(session.get("night_raid_loot_bonus", 0.3) or 0.0)


def hidden_ambush_uids(session: dict) -> set[int]:
    """尚未触发的伏兵 uid 集合（观战/敌方视图须整支屏蔽，§15）。"""
    out: set[int] = set()
    for amb in session.get("ambushes") or []:
        if amb.get("revealed"):
            continue
        try:
            out.add(int(amb.get("uid", 0)))
        except (TypeError, ValueError):
            continue
    return out


def is_hidden_ambush(session: dict, unit: dict) -> bool:
    """单位是否为未揭示的伏兵（v4.3.0：对敌方完全不可见、不投射 ZOC）。"""
    if not unit:
        return False
    uid = int(unit.get("uid", -1))
    for amb in session.get("ambushes") or []:
        if not amb.get("revealed") and int(amb.get("uid", -2)) == uid:
            return True
    return False


def reveal_ambush(session: dict, uid: int, reason: str = "") -> bool:
    """主动揭示伏兵（攻击/技能/移动/受伤即暴露，§15）。

    v4.3.0：此前全库唯一揭示点是 check_ambush_trigger（敌军移动进 1 格内），
    伏兵自己攻击/放技能/移动/挨打都保持隐身 → 敌军 can_attack 收到
    「目标不存在」却永远找不到它，30 回合耗尽判守方胜（伏兵永生僵局）。
    """
    uid = int(uid)
    out = False
    for amb in session.get("ambushes") or []:
        if int(amb.get("uid", -1)) == uid and not amb.get("revealed"):
            amb["revealed"] = True
            out = True
    if out:
        u = get_unit(session, uid)
        text = f"👁 {leader_name(u) if u else '伏兵'} 的埋伏暴露了"
        if reason:
            text += f"（{reason}）"
        _log(session, text, "ambush")
    return out


def set_ambush(session: dict, uid: int, q: int, r: int) -> tuple[bool, str]:
    unit = get_unit(session, uid)
    if not unit:
        return False, "单位不存在"
    if session["phase"] != "deploy":
        return False, "只能在部署阶段设伏"
    if eff_terrain(session, q, r) != T_FOREST:
        return False, "只能在森林格设伏"
    # 只能由隐匿行军军团设伏（鬼修/御兽，§5.3/§15）
    prof = ((unit.get("leader") or {}).get("profession") or "")
    if prof not in ("ghost", "beast"):
        return False, "只有鬼修/御兽军团可设伏"
    max_ambush = ambush_limit(session, unit["side"])
    same_side = [a for a in session["ambushes"] if get_unit(session, a["uid"])
                 and get_unit(session, a["uid"])["side"] == unit["side"]]
    if len(same_side) >= max_ambush:
        return False, f"每方设伏至多 {max_ambush} 处"
    ok, reason = valid_deploy(session, uid, q, r)
    if not ok:
        return False, reason
    unit["pos"] = [int(q), int(r)]
    session["ambushes"].append({"uid": uid, "q": q, "r": r, "revealed": False})
    _record(session, {"type": "ambush", "uid": uid, "q": q, "r": r})
    return True, ""


def check_ambush_trigger(session: dict, mover: dict, to_q: int, to_r: int,
                         rng: random.Random) -> list[dict]:
    """敌军进入/紧邻伏击格 → 触发：伏方首回合先手、被伏方士气 -10。"""
    triggered = []
    for amb in session["ambushes"]:
        if amb["revealed"]:
            continue
        ambu = get_unit(session, amb["uid"])
        if not ambu or ambu["side"] == mover["side"] or ambu["routed"] or ambu["troops"] <= 0:
            continue
        if hex_dist((to_q, to_r), (amb["q"], amb["r"])) <= 1:
            amb["revealed"] = True
            mover["morale"] = max(0, mover["morale"] - 10)
            session.setdefault("ambush_initiative", []).append(ambu["uid"])
            triggered.append({"ambush_uid": ambu["uid"], "victim_uid": mover["uid"],
                              "text": f"⚠️ {leader_name(ambu)} 的伏兵杀出！"
                                      f"{leader_name(mover)} 所部士气 -10"})
            _log(session, f"伏击触发：{leader_name(ambu)} ⚔ {leader_name(mover)}", "ambush")
    return triggered


# ---------------------------------------------------------------------------
# ZOC（§7.4）
# ---------------------------------------------------------------------------

def hex_dist(a: tuple[int, int], b: tuple[int, int]) -> int:
    return R.hex_distance(a, b)


def _projects_zoc(session: dict, unit: dict) -> bool:
    """单位是否投射 ZOC：存活、未溃逃；站在河流格 ZOC 取消（§7.4）。"""
    if unit["troops"] <= 0 or unit["routed"] or not unit["pos"]:
        return False
    # v4.3.0：未揭示伏兵不投射 ZOC——否则敌方可通过「移动被 ZOC 截停/拦截
    # 战报出现伏兵将领姓名」反推伏击位置，隐身形同虚设。
    if is_hidden_ambush(session, unit):
        return False
    # 用 eff_terrain：水淹会把河流临时变为沼泽，此时该格不再属于「河流
    # 格 ZOC 取消」，须与 passable/移动消耗及前端 terrainAt 同口径。
    if eff_terrain(session, unit["pos"][0], unit["pos"][1]) == T_RIVER:
        return False
    return True


def zoc_tiles_of_side(session: dict, side: str) -> set[tuple[int, int]]:
    """某方全部 ZOC 格：相邻 6 hex，剔除河流格与己方军团所在格（友军通道）。"""
    tiles: set[tuple[int, int]] = set()
    friendly_pos = set()
    for u in session["units"]:
        if u["side"] == side and u["pos"] and u["troops"] > 0 and not u["routed"]:
            friendly_pos.add((u["pos"][0], u["pos"][1]))
    for u in session["units"]:
        if u["side"] != side or not _projects_zoc(session, u):
            continue
        for nq, nr in R.hex_neighbors(u["pos"][0], u["pos"][1]):
            if not in_bounds(session, nq, nr):
                continue
            if eff_terrain(session, nq, nr) == T_RIVER:
                continue  # 河流格 ZOC 取消（水淹成沼泽后不再取消）
            if (nq, nr) in friendly_pos:
                continue  # 己方军团所在格取消（友军通道）
            tiles.add((nq, nr))
    return tiles


def enemy_zoc_map(session: dict, unit: dict) -> dict[tuple[int, int], list[dict]]:
    """对 unit 而言的敌方 ZOC 格 → 控制它的敌军单位列表。"""
    foe_side = "b" if unit["side"] == "a" else "a"
    out: dict[tuple[int, int], list[dict]] = {}
    for u in session["units"]:
        if u["side"] != foe_side or not _projects_zoc(session, u):
            continue
        for nq, nr in R.hex_neighbors(u["pos"][0], u["pos"][1]):
            if not in_bounds(session, nq, nr):
                continue
            if eff_terrain(session, nq, nr) == T_RIVER:
                continue  # 水淹临时沼泽不再视为河流
            out.setdefault((nq, nr), []).append(u)
    return out


def is_fully_encircled(session: dict, unit: dict) -> bool:
    """完全合围：所有可通行邻格均被敌军 ZOC 覆盖或敌军占据。"""
    if not unit["pos"]:
        return False
    zmap = enemy_zoc_map(session, unit)
    for nq, nr in R.hex_neighbors(unit["pos"][0], unit["pos"][1]):
        if not in_bounds(session, nq, nr):
            continue  # 地图边界视为封锁
        if not passable(session, nq, nr, unit["troop_type"]):
            continue
        occupant = unit_at(session, nq, nr)
        if occupant and occupant["side"] == unit["side"]:
            return False  # 友军通道
        if (nq, nr) not in zmap and not occupant:
            return False
    return True


def _strip_path_start(path: list[list[int]],
                      cur: tuple[int, int]) -> list[list[int]]:
    """剔除路径开头的当前站位。

    前端寻路回溯出的 path 常包含起点格；服务端只接受「移动步骤」，
    若首格就是当前站位，``hex_dist(cur, path[0]) == 0`` 会被误判为
    “第1步不相邻”。这里统一在入口兼容两种传参。
    """
    steps = [[int(s[0]), int(s[1])] for s in path]
    if steps and (steps[0][0], steps[0][1]) == cur:
        steps = steps[1:]
    return steps


def check_move_path(session: dict, uid: int, path: list[list[int]],
                    use_zoc_break: bool = False) -> dict[str, Any]:
    """ZOC 移动校验（§7.4 刚性 ZOC + 森林半刚性 + 剑修突破）。

    返回 {"ok", "reason", "end"（实际可走到第几步）, "intercepts": [uid...],
          "need_break", "triggered_ambush_pos"}。
    刚性：进入敌 ZOC 格即停（该格成为终点）；
    森林格降级半刚性：允许穿越，但禁止 ZOC 格 → ZOC 格直通；
    剑修每场 1 次突破：可无视一次「进入即停」。
    离开敌 ZOC 格 → 控制方获得一次拦截攻击。
    """
    unit = get_unit(session, uid)
    res: dict[str, Any] = {"ok": False, "reason": "", "end": 0, "intercepts": [],
                           "need_break": False, "cost": 0.0}
    if not unit or not unit["pos"]:
        res["reason"] = "单位未部署"
        return res
    if session["phase"] != "battle":
        res["reason"] = "不在战斗阶段"
        return res
    if unit["acted"]:
        res["reason"] = "本回合已行动"
        return res
    if unit["troops"] <= 0 or unit["routed"]:
        res["reason"] = "军团无法行动"
        return res
    if unit["moved"]:
        res["reason"] = "本回合已移动"
        return res
    zmap = enemy_zoc_map(session, unit)
    mp = move_points(unit, session)
    is_forest_free = unit["troop_type"] == "beast_horde"  # 兽群森林不减机动
    is_flying = unit["troop_type"] == "flying_sword"
    can_break = (not unit["zoc_break_used"] and use_zoc_break
                 and (unit.get("leader") or {}).get("profession") == "sword")
    cur = (unit["pos"][0], unit["pos"][1])
    # 兼容带起点格的路径（前端/旧客户端）：统一剥离后再做相邻校验
    path = _strip_path_start(path, cur)
    spent = 0.0
    end = 0
    intercepts: list[int] = []
    for i, step in enumerate(path):
        nq, nr = int(step[0]), int(step[1])
        if hex_dist(cur, (nq, nr)) != 1:
            res["reason"] = f"第{i + 1}步不相邻"
            res["end"] = end
            return res
        if not passable(session, nq, nr, unit["troop_type"]):
            res["reason"] = f"第{i + 1}步地形不可通行"
            res["end"] = end
            return res
        if unit_at(session, nq, nr):
            res["reason"] = f"第{i + 1}步有军团占位"
            res["end"] = end
            return res
        t = eff_terrain(session, nq, nr)
        cost = TERRAIN_MOVE_COST.get(t, 1)
        if is_forest_free and t == T_FOREST:
            cost = 1
        if is_flying and t == T_PLAIN:
            cost = max(0.5, cost - 0.5)  # 飞剑平原机动 +（消耗 -0.5）
        if spent + cost > mp + 1e-6:
            res["reason"] = "机动力不足"
            res["end"] = end
            res["cost"] = spent
            return res
        # 拦截：从敌 ZOC 格离开（当前格在 ZOC 内且下一步离开控制源邻接）
        if cur in zmap:
            leaving = not any((nq, nr) in R.hex_neighbors(cu["pos"][0], cu["pos"][1])
                              for cu in zmap[cur])
            if leaving:
                for controller in zmap[cur]:
                    intercepts.append(controller["uid"])
        # 进入敌 ZOC 格
        if (nq, nr) in zmap:
            semi = (t == T_FOREST)  # 森林半刚性
            if semi:
                if cur in zmap:
                    res["reason"] = "森林半刚性 ZOC：禁止 ZOC 格间直通"
                    res["end"] = end
                    res["cost"] = spent
                    return res
            else:
                # 刚性：进入即停
                if can_break:
                    res["need_break"] = True
                    can_break = False  # 一次性突破：后续刚性 ZOC 仍进入即停
                else:
                    spent += cost
                    end = i + 1
                    res.update({"ok": True, "reason": "zoc_stop", "end": end,
                                "intercepts": intercepts, "cost": spent})
                    return res
        spent += cost
        cur = (nq, nr)
        end = i + 1
    res.update({"ok": True, "reason": "", "end": end, "intercepts": intercepts,
                "cost": spent})
    return res


def do_move(session: dict, uid: int, path: list[list[int]], rng: random.Random,
            use_zoc_break: bool = False) -> dict[str, Any]:
    """执行移动（含 ZOC 截停/拦截攻击/伏击触发），追加回放指令。"""
    check = check_move_path(session, uid, path, use_zoc_break)
    if not check["ok"] and check["reason"] != "机动力不足" and check["end"] == 0:
        return check
    unit = get_unit(session, uid)
    # 与 check_move_path 同口径地剥离起点，保证 steps 切片的索引一致
    if unit and unit.get("pos"):
        path = _strip_path_start(
            path, (int(unit["pos"][0]), int(unit["pos"][1])))
    # 统一按校验结果截断执行：ZOC 突破路径同样受机动力上限约束（end 为
    # 校验通过的最后一步；机动力不足时为截断点，全部通过时 = len(path)）
    steps = [list(s) for s in path[:check["end"]]]
    if check.get("need_break") and use_zoc_break:
        unit["zoc_break_used"] = True
        check["reason"] = "zoc_break"
    if not steps:
        return check
    _record(session, {"type": "move", "uid": uid, "path": steps,
                      "break": bool(check.get("need_break") and use_zoc_break)})
    ambush_events: list[dict] = []
    for s in steps:
        unit["pos"] = [int(s[0]), int(s[1])]
        ambush_events.extend(check_ambush_trigger(session, unit, int(s[0]), int(s[1]), rng))
    # v4.3.0：伏兵自己移动即暴露（此前唯一揭示点是敌军靠近触发，
    # 伏兵可全程隐身游走，敌方无法反击）
    reveal_ambush(session, uid, reason="移动")
    unit["moved"] = True
    # 拦截攻击（体修锁定型 ×1.5）
    for icp_uid in check.get("intercepts") or []:
        icp = get_unit(session, icp_uid)
        if not icp or icp["troops"] <= 0 or icp["routed"]:
            continue
        mult = 1.5 if (icp.get("leader") or {}).get("profession") == "body" else 1.0
        atk_pow = R.attack_power(_sim_view(session, icp), 1.0, session["night"]) \
            * 0.5 * mult
        dfn_pow = R.defense_power(_sim_view(session, unit))
        dmg = R.damage_curve(atk_pow, dfn_pow, unit["troops"])
        icp_st = (session.get("statuses") or {}).get(str(icp["uid"])) or {}
        if int(icp_st.get("bashan", 0) or 0) > 0:
            dmg = int(round(dmg * 1.5))  # 搬山阵：3 回合拦截伤害 +50%（§7.5）
        _apply_damage(session, unit, dmg, rng, source=icp, kind="intercept")
        _log(session, f"🛡 {leader_name(icp)} 拦截 {leader_name(unit)}（-{dmg} 兵）", "intercept")
    check["ambush"] = ambush_events
    return check


# ---------------------------------------------------------------------------
# 攻击结算（hex 版，§7.4/§7.6）
# ---------------------------------------------------------------------------

def _sim_view(session: dict, unit: dict) -> dict:
    """hex 单位 → strategy_rules 的 army dict 视图（含地形/阵型系数）。"""
    q, r = unit["pos"] if unit["pos"] else (0, 0)
    t = eff_terrain(session, q, r)
    atk_m = TERRAIN_ATK_MULT.get(t, 1.0)
    def_m = TERRAIN_DEF_MULT.get(t, 1.0)
    st = session["statuses"].get(str(unit["uid"])) or {}
    if st.get("bulwark", 0) > 0:
        def_m *= 1.0 + SKILLS["bulwark"]["def_bonus"]
    if st.get("shieldwall"):
        def_m *= 1.15  # 盾墙：静止防御 +15%（§7.5）
    if unit.get("defending"):
        def_m *= 1.3
    if t in (T_WALL, T_GATE):
        unit_wall = 2.5 if t == T_WALL else 1.8
        def_m = max(def_m, unit_wall)  # 城墙守军
    formation = unit.get("formation", "")
    return {
        "id": unit["army_id"], "uid": unit["uid"], "faction_id": unit["faction_id"],
        "troop_type": unit["troop_type"], "troops": unit["troops"],
        "morale": unit["morale"], "veteran": unit["veteran"],
        "leader": unit.get("leader"), "siege_engine": unit.get("siege_engine"),
        "pos_atk_mult": float(unit.get("pos_atk_mult", 1.0)),
        "atk_terrain_mult": atk_m * FORMATION_ATK.get(formation, 1.0),
        "def_terrain_mult": def_m * FORMATION_DEF.get(formation, 1.0),
        "wall_mult": 1.0,
        # 只有攻城战（或真正站在城墙/城门格）才算守城方；野战/关隘战
        # 的 b 方不再无条件吃「守城专精 ×1.15 / 铁壁 ×1.20」。
        "defending_node": unit["side"] == "b" and (
            session.get("mode") == "siege"
            or t in (T_WALL, T_GATE)),
    }


def leader_name(unit: dict) -> str:
    # 召唤物没有主将，但 army_id 为负（与 NPC 守军同域），不能笼统显示
    # 「无主军团」，否则战报/行动提示会把己方兽群误标成守军。
    if unit.get("temp"):
        return "兽群"
    return (unit.get("leader") or {}).get("name") or "无主军团"


def attack_range(unit: dict, session: dict) -> int:
    troop = R.TROOP_TYPES.get(unit.get("troop_type", ""), {})
    rng_ = int(troop.get("range", 0))
    st = (session.get("statuses") or {}).get(str(unit.get("uid"))) or {}
    if rng_ > 0 and int(st.get("range_buff", 0) or 0) > 0:
        rng_ += 1  # 咒唱：3 回合全军射程 +1（§7.5）
    if rng_ > 0 and unit["pos"]:
        t = eff_terrain(session, unit["pos"][0], unit["pos"][1])
        if t in (T_HIGHLAND, T_WALL, T_GATE):
            rng_ += 1  # 高地射程 +1（§7.5 法修营）
    return rng_


def adjacent_enemy_count(session: dict, target: dict) -> int:
    """受击时邻格敌军数（夹击/合围判定，§7.4）。

    v4.3.0：未揭示伏兵不计入——隐身单位若贡献夹击加成，受击方会从
    异常高的伤害反推出「邻格藏有伏兵」，构成信息泄露。
    """
    if not target["pos"]:
        return 0
    n = 0
    for u in foes_of(session, target):
        if is_hidden_ambush(session, u):
            continue
        if u["pos"] and hex_dist((u["pos"][0], u["pos"][1]),
                                 (target["pos"][0], target["pos"][1])) == 1:
            n += 1
    # 鹤翼阵：包抄判定 +1 方向（2 格内有鹤翼友军视为多一面包抄）
    for u in session["units"]:
        if u["side"] != target["side"] and u is not target \
                and u["troops"] > 0 and not u["routed"] \
                and not is_hidden_ambush(session, u) \
                and u.get("formation") == "heyi" and u["pos"] and target["pos"] \
                and hex_dist((u["pos"][0], u["pos"][1]),
                             (target["pos"][0], target["pos"][1])) <= 2:
            n += 1
            break
    return n


def can_attack(session: dict, attacker: dict, target: dict) -> tuple[bool, str]:
    if not attacker or not target:
        return False, "单位不存在"
    if session["phase"] != "battle":
        return False, "不在战斗阶段"
    if attacker["acted"]:
        return False, "本回合已行动"
    if attacker["troops"] <= 0 or attacker["routed"]:
        return False, "军团无法行动"
    if not attacker["pos"] or not target["pos"]:
        return False, "未完成部署"
    # v4.3.0：敌方未揭示伏兵统一报「单位不存在」——此前不同错误文案
    # （友军/失去战斗力/超出攻击距离 d>r）构成 uid oracle，可逐格探测
    # 伏兵位置甚至精确距离。
    if target["side"] != attacker["side"] and is_hidden_ambush(session, target):
        return False, "目标不存在"
    if target["side"] == attacker["side"]:
        return False, "不能攻击友军"
    if target["troops"] <= 0 or target["routed"]:
        return False, "目标已失去战斗力"
    dist = hex_dist((attacker["pos"][0], attacker["pos"][1]),
                    (target["pos"][0], target["pos"][1]))
    rng_ = max(1, attack_range(attacker, session))
    if dist > rng_:
        return False, f"超出攻击距离（{dist}>{rng_}）"
    return True, ""


def do_attack(session: dict, atk_uid: int, dfn_uid: int, rng: random.Random) -> dict[str, Any]:
    """hex 攻击结算：克制 × 地形 × 阵型 × 夹击/合围 × 减伤曲线（§7.4/§7.6）。"""
    attacker = get_unit(session, atk_uid)
    target = get_unit(session, dfn_uid)
    if not attacker or not target:
        return {"ok": False, "reason": "目标不存在"}
    ok, reason = can_attack(session, attacker, target)
    if not ok:
        return {"ok": False, "reason": reason}
    # v4.3.0：伏兵出手即暴露（攻击是最强的揭示行为）
    reveal_ambush(session, atk_uid, reason="主动出击")
    _record(session, {"type": "attack", "uid": atk_uid, "target": dfn_uid})
    return _resolve_attack(session, attacker, target, rng)


def _resolve_attack(session: dict, attacker: dict, target: dict,
                    rng: random.Random, damage_mult: float = 1.0,
                    kind: str = "attack") -> dict[str, Any]:
    """通用伤害管线（普攻/合击/拦截复用）。"""
    flank_n = adjacent_enemy_count(session, target)
    f_mult = R.flank_factor(flank_n)
    atk_view = _sim_view(session, attacker)
    dfn_view = _sim_view(session, target)
    is_ranged = attack_range(attacker, session) > 1
    atk_pow = R.attack_power(atk_view, f_mult, session["night"])
    if is_ranged:
        atk_pow *= FORMATION_RANGED.get(attacker.get("formation", ""), 1.0)
    counter = R.counter_factor(attacker["troop_type"], target["troop_type"])
    dfn_pow = R.defense_power(dfn_view)
    dmg = int(R.damage_curve(atk_pow * counter * damage_mult, dfn_pow, target["troops"]) )
    _apply_damage(session, target, dmg, rng, source=attacker, kind=kind)
    event = {"ok": True, "kind": kind, "attacker": attacker["uid"],
             "target": target["uid"], "damage": dmg, "flank": f_mult,
             "counter": counter, "target_troops": target["troops"],
             "target_morale": target["morale"]}
    # 近战且未溃 → 目标反击 50%（远程不遭反击）
    if not is_ranged and target["troops"] > 0 and not target["routed"]:
        dist = hex_dist((attacker["pos"][0], attacker["pos"][1]),
                        (target["pos"][0], target["pos"][1]))
        if dist <= 1:
            back_pow = R.attack_power(_sim_view(session, target), 1.0, session["night"]) * 0.5
            back = R.damage_curve(back_pow, R.defense_power(_sim_view(session, attacker)),
                                  attacker["troops"])
            _apply_damage(session, attacker, back, rng, source=target, kind="counter")
            event["counter_damage"] = back
            _log(session, f"⚔ {leader_name(attacker)} 攻 {leader_name(target)}"
                          f"（-{dmg}），遭反击 -{back}", kind)
        else:
            _log(session, f"🏹 {leader_name(attacker)} 射击 {leader_name(target)}（-{dmg}）", kind)
    else:
        _log(session, f"⚔ {leader_name(attacker)} 攻 {leader_name(target)}（-{dmg}）", kind)
    attacker["acted"] = True
    attacker["moved"] = True
    event["routed"] = target["routed"]
    return event


def _apply_damage(session: dict, unit: dict, dmg: int, rng: random.Random,
                  source: dict | None = None, kind: str = "") -> None:
    """掉兵 + 士气 + 灵力(受击+5) + 溃逃判定 + 主将险情。"""
    dmg = max(0, int(dmg))
    if dmg <= 0:
        return
    st = (session.get("statuses") or {}).get(str(unit["uid"])) or {}
    if st.get("shieldwall"):
        dmg = int(dmg * 0.5)  # 盾墙列阵：本回合受伤 -50%（§7.5 剑盾专属）
        if dmg <= 0:
            return
    # v4.3.0：隐藏伏兵受伤即暴露（AoE/火攻战报本就含其姓名，不揭示反而
    # 造成「看不见却挨打」的矛盾状态）
    reveal_ambush(session, int(unit.get("uid", 0)), reason="中箭受创")
    before = unit["troops"]
    unit["troops"] = max(0, unit["troops"] - dmg)
    unit["injury_pool"] = float(unit.get("injury_pool", 0)) + dmg * 0.5
    loss_pct = dmg / max(1, before)
    unit["morale"] = max(0, int(unit["morale"] - loss_pct * 40))
    unit["mana"] = min(MANA_MAX, int(unit.get("mana", 0)) + MANA_ON_HIT)
    # 主将险情：兵力 <20% 起始 时 10% 伤势 +15（§7.6）
    if unit["troops"] > 0 and unit["troops"] < unit["start_troops"] * 0.2 \
            and rng.random() < 0.10 and unit.get("leader"):
        session.setdefault("peril_events", []).append(
            {"army_id": unit["army_id"], "injury": 15,
             "round": session["round"], "kind": kind or "battle"})
    # 溃逃判定：士气 <30（§7.6）
    if unit["morale"] < 30 and unit["troops"] > 0 and not unit["routed"]:
        if rng.random() < (30 - unit["morale"]) / 30 * 0.5 + 0.25:
            unit["routed"] = True
            _log(session, f"🏳 {leader_name(unit)} 所部士气崩溃，溃逃！", "rout")


# ---------------------------------------------------------------------------
# 技能（§7.7）
# ---------------------------------------------------------------------------

def unit_skills(session: dict, unit: dict) -> list[str]:
    """单位可用技能列表（职业/职责/器械/羁绊/适性 S）。"""
    leader = unit.get("leader") or {}
    prof = leader.get("profession", "")
    role = leader.get("role", "")
    out = []
    for sid, sk in SKILLS.items():
        if sid == "ram":
            if unit.get("siege_engine"):
                out.append(sid)
            continue
        if sid == "combo":
            if _has_sworn_partner(session, unit):
                out.append(sid)
            continue
        if sk["professions"] and prof not in sk["professions"]:
            continue
        if sk["roles"] and role not in sk["roles"]:
            continue
        out.append(sid)
    # S 级适性兵种专属技能（每场 1 次，§7.5）
    if R.has_aptitude_skill(unit.get("leader"), unit["troop_type"]):
        out.append("troop_special")
    return out


def _has_sworn_partner(session: dict, unit: dict) -> bool:
    leader = unit.get("leader") or {}
    bonds = leader.get("bonds") or {}
    for u in session["units"]:
        if u is unit or u["side"] != unit["side"] or not u.get("leader"):
            continue
        b = bonds.get(str(u["leader"].get("id"))) or {}
        if b.get("type") == "yijiao":  # 义交羁绊对（sworn）
            return True
    return False


def do_skill(session: dict, uid: int, skill_id: str, rng: random.Random,
             target_uid: int | None = None, target_hex: list[int] | None = None) -> dict[str, Any]:
    """释放技能。target_uid / target_hex 按技能 target 类型二选一。"""
    unit = get_unit(session, uid)
    if not unit or unit["troops"] <= 0 or unit["routed"]:
        return {"ok": False, "reason": "军团无法行动"}
    if session["phase"] != "battle":
        return {"ok": False, "reason": "不在战斗阶段"}
    if unit["acted"]:
        return {"ok": False, "reason": "本回合已行动"}
    sk = SKILLS.get(skill_id)
    if skill_id == "troop_special":
        # 兵种专属技能同样要求 S 级适性（unit_skills 已按适性注入该键）
        if skill_id not in unit_skills(session, unit):
            return {"ok": False, "reason": "该军团无 S 级兵种适性"}
        return _troop_special(session, unit, rng, target_uid)
    if not sk:
        return {"ok": False, "reason": "未知技能"}
    if skill_id not in unit_skills(session, unit):
        return {"ok": False, "reason": "该军团不掌握此技能"}
    mana_cost = int(sk.get("mana", 0))
    if int(unit.get("mana", 0)) < mana_cost:
        return {"ok": False, "reason": f"灵力不足（{unit.get('mana', 0)}/{mana_cost}）"}
    if sk.get("need_highland") and unit["pos"]:
        t = eff_terrain(session, unit["pos"][0], unit["pos"][1])
        if t not in (T_HIGHLAND, T_WALL, T_GATE):
            return {"ok": False, "reason": "需在高地/城墙上释放"}
    if sk.get("need_river") and unit["pos"]:
        near = any(eff_terrain(session, nq, nr) == T_RIVER
                   for nq, nr in R.hex_neighbors(unit["pos"][0], unit["pos"][1]))
        if not near:
            return {"ok": False, "reason": "需邻接河流"}
    handler = {
        "fire": _skill_fire, "flood": _skill_flood, "confuse": _skill_confuse,
        "taunt": _skill_taunt, "bulwark": _skill_bulwark, "heal": _skill_heal,
        "beastrush": _skill_beastrush, "barrage": _skill_barrage,
        "ram": _skill_ram, "combo": _skill_combo,
    }.get(skill_id)
    if not handler:
        return {"ok": False, "reason": "技能未实现"}
    res = handler(session, unit, sk, rng, target_uid, target_hex)
    if not res.get("ok"):
        # 施放失败：不扣灵力、不消耗行动、不进指令流（回放不含无效动作）
        return res
    # v4.3.0：伏兵施法即暴露
    reveal_ambush(session, uid, reason="施展法术")
    _record(session, {"type": "skill", "uid": uid, "skill": skill_id,
                      "target_uid": target_uid, "target_hex": target_hex})
    unit["mana"] = int(unit.get("mana", 0)) - mana_cost
    unit["acted"] = True
    unit["moved"] = True
    return res


def _in_skill_range(session: dict, unit: dict, sk: dict, q: int, r: int) -> bool:
    if not unit["pos"]:
        return False
    return hex_dist((unit["pos"][0], unit["pos"][1]), (q, r)) <= int(sk.get("range", 99))


def _skill_fire(session, unit, sk, rng, target_uid, target_hex):
    """火攻：目标 hex + 邻格灼烧 2 回合（森林 ×1.5、烧器械）。"""
    if not target_hex:
        return {"ok": False, "reason": "需指定目标格"}
    q, r = int(target_hex[0]), int(target_hex[1])
    if not in_bounds(session, q, r) or not _in_skill_range(session, unit, sk, q, r):
        return {"ok": False, "reason": "超出施放范围"}
    burned = []
    for hq, hr in [(q, r)] + R.hex_neighbors(q, r):
        if not in_bounds(session, hq, hr):
            continue
        victim = unit_at(session, hq, hr)
        if not victim or victim["side"] == unit["side"]:
            continue
        mult = sk["forest_mult"] if eff_terrain(session, hq, hr) == T_FOREST else 1.0
        fire_mult = float(R.trait_army_mods(
            ((unit.get("leader") or {}).get("traits")) or [])["fire_mult"])
        dmg = int((victim["troops"] * 0.04 * mult + rng.randint(10, 40)) * fire_mult)
        if victim.get("siege_engine"):
            dmg = int(dmg * 1.5)  # 烧器械
            victim["siege_engine"] = False
        _apply_damage(session, victim, dmg, rng, source=unit, kind="fire")
        session["statuses"].setdefault(str(victim["uid"]), {})["burn"] = int(sk["burn_rounds"])
        burned.append({"uid": victim["uid"], "damage": dmg})
    _log(session, f"🔥 {leader_name(unit)} 火攻覆盖 ({q},{r})，{len(burned)} 支敌军被点燃", "skill")
    return {"ok": True, "skill": "fire", "burned": burned}


def _skill_flood(session, unit, sk, rng, target_uid, target_hex):
    """水淹：目标下游 3 格变临时沼泽。"""
    if not target_hex:
        return {"ok": False, "reason": "需指定目标格"}
    q, r = int(target_hex[0]), int(target_hex[1])
    if not in_bounds(session, q, r) or not _in_skill_range(session, unit, sk, q, r):
        return {"ok": False, "reason": "超出施放范围"}
    flooded = []
    cq, cr = q, r
    for _ in range(3):  # 向地图下方（下游）蔓延
        k = _key(cq, cr)
        if in_bounds(session, cq, cr) and terrain_at(session, cq, cr) not in (T_WALL, T_GATE):
            # v4.3.0：eff_terrain 判定为 expire >= round（含施放当回合），
            # 此前 +swamp_rounds 使 3 回合沼泽实际持续 4 回合（R..R+3）
            session["temp_terrain"][k] = session["round"] + int(sk["swamp_rounds"]) - 1
            flooded.append([cq, cr])
        cr += 1
    _log(session, f"🌊 {leader_name(unit)} 水淹七军！{len(flooded)} 格化为沼泽", "skill")
    return {"ok": True, "skill": "flood", "flooded": flooded}


def _skill_confuse(session, unit, sk, rng, target_uid, target_hex):
    """惑敌：目标下回合随机行动（可打友军，由 AI/指令层校验豁免）。"""
    target = get_unit(session, target_uid) if target_uid else None
    if not target or target["side"] == unit["side"]:
        return {"ok": False, "reason": "目标无效"}
    if target["troops"] <= 0 or target["routed"]:
        return {"ok": False, "reason": "目标已失去战斗力"}
    if not _unit_in_range(session, unit, target, sk):
        return {"ok": False, "reason": "超出施放范围"}
    if (target.get("leader") or {}).get("profession") == sk.get("immune_profession", "beast"):
        _log(session, f"👻 惑敌被 {leader_name(target)} 的兽群直觉识破！", "skill")
        return {"ok": True, "skill": "confuse", "immune": True}
    st = session["statuses"].setdefault(str(target["uid"]), {})
    st["confuse"] = 2
    st["confuse_from"] = int(session.get("round", 0))  # 下一回合起生效
    _log(session, f"👻 {leader_name(unit)} 惑敌成功，{leader_name(target)} 下回合敌我不分", "skill")
    return {"ok": True, "skill": "confuse"}


def _skill_taunt(session, unit, sk, rng, target_uid, target_hex):
    """挑衅：强制目标下回合攻击自己（ZOC 锁位配合）。"""
    target = get_unit(session, target_uid) if target_uid else None
    if not target or target["side"] == unit["side"]:
        return {"ok": False, "reason": "目标无效"}
    if target["troops"] <= 0 or target["routed"]:
        return {"ok": False, "reason": "目标已失去战斗力"}
    if not _unit_in_range(session, unit, target, sk):
        return {"ok": False, "reason": "超出施放范围"}
    st = session["statuses"].setdefault(str(target["uid"]), {})
    st["taunt_by"] = unit["uid"]
    st["taunt_ttl"] = 2  # 覆盖目标的下一个行动回合
    # 键名必须是 taunt_by_from：_status_active(unit, "taunt_by") 读取的是
    # key + "_from"。此前写成 taunt_from，导致挑衅在施放当回合立即生效
    # （甚至可强迫本回合稍后行动的目标），与「下一回合起生效」不符。
    st["taunt_by_from"] = int(session.get("round", 0))
    _log(session, f"🗡 {leader_name(unit)} 阵前挑衅，{leader_name(target)} 怒不可遏", "skill")
    return {"ok": True, "skill": "taunt"}


def _skill_bulwark(session, unit, sk, rng, target_uid, target_hex):
    """坚守阵：3 回合防 +40%、ZOC 锁定型（拦截 ×1.5）。"""
    session["statuses"].setdefault(str(unit["uid"]), {})["bulwark"] = int(sk["rounds"])
    _log(session, f"🛡 {leader_name(unit)} 布下坚守阵（防 +40%，3 回合）", "skill")
    return {"ok": True, "skill": "bulwark"}


def _skill_heal(session, unit, sk, rng, target_uid, target_hex):
    """治疗术：兵力 +5%（上限伤兵池）、角色伤势 -5。"""
    target = get_unit(session, target_uid) if target_uid else unit
    if not target:
        return {"ok": False, "reason": "治疗目标不存在"}
    if target["side"] != unit["side"]:
        return {"ok": False, "reason": "只能治疗友军"}
    if target["troops"] <= 0 or target["routed"]:
        return {"ok": False, "reason": "目标已失去战斗力"}
    if not _unit_in_range(session, unit, target, sk, allow_self=True):
        return {"ok": False, "reason": "超出施放范围"}
    headroom = max(0.0, min(float(target.get("injury_pool", 0)),
                            target["start_troops"] - target["troops"]))
    heal = min(int(target["troops"] * sk["heal_pct"]), int(headroom))
    target["troops"] += max(0, heal)
    target["injury_pool"] = max(0.0, float(target.get("injury_pool", 0)) - heal)
    if target.get("leader"):
        before = int(target["leader"].get("injury", 0))
        after = max(0, before - int(sk["injury_heal"]))
        target["leader"]["injury"] = after
        lid = target["leader"].get("id")
        if lid and after < before:
            # 引擎只记录待写回值，由 manager 落库到 strategy_characters
            session.setdefault("_injury_heals", {})[str(int(lid))] = after
    _log(session, f"💚 {leader_name(unit)} 治疗 {leader_name(target)}（+{max(0, heal)} 兵）", "skill")
    return {"ok": True, "skill": "heal", "healed": max(0, heal)}


def _skill_beastrush(session, unit, sk, rng, target_uid, target_hex):
    """兽群突袭：召唤临时兽群（500 兵、3 回合）。"""
    if not target_hex:
        return {"ok": False, "reason": "需指定召唤格"}
    q, r = int(target_hex[0]), int(target_hex[1])
    if not _in_skill_range(session, unit, sk, q, r):
        return {"ok": False, "reason": "超出施放范围"}
    if not passable(session, q, r, "beast_horde") or unit_at(session, q, r):
        return {"ok": False, "reason": "该格无法容纳兽群"}
    summon = make_unit({"id": -(session["next_uid"] + 10000), "faction_id": unit["faction_id"],
                        "troop_type": "beast_horde", "troops": int(sk["summon_troops"]),
                        "morale": 100, "veteran": 0, "leader": None,
                        "siege_engine": False}, unit["side"], session["next_uid"])
    session["next_uid"] += 1
    summon["pos"] = [q, r]
    summon["temp"] = True
    summon["temp_expire"] = session["round"] + int(sk["rounds"])
    summon["start_troops"] = int(sk["summon_troops"])
    session["units"].append(summon)
    _log(session, f"🐺 {leader_name(unit)} 召唤兽群突袭（{sk['summon_troops']} 兵，3 回合）", "skill")
    return {"ok": True, "skill": "beastrush", "uid": summon["uid"]}


def _skill_barrage(session, unit, sk, rng, target_uid, target_hex):
    """落石/箭雨：hex 半径 1 范围压制（伤害 + 机动减半 1 回合）。"""
    if not target_hex:
        return {"ok": False, "reason": "需指定目标格"}
    q, r = int(target_hex[0]), int(target_hex[1])
    if not in_bounds(session, q, r) or not _in_skill_range(session, unit, sk, q, r):
        return {"ok": False, "reason": "超出施放范围"}
    hits = []
    for hq, hr in [(q, r)] + R.hex_neighbors(q, r):
        if not in_bounds(session, hq, hr):
            continue
        victim = unit_at(session, hq, hr)
        if not victim or victim["side"] == unit["side"]:
            continue
        dmg = int(victim["troops"] * 0.03) + rng.randint(5, 30)
        _apply_damage(session, victim, dmg, rng, source=unit, kind="barrage")
        vst = session["statuses"].setdefault(str(victim["uid"]), {})
        vst["suppress"] = 2
        vst["suppress_from"] = int(session.get("round", 0))  # 下一回合起生效
        hits.append({"uid": victim["uid"], "damage": dmg})
    _log(session, f"🪨 {leader_name(unit)} 落石箭雨覆盖 ({q},{r})，{len(hits)} 支敌军被压制", "skill")
    return {"ok": True, "skill": "barrage", "hits": hits}


def _skill_ram(session, unit, sk, rng, target_uid, target_hex):
    """破城槌撞击：城墙 ×3 伤害（作用于攻城图城防值，session["wall_hp"]）。"""
    if session["mode"] != "siege":
        return {"ok": False, "reason": "仅攻城战可用"}
    if not unit["pos"]:
        return {"ok": False, "reason": "未部署"}
    adj_wall = [n for n in R.hex_neighbors(unit["pos"][0], unit["pos"][1])
                if in_bounds(session, *n)
                and terrain_at(session, *n) in (T_GATE, T_WALL)]
    if not adj_wall:
        return {"ok": False, "reason": "需邻接城门/城墙"}
    dmg = int(unit["troops"] / 100 * 30 * sk["wall_mult"])
    session["wall_hp"] = max(0, int(session.get("wall_hp", 0)) - dmg)
    _log(session, f"🪵 破城槌撞击城门！（城防 -{dmg}，余 {session['wall_hp']}）", "skill")
    unit["acted"] = True
    unit["moved"] = True
    return {"ok": True, "skill": "ram", "wall_damage": dmg}


def _skill_combo(session, unit, sk, rng, target_uid, target_hex):
    """合击技：义交羁绊对双人联合大招（伤害 ×1.8 + 敌士气 -10），双 60 灵力。"""
    target = get_unit(session, target_uid) if target_uid else None
    if not target or target["side"] == unit["side"]:
        return {"ok": False, "reason": "目标无效"}
    if target["troops"] <= 0 or target["routed"]:
        return {"ok": False, "reason": "目标已失去战斗力"}
    if not _unit_in_range(session, unit, target, sk):
        return {"ok": False, "reason": "超出施放范围"}
    partner = None
    bonds = (unit.get("leader") or {}).get("bonds") or {}
    for u in session["units"]:
        if u is unit or u["side"] != unit["side"] or not u.get("leader"):
            continue
        if u["troops"] <= 0 or u["routed"] or u["acted"]:
            continue  # 搭档须仍可行动（已溃/已行动不能参与合击）
        if (bonds.get(str(u["leader"].get("id"))) or {}).get("type") == "yijiao":
            partner = u
            break
    if not partner:
        return {"ok": False, "reason": "无可用义交羁绊搭档"}
    if int(partner.get("mana", 0)) < 60:
        return {"ok": False, "reason": f"搭档灵力不足（{partner.get('mana', 0)}/60）"}
    partner["mana"] = int(partner.get("mana", 0)) - 60
    result = _resolve_attack(session, unit, target, rng,
                             damage_mult=sk["damage_mult"], kind="combo")
    if result.get("ok"):
        # 合击是双人联合行动：搭档同样消耗本回合行动权，否则"一次行动
        # 换两单位输出"，义交双人每回合可打出 3 次攻击/技能，明显超模。
        # 必须同步 session["acted"]：current_actor 只按该列表过滤，仅置
        # unit["acted"] 时搭档仍会被选为当前行动者（移动不校验 acted）。
        partner["acted"] = True
        end_actor_turn(session, partner["uid"])
        target["morale"] = max(0, target["morale"] - int(sk["morale_hit"]))
        _log(session, f"✨ {leader_name(unit)} 与 {leader_name(partner)} 发动合击技！"
                      f"{leader_name(target)} 士气 -{sk['morale_hit']}", "skill")
        result["combo_partner"] = partner["uid"]
    return result


def _troop_special(session, unit, rng, target_uid):
    """S 级适性兵种专属技能（每场 1 次）：盾墙/搬山阵/咒唱/剑阵冲锋/兽潮。"""
    if unit.get("special_used"):
        return {"ok": False, "reason": "本场已使用过兵种专属技能"}
    tt = unit["troop_type"]
    if tt not in ("sword_shield", "heavy_armor", "spell_corps",
                  "flying_sword", "beast_horde"):
        return {"ok": False, "reason": "该兵种无专属技能"}
    target = None
    if tt == "flying_sword":
        # 剑阵冲锋预选目标：无有效目标直接失败，不消耗本场一次机会
        target = get_unit(session, target_uid) if target_uid else None
        if target and (target["side"] == unit["side"] or target["troops"] <= 0
                       or target["routed"]):
            target = None
        charge_rng = max(1, attack_range(unit, session))
        if not target and unit["pos"]:
            cands = [f for f in foes_of(session, unit) if f["pos"] and hex_dist(
                (unit["pos"][0], unit["pos"][1]),
                (f["pos"][0], f["pos"][1])) <= charge_rng]
            target = min(cands, key=lambda x: hex_dist(
                (unit["pos"][0], unit["pos"][1]),
                (x["pos"][0], x["pos"][1]))) if cands else None
        if not target:
            return {"ok": False, "reason": "射程内无可冲锋目标"}
        # 冲锋是近战突击：必须已在攻击距离内（防止远程锁定 14 格外的敌军）
        if not unit["pos"] or hex_dist(
                (unit["pos"][0], unit["pos"][1]),
                (target["pos"][0], target["pos"][1])) > charge_rng:
            return {"ok": False, "reason": "目标不在冲锋距离内"}
    unit["special_used"] = True
    _record(session, {"type": "skill", "uid": unit["uid"],
                      "skill": "troop_special", "target_uid": target_uid})
    unit["acted"] = True
    unit["moved"] = True
    if tt == "sword_shield":      # 盾墙：静止防御 +15%，本回合受伤 -50%
        st = session["statuses"].setdefault(str(unit["uid"]), {})
        st["shieldwall"] = 1
        _log(session, f"🛡 {leader_name(unit)} 盾墙列阵（防御 +15%、本回合减伤 50%）", "skill")
        return {"ok": True, "skill": "troop_special", "effect": "shieldwall"}
    if tt == "heavy_armor":       # 搬山阵：3 回合拦截伤害 +50%
        session["statuses"].setdefault(str(unit["uid"]), {})["bashan"] = 3
        _log(session, f"⛰ {leader_name(unit)} 搬山阵（拦截伤害 +50%，3 回合）", "skill")
        return {"ok": True, "skill": "troop_special", "effect": "bashan"}
    if tt == "spell_corps":       # 咒唱：3 回合全军射程 +1（§7.5）
        for ally in alive_units(session, unit["side"]):
            ally_st = session["statuses"].setdefault(str(ally["uid"]), {})
            ally_st["range_buff"] = 3
        _log(session, f"🎵 {leader_name(unit)} 咒唱助阵（全军射程 +1，3 回合）", "skill")
        return {"ok": True, "skill": "troop_special", "effect": "chant"}
    if tt == "flying_sword":      # 剑阵冲锋：3 回合机动 +2 + 对最近敌军 ×1.5 一击
        # 施放当回合单位已用掉行动权，无法再移动；每回合开始 _tick_statuses
        # 会先 -1，因此写 4 才能让「下一回合起」实际享受 3 个完整回合。
        session["statuses"].setdefault(str(unit["uid"]), {})["charge"] = 4
        result = _resolve_attack(session, unit, target, rng, damage_mult=1.5, kind="charge")
        _log(session, f"🗡 {leader_name(unit)} 剑阵冲锋！（机动 +2，3 回合）", "skill")
        return result
    # beast_horde 兽潮：本回合邻格敌军合围判定士气额外 -5（§7.5）
    hits = []
    for foe in foes_of(session, unit):
        if foe["pos"] and unit["pos"] and hex_dist(
                (unit["pos"][0], unit["pos"][1]), (foe["pos"][0], foe["pos"][1])) <= 1:
            foe["morale"] = max(0, foe["morale"] - 5)
            hits.append({"uid": foe["uid"], "morale": -5})
    _log(session, f"🐾 {leader_name(unit)} 兽潮席卷（{len(hits)} 支敌军受创）", "skill")
    return {"ok": True, "skill": "troop_special", "hits": hits}


def _unit_in_range(session, unit, target, sk, allow_self=False) -> bool:
    if not unit["pos"] or not target["pos"]:
        return allow_self and target is unit
    return hex_dist((unit["pos"][0], unit["pos"][1]),
                    (target["pos"][0], target["pos"][1])) <= int(sk.get("range", 1))


# ---------------------------------------------------------------------------
# 防御 / 撤退（§7.3 行动：移动 → 攻击/技能/防御/撤退）
# ---------------------------------------------------------------------------

def do_defend(session: dict, uid: int) -> dict[str, Any]:
    unit = get_unit(session, uid)
    if not unit or unit["acted"] or unit["troops"] <= 0 or unit["routed"]:
        return {"ok": False, "reason": "无法防御"}
    unit["defending"] = True
    unit["acted"] = True
    unit["moved"] = True
    _record(session, {"type": "defend", "uid": uid})
    _log(session, f"🛡 {leader_name(unit)} 转入防御姿态", "defend")
    return {"ok": True}


def do_retreat(session: dict, uid: int, rng: random.Random) -> dict[str, Any]:
    """主动撤出战斗：军团离场（保留 80% 现有兵力，士气清零），敌方 ZOC 拦截照常。"""
    unit = get_unit(session, uid)
    if not unit or unit["acted"] or unit["troops"] <= 0 or unit["routed"]:
        return {"ok": False, "reason": "无法撤退"}
    _record(session, {"type": "retreat", "uid": uid})
    # 撤离时遭所有邻接 ZOC 控制方拦截；被完全合围者强行撤退承受双倍拦截
    encircled = is_fully_encircled(session, unit)
    if unit["pos"]:
        zmap = enemy_zoc_map(session, unit)
        if (unit["pos"][0], unit["pos"][1]) in zmap:
            for controller in zmap[(unit["pos"][0], unit["pos"][1])]:
                mult = 1.5 if (controller.get("leader") or {}).get("profession") == "body" else 1.0
                if encircled:
                    mult *= 2.0  # §7.4 撤退封锁：被合围者强行撤退双倍拦截
                dmg = R.damage_curve(
                    R.attack_power(_sim_view(session, controller), 1.0, session["night"]) * 0.5 * mult,
                    R.defense_power(_sim_view(session, unit)), unit["troops"])
                _apply_damage(session, unit, dmg, rng, source=controller, kind="intercept")
    unit["routed"] = True
    unit["voluntary_retreat"] = True
    unit["troops"] = int(unit["troops"] * 0.8)
    unit["morale"] = 0
    unit["acted"] = True
    _log(session, f"🏃 {leader_name(unit)} 撤出战场（保留 {unit['troops']} 兵）", "retreat")
    return {"ok": True, "kept": unit["troops"]}


# ---------------------------------------------------------------------------
# 单挑（斗法，§7.8；战场第 1~3 回合窗口）
# ---------------------------------------------------------------------------

DUEL_TEXT = [
    ("{a} 祭出本命法宝，灵光冲天而起！", "{b} 冷哼一声，掌中兵刃嗡鸣不止！"),
    ("{a} 身化流光，一招直取 {b} 面门！", "{b} 侧身让过，反手一记重击！"),
    ("两人战至酣处，气劲四溢，砂石飞溅！", "双方各展绝技，天地间灵力激荡！"),
]


def can_challenge(session: dict, uid: int, target_uid: int,
                  duel_cfg: dict | None = None) -> tuple[bool, str]:
    """战场单挑资格：第 1~3 回合窗口；武将职责武勇 ≥75；轻伤不可。"""
    duel_cfg = duel_cfg or {}
    lo, hi = duel_cfg.get("rounds") or duel_cfg.get("duel_rounds") or [1, 3]
    if not (int(lo) <= session["round"] <= int(hi)):
        return False, f"单挑窗口为第 {lo}~{hi} 回合"
    if session.get("duel"):
        return False, "已有单挑进行中"
    limit = max(1, int(duel_cfg.get("challenges_per_round", 1)))
    if int((session.get("duel_counts") or {}).get(str(uid), 0)) >= limit:
        return False, "本回合发起单挑次数已用完"
    # v4.3.0：每目标全场被挑战上限（duel_counts 每回合清零，仅靠它
    # 挑战方可在 1~3 回合窗口逐回合骚扰同一目标）
    t_limit = max(1, int(duel_cfg.get("challenges_per_target", 2)))
    if int((session.get("duel_target_counts") or {}).get(str(target_uid), 0)) >= t_limit:
        return False, "对方本场已多次拒战，不再接受挑战"
    unit = get_unit(session, uid)
    target = get_unit(session, target_uid) if target_uid else None
    if not unit or not target:
        return False, "目标无效"
    if unit["side"] == target["side"]:
        return False, "不能向友军挑战"
    if unit["troops"] <= 0 or unit["routed"] or unit["acted"]:
        return False, "该军团无法发起单挑"
    if target["troops"] <= 0 or target["routed"]:
        return False, "对方已失去战斗力"
    if not unit["pos"] or not target["pos"] or hex_dist(
            (unit["pos"][0], unit["pos"][1]),
            (target["pos"][0], target["pos"][1])) > 1:
        return False, "只能向相邻敌将发起单挑"
    # 无主军团（NPC/守军）无人应答，邀请会空转 15s 后白扣士气
    owners = session.get("owners") or {}
    if owners and not (owners.get(str(target_uid)) or {}).get("owner"):
        return False, "对方无人应战"
    leader = unit.get("leader") or {}
    if not leader or not R.can_challenge_duel(leader):
        return False, "只有健康且武勇 ≥75 的武将可发起单挑"
    tleader = target.get("leader")
    if not tleader:
        return False, "对方无将可挑"
    return True, ""


def challenge_duel(session: dict, uid: int, target_uid: int,
                   deadline_at: int = 0) -> dict[str, Any]:
    """挂起单挑请求（对方 15s 应战/拒绝，由 manager 传 deadline）。"""
    session["duel"] = {"challenger_uid": uid, "target_uid": target_uid,
                       "state": "pending", "deadline": deadline_at}
    _counts = session.setdefault("duel_counts", {})
    _counts[str(uid)] = int(_counts.get(str(uid), 0)) + 1
    # 全场维度：同一目标累计被挑战次数（不随回合清零）
    _t_counts = session.setdefault("duel_target_counts", {})
    _t_counts[str(target_uid)] = int(_t_counts.get(str(target_uid), 0)) + 1
    _record(session, {"type": "duel_challenge", "uid": uid, "target": target_uid})
    _log(session, f"⚔️ {leader_name(get_unit(session, uid))} 向 "
                  f"{leader_name(get_unit(session, target_uid))} 发起阵前斗法！", "duel")
    return {"ok": True}


R_SKILL_REFUSE_MORALE = -5


def refuse_duel(session: dict, penalty: bool = True) -> dict[str, Any]:
    """拒绝单挑：拒绝方士气 -5。

    v4.3.0：penalty=False 用于超时自动拒绝——托管/掉线玩家没有主动拒战，
    不应吃士气惩罚（此前挑战方可反复挑战挂机对手，靠超时白嫖 -5 士气
    零战损压溃对方）。
    """
    duel = session.get("duel")
    if not duel or duel["state"] != "pending":
        return {"ok": False, "reason": "无待决单挑"}
    target = get_unit(session, duel["target_uid"])
    if penalty and target:
        target["morale"] = max(0, target["morale"] + R_SKILL_REFUSE_MORALE)  # -5
    _record(session, {"type": "duel_refuse", "penalty": bool(penalty)})
    if penalty:
        _log(session, f"{leader_name(target)} 拒不出战，军心微挫（士气 -5）", "duel")
    else:
        _log(session, f"{leader_name(target)} 未在时限内应答，单挑作罢", "duel")
    session["duel"] = None
    return {"ok": True, "refused_by": duel["target_uid"], "penalty": bool(penalty)}


def resolve_duel(session: dict, rng: random.Random,
                 duel_cfg: dict | None = None,
                 apply: dict | None = None) -> dict[str, Any]:
    """三回合表演文字流 + 三局两胜（§7.8）。

    胜：己方士气 +15、敌 -10；败将 20% 伤势 +20；对方已濒死 → 阵前斩落。
    平：双方士气 +5、羁绊 +1 级。
    被俘判定（60%）由 manager 落库侧执行，这里返回 capture_hint。
    apply: manager 侧战果回填（败将整军溃败），供 replay 用同一指令重建。
    """
    duel_cfg = duel_cfg or {}
    duel = session.get("duel")
    if not duel:
        return {"ok": False, "reason": "无单挑进行中"}
    challenger = get_unit(session, duel["challenger_uid"])
    target = get_unit(session, duel["target_uid"])
    # 15s 应答窗口内任一方可能已被击溃/撤退：不得再结算士气、伤势与斩落，
    # 否则会对已不在场的单位继续造成战果。
    if not challenger or not target or challenger["troops"] <= 0 or \
            challenger["routed"] or target["troops"] <= 0 or target["routed"]:
        session["duel"] = None
        _log(session, "⚔️ 单挑因一方已失去战斗力而中止", "duel")
        return {"ok": False, "reason": "单挑对象已失去战斗力"}
    ca = dict(challenger.get("leader") or {})
    cb = dict(target.get("leader") or {})
    result = R.duel(ca, cb, rng)
    texts = []
    for i, (pa, pb) in enumerate(result.get("rounds") or []):
        ta, tb = DUEL_TEXT[i % len(DUEL_TEXT)]
        texts.append(f"第{i + 1}合：{ta.format(a=ca.get('name', '挑战者'), b=cb.get('name', '应战者'))}")
        texts.append(f"　　　　{tb.format(a=ca.get('name', ''), b=cb.get('name', ''))}"
                     f"（战力 {pa:.0f} vs {pb:.0f}）")
    out: dict[str, Any] = {"ok": True, "rounds": result.get("rounds"),
                           "draw": result.get("draw"), "texts": texts,
                           "challenger_uid": challenger["uid"], "target_uid": target["uid"]}
    win_morale = int(duel_cfg.get("win_morale", 15))
    lose_morale = int(duel_cfg.get("lose_morale", 10))
    loser_injury = int(duel_cfg.get("loser_injury", 20))
    if result.get("draw"):
        for u in (challenger, target):
            u["morale"] = min(120, u["morale"] + int(duel_cfg.get("draw_morale", 5)))
        out["outcome"] = "draw"
        out["bond_up"] = [ca.get("id"), cb.get("id")]  # 羁绊 +1 级（manager 落库）
        texts.append("两人战罢，惺惺相惜，各自收兵。（平局：双方士气 +5，羁绊 +1）")
    else:
        winner_is_challenger = result["winner"] == ca.get("id")
        wunit, lunit = (challenger, target) if winner_is_challenger else (target, challenger)
        wlead, llead = (ca, cb) if winner_is_challenger else (cb, ca)
        for u in (wunit,):
            u["morale"] = min(120, u["morale"] + win_morale)
        lunit["morale"] = max(0, lunit["morale"] - lose_morale)
        out["outcome"] = "challenger_win" if winner_is_challenger else "target_win"
        out["winner_uid"] = wunit["uid"]
        out["loser_uid"] = lunit["uid"]
        out["loser_injury"] = loser_injury
        texts.append(f"{llead.get('name', '败将')} 不敌 {wlead.get('name', '胜者')}，"
                     f"败阵而回！（胜方士气 +{win_morale}，败方 -{lose_morale}）")
        # 20% 伤势 +20
        if rng.random() < float(duel_cfg.get("winner_execute_chance", 0.2)):
            out["extra_injury"] = True
            texts.append(f"{wlead.get('name', '')} 乘胜追击，{llead.get('name', '')} 再添伤势！")
        # 濒死 → 阵前斩落（含本场追加伤势后的判定，避免文案/成就与实际伤势脱节）
        prospective_injury = int(llead.get("injury", 0)) + \
            (20 if out.get("extra_injury") else 0)
        if R.injury_state(prospective_injury) == "dying":
            out["executed"] = True
            texts.append(f"💀 {llead.get('name', '败将')} 已是强弩之末，"
                         f"被 {wlead.get('name', '')} 阵前斩落！（全服公告）")
        # 被俘判定 hint（单挑败 60%，manager 掷骰落库）
        out["capture_hint"] = {"loser_leader_id": llead.get("id"),
                               "captor_faction": wunit["faction_id"],
                               "kind": "duel"}
    for t in texts:
        _log(session, t, "duel")
    session["duel_log"].extend(texts)
    session["duel"] = None
    # manager 侧战果（主将失去战斗力 → 整军溃败）回填到指令流，
    # 保证 replay() 用同一条指令重建时战场状态一致。
    if apply:
        au = get_unit(session, int(apply.get("rout_uid", 0) or 0))
        if au and apply.get("rout"):
            au["troops"] = 0
            au["routed"] = True
            au["morale"] = 0
    _record(session, {"type": "duel_resolve", "outcome": out.get("outcome"),
                      "rounds": result.get("rounds"),
                      "duel_cfg": duel_cfg or None,
                      "apply": apply or None})
    return out


# ---------------------------------------------------------------------------
# 回合推进（§7.3 先手序 = 武勇 + 御兽/鬼修修正 + d10）
# ---------------------------------------------------------------------------

def start_battle(session: dict, rng: random.Random, wall_hp: int = 0,
                 begin: bool = True) -> None:
    """部署结束 → 开战。begin=False 供回放使用（回合由 'round' 指令驱动）。"""
    session["phase"] = "battle"
    if session["mode"] == "siege":
        session["wall_hp"] = int(wall_hp)
    for u in session["units"]:
        u["mana"] = 0
        u["moved"] = False
        u["acted"] = False
    _record(session, {"type": "start", "wall_hp": int(wall_hp),
                      "night": bool(session.get("night"))})
    if begin:
        begin_round(session, rng)


def _spectator_snapshot(session: dict) -> None:
    """记录「上一回合结束」的战场快照。

    §12.6 观战模式要求非参战玩家只读视图**延迟 1 回合**（防报点）。
    begin_round 在每回合开始调用，此时各单位仍是上一回合结束时的状态，
    因此以 round-1 标记；仅保留最近两回合，避免 session_json 膨胀。

    首回合（r=1）也写一份「部署完成」快照，否则观战者在第 1 回合看不到
    任何单位。未触发的伏兵整支不写入快照：观战者可能是敌方势力成员，
    即使隐藏坐标，仍可从单位数量/兵力推断出林中有伏兵（§15）。
    """
    r = int(session.get("round", 0) or 0)
    if r <= 0:
        return
    hidden = hidden_ambush_uids(session)
    units = []
    for u in session.get("units") or []:
        if int(u["uid"]) in hidden:
            continue
        units.append({
            "uid": u["uid"], "side": u["side"],
            "army_id": u.get("army_id", 0),
            "troop_type": u.get("troop_type"),
            "leader": leader_name(u),
            "troops": int(u.get("troops", 0)),
            "start_troops": u.get("start_troops"),
            "morale": int(u.get("morale", 0)),
            "pos": list(u["pos"]) if u.get("pos") else None,
            "routed": bool(u.get("routed")),
            "formation": u.get("formation"),
            "siege_engine": u.get("siege_engine"),
        })
    log = session.setdefault("spectator_log", [])
    log.append({"round": r - 1,
                # 城防同样延迟 1 回合：破城槌在回合内实时扣减，观战者
                # 若读实时值可推断当前回合的攻城进度（§12.6 防报点）。
                "wall_hp": int(session.get("wall_hp", 0) or 0),
                "units": units})
    del log[:-2]


def begin_round(session: dict, rng: random.Random) -> None:
    """回合开始：灵力 +15、状态结算（灼烧/到期清除）、先手序重掷。"""
    session["round"] = int(session["round"]) + 1
    if int(session["round"]) == 1:
        apply_night_buddha_morale(session)  # 夜袭「晨钟」首回合士气（§7.6）
    _spectator_snapshot(session)  # 观战延迟 1 回合（§12.6）
    _tick_statuses(session, rng)
    order = []
    amb_init = session.pop("ambush_initiative", [])  # 伏兵优先
    living = alive_units(session)
    scored = sorted(living, key=lambda u: -R.battle_initiative(
        _sim_view(session, u) | {"leader": u.get("leader")}, rng, session["night"]))
    for u in scored:
        if u["uid"] in amb_init:
            continue
        order.append(u["uid"])
    order = [uid for uid in amb_init if get_unit(session, uid)] + order
    session["order"] = order
    session["acted"] = []
    session["duel_counts"] = {}   # 每回合单挑次数（防止反复挑战刷士气）
    for u in living:
        u["moved"] = False
        u["acted"] = False
        u["defending"] = False
        u["mana"] = min(MANA_MAX, int(u.get("mana", 0)) + MANA_PER_ROUND)
    # 完全合围计时（§7.4：断粮 → 每回合兵力 -8%、士气 -15；持续 5 回合全部崩溃）
    for u in living:
        if is_fully_encircled(session, u):
            session["encircle_rounds"][str(u["uid"])] = \
                int(session["encircle_rounds"].get(str(u["uid"]), 0)) + 1
            cut = int(u["troops"] * 0.08)
            u["troops"] = max(0, int(u["troops"]) - cut)
            u["morale"] = max(0, int(u["morale"]) - 15)
            if cut:
                _log(session, f"🕸 {leader_name(u)} 被完全合围断粮（-{cut} 兵，士气 -15）",
                     "encircle")
            if session["encircle_rounds"][str(u["uid"])] >= 5 or u["troops"] <= 0:
                u["routed"] = True
                u["morale"] = 0
                _log(session, f"🕸 {leader_name(u)} 被完全合围五回合，全军崩溃！", "encircle")
        else:
            session["encircle_rounds"].pop(str(u["uid"]), None)
    # 临时召唤物到期
    for u in list(session["units"]):
        if u.get("temp") and int(u.get("temp_expire", 0)) < session["round"]:
            u["routed"] = True
            u["troops"] = 0
            _log(session, "🐺 召唤兽群消散", "summon")
    _record(session, {"type": "round", "round": session["round"], "order": order})


def _tick_statuses(session: dict, rng: random.Random) -> None:
    """灼烧掉血 / buff 倒计时。

    confuse / suppress / taunt 由技能在目标行动前施加，值为 2：本回合开始
    先减到 1 仍保留，下回合开始再减到 0。真正的「生效」由 _status_active
    按 *_from 判定——施加当回合不生效，只影响目标的下一个行动回合。
    """
    for uid_s, st in list(session["statuses"].items()):
        unit = get_unit(session, int(uid_s))
        if not unit:
            continue
        # 0 兵/已溃逃单位不再结算灼烧（否则只刷日志、白攒伤势池），
        # 但计时照常递减，保证状态自然过期不成为僵尸字段。
        if st.get("burn", 0) > 0 and unit["troops"] > 0 and not unit["routed"]:
            dmg = int(unit["troops"] * 0.03) + rng.randint(5, 20)
            _apply_damage(session, unit, dmg, rng, kind="burn")
            _log(session, f"🔥 {leader_name(unit)} 遭受灼烧（-{dmg} 兵）", "status")
        if st.get("burn", 0) > 0:
            st["burn"] = int(st["burn"]) - 1
        for k in ("bulwark", "confuse", "suppress", "shieldwall",
                  "bashan", "charge", "range_buff"):
            if st.get(k, 0) and isinstance(st[k], int):
                st[k] -= 1
        if st.get("taunt_by"):
            st["taunt_ttl"] = int(st.get("taunt_ttl", 1)) - 1
            if st["taunt_ttl"] <= 0:
                st.pop("taunt_by", None)
                st.pop("taunt_ttl", None)
        # 状态本体已结束时同步清理来源回合标记，避免 statuses 僵尸字段
        for k in ("confuse", "suppress", "taunt_by"):
            if not st.get(k):
                st.pop(k + "_from", None)
        session["statuses"][uid_s] = {k: v for k, v in st.items() if v}


def current_actor(session: dict) -> Optional[dict]:
    """先手序中下一个未行动单位。"""
    for uid in session["order"]:
        if uid in session["acted"]:
            continue
        unit = get_unit(session, uid)
        if unit and unit["troops"] > 0 and not unit["routed"]:
            return unit
    return None


def end_actor_turn(session: dict, uid: int) -> None:
    if uid not in session["acted"]:
        session["acted"].append(uid)


def round_complete(session: dict) -> bool:
    # 仅按本回合先手序（order）判定：召唤物等回合中途入场单位不在 order 内，
    # 若按 alive_units 全集判定会导致召唤后本回合永远无法结束（死锁）。
    return current_actor(session) is None


def check_end(session: dict) -> Optional[str]:
    """结束判定（§7.3）：一方总兵力 <30% 或全灭/全溃 → 对方胜。"""
    for side in ("a", "b"):
        units = [u for u in session["units"] if u["side"] == side]
        if not units:
            continue
        total0 = sum(u["start_troops"] for u in units if not u.get("temp")) or 1
        # 召唤物不计入存活判定（total0 已排除 temp，alive 必须同口径，
        # 否则正规军全灭但召唤兽群仍在时不会判负）
        alive = [u for u in units if not u.get("temp")
                 and u["troops"] > 0 and not u["routed"]]
        total = sum(u["troops"] for u in alive)
        if not alive or total < total0 * 0.3:
            return "attacker" if side == "b" else "defender"
    # 打满回合：守方守住。必须在**最后一个行动单位行动完毕**后才判，
    # 否则第 N 回合开始就会被提前终止（实际只打 N-1 个完整回合，与
    # R.simulate_battle 打满 N 回合的语义不一致）。
    if session["round"] >= session["max_rounds"] and round_complete(session):
        return "defender"
    if session["round"] > session["max_rounds"]:
        return "defender"
    return None


def finish(session: dict) -> dict[str, Any]:
    """终局：生成与 simulate_battle 兼容的 result（供 _apply_battle_aftermath 消费）。"""
    session["phase"] = "done"
    winner = session.get("winner") or "defender"

    # 名医特技：战后伤兵回收 8%、角色伤势额外 -1（§4.4）
    for u in session["units"]:
        if u.get("temp"):
            continue
        leader = u.get("leader") or {}
        lost = max(0, int(u["start_troops"]) - int(u["troops"]))
        heal = R.doctor_aftermath(leader, lost, int(leader.get("injury", 0) or 0))
        # 覆没（0 兵）军团不得靠名医"复活"：否则战报里全灭的军团会以
        # 残兵写回地图；已溃逃军团同样不回收伤兵（人已随军溃散）。
        if heal["recovered"] > 0 and int(u["troops"]) > 0 and not u.get("routed"):
            u["troops"] = min(u["start_troops"], int(u["troops"]) + heal["recovered"])
            _log(session, f"💊 {leader_name(u)} 随军名医救治，回收伤兵 {heal['recovered']} 人", "aftermath")
        if leader and heal["injury"] != int(leader.get("injury", 0) or 0):
            leader["injury"] = heal["injury"]
            lid = leader.get("id")
            if lid:
                session.setdefault("_injury_heals", {})[str(int(lid))] = heal["injury"]

    def _side(side: str) -> list[dict]:
        out = []
        for u in session["units"]:
            if u["side"] != side or u.get("temp"):
                continue
            out.append({
                "id": u["army_id"], "faction_id": u["faction_id"],
                "troops_before": u["start_troops"], "troops_after": int(u["troops"]),
                "morale": int(u["morale"]),
                "routed": bool(u["routed"]),
                "voluntary_retreat": bool(u.get("voluntary_retreat")),
                "leader_id": (u.get("leader") or {}).get("id"),
                "leader_name": (u.get("leader") or {}).get("name"),
                "troop_type": u["troop_type"], "veteran": int(u["veteran"]),
            })
        return out

    attackers = _side("a")
    defenders = _side("b")
    result = {
        "winner": winner,
        "rounds": int(session["round"]),
        "round_log": [{"round": e["round"], "log": [e["text"]]}
                      for e in session["events"][-120:]],
        "attackers": attackers,
        "defenders": defenders,
        "routed_attacker_ids": [a["id"] for a in attackers if a["routed"]],
        "routed_defender_ids": [d["id"] for d in defenders if d["routed"]],
        "peril_events": session.get("peril_events", []),
        "atk_losses": sum(a["troops_before"] - a["troops_after"] for a in attackers),
        "dfn_losses": sum(d["troops_before"] - d["troops_after"] for d in defenders),
        "mode": "manual_hex",
        "duel_log": session.get("duel_log", []),
        "night": bool(session.get("night")),
        "loot_bonus": night_loot_bonus(session),
        # 关键事件（开局/单挑/伏兵/合围/善后）：events 仅留尾部 400 条，
        # 回放时用它补回被截断的前中期高光。
        "key_events": list(session.get("key_events") or [])[-200:],
    }
    session["result"] = result
    _record(session, {"type": "finish", "winner": winner})
    return result


# ---------------------------------------------------------------------------
# 托管 AI（§7.3 离线自动托管；§15 科技 stratagem L3 智谋等效 +5）
# ---------------------------------------------------------------------------

def ai_tier(unit: dict, tech_wisdom_bonus: int = 0) -> str:
    """三档托管：智谋 ≥80 精明 / ≥50 均衡 / 其余 莽撞。"""
    leader = unit.get("leader") or {}
    wisdom = int((leader.get("stats") or {}).get("wisdom", 40)) + int(tech_wisdom_bonus)
    if wisdom >= 80:
        return "smart"
    if wisdom >= 50:
        return "balanced"
    return "brave"


def do_confused_attack(session: dict, uid: int, rng: random.Random) -> dict[str, Any]:
    """惑敌状态下攻击：敌我不分，按 round/uid 派生确定性伪随机选目标。

    与 ai_decide 的惑敌分支共用（AI 层不消耗 rng，回放对齐）；
    人类玩家被惑敌时经 battle_action 走同一入口，规则一致。
    """
    unit = get_unit(session, uid)
    if not unit or unit["acted"] or unit["troops"] <= 0 or unit["routed"]:
        return {"ok": False, "reason": "军团无法行动"}
    if not unit["pos"]:
        return {"ok": False, "reason": "惑敌：单位未部署"}
    targets = [u for u in alive_units(session) if u is not unit and u["pos"]]
    if not targets:
        return {"ok": False, "reason": "惑敌：无可攻击目标"}
    idx = (session["round"] * 7 + uid * 13) % len(targets)
    victim = targets[idx]
    if hex_dist((unit["pos"][0], unit["pos"][1]),
                (victim["pos"][0], victim["pos"][1])) > max(1, attack_range(unit, session)):
        return {"ok": False, "reason": "惑敌：所选目标不在射程内（可先移动）"}
    _record(session, {"type": "confused_attack", "uid": uid, "target": victim["uid"]})
    return _resolve_attack(session, unit, victim, rng, kind="confused")


def ai_decide(session: dict, uid: int, rng: random.Random,
              tech_wisdom_bonus: int = 0) -> dict[str, Any]:
    """为单个单位生成一条 AI 指令并执行（托管）。

    返回执行结果 dict。行为优先级：
    1. 被挑衅 → 强制攻击挑衅者；2. 血量过低且精明 → 撤退/防御；
    3. 有增益技能机会 → 释放；4. 进入射程 → 攻击（集火最弱）；
    5. 向目标移动（贪心逐步，ZOC 感知）。
    """
    unit = get_unit(session, uid)
    if not unit or unit["acted"] or unit["troops"] <= 0 or unit["routed"]:
        return {"ok": False, "reason": "skip"}
    tier = ai_tier(unit, tech_wisdom_bonus)
    # 惑敌：随机行动（可打友军）。AI 决策层不消耗 rng（回放对齐），
    # 用 round/uid 派生的确定性伪随机选择目标；伤害掷骰走已记录的指令。
    if is_confused(session, unit):
        cres = do_confused_attack(session, uid, rng)
        if cres.get("ok"):
            return cres
        return _ai_move_toward(session, unit, rng, _nearest_foe(session, unit), tier)
    # 挑衅：强制攻击挑衅者（下一回合起生效）
    taunter_uid = active_taunter(session, unit)
    if taunter_uid:
        taunter = get_unit(session, int(taunter_uid))
        if taunter and taunter["troops"] > 0 and not taunter["routed"]:
            if can_attack(session, unit, taunter)[0]:
                return do_attack(session, uid, taunter["uid"], rng)
            return _ai_move_toward(session, unit, rng, taunter, tier,
                                   forced=taunter)
    foes = foes_of(session, unit)
    if not foes:
        return do_defend(session, uid)
    # 精明档：兵力 <25% → 撤退；<40% 且被围 → 防御
    if tier == "smart" and unit["troops"] < unit["start_troops"] * 0.25:
        return do_retreat(session, uid, rng)
    # 技能机会
    skill_res = _ai_try_skill(session, unit, rng, tier, foes)
    if skill_res:
        return skill_res
    # 攻击：射程内集火最弱（精明档考虑克制）
    in_range = [f for f in foes if can_attack(session, unit, f)[0]]
    if in_range:
        if tier == "smart":
            target = max(in_range, key=lambda f: (
                R.counter_factor(unit["troop_type"], f["troop_type"]),
                -f["troops"]))
        else:
            target = min(in_range, key=lambda f: f["troops"])
        return do_attack(session, uid, target["uid"], rng)
    # 移动逼近
    target = _pick_pursuit_target(session, unit, foes, tier, rng)
    return _ai_move_toward(session, unit, rng, target, tier)


def _pick_pursuit_target(session, unit, foes, tier, rng):
    if tier == "smart":
        # 优先打远程/低兵力
        return min(foes, key=lambda f: (
            0 if R.TROOP_TYPES.get(f["troop_type"], {}).get("range", 0) > 0 else 1,
            f["troops"]))
    if tier == "balanced":
        return min(foes, key=lambda f: f["troops"])
    # 莽撞：任意冲一个（round/uid 派生的确定性伪随机，AI 层不消耗 rng）
    idx = (session["round"] * 11 + unit["uid"] * 5) % len(foes)
    return foes[idx]


def _nearest_foe(session, unit):
    foes = foes_of(session, unit)
    if not foes or not unit["pos"]:
        return None
    return min(foes, key=lambda f: hex_dist((unit["pos"][0], unit["pos"][1]),
                                            (f["pos"][0], f["pos"][1])) if f["pos"] else 999)


def _skill_castable(session: dict, unit: dict, skill_id: str) -> bool:
    """技能前置校验（不含目标合法性、不消耗 rng）——AI 决策层过滤用。"""
    sk = SKILLS.get(skill_id)
    if not sk or skill_id not in unit_skills(session, unit):
        return False
    if int(unit.get("mana", 0)) < int(sk.get("mana", 0)):
        return False
    if unit.get("acted") or unit["troops"] <= 0 or unit["routed"]:
        return False
    if sk.get("need_highland") and unit["pos"]:
        t = eff_terrain(session, unit["pos"][0], unit["pos"][1])
        if t not in (T_HIGHLAND, T_WALL, T_GATE):
            return False
    if sk.get("need_river") and unit["pos"]:
        if not any(eff_terrain(session, nq, nr) == T_RIVER
                   for nq, nr in R.hex_neighbors(unit["pos"][0], unit["pos"][1])):
            return False
    return True


def _ai_try_skill(session: dict, unit: dict, rng: random.Random, tier: str,
                  foes: list[dict]) -> Optional[dict]:
    """AI 技能决策：治疗（友军 <70%）、火攻/箭雨（敌群）、兽群（缺前排）、坚守（守点）。"""
    skills = unit_skills(session, unit)
    mana = int(unit.get("mana", 0))
    my_side_units = [u for u in alive_units(session, unit["side"])]
    if "heal" in skills and mana >= SKILLS["heal"]["mana"]:
        wounded = [u for u in my_side_units
                   if u["troops"] < u["start_troops"] * 0.7 and u["pos"] and unit["pos"]
                   and hex_dist((unit["pos"][0], unit["pos"][1]),
                                (u["pos"][0], u["pos"][1])) <= SKILLS["heal"]["range"]]
        if wounded:
            target = min(wounded, key=lambda u: u["troops"] / max(1, u["start_troops"]))
            res = do_skill(session, unit["uid"], "heal", rng, target_uid=target["uid"])
            if res.get("ok"):
                return res
    if tier in ("smart", "balanced") and "fire" in skills and mana >= SKILLS["fire"]["mana"]:
        best, best_n = None, 0
        for f in foes:
            if not f["pos"]:
                continue
            n = sum(1 for x in foes if x["pos"] and hex_dist(
                (f["pos"][0], f["pos"][1]), (x["pos"][0], x["pos"][1])) <= 1)
            if n > best_n and _in_skill_range(session, unit, SKILLS["fire"], f["pos"][0], f["pos"][1]):
                best, best_n = f, n
        if best and best_n >= 2:
            res = do_skill(session, unit["uid"], "fire", rng, target_hex=best["pos"])
            if res.get("ok"):
                return res
    if "barrage" in skills and mana >= SKILLS["barrage"]["mana"] \
            and _skill_castable(session, unit, "barrage"):
        cands = [f for f in foes if f["pos"]
                 and _in_skill_range(session, unit, SKILLS["barrage"], f["pos"][0], f["pos"][1])]
        if cands:
            # 确定性选择：邻格友军最密集的目标（AI 层不消耗 rng）
            target = max(cands, key=lambda f: (sum(
                1 for x in foes if x["pos"] and hex_dist(
                    (f["pos"][0], f["pos"][1]), (x["pos"][0], x["pos"][1])) <= 1), f["troops"]))
            res = do_skill(session, unit["uid"], "barrage", rng, target_hex=target["pos"])
            if res.get("ok"):
                return res
    if "beastrush" in skills and mana >= SKILLS["beastrush"]["mana"] \
            and len(my_side_units) <= 2 and unit.get("pos"):
        empties = [[nq, nr] for nq, nr in R.hex_neighbors(unit["pos"][0], unit["pos"][1])
                   if in_bounds(session, nq, nr) and passable(session, nq, nr)
                   and not unit_at(session, nq, nr)]
        if empties:
            res = do_skill(session, unit["uid"], "beastrush", rng,
                           target_hex=sorted(empties)[0])  # 确定性选格
            if res.get("ok"):
                return res
    if "bulwark" in skills and mana >= SKILLS["bulwark"]["mana"] \
            and unit["troops"] < unit["start_troops"] * 0.6 and unit.get("pos") \
            and any(hex_dist((unit["pos"][0], unit["pos"][1]),
                             (f["pos"][0], f["pos"][1])) <= 2 for f in foes if f["pos"]):
        res = do_skill(session, unit["uid"], "bulwark", rng)
        if res.get("ok"):
            return res
    if "ram" in skills and session["mode"] == "siege" and session.get("wall_hp", 0) > 0:
        res = do_skill(session, unit["uid"], "ram", rng)
        if res.get("ok"):
            return res
    if "combo" in skills and mana >= 60 and tier == "smart":
        cands = [f for f in foes if can_attack(session, unit, f)[0]]
        if cands:
            target = max(cands, key=lambda f: f["troops"])
            res = do_skill(session, unit["uid"], "combo", rng, target_uid=target["uid"])
            if res.get("ok"):
                return res
    return None


def _ai_move_toward(session: dict, unit: dict, rng: random.Random,
                    target: Optional[dict], tier: str,
                    forced: Optional[dict] = None) -> dict[str, Any]:
    """贪心逐步移动逼近目标（ZOC 感知：进入即停可接受）。

    ``forced``：被挑衅时必须攻击的挑衅者；移动后若仍不可攻击，宁可防御
    也不得改打其他敌人（与手操端的「只能攻击挑衅者」同口径）。
    """
    if not target or not target["pos"] or not unit["pos"]:
        return do_defend(session, unit["uid"])
    goal = (target["pos"][0], target["pos"][1])
    atk_r = max(1, attack_range(unit, session))
    path: list[list[int]] = []
    cur = (unit["pos"][0], unit["pos"][1])
    mp = move_points(unit, session)
    spent = 0.0
    visited = {cur}
    for _ in range(8):
        if hex_dist(cur, goal) <= atk_r:
            break
        options = []
        for nq, nr in R.hex_neighbors(*cur):
            if not in_bounds(session, nq, nr) or (nq, nr) in visited:
                continue
            if not passable(session, nq, nr, unit["troop_type"]):
                continue
            if unit_at(session, nq, nr):
                continue
            t = eff_terrain(session, nq, nr)
            cost = TERRAIN_MOVE_COST.get(t, 1)
            if unit["troop_type"] == "beast_horde" and t == T_FOREST:
                cost = 1
            if unit["troop_type"] == "flying_sword" and t == T_PLAIN:
                cost = max(0.5, cost - 0.5)  # 与 check_move_path 一致：飞剑平原 -0.5
            if spent + cost > mp + 1e-6:
                continue
            # 精明档规避高地敌射程（粗略：不走进距敌 2 格内的开阔地）
            options.append((hex_dist((nq, nr), goal), cost, (nq, nr)))
        if not options:
            break
        options.sort(key=lambda x: (x[0], x[1]))
        _, cost, nxt = options[0]
        path.append([nxt[0], nxt[1]])
        spent += cost
        visited.add(nxt)
        cur = nxt
    if not path:
        return do_defend(session, unit["uid"])
    res = do_move(session, unit["uid"], path, rng)
    if res.get("ok") is False and res.get("end", 0) == 0:
        return do_defend(session, unit["uid"])
    # 移动后若进入射程 → 顺手攻击
    in_range = [f for f in foes_of(session, unit) if can_attack(session, unit, f)[0]]
    if in_range and not unit["acted"]:
        if forced is not None:
            if can_attack(session, unit, forced)[0]:
                return do_attack(session, unit["uid"], forced["uid"], rng)
            # 被挑衅：不得转移目标，原地防御结束本回合
            return do_defend(session, unit["uid"])
        tgt = min(in_range, key=lambda f: f["troops"])
        return do_attack(session, unit["uid"], tgt["uid"], rng)
    if not unit["acted"]:
        return do_defend(session, unit["uid"])
    return res


def ai_auto_battle(session: dict, rng: random.Random,
                   tech_bonus: dict[int, int] | None = None) -> dict[str, Any]:
    """全托管瞬时模拟：从当前状态自动打到终局。

    tech_bonus: faction_id → stratagem 科技智谋等效加成。
    """
    tech_bonus = tech_bonus or {}
    if session["phase"] == "deploy":
        auto_deploy(session, rng)
        if not all_deployed(session):
            # 极端情况（可用格少于军团数）：未落位单位按溃散处理，
            # 避免 pos=None 的僵尸单位开战后不可被攻击、拖满回合
            for u in session["units"]:
                if not u["pos"]:
                    u["troops"] = 0
                    u["routed"] = True
        start_battle(session, rng, wall_hp=session.get("wall_hp", 0))
    guard = 0
    while session["phase"] == "battle" and guard < session["max_rounds"] + 2:
        guard += 1
        for uid in list(session["order"]):
            unit = get_unit(session, uid)
            if not unit or unit["acted"] or unit["troops"] <= 0 or unit["routed"]:
                continue
            ai_decide(session, uid, rng, tech_bonus.get(unit["faction_id"], 0))
            if unit["moved"] and not unit["acted"]:
                # 移动后未行动：AI 已在 _ai_move_toward 里处理；兜底防御
                do_defend(session, uid)
            end_actor_turn(session, uid)
            winner = check_end(session)
            if winner:
                session["winner"] = winner
                return finish(session)
        begin_round(session, rng)
    winner = check_end(session) or "defender"
    session["winner"] = winner
    return finish(session)


# ---------------------------------------------------------------------------
# 回放（§7.3 seed + 指令序列重建）
# ---------------------------------------------------------------------------

def _record(session: dict, cmd: dict) -> None:
    cmd = dict(cmd)
    cmd.setdefault("round", session.get("round", 0))
    session["commands"].append(cmd)


#: 需要长期保留的关键事件类型（不随 400 条尾部窗口丢弃）
_KEY_EVENT_KINDS = {"start", "duel", "ambush", "encircle",
                    "aftermath", "summon", "retreat"}


def _log(session: dict, text: str, kind: str = "") -> None:
    ev = {"round": session.get("round", 0), "text": text, "kind": kind}
    session["events"].append(ev)
    # 关键事件单独留档：events 只保留最近 400 条，长战役的前中期高光
    # （开局/单挑/伏兵/合围）会丢失，回放步进器无法再跳转。
    if kind in _KEY_EVENT_KINDS:
        key = session.setdefault("key_events", [])
        key.append(ev)
        del key[:-200]
    # 控制体积：只保留最近 400 条（战报/回放渲染取该尾部窗口）
    if len(session["events"]) > 400:
        session["events"] = session["events"][-400:]


def replay(seed: str, mode: str, units_a: list[dict], units_b: list[dict],
           commands: list[dict], rng: random.Random,
           hex_cfg: dict | None = None, wall_hp: int = 0) -> dict[str, Any]:
    """按 seed + 指令序列完整重建战报（观战回放/战报核验）。

    units_a/units_b 为开战时的 army_sim dict 列表（armies_snapshot）。
    """
    map_info = generate_map(seed, mode, hex_cfg)
    session = new_session(map_info, mode, seed, hex_cfg=hex_cfg)
    for sim in units_a:
        add_unit(session, sim, "a")
    for sim in units_b:
        add_unit(session, sim, "b")
    for cmd in commands:
        t = cmd.get("type")
        if t == "deploy":
            valid_deploy(session, cmd["uid"], cmd["q"], cmd["r"])
            place_unit(session, cmd["uid"], cmd["q"], cmd["r"])
        elif t == "formation":
            set_formation(session, cmd["uid"], cmd.get("formation", ""))
        elif t == "ambush":
            set_ambush(session, cmd["uid"], cmd["q"], cmd["r"])
        elif t == "auto_deploy":
            _uids = cmd.get("uids")
            auto_deploy(session, rng,
                        uids=set(_uids) if _uids is not None else None)
        elif t == "start":
            start_battle(session, rng, cmd.get("wall_hp", wall_hp), begin=False)
            if cmd.get("night") is not None:
                session["night"] = bool(cmd.get("night"))  # 夜战先手/攻击加成对齐
        elif t == "move":
            do_move(session, cmd["uid"], cmd["path"], rng, cmd.get("break", False))
        elif t == "attack":
            do_attack(session, cmd["uid"], cmd["target"], rng)
        elif t == "confused_attack":
            cu = get_unit(session, cmd["uid"])
            cv = get_unit(session, cmd["target"])
            if cu and cv:
                _resolve_attack(session, cu, cv, rng, kind="confused")
        elif t == "skill":
            do_skill(session, cmd["uid"], cmd["skill"], rng,
                     target_uid=cmd.get("target_uid"), target_hex=cmd.get("target_hex"))
        elif t == "defend":
            do_defend(session, cmd["uid"])
        elif t == "retreat":
            do_retreat(session, cmd["uid"], rng)
        elif t == "duel_challenge":
            challenge_duel(session, cmd["uid"], cmd["target"])
        elif t == "duel_refuse":
            # v4.3.0：回放须还原是否扣士气（超时自动拒绝不罚）
            refuse_duel(session, penalty=bool(cmd.get("penalty", True)))
        elif t == "duel_resolve":
            resolve_duel(session, rng, cmd.get("duel_cfg"), cmd.get("apply"))
        elif t == "round":
            begin_round(session, rng)
        elif t == "finish":
            session["winner"] = cmd.get("winner")
            return finish(session)
    return finish(session)


def render_hex_report(result: dict[str, Any], title: str = "会战") -> str:
    """hex 会战文字战报（复用 rules 的排版风格 + 单挑/技能事件）。"""
    lines = [f"⚔️【{title}·手操会战】历经 {result.get('rounds', 0)} 回合"]
    for entry in (result.get("round_log") or [])[-12:]:
        for text in entry.get("log", [])[:2]:
            lines.append(f"  · {text}")
    for side_key, side_name in (("attackers", "攻方"), ("defenders", "守方")):
        for a in result.get(side_key, []):
            state = "溃逃" if a.get("routed") else ("全灭" if a["troops_after"] <= 0 else "存留")
            leader = a.get("leader_name") or "无主军团"
            troop_name = R.TROOP_TYPES.get(a.get("troop_type", ""), {}).get("name", "部队")
            lines.append(
                f"  {side_name}·{leader}（{troop_name}）："
                f"{a['troops_before']} → {a['troops_after']} 兵【{state}】"
            )
    verdict = "攻方胜" if result.get("winner") == "attacker" else "守方胜"
    lines.append(f"🏁 结果：{verdict}（攻方损 {result.get('atk_losses', 0)} / "
                 f"守方损 {result.get('dfn_losses', 0)}）")
    return "\n".join(lines)
