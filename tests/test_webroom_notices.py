# -*- coding: utf-8 -*-
"""webroom 消息受众、隐私设置与个人消息历史回归测试。"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

_TMP = tempfile.mkdtemp(prefix="xiuxian_notices_")

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
core_mod = types.ModuleType("astrbot.core")
utils_mod = types.ModuleType("astrbot.core.utils")
path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")


class _L:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


api_mod.logger = _L()
path_mod.get_astrbot_data_path = lambda: _TMP
utils_mod.astrbot_path = path_mod
core_mod.utils = utils_mod
astrbot.api = api_mod
astrbot.core = core_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.core", core_mod)
sys.modules.setdefault("astrbot.core.utils", utils_mod)
sys.modules.setdefault("astrbot.core.utils.astrbot_path", path_mod)

try:
    accounts = __import__(f"{PKG}.webchat_room.accounts", fromlist=["x"])
    rs = __import__(f"{PKG}.webchat_room.room_server", fromlist=["x"])
    text_struct = __import__(f"{PKG}.webchat_room.text_struct", fromlist=["x"])
except Exception as exc:  # pragma: no cover - 依赖缺失时跳过
    raise unittest.SkipTest(f"webroom 依赖不可用: {exc}")


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class FakeAdapter:
    def __init__(self):
        self.messages = []

    async def on_player_message(self, room_id, player_id, player_name, text, **kwargs):
        self.messages.append((room_id, player_id, player_name, text, kwargs))


def _info(username, nick):
    return {
        "player_id": f"web_{username}",
        "player_name": nick,
        "username": username,
        "avatar": "",
    }


class SettingsTest(unittest.TestCase):
    def setUp(self):
        accounts._conn = None
        db_path = accounts.STORE_DIR / "accounts.db"
        if db_path.exists():
            db_path.unlink()
        accounts.init()
        accounts.register("alice", "secret1", "阿狸")

    def test_defaults(self):
        conf = accounts.get_settings("alice")
        self.assertEqual(conf["manual_visibility"], "public")
        self.assertEqual(conf["auto_visibility"], "self")
        self.assertEqual(conf["battle_detail_default"], "collapsed")
        self.assertEqual(conf["view_filter"], "all")

    def test_set_and_validate(self):
        ok, _, conf = accounts.set_settings("alice", {"view_filter": "self"})
        self.assertTrue(ok)
        self.assertEqual(conf["view_filter"], "self")
        ok, _, _ = accounts.set_settings("alice", {"view_filter": "nobody"})
        self.assertFalse(ok)
        self.assertEqual(accounts.get_settings("alice")["view_filter"], "self")

    def test_player_key_resolution(self):
        self.assertEqual(accounts.username_of_player_key("web_alice"), "alice")
        ok, _ = accounts.bind_qq("alice", "1234567")
        self.assertTrue(ok)
        self.assertEqual(accounts.username_of_player_key("1234567"), "alice")


class NoticeStoreTest(unittest.TestCase):
    def setUp(self):
        accounts._conn = None
        db_path = accounts.STORE_DIR / "accounts.db"
        if db_path.exists():
            db_path.unlink()
        accounts.init()
        accounts.register("bob", "secret1", "小博")

    def test_notice_lifecycle(self):
        self.assertEqual(accounts.count_unread("bob"), 0)
        first = accounts.add_notice("bob", title="自动代理", summary="押镖完成", detail="详情")
        second = accounts.add_notice("bob", title="战报", summary="胜利")
        self.assertEqual(accounts.count_unread("bob"), 2)

        items, unread = accounts.list_notices("bob", limit=10)
        self.assertEqual(unread, 2)
        self.assertEqual([i["id"] for i in items], [second, first])
        self.assertEqual(items[0]["title"], "战报")

        accounts.mark_notices_read("bob", ids=[first])
        self.assertEqual(accounts.count_unread("bob"), 1)
        accounts.mark_notices_read("bob", all_read=True)
        self.assertEqual(accounts.count_unread("bob"), 0)

        removed = accounts.clear_read_notices("bob")
        self.assertEqual(removed, 2)
        self.assertEqual(accounts.list_notices("bob")[0], [])


class AudienceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        accounts._conn = None
        db_path = accounts.STORE_DIR / "accounts.db"
        if db_path.exists():
            db_path.unlink()
        accounts.init()
        accounts.register("alice", "secret1", "阿狸")
        accounts.register("bob", "secret1", "小博")

        self.sender = FakeWS()
        self.other = FakeWS()
        self.adapter = FakeAdapter()
        self._orig_adapter = rs._adapter
        rs._adapter = self.adapter
        rs._rooms.clear()
        rs._recent_msgs.clear()
        rs._rooms[rs.ROOM_ID] = {
            self.sender: _info("alice", "阿狸"),
            self.other: _info("bob", "小博"),
        }

    async def asyncTearDown(self):
        rs._adapter = self._orig_adapter
        rs._rooms.clear()
        rs._recent_msgs.clear()

    async def _send(self, payload):
        await rs._handle_client_message(
            rs.ROOM_ID, self.sender, json.dumps(payload, ensure_ascii=False)
        )

    def _last(self, ws, typ):
        for item in reversed(ws.sent):
            if item.get("type") == typ:
                return item
        return None

    def test_resolve_audience(self):
        self.assertEqual(rs.resolve_audience("/签到", {}), "public")
        self.assertEqual(rs.resolve_audience("/我的信息", {}, panel=True), "self")
        # 旧版前端/手动输入不带 panel 标记时，仍按指令名强制私密
        self.assertEqual(rs.resolve_audience("/我的信息", {}), "self")
        self.assertEqual(rs.resolve_audience("/储物戒", {}), "self")
        self.assertEqual(
            rs.resolve_audience("/签到", {"manual_visibility": "self"}), "self"
        )

    async def test_manual_command_is_public(self):
        await self._send({"action": "cmd", "text": "/签到", "req_id": "r1"})
        for ws in (self.sender, self.other):
            chat = self._last(ws, "chat")
            self.assertIsNotNone(chat)
            self.assertTrue(chat["is_cmd"])
            self.assertEqual(chat["audience"], "public")
        self.assertEqual(self.adapter.messages[-1][4]["audience"], "public")

    async def test_panel_command_has_no_public_echo(self):
        await self._send(
            {"action": "cmd", "text": "/我的信息", "req_id": "r2", "panel": True}
        )
        self.assertIsNone(self._last(self.other, "chat"))
        self.assertIsNone(self._last(self.sender, "chat"))
        self.assertEqual(self.adapter.messages[-1][4]["audience"], "self")
        self.assertTrue(self.adapter.messages[-1][4]["panel"])

    async def test_panel_command_without_flag_stays_private(self):
        # 兼容旧版前端：只带 req_id、不带 panel 的看板请求也不能上公屏
        await self._send(
            {"action": "cmd", "text": "/我的信息", "req_id": "r2b", "card": True}
        )
        self.assertIsNone(self._last(self.other, "chat"))
        self.assertIsNone(self._last(self.sender, "chat"))
        self.assertEqual(self.adapter.messages[-1][4]["audience"], "self")
        self.assertTrue(self.adapter.messages[-1][4]["panel"])
        self.assertTrue(self.adapter.messages[-1][4]["want_card"])

    async def test_self_visibility_only_echoes_to_sender(self):
        accounts.set_settings("alice", {"manual_visibility": "self"})
        await self._send({"action": "cmd", "text": "/签到", "req_id": "r3"})
        self.assertIsNotNone(self._last(self.sender, "chat"))
        self.assertIsNone(self._last(self.other, "chat"))
        self.assertEqual(self.adapter.messages[-1][4]["audience"], "self")

    async def test_legacy_private_flag_keeps_command_private(self):
        # 旧版前端会对所有 cmd 发 private:true，升级期间不回退成公屏
        await self._send(
            {"action": "cmd", "text": "/签到", "req_id": "r4", "private": True}
        )
        self.assertIsNone(self._last(self.other, "chat"))
        self.assertIsNotNone(self._last(self.sender, "chat"))
        self.assertEqual(self.adapter.messages[-1][4]["audience"], "self")


class NotifyPlayerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        accounts._conn = None
        db_path = accounts.STORE_DIR / "accounts.db"
        if db_path.exists():
            db_path.unlink()
        accounts.init()
        accounts.register("alice", "secret1", "阿狸")
        accounts.register("bob", "secret1", "小博")
        self.alice = FakeWS()
        self.bob = FakeWS()
        rs._rooms.clear()
        rs._recent_msgs.clear()
        rs._rooms[rs.ROOM_ID] = {
            self.alice: _info("alice", "阿狸"),
            self.bob: _info("bob", "小博"),
        }

    async def asyncTearDown(self):
        rs._rooms.clear()
        rs._recent_msgs.clear()

    def _msgs(self, ws, typ):
        return [m for m in ws.sent if m.get("type") == typ]

    async def test_default_only_personal_notice(self):
        ok = await rs.notify_player(
            rs.ROOM_ID, "web_alice", "✅ 押镖完成\n获得 100 灵石", title="自动代理"
        )
        self.assertTrue(ok)
        notices = self._msgs(self.alice, "notice")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["notice"]["title"], "自动代理")
        self.assertEqual(notices[0]["unread"], 1)
        # 默认不广播
        self.assertEqual(self._msgs(self.bob, "bot"), [])

    async def test_public_summary_broadcasts(self):
        accounts.set_settings("alice", {"auto_visibility": "public_summary"})
        await rs.notify_player(
            rs.ROOM_ID, "web_alice", "✅ 押镖完成\n获得 100 灵石", title="自动代理"
        )
        bots = self._msgs(self.bob, "bot")
        self.assertEqual(len(bots), 1)
        self.assertEqual(bots[0]["channel"], "auto")
        self.assertEqual(bots[0]["audience"], "public")
        self.assertEqual(bots[0]["detail"], "")

    async def test_public_full_broadcasts_detail(self):
        accounts.set_settings("alice", {"auto_visibility": "public_full"})
        await rs.notify_player(
            rs.ROOM_ID, "web_alice", "✅ 押镖完成\n获得 100 灵石", title="自动代理"
        )
        bots = self._msgs(self.bob, "bot")
        self.assertEqual(len(bots), 1)
        self.assertIn("获得 100 灵石", bots[0]["detail"])
        self.assertFalse(bots[0]["detail_collapsed"])

    async def test_unknown_player_falls_back(self):
        ok = await rs.notify_player(rs.ROOM_ID, "999999999", "押镖完成")
        self.assertFalse(ok)


class BattleParseTest(unittest.TestCase):
    BATTLE = "\n".join(
        [
            "☆━━━━ 战斗开始 ━━━━☆",
            "阿狸 VS 小博",
            "-- 第 1 回合 --",
            "阿狸 挥剑斩出 120 点伤害",
            "-- 第 2 回合 --",
            "小博 气血耗尽",
            "",
            "☆━━━━ 战斗结束 ━━━━☆",
            "胜者：阿狸",
            "回合数：2",
            "阿狸 剩余 HP：300 | 小博 剩余 HP：0",
        ]
    )

    def test_parse_battle(self):
        card = text_struct.parse_battle(self.BATTLE)
        self.assertIsNotNone(card)
        self.assertEqual(card["kind"], "battle")
        self.assertIn("阿狸", card["summary"])
        self.assertIn("2 回合", card["summary"])
        self.assertIn("第 1 回合", card["detail"])
        self.assertTrue(card["folded"])

    def test_world_boss_result(self):
        raw = "\n".join(
            [
                "⚔️ 世界Boss战斗结果",
                "━━━━━━━━━━━━━━━",
                "Boss：九幽魔尊",
                "结果：击杀成功",
                "战斗回合：5",
                "造成伤害：12,345（3.20%）",
                "Boss剩余：0 / 400,000",
                "你的剩余气血：820",
                "",
                "📜 战斗详情：",
                "☆━━━━ Boss战开始 ━━━━☆",
                "阿狸 挑战 九幽魔尊",
                "-- 第 1 回合 --",
                "阿狸 挥剑造成 3000 点伤害",
                "九幽魔尊 剩余 HP: 397000",
                "🌀 战斗结束：阿狸 灵气 40/100",
            ]
        )
        card = text_struct.parse_battle(raw)
        self.assertIsNotNone(card)
        self.assertIn("结果：击杀成功", card["summary"])
        self.assertIn("5 回合", card["summary"])
        self.assertIn("第 1 回合", card["detail"])

    def test_not_battle(self):
        self.assertIsNone(text_struct.parse_battle("✅ 签到成功，获得 100 灵石"))

    def test_trophy_winner_and_draw(self):
        win = "\n".join(
            [
                "☆━━━━ 战斗开始 ━━━━☆",
                "阿狸 VS 小博",
                "-- 第 1 回合 --",
                "阿狸 挥剑",
                "🏆 决斗结束，阿狸 获胜！",
                "(剩余血量: 80% vs 0%)",
            ]
        )
        card = text_struct.parse_battle(win)
        self.assertIsNotNone(card)
        self.assertIn("胜者：阿狸", card["summary"])

        draw = "\n".join(
            [
                "☆━━━━ 战斗开始 ━━━━☆",
                "阿狸 VS 小博",
                "-- 第 1 回合 --",
                "🏆 切磋结束，平局！",
            ]
        )
        card = text_struct.parse_battle(draw)
        self.assertIsNotNone(card)
        self.assertIn("平局", card["summary"])


if __name__ == "__main__":
    unittest.main()
