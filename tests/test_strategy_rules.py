# tests/test_strategy_rules.py
# 「仙域争霸」规则引擎（core/strategy_rules.py）确定性单测：
# 克制环 / 适性 / 精锐 / 伤势体力 / 任务结算 / 战斗模拟确定性 / 围城标定 / NPC 评分
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub  # noqa: E402

_install_astrbot_stub()

R = __import__(f"{PKG}.core.strategy_rules", fromlist=["x"])


def _mk_army(army_id=1, troop="sword_shield", troops=1000, morale=100, veteran=0,
             leader_stats=None, profession="sword", siege_engine=False):
    leader = None
    if leader_stats is not None:
        leader = {"id": 999, "name": "测试主将", "stats": leader_stats,
                  "profession": profession, "traits": []}
    return {"id": army_id, "faction_id": 1, "troop_type": troop, "troops": troops,
            "morale": morale, "veteran": veteran, "leader": leader,
            "siege_engine": siege_engine}


class TestCounterAndAptitude(unittest.TestCase):
    def test_counter_ring(self):
        # 克制环（§7.5）：飞剑→法修 ×1.3、法修→兽群 ×1.3、兽群→重甲 ×1.3、
        # 重甲→剑盾 ×1.3、剑盾→飞剑 ×1.3；飞剑对重甲短板 ×0.8
        self.assertAlmostEqual(R.counter_factor("flying_sword", "spell_corps"), 1.3)
        self.assertAlmostEqual(R.counter_factor("spell_corps", "beast_horde"), 1.3)
        self.assertAlmostEqual(R.counter_factor("beast_horde", "heavy_armor"), 1.3)
        self.assertAlmostEqual(R.counter_factor("heavy_armor", "sword_shield"), 1.3)
        self.assertAlmostEqual(R.counter_factor("sword_shield", "flying_sword"), 1.3)
        self.assertAlmostEqual(R.counter_factor("flying_sword", "heavy_armor"), 0.8)
        self.assertAlmostEqual(R.counter_factor("flying_sword", "supply"), 1.5)
        # 无克制关系 = 1.0
        self.assertAlmostEqual(R.counter_factor("sword_shield", "spell_corps"), 1.0)

    def test_profession_s_aptitude(self):
        # 职业绑定 S 适性：剑修→飞剑、体修→重甲、灵修→法修、魔修/御兽→兽群、佛修→剑盾
        for prof, troop in R.PROFESSION_S_APTITUDE.items():
            char = {"profession": prof, "rarity": "rare", "aptitude": {}}
            apt = R.roll_aptitude(random.Random(1), prof, "rare")
            self.assertEqual(apt[troop], "S")

    def test_aptitude_bonus_order(self):
        s = {"aptitude": {"flying_sword": "S"}}
        a = {"aptitude": {"flying_sword": "A"}}
        b = {"aptitude": {"flying_sword": "B"}}
        self.assertGreater(R.aptitude_bonus(s, "flying_sword"),
                           R.aptitude_bonus(a, "flying_sword"))
        self.assertGreater(R.aptitude_bonus(a, "flying_sword"),
                           R.aptitude_bonus(b, "flying_sword"))

    def test_veteran_tier(self):
        # 精锐值分档单调不减
        prev = -1.0
        for v in (0, 20, 40, 60, 80, 100):
            _name, bonus, _rec = R.veteran_tier(v)
            self.assertGreaterEqual(bonus, prev)
            prev = bonus


