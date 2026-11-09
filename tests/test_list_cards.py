# 发现型列表（秘境 / 历练）的卡片转换与网页端读取
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


def _item(title, facet, **kw):
    item = {
        "title": title,
        "facet": facet,
        "cmd": f"/探索秘境 {len(title)}",
        "rows": [["耗时", "20 分钟"]],
        "line": f"{title}  20分",
    }
    item.update(kw)
    return item


class TestPickRecommend(unittest.TestCase):
    """推荐位要覆盖不同产物，不能同类堆叠。"""

    def setUp(self):
        self.mod = _load("list_card_pick", "utils/list_card.py")

    def test_covers_each_facet_before_repeating(self):
        rows = (
            [_item(f"妖{i}", "妖兽") for i in range(4)]
            + [_item(f"草{i}", "灵植") for i in range(3)]
            + [_item(f"矿{i}", "矿材") for i in range(2)]
        )
        picked = self.mod.pick_recommend(rows, ("灵植", "矿材", "灵液", "妖兽", "全产物"), 5)
        facets = [it["facet"] for it in picked]
        # 三类各取到一条之后才轮到第二轮的重复
        self.assertEqual(sorted(set(facets)), ["妖兽", "灵植", "矿材"])
        self.assertEqual(len(picked), 5)

    def test_stops_when_rows_run_out(self):
        rows = [_item("孤本", "灵植")]
        picked = self.mod.pick_recommend(rows, ("灵植", "矿材"), 6)
        self.assertEqual(len(picked), 1)

    def test_zero_count_returns_empty(self):
        self.assertEqual(self.mod.pick_recommend([_item("a", "灵植")], ("灵植",), 0), [])


class TestToCardParts(unittest.TestCase):
    def setUp(self):
        self.mod = _load("list_card_parts", "utils/list_card.py")

    def _data(self):
        return {
            "subtitle": "金丹中期 · 可进入 2 档 / 未解锁 5 档",
            "facets": [
                {"key": "all", "label": "推荐", "count": 2},
                {"key": "全部", "label": "全部", "count": 2},
                {"key": "灵植", "label": "灵植", "count": 1},
            ],
            "groups": [
                {
                    "key": "open",
                    "name": "当前可进",
                    "collapsed": False,
                    "items": [
                        _item("青云秘境·二重", "灵植", recommend=True, badge="最佳"),
                        _item("万妖洞·一重", "妖兽", recommend=True),
                        _item("玄铁矿脉·一重", "矿材"),
                    ],
                },
                {
                    "key": "locked",
                    "name": "尚未解锁 · 5 档",
                    "collapsed": True,
                    "items": [_item(f"深处{i}", "妖兽") for i in range(5)],
                },
            ],
        }

    def test_image_shows_only_recommended(self):
        """图片只画推荐位：三十条铺进去看不清也滑不动。"""
        sections, _ = self.mod.to_card_parts(self._data(), "rift_list", top_n=8)
        open_section = sections[0]
        self.assertEqual(open_section["lines"][:2], ["青云秘境·二重  20分", "万妖洞·一重  20分"])
        # 另有 1 条（玄铁矿脉）没画，用汇总行交代
        self.assertTrue(any("另有 1 条" in ln for ln in open_section["lines"]))

    def test_collapsed_group_is_summary_only(self):
        sections, _ = self.mod.to_card_parts(self._data(), "rift_list")
        locked = sections[1]
        self.assertEqual(len(locked["lines"]), 1)
        self.assertIn("共 5 条", locked["lines"][0])

    def test_payload_keeps_every_item_for_web(self):
        _, payload = self.mod.to_card_parts(self._data(), "rift_list")
        self.assertEqual(payload["kind"], "rift_list")
        self.assertEqual(len(payload["groups"][0]["items"]), 3)
        self.assertEqual(payload["groups"][1]["collapsed"], True)
        self.assertTrue(payload["groups"][0]["items"][0]["recommend"])
        self.assertFalse(payload["groups"][0]["items"][2]["recommend"])

    def test_note_becomes_own_section(self):
        sections, _ = self.mod.to_card_parts(self._data(), "rift_list", note="★ 为效率档")
        self.assertEqual(sections[-1]["name"], "说明")
        self.assertEqual(sections[-1]["lines"], ["★ 为效率档"])

    def test_locked_flag_survives_to_payload(self):
        """未解锁行必须带 locked/lock_text，网页端据此把「进入」换成「需 X」。"""
        data = self._data()
        data["groups"][1]["items"][0] = _item("锁图", "妖兽", locked=True, lock_text="需合体期")
        _, payload = self.mod.to_card_parts(data, "rift_list")
        locked_item = payload["groups"][1]["items"][0]
        self.assertTrue(locked_item["locked"])
        self.assertEqual(locked_item["lock_text"], "需合体期")
        # 可进入段不受影响，仍然是可点的
        self.assertFalse(payload["groups"][0]["items"][0]["locked"])
        self.assertEqual(payload["groups"][0]["items"][0]["lock_text"], "")


