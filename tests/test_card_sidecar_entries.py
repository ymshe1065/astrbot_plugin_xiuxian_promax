# 卡片 sidecar 结构化条目（储物戒）透传
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    """按文件路径加载，避免拉起插件包的 astrbot 依赖。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestCardSidecarEntries(unittest.TestCase):
    def setUp(self):
        self.mod = _load("card_sidecar_probe", "webchat_room/card_sidecar.py")
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.json_path = Path(path)
        self.img = self.json_path.with_suffix(".jpg")
        self.img.write_bytes(b"stub")

    def tearDown(self):
        for p in (self.img, self.json_path):
            try:
                p.unlink()
            except OSError:
                pass

    def _write(self, payload):
        self.json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_entries_survive_normalization(self):
        """装备按品质拆开的条目不能在这一层被削掉，否则网页端只能退回解析文本。"""
        entries = [
            {"name": "幽冥袍", "kind": "装备", "sub": "甲胄", "rank": "圣品",
             "quality": "极佳", "element": "土", "count": 1,
             "instance_ids": [7], "equip_index": 2, "affix_count": 0},
            {"name": "幽冥袍", "kind": "装备", "sub": "甲胄", "rank": "圣品",
             "quality": "一般", "element": "土", "count": 2,
             "instance_ids": [5, 6], "equip_index": 1, "affix_count": 0},
        ]
        self._write({
            "id": "bag1", "bg": "bag", "title": "储物戒",
            "sections": [{
                "name": "物品清单",
                "rows": [["幽冥袍（装备·甲胄 | 圣品）·极佳", "x1"],
                         ["幽冥袍（装备·甲胄 | 圣品）·一般", "x2"]],
                "entries": entries,
            }],
        })

        card = self.mod.load_card_for_image(str(self.img))
        section = card["sections"][0]
        self.assertEqual([e["quality"] for e in section["entries"]], ["极佳", "一般"])
        self.assertEqual(section["entries"][1]["instance_ids"], [5, 6])
        # rows 仍按二元字符串保留，Pillow 与老前端都还能用
        self.assertEqual(section["rows"][0], ["幽冥袍（装备·甲胄 | 圣品）·极佳", "x1"])

    def test_legacy_card_has_no_entries(self):
        """老卡片没有 entries 时不凭空造字段，前端据此回退解析展示名。"""
        self._write({
            "id": "bag2", "bg": "bag", "title": "储物戒",
            "sections": [{"name": "物品清单", "rows": [["无极骨（材料·妖兽 | 玄品）", "x23"]]}],
        })
        card = self.mod.load_card_for_image(str(self.img))
        self.assertNotIn("entries", card["sections"][0])

    def test_entries_without_name_are_dropped(self):
        self._write({
            "id": "bag3", "bg": "bag", "title": "储物戒",
            "sections": [{
                "name": "物品清单",
                "rows": [["无极骨", "x1"]],
                "entries": [{"kind": "材料", "count": 1}, {"name": "无极骨", "count": 1}],
            }],
        })
        card = self.mod.load_card_for_image(str(self.img))
        self.assertEqual([e["name"] for e in card["sections"][0]["entries"]], ["无极骨"])


class TestPavilionItemPromotion(unittest.TestCase):
    """百宝阁按品阶分区，单区常常只有 1~2 件，不能因为件数少就退回纯文本。"""

    def setUp(self):
        self.mod = _load("card_sidecar_promo", "webchat_room/card_sidecar.py")

    def _promote(self, name, lines):
        section = {"name": name, "rows": [], "lines": list(lines)}
        self.mod._promote_lines_to_items(section)
        return section

    HUANG = [
        "皇品·噬血手套[火] - 369,413灵石 · 库存1",
        "品级: 皇品",
        "五行: 火",
        "描述: 攻击时吸取 8% 伤害转化为自身气血。",
        "属性: 境界基础物伤+6.0%、境界基础法伤+6.0%",
        "同属性加法叠加；每落后3级效率×80%，最低20%",
        "部位: 手套 · 适用境界: 元婴期初期-元婴期中期（体修：钢筋境初期-钢筋境中期）",
        "龙虎丹 - 292,075灵石 · 库存1",
        "品级: 皇品",
        "描述: 皇品修为丹药，龙虎交汇修为大进",
        "效果: 立即获得修为：+220000",
    ]

    def test_two_items_are_promoted(self):
        """两件商品的分区过去卡在「条目数 >= 3」的门槛上，整段平铺成纯文本。"""
        section = self._promote("皇品区", self.HUANG)
        self.assertEqual(len(section["items"]), 2)
        self.assertEqual(section["lines"], [])

    def test_single_item_is_promoted(self):
        section = self._promote("皇品区", self.HUANG[:7])
        self.assertEqual(len(section["items"]), 1)
        self.assertEqual(section["items"][0]["title"], "噬血手套[火]")
        self.assertIn(["五行", "火"], section["items"][0]["rows"])

    def test_rank_prefix_and_rank_row_are_deduped(self):
        """分区名已写品阶，标题前缀与「品级」行不再重复；原名留在 name 里。"""
        section = self._promote("皇品区", self.HUANG)
        first = section["items"][0]
        self.assertEqual(first["title"], "噬血手套[火]")
        self.assertEqual(first["name"], "皇品·噬血手套[火]")
        self.assertNotIn(["品级", "皇品"], first["rows"])
        # 不带前缀的条目保持原名，不凭空造 name
        self.assertNotIn("name", section["items"][1])

    def test_section_without_rank_keeps_row(self):
        section = self._promote("在售清单", self.HUANG)
        self.assertEqual(section["items"][0]["title"], "皇品·噬血手套[火]")
        self.assertIn(["品级", "皇品"], section["items"][0]["rows"])

    def test_plain_section_is_not_promoted(self):
        """普通分区里的数字不能被当成商品标题。"""
        section = self._promote("境界", [
            "等级 1 - 10",
            "修为 100/500",
            "灵石 1,200,000",
            "说明: 每提升一级增加少量属性",
        ])
        self.assertNotIn("items", section)
        self.assertEqual(len(section["lines"]), 4)



if __name__ == "__main__":
    unittest.main()
