# tests/test_full_avatar_slots.py
# 满分身（本体 + 4 尊化身 = 5 槽）的守护用例。
#
# 现有 test_avatar_slots.py 主要覆盖 1~2 个槽位（有/没有化身）。5 槽是真会出事的规模：
#   · 第 6 项活动该被拦，且提示不能再劝人去仙缘商店（已经买满，劝了也没用）
#   · 同类型互斥在 5 槽下不能"顺手占掉一个空槽"
#   · 退出/结算只能放自己那一槽，不能把别的分身一起收掉
#   · 所有 start_activity 调用点必须传 total_slots(player) —— 写死数字会让
#     买满化身的玩家永远只用得上 1 号槽（find_free_slot 只按传入的上限找）

import ast
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

MAX_SLOTS = 5


async def _make_ext():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute(USER_CD_DDL)
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS players (user_id TEXT PRIMARY KEY, state TEXT)"
    )
    await conn.execute("INSERT INTO players (user_id, state) VALUES ('u1', '空闲')")
    await conn.commit()
    return conn, _imp("data.database_extended").DatabaseExtended(conn)


def _player(avatars=4, user_id="u1"):
    return _imp("models").Player(
        user_id=user_id,
        user_name="满分身道友",
        permanent_bonus=json.dumps({"avatar_slots": avatars}, ensure_ascii=False),
    )


async def _fill_five(ext, US):
    """按真实业务顺序把 5 个槽位占满，返回 {槽位: 类型}。"""
    for status in (
        US.CULTIVATING,
        US.RESTING,
        US.DELIVERING,
        US.SECT_TASK,
        US.EXPLORING,
    ):
        await ext.start_activity("u1", int(status), 1_000, {}, max_slots=MAX_SLOTS)
    rows = await ext.list_user_cds("u1")
    return {int(r.slot): int(r.type) for r in rows if int(r.type or 0) != int(US.IDLE)}


class TestSlotsFull(unittest.IsolatedAsyncioTestCase):
    """5 个槽位全满时的分配、互斥与释放。"""

    async def asyncSetUp(self):
        self.US = _imp("models_extended").UserStatus
        self.conn, self.ext = await _make_ext()

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_five_types_take_five_distinct_slots(self):
        busy = await _fill_five(self.ext, self.US)
        self.assertEqual(sorted(busy), [0, 1, 2, 3, 4])
        self.assertEqual(busy[0], int(self.US.CULTIVATING), "0 号槽是本体，先到先得")
        self.assertEqual(await self.ext.busy_slot_count("u1"), MAX_SLOTS)

    async def test_sixth_activity_has_no_slot(self):
        await _fill_five(self.ext, self.US)
        ok, slot, why = await self.ext.start_activity(
            "u1", int(self.US.ADVENTURING), 1_000, {}, max_slots=MAX_SLOTS
        )
        self.assertFalse(ok)
        self.assertEqual(slot, -1)
        self.assertEqual(why, "no_free_slot")
        self.assertEqual(await self.ext.busy_slot_count("u1"), MAX_SLOTS)

    async def test_same_type_does_not_steal_a_free_slot(self):
        await _fill_five(self.ext, self.US)
        await self.ext.set_user_free("u1", cd_type=self.US.DELIVERING)
        ok, slot, _ = await self.ext.start_activity(
            "u1", int(self.US.ADVENTURING), 2_000, {}, max_slots=MAX_SLOTS
        )
        self.assertTrue(ok)
        self.assertEqual(slot, 2, "空出来的最小槽位要被复用")

        before = await self.ext.get_active_cd("u1", self.US.ADVENTURING)
        ok2, slot2, why2 = await self.ext.start_activity(
            "u1", int(self.US.ADVENTURING), 9_999, {}, max_slots=MAX_SLOTS
        )
        after = await self.ext.get_active_cd("u1", self.US.ADVENTURING)
        self.assertFalse(ok2)
        self.assertEqual(why2, "already_running")
        self.assertEqual(slot2, 2, "返回的是原有那一槽")
        self.assertEqual(int(after.scheduled_time), int(before.scheduled_time),
                         "互斥挡住时不能覆盖原有时间戳")
        self.assertEqual(await self.ext.busy_slot_count("u1"), MAX_SLOTS,
                         "互斥时也不能顺手多占一个空槽")

    async def test_release_one_of_five_keeps_the_other_four(self):
        busy = await _fill_five(self.ext, self.US)
        target = int(self.US.SECT_TASK)
        await self.ext.set_user_free("u1", cd_type=target)
        after = await self.ext.list_user_cds("u1")
        left = {int(r.slot): int(r.type) for r in after if int(r.type or 0) != int(self.US.IDLE)}
        expected = {s: t for s, t in busy.items() if t != target}
        self.assertEqual(left, expected)

    async def test_release_all_clears_five(self):
        await _fill_five(self.ext, self.US)
        await self.ext.release_activity("u1", None)
        self.assertEqual(await self.ext.busy_slot_count("u1"), 0)

    async def test_busy_names_lists_all_five_in_slot_order(self):
        await _fill_five(self.ext, self.US)
        names = await self.ext.busy_activity_names("u1")
        self.assertEqual(len(names), MAX_SLOTS)
        self.assertEqual(
            names,
            [self.US.get_name(s) for s in (
                self.US.CULTIVATING,
                self.US.RESTING,
                self.US.DELIVERING,
                self.US.SECT_TASK,
                self.US.EXPLORING,
            )],
        )

    async def test_player_state_prefers_slot_zero(self):
        await _fill_five(self.ext, self.US)
        async with self.conn.execute("SELECT state FROM players WHERE user_id='u1'") as cur:
            row = await cur.fetchone()
        # _STATE_TEXT 里闭关是「修炼中」（与 get_name 的「闭关中」并存，S1b 待定）
        self.assertIn(row["state"], ("修炼中", "闭关中"))


