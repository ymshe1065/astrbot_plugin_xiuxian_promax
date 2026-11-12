# -*- coding: utf-8 -*-
"""仙缘【一心二用】：忙碌状态下的并行指令放行规则。"""

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


def _imp(p):
    return __import__(f"{PKG}.{p}", fromlist=["x"])


class _ParallelBase(unittest.TestCase):
    def setUp(self):
        self.rules = _imp("core.reincarnation_rules")
        self.parallel = _imp("core.parallel_rules")
        self.Player = _imp("models").Player

    def _player(self, level):
        bonus = {}
        for _ in range(level):
            self.rules.add_item(bonus, self.parallel.PARALLEL_ITEM_ID)
        return self.Player(
            user_id="u1",
            user_name="道友",
            permanent_bonus=json.dumps(bonus, ensure_ascii=False),
        )

    def _allowed(self, level, command):
        ok, _ = self.parallel.can_act_while_busy(self._player(level), command)
        return ok


class TestParallelCatalog(_ParallelBase):
    def test_item_metadata(self):
        item = self.rules.SHOP_ITEMS[self.parallel.PARALLEL_ITEM_ID]
        self.assertEqual(item["name"], "一心二用")
        self.assertEqual(item["group"], "便利")
        self.assertEqual(item["cost"], 80)
        self.assertEqual(item["cap"], 3)
        self.assertIn(self.parallel.PARALLEL_ITEM_ID, self.rules.items_in_group("便利"))

    def test_tier_names_cover_three_levels(self):
        self.assertEqual(sorted(self.parallel.TIER_NOTES), [1, 2, 3])
        self.assertEqual(len(self.parallel.allowed_commands(1)), len(self.parallel.TIER1_COMMANDS))
        self.assertEqual(
            len(self.parallel.allowed_commands(2)),
            len(self.parallel.TIER1_COMMANDS) + len(self.parallel.TIER2_COMMANDS),
        )
        # 第 3 档是「其余全部」，不再靠指令清单
        self.assertEqual(self.parallel.allowed_commands(3), ())

    def test_tier_lists_have_no_duplicates(self):
        for name, pool in (
            ("TIER1", self.parallel.TIER1_COMMANDS),
            ("TIER2", self.parallel.TIER2_COMMANDS),
            ("FORBIDDEN", self.parallel.PARALLEL_FORBIDDEN),
        ):
            self.assertEqual(len(pool), len(set(pool)), f"{name} 有重复指令")
        overlap = set(self.parallel.TIER1_COMMANDS) & set(self.parallel.TIER2_COMMANDS)
        self.assertFalse(overlap, f"两档清单重叠：{overlap}")

    def test_forbidden_never_appears_in_tiers(self):
        pooled = set(self.parallel.TIER1_COMMANDS) | set(self.parallel.TIER2_COMMANDS)
        overlap = pooled & set(self.parallel.PARALLEL_FORBIDDEN)
        self.assertFalse(overlap, f"禁止项混进了放行清单：{overlap}")


class TestParallelGating(_ParallelBase):
    def test_no_purchase_blocks_everything(self):
        for cmd in ("炼丹", "坊市购买 玄铁", "切磋", "突破"):
            self.assertFalse(self._allowed(0, cmd), cmd)
            ok, hint = self.parallel.can_act_while_busy(self._player(0), cmd)
            self.assertFalse(ok)
            self.assertEqual(hint, "", "没买时不该改变原始拦截文案")

    def test_tier1_covers_chores(self):
        for cmd in ("炼丹", "储物戒附魔 3", "强化 屠龙刀", "种植 灵草 all",
                    "喂养灵兽 小白", "洞天收取", "仙缘兑换 灵石 10", "服用丹药 回春散"):
            self.assertTrue(self._allowed(1, cmd), cmd)

    def test_tier1_blocks_trade(self):
        self.assertFalse(self._allowed(1, "坊市购买 玄铁"))
        self.assertFalse(self._allowed(1, "存灵石 100"))
        ok, hint = self.parallel.can_act_while_busy(self._player(1), "坊市")
        self.assertFalse(ok)
        self.assertIn("第 2 档", hint)

    def test_tier2_adds_trade_and_social(self):
        for cmd in ("坊市购买 玄铁", "存灵石 100", "拜师 张三", "我的宗门",
                    "百宝阁", "拍卖会出价 1 5000", "境界排行"):
            self.assertTrue(self._allowed(2, cmd), cmd)
        # 战斗与其余杂项留到第 3 档
        self.assertFalse(self._allowed(2, "切磋 张三"))
        self.assertFalse(self._allowed(2, "突破"))

    def test_tier3_allows_everything_else(self):
        for cmd in ("切磋 张三", "突破", "挑战世界Boss", "行善", "一键日常", "改道号 云中君"):
            self.assertTrue(self._allowed(3, cmd), cmd)

    def test_tier_is_additive(self):
        """第 2 档仍然能用第 1 档的指令。"""
        self.assertTrue(self._allowed(2, "炼丹"))
        self.assertTrue(self._allowed(3, "炼丹"))
        self.assertTrue(self._allowed(3, "坊市购买 玄铁"))


