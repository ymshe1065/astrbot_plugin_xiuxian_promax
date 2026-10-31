# handlers/storage_ring_handler.py

from typing import Tuple

from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger
from astrbot.api.all import At, Plain
from ..data import DataBase
from ..core import StorageRingManager, ItemRules, PillManager
from ..core.realm_rules import format_applicable_realm
from ..core.item_instance_manager import ItemInstanceManager
from ..config_manager import ConfigManager
from ..models import Player
from .utils import player_required

CMD_STORAGE_RING = "储物戒"
CMD_STORE_ITEM = "存入"
CMD_RETRIEVE_ITEM = "取出"
CMD_UPGRADE_RING = "更换储物戒"
CMD_DISCARD_ITEM = "丢弃"
CMD_GIFT_ITEM = "赠予"
CMD_ACCEPT_GIFT = "接收"
CMD_REJECT_GIFT = "拒绝"
CMD_STORE_ALL = "存入所有"
CMD_RETRIEVE_ALL = "取出所有"
CMD_SEARCH_ITEM = "搜索物品"
CMD_VIEW_CATEGORY = "查看分类"

# 物品分类定义
ITEM_CATEGORIES = {
    "材料": ["灵草", "精铁", "玄铁", "星辰石", "灵石碎片", "灵兽毛皮", "灵兽内丹", 
             "妖兽精血", "功法残页", "秘境精华", "天材地宝", "混沌精华", "神兽之骨", 
             "远古秘籍", "仙器碎片"],
    "装备": ["武器", "防具", "法器"],
    "功法": ["心法", "技能"],
    "其他": []
}

__all__ = ["StorageRingHandler"]


