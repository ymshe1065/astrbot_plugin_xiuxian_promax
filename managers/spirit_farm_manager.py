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
SPIRIT_HERBS = {
    "灵草": {"grow_time": 3600, "exp_yield": 500, "gold_yield": 100, "wither_time": 172800},
    "血灵草": {"grow_time": 7200, "exp_yield": 1500, "gold_yield": 300, "wither_time": 172800},
    "冰心草": {"grow_time": 14400, "exp_yield": 4000, "gold_yield": 800, "wither_time": 172800},
    "火焰花": {"grow_time": 28800, "exp_yield": 10000, "gold_yield": 2000, "wither_time": 172800},
    "九叶灵芝": {"grow_time": 86400, "exp_yield": 30000, "gold_yield": 6000, "wither_time": 172800},
    # 杂交灵草（仅可通过 /杂交 获得）
    "冰火双生花": {"grow_time": 43200, "exp_yield": 25000, "gold_yield": 8000, "wither_time": 172800},
}

# 杂交配方：材料 -> 产物
HYBRID_RECIPES = {
    frozenset({"火焰花", "冰心草"}): "冰火双生花",
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
    
    def __init__(self, db: DataBase, storage_ring_manager: "StorageRingManager" = None):
        self.db = db
        self.storage_ring_manager = storage_ring_manager
        self.last_harvest_count = 0
        self.last_harvest_counts = {}
    
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
        await self.db.update_player(player)
        
        await self.db.conn.execute(
            """
            INSERT INTO spirit_farms (user_id, level, crops)
            VALUES (?, 1, '[]')
            """,
            (player.user_id,)
        )
        await self.db.conn.commit()
        
        return True, (
            "🌱 灵田开垦成功！\n"
            "━━━━━━━━━━━━━━━\n"
            "灵田等级：Lv.1\n"
            "种植格数：3\n"
            "━━━━━━━━━━━━━━━\n"
            "可种植：灵草、血灵草、冰心草..."
        )
    
    async def plant_herb(self, player: Player, herb_name: str, count: int = 1) -> Tuple[bool, str]:
        """种植灵草"""
        if herb_name not in SPIRIT_HERBS:
            herbs_list = "、".join(SPIRIT_HERBS.keys())
            return False, f"❌ 未知的灵草。可种植：{herbs_list}"
        
        farm = await self.get_user_farm(player.user_id)
        if not farm:
            return False, "❌ 你还没有灵田！使用 /开垦灵田"
        
        level_config = FARM_LEVELS.get(farm["level"], FARM_LEVELS[1])
        max_slots = level_config["slots"]
        crops = farm["crops"]
        
        available_slots = max_slots - len(crops)
        if available_slots <= 0:
            return False, f"❌ 灵田已满！最多种植 {max_slots} 株。"
        
        # 调整种植数量
        plant_count = min(count, available_slots)
        if plant_count <= 0:
            return False, "❌ 种植数量必须大于0！"
        
        # 批量种植（成熟时间受季节影响）
        herb_config = SPIRIT_HERBS[herb_name]
        plant_time = int(time.time())
        grow_time = int(herb_config["grow_time"] * get_season_growth_mult(herb_name))
        mature_time = plant_time + grow_time

        for _ in range(plant_count):
            crops.append({
                "name": herb_name,
                "plant_time": plant_time,
                "mature_time": mature_time
            })

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
    
    async def harvest(self, player: Player) -> Tuple[bool, str]:
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
        
        for crop in crops:
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
        
        if not mature_crops and not withered_crops:
            return False, "❌ 没有成熟的灵草可以收获。"

        self.last_harvest_count = len(mature_crops)
        self.last_harvest_counts[player.user_id] = len(mature_crops)

        # 计算奖励（只有成熟未枯萎的才有收益；产量受土壤肥力与季节影响）
        fertility = farm.get("fertility", 100)
        fertility_mult = fertility / 100.0
        season = get_current_season()
        season_mult = get_season_yield_mult()
        total_exp = 0
        total_gold = 0
        harvest_details = []
        herb_counts = {}
        shield_penalized = False

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
            herb_counts[herb_name] = herb_counts.get(herb_name, 0) + output_mult

        herb_counts = {name: max(1, int(round(count))) for name, count in herb_counts.items()}
        
        # 应用奖励
        if total_exp > 0 or total_gold > 0:
            player.experience += total_exp
            player.gold += total_gold
            await self.db.update_player(player)
        
        # 将灵草存入储物戒
        stored_items = []
        if self.storage_ring_manager:
            for herb_name, count in herb_counts.items():
                success, _ = await self.storage_ring_manager.store_item(player, herb_name, count, silent=True)
                if success:
                    stored_items.append(f"{herb_name}×{count}")
                else:
                    stored_items.append(f"{herb_name}×{count}（储物戒已满，丢失）")
        
        # 更新灵田（收获消耗肥力，每株-8，最低20）
        new_fertility = max(20, fertility - 8 * max(1, len(mature_crops)))
        await self.db.conn.execute(
            "UPDATE spirit_farms SET crops = ?, fertility = ? WHERE user_id = ?",
            (json.dumps(remaining_crops), new_fertility, player.user_id)
        )
        await self.db.conn.commit()

        # 构建返回消息
        msg_lines = ["🌾 收获结果", "━━━━━━━━━━━━━━━"]

        if harvest_details:
            msg_lines.append(f"收获：{', '.join(harvest_details)}")
            msg_lines.append(f"获得修为：+{total_exp:,}")
            msg_lines.append(f"获得灵石：+{total_gold:,}")
            if fertility_mult < 1.0:
                msg_lines.append(f"⚠️ 土壤贫瘠（肥力{fertility}/100），产量已打折，请及时 /施肥")
            if season_mult != 1.0:
                pct = int((season_mult - 1) * 100)
                msg_lines.append(f"{season['icon']} 季节【{season['name']}】生效：产量{pct:+d}%")
            if shield_penalized:
                shield_penalty_pct = 10 if getattr(player, "life_skill", "") == "阵法师" else 20
                msg_lines.append(f"⚠️ 护府大阵生效期间，灵田产出降低{shield_penalty_pct}%")
            if stored_items:
                msg_lines.append(f"📦 存入储物戒：")
                for item in stored_items:
                    msg_lines.append(f"  {item}")
        
        if withered_crops:
            withered_names = [c["name"] for c in withered_crops]
            msg_lines.append(f"💀 枯萎清除：{', '.join(withered_names)}（共{len(withered_crops)}株）")
        
        msg_lines.append("━━━━━━━━━━━━━━━")
        msg_lines.append(f"剩余种植：{len(remaining_crops)} 株")

        # 只有清理枯萎、没有实际收获时，不应被上层视为“收获成功”
        return bool(mature_crops), "\n".join(msg_lines)
    
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
        await self.db.update_player(player)
        await self.db.conn.execute(
            "UPDATE spirit_farms SET fertility = ? WHERE user_id = ?",
            (new_fertility, player.user_id)
        )
        await self.db.conn.commit()

        return True, (
            f"🌱 施肥成功！土壤肥力：{fertility} → {new_fertility}\n"
            f"花费：{cost:,} 灵石\n"
            f"💡 肥力影响收获产量，收获会持续消耗肥力"
        )

    async def hybridize(self, player: Player, herb_a: str, herb_b: str) -> Tuple[bool, str]:
        """杂交嫁接：两种灵草有概率合成全新品种"""
        farm = await self.get_user_farm(player.user_id)
        if not farm:
            return False, "❌ 你还没有灵田！使用 /开垦灵田"

        if not herb_a or not herb_b or herb_a == herb_b:
            return False, (
                "🧬 灵草杂交\n"
                "━━━━━━━━━━━━━━━\n"
                "已知配方：\n"
                "  · 火焰花 + 冰心草 → 冰火双生花\n"
                "━━━━━━━━━━━━━━━\n"
                "消耗：各1株（从储物戒扣除）\n"
                "成功率：50%，失败则材料损失一半修为返还\n"
                "💡 用法：/杂交 火焰花 冰心草"
            )

        key = frozenset({herb_a.strip(), herb_b.strip()})
        product = HYBRID_RECIPES.get(key)
        if not product:
            return False, "❌ 未知的花卉组合，试试：火焰花 + 冰心草"

        if not self.storage_ring_manager:
            return False, "❌ 系统异常：储物戒管理器未加载。"

        for herb in key:
            if not self.storage_ring_manager.has_item(player, herb, 1):
                return False, f"❌ 储物戒中没有【{herb}】（杂交每种需要1株）"

        for herb in key:
            await self.storage_ring_manager.retrieve_item(player, herb, 1)

        if random.random() < 0.5:
            success, _ = await self.storage_ring_manager.store_item(player, product, 1, silent=True)
            if success:
                return True, (
                    f"🧬 杂交成功！\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"【{list(key)[0]}】+【{list(key)[1]}】融合为一株【{product}】！\n"
                    f"（已存入储物戒，可使用 /种植 {product} 种下）"
                )
            compensation = 3000
            player.gold += compensation
            await self.db.update_player(player)
            return False, (
                f"⚠️ 杂交成功，但储物戒已满，【{product}】未能保存。\n"
                f"获得补偿：{compensation:,} 灵石\n"
                f"💡 请清理储物戒后再试。"
            )
        else:
            compensation = 3000
            player.gold += compensation
            await self.db.update_player(player)
            return False, (
                f"💔 杂交失败...两株灵草灵气相斥枯萎了。\n"
                f"获得安慰补偿：{compensation:,} 灵石\n"
                f"再接再厉！"
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
        await self.db.update_player(player)
        
        new_level = current_level + 1
        await self.db.conn.execute(
            "UPDATE spirit_farms SET level = ? WHERE user_id = ?",
            (new_level, player.user_id)
        )
        await self.db.conn.commit()
        
        new_slots = FARM_LEVELS[new_level]["slots"]
        return True, f"🎉 灵田升级到 Lv.{new_level}！格数增加到 {new_slots}"
    
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
        lines = [
            f"🌾 我的灵田 (Lv.{farm['level']})",
            "━━━━━━━━━━━━━━━",
            f"种植格数：{len(crops)}/{level_config['slots']}",
            f"土壤肥力：{farm.get('fertility', 100)}/100" + ("（贫瘠，产量打折！/施肥 恢复）" if farm.get("fertility", 100) < 60 else ""),
            f"{season['icon']} 当前季节：【{season['name']}】{season['desc']}",
            ""
        ]
        
        if crops:
            lines.append("【种植中】")
            for i, crop in enumerate(crops, 1):
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
                lines.append(f"  {i}. {crop['name']} - {status}")
        else:
            lines.append("（空）")
        
        lines.append("")
        lines.append("💡 /种植 <灵草名> | /收获")
        
        return "\n".join(lines)
