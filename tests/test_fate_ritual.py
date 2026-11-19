# tests/test_fate_ritual.py
# 仙缘祭祀：灵石不再保底换点，而是按日期 / 气运 / 功德小概率成功
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


rules = _imp("core.reincarnation_rules")
Player = _imp("models").Player


def _player(luck=0, karma=0):
    p = Player(user_id="u1")
    p.luck = luck
    p.karma = karma
    p.gold = rules.GOLD_PER_FATE_POINT * 100
    return p


class TestRitualDateBonus(unittest.TestCase):
    def test_festival_gets_the_biggest_date_bonus(self):
        bonus, label = rules.fate_ritual_date_bonus(datetime(2026, 8, 15))
        self.assertAlmostEqual(bonus, rules.FATE_RITUAL_FESTIVAL_BONUS)
        self.assertIn("太阴盛会", label)
        self.assertIn("吉日", label)

    def test_lunar_first_and_fifteenth_get_small_bonus(self):
        for day in (1, 15):
            bonus, label = rules.fate_ritual_date_bonus(datetime(2026, 8, day))
            # 08-15 是节日，优先按节日算；这里只要求 01/15 至少有朔望加成
            self.assertGreaterEqual(bonus, rules.FATE_RITUAL_LUNAR_BONUS)
            self.assertTrue(label)
        bonus, label = rules.fate_ritual_date_bonus(datetime(2026, 9, 1))
        self.assertAlmostEqual(bonus, rules.FATE_RITUAL_LUNAR_BONUS)
        self.assertIn("初一", label)

    def test_ordinary_day_has_no_date_bonus(self):
        bonus, label = rules.fate_ritual_date_bonus(datetime(2026, 8, 2))
        self.assertEqual(bonus, 0.0)
        self.assertEqual(label, "")

    def test_bad_date_object_is_soft(self):
        self.assertEqual(rules.fate_ritual_date_bonus(object()), (0.0, ""))


class TestRitualRate(unittest.TestCase):
    def test_base_rate_is_small(self):
        info = rules.fate_ritual_rate(_player(), today=datetime(2026, 8, 2))
        self.assertAlmostEqual(info["rate"], rules.FATE_RITUAL_BASE_RATE)
        self.assertAlmostEqual(info["base"], rules.FATE_RITUAL_BASE_RATE)

    def test_luck_and_karma_raise_the_rate(self):
        plain = rules.fate_ritual_rate(_player(), today=datetime(2026, 8, 2))
        boosted = rules.fate_ritual_rate(
            _player(luck=10, karma=10), today=datetime(2026, 8, 2)
        )
        self.assertGreater(boosted["rate"], plain["rate"])
        self.assertAlmostEqual(boosted["luck_bonus"], 10 * rules.FATE_RITUAL_LUCK_PER_POINT)
        self.assertAlmostEqual(boosted["karma_bonus"], 10 * rules.FATE_RITUAL_KARMA_PER_POINT)
        self.assertIn("气运 10", rules.format_ritual_rate(boosted))
        self.assertIn("功德 10", rules.format_ritual_rate(boosted))
        evil = rules.fate_ritual_rate(_player(karma=-20), today=datetime(2026, 8, 2))
        self.assertIn("业力 20", rules.format_ritual_rate(evil))

    def test_negative_karma_lowers_the_rate(self):
        plain = rules.fate_ritual_rate(_player(), today=datetime(2026, 8, 2))
        evil = rules.fate_ritual_rate(
            _player(karma=-1000), today=datetime(2026, 8, 2)
        )
        self.assertLess(evil["rate"], plain["rate"])
        self.assertGreaterEqual(evil["karma_bonus"], rules.FATE_RITUAL_KARMA_FLOOR)
        self.assertGreaterEqual(evil["rate"], rules.FATE_RITUAL_MIN_RATE)

    def test_rate_is_capped(self):
        info = rules.fate_ritual_rate(
            _player(luck=9999, karma=9999), today=datetime(2026, 8, 15)
        )
        self.assertAlmostEqual(info["rate"], rules.FATE_RITUAL_MAX_RATE)

    def test_festival_and_lunar_are_reflected_in_rate(self):
        plain = rules.fate_ritual_rate(_player(), today=datetime(2026, 8, 2))
        lunar = rules.fate_ritual_rate(_player(), today=datetime(2026, 9, 1))
        festival = rules.fate_ritual_rate(_player(), today=datetime(2026, 8, 15))
        self.assertGreater(lunar["rate"], plain["rate"])
        self.assertGreater(festival["rate"], lunar["rate"])


class TestPerformRitual(unittest.TestCase):
    def test_each_offering_rolls_independently(self):
        player = _player()
        rolls = [0.1, 0.9, 0.4, 0.5]
        with mock.patch.multiple(
            rules, FATE_RITUAL_BASE_RATE=0.5, FATE_RITUAL_MAX_RATE=0.5
        ), mock.patch.object(rules.random, "random", side_effect=rolls):
            result = rules.perform_fate_ritual(player, 4, today=datetime(2026, 8, 2))
        self.assertEqual(result["times"], 4)
        self.assertEqual(result["success"], 2)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["cost"], 4 * rules.GOLD_PER_FATE_POINT)

    def test_perform_does_not_touch_gold_or_points(self):
        """掷骰函数只算结果，扣钱与记点由调用方统一落库。"""
        player = _player()
        player.gold = 123
        player.fate_points = 7
        with mock.patch.object(rules.random, "random", return_value=0.0):
            rules.perform_fate_ritual(player, 3, today=datetime(2026, 8, 2))
        self.assertEqual(player.gold, 123)
        self.assertEqual(player.fate_points, 7)


if __name__ == "__main__":
    unittest.main()
