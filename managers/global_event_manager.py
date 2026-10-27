# managers/global_event_manager.py
"""全服事件互斥管理：同一时间只允许天魔入侵或魔潮天灾之一运行"""
import asyncio
from typing import Tuple

from ..data.data_manager import DataBase

__all__ = ["GlobalEventManager"]


class GlobalEventManager:
    EVENT_NONE = ""
    EVENT_DEMON = "demon"
    EVENT_TIDE = "tide"

    KEY_OWNER = "global_event_owner"
    KEY_DEMON_ACTIVE = "demon_invasion_active"
    KEY_TIDE_ACTIVE = "tide_active"

    NAMES = {
        EVENT_DEMON: "天魔入侵",
        EVENT_TIDE: "魔潮天灾",
    }

    def __init__(self, db: DataBase):
        self.db = db
        self._lock = asyncio.Lock()

    async def _get(self, key: str) -> str:
        value = await self.db.ext.get_system_config(key)
        return value if value is not None else ""

    def event_name(self, event: str) -> str:
        return self.NAMES.get(event, "全服事件")

    async def get_active_event(self) -> str:
        owner = await self._get(self.KEY_OWNER)
        if owner in self.NAMES:
            return owner

        demon_active = await self._get(self.KEY_DEMON_ACTIVE) == "1"
        tide_active = await self._get(self.KEY_TIDE_ACTIVE) == "1"
        if demon_active:
            return self.EVENT_DEMON
        if tide_active:
            return self.EVENT_TIDE
        return self.EVENT_NONE

    async def acquire(self, event: str) -> Tuple[bool, str]:
        async with self._lock:
            active = await self.get_active_event()
            if active == event:
                return False, f"❌ 当前【{self.event_name(event)}】已在进行中。"
            if active:
                return False, (
                    f"❌ 当前【{self.event_name(active)}】正在进行，"
                    "同一时间只能存在一个全服事件。"
                )
            await self.db.ext.set_system_config(self.KEY_OWNER, event)
            return True, ""

    async def release(self, event: str) -> None:
        async with self._lock:
            owner = await self._get(self.KEY_OWNER)
            if owner in (self.EVENT_NONE, event):
                await self.db.ext.set_system_config(self.KEY_OWNER, self.EVENT_NONE)

    async def reconcile(self) -> str:
        async with self._lock:
            demon_active = await self._get(self.KEY_DEMON_ACTIVE) == "1"
            tide_active = await self._get(self.KEY_TIDE_ACTIVE) == "1"
            owner = await self._get(self.KEY_OWNER)

            if demon_active and tide_active:
                keep = owner if owner in self.NAMES else self.EVENT_DEMON
                if keep == self.EVENT_DEMON:
                    await self.db.ext.set_system_config(self.KEY_TIDE_ACTIVE, "0")
                    await self.db.ext.set_system_config("tide_end_time", "0")
                    await self.db.ext.set_system_config("tide_target", "0")
                    await self.db.ext.set_system_config("tide_progress", "0")
                    await self.db.ext.set_system_config("tide_aftermath_until", "0")
                    await self.db.ext.set_system_config(self.KEY_OWNER, self.EVENT_DEMON)
                else:
                    await self.db.ext.set_system_config(self.KEY_DEMON_ACTIVE, "0")
                    await self.db.ext.set_system_config("demon_invasion_end_time", "0")
                    await self.db.ext.set_system_config("demon_invasion_list", "[]")
                    await self.db.ext.set_system_config(self.KEY_OWNER, self.EVENT_TIDE)
                return "both"
            if demon_active:
                await self.db.ext.set_system_config(self.KEY_OWNER, self.EVENT_DEMON)
                return self.EVENT_DEMON
            if tide_active:
                await self.db.ext.set_system_config(self.KEY_OWNER, self.EVENT_TIDE)
                return self.EVENT_TIDE
            if owner:
                await self.db.ext.set_system_config(self.KEY_OWNER, self.EVENT_NONE)
            return self.EVENT_NONE
