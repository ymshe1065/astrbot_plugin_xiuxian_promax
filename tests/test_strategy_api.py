# -*- coding: utf-8 -*-
"""「仙域争霸」网页 API 层回归测试（设计方案 §12.2 / §12.5 / §19 / §20）。

覆盖：
- REST /api/strategy/state|action：401 未登录 / 503 插件未就绪 / 400 StrategyError
  / 423 世界推进中 / enter 自动补昵称 / 正常 state 快照；
- WS 通知链路：StrategyManager._notify 档位过滤 + room_server.notify_strategy
  按 player_key 解析网页账号并推送 strategy.notify；
- 服务器权威：battle_state 不泄露 map_seed、未揭晓伏兵 pos=None、
  get_state 敌军 grain/veteran 战争迷雾；
- GM 运维通道 gm()：开世界 / tick / 注入事件 / 势力平衡 / 未知操作。
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests._astrbot_stub import install as _install_astrbot_stub  # noqa: E402

_install_astrbot_stub()

# webroom 依赖 astrbot.core.utils.astrbot_path（accounts/room_server 模块级取数据目录）
_TMP = tempfile.mkdtemp(prefix="xiuxian_strategy_api_boot_")
_path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
_path_mod.get_astrbot_data_path = lambda: _TMP
_core_utils = sys.modules.setdefault(
    "astrbot.core.utils", types.ModuleType("astrbot.core.utils"))
if not hasattr(_core_utils, "astrbot_path"):
    _core_utils.astrbot_path = _path_mod
sys.modules.setdefault("astrbot.core.utils.astrbot_path", _path_mod)


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


def _now() -> int:
    import time
    return int(time.time())


# 先以 PKG 限定名导入 managers，再把异常模块别名进 sys.modules：
# strategy_panel 的 ImportError 兜底会优先复用已注册模块，保证
# room_server 捕获的 StrategyError/StrategyBusyError 与 manager 抛出的是同一类。
_err_mod = _imp("managers.strategy_errors")
sys.modules["managers.strategy_errors"] = _err_mod

try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from webchat_room import accounts as acc
    from webchat_room import channels
    from webchat_room import room_server as rs
except Exception as exc:  # pragma: no cover
    raise unittest.SkipTest(f"webroom 依赖不可用: {exc}")

# 防御：若 room_server 已被其他测试先导入（strategy_panel 兜底加载了独立
# 异常模块副本），把捕获用的异常类重绑到 manager 使用的同一身份上。
rs.StrategyError = _err_mod.StrategyError
rs.StrategyBusyError = _err_mod.StrategyBusyError

sm_mod = _imp("managers.strategy_manager")
StrategyManager = sm_mod.StrategyManager
StrategyError = _err_mod.StrategyError


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class FakePlugin:
    """只带 strategy_mgr/db 两个属性的插件替身（strategy_panel 只取这两个）。"""

    def __init__(self, db, strategy_mgr):
        self.db = db
        self.strategy_mgr = strategy_mgr


class StubStrategyManager:
    """WS 协议测试替身：只记录 handle_action 调用并按需抛错。"""

    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result if result is not None else {
            "ok": True, "round": 2, "events": ["第 2 回合开始"],
            "current_actor": 1,
        }
        self.error = error

    async def handle_action(self, user_id, action, params=None):
        self.calls.append((user_id, action, dict(params or {})))
        if self.error is not None:
            raise self.error
        return dict(self.result)

    async def get_battle(self, battle_id):
        return {"id": int(battle_id),
                "session": {"owners": {"1": {"owner": "web_alice"}}}}


class _ManagerBase(unittest.IsolatedAsyncioTestCase):
    """主玩法 DB + StrategyManager 的最小骨架（同 test_strategy_integration_p1p3）。"""

    async def _setup_manager(self, users=("web_alice", "web_bob")):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        Player = _imp("models").Player

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        for uid in users:
            await self.db.create_player(Player(
                user_id=uid, gold=10_000_000, user_name=f"测试{uid}"))

        self.m = StrategyManager(self.db)
        # 野战遭遇默认改为手操窗口；常规数值回归按自动结算口径跑
        self.m.main_cfg().setdefault("hex", {})["field_encounter_mode"] = "auto"
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        self.plugin = FakePlugin(self.db, self.m)

    async def _teardown_manager(self):
        await self.db.close()
        os.remove(self.db_path)

    # ------------------------------------------------------------------
    # 工具（与集成测试同款）
    # ------------------------------------------------------------------
    async def _give(self, uid: str, **res):
        p = await self.m.get_player(uid, self.wid)
        for k, v in res.items():
            p[k] = int(v)
        await self.m.save_player(p)
        await self.m._commit()
        return p

    async def _char_of(self, uid: str, role: str = "general"):
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == role:
                return c
        return None

    async def _mk_army(self, uid: str, node_id: int, troops: int = 800):
        await self._give(uid, coin=5_000_000, grain=5_000_000)
        c = await self._char_of(uid)
        # §4 所在地机制：编军要求主将已在该据点
        c["location"] = {"node_id": int(node_id)}
        c["status"] = "idle"
        await self.m.save_char(c)
        r = await self.m.create_army(uid, c["id"], [], node_id,
                                     "sword_shield", troops)
        return await self.m.get_army(r["army_id"])

    async def _own(self, uid: str, node: dict) -> dict:
        p = await self.m.get_player(uid, self.wid)
        node["owner_faction"] = int(p["faction_id"])
        await self.m.save_node(node)
        return node

    async def _free_node(self) -> int:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        nodes.sort(key=lambda n: (n["type"] not in ("city", "pass", "branch"),
                                  -int(n.get("level", 1)), int(n["id"])))
        self.assertTrue(nodes, "没有可选起始据点")
        return int(nodes[0]["id"])

    async def _enter(self, user_id: str, nickname: str = "") -> dict:
        return await self.m.enter_game(user_id, nickname,
                                       node_id=await self._free_node())


class _AccountsBase(unittest.TestCase):
    """webroom 账号库临时化（同 test_webroom_dm）。"""

    def _accounts_setup(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xiuxian_strategy_api_"))
        self._orig = (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn)
        acc.STORE_DIR = self.tmp
        acc.DB_PATH = self.tmp / "accounts.db"
        acc.SECRET_PATH = self.tmp / "secret.key"
        acc._conn = None
        acc.init()
        channels.init()
        rs._rooms.clear()

    def _accounts_teardown(self):
        rs._rooms.clear()
        (acc.STORE_DIR, acc.DB_PATH, acc.SECRET_PATH, acc._conn) = self._orig
        if acc._conn is not None:
            acc._conn.close()
            acc._conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)


class StrategyRestApiTest(_AccountsBase, _ManagerBase):
    """REST /api/strategy/state|action 端到端。"""

    async def asyncSetUp(self):
        self._accounts_setup()
        await self._setup_manager()
        rs.set_plugin(self.plugin)

        app = web.Application()
        app.router.add_post("/api/register", rs._api_register)
        app.router.add_get("/api/strategy/state", rs._api_strategy_state)
        app.router.add_post("/api/strategy/action", rs._api_strategy_action)
        app.router.add_get("/api/strategy/memorial", rs._api_strategy_memorial)
        app.router.add_get("/api/strategy/guide", rs._api_strategy_guide)
        sel = TestServer(app)
        self.cli = TestClient(sel)
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
        rs.set_plugin(None)
        await self._teardown_manager()
        self._accounts_teardown()

    def _h(self, name):
        return {"Authorization": "Bearer " + self.tokens[name]}

    async def _action(self, name, action, params=None):
        params = dict(params or {})
        if action == "enter" and not params.get("node_id"):
            params["node_id"] = await self._free_node()
        return await (await self.cli.post(
            "/api/strategy/action", headers=self._h(name),
            json={"action": action, "params": params})).json()

    async def test_unauthorized(self):
        resp = await self.cli.get("/api/strategy/state")
        self.assertEqual(resp.status, 401)
        resp = await self.cli.post("/api/strategy/action",
                                   json={"action": "enter"})
        self.assertEqual(resp.status, 401)

    async def test_plugin_not_ready_503(self):
        rs.set_plugin(None)
        try:
            resp = await self.cli.get("/api/strategy/state",
                                      headers=self._h("alice"))
            self.assertEqual(resp.status, 503)
            resp = await self.cli.post("/api/strategy/action",
                                       headers=self._h("alice"),
                                       json={"action": "enter"})
            self.assertEqual(resp.status, 503)
        finally:
            rs.set_plugin(self.plugin)

    async def test_missing_action_400(self):
        # 空 body → 请求格式不对
        resp = await self.cli.post("/api/strategy/action",
                                   headers=self._h("alice"), json={})
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn("格式", body["error"])
        # 有 body 但缺 action → 缺少 action
        resp = await self.cli.post("/api/strategy/action",
                                   headers=self._h("alice"),
                                   json={"params": {}})
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn("action", body["error"])

    async def test_enter_requires_manual_node_selection(self):
        resp = await self.cli.get("/api/strategy/state", headers=self._h("alice"))
        st = (await resp.json())["state"]
        self.assertFalse(st["joined"])
        self.assertTrue(st["start_candidates"])
        # 未选据点直接入场 → 400
        bad = await self.cli.post(
            "/api/strategy/action", headers=self._h("alice"),
            json={"action": "enter", "params": {}})
        self.assertEqual(bad.status, 400)
        self.assertIn("据点", (await bad.json())["error"])
        # 选定候选据点后入场，主城即所选据点
        nid = int(st["start_candidates"][0]["id"])
        r = await self._action("alice", "enter", {"node_id": nid})
        self.assertTrue(r["ok"], r)
        resp = await self.cli.get("/api/strategy/state", headers=self._h("alice"))
        st2 = (await resp.json())["state"]
        self.assertTrue(st2["joined"])
        self.assertEqual(int(st2["player"]["faction"]["capital_node_id"]), nid)

    async def test_enter_autofills_nickname_then_state(self):
        # enter 不带 nickname → strategy_action 用网页昵称补齐
        r = await self._action("alice", "enter")
        self.assertTrue(r["ok"], r)
        resp = await self.cli.get("/api/strategy/state", headers=self._h("alice"))
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["ok"])
        st = data["state"]
        self.assertTrue(st["joined"])
        self.assertEqual(st["world"]["id"], self.wid)
        self.assertTrue(st["nodes"])
        self.assertIn("positions", st["meta"])
        self.assertIn("wanderers", st)
        self.assertIn("petitions", st)
        self.assertIn("winds", st)
        # player_key = web_alice，昵称自动补 "alice"
        p = st["player"]
        self.assertEqual(p["user_id"], "web_alice")
        self.assertIsNotNone(p["faction"])

    async def test_strategy_error_maps_to_400(self):
        await self._action("alice", "enter")
        resp = await self.cli.post(
            "/api/strategy/action", headers=self._h("alice"),
            json={"action": "faction_apply", "params": {"faction_id": 999999}})
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("不存在", body["error"])

    async def test_busy_maps_to_423(self):
        await self._action("alice", "enter")
        await self.m._world_lock.acquire()
        try:
            resp = await self.cli.post(
                "/api/strategy/action", headers=self._h("alice"),
                json={"action": "faction_quit", "params": {}})
            self.assertEqual(resp.status, 423)
            body = await resp.json()
            self.assertIn("推进中", body["error"])
        finally:
            self.m._world_lock.release()
        # 锁释放后恢复正常
        r = await self._action("alice", "set_notify_pref", {"level": 2})
        self.assertTrue(r["ok"], r)

    # ------------------------------------------------------------------
    # §12.5 新端点：名将堂 / 指引索引
    # ------------------------------------------------------------------
    async def test_memorial_returns_hall_and_shrine(self):
        await self._action("alice", "enter")
        rows = (
            ("hall_of_fame", "关羽", "hall_of_fame", "蜀汉",
             json.dumps({"name": "关羽", "rarity": "legend",
                         "merit_season": 3200, "achievements": ["首破坚城"]},
                        ensure_ascii=False), 11),
            ("fame", "张飞", "fame", "蜀汉",
             json.dumps({"name": "张飞", "rarity": "epic",
                         "merit": 900, "achievements": []},
                        ensure_ascii=False), 12),
        )
        for kind, name, _k, faction, snap, season in rows:
            await self.db.conn.execute(
                "INSERT INTO strategy_memorials (season_no, world_id, kind,"
                " char_name, user_id, faction_name, snapshot_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (season, self.wid, kind, name, "web_alice", faction, snap, _now()))
        await self.m._commit()

        resp = await self.cli.get("/api/strategy/memorial", headers=self._h("alice"))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(len(body["hall"]), 1)
        self.assertEqual(len(body["shrine"]), 1)
        self.assertGreaterEqual(int(body["season_no"]), 1)
        top = body["hall"][0]
        for key in ("name", "rarity", "faction", "season", "merit",
                    "achievements", "kind"):
            self.assertIn(key, top)
        # 名将堂对任何登录玩家可见，不能下发全服存档主键 user_id
        self.assertNotIn("user_id", top)
        self.assertEqual(top["name"], "关羽")
        self.assertEqual(top["merit"], 3200)
        self.assertEqual(body["shrine"][0]["kind"], "fame")

    async def test_memorial_unjoined_returns_empty(self):
        # bob 未入场 → 200 + 空榜（不报 500 / 401）
        resp = await self.cli.get("/api/strategy/memorial", headers=self._h("bob"))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["hall"], [])
        self.assertEqual(body["shrine"], [])
        self.assertFalse(body.get("joined", True))

    async def test_memorial_and_guide_unauthorized(self):
        for path in ("/api/strategy/memorial", "/api/strategy/guide"):
            resp = await self.cli.get(path)
            self.assertEqual(resp.status, 401, path)

    async def test_guide_index_and_search(self):
        resp = await self.cli.get("/api/strategy/guide", headers=self._h("alice"))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["total"], 58)
        self.assertEqual(body["count"], 58)
        self.assertEqual(len(body["categories"]), 20)
        self.assertEqual(body["categories"], list(rs._STRATEGY_GUIDE_CATS))
        ids = {e["id"] for e in body["items"]}
        self.assertIn("enter", ids)
        self.assertIn("fair-play", ids)
        for entry in body["items"]:
            self.assertEqual(set(entry), {"id", "title", "cat", "keywords"})
            self.assertTrue(entry["keywords"])

        hit = await (await self.cli.get(
            "/api/strategy/guide?q=民心", headers=self._h("alice"))).json()
        self.assertGreaterEqual(hit["count"], 1)
        hit_ids = {e["id"] for e in hit["items"]}
        self.assertIn("morale", hit_ids)
        # 空格多关键词为 AND 语义
        and_hit = await (await self.cli.get(
            "/api/strategy/guide?q=民心 赈济", headers=self._h("alice"))).json()
        self.assertIn("morale", {e["id"] for e in and_hit["items"]})
        self.assertLessEqual(and_hit["count"], hit["count"])

    async def test_guide_empty_result(self):
        body = await (await self.cli.get(
            "/api/strategy/guide?q=不存在的关键词xyz", headers=self._h("alice"))).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["items"], [])
        self.assertEqual(body["sections"], [])
        self.assertEqual(body["total"], 58)


class StrategyNotifyTest(_AccountsBase, _ManagerBase):
    """通知链路：_notify 档位过滤 + notify_strategy WS 推送。"""

    async def asyncSetUp(self):
        self._accounts_setup()
        await self._setup_manager(users=("web_alice",))
        ok, err = acc.register("alice", "secret1", "阿狸")
        self.assertTrue(ok, err)
        self.pushed = []

        async def _collect(uid, kind, payload):
            self.pushed.append((uid, kind, payload))

        self.m.set_notifier(_collect)
        await self._enter("web_alice", "阿狸")

    async def asyncTearDown(self):
        await self._teardown_manager()
        self._accounts_teardown()

    async def _set_level(self, level: int):
        await self.m.set_notify_pref("web_alice", level)
        self.pushed.clear()

    async def test_notify_level_filter(self):
        world = await self.m.get_world()
        # 默认档位 1：siege_alert(≥1) 送达，event(≥2) 被滤
        await self.m._notify(world, ["web_alice"], "siege_alert", {"node_id": 1})
        await self.m._notify(world, ["web_alice"], "event", {"name": "丰收"})
        self.assertEqual([k for _, k, _ in self.pushed], ["siege_alert"])

        # 档位 0：只留手操窗口类
        await self._set_level(0)
        await self.m._notify(world, ["web_alice"], "battle_open", {"node_id": 2})
        await self.m._notify(world, ["web_alice"], "duel_invite", {})
        await self.m._notify(world, ["web_alice"], "siege_alert", {})
        self.assertEqual([k for _, k, _ in self.pushed],
                         ["battle_open", "duel_invite"])

        # 档位 2：全部送达；未知 kind 按最严（≥2）处理
        await self._set_level(2)
        await self.m._notify(world, ["web_alice"], "event", {})
        await self.m._notify(world, ["web_alice"], "unknown_kind", {})
        self.assertEqual([k for _, k, _ in self.pushed],
                         ["event", "unknown_kind"])

        # 去重 + 空 uid 忽略；notifier 抛错不外泄
        await self._set_level(2)
        await self.m._notify(world, ["web_alice", "web_alice", ""], "event", {})
        self.assertEqual(len(self.pushed), 1)

    async def test_notify_without_notifier_is_silent(self):
        self.m.set_notifier(None)
        world = await self.m.get_world()
        await self.m._notify(world, ["web_alice"], "event", {})  # 不应抛错

    async def test_notify_strategy_pushes_ws(self):
        ws = FakeWS()
        rs._rooms[rs.ROOM_ID] = {
            ws: {"nick": "阿狸", "username": "alice", "avatar": "",
                 "room": rs.ROOM_ID}}
        n = await rs.notify_strategy("web_alice", "battle_open", {"node_id": 7})
        self.assertEqual(n, 1)
        msg = ws.sent[-1]
        self.assertEqual(msg["type"], "strategy.notify")
        self.assertEqual(msg["kind"], "battle_open")
        self.assertEqual(msg["payload"], {"node_id": 7})
        self.assertLessEqual(msg["ts"], _now())
        # 纯 QQ 玩家（无网页账号）返回 0
        self.assertEqual(await rs.notify_strategy("10001", "event", {}), 0)
        # 离线（无连接）返回 0
        rs._rooms.clear()
        self.assertEqual(
            await rs.notify_strategy("web_alice", "event", {}), 0)


class StrategyWsProtocolTest(_AccountsBase, unittest.IsolatedAsyncioTestCase):
    """§12.5 WS 上行手操指令 + strategy.delta / battle.event 下行。"""

    async def asyncSetUp(self):
        self._accounts_setup()
        ok, err = acc.register("alice", "secret1", "阿狸")
        self.assertTrue(ok, err)
        self.mgr = StubStrategyManager()
        self.plugin = FakePlugin(None, self.mgr)
        rs.set_plugin(self.plugin)
        rs._strategy_subs.clear()
        rs._strategy_last_tick = 0
        self.ws = FakeWS()
        rs._rooms[rs.ROOM_ID] = {
            self.ws: {"player_id": "web_alice", "player_name": "阿狸",
                      "username": "alice", "avatar": "", "room": rs.ROOM_ID}}

    async def asyncTearDown(self):
        rs.set_plugin(None)
        rs._rooms.clear()
        rs._strategy_subs.clear()
        rs._strategy_last_tick = 0
        self._accounts_teardown()

    async def _send(self, payload):
        await rs._handle_client_message(
            rs.ROOM_ID, self.ws, json.dumps(payload, ensure_ascii=False))

    def _last(self, typ=None):
        sent = [m for m in self.ws.sent if typ is None or m.get("type") == typ]
        self.assertTrue(sent, f"未收到 {typ}: {self.ws.sent}")
        return sent[-1]

    async def test_ws_deploy_maps_to_battle_deploy(self):
        # 真实 battle_deploy 只回 {ok,msg}（不推进回合），照此构造
        self.mgr.result = {"ok": True, "msg": "部署完成"}
        await self._send({"type": "strategy.deploy", "battle_id": 7,
                          "payload": {"placements": [{"uid": 1, "q": 0, "r": 0}]},
                          "req_id": "r-deploy"})
        self.assertEqual(len(self.mgr.calls), 1)
        uid, action, params = self.mgr.calls[0]
        self.assertEqual(uid, "web_alice")
        self.assertEqual(action, "battle_deploy")
        self.assertEqual(params["battle_id"], 7)
        self.assertEqual(params["placements"], [{"uid": 1, "q": 0, "r": 0}])
        ack = self._last("strategy.battle.ack")
        self.assertEqual(ack["req"], "r-deploy")
        self.assertEqual(ack["action"], "strategy.deploy")
        self.assertEqual(ack["battle_id"], 7)
        self.assertTrue(ack["result"]["ok"])
        # deploy 不推进回合 → 不推 battle.event
        self.assertFalse([m for m in self.ws.sent
                          if m.get("type") == "strategy.battle.event"])

    async def test_ws_move_maps_to_battle_action_and_emits_event(self):
        await self._send({"type": "strategy.move", "battle_id": 8,
                          "payload": {"path": [[0, 0], [1, 0]]},
                          "req_id": "r-move"})
        uid, action, params = self.mgr.calls[0]
        self.assertEqual(action, "battle_action")
        self.assertEqual(params["act"], "move")
        self.assertEqual(params["params"]["path"], [[0, 0], [1, 0]])
        ack = self._last("strategy.battle.ack")
        self.assertEqual(ack["act"], "move")
        ev = self._last("strategy.battle.event")
        self.assertEqual(ev["battle_id"], 8)
        self.assertEqual(ev["round"], 2)
        self.assertEqual(ev["log"], ["第 2 回合开始"])

    async def test_ws_all_kind_mapping(self):
        cases = {"skill": "skill", "retreat": "retreat",
                 "end_turn": "end_turn"}
        for kind, act in cases.items():
            self.mgr.calls.clear()
            await self._send({"action": f"strategy.{kind}", "battle_id": 9,
                              "payload": {}, "req_id": f"r-{kind}"})
            self.assertEqual(self.mgr.calls[0][1], "battle_action")
            self.assertEqual(self.mgr.calls[0][2]["act"], act)
            self.assertEqual(self._last("strategy.battle.ack")["req"], f"r-{kind}")
        # duel：默认发起，带 accept 则视为应战
        self.mgr.calls.clear()
        await self._send({"type": "strategy.duel", "battle_id": 9,
                          "payload": {"target": 3}, "req_id": "r-duel"})
        self.assertEqual(self.mgr.calls[0][2]["act"], "duel_challenge")
        await self._send({"type": "strategy.duel", "battle_id": 9,
                          "payload": {"accept": True}, "req_id": "r-duel2"})
        self.assertEqual(self.mgr.calls[1][2]["act"], "duel_respond")

    async def test_ws_subscribe_channel(self):
        await self._send({"type": "strategy.subscribe", "req_id": "r-sub"})
        msg = self._last("strategy.subscribed")
        self.assertIn("tick", msg)
        self.assertIn("alice", rs._strategy_subs)
        self.assertFalse(self.mgr.calls)

    async def test_ws_strategy_error(self):
        self.mgr.error = StrategyError("当前不是你的单位行动")
        await self._send({"type": "strategy.skill", "battle_id": 10,
                          "payload": {"skill": "fire"}, "req_id": "r-err"})
        e = self._last("strategy.error")
        self.assertEqual(e["req"], "r-err")
        self.assertEqual(e["msg"], "当前不是你的单位行动")
        self.assertFalse([m for m in self.ws.sent
                          if m.get("type") == "strategy.battle.ack"])

    async def test_ws_strategy_error_bad_battle_id(self):
        await self._send({"type": "strategy.move", "battle_id": "abc",
                          "payload": {}, "req_id": "r-bad"})
        self.assertEqual(self._last("strategy.error")["req"], "r-bad")
        self.assertFalse(self.mgr.calls)

    async def test_delta_and_battle_event_push(self):
        # 订阅后推 delta
        rs.subscribe_strategy("alice")
        self.assertEqual(await rs.notify_strategy_delta(5), 1)
        d = self._last("strategy.delta")
        self.assertEqual(d["tick"], 5)
        self.assertLessEqual(d["ts"], _now())

        # tick 号不变不重复推；推进后才再推
        rs._strategy_last_tick = 0
        self.assertEqual(await rs._maybe_strategy_delta(5), 0)  # 基线
        self.assertEqual(await rs._maybe_strategy_delta(5), 0)  # 未变
        self.assertEqual(await rs._maybe_strategy_delta(6), 1)  # 推进

        # 定向 battle.event（参与者解析走 stub get_battle）
        n = await rs.notify_strategy_battle(
            9, {"round": 3, "events": ["斩将"], "current_actor": 2})
        self.assertEqual(n, 1)
        ev = self._last("strategy.battle.event")
        self.assertEqual(ev["battle_id"], 9)
        self.assertEqual(ev["round"], 3)
        self.assertEqual(ev["log"], ["斩将"])
        self.assertFalse(ev["finished"])

        # 未订阅/无连接：静默返回 0
        rs._rooms.clear()
        self.assertEqual(await rs.notify_strategy_delta(7), 0)


class StrategyStaticRouteTest(unittest.IsolatedAsyncioTestCase):
    """静态资源：/static/strategy/guide.{js,css} 与 guide-data.js 必须可访问。"""

    async def test_guide_assets_served(self):
        app = web.Application()
        app.router.add_static("/static/", rs.STATIC_DIR, name="static")
        cli = TestClient(TestServer(app))
        await cli.start_server()
        try:
            for name in ("guide.js", "guide.css", "guide-data.js"):
                resp = await cli.get(f"/static/strategy/{name}")
                self.assertEqual(resp.status, 200, name)
                text = await resp.text()
                self.assertTrue(text.strip(), name)
        finally:
            await cli.close()


class StrategyServerAuthorityTest(_ManagerBase):
    """服务器权威：快照不泄露 seed / 伏兵坐标 / 敌军后勤数据。"""

    async def asyncSetUp(self):
        await self._setup_manager()
        await self._enter("web_alice", "甲")
        await self._enter("web_bob", "乙")

    async def asyncTearDown(self):
        await self._teardown_manager()

    async def _setup_manual_battle(self, troops: int = 1200):
        # 强制布局：任取一对相邻节点，home 归 alice、target 强制由 NPC 接管，
        # 避免世界随机生成（种子含启动秒数）导致 next() 抛 StopIteration 的 flake。
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        home, adj_id = next((n, x) for n in nodes.values() for x in n["adj"])
        await self._own("web_alice", home)
        npc = await self.m._fetchone(
            "SELECT id FROM strategy_factions"
            " WHERE world_id=? AND kind='npc' LIMIT 1", (self.wid,))
        target = nodes[adj_id]
        target["owner_faction"] = int(npc["id"])
        target["npc_garrison"] = {"troops": 600, "morale": 70}
        await self.m.save_node(target)
        army = await self._mk_army("web_alice", home["id"], troops)
        world = await self.m.get_world()
        atk_sim = await self.m._army_sim(army)
        gar_sim = self.m._garrison_sim(target)
        battle = await self.m._create_manual_battle(
            world, target, [atk_sim], [gar_sim] if gar_sim else [],
            [army], [], "field", "field", 300, 0, "web_alice")
        return world, target, army, battle

    async def test_battle_state_hides_seed_and_ambush(self):
        world, target, army, battle = await self._setup_manual_battle()
        bid = battle["id"]
        st = await self.m.battle_state("web_alice", bid)
        blob = json.dumps(st, ensure_ascii=False, default=str)
        self.assertNotIn("map_seed", blob)
        self.assertNotIn('"seed"', blob)
        # 守军（army_id<0）不属于我方
        garrison = [u for u in st["units"] if u["army_id"] < 0]
        self.assertTrue(garrison)
        self.assertTrue(all(not u["mine"] for u in garrison))
        # 注入一条未揭晓伏兵（挂在守军上）→ 对敌方视角整支不下发：
        # 连「存在一支伏兵」都不该泄露（否则可推断林中有埋伏）
        b = await self.m.get_battle(bid)
        sess = b["session"]
        sess["ambushes"].append(
            {"uid": garrison[0]["uid"], "q": 1, "r": 1, "revealed": False})
        await self.m.update_battle_session(b)
        st2 = await self.m.battle_state("web_alice", bid)
        self.assertFalse([u for u in st2["units"]
                          if u["uid"] == garrison[0]["uid"]])
        # 己方伏兵正常下发（自己是知情方）
        own_uid = next(u["uid"] for u in st2["units"] if u["mine"])
        sess["ambushes"].append(
            {"uid": own_uid, "q": 0, "r": 0, "revealed": False})
        await self.m.update_battle_session(b)
        st3 = await self.m.battle_state("web_alice", bid)
        self.assertIn(own_uid, [u["uid"] for u in st3["units"]])

    async def test_battle_state_masks_current_hidden_ambush(self):
        """轮到敌方伏兵行动时也不下发该单位，并遮蔽 current_actor。"""
        world, target, army, battle = await self._setup_manual_battle()
        bid = battle["id"]
        b = await self.m.get_battle(bid)
        sess = b["session"]
        g_uid = next(int(u["uid"]) for u in sess["units"]
                     if int(u.get("army_id", 0) or 0) < 0)
        sess["phase"] = "battle"
        sess["order"] = [g_uid]
        sess["acted"] = []
        sess["ambushes"].append(
            {"uid": g_uid, "q": 0, "r": 0, "revealed": False})
        await self.m.update_battle_session(b)
        st = await self.m.battle_state("web_alice", bid)
        self.assertFalse([u for u in st["units"] if u["uid"] == g_uid])
        self.assertIsNone(st["current_actor"])

    async def test_battle_state_exposes_sword_zoc_break_once(self):
        world, target, army, battle = await self._setup_manual_battle()
        bid = battle["id"]
        b = await self.m.get_battle(bid)
        sess = b["session"]
        mine = next(u for u in sess["units"] if u["side"] == "a")
        mine.setdefault("leader", {})["profession"] = "sword"
        mine["zoc_break_used"] = False
        await self.m.update_battle_session(b)
        st = await self.m.battle_state("web_alice", bid)
        row = next(u for u in st["units"] if u["uid"] == mine["uid"])
        self.assertTrue(row["can_zoc_break"])
        # 突破按钮只下发给本方：敌方不应拿到可用标记
        self.assertFalse(any(u["can_zoc_break"] for u in st["units"]
                             if not u["mine"]))
        mine["zoc_break_used"] = True
        await self.m.update_battle_session(b)
        st2 = await self.m.battle_state("web_alice", bid)
        row2 = next(u for u in st2["units"] if u["uid"] == mine["uid"])
        self.assertFalse(row2["can_zoc_break"])

    async def test_battle_state_labels_summon_as_beast(self):
        world, target, army, battle = await self._setup_manual_battle()
        bid = battle["id"]
        b = await self.m.get_battle(bid)
        sess = b["session"]
        summon = {
            "uid": 9001, "side": "a", "army_id": -9001, "leader": None,
            "faction_id": 0, "troop_type": "beast_horde", "troops": 500,
            "start_troops": 500, "morale": 100, "veteran": 0, "mana": 0,
            "pos": [0, 0], "moved": False, "acted": False, "routed": False,
            "defending": False, "formation": "", "siege_engine": False,
            "temp": True, "temp_expire": 3,
        }
        sess["units"].append(summon)
        await self.m.update_battle_session(b)
        st = await self.m.battle_state("web_alice", bid)
        row = next(u for u in st["units"] if u["uid"] == 9001)
        self.assertEqual(row["leader"], "兽群")
        self.assertTrue(row["temp"])
        self.assertFalse(row["can_zoc_break"])

    async def test_attack_target_parsed_before_taunt_check(self):
        """攻击指令须先解析 target 再做挑衅校验（否则非法 target → 500）。"""
        world, target, army, battle = await self._setup_manual_battle()
        bid = battle["id"]
        b = await self.m.get_battle(bid)
        sess = b["session"]
        mine = next(u for u in sess["units"] if u["side"] == "a")
        foe = next(u for u in sess["units"] if u["side"] == "b")
        sess["phase"] = "battle"
        b["state"] = "battle"
        sess["round"] = max(1, int(sess.get("round", 0) or 0))
        sess["order"] = [mine["uid"]]
        sess["acted"] = []
        sess["turn_deadline"] = _now() + 30
        sess["statuses"][str(mine["uid"])] = {
            "taunt_by": int(foe["uid"]), "taunt_ttl": 2,
            "taunt_by_from": 0,  # 挑衅自下一回合起生效
        }
        await self.m.update_battle_session(b)
        # 非数字 target：必须抛 StrategyError（REST 400），而不是 ValueError（500）
        with self.assertRaises(StrategyError) as ctx:
            await self.m.battle_action("web_alice", bid, "attack",
                                       {"target": "not-a-number"})
        self.assertIn("攻击目标无效", str(ctx.exception))
        # 挑衅生效时改打其他目标 → 明确拒绝（同样不得因解析顺序漏判）
        with self.assertRaises(StrategyError) as ctx2:
            await self.m.battle_action("web_alice", bid, "attack",
                                       {"target": int(foe["uid"]) + 1})
        self.assertIn("挑衅", str(ctx2.exception))

    async def test_state_fog_on_enemy_armies(self):
        # bob 占一个无主据点；alice 占其相邻据点并建军 →
        # bob 视角能看到该敌军（相邻即可见）但后勤数据（grain/veteran）为 None
        # 强制布局：任取一对相邻节点，一个归 bob、另一个归 alice（不再依赖
        # 随机布局中恰好存在「无主且邻居无主」的节点，消除 flake）
        nodes = {n["id"]: n for n in await self.m.get_nodes(self.wid)}
        home_b, adj_id = next((n, x) for n in nodes.values() for x in n["adj"])
        await self._own("web_bob", home_b)
        await self._own("web_alice", nodes[adj_id])
        await self._mk_army("web_alice", adj_id, 900)
        st = await self.m.get_state("web_bob")
        enemy = [a for a in st["armies"] if not a["mine"]]
        self.assertTrue(enemy)
        for a in enemy:
            self.assertIsNone(a["grain"])
            self.assertIsNone(a["veteran"])
        # 自己视角数据齐全
        st_a = await self.m.get_state("web_alice")
        mine = [a for a in st_a["armies"] if a["mine"]]
        self.assertTrue(mine)
        self.assertIsNotNone(mine[0]["grain"])


class StrategyGmTest(_ManagerBase):
    """GM 运维通道（§19）。"""

    async def asyncSetUp(self):
        await self._setup_manager(users=("web_alice",))
        await self._enter("web_alice", "甲")

    async def asyncTearDown(self):
        await self._teardown_manager()

    async def test_gm_tick(self):
        t0 = int((await self.m.get_world())["tick_no"])
        r = await self.m.gm("tick", ["3"])
        self.assertTrue(r["ok"])
        self.assertEqual(int((await self.m.get_world())["tick_no"]), t0 + 3)
        # 非法参数 → StrategyError；次数 clamp 到 1~24
        with self.assertRaises(StrategyError):
            await self.m.gm("tick", ["abc"])
        r = await self.m.gm("tick", ["999"])
        self.assertEqual(int((await self.m.get_world())["tick_no"]), t0 + 3 + 24)

    async def test_gm_open_world(self):
        old = await self.m.get_world()
        with self.assertRaises(StrategyError):
            await self.m.gm("开世界", ["不存在的模板"])
        r = await self.m.gm("open", [])
        self.assertTrue(r["ok"])
        new = await self.m.get_world()
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(int(new["season_no"]), int(old["season_no"]) + 1)
        # 旧世界已归档
        row = await self.m._fetchone(
            "SELECT state FROM strategy_worlds WHERE id=?", (old["id"],))
        self.assertEqual(row["state"], "ended")

    async def test_gm_inject_event(self):
        ev_id = ((self.m.cfg.get("events") or {}).get("events") or [{}])[0].get("id")
        self.assertTrue(ev_id)
        r = await self.m.gm("注入事件", [str(ev_id)])
        self.assertTrue(r["ok"])
        rows = await self.m._fetchall(
            "SELECT * FROM strategy_events WHERE world_id=?", (self.wid,))
        self.assertEqual(len(rows), 1)
        data = json.loads(rows[0]["data_json"])
        self.assertTrue(data.get("gm"))
        # 不存在的事件 / 不存在的据点 → StrategyError
        with self.assertRaises(StrategyError):
            await self.m.gm("event", ["no_such_event"])
        with self.assertRaises(StrategyError):
            await self.m.gm("event", [str(ev_id), "999999"])

    async def test_gm_balance_faction(self):
        p = await self.m.get_player("web_alice", self.wid)
        fid = int(p["faction_id"])
        f0 = await self.m.get_faction(fid)
        coin0 = int((f0.get("treasury") or {}).get("coin", 0))
        grain0 = int((f0.get("treasury") or {}).get("grain", 0))
        r = await self.m.gm("势力平衡", [str(fid), "1000", "500"])
        self.assertTrue(r["ok"])
        f1 = await self.m.get_faction(fid)
        self.assertEqual(int(f1["treasury"]["coin"]), coin0 + 1000)
        self.assertEqual(int(f1["treasury"]["grain"]), grain0 + 500)
        # 负调整不会低于 0
        await self.m.gm("balance", [str(fid), "-9999999", "0"])
        f2 = await self.m.get_faction(fid)
        self.assertEqual(int(f2["treasury"]["coin"]), 0)
        # 参数不足 / 势力不存在 / 非整数 → StrategyError
        with self.assertRaises(StrategyError):
            await self.m.gm("balance", [str(fid)])
        with self.assertRaises(StrategyError):
            await self.m.gm("balance", ["999999", "1"])
        with self.assertRaises(StrategyError):
            await self.m.gm("balance", [str(fid), "x"])

    async def test_gm_reset_season(self):
        r = await self.m.gm("重置赛季", [])
        self.assertTrue(r["ok"])
        self.assertIn("赛季", r["msg"])
        new = await self.m.get_world()
        self.assertEqual(int(new["season_no"]), 2)

    async def test_gm_unknown_op(self):
        with self.assertRaises(StrategyError):
            await self.m.gm("跳舞", [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
