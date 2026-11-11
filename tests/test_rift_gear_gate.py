# tests/test_rift_gear_gate.py
# 三重装备门槛：八格齐全 + 品阶达到该秘境门槛境界可装备的最高品阶
import json
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


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


ConfigManager = _imp("config_manager").ConfigManager
RiftManager = _imp("managers.rift_manager").RiftManager
Player = _imp("models").Player
Rift = _imp("models_extended").Rift
ItemRules = _imp("core.item_rules").ItemRules
gate = _imp("core.rift_gate")

CFG = json.loads((ROOT / "config" / "rift_config.json").read_text(encoding="utf-8"))
RIFTS = {r["key"]: r for r in CFG["rifts"]}

# 槽位 → 装备类型（戒指两格都用 ring 类型；甲胄的配置类型是 body）
SLOT_TYPES = {
    "weapon": "weapon",
    "head": "head",
    "armor": "body",
    "gloves": "gloves",
    "boots": "boots",
    "ring1": "ring",
    "ring2": "ring",
    "amulet": "amulet",
}


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


def _find_name(cm, rank: str, want_type: str):
    for name, cfg in (cm.items_data or {}).items():
        if not isinstance(cfg, dict) or str(cfg.get("rank")) != rank:
            continue
        if ItemRules.normalize_equipment_type(cfg) == want_type:
            return name
    return None


def equip_all(p: Player, cm, rank: str, override: dict = None):
    """给玩家穿上一整套指定品阶的装备；override 可指定某槽用别的品阶。"""
    override = override or {}
    instances, mapping = [], {}
    for i, slot in enumerate(gate.SLOT_ORDER):
        use_rank = override.get(slot, rank)
        name = _find_name(cm, use_rank, SLOT_TYPES[slot])
        if not name:
            raise AssertionError(f"配置里缺 {use_rank} 的 {SLOT_TYPES[slot]} 装备")
        instances.append(
            {
                "id": i + 1,
                "name": name,
                "type": SLOT_TYPES[slot],
                "quality": "一般",
                "location": "equipped",
                "slot": slot,
            }
        )
        mapping[slot] = i + 1
    p.equipment_instances = json.dumps(instances, ensure_ascii=False)
    p.equipped_instances = json.dumps(mapping, ensure_ascii=False)


class TestRequiredRank(unittest.TestCase):
    def test_rank_follows_unlock_table(self):
        self.assertEqual(gate.required_gear_rank(9), "玄品")
        self.assertEqual(gate.required_gear_rank(18), "圣品")
        self.assertEqual(gate.required_gear_rank(27), "帝品")
        self.assertEqual(gate.required_gear_rank(35), "混元先天")

    def test_rank_is_based_on_rift_level_not_player_level(self):
        """lv35 玩家回刷青云三重，要求仍是玄品，不是混元先天。"""
        cm = ConfigManager(ROOT)
        rift = make_rift("qingyun", 3)
        p = make_player(35)
        equip_all(p, cm, "玄品")
        ok, detail = gate.check(p, rift.required_level, cm)
        self.assertTrue(ok, detail)


class TestGateCheck(unittest.TestCase):
    def setUp(self):
        self.cm = ConfigManager(ROOT)

    def test_naked_player_fails_with_all_slots_missing(self):
        p = make_player(27)
        ok, detail = gate.check(p, 27, self.cm)
        self.assertFalse(ok)
        self.assertEqual(len(detail["empty"]), 8)
        self.assertIn("帝品", detail["need"])

    def test_full_set_passes(self):
        p = make_player(27)
        equip_all(p, self.cm, "帝品")
        ok, detail = gate.check(p, 27, self.cm)
        self.assertTrue(ok, detail)

    def test_one_low_rank_slot_fails(self):
        """7 件帝品 + 1 件圣品：只差一格也算没准备够。"""
        p = make_player(27)
        equip_all(p, self.cm, "帝品", override={"boots": "圣品"})
        ok, detail = gate.check(p, 27, self.cm)
        self.assertFalse(ok)
        self.assertEqual(detail["low"], ["boots"])

    def test_higher_rank_than_needed_passes(self):
        """穿得比要求更好当然可以进。"""
        p = make_player(27)
        equip_all(p, self.cm, "混元先天")
        ok, _ = gate.check(p, 27, self.cm)
        self.assertTrue(ok)

    def test_describe_mentions_need_and_gap(self):
        p = make_player(27)
        equip_all(p, self.cm, "帝品", override={"boots": "圣品"})
        _, detail = gate.check(p, 27, self.cm)
        text = gate.describe(detail)
        self.assertIn("帝品全套", text)
        self.assertIn("靴子", text)


class TestGearLockFlag(unittest.TestCase):
    def setUp(self):
        self.cm = ConfigManager(ROOT)
        self.mgr = RiftManager(None, self.cm)

    def test_third_tier_is_locked_and_first_two_are_not(self):
        for rift in self.mgr.rifts:
            for diff in rift["difficulties"]:
                expect = int(diff["tier"]) == 3
                self.assertEqual(bool(diff.get("gear_lock")), expect, diff.get("label"))

    def test_detail_returns_none_when_gear_is_ready(self):
        p = make_player(27)
        equip_all(p, self.cm, "帝品")
        self.assertIsNone(self.mgr._gear_lock_detail(p, make_rift("tianlei", 3)))

    def test_detail_reports_gap_when_gear_is_missing(self):
        p = make_player(27)
        detail = self.mgr._gear_lock_detail(p, make_rift("tianlei", 3))
        self.assertIsNotNone(detail)
        self.assertEqual(detail["need"], "帝品")

    def test_no_lock_on_lower_tiers(self):
        p = make_player(27)
        self.assertIsNone(self.mgr._gear_lock_detail(p, make_rift("tianlei", 2)))
        self.assertIsNone(self.mgr._gear_lock_detail(p, make_rift("tianlei", 1)))


if __name__ == "__main__":
    unittest.main()
