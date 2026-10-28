# handlers/combat_handlers.py
import re
import time
from astrbot.api.event import AstrMessageEvent
from astrbot.api.all import *
from ..managers.combat_manager import CombatManager, CombatStats
from ..data.data_manager import DataBase
from ..core import EquipmentManager, PillManager
from .utils import player_required, set_event_flag
from ..models import Player
from ..models_extended import UserStatus

DUEL_COOLDOWN = 300
SPAR_COOLDOWN = 60

class CombatHandlers:
    def __init__(self, db: DataBase, combat_mgr: CombatManager, config_manager=None):
        self.db = db
        self.combat_mgr = combat_mgr
        self.config_manager = config_manager
        self.equipment_mgr = EquipmentManager(db, config_manager)
        self.pill_mgr = PillManager(db, config_manager)
        self.last_duel_result = None
        self.last_spar_fought = False
    
    async def _get_combat_cooldown(self, user_id: str) -> dict:
        try:
            async with self.db.conn.execute("SELECT last_duel_time, last_spar_time FROM combat_cooldowns WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if row: return {"last_duel_time": row[0], "last_spar_time": row[1]}
        except: pass
        return {"last_duel_time": 0, "last_spar_time": 0}
    
    async def _update_combat_cooldown(self, user_id: str, combat_type: str):
        now = int(time.time())
        try:
            if combat_type == "duel":
                await self.db.conn.execute("INSERT INTO combat_cooldowns (user_id, last_duel_time, last_spar_time) VALUES (?, ?, 0) ON CONFLICT(user_id) DO UPDATE SET last_duel_time = ?", (user_id, now, now))
            else:
                await self.db.conn.execute("INSERT INTO combat_cooldowns (user_id, last_duel_time, last_spar_time) VALUES (?, 0, ?) ON CONFLICT(user_id) DO UPDATE SET last_spar_time = ?", (user_id, now, now))
            await self.db.conn.commit()
        except: pass

    def _combat_config(self) -> dict:
        try:
            return (getattr(self.config_manager, "game_config", {}) or {}).get("combat", {}) or {}
        except Exception:
            return {}

    def _daily_limit(self, key: str, default: int) -> int:
        try:
            return max(1, int(self._combat_config().get(key, default)))
        except (TypeError, ValueError):
            return default

    def _spar_request_expire(self) -> int:
        try:
            return max(30, int(self._combat_config().get("spar_request_expire", 300)))
        except (TypeError, ValueError):
            return 300

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    async def _get_daily_usage(self, user_id: str) -> dict:
        """读取玩家今日切磋/决斗次数。"""
        try:
            async with self.db.conn.execute(
                "SELECT duel_count, spar_count FROM combat_daily WHERE user_id = ? AND day = ?",
                (str(user_id), self._today()),
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                return {"duel_count": int(row[0] or 0), "spar_count": int(row[1] or 0)}
        except Exception:
            pass
        return {"duel_count": 0, "spar_count": 0}

    async def _consume_daily_pair(self, user_ids, field: str, limit: int):
        """原子消耗双方每日次数，返回 (是否成功, 达到上限的用户ID)。"""
        if field not in ("duel_count", "spar_count"):
            return True, None
        unique_ids = list(dict.fromkeys(str(uid) for uid in user_ids if uid))
        today = self._today()
        # 使用 SAVEPOINT 而非 BEGIN IMMEDIATE：即使调用方已有未提交事务
        # 也能安全嵌套，避免 “cannot start a transaction within a transaction”。
        await self.db.conn.execute("SAVEPOINT combat_daily_pair")
        try:
            for uid in unique_ids:
                async with self.db.conn.execute(
                    f"SELECT {field} FROM combat_daily WHERE user_id = ? AND day = ?",
                    (uid, today),
                ) as cursor:
                    row = await cursor.fetchone()
                current = int(row[0] or 0) if row else 0
                if current >= limit:
                    await self.db.conn.execute("ROLLBACK TO SAVEPOINT combat_daily_pair")
                    await self.db.conn.execute("RELEASE SAVEPOINT combat_daily_pair")
                    return False, uid
            for uid in unique_ids:
                await self.db.conn.execute(
                    f"""INSERT INTO combat_daily (user_id, day, {field}) VALUES (?, ?, 1)
                        ON CONFLICT(user_id, day) DO UPDATE SET {field} = {field} + 1""",
                    (uid, today),
                )
            await self.db.conn.execute("RELEASE SAVEPOINT combat_daily_pair")
            return True, None
        except Exception:
            try:
                await self.db.conn.execute("ROLLBACK TO SAVEPOINT combat_daily_pair")
                await self.db.conn.execute("RELEASE SAVEPOINT combat_daily_pair")
            except Exception:
                pass
            raise

    async def _busy_status(self, user_id: str, player: Player = None) -> str:
        """返回忙碌状态名称；空闲返回空串。"""
        user_cd = await self.db.ext.get_user_cd(str(user_id))
        if user_cd and user_cd.type != UserStatus.IDLE:
            return UserStatus.get_name(user_cd.type)
        if not user_cd or user_cd.type == UserStatus.IDLE:
            if player is None:
                player = await self.db.get_player_by_id(str(user_id))
            if (getattr(player, "state", "") or "") == "修炼中":
                return player.state
        return ""

    @staticmethod
    def _effective_cooldown(base: int, player: Player, config_manager) -> int:
        try:
            from ..core.technique_bonus import TechniqueBonusManager
            return TechniqueBonusManager.effective_cooldown(base, player, config_manager)
        except Exception:
            return base

    async def _get_pending_spar_request(self, target_id: str):
        now = int(time.time())
        try:
            await self.db.conn.execute(
                "DELETE FROM spar_requests WHERE expires_at <= ?", (now,)
            )
            await self.db.conn.commit()
            async with self.db.conn.execute(
                """SELECT id, from_id, from_name, target_id, created_at, expires_at
                   FROM spar_requests
                   WHERE target_id = ? AND expires_at > ?
                   ORDER BY created_at DESC LIMIT 1""",
                (str(target_id), now),
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "from_id": row[1],
                    "from_name": row[2],
                    "target_id": row[3],
                    "created_at": row[4],
                    "expires_at": row[5],
                }
        except Exception:
            pass
        return None

    async def _create_spar_request(self, from_id: str, from_name: str, target_id: str):
        now = int(time.time())
        await self.db.conn.execute(
            "DELETE FROM spar_requests WHERE from_id IN (?, ?) OR target_id IN (?, ?)",
            (str(from_id), str(target_id), str(from_id), str(target_id)),
        )
        await self.db.conn.execute(
            """INSERT INTO spar_requests
               (from_id, from_name, target_id, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (str(from_id), from_name or str(from_id)[:8], str(target_id), now, now + self._spar_request_expire()),
        )
        await self.db.conn.commit()

    async def _delete_spar_request(self, request_id: int):
        await self.db.conn.execute("DELETE FROM spar_requests WHERE id = ?", (int(request_id),))
        await self.db.conn.commit()

    async def _get_target_id(self, event: AstrMessageEvent, arg: str) -> str:
        """解析目标ID（终极修复）"""
        sender_id = str(event.get_sender_id())
        self_id = ""
        try:
            if hasattr(event, "get_self_id"): self_id = str(event.get_self_id())
            elif hasattr(event, "message_obj") and hasattr(event.message_obj, "self_id"): self_id = str(event.message_obj.self_id)
        except Exception: pass

        message_chain = []
        if hasattr(event, "message_obj") and event.message_obj:
            message_chain = getattr(event.message_obj, "message", []) or []

        valid_at_ids = []
        for component in message_chain:
            candidate_str = ""
            if isinstance(component, At):
                for attr in ("qq", "target", "uin", "user_id"):
                    val = getattr(component, attr, None)
                    if val:
                        candidate_str = str(val).lstrip("@")
                        break
            elif isinstance(component, str) and "[CQ:at,qq=" in component:
                m = re.search(r"\[CQ:at,qq=(\d+)\]", component)
                if m: candidate_str = m.group(1)

            if candidate_str and candidate_str.isdigit():
                if candidate_str != sender_id and candidate_str != self_id:
                    valid_at_ids.append(candidate_str)

        if valid_at_ids: return valid_at_ids[-1]
        if arg:
            cleaned = arg.strip().lstrip("@")
            if cleaned.isdigit() and cleaned != sender_id and cleaned != self_id: return cleaned
        
        message_text = ""
        if hasattr(event, "message_str"): message_text = event.message_str or ""
        numbers = re.findall(r'\d{5,}', message_text)
        for num in numbers:
            if num != sender_id and num != self_id: return num
                
        return None

    def _calculate_equipment_bonus(self, player) -> dict:
        return self.combat_mgr.calculate_equipment_bonus(player, self.config_manager)

    async def _prepare_combat_stats(self, user_id: str) -> CombatStats:
        player = await self.db.get_player_by_id(user_id)
        if not player: return None
        
        await self.pill_mgr.update_temporary_effects(player)
        pill_multipliers = self.pill_mgr.calculate_pill_attribute_effects(player)
        
        impart_info = await self.db.ext.get_impart_info(user_id)
        hp_buff = impart_info.impart_hp_per if impart_info else 0.0
        mp_buff = impart_info.impart_mp_per if impart_info else 0.0
        atk_buff = impart_info.impart_atk_per if impart_info else 0.0
        crit_rate_buff = impart_info.impart_know_per if impart_info else 0.0
        crit_damage_buff = impart_info.impart_burst_per if impart_info else 0.0

        stats = self.combat_mgr.build_player_combat_stats(
            player,
            self.config_manager,
            db=self.db,
            hp_buff=hp_buff,
            mp_buff=mp_buff,
            atk_buff=atk_buff,
            crit_rate=int(crit_rate_buff * 100),
            crit_damage_bonus=crit_damage_buff,
            pill_multipliers=pill_multipliers,
        )

        from ..managers.pet_manager import PetManager
        pet_mgr = PetManager(self.db)
        main_pet, support_pets, pet_effects, final_atk = await pet_mgr.build_pet_lineup(
            user_id, stats.max_hp, stats.atk, stats.defense, player.cultivation_type
        )
        self.combat_mgr.apply_pet_lineup(
            stats, main_pet, support_pets, pet_effects, final_atk
        )
        
        if int(player.hp or 0) <= 0:
            player.hp = stats.max_hp
        player.mp = int(player.spiritual_qi or 0)
        player.atk = stats.atk
        await self.db.update_player(player)

        return stats

    def _get_element_of(self, item_name: str) -> str:
        if not item_name: return ""
        if self.config_manager and hasattr(self.config_manager, "get_item_element"):
            explicit = self.config_manager.get_item_element(item_name)
            if explicit: return explicit
        return self.combat_mgr.get_element_from_name(item_name)

    async def _save_pet_states(self, *stats_list):
        from ..managers.pet_manager import PetManager
        pet_mgr = PetManager(self.db)
        for stats_item in stats_list:
            pets = []
            if getattr(stats_item, "pet", None):
                pets.append(stats_item.pet)
            pets.extend(getattr(stats_item, "support_pets", None) or [])
            for pet in pets:
                await pet_mgr.save_pet_combat_state(pet)

    @staticmethod
    def _pet_status_line(stats_item) -> str:
        pets = []
        if getattr(stats_item, "pet", None):
            pets.append(stats_item.pet)
        pets.extend(getattr(stats_item, "support_pets", None) or [])
        if not pets:
            return ""
        lines = []
        for index, pet in enumerate(pets):
            tag = "主战" if index == 0 else "辅助"
            if pet.hp <= 0:
                lines.append(f"🐾 【{pet.name}】({tag}) 力竭倒下，需时间复活。")
            else:
                lines.append(f"🐾 【{pet.name}】({tag}) 精力：{pet.hp}/{pet.max_hp}")
        return "\n" + "\n".join(lines)

    @player_required
    async def handle_duel(self, player: Player, event: AstrMessageEvent, target: str):
        set_event_flag(event, "duel_result", None)
        user_id = str(event.get_sender_id())
        target_id = await self._get_target_id(event, target)
        if not target_id:
            yield event.plain_result("❌ 未识别到对手！请 @对方 或输入对方QQ号。\n用法：/决斗 @道友")
            return
        target_id = str(target_id)
        if user_id == target_id:
            yield event.plain_result("❌ 不能和自己决斗。")
            return

        cooldowns = await self._get_combat_cooldown(user_id)
        now = int(time.time())
        duel_cooldown = self._effective_cooldown(DUEL_COOLDOWN, player, self.config_manager)
        remaining_cd = duel_cooldown - (now - cooldowns["last_duel_time"])
        if cooldowns["last_duel_time"] and remaining_cd > 0:
            yield event.plain_result(f"❌ 决斗调息中，请 {remaining_cd // 60} 分 {remaining_cd % 60} 秒后再战。")
            return

        target_player = await self.db.get_player_by_id(target_id)
        if not target_player:
            yield event.plain_result(f"❌ 对方（识别到ID: {target_id}）尚未踏入仙途，请先发送 /我要修仙。")
            return

        daily_limit = self._daily_limit("duel_daily_limit", 1)
        usage = await self._get_daily_usage(user_id)
        if usage["duel_count"] >= daily_limit:
            yield event.plain_result(
                f"❌ 今日决斗次数已用尽（每日 {daily_limit} 次），明天再来吧。"
            )
            return
        target_usage = await self._get_daily_usage(target_id)
        if target_usage["duel_count"] >= daily_limit:
            target_name = target_player.user_name or target_id[:8]
            yield event.plain_result(
                f"❌ 【{target_name}】今日决斗次数已用尽（每日 {daily_limit} 次），换一位对手吧。"
            )
            return

        target_busy = await self._busy_status(target_id, target_player)
        if target_busy:
            yield event.plain_result(
                f"❌ 对方当前正{target_busy}，无法接受切磋/决斗。"
            )
            return

        p1_stats = await self._prepare_combat_stats(user_id)
        p2_stats = await self._prepare_combat_stats(target_id)
        if not p1_stats or not p2_stats:
            yield event.plain_result("❌ 战斗状态准备失败，请重试。")
            return

        consumed, blocked_uid = await self._consume_daily_pair(
            [user_id, target_id], "duel_count", daily_limit
        )
        if not consumed:
            if blocked_uid == user_id:
                yield event.plain_result(
                    f"❌ 今日决斗次数已用尽（每日 {daily_limit} 次），明天再来吧。"
                )
            else:
                target_name = target_player.user_name or target_id[:8]
                yield event.plain_result(
                    f"❌ 【{target_name}】今日决斗次数已用尽（每日 {daily_limit} 次），换一位对手吧。"
                )
            return

        result = self.combat_mgr.player_vs_player(p1_stats, p2_stats, combat_type=2)
        set_event_flag(event, "duel_result", result)
        await self._save_pet_states(p1_stats, p2_stats)
        
        await self.db.ext.update_player_hp_mp(user_id, max(1, p1_stats.hp), max(0, p1_stats.mp))
        await self.db.ext.update_player_hp_mp(target_id, max(1, p2_stats.hp), max(0, p2_stats.mp))
        await self._update_combat_cooldown(user_id, "duel")
        
        log_lines = result['combat_log']
        final_log = "\n".join(log_lines)
        
        # 计算真实的剩余血量百分比
        p1_hp_pct = int(result["player1_real_hp"] / p1_stats.max_hp * 100) if p1_stats.max_hp else 0
        p2_hp_pct = int(result["player2_real_hp"] / p2_stats.max_hp * 100) if p2_stats.max_hp else 0

        if result['winner'] == user_id:
            final_log += f"\n\n🏆 决斗结束，{p1_stats.name} 获胜！\n(剩余血量: {p1_hp_pct}% vs {p2_hp_pct}%)"
        elif result['winner'] == target_id:
            final_log += f"\n\n🏆 决斗结束，{p2_stats.name} 获胜！\n(剩余血量: {p1_hp_pct}% vs {p2_hp_pct}%)"
        else:
            final_log += f"\n\n🏆 决斗结束，平局！\n(剩余血量: {p1_hp_pct}% vs {p2_hp_pct}%)"

        final_log += self._pet_status_line(p1_stats)
        final_log += self._pet_status_line(p2_stats)
        final_log += f"\n\n📅 今日决斗：{usage['duel_count'] + 1}/{daily_limit}"
        yield event.plain_result(final_log)

    @player_required
    async def handle_spar(self, player: Player, event: AstrMessageEvent, target: str):
        set_event_flag(event, "spar_fought", False)
        set_event_flag(event, "spar_result", None)
        user_id = str(event.get_sender_id())
        target_id = await self._get_target_id(event, target)
        if not target_id:
            yield event.plain_result("❌ 未识别到对手！请 @对方 或输入对方QQ号。\n用法：/切磋 @道友")
            return
        target_id = str(target_id)
        if user_id == target_id:
            yield event.plain_result("❌ 不能和自己切磋。")
            return

        cooldowns = await self._get_combat_cooldown(user_id)
        now = int(time.time())
        spar_cooldown = self._effective_cooldown(SPAR_COOLDOWN, player, self.config_manager)
        remaining_cd = spar_cooldown - (now - cooldowns["last_spar_time"])
        if cooldowns["last_spar_time"] and remaining_cd > 0:
            yield event.plain_result(f"❌ 切磋调息中，请 {remaining_cd // 60} 分 {remaining_cd % 60} 秒后再战。")
            return

        target_player = await self.db.get_player_by_id(target_id)
        if not target_player:
            yield event.plain_result(f"❌ 对方（识别到ID: {target_id}）尚未踏入仙途，请先发送 /我要修仙。")
            return

        spar_limit = self._daily_limit("spar_daily_limit", 3)
        usage = await self._get_daily_usage(user_id)
        if usage["spar_count"] >= spar_limit:
            yield event.plain_result(
                f"❌ 今日切磋次数已用尽（每日 {spar_limit} 次），明天再来吧。"
            )
            return
        target_usage = await self._get_daily_usage(target_id)
        if target_usage["spar_count"] >= spar_limit:
            target_name = target_player.user_name or target_id[:8]
            yield event.plain_result(
                f"❌ 【{target_name}】今日切磋次数已用尽（每日 {spar_limit} 次），换一位对手吧。"
            )
            return

        target_busy = await self._busy_status(target_id, target_player)
        if target_busy:
            yield event.plain_result(
                f"❌ 对方当前正{target_busy}，无法接受切磋/决斗。"
            )
            return

        target_cooldowns = await self._get_combat_cooldown(target_id)
        target_spar_cd = self._effective_cooldown(
            SPAR_COOLDOWN, target_player, self.config_manager
        )
        target_remaining = target_spar_cd - (now - target_cooldowns["last_spar_time"])
        if target_cooldowns["last_spar_time"] and target_remaining > 0:
            yield event.plain_result(
                f"❌ 对方切磋调息中，请 {target_remaining // 60} 分 {target_remaining % 60} 秒后再邀请。"
            )
            return

        await self._create_spar_request(
            user_id,
            player.user_name or user_id[:8],
            target_id,
        )
        target_name = target_player.user_name or target_id[:8]
        expire_minutes = max(1, self._spar_request_expire() // 60)
        yield event.plain_result(
            f"⚔️ 已向【{target_name}】发出切磋邀请！\n"
            f"对方使用 /接受切磋 或 /拒绝切磋 响应。\n"
            f"请求将在{expire_minutes}分钟后过期。\n"
            f"📅 今日切磋：{usage['spar_count']}/{spar_limit} 次"
        )

    @player_required
    async def handle_spar_accept(self, player: Player, event: AstrMessageEvent):
        set_event_flag(event, "spar_fought", False)
        set_event_flag(event, "spar_result", None)
        acceptor_id = str(event.get_sender_id())
        request = await self._get_pending_spar_request(acceptor_id)
        if not request:
            yield event.plain_result("❌ 没有待接受的切磋邀请。")
            return

        initiator = await self.db.get_player_by_id(request["from_id"])
        if not initiator:
            await self._delete_spar_request(request["id"])
            yield event.plain_result("❌ 发起切磋的道友已不在此界，邀请作废。")
            return
        initiator_id = str(initiator.user_id)
        initiator_name = initiator.user_name or initiator_id[:8]

        initiator_busy = await self._busy_status(initiator_id, initiator)
        if initiator_busy:
            yield event.plain_result(
                f"❌【{initiator_name}】当前正{initiator_busy}，切磋无法进行。"
            )
            return
        acceptor_busy = await self._busy_status(acceptor_id, player)
        if acceptor_busy:
            yield event.plain_result(f"❌ 你当前正{acceptor_busy}，无法接受切磋。")
            return

        now = int(time.time())
        spar_limit = self._daily_limit("spar_daily_limit", 3)
        initiator_usage = await self._get_daily_usage(initiator_id)
        if initiator_usage["spar_count"] >= spar_limit:
            yield event.plain_result(
                f"❌【{initiator_name}】今日切磋次数已用尽（每日 {spar_limit} 次）。"
            )
            return
        acceptor_usage = await self._get_daily_usage(acceptor_id)
        if acceptor_usage["spar_count"] >= spar_limit:
            yield event.plain_result(
                f"❌ 你今日切磋次数已用尽（每日 {spar_limit} 次），明天再来吧。"
            )
            return

        cooldowns = await self._get_combat_cooldown(initiator_id)
        spar_cooldown = self._effective_cooldown(SPAR_COOLDOWN, initiator, self.config_manager)
        remaining_cd = spar_cooldown - (now - cooldowns["last_spar_time"])
        if cooldowns["last_spar_time"] and remaining_cd > 0:
            yield event.plain_result(
                f"❌【{initiator_name}】切磋调息中，请 {remaining_cd // 60} 分 {remaining_cd % 60} 秒后再试。"
            )
            return
        acceptor_cooldowns = await self._get_combat_cooldown(acceptor_id)
        acceptor_cd = self._effective_cooldown(SPAR_COOLDOWN, player, self.config_manager)
        acceptor_remaining = acceptor_cd - (now - acceptor_cooldowns["last_spar_time"])
        if acceptor_cooldowns["last_spar_time"] and acceptor_remaining > 0:
            yield event.plain_result(
                f"❌ 你切磋调息中，请 {acceptor_remaining // 60} 分 {acceptor_remaining % 60} 秒后再试。"
            )
            return

        p1_stats = await self._prepare_combat_stats(initiator_id)
        p2_stats = await self._prepare_combat_stats(acceptor_id)
        if not p1_stats or not p2_stats:
            yield event.plain_result("❌ 战斗状态准备失败，请重试。")
            return

        # 所有校验通过后再原子抢占请求，避免对方忙碌/次数不足时白白吞掉邀请。
        cursor = await self.db.conn.execute(
            "DELETE FROM spar_requests WHERE id = ? AND target_id = ?",
            (request["id"], acceptor_id),
        )
        await self.db.conn.commit()
        if cursor.rowcount == 0:
            yield event.plain_result("❌ 该切磋邀请已被处理，请重新邀请。")
            return

        consumed, blocked_uid = await self._consume_daily_pair(
            [initiator_id, acceptor_id], "spar_count", spar_limit
        )
        if not consumed:
            if blocked_uid == initiator_id:
                yield event.plain_result(
                    f"❌【{initiator_name}】今日切磋次数已用尽（每日 {spar_limit} 次）。"
                )
            else:
                yield event.plain_result(
                    f"❌ 你今日切磋次数已用尽（每日 {spar_limit} 次），明天再来吧。"
                )
            return

        result = self.combat_mgr.player_vs_player(p1_stats, p2_stats, combat_type=1)
        result["participants"] = [initiator_id, acceptor_id]
        set_event_flag(event, "spar_fought", True)
        set_event_flag(event, "spar_result", result)
        await self._save_pet_states(p1_stats, p2_stats)

        await self.db.ext.update_player_hp_mp(initiator_id, max(1, p1_stats.hp), max(0, p1_stats.mp))
        await self.db.ext.update_player_hp_mp(acceptor_id, max(1, p2_stats.hp), max(0, p2_stats.mp))
        await self._update_combat_cooldown(initiator_id, "spar")
        await self._update_combat_cooldown(acceptor_id, "spar")

        log_lines = result['combat_log']
        final_log = "\n".join(log_lines)

        # 计算真实的剩余血量百分比
        p1_hp_pct = int(result["player1_real_hp"] / p1_stats.max_hp * 100) if p1_stats.max_hp else 0
        p2_hp_pct = int(result["player2_real_hp"] / p2_stats.max_hp * 100) if p2_stats.max_hp else 0

        if result['winner'] == initiator_id:
            final_log += f"\n\n🏆 切磋结束，{p1_stats.name} 获胜！\n(剩余血量: {p1_hp_pct}% vs {p2_hp_pct}%)"
        elif result['winner'] == acceptor_id:
            final_log += f"\n\n🏆 切磋结束，{p2_stats.name} 获胜！\n(剩余血量: {p1_hp_pct}% vs {p2_hp_pct}%)"
        else:
            final_log += f"\n\n🏆 切磋结束，平局！\n(剩余血量: {p1_hp_pct}% vs {p2_hp_pct}%)"

        final_log += self._pet_status_line(p1_stats)
        final_log += self._pet_status_line(p2_stats)
        final_log += f"\n\n📅 今日切磋：{acceptor_usage['spar_count'] + 1}/{spar_limit}"
        yield event.plain_result(final_log)

    @player_required
    async def handle_spar_reject(self, player: Player, event: AstrMessageEvent):
        acceptor_id = str(event.get_sender_id())
        request = await self._get_pending_spar_request(acceptor_id)
        if not request:
            yield event.plain_result("❌ 没有待处理的切磋邀请。")
            return
        await self._delete_spar_request(request["id"])
        from_name = request.get("from_name") or str(request.get("from_id", ""))[:8]
        yield event.plain_result(f"✅ 已拒绝【{from_name}】的切磋邀请。")