# -*- coding: utf-8 -*-
"""「仙域争霸」江湖纪事（v4.7.0 / DB v87）回归。

覆盖：可见性（public / 本方 / 被针对）、延迟公开、分页、保留策略、
据点易主落日志、快照首屏与 meta 下发。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub  # noqa: E402

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class TestWorldLog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        self.StrategyManager = _imp("managers.strategy_manager").StrategyManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)
        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        for uid in ("u1", "u2"):
            await self.db.create_player(self.Player(
                user_id=uid, gold=10_000_000, user_name=f"测试{uid}"))
        self.m = self.StrategyManager(self.db)
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = int(self.world["id"])
        self.p1 = await self._enter("u1", "甲")
        self.p2 = await self._enter("u2", "乙")
        self.f1 = await self.m.get_faction(int(self.p1["faction_id"]))
        self.f2 = await self.m.get_faction(int(self.p2["faction_id"]))

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _enter(self, user_id: str, nickname: str) -> dict:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        nodes.sort(key=lambda n: (n["type"] not in ("city", "pass", "branch"),
                                  -int(n.get("level", 1)), int(n["id"])))
        return await self.m.enter_game(
            user_id, nickname, node_id=int(nodes[0]["id"]))

    async def _insert(self, summary: str, **kw):
        return await self.m._log_action(
            self.world, kw.pop("kind", "plot"), kw.pop("action", "test"),
            summary=summary, **kw)

    async def test_visibility_and_delay(self):
        f1, f2 = int(self.f1["id"]), int(self.f2["id"])
        await self._insert("公开记录", visibility="public")
        await self._insert("本方记录", visibility="faction", actor_faction=f1)
        await self._insert("被针对记录", visibility="faction", target_faction=f1)
        await self._insert("别人记录", visibility="faction", actor_faction=f2)
        await self._insert("延迟公开", visibility="public",
                           publish_at=int(time.time()) + 3600)
        page = await self.m.get_world_logs(self.wid, {f1}, limit=20)
        texts = [x["summary"] for x in page["items"]]
        self.assertIn("公开记录", texts)
        self.assertIn("本方记录", texts)
        self.assertIn("被针对记录", texts)
        self.assertNotIn("别人记录", texts)
        self.assertNotIn("延迟公开", texts)
        # 未入场/无势力：只看 public
        page2 = await self.m.get_world_logs(self.wid, set(), limit=20)
        texts2 = [x["summary"] for x in page2["items"]]
        self.assertEqual(texts2, ["公开记录"])
        # 条目标签由服务端补全
        self.assertEqual(page["items"][0]["kind_name"], "计略")

    async def test_pagination(self):
        for i in range(25):
            await self._insert(f"公开#{i}", visibility="public")
        page1 = await self.m.get_world_logs(self.wid, set(), limit=10)
        self.assertEqual(len(page1["items"]), 10)
        self.assertTrue(page1["has_more"])
        before = int(page1["items"][-1]["id"])
        page2 = await self.m.get_world_logs(
            self.wid, set(), before_id=before, limit=10)
        self.assertEqual(len(page2["items"]), 10)
        self.assertTrue(page2["has_more"])
        page3 = await self.m.get_world_logs(
            self.wid, set(),
            before_id=int(page2["items"][-1]["id"]), limit=10)
        self.assertEqual(len(page3["items"]), 5)
        self.assertFalse(page3["has_more"])
        ids1 = {x["id"] for x in page1["items"]}
        ids2 = {x["id"] for x in page2["items"]}
        ids3 = {x["id"] for x in page3["items"]}
        self.assertFalse(ids1 & ids2 or ids2 & ids3 or ids1 & ids3)

    async def test_retention_by_days(self):
        cfg = self.m.main_cfg().setdefault("world_log", {})
        cfg["keep_days"] = 1
        cfg["keep_rows"] = 0
        old_id = await self._insert("旧记录", visibility="public")
        await self.m._execute(
            "UPDATE strategy_world_log SET created_at=? WHERE id=?",
            (int(time.time()) - 3 * 86400, old_id))
        await self._insert("新记录", visibility="public")
        removed = await self.m._step_world_log_cleanup(self.world)
        self.assertGreaterEqual(int(removed), 1)
        page = await self.m.get_world_logs(self.wid, set(), limit=20)
        texts = [x["summary"] for x in page["items"]]
        self.assertNotIn("旧记录", texts)
        self.assertIn("新记录", texts)

    async def test_capture_writes_log(self):
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n["owner_faction"]) == 0 and n["type"] != "ruin")
        await self.m._capture_node(self.world, node, int(self.f1["id"]))
        await self.m._commit()
        page = await self.m.get_world_logs(self.wid, set(), kind="capture", limit=10)
        self.assertTrue(page["items"])
        self.assertIn("易主", page["items"][0]["summary"])
        self.assertEqual(page["items"][0]["kind"], "capture")

    async def test_backfill_existing_winds(self):
        """首次升级：现有最近风声回填为纪事（仅当日志为空时）。"""
        data = dict(self.world.get("data") or {})
        data["winds"] = [{
            "text": "旧风声：某势力宣战", "kind": "war", "tick": 3,
            "visible_at": int(time.time()) - 10,
        }]
        await self.m._execute(
            "UPDATE strategy_worlds SET data_json=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), self.wid))
        migration = _imp("data.migration")
        await migration._ensure_strategy_world_log_v87(self.conn)
        logs = await self.m.get_world_logs(self.wid, set(), limit=10)
        self.assertTrue(any("旧风声" in x["summary"] for x in logs["items"]))
        # 幂等：再次调用不会重复回填
        await migration._ensure_strategy_world_log_v87(self.conn)
        logs2 = await self.m.get_world_logs(self.wid, set(), limit=50)
        self.assertEqual(len(logs2["items"]), len(logs["items"]))

    async def test_state_includes_logs_and_kinds(self):
        await self._insert("快照记录", visibility="public")
        st = await self.m.get_state("u1")
        self.assertIn("logs", st)
        self.assertTrue(st["logs"])
        self.assertIn("world_log_kinds", st["meta"])
        self.assertIn("plot", st["meta"]["world_log_kinds"])
        self.assertIn("battle", st["meta"]["world_log_kinds"])
        self.assertTrue(st["meta"]["world_log_kinds"]["plot"]["icon"])


if __name__ == "__main__":
    unittest.main()