class TestCharacterSystem(unittest.TestCase):
    def test_generate_character_fields(self):
        rng = random.Random(42)
        c = R.generate_character(rng, rarity="rare", role="general")
        for k in ("name", "role", "rarity", "profession", "stats", "traits",
                  "aptitude", "level", "loyalty", "injury", "stamina", "status"):
            self.assertIn(k, c)
        total = sum(c["stats"].values())
        lo, hi = R.RARITY_STAT_RANGE["rare"]
        self.assertTrue(lo <= total <= hi, f"珍品六维总和 {total} 应在 [{lo},{hi}]")

    def test_generate_deterministic(self):
        c1 = R.generate_character(random.Random(7), rarity="epic")
        c2 = R.generate_character(random.Random(7), rarity="epic")
        self.assertEqual(c1["stats"], c2["stats"])
        self.assertEqual(c1["name"], c2["name"])

    def test_epic_has_traits(self):
        c = R.generate_character(random.Random(3), rarity="epic")
        self.assertTrue(1 <= len(c["traits"]) <= 2)
        c2 = R.generate_character(random.Random(3), rarity="legend")
        self.assertTrue(1 <= len(c2["traits"]) <= 2)

    def test_injury_states(self):
        self.assertEqual(R.injury_state(0), "healthy")
        self.assertEqual(R.injury_state(30), "light")
        self.assertEqual(R.injury_state(60), "heavy")
        self.assertEqual(R.injury_state(70), "dying")
        self.assertEqual(R.injury_state(100), "dead")

    def test_dying_then_any_injury_kills(self):
        c = {"injury": 70, "status": "idle"}
        dead = R.apply_injury(c, 5)
        self.assertTrue(dead)
        self.assertEqual(R.injury_state(c["injury"]), "dead")

    def test_injury_stat_penalty(self):
        c = {"stats": {"valor": 100}, "injury": 0}
        self.assertEqual(R.effective_stats(c)["valor"], 100)
        c["injury"] = 30  # 轻伤 -10%
        self.assertEqual(R.effective_stats(c)["valor"], 90)
        c["injury"] = 60  # 重伤 -25%
        self.assertEqual(R.effective_stats(c)["valor"], 75)

    def test_stamina_spend_and_recover(self):
        c = {"stamina": 25}
        ok = R.spend_stamina(c, "task")  # task 消耗 20
        self.assertTrue(ok)
        self.assertEqual(c["stamina"], 5)
        R.recover_stamina(c, 10)
        self.assertEqual(c["stamina"], 15)
        c2 = {"stamina": 10}
        self.assertFalse(R.spend_stamina(c2, "stratagem"))  # 需 30

    def test_troop_cap_by_position(self):
        self.assertEqual(R.troop_cap("leader"), 3000)
        self.assertEqual(R.troop_cap("taiwei"), 3000)
        self.assertEqual(R.troop_cap("situ"), 2500)
        self.assertEqual(R.troop_cap("zhonglang"), 2000)
        self.assertEqual(R.troop_cap("taishou"), 2000)
        self.assertEqual(R.troop_cap("taishou", is_taishou_outside=True), 1500)
        self.assertEqual(R.troop_cap(""), 1500)

    def test_position_unlock_by_node_count(self):
        # 角色官职随据点数量逐步开放（§4.5）
        self.assertEqual(R.available_positions(0), {})
        self.assertEqual(R.available_positions(1), {"zhonglang": 1})
        self.assertEqual(R.position_unlock_nodes("zhonglang"), 1)
        self.assertEqual(R.position_unlock_nodes("jijiu"), 2)
        self.assertEqual(R.position_unlock_nodes("situ"), 3)
        self.assertEqual(R.position_unlock_nodes("taiwei"), 4)
        at6 = R.available_positions(6)
        self.assertEqual(at6["zhonglang"], 2)
        self.assertEqual(at6["jijiu"], 2)
        at8 = R.available_positions(8)
        self.assertIn("taishou", at8)
        self.assertEqual(at8["taishou"], 8)  # 每据点 1 名
        at9 = R.available_positions(9)
        self.assertEqual(at9["taishou"], 9)

    def test_wanderer_auction_pricing(self):
        acfg = {
            "start_price": {"epic": 20000, "legend": 60000},
            "quality_floor": {"epic": 300, "legend": 360},
            "quality_step": {"epic": 250, "legend": 400},
            "trait_bonus": 2000,
            "min_increment_abs": 200,
            "min_increment_pct": 0.05,
        }
        epic = {"rarity": "epic",
                "stats": {k: 55 for k in R.STAT_KEYS}, "traits": []}  # 六维 330
        self.assertEqual(R.wanderer_auction_price(epic, acfg),
                         20000 + (330 - 300) * 250)
        epic["traits"] = ["骁勇"]
        self.assertEqual(R.wanderer_auction_price(epic, acfg),
                         20000 + (330 - 300) * 250 + 2000)
        self.assertEqual(R.wanderer_min_bid(27500, 0, False, acfg), 27500)
        self.assertEqual(R.wanderer_min_bid(27500, own_bid=27500, acfg=acfg), 28875)
        # 低稀有度流亡角色按低档兜底，不被按极品底价起拍
        common = {"rarity": "common",
                  "stats": {k: 25 for k in R.STAT_KEYS}, "traits": []}
        self.assertLessEqual(R.wanderer_auction_price(common, acfg), 2000)

    def test_wanderer_quota_and_stat_cap(self):
        acfg = {"season_quota": {"common": 5, "fine": 4, "rare": 3,
                                 "epic": 2, "legend": 1}}
        self.assertEqual(R.wanderer_quota_limit(acfg, "common"), 5)
        self.assertEqual(R.wanderer_quota_limit(acfg, "legend"), 1)
        self.assertEqual(R.wanderer_quota_limit({}, "epic"), 3)
        # 生成与成长的单项属性都不应出现 100
        for rarity in R.RARITIES:
            for seed in range(20):
                c = R.generate_character(random.Random(seed), rarity=rarity)
                self.assertLessEqual(max(c["stats"].values()), R.STAT_HARD_CAP)
        c = {"exp": 0, "level": 1, "rarity": "legend", "role": "general",
             "stats": {k: 5 for k in R.STAT_KEYS}}
        c["stats"]["valor"] = 89
        R.gain_exp(c, 10_000_000, 30)
        self.assertLessEqual(max(c["stats"].values()), R.STAT_HARD_CAP)
        self.assertEqual(c["stats"]["valor"], R.STAT_HARD_CAP)

    def test_overstaffed(self):
        leader = {"stats": {"leadership": 50}}  # 50×20 = 1000
        self.assertFalse(R.is_overstaffed(leader, 1000))
        self.assertTrue(R.is_overstaffed(leader, 1001))

    def test_gain_exp_levelup(self):
        c = {"exp": 0, "level": 1, "rarity": "common", "role": "general",
             "stats": {k: 20 for k in R.STAT_KEYS}}
        logs = R.gain_exp(c, 10_000)
        self.assertGreater(c["level"], 1)
        self.assertTrue(logs)
        total = sum(c["stats"].values())
        self.assertLessEqual(total, R.RARITY_STAT_RANGE["common"][1])

    def test_gain_exp_can_exceed_rarity_with_growth_bonus(self):
        # 满成长后普通角色仍可继续历练 30 点，鼓励长期养成而非反复替换
        c = {"exp": 0, "level": 1, "rarity": "common", "role": "general",
             "stats": {k: 15 for k in R.STAT_KEYS}}  # 90，低于凡品生成上限
        base_cap = R.RARITY_STAT_RANGE["common"][1]
        R.gain_exp(c, 100_000, 30)
        self.assertEqual(R.stat_total(c), base_cap + 30)
        # 达到历练上限后继续加阅历不会突破
        R.gain_exp(c, 10_000, 30)
        self.assertEqual(R.stat_total(c), base_cap + 30)
        self.assertEqual(R.stat_growth_cap(c, 30), base_cap + 30)

    def test_gain_exp_respects_max_level(self):
        c = {"exp": 0, "level": 1, "rarity": "legend", "role": "general",
             "stats": {k: 60 for k in R.STAT_KEYS}}
        R.gain_exp(c, 10_000_000, 30)
        self.assertLessEqual(c["level"], R.MAX_LEVEL)
        self.assertLess(c["exp"], R.exp_to_next(R.MAX_LEVEL))


