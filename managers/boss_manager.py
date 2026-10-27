# managers/boss_manager.py
"""
Boss系统管理器 - 处理Boss生成、战斗、奖励等逻辑
"""

import asyncio
import random
import time
from typing import Tuple, Dict, Optional, List, TYPE_CHECKING
from ..data.data_manager import DataBase
from ..models_extended import Boss, UserStatus
from ..models import Player
from .combat_manager import CombatManager, CombatStats

if TYPE_CHECKING:
    from ..core import StorageRingManager


class BossManager:
    """Boss系统管理器"""
    
    BOSS_LEVELS = [
        {"name": "练气", "level_index": 0, "hp_mult": 1.0, "atk_mult": 1.0, "reward_mult": 1.0},
        {"name": "筑基", "level_index": 3, "hp_mult": 1.5, "atk_mult": 1.2, "reward_mult": 1.5},
        {"name": "金丹", "level_index": 6, "hp_mult": 2.0, "atk_mult": 1.5, "reward_mult": 2.0},
        {"name": "元婴", "level_index": 9, "hp_mult": 2.5, "atk_mult": 1.8, "reward_mult": 2.5},
        {"name": "化神", "level_index": 12, "hp_mult": 3.0, "atk_mult": 2.0, "reward_mult": 3.0},
        {"name": "炼虚", "level_index": 15, "hp_mult": 4.0, "atk_mult": 2.5, "reward_mult": 4.0},
        {"name": "合体", "level_index": 18, "hp_mult": 5.0, "atk_mult": 3.0, "reward_mult": 5.0},
        {"name": "大乘", "level_index": 21, "hp_mult": 6.0, "atk_mult": 3.5, "reward_mult": 6.0},
    ]
    
    BOSS_NAMES = [
        "血魔", "邪修", "魔头", "妖王", "魔君",
        "异兽", "凶兽", "妖尊", "魔尊", "邪帝",
        "天魔", "地魔", "魔神", "妖神", "邪神"
    ]

    DEFAULT_ARCHETYPES = {
        "warrior": {
            "name": "狂战型", "weight": 30, "atk_mult": 1.1, "defense_mult": 0.9,
            "physical_atk_share": 0.8, "magic_atk_share": 0.2,
            "physical_defense_share": 0.7, "magic_defense_share": 0.3,
            "defense_flat": 30,
        },
        "mage": {
            "name": "法尊型", "weight": 30, "atk_mult": 1.1, "defense_mult": 0.8,
            "physical_atk_share": 0.2, "magic_atk_share": 0.8,
            "physical_defense_share": 0.3, "magic_defense_share": 0.7,
            "defense_flat": 20,
        },
        "tank": {
            "name": "铁壁型", "weight": 20, "atk_mult": 0.8, "defense_mult": 1.4,
            "physical_atk_share": 0.4, "magic_atk_share": 0.6,
            "physical_defense_share": 0.8, "magic_defense_share": 0.2,
            "defense_flat": 80,
        },
        "balanced": {
            "name": "均衡型", "weight": 20, "atk_mult": 1.0, "defense_mult": 1.0,
            "physical_atk_share": 0.5, "magic_atk_share": 0.5,
            "physical_defense_share": 0.5, "magic_defense_share": 0.5,
            "defense_flat": 40,
        },
    }
    
    BOSS_DROP_TABLE = {
        "low": [
            {"name": "灵兽内丹", "weight": 40, "min": 1, "max": 2},
            {"name": "妖兽精血", "weight": 30, "min": 1, "max": 3},
            {"name": "玄铁", "weight": 30, "min": 3, "max": 6},
        ],
        "mid": [
            {"name": "灵兽内丹", "weight": 30, "min": 2, "max": 4},
            {"name": "星辰石", "weight": 25, "min": 2, "max": 4},
            {"name": "天材地宝", "weight": 20, "min": 1, "max": 2},
            {"name": "功法残页", "weight": 25, "min": 1, "max": 2},
        ],
        "high": [
            {"name": "天材地宝", "weight": 30, "min": 2, "max": 4},
            {"name": "鸿蒙草", "weight": 25, "min": 1, "max": 2},
            {"name": "九天骨", "weight": 20, "min": 1, "max": 2},
            {"name": "混沌皮", "weight": 15, "min": 1, "max": 2},
            {"name": "空间石", "weight": 10, "min": 1, "max": 1},
        ],
    }
    
    def __init__(self, db: DataBase, combat_mgr: CombatManager, config_manager=None, storage_ring_manager: "StorageRingManager" = None):
        self.db = db
        self.combat_mgr = combat_mgr
        self.config_manager = config_manager
        self.storage_ring_manager = storage_ring_manager
        self.config = config_manager.boss_config if config_manager else {}
        self.levels = self.config.get("levels", self.BOSS_LEVELS)
        self.archetypes = self.config.get("archetypes") or self.DEFAULT_ARCHETYPES
        self._battle_lock = None
    
    def _get_battle_lock(self) -> asyncio.Lock:
        if self._battle_lock is None:
            self._battle_lock = asyncio.Lock()
        return self._battle_lock

    def _get_element_of(self, item_name: str) -> str:
        """玩家五行：优先配置显式标注，其次名称推断"""
        if not item_name:
            return ""
        if self.config_manager and hasattr(self.config_manager, "get_item_element"):
            explicit = self.config_manager.get_item_element(item_name)
            if explicit:
                return explicit
        return self.combat_mgr.get_element_from_name(item_name)

    def get_archetype(self, key: str) -> dict:
        cfg = dict(self.archetypes.get(key) or self.archetypes.get("balanced") or {})
        base = dict(self.DEFAULT_ARCHETYPES.get("balanced", {}))
        base.update(cfg)
        return base

    def pick_archetype(self) -> str:
        keys = list(self.archetypes.keys())
        if not keys:
            return "balanced"
        weights = [max(1, int(self.get_archetype(k).get("weight", 1) or 1)) for k in keys]
        return random.choices(keys, weights=weights, k=1)[0]

    def build_boss_combat_stats(self, boss: Boss, element: str = "") -> CombatStats:
        """按 Boss 流派把总 atk/defense 拆成物法双轨。"""
        cfg = self.get_archetype(getattr(boss, "archetype", "balanced"))
        atk_total = int(boss.atk) * float(cfg.get("atk_mult", 1.0) or 1.0)
        physical_atk = int(atk_total * float(cfg.get("physical_atk_share", 0.5) or 0.5))
        magic_atk = int(atk_total * float(cfg.get("magic_atk_share", 0.5) or 0.5))

        defense_total = (int(boss.defense) + int(cfg.get("defense_flat", 0) or 0)) * float(
            cfg.get("defense_mult", 1.0) or 1.0
        )
        physical_defense = int(
            defense_total * float(cfg.get("physical_defense_share", 0.5) or 0.5)
        )
        magic_defense = int(
            defense_total * float(cfg.get("magic_defense_share", 0.5) or 0.5)
        )
        return CombatStats(
            user_id=str(boss.boss_id),
            name=boss.boss_name,
            hp=boss.hp,
            max_hp=boss.max_hp,
            mp=boss.max_hp,
            max_mp=boss.max_hp,
            atk=max(1, physical_atk + magic_atk),
            defense=max(0, physical_defense + magic_defense),
            physical_atk=physical_atk,
            magic_atk=magic_atk,
            physical_defense=physical_defense,
            magic_defense=magic_defense,
            crit_rate=30,
            exp=boss.stone_reward,
            element=element or "",
        )

    async def spawn_boss(
        self,
        base_exp: int = 100000,
        level_config: Optional[Dict] = None
    ) -> Tuple[bool, str, Optional[Boss]]:
        existing_boss = await self.db.ext.get_active_boss()
        if existing_boss:
            return False, f"❌ 当前已有Boss『{existing_boss.boss_name}』存在！", None
        
        if not level_config:
            level_config = random.choice(self.levels)
        
        boss_name = random.choice(self.BOSS_NAMES) + f"·{level_config['name']}境"
        
        hp_mult = level_config["hp_mult"]
        atk_mult = level_config["atk_mult"]
        reward_mult = level_config["reward_mult"]
        
        max_hp = int(base_exp * hp_mult // 2)
        atk = int(base_exp * atk_mult // 10)
        stone_reward = int(base_exp * reward_mult // 10)
        
        defense = 0
        if level_config["level_index"] >= 15:
            defense = random.randint(40, 90)
        archetype = self.pick_archetype()
        archetype_cfg = self.get_archetype(archetype)

        boss = Boss(
            boss_id=0,
            boss_name=boss_name,
            boss_level=level_config["name"],
            hp=max_hp,
            max_hp=max_hp,
            atk=atk,
            defense=defense,
            stone_reward=stone_reward,
            create_time=int(time.time()),
            status=1,
            archetype=archetype,
        )

        preview = self.build_boss_combat_stats(boss, "")
        boss_id = await self.db.ext.create_boss(boss)
        boss.boss_id = boss_id

        msg = f"""
👹 Boss降临
━━━━━━━━━━━━━━━

{boss_name}降临世间！

境界：{level_config["name"]}
流派：{archetype_cfg.get('name', archetype)}
HP：{max_hp}
物攻：{preview.physical_atk:,} | 法攻：{preview.magic_atk:,}
物防：{preview.physical_defense:,} | 法防：{preview.magic_defense:,}
奖励：{stone_reward}灵石

快来挑战吧！
        """.strip()
        
        return True, msg, boss
    
    async def challenge_boss(
        self,
        user_id: str
    ) -> Tuple[bool, str, Optional[Dict]]:
        """旧版 Boss 战全局串行，避免并发击杀重复结算。"""
        async with self._get_battle_lock():
            return await self._challenge_boss_locked(user_id)

    async def _challenge_boss_locked(
        self,
        user_id: str
    ) -> Tuple[bool, str, Optional[Dict]]:
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！", None
        
        boss = await self.db.ext.get_active_boss()
        if not boss:
            return False, "❌ 当前没有Boss！", None
        
        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd:
            await self.db.ext.create_user_cd(user_id)
            user_cd = await self.db.ext.get_user_cd(user_id)
        
        if user_cd.type != UserStatus.IDLE:
            return False, "❌ 你当前正忙，无法挑战Boss！", None
        
        if player.hp <= 1:
            import json
            cooldown_time = 10 * 60
            try:
                extra_data = json.loads(user_cd.extra_data) if user_cd.extra_data else {}
                last_defeat_time = extra_data.get('last_boss_defeat_time', 0)
                if last_defeat_time:
                    if int(time.time()) - last_defeat_time < cooldown_time:
                        remaining_time = cooldown_time - (int(time.time()) - last_defeat_time)
                        minutes = remaining_time // 60
                        seconds = remaining_time % 60
                        return False, f"❌ 你当前血量过低，需要休息一段时间才能再次挑战Boss！\n\n💡 剩余冷却时间：{minutes}分{seconds}秒", None
            except Exception:
                pass
        
        impart_info = await self.db.ext.get_impart_info(user_id)
        hp_buff = impart_info.impart_hp_per if impart_info else 0.0
        mp_buff = impart_info.impart_mp_per if impart_info else 0.0
        atk_buff = impart_info.impart_atk_per if impart_info else 0.0
        crit_rate_buff = impart_info.impart_know_per if impart_info else 0.0
        crit_damage_buff = impart_info.impart_burst_per if impart_info else 0.0
        
        hp, mp = 0, 0
        atk = 1
        try:
            hp, mp = self.combat_mgr.calculate_hp_mp(player.experience, hp_buff, mp_buff)
            # 补全了参数，包括 player.cultivation_type
            atk = self.combat_mgr.calculate_atk(player.experience, player.atkpractice, atk_buff, player.physical_damage, player.magic_damage, player.cultivation_type)
        except Exception:
            pass
        player.hp = hp if hp > 0 else max(1, player.experience // 2)
        player.mp = mp if mp > 0 else max(1, player.experience)
        player.atk = atk if atk > 0 else max(1, player.experience // 10)
        if player.experience < 0: player.experience = 0
        if player.atkpractice < 0: player.atkpractice = 0
        await self.db.update_player(player)

        player_stats = self.combat_mgr.build_player_combat_stats(
            player,
            self.config_manager,
            db=self.db,
            hp_buff=hp_buff,
            mp_buff=mp_buff,
            atk_buff=atk_buff,
            crit_rate=int(crit_rate_buff * 100),
            crit_damage_bonus=crit_damage_buff,
        )

        from .pet_manager import PetManager
        pet_mgr = PetManager(self.db)
        main_pet, support_pets, pet_effects, atk = await pet_mgr.build_pet_lineup(
            user_id, player_stats.max_hp, player_stats.atk,
            player_stats.defense, player.cultivation_type,
        )
        old_atk = int(player_stats.atk)
        if atk != old_atk:
            player_stats.physical_atk = max(
                0, int(getattr(player_stats, "physical_atk", 0)) + int(atk - old_atk)
            )
            player_stats.atk = player_stats.physical_atk + int(
                getattr(player_stats, "magic_atk", 0)
            )
        player_stats.pet = main_pet
        player_stats.support_pets = support_pets
        merged_effects = dict(player_stats.effects or {})
        for key, value in (pet_effects or {}).items():
            if isinstance(value, (int, float)):
                merged_effects[key] = merged_effects.get(key, 0) + value
            else:
                merged_effects[key] = value
        player_stats.effects = merged_effects

        # Boss 随机获得一种五行属性，构成克制关系
        boss_element = random.choice(["金", "木", "水", "火", "土"])
        boss_stats = self.build_boss_combat_stats(boss, boss_element)
        
        battle_result = self.combat_mgr.player_vs_boss(player_stats, boss_stats)
        winner = battle_result["winner"]
        reward = battle_result["reward"]

        pet_states = []
        if player_stats.pet:
            pet_states.append(player_stats.pet)
        pet_states.extend(player_stats.support_pets or [])
        for pet_state in pet_states:
            await pet_mgr.save_pet_combat_state(pet_state)
        
        if winner == user_id:
            boss.status = 0
            await self.db.ext.defeat_boss(boss.boss_id)
            player.gold += reward
            
            item_msg = ""
            dropped_items = []
            if self.storage_ring_manager:
                dropped_items = await self._roll_boss_drops(player, boss)
                if dropped_items:
                    item_lines = []
                    for item_name, count in dropped_items:
                        success, _ = await self.storage_ring_manager.store_item(player, item_name, count, silent=True)
                        if success:
                            item_lines.append(f"  · {item_name} x{count}")
                        else:
                            item_lines.append(f"  · {item_name} x{count}（储物戒已满，丢失）")
                    if item_lines:
                        item_msg = "\n\n📦 获得物品：\n" + "\n".join(item_lines)
            
            result_msg = f"""
🎉 挑战成功！
━━━━━━━━━━━━━━━

你成功击败了『{boss.boss_name}』！

战斗回合数：{battle_result['rounds']}
获得灵石：{reward}{item_msg}

{player_stats.name}
HP：{battle_result['player_final_hp']}/{player_stats.max_hp}
            """.strip()
        else:
            boss.hp = battle_result["boss_final_hp"]
            await self.db.ext.update_boss(boss)
            
            result_msg = f"""
💀 挑战失败
━━━━━━━━━━━━━━━

你被『{boss.boss_name}』击败了！

战斗回合数：{battle_result['rounds']}
安慰奖：{reward}灵石

{boss.boss_name} 剩余HP：{boss.hp}/{boss.max_hp}
            """.strip()
            
            if reward > 0:
                player.gold += reward
        
        player.hp = battle_result["player_final_hp"]
        player.mp = battle_result["player_final_mp"]
        await self.db.update_player(player)
        
        if battle_result["winner"] != user_id and player.hp <= 1:
            import json
            try:
                extra_data = json.loads(user_cd.extra_data) if user_cd.extra_data else {}
                extra_data['last_boss_defeat_time'] = int(time.time())
                user_cd.extra_data = json.dumps(extra_data)
                await self.db.ext.update_user_cd(user_cd)
            except Exception:
                pass
        
        combat_log = "\n".join(battle_result["combat_log"])
        full_msg = combat_log + "\n\n" + result_msg
        
        return True, full_msg, battle_result
    
    async def get_boss_info(self) -> Tuple[bool, str, Optional[Boss]]:
        boss = await self.db.ext.get_active_boss()
        if not boss:
            # 读取真实的持久化刷新时间，不要每次查询都 now + 2 小时
            next_spawn_str = await self.db.ext.get_system_config("boss_next_spawn_time")
            try:
                next_spawn_time = int(next_spawn_str) if next_spawn_str else int(time.time()) + 2 * 3600
            except (TypeError, ValueError):
                next_spawn_time = int(time.time()) + 2 * 3600

            if next_spawn_time <= int(time.time()):
                next_time_str = "即将刷新"
            else:
                next_time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_spawn_time))
            return False, f"❌ 当前没有Boss！\n\n💡 预计下一个Boss将在 {next_time_str} 复活", None
        
        hp_percent = (boss.hp / boss.max_hp) * 100
        
        msg = f"""
👹 当前Boss
━━━━━━━━━━━━━━━

名称：{boss.boss_name}
境界：{boss.boss_level}

HP：{boss.hp}/{boss.max_hp} ({hp_percent:.1f}%)
ATK：{boss.atk}
防御：{boss.defense}%减伤

奖励：{boss.stone_reward}灵石

使用 /挑战Boss 来挑战！

💡 提示：全服共斗的新版世界Boss请使用
   /世界Boss状态 /挑战世界Boss
        """.strip()
        
        return True, msg, boss
    
    async def auto_spawn_boss(self, player_count: int = 0) -> Tuple[bool, str, Optional[Boss]]:
        existing_boss = await self.db.ext.get_active_boss()
        if existing_boss:
            return False, "当前已有Boss存在", None
        
        all_players = await self.db.get_all_players()
        if not all_players:
            level_config = self.levels[0]
            base_exp = 50000
        else:
            total_exp = sum(p.experience for p in all_players)
            avg_exp = total_exp // len(all_players) if all_players else 50000
            for config in reversed(self.levels):
                if avg_exp >= config.get("level_index", 0) * 10000:
                    level_config = config
                    break
            else:
                level_config = self.levels[0]
            base_exp = int(avg_exp * 1.2)
        
        return await self.spawn_boss(base_exp, level_config)
    
    async def _roll_boss_drops(self, player: Player, boss: Boss) -> List[Tuple[str, int]]:
        dropped_items = []
        boss_level_index = 0
        for level in self.levels:
            if level["name"] == boss.boss_level:
                boss_level_index = level["level_index"]
                break
        
        if boss_level_index <= 6:
            drop_table = self.BOSS_DROP_TABLE["low"]
        elif boss_level_index <= 12:
            drop_table = self.BOSS_DROP_TABLE["mid"]
        else:
            drop_table = self.BOSS_DROP_TABLE["high"]
        
        total_weight = sum(item["weight"] for item in drop_table)
        roll = random.randint(1, total_weight)
        
        current_weight = 0
        for item in drop_table:
            current_weight += item["weight"]
            if roll <= current_weight:
                count = random.randint(item["min"], item["max"])
                dropped_items.append((item["name"], count))
                break
        
        if boss_level_index >= 9:
            extra_chance = 50 if boss_level_index < 15 else 70
            if random.randint(1, 100) <= extra_chance:
                roll = random.randint(1, total_weight)
                current_weight = 0
                for item in drop_table:
                    current_weight += item["weight"]
                    if roll <= current_weight:
                        count = random.randint(item["min"], item["max"])
                        dropped_items.append((item["name"], count))
                        break
        
        return dropped_items