# handlers/battle_event_handlers.py

import time

from astrbot.api.event import AstrMessageEvent

from ..data import DataBase
from ..managers.battle_event_manager import BattleEventManager
from ..models import Player
from .utils import player_required, set_event_flag


class BattleEventHandlers:
    def __init__(self, db: DataBase, battle_event_mgr: BattleEventManager):
        self.db = db
        self.battle_event_mgr = battle_event_mgr

    @player_required
    async def handle_attack(self, player: Player, event: AstrMessageEvent):
        success, msg = await self.battle_event_mgr.challenge_event(player)
        set_event_flag(event, "battle_event_attack", bool(success))
        set_event_flag(event, "battle_event_kill", bool(self.battle_event_mgr.last_kill))
        settle_msg = getattr(self.battle_event_mgr, "last_settle_msg", None)
        if settle_msg:
            set_event_flag(event, "battle_event_settle_msg", settle_msg)
            self.battle_event_mgr.last_settle_msg = None
        yield event.plain_result(msg)

    async def handle_status(self, event: AstrMessageEvent):
        await self.battle_event_mgr.expire_events()
        event_data = await self.battle_event_mgr.get_active_event()
        if not event_data:
            remaining = await self.battle_event_mgr.next_spawn_remaining()
            if remaining is None:
                yield event.plain_result("🎲 当前没有正在进行的江湖事件，请等待江湖快报。")
                return
            yield event.plain_result(
                "🎲 当前风平浪静，江湖暂无风波。\n"
                f"⏳ 下一场江湖事件预计 {remaining // 3600} 小时 {(remaining % 3600) // 60} 分后出现。\n"
                "💡 江湖事件仅在每日 10:00-24:00 之间出现。"
            )
            return

        type_cfg = self.battle_event_mgr._event_type_cfg(event_data)
        tier_cfg = self.battle_event_mgr._tier_cfg(event_data)
        now = int(time.time())
        remain = max(0, int(event_data.get("expire_time") or 0) - now)
        max_hp = max(1, int(event_data.get("max_hp") or 0))
        current_hp = max(0, int(event_data.get("current_hp") or 0))
        hp_percent = current_hp * 100.0 / max_hp
        cooldown = int(tier_cfg.get("cooldown", self.battle_event_mgr.cooldown) or self.battle_event_mgr.cooldown)
        min_level = int(event_data.get("min_level") or tier_cfg.get("min_level", 0) or 0)
        drop_table = tier_cfg.get("drop_table", type_cfg.get("drop_table", "low"))
        drop_names = ""
        if self.battle_event_mgr.world_boss_mgr:
            table = self.battle_event_mgr.world_boss_mgr._drop_tables.get(drop_table, [])
            drop_names = "、".join(str(item.get("name", "")) for item in table)

        lines = [
            f"{type_cfg.get('icon', '🎲')} 当前江湖事件：{tier_cfg.get('rank', '丁')}级{type_cfg.get('name', '事件')}【{event_data.get('event_name')}】",
            "━━━━━━━━━━━━━━━",
            f"📖 {type_cfg.get('desc', '')}",
            f"🏷️ 属性：{event_data.get('element') or '无'}",
            f"❤️ 气血：{current_hp:,} / {max_hp:,}（{hp_percent:.1f}%）",
            f"🚪 门槛境界：{min_level}",
            f"⚔️ 攻击间隔：{cooldown} 秒",
            f"⏳ 剩余时间：{remain // 3600} 小时 {(remain % 3600) // 60} 分",
        ]
        if drop_names:
            lines.append(f"🎁 掉落：{drop_names}")

        async with self.db.conn.execute(
            "SELECT user_id, total_damage FROM battle_event_damage "
            "WHERE event_id = ? ORDER BY total_damage DESC LIMIT 5",
            (event_data["event_id"],),
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        if rows:
            lines.append("🏆 当前伤害榜：")
            for index, row in enumerate(rows, start=1):
                target = await self.db.get_player_by_id(row["user_id"])
                name = target.user_name if target and target.user_name else row["user_id"]
                lines.append(f"  {index}. {name} - {int(row['total_damage']):,}")
        else:
            lines.append("🏆 当前伤害榜：暂无修士出手")

        lines.extend([
            "━━━━━━━━━━━━━━━",
            "💡 发送 /讨伐 参与围剿（别名：/追捕、/挑战通缉犯）",
        ])
        yield event.plain_result("\n".join(lines))
