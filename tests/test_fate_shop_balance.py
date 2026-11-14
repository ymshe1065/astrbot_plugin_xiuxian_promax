# tests/test_fate_shop_balance.py
# 仙缘商店定价配平（v3.45.20）：以【修炼加速】为锚，锁住三件事——
#   1. 锚本身没被改（100 点 = 闭关 +10%），否则整张表都要重算；
#   2. 每项价格与自己「折算出来的价值」一致（docs/仙缘商店定价表.md 与 _SHOP_DEFS 同步）；
#   3. 掉落组（v3.45.6 已单独配平）不被顺手改掉。
import subprocess
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


rules = _imp("core.reincarnation_rules")

SCRIPT = ROOT / "scripts" / "fate_shop_balance.py"
DOC = ROOT / "docs" / "仙缘商店定价表.md"


class TestAnchor(unittest.TestCase):
    def test_anchor_item_is_untouched(self):
        """锚：修炼加速 100 点 = 每层 +10% 闭关。改了它整张表都要重算。"""
        item = rules.SHOP_ITEMS[1]
        self.assertEqual(item["cost"], 100)
        self.assertEqual(item["step"], 0.10)
        self.assertEqual(item["cap"], 0)  # 无上限，玩家愿意肝就一直变强

    def test_price_per_one_percent_speed(self):
        """修炼加速同时乘在闭关与历练上（吃满总速度）→ 10 个百分点 → 100 点 ⇒ 10 点/点。"""
        anchor = rules.SHOP_ITEMS[1]
        pp = anchor["step"] * 100  # step=0.10 → 10 个百分点
        self.assertAlmostEqual(anchor["cost"] / pp, 10.0, places=6)


class TestPricesMatchModel(unittest.TestCase):
    def test_doc_and_prices_in_sync(self):
        """跑一遍 --check：文档过期或价格被手改都会挂。"""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--check"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_progress_items_are_ordered_by_value(self):
        """修为组的价格必须跟「来源占总速度的比例」同向。

        历练心得只吃「历练」那一半速度：+12% × 半速 = 6 个百分点 → 60 点，
        正好是修炼加速（10 个百分点 / 100 点）的 60%；
        灵眼/洞天/灵田/悬赏这些副系统在 lv34 只占万分之几，必须压到下限 10 点。
        """
        self.assertEqual(rules.SHOP_ITEMS[6]["cost"], 60)    # 历练心得
        for item_id in (9, 10, 12, 13, 14):
            self.assertEqual(rules.SHOP_ITEMS[item_id]["cost"], 10, f"{item_id} 应按下限定价")

    def test_every_item_price_is_positive_and_bounded(self):
        for item_id, item in rules.SHOP_ITEMS.items():
            self.assertGreater(item["cost"], 0, f"{item_id} 没有价格")
            self.assertLessEqual(item["cost"], 400, f"{item_id} 价格越界: {item['cost']}")


class TestDropGroupUntouched(unittest.TestCase):
    """掉落组在 v3.45.6 已按秘境材料/装备的金币产出单独配平，本次不参与折算。"""

    EXPECTED = {7: 30, 8: 40, 29: 100, 30: 80, 31: 150, 32: 400, 33: 80}

    def test_drop_prices_unchanged(self):
        for item_id, cost in self.EXPECTED.items():
            self.assertEqual(rules.SHOP_ITEMS[item_id]["cost"], cost,
                             f"掉落组 {item_id} 的价格被改动了")


class TestDualCultCap(unittest.TestCase):
    """双修一次 = 对方总修为的 10%，按收益该 200 点以上；用「限购 5 层」兜底。"""

    def test_cap_is_five(self):
        self.assertEqual(rules.SHOP_ITEMS[11]["cap"], 5)

    def test_at_cap_blocks_sixth_purchase(self):
        bonus = {}
        for _ in range(5):
            rules.add_item(bonus, 11)
        self.assertEqual(rules.item_owned(bonus, 11), 5)
        self.assertTrue(rules.item_at_cap(bonus, 11))

    def test_entry_marks_full(self):
        bonus = {}
        for _ in range(5):
            rules.add_item(bonus, 11)
        entry = next(e for e in rules.shop_entries(bonus) if e["id"] == 11)
        self.assertEqual(entry["state"], "lock")
        self.assertEqual(entry["value"], "已满")
        self.assertNotIn("price", entry)


class TestStructuralCapsUnchanged(unittest.TestCase):
    def test_structural_caps(self):
        expected = {26: 3, 31: 3, 32: 1, 39: 6, 43: 3, 44: 2, 46: 1, 48: 1, 49: 1, 50: 1, 51: 3, 52: 4, 11: 5}
        for item_id, cap in expected.items():
            self.assertEqual(rules.SHOP_ITEMS[item_id]["cap"], cap, f"{item_id} 的硬顶变了")


if __name__ == "__main__":
    unittest.main()
