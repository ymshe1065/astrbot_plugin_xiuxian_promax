# tests/test_pill_effect_fidelity.py
# 丹药「描述承诺」与「实际生效」的一致性守卫
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")


class _StubLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


api_mod.logger = _StubLogger()
api_mod.AstrBotConfig = dict
event_mod.AstrMessageEvent = object
api_mod.event = event_mod
all_mod = types.ModuleType("astrbot.api.all")
all_mod.At = object
all_mod.Plain = object
api_mod.all = all_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod)
sys.modules.setdefault("astrbot.api.all", all_mod)


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class _FakeDB:
    async def get_player_by_id(self, user_id):
        return None

    async def update_player(self, player, commit=True):
        return None


class TestPillEffectFidelity(unittest.IsolatedAsyncioTestCase):
    """静态配置 + 真实服用流程，双重校验描述与实际效果。"""

    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.config_manager = _imp("config_manager").ConfigManager(ROOT)
        cls.pill_module = _imp("core.pill_manager")
        cls.models = _imp("models")
        cls.cult_module = _imp("core.cultivation_manager")

    def _pm(self):
        return self.pill_module.PillManager(_FakeDB(), self.config_manager)

    def _player(self, level_index=16):
        p = self.models.Player(user_id="u1")
        p.level_index = level_index
        p.permanent_bonus = "{}"
        p.set_active_pill_effects([])
        p.set_permanent_pill_gains({})
        p.pills_inventory = json.dumps({"伐体丹|一般": 5, "无缺丹|一般": 5}, ensure_ascii=False)
        for attr, value in (
            ("physical_damage", 10000), ("magic_damage", 10000),
            ("physical_defense", 5000), ("magic_defense", 5000),
            ("mental_power", 50000), ("lifespan", 5000),
            ("max_spiritual_qi", 10000), ("max_blood_qi", 10000),
        ):
            setattr(p, attr, value)
        return p

    # ---------- 伐体丹：永久降低所有属性 30% ----------
    async def test_weiti_penalty_actually_applies(self):
        """描述承诺「永久降低所有属性-30%」，曾经四个倍率字段被静默丢弃。"""
        player = self._player()
        pm = self._pm()
        ok, msg = await pm._use_permanent_pill(player, "伐体丹|一般", pm.get_pill_by_name("伐体丹"), "一般")
        self.assertTrue(ok, msg)

        multipliers = pm.calculate_pill_attribute_effects(player)
        for key in ("physical_damage", "magic_damage", "physical_defense", "magic_defense"):
            self.assertAlmostEqual(multipliers[key], 0.7, places=3, msg=f"{key} 未按 -30% 生效")
        self.assertAlmostEqual(multipliers["cultivation_speed"], 3.0, places=3)

    async def test_weiti_stacks_with_repeat_use(self):
        player = self._player()
        pm = self._pm()
        pill = pm.get_pill_by_name("伐体丹")
        await pm._use_permanent_pill(player, "伐体丹|一般", pill, "一般")
        await pm._use_permanent_pill(player, "伐体丹|一般", pill, "一般")
        multipliers = pm.calculate_pill_attribute_effects(player)
        # 两层 -30% → 0.4，但倍率下限保护不能为负
        self.assertAlmostEqual(multipliers["physical_damage"], 0.4, places=3)

    # ---------- 无缺丹：五项都要动 ----------
    async def test_wuque_touches_all_five_attributes(self):
        player = self._player(level_index=16)
        pm = self._pm()
        pill = pm.get_pill_by_name("无缺丹")
        ok, msg = await pm._use_permanent_pill(player, "无缺丹|一般", pill, "一般")
        self.assertTrue(ok, msg)

        gains = player.get_permanent_pill_gains()["level_16"]
        for attr in ("physical_damage", "magic_damage", "physical_defense", "magic_defense", "mental_power"):
            self.assertGreater(gains.get(attr, 0), 0, f"{attr} 一点都没加上：{msg}")
        # 精神力的配置额度是其它四项的两倍
        self.assertGreaterEqual(gains["mental_power"], gains["physical_damage"])

    async def test_wuque_reports_limit_when_capped(self):
        player = self._player(level_index=16)
        pm = self._pm()
        pill = pm.get_pill_by_name("无缺丹")
        first_ok, _ = await pm._use_permanent_pill(player, "无缺丹|一般", pill, "一般")
        self.assertTrue(first_ok)
        # 第二颗：额度已被第一颗吃满
        second_ok, second_msg = await pm._use_permanent_pill(player, "无缺丹|一般", pill, "一般")
        self.assertFalse(second_ok)
        self.assertIn("上限", second_msg)

    # ---------- 描述与配置的静态一致性 ----------
    def test_all_attr_pills_describe_mental_quirk(self):
        """「所有属性+N」的丹药精神力实际是 2N，描述必须写清楚。"""
        pills = self.config_manager.utility_pills_data
        offenders = []
        for name, data in pills.items():
            desc = str(data.get("description") or "")
            if "所有属性" not in desc:
                continue
            mental = int(data.get("mental_power_gain") or 0)
            base = int(data.get("physical_damage_gain") or 0)
            if mental and base and mental != base and "精神力" not in desc:
                offenders.append(name)
        self.assertEqual(offenders, [], f"描述没交代精神力翻倍：{offenders}")

    def test_every_permanent_pill_field_is_consumed(self):
        """永久丹配置里出现的 *_gain / *_multiplier 都必须有落地路径。"""
        consumed = (
            "physical_damage_gain", "magic_damage_gain",
            "physical_defense_gain", "magic_defense_gain",
            "mental_power_gain", "lifespan_gain",
            "max_spiritual_qi_gain", "max_blood_qi_gain",
            "base_attribute_limit_increase", "cultivation_multiplier",
            "death_protection_multiplier",
            "physical_damage_multiplier", "magic_damage_multiplier",
            "physical_defense_multiplier", "magic_defense_multiplier",
        )
        offenders = []
        for name, data in self.config_manager.utility_pills_data.items():
            if data.get("effect_type") != "permanent":
                continue
            for key in data:
                if key.endswith(("_gain", "_multiplier")) and key not in consumed:
                    offenders.append(f"{name}.{key}")
        self.assertEqual(offenders, [], f"永久丹存在无人消费的字段：{offenders}")

    def test_temporary_combat_pills_respect_settlement_cap(self):
        """临时攻防丹的数值与描述都不能超过 +50% 结算上限。

        实战在 ``CombatManager`` 与 ``PillManager`` 两处都把四维临时倍率
        钳制在 +50%，这里从配置源头守住，避免出现「描述 +90%、实际 +50%」。
        """
        import re

        item_rules = _imp("core.item_rules")
        cap = item_rules.TEMPORARY_COMBAT_BONUS_CAP
        keys = (
            "physical_damage_multiplier", "magic_damage_multiplier",
            "physical_defense_multiplier", "magic_defense_multiplier",
        )
        label_to_key = {"物伤": keys[0], "法伤": keys[1], "物防": keys[2], "法防": keys[3]}
        sources = (
            ("utility_pills", self.config_manager.utility_pills_data),
            ("pills", self.config_manager.pills_data),
            ("exp_pills", self.config_manager.exp_pills_data),
        )
        offenders = []
        for source_name, pills in sources:
            for name, data in (pills or {}).items():
                if not isinstance(data, dict) or data.get("effect_type") == "permanent":
                    continue
                for key in keys:
                    value = data.get(key)
                    if value is None:
                        continue
                    if abs(float(value)) > cap + 1e-9:
                        offenders.append(f"{source_name}.{name}.{key}={value}")
                desc = str(data.get("description") or "")
                for label, key in label_to_key.items():
                    for sign, number in re.findall(
                        rf"{label}\s*([+-])\s*(\d+(?:\.\d+)?)\s*%", desc
                    ):
                        if sign == "+" and float(number) / 100.0 > cap + 1e-9:
                            offenders.append(
                                f"{source_name}.{name} 描述{label}+{number}% > 上限{cap:.0%}"
                            )
        self.assertEqual(offenders, [], f"临时攻防丹超出结算上限：{offenders}")


if __name__ == "__main__":
    unittest.main()
