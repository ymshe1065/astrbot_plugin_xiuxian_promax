# 物品详情/简略属性口径回归测试
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


class _L:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


api_mod.logger = _L()
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


def _imp(path):
    return __import__(f"{PKG}.{path}", fromlist=["x"])


class TestItemDetailFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _imp("data")
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.shop = _imp("core.shop_manager").ShopManager({}, cls.cm)
        cls.display = _imp("core.item_display")

    def _details(self, name, instance=None):
        item = self.shop.find_item_by_name(name)
        self.assertIsNotNone(item, name)
        return "\n".join(self.shop.get_trade_details(item, instance))

    # ===== 交易详情：实例品质必须同时作用于主属性与特殊效果 =====
    def test_trade_details_passes_instance_quality_to_special_effects(self):
        text = self._details("凡品·破甲手套", {"quality": "大师"})
        self.assertIn("特殊效果（大师品质）", text)
        self.assertNotIn("特殊效果（一般品质）", text)
        self.assertIn("无视防御 +3.4%", text)
        self.assertIn("品质: 大师（特殊效果×1.15）", text)

    # ===== 装备特效必须按单件上限展示，和实战结算一致 =====
    def test_equipment_detail_caps_sunder_pct_like_combat(self):
        text = self._details("道品·破甲手套")
        self.assertIn("破防减防 +15%", text)
        self.assertNotIn("破防减防 +17%", text)
        # 描述里的减防值也同步为实际生效值，避免同一条详情自相矛盾
        self.assertIn("防御 -15%", text)

    # ===== 简略属性：新版装备走百分比，不再读 flat 字段 =====
    def test_short_effect_uses_equipment_rates_for_new_gear(self):
        item = self.shop.find_item_by_name("凡品·灵纹法手")
        text = self.shop._get_item_effect_short(item)
        self.assertIn("法伤+4.8%", text)
        self.assertIn("物伤+1.2%", text)
        for raw_key in ("physical_damage", "magic_damage", "equipment_rates"):
            self.assertNotIn(raw_key, text)

    def test_short_effect_scales_rates_with_shelf_quality(self):
        item = self.shop.find_item_by_name("凡品·灵纹法手")
        item["quality"] = "大师"
        text = self.shop._get_item_effect_short(item)
        self.assertIn("法伤+5.5%", text)
        self.assertIn("物伤+1.4%", text)

    def test_short_effect_uses_rates_for_legacy_gear(self):
        item = self.shop.find_item_by_name("幽冥袍")
        text = self.shop._get_item_effect_short(item)
        self.assertIn("物防+10.8%", text)
        self.assertIn("法防+10.8%", text)

    # ===== 丹药详情：恢复/突破/永久增益字段不能静默丢失 =====
    def test_instant_restore_and_percent_pills_show_effect(self):
        self.assertIn("恢复气血：10%", self.shop.get_item_details(self.shop.find_item_by_name("回春散·凡")))
        self.assertIn("恢复全部灵气", self.shop.get_item_details(self.shop.find_item_by_name("满灵丹")))

    def test_instant_qi_percent_uses_primary_field_only(self):
        item = {
            "name": "测试瞬回丹", "type": "utility_pill", "rank": "凡品", "price": 0,
            "data": {
                "name": "测试瞬回丹", "effect_type": "instant", "subtype": "instant_restore",
                "restore_qi_percent": 0.3, "restore_mp_percent": 0.1,
            },
        }
        text = self.shop.get_item_details(item)
        self.assertEqual(text.count("恢复灵气"), 1, text)
        self.assertIn("30%", text)
        self.assertNotIn("10%", text)

    def test_temporary_breakthrough_pill_shows_bonus(self):
        text = self.shop.get_item_details(self.shop.find_item_by_name("天命幸运丹"))
        self.assertIn("突破成功率+20%", text)
        self.assertIn("可超出破境丹上限", text)

    def test_permanent_pill_shows_lifespan_and_multiplier(self):
        self.assertIn("寿命+300", self.shop.get_item_details(self.shop.find_item_by_name("延年丹")))
        text = self.shop.get_item_details(self.shop.find_item_by_name("伐体丹"))
        self.assertIn("修炼速度+240%（永久）", text)
        self.assertIn("物伤-30%（永久）", text)

    def test_every_utility_pill_has_effect_section(self):
        missing = [
            name for name in self.cm.utility_pills_data
            if "效果:" not in self.shop.get_item_details(self.shop.find_item_by_name(name))
        ]
        self.assertEqual(missing, [])

    # ===== 没有 type 字段的 exp/utility 丹药也要有展示标签 =====
    def test_exp_and_utility_pill_tags_are_recognized(self):
        self.assertIn("丹药·修为", self.display.item_tag("炼气丹", self.cm))
        self.assertIn("丹药·回生", self.display.item_tag("回生丹", self.cm))
        self.assertIn("丹药·恢复", self.display.item_tag("回春散·凡", self.cm))


if __name__ == "__main__":
    unittest.main()
