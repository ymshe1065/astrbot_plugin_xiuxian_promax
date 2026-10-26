# managers/pet_manager.py
import random
from typing import Tuple, Dict, Optional
from astrbot.api import logger
from ..data import DataBase
from ..models import Player

class PetManager:
    """灵兽管理器"""
    
    # 灵兽种类库
    PET_TYPES = {
        "火灵狐": {"desc": "攻击时附带灼烧，增加主人暴击率", "attr": "crit_rate", "val": 10},
        "冰晶兽": {"desc": "拥有极寒护盾，增加主人防御力", "attr": "defense", "val": 50},
        "噬金鼠": {"desc": "嗅觉敏锐，增加日常灵石掉落", "attr": "gold_bonus", "val": 0.1},
        "青龙幼崽": {"desc": "神兽血脉，全面增加主人属性", "attr": "all", "val": 20}
    }

    def __init__(self, db: DataBase):
        self.db = db

    async def get_pet(self, user_id: str) -> Optional[Dict]:
        """获取玩家灵兽信息"""
        async with self.db.conn.execute("SELECT * FROM player_pets WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

    async def adopt_pet(self, player: Player, pet_name: str) -> Tuple[bool, str]:
        """孵化/捕捉灵兽"""
        existing = await self.get_pet(player.user_id)
        if existing:
            return False, f"❌ 你已经契约了【{existing['pet_name']}】，无法携带第二只。\n💡 如果需要更换，请先使用 /放生灵兽 或 /吞噬灵兽。"
            
        if pet_name not in self.PET_TYPES:
            return False, f"❌ 未知的灵兽种类！可用灵兽：{', '.join(self.PET_TYPES.keys())}"
            
        cost = 100000
        if player.gold < cost:
            return False, f"❌ 捕捉【{pet_name}】需要 {cost:,} 灵石。"
            
        player.gold -= cost
        await self.db.update_player(player)
        
        await self.db.conn.execute(
            "INSERT INTO player_pets (user_id, pet_name, pet_type, level, exp) VALUES (?, ?, ?, 1, 0)",
            (player.user_id, pet_name, pet_name)
        )
        await self.db.conn.commit()
        
        pet_info = self.PET_TYPES[pet_name]
        return True, f"🎉 恭喜！你成功契约了【{pet_name}】！\n特性：{pet_info['desc']}"

    async def release_pet(self, player: Player) -> Tuple[bool, str]:
        """放生灵兽"""
        pet = await self.get_pet(player.user_id)
        if not pet:
            return False, "❌ 你当前没有携带灵兽。"
        
        pet_name = pet["pet_name"]
        await self.db.conn.execute("DELETE FROM player_pets WHERE user_id = ?", (player.user_id,))
        await self.db.conn.commit()
        
        return True, f"🕊️ 你已解开契约，将【{pet_name}】放归山林。\n当前灵兽栏已空，可以契约新灵兽了。"

    async def devour_pet(self, player: Player) -> Tuple[bool, str]:
        """吞噬灵兽（返还少量资源）"""
        pet = await self.get_pet(player.user_id)
        if not pet:
            return False, "❌ 你当前没有携带灵兽。"
            
        pet_name = pet["pet_name"]
        level = pet["level"]
        
        # 返还公式：每级返还 500 灵石，250 修为（相比10万的契约费，吞噬是巨亏的，防止刷资源）
        refund_gold = level * 500
        refund_exp = level * 250
        
        player.gold += refund_gold
        player.experience += refund_exp
        
        await self.db.conn.execute("DELETE FROM player_pets WHERE user_id = ?", (player.user_id,))
        await self.db.update_player(player)
        await self.db.conn.commit()
        
        return True, f"🩸 你运转魔功，强行吞噬了【{pet_name}】！\n炼化获得：{refund_gold:,} 灵石，{refund_exp:,} 修为。\n（灵兽栏已空，可契约新灵兽）"

    async def feed_pet(self, player: Player, cost_mult: float = 1.0) -> Tuple[bool, str]:
        """喂养灵兽（消耗灵石和修为；cost_mult 为外部加成折扣）"""
        pet = await self.get_pet(player.user_id)
        if not pet:
            return False, "❌ 你还没有灵兽，请先捕捉。"

        if pet["level"] >= 50:
            return False, "❌ 你的灵兽已经满级（Lv.50）。"

        cost_gold = int(pet["level"] * 1000 * cost_mult)
        cost_exp = int(pet["level"] * 500 * cost_mult)
        
        if player.gold < cost_gold:
            return False, f"❌ 灵石不足，喂养需要 {cost_gold:,} 灵石。"
        if player.experience < cost_exp:
            return False, f"❌ 修为不足，喂养需要 {cost_exp:,} 修为（灵兽会吞噬你的修为）。"
            
        player.gold -= cost_gold
        player.experience -= cost_exp
        
        # 增加经验，每100经验升1级
        new_exp = pet["exp"] + 100
        new_level = pet["level"]
        if new_exp >= new_level * 500: # 升级所需经验
            new_level += 1
            new_exp = 0
            
        await self.db.conn.execute(
            "UPDATE player_pets SET level = ?, exp = ? WHERE user_id = ?",
            (new_level, new_exp, player.user_id)
        )
        await self.db.update_player(player)
        await self.db.conn.commit()
        
        return True, (
            f"✨ 喂养成功！\n"
            f"消耗灵石：{cost_gold:,}\n"
            f"剩余灵石：{player.gold:,}\n"
            f"灵兽当前等级：Lv.{new_level} (经验：{new_exp}/{new_level*500})"
        )

    def get_pet_combat_effects(self, pet: Optional[Dict]) -> dict:
        """获取灵兽提供的战斗特效"""
        if not pet:
            return {}
            
        pet_info = self.PET_TYPES.get(pet["pet_type"])
        if not pet_info:
            return {}
            
        level_mult = pet["level"] * 0.1
        base_val = pet_info["val"] * level_mult
        
        effects = {}
        if pet_info["attr"] == "crit_rate":
            effects["crit_rate"] = base_val
        elif pet_info["attr"] == "defense":
            effects["counter_damage"] = base_val / 5  # 转化为反伤
        elif pet_info["attr"] == "all":
            effects["crit_rate"] = base_val
            effects["lifesteal"] = base_val / 2
            
        return effects

    async def pet_coop_attack(self, pet: Optional[Dict]) -> Tuple[bool, str, int]:
        """灵兽协战判定（每回合触发）"""
        if not pet:
            return False, "", 0
            
        # 基础触发概率 20% + 等级加成
        trigger_rate = 0.2 + (pet["level"] * 0.01)
        
        if random.random() < trigger_rate:
            # 灵兽伤害基于主人境界（假设与玩家等级相关）
            damage_mult = pet["level"] * 0.5
            return True, f"🐾 【{pet['pet_name']}】扑向敌人，发起协同攻击！", damage_mult
            
        return False, "", 0