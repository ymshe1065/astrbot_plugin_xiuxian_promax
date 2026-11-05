# -*- coding: utf-8 -*-
"""webroom 消息撤回回归：消息 id、发送者鉴权、广播撤回。"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

_TMP = tempfile.mkdtemp(prefix="xiuxian_recall_")

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
    rs = __import__(f"{PKG}.webchat_room.room_server", fromlist=["x"])
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
    return {"player_id": f"web_{username}", "player_name": nick, "username": username, "avatar": ""}


class RecallTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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

    async def _send(self, ws, payload):
        await rs._handle_client_message(rs.ROOM_ID, ws, json.dumps(payload, ensure_ascii=False))

    def _last(self, ws, typ):
        for item in reversed(ws.sent):
            if item.get("type") == typ:
                return item
        return None

    async def test_chat_message_has_id_and_can_be_recalled(self):
        await self._send(self.sender, {"action": "msg", "text": "晚上好"})
        chat = self._last(self.other, "chat")
        self.assertIsNotNone(chat)
        self.assertTrue(chat.get("msg_id"))
        self.assertIn(chat["msg_id"], rs._recent_msgs)

        await self._send(self.sender, {"action": "recall", "msg_id": chat["msg_id"]})
        for ws in (self.sender, self.other):
            evt = self._last(ws, "recall")
            self.assertIsNotNone(evt)
            self.assertEqual(evt["msg_id"], chat["msg_id"])
            self.assertEqual(evt["username"], "alice")
        self.assertNotIn(chat["msg_id"], rs._recent_msgs)

    async def test_cannot_recall_other_players_message(self):
        await self._send(self.sender, {"action": "msg", "text": "只许我撤回"})
        msg_id = self._last(self.sender, "chat")["msg_id"]
        await self._send(self.other, {"action": "recall", "msg_id": msg_id})
        fail = self._last(self.other, "recall_fail")
        self.assertIsNotNone(fail)
        self.assertIn("只能撤回自己的", fail["text"])
        self.assertIn(msg_id, rs._recent_msgs)

    async def test_recall_twice_reports_expired(self):
        await self._send(self.sender, {"action": "msg", "text": "撤回一次"})
        msg_id = self._last(self.sender, "chat")["msg_id"]
        await self._send(self.sender, {"action": "recall", "msg_id": msg_id})
        await self._send(self.sender, {"action": "recall", "msg_id": msg_id})
        fail = self._last(self.sender, "recall_fail")
        self.assertIsNotNone(fail)
        self.assertIn("已过期", fail["text"])


if __name__ == "__main__":
    unittest.main()
