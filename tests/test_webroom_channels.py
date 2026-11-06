# -*- coding: utf-8 -*-
"""webroom 仙界（频道）系统回归测试。"""
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="xiuxian_channels_boot_")

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
api_mod.AstrBotConfig = dict
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
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from webchat_room import accounts as acc
    from webchat_room import channels
    from webchat_room import room_server as rs
except Exception as exc:  # pragma: no cover
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


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xiuxian_channels_"))
        self._orig = (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn)
        acc.STORE_DIR = self.tmp
        acc.DB_PATH = self.tmp / "accounts.db"
        acc.SECRET_PATH = self.tmp / "secret.key"
        acc._conn = None
        acc.init()
        channels.init()
        acc.register("alice", "secret1", "阿狸")
        acc.register("bob", "secret1", "小博")

    def tearDown(self):
        (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn) = self._orig
        if acc._conn is not None:
            acc._conn.close()
            acc._conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_main_channel_always_joined(self):
        joined = channels.joined_channels("alice")
        self.assertEqual(joined[0]["id"], channels.MAIN_CHANNEL_ID)
        self.assertTrue(joined[0]["is_main"])
        self.assertTrue(channels.is_member(channels.MAIN_CHANNEL_ID, "alice"))
        self.assertTrue(channels.can_access(channels.MAIN_CHANNEL_ID, "alice"))

    def test_auto_name_and_owner_joined(self):
        ok, _msg, ch = channels.create("alice", name="", is_public=True)
        self.assertTrue(ok)
        self.assertEqual(ch["name"], "仙界1")
        self.assertTrue(ch["is_mine"])
        self.assertTrue(ch["joined"])
        self.assertTrue(ch["uuid"])
        ok2, msg2, ch2 = channels.create("alice", name="", is_public=True)
        self.assertTrue(ok2, msg2)
        self.assertEqual(ch2["name"], "仙界2")

    def test_name_unique(self):
        ok, _msg, _ch = channels.create("alice", name="青云界")
        self.assertTrue(ok)
        ok2, msg2, _ch2 = channels.create("bob", name="青云界")
        self.assertFalse(ok2)
        self.assertIn("已被占用", msg2)

    def test_public_password_join(self):
        ok, _msg, ch = channels.create("alice", name="青云界", password="1234")
        self.assertTrue(ok)
        self.assertTrue(ch["has_password"])
        self.assertTrue(ch["public"])

        hall = channels.public_channels("bob")
        self.assertIn(ch["id"], [item["id"] for item in hall])

        ok2, _msg2, _ = channels.join("bob", identifier="青云界", password="bad")
        self.assertFalse(ok2)
        ok3, msg3, joined = channels.join("bob", identifier="青云界", password="1234")
        self.assertTrue(ok3, msg3)
        self.assertTrue(joined["joined"])
        # 已加入后再次加入不再要求密码
        ok4, _msg4, _ = channels.join("bob", identifier="青云界", password="")
        self.assertTrue(ok4)

    def test_private_hidden_from_hall_and_needs_uuid(self):
        ok, _msg, ch = channels.create(
            "alice", name="幽冥界", password="1234", is_public=False
        )
        self.assertTrue(ok)
        self.assertFalse(ch["public"])
        self.assertNotIn(ch["id"], [item["id"] for item in channels.public_channels("bob")])

        # 私密仙界用名称加入应被拒绝
        ok2, msg2, _ = channels.join("bob", identifier="幽冥界", password="1234")
        self.assertFalse(ok2)
        self.assertIn("UUID", msg2)
        # 用 UUID 才可以
        ok3, msg3, joined = channels.join("bob", identifier=ch["uuid"], password="1234")
        self.assertTrue(ok3, msg3)
        self.assertTrue(joined["joined"])
        # 已加入后按名称回来也不受影响
        ok4, _msg4, _ = channels.join("bob", identifier="幽冥界", password="")
        self.assertTrue(ok4)

    def test_owner_can_toggle_visibility_and_password(self):
        _ok, _msg, ch = channels.create("alice", name="青云界", password="1234")
        ok, _msg, updated = channels.update(
            "alice", ch["id"], is_public=False, clear_password=True, name="青云秘界"
        )
        self.assertTrue(ok)
        self.assertFalse(updated["public"])
        self.assertFalse(updated["has_password"])
        self.assertEqual(updated["name"], "青云秘界")
        # 非创建者不能管理
        ok2, msg2, _ = channels.update("bob", ch["id"], name="抢过来")
        self.assertFalse(ok2)
        self.assertIn("创建者", msg2)

    def test_owned_limit(self):
        for i in range(channels.MAX_OWNED):
            ok, msg, _ = channels.create("alice", name=f"界{i + 1}")
            self.assertTrue(ok, msg)
        ok, msg, _ = channels.create("alice", name="界超出")
        self.assertFalse(ok)
        self.assertIn("最多", msg)


class ChannelSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xiuxian_channels_ws_"))
        self._orig = (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn, rs._adapter)
        acc.STORE_DIR = self.tmp
        acc.DB_PATH = self.tmp / "accounts.db"
        acc.SECRET_PATH = self.tmp / "secret.key"
        acc._conn = None
        acc.init()
        channels.init()
        acc.register("alice", "secret1", "阿狸")
        acc.register("bob", "secret1", "小博")
        self.alice = FakeWS()
        self.bob = FakeWS()
        self.adapter = FakeAdapter()
        rs._adapter = self.adapter
        rs._rooms.clear()
        rs._recent_msgs.clear()
        rs._channel_history.clear()
        rs._rooms[rs.ROOM_ID] = {
            self.alice: {
                "player_id": "web_alice",
                "player_name": "阿狸",
                "username": "alice",
                "avatar": "",
                "room": rs.ROOM_ID,
            },
            self.bob: {
                "player_id": "web_bob",
                "player_name": "小博",
                "username": "bob",
                "avatar": "",
                "room": rs.ROOM_ID,
            },
        }

    async def asyncTearDown(self):
        await super().asyncTearDown()
        rs._adapter = self._orig[4]
        rs._rooms.clear()
        rs._recent_msgs.clear()
        rs._channel_history.clear()
        (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn, _adapter) = self._orig
        if acc._conn is not None:
            acc._conn.close()
            acc._conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _send(self, ws, payload):
        await rs._handle_client_message(rs.ROOM_ID, ws, json.dumps(payload, ensure_ascii=False))

    def _last(self, ws, typ):
        for item in reversed(ws.sent):
            if item.get("type") == typ:
                return item
        return None

    async def test_switch_isolates_chat_and_routes_replies(self):
        ok, msg, ch = channels.create("alice", name="青云界", password="1234")
        self.assertTrue(ok, msg)
        ok, msg, _ = channels.join("bob", identifier=ch["id"], password="1234")
        self.assertTrue(ok, msg)

        await self._send(self.alice, {"action": "switch", "channel_id": ch["id"]})
        ack = self._last(self.alice, "channel_switched")
        self.assertIsNotNone(ack)
        self.assertEqual(ack["channel"]["id"], ch["id"])
        self.assertIn(self.alice, rs._rooms[ch["id"]])
        self.assertNotIn(self.alice, rs._rooms[rs.ROOM_ID])
        self.assertIn(self.bob, rs._rooms[rs.ROOM_ID])

        # 私人频道里的消息不会串到主频道
        await self._send(self.alice, {"action": "msg", "text": "青云界内说话"})
        self.assertIsNotNone(self._last(self.alice, "chat"))
        self.assertIsNone(self._last(self.bob, "chat"))
        # 机器人会话仍用主频道，但回复频道是私人仙界
        self.assertEqual(self.adapter.messages[-1][0], rs.ROOM_ID)
        self.assertEqual(self.adapter.messages[-1][4]["channel_id"], ch["id"])

        # 小博切进来后能收到，并能看到频道最近消息
        await self._send(self.bob, {"action": "switch", "channel_id": ch["id"]})
        ack = self._last(self.bob, "channel_switched")
        self.assertIsNotNone(ack)
        self.assertTrue(any(m.get("text") == "青云界内说话" for m in ack["history"]))
        await self._send(self.alice, {"action": "msg", "text": "欢迎小博"})
        self.assertIsNotNone(self._last(self.bob, "chat"))

    async def test_switch_requires_membership(self):
        ok, msg, ch = channels.create("alice", name="青云界")
        self.assertTrue(ok, msg)
        await self._send(self.bob, {"action": "switch", "channel_id": ch["id"]})
        fail = self._last(self.bob, "channel_error")
        self.assertIsNotNone(fail)
        self.assertIn("尚未加入", fail["text"])
        self.assertIn(self.bob, rs._rooms[rs.ROOM_ID])

    async def test_private_roster_only_lists_members(self):
        ok, msg, ch = channels.create("alice", name="青云界")
        self.assertTrue(ok, msg)
        acc.touch_room("alice")
        acc.touch_room("bob")

        # 主频道保留所有来过的人
        main_names = {m["username"] for m in rs.room_members(rs.ROOM_ID)}
        self.assertEqual(main_names, {"alice", "bob"})

        # 小博还没加入，私人仙界名单里不应出现他
        await self._send(self.alice, {"action": "switch", "channel_id": ch["id"]})
        private_names = {m["username"] for m in rs.room_members(ch["id"])}
        self.assertEqual(private_names, {"alice"})
        alice = next(m for m in rs.room_members(ch["id"]) if m["username"] == "alice")
        self.assertTrue(alice["online"])

        # 加入后即使离线也留在私人仙界名单里
        ok, msg, _ = channels.join("bob", ch["id"])
        self.assertTrue(ok, msg)
        by_name = {m["username"]: m for m in rs.room_members(ch["id"])}
        self.assertIn("bob", by_name)
        self.assertFalse(by_name["bob"]["online"])


