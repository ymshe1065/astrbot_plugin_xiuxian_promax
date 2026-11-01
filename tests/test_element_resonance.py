# 五行共鸣回归测试：心法主属性 + 同属性装备/功法 + 同属性灵根
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


FIRE_GEAR = [
    ("weapon", "焚天赤霄刀"),
    ("head", "焚天赤焰冠"),
    ("armor", "焚天熔鳞甲"),
    ("gloves", "焚天炎狱手"),
    ("boots", "焚天踏火靴"),
    ("ring1", "焚天业火环"),
    ("ring2", "焚天燎原戒"),
    ("amulet", "焚天火魄符"),
]


class TestElementResonance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.Player = _imp("models").Player
        cls.ER = _imp("core.element_resonance").ElementResonance
        cls.CM = _imp("managers.combat_manager").CombatManager

    def _player(self, main="赤炎焚脉诀", weapon="", root="火灵根", fire_gear=True, techniques=None):
        player = self.Player(
            user_id="u1",
            user_name="测试道友",
            level_index=10,
            experience=100000,
            main_technique=main,
            weapon=weapon,
            spiritual_root=root,
        )
        if fire_gear:
            instances = []
            mapping = {}
            for index, (slot, name) in enumerate(FIRE_GEAR, 1):
                instances.append({
                    "id": index,
                    "name": name,
                    "type": "weapon" if slot == "weapon" else "armor",
                    "quality": "一般",
                    "affixes": [],
                    "location": "equipped",
                    "slot": slot,
                })
                mapping[slot] = index
            player.set_equipment_instances(instances)
            player.set_equipped_instances(mapping)
        if techniques:
            player.set_techniques_list(techniques)
        return player

    def test_main_element_prefers_technique_over_weapon(self):
        player = self._player(weapon="玄水寒渊剑", main="赤炎焚脉诀", fire_gear=False)
        result = self.ER.compute(player, self.cm)
        self.assertEqual(result["main_element"], "火")

    def test_weapon_fallback_when_no_technique(self):
        player = self._player(main="", weapon="玄水寒渊剑", fire_gear=False)
        result = self.ER.compute(player, self.cm)
        self.assertEqual(result["main_element"], "水")

    def test_matching_gear_and_life_technique_counted(self):
        player = self._player(techniques=["四象功"])
        result = self.ER.compute(player, self.cm)
        self.assertEqual(result["main_element"], "火")
        # 8 件火装备 + 1 本火属性普通功法
        self.assertEqual(result["match_count"], 9)
        self.assertEqual(result["matched_items"].count("四象功"), 1)
        self.assertNotIn("玄水鲛绡袍", result["matched_items"])

    def test_non_matching_gear_not_counted(self):
        player = self._player(main="玄水化元经", fire_gear=True)
        result = self.ER.compute(player, self.cm)
        self.assertEqual(result["main_element"], "水")
        self.assertEqual(result["match_count"], 0)

    def test_same_root_adds_extra_bonus(self):
        fire = self.ER.compute(self._player(root="火灵根"), self.cm)
        water = self.ER.compute(self._player(root="水灵根"), self.cm)
        self.assertTrue(fire["root_match"])
        self.assertFalse(water["root_match"])
        self.assertGreater(fire["physical_atk_pct"], water["physical_atk_pct"])
        self.assertGreater(fire["crit_damage"], water["crit_damage"])

    def test_multi_root_bonus_shared(self):
        dual = self.ER.compute(self._player(root="火土灵根"), self.cm)
        single = self.ER.compute(self._player(root="火灵根"), self.cm)
        self.assertTrue(dual["root_match"])
        self.assertAlmostEqual(dual["root_share"], 0.5)
        self.assertLess(dual["physical_atk_pct"], single["physical_atk_pct"])

    def test_variant_root_maps_to_element(self):
        result = self.ER.compute(self._player(main="青木回春诀", root="雷灵根"), self.cm)
        self.assertEqual(result["main_element"], "木")
        self.assertTrue(result["root_match"])

    def test_yin_yang_root_is_not_fire(self):
        elements = self.ER.root_elements("阴阳灵根")
        self.assertNotIn("火", elements)

    def test_fusion_and_chaos_roots_cover_five_elements(self):
        for root in ("融合灵根", "混沌灵根"):
            elements = self.ER.root_elements(root)
            self.assertEqual(elements, {"金", "木", "水", "火", "土"})
            result = self.ER.compute(self._player(root=root), self.cm)
            self.assertTrue(result["root_match"])
            self.assertAlmostEqual(result["root_share"], 0.2)

    def test_resonance_applied_to_combat_panel(self):
        fire = self._player(root="火灵根", techniques=["四象功"])
        water = self._player(root="水灵根", techniques=["四象功"])
        fire_stats = self.CM.build_player_combat_stats(fire, self.cm)
        water_stats = self.CM.build_player_combat_stats(water, self.cm)
        self.assertEqual(fire_stats.element, "火")
        self.assertEqual(fire_stats.element_resonance["match_count"], 9)
        self.assertTrue(fire_stats.element_resonance["root_match"])
        self.assertFalse(water_stats.element_resonance["root_match"])
        self.assertGreater(fire_stats.atk, water_stats.atk)
        self.assertGreater(
            float((fire_stats.effects or {}).get("crit_damage", 0)),
            float((water_stats.effects or {}).get("crit_damage", 0)),
        )

    def test_bonus_is_capped(self):
        result = self.ER.compute(self._player(techniques=["四象功"]), self.cm)
        self.assertLessEqual(result["physical_atk_pct"], 0.20)
        self.assertLessEqual(result["magic_atk_pct"], 0.20)
        self.assertLessEqual(result["crit_rate"], 10.0)
        self.assertLessEqual(result["crit_damage"], 0.30)


if __name__ == "__main__":
    unittest.main()
