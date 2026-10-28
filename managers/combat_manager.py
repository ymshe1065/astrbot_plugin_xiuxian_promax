# managers/combat_manager.py
"""
战斗系统管理器 - 处理HP/MP/ATK系统和战斗逻辑
"""

import random
from typing import Tuple, Dict, Optional, List
from dataclasses import dataclass

@dataclass
class PetCombatState:
    """灵兽独立战斗状态"""
    user_id: str
    name: str
    pet_type: str
    level: int
    hp: int
    max_hp: int
    atk: int
    defense: int = 0
    guard_rate: float = 0.15
    mechanic: str = ""
    pet_id: int = 0
    revive_mult: float = 1.0

    @property
    def alive(self) -> bool:
        return self.hp > 0


@dataclass
class CombatStats:
    """战斗属性"""
    user_id: str
    name: str  # 道号
    hp: int  # 当前气血
    max_hp: int  # 最大气血
    mp: int  # 当前真元值
    max_mp: int  # 最大真元值
    atk: int  # 攻击力
    defense: int = 0  # 防御力
    crit_rate: int = 0  # 会心率（百分比）
    exp: int = 0  # 修为（用于计算攻击力）
    effects: dict = None # 战斗特效（功法/词条/灵兽）
    element: str = ""  # 五行属性（金木水火土，空为无属性）
    pet: Optional[PetCombatState] = None  # 主战灵兽
    support_pets: Optional[List[PetCombatState]] = None  # 辅助出战灵兽
    defense_scale: int = 0  # 攻击方境界基础尺度，不受暴击/装备影响


