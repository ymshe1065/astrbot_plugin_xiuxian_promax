# -*- coding: utf-8 -*-
"""webroom 单向 QQ 绑定回归：存档指针、唯一约束、握手换档。

用临时账号库 + 临时游戏库，不触碰真实数据。
"""
import asyncio
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from webchat_room import accounts as acc
    from webchat_room import room_server as rs
except Exception as exc:  # 没装 aiohttp / astrbot 时跳过，不拖累其他用例
    raise unittest.SkipTest(f"webroom 依赖不可用: {exc}")


def _make_game_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE players (user_id TEXT PRIMARY KEY, level_index INTEGER, "
        "cultivation_type TEXT, experience INTEGER)"
    )
    con.executemany(
        "INSERT INTO players VALUES (?,?,?,?)",
        [("1610049038", 23, "御兽", 64878939), ("919554761", 20, "灵修", 12653397)],
    )
    con.commit()
    con.close()


class QqBindTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xiuxian_bind_"))
        self._orig = (
            acc.STORE_DIR,
            acc.DB_PATH,
            acc.SECRET_PATH,
            acc._conn,
            rs._xiuxian_db_path,
        )
        acc.STORE_DIR = self.tmp
        acc.DB_PATH = self.tmp / "accounts.db"
        acc.SECRET_PATH = self.tmp / "secret.key"
        acc._conn = None
        acc.init()

        self.game_db = self.tmp / "game.db"
        _make_game_db(self.game_db)
        rs._xiuxian_db_path = lambda: self.game_db
        rs._rooms.clear()

        app = web.Application()
        app.router.add_post("/api/register", rs._api_register)
        app.router.add_get("/api/bind/status", rs._api_bind_status)
        app.router.add_post("/api/bind", rs._api_bind)
        app.router.add_post("/api/bind/unbind", rs._api_bind_unbind)
        app.router.add_get("/ws", rs._ws_handler)
        self.cli = TestClient(TestServer(app))
        await self.cli.start_server()

        d = await (await self.cli.post(
            "/api/register",
            json={"username": "阿猫", "password": "123456", "nick": "阿猫"},
        )).json()
        self.token = d["token"]
        self.h = {"Authorization": "Bearer " + self.token}

    async def asyncTearDown(self):
        await self.cli.close()
        rs._rooms.clear()
        (
            acc.STORE_DIR,
            acc.DB_PATH,
            acc.SECRET_PATH,
            acc._conn,
            rs._xiuxian_db_path,
        ) = self._orig
        if acc._conn is not None:
            acc._conn.close()
            acc._conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _status(self, headers=None):
        return await (await self.cli.get("/api/bind/status", headers=headers or self.h)).json()

    async def test_default_uses_web_key(self):
        d = await self._status()
        self.assertTrue(d["ok"])
        self.assertEqual(d["qq"], "")
        self.assertEqual(d["player_key"], "web_阿猫")
        self.assertIsNone(d["save"])

    async def test_auth_required(self):
        r = await self.cli.get("/api/bind/status")
        self.assertEqual(r.status, 401)

    async def test_reject_bad_format(self):
        r = await self.cli.post("/api/bind", headers=self.h, json={"qq": "abc"})
        self.assertEqual(r.status, 400)
        self.assertIn("格式", (await r.json())["error"])

    async def test_reject_missing_save(self):
        r = await self.cli.post("/api/bind", headers=self.h, json={"qq": "99999999999"})
        self.assertEqual(r.status, 400)
        self.assertIn("没有找到", (await r.json())["error"])

    async def test_bind_and_unbind(self):
        d = await (await self.cli.post(
            "/api/bind", headers=self.h, json={"qq": "1610049038"}
        )).json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["player_key"], "1610049038")
        self.assertEqual(d["save"]["level_index"], 23)

        d = await self._status()
        self.assertEqual(d["qq"], "1610049038")
        self.assertEqual(d["save"]["cultivation_type"], "御兽")

        d = await (await self.cli.post("/api/bind/unbind", headers=self.h)).json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["player_key"], "web_阿猫")

        d = await self._status()
        self.assertEqual(d["qq"], "")
        self.assertIsNone(d["save"])

    async def test_qq_unique_per_account(self):
        await self.cli.post("/api/bind", headers=self.h, json={"qq": "1610049038"})
        t2 = (await (await self.cli.post(
            "/api/register", json={"username": "阿狗", "password": "123456"}
        )).json())["token"]
        r = await self.cli.post(
            "/api/bind",
            headers={"Authorization": "Bearer " + t2},
            json={"qq": "1610049038"},
        )
        self.assertEqual(r.status, 400)
        self.assertIn("已被", (await r.json())["error"])

    async def test_handshake_switches_key_live(self):
        def key_of(username):
            for info in (rs._rooms.get(rs.ROOM_ID) or {}).values():
                if info.get("username") == username:
                    return info["player_id"]
            return None

        ws = await self.cli.ws_connect("/ws?token=" + self.token)
        hello = None
        for _ in range(8):
            m = await ws.receive_json()
            if m.get("type") == "hello":
                hello = m
                break
        try:
            self.assertIsNotNone(hello)
            self.assertEqual(hello["username"], "阿猫")
            self.assertEqual(key_of("阿猫"), "web_阿猫")

            await self.cli.post("/api/bind", headers=self.h, json={"qq": "1610049038"})
            self.assertEqual(key_of("阿猫"), "1610049038")

            await self.cli.post("/api/bind/unbind", headers=self.h)
            self.assertEqual(key_of("阿猫"), "web_阿猫")
        finally:
            await ws.close()


if __name__ == "__main__":
    unittest.main()
