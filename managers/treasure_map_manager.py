# managers/treasure_map_manager.py
"""古修藏宝图爬塔：逐层深入、随时撤退的高风险探险。

核心约束（与需求一致）：
- 爬塔期间禁用自然恢复与调息恢复；
- 丹药/符箓回血有每层上限（默认最大气血的 18%），不能靠嗑药无限续命；
- 每层都可能触发陷阱/妖兽，气血归零则本次所有战利品尽失；
- 层数越深，掉落品阶与灵石越高，但受伤也更重。

v3.49 肉鸽化：
- 每推进一层，除了原本的伤害与战利品，还会触发一次随机奇遇；
- 玩家必须从 2-3 个选项中做出抉择，选项可能带来灵石/战利品/气血变化，
  也会叠上一层「临时效果」；
- 临时效果只在本次爬塔内生效，可叠加，涵盖减伤、陷阱规避、寻宝、聚财、
  回血额度、护身符与负面诅咒等，让每一趟爬塔的构筑都不一样。
"""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from ..data import DataBase
from ..models import Player
from ..utils.game_secret import seeded_rng

__all__ = ["TreasureMapManager"]

MAX_FLOOR = 6
HEAL_BUDGET_RATE = 0.18
MIN_START_HP_RATE = 0.35
# 超过 24 小时没有推进的爬塔视为异常中断，由后台按撤退结算
STALE_RUN_SECONDS = 86400

MAP_TIERS = {
    1: {"name": "残破藏宝图", "price": 8000, "gold": 900, "loot_chance": 0.55, "desc": "边角残缺，只能看出前几层路径。"},
    2: {"name": "古修藏宝图", "price": 40000, "gold": 2600, "loot_chance": 0.68, "desc": "古修洞府遗图，灵纹尚存。"},
    3: {"name": "太古董藏图", "price": 180000, "gold": 7200, "loot_chance": 0.80, "desc": "以帝血绘制的太古图卷，深处封着大机缘。"},
}

FLOOR_RULES = {
    1: {"damage": (0.06, 0.12), "trap": 0.18, "trap_mult": 1.6, "label": "残破石阶"},
    2: {"damage": (0.09, 0.17), "trap": 0.26, "trap_mult": 1.7, "label": "腐骨回廊"},
    3: {"damage": (0.13, 0.22), "trap": 0.34, "trap_mult": 1.8, "label": "禁制甬道"},
    4: {"damage": (0.17, 0.28), "trap": 0.42, "trap_mult": 1.9, "label": "血池地宫"},
    5: {"damage": (0.22, 0.34), "trap": 0.50, "trap_mult": 2.0, "label": "万骨祭坛"},
    6: {"damage": (0.28, 0.42), "trap": 0.58, "trap_mult": 2.1, "label": "古修主墓"},
}

# ================= 肉鸽临时效果 =================
# value 为每层叠加的数值，max 为本次爬塔内的叠加上限。
# 效果只在当前 treasure_map_runs 记录内生效，撤退/阵亡/通关后自动失效。
EFFECT_DEFS = {
    "iron_body": {
        "name": "铜皮铁骨", "kind": "buff", "value": 0.08, "max": 5,
        "desc": "后续楼层受到伤害 -{pct}",
    },
    "keen_sense": {
        "name": "灵觉敏锐", "kind": "buff", "value": 0.10, "max": 4,
        "desc": "陷阱触发率 -{pct}",
    },
    "treasure_luck": {
        "name": "寻宝直觉", "kind": "buff", "value": 0.08, "max": 5,
        "desc": "战利品概率 +{pct}，额外掉落率 +{extra}",
    },
    "gold_luck": {
        "name": "聚财灵光", "kind": "buff", "value": 0.12, "max": 5,
        "desc": "灵石收获 +{pct}",
    },
    "heal_flow": {
        "name": "回春灵息", "kind": "buff", "value": 0.10, "max": 5,
        "desc": "每层回血额度 +{pct}",
    },
    "ward": {
        "name": "护身灵符", "kind": "token", "value": 1, "max": 3,
        "desc": "可抵挡 {n} 次陷阱伤害，触发后消耗一层",
    },
    "fragile": {
        "name": "魂体受损", "kind": "debuff", "value": 0.10, "max": 4,
        "desc": "受到伤害 +{pct}",
    },
    "curse": {
        "name": "墓府诅咒", "kind": "debuff", "value": 0.10, "max": 3,
        "desc": "灵石收获 -{pct}",
    },
}

