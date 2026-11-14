# tests/test_long_list_dual_platform.py
# 长清单网格化的「双平台契约」：同一份代码在 QQ / 网页两端各走一条路。
#
# 为什么单独写：之前的用例全部跑在默认的 bitmap=True 下，也就是只走 QQ 分支。
# `_FakeEvent` 连 image_result 都没有，webroom 那条分支在单测里从未执行过——
# 而本轮改动的全部风险恰好在那里（出不出格、会不会多刷一条纯文本）。
#
# 三条红线：
#   1. QQ 端（bitmap=True）必须是纯文本，且文本与改造前逐字一致；
#   2. 网页端（bitmap=False）必须出结构化卡片，且**不额外补一条纯文本**；
#   3. 两端的文本内容必须同源（网页端丢弃的文本，不能在 QQ 端被悄悄改掉）。
import json
import unittest
from pathlib import Path

from test_equipment_instances import _imp


class _DualEvent:
    """同时支持 plain_result 与 image_result 的事件桩，并记录走了哪条路。"""

    def __init__(self, user_id, name="道友", text=""):
        self._user_id = user_id
        self._name = name
        self._text = text
        self.plain_calls = []
        self.card_calls = []

    def get_sender_id(self):
        return self._user_id

    def get_sender_name(self):
        return self._name

    def get_message_str(self):
        return self._text

    def plain_result(self, text):
        self.plain_calls.append(text)
        return text

    def image_result(self, card):
        self.card_calls.append(card)
        return card


def _img():
    return _imp("utils.image_generator")


class TestPillBagDualPlatform(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_equipment_instances import TestEquipmentInstances

        self._case = TestEquipmentInstances(methodName="runTest")
        await self._case.asyncSetUp()
        self.db = self._case.db
        self.cm = self._case.cm
        self.handler = _imp("handlers.pill_handler").PillHandler(self.db, self.cm)

        player = await self._case._player(gold=1000)
        inv = player.get_pills_inventory()
        inv["炼气丹|一般"] = 3
        inv["炼气丹|良好"] = 1
        inv["固基丹|极佳"] = 2
        player.set_pills_inventory(inv)
        await self.db.update_player(player)
        self.player = await self.db.get_player_by_id(player.user_id)

    async def asyncTearDown(self):
        await self._case.asyncTearDown()

    async def _run(self, event):
        return [r async for r in self.handler.handle_show_pills(event)]

    async def test_qq_mode_yields_plain_text(self):
        """默认（QQ / 群）走纯文本，且不产生任何卡片。"""
        ev = _DualEvent(self.player.user_id)
        out = await self._run(ev)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(ev.card_calls), 0, "QQ 端不该出卡片")
        self.assertIn("炼气丹", out[0])
        self.assertIn("一般×3", out[0])

    async def test_webroom_mode_yields_card_without_extra_text(self):
        """网页端出卡片，而且**只有**卡片——多一条纯文本就是刷屏。

        注意：这里的「卡片」是 sidecar 路径（.jpg 名 + 不存在的文件 + 同名 .json），
        webroom 下 render_card 只写结构化数据、不碰 Pillow，返回路径只用于前端拼 id。
        """
        img = _img()
        token = img.set_render_bitmap(False)
        try:
            ev = _DualEvent(self.player.user_id)
            out = await self._run(ev)
        finally:
            img.reset_render_bitmap(token)

        self.assertEqual(len(out), 1, f"网页端应只产出 1 条结果，实际 {len(out)}")
        self.assertEqual(len(ev.card_calls), 1, "应产出 1 张卡片")
        self.assertEqual(ev.plain_calls, [], "网页端不该再补纯文本")

        card_path = Path(ev.card_calls[0])
        self.assertEqual(card_path.suffix, ".jpg")
        self.assertFalse(card_path.exists(), "网页端不该真的画位图（这轮性能改造的核心）")

        payload = json.loads(card_path.with_suffix(".json").read_text(encoding="utf-8"))
        sec = payload["sections"][0]
        self.assertEqual(sec["view"], "grid")
        # 一味丹 × 一个品质 = 一格
        self.assertEqual(len(sec["entries"]), 3)
        names = {(e["name"], e["kind"]) for e in sec["entries"]}
        self.assertIn(("炼气丹", "一般"), names)
        self.assertIn(("炼气丹", "良好"), names)
        # 条目必须带完整指令，前端点格子才能直接下单
        self.assertTrue(all(e.get("cmd", "").startswith("/服用丹药") for e in sec["entries"]))

    async def test_text_identical_across_platforms(self):
        """文本路径不受旁路影响：网页端丢掉的文本，必须与 QQ 端收到的完全一致。"""
        qq_text = await self.handler._render_pill_bag(self.player)

        img = _img()
        token = img.set_render_bitmap(False)
        try:
            web_text = await self.handler._render_pill_bag(self.player)
        finally:
            img.reset_render_bitmap(token)

        self.assertEqual(qq_text, web_text, "开关改变了文本产出，QQ 端文案被动了")

    async def test_empty_bag_still_plain_on_webroom(self):
        """空背包没有格子可排，网页端也必须回退到文本，不能出空卡片。"""
        empty = await self._case._player(uid="u_empty", gold=1000)
        empty.set_pills_inventory({})
        await self.db.update_player(empty)
        empty = await self.db.get_player_by_id(empty.user_id)

        img = _img()
        token = img.set_render_bitmap(False)
        try:
            ev = _DualEvent(empty.user_id)
            out = [r async for r in self.handler.handle_show_pills(ev)]
        finally:
            img.reset_render_bitmap(token)

        self.assertEqual(len(out), 1)
        self.assertEqual(len(ev.card_calls), 0, "空背包不该出空卡片")
        self.assertIn("丹药背包是空的", out[0])


