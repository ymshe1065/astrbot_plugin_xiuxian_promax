# 主修心法 / 普通功法体系回归测试
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestTechniqueSystem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.Player = _imp("models").Player
        cls.TBM = _imp("core.technique_bonus").TechniqueBonusManager
        cls.ShopManager = _imp("core.shop_manager").ShopManager
        cls.LuckManager = _imp("managers.luck_manager").LuckManager

    def test_main_techniques_are_combat_focused(self):
        main_items = {
            name: cfg for name, cfg in self.cm.techniques_data.items()
            if cfg.get("type") == "main_technique"
        }
        self.assertGreaterEqual(len(main_items), 20)
        elements = {cfg.get("element") for cfg in main_items.values()}
        self.assertEqual(elements, {"金", "木", "水", "火", "土"})
        for cfg in main_items.values():
            self.assertTrue(cfg.get("effects"), cfg.get("name"))
            self.assertEqual(float(cfg.get("exp_multiplier", 0)), 0.0, cfg.get("name"))
            self.assertFalse(cfg.get("life_effects"), cfg.get("name"))

    def test_ordinary_techniques_are_life_focused(self):
        ordinary = []
        for source in (self.cm.items_data, self.cm.techniques_data):
            for name, cfg in source.items():
                if cfg.get("type") in ("technique", "功法"):
                    ordinary.append((name, cfg))
        self.assertGreaterEqual(len(ordinary), 150)
        recovery_effects = {
            "natural_hp_regen_bonus", "natural_qi_regen_bonus",
            "rest_hp_regen_bonus", "rest_qi_regen_bonus",
        }
        for name, cfg in ordinary:
            self.assertTrue(
                not cfg.get("effects") or set(cfg.get("effects") or {}) <= recovery_effects,
                name,
            )
            has_life = bool(cfg.get("life_effects")) or float(cfg.get("exp_multiplier", 0) or 0) > 0
            self.assertTrue(has_life, name)

    def test_life_effects_aggregate_and_cap(self):
        player = self.Player(user_id="1")
        ordinary = [
            name for name, cfg in self.cm.items_data.items()
            if cfg.get("type") == "功法" and cfg.get("life_effects")
        ]
        player.set_techniques_list(ordinary[:3])
        life = self.TBM.collect_life_effects(player, self.cm)
        self.assertGreaterEqual(life["gold_bonus"], 0)
        self.assertGreaterEqual(life["drop_bonus"], 0)
        self.assertGreaterEqual(life["cooldown_reduction"], 0)
        self.assertGreaterEqual(life["luck_bonus"], 0)

    def test_technique_luck_and_cooldown(self):
        player = self.Player(user_id="1")
        luck_name = next(
            name for name, cfg in self.cm.items_data.items()
            if cfg.get("type") == "功法" and cfg.get("life_effects", {}).get("luck_bonus")
        )
        player.set_techniques_list([luck_name])
        luck_cfg = self.cm.items_data[luck_name]["life_effects"]["luck_bonus"]
        self.assertEqual(self.LuckManager.get_technique_luck(player, self.cm), int(luck_cfg))

        cooldown_name = next(
            name for name, cfg in self.cm.items_data.items()
            if cfg.get("type") == "功法" and cfg.get("life_effects", {}).get("cooldown_reduction")
        )
        player.set_techniques_list([cooldown_name])
        self.assertLess(self.TBM.effective_cooldown(300, player, self.cm), 300)

    def test_shop_pool_contains_both_technique_types(self):
        shop = self.ShopManager({}, self.cm)
        pool = {item["name"]: item for item in shop.get_all_items_for_display(9999)}
        main_name = next(
            name for name, cfg in self.cm.techniques_data.items()
            if cfg.get("type") == "main_technique"
        )
        life_name = next(
            name for name, cfg in self.cm.items_data.items()
            if cfg.get("type") == "功法"
        )
        self.assertEqual(pool[main_name]["type"], "main_technique")
        self.assertEqual(pool[life_name]["type"], "technique")

    def test_drop_roll_returns_configured_technique(self):
        player = self.Player(user_id="1", level_index=40)
        names = {
            cfg["name"] for cfg in self.cm.techniques_data.values()
            if cfg.get("type") == "main_technique"
        }
        # roll_technique_drop 的掉落概率上限固定为 95%（与奇遇等概率触发一致，永不为 100% 必掉），
        # 因此 base_chance=100 仍有 5% 概率直接返回空串。这里把随机闸门固定为必中，
        # 只验证“掉落时取到的是配置的主修心法”，避免用例随随机状态偶发失败。
        with patch("random.random", return_value=0.0):
            picked = self.TBM.roll_technique_drop(
                player, self.cm, base_chance=100, allow_main=True, main_ratio=1.0
            )
        self.assertIn(picked, names)


if __name__ == "__main__":
    unittest.main()
