# managers/raid_manager.py
import time
import random
from typing import Optional, Tuple, List, Dict
from astrbot.api import logger
from ..data import DataBase
from ..models import Player


class RaidManager:
    """全服团本管理器 - 多阶段Boss、团队协作奖励与掉落"""

    RAID_TYPES: Dict[str, dict] = {
        "flame_abyss": {
            "name": "焚天熔渊", "element": "火", "difficulty": 1, "icon": "🌋",
            "phases": 2, "hp_mult": 1.2, "min_level": 6, "min_players": 2,
            "drop_table": "mid", "expire_hours": 36,
            "desc": "地心熔渊崩裂，火灵汇聚成灾。需团队轮番压制，方可熄灭焚天之焰。",
        },
        "yellow_springs": {
            "name": "九幽黄泉", "element": "水", "difficulty": 2, "icon": "🌊",
            "phases": 3, "hp_mult": 1.8, "min_level": 9, "min_players": 3,
            "drop_table": "high", "expire_hours": 48,
            "desc": "黄泉倒灌，万鬼同哭。三重冥关层层递进，非齐心不可破。",
        },
        "chaos_palace": {
            "name": "混沌天宫", "element": "土", "difficulty": 3, "icon": "🏯",
            "phases": 4, "hp_mult": 2.5, "min_level": 12, "min_players": 4,
            "drop_table": "high", "expire_hours": 72,
            "desc": "混沌古天宫现世，四重天阙皆由太古神纹守护，乃当世最凶险的团本。",
        },
    }

    RAID_REWARDS: Dict[int, dict] = {
        1: {"rank1": 60000, "top3": 35000, "participate": 15000, "killer": 30000},
        2: {"rank1": 120000, "top3": 70000, "participate": 25000, "killer": 60000},
        3: {"rank1": 250000, "top3": 140000, "participate": 50000, "killer": 120000},
    }

    DIFFICULTY_STARS = {1: "★☆☆", 2: "★★☆", 3: "★★★"}

    def __init__(self, db: DataBase, config_manager, storage_ring_mgr, world_boss_mgr, title_mgr=None):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_mgr = storage_ring_mgr
        self.world_boss_mgr = world_boss_mgr
        self.title_mgr = title_mgr
        self.last_challenge_ok = False
        self.last_settle_msg: Optional[str] = None
        self.last_broadcast_msg: Optional[str] = None
        self._config = (getattr(config_manager, "boss_config", {}) or {}) if config_manager else {}
        self._raid_cfg = self._config.get("raid", {}) or {}
        self._types = self._load_types()
        self._rewards = self._load_rewards()
        self.enabled = bool(self._raid_cfg.get("enabled", True))
        self.auto_spawn_interval = int(self._raid_cfg.get("auto_spawn_interval", 21600) or 21600)
        self.expire_hours = int(self._raid_cfg.get("expire_hours", 48) or 48)
        self.cooldown = int(self._raid_cfg.get("cooldown", 600) or 600)
        self.damage_floor_pct = float(self._raid_cfg.get("damage_floor_pct", 0.3) or 0.3)
        self.damage_cap_pct = float(self._raid_cfg.get("damage_cap_pct", 8.0) or 8.0)
        self.min_players = int(self._raid_cfg.get("min_players", 2) or 2)

    def _load_types(self) -> Dict[str, dict]:
        raw = self._raid_cfg.get("types")
        if isinstance(raw, dict) and raw:
            result = {k: dict(v) for k, v in self.RAID_TYPES.items()}
            for key, cfg in raw.items():
                if not isinstance(cfg, dict):
                    continue
                item = dict(result.get(str(key), {}))
                item.update(cfg)
                item.setdefault("name", key)
                result[str(key)] = item
            return result
        if isinstance(raw, list) and raw:
            result = {k: dict(v) for k, v in self.RAID_TYPES.items()}
            for idx, cfg in enumerate(raw):
                if not isinstance(cfg, dict):
                    continue
                key = str(cfg.get("key") or f"custom_raid_{idx}")
                item = dict(result.get(key, {}))
                item.update(cfg)
                item.setdefault("name", key)
                result[key] = item
            return result
        return {k: dict(v) for k, v in self.RAID_TYPES.items()}

    def _load_rewards(self) -> Dict[int, dict]:
        raw = self._raid_cfg.get("rewards")
        result = {k: dict(v) for k, v in self.RAID_REWARDS.items()}
        if isinstance(raw, dict):
            for key, cfg in raw.items():
                try:
                    diff = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(cfg, dict):
                    result.setdefault(diff, {}).update(cfg)
        return result

    def _type_cfg(self, raid: dict) -> dict:
        key = str(raid.get("boss_type") or "")
        cfg = self._types.get(key)
        if not cfg:
            cfg = next((v for v in self._types.values() if v.get("name") == raid.get("boss_name")), None)
        if not cfg:
            cfg = {
                "name": raid.get("boss_name") or "未知团本", "element": raid.get("element") or "",
                "difficulty": int(raid.get("difficulty") or 1), "icon": "🏯",
                "phases": int(raid.get("total_phases") or 3), "hp_mult": 1.0,
                "min_level": int(raid.get("min_level") or 6), "min_players": self.min_players,
                "drop_table": "high", "expire_hours": self.expire_hours, "desc": "",
            }
        cfg = dict(cfg)
        cfg.setdefault("difficulty", int(raid.get("difficulty") or 1))
        cfg.setdefault("phases", int(raid.get("total_phases") or 3))
        cfg.setdefault("min_level", int(raid.get("min_level") or 6))
        cfg.setdefault("min_players", self.min_players)
        cfg.setdefault("element", raid.get("element") or "")
        cfg.setdefault("drop_table", "high")
        cfg.setdefault("icon", "🏯")
        return cfg

    def _choose_raid_key(self, difficulty: Optional[int] = None, max_level: Optional[int] = None) -> str:
        if difficulty:
            candidates = [k for k, v in self._types.items() if int(v.get("difficulty", 1)) == int(difficulty)]
            if candidates:
                return random.choice(candidates)
        candidates = dict(self._types)
        if max_level is not None:
            filtered = {
                k: v for k, v in candidates.items()
                if int(v.get("min_level", 6) or 6) <= int(max_level)
            }
            if filtered:
                candidates = filtered
            else:
                lowest = min(int(v.get("min_level", 6) or 6) for v in candidates.values())
                candidates = {
                    k: v for k, v in candidates.items()
                    if int(v.get("min_level", 6) or 6) == lowest
                }
        keys = list(candidates.keys())
        weights = [max(1, 5 - int(candidates[k].get("difficulty", 1))) for k in keys]
        if not keys:
            return "flame_abyss"
        return random.choices(keys, weights=weights, k=1)[0]

    async def _server_max_level(self) -> int:
        try:
            async with self.db.conn.execute("SELECT MAX(level_index) AS lv FROM players") as cursor:
                row = await cursor.fetchone()
            return int(row["lv"]) if row and row["lv"] is not None else 0
        except Exception:
            return 0

    async def expire_raids(self, now: Optional[int] = None) -> int:
        if now is None:
            now = int(time.time())
        async with self.db.conn.execute(
            "UPDATE world_boss_raids SET status = 3 WHERE status = 1 AND expire_time <= ?",
            (now,),
        ) as cursor:
            changed = cursor.rowcount
        await self.db.conn.commit()
        return changed

    async def get_active_raid(self) -> Optional[dict]:
        async with self.db.conn.execute(
            "SELECT * FROM world_boss_raids WHERE status = 1 LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def spawn_raid(self, difficulty: Optional[int] = None) -> Tuple[bool, str]:
        now = int(time.time())
        await self.expire_raids(now)

        max_level = await self._server_max_level()
        raid_key = self._choose_raid_key(difficulty, max_level)
        type_cfg = dict(self._types.get(raid_key, {}))
        raid_name = type_cfg.get("name") or raid_key
        element = type_cfg.get("element") or ""
        diff = int(type_cfg.get("difficulty", difficulty or 1) or 1)
        phases = max(1, int(type_cfg.get("phases", 3) or 3))
        min_level = int(type_cfg.get("min_level", 6) or 6)
        min_players = int(type_cfg.get("min_players", self.min_players) or self.min_players)
        expire_hours = int(type_cfg.get("expire_hours", self.expire_hours) or self.expire_hours)

        if self.world_boss_mgr:
            base_hp, player_count = await self.world_boss_mgr.calculate_dynamic_hp()
        else:
            players = await self.db.get_all_players()
            player_count = len(players)
            base_hp = 1_000_000

        total_hp = int(base_hp * float(type_cfg.get("hp_mult", 1.0) or 1.0))
        total_hp = max(1_000_000, min(total_hp, 20_000_000_000))
        phase_hp = max(1, total_hp // phases)
        expire_time = now + expire_hours * 3600

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.conn.execute(
                "SELECT raid_id FROM world_boss_raids WHERE status = 1 LIMIT 1"
            ) as cursor:
                if await cursor.fetchone():
                    await self.db.conn.rollback()
                    return False, "❌ 当前已有团本正在进行中！"
            await self.db.conn.execute(
                """INSERT INTO world_boss_raids
                   (boss_name, boss_type, element, difficulty, max_hp, current_hp, phase,
                    total_phases, phase_max_hp, min_level, min_players, status, spawn_time,
                    expire_time, settled, total_damage)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 1, ?, ?, 0, 0)""",
                (raid_name, raid_key, element, diff, phase_hp * phases, phase_hp, phases,
                 phase_hp, min_level, min_players, now, expire_time),
            )
            await self.db.conn.commit()
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"团本生成异常: {e}")
            return False, "❌ 团本生成失败，请稍后再试。"

        stars = self.DIFFICULTY_STARS.get(diff, "★☆☆")
        return True, (
            f"{type_cfg.get('icon', '🏯')} 【天道警示】团本降临！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🏯 团本：{raid_name}\n"
            f"🏷️ 难度：{stars} · {element or '无'}属性\n"
            f"🧩 阶段：共 {phases} 阶段，每阶段 {phase_hp:,} 血量\n"
            f"👥 建议人数：至少 {min_players} 人 · 门槛境界 {min_level}\n"
            f"⏳ 开放时间：{expire_hours}小时（基于全服 {player_count} 名活跃修士）\n"
            f"📖 {type_cfg.get('desc', '')}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 发送 /挑战团本 合力讨伐！"
        )

    async def challenge_raid(self, player: Player) -> Tuple[bool, str]:
        self.last_challenge_ok = False
        self.last_broadcast_msg = None
        now = int(time.time())
        await self.expire_raids(now)

        raid = await self.get_active_raid()
        if not raid:
            return False, "❌ 当前没有正在进行的团本。"
        if now >= int(raid.get("expire_time") or 0):
            return False, "❌ 团本已关闭，请等待下次开启。"

        type_cfg = self._type_cfg(raid)
        min_level = int(raid.get("min_level") or type_cfg.get("min_level", 6) or 6)
        if player.level_index < min_level:
            return False, (
                f"❌ 境界不足，无法进入团本！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"【{raid.get('boss_name')}】需要境界等级 {min_level}\n"
                f"你当前境界等级：{player.level_index}（还差 {min_level - player.level_index} 级）"
            )

        async with self.db.conn.execute(
            "SELECT * FROM world_boss_raid_damage WHERE raid_id = ? AND user_id = ?",
            (raid["raid_id"], player.user_id),
        ) as cursor:
            record = await cursor.fetchone()
        rec = dict(record) if record else None
        if rec and now - int(rec.get("last_attack_time") or 0) < self.cooldown:
            remaining = self.cooldown - (now - int(rec.get("last_attack_time") or 0))
            minutes, seconds = divmod(max(0, remaining), 60)
            return False, f"❌ 团本调息中，请 {minutes} 分 {seconds} 秒后再来。"

        phase_max_hp = max(1, int(raid.get("phase_max_hp") or raid.get("max_hp") or 1))
        if self.world_boss_mgr:
            final_damage, damage_percent, player_element = await self.world_boss_mgr.calculate_damage(
                player, phase_max_hp, type_cfg.get("element", ""),
                floor_pct=self.damage_floor_pct, cap_pct=self.damage_cap_pct,
            )
        else:
            final_damage = max(1, int(phase_max_hp * 0.02))
            damage_percent = 2.0
            player_element = ""

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.conn.execute(
                "SELECT * FROM world_boss_raids WHERE raid_id = ? AND status = 1",
                (raid["raid_id"],),
            ) as cursor:
                fresh_row = await cursor.fetchone()
            if not fresh_row:
                await self.db.conn.rollback()
                return False, "❌ 该团本已结束，请查看最新状态。"
            fresh = dict(fresh_row)
            if now >= int(fresh.get("expire_time") or 0):
                await self.db.conn.execute(
                    "UPDATE world_boss_raids SET status = 3 WHERE raid_id = ? AND status = 1",
                    (raid["raid_id"],),
                )
                await self.db.conn.commit()
                return False, "❌ 团本已关闭，请等待下次开启。"

            current_hp = int(fresh["current_hp"])
            actual_damage = min(final_damage, current_hp)
            new_hp = current_hp - actual_damage
            phase = int(fresh["phase"])
            total_phases = int(fresh["total_phases"])
            phase_max_hp = int(fresh["phase_max_hp"])
            phase_cleared = False
            is_kill = False
            new_phase = phase

            if new_hp <= 0:
                if phase < total_phases:
                    new_phase = phase + 1
                    new_hp = phase_max_hp
                    phase_cleared = True
                else:
                    new_hp = 0
                    new_phase = total_phases
                    is_kill = True

            if is_kill:
                await self.db.conn.execute(
                    "UPDATE world_boss_raids SET current_hp = 0, phase = ?, status = 2, "
                    "settled = 0, total_damage = total_damage + ? WHERE raid_id = ? AND status = 1",
                    (new_phase, actual_damage, raid["raid_id"]),
                )
            else:
                await self.db.conn.execute(
                    "UPDATE world_boss_raids SET current_hp = ?, phase = ?, "
                    "total_damage = total_damage + ? WHERE raid_id = ? AND status = 1",
                    (new_hp, new_phase, actual_damage, raid["raid_id"]),
                )

            async with self.db.conn.execute(
                "SELECT id, last_attack_time FROM world_boss_raid_damage "
                "WHERE raid_id = ? AND user_id = ?",
                (raid["raid_id"], player.user_id),
            ) as cursor:
                fresh_record = await cursor.fetchone()
            if fresh_record and now - int(fresh_record["last_attack_time"] or 0) < self.cooldown:
                await self.db.conn.rollback()
                remaining = self.cooldown - (now - int(fresh_record["last_attack_time"] or 0))
                minutes, seconds = divmod(max(0, remaining), 60)
                return False, f"❌ 团本调息中，请 {minutes} 分 {seconds} 秒后再来。"
            if fresh_record:
                await self.db.conn.execute(
                    "UPDATE world_boss_raid_damage SET total_damage = total_damage + ?, "
                    "attack_count = attack_count + 1, last_attack_time = ? WHERE id = ?",
                    (actual_damage, now, fresh_record["id"]),
                )
            else:
                await self.db.conn.execute(
                    "INSERT INTO world_boss_raid_damage "
                    "(raid_id, user_id, total_damage, attack_count, last_attack_time) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (raid["raid_id"], player.user_id, actual_damage, now),
                )
            await self.db.conn.commit()
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"团本战斗异常: {e}")
            return False, "❌ 战斗异常，请稍后再试。"

        self.last_challenge_ok = True
        msg = [
            f"⚔️ 你对团本【{fresh['boss_name']}】造成了 {actual_damage:,} 点伤害！",
            "━━━━━━━━━━━━━━━",
            f"🧩 当前阶段：{new_phase}/{total_phases}",
            f"📊 本次伤害占比：{damage_percent:.2f}%",
            f"❤️ 本阶段剩余血量：{new_hp:,} / {phase_max_hp:,}",
        ]
        if phase_cleared:
            msg.append(f"\n🌋 第 {phase} 阶段已击破！团本进入第 {new_phase} 阶段！")
            self.last_broadcast_msg = (
                f"🌋 【团本推进】{fresh['boss_name']} 第 {phase} 阶段已被击破！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"⚔️ 击破者：{player.user_name or player.user_id}\n"
                f"🧩 当前阶段：{new_phase}/{total_phases}\n"
                f"💡 发送 /挑战团本 继续讨伐！"
            )
        if is_kill:
            settle_msg = await self.settle_raid(raid["raid_id"], player.user_id)
            msg.append("\n" + (settle_msg or "🎉 团本已通关，奖励正在结算。"))
            self.last_broadcast_msg = settle_msg
        elif not phase_cleared:
            msg.append("\n💡 使用 /团本状态 查看进度与队伍伤害榜")
        return True, "\n".join(msg)

    async def settle_raid(self, raid_id: int, killer_id: str = "") -> str:
        async with self.db.conn.execute(
            "SELECT * FROM world_boss_raids WHERE raid_id = ?", (raid_id,)
        ) as cursor:
            raid_row = await cursor.fetchone()
        if not raid_row:
            return "❌ 团本记录不存在，无法结算。"
        raid = dict(raid_row)
        if int(raid.get("settled") or 0):
            return ""
        async with self.db.conn.execute(
            "UPDATE world_boss_raids SET status = 4, settled = 1 "
            "WHERE raid_id = ? AND settled = 0 AND status = 2",
            (raid_id,),
        ) as cursor:
            claimed = cursor.rowcount
        await self.db.conn.commit()
        if not claimed:
            return ""

        async with self.db.conn.execute(
            "SELECT * FROM world_boss_raid_damage WHERE raid_id = ? ORDER BY total_damage DESC",
            (raid_id,),
        ) as cursor:
            damages = [dict(r) for r in await cursor.fetchall()]

        if not damages:
            return "🏯 团本已通关，但无人造成有效伤害，奖励作废。"

        # 断线补结算时没有传 killer：以最后一次攻击者作为尾刀者，避免斩首奖励丢失
        if not killer_id:
            async with self.db.conn.execute(
                "SELECT user_id FROM world_boss_raid_damage WHERE raid_id = ? "
                "ORDER BY last_attack_time DESC LIMIT 1",
                (raid_id,),
            ) as cursor:
                killer_row = await cursor.fetchone()
            if killer_row:
                killer_id = str(killer_row[0])

        type_cfg = self._type_cfg(raid)
        diff = int(raid.get("difficulty") or type_cfg.get("difficulty", 1) or 1)
        reward_cfg = self._rewards.get(diff, self._rewards.get(1, {}))
        drop_table = type_cfg.get("drop_table", "high")
        min_players = int(raid.get("min_players") or type_cfg.get("min_players", self.min_players) or 1)
        participant_count = len(damages)
        total_damage = sum(int(d.get("total_damage") or 0) for d in damages)
        team_mult = 1.0
        understaffed = participant_count < min_players
        if understaffed:
            team_mult = 0.6
        else:
            team_mult = 1.0 + min(0.5, 0.1 * (participant_count - min_players))

        stars = self.DIFFICULTY_STARS.get(diff, "★☆☆")
        msg = [
            "🎉 团本通关！",
            "━━━━━━━━━━━━━━━",
            f"{type_cfg.get('icon', '🏯')} {raid.get('boss_name')} · {stars}",
            f"👥 参与修士：{participant_count} 人 · 总伤害：{total_damage:,}",
        ]
        if understaffed:
            msg.append(f"⚠️ 参战人数不足 {min_players} 人，团队奖励降至 60%。")
        elif team_mult > 1.0:
            msg.append(f"🤝 团队协作加成：奖励 ×{team_mult:.1f}")

        for index, dmg in enumerate(damages):
            rank = index + 1
            target_player = await self.db.get_player_by_id(dmg["user_id"])
            if not target_player:
                continue
            display_name = target_player.user_name or dmg["user_id"]
            if target_player.title:
                display_name = f"【{target_player.title}】{display_name}"
            is_killer = str(dmg["user_id"]) == str(killer_id)

            if rank == 1:
                gold = int(reward_cfg.get("rank1", 60000))
            elif rank <= 3:
                gold = int(reward_cfg.get("top3", 35000))
            else:
                gold = int(reward_cfg.get("participate", 15000))
            if is_killer:
                gold += int(reward_cfg.get("killer", 30000))
            gold = max(0, int(gold * team_mult))

            await self.db.add_player_gold(target_player.user_id, gold)
            target_player.gold += gold

            collected = {}
            lost = {}
            drops_to_grant = []
            if is_killer:
                bonus_drop = self.world_boss_mgr.roll_drop(drop_table) if self.world_boss_mgr else None
                if bonus_drop:
                    drops_to_grant.append(bonus_drop)
            rolls = 3 if is_killer else (2 if rank <= 3 else 1)
            for _ in range(rolls):
                drop = self.world_boss_mgr.roll_drop(drop_table) if self.world_boss_mgr else None
                if drop:
                    drops_to_grant.append(drop)
            for item_name, count in drops_to_grant:
                stored = await self._store_item(target_player, item_name, count)
                bucket = collected if stored else lost
                bucket[item_name] = bucket.get(item_name, 0) + count
            item_lines = [f"{name}x{count}" for name, count in collected.items()]
            item_lines.extend(f"{name}x{count}（储物戒已满）" for name, count in lost.items())

            tag = "👑 斩首" if is_killer else ("🥇" if rank == 1 else ("🥈" if rank <= 3 else "🥉"))
            line = f"{tag} {rank}. {display_name} - {gold:,}灵石"
            if item_lines:
                line += " · " + "、".join(item_lines)
            msg.append(line)

        msg.append("━━━━━━━━━━━━━━━")
        msg.append("💡 团本奖励已发放至各位修士的储物戒/账户！")
        self.last_settle_msg = "\n".join(msg)
        return self.last_settle_msg

    async def _store_item(self, player: Player, item_name: str, count: int) -> bool:
        if not self.storage_ring_mgr:
            return False
        try:
            success, _ = await self.storage_ring_mgr.store_item(player, item_name, count, silent=True)
            return bool(success)
        except Exception as e:
            logger.warning(f"团本掉落发放失败 {item_name}x{count}: {e}")
            return False

    async def settle_pending_raids(self) -> List[str]:
        async with self.db.conn.execute(
            "SELECT raid_id, boss_name FROM world_boss_raids WHERE status = 2 AND settled = 0"
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        messages = []
        for row in rows:
            try:
                msg = await self.settle_raid(int(row["raid_id"]))
                if msg:
                    messages.append(msg)
            except Exception as e:
                logger.error(f"补结算团本 {row.get('raid_id')} 失败: {e}")
        return messages

    async def force_end_raid(self) -> Tuple[bool, str]:
        raid = await self.get_active_raid()
        if not raid:
            return False, "❌ 当前没有正在进行的团本。"
        async with self.db.conn.execute(
            "UPDATE world_boss_raids SET status = 3, settled = 1 WHERE raid_id = ? AND status = 1",
            (raid["raid_id"],),
        ) as cursor:
            changed = cursor.rowcount
        await self.db.conn.commit()
        if not changed:
            return False, "❌ 团本状态已变化，请刷新后重试。"
        return True, f"🏯 团本【{raid['boss_name']}】已被天道关闭，无奖励结算。"

    async def get_status(self) -> str:
        now = int(time.time())
        await self.expire_raids(now)
        raid = await self.get_active_raid()
        if not raid:
            return (
                "🏯 当前没有正在进行的团本。\n"
                "━━━━━━━━━━━━━━━\n"
                f"⏳ 下次天道开团预计在 {self.auto_spawn_interval // 3600} 小时内。\n"
                "💡 管理员可发送 /开启团本 [1-3] 手动开启。"
            )

        type_cfg = self._type_cfg(raid)
        remaining = max(0, int(raid.get("expire_time") or 0) - now)
        hours, minutes = divmod(remaining // 60, 60)
        phase = int(raid.get("phase") or 1)
        total_phases = int(raid.get("total_phases") or 1)
        current_hp = int(raid.get("current_hp") or 0)
        phase_max_hp = max(1, int(raid.get("phase_max_hp") or current_hp or 1))
        hp_percent = (current_hp / phase_max_hp) * 100
        diff = int(raid.get("difficulty") or 1)
        stars = self.DIFFICULTY_STARS.get(diff, "★☆☆")

        async with self.db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM world_boss_raid_damage WHERE raid_id = ?",
            (raid["raid_id"],),
        ) as cursor:
            row = await cursor.fetchone()
        participant_count = int(row["cnt"]) if row else 0
        drop_names = ""
        if self.world_boss_mgr:
            drop_names = "、".join(
                item.get("name", "")
                for item in self.world_boss_mgr._drop_tables.get(type_cfg.get("drop_table", "high"), [])
            )

        msg = [
            f"{type_cfg.get('icon', '🏯')} 团本：{raid.get('boss_name')}",
            "━━━━━━━━━━━━━━━",
            f"🏷️ 难度：{stars} · {raid.get('element') or type_cfg.get('element', '') or '无'}属性",
            f"🧩 阶段：{phase}/{total_phases}",
            f"❤️ 本阶段血量：{current_hp:,} / {phase_max_hp:,} ({hp_percent:.1f}%)",
            f"👥 已参战：{participant_count} 人 · 建议至少 {int(raid.get('min_players') or 2)} 人",
            f"⏳ 剩余时间：{hours} 小时 {minutes} 分",
            f"📊 累计伤害：{int(raid.get('total_damage') or 0):,}",
            f"🎁 掉落：{drop_names or '稀有材料'}",
            "━━━━━━━━━━━━━━━",
            f"💡 发送 /挑战团本 参与讨伐（{self.cooldown // 60} 分钟CD）",
            "💡 发送 /团本排行 查看队伍伤害榜",
        ]
        return "\n".join(msg)

    async def get_ranking(self) -> str:
        raid = await self.get_active_raid()
        if not raid:
            return "🏯 当前没有正在进行的团本。"
        async with self.db.conn.execute(
            "SELECT user_id, total_damage, attack_count FROM world_boss_raid_damage "
            "WHERE raid_id = ? ORDER BY total_damage DESC LIMIT 10",
            (raid["raid_id"],),
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        if not rows:
            return f"🏯 【{raid.get('boss_name')}】暂无修士造成伤害，快来开荒！"
        total = int(raid.get("total_damage") or 0) or sum(int(r.get("total_damage") or 0) for r in rows)
        lines = [f"📊 【{raid.get('boss_name')}】团本伤害榜", "━━━━━━━━━━━━━━━"]
        for index, row in enumerate(rows, start=1):
            player = await self.db.get_player_by_id(row["user_id"])
            name = (player.user_name if player and player.user_name else row["user_id"])
            if player and player.title:
                name = f"【{player.title}】{name}"
            percent = (int(row["total_damage"]) / total * 100) if total else 0
            lines.append(
                f"{index}. {name} - {int(row['total_damage']):,}伤害 ({percent:.1f}%) · {int(row['attack_count'])}次"
            )
        lines.append("━━━━━━━━━━━━━━━")
        return "\n".join(lines)
