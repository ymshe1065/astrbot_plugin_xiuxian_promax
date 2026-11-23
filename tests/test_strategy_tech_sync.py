# tests/test_strategy_tech_sync.py
# 「仙域争霸」势力科技资源 / 科技点 / 状态同步回归：
#   v4.5.7 起研究直接扣玩家钱包（势力无国库）、钱包不足时不扣任何资源、
#   科技点只抵扣实际可缩短时长、到期未结算的研究在入口自动结算、
#   旧存档势力国库按人数退回成员钱包。
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


def _now() -> int:
    return int(time.time())


class TestStrategyTechSync(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        sm_mod = _imp("managers.strategy_manager")
        self.StrategyManager = sm_mod.StrategyManager
        self.StrategyError = sm_mod.StrategyError
        self.R = _imp("core.strategy_rules")

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        await self.db.create_player(self.Player(
            user_id="u1", gold=10_000_000, user_name="测试u1"))

        self.m = self.StrategyManager(self.db)
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        nodes = await self.m.get_nodes(self.wid)
        free = [n for n in nodes
                if int(n.get("owner_faction", 0) or 0) == 0
                and n.get("type") != "ruin"]
        await self.m.enter_game("u1", "甲", node_id=int(free[0]["id"]))
        self.p = await self.m.get_player("u1", self.wid)
        self.fid = int(self.p["faction_id"])

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _set_resources(self, *, coin: int = 9_000, iron: int = 9_000,
                             treasury_coin: int = 0,
                             treasury_iron: int = 0,
                             points: int = 0):
        p = await self.m.get_player("u1", self.wid)
        p["coin"] = int(coin)
        p["iron"] = int(iron)
        await self.m.save_player(p)
        f = await self.m.get_faction(self.fid)
        f["treasury"] = {"coin": int(treasury_coin), "iron": int(treasury_iron)}
        f["tech"] = {"points": int(points)}
        await self.m.save_faction(f)
        await self.m._commit()

    async def _idle_char(self):
        chars = await self.m.get_chars(
            faction_id=self.fid, world_id=self.wid, status="idle")
        self.assertTrue(chars)
        return chars[0]

    async def test_research_spends_player_wallet(self):
        await self._set_resources(coin=9_000, iron=9_000)
        c = await self._idle_char()
        res = await self.m.tech_research("u1", "military", char_id=int(c["id"]))
        self.assertTrue(res["ok"])
        self.assertIn("个人铜钱", res["msg"])
        p = await self.m.get_player("u1", self.wid)
        f = await self.m.get_faction(self.fid)
        # L1 成本 500/500，直接扣操作者钱包；势力不再有国库
        self.assertEqual(int(p["coin"]), 8_500)
        self.assertEqual(int(p["iron"]), 8_500)
        self.assertEqual(int((f.get("treasury") or {}).get("coin", 0)), 0)
        self.assertEqual(int((f.get("treasury") or {}).get("iron", 0)), 0)
        self.assertTrue((f.get("tech") or {}).get("pending"))
        # 动作后的快照必须立即反映钱包扣款与研究状态，不能等下一 tick
        state = await self.m.get_state("u1")
        view = (state.get("player") or {}).get("faction") or {}
        self.assertNotIn("treasury", view)
        self.assertEqual(int((state.get("player") or {}).get("coin")), 8_500)
        self.assertEqual(int((state.get("player") or {}).get("iron")), 8_500)
        self.assertTrue((view.get("tech") or {}).get("pending"))

    async def test_research_rejects_when_wallet_short(self):
        await self._set_resources(coin=100, iron=500)
        c = await self._idle_char()
        with self.assertRaises(self.StrategyError) as ctx:
            await self.m.tech_research("u1", "military", char_id=int(c["id"]))
        self.assertIn("铜钱/精铁不足", str(ctx.exception))
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p["coin"]), 100)
        self.assertEqual(int(p["iron"]), 500)
        c = await self.m.get_char(int(c["id"]))
        self.assertEqual(c["status"], "idle")

    async def test_legacy_faction_treasury_refunded_to_members(self):
        p = await self.m.get_player("u1", self.wid)
        p["coin"], p["grain"], p["iron"] = 0, 0, 0
        await self.m.save_player(p)
        f = await self.m.get_faction(self.fid)
        f["treasury"] = {"coin": 1_000, "grain": 500, "iron": 200}
        await self.m.save_faction(f)
        await self.m._commit()

        world = await self.m.get_world()
        settled = await self.m._settle_legacy_player_treasuries(world)
        self.assertEqual(settled, 1)
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p["coin"]), 1_000)
        self.assertEqual(int(p["grain"]), 500)
        self.assertEqual(int(p["iron"]), 200)
        f = await self.m.get_faction(self.fid)
        self.assertEqual(f.get("treasury") or {}, {})
        world = await self.m.get_world()
        self.assertEqual(
            int((world.get("data") or {}).get("player_treasury_removed", 0)), 456)
        # 幂等：再次迁移不会重复发放
        await self.m._settle_legacy_player_treasuries(world)
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p["coin"]), 1_000)

    async def test_tech_points_only_consume_usable_minutes(self):
        await self._set_resources(coin=9_000, iron=9_000, points=1_000)
        c = await self._idle_char()
        c["stats"] = dict(c.get("stats") or {})
        c["stats"]["wisdom"] = 90
        c["stats"]["admin"] = 90
        await self.m.save_char(c)
        await self.m._commit()

        await self.m.tech_research("u1", "military", char_id=int(c["id"]))
        f = await self.m.get_faction(self.fid)
        tech = f.get("tech") or {}
        hours = self.R.tech_research_hours(8, 90)
        expect_cut = min(1_000, int(8 * 60 * 0.5), int((hours - 0.5) * 60))
        self.assertEqual(int(tech["points"]), 1_000 - expect_cut)
        # 研究被压到 0.5h 下限后不应继续扣科技点
        self.assertLess(expect_cut, 240)
        left = int(tech["pending"]["finish_at"]) - _now()
        self.assertLessEqual(left, 1_800 + 5)
        self.assertGreater(left, 1_700)

    async def test_expired_research_settles_before_next_one(self):
        await self._set_resources(coin=9_000, iron=9_000)
        c = await self._idle_char()
        f = await self.m.get_faction(self.fid)
        f["tech"] = {
            "points": 0, "military": 0,
            "pending": {"line": "military", "by": "u1",
                        "finish_at": _now() - 1, "char_id": int(c["id"]),
                        "char_name": c["name"], "exp": 60},
        }
        await self.m.save_faction(f)
        c["status"] = "research"
        await self.m.save_char(c)
        await self.m._commit()

        await self.m.tech_research("u1", "agri", char_id=int(c["id"]))
        f = await self.m.get_faction(self.fid)
        tech = f.get("tech") or {}
        self.assertEqual(int(tech.get("military", 0)), 1)
        self.assertEqual((tech.get("pending") or {}).get("line"), "agri")

    async def test_research_rejects_dying_char(self):
        """濒死武将不能主研：显式指派与自动挑选都必须跳过。"""
        await self._set_resources(coin=9_000, iron=9_000)
        c = await self._idle_char()
        c["injury"] = 70
        await self.m.save_char(c)
        await self.m._commit()
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.tech_research("u1", "military", char_id=int(c["id"]))
        self.assertIn("伤势过重", str(cm.exception))
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p["coin"]), 9_000)
        self.assertEqual(int(p["iron"]), 9_000)
        c = await self.m.get_char(int(c["id"]))
        self.assertEqual(c["status"], "idle")
        f = await self.m.get_faction(self.fid)
        self.assertFalse((f.get("tech") or {}).get("pending"))
        # 自动挑选同样跳过濒死：把所有空闲角色置为濒死后无法开研究
        for x in await self.m.get_chars(
                faction_id=self.fid, world_id=self.wid, status="idle"):
            x["injury"] = 70
            await self.m.save_char(x)
        await self.m._commit()
        with self.assertRaises(self.StrategyError):
            await self.m.tech_research("u1", "military")
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p["coin"]), 9_000)
        self.assertEqual(int(p["iron"]), 9_000)

    async def test_expired_research_keeps_moved_char_new_research(self):
        """v4.5.7：角色转投他势力并开始新研究后，旧研究完成不得清其状态。"""
        c = await self._idle_char()
        f = await self.m.get_faction(self.fid)
        f["tech"] = {
            "points": 0, "military": 0,
            "pending": {"line": "military", "by": "u1",
                        "finish_at": _now() - 1, "char_id": int(c["id"]),
                        "char_name": c["name"], "exp": 60},
        }
        await self.m.save_faction(f)
        await self.m._commit()

        await self.db.create_player(self.Player(
            user_id="u2", gold=10_000_000, user_name="测试u2"))
        free = [n for n in await self.m.get_nodes(self.wid)
                if int(n.get("owner_faction", 0) or 0) == 0
                and n.get("type") != "ruin"]
        self.assertTrue(free)
        await self.m.enter_game("u2", "乙", node_id=int(free[0]["id"]))
        p2 = await self.m.get_player("u2", self.wid)
        c["faction_id"] = int(p2["faction_id"])
        c["owner_user_id"] = "u2"
        c["status"] = "research"
        await self.m.save_char(c)
        await self.m._commit()

        await self.m._step_tech(self.world)
        c = await self.m.get_char(int(c["id"]))
        self.assertEqual(c["status"], "research")
        f = await self.m.get_faction(self.fid)
        self.assertEqual(int((f.get("tech") or {}).get("military", 0)), 1)
        self.assertNotIn("pending", f.get("tech") or {})


if __name__ == "__main__":
    unittest.main()
