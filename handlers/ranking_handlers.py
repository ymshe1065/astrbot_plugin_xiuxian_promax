# handlers/ranking_handlers.py
from astrbot.api.event import AstrMessageEvent
from ..managers.ranking_manager import RankingManager
from ..data.data_manager import DataBase

class RankingHandlers:
    def __init__(self, db: DataBase, rank_mgr: RankingManager):
        self.db = db
        self.rank_mgr = rank_mgr

    async def handle_rank_level(self, event: AstrMessageEvent):
        """境界排行"""
        success, msg = await self.rank_mgr.get_level_ranking()
        yield event.plain_result(msg)

    async def handle_rank_power(self, event: AstrMessageEvent):
        """战力排行"""
        success, msg = await self.rank_mgr.get_power_ranking(
            viewer_id=str(event.get_sender_id())
        )
        yield event.plain_result(msg)
    
    async def handle_rank_wealth(self, event: AstrMessageEvent):
        """财富排行"""
        success, msg = await self.rank_mgr.get_wealth_ranking()
        yield event.plain_result(msg)
    
    async def handle_rank_sect(self, event: AstrMessageEvent):
        """宗门排行"""
        success, msg = await self.rank_mgr.get_sect_ranking()
        yield event.plain_result(msg)
    
    async def handle_rank_deposit(self, event: AstrMessageEvent):
        """存款排行"""
        success, msg = await self.rank_mgr.get_deposit_ranking()
        yield event.plain_result(msg)
    
    async def handle_rank_sect_contribution(self, event: AstrMessageEvent):
        """宗门贡献排行（当前所在宗门）"""
        user_id = str(event.get_sender_id())
        player = await self.db.get_player_by_id(user_id)
        
        if not player:
            yield event.plain_result("❌ 你还未踏入修仙之路，请先发送「我要修仙」开始修行。")
            return
        
        if not player.sect_id or player.sect_id == 0:
            yield event.plain_result("❌ 你尚未加入任何宗门，无法查看宗门贡献排行。")
            return
        
        success, msg = await self.rank_mgr.get_contribution_ranking(player.sect_id)
        yield event.plain_result(msg)
