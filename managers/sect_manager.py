# managers/sect_manager.py
"""
宗门系统管理器 - 处理宗门创建、管理、捐献、任务等逻辑
"""

import json
import random
import time
from typing import Tuple, List, Optional, Dict
from ..data.data_manager import DataBase
from ..models_extended import Sect, UserStatus
from ..models import Player
from .task_penalty import calc_abandon_penalty, charge_abandon_penalty

SECT_NAME_MIN_LENGTH = 2
SECT_NAME_MAX_LENGTH = 12
SECT_NAME_FORBIDDEN = ["管理员", "系统", "官方", "GM", "admin"]

# 宗门建筑配置：消耗宗门资金升级，给全宗提供被动
SECT_BUILDINGS: Dict[str, dict] = {
    "藏经阁": {"max_level": 5, "base_cost": 5000, "unlock_level": 1, "desc": "全宗闭关收益+3%/级"},
    "演武场": {"max_level": 5, "base_cost": 5000, "unlock_level": 1, "desc": "宗门任务奖励+10%/级"},
    "炼丹房": {"max_level": 5, "base_cost": 8000, "unlock_level": 1, "desc": "全宗炼丹成功率+2%/级"},
    "商堂": {"max_level": 5, "base_cost": 6000, "unlock_level": 2, "desc": "宗门贡献商店价格-2%/级"},
    "任务堂": {"max_level": 5, "base_cost": 6000, "unlock_level": 3, "desc": "日常任务修为/灵石奖励+3%/级"},
    "护山大阵": {"max_level": 5, "base_cost": 10000, "unlock_level": 4, "desc": "宗门战被掠夺灵石-5%/级"},
}

SECT_LEVEL_THRESHOLDS = [(1, 0), (2, 10000), (3, 30000), (4, 60000), (5, 100000)]
SECT_ORDER_PENALTY = 500
SECT_ORDER_MAX_HOURS = 72
SECT_TOURNAMENT_DURATION = 1800
SECT_TOURNAMENT_COOLDOWN = 86400
SECT_TOURNAMENT_MAX_PLAYERS = 32
SECT_WAR_DECLARE_COST = 10000
SECT_WAR_COOLDOWN = 72 * 3600
SECT_WAR_PROTECT = 24 * 3600
SECT_WAR_WINNER_COOLDOWN = 24 * 3600
SECT_WAR_PLUNDER_RATE = 0.20


