# tests/test_strategy_salary.py
# 仙域争霸俸禄机制回归：v4.5.7 起角色官职 / 据点太守俸禄按 tick 从
# **势力主钱包**扣铜钱；钱包不足时停发对应增益（部分支付按优先级）。
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


class TestStrategySalary(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.Player = _imp("models").Player
        sm_mod = _imp("managers.strategy_manager")
        self.StrategyManager = sm_mod.StrategyManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        for uid in ("u1", "u2", "u3"):
            await self.db.create_player(self.Player(
                user_id=uid, gold=10_000_000, user_name=f"测试{uid}"))

        self.m = self.StrategyManager(self.db)
        self.m.main_cfg()["tick_minutes"] = 1
        self.world = await self.m.ensure_world()
        self.wid = self.world["id"]
        self.p1 = await self._enter("u1", "甲")
        self.p2 = await self._enter("u2", "乙")
        await self._enter("u3", "丙")
        self.f1 = await self.m.get_faction(int(self.p1["faction_id"]))
        self.home1 = await self._owned_node("u1")

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

    async def _owned_node(self, uid: str) -> dict:
        p = await self.m.get_player(uid, self.wid)
        for n in await self.m.get_nodes(self.wid):
            if int(n["owner_faction"]) == int(p["faction_id"]):
                return n
        self.fail(f"{uid} 没有据点")

    async def _char(self, uid: str, role: str = "general") -> dict:
        for c in await self.m.get_chars(owner_user_id=uid, world_id=self.wid):
            if c["role"] == role:
                return c
        self.fail(f"{uid} 没有 {role} 角色")

    async def _no_salary_mods(self, c: dict) -> dict:
        """清除会影响俸禄的随机个性/特技（贪财/节俭），保证基线确定性。"""
        c["personality"] = {"top": "", "negative": ""}
        c["traits"] = [t for t in (c.get("traits") or []) if t != "frugal"]
        await self.m.save_char(c)
        return c

    async def _set_wallet(self, coin: int, fid: int = 0):
        """设置势力主钱包铜钱（俸禄由势力主钱包支付）。"""
        f = await self.m.get_faction(int(fid or self.f1["id"]))
        lp = await self.m._leader_player(self.wid, f)
        self.assertIsNotNone(lp)
        lp["coin"] = int(coin)
        await self.m.save_player(lp)
        await self.m._commit()
        return f

    async def _wallet_coin(self, fid: int = 0) -> int:
        f = await self.m.get_faction(int(fid or self.f1["id"]))
        lp = await self.m._leader_player(self.wid, f)
        return int((lp or {}).get("coin", 0) or 0)

    async def _salary(self) -> dict:
        await self.m._step_salary(self.world)
        f = await self.m.get_faction(int(self.f1["id"]))
        return f

    # ------------------------------------------------------------------
    # 足额发放：官职 + 太守都扣款，加成生效
    # ------------------------------------------------------------------
    async def test_salary_paid_and_bonus_active(self):
        general = await self._no_salary_mods(await self._char("u1", "general"))
        official = await self._no_salary_mods(await self._char("u1", "official"))
        await self.m.faction_appoint_char("u1", int(general["id"]), "zhonglang")
        await self.m.appoint_taishou("u1", int(self.home1["id"]), int(official["id"]))
        await self._set_wallet(1000)

        f = await self._salary()
        per = self.m.main_cfg()["faction"]["salary"]["per_tick"]
        expect = int(per["zhonglang"]) + int(per["taishou"])
        self.assertEqual(await self._wallet_coin(), 1000 - expect)
        self.assertEqual(int(f["salary"]["due"]), expect)
        self.assertEqual(int(f["salary"]["paid"]), expect)
        self.assertEqual(int(f["salary"]["short"]), 0)
        self.assertEqual(f["salary"]["unpaid_role_char_ids"], [])
        self.assertEqual(f["salary"]["unpaid_governor_node_ids"], [])

        bonus = await self.m._faction_position_bonus(f)
        self.assertAlmostEqual(float(bonus["recruit_quality"]), 0.10)
        self.assertTrue(self.m._salary_governor_paid(f, int(self.home1["id"])))

    # ------------------------------------------------------------------
    # 国库只够官职：太守欠俸，太守加成暂停；官职加成保留
    # ------------------------------------------------------------------
    async def test_partial_payment_stops_governor_bonus(self):
        general = await self._no_salary_mods(await self._char("u1", "general"))
        official = await self._no_salary_mods(await self._char("u1", "official"))
        await self.m.faction_appoint_char("u1", int(general["id"]), "zhonglang")
        await self.m.appoint_taishou("u1", int(self.home1["id"]), int(official["id"]))
        per = self.m.main_cfg()["faction"]["salary"]["per_tick"]
        # 够付中郎将，不够再付太守（优先级中郎将在太守之前）
        await self._set_wallet(int(per["zhonglang"]) + 5)

        f = await self._salary()
        self.assertEqual(await self._wallet_coin(), 5)
        self.assertEqual(int(f["salary"]["short"]), int(per["taishou"]))
        self.assertEqual(f["salary"]["unpaid_role_char_ids"], [])
        self.assertEqual(f["salary"]["unpaid_governor_node_ids"],
                         [int(self.home1["id"])])
        bonus = await self.m._faction_position_bonus(f)
        self.assertAlmostEqual(float(bonus["recruit_quality"]), 0.10)
        self.assertFalse(self.m._salary_governor_paid(f, int(self.home1["id"])))

    # ------------------------------------------------------------------
    # 国库不足：官职俸禄停发，角色官职加成与带兵上限回落
    # ------------------------------------------------------------------
    async def test_unpaid_role_bonus_and_troop_cap_disabled(self):
        general = await self._char("u1", "general")
        await self.m.faction_appoint_char("u1", int(general["id"]), "zhonglang")
        await self._set_wallet(0)

        f = await self._salary()
        self.assertEqual(int(f["salary"]["paid"]), 0)
        self.assertGreater(int(f["salary"]["short"]), 0)
        self.assertIn(int(general["id"]), f["salary"]["unpaid_role_char_ids"])
        bonus = await self.m._faction_position_bonus(f)
        self.assertAlmostEqual(float(bonus["recruit_quality"]), 0.0)

        fresh = await self.m.get_char(int(general["id"]))
        cap = await self.m._leader_cap(fresh, f, int(self.home1["id"]))
        self.assertEqual(int(cap), 1500)

    # ------------------------------------------------------------------
    # 太守欠俸：无角色官职时带兵上限回落到 1500
    # ------------------------------------------------------------------
    async def test_unpaid_governor_troop_cap_disabled(self):
        official = await self._char("u1", "official")
        await self.m.appoint_taishou("u1", int(self.home1["id"]), int(official["id"]))
        await self._set_wallet(0)

        f = await self._salary()
        self.assertIn(int(self.home1["id"]),
                      f["salary"]["unpaid_governor_node_ids"])
        fresh = await self.m.get_char(int(official["id"]))
        cap = await self.m._leader_cap(fresh, f, int(self.home1["id"]))
        self.assertEqual(int(cap), 1500)

    # ------------------------------------------------------------------
    # 联盟不共享国库：成员角色官职只从成员自己势力发薪
    # ------------------------------------------------------------------
    async def test_alliance_salary_never_uses_host_treasury(self):
        f1 = await self.m.get_faction(int(self.f1["id"]))
        f2 = await self.m.get_faction(int(self.p2["faction_id"]))
        # 建立最小联盟：u1 势力为盟主，u2 势力为成员
        f1["alliance_id"] = int(f1["id"])
        f2["alliance_id"] = int(f1["id"])
        await self.m.save_faction(f1)
        await self.m.save_faction(f2)
        await self.m._commit()

        c2 = await self._no_salary_mods(await self._char("u2", "general"))
        await self.m.faction_appoint_char("u1", int(c2["id"]), "zhonglang")
        per = self.m.main_cfg()["faction"]["salary"]["per_tick"]
        await self._set_wallet(1000, int(f1["id"]))
        await self._set_wallet(0, int(f2["id"]))

        # 盟主钱包充足、成员势力主钱包为空：成员官职欠俸，盟主钱包不得被扣
        await self.m._step_salary(self.world)
        f1 = await self.m.get_faction(int(f1["id"]))
        f2 = await self.m.get_faction(int(f2["id"]))
        self.assertEqual(await self._wallet_coin(int(f1["id"])), 1000)
        self.assertEqual(await self._wallet_coin(int(f2["id"])), 0)
        self.assertIn(int(c2["id"]), f2["salary"]["unpaid_role_char_ids"])
        # 联盟不共享经济：盟主看不到成员中郎将的招募加成
        bonus = await self.m._faction_position_bonus(
            f1, include_alliance=False)
        self.assertAlmostEqual(float(bonus["recruit_quality"]), 0.0)
        bonus = await self.m._faction_position_bonus(
            f2, include_alliance=False)
        self.assertAlmostEqual(float(bonus["recruit_quality"]), 0.0)

        # 成员自己补足钱包：只扣成员势力主钱包，本势力官职加成恢复
        await self._set_wallet(int(per["zhonglang"]), int(f2["id"]))
        await self.m._step_salary(self.world)
        f1 = await self.m.get_faction(int(f1["id"]))
        f2 = await self.m.get_faction(int(f2["id"]))
        self.assertEqual(await self._wallet_coin(int(f1["id"])), 1000)
        self.assertEqual(await self._wallet_coin(int(f2["id"])), 0)
        bonus = await self.m._faction_position_bonus(
            f1, include_alliance=False)
        self.assertAlmostEqual(float(bonus["recruit_quality"]), 0.0)
        bonus = await self.m._faction_position_bonus(
            f2, include_alliance=False)
        self.assertAlmostEqual(float(bonus["recruit_quality"]), 0.10)

    # ------------------------------------------------------------------
    # 联盟独立资源：据点灵石入产出玩家钱包，不使用盟主科技
    # ------------------------------------------------------------------
    async def test_alliance_output_uses_own_tech(self):
        f1 = await self.m.get_faction(int(self.f1["id"]))
        f2 = await self.m.get_faction(int(self.p2["faction_id"]))
        f1["alliance_id"] = int(f1["id"])
        f2["alliance_id"] = int(f1["id"])
        await self.m.save_faction(f1)
        await self.m.save_faction(f2)
        # 让盟主势力没有据点，只剩成员势力产出
        for n in await self.m.get_nodes(self.wid):
            if int(n["owner_faction"]) == int(f1["id"]):
                n["owner_faction"] = 0
                await self.m.save_node(n)
        await self.m._commit()

        async def _gold(uid: str) -> int:
            player = await self.db.get_player_by_id(uid)
            return int((player.gold if player else 0) or 0)

        # 第一轮：盟主科技 0，记录成员玩家主玩法灵石增量
        f1 = await self.m.get_faction(int(f1["id"]))
        f2 = await self.m.get_faction(int(f2["id"]))
        f1["tech"] = {}
        f2["tech"] = {}
        await self.m.save_faction(f1)
        await self.m.save_faction(f2)
        p2 = await self.m.get_player("u2", self.wid)
        p2["daily_output_log"] = {}
        await self.m.save_player(p2)
        await self.m._commit()
        base_gold = await _gold("u2")
        await self.m._step_distribute_output(self.world, True)
        base = await _gold("u2") - base_gold
        self.assertGreater(base, 0)

        # 第二轮：只给盟主加满经营科技，成员产出不应被盟主科技放大
        f1 = await self.m.get_faction(int(f1["id"]))
        f2 = await self.m.get_faction(int(f2["id"]))
        f1["tech"] = {"agri": 7}
        f2["tech"] = {}
        await self.m.save_faction(f1)
        await self.m.save_faction(f2)
        await self.m._commit()
        before = await _gold("u2")
        await self.m._step_distribute_output(self.world, True)
        self.assertEqual(await _gold("u2") - before, base)

    # ------------------------------------------------------------------
    # 个性/特技俸禄联动：贪财 +20%、节俭 -15%；与科技「仁政」-20% 叠乘
    # ------------------------------------------------------------------
    async def test_salary_personality_and_tech_multipliers(self):
        general = await self._char("u1", "general")
        official = await self._char("u1", "official")
        # 将领贪财（俸禄 ×1.20），并确保未带节俭；官员节俭（×0.85）
        general["personality"] = {"negative": "greedy"}
        general["traits"] = [t for t in (general.get("traits") or [])
                             if t != "frugal"]
        await self.m.save_char(general)
        official["traits"] = [t for t in (official.get("traits") or [])
                              if t != "frugal"] + ["frugal"]
        await self.m.save_char(official)
        await self.m.faction_appoint_char("u1", int(general["id"]), "zhonglang")
        await self.m.appoint_taishou("u1", int(self.home1["id"]),
                                     int(official["id"]))
        f = await self.m.get_faction(int(self.f1["id"]))
        f["tech"] = {}
        await self.m.save_faction(f)
        per = self.m.main_cfg()["faction"]["salary"]["per_tick"]
        role_due = int(round(int(per["zhonglang"]) * 1.0 * 1.20))
        gov_due = int(round(int(per["taishou"]) * 1.0 * 0.85))

        await self._set_wallet(100000)
        f = await self._salary()
        self.assertEqual(int(f["salary"]["due"]), role_due + gov_due)

        # 科技「仁政」L7（salary_mult=0.8）叠乘
        f = await self.m.get_faction(int(self.f1["id"]))
        f["tech"] = {"agri": 7}
        await self.m.save_faction(f)
        await self._set_wallet(100000)
        f = await self._salary()
        role_due2 = int(round(int(per["zhonglang"]) * 0.8 * 1.20))
        gov_due2 = int(round(int(per["taishou"]) * 0.8 * 0.85))
        self.assertEqual(int(f["salary"]["due"]), role_due2 + gov_due2)

    # ------------------------------------------------------------------
    # 关闭俸禄：清除停发状态，加成恢复且不扣款
    # ------------------------------------------------------------------
    async def test_salary_disabled_clears_unpaid_state(self):
        general = await self._char("u1", "general")
        await self.m.faction_appoint_char("u1", int(general["id"]), "zhonglang")
        await self._set_wallet(0)
        await self._salary()
        cfg = self.m.main_cfg()
        cfg.setdefault("faction", {}).setdefault("salary", {})["enabled"] = False
        try:
            f = await self._salary()
            self.assertEqual(f.get("salary") or {}, {})
            bonus = await self.m._faction_position_bonus(f)
            self.assertAlmostEqual(float(bonus["recruit_quality"]), 0.10)
        finally:
            cfg["faction"]["salary"]["enabled"] = True

    async def test_legacy_salary_from_treasury_false_still_disables(self):
        """旧配置只保留 salary_from_treasury=false 时，升级后不得静默发俸禄。"""
        general = await self._char("u1", "general")
        await self.m.faction_appoint_char("u1", int(general["id"]), "zhonglang")
        await self._set_wallet(1000)
        cfg = self.m.main_cfg()
        salary_cfg = cfg.setdefault("faction", {}).setdefault("salary", {})
        old_enabled = salary_cfg.pop("enabled", None)
        old_switch = cfg["faction"].get("salary_enabled")
        cfg["faction"].pop("salary_enabled", None)
        cfg["faction"]["salary_from_treasury"] = False
        try:
            f = await self._salary()
            self.assertEqual(f.get("salary") or {}, {})
            self.assertEqual(await self._wallet_coin(), 1000)
        finally:
            cfg["faction"].pop("salary_from_treasury", None)
            if old_enabled is not None:
                salary_cfg["enabled"] = old_enabled
            if old_switch is not None:
                cfg["faction"]["salary_enabled"] = old_switch


if __name__ == "__main__":
    unittest.main()
