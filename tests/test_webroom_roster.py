# -*- coding: utf-8 -*-
"""房间成员名单：进过房间的人都留在列表里，只有状态区分在线/离线。"""
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

_TMP = tempfile.mkdtemp(prefix="xiuxian_roster_")

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
    accounts = __import__(f"{PKG}.webchat_room.accounts", fromlist=["x"])
    rs = __import__(f"{PKG}.webchat_room.room_server", fromlist=["x"])
except Exception as exc:  # pragma: no cover - 依赖缺失时跳过
    raise unittest.SkipTest(f"webroom 依赖不可用: {exc}")


class FakeWS:
    def __init__(self):
        self.sent = []


def _conn(username, nick):
    return {"player_id": f"web_{username}", "player_name": nick,
            "username": username, "avatar": ""}


class RosterTest(unittest.TestCase):
    def setUp(self):
        # 复用同一个库连接（Windows 上删 sqlite 文件会被占用），只清数据
        conn = accounts._db()
        conn.execute("DELETE FROM users")
        conn.commit()
        for name, nick in (("alice", "阿狸"), ("bob", "鲍勃"), ("carol", "卡萝")):
            accounts.register(name, "secret1", nick)
        rs._rooms.clear()

    def tearDown(self):
        rs._rooms.clear()

    def test_member_list_keeps_offline_users(self):
        accounts.touch_room("alice")
        accounts.touch_room("bob")
        accounts.touch_room("carol")
        ws = FakeWS()
        rs._rooms[rs.ROOM_ID] = {ws: _conn("bob", "鲍勃")}

        members = rs.room_members(rs.ROOM_ID)
        names = [m["username"] for m in members]
        self.assertEqual(sorted(names), ["alice", "bob", "carol"])

        by_name = {m["username"]: m for m in members}
        self.assertTrue(by_name["bob"]["online"])
        self.assertFalse(by_name["alice"]["online"])
        self.assertFalse(by_name["carol"]["online"])

    def test_online_sorted_first(self):
        accounts.touch_room("alice")   # 先来
        accounts.touch_room("bob")
        rs._rooms[rs.ROOM_ID] = {FakeWS(): _conn("alice", "阿狸")}
        members = rs.room_members(rs.ROOM_ID)
        self.assertEqual([m["username"] for m in members], ["alice", "bob"])

    def test_user_who_never_entered_is_hidden(self):
        accounts.touch_room("alice")
        accounts.register("dave", "secret1", "戴夫")
        usernames = [m["username"] for m in rs.room_members(rs.ROOM_ID)]
        self.assertEqual(usernames, ["alice"])

    def test_online_user_missing_from_db_still_listed(self):
        """老库刚补列、或写库失败时，在线的人也不能从名单里消失。"""
        rs._rooms[rs.ROOM_ID] = {FakeWS(): _conn("zoe", "佐伊")}
        members = rs.room_members(rs.ROOM_ID)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["username"], "zoe")
        self.assertTrue(members[0]["online"])

    def test_touch_room_does_not_touch_last_login_only(self):
        accounts.touch_room("alice")
        row = accounts.list_room_members()
        self.assertEqual(len(row), 1)
        self.assertGreater(row[0]["seen"], 0)


if __name__ == "__main__":
    unittest.main()