class TestParallelForbidden(_ParallelBase):
    def test_activity_starts_stay_blocked_at_tier3(self):
        for cmd in ("闭关", "开始历练 落霞谷", "探索秘境 1", "押镖",
                    "宗门任务", "调息", "周本确认"):
            self.assertFalse(self._allowed(3, cmd), cmd)
            ok, hint = self.parallel.can_act_while_busy(self._player(3), cmd)
            self.assertFalse(ok)
            self.assertIn("活动位只有一个", hint)

    def test_life_reset_stays_blocked_at_tier3(self):
        for cmd in ("飞升", "弃道重修", "我要修仙 剑修"):
            self.assertFalse(self._allowed(3, cmd), cmd)

    def test_forbidden_is_reported_even_when_unpurchased(self):
        """没买的时候按原始文案拦截，不额外解释。"""
        ok, hint = self.parallel.can_act_while_busy(self._player(0), "闭关")
        self.assertFalse(ok)
        self.assertEqual(hint, "")

    def test_forbidden_prefix_does_not_leak(self):
        """「宗门任务」被禁，但「宗门任务奖励」这类不该被顺带放行到第 2 档。"""
        self.assertFalse(self._allowed(2, "宗门任务"))
        # 「宗门商店」是另一条指令，第 2 档正常放行
        self.assertTrue(self._allowed(2, "宗门商店"))


class TestParallelNormalize(_ParallelBase):
    def test_slash_and_prefix_are_stripped(self):
        for raw in ("/炼丹", "  /炼丹  ", "[主人(123456)] /炼丹", "@某某 /炼丹"):
            self.assertTrue(self._allowed(1, raw), raw)

    def test_empty_command_is_blocked(self):
        ok, hint = self.parallel.can_act_while_busy(self._player(3), "   ")
        self.assertFalse(ok)
        self.assertEqual(hint, "")

    def test_broken_bonus_does_not_raise(self):
        player = self.Player(user_id="u1", user_name="道友", permanent_bonus="{not json")
        ok, _ = self.parallel.can_act_while_busy(player, "炼丹")
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# 装饰器接线：确认 player_required 真的按并行档位放行 / 拦截
# ---------------------------------------------------------------------------

class _Event:
    def __init__(self, text):
        self._text = text

    def get_sender_id(self):
        return "u1"

    def get_message_str(self):
        return self._text

    def plain_result(self, text):
        return text


class _FakeExt:
    def __init__(self, cd):
        self._cd = cd

    async def get_user_cd(self, user_id):
        return self._cd

    async def get_active_loan(self, user_id):
        return None

    async def settle_sect_debt(self, user_id):
        return 0


class _FakeDB:
    def __init__(self, player, cd):
        self.player = player
        self.ext = _FakeExt(cd)

    async def get_player_by_id(self, user_id):
        return self.player


class _Handler:
    """最小可用的 handler：只需要 db 与被装饰的方法。"""

    def __init__(self, db):
        self.db = db
        self.hit = False


class TestPlayerRequiredWiring(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.utils = _imp("handlers.utils")
        self.rules = _imp("core.reincarnation_rules")
        self.parallel = _imp("core.parallel_rules")
        self.Player = _imp("models").Player
        self.UserStatus = _imp("models_extended").UserStatus

    def _handler(self, level):
        bonus = {}
        for _ in range(level):
            self.rules.add_item(bonus, self.parallel.PARALLEL_ITEM_ID)
        player = self.Player(
            user_id="u1",
            user_name="道友",
            permanent_bonus=json.dumps(bonus, ensure_ascii=False),
        )
        cd = type("CD", (), {})()
        cd.type = self.UserStatus.ADVENTURING
        cd.scheduled_time = 0
        handler = _Handler(_FakeDB(player, cd))

        async def body(_self, player, event, *args, **kwargs):
            _self.hit = True
            yield event.plain_result("✅ 执行成功")

        handler.act = self.utils.player_required(body).__get__(handler, _Handler)
        return handler

    async def _run(self, level, command):
        handler = self._handler(level)
        out = [r async for r in handler.act(_Event(command))]
        return handler.hit, out

    async def test_unpurchased_keeps_original_block_message(self):
        hit, out = await self._run(0, "炼丹")
        self.assertFalse(hit)
        self.assertIn("无法分心他顾", out[0])

    async def test_purchased_allows_chores(self):
        hit, out = await self._run(1, "炼丹")
        self.assertTrue(hit)
        self.assertEqual(out[0], "✅ 执行成功")

    async def test_purchased_still_blocks_activity_start(self):
        hit, out = await self._run(3, "闭关")
        self.assertFalse(hit)
        self.assertIn("活动位只有一个", out[0])

    async def test_tier_hint_replaces_generic_message(self):
        hit, out = await self._run(1, "坊市购买 玄铁")
        self.assertFalse(hit)
        self.assertIn("第 2 档", out[0])


if __name__ == "__main__":
    unittest.main()
