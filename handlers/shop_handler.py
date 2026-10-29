# handlers/shop_handler.py

import time
import re
import random
from typing import Tuple
from astrbot.api.event import AstrMessageEvent
from astrbot.api import AstrBotConfig, logger
from ..data import DataBase
from ..core import ShopManager, EquipmentManager, PillManager, StorageRingManager, ItemRules
from ..models import Player
from ..config_manager import ConfigManager
from ..rank_rules import normalize_rank
from .utils import player_required, set_event_flag

__all__ = ["ShopHandler"]

class ShopHandler:
    """商店处理器"""
    
    ITEM_ACQUIRE_HINTS = {
        'pill': "丹阁刷新、秘境稀有掉落",
        'exp_pill': "丹阁、炼丹系统、历练/秘境修为奖励",
        'utility_pill': "丹阁、秘境掉落、炼丹产出",
        'inventory_pill': "丹阁刷新、炼丹产出",
        'legacy_pill': "丹阁刷新，购买后进入丹药背包",
        'recipe': "丹方阁购买后学习",
        'material': "材料阁、灵田、历练、秘境、悬赏",
        'material_package': "材料阁购买后开启",
        '符箓': "符箓师绘制、百宝阁、历练/奇遇掉落",
        'weapon': "器阁、百宝阁、黑市",
        'armor': "器阁、百宝阁",
        'head': "器阁、百宝阁",
        'body': "器阁、百宝阁",
        'gloves': "器阁、百宝阁",
        'boots': "器阁、百宝阁",
        'ring': "百宝阁、黑市",
        'amulet': "百宝阁、黑市",
        'accessory': "百宝阁、黑市",
        'main_technique': "百宝阁稀有刷新、历练/秘境/奇遇低概率掉落、Boss/世界Boss/团本稀有掉落",
        'technique': "百宝阁、宗门商店、历练、秘境、奇遇、Boss 掉落",
        'storage_ring': "基础储物戒初始自带；高级储物戒商店/百宝阁购买",
    }

    def __init__(self, db: DataBase, config: AstrBotConfig, config_manager: ConfigManager):
        self.db = db
        self.config = config
        self.config_manager = config_manager
        self.shop_manager = ShopManager(config, config_manager)
        self.storage_ring_manager = StorageRingManager(db, config_manager)
        self.equipment_manager = EquipmentManager(db, config_manager, self.storage_ring_manager)
        self.pill_manager = PillManager(db, config_manager)
        self.last_buy_success = False
        self._last_viewed_pavilion: dict[str, str] = {}
        access_control = self.config.get("ACCESS_CONTROL", {})
        self.shop_manager_ids = {
            str(user_id)
            for user_id in access_control.get("SHOP_MANAGERS", [])
        }

    def _cfg(self, key: str, default):
        """读取配置：优先新 schema 的 VALUES 层，兼容旧根键。"""
        values = self.config.get("VALUES", {}) or {}
        return values.get(key, self.config.get(key, default))

    def _sender_key(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    def _remember_viewed_pavilion(self, event: AstrMessageEvent, pavilion_id: str) -> None:
        """记录玩家最近查看的货架，购买同名物品时优先从该货架扣库存。"""
        uid = self._sender_key(event)
        if not uid:
            return
        self._last_viewed_pavilion[uid] = pavilion_id
        if len(self._last_viewed_pavilion) > 512:
            for stale in list(self._last_viewed_pavilion)[:128]:
                self._last_viewed_pavilion.pop(stale, None)

    def _viewed_pavilion(self, event: AstrMessageEvent) -> str:
        return self._last_viewed_pavilion.get(self._sender_key(event), "")

    def _rare_recipe_count(self) -> int:
        """丹方阁每次随机刷新的稀有丹方数量（突破丹方常驻，不占此名额）。"""
        raw = self._cfg("PAVILION_RARE_RECIPE_COUNT", self._cfg("PAVILION_RECIPE_COUNT", 3))
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 3

    async def _ensure_pavilion_refreshed(self, pavilion_id: str, item_getter, count: int, apply_rank_band: bool = False) -> None:
        """确保阁楼已刷新"""
        last_refresh_time, current_items = await self.db.get_shop_data(pavilion_id)
        if current_items:
            updated = self.shop_manager.ensure_items_have_stock(current_items)
            updated = self.shop_manager.ensure_equipment_quality(current_items) or updated
            updated = self.shop_manager.refresh_item_data(current_items) or updated
            if updated:
                await self.db.update_shop_data(pavilion_id, last_refresh_time, current_items)
        refresh_hours = self._cfg("PAVILION_REFRESH_HOURS", 1)
        if not current_items or self.shop_manager.should_refresh_shop(last_refresh_time, refresh_hours):
            if apply_rank_band:
                try:
                    players = await self.db.get_all_players()
                    self.shop_manager.resolve_rank_band(players, force=True)
                except Exception:
                    pass
            new_items = self.shop_manager.generate_pavilion_items(item_getter, count)
            await self.db.update_shop_data(pavilion_id, int(time.time()), new_items)

    async def auto_restock(self) -> Tuple[int, str]:
        """v6.17 自动上货（定时任务调用）：
        1) 到刷新周期照常换货；2) 未到周期但货架售罄率≥80% 时提前补货（不变更刷新周期锚点）
        返回 (补货阁数, 描述)"""
        import time as _time
        restocked = 0
        details = []
        try:
            players = await self.db.get_all_players()
            self.shop_manager.resolve_rank_band(players, force=True)
        except Exception:
            pass
        jobs = [
            ("pill_pavilion", "丹阁", self.shop_manager.get_pills_for_display, self._cfg("PAVILION_PILL_COUNT", 20)),
            ("recipe_pavilion", "丹方阁", self.shop_manager.get_recipes_for_display, self._rare_recipe_count()),
            ("material_pavilion", "材料阁", self.shop_manager.get_materials_for_display, self._cfg("PAVILION_MATERIAL_COUNT", 20)),
            ("weapon_pavilion", "器阁", self.shop_manager.get_weapons_for_display, self._cfg("PAVILION_WEAPON_COUNT", 30)),
            ("treasure_pavilion", "百宝阁", self.shop_manager.get_all_items_for_display, self._cfg("PAVILION_TREASURE_COUNT", 40)),
        ]
        for pavilion_id, name, getter, cnt in jobs:
            try:
                last_refresh, items = await self.db.get_shop_data(pavilion_id)
                if items:
                    data_changed = self.shop_manager.ensure_equipment_quality(items)
                    data_changed = self.shop_manager.refresh_item_data(items) or data_changed
                    if data_changed:
                        await self.db.update_shop_data(pavilion_id, last_refresh, items)
                refresh_hours = self._cfg("PAVILION_REFRESH_HOURS", 1)
                due = self.shop_manager.should_refresh_shop(last_refresh, refresh_hours)
                if not items:
                    # 空货架（从未初始化）：立即上货
                    new_items = self.shop_manager.generate_pavilion_items(getter, cnt)
                    await self.db.update_shop_data(pavilion_id, int(_time.time()), new_items)
                    restocked += 1
                    details.append(name)
                    continue
                if due:
                    new_items = self.shop_manager.generate_pavilion_items(getter, cnt)
                    await self.db.update_shop_data(pavilion_id, int(_time.time()), new_items)
                    restocked += 1
                    details.append(name)
                    continue
                # 售罄检测：有货商品占比 < 20% 则补货
                total = len(items)
                on_sale = sum(1 for it in items if it.get('stock', 0) > 0)
                if total > 0 and on_sale / total < 0.2:
                    new_items = self.shop_manager.generate_pavilion_items(getter, cnt)
                    await self.db.update_shop_data(pavilion_id, last_refresh, new_items)  # 保持刷新锚点不变
                    restocked += 1
                    details.append(f"{name}(售罄补货)")
            except Exception as e:
                from astrbot.api import logger
                logger.warning(f"自动上货 {name} 异常: {e}")
        return restocked, "、".join(details) if details else ""

    async def handle_pill_pavilion(self, event: AstrMessageEvent, section: str = ""):
        """丹阁：默认丹药，可选丹方/材料分区。"""
        section = (section or "").strip()
        jobs = {
            "丹药": ("pill_pavilion", "丹阁", self.shop_manager.get_pills_for_display, self._cfg("PAVILION_PILL_COUNT", 20)),
            "丹方": ("recipe_pavilion", "丹方阁", self.shop_manager.get_recipes_for_display, self._rare_recipe_count()),
            "材料": ("material_pavilion", "材料阁", self.shop_manager.get_materials_for_display, self._cfg("PAVILION_MATERIAL_COUNT", 20)),
        }
        if not section:
            yield event.plain_result(
                "🏯 丹阁分区\n"
                "━━━━━━━━━━━━━━━\n"
                "  · /丹阁 丹药：成品丹药\n"
                "  · /丹阁 丹方：可购买学习的丹方卷轴\n"
                "  · /丹阁 材料：炼丹材料\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 购买使用 /购买 <物品名> [数量]"
            )
            return
        if section not in jobs:
            yield event.plain_result("❌ 丹阁分区只有：丹药、丹方、材料。")
            return
        pavilion_id, name, getter, count = jobs[section]
        await self._ensure_pavilion_refreshed(pavilion_id, getter, count)
        self._remember_viewed_pavilion(event, pavilion_id)
        last_refresh, items = await self.db.get_shop_data(pavilion_id)
        if not items:
            yield event.plain_result(f"{name}暂无物品出售。")
            return
        refresh_hours = self._cfg("PAVILION_REFRESH_HOURS", 1)
        display = self.shop_manager.format_pavilion_display(name, items, refresh_hours, last_refresh)
        cards = await self._pavilion_cards(name, items)
        if cards:
            for card in cards:
                yield event.image_result(card)
        else:
            yield event.plain_result(display)

    async def handle_weapon_pavilion(self, event: AstrMessageEvent):
        """处理器阁命令 - 展示武器列表"""
        count = self._cfg("PAVILION_WEAPON_COUNT", 30)
        await self._ensure_pavilion_refreshed("weapon_pavilion", self.shop_manager.get_weapons_for_display, count, apply_rank_band=True)
        self._remember_viewed_pavilion(event, "weapon_pavilion")
        last_refresh, items = await self.db.get_shop_data("weapon_pavilion")
        if not items:
            yield event.plain_result("器阁暂无武器出售。")
            return
        refresh_hours = self._cfg("PAVILION_REFRESH_HOURS", 1)
        display = self.shop_manager.format_pavilion_display("器阁", items, refresh_hours, last_refresh)
        cards = await self._pavilion_cards("器阁", items)
        if cards:
            for card in cards:
                yield event.image_result(card)
        else:
            yield event.plain_result(display)

    async def handle_treasure_pavilion(self, event: AstrMessageEvent):
        """处理百宝阁命令 - 展示所有物品"""
        count = self._cfg("PAVILION_TREASURE_COUNT", 40)
        await self._ensure_pavilion_refreshed("treasure_pavilion", self.shop_manager.get_all_items_for_display, count, apply_rank_band=True)
        self._remember_viewed_pavilion(event, "treasure_pavilion")
        last_refresh, items = await self.db.get_shop_data("treasure_pavilion")
        if not items:
            yield event.plain_result("百宝阁暂无物品出售。")
            return
        refresh_hours = self._cfg("PAVILION_REFRESH_HOURS", 1)
        display = self.shop_manager.format_pavilion_display("百宝阁", items, refresh_hours, last_refresh)
        cards = await self._pavilion_cards("百宝阁", items)
        if cards:
            for card in cards:
                yield event.image_result(card)
        else:
            yield event.plain_result(display)

    async def _pavilion_cards(self, pavilion_name: str, items: list):
        """百宝阁按品阶分开成卡，同品阶超过 5 件时再分页；其他阁保持整架长图。"""
        available = [item for item in items if item.get("stock", 0) > 0]
        if not available:
            return []
        if pavilion_name != "百宝阁":
            card = await self._pavilion_card(pavilion_name, available)
            return [card] if card else []
        cards = []
        for rank, group in self.shop_manager.group_items_by_rank(available):
            page_size = 5
            pages = [group[i:i + page_size] for i in range(0, len(group), page_size)]
            for index, page in enumerate(pages, 1):
                title = f"百宝阁 · {rank}区"
                if len(pages) > 1:
                    title += f" ({index}/{len(pages)})"
                card = await self._pavilion_card(pavilion_name, page, title=title)
                if card:
                    cards.append(card)
        return cards

    async def _pavilion_card(self, pavilion_name: str, items: list, title: str = ""):
        """阁楼列表的磨砂玻璃卡片（关闭或失败时不产出任何消息）"""
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            if not card_enabled("CARD_SHOP"):
                return None
            lines = []
            count = 0
            for item in items:
                stock = item.get('stock', 0)
                if stock <= 0:
                    continue
                discount = item.get('discount', 1.0)
                price_tag = f"{item['price']:,}灵石"
                if discount < 1.0:
                    price_tag += f"({round(discount * 10, 1)}折)"
                element = item.get('element') or (item.get('data') or {}).get('element', '')
                elem_tag = f"[{element}]" if element else ""
                permanent_tag = " [常驻]" if item.get('always_available') else ""
                count += 1
                lines.append(f"{item['name']}{elem_tag}{permanent_tag} - {price_tag} · 库存{stock}")
                lines.extend(self.shop_manager.get_trade_details(item))
                lines.append("")
            if not lines:
                return None
            card_path = await render_card_async(
                "shop",
                title or f"{pavilion_name} · 今日货架",
                f"共 {count} 种在售",
                sections=[{"name": "在售清单", "lines": lines}],
                footer="购买 <物品名> [数量] · 售罄商品不显示",
            )
            if card_path:
                return card_path
            return None
        except Exception:
            return None

    async def _find_item_in_pavilions(self, item_name: str, preferred_pavilion: str = ""):
        """在所有阁楼中查找物品；同一物品重复上架时优先匹配玩家刚查看的货架。

        优先返回仍有库存的货架；若全部售罄，则返回首个匹配条目，供购买流程提示“已售罄”。
        """
        pavilion_ids = ["pill_pavilion", "recipe_pavilion", "material_pavilion", "weapon_pavilion", "treasure_pavilion"]
        if preferred_pavilion in pavilion_ids:
            pavilion_ids.remove(preferred_pavilion)
            pavilion_ids.insert(0, preferred_pavilion)
        sold_out = None
        for pavilion_id in pavilion_ids:
            last_refresh, items = await self.db.get_shop_data(pavilion_id)
            if items:
                changed = self.shop_manager.ensure_equipment_quality(items)
                changed = self.shop_manager.refresh_item_data(items) or changed
                if changed:
                    await self.db.update_shop_data(pavilion_id, last_refresh, items)
                for item in items:
                    if item['name'] != item_name:
                        continue
                    if item.get('stock', 0) > 0:
                        return pavilion_id, item
                    if sold_out is None:
                        sold_out = (pavilion_id, item)
        return sold_out if sold_out else (None, None)

    @player_required
    async def handle_buy(self, player: Player, event: AstrMessageEvent, item_name: str = ""):
        """处理购买物品命令"""
        set_event_flag(event, "shop_buy", False)
        if not item_name or item_name.strip() == "":
            yield event.plain_result("请指定要购买的物品名称，例如：购买 青铜剑")
            return

        # 兼容全角空格/数字与“x10”写法
        normalized = item_name.strip().replace("　", " ")
        normalized = normalized.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        quantity = 1
        item_part = normalized

        def parse_qty(text: str):
            text = re.sub(r"\s+", " ", text)
            m = re.match(r"^(.*?)(?:\s+(\d+)|[xX＊*]\s*(\d+))$", text)
            if m:
                part = m.group(1).strip()
                qty_str = m.group(2) or m.group(3)
                return part, int(qty_str)
            return text.strip(), 1

        item_part, quantity = parse_qty(normalized)
        if quantity <= 0:
            yield event.plain_result("❌ 购买数量必须大于 0。")
            return

        if quantity == 1:
            try:
                raw_msg = event.get_message_str().strip()
                raw_msg = raw_msg.lstrip('/').strip()  # 兼容 /购买 斜杠前缀，防止物品名带上命令前缀
                # 最长前缀优先：购买物品 必须先于 购买 剥离
                for prefix in ("购买物品", "购买"):
                    if raw_msg.startswith(prefix):
                        raw_msg = raw_msg[len(prefix):].strip()
                        break
                raw_msg = raw_msg.replace("　", " ")
                raw_msg = raw_msg.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
                parsed_name, parsed_qty = parse_qty(raw_msg)
                # 仅当重新解析得到有效物品名时才采用（防御性，避免前缀残留污染名称）
                if parsed_name and not parsed_name.startswith("/"):
                    item_part, quantity = parsed_name, parsed_qty
            except Exception:
                pass

        if quantity <= 0:
            yield event.plain_result("❌ 购买数量必须大于 0。")
            return

        item_name = item_part

        pavilion_id, target_item = await self._find_item_in_pavilions(item_name, self._viewed_pavilion(event))
        if not target_item:
            known = self.shop_manager.find_item_by_name(item_name)
            if known:
                acquire_hint = self._get_acquire_hint(known.get('type', ''), known)
                yield event.plain_result(
                    f"【{item_name}】当前未在货架上（货架每 1 小时刷新）。\n"
                    f"💡 获取途径：{acquire_hint}\n"
                    "💡 材料/材料包可查看 /丹阁 材料，其他物品可查看 /器阁、/百宝阁\n"
                    "💡 也可先发 /物品信息 <名称> 查询物品详情"
                )
                return
            yield event.plain_result(
                f"没有找到【{item_name}】。\n"
                "💡 物品名需与货架完全一致，且该物品当前在售（每 1 小时刷新）：\n"
                "  · 丹药 → /丹阁 丹药    丹方 → /丹阁 丹方    材料/材料包 → /丹阁 材料\n"
                "  · 武器/装备 → /器阁    符箓/杂物 → /百宝阁\n"
                "💡 也可先发 /物品信息 <名称> 查询物品详情与获取途径"
            )
            return

        stock = target_item.get('stock', 0)
        if stock <= 0:
            yield event.plain_result(f"【{item_name}】已售罄，请等待货架刷新。")
            return
        limited_note = ""
        if self.shop_manager.is_gear_entry(target_item) and quantity > 1:
            quantity = 1
            limited_note = "⚠️ 装备每次限购 1 件，已按 1 件结算。\n"
        if quantity > stock:
            yield event.plain_result(f"【{item_name}】库存不足，当前库存: {stock}。")
            return

        price = target_item['price']
        total_price = price * quantity
        if player.gold < total_price:
            yield event.plain_result(
                f"灵石不足！\n【{target_item['name']}】价格: {price} 灵石\n"
                f"购买数量: {quantity}\n需要灵石: {total_price}\n你的灵石: {player.gold}"
            )
            return

        result_lines = []
        norm_type = ItemRules.normalize_shop_type(target_item)
        equippable_types = ItemRules.EQUIPPABLE_TYPES

        # ===== 门槛前置检查（事务前，无任何扣除，附当前可买清单）=====
        if norm_type in equippable_types:
            rank = normalize_rank(target_item.get('rank'), '凡品')
            req_level = ItemRules.get_required_level(target_item)
            if player.level_index < req_level:
                # 收集同一阁楼中玩家当前买得起的同类物品
                _, shelf_items = await self.db.get_shop_data(pavilion_id)
                affordable = []
                for it in (shelf_items or []):
                    if it.get('stock', 0) <= 0:
                        continue
                    if ItemRules.normalize_shop_type(it) != norm_type:
                        continue
                    it_req = ItemRules.get_required_level(it)
                    if player.level_index >= it_req:
                        affordable.append(f"【{it['name']}】({it.get('rank', '')} · {it['price']:,}灵石)")
                affordable_text = "\n".join(f"  · {a}" for a in affordable[:8]) if affordable else "  · 暂无（等货架刷新或先提升境界）"
                yield event.plain_result(
                    f"❌ 境界不足，无法购买！\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"【{target_item['name']}】为 {rank} 品阶，需要达到境界等级 {req_level}\n"
                    f"你当前境界等级：{player.level_index}（还差 {req_level - player.level_index} 级）\n"
                    f"💡 通过 /闭关 /历练 /探索秘境 提升修为后 /突破 提高境界\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"✅ 你当前可购买的同类物品：\n{affordable_text}"
                )
                return

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(event.get_sender_id())
            if player.gold < total_price:
                await self.db.conn.rollback()
                yield event.plain_result(
                    f"灵石不足！\n【{target_item['name']}】价格: {price} 灵石\n"
                    f"购买数量: {quantity}\n需要灵石: {total_price}\n你的灵石: {player.gold}"
                )
                return

            reserved, _, remaining = await self.db.decrement_shop_item_stock(pavilion_id, item_name, quantity, external_transaction=True)
            if not reserved:
                await self.db.conn.rollback()
                yield event.plain_result(f"【{item_name}】已售罄，请等待刷新。")
                return

            item_type = ItemRules.normalize_shop_type(target_item)

            # ===== 门槛防线（前置检查已拦截，此处仅事务内兜底）=====
            if item_type in equippable_types:
                rank = normalize_rank(target_item.get('rank'), '凡品')
                req_level = ItemRules.get_required_level(target_item)

                if player.level_index < req_level:
                    await self.db.conn.rollback()
                    yield event.plain_result(f"❌ 你的境界不足以驾驭此等神物！购买【{rank}】物品需要达到境界等级 {req_level}。")
                    return
            # ==========================================

            if item_type in equippable_types:
                success, msg = await self.storage_ring_manager.store_item(
                    player, target_item['name'], quantity, external_transaction=True,
                    quality=target_item.get('quality') or "",
                )
                if not success:
                    await self.db.conn.rollback()
                    yield event.plain_result(f"❌ 购买失败：储物戒空间不足。\n{msg}\n本次未扣除灵石。")
                    return
                type_name = {
                    "weapon": "武器", "armor": "防具", "head": "头饰", "body": "甲胄",
                    "gloves": "手套", "boots": "靴子", "ring": "戒指",
                    "amulet": "护符", "accessory": "饰品",
                    "main_technique": "心法", "technique": "功法",
                }.get(item_type, "装备")
                result_lines.append(f"成功购买{type_name}【{target_item['name']}】x{quantity}。\n{msg}")
            
            elif item_type == 'inventory_pill':
                self.pill_manager._reset_daily_if_needed(player)
                inventory = player.get_pills_inventory()
                pill_key = self.pill_manager.make_pill_key(target_item['name'], '一般')
                inventory[pill_key] = inventory.get(pill_key, 0) + quantity
                player.set_pills_inventory(inventory)
                result_lines.append(
                    f"成功购买丹药【{target_item['name']}·一般】x{quantity}，已放入丹药背包。"
                )
                result_lines.append(f"💡 使用 /服用丹药 {target_item['name']} [品质] [数量]")

            elif item_type == 'recipe':
                success, msg = await self.storage_ring_manager.store_item(player, target_item['name'], quantity, external_transaction=True)
                if success:
                    result_lines.append(f"成功购买丹方【{target_item['name']}】x{quantity}，已存入储物戒。")
                    result_lines.append(f"💡 使用 /学习丹方 {target_item['name']} 学习")
                else:
                    await self.db.conn.rollback()
                    yield event.plain_result(f"❌ 丹方存入储物戒失败：{msg}")
                    return

            elif item_type == 'material':
                success, msg = await self.storage_ring_manager.store_item(player, target_item['name'], quantity, external_transaction=True)
                if not success:
                    await self.db.conn.rollback()
                    yield event.plain_result(f"❌ 物品存入储物戒失败：{msg}\n本次未扣除灵石。")
                    return
                raw_type = (target_item.get('data') or {}).get('type')
                type_name = "符箓" if raw_type == "符箓" else "材料"
                result_lines.append(f"成功购买{type_name}【{target_item['name']}】x{quantity}。\n{msg}")
            elif item_type == 'material_package':
                opened = {}
                for _ in range(quantity):
                    picks = self.shop_manager.open_material_package(target_item)
                    if not picks:
                        await self.db.conn.rollback()
                        yield event.plain_result(
                            f"❌ 开启【{target_item['name']}】失败：暂无可开出的材料，本次未扣除灵石。"
                        )
                        return
                    for material_name, count in picks:
                        opened[material_name] = opened.get(material_name, 0) + count
                for material_name, count in opened.items():
                    success, msg = await self.storage_ring_manager.store_item(
                        player, material_name, count, external_transaction=True, silent=True
                    )
                    if not success:
                        await self.db.conn.rollback()
                        yield event.plain_result(f"❌ 材料包开启失败：{msg}\n本次未扣除灵石。")
                        return
                gained = "、".join(
                    f"{name}×{count}" for name, count in sorted(opened.items(), key=lambda kv: (-kv[1], kv[0]))
                )
                result_lines.append(f"成功购买并开启【{target_item['name']}】x{quantity}，获得：{gained}")
            else:
                await self.db.conn.rollback()
                yield event.plain_result(f"未知的物品类型：{item_type}")
                return

            player.gold -= total_price
            await self.db.update_player(player, commit=False)
            await self.db.conn.commit()
            
            result_lines.append(f"花费灵石: {total_price}，剩余: {player.gold}")
            result_lines.append(f"剩余库存: {remaining}" if remaining > 0 else "该物品已售罄！")
            if limited_note:
                result_lines.insert(0, limited_note.strip())
            set_event_flag(event, "shop_buy", True)
            yield event.plain_result("\n".join(result_lines))
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"购买异常: {e}")
            raise

    def _get_acquire_hint(self, item_type: str, item: dict = None) -> str:
        """优先返回物品自定义获取途径，否则按类型返回提示"""
        data = (item or {}).get("data") or {}
        hint = data.get("acquire_hint") if isinstance(data, dict) else None
        if hint:
            return hint
        return self.ITEM_ACQUIRE_HINTS.get(item_type, "商店刷新或活动奖励")

    async def handle_item_info(self, event: AstrMessageEvent, item_name: str = ""):
        """查询物品/丹药的具体效果与获取方式"""
        if not item_name or item_name.strip() == "":
            yield event.plain_result(
                "请指定要查询的物品名称\n"
                "用法：物品信息 <名称>\n"
                "示例：物品信息 筑基丹"
            )
            return

        item = self.shop_manager.find_item_by_name(item_name.strip())
        if not item:
            yield event.plain_result(f"未找到物品【{item_name}】，请检查名称或等待刷新。")
            return

        detail_text = self.shop_manager.get_item_details(item)
        acquire_hint = self._get_acquire_hint(item.get('type', ''), item)

        lines = [
            detail_text,
            f"获取途径：{acquire_hint}",
            "💡 使用 /丹阁 丹药|丹方|材料、/器阁、/百宝阁 查看当前售卖物品"
        ]
        yield event.plain_result("\n".join(lines))
