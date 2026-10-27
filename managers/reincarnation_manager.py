# managers/reincarnation_manager.py
import json
import math
from ..data import DataBase
from ..models import Player
from ..core.item_instance_manager import ItemInstanceManager

class ReincarnationManager:
    """飞升转生管理器 - 支持传家宝封印与仙缘商店"""
    
    def __init__(self, db: DataBase, config_manager, storage_ring_mgr):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_mgr = storage_ring_mgr

    async def try_reincarnate(self, player: Player, heirloom_name: str = "") -> tuple[bool, str]:
        """尝试飞升转生"""
        level_data = self.config_manager.get_level_data(player.cultivation_type)
        max_level = len(level_data) - 1
        
        if player.level_index < max_level:
            return False, "❌ 你的境界尚未达到巅峰，无法飞升。"

        # 与弃道重修一致：有未结清贷款时不允许转生，避免借新账转生
        try:
            loan = await self.db.ext.get_active_loan(player.user_id)
            if loan:
                return False, "❌ 你仍有未结清的灵石贷款，无法飞升。请先还清贷款。"
        except Exception:
            pass

        # 解析并扣除传家宝（优先按装备实例处理，保留品质与词条）
        heirloom_data = {}
        if heirloom_name and heirloom_name.strip():
            heirloom_name = heirloom_name.strip()
            im = ItemInstanceManager(self.db, self.config_manager)
            instances = im.list_instances(player, "storage", heirloom_name)
            if instances:
                inst = sorted(instances, key=lambda x: int(x.get("id", 0) or 0))[0]
                snapshot = {k: v for k, v in inst.items() if k != "id"}
                if not im.remove(player, int(inst["id"])):
                    return False, f"❌ 封印【{heirloom_name}】失败，请稍后再试。"
                heirloom_data = {
                    "name": heirloom_name,
                    "sealed": True,
                    "kind": "equipment",
                    "instance": snapshot,
                }
            else:
                items = player.get_storage_ring_items()
                if heirloom_name not in items or items[heirloom_name] <= 0:
                    return False, f"❌ 你的储物戒中并没有【{heirloom_name}】，无法将其封印为传家宝。"
                items[heirloom_name] -= 1
                if items[heirloom_name] <= 0:
                    del items[heirloom_name]
                player.set_storage_ring_items(items)
                heirloom_data = {"name": heirloom_name, "sealed": True, "kind": "item"}
            
        # 计算仙缘点
        base_points = 100 + int(math.log10(max(player.experience, 1)) * 10)
        bonus_points = player.reincarnation_count * 50
        earned_points = base_points + bonus_points
        old_level_name = level_data[player.level_index]["level_name"]
        
        # 读取旧有的永久加成（仙缘商店买的、传家宝等）
        try:
            bonus_data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
        except Exception:
            bonus_data = {}
        
        # 写入传家宝封印
        if heirloom_data:
            bonus_data["heirloom"] = heirloom_data
        else:
            bonus_data.pop("heirloom", None)
        
        # 执行飞升重置
        player.level_index = 0
        player.experience = 0
        player.gold = 1000
        player.hp = 0
        player.mp = 0
        player.atk = 0
        player.atkpractice = 0
        player.spiritual_qi = 100
        player.max_spiritual_qi = 1000
        player.blood_qi = 0
        player.max_blood_qi = 0
        player.magic_damage = 10
        player.physical_damage = 10
        player.magic_defense = 5
        player.physical_defense = 5
        player.mental_power = 100
        
        player.weapon = ""
        player.armor = ""
        player.main_technique = ""
        player.set_techniques_list([])
        
        player.set_storage_ring_items({})
        player.set_overflow_items({})
        player.set_equipment_instances([])
        player.set_equipped_instances({})
        player.set_pills_inventory({})
        player.active_pill_effects = "[]"
        player.permanent_pill_gains = "{}"
        player.has_resurrection_pill = False
        player.resurrection_pill_quality = "一般"
        player.has_debuff_shield = False
        player.daily_pill_usage = "{}"
        
        player.tutorial_step = 0
        player.reincarnation_count += 1
        player.fate_points += earned_points
        player.state = "空闲"
        player.cultivation_start_time = 0

        # 保存永久加成（含传家宝）
        player.permanent_bonus = json.dumps(bonus_data, ensure_ascii=False)
        await self.db.update_player(player)

        # 强制清理附属产业与交易挂单，避免飞升后通过坊市/赠予/银行存款携带旧资产
        try:
            if self.db.ext:
                await self.db.ext.return_gifts_for_receiver(player.user_id)
            await self.db.conn.execute("DELETE FROM pending_gifts WHERE sender_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM auction_npc_offers WHERE seller_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM auction_items WHERE seller_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM bank_accounts WHERE user_id = ?", (player.user_id,))
            await self.db.ext.delete_auto_proxy(player.user_id)
        except Exception:
            pass
        await self.db.conn.execute("DELETE FROM blessed_lands WHERE user_id = ?", (player.user_id,))
        await self.db.conn.execute("DELETE FROM spirit_farms WHERE user_id = ?", (player.user_id,))
        await self.db.conn.execute("UPDATE spirit_eyes SET owner_id = NULL, owner_name = NULL, claim_time = NULL, last_collect_time = NULL WHERE owner_id = ?", (player.user_id,))
        await self.db.conn.execute("DELETE FROM npc_partners WHERE user_id = ?", (player.user_id,))
        # 清空活动进度，避免高境界接取任务后飞升再以 0 级结算旧奖励
        for table in (
            "bounty_tasks", "daily_tasks", "combat_cooldowns",
            "world_boss_damage", "world_boss_raid_damage",
            "dual_cultivation", "dual_cultivation_requests", "sect_tournament_entries",
        ):
            if table == "dual_cultivation_requests":
                await self.db.conn.execute(
                    f"DELETE FROM {table} WHERE from_id = ? OR target_id = ?",
                    (player.user_id, player.user_id),
                )
            else:
                await self.db.conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (player.user_id,))
        await self.db.conn.execute(
            "UPDATE sect_orders SET status = 'cancelled' WHERE status = 'active' AND (issuer_id = ? OR target_id = ?)",
            (player.user_id, player.user_id),
        )
        await self.db.ext.set_user_free(player.user_id, commit=False)
        await self.db.conn.commit()
        
        seal_msg = f"\n🔒 你的【{heirloom_name}】已被封印为传家宝，静待重见天日。" if heirloom_name else ""
        
        return True, (
            f"✨ 飞升成功！✨\n"
            f"━━━━━━━━━━━━━━━\n"
            f"你已从【{old_level_name}】破碎虚空，飞升上界！\n"
            f"境界与装备已全部化为虚无，洞天、灵田与仙灵也随风消散...\n"
            f"但你的修仙记忆化为了【仙缘】。{seal_msg}\n"
            f"获得仙缘点：{earned_points}\n"
            f"转生次数：{player.reincarnation_count}\n"
            f"当前仙缘点：{player.fate_points}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 使用 `/仙缘商店` 可兑换永久加成，重新踏上仙途吧！"
        )

    async def unseal_heirloom(self, player: Player) -> tuple[bool, str]:
        """解封传家宝"""
        try:
            bonus_data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
        except Exception:
            bonus_data = {}
            
        heirloom = bonus_data.get("heirloom")
        if not heirloom or not heirloom.get("sealed"):
            return False, "❌ 你没有处于封印状态的传家宝。"

        # 限制解封境界（筑基期 level_index >= 3）
        if player.level_index < 3:
            return False, "❌ 解封传家宝需要至少达到【筑基期】！请继续努力修行。"
            
        heirloom_name = heirloom.get("name")
        snapshot = heirloom.get("instance") if isinstance(heirloom.get("instance"), dict) else None
        if heirloom.get("kind") == "equipment" or snapshot:
            volume = self.storage_ring_mgr._get_item_volume(heirloom_name)
            if self.storage_ring_mgr.get_available_slots(player) < volume:
                return False, "❌ 解封失败：你的储物戒空间不足，请清理后再试。"
            im = ItemInstanceManager(self.db, self.config_manager)
            im.create_instances(
                player,
                heirloom_name,
                1,
                quality=(snapshot or {}).get("quality", "一般"),
                affixes=(snapshot or {}).get("affixes", []),
                location="storage",
            )
            await self.db.update_player(player)
            desc = f"【{heirloom_name}·{(snapshot or {}).get('quality', '一般')}】"
        else:
            success, msg = await self.storage_ring_mgr.store_item(player, heirloom_name, 1, silent=True)
            if not success:
                return False, f"❌ 解封失败：你的储物戒已满或无法存入。\n{msg}"
            desc = f"【{heirloom_name}】"

        bonus_data.pop("heirloom", None)
        player.permanent_bonus = json.dumps(bonus_data, ensure_ascii=False)
        await self.db.update_player(player)
        
        return True, f"✨ 传家宝解封成功！{desc} 已存入你的储物戒。"

    async def buy_fate_shop_item(self, player: Player, item_id: int) -> tuple[bool, str]:
        """仙缘商店兑换"""
        try:
            bonus_data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
        except Exception:
            bonus_data = {}

        if item_id == 1:
            # 1. 修炼加速
            cost = 100
            if player.fate_points < cost:
                return False, f"❌ 仙缘点不足，需要 {cost} 点。"
            player.fate_points -= cost
            
            current_mult = bonus_data.get("global_cultivation_multiplier", 0.0)
            bonus_data["global_cultivation_multiplier"] = current_mult + 0.1
            msg = "🌟 兑换成功！你的闭关修炼速度永久提升了 10%。"
            
        elif item_id == 2:
            # 2. 初始灵石
            cost = 50
            if player.fate_points < cost:
                return False, f"❌ 仙缘点不足，需要 {cost} 点。"
            player.fate_points -= cost
            player.gold += 10000
            msg = "💰 兑换成功！你获得了 10,000 灵石。"
            
        elif item_id == 3:
            # 3. 大道馈赠（全属性）
            cost = 500
            if player.fate_points < cost:
                return False, f"❌ 仙缘点不足，需要 {cost} 点。"
            player.fate_points -= cost
            
            player.mental_power += 100
            player.max_spiritual_qi += 100
            player.max_blood_qi += 100
            player.magic_damage += 20
            player.physical_damage += 20
            player.magic_defense += 10
            player.physical_defense += 10
            msg = "✨ 兑换成功！大道之力加身，你的各项基础属性获得了永久提升！"
            
        else:
            return False, "❌ 无效的商品编号。"

        player.permanent_bonus = json.dumps(bonus_data, ensure_ascii=False)
        await self.db.update_player(player)
        return True, f"{msg}\n当前剩余仙缘点：{player.fate_points}"