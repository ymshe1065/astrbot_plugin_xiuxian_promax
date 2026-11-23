# tests/test_strategy_alliance_economy.py
# v4.5.0 联盟经济隔离回归：
#   - 联盟只共享外交 / 协战类官职效果（太尉攻、祭酒计略/托管智谋）
#   - 经济经营类官职（司徒产出、中郎将招募）只作用于角色所属势力
#   - 招募品质、据点产出不再上溯盟主或盟友
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


class TestStrategyAllianceEconomy(unittest.IsolatedAsyncioTestCase):
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
        for uid in ("u1", "u2"):
            await self.db.create_player(self.Player(
                user_id=uid, gold=10_000_000, user_name=f"测试{uid}"))

        self.m = self.StrategyManager(self.db)
        self.m.main_cfg().setdefault("hex", {})["field_encounter_mode"] = "auto"
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        for uid, nickname in (("u1", "甲"), ("u2", "乙")):
            await self.m.enter_game(uid, nickname,
                                    node_id=await self._free_node())
        p1 = await self.m.get_player("u1", self.wid)
        p2 = await self.m.get_player("u2", self.wid)
        self.host = await self.m.get_faction(int(p1["faction_id"]))
        self.member = await self.m.get_faction(int(p2["faction_id"]))

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _free_node(self) -> int:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        nodes.sort(key=lambda n: (n["type"] not in ("city", "pass", "branch"),
                                  -int(n.get("level", 1)), int(n["id"])))
        self.assertTrue(nodes, "没有可选起始据点")
        return int(nodes[0]["id"])

    async def _ally(self):
        await self.m.faction_apply("u2", int(self.host["id"]))
        await self.m.faction_approve("u1", "u2")
        self.host = await self.m.get_faction(int(self.host["id"]))
        self.member = await self.m.get_faction(int(self.member["id"]))

    async def _own_extra_node(self, fid: int) -> int:
        nid = await self._free_node()
        node = await self.m.get_node(self.wid, nid)
        node["owner_faction"] = int(fid)
        await self.m.save_node(node)
        await self.m._commit()
        return nid

    async def _chars(self, uid: str) -> list[dict]:
        return [c for c in await self.m.get_chars(
            owner_user_id=uid, world_id=self.wid)
            if c.get("status") not in ("dead", "captive", "transfer")]

    async def _add_char(self, uid: str, fid: int) -> dict:
        import random
        c = self.R.generate_character(
            random.Random(f"{uid}-{fid}"), rarity="rare", role="general")
        c.update({"world_id": self.wid, "faction_id": int(fid),
                  "owner_user_id": uid, "loyalty": 90, "location": {}})
        await self.m.insert_char(c)
        await self.m._commit()
        return await self.m.get_char(int(c["id"]))

    async def test_economic_officer_bonus_stays_inside_faction(self):
        await self._ally()
        # 联盟总据点 3 座 → 解锁司徒；成员势力承担第 3 座
        await self._own_extra_node(int(self.member["id"]))
        chars = await self._chars("u2")
        if len(chars) < 3:
            await self._add_char("u2", int(self.member["id"]))
            chars = await self._chars("u2")
        self.assertGreaterEqual(len(chars), 3)
        # 成员势力：司徒（经济）+ 中郎将（经济）+ 祭酒（协战）
        await self.m.faction_appoint_char(
            "u1", int(chars[0]["id"]), "situ")
        await self.m.faction_appoint_char(
            "u1", int(chars[1]["id"]), "zhonglang")
        await self.m.faction_appoint_char(
            "u1", int(chars[2]["id"]), "jijiu")

        # 本势力口径：成员拿到自己的司徒/中郎将/祭酒加成
        own = await self.m._faction_position_bonus(
            self.member, include_alliance=False)
        self.assertAlmostEqual(float(own["node_output"]), 0.05)
        self.assertAlmostEqual(float(own["recruit_quality"]), 0.10)
        self.assertAlmostEqual(float(own["plot_bonus"]), 0.10)
        self.assertEqual(int(own["ai_wisdom"]), 5)

        # 盟主经济口径：不得吃到成员的司徒/中郎将
        host_eco = await self.m._faction_position_bonus(
            self.host, include_alliance=False)
        self.assertAlmostEqual(float(host_eco["node_output"]), 0.0)
        self.assertAlmostEqual(float(host_eco["recruit_quality"]), 0.0)

        # 默认（协战）口径：只有祭酒类协战效果跨势力，经济键强制归零
        host_all = await self.m._faction_position_bonus(self.host)
        self.assertAlmostEqual(float(host_all["node_output"]), 0.0)
        self.assertAlmostEqual(float(host_all["recruit_quality"]), 0.0)
        self.assertAlmostEqual(float(host_all["plot_bonus"]), 0.10)
        self.assertEqual(int(host_all["ai_wisdom"]), 5)

    async def test_gm_legacy_alliance_treasury_dry_run_then_split(self):
        """GM 联盟国库清算：默认只探测，带「执行」才人均平分。"""
        await self._ally()
        host = await self.m.get_faction(int(self.host["id"]))
        host["treasury"] = {"coin": 1000, "grain": 800, "iron": 600,
                            "gold": 1000, "reserved_coin": 77}
        await self.m.save_faction(host)
        await self.m._commit()
        # 探测：不写库
        res = await self.m.gm("联盟国库清算", [])
        self.assertIn("探测", res["msg"])
        host = await self.m.get_faction(int(self.host["id"]))
        self.assertEqual(int(host["treasury"]["coin"]), 1000)
        self.assertEqual(int(host["treasury"]["gold"]), 1000)
        before = {}
        for uid in ("u1", "u2"):
            p = await self.m.get_player(uid, self.wid)
            main = await self.db.get_player_by_id(uid)
            before[uid] = (int(p["coin"]), int(p["grain"]), int(p["iron"]),
                           int(main.gold))
        # 执行：按 2 人人均，reserved_coin 不参与平分
        res = await self.m.gm("联盟国库清算", ["执行"])
        self.assertIn("已执行", res["msg"])
        host = await self.m.get_faction(int(self.host["id"]))
        self.assertEqual(int(host["treasury"].get("coin", 0)), 0)
        self.assertEqual(int(host["treasury"].get("grain", 0)), 0)
        self.assertEqual(int(host["treasury"].get("iron", 0)), 0)
        self.assertEqual(int(host["treasury"].get("gold", 0)), 0)
        self.assertEqual(int(host["treasury"].get("reserved_coin", 0)), 77)
        for uid in ("u1", "u2"):
            p = await self.m.get_player(uid, self.wid)
            main = await self.db.get_player_by_id(uid)
            bc, bg, bi, bgold = before[uid]
            self.assertEqual(int(p["coin"]), bc + 500)
            self.assertEqual(int(p["grain"]), bg + 400)
            self.assertEqual(int(p["iron"]), bi + 300)
            self.assertEqual(int(main.gold), bgold + 500)
        # 重复执行默认跳过
        res = await self.m.gm("联盟国库清算", ["执行"])
        self.assertIn("已清算过", res["msg"])

    async def test_recruit_quality_does_not_use_alliance_officers(self):
        await self._ally()
        chars = await self._chars("u2")
        await self.m.faction_appoint_char(
            "u1", int(chars[0]["id"]), "zhonglang")
        # 盟主没有中郎将：招募品质不受成员中郎将影响
        self.assertAlmostEqual(
            await self.m._recruit_quality_bonus(int(self.host["id"])), 0.0)
        self.assertGreaterEqual(
            await self.m._recruit_quality_bonus(int(self.member["id"])),
            0.10)

    async def test_faction_ext_uses_own_capital_and_rebuild_fund(self):
        """v4.5.5：联盟任务/动员令共享，但重建合力/主城/迁都 CD 归本势力。"""
        await self._ally()
        host = await self.m.get_faction(int(self.host["id"]))
        member = await self.m.get_faction(int(self.member["id"]))
        now = int(time.time())
        host_last = now - 1000
        member_last = now - 50
        await self.m._execute(
            "UPDATE strategy_factions SET rebuild_fund=?, last_capital_move_at=? "
            "WHERE id=?", (111, host_last, int(host["id"])))
        await self.m._execute(
            "UPDATE strategy_factions SET rebuild_fund=?, last_capital_move_at=? "
            "WHERE id=?", (222, member_last, int(member["id"])))
        await self.m._commit()

        state = await self.m.get_state("u2")
        ext = state.get("faction_ext") or {}
        self.assertEqual(int(ext.get("own_faction_id")), int(member["id"]))
        self.assertEqual(int(ext.get("rebuild_fund")), 222)
        self.assertEqual(int((ext.get("capital") or {}).get("node_id")),
                         int(member["capital_node_id"]))
        cd_days = int((self.m.main_cfg().get("faction") or {}).get(
            "capital_relocate_cd_days", 7) or 0)
        self.assertEqual(int(ext.get("relocate_cd_until")),
                         member_last + cd_days * 86400)


if __name__ == "__main__":
    unittest.main()
