# managers/adventure_manager.py
"""
历练系统管理器 - 可配置路线、风险与奖励
"""

import json
import random
import time
from pathlib import Path
from typing import Tuple, Dict, Optional, List, TYPE_CHECKING

from astrbot.api import logger

from ..data.data_manager import DataBase
from ..models import Player
from ..models_extended import UserStatus

if TYPE_CHECKING:
    from ..core import StorageRingManager


class AdventureManager:
    """历练系统管理器"""

    CONFIG_FILE = Path(__file__).resolve().parents[1] / "config" / "adventure_config.json"

    # 材料品阶从低到高。掉落池在某品阶为空时（例如妖兽类没有凡品材料），
    # 沿这个顺序向相邻品阶回退，避免某个专精路线在某些境界段掉不出东西。
    TIER_ORDER = ("凡品", "黄品", "玄品", "圣品", "帝品")
    PRODUCT_CATEGORIES = ("灵植", "矿材", "灵液", "妖兽")

    # 品阶回退时的数量折扣：往上回退（拿到了更值钱的品阶）要少给一些，
    # 否则「妖兽类没有凡品」会让炼气期的夜猎路线比采药路线肥 57%，新人全去刷妖兽。
    FALLBACK_COUNT_SCALE = 0.6

    # v7.7 起历练回到「走完全程一次性结算」，并成为修为主产地：
    # 每小时修为 = 本境界突破需求 × BASE_RATE_PER_HOUR × (1 + 丹药 + 符箓修炼加成) × 仙缘，
    # 灵根与功法不参与（它们只作用于闭关）。
    # v7.8.2 把基准从「满配」挪到「零加成」：0.75% = 闭关零加成 0.25% × 3。
    # 旧值 1.656% 是用满配灵根 3.2 × 功法 2.3 × 0.9 倒推的常数，而闭关的值随灵根/功法
    # 缩放，于是零加成时历练变成闭关的 6.6 倍、伪灵根更是 9.5 倍 —— 灵根越差越被迫
    # 全走历练，v7.7「灵根差也能跟上」的设计反而变成了对差灵根的惩罚。
    # 现在以零加成为锚点：历练 = 闭关零加成 × 3。灵根/功法越高，历练相对越慢
    # （伪灵根 4.3 倍 → 三灵根 3.2 → 天灵根 1.6 → 先天道体 0.94），但不再有 9.5 倍那种
    # 失控区间；分水岭落在先天道体，只有顶级修炼天赋值得纯闭关。
    BASE_RATE_PER_HOUR = 0.0075
    GOLD_MULTIPLIER = 2.0
    # 一次性结算的材料判定次数。时长压到 15~30 分钟后，1 次判定就让单位时间产出
    # 与 v7.6 的「56 分钟 2 次判定」持平，不会因为结算变快而让材料翻倍。
    ITEM_ROLLS = 1
    # v7.9 定位调整：历练是修为/灵石产地，不是刷材料的地方，材料交给秘境。
    # 1.2 → 0.33 是「价值口径约为秘境最优档的 1/5」（模拟：秘境 ≈110k 灵石/时，
    # 历练砍后 ≈22k）。想再调直接改 config 的 material_model.chance_scale。
    ITEM_CHANCE_SCALE = 0.33
    # 材料品阶是否受「路线等级」封顶（见 _material_band）。
    MATERIAL_CAP_BY_ROUTE = True

    # 列表展示用：产物分类顺序、推荐位展示条数
    LIST_FACETS = ("灵植", "矿材", "灵液", "妖兽", "全产物")
    LIST_TOP_N = 6

    DEFAULT_CONFIG = {
        "routes": [
            {
                "key": "scout",
                "name": "巡山问道",
                "aliases": ["短途", "巡山"],
                "description": "巡视宗门周边，风险较低，适合积累经验。",
                "risk": "低",
                "duration": 1800,
                "reward_duration": 1800,
                "min_level": 0,
                "fatigue_cooldown": 300,
                "base_exp_per_min": 45,
                "base_gold_per_min": 10,
                "level_bonus_exp": 12,
                "level_bonus_gold": 3,
                "completion_bonus": {"exp": 300, "gold": 120},
                "event_weights": {"safe": 60, "standard": 30, "risky": 10},
                "drop_tier": "low",
                "bounty_tag": "adventure_scout",
                "bounty_progress": 1
            }
        ],
        "event_groups": {
            "safe": [
                {
                    "key": "steady_path",
                    "name": "平稳推进",
                    "desc": "历练过程顺风顺水，按部就班地完成目标。",
                    "exp_mult": 1.1,
                    "gold_mult": 1.1,
                    "item_chance": 60,
                    "bonus_progress": 0
                }
            ],
            "standard": [
                {
                    "key": "minor_skirmish",
                    "name": "遭遇小型冲突",
                    "desc": "击退拦路妖兽，实战经验有所增长。",
                    "exp_mult": 1.2,
                    "gold_mult": 1.2,
                    "item_chance": 50,
                    "bonus_progress": 1
                }
            ],
            "risky": [
                {
                    "key": "ambush",
                    "name": "埋伏受创",
                    "desc": "遭遇伏击，受了点伤但仍坚持完成任务。",
                    "exp_mult": 0.7,
                    "gold_mult": 0.7,
                    "item_chance": 15,
                    "bonus_progress": 0,
                    "injury": True
                }
            ]
        },
        "drop_tables": {
            "low": [
                {"name": "灵草", "weight": 50, "min": 1, "max": 3},
                {"name": "精铁", "weight": 30, "min": 1, "max": 2},
                {"name": "灵石碎片", "weight": 20, "min": 2, "max": 5}
            ]
        },
        "equipment_drop_chances": {"low": 5, "mid": 8, "high": 12},
        "equipment_slot_weights": {
            "weapon": 16, "armor": 16, "head": 12, "boots": 12,
            "amulet": 12, "gloves": 16, "ring": 16,
        },
    }

    def __init__(self, db: DataBase, storage_ring_manager: "StorageRingManager" = None, config_manager=None):
        self.db = db
        self.storage_ring_manager = storage_ring_manager
        self.config_manager = config_manager
        self._social = None  # 懒加载 SocialManager（避免循环导入）
        self._route_cooldowns: Dict[str, Dict[str, int]] = {}
        self.routes: Dict[str, dict] = {}
        self.route_alias_index: Dict[str, str] = {}
        self.event_groups: Dict[str, List[dict]] = {}
        self.drop_tables: Dict[str, List[dict]] = {}
        self.default_route_key: str = "scout"
        self.reload_config()

    # -------- 配置加载 --------

    def reload_config(self):
        """重新加载配置文件"""
        config = self._load_config_file()
        self.routes = {route["key"]: route for route in config.get("routes", [])}
        self.default_route_key = next(iter(self.routes.keys()), "scout")

        self.route_alias_index = {}
        for key, route in self.routes.items():
            aliases = set(route.get("aliases", []))
            aliases.add(route["key"])
            aliases.add(route["name"])
            # 兼容旧指令
            if route["key"] == "scout":
                aliases.update({"short", "短途"})
            elif route["key"] == "journey":
                aliases.update({"medium", "中途"})
            elif route["key"] == "peril":
                aliases.update({"long", "长途"})
            for alias in aliases:
                self.route_alias_index[alias.lower()] = key

        self.event_groups = config.get("event_groups", self.DEFAULT_CONFIG["event_groups"])
        self.drop_tables = config.get("drop_tables", self.DEFAULT_CONFIG["drop_tables"])
        self.equipment_drop_chances = config.get(
            "equipment_drop_chances", self.DEFAULT_CONFIG["equipment_drop_chances"]
        )
        self.equipment_slot_weights = config.get(
            "equipment_slot_weights", self.DEFAULT_CONFIG["equipment_slot_weights"]
        )

        # 新掉落体系：境界段 → 品阶 → 材料池。老配置文件没有这几项时回退到 drop_tables。
        self.realm_bands: List[dict] = config.get("realm_bands") or []
        self.drop_pools: Dict[str, Dict[str, List[str]]] = config.get("drop_pools") or {}
        self.drop_quantity: Dict[str, List[int]] = config.get("drop_quantity") or {
            "凡品": [2, 5], "黄品": [1, 4], "玄品": [1, 3], "圣品": [1, 2], "帝品": [1, 1],
        }
        self.category_quantity_scale: Dict[str, float] = config.get("category_quantity_scale") or {}
        self.category_weights: Dict[str, int] = config.get("category_weights") or {
            "灵植": 25, "矿材": 25, "灵液": 25, "妖兽": 25,
        }

        # 材料模型：整体系数 + 是否按路线等级封顶品阶
        mat_cfg = config.get("material_model") or {}
        try:
            self.material_chance_scale = float(
                mat_cfg.get("chance_scale", self.ITEM_CHANCE_SCALE)
            )
        except (TypeError, ValueError):
            self.material_chance_scale = self.ITEM_CHANCE_SCALE
        cap = mat_cfg.get("cap_by_route", self.MATERIAL_CAP_BY_ROUTE)
        if isinstance(cap, bool):
            self.material_cap_by_route = cap
        else:
            self.material_cap_by_route = str(cap).strip().lower() in ("1", "true", "yes")

        # 装备模型：与材料系数解耦（材料 0.33 不该把装备一起砍到 1/3），
        # 品阶同样按路线门槛封顶，避免 lv35 刷 lv0 路线白嫖混元先天。
        eq_cfg = config.get("equipment_model") or {}
        try:
            self.equipment_chance_scale = float(eq_cfg.get("chance_scale", 1.0))
        except (TypeError, ValueError):
            self.equipment_chance_scale = 1.0
        try:
            self.equipment_level_cap_bonus = int(eq_cfg.get("level_cap_bonus", 2))
        except (TypeError, ValueError):
            self.equipment_level_cap_bonus = 2

        # 修为模型：按突破需求折算，只吃丹药与符箓
        exp_cfg = config.get("exp_model") or {}
        try:
            self.exp_rate_per_hour = float(
                exp_cfg.get("base_rate_per_hour", self.BASE_RATE_PER_HOUR)
            )
        except (TypeError, ValueError):
            self.exp_rate_per_hour = self.BASE_RATE_PER_HOUR
        try:
            self.gold_multiplier = max(1.0, float(exp_cfg.get("gold_multiplier", self.GOLD_MULTIPLIER)))
        except (TypeError, ValueError):
            self.gold_multiplier = self.GOLD_MULTIPLIER

    def _load_config_file(self) -> dict:
        """加载配置文件并在失败时回退到默认配置"""
        if self.CONFIG_FILE.exists():
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info("已加载 adventure_config.json")
                    return data
            except Exception as exc:
                logger.error(f"加载 adventure_config.json 失败，将使用默认配置: {exc}")
        return self.DEFAULT_CONFIG

    def get_route_overview(self) -> List[dict]:
        """暴露给指令层的路线概览"""
        overview = []
        for route in self.routes.values():
            overview.append(
                {
                    "key": route["key"],
                    "name": route["name"],
                    "risk": route.get("risk", "未知"),
                    "duration": route.get("duration", 0),
                    "min_level": route.get("min_level", 0),
                    "description": route.get("description", ""),
                    "product": route.get("product", ""),
                }
            )
        return overview

    def route_product_names(self) -> Dict[str, str]:
        """路线主产物展示名，供列表与提示复用。"""
        return {
            route["key"]: str(route.get("product") or "全产物")
            for route in self.routes.values()
        }

    @staticmethod
    def _route_facet(route: dict) -> str:
        """路线产物归一成 5 个展示分类（配置里可能为空）。"""
        text = str(route.get("product") or "").strip()
        for facet in AdventureManager.LIST_FACETS:
            if facet != "全产物" and facet in text:
                return facet
        return "全产物"

    async def build_route_list(self, user_id: str = "", product: str = "") -> dict:
        """结构化历练列表：最新解锁的排在前面，未解锁折叠成一段。

        同一境界下所有路线的修为速度完全相同（只与时长成正比），路线之间真正的
        差别是产物、灵石与风险，所以排序不按修为，而按「境界门槛降序」——
        把玩家刚解锁、通常也最划算的路线放最上面，符合「快速定位到当前进度」
        的浏览习惯；想专攻某类材料则用产物筛选。
        """
        player = None
        if user_id:
            try:
                player = await self.db.get_player_by_id(str(user_id))
            except Exception:
                player = None
        player_level = int(getattr(player, "level_index", 0) or 0) if player else 0

        wanted = str(product or "").strip()
        rows: List[dict] = []
        for route in self.routes.values():
            facet = self._route_facet(route)
            if wanted and wanted not in ("全部", "推荐") and wanted != facet:
                continue
            duration = max(60, int(route.get("duration") or 1800))
            minutes = max(1, duration // 60)
            if player:
                pool = self._full_reward_pool(player, route)
                exp, gold = int(pool["exp"]), int(pool["gold"])
            else:
                exp, gold = 0, 0
            rows.append(
                {
                    "key": str(route.get("key") or ""),
                    "title": str(route.get("name") or ""),
                    "facet": facet,
                    "risk": str(route.get("risk") or "未知"),
                    "minutes": minutes,
                    "level": int(route.get("min_level") or 0),
                    "exp": exp,
                    "gold": gold,
                    "exp_per_hour": int(exp * 3600 / duration),
                    "gold_per_hour": int(gold * 3600 / duration),
                    "desc": str(route.get("description") or ""),
                    "cmd": f"/开始历练 {route.get('name') or ''}".strip(),
                    "rows": [
                        ["耗时", f"{minutes} 分钟"],
                        ["主产", facet],
                        ["风险", f"{route.get('risk') or '未知'}"],
                        ["修为", self._fmt_amount(exp)],
                        ["灵石", self._fmt_amount(gold)],
                    ],
                    "line": (
                        f"{route.get('name')}  {minutes}分  {facet}·{route.get('risk') or '?'}风险"
                        f"  修为{self._fmt_amount(exp)}"
                    ),
                }
            )

        # 门槛降序（新解锁优先），同门槛再按小时灵石兜底
        rows.sort(key=lambda r: (-r["level"], -r["gold_per_hour"], r["title"]))
        open_rows = [r for r in rows if r["level"] <= player_level]
        # 未解锁段反过来：离得最近的排前面，方便看「下一站是哪条」
        locked_rows = sorted(
            (r for r in rows if r["level"] > player_level),
            key=lambda r: (r["level"], -r["gold_per_hour"], r["title"]),
        )

        # 历练不标「最佳」：同一境界下修为同速，选哪条取决于想刷什么材料、
        # 愿意担多大风险，没有客观最优解。只用排序把新解锁的放前面。
        for i, row in enumerate(open_rows):
            row["badge"] = ""
            row["rank"] = i + 1

        # 未解锁的路线不给「出发」按钮：点了只会换一句「境界不足」，
        # 换成「需境界 N」的提示更直接。
        for row in locked_rows:
            row["locked"] = True
            row["lock_text"] = f"需境界 {row['level']}"

        # 推荐位按产物轮转：高境界路线常常清一色「极高风险 + 全产物」，
        # 直接取前 N 条等于没推荐，轮转后玩家一眼能选出想刷的材料。
        recommended: List[dict] = []
        try:
            from ..utils.list_card import pick_recommend
            recommended = pick_recommend(open_rows, self.LIST_FACETS, self.LIST_TOP_N)
            for row in recommended:
                row["recommend"] = True
        except Exception:
            recommended = open_rows[: self.LIST_TOP_N]

        facets = [{"key": "all", "label": "推荐", "count": len(recommended)}]
        facets.append({"key": "全部", "label": "全部", "count": len(open_rows)})
        for facet in self.LIST_FACETS:
            count = sum(1 for r in open_rows if r["facet"] == facet)
            if count:
                facets.append({"key": facet, "label": facet, "count": count})

        groups: List[dict] = []
        if open_rows:
            groups.append(
                {
                    "key": "open",
                    "name": "可出发 · 最新解锁优先",
                    "collapsed": False,
                    "items": open_rows,
                }
            )
        if locked_rows:
            groups.append(
                {
                    "key": "locked",
                    "name": f"未解锁 · {len(locked_rows)} 条（境界提升后开放）",
                    "collapsed": True,
                    "items": locked_rows,
                }
            )

        subtitle = f"可走 {len(open_rows)} 条 / 未解锁 {len(locked_rows)} 条"
        if player:
            subtitle = f"{self._level_name(player_level)} · {subtitle}"
        if wanted:
            subtitle = f"{wanted} · {subtitle}"

        return {
            "subtitle": subtitle,
            "facets": facets,
            "groups": groups,
            "total": len(rows),
            "open": len(open_rows),
            "filter": wanted,
        }

    def _level_name(self, level_index: int) -> str:
        """境界名（与秘境列表保持同一套称谓）。"""
        try:
            data = self.config_manager.get_level_data("灵修") if self.config_manager else []
        except Exception:
            data = []
        if data and 0 <= level_index < len(data):
            return str(data[level_index].get("level_name") or f"境界{level_index}")
        return f"境界{level_index}"

    @staticmethod
    def _fmt_amount(value) -> str:
        """修为/灵石量级：一万以下带千分位，以上折成「1.2 万」。"""
        try:
            v = int(round(float(value or 0)))
        except (TypeError, ValueError):
            return "0"
        if v < 10000:
            return f"{v:,}"
        wan = v / 10000.0
        return f"{wan:.0f} 万" if wan >= 10 else f"{wan:.1f} 万"

    # -------- 核心流程 --------

    async def start_adventure(self, user_id: str, route_token: str = "", umo: str = "") -> Tuple[bool, str]:
        """开始指定路线的历练"""
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！"

        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd:
            await self.db.ext.create_user_cd(user_id)
            user_cd = await self.db.ext.get_user_cd(user_id)

        if user_cd.type != UserStatus.IDLE:
            return False, f"❌ 你当前正{UserStatus.get_name(user_cd.type)}，无法开始历练！"

        # ===== v6.18 空参必须明选路线（不再静默进默认路线）=====
        # 路线按主产物分组，每组只列门槛最高的 4 条，避免 60 条铺满屏幕。
        if not route_token or not route_token.strip():
            available = [
                r for r in self.routes.values() if player.level_index >= r.get("min_level", 0)
            ]
            locked = sorted(
                [r for r in self.routes.values() if player.level_index < r.get("min_level", 0)],
                key=lambda r: r.get("min_level", 0)
            )
            band = self.realm_band(player.level_index)
            tier_text = " / ".join((band.get("tiers") or {}).keys()) if band else "凡品"

            lines = [
                "🗺️ 请选择历练路线",
                "━━━━━━━━━━━━━━━",
                f"你当前可得材料：{tier_text}（境界与路线等级共同决定品阶，路线决定类别）",
            ]
            groups: Dict[str, List[dict]] = {}
            for route in available:
                groups.setdefault(route.get("product") or "全产物", []).append(route)
            for product in ("灵植", "矿材", "灵液", "妖兽", "全产物"):
                items = groups.get(product) or []
                if not items:
                    continue
                items = sorted(items, key=lambda r: -r.get("min_level", 0))
                lines.append(f"▸ {product} · 共 {len(items)} 条")
                for route in items[:4]:
                    lines.append(
                        f"   {route['name']}  {route.get('duration', 0) // 60}分"
                        f" · {route.get('risk', '?')}风险"
                        + (f" · 需境界{route.get('min_level')}" if route.get('min_level') else "")
                    )
                if len(items) > 4:
                    lines.append(f"   …另有 {len(items) - 4} 条，/历练信息 看全部")
            if not available:
                lines.append("  （暂无可用路线，异常！）")
            if locked:
                lines.append("")
                lines.append("【即将解锁】")
                for route in locked[:2]:
                    lines.append(
                        f"  🔒 {route['name']}（需境界{route.get('min_level')}，"
                        f"还差 {route.get('min_level', 0) - player.level_index} 级）"
                    )
            lines.append("━━━━━━━━━━━━━━━")
            lines.append(f"💡 共 {len(available)} 条可走（全服共 {len(self.routes)} 条）")
            lines.append("💡 /开始历练 <路线名> 出发（如 /开始历练 灵谷采药）")
            return False, "\n".join(lines)

        route_key = self._resolve_route(route_token)
        route = self.routes.get(route_key)
        if not route:
            valid = "、".join(r["name"] for r in self.routes.values())
            return False, (
                f"❌ 未找到路线【{route_token}】。\n"
                f"可选路线：{valid}\n"
                f"💡 也可发送 /开始历练 查看路线详情"
            )

        if player.level_index < route.get("min_level", 0):
            return False, (
                f"❌ 境界不足，无法踏上【{route['name']}】！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"该路线需要境界等级 {route.get('min_level')}\n"
                f"你当前境界等级：{player.level_index}（还差 {route.get('min_level', 0) - player.level_index} 级）\n"
                f"💡 先在低风险路线积累修为，或 /闭关 提升"
            )

        cooldown_key = f"adv_cd_{user_id}_{route_key}"
        cooldown_end = 0
        cooldown_str = await self.db.ext.get_system_config(cooldown_key)
        if cooldown_str and cooldown_str.isdigit():
            cooldown_end = int(cooldown_str)
        now = int(time.time())
        if cooldown_end > now:
            remaining = cooldown_end - now
            minutes = remaining // 60 or 1
            return False, f"⚠️ 该路线尚在休整中，请 {minutes} 分钟后再试。"

        duration = int(route.get("duration", 1800) or 1800)
        now = int(time.time())
        scheduled_time = now + duration
        extra = {"route_key": route_key, "umo": umo or ""}
        await self.db.ext.set_user_busy(user_id, UserStatus.ADVENTURING, scheduled_time, extra_data=extra)

        band = self._material_band(player, route)
        tier_text = " / ".join((band.get("tiers") or {}).keys()) if band else "凡品"
        est = self._full_reward_pool(player, route)
        hint = [
            f"✨ 你选择了「{route['name']}」——{route.get('description', '未知冒险')}",
            f"路线风险：{route.get('risk', '未知')} | 历练时长：{duration // 60} 分钟",
            f"主要产出：{route.get('product') or '全产物'}（本路线可得 {tier_text}）",
            f"🧘 预计修为：约 +{int(est['exp']):,}（丹药/符箓加成已计入）",
            f"⏱️ 必须走完全程才能结算，到点自动发放；剩余 {duration // 60} 分钟",
            "🏳️ 中途可 /退出历练：本次没有奖励，也不收违约金、不加冷却",
        ]
        if route.get("min_level", 0):
            hint.append(f"建议境界：{route['min_level']} 阶以上")
        fatigue = int(route.get("fatigue_cooldown", 0) or 0)
        if fatigue:
            hint.append(f"（走完全程后该路线需休整 {fatigue // 60} 分钟；中途退出不休整）")

        return True, "\n".join(hint)

    # -------- 结算 --------

    def _read_extra(self, user_cd) -> dict:
        try:
            if hasattr(user_cd, "get_extra_data"):
                return user_cd.get_extra_data() or {}
            return json.loads(getattr(user_cd, "extra_data", "{}") or "{}")
        except Exception:
            return {}

    def breakthrough_need(self, player) -> int:
        """当前境界突破所需修为；lv0 或数据缺失时回退到下一级。"""
        try:
            level_index = max(0, int(getattr(player, "level_index", 0) or 0))
        except (TypeError, ValueError):
            level_index = 0
        level_data = []
        try:
            if self.config_manager:
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

    def _cultivation_mult(self, player) -> float:
        """历练修为倍率：只吃丹药与符箓，灵根/功法不参与。

        `calculate_pill_attribute_effects` 已经把丹药和符箓的 cultivation_multiplier
        合并并做上限（默认 3.0），这里直接用它的结果，避免两边各算一次。
        """
        mult = 1.0
        try:
            from ..core.pill_manager import PillManager

            if self.config_manager:
                effects = PillManager(
                    self.db, self.config_manager
                ).calculate_pill_attribute_effects(player)
                mult = float(effects.get("cultivation_speed", 1.0) or 1.0)
        except Exception:
            mult = 1.0
        try:
            bonus_data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            mult *= 1.0 + float(bonus_data.get("global_cultivation_multiplier", 0.0) or 0.0)
        except Exception:
            pass
        return max(0.0, mult)

    def _full_reward_pool(self, player, route) -> Dict[str, float]:
        """整趟历练的修为/灵石总量（不含事件倍率）。

        修为按本境界突破需求折算，与闭关共用同一把尺子：
            突破需求 × 0.75%/小时 × 时长(小时) × 丹药符箓倍率
        （0.75% = 闭关零加成 0.25% × 3，不再按满配反推，见 BASE_RATE_PER_HOUR 注释）
        """
        duration = max(60, int(route.get("duration") or 1800))
        need = self.breakthrough_need(player)
        exp = need * self.exp_rate_per_hour * (duration / 3600.0) * self._cultivation_mult(player)

        reward_duration = int(route.get("reward_duration") or duration)
        minutes = max(1, reward_duration // 60)
        level_index = max(0, int(getattr(player, "level_index", 0) or 0))
        gold = (
            minutes * route.get("base_gold_per_min", 10)
            + level_index * route.get("level_bonus_gold", 2)
        )
        gold += (route.get("completion_bonus") or {}).get("gold", 0)
        return {"exp": exp, "gold": gold * self.gold_multiplier}

    def _personal_reward_mult(self, player) -> float:
        """材料/灵石用的个人倍率（探索符 + 气运），不含丹药符箓的修炼加成。"""
        mult = 1.0
        try:
            from .talisman_manager import TalismanManager
            from .luck_manager import LuckManager

            mult += TalismanManager.get_active_bonus(player, "exploration_reward_multiplier")
            mult *= 1.0 + LuckManager.percent_bonus(
                player, per_point=0.3, cap=30.0, config_manager=self.config_manager
            ) / 100.0
        except Exception:
            pass
        return mult

    async def _apply_social_bonuses(self, user_id: str, player, rewards: Dict[str, int]) -> None:
        """道侣/称号/灵兽/功法生活加成。"""
        try:
            if self._social is None:
                from .social_manager import SocialManager

                self._social = SocialManager(self.db)
            comp_bonus = await self._social.companion_bonus_async(user_id, "adventure")
            if comp_bonus > 0:
                rewards["exp"] = int(rewards["exp"] * (1 + comp_bonus))
        except Exception:
            pass
        try:
            from .title_manager import TitleManager

            title_exp = TitleManager.get_exp_bonus(player)
            if title_exp > 0:
                rewards["exp"] = int(rewards["exp"] * (1 + title_exp))
        except Exception:
            pass
        try:
            from .pet_manager import PetManager

            gold_bonus = await PetManager(self.db).get_life_bonus(user_id, "gold_bonus")
            if gold_bonus > 0:
                rewards["gold"] = int(rewards["gold"] * (1 + gold_bonus))
        except Exception:
            pass
        try:
            from ..core.technique_bonus import TechniqueBonusManager

            rewards["gold"] = TechniqueBonusManager.apply_gold_bonus(
                player, rewards["gold"], self.config_manager
            )
        except Exception:
            pass

    async def _settle_once(
        self, user_id: str, player, user_cd
    ) -> Tuple[bool, str, Optional[Dict]]:
        """走完全程后的一次性结算：修为、灵石、材料、装备、宠物、符箓次数、休整冷却。"""
        extra = self._read_extra(user_cd)
        route = self.routes.get(extra.get("route_key") or self.default_route_key)
        if not route:
            return False, "❌ 未找到历练路线配置，请联系管理员。", None

        event = self._trigger_route_event(route)
        pet_encounter = None
        consumed_talismans: List[Tuple[str, int]] = []
        items: List[Tuple] = []

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(user_id)
            if not player:
                await self.db.conn.rollback()
                return False, "❌ 你还未踏入修仙之路！", None

            try:
                from .pet_manager import PetManager

                pet_encounter = await PetManager(self.db).try_exploration_encounter(
                    player, "历练途中", commit=False
                )
            except Exception:
                pet_encounter = None

            pool = self._full_reward_pool(player, route)
            rewards = {
                "exp": int(pool["exp"] * float(event.get("exp_mult", 1.0) or 1.0)),
                "gold": int(pool["gold"] * float(event.get("gold_mult", 1.0) or 1.0)),
            }
            await self._apply_social_bonuses(user_id, player, rewards)

            items = self._roll_drops(
                player,
                route,
                event,
                chance_scale=self.material_chance_scale,
                rolls_bonus=max(0, int(self.ITEM_ROLLS) - 1),
            )

            player.experience += int(rewards["exp"])
            player.gold += int(rewards["gold"])
            try:
                from .talisman_manager import TalismanManager

                consumed_talismans = TalismanManager.consume_run_charges(player, "adventure")
            except Exception:
                consumed_talismans = []

            await self.db.update_player(player, commit=False)
            await self.db.ext.set_user_free(user_id, commit=False)
            _, item_msg = await self._grant_drops(
                player, items, external_transaction=True
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        # 休整冷却只在走完时加；受伤整趟只追加一次 10 分钟
        fatigue = int(route.get("fatigue_cooldown", 0) or 0)
        if event.get("injury"):
            fatigue += 600
        if fatigue:
            cooldown_until = int(time.time()) + fatigue
            self._route_cooldowns.setdefault(user_id, {})[route["key"]] = cooldown_until
            await self.db.ext.set_system_config(
                f"adv_cd_{user_id}_{route['key']}", str(cooldown_until)
            )

        pet_msg = ""
        if pet_encounter:
            from .pet_manager import PetManager

            pet_msg = PetManager.format_exploration_encounter(pet_encounter)
        talisman_hint = ""
        if consumed_talismans:
            talisman_hint = "\n" + "\n".join(
                f"🧿 {name}：剩余 {left} 次历练/秘境" if left > 0 else f"🧿 {name}：次数已用尽"
                for name, left in consumed_talismans
            )
        fatigue_hint = f"\n⏳ 该路线休整：{fatigue // 60} 分钟" if fatigue else ""
        duration_minutes = max(1, int(route.get("duration") or 0) // 60)
        msg = (
            f"🎉 历练完成 · {route['name']}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{event['desc']}\n\n"
            f"全程 {duration_minutes} 分钟\n"
            f"🧘 修为 +{int(rewards['exp']):,} · 💰 灵石 +{int(rewards['gold']):,}"
            f"{item_msg}"
            f"{pet_msg}"
            f"{talisman_hint}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"当前修为：{player.experience:,}\n"
            f"当前灵石：{player.gold:,}"
            f"{fatigue_hint}"
        )
        return True, msg, {
            "route_key": route["key"],
            "route_name": route["name"],
            "event_key": event.get("key"),
            "event_desc": event.get("desc", ""),
            "exp_reward": int(rewards["exp"]),
            "gold_reward": int(rewards["gold"]),
            "total_exp": int(rewards["exp"]),
            "total_gold": int(rewards["gold"]),
            "items": items,
            "pet": pet_encounter,
            "duration": int(route.get("duration") or 0),
            "bounty_tag": route.get("bounty_tag", "adventure"),
            "bounty_progress": max(
                1, route.get("bounty_progress", 1) + event.get("bonus_progress", 0)
            ),
            "message": msg,
        }

    async def finish_adventure(self, user_id: str) -> Tuple[bool, str, Optional[Dict]]:
        """结算：必须走完全程；未到点只返回剩余时间。"""
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！", None

        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.ADVENTURING:
            return False, "❌ 你当前不在历练中！", None

        now = int(time.time())
        remain = int(user_cd.scheduled_time or 0) - now
        if remain > 0:
            extra = self._read_extra(user_cd)
            route = self.routes.get(extra.get("route_key") or self.default_route_key) or {}
            return False, (
                f"⏳ 「{route.get('name', '历练')}」还未走完\n"
                f"━━━━━━━━━━━━━━━\n"
                f"剩余 {remain // 60} 分 {remain % 60} 秒\n"
                "本次历练必须走完全程才能结算，到点会自动发放。\n"
                "💡 想提前离开可发送 /退出历练（本次无奖励，也不扣钱）。"
            ), None

        return await self._settle_once(user_id, player, user_cd)

    async def abandon_adventure(self, user_id: str) -> Tuple[bool, str]:
        """中途退出：走完全程按完成结算；否则放弃全部奖励，无违约金、不加冷却。"""
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！"

        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.ADVENTURING:
            return False, "❌ 你当前不在历练中！"

        now = int(time.time())
        if now >= int(user_cd.scheduled_time or 0):
            ok, msg, _ = await self._settle_once(user_id, player, user_cd)
            return ok, msg

        extra = self._read_extra(user_cd)
        route = self.routes.get(extra.get("route_key") or self.default_route_key) or {}
        elapsed = max(0, now - int(user_cd.create_time or now))
        remain = max(0, int(user_cd.scheduled_time or 0) - now)
        await self.db.ext.set_user_free(user_id)
        return True, (
            f"🏳️ 你结束了「{route.get('name', '历练')}」的历练\n"
            f"━━━━━━━━━━━━━━━\n"
            f"已走 {elapsed // 60} 分 {elapsed % 60} 秒，剩余 {remain // 60} 分 {remain % 60} 秒\n"
            "本次未走完全程，没有获得任何奖励。\n"
            "✅ 退出无违约金、不加休整冷却，可立即重新出发。"
        )

    async def check_adventure_status(self, user_id: str) -> Tuple[bool, str]:
        """查看历练进度"""
        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.ADVENTURING:
            return False, "❌ 你当前不在历练中！"

        now = int(time.time())
        extra = self._read_extra(user_cd)
        route = self.routes.get(extra.get("route_key") or self.default_route_key)
        if not route:
            return False, "❌ 未找到历练路线配置，请联系管理员。"

        elapsed = max(0, now - int(user_cd.create_time or now))
        total = int(route.get("duration") or 0)
        remain = max(0, int(user_cd.scheduled_time or 0) - now)
        if remain <= 0:
            return True, (
                f"✅ {route['name']} 已走完全程\n"
                "━━━━━━━━━━━━━━━\n"
                "结算即将发放；若长时间未收到可发送 /完成历练 补领。"
            )

        player = await self.db.get_player_by_id(user_id)
        est = self._full_reward_pool(player, route) if player else {"exp": 0, "gold": 0}
        return True, (
            f"📍 历练进度 · {route['name']}\n"
            "━━━━━━━━━━━━━━━\n"
            f"已走：{elapsed // 60} 分 {elapsed % 60} 秒 / 全程 {total // 60} 分钟\n"
            f"剩余：{remain // 60} 分 {remain % 60} 秒\n"
            f"预计奖励：修为 +{int(est['exp']):,} · 灵石 +{int(est['gold']):,}\n"
            "⏱️ 必须走完全程才能结算\n"
            "🏳️ /退出历练 可提前离开（无奖励、无惩罚）"
        )

    # -------- 内部工具 --------

    def _resolve_route(self, token: str) -> str:
        if not token:
            return self.default_route_key
        normalized = token.strip().lower()
        # 未知路线返回空值，由调用方报错；不要静默回退到默认路线
        return self.route_alias_index.get(normalized, "")

    def _trigger_route_event(self, route: dict) -> dict:
        weights = route.get("event_weights", {})
        if not weights:
            group_key = "standard"
        else:
            total_weight = sum(max(0, w) for w in weights.values()) or 1
            roll = random.randint(1, total_weight)
            upto = 0
            group_key = "standard"
            for key, weight in weights.items():
                upto += max(0, weight)
                if roll <= upto:
                    group_key = key
                    break

        group = self.event_groups.get(group_key) or self.event_groups.get("standard") or self.DEFAULT_CONFIG["event_groups"]["standard"]
        return random.choice(group)

    # -------- 掉落判定（境界段 × 路线主产物）--------

    def realm_band(self, level_index: int) -> dict:
        """按玩家境界取掉落段；超出配置范围时归入最后一段。"""
        if not self.realm_bands:
            return {}
        for band in self.realm_bands:
            try:
                if int(level_index) <= int(band.get("max_level", 999)):
                    return band
            except (TypeError, ValueError):
                continue
        return self.realm_bands[-1]

    def realm_band_label(self, level_index: int) -> str:
        return str(self.realm_band(level_index).get("label", "") or "")

    def _material_band(self, player, route: dict) -> dict:
        """材料判定用的境界段：玩家境界段与路线等级段取「低」的那一段。

        旧逻辑只看玩家境界段，于是 lv27 去刷 lv0 的「灵谷采药」（15 分钟、
        冷却 2 分钟）反而比刷 lv27「太初之巅」（28 分钟、冷却 18 分钟）材料
        产出高一倍 —— 高境界白嫖低阶路线的结算频率。现在路线等级也参与封顶：
        低阶路线无论玩家多高都只出低阶材料，高阶路线才配得上高阶池。
        """
        player_band = self.realm_band(getattr(player, "level_index", 0))
        if not self.material_cap_by_route or not self.realm_bands:
            return player_band
        try:
            route_level = int(route.get("min_level", 0) or 0)
        except (TypeError, ValueError):
            route_level = 0
        route_band = self.realm_band(route_level)
        if not route_band or route_band is player_band:
            return player_band
        order = [str(b.get("key") or "") for b in self.realm_bands]
        try:
            pi = order.index(str(player_band.get("key") or ""))
            ri = order.index(str(route_band.get("key") or ""))
        except ValueError:
            return player_band
        return self.realm_bands[min(pi, ri)]

    def _equipment_base_level(self, player, route: dict) -> int:
        """装备品阶基准：玩家等级与「路线 min_level + 余量」取低。

        旧逻辑是 ``max(玩家, 路线)``，只兜下限、没有上限，于是 lv35 刷 lv0
        的灵谷采药照样掉混元先天 —— 和秘境 v7.9.1 修掉的是同一个洞。
        取 min 之后，低阶路线只出低阶装备；正常按门槛推进的玩家不受影响。
        """
        player_level = int(getattr(player, "level_index", 0) or 0)
        try:
            bonus = int(getattr(self, "equipment_level_cap_bonus", 2))
        except (TypeError, ValueError):
            bonus = 2
        try:
            required = int(route.get("min_level", 0) or 0)
        except (TypeError, ValueError):
            return player_level
        return min(player_level, max(0, required + bonus))

    def _route_category(self, route: dict) -> str:
        """路线主产物；「全产物」或未标注时按全局权重抽一类。"""
        product = str(route.get("product") or "").strip()
        if product in self.PRODUCT_CATEGORIES:
            return product
        weights = {
            k: max(0, int(v))
            for k, v in (self.category_weights or {}).items()
            if k in self.PRODUCT_CATEGORIES
        }
        total = sum(weights.values())
        if not total:
            return "灵植"
        roll = random.randint(1, total)
        upto = 0
        for key, weight in weights.items():
            upto += weight
            if roll <= upto:
                return key
        return "灵植"

    def _roll_tier(self, band: dict) -> str:
        """在境界段允许的品阶里按权重抽一个。"""
        tiers = {
            k: max(0, int(v))
            for k, v in (band.get("tiers") or {}).items()
            if k in self.TIER_ORDER
        }
        total = sum(tiers.values())
        if not total:
            return self.TIER_ORDER[0]
        roll = random.randint(1, total)
        upto = 0
        for key, weight in tiers.items():
            upto += weight
            if roll <= upto:
                return key
        return list(tiers)[-1]

    def _resolve_pool(self, category: str, tier: str) -> Tuple[List[str], str]:
        """返回 (材料名列表, 实际使用品阶)。该类别缺少此品阶时向相邻品阶回退。"""
        pool = self.drop_pools.get(category) or {}
        names = pool.get(tier)
        if names:
            return names, tier
        if tier not in self.TIER_ORDER:
            return [], tier
        idx = self.TIER_ORDER.index(tier)
        for candidate in self.TIER_ORDER[idx + 1:]:
            if pool.get(candidate):
                return pool[candidate], candidate
        for candidate in reversed(self.TIER_ORDER[:idx]):
            if pool.get(candidate):
                return pool[candidate], candidate
        return [], tier

    def _roll_material_drop(self, player: Player, route: dict, event: dict) -> Optional[Tuple[str, int]]:
        """抽一件材料：境界段定品阶，路线主产物定类别。"""
        if not self.drop_pools or not self.realm_bands:
            # 老配置兜底：沿用 3 档固定掉落表
            tier_key = event.get("drop_tier") or route.get("drop_tier") or "low"
            drop_table = self.drop_tables.get(
                tier_key, self.DEFAULT_CONFIG["drop_tables"]["low"]
            )
            if not drop_table:
                return None
            total_weight = sum(item["weight"] for item in drop_table)
            roll = random.randint(1, max(1, total_weight))
            upto = 0
            chosen = drop_table[0]
            for item in drop_table:
                upto += item["weight"]
                if roll <= upto:
                    chosen = item
                    break
            return (chosen["name"], random.randint(chosen["min"], chosen["max"]))

        band = self._material_band(player, route)
        category = self._route_category(route)
        tier = self._roll_tier(band)
        names, actual_tier = self._resolve_pool(category, tier)
        if not names:
            return None
        name = random.choice(names)
        bounds = self.drop_quantity.get(actual_tier) or [1, 2]
        low, high = int(bounds[0]), int(bounds[1])
        scale = float(self.category_quantity_scale.get(category, 1.0) or 1.0)
        # 回退到更高品阶时按跳档数打折，抵消「捡到更贵材料」的额外收益
        if actual_tier in self.TIER_ORDER and tier in self.TIER_ORDER:
            steps = self.TIER_ORDER.index(actual_tier) - self.TIER_ORDER.index(tier)
            if steps > 0:
                scale *= self.FALLBACK_COUNT_SCALE ** steps
        # 小数部分按概率进位：直接 round() 会让 0.8 系数给出 1.5 个（期望 1.2），
        # 缩放等于没生效，四类专精的收益差距又回来了。
        raw = random.randint(low, max(low, high)) * scale
        count = int(raw)
        if random.random() < raw - count:
            count += 1
        # 数量系数 <1 的类别（灵液/妖兽）单价更高，必须允许本次掉 0 个，
        # 否则 max(1, count) 会把 0.5 系数直接抬回 1，专精线凭空多赚一倍。
        if count <= 0:
            return None
        return (name, count)

    def _roll_drops(
        self,
        player: Player,
        route: dict,
        event: dict,
        chance_scale: float = 1.0,
        rolls_bonus: int = 0,
    ) -> List[Tuple]:
        """抽一趟掉落（不落库）。

        chance_scale 用于整体缩放概率；rolls_bonus 追加额外判定次数
        （v7.7 一次性结算靠它维持分段版的整趟期望）。
        """
        dropped: List[Tuple] = []
        item_chance = float(event.get("item_chance", 40) or 0)
        try:
            from .talisman_manager import TalismanManager
            from .luck_manager import LuckManager

            item_chance += int(TalismanManager.get_active_bonus(player, "drop_chance_bonus"))
            item_chance += int(
                LuckManager.percent_bonus(
                    player, per_point=0.4, cap=20.0, config_manager=self.config_manager
                )
            )
        except Exception:
            pass
        item_chance = min(100.0, max(0.0, item_chance * max(0.0, chance_scale)))

        # 高风险事件靠「多判定一次」换回报，每次判定都按完整概率走；
        # 摊薄交给 chance_scale（按段折算），不要在这里再除一次 rolls。
        rolls = 1 + max(0, int(event.get("extra_rolls", 0) or 0)) + max(0, int(rolls_bonus or 0))
        for _ in range(rolls):
            if random.random() * 100.0 >= item_chance:
                continue
            drop = self._roll_material_drop(player, route, event)
            if drop:
                dropped.append(drop)

        # 历练额外机缘：低概率掉落功法，极小概率掉落随机五行心法
        try:
            from ..core.technique_bonus import TechniqueBonusManager

            tech_name = TechniqueBonusManager.roll_technique_drop(
                player,
                self.config_manager,
                base_chance=(4 + min(6, max(0, int(player.level_index) // 3)))
                * max(0.0, chance_scale),
                allow_main=True,
                main_ratio=0.08,
            )
            if tech_name:
                dropped.append((tech_name, 1))
        except Exception:
            pass

        # 独立装备掉落：与材料互不影响，单段最多 1 件
        try:
            from ..core.equipment_drop import roll_equipment_drop

            tier = event.get("drop_tier") or route.get("drop_tier") or "low"
            chance = float(self.equipment_drop_chances.get(tier, 0) or 0)
            # 装备用独立系数：材料系数 0.33 只该砍材料，v7.9.0 曾把它一路乘到
            # 装备上，导致配置写 5/8/12 实际只有 1.65/2.64/3.96。
            chance *= max(0.0, float(self.equipment_chance_scale))
            if chance > 0 and random.random() * 100.0 < chance:
                base_level = self._equipment_base_level(player, route)
                drop = roll_equipment_drop(
                    self.config_manager, base_level, self.equipment_slot_weights
                )
                if drop:
                    dropped.append((drop["name"], 1, drop["quality"]))
        except Exception:
            pass
        return dropped

    async def _grant_drops(
        self, player: Player, dropped_items: List[Tuple], external_transaction: bool = False
    ) -> Tuple[List[Tuple], str]:
        """把抽到的物品写进储物戒，并拼出展示文案。"""
        if not self.storage_ring_manager or not dropped_items:
            return dropped_items, ""

        item_lines = []
        for entry in dropped_items:
            item_name, qty, *rest = entry
            quality = rest[0] if rest else ""
            success, _ = await self.storage_ring_manager.grant_item(
                player, item_name, qty, silent=True,
                external_transaction=external_transaction, quality=quality,
            )
            if success:
                suffix = f"（{quality}）" if quality else ""
                item_lines.append(f"  · {item_name} x{qty}{suffix}")
            else:
                item_lines.append(f"  · {item_name} x{qty}（储物戒已满，丢失）")

        if item_lines:
            return dropped_items, "\n\n📦 获得物品：\n" + "\n".join(item_lines)
        return dropped_items, ""
