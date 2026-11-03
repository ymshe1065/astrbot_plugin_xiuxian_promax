# handlers/warehouse_handler.py
"""洞天仓库与批量操作：/仓库 /升级仓库 /一键转移 /一键取回 /一键回收。"""

import time
from typing import Dict, Tuple

from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from ..core.storage_ring_manager import StorageRingManager
from ..core.warehouse_manager import WarehouseManager
from ..data import DataBase
from ..config_manager import ConfigManager
from ..models import Player
from .utils import player_required

CMD_WAREHOUSE = "仓库"
CMD_UPGRADE_WAREHOUSE = "升级仓库"
CMD_TRANSFER_IN = "一键转移"
CMD_TRANSFER_OUT = "一键取回"
CMD_BULK_RECYCLE = "一键回收"

CATEGORIES = ("材料", "符箓", "丹方", "装备")
RECYCLE_TTL = 60  # 预览有效期（秒）

# 待确认的回收清单：user_id -> (生成时间, plan)
_PENDING_RECYCLE: Dict[str, Tuple[float, dict]] = {}

__all__ = ["WarehouseHandler"]


def _parse_category(raw: str) -> Tuple[str, bool]:
    """从参数里解析分类；返回 (分类, 是否合法)。空表示全部。"""
    text = (raw or "").strip()
    if not text:
        return "", True
    if text in CATEGORIES:
        return text, True
    return "", False


