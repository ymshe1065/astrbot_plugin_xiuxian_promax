# managers/rift_manager.py
"""
秘境系统管理器 - 处理秘境探索、奖励等逻辑
"""

import json
import random
import time
from typing import Tuple, List, Optional, Dict, TYPE_CHECKING
from astrbot.api import logger
from ..data.data_manager import DataBase
from ..data.default_configs import RIFT_CONFIG
from ..models_extended import Rift, UserStatus
from ..models import Player
from .task_penalty import calc_abandon_penalty, charge_abandon_penalty

if TYPE_CHECKING:
    from ..core import StorageRingManager


class RiftManager:
    """秘境系统管理器"""
    
    # 默认秘境探索时长（秒）
    DEFAULT_DURATION = 1800
    # 缺省配置（补齐旧配置文件缺失的新键）
    DEFAULT_CONFIG = RIFT_CONFIG
    
    # 旧版分档掉落表：仅在数据库里的秘境行缺少 drops 配置时兜底
    RIFT_DROP_TABLE = {
        1: [  # 低级秘境
            {"name": "灵草", "weight": 40, "min": 2, "max": 5},
            {"name": "精铁", "weight": 30, "min": 1, "max": 3},
            {"name": "灵石碎片", "weight": 30, "min": 3, "max": 8},
        ],
        2: [  # 中级秘境
            {"name": "灵草", "weight": 30, "min": 3, "max": 7},
            {"name": "玄铁", "weight": 25, "min": 2, "max": 4},
            {"name": "灵兽毛皮", "weight": 20, "min": 1, "max": 3},
            {"name": "功法残页", "weight": 15, "min": 1, "max": 1},
            {"name": "秘境精华", "weight": 10, "min": 1, "max": 2},
        ],
        3: [  # 高级秘境
            {"name": "玄铁", "weight": 25, "min": 3, "max": 6},
            {"name": "星辰石", "weight": 20, "min": 2, "max": 4},
            {"name": "灵兽内丹", "weight": 20, "min": 1, "max": 2},
            {"name": "功法残页", "weight": 20, "min": 1, "max": 2},
            {"name": "天材地宝", "weight": 15, "min": 1, "max": 1},
        ],
        4: [  # 玄冰地宫
            {"name": "星辰石", "weight": 25, "min": 4, "max": 8},
            {"name": "灵兽内丹", "weight": 20, "min": 2, "max": 4},
            {"name": "天材地宝", "weight": 20, "min": 1, "max": 2},
            {"name": "功法残页", "weight": 20, "min": 2, "max": 3},
            {"name": "玄铁", "weight": 15, "min": 4, "max": 8},
        ],
        5: [  # 上古遗迹
            {"name": "天材地宝", "weight": 25, "min": 2, "max": 3},
            {"name": "灵兽内丹", "weight": 22, "min": 2, "max": 5},
            {"name": "星辰石", "weight": 20, "min": 5, "max": 10},
            {"name": "功法残页", "weight": 20, "min": 2, "max": 4},
            {"name": "秘境精华", "weight": 13, "min": 3, "max": 6},
        ],
    }
    
    def __init__(self, db: DataBase, config_manager=None, storage_ring_manager: "StorageRingManager" = None):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_manager = storage_ring_manager
        # 旧存档的 rift_config.json 可能缺少新增键（如 equipment_up_rank_weight），
        # 这里用缺省配置补齐顶层键，避免读不到时掉率被当成 0 而完全不掉装备。
        merged = dict(self.DEFAULT_CONFIG)
        if config_manager and isinstance(config_manager.rift_config, dict):
            merged.update(config_manager.rift_config)
        self.config = merged
        self.explore_duration = self.config.get("default_duration", self.DEFAULT_DURATION)
        # 秘境定义（15 处 × 3 难度）：以配置文件为准，缺省回落到内置清单
        rifts_cfg = self.config.get("rifts")
        self.rifts: List[dict] = [
            r for r in (rifts_cfg or []) if isinstance(r, dict) and r.get("difficulties")
        ]
        # 秘境总数量（按秘境计，不含难度）
        self.group_count = len(self.rifts)

        # v7.7：秘境改成分段结算（每 5 分钟一段），修为/灵石打回旧版一半，主要做材料副本
        tick_cfg = self.config.get("tick_settlement") or {}
        try:
            self.tick_seconds = max(60, int(tick_cfg.get("tick_seconds", 300)))
        except (TypeError, ValueError):
            self.tick_seconds = 300
        try:
            self.tick_share = min(0.9, max(0.1, float(tick_cfg.get("tick_share", 0.6))))
        except (TypeError, ValueError):
            self.tick_share = 0.6
        try:
            self.tick_item_mult = max(0.0, float(tick_cfg.get("tick_item_chance_mult", 1.2)))
        except (TypeError, ValueError):
            self.tick_item_mult = 1.2
        try:
            self.final_item_mult = max(0.0, float(tick_cfg.get("final_item_chance_mult", 0.8)))
        except (TypeError, ValueError):
            self.final_item_mult = 0.8

    # ---------------------------------------------------------------
    # 秘境定义 → 数据库
    # ---------------------------------------------------------------
    def _iter_rift_rows(self):
        """把配置里的「秘境 × 难度」摊平成数据库行。

        rift_id 按「秘境序号 * 3 + 难度」生成（1..45），保证同一份配置
        每次生成的 ID 完全一致，存档里的 rift_id 才不会漂移。
        """
        for idx, rift in enumerate(self.rifts):
            key = str(rift.get("key") or f"rift{idx + 1}")
            name = str(rift.get("name") or f"秘境{idx + 1}")
            product = str(rift.get("product") or "")
            for diff in rift.get("difficulties") or []:
                try:
                    tier = int(diff.get("tier") or 1)
                except (TypeError, ValueError):
                    tier = 1
                rift_id = idx * 3 + tier
                rewards = {
                    "exp": list(diff.get("exp") or [0, 0]),
                    "gold": list(diff.get("gold") or [0, 0]),
                    "item_chance": int(diff["item_chance"] if diff.get("item_chance") is not None else 70),
                    "extra_drop_chance": int(diff.get("extra_drop_chance") or 0),
                    "equipment_chance": int(diff.get("equipment_chance") or 0),
                    "up_rank_weight": diff.get("up_rank_weight"),
                    "drops": list(diff.get("drops") or []),
                }
                yield {
                    "rift_id": rift_id,
                    "rift_name": name,
                    "rift_level": tier,
                    "required_level": int(diff.get("required_level") or 0),
                    "rewards": json.dumps(rewards, ensure_ascii=False),
                    "rift_key": key,
                    "product": product,
                    "difficulty": tier,
                    "duration": int(diff.get("duration") or self.DEFAULT_DURATION),
                }

    async def sync_rifts(self) -> int:
        """把配置里的秘境写入数据库（幂等）。

        - 已存在的 rift_id 覆盖更新（配置改了就生效）
        - 不在配置里的旧行直接删除（旧版 5 个秘境、或删掉的难度）
        探索中的玩家不受影响：结算时按 rift_id 取当前配置。
        """
        if not self.rifts:
            return 0
        rows = list(self._iter_rift_rows())
        if not rows:
            return 0
        valid_ids = tuple(r["rift_id"] for r in rows)
        try:
            await self.db.conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                await self.db.conn.execute(
                    """
                    INSERT INTO rifts (
                        rift_id, rift_name, rift_level, required_level, rewards,
                        rift_key, product, difficulty, duration
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(rift_id) DO UPDATE SET
                        rift_name = excluded.rift_name,
                        rift_level = excluded.rift_level,
                        required_level = excluded.required_level,
                        rewards = excluded.rewards,
                        rift_key = excluded.rift_key,
                        product = excluded.product,
                        difficulty = excluded.difficulty,
                        duration = excluded.duration
                    """,
                    (
                        row["rift_id"], row["rift_name"], row["rift_level"],
                        row["required_level"], row["rewards"], row["rift_key"],
                        row["product"], row["difficulty"], row["duration"],
                    ),
                )
            placeholders = ",".join("?" * len(valid_ids))
            await self.db.conn.execute(
                f"DELETE FROM rifts WHERE rift_id NOT IN ({placeholders})", valid_ids
            )
            await self.db.conn.commit()
        except Exception as e:
            await self.db.conn.rollback()
            logger.warning(f"【秘境】配置同步失败，沿用数据库现有数据：{e}")
            return 0
        return len(rows)

    def _rift_cfg(self, rift: Optional[Rift]) -> Dict:
        """读取秘境行里随难度一起下发的结算参数。"""
        if not rift:
            return {}
        try:
            return rift.get_rewards() or {}
        except Exception:
            return {}

    def _duration_of(self, rift: Optional[Rift]) -> int:
        if rift:
            try:
                d = int(rift.duration or 0)
                if d > 0:
                    return d
            except (TypeError, ValueError):
                pass
        return int(self.explore_duration or self.DEFAULT_DURATION)
    
    def _get_level_name(self, level_index: int) -> str:
        """获取境界名称"""
        if self.config_manager and hasattr(self.config_manager, 'level_data'):
            if 0 <= level_index < len(self.config_manager.level_data):
                return self.config_manager.level_data[level_index].get("level_name", f"境界{level_index}")
        # 默认境界名称
        level_names = ["炼气期一层", "炼气期二层", "炼气期三层", "炼气期四层", "炼气期五层",
                       "炼气期六层", "炼气期七层", "炼气期八层", "炼气期九层", "炼气期十层",
                       "筑基期初期", "筑基期中期", "筑基期后期", "金丹期初期", "金丹期中期", "金丹期后期"]
        if 0 <= level_index < len(level_names):
            return level_names[level_index]
        return f"境界{level_index}"
    
    async def list_rifts(self, user_id: str = None) -> Tuple[bool, str]:
        """
        列出所有秘境（按秘境分组，每个秘境下展开难度）

        Args:
            user_id: 传入时按玩家境界标注可进入状态

        Returns:
            (成功标志, 消息)
        """
        rifts = await self.db.ext.get_all_rifts()

        if not rifts:
            return False, "❌ 当前没有开放的秘境！"

        player_level = None
        if user_id:
            try:
                player = await self.db.get_player_by_id(str(user_id))
                if player:
                    player_level = int(player.level_index)
            except Exception:
                player_level = None

        groups: Dict[str, List[Rift]] = {}
        for rift in rifts:
            group_key = rift.rift_key or f"id{rift.rift_id}"
            groups.setdefault(group_key, []).append(rift)

        open_count = 0
        locked_count = 0
        msg = "🌀 秘境列表\n━━━━━━━━━━━━━━━\n"
        for group_key, items in groups.items():
            items.sort(key=lambda r: (r.difficulty or 1, r.rift_id))
            head = items[0]
            product = head.product or ""
            title = f"【{head.rift_name}】" + (f" 主产：{product}" if product else "")
            msg += title + "\n"
            for rift in items:
                exp_range = self._rift_cfg(rift).get("exp", [0, 0]) or [0, 0]
                minutes = max(1, self._duration_of(rift) // 60)
                if player_level is None:
                    mark = "·"
                    enterable = True
                elif player_level >= rift.required_level:
                    mark = "✅"
                    enterable = True
                    open_count += 1
                else:
                    mark = "🔒"
                    enterable = False
                    locked_count += 1
                # 未解锁的档只报门槛与耗时，省版面
                detail = (
                    f"{minutes}分 · 修为{exp_range[0]:,}~{exp_range[1]:,}"
                    if enterable else f"{minutes}分"
                )
                msg += (
                    f"  {mark} {rift.difficulty_label} (ID:{rift.rift_id}) "
                    f"需{self._get_level_name(rift.required_level)} · {detail}\n"
                )
            msg += "\n"

        if player_level is not None:
            msg += f"✅ 可进入 {open_count} 档 / 🔒 未解锁 {locked_count} 档\n"
        msg += "💡 /探索秘境 <ID> 进入（如：/探索秘境 2）；/秘境详情 <ID> 查看掉落"
        return True, msg

    async def find_rift(self, keyword: str, difficulty: str = "") -> Tuple[Optional[Rift], List[Rift]]:
        """按秘境名（可带难度）解析出唯一秘境行。

        Returns:
            (命中的秘境行, 候选列表)。命中唯一时第一项非空；否则第一项为 None，
            第二项给出候选供玩家选择。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return None, []
        all_rifts = await self.db.ext.get_all_rifts() or []
        matched = [
            r for r in all_rifts
            if keyword in (r.rift_name or "") or keyword in (r.display_name or "")
        ]
        if not matched:
            return None, []
        want = (difficulty or "").strip()
        if want:
            tier = None
            aliases = {"一": 1, "二": 2, "三": 3, "1": 1, "2": 2, "3": 3}
            key = want.replace("重", "").replace("层", "").strip()
            if key in aliases:
                tier = aliases[key]
            if tier is not None:
                narrowed = [r for r in matched if int(r.difficulty or 1) == tier]
                if narrowed:
                    matched = narrowed
        matched.sort(key=lambda r: (r.rift_id,))
        if len(matched) == 1:
            return matched[0], matched
        return None, matched

    async def rift_detail(self, rift_id: int) -> Tuple[bool, str]:
        """查看某个秘境难度的产出明细（含主产物与掉落表）"""
        rift = await self.db.ext.get_rift_by_id(int(rift_id))
        if not rift:
            return False, "❌ 秘境不存在！使用 /秘境列表 查看可用秘境"
        cfg = self._rift_cfg(rift)
        exp_range = cfg.get("exp", [0, 0]) or [0, 0]
        gold_range = cfg.get("gold", [0, 0]) or [0, 0]
        minutes = max(1, self._duration_of(rift) // 60)
        drops = [d for d in (cfg.get("drops") or []) if isinstance(d, dict)]

        lines = [
            f"🌀 {rift.display_name} (ID:{rift.rift_id})",
            "━━━━━━━━━━━━━━━",
            f"主产物：{rift.product or '混合'}",
            f"境界要求：{self._get_level_name(rift.required_level)}",
            f"探索时长：{minutes} 分钟",
            f"修为：{exp_range[0]:,}~{exp_range[1]:,}　灵石：{gold_range[0]:,}~{gold_range[1]:,}",
            f"掉落概率：{cfg.get('item_chance', 70)}%"
            f"（额外掉落 {cfg.get('extra_drop_chance', 0)}%"
            f"／装备 {cfg.get('equipment_chance', 0)}%）",
        ]
        if drops:
            total = sum(max(0, int(d.get("weight") or 0)) for d in drops) or 1
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("📦 可能掉落：")
            for d in sorted(drops, key=lambda x: -(int(x.get("weight") or 0))):
                pct = max(0, int(d.get("weight") or 0)) / total * 100
                lo, hi = d.get("min", 1), d.get("max", 1)
                cnt = f"x{lo}" if lo == hi else f"x{lo}~{hi}"
                lines.append(f"  · {d.get('name', '?')} {cnt}　{pct:.0f}%")
        return True, "\n".join(lines)

    async def enter_rift(
        self,
        user_id: str,
        rift_id: int,
        umo: str = "",
    ) -> Tuple[bool, str]:
        async with self.db.transaction():
            return await self._enter_rift(user_id, rift_id, umo)

    async def _enter_rift(self, user_id: str, rift_id: int, umo: str = "") -> Tuple[bool, str]:
        """
        进入秘境
        
        Args:
            user_id: 用户ID
            rift_id: 秘境ID
            
        Returns:
            (成功标志, 消息)
        """
        # 1. 检查用户
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！"
        
        # 2. 检查用户状态
        user_cd = await self.db.ext.get_user_cd(user_id)
        if user_cd and user_cd.type != UserStatus.IDLE:
            return False, f"❌ 你当前正{UserStatus.get_name(user_cd.type)}，无法探索秘境！"
        
        # 3. 检查秘境
        rift = await self.db.ext.get_rift_by_id(rift_id)
        if not rift:
            return False, "❌ 秘境不存在！使用 /秘境列表 查看可用秘境"
        
        # 4. 检查境界要求（含差距与可进入秘境清单）
        if player.level_index < rift.required_level:
            level_name = self._get_level_name(rift.required_level)
            all_rifts = await self.db.ext.get_all_rifts()
            enterable = [
                r for r in (all_rifts or [])
                if player.level_index >= r.required_level
            ]
            if enterable:
                enterable.sort(key=lambda r: -r.required_level)
                alt_text = "\n".join(
                    f"  · 【{r.display_name}】(ID:{r.rift_id} · 需境界{r.required_level})"
                    for r in enterable[:6]
                )
            else:
                alt_text = "  · 暂无（先提升境界吧）"
            return False, (
                f"❌ 境界不足，无法进入！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"【{rift.display_name}】需要达到【{level_name}】（境界等级 {rift.required_level}）\n"
                f"你当前境界等级：{player.level_index}（还差 {rift.required_level - player.level_index} 级）\n"
                f"💡 通过 /闭关 /历练 提升修为后 /突破 提高境界\n"
                f"━━━━━━━━━━━━━━━\n"
                f"✅ 你当前可进入的秘境：\n{alt_text}"
            )
        
        # 5. 设置探索状态，存储秘境ID（时长按难度取，同秘境不同深度耗时不同）
        duration = self._duration_of(rift)
        scheduled_time = int(time.time()) + duration
        # 进入时把整趟修为/灵石 roll 定，分段只按比例拆；umo 用于主动推送小结
        base_exp, base_gold = self._roll_base_rewards(rift)
        extra_data = {
            "rift_id": rift_id,
            "rift_level": rift.rift_level,
            "difficulty": rift.difficulty or 1,
            "umo": umo or "",
            "tick_seconds": self.tick_seconds,
            "ticks_total": self.tick_count(duration),
            "ticks_done": 0,
            "next_tick_at": scheduled_time - duration + self.tick_seconds,
            "base_exp": base_exp,
            "base_gold": base_gold,
            "earned_exp": 0,
            "earned_gold": 0,
        }
        await self.db.ext.set_user_busy(
            user_id, UserStatus.EXPLORING, scheduled_time, extra_data, commit=False
        )
        
        return True, (
            f"✨ 你进入了『{rift.display_name}』！探索需要 {max(1, duration // 60)} 分钟。\n"
            f"⏱️ 每 {self.tick_seconds // 60} 分钟自动结算一段，走完再发大结算\n"
            f"🏳️ 中途可 /退出秘境：已结算奖励保留，未走完部分放弃"
        )
    
    # -------- 分段结算（v7.7） --------

    def tick_count(self, duration: int) -> int:
        """整趟秘境切成几段：前 n-1 段每 tick_seconds 结算，最后一段合并进大结算。"""
        duration = max(60, int(duration or 0))
        return max(2, int(round(duration / self.tick_seconds)))

    def _read_extra(self, user_cd) -> dict:
        try:
            if hasattr(user_cd, "get_extra_data"):
                return user_cd.get_extra_data() or {}
            return json.loads(getattr(user_cd, "extra_data", "{}") or "{}")
        except Exception:
            return {}

    @staticmethod
    def _roll_base_rewards(rift: Optional[Rift]) -> Tuple[int, int]:
        """进入时把整趟修为/灵石 roll 定，分段只按比例拆，总和不因拆段而波动。"""
        if rift:
            cfg = rift.get_rewards()
            exp_range = cfg.get("exp") or [1000, 5000]
            gold_range = cfg.get("gold") or [500, 2000]
        else:
            exp_range, gold_range = [1000, 5000], [500, 2000]
        lo, hi = int(exp_range[0]), int(exp_range[1])
        g_lo, g_hi = int(gold_range[0]), int(gold_range[1])
        return (
            random.randint(min(lo, hi), max(lo, hi)),
            random.randint(min(g_lo, g_hi), max(g_lo, g_hi)),
        )

    def _ensure_tick_state(self, extra: dict, rift: Optional[Rift], user_cd) -> dict:
        """旧存档（v7.6 及以前进入的秘境）按已过时间直接接管。"""
        extra = dict(extra or {})
        duration = self._duration_of(rift)
        extra.setdefault("tick_seconds", self.tick_seconds)
        extra.setdefault("ticks_done", 0)
        extra.setdefault("earned_exp", 0)
        extra.setdefault("earned_gold", 0)
        if not extra.get("ticks_total"):
            extra["ticks_total"] = self.tick_count(duration)
            extra["next_tick_at"] = int(
                getattr(user_cd, "create_time", 0) or 0
            ) + int(self.tick_seconds)
        if not extra.get("base_exp"):
            exp, gold = self._roll_base_rewards(rift)
            extra["base_exp"], extra["base_gold"] = exp, gold
        return extra

    def _reward_mult(self, player) -> float:
        """修为/灵石的个人倍率（秘境探索符 + 气运），称号另算。"""
        mult = 1.0
        try:
            from .talisman_manager import TalismanManager
            from .luck_manager import LuckManager

            mult = 1.0 + TalismanManager.get_active_bonus(player, "rift_reward_multiplier")
            mult *= 1.0 + LuckManager.percent_bonus(
                player, per_point=0.3, cap=30.0, config_manager=self.config_manager
            ) / 100.0
        except Exception:
            pass
        return mult

    def _tick_rewards(self, extra: dict, share: float, segments: int) -> Tuple[int, int]:
        """一段秘境的修为/灵石：整趟基数 × 该段占比 ÷ 段数。"""
        segments = max(1, int(segments))
        exp = int(int(extra.get("base_exp") or 0) * float(share) / segments)
        gold = int(int(extra.get("base_gold") or 0) * float(share) / segments)
        return max(0, exp), max(0, gold)

    async def settle_due(self, user_id: str, now: int = None) -> Dict:
        """结算所有已到期的段落，走完则补一次大结算。

        返回 {"ok", "reason", "messages": [...], "ticks": n, "final": {...}|None}
        """
        now = int(now if now is not None else time.time())
        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.EXPLORING:
            return {"ok": False, "reason": "idle", "messages": [], "ticks": 0, "final": None}

        extra = self._read_extra(user_cd)
        rift_id = int(extra.get("rift_id", 0) or 0)
        rift = await self.db.ext.get_rift_by_id(rift_id) if rift_id else None
        extra = self._ensure_tick_state(extra, rift, user_cd)

        segments = max(2, int(extra["ticks_total"]))
        tick_seconds = max(60, int(extra.get("tick_seconds") or self.tick_seconds))
        settled = int(extra.get("ticks_done", 0) or 0)
        elapsed = max(0, now - int(getattr(user_cd, "create_time", now) or now))
        due = min(segments - 1, elapsed // tick_seconds)

        result = {"ok": True, "reason": "", "messages": [], "ticks": 0, "final": None}
        if due > settled:
            msg = await self._settle_tick_batch(
                user_id, rift, extra, due - settled, due, segments
            )
            result["ticks"] = due - settled
            if msg:
                result["messages"].append(msg)

        if now >= int(getattr(user_cd, "scheduled_time", 0) or 0):
            final = await self._settle_final(user_id, rift, extra, segments)
            result["final"] = final
            if final and final.get("message"):
                result["messages"].append(final["message"])
        return result

    async def _settle_tick_batch(
        self,
        user_id: str,
        rift: Optional[Rift],
        extra: dict,
        count: int,
        settled_after: int,
        segments: int,
    ) -> str:
        """把 count 个段落合并成一个事务结算。"""
        small_segments = max(1, segments - 1)
        rift_name = rift.display_name if rift else "未知秘境"
        total_exp = total_gold = 0
        items: List[Tuple] = []
        event = None

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(user_id)
            if not player:
                await self.db.conn.rollback()
                return ""

            mult = self._reward_mult(player)
            for _ in range(max(1, int(count))):
                event = self._pick_event()
                exp, gold = self._tick_rewards(extra, self.tick_share, small_segments)
                exp = int(exp * mult)
                gold = int(gold * mult)
                try:
                    from .title_manager import TitleManager

                    exp = int(exp * (1 + TitleManager.get_exp_bonus(player)))
                except Exception:
                    pass
                total_exp += exp
                total_gold += gold
                items.extend(
                    await self._roll_rift_drops(
                        player,
                        rift,
                        event["drop_mod"],
                        chance_scale=self.tick_item_mult / small_segments,
                    )
                )

            gold_bonus = self._gold_bonus_mult(player)
            total_gold = int(total_gold * gold_bonus)

            player.experience += total_exp
            player.gold += total_gold
            extra["ticks_done"] = int(settled_after)
            extra["next_tick_at"] = int(extra.get("next_tick_at") or 0) + int(
                extra.get("tick_seconds") or self.tick_seconds
            ) * int(count)
            extra["earned_exp"] = int(extra.get("earned_exp", 0) or 0) + total_exp
            extra["earned_gold"] = int(extra.get("earned_gold", 0) or 0) + total_gold

            # CAS：只有「当前已经结算到 prev_done 段」的那次调用能写入，
            # 避免后台分段任务与玩家手动结算并发时同一段被发两次。
            prev_done = int(settled_after) - int(count)
            cursor = await self.db.conn.execute(
                "UPDATE user_cd SET extra_data = ? "
                "WHERE user_id = ? AND type = ? "
                "AND COALESCE(json_extract(extra_data, '$.ticks_done'), 0) = ?",
                (
                    json.dumps(extra, ensure_ascii=False),
                    user_id,
                    int(UserStatus.EXPLORING),
                    prev_done,
                ),
            )
            if (cursor.rowcount or 0) == 0:
                await self.db.conn.rollback()
                return ""

            # 先发物品再落库：grant_item 走「外部事务」模式，
            # 只改 player 对象，由这里的 update_player 统一持久化。
            _, item_msg = await self._grant_rift_drops(player, items)
            await self.db.update_player(player, commit=False)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        if count > 1:
            head = f"⏱️ 秘境小结 · {rift_name}（补算 {count} 段 → {settled_after}/{small_segments}）"
        else:
            head = f"⏱️ 秘境小结 · {rift_name}（{settled_after}/{small_segments} 段）"
        return "\n".join(
            [
                head,
                "━━━━━━━━━━━━━━━",
                event["desc"] if event else "一路无话。",
                f"本段：修为 +{total_exp:,} · 灵石 +{total_gold:,}{item_msg}",
                f"已累计：修为 +{int(extra.get('earned_exp', 0)):,} · "
                f"灵石 +{int(extra.get('earned_gold', 0)):,}",
            ]
        )

    async def _settle_final(
        self,
        user_id: str,
        rift: Optional[Rift],
        extra: dict,
        segments: int,
    ) -> Optional[Dict]:
        """终段大结算：剩余 40% 收益 + 材料 + 宠物奇遇 + 符箓次数。"""
        rift_name = rift.display_name if rift else "未知秘境"
        event = self._pick_event()
        items: List[Tuple] = []
        pet_encounter = None
        consumed_talismans: List[Tuple[str, int]] = []

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(user_id)
            if not player:
                await self.db.conn.rollback()
                return None

            # CAS 认领：只有把状态从「秘境中」改成「空闲」的那一次调用才算完成，
            # 并发（手动 /秘境结算 + 后台定时任务）时后来者直接放弃，不会双发奖励。
            cursor = await self.db.conn.execute(
                "UPDATE user_cd SET type = ?, create_time = ?, scheduled_time = 0, extra_data = '{}' "
                "WHERE user_id = ? AND type = ?",
                (0, int(time.time()), user_id, int(UserStatus.EXPLORING)),
            )
            if (cursor.rowcount or 0) == 0:
                # 认领失败：本次没有写入任何东西，直接放手让认领成功的那次提交
                return None
            await self.db.conn.execute(
                "UPDATE players SET state = ? WHERE user_id = ?", ("空闲", user_id)
            )

            mult = self._reward_mult(player)
            exp, gold = self._tick_rewards(extra, 1.0 - self.tick_share, 1)
            exp = int(exp * mult)
            gold = int(gold * mult)
            try:
                from .title_manager import TitleManager

                exp = int(exp * (1 + TitleManager.get_exp_bonus(player)))
            except Exception:
                pass
            gold = int(gold * self._gold_bonus_mult(player))

            items = await self._roll_rift_drops(
                player, rift, event["drop_mod"], chance_scale=self.final_item_mult
            )

            player.experience += exp
            player.gold += gold
            try:
                from .talisman_manager import TalismanManager

                consumed_talismans = TalismanManager.consume_run_charges(player, "rift")
            except Exception:
                consumed_talismans = []

            try:
                from .pet_manager import PetManager

                pet_encounter = await PetManager(self.db).try_exploration_encounter(
                    player, "秘境探索中", commit=False
                )
            except Exception:
                pet_encounter = None

            # 先发物品再落库：grant_item 走「外部事务」模式，
            # 只改 player 对象，由这里的 update_player 统一持久化。
            _, item_msg = await self._grant_rift_drops(player, items)
            await self.db.update_player(player, commit=False)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

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

        total_exp = int(extra.get("earned_exp", 0) or 0) + int(exp)
        total_gold = int(extra.get("earned_gold", 0) or 0) + int(gold)
        msg = (
            f"🌀 秘境探索完成 · {rift_name}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{event['desc']}\n\n"
            f"全程 {max(1, self._duration_of(rift) // 60)} 分钟（{segments - 1} 段小结 + 大结算）\n"
            f"🏆 大结算：修为 +{int(exp):,} · 灵石 +{int(gold):,}"
            f"{item_msg}{pet_msg}{talisman_hint}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"本趟合计：修为 +{total_exp:,} · 灵石 +{total_gold:,}\n"
            f"当前修为：{player.experience:,}\n"
            f"当前灵石：{player.gold:,}"
        )
        return {
            "rift_name": rift_name,
            "exp": int(exp),
            "gold": int(gold),
            "total_exp": total_exp,
            "total_gold": total_gold,
            "event": event["desc"],
            "items": items,
            "pet": pet_encounter,
            "message": msg,
        }

    @staticmethod
    def _pick_event() -> Dict:
        """秘境小事件：只做小幅运气浮动，掉落基准概率由难度决定。"""
        return random.choice(
            [
                {"desc": "你发现了一处灵泉，修为大增！", "drop_mod": 6},
                {"desc": "你在秘境中击败了一只妖兽！", "drop_mod": 10},
                {"desc": "你找到了一个隐藏的宝箱！", "drop_mod": 15},
                {"desc": "你领悟了一些修炼心得。", "drop_mod": -8},
                {"desc": "你在秘境中遇到了前辈留下的传承！", "drop_mod": 8},
            ]
        )

    def _gold_bonus_mult(self, player) -> float:
        """灵石加成：功法生活加成 + 灵兽。"""
        mult = 1.0
        try:
            from ..core.technique_bonus import TechniqueBonusManager

            mult *= TechniqueBonusManager.gold_multiplier(player, self.config_manager)
        except Exception:
            pass
        return mult

    async def _grant_rift_drops(self, player, items: List[Tuple]) -> Tuple[List[Tuple], str]:
        """把掉落写进储物戒/丹药背包，并拼出展示文案（事务内调用）。"""
        if not items:
            return items, ""
        lines = []
        for entry in items:
            item_name, count, *rest = entry
            quality = rest[0] if rest else ""
            is_pill = self._is_pill_item(item_name)
            if not self.storage_ring_manager:
                lines.append(f"  · {item_name} x{count}（无法存储）")
                continue
            success, _ = await self.storage_ring_manager.grant_item(
                player,
                item_name,
                count,
                quality=quality,
                silent=True,
                external_transaction=True,
            )
            if success:
                suffix = "（丹药背包）" if is_pill else (f"（{quality}）" if quality else "")
                lines.append(f"  · {item_name} x{count}{suffix}")
            else:
                lines.append(f"  · {item_name} x{count}（储物戒已满，丢失）")
        if not lines:
            return items, ""
        return items, "\n\n📦 获得物品：\n" + "\n".join(lines)

    async def finish_exploration(
        self, user_id: str
    ) -> Tuple[bool, str, Optional[Dict]]:
        """手动结算：补齐到期段落；走完则发大结算，否则返回进度。"""
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！", None

        result = await self.settle_due(user_id)
        if result.get("final"):
            messages = result.get("messages") or []
            return (
                True,
                "\n\n".join(messages) if messages else result["final"]["message"],
                result["final"],
            )
        if not result.get("ok"):
            return False, "❌ 你当前不在探索秘境！", None

        user_cd = await self.db.ext.get_user_cd(user_id)
        extra = self._read_extra(user_cd) if user_cd else {}
        rift_id = int(extra.get("rift_id", 0) or 0)
        rift = await self.db.ext.get_rift_by_id(rift_id) if rift_id else None
        rift_name = rift.display_name if rift else "秘境"
        segments = max(2, int(extra.get("ticks_total") or 2))
        done = int(extra.get("ticks_done", 0) or 0)
        remain = max(0, int(getattr(user_cd, "scheduled_time", 0) or 0) - int(time.time()))
        return False, (
            f"⏳ 「{rift_name}」仍在探索中（{done}/{segments - 1} 段已结算）\n"
            f"剩余 {remain // 60} 分 {remain % 60} 秒\n"
            "小结会自动送达；走完全程后自动发放大结算。\n"
            "想提前离开可发送 /退出秘境（已结算奖励保留）。"
        ), None

    async def exit_rift(self, user_id: str) -> Tuple[bool, str]:
        """中途退出：先补齐已结算段落，未走完部分放弃，不再收违约金。"""
        return await self._exit_rift(user_id)

    async def _exit_rift(self, user_id: str) -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！"

        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.EXPLORING:
            return False, "❌ 你当前不在探索秘境！"

        result = await self.settle_due(user_id)
        if result.get("final"):
            # 刚好走到终点：按完成处理
            return True, result["final"]["message"]

        user_cd = await self.db.ext.get_user_cd(user_id)
        extra = self._read_extra(user_cd) if user_cd else {}
        rift_id = int(extra.get("rift_id", 0) or 0)
        rift = await self.db.ext.get_rift_by_id(rift_id) if rift_id else None
        rift_name = rift.display_name if rift else "秘境"
        segments = max(2, int(extra.get("ticks_total") or 2))
        done = int(extra.get("ticks_done", 0) or 0)
        earned_exp = int(extra.get("earned_exp", 0) or 0)
        earned_gold = int(extra.get("earned_gold", 0) or 0)

        await self.db.ext.set_user_free(user_id)
        head = "\n\n".join(result.get("messages") or [])
        body = (
            f"🏳️ 你退出了「{rift_name}」\n"
            f"━━━━━━━━━━━━━━━\n"
            f"已走完 {done}/{segments - 1} 段小结\n"
            f"已到手：修为 +{earned_exp:,} · 灵石 +{earned_gold:,}\n"
            f"剩余 {segments - 1 - done} 段与最终大结算已放弃。\n"
            "✅ 退出无违约金、不额外扣费，已结算奖励全部保留。"
        )
        return True, (head + "\n\n" + body) if head else body

    def _is_pill_item(self, item_name: str) -> bool:
        """检查物品是否为丹药"""
        if self.config_manager and hasattr(self.config_manager, 'is_pill'):
            return self.config_manager.is_pill(item_name)
        return False
    
    def _rift_power(self, rift: Optional[Rift], player: Optional[Player] = None) -> int:
        """秘境强度（1~10）：决定功法残页等附加掉落的基础概率。

        由难度的境界门槛换算，合体期跑混沌天渊拿到的功法品质/概率
        才不会和炼气期跑青云秘境一样。
        """
        required = 0
        if rift:
            try:
                required = int(rift.required_level or 0)
            except (TypeError, ValueError):
                required = 0
        if rift is None and player is not None:
            required = int(getattr(player, "level_index", 0) or 0)
        return max(1, min(10, 1 + required // 4))
    
    async def _roll_rift_drops(
        self,
        player: Player,
        rift: Optional[Rift],
        drop_mod: int = 0,
        chance_scale: float = 1.0,
    ) -> List[Tuple]:
        """
        按秘境难度结算掉落

        Args:
            player: 玩家对象
            rift: 秘境行（携带该难度的掉落表与各档概率）
            drop_mod: 随机事件带来的概率浮动（百分点）

        Returns:
            掉落物品列表 [(物品名, 数量[, 品质]), ...]；装备条目含品质
        """
        dropped_items: List[Tuple] = []
        cfg = self._rift_cfg(rift)
        tier = int(getattr(rift, "difficulty", 1) or 1) if rift else 1
        power = self._rift_power(rift, player)

        # 掉落表：优先用难度自带配置，旧行无 drops 时回落到内置分档表
        drop_table = [d for d in (cfg.get("drops") or []) if isinstance(d, dict) and d.get("name")]
        if not drop_table:
            drop_table = self.RIFT_DROP_TABLE.get(tier, self.RIFT_DROP_TABLE[1])

        base_chance = int(cfg["item_chance"] if cfg.get("item_chance") is not None else 70)
        extra_chance = int(cfg.get("extra_drop_chance") or 0)
        equipment_chance = cfg.get("equipment_chance")

        item_chance = base_chance + int(drop_mod or 0)
        try:
            from .talisman_manager import TalismanManager
            from .luck_manager import LuckManager
            item_chance += int(TalismanManager.get_active_bonus(player, "drop_chance_bonus"))
            item_chance += int(LuckManager.percent_bonus(
                player, per_point=0.4, cap=20.0, config_manager=self.config_manager
            ))
        except Exception:
            pass
        # 分段结算按 chance_scale 摊薄，整趟期望不变
        # 额外掉落是「主掉落成功后的条件概率」，不跟着摊薄：
        # 主掉落按 s 摊、extra 保持原值，整趟期望才等于 主×(1+extra)。
        item_chance = min(100.0, max(0.0, item_chance * max(0.0, float(chance_scale))))

        def _pick() -> Optional[Tuple[str, int]]:
            total_weight = sum(max(0, int(i.get("weight") or 0)) for i in drop_table)
            if total_weight <= 0:
                return None
            roll = random.randint(1, total_weight)
            cursor = 0
            for item in drop_table:
                cursor += max(0, int(item.get("weight") or 0))
                if roll <= cursor:
                    lo = int(item.get("min", 1) or 1)
                    hi = int(item.get("max", lo) or lo)
                    return item["name"], random.randint(min(lo, hi), max(lo, hi))
            return None

        # 主掉落
        if random.random() * 100.0 <= item_chance:
            picked = _pick()
            if picked:
                dropped_items.append(picked)
            # 额外掉一件：概率来自难度配置（一重为 0，二重 45%，三重 70%）
            if extra_chance > 0 and random.random() * 100.0 <= extra_chance:
                picked = _pick()
                if picked:
                    dropped_items.append(picked)

        # 秘境传承机缘：越深的秘境更容易出功法/心法
        try:
            from ..core.technique_bonus import TechniqueBonusManager
            tech_name = TechniqueBonusManager.roll_technique_drop(
                player,
                self.config_manager,
                base_chance=(4 + power * 2) * max(0.0, float(chance_scale)),
                allow_main=True,
                main_ratio=0.10,
            )
            if tech_name:
                dropped_items.append((tech_name, 1))
        except Exception:
            pass

        # 独立装备掉落：一次最多 1 件，掉落实例时 roll 品质
        try:
            from ..core.equipment_drop import roll_equipment_drop
            chance = equipment_chance
            if chance is None:
                # 旧行没有 equipment_chance，回落到底层按档位配置
                chances = self.config.get("equipment_drop_chances") or {}
                chance = chances.get(str(tier), chances.get(tier, 0))
            chance = float(chance or 0) * max(0.0, float(chance_scale))
            if chance > 0 and random.random() * 100.0 <= chance:
                drop = roll_equipment_drop(
                    self.config_manager,
                    player.level_index,
                    self.config.get("equipment_slot_weights"),
                    up_rank_weight=self._equipment_up_rank_weight(rift, tier),
                )
                if drop:
                    dropped_items.append((drop["name"], 1, drop["quality"]))
        except Exception as e:
            if self.config_manager:
                logger.warning(f"秘境装备掉落判定失败：{e}")

        return dropped_items

    def _equipment_up_rank_weight(self, rift: Optional[Rift] = None, tier: int = None) -> float:
        """越阶（基准 +1）装备权重。

        取值优先级：难度自带的 ``up_rank_weight`` > 配置里的按档映射/标量 > 缺省 1.0。
        支持两种写法：
        - 映射：``{"1": 1.0, "2": 0.7, "3": 0.5}``
        - 标量：``0.25``（所有秘境统一）
        """
        from ..core.equipment_drop import DEFAULT_UP_RANK_WEIGHT
        # 兼容旧调用：_equipment_up_rank_weight(3) 传入的是档位而不是秘境对象
        if isinstance(rift, (int, str)) and not isinstance(rift, bool):
            if tier is None:
                tier, rift = int(rift), None
        tier = int(tier or 1)
        cfg = self._rift_cfg(rift)
        raw = cfg.get("up_rank_weight")
        if raw is None:
            raw = self.config.get("equipment_up_rank_weight", DEFAULT_UP_RANK_WEIGHT)
        if isinstance(raw, dict):
            raw = raw.get(str(tier), raw.get(tier, DEFAULT_UP_RANK_WEIGHT))
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return DEFAULT_UP_RANK_WEIGHT

