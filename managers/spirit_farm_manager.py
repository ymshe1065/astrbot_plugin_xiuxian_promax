# managers/spirit_farm_manager.py
"""灵田系统管理器"""
import time
import json
import random
from typing import Tuple, Optional, Dict, List, TYPE_CHECKING
from ..data import DataBase
from ..models import Player

if TYPE_CHECKING:
    from ..core import StorageRingManager

__all__ = ["SpiritFarmManager"]

# 灵草配置 (wither_time: 成熟后枯萎时间，默认48小时)
# unlock_level: 灵田达到该等级后方可种植
SPIRIT_HERBS = {
    "灵草": {"grow_time": 3600, "exp_yield": 500, "gold_yield": 100, "wither_time": 172800, "unlock_level": 1},
    "血灵草": {"grow_time": 7200, "exp_yield": 1500, "gold_yield": 300, "wither_time": 172800, "unlock_level": 1},
    "冰心草": {"grow_time": 14400, "exp_yield": 4000, "gold_yield": 800, "wither_time": 172800, "unlock_level": 2},
    "火焰花": {"grow_time": 28800, "exp_yield": 10000, "gold_yield": 2000, "wither_time": 172800, "unlock_level": 3},
    "九叶灵芝": {"grow_time": 86400, "exp_yield": 30000, "gold_yield": 6000, "wither_time": 172800, "unlock_level": 4},
    "幽冥草": {"grow_time": 172800, "exp_yield": 100000, "gold_yield": 25000, "wither_time": 172800, "unlock_level": 5},
    # 杂交灵草：需先通过 /杂交 获得种苗，种植时消耗 1 株
    "冰火双生花": {"grow_time": 43200, "exp_yield": 25000, "gold_yield": 8000, "wither_time": 172800, "unlock_level": 5, "hybrid_only": True},
    "血玉灵芝": {"grow_time": 86400, "exp_yield": 60000, "gold_yield": 15000, "wither_time": 172800, "unlock_level": 4, "hybrid_only": True},
    "九幽仙芝": {"grow_time": 172800, "exp_yield": 150000, "gold_yield": 35000, "wither_time": 172800, "unlock_level": 5, "hybrid_only": True},
}

# 杂交配方：材料 -> 产物
HYBRID_RECIPES = {
    frozenset({"火焰花", "冰心草"}): "冰火双生花",
    frozenset({"血灵草", "九叶灵芝"}): "血玉灵芝",
    frozenset({"九叶灵芝", "幽冥草"}): "九幽仙芝",
}

# ===== 季节系统（v6.14）=====
# 按年内第几周自动轮转（每约一周一换，无需存储，全服同步可预测）
SEASON_EFFECTS = {
    0: {"name": "春·生发", "icon": "🌱",
        "desc": "万物生发：所有灵草生长速度+33%，产量+10%",
        "growth_mult": 0.75, "yield_mult": 1.10, "boost_herbs": ["*"]},
    1: {"name": "夏·炎灼", "icon": "☀️",
        "desc": "烈日炎炎：阳性灵草（火焰花/血灵草/冰火双生花）生长+33%",
        "growth_mult": 1.00, "yield_mult": 1.00,
        "boost_herbs": ["火焰花", "血灵草", "冰火双生花"], "boost_growth": 0.75},
    2: {"name": "秋·丰收", "icon": "🍂",
        "desc": "天高气爽：所有灵草产量+25%",
        "growth_mult": 1.00, "yield_mult": 1.25, "boost_herbs": []},
    3: {"name": "冬·蛰藏", "icon": "❄️",
        "desc": "天地蛰藏：生长放缓50%，唯有喜寒的冰心草不受影响",
        "growth_mult": 1.50, "yield_mult": 1.00,
        "boost_herbs": ["冰心草"], "boost_growth": 1.00},
}


