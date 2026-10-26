# managers/alchemy_manager.py
"""
炼丹系统管理器 - 处理炼丹、配方等逻辑（加入事务防并发）
"""

import json
import random
from typing import Tuple, List, Dict, Optional, TYPE_CHECKING
from ..data.data_manager import DataBase
from ..models import Player
from ..models_extended import UserStatus

if TYPE_CHECKING:
    from ..config_manager import ConfigManager
    from ..core import StorageRingManager


class AlchemyManager:
    """炼丹系统管理器（防并发版）"""
    
    def __init__(self, db: DataBase, config_manager: "ConfigManager" = None, storage_ring_manager: "StorageRingManager" = None):
        self.db = db
        self.config_manager = config_manager
        self._social = None  # 懒加载 SocialManager（仙灵加成）
        self.storage_ring_manager = storage_ring_manager
        self.config = config_manager.alchemy_config if config_manager else {}
        
        raw_recipes = {}
        if config_manager and hasattr(config_manager, 'alchemy_recipes') and config_manager.alchemy_recipes:
            raw_recipes = config_manager.alchemy_recipes
        
        self.recipes = {}
        for recipe in raw_recipes.values():
            if isinstance(recipe, dict) and recipe.get("id"):
                recipe_id = int(recipe["id"])
                self.recipes[recipe_id] = self._normalize_recipe(recipe_id, recipe)
    
    def _normalize_recipe(self, recipe_id: int, recipe: Dict) -> Dict:
        """标准化配方字段，兼容不同格式的配置"""
        name = recipe.get("name", f"丹药{recipe_id}")
        
        desc = recipe.get("desc", None)
        if not desc and self.config_manager:
            pill_config = self._get_pill_config_by_name(name)
            if pill_config:
                desc = self._generate_pill_desc(pill_config)
        if not desc:
            desc = "丹药效果"
        
        return {
            "id": recipe.get("id", recipe_id),
            "name": name,
            "level_required": recipe.get("level_required", recipe.get("level", 0)),
            "materials": recipe.get("materials", recipe.get("cost", {})),
            "success_rate": recipe.get("success_rate", recipe.get("success", 50)),
            "desc": desc
        }
    
    def _generate_pill_desc(self, pill_config: Dict) -> str:
        """根据丹药配置生成描述"""
        rank = pill_config.get("rank", "")
        
        if pill_config.get("exp_gain"):
            return f"增加{pill_config['exp_gain']}修为（{rank}修为丹）"
        
        if pill_config.get("breakthrough_bonus"):
            bonus = int(pill_config["breakthrough_bonus"] * 100)
            return f"提升{bonus}%突破成功率（{rank}破境丹）"
        
        if pill_config.get("description"):
            return pill_config["description"]
        
        return f"{rank}丹药"
    
    def _get_pill_config_by_name(self, name: str) -> Optional[Dict]:
        """根据丹药名称获取配置"""
        if not self.config_manager:
            return None
        if hasattr(self.config_manager, 'exp_pills_data') and name in self.config_manager.exp_pills_data:
            return self.config_manager.exp_pills_data[name]
        if hasattr(self.config_manager, 'utility_pills_data') and name in self.config_manager.utility_pills_data:
            return self.config_manager.utility_pills_data[name]
        if hasattr(self.config_manager, 'pills_data') and name in self.config_manager.pills_data:
            return self.config_manager.pills_data[name]
        return None
    
    def _get_bonus_data(self, player: Player) -> dict:
        """安全读取玩家永久加成数据"""
        try:
            data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get_furnace_level(self, player: Player) -> int:
        """丹炉等级（0-5），每级炼丹成功率 +3%"""
        return int(self._get_bonus_data(player).get("alchemy_furnace", 0))

    def get_furnace_bonus_rate(self, player: Player) -> int:
        """丹炉带来的成功率加成（百分点）"""
        return self.get_furnace_level(player) * 3

    async def upgrade_furnace(self, player: Player) -> Tuple[bool, str]:
        """升级丹炉（消耗灵石，等级越高越贵）"""
        level = self.get_furnace_level(player)
        if level >= 5:
            return False, "❌ 你的丹炉已达最高等级【五品玄天炉】。"

        cost = 20000 * (level + 1)
        if player.gold < cost:
            return False, f"❌ 灵石不足！升级丹炉到 {level + 1} 级需要 {cost:,} 灵石。"

        player.gold -= cost
        data = self._get_bonus_data(player)
        data["alchemy_furnace"] = level + 1
        player.permanent_bonus = json.dumps(data, ensure_ascii=False)
        await self.db.update_player(player)

        furnace_names = ["凡铁炉", "青石炉", "紫金炉", "灵纹炉", "八卦炉", "五品玄天炉"]
        return True, (
            f"🔥 丹炉升级成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{furnace_names[level]} → 【{furnace_names[level + 1]}】\n"
            f"炼丹成功率加成：+{level * 3}% → +{(level + 1) * 3}%\n"
            f"花费：{cost:,} 灵石"
        )

    # ===== 火候小游戏（v6.16）：投料后 3 分钟内选火候，影响成功率与极品概率 =====
    FIRE_STATE_PREFIX = "firecontrol_"
    FIRE_OPTIONS = {
        "1": {"label": "猛火急攻", "rate_bonus": -8, "perfect_bonus": 8,
              "desc": "孤注一掷！成功率-8%，但极品概率大增"},
        "2": {"label": "文火慢煨", "rate_bonus": 6, "perfect_bonus": -6,
              "desc": "稳中求进！成功率+6%，难出极品"},
        "3": {"label": "控火守中", "rate_bonus": 0, "perfect_bonus": 0,
              "desc": "不偏不倚，一切如常"},
    }

    async def start_fire_control(self, user_id: str, pill_id: int, recipe_name: str) -> Tuple[bool, str]:
        """投料后进入火候选择（3分钟内 /火候 <1-3>）"""
        import json as _json
        state = {
            "pill_id": pill_id,
            "recipe": recipe_name,
            "expire": int(time.time()) + 180,
        }
        await self.db.ext.set_system_config(
            self.FIRE_STATE_PREFIX + user_id, _json.dumps(state, ensure_ascii=False)
        )
        opts = "\n".join(
            f"  [{k}] {v['label']} —— {v['desc']}" for k, v in self.FIRE_OPTIONS.items()
        )
        return True, (
            f"🔥 药材入炉，丹火升腾！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"正在炼制【{recipe_name}】，请选择火候：\n"
            f"{opts}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💡 3分钟内发送 /火候 <编号> 掌控丹炉（超时按控火守中处理）"
        )

    async def pop_fire_state(self, user_id: str) -> Tuple[Optional[int], Optional[str]]:
        """取出火候状态（一次性），返回 (pill_id, choice)。无状态返回 (None, None)"""
        import json as _json
        raw = await self.db.ext.get_system_config(self.FIRE_STATE_PREFIX + user_id)
        if not raw:
            return None, None
        await self.db.ext.set_system_config(self.FIRE_STATE_PREFIX + user_id, "")  # 一次性消费
        try:
            state = _json.loads(raw)
        except Exception:
            return None, None
        if int(time.time()) > int(state.get("expire", 0)):
            return state.get("pill_id"), "3"  # 超时视为守中
        return state.get("pill_id"), state.get("choice", "3")

    async def set_fire_choice(self, user_id: str, choice: str) -> Tuple[bool, str]:
        """玩家选择火候（写回状态）"""
        import json as _json
        raw = await self.db.ext.get_system_config(self.FIRE_STATE_PREFIX + user_id)
        if not raw:
            return False, "❌ 你当前没有正在炼制的丹药。先发送 /炼丹 <ID>。"
        try:
            state = _json.loads(raw)
        except Exception:
            return False, "❌ 火候数据异常，请重新 /炼丹。"
        if choice not in self.FIRE_OPTIONS:
            return False, "❌ 请输入 1-3 的编号选择火候。"
        if int(time.time()) > int(state.get("expire", 0)):
            await self.db.ext.set_system_config(self.FIRE_STATE_PREFIX + user_id, "")
            return False, "⌛ 炉火已熄（超时），请重新 /炼丹。"
        state["choice"] = choice
        await self.db.ext.set_system_config(
            self.FIRE_STATE_PREFIX + user_id, _json.dumps(state, ensure_ascii=False)
        )
        opt = self.FIRE_OPTIONS[choice]
        return True, f"🔥 你以「{opt['label']}」掌控丹炉！（{opt['desc']}）\n💡 现在发送 /炼丹 <同一ID> 开炉成丹！"

    async def craft_pill(self, user_id: str, pill_id: int) -> Tuple[bool, str, Optional[Dict]]:
        """炼制丹药（加入事务防并发；含火候加成与极品判定）"""
        # 取出火候选择（3分钟内 /火候 选的档位）
        fire_pill_id, fire_choice = await self.pop_fire_state(user_id)
        fire_cfg = self.FIRE_OPTIONS.get(fire_choice or "3", self.FIRE_OPTIONS["3"])
        fire_matched = (fire_pill_id is not None and int(fire_pill_id) == int(pill_id))
        if not fire_matched:
            fire_cfg = self.FIRE_OPTIONS["3"]  # 未选火候或不匹配，按守中处理

        await self.db.conn.execute("BEGIN IMMEDIATE")
        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 事务内重新获取玩家对象，确保数据是最新的
            player = await self.db.get_player_by_id(user_id)
            if not player:
                await self.db.conn.rollback()
                return False, "❌ 你还未踏入修仙之路！", None
            
            if pill_id not in self.recipes:
                await self.db.conn.rollback()
                return False, "❌ 无效的丹药ID！", None
            
            recipe = self.recipes[pill_id]
            
            if player.level_index < recipe["level_required"]:
                await self.db.conn.rollback()
                return False, f"❌ 炼制{recipe['name']}需要达到境界等级 {recipe['level_required']}！", None
            
            # 检查所有材料
            materials = recipe["materials"]
            missing_materials = []
            
            required_gold = materials.get("灵石", 0)
            if player.gold < required_gold:
                missing_materials.append(f"灵石（需要{required_gold}，拥有{player.gold}）")
            
            if self.storage_ring_manager:
                for material_name, required_count in materials.items():
                    if material_name == "灵石":
                        continue
                    current_count = self.storage_ring_manager.get_item_count(player, material_name)
                    if current_count < required_count:
                        missing_materials.append(f"{material_name}（需要{required_count}，拥有{current_count}）")
            
            if missing_materials:
                await self.db.conn.rollback()
                return False, f"❌ 材料不足！\n" + "\n".join(f"  · {m}" for m in missing_materials), None
            
            # 扣除材料（灵石和储物戒物品）
            player.gold -= required_gold
            consumed_materials = []
            
            if self.storage_ring_manager:
                items = player.get_storage_ring_items()
                for material_name, required_count in materials.items():
                    if material_name == "灵石":
                        continue
                    if material_name in items and items[material_name] >= required_count:
                        if items[material_name] == required_count:
                            del items[material_name]
                        else:
                            items[material_name] -= required_count
                        consumed_materials.append(f"{material_name}×{required_count}")
                player.set_storage_ring_items(items)
            
            # 判定成功率（丹炉等级每级+3%；宗门炼丹房每级+2%）
            success_rate = recipe["success_rate"]
            level_bonus = (player.level_index - recipe["level_required"]) * 2
            furnace_bonus = self.get_furnace_bonus_rate(player)
            sect_bonus = 0
            try:
                if player.sect_id:
                    sect = await self.db.ext.get_sect_by_id(player.sect_id)
                    if sect:
                        sect_bonus = int(json.loads(getattr(sect, "buildings", "{}") or "{}").get("炼丹房", 0)) * 2
            except Exception:
                sect_bonus = 0
            # 仙灵道侣加成（青璃：炼丹+5%起）
            comp_bonus_pct = 0
            try:
                if self._social is None:
                    from .social_manager import SocialManager
                    self._social = SocialManager(self.db)
                comp_bonus_pct = int(await self._social.companion_bonus_async(user_id, "alchemy") * 100)
            except Exception:
                comp_bonus_pct = 0
            final_success_rate = min(95, max(5, success_rate + level_bonus + furnace_bonus + sect_bonus + comp_bonus_pct + fire_cfg["rate_bonus"]))
            # 极品判定：火候影响极品概率，极品丹效果x1.5
            perfect = (random.randint(1, 100) <= max(2, 10 + fire_cfg["perfect_bonus"])) if is_success else False
            
            is_success = random.randint(1, 100) <= final_success_rate
            
            # 构建消耗描述
            cost_lines = []
            if required_gold > 0:
                cost_lines.append(f"灵石 -{required_gold}")
            cost_lines.extend(consumed_materials)
            cost_str = "、".join(cost_lines) if cost_lines else "无"
            
            if is_success:
                # ===== 丹劫机制：高阶丹药出炉引来雷劫，需消耗引雷符抵消 =====
                tribulation_msg = ""
                pill_ruined = False
                if recipe["level_required"] >= 6 and random.randint(1, 100) <= 25:
                    # 事务内直接操作储物戒 JSON（retrieve_item 内部会开新事务，不能在此调用）
                    ring_items = player.get_storage_ring_items()
                    if self.storage_ring_manager and ring_items.get("引雷符", 0) >= 1:
                        if ring_items["引雷符"] <= 1:
                            del ring_items["引雷符"]
                        else:
                            ring_items["引雷符"] -= 1
                        player.set_storage_ring_items(ring_items)
                        tribulation_msg = ("\n⚡ 天降丹劫！你祭出【引雷符】引雷入体，"
                                           "雷光淬丹，丹药品质得以保全！（消耗引雷符x1）")
                    else:
                        pill_ruined = True
                        tribulation_msg = ("\n💥 天降丹劫！你没有【引雷符】护身，"
                                           "雷火焚炉，丹药在最后一刻化为灰烬...")
                # ============================================================

                if not pill_ruined:
                    # 炼制成功 - 存入丹药背包（极品丹产出双份，等效强化收益）
                    pill_name = recipe["name"]
                    inventory = player.get_pills_inventory()
                    inventory[pill_name] = inventory.get(pill_name, 0) + (2 if perfect else 1)
                    player.set_pills_inventory(inventory)
                    if perfect:
                        pill_name = recipe["name"] + "（极品）"
                else:
                    pill_name = recipe["name"]

                # 这里非常重要：commit=False，由外层事务统一提交
                await self.db.update_player(player, commit=False)

                if pill_ruined:
                    msg = f"""
💔 丹劫毁丹
━━━━━━━━━━━━━━━

炼制【{recipe['name']}】本已成功，却引来天雷降劫...

消耗：{cost_str}
成功率：{final_success_rate}%
{tribulation_msg}

💡 高阶丹药请随身携带【引雷符】（百宝阁有售）
                    """.strip()

                    result_data = {
                        "success": False,
                        "pill_name": recipe["name"],
                        "cost": required_gold,
                        "materials_consumed": consumed_materials,
                        "tribulation": True
                    }
                    await self.db.conn.commit()
                    return True, msg, result_data

                perfect_note = ""
                if perfect:
                    perfect_note = "\n🌟 天时地利！此丹竟是【极品】！额外多得一枚同款丹药"
                msg = f"""
🎉 炼丹成功！
━━━━━━━━━━━━━━━

你成功炼制了【{pill_name}】！{perfect_note}
丹药已存入丹药背包

消耗：{cost_str}
成功率：{final_success_rate}%
火候：{fire_cfg['label']}
{tribulation_msg}

💡 使用 /服用丹药 {pill_name} [数量] 可服用此丹药
💡 使用 /丹药背包 查看所有丹药
                """.strip()

                result_data = {
                    "success": True,
                    "pill_name": pill_name,
                    "cost": required_gold,
                    "materials_consumed": consumed_materials,
                    "perfect": perfect
                }
            else:
                # 炼制失败 - 扣除材料
                await self.db.update_player(player, commit=False)
                
                msg = f"""
💔 炼丹失败
━━━━━━━━━━━━━━━

炼制【{recipe['name']}】失败了...

材料已消耗
消耗：{cost_str}
成功率：{final_success_rate}%

再接再厉！
                """.strip()
                
                result_data = {
                    "success": False,
                    "pill_name": recipe["name"],
                    "cost": required_gold,
                    "materials_consumed": consumed_materials
                }
            
            await self.db.conn.commit()
            return True, msg, result_data
            
        except Exception as e:
            await self.db.conn.rollback()
            raise