class SectManager:
    """宗门系统管理器"""
    
    POSITIONS = {
        0: "宗主", 1: "长老", 2: "亲传弟子", 3: "内门弟子", 4: "外门弟子"
    }
    
    POSITION_PERMISSIONS = {
        0: ["manage_all", "kick", "position_change", "build", "search_skill"],
        1: ["kick_outer", "build"],
        2: ["learn_skill"],
        3: ["learn_skill"],
        4: []
    }
    
    def __init__(self, db: DataBase, config_manager=None):
        self.db = db
        self.config_manager = config_manager
        self.config = config_manager.sect_config if config_manager else {}
    
    def _validate_sect_name(self, name: str) -> Tuple[bool, str]:
        """验证宗门名称"""
        if len(name) < SECT_NAME_MIN_LENGTH or len(name) > SECT_NAME_MAX_LENGTH:
            return False, f"❌ 宗门名称长度需在{SECT_NAME_MIN_LENGTH}-{SECT_NAME_MAX_LENGTH}字之间！"
        for forbidden in SECT_NAME_FORBIDDEN:
            if forbidden.lower() in name.lower():
                return False, f"❌ 宗门名称包含禁用词汇！"
        return True, ""
    
    def get_buildings(self, sect: Sect) -> Dict[str, int]:
        """读取宗门建筑等级"""
        try:
            data = json.loads(sect.buildings) if getattr(sect, "buildings", None) else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get_building_level(self, sect: Sect, name: str) -> int:
        return int(self.get_buildings(sect).get(name, 0))

    def get_sect_level(self, sect: Sect) -> int:
        return max(1, min(5, int(getattr(sect, "sect_level", 1) or 1)))

    def get_sect_exp(self, sect: Sect) -> int:
        return max(0, int(getattr(sect, "sect_exp", 0) or 0))

    def get_building_limit(self, sect: Sect) -> int:
        """建筑等级上限：宗门等级 + 1，最高 5；已有超限建筑保留。"""
        return min(5, self.get_sect_level(sect) + 1)

    def apply_sect_exp(self, sect: Sect, amount: int) -> Tuple[int, int, bool]:
        """给宗门加经验并计算等级，返回 (旧等级, 新等级, 是否升级)。"""
        old_level = self.get_sect_level(sect)
        sect.sect_exp = self.get_sect_exp(sect) + max(0, int(amount))
        new_level = 1
        for level, threshold in SECT_LEVEL_THRESHOLDS:
            if sect.sect_exp >= threshold:
                new_level = level
        sect.sect_level = new_level
        return old_level, new_level, new_level > old_level

    async def add_sect_exp(self, sect_id: int, amount: int) -> Tuple[int, int, bool]:
        sect = await self.db.ext.get_sect_by_id(sect_id)
        if not sect:
            return 0, 0, False
        old_level, new_level, leveled = self.apply_sect_exp(sect, amount)
        await self.db.ext.update_sect(sect)
        return old_level, new_level, leveled

    def get_level_up_text(self, old_level: int, new_level: int) -> str:
        if new_level <= old_level:
            return ""
        unlocked = []
        for name, cfg in SECT_BUILDINGS.items():
            if cfg.get("unlock_level") == new_level:
                unlocked.append(name)
        suffix = f"，解锁：{'、'.join(unlocked)}" if unlocked else ""
        return f"\n🎉 宗门等级提升：Lv.{old_level} → Lv.{new_level}{suffix}"

    async def get_sect_building_bonus(self, user_id: str, name: str) -> int:
        """获取某玩家所在宗门指定建筑的等级（无宗门返回0）"""
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0:
            return 0
        sect = await self.db.ext.get_sect_by_id(player.sect_id)
        if not sect:
            return 0
        return self.get_building_level(sect, name)

    async def show_buildings(self, user_id: str) -> Tuple[bool, str]:
        """查看宗门建筑面板"""
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0:
            return False, "❌ 你还未加入宗门！"
        sect = await self.db.ext.get_sect_by_id(player.sect_id)
        if not sect:
            return False, "❌ 宗门信息异常！"

        levels = self.get_buildings(sect)
        sect_level = self.get_sect_level(sect)
        building_limit = self.get_building_limit(sect)
        lines = [
            f"🏛️ 【{sect.sect_name}】宗门建筑",
            f"宗门等级：Lv.{sect_level}（经验 {self.get_sect_exp(sect):,}）",
            f"宗门资金：{sect.sect_used_stone:,} 灵石",
            f"当前建筑上限：Lv.{building_limit}",
            "━━━━━━━━━━━━━━━",
        ]
        for name, cfg in SECT_BUILDINGS.items():
            lv = levels.get(name, 0)
            unlock_level = cfg.get("unlock_level", 1)
            if lv <= 0 and sect_level < unlock_level:
                lines.append(f"🔒【{name}】未解锁（需宗门等级 Lv.{unlock_level}）")
                lines.append(f"  └ {cfg['desc']}")
                continue
            next_cost = cfg["base_cost"] * (lv + 1)
            max_level = min(cfg["max_level"], building_limit)
            if lv >= cfg["max_level"]:
                status = "满级"
            elif lv >= building_limit:
                status = f"需提升宗门等级（当前上限 Lv.{building_limit}）"
            else:
                status = f"升级需 {next_cost:,} 资金"
            lines.append(f"【{name}】Lv.{lv}/{cfg['max_level']} - {cfg['desc']}")
            lines.append(f"  └ {status}")
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("💡 宗主/长老发送 /升级建筑 <名称>（消耗宗门资金）")
        return True, "\n".join(lines)

    async def upgrade_building(self, operator_id: str, name: str) -> Tuple[bool, str]:
        """升级宗门建筑（宗主/长老操作，消耗宗门资金）"""
        cfg = SECT_BUILDINGS.get(name)
        if not cfg:
            listing = "\n".join(
                f"  · {n}（{c['desc']}，满级{c['max_level']}级）" for n, c in SECT_BUILDINGS.items()
            )
            return False, (
                f"❌ 未知建筑【{name}】。\n可升级的建筑：\n{listing}\n"
                "💡 用法：/升级建筑 <建筑名>"
            )

        operator = await self.db.get_player_by_id(operator_id)
        if not operator or operator.sect_id == 0:
            return False, "❌ 你还未加入宗门！"
        if operator.sect_position not in (0, 1):
            return False, "❌ 只有宗主和长老才能升级建筑！"

        sect = await self.db.ext.get_sect_by_id(operator.sect_id)
        if not sect:
            return False, "❌ 宗门信息异常！"

        levels = self.get_buildings(sect)
        lv = levels.get(name, 0)
        sect_level = self.get_sect_level(sect)
        building_limit = self.get_building_limit(sect)
        unlock_level = cfg.get("unlock_level", 1)
        if lv <= 0 and sect_level < unlock_level:
            return False, f"❌ 【{name}】需要宗门等级 Lv.{unlock_level} 才能解锁。"
        if lv >= cfg["max_level"]:
            return False, f"❌ 【{name}】已达最高等级。"
        if lv >= building_limit:
            return False, f"❌ 【{name}】受宗门等级限制，当前最高只能升到 Lv.{building_limit}。"

        cost = cfg["base_cost"] * (lv + 1)
        if sect.sect_used_stone < cost:
            return False, f"❌ 宗门资金不足！升级【{name}】需要 {cost:,} 灵石（当前 {sect.sect_used_stone:,}），请号召成员 /宗门捐献。"

        sect.sect_used_stone -= cost
        levels[name] = lv + 1
        sect.buildings = json.dumps(levels, ensure_ascii=False)
        await self.db.ext.update_sect(sect)
        return True, (
            f"🏗️ 宗门建筑升级成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"【{name}】Lv.{lv} → Lv.{lv + 1}\n"
            f"效果：{cfg['desc']}\n"
            f"消耗宗门资金：{cost:,} 灵石"
        )

    async def create_sect(self, user_id: str, sect_name: str, required_stone: int = None, required_level: int = None) -> Tuple[bool, str]:
        """创建宗门"""
        if required_stone is None: required_stone = self.config.get("create_cost", 10000)
        if required_level is None: required_level = self.config.get("create_level_required", 3)
            
        player = await self.db.get_player_by_id(user_id)
        if not player: return False, "❌ 你还未踏入修仙之路！"
        if player.sect_id != 0: return False, "❌ 你已经加入了宗门，无法创建新宗门！"
        if player.level_index < required_level: return False, f"❌ 创建宗门需要达到境界等级 {required_level}！"
        if player.gold < required_stone: return False, f"❌ 创建宗门需要 {required_stone} 灵石！"
        
        valid, error = self._validate_sect_name(sect_name)
        if not valid: return False, error
        if await self.db.ext.get_sect_by_name(sect_name): return False, f"❌ 宗门名称『{sect_name}』已被使用！"
        
        player.gold -= required_stone
        await self.db.update_player(player)
        
        new_sect = Sect(
            sect_id=0, sect_name=sect_name, sect_owner=user_id, sect_scale=100, sect_used_stone=0,
            sect_fairyland=0, sect_materials=100, mainbuff="0", secbuff="0", elixir_room_level=0,
            sect_level=1, sect_exp=0, sect_war_cd_until=0,
            sect_war_protect_until=0, sect_tournament_cd_until=0,
        )
        sect_id = await self.db.ext.create_sect(new_sect)
        await self.db.ext.update_player_sect_info(user_id, sect_id, 0)
        
        buff_info = await self.db.ext.get_buff_info(user_id)
        if not buff_info: await self.db.ext.create_buff_info(user_id)
        
        return True, f"✨ 恭喜！你成功创建了宗门『{sect_name}』，成为一代宗主！"
    
    async def join_sect(self, user_id: str, sect_name: str) -> Tuple[bool, str]:
        """加入宗门"""
        player = await self.db.get_player_by_id(user_id)
        if not player: return False, "❌ 你还未踏入修仙之路！"
        if player.sect_id != 0: return False, "❌ 你已经加入了宗门！请先退出当前宗门。"
        
        sect = await self.db.ext.get_sect_by_name(sect_name)
        if not sect: return False, f"❌ 未找到宗门『{sect_name}』！"
        
        await self.db.ext.update_player_sect_info(user_id, sect.sect_id, 4)
        buff_info = await self.db.ext.get_buff_info(user_id)
        if not buff_info: await self.db.ext.create_buff_info(user_id)
        return True, f"✨ 你成功加入了宗门『{sect_name}』，成为外门弟子！"
    
    async def leave_sect(self, user_id: str) -> Tuple[bool, str]:
        """退出宗门"""
        player = await self.db.get_player_by_id(user_id)
        if not player: return False, "❌ 你还未踏入修仙之路！"
        if player.sect_id == 0: return False, "❌ 你还未加入任何宗门！"
        if getattr(player, "sect_debt", 0) > 0:
            return False, f"❌ 你还有 {player.sect_debt:,} 灵石宗门欠款，还清后才能退出宗门。"
        
        sect = await self.db.ext.get_sect_by_id(player.sect_id)
        if sect and sect.sect_owner == user_id: return False, "❌ 宗主无法直接退出宗门！请先传位或解散宗门。"
        sect_name = sect.sect_name if sect else "未知宗门"
        
        await self._cancel_orders_on_leave(user_id, player.sect_id)
        await self.db.ext.update_player_sect_info(user_id, 0, 0)
        player.sect_id = 0
        player.sect_position = 0
        player.sect_contribution = 0
        await self.db.update_player(player)
        return True, f"✨ 你已退出宗门『{sect_name}』！贡献度已清零。"
    
    async def disband_sect(self, user_id: str, confirm_text: str = "") -> Tuple[bool, str]:
        """宗主解散宗门；需要二次确认。"""
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！"
        if player.sect_id == 0:
            return False, "❌ 你还未加入任何宗门！"
        sect = await self.db.ext.get_sect_by_id(player.sect_id)
        if not sect:
            return False, "❌ 宗门信息异常！"
        if sect.sect_owner != user_id:
            return False, "❌ 只有宗主才能解散宗门！"

        members = await self.db.ext.get_sect_members(sect.sect_id)
        if (confirm_text or "").strip() != "确认":
            return False, (
                f"⚠️ 你正在解散宗门【{sect.sect_name}】\n"
                f"当前成员：{len(members)} 人\n"
                "解散后会清空所有成员的宗门归属、贡献和欠款，"
                "宗门资金/建筑/商店/大比记录不会保留。\n"
                "若确定，请发送：\n"
                f"/解散宗门 确认"
            )

        await self.db.ext.delete_sect(sect.sect_id)
        return True, (
            f"🏳️ 宗门【{sect.sect_name}】已解散。\n"
            f"原成员 {len(members)} 人已恢复无宗门状态。"
        )

    async def donate_to_sect(self, user_id: str, stone_amount: int) -> Tuple[bool, str]:
        """宗门捐献"""
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0: return False, "❌ 你还未加入宗门！"
        if stone_amount <= 0: return False, "❌ 捐献数量必须大于0！"
        if player.gold < stone_amount: return False, f"❌ 你的灵石不足！当前拥有 {player.gold} 灵石。"
        
        # 原子增减：避免全字段覆盖回滚其他并发修改
        await self.db.conn.execute(
            "UPDATE players SET gold = MAX(0, gold - ?), sect_contribution = sect_contribution + ? WHERE user_id = ?",
            (stone_amount, stone_amount, user_id)
        )
        await self.db.conn.commit()
        # 同步内存对象，供后续消息展示
        player.gold -= stone_amount
        player.sect_contribution += stone_amount
        await self.db.ext.donate_to_sect(player.sect_id, stone_amount)
        old_level, new_level, leveled = await self.add_sect_exp(player.sect_id, stone_amount)
        order_msg = await self.record_order_progress(user_id, "donate", stone_amount)
        level_msg = self.get_level_up_text(old_level, new_level) if leveled else ""
        return True, (
            f"✨ 捐献成功！消耗 {stone_amount} 灵石，宗门获得 {stone_amount * 10} 建设度！\n"
            f"你的宗门贡献度：{player.sect_contribution}"
            f"{level_msg}{order_msg}"
        )

    async def get_sect_info(self, user_id: str) -> Tuple[bool, str, Optional[Dict]]:
        """获取宗门信息"""
        player = await self.db.get_player_by_id(user_id)
        if not player: return False, "❌ 你还未踏入修仙之路！", None
        if player.sect_id == 0: return False, "❌ 你还未加入宗门！", None
        
        sect = await self.db.ext.get_sect_by_id(player.sect_id)
        if not sect: return False, "❌ 宗门信息异常！", None
        
        owner = await self.db.get_player_by_id(sect.sect_owner)
        owner_name = owner.user_name if owner and owner.user_name else sect.sect_owner
        members = await self.db.ext.get_sect_members(sect.sect_id)
        
        info_msg = (
            f"🏛️ 宗门信息\n━━━━━━━━━━━━━━━\n"
            f"宗门名称：{sect.sect_name}\n宗主：{owner_name}\n"
            f"宗门等级：Lv.{self.get_sect_level(sect)}（经验 {self.get_sect_exp(sect):,}）\n"
            f"建设度：{sect.sect_scale}\n"
            f"宗门灵石：{sect.sect_used_stone}\n宗门资材：{sect.sect_materials}\n丹房等级：{sect.elixir_room_level}\n"
            f"成员数量：{len(members)}人\n"
            f"\n你的职位：{self.POSITIONS.get(player.sect_position, '未知')}\n"
            f"你的贡献：{player.sect_contribution}\n"
            f"你的宗门欠款：{getattr(player, 'sect_debt', 0):,} 灵石"
        )
        return True, info_msg, {"sect": sect, "player_position": player.sect_position, "player_contribution": player.sect_contribution, "member_count": len(members)}
    
    async def list_all_sects(self) -> Tuple[bool, str]:
        """获取所有宗门列表"""
        sects = await self.db.ext.get_all_sects()
        if not sects: return False, "❌ 当前还没有任何宗门！"
        
        msg = "🏛️ 宗门列表\n━━━━━━━━━━━━━━━\n"
        for idx, sect in enumerate(sects[:10], 1):
            owner = await self.db.get_player_by_id(sect.sect_owner)
            owner_name = owner.user_name if owner and owner.user_name else "未知"
            members = await self.db.ext.get_sect_members(sect.sect_id)
            msg += (
                f"{idx}. 【{sect.sect_name}】\n"
                f"   宗主：{owner_name} | 等级：Lv.{self.get_sect_level(sect)}\n"
                f"   建设度：{sect.sect_scale} | 成员：{len(members)}人\n\n"
            )
        return True, msg
    
    async def change_position(self, operator_id: str, target_id: str, new_position: int) -> Tuple[bool, str]:
        """变更宗门职位"""
        operator = await self.db.get_player_by_id(operator_id)
        if not operator or operator.sect_id == 0: return False, "❌ 你还未加入宗门！"
        if operator.sect_position != 0: return False, "❌ 只有宗主才能变更职位！"
        
        target = await self.db.get_player_by_id(target_id)
        if not target or target.sect_id != operator.sect_id: return False, "❌ 目标用户不在你的宗门！"
        if target_id == operator_id: return False, "❌ 无法变更自己的职位！"
        if new_position not in self.POSITIONS: return False, "❌ 无效的职位！"
        if new_position == 0: return False, "❌ 无法直接任命宗主！请使用传位功能。"
        
        await self.db.ext.update_player_sect_info(target_id, target.sect_id, new_position)
        return True, f"✨ 已将 {target.user_name or target_id} 的职位变更为：{self.POSITIONS[new_position]}"
    
    async def transfer_ownership(self, current_owner_id: str, new_owner_id: str) -> Tuple[bool, str]:
        """宗主传位"""
        current_owner = await self.db.get_player_by_id(current_owner_id)
        if not current_owner or current_owner.sect_id == 0: return False, "❌ 你还未加入宗门！"
        
        sect = await self.db.ext.get_sect_by_id(current_owner.sect_id)
        if not sect or sect.sect_owner != current_owner_id: return False, "❌ 你不是宗主！"
        
        new_owner = await self.db.get_player_by_id(new_owner_id)
        if not new_owner or new_owner.sect_id != current_owner.sect_id: return False, "❌ 目标用户不在你的宗门！"
        if new_owner_id == current_owner_id: return False, "❌ 无法传位给自己！"
        
        sect.sect_owner = new_owner_id
        await self.db.ext.update_sect(sect)
        await self.db.ext.update_player_sect_info(new_owner_id, sect.sect_id, 0)
        await self.db.ext.update_player_sect_info(current_owner_id, sect.sect_id, 1)
        
        return True, f"✨ 宗主之位已传给 {new_owner.user_name or new_owner_id}！你现在是长老。"
    
    async def kick_member(self, operator_id: str, target_id: str) -> Tuple[bool, str]:
        """踢出宗门成员"""
        operator = await self.db.get_player_by_id(operator_id)
        if not operator or operator.sect_id == 0: return False, "❌ 你还未加入宗门！"
        if operator.sect_position not in [0, 1]: return False, "❌ 只有宗主和长老才能踢出成员！"
        
        target = await self.db.get_player_by_id(target_id)
        if not target or target.sect_id != operator.sect_id: return False, "❌ 目标用户不在你的宗门！"
        if target_id == operator_id: return False, "❌ 无法踢出自己！"
        if operator.sect_position == 1 and target.sect_position <= 3: return False, "❌ 长老只能踢出外门弟子！"
        if target.sect_position == 0: return False, "❌ 无法踢出宗主！"
        if getattr(target, "sect_debt", 0) > 0:
            return False, f"❌ 目标还有 {target.sect_debt:,} 灵石宗门欠款，不能踢出。"
        
        await self._cancel_orders_on_leave(target_id, operator.sect_id)
        await self.db.ext.update_player_sect_info(target_id, 0, 0)  # 与主动退宗保持一致：无宗门时职位归零
        target.sect_contribution = 0
        await self.db.update_player(target)
        return True, f"✨ 已将 {target.user_name or target_id} 踢出宗门！"

    async def perform_sect_task(self, user_id: str) -> Tuple[bool, str]:
        """执行宗门任务"""
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0: return False, "❌ 你还未加入宗门！"
        
        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd:
            await self.db.ext.create_user_cd(user_id)
            user_cd = await self.db.ext.get_user_cd(user_id)
            
        current_time = int(time.time())
        if user_cd.type == UserStatus.SECT_TASK and user_cd.scheduled_time and current_time < user_cd.scheduled_time:
            return False, f"❌ 宗门任务冷却中！还需 {(user_cd.scheduled_time - current_time)//60} 分钟。"
        elif user_cd.type == UserStatus.SECT_TASK:
            await self.db.ext.set_user_free(user_id)

        await self.db.ext.set_user_busy(user_id, UserStatus.SECT_TASK, current_time + 3600)
        return True, "✨ 你开始执行宗门任务！任务需要1小时完成，请在完成后使用 /完成宗门任务 领取奖励。"

    async def finish_sect_task(self, user_id: str) -> Tuple[bool, str]:
        """完成宗门任务"""
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0: return False, "❌ 你还未加入宗门！"

        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd: return False, "❌ 你当前不在宗门任务中！"

        if user_cd.type == UserStatus.SECT_TASK:
            now = int(time.time())
            if user_cd.scheduled_time and now < user_cd.scheduled_time:
                return False, f"❌ 宗门任务尚未完成！还需 {(user_cd.scheduled_time - now)//60} 分。"

            contribution_gain = random.randint(10, 30)

            # 演武场：宗门建筑，宗门任务奖励+10%/级
            arena_note = ""
            sect = None
            try:
                sect = await self.db.ext.get_sect_by_id(player.sect_id)
                levels = self.get_buildings(sect) if sect else {}
                arena_lv = int(levels.get("演武场", 0))
                if arena_lv > 0:
                    contribution_gain = int(contribution_gain * (1 + 0.10 * arena_lv))
                    arena_note = f"\n🏟️ 演武场 Lv.{arena_lv} 生效：奖励+{10 * arena_lv}%"
            except Exception:
                pass

            player.sect_contribution += contribution_gain
            await self.db.update_player(player)

            level_msg = ""
            if sect:
                old_level, new_level, leveled = self.apply_sect_exp(
                    sect, contribution_gain * 10
                )
                sect.sect_materials += contribution_gain * 10
                await self.db.ext.update_sect(sect)
                if leveled:
                    level_msg = self.get_level_up_text(old_level, new_level)

            order_msg = await self.record_order_progress(user_id, "task", 1)
            await self.db.ext.set_user_free(user_id)
            return True, (
                f"✨ 宗门任务完成！\n获得贡献：{contribution_gain}\n"
                f"宗门资材：+{contribution_gain * 10}{arena_note}{level_msg}{order_msg}"
            )
        else:
            await self.db.ext.set_user_free(user_id)
            return True, "✅ 状态异常已修复！你的状态已恢复正常。"

    async def abandon_sect_task(self, user_id: str) -> Tuple[bool, str]:
        """放弃宗门任务：宗门任务只给贡献，按固定灵石违约金处理。"""
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！"

        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd or user_cd.type != UserStatus.SECT_TASK:
            return False, "❌ 你当前不在宗门任务中！"

        cost, currency = calc_abandon_penalty(0, 0)
        currency_label = "灵气" if currency == "qi" else "灵石"
        ok, msg = await charge_abandon_penalty(player, self.db, cost, currency)
        if not ok:
            return False, msg

        await self.db.ext.set_user_free(user_id)
        return True, (
            "🏳️ 你放弃了宗门任务。\n"
            f"向宗门支付违约金：{cost:,} {currency_label}\n"
            "本次宗门任务无贡献、无宗门资材。"
        )

    # ===== 宗门命令 =====

    def _position_limit(self, position: int, sect_level: int) -> int:
        if position == 0:
            return 3 + sect_level
        if position == 1:
            return 1 + sect_level // 2
        if position == 2:
            return 1
        return 0

    async def create_order(
        self, issuer_id: str, target_id: str, order_type: str,
        target_value: int, hours: int, origin: str = ""
    ) -> Tuple[bool, str]:
        operator = await self.db.get_player_by_id(issuer_id)
        target = await self.db.get_player_by_id(target_id)
        if not operator or operator.sect_id == 0:
            return False, "❌ 你还未加入宗门！"
        if not target or target.sect_id != operator.sect_id:
            return False, "❌ 目标不在你的宗门！"
        if issuer_id == target_id:
            return False, "❌ 不能给自己下命令。"
        if operator.sect_position not in (0, 1, 2):
            return False, "❌ 只有宗主、长老、亲传可以下命令。"
        if target.sect_position <= operator.sect_position:
            return False, "❌ 只能向职位更低的成员下命令。"

        type_map = {"捐献": "donate", "宗门任务": "task"}
        if order_type not in type_map:
            return False, "❌ 命令类型只能是：捐献 / 宗门任务。"
        if target_value <= 0:
            return False, "❌ 命令目标数量必须大于 0。"
        if hours < 1 or hours > SECT_ORDER_MAX_HOURS:
            return False, f"❌ 命令期限必须在 1–{SECT_ORDER_MAX_HOURS} 小时之间。"

        sect = await self.db.ext.get_sect_by_id(operator.sect_id)
        if not sect:
            return False, "❌ 宗门信息异常！"

        async with self.db.conn.execute(
            "SELECT COUNT(*) FROM sect_orders WHERE issuer_id = ? AND status = 'active'",
            (issuer_id,),
        ) as cursor:
            active_count = (await cursor.fetchone())[0]
        limit = self._position_limit(operator.sect_position, self.get_sect_level(sect))
        if active_count >= limit:
            return False, f"❌ 你的进行中命令已达上限（{limit} 条）。"

        async with self.db.conn.execute(
            "SELECT id FROM sect_orders WHERE target_id = ? AND status = 'active'",
            (target_id,),
        ) as cursor:
            if await cursor.fetchone():
                return False, "❌ 该成员已有进行中的宗门命令。"

        now = int(time.time())
        await self.db.conn.execute(
            "INSERT INTO sect_orders "
            "(sect_id, issuer_id, target_id, order_type, target_value, progress, "
            " penalty_gold, created_at, deadline, status, origin) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 'active', ?)",
            (
                operator.sect_id, issuer_id, target_id, type_map[order_type],
                target_value, SECT_ORDER_PENALTY, now, now + hours * 3600, origin,
            ),
        )
        await self.db.conn.commit()
        return True, (
            f"📜 宗门命令已下达！\n"
            f"目标：{target.user_name or target_id}\n"
            f"内容：{order_type} {target_value}\n"
            f"期限：{hours} 小时\n"
            f"未完成罚款：{SECT_ORDER_PENALTY:,} 灵石（不足记欠款）"
        )

    async def list_orders(self, user_id: str) -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0:
            return False, "❌ 你还未加入宗门！"
        async with self.db.conn.execute(
            "SELECT * FROM sect_orders WHERE sect_id = ? AND status = 'active' "
            "ORDER BY id DESC",
            (player.sect_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        lines = [
            "📜 宗门命令",
            f"你的宗门欠款：{getattr(player, 'sect_debt', 0):,} 灵石",
            "━━━━━━━━━━━━━━━",
        ]
        if not rows:
            lines.append("当前没有进行中的命令。")
        for row in rows:
            order = dict(row)
            target = await self.db.get_player_by_id(order["target_id"])
            target_name = target.user_name if target and target.user_name else order["target_id"]
            remaining = max(0, order["deadline"] - int(time.time()))
            type_name = "捐献" if order["order_type"] == "donate" else "宗门任务"
            lines.append(
                f"#{order['id']} {type_name} {order['target_value']} → {target_name}\n"
                f"   进度：{order['progress']}/{order['target_value']} "
                f"| 剩余：{remaining // 3600}小时{(remaining % 3600) // 60}分"
            )
        lines.append("━━━━━━━━━━━━━━━")
        lines.append("💡 /宗门命令撤销 <编号> 可撤销自己下达的命令")
        return True, "\n".join(lines)

    async def cancel_order(self, user_id: str, order_id: int) -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0:
            return False, "❌ 你还未加入宗门！"
        async with self.db.conn.execute(
            "SELECT * FROM sect_orders WHERE id = ?", (order_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False, "❌ 没有这个命令。"
        order = dict(row)
        if order["sect_id"] != player.sect_id:
            return False, "❌ 这不是你宗门的命令。"
        if order["status"] != "active":
            return False, "❌ 该命令已经结束。"
        if order["issuer_id"] != user_id and player.sect_position != 0:
            return False, "❌ 只有命令发起人或宗主可以撤销。"
        await self.db.conn.execute(
            "UPDATE sect_orders SET status = 'cancelled' WHERE id = ?", (order_id,)
        )
        await self.db.conn.commit()
        return True, f"✅ 已撤销宗门命令 #{order_id}。"

    async def record_order_progress(self, user_id: str, order_type: str, amount: int) -> str:
        """记录命令进度，返回给玩家的提示文本。"""
        async with self.db.conn.execute(
            "SELECT * FROM sect_orders WHERE target_id = ? AND order_type = ? "
            "AND status = 'active'",
            (user_id, order_type),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return ""
        messages = []
        now = int(time.time())
        for row in rows:
            order = dict(row)
            if order["deadline"] < now:
                continue
            new_progress = int(order["progress"]) + max(0, int(amount))
            if new_progress >= int(order["target_value"]):
                await self.db.conn.execute(
                    "UPDATE sect_orders SET progress = ?, status = 'completed' WHERE id = ?",
                    (int(order["target_value"]), order["id"]),
                )
                messages.append(f"\n✅ 宗门命令 #{order['id']} 已完成。")
            else:
                await self.db.conn.execute(
                    "UPDATE sect_orders SET progress = ? WHERE id = ?",
                    (new_progress, order["id"]),
                )
        await self.db.conn.commit()
        return "".join(messages)

    async def _cancel_orders_on_leave(self, user_id: str, sect_id: int):
        await self.db.conn.execute(
            "UPDATE sect_orders SET status = 'cancelled' "
            "WHERE sect_id = ? AND status = 'active' AND (issuer_id = ? OR target_id = ?)",
            (sect_id, user_id, user_id),
        )
        await self.db.conn.execute(
            "DELETE FROM sect_tournament_entries WHERE user_id = ? AND tournament_id IN "
            "(SELECT id FROM sect_tournaments WHERE sect_id = ? "
            "AND status IN ('registering','running'))",
            (user_id, sect_id),
        )
        await self.db.conn.commit()

    async def expire_orders(self) -> List[Tuple[str, str]]:
        """处理到期命令，返回 [(origin, 提示文本)]。"""
        now = int(time.time())
        async with self.db.conn.execute(
            "SELECT * FROM sect_orders WHERE status = 'active' AND deadline <= ?",
            (now,),
        ) as cursor:
            rows = await cursor.fetchall()
        results: List[Tuple[str, str]] = []
        for row in rows:
            order = dict(row)
            if int(order["progress"]) >= int(order["target_value"]):
                await self.db.conn.execute(
                    "UPDATE sect_orders SET status = 'completed' WHERE id = ?",
                    (order["id"],),
                )
                await self.db.conn.commit()
                continue

            target = await self.db.get_player_by_id(order["target_id"])
            if not target or target.sect_id != order["sect_id"]:
                await self.db.conn.execute(
                    "UPDATE sect_orders SET status = 'cancelled' WHERE id = ?",
                    (order["id"],),
                )
                await self.db.conn.commit()
                continue

            loss = min(max(0, int(target.gold)), SECT_ORDER_PENALTY)
            debt = SECT_ORDER_PENALTY - loss
            target.gold -= loss
            target.sect_debt = int(getattr(target, "sect_debt", 0)) + debt
            await self.db.update_player(target)
            # 单独写欠款，避免全字段覆盖把自动还款写回旧值
            await self.db.conn.execute(
                "UPDATE players SET sect_debt = ? WHERE user_id = ?",
                (target.sect_debt, target.user_id),
            )
            await self.db.conn.execute(
                "UPDATE sects SET sect_used_stone = sect_used_stone + ? WHERE sect_id = ?",
                (loss, order["sect_id"]),
            )
            await self.db.conn.execute(
                "UPDATE sect_orders SET status = 'failed' WHERE id = ?",
                (order["id"],),
            )
            await self.db.conn.commit()

            target_name = target.user_name or order["target_id"]
            text = (
                f"⚖️ 宗门命令 #{order['id']} 到期未完成\n"
                f"目标：{target_name}\n"
                f"扣除灵石：{loss:,}\n"
                f"新增欠款：{debt:,}\n"
                f"当前欠款：{target.sect_debt:,}"
            )
            results.append((order.get("origin", ""), text))
        return results

    async def handle_owner_death(self, sect_id: int, dead_owner_id: str) -> Tuple[bool, str]:
        """处理宗主死亡，自动传位或解散宗门"""
        members = await self.db.ext.get_sect_members(sect_id)
        remaining = [m for m in members if m.user_id != dead_owner_id]
        
        if not remaining:
            await self.db.ext.delete_sect(sect_id)
            return True, "宗门已解散"
        
        remaining.sort(key=lambda m: (m.sect_position, -m.sect_contribution))
        new_owner = remaining[0]
        
        sect = await self.db.ext.get_sect_by_id(sect_id)
        if sect:
            sect.sect_owner = new_owner.user_id
            await self.db.ext.update_sect(sect)
            await self.db.ext.update_player_sect_info(new_owner.user_id, sect_id, 0)
        
        return True, f"宗主之位已传给{new_owner.user_name or new_owner.user_id}"

    # ===== v6.6 宗门大比 =====
    async def start_sect_battle(self) -> Tuple[bool, str]:
        """开启宗门大比"""
        is_active = await self.db.ext.get_system_config("sect_battle_active")
        if is_active == "1":
            return False, "❌ 宗门大比正在进行中。"
        await self.db.ext.set_system_config("sect_battle_active", "1")
        return True, "⚔️ 宗门大比正式开启！所有修士可通过 /大比切磋 @某人 争夺荣誉！"

    async def end_sect_battle(self) -> Tuple[bool, str]:
        """结束宗门大比并结算"""
        is_active = await self.db.ext.get_system_config("sect_battle_active")
        if is_active != "1":
            return False, "❌ 当前没有进行中的大比。"
        await self.db.ext.set_system_config("sect_battle_active", "0")
        return True, "🏁 宗门大比结束！天道压制解除，全服已恢复正常状态。"

    async def record_battle_result(self, user_id: str, is_winner: bool = True) -> str:
        """记录大比结果，胜利者发称号，失败/平局发保底"""
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0:
            return ""
        
        title_msg = ""
        
        if is_winner:
            # ===== 胜者逻辑 =====
            win_key = f"sect_battle_wins_{user_id}"
            current_wins_str = await self.db.ext.get_system_config(win_key)
            current_wins = int(current_wins_str) if current_wins_str and current_wins_str.isdigit() else 0
            current_wins += 1
            await self.db.ext.set_system_config(win_key, str(current_wins))

            if current_wins == 1 and not player.title:
                player.title = "初露锋芒"
                title_msg = "\n👑 恭喜你！赢得大比首胜，获得荣誉称号【初露锋芒】！"
            elif current_wins >= 3 and player.title not in ["一代宗师", "灭世魔神"]:
                player.title = "一代宗师"
                title_msg = "\n👑 恭喜你！累计胜场达到 3 场，获得荣誉称号【一代宗师】！"

            player.sect_contribution += 100
            # 给宗门加积分
            score_key = f"sect_score_{player.sect_id}"
            current_score = await self.db.ext.get_system_config(score_key)
            new_score = int(current_score) + 10 if current_score else 10
            await self.db.ext.set_system_config(score_key, str(new_score))
            
            await self.db.update_player(player)
            return f"\n🏅 你为大比赢得一场胜利！宗门贡献+100，宗门积分+10。{title_msg}"
            
        else:
            # ===== 败者/平局保底逻辑 =====
            player.sect_contribution += 20
            score_key = f"sect_score_{player.sect_id}"
            current_score = await self.db.ext.get_system_config(score_key)
            new_score = int(current_score) + 2 if current_score else 2
            await self.db.ext.set_system_config(score_key, str(new_score))
            
            await self.db.update_player(player)
            return "" # 败者不弹额外提示，避免刷屏

    # ===== 宗门大比（宗主开启，30分钟报名） =====

    async def start_tournament(self, user_id: str, origin: str = "") -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0:
            return False, "❌ 你还未加入宗门！"
        if player.sect_position != 0:
            return False, "❌ 只有宗主才能开启宗门大比！"
        sect = await self.db.ext.get_sect_by_id(player.sect_id)
        if not sect:
            return False, "❌ 宗门信息异常！"
        now = int(time.time())
        if int(getattr(sect, "sect_tournament_cd_until", 0) or 0) > now:
            remain = int(sect.sect_tournament_cd_until) - now
            return False, f"❌ 宗门大比冷却中，还需 {remain // 3600} 小时 {(remain % 3600) // 60} 分。"
        async with self.db.conn.execute(
            "SELECT id FROM sect_tournaments WHERE sect_id = ? "
            "AND status IN ('registering','running')",
            (player.sect_id,),
        ) as cursor:
            if await cursor.fetchone():
                return False, "❌ 本宗门已有进行中的大比。"
        await self.db.conn.execute(
            "INSERT INTO sect_tournaments "
            "(sect_id, host_id, origin, status, registration_end) "
            "VALUES (?, ?, ?, 'registering', ?)",
            (player.sect_id, user_id, origin, now + SECT_TOURNAMENT_DURATION),
        )
        await self.db.conn.commit()
        return True, (
            f"⚔️ 宗门大比开启！\n"
            f"报名时间：30 分钟\n"
            f"请本宗门成员发送 /宗门大比报名\n"
            f"报名结束后自动抽签开战，冠军奖励 10,000 灵石。"
        )

    async def register_tournament(self, user_id: str) -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0:
            return False, "❌ 你还未加入宗门！"
        async with self.db.conn.execute(
            "SELECT * FROM sect_tournaments WHERE sect_id = ? AND status = 'registering' "
            "ORDER BY id DESC LIMIT 1",
            (player.sect_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False, "❌ 当前没有开放报名的宗门大比。"
        tournament = dict(row)
        if int(tournament["registration_end"]) <= int(time.time()):
            return False, "❌ 报名已经截止。"
        async with self.db.conn.execute(
            "SELECT COUNT(*) FROM sect_tournament_entries WHERE tournament_id = ?",
            (tournament["id"],),
        ) as cursor:
            count = (await cursor.fetchone())[0]
        if count >= SECT_TOURNAMENT_MAX_PLAYERS:
            return False, f"❌ 报名人数已满（{SECT_TOURNAMENT_MAX_PLAYERS} 人）。"
        cursor = await self.db.conn.execute(
            "INSERT OR IGNORE INTO sect_tournament_entries (tournament_id, user_id, seed) "
            "VALUES (?, ?, ?)",
            (tournament["id"], user_id, count + 1),
        )
        if cursor.rowcount == 0:
            await self.db.conn.commit()
            return False, "❌ 你已经报名过了。"
        await self.db.conn.commit()
        return True, f"✅ 报名成功！当前报名人数：{min(count + 1, SECT_TOURNAMENT_MAX_PLAYERS)} 人。"

    async def get_tournament_status(self, user_id: str) -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0:
            return False, "❌ 你还未加入宗门！"
        async with self.db.conn.execute(
            "SELECT * FROM sect_tournaments WHERE sect_id = ? ORDER BY id DESC LIMIT 1",
            (player.sect_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False, "📭 本宗门还没有开启过宗门大比。"
        t = dict(row)
        async with self.db.conn.execute(
            "SELECT COUNT(*) FROM sect_tournament_entries WHERE tournament_id = ?",
            (t["id"],),
        ) as cursor:
            count = (await cursor.fetchone())[0]
        if t["status"] == "registering":
            remain = max(0, int(t["registration_end"]) - int(time.time()))
            return True, (
                f"⚔️ 宗门大比报名中\n报名人数：{count}/{SECT_TOURNAMENT_MAX_PLAYERS}\n"
                f"剩余时间：{remain // 60} 分 {remain % 60} 秒"
            )
        if t["status"] == "finished":
            winner = await self.db.get_player_by_id(t["winner_id"])
            winner_name = winner.user_name if winner and winner.user_name else t["winner_id"]
            return True, f"🏆 上届宗门大比冠军：{winner_name}\n冠军奖励：10,000 灵石"
        return True, f"⚔️ 宗门大比状态：{t['status']}"

    async def _build_combat_stats(self, user_id: str):
        from .combat_manager import CombatManager, CombatStats
        from ..core import EquipmentManager
        from .pet_manager import PetManager
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return None
        cm = CombatManager()
        hp, mp = cm.calculate_hp_mp(player.experience)
        atk = cm.calculate_atk(
            player.experience, player.atkpractice, 0,
            player.physical_damage, player.magic_damage, player.cultivation_type,
        )
        em = EquipmentManager(self.db, self.config_manager)
        effects = em.get_combat_effects(player)
        pet = await PetManager(self.db).get_pet(user_id)
        if pet:
            for k, v in PetManager(self.db).get_pet_combat_effects(pet).items():
                effects[k] = effects.get(k, 0) + v
        return CombatStats(
            user_id=user_id, name=player.user_name or user_id,
            hp=hp, max_hp=hp, mp=mp, max_mp=mp, atk=atk, effects=effects,
            element=(cm.get_element_from_name(player.main_technique)
                     or cm.get_element_from_name(player.weapon)),
        )

    async def _run_duel(self, uid1: str, uid2: str, balanced: bool = False) -> Optional[dict]:
        from .combat_manager import CombatManager
        p1 = await self._build_combat_stats(uid1)
        p2 = await self._build_combat_stats(uid2)
        if not p1 or not p2:
            return None
        cm = CombatManager()
        if balanced:
            return cm.player_vs_player_balanced(p1, p2)
        return cm.player_vs_player(p1, p2)

    async def finish_due_tournaments(self) -> List[Tuple[str, str]]:
        now = int(time.time())
        async with self.db.conn.execute(
            "SELECT * FROM sect_tournaments WHERE status = 'registering' AND registration_end <= ?",
            (now,),
        ) as cursor:
            rows = await cursor.fetchall()
        results = []
        for row in rows:
            results.append(await self._finish_tournament(dict(row)))
        return [r for r in results if r]

    async def _finish_tournament(self, tournament: dict) -> Optional[Tuple[str, str]]:
        tid = tournament["id"]
        async with self.db.conn.execute(
            "SELECT e.user_id FROM sect_tournament_entries e "
            "JOIN players p ON p.user_id = e.user_id "
            "WHERE e.tournament_id = ? AND p.sect_id = ?",
            (tid, tournament["sect_id"]),
        ) as cursor:
            entrants = [row[0] for row in await cursor.fetchall()]
        if len(entrants) < 2:
            await self.db.conn.execute(
                "UPDATE sect_tournaments SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (int(time.time()), tid),
            )
            await self.db.conn.commit()
            return tournament.get("origin", ""), "⚔️ 宗门大比报名人数不足 2 人，已取消。"

        random.shuffle(entrants)
        bracket = []
        round_no = 1
        while len(entrants) > 1:
            next_round = []
            round_logs = []
            random.shuffle(entrants)
            i = 0
            while i < len(entrants):
                if i + 1 >= len(entrants):
                    next_round.append(entrants[i])
                    round_logs.append(f"{entrants[i]} 轮空")
                    i += 1
                    continue
                a, b = entrants[i], entrants[i + 1]
                result = await self._run_duel(a, b, balanced=True)
                if not result:
                    next_round.append(a)
                    round_logs.append(f"{a} 因对手数据异常晋级")
                    i += 2
                    continue
                winner = result["winner"]
                if winner == "平局":
                    winner = random.choice([a, b])
                next_round.append(winner)
                round_logs.append(f"{a} vs {b} → {winner}")
                i += 2
            bracket.append({"round": round_no, "matches": round_logs})
            entrants = next_round
            round_no += 1

        winner_id = entrants[0] if entrants else ""
        now = int(time.time())
        await self.db.conn.execute(
            "UPDATE sect_tournaments SET status = 'finished', finished_at = ?, "
            "winner_id = ?, bracket_json = ? WHERE id = ?",
            (now, winner_id, json.dumps(bracket, ensure_ascii=False), tid),
        )
        await self.db.conn.execute(
            "UPDATE players SET gold = gold + ? WHERE user_id = ?", (10000, winner_id)
        )
        await self.db.conn.execute(
            "UPDATE sects SET sect_tournament_cd_until = ? WHERE sect_id = ?",
            (now + SECT_TOURNAMENT_COOLDOWN, tournament["sect_id"]),
        )
        await self.db.conn.commit()

        await self.add_sect_exp(tournament["sect_id"], 50 * len(entrants) + 500)
        winner = await self.db.get_player_by_id(winner_id)
        winner_name = winner.user_name if winner and winner.user_name else winner_id
        return tournament.get("origin", ""), (
            f"🏆 宗门大比结束！\n冠军：{winner_name}\n奖励：10,000 灵石\n"
            f"共 {len(bracket)} 轮，详细战报已记录。"
        )

    # ===== 宗门战争 =====

    def _war_sort(self, members: List[Player]) -> List[Player]:
        return sorted(
            members,
            key=lambda p: (-int(p.sect_position), -int(p.level_index), -int(p.sect_contribution)),
        )

    async def get_war_status(self, user_id: str) -> Tuple[bool, str]:
        player = await self.db.get_player_by_id(user_id)
        if not player or player.sect_id == 0:
            return False, "❌ 你还未加入宗门！"
        sect = await self.db.ext.get_sect_by_id(player.sect_id)
        if not sect:
            return False, "❌ 宗门信息异常！"
        now = int(time.time())
        lines = [f"⚔️ 【{sect.sect_name}】宗门战争状态", "━━━━━━━━━━━━━━━"]
        cd = int(getattr(sect, "sect_war_cd_until", 0) or 0)
        protect = int(getattr(sect, "sect_war_protect_until", 0) or 0)
        lines.append(f"宣战冷却：{'无' if cd <= now else f'{cd - now} 秒'}")
        lines.append(f"被宣战保护：{'无' if protect <= now else f'{protect - now} 秒'}")
        async with self.db.conn.execute(
            "SELECT * FROM sect_wars WHERE attacker_id = ? OR defender_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (player.sect_id, player.sect_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            war = dict(row)
            attacker = await self.db.ext.get_sect_by_id(war["attacker_id"])
            defender = await self.db.ext.get_sect_by_id(war["defender_id"])
            winner = await self.db.ext.get_sect_by_id(war["winner_id"])
            lines.append(
                f"最近战报：{attacker.sect_name if attacker else '?'} vs "
                f"{defender.sect_name if defender else '?'}\n"
                f"胜者：{winner.sect_name if winner else '?'} | 掠夺：{war['gold_taken']:,} 灵石"
            )
        else:
            lines.append("最近没有宗门战记录。")
        return True, "\n".join(lines)

    async def declare_war(self, user_id: str, target_name: str, origin: str = "") -> Tuple[bool, str]:
        operator = await self.db.get_player_by_id(user_id)
        if not operator or operator.sect_id == 0:
            return False, "❌ 你还未加入宗门！"
        if operator.sect_position != 0:
            return False, "❌ 只有宗主才能宣战！"
        attacker = await self.db.ext.get_sect_by_id(operator.sect_id)
        defender = await self.db.ext.get_sect_by_name(target_name)
        if not attacker or not defender:
            return False, "❌ 找不到目标宗门。"
        if attacker.sect_id == defender.sect_id:
            return False, "❌ 不能向自己的宗门宣战。"
        now = int(time.time())
        if int(getattr(attacker, "sect_war_cd_until", 0) or 0) > now:
            return False, "❌ 本宗门还在宣战冷却中。"
        if int(getattr(defender, "sect_war_protect_until", 0) or 0) > now:
            return False, "❌ 目标宗门还在保护期内。"
        a_members = self._war_sort(await self.db.ext.get_sect_members(attacker.sect_id))
        d_members = self._war_sort(await self.db.ext.get_sect_members(defender.sect_id))
        if len(a_members) < 3 or len(d_members) < 3:
            return False, "❌ 双方宗门都必须至少有 3 名成员。"
        for sid in (attacker.sect_id, defender.sect_id):
            async with self.db.conn.execute(
                "SELECT id FROM sect_tournaments WHERE sect_id = ? "
                "AND status IN ('registering','running')", (sid,)
            ) as cursor:
                if await cursor.fetchone():
                    return False, "❌ 双方宗门有进行中的大比，不能开战。"
        if attacker.sect_used_stone < SECT_WAR_DECLARE_COST:
            return False, f"❌ 宣战需要 {SECT_WAR_DECLARE_COST:,} 宗门资金，当前不足。"

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE sects SET sect_used_stone = sect_used_stone - ? "
                "WHERE sect_id = ? AND sect_used_stone >= ?",
                (SECT_WAR_DECLARE_COST, attacker.sect_id, SECT_WAR_DECLARE_COST),
            )
            if cursor.rowcount == 0:
                await self.db.conn.rollback()
                return False, "❌ 宗门资金不足，宣战取消。"

            a_idx = d_idx = 0
            logs = []
            winner_side = ""
            while a_idx < len(a_members) and d_idx < len(d_members):
                a = a_members[a_idx]
                d = d_members[d_idx]
                result = await self._run_duel(a.user_id, d.user_id, balanced=False)
                if not result:
                    a_idx += 1
                    d_idx += 1
                    continue
                w = result["winner"]
                logs.append(f"{a.user_name or a.user_id} vs {d.user_name or d.user_id} → {w}")
                if w == a.user_id:
                    d_idx += 1
                elif w == d.user_id:
                    a_idx += 1
                else:
                    a_idx += 1
                    d_idx += 1
            if a_idx >= len(a_members) and d_idx >= len(d_members):
                # 双方同时无人：防守方守住宗门
                winner_side = "defender"
            elif d_idx >= len(d_members):
                winner_side = "attacker"
            else:
                winner_side = "defender"
            winner_sect = attacker if winner_side == "attacker" else defender
            loser_sect = defender if winner_side == "attacker" else attacker
            winner_members = a_members if winner_side == "attacker" else d_members
            loser_members = d_members if winner_side == "attacker" else a_members

            # 护山大阵：败方每级减少 5% 被掠夺，最高 25%
            levels = self.get_buildings(loser_sect)
            defense_lv = min(5, int(levels.get("护山大阵", 0)))
            rate = SECT_WAR_PLUNDER_RATE * (1 - min(0.25, 0.05 * defense_lv))

            taken = 0
            for loser in loser_members:
                fresh = await self.db.get_player_by_id(loser.user_id)
                if not fresh:
                    continue
                loss = min(int(fresh.gold), int(fresh.gold * rate))
                if loss <= 0:
                    continue
                await self.db.conn.execute(
                    "UPDATE players SET gold = gold - ? WHERE user_id = ?",
                    (loss, fresh.user_id),
                )
                taken += loss

            total_contrib = sum(max(0, int(m.sect_contribution)) for m in winner_members)
            if total_contrib <= 0:
                base = taken // max(1, len(winner_members))
                remainder = taken - base * len(winner_members)
                shares = [(m.user_id, base) for m in winner_members]
                if remainder > 0 and shares:
                    shares[0] = (shares[0][0], shares[0][1] + remainder)
            else:
                shares = []
                for m in winner_members:
                    share = int(taken * max(0, int(m.sect_contribution)) / total_contrib)
                    shares.append((m.user_id, share))
                distributed = sum(s for _, s in shares)
                if taken > distributed and shares:
                    top = max(winner_members, key=lambda x: int(x.sect_contribution))
                    shares = [(uid, s + (taken - distributed) if uid == top.user_id else s) for uid, s in shares]
            for uid, share in shares:
                if share > 0:
                    await self.db.conn.execute(
                        "UPDATE players SET gold = gold + ? WHERE user_id = ?", (share, uid)
                    )

            await self.db.conn.execute(
                "UPDATE sects SET sect_war_cd_until = ? WHERE sect_id = ?",
                (now + SECT_WAR_COOLDOWN, attacker.sect_id),
            )
            await self.db.conn.execute(
                "UPDATE sects SET sect_war_protect_until = ? WHERE sect_id = ?",
                (now + SECT_WAR_PROTECT, loser_sect.sect_id),
            )
            await self.db.conn.execute(
                "UPDATE sects SET sect_war_cd_until = MAX(sect_war_cd_until, ?) WHERE sect_id = ?",
                (now + SECT_WAR_WINNER_COOLDOWN, winner_sect.sect_id),
            )
            await self.db.conn.execute(
                "INSERT INTO sect_wars "
                "(attacker_id, defender_id, winner_id, gold_taken, log_json, created_at, origin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    attacker.sect_id, defender.sect_id, winner_sect.sect_id,
                    taken, json.dumps(logs, ensure_ascii=False), now, origin,
                ),
            )
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise

        await self.add_sect_exp(winner_sect.sect_id, 1000)
        await self.add_sect_exp(loser_sect.sect_id, 200)
        return True, (
            f"⚔️ 宗门战结束！\n"
            f"{attacker.sect_name} VS {defender.sect_name}\n"
            f"胜者：{winner_sect.sect_name}\n"
            f"掠夺灵石：{taken:,}\n"
            f"已按胜方成员贡献比例分配。\n"
            f"败方保护：24 小时"
        )