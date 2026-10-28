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
    """储物戒管理器 - 容量制（支持空间附魔扩容）"""

    PILL_TYPES = ["丹药"]

    ENCHANT_ITEM = "空间石"          # 附魔材料
    ENCHANT_BONUS_PER_STONE = 5      # 每颗空间石 +5 容量
    ENCHANT_MAX = 50                 # 附魔上限（+50 容量）

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
        """空间附魔：消耗空间石永久扩充储物戒容量"""
        if stone_count <= 0:
            return False, "❌ 请输入有效的空间石数量。"

        current_enchant = self.get_enchant_bonus(player)
        if current_enchant >= self.ENCHANT_MAX:
            return False, f"❌ 空间附魔已达上限（+{self.ENCHANT_MAX}容量）。"

        stones_usable = min(stone_count, (self.ENCHANT_MAX - current_enchant) // self.ENCHANT_BONUS_PER_STONE)
        if stones_usable <= 0:
            return False, f"❌ 剩余可附魔空间不足 {self.ENCHANT_BONUS_PER_STONE} 点，无法继续。"

        need = stones_usable
        if not self.has_item(player, self.ENCHANT_ITEM, need):
            have = self.get_item_count(player, self.ENCHANT_ITEM)
            return False, f"❌ 空间石不足（拥有 {have}，需要 {need}）。"

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

    def get_used_slots(self, player: Player) -> int:
        """获取已使用的容量数（含装备实例）"""
        items = player.get_storage_ring_items()
        used_volume = 0
        for item_name, count in items.items():
            used_volume += self._get_item_volume(item_name) * int(count or 0)
        for inst in self.instance_manager.list_instances(player, "storage"):
            used_volume += self._get_item_volume(inst.get("name", ""))
        return used_volume

    def get_available_slots(self, player: Player) -> int:
        capacity = self.get_total_capacity(player)
        used = self.get_used_slots(player)
        return max(0, capacity - used)

    def get_space_warning(self, player: Player) -> Optional[str]:
        available = self.get_available_slots(player)
        capacity = self.get_total_capacity(player)
        used = self.get_used_slots(player)

        if available <= 0:
            return f"⚠️ 储物戒已满！({used}/{capacity}容量)"
        elif available <= capacity * 0.2:
            return f"⚠️ 储物戒空间不足！仅剩{available}容量({used}/{capacity}容量)"
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
            required_volume = item_volume * count
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
                return False, f"储物戒容量不足！需要 {required_volume}，仅剩 {available}（上限{capacity}）"

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
                return True, f"已将【{item_name}】x{count} 存入储物戒（{used}/{capacity}容量）"

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
            msg = f"已将【{item_name}】x{count} 存入储物戒（{used}/{capacity}容量）"
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
                fit = min(count, available // max(1, item_volume))
                if fit > 0:
                    # 兼容旧格式：旧溢出记录只有名称和数量，无法恢复品质
                    if self.is_equipment_item(item_name):
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
                    return True, f"已从储物戒取出【{item_name}】x{count}（{used}/{capacity}容量）"
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
                    return True, f"已从储物戒取出【{item_name}】x{count}"
                await self.db.conn.rollback()
                return False, f"储物戒中【{item_name}】数量不足（当前：{len(insts) + legacy}个）"

            if item_name not in items:
                await self.db.conn.rollback()
                return False, f"储物戒中没有【{item_name}】"

            current_count = items[item_name]
            if count > current_count:
                await self.db.conn.rollback()
                return False, f"储物戒中【{item_name}】数量不足（当前：{current_count}个）"

            if count >= current_count:
                del items[item_name]
            else:
                items[item_name] = current_count - count

            player.set_storage_ring_items(items)
            orig_player.set_storage_ring_items(items)  # 同步调用方对象
            await self.db.update_player(player)
            await self.db.conn.commit()

            capacity = self.get_total_capacity(player)
            used = self.get_used_slots(player)
            return True, f"已从储物戒取出【{item_name}】x{count}（{used}/{capacity}容量）"
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
                    return True, f"已丢弃【{item_name}】x{count}（{used}/{capacity}容量）"
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
                    return True, f"已丢弃【{item_name}】x{count}"
                await self.db.conn.rollback()
                return False, f"储物戒中【{item_name}】数量不足（当前：{len(insts) + legacy}个）"

            if item_name not in items:
                await self.db.conn.rollback()
                return False, f"储物戒中没有【{item_name}】"

            current_count = items[item_name]
            if count > current_count:
                await self.db.conn.rollback()
                return False, f"储物戒中【{item_name}】数量不足（当前：{current_count}个）"

            if count >= current_count:
                del items[item_name]
                discard_count = current_count
            else:
                items[item_name] = current_count - count
                discard_count = count

            player.set_storage_ring_items(items)
            orig_player.set_storage_ring_items(items)  # 同步调用方对象
            await self.db.update_player(player)
            await self.db.conn.commit()

            capacity = self.get_total_capacity(player)
            used = self.get_used_slots(player)
            return True, f"已丢弃【{item_name}】x{discard_count}（{used}/{capacity}容量）"
        except Exception:
            await self.db.conn.rollback()
            raise

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

    def get_item_count(self, player: Player, item_name: str) -> int:
        items = player.get_storage_ring_items()
        count = int(items.get(item_name, 0) or 0)
        if self.is_equipment_item(item_name):
            count += self.instance_manager.count_storage(player, item_name)
        return count

    def has_item(self, player: Player, item_name: str, count: int = 1) -> bool:
        return self.get_item_count(player, item_name) >= count