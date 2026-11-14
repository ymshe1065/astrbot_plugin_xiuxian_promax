# tests/test_avatar_slots.py
# v3.45.12 身外化身：活动槽位（一个槽位 = 一个分身）
#
# 覆盖：
#   · 阶梯定价 60/120/240/480、上限 4 尊、total_slots = 1 + 化身数
#   · user_cd 多槽位读写：按类型定位、只释放自己的槽、同类型不重复占槽
#   · 忙碌拦截：所有分身都出动了才拦，还有空闲分身就放行
#   · @player_required 真实接线（跑装饰器的 async generator）

import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()

import aiosqlite  # noqa: E402

PKG = ROOT.name


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


AdventureManager = None  # 延迟到 setUp 里取，避免顶层 import 顺序问题

USER_CD_DDL = """
CREATE TABLE IF NOT EXISTS user_cd (
    user_id TEXT NOT NULL,
    slot INTEGER NOT NULL DEFAULT 0,
    type INTEGER NOT NULL DEFAULT 0,
    create_time INTEGER NOT NULL DEFAULT 0,
    scheduled_time INTEGER NOT NULL DEFAULT 0,
    extra_data TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (user_id, slot)
)
"""


class _Player:
    """够用的 Player 替身：分身数量只读 permanent_bonus。"""

    def __init__(self, user_id="u1", avatars=0, state="空闲"):
        self.user_id = user_id
        self.user_name = "道友"
        self.state = state
        self.cultivation_type = "剑修"
        self.permanent_bonus = json.dumps({"avatar_slots": avatar(avatars)} if avatars else {})


def avatar(n: int) -> int:
    """账本写法：每尊 step=1，所以层数即尊数。"""
    return int(n)


async def _make_ext():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute(USER_CD_DDL)
    await conn.execute("CREATE TABLE IF NOT EXISTS players (user_id TEXT PRIMARY KEY, state TEXT)")
    await conn.execute("INSERT INTO players (user_id, state) VALUES ('u1', '空闲')")
    await conn.commit()
    ext = _imp("data.database_extended").DatabaseExtended(conn)
    return conn, ext


class TestAvatarCatalog(unittest.TestCase):
    """分身目录：价格曲线、上限、槽位命名。"""

    def setUp(self):
        self.avatar = _imp("core.avatar_rules")
        self.rules = _imp("core.reincarnation_rules")

    def test_price_curve(self):
        self.assertEqual(
            [self.avatar.avatar_price_at(i) for i in range(5)],
            [60, 120, 240, 480, None],
        )

    def test_catalog_matches_price_table(self):
        self.assertEqual(self.avatar.AVATAR_PRICES, (60, 120, 240, 480))
        self.assertEqual(self.avatar.MAX_AVATARS, 4)

    def test_total_price_900(self):
        self.assertEqual(sum(self.avatar.AVATAR_PRICES), 900)

    def test_total_slots(self):
        self.assertEqual(self.avatar.total_slots(_Player(avatars=0)), 1)
        self.assertEqual(self.avatar.total_slots(_Player(avatars=2)), 3)
        self.assertEqual(self.avatar.total_slots(_Player(avatars=9)), 5)  # 超买按上限收紧

    def test_next_price_and_maxed(self):
        self.assertEqual(self.avatar.next_avatar_price(_Player(avatars=0)), 60)
        self.assertEqual(self.avatar.next_avatar_price(_Player(avatars=2)), 240)
        self.assertIsNone(self.avatar.next_avatar_price(_Player(avatars=4)))
        self.assertTrue(self.avatar.is_avatar_maxed(_Player(avatars=4)))
        self.assertFalse(self.avatar.is_avatar_maxed(_Player(avatars=3)))

    def test_slot_labels(self):
        self.assertEqual(self.avatar.slot_label(0), "本体")
        self.assertEqual(self.avatar.slot_label(1), "化身·一")
        self.assertEqual(self.avatar.slot_label(7), "化身·7")

    def test_shop_price_escalates(self):
        self.assertEqual(self.rules.shop_price(52, 0), 60)
        self.assertEqual(self.rules.shop_price(52, 1), 120)
        self.assertEqual(self.rules.shop_price(52, 3), 480)
        self.assertEqual(self.rules.shop_price(52, 4), 0)  # 已满
        self.assertEqual(self.rules.shop_price(35, 3), 40)  # 其它商品固定价

    def test_item_52_in_shoppanel(self):
        item = self.rules.SHOP_ITEMS[52]
        self.assertEqual(item["name"], "身外化身")
        self.assertEqual(item["cap"], 4)
        self.assertEqual(item["group"], "便利")


