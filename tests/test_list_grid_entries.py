# 第二批长清单的网格契约：丹药背包 / 宗门商店 / 仙缘商店 / 坊市 / 黑市 / 称号图鉴
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


CM = None


def _cm():
    global CM
    if CM is None:
        CM = _imp("config_manager").ConfigManager(ROOT)
    return CM


def _player(**kw):
    Player = _imp("models").Player
    p = Player(user_id="u1", user_name="道友", gold=100000, level_index=22)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


class TestPillBagEntries(unittest.TestCase):
    """丹药背包：一味丹 × 一个品质 = 一格，指令要能直接服下。"""

    def setUp(self):
        self.mgr = _imp("core.pill_manager").PillManager(None, _cm())

    def _entries(self, inv):
        player = _player()
        player.set_pills_inventory(inv)
        return self.mgr.build_pill_bag_entries(player), player

    def test_one_cell_per_quality(self):
        entries, _ = self._entries({"筑基丹|一般": 3, "筑基丹|良好": 2})
        self.assertEqual(len(entries), 2)
        self.assertEqual({e["kind"] for e in entries}, {"一般", "良好"})
        self.assertEqual({e["name"] for e in entries}, {"筑基丹"})
        # 品质既是第二行也是筛选维度
        self.assertEqual({e["facet"] for e in entries}, {"一般", "良好"})

    def test_cmd_carries_name_and_quality(self):
        entries, _ = self._entries({"筑基丹|大师": 1})
        e = entries[0]
        self.assertEqual(e["cmd"], "/服用丹药 筑基丹 大师")
        self.assertEqual(e["action"], "服用")
        self.assertEqual(e["source"], "pill")
        self.assertEqual(e["unit"], "颗")
        # 上限 = 这一摞的数量，抽屉里的「全部」不会超发
        self.assertEqual(e["max"], 1)
        self.assertEqual(e["held"], 1)

    def test_entries_are_quick_use(self):
        """丹药格子必须声明 quick：前端据此把单击从「开详情」改成「直接吞一颗」。

        少了这个字段，点格子弹的是详情抽屉，玩家要「点开 → 找服用 → 再点」三步，
        卡片上「点格子直接服用」就成了空话。
        """
        entries, _ = self._entries({"筑基丹|一般": 3})
        self.assertEqual(entries[0]["quick"], "服用")

    def test_rank_and_effect_from_config(self):
        entries, _ = self._entries({"筑基丹|一般": 1})
        e = entries[0]
        self.assertEqual(e["rank"], "灵品")
        self.assertTrue(e["effect"], "效果不能为空，格子里要看得出这颗丹干什么")
        self.assertEqual(e["effectLabel"], "效果")

    def test_unknown_pill_still_visible(self):
        """配置里没有的丹药（历史/外部导入）也得列出来，不能静默吞掉。"""
        entries, _ = self._entries({"来历不明丹|一般": 2})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["rank"], "凡品")
        self.assertEqual(entries[0]["effect"], "")

    def test_zero_and_bad_counts_skipped(self):
        entries, _ = self._entries({"筑基丹|一般": 0, "筑基丹|良好": "坏", "筑基丹|极佳": 2})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "极佳")

    def test_sorted_by_count_desc(self):
        entries, _ = self._entries({"筑基丹|一般": 1, "回春散|一般": 9})
        self.assertEqual([e["name"] for e in entries][0], "回春散")

    def test_empty_inventory(self):
        entries, _ = self._entries({})
        self.assertEqual(entries, [])


