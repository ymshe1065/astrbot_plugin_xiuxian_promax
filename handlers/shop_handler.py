# handlers/shop_handler.py

import time
import re
from typing import Tuple
from astrbot.api.event import AstrMessageEvent
from astrbot.api import AstrBotConfig, logger
from ..data import DataBase
from ..core import ShopManager, EquipmentManager, PillManager, StorageRingManager
from ..models import Player
from ..config_manager import ConfigManager
from .utils import player_required

__all__ = ["ShopHandler"]

class ShopHandler:
    """商店处理器"""
    
    ITEM_ACQUIRE_HINTS = {
        'pill': "丹阁刷新、秘境稀有掉落",
        'exp_pill': "丹阁、炼丹系统、历练/秘境奖励",
        'utility_pill': "丹阁稀有、秘境/Boss 掉落",
        'legacy_pill': "百宝阁限量，购买后立即生效",
        'weapon': "器阁、Boss 掉落",
        'armor': "器阁、Boss 掉落",
        'accessory': "器阁、Boss 掉落",
        'main_technique': "百宝阁稀有刷新",
        'technique': "百宝阁、Boss 掉落",
        'material': "历练、秘境、悬赏、灵田收获与百宝阁限量",
    }

    def __init__(self, db: DataBase, config: AstrBotConfig, config_manager: ConfigManager):
        self.db = db
        self.config = config
        self.config_manager = config_manager
        self.shop_manager = ShopManager(config, config_manager)
        self.storage_ring_manager = StorageRingManager(db, config_manager)
        self.equipment_manager = EquipmentManager(db, config_manager, self.storage_ring_manager)
        self.pill_manager = PillManager(db, config_manager)
        access_control = self.config.get("ACCESS_CONTROL", {})
        self.shop_manager_ids = {
            str(user_id)
            for user_id in access_control.get("SHOP_MANAGERS", [])
        }

    async def _ensure_pavilion_refreshed(self, pavilion_id: str, item_getter, count: int) -> None:
        """确保阁楼已刷新"""
        last_refresh_time, current_items = await self.db.get_shop_data(pavilion_id)
        if current_items:
            updated = self.shop_manager.ensure_items_have_stock(current_items)
            if updated:
                await self.db.update_shop_data(pavilion_id, last_refresh_time, current_items)
        refresh_hours = self.config.get("PAVILION_REFRESH_HOURS", 1)
        if not current_items or self.shop_manager.should_refresh_shop(last_refresh_time, refresh_hours):
            new_items = self.shop_manager.generate_pavilion_items(item_getter, count)
            await self.db.update_shop_data(pavilion_id, int(time.time()), new_items)

    async def auto_restock(self) -> Tuple[int, str]:
        """v6.17 自动上货（定时任务调用）：
        1) 到刷新周期照常换货；2) 未到周期但货架售罄率≥80% 时提前补货（不变更刷新周期锚点）
        返回 (补货阁数, 描述)"""
        import time as _time
        restocked = 0
        details = []
        jobs = [
            ("pill_pavilion", "丹阁", self.shop_manager.get_pills_for_display, self.config.get("PAVILION_PILL_COUNT", 20)),
            ("weapon_pavilion", "器阁", self.shop_manager.get_weapons_for_display, self.config.get("PAVILION_WEAPON_COUNT", 20)),
            ("treasure_pavilion", "百宝阁", self.shop_manager.get_all_items_for_display, self.config.get("PAVILION_TREASURE_COUNT", 25)),
        ]
        for pavilion_id, name, getter, cnt in jobs:
            try:
                last_refresh, items = await self.db.get_shop_data(pavilion_id)
                refresh_hours = self.config.get("PAVILION_REFRESH_HOURS", 1)
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

    async def handle_pill_pavilion(self, event: AstrMessageEvent):
        """处理丹阁命令 - 展示丹药列表"""
        count = self.config.get("PAVILION_PILL_COUNT", 20)
        await self._ensure_pavilion_refreshed("pill_pavilion", self.shop_manager.get_pills_for_display, count)
        last_refresh, items = await self.db.get_shop_data("pill_pavilion")
        if not items:
            yield event.plain_result("丹阁暂无丹药出售。")
            return
        refresh_hours = self.config.get("PAVILION_REFRESH_HOURS", 1)
        display = self.shop_manager.format_pavilion_display("丹阁", items, refresh_hours, last_refresh)
        # 卡片成功只发图，失败/关闭降级为文本
        card = await self._pavilion_card("丹阁", items)
        if card:
            yield event.image_result(card)
        else:
            yield event.plain_result(display)

    async def handle_weapon_pavilion(self, event: AstrMessageEvent):
        """处理器阁命令 - 展示武器列表"""
        count = self.config.get("PAVILION_WEAPON_COUNT", 20)
        await self._ensure_pavilion_refreshed("weapon_pavilion", self.shop_manager.get_weapons_for_display, count)
        last_refresh, items = await self.db.get_shop_data("weapon_pavilion")
        if not items:
            yield event.plain_result("器阁暂无武器出售。")
            return
        refresh_hours = self.config.get("PAVILION_REFRESH_HOURS", 1)
        display = self.shop_manager.format_pavilion_display("器阁", items, refresh_hours, last_refresh)
        card = await self._pavilion_card("器阁", items)
        if card:
            yield event.image_result(card)
        else:
            yield event.plain_result(display)

    async def handle_treasure_pavilion(self, event: AstrMessageEvent):
        """处理百宝阁命令 - 展示所有物品"""
        count = self.config.get("PAVILION_TREASURE_COUNT", 25)
        await self._ensure_pavilion_refreshed("treasure_pavilion", self.shop_manager.get_all_items_for_display, count)
        last_refresh, items = await self.db.get_shop_data("treasure_pavilion")
        if not items:
            yield event.plain_result("百宝阁暂无物品出售。")
            return
        refresh_hours = self.config.get("PAVILION_REFRESH_HOURS", 1)
        display = self.shop_manager.format_pavilion_display("百宝阁", items, refresh_hours, last_refresh)
        card = await self._pavilion_card("百宝阁", items)
        if card:
            yield event.image_result(card)
        else:
            yield event.plain_result(display)

    async def _pavilion_card(self, pavilion_name: str, items: list):
        """阁楼列表的磨砂玻璃卡片（关闭或失败时不产出任何消息）"""
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            if not card_enabled("CARD_SHOP"):
                return None
            lines = []
            for item in items[:14]:
                stock = item.get('stock', 0)
                if stock <= 0:
                    continue
                discount = item.get('discount', 1.0)
                price_tag = f"{item['price']:,}灵石"
                if discount < 1.0:
                    price_tag += f"({round(discount * 10, 1)}折)"
                element = item.get('element', '')
                elem_tag = f"[{element}]" if element else ""
                lines.append(f"{item['name']}{elem_tag} - {price_tag} · 库存{stock}")
            if not lines:
                return None
            card_path = await render_card_async(
                "shop",
                f"{pavilion_name} · 今日货架",
                f"共 {len(lines)} 种在售",
                sections=[{"name": "在售清单", "lines": lines}],
                footer="购买 <物品名> [数量] · 售罄商品不显示",
            )
            if card_path:
                return card_path
            return None
        except Exception:
            return None

    async def _find_item_in_pavilions(self, item_name: str):
        """在所有阁楼中查找物品"""
        for pavilion_id in ["pill_pavilion", "weapon_pavilion", "treasure_pavilion"]:
            _, items = await self.db.get_shop_data(pavilion_id)
            if items:
                for item in items:
                    if item['name'] == item_name and item.get('stock', 0) > 0:
                        return pavilion_id, item
        return None, None

    @player_required
    async def handle_buy(self, player: Player, event: AstrMessageEvent, item_name: str = ""):
        """处理购买物品命令"""
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
                return part, max(1, int(qty_str))
            return text.strip(), 1

        item_part, quantity = parse_qty(normalized)

        if quantity == 1:
            try:
                raw_msg = event.get_message_str().strip()
                raw_msg = raw_msg.lstrip('/').strip()  # 兼容 /购买 斜杠前缀，防止物品名带上命令前缀
                if raw_msg.startswith("购买"):
                    raw_msg = raw_msg[len("购买"):].strip()
                raw_msg = raw_msg.replace("　", " ")
                raw_msg = raw_msg.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
                parsed_name, parsed_qty = parse_qty(raw_msg)
                # 仅当重新解析得到有效物品名时才采用（防御性，避免前缀残留污染名称）
                if parsed_name and not parsed_name.startswith("/"):
                    item_part, quantity = parsed_name, parsed_qty
            except Exception:
                pass

        item_name = item_part

        pavilion_id, target_item = await self._find_item_in_pavilions(item_name)
        if not target_item:
            yield event.plain_result(
                f"没有找到【{item_name}】。\n"
                "💡 物品名需与货架完全一致，且该物品当前在售（每 1 小时刷新）：\n"
                "  · 丹药 → /丹阁    武器 → /器阁    材料/符箓等 → /百宝阁\n"
                "💡 也可先发 /物品信息 <名称> 查询物品详情与获取途径"
            )
            return

        stock = target_item.get('stock', 0)
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

        item_type = target_item['type']
        result_lines = []

        # ===== 品阶→境界门槛（与事务内判断共用一张表）=====
        RANK_REQ_LEVEL = {
            "玄阶": 3, "地阶": 9, "天阶": 18,
            "灵品": 3, "地品": 9, "天品": 18, "皇品": 24,
            "帝品": 30, "道品": 36, "仙品": 42, "混元先天": 48,
        }

        def _norm_type(t: str) -> str:
            if t in ['weapon', '武器', '法器']:
                return 'weapon'
            if t in ['armor', '防具']:
                return 'armor'
            if t in ['main_technique', 'technique', '心法', '功法']:
                return 'technique'
            if t in ['pill', 'exp_pill', 'utility_pill', '丹药']:
                return 'legacy_pill'
            if t in ['material', '材料', '符箓', '杂物']:
                return 'material'
            return t

        norm_type = _norm_type(item_type)

        # ===== 门槛前置检查（事务前，无任何扣除，附当前可买清单）=====
        if norm_type in ['weapon', 'armor', 'technique']:
            rank = target_item.get('rank', '黄阶')
            req_level = RANK_REQ_LEVEL.get(rank, 0)
            if player.level_index < req_level:
                # 收集同一阁楼中玩家当前买得起的同类物品
                _, shelf_items = await self.db.get_shop_data(pavilion_id)
                affordable = []
                for it in (shelf_items or []):
                    if it.get('stock', 0) <= 0:
                        continue
                    if _norm_type(it.get('type', '')) != norm_type:
                        continue
                    it_req = RANK_REQ_LEVEL.get(it.get('rank', ''), 0)
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

            # ===== 核心修复1：极其健壮的类型兼容映射 =====
            if item_type in ['weapon', '武器', '法器']:
                item_type = 'weapon'
            elif item_type in ['armor', '防具']:
                item_type = 'armor'
            elif item_type in ['main_technique', 'technique', '心法', '功法']:
                item_type = 'technique'
            elif item_type in ['pill', 'exp_pill', 'utility_pill']:
                # 破境丹/修为丹/功能丹：购买后放入丹药背包，由 /服用丹药 使用
                item_type = 'inventory_pill'
            elif item_type in ['丹药', 'legacy_pill']:
                # items.json 旧丹药：带 effect_type 的（如 temporary）走丹药背包，
                # 只有旧 effect 字典的才保持“购买后立即生效”
                if target_item.get('data', {}).get('effect_type'):
                    item_type = 'inventory_pill'
                else:
                    item_type = 'legacy_pill'
            elif item_type in ['material', '材料', '符箓', '杂物']:
                item_type = 'material'

            # ===== 门槛防线（前置检查已拦截，此处仅事务内兜底）=====
            if item_type in ['weapon', 'armor', 'technique']:
                rank = target_item.get('rank', '黄阶')
                req_level = RANK_REQ_LEVEL.get(rank, 0)

                if player.level_index < req_level:
                    await self.db.conn.rollback()
                    yield event.plain_result(f"❌ 你的境界不足以驾驭此等神物！购买【{rank}】物品需要达到境界等级 {req_level}。")
                    return
            # ==========================================

            if item_type in ['weapon', 'armor', 'technique']:
                success, msg = await self.storage_ring_manager.store_item(player, target_item['name'], quantity, external_transaction=True)
                if success:
                    type_name = {"weapon": "武器", "armor": "防具", "technique": "功法"}.get(item_type, "装备")
                    result_lines.append(f"成功购买{type_name}【{target_item['name']}】x{quantity}，已存入储物戒。")
                else:
                    result_lines.append(f"成功购买【{target_item['name']}】x{quantity}。")
                    result_lines.append(f"⚠️ 存入储物戒失败：{msg}")
            
            elif item_type == 'inventory_pill':
                inventory = player.get_pills_inventory()
                inventory[target_item['name']] = inventory.get(target_item['name'], 0) + quantity
                player.set_pills_inventory(inventory)
                result_lines.append(
                    f"成功购买丹药【{target_item['name']}】x{quantity}，已放入丹药背包。"
                )
                result_lines.append(f"💡 使用 /服用丹药 {target_item['name']} [数量]")

            elif item_type == 'legacy_pill':
                success, message = await self._apply_legacy_pill_effects(player, target_item, quantity)
                if not success:
                    await self.db.conn.rollback()
                    yield event.plain_result(message)
                    return
                result_lines.append(message)
            
            elif item_type == 'material':
                success, msg = await self.storage_ring_manager.store_item(player, target_item['name'], quantity, external_transaction=True)
                if success:
                    result_lines.append(f"成功购买材料【{target_item['name']}】x{quantity}，已存入储物戒。")
                else:
                    result_lines.append(f"成功购买材料【{target_item['name']}】x{quantity}。")
                    result_lines.append(f"⚠️ 存入储物戒失败：{msg}")
            else:
                await self.db.conn.rollback()
                yield event.plain_result(f"未知的物品类型：{item_type}")
                return

            player.gold -= total_price
            await self.db.update_player(player, commit=False)
            await self.db.conn.commit()
            
            result_lines.append(f"花费灵石: {total_price}，剩余: {player.gold}")
            result_lines.append(f"剩余库存: {remaining}" if remaining > 0 else "该物品已售罄！")
            yield event.plain_result("\n".join(result_lines))
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"购买异常: {e}")
            raise

    def _get_acquire_hint(self, item_type: str) -> str:
        """根据类型返回获取提示"""
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
        acquire_hint = self._get_acquire_hint(item.get('type', ''))

        lines = [
            detail_text,
            f"获取途径：{acquire_hint}",
            "💡 使用 /丹阁、/器阁、/百宝阁 查看当前售卖物品"
        ]
        yield event.plain_result("\n".join(lines))

    async def _apply_legacy_pill_effects(self, player: Player, item: dict, quantity: int) -> tuple:
        """应用旧系统丹药效果（items.json中的丹药）"""
        effects = item.get('data', {}).get('effect', {})
        if not effects:
            return False, f"丹药【{item['name']}】无效果配置。"

        effect_msgs = []
        pill_name = item['name']

        for _ in range(quantity):
            if 'add_hp' in effects:
                hp_change = effects['add_hp']
                if player.cultivation_type == "体修":
                    old_blood = player.blood_qi
                    player.blood_qi = max(0, min(player.max_blood_qi, player.blood_qi + hp_change))
                    effect_msgs.append(f"气血+{player.blood_qi - old_blood}" if hp_change > 0 else f"气血{hp_change}")
                else:
                    old_qi = player.spiritual_qi
                    player.spiritual_qi = max(0, min(player.max_spiritual_qi, player.spiritual_qi + hp_change))
                    effect_msgs.append(f"灵气+{player.spiritual_qi - old_qi}" if hp_change > 0 else f"灵气{hp_change}")
            if 'add_experience' in effects:
                exp_gain = effects['add_experience']
                player.experience += exp_gain
                effect_msgs.append(f"修为+{exp_gain}")
            if 'add_max_hp' in effects:
                max_hp_gain = effects['add_max_hp']
                if player.cultivation_type == "体修":
                    player.max_blood_qi += max_hp_gain
                    effect_msgs.append(f"最大气血+{max_hp_gain}")
                else:
                    player.max_spiritual_qi += max_hp_gain
                    effect_msgs.append(f"最大灵气+{max_hp_gain}")
            if 'add_spiritual_power' in effects:
                sp_gain = effects['add_spiritual_power']
                player.magic_damage += sp_gain
                effect_msgs.append(f"法伤+{sp_gain}")
            if 'add_mental_power' in effects:
                mp_gain = effects['add_mental_power']
                player.mental_power += mp_gain
                effect_msgs.append(f"精神力+{mp_gain}")
            if 'add_attack' in effects:
                atk_gain = effects['add_attack']
                player.physical_damage += atk_gain
                effect_msgs.append(f"物伤+{atk_gain}" if atk_gain > 0 else f"物伤{atk_gain}")
            if 'add_defense' in effects:
                def_gain = effects['add_defense']
                player.physical_defense += def_gain
                effect_msgs.append(f"物防+{def_gain}" if def_gain > 0 else f"物防{def_gain}")
            if 'add_gold' in effects:
                gold_change = effects['add_gold']
                player.gold += gold_change
                effect_msgs.append(f"灵石+{gold_change}" if gold_change > 0 else f"灵石{gold_change}")

        player.physical_damage = max(0, player.physical_damage)
        player.magic_damage = max(0, player.magic_damage)
        player.physical_defense = max(0, player.physical_defense)
        player.magic_defense = max(0, player.magic_defense)
        player.mental_power = max(0, player.mental_power)
        player.spiritual_qi = min(player.spiritual_qi, player.max_spiritual_qi)
        player.blood_qi = min(player.blood_qi, player.max_blood_qi)

        await self.db.update_player(player, commit=False)

        unique_effects = list(dict.fromkeys(effect_msgs))
        effects_str = "、".join(unique_effects[:5])
        if len(unique_effects) > 5:
            effects_str += "..."

        qty_str = f"x{quantity}" if quantity > 1 else ""
        return True, f"服用【{pill_name}】{qty_str}成功！效果：{effects_str}"