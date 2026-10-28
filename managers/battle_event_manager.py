# managers/battle_event_manager.py
"""江湖小事件：限时通缉犯、妖兽暴动、魔修作乱与宝库守卫讨伐"""

import random
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from ..data import DataBase
from ..models import Player

__all__ = ["BattleEventManager"]


class BattleEventManager:
    KEY_NEXT_SPAWN = "battle_event_next_spawn_time"

    EVENT_TYPES: Dict[str, dict] = {
        "wanted": {
            "name": "通缉犯",
            "icon": "🚩",
            "weight": 35,
            "victory": "缉拿归案",
            "names": ["血手人屠", "玉面毒狐", "夺命判官", "叛门邪修", "黑风寨主", "笑面阎罗"],
            "elements": ["金", "木", "水", "火", "土", ""],
            "locations": ["落霞城", "黑风寨", "断魂崖", "迷雾林", "长安古道", "赤水渡"],
            "drop_table": "low",
            "desc": "悬赏榜上恶名昭著之辈，人人得而诛之。",
            "intro": [
                "江湖百晓生传来密报：{rank}级通缉犯【{name}】近日现身{location}，悬赏重金缉拿！",
                "六扇门张榜天下：{rank}级通缉犯【{name}】流窜至{location}，望各路修士出手相助。",
                "有散修在{location}撞见{rank}级通缉犯【{name}】行凶，官府已开出巨额赏金。",
            ],
        },
        "beast": {
            "name": "妖兽",
            "icon": "🐾",
            "weight": 25,
            "victory": "击退",
            "names": ["赤焰妖狼", "玄甲地龙", "九尾灵狐", "噬金鼠王", "碧眼金蟾", "墨鳞蛟"],
            "elements": ["火", "土", "木", "金", "水"],
            "locations": ["万兽山", "黑风林", "赤水沼泽", "落星谷", "断魂崖", "苍梧古原"],
            "drop_table": "low",
            "desc": "灵智已开的凶兽，吞噬生灵精血，为祸一方。",
            "intro": [
                "万兽山方向妖气冲天，{rank}级妖兽【{name}】正在{location}肆虐！",
                "附近村落传来急报：{rank}级妖兽【{name}】出没{location}，已有数名修士遇袭。",
                "巡山弟子发现{rank}级妖兽【{name}】的巢穴就在{location}，请诸位道友联手围剿。",
            ],
        },
        "demon": {
            "name": "魔修",
            "icon": "🩸",
            "weight": 25,
            "victory": "伏诛",
            "names": ["血袍魔修", "噬魂老鬼", "炼尸道人", "千面魔君", "阴煞修罗", "焚心邪僧"],
            "elements": ["水", "火", "木", "金", "土"],
            "locations": ["幽冥古道", "血月废墟", "乱葬岗", "黑水泽", "枯骨洞", "无相崖"],
            "drop_table": "mid",
            "desc": "修炼邪法的魔道修士，所过之处生灵涂炭。",
            "intro": [
                "正道联盟发出警示：{rank}级魔修【{name}】在{location}布下血祭大阵！",
                "有道友在{location}感应到滔天魔气，疑似{rank}级魔修【{name}】正在炼制邪器。",
                "天机阁推演到{rank}级魔修【{name}】的踪迹，此人已残害数十名修士！",
            ],
        },
        "guardian": {
            "name": "守卫",
            "icon": "🗿",
            "weight": 15,
            "victory": "击败",
            "names": ["青铜守宫", "镇墓石将", "古藤树妖", "熔岩傀儡", "冰晶巨像", "无名剑卫"],
            "elements": ["金", "土", "木", "火", "水"],
            "locations": ["上古遗府", "失落宝库", "太古祭坛", "地底宫殿", "封灵古墓", "云中秘境"],
            "drop_table": "mid",
            "desc": "守护古宝的傀儡或精怪，击杀后可取走其守护的珍藏。",
            "intro": [
                "有修士在{location}发现一座无人看守的宝库，但被{rank}级守卫【{name}】拦住了去路。",
                "地脉震动，{location}深处的{rank}级守卫【{name}】苏醒，库中宝光隐现！",
                "传闻{location}藏有上古遗宝，{rank}级守卫【{name}】正在镇守，机缘与凶险并存。",
            ],
        },
    }

    TIER_META: Dict[int, dict] = {
        1: {"rank": "丁", "min_level": 0, "weight": 38, "hp_mult": 0.25, "cooldown": 180, "expire_hours": 2, "drop_table": "low"},
        2: {"rank": "丙", "min_level": 0, "weight": 28, "hp_mult": 0.40, "cooldown": 180, "expire_hours": 2, "drop_table": "low"},
        3: {"rank": "乙", "min_level": 3, "weight": 18, "hp_mult": 0.60, "cooldown": 240, "expire_hours": 2, "drop_table": "mid"},
        4: {"rank": "甲", "min_level": 6, "weight": 11, "hp_mult": 0.85, "cooldown": 300, "expire_hours": 3, "drop_table": "mid"},
        5: {"rank": "S", "min_level": 12, "weight": 5, "hp_mult": 1.20, "cooldown": 360, "expire_hours": 3, "drop_table": "high"},
    }

    TIER_REWARDS: Dict[int, dict] = {
        1: {"rank1": 6000, "top3": 3000, "participate": 1000, "killer": 5000},
        2: {"rank1": 12000, "top3": 6000, "participate": 2000, "killer": 10000},
        3: {"rank1": 24000, "top3": 12000, "participate": 4000, "killer": 20000},
        4: {"rank1": 45000, "top3": 22000, "participate": 7000, "killer": 35000},
        5: {"rank1": 80000, "top3": 40000, "participate": 12000, "killer": 60000},
    }

    def __init__(self, db: DataBase, config_manager, storage_ring_mgr, world_boss_mgr):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_mgr = storage_ring_mgr
        self.world_boss_mgr = world_boss_mgr
        self.last_challenge_ok = False
        self.last_kill = False
        self.last_settle_msg: Optional[str] = None

        boss_config = (getattr(config_manager, "boss_config", {}) or {}) if config_manager else {}
        self._cfg = boss_config.get("small_event", {}) or {}
        self.enabled = bool(self._cfg.get("enabled", True))
        self.interval_min_hours = max(1.0, float(self._cfg.get("interval_min_hours", 3) or 3))
        self.interval_max_hours = max(
            self.interval_min_hours, float(self._cfg.get("interval_max_hours", 4) or 4)
        )
        self.active_start_hour = int(self._cfg.get("active_start_hour", 10) or 10)
        self.active_end_hour = int(self._cfg.get("active_end_hour", 24) or 24)
        self.cooldown = int(self._cfg.get("cooldown", 180) or 180)
        self.damage_floor_pct = float(self._cfg.get("damage_floor_pct", 1.0) or 1.0)
        self.damage_cap_pct = float(self._cfg.get("damage_cap_pct", 12.0) or 12.0)
        self.expire_hours = float(self._cfg.get("expire_hours", 2) or 2)
        self.escape_reward_pct = float(self._cfg.get("escape_reward_pct", 0.5) or 0.5)
        self._types = self._load_types()
        self._tier_meta = self._load_tier_meta()
        self._rewards = self._load_rewards()

    def _load_types(self) -> Dict[str, dict]:
        result = {k: dict(v) for k, v in self.EVENT_TYPES.items()}
        raw = self._cfg.get("types")
        if isinstance(raw, dict):
            for key, cfg in raw.items():
                if not isinstance(cfg, dict):
                    continue
                item = dict(result.get(str(key), {}))
                item.update(cfg)
                item.setdefault("name", key)
                result[str(key)] = item
        elif isinstance(raw, list):
            for idx, cfg in enumerate(raw):
                if not isinstance(cfg, dict):
                    continue
                key = str(cfg.get("key") or f"custom_event_{idx}")
                item = dict(result.get(key, {}))
                item.update(cfg)
                item.setdefault("name", key)
                result[key] = item
        return result

    def _load_tier_meta(self) -> Dict[int, dict]:
        result = {k: dict(v) for k, v in self.TIER_META.items()}
        raw = self._cfg.get("tiers")
        if isinstance(raw, dict):
            for key, cfg in raw.items():
                try:
                    tier = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(cfg, dict):
                    result.setdefault(tier, {}).update(cfg)
        elif isinstance(raw, list):
            for idx, cfg in enumerate(raw, start=1):
                if isinstance(cfg, dict):
                    result.setdefault(idx, {}).update(cfg)
        return result

    def _load_rewards(self) -> Dict[int, dict]:
        result = {k: dict(v) for k, v in self.TIER_REWARDS.items()}
        raw = self._cfg.get("rewards")
        if isinstance(raw, dict):
            for key, cfg in raw.items():
                try:
                    tier = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(cfg, dict):
                    result.setdefault(tier, {}).update(cfg)
        return result

    def is_active_time(self, now: Optional[int] = None) -> bool:
        dt = datetime.fromtimestamp(int(now if now is not None else time.time()))
        hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
        start = self.active_start_hour
        end = self.active_end_hour
        if start == end:
            return True
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def next_spawn_time(self, now: Optional[int] = None) -> int:
        now = int(now if now is not None else time.time())
        dt = datetime.fromtimestamp(now)
        start = int(self.active_start_hour)
        end = int(self.active_end_hour)
        if start >= end:
            return now + int(random.uniform(self.interval_min_hours, self.interval_max_hours) * 3600)

        today = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = today + timedelta(hours=start)
        end_dt = today + timedelta(hours=end)
        jitter = random.uniform(0, 600)

        if dt < start_dt:
            return int((start_dt + timedelta(seconds=jitter)).timestamp())
        if dt >= end_dt:
            return int((start_dt + timedelta(days=1, seconds=jitter)).timestamp())

        candidate = now + int(random.uniform(self.interval_min_hours, self.interval_max_hours) * 3600)
        if candidate >= int(end_dt.timestamp()):
            return int((start_dt + timedelta(days=1, seconds=jitter)).timestamp())
        return candidate

    async def _server_max_level(self) -> int:
        try:
            async with self.db.conn.execute("SELECT MAX(level_index) AS lv FROM players") as cursor:
                row = await cursor.fetchone()
            return int(row["lv"]) if row and row["lv"] is not None else 0
        except Exception:
            return 0

    def _choose_tier(self, max_level: int) -> int:
        eligible = {
            tier: cfg for tier, cfg in self._tier_meta.items()
            if int(cfg.get("min_level", 0) or 0) <= int(max_level)
        }
        if not eligible:
            lowest = min(int(cfg.get("min_level", 0) or 0) for cfg in self._tier_meta.values())
            eligible = {
                tier: cfg for tier, cfg in self._tier_meta.items()
                if int(cfg.get("min_level", 0) or 0) == lowest
            }
        tiers = list(eligible.keys())
        weights = [max(1, int(eligible[t].get("weight", 10) or 10)) for t in tiers]
        return random.choices(tiers, weights=weights, k=1)[0]

    def _choose_type(self) -> str:
        keys = list(self._types.keys())
        weights = [max(1, int(self._types[k].get("weight", 10) or 10)) for k in keys]
        return random.choices(keys, weights=weights, k=1)[0]

    def _event_type_cfg(self, event: dict) -> dict:
        key = str(event.get("event_key") or "")
        cfg = self._types.get(key)
        if cfg:
            return dict(cfg)
        return {
            "name": event.get("event_name") or "江湖事件",
            "icon": "🎲",
            "victory": "击败",
            "drop_table": "low",
            "desc": "",
        }

    def _tier_cfg(self, event: dict) -> dict:
        tier = int(event.get("tier") or 1)
        cfg = dict(self._tier_meta.get(tier, self._tier_meta.get(1, {})))
        cfg.setdefault("rank", "丁")
        cfg.setdefault("min_level", 0)
        cfg.setdefault("drop_table", "low")
        return cfg

    async def _base_hp(self) -> int:
        if self.world_boss_mgr:
            try:
                hp, _ = await self.world_boss_mgr.calculate_dynamic_hp()
                return max(20_000, int(hp))
            except Exception:
                pass
        try:
            players = await self.db.get_all_players()
            count = max(1, len(players))
            return max(20_000, count * 50_000)
        except Exception:
            return 500_000

    async def get_active_event(self) -> Optional[dict]:
        async with self.db.conn.execute(
            "SELECT * FROM battle_events WHERE status = 1 ORDER BY event_id DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def expire_events(self, now: Optional[int] = None) -> int:
        now = int(now if now is not None else time.time())
        async with self.db.conn.execute(
            "UPDATE battle_events SET status = 3 WHERE status = 1 AND expire_time <= ?",
            (now,),
        ) as cursor:
            changed = cursor.rowcount
        await self.db.conn.commit()
        return changed

    async def spawn_event(self, force_type: Optional[str] = None, force_tier: Optional[int] = None) -> Tuple[bool, str, Optional[dict]]:
        now = int(time.time())
        await self.expire_events(now)
        if await self.get_active_event():
            return False, "❌ 当前已有江湖事件正在进行，请先将其了结。", None

        max_level = await self._server_max_level()
        type_key = force_type if force_type in self._types else self._choose_type()
        tier = int(force_tier) if force_tier in self._tier_meta else self._choose_tier(max_level)
        type_cfg = dict(self._types.get(type_key, {}))
        tier_cfg = dict(self._tier_meta.get(tier, {}))

        base_hp = await self._base_hp()
        max_hp = int(base_hp * float(tier_cfg.get("hp_mult", 0.25) or 0.25))
        max_hp = max(20_000, min(max_hp, 5_000_000_000))
        min_level = int(tier_cfg.get("min_level", 0) or 0)
        expire_hours = float(tier_cfg.get("expire_hours", self.expire_hours) or self.expire_hours)
        expire_time = now + int(expire_hours * 3600)
        rank = str(tier_cfg.get("rank", "丁"))
        names = type_cfg.get("names") or [type_cfg.get("name", "无名之辈")]
        event_name = random.choice(names)
        elements = type_cfg.get("elements") or [""]
        element = random.choice(elements)
        locations = type_cfg.get("locations") or ["江湖之中"]
        location = random.choice(locations)
        intros = type_cfg.get("intro") or ["{location}传来消息，{rank}级{type}【{name}】现身！"]
        intro = random.choice(intros).format(
            rank=rank,
            type=type_cfg.get("name", "江湖事件"),
            name=event_name,
            location=location,
            desc=type_cfg.get("desc", ""),
        )

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.conn.execute(
                "SELECT event_id FROM battle_events WHERE status = 1 LIMIT 1"
            ) as cursor:
                if await cursor.fetchone():
                    await self.db.conn.rollback()
                    return False, "❌ 当前已有江湖事件正在进行，请先将其了结。", None
            async with self.db.conn.execute(
                """INSERT INTO battle_events
                   (event_key, event_name, tier, element, max_hp, current_hp, min_level,
                    spawn_time, expire_time, status, settled, killer_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, '')""",
                (type_key, event_name, tier, element, max_hp, max_hp, min_level,
                 now, expire_time),
            ) as cursor:
                event_id = cursor.lastrowid
            await self.db.conn.commit()
        except Exception as exc:
            await self.db.conn.rollback()
            logger.error(f"江湖事件生成异常: {exc}")
            return False, "❌ 江湖事件生成失败，请稍后再试。", None

        icon = type_cfg.get("icon", "🎲")
        message = (
            f"{icon} 【江湖快报】{intro}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🏷️ 目标：{rank}级{type_cfg.get('name', '江湖事件')}【{event_name}】\n"
            f"📖 {type_cfg.get('desc', '')}\n"
            f"🧭 地点：{location}\n"
            f"🚪 门槛境界：{min_level}\n"
            f"💀 气血：{max_hp:,}\n"
            f"⏳ 时限：{expire_hours:g} 小时\n"
            f"🎁 悬赏：按伤害排名发放灵石与随机掉落（尾刀额外重赏）\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 发送 /讨伐 参与围剿，/江湖事件 查看详情"
        )
        event = {
            "event_id": event_id,
            "event_key": type_key,
            "event_name": event_name,
            "tier": tier,
            "element": element,
            "max_hp": max_hp,
            "current_hp": max_hp,
        }
        return True, message, event

    async def challenge_event(self, player: Player) -> Tuple[bool, str]:
        self.last_challenge_ok = False
        self.last_kill = False
        self.last_settle_msg = None
        now = int(time.time())
        await self.expire_events(now)

        event = await self.get_active_event()
        if not event:
            return False, "🎲 当前没有正在进行的江湖事件，请等待江湖快报。"
        if now >= int(event.get("expire_time") or 0):
            return False, "💨 该江湖事件已经结束，请等待下一场风波。"

        type_cfg = self._event_type_cfg(event)
        tier_cfg = self._tier_cfg(event)
        min_level = int(event.get("min_level") or tier_cfg.get("min_level", 0) or 0)
        if player.level_index < min_level:
            return False, (
                f"❌ 境界不足，无法参与本次围剿！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"【{tier_cfg.get('rank', '丁')}级{type_cfg.get('name', '事件')}"
                f"·{event.get('event_name')}】需要境界等级 {min_level}\n"
                f"你当前境界等级：{player.level_index}（还差 {min_level - player.level_index} 级）\n"
                f"💡 通过 /闭关 /历练 提升修为后 /突破 提高境界"
            )

        cooldown = int(tier_cfg.get("cooldown", self.cooldown) or self.cooldown)
        async with self.db.conn.execute(
            "SELECT * FROM battle_event_damage WHERE event_id = ? AND user_id = ?",
            (event["event_id"], player.user_id),
        ) as cursor:
            record = await cursor.fetchone()
        rec = dict(record) if record else None
        if rec and now - int(rec.get("last_attack_time") or 0) < cooldown:
            remaining = cooldown - (now - int(rec.get("last_attack_time") or 0))
            return False, f"❌ 调息中，请 {remaining} 秒后再来。"

        if self.world_boss_mgr:
            final_damage, damage_percent, player_element = await self.world_boss_mgr.calculate_damage(
                player,
                int(event["max_hp"]),
                event.get("element", ""),
                floor_pct=self.damage_floor_pct,
                cap_pct=self.damage_cap_pct,
            )
        else:
            final_damage = max(1, int(event["max_hp"] * 0.02))
            damage_percent = 2.0
            player_element = ""

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.conn.execute(
                "SELECT * FROM battle_events WHERE event_id = ? AND status = 1",
                (event["event_id"],),
            ) as cursor:
                fresh_row = await cursor.fetchone()
            if not fresh_row:
                await self.db.conn.rollback()
                return False, "❌ 该江湖事件已被了结，请查看最新状态。"
            fresh = dict(fresh_row)
            if now >= int(fresh.get("expire_time") or 0):
                await self.db.conn.execute(
                    "UPDATE battle_events SET status = 3 WHERE event_id = ? AND status = 1",
                    (event["event_id"],),
                )
                await self.db.conn.commit()
                return False, "💨 该江湖事件已经结束，请等待下一场风波。"

            final_damage = min(final_damage, int(fresh["current_hp"]))
            if final_damage <= 0:
                await self.db.conn.rollback()
                return False, "❌ 目标已无剩余气血。"
            damage_percent = (final_damage / max(1, int(fresh["max_hp"]))) * 100
            new_hp = int(fresh["current_hp"]) - final_damage
            is_kill = new_hp <= 0

            if is_kill:
                new_hp = 0
                await self.db.conn.execute(
                    "UPDATE battle_events SET current_hp = 0, status = 2, settled = 0, killer_id = ? "
                    "WHERE event_id = ? AND status = 1",
                    (player.user_id, event["event_id"]),
                )
            else:
                await self.db.conn.execute(
                    "UPDATE battle_events SET current_hp = ? WHERE event_id = ? AND status = 1",
                    (new_hp, event["event_id"]),
                )

            async with self.db.conn.execute(
                "SELECT id, last_attack_time FROM battle_event_damage WHERE event_id = ? AND user_id = ?",
                (event["event_id"], player.user_id),
            ) as cursor:
                fresh_record = await cursor.fetchone()
            if fresh_record and now - int(fresh_record["last_attack_time"] or 0) < cooldown:
                await self.db.conn.rollback()
                remaining = cooldown - (now - int(fresh_record["last_attack_time"] or 0))
                return False, f"❌ 调息中，请 {remaining} 秒后再来。"
            if fresh_record:
                await self.db.conn.execute(
                    "UPDATE battle_event_damage SET total_damage = total_damage + ?, "
                    "attack_count = attack_count + 1, last_attack_time = ? WHERE id = ?",
                    (final_damage, now, fresh_record["id"]),
                )
            else:
                await self.db.conn.execute(
                    "INSERT INTO battle_event_damage "
                    "(event_id, user_id, total_damage, attack_count, last_attack_time) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (event["event_id"], player.user_id, final_damage, now),
                )
            await self.db.conn.commit()
        except Exception as exc:
            await self.db.conn.rollback()
            logger.error(f"江湖事件战斗异常: {exc}")
            return False, "❌ 战斗异常，请稍后再试。"

        self.last_challenge_ok = True
        msg = [
            f"⚔️ 你对【{fresh['event_name']}】造成了 {final_damage:,} 点伤害！",
            f"━━━━━━━━━━━━━━━",
            f"🏷️ 目标：{tier_cfg.get('rank', '丁')}级{type_cfg.get('name', '事件')} · {fresh.get('element') or '无'}属性",
            f"📊 本次伤害占比：{damage_percent:.2f}%",
            f"❤️ 剩余气血：{new_hp:,} / {int(fresh['max_hp']):,}",
        ]
        if player_element:
            try:
                from .combat_manager import CombatManager
                mult = CombatManager.element_multiplier(player_element, fresh.get("element", ""))
                if mult > 1:
                    msg.append(f"✨ 五行克制！你的【{player_element}】属性对目标造成额外伤害。")
                elif mult < 1:
                    msg.append(f"⚠️ 五行受制！你的【{player_element}】属性被目标压制。")
            except Exception:
                pass

        if is_kill:
            self.last_kill = True
            settle_msg = await self.settle_event(event["event_id"], player.user_id)
            self.last_settle_msg = settle_msg
            msg.append("\n" + (settle_msg or f"🎉 目标已被{type_cfg.get('victory', '击败')}，奖励正在结算。"))
        else:
            msg.append("\n💡 使用 /江湖事件 查看当前目标状态")

        return True, "\n".join(msg)

    async def settle_event(self, event_id: int, killer_id: str = "", escaped: Optional[bool] = None) -> str:
        async with self.db.conn.execute(
            "SELECT * FROM battle_events WHERE event_id = ?", (event_id,)
        ) as cursor:
            event_row = await cursor.fetchone()
        if not event_row:
            return "❌ 江湖事件记录不存在，无法结算。"
        event = dict(event_row)
        if int(event.get("settled") or 0):
            return ""

        if escaped is None:
            escaped = int(event.get("status") or 0) != 2
        async with self.db.conn.execute(
            "UPDATE battle_events SET settled = 1 WHERE event_id = ? AND settled = 0 AND status IN (2, 3)",
            (event_id,),
        ) as cursor:
            claimed = cursor.rowcount
        await self.db.conn.commit()
        if not claimed:
            return ""

        async with self.db.conn.execute(
            "SELECT * FROM battle_event_damage WHERE event_id = ? ORDER BY total_damage DESC",
            (event_id,),
        ) as cursor:
            damages = [dict(r) for r in await cursor.fetchall()]

        type_cfg = self._event_type_cfg(event)
        tier_cfg = self._tier_cfg(event)
        tier = int(event.get("tier") or 1)
        reward_cfg = self._rewards.get(tier, self._rewards.get(1, {}))
        reward_mult = self.escape_reward_pct if escaped else 1.0
        drop_table = tier_cfg.get("drop_table", type_cfg.get("drop_table", "low"))
        total_damage = sum(int(d.get("total_damage") or 0) for d in damages)
        participant_count = len(damages)
        rank = tier_cfg.get("rank", "丁")
        type_name = type_cfg.get("name", "江湖事件")
        icon = type_cfg.get("icon", "🎲")
        victory = type_cfg.get("victory", "击败")

        if escaped:
            header = [
                f"💨 【江湖事件·逃逸】{rank}级{type_name}【{event.get('event_name')}】突出重围，逃之夭夭！",
                "━━━━━━━━━━━━━━━",
                f"👥 参与修士：{participant_count} 人 · 总伤害：{total_damage:,}",
                f"💰 参与修士仍可获得悬赏的 {int(self.escape_reward_pct * 100)}%。",
            ]
        else:
            header = [
                f"🏁 【江湖事件·结算】{rank}级{type_name}【{event.get('event_name')}】已被{victory}！",
                "━━━━━━━━━━━━━━━",
                f"👥 参与修士：{participant_count} 人 · 总伤害：{total_damage:,}",
            ]

        if not damages:
            header.append("🌫️ 无人出手，此事不了了之。")
            self.last_settle_msg = "\n".join(header)
            return self.last_settle_msg

        for index, dmg in enumerate(damages):
            rank_no = index + 1
            target_player = await self.db.get_player_by_id(dmg["user_id"])
            if not target_player:
                continue

            display_name = target_player.user_name or dmg["user_id"]
            if target_player.title:
                display_name = f"【{target_player.title}】{display_name}"

            is_killer = str(dmg["user_id"]) == str(killer_id)
            if rank_no == 1:
                gold = int(reward_cfg.get("rank1", 6000))
            elif rank_no <= 3:
                gold = int(reward_cfg.get("top3", 3000))
            else:
                gold = int(reward_cfg.get("participate", 1000))
            if not escaped and is_killer:
                gold += int(reward_cfg.get("killer", 5000))
            gold = max(0, int(gold * reward_mult))
            try:
                from ..core.technique_bonus import TechniqueBonusManager
                gold = TechniqueBonusManager.apply_gold_bonus(
                    target_player, gold, self.config_manager
                )
            except Exception:
                pass

            await self.db.add_player_gold(target_player.user_id, gold)
            target_player.gold += gold

            collected: Dict[str, int] = {}
            lost: Dict[str, int] = {}
            if not escaped:
                if is_killer:
                    rolls = 2 + tier
                elif rank_no <= 3:
                    rolls = 2
                else:
                    rolls = 1 if random.random() < 0.6 else 0
                drops_to_grant: List[Tuple[str, int]] = []
                if is_killer and self.world_boss_mgr:
                    first_drop = self.world_boss_mgr.roll_drop(drop_table)
                    if first_drop:
                        drops_to_grant.append(first_drop)
                for _ in range(rolls):
                    if self.world_boss_mgr:
                        drop = self.world_boss_mgr.roll_drop(drop_table)
                        if drop:
                            drops_to_grant.append(drop)
                try:
                    from ..core.technique_bonus import TechniqueBonusManager
                    tech_name = TechniqueBonusManager.roll_technique_drop(
                        target_player,
                        self.config_manager,
                        base_chance=6 + max(0, tier) * 2,
                        allow_main=is_killer,
                        main_ratio=0.08,
                    )
                    if tech_name:
                        drops_to_grant.append((tech_name, 1))
                except Exception:
                    pass
                for item_name, count in drops_to_grant:
                    stored = await self._store_item(target_player, item_name, count)
                    bucket = collected if stored else lost
                    bucket[item_name] = bucket.get(item_name, 0) + count

            item_lines = [f"{name}x{count}" for name, count in collected.items()]
            item_lines.extend(f"{name}x{count}（储物戒已满）" for name, count in lost.items())
            tag = "👑 尾刀" if (is_killer and not escaped) else ("🥇" if rank_no == 1 else ("🥈" if rank_no <= 3 else "🥉"))
            line = f"{tag} {rank_no}. {display_name} - {gold:,}灵石"
            if item_lines:
                line += " · " + "、".join(item_lines)
            header.append(line)

        header.append("━━━━━━━━━━━━━━━")
        header.append("💰 悬赏奖励已发放至各位修士的账户/储物戒！")
        self.last_settle_msg = "\n".join(header)
        return self.last_settle_msg

    async def _store_item(self, player: Player, item_name: str, count: int) -> bool:
        if not self.storage_ring_mgr:
            return False
        try:
            success, _ = await self.storage_ring_mgr.store_item(
                player, item_name, count, silent=True
            )
            return bool(success)
        except Exception as exc:
            logger.warning(f"江湖事件掉落发放失败 {item_name}x{count}: {exc}")
            return False

    async def settle_pending_events(self) -> List[str]:
        async with self.db.conn.execute(
            "SELECT event_id, status, killer_id FROM battle_events WHERE status IN (2, 3) AND settled = 0"
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        messages = []
        for row in rows:
            try:
                msg = await self.settle_event(
                    int(row["event_id"]),
                    killer_id=str(row.get("killer_id") or ""),
                    escaped=int(row.get("status") or 0) == 3,
                )
                if msg:
                    messages.append(msg)
            except Exception as exc:
                logger.error(f"补结算江湖事件 {row.get('event_id')} 失败: {exc}")
        return messages

    async def next_spawn_remaining(self) -> Optional[int]:
        try:
            raw = await self.db.ext.get_system_config(self.KEY_NEXT_SPAWN)
            if not raw:
                return None
            return max(0, int(raw) - int(time.time()))
        except (TypeError, ValueError):
            return None
