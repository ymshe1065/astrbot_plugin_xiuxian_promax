# managers/auction_manager.py
import asyncio
import math
import random
import time
from functools import wraps
from typing import Tuple
from astrbot.api import logger
from ..data import DataBase
from ..models import Player


def _serialized(func):
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        async with self._lock:
            return await func(self, *args, **kwargs)
    return wrapper


class AuctionManager:
    """玩家交易坊市管理器"""

    TAX_RATE = 0.05 # 系统收取 5% 的交易税，用于回收灵石

    NPC_NAMES = (
        "青阳散修", "坊市掌柜", "游方道人", "万宝阁执事", "蒙面客",
        "炼器铺老板", "药铺先生", "行脚商人", "拍卖行鉴宝师", "白衣剑客",
    )

    def __init__(self, db: DataBase, storage_ring_mgr, config: dict = None):
        self.db = db
        self.storage_ring_mgr = storage_ring_mgr
        self.config = config or {}
        self._lock = asyncio.Lock()

        cfg = {}
        try:
            cfg = self.config.get("MARKET_NPC", {}) or {}
        except Exception:
            cfg = {}

        self.npc_enabled = bool(cfg.get("ENABLED", True))
        self.npc_min_interval = max(1, self._as_int(cfg.get("MIN_INTERVAL"), 3000))
        self.npc_max_interval = max(self.npc_min_interval, self._as_int(cfg.get("MAX_INTERVAL"), 4200))
        self.npc_offer_expire = max(60, self._as_int(cfg.get("OFFER_EXPIRE"), 1800))
        self.npc_check_interval = max(5, self._as_int(cfg.get("CHECK_INTERVAL"), 60))
        self.npc_min_rate = self._as_float(cfg.get("MIN_RATE"), 0.80)
        self.npc_max_rate = self._as_float(cfg.get("MAX_RATE"), 1.20)
        self.npc_min_rate = min(10.0, max(0.01, self.npc_min_rate))
        self.npc_max_rate = min(10.0, max(0.01, self.npc_max_rate))
        if self.npc_max_rate < self.npc_min_rate:
            self.npc_min_rate, self.npc_max_rate = self.npc_max_rate, self.npc_min_rate

    @staticmethod
    def _as_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _next_offer_delay(self) -> int:
        return random.randint(self.npc_min_interval, self.npc_max_interval)

    def _pill_manager(self):
        from ..core.pill_manager import PillManager
        config_manager = getattr(self.storage_ring_mgr, "config_manager", None)
        if not config_manager:
            return None
        return PillManager(self.db, config_manager)

    def _instance_manager(self):
        from ..core.item_instance_manager import ItemInstanceManager
        config_manager = getattr(self.storage_ring_mgr, "config_manager", None)
        if not config_manager:
            return None
        return ItemInstanceManager(self.db, config_manager)

    @staticmethod
    def _display_name(item) -> str:
        name = item.get("item_name", "")
        quality = item.get("item_quality", "")
        if item.get("item_kind") in ("pill", "equipment") and quality:
            return f"{name}·{quality}"
        return name

    @_serialized
    async def list_item(self, player: Player, item_name: str, count: int, price: int, quality: str = "") -> Tuple[bool, str]:
        """上架物品（支持丹药按品质交易）"""
        if count <= 0:
            return False, "❌ 上架数量必须大于 0。"
        if price <= 0:
            return False, "❌ 单价必须大于 0。"

        pm = self._pill_manager()
        pill_key = None
        if pm:
            base_name, embedded_quality = pm.split_pill_key(item_name)
            if pm.get_pill_by_name(base_name):
                if "|" in str(item_name):
                    quality = quality or embedded_quality
                inventory = player.get_pills_inventory()
                pill_key = pm.select_pill_key(inventory, base_name, quality or None)
                if not pill_key:
                    hint = f"（{quality}）" if quality else ""
                    return False, f"❌ 你的丹药背包中没有【{base_name}】{hint}。"
                base_name, quality = pm.split_pill_key(pill_key)
                if int(inventory.get(pill_key, 0)) < count:
                    return False, f"❌ 你的【{base_name}·{quality}】数量不足（拥有 {inventory.get(pill_key, 0)}）。"
                inventory[pill_key] = int(inventory[pill_key]) - count
                if inventory[pill_key] <= 0:
                    del inventory[pill_key]
                player.set_pills_inventory(inventory)
                await self.db.update_player(player)

                now = int(time.time())
                next_offer_time = now + self._next_offer_delay() if self.npc_enabled else 0
                try:
                    await self.db.conn.execute(
                        "INSERT INTO auction_items (seller_id, item_name, count, price, create_time, next_offer_time, item_kind, item_quality) VALUES (?, ?, ?, ?, ?, ?, 'pill', ?)",
                        (player.user_id, base_name, count, price, now, next_offer_time, quality)
                    )
                    await self.db.conn.commit()
                except Exception as e:
                    inventory[pill_key] = int(inventory.get(pill_key, 0)) + count
                    player.set_pills_inventory(inventory)
                    await self.db.update_player(player)
                    logger.error(f"丹药上架失败，已恢复背包: {e}")
                    return False, "❌ 上架失败，丹药已退回背包。"
                total = price * count
                tip = "\n💬 坊市NPC可能稍后发来问价，使用 /我的坊市 查看。" if self.npc_enabled else ""
                return True, f"✅ 上架成功！\n丹药：【{base_name}·{quality}】x{count}\n单价：{price:,} 灵石\n总价：{total:,} 灵石{tip}"

        im = self._instance_manager()
        if im and im.item_type(item_name) in {"weapon", "armor", "head", "body", "boots", "gloves", "ring", "amulet", "accessory"}:
            if count != 1:
                return False, "❌ 装备实例一次只能上架 1 件。"
            insts = im.list_instances(player, "storage", item_name)
            if quality:
                insts = [x for x in insts if x.get("quality") == quality]
            if not insts:
                return False, f"❌ 你的储物戒中没有可上架的【{item_name}】装备实例。"
            inst = sorted(insts, key=lambda x: int(x.get("id", 0) or 0))[0]
            quality = inst.get("quality", "一般")
            im.move(player, int(inst["id"]), "auction")
            await self.db.update_player(player)

            now = int(time.time())
            next_offer_time = now + self._next_offer_delay() if self.npc_enabled else 0
            try:
                await self.db.conn.execute(
                    "INSERT INTO auction_items (seller_id, item_name, count, price, create_time, next_offer_time, item_kind, item_quality, instance_id) VALUES (?, ?, 1, ?, ?, ?, 'equipment', ?, ?)",
                    (player.user_id, item_name, price, now, next_offer_time, quality, int(inst["id"]))
                )
                await self.db.conn.commit()
            except Exception as e:
                im.move(player, int(inst["id"]), "storage")
                await self.db.update_player(player)
                logger.error(f"装备上架失败，已恢复储物戒: {e}")
                return False, "❌ 上架失败，装备已退回储物戒。"
            tip = "\n💬 坊市NPC可能稍后发来问价，使用 /我的坊市 查看。" if self.npc_enabled else ""
            return True, f"✅ 上架成功！\n装备：【{item_name}·{quality}】\n单价：{price:,} 灵石{tip}"

        # 检查物品是否真实存在于配置中（防止上架幽灵物品）
        if not self.storage_ring_mgr.has_item(player, item_name, count):
            return False, f"❌ 你的储物戒中没有足够的【{item_name}】。"

         # ===== 修复点：检查物品是否绑定 =====
        items_data = player.get_storage_ring_items()
        item_info = items_data.get(item_name)
        if isinstance(item_info, dict) and item_info.get("bound"):
            return False, f"❌【{item_name}】是角色绑定物品，无法上架坊市交易！"

        # 从储物戒扣除物品
        success, msg = await self.storage_ring_mgr.retrieve_item(player, item_name, count)
        if not success:
            return False, f"❌ 扣除物品失败：{msg}"

        # 写入坊市表
        now = int(time.time())
        next_offer_time = now + self._next_offer_delay() if self.npc_enabled else 0
        try:
            await self.db.conn.execute(
                "INSERT INTO auction_items (seller_id, item_name, count, price, create_time, next_offer_time, item_kind, item_quality, instance_id) VALUES (?, ?, ?, ?, ?, ?, 'item', '', 0)",
                (player.user_id, item_name, count, price, now, next_offer_time)
            )
            await self.db.conn.commit()
        except Exception as e:
            await self.storage_ring_mgr.store_item(player, item_name, count, silent=True)
            logger.error(f"物品上架失败，已恢复储物戒: {e}")
            return False, "❌ 上架失败，物品已退回储物戒。"

        total = price * count
        tip = "\n💬 坊市NPC可能稍后发来问价，使用 /我的坊市 查看。" if self.npc_enabled else ""
        return True, f"✅ 上架成功！\n物品：【{item_name}】x{count}\n单价：{price:,} 灵石\n总价：{total:,} 灵石{tip}"

    async def cleanup_orphan_listings(self) -> int:
        """清理卖家已不存在的挂单，避免坊市出现无法成交的幽灵商品。"""
        try:
            await self.db.conn.execute(
                "DELETE FROM auction_npc_offers WHERE auction_id IN "
                "(SELECT id FROM auction_items WHERE seller_id NOT IN (SELECT user_id FROM players))"
            )
            cursor = await self.db.conn.execute(
                "DELETE FROM auction_items WHERE seller_id NOT IN (SELECT user_id FROM players)"
            )
            await self.db.conn.commit()
            return int(cursor.rowcount or 0)
        except Exception as e:
            logger.warning(f"清理无主坊市挂单失败: {e}")
            return 0

    async def get_market_list(self) -> str:
        """查看坊市列表"""
        await self.cleanup_orphan_listings()
        # 按最新上架的排在前面
        async with self.db.conn.execute("SELECT * FROM auction_items ORDER BY id DESC LIMIT 30") as cursor:
            items = [dict(r) for r in await cursor.fetchall()]

        if not items:
            return "🕊️ 坊市目前空空如也，快去上架第一件商品吧！"

        msg = ["🏪 玩家交易坊市 (最近30条)", "━━━━━━━━━━━━━━━"]
        for item in items:
            # 查询卖家名字
            seller = await self.db.get_player_by_id(item["seller_id"])
            seller_name = seller.user_name if seller and seller.user_name else item["seller_id"][:6]
            if seller and seller.title:
                seller_name = f"【{seller.title}】{seller_name}"

            total_price = item["price"] * item["count"]

            # 截断过长的物品名，防止群聊刷屏
            full_name = self._display_name(item)
            display_name = full_name if len(full_name) <= 12 else full_name[:12] + "..."

            msg.append(f"[{item['id']}] {display_name} x{item['count']}")
            msg.append(f"  └ 总价: {total_price:,} 灵石 | 卖家: {seller_name}")

        msg.append("━━━━━━━━━━━━━━━")
        msg.append(f"💡 购买：/坊市购买 <编号> (系统收取 {self.TAX_RATE:.0%} 交易税)")
        msg.append("💡 上架：/坊市上架 <物品名/丹药名> [品质] <数量> <单价>")
        msg.append("💡 下架：/坊市下架 <编号>")
        if self.npc_enabled:
            msg.append("💡 NPC问价：/我的坊市 查看并选择卖出或继续等待")
        return "\n".join(msg)

    async def get_market_items(self, limit: int = 10) -> list:
        """获取最近上架的坊市条目（供图片卡片使用）。"""
        async with self.db.conn.execute(
            "SELECT * FROM auction_items ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        for row in rows:
            row["item_name"] = self._display_name(row)
        return rows

    @_serialized
    async def process_npc_offers(self, now: int = None) -> Tuple[int, int]:
        """生成/过期NPC问价，返回（新生成数, 过期数）"""
        if not self.npc_enabled:
            return 0, 0
        await self.cleanup_orphan_listings()
        if getattr(self.db.conn, "in_transaction", False):
            return 0, 0
        now = int(now or time.time())
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            expired = 0
            async with self.db.conn.execute(
                "SELECT id, auction_id FROM auction_npc_offers WHERE status = 'pending' AND expire_time <= ?",
                (now,)
            ) as cursor:
                expired_rows = [dict(r) for r in await cursor.fetchall()]
            for row in expired_rows:
                await self.db.conn.execute(
                    "UPDATE auction_npc_offers SET status = 'expired', responded_time = ? WHERE id = ? AND status = 'pending'",
                    (now, row["id"]),
                )
                await self._schedule_next_offer(row["auction_id"], now)
                expired += 1

            async with self.db.conn.execute(
                "SELECT id FROM auction_items WHERE next_offer_time = 0"
            ) as cursor:
                legacy_ids = [r[0] for r in await cursor.fetchall()]
            for auction_id in legacy_ids:
                await self.db.conn.execute(
                    "UPDATE auction_items SET next_offer_time = ? WHERE id = ?",
                    (now + self._next_offer_delay(), auction_id),
                )

            async with self.db.conn.execute(
                "SELECT * FROM auction_items WHERE next_offer_time > 0 AND next_offer_time <= ? ORDER BY id",
                (now,)
            ) as cursor:
                due_items = [dict(r) for r in await cursor.fetchall()]

            generated = 0
            for item in due_items:
                async with self.db.conn.execute(
                    "SELECT 1 FROM auction_npc_offers WHERE auction_id = ? AND status = 'pending' LIMIT 1",
                    (item["id"],)
                ) as cursor:
                    if await cursor.fetchone():
                        await self.db.conn.execute(
                            "UPDATE auction_items SET next_offer_time = -1 WHERE id = ?", (item["id"],)
                        )
                        continue

                async with self.db.conn.execute(
                    "SELECT 1 FROM players WHERE user_id = ?", (item["seller_id"],)
                ) as cursor:
                    if not await cursor.fetchone():
                        await self.db.conn.execute(
                            "UPDATE auction_items SET next_offer_time = 0 WHERE id = ?", (item["id"],)
                        )
                        continue

                rate = random.uniform(self.npc_min_rate, self.npc_max_rate)
                offer_price = max(1, int(round(item["price"] * rate)))
                npc_name = random.choice(self.NPC_NAMES)
                await self.db.conn.execute(
                    """INSERT INTO auction_npc_offers
                       (auction_id, seller_id, item_name, count, listed_price, offer_price,
                        npc_name, create_time, expire_time, status, responded_time)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0)""",
                    (
                        item["id"], item["seller_id"], self._display_name(item), item["count"],
                        item["price"], offer_price, npc_name, now, now + self.npc_offer_expire,
                    ),
                )
                await self.db.conn.execute(
                    "UPDATE auction_items SET next_offer_time = -1 WHERE id = ?", (item["id"],)
                )
                generated += 1

            await self.db.conn.execute(
                "DELETE FROM auction_npc_offers WHERE status <> 'pending' AND create_time < ?",
                (now - 86400,),
            )
            await self.db.conn.commit()
            return generated, expired
        except Exception:
            await self.db.conn.rollback()
            raise

    async def _schedule_next_offer(self, auction_id: int, now: int):
        await self.db.conn.execute(
            """UPDATE auction_items SET next_offer_time = ?
               WHERE id = ? AND NOT EXISTS (
                   SELECT 1 FROM auction_npc_offers WHERE auction_id = ? AND status = 'pending'
               )""",
            (now + self._next_offer_delay(), auction_id, auction_id),
        )

    async def _expire_seller_offers(self, seller_id: str, now: int):
        if getattr(self.db.conn, "in_transaction", False):
            return
        async with self.db.conn.execute(
            "SELECT id, auction_id FROM auction_npc_offers "
            "WHERE seller_id = ? AND status = 'pending' AND expire_time <= ?",
            (seller_id, now)
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        if not rows:
            return
        for row in rows:
            await self.db.conn.execute(
                "UPDATE auction_npc_offers SET status = 'expired', responded_time = ? WHERE id = ? AND status = 'pending'",
                (now, row["id"]),
            )
            await self._schedule_next_offer(row["auction_id"], now)
        await self.db.conn.commit()

    @_serialized
    async def get_player_market(self, player: Player) -> str:
        """查看个人坊市：上架商品与NPC问价"""
        now = int(time.time())
        await self._expire_seller_offers(player.user_id, now)

        async with self.db.conn.execute(
            "SELECT * FROM auction_items WHERE seller_id = ? ORDER BY id DESC",
            (player.user_id,)
        ) as cursor:
            listings = [dict(r) for r in await cursor.fetchall()]

        async with self.db.conn.execute(
            "SELECT * FROM auction_npc_offers WHERE seller_id = ? AND status = 'pending' AND expire_time > ? ORDER BY id",
            (player.user_id, now)
        ) as cursor:
            offers = [dict(r) for r in await cursor.fetchall()]

        if not listings:
            return "🕊️ 你当前没有在坊市上架任何商品。\n💡 上架：/坊市上架 <物品名/丹药名> [品质] <数量> <单价>"

        offers_by_auction = {o["auction_id"]: o for o in offers}
        msg = ["🏪 我的坊市", "━━━━━━━━━━━━━━━"]
        for item in listings:
            total = item["price"] * item["count"]
            msg.append(f"[坊市#{item['id']}] {self._display_name(item)} x{item['count']}")
            msg.append(f"  └ 定价：{item['price']:,}/件 · 总价 {total:,} 灵石")
            offer = offers_by_auction.get(item["id"])
            if offer:
                remaining = max(0, offer["expire_time"] - now)
                remain_min = max(1, math.ceil(remaining / 60))
                offer_total = offer["offer_price"] * offer["count"]
                diff = offer["offer_price"] - item["price"]
                sign = "+" if diff >= 0 else ""
                msg.append(
                    f"  💬 问价[#{offer['id']}] {offer['npc_name']}："
                    f"{offer['offer_price']:,}/件（总价 {offer_total:,}，{sign}{diff:,}）"
                )
                msg.append(f"     ⏳ 剩余 {remain_min} 分钟")
            elif item.get("next_offer_time", 0) > now:
                eta_min = max(1, math.ceil((item["next_offer_time"] - now) / 60))
                msg.append(f"  ⏳ 等待NPC问价（约 {eta_min} 分钟后）")
            elif item.get("next_offer_time", 0) == -1:
                msg.append("  ⏳ NPC正在考虑，请稍候")
            else:
                msg.append("  ⏳ 等待NPC问价")

        msg.append("━━━━━━━━━━━━━━━")
        if offers:
            msg.append("💡 /坊市卖出 <问价ID> 接受问价并卖出")
            msg.append("💡 /坊市等待 <问价ID> 放弃本次问价，继续等待")
        msg.append("💡 /坊市下架 <坊市ID> 撤回商品")
        if self.npc_enabled:
            msg.append(
                f"📈 NPC问价区间：{self.npc_min_rate:.0%} ~ {self.npc_max_rate:.0%} 定价，"
                f"每 {self.npc_min_interval}~{self.npc_max_interval} 秒可能来问一次"
            )
        return "\n".join(msg)

    @_serialized
    async def accept_offer(self, player: Player, offer_id: int) -> Tuple[bool, str]:
        """接受NPC问价，按问价卖出整件商品"""
        now = int(time.time())
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.conn.execute(
                "SELECT * FROM auction_npc_offers WHERE id = ?", (offer_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                await self.db.conn.rollback()
                return False, "❌ 该问价不存在或已结束。"
            offer = dict(row)

            if offer["seller_id"] != player.user_id:
                await self.db.conn.rollback()
                return False, "❌ 只能处理自己的NPC问价。"
            if offer["status"] != "pending":
                await self.db.conn.rollback()
                return False, "❌ 该问价已失效，请查看 /我的坊市。"
            if offer["expire_time"] <= now:
                await self.db.conn.execute(
                    "UPDATE auction_npc_offers SET status = 'expired', responded_time = ? WHERE id = ?",
                    (now, offer_id),
                )
                await self._schedule_next_offer(offer["auction_id"], now)
                await self.db.conn.commit()
                return False, "⏰ 该NPC问价已过期，已继续等待下一位买家。"

            async with self.db.conn.execute(
                "SELECT * FROM auction_items WHERE id = ?", (offer["auction_id"],)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                await self.db.conn.execute(
                    "DELETE FROM auction_npc_offers WHERE auction_id = ?", (offer["auction_id"],)
                )
                await self.db.conn.commit()
                return False, "❌ 该商品已不在坊市，问价自动取消。"
            item = dict(row)

            if item["seller_id"] != player.user_id:
                await self.db.conn.rollback()
                return False, "❌ 商品归属异常，无法交易。"

            if item.get("item_kind") == "equipment":
                im = self._instance_manager()
                if im:
                    im.remove(player, int(item.get("instance_id", 0) or 0))
                    await self.db.update_player(player, commit=False)

            total = offer["offer_price"] * item["count"]
            await self.db.add_player_gold(player.user_id, total, commit=False)
            await self.db.conn.execute(
                "DELETE FROM auction_npc_offers WHERE auction_id = ?", (offer["auction_id"],)
            )
            await self.db.conn.execute(
                "DELETE FROM auction_items WHERE id = ?", (offer["auction_id"],)
            )
            await self.db.conn.commit()

            return True, (
                f"✅ NPC交易达成！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"买家：{offer['npc_name']}\n"
                f"商品：【{self._display_name(item)}】x{item['count']}\n"
                f"成交价：{offer['offer_price']:,}/件（定价 {item['price']:,}）\n"
                f"获得灵石：{total:,}（NPC收购免税）"
            )
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"坊市NPC问价卖出异常: {e}")
            return False, "❌ 交易异常，已取消，请稍后再试。"

    @_serialized
    async def decline_offer(self, player: Player, offer_id: int) -> Tuple[bool, str]:
        """拒绝本次NPC问价，继续等待下一次"""
        now = int(time.time())
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.conn.execute(
                "SELECT * FROM auction_npc_offers WHERE id = ?", (offer_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                await self.db.conn.rollback()
                return False, "❌ 该问价不存在或已结束。"
            offer = dict(row)

            if offer["seller_id"] != player.user_id:
                await self.db.conn.rollback()
                return False, "❌ 只能处理自己的NPC问价。"
            if offer["status"] != "pending":
                await self.db.conn.rollback()
                return False, "❌ 该问价已失效，请查看 /我的坊市。"

            if offer["expire_time"] <= now:
                await self.db.conn.execute(
                    "UPDATE auction_npc_offers SET status = 'expired', responded_time = ? WHERE id = ?",
                    (now, offer_id),
                )
                await self._schedule_next_offer(offer["auction_id"], now)
                await self.db.conn.commit()
                return False, "⏰ 该NPC问价已过期，已继续等待下一位买家。"

            await self.db.conn.execute(
                "UPDATE auction_npc_offers SET status = 'rejected', responded_time = ? WHERE id = ?",
                (now, offer_id),
            )
            await self._schedule_next_offer(offer["auction_id"], now)
            await self.db.conn.commit()

            return True, (
                f"🤝 已婉拒【{offer['npc_name']}】的问价。\n"
                f"商品继续留在坊市，等待下一位NPC问价。"
            )
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"坊市NPC问价等待异常: {e}")
            return False, "❌ 操作异常，请稍后再试。"

    @_serialized
    async def buy_item(self, buyer: Player, auction_id: int) -> Tuple[bool, str]:
        """购买物品（带事务与防并发处理）"""
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. 检查商品是否存在
            async with self.db.conn.execute("SELECT * FROM auction_items WHERE id = ?", (auction_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await self.db.conn.rollback()
                    return False, "❌ 该商品不存在或已被其他玩家买走。"
                item_data = dict(row)

            if item_data["seller_id"] == buyer.user_id:
                await self.db.conn.rollback()
                return False, "❌ 不能购买自己上架的物品，如需撤回请使用下架功能。"

            total_price = item_data["price"] * item_data["count"]
            if buyer.gold < total_price:
                await self.db.conn.rollback()
                return False, f"❌ 灵石不足！\n需要 {total_price:,} 灵石，你只有 {buyer.gold:,} 灵石。"

            is_pill_item = item_data.get("item_kind") == "pill"
            is_equipment_item = item_data.get("item_kind") == "equipment"
            if is_equipment_item:
                im = self._instance_manager()
                seller = await self.db.get_player_by_id(item_data["seller_id"])
                inst = im.get_instance(seller, item_data.get("instance_id", 0)) if im and seller else None
                if not inst:
                    await self.db.conn.rollback()
                    return False, "❌ 该装备实例已不存在，交易取消。"
                volume = self.storage_ring_mgr._get_item_volume(item_data["item_name"])
                if self.storage_ring_mgr.get_available_slots(buyer) < volume:
                    await self.db.conn.rollback()
                    return False, "❌ 你的储物戒空间不足，无法接收该装备。"
                if not im.change_owner(seller, buyer, int(inst["id"])):
                    await self.db.conn.rollback()
                    return False, "❌ 装备实例转移失败，交易取消。"
                await self.db.update_player(seller, commit=False)
                await self.db.update_player(buyer, commit=False)
            elif is_pill_item:
                pm = self._pill_manager()
                if not pm:
                    await self.db.conn.rollback()
                    return False, "❌ 丹药交易模块未加载。"
                quality = item_data.get("item_quality") or pm.DEFAULT_QUALITY
                pill_key = pm.make_pill_key(item_data["item_name"], quality)
                inventory = buyer.get_pills_inventory()
                inventory[pill_key] = int(inventory.get(pill_key, 0)) + int(item_data["count"])
                buyer.set_pills_inventory(inventory)
                await self.db.update_player(buyer, commit=False)
            else:
                # 2. 尝试存入买家储物戒
                success, store_msg = await self.storage_ring_mgr.store_item(
                    buyer, item_data["item_name"], item_data["count"],
                    external_transaction=True, silent=True
                )
                if not success:
                    await self.db.conn.rollback()
                    return False, f"❌ 购买失败：你的储物戒空间不足，请先清理背包。"

            # 3. 扣除买家灵石
            buyer.gold -= total_price
            await self.db.update_player(buyer, commit=False)

            # 4. 给卖家打款（扣除 5% 交易税）
            seller = await self.db.get_player_by_id(item_data["seller_id"])
            if seller:
                seller_income = int(total_price * (1 - self.TAX_RATE))
                seller.gold += seller_income
                await self.db.update_player(seller, commit=False)

            # 5. 从坊市和NPC问价中移除该记录
            await self.db.conn.execute("DELETE FROM auction_npc_offers WHERE auction_id = ?", (auction_id,))
            await self.db.conn.execute("DELETE FROM auction_items WHERE id = ?", (auction_id,))
            await self.db.conn.commit()

            target_desc = "丹药背包" if is_pill_item else "储物戒"
            return True, f"✅ 购买成功！\n花费：{total_price:,} 灵石\n获得：【{self._display_name(item_data)}】x{item_data['count']}（已存入{target_desc}）"

        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"坊市购买异常: {e}")
            return False, "❌ 交易异常，已取消购买，请联系管理员。"

    @_serialized
    async def cancel_item(self, player: Player, auction_id: int) -> Tuple[bool, str]:
        """下架物品"""
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.conn.execute("SELECT * FROM auction_items WHERE id = ?", (auction_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await self.db.conn.rollback()
                    return False, "❌ 该商品不存在或已被买走。"
                item_data = dict(row)

            if item_data["seller_id"] != player.user_id:
                await self.db.conn.rollback()
                return False, "❌ 只能下架自己上架的物品。"

            is_pill_item = item_data.get("item_kind") == "pill"
            is_equipment_item = item_data.get("item_kind") == "equipment"
            if is_equipment_item:
                im = self._instance_manager()
                instance_id = int(item_data.get("instance_id", 0) or 0)
                volume = self.storage_ring_mgr._get_item_volume(item_data["item_name"])
                target_location = "storage"
                target_desc = "储物戒"
                if self.storage_ring_mgr.get_available_slots(player) < volume:
                    target_location = "overflow"
                    target_desc = "溢出仓库"
                if not im or not im.move(player, instance_id, target_location):
                    await self.db.conn.rollback()
                    return False, "❌ 装备实例不存在，无法下架。"
                await self.db.update_player(player, commit=False)
            elif is_pill_item:
                pm = self._pill_manager()
                if not pm:
                    await self.db.conn.rollback()
                    return False, "❌ 丹药交易模块未加载。"
                quality = item_data.get("item_quality") or pm.DEFAULT_QUALITY
                pill_key = pm.make_pill_key(item_data["item_name"], quality)
                inventory = player.get_pills_inventory()
                inventory[pill_key] = int(inventory.get(pill_key, 0)) + int(item_data["count"])
                player.set_pills_inventory(inventory)
                await self.db.update_player(player, commit=False)
                target_desc = "丹药背包"
            else:
                success, msg = await self.storage_ring_mgr.store_item(
                    player, item_data["item_name"], item_data["count"],
                    external_transaction=True, silent=True
                )
                if not success:
                    await self.db.conn.rollback()
                    return False, f"❌ 储物戒已满，无法下架退回物品。请清理背包后再试。"
                await self.db.update_player(player, commit=False)
                target_desc = "储物戒"

            await self.db.conn.execute("DELETE FROM auction_npc_offers WHERE auction_id = ?", (auction_id,))
            await self.db.conn.execute("DELETE FROM auction_items WHERE id = ?", (auction_id,))
            await self.db.conn.commit()

            return True, f"✅ 下架成功！\n【{self._display_name(item_data)}】x{item_data['count']} 已退回到你的{target_desc}。"
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"坊市下架异常: {e}")
            return False, "❌ 下架异常，请稍后再试。"
