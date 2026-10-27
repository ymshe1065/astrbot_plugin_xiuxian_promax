# core/storage_ring_manager.py

import json
from typing import TYPE_CHECKING, Optional, Tuple, List, Dict
from ..models import Player

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
        """获取已使用的容量数"""
        items = player.get_storage_ring_items()
        used_volume = 0
        for item_name, count in items.items():
            used_volume += self._get_item_volume(item_name) * count
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

    def is_pill(self, item_name: str) -> bool:
        return self.config_manager.is_pill(item_name)

    def can_store_item(self, item_name: str) -> Tuple[bool, str]:
        if self.is_pill(item_name):
            return False, f"【{item_name}】是丹药，不能存入储物戒（请使用丹药背包）"
        if item_name in self.config_manager.storage_rings_data:
            return False, f"【{item_name}】是储物戒，不能存入另一个储物戒"
        return True, ""

    async def store_item(self, player: Player, item_name: str, count: int = 1, silent: bool = False, external_transaction: bool = False) -> Tuple[bool, str]:
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
                if not external_transaction:
                    await self.db.conn.rollback()
                capacity = self.get_total_capacity(player)
                return False, f"储物戒容量不足！需要 {required_volume}，仅剩 {available}（上限{capacity}）"

            items[item_name] = items.get(item_name, 0) + count
            player.set_storage_ring_items(items)
            orig_player.set_storage_ring_items(items)  # 同步调用方对象

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

    async def retrieve_item(self, player: Player, item_name: str, count: int = 1) -> Tuple[bool, str]:
        orig_player = player  # 同步调用方对象，防止其后续 update_player 回滚本次取出
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            player = await self.db.get_player_by_id(player.user_id)
            if not player:
                await self.db.conn.rollback()
                return False, "玩家数据不存在，请重新踏入仙途"
            items = player.get_storage_ring_items()

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

            capacity = self.get_ring_capacity(player.storage_ring)
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

            capacity = self.get_ring_capacity(player.storage_ring)
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
        items = player.get_storage_ring_items()
        enchant = self.get_enchant_bonus(player)
        capacity = self.get_ring_capacity(player.storage_ring) + enchant
        used = self.get_used_slots(player)

        return {
            "name": player.storage_ring,
            "rank": ring_config.get("rank", "未知"),
            "description": ring_config.get("description", ""),
            "capacity": capacity,
            "used": used,
            "available": capacity - used,
            "enchant": enchant,
            "items": items
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
        return items.get(item_name, 0)

    def has_item(self, player: Player, item_name: str, count: int = 1) -> bool:
        return self.get_item_count(player, item_name) >= count