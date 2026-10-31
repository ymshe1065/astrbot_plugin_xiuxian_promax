# tests/test_escort_balance.py
# 押镖/闭关定位分离：押镖修为应约为白板闭关的 1/3，绯烟只加镖银
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


class TestEscortBalance(unittest.TestCase):
    def setUp(self):
        self.EscortManager = _imp("managers.escort_manager").EscortManager
        self.manager = self.EscortManager(None, None, None, None)

    def _white_seclusion_30min(self, level_index: int) -> float:
        # 白板闭关：100 修为/分钟 × 30 分钟 × (1 + 0.1×境界)
        return 100 * 30 * (1 + 0.1 * level_index)

    def test_escort_exp_is_about_one_third_of_seclusion(self):
        for level_index in (3, 5, 9, 16, 25, 35):
            for _ in range(50):
                exp = self.manager.calculate_escort_exp(level_index)
                ratio = exp / self._white_seclusion_30min(level_index)
                self.assertGreaterEqual(ratio, 0.30)
                self.assertLessEqual(ratio, 0.38)

    def test_escort_exp_does_not_scale_with_seclusion_bonuses(self):
        # 公式只依赖境界等级，不接受功法/丹药/仙缘等额外倍率
        import inspect

        params = inspect.signature(self.manager.calculate_escort_exp).parameters
        self.assertEqual(list(params), ["level_index"])

    def test_feiyan_only_buffs_gold(self):
        SocialManager = _imp("managers.social_manager")
        self.assertEqual(SocialManager.SPIRIT_COMPANIONS["绯烟"]["bonus_max"], 0.08)
        self.assertIn("镖银", SocialManager.SPIRIT_COMPANIONS["绯烟"]["perk"])


class TestEscortStartReward(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import os
        import tempfile

        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.EscortManager = _imp("managers.escort_manager").EscortManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        await self.db.create_player(
            self.Player(user_id="u1", level_index=16, hp=1000, gold=100, experience=0)
        )

    async def asyncTearDown(self):
        import os

        await self.db.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    async def test_start_escort_stores_reduced_exp_and_full_gold(self):
        manager = self.EscortManager(self.db, None, None, None)
        player = await self.db.get_player_by_id("u1")
        ok, _, rewards = await manager.start_escort(player)
        self.assertTrue(ok)

        # 元婴期（境界16）：修为 2600~2700；方案A镖银 9000×4.0=36000 ±500
        self.assertGreaterEqual(rewards["exp"], 2600)
        self.assertLessEqual(rewards["exp"], 2700)
        self.assertGreaterEqual(rewards["gold"], 35500)
        self.assertLessEqual(rewards["gold"], 36500)

        cd = await self.db.ext.get_user_cd("u1")
        extra = cd.get_extra_data()
        self.assertEqual(extra["escort_exp"], rewards["exp"])
        self.assertEqual(extra["escort_gold"], rewards["gold"])


if __name__ == "__main__":
    unittest.main()