class WarehouseHandler:
    def __init__(self, db: DataBase, config_manager: ConfigManager):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_manager = StorageRingManager(db, config_manager)
        self.warehouse_manager = WarehouseManager(
            db, config_manager, self.storage_ring_manager
        )

    # ===== /仓库 =====

    @player_required
    async def handle_warehouse(self, player: Player, event: AstrMessageEvent):
        info = await self.warehouse_manager.get_warehouse_info(player)
        if info["level"] <= 0:
            yield event.plain_result(
                "🏛️ 你还没有【藏宝阁】\n"
                "━━━━━━━━━━━━━━━\n"
                f"修建费用：{WarehouseManager.OPEN_COST:,} 灵石\n"
                f"初始容量：{WarehouseManager.SLOTS_PER_LEVEL} 格\n"
                "用途：把材料/符箓/装备存进洞天，给储物戒腾地方\n"
                "━━━━━━━━━━━━━━━\n"
                "💡 修建：/洞天装修 藏宝阁"
            )
            return

        lines = [
            f"🏛️ 藏宝阁 Lv.{info['level']}／上限 Lv.{info['max_level']}\n",
            f"容量：{info['used']}/{info['capacity']}格（剩余 {info['available']}）\n",
            "━━━━━━━━━━━━━━━\n",
        ]

        items = info["items"]
        equipment = info["equipment"]
        if items:
            by_cat: Dict[str, list] = {}
            for name, count in items.items():
                cat = self.warehouse_manager.category_of(name)
                by_cat.setdefault(cat, []).append((name, count))
            for cat in CATEGORIES:
                rows = by_cat.get(cat)
                if not rows:
                    continue
                lines.append(f"【{cat}】\n")
                for name, count in rows[:12]:
                    lines.append(f"  · {name}×{count}\n")
                if len(rows) > 12:
                    lines.append(f"  · …共 {len(rows)} 种\n")
        else:
            lines.append("【存储物品】空\n")

        if equipment:
            lines.append(f"\n【装备实例】{len(equipment)} 件\n")
            for inst in equipment[:8]:
                lines.append(f"  · {inst.get('name')}·{inst.get('quality') or '一般'}\n")
            if len(equipment) > 8:
                lines.append("  · …\n")

        lines.append("\n━━━━━━━━━━━━━━━\n")
        if info["land_type"] <= 0:
            lines.append("⚠️ 你当前没有洞天，仓库暂不可扩建（物品仍可取回）\n")
            lines.append("💡 先 /购买洞天 1 恢复洞天\n")
        elif info["level"] < info["max_level"]:
            lines.append(f"升级：/{CMD_UPGRADE_WAREHOUSE}（下一级 {info['next_cost']:,} 灵石 · +{WarehouseManager.SLOTS_PER_LEVEL} 格）\n")
        else:
            lines.append("🏆 已达当前洞天上限，进阶洞天可继续扩建\n")
        lines.append(f"存入：/{CMD_TRANSFER_IN}　取回：/{CMD_TRANSFER_OUT}\n")
        lines.append(f"详情：/{CMD_WAREHOUSE}")

        yield event.plain_result("".join(lines))

    # ===== /升级仓库 =====

    @player_required
    async def handle_upgrade_warehouse(self, player: Player, event: AstrMessageEvent, args: str = ""):
        text = (args or "").strip().lower()
        if text in ("all", "max", "满级", "全部", "一键"):
            times = 99
        elif text.isdigit():
            times = max(1, int(text))
        elif not text:
            times = 1
        else:
            yield event.plain_result(
                f"用法：/{CMD_UPGRADE_WAREHOUSE} [次数|all]\n"
                f"示例：/{CMD_UPGRADE_WAREHOUSE} 3　/{CMD_UPGRADE_WAREHOUSE} all"
            )
            return

        ok, msg = await self.warehouse_manager.upgrade_warehouse(player, times)
        yield event.plain_result(("🏛️ " if ok else "❌ ") + msg)

    # ===== /一键转移 与 /一键取回 =====

    @player_required
    async def handle_transfer_in(self, player: Player, event: AstrMessageEvent, args: str = ""):
        category, valid = _parse_category(args)
        if not valid:
            yield event.plain_result(
                f"用法：/{CMD_TRANSFER_IN} [分类]\n"
                f"分类：{'、'.join(CATEGORIES)}（留空 = 全部）\n"
                f"示例：/{CMD_TRANSFER_IN} 材料"
            )
            return
        ok, msg = await self.warehouse_manager.transfer_in(player, category)
        yield event.plain_result(msg)

    @player_required
    async def handle_transfer_out(self, player: Player, event: AstrMessageEvent, args: str = ""):
        category, valid = _parse_category(args)
        if not valid:
            yield event.plain_result(
                f"用法：/{CMD_TRANSFER_OUT} [分类]\n"
                f"分类：{'、'.join(CATEGORIES)}（留空 = 全部）"
            )
            return
        ok, msg = await self.warehouse_manager.transfer_out(player, category)
        yield event.plain_result(msg)

    # ===== /一键回收 =====

    @player_required
    async def handle_bulk_recycle(self, player: Player, event: AstrMessageEvent, args: str = ""):
        tokens = [t for t in (args or "").replace("　", " ").split() if t]
        confirm = any(t in ("确认", "确定", "confirm", "ok") for t in tokens)
        include_equipment = any(t in ("装备", "含装备") for t in tokens)
        include_warehouse = any(t in ("仓库", "含仓库") for t in tokens)
        include_pills = any(t in ("丹药", "含丹药") for t in tokens)

        unknown = [
            t for t in tokens
            if t not in ("确认", "确定", "confirm", "ok", "装备", "含装备", "仓库", "含仓库", "丹药", "含丹药")
        ]
        if unknown:
            yield event.plain_result(self._usage())
            return

        if confirm:
            ok, msg = await self._confirm_recycle(player)
            yield event.plain_result(msg)
            return

        # 预览
        plan = self.storage_ring_manager.plan_bulk_recycle(
            player,
            include_equipment=include_equipment,
            include_warehouse=include_warehouse,
            include_pills=include_pills,
        )
        if plan["total"] <= 0 and not plan["kept"]:
            yield event.plain_result("♻️ 没有可回收的物品。")
            return

        _PENDING_RECYCLE[player.user_id] = (
            time.time(),
            {
                "plan": plan,
                "include_equipment": include_equipment,
                "include_warehouse": include_warehouse,
                "include_pills": include_pills,
            },
        )

        lines = ["♻️ 一键回收 · 预览", "━━━━━━━━━━━━━━━"]
        shown = 0
        for entry in sorted(plan["stacks"], key=lambda x: -x["unit"] * x["count"]):
            if shown >= 12:
                break
            lines.append(
                f"  · {entry['name']}×{entry['count']}（{entry['unit'] * entry['count']:,}）\n"
            )
            shown += 1
        for entry in plan["equipment"][:6]:
            lines.append(f"  · {entry['name']}·{entry['quality']}（{entry['unit']:,}）\n")
            shown += 1
        for entry in plan["pills"][:6]:
            lines.append(
                f"  · {entry['name']}·{entry['quality']}×{entry['count']}（{entry['unit'] * entry['count']:,}）\n"
            )
            shown += 1
        total_kinds = len(plan["stacks"]) + len(plan["equipment"]) + len(plan["pills"])
        if total_kinds > shown:
            lines.append(f"  · …共 {total_kinds} 项\n")

        lines.append("━━━━━━━━━━━━━━━\n")
        lines.append(f"预计获得：{plan['total']:,} 灵石（配置价值 60%）\n")
        if plan["kept"]:
            kept_text = "、".join(
                f"{k['name']}×{k['count']}" for k in plan["kept"][:6]
            )
            more = "…" if len(plan["kept"]) > 6 else ""
            lines.append(f"⚠️ 已保留贵重物品（单价≥{StorageRingManager.EXPENSIVE_THRESHOLD:,}）：{kept_text}{more}\n")
        if not include_equipment:
            lines.append("（装备未计入，如需回收请加「装备」）\n")
        if not include_warehouse:
            lines.append("（仅回收储物戒，如需含仓库请加「仓库」）\n")
        lines.append("\n⚠️ 回收不可撤销，确认请输入：\n")
        cmd = f"/{CMD_BULK_RECYCLE} 确认"
        for t in tokens:
            cmd += f" {t}"
        lines.append(cmd)
        yield event.plain_result("".join(lines))

    async def _confirm_recycle(self, player: Player) -> Tuple[bool, str]:
        pending = _PENDING_RECYCLE.pop(player.user_id, None)
        if not pending:
            return False, (
                f"❌ 没有待确认的回收清单，或清单已过期（{RECYCLE_TTL} 秒）。\n"
                f"请重新发送 /{CMD_BULK_RECYCLE} 生成预览。"
            )
        created_at, payload = pending
        if time.time() - created_at > RECYCLE_TTL:
            return False, f"❌ 回收清单已过期（{RECYCLE_TTL} 秒），请重新发送 /{CMD_BULK_RECYCLE}。"
        ok, msg = await self.storage_ring_manager.apply_bulk_recycle(player, payload["plan"])
        return ok, ("♻️ " if ok else "❌ ") + msg

    @staticmethod
    def _usage() -> str:
        return (
            "♻️ 一键回收\n"
            "━━━━━━━━━━━━━━━\n"
            "用法：/一键回收 [选项]\n"
            "  默认：回收储物戒中的材料/符箓/丹方（装备与贵重物品保留）\n"
            "  装备：连装备实例一起回收\n"
            "  仓库：连洞天仓库一起回收\n"
            "  丹药：连丹药背包一起回收\n"
            "  确认：执行上一次预览的回收\n"
            "示例：/一键回收 装备 仓库\n"
            "　　　/一键回收 确认 装备 仓库\n"
            f"⚠️ 单价 ≥{StorageRingManager.EXPENSIVE_THRESHOLD:,} 灵石的物品默认保留，请用 /回收 单独处理。"
        )