class ChannelApiTest(unittest.IsolatedAsyncioTestCase):
    """HTTP 接口 + WS 握手恢复频道的端到端回归。"""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xiuxian_channels_api_"))
        self._orig = (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn)
        acc.STORE_DIR = self.tmp
        acc.DB_PATH = self.tmp / "accounts.db"
        acc.SECRET_PATH = self.tmp / "secret.key"
        acc._conn = None
        acc.init()
        channels.init()
        rs._rooms.clear()
        rs._recent_msgs.clear()
        rs._channel_history.clear()

        app = web.Application()
        app.router.add_post("/api/register", rs._api_register)
        app.router.add_get("/api/channels", rs._api_channels)
        app.router.add_post("/api/channels/create", rs._api_channels_create)
        app.router.add_post("/api/channels/join", rs._api_channels_join)
        app.router.add_post("/api/channels/update", rs._api_channels_update)
        app.router.add_get("/ws", rs._ws_handler)
        self.cli = TestClient(TestServer(app))
        await self.cli.start_server()

        self.tokens = {}
        for name in ("alice", "bob", "carol"):
            d = await (
                await self.cli.post(
                    "/api/register",
                    json={"username": name, "password": "secret1", "nick": name},
                )
            ).json()
            self.tokens[name] = d["token"]

    async def asyncTearDown(self):
        await self.cli.close()
        rs._rooms.clear()
        rs._recent_msgs.clear()
        rs._channel_history.clear()
        (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn) = self._orig
        if acc._conn is not None:
            acc._conn.close()
            acc._conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _h(self, name):
        return {"Authorization": "Bearer " + self.tokens[name]}

    async def _post(self, name, path, body):
        return await (await self.cli.post(path, json=body, headers=self._h(name))).json()

    async def _channels(self, name, q=""):
        url = "/api/channels" + (f"?q={q}" if q else "")
        return await (await self.cli.get(url, headers=self._h(name))).json()

    async def _hello(self, name, channel=""):
        url = f"/ws?token={self.tokens[name]}"
        if channel:
            url += f"&channel={channel}"
        async with self.cli.ws_connect(url) as ws:
            for _ in range(10):
                msg = await ws.receive_json()
                if msg.get("type") == "hello":
                    return msg
        return None

    async def test_http_visibility_join_and_ws_restore(self):
        ch = await self._post(
            "alice",
            "/api/channels/create",
            {"name": "幽冥界", "password": "1234", "public": False},
        )
        self.assertTrue(ch["ok"], ch)
        channel = ch["channel"]
        channel_id = channel["id"]
        # 创建者能看到 UUID
        self.assertEqual(channel["uuid"], channel_id)

        # 私密仙界不出现在大厅
        bob_view = await self._channels("bob")
        self.assertNotIn(channel_id, [c["id"] for c in bob_view["public"]])
        self.assertEqual([c["id"] for c in bob_view["joined"]], [channels.MAIN_CHANNEL_ID])

        # 不能按名称加入，只能凭 UUID + 密码
        by_name = await self._post("bob", "/api/channels/join", {"channel_id": "幽冥界"})
        self.assertFalse(by_name["ok"])
        self.assertIn("UUID", by_name["error"])
        bad_pwd = await self._post(
            "bob",
            "/api/channels/join",
            {"channel_id": channel_id, "password": "0000"},
        )
        self.assertFalse(bad_pwd["ok"])
        joined = await self._post(
            "bob",
            "/api/channels/join",
            {"channel_id": channel_id, "password": "1234"},
        )
        self.assertTrue(joined["ok"], joined)
        # 非创建者拿不到 UUID
        self.assertEqual(joined["channel"]["uuid"], "")

        # 非创建者不能修改
        denied = await self._post(
            "bob", "/api/channels/update", {"channel_id": channel_id, "name": "抢过来"}
        )
        self.assertFalse(denied["ok"])

        # 已加入的成员重连带 channel 参数可以恢复原频道
        hello = await self._hello("bob", channel_id)
        self.assertIsNotNone(hello)
        self.assertEqual(hello["room"], channel_id)
        self.assertEqual(hello["channel"]["id"], channel_id)

        # 未加入的玩家即使带 channel 参数也回落到主频道
        hello2 = await self._hello("carol", channel_id)
        self.assertIsNotNone(hello2)
        self.assertEqual(hello2["room"], channels.MAIN_CHANNEL_ID)

        # 关闭公开后仍在已加入列表中，但大厅消失
        public_ch = await self._post(
            "alice", "/api/channels/create", {"name": "青云界", "public": True}
        )
        self.assertTrue(public_ch["ok"], public_ch)
        public_id = public_ch["channel"]["id"]
        hall = await self._channels("bob", q="青云")
        self.assertIn(public_id, [c["id"] for c in hall["public"]])
        await self._post("alice", "/api/channels/update", {"channel_id": public_id, "public": False})
        hall2 = await self._channels("bob", q="青云")
        self.assertNotIn(public_id, [c["id"] for c in hall2["public"]])

    async def test_public_channel_join_by_name(self):
        ch = await self._post(
            "alice", "/api/channels/create", {"name": "青云界", "public": True}
        )
        self.assertTrue(ch["ok"], ch)
        channel_id = ch["channel"]["id"]
        r = await self._post("bob", "/api/channels/join", {"name": "青云界"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["channel"]["id"], channel_id)
        # 加入后出现在“我的仙界”
        view = await self._channels("bob")
        self.assertIn(channel_id, [c["id"] for c in view["joined"]])


if __name__ == "__main__":
    unittest.main()
