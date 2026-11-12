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
from ..rank_rules import RANK_LEVELS
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

        # 单趟材料价值缓存（配置静态、掉落表静态，按 rift_id 缓存即可）
        self._value_cache: Dict[int, float] = {}

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
                    # 深档强度：缺省 1.0 / 1.0 / false，老存档读不到时按原行为跑
                    "tier_yield": float(diff.get("tier_yield") or 1.0),
                    "high_tier_weight_mult": float(diff.get("high_tier_weight_mult") or 1.0),
                    "gear_lock": bool(diff.get("gear_lock")),
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

    def _level_label(self, level_index: int) -> str:
        """「合体期初期·25级」：解锁境界名 + 境界等级，列表里标明门槛用。

        玩家看「需合体期初期」不知道自己还差几级，看「需 25 级」不知道是哪个
        境界的中/后期，所以两者一起给。
        """
        from ..core.realm_rules import level_label

        data = []
        if self.config_manager and hasattr(self.config_manager, "level_data"):
            data = self.config_manager.level_data or []
        return level_label(data, level_index)
    
    # -------- 列表数据（卡片 / 网页 / 兜底文本共用）--------

    # 可进入段默认只推最划算的前若干档，其余靠筛选或「全部」展开
    LIST_TOP_N = 8
    # ★ 评级按境界段（每 10 级）内的相对价值分档：跨段比绝对数值没有意义
    VALUE_BAND_SIZE = 10
    # 展示用产物分类（配置里的 "灵液+矿材" 这类复合值按包含关系归入对应类）
    PRODUCT_FACETS = ("灵植", "矿材", "灵液", "妖兽", "全产物")

    def _material_value(self, rift: Optional[Rift]) -> float:
        """单趟期望材料价值（按目录价折成灵石等值）。

        期望 = Σ(权重占比 × 平均数量 × 单价) × 主掉落概率 × (1 + 额外掉落概率)
        额外掉落是条件概率（不参与分段摊薄），所以整趟期望直接相加。
        结果既是排序依据，也是 ★ 评级的输入。
        """
        if not rift or not self.config_manager:
            return 0.0
        try:
            rid = int(rift.rift_id)
        except (TypeError, ValueError):
            rid = None
        if rid is not None and rid in self._value_cache:
            return self._value_cache[rid]

        cfg = self._rift_cfg(rift)
        drops = cfg.get("drops") or []
        value = 0.0
        # 与真实掉落同口径：深档的数量乘 tier_yield、权重偏向最高品阶，
        # 否则三重的单趟价值会被低估，排序和 ★ 反而把它排到二重后面。
        try:
            tier_yield = max(0.0, float(cfg.get("tier_yield") or 1.0))
        except (TypeError, ValueError):
            tier_yield = 1.0
        try:
            high_mult = max(0.0, float(cfg.get("high_tier_weight_mult") or 1.0))
        except (TypeError, ValueError):
            high_mult = 1.0
        weights = self._rift_drop_weights(drops, high_mult)
        total_w = sum(weights)
        if total_w > 0:
            for d, weight in zip(drops, weights):
                try:
                    w = float(weight)
                    lo = float(d.get("min", 1) or 1)
                    hi = float(d.get("max", lo) or lo)
                except (TypeError, ValueError):
                    continue
                try:
                    name = str(d.get("name", ""))
                    # 掉落表里混了丹药等非材料赠品（五品渡劫增益丹单件 30 万），
                    # 计进「材料价值」会把排序彻底带偏，所以只认 material_catalog。
                    if not self.config_manager.get_material_info(name):
                        continue
                    price = float(self.config_manager.get_item_price(name) or 0)
                except Exception:
                    continue
                value += (w / total_w) * ((lo + hi) / 2.0) * price
        try:
            chance = float(cfg.get("item_chance", 0) or 0) / 100.0
            extra = 1.0 + float(cfg.get("extra_drop_chance", 0) or 0) / 100.0
        except (TypeError, ValueError):
            chance, extra = 0.0, 1.0
        value = value * max(0.0, chance) * max(1.0, extra) * max(0.0, tier_yield)

        if rid is not None:
            self._value_cache[rid] = value
        return value

    def _value_peak_by_band(self, rifts: List[Rift]) -> Dict[int, float]:
        """每个境界段内最高的单趟材料价值，作为该段 ★ 的 100% 基准。"""
        peaks: Dict[int, float] = {}
        for rift in rifts:
            band = int(rift.required_level or 0) // self.VALUE_BAND_SIZE
            peaks[band] = max(peaks.get(band, 0.0), self._material_value(rift))
        return peaks

    @staticmethod
    def _stars(value: float, peak: float) -> int:
        """★ 相对「当前可进入段最高单趟价值」评级。

        基准必须跟着玩家境界走：如果按境界段各算各的基准，列表里会出现
        「第一名 1 星、最后一名的老秘境 3 星」，反而误导。用可进入段的
        最高值当 100%，列表第一条永远是 ★★★，往下依次递减。
        """
        if peak <= 0:
            return 0
        ratio = value / peak
        if ratio >= 0.85:
            return 3
        if ratio >= 0.55:
            return 2
        if ratio >= 0.30:
            return 1
        return 0

    @staticmethod
    def _star_text(stars: int) -> str:
        stars = max(0, min(3, int(stars or 0)))
        return "★" * stars + "☆" * (3 - stars)

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

    @classmethod
    def _facet_of(cls, product: str) -> str:
        """把配置里的主产物值归到 5 个展示分类之一。"""
        text = str(product or "").strip()
        for facet in cls.PRODUCT_FACETS:
            if facet != "全产物" and facet in text:
                return facet
        return "全产物"

    def _decorate_rift_row(self, row: dict, stars: int) -> None:
        """把 ★ 与展示文案补进条目（星级要等分组后才知道基准，故分两步）。"""
        star_text = self._star_text(stars)
        row["stars"] = stars
        row["star_text"] = star_text
        level_text = row.get("level_text") or self._level_label(row["required_level"])
        row["level_text"] = level_text
        row["rows"] = [
            ["解锁", level_text],
            ["耗时", f"{row['minutes']} 分钟"],
            ["主产", f"{row['facet']} {star_text}"],
            ["修为", self._fmt_amount(row["exp"])],
            ["灵石", self._fmt_amount(row["gold"])],
        ]
        if row.get("gate_text"):
            # 网页行里明确写出门槛：图片行只有一个 🔒，说不出要什么品阶
            row["rows"].insert(1, ["门槛", row["gate_text"]])
        # 图片版一档一行：名字+档位 解锁境界 时长 产物★
        # 修为不进图片行：它是象征量（详情与网页 rows 里都有），塞进来会把行挤到
        # 换行，出现「修为5,625」被拆成「修为5,」「625」的难看断行。
        gate_need = row.get("gate_need") or ""
        if gate_need:
            # 深档要报门槛：三重固定 45 分钟，把分钟让位给「需 X 套」，
            # 否则一行塞不下会折行，8 行里折三行的卡片比不放更难看。
            row["line"] = (
                f"{row['title']} {level_text} {row['facet']}{star_text} 需{gate_need}套"
            )
        else:
            row["line"] = (
                f"{row['title']} {level_text}  {row['minutes']}分 {row['facet']}{star_text}"
            )
        if row.get("gate_ok") is False:
            row["line"] = f"🔒{row['line']}"

    async def build_rift_list(self, user_id: str = None, product: str = "") -> dict:
        """结构化秘境列表：分组 + 排序 + ★ 评级 + 每行可直接进入的指令。

        排序依据是「单趟期望材料价值 ÷ 分钟」，即秘境作为材料副本的真实效率；
        修为已经压到象征量，不再参与排序。
        """
        rifts = await self.db.ext.get_all_rifts()
        if not rifts:
            return {}

        player_level = None
        player = None
        if user_id:
            try:
                player = await self.db.get_player_by_id(str(user_id))
                if player:
                    player_level = int(player.level_index)
            except Exception:
                player = None
                player_level = None

        wanted = str(product or "").strip()

        rows: List[dict] = []
        for rift in rifts:
            minutes = max(1, self._duration_of(rift) // 60)
            value = self._material_value(rift)
            cfg = self._rift_cfg(rift)
            exp_range = cfg.get("exp") or [0, 0]
            gold_range = cfg.get("gold") or [0, 0]
            try:
                exp_mid = (float(exp_range[0]) + float(exp_range[-1])) / 2.0
            except (TypeError, ValueError, IndexError):
                exp_mid = 0.0
            try:
                gold_mid = (float(gold_range[0]) + float(gold_range[-1])) / 2.0
            except (TypeError, ValueError, IndexError):
                gold_mid = 0.0
            rows.append(
                {
                    "id": int(rift.rift_id),
                    "name": str(rift.rift_name or ""),
                    "tier": str(rift.difficulty_label or ""),
                    "title": str(rift.display_name or f"{rift.rift_name} {rift.difficulty_label}"),
                    "product": str(rift.product or ""),
                    "facet": self._facet_of(rift.product),
                    "required_level": int(rift.required_level or 0),
                    "level_name": self._get_level_name(int(rift.required_level or 0)),
                    "level_text": self._level_label(int(rift.required_level or 0)),
                    "minutes": minutes,
                    "value": round(value, 1),
                    "value_per_min": round(value / minutes, 2),
                    "exp": int(exp_mid),
                    "gold": int(gold_mid),
                    # 深档门槛：三重要求该门槛境界可装备的最高品阶全套
                    "gear_lock": bool(cfg.get("gear_lock")),
                    "gate_need": (
                        self._gear_need_name(int(rift.required_level or 0))
                        if cfg.get("gear_lock")
                        else ""
                    ),
                    "cmd": f"/探索秘境 {int(rift.rift_id)}",
                }
            )

        # 筛选：复合主产物（如「灵液+矿材」）按包含关系归属，同时保留全产物类
        if wanted and wanted != "全部":
            target = self._facet_of(wanted) if wanted != "全产物" else "全产物"
            rows = [r for r in rows if r["facet"] == target]

        rows.sort(key=lambda r: (-r["value_per_min"], r["required_level"], r["id"]))

        open_rows: List[dict] = []
        soon_rows: List[dict] = []
        locked_rows: List[dict] = []
        if player_level is None:
            open_rows = list(rows)
        else:
            for row in rows:
                gap = row["required_level"] - player_level
                if gap <= 0:
                    open_rows.append(row)
                elif gap <= 3:
                    soon_rows.append(row)
                else:
                    locked_rows.append(row)

        # 深档门槛先判定：分组、★ 基准、推荐位都要用到「这一档现在进不进得去」
        for row in rows:
            gate_need = row.get("gate_need") or ""
            if not gate_need:
                continue
            row["gate_text"] = f"需{gate_need}全套"
            if player is not None:
                row["gate_ok"] = self._gear_gate_ok(player, row["required_level"])

        # ★ 以「当前可进入段的最高每分钟材料价值」为 100%：
        # 与排序同口径，星级才能顺着列表单调递减，不会出现「第二名没星、第三名有星」。
        # 装备没备齐的三重不参与基准：它价值最高但玩家进不去，拿它当 100% 会让
        # 整页可去的档位全变成一星。
        rated = [r for r in open_rows if r.get("gate_ok") is not False] or open_rows
        peak = max((r["value_per_min"] for r in rated), default=0.0)
        if peak <= 0:
            peak = max((r["value_per_min"] for r in rows), default=0.0)
        for row in rows:
            self._decorate_rift_row(row, self._stars(row["value_per_min"], peak))

        # 同分并列时只给第一条「最佳」，避免整段都是奖牌
        for i, row in enumerate(open_rows):
            row["badge"] = "最佳" if i == 0 and len(open_rows) > 1 else ""
            row["rank"] = i + 1

        # 未解锁的两段打上 locked：网页端据此把行内按钮换成「需 XX」，
        # 不能给一个点了只会弹「境界不足」的「进入」按钮。
        for row in soon_rows + locked_rows:
            row["locked"] = True
            row["badge"] = ""
            row["lock_text"] = (
                f"需{row['level_text']}" if row.get("level_text")
                else f"需境界 {row['required_level']}"
            )

        # 推荐位按产物轮转，保证前 N 条覆盖不同材料而不是同类堆叠
        recommended: List[dict] = []
        try:
            from ..utils.list_card import pick_recommend

            # 装备没备齐的三重不进推荐位：深档价值高，混进来会把「推荐」全占满，
            # 玩家点一个撞一次「准备不足」。够数就只推能进的，不够再回退全量。
            eligible = [r for r in open_rows if r.get("gate_ok") is not False]
            pool = eligible if len(eligible) >= self.LIST_TOP_N else open_rows
            recommended = pick_recommend(pool, self.PRODUCT_FACETS, self.LIST_TOP_N)
            for row in recommended:
                row["recommend"] = True
        except Exception:
            recommended = open_rows[: self.LIST_TOP_N]

        facets = [{"key": "all", "label": "推荐", "count": len(recommended)}]
        facets.append({"key": "全部", "label": "全部", "count": len(open_rows)})
        for facet in self.PRODUCT_FACETS:
            count = sum(1 for r in open_rows if r["facet"] == facet)
            if count:
                facets.append({"key": facet, "label": facet, "count": count})

        groups: List[dict] = []
        if open_rows:
            groups.append(
                {
                    "key": "open",
                    "name": "当前可进 · 按材料价值排序",
                    "collapsed": False,
                    "items": open_rows,
                }
            )
        if soon_rows:
            groups.append(
                {
                    "key": "soon",
                    "name": f"即将解锁 · 还差 1~3 级 · {len(soon_rows)} 档",
                    "collapsed": True,
                    "items": soon_rows,
                }
            )
        if locked_rows:
            groups.append(
                {
                    "key": "locked",
                    "name": f"尚未解锁 · {len(locked_rows)} 档",
                    "collapsed": True,
                    "items": locked_rows,
                }
            )

        if player_level is None:
            subtitle = f"共 {len(rows)} 档"
        else:
            # 副标题也带等级：玩家一眼能算出「还差几级」才够下一档
            subtitle = (
                f"{self._level_label(player_level)} · 可进入 {len(open_rows)} 档"
                f" / 未解锁 {len(rows) - len(open_rows)} 档"
            )
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

    async def list_rifts(self, user_id: str = None, product: str = "", data: dict = None) -> Tuple[bool, str]:
        """秘境列表（文本兜底版：图片与网页都不可用时使用）。

        Args:
            user_id: 传入时按玩家境界标注可进入状态
            product: 主产物筛选（灵植/矿材/灵液/妖兽/全产物）
            data: 已算好的 build_rift_list 结果，传入可省一次重复计算

        Returns:
            (成功标志, 消息)
        """
        data = data or await self.build_rift_list(user_id, product)
        if not data:
            return False, "❌ 当前没有开放的秘境！"

        lines = ["🌀 秘境列表", data["subtitle"], "━━━━━━━━━━━━━━━"]
        for group in data["groups"]:
            items = group["items"]
            if group["key"] == "open":
                lines.append(f"▸ {group['name']}")
                shown = items[: self.LIST_TOP_N]
                for row in shown:
                    gate = f" · {row['gate_text']}" if row.get("gate_text") else ""
                    lock = "🔒" if row.get("gate_ok") is False else ""
                    lines.append(
                        f"  {lock}{row['title']} (ID:{row['id']})  {row['level_text']} · {row['minutes']}分 · "
                        f"{row['facet']}{row['star_text']} · 修为{self._fmt_amount(row['exp'])}"
                        f" · 灵石{self._fmt_amount(row['gold'])}{gate}"
                    )
                if len(items) > len(shown):
                    lines.append(f"  …另有 {len(items) - len(shown)} 档，/秘境列表 全部 查看")
            elif group["key"] == "soon":
                lines.append(f"▸ {group['name']}")
                for row in items[:4]:
                    lines.append(
                        f"  🔒 {row['title']} (ID:{row['id']}) 需{row['level_text']} · {row['minutes']}分"
                    )
            else:
                lines.append(f"▸ {group['name']}（含更深处秘境，境界提升后开放）")

        lines.append("━━━━━━━━━━━━━━━")
        lines.append("🔒 三重需该门槛境界可装备的最高品阶全套（八格齐全）；深档产出更高")
        lines.append("💡 /秘境列表 灵植 / 矿材 / 灵液 / 妖兽 / 全产物 按产物筛选")
        lines.append("💡 /探索秘境 <ID> 进入；/秘境详情 <ID> 查看掉落")
        return True, "\n".join(lines)

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
        ]
        gate_need = self._gear_need_name(int(rift.required_level or 0))
        if cfg.get("gear_lock") and gate_need:
            lines.append(f"装备门槛：{gate_need}全套（八格齐全且品阶达标）")
        try:
            tier_yield = float(cfg.get("tier_yield") or 1.0)
        except (TypeError, ValueError):
            tier_yield = 1.0
        if tier_yield != 1.0:
            lines.append(f"深档加成：修为一律 ×{tier_yield:g}（深档掉落更偏高阶材料）")
            exp_range = [int(float(exp_range[0]) * tier_yield), int(float(exp_range[-1]) * tier_yield)]
            gold_range = [int(float(gold_range[0]) * tier_yield), int(float(gold_range[-1]) * tier_yield)]
        lines.extend([
            f"修为：{exp_range[0]:,}~{exp_range[1]:,}　灵石：{gold_range[0]:,}~{gold_range[1]:,}",
            f"掉落概率：{cfg.get('item_chance', 70)}%"
            f"（额外掉落 {cfg.get('extra_drop_chance', 0)}%"
            f"／装备 {cfg.get('equipment_chance', 0)}%）",
        ])
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
        
        # 5. 深档装备门槛（三重）：八格全部达到该门槛境界可装备的最高品阶
        gate_detail = self._gear_lock_detail(player, rift)
        if gate_detail:
            need = gate_detail.get("need") or ""
            tips = []
            if gate_detail.get("empty_text"):
                tips.append(f"未装备：{gate_detail['empty_text']}")
            if gate_detail.get("low_text"):
                tips.append(f"品阶不足：{gate_detail['low_text']}")
            return False, (
                f"❌ 准备不足，无法深入！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"【{rift.display_name}】要求【{need}】全套装备（八格齐全且品阶达标）\n"
                + ("\n".join(f"  · {t}" for t in tips) if tips else "")
                + f"\n💡 深档产出更高，但也更挑人"
                f"\n   /装备 查看当前穿戴，/秘境列表 查看各档门槛"
            )

        # 6. 设置探索状态，存储秘境ID（时长按难度取，同秘境不同深度耗时不同）
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
        """进入时把整趟修为/灵石 roll 定，分段只按比例拆，总和不因拆段而波动。

        ``tier_yield`` 是深档强度：三重时长是二重的 1.5 倍，若产出只按配置线性
        涨，每小时反而低于二重。把它乘在单趟产出上，深档才有「同样的本，准备
        更充分就能拿更多」的意义（每小时倍数 = tier_yield ÷ 时长比）。
        """
        cfg: Dict = {}
        if rift:
            cfg = rift.get_rewards()
            exp_range = cfg.get("exp") or [1000, 5000]
            gold_range = cfg.get("gold") or [500, 2000]
        else:
            exp_range, gold_range = [1000, 5000], [500, 2000]
        lo, hi = int(exp_range[0]), int(exp_range[1])
        g_lo, g_hi = int(gold_range[0]), int(gold_range[1])
        try:
            mult = max(0.0, float(cfg.get("tier_yield") or 1.0)) if cfg else 1.0
        except (TypeError, ValueError):
            mult = 1.0
        exp = random.randint(min(lo, hi), max(lo, hi))
        gold = random.randint(min(g_lo, g_hi), max(g_lo, g_hi))
        if mult != 1.0:
            exp = int(exp * mult)
            gold = int(gold * mult)
        return exp, gold

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

        # 深档强度：材料数量乘以 tier_yield，重量偏向该档掉落表里品阶最高的那几件
        try:
            tier_yield = max(0.0, float(cfg.get("tier_yield") or 1.0))
        except (TypeError, ValueError):
            tier_yield = 1.0
        try:
            high_mult = max(0.0, float(cfg.get("high_tier_weight_mult") or 1.0))
        except (TypeError, ValueError):
            high_mult = 1.0
        weights = self._rift_drop_weights(drop_table, high_mult)

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
        # 仙缘商店【采药心得】：材料掉率加点（百分点），与符箓/气运同一口径
        try:
            from ..core.reincarnation_rules import permanent_rift_item_bonus

            item_chance += int(round(permanent_rift_item_bonus(player) * 100))
        except Exception:
            pass
        # 分段结算按 chance_scale 摊薄，整趟期望不变
        # 额外掉落是「主掉落成功后的条件概率」，不跟着摊薄：
        # 主掉落按 s 摊、extra 保持原值，整趟期望才等于 主×(1+extra)。
        item_chance = min(100.0, max(0.0, item_chance * max(0.0, float(chance_scale))))

        def _pick() -> Optional[Tuple[str, int]]:
            total_weight = sum(weights)
            if total_weight <= 0:
                return None
            roll = random.random() * total_weight
            cursor = 0.0
            for item, weight in zip(drop_table, weights):
                cursor += weight
                if roll <= cursor:
                    lo = int(item.get("min", 1) or 1)
                    hi = int(item.get("max", lo) or lo)
                    lo, hi = min(lo, hi), max(lo, hi)
                    count = random.randint(lo, hi)
                    if tier_yield != 1.0:
                        count = max(1, int(round(count * tier_yield)))
                    return item["name"], count
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
            # 仙缘商店【寻宝直觉】：装备掉率乘区（材料与装备分开计，互不稀释）
            try:
                from ..core.reincarnation_rules import permanent_rift_equip_bonus

                chance *= 1.0 + max(0.0, float(permanent_rift_equip_bonus(player) or 0.0))
            except Exception:
                pass
            if chance > 0 and random.random() * 100.0 <= chance:
                drop = roll_equipment_drop(
                    self.config_manager,
                    self._equipment_base_level(rift, player),
                    self.config.get("equipment_slot_weights"),
                    up_rank_weight=self._equipment_up_rank_weight(rift, tier),
                )
                if drop:
                    dropped_items.append((drop["name"], 1, drop["quality"]))
        except Exception as e:
            if self.config_manager:
                logger.warning(f"秘境装备掉落判定失败：{e}")

        return dropped_items

    def _gear_need_name(self, required_level) -> str:
        """该门槛境界可装备的最高品阶名（空串表示取不到）。"""
        try:
            from ..core.rift_gate import required_gear_rank

            return required_gear_rank(required_level)
        except Exception:
            return ""

    def _gear_lock_detail(self, player, rift) -> Optional[dict]:
        """深档装备门槛：未配置锁、或玩家已达标时返回 None。"""
        if player is None or rift is None:
            return None
        if not self._rift_cfg(rift).get("gear_lock"):
            return None
        try:
            from ..core.rift_gate import check as _check

            ok, detail = _check(
                player, int(rift.required_level or 0), self.config_manager
            )
        except Exception:
            return None
        return None if ok else detail

    def _gear_gate_ok(self, player, required_level) -> bool:
        """列表用：玩家装备是否满足该档次的门槛（异常时按可达处理）。"""
        if player is None:
            return True
        try:
            from ..core.rift_gate import check as _check

            ok, _ = _check(player, int(required_level or 0), self.config_manager)
            return bool(ok)
        except Exception:
            return True

    def _material_rank_score(self, name: str) -> int:
        """材料品阶序号（用解锁等级当序，比 1~5 的稀有度档更细）。"""
        if not name or not self.config_manager:
            return -1
        try:
            info = self.config_manager.get_material_info(name)
        except Exception:
            return -1
        if not isinstance(info, dict):
            return -1
        return int(RANK_LEVELS.get(str(info.get("tier") or ""), -1))

    def _rift_drop_weights(self, drop_table: List[dict], mult: float) -> List[float]:
        """计算实际抽取权重：深档把权重压向掉落表里品阶最高的那几件。

        深档的意义是「更好的材料」，而不是把整张表按比例放大——后者只是把
        总产出乘一个系数，稀释不了低阶材料。这里只放大最高品阶那一档，
        低阶材料的权重重不变，占比自然下降。
        """
        weights = [float(max(0, int(d.get("weight") or 0))) for d in drop_table]
        if mult == 1.0 or not drop_table:
            return weights
        scores = [self._material_rank_score(str(d.get("name") or "")) for d in drop_table]
        best = max(scores) if scores else -1
        if best <= 0:
            return weights
        return [w * mult if score == best else w for w, score in zip(weights, scores)]

    def _equipment_base_level(self, rift: Optional[Rift], player) -> int:
        """装备品阶基准：玩家等级与「秘境门槛 + 越级余量」取低。

        旧逻辑只用 ``player.level_index``，于是 lv35 刷 lv0 的青云秘境也掉
        混元先天装备 —— 低门槛本成了顶级装备农场（实测进阶本每小时装备价值
        75 万灵石，与本境界最高秘境持平）。现在按秘境门槛封顶：
        lv35 刷青云一重只出凡/黄品，要拿当前阶必须刷对应门槛的本。
        """
        player_level = int(getattr(player, "level_index", 0) or 0)
        if rift is None:
            return player_level
        try:
            bonus = int(self.config.get("equipment_level_cap_bonus", 2))
        except (TypeError, ValueError):
            bonus = 2
        try:
            required = int(rift.required_level or 0)
        except (TypeError, ValueError):
            return player_level
        return min(player_level, max(0, required + bonus))

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

