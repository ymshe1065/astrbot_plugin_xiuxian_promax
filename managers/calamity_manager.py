# managers/calamity_manager.py
"""天道天灾事件 - 魔潮降临，全服捐献抵御"""
import asyncio
import time
from typing import Optional, Tuple

from ..data.data_manager import DataBase
from ..models import Player
from .global_event_manager import GlobalEventManager

__all__ = ["CalamityManager"]


class CalamityManager:
    """魔潮事件：开启后全服修炼-20%，捐灵石填进度条抵御；超时未达标再罚24小时"""

    KEY_ACTIVE = "tide_active"
    KEY_END = "tide_end_time"
    KEY_TARGET = "tide_target"
    KEY_PROGRESS = "tide_progress"
    KEY_AFTERMATH = "tide_aftermath_until"
    KEY_SCALE = "tide_scale"
    DEBUFF_RATE = 0.20
    MIN_HOURS = 1
    MAX_HOURS = 48

    def __init__(self, db: DataBase, event_mgr: Optional[GlobalEventManager] = None):
        self.db = db
        self.event_mgr = event_mgr or GlobalEventManager(db)
        self._lock = asyncio.Lock()

    async def _get(self, key: str, default: str = "") -> str:
        value = await self.db.ext.get_system_config(key)
        return value if value is not None else default

    def _normalize_hours(self, hours) -> int:
        try:
            value = int(hours)
        except (TypeError, ValueError):
            value = 12
        return max(self.MIN_HOURS, min(self.MAX_HOURS, value))

    async def is_tide_active(self) -> bool:
        return await self._get(self.KEY_ACTIVE) == "1"

    async def is_aftermath(self) -> bool:
        until = await self._get(self.KEY_AFTERMATH)
        try:
            return bool(until) and int(time.time()) < int(until)
        except (TypeError, ValueError):
            return False

    async def start_tide(self, hours: int = 12, scale: str = "manual") -> Tuple[bool, str]:
        """管理员开启魔潮"""
        hours = self._normalize_hours(hours)

        async with self._lock:
            if await self.is_tide_active():
                return False, "❌ 魔潮已在肆虐中！"

            acquired, conflict_msg = await self.event_mgr.acquire(
                GlobalEventManager.EVENT_TIDE
            )
            if not acquired:
                return False, conflict_msg

            try:
                players = await self.db.get_all_players()
                if len(players) < 3:
                    await self.event_mgr.release(GlobalEventManager.EVENT_TIDE)
                    return False, (
                        "❌ 全服至少需要 3 名修士才能掀起魔潮"
                        f"（当前 {len(players)} 人）。"
                    )

                target = max(50000, len(players) * 10000)
                now = int(time.time())
                end_time = now + hours * 3600

                await self.db.ext.set_system_config(self.KEY_ACTIVE, "1")
                await self.db.ext.set_system_config(self.KEY_END, str(end_time))
                await self.db.ext.set_system_config(self.KEY_TARGET, str(target))
                await self.db.ext.set_system_config(self.KEY_PROGRESS, "0")
                await self.db.ext.set_system_config(self.KEY_SCALE, scale or "manual")
                await self.db.ext.set_system_config(
                    f"tide_auto_{time.strftime('%Y%m%d')}", "1"
                )
            except Exception:
                await self.db.ext.set_system_config(self.KEY_ACTIVE, "0")
                await self.event_mgr.release(GlobalEventManager.EVENT_TIDE)
                raise

            return True, (
                f"🌑【天道天灾】魔潮降临！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"域外魔气侵蚀天地灵脉，持续 {hours} 小时！\n"
                f"⚠️ 魔潮期间全服修炼效率 -20%\n"
                f"━━━━━━━━━━━━━━━\n"
                f"抵御目标：全服捐献 {target:,} 灵石\n"
                f"💡 发送 /抵御魔潮 <灵石> 参与抵御\n"
                f"若超时未达标，魔潮将再肆虐 24 小时！"
            )

    async def get_status(self) -> str:
        if not await self.is_tide_active():
            if await self.is_aftermath():
                try:
                    until = int(await self._get(self.KEY_AFTERMATH, "0"))
                except (TypeError, ValueError):
                    until = 0
                remain = max(0, until - int(time.time()))
                return (
                    f"🌑 魔潮虽退，魔气余毒仍在侵蚀灵脉……\n"
                    f"修炼效率 -20%，还剩 {remain // 3600} 小时"
                    f"{(remain % 3600) // 60}分"
                )
            return "🌤️ 当前风平浪静，天地灵脉运转如常。"

        end_time = int(await self._get(self.KEY_END, "0"))
        target = int(await self._get(self.KEY_TARGET, "0"))
        progress = int(await self._get(self.KEY_PROGRESS, "0"))
        remain = max(0, end_time - int(time.time()))
        pct = min(100.0, progress * 100.0 / max(1, target))

        filled = int(pct // 5)
        bar = "█" * filled + "░" * (20 - filled)

        return (
            f"🌑【魔潮肆虐中】\n"
            f"━━━━━━━━━━━━━━━\n"
            f"抵御进度 [{bar}] {pct:.1f}%\n"
            f"已捐献：{progress:,} / {target:,} 灵石\n"
            f"剩余时间：{remain // 3600}小时{(remain % 3600) // 60}分\n"
            f"⚠️ 魔潮期间全服修炼效率 -20%\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 发送 /抵御魔潮 <灵石> 参与抵御"
        )

    async def resist(self, player: Player, amount: int) -> Tuple[bool, str]:
        """玩家捐献灵石抵御魔潮（原子扣款与进度累加）"""
        async with self._lock:
            if not await self.is_tide_active():
                await self.event_mgr.release(GlobalEventManager.EVENT_TIDE)
                return False, "❌ 当前没有魔潮，无需抵御。"
            if amount <= 0:
                return False, "❌ 请输入有效的捐献金额。"
            if amount > 500000:
                return False, "❌ 单次捐献上限 500,000 灵石，量力而行。"

            target = int(await self._get(self.KEY_TARGET, "0"))
            now = int(time.time())
            await self.db.ext.ensure_system_config_table()

            await self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.conn.execute(
                    "UPDATE players SET gold = gold - ? "
                    "WHERE user_id = ? AND gold >= ?",
                    (amount, player.user_id, amount),
                )
                if cursor.rowcount != 1:
                    await self.db.conn.rollback()
                    fresh = await self.db.get_player_by_id(player.user_id)
                    current = fresh.gold if fresh else 0
                    return False, f"❌ 灵石不足！你当前持有 {current:,}。"

                await self.db.conn.execute(
                    "INSERT INTO system_config (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value = CAST(system_config.value AS INTEGER) + CAST(excluded.value AS INTEGER), "
                    "updated_at = excluded.updated_at",
                    (self.KEY_PROGRESS, str(amount), now),
                )
                await self.db.conn.execute(
                    "INSERT INTO system_config (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value = CAST(system_config.value AS INTEGER) + CAST(excluded.value AS INTEGER), "
                    "updated_at = excluded.updated_at",
                    (f"tide_contrib_{player.user_id}", str(amount), now),
                )
                async with self.db.conn.execute(
                    "SELECT value FROM system_config WHERE key = ?",
                    (self.KEY_PROGRESS,),
                ) as cursor:
                    row = await cursor.fetchone()
                await self.db.conn.commit()
            except Exception:
                await self.db.conn.rollback()
                raise

            player.gold = max(0, player.gold - amount)
            progress = int(row[0]) if row and row[0] is not None else amount

            if progress >= target:
                settlement = await self._settle_success_locked()
                return True, (
                    f"{settlement[1]}\n\n"
                    f"💰 你本次捐献：{amount:,} 灵石"
                )

            pct = min(100.0, progress * 100.0 / max(1, target))
            return True, (
                f"🛡️ 捐献成功！你向天道大阵注入 {amount:,} 灵石。\n"
                f"抵御进度：{progress:,}/{target:,}（{pct:.1f}%）\n"
                f"感谢你为天下苍生出力！"
            )

    async def _settle_success_locked(self) -> Tuple[bool, str]:
        """抵御成功结算（调用方需持有 self._lock）"""
        async with self.db.conn.execute(
            "SELECT key, value FROM system_config WHERE key LIKE 'tide_contrib_%'"
        ) as cursor:
            rows = await cursor.fetchall()

        bonus_note = ""
        for key, value in rows:
            uid = key.replace("tide_contrib_", "")
            try:
                refund = int(int(value) * 0.15)
            except (TypeError, ValueError):
                refund = 0
            await self.db.add_player_gold(uid, refund + 3000)
            await self.db.ext.set_system_config(
                f"tide_bless_{uid}", str(int(time.time()) + 86400)
            )

        if rows:
            bonus_note = (
                f"\n🧧 {len(rows)} 位义士各获 3,000 灵石 + 捐献额15%返还"
                f"\n🌟 义士获得 24 小时【天道庇佑】：闭关收益 +10%"
            )

        await self.db.conn.execute(
            "DELETE FROM system_config WHERE key LIKE 'tide_contrib_%'"
        )
        await self.db.conn.commit()

        await self.db.ext.set_system_config(self.KEY_ACTIVE, "0")
        await self.db.ext.set_system_config(self.KEY_AFTERMATH, "")
        await self.db.ext.set_system_config(self.KEY_PROGRESS, "0")
        await self.db.ext.set_system_config(self.KEY_END, "0")
        await self.db.ext.set_system_config(self.KEY_TARGET, "0")
        await self.db.ext.set_system_config(self.KEY_SCALE, "")
        await self.event_mgr.release(GlobalEventManager.EVENT_TIDE)

        return True, (
            f"🌟【天道昭彰】魔潮被成功抵御！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"天下修士同心戮力，魔气溃散！\n"
            f"全服修炼效率恢复正常{bonus_note}"
        )

    async def check_and_expire(self) -> Tuple[bool, str]:
        """超时未达标：魔潮再肆虐24小时（由定时任务调用）"""
        async with self._lock:
            return await self._check_and_expire_locked()

    async def _check_and_expire_locked(self) -> Tuple[bool, str]:
        if not await self.is_tide_active():
            await self.event_mgr.release(GlobalEventManager.EVENT_TIDE)
            return False, ""

        try:
            end_time = int(await self._get(self.KEY_END, "0"))
        except (TypeError, ValueError):
            end_time = 0
        if not end_time or int(time.time()) <= end_time:
            return False, ""

        try:
            progress = int(await self._get(self.KEY_PROGRESS, "0"))
            target = int(await self._get(self.KEY_TARGET, "0"))
        except (TypeError, ValueError):
            progress, target = 0, 0

        if progress >= target:
            return await self._settle_success_locked()

        await self.db.conn.execute(
            "DELETE FROM system_config WHERE key LIKE 'tide_contrib_%'"
        )
        await self.db.conn.commit()

        await self.db.ext.set_system_config(self.KEY_ACTIVE, "0")
        await self.db.ext.set_system_config(
            self.KEY_AFTERMATH, str(int(time.time()) + 86400)
        )
        await self.db.ext.set_system_config(self.KEY_PROGRESS, "0")
        await self.db.ext.set_system_config(self.KEY_END, "0")
        await self.db.ext.set_system_config(self.KEY_TARGET, "0")
        await self.db.ext.set_system_config(self.KEY_SCALE, "")
        await self.event_mgr.release(GlobalEventManager.EVENT_TIDE)

        return True, (
            f"💀【魔潮得逞】修士们未能及时抵御！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"魔气彻底侵蚀灵脉，全服修炼效率 -20% 将持续 24 小时……\n"
            f"（最终进度：{progress:,}/{target:,}）"
        )

    async def auto_check(self) -> Tuple[bool, str]:
        """定时任务入口：到期结算 + 按日期自动开启魔潮"""
        settled, msg = await self.check_and_expire()
        if settled:
            return True, msg

        if await self.is_tide_active() or await self.is_aftermath():
            return False, ""

        if await self.event_mgr.get_active_event():
            return False, ""

        local = time.localtime()
        key_today = f"tide_auto_{time.strftime('%Y%m%d')}"
        if await self._get(key_today):
            return False, ""

        is_first_of_month = local.tm_mday == 1
        is_saturday = local.tm_wday == 5
        hour_ok = local.tm_hour >= 20

        if not ((is_first_of_month or is_saturday) and hour_ok):
            return False, ""

        players = await self.db.get_all_players()
        if len(players) < 3:
            return False, ""

        hours = 8 if is_first_of_month else 5
        scale = "month" if is_first_of_month else "sat"
        ok, msg = await self.start_tide(hours, scale=scale)
        if ok:
            scale = "大魔潮（月初·8小时）" if is_first_of_month else "魔潮（周六·5小时）"
            return True, f"🌑【天道自动警示】{scale}降临！\n{msg}"
        return False, ""

    async def get_calendar(self) -> str:
        """仙界日历：未来事件预告"""
        local = time.localtime()
        season_idx = get_season_index_pub()
        seasons = ["🌱 春·生发", "☀️ 夏·炎灼", "🍂 秋·丰收", "❄️ 冬·蛰藏"]
        wday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

        days_to_sat = (5 - local.tm_wday) % 7
        if days_to_sat == 0:
            sat_when = "今天 20:00"
        else:
            sat_when = f"还有 {days_to_sat} 天"

        if local.tm_mday == 1:
            days_to_month = 0
        else:
            import calendar as _cal
            month_days = _cal.monthrange(local.tm_year, local.tm_mon)[1]
            days_to_month = month_days - local.tm_mday + 1
        month_when = "今天 20:00" if days_to_month == 0 else f"还有 {days_to_month} 天"

        tide_active = await self.is_tide_active()
        aftermath = await self.is_aftermath()
        tide_scale = (await self._get(self.KEY_SCALE)) if tide_active else ""
        if tide_active and not tide_scale:
            tide_scale = "month" if local.tm_mday == 1 else ("sat" if local.tm_wday == 5 else "manual")

        sat_text = "进行中" if tide_active and tide_scale == "sat" else sat_when
        month_text = "进行中" if tide_active and tide_scale == "month" else month_when

        lines = [
            "📅 仙界日历",
            "━━━━━━━━━━━━━━━",
            f"今日：{wday_names[local.tm_wday]} · {local.tm_mon}月{local.tm_mday}日",
            f"当前季节：{seasons[season_idx]}（每约一周轮转）",
            f"魔潮状态：{'🌑 肆虐中！/抵御魔潮 参与抵御' if tide_active else ('🌫 余波未散，修炼-20%' if aftermath else '🌤 风平浪静')}",
            "━━━━━━━━━━━━━━━",
            "【事件预告】",
            f"  · 周六 20:00：周六魔潮（{sat_text}）",
            f"  · 每月 1 日 20:00：月初大魔潮（8小时，{month_text}）",
            "  · 世界Boss：按全服周期自动降临（关注广播）",
            "  · 灵眼：每 2 小时出现一处",
            "━━━━━━━━━━━━━━━",
            "💡 魔潮来袭前囤好灵石，抵御成功者获 24 小时天道庇佑！",
        ]
        return "\n".join(lines)


def get_season_index_pub() -> int:
    """供日历使用的季节索引（避免循环导入 spirit_farm_manager）"""
    return (time.localtime().tm_yday // 7) % 4