class TestFateShopPriceMatchesCharge(unittest.IsolatedAsyncioTestCase):
    """配平后的价格必须「看得见 = 扣得着」，且满级项既无价也不可买。"""

    async def asyncSetUp(self):
        from test_equipment_instances import TestEquipmentInstances

        self._case = TestEquipmentInstances(methodName="runTest")
        await self._case.asyncSetUp()
        self.db = self._case.db
        self.cm = self._case.cm
        self.rules = _imp("core.reincarnation_rules")

    async def asyncTearDown(self):
        await self._case.asyncTearDown()

    def test_every_entry_price_equals_shop_price(self):
        """网格里写的价，必须就是 shop_price 算出来的价（同一来源）。"""
        entries = self.rules.shop_entries({})
        by_id = {int(e["id"]): e for e in entries}
        for item_id, item in self.rules.SHOP_ITEMS.items():
            e = by_id[item_id]
            if e.get("state") == "lock":
                self.assertNotIn("price", e, f"{item_id} 已满却还带价")
                continue
            self.assertEqual(
                e["price"], self.rules.shop_price(item_id, 0),
                f"{item_id} {item['name']} 展示价与 shop_price 不一致",
            )

    async def test_charge_equals_display(self):
        """真买一次：扣掉的点数必须等于格子上的价。"""
        mgr = _imp("managers.reincarnation_manager").ReincarnationManager(
            self.db, self.cm, self._case.ring
        )
        player = await self._case._player()
        player.fate_points = 10000
        item_id = 1  # 修炼加速，锚
        price = self.rules.shop_price(item_id, 0)

        ok, msg = await mgr.buy_fate_shop_item(player, item_id)
        self.assertTrue(ok, msg)
        self.assertEqual(player.fate_points, 10000 - price)

    async def test_dual_cultivation_capped_at_five(self):
        """双修心得限购 5 层：第 6 次应被拒，且不扣点。"""
        mgr = _imp("managers.reincarnation_manager").ReincarnationManager(
            self.db, self.cm, self._case.ring
        )
        player = await self._case._player()
        player.fate_points = 100000
        for i in range(5):
            ok, msg = await mgr.buy_fate_shop_item(player, 11)
            self.assertTrue(ok, f"第 {i + 1} 层应可买：{msg}")
        before = player.fate_points
        ok, msg = await mgr.buy_fate_shop_item(player, 11)
        self.assertFalse(ok, "第 6 层应被上限拦住")
        self.assertIn("已满", msg)
        self.assertEqual(player.fate_points, before, "被拒时不能扣点")


if __name__ == "__main__":
    unittest.main()
