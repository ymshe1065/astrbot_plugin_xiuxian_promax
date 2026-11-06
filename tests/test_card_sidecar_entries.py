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


if __name__ == "__main__":
    unittest.main()