class TestTaskSettle(unittest.TestCase):
    def test_settle_task_formula(self):
        rng = random.Random(1)
        c = {"role": R.ROLE_OFFICIAL, "stats": {"admin": 100}, "traits": ["farming"],
             "injury": 0}
        v = R.settle_task(rng, "farm", c, base_per_tick=100, ticks=4,
                          node_type="grain", node_morale=100)
        # 100 × (1+100/200) × 1.3(内政官屯田) × 1.25(屯垦) × 2.0(谷田) × 1.0(民心)
        # × [0.85,1.15] × 4tick = 1950 ± 15%
        self.assertTrue(1950 * 0.84 <= v <= 1950 * 1.16, v)

    def test_estimate_task_without_random_roll(self):
        """面板预期收益 = 结算公式去掉 ±15% 随机波动。"""
        c = {"role": R.ROLE_OFFICIAL, "stats": {"admin": 100}, "traits": ["farming"],
             "injury": 0}
        est = R.estimate_task("farm", c, base_per_tick=100, ticks=4,
                              node_type="grain", node_morale=100)
        self.assertAlmostEqual(est, 1950.0, places=6)
        for seed in range(20):
            v = R.settle_task(random.Random(seed), "farm", c, base_per_tick=100,
                              ticks=4, node_type="grain", node_morale=100)
            self.assertTrue(est * 0.85 <= v <= est * 1.15, (seed, v))

    def test_task_node_mult(self):
        self.assertEqual(R.task_node_mult("govern", "city"), 1.5)
        self.assertEqual(R.task_node_mult("farm", "grain"), 2.0)
        self.assertEqual(R.task_node_mult("farm", "city"), 1.0)
        # 采矿：灵脉矿 ×2.0，关隘/遗迹 ×1.3，其他 ×1.0
        self.assertEqual(R.task_node_mult("mining", "mine"), 2.0)
        self.assertEqual(R.task_node_mult("mining", "pass"), 1.3)
        self.assertEqual(R.task_node_mult("mining", "ruin"), 1.3)
        self.assertEqual(R.task_node_mult("mining", "city"), 1.0)

    def test_mining_task_settle(self):
        """采矿：武勇主属性 + 武将适配 1.3，非矿据点也能稳定出铁。"""
        c = {"role": R.ROLE_GENERAL, "stats": {"valor": 100}, "traits": [],
             "injury": 0}
        # 20 × (1+100/200) × 1.3 × 1.0(普通据点) × 1.0(民心) × 32tick
        v_city = R.settle_task(random.Random(1), "mining", c, base_per_tick=20,
                               ticks=32, node_type="city", node_morale=100)
        self.assertTrue(1248 * 0.84 <= v_city <= 1248 * 1.16, v_city)
        # 灵脉矿系数 ×2 → 约为普通据点两倍
        v_mine = R.settle_task(random.Random(1), "mining", c, base_per_tick=20,
                               ticks=32, node_type="mine", node_morale=100)
        self.assertTrue(2496 * 0.84 <= v_mine <= 2496 * 1.16, v_mine)
        self.assertGreater(v_mine, v_city * 1.7)

    def test_role_fit(self):
        off = {"role": R.ROLE_OFFICIAL}
        gen = {"role": R.ROLE_GENERAL}
        self.assertEqual(R.role_fit_mult(off, "farm"), 1.3)
        self.assertEqual(R.role_fit_mult(gen, "farm"), 1.0)


