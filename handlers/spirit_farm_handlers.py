# handlers/spirit_farm_handlers.py
"""灵田处理器"""
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.spirit_farm_manager import SpiritFarmManager, parse_plant_args
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
                from ..managers.spirit_farm_manager import (
                    FARM_LEVELS, HYBRID_MARKER, collect_hybrid_jobs,
                    get_herbs_for_level, get_next_unlock,
                )
                import time as _time
                farm = await self.mgr.get_user_farm(player.user_id)
                if farm:
                    level_config = FARM_LEVELS.get(farm["level"], FARM_LEVELS[1])
                    farm_level = int(farm.get("level", 1) or 1)
                    now = int(_time.time())
                    rows = []
                    for job in collect_hybrid_jobs(farm["crops"]):
                        remaining = max(0, job["mature_time"] - now)
                        status = (
                            f"杂交中 还需{remaining // 3600}时{(remaining % 3600) // 60}分"
                            if remaining > 0 else "✅杂交完成"
                        )
                        slots_text = "/".join(f"格{slot}" for slot in job["slots"])
                        rows.append((f"{slots_text} → {job['product']}", status))
                    for crop in farm["crops"]:
                        if len(rows) >= 8:
                            break
                        if crop.get("name") == HYBRID_MARKER:
                            continue
                        slot = int(crop.get("slot") or 0)
                        remaining = max(0, crop["mature_time"] - now)
                        if remaining > 0:
                            status = f"还需{remaining // 3600}时{(remaining % 3600) // 60}分"
                        else:
                            status = "✅已成熟"
                        rows.append((f"格{slot}. {crop['name']}", status))
                    if not rows:
                        rows.append(("（空地）", "可种植"))
                    season_rows = []
                    try:
                        from ..managers.spirit_farm_manager import get_current_season
                        season = get_current_season()
                        season_rows.append(("当前季节", f"{season['icon']} {season['name']}"))
                    except Exception:
                        pass
                    next_level, next_herbs = get_next_unlock(farm_level)
                    unlock_rows = [("下一档解锁", f"Lv.{next_level} " + "、".join(next_herbs))] if next_level else []
                    card_path = await render_card_async(
                        "spirit",
                        f"我的灵田 Lv.{farm['level']}",
                        f"{player.user_name or '道友'} 的灵草园",
                        sections=[
                            {"name": "灵田", "rows": [
                                ("占用格数", f"{len(farm['crops'])}/{level_config['slots']}"),
                                ("土壤肥力", f"{farm.get('fertility', 100)}/100"),
                                ("可种植", "、".join(get_herbs_for_level(farm_level))),
                            ] + season_rows + unlock_rows},
                            {"name": "种植中", "rows": rows},
                        ],
                        footer="/种植 <灵草名> [数量|格号区间|all] · /杂交 <草A> <草B> · /收获 · /铲除 <格号>",
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
            from ..managers.spirit_farm_manager import (
                SPIRIT_HERBS, format_hybrid_recipes, get_herb_unlock_level,
            )
            farm = await self.mgr.get_user_farm(player.user_id)
            farm_level = int((farm or {}).get("level", 1) or 1)
            lines = [
                "🌱 可种植的灵草",
                "━━━━━━━━━━━━━━━",
                f"当前灵田：Lv.{farm_level}",
            ]
            for name, cfg in SPIRIT_HERBS.items():
                need = get_herb_unlock_level(name)
                mark = "✅" if farm_level >= need else f"🔒Lv.{need}"
                hours = max(1, int(cfg["grow_time"]) // 3600)
                note = "（需杂交种苗）" if cfg.get("hybrid_only") else ""
                lines.append(f"{mark} {name} - {hours}小时 (修为+{int(cfg['exp_yield']):,}){note}")
            lines.extend([
                "━━━━━━━━━━━━━━━",
                "🌱 所有灵草统一：种1收4（普通灵草可直接种植）",
                "🧬 杂交需占用2格，耗时与产物生长时间一致，完成后 /收获 领取1株：",
                format_hybrid_recipes(farm_level),
                "━━━━━━━━━━━━━━━",
                "💡 种植数量支持三种写法：",
                "   /种植 灵草 6      （种6株）",
                "   /种植 灵草 1-20   （种在1~20号空格）",
                "   /种植 灵草 all    （种满所有空格）",
                "💡 也可紧贴书写：/种植 灵草6、/种植 灵草1-20、/种植 灵草all",
                "💡 /种植 all 不写灵草时，自动沿用灵田里种得最多的灵草",
                "💡 提升灵田等级可解锁更高级灵草：/升级灵田",
            ])
            yield event.plain_result("\n".join(lines))
            return
        
        name, count, slot_range, fill_all = parse_plant_args(herb_name)
        success, msg = await self.mgr.plant_herb(
            player, name, count, slot_range=slot_range, fill_all=fill_all
        )
        set_event_flag(event, "farm_plant_ok", bool(success))
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

    @player_required
    async def handle_remove_crop(self, player: Player, event: AstrMessageEvent, slot: str = ""):
        """铲除指定格子的灵草"""
        if not (slot or "").strip():
            yield event.plain_result("❌ 请指定要铲除的格子序号，例如：/铲除 1")
            return
        try:
            slot_index = int((slot or "").strip())
        except ValueError:
            yield event.plain_result("❌ 格子序号必须是数字，例如：/铲除 1")
            return
        success, msg = await self.mgr.remove_crop(player, slot_index)
        set_event_flag(event, "farm_remove_ok", bool(success))
        set_event_flag(event, "farm_remove_slot", slot_index)
        yield event.plain_result(msg)