# 每层结算后随机抽取的奇遇。actions 为成功结果，risk 为失败分支。
# 动作类型：effect / hp / gold / loot / cleanse / rand_effect。
EVENT_POOL = [
    {
        "key": "ancient_spirit",
        "name": "古修残魂",
        "desc": "一缕残魂悬在墓道尽头，正打量着你。",
        "options": [
            {
                "label": "承接传承", "desc": "以精血换取古修留下的炼体感悟。",
                "actions": [
                    {"type": "hp", "pct": -0.08},
                    {"type": "effect", "key": "iron_body", "stacks": 2},
                ],
                "risk": {
                    "chance": 0.25, "text": "残魂骤然反噬，你的神魂一阵刺痛。",
                    "actions": [{"type": "effect", "key": "fragile", "stacks": 1}],
                },
            },
            {
                "label": "超度残魂", "desc": "诵经送残魂往生，墓府灵气反哺己身。",
                "actions": [
                    {"type": "hp", "pct": 0.12},
                    {"type": "effect", "key": "heal_flow", "stacks": 1},
                ],
            },
            {
                "label": "搜刮陪葬", "desc": "不理会残魂，翻找棺椁中的陪葬品。",
                "actions": [
                    {"type": "loot", "count": 1},
                    {"type": "hp", "pct": -0.10},
                ],
            },
        ],
    },
    {
        "key": "spirit_spring",
        "name": "灵泉",
        "desc": "一汪灵泉从石缝中涌出，寒气沁骨却生机盎然。",
        "options": [
            {
                "label": "饮下灵泉", "desc": "任由灵泉冲刷经脉，伤势缓缓愈合。",
                "actions": [{"type": "hp", "pct": 0.16}],
            },
            {
                "label": "装瓶带走", "desc": "把灵泉封进玉瓶，转手便是白花花的灵石。",
                "actions": [
                    {"type": "gold", "mult": 0.8},
                    {"type": "effect", "key": "treasure_luck", "stacks": 1},
                ],
            },
            {
                "label": "引泉淬体", "desc": "以灵泉淬炼肉身，痛楚中透着坚韧。",
                "actions": [
                    {"type": "hp", "pct": -0.12},
                    {"type": "effect", "key": "iron_body", "stacks": 2},
                ],
            },
        ],
    },
    {
        "key": "gambler_idol",
        "name": "赌徒石像",
        "desc": "一尊咧嘴大笑的石像，手里攥着两枚斑驳的骰子。",
        "options": [
            {
                "label": "与石像对赌", "desc": "押上自己的气血与运气，赌石像肚里的陪葬。",
                "actions": [
                    {"type": "hp", "pct": -0.10},
                    {"type": "gold", "mult": 2.0},
                    {"type": "effect", "key": "gold_luck", "stacks": 2},
                ],
                "risk": {
                    "chance": 0.50, "text": "石像咧嘴一笑，你的钱袋凭空瘪了一截。",
                    "actions": [{"type": "effect", "key": "curse", "stacks": 1}],
                },
            },
            {
                "label": "献上灵石", "desc": "花钱买个心安，也让石像记住你的供奉。",
                "cost_mult": 0.8,
                "actions": [{"type": "effect", "key": "gold_luck", "stacks": 2}],
            },
            {
                "label": "转身离开", "desc": "不与邪物纠缠，继续赶路。",
                "actions": [],
            },
        ],
    },
    {
        "key": "guardian_beast",
        "name": "镇墓兽",
        "desc": "石兽双目亮起幽光，拦在通往深处的石门前。",
        "options": [
            {
                "label": "血战镇墓兽", "desc": "硬碰硬撕开一条路，石兽身上也许藏着护符。",
                "actions": [
                    {"type": "hp", "pct": -0.15},
                    {"type": "loot", "count": 1},
                    {"type": "effect", "key": "ward", "stacks": 1},
                ],
                "risk": {
                    "chance": 0.30, "text": "石兽利爪撕开护体灵光，你伤得不轻。",
                    "actions": [{"type": "effect", "key": "fragile", "stacks": 2}],
                },
            },
            {
                "label": "绕行暗道", "desc": "贴着墙缝摸过去，记下石兽的巡逻规律。",
                "actions": [{"type": "effect", "key": "keen_sense", "stacks": 1}],
            },
            {
                "label": "投喂灵兽", "desc": "用灵石供品换石兽让路，顺便讨一张旧符。",
                "cost_mult": 0.6,
                "actions": [{"type": "effect", "key": "ward", "stacks": 1}],
            },
        ],
    },
    {
        "key": "alchemy_furnace",
        "name": "古修丹炉",
        "desc": "一座熄火多年的丹炉，炉腹仍残留着药香。",
        "options": [
            {
                "label": "开炉取丹", "desc": "掀开炉盖，赌里面还留着一枚古丹。",
                "actions": [
                    {"type": "effect", "key": "heal_flow", "stacks": 2},
                    {"type": "effect", "key": "ward", "stacks": 1},
                ],
                "risk": {
                    "chance": 0.35, "text": "炉中残火烧穿护体灵光，你被灼伤。",
                    "actions": [
                        {"type": "hp", "pct": -0.12},
                        {"type": "effect", "key": "fragile", "stacks": 2},
                    ],
                },
            },
            {
                "label": "拆炉取材", "desc": "把炉壁上的灵金与药渣一并拆走。",
                "actions": [
                    {"type": "loot", "count": 1},
                    {"type": "gold", "mult": 0.5},
                ],
            },
            {
                "label": "添柴炼体", "desc": "引炉火淬炼筋骨，借余温逼出体内杂质。",
                "actions": [
                    {"type": "hp", "pct": -0.08},
                    {"type": "effect", "key": "iron_body", "stacks": 1},
                ],
            },
        ],
    },
    {
        "key": "maze",
        "name": "机关迷宫",
        "desc": "四面石壁缓缓移动，脚下地砖刻满看不懂的纹路。",
        "options": [
            {
                "label": "强行破阵", "desc": "以蛮力撞开机关，赌自己的身板够硬。",
                "actions": [
                    {"type": "hp", "pct": -0.12},
                    {"type": "effect", "key": "ward", "stacks": 2},
                ],
                "risk": {
                    "chance": 0.25, "text": "机关连环触发，你的护体被磨去一层。",
                    "actions": [{"type": "effect", "key": "fragile", "stacks": 1}],
                },
            },
            {
                "label": "推演生门", "desc": "静心推演阵纹，找出真正的出口。",
                "actions": [
                    {"type": "loot", "count": 2},
                    {"type": "effect", "key": "treasure_luck", "stacks": 1},
                ],
                "risk": {
                    "chance": 0.40, "text": "推演出了偏差，你被阵纹反噬。",
                    "actions": [{"type": "hp", "pct": -0.15}],
                },
            },
            {
                "label": "原路返回", "desc": "不冒这个险，退回安全处重新观察。",
                "actions": [{"type": "effect", "key": "keen_sense", "stacks": 1}],
            },
        ],
    },
    {
        "key": "merchant",
        "name": "幽冥商贩",
        "desc": "一个披着黑斗篷的身影在墓道里摆摊，货物来路不明。",
        "options": [
            {
                "label": "买护身符", "desc": "摊主说这符能替你挡下墓府里的致命机关。",
                "cost_mult": 0.9,
                "actions": [{"type": "effect", "key": "ward", "stacks": 2}],
            },
            {
                "label": "买寻宝罗盘", "desc": "罗盘指针乱转，据说能指向陪葬品所在。",
                "cost_mult": 1.1,
                "actions": [{"type": "effect", "key": "treasure_luck", "stacks": 2}],
            },
            {
                "label": "买回血丹", "desc": "丹药腥气扑鼻，但确实是疗伤的好东西。",
                "cost_mult": 0.7,
                "actions": [
                    {"type": "hp", "pct": 0.20},
                    {"type": "effect", "key": "heal_flow", "stacks": 1},
                ],
            },
            {
                "label": "离开摊位", "desc": "不买来路不明的东西，继续深入。",
                "actions": [],
            },
        ],
    },
    {
        "key": "sword_tomb",
        "name": "剑冢",
        "desc": "万千残剑插在土中，剑鸣如泣，隐隐指向一柄古剑。",
        "options": [
            {
                "label": "拔出古剑", "desc": "握住剑柄的瞬间，凌厉剑意冲入识海。",
                "actions": [
                    {"type": "hp", "pct": -0.10},
                    {"type": "effect", "key": "gold_luck", "stacks": 2},
                    {"type": "effect", "key": "treasure_luck", "stacks": 1},
                ],
                "risk": {
                    "chance": 0.45, "text": "古剑不认主，剑气反噬神魂。",
                    "actions": [{"type": "effect", "key": "fragile", "stacks": 2}],
                },
            },
            {
                "label": "祭拜剑冢", "desc": "以灵力安抚剑灵，剑冢回以温润灵气。",
                "actions": [
                    {"type": "hp", "pct": 0.10},
                    {"type": "effect", "key": "treasure_luck", "stacks": 1},
                ],
            },
            {
                "label": "收集残剑", "desc": "把还能用的残剑捆成一束带走。",
                "actions": [
                    {"type": "loot", "count": 1},
                    {"type": "gold", "mult": 0.6},
                ],
            },
        ],
    },
    {
        "key": "blood_pool",
        "name": "血池",
        "desc": "一池暗红液体翻滚着血气，池底似乎沉着什么。",
        "options": [
            {
                "label": "沐浴血池", "desc": "以血气修补伤势，但邪气也会趁虚而入。",
                "actions": [
                    {"type": "hp", "pct": 0.18},
                    {"type": "effect", "key": "fragile", "stacks": 1},
                ],
            },
            {
                "label": "汲取血精", "desc": "强行炼化池中血精，以伤换财。",
                "actions": [
                    {"type": "hp", "pct": -0.18},
                    {"type": "effect", "key": "gold_luck", "stacks": 2},
                    {"type": "effect", "key": "treasure_luck", "stacks": 1},
                ],
            },
            {
                "label": "净化血池", "desc": "以灵力净化污血，顺便洗去身上的邪气。",
                "actions": [
                    {"type": "effect", "key": "heal_flow", "stacks": 1},
                    {"type": "cleanse", "count": 1},
                ],
            },
        ],
    },
    {
        "key": "stone_tablet",
        "name": "古碑",
        "desc": "半截残碑立在墓室中央，碑文被岁月磨得模糊不清。",
        "options": [
            {
                "label": "参悟古碑", "desc": "凝神参悟碑文，或许能窥得古修的一鳞半爪。",
                "actions": [
                    {"type": "rand_effect", "stacks": 2,
                     "pool": ["iron_body", "keen_sense", "treasure_luck", "gold_luck"]},
                ],
                "risk": {
                    "chance": 0.40, "text": "碑文暗藏杀机，你的心神被狠狠撕扯。",
                    "actions": [
                        {"type": "hp", "pct": -0.12},
                        {"type": "effect", "key": "fragile", "stacks": 1},
                    ],
                },
            },
            {
                "label": "拓印碑文", "desc": "把碑文拓下来，出去后也许能卖个好价钱。",
                "actions": [
                    {"type": "loot", "count": 1},
                    {"type": "gold", "mult": 0.5},
                ],
            },
            {
                "label": "静心调息", "desc": "不碰碑文，借墓室寂静梳理自身灵力。",
                "actions": [{"type": "effect", "key": "heal_flow", "stacks": 1}],
            },
        ],
    },
    {
        "key": "fox_mirage",
        "name": "妖狐幻境",
        "desc": "粉雾弥漫，隐约有女子轻笑，脚下的路变得真假难辨。",
        "options": [
            {
                "label": "破幻而出", "desc": "咬破舌尖，以痛楚守住灵台清明。",
                "actions": [
                    {"type": "hp", "pct": -0.10},
                    {"type": "effect", "key": "keen_sense", "stacks": 2},
                ],
            },
            {
                "label": "沉沦幻境", "desc": "在幻境中醉生梦死，醒来时怀里多了些东西。",
                "actions": [
                    {"type": "effect", "key": "gold_luck", "stacks": 3},
                    {"type": "effect", "key": "curse", "stacks": 1},
                ],
            },
            {
                "label": "献宝求路", "desc": "给幻境中的妖狐留下买路财，换一条生路。",
                "cost_mult": 1.0,
                "actions": [{"type": "effect", "key": "treasure_luck", "stacks": 2}],
            },
        ],
    },
    {
        "key": "grave_keeper",
        "name": "守墓人",
        "desc": "一个佝偻老人坐在石阶上，脚边放着一盏将熄的油灯。",
        "options": [
            {
                "label": "与守墓人交易", "desc": "用灵石换一碗热汤与片刻喘息。",
                "cost_mult": 0.8,
                "actions": [
                    {"type": "hp", "pct": 0.25},
                    {"type": "effect", "key": "heal_flow", "stacks": 1},
                ],
            },
            {
                "label": "强夺祭品", "desc": "老人守着的祭品里，说不定有值钱的东西。",
                "actions": [{"type": "loot", "count": 2}],
                "risk": {
                    "chance": 0.40, "text": "老人突然暴起，枯瘦的手爪撕开了你的护体。",
                    "actions": [
                        {"type": "hp", "pct": -0.15},
                        {"type": "effect", "key": "fragile", "stacks": 1},
                    ],
                },
            },
            {
                "label": "问路", "desc": "向老人打听前路机关，记在心里。",
                "actions": [{"type": "effect", "key": "keen_sense", "stacks": 1}],
            },
        ],
    },
]


