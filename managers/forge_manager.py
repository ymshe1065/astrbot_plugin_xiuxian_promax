# managers/forge_manager.py
import random
import json
from typing import Tuple
from ..data import DataBase
from ..models import Player
from ..core.item_instance_manager import ItemInstanceManager
from ..rank_rules import rank_tier

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

    # 妖兽材料定向洗练：品质越高，成功率和词条数值越高。
    MONSTER_CATEGORY = "妖兽"
    TARGETED_AFFIX_ALIASES = {
        "暴击": "crit_rate", "暴击率": "crit_rate",
        "吸血": "lifesteal",
        "反伤": "counter_damage",
        "连击": "combo_rate", "连击率": "combo_rate",
        "物伤": "physical_damage_multiplier",
        "物伤加成": "physical_damage_multiplier",
        "法伤": "magic_damage_multiplier",
        "法伤加成": "magic_damage_multiplier",
        "气运": "luck",
    }
    TARGETED_TIER_RULES = {
        1: {"success": 0.45, "value_mult": 0.70, "gold": 5000},
        2: {"success": 0.60, "value_mult": 0.90, "gold": 12000},
        3: {"success": 0.72, "value_mult": 1.00, "gold": 30000},
        4: {"success": 0.85, "value_mult": 1.15, "gold": 60000},
        5: {"success": 0.95, "value_mult": 1.30, "gold": 120000},
    }

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
        if not target_instance:
            return False, "❌ 你当前没有已装备的装备实例，无法洗练。请先穿上需要洗练的装备。"

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

    def _find_equipped_target(self, player: Player, slot: str = ""):
        """返回 (装备实例, 错误信息)；定向洗练必须针对已装备实例。"""
        from ..core.item_rules import ItemRules

        normalized_slot = (slot or "").strip().lower()
        slot_aliases = ItemRules.SLOT_ALIASES
        if normalized_slot and normalized_slot not in slot_aliases:
            return None, "❌ 洗练部位：武器/头饰/甲胄/手套/靴子/戒指1/戒指2/护符"
        requested_slot = slot_aliases.get(normalized_slot)
        slots = [requested_slot] if requested_slot else list(ItemRules.SLOT_LABELS)
        equipped_map = player.get_equipped_instances()
        im = ItemInstanceManager(self.db, self.config_manager)
        for slot_key in slots:
            inst = im.get_instance(player, equipped_map.get(slot_key, 0))
            if inst:
                return inst, ""
        if requested_slot:
            return None, "❌ 该部位没有已装备的装备实例，无法洗练。"
        return None, "❌ 你当前没有已装备的装备实例，无法洗练。请先穿上需要洗练的装备。"

    def resolve_targeted_affix(self, token: str):
        """把玩家输入的词条名转换为词条基础配置。"""
        text = str(token or "").strip()
        key = self.TARGETED_AFFIX_ALIASES.get(text, text)
        for affix in self.AFFIX_POOL:
            if affix["key"] == key or affix["name"] == text:
                return affix.copy()
        return None

    def get_monster_material_rule(self, material_name: str):
        """校验妖兽材料并返回 (材料信息, 品阶规则, 错误信息)。"""
        try:
            info = self.config_manager.get_material_info(material_name) or {}
        except Exception:
            info = {}
        if not info:
            return None, None, f"❌ 未找到材料【{material_name}】。"
        if info.get("category") != self.MONSTER_CATEGORY:
            return None, None, f"❌ 【{material_name}】不是妖兽材料，无法用于定向洗练。"
        tier = rank_tier(info.get("tier"), 1)
        rule = self.TARGETED_TIER_RULES.get(tier)
        if not rule:
            return None, None, "❌ 该材料品阶不支持定向洗练。"
        return info, rule, ""

    async def targeted_wash(self, player: Player, slot: str, affix_token: str,
                            material_name: str, count: int = 1) -> Tuple[bool, str]:
        """消耗妖兽材料和洗练石，为已装备实例指定附加词条。"""
        target, error = self._find_equipped_target(player, slot)
        if not target:
            return False, error
        affix_base = self.resolve_targeted_affix(affix_token)
        if not affix_base:
            names = "、".join(a["name"] for a in self.AFFIX_POOL)
            return False, f"❌ 未知词条【{affix_token}】。可指定：{names}"

        info, tier_rule, error = self.get_monster_material_rule(material_name)
        if error:
            return False, error
        if not self.storage_ring_mgr.has_item(player, material_name, 1):
            return False, f"❌ 储物戒中没有【{material_name}】。"
        if not self.storage_ring_mgr.has_item(player, "洗练石", 1):
            return False, "❌ 你的储物戒里没有【洗练石】。"

        try:
            count = max(1, min(5, int(count)))
        except (TypeError, ValueError):
            count = 1
        have = self.storage_ring_mgr.get_item_count(player, material_name)
        if have < count:
            return False, f"❌ 【{material_name}】数量不足（拥有 {have}，需要 {count}）。"

        gold_cost = int(tier_rule["gold"] * (1 + (count - 1) * 0.5))
        if getattr(player, "life_skill", "") == "炼器师":
            gold_cost //= 2
        if player.gold < gold_cost:
            return False, f"❌ 灵石不足！需要 {gold_cost:,} 灵石。"

        ok, msg = await self.storage_ring_mgr.retrieve_item(player, material_name, count)
        if not ok:
            return False, f"❌ 扣除妖兽材料失败：{msg}"
        ok, msg = await self.storage_ring_mgr.retrieve_item(player, "洗练石", 1)
        if not ok:
            return False, f"❌ 扣除洗练石失败：{msg}"
        player.gold -= gold_cost

        success_rate = min(0.98, tier_rule["success"] + (count - 1) * 0.05)
        if random.random() >= success_rate:
            await self.db.update_player(player)
            return False, (
                f"💥 定向洗练失败！\n"
                f"已消耗【{material_name}】×{count}、洗练石×1、{gold_cost:,}灵石。\n"
                f"本次未附加【{affix_base['name']}】词条。"
            )

        new_affix = affix_base.copy()
        value_mult = tier_rule["value_mult"] * (1 + (count - 1) * 0.05)
        value_mult *= random.uniform(0.9, 1.1)
        if isinstance(new_affix["value"], float):
            new_affix["value"] = round(new_affix["value"] * value_mult, 3)
            new_affix["display"] = f"{new_affix['name']}+{new_affix['value']*100:.1f}%"
        else:
            new_affix["value"] = max(1, int(new_affix["value"] * value_mult))
            new_affix["display"] = f"{new_affix['name']}+{new_affix['value']}"

        affixes = target.get("affixes", []) or []
        replaced = False
        for index, old in enumerate(affixes):
            if old.get("key") == new_affix["key"]:
                affixes[index] = new_affix
                replaced = True
                break
        if not replaced:
            affixes.append(new_affix)
        if len(affixes) > 3:
            affixes.pop(0)
        target["affixes"] = affixes
        im = ItemInstanceManager(self.db, self.config_manager)
        instances = im.load(player)
        for index, inst in enumerate(instances):
            if int(inst.get("id", 0) or 0) == int(target.get("id", 0) or 0):
                instances[index] = target
                break
        im.save(player, instances)
        await self.db.update_player(player)

        lines = [
            "✨ 定向洗练成功！",
            f"👉 指定词条：{new_affix['display']}",
            f"消耗：{material_name}×{count}、洗练石×1、{gold_cost:,}灵石",
            "━━━━━━━━━━━━━━━",
            "【当前装备总词条】",
        ]
        lines.extend(f"· {a.get('display', a.get('name', ''))}" for a in affixes)
        master_note = "\n⚒️ 炼器师专精：灵石消耗减半" if getattr(player, "life_skill", "") == "炼器师" else ""
        return True, "\n".join(lines) + master_note