class TestPillBagQuickUse(unittest.TestCase):
    """点格子就直接吞一颗：前端那条快捷分支的守卫。

    真实页面上「点格子 → 弹详情 → 再点服用」是三步，玩家在背包里十次有九次
    只是想吃掉，所以 pill 条目声明 quick，单击直接发 /服用丹药。
    详情保留两条入口：⋯ 菜单（PC）/ 长按（触屏）。
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "webchat_room/static/app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "webchat_room/static/app.css").read_text(encoding="utf-8")

    def test_cell_has_quick_branch(self):
        self.assertIn("entry.quick && entry.state !== \"lock\"", self.js)
        self.assertIn("function quickRun(entry, cell)", self.js)
        # 非快捷格必须还是开详情，别把商店/称号的单击也改成「执行」
        self.assertIn("if (!quick) {\n        openDetail();", self.js)

    def test_quick_use_sends_cmd_not_detail(self):
        body = self.js[self.js.index("function quickRun"):]
        body = body[: body.index("\n  }")]
        self.assertIn("dispatchCommand(entry.cmd || entryPrimaryCommand(entry, \"1\"))", body)

    def test_long_press_opens_detail(self):
        """长按 550ms 开详情；长按之后那次 click 不能再吞一颗。"""
        self.assertIn("setTimeout(() => {", self.js)
        self.assertIn("}, 550);", self.js)
        self.assertIn("if (longPressed) {", self.js)

    def test_optimistic_decrement(self):
        """背包卡片不会因为吃药自动重刷，本地至少要扣掉已吃掉的那颗。"""
        body = self.js[self.js.index("function quickRun"):]
        body = body[: body.index("\n  }")]
        self.assertIn("entry.held = Math.max(0, held - 1)", body)
        self.assertIn("已用完", body)

    def test_styles_for_quick_and_used(self):
        self.assertIn(".gcell.quick::after", self.css)
        self.assertIn(".gcell.used", self.css)

    def test_long_press_has_visible_feedback(self):
        """长按必须看得见：只有 550ms 的沉默，玩家只会以为格子卡了。"""
        self.assertIn('class="holdhint"', self.js)
        self.assertIn("松手 · 选数量", self.js)
        self.assertIn('cell.classList.add("pressing")', self.js)
        self.assertIn(".gcell.pressing .holdhint", self.css)
        # 90ms 延迟 + 460ms 走满 = 550ms，与 app.js 的 pressTimer 对齐
        self.assertIn("hold-grow .46s linear .09s", self.css)
        self.assertIn("}, 550);", self.js)

    def test_press_hint_does_not_flash_on_tap(self):
        """轻点（<90ms）不该闪出覆盖层，所以淡入是延迟起跑的。"""
        self.assertIn("animation: hold-in .12s ease-out .09s both", self.css)
        # 底层三行文字在按住时退场，否则会透过覆盖层重影
        self.assertIn(".gcell.pressing > .gn", self.css)

    def test_press_overlay_removed_before_detail(self):
        """长按到点先摘掉 pressing 再开详情，遮罩不能压在抽屉底下。"""
        body = self.js[self.js.index("cell.addEventListener(\"pointerdown\""):]
        body = body[: body.index("}, 550);")]
        self.assertIn('cell.classList.remove("pressing")', body)
        self.assertLess(body.index('cell.classList.remove("pressing")'),
                        body.index("openDetail()"))

    def test_bag_card_text_mentions_long_press(self):
        """卡片文案要自己说清「长按多吃」，不能指望玩家摸出来。"""
        handler = (ROOT / "handlers/pill_handler.py").read_text(encoding="utf-8")
        self.assertIn("长按多吃", handler)
        self.assertIn("长按格子选数量", handler)


class TestSectShopEntries(unittest.TestCase):
    """宗门商店：价格换成商堂折扣后的贡献，兑换按编号。"""

    def setUp(self):
        self.handler = _imp("handlers.sect_shop_handler").SectShopHandler.__new__(
            _imp("handlers.sect_shop_handler").SectShopHandler
        )
        self.handler.shop_manager = _imp("core.shop_manager").ShopManager({}, _cm())
        self.handler.config = {}
        self.handler.sect_mgr = None
        self.player = _player(sect_contribution=99999)

    def _items(self, stock=(2, 0)):
        return [
            {"name": "回春散", "type": "pill", "rank": "凡品", "price": 1000,
             "stock": stock[0], "data": {"name": "回春散", "type": "pill"}},
            {"name": "玄铁", "type": "material", "rank": "玄品", "price": 2000,
             "stock": stock[1], "data": {"name": "玄铁", "type": "material"}},
        ]

    def test_index_is_the_command_arg(self):
        entries = self.handler._build_entries(None, self._items(), self.player)
        self.assertEqual([e["id"] for e in entries], [1, 2])
        self.assertEqual([e["cmd"] for e in entries],
                         ["/宗门兑换 1", "/宗门兑换 2"])

    def test_price_is_contribution_not_gold(self):
        entries = self.handler._build_entries(None, self._items(), self.player)
        self.assertEqual(entries[0]["currency"], "贡献")
        self.assertEqual(entries[0]["priceUnit"], "贡献")
        self.assertEqual(entries[0]["price"], 1000)

    def test_sold_out_is_locked(self):
        entries = self.handler._build_entries(None, self._items(), self.player)
        self.assertEqual([e["state"] for e in entries], ["ok", "lock"])
        self.assertEqual([e["stock"] for e in entries], [2, 0])

    def test_type_label_used(self):
        entries = self.handler._build_entries(None, self._items(), self.player)
        # pill 在宗门商店叫「破境丹」，和丹阁的标签表同一份
        self.assertEqual(entries[0]["kind"], "破境丹")
        self.assertEqual(entries[1]["kind"], "材料")

    def test_no_rank_dropped(self):
        items = self._items()
        entries = self.handler._build_entries(None, items, self.player)
        self.assertTrue(all(e["rank"] for e in entries))


class TestFateShopEntries(unittest.TestCase):
    """仙缘商店：按分组成 facet，满级的写「已满」且不带价。"""

    @classmethod
    def setUpClass(cls):
        cls.rules = _imp("core.reincarnation_rules")

    def test_all_items_listed(self):
        entries = self.rules.shop_entries({})
        self.assertEqual(len(entries), len(self.rules.SHOP_ITEMS))
        self.assertEqual({e["facet"] for e in entries},
                         {it["group"] for it in self.rules.SHOP_ITEMS.values()})

    def test_capped_item_has_no_price(self):
        bonus = {"equipment_tier_bonus": 1}   # 上限 1 阶
        entries = {e["id"]: e for e in self.rules.shop_entries(bonus)}
        capped = entries[32]
        self.assertEqual(capped["state"], "lock")
        self.assertEqual(capped["value"], "已满")
        self.assertNotIn("price", capped)
        self.assertNotIn("currency", capped)

    def test_normal_item_has_price_and_notes(self):
        entries = {e["id"]: e for e in self.rules.shop_entries({"global_cultivation_multiplier": 0.3})}
        e = entries[1]
        self.assertEqual(e["state"], "ok")
        self.assertEqual(e["price"], self.rules.shop_price(1, 3))
        self.assertEqual(e["currency"], "仙缘点")
        self.assertTrue(any("已购 3 层" in n for n in e["notes"]))

    def test_avatar_ladder_price(self):
        """身外化身走阶梯价：0/1/2 尊的下一份价格必须递增。"""
        序列 = []
        for owned in (0, 1, 2):
            bonus = {"avatar_slots": owned} if owned else {}
            entries = {e["id"]: e for e in self.rules.shop_entries(bonus)}
            序列.append(entries[52]["price"])
        self.assertEqual(序列, sorted(序列))
        self.assertEqual(len(set(序列)), 3)

    def test_cmd_is_shop_item_id(self):
        for e in self.rules.shop_entries({}):
            self.assertEqual(e["cmd"], f"/仙缘兑换 {e['id']}")

    def test_no_rank_field(self):
        """仙缘卖的是永久加成，没有品阶——前端就不画品阶色条。"""
        self.assertTrue(all("rank" not in e for e in self.rules.shop_entries({})))


class TestTitleEntries(unittest.TestCase):
    """称号图鉴：111 个称号按六品阶分档，已收集的才能点佩戴。"""

    @classmethod
    def setUpClass(cls):
        cls.mgr = _imp("managers.title_manager").TitleManager(None)
        cls.titles = _imp("managers.title_data").TITLES
        cls.tiers = _imp("managers.title_data").TIERS

    def _entries(self, owned):
        player = _player()
        player.permanent_bonus = json.dumps({"titles": owned})
        return self.mgr.build_collection_entries(player)

    def test_every_title_listed(self):
        entries = self._entries({})
        self.assertEqual(len(entries), len(self.titles))
        self.assertEqual(len({e["name"] for e in entries}), len(entries))

    def test_rank_is_tier_name(self):
        entries = self._entries({})
        names = {t[0] for t in self.tiers.values()}
        self.assertEqual({e["rank"] for e in entries}, names)

    def test_owned_can_equip(self):
        entries = {e["name"]: e for e in self._entries({"初入仙途": 1700000000})}
        self.assertEqual(entries["初入仙途"]["state"], "ok")
        self.assertEqual(entries["初入仙途"]["cmd"], "/佩戴称号 初入仙途")
        self.assertEqual(entries["初入仙途"]["facet"], "已收集")
        locked = entries["炼气小成"]
        self.assertEqual(locked["state"], "lock")
        self.assertNotIn("cmd", locked)
        self.assertEqual(locked["facet"], "未收集")

    def test_cell_shows_bonus(self):
        """格子第三行必须是加成，只有名字的格子等于没信息。"""
        entries = {e["name"]: e for e in self._entries({})}
        self.assertEqual(entries["初入仙途"]["value"], "修为+2%")
        self.assertEqual(entries["初入仙途"]["effectLabel"], "加成")

    def test_notes_have_unlock_condition(self):
        entries = {e["name"]: e for e in self._entries({})}
        self.assertTrue(any("解锁" in n for n in entries["初入仙途"]["notes"]))


class TestMarketEntries(unittest.IsolatedAsyncioTestCase):
    """坊市：总价 = 单价 × 数量，品阶回查配置补齐。"""

    class _Ring:
        config_manager = None

    class _DB:
        async def get_player_by_id(self, uid):
            return _player(user_id=uid, user_name="卖家甲")

    async def _entries(self, items):
        ring = self._Ring()
        ring.config_manager = _cm()
        mgr = _imp("managers.auction_manager").AuctionManager(self._DB(), ring, {})
        return await mgr.build_market_entries(items)

    async def test_total_price_and_count(self):
        entries = await self._entries([{
            "id": 7, "item_name": "玄铁", "base_item_name": "玄铁",
            "count": 3, "price": 1200, "item_kind": "item", "seller_id": "u9",
        }])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["price"], 3600)
        self.assertEqual(entries[0]["kind"], "×3")
        self.assertEqual(entries[0]["cmd"], "/坊市购买 7")

    async def test_rank_from_config(self):
        entries = await self._entries([{
            "id": 9, "item_name": "筑基丹·良好", "base_item_name": "筑基丹",
            "count": 1, "price": 8000, "item_quality": "良好",
            "item_kind": "pill", "seller_id": "u9",
        }])
        self.assertEqual(entries[0]["rank"], "灵品")

    async def test_seller_in_notes(self):
        entries = await self._entries([{
            "id": 1, "item_name": "玄铁", "base_item_name": "玄铁",
            "count": 1, "price": 10, "seller_id": "u9",
        }])
        self.assertTrue(any("卖家" in n for n in entries[0]["notes"]))

    async def test_recipe_scroll_effect_is_output(self):
        """坊市上架的丹方卷轴，格子第三行要写「炼出来干什么用」。"""
        entries = await self._entries([{
            "id": 11, "item_name": "《炼气丹方》", "base_item_name": "《炼气丹方》",
            "count": 1, "price": 50, "item_kind": "item", "seller_id": "u9",
        }])
        self.assertIn("修为", entries[0]["effect"])
        self.assertFalse(any(n.startswith("产出:") for n in entries[0]["notes"]))

    async def test_empty(self):
        self.assertEqual(await self._entries([]), [])


class TestBlackMarketEntries(unittest.TestCase):
    """黑市：编号即 /黑市购买 的参数；「品级:」前缀不在详情里重复。"""

    def setUp(self):
        self.mgr = _imp("managers.gamble_manager").GambleManager(None, _cm(), None)

    def test_entries_match_text_list(self):
        entries = self.mgr.build_black_market_entries()
        _ok, text = self.mgr.get_black_market()
        self.assertEqual(len(entries), 3)
        for e in entries:
            self.assertIn(f"[{e['id']}]", text)
            self.assertEqual(e["cmd"], f"/黑市购买 {e['id']}")

    def test_effect_strips_rank_prefix(self):
        for e in self.mgr.build_black_market_entries():
            self.assertFalse(e["effect"].startswith("品级:"), e["effect"])


class TestFacetPayload(unittest.TestCase):
    """sidecar 只在声明了 facet 时才写这个键：老卡片少一个字段，前端走旧分支。"""

    @classmethod
    def setUpClass(cls):
        cls.gen = _imp("utils.image_generator")

    def _payload(self, sections):
        return self.gen._build_card_payload("id1", "shop", "t", "s", sections, "")

    def test_facet_kept_when_declared(self):
        payload = self._payload([{
            "name": "丹药", "view": "grid", "entries": [{"name": "丹"}],
            "facet": {"label": "品质", "okLabel": "可用"},
        }])
        self.assertEqual(payload["sections"][0]["facet"],
                         {"label": "品质", "okLabel": "可用"})

    def test_facet_absent_by_default(self):
        payload = self._payload([{"name": "已学丹方", "view": "grid", "entries": [{"name": "丹"}]}])
        self.assertNotIn("facet", payload["sections"][0])

    def test_empty_facet_not_written(self):
        payload = self._payload([{"name": "x", "view": "grid", "entries": [], "facet": {}}])
        self.assertNotIn("facet", payload["sections"][0])

    def test_entries_fields_survive(self):
        payload = self._payload([{
            "name": "商品", "view": "grid",
            "entries": [{"name": "修炼加速", "price": 100, "currency": "仙缘点",
                         "priceUnit": "点", "facet": "修为", "value": "已满"}],
        }])
        e = payload["sections"][0]["entries"][0]
        self.assertEqual(e["currency"], "仙缘点")
        self.assertEqual(e["priceUnit"], "点")
        self.assertEqual(e["facet"], "修为")


class TestWebOnlyCard(unittest.TestCase):
    """render_web_card 只在网页端（位图关）出卡；QQ 端返回 None 走原文。"""

    @classmethod
    def setUpClass(cls):
        cls.gen = _imp("utils.image_generator")

    def test_returns_none_when_bitmap_enabled(self):
        self.assertTrue(self.gen.render_bitmap_enabled())
        self.assertIsNone(
            self.gen.render_web_card("bag", "丹药背包", "", [{"name": "丹药", "view": "grid",
                                                             "entries": [{"name": "丹"}]}])
        )

    def test_writes_sidecar_when_bitmap_off(self):
        sections = [{"name": "丹药", "view": "grid",
                     "entries": [{"name": "回春散", "kind": "一般"}]}]
        token = self.gen.set_render_bitmap(False)
        try:
            path = self.gen.render_web_card("bag", "丹药背包", "副标题", sections, "脚注")
        finally:
            self.gen.reset_render_bitmap(token)
        self.assertIsNotNone(path)
        json_path = Path(path).with_suffix(".json")
        self.assertTrue(json_path.is_file())
        self.assertFalse(Path(path).exists(), "网页端不该再落 jpg")
        card = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(card["title"], "丹药背包")
        self.assertEqual(card["sections"][0]["view"], "grid")
        self.assertEqual(card["sections"][0]["entries"][0]["name"], "回春散")
        json_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