class TestHexUtils(unittest.TestCase):
    def test_oddq_cube_roundtrip_distance(self):
        self.assertEqual(R.hex_distance((0, 0), (0, 0)), 0)
        self.assertEqual(R.hex_distance((0, 0), (1, 0)), 1)
        self.assertEqual(R.hex_distance((0, 0), (2, 0)), 2)
        self.assertEqual(R.hex_distance((0, 0), (0, 2)), 2)

    def test_hex_neighbors_six(self):
        for q in range(-2, 3):
            for r in range(-2, 3):
                self.assertEqual(len(set(R.hex_neighbors(q, r))), 6)

    def test_zoc_is_neighbors(self):
        self.assertEqual(R.zoc_tiles((1, 1)), set(R.hex_neighbors(1, 1)))

    def test_flank_factor(self):
        self.assertEqual(R.flank_factor(1), 1.0)
        self.assertAlmostEqual(R.flank_factor(2), R.FLANK_FACTOR)
        self.assertAlmostEqual(R.flank_factor(3), R.ENCIRCLE_FACTOR)
        self.assertAlmostEqual(R.flank_factor(5), R.ENCIRCLE_FACTOR)

    def test_is_night(self):
        for h in (22, 23, 0, 5):
            self.assertTrue(R.is_night(h))
        for h in (6, 12, 21):
            self.assertFalse(R.is_night(h))


class TestBattleSimulation(unittest.TestCase):
    def test_bigger_army_usually_wins(self):
        wins = 0
        for seed in range(20):
            rng = random.Random(seed)
            big = _mk_army(1, troops=3000, leader_stats={"valor": 60, "leadership": 60})
            small = _mk_army(2, troops=500, leader_stats={"valor": 60, "leadership": 60})
            res = R.simulate_battle([big], [small], rng)
            if res["winner"] == "attacker":
                wins += 1
        self.assertGreaterEqual(wins, 18)

    def test_deterministic_same_seed(self):
        def run(seed):
            rng = random.Random(seed)
            a = _mk_army(1, troop="flying_sword", troops=1200,
                         leader_stats={"valor": 80, "leadership": 60})
            d = _mk_army(2, troop="spell_corps", troops=1200,
                         leader_stats={"valor": 60, "leadership": 70})
            return R.simulate_battle([a], [d], rng)
        r1, r2 = run(123), run(123)
        self.assertEqual(r1["winner"], r2["winner"])
        self.assertEqual(r1["atk_losses"], r2["atk_losses"])
        self.assertEqual(r1["dfn_losses"], r2["dfn_losses"])
        self.assertEqual(r1["rounds"], r2["rounds"])

    def test_counter_advantage_helps(self):
        # 剑盾步（克飞剑）vs 飞剑队：同兵力同主将时，平局判守方胜是设计行为
        # （§7.3 攻方未能击溃 → 守方守住），克制优势体现在残兵差上。
        def run(atk_troop, def_troop, seed):
            rng = random.Random(seed)
            a = _mk_army(1, troop=atk_troop, troops=1000,
                         leader_stats={"valor": 60, "leadership": 60})
            d = _mk_army(2, troop=def_troop, troops=1000,
                         leader_stats={"valor": 60, "leadership": 60})
            res = R.simulate_battle([a], [d], rng)
            return (res["attackers"][0]["troops_after"]
                    - res["defenders"][0]["troops_after"])
        adv = sum(run("sword_shield", "flying_sword", s) for s in range(30))
        dis = sum(run("flying_sword", "sword_shield", s) for s in range(30))
        self.assertGreater(adv, dis)

    def test_counter_advantage_with_edge_wins(self):
        # 带 30% 兵力优势时，克制方应几乎必胜
        wins = 0
        for s in range(20):
            rng = random.Random(s)
            a = _mk_army(1, troop="sword_shield", troops=1300,
                         leader_stats={"valor": 60, "leadership": 60})
            d = _mk_army(2, troop="flying_sword", troops=1000,
                         leader_stats={"valor": 60, "leadership": 60})
            if R.simulate_battle([a], [d], rng)["winner"] == "attacker":
                wins += 1
        self.assertGreaterEqual(wins, 16)

    def test_casualty_soft_cap(self):
        # 单轮伤亡 ≤ 30% 守方兵力
        dmg = R.damage_curve(10 ** 9, 1.0, 1000)
        self.assertLessEqual(dmg, 300)

    def test_damage_curve_ratio_monotonic(self):
        d1 = R.damage_curve(100, 100, 1000)
        d2 = R.damage_curve(200, 100, 1000)
        self.assertGreater(d2, d1)

    def test_defending_node_bonus(self):
        a = _mk_army(1, troops=1000, leader_stats={"leadership": 70})
        base = R.defense_power(a, defending_node=False)
        a["wall_mult"] = 1.3
        boosted = R.defense_power(a, defending_node=True)
        self.assertGreater(boosted, base)

    def test_report_render(self):
        rng = random.Random(9)
        a = _mk_army(1, troops=1500, leader_stats={"valor": 70, "leadership": 60})
        d = _mk_army(2, troops=800, leader_stats={"valor": 50, "leadership": 50})
        res = R.simulate_battle([a], [d], rng)
        text = R.render_battle_report(res, title="测试会战")
        self.assertIn("测试会战", text)
        self.assertIn("结果", text)
        self.assertLessEqual(len(text.encode("utf-8")), 50 * 1024)


