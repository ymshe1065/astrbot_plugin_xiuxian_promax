# tests/test_rift_equipment.py
# 秘境装备品阶封顶：低门槛本不许掉当前阶装备
import asyncio
import json
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

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


ConfigManager = _imp("config_manager").ConfigManager
RiftManager = _imp("managers.rift_manager").RiftManager
Player = _imp("models").Player
Rift = _imp("models_extended").Rift

CFG = json.loads((ROOT / "config" / "rift_config.json").read_text(encoding="utf-8"))
RIFTS = {r["key"]: r for r in CFG["rifts"]}
TOP_RANKS = {"仙品", "混元先天"}
LOW_RANKS = {"凡品", "黄品", "玄品"}


def make_rift(key: str, tier: int) -> Rift:
    rc = RIFTS[key]
    diff = rc["difficulties"][tier - 1]
    return Rift(
        rift_id=0,
        rift_name=rc["name"],
        rift_level=int(diff["required_level"]),
        required_level=int(diff["required_level"]),
        rewards=json.dumps(diff, ensure_ascii=False),
        rift_key=rc["key"],
        product=rc.get("product", ""),
        difficulty=int(diff["tier"]),
        duration=int(diff["duration"]),
    )


def make_player(level: int) -> Player:
    p = Player(user_id="sim")
    p.level_index = level
    return p


class TestEquipmentBaseLevel(unittest.TestCase):
    def setUp(self):
        self.cm = ConfigManager(ROOT)
        self.mgr = RiftManager(None, self.cm)

    def test_low_rift_caps_high_level_player(self):
        """lv35 刷 lv0 秘境，基准只能是门槛 + 余量，不能是 35。"""
        rift = make_rift("qingyun", 1)  # 门槛 lv0
        base = self.mgr._equipment_base_level(rift, make_player(35))
        self.assertEqual(base, 2, f"lv0 秘境对满级玩家应封到 lv2，实际 {base}")

    def test_high_rift_keeps_player_level(self):
        rift = make_rift("hundun", 3)  # 门槛 lv35
        base = self.mgr._equipment_base_level(rift, make_player(35))
        self.assertEqual(base, 35)

    def test_bonus_is_configurable(self):
        rift = make_rift("qingyun", 1)
        self.mgr.config["equipment_level_cap_bonus"] = 0
        base = self.mgr._equipment_base_level(rift, make_player(35))
        self.assertEqual(base, 0, "余量为 0 时应严格按秘境门槛")

    def test_never_exceeds_player_level(self):
        """低境界玩家进高档本（理论上进不去，兜底也不能反向拔高）。"""
        rift = make_rift("hundun", 3)
        base = self.mgr._equipment_base_level(rift, make_player(20))
        self.assertEqual(base, 20)


class TestEquipmentDropCap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cm = ConfigManager(ROOT)
        cls.mgr = RiftManager(None, cls.cm)
        cls.ranks = {
            name: (cfg.get("rank") or "")
            for name, cfg in {**cls.cm.items_data, **cls.cm.weapons_data}.items()
            if isinstance(cfg, dict)
        }

    def _sample(self, rift, player, runs=2500):
        ranks = set()
        taken = 0
        for _ in range(runs):
            drops = asyncio.run(self.mgr._roll_rift_drops(player, rift, 0, chance_scale=1.0))
            for d in drops:
                if len(d) >= 3:
                    taken += 1
                    ranks.add(self.ranks.get(d[0], ""))
        return taken, ranks

    def test_top_rank_never_drops_from_low_rift(self):
        """lv35 刷 lv0 秘境：采样里不能出现仙品/混元先天。"""
        taken, ranks = self._sample(make_rift("qingyun", 1), make_player(35))
        self.assertGreater(taken, 20, f"样本太小（{taken} 件），判定无效")
        leaked = ranks & TOP_RANKS
        self.assertEqual(leaked, set(), f"lv0 秘境掉了顶级装备：{sorted(leaked)}")
        self.assertTrue(ranks <= LOW_RANKS | {"灵品", "地品"}, f"出现了不该有的品阶：{sorted(ranks)}")

    def test_high_rift_still_drops_top_rank(self):
        """高阶本必须还能掉当前阶，否则封顶改过头了。"""
        taken, ranks = self._sample(make_rift("hundun", 3), make_player(35))
        self.assertGreater(taken, 20, f"样本太小（{taken} 件），判定无效")
        self.assertTrue(ranks & TOP_RANKS, f"lv35 秘境没掉顶级装备：{sorted(ranks)}")


if __name__ == "__main__":
    unittest.main()
