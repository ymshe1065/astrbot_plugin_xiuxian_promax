# managers/bounty_manager.py
"""悬赏令系统管理器"""

import json
import random
import time
from pathlib import Path
from typing import Tuple, List, Optional, Dict, TYPE_CHECKING

from astrbot.api import logger

from ..data import DataBase
from ..models import Player
from .task_penalty import calc_abandon_penalty, charge_abandon_penalty

if TYPE_CHECKING:
    from ..core import StorageRingManager

__all__ = ["BountyManager"]


class BountyManager:
    """悬赏令管理器"""

    BOUNTY_CACHE_DURATION = 600  # 任务列表缓存10分钟
    # 当前版本所有能产生悬赏进度的活动标签，配置校验与扩展时以此为准
    TRIGGERABLE_TAGS = {
        "adventure_scout",
        "adventure_roam",
        "adventure_hunt",
        "adventure_peril",
        "rift",
        "escort",
        "craft",
        "spar",
        "pvp",
        "tide",
        "boss_damage",
        "harvest",
        "sect",
    }
    CONFIG_FILE = Path(__file__).resolve().parents[1] / "config" / "bounty_templates.json"
    ADVENTURE_CONFIG_FILE = Path(__file__).resolve().parents[1] / "config" / "adventure_config.json"
    DEFAULT_CONFIG = {
        "difficulties": {
            "easy": {"name": "F级", "stone_scale": 1.0, "exp_scale": 1.0, "min_level": 0}
        },
        "templates": [
            {
                "id": 1,
                "name": "击退妖兽",
                "difficulty": "easy",
                "category": "巡山",
                "progress_tags": ["adventure_scout"],
                "min_target": 3,
                "max_target": 5,
                "time_limit": 3600,
                "reward": {"stone": 300, "exp": 2500},
                "item_table": "hunt",
                "description": "驱逐骚扰山门的妖兽。"
            }
        ],
        "item_tables": {
            "hunt": [
                {"name": "灵兽毛皮", "weight": 40, "min": 1, "max": 3},
                {"name": "妖兽精血", "weight": 30, "min": 1, "max": 2},
                {"name": "玄铁", "weight": 30, "min": 1, "max": 2}
            ]
        }
    }

    def __init__(self, db: DataBase, storage_ring_manager: Optional["StorageRingManager"] = None):
        self.db = db
        self.storage_ring_manager = storage_ring_manager
        self._bounty_cache: Dict[str, Dict] = {}
        self.difficulties: Dict[str, dict] = {}
        self.templates_by_id: Dict[int, dict] = {}
        self.templates_by_diff: Dict[str, List[dict]] = {}
        self.item_tables: Dict[str, List[dict]] = {}
        self.adventure_tag_meta: Dict[str, Dict[str, int]] = {}
        self.reload_config()

    # -------- 配置 --------

    def reload_config(self):
        config = self._load_config_file()
        self.difficulties = config.get("difficulties", self.DEFAULT_CONFIG["difficulties"])
        self.item_tables = config.get("item_tables", self.DEFAULT_CONFIG["item_tables"])
        self.templates_by_id = {}
        self.templates_by_diff = {}
        for tpl_copy in self._sanitize_templates(config.get("templates", [])):
            self.templates_by_id[tpl_copy["id"]] = tpl_copy
            self.templates_by_diff.setdefault(tpl_copy["difficulty"], []).append(tpl_copy)
        self._bounty_cache.clear()
        logger.info(f"悬赏配置加载完成：{len(self.templates_by_id)} 条模板")
        self._load_adventure_meta()

    def _sanitize_templates(self, templates: list) -> List[dict]:
        """过滤冲突或残缺的模板，避免列表展示后无法接取/推进。"""
        cleaned: List[dict] = []
        seen_ids = set()
        for raw in templates:
            try:
                tpl = dict(raw)
                template_id = int(tpl["id"])
                name = str(tpl.get("name", "")).strip()
                difficulty = str(tpl.get("difficulty", "")).strip()
                if not name or difficulty not in self.difficulties:
                    logger.warning(f"悬赏模板 {template_id} 名称或难度无效，已跳过")
                    continue
                if template_id in seen_ids:
                    logger.warning(f"悬赏模板 ID 重复：{template_id}，已跳过后者")
                    continue

                min_target = max(1, int(tpl.get("min_target", 1)))
                max_target = max(min_target, int(tpl.get("max_target", min_target)))
                progress_tags = [
                    str(tag).strip().lower()
                    for tag in tpl.get("progress_tags", [])
                    if str(tag).strip()
                ]
                if not progress_tags:
                    logger.warning(f"悬赏模板 {template_id} 缺少可推进标签，已跳过")
                    continue
                unknown_tags = set(progress_tags) - self.TRIGGERABLE_TAGS
                if unknown_tags:
                    logger.warning(
                        f"悬赏模板 {template_id} 含未知进度标签 {sorted(unknown_tags)}，"
                        "该标签不会产生进度"
                    )

                item_table = str(tpl.get("item_table", "gather") or "gather")
                if item_table not in self.item_tables:
                    logger.warning(
                        f"悬赏模板 {template_id} 的奖励表 {item_table} 不存在，回退为 gather"
                    )
                    item_table = "gather"

                tpl.update(
                    {
                        "id": template_id,
                        "name": name,
                        "difficulty": difficulty,
                        "min_target": min_target,
                        "max_target": max_target,
                        "progress_tags": progress_tags,
                        "item_table": item_table,
                        "weight": max(1, int(tpl.get("weight", 1))),
                        "time_limit": max(60, int(tpl.get("time_limit", 3600))),
                    }
                )
                seen_ids.add(template_id)
                cleaned.append(tpl)
            except Exception as exc:
                logger.warning(f"悬赏模板配置异常，已跳过：{exc}")
        return cleaned

    def _load_config_file(self) -> dict:
        if self.CONFIG_FILE.exists():
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.error(f"加载 bounty_templates.json 失败，将使用默认配置: {exc}")
        return self.DEFAULT_CONFIG

    def _load_adventure_meta(self):
        self.adventure_tag_meta = {}
        if not self.ADVENTURE_CONFIG_FILE.exists():
            return
        try:
            with open(self.ADVENTURE_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for route in data.get("routes", []):
                tag = str(route.get("bounty_tag", "")).lower()
                if not tag:
                    continue
                self.adventure_tag_meta[tag] = {
                    "duration": int(route.get("duration", 3600)),
                    "fatigue": int(route.get("fatigue_cooldown", 0))
                }
            logger.info("已加载冒险路线元数据用于悬赏校准")
        except Exception as exc:
            logger.warning(f"加载冒险路线配置失败，将使用默认时限: {exc}")

    # -------- 列表 & 缓存 --------

    def _get_cached_bounties(
        self, user_id: str, level_index: Optional[int] = None
    ) -> Optional[List[dict]]:
        cache = self._bounty_cache.get(user_id)
        if not cache or cache["expire_time"] <= int(time.time()):
            return None
        if level_index is not None and cache.get("level_index") != level_index:
            return None
        return cache["bounties"]

    def _set_cached_bounties(self, user_id: str, bounties: List[dict], level_index: int):
        self._bounty_cache[user_id] = {
            "bounties": bounties,
            "level_index": level_index,
            "expire_time": int(time.time()) + self.BOUNTY_CACHE_DURATION
        }

    async def get_bounty_list(self, player: Player) -> List[dict]:
        """获取悬赏列表（境界变化后立即重算，避免接取到未解锁等级）"""
        cached = self._get_cached_bounties(player.user_id, player.level_index)
        if cached:
            return cached

        plan = self._get_difficulty_plan(player.level_index)
        bounties: List[dict] = []
        for diff in plan:
            entry = self._build_bounty_entry(diff, player)
            if entry:
                bounties.append(entry)

        self._set_cached_bounties(player.user_id, bounties, player.level_index)
        return bounties

    def _get_difficulty_plan(self, level_index: int) -> List[str]:
        """按配置中的 min_level 解锁难度，避免硬编码与配置冲突。"""
        plan = [
            diff
            for diff, cfg in self.difficulties.items()
            if level_index >= int(cfg.get("min_level", 0) or 0)
        ]
        plan.sort(key=lambda diff: int(self.difficulties[diff].get("min_level", 0) or 0))
        return plan

    def _pick_template(self, difficulty: str) -> Optional[dict]:
        templates = self.templates_by_diff.get(difficulty)
        if not templates:
            return None
        total = sum(max(1, tpl.get("weight", 1)) for tpl in templates)
        roll = random.randint(1, total)
        upto = 0
        for tpl in templates:
            upto += max(1, tpl.get("weight", 1))
            if roll <= upto:
                return tpl
        return templates[0]

    def _build_bounty_entry(self, difficulty: str, player: Player) -> Optional[dict]:
        template = self._pick_template(difficulty)
        if not template:
            return None
        diff_cfg = self.difficulties.get(difficulty, {})
        target = random.randint(template.get("min_target", 1), template.get("max_target", 1))
        reward = self._calculate_reward(template, diff_cfg, player, target)
        progress_tags = [str(tag).lower() for tag in template.get("progress_tags", [])]
        time_limit = self._calculate_time_limit(template, target)
        return {
            "id": template["id"],
            "name": template["name"],
            "category": template.get("category", "任务"),
            "difficulty": difficulty,
            "difficulty_name": diff_cfg.get("name", difficulty),
            "description": template.get("description", ""),
            "count": target,
            "reward": reward,
            "time_limit": time_limit,
            "progress_tags": progress_tags,
            "item_table": template.get("item_table", "gather")
        }

    def _calculate_reward(self, template: dict, diff_cfg: dict, player: Player, target: int) -> Dict[str, int]:
        base_reward = template.get("reward", {"stone": 200, "exp": 2000})
        stone = base_reward.get("stone", 0)
        exp = base_reward.get("exp", 0)
        level_bonus = 1 + max(0, player.level_index - 3) * 0.06
        progress_factor = max(1, target) / max(1, template.get("min_target", 1))
        stone_scale = diff_cfg.get("stone_scale", 1.0)
        exp_scale = diff_cfg.get("exp_scale", 1.0)
        final_stone = int(stone * stone_scale * progress_factor * level_bonus)
        final_exp = int(exp * exp_scale * progress_factor * level_bonus)
        return {"stone": final_stone, "exp": final_exp}

    def _calculate_time_limit(self, template: dict, target: int) -> int:
        base_limit = template.get("time_limit", 3600)
        unit = self._get_adventure_unit_time(template)
        if not unit:
            return base_limit
        # 预估完成所有进度所需的最小时间
        expected = unit * target
        # 追加容错缓冲，兼顾疲劳与队列
        buffer = max(600, unit // 2)
        return max(base_limit, expected + buffer)

    def _get_adventure_unit_time(self, template: dict) -> Optional[int]:
        durations = []
        for tag in template.get("progress_tags", []):
            meta = self.adventure_tag_meta.get(tag.lower())
            if meta:
                # 考虑路线基础时长+部分休整
                durations.append(meta.get("duration", 0) + meta.get("fatigue", 0))
        if not durations:
            return None
        return max(60, min(durations))

    # -------- 接取与状态 --------

    async def accept_bounty(self, player: Player, bounty_id: int) -> Tuple[bool, str]:
        if bounty_id <= 0:
            return False, "无效的悬赏编号。"

        template = self.templates_by_id.get(bounty_id)
        if not template:
            return False, "该悬赏已失效，请刷新列表。"

        diff_key = template.get("difficulty", "easy")
        diff_cfg = self.difficulties.get(diff_key, {})
        if diff_key not in self._get_difficulty_plan(player.level_index):
            return False, (
                f"❌ 你的境界尚不足以接取【{diff_cfg.get('name', diff_key)}】悬赏，"
                "请提升境界后再来。"
            )
        cached_bounties = self._get_cached_bounties(player.user_id, player.level_index)
        cached = None
        if cached_bounties:
            cached = next((b for b in cached_bounties if b["id"] == bounty_id), None)
        if not cached:
            return False, "⚠️ 悬赏列表已刷新，请先发送 /悬赏令 重新查看后再接取。"

        now = int(time.time())
        time_limit = cached.get("time_limit", template.get("time_limit", 3600))

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            active = await self.db.ext.get_active_bounty(player.user_id)
            if active:
                await self.db.conn.rollback()
                return False, f"你已有进行中的悬赏：{active['bounty_name']}，请先完成或放弃。"

            cd_key = f"bounty_abandon_cd_{player.user_id}"
            cd_value = await self.db.ext.get_system_config(cd_key)
            if cd_value:
                cd_time = int(cd_value)
                if now < cd_time:
                    await self.db.conn.rollback()
                    remaining = (cd_time - now) // 60 or 1
                    return False, f"你刚放弃过悬赏，还需等待 {remaining} 分钟才能再次接取。"

            expire_time = now + time_limit
            rewards_json = json.dumps({
                "stone": cached["reward"]["stone"],
                "exp": cached["reward"]["exp"],
                "difficulty": diff_key,
                "difficulty_name": cached.get("difficulty_name", diff_key),
                "item_table": cached.get("item_table"),
                "description": cached.get("description", ""),
                "progress_tags": cached.get("progress_tags", [])
            }, ensure_ascii=False)

            await self.db.conn.execute(
                """
                INSERT INTO bounty_tasks (
                    user_id, bounty_id, bounty_name, target_type,
                    target_count, current_progress, rewards,
                    start_time, expire_time, status
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 1)
                """,
                (
                    player.user_id,
                    bounty_id,
                    template["name"],
                    cached.get("category", template.get("category", "任务")),
                    cached["count"],
                    rewards_json,
                    now,
                    expire_time
                )
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        return True, (
            f"🎯 接取悬赏成功！\n"
            f"任务：{template['name']}（{cached.get('difficulty_name', diff_key)}）\n"
            f"目标：完成 {cached['count']} 次\n"
            f"奖励：{cached['reward']['stone']:,} 灵石 + {cached['reward']['exp']:,} 修为\n"
            f"时限：{time_limit // 60} 分钟"
        )

    async def check_bounty_status(self, player: Player) -> Tuple[bool, str]:
        active = await self.db.ext.get_active_bounty(player.user_id)
        if not active:
            return False, "你当前没有进行中的悬赏任务。\n使用 /悬赏令 查看可接取的任务。"

        rewards = json.loads(active["rewards"])
        remaining = max(0, active["expire_time"] - int(time.time()))
        progress = active.get("current_progress", 0)
        target = active.get("target_count", 1)

        diff_name = rewards.get("difficulty_name", rewards.get("difficulty", "未知"))
        desc = rewards.get("description", "")

        return True, (
            f"📜 当前悬赏（{diff_name}）\n"
            f"━━━━━━━━━━━━━━━\n"
            f"任务：{active['bounty_name']}\n"
            f"说明：{desc}\n"
            f"进度：{progress}/{target}\n"
            f"奖励：{rewards.get('stone', 0):,} 灵石 + {rewards.get('exp', 0):,} 修为\n"
            f"剩余时间：{remaining // 60} 分钟\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 完成后使用 /完成悬赏 领取奖励"
        )

    async def complete_bounty(self, player: Player) -> Tuple[bool, str]:
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            active = await self.db.ext.get_active_bounty(player.user_id)
            if not active:
                await self.db.conn.rollback()
                return False, "你当前没有进行中的悬赏任务。"

            if int(time.time()) > active["expire_time"]:
                await self.db.conn.execute(
                    "UPDATE bounty_tasks SET status = 0 WHERE user_id = ? AND status = 1",
                    (player.user_id,)
                )
                await self.db.conn.commit()
                return False, "悬赏任务已超时，自动取消。"

            progress = active.get("current_progress", 0)
            target = active.get("target_count", 1)
            if progress < target:
                await self.db.conn.rollback()
                return False, (
                    f"❌ 任务尚未完成！\n"
                    f"任务：{active['bounty_name']}\n"
                    f"进度：{progress}/{target}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"💡 通过历练或秘境推进悬赏进度"
                )

            rewards = json.loads(active["rewards"])
            stone_reward = rewards.get("stone", 0)
            exp_reward = rewards.get("exp", 0)
            try:
                from .title_manager import TitleManager
                exp_reward = int(exp_reward * (1 + TitleManager.get_exp_bonus(player)))
            except Exception:
                pass

            await self.db.conn.execute(
                "UPDATE bounty_tasks SET status = 2 WHERE user_id = ? AND status = 1",
                (player.user_id,)
            )

            MAX_VALUE = 2**63 - 1
            player.gold = min(player.gold + stone_reward, MAX_VALUE)
            player.experience = min(player.experience + exp_reward, MAX_VALUE)
            await self.db.conn.execute(
                "UPDATE players SET gold = ?, experience = ? WHERE user_id = ?",
                (player.gold, player.experience, player.user_id)
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        item_msg = ""
        if self.storage_ring_manager:
            try:
                rewards = json.loads(active["rewards"])
                item_table = rewards.get("item_table") or active.get("target_type", "gather")
                dropped_items = await self._roll_bounty_items(player, item_table)
                if dropped_items:
                    lines = []
                    for item_name, count in dropped_items:
                        success, _ = await self.storage_ring_manager.store_item(player, item_name, count, silent=True)
                        if success:
                            lines.append(f"  · {item_name} x{count}")
                        else:
                            lines.append(f"  · {item_name} x{count}（储物戒已满，丢失）")
                    if lines:
                        item_msg = "\n\n📦 获得物品：\n" + "\n".join(lines)
            except Exception:
                logger.warning("悬赏物品奖励发放异常", exc_info=True)

        rewards = json.loads(active["rewards"])
        diff_name = rewards.get("difficulty_name", rewards.get("difficulty", "未知"))
        return True, (
            f"✅ 悬赏完成（{diff_name}）！\n"
            f"任务：{active['bounty_name']}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"获得灵石：+{rewards.get('stone', 0):,}\n"
            f"获得修为：+{rewards.get('exp', 0):,}{item_msg}"
        )

    async def abandon_bounty(self, player: Player) -> Tuple[bool, str]:
        active = await self.db.ext.get_active_bounty(player.user_id)
        if not active:
            return False, "你当前没有进行中的悬赏任务。"

        try:
            rewards = json.loads(active.get("rewards") or "{}")
        except Exception:
            rewards = {}
        cost, currency = calc_abandon_penalty(
            int(rewards.get("stone", 0) or 0),
            int(rewards.get("spiritual_qi", 0) or 0),
        )
        currency_label = "灵气" if currency == "qi" else "灵石"

        fresh = await self.db.get_player_by_id(player.user_id)
        if not fresh:
            return False, "❌ 玩家数据不存在。"

        ok, msg = await charge_abandon_penalty(fresh, self.db, cost, currency)
        if not ok:
            return False, msg

        await self.db.ext.cancel_bounty(player.user_id)
        abandon_cooldown = int(time.time()) + 1800
        await self.db.ext.set_system_config(
            f"bounty_abandon_cd_{player.user_id}", str(abandon_cooldown)
        )
        return True, (
            f"已放弃悬赏：{active['bounty_name']}\n"
            f"扣除违约金：{cost:,} {currency_label}\n"
            "⚠️ 30分钟内无法接取新悬赏"
        )

    # -------- 进度与奖励 --------

    async def _roll_bounty_items(self, player: Player, table_name: str) -> List[Tuple[str, int]]:
        dropped_items: List[Tuple[str, int]] = []
        drop_table = self.item_tables.get(table_name, self.item_tables.get("gather", []))
        if not drop_table or random.randint(1, 100) > 70:
            return dropped_items

        total_weight = sum(item["weight"] for item in drop_table)
        roll = random.randint(1, total_weight)
        upto = 0
        chosen = drop_table[0]
        for item in drop_table:
            upto += item["weight"]
            if roll <= upto:
                chosen = item
                break

        count = random.randint(chosen["min"], chosen["max"])
        dropped_items.append((chosen["name"], count))
        return dropped_items

    async def add_bounty_progress(self, player: Player, activity_tag: str, count: int = 1) -> Tuple[bool, str]:
        """根据活动标签推进悬赏"""
        if not isinstance(count, int) or count <= 0:
            return False, ""
        count = min(count, 10)
        activity_tag = (activity_tag or "").strip().lower()
        if not activity_tag:
            return False, ""

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            active = await self.db.ext.get_active_bounty(player.user_id)
            if not active:
                await self.db.conn.rollback()
                return False, ""

            if int(time.time()) > active["expire_time"]:
                await self.db.conn.rollback()
                return False, ""

            rewards_data = {}
            try:
                rewards_data = json.loads(active["rewards"])
            except Exception:
                rewards_data = {}

            template = self.templates_by_id.get(active["bounty_id"])
            if template:
                allowed_tags = template.get("progress_tags", [])
            else:
                allowed_tags = [str(tag).lower() for tag in rewards_data.get("progress_tags", [])]

            if activity_tag not in allowed_tags:
                await self.db.conn.rollback()
                return False, ""

            progress = active.get("current_progress", 0)
            target = active.get("target_count", 1)
            if progress >= target:
                await self.db.conn.rollback()
                return False, ""

            new_progress = min(target, progress + count)
            await self.db.conn.execute(
                "UPDATE bounty_tasks SET current_progress = ? WHERE user_id = ? AND status = 1 AND current_progress = ?",
                (new_progress, player.user_id, progress)
            )
            await self.db.conn.commit()

            if new_progress >= target:
                return True, f"\n\n📜 悬赏【{active['bounty_name']}】已完成！使用 /完成悬赏 领取奖励"
            return True, f"\n\n📜 悬赏进度：{new_progress}/{target}"
        except Exception:
            await self.db.conn.rollback()
            raise

    async def check_and_expire_bounties(self) -> int:
        now = int(time.time())
        cursor = await self.db.conn.execute(
            "UPDATE bounty_tasks SET status = 3 WHERE status = 1 AND expire_time < ?",
            (now,)
        )
        await self.db.conn.commit()
        return cursor.rowcount
