# core/warehouse_manager.py
"""洞天仓库（藏宝阁）：开通、升级、存取。

设计要点：
- 等级与物品都挂在 ``players`` 上（``warehouse_level`` / ``warehouse_items``），
  不挂 ``blessed_lands``——洞天进阶是 DELETE + INSERT 重建整行，挂那张表会丢数据。
- 等级上限由**当前洞天品质**决定，运行时不落库。
- 容量单位与储物戒一致：可堆叠物品每种 1 格（数量不限），装备实例按体积逐件占格。
"""

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from ..models import Player
from .item_instance_manager import ItemInstanceManager
from .storage_ring_manager import StorageRingManager

if TYPE_CHECKING:
    from ..data import DataBase
    from ..config_manager import ConfigManager

__all__ = ["WarehouseManager", "WAREHOUSE_FURNITURE"]

# 洞天装修入口名称（与 blessed_land_manager.FURNITURES 对齐）
WAREHOUSE_FURNITURE = "藏宝阁"


class WarehouseManager:
    """洞天仓库管理器"""

    OPEN_COST = 30000              # 开通费用
    SLOTS_PER_LEVEL = 50           # 每级容量
    BASE_UPGRADE_COST = 15000      # 升到 2 级的费用
    UPGRADE_GROWTH = 1.15          # 每级费用增长

    # 洞天品质 → 仓库等级上限
    MAX_LEVEL_BY_LAND = {1: 5, 2: 10, 3: 15, 4: 20, 5: 30}

    def __init__(
        self,
        db: "DataBase",
        config_manager: "ConfigManager",
        storage_ring_manager: Optional[StorageRingManager] = None,
    ):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_manager = storage_ring_manager or StorageRingManager(db, config_manager)
        self.instance_manager = ItemInstanceManager(db, config_manager)

    # ===== 等级与容量 =====

    def get_level(self, player: Player) -> int:
        try:
            return max(0, int(getattr(player, "warehouse_level", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def is_open(self, player: Player) -> bool:
        return self.get_level(player) > 0

    async def get_land_type(self, player: Player) -> int:
        """读取玩家当前洞天品质（1-5），无洞天返回 0。"""
        async with self.db.conn.execute(
            "SELECT land_type FROM blessed_lands WHERE user_id = ?", (player.user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return 0
        try:
            return int(row[0] or 0)
        except (TypeError, ValueError, IndexError):
            return 0

    async def get_max_level(self, player: Player) -> int:
        land_type = await self.get_land_type(player)
        return self.MAX_LEVEL_BY_LAND.get(land_type, 0)

    def get_capacity(self, player: Player) -> int:
        return self.get_level(player) * self.SLOTS_PER_LEVEL

    def upgrade_cost(self, current_level: int) -> int:
        """升到下一级的费用（取整到千）。"""
        if current_level < 1:
            return self.OPEN_COST
        cost = self.BASE_UPGRADE_COST * (self.UPGRADE_GROWTH ** (current_level - 1))
        return int(round(cost, -3))

    def get_items(self, player: Player) -> Dict[str, int]:
        items = player.get_warehouse_items()
        cleaned: Dict[str, int] = {}
        for name, count in (items or {}).items():
            try:
                amount = int(count or 0)
            except (TypeError, ValueError):
                continue
            if amount > 0:
                cleaned[name] = amount
        return cleaned

    def get_used_slots(self, player: Player) -> int:
        used = 0
        for name in self.get_items(player):
            if self.storage_ring_manager.is_equipment_item(name):
                used += self.storage_ring_manager._get_item_volume(name)
            else:
                used += 1
        for inst in self.instance_manager.list_instances(player, "warehouse"):
            used += self.storage_ring_manager._get_item_volume(inst.get("name", ""))
        return used

    def get_available_slots(self, player: Player) -> int:
        return max(0, self.get_capacity(player) - self.get_used_slots(player))

    # ===== 开通与升级 =====

    async def open_warehouse(self, player: Player) -> Tuple[bool, str]:
        if self.get_level(player) > 0:
            return False, "❌ 你已经开通了【藏宝阁】，无需重复修建。"
        max_level = await self.get_max_level(player)
        if max_level <= 0:
            return False, "❌ 需要先拥有洞天，才能在洞天中修建【藏宝阁】。使用 /购买洞天 1。"

        if player.gold < self.OPEN_COST:
            return False, (
                f"❌ 灵石不足！修建【藏宝阁】需要 {self.OPEN_COST:,} 灵石，"
                f"你当前拥有 {player.gold:,}。"
            )

        player.gold -= self.OPEN_COST
        player.warehouse_level = 1
        await self.db.update_player(player)
        return True, (
            f"🏛️ 【藏宝阁】修建完成！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"等级：Lv.1｜容量：{self.SLOTS_PER_LEVEL} 格\n"
            f"花费：{self.OPEN_COST:,} 灵石\n"
            f"当前洞天可支持到 Lv.{max_level}（{max_level * self.SLOTS_PER_LEVEL} 格）\n"
            f"━━━━━━━━━━━━━━━\n"
            f"存入：/一键转移　取回：/一键取回　查看：/仓库\n"
            f"💡 洞天品质越高，可扩建的仓库越大"
        )

    async def upgrade_warehouse(self, player: Player, times: int = 1) -> Tuple[bool, str]:
        level = self.get_level(player)
        if level <= 0:
            return False, "❌ 你还没有【藏宝阁】。使用 /洞天装修 藏宝阁 修建。"
        max_level = await self.get_max_level(player)
        if level >= max_level:
            return False, (
                f"❌ 【藏宝阁】已达当前洞天上限 Lv.{max_level}。\n"
                f"💡 进阶洞天可提升仓库上限。"
            )

        times = max(1, int(times or 1))
        target = min(max_level, level + times)
        upgraded = 0
        spent = 0
        for _lv in range(level, target):
            cost = self.upgrade_cost(_lv)
            if player.gold < cost:
                break
            player.gold -= cost
            spent += cost
            upgraded += 1
            player.warehouse_level = _lv + 1

        if upgraded <= 0:
            next_cost = self.upgrade_cost(level)
            return False, (
                f"❌ 灵石不足！升级到 Lv.{level + 1} 需要 {next_cost:,} 灵石，"
                f"你当前拥有 {player.gold:,}。"
            )

        await self.db.update_player(player)
        new_level = self.get_level(player)
        tail = ""
        if new_level < target:
            tail = f"\n⚠️ 灵石不足，停在 Lv.{new_level}（还需 {self.upgrade_cost(new_level):,} 灵石可继续）"
        elif new_level >= max_level:
            tail = f"\n🏆 已达当前洞天可支持的满级 Lv.{max_level}，进阶洞天可继续扩建。"
        return True, (
            f"🏛️ 【藏宝阁】升级完成！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"等级：Lv.{level} → Lv.{new_level}（+{upgraded} 级）\n"
            f"容量：{self.get_capacity(player)} 格\n"
            f"花费：{spent:,} 灵石\n"
            f"剩余灵石：{player.gold:,}{tail}"
        )

    # ===== 存取 =====

    def category_of(self, item_name: str) -> str:
        """粗分类：装备 / 符箓 / 丹方 / 材料。"""
        if self.storage_ring_manager.is_equipment_item(item_name):
            return "装备"
        cfg = self.config_manager.items_data.get(item_name) or {}
        raw_type = str(cfg.get("type", "") or "")
        if raw_type == "符箓" or "符" in item_name:
            return "符箓"
        if raw_type == "丹方" or "丹方" in item_name:
            return "丹方"
        return "材料"

    async def transfer_in(self, player: Player, category: str = "") -> Tuple[bool, str]:
        """储物戒 → 仓库。仓库放不下的留在储物戒并报告。"""
        if not self.is_open(player):
            return False, "❌ 你还没有【藏宝阁】。使用 /洞天装修 藏宝阁 修建。"
        return await self._transfer(player, category, to_warehouse=True)

    async def transfer_out(self, player: Player, category: str = "") -> Tuple[bool, str]:
        """仓库 → 储物戒。"""
        if not self.is_open(player):
            return False, "❌ 你还没有【藏宝阁】。"
        return await self._transfer(player, category, to_warehouse=False)

    async def _transfer(
        self, player: Player, category: str, to_warehouse: bool
    ) -> Tuple[bool, str]:
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                return False, "玩家数据不存在，请重新踏入仙途"

            ring_items = fresh.get_storage_ring_items()
            wh_items = self.get_items(fresh)
            src, dst = (ring_items, wh_items) if to_warehouse else (wh_items, ring_items)

            moved: List[str] = []
            skipped: List[str] = []
            moved_equipment = 0

            # 可堆叠物品
            for name in sorted(src.keys()):
                if category and self.category_of(name) != category:
                    continue
                if self.storage_ring_manager.is_equipment_item(name):
                    continue
                count = int(src.get(name, 0) or 0)
                if count <= 0:
                    continue

                if to_warehouse:
                    # 仓库已有同名栈或还有空位即可整批转入
                    fits = int(dst.get(name, 0) or 0) > 0 or self.get_available_slots(fresh) >= 1
                else:
                    fits = self.storage_ring_manager.can_accept(fresh, name, count)
                if not fits:
                    skipped.append(f"{name}×{count}")
                    continue

                del src[name]
                dst[name] = int(dst.get(name, 0) or 0) + count
                moved.append(f"{name}×{count}")
                # 立即写回，后续容量判定才看得到本轮已占用的格数
                fresh.set_storage_ring_items(ring_items)
                fresh.set_warehouse_items(wh_items)

            # 装备实例：源容器里的实例整件搬走（保留品质与词条）
            src_loc, dst_loc = ("storage", "warehouse") if to_warehouse else ("warehouse", "storage")
            for inst in self.instance_manager.list_instances(fresh, src_loc):
                name = inst.get("name", "")
                if category and self.category_of(name) != category:
                    continue
                if to_warehouse:
                    if self.get_available_slots(fresh) < self.storage_ring_manager._get_item_volume(name):
                        skipped.append(f"{name}×1")
                        continue
                else:
                    if not self.storage_ring_manager.can_accept(fresh, name, 1):
                        skipped.append(f"{name}×1")
                        continue
                if self.instance_manager.move(fresh, int(inst.get("id", 0) or 0), dst_loc):
                    moved_equipment += 1
                    moved.append(f"{name}·{inst.get('quality') or '一般'}")

            fresh.set_storage_ring_items(ring_items)
            fresh.set_warehouse_items(wh_items)
            player.set_storage_ring_items(ring_items)
            player.set_warehouse_items(wh_items)
            player.set_equipment_instances(fresh.get_equipment_instances())
            await self.db.update_player(fresh)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        direction = "储物戒 → 仓库" if to_warehouse else "仓库 → 储物戒"
        if not moved:
            head = "📦 没有可转移的物品" + (f"（分类：{category}）" if category else "")
            if skipped:
                head += f"\n⚠️ 目标空间不足，未转移：" + "、".join(skipped[:10])
            return False, head

        lines = [f"📦 一键转移完成（{direction}）", "━━━━━━━━━━━━━━━"]
        lines.append(f"共 {len(moved)} 项：" + "、".join(moved[:12]) + ("…" if len(moved) > 12 else ""))
        if moved_equipment:
            lines.append(f"其中装备 {moved_equipment} 件")
        lines.append(
            f"储物戒：{self.storage_ring_manager.get_used_slots(fresh)}/"
            f"{self.storage_ring_manager.get_total_capacity(fresh)}格"
            f"｜仓库：{self.get_used_slots(fresh)}/{self.get_capacity(fresh)}格"
        )
        if skipped:
            lines.append(f"⚠️ 空间不足未转移 {len(skipped)} 项：" + "、".join(skipped[:10]))
        return True, "\n".join(lines)

    # ===== 展示 =====

    async def get_warehouse_info(self, player: Player) -> Dict:
        level = self.get_level(player)
        max_level = await self.get_max_level(player)
        land_type = await self.get_land_type(player)
        items = self.get_items(player)
        equipment = self.instance_manager.list_instances(player, "warehouse")
        return {
            "level": level,
            "max_level": max_level,
            "land_type": land_type,
            "capacity": self.get_capacity(player),
            "used": self.get_used_slots(player),
            "available": self.get_available_slots(player),
            "next_cost": self.upgrade_cost(level) if level < max_level else 0,
            "items": items,
            "equipment": equipment,
        }
