# managers/encounter_manager.py
"""奇遇选项系统 - Roguelike 文字探险（历练/秘境归来概率触发三选一事件）"""
import time
import random
import json
from typing import Tuple, Optional, Dict, List

from ..data.data_manager import DataBase
from ..models import Player
from .encounter_events import EVENTS as EVENT_POOL
from .luck_manager import LuckManager

__all__ = ["EncounterManager"]


class EncounterManager:
    """奇遇管理器：触发后在限时内回复 /奇遇选择 <编号> 结算
    奖励随境界缩放：gold/exp × (1 + level_index × 0.12)，封顶4倍"""

    CHOICE_EXPIRE = 180      # 选择有效期（秒）
    STORAGE_KEY_PREFIX = "encounter_"
    REWARD_SCALE_PER_LEVEL = 0.12
    REWARD_SCALE_MAX = 4.0

    def __init__(self, db: DataBase, storage_ring_mgr=None):
        self.db = db
        self.storage_ring_mgr = storage_ring_mgr
        self.events = {ev["key"]: ev for ev in EVENT_POOL}

    def _storage_key(self, user_id: str) -> str:
        return f"{self.STORAGE_KEY_PREFIX}{user_id}"

    def _reward_scale(self, player: Player) -> float:
        return min(self.REWARD_SCALE_MAX, 1.0 + (player.level_index or 0) * self.REWARD_SCALE_PER_LEVEL)

    async def maybe_trigger(self, player: Player, base_chance: int = 20) -> Optional[str]:
        """历练(20%)/秘境(25%)归来时调用，概率触发奇遇。返回提示文本（None 表示未触发）"""
        if not player:
            return None
        # 功德越高奇遇越容易触发（每点+2%，上限+20%）；气运每点+0.5%，上限+20%
        chance = base_chance + max(0, min(10, getattr(player, "karma", 0) or 0) * 2)
        chance += LuckManager.percent_bonus(player, per_point=0.5, cap=20.0)
        chance = min(95, chance)
        if random.randint(1, 100) > chance:
            return None

        # 已有未结算的奇遇则不再叠加
        existing = await self.db.ext.get_system_config(self._storage_key(player.user_id))
        if existing:
            return None

        # 按境界筛选可用事件
        eligible = [ev for ev in EVENT_POOL if player.level_index >= ev.get("min_level", 0)]
        if not eligible:
            return None
        event = random.choice(eligible)

        payload = {
            "key": event["key"],
            "expire": int(time.time()) + self.CHOICE_EXPIRE,
        }
        await self.db.ext.set_system_config(self._storage_key(player.user_id), json.dumps(payload, ensure_ascii=False))

        option_lines = "\n".join(
            f"  [{i}] {opt['label']}" for i, opt in enumerate(event["options"], 1)
        )
        return (
            f"\n\n✨【奇遇】{event['title']}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{event['desc']}\n"
            f"{option_lines}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 发送 /奇遇选择 <编号> 做出抉择（{self.CHOICE_EXPIRE // 60}分钟内有效，过期视作离开）"
        )

    async def resolve_choice(self, player: Player, choice: int) -> Tuple[bool, str]:
        """处理玩家的奇遇选择"""
        raw = await self.db.ext.get_system_config(self._storage_key(player.user_id))
        if not raw:
            return False, "❌ 你当前没有进行中的奇遇。"

        try:
            payload = json.loads(raw)
        except Exception:
            await self.db.ext.set_system_config(self._storage_key(player.user_id), "")
            return False, "❌ 奇遇数据异常，已清除。"

        if int(time.time()) > int(payload.get("expire", 0)):
            await self.db.ext.set_system_config(self._storage_key(player.user_id), "")
            return False, "⌛ 奇遇已消散于风中（超时未选择）。"

        event = self.events.get(payload.get("key", ""))
        if not event:
            await self.db.ext.set_system_config(self._storage_key(player.user_id), "")
            return False, "❌ 未知奇遇事件，已清除。"

        if choice < 1 or choice > len(event["options"]):
            return False, f"❌ 请输入 1-{len(event['options'])} 的编号。"

        # 一经选择立即清除，防止重复结算
        await self.db.ext.set_system_config(self._storage_key(player.user_id), "")

        option = event["options"][choice - 1]
        weight = option.get("weight")

        # 离开类选项直接结算
        if weight == "leave":
            return True, "🚶 你谨慎地离开了，世间机缘，随缘而已。"

        # 风险类 55% 成功；善行必成；恶行 70% 成功；稳妥类 85% 成功
        # 气运提供额外成功率：每点+0.25%，上限+15%
        luck_rate = LuckManager.percent_bonus(player, per_point=0.25, cap=15.0) / 100.0
        fail = False
        if weight == "risk":
            fail = random.random() > min(0.98, 0.55 + luck_rate)
        elif weight == "evil":
            fail = random.random() > min(0.98, 0.7 + luck_rate)
        elif weight == "safe":
            fail = random.random() > min(0.98, 0.85 + luck_rate)
        elif weight == "good":
            fail = False

        outcome = option.get("lose") if fail else option.get("win")
        if not outcome:
            return True, "🤔 你尝试了一番，却一无所获……"

        lines = [f"🎲【{event['title']}】你选择了「{option['label']}」", "━━━━━━━━━━━━━━━"]

        if fail:
            lines.append("💀 事情朝糟糕的方向发展了...")
        else:
            lines.append("🎉 你的抉择带来了回报！")

        scale = self._reward_scale(player)
        gold = int(outcome.get("gold", 0))
        exp = int(outcome.get("exp", 0))
        hp_pct = float(outcome.get("hp_pct", 0))
        karma = int(outcome.get("karma", 0))
        luck = int(outcome.get("luck", 0) or 0)
        item = outcome.get("item")

        if gold:
            gold = int(gold * scale)
            player.gold = max(0, player.gold + gold)
            lines.append(f"{'获得' if gold > 0 else '损失'}灵石：{gold:+,}")
        if exp:
            exp = int(exp * scale)
            player.experience = max(0, player.experience + exp)
            lines.append(f"{'获得' if exp > 0 else '损失'}修为：{exp:+,}")
        if hp_pct:
            lost = int(player.hp * hp_pct)
            player.hp = max(1, player.hp - lost)
            lines.append(f"损失气血：-{lost:,}")
        if karma > 0:
            player.karma = (getattr(player, "karma", 0) or 0) + karma
            lines.append(f"✨功德 +{karma}（当前功德 {player.karma}）：奇遇概率与突破成功率提升")
        elif karma < 0:
            player.karma = (getattr(player, "karma", 0) or 0) + karma
            lines.append(f"🌑业力 {karma}（当前 {player.karma}）：突破成功率降低，多行善可消解")

        # 奇遇是气运的主要获取途径：部分事件直接给气运，成功抉择也有基础机缘
        if not fail and weight != "leave" and luck == 0:
            ambient_chance = 10 + min(15, LuckManager.get_total_luck(player) * 0.1)
            if random.randint(1, 100) <= ambient_chance:
                luck = 1
        if luck:
            player.luck = LuckManager.add_luck(player, luck)
            if luck > 0:
                lines.append(f"🍀 机缘临身，气运 +{luck}（当前气运 {player.luck}）")
            else:
                lines.append(f"🍂 气运受损 {luck}（当前气运 {player.luck}）")

        need_save = bool(gold or exp or hp_pct or karma or luck)

        if item and self.storage_ring_mgr and not fail:
            item_name = item.get("name", "")
            item_count = int(item.get("count", 1))
            success, _ = await self.storage_ring_mgr.store_item(player, item_name, item_count, silent=True)
            if success:
                lines.append(f"📦 获得物品：【{item_name}】x{item_count}（已存入储物戒）")
            else:
                lines.append(f"⚠️ 获得【{item_name}】x{item_count}，但储物戒已满，物品丢失")

        if need_save:
            await self.db.update_player(player)
        return True, "\n".join(lines)
