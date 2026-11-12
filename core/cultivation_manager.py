# core/cultivation_manager.py
import random
import json
from typing import Dict, Optional

from astrbot.api import AstrBotConfig, logger
from ..config_manager import ConfigManager
from ..models import Player

def weighted_cultivation_multiplier(
    effects, begin_ts: int, end_ts: int, cap: float = 3.0
) -> dict:
    """把「临时丹药 / 符箓的修炼加速」按实际覆盖的时段加权，算出等效倍率。

    旧口径只看**出关那一瞬间**是否还有效，于是两种极端都不对：
      - 闭关前吃一颗 60 分钟的加速丹、闭关 24 小时 → 全程都按加速算（白赚）；
      - 闭关 10 小时、第 2 小时吃了丹，出关时早过期 → 一点加成都不算（白吃）。

    改成按时间轴分段：每段的倍率 = 1 + 该段内所有加速效果之和，再按分钟数加权平均。
    没有任何覆盖的时段按 1.0 计，所以「吃一颗 60 分钟的丹闭关 10 小时」只拿 10% 的权重。

    参数里的 ``effects`` 要传**清理过期之前**的快照，否则闭关中途过期的那批
    已经被 ``update_temporary_effects`` 删掉了，权重就丢了。
    """
    total_minutes = max(0, int((end_ts - begin_ts) // 60))
    result = {
        "multiplier": 1.0,
        "total_minutes": total_minutes,
        "covered_minutes": 0,
        "segments": [],
        "uncovered_minutes": total_minutes,
        "cap_hit": False,
    }
    if total_minutes <= 0 or not effects:
        return result

    active = []
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        try:
            bonus = float(effect.get("cultivation_multiplier") or 0)
        except (TypeError, ValueError):
            continue
        if bonus == 0:
            continue

        # expiry_time 为 0 / 缺失 = 不按时间失效（按次数结算的那类），视为全程有效
        expiry = 0
        try:
            expiry = int(effect.get("expiry_time") or 0)
        except (TypeError, ValueError):
            expiry = 0
        start = begin_ts
        try:
            start = max(begin_ts, int(effect.get("start_time") or begin_ts))
        except (TypeError, ValueError):
            start = begin_ts
        stop = end_ts if expiry <= 0 else min(end_ts, expiry)
        if stop <= start:
            continue
        active.append({
            "name": str(effect.get("talisman_name") or effect.get("pill_name") or "临时效果"),
            "quality": str(effect.get("quality") or ""),
            "source": "talisman" if effect.get("source") == "talisman" else "pill",
            "bonus": bonus,
            "begin": start,
            "end": stop,
        })

    if not active:
        return result

    # 事件点扫描：效果区间的端点切开时间轴，段内倍率恒定
    points = sorted({begin_ts, end_ts} | {e["begin"] for e in active} | {e["end"] for e in active})
    weighted = 0.0
    covered = 0
    segments = {}
    cap_hit = False

    for left, right in zip(points, points[1:]):
        if right <= left:
            continue
        minute = (right - left) / 60.0
        mid = (left + right) / 2
        bonus = sum(e["bonus"] for e in active if e["begin"] <= mid < e["end"])
        if bonus == 0:
            weighted += minute
            continue
        seg_mult = 1.0 + bonus
        if seg_mult > cap:
            seg_mult = cap
            cap_hit = True
        elif seg_mult < 0:
            seg_mult = 0.0
        weighted += minute * seg_mult
        covered += minute
        for e in active:
            if e["begin"] <= mid < e["end"]:
                key = (e["name"], e["quality"], e["source"], e["bonus"])
                segments[key] = segments.get(key, 0) + minute

    result.update({
        "multiplier": weighted / total_minutes,
        "covered_minutes": covered,
        "uncovered_minutes": total_minutes - covered,
        "cap_hit": cap_hit,
        "segments": [
            {"name": k[0], "quality": k[1], "source": k[2], "bonus": k[3], "minutes": m}
            for k, m in sorted(segments.items(), key=lambda kv: (-kv[1], kv[0][0]))
        ],
    })
    return result


def format_bonus_note(cult_bonus: dict, permanent_bonus: float = 0.0, effects=None) -> str:
    """把丹符加成拼成结算里的一行说明。

    数值已经算进「获得修为」里了，这里只负责让它看得见：
    每一项加了多少、覆盖了多少分钟、加权后等效几倍。
    """
    segments = (cult_bonus or {}).get("segments") or []
    try:
        permanent = float(permanent_bonus or 0)
    except (TypeError, ValueError):
        permanent = 0.0

    if segments:
        parts = []
        for seg in segments[:4]:
            quality = f"·{seg['quality']}" if seg.get("quality") else ""
            try:
                percent = float(seg["bonus"]) * 100
            except (TypeError, ValueError):
                continue
            parts.append(f"{seg['name']}{quality} +{percent:.0f}%（{int(seg['minutes'])}分）")
        if len(segments) > 4:
            parts.append(f"等 {len(segments)} 项")
        covered = int(cult_bonus.get("covered_minutes") or 0)
        total = int(cult_bonus.get("total_minutes") or 0)
        try:
            mult = float(cult_bonus.get("multiplier") or 1.0)
        except (TypeError, ValueError):
            mult = 1.0
        note = (
            f"\n🧪 丹符加成：{'、'.join(parts)}"
            f"\n　 └ 等效 ×{mult:.2f}（覆盖 {covered}/{total} 分钟）"
        )
        if permanent > 0:
            note += f"，永久 +{permanent * 100:.0f}%"
        return note

    if permanent > 0:
        return f"\n🧪 丹符加成：永久 +{permanent * 100:.0f}%"

    # 吃了/用了但一点也没覆盖到闭关时段：说清楚为什么没加，别让人以为忘了结算
    for effect in effects or []:
        if not isinstance(effect, dict):
            continue
        try:
            if float(effect.get("cultivation_multiplier") or 0):
                return "\n⏳ 修炼加速丹药/符箓未覆盖闭关时段，本次未计入"
        except (TypeError, ValueError):
            continue
    return ""


class CultivationManager:
    """修炼管理器，包含角色生成和闭关修炼功能"""

    def __init__(self, config: AstrBotConfig, config_manager: ConfigManager):
        self.config = config
        self.config_manager = config_manager

        # 灵根名称到配置项键的映射
        self.root_to_config_key = {
            "伪": "PSEUDO_ROOT_SPEED",
            "金木水火": "QUAD_ROOT_SPEED", "金木水土": "QUAD_ROOT_SPEED", "金木火土": "QUAD_ROOT_SPEED", "金水火土": "QUAD_ROOT_SPEED", "木水火土": "QUAD_ROOT_SPEED",
            "金木水": "TRI_ROOT_SPEED", "金木火": "TRI_ROOT_SPEED", "金木土": "TRI_ROOT_SPEED", "金水火": "TRI_ROOT_SPEED", "金水土": "TRI_ROOT_SPEED", "金火土": "TRI_ROOT_SPEED", "木水火": "TRI_ROOT_SPEED", "木水土": "TRI_ROOT_SPEED", "木火土": "TRI_ROOT_SPEED", "水火土": "TRI_ROOT_SPEED",
            "金木": "DUAL_ROOT_SPEED", "金水": "DUAL_ROOT_SPEED", "金火": "DUAL_ROOT_SPEED", "金土": "DUAL_ROOT_SPEED", "木水": "DUAL_ROOT_SPEED", "木火": "DUAL_ROOT_SPEED", "木土": "DUAL_ROOT_SPEED", "水火": "DUAL_ROOT_SPEED", "水土": "DUAL_ROOT_SPEED", "火土": "DUAL_ROOT_SPEED",
            "金": "WUXING_ROOT_SPEED", "木": "WUXING_ROOT_SPEED", "水": "WUXING_ROOT_SPEED", "火": "WUXING_ROOT_SPEED", "土": "WUXING_ROOT_SPEED",
            "雷": "THUNDER_ROOT_SPEED", "冰": "ICE_ROOT_SPEED", "风": "WIND_ROOT_SPEED", "暗": "DARK_ROOT_SPEED", "光": "LIGHT_ROOT_SPEED",
            "天金": "HEAVENLY_ROOT_SPEED", "天木": "HEAVENLY_ROOT_SPEED", "天水": "HEAVENLY_ROOT_SPEED", "天火": "HEAVENLY_ROOT_SPEED", "天土": "HEAVENLY_ROOT_SPEED", "天雷": "HEAVENLY_ROOT_SPEED",
            "阴阳": "YIN_YANG_ROOT_SPEED", "融合": "FUSION_ROOT_SPEED",
            "混沌": "CHAOS_ROOT_SPEED",
            "先天道体": "INNATE_BODY_SPEED", "神圣体质": "DIVINE_BODY_SPEED"
        }

        # 灵根池定义（按权重类别）
        self.root_pools = {
            "PSEUDO": ["伪"],
            "QUAD": ["金木水火", "金木水土", "金木火土", "金水火土", "木水火土"],
            "TRI": ["金木水", "金木火", "金木土", "金水火", "金水土", "金火土", "木水火", "木水土", "木火土", "水火土"],
            "DUAL": ["金木", "金水", "金火", "金土", "木水", "木火", "木土", "水火", "水土", "火土"],
            "WUXING": ["金", "木", "水", "火", "土"],
            "VARIANT": ["雷", "冰", "风", "暗", "光"],
            "HEAVENLY": ["天金", "天木", "天水", "天火", "天土", "天雷"],
            "LEGENDARY": ["阴阳", "融合"],
            "MYTHIC": ["混沌"],
            "DIVINE_BODY": ["先天道体", "神圣体质"]
        }

        # 灵根阶位（由低到高）：抽取池即阶位。飞升后的道体重塑按这张表给保底：
        # 平时保「与上一世同档」，每 5 批保「高一档」。
        # 括号里是展示用的阶名，玩家在 /灵根说明 里看到的也是这套。
        self.root_tier_order = [
            ("PSEUDO", "废灵根"),
            ("QUAD", "四灵根·凡品"),
            ("TRI", "三灵根·凡品"),
            ("DUAL", "双灵根·良品"),
            ("WUXING", "五行单灵根·上品"),
            ("VARIANT", "变异灵根·稀有"),
            ("HEAVENLY", "天灵根·极品"),
            ("LEGENDARY", "传说灵根"),
            ("MYTHIC", "神话灵根"),
            ("DIVINE_BODY", "禁忌道体"),
        ]
        self.root_to_tier = {
            root: idx
            for idx, (pool_key, _) in enumerate(self.root_tier_order)
            for root in self.root_pools[pool_key]
        }
        # 抽取权重：池权重 × 池内条数才是每条灵根的权重，用于 /灵根说明 显示单抽概率
        self.tier_weight_keys = {
            "PSEUDO": ("PSEUDO_ROOT_WEIGHT", 1),
            "QUAD": ("QUAD_ROOT_WEIGHT", 10),
            "TRI": ("TRI_ROOT_WEIGHT", 30),
            "DUAL": ("DUAL_ROOT_WEIGHT", 100),
            "WUXING": ("WUXING_ROOT_WEIGHT", 200),
            "VARIANT": ("VARIANT_ROOT_WEIGHT", 20),
            "HEAVENLY": ("HEAVENLY_ROOT_WEIGHT", 5),
            "LEGENDARY": ("LEGENDARY_ROOT_WEIGHT", 2),
            "MYTHIC": ("MYTHIC_ROOT_WEIGHT", 1),
            "DIVINE_BODY": ("DIVINE_BODY_WEIGHT", 1),
        }

    def _calculate_base_stats(self, level_index: int, cultivation_type: str = "灵修") -> Dict[str, int]:
        level_data = self.config_manager.get_level_data(cultivation_type)
        if 0 <= level_index < len(level_data):
            level_config = level_data[level_index]
            return {
                "lifespan": level_config.get("base_lifespan", 100 + level_index * 50),
                "max_spiritual_qi": level_config.get("base_max_spiritual_qi", 50 + level_index * 20),
                "max_blood_qi": level_config.get("base_max_blood_qi", 50 + level_index * 20),
                "mental_power": level_config.get("base_mental_power", 50 + level_index * 20),
                "physical_damage": level_config.get("base_physical_damage", 10 + level_index * 8),
                "magic_damage": level_config.get("base_magic_damage", 10 + level_index * 8),
                "physical_defense": level_config.get("base_physical_defense", 5 + level_index * 4),
                "magic_defense": level_config.get("base_magic_defense", 5 + level_index * 4)
            }
        else:
            return {
                "lifespan": 100 + level_index * 50, "max_spiritual_qi": 50 + level_index * 20, "max_blood_qi": 50 + level_index * 20,
                "mental_power": 50 + level_index * 20, "physical_damage": 10 + level_index * 8, "magic_damage": 10 + level_index * 8,
                "physical_defense": 5 + level_index * 4, "magic_defense": 5 + level_index * 4
            }

    def _get_random_spiritual_root(self) -> str:
        weights_config = self.config.get("SPIRIT_ROOT_WEIGHTS", {})
        weight_pool = []
        
        def add_pool(weight_key, pool_key, default_weight):
            weight = int(weights_config.get(weight_key, default_weight))
            if weight > 0:
                weight_pool.extend([(pool_key, root) for root in self.root_pools[pool_key]] * weight)

        add_pool("PSEUDO_ROOT_WEIGHT", "PSEUDO", 1)
        add_pool("QUAD_ROOT_WEIGHT", "QUAD", 10)
        add_pool("TRI_ROOT_WEIGHT", "TRI", 30)
        add_pool("DUAL_ROOT_WEIGHT", "DUAL", 100)
        add_pool("WUXING_ROOT_WEIGHT", "WUXING", 200)
        add_pool("VARIANT_ROOT_WEIGHT", "VARIANT", 20)
        add_pool("HEAVENLY_ROOT_WEIGHT", "HEAVENLY", 5)
        add_pool("LEGENDARY_ROOT_WEIGHT", "LEGENDARY", 2)
        add_pool("MYTHIC_ROOT_WEIGHT", "MYTHIC", 1)
        add_pool("DIVINE_BODY_WEIGHT", "DIVINE_BODY", 1)

        if not weight_pool:
            logger.warning("灵根权重池为空，使用默认金灵根")
            return "金"

        _, selected_root = random.choice(weight_pool)
        return selected_root

    def _get_root_speed(self, root_name: str) -> float:
        """灵根对应的修炼速度倍率（用于展示）。"""
        config_key = self.root_to_config_key.get(root_name)
        if not config_key:
            return 1.0
        speeds = self.config.get("SPIRIT_ROOT_SPEEDS", {}) or {}
        try:
            return float(speeds.get(config_key, 1.0) or 1.0)
        except (TypeError, ValueError):
            return 1.0

    # ---------- 灵根阶位 / 图鉴 ----------

    @staticmethod
    def root_name(root: str) -> str:
        """去掉「灵根」后缀，统一成灵根名（金木 / 天雷 / 先天道体）。"""
        return str(root or "").replace("灵根", "").strip()

    def root_tier_index(self, root_name: str) -> int:
        """返回灵根所在阶位下标（0 最低），未知返回 -1。"""
        return self.root_to_tier.get(self.root_name(root_name), -1)

    def root_tier_label(self, root_name: str) -> str:
        idx = self.root_tier_index(root_name)
        if idx < 0:
            return "未知阶位"
        return self.root_tier_order[idx][1]

    def root_tier_roots(self, idx: int) -> list:
        """某一阶位的全部灵根。"""
        if 0 <= idx < len(self.root_tier_order):
            return list(self.root_pools[self.root_tier_order[idx][0]])
        return []

    def upgrade_root_for(self, base_root: str) -> str:
        """比 base_root 高一档的灵根（随机一条）。

        仅作展示/工具用途（例如说明里「高一档」的示例）；真正的抽取保底走
        `draw_spiritual_roots(min_tier=...)`，由调用方按批次决定同档还是升档。
        已是最高档或无法识别时返回空串。
        """
        idx = self.root_tier_index(base_root)
        if idx < 0 or idx + 1 >= len(self.root_tier_order):
            return ""
        pool = self.root_tier_roots(idx + 1)
        return random.choice(pool) if pool else ""

    def root_draw_chance(self, root_name: str) -> float:
        """单条灵根的单抽概率（0~1），按当前配置权重实时计算。"""
        name = self.root_name(root_name)
        idx = self.root_tier_index(name)
        if idx < 0:
            return 0.0
        weights_cfg = self.config.get("SPIRIT_ROOT_WEIGHTS", {}) or {}
        total = 0.0
        weight = 0.0
        for tier_idx, (pool_key, _) in enumerate(self.root_tier_order):
            key, default = self.tier_weight_keys[pool_key]
            try:
                w = max(0.0, float(weights_cfg.get(key, default)))
            except (TypeError, ValueError):
                w = 0.0
            size = len(self.root_pools[pool_key])
            total += w * size
            if tier_idx == idx:
                weight = w
        if total <= 0:
            return 0.0
        return weight / total

    def root_tier_label_by_index(self, idx: int) -> str:
        if 0 <= idx < len(self.root_tier_order):
            return self.root_tier_order[idx][1]
        return "未知阶位"

    def draw_spiritual_roots(self, count: int = 3, min_tier: int = -1) -> list:
        """按现有权重抽多个不重复的灵根，供创号/重修三选一。

        `min_tier` >= 0 时（飞升后的道体重塑），本批必有一条灵根的阶位不低于它：
        自然抽到的最高阶位已达标就不干预，否则把阶位最低的那条换成该档位的随机灵根。
        传 -1 表示纯随机（首次创号、弃道重修）。
        """
        results = []
        attempts = 0
        while len(results) < max(1, int(count)) and attempts < max(20, count * 20):
            root = self._get_random_spiritual_root()
            attempts += 1
            if root not in results:
                results.append(root)
        if not results:
            results.append("金")

        if not (0 <= min_tier < len(self.root_tier_order)):
            return results
        best = max((self.root_tier_index(r) for r in results), default=-1)
        if best >= min_tier:
            # 已经自然抽到达标灵根，再替换反而是降级
            return results
        pool = [r for r in self.root_tier_roots(min_tier) if r not in results]
        if not pool:
            return results
        lowest_idx = min(
            range(len(results)),
            key=lambda i: (self.root_tier_index(results[i]), i),
        )
        results[lowest_idx] = random.choice(pool)
        return results

    def _get_root_description(self, root_name: str) -> str:
        descriptions = {
            "伪": "【废柴】资质低劣，修炼如龟速",
            "金木水火": "【凡品】四灵根杂乱，资质平庸", "金木水土": "【凡品】四灵根杂乱，资质平庸", "金木火土": "【凡品】四灵根杂乱，资质平庸", "金水火土": "【凡品】四灵根杂乱，资质平庸", "木水火土": "【凡品】四灵根杂乱，资质平庸",
            "金木水": "【凡品】三灵根较杂，资质一般", "金木火": "【凡品】三灵根较杂，资质一般", "金木土": "【凡品】三灵根较杂，资质一般", "金水火": "【凡品】三灵根较杂，资质一般", "金水土": "【凡品】三灵根较杂，资质一般", "金火土": "【凡品】三灵根较杂，资质一般", "木水火": "【凡品】三灵根较杂，资质一般", "木水土": "【凡品】三灵根较杂，资质一般", "木火土": "【凡品】三灵根较杂，资质一般", "水火土": "【凡品】三灵根较杂，资质一般",
            "金木": "【良品】双灵根，较为常见", "金水": "【良品】双灵根，较为常见", "金火": "【良品】双灵根，较为常见", "金土": "【良品】双灵根，较为常见", "木水": "【良品】双灵根，较为常见", "木火": "【良品】双灵根，较为常见", "木土": "【良品】双灵根，较为常见", "水火": "【良品】双灵根，较为常见", "水土": "【良品】双灵根，较为常见", "火土": "【良品】双灵根，较为常见",
            "金": "【上品】金之精华，锋锐无双", "木": "【上品】木之生机，生生不息", "水": "【上品】水之灵韵，柔中带刚", "火": "【上品】火之烈焰，霸道无匹", "土": "【上品】土之厚重，稳如磐石",
            "雷": "【稀有】天地雷霆，毁灭之力", "冰": "【稀有】极寒冰封，万物凝固", "风": "【稀有】疾风骤雨，来去无踪", "暗": "【稀有】幽暗深邃，诡异莫测", "光": "【稀有】神圣光明，普照万物",
            "天金": "【极品】天选之子，金之极致", "天木": "【极品】天选之子，木之极致", "天水": "【极品】天选之子，水之极致", "天火": "【极品】天选之子，火之极致", "天土": "【极品】天选之子，土之极致", "天雷": "【极品】天选之子，雷之极致",
            "阴阳": "【传说】阴阳调和，造化玄机", "融合": "【传说】五行融合，万法归一",
            "混沌": "【神话】混沌初开，包罗万象",
            "先天道体": "【禁忌】天生道体，与天地同寿", "神圣体质": "【禁忌】神之后裔，天赋异禀"
        }
        return descriptions.get(root_name, "【未知】神秘的灵根")

    def generate_new_player_stats(
        self, user_id: str, cultivation_type: str = "灵修", root: str = ""
    ) -> Player:
        root = root or self._get_random_spiritual_root()
        initial_gold = self.config.get("VALUES", {}).get("INITIAL_GOLD", 1000)

        if cultivation_type == "体修":
            initial_blood_qi = random.randint(100, 500)
            return Player(
                user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="体修",
                lifespan=random.randint(50, 100), experience=0, gold=initial_gold,
                spiritual_qi=0, max_spiritual_qi=0, blood_qi=initial_blood_qi, max_blood_qi=initial_blood_qi,
                magic_damage=0, physical_damage=random.randint(100, 500),
                magic_defense=random.randint(50, 200), physical_defense=random.randint(100, 500),
                mental_power=random.randint(100, 500)
            )
        elif cultivation_type == "魔修":
            return Player(user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="魔修", lifespan=80, experience=0, gold=initial_gold, spiritual_qi=500, max_spiritual_qi=500, blood_qi=0, max_blood_qi=0, magic_damage=random.randint(150, 300), physical_damage=random.randint(150, 300), magic_defense=0, physical_defense=0, mental_power=200)
        elif cultivation_type == "剑修":
            return Player(user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="剑修", lifespan=100, experience=0, gold=initial_gold, spiritual_qi=100, max_spiritual_qi=100, blood_qi=0, max_blood_qi=0, magic_damage=0, physical_damage=random.randint(300, 500), magic_defense=0, physical_defense=0, mental_power=150)
        elif cultivation_type == "佛修":
            return Player(user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="佛修", lifespan=150, experience=0, gold=initial_gold, spiritual_qi=2000, max_spiritual_qi=2000, blood_qi=0, max_blood_qi=0, magic_damage=10, physical_damage=10, magic_defense=200, physical_defense=300, mental_power=500)
        elif cultivation_type == "鬼修":
            return Player(user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="鬼修", lifespan=80, experience=0, gold=initial_gold, spiritual_qi=800, max_spiritual_qi=800, blood_qi=0, max_blood_qi=0, magic_damage=200, physical_damage=50, magic_defense=20, physical_defense=20, mental_power=800)
        elif cultivation_type == "御兽":
            return Player(user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="御兽", lifespan=100, experience=0, gold=initial_gold, spiritual_qi=600, max_spiritual_qi=600, blood_qi=0, max_blood_qi=0, magic_damage=100, physical_damage=100, magic_defense=50, physical_defense=50, mental_power=300)
        else: # 默认灵修
            initial_max_qi = random.randint(100, 1000)
            return Player(
                user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="灵修",
                lifespan=100, experience=0, gold=initial_gold,
                spiritual_qi=random.randint(0, initial_max_qi), max_spiritual_qi=initial_max_qi,
                blood_qi=0, max_blood_qi=0, magic_damage=random.randint(100, 200), physical_damage=5,
                magic_defense=10, physical_defense=5, mental_power=random.randint(100, 500)
            )

    def get_spiritual_root_speed(self, player: Player) -> float:
        root_name = player.spiritual_root.replace("灵根", "")
        config_key = self.root_to_config_key.get(root_name)
        if not config_key:
            return 1.0
        speeds_config = self.config.get("SPIRIT_ROOT_SPEEDS", {})
        return speeds_config.get(config_key, 1.0)

    def breakthrough_need(self, player: Player) -> int:
        """当前境界突破所需修为（lv0 或缺失时回退到下一级）。"""
        try:
            level_index = max(0, int(getattr(player, "level_index", 0) or 0))
        except (TypeError, ValueError):
            level_index = 0
        try:
            level_data = self.config_manager.get_level_data(
                getattr(player, "cultivation_type", "灵修") or "灵修"
            )
        except Exception:
            level_data = []
        for idx in (level_index, level_index + 1):
            if 0 <= idx < len(level_data):
                try:
                    need = int(level_data[idx].get("exp_needed") or 0)
                except (TypeError, ValueError):
                    need = 0
                if need > 0:
                    return need
        return 1000

    def calculate_cultivation_exp(
        self,
        player: Player,
        minutes: int,
        technique_bonus: float = 0.0,
        pill_multipliers: Optional[Dict[str, float]] = None
    ) -> int:
        """计算闭关修炼获得的修为。

        v7.7 起改为「按本境界突破需求折算」，避免固定基数跟不上境界膨胀：
            每小时修为 = 突破需求 × BREAKTHROUGH_RATE_PER_HOUR × 灵根 × (1+功法) × 丹药符箓 × 仙缘
        0.25%/小时 意味着无加成闭关约 400 小时突破，满配约 20 小时。
        """
        try:
            rate_per_hour = float(
                self.config.get("VALUES", {}).get("BREAKTHROUGH_RATE_PER_HOUR", 0.0025)
            )
        except (TypeError, ValueError):
            rate_per_hour = 0.0025
        need = self.breakthrough_need(player)
        base_exp = need * rate_per_hour / 60.0  # 每分钟基础修为
        root_speed = self.get_spiritual_root_speed(player)

        cultivation_pill_bonus = 1.0
        if pill_multipliers:
            cultivation_pill_bonus = pill_multipliers.get("cultivation_speed", 1.0)

        # ===== 获取仙缘商店的永久修炼加速 =====
        global_mult = 1.0
        try:
            bonus_data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            global_mult = 1.0 + bonus_data.get("global_cultivation_multiplier", 0.0)
            # 【悟道蒲团】只作用于闭关打坐，与修炼加速相乘
            global_mult *= 1.0 + max(0.0, float(bonus_data.get("seclusion_bonus", 0.0) or 0.0))
        except Exception:
            pass
        # ======================================

        # 境界差异已由「突破需求」本身体现，不再额外乘等级倍率。
        # 总倍率 = 灵根 × (1+功法) × 丹药符箓 × 仙缘
        total_multiplier = (
            root_speed
            * (1.0 + technique_bonus)
            * cultivation_pill_bonus
            * global_mult
        )
        total_exp = int(base_exp * minutes * total_multiplier)

        logger.info(
            f"玩家 {player.user_id} 闭关 {minutes} 分钟，突破需求 {need}，"
            f"基础 {base_exp:.1f}/分钟，灵根倍率 {root_speed:.2f}，"
            f"功法加成 {technique_bonus:.2%}，丹药倍率 {cultivation_pill_bonus:.2f}，"
            f"仙缘倍率 {global_mult:.2f}，获得修为 {total_exp}"
        )
        return total_exp