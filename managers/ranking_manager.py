# managers/ranking_manager.py
"""
排行榜系统管理器 - 处理各种排行榜逻辑
"""

from typing import Tuple, List, TYPE_CHECKING, Optional
from ..data.data_manager import DataBase
from ..managers.combat_manager import CombatManager

if TYPE_CHECKING:
    from ..config_manager import ConfigManager
    from ..models import Player

# 宗门职位映射（安全映射，防止索引越界）
POSITION_MAP = {
    0: "宗主",
    1: "长老",
    2: "亲传",
    3: "内门",
    4: "外门",
}

# 名称最大显示长度
MAX_NAME_LENGTH = 12


def _short_id(user_id) -> str:
    """安全获取短ID，防止非字符串类型报错"""
    if user_id is None:
        return "未知"
    return str(user_id)[:6]


def _safe_name(player: Optional["Player"], fallback_id) -> str:
    """安全获取玩家名称，带长度截断和特殊字符过滤"""
    if player and player.user_name:
        name = player.user_name
    else:
        name = f"道友{_short_id(fallback_id)}"
    
    # 过滤危险字符（@可能触发群通知）
    name = name.replace("@", "＠")
    # 截断过长名称
    if len(name) > MAX_NAME_LENGTH:
        name = name[:MAX_NAME_LENGTH] + "…"
    return name


