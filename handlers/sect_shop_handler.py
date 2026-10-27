# handlers/sect_shop_handler.py
"""宗门贡献商店：每个宗门独立随机 5 件商品，用宗门贡献购买。"""

import random
import time
from astrbot.api.event import AstrMessageEvent

from ..data import DataBase
from ..models import Player
from ..core import ItemRules
from .utils import player_required, set_event_flag

_TYPE_LABELS = {
    "weapon": "武器",
    "armor": "防具",
    "accessory": "饰品",
    "technique": "功法",
    "main_technique": "心法",
    "pill": "破境丹",
    "exp_pill": "修为丹",
    "utility_pill": "功能丹",
    "inventory_pill": "丹药",
    "legacy_pill": "丹药",
    "recipe": "丹方",
    "material": "材料",
    "符箓": "符箓",
    "杂物": "杂物",
}


class SectShopHandler:
    def __init__(self, db: DataBase, shop_manager, storage_ring_mgr, config, sect_mgr=None):
        self.db = db
        self.shop_manager = shop_manager
        self.storage_ring_mgr = storage_ring_mgr
        self.config = config
        self.sect_mgr = sect_mgr
        self.last_buy_success = False

    def _discounted_price(self, sect, base_price: int) -> int:
        """商堂每级 -2%，最高 -10%。"""
        if not sect or not self.sect_mgr:
            return int(base_price)
        level = self.sect_mgr.get_building_level(sect, "商堂")
        discount = min(0.10, 0.02 * max(0, level))
        return max(1, int(base_price * (1 - discount)))

    def _refresh_hours(self) -> int:
        values = self.config.get("VALUES", {}) or {}
        raw = values.get("PAVILION_REFRESH_HOURS", self.config.get("PAVILION_REFRESH_HOURS", 1))
        try:
            hours = int(raw)
        except (TypeError, ValueError):
            hours = 1
        return max(1, hours)

    async def _get_shop(self, sect_id: int, player: Player = None):
        shop_id = f"sect_shop_{sect_id}"
        last_refresh, items = await self.db.get_shop_data(shop_id)
        if items:
            changed = self.shop_manager.refresh_item_data(items)
            if changed:
                await self.db.update_shop_data(shop_id, last_refresh, items)
        hours = self._refresh_hours()
        if not items or self.shop_manager.should_refresh_shop(last_refresh, hours):
            pool = self.shop_manager.get_all_items_for_display(9999)
            try:
                pool = pool + self.shop_manager.get_recipes_for_display(9999)
            except Exception:
                pass
            if player is not None:
                filtered = []
                for item in pool:
                    req = ItemRules.get_required_level(item.get("data", {}) or {})
                    if req <= player.level_index:
                        filtered.append(item)
                pool = filtered
            if pool:
                weighted = []
                for item in pool:
                    weight = int((item.get("data", {}) or {}).get("shop_weight", 100) or 100)
                    weighted.append({**item, "weight": max(1, weight)})
                selected = self.shop_manager._weighted_random_choice(weighted, min(5, len(weighted)))
                items = [{**{k: v for k, v in item.items() if k != "weight"}, "stock": 1} for item in selected]
            else:
                items = []
            last_refresh = int(time.time())
            await self.db.update_shop_data(shop_id, last_refresh, items)
        return shop_id, last_refresh, items

    @player_required
    async def handle_shop(self, player: Player, event: AstrMessageEvent):
        if not player.sect_id:
            yield event.plain_result("❌ 你还没有加入宗门，无法使用宗门贡献商店。")
            return

        sect = await self.db.ext.get_sect_by_id(player.sect_id)
        if not sect:
            yield event.plain_result("❌ 宗门信息异常！")
            return

        _, last_refresh, items = await self._get_shop(player.sect_id, player)
        hours = self._refresh_hours()
        remaining = max(0, last_refresh + hours * 3600 - int(time.time()))

        lines = [
            f"🏪 【{sect.sect_name}】宗门贡献商店",
            f"你的贡献：{player.sect_contribution:,}",
            f"下次刷新：{remaining // 60} 分钟后",
            "━━━━━━━━━━━━━━━",
        ]
        if not items:
            lines.append("当前没有商品，请稍后再来。")
        else:
            for idx, item in enumerate(items, 1):
                stock = int(item.get("stock", 0) or 0)
                base_price = int(item.get("price", 0) or 0)
                price = self._discounted_price(sect, base_price)
                rank = item.get("rank", "凡品")
                status = f"库存 {stock}" if stock > 0 else "已售罄"
                type_label = _TYPE_LABELS.get(item.get("type", ""), "物品")
                price_text = f"{price:,} 贡献"
                if price < base_price:
                    price_text += f"（原价 {base_price:,}）"
                lines.append(
                    f"{idx}. [{rank}] {item['name']}（{type_label}）"
                    f" - {price_text}（{status}）"
                )
                effect = self.shop_manager._get_item_effect_short(item)
                if effect:
                    lines.append(f"   效果：{effect}")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("💡 使用 /宗门兑换 <编号> 购买")
        yield event.plain_result("\n".join(lines))

    async def _refund_contribution(self, user_id: str, amount: int):
        await self.db.conn.execute(
            "UPDATE players SET sect_contribution = sect_contribution + ? WHERE user_id = ?",
            (amount, user_id),
        )
        await self.db.conn.commit()

    async def _restore_stock(self, shop_id: str, item_name: str):
        last_refresh, items = await self.db.get_shop_data(shop_id)
        changed = False
        for item in items:
            if item.get("name") == item_name:
                item["stock"] = int(item.get("stock", 0) or 0) + 1
                changed = True
                break
        if changed:
            await self.db.update_shop_data(shop_id, last_refresh, items)

    @player_required
    async def handle_buy(self, player: Player, event: AstrMessageEvent, index: str = ""):
        set_event_flag(event, "sect_shop_buy", False)
        if not player.sect_id:
            yield event.plain_result("❌ 你还没有加入宗门，无法使用宗门贡献商店。")
            return

        try:
            idx = int((index or "").strip()) - 1
        except ValueError:
            idx = -1
        if idx < 0:
            yield event.plain_result("❌ 请输入商品编号，例如：/宗门兑换 1")
            return

        shop_id, _, items = await self._get_shop(player.sect_id, player)
        if idx >= len(items):
            yield event.plain_result("❌ 没有这个商品，请发 /宗门商店 查看编号。")
            return

        sect = await self.db.ext.get_sect_by_id(player.sect_id)
        item = items[idx]
        req_level = ItemRules.get_required_level(item.get("data", {}) or {})
        if player.level_index < req_level:
            yield event.plain_result(f"❌ 兑换【{item.get('name')}】需要达到境界等级 {req_level}。")
            return
        item_name = item.get("name", "")
        stock = int(item.get("stock", 0) or 0)
        price = self._discounted_price(sect, int(item.get("price", 0) or 0))
        if stock <= 0:
            yield event.plain_result(f"❌ 【{item_name}】已售罄，请等待刷新。")
            return
        if price <= 0:
            yield event.plain_result("❌ 商品价格异常，请联系管理员。")
            return
        if player.sect_contribution < price:
            yield event.plain_result(
                f"❌ 宗门贡献不足！【{item_name}】需要 {price:,} 贡献，"
                f"你当前 {player.sect_contribution:,}。"
            )
            return

        # 先扣贡献（原子校验，防止并发透支）
        cursor = await self.db.conn.execute(
            "UPDATE players SET sect_contribution = sect_contribution - ? "
            "WHERE user_id = ? AND sect_contribution >= ?",
            (price, player.user_id, price),
        )
        if cursor.rowcount == 0:
            await self.db.conn.rollback()
            yield event.plain_result("❌ 贡献扣除失败，请重试。")
            return
        await self.db.conn.commit()
        player.sect_contribution -= price

        # 扣库存
        reserved, _, _ = await self.db.decrement_shop_item_stock(
            shop_id, item_name, 1
        )
        if not reserved:
            await self._refund_contribution(player.user_id, price)
            yield event.plain_result(f"❌ 【{item_name}】已售罄，贡献已退回。")
            return

        item_type = item.get("type", "")
        if item_type in ("pill", "exp_pill", "utility_pill", "inventory_pill"):
            inventory = player.get_pills_inventory()
            pill_key = f"{item_name}|一般"
            inventory[pill_key] = inventory.get(pill_key, 0) + 1
            player.set_pills_inventory(inventory)
            # 只写丹药字段，避免用旧快照把刚原子扣除的贡献回滚
            await self.db.update_player_fields(
                player.user_id, pills_inventory=player.pills_inventory
            )
            set_event_flag(event, "sect_shop_buy", True)
            yield event.plain_result(
                f"✅ 兑换成功：{item_name}·一般 x1，已放入丹药背包。\n"
                f"💡 使用 /服用丹药 {item_name} 服用"
            )
            return

        if item_type in ("weapon", "armor", "accessory", "technique", "material", "recipe", "符箓", "杂物"):
            ok, msg = await self.storage_ring_mgr.store_item(
                player, item_name, 1, silent=True
            )
            if not ok:
                await self._refund_contribution(player.user_id, price)
                await self._restore_stock(shop_id, item_name)
                yield event.plain_result(
                    f"❌ 储物戒空间不足，兑换失败：{msg}\n贡献已退回。"
                )
                return
            set_event_flag(event, "sect_shop_buy", True)
            yield event.plain_result(
                f"✅ 兑换成功：{item_name} x1，已存入储物戒。\n"
                f"消耗宗门贡献：{price:,}"
            )
            return

        # 未知类型：退款并恢复库存
        await self._refund_contribution(player.user_id, price)
        await self._restore_stock(shop_id, item_name)
        yield event.plain_result(f"❌ 暂不支持兑换该类型物品（{item_type}），贡献已退回。")