class TestSiegeCalibration(unittest.TestCase):
    def test_1500_with_engine_breaks_lv1_city_in_about_4_ticks(self):
        # §15 标定：1500 兵 + 破城槌 ≈ 4 tick 破 1 级城（城防 1000）
        army = _mk_army(1, troops=1500, siege_engine=True,
                       leader_stats={"valor": 40})
        dmg = R.siege_tick_damage([army])
        ticks = 1000 / dmg
        self.assertTrue(3 <= ticks <= 5, f"破城 tick={ticks:.2f} 应在 3~5")

    def test_city_defense_max(self):
        self.assertEqual(R.city_defense_max(1), 1000)
        self.assertEqual(R.city_defense_max(3), 3000)

    def test_counter_damage_split(self):
        shares = R.siege_counter_damage(garrison_troops=2000, towers=2, attacker_count=2)
        self.assertEqual(len(shares), 2)
        # (2000/100 + 2×20) × 0.6 = 36 → 每军分摊 18
        self.assertEqual(shares[0], 18)

    def test_self_repair_cap_70pct(self):
        rep = R.city_self_repair(690, 1)
        self.assertLessEqual(rep, 700)
        self.assertEqual(R.city_self_repair(700, 1), 700)


class TestMarchAndSupply(unittest.TestCase):
    def test_terrain_slows_march(self):
        a = _mk_army(1, troop="sword_shield")
        self.assertGreater(R.march_speed(a, "plain"), R.march_speed(a, "forest"))
        self.assertGreater(R.march_speed(a, "forest"), R.march_speed(a, "river"))

    def test_flying_sword_faster_on_plain(self):
        fly = _mk_army(1, troop="flying_sword")
        walk = _mk_army(2, troop="heavy_armor")
        self.assertGreater(R.march_speed(fly, "plain"), R.march_speed(walk, "plain"))

    def test_grain_upkeep_far_multiplier(self):
        a = _mk_army(1, troops=1000)
        near = R.grain_upkeep(a, "plain", far_from_home=False)
        far = R.grain_upkeep(a, "plain", far_from_home=True)
        self.assertGreater(far, near)

    def test_carry_grain_cap(self):
        self.assertEqual(R.carry_grain_cap({"stats": {"leadership": 60}}), 3000)
        self.assertEqual(R.carry_grain_cap(None), 0)


class TestNpcUtility(unittest.TestCase):
    def test_aggressive_faction_scores_higher(self):
        node = {"type": "city", "defense": 1000}
        calm = {"personality": {"aggression": 10, "econ_weight": 1.0}}
        war = {"personality": {"aggression": 95, "econ_weight": 1.0}}
        u_calm = R.npc_utility(node, calm, own_power=3000, garrison_power=800,
                               path_edges=1)
        u_war = R.npc_utility(node, war, own_power=3000, garrison_power=800,
                              path_edges=1)
        self.assertGreater(u_war, u_calm)

    def test_distance_reduces_utility(self):
        node = {"type": "grain", "defense": 0}
        f = {"personality": {"aggression": 60, "econ_weight": 1.0}}
        near = R.npc_utility(node, f, 3000, 500, path_edges=1)
        far = R.npc_utility(node, f, 3000, 500, path_edges=5)
        self.assertGreater(near, far)

    def test_pass_and_capital_strategic_bonus(self):
        f = {"personality": {"aggression": 60, "econ_weight": 1.0}}
        plain = R.npc_utility({"type": "ruin", "defense": 0}, f, 3000, 300, 1)
        cap = R.npc_utility({"type": "ruin", "defense": 0}, f, 3000, 300, 1,
                            adjacent_to_enemy_core=True)
        self.assertGreater(cap, plain)


class TestCaptiveDuelBond(unittest.TestCase):
    def test_capture_rates_order(self):
        rates = {}
        for kind in ("duel", "city_fall", "wipeout"):
            rng = random.Random(20260)
            hits = sum(R.capture_check(kind, rng) for _ in range(4000))
            rates[kind] = hits / 4000
        self.assertAlmostEqual(rates["duel"], 0.60, delta=0.03)
        self.assertAlmostEqual(rates["city_fall"], 0.40, delta=0.03)
        self.assertAlmostEqual(rates["wipeout"], 0.20, delta=0.03)

    def test_persuade_rate_bounds(self):
        r = R.persuade_rate(50, {"stats": {"charm": 80, "wisdom": 80}}, reputation=0)
        self.assertTrue(0.05 <= r <= 0.90)
        self.assertEqual(R.persuade_rate(0, None, disloyal=True), 0.80)
        low = R.persuade_rate(95, None, reputation=-60)
        self.assertEqual(low, 0.05)

    def test_duel_requires_general(self):
        gen = {"role": R.ROLE_GENERAL, "injury": 0, "stats": {"valor": 80}}
        off = {"role": R.ROLE_OFFICIAL, "injury": 0, "stats": {"valor": 90}}
        hurt = {"role": R.ROLE_GENERAL, "injury": 35, "stats": {"valor": 80}}
        self.assertTrue(R.can_challenge_duel(gen))
        self.assertFalse(R.can_challenge_duel(off))
        self.assertFalse(R.can_challenge_duel(hurt))

    def test_duel_best_of_three(self):
        a = {"id": 1, "role": R.ROLE_GENERAL, "injury": 0,
             "stats": {"valor": 95, "luck": 60}, "profession": "sword"}
        b = {"id": 2, "role": R.ROLE_GENERAL, "injury": 0,
             "stats": {"valor": 40, "luck": 40}, "profession": "body"}
        wins = sum(R.duel(a, b, random.Random(s))["winner"] == 1 for s in range(50))
        self.assertGreaterEqual(wins, 45)

    def test_bond_morale(self):
        a = {"id": 1, "bonds": {"2": {"type": "yijiao", "level": 1}}}
        b = {"id": 2, "bonds": {}}
        self.assertEqual(R.bond_morale_bonus(a, b), 8)
        a["bonds"]["2"]["type"] = "tongdao"
        self.assertEqual(R.bond_morale_bonus(a, b), 3)

    def test_merit_and_veteran(self):
        self.assertEqual(R.merit_from_battle(1000, winner=True), 150)
        self.assertEqual(R.merit_from_battle(1000, winner=False), 50)
        self.assertEqual(R.merit_from_battle(0, True, manual_win=True), 150)
        self.assertEqual(R.veteran_gain(True, siege=True), 5)


