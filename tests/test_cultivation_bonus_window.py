# tests/test_cultivation_bonus_window.py
# 闭关结算：丹药/符箓的修炼加速按「覆盖时段」加权，并在结算文案里显示
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestCultivationBonusWindow(unittest.TestCase):
    """weighted_cultivation_multiplier / format_bonus_note / calculate_cultivation_exp 联动。"""

    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.config_manager = _imp("config_manager").ConfigManager(ROOT)
        cls.cult_module = _imp("core.cultivation_manager")
        cls.models = _imp("models")
        cls.mgr = cls.cult_module.CultivationManager(
            cls.config_manager.game_config, cls.config_manager
        )
        cls.now = int(time.time())

    # ---------- 帮手 ----------
    def _player(self, effects=None):
        player = self.models.Player(user_id="u1", spiritual_root="金木灵根")
        player.level_index = 0
        player.permanent_bonus = "{}"
        player.set_active_pill_effects(effects or [])
        return player

    def _effect(self, name, bonus, start, expire, source="pill", quality="一般"):
        data = {
            "pill_name": name,
            "start_time": start,
            "expiry_time": expire,
            "duration_minutes": max(0, (expire - start) // 60),
            "quality": quality,
            "cultivation_multiplier": bonus,
        }
        if source == "talisman":
            data.update({"talisman_name": name, "source": "talisman", "subtype": "talisman"})
        return data

    def _window(self, effects, minutes):
        begin = self.now - minutes * 60
        return begin, self.now

    # ---------- 加权 ----------
    def test_no_effect_keeps_multiplier(self):
        bonus = self.cult_module.weighted_cultivation_multiplier([], self.now - 3600, self.now)
        self.assertEqual(bonus["multiplier"], 1.0)
        self.assertEqual(bonus["segments"], [])

    def test_full_coverage_applies_whole_bonus(self):
        eff = [self._effect("修炼加速丹", 0.5, self.now - 7200, self.now + 7200, quality="极佳")]
        bonus = self.cult_module.weighted_cultivation_multiplier(eff, self.now - 3600, self.now)
        self.assertAlmostEqual(bonus["multiplier"], 1.5, places=3)
        self.assertEqual(bonus["covered_minutes"], 60)

    def test_partial_coverage_is_weighted(self):
        # 闭关 10 小时，丹药只覆盖前 1 小时（+50%）
        begin, end = self._window([], 600)
        eff = [self._effect("修炼加速丹", 0.5, begin, begin + 3600)]
        bonus = self.cult_module.weighted_cultivation_multiplier(eff, begin, end)
        self.assertAlmostEqual(bonus["multiplier"], 1.05, places=3)
        self.assertEqual(bonus["covered_minutes"], 60)
        self.assertEqual(bonus["total_minutes"], 600)

    def test_expired_before_seclusion_is_ignored(self):
        begin, end = self._window([], 600)
        eff = [self._effect("修炼加速丹", 0.5, begin - 7200, begin - 60)]
        bonus = self.cult_module.weighted_cultivation_multiplier(eff, begin, end)
        self.assertEqual(bonus["multiplier"], 1.0)
        self.assertEqual(bonus["segments"], [])
        # 但文案要告诉玩家「没覆盖到」，而不是默默吞掉
        self.assertIn("未覆盖闭关时段", self.cult_module.format_bonus_note(bonus, 0.0, eff))

    def test_talisman_counted_same_as_pill(self):
        begin, end = self._window([], 60)
        eff = [self._effect("聚灵符", 0.2, begin, end, source="talisman", quality="上品")]
        bonus = self.cult_module.weighted_cultivation_multiplier(eff, begin, end)
        self.assertAlmostEqual(bonus["multiplier"], 1.2, places=3)
        self.assertEqual(bonus["segments"][0]["source"], "talisman")

    def test_pill_and_talisman_stack_additively(self):
        begin, end = self._window([], 60)
        eff = [
            self._effect("修炼加速丹", 0.5, begin, end, quality="极佳"),
            self._effect("聚灵符", 0.2, begin, end, source="talisman"),
        ]
        bonus = self.cult_module.weighted_cultivation_multiplier(eff, begin, end)
        self.assertAlmostEqual(bonus["multiplier"], 1.7, places=3)

    def test_multi_segment_timeline(self):
        # 前 30 分钟 +100%，中间 30 分钟无加成，最后 30 分钟 +50%
        begin, end = self._window([], 90)
        eff = [
            self._effect("甲丹", 1.0, begin, begin + 1800),
            self._effect("乙符", 0.5, begin + 3600, end, source="talisman"),
        ]
        bonus = self.cult_module.weighted_cultivation_multiplier(eff, begin, end)
        expected = (30 * 2.0 + 30 * 1.0 + 30 * 1.5) / 90
        self.assertAlmostEqual(bonus["multiplier"], expected, places=3)

    def test_cap_limits_single_segment(self):
        begin, end = self._window([], 60)
        eff = [self._effect("超模丹", 5.0, begin, end)]
        bonus = self.cult_module.weighted_cultivation_multiplier(eff, begin, end, cap=3.0)
        self.assertAlmostEqual(bonus["multiplier"], 3.0, places=3)
        self.assertTrue(bonus["cap_hit"])

    def test_negative_penalty_is_weighted(self):
        begin, end = self._window([], 60)
        eff = [self._effect("透支丹", -0.3, begin, end)]
        bonus = self.cult_module.weighted_cultivation_multiplier(eff, begin, end)
        self.assertAlmostEqual(bonus["multiplier"], 0.7, places=3)

    def test_effect_without_expiry_covers_whole_window(self):
        begin, end = self._window([], 120)
        eff = [self._effect("常驻丹", 0.25, begin, 0)]
        bonus = self.cult_module.weighted_cultivation_multiplier(eff, begin, end)
        self.assertAlmostEqual(bonus["multiplier"], 1.25, places=3)
        self.assertEqual(bonus["covered_minutes"], 120)

    # ---------- 数值落地 ----------
    def test_exp_reflects_weighted_multiplier(self):
        player = self._player()
        # 放在中期境界上取整误差可忽略（练气期需求仅 500，取整会把 1.05 抬成 1.08）
        player.level_index = 20
        base = self.mgr.calculate_cultivation_exp(player, 600, 0.0, {"cultivation_speed": 1.0})

        begin, _end = self._window([], 600)
        eff = [self._effect("修炼加速丹", 0.5, begin, begin + 3600)]
        bonus = self.cult_module.weighted_cultivation_multiplier(
            eff, begin, begin + 600 * 60
        )
        boosted = self.mgr.calculate_cultivation_exp(
            player, 600, 0.0, {"cultivation_speed": bonus["multiplier"]}
        )
        # 只覆盖 1/10 时长，整体只该多 5%
        self.assertAlmostEqual(boosted / base, 1.05, places=2)

    # ---------- 文案 ----------
    def test_note_lists_items_and_coverage(self):
        begin, end = self._window([], 120)
        eff = [
            self._effect("修炼加速丹", 0.5, begin, end, quality="极佳"),
            self._effect("聚灵符", 0.2, begin, end, source="talisman", quality="上品"),
        ]
        bonus = self.cult_module.weighted_cultivation_multiplier(eff, begin, end)
        note = self.cult_module.format_bonus_note(bonus, 0.0, eff)
        self.assertIn("丹符加成", note)
        self.assertIn("修炼加速丹·极佳 +50%", note)
        self.assertIn("聚灵符·上品 +20%", note)
        self.assertIn("×1.70", note)
        self.assertIn("120/120", note)

    def test_note_shows_permanent_bonus_only(self):
        note = self.cult_module.format_bonus_note({"segments": [], "multiplier": 1.0}, 0.25, [])
        self.assertIn("永久 +25%", note)

    def test_note_empty_when_nothing_used(self):
        self.assertEqual(self.cult_module.format_bonus_note({"segments": []}, 0.0, []), "")


if __name__ == "__main__":
    unittest.main()
