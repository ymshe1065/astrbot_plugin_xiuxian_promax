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

    # 分段结算：长路线拆成每 5 分钟一段，段内即时给奖，终段一次性大结算。
    # 总产出 = 旧版整趟 × reward_multiplier，不会因为拆段而缩水。
    TICK_SECONDS = 300
    TICK_SHARE = 0.6
    REWARD_MULTIPLIER = 2.0
    TICK_ITEM_MULT = 1.2
    FINAL_ITEM_MULT = 0.8

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

        # 分段结算参数
        tick_cfg = config.get("tick_settlement") or {}
        self.tick_seconds = max(60, int(tick_cfg.get("tick_seconds", self.TICK_SECONDS)))
        self.tick_share = min(0.9, max(0.1, float(tick_cfg.get("tick_share", self.TICK_SHARE))))
        self.reward_multiplier = max(1.0, float(tick_cfg.get("reward_multiplier", self.REWARD_MULTIPLIER)))
        self.tick_item_mult = max(0.0, float(tick_cfg.get("tick_item_chance_mult", self.TICK_ITEM_MULT)))
        self.final_item_mult = max(0.0, float(tick_cfg.get("final_item_chance_mult", self.FINAL_ITEM_MULT)))

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
                f"你当前可得材料：{tier_text}（境界决定品阶，路线决定类别）",
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

        duration = int(route.get("duration", 3600) or 3600)
        now = int(time.time())
        scheduled_time = now + duration
        ticks_total = self.tick_count(duration)
        extra = {
            "route_key": route_key,
            "umo": umo or "",
            "tick_seconds": self.tick_seconds,
            "ticks_total": ticks_total,
            "ticks_done": 0,
            "next_tick_at": now + self.tick_seconds,
            "injuries": 0,
            "earned_exp": 0,
            "earned_gold": 0,
        }
        await self.db.ext.set_user_busy(user_id, UserStatus.ADVENTURING, scheduled_time, extra_data=extra)

        band = self.realm_band(player.level_index)
        tier_text = " / ".join((band.get("tiers") or {}).keys()) if band else "凡品"
        hint = [
            f"✨ 你选择了「{route['name']}」——{route.get('description', '未知冒险')}",
            f"路线风险：{route.get('risk', '未知')} | 历练时长：{duration // 60} 分钟",
            f"主要产出：{route.get('product') or '全产物'}（本境界可得 {tier_text}）",
            f"⏱️ 每 {self.tick_seconds // 60} 分钟自动结算一次小结（共 {ticks_total - 1} 段），"
            f"走完还有一次大结算",
            "🏳️ 中途可随时 /退出历练：已结算的奖励全部保留，未走完的部分直接放弃，无任何惩罚",
        ]
        if route.get("min_level", 0):
            hint.append(f"建议境界：{route['min_level']} 阶以上")
        fatigue = int(route.get("fatigue_cooldown", 0) or 0)
        if fatigue:
            hint.append(f"（走完全程后该路线需休整 {fatigue // 60} 分钟；中途退出不休整）")

        return True, "\n".join(hint)

    # -------- 分段结算 --------

    def tick_count(self, duration: int) -> int:
        """整趟历练切成几段：前 n-1 段每 tick_seconds 结算，最后一段合并进大结算。"""
        duration = max(60, int(duration or 0))
        return max(2, int(round(duration / self.tick_seconds)))

    def _read_extra(self, user_cd) -> dict:
        try:
            if hasattr(user_cd, "get_extra_data"):
                return user_cd.get_extra_data() or {}
            return json.loads(getattr(user_cd, "extra_data", "{}") or "{}")
        except Exception:
            return {}

    def _ensure_tick_state(self, extra: dict, route: dict, user_cd) -> dict:
        """旧存档（升级前开始的一次性历练）按当前进度直接接管。"""
        extra = dict(extra or {})
        extra.setdefault("tick_seconds", self.tick_seconds)
        extra.setdefault("ticks_done", 0)
        extra.setdefault("injuries", 0)
        extra.setdefault("earned_exp", 0)
        extra.setdefault("earned_gold", 0)
        if not extra.get("ticks_total"):
            extra["ticks_total"] = self.tick_count(route.get("duration") or 3600)
            extra["next_tick_at"] = int(user_cd.create_time or 0) + int(extra["tick_seconds"])
        return extra

    def _full_reward_pool(self, player, route) -> Dict[str, float]:
        """整趟历练的修为/灵石总量（不含事件倍率），已乘总产出倍率。"""
        route_duration = max(60, int(route.get("duration") or 3600))
        reward_duration = int(route.get("reward_duration") or route_duration)
        minutes = max(1, reward_duration // 60)
        exp = (
            minutes * route.get("base_exp_per_min", 40)
            + player.level_index * route.get("level_bonus_exp", 10)
        )
        gold = (
            minutes * route.get("base_gold_per_min", 10)
            + player.level_index * route.get("level_bonus_gold", 2)
        )
        comp = route.get("completion_bonus") or {}
        exp += comp.get("exp", 0)
        gold += comp.get("gold", 0)
        return {"exp": exp * self.reward_multiplier, "gold": gold * self.reward_multiplier}

    def _personal_reward_mult(self, player) -> float:
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

    def segment_rewards(
        self, player, route, event: dict, share: float = None, segments: int = None
    ) -> Dict[str, int]:
        """一段历练的修为/灵石：整趟总量 × 该段占比 ÷ 小结段数。

        注意 segments 指「小结段数」，不包含最后的大结算段；大结算用 share=1-tick_share、segments=1。
        """
        segments = max(1, int(segments or (self.tick_count(route.get("duration") or 3600) - 1)))
        share = self.tick_share if share is None else float(share)
        pool = self._full_reward_pool(player, route)
        mult = self._personal_reward_mult(player)
        exp = pool["exp"] * share / segments * float(event.get("exp_mult", 1.0) or 1.0) * mult
        gold = pool["gold"] * share / segments * float(event.get("gold_mult", 1.0) or 1.0) * mult
        return {"exp": max(0, int(exp)), "gold": max(0, int(gold))}

    async def _apply_social_bonuses(self, user_id: str, player, rewards: Dict[str, int]) -> None:
        """道侣/称号/灵兽/功法加成：逐段生效，不集中在终段。"""
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

    # -------- 对外：分段结算入口 --------

    async def settle_due(self, user_id: str, now: int = None) -> Dict:
        """结算所有已到期的段落，走完则补一次大结算。

        返回 {"ok", "reason", "messages": [...], "ticks": n, "final": {...}|None}。
        调用方需自行持有该玩家的状态锁。
        """
        now = int(now if now is not None else time.time())
        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.ADVENTURING:
            return {"ok": False, "reason": "idle", "messages": [], "ticks": 0, "final": None}

        extra = self._read_extra(user_cd)
        route = self.routes.get(extra.get("route_key") or self.default_route_key)
        if not route:
            return {"ok": False, "reason": "no_route", "messages": [], "ticks": 0, "final": None}

        extra = self._ensure_tick_state(extra, route, user_cd)
        segments = max(2, int(extra["ticks_total"]))
        tick_seconds = max(60, int(extra.get("tick_seconds") or self.tick_seconds))
        settled = int(extra.get("ticks_done", 0) or 0)
        due = min(segments - 1, max(0, (now - int(user_cd.create_time or now)) // tick_seconds))

        result = {"ok": True, "reason": "", "messages": [], "ticks": 0, "final": None}
        if due > settled:
            msg = await self._settle_tick_batch(user_id, route, extra, due - settled, due)
            result["ticks"] = due - settled
            if msg:
                result["messages"].append(msg)

        if now >= int(user_cd.scheduled_time or 0):
            final = await self._settle_final(user_id, route, extra, user_cd, now)
            result["final"] = final
            if final and final.get("message"):
                result["messages"].append(final["message"])
        return result

    async def _settle_tick_batch(
        self, user_id: str, route: dict, extra: dict, count: int, settled_after: int
    ) -> str:
        """把 count 个小结合并成一个事务结算，避免多段补算时反复开事务。"""
        segments = max(2, int(extra.get("ticks_total") or 2))
        small_segments = max(1, segments - 1)
        total_exp = total_gold = 0
        items: List[Tuple] = []
        injuries = 0
        event = None

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(user_id)
            if not player:
                await self.db.conn.rollback()
                return ""
            for _ in range(max(1, int(count))):
                event = self._trigger_route_event(route)
                rewards = self.segment_rewards(
                    player, route, event, share=self.tick_share, segments=small_segments
                )
                total_exp += int(rewards["exp"])
                total_gold += int(rewards["gold"])
                items.extend(
                    self._roll_drops(
                        player,
                        route,
                        event,
                        chance_scale=self.tick_item_mult / small_segments,
                    )
                )
                if event.get("injury"):
                    injuries += 1

            # 道侣/称号/灵兽/功法加成按批结算一次，不逐段查库
            bonus = {"exp": total_exp, "gold": total_gold}
            await self._apply_social_bonuses(user_id, player, bonus)
            total_exp, total_gold = int(bonus["exp"]), int(bonus["gold"])

            player.experience += total_exp
            player.gold += total_gold
            extra["ticks_done"] = int(settled_after)
            extra["next_tick_at"] = int(extra.get("next_tick_at") or 0) + int(extra["tick_seconds"]) * int(count)
            extra["injuries"] = min(3, int(extra.get("injuries", 0) or 0) + injuries)
            extra["earned_exp"] = int(extra.get("earned_exp", 0) or 0) + total_exp
            extra["earned_gold"] = int(extra.get("earned_gold", 0) or 0) + total_gold
            extra["last_event"] = event.get("name", "") if event else ""

            await self.db.conn.execute(
                "UPDATE user_cd SET extra_data = ? WHERE user_id = ?",
                (json.dumps(extra, ensure_ascii=False), user_id),
            )
            await self.db.update_player(player, commit=False)
            _, item_msg = await self._grant_drops(
                player, items, external_transaction=True
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        if count > 1:
            head = f"⏱️ 历练小结 · {route['name']}（补算 {count} 段 → {settled_after}/{segments - 1} 段）"
        else:
            head = f"⏱️ 历练小结 · {route['name']}（{settled_after}/{segments - 1} 段）"
        lines = [
            head,
            "━━━━━━━━━━━━━━━",
            f"{event['desc'] if event else '一路无话。'}",
            f"本段：修为 +{total_exp:,} · 灵石 +{total_gold:,}{item_msg}",
            f"已累计：修为 +{int(extra.get('earned_exp', 0)):,} · 灵石 +{int(extra.get('earned_gold', 0)):,}",
        ]
        return "\n".join(lines)

    async def _settle_final(
        self, user_id: str, route: dict, extra: dict, user_cd, now: int
    ) -> Optional[Dict]:
        """终段大结算：终段收益 + 事件奖励，并结束历练、写入休整冷却。"""
        event = self._trigger_route_event(route)
        items: List[Tuple] = []
        pet_encounter = None
        consumed_talismans: List[Tuple[str, int]] = []

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(user_id)
            if not player:
                await self.db.conn.rollback()
                return None

            try:
                from .pet_manager import PetManager

                pet_encounter = await PetManager(self.db).try_exploration_encounter(
                    player, "历练途中", commit=False
                )
            except Exception:
                pet_encounter = None

            rewards = self.segment_rewards(
                player, route, event, share=1.0 - self.tick_share, segments=1
            )
            await self._apply_social_bonuses(user_id, player, rewards)
            items = self._roll_drops(
                player, route, event, chance_scale=self.final_item_mult
            )

            player.experience += int(rewards["exp"])
            player.gold += int(rewards["gold"])
            try:
                from .talisman_manager import TalismanManager

                consumed_talismans = TalismanManager.consume_run_charges(player, "adventure")
            except Exception:
                consumed_talismans = []

            extra["ticks_done"] = int(extra.get("ticks_total", 2)) - 1
            extra["earned_exp"] = int(extra.get("earned_exp", 0) or 0) + int(rewards["exp"])
            extra["earned_gold"] = int(extra.get("earned_gold", 0) or 0) + int(rewards["gold"])

            await self.db.update_player(player, commit=False)
            await self.db.ext.set_user_free(user_id, commit=False)
            _, item_msg = await self._grant_drops(
                player, items, external_transaction=True
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        fatigue = int(route.get("fatigue_cooldown", 0) or 0)
        if int(extra.get("injuries", 0) or 0) > 0:
            # 途中受过伤：整趟只加一次休整，不像逐段那样叠 10 倍
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

        segments = max(2, int(extra.get("ticks_total") or 2))
        total_exp = int(extra.get("earned_exp", 0) or 0)
        total_gold = int(extra.get("earned_gold", 0) or 0)
        duration_minutes = max(1, int(route.get("duration") or 0) // 60)
        fatigue_hint = f"\n⏳ 该路线休整：{fatigue // 60} 分钟" if fatigue else ""
        msg = (
            f"🎉 历练完成 · {route['name']}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{event['desc']}\n\n"
            f"全程 {duration_minutes} 分钟，共 {segments} 段（{segments - 1} 段小结 + 大结算）\n"
            f"🏆 大结算：修为 +{int(rewards['exp']):,} · 灵石 +{int(rewards['gold']):,}"
            f"{item_msg}"
            f"{pet_msg}"
            f"{talisman_hint}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"本趟合计：修为 +{total_exp:,} · 灵石 +{total_gold:,}\n"
            f"当前修为：{player.experience:,}\n"
            f"当前灵石：{player.gold:,}"
            f"{fatigue_hint}"
        )
        return {
            "route_key": route["key"],
            "route_name": route["name"],
            "event_key": event.get("key"),
            "event_desc": event["desc"],
            "exp_reward": int(rewards["exp"]),
            "gold_reward": int(rewards["gold"]),
            "total_exp": total_exp,
            "total_gold": total_gold,
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
        """手动结算：已到期的段落照常补算；走完则发大结算。"""
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！", None

        result = await self.settle_due(user_id)
        if result.get("final"):
            messages = result.get("messages") or []
            return True, "\n\n".join(messages) if messages else result["final"]["message"], result["final"]
        if not result.get("ok"):
            return False, "❌ 你当前不在历练中！", None

        user_cd = await self.db.ext.get_user_cd(user_id)
        extra = self._read_extra(user_cd) if user_cd else {}
        route = self.routes.get(extra.get("route_key") or self.default_route_key) or {}
        segments = max(2, int(extra.get("ticks_total", 2) or 2))
        done = int(extra.get("ticks_done", 0) or 0)
        remain = max(0, int(user_cd.scheduled_time or 0) - int(time.time())) if user_cd else 0
        return False, (
            f"⏳ 「{route.get('name', '历练')}」仍在进行中（{done}/{segments - 1} 段已结算）\n"
            f"剩余 {remain // 60} 分 {remain % 60} 秒\n"
            "小结会自动送达；走完全程后自动发放大结算。\n"
            "想提前离开可发送 /退出历练（已结算奖励保留）。"
        ), None

    async def abandon_adventure(self, user_id: str) -> Tuple[bool, str]:
        """中途退出：先补算到期段落，已拿到的全部保留，未走完部分直接放弃。"""
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！"

        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.ADVENTURING:
            return False, "❌ 你当前不在历练中！"

        result = await self.settle_due(user_id)
        if result.get("final"):
            # 刚好走到终点：按完成处理，不按退出
            return True, result["final"]["message"]

        user_cd = await self.db.ext.get_user_cd(user_id)
        extra = self._read_extra(user_cd) if user_cd else {}
        route = self.routes.get(extra.get("route_key") or self.default_route_key) or {}
        segments = max(2, int(extra.get("ticks_total", 2) or 2))
        done = int(extra.get("ticks_done", 0) or 0)
        earned_exp = int(extra.get("earned_exp", 0) or 0)
        earned_gold = int(extra.get("earned_gold", 0) or 0)

        await self.db.ext.set_user_free(user_id)
        head = "\n\n".join(result.get("messages") or [])
        body = (
            f"🏳️ 你结束了「{route.get('name', '历练')}」的历练\n"
            f"━━━━━━━━━━━━━━━\n"
            f"已走完 {done}/{segments - 1} 段小结\n"
            f"已到手：修为 +{earned_exp:,} · 灵石 +{earned_gold:,}\n"
            f"剩余 {segments - 1 - done} 段与最终大结算已放弃。\n"
            "✅ 中途退出无任何惩罚，已结算奖励全部保留。"
        )
        return True, (head + "\n\n" + body) if head else body

    async def check_adventure_status(self, user_id: str) -> Tuple[bool, str]:
        """查看分段进度"""
        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.ADVENTURING:
            return False, "❌ 你当前不在历练中！"

        now = int(time.time())
        extra = self._read_extra(user_cd)
        route = self.routes.get(extra.get("route_key") or self.default_route_key)
        route_name = route["name"] if route else "未知路线"
        if not route:
            return False, "❌ 未找到历练路线配置，请联系管理员。"

        extra = self._ensure_tick_state(extra, route, user_cd)
        segments = max(2, int(extra["ticks_total"]))
        tick_seconds = max(60, int(extra.get("tick_seconds") or self.tick_seconds))
        done = int(extra.get("ticks_done", 0) or 0)
        elapsed = max(0, now - int(user_cd.create_time or now))

        if now >= int(user_cd.scheduled_time or 0):
            return True, (
                f"✅ {route_name} 已走完全程\n"
                f"━━━━━━━━━━━━━━━\n"
                f"小结 {done}/{segments - 1} 段 · 已累计修为 +{int(extra.get('earned_exp', 0)):,} · "
                f"灵石 +{int(extra.get('earned_gold', 0)):,}\n"
                "🏆 大结算即将送达；若长时间未收到可发送 /完成历练 补领。"
            )

        next_in = max(0, tick_seconds - (elapsed % tick_seconds))
        remaining = int(user_cd.scheduled_time or 0) - now
        return True, (
            f"📍 历练进度 · {route_name}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"已历练：{elapsed // 60} 分钟（{done}/{segments - 1} 段已结算）\n"
            f"下一次小结：{next_in // 60}分{next_in % 60}秒后\n"
            f"全程剩余：{remaining // 60}分{remaining % 60}秒\n"
            f"已到手：修为 +{int(extra.get('earned_exp', 0)):,} · 灵石 +{int(extra.get('earned_gold', 0)):,}\n"
            "🏳️ 随时可 /退出历练，已结算奖励保留、无惩罚"
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

        band = self.realm_band(player.level_index)
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
        self, player: Player, route: dict, event: dict, chance_scale: float = 1.0
    ) -> List[Tuple]:
        """抽本段掉落（不落库）。chance_scale 把整趟概率摊到各段，避免分段后掉落翻十倍。"""
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
        rolls = 1 + max(0, int(event.get("extra_rolls", 0) or 0))
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
            chance *= max(0.0, chance_scale)
            if chance > 0 and random.random() * 100.0 < chance:
                base_level = max(
                    int(player.level_index or 0), int(route.get("min_level", 0) or 0)
                )
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
