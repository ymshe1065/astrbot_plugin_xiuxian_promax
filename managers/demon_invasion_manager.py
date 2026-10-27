# managers/demon_invasion_manager.py
import asyncio
import json
import random
import time
from typing import List, Optional, Tuple

from ..data import DataBase
from ..models import Player
from .global_event_manager import GlobalEventManager


class DemonInvasionManager:
    """天魔入侵管理器"""

    MIN_PLAYERS = 10
    DEMON_REWARD = 50000
    CATCH_REWARD = 30000
    MORTAL_REWARD = 5000
    MIN_HOURS = 1
    MAX_HOURS = 48

    def __init__(self, db: DataBase, event_mgr: Optional[GlobalEventManager] = None):
        self.db = db
        self.event_mgr = event_mgr or GlobalEventManager(db)
        self._lock = asyncio.Lock()
        self.last_event_msg = ""

    def _normalize_hours(self, duration_hours) -> int:
        try:
            hours = int(duration_hours)
        except (TypeError, ValueError):
            hours = 24
        return max(self.MIN_HOURS, min(self.MAX_HOURS, hours))

    def pop_last_event_msg(self) -> str:
        msg = self.last_event_msg
        self.last_event_msg = ""
        return msg

    async def start_invasion(self, duration_hours: int = 24) -> Tuple[bool, str]:
        """开启天魔入侵"""
        duration_hours = self._normalize_hours(duration_hours)

        async with self._lock:
            is_active = await self.db.ext.get_system_config("demon_invasion_active")
            if is_active == "1":
                return False, "❌ 当前已有天魔入侵正在进行中。"

            acquired, conflict_msg = await self.event_mgr.acquire(
                GlobalEventManager.EVENT_DEMON
            )
            if not acquired:
                return False, conflict_msg

            try:
                players = await self.db.get_all_players()
                if len(players) < self.MIN_PLAYERS:
                    await self.event_mgr.release(GlobalEventManager.EVENT_DEMON)
                    return False, (
                        f"❌ 开启失败！当前全服修仙者仅有 {len(players)} 人。\n"
                        f"(天魔入侵至少需要 {self.MIN_PLAYERS} 名修士参与才能开启)"
                    )

                demon_count = 1 if len(players) < 16 else 2
                demons = random.sample(players, demon_count)
                demon_ids = [p.user_id for p in demons]

                now = int(time.time())
                end_time = now + duration_hours * 3600

                await self.db.ext.set_system_config("demon_invasion_active", "1")
                await self.db.ext.set_system_config("demon_invasion_end_time", str(end_time))
                await self.db.ext.set_system_config(
                    "demon_invasion_list", json.dumps(demon_ids)
                )
            except Exception:
                await self.db.ext.set_system_config("demon_invasion_active", "0")
                await self.db.ext.set_system_config("demon_invasion_list", "[]")
                await self.db.ext.set_system_config("demon_invasion_end_time", "0")
                await self.event_mgr.release(GlobalEventManager.EVENT_DEMON)
                raise

            self.last_event_msg = ""
            return True, (
                f"天魔已降临！{demon_count}名修士被附体。"
                f"活动时长：{duration_hours}小时。"
            )

    async def end_invasion(self) -> Tuple[bool, str]:
        """结束天魔入侵并结算（管理员或超时任务调用）"""
        async with self._lock:
            return await self._finish_locked(reward_demons=True)

    async def _finish_locked(
        self,
        reward_demons: bool,
        demon_ids: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        is_active = await self.db.ext.get_system_config("demon_invasion_active")
        if is_active != "1":
            await self.event_mgr.release(GlobalEventManager.EVENT_DEMON)
            return False, "❌ 当前没有正在进行的活动。"

        if demon_ids is None:
            demon_list_str = await self.db.ext.get_system_config("demon_invasion_list")
            try:
                demon_ids = json.loads(demon_list_str) if demon_list_str else []
            except (TypeError, ValueError, json.JSONDecodeError):
                demon_ids = []

        if reward_demons:
            for uid in demon_ids:
                await self.db.add_player_gold(uid, self.DEMON_REWARD)

        all_players = await self.db.get_all_players()
        demon_set = set(demon_ids)
        for p in all_players:
            if p.user_id not in demon_set:
                await self.db.add_player_gold(p.user_id, self.MORTAL_REWARD)

        await self.db.ext.set_system_config("demon_invasion_active", "0")
        await self.db.ext.set_system_config("demon_invasion_list", "[]")
        await self.db.ext.set_system_config("demon_invasion_end_time", "0")
        await self.event_mgr.release(GlobalEventManager.EVENT_DEMON)

        if demon_ids:
            msg = (
                f"🌑 天魔入侵结束！天魔潜伏成功（共 {len(demon_ids)} 人），"
                f"每人获得 {self.DEMON_REWARD:,} 灵石。\n"
                f"全服正道修士获得 {self.MORTAL_REWARD:,} 灵石安慰奖。"
            )
        else:
            msg = (
                "🌑 天魔入侵结束！所有天魔均已被揪出，"
                f"全服正道修士获得 {self.MORTAL_REWARD:,} 灵石慰劳。"
            )

        self.last_event_msg = msg
        return True, msg

    async def catch_demon(
        self, player: Player, target_id: str, storage_ring_mgr
    ) -> Tuple[bool, str]:
        """查验并捉拿天魔"""
        async with self._lock:
            is_active = await self.db.ext.get_system_config("demon_invasion_active")
            if is_active != "1":
                return False, "❌ 当前没有进行天魔入侵。"

            if str(target_id) == str(player.user_id):
                return False, "❌ 不能查验自己。"

            if not storage_ring_mgr.has_item(player, "照妖镜", 1):
                return False, "❌ 你没有【照妖镜】，无法查验。"

            ok, retrieve_msg = await storage_ring_mgr.retrieve_item(
                player, "照妖镜", 1
            )
            if not ok:
                return False, f"❌ 消耗照妖镜失败：{retrieve_msg}"

            demon_list_str = await self.db.ext.get_system_config("demon_invasion_list")
            try:
                demon_ids = json.loads(demon_list_str) if demon_list_str else []
            except (TypeError, ValueError, json.JSONDecodeError):
                demon_ids = []

            if target_id not in demon_ids:
                await self.db.scale_player_hp(player.user_id, 0.5, minimum=1)
                return False, (
                    "💥 查验失败！对方是清清白白的正道修士。"
                    "你遭到天道反噬，气血减半，照妖镜碎裂。"
                )

            demon_ids.remove(target_id)
            await self.db.add_player_gold(player.user_id, self.CATCH_REWARD)
            await self.db.scale_player_exp(target_id, 0.8)

            if not demon_ids:
                settlement_msg = await self._finish_locked(
                    reward_demons=False, demon_ids=[]
                )
                if settlement_msg[0]:
                    await self.db.ext.set_system_config("demon_invasion_list", "[]")
                return True, (
                    "🎉 天地震动！你一语道破天机，【最后一名天魔】被你成功揪出！"
                    f"天魔入侵彻底结束！你获得了 {self.CATCH_REWARD:,} 灵石奖励。\n\n"
                    f"{settlement_msg[1]}"
                )

            await self.db.ext.set_system_config(
                "demon_invasion_list", json.dumps(demon_ids)
            )
            return True, (
                "🔔 降妖除魔！你成功查验出对方是【天魔】！对方修为受损，"
                f"你获得了 {self.CATCH_REWARD:,} 灵石奖励。但似乎还有天魔潜伏..."
            )