class TestSidecarGroups(unittest.TestCase):
    """网页端要能读到 groups 卡（没有 sections 的老式结构）。"""

    def setUp(self):
        self.mod = _load("card_sidecar_groups", "webchat_room/card_sidecar.py")
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

    def test_groups_card_is_readable_without_sections(self):
        self._write({
            "id": "r1",
            "kind": "rift_list",
            "title": "秘境列表",
            "subtitle": "金丹中期",
            "sections": [],
            "facets": [{"key": "all", "label": "推荐", "count": 1}],
            "groups": [{
                "key": "open",
                "name": "当前可进",
                "collapsed": False,
                "items": [{
                    "title": "青云秘境·二重",
                    "badge": "最佳",
                    "cmd": "/探索秘境 4",
                    "facet": "灵植",
                    "recommend": True,
                    "rows": [["修为", "1.2 万"]],
                }],
            }],
        })
        card = self.mod.load_card_for_image(str(self.img))
        self.assertEqual(card["kind"], "rift_list")
        self.assertEqual(card["sections"], [])
        self.assertEqual(card["facets"][0]["label"], "推荐")
        item = card["groups"][0]["items"][0]
        self.assertEqual(item["cmd"], "/探索秘境 4")
        self.assertTrue(item["recommend"])
        self.assertEqual(item["rows"], [["修为", "1.2 万"]])

    def test_group_without_items_is_dropped(self):
        self._write({
            "id": "r2", "kind": "route_list", "title": "历练",
            "groups": [
                {"key": "open", "name": "可出发", "items": []},
                {"key": "locked", "name": "未解锁", "items": [{"title": "大道尽头"}]},
            ],
        })
        card = self.mod.load_card_for_image(str(self.img))
        self.assertEqual([g["key"] for g in card["groups"]], ["locked"])

    def test_locked_fields_are_kept(self):
        """未解锁标记不能在读取时被丢掉，否则网页端又会给它一个「进入」按钮。"""
        self._write({
            "id": "r4", "kind": "rift_list", "title": "秘境",
            "groups": [{
                "key": "locked", "name": "尚未解锁", "collapsed": True,
                "items": [{"title": "混沌天渊·三重", "locked": True, "lock_text": "需混元大罗金仙"}],
            }],
        })
        card = self.mod.load_card_for_image(str(self.img))
        item = card["groups"][0]["items"][0]
        self.assertTrue(item["locked"])
        self.assertEqual(item["lock_text"], "需混元大罗金仙")

    def test_card_with_neither_sections_nor_groups_is_ignored(self):
        self._write({"id": "r3", "kind": "rift_list", "title": "空卡"})
        self.assertIsNone(self.mod.load_card_for_image(str(self.img)))


if __name__ == "__main__":
    unittest.main()
