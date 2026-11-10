# tests/test_adventure_equipment.py
# 历练装备：品阶按路线封顶 + 掉率系数与材料解耦
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

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

    def debug(self, *a, **k):
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

from astrbot_plugin_xiuxian_promax.config_manager import ConfigManager  # noqa: E402
from astrbot_plugin_xiuxian_promax.managers.adventure_manager import (  # noqa: E402
    AdventureManager,
)
from astrbot_plugin_xiuxian_promax.models import Player  # noqa: E402

CFG = json.loads((ROOT / "config" / "adventure_config.json").read_text(encoding="utf-8"))
ROUTES = {r["key"]: r for r in CFG["routes"]}
TOP_RANKS = {"仙品", "混元先天", "道品"}
LOW_RANKS = {"凡品", "黄品", "玄品", "灵品"}


def make_player(level: int) -> Player:
    p = Player(user_id="sim")
    p.level_index = level
    return p


class TestAdvEquipmentBaseLevel(unittest.TestCase):
    def setUp(self):
        self.cm = ConfigManager(ROOT)
        self.mgr = AdventureManager(None, None, self.cm)

    def test_low_route_caps_high_level_player(self):
        """lv35 刷 lv0 灵谷采药，基准只能是路线门槛 + 余量。"""
        base = self.mgr._equipment_base_level(make_player(35), ROUTES["herb_valley"])
        self.assertEqual(base, 2, f"lv0 路线对满级玩家应封到 lv2，实际 {base}")

    def test_high_route_keeps_player_level(self):
        route = ROUTES["primordial_peak"]  # min_level 27
        base = self.mgr._equipment_base_level(make_player(29), route)
        self.assertEqual(base, 29)

    def test_bonus_is_configurable(self):
        self.mgr.equipment_level_cap_bonus = 0
        base = self.mgr._equipment_base_level(make_player(35), ROUTES["herb_valley"])
        self.assertEqual(base, 0, "余量为 0 时应严格按路线门槛")

    def test_never_exceeds_player_level(self):
        base = self.mgr._equipment_base_level(make_player(20), ROUTES["primordial_peak"])
        self.assertEqual(base, 20, "路线比玩家高时不能反向拔高")


class TestAdvEquipmentScale(unittest.TestCase):
    def test_scale_is_decoupled_from_material(self):
        """材料系数 0.33 不该连带砍装备，装备默认按配置标称值走。"""
        cm = ConfigManager(ROOT)
        mgr = AdventureManager(None, None, cm)
        self.assertAlmostEqual(mgr.material_chance_scale, 0.33, places=2)
        self.assertAlmostEqual(mgr.equipment_chance_scale, 1.0, places=2)
        self.assertNotEqual(mgr.equipment_chance_scale, mgr.material_chance_scale)


class TestAdvEquipmentDropCap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cm = ConfigManager(ROOT)
        cls.mgr = AdventureManager(None, None, cls.cm)
        cls.ranks = {
            name: (cfg.get("rank") or "")
            for name, cfg in {**cls.cm.items_data, **cls.cm.weapons_data}.items()
            if isinstance(cfg, dict)
        }

    def _sample(self, player, route, runs=3000):
        ranks = set()
        taken = 0
        for _ in range(runs):
            for d in self.mgr._roll_drops(player, route, {}, chance_scale=1.0):
                if len(d) >= 3:
                    taken += 1
                    ranks.add(self.ranks.get(d[0], ""))
        return taken, ranks

    def test_low_route_never_drops_top_rank(self):
        """lv35 刷 lv0 灵谷采药：采样里不能出现道品/仙品/混元先天。"""
        taken, ranks = self._sample(make_player(35), ROUTES["herb_valley"])
        self.assertGreater(taken, 20, f"样本太小（{taken} 件），判定无效")
        leaked = ranks & TOP_RANKS
        self.assertEqual(leaked, set(), f"lv0 路线掉了顶级装备：{sorted(leaked)}")
        self.assertTrue(ranks <= LOW_RANKS, f"出现了不该有的品阶：{sorted(ranks)}")

    def test_high_route_still_drops_top_rank(self):
        """高阶路线必须还能掉当前阶，否则封顶改过头了。"""
        taken, ranks = self._sample(make_player(35), ROUTES["primordial_peak"])
        self.assertGreater(taken, 20, f"样本太小（{taken} 件），判定无效")
        self.assertTrue(ranks & TOP_RANKS, f"lv27 路线没掉顶级装备：{sorted(ranks)}")


if __name__ == "__main__":
    unittest.main()
