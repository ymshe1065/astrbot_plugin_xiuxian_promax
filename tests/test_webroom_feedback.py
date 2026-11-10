# -*- coding: utf-8 -*-
"""网页端意见反馈：存储校验、频率限制与管理员处理接口回归。"""
import json
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

_TMP = tempfile.mkdtemp(prefix="xiuxian_feedback_")

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


def _fresh_db():
    if accounts._conn is not None:
        try:
            accounts._conn.close()
        except Exception:
            pass
        accounts._conn = None
    db_path = accounts.STORE_DIR / "accounts.db"
    if db_path.exists():
        db_path.unlink()
    accounts.init()


class FakeRequest:
    def __init__(self, method="GET", query=None, body=None, token=""):
        self.method = method
        self.query = query or {}
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._body = body or {}

    async def json(self):
        return self._body


class FakePlugin:
    def __init__(self, admins=None):
        self.boss_admins = list(admins or [])
        self.db = None


def _payload(response):
    return json.loads(response.text)


class FeedbackStoreTest(unittest.TestCase):
    def setUp(self):
        _fresh_db()
        accounts.register("alice", "secret1", "阿狸")

    def test_submit_validates_kind_and_length(self):
        ok, msg, _ = accounts.add_feedback("alice", kind="bad", content="这是一条足够长的反馈")
        self.assertFalse(ok)
        self.assertIn("类型", msg)

        ok, msg, _ = accounts.add_feedback("alice", kind="bug", content="短")
        self.assertFalse(ok)
        self.assertIn("至少", msg)

        ok, msg, item = accounts.add_feedback(
            "alice", player_key="web_alice", kind="bug", content="  战斗结算会丢失一次攻击  ",
            contact="qq123", snapshot={"realm": "筑基初期"},
        )
        self.assertTrue(ok)
        self.assertEqual(item["kind"], "bug")
        self.assertEqual(item["content"], "战斗结算会丢失一次攻击")
        self.assertEqual(item["status"], 0)
        self.assertEqual(item["status_label"], "待处理")
        self.assertEqual(item["snapshot"]["realm"], "筑基初期")

    def test_content_is_truncated_to_limit(self):
        ok, _, item = accounts.add_feedback(
            "alice", kind="suggestion", content="长" * (accounts.FEEDBACK_CONTENT_MAX + 50)
        )
        self.assertTrue(ok)
        self.assertEqual(len(item["content"]), accounts.FEEDBACK_CONTENT_MAX)

    def test_cooldown_between_submissions(self):
        ok, _, first = accounts.add_feedback("alice", kind="bug", content="第一条反馈内容")
        self.assertTrue(ok)
        ok, msg, _ = accounts.add_feedback("alice", kind="bug", content="第二条反馈内容")
        self.assertFalse(ok)
        self.assertIn("秒后再试", msg)

        # 把第一条时间往前拨，冷却结束后可再次提交
        with accounts._lock:
            db = accounts._db()
            db.execute(
                "UPDATE webroom_feedback SET created = ? WHERE id = ?",
                (int(time.time()) - accounts.FEEDBACK_COOLDOWN - 1, first["id"]),
            )
            db.commit()
        ok, _, _ = accounts.add_feedback("alice", kind="bug", content="第三条反馈内容")
        self.assertTrue(ok)

    def test_daily_limit(self):
        with accounts._lock:
            db = accounts._db()
            now = int(time.time())
            for i in range(accounts.FEEDBACK_DAILY_LIMIT):
                db.execute(
                    "INSERT INTO webroom_feedback "
                    "(username, player_key, kind, content, contact, snapshot, status, admin_note, created, updated) "
                    "VALUES (?,?,?,?,?,?,0,'',?,0)",
                    ("alice", "", "bug", f"历史反馈内容 {i}", "", "{}", now - 120),
                )
            db.commit()
        ok, msg, _ = accounts.add_feedback("alice", kind="bug", content="超过上限的反馈")
        self.assertFalse(ok)
        self.assertIn("上限", msg)

    def test_list_scoped_by_user_and_status(self):
        accounts.register("bob", "secret2", "小博")
        _, _, mine = accounts.add_feedback("alice", kind="bug", content="阿狸的反馈内容")
        _, _, other = accounts.add_feedback("bob", kind="balance", content="小博的反馈内容")
        self.assertEqual([i["id"] for i in accounts.list_feedback("alice")], [mine["id"]])
        self.assertEqual(len(accounts.list_feedback()), 2)
        accounts.update_feedback_status(other["id"], 1, "已修复")
        self.assertEqual(accounts.count_feedback(0), 1)
        pending = accounts.list_feedback(status=0)
        self.assertEqual([i["id"] for i in pending], [mine["id"]])

    def test_update_status_rejects_invalid_values(self):
        _, _, item = accounts.add_feedback("alice", kind="bug", content="等待处理的反馈")
        ok, _, _ = accounts.update_feedback_status(item["id"], 0)
        self.assertFalse(ok)
        ok, _, _ = accounts.update_feedback_status(99999, 1)
        self.assertFalse(ok)
        ok, _, updated = accounts.update_feedback_status(item["id"], 1, " 已处理 ")
        self.assertTrue(ok)
        self.assertEqual(updated["status"], 1)
        self.assertEqual(updated["admin_note"], "已处理")
        self.assertGreater(updated["updated"], 0)


class FeedbackApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _fresh_db()
        accounts.register("alice", "secret1", "阿狸")
        accounts.register("boss", "secret2", "掌柜")

    def _token(self, name):
        return accounts.make_token(name)

    async def test_submit_and_list_mine(self):
        request = FakeRequest(
            method="POST",
            token=self._token("alice"),
            body={
                "kind": "bug",
                "content": "秘境第三层会卡住不动",
                "contact": "qq123",
                "channel": "一区",
                "client_version": "web",
            },
        )
        with patch.object(rs, "_plugin", None):
            response = await rs._api_feedback(request)
        data = _payload(response)
        self.assertTrue(data["ok"])
        self.assertEqual(data["item"]["kind"], "bug")
        self.assertEqual(data["item"]["snapshot"]["channel"], "一区")
        self.assertNotIn("realm", data["item"]["snapshot"])

        listing = await rs._api_feedback(
            FakeRequest(method="GET", query={"scope": "mine"}, token=self._token("alice"))
        )
        data = _payload(listing)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["items"]), 1)
        self.assertFalse(data["is_admin"])

    async def test_invalid_submission_returns_400(self):
        response = await rs._api_feedback(
            FakeRequest(method="POST", token=self._token("alice"), body={"kind": "bug", "content": "短"})
        )
        self.assertEqual(response.status, 400)
        self.assertFalse(_payload(response)["ok"])

    async def test_non_admin_cannot_view_all(self):
        response = await rs._api_feedback(
            FakeRequest(method="GET", query={"scope": "all"}, token=self._token("alice"))
        )
        self.assertEqual(response.status, 403)

    async def test_admin_can_view_all_and_resolve(self):
        accounts.bind_qq("boss", "10001")
        await rs._api_feedback(
            FakeRequest(
                method="POST",
                token=self._token("alice"),
                body={"kind": "balance", "content": "帝品心法消耗太高了"},
            )
        )
        plugin = FakePlugin(admins=["10001"])
        with patch.object(rs, "_plugin", plugin):
            listing = await rs._api_feedback(
                FakeRequest(method="GET", query={"scope": "all"}, token=self._token("boss"))
            )
            data = _payload(listing)
            self.assertTrue(data["ok"])
            self.assertTrue(data["is_admin"])
            self.assertEqual(data["pending"], 1)
            self.assertEqual(len(data["items"]), 1)
            feedback_id = data["items"][0]["id"]

            resolved = await rs._api_feedback_status(
                FakeRequest(
                    method="POST",
                    token=self._token("boss"),
                    body={"id": feedback_id, "status": 1},
                )
            )
            data = _payload(resolved)
            self.assertTrue(data["ok"])
            self.assertEqual(data["pending"], 0)

        denied = await rs._api_feedback_status(
            FakeRequest(
                method="POST",
                token=self._token("alice"),
                body={"id": feedback_id, "status": 1},
            )
        )
        self.assertEqual(denied.status, 403)

    async def test_expired_token_returns_401(self):
        response = await rs._api_feedback(FakeRequest(method="GET"))
        self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
