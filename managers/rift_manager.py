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
                    "item_chance": int(diff.get("item_chance") or 70),
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
        rift_id: int
    ) -> Tuple[bool, str]:
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
        if not user_cd:
            await self.db.ext.create_user_cd(user_id)
            user_cd = await self.db.ext.get_user_cd(user_id)
        
        if user_cd.type != UserStatus.IDLE:
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
        extra_data = {
            "rift_id": rift_id,
            "rift_level": rift.rift_level,
            "difficulty": rift.difficulty or 1,
        }
        await self.db.ext.set_user_busy(user_id, UserStatus.EXPLORING, scheduled_time, extra_data)
        
        return True, (
            f"✨ 你进入了『{rift.display_name}』！探索需要 {max(1, duration // 60)} 分钟。\n"
            f"使用 /完成探索 领取奖励"
        )
    
    async def finish_exploration(
        self,
        user_id: str
    ) -> Tuple[bool, str, Optional[Dict]]:
        """
        完成秘境探索
        
        Args:
            user_id: 用户ID
            
        Returns:
            (成功标志, 消息, 奖励数据)
        """
        # 1. 检查用户
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！", None
        
        # 2. 检查CD状态
        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.EXPLORING:
            return False, "❌ 你当前不在探索秘境！", None
        
        # 3. 检查时间
        current_time = int(time.time())
        if current_time < user_cd.scheduled_time:
            remaining = user_cd.scheduled_time - current_time
            minutes = remaining // 60
            return False, f"❌ 探索尚未完成！还需要 {minutes} 分钟。", None
        
        # 4. 获取秘境信息（从extra_data中读取）
        extra_data = user_cd.get_extra_data() if hasattr(user_cd, 'get_extra_data') else {}
        rift_id = extra_data.get("rift_id", 0)
        
        # 获取秘境配置
        rift = await self.db.ext.get_rift_by_id(rift_id) if rift_id else None
        rift_name = rift.display_name if rift else "未知秘境"
        
        # 5. 根据秘境配置计算奖励
        if rift:
            rewards_config = rift.get_rewards()
            exp_range = rewards_config.get("exp", [1000, 5000])
            gold_range = rewards_config.get("gold", [500, 2000])
            exp_reward = random.randint(exp_range[0], exp_range[1])
            gold_reward = random.randint(gold_range[0], gold_range[1])
        else:
            # 兼容旧数据，使用默认奖励
            exp_reward = random.randint(1000, 5000)
            gold_reward = random.randint(500, 2000)
        
        try:
            from .talisman_manager import TalismanManager
            from .luck_manager import LuckManager
            reward_mult = 1.0 + TalismanManager.get_active_bonus(player, "rift_reward_multiplier")
            reward_mult *= 1.0 + LuckManager.percent_bonus(
                player, per_point=0.3, cap=30.0, config_manager=self.config_manager
            ) / 100.0
            exp_reward = int(exp_reward * reward_mult)
            gold_reward = int(gold_reward * reward_mult)
            try:
                from .title_manager import TitleManager
                exp_reward = int(exp_reward * (1 + TitleManager.get_exp_bonus(player)))
            except Exception:
                pass
        except Exception:
            pass

        # 随机事件：只做小幅运气浮动，掉落基准概率由秘境难度决定
        events = [
            {"desc": "你发现了一处灵泉，修为大增！", "drop_mod": 6},
            {"desc": "你在秘境中击败了一只妖兽！", "drop_mod": 10},
            {"desc": "你找到了一个隐藏的宝箱！", "drop_mod": 15},
            {"desc": "你领悟了一些修炼心得。", "drop_mod": -8},
            {"desc": "你在秘境中遇到了前辈留下的传承！", "drop_mod": 8},
        ]
        event = random.choice(events)
        
        # 6. 物品掉落（按秘境难度的掉落表）
        dropped_items = []
        item_msg = ""
        dropped_items = await self._roll_rift_drops(player, rift, event["drop_mod"])

        # 7. 奖励、掉落与清空 CD 必须原子提交，避免结算中断后重复领取
        pet_encounter = None
        pet_msg = ""
        consumed_talismans: List[Tuple[str, int]] = []
        try:
            await self.db.conn.execute("BEGIN IMMEDIATE")
            if dropped_items:
                item_lines = []
                for entry in dropped_items:
                    item_name, count, *rest = entry
                    quality = rest[0] if rest else ""
                    # 统一发放入口：丹药固定进入丹药背包，其余进入储物戒
                    is_pill = self._is_pill_item(item_name)
                    if self.storage_ring_manager:
                        success, _ = await self.storage_ring_manager.grant_item(
                            player, item_name, count, quality=quality,
                            silent=True, external_transaction=True,
                        )
                        if success:
                            if is_pill:
                                suffix = "（丹药背包）"
                            else:
                                suffix = f"（{quality}）" if quality else ""
                            item_lines.append(f"  · {item_name} x{count}{suffix}")
                        else:
                            item_lines.append(f"  · {item_name} x{count}（储物戒已满，丢失）")
                    else:
                        item_lines.append(f"  · {item_name} x{count}（无法存储）")
                if item_lines:
                    item_msg = "\n\n📦 获得物品：\n" + "\n".join(item_lines)

            try:
                from ..core.technique_bonus import TechniqueBonusManager
                gold_reward = TechniqueBonusManager.apply_gold_bonus(
                    player, gold_reward, self.config_manager
                )
            except Exception:
                pass
            try:
                from .pet_manager import PetManager
                gold_reward = await PetManager(self.db).apply_gold_bonus(player, gold_reward)
            except Exception:
                pass
            player.experience += exp_reward
            player.gold += gold_reward

            # 探索符按次数生效：本次秘境结算后扣减一次，与耗时无关
            try:
                from .talisman_manager import TalismanManager
                consumed_talismans = TalismanManager.consume_run_charges(player, "rift")
            except Exception:
                consumed_talismans = []
            await self.db.update_player(player, commit=False)

            # 灵兽奇遇：秘境归来的空槽判定
            try:
                from .pet_manager import PetManager
                pet_encounter = await PetManager(self.db).try_exploration_encounter(
                    player, "秘境探索中", commit=False
                )
            except Exception:
                pet_encounter = None
            if pet_encounter:
                from .pet_manager import PetManager
                pet_msg = PetManager.format_exploration_encounter(pet_encounter)

            # 8. 清除CD
            await self.db.ext.set_user_free(user_id, commit=False)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        talisman_hint = ""
        if consumed_talismans:
            lines = [
                f"🧿 {name}：剩余 {left} 次历练/秘境" if left > 0 else f"🧿 {name}：次数已用尽"
                for name, left in consumed_talismans
            ]
            talisman_hint = "\n" + "\n".join(lines)

        msg = f"""
🌀 探索完成 - {rift_name}
━━━━━━━━━━━━━━━

{event["desc"]}

获得修为：+{exp_reward:,}
获得灵石：+{gold_reward:,}{item_msg}{pet_msg}{talisman_hint}
        """.strip()
        
        reward_data = {
            "exp": exp_reward,
            "gold": gold_reward,
            "event": event["desc"],
            "items": dropped_items,
            "pet": pet_encounter,
            "rift_name": rift_name
        }
        
        return True, msg, reward_data
    
    async def exit_rift(self, user_id: str) -> Tuple[bool, str]:
        """
        退出秘境（放弃探索）
        
        Args:
            user_id: 用户ID
            
        Returns:
            (成功标志, 消息)
        """
        # 1. 检查用户
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！"
        
        # 2. 检查CD状态
        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.EXPLORING:
            return False, "❌ 你当前不在探索秘境！"
        
        # 3. 按秘境最低灵石奖励的 80% 收违约金
        try:
            extra = user_cd.get_extra_data() if hasattr(user_cd, "get_extra_data") else {}
        except Exception:
            extra = {}
        rift_id = int((extra or {}).get("rift_id", 0) or 0)
        rift = await self.db.ext.get_rift_by_id(rift_id) if rift_id else None
        gold_reward = 0
        if rift:
            try:
                gold_range = rift.get_rewards().get("gold", [0, 0])
                gold_reward = int(min(gold_range[0], gold_range[1]))
            except Exception:
                gold_reward = 0

        cost, currency = calc_abandon_penalty(gold_reward)
        currency_label = "灵气" if currency == "qi" else "灵石"
        ok, msg = await charge_abandon_penalty(player, self.db, cost, currency)
        if not ok:
            return False, msg

        # 4. 清除CD状态
        await self.db.ext.set_user_free(user_id)
        
        return True, (
            "✅ 你已退出秘境，本次探索未获得任何奖励。\n"
            f"向秘境支付违约金：{cost:,} {currency_label}"
        )
    
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
        if not required and player is not None:
            required = int(getattr(player, "level_index", 0) or 0)
        return max(1, min(10, 1 + required // 4))
    
    async def _roll_rift_drops(self, player: Player, rift: Optional[Rift], drop_mod: int = 0) -> List[Tuple]:
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

        base_chance = int(cfg.get("item_chance") or 70)
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
        item_chance = min(100, max(0, item_chance))

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
        if random.randint(1, 100) <= item_chance:
            picked = _pick()
            if picked:
                dropped_items.append(picked)
            # 额外掉一件：概率来自难度配置（一重为 0，二重 45%，三重 70%）
            if extra_chance > 0 and random.randint(1, 100) <= extra_chance:
                picked = _pick()
                if picked:
                    dropped_items.append(picked)

        # 秘境传承机缘：越深的秘境更容易出功法/心法
        try:
            from ..core.technique_bonus import TechniqueBonusManager
            tech_name = TechniqueBonusManager.roll_technique_drop(
                player,
                self.config_manager,
                base_chance=4 + power * 2,
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
            chance = int(chance or 0)
            if chance > 0 and random.randint(1, 100) <= chance:
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

