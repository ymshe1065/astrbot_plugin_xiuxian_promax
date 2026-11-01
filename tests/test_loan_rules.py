# tests/test_loan_rules.py
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


class TestLoanRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = _imp("loan_rules")
        cls.BankManager = _imp("managers.bank_manager").BankManager
        cls.utils = _imp("handlers.utils")
        cls.Player = _imp("models").Player

    def test_loan_limit_scales_with_realm(self):
        cases = {3: 216_000, 16: 864_000, 22: 2_160_000, 35: 9_720_000}
        for level, expected in cases.items():
            self.assertEqual(self.rules.loan_limit(level), expected, level)
        # 低境界保底 10 万（L0~L2 无押镖收入）
        self.assertEqual(self.rules.loan_limit(0), 100_000)

    def test_debt_thresholds_scale_with_daily_income(self):
        t16 = self.rules.debt_thresholds(16)
        income16 = self.rules.daily_income(16)
        self.assertEqual(income16, 1_728_000)
        self.assertEqual(t16["warn"], 864_000)
        self.assertEqual(t16["high"], 1_728_000)
        self.assertEqual(t16["block"], 3_456_000)

    def test_identity_flags_high_realm_not_instant_laolai(self):
        # L16 欠 100 万：只是负债修士，不再直接老赖
        label, blocked = self.utils._loan_identity(1_000_000, 5, 16)
        self.assertEqual(label, "🟡 负债修士")
        self.assertFalse(blocked)
        # L16 欠 350 万：老赖
        label, blocked = self.utils._loan_identity(3_500_000, 5, 16)
        self.assertEqual(label, "🔴 老赖")
        self.assertTrue(blocked)
        # L3 欠 100 万：低境界照样判老赖
        label, blocked = self.utils._loan_identity(1_000_000, 5, 3)
        self.assertEqual(label, "🔴 老赖")
        self.assertTrue(blocked)

    def test_effective_loan_limit_method(self):
        player = self.Player(user_id="u1", user_name="t", level_index=22)
        self.assertEqual(self.BankManager.effective_loan_limit(player), 2_160_000)


if __name__ == "__main__":
    unittest.main()
