# core/pill_manager.py

import json
import time
from typing import Dict, List, Optional, Tuple
from astrbot.api import logger

from ..models import Player
from ..data import DataBase
from ..config_manager import ConfigManager


class PillManager:
    """丹药管理器 - 处理丹药效果、属性加成和限制机制"""

    QUALITY_ORDER = ["一般", "良好", "极佳", "大师"]
    DEFAULT_QUALITY = "一般"

    def __init__(self, db: DataBase, config_manager: ConfigManager):
        self.db = db
        self.config_manager = config_manager

    # ===== 品质与背包 key =====
    def split_pill_key(self, key: str) -> Tuple[str, str]:
        """解析 名称|品质，旧 key 默认一般。"""
        raw = str(key or "")
        if "|" in raw:
            name, quality = raw.rsplit("|", 1)
            if quality in self.QUALITY_ORDER:
                return name, quality
        return raw, self.DEFAULT_QUALITY

    def make_pill_key(self, pill_name: str, quality: str = DEFAULT_QUALITY) -> str:
        return f"{pill_name}|{quality}"

    def normalize_quality(self, quality: Optional[str]) -> Optional[str]:
        if not quality:
            return None
        quality = str(quality).strip()
        return quality if quality in self.QUALITY_ORDER else None

    def format_pill_display_name(self, base_name: str, quality: Optional[str] = None) -> str:
        """显示丹药名；默认品质不追加后缀，避免“固基丹·中·一般”。"""
        name = str(base_name or "")
        q = self.normalize_quality(quality)
        if not q or q == self.DEFAULT_QUALITY:
            return name
        return f"{name}（{q}）"

    def get_quality_info(self, quality: str) -> dict:
        try:
            return self.config_manager.get_pill_quality(quality) or {}
        except Exception:
            return {}

    def get_quality_multiplier(self, quality: str) -> float:
        try:
            return float(self.get_quality_info(quality).get("effect_multiplier", 1.0))
        except (TypeError, ValueError):
            return 1.0

    def get_breakthrough_multiplier(self, quality: str) -> float:
        try:
            return float(self.get_quality_info(quality).get("breakthrough_multiplier", 1.0))
        except (TypeError, ValueError):
            return 1.0

    def get_resurrection_keep(self, quality: str) -> float:
        try:
            return float(self.get_quality_info(quality).get("resurrection_keep", 0.5))
        except (TypeError, ValueError):
            return 0.5

    def get_quality_rank(self, quality: str) -> int:
        try:
            return int(self.get_quality_info(quality).get("order", self.QUALITY_ORDER.index(quality) if quality in self.QUALITY_ORDER else 0))
        except (TypeError, ValueError):
            return 0

    def select_pill_key(self, inventory: dict, pill_name: str, quality: Optional[str] = None) -> Optional[str]:
        """从背包中选出要消耗的 key：指定品质优先，否则自动最高品质。"""
        base_name, embedded_quality = self.split_pill_key(pill_name)
        target_quality = self.normalize_quality(quality) or (embedded_quality if "|" in str(pill_name) else None)
        candidates = []
        for key, count in inventory.items():
            try:
                if int(count) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            name, key_quality = self.split_pill_key(key)
            if name != base_name:
                continue
            if target_quality and key_quality != target_quality:
                continue
            candidates.append((self.get_quality_rank(key_quality), key, key_quality))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _consume_inventory_key(self, player: Player, pill_key: str) -> bool:
        inventory = player.get_pills_inventory()
        if pill_key not in inventory or int(inventory.get(pill_key, 0)) <= 0:
            return False
        inventory[pill_key] = int(inventory[pill_key]) - 1
        if inventory[pill_key] <= 0:
            del inventory[pill_key]
        player.set_pills_inventory(inventory)
        return True

    def _reset_daily_if_needed(self, player: Player):
        today = time.strftime("%Y-%m-%d")
        if getattr(player, "last_daily_reset", "") != today:
            player.daily_pill_usage = "{}"
            player.last_daily_reset = today

    def _check_daily_limit(self, player: Player, pill_name: str, pill_data: dict) -> Tuple[bool, str]:
        try:
            limit = int(pill_data.get("daily_limit", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return True, ""
        self._reset_daily_if_needed(player)
        try:
            usage = json.loads(player.daily_pill_usage) if player.daily_pill_usage else {}
        except Exception:
            usage = {}
        if not isinstance(usage, dict):
            usage = {}
        used = int(usage.get(pill_name, 0) or 0)
        if used >= limit:
            return False, f"【{pill_name}】今日使用次数已达上限（{limit} 次），明日再来。"
        return True, ""

    def _record_daily_use(self, player: Player, pill_name: str, pill_data: dict):
        try:
            limit = int(pill_data.get("daily_limit", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return
        self._reset_daily_if_needed(player)
        try:
            usage = json.loads(player.daily_pill_usage) if player.daily_pill_usage else {}
        except Exception:
            usage = {}
        if not isinstance(usage, dict):
            usage = {}
        usage[pill_name] = int(usage.get(pill_name, 0) or 0) + 1
        player.daily_pill_usage = json.dumps(usage, ensure_ascii=False)

    def _ensure_non_negative_attributes(self, player: Player):
        """保证属性不为负，并同步能量上限约束"""
        attrs = [
            "lifespan",
            "experience",
            "physical_damage",
            "magic_damage",
            "physical_defense",
            "magic_defense",
            "mental_power",
            "spiritual_qi",
            "max_spiritual_qi",
            "blood_qi",
            "max_blood_qi",
        ]
        for attr in attrs:
            value = getattr(player, attr, 0)
            if value < 0:
                setattr(player, attr, 0)

        if player.spiritual_qi > player.max_spiritual_qi:
            player.spiritual_qi = player.max_spiritual_qi
        if player.blood_qi > player.max_blood_qi:
            player.blood_qi = player.max_blood_qi

    def get_pill_by_name(self, pill_name: str) -> Optional[dict]:
        """根据名称获取丹药配置（自动忽略品质后缀）"""
        base_name, _ = self.split_pill_key(pill_name)
        pill = self.config_manager.pills_data.get(base_name)
        if pill:
            return pill

        pill = self.config_manager.exp_pills_data.get(base_name)
        if pill:
            return pill

        pill = self.config_manager.utility_pills_data.get(base_name)
        if pill:
            return pill

        for item in self.config_manager.items_data.values():
            if item.get("name") == base_name and item.get("type") == "丹药":
                return item

        return None

    async def update_temporary_effects(self, player: Player):
        """更新临时丹药效果，移除过期效果"""
        effects = player.get_active_pill_effects()
        current_time = int(time.time())
        updated_effects = []
        has_changes = False

        for effect in effects:
            if self._apply_periodic_effects(player, effect, current_time):
                has_changes = True

            expiry_time = effect.get("expiry_time", 0)
            if expiry_time <= 0 or current_time < expiry_time:
                updated_effects.append(effect)
            else:
                has_changes = True
                logger.info(f"玩家 {player.user_id} 的丹药效果 {effect.get('pill_name')} 已过期")

        if has_changes or len(updated_effects) != len(effects):
            player.set_active_pill_effects(updated_effects)
            await self.db.update_player(player)

    async def use_pill(
        self,
        player: Player,
        pill_name: str,
        quality: Optional[str] = None
    ) -> Tuple[bool, str]:
        """使用丹药。quality 为空时自动选择背包中最高品质。"""
        inventory = player.get_pills_inventory()
        pill_key = self.select_pill_key(inventory, pill_name, quality)
        if not pill_key:
            base_name, _ = self.split_pill_key(pill_name)
            quality_hint = f"（{quality}）" if quality else ""
            return False, f"你的背包中没有【{base_name}】{quality_hint}！"

        base_name, actual_quality = self.split_pill_key(pill_key)
        pill_data = self.get_pill_by_name(base_name)
        if not pill_data:
            return False, f"丹药【{base_name}】配置不存在！"

        required_level = int(pill_data.get("required_level_index", 0) or 0)
        if player.level_index < required_level:
            level_data = self.config_manager.get_level_data(player.cultivation_type)
            level_name = f"境界{required_level}"
            if 0 <= required_level < len(level_data):
                level_name = level_data[required_level]["level_name"]
            return False, (
                f"❌ 境界不足，无法服用！\n"
                f"━━━━━━━━━━━━━━━\n"
                f"【{base_name}·{actual_quality}】需要达到【{level_name}】（境界等级 {required_level}）\n"
                f"你当前境界等级：{player.level_index}（还差 {required_level - player.level_index} 级）\n"
                f"💡 丹药不会过期，先提升境界再来服用\n"
                f"━━━━━━━━━━━━━━━\n"
                f"💡 通过 /闭关 /历练 提升修为后 /突破 提高境界"
            )

        effect_type = pill_data.get("effect_type", "instant")
        subtype = pill_data.get("subtype", "")

        if subtype == "breakthrough":
            return False, f"【{base_name}·{actual_quality}】是破境丹，只能在突破时使用！"

        allowed, limit_msg = self._check_daily_limit(player, base_name, pill_data)
        if not allowed:
            return False, limit_msg

        if subtype == "exp":
            result = await self._use_exp_pill(player, pill_key, pill_data, actual_quality)
        elif subtype == "resurrection":
            result = await self._use_resurrection_pill(player, pill_key, pill_data, actual_quality)
        elif pill_data.get("effect") and not pill_data.get("effect_type"):
            result = await self._use_legacy_effect_pill(player, pill_key, pill_data, actual_quality)
        elif effect_type == "temporary":
            result = await self._use_temporary_pill(player, pill_key, pill_data, actual_quality)
        elif effect_type == "permanent":
            result = await self._use_permanent_pill(player, pill_key, pill_data, actual_quality)
        elif effect_type == "instant":
            result = await self._use_instant_pill(player, pill_key, pill_data, actual_quality)
        else:
            return False, f"未知的丹药类型：{effect_type}"

        if result[0]:
            self._record_daily_use(player, base_name, pill_data)
            if pill_data.get("daily_limit"):
                await self.db.update_player(player)
        return result

    # ===== 批量服用与累计效果 =====
    def _pill_snapshot(self, player: Player) -> dict:
        attr_keys = (
            "experience", "spiritual_qi", "max_spiritual_qi",
            "hp", "mp",
            "blood_qi", "max_blood_qi", "lifespan", "mental_power",
            "physical_damage", "magic_damage",
            "physical_defense", "magic_defense",
            "has_resurrection_pill",
        )
        attrs = {}
        for key in attr_keys:
            value = getattr(player, key, 0)
            attrs[key] = bool(value) if key == "has_resurrection_pill" else int(value or 0)
        effect_names = []
        for effect in player.get_active_pill_effects():
            if isinstance(effect, dict):
                name = effect.get("pill_name") or effect.get("name")
                if name and name not in effect_names:
                    effect_names.append(str(name))
        return {"attrs": attrs, "effects": tuple(sorted(effect_names))}

    def _format_batch_effect(self, before: dict, after: dict, last_message: str) -> str:
        labels = (
            ("experience", "修为"),
            ("hp", "战斗气血"),
            ("mp", "战斗真元"),
            ("spiritual_qi", "灵气"),
            ("max_spiritual_qi", "灵气上限"),
            ("blood_qi", "气血"),
            ("max_blood_qi", "气血上限"),
            ("lifespan", "寿命"),
            ("mental_power", "精神力"),
            ("physical_damage", "物伤"),
            ("magic_damage", "法伤"),
            ("physical_defense", "物防"),
            ("magic_defense", "法防"),
        )
        lines = []
        for key, label in labels:
            delta = int(after["attrs"].get(key, 0)) - int(before["attrs"].get(key, 0))
            if delta:
                lines.append(f"{label} {delta:+,}")
        new_effects = sorted(set(after["effects"]) - set(before["effects"]))
        if new_effects:
            lines.append("新增临时效果：" + "、".join(new_effects[:6]))
        if after["attrs"].get("has_resurrection_pill") and not before["attrs"].get("has_resurrection_pill"):
            lines.append("获得回生效果")
        return "\n".join(lines) if lines else last_message

    async def use_pill_batch(
        self, player: Player, pill_name: str, quality: Optional[str], count: int
    ) -> Tuple[bool, int, str]:
        """连续服用丹药，返回实际数量和累计效果，而不是最后一颗的单颗效果。"""
        count = max(0, int(count or 0))
        if count <= 0:
            return False, 0, "❌ 服用数量必须大于 0。"

        before = self._pill_snapshot(player)
        used = 0
        last_message = ""
        error_message = ""
        for _ in range(count):
            success, message = await self.use_pill(player, pill_name, quality)
            if not success:
                error_message = message
                break
            used += 1
            last_message = message

        if used <= 0:
            return False, 0, error_message or "❌ 服用失败。"

        after = self._pill_snapshot(player)
        effect_text = self._format_batch_effect(before, after, last_message)
        display_name = self.format_pill_display_name(pill_name, quality)
        if error_message:
            header = (
                f"⚠️ 已服用【{display_name}】x{used}，后续停止："
                f"{error_message}"
            )
        else:
            header = f"✅ 已服用【{display_name}】x{used}"
        return True, used, f"{header}\n━━━━━━━━━━━━━━━\n{effect_text}"

    async def _use_exp_pill(self, player: Player, pill_key: str, pill_data: dict, quality: str) -> Tuple[bool, str]:
        """使用修为丹"""
        base_name, _ = self.split_pill_key(pill_key)
        mult = self.get_quality_multiplier(quality)
        exp_gain = int(float(pill_data.get("exp_gain", 0)) * mult)
        player.experience += exp_gain

        self._consume_inventory_key(player, pill_key)
        await self.db.update_player(player)

        return True, (
            f"✨ 服用【{base_name}·{quality}】成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📈 获得修为：{exp_gain}\n"
            f"💫 当前修为：{player.experience}\n"
            f"━━━━━━━━━━━━━━━"
        )

    async def _use_resurrection_pill(self, player: Player, pill_key: str, pill_data: dict, quality: str) -> Tuple[bool, str]:
        """使用回生丹"""
        if player.has_resurrection_pill:
            return False, "你已经拥有回生丹效果，无需重复使用！"

        base_name, _ = self.split_pill_key(pill_key)
        player.has_resurrection_pill = True
        player.resurrection_pill_quality = quality

        self._consume_inventory_key(player, pill_key)
        await self.db.update_player(player)

        keep = self.get_resurrection_keep(quality)
        return True, (
            f"✨ 服用【{base_name}·{quality}】成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🛡️ 你获得了起死回生的能力\n"
            f"下次死亡时将自动复活\n"
            f"（复活后保留 {keep:.0%} 属性）\n"
            f"━━━━━━━━━━━━━━━"
        )

    async def _use_temporary_pill(self, player: Player, pill_key: str, pill_data: dict, quality: str) -> Tuple[bool, str]:
        """使用临时效果丹药"""
        base_name, _ = self.split_pill_key(pill_key)
        duration_minutes = pill_data.get("duration_minutes", 60)
        current_time = int(time.time())
        expiry_time = current_time + duration_minutes * 60
        mult = self.get_quality_multiplier(quality)

        effect = {
            "pill_name": base_name,
            "pill_key": pill_key,
            "quality": quality,
            "pill_id": pill_data.get("id", ""),
            "subtype": pill_data.get("subtype", ""),
            "start_time": current_time,
            "expiry_time": expiry_time,
            "duration_minutes": duration_minutes,
            "last_tick_time": current_time,
        }

        effect_keys = [
            "cultivation_multiplier", "physical_damage_multiplier", "magic_damage_multiplier",
            "physical_defense_multiplier", "magic_defense_multiplier",
            "lifespan_cost_per_minute", "lifespan_regen_per_minute",
            "spiritual_qi_regen_per_minute", "blood_qi_regen_per_minute", "blood_qi_cost_per_minute",
            "breakthrough_bonus"
        ]
        cost_keys = {"lifespan_cost_per_minute", "blood_qi_cost_per_minute"}
        for key in effect_keys:
            if key in pill_data:
                value = float(pill_data[key])
                effect[key] = value / mult if key in cost_keys and mult > 0 else value * mult

        # 同名丹药不叠加：刷新为新服用的品质与时间
        effects = [
            e for e in player.get_active_pill_effects()
            if e.get("pill_name") != base_name
        ]
        effects.append(effect)
        player.set_active_pill_effects(effects)

        self._consume_inventory_key(player, pill_key)
        await self.db.update_player(player)

        effect_desc = []
        if "cultivation_multiplier" in effect:
            value = effect["cultivation_multiplier"]
            effect_desc.append(f"修炼速度{'+' if value > 0 else ''}{value:.0%}")
        if "physical_damage_multiplier" in effect:
            value = effect["physical_damage_multiplier"]
            effect_desc.append(f"物伤{'+' if value > 0 else ''}{value:.0%}")
        if "magic_damage_multiplier" in effect:
            value = effect["magic_damage_multiplier"]
            effect_desc.append(f"法伤{'+' if value > 0 else ''}{value:.0%}")
        if "breakthrough_bonus" in effect:
            value = effect["breakthrough_bonus"]
            effect_desc.append(f"突破成功率{'+' if value > 0 else ''}{value:.0%}")

        effects_str = "、".join(effect_desc) if effect_desc else "特殊效果"

        return True, (
            f"✨ 服用【{base_name}·{quality}】成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⏱️ 持续时间：{duration_minutes}分钟\n"
            f"🎯 效果：{effects_str}\n"
            f"━━━━━━━━━━━━━━━"
        )

    async def _use_permanent_pill(self, player: Player, pill_key: str, pill_data: dict, quality: str) -> Tuple[bool, str]:
        """使用永久属性丹药"""
        base_name, _ = self.split_pill_key(pill_key)
        mult = self.get_quality_multiplier(quality)
        permanent_gains = player.get_permanent_pill_gains()
        level_key = f"level_{player.level_index}"

        if level_key not in permanent_gains:
            permanent_gains[level_key] = {
                "physical_damage": 0, "magic_damage": 0,
                "physical_defense": 0, "magic_defense": 0,
                "mental_power": 0, "lifespan": 0,
                "max_spiritual_qi": 0, "max_blood_qi": 0,
            }

        base_attrs = self._get_base_attributes_for_level(player, player.level_index)

        attr_mapping = {
            "physical_damage_gain": ("physical_damage", "物伤"),
            "magic_damage_gain": ("magic_damage", "法伤"),
            "physical_defense_gain": ("physical_defense", "物防"),
            "magic_defense_gain": ("magic_defense", "法防"),
            "mental_power_gain": ("mental_power", "精神力"),
            "lifespan_gain": ("lifespan", "寿命"),
            "max_spiritual_qi_gain": ("max_spiritual_qi", "最大灵气"),
            "max_blood_qi_gain": ("max_blood_qi", "最大气血"),
        }

        gains_applied = {}
        gains_blocked = {}

        for gain_key, (attr_key, attr_name) in attr_mapping.items():
            if gain_key not in pill_data:
                continue

            raw_gain = float(pill_data[gain_key])
            gain = int(raw_gain * mult) if raw_gain > 0 else int(raw_gain)
            if gain == 0:
                continue

            if gain > 0:
                current_gain = permanent_gains[level_key].get(attr_key, 0)
                base_value = base_attrs.get(attr_key, 100)
                limit = base_value * 0.3

                if current_gain >= limit:
                    gains_blocked[attr_name] = f"已达上限({limit:.0f})"
                    continue

                actual_gain = min(gain, limit - current_gain)
                if actual_gain < gain:
                    gains_blocked[attr_name] = f"部分受限(+{actual_gain:.0f}/{gain})"

                permanent_gains[level_key][attr_key] += actual_gain
                setattr(player, attr_key, getattr(player, attr_key) + int(actual_gain))
                gains_applied[attr_name] = int(actual_gain)
            else:
                permanent_gains[level_key][attr_key] += gain
                setattr(player, attr_key, getattr(player, attr_key) + int(gain))
                gains_applied[attr_name] = int(gain)

        if "cultivation_multiplier" in pill_data:
            cult_mult = float(pill_data["cultivation_multiplier"]) * mult
            if "cultivation_multiplier" not in permanent_gains[level_key]:
                permanent_gains[level_key]["cultivation_multiplier"] = 0
            permanent_gains[level_key]["cultivation_multiplier"] += cult_mult
            gains_applied["修炼速度"] = f"{cult_mult:+.0%}"

        if "death_protection_multiplier" in pill_data:
            death_mult = pill_data["death_protection_multiplier"]
            if "death_protection_multiplier" not in permanent_gains[level_key]:
                permanent_gains[level_key]["death_protection_multiplier"] = 1.0
            permanent_gains[level_key]["death_protection_multiplier"] *= death_mult
            gains_applied["突破死亡概率"] = f"降低{(1 - death_mult) * 100:.0f}%"

        if not gains_applied:
            return False, "该丹药的所有属性增益都已达到上限，无法使用！"

        self._ensure_non_negative_attributes(player)
        player.set_permanent_pill_gains(permanent_gains)
        self._consume_inventory_key(player, pill_key)

        await self.db.update_player(player)

        msg_parts = [f"✨ 服用【{base_name}·{quality}】成功！", "━━━━━━━━━━━━━━━", "💪 永久增益："]
        for attr_name, value in gains_applied.items():
            if isinstance(value, int):
                msg_parts.append(f"  {attr_name} +{value}")
            else:
                msg_parts.append(f"  {attr_name} {value}")

        if gains_blocked:
            msg_parts.append("\n⚠️ 受限提示：")
            for attr_name, reason in gains_blocked.items():
                msg_parts.append(f"  {attr_name} {reason}")

        msg_parts.append("━━━━━━━━━━━━━━━")
        msg_parts.append("注：每个境界的永久属性丹药增益最多为基础属性的30%")

        return True, "\n".join(msg_parts)

    async def _use_instant_pill(self, player: Player, pill_key: str, pill_data: dict, quality: str) -> Tuple[bool, str]:
        """使用瞬间效果丹药"""
        base_name, _ = self.split_pill_key(pill_key)
        mult = self.get_quality_multiplier(quality)
        msg_parts = [f"✨ 服用【{base_name}·{quality}】成功！", "━━━━━━━━━━━━━━━"]

        energy_restore = None
        energy_label = "灵气"
        current_energy = player.spiritual_qi
        max_energy = player.max_spiritual_qi

        if player.cultivation_type == "体修" and "blood_qi_restore" in pill_data:
            energy_restore = pill_data["blood_qi_restore"]
            energy_label = "气血"
            current_energy = player.blood_qi
            max_energy = player.max_blood_qi
        elif "spiritual_qi_restore" in pill_data:
            energy_restore = pill_data["spiritual_qi_restore"]
            if player.cultivation_type == "体修":
                energy_label = "气血"
                current_energy = player.blood_qi
                max_energy = player.max_blood_qi

        # 战斗气血/真元恢复
        if "restore_hp_percent" in pill_data or "restore_mp_percent" in pill_data:
            from ..managers.combat_manager import CombatManager
            stats = CombatManager.build_player_combat_stats(
                player, self.config_manager, db=self.db
            )
            if "restore_hp_percent" in pill_data:
                pct = max(0.0, min(1.0, float(pill_data.get("restore_hp_percent", 0) or 0) * mult))
                old = max(0, int(player.hp or 0))
                new = stats.max_hp if pct >= 1.0 else min(stats.max_hp, old + int(stats.max_hp * pct))
                player.hp = new
                msg_parts.append(f"🌟 恢复气血：+{max(0, new - old)}")
                msg_parts.append(f"🩸 当前气血：{player.hp}/{stats.max_hp}")
            if "restore_mp_percent" in pill_data:
                pct = max(0.0, min(1.0, float(pill_data.get("restore_mp_percent", 0) or 0) * mult))
                old = max(0, int(player.mp or 0))
                new = stats.max_mp if pct >= 1.0 else min(stats.max_mp, old + int(stats.max_mp * pct))
                player.mp = new
                msg_parts.append(f"💠 恢复真元：+{max(0, new - old)}")
                msg_parts.append(f"💫 当前真元：{player.mp}/{stats.max_mp}")

        if energy_restore is not None:
            if energy_restore == -1:
                actual_restore = max_energy - current_energy
                current_energy = max_energy
            else:
                restore_value = int(float(energy_restore) * mult)
                old_energy = current_energy
                current_energy = min(current_energy + restore_value, max_energy)
                actual_restore = current_energy - old_energy

            if energy_label == "气血":
                player.blood_qi = current_energy
                msg_parts.append(f"🌟 恢复气血：+{actual_restore}")
                msg_parts.append(f"🩸 当前气血：{player.blood_qi}/{player.max_blood_qi}")
            else:
                player.spiritual_qi = current_energy
                msg_parts.append(f"🌟 恢复灵气：+{actual_restore}")
                msg_parts.append(f"💫 当前灵气：{player.spiritual_qi}/{player.max_spiritual_qi}")

        if pill_data.get("resets_permanent_pills"):
            reset_applied = self._reset_permanent_pill_effects(player)
            if reset_applied:
                msg_parts.append("🔄 已重置所有永久属性丹药增益")
                refund_ratio = min(1.0, float(pill_data.get("reset_refund_ratio", 0.5)) * mult)
                refund = int(float(pill_data.get("price", 0)) * refund_ratio)
                if refund > 0:
                    player.gold += refund
                    msg_parts.append(f"💰 返还灵石：{refund}")
            else:
                msg_parts.append("ℹ️ 当前没有可重置的永久增益")

        if pill_data.get("blocks_next_debuff"):
            if player.has_debuff_shield:
                msg_parts.append("🛡️ 定魂护盾已存在，无需重复使用")
            else:
                player.has_debuff_shield = True
                msg_parts.append("🛡️ 获得定魂护盾：下一次负面效果将被抵消")

        self._consume_inventory_key(player, pill_key)
        await self.db.update_player(player)
        msg_parts.append("━━━━━━━━━━━━━━━")
        return True, "\n".join(msg_parts)

    async def _use_legacy_effect_pill(self, player: Player, pill_key: str, pill_data: dict, quality: str) -> Tuple[bool, str]:
        """兼容 items.json 旧式 effect 丹药：统一进入丹药背包后仍可正常服用。"""
        base_name, _ = self.split_pill_key(pill_key)
        mult = self.get_quality_multiplier(quality)
        effects = pill_data.get("effect") or {}
        if not isinstance(effects, dict) or not effects:
            return False, f"丹药【{base_name}】没有可用的效果配置。"

        msgs = []
        def scaled(key):
            try:
                return int(float(effects.get(key, 0)) * mult)
            except (TypeError, ValueError):
                return 0

        add_hp = scaled("add_hp")
        if add_hp:
            if player.cultivation_type == "体修":
                old = player.blood_qi
                player.blood_qi = max(0, min(player.max_blood_qi, player.blood_qi + add_hp))
                msgs.append(f"气血{player.blood_qi - old:+d}")
            else:
                old = player.spiritual_qi
                player.spiritual_qi = max(0, min(player.max_spiritual_qi, player.spiritual_qi + add_hp))
                msgs.append(f"灵气{player.spiritual_qi - old:+d}")

        add_exp = scaled("add_experience")
        if add_exp:
            player.experience += add_exp
            msgs.append(f"修为{add_exp:+d}")

        add_max_hp = scaled("add_max_hp")
        if add_max_hp:
            if player.cultivation_type == "体修":
                player.max_blood_qi += add_max_hp
                msgs.append(f"最大气血{add_max_hp:+d}")
            else:
                player.max_spiritual_qi += add_max_hp
                msgs.append(f"最大灵气{add_max_hp:+d}")

        for key, attr, label in (
            ("add_spiritual_power", "magic_damage", "法伤"),
            ("add_mental_power", "mental_power", "精神力"),
            ("add_attack", "physical_damage", "物伤"),
            ("add_defense", "physical_defense", "物防"),
        ):
            value = scaled(key)
            if value:
                setattr(player, attr, getattr(player, attr) + value)
                msgs.append(f"{label}{value:+d}")

        add_gold = scaled("add_gold")
        if add_gold:
            player.gold += add_gold
            msgs.append(f"灵石{add_gold:+d}")

        breakthrough_bonus = float(effects.get("add_breakthrough_bonus", 0) or 0) * mult
        if breakthrough_bonus:
            current_time = int(time.time())
            effects_list = [
                e for e in player.get_active_pill_effects()
                if e.get("pill_name") != base_name
            ]
            effects_list.append({
                "pill_name": base_name,
                "pill_key": pill_key,
                "quality": quality,
                "subtype": "breakthrough_boost",
                "start_time": current_time,
                "expiry_time": current_time + 3600,
                "duration_minutes": 60,
                "last_tick_time": current_time,
                "breakthrough_bonus": breakthrough_bonus,
            })
            player.set_active_pill_effects(effects_list)
            msgs.append(f"突破成功率{breakthrough_bonus:+.0%}（1小时）")

        self._ensure_non_negative_attributes(player)
        self._consume_inventory_key(player, pill_key)
        await self.db.update_player(player)

        return True, (
            f"✨ 服用【{base_name}·{quality}】成功！\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🎯 效果：" + "、".join(msgs[:6]) + "\n"
            f"━━━━━━━━━━━━━━━"
        )

    def _get_base_attributes_for_level(self, player: Player, level_index: int) -> dict:
        """获取当前境界的基础属性"""
        level_data = self.config_manager.get_level_data(player.cultivation_type)
        if not level_data:
            level_data = self.config_manager.level_data

        if level_data:
            level_index = min(level_index, len(level_data) - 1)
            level_config = level_data[level_index]
        else:
            level_config = {}

        return {
            "physical_damage": level_config.get("breakthrough_physical_damage_gain", 10),
            "magic_damage": level_config.get("breakthrough_magic_damage_gain", 10),
            "physical_defense": level_config.get("breakthrough_physical_defense_gain", 5),
            "magic_defense": level_config.get("breakthrough_magic_defense_gain", 5),
            "mental_power": level_config.get("breakthrough_mental_power_gain", 100),
            "lifespan": level_config.get("breakthrough_lifespan_gain", 100),
            "max_spiritual_qi": level_config.get("breakthrough_spiritual_qi_gain", 100),
            "max_blood_qi": level_config.get("breakthrough_blood_qi_gain", 100),
        }

    async def handle_resurrection(self, player: Player) -> bool:
        """处理玩家死亡时的回生丹效果"""
        if not player.has_resurrection_pill:
            return False

        quality = getattr(player, "resurrection_pill_quality", self.DEFAULT_QUALITY) or self.DEFAULT_QUALITY
        keep = self.get_resurrection_keep(quality)
        logger.info(f"玩家 {player.user_id} 触发回生丹效果（{quality}，保留 {keep:.0%}）")
        player.has_resurrection_pill = False
        player.resurrection_pill_quality = self.DEFAULT_QUALITY

        def kept(value):
            return int(value * keep)

        player.lifespan = kept(player.lifespan)
        player.experience = kept(player.experience)
        player.physical_damage = kept(player.physical_damage)
        player.magic_damage = kept(player.magic_damage)
        player.physical_defense = kept(player.physical_defense)
        player.magic_defense = kept(player.magic_defense)
        player.mental_power = kept(player.mental_power)
        player.max_spiritual_qi = kept(player.max_spiritual_qi)
        player.spiritual_qi = player.max_spiritual_qi
        player.max_blood_qi = kept(player.max_blood_qi)
        player.blood_qi = player.max_blood_qi

        # 永久丹药增益记录同步缩放，否则复活后旧记录仍占满 30% 上限，无法继续吃永久丹
        try:
            permanent_gains = player.get_permanent_pill_gains()
            for gains in permanent_gains.values():
                if not isinstance(gains, dict):
                    continue
                for attr_key in (
                    "physical_damage", "magic_damage", "physical_defense",
                    "magic_defense", "mental_power", "lifespan",
                    "max_spiritual_qi", "max_blood_qi",
                ):
                    if attr_key in gains:
                        gains[attr_key] = kept(int(gains.get(attr_key, 0) or 0))
            player.set_permanent_pill_gains(permanent_gains)
        except Exception:
            pass

        self._ensure_non_negative_attributes(player)
        await self.db.update_player(player)
        return True

    def calculate_pill_attribute_effects(self, player: Player) -> dict:
        """计算丹药对属性的影响"""
        effects = player.get_active_pill_effects()
        current_time = int(time.time())
        multipliers = {
            "physical_damage": 1.0,
            "magic_damage": 1.0,
            "physical_defense": 1.0,
            "magic_defense": 1.0,
            "cultivation_speed": 1.0,
        }

        for effect in effects:
            expiry_time = effect.get("expiry_time", 0)
            if expiry_time > 0 and current_time >= expiry_time:
                continue
            if "physical_damage_multiplier" in effect:
                multipliers["physical_damage"] += effect["physical_damage_multiplier"]
            if "magic_damage_multiplier" in effect:
                multipliers["magic_damage"] += effect["magic_damage_multiplier"]
            if "physical_defense_multiplier" in effect:
                multipliers["physical_defense"] += effect["physical_defense_multiplier"]
            if "magic_defense_multiplier" in effect:
                multipliers["magic_defense"] += effect["magic_defense_multiplier"]
            if "cultivation_multiplier" in effect:
                multipliers["cultivation_speed"] += effect["cultivation_multiplier"]

        permanent_gains = player.get_permanent_pill_gains()
        level_key = f"level_{player.level_index}"
        if level_key in permanent_gains:
            level_gains = permanent_gains[level_key]
            if "cultivation_multiplier" in level_gains:
                multipliers["cultivation_speed"] += level_gains["cultivation_multiplier"]

        cap = 3.0
        try:
            cap = float(self.config_manager.pill_quality_data.get("temporary_effect_cap", 3.0))
        except Exception:
            cap = 3.0
        for key in multipliers:
            multipliers[key] = max(0.0, min(cap, multipliers[key]))

        return multipliers

    def get_breakthrough_modifiers(self, player: Player) -> dict:
        """获取突破时的临时与永久加成信息"""
        effects = player.get_active_pill_effects()
        current_time = int(time.time())
        temp_bonus = 0.0
        has_temp_effects = False

        bonus_groups = {}
        for effect in effects:
            expiry_time = effect.get("expiry_time", 0)
            if expiry_time > 0 and current_time >= expiry_time:
                continue

            subtype = effect.get("subtype", "")
            if subtype in {"breakthrough_boost", "breakthrough_debuff"}:
                pill_name = effect.get("pill_name", "")
                group = "other"
                if any(x in pill_name for x in ["凝神增益丹", "破境增益丹", "渡劫增益丹", "化神增益丹"]):
                    group = "zengyi"
                
                current_bonus = effect.get("breakthrough_bonus", 0)
                if group not in bonus_groups or current_bonus > bonus_groups[group]:
                    bonus_groups[group] = current_bonus

        temp_bonus = sum(bonus_groups.values())
        has_temp_effects = len(bonus_groups) > 0
        temp_bonus = min(temp_bonus, 0.5)

        permanent_multiplier = 1.0
        permanent_gains = player.get_permanent_pill_gains()
        for level_gain in permanent_gains.values():
            permanent_multiplier *= level_gain.get("death_protection_multiplier", 1.0)

        return {
            "temp_bonus": temp_bonus,
            "has_temp_effects": has_temp_effects,
            "permanent_death_multiplier": max(0.0, min(1.0, permanent_multiplier)),
        }

    async def consume_breakthrough_effects(self, player: Player):
        """突破完成后移除相关临时丹药效果"""
        effects = player.get_active_pill_effects()
        remaining_effects = [
            effect for effect in effects
            if effect.get("subtype", "") not in {"breakthrough_boost", "breakthrough_debuff"}
        ]

        if len(remaining_effects) != len(effects):
            player.set_active_pill_effects(remaining_effects)
            await self.db.update_player(player)

    async def add_pill_to_inventory(self, player: Player, pill_name: str, count: int = 1, external_transaction: bool = False, quality: str = DEFAULT_QUALITY):
        """添加丹药到背包（品质分 key）"""
        base_name, embedded_quality = self.split_pill_key(pill_name)
        quality = self.normalize_quality(quality) or embedded_quality
        pill_key = self.make_pill_key(base_name, quality) if "|" not in pill_name else pill_name
        inventory = player.get_pills_inventory()
        inventory[pill_key] = int(inventory.get(pill_key, 0)) + count
        player.set_pills_inventory(inventory)
        
        if not external_transaction:
            await self.db.update_player(player)

    def get_pill_inventory_display(self, player: Player) -> str:
        """获取丹药背包显示文本"""
        inventory = player.get_pills_inventory()
        if not inventory:
            return "你的丹药背包是空的！"

        grouped = {}
        for pill_key, count in inventory.items():
            base_name, quality = self.split_pill_key(pill_key)
            try:
                count = int(count)
            except (TypeError, ValueError):
                count = 0
            if count <= 0:
                continue
            grouped.setdefault(base_name, {})[quality] = grouped.setdefault(base_name, {}).get(quality, 0) + count

        lines = ["--- 丹药背包 ---"]
        for base_name in sorted(grouped):
            pill_data = self.get_pill_by_name(base_name)
            rank = pill_data.get("rank", "未知") if pill_data else "未知"
            parts = []
            for quality in self.QUALITY_ORDER:
                count = grouped[base_name].get(quality, 0)
                if count:
                    parts.append(f"{quality}×{count}")
            if not parts:
                continue
            lines.append(f"[{rank}] {base_name}（{'、'.join(parts)}）")

        lines.append("-" * 20)
        return "\n".join(lines)

    def _apply_periodic_effects(self, player: Player, effect: dict, current_time: int) -> bool:
        """根据时间自动结算持续恢复/扣减"""
        expiry_time = effect.get("expiry_time", 0)
        tick_limit = min(current_time, expiry_time) if expiry_time > 0 else current_time
        last_tick = effect.get("last_tick_time", effect.get("start_time", current_time))

        if tick_limit <= last_tick:
            return False

        elapsed_seconds = tick_limit - last_tick
        minutes = elapsed_seconds // 60
        if minutes <= 0:
            return False

        effect["last_tick_time"] = last_tick + minutes * 60
        changed = False

        if "lifespan_cost_per_minute" in effect:
            total_cost = effect["lifespan_cost_per_minute"] * minutes
            player.lifespan = max(0, player.lifespan - total_cost)
            changed = True

        if "lifespan_regen_per_minute" in effect:
            total_regen = effect["lifespan_regen_per_minute"] * minutes
            player.lifespan += total_regen
            changed = True

        if "spiritual_qi_regen_per_minute" in effect:
            total_qi = effect["spiritual_qi_regen_per_minute"] * minutes
            player.spiritual_qi = min(player.max_spiritual_qi, player.spiritual_qi + total_qi)
            changed = True

        if "blood_qi_regen_per_minute" in effect:
            total_blood = effect["blood_qi_regen_per_minute"] * minutes
            player.blood_qi = min(player.max_blood_qi, player.blood_qi + total_blood)
            changed = True

        if "blood_qi_cost_per_minute" in effect:
            total_cost = effect["blood_qi_cost_per_minute"] * minutes
            player.blood_qi = max(0, player.blood_qi - total_cost)
            changed = True

        if changed:
            self._ensure_non_negative_attributes(player)

        return changed

    def _reset_permanent_pill_effects(self, player: Player) -> bool:
        """清空永久丹药增益并回退属性"""
        permanent_gains = player.get_permanent_pill_gains()
        if not permanent_gains:
            return False

        attr_keys = [
            "physical_damage",
            "magic_damage",
            "physical_defense",
            "magic_defense",
            "mental_power",
            "lifespan",
            "max_spiritual_qi",
            "max_blood_qi",
        ]

        changed = False
        for gain in permanent_gains.values():
            for attr_key in attr_keys:
                value = gain.get(attr_key, 0)
                if value:
                    delta = int(value)
                    setattr(player, attr_key, getattr(player, attr_key) - delta)
                    changed = True

            if "cultivation_multiplier" in gain:
                gain["cultivation_multiplier"] = 0
            if "death_protection_multiplier" in gain:
                gain["death_protection_multiplier"] = 1.0

        player.set_permanent_pill_gains({})
        self._ensure_non_negative_attributes(player)
        return changed