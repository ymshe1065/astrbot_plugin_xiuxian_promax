import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestShieldPenalty(unittest.TestCase):
    def setUp(self):
        self.Player = _imp("models").Player

    def test_no_shield(self):
        player = self.Player(user_id="u1")
        self.assertEqual(player.shield_output_multiplier(1000, 4600), 1.0)

    def test_full_overlap(self):
        player = self.Player(user_id="u1", shield_start_time=1000, shield_expire_time=8000)
        self.assertAlmostEqual(player.shield_output_multiplier(2000, 6000), 0.8)

    def test_partial_overlap(self):
        player = self.Player(user_id="u1", shield_start_time=2500, shield_expire_time=5000)
        self.assertAlmostEqual(player.shield_output_multiplier(2000, 4000), 0.85)

    def test_array_master_reduced_penalty(self):
        player = self.Player(user_id="u1", life_skill="阵法师", shield_start_time=1000, shield_expire_time=8000)
        self.assertAlmostEqual(player.shield_output_multiplier(2000, 6000), 0.9)

    def test_expired_before_period(self):
        player = self.Player(user_id="u1", shield_start_time=1000, shield_expire_time=2000)
        self.assertEqual(player.shield_output_multiplier(3000, 4000), 1.0)


if __name__ == "__main__":
    unittest.main()
