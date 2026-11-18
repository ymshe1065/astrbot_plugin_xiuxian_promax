"""v7.10.3 高风险玩法回归：藏宝图灵石、暗标/盘口并发、矿脉合伙、古方重复参悟、血镖文案。"""

import time
import unittest

try:
    import test_equipment_instances as fixtures
except ModuleNotFoundError:  # 从仓库根目录运行时走包内导入
    from tests import test_equipment_instances as fixtures


class TestHighStakesRegressions(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TestEquipmentInstances.asyncSetUp
    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player
    _imp = staticmethod(fixtures._imp)

    async def _gold(self, user_id):
        fresh = await self.db.get_player_by_id(user_id)
        return int(fresh.gold or 0)

    def _manager(self, module, cls_name, *args):
        return getattr(self._imp(module), cls_name)(*args)

    # ===== 古修藏宝图 =====
    async def test_treasure_map_settle_pays_pending_floor_gold(self):
        p = await self._player("treasure_gold", gold=1000)
        mgr = self._manager("managers.treasure_map_manager", "TreasureMapManager",
                            self.db, self.ring, self.cm)
        run = {
            "floor": 3,
            "map_tier": 1,
            "loot": [
                {"kind": "gold", "amount": 1234},
                {"kind": "material", "name": "玄铁", "qty": 1},
            ],
            "log": [],
        }
        ok, msg = await mgr._settle(p, run, success=True, reason="主动撤退")
        self.assertTrue(ok, msg)
        self.assertEqual(await self._gold(p.user_id), 1000 + 1234)

    async def test_treasure_map_failed_run_keeps_no_pending_gold(self):
        p = await self._player("treasure_dead", gold=1000)
        mgr = self._manager("managers.treasure_map_manager", "TreasureMapManager",
                            self.db, self.ring, self.cm)
        run = {"floor": 2, "map_tier": 1, "loot": [{"kind": "gold", "amount": 999}], "log": []}
        ok, _msg = await mgr._settle(p, run, success=False, reason="倒下")
        self.assertTrue(ok)
        self.assertEqual(await self._gold(p.user_id), 1000)

    # ===== 拍卖暗标 =====
    async def test_blind_auction_bid_rolls_back_if_round_settles_mid_flight(self):
        p = await self._player("blind_race", gold=100000)
        mgr = self._manager("managers.blind_auction_manager", "BlindAuctionManager",
                            self.db, self.ring, self.cm)
        original_ensure = mgr._ensure_round

        async def settle_before_txn(now=None):
            rnd = await original_ensure(now)
            await self.db.conn.execute(
                "UPDATE blind_auction_rounds SET settled = 1 WHERE round_key = ?",
                (rnd["round_key"],),
            )
            await self.db.conn.commit()
            return rnd

        mgr._ensure_round = settle_before_txn
        ok, msg = await mgr.place_bid(p, 1, 5000)
        self.assertFalse(ok, msg)
        self.assertEqual(await self._gold(p.user_id), 100000)
        async with self.db.conn.execute(
            "SELECT COUNT(*) FROM blind_auction_bids WHERE user_id = ?", (p.user_id,)
        ) as cursor:
            self.assertEqual(int((await cursor.fetchone())[0]), 0)

    # ===== 斗法盘口 =====
    async def test_betting_pool_bet_rolls_back_if_pool_closes_mid_flight(self):
        p = await self._player("bet_race", gold=100000)
        now = int(time.time())
        mgr = self._manager("managers.betting_pool_manager", "BettingPoolManager", self.db)
        pools = await mgr.ensure_pools(now)
        first_id = int(pools[0]["id"])
        await self.db.conn.execute(
            "UPDATE betting_pools SET status = 'open', close_at = ? WHERE id = ?",
            (now + 3600, first_id),
        )
        await self.db.conn.commit()

        async def no_settle(_now=None):
            return 0

        async def close_before_txn(_now=None):
            rows = await mgr._fetchall(
                "SELECT * FROM betting_pools WHERE pool_key LIKE ? ORDER BY id",
                (f"duel:{mgr._block(now)}:%",),
            )
            await self.db.conn.execute(
                "UPDATE betting_pools SET status = 'settled' WHERE id = ?", (first_id,)
            )
            await self.db.conn.commit()
            return rows

        mgr.settle_due = no_settle
        mgr.ensure_pools = close_before_txn
        ok, msg = await mgr.place_bet(p, 1, "A", 1000)
        self.assertFalse(ok, msg)
        self.assertEqual(await self._gold(p.user_id), 100000)
        async with self.db.conn.execute(
            "SELECT COUNT(*) FROM betting_bets WHERE user_id = ?", (p.user_id,)
        ) as cursor:
            self.assertEqual(int((await cursor.fetchone())[0]), 0)

    # ===== 矿脉合伙人 =====
    async def test_mine_partner_acceptance_does_not_backpay_history(self):
        owner = await self._player("mine_owner", gold=0)
        partner = await self._player("mine_partner", gold=0)
        mgr = self._manager("managers.mine_vein_manager", "MineVeinManager",
                            self.db, self.ring, self.cm)
        now = int(time.time())
        await self.db.conn.execute(
            "INSERT INTO mine_veins "
            "(owner_id, vein_key, name, tier, buy_price, base_yield, richness, "
            "created_at, expire_at, owner_claimed_hour, status) "
            "VALUES (?, ?, ?, 1, 0, 1000, 100, ?, ?, ?, 'active')",
            (owner.user_id, "test:1", "测试矿脉", now - 5 * 3600, now + 3600,
             mgr._hour_key(now) - 5),
        )
        await self.db.conn.commit()
        mine_id = int((await mgr._fetchone(
            "SELECT id FROM mine_veins WHERE owner_id = ?", (owner.user_id,)
        ))["id"])
        ok, msg = await mgr.invite_partner(owner, mine_id, partner.user_name, 20)
        self.assertTrue(ok, msg)
        # 受邀方在 /矿脉 我的 里能看到待处理邀请
        self.assertIn("待处理的合伙邀请", await mgr.my_mines(partner.user_id))
        ok, msg = await mgr.respond_invite(partner, mine_id, True)
        self.assertTrue(ok, msg)
        # 接受后转为「我参与的矿脉」，且邀请区块消失
        partner_view = await mgr.my_mines(partner.user_id)
        self.assertIn("我参与的矿脉", partner_view)
        self.assertNotIn("待处理的合伙邀请", partner_view)
        row = await mgr._fetchone(
            "SELECT last_claim_hour FROM mine_partners WHERE mine_id = ? AND user_id = ?",
            (mine_id, partner.user_id),
        )
        self.assertEqual(int(row["last_claim_hour"]), mgr._hour_key())
        # 接受邀请后不应补领邀请之前的历史产量
        ok, _msg = await mgr.claim(partner, mine_id)
        self.assertFalse(ok)
        self.assertEqual(await self._gold(partner.user_id), 0)

    # ===== 古方残卷 =====
    async def test_ancient_recipe_cannot_be_charged_twice_in_same_period(self):
        p = await self._player("ancient_user", gold=500000)
        mgr = self._manager("managers.alchemy_manager", "AlchemyManager",
                            self.db, self.cm, self.ring)
        candidates = mgr._ancient_candidates(p.user_id)
        if not candidates:
            self.skipTest("当前配置没有可用的古方残卷")
        cost = int(candidates[0]["cost"])
        ok, msg = await mgr.study_ancient(p, 1)
        self.assertTrue(ok, msg)
        gold_after_first = await self._gold(p.user_id)
        self.assertEqual(gold_after_first, 500000 - cost)
        ok, msg = await mgr.study_ancient(p, 1)
        self.assertFalse(ok, msg)
        self.assertIn("已经参悟过", msg)
        self.assertEqual(await self._gold(p.user_id), gold_after_first)

    # ===== 网页仙市杠杆参数 =====
    async def test_web_invest_margin_plan_covers_open_status_and_close(self):
        from webchat_room.arg_options import plan_for

        opened = plan_for("仙市", ["杠杆"])
        self.assertEqual([f.get("key") for f in opened], ["sub", "arg1", "arg2", "arg3"])
        self.assertEqual(opened[1].get("source"), "margin_arg1")
        self.assertEqual(opened[2].get("source"), "margin_leverage")
        self.assertEqual([f.get("key") for f in plan_for("仙市", ["杠杆 状态"])], ["sub"])
        closed = plan_for("仙市", ["杠杆 平仓"])
        self.assertEqual([f.get("key") for f in closed], ["sub", "arg1"])
        self.assertEqual(closed[1].get("source"), "margin_arg1")

    # ===== 血镖业力文案 =====
    async def test_blood_escort_karma_text_matches_negative_karma(self):
        UserStatus = self._imp("models_extended").UserStatus

        p = await self._player("blood_escort", gold=0)
        await self.db.ext.set_user_busy(
            p.user_id, UserStatus.DELIVERING, scheduled_time=0,
            extra_data={
                "escort_gold": 2000,
                "escort_exp": 500,
                "escort_mode": "blood",
                "bonus_applied": True,
            },
        )
        mgr = self._manager("managers.escort_manager", "EscortManager",
                            self.db, None, self.ring, self.cm)
        ok, msg = await mgr.finish_escort(p)
        self.assertTrue(ok, msg)
        self.assertIn("业力 +1（功德 -1）", msg)
        fresh = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(int(fresh.karma or 0), -1)


if __name__ == "__main__":
    unittest.main()