class TreasureMapManager:
    def __init__(self, db: DataBase, storage_ring_mgr, config_manager, combat_mgr=None, gamble_mgr=None):
        self.db = db
        self.storage_ring_mgr = storage_ring_mgr
        self.config_manager = config_manager
        self.combat_mgr = combat_mgr
        self.gamble_mgr = gamble_mgr

    # ================= 基础查询 =================
    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        keys = [d[0] for d in cursor.description]
        return dict(zip(keys, row))

    async def _fetchall(self, sql: str, params: tuple = ()) -> List[dict]:
        async with self.db.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            keys = [d[0] for d in cursor.description]
        return [dict(zip(keys, row)) for row in rows]

    @staticmethod
    async def is_climbing(db: DataBase, user_id: str) -> bool:
        try:
            async with db.conn.execute(
                "SELECT 1 FROM treasure_map_runs WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ) as cursor:
                return await cursor.fetchone() is not None
        except Exception:
            return False

    async def get_active_run(self, user_id: str) -> Optional[dict]:
        run = await self._fetchone(
            "SELECT * FROM treasure_map_runs WHERE user_id = ? AND status = 'active'",
            (user_id,),
        )
        if not run:
            return None
        try:
            run["loot"] = json.loads(run.get("loot") or "[]")
        except Exception:
            run["loot"] = []
        try:
            run["log"] = json.loads(run.get("log") or "[]")
        except Exception:
            run["log"] = []
        try:
            run["effects"] = json.loads(run.get("effects") or "[]")
        except Exception:
            run["effects"] = []
        # 保留原始 JSON，供「选择奇遇」时做 CAS 守卫：同一事件只能被处理一次。
        raw_event = run.get("event") or "{}"
        run["_event_raw"] = raw_event
        try:
            run["event"] = json.loads(raw_event)
        except Exception:
            run["event"] = {}
        return run

    # ================= 肉鸽效果辅助 =================
    @staticmethod
    def _effect_stacks(effects: List[dict], key: str) -> int:
        total = 0
        for item in effects or []:
            if not isinstance(item, dict) or item.get("key") != key:
                continue
            try:
                total += max(0, int(item.get("stacks") or 0))
            except (TypeError, ValueError):
                continue
        return total

    @classmethod
    def effect_rate(cls, effects: List[dict], key: str) -> float:
        """返回某个百分比效果的合计数值（已按上限截断）。"""
        cfg = EFFECT_DEFS.get(key)
        if not cfg:
            return 0.0
        stacks = min(cls._effect_stacks(effects, key), int(cfg.get("max") or 1))
        try:
            value = float(cfg.get("value") or 0)
        except (TypeError, ValueError):
            value = 0.0
        return stacks * value

    @staticmethod
    def _add_effect(effects: List[dict], key: str, stacks: int = 1) -> str:
        cfg = EFFECT_DEFS.get(key)
        if not cfg:
            return ""
        try:
            stacks = max(1, int(stacks))
        except (TypeError, ValueError):
            stacks = 1
        cap = max(1, int(cfg.get("max") or 1))
        entry = next(
            (item for item in effects if isinstance(item, dict) and item.get("key") == key),
            None,
        )
        if entry is None:
            entry = {"key": key, "stacks": 0}
            effects.append(entry)
        before = max(0, int(entry.get("stacks") or 0))
        after = min(cap, before + stacks)
        entry["stacks"] = after
        if after <= before:
            return f"【{cfg['name']}】已达叠加上限（{cap} 层）"
        gained = after - before
        return f"获得临时效果【{cfg['name']}×{gained}】（当前 {after}/{cap} 层）"

    @staticmethod
    def _remove_effect(effects: List[dict], key: str, stacks: int = 1) -> int:
        try:
            stacks = max(0, int(stacks))
        except (TypeError, ValueError):
            stacks = 0
        removed = 0
        for item in list(effects or []):
            if not isinstance(item, dict) or item.get("key") != key:
                continue
            try:
                current = max(0, int(item.get("stacks") or 0))
            except (TypeError, ValueError):
                current = 0
            take = min(current, stacks - removed)
            if take <= 0:
                break
            item["stacks"] = current - take
            removed += take
            if item["stacks"] <= 0:
                effects.remove(item)
            if removed >= stacks:
                break
        return removed

    @classmethod
    def effect_value_text(cls, key: str, stacks: int) -> str:
        cfg = EFFECT_DEFS.get(key) or {}
        try:
            pct = float(cfg.get("value") or 0) * int(stacks or 0)
        except (TypeError, ValueError):
            pct = 0.0
        n = max(0, int(stacks or 0))
        return str(cfg.get("desc") or "").format(
            pct=f"{pct:.0%}",
            extra=f"{min(0.50, pct * 0.8):.0%}",
            n=n,
        )

    @classmethod
    def _effect_line(cls, effects: List[dict]) -> str:
        parts = []
        for key in EFFECT_DEFS:
            stacks = cls._effect_stacks(effects, key)
            if stacks <= 0:
                continue
            cap = int(EFFECT_DEFS[key].get("max") or 1)
            parts.append(f"{EFFECT_DEFS[key]['name']}×{min(stacks, cap)}")
        return "、".join(parts) if parts else "无"

    @classmethod
    def _effects_detail(cls, effects: List[dict]) -> List[str]:
        lines = []
        for key, cfg in EFFECT_DEFS.items():
            stacks = cls._effect_stacks(effects, key)
            if stacks <= 0:
                continue
            stacks = min(stacks, int(cfg.get("max") or 1))
            icon = "🟢" if cfg.get("kind") == "buff" else ("🟡" if cfg.get("kind") == "token" else "🔴")
            lines.append(f"{icon} {cfg['name']}×{stacks}：{cls.effect_value_text(key, stacks)}")
        return lines

    async def _max_hp(self, player: Player) -> int:
        if self.combat_mgr:
            try:
                stats = self.combat_mgr.build_player_combat_stats(
                    player, self.config_manager, db=self.db
                )
                return max(1, int(stats.max_hp))
            except Exception:
                pass
        return max(1, int(player.max_blood_qi or 100))

    # ================= 回血上限 =================
    @staticmethod
    async def allow_heal(db: DataBase, user_id: str, requested: int, max_hp: int) -> int:
        """爬塔期间把一次回血裁剪到本层剩余额度；非爬塔状态原样返回。"""
        if requested <= 0:
            return requested
        try:
            async with db.conn.execute(
                "SELECT heal_used, heal_budget FROM treasure_map_runs "
                "WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return requested
            used, budget = int(row[0] or 0), int(row[1] or 0)
            remaining = max(0, budget - used)
            allowed = min(int(requested), remaining)
            if allowed > 0:
                await db.conn.execute(
                    "UPDATE treasure_map_runs SET heal_used = heal_used + ? "
                    "WHERE user_id = ? AND status = 'active'",
                    (allowed, user_id),
                )
                await db.conn.commit()
            return allowed
        except Exception:
            return requested

    # ================= 战斗写回裁剪 =================
    @staticmethod
    async def clamp_combat_hp(db: DataBase, user_id: str, new_hp: int) -> int:
        """战斗结束写回气血前裁剪爬塔玩家的治疗量。

        爬塔期间禁止自然恢复/调息，丹药回血也受每层额度限制。但切磋战后是
        「按满血写回」、决斗/世界Boss/悬赏的战斗内又有吸血与治疗，若直接落库
        就等于绕过额度。这里统一按 :meth:`allow_heal` 裁剪：掉血照常，
        回血只能吃掉本层剩余额度。非爬塔状态原样返回。
        """
        try:
            new_hp = int(new_hp)
        except (TypeError, ValueError):
            return new_hp
        try:
            async with db.conn.execute(
                "SELECT p.hp FROM treasure_map_runs r "
                "JOIN players p ON p.user_id = r.user_id "
                "WHERE r.user_id = ? AND r.status = 'active'",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return new_hp
            current = int(row[0] or 0)
            if new_hp <= current:
                return new_hp
            allowed = await TreasureMapManager.allow_heal(db, user_id, new_hp - current, 0)
            return current + max(0, int(allowed or 0))
        except Exception:
            return new_hp

    # ================= 战利品 =================
    def _roll_loot(self, tier: int, floor_no: int, rng, bonus: float = 0.0) -> List[dict]:
        cfg = MAP_TIERS.get(tier, MAP_TIERS[1])
        loot: List[dict] = []
        try:
            bonus = max(0.0, min(0.5, float(bonus or 0)))
        except (TypeError, ValueError):
            bonus = 0.0
        if rng.random() > float(cfg["loot_chance"]) + floor_no * 0.03 + bonus:
            return loot
        if self.gamble_mgr:
            try:
                stone_tier = min(5, tier + (1 if floor_no >= 4 else 0))
                hidden = self.gamble_mgr._roll_tier_loot(stone_tier, rng)
                if hidden.get("kind") != "dust":
                    loot.extend(hidden.get("loot") or [])
                if hidden.get("kind") == "material" and rng.random() < 0.35:
                    extra = self.gamble_mgr._roll_tier_loot(stone_tier, rng)
                    loot.extend(extra.get("loot") or [])
            except Exception as exc:
                logger.warning(f"【藏宝图】掉落生成失败: {exc}")
        if not loot:
            catalog = getattr(self.config_manager, "material_catalog", {}) or {}
            pool = [x for x in catalog.values() if isinstance(x, dict)]
            if pool:
                info = rng.choice(pool)
                loot.append({
                    "kind": "material", "name": info.get("name"),
                    "rank": info.get("tier") or "凡品", "qty": 1,
                })
        return loot

    def _loot_label(self, loot: dict) -> str:
        if self.gamble_mgr:
            try:
                return self.gamble_mgr._loot_label(loot)
            except Exception:
                pass
        name = loot.get("name") or "碎屑"
        qty = int(loot.get("qty") or 1)
        return f"【{name}】" + (f"x{qty}" if qty > 1 else "")

    # ================= 奇遇 =================
    @staticmethod
    def _scale_gold(tier: int, floor_no: int, mult: float) -> int:
        cfg = MAP_TIERS.get(tier, MAP_TIERS[1])
        try:
            base = int(cfg["gold"]) * max(1, int(floor_no))
            return max(1, int(base * float(mult or 0)))
        except (TypeError, ValueError):
            return 0

    def _build_event(self, run: dict, floor_no: int, rng) -> dict:
        """为下一层生成一次奇遇；尽量避开最近出现过的奇遇。"""
        recent = " ".join(str(x) for x in (run.get("log") or []))
        pool = [ev for ev in EVENT_POOL if ev.get("name") not in recent] or EVENT_POOL
        picked = rng.choice(pool)
        tier = int(run.get("map_tier") or 1)
        options = []
        for opt in picked.get("options") or []:
            item = {
                "label": opt.get("label") or "查看",
                "desc": opt.get("desc") or "",
                "actions": list(opt.get("actions") or []),
            }
            if opt.get("risk"):
                item["risk"] = {
                    "chance": float(opt["risk"].get("chance") or 0),
                    "text": opt["risk"].get("text") or "事与愿违。",
                    "actions": list(opt["risk"].get("actions") or []),
                }
            if opt.get("cost_mult"):
                item["cost_gold"] = self._scale_gold(
                    tier, floor_no, float(opt.get("cost_mult") or 0)
                )
            options.append(item)
        return {
            "key": picked.get("key"),
            "name": picked.get("name"),
            "desc": picked.get("desc") or "",
            "floor": int(floor_no),
            "options": options,
        }

    @classmethod
    def _event_prompt(cls, run: dict) -> str:
        event = run.get("event") or {}
        options = event.get("options") or []
        lines = [
            f"🎲 第 {int(event.get('floor') or int(run.get('floor') or 0) + 1)} 层奇遇 · "
            f"{event.get('name') or '未知奇遇'}",
            "━━━━━━━━━━━━━━━",
            str(event.get("desc") or ""),
            "请做出选择：",
        ]
        for idx, opt in enumerate(options, 1):
            suffix = ""
            try:
                cost = int(opt.get("cost_gold") or 0)
            except (TypeError, ValueError):
                cost = 0
            if cost > 0:
                suffix = f"（需 {cost:,} 灵石）"
            lines.append(f"{idx}. {opt.get('label') or '查看'}{suffix}")
            if opt.get("desc"):
                lines.append(f"   {opt['desc']}")
        lines.extend([
            "━━━━━━━━━━━━━━━",
            f"当前临时效果：{cls._effect_line(run.get('effects') or [])}",
            "💡 `/藏宝图 选择 <编号>` 做出抉择｜`/藏宝图 撤退` 放弃本次探索",
        ])
        return "\n".join(lines)

    async def choose(self, player: Player, index: int) -> Tuple[bool, str]:
        """处理当前待选择的奇遇，按选择结算临时效果与即时收益/损失。"""
        run = await self.get_active_run(player.user_id)
        if not run:
            return False, "❌ 你当前没有在爬塔。使用 `/藏宝图 开始 <1-3>` 进入。"
        event = run.get("event") or {}
        options = event.get("options") or []
        if not options:
            return False, "❌ 当前没有待处理的奇遇，直接 `/藏宝图 继续` 深入即可。"
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False, "❌ 奇遇选项编号必须是整数。"
        if index < 1 or index > len(options):
            return False, f"❌ 奇遇选项编号只能是 1-{len(options)}。"
        option = options[index - 1]

        fresh = await self._fetchone(
            "SELECT hp, gold FROM players WHERE user_id = ?", (player.user_id,)
        )
        hp = int((fresh or {}).get("hp") or player.hp or 0)
        gold = int((fresh or {}).get("gold") or player.gold or 0)
        max_hp = max(1, int(run.get("max_hp") or 1))
        tier = int(run.get("map_tier") or 1)
        floor_no = max(1, int(event.get("floor") or int(run.get("floor") or 0) + 1))
        effects = list(run.get("effects") or [])
        loot = list(run.get("loot") or [])
        log_entries = list(run.get("log") or [])

        try:
            cost_gold = max(0, int(option.get("cost_gold") or 0))
        except (TypeError, ValueError):
            cost_gold = 0
        if cost_gold > gold:
            return False, (
                f"❌ 你选择了「{option.get('label')}」，但需要 {cost_gold:,} 灵石，"
                f"你当前只有 {gold:,}。"
            )

        rng = seeded_rng(
            "map-event", player.user_id, run.get("started_at"),
            event.get("key"), floor_no, index,
        )
        actions = list(option.get("actions") or [])
        result_text = str(option.get("desc") or "")
        risk = option.get("risk") or {}
        if risk and rng.random() < float(risk.get("chance") or 0):
            actions = list(risk.get("actions") or [])
            result_text = str(risk.get("text") or "事与愿违。")

        gold -= cost_gold
        changes: List[str] = []
        if cost_gold:
            changes.append(f"灵石 -{cost_gold:,}")
            log_entries.append(f"奇遇「{option.get('label')}」花费 {cost_gold:,} 灵石")

        # 先记录选择，再逐条执行动作。
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = action.get("type")
            if kind == "effect":
                msg = self._add_effect(effects, str(action.get("key") or ""),
                                       int(action.get("stacks") or 1))
                if msg:
                    changes.append(msg)
            elif kind == "rand_effect":
                pool = [k for k in (action.get("pool") or []) if k in EFFECT_DEFS]
                if pool:
                    key = rng.choice(pool)
                    msg = self._add_effect(effects, key, int(action.get("stacks") or 1))
                    if msg:
                        changes.append(msg)
            elif kind == "hp":
                try:
                    pct = float(action.get("pct") or 0)
                except (TypeError, ValueError):
                    pct = 0.0
                delta = int(max_hp * abs(pct))
                if pct >= 0:
                    healed = min(max_hp - hp, delta)
                    if healed > 0:
                        hp += healed
                        changes.append(f"气血 +{healed:,}")
                else:
                    hp -= delta
                    changes.append(f"气血 -{delta:,}")
            elif kind == "gold":
                amount = self._scale_gold(tier, floor_no, float(action.get("mult") or 0))
                if amount > 0:
                    loot.append({"kind": "gold", "amount": amount})
                    changes.append(f"灵石 +{amount:,}（撤退时结算）")
                    log_entries.append(f"奇遇所得灵石 {amount:,}（撤退时结算）")
            elif kind == "loot":
                count = max(0, int(action.get("count") or 0))
                for _ in range(count):
                    found = self._roll_loot(tier, floor_no, rng, bonus=0.5)
                    for item in found:
                        loot.append(item)
                        changes.append("发现 " + self._loot_label(item))
                        log_entries.append("奇遇发现 " + self._loot_label(item))
            elif kind == "cleanse":
                count = max(0, int(action.get("count") or 0))
                for key in ("fragile", "curse"):
                    if count <= 0:
                        break
                    removed = self._remove_effect(effects, key, count)
                    if removed:
                        count -= removed
                        changes.append(f"祛除【{EFFECT_DEFS[key]['name']}×{removed}】")

        dead = hp <= 0
        if dead:
            hp = 0
        effects_json = json.dumps(effects, ensure_ascii=False)
        loot_json = json.dumps([] if dead else loot, ensure_ascii=False)
        log_entries.append(f"奇遇「{option.get('label')}」：{result_text}")
        # 死亡时把本次战利品与临时效果一并清空，只保留历史日志。
        effects_json = "[]" if dead else effects_json
        status = "ended" if dead else "active"
        player_hp = 1 if dead else hp
        new_budget = int(run.get("heal_budget") or 0)
        if not dead and self.effect_rate(effects, "heal_flow"):
            new_budget = max(1, int(max_hp * HEAL_BUDGET_RATE * (
                1 + self.effect_rate(effects, "heal_flow")
            )))

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            payload = (
                status, player_hp, effects_json, loot_json,
                json.dumps(log_entries[-12:], ensure_ascii=False),
                new_budget, int(time.time()), player.user_id, run.get("_event_raw") or "{}",
            )
            cursor = await self.db.conn.execute(
                "UPDATE treasure_map_runs SET status=?, hp=?, effects=?, loot=?, log=?, "
                "heal_budget=?, updated_at=?, event='{}' "
                "WHERE user_id = ? AND status='active' AND event = ?",
                payload,
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "⏳ 这次奇遇已经被处理过了，或本次爬塔已结算。"
            if cost_gold:
                upd = await self.db.conn.execute(
                    "UPDATE players SET hp = ?, gold = gold - ? "
                    "WHERE user_id = ? AND gold >= ?",
                    (player_hp, cost_gold, player.user_id, cost_gold),
                )
                if not upd.rowcount:
                    await self.db.conn.rollback()
                    return False, "❌ 灵石不足，无法完成这次奇遇选择。"
            else:
                await self.db.conn.execute(
                    "UPDATE players SET hp = ? WHERE user_id = ?",
                    (player_hp, player.user_id),
                )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        player.hp = player_hp
        player.gold = gold if not dead else max(0, gold)

        if dead:
            lost_items = sum(1 for x in loot if x.get("kind") != "gold")
            lost_gold = sum(int(x.get("amount") or 0) for x in loot if x.get("kind") == "gold")
            lost_desc = f"本次获得的 {lost_items} 件战利品"
            if lost_gold:
                lost_desc += f"与 {lost_gold:,} 灵石"
            return True, (
                f"💀 你在奇遇「{event.get('name')}」中倒下了！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{result_text}\n"
                f"气血被打空，{lost_desc}全部遗失。\n"
                f"你拖着残躯爬出墓府，气血仅余 1 点。"
            )

        lines = [
            f"🎲 奇遇 · {event.get('name') or '未知'}",
            "━━━━━━━━━━━━━━━",
            f"你选择了「{option.get('label')}」。",
            result_text,
        ]
        if changes:
            lines.extend(f"· {x}" for x in changes)
        lines.extend([
            f"气血：{hp:,} / {max_hp:,}",
            f"当前临时效果：{self._effect_line(effects)}",
            "━━━━━━━━━━━━━━━",
            f"💡 `/藏宝图 继续` 深入第 {floor_no} 层｜`/藏宝图 撤退` 落袋为安",
        ])
        return True, "\n".join(lines)

    # ================= 指令 =================
    async def get_status(self, player: Player) -> str:
        run = await self.get_active_run(player.user_id)
        if not run:
            return (
                "🗺️ 古修藏宝图\n"
                "━━━━━━━━━━━━━━━\n"
                "逐层深入古修墓府，每层都可能遇到陷阱与妖兽。\n"
                "每层结算后还会触发随机奇遇，做出选择即可获得临时效果，构筑本趟爬塔的增益。\n"
                "气血归零则本次战利品尽失；可随时撤退落袋为安。\n"
                "⚠️ 爬塔期间无法自然恢复/调息；丹药回血每层有上限，符箓不能直接补战斗气血。\n"
                "⚠️ 战斗也不回血：切磋不按满血复原，决斗/世界Boss/悬赏的战斗吸血只吃本层剩余额度。\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 `/藏宝图 开始 <1-3>` 购买并进入：\n"
                "  1 残破藏宝图 8,000 灵石\n"
                "  2 古修藏宝图 40,000 灵石\n"
                "  3 太古董藏图 180,000 灵石\n"
                "💡 `/藏宝图 继续` 深入一层｜`/藏宝图 选择 <编号>` 处理奇遇｜`/藏宝图 撤退` 带走战利品"
            )
        max_hp = int(run.get("max_hp") or 1)
        hp = int(player.hp or 0)
        budget = int(run.get("heal_budget") or 0)
        used = int(run.get("heal_used") or 0)
        lines = [
            "🗺️ 古修藏宝图 · 爬塔中",
            "━━━━━━━━━━━━━━━",
            f"地图：{MAP_TIERS.get(int(run['map_tier']), {}).get('name', '未知')}",
            f"层数：第 {int(run['floor'])} / {MAX_FLOOR} 层",
            f"气血：{hp:,} / {max_hp:,}",
            f"本层回血额度：{max(0, budget - used):,} / {budget:,}",
            f"已获战利品：{len(run.get('loot') or [])} 件",
            f"临时效果：{self._effect_line(run.get('effects') or [])}",
        ]
        detail = self._effects_detail(run.get("effects") or [])
        if detail:
            lines.append("效果明细：")
            lines.extend(f"· {line}" for line in detail)
        if run.get("event"):
            lines.extend([
                "━━━━━━━━━━━━━━━",
                self._event_prompt(run),
            ])
        for entry in (run.get("log") or [])[-4:]:
            lines.append(f"· {entry}")
        lines.extend([
            "━━━━━━━━━━━━━━━",
            "💡 `/藏宝图 继续` 深入｜`/藏宝图 选择 <编号>` 处理奇遇｜`/藏宝图 撤退` 结算带走",
            "⚠️ 越深掉落越好，但陷阱伤害与触发率也越高。",
            "⚠️ 战斗回血同样只吃本层额度；超过 24 小时未推进会由后台按撤退结算。",
        ])
        return "\n".join(lines)

    async def start(self, player: Player, tier: int = 1) -> Tuple[bool, str]:
        tier = int(tier or 1)
        cfg = MAP_TIERS.get(tier)
        if not cfg:
            return False, "❌ 地图档位只能是 1 / 2 / 3。"
        if await self.get_active_run(player.user_id):
            return False, "❌ 你已经在爬塔途中，先 /藏宝图 继续 或 /藏宝图 撤退。"
        busy = None
        try:
            busy = await self.db.ext.first_busy_cd(player.user_id)
        except Exception:
            busy = None
        if busy is not None:
            from ..models_extended import UserStatus

            name = UserStatus.get_name(int(busy.type))
            return False, f"❌ 你正在{name}，无法同时进入藏宝图，先结束当前活动。"
        max_hp = await self._max_hp(player)
        hp = int(player.hp or 0)
        if hp <= 0:
            return False, "❌ 你当前气血不足，先恢复后再进藏宝图。"
        if hp < max_hp * MIN_START_HP_RATE:
            return False, (
                f"❌ 气血低于 {int(MIN_START_HP_RATE * 100)}% 时进入必死无疑。\n"
                f"当前气血：{hp:,}/{max_hp:,}，请先调息恢复。"
            )
        price = int(cfg["price"])
        if player.gold < price:
            return False, f"❌ 购买【{cfg['name']}】需要 {price:,} 灵石，你当前只有 {player.gold:,}。"

        now = int(time.time())
        budget = max(1, int(max_hp * HEAL_BUDGET_RATE))
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (price, player.user_id, price),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 灵石不足，购买失败。"
            await self.db.conn.execute(
                "INSERT INTO treasure_map_runs "
                "(user_id, floor, map_tier, hp, max_hp, heal_used, heal_budget, loot, log, "
                "effects, event, status, started_at, updated_at) "
                "VALUES (?, 0, ?, ?, ?, 0, ?, '[]', '[]', '[]', '{}', 'active', ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET floor=0, map_tier=excluded.map_tier, "
                "hp=excluded.hp, max_hp=excluded.max_hp, heal_used=0, heal_budget=excluded.heal_budget, "
                "loot='[]', log='[]', effects='[]', event='{}', status='active', "
                "started_at=excluded.started_at, updated_at=excluded.updated_at",
                (player.user_id, tier, hp, max_hp, budget, now, now),
            )
            await self.db.conn.commit()
            player.gold -= price
        except Exception:
            await self.db.conn.rollback()
            raise
        return True, (
            f"🗺️ 你花费 {price:,} 灵石买下【{cfg['name']}】，踏入古修墓府。\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{cfg['desc']}\n"
            f"当前气血：{hp:,}/{max_hp:,}\n"
            f"⚠️ 爬塔期间无法自然恢复/调息；丹药回血每层最多 {budget:,}，符箓不能直接补战斗气血。\n"
            f"⚠️ 战斗回血（切磋满血复原、决斗/世界Boss/悬赏吸血）同样只吃这 {budget:,} 的额度。\n"
            f"🎲 每层结算后会触发随机奇遇，选择可叠加临时效果；层数越深收益越高。\n"
            f"💡 `/藏宝图 继续` 深入第一层"
        )

    async def advance(self, player: Player) -> Tuple[bool, str]:
        run = await self.get_active_run(player.user_id)
        if not run:
            return False, "❌ 你当前没有在爬塔。使用 `/藏宝图 开始 <1-3>` 进入。"
        if run.get("event"):
            return False, self._event_prompt(run)
        old_floor = int(run["floor"])
        floor_no = old_floor + 1
        if floor_no > MAX_FLOOR:
            return await self._settle(player, run, success=True, reason="已通关全图")
        rule = FLOOR_RULES.get(floor_no, FLOOR_RULES[MAX_FLOOR])
        max_hp = int(run.get("max_hp") or 1)
        effects = list(run.get("effects") or [])
        rng = seeded_rng("map", player.user_id, run["started_at"], floor_no)

        # 先结算本层伤害：铜皮铁骨减伤、魂体受损增伤、灵觉敏锐降低陷阱率。
        damage_reduce = min(0.60, self.effect_rate(effects, "iron_body"))
        damage_amp = self.effect_rate(effects, "fragile")
        trap_chance = max(0.0, float(rule["trap"]) - self.effect_rate(effects, "keen_sense"))
        low, high = rule["damage"]
        damage_pct = rng.uniform(low, high)
        damage_pct *= max(0.20, 1.0 - damage_reduce + damage_amp)
        trap = rng.random() < trap_chance
        if trap:
            damage_pct *= rule["trap_mult"]
        damage = max(1, int(max_hp * damage_pct))
        ward_used = False
        if trap and self._effect_stacks(effects, "ward") > 0:
            self._remove_effect(effects, "ward", 1)
            damage = 0
            ward_used = True
        hp_before = int(player.hp or 0)
        hp_after = hp_before - damage

        log_entries = list(run.get("log") or [])
        if ward_used:
            log_entries.append(f"第{floor_no}层护身灵符挡下陷阱")
        elif trap:
            log_entries.append(f"第{floor_no}层触发陷阱，气血 -{damage:,}")
        else:
            log_entries.append(f"第{floor_no}层环境侵蚀，气血 -{damage:,}")

        if hp_after <= 0:
            # 失败：清空战利品与临时效果，气血压到 1，结束爬塔
            old_loot = list(run.get("loot") or [])
            lost_items = [x for x in old_loot if x.get("kind") != "gold"]
            lost_gold = sum(int(x.get("amount") or 0) for x in old_loot if x.get("kind") == "gold")
            # 带 status 守卫：后台 settle_stale_runs 可能刚好把这条 run 按撤退结算掉，
            # 此时战利品已经发放，不能再显示「全部遗失」误导玩家。
            cursor = await self.db.conn.execute(
                "UPDATE treasure_map_runs SET status='ended', floor=?, hp=1, loot='[]', "
                "effects='[]', event='{}', log=?, "
                "updated_at=? WHERE user_id = ? AND status='active'",
                (
                    floor_no,
                    json.dumps(log_entries[-12:], ensure_ascii=False),
                    int(time.time()), player.user_id,
                ),
            )
            already_settled = not cursor.rowcount
            await self.db.conn.execute(
                "UPDATE players SET hp = 1 WHERE user_id = ?", (player.user_id,)
            )
            await self.db.conn.commit()
            player.hp = 1
            if already_settled:
                return True, (
                    f"💀 你在第 {floor_no} 层倒下了！\n"
                    "━━━━━━━━━━━━━━━\n"
                    "⚠️ 这座墓府已被后台按「撤退」结算过，此前带回的战利品仍然有效。\n"
                    "💡 发送 `/藏宝图` 查看当前状态。"
                )
            lost_desc = f"本次获得的 {len(lost_items)} 件战利品"
            if lost_gold:
                lost_desc += f"与 {lost_gold:,} 灵石"
            return True, (
                f"💀 你在第 {floor_no} 层倒下了！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"气血被打空，{lost_desc}全部遗失，临时效果也随之消散。\n"
                f"你拖着残躯爬出墓府，气血仅余 1 点。\n"
                f"⚠️ 下次请在气血见底前及时撤退。"
            )

        # 再结算本层收获：聚财灵光/墓府诅咒影响灵石，寻宝直觉影响掉落。
        gold_gain = 0
        loot = list(run.get("loot") or [])
        cfg = MAP_TIERS.get(int(run["map_tier"]), MAP_TIERS[1])
        gold_mult = max(
            0.10,
            1.0 + self.effect_rate(effects, "gold_luck") - self.effect_rate(effects, "curse"),
        )
        if rng.random() < 0.78:
            gold_gain = int(cfg["gold"] * floor_no * rng.uniform(0.7, 1.3) * gold_mult)
            # 层内灵石先记入战利品，撤退/通关时统一结算；中途倒下则随战利品一起丢失
            loot.append({"kind": "gold", "amount": gold_gain})
            log_entries.append(f"拾得灵石 {gold_gain:,}（撤退时结算）")
        loot_bonus = self.effect_rate(effects, "treasure_luck")
        new_loot = self._roll_loot(int(run["map_tier"]), floor_no, rng, bonus=loot_bonus)
        extra_chance = min(0.50, loot_bonus * 0.8)
        if extra_chance > 0 and rng.random() < extra_chance:
            new_loot.extend(self._roll_loot(
                int(run["map_tier"]), floor_no, rng, bonus=loot_bonus
            ))
        for item in new_loot:
            loot.append(item)
            log_entries.append("发现 " + self._loot_label(item))
        if not gold_gain and not new_loot:
            log_entries.append("这一层空空如也，只有尘土")

        if floor_no >= MAX_FLOOR:
            # 最后一层：自动结算并给通关额外奖励。
            # 先把本层的 floor/loot/log 落库再结算，保证数据库里的记录
            # 与实际发放的战利品一致（否则 _settle 回滚后无法排查发了什么）。
            run["floor"] = floor_no
            run["loot"] = loot
            run["log"] = log_entries
            run["effects"] = effects
            run["event"] = {}
            cursor = await self.db.conn.execute(
                "UPDATE treasure_map_runs SET floor=?, hp=?, loot=?, log=?, effects=?, "
                "event='{}', updated_at=? WHERE user_id = ? AND status='active'",
                (
                    floor_no, hp_after,
                    json.dumps(loot, ensure_ascii=False),
                    json.dumps(log_entries[-12:], ensure_ascii=False),
                    json.dumps(effects, ensure_ascii=False),
                    int(time.time()), player.user_id,
                ),
            )
            if not cursor.rowcount:
                return False, (
                    "⏳ 本次爬塔已被后台按「撤退」结算，此前战利品已发放。\n"
                    "💡 发送 `/藏宝图` 查看状态，或 `/藏宝图 开始 <1-3>` 重新进入。"
                )
            await self.db.conn.execute(
                "UPDATE players SET hp = ? WHERE user_id = ?", (hp_after, player.user_id)
            )
            await self.db.conn.commit()
            player.hp = hp_after
            return await self._settle(player, run, success=True, reason="通关主墓")

        # 每层结算后生成一次奇遇，处理完才能继续深入。
        next_event = self._build_event(run, floor_no + 1, rng)
        log_entries.append(f"奇遇：{next_event['name']}（待抉择）")
        budget = max(1, int(max_hp * HEAL_BUDGET_RATE * (
            1 + self.effect_rate(effects, "heal_flow")
        )))
        # 落库带 status/floor/event 三重守卫：后台结算、并发推进、重复处理奇遇
        # 都不能把本层结果写成两条。
        #
        # 这里刻意不开事务：单条条件 UPDATE 在 SQLite 里本身就是原子的，守卫语义
        # 已经完整；而全库共用一条 autocommit 连接，多一个 BEGIN IMMEDIATE 就多一处
        # 「cannot start a transaction within a transaction」的嵌套抛错面。
        cursor = await self.db.conn.execute(
            "UPDATE treasure_map_runs SET floor=?, hp=?, heal_used=0, heal_budget=?, "
            "loot=?, log=?, effects=?, event=?, updated_at=? "
            "WHERE user_id = ? AND status='active' AND floor = ? AND event = ?",
            (
                floor_no, hp_after, budget,
                json.dumps(loot, ensure_ascii=False),
                json.dumps(log_entries[-12:], ensure_ascii=False),
                json.dumps(effects, ensure_ascii=False),
                json.dumps(next_event, ensure_ascii=False),
                int(time.time()), player.user_id, old_floor, run.get("_event_raw") or "{}",
            ),
        )
        if not cursor.rowcount:
            current = await self.get_active_run(player.user_id)
            if not current:
                return False, (
                    "⏳ 本次爬塔已被后台按「撤退」结算，此前战利品已发放。\n"
                    "💡 发送 `/藏宝图` 查看状态，或 `/藏宝图 开始 <1-3>` 重新进入。"
                )
            return False, (
                "⏳ 本次爬塔状态已变化（可能已推进或处理过奇遇）。\n"
                "💡 发送 `/藏宝图` 查看最新状态。"
            )
        await self.db.conn.execute(
            "UPDATE players SET hp = ? WHERE user_id = ?", (hp_after, player.user_id)
        )
        await self.db.conn.commit()
        player.hp = hp_after
        return True, (
            f"🗺️ 第 {floor_no} 层 · {rule['label']}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"气血 -{damage:,} → {hp_after:,}/{max_hp:,}"
            f"{'（护身灵符挡下陷阱）' if ward_used else ('（陷阱！）' if trap else '')}\n"
            f"灵石 +{gold_gain:,}（撤退/通关时结算）\n"
            f"战利品：{sum(1 for x in loot if x.get('kind') != 'gold')} 件\n"
            f"临时效果：{self._effect_line(effects)}\n"
            f"━━━━━━━━━━━━━━━\n"
            + self._event_prompt({
                "event": next_event, "effects": effects, "floor": floor_no,
            })
            + "\n⚠️ 必须先处理奇遇，才能继续深入下一层。"
        )

    async def settle_stale_runs(self, now: Optional[int] = None,
                                stale_seconds: int = STALE_RUN_SECONDS) -> int:
        """后台清理长时间没有推进的爬塔。

        旧实现直接把 run 置为 ended，玩家攒在 ``loot`` 里的灵石与战利品会被
        静默吞掉（既没发放也没提示）。这里改为按「撤退」结算：
        已经活着拿到的东西照常带走，只是不能再继续深入。
        """
        now = int(now or time.time())
        rows = await self._fetchall(
            "SELECT user_id FROM treasure_map_runs "
            "WHERE status = 'active' AND updated_at + ? < ?",
            (int(stale_seconds), now),
        )
        settled = 0
        for row in rows:
            user_id = str(row.get("user_id") or "")
            if not user_id:
                continue
            try:
                player = await self.db.get_player_by_id(user_id)
                run = await self.get_active_run(user_id)
                if not player or not run:
                    # 角色已不存在：直接结束，避免每 60 秒重复扫描同一条僵尸记录
                    await self.db.conn.execute(
                        "UPDATE treasure_map_runs SET status='ended', updated_at=? "
                        "WHERE user_id = ? AND status='active'",
                        (now, user_id),
                    )
                    await self.db.conn.commit()
                    continue
                ok, msg = await self._settle(
                    player, run, success=True, reason="墓府封闭（超时自动结算）"
                )
                if ok:
                    settled += 1
                    logger.info(f"【藏宝图】超时结算 {user_id}：{msg.splitlines()[2] if len(msg.splitlines()) > 2 else ''}")
            except Exception as exc:
                logger.warning(f"【藏宝图】超时结算失败 {user_id}: {exc}")
        return settled

    async def retreat(self, player: Player) -> Tuple[bool, str]:
        run = await self.get_active_run(player.user_id)
        if not run:
            return False, "❌ 你当前没有在爬塔。"
        return await self._settle(player, run, success=True, reason="主动撤退")

    async def _settle(self, player: Player, run: dict, success: bool,
                      reason: str, extra_gold: int = 0) -> Tuple[bool, str]:
        all_loot = list(run.get("loot") or [])
        loot = [item for item in all_loot if item.get("kind") != "gold"]
        loot_gold = sum(
            int(item.get("amount") or 0) for item in all_loot if item.get("kind") == "gold"
        )
        labels = []
        gold_bonus = 0
        if success:
            gold_bonus = int(extra_gold or 0) + loot_gold
            if reason == "通关主墓":
                cfg = MAP_TIERS.get(int(run.get("map_tier")), MAP_TIERS[1])
                gold_bonus += int(cfg["price"] * 1.8)
            elif reason == "已通关全图":
                cfg = MAP_TIERS.get(int(run.get("map_tier")), MAP_TIERS[1])
                gold_bonus += int(cfg["price"] * 1.5)
        # 「结束本次爬塔」与发奖必须同事务并带 status 守卫：
        # 否则中途异常会留下 active 的 run，玩家再撤退一次就能领双份。
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE treasure_map_runs SET status='ended', effects='[]', event='{}', "
                "updated_at=? WHERE user_id = ? AND status='active'",
                (int(time.time()), player.user_id),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 本次爬塔已经结算过了。"
            # 事务内重读灵石：后台暗标退款/盘口派彩不持玩家锁，
            # 直接用命令开始时的旧快照回写会把它们的入账覆盖掉。
            # 失败分支同样要重读——它也会走下面的全字段 update_player。
            fresh = await self._fetchone(
                "SELECT gold FROM players WHERE user_id = ?", (player.user_id,)
            )
            base_gold = int((fresh or {}).get("gold") or 0) if fresh else int(player.gold or 0)
            if success:
                if self.gamble_mgr:
                    for item in loot:
                        try:
                            if item.get("kind") == "jewel" and isinstance(item.get("jewel"), dict):
                                gem = self.gamble_mgr.jewel_manager.add_jewel(player, dict(item["jewel"]))
                                labels.append(f"💎【{gem.get('name')}·{gem.get('quality')}】")
                            else:
                                labels.append(await self.gamble_mgr._award_loot(player, item, item.get("quality") or ""))
                        except Exception as exc:
                            logger.warning(f"【藏宝图】发放战利品失败: {exc}")
                else:
                    for item in loot:
                        name = item.get("name")
                        if not name:
                            continue
                        ok, _msg = await self.storage_ring_mgr.store_item(
                            player, name, int(item.get("qty") or 1), silent=True,
                            external_transaction=True,
                        )
                        if ok:
                            labels.append(f"【{name}】")
                player.gold = base_gold + gold_bonus
            else:
                player.gold = base_gold
            await self.db.update_player(player, commit=False)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise
        lines = [
            f"🗺️ 藏宝图结算 · {reason}",
            "━━━━━━━━━━━━━━━",
            f"到达层数：第 {int(run.get('floor') or 0)} 层",
            f"灵石收获：+{gold_bonus:,}",
        ]
        if labels:
            lines.append("物品收获：" + "、".join(labels[:8]) + ("…" if len(labels) > 8 else ""))
        elif loot:
            lines.append("物品收获：无（储物戒空间不足或发放失败）")
        elif loot_gold:
            lines.append("物品收获：无（只带回了灵石）")
        else:
            lines.append("物品收获：空手而归")
        lines.extend([
            "━━━━━━━━━━━━━━━",
            f"💰 当前灵石：{player.gold:,}",
            "💡 可再次 `/藏宝图 开始 <1-3>` 重新探险。",
        ])
        return True, "\n".join(lines)