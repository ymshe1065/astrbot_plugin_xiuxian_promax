# managers/life_skill_manager.py
"""生活职业系统 - 炼器师/符箓师/阵法师/炼丹师专精"""
import time
from typing import Tuple

from ..data.data_manager import DataBase
from ..models import Player

__all__ = ["LifeSkillManager", "LIFE_SKILLS"]


LIFE_SKILLS = {
    "炼器师": {
        "desc": "强化装备的灵石消耗减半",
        "perk_hint": "发送 /强化 时自动生效",
    },
    "符箓师": {
        "desc": "每日免费【引雷符】；绘制成功率+10%、消耗-20%、20%概率额外产出",
        "perk_hint": "发送 /符箓 查看配方，/制符 <符名> 绘制，/使用符箓 使用",
    },
    "阵法师": {
        "desc": "护府大阵持续时间 24小时 → 36小时，产出惩罚降至10%",
        "perk_hint": "发送 /开启护府 时自动生效",
    },
    "炼丹师": {
        "desc": "炼丹成功率 +10%",
        "perk_hint": "发送 /炼丹 时自动生效",
    },
}

CHANGE_BASE = 50_000
CHANGE_LEVEL_FACTOR = 0.12
CHANGE_GROWTH = 2.0
CHANGE_DECAY_HOURS = 72
CHANGE_MAX = 50_000_000
_CHANGE_ROUND = 10_000


