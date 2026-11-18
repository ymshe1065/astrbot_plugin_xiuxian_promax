# tests/test_bounty_daily.py
# 每日通缉榜：榜单稳定、境界覆盖、每日挑战次数上限
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()

import aiosqlite


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class _FakeDB:
    def __init__(self, conn):
        self.conn = conn


class TestBountyDaily(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.ConfigManager = _imp("config_manager").ConfigManager
        cls.BountyManager = _imp("managers.bounty_manager").BountyManager
        cls.Player = _imp("models").Player
        cls.config_manager = cls.ConfigManager(ROOT)

    def _manager(self, db=None):
        return self.BountyManager(db, None, self.config_manager)

    def test_daily_board_is_stable_and_covers_realms(self):
        player = self.Player(user_id="u1", level_index=10)
        first = self._manager()._generate_daily_board(player)
        second = self._manager()._generate_daily_board(player)

        self.assertEqual(len(first), self._manager().BOARD_SIZE)
        self.assertEqual(
            [(b["id"], b["name"], b["level_index"]) for b in first],
            [(b["id"], b["name"], b["level_index"]) for b in second],
            "同一天同一玩家的通缉榜应稳定不变",
        )
        self.assertEqual(len({b["id"] for b in first}), len(first))
        levels = [b["level_index"] for b in first]
        self.assertLessEqual(min(levels), player.level_index)
        self.assertGreaterEqual(max(levels), player.level_index)
        for bounty in first:
            self.assertTrue(bounty["name"])
            self.assertTrue(bounty["realm_name"])
            self.assertGreater(bounty["max_hp"], 0)
            self.assertGreater(bounty["atk"], 0)
            self.assertGreater(bounty["reward"]["stone"], 0)
            self.assertGreater(bounty["reward"]["exp"], 0)
            self.assertIn(bounty["drop_table"], {"low", "mid", "high", "legend"})

    async def test_daily_attempts_capped_at_three(self):
        conn = await aiosqlite.connect(":memory:")
        self.addAsyncCleanup(conn.close)
        manager = self._manager(_FakeDB(conn))
        player = self.Player(user_id="u1")

        results = [await manager._consume_daily_attempt("u1") for _ in range(4)]
        self.assertEqual(results, [True, True, True, False])
        self.assertEqual(
            await manager.get_daily_attempts(player), manager.MAX_DAILY_ATTEMPTS
        )
        self.assertEqual(await manager.remaining_attempts(player), 0)


if __name__ == "__main__":
    unittest.main()
