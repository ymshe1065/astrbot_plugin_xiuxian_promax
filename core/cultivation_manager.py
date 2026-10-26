# core/cultivation_manager.py
import random
import json
from typing import Dict, Optional

from astrbot.api import AstrBotConfig, logger
from ..config_manager import ConfigManager
from ..models import Player

class CultivationManager:
    """修炼管理器，包含角色生成和闭关修炼功能"""

    def __init__(self, config: AstrBotConfig, config_manager: ConfigManager):
        self.config = config
        self.config_manager = config_manager

        # 灵根名称到配置项键的映射
        self.root_to_config_key = {
            "伪": "PSEUDO_ROOT_SPEED",
            "金木水火": "QUAD_ROOT_SPEED", "金木水土": "QUAD_ROOT_SPEED", "金木火土": "QUAD_ROOT_SPEED", "金水火土": "QUAD_ROOT_SPEED", "木水火土": "QUAD_ROOT_SPEED",
            "金木水": "TRI_ROOT_SPEED", "金木火": "TRI_ROOT_SPEED", "金木土": "TRI_ROOT_SPEED", "金水火": "TRI_ROOT_SPEED", "金水土": "TRI_ROOT_SPEED", "金火土": "TRI_ROOT_SPEED", "木水火": "TRI_ROOT_SPEED", "木水土": "TRI_ROOT_SPEED", "木火土": "TRI_ROOT_SPEED", "水火土": "TRI_ROOT_SPEED",
            "金木": "DUAL_ROOT_SPEED", "金水": "DUAL_ROOT_SPEED", "金火": "DUAL_ROOT_SPEED", "金土": "DUAL_ROOT_SPEED", "木水": "DUAL_ROOT_SPEED", "木火": "DUAL_ROOT_SPEED", "木土": "DUAL_ROOT_SPEED", "水火": "DUAL_ROOT_SPEED", "水土": "DUAL_ROOT_SPEED", "火土": "DUAL_ROOT_SPEED",
            "金": "WUXING_ROOT_SPEED", "木": "WUXING_ROOT_SPEED", "水": "WUXING_ROOT_SPEED", "火": "WUXING_ROOT_SPEED", "土": "WUXING_ROOT_SPEED",
            "雷": "THUNDER_ROOT_SPEED", "冰": "ICE_ROOT_SPEED", "风": "WIND_ROOT_SPEED", "暗": "DARK_ROOT_SPEED", "光": "LIGHT_ROOT_SPEED",
            "天金": "HEAVENLY_ROOT_SPEED", "天木": "HEAVENLY_ROOT_SPEED", "天水": "HEAVENLY_ROOT_SPEED", "天火": "HEAVENLY_ROOT_SPEED", "天土": "HEAVENLY_ROOT_SPEED", "天雷": "HEAVENLY_ROOT_SPEED",
            "阴阳": "YIN_YANG_ROOT_SPEED", "融合": "FUSION_ROOT_SPEED",
            "混沌": "CHAOS_ROOT_SPEED",
            "先天道体": "INNATE_BODY_SPEED", "神圣体质": "DIVINE_BODY_SPEED"
        }

        # 灵根池定义（按权重类别）
        self.root_pools = {
            "PSEUDO": ["伪"],
            "QUAD": ["金木水火", "金木水土", "金木火土", "金水火土", "木水火土"],
            "TRI": ["金木水", "金木火", "金木土", "金水火", "金水土", "金火土", "木水火", "木水土", "木火土", "水火土"],
            "DUAL": ["金木", "金水", "金火", "金土", "木水", "木火", "木土", "水火", "水土", "火土"],
            "WUXING": ["金", "木", "水", "火", "土"],
            "VARIANT": ["雷", "冰", "风", "暗", "光"],
            "HEAVENLY": ["天金", "天木", "天水", "天火", "天土", "天雷"],
            "LEGENDARY": ["阴阳", "融合"],
            "MYTHIC": ["混沌"],
            "DIVINE_BODY": ["先天道体", "神圣体质"]
        }

    def _calculate_base_stats(self, level_index: int, cultivation_type: str = "灵修") -> Dict[str, int]:
        level_data = self.config_manager.get_level_data(cultivation_type)
        if 0 <= level_index < len(level_data):
            level_config = level_data[level_index]
            return {
                "lifespan": level_config.get("base_lifespan", 100 + level_index * 50),
                "max_spiritual_qi": level_config.get("base_max_spiritual_qi", 50 + level_index * 20),
                "max_blood_qi": level_config.get("base_max_blood_qi", 50 + level_index * 20),
                "mental_power": level_config.get("base_mental_power", 50 + level_index * 20),
                "physical_damage": level_config.get("base_physical_damage", 10 + level_index * 8),
                "magic_damage": level_config.get("base_magic_damage", 10 + level_index * 8),
                "physical_defense": level_config.get("base_physical_defense", 5 + level_index * 4),
                "magic_defense": level_config.get("base_magic_defense", 5 + level_index * 4)
            }
        else:
            return {
                "lifespan": 100 + level_index * 50, "max_spiritual_qi": 50 + level_index * 20, "max_blood_qi": 50 + level_index * 20,
                "mental_power": 50 + level_index * 20, "physical_damage": 10 + level_index * 8, "magic_damage": 10 + level_index * 8,
                "physical_defense": 5 + level_index * 4, "magic_defense": 5 + level_index * 4
            }

    def _get_random_spiritual_root(self) -> str:
        weights_config = self.config.get("SPIRIT_ROOT_WEIGHTS", {})
        weight_pool = []
        
        def add_pool(weight_key, pool_key, default_weight):
            weight = int(weights_config.get(weight_key, default_weight))
            if weight > 0:
                weight_pool.extend([(pool_key, root) for root in self.root_pools[pool_key]] * weight)

        add_pool("PSEUDO_ROOT_WEIGHT", "PSEUDO", 1)
        add_pool("QUAD_ROOT_WEIGHT", "QUAD", 10)
        add_pool("TRI_ROOT_WEIGHT", "TRI", 30)
        add_pool("DUAL_ROOT_WEIGHT", "DUAL", 100)
        add_pool("WUXING_ROOT_WEIGHT", "WUXING", 200)
        add_pool("VARIANT_ROOT_WEIGHT", "VARIANT", 20)
        add_pool("HEAVENLY_ROOT_WEIGHT", "HEAVENLY", 5)
        add_pool("LEGENDARY_ROOT_WEIGHT", "LEGENDARY", 2)
        add_pool("MYTHIC_ROOT_WEIGHT", "MYTHIC", 1)
        add_pool("DIVINE_BODY_WEIGHT", "DIVINE_BODY", 1)

        if not weight_pool:
            logger.warning("灵根权重池为空，使用默认金灵根")
            return "金"

        _, selected_root = random.choice(weight_pool)
        return selected_root

    def _get_root_description(self, root_name: str) -> str:
        descriptions = {
            "伪": "【废柴】资质低劣，修炼如龟速",
            "金木水火": "【凡品】四灵根杂乱，资质平庸", "金木水土": "【凡品】四灵根杂乱，资质平庸", "金木火土": "【凡品】四灵根杂乱，资质平庸", "金水火土": "【凡品】四灵根杂乱，资质平庸", "木水火土": "【凡品】四灵根杂乱，资质平庸",
            "金木水": "【凡品】三灵根较杂，资质一般", "金木火": "【凡品】三灵根较杂，资质一般", "金木土": "【凡品】三灵根较杂，资质一般", "金水火": "【凡品】三灵根较杂，资质一般", "金水土": "【凡品】三灵根较杂，资质一般", "金火土": "【凡品】三灵根较杂，资质一般", "木水火": "【凡品】三灵根较杂，资质一般", "木水土": "【凡品】三灵根较杂，资质一般", "木火土": "【凡品】三灵根较杂，资质一般", "水火土": "【凡品】三灵根较杂，资质一般",
            "金木": "【良品】双灵根，较为常见", "金水": "【良品】双灵根，较为常见", "金火": "【良品】双灵根，较为常见", "金土": "【良品】双灵根，较为常见", "木水": "【良品】双灵根，较为常见", "木火": "【良品】双灵根，较为常见", "木土": "【良品】双灵根，较为常见", "水火": "【良品】双灵根，较为常见", "水土": "【良品】双灵根，较为常见", "火土": "【良品】双灵根，较为常见",
            "金": "【上品】金之精华，锋锐无双", "木": "【上品】木之生机，生生不息", "水": "【上品】水之灵韵，柔中带刚", "火": "【上品】火之烈焰，霸道无匹", "土": "【上品】土之厚重，稳如磐石",
            "雷": "【稀有】天地雷霆，毁灭之力", "冰": "【稀有】极寒冰封，万物凝固", "风": "【稀有】疾风骤雨，来去无踪", "暗": "【稀有】幽暗深邃，诡异莫测", "光": "【稀有】神圣光明，普照万物",
            "天金": "【极品】天选之子，金之极致", "天木": "【极品】天选之子，木之极致", "天水": "【极品】天选之子，水之极致", "天火": "【极品】天选之子，火之极致", "天土": "【极品】天选之子，土之极致", "天雷": "【极品】天选之子，雷之极致",
            "阴阳": "【传说】阴阳调和，造化玄机", "融合": "【传说】五行融合，万法归一",
            "混沌": "【神话】混沌初开，包罗万象",
            "先天道体": "【禁忌】天生道体，与天地同寿", "神圣体质": "【禁忌】神之后裔，天赋异禀"
        }
        return descriptions.get(root_name, "【未知】神秘的灵根")

    def generate_new_player_stats(self, user_id: str, cultivation_type: str = "灵修") -> Player:
        root = self._get_random_spiritual_root()
        initial_gold = self.config.get("VALUES", {}).get("INITIAL_GOLD", 1000)

        if cultivation_type == "体修":
            initial_blood_qi = random.randint(100, 500)
            return Player(
                user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="体修",
                lifespan=random.randint(50, 100), experience=0, gold=initial_gold,
                spiritual_qi=0, max_spiritual_qi=0, blood_qi=initial_blood_qi, max_blood_qi=initial_blood_qi,
                magic_damage=0, physical_damage=random.randint(100, 500),
                magic_defense=random.randint(50, 200), physical_defense=random.randint(100, 500),
                mental_power=random.randint(100, 500)
            )
        elif cultivation_type == "魔修":
            return Player(user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="魔修", lifespan=80, experience=0, gold=initial_gold, spiritual_qi=500, max_spiritual_qi=500, blood_qi=0, max_blood_qi=0, magic_damage=random.randint(150, 300), physical_damage=random.randint(150, 300), magic_defense=0, physical_defense=0, mental_power=200)
        elif cultivation_type == "剑修":
            return Player(user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="剑修", lifespan=100, experience=0, gold=initial_gold, spiritual_qi=100, max_spiritual_qi=100, blood_qi=0, max_blood_qi=0, magic_damage=0, physical_damage=random.randint(300, 500), magic_defense=0, physical_defense=0, mental_power=150)
        elif cultivation_type == "佛修":
            return Player(user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="佛修", lifespan=150, experience=0, gold=initial_gold, spiritual_qi=2000, max_spiritual_qi=2000, blood_qi=0, max_blood_qi=0, magic_damage=10, physical_damage=10, magic_defense=200, physical_defense=300, mental_power=500)
        elif cultivation_type == "鬼修":
            return Player(user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="鬼修", lifespan=80, experience=0, gold=initial_gold, spiritual_qi=800, max_spiritual_qi=800, blood_qi=0, max_blood_qi=0, magic_damage=200, physical_damage=50, magic_defense=20, physical_defense=20, mental_power=800)
        elif cultivation_type == "御兽":
            return Player(user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="御兽", lifespan=100, experience=0, gold=initial_gold, spiritual_qi=600, max_spiritual_qi=600, blood_qi=0, max_blood_qi=0, magic_damage=100, physical_damage=100, magic_defense=50, physical_defense=50, mental_power=300)
        else: # 默认灵修
            initial_max_qi = random.randint(100, 1000)
            return Player(
                user_id=user_id, spiritual_root=f"{root}灵根", cultivation_type="灵修",
                lifespan=100, experience=0, gold=initial_gold,
                spiritual_qi=random.randint(0, initial_max_qi), max_spiritual_qi=initial_max_qi,
                blood_qi=0, max_blood_qi=0, magic_damage=random.randint(100, 200), physical_damage=5,
                magic_defense=10, physical_defense=5, mental_power=random.randint(100, 500)
            )

    def get_spiritual_root_speed(self, player: Player) -> float:
        root_name = player.spiritual_root.replace("灵根", "")
        config_key = self.root_to_config_key.get(root_name)
        if not config_key:
            return 1.0
        speeds_config = self.config.get("SPIRIT_ROOT_SPEEDS", {})
        return speeds_config.get(config_key, 1.0)

    def calculate_cultivation_exp(
        self,
        player: Player,
        minutes: int,
        technique_bonus: float = 0.0,
        pill_multipliers: Optional[Dict[str, float]] = None
    ) -> int:
        """计算闭关修炼获得的修为（包含仙缘永久加成）"""
        base_exp = self.config.get("VALUES", {}).get("BASE_EXP_PER_MINUTE", 100)
        root_speed = self.get_spiritual_root_speed(player)

        cultivation_pill_bonus = 1.0
        if pill_multipliers:
            cultivation_pill_bonus = pill_multipliers.get("cultivation_speed", 1.0)

        # ===== 获取仙缘商店的永久修炼加速 =====
        global_mult = 1.0
        try:
            bonus_data = json.loads(player.permanent_bonus) if player.permanent_bonus else {}
            global_mult = 1.0 + bonus_data.get("global_cultivation_multiplier", 0.0)
        except Exception:
            pass
        # ======================================

        # 境界越高，每分钟基础收益越高（默认每级 +10%）
        try:
            level_bonus_per_level = float(
                self.config.get("VALUES", {}).get("LEVEL_EXP_BONUS_PER_LEVEL", 0.10)
            )
        except (TypeError, ValueError):
            level_bonus_per_level = 0.10
        level_mult = 1.0 + max(0, player.level_index) * level_bonus_per_level

        # 计算总修为倍率：灵根倍率 * (1 + 功法倍率) * 丹药倍率 * 仙缘倍率 * 境界倍率
        total_multiplier = (
            root_speed
            * (1.0 + technique_bonus)
            * cultivation_pill_bonus
            * global_mult
            * level_mult
        )
        total_exp = int(base_exp * minutes * total_multiplier)

        logger.info(
            f"玩家 {player.user_id} 闭关 {minutes} 分钟，"
            f"基础修为 {base_exp}，灵根倍率 {root_speed}，"
            f"功法加成 {technique_bonus:.2%}，丹药倍率 {cultivation_pill_bonus:.2f}，"
            f"仙缘倍率 {global_mult:.2f}，境界倍率 {level_mult:.2f}，获得修为 {total_exp}"
        )
        return total_exp