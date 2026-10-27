# managers/forge_manager.py
import random
import json
from typing import Tuple
from ..data import DataBase
from ..models import Player
from ..core.item_instance_manager import ItemInstanceManager

class ForgeManager:
    """炼器与洗练管理器"""
    
    # 洗练词条池（随机抽取）
    AFFIX_POOL = [
        {"key": "crit_rate", "name": "暴击率", "value": 5, "desc": "战斗中增加5%暴击率"},
        {"key": "lifesteal", "name": "吸血", "value": 3, "desc": "战斗中增加3%吸血"},
        {"key": "counter_damage", "name": "反伤", "value": 5, "desc": "战斗中反弹5%伤害"},
        {"key": "combo_rate", "name": "连击率", "value": 5, "desc": "战斗中增加5%连击率"},
        {"key": "physical_damage_multiplier", "name": "物伤加成", "value": 0.05, "desc": "提升5%物伤"},
        {"key": "magic_damage_multiplier", "name": "法伤加成", "value": 0.05, "desc": "提升5%法伤"},
        {"key": "luck", "name": "气运", "value": 2, "desc": "永久增加2点气运"}
    ]

    def __init__(self, db: DataBase, config_manager, storage_ring_mgr):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_mgr = storage_ring_mgr

    def _get_bonus_data(self, player: Player) -> dict:
        """安全获取玩家 permanent_bonus 数据"""
        try:
            data = json.loads(player.permanent_bonus)
            if not isinstance(data, dict):
                return {"forge_level": 0, "affixes": []}
            if "forge_level" not in data:
                data["forge_level"] = 0
            if "affixes" not in data:
                data["affixes"] = []
            return data
        except:
            return {"forge_level": 0, "affixes": []}

    async def enhance_equipment(self, player: Player) -> Tuple[bool, str]:
        """强化当前装备（全局炼器等级）"""
        data = self._get_bonus_data(player)
        current_level = data.get("forge_level", 0)
        
        if current_level >= 15:
            return False, "❌ 你的炼器等级已达上限（+15）。"
            
        # 消耗公式：等级越高，消耗越多
        cost_stone = 10 + current_level * 5
        cost_sword = 10000 * (current_level + 1)

        # 炼器师专精：灵石消耗减半
        skill_note = ""
        if getattr(player, "life_skill", "") == "炼器师":
            cost_sword = cost_sword // 2
            skill_note = "\n⚒️ 炼器师专精生效：灵石消耗减半"

        if not self.storage_ring_mgr.has_item(player, "强化石", cost_stone):
            return False, f"❌ 你的储物戒里没有足够的【强化石】。\n需要：{cost_stone}个"

        if player.gold < cost_sword:
            return False, f"❌ 灵石不足！需要 {cost_sword:,} 灵石。"

        # 扣除物品
        await self.storage_ring_mgr.retrieve_item(player, "强化石", cost_stone)
        player.gold -= cost_sword

        # 判定成功率
        success_rate = max(0.2, 1.0 - current_level * 0.05)

        if random.random() < success_rate:
            data["forge_level"] = current_level + 1
            player.permanent_bonus = json.dumps(data)
            await self.db.update_player(player)
            return True, f"✨ 炼器成功！你的炼器等级提升至 +{current_level + 1}。\n（全属性基础攻击/防御获得加成）{skill_note}"
        else:
            await self.db.update_player(player)
            return False, f"💥 炼器失败！由于火候不佳，消耗了材料和灵石，但等级未提升（当前 +{current_level}）。{skill_note}"

    async def wash_equipment(self, player: Player, slot: str = "") -> Tuple[bool, str]:
        """洗练装备词条（可用 slot 指定武器/防具，默认优先武器）"""
        data = self._get_bonus_data(player)
        im = ItemInstanceManager(self.db, self.config_manager)
        equipped_map = player.get_equipped_instances()

        normalized_slot = (slot or "").strip().lower()
        from ..core.item_rules import ItemRules
        slot_aliases = ItemRules.SLOT_ALIASES
        if normalized_slot and normalized_slot not in slot_aliases:
            return False, "❌ 洗练部位：武器/头饰/甲胄/手套/靴子/戒指1/戒指2/护符"
        requested_slot = slot_aliases.get(normalized_slot)
        slots = [requested_slot] if requested_slot else list(ItemRules.SLOT_LABELS)

        target_instance = None
        for slot_key in slots:
            inst = im.get_instance(player, equipped_map.get(slot_key, 0))
            if inst:
                target_instance = inst
                break

        if requested_slot and not target_instance:
            return False, "❌ 该部位没有已装备的装备实例，无法洗练。"

        # 每次洗练消耗一个洗练石
        if not self.storage_ring_mgr.has_item(player, "洗练石", 1):
            return False, "❌ 你的储物戒里没有【洗练石】。"
            
        ok, msg = await self.storage_ring_mgr.retrieve_item(player, "洗练石", 1)
        if not ok:
            return False, f"❌ 扣除洗练石失败：{msg}"
        
        # 随机抽取一个新词条
        new_affix = random.choice(self.AFFIX_POOL).copy()
        # 数值浮动 ±20%
        multiplier = random.uniform(0.8, 1.2)
        if isinstance(new_affix["value"], float):
            new_affix["value"] = round(new_affix["value"] * multiplier, 3)
        else:
            new_affix["value"] = int(new_affix["value"] * multiplier)
            
        new_affix["display"] = f"{new_affix['name']}+{new_affix['value']}"
        if isinstance(new_affix["value"], float):
            new_affix["display"] = f"{new_affix['name']}+{new_affix['value']*100:.1f}%"
            
        if target_instance:
            affixes = target_instance.get("affixes", []) or []
            affixes.append(new_affix)
            if len(affixes) > 3:
                affixes.pop(0)
            target_instance["affixes"] = affixes
            instances = im.load(player)
            for idx, inst in enumerate(instances):
                if int(inst.get("id", 0) or 0) == int(target_instance.get("id", 0) or 0):
                    instances[idx] = target_instance
                    break
            im.save(player, instances)
        else:
            # 兼容旧版全局词条
            affixes = data.get("affixes", [])
            affixes.append(new_affix)
            if len(affixes) > 3:
                affixes.pop(0)
            data["affixes"] = affixes
            player.permanent_bonus = json.dumps(data)
        await self.db.update_player(player)
        
        msg = [
            "✨ 洗练成功！你获得了新词条：",
            f"👉 {new_affix['display']}",
            "━━━━━━━━━━━━━━━",
            "【当前装备总词条】"
        ]
        for a in affixes:
            msg.append(f"· {a['display']}")
            
        return True, "\n".join(msg)