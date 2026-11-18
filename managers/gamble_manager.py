# managers/gamble_manager.py
import json
import random
import time
from typing import List, Tuple
from ..core.jewel_manager import JewelManager
from ..data import DataBase
from ..models import Player
from ..rank_rules import RANK_NAMES, normalize_rank

class GambleManager:
    """赌石与黑市管理器"""

    # ===== 切原石定价与掉落表 =====
    # 单块原石价：旧版 10000 灵石 / 块，与材料侧价值严重脱节（期望回收仅 24%），
    # 且 80% 的结果是 5 个凡品材料（约 400 灵石），体验极差。改为 5000 / 块。
    STONE_PRICE = 5000
    STONE_MAX_COUNT = 10
    # 装备总概率与「圣品占比」保持不变：玄品 3.92% / 圣品 0.98%
    STONE_GEAR_CHANCE = 0.049
    STONE_SAINT_GEAR_SHARE = 0.20
    STONE_EGG_CHANCE = 0.001
    # 材料掉落：矿材占 80%，其余 20% 为妖兽/灵植/灵液
    STONE_ORE_SHARE = 0.80
    # (品阶, 每块份数, 权重)：品阶越高份数越少、权重越低，形成梯度
    STONE_ORE_TABLE = (("凡品", 6, 42), ("黄品", 4, 32), ("玄品", 2, 23), ("圣品", 1, 3))
    STONE_MATERIAL_TABLE = (("凡品", 4, 42), ("黄品", 2, 32), ("玄品", 1, 23), ("圣品", 1, 3))
    STONE_RANKS = ("凡品", "黄品", "玄品", "圣品")
    STONE_FLAVOR = {
        "圣品装备": ("🌌 天地异象！你切出了传说中的", "！"),
        "玄品装备": ("🎉 金光四射！你切出了", "！"),
        "彩蛋": ("🌠 石中藏宝！你切出了", "！"),
        "圣品": ("🌟 矿脉交辉！你切出了", "！"),
        "玄品": ("✨ 切出了", "（灵矿）"),
        "黄品": ("切出了", "（寻常矿料）"),
        "凡品": ("切出了", "（碎石渣）"),
    }

    # ===== 赌石市场：五档价位，线索真假混杂 =====
    # 每档每小时只生成一颗，价格小幅浮动；隐藏结果预生成但不在列表中展示。
    # 风险设计：价格整体上抬，低档不再稳定回本；空石率、假线索与开窗成本同步提高，
    # 高档靠稀有法器与双珠宝拉开方差，绝大多数切割都是亏损。
    STONE_TIER_CONFIG = (
        {
            "name": "碎玉原石", "price": 3600,
            "desc": "石皮粗劣、灵气稀薄，只适合手痒时赌一刀。",
            "risk": "低·试水",
            "jewel_chance": 0.05, "gear_chance": 0.010,
            "egg_chance": 0.0002, "empty_chance": 0.08,
            "display_clues": 3, "true_clues": 1,
            "bonus_jewel_chance": 0.0,
            "saint_gear_share": 0.0,
            "quality_weights": {"一般": 920, "良好": 75, "极佳": 5, "大师": 0},
            "gear_quality_weights": {"一般": 85, "良好": 13, "极佳": 2, "大师": 0},
        },
        {
            "name": "寻常原石", "price": 12000,
            "desc": "市集常见货色，石皮真真假假，全凭眼力。",
            "risk": "中",
            "jewel_chance": 0.12, "gear_chance": 0.025,
            "egg_chance": 0.0005, "empty_chance": 0.12,
            "display_clues": 4, "true_clues": 1,
            "bonus_jewel_chance": 0.0,
            "saint_gear_share": 0.02,
            "quality_weights": {"一般": 830, "良好": 145, "极佳": 24, "大师": 1},
            "gear_quality_weights": {"一般": 70, "良好": 25, "极佳": 5, "大师": 0},
        },
        {
            "name": "灵纹原石", "price": 32000,
            "desc": "石皮有天然灵纹流转，但商贩最会在纹路上做手脚。",
            "risk": "高",
            "jewel_chance": 0.26, "gear_chance": 0.045,
            "egg_chance": 0.001, "empty_chance": 0.16,
            "display_clues": 5, "true_clues": 2,
            "bonus_jewel_chance": 0.02,
            "saint_gear_share": 0.06,
            "quality_weights": {"一般": 680, "良好": 240, "极佳": 72, "大师": 8},
            "gear_quality_weights": {"一般": 42, "良好": 40, "极佳": 15, "大师": 3},
        },
        {
            "name": "秘境原石", "price": 72000,
            "desc": "据传出自上古秘境矿脉，灵压沉厚，也最容易看走眼。",
            "risk": "极高",
            "jewel_chance": 0.44, "gear_chance": 0.075,
            "egg_chance": 0.002, "empty_chance": 0.20,
            "display_clues": 5, "true_clues": 2,
            "bonus_jewel_chance": 0.06,
            "saint_gear_share": 0.12,
            "quality_weights": {"一般": 520, "良好": 300, "极佳": 145, "大师": 35},
            "gear_quality_weights": {"一般": 18, "良好": 38, "极佳": 32, "大师": 12},
        },
        {
            "name": "太古原石", "price": 150000,
            "desc": "太古遗脉所结，石中或封着帝品珠玉，也可能只是一场空。",
            "risk": "九死一生",
            "jewel_chance": 0.62, "gear_chance": 0.110,
            "egg_chance": 0.004, "empty_chance": 0.25,
            "display_clues": 6, "true_clues": 3,
            "bonus_jewel_chance": 0.14,
            "saint_gear_share": 0.20,
            "quality_weights": {"一般": 380, "良好": 320, "极佳": 220, "大师": 80},
            "gear_quality_weights": {"一般": 6, "良好": 24, "极佳": 42, "大师": 28},
        },
    )
    STONE_TIER_MATERIAL_TABLES = {
        1: (("凡品", 6, 42), ("黄品", 4, 32), ("玄品", 2, 23), ("圣品", 1, 3)),
        2: (("凡品", 8, 34), ("黄品", 6, 34), ("玄品", 3, 25), ("圣品", 1, 7)),
        3: (("凡品", 6, 22), ("黄品", 10, 34), ("玄品", 6, 32), ("圣品", 2, 12)),
        4: (("黄品", 8, 36), ("玄品", 10, 40), ("圣品", 4, 24)),
        5: (("黄品", 6, 18), ("玄品", 12, 38), ("圣品", 8, 44)),
    }
    STONE_WINDOW_COST_RATE = 0.25
    STONE_WINDOW_MAX = 1
    STONE_STATE_KEY = "stone_market_state"
    BLACK_MARKET_JEWEL_CHANCE = 0.08

    def __init__(self, db: DataBase, config_manager, storage_ring_mgr):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_mgr = storage_ring_mgr
        self.jewel_manager = JewelManager(db, config_manager, storage_ring_mgr)
        self._stone_pools = None

    # ===== 掉落池：只收「能存进储物戒」的物品，避免切出后静默丢失 =====
    def _can_store(self, item_name: str) -> bool:
        if not self.storage_ring_mgr:
            return True
        try:
            can_store, _ = self.storage_ring_mgr.can_store_item(item_name)
            return bool(can_store)
        except Exception:
            return False

    def _stone_pool_cache(self):
        """按 (类别, 品阶) 分桶的材料池 + 按品阶分桶的法器池，首次使用时构建。"""
        if self._stone_pools is not None:
            return self._stone_pools
        items = getattr(self.config_manager, "items_data", {}) or {}
        catalog = getattr(self.config_manager, "material_catalog", {}) or {}
        materials, gears = {}, {}
        for name, info in catalog.items():
            item = items.get(name)
            if not isinstance(item, dict) or item.get("type") != "材料":
                continue
            if not self._can_store(name):
                continue
            info = info if isinstance(info, dict) else {}
            category = info.get("category") or ""
            rank = normalize_rank(info.get("tier") or item.get("rank"), "凡品")
            materials.setdefault((category, rank), []).append(name)
        for item in items.values():
            if not isinstance(item, dict) or item.get("type") != "法器":
                continue
            name = item.get("name")
            if not name or not self._can_store(name):
                continue
            rank = normalize_rank(item.get("rank"), "凡品")
            gears.setdefault(rank, []).append(name)
        self._stone_pools = (materials, gears)
        return self._stone_pools

    @classmethod
    def _rank_fallback(cls, rank: str):
        """该品阶无可用物品时，先往下退，再往上找。"""
        ranks = list(cls.STONE_RANKS)
        index = ranks.index(rank) if rank in ranks else 0
        return [ranks[index]] + ranks[:index][::-1] + ranks[index + 1:]

    def _roll_stone_material(self, table, categories, rng) -> dict:
        materials, _ = self._stone_pool_cache()
        ranks = [rank for rank, _, _ in table]
        weights = [weight for _, _, weight in table]
        rank = rng.choices(ranks, weights=weights, k=1)[0]
        qty = next(q for r, q, _ in table if r == rank)
        for candidate in self._rank_fallback(rank):
            pool = []
            for category in categories:
                pool.extend(materials.get((category, candidate), []))
            if pool:
                return {"kind": "material", "rank": candidate, "name": rng.choice(pool), "qty": qty}
        return {"kind": "dust", "rank": rank, "name": None, "qty": 0}

    def _roll_stone_gear(self, rng=None, saint_share: float = None) -> dict:
        rng = rng or random
        _, gears = self._stone_pool_cache()
        share = self.STONE_SAINT_GEAR_SHARE if saint_share is None else max(0.0, min(1.0, float(saint_share)))
        rank = "圣品" if rng.random() < share else "玄品"
        for candidate in self._rank_fallback(rank):
            pool = gears.get(candidate)
            if pool:
                return {"kind": "gear", "rank": candidate, "name": rng.choice(pool), "qty": 1}
        return {"kind": "dust", "rank": rank, "name": None, "qty": 0}

    def roll_stone_loot(self, rng=None) -> list:
        """开一块原石的掉落；返回列表便于以后扩展成「一块出多件」。"""
        rng = rng or random
        roll = rng.random()
        if roll < self.STONE_GEAR_CHANCE:
            return [self._roll_stone_gear(rng)]
        if roll < self.STONE_GEAR_CHANCE + self.STONE_EGG_CHANCE:
            return [{"kind": "egg", "rank": "", "name": "护府令", "qty": 3}]
        if rng.random() < self.STONE_ORE_SHARE:
            return [self._roll_stone_material(self.STONE_ORE_TABLE, ("矿材",), rng)]
        return [self._roll_stone_material(self.STONE_MATERIAL_TABLE, ("妖兽", "灵植", "灵液"), rng)]

    def _loot_label(self, loot: dict) -> str:
        name, qty = loot.get("name"), int(loot.get("qty") or 0)
        kind, rank = loot.get("kind"), loot.get("rank", "")
        if not name:
            return f"{'碎石渣一堆' if kind == 'dust' else '一团碎屑'}，一无所获（可掉落物品池为空）"
        if kind == "gear":
            prefix, suffix = self.STONE_FLAVOR["圣品装备" if rank == "圣品" else "玄品装备"]
        elif kind == "egg":
            prefix, suffix = self.STONE_FLAVOR["彩蛋"]
        else:
            prefix, suffix = self.STONE_FLAVOR.get(rank, ("切出了", ""))
        return f"{prefix}【{name}】" + (f"x{qty}" if qty > 1 else "") + suffix

    async def _award_loot(self, player: Player, loot: dict, quality: str = "") -> str:
        label = self._loot_label(loot)
        name = loot.get("name")
        if not name:
            return label
        success, reason = await self.storage_ring_mgr.store_item(
            player, name, int(loot.get("qty") or 1), silent=True,
            external_transaction=True, quality=quality or loot.get("quality") or "",
        )
        if success:
            return label
        return f"{label}\n   ⚠️ 未能存入储物戒：{reason}"

    # ===== 分档原石：候选、线索、开窗与切割 =====
    @staticmethod
    def _stone_hour_key() -> int:
        return int(time.time()) // 3600

    def _stone_state_key(self, user_id: str) -> str:
        return f"{self.STONE_STATE_KEY}_{user_id}"

    async def _get_stone_state(self, user_id: str) -> dict:
        """读取本小时赌石状态；跨小时自动清空，不在系统配置里无限堆积。"""
        hour = self._stone_hour_key()
        try:
            raw = await self.db.ext.get_system_config(self._stone_state_key(user_id))
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        if not isinstance(data, dict) or int(data.get("hour", -1) or -1) != hour:
            data = {"hour": hour, "bought": [], "revealed": {}}
        data["bought"] = [int(x) for x in (data.get("bought") or []) if str(x).lstrip("-").isdigit()]
        if not isinstance(data.get("revealed"), dict):
            data["revealed"] = {}
        return data

    async def _save_stone_state(self, user_id: str, state: dict) -> None:
        await self.db.ext.set_system_config(
            self._stone_state_key(user_id),
            json.dumps(state, ensure_ascii=False),
        )

    def _stone_clue_pools(self, hidden: dict, tier_index: int, rng) -> Tuple[List[str], List[str]]:
        """按隐藏结果生成真假线索池。

        设计原则：真线索只给「倾向」，不直接报结果；假线索刻意模仿真线索的措辞，
        让玩家无法靠单个关键词判断，只能结合多条线索与价位自行下注。
        """
        kind = str(hidden.get("kind") or "dust")
        gear_quality = str((hidden.get("loot") or [{}])[0].get("quality") or "一般")
        fake_quality = rng.choice(("一般", "良好", "极佳", "大师"))
        fake_rank = rng.choice(("凡品", "黄品", "玄品", "圣品"))
        if kind == "jewel":
            gem = hidden.get("jewel") or {}
            quality = str(gem.get("quality") or "一般")
            true = [
                "石皮薄处透出一线温润光晕，指尖能触到极细的玉脉。",
                "石体比同体积的原石轻上三分，叩之声脆，余音发甜。",
                "裂缝深处凝着一层不散的脂光，像是被什么温养过。",
                "石心有一处细小空腔，灵识探入时被柔和的阻力弹回。",
                f"断口处的灵韵偏「{quality}」品，但石皮太厚，看不真切。",
            ]
        elif kind == "gear":
            true = [
                "敲击时石心传来一声极短的金铁回响，随后又归于沉闷。",
                "石皮上有一道笔直的暗纹，轮廓像某种器物的边。",
                "灵识探到一处棱角分明的硬物，不像天然石核。",
                f"石心透出的锋锐灵压约为「{gear_quality}」品，若有若无。",
                "这块石料沉得反常，像是裹着一层未化开的精铁。",
            ]
        elif kind == "egg":
            true = [
                "石心有一点温热，像被什么符箓压着。",
                "缝隙里偶尔闪过一缕极淡的金光，转瞬即逝。",
                "夜里贴近耳边，能听到极轻的震颤声。",
                "石皮内侧有一圈细密的符纹，不像天然生成。",
            ]
        elif kind == "material":
            rank = str((hidden.get("loot") or [{}])[0].get("rank") or "凡品")
            true = [
                f"矿脉走线整齐，品阶大致在「{rank}」上下。",
                "石体沉重，断口泛着细碎而稳定的矿光。",
                "灵气不浓，却很绵长，像一整块未经提炼的矿料。",
                "石皮上有一层自然沉积的矿壳，不是人为做旧。",
                "敲击声厚实，回音里带着砂砾般的粗粝感。",
            ]
        else:
            true = [
                "敲击声发闷，回音散得很快，石心没有明显空腔。",
                "石皮裂纹里的灵气已经散尽，断口只剩灰白石质。",
                "灵识探进去几乎没有阻碍，里面是空荡荡的一片。",
                "石体重量正常，却沉得没有一丝灵性。",
            ]

        false = [
            "石皮薄处透出一线温润光晕，也可能只是灵砂反光。",
            "叩之声脆，但脆声未必来自珠玉，也可能是中空裂层。",
            "石心传来极短的金铁回响，兴许只是伴生矿。",
            "断口泛着细碎矿光，看着像有货，也可能只是石屑。",
            "石体轻得反常，商贩说是老坑料，谁知道呢。",
            "有一道笔直的暗纹，像是人为封痕，也可能只是石脉。",
            "缝隙里闪过一缕金光，转瞬即逝，再看就没了。",
            "石心有一点温热，摸着却像刚被人捂过。",
            "矿脉走线整齐，可惜表层灵气驳杂，做不得准。",
            "敲击声发闷，但老坑石常有这种闷响，未必是空石。",
            "商贩拍着胸口保证这是秘境老料，眼神却有些飘。",
            "石皮上的灵纹太规整，像是后天描上去的。",
            f"灵识里晃过一丝「{fake_quality}」品灵韵，细探又散了。",
            f"断口看着像「{fake_rank}」矿料，表层却被人抹过灵砂。",
            "石心似乎有一处空腔，敲起来却又不像中空。",
            "这块石头被反复转手，灵气驳杂，什么可能都有。",
        ]
        # 高档原石也会故意给出“像空石”“像废料”的真线索，不能只靠价位判断。
        if tier_index >= 4:
            if kind in ("jewel", "gear", "egg"):
                true.append("石皮最不起眼的那道暗纹下，反而藏着最浓的灵机。")
            else:
                true.append("石皮上的灵纹亮得刺眼，反而像是诱人下刀的幌子。")
        return true, false

    def _roll_tier_jewel(self, cfg: dict, rng) -> dict:
        category = rng.choice(("attack", "defense"))
        pool = [
            t for t in self.jewel_manager.templates()
            if str(t.get("category") or "attack") == category
        ] or self.jewel_manager.templates()
        return self.jewel_manager.roll_jewel(
            template=rng.choice(pool),
            quality_weights=cfg["quality_weights"],
            rng=rng,
        )

    def _roll_tier_loot(self, tier_index: int, rng) -> dict:
        cfg = self.STONE_TIER_CONFIG[tier_index - 1]
        roll = rng.random()
        jewel_end = float(cfg["jewel_chance"])
        gear_end = jewel_end + float(cfg["gear_chance"])
        egg_end = gear_end + float(cfg["egg_chance"])
        empty_end = egg_end + float(cfg["empty_chance"])
        if roll < jewel_end:
            gem = self._roll_tier_jewel(cfg, rng)
            loot = [{
                "kind": "jewel", "name": gem["name"], "rank": gem["rank"],
                "qty": 1, "jewel": gem,
            }]
            # 高档原石可能一刀开出双珠，这是高价石唯一值得赌的念想。
            if rng.random() < float(cfg.get("bonus_jewel_chance", 0) or 0):
                extra = self._roll_tier_jewel(cfg, rng)
                loot.append({
                    "kind": "jewel", "name": extra["name"], "rank": extra["rank"],
                    "qty": 1, "jewel": extra,
                })
            return {"kind": "jewel", "jewel": gem, "loot": loot}
        if roll < gear_end:
            gear = self._roll_stone_gear(rng, cfg.get("saint_gear_share"))
            qualities = list(cfg["gear_quality_weights"])
            weights = [max(0, int(cfg["gear_quality_weights"][q])) for q in qualities]
            gear["quality"] = rng.choices(qualities, weights=weights, k=1)[0]
            return {"kind": "gear", "loot": [gear]}
        if roll < egg_end:
            return {"kind": "egg", "loot": [{"kind": "egg", "rank": "", "name": "护府令", "qty": 3}]}
        if roll < empty_end:
            return {"kind": "dust", "loot": [{"kind": "dust", "rank": "", "name": None, "qty": 0}]}
        table = self.STONE_TIER_MATERIAL_TABLES[tier_index]
        categories = ("矿材",) if rng.random() < self.STONE_ORE_SHARE else ("妖兽", "灵植", "灵液")
        loot = [self._roll_stone_material(table, categories, rng)]
        # 高档石出材料时多给一份，避免高价位切开只得到一堆碎矿。
        if tier_index >= 3 and rng.random() < 0.35:
            loot.append(self._roll_stone_material(table, categories, rng))
        return {"kind": "material", "loot": loot}

    def _get_stone_candidates(self, user_id: str) -> List[dict]:
        """每小时每名玩家固定生成五档候选，结果与线索都由种子稳定复现。"""
        hour = self._stone_hour_key()
        candidates = []
        for index, cfg in enumerate(self.STONE_TIER_CONFIG, 1):
            market_rng = random.Random(f"stone-market:{hour}:{user_id}:{index}")
            price = max(1, int(round(cfg["price"] * (1 + market_rng.uniform(-0.08, 0.08)))))
            hidden_rng = random.Random(f"stone-hidden:{hour}:{user_id}:{index}")
            hidden = self._roll_tier_loot(index, hidden_rng)
            true_clues, false_clues = self._stone_clue_pools(hidden, index, market_rng)
            true_count = max(0, min(int(cfg["true_clues"]), len(true_clues)))
            false_count = max(0, int(cfg["display_clues"]) - true_count)
            shown_true = market_rng.sample(true_clues, true_count) if true_count else []
            shown_false = market_rng.sample(false_clues, min(false_count, len(false_clues)))
            clues = [{"text": text, "true": True} for text in shown_true]
            clues += [{"text": text, "true": False} for text in shown_false]
            market_rng.shuffle(clues)
            extra_true = [text for text in true_clues if text not in shown_true]
            candidates.append({
                "index": index,
                "name": cfg["name"],
                "price": price,
                "desc": cfg["desc"],
                "risk": cfg.get("risk", "未知"),
                "clues": clues,
                "extra_true": extra_true,
                "hidden": hidden,
            })
        return candidates

    async def get_stone_market(self, user_id: str) -> Tuple[bool, str]:
        """`/黑市 原石`：展示本小时五档原石与真假线索。"""
        candidates = self._get_stone_candidates(user_id)
        state = await self._get_stone_state(user_id)
        bought = set(state.get("bought") or [])
        revealed_map = state.get("revealed") or {}
        lines = ["🕶️ 黑市 · 赌石原石（每小时刷新）", "━━━━━━━━━━━━━━━"]
        for item in candidates:
            index = item["index"]
            status = " ❌已售出" if index in bought else ""
            lines.append(
                f"[{index}] {item['name']} - {item['price']:,} 灵石"
                f"（风险：{item['risk']}）{status}"
            )
            lines.append(f"    {item['desc']}")
            for clue in item["clues"]:
                lines.append(f"    🔍 {clue['text']}")
            for text in revealed_map.get(str(index)) or []:
                lines.append(f"    🔎 已开窗真线索：{text}")
        remaining = 3600 - (int(time.time()) % 3600)
        lines.append("━━━━━━━━━━━━━━━")
        lines.append(f"⏳ 下次刷新：{remaining // 60} 分 {remaining % 60} 秒后")
        lines.append("💡 `/黑市 切原石 <编号>` 切割｜`/黑市 开窗 <编号>` 付费揭示一条真线索")
        lines.append("📈 档位越高，稀有珠宝概率越高，但空石率也越高。")
        lines.append("⚠️ 赌石十切九空：线索真假混杂，每块原石只能开窗一次，切垮自负。")
        return True, "\n".join(lines)

    async def open_stone_window(self, player: Player, index: int) -> Tuple[bool, str]:
        """付费开窗：额外揭示一条隐藏的真线索。"""
        candidates = self._get_stone_candidates(player.user_id)
        if index < 1 or index > len(candidates):
            return False, "❌ 无效的原石编号，请使用 `/黑市 原石` 查看。"
        state = await self._get_stone_state(player.user_id)
        if index in set(state.get("bought") or []):
            return False, "❌ 这块原石已经被你切开了。"
        candidate = candidates[index - 1]
        revealed_map = state.setdefault("revealed", {})
        shown = list(revealed_map.get(str(index)) or [])
        if len(shown) >= self.STONE_WINDOW_MAX:
            return False, "❌ 这块原石已经开过窗了，赌石没有第二次机会。"
        available = [text for text in candidate["extra_true"] if text not in shown]
        if not available:
            return False, "❌ 这块原石石皮太死，老师傅也找不出更多真线索。"
        cost = max(100, int(candidate["price"] * self.STONE_WINDOW_COST_RATE))
        if player.gold < cost:
            return False, f"❌ 开窗需要 {cost:,} 灵石，你当前只有 {player.gold:,}。"

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                return False, "❌ 角色数据异常，请重新踏入仙途。"
            player.gold = fresh.gold
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (cost, player.user_id, cost),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, f"❌ 开窗需要 {cost:,} 灵石，你当前只有 {fresh.gold:,}。"
            player.gold -= cost
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        clue = available[0]
        shown.append(clue)
        revealed_map[str(index)] = shown
        await self._save_stone_state(player.user_id, state)
        return True, (
            f"🔎 你花费 {cost:,} 灵石，请老师傅在【{candidate['name']}】上开了一扇小窗。\n"
            f"真线索：{clue}\n"
            f"⚠️ 开窗只有一次，剩下的只能靠你自己判断。\n"
            f"💰 剩余灵石：{player.gold:,}"
        )

    async def _award_tier_loot(self, player: Player, hidden: dict) -> str:
        labels = []
        for loot in hidden.get("loot") or []:
            if loot.get("kind") == "jewel" and isinstance(loot.get("jewel"), dict):
                gem = self.jewel_manager.add_jewel(player, loot["jewel"])
                labels.append(
                    f"💎 切出了【{gem.get('name')}·{gem.get('quality')}】！\n"
                    f"   词条：{self.jewel_manager.describe_jewel(gem)}"
                )
            else:
                labels.append(await self._award_loot(player, loot, loot.get("quality") or ""))
        if not labels:
            return "💀 石皮崩开，里面只有一团死气沉沉的废料——这刀切垮了。"
        if hidden.get("kind") == "dust":
            return "💀 石皮崩开，里面只有一团死气沉沉的废料——这刀切垮了。"
        return "\n".join(labels)

    async def cut_tier_stone(self, player: Player, index: int) -> Tuple[bool, str]:
        """按档位切割本小时的一颗原石（每个档位每小时限切一次）。"""
        candidates = self._get_stone_candidates(player.user_id)
        if index < 1 or index > len(candidates):
            return False, "❌ 无效的原石编号，请使用 `/黑市 原石` 查看。"
        state = await self._get_stone_state(player.user_id)
        if index in set(state.get("bought") or []):
            return False, "❌ 这块原石本小时已经被你切开了，请等待下次刷新。"
        candidate = candidates[index - 1]
        price = int(candidate["price"])
        if player.gold < price:
            return False, f"❌ 灵石不足！切割【{candidate['name']}】需要 {price:,} 灵石。"

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                return False, "❌ 角色数据异常，请重新踏入仙途。"
            player.gold = fresh.gold
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (price, player.user_id, price),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, (
                    f"❌ 灵石不足！切割【{candidate['name']}】需要 {price:,} 灵石，"
                    f"你当前只有 {fresh.gold:,}。"
                )
            player.gold -= price
            result = await self._award_tier_loot(player, candidate["hidden"])
            await self.db.update_player(player, commit=False)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        state.setdefault("bought", []).append(index)
        await self._save_stone_state(player.user_id, state)
        return True, (
            f"⛏️ 你花费 {price:,} 灵石，切开了【{candidate['name']}】"
            f"（风险：{candidate['risk']}）：\n"
            f"━━━━━━━━━━━━━━━\n{result}\n━━━━━━━━━━━━━━━\n"
            f"💰 剩余灵石：{player.gold:,}"
        )

    async def cut_stone(self, player: Player, count: int) -> Tuple[bool, str]:
        """切原石"""
        if count <= 0 or count > self.STONE_MAX_COUNT:
            return False, f"❌ 一次最多切 {self.STONE_MAX_COUNT} 块原石。"

        total_cost = self.STONE_PRICE * count
        if player.gold < total_cost:
            return False, f"❌ 灵石不足！切 {count} 块原石需要 {total_cost:,} 灵石。"

        results = []
        # 扣费与所有掉落放入同一事务，避免中途异常时灵石被扣而物品丢失
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 事务内重读最新灵石并同步回调用方对象：后续 update_player 全字段回写
            # 不会再拿旧快照覆盖并发入账
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                return False, "❌ 角色数据异常，请重新踏入仙途。"
            player.gold = fresh.gold

            # 条件原子扣款：余额不足时 rowcount=0，绝不透支
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (total_cost, player.user_id, total_cost),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, (
                    f"❌ 灵石不足！切 {count} 块原石需要 {total_cost:,} 灵石，"
                    f"你当前只有 {fresh.gold:,}。"
                )
            player.gold -= total_cost

            for _ in range(count):
                for loot in self.roll_stone_loot():
                    results.append(await self._award_loot(player, loot))

            await self.db.update_player(player, commit=False)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        msg = [
            f"⛏️ 你花费了 {total_cost:,} 灵石，切开了 {count} 块原石（{self.STONE_PRICE:,}/块）：",
            "━━━━━━━━━━━━━━━",
            "\n".join(results),
            "━━━━━━━━━━━━━━━",
            f"💰 剩余灵石：{player.gold:,}"
        ]
        return True, "\n".join(msg)

    BLACK_MARKET_FAKES = [
        {"name": "神秘的葫芦", "price": 5000, "desc": "不知道装了什么。", "fake": True},
        {"name": "劣质回气散", "price": 200, "desc": "闻起来有点刺鼻。", "fake": True},
    ]
    BLACK_MARKET_RANK_WEIGHTS = {
        "黄品": 6, "玄品": 5, "灵品": 4, "地品": 3, "天品": 2, "皇品": 2,
        "圣品": 1, "帝品": 1, "道品": 1, "仙品": 1, "混元先天": 1,
    }
    # 黑市装备品质：整体高于普通货架，大师/极佳更常见
    BLACK_MARKET_QUALITY_WEIGHTS = {"一般": 20, "良好": 35, "极佳": 30, "大师": 15}

    @staticmethod
    def _rank_index(rank: str) -> int:
        try:
            return RANK_NAMES.index(normalize_rank(rank))
        except ValueError:
            return 0

    def _black_market_pick_weight(self, data: dict) -> float:
        """黑市抽取权重：品阶越高、原始权重越低（越稀有）越容易入选。"""
        try:
            base = int(
                data.get("black_market_weight")
                or data.get("drop_weight")
                or data.get("shop_weight")
                or self.BLACK_MARKET_RANK_WEIGHTS.get(data.get("rank", ""), 1)
            )
        except (TypeError, ValueError):
            base = 1
        base = max(1, base)
        index = self._rank_index(data.get("rank", ""))
        return max(1.0, ((1.0 + index) ** 1.5) * (base ** -0.5) * 100)

    def _black_market_type_label(self, data: dict) -> str:
        item_type = data.get("type", "")
        subtype = str(data.get("subtype") or "")
        if item_type == "法器":
            return data.get("subtype") or "法器"
        # 三类丹药共用同一套展示名：修为丹/功能丹靠独立配置的字段识别，
        # 避免装备的 subtype（如“手套”“戒指”）被误判成功能丹。
        if item_type == "exp_pill" or subtype == "exp" or "exp_gain" in data:
            return "修为丹"
        if item_type == "utility_pill" or (
            not item_type and "effect_type" in data
        ):
            return "功能丹"
        if item_type in ("pill", "丹药") or subtype == "breakthrough":
            return "丹药"
        return {
            "丹药": "丹药",
            "功法": "功法",
            "technique": "功法",
            "main_technique": "心法",
            "符箓": "符箓",
            "材料": "材料",
            "杂物": "杂物",
            "weapon": "武器",
            "armor": "防具",
            "head": "头饰",
            "body": "甲胄",
            "boots": "靴子",
            "gloves": "手套",
            "ring": "戒指",
            "amulet": "护符",
        }.get(item_type, item_type or "物品")

    def _black_market_effect(self, data: dict, quality: str = "") -> str:
        from ..core.shop_manager import ShopManager
        return "；".join(ShopManager({}, self.config_manager).get_trade_details({
            "name": data.get("name", "未知物品"), "data": data,
            "price": data.get("price", 0), "rank": data.get("rank", "凡品"),
            "quality": quality,
        }))

    def _get_black_market_items(self):
        """按小时生成黑市商品；同一小时展示与购买一致。"""
        now = int(time.time())
        hour_key = now // 3600
        rng = random.Random(hour_key)

        real_pool = {}
        for source in (
            self.config_manager.items_data,
            self.config_manager.weapons_data,
            getattr(self.config_manager, "techniques_data", {}) or {},
            # 突破丹/修为丹/功能丹独立配置，也必须进入黑市真实商品池，
            # 否则新增丹药只有在丹阁/百宝阁才能买到。
            getattr(self.config_manager, "pills_data", {}) or {},
            getattr(self.config_manager, "exp_pills_data", {}) or {},
            getattr(self.config_manager, "utility_pills_data", {}) or {},
        ):
            for name, data in source.items():
                if not isinstance(data, dict):
                    continue
                if int(data.get("price", 0) or 0) <= 0:
                    continue
                if data.get("rank") not in self.BLACK_MARKET_RANK_WEIGHTS:
                    continue
                if data.get("type") == "储物戒":
                    continue
                # 显式 shop_weight=0 的物品只通过掉落/活动获取（如周本专属天外陨核、
                # 帝品材料、炼丹专属增益丹），不进入黑市；如需黑市专供请配置
                # black_market_weight / drop_weight 覆盖。
                if int(data.get("shop_weight", 1) or 0) == 0 and not (
                    data.get("black_market_weight") or data.get("drop_weight")
                ):
                    continue
                real_name = data.get("name") or name
                real_pool[real_name] = data

        candidates = list(real_pool)
        selected = []
        while candidates and len(selected) < 3:
            weights = [self._black_market_pick_weight(real_pool[name]) for name in candidates]
            pick = rng.choices(candidates, weights=weights, k=1)[0]
            selected.append(pick)
            candidates.remove(pick)

        items = []
        for name in selected:
            data = real_pool[name]
            base_price = max(1, int(data.get("price", 0) or 0))
            # 稀有货溢价：品阶越高，溢价越重
            premium = 1.8 + self._rank_index(data.get("rank", "")) * 0.18 + rng.uniform(0.0, 0.7)
            price = max(1, int(base_price * premium))
            items.append({
                "name": name,
                "price": price,
                "fake": False,
                "data": data,
                "rank": data.get("rank", ""),
                "type": self._black_market_type_label(data),
                "desc": self._black_market_effect(data),
            })
        # 黑市以稀有真品为主，小概率混入一件假货（真假自负）
        if items and rng.random() < 0.2:
            items[rng.randrange(len(items))] = dict(rng.choice(self.BLACK_MARKET_FAKES))

        from ..core.item_rules import ItemRules
        quality_names = list(self.BLACK_MARKET_QUALITY_WEIGHTS)
        quality_weights = [self.BLACK_MARKET_QUALITY_WEIGHTS[q] for q in quality_names]
        for item in items:
            data = item.get("data")
            if item.get("fake") or not isinstance(data, dict):
                continue
            if ItemRules.normalize_equipment_type(data) in ItemRules.GEAR_TYPES:
                item["quality"] = rng.choices(quality_names, weights=quality_weights, k=1)[0]
                item["desc"] = self._black_market_effect(data, item["quality"])

        # 黑市小概率混入一件珠宝：用替补而不是加第四件，保持「每小时三件」的节奏。
        if items and rng.random() < self.BLACK_MARKET_JEWEL_CHANCE:
            gem = self.jewel_manager.roll_jewel(
                quality_weights={"一般": 40, "良好": 40, "极佳": 17, "大师": 3},
                rng=rng,
            )
            base_price = max(1, int(gem.get("price") or 1))
            premium = 1.9 + self._rank_index(gem.get("rank", "")) * 0.18 + rng.uniform(0.0, 0.5)
            price = max(1, int(base_price * premium))
            affix_text = self.jewel_manager.describe_jewel(gem)
            idx = rng.randrange(len(items))
            items[idx] = {
                "name": gem["name"],
                "price": price,
                "fake": False,
                "rank": gem.get("rank", "黄品"),
                "type": "珠宝",
                "quality": gem.get("quality", "一般"),
                "desc": f"珠宝（{gem.get('category') == 'attack' and '攻击' or '防御'}）：{affix_text}",
                "jewel": gem,
                "data": {
                    "name": gem["name"], "type": "jewel", "price": base_price,
                    "rank": gem.get("rank", "黄品"),
                    "description": gem.get("description", ""),
                },
            }

        next_refresh = (hour_key + 1) * 3600
        return items, next_refresh

    LEGACY_GHOST_ITEMS = {
        "圣品·混元道果": 100000,
        "玄品·洗髓丹": 1000,
        "圣品·诛仙剑": 50000,
    }

    async def cleanup_legacy_black_market_items(self) -> int:
        """清理旧黑市产生的幽灵物品并按旧价退款；只执行一次。"""
        flag_key = "black_market_ghost_cleanup_v1"
        if await self.db.ext.get_system_config(flag_key) == "1":
            return 0

        async with self.db.conn.execute(
            "SELECT user_id, storage_ring_items FROM players"
        ) as cursor:
            rows = await cursor.fetchall()

        total_refund = 0
        for row in rows:
            user_id, raw = row[0], row[1]
            try:
                items = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                continue
            if not isinstance(items, dict):
                continue

            refund = 0
            for name, price in self.LEGACY_GHOST_ITEMS.items():
                count = int(items.get(name, 0) or 0)
                if count > 0:
                    refund += count * price
                    del items[name]
            if refund > 0:
                await self.db.conn.execute(
                    "UPDATE players SET gold = gold + ?, storage_ring_items = ? "
                    "WHERE user_id = ?",
                    (refund, json.dumps(items, ensure_ascii=False), user_id),
                )
                total_refund += refund

        await self.db.ext.set_system_config(flag_key, "1")
        await self.db.conn.commit()
        return total_refund

    def build_black_market_entries(self) -> list:
        """黑市网格条目（网页端用）。与 ``get_black_market`` 读同一份数据。

        假货不透露真假：格子只写「风险品」，跟文本列表一个口径。
        """
        items, _ = self._get_black_market_items()
        entries = []
        for index, item in enumerate(items, 1):
            name = str(item.get("name") or "")
            if not name:
                continue
            fake = bool(item.get("fake"))
            desc = str(item.get("desc") or "")
            # desc 形如「品级: 帝品；五行: 木；描述: …」——品阶在格子里已经用色条表达，
            # 详情页再重复一遍只是噪音
            if desc.startswith("品级:") and "；" in desc:
                desc = desc.split("；", 1)[1]
            entries.append({
                "id": index,
                "name": name,
                "rank": str(item.get("rank") or "凡品"),
                "kind": "风险品" if fake else str(item.get("type") or "物品"),
                "price": int(item.get("price") or 0),
                "currency": "灵石",
                "effect": desc,
                "effectLabel": "效果",
                "state": "ok",
                "cmd": f"/黑市购买 {index}",
                "source": "blackmarket",
                "action": "购买",
                "notes": ["真假自负，每人每小时每件限购 1 次"],
            })
        return entries

    def get_black_market(self) -> Tuple[bool, str]:
        """获取当前小时的黑市商品。"""
        items, next_refresh = self._get_black_market_items()

        msg = ["🕶️ 黑市（每小时刷新）", "━━━━━━━━━━━━━━━"]
        for i, item in enumerate(items, 1):
            if item.get("fake"):
                msg.append(f"[{i}] {item['name']} - {item['price']:,} 灵石")
                msg.append(f"    {item.get('desc', '风险品')}")
            else:
                msg.append(
                    f"[{i}] [{item.get('rank', '凡品')}] {item['name']}"
                    f"（{item.get('type', '物品')}） - {item['price']:,} 灵石"
                )
                if item.get("desc"):
                    msg.append(f"    效果：{item['desc']}")

        remaining = max(0, next_refresh - int(time.time()))
        msg.append("━━━━━━━━━━━━━━━")
        msg.append(f"⏳ 下次刷新：{remaining // 60} 分 {remaining % 60} 秒后")
        msg.append("💡 使用 `/黑市购买 <编号>`，真假自负！")
        return True, "\n".join(msg)

    async def _black_market_purchase_key(self, user_id: str) -> str:
        return f"black_market_bought_{int(time.time()) // 3600}_{user_id}"

    async def _black_market_bought(self, user_id: str) -> set:
        try:
            raw = await self.db.ext.get_system_config(
                await self._black_market_purchase_key(user_id)
            )
            data = json.loads(raw) if raw else []
            return {int(x) for x in data} if isinstance(data, list) else set()
        except Exception:
            return set()

    async def _mark_black_market_bought(self, user_id: str, index: int) -> None:
        bought = await self._black_market_bought(user_id)
        bought.add(int(index))
        await self.db.ext.set_system_config(
            await self._black_market_purchase_key(user_id),
            json.dumps(sorted(bought)),
        )

    async def buy_black_market(self, player: Player, index: int) -> Tuple[bool, str]:
        """购买当前小时的黑市商品（每人每小时每件限购 1 次）。"""
        items, _ = self._get_black_market_items()

        if index < 1 or index > len(items):
            return False, "❌ 无效的商品编号。"

        if index in await self._black_market_bought(player.user_id):
            return False, "❌ 本小时你已经买过这件商品了，请等待下次刷新。"

        target = items[index - 1]
        price = int(target.get("price") or 0)
        if player.gold < price:
            return False, f"❌ 灵石不足！需要 {price:,} 灵石。"

        if target.get("fake"):
            # 假货：扣灵石与扣气血放入同一事务，条件原子扣款防止并发透支
            await self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.conn.execute(
                    "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                    (price, player.user_id, price),
                )
                if not cursor.rowcount:
                    await self.db.conn.rollback()
                    fresh = await self.db.get_player_by_id(player.user_id)
                    have = fresh.gold if fresh else player.gold
                    return False, (
                        f"❌ 灵石不足！需要 {price:,} 灵石，"
                        f"你当前只有 {have:,}。"
                    )
                # 事务内重读最新气血；死亡状态（hp<=0）保持原值不再扣血
                fresh = await self.db.get_player_by_id(player.user_id)
                cur_hp = fresh.hp if fresh else player.hp
                penalty_hp = int(cur_hp * 0.2) if cur_hp > 0 else 0
                new_hp = max(1, cur_hp - penalty_hp) if cur_hp > 0 else cur_hp
                if cur_hp > 0 and new_hp != cur_hp:
                    await self.db.conn.execute(
                        "UPDATE players SET hp = ? WHERE user_id = ?",
                        (new_hp, player.user_id),
                    )
                await self.db.conn.commit()
                player.gold = fresh.gold if fresh else player.gold - price
                player.hp = new_hp
            except Exception:
                await self.db.conn.rollback()
                raise
            await self._mark_black_market_bought(player.user_id, index)
            return True, (
                f"🤢 你买下了【{target['name']}】，结果发现是个假货！\n"
                f"你被毒气所伤，损失了 {penalty_hp} 点气血。"
            )

        data = target.get("data")
        if not data:
            return False, "❌ 商品信息异常，本次交易已取消。"

        # 丹药进入丹药背包，其余进入储物戒
        is_pill = False
        try:
            is_pill = self.config_manager.is_pill(target["name"]) or data.get("type") == "丹药"
        except Exception:
            is_pill = data.get("type") == "丹药"

        # 先条件原子扣灵石（防并发透支），发货失败再原路退回，绝不"扣钱又丢货"
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (price, player.user_id, price),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                fresh = await self.db.get_player_by_id(player.user_id)
                have = fresh.gold if fresh else player.gold
                return False, (
                    f"❌ 灵石不足！需要 {price:,} 灵石，"
                    f"你当前只有 {have:,}。"
                )

            if target.get("jewel"):
                # 珠宝进入独立珠囊，不占储物戒格数；与扣款同事务落库。
                fresh = await self.db.get_player_by_id(player.user_id)
                if not fresh:
                    await self.db.conn.rollback()
                    return False, "❌ 角色数据异常，本次交易已取消。"
                gem = self.jewel_manager.add_jewel(fresh, dict(target["jewel"]))
                await self.db.update_player(fresh, commit=False)
                await self.db.conn.commit()
                player.gold = fresh.gold
                player.set_jewel_inventory(fresh.get_jewel_inventory())
                await self._mark_black_market_bought(player.user_id, index)
                return True, (
                    f"✅ 交易成功！你获得了珠宝【{gem.get('name')}·{gem.get('quality')}】，"
                    f"已放入珠囊（#{gem.get('id')}）。\n"
                    f"词条：{self.jewel_manager.describe_jewel(gem)}\n"
                    f"消耗灵石：{price:,}"
                )

            if is_pill:
                # 丹药在事务内基于最新数据直接入背包，避免旧快照覆盖并发入账
                fresh = await self.db.get_player_by_id(player.user_id)
                if fresh:
                    inventory = fresh.get_pills_inventory()
                    pill_key = f"{target['name']}|一般"
                    inventory[pill_key] = inventory.get(pill_key, 0) + 1
                    fresh.set_pills_inventory(inventory)
                    await self.db.update_player(fresh, commit=False)
                await self.db.conn.commit()
                player.gold = fresh.gold if fresh else player.gold - price
                if fresh:
                    player.set_pills_inventory(fresh.get_pills_inventory())
                await self._mark_black_market_bought(player.user_id, index)
                return True, (
                    f"✅ 交易成功！你获得了【{target['name']}】x1，已放入丹药背包。\n"
                    f"消耗灵石：{price:,}"
                )
            await self.db.conn.commit()
            player.gold -= price
        except Exception:
            await self.db.conn.rollback()
            raise

        # 非丹药走储物戒入库；store_item 自带事务与失败回滚，异常也不吞灵石
        try:
            ok, store_msg = await self.storage_ring_mgr.store_item(
                player, target["name"], 1, silent=True,
                quality=target.get("quality") or "",
            )
        except Exception as e:
            ok, store_msg = False, f"入库异常：{e}"
        if not ok:
            # 储物戒满/类型不可存：退回灵石，绝不发幽灵物品
            await self.db.add_player_gold(player.user_id, price)
            player.gold += price
            return False, (
                f"❌ 购买【{target['name']}】失败：{store_msg}\n"
                f"灵石已退回：{price:,}"
            )

        await self._mark_black_market_bought(player.user_id, index)
        return True, (
            f"✅ 交易成功！你获得了【{target['name']}】x1，已存入储物戒。\n"
            f"消耗灵石：{price:,}"
        )
    # ===== 黑市密封锦囊（v7.10.3）=====
    # 与赌石同源：每小时刷新、真假线索、付费验囊、一次性拆开。
    # 风险设计：档位越高，稀有物概率越高，但空囊/假货/诅咒概率也同步提高。
    POUCH_TIER_CONFIG = (
        {
            "name": "灰布锦囊", "price": 1800, "stone_tier": 1,
            "risk": "低·试水", "fake_chance": 0.34, "curse_chance": 0.05,
            "desc": "灰布缝得潦草，商贩说是从散修遗物里翻出来的。",
        },
        {
            "name": "灵纹锦囊", "price": 8000, "stone_tier": 2,
            "risk": "中", "fake_chance": 0.32, "curse_chance": 0.07,
            "desc": "袋口灵纹半明半灭，像是封过什么东西。",
        },
        {
            "name": "紫金锦囊", "price": 30000, "stone_tier": 4,
            "risk": "高", "fake_chance": 0.30, "curse_chance": 0.09,
            "desc": "紫金丝线织成，灵压沉得压手，也最容易被做局。",
        },
        {
            "name": "太古锦囊", "price": 100000, "stone_tier": 5,
            "risk": "九死一生", "fake_chance": 0.28, "curse_chance": 0.12,
            "desc": "传自太古秘市，袋上禁制已经斑驳，里面可能是帝品遗珍。",
        },
    )
    POUCH_INSPECT_RATE = 0.20
    POUCH_TIPS_TRUE = {
        "jewel": ["灵压沉凝如珠玉", "袋中隐有清越的碰撞声", "隔着锦囊也能感到温润宝光"],
        "gear": ["有金铁之气透袋而出", "袋身沉甸甸，像封着兵刃", "灵纹随呼吸微微发亮"],
        "material": ["灵气驳杂但充沛", "袋底有细碎的矿砂声", "药香与土腥气混在一起"],
        "egg": ["袋中似乎有活物在动", "隐约传来极轻的心跳声", "锦囊内里微微发热"],
        "fake": ["袋口死气沉沉", "轻得几乎没有分量", "灵纹早已断裂"],
        "curse": ["袋身透出一股阴冷", "指尖触到禁制时微微刺痛", "隐约有煞气外泄"],
    }
    POUCH_TIPS_FALSE = {
        "jewel": ["商贩赌咒说里面是帝品珠玉", "袋面绣着夸张的宝物纹样"],
        "gear": ["说是某位剑修的随身锦囊", "外袋写着「神兵」二字"],
        "material": ["据说是灵矿脉心所结", "闻起来有一股浓重药味"],
        "fake": ["商贩拍胸脯保证有货", "说是刚从秘境里带出来的"],
        "curse": ["商贩说只是普通旧物", "袋口被人用朱砂封过"],
    }

    @staticmethod
    def _pouch_period() -> str:
        return time.strftime("%Y-%m-%d-%H", time.localtime())

    def _get_pouch_candidates(self, user_id: str) -> List[dict]:
        period = self._pouch_period()
        result = []
        for index, cfg in enumerate(self.POUCH_TIER_CONFIG, 1):
            rng = random.Random(f"pouch:{period}:{user_id}:{index}")
            roll = rng.random()
            fake_end = float(cfg["fake_chance"])
            curse_end = fake_end + float(cfg["curse_chance"])
            if roll < fake_end:
                hidden = {"kind": "fake", "loot": []}
            elif roll < curse_end:
                hidden = {"kind": "curse", "loot": [], "hp_pct": 0.12, "gold_pct": 0.08}
            else:
                hidden = self._roll_tier_loot(int(cfg["stone_tier"]), rng)
                if hidden.get("kind") == "dust":
                    hidden = {"kind": "fake", "loot": []}
            kind = hidden.get("kind") or "fake"
            true_pool = self.POUCH_TIPS_TRUE.get(kind) or self.POUCH_TIPS_TRUE["material"]
            false_pool = self.POUCH_TIPS_FALSE.get(kind) or self.POUCH_TIPS_FALSE["fake"]
            clues = [{"text": rng.choice(true_pool), "true": True}]
            if rng.random() < 0.55:
                clues.append({"text": rng.choice(true_pool), "true": True})
            while len(clues) < 4:
                clues.append({"text": rng.choice(false_pool), "true": False})
            rng.shuffle(clues)
            result.append({
                "index": index,
                "name": cfg["name"],
                "price": int(cfg["price"]),
                "desc": cfg["desc"],
                "risk": cfg["risk"],
                "clues": clues,
                "hidden": hidden,
                "period": period,
            })
        return result

    async def _get_pouch_state(self, user_id: str, period: str) -> dict:
        async with self.db.conn.execute(
            "SELECT slot, inspected, opened, result FROM black_market_pouches "
            "WHERE user_id = ? AND period = ?",
            (user_id, period),
        ) as cursor:
            rows = await cursor.fetchall()
        return {
            int(row[0]): {
                "inspected": bool(row[1]),
                "opened": bool(row[2]),
                "result": row[3] or "",
            }
            for row in rows
        }

    async def _save_pouch_state(self, user_id: str, period: str, slot: int,
                                inspected: bool, opened: bool, result: str = "",
                                commit: bool = True) -> None:
        await self.db.conn.execute(
            "INSERT INTO black_market_pouches "
            "(user_id, period, slot, inspected, opened, result) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, period, slot) DO UPDATE SET "
            "inspected = excluded.inspected, opened = excluded.opened, result = excluded.result",
            (user_id, period, slot, int(inspected), int(opened), result),
        )
        if commit:
            await self.db.conn.commit()

    async def get_pouches(self, user_id: str) -> Tuple[bool, str]:
        candidates = self._get_pouch_candidates(user_id)
        state = await self._get_pouch_state(user_id, candidates[0]["period"])
        lines = ["🕶️ 黑市 · 密封锦囊（每小时刷新）", "━━━━━━━━━━━━━━━"]
        for item in candidates:
            slot_state = state.get(item["index"], {})
            status = " ❌已拆开" if slot_state.get("opened") else ""
            lines.append(
                f"[{item['index']}] {item['name']} - {item['price']:,} 灵石"
                f"（风险：{item['risk']}）{status}"
            )
            lines.append(f"    {item['desc']}")
            for clue in item["clues"]:
                lines.append(f"    🔍 {clue['text']}")
            if slot_state.get("inspected"):
                lines.append(f"    🔎 已验囊：{slot_state.get('result') or '禁制反噬，未看到结果'}")
        remaining = 3600 - (int(time.time()) % 3600)
        lines.extend([
            "━━━━━━━━━━━━━━━",
            f"⏳ 下次刷新：{remaining // 60} 分 {remaining % 60} 秒后",
            "💡 `/黑市 拆囊 <编号>` 拆开｜`/黑市 验囊 <编号>` 付费验囊（每袋一次）",
            "⚠️ 档位越高，稀有物概率越高，但空囊、假货与诅咒也越多。",
        ])
        return True, "\n".join(lines)

    def build_pouch_entries(self, user_id: str, state: dict = None) -> list:
        state = state or {}
        entries = []
        for item in self._get_pouch_candidates(user_id):
            slot_state = state.get(item["index"], {})
            entries.append({
                "id": item["index"],
                "name": item["name"],
                "rank": "密封",
                "kind": "锦囊",
                "price": item["price"],
                "currency": "灵石",
                "effect": item["desc"],
                "effectLabel": "商贩说辞",
                "state": "sold" if slot_state.get("opened") else "ok",
                "cmd": f"/黑市 拆囊 {item['index']}",
                "source": "pouch",
                "action": "拆囊",
                "notes": [clue["text"] for clue in item["clues"]]
                         + ([f"已验囊：{slot_state.get('result')}"] if slot_state.get("inspected") else [])
                         + ["可先 /黑市 验囊 <编号> 付费验囊"],
            })
        return entries

    async def inspect_pouch(self, player: Player, index: int) -> Tuple[bool, str]:
        candidates = self._get_pouch_candidates(player.user_id)
        if index < 1 or index > len(candidates):
            return False, "❌ 无效的锦囊编号，请使用 `/黑市 锦囊` 查看。"
        item = candidates[index - 1]
        period = item["period"]
        state = await self._get_pouch_state(player.user_id, period)
        slot_state = state.get(index, {})
        if slot_state.get("opened"):
            return False, "❌ 这个锦囊已经拆开了。"
        if slot_state.get("inspected"):
            return False, "❌ 每个锦囊只能验一次，禁制已经惊动了。"
        cost = max(100, int(item["price"] * self.POUCH_INSPECT_RATE))
        if player.gold < cost:
            return False, f"❌ 验囊需要 {cost:,} 灵石，你当前只有 {player.gold:,}。"

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (cost, player.user_id, cost),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 灵石不足，验囊失败。"
            fresh = await self.db.get_player_by_id(player.user_id)
            if fresh:
                player.gold = fresh.gold
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        # 验囊结果 80% 指向真实类别，20% 被商贩做局误导
        rng = random.Random(f"pouch-inspect:{period}:{player.user_id}:{index}")
        kind = item["hidden"].get("kind") or "fake"
        if rng.random() < 0.20:
            kind = rng.choice([k for k in self.POUCH_TIPS_TRUE if k != kind])
        pool = self.POUCH_TIPS_TRUE.get(kind) or self.POUCH_TIPS_TRUE["fake"]
        hint = rng.choice(pool)
        await self._save_pouch_state(
            player.user_id, period, index, True, False, hint
        )
        return True, (
            f"🔎 你花 {cost:,} 灵石请老师傅验囊。\n"
            f"老师傅眯眼看了半晌，只说了一句：\n"
            f"「{hint}」\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 剩余灵石：{player.gold:,}\n"
            f"⚠️ 验囊结果也可能被做局，拆囊前请自行判断。"
        )

    async def open_pouch(self, player: Player, index: int) -> Tuple[bool, str]:
        candidates = self._get_pouch_candidates(player.user_id)
        if index < 1 or index > len(candidates):
            return False, "❌ 无效的锦囊编号，请使用 `/黑市 锦囊` 查看。"
        item = candidates[index - 1]
        period = item["period"]
        state = await self._get_pouch_state(player.user_id, period)
        if state.get(index, {}).get("opened"):
            return False, "❌ 这个锦囊本小时已经被你拆开了，请等待下次刷新。"

        price = int(item["price"])
        if player.gold < price:
            return False, f"❌ 灵石不足！拆开【{item['name']}】需要 {price:,} 灵石。"

        hidden = item["hidden"]
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (price, player.user_id, price),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 灵石不足，拆囊失败。"
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                return False, "❌ 角色数据异常，本次交易已取消。"

            kind = hidden.get("kind") or "fake"
            extra_lines = []
            if kind == "curse":
                hp_pct = float(hidden.get("hp_pct") or 0.12)
                gold_pct = float(hidden.get("gold_pct") or 0.08)
                from ..managers.combat_manager import CombatManager
                stats = CombatManager.build_player_combat_stats(
                    fresh, self.config_manager, db=self.db
                )
                max_hp = max(1, int(stats.max_hp))
                hp_loss = max(0, int(max_hp * hp_pct))
                gold_loss = min(fresh.gold, max(0, int(fresh.gold * gold_pct)))
                new_hp = max(1, int(fresh.hp or max_hp) - hp_loss)
                await self.db.conn.execute(
                    "UPDATE players SET hp = ?, gold = gold - ? WHERE user_id = ?",
                    (new_hp, gold_loss, player.user_id),
                )
                fresh.hp = new_hp
                fresh.gold = max(0, fresh.gold - gold_loss)
                extra_lines.append(
                    f"☠️ 锦囊上残留的诅咒爆发：气血 -{hp_loss:,}、灵石 -{gold_loss:,}"
                )
                result_text = "诅咒锦囊"
            elif kind == "fake":
                result_text = "空囊/假货"
                extra_lines.append("💨 袋中只有几张废符纸和一把灰，商贩早已不知去向。")
            else:
                result_text = await self._award_tier_loot(fresh, hidden)
            await self.db.update_player(fresh, commit=False)
            await self._save_pouch_state(
                player.user_id, period, index,
                bool(state.get(index, {}).get("inspected")), True, result_text,
                commit=False,
            )
            await self.db.conn.commit()
            player.gold = fresh.gold
            player.hp = fresh.hp
        except Exception:
            await self.db.conn.rollback()
            raise

        body = "\n".join(extra_lines) if extra_lines else result_text
        return True, (
            f"🧧 你花 {price:,} 灵石拆开了【{item['name']}】（风险：{item['risk']}）：\n"
            f"━━━━━━━━━━━━━━━\n{body}\n━━━━━━━━━━━━━━━\n"
            f"💰 剩余灵石：{player.gold:,}"
        )
