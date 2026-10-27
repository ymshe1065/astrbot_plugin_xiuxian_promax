# managers/world_boss_manager.py
import time
import random
from typing import Optional, Tuple, List, Dict
from astrbot.api import logger
from ..data import DataBase
from ..models import Player


class WorldBossManager:
    """全服世界Boss管理器 - 动态水位、种类、掉落与防重复结算"""

    BOSS_TYPES: Dict[str, dict] = {
        "abyss_dragon": {
            "name": "深渊魔龙", "element": "水", "tier": 1, "icon": "🐉",
            "hp_mult": 1.0, "min_level": 3, "drop_table": "low",
            "desc": "蛰伏深渊的远古魔龙，鳞甲如山，吐息可冻结江河。",
        },
        "ancient_demon": {
            "name": "远古天魔", "element": "火", "tier": 1, "icon": "😈",
            "hp_mult": 1.0, "min_level": 3, "drop_table": "low",
            "desc": "自域外降临的天魔残念，焚尽所过之处的一切生机。",
        },
        "nether_king": {
            "name": "九幽冥王", "element": "木", "tier": 1, "icon": "👻",
            "hp_mult": 1.0, "min_level": 3, "drop_table": "low",
            "desc": "统御九幽鬼众的冥王，怨气化藤，缠魂夺魄。",
        },
        "sky_devourer": {
            "name": "吞天巨兽", "element": "土", "tier": 1, "icon": "🦣",
            "hp_mult": 1.0, "min_level": 3, "drop_table": "low",
            "desc": "以山岳为食的太古巨兽，一步便令大地震颤。",
        },
        "blood_ancestor": {
            "name": "血海老祖", "element": "水", "tier": 1, "icon": "🩸",
            "hp_mult": 1.0, "min_level": 3, "drop_table": "low",
            "desc": "盘踞血海万年的邪修老祖，血浪滔天，诡谲难测。",
        },
        "flame_phoenix": {
            "name": "焚天火凤", "element": "火", "tier": 2, "icon": "🔥",
            "hp_mult": 1.6, "min_level": 6, "drop_table": "mid",
            "desc": "涅槃于太阳真火中的上古神禽，双翼可焚山煮海。",
        },
        "frost_python": {
            "name": "玄冥冰蟒", "element": "水", "tier": 2, "icon": "🐍",
            "hp_mult": 1.6, "min_level": 6, "drop_table": "mid",
            "desc": "玄冥寒渊中沉睡的冰蟒，所过之处万里冰封。",
        },
        "golden_ape": {
            "name": "金甲战猿", "element": "金", "tier": 2, "icon": "🦍",
            "hp_mult": 1.6, "min_level": 6, "drop_table": "mid",
            "desc": "身披金甲的太古战猿，一棍可碎星辰，战意滔天。",
        },
        "venom_queen": {
            "name": "万毒蛛后", "element": "木", "tier": 3, "icon": "🕷️",
            "hp_mult": 2.2, "min_level": 9, "drop_table": "high",
            "desc": "万毒之源的蛛后，丝网遮天，毒雾可蚀仙骨。",
        },
        "thunder_qilin": {
            "name": "雷罚麒麟", "element": "金", "tier": 3, "icon": "⚡",
            "hp_mult": 2.5, "min_level": 12, "drop_table": "high",
            "desc": "代天行罚的雷霆麒麟，蹄落之处天雷滚滚，万法辟易。",
        },
    }

    # 世界 Boss 的攻击物法占比
    ATTACK_PROFILES: Dict[str, tuple] = {
        "abyss_dragon": (0.8, 0.2),
        "ancient_demon": (0.2, 0.8),
        "nether_king": (0.5, 0.5),
        "sky_devourer": (0.8, 0.2),
        "blood_ancestor": (0.4, 0.6),
        "flame_phoenix": (0.2, 0.8),
        "frost_python": (0.7, 0.3),
        "golden_ape": (0.8, 0.2),
        "venom_queen": (0.3, 0.7),
        "thunder_qilin": (0.5, 0.5),
    }

    # 世界 Boss 的物法抗性：突出不同 Boss 的攻略差异
    RESIST_PROFILES: Dict[str, tuple] = {
        "abyss_dragon": (60, 20),     # 物抗高，法伤好打
        "ancient_demon": (20, 60),    # 法抗高，物伤好打
        "nether_king": (40, 40),      # 均衡
        "sky_devourer": (60, 20),     # 物抗高
        "blood_ancestor": (40, 60),   # 法抗高
        "flame_phoenix": (20, 60),    # 法抗高
        "frost_python": (60, 30),     # 物抗高
        "golden_ape": (60, 20),       # 物抗高
        "venom_queen": (30, 60),      # 法抗高
        "thunder_qilin": (40, 40),    # 均衡
    }

    TIER_META: Dict[int, dict] = {
        1: {"label": "灭世级", "weight": 60},
        2: {"label": "灾厄级", "weight": 30},
        3: {"label": "神话级", "weight": 10},
    }

    DROP_TABLES: Dict[str, List[dict]] = {
        "low": [
            {"name": "洗练石", "weight": 32, "min": 1, "max": 2},
            {"name": "轮回骨", "weight": 28, "min": 1, "max": 3},
            {"name": "无极骨", "weight": 22, "min": 1, "max": 2},
            {"name": "噬魂铁", "weight": 18, "min": 1, "max": 2},
        ],
        "mid": [
            {"name": "洗练石", "weight": 24, "min": 1, "max": 3},
            {"name": "星辰铁", "weight": 22, "min": 2, "max": 4},
            {"name": "九天骨", "weight": 20, "min": 1, "max": 2},
            {"name": "混沌皮", "weight": 18, "min": 1, "max": 2},
            {"name": "空间石", "weight": 16, "min": 1, "max": 1},
        ],
        "high": [
            {"name": "洗练石", "weight": 20, "min": 2, "max": 4},
            {"name": "八卦日曜晶皮", "weight": 18, "min": 1, "max": 2},
            {"name": "诛仙龙骨铁", "weight": 18, "min": 1, "max": 2},
            {"name": "噬魂幽冥草草", "weight": 16, "min": 1, "max": 2},
            {"name": "紫电草", "weight": 14, "min": 1, "max": 2},
            {"name": "寂灭液", "weight": 14, "min": 1, "max": 1},
        ],
    }

    TIER_REWARDS: Dict[int, dict] = {
        1: {"rank1": 30000, "top3": 15000, "participate": 5000, "killer": 50000},
        2: {"rank1": 60000, "top3": 30000, "participate": 10000, "killer": 100000},
        3: {"rank1": 120000, "top3": 60000, "participate": 20000, "killer": 200000},
    }

    def __init__(self, db: DataBase, config_manager, storage_ring_mgr, title_mgr=None):
        self.db = db
        self.config_manager = config_manager
        self.storage_ring_mgr = storage_ring_mgr
        self.title_mgr = title_mgr
        self.last_challenge_ok = False
        self.last_settle_msg: Optional[str] = None
        self._config = (getattr(config_manager, "boss_config", {}) or {}) if config_manager else {}
        self._world_cfg = self._config.get("world_boss", {}) or {}
        self._types = self._load_types()
        self._tier_meta = self._load_tier_meta()
        self._drop_tables = self._load_drop_tables()
        self._rewards = self._load_rewards()
        self.min_level = int(self._world_cfg.get("min_level", 3) or 3)
        self.cooldown = int(self._world_cfg.get("cooldown", 300) or 300)
        self.damage_floor_pct = float(self._world_cfg.get("damage_floor_pct", 0.5) or 0.5)
        self.damage_cap_pct = float(self._world_cfg.get("damage_cap_pct", 20.0) or 20.0)
        self.expire_hours = int(self._world_cfg.get("expire_hours", 24) or 24)

    def _load_types(self) -> Dict[str, dict]:
        raw = self._world_cfg.get("types")
        if isinstance(raw, dict) and raw:
            result = {k: dict(v) for k, v in self.BOSS_TYPES.items()}
            for key, cfg in raw.items():
                if not isinstance(cfg, dict):
                    continue
                item = dict(result.get(str(key), {}))
                item.update(cfg)
                item.setdefault("name", key)
                result[str(key)] = item
            return result
        if isinstance(raw, list) and raw:
            result = {k: dict(v) for k, v in self.BOSS_TYPES.items()}
            for idx, cfg in enumerate(raw):
                if not isinstance(cfg, dict):
                    continue
                key = str(cfg.get("key") or f"custom_{idx}")
                item = dict(result.get(key, {}))
                item.update(cfg)
                item.setdefault("name", key)
                result[key] = item
            return result
        return {k: dict(v) for k, v in self.BOSS_TYPES.items()}

    def get_resists(self, boss: dict) -> tuple:
        """返回 (物抗%, 法抗%)；优先类型配置，其次内置抗性表。"""
        key = str((boss or {}).get("boss_type") or "")
        cfg = self._types.get(key, {}) or {}
        default_p, default_m = self.RESIST_PROFILES.get(key, (0, 0))
        try:
            physical = float(cfg.get("physical_resist_pct", default_p) or 0)
        except (TypeError, ValueError):
            physical = float(default_p)
        try:
            magic = float(cfg.get("magic_resist_pct", default_m) or 0)
        except (TypeError, ValueError):
            magic = float(default_m)
        return max(0.0, min(90.0, physical)), max(0.0, min(90.0, magic))

    def get_attack_profile(self, boss: dict) -> tuple:
        key = str((boss or {}).get("boss_type") or "")
        cfg = self._types.get(key, {}) or {}
        default_p, default_m = self.ATTACK_PROFILES.get(key, (0.5, 0.5))
        try:
            physical = float(cfg.get("physical_atk_share", default_p) or default_p)
        except (TypeError, ValueError):
            physical = float(default_p)
        try:
            magic = float(cfg.get("magic_atk_share", default_m) or default_m)
        except (TypeError, ValueError):
            magic = float(default_m)
        total = physical + magic
        if total <= 0:
            return 0.5, 0.5
        return physical / total, magic / total

    @staticmethod
    def _defense_from_resist(resist_pct: float) -> int:
        """把抗性百分比换算成 PVP 防御值，保证同样减伤率。"""
        pct = max(0.0, min(90.0, float(resist_pct or 0)))
        if pct <= 0:
            return 0
        return max(0, int(round(100.0 * pct / (100.0 - pct))))

    def build_world_boss_combat_stats(self, boss: dict, type_cfg: dict):
        """构建世界 Boss 的 PVP 战斗面板（只用于无战报模拟）。"""
        from .combat_manager import CombatStats

        max_hp = max(1, int(boss.get("max_hp") or 1))
        current_hp = max(0, int(boss.get("current_hp") or 0))
        atk = max(1, int(boss.get("atk") or max(1, int(max_hp * 0.001))))
        p_share, m_share = self.get_attack_profile(boss)
        physical_atk = int(atk * p_share)
        magic_atk = max(0, int(atk * m_share))
        p_res, m_res = self.get_resists(boss)
        return CombatStats(
            user_id=str(boss.get("boss_id") or "world_boss"),
            name=str(boss.get("boss_name") or type_cfg.get("name") or "世界Boss"),
            hp=current_hp,
            max_hp=max_hp,
            mp=max_hp,
            max_mp=max_hp,
            atk=max(1, physical_atk + magic_atk),
            defense=self._defense_from_resist(p_res) + self._defense_from_resist(m_res),
            physical_atk=physical_atk,
            magic_atk=magic_atk,
            physical_defense=self._defense_from_resist(p_res),
            magic_defense=self._defense_from_resist(m_res),
            crit_rate=30,
            exp=0,
            element=str(type_cfg.get("element") or boss.get("element") or ""),
        )

    async def build_player_boss_stats(self, player: Player):
        """构建世界 Boss 用的玩家面板，并接入灵兽协战。"""
        from .combat_manager import CombatManager
        from .pet_manager import PetManager

        cm = CombatManager()
        impart = None
        try:
            impart = await self.db.ext.get_impart_info(player.user_id)
        except Exception:
            impart = None
        stats = cm.build_player_combat_stats(
            player,
            self.config_manager,
            db=self.db,
            hp_buff=impart.impart_hp_per if impart else 0.0,
            mp_buff=impart.impart_mp_per if impart else 0.0,
            atk_buff=impart.impart_atk_per if impart else 0.0,
            crit_rate=int((impart.impart_know_per if impart else 0.0) * 100),
            crit_damage_bonus=impart.impart_burst_per if impart else 0.0,
        )
        try:
            pet_mgr = PetManager(self.db)
            main_pet, support_pets, pet_effects, owner_atk = await pet_mgr.build_pet_lineup(
                player.user_id,
                stats.max_hp,
                stats.atk,
                stats.defense,
                player.cultivation_type,
                persist=False,
            )
            old_atk = int(stats.atk)
            if owner_atk != old_atk:
                stats.physical_atk = max(
                    0, int(stats.physical_atk) + int(owner_atk - old_atk)
                )
            stats.atk = int(stats.physical_atk) + int(stats.magic_atk)
            stats.pet = main_pet
            stats.support_pets = support_pets
            effects = dict(stats.effects or {})
            for key, value in (pet_effects or {}).items():
                if isinstance(value, (int, float)):
                    effects[key] = effects.get(key, 0) + value
                else:
                    effects[key] = value
            stats.effects = effects
        except Exception:
            pass
        return stats

    async def simulate_battle(self, player: Player, boss: dict, type_cfg: dict) -> dict:
        """PVP 规则 5 回合模拟世界 Boss，只返回结果，不返回逐回合战报。"""
        from .combat_manager import CombatManager

        player_stats = await self.build_player_boss_stats(player)
        boss_stats = self.build_world_boss_combat_stats(boss, type_cfg)
        start_hp = int(boss_stats.hp)
        result = CombatManager.player_vs_player(
            player_stats, boss_stats, combat_type=2
        )
        raw_damage = max(0, start_hp - max(0, int(boss_stats.hp)))
        max_hp = max(1, int(boss.get("max_hp") or 1))
        damage_percent = (raw_damage / max_hp) * 100
        if raw_damage > 0:
            damage_percent = max(self.damage_floor_pct, min(self.damage_cap_pct, damage_percent))
        else:
            damage_percent = 0.0
        final_damage = int(max_hp * damage_percent / 100)
        if final_damage <= 0 and raw_damage <= 0:
            final_damage = 0
        return {
            "damage": max(0, final_damage),
            "raw_damage": max(0, raw_damage),
            "damage_percent": damage_percent,
            "player_element": self.player_element(player),
            "killed": int(boss_stats.hp) <= 0,
            "boss_remaining": max(0, int(boss_stats.hp)),
            "player_remaining": max(0, int(player_stats.hp)),
            "rounds": int(result.get("rounds", 0) or 0),
        }

    def _load_tier_meta(self) -> Dict[int, dict]:
        raw = self._world_cfg.get("tiers")
        result = {k: dict(v) for k, v in self.TIER_META.items()}
        if isinstance(raw, dict):
            for key, cfg in raw.items():
                try:
                    tier = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(cfg, dict):
                    result.setdefault(tier, {}).update(cfg)
        elif isinstance(raw, list):
            for idx, cfg in enumerate(raw, start=1):
                if isinstance(cfg, dict):
                    result.setdefault(idx, {}).update(cfg)
        return result

    def _load_drop_tables(self) -> Dict[str, List[dict]]:
        result = {k: [dict(x) for x in v] for k, v in self.DROP_TABLES.items()}
        raw = self._world_cfg.get("drop_tables")
        if isinstance(raw, dict):
            for key, table in raw.items():
                if isinstance(table, list):
                    result[str(key)] = [dict(x) for x in table]
        return result

    def _load_rewards(self) -> Dict[int, dict]:
        raw = self._world_cfg.get("rewards")
        result = {k: dict(v) for k, v in self.TIER_REWARDS.items()}
        if isinstance(raw, dict):
            for key, cfg in raw.items():
                try:
                    tier = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(cfg, dict):
                    result.setdefault(tier, {}).update(cfg)
        return result

    async def expire_bosses(self, now: int = None) -> int:
        """将已到期的存活Boss标记为逃遁，返回本次清理数量。"""
        if now is None:
            now = int(time.time())
        async with self.db.conn.execute(
            "UPDATE world_boss SET status = 3 WHERE status = 1 AND expire_time <= ?",
            (now,),
        ) as cursor:
            changed = cursor.rowcount
        await self.db.conn.commit()
        return changed

    async def calculate_dynamic_hp(self) -> Tuple[int, int]:
        """动态水位评估：计算全服活跃玩家战力，返回 (目标血量, 参与人数基数)"""
        players = await self.db.get_all_players()
        if not players:
            return 500_000, 10

        active_powers = []
        for p in players:
            if p.experience > 1000:
                power = p.experience + p.physical_damage + p.magic_damage
                active_powers.append(power)

        if not active_powers:
            return 500_000, 10

        active_powers.sort()
        trim_start = int(len(active_powers) * 0.2)
        trim_end = int(len(active_powers) * 0.95)
        valid_powers = active_powers[trim_start:trim_end]
        if not valid_powers:
            valid_powers = active_powers

        avg_power = sum(valid_powers) / len(valid_powers)
        player_count = len(valid_powers)

        target_hp = int(avg_power * player_count * 3)
        target_hp = max(500_000, min(target_hp, 5_000_000_000))
        return target_hp, player_count

    def get_boss_type(self, boss: dict) -> dict:
        """返回Boss种类配置，兼容旧数据。"""
        key = str(boss.get("boss_type") or "")
        if key in self._types:
            cfg = dict(self._types[key])
        else:
            name = str(boss.get("boss_name") or "")
            cfg = next((dict(v) for v in self._types.values() if v.get("name") == name), None)
        if not cfg:
            cfg = {
                "name": boss.get("boss_name") or "未知魔物",
                "element": boss.get("element") or "",
                "tier": 1,
                "icon": "👹",
                "hp_mult": 1.0,
                "min_level": self.min_level,
                "drop_table": "low",
                "desc": "",
            }
        cfg.setdefault("name", boss.get("boss_name") or "未知魔物")
        cfg.setdefault("element", boss.get("element") or "")
        cfg.setdefault("tier", 1)
        cfg.setdefault("min_level", self.min_level)
        cfg.setdefault("drop_table", "low")
        cfg.setdefault("icon", "👹")
        return cfg

    async def _server_max_level(self) -> int:
        try:
            async with self.db.conn.execute("SELECT MAX(level_index) AS lv FROM players") as cursor:
                row = await cursor.fetchone()
            return int(row["lv"]) if row and row["lv"] is not None else 0
        except Exception:
            return 0

    def _choose_boss_key(self, max_level: Optional[int] = None) -> str:
        eligible = self._types
        if max_level is not None:
            filtered = {
                key: cfg for key, cfg in self._types.items()
                if int(cfg.get("min_level", self.min_level) or self.min_level) <= int(max_level)
            }
            if filtered:
                eligible = filtered
            else:
                lowest = min(
                    int(cfg.get("min_level", self.min_level) or self.min_level)
                    for cfg in self._types.values()
                )
                eligible = {
                    key: cfg for key, cfg in self._types.items()
                    if int(cfg.get("min_level", self.min_level) or self.min_level) == lowest
                }
        keys = []
        weights = []
        for key, cfg in eligible.items():
            tier = int(cfg.get("tier", 1) or 1)
            weight = int(cfg.get("weight") or self._tier_meta.get(tier, {}).get("weight", 10))
            keys.append(key)
            weights.append(max(1, weight))
        if not keys:
            return "abyss_dragon"
        return random.choices(keys, weights=weights, k=1)[0]

    async def spawn_boss(self) -> Tuple[bool, str]:
        """生成世界Boss"""
        now = int(time.time())
        await self.expire_bosses(now)

        base_hp, p_count = await self.calculate_dynamic_hp()
        max_level = await self._server_max_level()
        boss_key = self._choose_boss_key(max_level)
        type_cfg = dict(self._types.get(boss_key, {}))
        boss_name = type_cfg.get("name") or boss_key
        element = type_cfg.get("element") or ""
        tier = int(type_cfg.get("tier", 1) or 1)
        max_hp = int(base_hp * float(type_cfg.get("hp_mult", 1.0) or 1.0))
        max_hp = max(500_000, min(max_hp, 12_000_000_000))
        expire_time = now + self.expire_hours * 3600
        tier_label = self._tier_meta.get(tier, {}).get("label", f"{tier}阶")
        physical_resist, magic_resist = self.get_resists({"boss_type": boss_key})

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.conn.execute("SELECT boss_id FROM world_boss WHERE status = 1 LIMIT 1") as cursor:
                if await cursor.fetchone():
                    await self.db.conn.rollback()
                    return False, "❌ 当前已有世界Boss存活！"
            await self.db.conn.execute(
                """INSERT INTO world_boss
                   (boss_name, boss_level, max_hp, current_hp, atk, defense, spawn_time, expire_time,
                    status, boss_type, element, settled, killer_id)
                   VALUES (?, ?, ?, ?, ?, 50, ?, ?, 1, ?, ?, 0, '')""",
                (boss_name, tier, max_hp, max_hp, max(1, int(max_hp * 0.001)), now,
                 expire_time, boss_key, element),
            )
            await self.db.conn.commit()
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"世界Boss生成异常: {e}")
            return False, "❌ 世界Boss生成失败，请稍后再试。"

        preview = self.build_world_boss_combat_stats(
            {
                "boss_id": 0,
                "boss_name": boss_name,
                "max_hp": max_hp,
                "current_hp": max_hp,
                "atk": max(1, int(max_hp * 0.001)),
                "defense": 50,
                "boss_type": boss_key,
                "element": element,
            },
            type_cfg,
        )
        return True, (
            f"{type_cfg.get('icon', '👹')} 【天道警示】世界Boss降临！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👹 名称：{boss_name}\n"
            f"🏷️ 种类：{tier_label} · {element or '无'}属性\n"
            f"🚪 门槛境界：{int(type_cfg.get('min_level', self.min_level) or self.min_level)}\n"
            f"⚔️ 物攻：{preview.physical_atk:,} | 法攻：{preview.magic_atk:,}\n"
            f"🛡️ 物防：{preview.physical_defense:,}（物抗 {physical_resist:.0f}%）| "
            f"法防：{preview.magic_defense:,}（法抗 {magic_resist:.0f}%）\n"
            f"💀 血量：{max_hp:,} （基于全服 {p_count} 名活跃修士战力生成）\n"
            f"⏳ 存活时间：{self.expire_hours}小时\n"
            f"📖 {type_cfg.get('desc', '')}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 发送 /挑战世界Boss 参与讨伐！"
        )

    def player_element(self, player: Player) -> str:
        from .combat_manager import CombatManager
        cm = CombatManager()
        try:
            if self.config_manager and hasattr(self.config_manager, "get_item_element"):
                element = (
                    self.config_manager.get_item_element(player.main_technique)
                    or self.config_manager.get_item_element(player.weapon)
                )
                if element:
                    return element
        except Exception:
            pass
        return (
            cm.get_element_from_name(player.main_technique)
            or cm.get_element_from_name(player.weapon)
            or ""
        )

    async def calculate_realtime_components(self, player: Player) -> tuple:
        """返回 (物攻, 法攻, 总攻击)；灵兽协战按物理攻击计入。"""
        from .combat_manager import CombatManager
        from .pet_manager import PetManager
        cm = CombatManager()
        impart = None
        try:
            impart = await self.db.ext.get_impart_info(player.user_id)
        except Exception:
            impart = None
        stats = cm.build_player_combat_stats(
            player,
            self.config_manager,
            db=self.db,
            hp_buff=impart.impart_hp_per if impart else 0.0,
            mp_buff=impart.impart_mp_per if impart else 0.0,
            atk_buff=impart.impart_atk_per if impart else 0.0,
            crit_rate=int((impart.impart_know_per if impart else 0.0) * 100),
            crit_damage_bonus=impart.impart_burst_per if impart else 0.0,
        )
        physical_atk = int(getattr(stats, "physical_atk", 0) or 0)
        magic_atk = int(getattr(stats, "magic_atk", 0) or 0)
        total_atk = int(stats.atk)
        try:
            pet_mgr = PetManager(self.db)
            main_pet, support_pets, _, owner_atk = await pet_mgr.build_pet_lineup(
                player.user_id,
                stats.max_hp,
                stats.atk,
                stats.defense,
                player.cultivation_type,
                persist=False,
            )
            if owner_atk != stats.atk:
                physical_atk += int(owner_atk - stats.atk)
            for pet in [main_pet] + list(support_pets or []):
                if pet and getattr(pet, "hp", 0) > 0:
                    physical_atk += int(pet.atk)
            total_atk = physical_atk + magic_atk
        except Exception:
            total_atk = int(stats.atk)
        if total_atk <= 0:
            total_atk = 1
        return max(0, physical_atk), max(0, magic_atk), max(1, total_atk)

    async def calculate_realtime_atk(self, player: Player) -> int:
        """兼容旧入口：返回物攻+法攻总值。"""
        return (await self.calculate_realtime_components(player))[2]

    async def calculate_damage(
        self,
        player: Player,
        max_hp: int,
        boss_element: str = "",
        floor_pct: Optional[float] = None,
        cap_pct: Optional[float] = None,
        extra_mult: float = 1.0,
        boss: Optional[dict] = None,
    ) -> Tuple[int, float, str]:
        """计算对Boss的单次伤害，物法分别应用抗性后合计。"""
        floor_pct = self.damage_floor_pct if floor_pct is None else float(floor_pct)
        cap_pct = self.damage_cap_pct if cap_pct is None else float(cap_pct)
        player_element = self.player_element(player)
        physical_atk, magic_atk, total_atk = await self.calculate_realtime_components(player)

        total_base = total_atk * 100 + player.experience * 0.1
        physical_share = physical_atk / total_atk if total_atk > 0 else 0.5
        base_physical = total_base * physical_share
        base_magic = total_base * (1 - physical_share)

        if boss:
            physical_resist, magic_resist = self.get_resists(boss)
        else:
            physical_resist, magic_resist = 0.0, 0.0
        base_damage = int(
            base_physical * (1 - physical_resist / 100)
            + base_magic * (1 - magic_resist / 100)
        )
        try:
            from .combat_manager import CombatManager
            elem_mult = CombatManager.element_multiplier(player_element, boss_element or "")
            base_damage = int(base_damage * elem_mult * max(0.1, extra_mult))
        except Exception:
            pass
        max_hp = max(1, int(max_hp))
        damage_percent = (base_damage / max_hp) * 100
        damage_percent = max(float(floor_pct), min(float(cap_pct), damage_percent))
        return max(1, int(max_hp * (damage_percent / 100))), damage_percent, player_element

    def roll_drop(self, table_key: str) -> Optional[Tuple[str, int]]:
        table = self._drop_tables.get(table_key) or self._drop_tables.get("low") or []
        if not table:
            return None
        total_weight = sum(max(1, int(item.get("weight", 1))) for item in table)
        roll = random.randint(1, total_weight)
        current = 0
        for item in table:
            current += max(1, int(item.get("weight", 1)))
            if roll <= current:
                low = int(item.get("min", 1))
                high = max(low, int(item.get("max", low)))
                return str(item.get("name", "")), random.randint(low, high)
        return None

    async def _grant_title_async(self, player: Player) -> str:
        if not self.title_mgr:
            return ""
        try:
            is_new, msg = self.title_mgr.grant(player, "灭世魔神")
            if not is_new:
                return ""
            fields = {"permanent_bonus": player.permanent_bonus}
            if not player.title:
                fields["title"] = "灭世魔神"
                player.title = "灭世魔神"
            await self.db.update_player_fields(player.user_id, **fields)
            return msg.replace("\n", "")
        except Exception:
            return ""

    async def get_active_boss(self) -> Optional[dict]:
        async with self.db.conn.execute("SELECT * FROM world_boss WHERE status = 1 LIMIT 1") as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def challenge_boss(self, player: Player) -> Tuple[bool, str]:
        """挑战世界Boss"""
        self.last_challenge_ok = False
        now = int(time.time())
        await self.expire_bosses(now)

        boss = await self.get_active_boss()
        if not boss:
            return False, "❌ 当前没有存活的世界Boss。"
        if now >= int(boss.get("expire_time") or 0):
            return False, "❌ Boss已逃遁，请等待下次降临。"

        type_cfg = self.get_boss_type(boss)
        min_level = int(type_cfg.get("min_level", self.min_level) or self.min_level)
        if player.level_index < min_level:
            return False, (
                f"❌ 境界不足，无法参与讨伐！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"【{type_cfg.get('name', boss.get('boss_name'))}】需要境界等级 {min_level}\n"
                f"你当前境界等级：{player.level_index}（还差 {min_level - player.level_index} 级）\n"
                f"💡 通过 /闭关 /历练 提升修为后 /突破 提高境界"
            )

        async with self.db.conn.execute(
            "SELECT * FROM world_boss_damage WHERE boss_id = ? AND user_id = ?",
            (boss["boss_id"], player.user_id),
        ) as cursor:
            record = await cursor.fetchone()
        rec = dict(record) if record else None
        if rec and now - int(rec.get("last_attack_time") or 0) < self.cooldown:
            remaining = self.cooldown - (now - int(rec.get("last_attack_time") or 0))
            return False, f"❌ 调息中，请 {remaining} 秒后再来。"

        battle = await self.simulate_battle(player, boss, type_cfg)
        final_damage = int(battle["damage"])
        damage_percent = float(battle["damage_percent"])
        player_element = battle["player_element"]
        battle_killed = bool(battle["killed"])
        battle_rounds = int(battle.get("rounds", 0) or 0)
        player_remaining = int(battle.get("player_remaining", 0) or 0)

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.conn.execute(
                "SELECT * FROM world_boss WHERE boss_id = ? AND status = 1",
                (boss["boss_id"],),
            ) as cursor:
                fresh_row = await cursor.fetchone()
            if not fresh_row:
                await self.db.conn.rollback()
                return False, "❌ 该Boss已被击杀，请查看最新状态。"
            fresh = dict(fresh_row)
            if now >= int(fresh.get("expire_time") or 0):
                await self.db.conn.execute(
                    "UPDATE world_boss SET status = 3 WHERE boss_id = ? AND status = 1",
                    (boss["boss_id"],),
                )
                await self.db.conn.commit()
                return False, "❌ Boss已逃遁，请等待下次降临。"

            if battle_killed:
                final_damage = int(fresh["current_hp"])
            else:
                final_damage = min(final_damage, int(fresh["current_hp"]))
            if final_damage <= 0:
                await self.db.conn.rollback()
                return False, "❌ Boss已无剩余血量。"
            damage_percent = (final_damage / max(1, int(fresh["max_hp"]))) * 100
            new_hp = int(fresh["current_hp"]) - final_damage
            is_kill = new_hp <= 0

            if is_kill:
                new_hp = 0
                await self.db.conn.execute(
                    "UPDATE world_boss SET current_hp = 0, status = 2, settled = 0, killer_id = ? "
                    "WHERE boss_id = ? AND status = 1",
                    (player.user_id, boss["boss_id"]),
                )
            else:
                await self.db.conn.execute(
                    "UPDATE world_boss SET current_hp = ? WHERE boss_id = ? AND status = 1",
                    (new_hp, boss["boss_id"]),
                )

            async with self.db.conn.execute(
                "SELECT id, last_attack_time FROM world_boss_damage WHERE boss_id = ? AND user_id = ?",
                (boss["boss_id"], player.user_id),
            ) as cursor:
                fresh_record = await cursor.fetchone()
            if fresh_record and now - int(fresh_record["last_attack_time"] or 0) < self.cooldown:
                await self.db.conn.rollback()
                remaining = self.cooldown - (now - int(fresh_record["last_attack_time"] or 0))
                return False, f"❌ 调息中，请 {remaining} 秒后再来。"
            if fresh_record:
                await self.db.conn.execute(
                    "UPDATE world_boss_damage SET total_damage = total_damage + ?, "
                    "attack_count = attack_count + 1, last_attack_time = ? WHERE id = ?",
                    (final_damage, now, fresh_record["id"]),
                )
            else:
                await self.db.conn.execute(
                    "INSERT INTO world_boss_damage "
                    "(boss_id, user_id, total_damage, attack_count, last_attack_time) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (boss["boss_id"], player.user_id, final_damage, now),
                )
            await self.db.conn.commit()
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"世界Boss战斗异常: {e}")
            return False, "❌ 战斗异常，请稍后再试。"

        self.last_challenge_ok = True

        msg = [
            "⚔️ 世界Boss战斗结果",
            "━━━━━━━━━━━━━━━",
            f"Boss：{fresh['boss_name']}",
            f"结果：{'击杀成功' if is_kill else 'Boss未击杀'}",
            f"战斗回合：{battle_rounds}",
            f"造成伤害：{final_damage:,}（{damage_percent:.2f}%）",
            f"Boss剩余：{new_hp:,} / {int(fresh['max_hp']):,}",
            f"你的剩余气血：{player_remaining:,}",
        ]

        if is_kill:
            settle_msg = await self.settle_boss_kill(boss["boss_id"], player.user_id)
            msg.append("\n" + (settle_msg or "🎉 Boss已被击杀，奖励正在结算。"))
        else:
            msg.append("\n💡 使用 /世界Boss状态 查看当前状态")

        return True, "\n".join(msg)

    async def settle_boss_kill(self, boss_id: int, killer_id: str) -> str:
        """结算Boss击杀奖励（幂等，防重复发放）"""
        async with self.db.conn.execute(
            "SELECT * FROM world_boss WHERE boss_id = ?", (boss_id,)
        ) as cursor:
            boss_row = await cursor.fetchone()
        if not boss_row:
            return "❌ Boss记录不存在，无法结算。"
        boss = dict(boss_row)
        if not boss.get("settled"):
            async with self.db.conn.execute(
                "UPDATE world_boss SET settled = 1 WHERE boss_id = ? AND settled = 0",
                (boss_id,),
            ) as cursor:
                claimed = cursor.rowcount
            await self.db.conn.commit()
            if not claimed:
                return ""
        else:
            return ""

        async with self.db.conn.execute(
            "SELECT * FROM world_boss_damage WHERE boss_id = ? ORDER BY total_damage DESC",
            (boss_id,),
        ) as cursor:
            damages = [dict(r) for r in await cursor.fetchall()]

        type_cfg = self.get_boss_type(boss)
        tier = int(type_cfg.get("tier", 1) or 1)
        reward_cfg = self._rewards.get(tier, self._rewards.get(1, {}))
        drop_table = type_cfg.get("drop_table", "low")
        total_damage = sum(int(d.get("total_damage") or 0) for d in damages)
        participant_count = len(damages)

        msg = [
            "🎉 世界Boss已被成功击杀！",
            "━━━━━━━━━━━━━━━",
            f"👹 {boss.get('boss_name', '未知Boss')} · {self._tier_meta.get(tier, {}).get('label', f'{tier}阶')}",
            f"👥 参与修士：{participant_count} 人 · 总伤害：{total_damage:,}",
        ]

        for index, dmg in enumerate(damages):
            rank = index + 1
            target_player = await self.db.get_player_by_id(dmg["user_id"])
            if not target_player:
                continue

            display_name = target_player.user_name or dmg["user_id"]
            if target_player.title:
                display_name = f"【{target_player.title}】{display_name}"

            is_killer = str(dmg["user_id"]) == str(killer_id)
            if rank == 1:
                gold = int(reward_cfg.get("rank1", 30000))
            elif rank <= 3:
                gold = int(reward_cfg.get("top3", 15000))
            else:
                gold = int(reward_cfg.get("participate", 5000))
            if is_killer:
                gold += int(reward_cfg.get("killer", 50000))

            await self.db.add_player_gold(target_player.user_id, gold)
            target_player.gold += gold

            if is_killer:
                rolls = 2 + tier
            elif rank <= 3:
                rolls = 2
            else:
                rolls = 1 if random.random() < 0.65 else 0
            collected = {}
            lost = {}
            drops_to_grant = []
            if is_killer:
                first_drop = self.roll_drop("mid" if tier >= 2 else "low")
                if first_drop:
                    drops_to_grant.append(first_drop)
            for _ in range(rolls):
                drop = self.roll_drop(drop_table)
                if drop:
                    drops_to_grant.append(drop)
            for item_name, count in drops_to_grant:
                stored = await self._store_item(target_player, item_name, count)
                bucket = collected if stored else lost
                bucket[item_name] = bucket.get(item_name, 0) + count
            item_lines = [f"{name}x{count}" for name, count in collected.items()]
            item_lines.extend(f"{name}x{count}（储物戒已满）" for name, count in lost.items())

            title_msg = ""
            if is_killer:
                title_msg = await self._grant_title_async(target_player)
                display_name = f"【{target_player.title or '灭世魔神'}】{target_player.user_name or dmg['user_id']}"

            tag = "👑 尾刀" if is_killer else ("🥇" if rank == 1 else ("🥈" if rank <= 3 else "🥉"))
            line = f"{tag} {rank}. {display_name} - {gold:,}灵石"
            if item_lines:
                line += " · " + "、".join(item_lines)
            if title_msg:
                line += f" · {title_msg}"
            msg.append(line)

        msg.append("━━━━━━━━━━━━━━━")
        msg.append("💡 奖励已发放至各位修士的储物戒/账户！")
        self.last_settle_msg = "\n".join(msg)
        return self.last_settle_msg

    async def _store_item(self, player: Player, item_name: str, count: int) -> bool:
        if not self.storage_ring_mgr:
            return False
        try:
            success, _ = await self.storage_ring_mgr.store_item(player, item_name, count, silent=True)
            return bool(success)
        except Exception as e:
            logger.warning(f"世界Boss掉落发放失败 {item_name}x{count}: {e}")
            return False

    async def settle_pending_bosses(self) -> List[str]:
        """补偿结算所有已击杀但未结算的Boss，返回结算文本列表。"""
        async with self.db.conn.execute(
            "SELECT boss_id, killer_id FROM world_boss WHERE status = 2 AND settled = 0"
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        messages = []
        for row in rows:
            try:
                msg = await self.settle_boss_kill(int(row["boss_id"]), str(row.get("killer_id") or ""))
                if msg:
                    messages.append(msg)
            except Exception as e:
                logger.error(f"补结算世界Boss {row.get('boss_id')} 失败: {e}")
        return messages
