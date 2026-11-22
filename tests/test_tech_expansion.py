# tests/test_tech_expansion.py
# v4.3.13 势力科技线扩展：地形轴 / 带兵轴 / 资源轴 / 配置覆盖 / 行军先手联动
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub  # noqa: E402

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


R = _imp("core.strategy_rules")
H = _imp("core.strategy_hex")


class TestTechExpansion(unittest.TestCase):
    def test_legacy_keys_preserved(self):
        eff = R.tech_effects({"military": 5, "agri": 3, "stratagem": 4})
        self.assertAlmostEqual(eff["atk_bonus"], 0.06)
        self.assertAlmostEqual(eff["flank_bonus"], 0.05)
        self.assertAlmostEqual(eff["output_bonus"], 0.08)
        self.assertEqual(eff["plot_cd_reduce"], 2)
        self.assertEqual(eff["counter_spy"], 0.0)
        self.assertEqual(R.tech_effects({"stratagem": 5})["counter_spy"], 0.30)
        self.assertEqual(R.tech_effects(None)["atk_bonus"], 0.0)

    def test_terrain_axis(self):
        eff = R.tech_effects({"military": 5})
        # L3 高地 +8% 与 L5 全地形 +3% 相加
        self.assertAlmostEqual(eff["terrain_atk_mult"]["highland"], 0.11)
        self.assertAlmostEqual(eff["terrain_atk_mult"]["plain"], 0.06)
        self.assertAlmostEqual(eff["terrain_def_mult"]["forest"], 0.10)
        self.assertAlmostEqual(eff["terrain_def_mult"]["wall"], 0.10)
        self.assertAlmostEqual(eff["terrain_def_mult"]["plain"], 0.03)

    def test_troop_thresholds(self):
        eff = R.tech_effects({"military": 5})
        self.assertEqual(eff["big_army_threshold"], 2000)
        self.assertEqual(eff["small_army_threshold"], 800)
        self.assertAlmostEqual(eff["big_army_atk_bonus"], 0.05)
        self.assertEqual(eff["small_army_move_bonus"], 1)
        self.assertAlmostEqual(eff["small_army_init_bonus"], 5.0)
        # 未到 L2/L5 时阈值加成应为 0
        low = R.tech_effects({"military": 1})
        self.assertEqual(low["big_army_atk_bonus"], 0.0)
        self.assertEqual(low["small_army_move_bonus"], 0)

    def test_resource_axis(self):
        eff = R.tech_effects({"agri": 5})
        # L2 grain ×1.10，再叠 L5 ×1.02
        self.assertAlmostEqual(eff["resource_output_mult"]["grain"], 1.122)
        # 灵石单列 +5%；「全资源再 +2%」只作用于铜钱/粮草/精铁
        self.assertAlmostEqual(eff["resource_output_mult"]["gold"], 1.05)
        self.assertAlmostEqual(eff["resource_output_mult"]["coin"], 1.02)
        self.assertAlmostEqual(eff["node_type_output_mult"]["mine"]["iron"], 1.15)
        self.assertAlmostEqual(eff["node_type_output_mult"]["market"]["coin"], 1.10)
        self.assertAlmostEqual(eff["node_type_output_mult"]["grain"]["grain"], 1.10)

    def test_config_override(self):
        tcfg = {"big_army_threshold": 3000, "small_army_threshold": 500,
                "effects_values": {"military": {"1": {"atk_bonus": 0.10}}}}
        eff = R.tech_effects({"military": 1}, tcfg=tcfg)
        self.assertAlmostEqual(eff["atk_bonus"], 0.10)
        self.assertEqual(eff["big_army_threshold"], 3000)
        self.assertEqual(eff["small_army_threshold"], 500)
        # 未覆盖的键仍走默认补丁（L1 平原 +3%）
        self.assertAlmostEqual(eff["terrain_atk_mult"]["plain"], 0.03)

    def test_march_move_bonus(self):
        base = {"troop_type": "sword_shield", "troops": 500, "leader": None}
        fast = dict(base, move_bonus=1)
        self.assertGreater(R.march_speed(fast, "plain"),
                           R.march_speed(base, "plain"))

    def test_initiative_bonus(self):
        base = {"troops": 500, "leader": {"profession": "sword",
                "stats": {"valor": 50}}}
        fast = dict(base, init_bonus=5)
        # 同一随机种子下 +5 先手应更高
        a = R.battle_initiative(fast, random.Random(7))
        b = R.battle_initiative(base, random.Random(7))
        self.assertGreater(a, b)

    def test_hex_terrain_tech_applies(self):
        w, h = 13, 9
        territory = {f"{q},{r}": H.T_PLAIN for q in range(w) for r in range(h)}
        za, zb = H.deploy_zones(w, h, 3)
        s = H.new_session({"size": [w, h], "terrain": territory,
                           "deploy_a": za, "deploy_b": zb}, "field", "t")
        u = H.add_unit(s, {"id": 1, "faction_id": 1,
                           "troop_type": "sword_shield", "troops": 1000,
                           "morale": 100, "veteran": 0, "leader": None,
                           "siege_engine": False}, "a")
        u["pos"] = [5, 4]
        v0 = H._sim_view(s, u)
        # 军事 L5：平原攻 +6%、防 +3%
        s["tech_effects"] = {"1": R.tech_effects({"military": 5})}
        v1 = H._sim_view(s, u)
        self.assertGreater(v1["atk_terrain_mult"], v0["atk_terrain_mult"])
        self.assertGreater(v1["def_terrain_mult"], v0["def_terrain_mult"])

    def test_hex_small_army_initiative(self):
        w, h = 13, 9
        territory = {f"{q},{r}": H.T_PLAIN for q in range(w) for r in range(h)}
        za, zb = H.deploy_zones(w, h, 3)
        s = H.new_session({"size": [w, h], "terrain": territory,
                           "deploy_a": za, "deploy_b": zb}, "field", "t")
        u = H.add_unit(s, {"id": 1, "faction_id": 1,
                           "troop_type": "sword_shield", "troops": 500,
                           "morale": 100, "veteran": 0, "leader": None,
                           "siege_engine": False}, "a")
        u["pos"] = [5, 4]
        s["tech_effects"] = {"1": R.tech_effects({"military": 5})}
        self.assertAlmostEqual(H._sim_view(s, u)["init_bonus"], 5.0)

    def test_level_6_7_effects(self):
        """L6/L7 三国志个性转化效果。"""
        tcfg7 = {"levels": 7}
        mil = R.tech_effects({"military": 7}, tcfg=tcfg7)
        self.assertAlmostEqual(mil["morale_loss_mult"], 0.8)
        self.assertEqual(mil["kill_morale_bonus"], 10)
        self.assertTrue(mil["ignore_zoc"])
        self.assertTrue(mil["immune_surround"])
        self.assertEqual(mil["duel_win_morale_bonus"], 5)
        self.assertAlmostEqual(mil["duel_injury_bonus"], 0.10)
        self.assertAlmostEqual(mil["capture_bonus"], 0.30)
        self.assertTrue(mil["no_capture"])
        agri = R.tech_effects({"agri": 7}, tcfg=tcfg7)
        self.assertAlmostEqual(agri["recruit_cost_mult"], 0.85)
        self.assertAlmostEqual(agri["train_mult"], 1.20)
        self.assertAlmostEqual(agri["garrison_grow_bonus"], 0.005)
        self.assertAlmostEqual(agri["heal_mult"], 1.30)
        self.assertAlmostEqual(agri["salary_mult"], 0.80)
        self.assertAlmostEqual(agri["morale_recover_bonus"], 1.0)
        strat = R.tech_effects({"stratagem": 7}, tcfg=tcfg7)
        self.assertAlmostEqual(strat["fire_damage_mult"], 1.30)
        self.assertAlmostEqual(strat["fire_def_mult"], 0.70)
        self.assertAlmostEqual(strat["plot_duration_mult"], 1.20)
        self.assertAlmostEqual(strat["enemy_plot_rate_mult"], 0.90)
        # 未点到对应级时保持默认（无 config 时默认 max_lvl=5，L6/L7 不生效）
        low = R.tech_effects({"military": 5})
        self.assertAlmostEqual(low["morale_loss_mult"], 1.0)
        self.assertFalse(low["ignore_zoc"])

    def test_config_effects_values_match_defaults(self):
        """config 的 effects_values 与代码默认补丁保持一致（防漂移）。"""
        import json
        cfg = json.loads(
            (ROOT / "config/strategy_config.json").read_text(encoding="utf-8"))
        tcfg = cfg["tech"]
        for line in ("military", "agri", "stratagem"):
            for lv in range(1, 8):
                a = R.tech_effects({line: lv}, tcfg={"levels": 7})
                b = R.tech_effects({line: lv}, tcfg=tcfg)
                for key in ("atk_bonus", "ram_cost_mult", "terrain_atk_mult",
                            "terrain_def_mult", "big_army_atk_bonus",
                            "big_army_siege_bonus", "small_army_move_bonus",
                            "small_army_init_bonus", "resource_output_mult",
                            "node_type_output_mult", "counter_spy",
                            "counter_spy_defense", "plot_bonus_vs_big_army",
                            "scout_terrain_reveal", "morale_loss_mult",
                            "kill_morale_bonus", "ignore_zoc", "immune_surround",
                            "duel_win_morale_bonus", "duel_injury_bonus",
                            "capture_bonus", "no_capture", "recruit_cost_mult",
                            "train_mult", "heal_mult", "salary_mult",
                            "garrison_grow_bonus", "morale_recover_bonus",
                            "fire_damage_mult", "fire_def_mult",
                            "plot_duration_mult", "enemy_plot_rate_mult"):
                    self.assertEqual(a[key], b[key],
                                     f"{line} L{lv} {key} 配置与默认不一致")


if __name__ == "__main__":
    unittest.main()