class RankingManager:
    """排行榜系统管理器"""
    
    def __init__(self, db: DataBase, combat_mgr: CombatManager, config_manager: "ConfigManager"):
        self.db = db
        self.combat_mgr = combat_mgr
        self.config_manager = config_manager
        
        # 延迟导入，避免循环依赖，只初始化一次
        from ..core import EquipmentManager
        self.equipment_manager = EquipmentManager(self.db, self.config_manager)
    
    async def get_level_ranking(self, limit: int = 10) -> Tuple[bool, str]:
        """
        境界排行榜
        
        Args:
            limit: 显示数量
            
        Returns:
            (成功标志, 消息)
        """
        all_players = await self.db.get_all_players()
        
        if not all_players:
            return False, "❌ 暂无数据！"
        
        # 境界由 level_index 决定，修为只是突破门槛的累积值；
        # 仅按修为排序会出现「炼气期排在筑基期前面」的情况，须以境界优先、修为次之。
        sorted_players = sorted(
            all_players,
            key=lambda p: (p.level_index or 0, p.experience or 0),
            reverse=True,
        )[:limit]
        
        msg = "📊 境界排行榜\n"
        msg += "━━━━━━━━━━━━━━━\n"
        
        for idx, player in enumerate(sorted_players, 1):
            name = _safe_name(player, player.user_id)
            level_name = player.get_level(self.config_manager)
            required_exp = player.get_required_exp(self.config_manager)
            msg += f"{idx}. {name}\n"
            msg += (
                f"   境界：{level_name} | "
                f"修为：{int(player.experience):,}/{int(required_exp):,}\n\n"
            )
        
        return True, msg
    
    async def get_power_ranking(self, limit: int = 10) -> Tuple[bool, str]:
        """
        战力排行榜（与「我的信息」名片使用同一套战斗面板与战力公式）
        
        战力计算公式：气血/10 + 攻击 + 防御 + 精神力/10
        战斗面板完整计入装备、临时丹药、传承与灵兽加成，
        确保排行显示的战力与玩家名片完全一致。
        
        Args:
            limit: 显示数量
            
        Returns:
            (成功标志, 消息)
        """
        all_players = await self.db.get_all_players()
        
        if not all_players:
            return False, "❌ 暂无数据！"
        
        from ..core import PillManager
        from ..managers.pet_manager import PetManager
        
        pill_manager = PillManager(self.db, self.config_manager)
        pet_manager = PetManager(self.db)
        impart_map = await self.db.ext.get_all_impart_info()
        pet_map = await pet_manager.get_all_pets_grouped()
        
        # 计算战力（与玩家名片完全一致的战斗面板）
        player_power = []
        for player in all_players:
            # 临时丹药效果（过期效果会被 calculate 自动忽略，不写库）
            pill_multipliers = pill_manager.calculate_pill_attribute_effects(player)
            
            equipped_items = self.equipment_manager.get_equipped_items(
                player,
                self.config_manager.items_data,
                self.config_manager.weapons_data
            )
            total_attrs = player.get_total_attributes(equipped_items, pill_multipliers, self.equipment_manager.get_base_attributes(player))
            
            impart_info = impart_map.get(player.user_id)
            effects = self.equipment_manager.get_combat_effects(player)
            combat_panel = CombatManager.build_combat_panel(
                player, pill_multipliers, impart_info, self.config_manager, effects
            )
            try:
                pets = pet_map.get(player.user_id, [])
                if pets:
                    owner_atk, pet_atk, pet_effects, pet_def = pet_manager.summarize_lineup(
                        player,
                        pets,
                        combat_panel["hp"],
                        combat_panel["atk"],
                        combat_panel["defense"],
                        player.cultivation_type,
                    )
                    combat_panel["atk"] = int(owner_atk) + int(pet_atk)
                    combat_panel["defense"] += int(pet_def)
                    for k, v in (pet_effects or {}).items():
                        if k in combat_panel and isinstance(v, (int, float)):
                            combat_panel[k] = combat_panel.get(k, 0) + v
            except Exception:
                pass
            combat_power = CombatManager.calculate_combat_power(
                combat_panel, total_attrs["mental_power"]
            )
            player_power.append((player, combat_power, total_attrs))
        
        # 按战力排序
        sorted_players = sorted(player_power, key=lambda x: x[1], reverse=True)[:limit]
        
        msg = "📊 战力排行榜\n"
        msg += "━━━━━━━━━━━━━━━\n"
        
        for idx, (player, power, attrs) in enumerate(sorted_players, 1):
            name = _safe_name(player, player.user_id)
            # 显示主要攻击属性（根据修炼类型）
            if player.cultivation_type == "体修":
                main_atk = int(attrs['physical_damage'])
                atk_label = "物伤"
            else:
                main_atk = int(attrs['magic_damage'])
                atk_label = "法伤"
            msg += f"{idx}. {name}\n"
            msg += f"   战力：{power:,} | {atk_label}：{main_atk:,}\n\n"
        
        return True, msg
    
    async def get_wealth_ranking(self, limit: int = 10) -> Tuple[bool, str]:
        """
        财富排行榜（灵石）
        
        Args:
            limit: 显示数量
            
        Returns:
            (成功标志, 消息)
        """
        all_players = await self.db.get_all_players()
        
        if not all_players:
            return False, "❌ 暂无数据！"
        
        # 按灵石排序
        sorted_players = sorted(all_players, key=lambda p: p.gold, reverse=True)[:limit]
        
        msg = "📊 财富排行榜\n"
        msg += "━━━━━━━━━━━━━━━\n"
        
        for idx, player in enumerate(sorted_players, 1):
            name = _safe_name(player, player.user_id)
            msg += f"{idx}. {name}\n"
            msg += f"   灵石：{player.gold:,}\n\n"
        
        return True, msg
    
    async def get_sect_ranking(self, limit: int = 10) -> Tuple[bool, str]:
        """
        宗门排行榜（建设度）
        
        Args:
            limit: 显示数量
            
        Returns:
            (成功标志, 消息)
        """
        all_sects = await self.db.ext.get_all_sects()
        
        if not all_sects:
            return False, "❌ 暂无宗门数据！"
        
        # 显式按建设度排序，不依赖DB层的排序行为
        top_sects = sorted(all_sects, key=lambda s: s.sect_scale, reverse=True)[:limit]
        
        msg = "📊 宗门排行榜\n"
        msg += "━━━━━━━━━━━━━━━\n"
        
        for idx, sect in enumerate(top_sects, 1):
            owner = await self.db.get_player_by_id(sect.sect_owner)
            owner_name = _safe_name(owner, sect.sect_owner)
            members = await self.db.ext.get_sect_members(sect.sect_id)
            
            # 宗门名称也需要安全处理
            sect_name = sect.sect_name.replace("@", "＠")
            if len(sect_name) > MAX_NAME_LENGTH:
                sect_name = sect_name[:MAX_NAME_LENGTH] + "…"
            
            msg += f"{idx}. 【{sect_name}】\n"
            msg += f"   宗主：{owner_name}\n"
            msg += f"   建设度：{sect.sect_scale:,} | 成员：{len(members)}人\n\n"
        
        return True, msg
    
    async def get_deposit_ranking(self, limit: int = 10) -> Tuple[bool, str]:
        """
        存款排行榜（银行存款）
        
        Args:
            limit: 显示数量
            
        Returns:
            (成功标志, 消息)
        """
        rankings = await self.db.ext.get_deposit_ranking(limit)
        
        if not rankings:
            return False, "❌ 暂无存款数据！"
        
        msg = "📊 存款排行榜\n"
        msg += "━━━━━━━━━━━━━━━\n"
        
        for idx, item in enumerate(rankings, 1):
            uid = item["user_id"]
            player = await self.db.get_player_by_id(uid)
            name = _safe_name(player, uid)
            msg += f"{idx}. {name}\n"
            msg += f"   存款：{item['balance']:,} 灵石\n\n"
        
        return True, msg
    
    async def get_contribution_ranking(self, sect_id: int, limit: int = 10) -> Tuple[bool, str]:
        """
        宗门贡献度排行榜
        
        Args:
            sect_id: 宗门ID
            limit: 显示数量
            
        Returns:
            (成功标志, 消息)
        """
        sect = await self.db.ext.get_sect_by_id(sect_id)
        if not sect:
            return False, "❌ 宗门不存在！"
        
        members = await self.db.ext.get_sect_members(sect_id)
        
        if not members:
            return False, "❌ 宗门暂无成员！"
        
        # 按贡献度排序
        sorted_members = sorted(members, key=lambda p: p.sect_contribution, reverse=True)[:limit]
        
        # 宗门名称安全处理
        sect_name = sect.sect_name.replace("@", "＠")
        if len(sect_name) > MAX_NAME_LENGTH:
            sect_name = sect_name[:MAX_NAME_LENGTH] + "…"
        
        msg = f"📊 {sect_name} 贡献排行\n"
        msg += f"━━━━━━━━━━━━━━━\n"
        
        for idx, member in enumerate(sorted_members, 1):
            name = _safe_name(member, member.user_id)
            # 使用安全映射获取职位名称，防止索引越界
            position_name = POSITION_MAP.get(member.sect_position, "成员")
            msg += f"{idx}. {name} ({position_name})\n"
            msg += f"   贡献度：{member.sect_contribution:,}\n\n"

        return True, msg

    # ===== v6.16 排行扩展 =====

    async def get_karma_ranking(self, limit: int = 10) -> Tuple[bool, str]:
        """功德榜 / 业力榜（二合一：正数功德榜，负数业力榜）"""
        all_players = await self.db.get_all_players()
        if not all_players:
            return False, "❌ 暂无数据！"

        goods = sorted([p for p in all_players if ((p.karma or 0) > 0)], key=lambda p: (p.karma or 0), reverse=True)[:limit]
        evils = sorted([p for p in all_players if ((p.karma or 0) < 0)], key=lambda p: (p.karma or 0))[:limit]

        msg = "📊 功德·业力榜\n━━━━━━━━━━━━━━━\n"
        msg += "【善】功德榜：\n"
        if goods:
            for idx, p in enumerate(goods, 1):
                msg += f"{idx}. {_safe_name(p, p.user_id)} 功德 {p.karma:+d}\n"
        else:
            msg += "  暂无行善之人……\n"
        msg += "【恶】业力榜：\n"
        if evils:
            for idx, p in enumerate(evils, 1):
                msg += f"{idx}. {_safe_name(p, p.user_id)} 业力 {p.karma}\n"
        else:
            msg += "  天下太平，无作恶之徒。\n"
        msg += "━━━━━━━━━━━━━━━\n💡 功德提升奇遇与突破，业力反之。/行善 可积德"
        return True, msg

    async def get_luck_ranking(self, limit: int = 10) -> Tuple[bool, str]:
        """气运榜：按玩家真实气运总值排序（本体+称号+词条+符箓）"""
        from .luck_manager import LuckManager

        all_players = await self.db.get_all_players()
        if not all_players:
            return False, "❌ 暂无数据！"

        scored = []
        for p in all_players:
            luck = LuckManager.get_total_luck(p)
            if luck > 0:
                scored.append((p, luck))
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:limit]
        if not top:
            return False, "🍀 气运榜暂无数据。\n💡 触发奇遇、佩戴气运称号、洗练气运词条或使用神符可积累气运。"

        msg = "🍀 气运榜（气运之子）\n━━━━━━━━━━━━━━━\n"
        medals = ["🥇", "🥈", "🥉"]
        for idx, (p, luck) in enumerate(top, 1):
            medal = medals[idx - 1] if idx <= 3 else f"{idx}."
            level_name = p.get_level(self.config_manager)
            msg += f"{medal} {_safe_name(p, p.user_id)} 气运 {luck}（境界{level_name}）\n"
        msg += "━━━━━━━━━━━━━━━\n💡 气运来自奇遇、称号、装备词条与神符。发送 /气运 查看明细"
        return True, msg

    async def get_virtue_ranking(self, limit: int = 10) -> Tuple[bool, str]:
        """师德榜：传道者的荣耀"""
        all_players = await self.db.get_all_players()
        if not all_players:
            return False, "❌ 暂无数据！"
        teachers = sorted([p for p in all_players if (getattr(p, "virtue_points", 0) or 0) > 0],
                          key=lambda p: (p.virtue_points or 0), reverse=True)[:limit]
        if not teachers:
            return False, "🎓 师德榜暂无数据。\n💡 收徒并助弟子突破可累积师德（/拜师 了解详情）"
        msg = "🎓 师德榜（传道真人）\n━━━━━━━━━━━━━━━\n"
        for idx, p in enumerate(teachers, 1):
            msg += f"{idx}. {_safe_name(p, p.user_id)} 师德 {p.virtue_points or 0}\n"
        msg += "━━━━━━━━━━━━━━━\n💡 师德可在 /师德兑换 换取稀有奖励"
        return True, msg
