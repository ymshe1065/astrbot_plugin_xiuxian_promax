# -*- coding: utf-8 -*-
"""webroom 私聊（DM）功能回归测试（设计方案 §12.7）。

覆盖：dm_store 存储层（会话/历史/未读/拉黑/30 天清理）、
WS dm.* 消息族（发送投递/已读回执/限速）、REST /api/dm/* 权限与流程。
"""
import json
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="xiuxian_dm_boot_")

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
    def debug(self, *a, **k): pass


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
    from webchat_room import dm_store
    from webchat_room import room_server as rs
except Exception as exc:  # pragma: no cover
    raise unittest.SkipTest(f"webroom 依赖不可用: {exc}")


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class _StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xiuxian_dm_"))
        self._orig = (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn)
        acc.STORE_DIR = self.tmp
        acc.DB_PATH = self.tmp / "accounts.db"
        acc.SECRET_PATH = self.tmp / "secret.key"
        acc._conn = None
        acc.init()
        channels.init()
        dm_store.init()
        acc.register("alice", "secret1", "阿狸")
        acc.register("bob", "secret1", "小博")
        acc.register("carol", "secret1", "卡萝")

    def tearDown(self):
        (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn) = self._orig
        if acc._conn is not None:
            acc._conn.close()
            acc._conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)


class DMStoreTest(_StoreBase):
    def test_convo_id_symmetric(self):
        self.assertEqual(dm_store.convo_id("alice", "bob"),
                         dm_store.convo_id("bob", "alice"))
        self.assertEqual(dm_store.convo_id("bob", "alice"), "alice|bob")

    def test_send_and_history_both_sides(self):
        ok, err, msg = dm_store.send("alice", "bob", "在吗道友")
        self.assertTrue(ok, err)
        self.assertEqual(msg["sender"], "alice")
        self.assertEqual(msg["recipient"], "bob")
        self.assertFalse(msg["read"])
        ok2, _, msg2 = dm_store.send("bob", "alice", "在的")
        self.assertTrue(ok2)
        # 双方看到同一会话的完整历史（升序）
        h_alice = dm_store.history("alice", "bob")
        h_bob = dm_store.history("bob", "alice")
        self.assertEqual([m["content"] for m in h_alice], ["在吗道友", "在的"])
        self.assertEqual([m["id"] for m in h_alice], [m["id"] for m in h_bob])
        # 分页：before 截断
        page = dm_store.history("alice", "bob", limit=1)
        self.assertEqual([m["content"] for m in page], ["在的"])
        older = dm_store.history("alice", "bob", limit=10, before=page[0]["id"])
        self.assertEqual([m["content"] for m in older], ["在吗道友"])

    def test_conversations_unread_and_mark_read(self):
        dm_store.send("alice", "bob", "消息一")
        dm_store.send("alice", "bob", "消息二")
        dm_store.send("carol", "bob", "卡萝问安")
        convos = dm_store.conversations("bob")
        self.assertEqual(len(convos), 2)
        by_peer = {c["peer"]: c for c in convos}
        self.assertEqual(by_peer["alice"]["unread"], 2)
        self.assertEqual(by_peer["alice"]["peer_nick"], "阿狸")
        self.assertEqual(by_peer["alice"]["preview"], "消息二")
        self.assertEqual(by_peer["carol"]["unread"], 1)
        self.assertEqual(dm_store.unread_total("bob"), 3)
        # alice 视角：自己发出的消息不计未读
        convos_a = dm_store.conversations("alice")
        self.assertEqual(convos_a[0]["unread"], 0)
        # 标读一个会话
        n = dm_store.mark_read("bob", "alice")
        self.assertEqual(n, 2)
        self.assertEqual(dm_store.unread_total("bob"), 1)
        self.assertFalse(any(m["read"] is False for m in
                             dm_store.history("bob", "alice")))
        # 重复标读幂等
        self.assertEqual(dm_store.mark_read("bob", "alice"), 0)

    def test_block_semantics(self):
        ok, msg = dm_store.block("bob", "alice")
        self.assertTrue(ok, msg)
        self.assertTrue(dm_store.is_blocked("bob", "alice"))
        self.assertFalse(dm_store.is_blocked("alice", "bob"))
        # 被拉黑方发送失败（不泄露拉黑状态），消息不落库
        ok2, err2, m2 = dm_store.send("alice", "bob", "为何不理我")
        self.assertFalse(ok2)
        self.assertEqual(err2, "消息发送失败")
        self.assertIsNone(m2)
        self.assertEqual(dm_store.history("alice", "bob"), [])
        # 反向不受影响
        ok3, _, _ = dm_store.send("bob", "alice", "勿念")
        self.assertTrue(ok3)
        # 拉黑名单
        blocks = dm_store.blocks("bob")
        self.assertEqual([b["username"] for b in blocks], ["alice"])
        self.assertEqual(blocks[0]["nick"], "阿狸")
        # 解除后恢复
        ok4, _ = dm_store.unblock("bob", "alice")
        self.assertTrue(ok4)
        ok5, _, _ = dm_store.send("alice", "bob", "回来了")
        self.assertTrue(ok5)

    def test_send_validations(self):
        self.assertFalse(dm_store.send("alice", "alice", "自言自语")[0])
        self.assertFalse(dm_store.send("alice", "", "无人")[0])
        self.assertFalse(dm_store.send("alice", "bob", "   ")[0])
        ok, err, _ = dm_store.send("alice", "ghost", "查无此人")
        self.assertFalse(ok)
        self.assertIn("不存在", err)
        ok, err, _ = dm_store.send("alice", "bob", "长" * 2001)
        self.assertFalse(ok)
        self.assertIn("过长", err)
        # 边界：恰好 2000 字可以发
        ok, err, _ = dm_store.send("alice", "bob", "长" * 2000)
        self.assertTrue(ok, err)

    def test_prune_retention(self):
        dm_store.send("alice", "bob", "新鲜消息")
        # 直接插一条 31 天前的旧消息
        cid = dm_store.convo_id("alice", "carol")
        old = int(time.time()) - 31 * 86400
        acc._db().execute(
            "INSERT INTO dm_conversations (id, user_a, user_b, preview, updated_at) "
            "VALUES (?,?,?,?,?)", (cid, "alice", "carol", "旧闻", old))
        acc._db().execute(
            "INSERT INTO dm_messages "
            "(convo_id, sender, recipient, content, created, read) "
            "VALUES (?,?,?,?,?,1)", (cid, "alice", "carol", "旧闻", old))
        acc._db().commit()
        removed = dm_store.prune()
        self.assertEqual(removed, 1)
        # 旧会话随消息清空被删；新消息保留
        self.assertEqual(dm_store.history("alice", "carol"), [])
        self.assertEqual(len(dm_store.history("alice", "bob")), 1)
        convos = dm_store.conversations("alice")
        self.assertEqual([c["peer"] for c in convos], ["bob"])


class DMWsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xiuxian_dm_ws_"))
        self._orig = (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn)
        acc.STORE_DIR = self.tmp
        acc.DB_PATH = self.tmp / "accounts.db"
        acc.SECRET_PATH = self.tmp / "secret.key"
        acc._conn = None
        acc.init()
        channels.init()
        dm_store.init()
        acc.register("alice", "secret1", "阿狸")
        acc.register("bob", "secret1", "小博")
        self.alice = FakeWS()
        self.bob = FakeWS()
        rs._rooms.clear()
        rs._dm_rate.clear()
        rs._rooms[rs.ROOM_ID] = {
            self.alice: {"player_id": "web_alice", "player_name": "阿狸",
                         "username": "alice", "avatar": "", "room": rs.ROOM_ID},
            self.bob: {"player_id": "web_bob", "player_name": "小博",
                       "username": "bob", "avatar": "", "room": rs.ROOM_ID},
        }

    async def asyncTearDown(self):
        rs._rooms.clear()
        rs._dm_rate.clear()
        (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn) = self._orig
        if acc._conn is not None:
            acc._conn.close()
            acc._conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _send(self, ws, payload):
        await rs._handle_client_message(
            rs.ROOM_ID, ws, json.dumps(payload, ensure_ascii=False))

    def _last(self, ws, typ):
        for item in reversed(ws.sent):
            if item.get("type") == typ:
                return item
        return None

    async def test_dm_send_delivers_to_recipient(self):
        await self._send(self.alice, {
            "action": "dm.send", "to": "bob", "text": "晚上打城吗", "req_id": "r1"})
        sent = self._last(self.alice, "dm.sent")
        self.assertIsNotNone(sent)
        self.assertEqual(sent["req_id"], "r1")
        self.assertEqual(sent["message"]["content"], "晚上打城吗")
        new = self._last(self.bob, "dm.new")
        self.assertIsNotNone(new)
        self.assertEqual(new["message"]["sender"], "alice")
        # 公屏不受影响
        self.assertIsNone(self._last(self.bob, "chat"))

    async def test_dm_history_list_read_receipt(self):
        await self._send(self.alice, {"action": "dm.send", "to": "bob", "text": "在？"})
        await self._send(self.bob, {"action": "dm.history", "with": "alice"})
        hist = self._last(self.bob, "dm.history")
        self.assertEqual([m["content"] for m in hist["items"]], ["在？"])
        await self._send(self.bob, {"action": "dm.list"})
        lst = self._last(self.bob, "dm.list")
        self.assertEqual(lst["unread"], 1)
        self.assertEqual(lst["items"][0]["peer"], "alice")
        # 标读：自己收到 read_ok，对方收到已读回执
        await self._send(self.bob, {"action": "dm.read", "with": "alice"})
        read_ok = self._last(self.bob, "dm.read_ok")
        self.assertEqual(read_ok["unread"], 0)
        receipt = self._last(self.alice, "dm.read")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["by"], "bob")

    async def test_dm_block_prevents_delivery(self):
        await self._send(self.bob, {"action": "dm.block", "target": "alice"})
        block_ok = self._last(self.bob, "dm.block_ok")
        self.assertTrue(block_ok["ok"])
        self.assertTrue(block_ok["blocked"])
        await self._send(self.alice, {"action": "dm.send", "to": "bob", "text": "?"})
        err = self._last(self.alice, "dm.error")
        self.assertIsNotNone(err)
        self.assertEqual(err["text"], "消息发送失败")
        self.assertIsNone(self._last(self.bob, "dm.new"))
        # 解除拉黑后恢复投递
        await self._send(self.bob, {"action": "dm.unblock", "target": "alice"})
        unblock = self._last(self.bob, "dm.block_ok")
        self.assertFalse(unblock["blocked"])
        await self._send(self.alice, {"action": "dm.send", "to": "bob", "text": "好了"})
        self.assertIsNotNone(self._last(self.bob, "dm.new"))

    async def test_dm_rate_limit(self):
        for i in range(2):
            await self._send(self.alice, {
                "action": "dm.send", "to": "bob", "text": f"连发{i}"})
        await self._send(self.alice, {
            "action": "dm.send", "to": "bob", "text": "第三条"})
        err = self._last(self.alice, "dm.error")
        self.assertIsNotNone(err)
        self.assertIn("太快", err["text"])
        # 前两条已送达，第三条被丢弃
        delivered = [m for m in self.bob.sent if m.get("type") == "dm.new"]
        self.assertEqual(len(delivered), 2)

    async def test_dm_requires_handshake(self):
        lone = FakeWS()
        await rs._handle_client_message(
            rs.ROOM_ID, lone, json.dumps({"action": "dm.list"}))
        err = self._last(lone, "error")
        self.assertIsNotNone(err)
        self.assertIn("握手", err["text"])