class TestBusyRules(unittest.TestCase):
    """忙碌判定：只看还有没有空闲分身。"""

    def setUp(self):
        self.parallel = _imp("core.parallel_rules")

    def test_free_slots_math(self):
        self.assertEqual(self.parallel.free_slots(3, 1), 2)
        self.assertEqual(self.parallel.free_slots(1, 1), 0)
        self.assertEqual(self.parallel.free_slots(1, 5), 0)

    def test_free_slot_allows_anything(self):
        ok, hint = self.parallel.can_act_while_busy(None, "炼丹", 2, 1)
        self.assertTrue(ok)
        self.assertEqual(hint, "")

    def test_no_free_slot_blocks(self):
        ok, hint = self.parallel.can_act_while_busy(None, "炼丹", 1, 1)
        self.assertFalse(ok)
        self.assertEqual(hint, "")  # 空提示由调用方拼「分身都出去了」

    def test_reset_command_always_blocked(self):
        for cmd in ("飞升", "弃道重修", "我要修仙"):
            ok, hint = self.parallel.can_act_while_busy(None, cmd, 5, 0)
            self.assertFalse(ok, cmd)
            self.assertIn("重开人生", hint)

    def test_reset_command_prefix_not_confused(self):
        self.assertFalse(self.parallel.is_reset_command("飞升图鉴"))
        self.assertTrue(self.parallel.is_reset_command("飞升"))

    def test_hint_text_mentions_avatar(self):
        text = self.parallel.no_slot_hint(2, 2, ["历练中", "押镖中"])
        self.assertIn("2/2", text)
        self.assertIn("身外化身", text)
        self.assertIn("历练中", text)

    def test_hint_when_avatar_maxed_stops_advertising_shop(self):
        # 已请满 4 尊还给「去仙缘商店」的提示是空话，改劝腾槽位
        text = self.parallel.no_slot_hint(5, 5, ["历练中"], maxed=True)
        self.assertNotIn("去 /仙缘商店", text)
        self.assertIn("请满了", text)
        self.assertIn("腾个槽位", text)


