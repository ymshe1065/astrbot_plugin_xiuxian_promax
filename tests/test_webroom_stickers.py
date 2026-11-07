# -*- coding: utf-8 -*-
"""网页聊天室内置贴吧表情包的回归测试。

覆盖：
- 表情目录完整、id 唯一、图片文件随插件存在；
- 客户端提交 id 后服务端按白名单广播，不进入机器人流水线；
- 非法 id 被忽略，不能借此注入任意图片地址；
- 表情消息仍可撤回。
"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

_TMP = tempfile.mkdtemp(prefix="xiuxian_stickers_")

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
    stickers = __import__(f"{PKG}.webchat_room.stickers", fromlist=["x"])
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


class StickerCatalogTest(unittest.TestCase):
    def test_catalog_is_complete_and_unique(self):
        items = stickers.catalog()
        self.assertEqual(len(items), 60)
        ids = [item["id"] for item in items]
        labels = [item["label"] for item in items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(labels), len(set(labels)))
        self.assertTrue(all(item["url"].startswith(stickers.STICKER_URL_PREFIX) for item in items))

    def test_sticker_files_exist(self):
        for item in stickers.STICKERS:
            path = stickers.STICKER_DIR / item["file"]
            self.assertTrue(path.is_file(), f"缺少表情图片: {path}")
            self.assertGreater(path.stat().st_size, 100)

    def test_lookup_by_id_and_tieba_code(self):
        huaji = stickers.get("huaji")
        self.assertIsNotNone(huaji)
        self.assertEqual(huaji["label"], "滑稽")
        self.assertEqual(stickers.find_by_label("滑稽"), huaji)
        self.assertIsNone(stickers.get("not-a-sticker"))


class StickerMessageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sender = FakeWS()
        self.other = FakeWS()
        self.adapter = FakeAdapter()
        self._orig_adapter = rs._adapter
        rs._adapter = self.adapter
        rs._rooms.clear()
        rs._recent_msgs.clear()
        rs._channel_history.clear()
        rs._rooms[rs.ROOM_ID] = {
            self.sender: _info("alice", "阿狸"),
            self.other: _info("bob", "小博"),
        }

    async def asyncTearDown(self):
        rs._adapter = self._orig_adapter
        rs._rooms.clear()
        rs._recent_msgs.clear()
        rs._channel_history.clear()

    async def _send(self, ws, payload):
        await rs._handle_client_message(
            rs.ROOM_ID, ws, json.dumps(payload, ensure_ascii=False)
        )

    def _last(self, ws, typ):
        for item in reversed(ws.sent):
            if item.get("type") == typ:
                return item
        return None

    async def test_sticker_broadcast_without_bot_pipeline(self):
        await self._send(self.sender, {"action": "msg", "sticker": "huaji"})

        for ws in (self.sender, self.other):
            chat = self._last(ws, "chat")
            self.assertIsNotNone(chat)
            self.assertEqual(chat["text"], "")
            self.assertEqual(chat["sticker"]["id"], "huaji")
            self.assertEqual(chat["sticker"]["label"], "滑稽")
            self.assertTrue(chat["sticker"]["url"].endswith("tieba_33_huaji.png"))
            self.assertTrue(chat["msg_id"])

        # 表情只是聊天内容，不应触发机器人回复
        self.assertEqual(self.adapter.messages, [])

    async def test_invalid_sticker_is_ignored(self):
        await self._send(
            self.sender,
            {"action": "msg", "sticker": "../../etc/passwd"},
        )
        self.assertIsNone(self._last(self.other, "chat"))
        self.assertEqual(self.adapter.messages, [])

    async def test_tieba_shortcut_text_becomes_sticker(self):
        await self._send(self.sender, {"action": "msg", "text": "#(滑稽)"})
        chat = self._last(self.other, "chat")
        self.assertIsNotNone(chat)
        self.assertEqual(chat["text"], "")
        self.assertEqual(chat["sticker"]["id"], "huaji")
        self.assertEqual(self.adapter.messages, [])

    async def test_sticker_message_can_be_recalled(self):
        await self._send(self.sender, {"action": "msg", "sticker": "huaji"})
        msg_id = self._last(self.sender, "chat")["msg_id"]

        await self._send(self.sender, {"action": "recall", "msg_id": msg_id})
        for ws in (self.sender, self.other):
            evt = self._last(ws, "recall")
            self.assertIsNotNone(evt)
            self.assertEqual(evt["msg_id"], msg_id)
        self.assertNotIn(msg_id, rs._recent_msgs)


if __name__ == "__main__":
    unittest.main()