class DMApiTest(unittest.IsolatedAsyncioTestCase):
    """REST /api/dm/* 端到端：鉴权 / 发送投递 / 历史 / 标读 / 拉黑。"""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xiuxian_dm_api_"))
        self._orig = (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn)
        acc.STORE_DIR = self.tmp
        acc.DB_PATH = self.tmp / "accounts.db"
        acc.SECRET_PATH = self.tmp / "secret.key"
        acc._conn = None
        acc.init()
        channels.init()
        dm_store.init()
        rs._rooms.clear()
        rs._dm_rate.clear()

        app = web.Application()
        app.router.add_post("/api/register", rs._api_register)
        app.router.add_get("/api/dm/conversations", rs._api_dm_conversations)
        app.router.add_get("/api/dm/history", rs._api_dm_history)
        app.router.add_get("/api/dm/blocks", rs._api_dm_blocks)
        app.router.add_post("/api/dm/send", rs._api_dm_send)
        app.router.add_post("/api/dm/read", rs._api_dm_read)
        app.router.add_post("/api/dm/block", rs._api_dm_block)
        app.router.add_post("/api/dm/unblock", rs._api_dm_unblock)
        self.cli = TestClient(TestServer(app))
        await self.cli.start_server()

        self.tokens = {}
        for name in ("alice", "bob"):
            d = await (await self.cli.post(
                "/api/register",
                json={"username": name, "password": "secret1", "nick": name},
            )).json()
            self.tokens[name] = d["token"]

    async def asyncTearDown(self):
        await self.cli.close()
        rs._rooms.clear()
        rs._dm_rate.clear()
        (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn) = self._orig
        if acc._conn is not None:
            acc._conn.close()
            acc._conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _h(self, name):
        return {"Authorization": "Bearer " + self.tokens[name]}

    async def _post(self, name, path, body):
        return await (await self.cli.post(
            path, json=body, headers=self._h(name))).json()

    async def _get(self, name, path):
        return await (await self.cli.get(path, headers=self._h(name))).json()

    async def test_unauthorized(self):
        resp = await self.cli.get("/api/dm/conversations")
        self.assertEqual(resp.status, 401)

    async def test_full_flow(self):
        # alice → bob 发送
        r = await self._post("alice", "/api/dm/send", {"to": "bob", "text": "结盟吗"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["message"]["recipient"], "bob")
        # bob 会话列表：1 未读
        convos = await self._get("bob", "/api/dm/conversations")
        self.assertTrue(convos["ok"])
        self.assertEqual(convos["unread"], 1)
        self.assertEqual(convos["items"][0]["peer"], "alice")
        # bob 拉历史并标读
        hist = await self._get("bob", "/api/dm/history?with=alice")
        self.assertEqual([m["content"] for m in hist["items"]], ["结盟吗"])
        r = await self._post("bob", "/api/dm/read", {"with": "alice"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["unread"], 0)
        # bob 拉黑 alice → alice 发送失败（400）
        r = await self._post("bob", "/api/dm/block", {"target": "alice"})
        self.assertTrue(r["ok"], r)
        resp = await self.cli.post("/api/dm/send", headers=self._h("alice"),
                                   json={"to": "bob", "text": "？"})
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertEqual(body["error"], "消息发送失败")
        # 拉黑名单与解除
        blocks = await self._get("bob", "/api/dm/blocks")
        self.assertEqual([b["username"] for b in blocks["items"]], ["alice"])
        r = await self._post("bob", "/api/dm/unblock", {"target": "alice"})
        self.assertTrue(r["ok"])
        # 前面已发 3 条（含被拉黑失败的一条，限速先于校验），重置限速窗口再验证恢复
        rs._dm_rate.clear()
        r = await self._post("alice", "/api/dm/send", {"to": "bob", "text": "再试"})
        self.assertTrue(r["ok"], r)

    async def test_rate_limit_429(self):
        for i in range(2):
            r = await self._post("alice", "/api/dm/send",
                                 {"to": "bob", "text": f"快{i}"})
            self.assertTrue(r["ok"], r)
        resp = await self.cli.post("/api/dm/send", headers=self._h("alice"),
                                   json={"to": "bob", "text": "第三条"})
        self.assertEqual(resp.status, 429)

    async def test_param_validation(self):
        r = await self._post("alice", "/api/dm/send", {"to": "", "text": "无人"})
        self.assertFalse(r["ok"])
        r = await self._post("alice", "/api/dm/send", {"to": "bob", "text": ""})
        self.assertFalse(r["ok"])
        resp = await self.cli.get("/api/dm/history", headers=self._h("alice"))
        self.assertEqual(resp.status, 400)
        resp = await self.cli.get("/api/dm/history?with=bob&limit=abc",
                                  headers=self._h("alice"))
        self.assertEqual(resp.status, 400)


if __name__ == "__main__":
    unittest.main()