class TestNpcFavor(unittest.TestCase):
    """NPC 好感度：档位 / 门槛 / 赠礼换算 / 性格预置 / 结盟加成。"""

    def test_favor_tiers_and_gate(self):
        self.assertEqual(R.favor_of(None, 3), 0)
        self.assertEqual(R.favor_of({"3": {"score": 45}}, 3), 45)
        self.assertEqual(R.favor_tier(45), "friendly")
        self.assertEqual(R.favor_name(45), "友善")
        self.assertEqual(R.favor_tier(-70), "hostile")
        self.assertEqual(R.favor_tier(90), "trusted")
        # 档位边界
        self.assertEqual(R.favor_tier(19), "neutral")
        self.assertEqual(R.favor_tier(20), "friendly")
        self.assertEqual(R.favor_tier(-20), "cold")
        self.assertEqual(R.favor_tier(-60), "hostile")
        # 门槛：结盟 60 / 停战 -20 / 和谈 -40 / 宣战无门槛
        cfg = {"ally_min": 60, "truce_min": -20, "peace_min": -40}
        self.assertFalse(R.favor_gate_ok("ally", 59, cfg))
        self.assertTrue(R.favor_gate_ok("ally", 60, cfg))
        self.assertFalse(R.favor_gate_ok("truce", -25, cfg))
        self.assertTrue(R.favor_gate_ok("peace", -35, cfg))
        self.assertTrue(R.favor_gate_ok("war", -100, cfg))
        self.assertEqual(R.favor_gate("ally", 0, cfg), 60)
        self.assertIsNone(R.favor_gate("war", 0, cfg))

    def test_favor_gift_points(self):
        cfg = {"gift_coin_per_point": 800, "gift_max_points": 30}
        self.assertEqual(R.favor_gift_points(800, cfg), 1)
        self.assertEqual(R.favor_gift_points(8000, cfg), 10)
        self.assertEqual(R.favor_gift_points(100_000, cfg), 30)  # 单次封顶
        self.assertEqual(R.favor_gift_points(100, cfg), 0)

    def test_favor_aggression(self):
        cfg = {"npc_peace_favor": 50}
        self.assertEqual(R.favor_aggression_mult(60, cfg), 0.0)
        self.assertGreater(R.favor_aggression_mult(-80, cfg), 1.0)
        self.assertLess(R.favor_aggression_mult(30, cfg), 1.0)

    def test_npc_seed_relation(self):
        a = {"aggression": 10, "key": "hermit"}
        b = {"aggression": 20, "key": "developer"}
        rels = set()
        scores = []
        for seed in range(50):
            rel, score = R.npc_seed_relation(random.Random(seed), a, b)
            rels.add(rel)
            scores.append(score)
        self.assertTrue(rels.issubset({R.REL_NEUTRAL, R.REL_ALLY, R.REL_WAR}))
        self.assertTrue(all(-100 <= s <= 100 for s in scores))
        # 同样输入 + 同 seed 可复现
        self.assertEqual(R.npc_seed_relation(random.Random(7), a, b),
                         R.npc_seed_relation(random.Random(7), a, b))
        # 魔道性格更难结盟、更易开战
        demon = {"aggression": 95, "key": "demonic"}
        demon_war = sum(R.npc_seed_relation(random.Random(s), a, demon)[0]
                        == R.REL_WAR for s in range(200))
        normal_war = sum(R.npc_seed_relation(random.Random(s), a, b)[0]
                         == R.REL_WAR for s in range(200))
        self.assertGreater(demon_war, normal_war)

    def test_ally_chance_favor_bonus(self):
        low = R.ally_accept_chance(50, 0, 0, favor=0)
        high = R.ally_accept_chance(50, 0, 0, favor=80)
        self.assertGreater(high, low)
        self.assertLessEqual(high, 95.0)



