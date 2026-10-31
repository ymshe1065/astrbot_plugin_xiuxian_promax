"""生活职业更换费用：境界、连续更换与 72 小时时间衰减。"""
import asyncio
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
api_mod.logger = types.SimpleNamespace(
    info=lambda *a, **k: None, warning=lambda *a, **k: None,
    error=lambda *a, **k: None, exception=lambda *a, **k: None,
)
api_mod.AstrBotConfig = dict
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))


def _imp(path):
    return __import__(f"{PKG}.{path}", fromlist=["x"])


class _DB:
    async def update_player(self, player, commit=True):
        return None


def _player(level=17, gold=1_000_000):
    return types.SimpleNamespace(
        level_index=level, gold=gold, life_skill="",
        life_skill_changes=0, life_skill_cost_peak=0,
        life_skill_last_change_at=0,
    )


class TestLifeSkillCost(unittest.TestCase):
    def setUp(self):
        self.mgr = _imp("managers.life_skill_manager").LifeSkillManager(_DB())

    def test_level_and_repeat_cost(self):
        p = _player()
        self.assertEqual(self.mgr.base_change_cost(p), 150_000)
        asyncio.run(self.mgr.choose_skill(p, "符箓师"))
        self.assertEqual(p.gold, 1_000_000)
        self.assertEqual(p.life_skill_changes, 0)
        asyncio.run(self.mgr.choose_skill(p, "炼丹师"))
        self.assertEqual(p.gold, 850_000)
        self.assertEqual(p.life_skill_changes, 1)
        self.assertEqual(p.life_skill_cost_peak, 300_000)
        # 立即连续更换：按当前峰值 300,000，新的峰值为 600,000
        asyncio.run(self.mgr.choose_skill(p, "阵法师"))
        self.assertEqual(p.gold, 550_000)
        self.assertEqual(p.life_skill_changes, 2)
        self.assertEqual(p.life_skill_cost_peak, 600_000)

    def test_72h_decay_returns_to_base(self):
        p = _player()
        p.life_skill = "符箓师"
        p.life_skill_cost_peak = 600_000
        p.life_skill_last_change_at = 1_000_000
        self.assertEqual(self.mgr.get_change_cost(p, 1_000_000 + 86400), 450_000)
        self.assertEqual(self.mgr.get_change_cost(p, 1_000_000 + 259200), 150_000)

    def test_insufficient_gold_does_not_change(self):
        p = _player(gold=0)
        p.life_skill = "符箓师"
        p.life_skill_cost_peak = 300_000
        p.life_skill_last_change_at = 0
        ok, msg = asyncio.run(self.mgr.choose_skill(p, "炼丹师"))
        self.assertFalse(ok)
        self.assertEqual(p.life_skill, "符箓师")
        self.assertEqual(p.gold, 0)


if __name__ == "__main__":
    unittest.main()
