# 网格条目的数据契约：后端多加一条 entries 旁路，文本一个字都不能变
import importlib.util
import json
import os
import sys
import tempfile
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


def _load_file(name: str, rel: str):
    """按文件路径加载，避免为 sidecar 拉起整个插件包。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeDB:
    def __init__(self, player):
        self.player = player

    async def get_player_by_id(self, user_id):
        return self.player if user_id == self.player.user_id else None


class _FakeStorage:
    """储物戒替身：只认「灵植」类材料，用来制造「缺料」状态。"""

    def __init__(self, owned=None):
        self.owned = owned or {}

    def get_item_count(self, player, item_name):
        return int(self.owned.get(item_name, 0))

    def ring_items(self, player):
        return dict(self.owned)


class TestRecipeEntries(unittest.IsolatedAsyncioTestCase):
    """丹方：entries 与卡片行同源，行文本仍是老样子。"""

    @classmethod
    def setUpClass(cls):
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.recipes = json.loads((ROOT / "config" / "pill_recipes.json").read_text(encoding="utf-8"))

    async def _fetch(self, ids, owned=None):
        Player = _imp("models").Player
        player = Player(user_id="u1", user_name="道友", learned_recipes=json.dumps(ids))
        mgr = _imp("managers.alchemy_manager").AlchemyManager(
            _FakeDB(player), self.cm, _FakeStorage(owned)
        )
        return await mgr.get_available_recipes("u1"), player

    async def test_entries_one_per_recipe(self):
        ids = [r["id"] for r in self.recipes[:12]]
        (ok, text, rows, card_lines, entries), _player = await self._fetch(ids)
        self.assertTrue(ok)
        self.assertEqual(len(entries), len(ids))
        self.assertEqual(len(entries), len(rows))
        for entry in entries:
            self.assertTrue(entry["name"])
            self.assertIn(entry["rank"], _imp("rank_rules").RANK_NAMES)
            self.assertIn(entry["state"], ("ok", "lack", "lock"))
            self.assertEqual(entry["source"], "recipe")
            self.assertEqual(entry["kind"], "丹方")

    async def test_lines_are_untouched(self):
        """QQ 端吃的是 card_lines：多一条 entries 不许改动它们。"""
        ids = [r["id"] for r in self.recipes[:6]]
        (_ok, _text, _rows, card_lines, entries), _player = await self._fetch(ids)
        joined = "\n".join(card_lines)
        for entry in entries:
            self.assertIn(f"[{entry['id']}] {entry['name']}（", joined)
            self.assertIn("效果：", joined)
        # 一条配方固定 2 行起步（标题 + 效果），后面按材料数追加
        self.assertGreaterEqual(len(card_lines), len(entries) * 2)

    async def test_missing_material_marks_lack(self):
        # 只挑 0 级就能炼的：境界不足会先判 lock，那是另一条分支
        ids = [
            r["id"] for r in self.recipes
            if r.get("materials") and int(r.get("level_required", 0) or 0) == 0
        ][:5]
        (_ok, _t, _r, _c, entries), _player = await self._fetch(ids, owned={})
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual(entry["state"], "lack")
            self.assertTrue(any(m["have"] < m["need"] for m in entry["materials"]))

    async def test_level_gate_marks_lock(self):
        # level_required 最高的那条，1 级角色一定没到
        target = max(self.recipes, key=lambda r: int(r.get("level_required", 0) or 0))
        (_ok, _t, _r, _c, entries), _player = await self._fetch([target["id"]])
        self.assertEqual(entries[0]["state"], "lock")
        self.assertEqual(entries[0]["level"], int(target.get("level_required", 0) or 0))

    async def test_max_qty_is_min_of_materials_and_gold(self):
        """详情抽屉的数量上限：材料与工费里「够几次」的最小值。

        给多了就是让玩家点出必然失败的一次，所以必须等于能真的炼出来的次数。
        """
        ids = [r["id"] for r in self.recipes[:12]]
        (_ok, _t, _r, _c, entries), _player = await self._fetch(ids, owned={"凡品灵植": 7})
        for entry in entries:
            self.assertIn("max", entry)
            rounds = [m["have"] // m["need"] for m in entry["materials"] if m["need"]]
            if entry["cost"]:
                rounds.append(int(entry["gold"] or 0) // int(entry["cost"]))
            if not rounds:
                self.assertIsNone(entry["max"])
                continue
            self.assertEqual(entry["max"], min(rounds), entry["name"])


class TestPavilionEntries(unittest.TestCase):
    """商店四阁：货架 dict 直接摊成条目，售罄的不进网格。"""

    @classmethod
    def setUpClass(cls):
        cls.cm = _imp("config_manager").ConfigManager(ROOT)
        cls.mgr = _imp("core.shop_manager").ShopManager({}, cls.cm)

    def _items(self):
        return [
            {"name": "回春散方", "type": "recipe", "rank": "凡品", "price": 380,
             "stock": 3, "discount": 0.95, "data": {"desc": "恢复气血 300~500"}},
            {"name": "九转还魂丹方", "type": "recipe", "rank": "圣品", "price": 8800,
             "stock": 1, "discount": 1.0, "data": {"desc": "复活并恢复六成气血"}},
            {"name": "卖光了", "type": "recipe", "rank": "凡品", "price": 100,
             "stock": 0, "discount": 1.0, "data": {}},
        ]

    def test_sold_out_items_are_dropped(self):
        entries = self.mgr.build_pavilion_entries(self._items())
        self.assertEqual([e["name"] for e in entries], ["回春散方", "九转还魂丹方"])
        for entry in entries:
            self.assertGreater(entry["stock"], 0)
            self.assertIn(entry["rank"], _imp("rank_rules").RANK_NAMES)
            self.assertEqual(entry["source"], "shop")
            self.assertEqual(entry["action"], "购买")

    def test_discount_and_price_survive(self):
        entries = self.mgr.build_pavilion_entries(self._items())
        self.assertAlmostEqual(entries[0]["discount"], 0.95)
        self.assertEqual(entries[0]["price"], 380)
        self.assertEqual(entries[1]["discount"], 1.0)

    def test_legacy_rank_is_normalized(self):
        items = [{"name": "旧货", "type": "recipe", "rank": "天阶", "price": 10,
                  "stock": 1, "discount": 1.0, "data": {}}]
        entries = self.mgr.build_pavilion_entries(items)
        self.assertEqual(entries[0]["rank"], "圣品")

    def test_max_qty_follows_stock(self):
        """购买数量上限取库存；不限量的给 None，交给后端判。"""
        items = self._items() + [
            {"name": "无限量灵草", "type": "material", "rank": "凡品", "price": 10,
             "stock": 999, "unlimited": True, "discount": 1.0, "data": {}},
        ]
        entries = self.mgr.build_pavilion_entries(items)
        by_name = {e["name"]: e for e in entries}
        self.assertEqual(by_name["回春散方"]["max"], 3)
        self.assertEqual(by_name["九转还魂丹方"]["max"], 1)
        self.assertIsNone(by_name["无限量灵草"]["max"])

    def test_effect_line_not_repeated_in_notes(self):
        """「描述:」那行升级成了产出效果，明细里不能再出现一遍。"""
        src = self.cm.items_data.get("《炼气丹方》")
        self.assertTrue(src, "配置里没有《炼气丹方》，换一条真实丹方")
        items = [{"name": src["name"], "type": "recipe", "rank": src.get("rank", "凡品"),
                  "price": 50, "stock": 3, "discount": 1.0, "data": dict(src)}]
        entry = self.mgr.build_pavilion_entries(items)[0]
        self.assertTrue(entry["effect"])
        self.assertIn("丹方", entry["effect"])
        for note in entry["notes"]:
            self.assertNotIn(entry["effect"], note)


class TestTalismanBookEntries(unittest.TestCase):
    """符箓配方总览：拿不到背包，材料只能报需量，不能谎称「缺 N」。"""

    def test_recipe_page_materials_have_unknown_count(self):
        cm = _imp("config_manager").ConfigManager(ROOT)
        mgr = _imp("managers.talisman_manager").TalismanManager(None, cm, None)
        spec = mgr.build_recipe_page()
        sections = spec["web_sections"]
        self.assertEqual(len(sections), 2, "说明 + 网格")
        self.assertEqual(sections[1]["view"], "grid")
        entries = sections[1]["entries"]
        self.assertEqual(len(entries), len(mgr.iter_recipes()))
        for entry in entries:
            self.assertEqual(entry["source"], "talisman_recipe")
            for mat in entry["materials"]:
                self.assertIsNone(mat["have"], "总览页不该给出「有多少」")
                self.assertGreater(mat["need"], 0)


class TestGridSidecarRoundTrip(unittest.TestCase):
    """view=grid 必须原样到网页端，且不能被「文本提升条目」覆盖。"""

    def setUp(self):
        self.mod = _load_file("card_sidecar_grid", "webchat_room/card_sidecar.py")
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

    def test_grid_view_survives(self):
        payload = {
            "id": "craft1", "bg": "craft", "title": "已学丹方",
            "sections": [{
                "name": "已学丹方",
                "view": "grid",
                "rows": [],
                "lines": ["[1] 炼气丹方（可炼制 · 成功率 85%）"],
                "entries": [{"name": "炼气丹方", "rank": "凡品", "state": "ok", "source": "recipe"}],
            }],
        }
        self.json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        card = self.mod.load_card_for_image(str(self.img))
        section = card["sections"][0]
        self.assertEqual(section["view"], "grid")
        self.assertEqual(section["entries"][0]["name"], "炼气丹方")
        # 网格视图不吃「文本提升」：否则前端会先命中 items 分支，格子永远出不来
        self.assertNotIn("items", section)

    def test_entries_only_section_is_kept(self):
        """商店货架的网页分区只有 entries、没有 lines，不能被当成空分区丢掉。"""
        payload = {
            "id": "shop1", "bg": "shop", "title": "丹阁",
            "sections": [{
                "name": "在售清单",
                "view": "grid",
                "rows": [],
                "lines": [],
                "entries": [{"name": "回春散方", "rank": "凡品", "price": 380, "stock": 3}],
            }],
        }
        self.json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        card = self.mod.load_card_for_image(str(self.img))
        self.assertIsNotNone(card)
        self.assertEqual(card["sections"][0]["entries"][0]["price"], 380)


if __name__ == "__main__":
    unittest.main()