class TestFullAvatarBusyGate(unittest.IsolatedAsyncioTestCase):
    """5/5 满员时 @player_required 的提示口径。"""

    class _Event:
        def __init__(self, text):
            self._text = text

        def get_sender_id(self):
            return "u1"

        def get_message_str(self):
            return self._text

        def plain_result(self, text):
            return text

    async def asyncSetUp(self):
        self.utils = _imp("handlers.utils")
        self.US = _imp("models_extended").UserStatus
        self.conn, self.ext = await _make_ext()
        await _fill_five(self.ext, self.US)

    async def asyncTearDown(self):
        await self.conn.close()

    async def _run(self, command, avatars=4):
        ext = self.ext
        player = _player(avatars)

        class _Ext:
            def __getattr__(self, name):
                return getattr(ext, name)

            async def get_active_loan(self, user_id):
                return None

            async def settle_sect_debt(self, user_id):
                return 0

        class _DB:
            def __init__(self):
                self.ext = _Ext()

            async def get_player_by_id(self, user_id):
                return player

        class _H:
            def __init__(self):
                self.db = _DB()
                self.hit = False

        handler = _H()

        async def body(_self, p, event, *args, **kwargs):
            _self.hit = True
            yield event.plain_result("✅ 执行成功")

        handler.act = self.utils.player_required(body).__get__(handler, _H)
        out = [r async for r in handler.act(self._Event(command))]
        return handler, out

    async def test_chore_blocked_with_5_of_5(self):
        handler, out = await self._run("炼丹")
        self.assertFalse(handler.hit)
        self.assertIn("5/5 在忙", out[0])

    async def test_maxed_hint_stops_advertising_shop(self):
        _, out = await self._run("炼丹")
        self.assertIn("化身已经请满了", out[0])
        self.assertNotIn("去 /仙缘商店", out[0])

    async def test_readonly_command_still_allowed(self):
        handler, out = await self._run("我的信息")
        self.assertTrue(handler.hit)
        self.assertEqual(out[0], "✅ 执行成功")

    async def test_exit_command_still_allowed(self):
        # 退出指令漏登记 = 玩家被自己拦在门外（S2 要结构性消掉的那个坑）
        handler, _ = await self._run("退出历练")
        self.assertTrue(handler.hit)


class TestMaxSlotsCallSites(unittest.TestCase):
    """所有 start_activity 调用点都必须用 total_slots(player)，不能写死数字。

    find_free_slot 只按传入的 max_slots 找槽，写死 1 的话买满 4 尊化身的玩家
    也只能用 0 号槽 —— 花了 900 仙缘点却一点不并行，而且不会报错。
    """

    def test_no_literal_max_slots_in_production_code(self):
        offenders = []
        for path in ROOT.rglob("*.py"):
            if "tests" in path.parts or ".git" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name != "start_activity":
                    continue
                for kw in node.keywords:
                    if kw.arg != "max_slots":
                        continue
                    value = kw.value
                    if isinstance(value, ast.Constant):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                        continue
                    text = ast.unparse(value)
                    if "total_slots" not in text:
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} → {text}"
                        )
        self.assertEqual(offenders, [], f"这些调用点的 max_slots 不是 total_slots：{offenders}")

    def test_total_slots_is_five_when_maxed(self):
        avatar = _imp("core.avatar_rules")
        self.assertEqual(avatar.total_slots(_player(4)), 5)
        self.assertTrue(avatar.is_avatar_maxed(_player(4)))
        self.assertIsNone(avatar.next_avatar_price(_player(4)))

    def test_book_over_cap_still_five(self):
        avatar = _imp("core.avatar_rules")
        self.assertEqual(avatar.total_slots(_player(99)), 5)


if __name__ == "__main__":
    unittest.main()
