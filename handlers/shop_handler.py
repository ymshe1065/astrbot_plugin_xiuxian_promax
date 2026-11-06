# handlers/shop_handler.py

import json
import time
import re
import random
from typing import Tuple
from astrbot.api.event import AstrMessageEvent
from astrbot.api import AstrBotConfig, logger
from ..data import DataBase
from ..data.data_manager import _find_shop_entry_index
from ..core import ShopManager, EquipmentManager, PillManager, StorageRingManager, ItemRules
from ..core.item_display import strip_item_tag
from ..models import Player
from ..config_manager import ConfigManager
from ..rank_rules import normalize_rank
from .utils import player_required, set_event_flag

__all__ = ["ShopHandler", "BUY_SEARCH_PAVILION_IDS"]

# /购买 允许搜索的货架（突破丹方必须在内，否则《温神丹·中方》这类卷轴买不到）
BUY_SEARCH_PAVILION_IDS = [
    "pill_pavilion", "recipe_pavilion", "breakthrough_recipe_pavilion",
    "material_pavilion", "weapon_pavilion",
    "treasure_pavilion", "talisman_pavilion", "talisman_recipe_pavilion",
]


class ShopHandler:
    """商店处理器"""
    
    ITEM_ACQUIRE_HINTS = {
        'pill': "丹阁刷新、秘境稀有掉落",
        'exp_pill': "丹阁、炼丹系统、历练/秘境修为奖励",
        'utility_pill': "丹阁、秘境掉落、炼丹产出",
        'inventory_pill': "丹阁刷新、炼丹产出",
        'legacy_pill': "丹阁刷新，购买后进入丹药背包",
        'recipe': "丹方阁购买后学习",
        'talisman_recipe': "符阁·符图 购买后习得绘制方法",
        'material': "材料阁、灵田、历练、秘境、悬赏",
        'material_package': "材料阁购买后开启",
        '符箓': "符阁购买成品符、符箓师绘制、历练/奇遇掉落",
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
        """丹方阁每次随机刷新的丹方数量（突破丹方与初始已学丹方不占此名额）。"""
        raw = self._cfg("PAVILION_RARE_RECIPE_COUNT", self._cfg("PAVILION_RECIPE_COUNT", 10))
        try:
            count = int(raw)
        except (TypeError, ValueError):
            count = 10
        # 每次固定 10 个，服务器配置写小值也强制保底 10
        return max(10, count)

    async def _ensure_pavilion_refreshed(self, pavilion_id: str, item_getter, count: int,
                                         apply_rank_band: bool = False, pick_mode: str = "",
                                         stock_cap: int = 0) -> None:
        """确保阁楼已刷新"""
        # 货架「读取→数据修复→回写」放进同一 BEGIN IMMEDIATE 事务：
        # 回写前基于事务内重读的最新货架，避免旧快照 INSERT OR REPLACE
        # 把并发购买的原子扣库存覆盖回滚
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            last_refresh_time, current_items = await self.db.get_shop_data(pavilion_id)
            if current_items:
                updated = self.shop_manager.dedupe_same_name_items(current_items)
                updated = self.shop_manager.ensure_items_have_stock(current_items) or updated
                updated = self.shop_manager.ensure_equipment_quality(current_items) or updated
                updated = self.shop_manager.refresh_item_data(current_items) or updated
                # 配置里新增的常驻商品（如护劫类符图）不等整轮刷新，读取时直接补上
                updated = self.shop_manager.sync_permanent_entries(current_items, item_getter, count) or updated
                kept, filtered = self.shop_manager.filter_stale_shelf_items(
                    current_items, exclude_breakthrough=(pavilion_id == "recipe_pavilion")
                )
                if filtered:
                    current_items = kept
                    updated = True
                if updated:
                    await self.db.conn.execute(
                        "UPDATE shop SET current_items = ?, last_refresh_time = ? WHERE shop_id = ?",
                        (json.dumps(current_items, ensure_ascii=False), last_refresh_time, pavilion_id),
                    )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise
        # 丹方总数变化（新增/删除丹方）时强制重建丹方货架，避免旧货架残留
        if pavilion_id in ("recipe_pavilion", "breakthrough_recipe_pavilion"):
            try:
                expected = f"recipes-{len(self.config_manager.pill_recipes_data or {})}"
                stored = await self.db.ext.get_system_config("shop_recipe_fingerprint")
                if stored != expected:
                    last_refresh_time = 0
                    await self.db.ext.set_system_config("shop_recipe_fingerprint", expected)
            except Exception:
                pass
        # 符图货架指纹：配方数变化或常驻规则调整（v6.49.9 护劫符符图转常驻）时强制重建，
        # 让老服务器更新插件后立刻能看到常驻的混元符符图，不必等刷新周期
        if pavilion_id == "talisman_recipe_pavilion":
            try:
                expected = f"talisman-recipes-{len(self.config_manager.talisman_recipes_data or {})}-v2"
                stored = await self.db.ext.get_system_config("shop_talisman_recipe_fingerprint")
                if stored != expected:
                    last_refresh_time = 0
                    await self.db.ext.set_system_config("shop_talisman_recipe_fingerprint", expected)
            except Exception:
                pass
        refresh_hours = self._cfg("PAVILION_REFRESH_HOURS", 1)
        if not current_items or self.shop_manager.should_refresh_shop(last_refresh_time, refresh_hours):
            if apply_rank_band:
                try:
                    players = await self.db.get_all_players()
                    self.shop_manager.resolve_rank_band(players, force=True)
                except Exception:
                    pass
            new_items = self.shop_manager.generate_pavilion_items(
                item_getter, count, pick_mode=pick_mode, stock_cap=stock_cap
            )
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
            ("pill_pavilion", "丹阁", self.shop_manager.get_pills_for_display, self._cfg("PAVILION_PILL_COUNT", 20), "", 0),
            ("recipe_pavilion", "丹方阁", self.shop_manager.get_recipes_for_display, self._rare_recipe_count(), "rare", 0),
            ("breakthrough_recipe_pavilion", "突破丹方", self.shop_manager.get_breakthrough_recipes_for_display, 0, "", 0),
            ("material_pavilion", "材料阁", self.shop_manager.get_materials_for_display, self._cfg("PAVILION_MATERIAL_COUNT", 20), "", 0),
            ("weapon_pavilion", "器阁", self.shop_manager.get_weapons_for_display, self._cfg("PAVILION_WEAPON_COUNT", 40), "common", 0),
            ("treasure_pavilion", "百宝阁", self.shop_manager.get_treasure_items_for_display, self._cfg("PAVILION_TREASURE_COUNT", 12), "rare", 5),
            ("talisman_pavilion", "符阁", self.shop_manager.get_talismans_for_display, self._cfg("PAVILION_TALISMAN_COUNT", 12), "", 0),
            ("talisman_recipe_pavilion", "符阁·符图", self.shop_manager.get_talisman_recipes_for_display, self._cfg("PAVILION_TALISMAN_RECIPE_COUNT", 4), "", 0),
        ]
        for pavilion_id, name, getter, cnt, pick_mode, stock_cap in jobs:
            try:
                # 读取+修复回写放进 BEGIN IMMEDIATE 事务，写回基于事务内重读的最新货架，
                # 避免旧快照覆盖并发购买的原子扣库存
                await self.db.conn.execute("BEGIN IMMEDIATE")
                try:
                    last_refresh, items = await self.db.get_shop_data(pavilion_id)
                    if items:
                        data_changed = self.shop_manager.dedupe_same_name_items(items)
                        data_changed = self.shop_manager.ensure_equipment_quality(items) or data_changed
                        data_changed = self.shop_manager.refresh_item_data(items) or data_changed
                        data_changed = self.shop_manager.sync_permanent_entries(items, getter, cnt) or data_changed
                        kept, filtered = self.shop_manager.filter_stale_shelf_items(
                            items, exclude_breakthrough=(pavilion_id == "recipe_pavilion")
                        )
                        if filtered:
                            items[:] = kept
                            data_changed = True
                        if data_changed:
                            await self.db.conn.execute(
                                "UPDATE shop SET current_items = ?, last_refresh_time = ? WHERE shop_id = ?",
                                (json.dumps(items, ensure_ascii=False), last_refresh, pavilion_id),
                            )
                    await self.db.conn.commit()
                except Exception:
                    await self.db.conn.rollback()
                    raise
                refresh_hours = self._cfg("PAVILION_REFRESH_HOURS", 1)
                due = self.shop_manager.should_refresh_shop(last_refresh, refresh_hours)
                if not items:
                    # 空货架（从未初始化）：立即上货
                    new_items = self.shop_manager.generate_pavilion_items(getter, cnt, pick_mode=pick_mode, stock_cap=stock_cap)
                    await self.db.update_shop_data(pavilion_id, int(_time.time()), new_items)
                    restocked += 1
                    details.append(name)
                    continue
                if due:
                    new_items = self.shop_manager.generate_pavilion_items(getter, cnt, pick_mode=pick_mode, stock_cap=stock_cap)
                    await self.db.update_shop_data(pavilion_id, int(_time.time()), new_items)
                    restocked += 1
                    details.append(name)
                    continue
                # 售罄检测：有货商品占比 < 20% 则补货
                total = len(items)
                on_sale = sum(1 for it in items if it.get('stock', 0) > 0)
                if total > 0 and on_sale / total < 0.2:
                    new_items = self.shop_manager.generate_pavilion_items(getter, cnt, pick_mode=pick_mode, stock_cap=stock_cap)
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
            "丹药": ("pill_pavilion", "丹阁", self.shop_manager.get_pills_for_display, self._cfg("PAVILION_PILL_COUNT", 20), ""),
            "丹方": ("recipe_pavilion", "丹方阁", self.shop_manager.get_recipes_for_display, self._rare_recipe_count(), "rare"),
            "突破丹方": ("breakthrough_recipe_pavilion", "突破丹方", self.shop_manager.get_breakthrough_recipes_for_display, 0, ""),
            "材料": ("material_pavilion", "材料阁", self.shop_manager.get_materials_for_display, self._cfg("PAVILION_MATERIAL_COUNT", 20), ""),
        }
        if not section:
            yield event.plain_result(
                "🏯 丹阁分区\n"
                "━━━━━━━━━━━━━━━\n"
                "  · /丹阁 丹药：成品丹药\n"
                "  · /丹阁 丹方：随机刷新的丹方卷轴（每刷 10 个）\n"
                "  · /丹阁 突破丹方：全部突破丹方（常驻）\n"
                "  · /丹阁 材料：炼丹材料\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 购买使用 /购买 <物品名> [数量]"
            )
            return
        if section not in jobs:
            yield event.plain_result("❌ 丹阁分区只有：丹药、丹方、突破丹方、材料。")
            return
        pavilion_id, name, getter, count, pick_mode = jobs[section]
        await self._ensure_pavilion_refreshed(pavilion_id, getter, count, pick_mode=pick_mode)
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

    async def handle_talisman_pavilion(self, event: AstrMessageEvent, section: str = ""):
        """符阁：成品符与符图两区，不卖材料。"""
        section = (section or "").strip()
        jobs = {
            "符箓": ("talisman_pavilion", "符阁", self.shop_manager.get_talismans_for_display, self._cfg("PAVILION_TALISMAN_COUNT", 12)),
            "符图": ("talisman_recipe_pavilion", "符阁·符图", self.shop_manager.get_talisman_recipes_for_display, self._cfg("PAVILION_TALISMAN_RECIPE_COUNT", 4)),
        }
        section = {"配方": "符图"}.get(section, section)
        if not section:
            yield event.plain_result(
                "🏯 符阁分区\n"
                "━━━━━━━━━━━━━━━\n"
                "  · /符阁 符箓：凡品与黄品成品符\n"
                "  · /符阁 符图：玄品以上符箓的绘制图纸（护劫符符图常驻）\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 玄品以上成品符只能由符箓师绘制\n"
                "💡 购买使用 /购买 <物品名> [数量]"
            )
            return
        if section not in jobs:
            yield event.plain_result("❌ 符阁分区只有：符箓、符图。")
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
        # 符图与成品符同名，货架上必须写清购买写法，否则玩家会买错种类
        buy_hint = ""
        card_footer = ""
        if pavilion_id == "talisman_recipe_pavilion":
            card_footer = "买符图用 /购买 符名·符图 ｜ 只写符名 = 买成品符 ｜ [常驻] 不限量"
            buy_hint = (
                "\n━━━━━━━━━━━━━━━\n"
                "💡 符图与成品符同名：买符图请用 /购买 <符名>·符图（例：/购买 避劫符·符图）\n"
                "💡 只写符名 = 买成品符；标 [常驻] 的护劫符图不限量，随时可买"
            )
        cards = await self._pavilion_cards(name, items, footer=card_footer)
        if cards:
            for card in cards:
                yield event.image_result(card)
        else:
            yield event.plain_result(display + buy_hint)

    async def handle_weapon_pavilion(self, event: AstrMessageEvent):
        """处理器阁命令 - 展示武器列表"""
        count = self._cfg("PAVILION_WEAPON_COUNT", 40)
        await self._ensure_pavilion_refreshed(
            "weapon_pavilion", self.shop_manager.get_weapons_for_display, count,
            apply_rank_band=True, pick_mode="common",
        )
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
        count = self._cfg("PAVILION_TREASURE_COUNT", 12)
        await self._ensure_pavilion_refreshed(
            "treasure_pavilion", self.shop_manager.get_treasure_items_for_display, count,
            apply_rank_band=True, pick_mode="rare", stock_cap=5,
        )
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

    async def _pavilion_cards(self, pavilion_name: str, items: list, footer: str = ""):
        """每个阁楼只生成一张长图；百宝阁在同一张图内按品阶分区。"""
        available = [item for item in items if item.get("stock", 0) > 0]
        if not available:
            return []
        groups = None
        if pavilion_name == "百宝阁":
            groups = self.shop_manager.group_items_by_rank(available)
            ordered = [item for _, group in groups for item in group]
        else:
            ordered = available
        card = await self._pavilion_card(
            pavilion_name, ordered, groups=groups, footer=footer
        )
        return [card] if card else []

    def _pavilion_section_lines(self, items: list):
        """生成单个区块的商品行，图片与文本共用完整交易详情。"""
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
            # 五行 / 常驻等展示标记放在价格段，不能拼进物品名：
            # 卡片标题就是 /购买 要用的名字，一旦带上 [金]、[常驻] 就买不到。
            if item.get('always_available'):
                price_tag += " [常驻]"
            count += 1
            lines.append(f"{item['name']} - {price_tag} · 库存{stock}")
            lines.extend(self.shop_manager.get_trade_details(item))
            lines.append("")
        return count, lines

    async def _pavilion_card(self, pavilion_name: str, items: list,
                             title: str = "", groups=None, footer: str = ""):
        """阁楼列表的磨砂玻璃卡片（关闭或失败时不产出任何消息）。"""
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            if not card_enabled("CARD_SHOP"):
                return None
            sections = []
            count = 0
            if groups:
                for rank, group in groups:
                    group_count, lines = self._pavilion_section_lines(group)
                    if lines:
                        sections.append({"name": f"{rank}区", "lines": lines})
                        count += group_count
            else:
                count, lines = self._pavilion_section_lines(items)
                if lines:
                    sections.append({"name": "在售清单", "lines": lines})
            if not sections:
                return None
            card_path = await render_card_async(
                "shop",
                title or f"{pavilion_name} · 今日货架",
                f"共 {count} 种在售",
                sections=sections,
                footer=footer or "购买 <物品名> [数量] · 售罄商品不显示",
            )
            if card_path:
                return card_path
            return None
        except Exception:
            return None

    async def _sync_talisman_recipe_shelf(self) -> None:
        """只补常驻符图条目，不重建整个货架（避免买一次就换掉玩家看到的清单）。"""
        try:
            last_refresh, items = await self.db.get_shop_data("talisman_recipe_pavilion")
            if not isinstance(items, list) or not items:
                # 货架还没建过（新库、被清空、或玩家从没打开过 /符阁 符图）：
                # 此时若直接返回，常驻护劫符图就无处可买，而丹劫提示已经把玩家指向这里。
                # 先按正常规则刷一轮货架，再补常驻条目。
                await self._ensure_pavilion_refreshed(
                    "talisman_recipe_pavilion",
                    self.shop_manager.get_talisman_recipes_for_display,
                    self._cfg("PAVILION_TALISMAN_RECIPE_COUNT", 4),
                )
                last_refresh, items = await self.db.get_shop_data("talisman_recipe_pavilion")
            if not items:
                return
            changed = self.shop_manager.refresh_item_data(items)
            changed = self.shop_manager.sync_permanent_entries(
                items,
                self.shop_manager.get_talisman_recipes_for_display,
                self._cfg("PAVILION_TALISMAN_RECIPE_COUNT", 4),
            ) or changed
            if changed:
                await self.db.update_shop_data("talisman_recipe_pavilion", last_refresh, items)
        except Exception:
            return

    async def _find_item_in_pavilions(self, item_name: str, preferred_pavilion: str = "", want_type: str = ""):
        """在所有阁楼中查找物品；同名符图与成品符按类型优先级区分。

        优先返回仍有库存的条目；若全部售罄，则返回优先级最高的匹配条目，供购买流程
        提示“已售罄”。未指定类型时成品符优先（符图必须显式写「符图」）。
        """
        query_base, explicit_recipe = self.shop_manager.split_talisman_query(item_name)
        target_type = want_type or ("talisman_recipe" if explicit_recipe else "")
        # 明确买符图时先补齐符图阁的常驻条目（护劫符图必须随时买得到）
        if target_type == "talisman_recipe":
            await self._sync_talisman_recipe_shelf()
        pavilion_ids = list(BUY_SEARCH_PAVILION_IDS)
        if preferred_pavilion in pavilion_ids:
            pavilion_ids.remove(preferred_pavilion)
            pavilion_ids.insert(0, preferred_pavilion)

        def _priority(item: dict) -> int:
            norm_type = ItemRules.normalize_shop_type(item)
            if target_type:
                return 0 if norm_type == target_type else 2
            # 裸名：成品符优先，符图退后，避免“买了符图却没买到符”
            return 1 if norm_type == "talisman_recipe" else 0

        in_stock: list = []
        sold_out: list = []
        for pavilion_id in pavilion_ids:
            last_refresh, items = await self.db.get_shop_data(pavilion_id)
            if not items:
                continue
            changed = self.shop_manager.dedupe_same_name_items(items)
            changed = self.shop_manager.ensure_equipment_quality(items) or changed
            changed = self.shop_manager.refresh_item_data(items) or changed
            if changed:
                await self.db.update_shop_data(pavilion_id, last_refresh, items)
            for item in items:
                if self.shop_manager.entry_base_name(item) != query_base:
                    continue
                if target_type and ItemRules.normalize_shop_type(item) != target_type:
                    continue
                if item.get('stock', 0) > 0:
                    in_stock.append((_priority(item), pavilion_id, item))
                else:
                    sold_out.append((_priority(item), pavilion_id, item))

        for bucket in (in_stock, sold_out):
            if bucket:
                bucket.sort(key=lambda entry: entry[0])
                return bucket[0][1], bucket[0][2]
        return None, None

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
        # 符图/成品符同名：裸名指成品符，显式写「符图」才买符图
        query_base, explicit_recipe = self.shop_manager.split_talisman_query(item_name)

        pavilion_id, target_item = await self._find_item_in_pavilions(
            item_name,
            self._viewed_pavilion(event),
            want_type="talisman_recipe" if explicit_recipe else "",
        )
        if not target_item:
            known = self.shop_manager.find_item_by_name(query_base)
            if known:
                acquire_hint = self._get_acquire_hint(known.get('type', ''), known)
                yield event.plain_result(
                    f"【{query_base}】当前未在货架上（货架每 1 小时刷新）。\n"
                    f"💡 获取途径：{acquire_hint}\n"
                    "💡 材料/材料包可查看 /丹阁 材料，其他物品可查看 /器阁、/百宝阁\n"
                    "💡 也可先发 /物品信息 <名称> 查询物品详情"
                )
                return
            yield event.plain_result(
                f"没有找到【{query_base}】。\n"
                "💡 物品名需与货架完全一致，且该物品当前在售（每 1 小时刷新）：\n"
                "  · 丹药 → /丹阁 丹药    丹方 → /丹阁 丹方    材料/材料包 → /丹阁 材料\n"
                "  · 武器/装备 → /器阁    符箓 → /符阁    杂物 → /百宝阁\n"
                "💡 也可先发 /物品信息 <名称> 查询物品详情与获取途径"
            )
            return

        stock = target_item.get('stock', 0)
        unlimited = bool(target_item.get('unlimited'))
        if not unlimited and stock <= 0:
            yield event.plain_result(f"【{target_item['name']}】已售罄，请等待货架刷新。")
            return
        limited_note = ""
        norm_type = ItemRules.normalize_shop_type(target_item)
        if self.shop_manager.is_gear_entry(target_item) and quantity > 1:
            quantity = 1
            limited_note = "⚠️ 装备每次限购 1 件，已按 1 件结算。\n"
        # 符图是「一次性习得」，不可能批量买
        if norm_type == 'talisman_recipe' and quantity > 1:
            quantity = 1
            limited_note = "⚠️ 符图每次限购 1 份，已按 1 份结算。\n"
        if not unlimited and quantity > stock:
            yield event.plain_result(f"【{target_item['name']}】库存不足，当前库存: {stock}。")
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

            # 事务内重读货架条目：下单后货架可能已被刷新/调价，
            # 价格与下单价不一致时中止购买，避免按过期价格结算
            _, shelf_items = await self.db.get_shop_data(pavilion_id)
            entry_idx = _find_shop_entry_index(
                shelf_items, target_item['name'],
                self.shop_manager.shop_entry_key(target_item), quantity,
            )
            if entry_idx >= 0:
                try:
                    fresh_price = int(shelf_items[entry_idx].get('price', price) or 0)
                except (TypeError, ValueError):
                    fresh_price = int(price)
                if fresh_price != int(price):
                    await self.db.conn.rollback()
                    yield event.plain_result(
                        f"⚠️ 货架已刷新：【{target_item['name']}】价格已由 {price:,} 变为 {fresh_price:,} 灵石，请重新购买。"
                    )
                    return
            elif not unlimited:
                await self.db.conn.rollback()
                yield event.plain_result(f"❌ 货架已刷新，【{target_item['name']}】已下架，请重新购买。")
                return

            if player.gold < total_price:
                await self.db.conn.rollback()
                yield event.plain_result(
                    f"灵石不足！\n【{target_item['name']}】价格: {price} 灵石\n"
                    f"购买数量: {quantity}\n需要灵石: {total_price}\n你的灵石: {player.gold}"
                )
                return

            if unlimited:
                reserved = True
                remaining = None
            else:
                reserved, _, remaining = await self.db.decrement_shop_item_stock(
                    pavilion_id, target_item['name'], quantity,
                    external_transaction=True,
                    entry_key=self.shop_manager.shop_entry_key(target_item),
                )
            if not reserved:
                await self.db.conn.rollback()
                yield event.plain_result(f"【{target_item['name']}】已售罄，请等待刷新。")
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

            elif item_type == 'talisman_recipe':
                from ..managers.talisman_manager import TalismanManager
                talisman_mgr = TalismanManager(self.db, self.config_manager, self.storage_ring_manager)
                recipe_data = target_item.get('data') or {}
                recipe_base = recipe_data.get('base_name') or recipe_data.get('name') or query_base
                ok, msg = await talisman_mgr.learn_recipe(player, recipe_base, commit=False)
                if not ok:
                    await self.db.conn.rollback()
                    yield event.plain_result(
                        f"{msg}\n本次未扣除灵石。\n"
                        f"💡 已会绘制时购买的是成品符：/购买 {recipe_base}"
                    )
                    return
                result_lines.append(f"成功购入符图【{recipe_base}】，已参悟绘制方法。")
                result_lines.append(f"💡 使用 /制符 {recipe_base} 绘制，/符箓书 查看材料与可画张数")

            elif item_type == 'material':
                success, msg = await self.storage_ring_manager.store_item(player, target_item['name'], quantity, external_transaction=True)
                if not success:
                    await self.db.conn.rollback()
                    yield event.plain_result(f"❌ 物品存入储物戒失败：{msg}\n本次未扣除灵石。")
                    return
                raw_type = (target_item.get('data') or {}).get('type')
                type_name = "符箓" if raw_type == "符箓" else "材料"
                result_lines.append(f"成功购买{type_name}【{target_item['name']}】x{quantity}。\n{msg}")
                if raw_type == "符箓" and not explicit_recipe:
                    base = self.shop_manager.entry_base_name(target_item)
                    if self.shop_manager.has_talisman_recipe(base):
                        result_lines.append(
                            f"💡 同名符图可买：/购买 {base}·符图（习得后 /制符 {base} 自己画）"
                        )
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
            if unlimited:
                result_lines.append("该物品常驻出售，不限量。")
            else:
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

        query = strip_item_tag(item_name)
        query_base, explicit_recipe = self.shop_manager.split_talisman_query(query)
        if explicit_recipe:
            recipe = (getattr(self.config_manager, "talisman_recipes_data", {}) or {}).get(query_base)
            if isinstance(recipe, dict):
                rank = normalize_rank(recipe.get("rank"), "凡品")
                level_required = int(recipe.get("level_required", 0) or 0)
                gold_cost = int(recipe.get("gold_cost", 0) or 0)
                rate = float(recipe.get("success_rate", 75) or 75)
                materials = recipe.get("materials") or []
                mat_text = "、".join(
                    f"{spec.get('name') or (spec.get('tier', '') + spec.get('category', ''))}×{spec.get('count', 0)}"
                    for spec in materials if isinstance(spec, dict)
                ) or "无需材料"
                yield event.plain_result(
                    f"📜 【{query_base}·符图】（{rank}）\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"记载【{query_base}】绘制方法的一次性符图，购买后永久掌握。\n"
                    f"绘制要求：境界等级 {level_required}｜基础工费 {gold_cost:,} 灵石｜基础成功率 {rate:.0f}%\n"
                    f"配方材料：{mat_text}\n"
                    f"━━━━━━━━━━━━━━━\n"
                    "💡 /符阁 符图 查看在售符图，/购买 <符名>·符图 购买\n"
                    "💡 /符箓书 查看已掌握符图的材料与可画张数"
                )
                return

        item = self.shop_manager.find_item_by_name(query_base)
        if not item:
            yield event.plain_result(f"未找到物品【{query_base}】，请检查名称或等待刷新。")
            return

        detail_text = self.shop_manager.get_item_details(item)
        acquire_hint = self._get_acquire_hint(item.get('type', ''), item)

        lines = [
            detail_text,
            f"获取途径：{acquire_hint}",
            "💡 使用 /丹阁 丹药|丹方|材料、/器阁、/百宝阁 查看当前售卖物品"
        ]
        yield event.plain_result("\n".join(lines))