class TestSlotStorage(unittest.IsolatedAsyncioTestCase):
    """user_cd 多槽位：分配、定位、按类型释放。"""

    async def asyncSetUp(self):
        self.US = _imp("models_extended").UserStatus
        self.conn, self.ext = await _make_ext()

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_first_activity_uses_slot_zero(self):
        ok, slot, why = await self.ext.start_activity("u1", self.US.ADVENTURING, 111, {"route_key": "a"}, max_slots=1)
        self.assertTrue(ok)
        self.assertEqual(slot, 0)
        self.assertEqual(await self.ext.busy_slot_count("u1"), 1)

    async def test_no_free_slot_when_max_is_one(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=1)
        ok, slot, why = await self.ext.start_activity("u1", self.US.DELIVERING, 222, max_slots=1)
        self.assertFalse(ok)
        self.assertEqual(slot, -1)
        self.assertEqual(why, "no_free_slot")

    async def test_second_avatar_takes_slot_one(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=2)
        ok, slot, _ = await self.ext.start_activity("u1", self.US.DELIVERING, 222, max_slots=2)
        self.assertTrue(ok)
        self.assertEqual(slot, 1)
        self.assertEqual(await self.ext.busy_slot_count("u1"), 2)

    async def test_same_type_is_not_double_started(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=2)
        ok, slot, why = await self.ext.start_activity("u1", self.US.ADVENTURING, 999, max_slots=2)
        self.assertFalse(ok)
        self.assertEqual(why, "already_running")
        self.assertEqual(slot, 0)
        # 原槽位时间戳没被覆盖
        cd = await self.ext.get_active_cd("u1", self.US.ADVENTURING)
        self.assertEqual(int(cd.scheduled_time), 111)

    async def test_type_lookup_finds_the_right_row(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=2)
        await self.ext.start_activity("u1", self.US.DELIVERING, 222, {"escort_gold": 7}, max_slots=2)
        adv = await self.ext.get_active_cd("u1", self.US.ADVENTURING)
        dlv = await self.ext.get_active_cd("u1", self.US.DELIVERING)
        self.assertEqual(int(adv.scheduled_time), 111)
        self.assertEqual(int(dlv.scheduled_time), 222)
        self.assertIsNone(await self.ext.get_active_cd("u1", self.US.EXPLORING))

    async def test_release_only_frees_its_own_slot(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=2)
        await self.ext.start_activity("u1", self.US.DELIVERING, 222, max_slots=2)
        await self.ext.set_user_free("u1", cd_type=self.US.DELIVERING)
        self.assertIsNone(await self.ext.get_active_cd("u1", self.US.DELIVERING))
        still = await self.ext.get_active_cd("u1", self.US.ADVENTURING)
        self.assertIsNotNone(still, "押镖结算不该顺手把历练也放掉")

    async def test_release_all_clears_every_slot(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=2)
        await self.ext.start_activity("u1", self.US.DELIVERING, 222, max_slots=2)
        await self.ext.release_activity("u1", None)
        self.assertEqual(await self.ext.busy_slot_count("u1"), 0)

    async def test_busy_activity_names(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=2)
        await self.ext.start_activity("u1", self.US.DELIVERING, 222, max_slots=2)
        names = await self.ext.busy_activity_names("u1")
        self.assertEqual(names, ["历练中", "押镖中"])

    async def test_player_state_follows_slots(self):
        def state_of():
            return None

        async with self.conn.execute("SELECT state FROM players WHERE user_id='u1'") as cur:
            row = await cur.fetchone()
        self.assertEqual(row["state"], "空闲")

        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=2)
        async with self.conn.execute("SELECT state FROM players WHERE user_id='u1'") as cur:
            row = await cur.fetchone()
        self.assertEqual(row["state"], "历练中")

        await self.ext.start_activity("u1", self.US.DELIVERING, 222, max_slots=2)
        async with self.conn.execute("SELECT state FROM players WHERE user_id='u1'") as cur:
            row = await cur.fetchone()
        self.assertEqual(row["state"], "历练中", "取 0 号槽（本体）优先展示")

        await self.ext.set_user_free("u1", cd_type=self.US.ADVENTURING)
        async with self.conn.execute("SELECT state FROM players WHERE user_id='u1'") as cur:
            row = await cur.fetchone()
        self.assertEqual(row["state"], "押镖中", "本体空出来后显示剩下的分身状态")

        await self.ext.set_user_free("u1", cd_type=self.US.DELIVERING)
        async with self.conn.execute("SELECT state FROM players WHERE user_id='u1'") as cur:
            row = await cur.fetchone()
        self.assertEqual(row["state"], "空闲")

    async def test_free_slot_reused_after_release(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=2)
        await self.ext.start_activity("u1", self.US.DELIVERING, 222, max_slots=2)
        await self.ext.set_user_free("u1", cd_type=self.US.ADVENTURING)
        ok, slot, _ = await self.ext.start_activity("u1", self.US.EXPLORING, 333, max_slots=2)
        self.assertTrue(ok)
        self.assertEqual(slot, 0, "空出来的 0 号槽要能重新分配")