def get_current_season_index() -> int:
    """当前季节索引（0春1夏2秋3冬）：年内第几周 % 4"""
    return (time.localtime().tm_yday // 7) % 4


def get_current_season() -> dict:
    return SEASON_EFFECTS[get_current_season_index()]


def get_season_growth_mult(herb_name: str) -> float:
    """某灵草在当前季节的生长时长倍率（越小越快）"""
    season = get_current_season()
    boost = season.get("boost_herbs", [])
    if "*" in boost:
        return season["growth_mult"]
    if herb_name in boost:
        return season.get("boost_growth", season["growth_mult"])
    return season["growth_mult"]


def get_season_yield_mult() -> float:
    """当前季节的产量倍率"""
    return get_current_season()["yield_mult"]


def get_herb_unlock_level(herb_name: str) -> int:
    """灵草所需灵田等级"""
    return int(SPIRIT_HERBS.get(herb_name, {}).get("unlock_level", 1) or 1)


def get_herbs_for_level(level: int) -> List[str]:
    """当前灵田等级可种植的灵草"""
    level = int(level or 1)
    return [name for name, cfg in SPIRIT_HERBS.items() if int(cfg.get("unlock_level", 1) or 1) <= level]


def get_next_unlock(level: int) -> Tuple[Optional[int], List[str]]:
    """返回下一档解锁等级及对应灵草，已全部解锁返回 (None, [])"""
    level = int(level or 1)
    locked = {
        int(cfg.get("unlock_level", 1) or 1): []
        for cfg in SPIRIT_HERBS.values()
        if int(cfg.get("unlock_level", 1) or 1) > level
    }
    for name, cfg in SPIRIT_HERBS.items():
        need = int(cfg.get("unlock_level", 1) or 1)
        if need > level:
            locked[need].append(name)
    if not locked:
        return None, []
    next_level = min(locked)
    return next_level, locked[next_level]


def requires_seed(herb_name: str) -> bool:
    """杂交灵草需消耗同名种苗才能种植"""
    return bool(SPIRIT_HERBS.get(herb_name, {}).get("hybrid_only"))


def get_hybrid_formula(product: str) -> str:
    """返回杂交产物对应的材料公式"""
    for materials, name in HYBRID_RECIPES.items():
        if name == product:
            return " + ".join(sorted(materials))
    return ""


def format_hybrid_recipes(level: Optional[int] = None) -> str:
    """格式化全部杂交配方；传入等级时标记是否可种植"""
    lines = []
    for materials, product in HYBRID_RECIPES.items():
        need = get_herb_unlock_level(product)
        lock = ""
        if level is not None:
            lock = " ✅" if int(level or 1) >= need else f" 🔒Lv.{need}"
        lines.append(
            f"  · {' + '.join(sorted(materials))} → {product}（种植需灵田 Lv.{need}）{lock}"
        )
    return "\n".join(lines)


HYBRID_MARKER = "__HYBRID__"
HYBRID_PLOT_COST = 2


def collect_hybrid_jobs(crops: List[Dict]) -> List[Dict]:
    """收集灵田中正在占用格子的杂交任务。"""
    jobs: Dict[str, Dict] = {}
    for crop in crops or []:
        if crop.get("name") != HYBRID_MARKER:
            continue
        job_id = str(crop.get("job_id") or "")
        if not job_id:
            continue
        job = jobs.get(job_id)
        if not job:
            job = {
                "job_id": job_id,
                "slots": [],
                "product": crop.get("product") or "",
                "parents": list(crop.get("parents") or []),
                "plant_time": int(crop.get("plant_time") or 0),
                "mature_time": int(crop.get("mature_time") or 0),
            }
            jobs[job_id] = job
        slot = int(crop.get("slot") or 0)
        if slot and slot not in job["slots"]:
            job["slots"].append(slot)
        job["mature_time"] = max(job["mature_time"], int(crop.get("mature_time") or 0))
    for job in jobs.values():
        job["slots"].sort()
    return sorted(jobs.values(), key=lambda item: item["mature_time"])


def remove_hybrid_job(crops: List[Dict], job_id: str) -> None:
    crops[:] = [crop for crop in crops if str(crop.get("job_id") or "") != str(job_id)]


# 灵田等级配置
FARM_LEVELS = {
    1: {"slots": 3, "upgrade_cost": 5000},
    2: {"slots": 5, "upgrade_cost": 15000},
    3: {"slots": 8, "upgrade_cost": 50000},
    4: {"slots": 12, "upgrade_cost": 150000},
    5: {"slots": 20, "upgrade_cost": 0},  # 最高级
}


class SpiritFarmManager:
    """灵田管理器"""
    
    def __init__(self, db: DataBase, storage_ring_manager: "StorageRingManager" = None, config_manager=None):
        self.db = db
        self.storage_ring_manager = storage_ring_manager
        self.config_manager = config_manager
        self.last_harvest_count = 0
        self.last_harvest_counts = {}
    
    @staticmethod
    def get_max_slots(farm: Dict) -> int:
        """灵田当前可种植格数"""
        level = int((farm or {}).get("level", 1) or 1)
        return FARM_LEVELS.get(level, FARM_LEVELS[1])["slots"]

    @staticmethod
    def _normalize_crop_slots(crops: List[Dict], max_slots: int) -> List[Dict]:
        """为旧灵田补齐稳定格号，避免收获后格号整体前移"""
        limit = max(max_slots, len(crops), 1)
        used = set()
        for crop in crops:
            try:
                slot = int(crop.get("slot"))
            except (TypeError, ValueError):
                slot = 0
            if 1 <= slot <= limit and slot not in used:
                crop["slot"] = slot
                used.add(slot)
            else:
                crop["slot"] = 0
        next_slot = 1
        for crop in crops:
            if crop.get("slot"):
                continue
            while next_slot in used:
                next_slot += 1
            crop["slot"] = next_slot
            used.add(next_slot)
        crops.sort(key=lambda c: int(c.get("slot") or 0))
        return crops

    async def get_user_farm(self, user_id: str) -> Optional[Dict]:
        """获取用户灵田信息"""
        async with self.db.conn.execute(
            "SELECT * FROM spirit_farms WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                data = dict(row)
                data["crops"] = json.loads(data.get("crops", "[]"))
                if not data.get("fertility"):
                    data["fertility"] = 100
                data["crops"] = self._normalize_crop_slots(
                    data["crops"], self.get_max_slots(data)
                )
                return data
            return None
    
    async def create_farm(self, player: Player) -> Tuple[bool, str]:
        """开垦灵田"""
        existing = await self.get_user_farm(player.user_id)
        if existing:
            return False, "❌ 你已经拥有灵田了！"
        
        cost = 10000
        if player.gold < cost:
            return False, f"❌ 开垦灵田需要 {cost:,} 灵石。"
        
        player.gold -= cost
        await self.db.update_player_fields(player.user_id, gold=player.gold)
        
        await self.db.conn.execute(
            """
            INSERT INTO spirit_farms (user_id, level, crops)
            VALUES (?, 1, '[]')
            """,
            (player.user_id,)
        )
        await self.db.conn.commit()
        
        unlocked = "、".join(get_herbs_for_level(1))
        return True, (
            "🌱 灵田开垦成功！\n"
            "━━━━━━━━━━━━━━━\n"
            "灵田等级：Lv.1\n"
            "种植格数：3\n"
            f"可种植：{unlocked}\n"
            "━━━━━━━━━━━━━━━\n"
            "💡 升级灵田可解锁更高级的灵草"
        )
    
    async def plant_herb(self, player: Player, herb_name: str, count: int = 1) -> Tuple[bool, str]:
        """种植灵草"""
        if herb_name not in SPIRIT_HERBS:
            herbs_list = "、".join(SPIRIT_HERBS.keys())
            return False, f"❌ 未知的灵草。可种植：{herbs_list}"
        
        farm = await self.get_user_farm(player.user_id)
        if not farm:
            return False, "❌ 你还没有灵田！使用 /开垦灵田"

        farm_level = int(farm.get("level", 1) or 1)
        need_level = get_herb_unlock_level(herb_name)
        if farm_level < need_level:
            return False, (
                f"🔒 【{herb_name}】需要灵田 Lv.{need_level} 才能种植（当前 Lv.{farm_level}）。\n"
                "💡 使用 /升级灵田 提升灵田等级"
            )
        
        max_slots = self.get_max_slots(farm)
        crops = farm["crops"]
        used_slots = {int(c.get("slot") or 0) for c in crops}
        free_slots = [s for s in range(1, max_slots + 1) if s not in used_slots]
        
        available_slots = len(free_slots)
        if available_slots <= 0:
            return False, f"❌ 灵田已满！最多种植 {max_slots} 株。"
        
        # 调整种植数量
        plant_count = min(count, available_slots)
        if plant_count <= 0:
            return False, "❌ 种植数量必须大于0！"

        # 杂交灵草必须消耗同名种苗（由 /杂交 或坊市交易获得）
        if requires_seed(herb_name):
            if not self.storage_ring_manager:
                return False, "❌ 系统异常：储物戒管理器未加载，无法消耗种苗。"
            have = self.storage_ring_manager.get_item_count(player, herb_name)
            if have < plant_count:
                formula = get_hybrid_formula(herb_name)
                source = f"通过 /杂交 {formula} 合成" if formula else "通过 /杂交 合成"
                return False, (
                    f"❌ 【{herb_name}】种苗不足（拥有 {have}，需要 {plant_count}）。\n"
                    f"💡 请先{source}，或在坊市购买后再种植。"
                )
            ok, seed_msg = await self.storage_ring_manager.retrieve_item(player, herb_name, plant_count)
            if not ok:
                return False, f"❌ 扣除【{herb_name}】种苗失败：{seed_msg}"

        # 批量种植（成熟时间受季节影响）
        herb_config = SPIRIT_HERBS[herb_name]
        plant_time = int(time.time())
        grow_time = int(herb_config["grow_time"] * get_season_growth_mult(herb_name))
        mature_time = plant_time + grow_time

        for slot in free_slots[:plant_count]:
            crops.append({
                "name": herb_name,
                "slot": slot,
                "plant_time": plant_time,
                "mature_time": mature_time
            })
        self._normalize_crop_slots(crops, max_slots)

        await self.db.conn.execute(
            "UPDATE spirit_farms SET crops = ? WHERE user_id = ?",
            (json.dumps(crops), player.user_id)
        )
        await self.db.conn.commit()

        season = get_current_season()
        grow_hours = grow_time // 3600
        season_note = ""
        if get_season_growth_mult(herb_name) != 1.0:
            season_note = f"\n{season['icon']} 当前季节【{season['name']}】影响了生长速度"
        return True, (
            f"🌱 成功种植【{herb_name}】！\n"
            f"成熟时间：约 {grow_hours} 小时"
            f"{season_note}\n"
            f"当前种植：{len(crops)}/{max_slots}"
        )

    async def plant_herb_at_slots(
        self, player: Player, assignments: Dict[int, str]
    ) -> Tuple[int, str]:
        """在指定空格种植指定灵草；返回成功株数与提示。"""
        if not assignments:
            return 0, ""
        farm = await self.get_user_farm(player.user_id)
        if not farm:
            return 0, "❌ 你还没有灵田！"
        max_slots = self.get_max_slots(farm)
        farm_level = int(farm.get("level", 1) or 1)
        crops = farm["crops"]
        used_slots = {int(c.get("slot") or 0) for c in crops}
        planted = []
        plant_time = int(time.time())
        for raw_slot, herb_name in assignments.items():
            try:
                slot = int(raw_slot)
            except (TypeError, ValueError):
                continue
            if slot < 1 or slot > max_slots or slot in used_slots:
                continue
            if herb_name not in SPIRIT_HERBS:
                continue
            if farm_level < get_herb_unlock_level(herb_name):
                continue
            if requires_seed(herb_name):
                if not self.storage_ring_manager:
                    continue
                ok, _ = await self.storage_ring_manager.retrieve_item(player, herb_name, 1)
                if not ok:
                    continue
            herb_config = SPIRIT_HERBS[herb_name]
            grow_time = int(herb_config["grow_time"] * get_season_growth_mult(herb_name))
            crops.append({
                "name": herb_name,
                "slot": slot,
                "plant_time": plant_time,
                "mature_time": plant_time + grow_time,
            })
            used_slots.add(slot)
            planted.append(f"格{slot} {herb_name}")
        if not planted:
            return 0, ""
        self._normalize_crop_slots(crops, max_slots)
        await self.db.conn.execute(
            "UPDATE spirit_farms SET crops = ? WHERE user_id = ?",
            (json.dumps(crops), player.user_id)
        )
        await self.db.conn.commit()
        return len(planted), f"♻️ 已续种：{'、'.join(planted)}"

    async def remove_crop(self, player: Player, slot: int) -> Tuple[bool, str]:
        """铲除指定格子的灵草（无产出）。"""
        farm = await self.get_user_farm(player.user_id)
        if not farm:
            return False, "❌ 你还没有灵田！"
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            return False, "❌ 请提供要铲除的格子序号。"
        crops = farm["crops"]
        target = next((c for c in crops if int(c.get("slot") or 0) == slot), None)
        if not target:
            return False, f"❌ 格{slot} 没有种植灵草。"
        if target.get("name") == HYBRID_MARKER:
            job_id = str(target.get("job_id") or "")
            job = next((j for j in collect_hybrid_jobs(crops) if j["job_id"] == job_id), None)
            remove_hybrid_job(crops, job_id)
            await self.db.conn.execute(
                "UPDATE spirit_farms SET crops = ? WHERE user_id = ?",
                (json.dumps(crops, ensure_ascii=False), player.user_id)
            )
            await self.db.conn.commit()
            slots_text = "、".join(f"格{s}" for s in (job or {}).get("slots", [slot]))
            product = (job or {}).get("product", "杂交植株")
            return True, f"🪓 已取消 {slots_text} 的杂交【{product}】（材料不返还）。"
        crops.remove(target)
        await self.db.conn.execute(
            "UPDATE spirit_farms SET crops = ? WHERE user_id = ?",
            (json.dumps(crops), player.user_id)
        )
        await self.db.conn.commit()
        return True, f"🪓 已铲除格{slot}的【{target.get('name', '灵草')}】。"
    
    async def harvest(self, player: Player, slots: Optional[List[int]] = None) -> Tuple[bool, str]:
        """收获灵草"""
        self.last_harvest_count = 0
        self.last_harvest_counts[player.user_id] = 0
        farm = await self.get_user_farm(player.user_id)
        if not farm:
            return False, "❌ 你还没有灵田！"
        
        crops = farm["crops"]
        if not crops:
            return False, "❌ 灵田里没有种植任何灵草。"
        
        now = int(time.time())
        mature_crops = []
        withered_crops = []
        remaining_crops = []
        target_slots = None
        if slots is not None:
            target_slots = set()
            for value in slots:
                try:
                    target_slots.add(int(value))
                except (TypeError, ValueError):
                    continue

        hybrid_jobs = collect_hybrid_jobs(crops)
        completed_jobs = []
        for job in hybrid_jobs:
            selected = target_slots is None or bool(set(job["slots"]) & target_slots)
            if selected and now >= job["mature_time"]:
                completed_jobs.append(job)
        completed_job_ids = {job["job_id"] for job in completed_jobs}

        for job in hybrid_jobs:
            if job["job_id"] in completed_job_ids:
                continue
            remaining_crops.extend(
                crop for crop in crops
                if str(crop.get("job_id") or "") == job["job_id"]
            )

        for crop in crops:
            if crop.get("name") == HYBRID_MARKER:
                continue
            if target_slots is not None and int(crop.get("slot") or 0) not in target_slots:
                remaining_crops.append(crop)
                continue
            if now >= crop["mature_time"]:
                herb_config = SPIRIT_HERBS.get(crop["name"], SPIRIT_HERBS["灵草"])
                wither_time = herb_config.get("wither_time", 172800)
                wither_deadline = crop["mature_time"] + wither_time
                if now >= wither_deadline:
                    withered_crops.append(crop)
                else:
                    mature_crops.append(crop)
            else:
                remaining_crops.append(crop)

        if not mature_crops and not withered_crops and not completed_jobs:
            return False, "❌ 没有成熟的灵草或杂交产物可以收获。"

        self.last_harvest_count = len(mature_crops) + len(completed_jobs)
        self.last_harvest_counts[player.user_id] = len(mature_crops) + len(completed_jobs)

        # 计算奖励（只有成熟未枯萎的才有收益；产量受土壤肥力与季节影响）
        fertility = farm.get("fertility", 100)
        fertility_mult = fertility / 100.0
        season = get_current_season()
        season_mult = get_season_yield_mult()
        total_exp = 0
        total_gold = 0
        harvest_details = []
        hybrid_details = []
        herb_counts = {}
        shield_penalized = False

        for job in completed_jobs:
            product = job.get("product")
            if product in SPIRIT_HERBS:
                herb_counts[product] = herb_counts.get(product, 0) + 1
                hybrid_details.append(product)

        for crop in mature_crops:
            herb_name = crop["name"]
            herb_config = SPIRIT_HERBS.get(herb_name, SPIRIT_HERBS["灵草"])
            mature_time = crop.get("mature_time", now)
            plant_time = crop.get("plant_time", mature_time)
            output_mult = player.shield_output_multiplier(plant_time, mature_time)
            if output_mult < 1.0:
                shield_penalized = True
            total_exp += int(herb_config["exp_yield"] * fertility_mult * season_mult * output_mult)
            total_gold += int(herb_config["gold_yield"] * fertility_mult * season_mult * output_mult)
            harvest_details.append(herb_name)
            # 统一规则：每株成功种植的灵草收获 4 株
            herb_counts[herb_name] = herb_counts.get(herb_name, 0) + 4
        
        # 应用奖励
        if total_exp > 0 or total_gold > 0:
            try:
                from ..core.technique_bonus import TechniqueBonusManager
                total_gold = TechniqueBonusManager.apply_gold_bonus(
                    player, total_gold, self.config_manager
                )
            except Exception:
                pass
            player.experience += total_exp
            player.gold += total_gold
            # 只回写收益字段，避免旧快照覆盖当前气血/灵气
            await self.db.update_player_fields(
                player.user_id,
                experience=player.experience,
                gold=player.gold,
            )
        
        # 将灵草存入储物戒
        stored_items = []
        if self.storage_ring_manager:
            for herb_name, count in herb_counts.items():
                success, _ = await self.storage_ring_manager.store_item(player, herb_name, count, silent=True)
                if success:
                    stored_items.append(f"{herb_name}×{count}")
                else:
                    stored_items.append(f"{herb_name}×{count}（储物戒已满，丢失）")
        
        # 更新灵田（收获消耗肥力，每株-8，最低20；杂交领取不额外扣肥力）
        new_fertility = fertility
        if mature_crops:
            new_fertility = max(20, fertility - 8 * len(mature_crops))
        await self.db.conn.execute(
            "UPDATE spirit_farms SET crops = ?, fertility = ? WHERE user_id = ?",
            (json.dumps(remaining_crops), new_fertility, player.user_id)
        )
        await self.db.conn.commit()

        # 构建返回消息
        msg_lines = ["🌾 收获结果", "━━━━━━━━━━━━━━━"]

        if hybrid_details:
            msg_lines.append(f"🧬 杂交完成：{', '.join(f'{name}×1' for name in hybrid_details)}")

        if harvest_details:
            msg_lines.append(f"收获：{', '.join(harvest_details)}（每株产出4份）")
            msg_lines.append(f"获得修为：+{total_exp:,}")
            msg_lines.append(f"获得灵石：+{total_gold:,}")
            if fertility_mult < 1.0:
                msg_lines.append(f"⚠️ 土壤贫瘠（肥力{fertility}/100），修为/灵石收益已打折，请及时 /施肥")
            if season_mult != 1.0:
                pct = int((season_mult - 1) * 100)
                msg_lines.append(f"{season['icon']} 季节【{season['name']}】生效：修为/灵石收益{pct:+d}%")
            if shield_penalized:
                shield_penalty_pct = 10 if getattr(player, "life_skill", "") == "阵法师" else 20
                msg_lines.append(f"⚠️ 护府大阵生效期间，灵田收益降低{shield_penalty_pct}%")

        if stored_items:
            msg_lines.append(f"📦 存入储物戒：")
            for item in stored_items:
                msg_lines.append(f"  {item}")
        
        if withered_crops:
            withered_names = [c["name"] for c in withered_crops]
            msg_lines.append(f"💀 枯萎清除：{', '.join(withered_names)}（共{len(withered_crops)}株）")
        
        remaining_crops_count = sum(1 for c in remaining_crops if c.get("name") != HYBRID_MARKER)
        remaining_jobs = len(collect_hybrid_jobs(remaining_crops))
        msg_lines.append("━━━━━━━━━━━━━━━")
        remaining_text = f"剩余种植：{remaining_crops_count} 株"
        if remaining_jobs:
            remaining_text += f" | 杂交任务：{remaining_jobs} 个"
        msg_lines.append(remaining_text)

        # 只有清理枯萎、没有实际收获时，不应被上层视为“收获成功”
        return bool(mature_crops or completed_jobs), "\n".join(msg_lines)
    
    async def fertilize(self, player: Player) -> Tuple[bool, str]:
        """施肥：消耗灵石恢复土壤肥力"""
        farm = await self.get_user_farm(player.user_id)
        if not farm:
            return False, "❌ 你还没有灵田！"

        fertility = farm.get("fertility", 100)
        if fertility >= 100:
            return False, "✅ 你的灵田土壤肥沃（肥力100/100），无需施肥。"

        cost = 2000
        if player.gold < cost:
            return False, f"❌ 施肥一次需要 {cost:,} 灵石（恢复30点肥力）。"

        player.gold -= cost
        new_fertility = min(100, fertility + 30)
        await self.db.update_player_fields(player.user_id, gold=player.gold)
        await self.db.conn.execute(
            "UPDATE spirit_farms SET fertility = ? WHERE user_id = ?",
            (new_fertility, player.user_id)
        )
        await self.db.conn.commit()

        return True, (
            f"🌱 施肥成功！土壤肥力：{fertility} → {new_fertility}\n"
            f"花费：{cost:,} 灵石\n"
            f"💡 肥力影响修为/灵石收益，收获会持续消耗肥力"
        )

    async def hybridize(self, player: Player, herb_a: str, herb_b: str) -> Tuple[bool, str]:
        """杂交嫁接：消耗父本并占用两个灵田格培育全新品种"""
        farm = await self.get_user_farm(player.user_id)
        if not farm:
            return False, "❌ 你还没有灵田！使用 /开垦灵田"

        farm_level = int(farm.get("level", 1) or 1)
        if not herb_a or not herb_b or herb_a == herb_b:
            return False, (
                "🧬 灵草杂交\n"
                "━━━━━━━━━━━━━━━\n"
                "已知配方：\n"
                f"{format_hybrid_recipes(farm_level)}\n"
                "━━━━━━━━━━━━━━━\n"
                "消耗：配方材料各1株（从储物戒扣除）\n"
                "占用：2个灵田空格，耗时与产物生长时间一致\n"
                "产出：杂交植株×1，完成后 /收获 领取\n"
                "💡 种植杂交植株时消耗1株；非杂交灵草可直接种植\n"
                "💡 用法：/杂交 火焰花 冰心草"
            )

        key = frozenset({herb_a.strip(), herb_b.strip()})
        product = HYBRID_RECIPES.get(key)
        if not product:
            return False, (
                "❌ 未知的花卉组合。\n"
                "已知配方：\n"
                f"{format_hybrid_recipes(farm_level)}"
            )

        need_level = get_herb_unlock_level(product)
        if farm_level < need_level:
            return False, (
                f"🔒 合成【{product}】需要灵田 Lv.{need_level}（当前 Lv.{farm_level}）。\n"
                "💡 使用 /升级灵田 提升灵田等级"
            )

        if not self.storage_ring_manager:
            return False, "❌ 系统异常：储物戒管理器未加载。"

        max_slots = self.get_max_slots(farm)
        crops = farm["crops"]
        used_slots = {int(crop.get("slot") or 0) for crop in crops}
        free_slots = [slot for slot in range(1, max_slots + 1) if slot not in used_slots]
        if len(free_slots) < HYBRID_PLOT_COST:
            return False, (
                f"❌ 杂交需要占用 {HYBRID_PLOT_COST} 个空格（当前可用 {len(free_slots)} 格）。\n"
                "💡 先将其他灵草收获或 /铲除，再重新 /杂交"
            )

        for herb in key:
            if not self.storage_ring_manager.has_item(player, herb, 1):
                return False, f"❌ 储物戒中没有【{herb}】（杂交每种需要1株）"

        for herb in key:
            await self.storage_ring_manager.retrieve_item(player, herb, 1)

        now = int(time.time())
        grow_time = int(SPIRIT_HERBS[product]["grow_time"] * get_season_growth_mult(product))
        mature_time = now + grow_time
        job_id = f"{now}-{random.randint(1000, 9999)}"
        parents = sorted(key)
        for slot in free_slots[:HYBRID_PLOT_COST]:
            crops.append({
                "name": HYBRID_MARKER,
                "job_id": job_id,
                "slot": slot,
                "product": product,
                "parents": parents,
                "plant_time": now,
                "mature_time": mature_time,
            })
        self._normalize_crop_slots(crops, max_slots)
        await self.db.conn.execute(
            "UPDATE spirit_farms SET crops = ? WHERE user_id = ?",
            (json.dumps(crops, ensure_ascii=False), player.user_id)
        )
        await self.db.conn.commit()

        hours = max(1, grow_time // 3600)
        return True, (
            f"🧬 杂交开始！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"配方：{' + '.join(parents)} → {product}\n"
            f"占用格子：格{free_slots[0]}、格{free_slots[1]}（共 {HYBRID_PLOT_COST} 格）\n"
            f"预计产出：{product}×1，耗时约 {hours} 小时\n"
            f"💡 完成后发送 /收获 领取；种植杂交植株时消耗1株"
        )

    async def upgrade_farm(self, player: Player) -> Tuple[bool, str]:
        """升级灵田"""
        farm = await self.get_user_farm(player.user_id)
        if not farm:
            return False, "❌ 你还没有灵田！"
        
        current_level = farm["level"]
        if current_level >= 5:
            return False, "❌ 灵田已达最高等级！"
        
        level_config = FARM_LEVELS.get(current_level, FARM_LEVELS[1])
        cost = level_config["upgrade_cost"]
        
        if player.gold < cost:
            return False, f"❌ 升级需要 {cost:,} 灵石。"
        
        player.gold -= cost
        await self.db.update_player_fields(player.user_id, gold=player.gold)
        
        new_level = current_level + 1
        await self.db.conn.execute(
            "UPDATE spirit_farms SET level = ? WHERE user_id = ?",
            (new_level, player.user_id)
        )
        await self.db.conn.commit()
        
        new_slots = FARM_LEVELS[new_level]["slots"]
        unlocked = [name for name, cfg in SPIRIT_HERBS.items()
                    if int(cfg.get("unlock_level", 1) or 1) == new_level]
        msg_lines = [f"🎉 灵田升级到 Lv.{new_level}！格数增加到 {new_slots}"]
        if unlocked:
            msg_lines.append(f"🌿 新解锁灵草：{'、'.join(unlocked)}")
        next_level, next_herbs = get_next_unlock(new_level)
        if next_level is not None:
            msg_lines.append(f"🔒 下一级 Lv.{next_level} 解锁：{'、'.join(next_herbs)}")
        return True, "\n".join(msg_lines)
    
    async def get_farm_info(self, user_id: str) -> str:
        """获取灵田信息展示"""
        farm = await self.get_user_farm(user_id)
        if not farm:
            return (
                "🌾 灵田系统\n"
                "━━━━━━━━━━━━━━━\n"
                "你还没有灵田！\n"
                "开垦费用：10,000 灵石\n\n"
                "💡 使用 /开垦灵田"
            )
        
        level_config = FARM_LEVELS.get(farm["level"], FARM_LEVELS[1])
        crops = farm["crops"]
        now = int(time.time())
        
        season = get_current_season()
        farm_level = int(farm.get("level", 1) or 1)
        lines = [
            f"🌾 我的灵田 (Lv.{farm['level']})",
            "━━━━━━━━━━━━━━━",
            f"占用格数：{len(crops)}/{level_config['slots']}",
            f"土壤肥力：{farm.get('fertility', 100)}/100" + ("（贫瘠，收益打折！/施肥 恢复）" if farm.get("fertility", 100) < 60 else ""),
            f"{season['icon']} 当前季节：【{season['name']}】{season['desc']}",
            f"🌿 可种植：{'、'.join(get_herbs_for_level(farm_level))}",
        ]
        next_level, next_herbs = get_next_unlock(farm_level)
        if next_level is not None:
            lines.append(f"🔒 Lv.{next_level} 解锁：{'、'.join(next_herbs)}")
        lines.append("")
        
        hybrid_jobs = collect_hybrid_jobs(crops)
        normal_crops = [crop for crop in crops if crop.get("name") != HYBRID_MARKER]
        if hybrid_jobs or normal_crops:
            lines.append("【种植中】")
            for job in hybrid_jobs:
                remaining = max(0, job["mature_time"] - now)
                if remaining > 0:
                    status = f"杂交中，还需 {remaining // 3600}时{(remaining % 3600) // 60}分"
                else:
                    status = "✅ 杂交完成，等待 /收获"
                slots_text = "、".join(f"格{slot}" for slot in job["slots"])
                lines.append(f"  {slots_text}. 杂交→{job['product']} - {status}")
            for crop in normal_crops:
                herb_config = SPIRIT_HERBS.get(crop["name"], SPIRIT_HERBS["灵草"])
                remaining = max(0, crop["mature_time"] - now)
                if remaining > 0:
                    hours = remaining // 3600
                    minutes = (remaining % 3600) // 60
                    status = f"成熟还需 {hours}时{minutes}分"
                else:
                    wither_time = herb_config.get("wither_time", 172800)
                    wither_deadline = crop["mature_time"] + wither_time
                    wither_remaining = wither_deadline - now
                    if wither_remaining <= 0:
                        status = "💀 已枯萎"
                    elif wither_remaining <= 3600:
                        minutes_left = wither_remaining // 60
                        status = f"⚠️ 即将枯萎（{minutes_left}分钟）"
                    else:
                        hours_left = wither_remaining // 3600
                        status = f"✅ 已成熟（{hours_left}小时后枯萎）"
                slot = int(crop.get("slot") or 0)
                lines.append(f"  格{slot}. {crop['name']} - {status}")
        else:
            lines.append("（空）")
        
        lines.append("")
        lines.append("💡 /种植 <灵草名> | /收获 | /铲除 <格号>")
        lines.append("🧬 /杂交 消耗父本各1株并占用2格，完成后产出1株杂交植株")
        lines.append("🌱 非杂交灵草可按灵田等级直接种植；杂交植株种植消耗1株，收获4株")
        
        return "\n".join(lines)
