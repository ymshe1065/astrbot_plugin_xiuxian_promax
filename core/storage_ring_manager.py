# core/storage_ring_manager.py

import json
from typing import TYPE_CHECKING, Optional, Tuple, List, Dict
from ..models import Player
from .item_instance_manager import ItemInstanceManager
from .item_rules import ItemRules

if TYPE_CHECKING:
    from ..data import DataBase
    from ..config_manager import ConfigManager


class StorageRingManager:
    """储物戒管理器 - 容量制（支持空间附魔扩容）

    计容规则：
    - 装备按实例逐件占格（使用物品配置的 volume）
    - 材料 / 符箓 / 丹方 / 杂物等可堆叠物品「每格 50 个」，超出部分往下占新格
    - 丹药不占储物戒（走独立丹药背包）
    """

    PILL_TYPES = ["丹药"]

    STACK_LIMIT = 50                 # 单格堆叠上限（存量与仓库共用）
    ENCHANT_ITEM = "空间石"          # 附魔材料
    ENCHANT_BONUS_PER_STONE = 5      # 每颗空间石 +5 容量
    # 空间附魔不设上限：容量只受空间石持有量限制

    def __init__(self, db: "DataBase", config_manager: "ConfigManager"):
        self.db = db
        self.config_manager = config_manager
        self.instance_manager = ItemInstanceManager(db, config_manager)

    def get_storage_ring_config(self, ring_name: str) -> Optional[dict]:
        return self.config_manager.storage_rings_data.get(ring_name)

    def get_enchant_bonus(self, player: Player) -> int:
        """读取玩家的空间附魔加成容量（存于 permanent_bonus）"""
        try:
            data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            return int(data.get("storage_enchant", 0))
        except Exception:
            return 0

    def get_total_capacity(self, player: Player) -> int:
        """戒指基础容量 + 空间附魔"""
        return self.get_ring_capacity(player.storage_ring) + self.get_enchant_bonus(player)

    async def enchant_ring(self, player: Player, stone_count: int = 1) -> Tuple[bool, str]:
        """空间附魔：消耗空间石永久扩充储物戒容量（不设上限）"""
        if stone_count <= 0:
            return False, "❌ 请输入有效的空间石数量。"

        current_enchant = self.get_enchant_bonus(player)
        need = int(stone_count)
        if not self.has_item(player, self.ENCHANT_ITEM, need):
            have = self.get_item_count(player, self.ENCHANT_ITEM)
            return False, (
                f"❌ 空间石不足（拥有 {have}，需要 {need}）。\n"
                f"每颗空间石可扩容 +{self.ENCHANT_BONUS_PER_STONE} 格。"
            )

        success, msg = await self.retrieve_item(player, self.ENCHANT_ITEM, need)
        if not success:
            return False, f"❌ 扣除空间石失败：{msg}"

        try:
            data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
        except Exception:
            data = {}
        data["storage_enchant"] = current_enchant + need * self.ENCHANT_BONUS_PER_STONE
        player.permanent_bonus = json.dumps(data, ensure_ascii=False)
        await self.db.update_player(player)

        return True, (
            f"✨ 空间附魔成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"消耗【{self.ENCHANT_ITEM}】x{need}\n"
            f"附魔容量：+{current_enchant} → +{data['storage_enchant']}\n"
            f"当前总容量：{self.get_total_capacity(player)} 格"
        )

    def get_ring_capacity(self, ring_name: str) -> int:
        config = self.get_storage_ring_config(ring_name)
        if config:
            return config.get("capacity", 20)
        return 20

    def _get_item_volume(self, item_name: str) -> int:
        """获取单个物品的容量体积（默认1）"""
        item_config = self.config_manager.items_data.get(item_name)
        if not item_config:
            item_config = self.config_manager.weapons_data.get(item_name)
        if not item_config and hasattr(self.config_manager, "techniques_data"):
            item_config = self.config_manager.techniques_data.get(item_name)

        if item_config:
            vol = item_config.get("volume", item_config.get("weight", 1))
            try:
                return max(1, int(vol))
            except (TypeError, ValueError):
                return 1
        return 1

    @staticmethod
    def stack_slots(count: int) -> int:
        """可堆叠物品占格数：每 STACK_LIMIT（50）个占 1 格，不足一格按一格算。"""
        try:
            amount = int(count or 0)
        except (TypeError, ValueError):
            return 0
        if amount <= 0:
            return 0
        limit = StorageRingManager.STACK_LIMIT
        return (amount + limit - 1) // limit

    def extra_slots(self, owned: int, add: int) -> int:
        """从 owned 个增加到 owned + add 个需要的新增格数。"""
        try:
            owned = int(owned or 0)
            add = int(add or 0)
        except (TypeError, ValueError):
            return 0
        return self.stack_slots(owned + add) - self.stack_slots(owned)

    def room_for(self, owned: int, available: int) -> int:
        """available 个空格下，已有 owned 个时还能再放入多少个同类物品。"""
        try:
            owned = max(0, int(owned or 0))
            available = max(0, int(available or 0))
        except (TypeError, ValueError):
            return 0
        limit = self.STACK_LIMIT
        return (self.stack_slots(owned) + available) * limit - owned

    def get_used_slots(self, player: Player) -> int:
        """获取已使用的容量数。

        装备实例逐件计格；材料/符箓/丹方/杂物等可堆叠物品每 50 个占 1 格，
        超出 50 的部分继续占新格（例如 120 个占 3 格）。
        """
        items = player.get_storage_ring_items()
        used_volume = 0
        for item_name, count in items.items():
            try:
                amount = int(count or 0)
            except (TypeError, ValueError):
                amount = 0
            if amount <= 0:
                continue
            if self.is_equipment_item(item_name):
                # 兼容旧存档中按名称堆叠的装备
                used_volume += self._get_item_volume(item_name) * amount
            else:
                used_volume += self.stack_slots(amount)
        for inst in self.instance_manager.list_instances(player, "storage"):
            used_volume += self._get_item_volume(inst.get("name", ""))
        return used_volume

    def get_available_slots(self, player: Player) -> int:
        capacity = self.get_total_capacity(player)
        used = self.get_used_slots(player)
        return max(0, capacity - used)

    def can_accept(self, player: Player, item_name: str, count: int = 1) -> bool:
        """判断储物戒还能否再收下 count 个该物品（按堆叠规则计算占地）。

        装备逐件占格；可堆叠物品每 50 个 1 格，已有存货时只需补足新增格数。
        """
        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            return True
        if self.is_equipment_item(item_name):
            return self.get_available_slots(player) >= self._get_item_volume(item_name) * count
        items = player.get_storage_ring_items()
        try:
            owned = int(items.get(item_name, 0) or 0)
        except (TypeError, ValueError):
            owned = 0
        return self.get_available_slots(player) >= self.extra_slots(owned, count)

    def get_space_warning(self, player: Player) -> Optional[str]:
        available = self.get_available_slots(player)
        capacity = self.get_total_capacity(player)
        used = self.get_used_slots(player)

        if available <= 0:
            return f"⚠️ 储物戒已满！({used}/{capacity}格)"
        elif available <= capacity * 0.2:
            return f"⚠️ 储物戒空间不足！仅剩{available}格({used}/{capacity}格)"
        return None

    GEAR_TYPES = {"weapon", "armor", "head", "body", "boots", "gloves", "ring", "amulet", "accessory"}

    def is_equipment_item(self, item_name: str) -> bool:
        cfg = self.config_manager.items_data.get(item_name)
        if not cfg:
            cfg = self.config_manager.weapons_data.get(item_name)
        if not cfg:
            return False
        return ItemRules.normalize_equipment_type(cfg) in self.GEAR_TYPES

    def is_pill(self, item_name: str) -> bool:
        return self.config_manager.is_pill(item_name)

    def can_store_item(self, item_name: str) -> Tuple[bool, str]:
        if self.is_pill(item_name):
            return False, f"【{item_name}】是丹药，不能存入储物戒（请使用丹药背包）"
        if item_name in self.config_manager.storage_rings_data:
            return False, f"【{item_name}】是储物戒，不能存入另一个储物戒"
        return True, ""

    async def grant_item(
        self,
        player: Player,
        item_name: str,
        count: int = 1,
        *,
        quality: str = "",
        silent: bool = True,
        external_transaction: bool = False,
        allow_overflow: bool = True,
    ) -> Tuple[bool, str]:
        """发放奖励/掉落：丹药进入丹药背包，其余物品进入储物戒。

        与 store_item 不同，此入口不会把丹药当成“不可存入物品”而丢弃，
        用于所有系统奖励、掉落、任务结算等“给玩家物品”的场景。
        """
        count = max(0, int(count or 0))
        if count <= 0:
            return True, ""
        if self.is_pill(item_name):
            from .pill_manager import PillManager

            pm = PillManager(self.db, self.config_manager)
            await pm.add_pill_to_inventory(
                player,
                item_name,
                count,
                external_transaction=external_transaction,
                quality=quality or PillManager.DEFAULT_QUALITY,
            )
            return True, f"【{item_name}】x{count} 已放入丹药背包"
        return await self.store_item(
            player,
            item_name,
            count,
            silent=silent,
            external_transaction=external_transaction,
            allow_overflow=allow_overflow,
            quality=quality,
        )

    async def store_item(self, player: Player, item_name: str, count: int = 1, silent: bool = False, external_transaction: bool = False, allow_overflow: bool = True, quality: str = "") -> Tuple[bool, str]:
        can_store, reason = self.can_store_item(item_name)
        if not can_store:
            return False, reason

        orig_player = player  # 保留调用方对象引用，结束时同步物品，防止调用方后续用旧数据覆盖
        if not external_transaction:
            await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            if not external_transaction:
                fresh_player = await self.db.get_player_by_id(player.user_id)
                if not fresh_player:
                    if not external_transaction:
                        await self.db.conn.rollback()
                    return False, "玩家不存在或已被删除"
                player = fresh_player
                
            items = player.get_storage_ring_items()

            item_volume = self._get_item_volume(item_name)
            if self.is_equipment_item(item_name):
                required_volume = item_volume * count
            else:
                # 可堆叠物品：每 50 个 1 格，已有存货时只算新增格数
                try:
                    owned = int(items.get(item_name, 0) or 0)
                except (TypeError, ValueError):
                    owned = 0
                required_volume = self.extra_slots(owned, count)
            available = self.get_available_slots(player)

            if available < required_volume:
                if allow_overflow:
                    if self.is_equipment_item(item_name):
                        # 装备按实例保存到 overflow 位置，保留品质/词条
                        self.instance_manager.create_instances(
                            player, item_name, count,
                            quality=quality or self.instance_manager.roll_quality(),
                            location="overflow",
                        )
                        orig_player.set_equipment_instances(player.get_equipment_instances())
                    else:
                        overflow = player.get_overflow_items()
                        overflow[item_name] = int(overflow.get(item_name, 0) or 0) + count
                        player.set_overflow_items(overflow)
                        orig_player.set_overflow_items(overflow)
                    if not external_transaction:
                        await self.db.update_player(player)
                        await self.db.conn.commit()
                    return True, f"储物戒容量不足，【{item_name}】x{count} 已转入溢出仓库，请清理储物戒后 /领取溢出"
                if not external_transaction:
                    await self.db.conn.rollback()
                capacity = self.get_total_capacity(player)
                return False, (
                    f"储物戒容量不足！还需要 {required_volume} 格，仅剩 {available} 格（上限 {capacity} 格）"
                )

            if self.is_equipment_item(item_name):
                self.instance_manager.create_instances(
                    player, item_name, count,
                    quality=quality or self.instance_manager.roll_quality(),
                    location="storage",
                )
                orig_player.set_equipment_instances(player.get_equipment_instances())
                if not external_transaction:
                    await self.db.update_player(player)
                    await self.db.conn.commit()
                capacity = self.get_total_capacity(player)
                used = self.get_used_slots(player)
                if silent:
                    return True, ""
                return True, f"已将【{item_name}】x{count} 存入储物戒（{used}/{capacity}格）"

            items[item_name] = items.get(item_name, 0) + count
            player.set_storage_ring_items(items)
            orig_player.set_storage_ring_items(items)  # 同步调用方对象
            orig_player.set_equipment_instances(player.get_equipment_instances())

            if not external_transaction:
                await self.db.update_player(player)
                await self.db.conn.commit()

            capacity = self.get_total_capacity(player)
            used = self.get_used_slots(player)

            if silent:
                return True, ""

            warning = self.get_space_warning(player)
            msg = f"已将【{item_name}】x{count} 存入储物戒（{used}/{capacity}格）"
            if warning:
                msg += f"\n{warning}"
            return True, msg
        except Exception:
            if not external_transaction:
                await self.db.conn.rollback()
            raise

    async def claim_overflow(self, player: Player) -> Tuple[int, str]:
        """把溢出仓库物品尽量取回储物戒，返回 (取回数量, 描述)。"""
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                return 0, "玩家不存在"
            overflow = fresh.get_overflow_items()
            overflow_equipment = self.instance_manager.list_instances(fresh, "overflow")
            if not overflow and not overflow_equipment:
                await self.db.conn.rollback()
                return 0, "溢出仓库是空的"
            items = fresh.get_storage_ring_items()
            claimed = []
            remaining = {}
            for item_name, raw_count in overflow.items():
                count = int(raw_count or 0)
                if count <= 0:
                    continue
                item_volume = self._get_item_volume(item_name)
                available = self.get_available_slots(fresh)
                if self.is_equipment_item(item_name):
                    fit = min(count, available // max(1, item_volume))
                else:
                    # 可堆叠物品：已有存货 + 剩余空格能装多少取多少（每格 50）
                    try:
                        owned = int(items.get(item_name, 0) or 0)
                    except (TypeError, ValueError):
                        owned = 0
                    room = (self.stack_slots(owned) + available) * self.STACK_LIMIT - owned
                    fit = max(0, min(count, room))
                if fit > 0:
                    # 兼容旧格式：旧溢出记录只有名称和数量，无法恢复品质
                    if self.is_pill(item_name):
                        # 丹药必须回到丹药背包，历史错误进入溢出仓库的丹药也在这里纠正
                        await self.grant_item(
                            fresh, item_name, fit, external_transaction=True
                        )
                    elif self.is_equipment_item(item_name):
                        self.instance_manager.create_instances(
                            fresh, item_name, fit,
                            quality=self.instance_manager.roll_quality(),
                            location="storage",
                        )
                    else:
                        items[item_name] = int(items.get(item_name, 0) or 0) + fit
                        fresh.set_storage_ring_items(items)
                    claimed.append(f"{item_name}×{fit}")
                    count -= fit
                if count > 0:
                    remaining[item_name] = count

            # 新格式：装备实例保存在 overflow 位置，领取时保持品质与词条
            for inst in sorted(overflow_equipment, key=lambda x: int(x.get("id", 0) or 0)):
                name = inst.get("name", "")
                item_volume = self._get_item_volume(name)
                if self.get_available_slots(fresh) < item_volume:
                    continue
                if self.instance_manager.move(fresh, int(inst.get("id", 0) or 0), "storage"):
                    claimed.append(f"{name}×1")
            fresh.set_overflow_items(remaining)
            # 同步调用方对象
            player.set_storage_ring_items(items)
            player.set_overflow_items(remaining)
            player.set_pills_inventory(fresh.get_pills_inventory())
            player.set_equipment_instances(fresh.get_equipment_instances())
            await self.db.update_player(fresh)
            await self.db.conn.commit()
            if claimed:
                return len(claimed), "已取回：" + "、".join(claimed)
            return 0, "储物戒空间不足，未能取回任何溢出物品"
        except Exception:
            await self.db.conn.rollback()
            raise

    async def retrieve_item(self, player: Player, item_name: str, count: int = 1) -> Tuple[bool, str]:
        orig_player = player  # 同步调用方对象，防止其后续 update_player 回滚本次取出
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(player.user_id)
            if not player:
                await self.db.conn.rollback()
                return False, "玩家数据不存在，请重新踏入仙途"
            items = player.get_storage_ring_items()

            if self.is_equipment_item(item_name):
                insts = self.instance_manager.list_instances(player, "storage", item_name)
                if len(insts) >= count:
                    for inst in sorted(insts, key=lambda x: int(x.get("id", 0) or 0))[:count]:
                        self.instance_manager.remove(player, int(inst.get("id", 0) or 0))
                    orig_player.set_equipment_instances(player.get_equipment_instances())
                    await self.db.update_player(player)
                    await self.db.conn.commit()
                    capacity = self.get_total_capacity(player)
                    used = self.get_used_slots(player)
                    return True, f"已取出【{item_name}】x{count}（{used}/{capacity}格）"
                # 兼容旧名称堆叠
                legacy = int(items.get(item_name, 0) or 0)
                if legacy >= count:
                    if legacy <= count:
                        del items[item_name]
                    else:
                        items[item_name] = legacy - count
                    player.set_storage_ring_items(items)
                    orig_player.set_storage_ring_items(items)
                    await self.db.update_player(player)
                    await self.db.conn.commit()
                    return True, f"已取出【{item_name}】x{count}"
                await self.db.conn.rollback()
                return False, f"储物戒中【{item_name}】数量不足（当前：{len(insts) + legacy}个）"

            # 只认储物戒：仓库里的东西必须先 /一键取出 才能用
            ring_have = int(items.get(item_name, 0) or 0)
            if ring_have < count:
                await self.db.conn.rollback()
                if ring_have <= 0:
                    return False, f"储物戒中没有【{item_name}】（仓库物品需先 /一键取出）"
                return False, f"储物戒中【{item_name}】数量不足（当前：{ring_have}个）"

            if count >= ring_have:
                items.pop(item_name, None)
            else:
                items[item_name] = ring_have - count

            player.set_storage_ring_items(items)
            orig_player.set_storage_ring_items(items)  # 同步调用方对象
            await self.db.update_player(player)
            await self.db.conn.commit()

            capacity = self.get_total_capacity(player)
            used = self.get_used_slots(player)
            return True, f"已从储物戒取出【{item_name}】x{count}（{used}/{capacity}格）"
        except Exception:
            await self.db.conn.rollback()
            raise

    async def discard_item(self, player: Player, item_name: str, count: int = 1) -> Tuple[bool, str]:
        orig_player = player  # 同步调用方对象，防止其后续 update_player 回滚本次丢弃
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(player.user_id)
            if not player:
                await self.db.conn.rollback()
                return False, "玩家数据不存在，请重新踏入仙途"
            items = player.get_storage_ring_items()

            if self.is_equipment_item(item_name):
                insts = self.instance_manager.list_instances(player, "storage", item_name)
                if len(insts) >= count:
                    for inst in sorted(insts, key=lambda x: int(x.get("id", 0) or 0))[:count]:
                        self.instance_manager.remove(player, int(inst.get("id", 0) or 0))
                    orig_player.set_equipment_instances(player.get_equipment_instances())
                    await self.db.update_player(player)
                    await self.db.conn.commit()
                    capacity = self.get_total_capacity(player)
                    used = self.get_used_slots(player)
                    return True, f"已消耗【{item_name}】x{count}（{used}/{capacity}格）"
                legacy = int(items.get(item_name, 0) or 0)
                if legacy >= count:
                    if legacy <= count:
                        del items[item_name]
                    else:
                        items[item_name] = legacy - count
                    player.set_storage_ring_items(items)
                    orig_player.set_storage_ring_items(items)
                    await self.db.update_player(player)
                    await self.db.conn.commit()
                    return True, f"已消耗【{item_name}】x{count}"
                await self.db.conn.rollback()
                return False, f"储物戒中【{item_name}】数量不足（当前：{len(insts) + legacy}个）"

            # 只认储物戒：仓库物品不参与消耗，需先 /一键取出
            ring_have = int(items.get(item_name, 0) or 0)
            if ring_have < count:
                await self.db.conn.rollback()
                if ring_have <= 0:
                    return False, f"储物戒中没有【{item_name}】（仓库物品需先 /一键取出）"
                return False, f"储物戒中【{item_name}】数量不足（当前：{ring_have}个）"

            if count >= ring_have:
                items.pop(item_name, None)
            else:
                items[item_name] = ring_have - count
            discard_count = count

            player.set_storage_ring_items(items)
            orig_player.set_storage_ring_items(items)  # 同步调用方对象
            await self.db.update_player(player)
            await self.db.conn.commit()

            capacity = self.get_total_capacity(player)
            used = self.get_used_slots(player)
            return True, f"已丢弃【{item_name}】x{discard_count}（{used}/{capacity}格）"
        except Exception:
            await self.db.conn.rollback()
            raise

    QUALITY_PRICE_MULT = {"一般": 1.0, "良好": 1.05, "极佳": 1.10, "大师": 1.15}
    RECYCLE_RATE = 0.60

    def _recycle_value(self, item_name: str, quality: str = "一般", affixes=None) -> int:
        from .item_valuation import recycle_value
        try:
            return recycle_value(self.config_manager, item_name, quality, affixes)
        except Exception:
            base = 0
            cfg = self.config_manager.get_item_config(item_name) or {}
            if not cfg:
                cfg = self.config_manager.get_material_info(item_name) or {}
            try:
                base = max(0, int(cfg.get("price", 0) or 0))
            except (TypeError, ValueError):
                base = 0
            mult = self.QUALITY_PRICE_MULT.get(str(quality or "一般"), 1.0)
            return max(0, int(round(base * mult * self.RECYCLE_RATE)))

    def _is_gear(self, item_name: str) -> bool:
        cfg = self.config_manager.items_data.get(item_name) or self.config_manager.weapons_data.get(item_name)
        return bool(cfg and ItemRules.is_equipment(cfg))

    async def recycle_item(self, player: Player, item_name: str, count: int = 1, quality: str = "") -> Tuple[bool, str]:
        """按 60% 配置价值回收储物戒物品/装备/丹药背包中的丹药。"""
        if count <= 0:
            return False, "数量必须大于0"
        orig_player = player
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(player.user_id)
            if not player:
                await self.db.conn.rollback()
                return False, "玩家数据不存在，请重新踏入仙途"

            total_gold = 0
            recycled = 0
            details = []
            items = player.get_storage_ring_items()
            pills = player.get_pills_inventory()

            # 1. 装备实例优先；未指定品质时从低品质开始回收，保护高品质装备。
            if self._is_gear(item_name):
                insts = self.instance_manager.list_instances(player, "storage", item_name)
                if quality:
                    insts = [x for x in insts if str(x.get("quality") or "一般") == quality]
                insts.sort(key=lambda x: (
                    self.QUALITY_PRICE_MULT.get(str(x.get("quality") or "一般"), 1.0),
                    int(x.get("id", 0) or 0),
                ))
                for inst in insts[:max(0, count - recycled)]:
                    q = str(inst.get("quality") or "一般")
                    value = self._recycle_value(item_name, q, inst.get("affixes"))
                    self.instance_manager.remove(player, int(inst.get("id", 0) or 0))
                    total_gold += value
                    recycled += 1
                    details.append(f"{item_name}·{q}")
                if recycled:
                    orig_player.set_equipment_instances(player.get_equipment_instances())
                # 旧存档可能还有同名计数；实例不足时继续走计数。
                if not quality and recycled < count and int(items.get(item_name, 0) or 0) > 0:
                    legacy = int(items.get(item_name, 0) or 0)
                    take = min(legacy, count - recycled)
                    value = self._recycle_value(item_name, "一般") * take
                    if take >= legacy:
                        del items[item_name]
                    else:
                        items[item_name] = legacy - take
                    total_gold += value
                    recycled += take
                    details.append(f"{item_name}x{take}")

            # 2. 丹药背包
            if recycled < count and self.config_manager.is_pill(item_name):
                matching = []
                for key, amount in list(pills.items()):
                    base, q = (key.rsplit("|", 1) + ["一般"])[:2] if "|" in key else (key, "一般")
                    if base != item_name:
                        continue
                    if quality and q != quality:
                        continue
                    matching.append((self.QUALITY_PRICE_MULT.get(q, 1.0), key, q, int(amount or 0)))
                for _, key, q, amount in sorted(matching):
                    take = min(amount, count - recycled)
                    if take <= 0:
                        continue
                    value = self._recycle_value(item_name, q) * take
                    if take >= amount:
                        del pills[key]
                    else:
                        pills[key] = amount - take
                    total_gold += value
                    recycled += take
                    details.append(f"{item_name}·{q}x{take}")

            # 3. 普通材料/符箓/丹方等堆叠物品
            if recycled < count and item_name in items:
                amount = int(items.get(item_name, 0) or 0)
                take = min(amount, count - recycled)
                value = self._recycle_value(item_name, quality or "一般") * take
                if take >= amount:
                    del items[item_name]
                else:
                    items[item_name] = amount - take
                total_gold += value
                recycled += take
                details.append(f"{item_name}x{take}")

            if recycled <= 0:
                await self.db.conn.rollback()
                return False, f"储物戒和丹药背包中没有可回收的【{item_name}】"

            player.gold += total_gold
            player.set_storage_ring_items(items)
            player.set_pills_inventory(pills)
            orig_player.gold = player.gold
            orig_player.set_storage_ring_items(items)
            orig_player.set_pills_inventory(pills)
            await self.db.update_player(player)
            await self.db.conn.commit()
            name_text = "、".join(details[:5]) + ("…" if len(details) > 5 else "")
            return True, (
                f"♻️ 已回收 {name_text}\n"
                f"获得灵石：{total_gold:,}（按配置价值 60%）"
            )
        except Exception:
            await self.db.conn.rollback()
            raise

    # ===== 一键回收 =====

    EXPENSIVE_THRESHOLD = 10000  # 单价达到该值的物品默认保留，交由玩家单独回收

    def _unit_price(self, item_name: str) -> int:
        """物品配置单价（用于"贵重物品保护"判定，不含品质与折扣）。"""
        cfg = self.config_manager.get_item_config(item_name) or {}
        if not cfg:
            cfg = self.config_manager.items_data.get(item_name) or {}
        if not cfg:
            cfg = self.config_manager.get_material_info(item_name) or {}
        try:
            return max(0, int(cfg.get("price", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def plan_bulk_recycle(
        self,
        player: Player,
        include_equipment: bool = False,
        include_warehouse: bool = False,
        include_pills: bool = False,
        expensive_threshold: int = 0,
    ) -> dict:
        """生成一键回收清单（不落库）。

        - 堆叠物品：储物戒 + 可选仓库
        - 装备实例：默认不回收，``include_equipment`` 打开后按实例逐件计入
        - 丹药：默认不回收，``include_pills`` 打开后计入丹药背包
        - 单价 ≥ ``expensive_threshold`` 的物品进入 ``kept``，不回收
        """
        threshold = self.EXPENSIVE_THRESHOLD if expensive_threshold is None else expensive_threshold
        if expensive_threshold == 0:
            threshold = self.EXPENSIVE_THRESHOLD
        plan = {"stacks": [], "pills": [], "equipment": [], "kept": [], "total": 0}

        containers = [("storage", player.get_storage_ring_items())]
        if include_warehouse:
            containers.append(("warehouse", player.get_warehouse_items()))

        for where, items in containers:
            for name, raw in sorted((items or {}).items()):
                try:
                    count = int(raw or 0)
                except (TypeError, ValueError):
                    continue
                if count <= 0:
                    continue
                if self.is_equipment_item(name):
                    continue  # 旧存档按名称堆叠的装备单独处理
                if self.is_pill(name):
                    continue
                price = self._unit_price(name)
                if threshold > 0 and price >= threshold:
                    plan["kept"].append({"name": name, "count": count, "price": price})
                    continue
                unit = self._recycle_value(name, "一般")
                plan["stacks"].append(
                    {"name": name, "count": count, "unit": unit, "where": where}
                )
                plan["total"] += unit * count

        if include_equipment:
            for where in ("storage", "warehouse") if include_warehouse else ("storage",):
                for inst in self.instance_manager.list_instances(player, where):
                    name = inst.get("name", "")
                    if not name:
                        continue
                    quality = str(inst.get("quality") or "一般")
                    value = self._recycle_value(name, quality, inst.get("affixes"))
                    plan["equipment"].append(
                        {
                            "id": int(inst.get("id", 0) or 0),
                            "name": name,
                            "quality": quality,
                            "unit": value,
                            "where": where,
                        }
                    )
                    plan["total"] += value

        if include_pills:
            for key, raw in sorted((player.get_pills_inventory() or {}).items()):
                try:
                    count = int(raw or 0)
                except (TypeError, ValueError):
                    continue
                if count <= 0:
                    continue
                base, quality = (
                    (key.rsplit("|", 1) + ["一般"])[:2] if "|" in key else (key, "一般")
                )
                price = self._unit_price(base)
                if threshold > 0 and price >= threshold:
                    plan["kept"].append({"name": base, "count": count, "price": price})
                    continue
                unit = self._recycle_value(base, quality)
                plan["pills"].append(
                    {"key": key, "name": base, "quality": quality, "count": count, "unit": unit}
                )
                plan["total"] += unit * count

        return plan

    async def apply_bulk_recycle(self, player: Player, plan: dict) -> Tuple[bool, str]:
        """执行一键回收（单事务）。以实际持有量为准，计划与实物不符时只扣实际数量。"""
        if not plan or plan.get("total", 0) <= 0:
            return False, "没有可回收的物品"
        orig_player = player
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(player.user_id)
            if not player:
                await self.db.conn.rollback()
                return False, "玩家数据不存在，请重新踏入仙途"

            total_gold = 0
            kinds = 0
            count_total = 0
            ring_items = player.get_storage_ring_items()
            warehouse_items = player.get_warehouse_items()
            pills = player.get_pills_inventory()

            for entry in plan.get("stacks", []):
                name = entry.get("name", "")
                want = int(entry.get("count", 0) or 0)
                unit = int(entry.get("unit", 0) or 0)
                if want <= 0 or unit <= 0:
                    continue
                # 严格按预览记录的来源容器扣减，避免把预览范围外（如仓库）的同名物品一并回收；
                # 兼容缺少 where 字段的旧预览，退回原有的 储物戒→仓库 顺序
                where = entry.get("where", "")
                if where == "storage":
                    containers = (ring_items,)
                elif where == "warehouse":
                    containers = (warehouse_items,)
                else:
                    containers = (ring_items, warehouse_items)
                got = 0
                for container in containers:
                    if got >= want:
                        break
                    try:
                        have = int(container.get(name, 0) or 0)
                    except (TypeError, ValueError):
                        have = 0
                    if have <= 0:
                        continue
                    take = min(have, want - got)
                    if take >= have:
                        container.pop(name, None)
                    else:
                        container[name] = have - take
                    got += take
                if got > 0:
                    total_gold += unit * got
                    count_total += got
                    kinds += 1

            for entry in plan.get("pills", []):
                key = entry.get("key", "")
                want = int(entry.get("count", 0) or 0)
                unit = int(entry.get("unit", 0) or 0)
                try:
                    have = int(pills.get(key, 0) or 0)
                except (TypeError, ValueError):
                    have = 0
                take = min(have, want)
                if take <= 0 or unit <= 0:
                    continue
                if take >= have:
                    pills.pop(key, None)
                else:
                    pills[key] = have - take
                total_gold += unit * take
                count_total += take
                kinds += 1

            equipment_sold = 0
            for entry in plan.get("equipment", []):
                inst_id = int(entry.get("id", 0) or 0)
                unit = int(entry.get("unit", 0) or 0)
                inst = self.instance_manager.get_instance(player, inst_id)
                if not inst or unit <= 0:
                    continue
                # 预览后实例被挪去别的容器（储物戒↔仓库）时跳过，严格按预览来源回收
                planned_where = entry.get("where", "")
                if planned_where and str(inst.get("location", "")) != planned_where:
                    continue
                if self.instance_manager.remove(player, inst_id):
                    total_gold += unit
                    equipment_sold += 1

            if total_gold <= 0:
                await self.db.conn.rollback()
                return False, "没有可回收的物品（物品已变动，请重新发送 /一键回收）"

            player.gold += total_gold
            player.set_storage_ring_items(ring_items)
            player.set_warehouse_items(warehouse_items)
            player.set_pills_inventory(pills)
            orig_player.gold = player.gold
            orig_player.set_storage_ring_items(ring_items)
            orig_player.set_warehouse_items(warehouse_items)
            orig_player.set_pills_inventory(pills)
            orig_player.set_equipment_instances(player.get_equipment_instances())
            await self.db.update_player(player)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        parts = []
        if count_total:
            parts.append(f"{kinds} 种共 {count_total:,} 件")
        if equipment_sold:
            parts.append(f"装备 {equipment_sold} 件")
        return True, (
            f"♻️ 一键回收完成\n"
            f"━━━━━━━━━━━━━━━\n"
            f"已回收：{'、'.join(parts) if parts else '—'}\n"
            f"获得灵石：{total_gold:,}（按配置价值 60%）\n"
            f"当前灵石：{player.gold:,}"
        )

    def check_upgrade_requirement(self, player: Player, new_ring_name: str) -> Tuple[bool, str]:
        ring_config = self.get_storage_ring_config(new_ring_name)
        if not ring_config:
            return False, f"【{new_ring_name}】不是储物戒类型的物品"
        if ring_config.get("type") != "storage_ring":
            return False, f"【{new_ring_name}】不是储物戒类型的物品"

        required_level = ring_config.get("required_level_index", 0)
        if player.level_index < required_level:
            level_name = self._format_required_level(required_level)
            # 列出当前可更换的储物戒
            better = []
            for name, cfg in (self.config_manager.storage_rings_data or {}).items():
                c_req = cfg.get("required_level_index", 0)
                c_cap = cfg.get("capacity", 20)
                if player.level_index >= c_req and c_cap > self.get_ring_capacity(player.storage_ring):
                    better.append(f"【{name}】({cfg.get('rank', '')} · {c_cap}格)")
            better_text = "\n".join(f"  · {b}" for b in better[:6]) if better else "  · 暂无（先提升境界）"
            return False, (
                f"❌ 境界不足，无法更换！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"【{new_ring_name}】（{ring_config.get('rank', '')}）需要达到【{level_name}】（境界等级 {required_level}）\n"
                f"你当前境界等级：{player.level_index}（还差 {required_level - player.level_index} 级）\n"
                f"━━━━━━━━━━━━━━━\n"
                f"✅ 你当前可更换的储物戒：\n{better_text}"
            )

        current_capacity = self.get_ring_capacity(player.storage_ring)
        new_capacity = ring_config.get("capacity", 20)
        if new_capacity <= current_capacity:
            return False, f"【{new_ring_name}】容量（{new_capacity}）不高于当前储物戒（{current_capacity}），无法替换"
        return True, ""

    def _format_required_level(self, level_index: int) -> str:
        names = []
        if 0 <= level_index < len(self.config_manager.level_data):
            name = self.config_manager.level_data[level_index].get("level_name", "")
            if name:
                names.append(name)
        if 0 <= level_index < len(self.config_manager.body_level_data):
            name = self.config_manager.body_level_data[level_index].get("level_name", "")
            if name and name not in names:
                names.append(name)
        if not names:
            return f"境界{level_index}"
        return " / ".join(names)

    async def upgrade_ring(self, player: Player, new_ring_name: str) -> Tuple[bool, str]:
        can_upgrade, error_msg = self.check_upgrade_requirement(player, new_ring_name)
        if not can_upgrade:
            return False, error_msg

        ring_config = self.get_storage_ring_config(new_ring_name)
        old_ring = player.storage_ring
        old_capacity = self.get_ring_capacity(old_ring)
        new_capacity = ring_config.get("capacity", 20)
        
        price = ring_config.get("price", 0)
        if price > 0:
            if player.gold < price:
                return False, f"❌ 灵石不足！\n【{new_ring_name}】需要 {price:,} 灵石\n你当前拥有：{player.gold:,} 灵石"
            player.gold -= price

        player.storage_ring = new_ring_name
        await self.db.update_player(player)

        cost_msg = f"\n消耗灵石：{price:,}" if price > 0 else ""
        return True, (
            f"储物戒升级成功！\n"
            f"【{old_ring}】({old_capacity}容量) → 【{new_ring_name}】({new_capacity}容量)\n"
            f"品级：{ring_config.get('rank', '未知')}{cost_msg}"
        )

    def get_storage_ring_info(self, player: Player) -> dict:
        ring_config = self.get_storage_ring_config(player.storage_ring) or {}
        items = dict(player.get_storage_ring_items())
        for name, count in self.instance_manager.storage_counts(player).items():
            items[name] = int(items.get(name, 0) or 0) + count
        enchant = self.get_enchant_bonus(player)
        capacity = self.get_ring_capacity(player.storage_ring) + enchant
        used = self.get_used_slots(player)
        overflow = dict(player.get_overflow_items())
        for inst in self.instance_manager.list_instances(player, "overflow"):
            name = inst.get("name", "")
            if name:
                overflow[name] = int(overflow.get(name, 0) or 0) + 1

        return {
            "name": player.storage_ring,
            "rank": ring_config.get("rank", "未知"),
            "description": ring_config.get("description", ""),
            "capacity": capacity,
            "used": used,
            "available": capacity - used,
            "enchant": enchant,
            "items": items,
            "overflow": overflow
        }

    def get_all_storage_rings(self) -> List[dict]:
        rings = []
        for name, config in self.config_manager.storage_rings_data.items():
            rings.append({
                "name": name,
                "rank": config.get("rank", ""),
                "capacity": config.get("capacity", 20),
                "required_level_index": config.get("required_level_index", 0),
                "price": config.get("price", 0),
                "description": config.get("description", "")
            })
        rings.sort(key=lambda x: x["capacity"])
        return rings

    @staticmethod
    def ring_items(player: Player) -> dict:
        """储物戒内物品的只读快照。

        仓库里的东西不算「持有」：炼丹、制符、强化等消耗路径只认储物戒，
        玩家必须先用 /一键取出 搬到戒指里。
        """
        snapshot: Dict[str, int] = {}
        for name, raw in (player.get_storage_ring_items() or {}).items():
            try:
                amount = int(raw or 0)
            except (TypeError, ValueError):
                continue
            if amount > 0:
                snapshot[name] = amount
        return snapshot

    def get_item_count(self, player: Player, item_name: str) -> int:
        items = player.get_storage_ring_items()
        count = int(items.get(item_name, 0) or 0)
        if self.is_equipment_item(item_name):
            count += self.instance_manager.count_storage(player, item_name)
        return count

    def has_item(self, player: Player, item_name: str, count: int = 1) -> bool:
        return self.get_item_count(player, item_name) >= count