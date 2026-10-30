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

CHANGE_COST = 10000  # 改换职业费用


class LifeSkillManager:
    """生活职业管理器"""

    def __init__(self, db: DataBase, storage_ring_mgr=None):
        self.db = db
        self.storage_ring_mgr = storage_ring_mgr

    def get_skill(self, player: Player) -> str:
        return getattr(player, "life_skill", "") or ""

    async def choose_skill(self, player: Player, skill_name: str) -> Tuple[bool, str]:
        """选择/改换生活职业"""
        if skill_name not in LIFE_SKILLS:
            listing = "\n".join(f"  · {n}：{c['desc']}" for n, c in LIFE_SKILLS.items())
            current = self.get_skill(player) or "无"
            return False, (
                "⚒️ 生活职业\n"
                "━━━━━━━━━━━━━━━\n"
                f"当前职业：{current}\n"
                f"{listing}\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 用法：/选择生活职业 <名称>\n"
                f"改换职业需消耗 {CHANGE_COST:,} 灵石"
            )

        current = self.get_skill(player)
        if current == skill_name:
            return False, f"❌ 你已经是【{skill_name}】了。"

        cost = 0 if not current else CHANGE_COST
        if cost and player.gold < cost:
            return False, f"❌ 改换职业需要 {cost:,} 灵石。"

        player.gold -= cost
        player.life_skill = skill_name
        await self.db.update_player(player)

        info = LIFE_SKILLS[skill_name]
        return True, (
            f"⚒️ 生活职业设定成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"你已成为一名【{skill_name}】\n"
            f"专精效果：{info['desc']}\n"
            f"{info['perk_hint']}"
            + (f"\n花费：{cost:,} 灵石" if cost else "")
        )

    async def daily_talisman(self, player: Player) -> Tuple[bool, str]:
        """符箓师专属：每日免费绘制一张引雷符；按状态给出制符引导。"""
        skill = self.get_skill(player)
        if skill != "符箓师":
            return False, (
                "🖌️ 制符指引\n"
                "━━━━━━━━━━━━━━━\n"
                f"当前生活职业：{skill or '未选择'}\n"
                "制符是【符箓师】的专属能力，其他职业无法绘制符箓。\n"
                "· 转职：/选择生活职业 符箓师\n"
                "· 查看配方：/符箓配方\n"
                "· 获取符箓：百宝阁 / 坊市 / 拍卖会 / 历练奇遇"
            )

        today = time.strftime("%Y-%m-%d")
        key = f"talisman_{player.user_id}"
        last_day = await self.db.ext.get_system_config(key)
        if last_day == today:
            return False, (
                "🖌️ 今日引雷符已领取\n"
                "━━━━━━━━━━━━━━━\n"
                "每日免费【引雷符】已于今日领取，明日 00:00 后刷新。\n"
                "继续绘制（消耗灵石与材料）：\n"
                "· /制符 稳魂符\n"
                "· /制符 寻宝符 2\n"
                "· 全部配方：/符箓配方"
            )

        if not self.storage_ring_mgr:
            return False, "❌ 系统异常：储物戒管理器未加载。"

        success, msg = await self.storage_ring_mgr.store_item(player, "引雷符", 1, silent=True)
        if not success:
            return False, (
                f"❌ 今日引雷符已绘成，但存入储物戒失败：{msg}\n"
                "💡 请先 /储物戒 整理空间，或 /丢弃 无用物品后再发送 /制符。"
            )

        await self.db.ext.set_system_config(key, today)
        return True, (
            "🖌️ 符箓绘制成功！\n"
            "━━━━━━━━━━━━━━━\n"
            "你以朱砂灵墨勾画雷纹，一笔成符——\n"
            "获得【引雷符】x1（已存入储物戒）\n"
            "💡 高阶炼丹的丹劫保命神器\n"
            "💡 继续绘制：/制符 <符名> [数量]｜全部配方：/符箓配方"
        )