class StorageRingHandler:
    """储物戒系统处理器"""

    def __init__(self, db: DataBase, config_manager: ConfigManager):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_manager = StorageRingManager(db, config_manager)

    @player_required
    async def handle_storage_ring(self, player: Player, event: AstrMessageEvent):
        """显示储物戒信息"""
        display_name = event.get_sender_name()

        # 获取储物戒信息
        ring_info = self.storage_ring_manager.get_storage_ring_info(player)

        lines = [
            f"=== {display_name} 的储物戒 ===\n",
            f"【{ring_info['name']}】（{ring_info['rank']}）\n",
            f"{ring_info['description']}\n",
            f"\n容量：{ring_info['used']}/{ring_info['capacity']}格\n",
            f"━━━━━━━━━━━━━━━\n",
        ]

        # 按分类显示存储的物品
        items = ring_info['items']
        if items:
            categorized = self._categorize_items(items)
            for category, cat_items in categorized.items():
                if cat_items:
                    lines.append(f"【{category}】\n")
                    for item_name, count in cat_items:
                        if count > 1:
                            lines.append(f"  · {item_name}×{count}\n")
                        else:
                            lines.append(f"  · {item_name}\n")
        else:
            lines.append("【存储物品】空\n")

        overflow = ring_info.get("overflow", {})
        if overflow:
            overflow_total = sum(int(v or 0) for v in overflow.values())
            lines.append(f"\n【溢出仓库】{len(overflow)} 种 / {overflow_total} 件\n")
            for name, count in list(overflow.items())[:10]:
                lines.append(f"  · {name}×{count}\n")
            if len(overflow) > 10:
                lines.append("  · ...\n")
            lines.append(f"领取：/领取溢出\n")

        # 空间警告
        warning = self.storage_ring_manager.get_space_warning(player)
        if warning:
            lines.append(f"\n{warning}\n")

        lines.append(f"\n{'=' * 28}\n")
        lines.append(f"丢弃：{CMD_DISCARD_ITEM} 物品名 [数量]\n")
        lines.append(f"搜索：{CMD_SEARCH_ITEM} 关键词\n")
        lines.append(f"升级：{CMD_UPGRADE_RING} 储物戒名")

        text_msg = "".join(lines)

        # ===== 图片卡片：成功只发图，失败/关闭降级为纯文本 =====
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            card_path = None
            if card_enabled("CARD_BAG"):
                enchant = ring_info.get("enchant", 0)
                rows = []
                # 名称完整显示，不做省略；物品较多时由渲染器自动加长图片
                for item_name, count in items.items():
                    rows.append((item_name, f"x{count}"))
                if not rows:
                    rows.append(("空空如也", "—"))
                card_path = await render_card_async(
                    "bag",
                    f"{display_name} 的储物戒",
                    f"【{ring_info['name']}】({ring_info['rank']})",
                    sections=[
                        {"name": "容量", "rows": [
                            ("容量", f"{ring_info['used']}/{ring_info['capacity']}"),
                            ("空间附魔", f"+{enchant}" if enchant else "无"),
                            ("剩余空间", f"{ring_info['available']}"),
                        ]},
                        {"name": "物品清单", "rows": rows},
                    ],
                    footer=f"共 {len(items)} 种物品 · /装备 物品名 · /丢弃 物品名 [数量]",
                )
            if card_path:
                yield event.image_result(card_path)
            else:
                yield event.plain_result(text_msg)
        except Exception:
            yield event.plain_result(text_msg)

    @player_required
    async def handle_claim_overflow(self, player: Player, event: AstrMessageEvent):
        count, msg = await self.storage_ring_manager.claim_overflow(player)
        yield event.plain_result(f"📦 {msg}")

    @player_required
    async def handle_store_item(self, player: Player, event: AstrMessageEvent, args: str):
        """存入物品到储物戒 - 已禁用手动存入"""
        yield event.plain_result(
            "📦 储物戒说明：\n"
            "物品会在以下情况自动存入储物戒：\n"
            "  · 商店购买物品\n"
            "  · 历练/秘境获得物品\n"
            "  · Boss击杀掉落\n"
            "  · 悬赏任务奖励\n"
            "  · 卸下装备\n"
            "\n⚠️ 不支持手动存入物品"
        )

    @player_required
    async def handle_retrieve_item(self, player: Player, event: AstrMessageEvent, args: str):
        """说明储物戒物品无需取出，避免误删"""
        yield event.plain_result(
            "📦 储物戒物品无需取出：\n"
            "  · 装备：直接发送 /装备 <物品名>\n"
            "  · 材料：炼丹、强化、护府等操作会自动从储物戒扣除\n"
            "  · 销毁：请使用 /丢弃 <物品名> [数量]（不可恢复）\n"
            "⚠️ 为避免误删，/取出 已停用。"
        )

    @player_required
    async def handle_discard_item(self, player: Player, event: AstrMessageEvent, args: str):
        """丢弃储物戒中的物品"""
        if not args or args.strip() == "":
            yield event.plain_result(
                f"请指定要丢弃的物品\n"
                f"用法：{CMD_DISCARD_ITEM} 物品名 [数量]\n"
                f"示例：{CMD_DISCARD_ITEM} 精铁 5\n"
                f"⚠️ 丢弃的物品将永久销毁！"
            )
            return

        args = args.strip()
        parts = args.rsplit(" ", 1)

        # 解析物品名和数量（isdigit 对 ²/① 等字符也返回 True，需限制为 ASCII 数字）
        if len(parts) == 2 and parts[1].isascii() and parts[1].isdigit():
            item_name = parts[0]
            count = int(parts[1])
        else:
            item_name = args
            count = 1

        if count <= 0:
            yield event.plain_result("数量必须大于0")
            return

        # 丢弃物品
        success, message = await self.storage_ring_manager.discard_item(player, item_name, count)

        if success:
            yield event.plain_result(f"🗑️ {message}")
        else:
            yield event.plain_result(f"❌ {message}")

    async def _transfer_gold(
        self, player: Player, target: Player, amount: int, sender_name: str
    ) -> Tuple[bool, str]:
        """原子转账灵石：扣款与到账在同一事务内，余额不足则整笔失败。"""
        sender_id = player.user_id
        try:
            await self.db.conn.execute("BEGIN IMMEDIATE")
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (amount, sender_id, amount),
            )
            if cursor.rowcount == 0:
                await self.db.conn.rollback()
                return False, (
                    f"❌ 灵石不足！\n"
                    f"当前持有：{int(player.gold):,} 灵石\n"
                    f"需要转账：{amount:,} 灵石"
                )
            await self.db.conn.execute(
                "UPDATE players SET gold = gold + ? WHERE user_id = ?",
                (amount, target.user_id),
            )
            await self.db.conn.commit()
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"灵石转账失败（{sender_id} → {target.user_id}）: {e}")
            return False, "❌ 转账失败，灵石未扣除，请稍后再试。"

        # 以数据库为准刷新余额，避免并发操作导致内存快照偏差
        async with self.db.conn.execute(
            "SELECT gold FROM players WHERE user_id = ?", (sender_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            player.gold = int(row[0] or 0)

        target_name = target.user_name or target.user_id
        logger.info(
            f"灵石转账：{sender_name}({sender_id}) → {target_name}({target.user_id}) {amount} 灵石"
        )
        return True, (
            f"💎 转账成功！\n"
            f"【{amount:,} 灵石】→ {target_name}\n"
            f"当前余额：{player.gold:,} 灵石"
        )

    @player_required
    async def handle_gift_item(self, player: Player, event: AstrMessageEvent, args: str):
        """赠予物品给其他玩家；item_name 为「灵石」时直接转账"""
        target_id = None
        item_name = None
        count = 1

        # 从消息链中提取 At 组件和 Plain 文本
        text_parts = []
        message_chain = event.message_obj.message if hasattr(event, 'message_obj') and event.message_obj else []
        
        for comp in message_chain:
            if isinstance(comp, At):
                # 兼容多种At属性名
                if target_id is None:
                    if hasattr(comp, 'qq'):
                        target_id = str(comp.qq)
                    elif hasattr(comp, 'target'):
                        target_id = str(comp.target)
                    elif hasattr(comp, 'uin'):
                        target_id = str(comp.uin)
            elif isinstance(comp, Plain):
                text_parts.append(comp.text)

        # 合并文本内容并移除命令前缀
        text_content = "".join(text_parts).strip()
        for prefix in ["#赠予", "/赠予", "赠予"]:
            if text_content.startswith(prefix):
                text_content = text_content[len(prefix):].strip()
                break
        
        # 如果没有从At组件获取到target_id，尝试从文本解析纯数字QQ号
        if not target_id and text_content:
            parts = text_content.split(None, 1)
            if len(parts) >= 1:
                potential_id = parts[0].lstrip('@')
                if potential_id.isdigit() and len(potential_id) >= 5:
                    target_id = potential_id
                    text_content = parts[1].strip() if len(parts) > 1 else ""

        # 解析物品名和数量
        if text_content:
            parts = text_content.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].isascii() and parts[1].isdigit():
                item_name = parts[0].strip()
                count = int(parts[1])
            else:
                item_name = text_content.strip()

        # 验证必要参数
        if not target_id:
            yield event.plain_result(
                f"请指定赠予对象\n"
                f"用法：{CMD_GIFT_ITEM} @某人 物品名 [数量]\n"
                f"或：{CMD_GIFT_ITEM} QQ号 物品名 [数量]\n"
                f"转账灵石：{CMD_GIFT_ITEM} @某人 灵石 数量（直接到账）\n"
                f"示例：{CMD_GIFT_ITEM} 123456789 精铁 5\n"
                f"示例：{CMD_GIFT_ITEM} @某人 灵石 1000"
            )
            return

        if not item_name:
            yield event.plain_result("请指定要赠予的物品名称")
            return

        if count <= 0:
            yield event.plain_result("数量必须大于0")
            return

        target_player = await self.db.get_player_by_id(target_id)
        if not target_player:
            yield event.plain_result(f"目标玩家（QQ:{target_id}）尚未开始修仙")
            return
        if target_id == player.user_id:
            yield event.plain_result("不能赠予物品给自己")
            return

        # 灵石属于货币，不在储物戒/丹药背包中，直接走原子转账，无需对方确认
        if item_name == "灵石":
            success, message = await self._transfer_gold(
                player, target_player, count, event.get_sender_name()
            )
            yield event.plain_result(message)
            return

        pm = PillManager(self.db, self.storage_ring_manager.config_manager)
        quality = ""
        tokens = item_name.split()
        if len(tokens) >= 2 and tokens[-1] in pm.QUALITY_ORDER:
            quality = tokens.pop()
            item_name = " ".join(tokens)
        elif "|" in item_name:
            base_name, embedded_quality = pm.split_pill_key(item_name)
            item_name = base_name
            quality = embedded_quality

        sender_name = event.get_sender_name()
        im = ItemInstanceManager(self.db, self.config_manager)
        if im.item_type(item_name) in {"weapon", "armor", "head", "body", "boots", "gloves", "ring", "amulet", "accessory"}:
            if count != 1:
                yield event.plain_result("❌ 装备实例一次只能赠予 1 件。")
                return
            insts = im.list_instances(player, "storage", item_name)
            if quality:
                insts = [x for x in insts if x.get("quality") == quality]
            if not insts:
                yield event.plain_result(f"储物戒中没有可赠予的【{item_name}】装备实例")
                return
            inst = sorted(insts, key=lambda x: int(x.get("id", 0) or 0))[0]
            quality = inst.get("quality", "一般")
            im.move(player, int(inst["id"]), "gift")
            await self.db.update_player(player)
            try:
                await self.db.ext.create_pending_gift(
                    receiver_id=target_id,
                    sender_id=player.user_id,
                    sender_name=sender_name,
                    item_name=item_name,
                    count=1,
                    expires_hours=24,
                    item_kind="equipment",
                    item_quality=quality,
                    instance_id=int(inst["id"]),
                )
            except Exception as e:
                im.move(player, int(inst["id"]), "storage")
                await self.db.update_player(player)
                logger.error(f"装备赠予创建失败，已退回: {e}")
                yield event.plain_result("❌ 赠予创建失败，装备已退回储物戒。")
                return
            yield event.plain_result(
                f"📦 赠予请求已发送！\n"
                f"【{item_name}·{quality}】x1 → @{target_id}\n"
                f"等待对方确认...（24小时内有效）\n"
                f"对方可使用 {CMD_ACCEPT_GIFT} 接收或 {CMD_REJECT_GIFT} 拒绝"
            )
            return

        if pm.get_pill_by_name(item_name):
            pill_key = pm.select_pill_key(player.get_pills_inventory(), item_name, quality or None)
            if not pill_key:
                hint = f"（{quality}）" if quality else ""
                yield event.plain_result(f"丹药背包中没有【{item_name}】{hint}")
                return
            base_name, quality = pm.split_pill_key(pill_key)
            inventory = player.get_pills_inventory()
            if int(inventory.get(pill_key, 0)) < count:
                yield event.plain_result(f"【{base_name}·{quality}】数量不足（拥有 {inventory.get(pill_key, 0)}）")
                return
            inventory[pill_key] = int(inventory[pill_key]) - count
            if inventory[pill_key] <= 0:
                del inventory[pill_key]
            player.set_pills_inventory(inventory)
            await self.db.update_player(player)
            try:
                await self.db.ext.create_pending_gift(
                    receiver_id=target_id,
                    sender_id=player.user_id,
                    sender_name=sender_name,
                    item_name=base_name,
                    count=count,
                    expires_hours=24,
                    item_kind="pill",
                    item_quality=quality,
                )
            except Exception as e:
                inventory[pill_key] = int(inventory.get(pill_key, 0)) + count
                player.set_pills_inventory(inventory)
                await self.db.update_player(player)
                logger.error(f"丹药赠予创建失败，已退回: {e}")
                yield event.plain_result("❌ 赠予创建失败，丹药已退回背包。")
                return
            yield event.plain_result(
                f"📦 赠予请求已发送！\n"
                f"【{base_name}·{quality}】x{count} → @{target_id}\n"
                f"等待对方确认...（24小时内有效）\n"
                f"对方可使用 {CMD_ACCEPT_GIFT} 接收或 {CMD_REJECT_GIFT} 拒绝"
            )
            return

        # 检查物品是否在储物戒中
        if not self.storage_ring_manager.has_item(player, item_name, count):
            current = self.storage_ring_manager.get_item_count(player, item_name)
            if current == 0:
                yield event.plain_result(f"储物戒中没有【{item_name}】")
            else:
                yield event.plain_result(f"储物戒中【{item_name}】数量不足（当前：{current}个）")
            return

        # 先从储物戒中取出物品
        success, _ = await self.storage_ring_manager.retrieve_item(player, item_name, count)
        if not success:
            yield event.plain_result("赠予失败：无法取出物品")
            return

        try:
            await self.db.ext.create_pending_gift(
                receiver_id=target_id,
                sender_id=player.user_id,
                sender_name=sender_name,
                item_name=item_name,
                count=count,
                expires_hours=24,
            )
        except Exception as e:
            await self.storage_ring_manager.store_item(player, item_name, count, silent=True)
            logger.error(f"物品赠予创建失败，已退回: {e}")
            yield event.plain_result("❌ 赠予创建失败，物品已退回储物戒。")
            return

        yield event.plain_result(
            f"📦 赠予请求已发送！\n"
            f"【{item_name}】x{count} → @{target_id}\n"
            f"等待对方确认...（24小时内有效）\n"
            f"对方可使用 {CMD_ACCEPT_GIFT} 接收或 {CMD_REJECT_GIFT} 拒绝"
        )

    @player_required
    async def handle_accept_gift(self, player: Player, event: AstrMessageEvent):
        """接收赠予的物品（带事务防并发）"""
        user_id = player.user_id

        # 过期赠予不会自动过期删除：在事务外先退回发送者，避免物品永久卡在 pending_gifts
        try:
            await self.db.ext.cleanup_expired_gifts()
        except Exception as e:
            logger.warning(f"清理过期赠予失败: {e}")

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 事务内重新获取请求（不触发内部 commit 的过期清理）
            gift = await self.db.ext.get_pending_gift(user_id, cleanup=False)
            if not gift:
                await self.db.conn.rollback()
                yield event.plain_result("你没有待接收的赠予物品")
                return

            item_name = gift["item_name"]
            count = gift["count"]
            sender_name = gift["sender_name"]
            gift_id = gift["id"]
            item_kind = gift.get("item_kind", "item")
            item_quality = gift.get("item_quality", "")

            # 先删除请求记录，防止并发卡出多份
            await self.db.conn.execute("DELETE FROM pending_gifts WHERE id = ?", (gift_id,))

            if item_kind == "equipment":
                im = ItemInstanceManager(self.db, self.config_manager)
                sender = await self.db.get_player_by_id(gift.get("sender_id"))
                inst = im.get_instance(sender, gift.get("instance_id", 0)) if sender else None
                if not inst:
                    await self.db.conn.rollback()
                    yield event.plain_result("❌ 装备实例已不存在，赠予已取消。")
                    return
                volume = self.storage_ring_manager._get_item_volume(item_name)
                if self.storage_ring_manager.get_available_slots(player) < volume:
                    await self.db.conn.rollback()
                    yield event.plain_result("❌ 储物戒空间不足，接收失败，赠予请求已保留。")
                    return
                if not im.change_owner(sender, player, int(inst["id"])):
                    await self.db.conn.rollback()
                    yield event.plain_result("❌ 装备实例转移失败，请稍后再试。")
                    return
                await self.db.update_player(sender, commit=False)
                await self.db.update_player(player, commit=False)
                await self.db.conn.commit()
                yield event.plain_result(
                    f"✅ 已接收来自【{sender_name}】的赠予！\n"
                    f"获得：【{item_name}·{item_quality}】x1（已存入储物戒）"
                )
                return

            if item_kind == "pill":
                pm = PillManager(self.db, self.storage_ring_manager.config_manager)
                quality = item_quality or pm.DEFAULT_QUALITY
                pill_key = pm.make_pill_key(item_name, quality)
                inventory = player.get_pills_inventory()
                inventory[pill_key] = int(inventory.get(pill_key, 0)) + count
                player.set_pills_inventory(inventory)
                await self.db.update_player(player, commit=False)
                await self.db.conn.commit()
                yield event.plain_result(
                    f"✅ 已接收来自【{sender_name}】的赠予！\n"
                    f"获得：【{item_name}·{quality}】x{count}（已放入丹药背包）"
                )
                return

            # 尝试发放：丹药进丹药背包，其余存入储物戒（兼容缺少 item_kind 的旧赠予）
            success, message = await self.storage_ring_manager.grant_item(
                player, item_name, count, external_transaction=True, silent=True
            )

            if success:
                await self.db.update_player(player, commit=False)
                await self.db.conn.commit()
                target_desc = "丹药背包" if self.storage_ring_manager.is_pill(item_name) else "储物戒"
                extra_hint = f"\n{message}" if message and not self.storage_ring_manager.is_pill(item_name) else ""
                yield event.plain_result(
                    f"✅ 已接收来自【{sender_name}】的赠予！\n"
                    f"获得：【{item_name}】x{count}（已存入{target_desc}）{extra_hint}"
                )
            else:
                # 存入失败，回滚事务（这样 pending_gift 不会被删，物品还在）
                await self.db.conn.rollback()
                yield event.plain_result(f"❌ 接收失败：{message}\n请清理储物戒空间后再接收。")
                
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"接收赠予异常: {e}")
            yield event.plain_result("❌ 接收异常，请稍后再试。")

    @player_required
    async def handle_reject_gift(self, player: Player, event: AstrMessageEvent):
        """拒绝赠予的物品（事务内完成退回与删除）"""
        user_id = player.user_id

        try:
            await self.db.ext.cleanup_expired_gifts()
        except Exception as e:
            logger.warning(f"清理过期赠予失败: {e}")

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 事务内重新获取请求（不触发内部 commit 的过期清理）
            gift = await self.db.ext.get_pending_gift(user_id, cleanup=False)
            if not gift:
                await self.db.conn.rollback()
                yield event.plain_result("你没有待处理的赠予请求")
                return

            item_name = gift["item_name"]
            count = gift["count"]
            sender_id = gift["sender_id"]
            sender_name = gift["sender_name"]
            gift_id = gift["id"]
            item_kind = gift.get("item_kind", "item")
            item_quality = gift.get("item_quality", "")

            # 物品返还给发送者
            sender_player = await self.db.get_player_by_id(sender_id)
            if not sender_player:
                # 赠送者角色已不存在，物品无处可退；记录日志并作废，避免请求永远卡住。
                await self.db.conn.execute("DELETE FROM pending_gifts WHERE id = ?", (gift_id,))
                await self.db.conn.commit()
                logger.warning(f"拒绝赠予：赠送者 {sender_id} 不存在，赠予 {gift_id} 已作废")
                yield event.plain_result(
                    f"❌ 赠送者【{sender_name}】的角色已不存在，该赠予请求已作废（物品无法退回）。"
                )
                return

            # 原子删除：若已被接收流程处理，直接放弃，避免双方各得一份
            cursor = await self.db.conn.execute(
                "DELETE FROM pending_gifts WHERE id = ? AND receiver_id = ?",
                (gift_id, user_id),
            )
            if cursor.rowcount <= 0:
                await self.db.conn.rollback()
                yield event.plain_result("该赠予请求已被处理，请刷新后重试。")
                return

            if item_kind == "equipment":
                im = ItemInstanceManager(self.db, self.config_manager)
                inst = im.get_instance(sender_player, gift.get("instance_id", 0))
                if not inst:
                    await self.db.conn.commit()
                    yield event.plain_result("❌ 装备实例已不存在，赠予已取消。")
                    return
                volume = self.storage_ring_manager._get_item_volume(item_name)
                if self.storage_ring_manager.get_available_slots(sender_player) < volume:
                    await self.db.conn.rollback()
                    yield event.plain_result(
                        "❌ 赠送者储物戒空间不足，装备暂时无法退回，赠予请求已保留。"
                    )
                    return
                if not im.move(sender_player, int(inst["id"]), "storage"):
                    await self.db.conn.rollback()
                    yield event.plain_result("❌ 装备实例退回失败，请稍后再试。")
                    return
                await self.db.update_player(sender_player, commit=False)
                await self.db.conn.commit()
                yield event.plain_result(
                    f"已拒绝来自【{sender_name}】的赠予\n"
                    f"【{item_name}·{item_quality}】x1 已退回赠送者储物戒"
                )
                return

            if item_kind == "pill":
                pm = PillManager(self.db, self.storage_ring_manager.config_manager)
                quality = item_quality or pm.DEFAULT_QUALITY
                pill_key = pm.make_pill_key(item_name, quality)
                inventory = sender_player.get_pills_inventory()
                inventory[pill_key] = int(inventory.get(pill_key, 0)) + count
                sender_player.set_pills_inventory(inventory)
                await self.db.update_player(sender_player, commit=False)
                await self.db.conn.commit()
                yield event.plain_result(
                    f"已拒绝来自【{sender_name}】的赠予\n"
                    f"【{item_name}·{quality}】x{count} 已返还"
                )
                return

            ok, store_msg = await self.storage_ring_manager.store_item(
                sender_player, item_name, count,
                silent=False, external_transaction=True, allow_overflow=True,
            )
            if not ok:
                await self.db.conn.rollback()
                yield event.plain_result(
                    f"❌ 退回失败：{store_msg}\n赠予请求已保留，请让赠送者清理储物戒后重试。"
                )
                return
            await self.db.update_player(sender_player, commit=False)
            await self.db.conn.commit()
            label = f"{item_name}·{item_quality}" if item_quality else item_name
            yield event.plain_result(
                f"已拒绝来自【{sender_name}】的赠予\n"
                f"【{label}】x{count} 已返还\n{store_msg}"
            )
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"拒绝赠予异常: {e}")
            yield event.plain_result("❌ 拒绝赠予异常，请稍后再试。")

    @player_required
    async def handle_upgrade_ring(self, player: Player, event: AstrMessageEvent, ring_name: str):
        """升级/更换储物戒"""
        if not ring_name or ring_name.strip() == "":
            # 显示可用的储物戒列表
            rings = self.storage_ring_manager.get_all_storage_rings()
            current_capacity = self.storage_ring_manager.get_ring_capacity(player.storage_ring)

            lines = [
                f"=== 储物戒列表 ===\n",
                f"当前：【{player.storage_ring}】({current_capacity}格)\n",
                f"━━━━━━━━━━━━━━━\n",
            ]

            for ring in rings:
                # 标记当前装备
                if ring["name"] == player.storage_ring:
                    marker = "✓ "
                elif ring["capacity"] <= current_capacity:
                    marker = "✗ "  # 容量不高于当前的
                else:
                    marker = "  "

                level_name = format_applicable_realm(
                    ring["required_level_index"],
                    self.config_manager.level_data,
                    self.config_manager.body_level_data,
                    rank=str(ring.get("rank", "") or ""),
                )
                lines.append(
                    f"{marker}【{ring['name']}】({ring['rank']})\n"
                    f"    容量：{ring['capacity']}格 | 适用境界：{level_name}\n"
                )

            lines.append(f"\n用法：{CMD_UPGRADE_RING} 储物戒名")
            lines.append("\n注：储物戒只能升级，不能卸下")

            yield event.plain_result("".join(lines))
            return

        ring_name = ring_name.strip()

        # 检查是否为储物戒类型
        ring_config = self.storage_ring_manager.get_storage_ring_config(ring_name)
        if not ring_config:
            yield event.plain_result(f"未找到储物戒：{ring_name}")
            return

        # 升级储物戒
        success, message = await self.storage_ring_manager.upgrade_ring(player, ring_name)

        if success:
            yield event.plain_result(f"✅ {message}")
        else:
            yield event.plain_result(f"❌ {message}")

    def _categorize_items(self, items: dict) -> dict:
        """将物品按分类整理。已知物品优先按配置类型分类。"""
        result = {cat: [] for cat in ITEM_CATEGORIES.keys()}

        gear_types = {"weapon", "armor", "head", "body", "boots", "gloves", "ring", "amulet", "accessory"}
        technique_types = {"technique", "main_technique"}
        material_types = {"material", "符箓", "talisman", "杂物", "丹方", "材料包"}

        for item_name, count in items.items():
            item_config = self.config_manager.items_data.get(item_name)
            if not item_config:
                item_config = self.config_manager.weapons_data.get(item_name)
            if not item_config and hasattr(self.config_manager, "techniques_data"):
                item_config = self.config_manager.techniques_data.get(item_name)

            if item_config:
                item_type = ItemRules.normalize_equipment_type(item_config)
                raw_type = str(item_config.get("type", "") or "")
                if item_type in gear_types:
                    result["装备"].append((item_name, count))
                elif item_type in technique_types:
                    result["功法"].append((item_name, count))
                elif item_type in material_types or raw_type in ("材料", "符箓", "杂物", "丹方", "材料包"):
                    result["材料"].append((item_name, count))
                else:
                    result["其他"].append((item_name, count))
                continue

            categorized = False
            for category, keywords in ITEM_CATEGORIES.items():
                if category == "其他":
                    continue
                for keyword in keywords:
                    if keyword in item_name or item_name in keyword:
                        result[category].append((item_name, count))
                        categorized = True
                        break
                if categorized:
                    break
            if not categorized:
                result["其他"].append((item_name, count))

        return {k: v for k, v in result.items() if v}

    @player_required
    async def handle_search_item(self, player: Player, event: AstrMessageEvent, keyword: str):
        """搜索储物戒中的物品"""
        if not keyword or keyword.strip() == "":
            yield event.plain_result(
                f"请指定搜索关键词\n"
                f"用法：{CMD_SEARCH_ITEM} 关键词\n"
                f"示例：{CMD_SEARCH_ITEM} 灵草"
            )
            return

        keyword = keyword.strip().lower()
        items = self.storage_ring_manager.get_storage_ring_info(player).get("items", {})
        
        # 模糊搜索
        matched = []
        for item_name, count in items.items():
            if keyword in item_name.lower():
                matched.append((item_name, count))
        
        if not matched:
            yield event.plain_result(f"未找到包含「{keyword}」的物品")
            return
        
        lines = [f"=== 搜索结果：{keyword} ===\n"]
        for item_name, count in matched:
            lines.append(f"  · {item_name}×{count}\n")
        lines.append(f"\n共找到 {len(matched)} 种物品")
        
        yield event.plain_result("".join(lines))

    @player_required
    async def handle_store_all(self, player: Player, event: AstrMessageEvent, category: str = None):
        """批量存入物品（预留接口，实际物品来源需要其他系统配合）"""
        yield event.plain_result(
            f"📦 批量存入功能说明：\n"
            f"当前物品会在以下情况自动存入储物戒：\n"
            f"  · 商店购买物品\n"
            f"  · 历练/秘境获得物品\n"
            f"  · Boss击杀掉落\n"
            f"  · 悬赏任务奖励\n"
            f"  · 卸下装备\n"
            f"\n所有物品获取后会自动存入储物戒"
        )

    @player_required
    async def handle_retrieve_all(self, player: Player, event: AstrMessageEvent, category: str = None):
        """说明批量取出已停用，避免整类材料被误删"""
        yield event.plain_result(
            "📦 批量取出已停用：\n"
            "  · 装备：/装备 <物品名>\n"
            "  · 材料：炼丹、强化、护府等操作会自动扣除\n"
            "  · 销毁：/丢弃 <物品名> [数量]（不可恢复）\n"
            "⚠️ 储物戒中的物品会安全保留。"
        )