class TestGapFixRules(unittest.TestCase):
    """v4.1.0 缺口补全（契约 §3 Agent A）：特技 / 夜袭 / 羁绊 / 规则工具。"""

    # ---- 特技 8 条 ----
    def test_trait_army_mods_all(self):
        mods = R.trait_army_mods([])
        self.assertEqual(mods["grain_mult"], 1.0)
        self.assertEqual(mods["speed_mult"], 1.0)
        self.assertEqual(mods["defense_mult"], 1.0)
        self.assertEqual(mods["attack_mult"], 1.0)
        self.assertEqual(mods["fire_mult"], 1.0)
        self.assertEqual(mods["recover_pct"], 0.0)
        swift = R.trait_army_mods(["swift"])
        self.assertAlmostEqual(swift["speed_mult"], 1.15)
        self.assertAlmostEqual(swift["grain_mult"], 0.9)
        self.assertAlmostEqual(R.trait_army_mods(["fire_master"])["fire_mult"], 1.2)
        doctor = R.trait_army_mods(["doctor"])
        self.assertAlmostEqual(doctor["recover_pct"], 0.08)
        self.assertEqual(doctor["injury_reduce"], 1)

    def test_trait_defense_mult_stacks(self):
        both = R.trait_army_mods(["siege_expert", "ironwall"])["defense_mult"]
        self.assertAlmostEqual(both, 1.15 * 1.20)

    def test_swift_affects_march_grain_and_pursuit(self):
        leader = {"stats": {"leadership": 50}, "traits": ["swift"]}
        base_army = {"troop_type": "sword_shield", "troops": 1000,
                     "leader": {"stats": {"leadership": 50}, "traits": []}}
        fast_army = dict(base_army, leader=leader)
        self.assertAlmostEqual(
            R.march_speed(fast_army, "plain") / R.march_speed(base_army, "plain"),
            1.15)
        self.assertEqual(R.grain_upkeep(fast_army, "plain", False),
                         int(R.grain_upkeep(base_army, "plain", False) * 0.9))

        def count(**kw):
            rng = random.Random(9)
            return sum(R.pursuit_capture(rng, **kw) for _ in range(500))

        plain = count()
        swift = count(swift=True)
        cavalry = count(cavalry=True)
        both = count(cavalry=True, swift=True)
        self.assertGreater(swift, plain)
        self.assertGreater(cavalry, plain)
        self.assertGreater(both, swift)

    def test_garrison_defense_trait(self):
        def leader(traits):
            return {"stats": {"leadership": 50}, "profession": "sword",
                    "traits": traits}
        army = {"troop_type": "sword_shield", "troops": 1000,
                "leader": leader([])}
        base = R.defense_power(army)
        garrison = R.defense_power(dict(army, leader=leader(["siege_expert", "ironwall"])),
                                   defending_node=True)
        field = R.defense_power(dict(army, leader=leader(["siege_expert", "ironwall"])))
        self.assertAlmostEqual(garrison / base, 1.15 * 1.20)
        self.assertAlmostEqual(field, base)  # 非守城不加成

    # ---- 规则工具（契约 §2.2）----
    def test_morale_task_gain(self):
        cfg = {"governance": {"govern_morale_per_tick": 1.5,
                              "heal_morale_per_tick": 1.0}}
        self.assertEqual(R.morale_task_gain("govern", 4, cfg), 6)
        self.assertEqual(R.morale_task_gain("heal", 4, cfg), 4)
        self.assertEqual(R.morale_task_gain("farm", 4, cfg), 0)
        self.assertEqual(R.morale_task_gain("govern", 0, cfg), 0)

    def test_riot_check(self):
        cfg = {"governance": {"riot_morale": 20, "riot_chance": 1.0}}
        self.assertTrue(R.riot_check(random.Random(1), 10, cfg))
        self.assertFalse(R.riot_check(random.Random(1), 20, cfg))
        self.assertFalse(R.riot_check(random.Random(1), 90, cfg))
        never = {"governance": {"riot_morale": 20, "riot_chance": 0.0}}
        self.assertFalse(R.riot_check(random.Random(1), 0, never))

    def test_overstaff_penalty(self):
        leader = {"stats": {"leadership": 50}}  # 上限 1000
        cfg = {"army": {"overstaff_morale_penalty": 0.1}}
        self.assertEqual(R.overstaff_penalty(leader, 1000, cfg), 0)
        self.assertEqual(R.overstaff_penalty(leader, 1200, cfg), 10)
        # 直接传子配置块也兼容
        self.assertEqual(R.overstaff_penalty(leader, 1200,
                                             {"overstaff_morale_penalty": 0.1}), 10)

    def test_bandit_raid_ready(self):
        node = {"lair": {"last_raid_tick": 8}}
        self.assertFalse(R.bandit_raid_ready(node, 15, 8))
        self.assertTrue(R.bandit_raid_ready(node, 16, 8))
        as_json = {"lair_json": '{"last_raid_tick": 8}'}
        self.assertTrue(R.bandit_raid_ready(as_json, 16, 8))
        self.assertTrue(R.bandit_raid_ready({"lair": {}}, 8, 8))   # 新匪寨从 0 起算
        self.assertFalse(R.bandit_raid_ready(None, 99, 8))

    def test_mini_event_roll(self):
        hits = 0
        for seed in range(400):
            ev = R.mini_event_roll(random.Random(seed))
            if ev is None:
                continue
            hits += 1
            self.assertIn("kind", ev)
            self.assertTrue(ev["text"])
            self.assertGreaterEqual(ev["mult"], 0.6)
            self.assertLessEqual(ev["mult"], 1.6)
        self.assertTrue(20 <= hits <= 80, f"10% 期望命中 40 左右，实际 {hits}")

    def test_arena_bracket(self):
        self.assertEqual(R.arena_bracket(0), "novice")
        self.assertEqual(R.arena_bracket(499), "novice")
        self.assertEqual(R.arena_bracket(500), "veteran")
        self.assertEqual(R.arena_bracket(1999), "veteran")
        self.assertEqual(R.arena_bracket(2000), "master")

    # ---- 羁绊 ----
    def test_maybe_form_yijiao(self):
        a = {"id": 1, "bonds": {"2": {"type": "tongdao", "battles": 5}}}
        b = {"id": 2, "bonds": {"1": {"type": "tongdao", "battles": 5}}}
        self.assertTrue(R.maybe_form_yijiao(a, b, random.Random(1)))
        self.assertEqual(R.bond_get(a, 2)["type"], "yijiao")
        self.assertFalse(R.maybe_form_yijiao(a, b, random.Random(1)))  # 幂等
        # 互救：无 5 战也可按 50% 结义
        formed = [R.maybe_form_yijiao(
            {"id": 3, "bonds": {"4": {"type": "tongdao", "battles": 1, "rescued": True}}},
            {"id": 4, "bonds": {"3": {"type": "tongdao", "battles": 1, "rescued": True}}},
            random.Random(seed)) for seed in range(20)]
        self.assertTrue(any(formed) and not all(formed))

    def test_bond_apply_battle_evolution(self):
        a = {"id": 1, "rarity": "legend", "bonds": {}}
        b = {"id": 2, "rarity": "common", "bonds": {}}
        out = R.bond_apply_battle(a, b, won=True)
        self.assertTrue(out["tongdao"])
        self.assertFalse(out["yijiao"])
        for _ in range(4):
            R.bond_apply_battle(a, b, won=True)
        self.assertEqual(R.bond_get(a, 2)["type"], "yijiao")  # ≥5 战结义
        out10 = None
        for _ in range(5):
            out10 = R.bond_apply_battle(a, b, won=True)
        self.assertTrue(out10["shitu"])  # 传世 + 凡品 10 战 → 师徒
        self.assertEqual(R.bond_get(a, 2)["type"], "shitu")

        # 宿怨：显式标记单挑落败
        c = {"id": 3, "bonds": {}, "duel_lost_to": 4}
        d = {"id": 4, "bonds": {}}
        out2 = R.bond_apply_battle(c, d, won=False)
        self.assertTrue(out2["suyuan"])
        self.assertEqual(R.bond_get(c, 4)["type"], "suyuan")

        # 知遇：招募关系战后补登记
        e = {"id": 5, "bonds": {}, "recruited_by": 6}
        f = {"id": 6, "bonds": {}}
        out3 = R.bond_apply_battle(e, f, won=True)
        self.assertTrue(out3["zhiyu"])
        self.assertEqual(R.bond_get(e, 6)["type"], "zhiyu")

    def test_maybe_form_suyuan_condition(self):
        a, b = {"id": 1, "bonds": {}}, {"id": 2, "bonds": {}}
        self.assertFalse(R.maybe_form_suyuan(a, b))
        self.assertTrue(R.maybe_form_suyuan(a, b, duel_defeated=True))
        self.assertEqual(R.bond_get(a, 2)["type"], "suyuan")
        self.assertFalse(R.maybe_form_suyuan(a, b, killed_bond=True))

    def test_bond_army_morale_bonus(self):
        a = {"id": 1, "bonds": {"2": {"type": "tongdao", "level": 1}}}
        b = {"id": 2, "bonds": {"1": {"type": "tongdao", "level": 1}}}
        c = {"id": 3, "bonds": {"4": {"type": "yijiao", "level": 1}}}
        d = {"id": 4, "bonds": {"3": {"type": "yijiao", "level": 1}}}
        self.assertEqual(R.bond_army_morale_bonus([a, b]), 3)
        self.assertEqual(R.bond_army_morale_bonus([c, d]), 8)
        self.assertEqual(R.bond_army_morale_bonus([a, b, c, d]), 11)
        self.assertEqual(R.bond_army_morale_bonus([]), 0)
        self.assertEqual(R.bond_army_morale_bonus([a]), 0)

    def test_doctor_aftermath(self):
        doc = {"traits": ["doctor"]}
        plain = {"traits": []}
        self.assertEqual(R.doctor_aftermath(doc, 1000, 5), {"recovered": 80, "injury": 4})
        self.assertEqual(R.doctor_aftermath(plain, 1000, 5), {"recovered": 0, "injury": 5})
        self.assertEqual(R.doctor_aftermath({}, 0, 0), {"recovered": 0, "injury": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
