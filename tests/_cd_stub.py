# tests/_cd_stub.py
"""把测试里手写的「单槽 user_cd 假实现」补齐成 v62 的多槽位接口。

v62 起 ``user_cd`` 变成 ``(user_id, slot)`` 复合主键：本体占 0 号槽，身外化身占
1..4 号槽，每个槽位挂一项耗时活动。大量测试原本只造了一个 ``self.cd`` 属性来模拟
「唯一那条状态」，这里用 mixin 把它按槽位语义包一层，测试本身不用重写。

约定：假 ext 用 ``self.cd`` 保存唯一一条 CD（对象或 None），对象至少具备
``type / create_time / scheduled_time / extra_data`` 四个属性。
"""

import json
import time

# 不在这里 import 项目包：部分测试（如 test_proxy_panel）没有装 astrbot 桩，
# 只需要「0 = 空闲」这个约定就够了。
IDLE = 0


class _StubCD:
    """最简 UserCd 替身（假 ext 里原本没有 CD 对象时临时创建）。"""

    def __init__(self, cd_type=IDLE, scheduled_time=0, extra_data=None):
        self.type = int(cd_type)
        self.create_time = int(time.time())
        self.scheduled_time = int(scheduled_time or 0)
        self.extra_data = json.dumps(extra_data or {}, ensure_ascii=False)
        self.slot = 0

    def get_extra_data(self):
        try:
            return json.loads(self.extra_data or "{}")
        except Exception:
            return {}


class SlotCdStubMixin:
    """单槽 CD 桩 → 多槽位接口。"""

    # ---- 读 ----

    def _stub_cd(self):
        return getattr(self, "cd", None)

    async def list_user_cds(self, user_id):
        cd = self._stub_cd()
        if not cd:
            return []
        if getattr(cd, "slot", None) is None:
            try:
                cd.slot = 0
            except Exception:
                pass
        return [cd]

    async def get_active_cd(self, user_id, cd_type):
        cd = self._stub_cd()
        if cd and int(getattr(cd, "type", 0) or 0) == int(cd_type):
            return cd
        return None

    async def first_busy_cd(self, user_id):
        cd = self._stub_cd()
        if cd and int(getattr(cd, "type", 0) or 0) != IDLE:
            return cd
        return None

    async def busy_slot_count(self, user_id):
        return 1 if await self.first_busy_cd(user_id) else 0

    async def busy_activity_names(self, user_id):
        from astrbot_plugin_xiuxian_promax.models_extended import UserStatus

        cd = await self.first_busy_cd(user_id)
        return [UserStatus.get_name(int(cd.type))] if cd else []

    async def find_free_slot(self, user_id, max_slots):
        if await self.first_busy_cd(user_id):
            return None
        return 0

    # ---- 写 ----

    def _stub_write(self, cd_type, scheduled_time, extra_data):
        cd = self._stub_cd()
        if cd is None:
            cd = _StubCD(cd_type, scheduled_time, extra_data)
            self.cd = cd
            return cd
        cd.type = int(cd_type)
        cd.create_time = int(time.time())
        cd.scheduled_time = int(scheduled_time or 0)
        payload = dict(extra_data or {})
        try:
            cd.extra_data = json.dumps(payload, ensure_ascii=False)
        except Exception:
            pass
        if hasattr(cd, "_extra"):
            cd._extra = payload
        return cd

    async def start_activity(
        self, user_id, cd_type, scheduled_time=0, extra_data=None,
        max_slots=1, commit=True,
    ):
        if await self.get_active_cd(user_id, cd_type) is not None:
            return False, 0, "already_running"
        busy = await self.busy_slot_count(user_id)
        if busy >= max(1, int(max_slots or 1)):
            return False, -1, "no_free_slot"
        self._stub_write(cd_type, scheduled_time, extra_data)
        return True, 0, ""

    async def free_slot(self, user_id, slot, commit=True):
        cd = self._stub_cd()
        if cd:
            cd.type = IDLE
            cd.create_time = 0
            cd.scheduled_time = 0

    async def release_activity(self, user_id, cd_type=None, commit=True):
        cd = self._stub_cd()
        if not cd:
            return
        if cd_type is None or int(getattr(cd, "type", 0) or 0) == int(cd_type):
            cd.type = IDLE
            cd.create_time = 0
            cd.scheduled_time = 0

    async def set_user_busy(
        self, user_id, busy_type, scheduled_time=0, extra_data=None,
        commit=True, max_slots=1,
    ):
        if int(busy_type) == IDLE:
            await self.release_activity(user_id, None, commit=commit)
            return
        await self.start_activity(
            user_id, busy_type, scheduled_time, extra_data=extra_data,
            max_slots=max_slots, commit=commit,
        )

    async def set_user_free(self, user_id, commit=True, cd_type=None):
        await self.release_activity(user_id, cd_type, commit=commit)
