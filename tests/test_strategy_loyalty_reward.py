# tests/test_strategy_loyalty_reward.py
# 「仙域争霸」v4.5.8 忠诚正反馈回归：
#   每 tick 自然恢复（无官职也有）/ 官职加成与更高上限 /
#   低忠诚不自动回血 / 俘虏与 NPC 不参与 /
#   单个奖赏扣铜钱加忠诚 / 一键奖赏按忠诚从低到高部分结算 / 日限与余额
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


class TestStrategyLoyaltyReward(unittest.IsolatedAsyncioTestCase):
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
        self.m.main_cfg()["loyalty"] = {
            "natural_gain_per_tick": 1,
            "natural_cap": 80,
            "natural_min_loyalty": 30,
            "appointed_bonus_per_tick": 1,
            "appointed_natural_cap": 90,
            "reward_gain": 5,
            "reward_cost_coin": 100,
            "reward_daily_limit": 20,
            "reward_cap": 100,
        }
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        self.p1 = await self._enter("u1", "甲")
        self.p2 = await self._enter("u2", "乙")
        await self._give("u1", coin=10_000)

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    async def _give(self, uid: str, **res):
        p = await self.m.get_player(uid, self.wid)
        for k, v in res.items():
            p[k] = int(v)
        await self.m.save_player(p)
        await self.m._commit()
        return p

    async def _free_node(self) -> int:
        nodes = [n for n in await self.m.get_nodes(self.wid)
                 if int(n["owner_faction"]) == 0 and n["type"] != "ruin"]
        nodes.sort(key=lambda n: (n["type"] not in ("city", "pass", "branch"),
                                  -int(n.get("level", 1)), int(n["id"])))
        self.assertTrue(nodes, "没有可选起始据点")
        return int(nodes[0]["id"])

    async def _enter(self, user_id: str, nickname: str) -> dict:
        return await self.m.enter_game(user_id, nickname,
                                       node_id=await self._free_node())

    async def _new_char(self, uid: str, name: str, loyalty: int = 70,
                        status: str = "idle", faction_id: int = 0) -> dict:
        p = await self.m.get_player(uid, self.wid)
        c = {"world_id": self.wid,
             "faction_id": int(faction_id or p["faction_id"]),
             "owner_user_id": uid, "name": name, "role": "general",
             "rarity": "common", "profession": "sword",
             "stats": {"leadership": 60, "valor": 60, "wisdom": 60,
                       "charm": 60, "politics": 60, "luck": 60},
             "loyalty": loyalty, "status": status, "location": {}}
        await self.m.insert_char(c)
        await self.m._commit()
        return await self.m.get_char(c["id"])

    async def _set_char(self, cid: int, **kv):
        c = await self.m.get_char(int(cid))
        c.update(kv)
        await self.m.save_char(c)
        await self.m._commit()
        return await self.m.get_char(int(cid))

    async def _park_all(self, uid: str = "u1", loyalty: int = 100):
        """把已有角色（新手包）拉到上限，隔离一键奖赏的统计口径。"""
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if int(c.get("loyalty", 0) or 0) < loyalty:
                await self._set_char(c["id"], loyalty=loyalty)

    # ------------------------------------------------------------------
    # 自然恢复
    # ------------------------------------------------------------------
    async def test_natural_growth_without_appointment(self):
        """无官职角色也每 tick +1，达到 natural_cap 后停止。"""
        c = await self._new_char("u1", "平民武将", loyalty=40)
        await self.m._step_loyalty_recovery(self.world)
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 41)
        await self._set_char(c["id"], loyalty=79)
        await self.m._step_loyalty_recovery(self.world)
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 80)
        await self.m._step_loyalty_recovery(self.world)
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 80)

    async def test_natural_growth_appointed_bonus_and_cap(self):
        """有官职角色 +2/tick，上限 appointed_natural_cap（90）。"""
        c = await self._new_char("u1", "任官武将", loyalty=79)
        await self._set_char(c["id"], appointed_position="situ")
        await self.m._step_loyalty_recovery(self.world)
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 81)
        await self._set_char(c["id"], loyalty=89)
        await self.m._step_loyalty_recovery(self.world)
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 90)
        await self.m._step_loyalty_recovery(self.world)
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 90)

    async def test_defaults_when_loyalty_section_missing(self):
        """旧配置缺少 loyalty 段时仍启用自然恢复与奖赏默认值。"""
        self.m.main_cfg().pop("loyalty", None)
        c = await self._new_char("u1", "旧配置武将", loyalty=40)
        await self.m._step_loyalty_recovery(self.world)
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 41)
        lr = (await self.m.get_state("u1"))["player"]["loyalty_reward"]
        self.assertEqual(lr["natural_gain_per_tick"], 1)
        self.assertEqual(lr["cost_coin"], 100)
        self.assertEqual(lr["daily_limit"], 20)

    async def test_natural_growth_governor_counts_as_appointed(self):
        """太守（据点 governor_char_id）也算官职：+2/tick，上限 90。"""
        c = await self._new_char("u1", "太守武将", loyalty=88)
        node = next(n for n in await self.m.get_nodes(self.wid)
                    if int(n["owner_faction"]) == int(self.p1["faction_id"]))
        node["governor_char_id"] = int(c["id"])
        await self.m.save_node(node)
        await self.m._commit()
        await self.m._step_loyalty_recovery(self.world)
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 90)

    async def test_natural_growth_skips_low_captive_and_npc(self):
        """低于 30 不回血；俘虏与 NPC 势力角色不参与自然恢复。"""
        low = await self._new_char("u1", "离心边缘", loyalty=25)
        cap = await self._new_char("u1", "在押", loyalty=40,
                                   status="captive")
        row = await self.m._fetchone(
            "SELECT id FROM strategy_factions WHERE world_id=? AND kind='npc' "
            "AND alive=1 LIMIT 1", (self.wid,))
        npc = await self._new_char("", "NPC 武将", loyalty=40,
                                   faction_id=int(row["id"]))
        await self.m._step_loyalty_recovery(self.world)
        self.assertEqual(int((await self.m.get_char(low["id"]))["loyalty"]), 25)
        self.assertEqual(int((await self.m.get_char(cap["id"]))["loyalty"]), 40)
        self.assertEqual(int((await self.m.get_char(npc["id"]))["loyalty"]), 40)

    # ------------------------------------------------------------------
    # 单个奖赏
    # ------------------------------------------------------------------
    async def test_reward_char_deducts_coin_and_gains(self):
        c = await self._new_char("u1", "待奖赏", loyalty=40)
        r = await self.m.reward_char("u1", c["id"])
        self.assertTrue(r["ok"])
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 45)
        p = await self.m.get_player("u1", self.wid)
        self.assertEqual(int(p["coin"]), 10_000 - 100)
        # 到上限后拒绝，且不扣钱
        await self._set_char(c["id"], loyalty=98)
        await self.m.reward_char("u1", c["id"])
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 100)
        coin = int((await self.m.get_player("u1", self.wid))["coin"])
        with self.assertRaises(self.StrategyError):
            await self.m.reward_char("u1", c["id"])
        self.assertEqual(int((await self.m.get_player("u1", self.wid))["coin"]),
                         coin)

    async def test_reward_char_daily_limit(self):
        self.m.main_cfg()["loyalty"]["reward_daily_limit"] = 2
        cs = [await self._new_char("u1", f"奖赏{i}", loyalty=40)
              for i in range(3)]
        await self.m.reward_char("u1", cs[0]["id"])
        await self.m.reward_char("u1", cs[1]["id"])
        with self.assertRaises(self.StrategyError) as cm:
            await self.m.reward_char("u1", cs[2]["id"])
        self.assertIn("今日奖赏次数已用完", str(cm.exception))

    async def test_reward_char_rejects_foreign_and_captive(self):
        foreign = await self._new_char("u2", "别人家的", loyalty=40)
        with self.assertRaises(self.StrategyError):
            await self.m.reward_char("u1", foreign["id"])
        cap = await self._new_char("u1", "在押2", loyalty=40, status="captive")
        with self.assertRaises(self.StrategyError):
            await self.m.reward_char("u1", cap["id"])

    # ------------------------------------------------------------------
    # 一键奖赏
    # ------------------------------------------------------------------
    async def test_reward_all_prioritizes_low_loyalty_and_partial_by_coin(self):
        """铜钱只够 2 人时，优先奖赏忠诚最低的两名。"""
        await self._park_all("u1")
        await self._give("u1", coin=250)
        a = await self._new_char("u1", "低", loyalty=40)
        b = await self._new_char("u1", "中", loyalty=50)
        c = await self._new_char("u1", "高", loyalty=60)
        r = await self.m.reward_all_chars("u1")
        self.assertTrue(r["ok"])
        self.assertEqual(r["rewarded"], 2)
        self.assertEqual(r["skipped"], 1)
        self.assertEqual(r["cost"], 200)
        self.assertEqual(int((await self.m.get_char(a["id"]))["loyalty"]), 45)
        self.assertEqual(int((await self.m.get_char(b["id"]))["loyalty"]), 55)
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 60)
        self.assertEqual(int((await self.m.get_player("u1", self.wid))["coin"]), 50)

    async def test_reward_all_partial_by_daily_limit(self):
        self.m.main_cfg()["loyalty"]["reward_daily_limit"] = 2
        await self._park_all("u1")
        cs = [await self._new_char("u1", f"一键{i}", loyalty=40 + i * 10)
              for i in range(3)]
        r = await self.m.reward_all_chars("u1")
        self.assertEqual(r["rewarded"], 2)
        self.assertEqual(r["skipped"], 1)
        # 优先最低的两名
        self.assertEqual(int((await self.m.get_char(cs[0]["id"]))["loyalty"]), 45)
        self.assertEqual(int((await self.m.get_char(cs[1]["id"]))["loyalty"]), 55)
        self.assertEqual(int((await self.m.get_char(cs[2]["id"]))["loyalty"]), 60)

    async def test_reward_all_rejects_when_none_eligible(self):
        await self._park_all("u1")
        c = await self._new_char("u1", "满忠诚", loyalty=100)
        with self.assertRaises(self.StrategyError):
            await self.m.reward_all_chars("u1")
        self.assertEqual(int((await self.m.get_char(c["id"]))["loyalty"]), 100)

    # ------------------------------------------------------------------
    # 状态快照
    # ------------------------------------------------------------------
    async def test_state_exposes_reward_meta(self):
        c = await self._new_char("u1", "快照武将", loyalty=40)
        state = await self.m.get_state("u1")
        pv = state["player"]
        lr = pv["loyalty_reward"]
        self.assertEqual(lr["gain"], 5)
        self.assertEqual(lr["cost_coin"], 100)
        self.assertEqual(lr["cap"], 100)
        self.assertEqual(lr["daily_left"], 20)
        self.assertEqual(lr["natural_gain_per_tick"], 1)
        item = next(x for x in pv["chars"] if int(x["id"]) == int(c["id"]))
        self.assertTrue(item["can_reward"])
        self.assertEqual(item["reward_gain"], 5)
        self.assertEqual(item["reward_cost"], 100)


if __name__ == "__main__":
    unittest.main()
