# handlers/player_handler.py
import time
import random
from datetime import datetime
from astrbot.api.event import AstrMessageEvent
from astrbot.api import AstrBotConfig
from ..data import DataBase
from ..core import CultivationManager, PillManager, StorageRingManager
from ..models import Player
from ..models_extended import UserStatus
from ..config_manager import ConfigManager
from ..managers.tutorial_manager import TutorialManager
from .utils import player_required, with_player_lock, _loan_identity, set_event_flag

CMD_START_XIUXIAN = "我要修仙"
CMD_PLAYER_INFO = "我的信息"
CMD_START_CULTIVATION = "闭关"
CMD_END_CULTIVATION = "出关"
CMD_CHECK_IN = "签到"
REBIRTH_COOLDOWN = 7 * 24 * 3600

# 七大职业与别名（单字简称、常见写法），入口统一归一化
CULTIVATION_ALIASES = {
    "灵修": "灵修", "灵": "灵修", "法师": "灵修", "灵修者": "灵修",
    "体修": "体修", "体": "体修", "武僧": "体修", "体修者": "体修",
    "魔修": "魔修", "魔": "魔修", "魔修者": "魔修",
    "剑修": "剑修", "剑": "剑修", "剑修者": "剑修", "剑客": "剑修",
    "鬼修": "鬼修", "鬼": "鬼修", "鬼修者": "鬼修",
    "佛修": "佛修", "佛": "佛修", "佛修者": "佛修", "和尚": "佛修",
    "御兽": "御兽", "兽": "御兽", "御兽师": "御兽", "驭兽": "御兽",
}
VALID_CULTIVATIONS = ["灵修", "体修", "魔修", "剑修", "鬼修", "佛修", "御兽"]

__all__ = ["PlayerHandler"]