class TestEnsureFreeSlot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.US = _imp("models_extended").UserStatus
        self.avatar = _imp("core.avatar_rules")
        self.conn, self.ext = await _make_ext()

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_hint_when_all_busy(self):
        player = _Player(avatars=0)
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=1)
        ok, hint = await self.avatar.ensure_free_slot(self.ext_proxy(), player, "u1")
        self.assertFalse(ok)
        self.assertIn("身外化身", hint)

    async def test_pass_when_one_free(self):
        player = _Player(avatars=1)
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=2)
        ok, hint = await self.avatar.ensure_free_slot(self.ext_proxy(), player, "u1")
        self.assertTrue(ok)
        self.assertEqual(hint, "")

    def ext_proxy(self):
        ext = self.ext

        class _Proxy:
            def __getattr__(self, name):
                return getattr(ext, name)

        return type("DB", (), {"ext": _Proxy()})()


# ---------------------------------------------------------------------------
# 装饰器接线：确认 @player_required 真的按分身槽位放行 / 拦截
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


class _Handler:
    def __init__(self, db):
        self.db = db
        self.hit = False


class TestPlayerRequiredWiring(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.utils = _imp("handlers.utils")
        self.US = _imp("models_extended").UserStatus
        self.Player = _imp("models").Player
        self.conn, self.ext = await _make_ext()

    async def asyncTearDown(self):
        await self.conn.close()

    def _db(self, player):
        ext = self.ext

        class _Ext:
            def __getattr__(self, name):
                return getattr(ext, name)

            async def get_active_loan(self, user_id):
                return None

            async def settle_sect_debt(self, user_id):
                return 0

        class _DB:
            def __init__(self, p):
                self.player = p
                self.ext = _Ext()

            async def get_player_by_id(self, user_id):
                return self.player

        return _DB(player)

    def _handler(self, avatars):
        player = self.Player(
            user_id="u1",
            user_name="道友",
            permanent_bonus=json.dumps({"avatar_slots": avatars} if avatars else {}),
        )
        handler = _Handler(self._db(player))

        async def body(_self, p, event, *args, **kwargs):
            _self.hit = True
            yield event.plain_result("✅ 执行成功")

        handler.act = self.utils.player_required(body).__get__(handler, _Handler)
        return handler

    async def _run(self, avatars, command):
        handler = self._handler(avatars)
        out = [r async for r in handler.act(_Event(command))]
        return handler.hit, out

    async def test_idle_player_passes(self):
        hit, out = await self._run(0, "炼丹")
        self.assertTrue(hit)
        self.assertEqual(out[0], "✅ 执行成功")

    async def test_busy_without_avatar_blocks(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=1)
        hit, out = await self._run(0, "炼丹")
        self.assertFalse(hit)
        self.assertIn("身外化身", out[0])

    async def test_busy_with_free_avatar_allows_chores(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=2)
        hit, out = await self._run(1, "炼丹")
        self.assertTrue(hit)
        self.assertEqual(out[0], "✅ 执行成功")

    async def test_all_slots_busy_blocks_even_with_avatars(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=2)
        await self.ext.start_activity("u1", self.US.DELIVERING, 222, max_slots=2)
        hit, out = await self._run(1, "炼丹")
        self.assertFalse(hit)
        self.assertIn("2/2", out[0])

    async def test_reset_command_blocked_while_busy(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=3)
        hit, out = await self._run(2, "弃道重修")
        self.assertFalse(hit)
        self.assertIn("重开人生", out[0])

    async def test_info_command_always_allowed(self):
        await self.ext.start_activity("u1", self.US.ADVENTURING, 111, max_slots=1)
        hit, out = await self._run(0, "我的信息")
        self.assertTrue(hit)


class TestAvatarPurchaseWiring(unittest.IsolatedAsyncioTestCase):
    """真实走一遍仙缘兑换：买一尊 → 账本 → 活动槽位真的变多。

    这条链路一旦断了（比如商品 key 写错、价格没走 shop_price、账本没回写），
    玩家会花了仙缘点却多不出槽位，所以单独钉住。
    """

    async def test_buy_avatar_widens_slots(self):
        avatar_rules = _imp("core.avatar_rules")
        Player = _imp("models").Player
        ConfigManager = _imp("config_manager").ConfigManager
        ReincarnationManager = _imp("managers.reincarnation_manager").ReincarnationManager

        class _DB:
            async def update_player(self, player, commit=True):
                pass

        mgr = ReincarnationManager(_DB(), ConfigManager(ROOT), None)
        player = Player(user_id="u1")
        player.fate_points = 1000

        self.assertEqual(avatar_rules.total_slots(player), 1)
        self.assertEqual(avatar_rules.avatar_count(player), 0)

        paid = []
        for expected in range(1, 5):
            price = avatar_rules.next_avatar_price(player)
            ok, msg = await mgr.buy_fate_shop_item(player, 52)
            self.assertTrue(ok, msg)
            paid.append(price)
            # 账本 key 必须是 avatar_slots，且槽位立刻跟着涨
            self.assertEqual(avatar_rules.avatar_count(player), expected)
            self.assertEqual(avatar_rules.total_slots(player), 1 + expected)

        self.assertEqual(paid, [60, 120, 240, 480])
        self.assertEqual(player.fate_points, 1000 - 900)
        self.assertIn("avatar_slots", json.loads(player.permanent_bonus))

        # 买满后第 5 次被拒，且不倒扣点数
        ok, msg = await mgr.buy_fate_shop_item(player, 52)
        self.assertFalse(ok)
        self.assertIn("已满 4 层", msg)
        self.assertEqual(player.fate_points, 100)
        self.assertTrue(avatar_rules.is_avatar_maxed(player))


class TestSectChoresNeedOneFreeAvatar(unittest.IsolatedAsyncioTestCase):
    """宗门事务是杂务：有分身闲着就能办，全员出动了才拦。

    这条钉住 `SectManager._idle_guard` —— 它以前是「任一分身在忙就拒绝」，
    会让「本体在历练、化身在家」的玩家连捐献都做不了。
    """

    def setUp(self):
        self.SectManager = _imp("managers.sect_manager").SectManager
        self.US = _imp("models_extended").UserStatus

    async def _guard(self, player, busy_types):
        conn, ext = await _make_ext()
        try:
            for idx, cd_type in enumerate(busy_types):
                ok, _slot, why = await ext.start_activity(
                    player.user_id, cd_type, 999, max_slots=1 + idx
                )
                self.assertTrue(ok, why)

            class _DB:
                def __init__(self):
                    self.ext = ext

                async def get_player_by_id(self, uid):
                    return player

            mgr = self.SectManager(_DB(), None)
            return await mgr._idle_guard(player.user_id, "捐献宗门")
        finally:
            await conn.close()

    async def test_one_busy_with_free_avatar_is_allowed(self):
        # 本体历练中，但有 1 尊化身在家 → 宗门事务照做
        msg = await self._guard(_Player(avatars=1), [self.US.ADVENTURING])
        self.assertEqual(msg, "")

    async def test_all_slots_busy_is_blocked(self):
        # 本体 + 化身全出动 → 拦下，并提示去请化身
        msg = await self._guard(
            _Player(avatars=1), [self.US.ADVENTURING, self.US.DELIVERING]
        )
        self.assertIn("分身都出去了", msg)
        self.assertIn("身外化身", msg)
        self.assertIn("捐献宗门", msg)


if __name__ == "__main__":
    unittest.main()
