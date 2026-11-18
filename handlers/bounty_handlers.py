# handlers/bounty_handlers.py
"""悬赏令处理器"""
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.bounty_manager import BountyManager
from ..models import Player
from .utils import player_required

__all__ = ["BountyHandlers"]

class BountyHandlers:
    """悬赏令处理器"""
    
    def __init__(self, db: DataBase, bounty_mgr: BountyManager):
        self.db = db
        self.bounty_mgr = bounty_mgr
    
    @player_required
    async def handle_bounty_list(self, player: Player, event: AstrMessageEvent):
        """显示每日通缉榜"""
        bounties = await self.bounty_mgr.get_bounty_list(player)
        remaining = await self.bounty_mgr.remaining_attempts(player)
        max_attempts = self.bounty_mgr.MAX_DAILY_ATTEMPTS

        lines = [
            "📜 通缉榜 · 今日悬赏",
            "━━━━━━━━━━━━━━━",
            f"🎯 今日剩余挑战次数：{remaining}/{max_attempts}",
            "",
        ]
        for b in bounties:
            reward = b.get("reward", {})
            lines.append(
                f"[{b['id']}] {b['name']}（{b.get('realm_name', '未知')}·{b.get('danger', '未知')}）\n"
                f"  - 流派：{b.get('archetype', '未知')} | 五行：{b.get('element') or '无'}\n"
                f"  - 气血：{int(b.get('max_hp', 0)):,} | 攻击：{int(b.get('atk', 0)):,} | 防御：{int(b.get('defense', 0)):,}\n"
                f"  - 功法：{b.get('main_technique') or '无'} | 装备：{len(b.get('equipment') or [])} 件\n"
                f"  - 悬赏：{int(reward.get('stone', 0)):,} 灵石 + {int(reward.get('exp', 0)):,} 修为"
            )
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("💡 /接取悬赏 <编号> 接取目标，接取后可查看详细数值")
        lines.append("💡 /挑战悬赏 开始战斗 | /放弃悬赏 缴纳罚款更换目标")

        yield event.plain_result("\n".join(lines))
    
    @player_required
    async def handle_accept_bounty(self, player: Player, event: AstrMessageEvent, bounty_id: int = 0):
        """接取悬赏"""
        if bounty_id <= 0:
            yield event.plain_result("❌ 请指定悬赏编号，例如：/接取悬赏 1")
            return
        
        success, msg = await self.bounty_mgr.accept_bounty(player, bounty_id)
        prefix = "✅" if success else "❌"
        yield event.plain_result(f"{prefix} {msg}")
    
    @player_required
    async def handle_bounty_status(self, player: Player, event: AstrMessageEvent):
        """查看悬赏状态"""
        success, msg = await self.bounty_mgr.check_bounty_status(player)
        yield event.plain_result(msg)
    
    @player_required
    async def handle_challenge_bounty(self, player: Player, event: AstrMessageEvent):
        """挑战已接取的通缉目标"""
        success, msg = await self.bounty_mgr.challenge_bounty(player)
        prefix = "✅" if success else "❌"
        yield event.plain_result(f"{prefix} {msg}")

    @player_required
    async def handle_complete_bounty(self, player: Player, event: AstrMessageEvent):
        """完成悬赏"""
        success, msg = await self.bounty_mgr.complete_bounty(player)
        prefix = "✅" if success else "❌"
        yield event.plain_result(f"{prefix} {msg}")
    
    @player_required
    async def handle_abandon_bounty(self, player: Player, event: AstrMessageEvent):
        """放弃悬赏"""
        self.last_abandon_success = False
        success, msg = await self.bounty_mgr.abandon_bounty(player)
        self.last_abandon_success = success
        prefix = "✅" if success else "❌"
        yield event.plain_result(f"{prefix} {msg}")