class PlayerHandler:
    """玩家基础信息处理器 - 支持七大职业（灵/体/魔/剑/鬼/佛/御兽，含单字别名）"""

    def __init__(self, db: DataBase, config: AstrBotConfig, config_manager: ConfigManager, social_mgr=None):
        self.db = db
        self.config = config
        self.config_manager = config_manager
        self.social_mgr_ref = social_mgr  # SocialManager（仙灵加成），main.py 注入
        self.cultivation_manager = CultivationManager(config, config_manager)
        self.pill_manager = PillManager(self.db, self.config_manager)
        self.storage_ring_mgr = StorageRingManager(db, config_manager)
        self.tutorial_mgr = TutorialManager(self.db)
        
        # ===== 修仙界专属节日配置表 =====
        self.FESTIVALS = {
            "01-01": {
                "name": "问道节", "desc": "一元复始，感悟天地法则，闭关修炼效率大增。", 
                "gold": 8888, "exp": 8888, "item": "洗练石", "item_count": 1
            },
            "02-14": {
                "name": "结缘节", "desc": "红线牵缘，道侣情深，天地灵气化作祝福。", 
                "gold": 5200, "exp": 5200, "item": "强化石", "item_count": 2
            },
            "03-03": {
                "name": "蟠桃盛会", "desc": "瑶池金母设宴，三千年一开花，三千年一结果。", 
                "gold": 30000, "exp": 50000, "item": "极品洗练石", "item_count": 2
            },
            "05-04": {
                "name": "百器争鸣", "desc": "天下炼器师齐聚，地火涌动，正是炼器好时节。", 
                "gold": 10400, "exp": 10400, "item": "天外陨铁", "item_count": 3
            },
            "06-06": {
                "name": "镇魔日", "desc": "天狗食日，魔气肆虐，天道赐下护身法宝。", 
                "gold": 6666, "exp": 6666, "item": "迷神香", "item_count": 2
            },
            "07-07": {
                "name": "双星会", "desc": "牛郎织女星交汇，此时双修可得双倍天地馈赠。", 
                "gold": 7700, "exp": 7700, "item": "护府令", "item_count": 1
            },
            "08-15": {
                "name": "太阴盛会", "desc": "月圆之夜，太阴之精最盛，适合吸收月华修炼。", 
                "gold": 15000, "exp": 15000, "item": "九叶灵芝", "item_count": 1
            },
            "09-09": {
                "name": "重阳飞升日", "desc": "登高望远，触摸天门，各大宗门发放试炼奖励。", 
                "gold": 9999, "exp": 9999, "item": "洗练石", "item_count": 3
            },
            "10-01": {
                "name": "万仙来朝", "desc": "普天同庆，仙界大开宝库，降下海量修行资源。", 
                "gold": 19490, "exp": 19490, "item": "极品洗练石", "item_count": 1
            },
            "11-11": {
                "name": "清心寡欲日", "desc": "红尘滚滚，我自独行，极致的孤独带来极致的道心。", 
                "gold": 11110, "exp": 11110, "item": "强化石", "item_count": 5
            },
            "12-22": {
                "name": "阴极冬至", "desc": "阴极之至，阳气始生，天道赐下纯阳之气助你突破。", 
                "gold": 12220, "exp": 12220, "item": "天阶·混元道果", "item_count": 1
            },
            "12-25": {
                "name": "瑞雪丰年", "desc": "天降祥瑞，灵石矿脉爆发，万物生机盎然。", 
                "gold": 12250, "exp": 12250, "item": "极品洗练石", "item_count": 1
            },
        }

    def _avatar_status(self, player: Player) -> str:
        """化身傀儡剩余时间描述"""
        import json as _json
        import time as _time
        try:
            bonus = _json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            until = int(bonus.get("avatar_until", 0))
            if until > int(_time.time()):
                return f"行功中（剩{(until - int(_time.time())) // 86400}天，闭关+20%）"
        except Exception:
            pass
        return "无"

    def _get_today_festival(self) -> dict:
        """获取今天的节日信息（如果没有则返回 None）"""
        today_md = datetime.now().strftime("%m-%d")
        return self.FESTIVALS.get(today_md)

    async def handle_start_xiuxian(self, event: AstrMessageEvent, cultivation_type: str = ""):
        """处理创建角色"""
        user_id = event.get_sender_id()

        if await self.db.get_player_by_id(user_id):
            yield event.plain_result("道友，你已踏入仙途，无需重复此举。")
            return

        if not cultivation_type or cultivation_type.strip() == "":
            # 参数为空时，从完整消息文本兜底提取职业关键词
            # （兼容「我要修仙御兽」无空格、带 / 前缀等写法）
            raw_msg = ""
            try:
                raw_msg = (event.get_message_str() or "").strip()
            except Exception:
                pass
            for alias in sorted(CULTIVATION_ALIASES.keys(), key=len, reverse=True):
                if alias in raw_msg:
                    cultivation_type = alias
                    break
            if not cultivation_type:
                help_msg = (
                    "🌟 欢迎踏入修仙之路！\n"
                    "━━━━━━━━━━━━━━━\n"
                    "请选择你的修炼方式（发送：我要修仙 职业名）：\n\n"
                    "【灵修】法系群攻，灵力充沛\n【体修】血厚防高，物理输出\n【魔修】高攻低防，吸血为生\n"
                    "【剑修】极致物理爆发\n【鬼修】高闪避与法系伤害\n【佛修】超级肉盾，反伤\n【御兽】与灵兽协同作战\n"
                    "━━━━━━━━━━━━━━━\n"
                    f"💡 使用方法：\n  {CMD_START_XIUXIAN} 剑修\n"
                    f"（也支持单字：{CMD_START_XIUXIAN} 剑）"
                )
                yield event.plain_result(help_msg)
                return

        cultivation_type = cultivation_type.strip().replace("　", " ").strip()
        # 别名归一化：支持单字简称（灵/体/魔/剑/鬼/佛/兽）与常见写法
        normalized = CULTIVATION_ALIASES.get(cultivation_type, cultivation_type)
        if normalized not in VALID_CULTIVATIONS:
            yield event.plain_result(
                f"❌ 职业选择错误！收到的是「{cultivation_type}」。\n"
                f"请从以下 7 种职业中选择其一：灵修 / 体修 / 魔修 / 剑修 / 鬼修 / 佛修 / 御兽\n"
                f"（也支持单字简称：灵 / 体 / 魔 / 剑 / 鬼 / 佛 / 兽）\n"
                f"例如：我要修仙 剑修"
            )
            return
        cultivation_type = normalized

        new_player = self.cultivation_manager.generate_new_player_stats(user_id, cultivation_type)
        await self.db.create_player(new_player)
        try:
            await self.db.ext.ensure_impart_info(user_id)
        except Exception:
            pass
        # 弃道重修次数保存在 system_config，跨角色保留；达到 3 次补发【百变星君】
        new_player_title_msg = ""
        try:
            raw_total = await self.db.ext.get_system_config(f"rebirth_total_{user_id}")
            total = int(raw_total or 0)
            if total >= 3:
                from ..managers.title_manager import TitleManager
                tm = TitleManager(self.db)
                new_title, tmsg = tm.grant(new_player, "百变星君")
                if new_title:
                    await self.db.update_player(new_player)
                    if tmsg:
                        new_player_title_msg = tmsg.strip()
                    else:
                        new_player_title_msg = ""
                else:
                    new_player_title_msg = ""
            else:
                new_player_title_msg = ""
        except Exception:
            new_player_title_msg = ""

        root_name = new_player.spiritual_root.replace("灵根", "")
        root_description = self.cultivation_manager._get_root_description(root_name)

        reply_msg = (
            f"🎉 恭喜道友 {event.get_sender_name()} 踏上仙途！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"修炼方式：【{new_player.cultivation_type}】\n"
            f"灵根：【{new_player.spiritual_root}】\n"
            f"评价：{root_description}\n"
            f"启动资金：{new_player.gold} 灵石\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 发送「{CMD_PLAYER_INFO}」查看状态"
        )
        
        tutorial_msg = await self.tutorial_mgr.check_and_advance(new_player, "create")
        reply_msg += tutorial_msg
        if new_player_title_msg:
            reply_msg += f"\n{new_player_title_msg}"
        
        yield event.plain_result(reply_msg)

    @player_required
    async def handle_player_info(self, player: Player, event: AstrMessageEvent):
        """处理查看玩家信息"""
        display_name = event.get_sender_name()
        required_exp = player.get_required_exp(self.config_manager)

        await self.pill_manager.update_temporary_effects(player)
        pill_multipliers = self.pill_manager.calculate_pill_attribute_effects(player)

        from ..core import EquipmentManager
        equipment_manager = EquipmentManager(self.db, self.config_manager)
        equipped_items = equipment_manager.get_equipped_items(
            player, self.config_manager.items_data, self.config_manager.weapons_data
        )
        total_attrs = player.get_total_attributes(equipped_items, pill_multipliers, equipment_manager.get_base_attributes(player))

        from ..managers.combat_manager import CombatManager
        impart_info = await self.db.ext.get_impart_info(player.user_id)
        effects = equipment_manager.get_combat_effects(player)

        combat_panel = CombatManager.build_combat_panel(
            player, pill_multipliers, impart_info, self.config_manager, effects
        )
        # 名片与实战一致：主战特效 + 全部出战灵兽攻击
        try:
            from ..managers.pet_manager import PetManager
            pet_mgr = PetManager(self.db)
            main_pet, support_pets, pet_effects, owner_atk = await pet_mgr.build_pet_lineup(
                player.user_id,
                combat_panel["hp"],
                combat_panel["atk"],
                combat_panel["defense"],
                player.cultivation_type,
                persist=False,
            )
            if main_pet or support_pets:
                pet_atk = 0
                for pet in [main_pet] + list(support_pets or []):
                    if pet and getattr(pet, "hp", 0) > 0:
                        pet_atk += int(pet.atk)
                combat_panel["atk"] = int(owner_atk) + pet_atk
                for k, v in (pet_effects or {}).items():
                    if k in combat_panel and isinstance(v, (int, float)):
                        combat_panel[k] = combat_panel.get(k, 0) + v
                combat_panel["defense"] += int((pet_effects or {}).get("defense_bonus", 0) or 0)
        except Exception:
            pass

        combat_power = CombatManager.calculate_combat_power(
            combat_panel, total_attrs["mental_power"]
        )
        element_name = (
            self.config_manager.get_item_element(player.main_technique)
            or self.config_manager.get_item_element(player.weapon)
            or "无"
        )
        from ..managers.luck_manager import LuckManager
        luck_parts = LuckManager.get_breakdown(player)
        luck_total = luck_parts["total"]
        luck_detail = (
            f"本体{luck_parts['base']}"
            + (f" + 称号{luck_parts['title']}" if luck_parts["title"] else "")
            + (f" + 词条{luck_parts['affix']}" if luck_parts["affix"] else "")
            + (f" + 符箓{luck_parts['talisman']}" if luck_parts["talisman"] else "")
        )
        
        sect_name = "无宗门"
        position_name = "散修"
        if player.sect_id and player.sect_id != 0:
            sect = await self.db.ext.get_sect_by_id(player.sect_id)
            if sect:
                sect_name = sect.sect_name
                if sect.sect_owner == player.user_id: position_name = "宗主"
                elif player.sect_position == 1: position_name = "长老"
                elif player.sect_position == 2: position_name = "亲传弟子"
                elif player.sect_position == 3: position_name = "内门弟子"
                else: position_name = "外门弟子"
        
        from ..core.item_rules import ItemRules
        gear_rows = [(ItemRules.SLOT_LABELS[slot], name or "无")
                     for slot, (name, _) in equipment_manager.get_gear_slots(player).items()]
        gear_text = "".join(f"  {label}：{name}\n" for label, name in gear_rows)
        technique_name = player.main_technique if player.main_technique else "无"
        
        dao_hao = player.user_name if player.user_name else display_name
        display_title = f"【{player.title}】" if player.title else ""
        full_name = f"{display_title}{dao_hao}"

        # 状态以 user_cd 为准（闭关/历练/押镖/秘境/宗门任务等都会写这里），
        # player.state 只在闭关时维护，不能作为唯一来源。
        user_cd = await self.db.ext.get_user_cd(player.user_id)
        if user_cd:
            status_text = UserStatus.get_name(user_cd.type)
        else:
            status_text = player.state or "空闲"

        reply_msg = (
            f"📋 道友 {full_name} 的信息\n"
            f"━━━━━━━━━━━━━━━\n"
            f"【基本信息】\n"
            f"  道号：{full_name}\n"
            f"  境界：{player.get_level(self.config_manager)}\n"
            f"  修为：{int(player.experience):,}/{int(required_exp):,}\n"
            f"  灵石：{player.gold:,}\n"
            f"  战力：{combat_power:,}\n"
            f"  灵根：{player.spiritual_root}\n"
            f"  气运：{luck_total}（{luck_detail}）\n"
            f"【修炼属性】\n"
            f"  修炼方式：{player.cultivation_type}\n"
            f"  状态：{status_text}\n"
            f"  寿命：{player.lifespan}\n"
            f"  精神力：{total_attrs['mental_power']}\n"
        )
        
        if player.cultivation_type == "体修":
            reply_msg += (
                f"  气血：{player.blood_qi}/{total_attrs.get('max_blood_qi', 0)}\n"
                f"  物伤：{total_attrs['physical_damage']} | 法伤：{total_attrs['magic_damage']}\n"
                f"  物防：{total_attrs['physical_defense']} | 法防：{total_attrs['magic_defense']}\n"
            )
        else:
            reply_msg += (
                f"  灵气：{player.spiritual_qi}/{total_attrs.get('max_spiritual_qi', 0)}\n"
                f"  法伤：{total_attrs['magic_damage']} | 物伤：{total_attrs['physical_damage']}\n"
                f"  法防：{total_attrs['magic_defense']} | 物防：{total_attrs['physical_defense']}\n"
            )
        
        reply_msg += (
            f"【战斗属性】\n"
            f"  气血：{combat_panel['hp']:,}/{combat_panel['max_hp']:,}\n"
            f"  灵气：{combat_panel['mp']:,}/{combat_panel['max_mp']:,}\n"
            f"  攻击：{combat_panel['atk']:,} | 防御：{combat_panel['defense']:,}\n"
            f"  暴击：{combat_panel['crit_rate']:.1f}% | 暴伤：{combat_panel['crit_damage'] * 100:.1f}%\n"
            f"  吸血：{combat_panel['lifesteal']:.1f}% | 反伤：{combat_panel['counter_damage']:.1f}%\n"
            f"  五行：{element_name}\n"
        )

        reply_msg += (
            f"【装备与宗门】\n"
            f"  主修心法：{technique_name}\n"
            f"{gear_text}"
            f"  宗门：{sect_name} | 职位：{position_name}\n"
        )
        
        loan = await self.db.ext.get_active_loan(player.user_id)
        loan_card_rows = []
        if loan:
            now = int(time.time())
            remaining_seconds = loan["due_at"] - now
            remaining_days = remaining_seconds // 86400
            remaining_hours = (remaining_seconds % 86400) // 3600
            days_borrowed = max(1, (now - loan["borrowed_at"]) // 86400)
            accrued_interest = int(
                loan["principal"] * loan["interest_rate"] * days_borrowed
            )
            paid_interest = int(loan.get("paid_interest", 0) or 0)
            paid_principal = int(loan.get("paid_principal", 0) or 0)
            interest_due = max(0, accrued_interest - paid_interest)
            principal_due = max(0, int(loan["principal"]) - paid_principal)
            total_due = principal_due + interest_due
            loan_type_name = "突破贷款" if loan["loan_type"] == "breakthrough" else "普通贷款"
            
            if remaining_seconds <= 0: time_str = "⚠️ 已逾期！"
            elif remaining_days <= 0: time_str = f"🔴 {remaining_hours}小时"
            elif remaining_days <= 1: time_str = f"🟠 {remaining_days}天{remaining_hours}小时"
            else: time_str = f"🟡 {remaining_days}天"
            
            identity_label, purchase_blocked = _loan_identity(
                total_due, max(0, remaining_days)
            )
            reply_msg += (
                f"\n🏷️ 身份：{identity_label}（欠款 {total_due:,} 灵石）\n"
                f"【贷款信息】💰\n"
                f"  类型：{loan_type_name}\n"
                f"  应还：{total_due:,} 灵石\n"
                f"  剩余：{time_str}\n"
            )
            if purchase_blocked:
                reply_msg += "  🚫 欠款过多，购买类功能已暂停。\n"

            loan_card_rows = [
                ("身份", identity_label),
                ("类型", loan_type_name),
                ("应还", f"{total_due:,} 灵石"),
                ("剩余", time_str),
            ]
            if purchase_blocked:
                loan_card_rows.append(("提示", "🚫 购买类功能已暂停"))

        if getattr(player, "sect_debt", 0) > 0:
            reply_msg += (
                f"\n【宗门欠款】\n"
                f"  欠款：{player.sect_debt:,} 灵石\n"
                f"  有灵石时会自动偿还；欠款未清前不能退出宗门。\n"
            )
        
        reply_msg += "━━━━━━━━━━━━━━━"

        # ===== 图片卡片：成功只发图，失败/关闭降级为纯文本 =====
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            card_path = None
            if card_enabled("CARD_INFO"):
                if player.cultivation_type == "体修":
                    energy_row = ("气血", f"{player.blood_qi}/{total_attrs.get('max_blood_qi', 0)}")
                else:
                    energy_row = ("灵气", f"{player.spiritual_qi}/{total_attrs.get('max_spiritual_qi', 0)}")

                card_path = await render_card_async(
                    "info",
                    full_name,
                    f"ID: {player.user_id} · {player.get_level(self.config_manager)}",
                    sections=[
                        {"name": "基础", "rows": [
                            ("职业", player.cultivation_type),
                            ("境界", player.get_level(self.config_manager)),
                            ("修为", f"{int(player.experience):,}/{int(required_exp):,}"),
                            ("灵石", f"{player.gold:,}"),
                            ("战力", f"{combat_power:,}"),
                            ("灵根", player.spiritual_root),
                            ("功德/业力", f"{getattr(player, 'karma', 0) or 0:+d}"),
                            ("气运", f"{luck_total}"),
                            ("生活职业", getattr(player, "life_skill", "") or "无"),
                            ("五行属性", element_name),
                            ("化身", self._avatar_status(player)),
                        ]},
                        {"name": "属性", "rows": [
                            energy_row,
                            ("物伤/法伤", f"{total_attrs['physical_damage']}/{total_attrs['magic_damage']}"),
                            ("物防/法防", f"{total_attrs['physical_defense']}/{total_attrs['magic_defense']}"),
                            ("精神力", f"{total_attrs['mental_power']}"),
                            ("状态", status_text),
                            ("寿命", f"{player.lifespan}"),
                        ]},
                        {"name": "战斗", "rows": [
                            ("气血", f"{combat_panel['hp']:,}/{combat_panel['max_hp']:,}"),
                            ("灵气", f"{combat_panel['mp']:,}/{combat_panel['max_mp']:,}"),
                            ("攻击/防御", f"{combat_panel['atk']:,}/{combat_panel['defense']:,}"),
                            ("暴击/暴伤", f"{combat_panel['crit_rate']:.1f}%/{combat_panel['crit_damage'] * 100:.1f}%"),
                            ("吸血/反伤", f"{combat_panel['lifesteal']:.1f}%/{combat_panel['counter_damage']:.1f}%"),
                            ("五行", element_name),
                        ]},
                        {"name": "装备与宗门", "rows": [
                            *gear_rows,
                            ("主修心法", technique_name),
                            ("宗门", sect_name),
                            ("职位", position_name),
                        ]},
                    ] + ([{"name": "贷款", "rows": loan_card_rows}] if loan_card_rows else []),
                    footer="模拟修仙 · 发送 /修仙帮助 查看全部指令",
                )
            if card_path:
                yield event.image_result(card_path)
            else:
                yield event.plain_result(reply_msg)
        except Exception as e:
            from astrbot.api import logger
            logger.warning(f"我的信息卡片生成失败（已降级为文本）: {e}")
            yield event.plain_result(reply_msg)

    @player_required
    async def handle_start_cultivation(self, player: Player, event: AstrMessageEvent):
        """处理闭关指令"""
        user_cd = await self.db.ext.get_user_cd(player.user_id)
        if user_cd and user_cd.type == UserStatus.CULTIVATING:
            yield event.plain_result("道友已在闭关中，请勿重复进入。")
            return
        if user_cd and user_cd.type != UserStatus.IDLE:
            yield event.plain_result(f"❌ 道友当前正{UserStatus.get_name(user_cd.type)}，无法闭关！")
            return

        # 兼容旧数据/异常中断：玩家状态仍为修炼中但 user_cd 丢失时，恢复闭关而不是覆盖开始时间
        if (
            (not user_cd or user_cd.type == UserStatus.IDLE)
            and player.state == "修炼中"
            and player.cultivation_start_time > 0
        ):
            await self.db.ext.set_user_busy(player.user_id, UserStatus.CULTIVATING, 0)
            yield event.plain_result(
                f"🧘 检测到未结算的闭关记录，已恢复闭关状态\n"
                f"💡 发送「{CMD_END_CULTIVATION}」结束闭关"
            )
            return

        player.state = "修炼中"
        player.cultivation_start_time = int(time.time())
        await self.db.update_player(player)
        await self.db.ext.set_user_busy(player.user_id, UserStatus.CULTIVATING, 0)

        yield event.plain_result(f"🧘 道友已进入闭关状态\n💡 发送「{CMD_END_CULTIVATION}」结束闭关")

    @with_player_lock
    async def handle_end_cultivation(self, event: AstrMessageEvent):
        """处理出关指令（不走忙碌状态拦截，保证任何时候都能出关）"""
        set_event_flag(event, "cultivation_success", False)
        player = await self.db.get_player_by_id(event.get_sender_id())
        if not player:
            yield event.plain_result("道友尚未踏入仙途，请发送「我要修仙」开启你的旅程。")
            return
        user_cd = await self.db.ext.get_user_cd(player.user_id)
        legacy_cultivating = (
            player.state == "修炼中"
            and (not user_cd or user_cd.type == UserStatus.IDLE)
        )
        if (not user_cd or user_cd.type != UserStatus.CULTIVATING) and not legacy_cultivating:
            yield event.plain_result("道友当前并未闭关，无需出关。")
            return
        if player.cultivation_start_time == 0:
            player.state = "空闲"
            player.cultivation_start_time = 0
            await self.db.update_player(player)
            await self.db.ext.set_user_free(player.user_id)
            yield event.plain_result("闭关记录异常，已为你恢复空闲状态，请重新闭关。")
            return

        end_time = int(time.time())
        duration_seconds = end_time - player.cultivation_start_time
        duration_minutes = duration_seconds // 60

        if duration_minutes < 1:
            player.state = "空闲"
            player.cultivation_start_time = 0
            await self.db.update_player(player)
            await self.db.ext.set_user_free(player.user_id)
            yield event.plain_result("道友闭关时间不足1分钟，已出关，未获得修为。")
            return

        try:
            cultivation_cfg = self.config_manager.game_config.get("cultivation", {})
        except AttributeError:
            cultivation_cfg = {}
        if not isinstance(cultivation_cfg, dict):
            cultivation_cfg = {}
        try:
            base_minutes = int(cultivation_cfg.get("max_cultivation_minutes", 1440))
        except (TypeError, ValueError):
            base_minutes = 1440
        base_minutes = max(1, base_minutes)
        realm_bonus = (player.level_index // 9) * 360
        MAX_CULTIVATION_MINUTES = base_minutes + realm_bonus
        effective_minutes = min(duration_minutes, MAX_CULTIVATION_MINUTES)
        exceeded_time = duration_minutes > MAX_CULTIVATION_MINUTES

        await self.pill_manager.update_temporary_effects(player)
        pill_multipliers = self.pill_manager.calculate_pill_attribute_effects(player)

        technique_bonus = 0.0
        try:
            from ..core import EquipmentManager
            equipment_manager = EquipmentManager(self.db, self.config_manager)
            equipped_items = equipment_manager.get_equipped_items(
                player, self.config_manager.items_data, self.config_manager.weapons_data
            )
            seen_techniques = set()
            for item in equipped_items:
                if (
                    item.item_type in ("main_technique", "technique")
                    and item.name not in seen_techniques
                ):
                    seen_techniques.add(item.name)
                    technique_bonus += item.exp_multiplier
        except Exception:
            technique_bonus = 0.0

        gained_exp = self.cultivation_manager.calculate_cultivation_exp(
            player, effective_minutes, technique_bonus, pill_multipliers
        )

        # 聚灵蒲团：洞天家具，闭关收益+5%
        furniture_note = ""
        try:
            land = await self.db.conn.execute(
                "SELECT furniture FROM blessed_lands WHERE user_id = ?", (player.user_id,)
            )
            row = await land.fetchone()
            if row and row[0] and "聚灵蒲团" in row[0]:
                gained_exp = int(gained_exp * 1.05)
                furniture_note = "\n🛋️ 聚灵蒲团生效：闭关收益+5%"
        except Exception:
            pass

        # 宗门藏经阁：全宗闭关收益+3%/级
        try:
            if player.sect_id:
                sect = await self.db.ext.get_sect_by_id(player.sect_id)
                if sect:
                    import json as _json
                    levels = _json.loads(getattr(sect, "buildings", "{}") or "{}")
                    lib_lv = int(levels.get("藏经阁", 0))
                    if lib_lv > 0:
                        gained_exp = int(gained_exp * (1 + 0.03 * lib_lv))
                        furniture_note += f"\n🏛️ 宗门藏经阁 Lv.{lib_lv} 生效：+{3 * lib_lv}%"
        except Exception:
            pass

        # 魔潮天灾：期间及余波中全服修炼效率-20%；抵御魔潮的义士获24小时天道庇佑(+10%)
        tide_note = ""
        try:
            tide_flag = await self.db.ext.get_system_config("tide_active")
            tide_aftermath = await self.db.ext.get_system_config("tide_aftermath_until")
            in_aftermath = bool(tide_aftermath) and int(time.time()) < int(tide_aftermath)
            if tide_flag == "1" or in_aftermath:
                gained_exp = int(gained_exp * 0.8)
                tide_note = "\n🌑 魔气侵蚀：修炼收益-20%"
            bless_until = await self.db.ext.get_system_config(f"tide_bless_{player.user_id}")
            if bless_until and int(time.time()) < int(bless_until):
                gained_exp = int(gained_exp * 1.10)
                tide_note += "\n🌟 天道庇佑：修炼收益+10%"
        except Exception:
            pass

        # 仙灵道侣加成（玄霜：闭关最高+6%）
        try:
            comp_bonus = await self.social_mgr_ref.companion_bonus_async(player.user_id, "cultivate")
            if comp_bonus > 0:
                gained_exp = int(gained_exp * (1 + comp_bonus))
                tide_note += f"\n💞 仙灵相伴：修炼收益+{int(comp_bonus * 100)}%"
        except Exception:
            pass

        # 化身傀儡：激活期间化身代为行功，闭关收益+20%
        try:
            import json as _json2
            _bonus = _json2.loads(player.permanent_bonus) if player.permanent_bonus else {}
            _avatar_until = int(_bonus.get("avatar_until", 0))
            if _avatar_until > int(time.time()):
                gained_exp = int(gained_exp * 1.20)
                tide_note += "\n🤖 化身行功：修炼收益+20%"
        except Exception:
            pass

        try:
            from ..managers.title_manager import TitleManager
            title_exp_bonus = TitleManager.get_exp_bonus(player)
            if title_exp_bonus > 0:
                gained_exp = int(gained_exp * (1 + title_exp_bonus))
                tide_note += f"\n🏅 称号修为加成：+{int(title_exp_bonus * 100)}%"
        except Exception:
            pass

        player.experience += gained_exp
        player.state = "空闲"
        player.cultivation_start_time = 0
        await self.db.update_player(player)
        await self.db.ext.set_user_free(player.user_id)

        await self.db.ext.add_task_progress(player.user_id, "cultivate", 1)
        set_event_flag(event, "cultivation_success", True)
        tutorial_msg = await self.tutorial_mgr.check_and_advance(player, "cultivate")

        hours = duration_minutes // 60
        minutes = duration_minutes % 60
        time_str = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"

        exceed_msg = f"\n⚠️ 闭关超过{MAX_CULTIVATION_MINUTES//60}小时，仅计算前{MAX_CULTIVATION_MINUTES//60}小时修为" if exceeded_time else ""
        
        reply_msg = (
            "🌟 道友出关成功！\n"
            "━━━━━━━━━━━━━━━\n"
            f"⏱️ 闭关时长：{time_str}\n"
            f"📈 获得修为：{gained_exp:,}{exceed_msg}{furniture_note}{tide_note}\n"
            f"💫 当前修为：{player.experience:,}\n"
        )
        reply_msg += tutorial_msg
        yield event.plain_result(reply_msg)

    @player_required
    async def handle_check_in(self, player: Player, event: AstrMessageEvent):
        """处理签到指令（包含修仙界节日特殊福利）"""
        today = datetime.now().strftime("%Y-%m-%d")
        if player.last_check_in_date == today:
            yield event.plain_result("📅 道友今日已经签到过了，请明日再来。")
            return

        check_in_gold_min = self.config.get("VALUES", {}).get("CHECK_IN_GOLD_MIN", 50)
        check_in_gold_max = self.config.get("VALUES", {}).get("CHECK_IN_GOLD_MAX", 500)
        if check_in_gold_min > check_in_gold_max:
            check_in_gold_min, check_in_gold_max = check_in_gold_max, check_in_gold_min

        check_in_gold = random.randint(check_in_gold_min, check_in_gold_max)
        try:
            from ..managers.pet_manager import PetManager
            pet_mgr = PetManager(self.db)
            pet = await pet_mgr.get_pet(player.user_id)
            gold_bonus = pet_mgr.get_gold_bonus(pet)
            if gold_bonus > 0:
                check_in_gold = int(check_in_gold * (1 + gold_bonus))
        except Exception:
            pass
        player.gold += check_in_gold
        player.last_check_in_date = today
        
        # ===== 修仙界节日福利判定 =====
        festival = self._get_today_festival()
        festival_msg = ""
        if festival:
            player.gold += festival["gold"]
            player.experience += festival["exp"]
            
            item_name = festival.get("item")
            item_count = festival.get("item_count", 1)
            if item_name:
                await self.storage_ring_mgr.store_item(player, item_name, item_count, silent=True)
            
            festival_msg = (
                f"\n🎊【{festival['name']}】专属福利降临！🎊\n"
                f"━━━━━━━━━━━━━━━\n"
                f"“{festival['desc']}”\n"
                f"额外获得：{festival['gold']:,} 灵石、{festival['exp']:,} 修为\n"
            )
            if item_name:
                festival_msg += f"福泽恩赐：【{item_name}】x{item_count}\n"
        # =================================

        await self.db.update_player(player)
        await self.db.ext.add_task_progress(player.user_id, "check_in", 1)
        # 称号：累计签到
        try:
            self._last_checkin_title = await self.title_hooks.bump(player, "checkin_count") if getattr(self, "title_hooks", None) else ""
        except Exception:
            self._last_checkin_title = ""

        reply_msg = (
            "✅ 签到成功！\n"
            "━━━━━━━━━━━━━━━\n"
            f"💰 获得灵石：{check_in_gold}\n"
            f"💎 当前灵石：{player.gold}\n"
            "━━━━━━━━━━━━━━━\n"
            "明日再来，莫要忘记哦~"
        )
        reply_msg += festival_msg

        # ===== 签到卡片：成功只发图，失败/关闭降级为纯文本（节日自动切换背景）=====
        try:
            from ..utils.image_generator import render_card_async, card_enabled
            card_path = None
            if card_enabled("CARD_CHECKIN"):
                card_path = await render_card_async(
                    "festival" if festival else "login",
                    f"签到成功 · {player.user_name or '道友'}",
                    today,
                    sections=[
                        {"name": "本次收益", "rows": [
                            ("签到灵石", f"+{check_in_gold}"),
                            ("当前灵石", f"{player.gold:,}"),
                            ("当前修为", f"{player.experience:,}"),
                        ] + (
                            [("节日福利", f"+{festival['gold']:,} 灵石 +{festival['exp']:,} 修为")]
                            if festival else []
                        )},
                    ],
                    footer="每日签到 · 节日有大礼 · 明日再来",
                )
            if card_path:
                yield event.image_result(card_path)
            else:
                yield event.plain_result(reply_msg)
        except Exception:
            yield event.plain_result(reply_msg)

    @player_required
    async def handle_rebirth(self, player: Player, event: AstrMessageEvent, confirm_text: str = ""):
        """弃道重修（7天冷却）"""
        self.last_rebirth_success = False
        user_cd = await self.db.ext.get_user_cd(player.user_id)
        if user_cd and user_cd.type != UserStatus.IDLE:
            status_name = UserStatus.get_name(user_cd.type)
            yield event.plain_result(f"❌ 你当前正在「{status_name}」，无法弃道重修。")
            return

        loan = await self.db.ext.get_active_loan(player.user_id)
        if loan:
            yield event.plain_result("❌ 你仍有未结清的灵石贷款，无法重修。请先还款。")
            return

        key = f"rebirth_last_{player.user_id}"
        last_ts = await self.db.ext.get_system_config(key)
        now = int(time.time())
        if last_ts:
            diff = now - int(last_ts)
            if diff < REBIRTH_COOLDOWN:
                remaining = REBIRTH_COOLDOWN - diff
                days = remaining // 86400
                hours = (remaining % 86400) // 3600
                minutes = (remaining % 3600) // 60
                yield event.plain_result(
                    "⌛ 弃道重修冷却中\n"
                    "━━━━━━━━━━━━━━━\n"
                    f"距离下次重修还需：{days}天{hours}小时{minutes}分钟"
                )
                return

        if confirm_text.strip() != "确认":
            yield event.plain_result(
                "⚠️ 弃道重修将删除当前角色的所有数据，并无法撤回！\n"
                "限制：每7天只能重修一次，且必须在空闲状态、无贷款时使用。\n"
                "━━━━━━━━━━━━━━━\n"
                "若你已做好准备，请发送：\n"
                "弃道重修 确认"
            )
            return

        try:
            raw_total = await self.db.ext.get_system_config(f"rebirth_total_{player.user_id}")
            total = int(raw_total or 0) + 1
        except (TypeError, ValueError):
            total = 1
        await self.db.delete_player_cascade(player.user_id)
        await self.db.ext.set_system_config(key, str(now))
        await self.db.ext.set_system_config(f"rebirth_total_{player.user_id}", str(total))
        self.last_rebirth_success = True

        yield event.plain_result(
            "💀 你选择了弃道重修，旧生一切化为尘埃。\n"
            "━━━━━━━━━━━━━━━\n"
            "可立即使用「我要修仙」重新踏上仙途。\n"
            "（7天内不可再次重修）"
        )