class CombatManager:
    """战斗系统管理器"""

    # 五行相生相克：key 克 value（金克木、木克土、土克水、水克火、火克金）
    ELEMENT_OVERCOMES = {"金": "木", "木": "土", "土": "水", "水": "火", "火": "金"}
    ELEMENT_ADVANTAGE_MULT = 1.3   # 克制方伤害 +30%
    ELEMENT_DISADVANTAGE_MULT = 0.8 # 被克制方伤害 -20%

    @staticmethod
    def get_element_from_name(name: str) -> str:
        """从功法/心法/武器名称中推断五行属性（金木水火土）"""
        if not name:
            return ""
        for e in ("金", "木", "水", "火", "土"):
            if e in name:
                return e
        return ""

    @classmethod
    def element_multiplier(cls, attacker_element: str, defender_element: str) -> float:
        """计算五行克制伤害倍率"""
        if not attacker_element or not defender_element:
            return 1.0
        if cls.ELEMENT_OVERCOMES.get(attacker_element) == defender_element:
            return cls.ELEMENT_ADVANTAGE_MULT
        if cls.ELEMENT_OVERCOMES.get(defender_element) == attacker_element:
            return cls.ELEMENT_DISADVANTAGE_MULT
        return 1.0
    
    @staticmethod
    def calculate_hp_mp(experience: int, hp_buff: float = 0.0, mp_buff: float = 0.0) -> Tuple[int, int]:
        """兼容旧调用：实战气血/灵气请使用 build_player_combat_stats（境界基础）。"""
        base_hp = max(1, int(experience) // 2) if experience else 80
        base_mp = max(1, int(experience) if experience else 100)
        return int(base_hp * (1 + hp_buff)), int(base_mp * (1 + mp_buff))

    @staticmethod
    def calculate_atk(experience: int, atkpractice: int = 0, atk_buff: float = 0.0, physical_damage: int = 0, magic_damage: int = 0, cultivation_type: str = "灵修") -> int:
        """兼容旧调用：实战攻击请使用 build_player_combat_stats（境界基础 + 装备百分比）。"""
        class_multiplier = {"剑修": 1.15, "魔修": 1.10, "佛修": 0.85}.get(cultivation_type, 1.0)
        practice_bonus = min(0.20, max(0, int(atkpractice or 0)) * 0.01)
        base = max(1, int((experience or 0) // 10 * class_multiplier * (1 + practice_bonus + atk_buff)))
        return max(1, base + int(physical_damage or 0) + int(magic_damage or 0))

    @staticmethod
    def calculate_equipment_bonus(player, config_manager) -> Dict:
        """百分比仅作用于固定境界基础，所有槽位先加总再取整。"""
        from ..core.equipment_manager import EquipmentManager
        manager = EquipmentManager(None, config_manager)
        base = manager.get_base_attributes(player)
        rates = manager.get_equipment_rates(player)
        return {"atk": sum(int(base[k] * rates.get(k, 0)) for k in ("physical_damage", "magic_damage")),
                "defense": sum(int(base[k] * rates.get(k, 0)) for k in ("physical_defense", "magic_defense"))}

    @staticmethod
    def get_permanent_bonus_data(player) -> dict:
        try:
            import json
            data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def get_title_combat_bonus(player) -> dict:
        """读取当前最高品阶称号的战斗加成（不依赖数据库写入）。"""
        try:
            from .title_manager import TitleManager
            return TitleManager(None).get_active_bonus(player) or {}
        except Exception:
            return {}

    @staticmethod
    def get_forge_multiplier(player) -> float:
        """炼器等级：每级 +2% 攻击/防御，最高 +15 级。"""
        data = CombatManager.get_permanent_bonus_data(player)
        try:
            level = max(0, min(15, int(data.get("forge_level", 0) or 0)))
        except (TypeError, ValueError):
            level = 0
        return 1.0 + level * 0.02

    @staticmethod
    def build_combat_panel(
        player,
        pill_multipliers: Optional[dict],
        impart_info,
        config_manager,
        effects: Optional[dict] = None,
    ) -> Dict:
        """名片、排行榜和实战共用唯一构建入口；不修改传入特效。"""
        stats = CombatManager.build_player_combat_stats(
            player, config_manager, pill_multipliers=pill_multipliers,
            hp_buff=impart_info.impart_hp_per if impart_info else 0.0,
            mp_buff=impart_info.impart_mp_per if impart_info else 0.0,
            atk_buff=impart_info.impart_atk_per if impart_info else 0.0,
            crit_rate=int(getattr(impart_info, "impart_know_per", 0) * 100) if impart_info else 0,
            crit_damage_bonus=getattr(impart_info, "impart_burst_per", 0) if impart_info else 0,
        )
        return {"hp": stats.hp, "max_hp": stats.max_hp, "mp": stats.mp, "max_mp": stats.max_mp,
                "atk": stats.atk, "defense": stats.defense,
                "crit_rate": min(50, stats.crit_rate + stats.effects.get("crit_rate", 0)),
                **{k: stats.effects.get(k, 1.5 if k == "crit_damage" else 0)
                   for k in ("crit_damage", "lifesteal", "counter_damage")}}

    @staticmethod
    def clamp_effects(effects):
        from ..core.equipment_balance import GLOBAL_EFFECT_CAPS
        result = dict(effects or {})
        for key, cap in GLOBAL_EFFECT_CAPS.items():
            result[key] = max(0, min(cap, float(result.get(key, 1.5 if key == "crit_damage" else 1.0 if key.endswith("_multiplier") else 0))))
        return result

    @staticmethod
    def collect_combat_effects(player, config_manager) -> dict:
        """收集功法/装备/全局词条特效，保持与装备管理器一致。"""
        effects = {
            "crit_rate": 0,
            "crit_damage": 1.5,
            "lifesteal": 0,
            "counter_damage": 0,
            "defense_bonus": 0,
            "combo_rate": 0,
            "dodge": 0,
            "physical_damage_multiplier": 1.0,
            "magic_damage_multiplier": 1.0,
        }
        techniques_data = getattr(config_manager, "techniques_data", {}) or {}
        items_data = getattr(config_manager, "items_data", {}) or {}
        for tech_name in player.get_techniques_list():
            tech_config = techniques_data.get(tech_name) or items_data.get(tech_name)
            if tech_config and "effects" in tech_config:
                for key, value in tech_config["effects"].items():
                    if key in effects:
                        effects[key] += value
        if player.main_technique:
            main_config = techniques_data.get(player.main_technique) or items_data.get(player.main_technique)
            if main_config and "effects" in main_config:
                for key, value in main_config["effects"].items():
                    if key in effects:
                        effects[key] += value
        try:
            import json
            bonus_data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            for affix in bonus_data.get("affixes", []):
                key = affix.get("key")
                value = affix.get("value", 0)
                if key in effects:
                    effects[key] += value
        except Exception:
            pass
        from ..core.equipment_manager import EquipmentManager
        for key, value in EquipmentManager(None, config_manager).get_gear_effects(player).items():
            if key in effects:
                effects[key] += value
        return CombatManager.clamp_effects(effects)

    @classmethod
    def build_player_combat_stats(
        cls,
        player,
        config_manager=None,
        db=None,
        hp_buff: float = 0.0,
        mp_buff: float = 0.0,
        atk_buff: float = 0.0,
        crit_rate: int = 0,
        crit_damage_bonus: float = 0.0,
        extra_effects: Optional[dict] = None,
        element: Optional[str] = None,
        pill_multipliers: Optional[dict] = None,
    ) -> "CombatStats":
        """统一构建玩家战斗属性，确保所有战斗入口读取装备、词条与丹药。"""
        from ..core.equipment_balance import realm_attributes
        base = realm_attributes(player, config_manager)
        base_atk = base["physical_damage"] + base["magic_damage"]
        base_def = base["physical_defense"] + base["magic_defense"]
        hp, mp = base["max_hp"], base["max_mp"]
        equip = {"atk": 0, "defense": 0}
        effects = {}
        if config_manager is not None:
            equip = cls.calculate_equipment_bonus(player, config_manager)
            effects = cls.collect_combat_effects(player, config_manager)
        if pill_multipliers is None and config_manager is not None:
            try:
                from ..core.pill_manager import PillManager
                pill_multipliers = PillManager(db, config_manager).calculate_pill_attribute_effects(player)
            except Exception:
                pill_multipliers = {}
        pill_multipliers = pill_multipliers or {}
        if crit_damage_bonus:
            effects["crit_damage"] = effects.get("crit_damage", 1.5) + float(crit_damage_bonus)
        if extra_effects:
            for key, value in extra_effects.items():
                if isinstance(value, (int, float)):
                    effects[key] = effects.get(key, 0) + value
                else:
                    effects[key] = value

        # 符箓效果：暴击/吸血/反击/闪避等
        try:
            from .talisman_manager import TalismanManager
            for key, value in TalismanManager.collect_active_combat_effects(player).items():
                effects[key] = effects.get(key, 0) + value
        except Exception:
            pass

        if element is None and config_manager is not None:
            try:
                element = (
                    config_manager.get_item_element(player.main_technique)
                    or config_manager.get_item_element(player.weapon)
                )
            except Exception:
                element = ""
        if not element:
            element = (
                cls.get_element_from_name(player.main_technique)
                or cls.get_element_from_name(player.weapon)
            )

        dmg_mult = max(
            float(pill_multipliers.get("physical_damage", 1.0) or 1.0),
            float(pill_multipliers.get("magic_damage", 1.0) or 1.0),
        )
        def_mult = max(
            float(pill_multipliers.get("physical_defense", 1.0) or 1.0),
            float(pill_multipliers.get("magic_defense", 1.0) or 1.0),
        )
        # 所有非装备增益作用于同一基础，且有明确上限，避免多层相乘。
        title = cls.get_title_combat_bonus(player)
        forge_bonus = cls.get_forge_multiplier(player) - 1.0
        attack_bonus = max(-.5, min(.50, dmg_mult - 1 + atk_buff + min(.20, max(0, player.atkpractice) * .01) + forge_bonus))
        defense_bonus = max(-.5, min(.50, def_mult - 1 + forge_bonus))
        # 永久丹药/先天随机属性保留为有上限的附加值，不作为装备基数。
        permanent_pill_limit = 0.30
        try:
            import json
            bonus_data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            if isinstance(bonus_data, dict):
                permanent_pill_limit += max(0.0, min(0.50, float(bonus_data.get("permanent_pill_limit_bonus", 0) or 0)))
        except Exception:
            pass
        permanent_atk = min(int(base_atk * permanent_pill_limit), max(0, player.physical_damage + player.magic_damage))
        permanent_def = min(int(base_def * permanent_pill_limit), max(0, player.physical_defense + player.magic_defense))
        title_atk = min(int(base_atk * .10), max(0, title.get("atk", 0) + title.get("magic", 0)))
        title_def = min(int(base_def * .10), max(0, title.get("phys_def", 0) + title.get("magic_def", 0)))
        final_atk = max(1, int(base_atk * (1 + attack_bonus)) + equip["atk"] + permanent_atk + title_atk)
        defense = max(0, int(base_def * (1 + defense_bonus)) + equip["defense"] + permanent_def + title_def
                      + min(int(base_def * .10), max(0, int(effects.get("defense_bonus", 0)))))
        hp = max(1, int(hp * (1 + max(-.5, min(.5, hp_buff)))))
        mp = max(1, int(mp * (1 + max(-.5, min(.5, mp_buff)))))
        effects["crit_rate"] = effects.get("crit_rate", 0) + title.get("crit", 0)
        effects["lifesteal"] = effects.get("lifesteal", 0) + title.get("lifesteal", 0)
        effects = cls.clamp_effects(effects)
        return CombatStats(
            user_id=player.user_id,
            name=player.user_name or player.user_id,
            hp=hp,
            max_hp=hp,
            mp=mp,
            max_mp=mp,
            atk=final_atk,
            defense=defense,
            crit_rate=int(crit_rate or 0),
            exp=player.experience,
            effects=effects,
            element=element or "",
            defense_scale=base["defense_scale"],
        )

    @staticmethod
    def calculate_combat_power(panel: Dict, mental_power: int = 0) -> int:
        """统一战力公式：气血/10 + 攻击 + 防御 + 精神力/10

        玩家名片、战力排行等所有展示入口都必须调用此方法，
        避免各自维护公式导致同一玩家显示不同战力。
        """
        return int(
            panel.get("hp", 0) / 10
            + panel.get("atk", 0)
            + panel.get("defense", 0)
            + int(mental_power or 0) // 10
        )
    
    @staticmethod
    def apply_damage_reduction(damage: int, defense: int = 0, scale: int = 0) -> int:
        """应用伤害减免（使用递减公式）"""
        if defense <= 0:
            return damage
        # 使用攻击方未加成的境界尺度；同尺度整体放大不改变减伤比例。
        reference = max(1, scale or damage)
        reduction_rate = min(.75, defense / (defense + reference))
        final_damage = int(damage * (1 - reduction_rate))
        return max(1, final_damage)
    
    @classmethod
    def _execute_attack(cls, attacker: CombatStats, defender: CombatStats) -> Tuple[int, List[str], int]:
        """执行一次攻击，返回 (最终伤害, 战斗日志列表, 吸血量)"""
        logs = []
        effects = cls.clamp_effects(attacker.effects)
        defender_effects = cls.clamp_effects(defender.effects)

        # 0. 闪避（符箓/词条）
        try:
            dodge = min(60.0, float(defender_effects.get("dodge", 0) or 0))
        except (TypeError, ValueError):
            dodge = 0.0
        if dodge > 0 and random.randint(1, 100) <= dodge:
            return 0, [f"{defender.name} 身形一闪，避开了攻击"], 0

        # 1. 基础伤害
        damage = int(round(random.uniform(0.95, 1.05), 2) * attacker.atk)

        # 功法/词条的物伤/法伤倍率（取较高的一项，避免双倍叠加）
        damage_mult = max(
            effects.get("physical_damage_multiplier", 1.0),
            effects.get("magic_damage_multiplier", 1.0),
        )
        if damage_mult != 1.0:
            damage = int(damage * damage_mult)

        # 1.5 五行克制倍率（克制+30%，被克-20%）
        elem_mult = cls.element_multiplier(attacker.element, defender.element)
        if elem_mult != 1.0:
            damage = int(damage * elem_mult)
        
        # 2. 暴击判定
        base_crit = attacker.crit_rate
        bonus_crit = effects.get("crit_rate", 0)
        is_crit = random.random() * 100 < min(50, max(0, base_crit + bonus_crit))
        
        if is_crit:
            crit_mult = effects.get("crit_damage", 1.5)
            damage = int(damage * crit_mult)
            logs.append(f"💥 {attacker.name} 触发会心一击！")
            
        # 3. 连击/追加伤害判定
        combo_rate = effects.get("combo_rate", 0)
        if random.random() * 100 < combo_rate:
            extra_dmg = int(damage * 0.5)
            damage += extra_dmg
            logs.append(f"⚡ {attacker.name} 触发功法连击，追加 {extra_dmg} 伤害！")
            
        # 4. 防御减伤
        final_damage = cls.apply_damage_reduction(damage, defender.defense, attacker.defense_scale or attacker.atk)
        logs.append(f"🗡️ {attacker.name} 造成 {final_damage} 点伤害")

        # 5. 灵兽每回合协战
        attacker_pets = []
        if getattr(attacker, "pet", None) and attacker.pet.alive:
            attacker_pets.append(attacker.pet)
        for support in (getattr(attacker, "support_pets", None) or []):
            if support and support.alive:
                attacker_pets.append(support)
        if attacker_pets:
            pet_count = len(attacker_pets)
            pet_factor = 1.0 / (1 + 0.4 * (pet_count - 1))
            for pet in attacker_pets:
                pet_damage = int(round(random.uniform(0.95, 1.05), 2) * pet.atk * pet_factor)
                pet_damage = cls.apply_damage_reduction(pet_damage, defender.defense, attacker.defense_scale or attacker.atk)
                if pet.mechanic == "burn":
                    burn = max(1, int(pet.atk * pet_factor * 0.3))
                    pet_damage += burn
                    logs.append(f"🔥 【{pet.name}】喷吐灵火，追加灼烧 {burn} 点伤害！")
                if pet_damage > 0:
                    final_damage += pet_damage
                    logs.append(f"🐾 【{pet.name}】协同攻击，追加 {pet_damage} 点伤害！")
                if pet.mechanic == "dragon" and pet_damage > 0:
                    heal_pet = max(1, int(pet_damage * 0.2))
                    attacker.hp = min(attacker.max_hp, attacker.hp + heal_pet)
                    logs.append(f"🐉 【{pet.name}】龙息反哺，{attacker.name} 恢复 {heal_pet} 点气血！")

        # 6. 防守方主战灵兽护主承伤
        defender_pets = []
        if getattr(defender, "pet", None):
            defender_pets.append(defender.pet)
        defender_pets.extend(getattr(defender, "support_pets", None) or [])
        defender_pet = next((p for p in defender_pets if p and p.alive), None)
        if defender_pet and final_damage > 0:
            incoming = int(final_damage * defender_pet.guard_rate)
            if incoming > 0:
                protected = min(incoming, defender_pet.hp)
                pet_loss = cls.apply_damage_reduction(incoming, defender_pet.defense, attacker.defense_scale or attacker.atk)
                pet_loss = min(defender_pet.hp, max(1, pet_loss))
                defender_pet.hp -= pet_loss
                final_damage -= protected
                logs.append(
                    f"🛡️ 【{defender_pet.name}】替 {defender.name} 挡下 {protected} 点伤害"
                    f"（自身损耗 {pet_loss} 点精力）！"
                )
                if defender_pet.hp <= 0:
                    defender_pet.hp = 0
                    logs.append(f"💀 【{defender_pet.name}】力竭倒下，本场战斗无法继续协战！")

        # 7. 吸血计算
        lifesteal_percent = effects.get("lifesteal", 0)
        heal_amount = 0
        if lifesteal_percent > 0:
            heal_amount = int(final_damage * (lifesteal_percent / 100))
            if heal_amount > 0:
                logs.append(f"🩸 {attacker.name} 吸取了 {heal_amount} 点气血！")

        return final_damage, logs, heal_amount

    @classmethod
    def _shorten_combat_log(cls, combat_log, round_num, winner, player1, player2):
        """只保留前 5 回合、后 5 回合和最终结果，避免长战报触发合并转发。"""
        if winner == player1.user_id:
            win_name = player1.name
        elif winner == player2.user_id:
            win_name = player2.name
        else:
            win_name = "平局"

        result_lines = [
            "",
            "☆━━━━ 战斗结束 ━━━━☆",
            f"胜者：{win_name}",
            f"回合数：{round_num}",
            f"{player1.name} 剩余 HP：{max(0, player1.hp)} | "
            f"{player2.name} 剩余 HP：{max(0, player2.hp)}",
        ]

        if round_num <= 10:
            return combat_log + result_lines

        markers = [
            i for i, line in enumerate(combat_log)
            if line.startswith("-- 第 ") and line.endswith(" 回合 --")
        ]
        if len(markers) <= 10:
            return combat_log + result_lines

        header = combat_log[:markers[0]]
        blocks = []
        for idx, start in enumerate(markers):
            end = markers[idx + 1] if idx + 1 < len(markers) else len(combat_log)
            blocks.append(combat_log[start:end])

        omitted = len(blocks) - 10
        shortened = list(header)
        for block in blocks[:5]:
            shortened.extend(block)
        shortened.append(f"……（中间省略 {omitted} 回合）……")
        for block in blocks[-5:]:
            shortened.extend(block)
        shortened.extend(result_lines)
        return shortened

    @classmethod
    def player_vs_player(cls, player1: CombatStats, player2: CombatStats, combat_type: int = 1) -> Dict:
        """玩家vs玩家战斗 (限制5回合，按剩余血量百分比判定)"""
        combat_log = [f"☆━━━━ 战斗开始 ━━━━☆", f"{player1.name} VS {player2.name}"]
        round_num = 0
        max_rounds = 5  # ★ 修改：最多5回合
        
        while player1.hp > 0 and player2.hp > 0 and round_num < max_rounds:
            round_num += 1
            combat_log.append(f"-- 第 {round_num} 回合 --")
            
            # 玩家1攻击
            dmg1, logs1, heal1 = cls._execute_attack(player1, player2)
            player2.hp -= dmg1
            player1.hp = min(player1.max_hp, player1.hp + heal1)
            combat_log.extend(logs1)
            combat_log.append(f"{player2.name} 剩余 HP: {max(0, player2.hp)}")
            
            # 反伤判定（针对玩家2的功法）
            counter = min(25, max(0, (player2.effects or {}).get("counter_damage", 0)))
            if counter > 0:
                reflect_dmg = int(dmg1 * (counter / 100))
                player1.hp -= reflect_dmg
                combat_log.append(f"🛡️ {player2.name} 触发反伤，反弹 {reflect_dmg} 点伤害！")
                combat_log.append(f"{player1.name} 剩余 HP: {max(0, player1.hp)}")
            
            if player2.hp <= 0 or player1.hp <= 0: break
            
            # 玩家2攻击
            dmg2, logs2, heal2 = cls._execute_attack(player2, player1)
            player1.hp -= dmg2
            player2.hp = min(player2.max_hp, player2.hp + heal2)
            combat_log.extend(logs2)
            combat_log.append(f"{player1.name} 剩余 HP: {max(0, player1.hp)}")
            
            # 反伤判定（针对玩家1的功法）
            counter1 = min(25, max(0, (player1.effects or {}).get("counter_damage", 0)))
            if counter1 > 0:
                reflect_dmg = int(dmg2 * (counter1 / 100))
                player2.hp -= reflect_dmg
                combat_log.append(f"🛡️ {player1.name} 触发反伤，反弹 {reflect_dmg} 点伤害！")
                combat_log.append(f"{player2.name} 剩余 HP: {max(0, player2.hp)}")
                
            combat_log.append("")
        
        # ★ 修改：判定胜负逻辑
        if player2.hp <= 0 and player1.hp > 0:
            winner = player1.user_id
        elif player1.hp <= 0 and player2.hp > 0:
            winner = player2.user_id
        elif player1.hp <= 0 and player2.hp <= 0:
            winner = "平局"
        else:
            # 打满回合未死亡，按剩余血量百分比判定
            p1_ratio = player1.hp / player1.max_hp if player1.max_hp > 0 else 0
            p2_ratio = player2.hp / player2.max_hp if player2.max_hp > 0 else 0
            if p1_ratio > p2_ratio:
                winner = player1.user_id
            elif p2_ratio > p1_ratio:
                winner = player2.user_id
            else:
                winner = "平局"
        
        # 提取真实的结束血量供展示（不打满血）
        real_p1_hp = max(0, player1.hp)
        real_p2_hp = max(0, player2.hp)
        
        if combat_type == 1:
            p1_hp, p2_hp = player1.max_hp, player2.max_hp
        else:
            p1_hp = max(1, real_p1_hp) if real_p1_hp > 0 else 1
            p2_hp = max(1, real_p2_hp) if real_p2_hp > 0 else 1
            
        combat_log = cls._shorten_combat_log(
            combat_log, round_num, winner, player1, player2
        )
        return {
            "winner": winner, "combat_log": combat_log, "rounds": round_num,
            "player1_final_hp": p1_hp, "player1_final_mp": player1.mp,
            "player2_final_hp": p2_hp, "player2_final_mp": player2.mp,
            "player1_real_hp": real_p1_hp, "player2_real_hp": real_p2_hp
        }

    @classmethod
    def player_vs_player_balanced(
        cls,
        player1: CombatStats,
        player2: CombatStats,
        player2_hp_bonus: float = 0.0,
        draw_to_player2: bool = False,
    ) -> Dict:
        """天道压制 PVP（强制拉平基础属性，限5回合）

        player2_hp_bonus：守方气血加成，用于押镖等防守方场景。
        draw_to_player2：5回合未分胜负时，将平局判给守方。
        """
        p1_orig_hp, p1_orig_atk = player1.hp, player1.atk
        p2_orig_hp, p2_orig_atk = player2.hp, player2.atk
        
        # 强制拉平基础属性 (HP 锁定 10000，守方可获得护镖气血加成)
        player1.hp = player1.max_hp = 10000
        player2.hp = player2.max_hp = int(10000 * (1 + max(0.0, player2_hp_bonus)))
        
        # 只保留相对境界基础的有限优势，禁止把高境界绝对数值带入压制赛。
        for stats, original_atk, original_hp in ((player1, p1_orig_atk, p1_orig_hp), (player2, p2_orig_atk, p2_orig_hp)):
            reference = max(1, stats.defense_scale or original_hp / 8)
            relative_bonus = min(.40, max(0, original_atk / reference - 1))
            stats.atk = int(500 * (1 + relative_bonus * .20))
            stats.defense = 100
            stats.defense_scale = 300

        for stats in (player1, player2):
            all_pets = []
            if stats.pet:
                all_pets.append(stats.pet)
            all_pets.extend(stats.support_pets or [])
            for pet in all_pets:
                if pet.hp > 0:
                    pet.atk = 100 + pet.level * 2
                    pet.defense = 50

        combat_log = [
            f"⚖️ 【天道压制】双方基础属性已被强制拉平！",
            f"（但保留了功法、装备、灵兽的特殊加成）",
            f"{player1.name} (ATK {player1.atk}) VS {player2.name} (ATK {player2.atk})",
        ]
        if player2_hp_bonus > 0:
            combat_log.append(
                f"🛡️ {player2.name} 护镖加持，气血 +{int(player2_hp_bonus * 100)}%！"
            )
        combat_log.append("")
        
        round_num = 0
        while player1.hp > 0 and player2.hp > 0 and round_num < 5:  # ★ 修改：最多5回合
            round_num += 1
            combat_log.append(f"-- 第 {round_num} 回合 --")
            
            # 玩家1攻击
            dmg1, logs1, heal1 = cls._execute_attack(player1, player2)
            player2.hp -= dmg1
            player1.hp = min(player1.max_hp, player1.hp + heal1)
            combat_log.extend(logs1)
            combat_log.append(f"{player2.name} 剩余 HP: {max(0, player2.hp)}")
            
            # 反伤
            counter = min(25, max(0, (player2.effects or {}).get("counter_damage", 0)))
            if counter > 0:
                reflect_dmg = int(dmg1 * (counter / 100))
                player1.hp -= reflect_dmg
                combat_log.append(f"🛡️ {player2.name} 触发反伤，反弹 {reflect_dmg} 点伤害！")
            
            if player2.hp <= 0: break
            
            # 玩家2攻击
            dmg2, logs2, heal2 = cls._execute_attack(player2, player1)
            player1.hp -= dmg2
            player2.hp = min(player2.max_hp, player2.hp + heal2)
            combat_log.extend(logs2)
            combat_log.append(f"{player1.name} 剩余 HP: {max(0, player1.hp)}")
            
            # 反伤
            counter1 = min(25, max(0, (player1.effects or {}).get("counter_damage", 0)))
            if counter1 > 0:
                reflect_dmg = int(dmg2 * (counter1 / 100))
                player2.hp -= reflect_dmg
                combat_log.append(f"🛡️ {player1.name} 触发反伤，反弹 {reflect_dmg} 点伤害！")
                
            combat_log.append("")
        
        # ★ 修改：判定胜负逻辑（打满回合按剩余血量百分比判定）
        was_draw = False
        if player2.hp <= 0 and player1.hp > 0:
            winner = player1.user_id
        elif player1.hp <= 0 and player2.hp > 0:
            winner = player2.user_id
        elif player1.hp <= 0 and player2.hp <= 0:
            winner = "平局"
            was_draw = True
        else:
            p1_ratio = player1.hp / player1.max_hp if player1.max_hp > 0 else 0
            p2_ratio = player2.hp / player2.max_hp if player2.max_hp > 0 else 0
            if p1_ratio > p2_ratio:
                winner = player1.user_id
            elif p2_ratio > p1_ratio:
                winner = player2.user_id
            else:
                winner = "平局"
                was_draw = True

        if was_draw and draw_to_player2:
            winner = player2.user_id

        combat_log = cls._shorten_combat_log(
            combat_log, round_num, winner, player1, player2
        )
        return {
            "winner": winner, 
            "combat_log": combat_log, 
            "rounds": round_num,
            "draw": was_draw,
            "player1_final_hp": 1, "player1_final_mp": 1,
            "player2_final_hp": 1, "player2_final_mp": 1,
            "player1_real_hp": max(0, player1.hp), "player2_real_hp": max(0, player2.hp)
        }
    
    @classmethod
    def player_vs_boss(cls, player: CombatStats, boss: CombatStats) -> Dict:
        """玩家vs Boss战斗 (保持100回合)"""
        combat_log = [f"☆━━━━ Boss战开始 ━━━━☆", f"{player.name} 挑战 {boss.name}"]
        round_num = 0
        total_damage_dealt = 0
        
        while player.hp > 0 and boss.hp > 0 and round_num < 100:
            round_num += 1
            combat_log.append(f"-- 第 {round_num} 回合 --")
            
            dmg, logs, heal = cls._execute_attack(player, boss)
            boss.hp -= dmg
            total_damage_dealt += dmg
            player.hp = min(player.max_hp, player.hp + heal)
            combat_log.extend(logs)
            combat_log.append(f"{boss.name} 剩余 HP: {max(0, boss.hp)}")
            
            if boss.hp <= 0: break
            
            b_dmg, b_logs, b_heal = cls._execute_attack(boss, player)
            player.hp -= b_dmg
            combat_log.extend(b_logs)
            combat_log.append(f"{player.name} 剩余 HP: {max(0, player.hp)}")
            combat_log.append("")
            
        if boss.hp <= 0:
            winner = player.user_id
            reward = boss.exp
        elif player.hp <= 0:
            winner = boss.user_id
            reward = int(boss.exp * (total_damage_dealt / boss.max_hp))
        else:
            winner = "平局"
            reward = 0
            
        return {
            "winner": winner, "combat_log": combat_log, "rounds": round_num,
            "player_final_hp": max(1, player.hp), "player_final_mp": player.mp,
            "boss_final_hp": max(0, boss.hp), "reward": reward
        }