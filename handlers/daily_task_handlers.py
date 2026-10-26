# handlers/daily_task_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.daily_task_manager import DailyTaskManager
from ..models import Player
from .utils import player_required

class DailyTaskHandlers:
    def __init__(self, db: DataBase, task_mgr: DailyTaskManager, storage_ring_mgr):
        self.db = db
        self.task_mgr = task_mgr
        self.storage_ring_mgr = storage_ring_mgr

    @player_required
    async def handle_task_board(self, player: Player, event: AstrMessageEvent):
        """查看任务榜"""
        msg = await self.task_mgr.get_task_board(player)
        yield event.plain_result(msg)

    @player_required
    async def handle_accept_task(self, player: Player, event: AstrMessageEvent, level: str = ""):
        """接取任务"""
        if not level:
            yield event.plain_result("❌ 请指定任务品阶，例如：/接取任务 黄")
            return
            
        success, msg = await self.task_mgr.generate_task(player, level)
        yield event.plain_result(msg)

    @player_required
    async def handle_abandon_task(self, player: Player, event: AstrMessageEvent, task_id: str = ""):
        """放弃进行中的任务"""
        if not task_id or not task_id.strip():
            tasks = await self.db.ext.get_user_active_tasks(player.user_id)
            if not tasks:
                yield event.plain_result("❌ 你当前没有进行中的任务，无需放弃。")
                return

            lines = [
                "🗑️ 放弃任务",
                "用法：/放弃任务 <编号>",
                "━━━━━━━━━━━━━━━",
            ]
            for t in tasks:
                lines.append(
                    f"[#{t['id']}] [{t['task_level']}阶] {t['title']}"
                    f"（{t['current_progress']}/{t['target_count']}）"
                )
            lines.append("💡 编号来自 /任务榜，也可以直接照上面填写。")
            yield event.plain_result("\n".join(lines))
            return

        raw = task_id.strip().lstrip("#")
        try:
            tid = int(raw)
        except ValueError:
            yield event.plain_result("❌ 任务编号必须是数字，例如：/放弃任务 12")
            return

        if tid <= 0:
            yield event.plain_result("❌ 任务编号必须大于 0。")
            return

        success, msg = await self.task_mgr.abandon_task(player, tid)
        yield event.plain_result(msg)

    @player_required
    async def handle_claim_reward(self, player: Player, event: AstrMessageEvent):
        """领取奖励"""
        success, msg, items = await self.task_mgr.claim_rewards(player)
        if success and items:
            # 将奖励材料存入储物戒
            item_lines = []
            for item in items:
                success_store, _ = await self.storage_ring_mgr.store_item(player, item["name"], item["count"], silent=True)
                if success_store:
                    item_lines.append(f"{item['name']} x{item['count']}")
                else:
                    item_lines.append(f"{item['name']} x{item['count']} (储物戒已满，丢失)")
            if item_lines:
                msg += "\n📦 获得物品：" + ", ".join(item_lines)
                
        yield event.plain_result(msg)