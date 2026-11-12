# managers/reincarnation_manager.py
import json
import math
from ..data import DataBase
from ..models import Player
from ..core.item_instance_manager import ItemInstanceManager
from ..core.reincarnation_rules import (
    DAO_GIFT_PCT_PER_STACK,
    NEWLIFE_ESCORT_TRIPS_PER_STACK,
    NEWLIFE_MARKER,
    NEWLIFE_ROOT_MARKER,
    SHOP_ITEMS,
    add_item,
    apply_dao_gift,
    dao_gift_pct,
    escort_gold_base,
    format_dao_gift,
    grant_dao_gift,
    item_at_cap,
    item_owned,
    ledger_value,
    newlife_gold,
    parse_bonus,
    root_reroll_stock,
)

class ReincarnationManager:
    """飞升转生管理器 - 支持传家宝封印与仙缘商店"""
    
    def __init__(self, db: DataBase, config_manager, storage_ring_mgr):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_mgr = storage_ring_mgr

    # ---------- 共用小工具 ----------

    def ascend_points(self, player: Player) -> int:
        """本次飞升可得仙缘点：100 基础 + 修为档位（log10 经验×10）+ 完美转生次数×50。"""
        base_points = 100 + int(math.log10(max(player.experience, 1)) * 10)
        return base_points + player.reincarnation_count * 50

    async def _find_heirloom(self, player: Player, heirloom_name: str) -> str:
        """校验待封印的物品是否存在，返回 'equipment' / 'item' / ''（不存在）。"""
        if not heirloom_name:
            return ""
        im = ItemInstanceManager(self.db, self.config_manager)
        for location in ("storage", "equipped"):
            try:
                if im.list_instances(player, location, heirloom_name):
                    return "equipment"
            except Exception:
                continue
        try:
            items = player.get_storage_ring_items()
        except Exception:
            items = {}
        if items.get(heirloom_name, 0) > 0:
            return "item"
        return ""

    def _bonus_preview_text(self, bonus_data: dict) -> str:
        """永久加成摘要，用于飞升确认页。"""
        parts = []
        try:
            speed = float(bonus_data.get("global_cultivation_multiplier", 0.0) or 0.0)
        except (TypeError, ValueError):
            speed = 0.0
        if speed > 0:
            parts.append(f"修炼加速 +{round(speed * 100)}%")
        gift = format_dao_gift(bonus_data)
        if gift:
            parts.append(gift)
        return " · ".join(parts)

    async def preview_reincarnate(self, player: Player, heirloom_name: str = "") -> tuple[bool, str]:
        """飞升前置确认：只读校验 + 罗列得失，不改变任何状态。

        返回 ``(可飞升, 文案)``；不可飞升时文案即拒绝原因。
        """
        level_data = self.config_manager.get_level_data(player.cultivation_type)
        max_level = len(level_data) - 1
        if player.level_index < max_level:
            current = level_data[min(player.level_index, max_level)].get("level_name", "未知")
            target = level_data[max_level].get("level_name", "最高境界")
            return False, (
                f"❌ 你的境界尚未达到巅峰（当前【{current}】，需【{target}】），无法飞升。"
            )

        try:
            loan = await self.db.ext.get_active_loan(player.user_id)
        except Exception as e:
            return False, f"❌ 贷款状态查询失败，无法飞升，请稍后再试。（{e}）"
        if loan:
            return False, "❌ 你仍有未结清的灵石贷款，无法飞升。请先还清贷款。"

        bonus_data = parse_bonus(player)
        existing = bonus_data.get("heirloom") or {}
        heirloom_name = (heirloom_name or "").strip()

        if heirloom_name and existing.get("sealed"):
            return False, (
                f"❌ 你还有未解封的传家宝【{existing.get('name', '未知')}】。\n"
                "请先达到筑基期使用 /解封传家宝，或本次飞升不再指定新传家宝。"
            )
        if heirloom_name and not await self._find_heirloom(player, heirloom_name):
            return False, (
                f"❌ 你的储物戒与已装备栏中都没有【{heirloom_name}】，无法将其封印为传家宝。"
            )

        points = self.ascend_points(player)
        next_count = player.reincarnation_count + 1
        level_name = level_data[player.level_index].get("level_name", "未知")
        break_even = f"100 基础 + {points - 100 - player.reincarnation_count * 50} 修为 + " \
                     f"{player.reincarnation_count * 50} 完美转生加成"

        bonus_line = self._bonus_preview_text(bonus_data)
        startup = int(escort_gold_base(player.level_index) * ledger_value(bonus_data, "newlife_trips"))
        lines = [
            "✨ 飞升确认 ✨",
            "━━━━━━━━━━━━━━━",
            f"当前境界：【{level_name}】· 修为 {int(player.experience):,}",
            f"本次为第 {next_count} 次完美转生，预计获得仙缘点 {points}",
            f"（{break_even}）",
            "",
            "将清空：境界 · 修为 · 灵石（重置为 1,000）· 装备 · 功法 · 储物戒",
            "        丹药 · 仓库物品 · 洞天 · 灵田 · 灵眼 · 仙灵 · 宗门订单",
            "        自动代理 · 坊市/拍卖/仙市资产 · 双修与师徒关系",
            "将重塑：灵根 · 职业 · 生活职业（飞升后不再保留，需重新选择）",
            "将保留：称号 · 仙缘点 · 完美转生次数 · 仙缘商店永久加成",
            "        已买断的储物戒容量与仓库等级 · 传家宝",
        ]
        if startup:
            lines.append(f"💰 转世资粮：飞升后到账 {startup:,} 灵石")
        if bonus_line:
            lines.append(f"        永久加成：{bonus_line}")
        if heirloom_name:
            lines.append(f"🔒 将封印传家宝：【{heirloom_name}】（筑基期后可 /解封传家宝 取回）")
        lines += [
            "━━━━━━━━━━━━━━━",
            "⚠️ 飞升不可撤销，确认后立即重置全部修为与家产。",
        ]
        cmd = f"/飞升 {heirloom_name} 确认" if heirloom_name else "/飞升 确认"
        lines.append(f"确认请发送：{cmd}")
        return True, "\n".join(lines)

    async def try_reincarnate(self, player: Player, heirloom_name: str = "") -> tuple[bool, str]:
        """尝试飞升转生"""
        level_data = self.config_manager.get_level_data(player.cultivation_type)
        max_level = len(level_data) - 1
        
        if player.level_index < max_level:
            return False, "❌ 你的境界尚未达到巅峰，无法飞升。"

        # 与弃道重修一致：有未结清贷款时不允许转生，避免借新账转生
        try:
            loan = await self.db.ext.get_active_loan(player.user_id)
        except Exception as e:
            # 贷款状态无法确认时必须中止，不能把异常当成“无贷款”
            return False, f"❌ 贷款状态查询失败，无法飞升，请稍后再试。（{e}）"
        if loan:
            return False, "❌ 你仍有未结清的灵石贷款，无法飞升。请先还清贷款。"

        # 解析并扣除传家宝（优先按装备实例处理，保留品质与词条）
        heirloom_data = {}
        if heirloom_name and heirloom_name.strip():
            heirloom_name = heirloom_name.strip()
            im = ItemInstanceManager(self.db, self.config_manager)
            instances = im.list_instances(player, "storage", heirloom_name)
            if not instances:
                # 支持直接封印当前穿戴中的装备（飞升重置会清空装备栏，无需先卸下）
                instances = im.list_instances(player, "equipped", heirloom_name)
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
                    return False, f"❌ 你的储物戒与已装备栏中都没有【{heirloom_name}】，无法将其封印为传家宝。"
                items[heirloom_name] -= 1
                if items[heirloom_name] <= 0:
                    del items[heirloom_name]
                player.set_storage_ring_items(items)
                heirloom_data = {"name": heirloom_name, "sealed": True, "kind": "item"}
            
        # 计算仙缘点
        earned_points = self.ascend_points(player)
        old_level_name = level_data[player.level_index]["level_name"]
        
        # 读取旧有的永久加成（仙缘商店买的、传家宝等）
        bonus_data = parse_bonus(player)
        
        # 写入传家宝封印：已有未解封传家宝时不允许静默覆盖/丢弃
        existing_heirloom = bonus_data.get("heirloom")
        if heirloom_data:
            if existing_heirloom and existing_heirloom.get("sealed"):
                return False, (
                    f"❌ 你还有未解封的传家宝【{existing_heirloom.get('name', '未知')}】。\n"
                    "请先达到筑基期使用 /解封传家宝，或本次飞升不再指定新传家宝。"
                )
            bonus_data["heirloom"] = heirloom_data

        # 飞升不再保留灵根 / 职业 / 生活职业：先给一副「未塑道体」，等玩家重新选择
        peak_level = player.level_index
        try:
            from ..core.cultivation_manager import CultivationManager
            base_player = CultivationManager(
                {"SPIRIT_ROOT_WEIGHTS": {"WUXING_ROOT_WEIGHT": 1}}, self.config_manager
            ).generate_new_player_stats(player.user_id, "灵修")
        except Exception:
            base_player = Player(user_id=player.user_id)

        # 执行飞升重置
        player.level_index = 0
        player.experience = 0
        player.gold = 1000 + int(escort_gold_base(peak_level) * ledger_value(bonus_data, "newlife_trips"))
        player.lifespan = base_player.lifespan
        player.hp = base_player.hp
        player.mp = base_player.mp
        player.atk = base_player.atk
        player.atkpractice = 0
        player.spiritual_qi = base_player.spiritual_qi
        player.max_spiritual_qi = base_player.max_spiritual_qi
        player.blood_qi = base_player.blood_qi
        player.max_blood_qi = base_player.max_blood_qi
        player.magic_damage = base_player.magic_damage
        player.physical_damage = base_player.physical_damage
        player.magic_defense = base_player.magic_defense
        player.physical_defense = base_player.physical_defense
        player.mental_power = base_player.mental_power

        # 仙缘商店的【道基淬炼】是跨世永久属性：基础属性已被覆盖，按账本重新放大
        apply_dao_gift(player, bonus_data)

        # 灵根 / 职业 / 生活职业不保留：留一个「待重塑」标记，由 /我要修仙 重新选择
        player.spiritual_root = NEWLIFE_ROOT_MARKER
        player.cultivation_type = NEWLIFE_MARKER
        player.life_skill = ""
        player.life_skill_changes = 0
        player.life_skill_cost_peak = 0
        player.life_skill_last_change_at = 0
        
        player.weapon = ""
        player.armor = ""
        player.main_technique = ""
        player.set_techniques_list([])
        
        player.set_storage_ring_items({})
        player.set_overflow_items({})
        # 仓库物品随飞升清空，等级保留（买断的永久资产，与储物戒同理）
        player.set_warehouse_items({})
        player.set_equipment_instances([])
        player.set_equipped_instances({})
        player.set_pills_inventory({})
        player.active_pill_effects = "[]"
        player.permanent_pill_gains = "{}"
        player.has_resurrection_pill = False
        player.resurrection_pill_quality = "一般"
        player.has_debuff_shield = False
        player.daily_pill_usage = "{}"
        
        # 新手引导已完成，避免转生后 step=0 但无法再次触发 create 而永久卡住
        player.tutorial_step = 4
        player.reincarnation_count += 1
        player.fate_points += earned_points
        player.state = "空闲"
        player.cultivation_start_time = 0

        # 保存永久加成（含传家宝）
        player.permanent_bonus = json.dumps(bonus_data, ensure_ascii=False)

        # 飞升重置与附属数据清理必须同一事务，避免中途异常留下“已重置但资产未清”的脏档
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            await self.db.update_player(player, commit=False)

            # 强制清理附属产业与交易挂单，避免飞升后通过坊市/赠予/银行存款携带旧资产
            if self.db.ext:
                await self.db.ext.return_gifts_for_receiver(player.user_id, commit=False)
            await self.db.conn.execute("DELETE FROM pending_gifts WHERE sender_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM auction_npc_offers WHERE seller_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM auction_items WHERE seller_id = ?", (player.user_id,))
            # v6.57.0: 仙市与拍卖会(v6.35)资产同样必须清空，否则分红/退款会打进转生后的新号
            for _tbl in ("invest_holdings", "invest_orders", "invest_dividends",
                         "auction_house_bids", "auction_house_entries"):
                try:
                    await self.db.conn.execute(f"DELETE FROM {_tbl} WHERE user_id = ?", (player.user_id,))
                except Exception:
                    pass  # 旧存档可能尚未迁移出这些表，缺失时跳过
            try:
                # 进行中的拍品：飞升者为最高出价人时摘除其出价（锁定资金随飞升作废），
                # 否则落槌后 _deliver_lot_atomic 会把拍品发给转生新号（资产携带）
                await self.db.conn.execute(
                    "UPDATE auction_house_lots SET highest_bidder='', highest_bid=0 "
                    "WHERE highest_bidder=? AND status='live'",
                    (player.user_id,),
                )
                # 已中标未领取的拍品直接作废（'cancelled' 不在任何投递/领取查询的状态集合内）
                await self.db.conn.execute(
                    "UPDATE auction_house_lots SET winner_id='', delivery_status='cancelled' "
                    "WHERE winner_id=? AND delivery_status IN ('pending','delivering')",
                    (player.user_id,),
                )
                # 未偿贷款随飞升作废，否则逾期定时任务会对转生新号置坏账并跌落境界
                await self.db.conn.execute(
                    "DELETE FROM bank_loans WHERE user_id=? AND status IN ('active','overdue')",
                    (player.user_id,),
                )
            except Exception:
                pass  # 旧存档缺表时跳过
            await self.db.conn.execute("DELETE FROM bank_accounts WHERE user_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM auto_proxy WHERE user_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM blessed_lands WHERE user_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM spirit_farms WHERE user_id = ?", (player.user_id,))
            await self.db.conn.execute("UPDATE spirit_eyes SET owner_id = NULL, owner_name = NULL, claim_time = NULL, last_collect_time = NULL WHERE owner_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM npc_partners WHERE user_id = ?", (player.user_id,))
            await self.db.conn.execute("DELETE FROM master_relations WHERE master_id = ? OR disciple_id = ?", (player.user_id, player.user_id))
            # 清空活动进度，避免高境界接取任务后飞升再以 0 级结算旧奖励
            for table in (
                "bounty_tasks", "daily_tasks", "combat_cooldowns", "combat_daily",
                "world_boss_damage", "world_boss_raid_damage",
                "dual_cultivation", "dual_cultivation_requests", "spar_requests", "sect_tournament_entries",
                "weekly_boss_members", "weekly_boss_claims", "weekly_boss_leaderboard", "weekly_boss_attempts",
            ):
                if table in ("dual_cultivation_requests", "spar_requests"):
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
            await self.db.conn.execute(
                "DELETE FROM system_config WHERE key IN (?, ?)",
                (f"tide_contrib_{player.user_id}", f"tide_bless_{player.user_id}"),
            )
            # 新一轮转生：灵根重抽预算回到「5 次免费」
            await self.db.conn.execute(
                "DELETE FROM system_config WHERE key = ?",
                (f"rebirth_root_reroll_{player.user_id}",),
            )
            await self.db.ext.set_user_free(player.user_id, commit=False)
            await self.db.conn.commit()
        except Exception as e:
            await self.db.conn.rollback()
            return False, f"❌ 飞升结算失败，已回滚，请稍后再试。（{e}）"
        
        seal_msg = f"\n🔒 你的【{heirloom_name}】已被封印为传家宝，静待重见天日。" if heirloom_name else ""
        bonus_line = self._bonus_preview_text(bonus_data)
        keep_msg = f"\n永久加成保留：{bonus_line}" if bonus_line else ""
        startup = int(escort_gold_base(peak_level) * ledger_value(bonus_data, "newlife_trips"))
        startup_msg = f"\n💰 转世资粮到账：{startup:,} 灵石" if startup else ""

        return True, (
            f"✨ 飞升成功！✨\n"
            f"━━━━━━━━━━━━━━━\n"
            f"你已从【{old_level_name}】破碎虚空，飞升上界！\n"
            f"境界与装备已全部化为虚无，洞天、灵田与仙灵也随风消散...\n"
            f"但你的修仙记忆化为了【仙缘】。{seal_msg}\n"
            f"获得仙缘点：{earned_points}{startup_msg}\n"
            f"完美转生次数：{player.reincarnation_count}{keep_msg}\n"
            f"当前仙缘点：{player.fate_points}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🌀 道体重塑：灵根、职业、生活职业已随上一世消散\n"
            f"请发送：我要修仙 职业名（灵修 / 体修 / 魔修 / 剑修 / 鬼修 / 佛修 / 御兽）\n"
            f"之后会为你抽取三条灵根，本轮有 5 次免费重抽\n"
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

        # 限制解封境界（筑基期 level_index >= 10）
        if player.level_index < 10:
            return False, "❌ 解封传家宝需要至少达到【筑基期】！请继续努力修行。"
            
        heirloom_name = heirloom.get("name")
        snapshot = heirloom.get("instance") if isinstance(heirloom.get("instance"), dict) else None

        # 物品发放与清除封印标记必须同一事务，避免两次提交之间崩溃导致重复解封
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            if heirloom.get("kind") == "equipment" or snapshot:
                volume = self.storage_ring_mgr._get_item_volume(heirloom_name)
                if self.storage_ring_mgr.get_available_slots(player) < volume:
                    await self.db.conn.rollback()
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
                desc = f"【{heirloom_name}·{(snapshot or {}).get('quality', '一般')}】"
            else:
                success, msg = await self.storage_ring_mgr.grant_item(
                    player, heirloom_name, 1, silent=True, external_transaction=True
                )
                if not success:
                    await self.db.conn.rollback()
                    return False, f"❌ 解封失败：你的储物戒已满或无法存入。\n{msg}"
                desc = f"【{heirloom_name}】"

            bonus_data.pop("heirloom", None)
            player.permanent_bonus = json.dumps(bonus_data, ensure_ascii=False)
            await self.db.update_player(player, commit=False)
            await self.db.conn.commit()
        except Exception as e:
            await self.db.conn.rollback()
            return False, f"❌ 解封失败，已回滚，请稍后再试。（{e}）"

        return True, f"✨ 传家宝解封成功！{desc} 已存入你的储物戒。"

    async def buy_fate_shop_item(self, player: Player, item_id: int) -> tuple[bool, str]:
        """仙缘商店兑换（目录见 core.reincarnation_rules.SHOP_ITEMS）。"""
        item = SHOP_ITEMS.get(int(item_id))
        if not item:
            return False, "❌ 无效的商品编号（1~8），发送 /仙缘商店 查看。"

        bonus_data = parse_bonus(player)
        name = item["name"]
        cost = int(item["cost"])
        cap = int(item.get("cap") or 0)

        if item_at_cap(bonus_data, item_id):
            return False, f"❌ 【{name}】已满 {cap} 层，无需继续兑换。"
        if player.fate_points < cost:
            return False, f"❌ 仙缘点不足：【{name}】需要 {cost} 点，当前 {player.fate_points} 点。"

        player.fate_points -= cost

        if item_id == 3:
            # 道基淬炼：当场放大现有属性，同时记账供转生重放
            grant_dao_gift(player, bonus_data)
            owned = item_owned(bonus_data, 3)
            msg = (
                f"✨ 【道基淬炼】第 {owned} 层生效：全属性 +{round(dao_gift_pct(bonus_data) * 100)}%"
                "（已记入永久账本，飞升与弃道重修后都会重新放大）"
            )
        else:
            add_item(bonus_data, item_id)
            msg = self._shop_effect_text(item_id, bonus_data, player)

        player.permanent_bonus = json.dumps(bonus_data, ensure_ascii=False)
        await self.db.update_player(player)
        return True, f"{msg}\n当前剩余仙缘点：{player.fate_points}"

    def _shop_effect_text(self, item_id: int, bonus_data: dict, player: Player) -> str:
        """购买成功后的效果说明（按商品各自的计量单位）。"""
        owned = item_owned(bonus_data, item_id)
        if item_id == 1:
            return (
                f"🌟 【修炼加速】第 {owned} 层：闭关与历练修为 +{owned * 10}%，"
                "跨世保留。"
            )
        if item_id == 2:
            per_life = int(escort_gold_base(player.level_index) * NEWLIFE_ESCORT_TRIPS_PER_STACK)
            return (
                f"💰 【转世资粮】第 {owned} 层：每次转生后到账"
                f" {per_life * 0:,} 灵石（按当前境界的 {owned * 2:g} 趟镖银折算；转生后境界越高越多）。"
            )
        if item_id == 4:
            return (
                f"🌀 【灵根洗炼】额度 +1，当前库存 {root_reroll_stock(player)} 次。"
                "每轮转生的 5 次免费重抽用完后自动消耗。"
            )
        if item_id == 5:
            return (
                f"🎯 【破境感悟】第 {owned} 层：突破基础成功率 +{owned * 0.5:g} 个百分点"
                "（破境丹的上限一并抬高）。"
            )
        if item_id == 6:
            return f"🗺️ 【历练心得】第 {owned} 层：历练修为与灵石收益 +{owned * 10}%。"
        if item_id == 7:
            return f"🌿 【采药心得】第 {owned} 层：秘境材料掉率 +{owned * 3:g} 个百分点。"
        if item_id == 8:
            return f"🧭 【寻宝直觉】第 {owned} 层：秘境装备掉率 +{owned * 10}%。"
        return f"✅ 【{SHOP_ITEMS[item_id]['name']}】第 {owned} 层已生效。"
