# tests/test_escort_bonus.py
import copy
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub
from tests._cd_stub import SlotCdStubMixin

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class _FakeCD:
    def __init__(self, status, extra=None, scheduled=0):
        self.type = status
        self.scheduled_time = scheduled
        self._extra = copy.deepcopy(extra or {})

    def get_extra_data(self):
        return copy.deepcopy(self._extra)


class _FakeExt(SlotCdStubMixin):
    def __init__(self):
        self.cd = None

    async def get_user_cd(self, user_id):
        return self.cd

    async def create_user_cd(self, user_id):
        self.cd = _FakeCD(0)

    async def set_user_busy(self, user_id, status, when, extra_data=None):
        self.cd = _FakeCD(status, extra_data or {}, when)

    async def set_user_free(self, user_id, commit=True, cd_type=None):
        self.cd = None


class _FakeDB:
    def __init__(self):
        self.ext = _FakeExt()

    async def update_player_fields(self, *a, **k):
        return None


class _FakeCM:
    def __init__(self, items):
        self.items_data = items

    def get_item_config(self, name):
        return self.items_data.get(name)


class TestEscortBonuses(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.escort_module = _imp("managers.escort_manager")
        cls.EscortManager = cls.escort_module.EscortManager
        cls.Player = _imp("models").Player
        cls.UserStatus = _imp("models_extended").UserStatus
        cls.TitleManager = _imp("managers.title_manager").TitleManager
        cls.PetManager = _imp("managers.pet_manager").PetManager

    def _manager(self, items=None):
        cm = _FakeCM(items or {
            "测试功法": {
                "name": "测试功法",
                "type": "功法",
                "exp_multiplier": 0.2,
                "life_effects": {"gold_bonus": 0.5},
            }
        })
        db = _FakeDB()
        return self.EscortManager(db, None, None, cm), db

    async def test_start_applies_bonuses_so_prompt_equals_payout(self):
        from unittest import mock

        mgr, db = self._manager()
        player = self.Player(user_id="u1", user_name="t", level_index=3, gold=0, experience=0, karma=0)
        player.techniques = '["测试功法"]'
        db.ext.cd = _FakeCD(self.UserStatus.IDLE)

        async def fake_pet(self, user_id, key):
            return 0.2 if key == "gold_bonus" else 0.0

        with mock.patch.object(self.TitleManager, "get_active_bonus", lambda self, p: {"gold_pct": 0.1}), \
             mock.patch.object(self.TitleManager, "get_exp_bonus", staticmethod(lambda p: 0.1)), \
             mock.patch.object(self.PetManager, "get_life_bonus", fake_pet), \
             mock.patch.object(self.escort_module.random, "randint", lambda a, b: 0):
            ok, msg, info = await mgr.start_escort(player)
            base = 9000  # L3 基础镖银
            expected_gold = int(int(int(base * 1.1) * 1.5) * 1.2)
            base_exp = mgr.calculate_escort_exp(3)
            expected_exp = int(int(base_exp * 1.1) * 1.2)

        self.assertTrue(ok, msg)
        self.assertEqual(info["gold"], expected_gold)
        self.assertEqual(info["exp"], expected_exp)
        # 开镖提示与结算入账一致
        self.assertIn(f"{expected_gold:,}", msg)
        self.assertIn(f"{expected_exp:,}", msg)
        stored = db.ext.cd.get_extra_data()
        self.assertEqual(stored["escort_gold"], expected_gold)
        self.assertEqual(stored["escort_exp"], expected_exp)
        self.assertEqual(stored.get("bonus_applied"), 1)

    async def test_finish_pays_stored_amount_without_double_bonus(self):
        mgr, db = self._manager()
        player = self.Player(user_id="u1", user_name="t", level_index=3, gold=0, experience=0, karma=0)
        player.techniques = '["测试功法"]'
        db.ext.cd = _FakeCD(
            self.UserStatus.DELIVERING,
            {"escort_gold": 17820, "escort_exp": 1320, "bonus_applied": 1},
        )
        ok, msg = await mgr.finish_escort(player)
        self.assertTrue(ok, msg)
        self.assertEqual(player.gold, 17820)
        self.assertEqual(player.experience, 1320)

    async def test_legacy_escort_without_flag_gets_bonus_at_finish(self):
        from unittest import mock

        mgr, db = self._manager()
        player = self.Player(user_id="u1", user_name="t", level_index=3, gold=0, experience=0, karma=0)
        player.techniques = '["测试功法"]'
        legacy_exp = mgr.calculate_escort_exp(3)
        db.ext.cd = _FakeCD(
            self.UserStatus.DELIVERING,
            {"escort_gold": 9000, "escort_exp": legacy_exp},
        )

        async def fake_pet(self, user_id, key):
            return 0.2 if key == "gold_bonus" else 0.0

        with mock.patch.object(self.TitleManager, "get_active_bonus", lambda self, p: {"gold_pct": 0.1}), \
             mock.patch.object(self.TitleManager, "get_exp_bonus", staticmethod(lambda p: 0.1)), \
             mock.patch.object(self.PetManager, "get_life_bonus", fake_pet):
            ok, msg = await mgr.finish_escort(player)

        self.assertTrue(ok, msg)
        self.assertEqual(player.gold, 17820)
        self.assertEqual(player.experience, int(int(legacy_exp * 1.1) * 1.2))


if __name__ == "__main__":
    unittest.main()