class LifeSkillManager:
    """生活职业管理器"""

    def __init__(self, db: DataBase, storage_ring_mgr=None):
        self.db = db
        self.storage_ring_mgr = storage_ring_mgr

    def get_skill(self, player: Player) -> str:
        return getattr(player, "life_skill", "") or ""

    @staticmethod
    def _round_cost(value: float) -> int:
        if value <= 0:
            return 0
        return max(_CHANGE_ROUND, int(round(value / _CHANGE_ROUND) * _CHANGE_ROUND))

    def base_change_cost(self, player: Player) -> int:
        """当前境界的基础首次更换费用。"""
        try:
            level = max(0, int(getattr(player, "level_index", 0) or 0))
        except (TypeError, ValueError):
            level = 0
        return self._round_cost(CHANGE_BASE * (1 + level * CHANGE_LEVEL_FACTOR))

    def change_cost_details(self, player: Player, now: int = None) -> dict:
        """返回基础费用、当前费用、峰值和距离最低费用的剩余时间。"""
        now = int(now or time.time())
        base = self.base_change_cost(player)
        try:
            peak = max(base, int(getattr(player, "life_skill_cost_peak", 0) or 0))
            last = max(0, int(getattr(player, "life_skill_last_change_at", 0) or 0))
        except (TypeError, ValueError):
            peak, last = base, 0
        if last <= 0 or peak <= base:
            return {
                "base": base, "current": base, "peak": base,
                "remaining_seconds": 0, "decayed": True,
            }
        elapsed = max(0, now - last)
        window = CHANGE_DECAY_HOURS * 3600
        ratio = min(1.0, elapsed / window)
        current = self._round_cost(base + (peak - base) * (1 - ratio))
        current = max(base, min(peak, current))
        remaining = max(0, window - elapsed)
        return {
            "base": base, "current": current, "peak": peak,
            "remaining_seconds": remaining, "decayed": remaining <= 0,
        }

    def get_change_cost(self, player: Player, now: int = None) -> int:
        return self.change_cost_details(player, now)["current"]

    def _format_remaining(self, seconds: int) -> str:
        seconds = max(0, int(seconds))
        if seconds <= 0:
            return "已降至基础费用"
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        if hours:
            return f"约 {hours} 小时 {minutes} 分钟后降至基础费用"
        return f"约 {max(1, minutes)} 分钟后降至基础费用"

    def _status_text(self, player: Player) -> str:
        details = self.change_cost_details(player)
        listing = "\n".join(f"  · {n}：{c['desc']}" for n, c in LIFE_SKILLS.items())
        current_skill = self.get_skill(player) or "未选择"
        changes = max(0, int(getattr(player, "life_skill_changes", 0) or 0))
        return (
            "⚒️ 生活职业\n"
            "━━━━━━━━━━━━━━━\n"
            f"当前职业：{current_skill}\n"
            f"已付费更换：{changes} 次\n"
            f"基础更换费用：{details['base']:,} 灵石\n"
            f"当前更换费用：{details['current']:,} 灵石\n"
            f"{self._format_remaining(details['remaining_seconds'])}\n"
            f"{listing}\n"
            "━━━━━━━━━━━━━━━\n"
            "💡 首次选择免费；之后更换按当前费用扣费。\n"
            "💡 用法：/选择生活职业 <名称>"
        )

    async def choose_skill(self, player: Player, skill_name: str) -> Tuple[bool, str]:
        """选择/改换生活职业；费用按境界、次数和距上次更换时间计算。"""
        if skill_name not in LIFE_SKILLS:
            return False, self._status_text(player)

        current = self.get_skill(player)
        if current == skill_name:
            return False, f"❌ 你已经是【{skill_name}】了。\n" + self._status_text(player)

        now = int(time.time())
        details = self.change_cost_details(player, now)
        cost = 0 if not current else details["current"]
        if cost and player.gold < cost:
            return False, (
                f"❌ 改换职业需要 {cost:,} 灵石，当前只有 {player.gold:,}。\n"
                f"{self._format_remaining(details['remaining_seconds'])}"
            )

        player.gold -= cost
        player.life_skill = skill_name
        if cost:
            peak = self._round_cost(cost * CHANGE_GROWTH)
            player.life_skill_cost_peak = min(CHANGE_MAX, max(details["base"], peak))
            player.life_skill_last_change_at = now
            player.life_skill_changes = max(0, int(getattr(player, "life_skill_changes", 0) or 0)) + 1
        else:
            # 首次选择免费；下一次更换从基础费用开始。
            player.life_skill_cost_peak = details["base"]
            player.life_skill_last_change_at = now
            player.life_skill_changes = 0
        await self.db.update_player(player)

        info = LIFE_SKILLS[skill_name]
        after = self.change_cost_details(player, now)
        return True, (
            f"⚒️ 生活职业设定成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"你已成为一名【{skill_name}】\n"
            f"专精效果：{info['desc']}\n"
            f"{info['perk_hint']}"
            + (f"\n本次花费：{cost:,} 灵石" if cost else "\n首次选择免费")
            + f"\n下次更换：{after['peak']:,} 灵石起"
            + f"\n{self._format_remaining(after['remaining_seconds'])}"
        )

    # 日课按境界发符：发的这张决定你当天能白扛什么品阶的丹劫
    DAILY_TALISMAN_BY_LEVEL = [
        (9, "避劫符"),   # Lv9+ 玄品护盾6，扛到圣品丹
        (0, "稳魂符"),   # Lv0-8 凡品护盾2，扛到灵品丹
    ]

    def _daily_talisman_name(self, player: Player) -> str:
        try:
            level = int(getattr(player, "level_index", 0) or 0)
        except (TypeError, ValueError):
            level = 0
        for threshold, name in self.DAILY_TALISMAN_BY_LEVEL:
            if level >= threshold:
                return name
        return "稳魂符"

    async def daily_talisman(self, player: Player) -> Tuple[bool, str]:
        """符箓师专属：每日免费护劫符（按境界发放）；按状态给出制符引导。"""
        skill = self.get_skill(player)
        if skill != "符箓师":
            return False, (
                "🖌️ 制符指引\n"
                "━━━━━━━━━━━━━━━\n"
                f"当前生活职业：{skill or '未选择'}\n"
                "制符是【符箓师】的专属能力，其他职业无法绘制符箓。\n"
                "· 转职：/选择生活职业 符箓师\n"
                "· 查看配方：/符箓配方\n"
                "· 获取符箓：符阁 / 坊市 / 拍卖会 / 历练奇遇"
            )

        today = time.strftime("%Y-%m-%d")
        key = f"talisman_{player.user_id}"
        last_day = await self.db.ext.get_system_config(key)
        if last_day == today:
            name = self._daily_talisman_name(player)
            return False, (
                "🖌️ 今日护劫符已领取\n"
                "━━━━━━━━━━━━━━━\n"
                f"每日免费【{name}】已于今日领取，明日 00:00 后刷新。\n"
                "继续绘制（消耗灵石与材料）：\n"
                "· /制符 烈焰符\n"
                "· /制符 寻宝符 2\n"
                "· 全部配方：/符箓配方"
            )

        if not self.storage_ring_mgr:
            return False, "❌ 系统异常：储物戒管理器未加载。"

        from ..core.talisman_rules import proficiency_level
        try:
            points = int(getattr(player, "talisman_exp", 0) or 0)
        except (TypeError, ValueError):
            points = 0
        # 大成（110 点）起每天 2 张；日课不计熟练点，它是职业标识不是收益
        count = 2 if proficiency_level(points) >= 4 else 1

        name = self._daily_talisman_name(player)
        success, msg = await self.storage_ring_mgr.store_item(player, name, count, silent=True)
        if not success:
            return False, (
                f"❌ 今日【{name}】已绘成，但存入储物戒失败：{msg}\n"
                "💡 请先 /储物戒 整理空间，或 /丢弃 无用物品后再发送 /制符。"
            )

        await self.db.ext.set_system_config(key, today)
        shield_note = "可保全灵品及以下丹药的丹劫" if name == "稳魂符" else "可保全圣品及以下丹药的丹劫"
        return True, (
            "🖌️ 符箓绘制成功！\n"
            "━━━━━━━━━━━━━━━\n"
            "你以朱砂灵墨勾画符纹，一笔成符——\n"
            f"获得【{name}】x{count}（已存入储物戒）\n"
            f"💡 {shield_note}；帝品起需自备混元符\n"
            "💡 继续绘制：/制符 <符名> [数量]｜全部配方：/符箓配方"
        )
