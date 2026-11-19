"""v3.47.1 第四轮高风险玩法 debug 回归。

覆盖本轮修复：
- 矿脉开采分成不再用旧快照覆盖并发入账的灵石；
- 藏宝图结算「失败分支」同样在事务内重读灵石；
- 藏宝图推进/阵亡落库都带 ``status='active'`` 守卫，且阵亡真正把战利品清空；
- 仙市杠杆强平线口径与 ``_margin_equity`` 一致（含利息与平仓费）；
- 盘口删除不可达的 ``kind='stone'`` 开奖分支，结算口径唯一化，
  两侧期望收益恒 ≤ 1（旧分支缺 ``p_success`` 时按 50% 开奖即印钞）；
- 黑市锦囊「抢占拆开权 + 扣费」同事务，绕过玩家锁也拆不了第二次；
- 灵田盲种扣费失败时不污染 player 对象与灵田数据。
"""

import json
import re
import time
import unittest

try:
    import test_equipment_instances as fixtures
except ModuleNotFoundError:  # 从仓库根目录运行时走包内导入
    from tests import test_equipment_instances as fixtures


class _FixedRng:
    """盘口开奖用：把 ``random()`` 钉死成指定值，验证概率边界。"""

    def __init__(self, value):
        self._value = value

    def random(self):
        return self._value

    def uniform(self, _a, _b):
        return self._value

    def choice(self, seq):
        return seq[0]

    def choices(self, seq, weights=None, k=1):  # noqa: ARG002 - 盲种只需确定性
        return [seq[0]]


