# tests/test_strategy_tech_alliance.py
# v4.5.7 回归：联盟内科技研究必须使用「本势力」名册与玩家钱包
#   - 盟主调整联盟共同名册（把成员国主降为白身）后，成员国主仍可研究自家科技
#   - 研究费用只扣发起研究的玩家钱包，绝不扣盟主钱包
#   - 主脉与支脉（node_id）两套入口同口径；旧国库存取接口已移除
import os
import sys
import tempfile
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


class TestStrategyTechAlliance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        sm_mod = _imp("managers.strategy_manager")
        self.StrategyManager = sm_mod.StrategyManager
        self.StrategyError = sm_mod.StrategyError

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
        self.member = await self.m.get_faction(int(self.member["id"]))

    async def _set_wallet(self, fid: int, coin: int, iron: int):
        f = await self.m.get_faction(int(fid))
        lp = await self.m._leader_player(self.wid, f)
        self.assertIsNotNone(lp)
        lp["coin"] = int(coin)
        lp["iron"] = int(iron)
        await self.m.save_player(lp)
        await self.m._commit()

    async def _wallet(self, fid: int) -> dict:
        f = await self.m.get_faction(int(fid))
        lp = await self.m._leader_player(self.wid, f)
        return dict(lp or {})

    async def test_member_research_after_alliance_roster_demotion(self):
        """联盟名册被降权后，成员国主仍用本势力国库研究主脉。"""
        await self._ally()
        # 盟主把成员国主在「联盟共同名册」里降为白身
        await self.m.faction_set_position("u1", "u2", "")
        host_members = {m["user_id"]: m.get("position", "")
                        for m in await self.m.get_members(int(self.host["id"]))}
        self.assertEqual(host_members.get("u2"), "")
        # 成员国主在自己势力内的官职不受影响
        own_members = {m["user_id"]: m.get("position", "")
                       for m in await self.m.get_members(int(self.member["id"]))}
        self.assertEqual(own_members.get("u2"), "leader")

        await self._set_wallet(int(self.host["id"]), 90_000, 90_000)
        await self._set_wallet(int(self.member["id"]), 1000, 1000)
        r = await self.m.tech_research("u2", "military")
        self.assertTrue(r["ok"])
        self.assertIn("个人铜钱", r["msg"])

        member_w = await self._wallet(int(self.member["id"]))
        host_w = await self._wallet(int(self.host["id"]))
        self.assertEqual(int(member_w.get("coin", 0)), 500)
        self.assertEqual(int(member_w.get("iron", 0)), 500)
        self.assertEqual(int(host_w.get("coin", 0)), 90_000)
        self.assertEqual(int(host_w.get("iron", 0)), 90_000)

        # 前端科技卡按 own_position 判断可操作性；势力视图不再下发国库
        state = await self.m.get_state("u2")
        view = (state.get("player") or {}).get("faction") or {}
        self.assertEqual(view.get("own_position"), "leader")
        self.assertNotIn("treasury", view)
        self.assertEqual(int((state.get("player") or {}).get("coin", 0)), 500)

    async def test_treasury_actions_removed(self):
        """v4.5.7：旧国库存取接口已彻底移除，玩家钱包即唯一账户。"""
        self.assertFalse(hasattr(self.m, "treasury_deposit"))
        self.assertFalse(hasattr(self.m, "treasury_withdraw"))

    async def test_member_branch_research_uses_own_wallet(self):
        """支脉（node_id）研究同样只扣发起研究的玩家钱包。"""
        await self._ally()
        await self.m.faction_set_position("u1", "u2", "")
        member = await self.m.get_faction(int(self.member["id"]))
        member["tech"] = {"military": 1}
        await self.m.save_faction(member)
        await self._set_wallet(int(self.host["id"]), 90_000, 90_000)
        await self._set_wallet(int(self.member["id"]), 1000, 1000)

        r = await self.m.tech_research("u2", "military",
                                       node_id="mil_b_plain")
        self.assertTrue(r["ok"])
        self.assertIn("平原战法", r["msg"])
        member_w = await self._wallet(int(self.member["id"]))
        host_w = await self._wallet(int(self.host["id"]))
        # 支脉 L1：铜钱 400 / 精铁 300
        self.assertEqual(int(member_w.get("coin", 0)), 600)
        self.assertEqual(int(member_w.get("iron", 0)), 700)
        self.assertEqual(int(host_w.get("coin", 0)), 90_000)
        self.assertEqual(int(host_w.get("iron", 0)), 90_000)
        pending = (await self.m.get_faction(
            int(self.member["id"])) or {}).get("tech", {}).get("pending") or {}
        self.assertEqual(pending.get("node_id"), "mil_b_plain")
        self.assertEqual(pending.get("kind"), "branch")

    async def test_governance_rank_does_not_grant_own_treasury_access(self):
        """联盟名册里的官职不能越过本势力名册去研究。"""
        await self._ally()
        # 给 u2 一个联盟共同治理官职（situ），但本势力名册降为白身
        await self.m.faction_set_position("u1", "u2", "situ")
        await self.m._execute(
            "UPDATE strategy_members SET position='' "
            "WHERE faction_id=? AND user_id=?", (int(self.member["id"]), "u2"))
        await self.m._commit()
        await self._set_wallet(int(self.member["id"]), 1000, 1000)
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.tech_research("u2", "military")
        self.assertIn("仅", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
