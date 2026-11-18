# handlers/jewel_handler.py
"""珠宝指令：珠囊、镶嵌、拆卸与扩孔。"""

from typing import List, Optional, Tuple

from astrbot.api.event import AstrMessageEvent

from ..core.jewel_manager import JewelManager, JewelRules, MAX_SOCKETS
from ..core.storage_ring_manager import StorageRingManager
from ..data import DataBase
from ..models import Player
from .utils import player_required

__all__ = ["JewelHandler"]

CMD_JEWEL = "珠宝"
CMD_SOCKET = "镶嵌"
CMD_UNSOCKET = "拆卸"
CMD_EXPAND_SOCKET = "扩孔"


class JewelHandler:
    def __init__(self, db: DataBase, config_manager, storage_ring_manager=None):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_manager = storage_ring_manager or StorageRingManager(db, config_manager)
        self.jewel_manager = JewelManager(db, config_manager, self.storage_ring_manager)

    # ===== 装备定位 =====
    @staticmethod
    def _equipped_ids(player: Player) -> set:
        return {
            int(x) for x in (player.get_equipped_instances() or {}).values()
            if str(x).lstrip("-").isdigit()
        }

    def _all_instances(self, player: Player) -> List[dict]:
        return [x for x in player.get_equipment_instances() if isinstance(x, dict)]

    def _find_instance(self, player: Player, selector: str) -> Tuple[Optional[dict], List[dict], str]:
        """按实例编号或名称定位装备；返回 (唯一实例, 候选列表, 错误提示)。"""
        text = (selector or "").strip().lstrip("#")
        if not text:
            return None, [], "❌ 请指定装备名称或实例编号。"
        instances = self._all_instances(player)
        if text.isdigit():
            target_id = int(text)
            for inst in instances:
                if int(inst.get("id", 0) or 0) == target_id:
                    return inst, [inst], ""
            return None, [], f"❌ 未找到编号为 #{target_id} 的装备实例。"
        matched = [inst for inst in instances if str(inst.get("name") or "") == text]
        if not matched:
            return None, [], f"❌ 未找到装备【{text}】。"
        if len(matched) == 1:
            return matched[0], matched, ""
        equipped = self._equipped_ids(player)
        return None, matched, (
            f"⚠️ 检测到 {len(matched)} 件【{text}】，请改用实例编号："
            + "、".join(
                f"#{inst.get('id')}（{'已装备' if int(inst.get('id', 0) or 0) in equipped else inst.get('location', '储物戒')}）"
                for inst in matched
            )
        )

    @staticmethod
    def _instance_tag(player: Player, instance: dict) -> str:
        equipped = int(instance.get("id", 0) or 0) in JewelHandler._equipped_ids(player)
        return "已装备" if equipped else str(instance.get("location") or "储物戒")

    # ===== /珠宝 =====
    @player_required
    async def handle_jewel(self, player: Player, event: AstrMessageEvent, args: str = ""):
        text = (args or "").strip()
        if text:
            try:
                raw = (event.get_message_str() or "").strip().lstrip("/").strip()
                if raw.startswith(CMD_JEWEL):
                    text = raw[len(CMD_JEWEL):].strip() or text
            except Exception:
                pass
        if text.startswith("详情"):
            jewel_id = text[len("详情"):].strip().lstrip("#")
            gem = self.jewel_manager.get_jewel(player, jewel_id)
            if not gem:
                yield event.plain_result(f"❌ 珠囊中没有编号为 #{jewel_id} 的珠宝。")
                return
            yield event.plain_result(self._describe_jewel(gem))
            return

        inventory = self.jewel_manager.get_inventory(player)
        lines = ["💎 珠囊", "━━━━━━━━━━━━━━━"]
        if not inventory:
            lines.append("珠囊空空如也。")
            lines.append("💡 原石切割、黑市、百宝阁、拍卖会与世界 Boss 均有机会获得珠宝。")
        else:
            for gem in sorted(inventory, key=lambda x: int(x.get("id", 0) or 0)):
                category = JewelRules.category_label(str(gem.get("category") or ""))
                lines.append(
                    f"#{gem.get('id')} 【{gem.get('name')}·{gem.get('quality')}】"
                    f"（{category}珠宝 / {gem.get('rank', '')}）"
                )
                lines.append(f"   词条：{self.jewel_manager.describe_jewel(gem)}")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("🛠️ 已穿戴装备孔位")
        equipped = self._equipped_ids(player)
        gear_instances = [
            inst for inst in self._all_instances(player)
            if int(inst.get("id", 0) or 0) in equipped
        ]
        if not gear_instances:
            lines.append("  当前没有已穿戴装备。")
        else:
            for inst in gear_instances:
                lines.append(
                    f"  #{inst.get('id')} 【{inst.get('name')}·{inst.get('quality', '一般')}】"
                    f"{self.jewel_manager.describe_sockets(inst)}"
                )
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("💡 /镶嵌 <珠宝编号> <装备名称或实例编号>")
        lines.append("💡 /拆卸 <装备名称或实例编号> <孔位序号>｜/扩孔 <装备名称或实例编号>")
        yield event.plain_result("\n".join(lines))

    def _describe_jewel(self, gem: dict) -> str:
        category = JewelRules.category_label(str(gem.get("category") or ""))
        lines = [
            f"💎 【{gem.get('name')}·{gem.get('quality')}】（{category}珠宝 / {gem.get('rank', '')}）",
            "━━━━━━━━━━━━━━━",
            f"词条：{self.jewel_manager.describe_jewel(gem)}",
        ]
        if gem.get("description"):
            lines.append(f"描述：{gem['description']}")
        lines.append(f"参考价值：{int(gem.get('price') or 0):,} 灵石")
        need = "攻击类装备（武器/手套/戒指/护符/饰品）" if gem.get("category") == "attack" \
            else "防御类装备（防具/甲胄/头饰/靴子）"
        lines.append(f"可镶嵌：{need}")
        lines.append("💡 拆卸需消耗灵石，费用随珠宝品质提高。")
        return "\n".join(lines)

    # ===== /镶嵌 =====
    @player_required
    async def handle_socket(self, player: Player, event: AstrMessageEvent, args: str = ""):
        text = (args or "").strip()
        if not text:
            try:
                raw = (event.get_message_str() or "").strip().lstrip("/").strip()
                if raw.startswith(CMD_SOCKET):
                    text = raw[len(CMD_SOCKET):].strip()
            except Exception:
                pass
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：/镶嵌 <珠宝编号> <装备名称或实例编号>\n示例：/镶嵌 3 青锋剑")
            return
        jewel_id, selector = parts[0].lstrip("#"), parts[1].strip()
        gem = self.jewel_manager.get_jewel(player, jewel_id)
        if not gem:
            yield event.plain_result(f"❌ 珠囊中没有编号为 #{jewel_id} 的珠宝。")
            return
        target, candidates, error = self._find_instance(player, selector)
        if not target:
            yield event.plain_result(error)
            return
        ok, msg = self.jewel_manager.can_socket(player, target, gem)
        if not ok:
            yield event.plain_result(msg)
            return

        instances = self._all_instances(player)
        live_target = next(
            (x for x in instances if int(x.get("id", 0) or 0) == int(target.get("id", 0) or 0)),
            None,
        )
        if not live_target:
            yield event.plain_result("❌ 装备数据已变化，请重试。")
            return
        ok, msg = self.jewel_manager.socket_gem_into_instance(player, live_target, gem)
        if not ok:
            yield event.plain_result(msg)
            return
        player.set_equipment_instances(instances)
        await self.db.update_player(player)
        yield event.plain_result(msg)
        # 镶嵌会改变战斗面板，提示玩家重新查看
        yield event.plain_result("💡 珠宝词条已计入装备面板与战斗，可用 /我的装备 查看最新加成。")

    # ===== /拆卸 =====
    @player_required
    async def handle_unsocket(self, player: Player, event: AstrMessageEvent, args: str = ""):
        text = (args or "").strip()
        if not text:
            try:
                raw = (event.get_message_str() or "").strip().lstrip("/").strip()
                if raw.startswith(CMD_UNSOCKET):
                    text = raw[len(CMD_UNSOCKET):].strip()
            except Exception:
                pass
        parts = text.split()
        if len(parts) < 2:
            yield event.plain_result("用法：/拆卸 <装备名称或实例编号> <孔位序号>\n示例：/拆卸 青锋剑 1")
            return
        selector, index_text = " ".join(parts[:-1]).strip(), parts[-1]
        try:
            socket_index = int(index_text.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        except ValueError:
            yield event.plain_result("❌ 孔位序号必须是数字，例如：/拆卸 青锋剑 1")
            return
        target, _candidates, error = self._find_instance(player, selector)
        if not target:
            yield event.plain_result(error)
            return
        sockets = self.jewel_manager.sockets_of(target)
        if socket_index < 1 or socket_index > len(sockets):
            yield event.plain_result(f"❌ 【{target.get('name')}】只有 {len(sockets)} 个已镶嵌孔位。")
            return
        gem = sockets[socket_index - 1]
        from ..core.jewel_manager import UNSOCKET_COST
        cost = int(UNSOCKET_COST.get(str(gem.get("quality") or "一般"), 2000))
        if player.gold < cost:
            yield event.plain_result(
                f"❌ 拆卸【{gem.get('name')}·{gem.get('quality')}】需要 {cost:,} 灵石，"
                f"你当前只有 {player.gold:,}。"
            )
            return

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                yield event.plain_result("❌ 角色数据异常，请重新踏入仙途。")
                return
            target, _candidates, error = self._find_instance(fresh, selector)
            if not target:
                await self.db.conn.rollback()
                yield event.plain_result(error)
                return
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (cost, fresh.user_id, cost),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                yield event.plain_result(f"❌ 灵石不足！拆卸需要 {cost:,} 灵石。")
                return
            instances = self._all_instances(fresh)
            live_target = next(
                (x for x in instances if int(x.get("id", 0) or 0) == int(target.get("id", 0) or 0)),
                None,
            )
            ok, msg, _cost = self.jewel_manager.unsocket_gem(fresh, live_target, socket_index)
            if not ok:
                await self.db.conn.rollback()
                yield event.plain_result(msg)
                return
            fresh.set_equipment_instances(instances)
            fresh.gold -= cost
            await self.db.update_player(fresh, commit=False)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise
        player.gold = fresh.gold
        player.set_equipment_instances(fresh.get_equipment_instances())
        player.set_jewel_inventory(fresh.get_jewel_inventory())
        yield event.plain_result(msg)
        yield event.plain_result(f"💰 拆卸费用：{cost:,} 灵石，剩余：{player.gold:,}")

    # ===== /扩孔 =====
    @player_required
    async def handle_expand(self, player: Player, event: AstrMessageEvent, args: str = ""):
        text = (args or "").strip()
        if not text:
            try:
                raw = (event.get_message_str() or "").strip().lstrip("/").strip()
                if raw.startswith(CMD_EXPAND_SOCKET):
                    text = raw[len(CMD_EXPAND_SOCKET):].strip()
            except Exception:
                pass
        if not text:
            yield event.plain_result("用法：/扩孔 <装备名称或实例编号>\n示例：/扩孔 青锋剑")
            return
        target, _candidates, error = self._find_instance(player, text)
        if not target:
            yield event.plain_result(error)
            return
        cost = JewelRules.next_expand_cost(target)
        if cost is None:
            yield event.plain_result(f"❌ 该装备孔位已达统一上限 {MAX_SOCKETS} 孔。")
            return
        stored = int((player.get_storage_ring_items() or {}).get("强化石", 0) or 0)
        if stored < cost:
            yield event.plain_result(
                f"❌ 扩孔需要 {cost} 个【强化石】，你储物戒中只有 {stored} 个。"
            )
            return

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = await self.db.get_player_by_id(player.user_id)
            if not fresh:
                await self.db.conn.rollback()
                yield event.plain_result("❌ 角色数据异常，请重新踏入仙途。")
                return
            items = fresh.get_storage_ring_items()
            have = int(items.get("强化石", 0) or 0)
            if have < cost:
                await self.db.conn.rollback()
                yield event.plain_result(f"❌ 强化石不足：需要 {cost} 个，当前 {have} 个。")
                return
            target, _candidates, error = self._find_instance(fresh, text)
            if not target:
                await self.db.conn.rollback()
                yield event.plain_result(error)
                return
            cost = JewelRules.next_expand_cost(target)
            if cost is None:
                await self.db.conn.rollback()
                yield event.plain_result(f"❌ 该装备孔位已达统一上限 {MAX_SOCKETS} 孔。")
                return
            instances = self._all_instances(fresh)
            live_target = next(
                (x for x in instances if int(x.get("id", 0) or 0) == int(target.get("id", 0) or 0)),
                None,
            )
            ok, msg, spent = self.jewel_manager.expand_instance_socket(live_target)
            if not ok:
                await self.db.conn.rollback()
                yield event.plain_result(msg)
                return
            if have <= spent:
                items.pop("强化石", None)
            else:
                items["强化石"] = have - spent
            fresh.set_storage_ring_items(items)
            fresh.set_equipment_instances(instances)
            await self.db.update_player(fresh, commit=False)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise
        player.set_storage_ring_items(fresh.get_storage_ring_items())
        player.set_equipment_instances(fresh.get_equipment_instances())
        yield event.plain_result(msg)
        yield event.plain_result(
            f"🪨 消耗强化石：{spent} 个，剩余：{int((items or {}).get('强化石', 0) or 0)} 个"
        )
