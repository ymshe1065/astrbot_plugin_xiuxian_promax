# 气运系统回归测试
import asyncio
import json
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


class TestLuckSystem(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = _imp("data")
        cls.ConfigManager = _imp("config_manager").ConfigManager
        cls.Player = _imp("models").Player
        cls.LuckManager = _imp("managers.luck_manager").LuckManager
        cls.TitleManager = _imp("managers.title_manager").TitleManager
        cls.config_manager = cls.ConfigManager(ROOT)

    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = self.data.DataBase(self.db_path)
        await self.db.connect()
        await self.data.MigrationManager(self.db.conn, self.config_manager).migrate()

    async def asyncTearDown(self):
        await self.db.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_breakdown_sums_sources(self):
        player = self.Player(user_id="1", luck=3)
        player.permanent_bonus = json.dumps({
            "titles": {"气运之子": 1},
            "affixes": [{"key": "luck", "value": 2}],
        })
        player.equipment_instances = json.dumps([
            {"id": 1, "affixes": [{"key": "luck", "value": 4}]}
        ])
        player.equipped_instances = json.dumps({"weapon": 1})
        player.active_pill_effects = json.dumps([
            {"source": "talisman", "expiry_time": 9999999999, "luck_bonus": 5}
        ])
        parts = self.LuckManager.get_breakdown(player)
        self.assertEqual(parts["base"], 3)
        self.assertEqual(parts["title"], 50)
        self.assertEqual(parts["affix"], 6)
        self.assertEqual(parts["talisman"], 5)
        self.assertEqual(parts["total"], 64)

    def test_expired_talisman_ignored(self):
        player = self.Player(user_id="1", luck=1)
        player.active_pill_effects = json.dumps([
            {"source": "talisman", "expiry_time": 1, "luck_bonus": 5}
        ])
        self.assertEqual(self.LuckManager.get_total_luck(player), 1)

    def test_gongde_wuliang_luck_not_karma(self):
        """【天品】功德无量只加气运，绝不能写入功德（karma）。"""
        player = self.Player(user_id="karma-luck", karma=50, luck=3)
        player.permanent_bonus = json.dumps({"titles": {"功德无量": 1}})

        parts = self.LuckManager.get_breakdown(player)
        self.assertEqual(parts["title"], 20)
        self.assertEqual(parts["total"], 23)
        self.assertEqual(player.karma, 50)

        title_mgr = self.TitleManager(None)
        self.assertEqual(title_mgr.get_active_bonus(player).get("luck"), 20)
        self.assertNotIn("karma", title_mgr.get_active_bonus(player))
        self.assertIn("气运+20", title_mgr.bonus_summary(player))

    def test_same_tier_title_priority_consistent(self):
        """同品阶称号：佩戴优先，否则最近获得；气运/修为/战斗共用同一口径。"""
        title_mgr = self.TitleManager(None)
        player = self.Player(user_id="same-tier")
        player.permanent_bonus = json.dumps(
            {"titles": {"天下第一善": 100.0, "积善之家": 200.0}}
        )

        # 未佩戴时：同品阶取最近获得的「积善之家」（气运+5，无修为加成）
        self.assertEqual(title_mgr.get_active_title(player)["name"], "积善之家")
        self.assertEqual(self.LuckManager.get_title_luck(player), 5)
        self.assertAlmostEqual(title_mgr.get_exp_bonus(player), 0.0)

        # 佩戴「天下第一善」后：同品阶优先佩戴，气运/修为/战斗同步切换
        player.title = "天下第一善"
        self.assertEqual(title_mgr.get_active_title(player)["name"], "天下第一善")
        self.assertEqual(self.LuckManager.get_title_luck(player), 8)
        self.assertAlmostEqual(title_mgr.get_exp_bonus(player), 0.06)
        self.assertEqual(title_mgr.get_active_bonus(player).get("luck"), 8)
        self.assertEqual(title_mgr.get_active_bonus(player).get("exp"), 0.06)

        # 更高品阶始终优先于佩戴的低品阶称号
        player.permanent_bonus = json.dumps(
            {"titles": {"积善之家": 200.0, "功德无量": 50.0}}
        )
        self.assertEqual(title_mgr.get_active_title(player)["name"], "功德无量")
        self.assertEqual(self.LuckManager.get_title_luck(player), 20)

    def test_title_stats_formatting(self):
        """吸血/会心为整数百分点，修为/灵石为小数，展示不能放大 100 倍。"""
        title_mgr = self.TitleManager(None)
        text = title_mgr._format_stats({"lifesteal": 5, "crit": 8, "exp": 0.2})
        self.assertIn("吸血+5%", text)
        self.assertIn("会心+8%", text)
        self.assertIn("修为+20%", text)
        self.assertNotIn("500%", text)
        self.assertNotIn("800%", text)

    async def test_luck_persists(self):
        player = self.Player(user_id="luck-test", luck=7)
        await self.db.create_player(player)
        loaded = await self.db.get_player_by_id("luck-test")
        self.assertEqual(loaded.luck, 7)

        loaded.luck = 9
        await self.db.update_player(loaded)
        reloaded = await self.db.get_player_by_id("luck-test")
        self.assertEqual(reloaded.luck, 9)


if __name__ == "__main__":
    unittest.main()
