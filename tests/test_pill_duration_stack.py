# tests/test_pill_duration_stack.py
# 临时丹药重复服用：时长叠加而非覆盖
import json
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


class _FakeDB:
    def __init__(self, player):
        self.player = player

    async def get_player_by_id(self, user_id):
        return self.player if user_id == self.player.user_id else None

    async def update_player(self, player, commit=True):
        self.player = player
        return None


class TestTemporaryPillStacking(unittest.IsolatedAsyncioTestCase):
    """同名临时丹药：剩余时长累加，效果取更强的一份。"""

    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.config_manager = _imp("config_manager").ConfigManager(ROOT)
        cls.pill_module = _imp("core.pill_manager")
        cls.models = _imp("models")

    def _player(self, inventory: dict, level_index: int = 20):
        player = self.models.Player(user_id="u1", level_index=level_index)
        player.pills_inventory = json.dumps(inventory, ensure_ascii=False)
        return player

    async def _pm(self, player):
        return self.pill_module.PillManager(_FakeDB(player), self.config_manager)

    def _remaining_minutes(self, player, name: str) -> int:
        for effect in player.get_active_pill_effects():
            if effect.get("pill_name") == name:
                return int((int(effect["expiry_time"]) - int(time.time())) // 60)
        return -1

    def _effect(self, player, name: str):
        for effect in player.get_active_pill_effects():
            if effect.get("pill_name") == name:
                return effect
        return None

    async def test_single_use_sets_one_duration(self):
        player = self._player({"修炼加速丹|一般": 5})
        pm = await self._pm(player)
        ok, msg = await pm.use_pill(player, "修炼加速丹")
        self.assertTrue(ok, msg)
        self.assertIn("持续时间：60分钟", msg)
        self.assertGreaterEqual(self._remaining_minutes(player, "修炼加速丹"), 59)
        self.assertEqual(len(player.get_active_pill_effects()), 1)

    async def test_batch_use_accumulates_duration(self):
        """30 颗应攒出 30 小时，而不是只剩最后一颗的 60 分钟。"""
        player = self._player({"修炼加速丹|一般": 30})
        pm = await self._pm(player)
        ok, used, msg = await pm.use_pill_batch(player, "修炼加速丹", None, 30)
        self.assertTrue(ok, msg)
        self.assertEqual(used, 30)
        # 只剩一条同名效果
        effects = [e for e in player.get_active_pill_effects()
                   if e.get("pill_name") == "修炼加速丹"]
        self.assertEqual(len(effects), 1)
        remaining = self._remaining_minutes(player, "修炼加速丹")
        self.assertGreaterEqual(remaining, 1795)   # 30*60 - 少量秒级误差
        self.assertLessEqual(remaining, 1800)
        self.assertEqual(player.get_pills_inventory().get("修炼加速丹|一般"), None)

    async def test_batch_message_reports_total_remaining(self):
        player = self._player({"修炼加速丹|一般": 5})
        pm = await self._pm(player)
        ok, _used, msg = await pm.use_pill_batch(player, "修炼加速丹", None, 5)
        self.assertTrue(ok, msg)
        self.assertIn("持续时间：修炼加速丹 剩余 5小时", msg)

    async def test_second_use_extends_instead_of_resetting(self):
        player = self._player({"修炼加速丹|一般": 3})
        pm = await self._pm(player)
        await pm.use_pill(player, "修炼加速丹")
        first = self._remaining_minutes(player, "修炼加速丹")
        await pm.use_pill(player, "修炼加速丹")
        second = self._remaining_minutes(player, "修炼加速丹")
        self.assertGreaterEqual(second, first + 59)
        self.assertEqual(len(player.get_active_pill_effects()), 1)

    async def test_higher_quality_effect_wins(self):
        """先吃一般再吃大师，效果抬到大师的档位，不吃品质倒挂。"""
        player = self._player({"修炼加速丹|一般": 1, "修炼加速丹|大师": 1})
        pm = await self._pm(player)
        ok, msg = await pm.use_pill(player, "修炼加速丹", "一般")
        self.assertTrue(ok, msg)
        normal = float(self._effect(player, "修炼加速丹")["cultivation_multiplier"])
        ok, msg = await pm.use_pill(player, "修炼加速丹", "大师")
        self.assertTrue(ok, msg)
        effect = self._effect(player, "修炼加速丹")
        self.assertGreater(float(effect["cultivation_multiplier"]), normal)
        self.assertEqual(effect["quality"], "大师")

    async def test_lower_quality_does_not_overwrite_higher(self):
        """先吃大师再吃一般：时长继续加，效果不被打回低档。"""
        player = self._player({"修炼加速丹|一般": 1, "修炼加速丹|大师": 1})
        pm = await self._pm(player)
        await pm.use_pill(player, "修炼加速丹", "大师")
        high = float(self._effect(player, "修炼加速丹")["cultivation_multiplier"])
        high_remaining = self._remaining_minutes(player, "修炼加速丹")
        await pm.use_pill(player, "修炼加速丹", "一般")
        effect = self._effect(player, "修炼加速丹")
        self.assertAlmostEqual(float(effect["cultivation_multiplier"]), high, places=6)
        self.assertEqual(effect["quality"], "大师")
        self.assertGreaterEqual(self._remaining_minutes(player, "修炼加速丹"), high_remaining + 59)

    async def test_expired_effect_restarts_fresh(self):
        """已过期的同名效果不再续时，重新起算一层。"""
        player = self._player({"修炼加速丹|一般": 2})
        pm = await self._pm(player)
        await pm.use_pill(player, "修炼加速丹")
        effects = player.get_active_pill_effects()
        effects[0]["expiry_time"] = int(time.time()) - 600
        effects[0]["duration_minutes"] = 60
        player.set_active_pill_effects(effects)

        await pm.use_pill(player, "修炼加速丹")
        effect = self._effect(player, "修炼加速丹")
        self.assertEqual(effect["duration_minutes"], 60)
        remaining = self._remaining_minutes(player, "修炼加速丹")
        self.assertGreaterEqual(remaining, 59)
        self.assertLessEqual(remaining, 60)

    async def test_different_pills_keep_separate_layers(self):
        """不同丹药各占一层，互不覆盖也不互相续时。"""
        player = self._player({"修炼加速丹|一般": 2, "灵气加速丹|一般": 2}, level_index=20)
        pm = await self._pm(player)
        ok, msg = await pm.use_pill(player, "修炼加速丹")
        self.assertTrue(ok, msg)
        ok, msg = await pm.use_pill(player, "灵气加速丹")
        self.assertTrue(ok, msg)
        names = {e.get("pill_name") for e in player.get_active_pill_effects()}
        self.assertEqual(names, {"修炼加速丹", "灵气加速丹"})
        # 灵气加速丹配的是 120 分钟，没被 60 分钟的叠时逻辑影响
        self.assertGreaterEqual(self._remaining_minutes(player, "灵气加速丹"), 119)

    async def test_start_time_and_tick_kept_on_extend(self):
        """续时不动 start_time / last_tick_time，避免每分钟结算被重置。"""
        player = self._player({"修炼加速丹|一般": 2})
        pm = await self._pm(player)
        await pm.use_pill(player, "修炼加速丹")
        before = self._effect(player, "修炼加速丹")
        start_time = before["start_time"]
        last_tick = before["last_tick_time"]
        await pm.use_pill(player, "修炼加速丹")
        after = self._effect(player, "修炼加速丹")
        self.assertEqual(after["start_time"], start_time)
        self.assertEqual(after["last_tick_time"], last_tick)
        self.assertEqual(after["duration_minutes"], 120)


if __name__ == "__main__":
    unittest.main()
