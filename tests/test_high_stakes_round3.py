"""v3.46.5 第三轮高风险玩法 debug 回归。

覆盖本轮修复：
- 盘口赌石「出货」口径与真实切石概率一致（否则是无限印钞漏洞）；
- 暗标免费描述只给品阶区间、付费鉴定的做局按档位反查；
- 古方残卷已掌握不再重复收费；
- 血炼魔化只烧旧词条；
- 黑市验囊「抢占 + 扣费」同事务；
- 赌局随机混入服务器密钥；
- 网页杠杆手输「平仓」的参数计划；
- 藏宝图进入忙碌白名单；
- 灵田盲种特殊灵植在材料目录里有价。
"""

import json
import re
import time
import unittest
from unittest import mock

try:
    import test_equipment_instances as fixtures
except ModuleNotFoundError:  # 从仓库根目录运行时走包内导入
    from tests import test_equipment_instances as fixtures


class _FakeRandom:
    """血炼用 `random.Random()`（不播种），测试里替换成可控实现。"""

    def __init__(self, roll=70.0):
        self._roll = roll

    def uniform(self, _a, _b):
        return self._roll

    def choice(self, seq):
        return seq[0]

    def random(self):
        return 0.5

    def randrange(self, n):
        return 0


class TestHighStakesRound3(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TestEquipmentInstances.asyncSetUp
    asyncTearDown = fixtures.TestEquipmentInstances.asyncTearDown
    _player = fixtures.TestEquipmentInstances._player
    _imp = staticmethod(fixtures._imp)

    async def _gold(self, user_id):
        fresh = await self.db.get_player_by_id(user_id)
        return int(fresh.gold or 0)

    def _manager(self, module, cls_name, *args):
        return getattr(self._imp(module), cls_name)(*args)

    # ===== 盘口赌石赔率 =====
    def test_stone_bet_odds_match_real_cut_probability(self):
        """盘口的「出货」概率必须等于切石里 1 - empty_chance。

        旧实现把 jewel+gear+egg 三个小概率相加当出货率（tier1 仅 6%），
        而真实出货率 92%，赔率却按 6% 定价 → 押「出货」期望收益 14 倍。
        """
        pool_mod = self._imp("managers.betting_pool_manager")
        gamble_mod = self._imp("managers.gamble_manager")
        tiers = pool_mod.STONE_TIERS
        configs = gamble_mod.GambleManager.STONE_TIER_CONFIG
        self.assertEqual(len(tiers), len(configs))
        for index, (tier, cfg) in enumerate(zip(tiers, configs), 1):
            self.assertEqual(tier["name"], cfg["name"])
            real_hit = 1 - float(cfg["empty_chance"])
            self.assertAlmostEqual(float(tier["p"]), real_hit, places=6)
            for side, p_bet in (("出货", real_hit), ("切垮", 1 - real_hit)):
                odds = max(pool_mod.MIN_STONE_ODDS,
                           round(pool_mod.HOUSE_RATE / p_bet, 2))
                self.assertGreaterEqual(odds, 1.0, f"tier{index} {side} 赔率不能低于 1")
                self.assertLess(
                    p_bet * odds, 1.0,
                    f"tier{index} 押「{side}」期望收益 {p_bet * odds:.3f} 必须小于 1（庄家抽水）",
                )

    async def test_stone_bet_payout_uses_round_not_truncate(self):
        """派彩用 int() 截断会因浮点误差少给 1 灵石。

        bet_stone 的输赢由当前秒数播种的 seeded_rng 决定（tier1 出货率 92%），
        不钉死就会有 8% 概率走「未命中」分支。旧守卫 `if "命中" in msg` 还会把
        `💀 未命中` 误判成命中——输的报文里没有「派彩 X 灵石」，
        `re.search(...).group(1)` 直接抛 AttributeError（全量回归偶发失败的来源）。
        这里注入可控 rng，赢/输两个分支都做确定性断言。
        """
        pool_mod = self._imp("managers.betting_pool_manager")
        p = await self._player("stone_bettor", gold=1_000_000)
        mgr = self._manager("managers.betting_pool_manager", "BettingPoolManager", self.db)
        original = pool_mod.seeded_rng

        class _WinRng:
            def random(self):
                return 0.0

        class _LoseRng:
            def random(self):
                return 0.999

        try:
            pool_mod.seeded_rng = lambda *a, **k: _WinRng()
            ok, msg = await mgr.bet_stone(p, 1, "出货", 300)
            self.assertTrue(ok, msg)
            odds_match = re.search(r"赔率：([\d.]+)", msg)
            self.assertIsNotNone(odds_match, msg)
            odds = float(odds_match.group(1))
            payout_match = re.search(r"派彩 ([\d,]+) 灵石", msg)
            self.assertIsNotNone(payout_match, f"命中报文必须带派彩金额：{msg}")
            payout = int(payout_match.group(1).replace(",", ""))
            self.assertEqual(payout, int(round(300 * odds)))
            gold_after_win = await self._gold(p.user_id)
            self.assertEqual(gold_after_win, 1_000_000 - 300 + payout)

            pool_mod.seeded_rng = lambda *a, **k: _LoseRng()
            ok, msg = await mgr.bet_stone(p, 1, "出货", 300)
            self.assertTrue(ok, msg)
            self.assertIn("💀 未命中", msg)
            self.assertNotIn("派彩", msg)
            self.assertEqual(await self._gold(p.user_id), gold_after_win - 300,
                             "未命中只能扣赌注，不能有任何派彩")
        finally:
            pool_mod.seeded_rng = original

    # ===== 暗标：免费区间 / 付费准确 =====
    def test_blind_auction_free_hint_gives_range_not_exact_rank(self):
        mgr = self._manager("managers.blind_auction_manager", "BlindAuctionManager",
                            self.db, self.ring, self.cm)
        names = self._imp("rank_rules").RANK_NAMES
        for rank in ("凡品", "黄品", "玄品", "圣品", "帝品", "仙品", "混元先天"):
            span = mgr._rank_range(rank)
            low, sep, high = span.partition("～")
            self.assertTrue(sep and high, f"{rank} 的免费描述必须是区间，实际：{span}")
            idx = names.index(rank)
            self.assertLessEqual(names.index(low), idx, f"{rank} 低于区间下界：{span}")
            self.assertGreaterEqual(names.index(high), idx, f"{rank} 高于区间上界：{span}")
            # 区间最多 ±1 名，不能宽到等于没给信息
            self.assertLessEqual(names.index(high) - names.index(low), 2)
        # 未知品阶原样返回，不能抛异常
        self.assertEqual(mgr._rank_range(""), "未知")

    async def test_blind_appraise_mislead_stays_one_tier_away(self):
        """做局必须落在相邻档位。

        旧写法 `RANK_NAMES[fake_tier - 1]` 把档位当列表下标，
        12 个品阶名共享 5 档，tier 4/5 会映射回「灵品/地品」（tier 3）。
        """
        rank_rules = self._imp("rank_rules")
        mgr = self._manager("managers.blind_auction_manager", "BlindAuctionManager",
                            self.db, self.ring, self.cm)
        rnd = await mgr._ensure_round()
        lots = rnd["lots"]
        self.assertTrue(lots)
        samples = 0
        for slot in range(len(lots)):
            for uid in range(6):
                p = await self._player(f"appraiser_{slot}_{uid}", gold=5_000_000)
                ok, msg = await mgr.appraise(p, int(lots[slot]["lot_no"]))
                if not ok:
                    continue
                match = re.search(r"鉴定为【(.+?)】", msg)
                self.assertIsNotNone(match, msg)
                reported = match.group(1)
                payload = json.loads(lots[slot]["payload"])
                real_tier = rank_rules.rank_tier(payload.get("rank"), 1)
                got_tier = rank_rules.rank_tier(reported, 1)
                self.assertLessEqual(
                    abs(got_tier - real_tier), 1,
                    f"鉴定把 {payload.get('rank')}(t{real_tier}) 说成 {reported}(t{got_tier})",
                )
                samples += 1
        self.assertGreater(samples, 0)

    # ===== 古方残卷 =====
    async def test_ancient_recipe_already_learned_is_not_charged(self):
        p = await self._player("ancient_known", gold=500_000)
        mgr = self._manager("managers.alchemy_manager", "AlchemyManager",
                            self.db, self.cm, self.ring)
        candidates = mgr._ancient_candidates(p.user_id)
        if not candidates:
            self.skipTest("当前配置没有可用的古方残卷")
        learned = sorted(set(await mgr.ensure_learned_recipes(p)) | {candidates[0]["recipe_id"]})
        p.learned_recipes = json.dumps(learned, ensure_ascii=False)
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        ok, msg = await mgr.study_ancient(p, 1)
        self.assertFalse(ok)
        self.assertIn("早已掌握", msg)
        self.assertEqual(await self._gold(p.user_id), 500_000)
        # 列表里要能看到「已掌握」标注
        _ok, listing = await mgr.get_ancient_recipes(p.user_id)
        self.assertIn("已掌握", listing)

    async def test_ancient_recipe_backlash_never_heals_a_downed_player(self):
        """hp=0 时 `player.hp or max_hp` 会退回满血，反噬反而变成治疗。"""
        p = await self._player("ancient_downed", gold=500_000)
        p.hp = 0
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(p.user_id)
        mgr = self._manager("managers.alchemy_manager", "AlchemyManager",
                            self.db, self.cm, self.ring)
        candidates = mgr._ancient_candidates(p.user_id)
        if not candidates:
            self.skipTest("当前配置没有可用的古方残卷")
        # 强制走「丹毒入体」分支
        mgr.ANCIENT_OUTCOMES = (("poison", 1, "丹毒入体"),)
        try:
            ok, msg = await mgr.study_ancient(p, 1)
        finally:
            del mgr.ANCIENT_OUTCOMES
        self.assertTrue(ok, msg)
        self.assertIn("气血枯竭", msg)
        fresh = await self.db.get_player_by_id(p.user_id)
        self.assertEqual(int(fresh.hp or 0), 0)

    # ===== 血炼 =====
    async def _equip_with_affixes(self, uid, affixes):
        p = await self._player(uid, gold=1_000_000)
        ok, msg = await self.ring.store_item(p, self.weapon, 1)
        self.assertTrue(ok, msg)
        inst = self.im.find_for_equip(p, self.weapon, 1)
        item = self.em.parse_item_from_name(
            self.weapon, self.cm.items_data, self.cm.weapons_data, inst
        )
        ok, msg = await self.em.equip_item(p, item, int(inst["id"]))
        self.assertTrue(ok, msg)
        p = await self.db.get_player_by_id(uid)
        instances = self.im.load(p)
        for index, row in enumerate(instances):
            if int(row.get("id", 0) or 0) == int(inst["id"]):
                instances[index] = dict(row, affixes=list(affixes))
                break
        self.im.save(p, instances)
        # im.save 只改内存对象，必须落库后重新读取才拿得到注入的词条
        await self.db.update_player(p)
        p = await self.db.get_player_by_id(uid)
        p.hp = 100_000  # 远高于血炼的 55% 气血门槛
        await self.db.update_player(p)
        return await self.db.get_player_by_id(uid), int(inst["id"])

    async def test_blood_forge_corrupt_never_burns_the_new_affix(self):
        forge_mod = self._imp("managers.forge_manager")
        p, inst_id = await self._equip_with_affixes("bf_corrupt", [
            {"key": "old_a", "name": "旧词条甲", "value": 1, "display": "旧词条甲+1"},
            {"key": "old_b", "name": "旧词条乙", "value": 2, "display": "旧词条乙+2"},
        ])
        mgr = forge_mod.ForgeManager(self.db, self.cm, self.ring)
        # roll=70 落在 corrupt 区间（great 10 / good 37 / backlash 65 / corrupt 90）
        with mock.patch.object(forge_mod.random, "Random", lambda *a, **k: _FakeRandom(70.0)):
            ok, msg = await mgr.blood_forge(p)
        self.assertTrue(ok, msg)
        self.assertIn("魔化", msg)
        self.assertIn("获得血炼词条", msg)
        # 烧掉的必须是旧词条，新词条要留着
        self.assertIn("原有的【旧词条甲+1】", msg)
        p = await self.db.get_player_by_id(p.user_id)
        inst = self.im.get_instance(p, inst_id)
        keys = [a.get("key") for a in (inst.get("affixes") or [])]
        self.assertNotIn("old_a", keys)
        self.assertIn("old_b", keys)
        self.assertIn("crit_rate", keys)

    async def test_blood_forge_corrupt_without_old_affix_keeps_new_one(self):
        forge_mod = self._imp("managers.forge_manager")
        p, inst_id = await self._equip_with_affixes("bf_corrupt_empty", [])
        mgr = forge_mod.ForgeManager(self.db, self.cm, self.ring)
        with mock.patch.object(forge_mod.random, "Random", lambda *a, **k: _FakeRandom(70.0)):
            ok, msg = await mgr.blood_forge(p)
        self.assertTrue(ok, msg)
        self.assertIn("没有旧词条可烧", msg)
        p = await self.db.get_player_by_id(p.user_id)
        inst = self.im.get_instance(p, inst_id)
        keys = [a.get("key") for a in (inst.get("affixes") or [])]
        self.assertEqual(keys, ["crit_rate"])

    # ===== 黑市锦囊 =====
    async def test_pouch_inspect_charges_only_once(self):
        p = await self._player("pouch_inspect", gold=1_000_000)
        mgr = self._manager("managers.gamble_manager", "GambleManager",
                            self.db, self.cm, self.ring)
        ok, msg = await mgr.inspect_pouch(p, 1)
        self.assertTrue(ok, msg)
        gold_after = await self._gold(p.user_id)
        self.assertLess(gold_after, 1_000_000)
        ok, msg = await mgr.inspect_pouch(p, 1)
        self.assertFalse(ok)
        self.assertIn("只能验一次", msg)
        self.assertEqual(await self._gold(p.user_id), gold_after)

    async def test_pouch_inspect_writes_state_even_if_repeated(self):
        """验囊记录与扣费同事务：标记必须落库，否则玩家会被重复收费。"""
        p = await self._player("pouch_state", gold=1_000_000)
        mgr = self._manager("managers.gamble_manager", "GambleManager",
                            self.db, self.cm, self.ring)
        ok, _msg = await mgr.inspect_pouch(p, 1)
        self.assertTrue(ok)
        candidates = mgr._get_pouch_candidates(p.user_id)
        period = candidates[0]["period"]
        async with self.db.conn.execute(
            "SELECT inspected, result FROM black_market_pouches "
            "WHERE user_id = ? AND period = ? AND slot = 1", (p.user_id, period)
        ) as cursor:
            row = await cursor.fetchone()
        self.assertIsNotNone(row, "验囊后必须留下记录")
        self.assertEqual(int(row[0]), 1)
        self.assertTrue(str(row[1]))

    # ===== 材料目录 =====
    def test_blind_plant_special_herbs_are_priced(self):
        farm_mod = self._imp("managers.spirit_farm_manager")
        for entry in farm_mod.BLIND_SPECIAL_HERBS:
            name = entry["name"]
            info = self.cm.material_catalog.get(name)
            self.assertIsNotNone(info, f"{name} 缺少 material_catalog 条目，回收价会跌成凡品")
            self.assertGreater(int(info.get("price") or 0), 0)
            self.assertEqual(self.cm.get_item_price(name), int(info["price"]))

    # ===== 随机密钥 =====
    def test_seeded_rng_mixes_server_secret(self):
        secret_mod = self._imp("utils.game_secret")
        secret_mod.reset_game_secret("")
        self.addCleanup(secret_mod.reset_game_secret, "")
        legacy = [secret_mod.seeded_rng("pool-result", "duel:1:1").random() for _ in range(3)]
        secret_mod.reset_game_secret("unit-test-secret")
        keyed = [secret_mod.seeded_rng("pool-result", "duel:1:1").random() for _ in range(3)]
        # 没有密钥时退回旧行为（可复现），有密钥时结果完全不同
        self.assertEqual(len(set(legacy)), 1)
        self.assertEqual(len(set(keyed)), 1)
        self.assertNotEqual(legacy[0], keyed[0])
        secret_mod.reset_game_secret("another-secret")
        self.assertNotEqual(keyed[0], secret_mod.seeded_rng("pool-result", "duel:1:1").random())

    def test_high_stakes_random_uses_seeded_rng(self):
        """所有赌局随机入口都必须走 seeded_rng，不能再出现公开可推算的 random.Random。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        targets = (
            "managers/betting_pool_manager.py",
            "managers/blind_auction_manager.py",
            "managers/mine_vein_manager.py",
            "managers/treasure_map_manager.py",
        )
        for rel in targets:
            text = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn("random.Random(", text, f"{rel} 仍在用可推算的种子")
            self.assertIn("seeded_rng", text, f"{rel} 没有接入服务器密钥")

    # ===== 网页参数计划 =====
    def test_web_margin_plan_handles_typed_close_keyword(self):
        from webchat_room.arg_options import plan_for

        # 弹层整值：「杠杆 平仓」
        picked = plan_for("仙市", ["杠杆 平仓"])
        self.assertEqual([f.get("key") for f in picked], ["sub", "arg1"])
        self.assertEqual(picked[1].get("source"), "margin_arg1")
        # 手输：先选「杠杆开仓」，再在下一栏打「平仓」
        typed = plan_for("仙市", ["杠杆", "平仓"])
        self.assertEqual([f.get("key") for f in typed], ["sub", "kw", "arg1"])
        self.assertEqual(typed[2].get("source"), "margin_arg1")
        # 手输「状态」不应再要求补持仓编号
        self.assertEqual([f.get("key") for f in plan_for("仙市", ["杠杆", "状态"])], ["sub", "kw"])
        # 正常开仓分支不受影响
        opened = plan_for("仙市", ["杠杆"])
        self.assertEqual([f.get("key") for f in opened], ["sub", "arg1", "arg2", "arg3"])

    # ===== 忙碌白名单 =====
    def test_treasure_map_is_allowed_while_busy(self):
        handlers_utils = self._imp("handlers.utils")
        allowed = handlers_utils.BUSY_STATE_ALLOWED_COMMANDS
        check = handlers_utils._is_command_allowed
        for text in ("藏宝图", "藏宝图 撤退", "藏宝图 继续", "藏宝图 开始 3"):
            self.assertTrue(check(text, allowed), f"{text} 应在忙碌白名单里")
        # 放行「开始」不会绕过限制：start() 自己会校验 first_busy_cd
        self.assertNotIn("藏宝图 开始", handlers_utils.BUSY_BYPASS_COMMANDS)

    # ===== 指令参数 =====
    def test_greedy_commands_keep_full_args(self):
        """`/仙市 杠杆 S01 3 50000`、`/飞升 青锋剑 确认` 必须整段收到。

        AstrBot 只把「无默认值」的参数当 GreedyStr，带默认值就只给第一个分段。
        """
        import ast
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        wanted = {"handle_invest_market", "handle_reincarnate", "handle_treasure_map",
                  "handle_mine_vein", "handle_betting_pool", "handle_auction_house_status"}
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in wanted:
                last = node.args.args[-1]
                annotation = last.annotation
                name = getattr(annotation, "id", None) or getattr(annotation, "attr", None)
                found[node.name] = (name, last.arg)
        self.assertEqual(set(found), wanted)
        for name, (annotation, arg_name) in found.items():
            self.assertEqual(annotation, "GreedyStr", f"{name}.{arg_name} 必须是 GreedyStr")

    def test_greedy_params_have_no_default(self):
        import ast
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            positional = list(node.args.args)
            defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
            for arg, default in zip(positional, defaults):
                annotation = arg.annotation
                name = getattr(annotation, "id", None) or getattr(annotation, "attr", None)
                if name == "GreedyStr":
                    self.assertIsNone(default, f"{node.name}.{arg.arg} 的 GreedyStr 不能带默认值")

    # ===== 矿脉持有上限 =====
    async def test_mine_buy_enforces_active_limit(self):
        p = await self._player("mine_limit", gold=10_000_000)
        mgr = self._manager("managers.mine_vein_manager", "MineVeinManager",
                            self.db, self.ring, self.cm)
        now = int(time.time())
        for index in range(2):
            await self.db.conn.execute(
                "INSERT INTO mine_veins "
                "(owner_id, vein_key, name, tier, buy_price, base_yield, richness, "
                "created_at, expire_at, owner_claimed_hour, status) "
                "VALUES (?, ?, ?, 1, 0, 1000, 100, ?, ?, ?, 'active')",
                (p.user_id, f"held:{index}", f"占位矿脉{index}", now, now + 3600,
                 mgr._hour_key(now)),
            )
        await self.db.conn.commit()
        ok, msg = await mgr.buy(p, 1)
        self.assertFalse(ok)
        self.assertIn("最多同时持有", msg)
        self.assertEqual(await self._gold(p.user_id), 10_000_000)


if __name__ == "__main__":
    unittest.main()
