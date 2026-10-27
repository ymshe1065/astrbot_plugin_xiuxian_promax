# handlers/world_boss_handlers.py
import time
from astrbot.api.event import AstrMessageEvent
from ..data import DataBase
from ..managers.world_boss_manager import WorldBossManager
from ..models import Player
from .utils import player_required, set_event_flag

class WorldBossHandlers:
    def __init__(self, db: DataBase, wb_mgr: WorldBossManager):
        self.db = db
        self.wb_mgr = wb_mgr

    @player_required
    async def handle_challenge(self, player: Player, event: AstrMessageEvent):
        """挑战世界Boss"""
        success, msg = await self.wb_mgr.challenge_boss(player)
        set_event_flag(event, "world_boss_challenge", bool(success))
        settle_msg = getattr(self.wb_mgr, "last_settle_msg", None)
        if settle_msg:
            set_event_flag(event, "world_boss_settle_msg", settle_msg)
            self.wb_mgr.last_settle_msg = None
        yield event.plain_result(msg)

    async def handle_query(self, event: AstrMessageEvent):
        """查询世界Boss信息"""
        await self.wb_mgr.expire_bosses()
        async with self.db.conn.execute("SELECT * FROM world_boss WHERE status = 1") as cursor:
            row = await cursor.fetchone()
            if not row:
                yield event.plain_result("👹 当前没有存活的世界Boss，请等待天道降临。")
                return
            boss = dict(row)

        now = int(time.time())
        remaining = max(0, boss["expire_time"] - now)
        if remaining <= 0:
            # 兜底：刚好跨过到期秒时，顺手标记为已逃遁
            try:
                await self.db.conn.execute(
                    "UPDATE world_boss SET status = 3 WHERE boss_id = ? AND status = 1",
                    (boss["boss_id"],),
                )
                await self.db.conn.commit()
            except Exception:
                pass
            yield event.plain_result(
                f"👹 世界Boss【{boss['boss_name']}】已逃遁，等待天道下次降临。"
            )
            return

        hp_percent = (boss["current_hp"] / boss["max_hp"]) * 100
        remaining_hours = remaining // 3600
        remaining_minutes = (remaining % 3600) // 60
        type_cfg = self.wb_mgr.get_boss_type(boss)
        tier = int(type_cfg.get("tier", 1) or 1)
        tier_label = self.wb_mgr._tier_meta.get(tier, {}).get("label", f"{tier}阶")
        drop_names = "、".join(item.get("name", "") for item in self.wb_mgr._drop_tables.get(type_cfg.get("drop_table", "low"), []))
        min_level = int(type_cfg.get("min_level", self.wb_mgr.min_level) or self.wb_mgr.min_level)
        physical_resist, magic_resist = self.wb_mgr.get_resists(boss)

        msg = [
            f"{type_cfg.get('icon', '👹')} 世界Boss：{boss['boss_name']}",
            f"━━━━━━━━━━━━━━━",
            f"🏷️ 种类：{tier_label} · {type_cfg.get('element') or '无'}属性",
            f"📖 {type_cfg.get('desc', '')}",
            f"❤️ 血量：{boss['current_hp']:,} / {boss['max_hp']:,} ({hp_percent:.1f}%)",
            f"🛡️ 物抗：{physical_resist:.0f}% | 法抗：{magic_resist:.0f}% · 门槛境界：{min_level}",
            f"🎁 掉落：{drop_names}",
            f"⏳ 剩余存活时间：{remaining_hours} 小时 {remaining_minutes} 分",
            f"━━━━━━━━━━━━━━━",
            f"💡 发送 /挑战世界Boss 参与讨伐（单次伤害上限20%，每日不限次数）"
        ]

        # ===== 图片卡片：成功只发图，失败/关闭降级为纯文本 =====
        sent = False
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            if card_enabled("CARD_BOSS"):
                remaining = max(0, boss['expire_time'] - int(time.time()))
                card_path = await render_card_async(
                    "boss",
                    f"世界Boss · {boss['boss_name']}",
                    "天道警示 · 全服共讨",
                    sections=[
                        {"name": "Boss状态", "rows": [
                            ("种类", f"{tier_label} · {type_cfg.get('element') or '无'}属性"),
                            ("血量", f"{boss['current_hp']:,} / {boss['max_hp']:,}"),
                            ("血量百分比", f"{hp_percent:.1f}%"),
                            ("物抗/法抗", f"{physical_resist:.0f}% / {magic_resist:.0f}%"),
                            ("门槛境界", f"{min_level}"),
                            ("剩余时间", f"{remaining // 3600}小时{(remaining % 3600) // 60}分"),
                        ]},
                        {"name": "讨伐规则", "lines": [
                            "· 每日挑战不限次数，每次间隔 5 分钟",
                            "· 单次伤害保底 0.5%，上限 20%",
                            "· 按伤害排名发放灵石与随机掉落",
                            "· 尾刀斩杀者获称号【灭世魔神】",
                        ]},
                    ],
                    footer="/挑战世界Boss 参与讨伐",
                )
                if card_path:
                    yield event.image_result(card_path)
                    sent = True
        except Exception:
            pass
        if not sent:
            yield event.plain_result("\n".join(msg))