class TestHighStakesRound4(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TestEquipmentInstances.asyncSetUp
    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player
    _imp = staticmethod(fixtures._imp)

    def _manager(self, module, cls_name, *args):
        return getattr(self._imp(module), cls_name)(*args)

    async def _gold(self, user_id):
        fresh = await self.db.get_player_by_id(user_id)
        return int(fresh.gold or 0)

    async def _scalar(self, sql, params=()):
        async with self.db.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    # ===== 矿脉：分成不能覆盖并发入账 =====
    async def test_mine_claim_does_not_clobber_concurrent_credit(self):
        """后台派彩（暗标退款/盘口）不持玩家锁，全字段回写前必须在事务内重读灵石。

        旧写法 `player.gold += total_gold` 用的是命令开始时的快照，
        结算期间的入账会被整笔抹掉。
        """
        p = await self._player("mine_race", gold=1000)
        mgr = self._manager("managers.mine_vein_manager", "MineVeinManager",
                            self.db, self.ring, self.cm)
        now = int(time.time())
        await self.db.conn.execute(
            "INSERT INTO mine_veins "
            "(owner_id, vein_key, name, tier, buy_price, base_yield, richness, "
            "created_at, expire_at, owner_claimed_hour, status) "
            "VALUES (?, ?, ?, 1, 0, 1000, 100, ?, ?, ?, 'active')",
            (p.user_id, "race:1", "并发矿脉", now - 5 * 3600, now + 3600,
             mgr._hour_key(now) - 5),
        )
        await self.db.conn.commit()
        mine_id = int((await mgr._fetchone(
            "SELECT id FROM mine_veins WHERE owner_id = ?", (p.user_id,)
        ))["id"])

        # 产量计算期间「后台」入账 5000 灵石（早于事务内的重读，晚于命令开始的快照）
        original_roll = mgr._roll_event
        injected = {"done": False}

        async def racing_roll(*args, **kwargs):
            out = await original_roll(*args, **kwargs)
            if not injected["done"]:
                injected["done"] = True
                await self.db.conn.execute(
                    "UPDATE players SET gold = gold + 5000 WHERE user_id = ?", (p.user_id,)
                )
            return out

        mgr._roll_event = racing_roll
        ok, msg = await mgr.claim(p, mine_id)
        self.assertTrue(ok, msg)
        self.assertTrue(injected["done"], "用例前提：并发入账必须真的发生过")
        income_match = re.search(r"灵石收入：\+([\d,]+)", msg)
        self.assertIsNotNone(income_match, f"开采成功报文必须带灵石收入行：{msg}")
        income = int(income_match.group(1).replace(",", ""))
        self.assertGreater(income, 0)
        self.assertEqual(await self._gold(p.user_id), 1000 + 5000 + income)

    async def test_mine_claim_progress_guard_still_holds(self):
        """重读灵石不能把「同一段产量只能领一次」的守卫弄丢。"""
        p = await self._player("mine_double", gold=0)
        mgr = self._manager("managers.mine_vein_manager", "MineVeinManager",
                            self.db, self.ring, self.cm)
        now = int(time.time())
        await self.db.conn.execute(
            "INSERT INTO mine_veins "
            "(owner_id, vein_key, name, tier, buy_price, base_yield, richness, "
            "created_at, expire_at, owner_claimed_hour, status) "
            "VALUES (?, ?, ?, 1, 0, 1000, 100, ?, ?, ?, 'active')",
            (p.user_id, "double:1", "双领矿脉", now - 3 * 3600, now + 3600,
             mgr._hour_key(now) - 3),
        )
        await self.db.conn.commit()
        mine_id = int((await mgr._fetchone(
            "SELECT id FROM mine_veins WHERE owner_id = ?", (p.user_id,)
        ))["id"])
        ok, _msg = await mgr.claim(p, mine_id)
        self.assertTrue(ok)
        first = await self._gold(p.user_id)
        self.assertGreater(first, 0)
        ok, msg = await mgr.claim(p, mine_id)
        self.assertFalse(ok)
        self.assertIn("还没有新的整点产量", msg)
        self.assertEqual(await self._gold(p.user_id), first)

    # ===== 藏宝图 =====
    async def _insert_active_run(self, user_id, run, hp=100, max_hp=1000):
        now = int(time.time())
        await self.db.conn.execute(
            "INSERT INTO treasure_map_runs "
            "(user_id, floor, map_tier, hp, max_hp, heal_used, heal_budget, "
            "loot, log, status, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 0, 180, ?, '[]', 'active', ?, ?)",
            (
                user_id, int(run.get("floor") or 0), int(run.get("map_tier") or 1),
                hp, max_hp, json.dumps(run.get("loot") or [], ensure_ascii=False),
                now, now,
            ),
        )
        await self.db.conn.commit()

    async def test_treasure_map_death_actually_clears_loot(self):
        """文案说「战利品尽失」，数据库里也必须真的清空。"""
        p = await self._player("tm_death", gold=1000)
        p.hp = 1
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        mgr = self._manager("managers.treasure_map_manager", "TreasureMapManager",
                            self.db, self.ring, self.cm)
        loot = [{"kind": "gold", "amount": 999}, {"kind": "material", "name": "玄铁", "qty": 1}]
        await self._insert_active_run(p.user_id, {"floor": 0, "map_tier": 1, "loot": loot}, hp=1)

        ok, msg = await mgr.advance(p)
        self.assertTrue(ok, msg)
        self.assertIn("倒下了", msg)
        self.assertIn("全部遗失", msg)
        self.assertEqual(await self._gold(p.user_id), 1000)
        row = await mgr._fetchone(
            "SELECT status, loot, hp, floor FROM treasure_map_runs WHERE user_id = ?",
            (p.user_id,),
        )
        self.assertEqual(row["status"], "ended")
        self.assertEqual(json.loads(row["loot"]), [], "阵亡必须清空 loot")
        self.assertEqual(int(row["hp"]), 1)
        self.assertEqual(int(row["floor"]), 1)
        fresh = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(int(fresh.hp or 0), 1)

    async def _race_get_active_run(self, mgr, user_id):
        """让 get_active_run 读到 active，返回后立刻被后台按撤退结算。"""
        original = mgr.get_active_run
        injected = {"done": False}

        async def racing(uid):
            run = await original(uid)
            if run is not None and not injected["done"]:
                injected["done"] = True
                await self.db.conn.execute(
                    "UPDATE treasure_map_runs SET status='ended' "
                    "WHERE user_id = ? AND status='active'", (uid,),
                )
            return run

        mgr.get_active_run = racing
        return injected

    async def test_treasure_map_advance_guarded_after_background_settle(self):
        """推进落库必须带 status 守卫，守卫失败要整笔回滚（含气血）。"""
        p = await self._player("tm_guard", gold=1000)
        p.hp = 1_000_000
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        mgr = self._manager("managers.treasure_map_manager", "TreasureMapManager",
                            self.db, self.ring, self.cm)
        await self._insert_active_run(p.user_id, {"floor": 0, "map_tier": 1, "loot": []},
                                     hp=1_000_000, max_hp=1000)
        injected = await self._race_get_active_run(mgr, p.user_id)

        ok, msg = await mgr.advance(p)
        self.assertTrue(injected["done"], "用例前提：必须模拟出后台结算竞态")
        self.assertFalse(ok)
        self.assertIn("已被后台按「撤退」结算", msg)
        # 气血与层数都不能被半截写入
        self.assertEqual(int((await self.db.get_player_by_id(p.user_id)).hp), 1_000_000)
        self.assertEqual(int(p.hp), 1_000_000)
        row = await mgr._fetchone(
            "SELECT status, floor, hp FROM treasure_map_runs WHERE user_id = ?", (p.user_id,)
        )
        self.assertEqual(row["status"], "ended")
        self.assertEqual(int(row["floor"]), 0)
        self.assertEqual(int(row["hp"]), 1_000_000)

    async def test_treasure_map_death_after_background_settle_keeps_loot(self):
        """阵亡落库同样要带守卫：已被后台结算过时不能再清掉玩家战利品。"""
        p = await self._player("tm_death_race", gold=1000)
        p.hp = 1
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        mgr = self._manager("managers.treasure_map_manager", "TreasureMapManager",
                            self.db, self.ring, self.cm)
        loot = [{"kind": "gold", "amount": 999}]
        await self._insert_active_run(p.user_id, {"floor": 2, "map_tier": 1, "loot": loot}, hp=1)
        injected = await self._race_get_active_run(mgr, p.user_id)

        ok, msg = await mgr.advance(p)
        self.assertTrue(injected["done"])
        self.assertTrue(ok, msg)
        self.assertIn("仍然有效", msg)
        self.assertNotIn("全部遗失", msg)
        row = await mgr._fetchone(
            "SELECT status, loot FROM treasure_map_runs WHERE user_id = ?", (p.user_id,)
        )
        self.assertEqual(row["status"], "ended")
        self.assertEqual(json.loads(row["loot"]), loot, "守卫失败时不能清空已发放的战利品")

    async def test_treasure_map_failed_settle_rereads_gold(self):
        """失败分支也会走全字段 update_player，同样必须在事务内重读灵石。"""
        p = await self._player("tm_fail_gold", gold=1000)
        mgr = self._manager("managers.treasure_map_manager", "TreasureMapManager",
                            self.db, self.ring, self.cm)
        await self._insert_active_run(
            p.user_id, {"floor": 2, "map_tier": 1, "loot": [{"kind": "gold", "amount": 999}]}
        )
        original = mgr._fetchone

        async def racing_fetchone(sql, params=()):
            # 结算事务内模拟后台入账
            await self.db.conn.execute(
                "UPDATE players SET gold = gold + 777 WHERE user_id = ?", (p.user_id,)
            )
            return await original(sql, params)

        mgr._fetchone = racing_fetchone
        ok, _msg = await mgr._settle(p, {"floor": 2, "map_tier": 1,
                                        "loot": [{"kind": "gold", "amount": 999}], "log": []},
                                    success=False, reason="倒下")
        self.assertTrue(ok)
        self.assertEqual(await self._gold(p.user_id), 1777, "失败结算不能吞掉并发入账")

    async def test_treasure_map_success_settle_rereads_gold(self):
        p = await self._player("tm_win_gold", gold=1000)
        mgr = self._manager("managers.treasure_map_manager", "TreasureMapManager",
                            self.db, self.ring, self.cm)
        loot = [{"kind": "gold", "amount": 500}]
        await self._insert_active_run(p.user_id, {"floor": 3, "map_tier": 1, "loot": loot})
        original = mgr._fetchone

        async def racing_fetchone(sql, params=()):
            await self.db.conn.execute(
                "UPDATE players SET gold = gold + 777 WHERE user_id = ?", (p.user_id,)
            )
            return await original(sql, params)

        mgr._fetchone = racing_fetchone
        ok, msg = await mgr._settle(p, {"floor": 3, "map_tier": 1, "loot": loot, "log": []},
                                    success=True, reason="主动撤退")
        self.assertTrue(ok, msg)
        self.assertEqual(await self._gold(p.user_id), 1000 + 777 + 500)

    # ===== 盘口：结算口径唯一，任何一侧期望收益都不能 > 1 =====
    async def _insert_pool(self, key, kind, odds_a, odds_b, info_a=""):
        now = int(time.time())
        await self.db.conn.execute(
            "INSERT INTO betting_pools "
            "(pool_key, kind, title, side_a, side_b, info_a, info_b, odds_a, odds_b, "
            "min_bet, max_bet, open_at, close_at, status) "
            "VALUES (?, ?, '测试盘口', 'A', 'B', ?, '', ?, ?, 100, 500000, ?, ?, 'open')",
            (key, kind, info_a, odds_a, odds_b, now - 60, now - 1),
        )
        await self.db.conn.commit()
        mgr = self._manager("managers.betting_pool_manager", "BettingPoolManager", self.db)
        return await mgr._fetchone("SELECT * FROM betting_pools WHERE pool_key = ?", (key,))

    async def _settle_with_roll(self, pool, roll):
        """用钉死的 rng 结算，反解出代码实际采用的 p_a 落在 roll 的哪一侧。"""
        pool_mod = self._imp("managers.betting_pool_manager")
        mgr = self._manager("managers.betting_pool_manager", "BettingPoolManager", self.db)
        original = pool_mod.seeded_rng
        pool_mod.seeded_rng = lambda *a, **k: _FixedRng(roll)
        try:
            settled = await mgr._settle_pool(pool, int(time.time()))
        finally:
            pool_mod.seeded_rng = original
        self.assertTrue(settled)
        return await self._scalar(
            "SELECT result FROM betting_pools WHERE id = ?", (pool["id"],)
        )

    async def test_bet_stone_settles_inline_without_pool_row(self):
        p = await self._player("stone_inline", gold=1_000_000)
        mgr = self._manager("managers.betting_pool_manager", "BettingPoolManager", self.db)
        for tier in range(1, 6):
            ok, msg = await mgr.bet_stone(p, tier, "出货", 300)
            self.assertTrue(ok, msg)
            self.assertIn("开奖：", msg)
        async with self.db.conn.execute("SELECT COUNT(*) FROM betting_pools") as cursor:
            self.assertEqual(int((await cursor.fetchone())[0]), 0,
                             "bet_stone 不应写入任何盘口行")
        async with self.db.conn.execute("SELECT COUNT(*) FROM betting_bets") as cursor:
            self.assertEqual(int((await cursor.fetchone())[0]), 0)

    async def test_ensure_pools_only_creates_duel_kind(self):
        mgr = self._manager("managers.betting_pool_manager", "BettingPoolManager", self.db)
        pools = await mgr.ensure_pools()
        self.assertTrue(pools)
        for pool in pools:
            self.assertEqual(pool["kind"], "duel")
            self.assertTrue(str(pool["pool_key"]).startswith("duel:"))
        async with self.db.conn.execute(
            "SELECT COUNT(*) FROM betting_pools WHERE kind != 'duel'"
        ) as cursor:
            self.assertEqual(int((await cursor.fetchone())[0]), 0)

    def test_settle_pool_has_a_single_probability_formula(self):
        """静态护栏：`_settle_pool` 里不允许再出现第二套按 kind 分流的开奖口径。

        这是「删掉 stone 分支」的前提与防回退守卫——那条分支一旦存在就是
        印钞地雷：p_success 缺失 → `or 0.5`，而赔率是按真实 25% 定的，
        押「切垮」期望收益 1.84 倍。
        """
        import ast
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "managers" / "betting_pool_manager.py"
        text = path.read_text(encoding="utf-8")
        fn = next(
            node for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_settle_pool"
        )
        source = ast.get_source_segment(text, fn)
        self.assertIsNotNone(source)
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn("kind", code, "结算不能按盘口类型分流")
        self.assertNotIn("info_a", code, "结算不能从 info_a 里解析 meta 概率")
        self.assertNotIn("json", code)
        self.assertEqual(code.count("HOUSE_RATE / odds_a"), 1, "开奖口径必须唯一")

    def test_stone_pool_rows_are_never_created(self):
        """全库静态断言：没有任何 INSERT 会写出 kind='stone' 的盘口行。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        inserts = 0
        for rel in ("managers", "handlers", "webchat_room"):
            for path in (root / rel).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                for chunk in text.split("INSERT")[1:]:
                    head = chunk.split(";")[0][:600]
                    if "betting_pools" not in head:
                        continue
                    inserts += 1
                    self.assertIn("'duel'", head,
                                  f"{path.relative_to(root)} 写入了非 duel 盘口")
        self.assertEqual(inserts, 1, "全库应该只有一个 betting_pools 写入点")

    def test_settle_probability_never_pays_above_par_on_either_side(self):
        """对 ensure_pools 能生成的每一个盘口，两侧期望收益都必须 ≤ 1。

        这是盘口唯一的「不印钞」不变量。旧 stone 分支破坏的正是它。
        """
        pool_mod = self._imp("managers.betting_pool_manager")
        house = pool_mod.HOUSE_RATE
        worst = 0.0
        for permille in range(200, 801):
            p_true = permille / 1000.0
            odds_a = round(house / p_true, 2)
            odds_b = round(house / (1 - p_true), 2)
            p_settle = min(0.95, max(0.05, house / odds_a))
            ev_a = p_settle * odds_a
            ev_b = (1 - p_settle) * odds_b
            self.assertLessEqual(ev_a, 1.0, f"p={p_true} 押 A 印钞：{ev_a}")
            self.assertLessEqual(ev_b, 1.0, f"p={p_true} 押 B 印钞：{ev_b}")
            worst = max(worst, ev_a, ev_b)
        # 抽水必须真实存在，否则盘口变成零和甚至负和给玩家
        self.assertLess(worst, 1.0)

    async def test_generated_duel_pools_settle_within_house_rate(self):
        """真跑一遍生成的盘口：结算 p 与赔率自洽，两侧都不超过 1。"""
        pool_mod = self._imp("managers.betting_pool_manager")
        house = pool_mod.HOUSE_RATE
        mgr = self._manager("managers.betting_pool_manager", "BettingPoolManager", self.db)
        pools = await mgr.ensure_pools()
        for offset in range(0, 4000, 137):
            now = int(time.time()) + offset * 3600
            for pool in await mgr.ensure_pools(now):
                odds_a = float(pool["odds_a"])
                odds_b = float(pool["odds_b"])
                p_settle = min(0.95, max(0.05, house / odds_a))
                self.assertLessEqual(p_settle * odds_a, 1.0)
                self.assertLessEqual((1 - p_settle) * odds_b, 1.0,
                                     f"{pool['pool_key']} 押 B 会印钞")

    async def test_legacy_stone_row_settles_by_odds_without_crash(self):
        """手工塞进来的 kind='stone' 脏行也不能崩、不能按 p_success 开奖。

        回归守卫：被删掉的分支里 `logger` 未导入会抛 NameError，
        导致该盘口永远结算失败、玩家下注的灵石一直挂着。
        """
        pool = await self._insert_pool(
            "stone:legacy", "stone", 1.23, 3.68,
            info_a=json.dumps({"p_success": 0.05}),
        )
        # 结算口径只看赔率：p_a = 0.92 / 1.23 ≈ 0.748，meta 里的 0.05 必须被忽略
        self.assertEqual(await self._settle_with_roll(pool, 0.70), "A")
        pool = await self._insert_pool(
            "stone:legacy2", "stone", 1.23, 3.68,
            info_a="not-json",
        )
        self.assertEqual(await self._settle_with_roll(pool, 0.80), "B")

    async def test_settle_pool_survives_broken_odds(self):
        """赔率为 0 / NULL 的脏行不能除零，也不能把整批盘口拖垮。"""
        pool = await self._insert_pool("duel:broken", "duel", 0, 0)
        result = await self._settle_with_roll(pool, 0.4)
        self.assertIn(result, ("A", "B"))
        # odds_a 缺失 → 退回 1.9 → p_a ≈ 0.484，roll 0.4 应开 A
        self.assertEqual(result, "A")

    # ===== 黑市锦囊 =====
    async def test_open_pouch_charges_only_once(self):
        p = await self._player("pouch_open", gold=1_000_000)
        mgr = self._manager("managers.gamble_manager", "GambleManager",
                            self.db, self.cm, self.ring)
        ok, _msg = await mgr.open_pouch(p, 1)
        self.assertTrue(ok)
        gold_after = await self._gold(p.user_id)
        self.assertLess(gold_after, 1_000_000)
        ok, msg = await mgr.open_pouch(p, 1)
        self.assertFalse(ok)
        self.assertIn("已经被你拆开", msg)
        self.assertEqual(await self._gold(p.user_id), gold_after)

    async def test_open_pouch_grabs_slot_inside_transaction(self):
        """绕过玩家锁（读到空的 state 缓存）也拆不开第二次。

        旧实现只在事务外预检查 ``state['opened']``，扣费与标记不同事务，
        任何不持锁的调用方都能反复拆同一个锦囊。
        """
        p = await self._player("pouch_race", gold=1_000_000)
        mgr = self._manager("managers.gamble_manager", "GambleManager",
                            self.db, self.cm, self.ring)
        ok, _msg = await mgr.open_pouch(p, 1)
        self.assertTrue(ok)
        gold_after = await self._gold(p.user_id)

        stale = await self.db.get_player_by_id(p.user_id)
        original_state = mgr._get_pouch_state

        async def empty_state(user_id, period):
            return {}

        mgr._get_pouch_state = empty_state
        try:
            ok, msg = await mgr.open_pouch(stale, 1)
        finally:
            mgr._get_pouch_state = original_state
        self.assertFalse(ok)
        self.assertIn("已经被你拆开", msg)
        self.assertEqual(await self._gold(p.user_id), gold_after, "第二次拆囊不能重复扣费")
        async with self.db.conn.execute(
            "SELECT COUNT(*) FROM black_market_pouches WHERE user_id = ? AND slot = 1",
            (p.user_id,),
        ) as cursor:
            self.assertEqual(int((await cursor.fetchone())[0]), 1)

    async def test_open_pouch_keeps_inspected_flag(self):
        """抢占拆开权时不能把已有的验囊标记抹掉。"""
        p = await self._player("pouch_inspected", gold=1_000_000)
        mgr = self._manager("managers.gamble_manager", "GambleManager",
                            self.db, self.cm, self.ring)
        ok, _msg = await mgr.inspect_pouch(p, 1)
        self.assertTrue(ok)
        p = await self.db.get_player_by_id(p.user_id)
        ok, _msg = await mgr.open_pouch(p, 1)
        self.assertTrue(ok)
        candidates = mgr._get_pouch_candidates(p.user_id)
        period = candidates[0]["period"]
        async with self.db.conn.execute(
            "SELECT inspected, opened FROM black_market_pouches "
            "WHERE user_id = ? AND period = ? AND slot = 1", (p.user_id, period)
        ) as cursor:
            row = await cursor.fetchone()
        self.assertEqual(int(row[0]), 1, "验囊标记被拆囊覆盖")
        self.assertEqual(int(row[1]), 1)

    # ===== 灵田盲种 =====
    async def _make_farm(self, user_id, level=5):
        await self.db.conn.execute(
            "INSERT INTO spirit_farms (user_id, level, crops, fertility) VALUES (?, ?, '[]', 0)",
            (user_id, level),
        )
        await self.db.conn.commit()

    async def test_blind_plant_failure_does_not_pollute_player(self):
        """扣费失败（快照够、库里不够）时不能留下「没付钱的解锁」。"""
        farm_mod = self._imp("managers.spirit_farm_manager")
        p = await self._player("farm_blind", gold=0)
        await self._make_farm(p.user_id)
        mgr = farm_mod.SpiritFarmManager(self.db, self.ring, None)

        # 内存快照给够钱，绕过前置校验；库里的 gold 仍是 0
        p.gold = farm_mod.BLIND_SEED_COST * 2
        unlock_before = p.farm_unlocks
        # roll > BLIND_SEED_EMPTY_CHANCE 必定进入「特殊灵植解锁」分支
        ok, msg = await mgr.blind_plant(p, count=2, rng=_FixedRng(0.99))
        self.assertFalse(ok)
        self.assertIn("灵石不足", msg)
        self.assertEqual(p.farm_unlocks, unlock_before, "失败路径污染了 player 对象")
        self.assertEqual(await self._gold(p.user_id), 0)
        self.assertEqual(
            json.loads(await self._scalar(
                "SELECT crops FROM spirit_farms WHERE user_id = ?", (p.user_id,)
            ) or "[]"),
            [],
        )
        fresh = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(
            farm_mod.SpiritFarmManager.get_unlocked_specials(fresh), [],
            "解锁结果被写进了库",
        )
        # 后续任何一次全字段回写都不能把幽灵解锁带进数据库
        await self.db.update_player(p)
        fresh = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(farm_mod.SpiritFarmManager.get_unlocked_specials(fresh), [])

    async def test_blind_plant_success_persists_unlock(self):
        farm_mod = self._imp("managers.spirit_farm_manager")
        p = await self._player("farm_blind_ok", gold=farm_mod.BLIND_SEED_COST * 4)
        await self._make_farm(p.user_id)
        mgr = farm_mod.SpiritFarmManager(self.db, self.ring, None)
        ok, msg = await mgr.blind_plant(p, count=1, rng=_FixedRng(0.99))
        self.assertTrue(ok, msg)
        self.assertIn("永久解锁", msg)
        self.assertEqual(await self._gold(p.user_id), farm_mod.BLIND_SEED_COST * 3)
        fresh = await self.db.get_player_by_id(p.user_id)
        unlocked = farm_mod.SpiritFarmManager.get_unlocked_specials(fresh)
        self.assertEqual(len(unlocked), 1)
        self.assertEqual(unlocked[0], farm_mod.BLIND_SPECIAL_HERBS[0]["name"])
        crops = json.loads(await self._scalar(
            "SELECT crops FROM spirit_farms WHERE user_id = ?", (p.user_id,)
        ))
        self.assertEqual(len(crops), 1)
        self.assertEqual(crops[0]["name"], unlocked[0])
        # 内存对象与数据库必须一致
        self.assertEqual(farm_mod.SpiritFarmManager.get_unlocked_specials(p), unlocked)

    async def test_blind_plant_empty_shell_does_not_unlock(self):
        farm_mod = self._imp("managers.spirit_farm_manager")
        p = await self._player("farm_blind_empty", gold=farm_mod.BLIND_SEED_COST * 2)
        await self._make_farm(p.user_id)
        mgr = farm_mod.SpiritFarmManager(self.db, self.ring, None)
        low = (farm_mod.BLIND_SEED_RARE_CHANCE + farm_mod.BLIND_SEED_EMPTY_CHANCE) / 2
        ok, msg = await mgr.blind_plant(p, count=1, rng=_FixedRng(low))
        self.assertTrue(ok, msg)
        self.assertIn("空壳", msg)
        fresh = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(farm_mod.SpiritFarmManager.get_unlocked_specials(fresh), [])
        self.assertEqual(json.loads(await self._scalar(
            "SELECT crops FROM spirit_farms WHERE user_id = ?", (p.user_id,)
        )), [])


FIXED_SECRET = "unit-test-secret-9f3a"


class TestMarginLiquidationLine(unittest.IsolatedAsyncioTestCase):
    """仙市杠杆强平线：显示口径必须与真实强平判定完全一致。"""

    async def asyncSetUp(self):
        import contextlib
        import os
        import tempfile

        import aiosqlite

        self._contextlib = contextlib
        self._os = os
        migration = fixtures._imp("data.migration")
        DataBase = fixtures._imp("data.data_manager").DataBase
        DatabaseExtended = fixtures._imp("data.database_extended").DatabaseExtended
        self.Player = fixtures._imp("models").Player
        self.InvestManager = fixtures._imp("managers.invest_manager").InvestManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        await self.db.ext.set_system_config("invest_secret", FIXED_SECRET)
        self.manager = self.InvestManager(self.db, {}, None)
        self.manager._secret = FIXED_SECRET
        await self.manager._seed_products()

    async def asyncTearDown(self):
        await self.db.close()
        try:
            self._os.remove(self.db_path)
        except OSError:
            pass

    def _patch_tick(self, tick, opened=True):
        from unittest import mock

        stack = self._contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            self.InvestManager, "tick_of", classmethod(lambda cls, moment=None: tick)))
        stack.enter_context(mock.patch.object(
            self.InvestManager, "is_open", classmethod(lambda cls, moment=None: opened)))
        return stack

    async def _open_position(self, uid="margin_u1", gold=1_000_000,
                             leverage=3, margin=50_000):
        await self.manager._settle_tick(10)
        await self.db.create_player(self.Player(user_id=uid, user_name=uid, gold=gold))
        player = await self.db.get_player_by_id(uid)
        with self._patch_tick(10):
            ok, msg = await self.manager.open_margin(player, "S01", leverage, margin)
        self.assertTrue(ok, msg)
        rows = await self.manager._rows(
            "SELECT * FROM invest_margin_positions WHERE user_id = ? AND status = 'open'",
            (uid,),
        )
        self.assertEqual(len(rows), 1)
        return player, rows[0]

    # ===== 纯函数口径 =====
    def test_liquidation_price_solves_the_equity_equation(self):
        """强平线是 ``value - borrow - interest - close_fee = margin × 20%`` 的解。"""
        position = {"shares": 100.0, "borrow": 200_000.0, "margin": 100_000.0}
        for interest in (0, 320, 5_000, 48_000):
            price = self.InvestManager._liquidation_price(position, interest, 100_000.0)
            value = position["shares"] * price
            close_fee = round(value * self.InvestManager.MARGIN_CLOSE_FEE_RATE)
            equity = value - position["borrow"] - interest - close_fee
            self.assertAlmostEqual(
                equity, 100_000.0 * self.InvestManager.MARGIN_LIQUIDATION_RATIO, delta=1.0,
                msg=f"interest={interest} 时强平线 {price} 与权益公式不自洽",
            )

    def test_liquidation_price_degenerate_inputs(self):
        self.assertEqual(
            self.InvestManager._liquidation_price({"shares": 0, "borrow": 100}, 0, 100), 0.0)
        self.assertEqual(self.InvestManager._liquidation_price({}, 0, 0), 0.0)

    def test_liquidation_price_includes_interest_and_close_fee(self):
        """旧公式 ``(borrow + margin×20%) / shares`` 漏算利息与平仓费，显示偏低。"""
        position = {"shares": 100.0, "borrow": 200_000.0, "margin": 100_000.0}
        legacy = (200_000.0 + 100_000.0 * self.InvestManager.MARGIN_LIQUIDATION_RATIO) / 100.0
        self.assertGreater(self.InvestManager._liquidation_price(position, 0, 100_000.0), legacy,
                           "平仓费未计入")
        self.assertGreater(self.InvestManager._liquidation_price(position, 5_000, 100_000.0),
                           self.InvestManager._liquidation_price(position, 0, 100_000.0),
                           "利息未计入")

    # ===== 真实持仓 =====
    async def test_displayed_liquidation_line_triggers_real_liquidation(self):
        """玩家按面板上的强平线设止损，就必须真的在那一刻被强平。"""
        player, position = await self._open_position()
        margin = float(position["margin"])
        target = margin * self.InvestManager.MARGIN_LIQUIDATION_RATIO
        with self._patch_tick(14):
            interest = self.InvestManager._margin_interest(position, 14)
            self.assertGreater(interest, 0, "用例前提：持有若干盘后必须产生利息")
            liq = self.InvestManager._liquidation_price(position, interest, margin)
            calc = await self.manager._margin_equity(position, liq, 14)
            self.assertAlmostEqual(calc["equity"], target, delta=1.0)
            # 强平线之上一点不该被平，之下就该被平
            above = await self.manager._margin_equity(position, liq * 1.05, 14)
            below = await self.manager._margin_equity(position, liq * 0.95, 14)
            self.assertGreater(above["equity"], target)
            self.assertLess(below["equity"], target)

            status = await self.manager.get_margin_status(player)
        match = re.search(r"强平线约 ([\d.]+)", status)
        self.assertIsNotNone(match, status)
        self.assertAlmostEqual(float(match.group(1)), round(liq, 2), places=2)

    async def test_liquidation_line_rises_with_interest(self):
        """利息随持有盘数增长，强平线必须同步上移。"""
        _player, position = await self._open_position(uid="margin_u2")
        margin = float(position["margin"])
        lines = {}
        for tick in (11, 20, 60):
            interest = self.InvestManager._margin_interest(position, tick)
            lines[tick] = self.InvestManager._liquidation_price(position, interest, margin)
        self.assertLess(lines[11], lines[20])
        self.assertLess(lines[20], lines[60])

    async def test_auto_liquidation_happens_at_displayed_line(self):
        """后台 ``liquidate_due`` 的触发价不能与面板显示的强平线脱节。"""
        player, position = await self._open_position(uid="margin_u3")
        margin = float(position["margin"])
        with self._patch_tick(14):
            interest = self.InvestManager._margin_interest(position, 14)
            liq = self.InvestManager._liquidation_price(position, interest, margin)
            await self.conn.execute(
                "UPDATE invest_products SET price = ? WHERE code = ?",
                (liq * 0.9, position["code"]),
            )
            await self.conn.commit()
            settled = await self.manager.liquidate_due()
        self.assertEqual(settled, 1)
        rows = await self.manager._rows(
            "SELECT * FROM invest_margin_positions WHERE user_id = ?", (player.user_id,)
        )
        self.assertEqual(rows[0]["status"], "liquidated")


if __name__ == "__main__":
    unittest.main()
