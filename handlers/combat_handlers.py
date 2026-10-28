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
        user_id = event.get_sender_id()
        target_id = await self._get_target_id(event, target)
        if not target_id:
            yield event.plain_result("❌ 未识别到对手！请 @对方 或输入对方QQ号。\n用法：/决斗 @道友")
            return
        if user_id == target_id:
            yield event.plain_result("❌ 不能和自己决斗。")
            return

        cooldowns = await self._get_combat_cooldown(user_id)
        now = int(time.time())
        try:
            from ..core.technique_bonus import TechniqueBonusManager
            duel_cooldown = TechniqueBonusManager.effective_cooldown(
                DUEL_COOLDOWN, player, self.config_manager
            )
        except Exception:
            duel_cooldown = DUEL_COOLDOWN
        remaining_cd = duel_cooldown - (now - cooldowns["last_duel_time"])
        if cooldowns["last_duel_time"] and remaining_cd > 0:
            yield event.plain_result(f"❌ 决斗调息中，请 {remaining_cd // 60} 分 {remaining_cd % 60} 秒后再战。")
            return

        target_player = await self.db.get_player_by_id(target_id)
        if not target_player:
            yield event.plain_result(f"❌ 对方（识别到ID: {target_id}）尚未踏入仙途，请先发送 /我要修仙。")
            return
        target_cd = await self.db.ext.get_user_cd(target_id)
        target_busy = (target_cd and target_cd.type != UserStatus.IDLE) or (
            (not target_cd or target_cd.type == UserStatus.IDLE)
            and (getattr(target_player, "state", "") or "") == "修炼中"
        )
        if target_busy:
            status_name = UserStatus.get_name(target_cd.type) if target_cd and target_cd.type != UserStatus.IDLE else target_player.state
            yield event.plain_result(
                f"❌ 对方当前正{status_name}，无法接受切磋/决斗。"
            )
            return

        p1_stats = await self._prepare_combat_stats(user_id)
        p2_stats = await self._prepare_combat_stats(target_id)
        if not p1_stats or not p2_stats:
            yield event.plain_result("❌ 战斗状态准备失败，请重试。")
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
        yield event.plain_result(final_log)

    @player_required
    async def handle_spar(self, player: Player, event: AstrMessageEvent, target: str):
        set_event_flag(event, "spar_fought", False)
        user_id = event.get_sender_id()
        target_id = await self._get_target_id(event, target)
        if not target_id:
            yield event.plain_result("❌ 未识别到对手！请 @对方 或输入对方QQ号。\n用法：/切磋 @道友")
            return
        if user_id == target_id:
            yield event.plain_result("❌ 不能和自己切磋。")
            return

        cooldowns = await self._get_combat_cooldown(user_id)
        now = int(time.time())
        try:
            from ..core.technique_bonus import TechniqueBonusManager
            spar_cooldown = TechniqueBonusManager.effective_cooldown(
                SPAR_COOLDOWN, player, self.config_manager
            )
        except Exception:
            spar_cooldown = SPAR_COOLDOWN
        remaining_cd = spar_cooldown - (now - cooldowns["last_spar_time"])
        if cooldowns["last_spar_time"] and remaining_cd > 0:
            yield event.plain_result(f"❌ 切磋调息中，请 {remaining_cd // 60} 分 {remaining_cd % 60} 秒后再战。")
            return

        target_player = await self.db.get_player_by_id(target_id)
        if not target_player:
            yield event.plain_result(f"❌ 对方（识别到ID: {target_id}）尚未踏入仙途，请先发送 /我要修仙。")
            return
        target_cd = await self.db.ext.get_user_cd(target_id)
        target_busy = (target_cd and target_cd.type != UserStatus.IDLE) or (
            (not target_cd or target_cd.type == UserStatus.IDLE)
            and (getattr(target_player, "state", "") or "") == "修炼中"
        )
        if target_busy:
            status_name = UserStatus.get_name(target_cd.type) if target_cd and target_cd.type != UserStatus.IDLE else target_player.state
            yield event.plain_result(
                f"❌ 对方当前正{status_name}，无法接受切磋/决斗。"
            )
            return

        p1_stats = await self._prepare_combat_stats(user_id)
        p2_stats = await self._prepare_combat_stats(target_id)
        if not p1_stats or not p2_stats:
            yield event.plain_result("❌ 战斗状态准备失败，请重试。")
            return

        result = self.combat_mgr.player_vs_player(p1_stats, p2_stats, combat_type=1)
        set_event_flag(event, "spar_fought", True)
        await self._save_pet_states(p1_stats, p2_stats)
        
        await self.db.ext.update_player_hp_mp(user_id, max(1, p1_stats.hp), max(0, p1_stats.mp))
        await self.db.ext.update_player_hp_mp(target_id, max(1, p2_stats.hp), max(0, p2_stats.mp))
        await self._update_combat_cooldown(user_id, "spar")
        
        log_lines = result['combat_log']
        final_log = "\n".join(log_lines)
        
        # 计算真实的剩余血量百分比
        p1_hp_pct = int(result["player1_real_hp"] / p1_stats.max_hp * 100) if p1_stats.max_hp else 0
        p2_hp_pct = int(result["player2_real_hp"] / p2_stats.max_hp * 100) if p2_stats.max_hp else 0

        if result['winner'] == user_id:
            final_log += f"\n\n🏆 切磋结束，{p1_stats.name} 获胜！\n(剩余血量: {p1_hp_pct}% vs {p2_hp_pct}%)"
        elif result['winner'] == target_id:
            final_log += f"\n\n🏆 切磋结束，{p2_stats.name} 获胜！\n(剩余血量: {p1_hp_pct}% vs {p2_hp_pct}%)"
        else:
            final_log += f"\n\n🏆 切磋结束，平局！\n(剩余血量: {p1_hp_pct}% vs {p2_hp_pct}%)"

        final_log += self._pet_status_line(p1_stats)
        final_log += self._pet_status_line(p2_stats)
        yield event.plain_result(final_log)