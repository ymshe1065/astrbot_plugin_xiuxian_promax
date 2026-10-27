# handlers/spirit_farm_handlers.py
"""灵田处理器"""
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.spirit_farm_manager import SpiritFarmManager
from ..models import Player
from .utils import player_required, set_event_flag

__all__ = ["SpiritFarmHandlers"]


class SpiritFarmHandlers:
    """灵田处理器"""
    
    def __init__(self, db: DataBase, farm_mgr: SpiritFarmManager):
        self.db = db
        self.mgr = farm_mgr
    
    @player_required
    async def handle_farm_info(self, player: Player, event: AstrMessageEvent):
        """查看灵田信息"""
        info = await self.mgr.get_farm_info(player.user_id)

        # ===== 图片卡片：成功只发图，失败/关闭降级为纯文本 =====
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            card_path = None
            if card_enabled("CARD_FARM"):
                from ..managers.spirit_farm_manager import FARM_LEVELS
                import time as _time
                farm = await self.mgr.get_user_farm(player.user_id)
                if farm:
                    level_config = FARM_LEVELS.get(farm["level"], FARM_LEVELS[1])
                    now = int(_time.time())
                    rows = []
                    for i, crop in enumerate(farm["crops"][:8], 1):
                        remaining = max(0, crop["mature_time"] - now)
                        if remaining > 0:
                            status = f"还需{remaining // 3600}时{(remaining % 3600) // 60}分"
                        else:
                            status = "✅已成熟"
                        rows.append((f"{i}. {crop['name']}", status))
                    if not rows:
                        rows.append(("（空地）", "可种植"))
                    season_rows = []
                    if hasattr(self.mgr, "get_current_season"):
                        season_rows.append(("当前季节", f"{self.mgr.get_current_season()['icon']} {self.mgr.get_current_season()['name']}"))
                    card_path = await render_card_async(
                        "spirit",
                        f"我的灵田 Lv.{farm['level']}",
                        f"{player.user_name or '道友'} 的灵草园",
                        sections=[
                            {"name": "灵田", "rows": [
                                ("种植格数", f"{len(farm['crops'])}/{level_config['slots']}"),
                                ("土壤肥力", f"{farm.get('fertility', 100)}/100"),
                            ] + season_rows},
                            {"name": "种植中", "rows": rows},
                        ],
                        footer="/种植 <灵草名> · /收获 · /施肥 恢复肥力",
                    )
            if card_path:
                yield event.image_result(card_path)
            else:
                yield event.plain_result(info)
        except Exception:
            yield event.plain_result(info)
    
    @player_required
    async def handle_create_farm(self, player: Player, event: AstrMessageEvent):
        """开垦灵田"""
        success, msg = await self.mgr.create_farm(player)
        yield event.plain_result(msg)
    
    @player_required
    async def handle_plant(self, player: Player, event: AstrMessageEvent, herb_name: str = ""):
        """种植灵草"""
        if not herb_name.strip():
            yield event.plain_result(
                "🌱 可种植的灵草\n"
                "━━━━━━━━━━━━━━━\n"
                "灵草 - 1小时 (修为+500)\n"
                "血灵草 - 2小时 (修为+1500)\n"
                "冰心草 - 4小时 (修为+4000)\n"
                "火焰花 - 8小时 (修为+10000)\n"
                "九叶灵芝 - 24小时 (修为+30000)\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 使用 /种植 <灵草名> 或 /种植 <灵草名><数量> 或 /种植 <灵草名> <数量>"
            )
            return
        
        # 解析数量后缀
        import re
        # 支持两种格式：灵草6 或 灵草 6
        match = re.match(r"(.*?)(\d+)$", herb_name.strip())
        if match:
            name = match.group(1).strip()
            count = int(match.group(2))
            count = max(1, count)  # 至少种植1株
        else:
            # 检查是否有空格分隔的数量
            parts = herb_name.strip().split()
            if len(parts) == 2 and parts[1].isdigit():
                name = parts[0].strip()
                count = int(parts[1])
                count = max(1, count)
            else:
                name = herb_name.strip()
                count = 1
        
        success, msg = await self.mgr.plant_herb(player, name, count)
        yield event.plain_result(msg)
    
    @player_required
    async def handle_harvest(self, player: Player, event: AstrMessageEvent):
        """收获灵草"""
        success, msg = await self.mgr.harvest(player)
        set_event_flag(event, "harvest_ok", bool(success))
        set_event_flag(event, "harvest_count", int((getattr(self.mgr, "last_harvest_counts", {}) or {}).get(player.user_id, 0) or 0))
        yield event.plain_result(msg)
    
    @player_required
    async def handle_upgrade_farm(self, player: Player, event: AstrMessageEvent):
        """升级灵田"""
        success, msg = await self.mgr.upgrade_farm(player)
        yield event.plain_result(msg)
