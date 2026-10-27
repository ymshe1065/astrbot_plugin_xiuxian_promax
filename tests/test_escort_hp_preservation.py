# tests/test_escort_hp_preservation.py
# 押镖结算不得用旧玩家快照覆盖当前气血
import os
import sys
import tempfile
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


class TestEscortHpPreservation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.UserStatus = _imp("models_extended").UserStatus
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
            self.Player(user_id="u1", level_index=5, hp=12345, gold=100, experience=100)
        )
        await self.db.ext.set_user_busy(
            "u1",
            self.UserStatus.DELIVERING,
            0,
            extra_data={"escort_gold": 500, "escort_exp": 300},
        )

    async def asyncTearDown(self):
        await self.db.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    async def test_finish_escort_does_not_overwrite_newer_hp(self):
        # 模拟自动代理先读取旧快照，随后气血被其他逻辑更新。
        stale_player = await self.db.get_player_by_id("u1")
        await self.db.update_player_fields("u1", hp=54321)

        manager = self.EscortManager(self.db, None, None, None)
        ok, _ = await manager.finish_escort(stale_player)
        self.assertTrue(ok)

        fresh = await self.db.get_player_by_id("u1")
        self.assertEqual(fresh.hp, 54321)
        self.assertEqual(fresh.gold, 600)
        self.assertEqual(fresh.experience, 400)


if __name__ == "__main__":
    unittest.main()
