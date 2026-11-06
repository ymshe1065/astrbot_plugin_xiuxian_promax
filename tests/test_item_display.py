# 物品信息标签（种类·细分 | 品阶）回归测试
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


api_mod.logger = _L(); api_mod.AstrBotConfig = dict
event_mod.AstrMessageEvent = object; api_mod.event = event_mod
all_mod = types.ModuleType("astrbot.api.all"); all_mod.At = object; all_mod.Plain = object
api_mod.all = all_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod)
sys.modules.setdefault("astrbot.api.all", all_mod)


def _imp(path):
    return __import__(f"{PKG}.{path}", fromlist=["x"])


class TestItemDisplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.dis = _imp("core.item_display")

    # ===== 材料 =====

    def test_material_tag_shows_category_and_rank(self):
        tag = self.dis.item_tag("万古铁", self.cm)
        self.assertEqual(tag, "（材料·矿材 | 黄品）")

    def test_material_category_is_from_catalog(self):
        # 灵植与矿材必须能区分开，不能都写成「材料」
        self.assertIn("灵植", self.dis.item_tag("九天草", self.cm))
        self.assertIn("妖兽", self.dis.item_tag("九天骨", self.cm))
        meta = self.dis.item_meta("九天草", self.cm)
        self.assertEqual(meta["kind"], "材料")
        self.assertEqual(meta["sub"], "灵植")

    # ===== 装备 =====

    def test_equipment_tag_shows_slot(self):
        meta = self.dis.item_meta("幽冥袍", self.cm)
        self.assertEqual(meta["kind"], "装备")
        self.assertEqual(meta["sub"], "甲胄")
        self.assertIn("圣品", meta["rank"])

    def test_equipment_label_keeps_quality(self):
        label = self.dis.equipment_label({"name": "幽冥袍", "quality": "极品"}, self.cm)
        self.assertEqual(label, "幽冥袍（装备·甲胄 | 圣品）·极品")

    def test_equipment_label_without_quality_falls_back(self):
        label = self.dis.equipment_label({"name": "幽冥袍", "quality": ""}, self.cm)
        self.assertTrue(label.endswith("·一般"), label)

    # ===== 非装备不能被装备判定吃掉 =====

    def test_technique_is_not_equipment(self):
        meta = self.dis.item_meta("九天谱", self.cm)
        self.assertEqual(meta["kind"], "功法")
        self.assertEqual(meta["sub"], "技能")

    def test_main_technique_marked_as_xinfa(self):
        meta = self.dis.item_meta("庚金锐锋诀", self.cm)
        self.assertEqual(meta["kind"], "功法")
        self.assertEqual(meta["sub"], "心法")

    # ===== 符箓 / 丹药 / 丹方 =====

    def test_talisman_tag_shows_usage(self):
        # 鸿蒙符是增益符，用途来自 talisman_recipes
        tag = self.dis.item_tag("鸿蒙符", self.cm)
        self.assertIn("符箓", tag)
        self.assertIn("增益", tag)

    def test_pill_tag_has_rank(self):
        tag = self.dis.item_tag("筑基丹", self.cm)
        self.assertIn("丹药", tag)
        self.assertIn("灵品", tag)

    def test_recipe_scroll_tag(self):
        scrolls = [n for n, cfg in self.cm.items_data.items() if cfg.get("type") == "丹方"]
        self.assertTrue(scrolls, "应存在注入的丹方条目")
        tag = self.dis.item_tag(scrolls[0], self.cm)
        self.assertIn("丹方", tag)

    # ===== 兜底 =====

    def test_unknown_item_returns_empty_tag(self):
        self.assertEqual(self.dis.item_tag("不存在的物品", self.cm), "")
        self.assertEqual(self.dis.item_label("不存在的物品", self.cm), "不存在的物品")

    def test_without_config_manager_does_not_raise(self):
        self.assertEqual(self.dis.item_tag("万古铁", None), "")
        self.assertEqual(self.dis.item_tag("", self.cm), "")

    # ===== 展示名回落成指令参数 =====

    def test_strip_item_tag_from_display_label(self):
        # 储物戒芯片点「详情」时传的是展示名，查询前必须去掉括号介绍
        self.assertEqual(
            self.dis.strip_item_tag("无极骨（材料·妖兽 | 玄品）"), "无极骨"
        )
        self.assertEqual(self.dis.strip_item_tag("筑基丹（丹药·突破 | 灵品）"), "筑基丹")
        self.assertEqual(self.dis.strip_item_tag("某丹方（丹方）"), "某丹方")

    def test_strip_item_tag_keeps_plain_name_and_quality_suffix(self):
        self.assertEqual(self.dis.strip_item_tag("无极骨"), "无极骨")
        self.assertEqual(
            self.dis.strip_item_tag("幽冥袍（装备·甲胄 | 圣品）·极品"), "幽冥袍"
        )
        # 物品名自带括号但不是展示标签时不能误删
        self.assertEqual(self.dis.strip_item_tag("空间石（绑定）"), "空间石（绑定）")

    # ===== 调用点 =====

    def test_storage_handler_label_merges_element(self):
        handler = _imp("handlers.storage_ring_handler").StorageRingHandler(None, self.cm)
        label = handler._item_label("幽冥袍")
        # 五行并入同一个括号，避免出现两个连着的括号
        self.assertEqual(label.count("（"), 1, label)
        self.assertIn("装备·甲胄", label)
        self.assertIn("圣品", label)

    # ===== 品阶排序（储物戒清单） =====

    def test_rank_index_follows_rank_ladder(self):
        self.assertEqual(self.dis.rank_index("凡品"), 0)
        self.assertEqual(self.dis.rank_index("混元先天"), 11)
        self.assertLess(self.dis.rank_index("黄品"), self.dis.rank_index("地品"))
        self.assertLess(self.dis.rank_index("地品"), self.dis.rank_index("仙品"))
        # 历史名称走归一化，不因为写法不同掉到最后
        self.assertEqual(self.dis.rank_index("天阶"), self.dis.rank_index("圣品"))

    def test_rank_index_unknown_goes_last(self):
        self.assertEqual(self.dis.rank_index(""), -1)
        self.assertEqual(self.dis.rank_index(None), -1)
        self.assertEqual(self.dis.rank_index("不存在的品阶"), -1)

    def test_quality_index_order(self):
        self.assertLess(self.dis.quality_index("一般"), self.dis.quality_index("大师"))
        self.assertEqual(self.dis.quality_index(""), -1)

    def test_entry_sort_key_ranks_desc_with_quality(self):
        entries = [
            {"name": "凡物", "rank": "凡品", "quality": ""},
            {"name": "帝物", "rank": "帝品", "quality": ""},
            {"name": "大师甲", "rank": "天品", "quality": "大师"},
            {"name": "一般甲", "rank": "天品", "quality": "一般"},
            {"name": "无名", "rank": "", "quality": ""},
        ]
        ordered = sorted(entries, key=self.dis.entry_sort_key)
        self.assertEqual(
            [e["name"] for e in ordered],
            ["帝物", "大师甲", "一般甲", "凡物", "无名"],
        )

    def test_entry_sort_key_keeps_input_order_within_same_bucket(self):
        entries = [
            {"name": "甲", "rank": "天品", "quality": "一般"},
            {"name": "乙", "rank": "天品", "quality": "一般"},
            {"name": "丙", "rank": "天品", "quality": "一般"},
        ]
        ordered = sorted(entries, key=self.dis.entry_sort_key)
        self.assertEqual([e["name"] for e in ordered], ["甲", "乙", "丙"])


if __name__ == "__main__":
    unittest.main()
