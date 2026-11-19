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

        # 条件原子扣灵石：余额不足时 rowcount=0，杜绝并发透支/旧快照覆盖入账
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (cost_sword, player.user_id, cost_sword),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                fresh = await self.db.get_player_by_id(player.user_id)
                have = fresh.gold if fresh else player.gold
                return False, (
                    f"❌ 灵石不足！需要 {cost_sword:,} 灵石，你当前只有 {have:,}。"
                )
            fresh = await self.db.get_player_by_id(player.user_id)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise
        if fresh:
            player.gold = fresh.gold
        else:
            player.gold -= cost_sword

        # 扣除强化石（retrieve_item 自带事务与回滚）；失败则原路退回灵石
        ok, msg = await self.storage_ring_mgr.retrieve_item(player, "强化石", cost_stone)
        if not ok:
            await self.db.add_player_gold(player.user_id, cost_sword)
            player.gold += cost_sword
            return False, f"❌ 扣除强化石失败：{msg}\n灵石已退回：{cost_sword:,}"

        # 判定成功率（仙缘【百炼成钢】永久加点，最高 95%）
        success_rate = max(0.2, 1.0 - current_level * 0.05)
        try:
            from ..core.reincarnation_rules import permanent_effect

            success_rate = min(0.95, success_rate + permanent_effect(player, "forge_success_bonus"))
        except Exception:
            pass

        if random.random() < success_rate:
            data["forge_level"] = current_level + 1
            player.permanent_bonus = json.dumps(data)
            # 只定向回写炼器字段，避免全字段覆盖抹掉并发期间的其他修改
            await self.db.update_player_fields(
                player.user_id, permanent_bonus=player.permanent_bonus
            )
            return True, f"✨ 炼器成功！你的炼器等级提升至 +{current_level + 1}。\n（全属性基础攻击/防御获得加成）{skill_note}"
        else:
            # 失败无需回写：灵石与强化石均已各自原子持久化
            return False, f"💥 炼器失败！由于火候不佳，消耗了材料和灵石，但等级未提升（当前 +{current_level}）。{skill_note}"

    async def wash_equipment(self, player: Player, slot: str = "") -> Tuple[bool, str]:
        """洗练装备词条（不指定部位时只洗当前武器，指定部位时洗对应槽位）"""
        im = ItemInstanceManager(self.db, self.config_manager)
        equipped_map = player.get_equipped_instances()

        normalized_slot = (slot or "").strip().lower()
        from ..core.item_rules import ItemRules
        slot_aliases = ItemRules.SLOT_ALIASES
        if normalized_slot and normalized_slot not in slot_aliases:
            return False, "❌ 洗练部位：武器/头饰/甲胄/手套/靴子/戒指1/戒指2/护符"
        requested_slot = slot_aliases.get(normalized_slot)
        # 不指定部位时只认武器，避免玩家以为在洗武器、实际洗到别的部位。
        slots = [requested_slot] if requested_slot else ["weapon"]

        target_slot = ""
        target_instance = None
        for slot_key in slots:
            inst = im.get_instance(player, equipped_map.get(slot_key, 0))
            if inst:
                target_instance = inst
                target_slot = slot_key
                break

        if not target_instance:
            if requested_slot:
                return False, "❌ 该部位没有已装备的装备实例，无法洗练。"
            return False, "❌ 你当前没有装备武器，无法洗练。请先装备武器，或用 /洗练 <部位> 指定其他部位。"

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
        # 仙缘【天工开物】：词条数值每档 +15%（无上限的档数由 3 档封顶）
        try:
            from ..core.reincarnation_rules import permanent_effect

            affix_tier = int(permanent_effect(player, "forge_affix_bonus"))
            if affix_tier:
                multiplier *= 1.0 + 0.15 * affix_tier
        except Exception:
            pass
        if isinstance(new_affix["value"], float):
            new_affix["value"] = round(new_affix["value"] * multiplier, 3)
        else:
            new_affix["value"] = int(new_affix["value"] * multiplier)
            
        new_affix["display"] = f"{new_affix['name']}+{new_affix['value']}"
        if isinstance(new_affix["value"], float):
            new_affix["display"] = f"{new_affix['name']}+{new_affix['value']*100:.1f}%"
            
        # 同 key 词条直接替换，避免随机洗练叠出多条暴击率；与定向洗练保持一致。
        affixes = list(target_instance.get("affixes", []) or [])
        replaced = False
        for index, old in enumerate(affixes):
            if isinstance(old, dict) and old.get("key") == new_affix["key"]:
                affixes[index] = new_affix
                replaced = True
                break
        if not replaced:
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

        # 重读最新数据合并本次洗练结果，避免旧快照全字段覆盖并发入账
        fresh = await self.db.get_player_by_id(player.user_id)
        if fresh:
            fresh.set_equipment_instances(player.get_equipment_instances())
            await self.db.update_player(fresh)
            player.gold = fresh.gold
        else:
            await self.db.update_player(player)

        slot_label = ItemRules.SLOT_LABELS.get(target_slot, target_slot)
        msg = [
            "✨ 洗练成功！你获得了新词条：",
            f"👉 {new_affix['display']}",
            f"🎯 目标：{slot_label}·{target_instance.get('name') or '未知装备'}"
            f"（{target_instance.get('quality') or '一般'}）",
            "━━━━━━━━━━━━━━━",
            "【当前装备总词条】"
        ]
        for a in affixes:
            msg.append(f"· {a.get('display', a.get('name', ''))}")

        return True, "\n".join(msg)

    def _find_equipped_target(self, player: Player, slot: str = ""):
        """返回 (装备实例, 错误信息)；洗练/血炼必须针对已装备实例。"""
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
            return None, "❌ 该部位没有已装备的装备实例，无法进行此操作。"
        return None, "❌ 你当前没有已装备的装备实例，无法进行此操作。"

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
        from ..core.item_rules import ItemRules

        target_slot_key = str(target.get("slot") or "").strip()
        if not target_slot_key:
            target_slot_key = ItemRules.SLOT_ALIASES.get(
                (slot or "").strip().lower(), ""
            )
        target_slot_label = ItemRules.SLOT_LABELS.get(
            target_slot_key, slot or "未知部位"
        )
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

        # 条件原子扣灵石：余额不足 rowcount=0，杜绝并发透支/旧快照覆盖入账
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ? WHERE user_id = ? AND gold >= ?",
                (gold_cost, player.user_id, gold_cost),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                fresh = await self.db.get_player_by_id(player.user_id)
                have = fresh.gold if fresh else player.gold
                return False, (
                    f"❌ 灵石不足！需要 {gold_cost:,} 灵石，你当前只有 {have:,}。"
                )
            fresh = await self.db.get_player_by_id(player.user_id)
            await self.db.conn.commit()
        except Exception:
            await self.db.conn.rollback()
            raise
        if fresh:
            player.gold = fresh.gold
        else:
            player.gold -= gold_cost

        ok, msg = await self.storage_ring_mgr.retrieve_item(player, material_name, count)
        if not ok:
            # 材料扣除失败：原路退回灵石
            await self.db.add_player_gold(player.user_id, gold_cost)
            player.gold += gold_cost
            return False, f"❌ 扣除妖兽材料失败：{msg}\n灵石已退回：{gold_cost:,}"
        ok, msg = await self.storage_ring_mgr.retrieve_item(player, "洗练石", 1)
        if not ok:
            # 洗练石扣除失败：退回材料与灵石
            try:
                await self.storage_ring_mgr.store_item(player, material_name, count, silent=True)
            except Exception:
                pass
            await self.db.add_player_gold(player.user_id, gold_cost)
            player.gold += gold_cost
            return False, f"❌ 扣除洗练石失败：{msg}\n已退回【{material_name}】×{count} 与灵石 {gold_cost:,}。"

        success_rate = min(0.98, tier_rule["success"] + (count - 1) * 0.05)
        if random.random() >= success_rate:
            # 失败路径：灵石与材料均已各自原子持久化，无需再全字段回写
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
        # 重读最新数据合并本次洗练结果，避免旧快照全字段覆盖并发入账
        fresh = await self.db.get_player_by_id(player.user_id)
        if fresh:
            fresh.set_equipment_instances(player.get_equipment_instances())
            await self.db.update_player(fresh)
            player.gold = fresh.gold
        else:
            await self.db.update_player(player)

        lines = [
            "✨ 定向洗练成功！",
            f"👉 指定词条：{new_affix['display']}",
            f"🎯 目标：{target_slot_label}·{target.get('name') or '未知装备'}"
            f"（{target.get('quality') or '一般'}）",
            f"消耗：{material_name}×{count}、洗练石×1、{gold_cost:,}灵石",
            "━━━━━━━━━━━━━━━",
            "【当前装备总词条】",
        ]
        lines.extend(f"· {a.get('display', a.get('name', ''))}" for a in affixes)
        master_note = "\n⚒️ 炼器师专精：灵石消耗减半" if getattr(player, "life_skill", "") == "炼器师" else ""
        return True, "\n".join(lines) + master_note
    # ===== 血炼（v7.10.3）=====
    # 以自身精血为引强行淬炼装备，收益高、反噬更狠：
    # 大成可附加超高数值词条，但也可能魔化换词条，甚至把整件装备的词条烧空。
    BLOOD_FORGE_HP_COST = 0.30          # 基础精血消耗（最大气血占比）
    BLOOD_FORGE_HP_BACKLASH = 0.15      # 反噬额外气血
    BLOOD_FORGE_MIN_HP = 0.55           # 低于该气血比例禁止血炼
    BLOOD_FORGE_OUTCOMES = (
        ("great", 10, "血炼大成"),
        ("good", 27, "血炼小成"),
        ("backlash", 28, "血煞反噬"),
        ("corrupt", 25, "魔化"),
        ("ruin", 10, "器毁"),
    )

    def _blood_forge_cost(self, instance: dict) -> int:
        tier = rank_tier(instance.get("rank"), 1)
        cost = int(18000 * (tier ** 1.6))
        return max(8000, cost)

    @staticmethod
    def _scale_affix(base: dict, mult: float) -> dict:
        affix = dict(base)
        if isinstance(affix.get("value"), float):
            affix["value"] = round(affix["value"] * mult, 3)
            affix["display"] = f"{affix['name']}+{affix['value'] * 100:.1f}%"
        else:
            affix["value"] = max(1, int(affix["value"] * mult))
            affix["display"] = f"{affix['name']}+{affix['value']}"
        return affix

    async def blood_forge(self, player: Player, slot: str = "") -> Tuple[bool, str]:
        """血炼：消耗精血与灵石为装备赌一次词条。"""
        target, err = self._find_equipped_target(player, slot)
        if not target:
            return False, err
        try:
            from ..managers.combat_manager import CombatManager
            stats = CombatManager.build_player_combat_stats(
                player, self.config_manager, db=self.db
            )
            max_hp = max(1, int(stats.max_hp))
        except Exception:
            max_hp = max(1, int(player.max_blood_qi or 100))
        hp = max(0, int(player.hp or 0))
        if hp < int(max_hp * self.BLOOD_FORGE_MIN_HP):
            return False, (
                f"❌ 血炼至少需要 {int(self.BLOOD_FORGE_MIN_HP * 100)}% 气血护住心脉。\n"
                f"当前气血：{hp:,}/{max_hp:,}，请先调息恢复。"
            )
        cost = self._blood_forge_cost(target)
        if getattr(player, "life_skill", "") == "炼器师":
            cost //= 2
        if player.gold < cost:
            return False, f"❌ 血炼需要 {cost:,} 灵石布阵，你当前只有 {player.gold:,}。"

        hp_cost = max(1, int(max_hp * self.BLOOD_FORGE_HP_COST))
        hp_after = max(1, hp - hp_cost)
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.conn.execute(
                "UPDATE players SET gold = gold - ?, hp = ? WHERE user_id = ? AND gold >= ?",
                (cost, hp_after, player.user_id, cost),
            )
            if not cursor.rowcount:
                await self.db.conn.rollback()
                return False, "❌ 灵石不足，血炼失败。"
            await self.db.conn.commit()
            player.gold -= cost
            player.hp = hp_after
        except Exception:
            await self.db.conn.rollback()
            raise

        rng = random.Random()
        total = sum(weight for _, weight, _ in self.BLOOD_FORGE_OUTCOMES)
        roll = rng.uniform(0, total)
        acc = 0
        outcome, label = "backlash", "血煞反噬"
        for key, weight, text in self.BLOOD_FORGE_OUTCOMES:
            acc += weight
            if roll <= acc:
                outcome, label = key, text
                break

        affixes = list(target.get("affixes", []) or [])
        extra_lines = []
        if outcome in ("great", "good", "corrupt"):
            base = rng.choice(self.AFFIX_POOL)
            if outcome == "great":
                new_affix = self._scale_affix(base, rng.uniform(1.7, 2.2))
            elif outcome == "good":
                new_affix = self._scale_affix(base, rng.uniform(1.15, 1.4))
            else:
                new_affix = self._scale_affix(base, rng.uniform(1.4, 1.7))
            affixes.append(new_affix)
            if len(affixes) > 3:
                affixes.pop(0)
            extra_lines.append(f"获得血炼词条：{new_affix['display']}")
            if outcome == "corrupt":
                # 只能烧「原有」词条。旧写法从 affixes 里随机 pop，
                # 有 1/N 概率把刚获得的血炼词条自己烧掉——玩家付出 30% 气血
                # 却净亏一条词，还与「原有的…被烧毁」的文案自相矛盾。
                old_idx = [i for i, a in enumerate(affixes) if a is not new_affix]
                if old_idx:
                    burned = affixes.pop(rng.choice(old_idx))
                    extra_lines.append(
                        f"☠️ 魔化反噬，原有的【{burned.get('display', burned.get('name', '词条'))}】被血煞烧毁。"
                    )
                else:
                    extra_lines.append("☠️ 魔化反噬，但装备上没有旧词条可烧，血煞散去。")
                extra_lines.append("☠️ 装备沾染血煞，词条虽强却带着不祥气息。")
        elif outcome == "backlash":
            backlash = max(1, int(max_hp * self.BLOOD_FORGE_HP_BACKLASH))
            player.hp = max(1, player.hp - backlash)
            # 这条裸 UPDATE 之前没有 commit，全靠后面 update_player 顺带提交；
            # 一旦后续写库抛异常，反噬掉的血就会回滚，玩家白嫖一次血炼惩罚。
            await self.db.conn.execute(
                "UPDATE players SET hp = ? WHERE user_id = ?",
                (player.hp, player.user_id),
            )
            await self.db.conn.commit()
            extra_lines.append(f"💥 血煞逆冲心脉，额外损失 {backlash:,} 气血。")
        else:
            if affixes:
                affixes.clear()
                extra_lines.append("💀 血焰失控，装备上的所有词条被烧成灰烬！")
            else:
                extra_lines.append("💀 血焰失控，所幸装备本就没有词条可烧。")

        target["affixes"] = affixes
        im = ItemInstanceManager(self.db, self.config_manager)
        instances = im.load(player)
        for index, inst in enumerate(instances):
            if int(inst.get("id", 0) or 0) == int(target.get("id", 0) or 0):
                instances[index] = target
                break
        im.save(player, instances)
        fresh = await self.db.get_player_by_id(player.user_id)
        if fresh:
            fresh.set_equipment_instances(player.get_equipment_instances())
            fresh.hp = player.hp
            await self.db.update_player(fresh)
        else:
            await self.db.update_player(player)

        lines = [
            f"🩸 血炼 · {label}",
            "━━━━━━━━━━━━━━━",
            f"装备：{target.get('name') or '未知装备'}（{target.get('rank') or '凡品'}）",
            f"消耗：气血 -{hp_cost:,}｜灵石 -{cost:,}",
        ]
        lines.extend(extra_lines)
        if affixes:
            lines.append("【当前装备词条】")
            lines.extend(f"· {a.get('display', a.get('name', ''))}" for a in affixes)
        else:
            lines.append("【当前装备词条】无")
        lines.extend([
            "━━━━━━━━━━━━━━━",
            f"💗 当前气血：{player.hp:,}/{max_hp:,}",
            f"💰 当前灵石：{player.gold:,}",
        ])
        master = "\n⚒️ 炼器师专精：灵石消耗减半" if getattr(player, "life_skill", "") == "炼器师" else ""
        return True, "\n".join(lines) + master
