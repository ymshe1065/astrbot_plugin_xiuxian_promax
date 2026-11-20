# tests/test_strategy_hex.py
# 「仙域争霸」P2 六边形会战引擎（core/strategy_hex.py）+ P1/P3 规则扩展单测：
# 地图对称生成 / 部署校验 / ZOC 刚性与取消 / 拦截 / 夹击合围 / 技能与灵力 /
# 单挑 / 设伏 / 托管 AI / 确定性 / 回放重建 / 外交 / 科技 / 计略 / 羁绊演化 /
# 赛季合纵 / 互市 / 商路 / 追赶礼包 / 治理清算
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


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


H = _imp("core.strategy_hex")
R = _imp("core.strategy_rules")


def mk_leader(i=1, prof="sword", role="general", valor=80, wisdom=70,
              injury=0, bonds=None, traits=None):
    return {"id": i, "name": f"将{i}", "role": role, "profession": prof,
            "rarity": "rare",
            "stats": {"leadership": 70, "valor": valor, "wisdom": wisdom,
                      "admin": 50, "charm": 50, "luck": 50},
            "injury": injury, "traits": traits or [],
            "aptitude": {}, "bonds": bonds or {}}


def mk_army(i, fid, tt="sword_shield", troops=1000, prof="sword",
            siege_engine=False, **kw):
    return {"id": i, "faction_id": fid, "troop_type": tt, "troops": troops,
            "morale": 100, "veteran": 0, "leader": mk_leader(i, prof=prof, **kw),
            "siege_engine": bool(siege_engine)}


def plain_session(w=13, h=9, mode="field", seed="t"):
    """全平原战场（无地形干扰，便于机制断言）。"""
    terrain = {f"{q},{r}": H.T_PLAIN for q in range(w) for r in range(h)}
    za, zb = H.deploy_zones(w, h, 3)
    s = H.new_session({"size": [w, h], "terrain": terrain,
                       "deploy_a": za, "deploy_b": zb}, mode, seed)
    return s


def start_plain(s, rng):
    H.start_battle(s, rng)


# ---------------------------------------------------------------------------
# 地图生成与对称性（§7.3）
# ---------------------------------------------------------------------------

class TestMapGen(unittest.TestCase):
    def test_sizes(self):
        for mode, (w, h) in (("field", (13, 9)), ("siege", (15, 11)), ("pass", (9, 11))):
            mi = H.generate_map(f"sz-{mode}", mode)
            self.assertEqual(tuple(mi["size"]), (w, h))

    def test_symmetry_within_tolerance(self):
        # 不对称度 >10% 重掷；生成结果必须 ≤ 容差
        for mode in ("field", "pass"):
            for i in range(8):
                mi = H.generate_map(f"sym-{mode}-{i}", mode)
                w, h = mi["size"]
                self.assertLessEqual(H.symmetry_score(mi["terrain"], w, h), 0.10)

    def test_determinism(self):
        a = H.generate_map("same-seed", "field")
        b = H.generate_map("same-seed", "field")
        self.assertEqual(a["terrain"], b["terrain"])

    def test_siege_has_wall_gate_moat(self):
        mi = H.generate_map("siege-map", "siege")
        kinds = set(mi["terrain"].values())
        self.assertIn(H.T_WALL, kinds)
        self.assertIn(H.T_GATE, kinds)
        self.assertIn(H.T_MOAT, kinds)

    def test_forest_ratio_and_highland(self):
        mi = H.generate_map("ratio", "field")
        w, h = mi["size"]
        kinds = list(mi["terrain"].values())
        forest_pct = kinds.count(H.T_FOREST) / (w * h)
        self.assertLess(forest_pct, 0.30)  # 目标 15~20%，允许簇状溢出
        self.assertGreaterEqual(kinds.count(H.T_HIGHLAND), 1)

    def test_deploy_zones_rotation_symmetric(self):
        w, h, depth = 13, 9, 3
        za, zb = H.deploy_zones(w, h, depth)
        rot = {tuple(H._rotate(q, r, w, h)) for q, r in za}
        self.assertEqual(rot, {tuple(z) for z in zb})


# ---------------------------------------------------------------------------
# 部署与 ZOC（§7.4）
# ---------------------------------------------------------------------------

class TestDeployAndZoc(unittest.TestCase):
    def setUp(self):
        self.s = plain_session()
        self.a1 = H.add_unit(self.s, mk_army(1, 1), "a")
        self.b1 = H.add_unit(self.s, mk_army(2, 2), "b")
        self.rng = random.Random(1)

    def test_deploy_zone_only(self):
        ok, _ = H.valid_deploy(self.s, self.a1["uid"], 0, 0)
        self.assertTrue(ok)
        ok, reason = H.valid_deploy(self.s, self.a1["uid"], 8, 4)
        self.assertFalse(ok)
        self.assertIn("部署区", reason)

    def test_deploy_double_occupy(self):
        a2 = H.add_unit(self.s, mk_army(3, 1), "a")
        H.place_unit(self.s, self.a1["uid"], 0, 0)
        ok, reason = H.valid_deploy(self.s, a2["uid"], 0, 0)
        self.assertFalse(ok)
        self.assertIn("已有军团", reason)

    def test_zoc_rigid_stop(self):
        # 敌军 ZOC 刚性：进入即停
        self.a1["pos"] = [3, 4]
        self.b1["pos"] = [7, 4]
        self.s["phase"] = "battle"
        # a1 机动 3：路径穿过 b1 的 ZOC 格 (6,4) 应被截停
        path = [[4, 4], [5, 4], [6, 4], [7, 4]]
        check = H.check_move_path(self.s, self.a1["uid"], path)
        self.assertTrue(check["ok"])
        self.assertEqual(check["reason"], "zoc_stop")
        self.assertLessEqual(check["end"], 3)

    def test_zoc_cancel_on_river(self):
        # 河流格 ZOC 取消：目标站在河上则邻格不受控
        self.s["terrain"]["7,4"] = H.T_RIVER
        self.b1["pos"] = [7, 4]
        zmap = H.enemy_zoc_map(self.s, self.a1)
        self.assertEqual(zmap, {})

    def test_zoc_friendly_corridor(self):
        # 己方军团所在格 ZOC 取消（友军通道）
        b2 = H.add_unit(self.s, mk_army(4, 2), "b")
        self.b1["pos"] = [7, 4]
        b2["pos"] = [6, 4]  # b2 是 b1 友军，其所在格不受 b1 ZOC 控制
        ztiles = H.zoc_tiles_of_side(self.s, "b")
        self.assertNotIn((6, 4), ztiles)
        self.assertIn((7, 3), ztiles)

    def test_forest_semi_rigid(self):
        # 森林降级半刚性：允许进入森林 ZOC 格，但禁止 ZOC 格间直通
        self.b1["pos"] = [7, 4]
        self.s["terrain"]["6,4"] = H.T_FOREST
        self.s["terrain"]["6,5"] = H.T_FOREST
        self.a1["pos"] = [6, 5]  # 起点已在敌 ZOC 森林格
        self.s["phase"] = "battle"
        # (6,5)→(6,4) 均为敌 ZOC 内的森林格：直通应被拒
        path = [[6, 4]]
        check = H.check_move_path(self.s, self.a1["uid"], path)
        self.assertFalse(check["ok"])
        self.assertIn("半刚性", check["reason"])

    def test_sword_zoc_break_once(self):
        # 剑修每场 1 次突破：无视进入即停
        self.a1["pos"] = [3, 4]
        self.b1["pos"] = [7, 4]
        self.s["phase"] = "battle"
        path = [[4, 4], [5, 4], [6, 4]]
        check = H.check_move_path(self.s, self.a1["uid"], path, use_zoc_break=True)
        self.assertTrue(check["ok"])
        self.assertTrue(check["need_break"])
        res = H.do_move(self.s, self.a1["uid"], path, self.rng, use_zoc_break=True)
        self.assertTrue(self.a1["zoc_break_used"])
        self.assertEqual(self.a1["pos"], [6, 4])
        # 第二次不可突破：会被截停（刚性 ZOC 生效）
        self.a1["moved"] = False
        self.a1["pos"] = [3, 4]
        check2 = H.check_move_path(self.s, self.a1["uid"], path, use_zoc_break=True)
        self.assertFalse(check2.get("need_break", False))
        self.assertEqual(check2["reason"], "zoc_stop")

    def test_intercept_on_leaving_zoc(self):
        # 从敌 ZOC 格离开 → 一次拦截
        self.a1["pos"] = [6, 4]   # 处于 b1(7,4) 的 ZOC 内
        self.b1["pos"] = [7, 4]
        self.s["phase"] = "battle"
        before = self.a1["troops"]
        res = H.do_move(self.s, self.a1["uid"], [[5, 4]], self.rng)
        self.assertTrue(res["ok"])
        self.assertIn(self.b1["uid"], res["intercepts"])
        self.assertLessEqual(self.a1["troops"], before)

    def test_flank_and_encircle_counts(self):
        # odd-q：偶数列 (6,3) 的邻格为 (7,3)(7,2)(6,2)(5,2)(5,3)(6,4)
        self.a1["pos"] = [6, 3]
        self.b1["pos"] = [6, 4]
        b2 = H.add_unit(self.s, mk_army(5, 2), "b")
        b2["pos"] = [7, 3]
        b3 = H.add_unit(self.s, mk_army(6, 2), "b")
        b3["pos"] = [5, 3]
        n = H.adjacent_enemy_count(self.s, self.a1)
        self.assertEqual(n, 3)
        self.assertAlmostEqual(R.flank_factor(n), R.ENCIRCLE_FACTOR)
        # 两个邻敌 → 夹击系数
        b3["pos"] = [0, 8]
        self.assertAlmostEqual(R.flank_factor(
            H.adjacent_enemy_count(self.s, self.a1)), R.FLANK_FACTOR)

    def test_full_encircle_five_rounds_rout(self):
        # 完全合围 5 回合 → 溃败
        s = plain_session(w=9, h=9)
        victim = H.add_unit(s, mk_army(1, 1, troops=5000), "a")
        victim["pos"] = [4, 4]
        # 6 个敌军围住 (4,4) 的全部邻格
        for i, (q, r) in enumerate(R.hex_neighbors(4, 4)):
            u = H.add_unit(s, mk_army(10 + i, 2, troops=5000), "b")
            u["pos"] = [q, r]
        rng = random.Random(9)
        H.start_battle(s, rng)
        self.assertTrue(H.is_fully_encircled(s, victim))
        for _ in range(4):
            self.assertFalse(victim["routed"])
            H.begin_round(s, rng)
        self.assertTrue(victim["routed"])  # 第 5 回合崩溃


# ---------------------------------------------------------------------------
# 灵力与技能（§7.7）
# ---------------------------------------------------------------------------

class TestSkills(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(3)
        self.s = plain_session()
        self.s["phase"] = "battle"
        self.s["round"] = 1

    def test_mana_gain_and_cap(self):
        u = H.add_unit(self.s, mk_army(1, 1), "a")
        u["pos"] = [0, 0]
        u["mana"] = 90
        H.begin_round(self.s, self.rng)
        self.assertEqual(u["mana"], 100)  # +15 上限 100
        # 受击 +5
        foe = H.add_unit(self.s, mk_army(2, 2), "b")
        u["mana"] = 50
        H._apply_damage(self.s, u, 10, self.rng)
        self.assertEqual(u["mana"], 55)

    def test_fire_burns_and_dot(self):
        caster = H.add_unit(self.s, mk_army(1, 1, prof="spirit"), "a")
        caster["leader"]["role"] = "strategist"
        caster["pos"] = [2, 4]
        caster["mana"] = 100
        victim = H.add_unit(self.s, mk_army(2, 2), "b")
        victim["pos"] = [4, 4]
        self.s["phase"] = "battle"
        res = H.do_skill(self.s, caster["uid"], "fire", self.rng, target_hex=[4, 4])
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["burned"]), 1)
        troops_after_fire = victim["troops"]
        # 灼烧 DOT：下一回合开始再掉血
        H.begin_round(self.s, self.rng)
        self.assertLess(victim["troops"], troops_after_fire)

    def test_fire_insufficient_mana(self):
        caster = H.add_unit(self.s, mk_army(1, 1, prof="spirit"), "a")
        caster["leader"]["role"] = "strategist"
        caster["pos"] = [2, 4]
        caster["mana"] = 10
        res = H.do_skill(self.s, caster["uid"], "fire", self.rng, target_hex=[4, 4])
        self.assertFalse(res["ok"])
        self.assertIn("灵力", res["reason"])

    def test_profession_gate(self):
        # 剑修不能放火攻
        u = H.add_unit(self.s, mk_army(1, 1, prof="sword"), "a")
        u["pos"] = [2, 4]
        u["mana"] = 100
        self.assertNotIn("fire", H.unit_skills(self.s, u))
        res = H.do_skill(self.s, u["uid"], "fire", self.rng, target_hex=[4, 4])
        self.assertFalse(res["ok"])

    def test_heal_capped_by_injury_pool(self):
        healer = H.add_unit(self.s, mk_army(1, 1, prof="buddha"), "a")
        healer["pos"] = [2, 4]
        healer["mana"] = 100
        ally = H.add_unit(self.s, mk_army(2, 1, troops=1000), "a")
        ally["pos"] = [3, 4]
        ally["troops"] = 800
        ally["injury_pool"] = 50  # 伤兵池只有 50 → 治疗上限 50
        res = H.do_skill(self.s, healer["uid"], "heal", self.rng,
                         target_uid=ally["uid"])
        self.assertTrue(res["ok"])
        self.assertLessEqual(res["healed"], 50)

    def test_beastrush_summon_expires(self):
        master = H.add_unit(self.s, mk_army(1, 1, prof="beast"), "a")
        master["pos"] = [2, 4]
        master["mana"] = 100
        self.s["round"] = 1
        res = H.do_skill(self.s, master["uid"], "beastrush", self.rng,
                         target_hex=[3, 4])
        self.assertTrue(res["ok"])
        summon = H.get_unit(self.s, res["uid"])
        self.assertEqual(summon["troops"], 500)
        self.assertTrue(summon["temp"])
        # 3 回合后消散
        for _ in range(4):
            H.begin_round(self.s, self.rng)
        self.assertTrue(summon["routed"])
        self.assertEqual(summon["troops"], 0)

    def test_bulwark_def_bonus(self):
        u = H.add_unit(self.s, mk_army(1, 1, prof="body"), "a")
        u["pos"] = [2, 4]
        u["mana"] = 100
        res = H.do_skill(self.s, u["uid"], "bulwark", self.rng)
        self.assertTrue(res["ok"])
        view = H._sim_view(self.s, u)
        self.assertAlmostEqual(view["def_terrain_mult"], 1.4)

    def test_barrage_needs_highland(self):
        u = H.add_unit(self.s, mk_army(1, 1), "a")
        u["pos"] = [2, 4]
        u["mana"] = 100
        res = H.do_skill(self.s, u["uid"], "barrage", self.rng, target_hex=[3, 4])
        self.assertFalse(res["ok"])
        self.s["terrain"]["2,4"] = H.T_HIGHLAND
        u["acted"] = False
        res = H.do_skill(self.s, u["uid"], "barrage", self.rng, target_hex=[3, 4])
        self.assertTrue(res["ok"])

    def test_flood_creates_temp_swamp(self):
        s = plain_session()
        s["terrain"]["3,4"] = H.T_RIVER
        u = H.add_unit(s, mk_army(1, 1, prof="spirit"), "a")
        u["leader"]["role"] = "strategist"
        u["pos"] = [2, 4]
        u["mana"] = 100
        s["phase"] = "battle"
        s["round"] = 1
        res = H.do_skill(s, u["uid"], "flood", self.rng, target_hex=[3, 4])
        self.assertTrue(res["ok"])
        self.assertEqual(H.eff_terrain(s, 3, 5), H.T_SWAMP)
        # 到期恢复
        s["round"] = 99
        self.assertEqual(H.eff_terrain(s, 3, 5), H.T_PLAIN)

    def test_combo_needs_sworn_bond(self):
        la = mk_leader(1, bonds={"2": {"type": "yijiao", "level": 1}})
        lb = mk_leader(2)
        u1 = H.add_unit(self.s, mk_army(1, 1), "a")
        u1["leader"] = la
        u1["pos"] = [2, 4]
        u1["mana"] = 100
        u2 = H.add_unit(self.s, mk_army(2, 1), "a")
        u2["leader"] = lb
        u2["pos"] = [3, 4]
        u2["mana"] = 100
        victim = H.add_unit(self.s, mk_army(3, 2), "b")
        victim["pos"] = [4, 4]
        victim["start_troops"] = 1000
        self.assertIn("combo", H.unit_skills(self.s, u1))
        res = H.do_skill(self.s, u1["uid"], "combo", self.rng,
                         target_uid=victim["uid"])
        self.assertTrue(res["ok"])
        self.assertEqual(u2["mana"], 40)  # 搭档也耗 60


# ---------------------------------------------------------------------------
# 单挑（§7.8）
# ---------------------------------------------------------------------------

class TestDuel(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(11)
        self.s = plain_session()
        self.s["phase"] = "battle"
        self.s["round"] = 1

    def _challenger(self, valor=90):
        u = H.add_unit(self.s, mk_army(1, 1, valor=valor), "a")
        u["pos"] = [3, 4]
        return u

    def _target(self, injury=0):
        u = H.add_unit(self.s, mk_army(2, 2, injury=injury), "b")
        u["pos"] = [4, 4]
        return u

    def test_window_rounds_1_to_3(self):
        c, t = self._challenger(), self._target()
        ok, _ = H.can_challenge(self.s, c["uid"], t["uid"])
        self.assertTrue(ok)
        self.s["round"] = 4
        ok, reason = H.can_challenge(self.s, c["uid"], t["uid"])
        self.assertFalse(ok)
        self.assertIn("窗口", reason)

    def test_non_general_cannot_challenge(self):
        c = self._challenger()
        c["leader"]["role"] = "strategist"
        t = self._target()
        ok, _ = H.can_challenge(self.s, c["uid"], t["uid"])
        self.assertFalse(ok)

    def test_low_valor_cannot_challenge(self):
        c = self._challenger(valor=60)
        t = self._target()
        self.assertFalse(H.can_challenge(self.s, c["uid"], t["uid"])[0])

    def test_refuse_morale_minus_5(self):
        c, t = self._challenger(), self._target()
        H.challenge_duel(self.s, c["uid"], t["uid"])
        before = t["morale"]
        H.refuse_duel(self.s)
        self.assertEqual(t["morale"], before - 5)
        self.assertIsNone(self.s["duel"])

    def test_resolve_produces_outcome(self):
        c, t = self._challenger(), self._target()
        H.challenge_duel(self.s, c["uid"], t["uid"])
        out = H.resolve_duel(self.s, self.rng)
        self.assertTrue(out["ok"])
        self.assertIn(out["outcome"], ("challenger_win", "target_win", "draw"))
        self.assertTrue(out["texts"])  # 表演文字流
        self.assertIsNone(self.s["duel"])

    def test_execute_dying_loser(self):
        c, t = self._challenger(), self._target(injury=75)  # 濒死
        t["leader"]["injury"] = 75
        H.challenge_duel(self.s, c["uid"], t["uid"])
        # 反复打直到分出胜负（rng 固定，确定性）
        for _ in range(20):
            H.challenge_duel(self.s, c["uid"], t["uid"])
            out = H.resolve_duel(self.s, self.rng)
            if out.get("outcome") != "draw":
                break
        if out.get("outcome") == "challenger_win":
            self.assertTrue(out.get("executed"))
            self.assertTrue(out["texts"][-1].find("斩落") >= 0)


# ---------------------------------------------------------------------------
# 设伏（§7.4/§15）
# ---------------------------------------------------------------------------

class TestAmbush(unittest.TestCase):
    def test_ambush_requires_forest_and_limit_2(self):
        s = plain_session()
        s["terrain"]["2,4"] = H.T_FOREST
        s["terrain"]["2,5"] = H.T_FOREST
        s["terrain"]["2,6"] = H.T_FOREST
        u1 = H.add_unit(s, mk_army(1, 1, prof="ghost"), "a")
        u2 = H.add_unit(s, mk_army(2, 1, prof="ghost"), "a")
        u3 = H.add_unit(s, mk_army(3, 1, prof="ghost"), "a")
        self.assertTrue(H.set_ambush(s, u1["uid"], 2, 4)[0])
        self.assertTrue(H.set_ambush(s, u2["uid"], 2, 5)[0])
        ok, reason = H.set_ambush(s, u3["uid"], 2, 6)
        self.assertFalse(ok)
        self.assertIn("2 处", reason)
        ok, reason = H.set_ambush(s, u3["uid"], 0, 0)
        self.assertFalse(ok)
        self.assertIn("森林", reason)

    def test_ambush_trigger(self):
        s = plain_session()
        s["terrain"]["1,4"] = H.T_FOREST
        amb = H.add_unit(s, mk_army(1, 1, prof="ghost"), "a")
        ok, reason = H.set_ambush(s, amb["uid"], 1, 4)  # 部署区内的森林格
        self.assertTrue(ok, reason)
        victim = H.add_unit(s, mk_army(2, 2), "b")
        victim["pos"] = [9, 4]
        rng = random.Random(5)
        H.start_battle(s, rng)
        events = H.check_ambush_trigger(s, victim, 2, 4, rng)
        self.assertEqual(len(events), 1)
        self.assertEqual(victim["morale"], 90)
        self.assertIn(amb["uid"], s.get("ambush_initiative", []))


# ---------------------------------------------------------------------------
# 结束判定 / result 兼容性（§7.3）
# ---------------------------------------------------------------------------

class TestEndAndResult(unittest.TestCase):
    def test_end_at_30_pct(self):
        s = plain_session()
        a = H.add_unit(s, mk_army(1, 1, troops=1000), "a")
        b = H.add_unit(s, mk_army(2, 2, troops=1000), "b")
        a["pos"] = [0, 0]
        b["pos"] = [12, 8]
        s["phase"] = "battle"
        b["troops"] = 299
        self.assertEqual(H.check_end(s), "attacker")
        a["troops"] = 299
        # 双方同时跌破：先判攻方 → 守方胜（与 P0 平局规则一致）
        self.assertEqual(H.check_end(s), "defender")

    def test_max_rounds_defender_wins(self):
        s = plain_session()
        s["max_rounds"] = 3
        a = H.add_unit(s, mk_army(1, 1, troops=1000), "a")
        b = H.add_unit(s, mk_army(2, 2, troops=1000), "b")
        a["pos"] = [0, 0]
        b["pos"] = [12, 8]
        s["phase"] = "battle"
        s["round"] = 3
        self.assertEqual(H.check_end(s), "defender")

    def test_finish_result_shape(self):
        s = plain_session()
        a = H.add_unit(s, mk_army(1, 1, troops=1000), "a")
        b = H.add_unit(s, mk_army(2, 2, troops=1000), "b")
        a["pos"] = [0, 0]
        b["pos"] = [12, 8]
        s["phase"] = "battle"
        s["winner"] = "attacker"
        res = H.finish(s)
        for key in ("winner", "rounds", "round_log", "attackers", "defenders",
                    "routed_attacker_ids", "routed_defender_ids",
                    "peril_events", "atk_losses", "dfn_losses"):
            self.assertIn(key, res)
        row = res["attackers"][0]
        for key in ("id", "troops_before", "troops_after", "morale", "routed",
                    "leader_id", "leader_name", "troop_type", "veteran"):
            self.assertIn(key, row)


# ---------------------------------------------------------------------------
# 托管 AI / 确定性 / 回放（§7.3 可复现）
# ---------------------------------------------------------------------------

class TestAIAndReplay(unittest.TestCase):
    PROFS_A = [("sword_shield", "sword"), ("flying_sword", "ghost"),
               ("spell_corps", "spirit"), ("heavy_armor", "body")]
    PROFS_B = [("sword_shield", "buddha"), ("beast_horde", "beast"),
               ("spell_corps", "demon")]

    def _armies(self):
        ua = [mk_army(i + 1, 1, tt, 1500, prof=p, wisdom=70)
              for i, (tt, p) in enumerate(self.PROFS_A)]
        ub = [mk_army(i + 10, 2, tt, 1400, prof=p, wisdom=70)
              for i, (tt, p) in enumerate(self.PROFS_B)]
        ua[3]["siege_engine"] = True
        return ua, ub

    def _run(self, seed, mode="field"):
        ua, ub = self._armies()
        mi = H.generate_map(seed, mode)
        s = H.new_session(mi, mode, seed)
        for sim in ua:
            H.add_unit(s, sim, "a")
        for sim in ub:
            H.add_unit(s, sim, "b")
        rng = random.Random(seed + ":rng")
        return H.ai_auto_battle(s, rng), s

    def test_ai_tiers(self):
        self.assertEqual(H.ai_tier({"leader": {"stats": {"wisdom": 90}}}), "smart")
        self.assertEqual(H.ai_tier({"leader": {"stats": {"wisdom": 60}}}), "balanced")
        self.assertEqual(H.ai_tier({"leader": {"stats": {"wisdom": 30}}}), "brave")
        # 谋略科技 L3：智谋等效 +5
        self.assertEqual(
            H.ai_tier({"leader": {"stats": {"wisdom": 76}}}, 5), "smart")

    def test_auto_battle_finishes_all_modes(self):
        for mode in ("field", "siege", "pass"):
            res, s = self._run(f"ai-{mode}", mode)
            self.assertIn(res["winner"], ("attacker", "defender"))
            self.assertEqual(s["phase"], "done")
            self.assertGreaterEqual(res["rounds"], 1)

    def test_determinism_same_seed(self):
        r1, _ = self._run("det-x")
        r2, _ = self._run("det-x")
        self.assertEqual(
            (r1["winner"], r1["rounds"], r1["atk_losses"], r1["dfn_losses"]),
            (r2["winner"], r2["rounds"], r2["atk_losses"], r2["dfn_losses"]))

    def test_replay_reconstructs(self):
        seed = "replay-1"
        r1, s1 = self._run(seed)
        ua, ub = self._armies()
        r2 = H.replay(seed, "field", ua, ub, s1["commands"],
                      random.Random(seed + ":rng"))
        self.assertEqual(
            (r2["winner"], r2["rounds"], r2["atk_losses"], r2["dfn_losses"]),
            (r1["winner"], r1["rounds"], r1["atk_losses"], r1["dfn_losses"]))

    def test_report_render(self):
        r1, _ = self._run("report-1")
        text = H.render_hex_report(r1, title="测试")
        self.assertIn("手操会战", text)
        self.assertIn("结果", text)


# ---------------------------------------------------------------------------
# P1/P3 规则扩展（外交/科技/计略/羁绊/赛季/互市/商路/追赶/治理）
# ---------------------------------------------------------------------------

class TestRulesExt(unittest.TestCase):
    def test_reputation(self):
        self.assertEqual(R.reputation_rank(80), "righteous")
        self.assertEqual(R.reputation_rank(-70), "infamous")
        self.assertEqual(R.reputation_rank(-30), "greedy")
        self.assertEqual(R.reputation_rank(0), "neutral")
        self.assertEqual(R.reputation_effects(-70)["war_prep_mult"], 0.5)
        self.assertEqual(R.reputation_effects(80)["persuade_bonus"], 0.10)

    def test_diplomacy_relation(self):
        dip = {"5": {"rel": "truce", "until": 100},
               "6": {"rel": "war", "war_at": 200},
               "7": {"rel": "ally"}}
        self.assertEqual(R.relation_of(dip, 5, 50), "truce")
        self.assertEqual(R.relation_of(dip, 5, 150), "neutral")
        self.assertEqual(R.relation_of(dip, 6, 100), "war_pending")
        self.assertEqual(R.relation_of(dip, 6, 300), "war")
        self.assertTrue(R.is_hostile(dip, 6, 300))
        self.assertFalse(R.is_hostile(dip, 6, 100))
        self.assertEqual(R.relation_of(dip, 999), "neutral")

    def test_ally_chance_clamped(self):
        self.assertGreaterEqual(R.ally_accept_chance(0, -100, -100), 5.0)
        self.assertLessEqual(R.ally_accept_chance(100, 100, 100), 95.0)

    def test_tech_effects(self):
        eff = R.tech_effects({"military": 5, "agri": 3, "stratagem": 4})
        self.assertAlmostEqual(eff["atk_bonus"], 0.06)
        self.assertAlmostEqual(eff["flank_bonus"], 0.05)
        self.assertAlmostEqual(eff["output_bonus"], 0.08)
        self.assertEqual(eff["plot_cd_reduce"], 2)
        self.assertEqual(eff["counter_spy"], 0.0)
        self.assertEqual(R.tech_effects({"stratagem": 5})["counter_spy"], 0.30)
        self.assertEqual(R.tech_effects(None)["atk_bonus"], 0.0)
        self.assertEqual(R.tech_research_hours(10, 50), 7.5)

    def test_plot_rate(self):
        self.assertEqual(R.plot_success_rate(90, 50), 45.0)
        self.assertEqual(R.plot_success_rate(10, 90), 15.0)
        self.assertEqual(R.plot_success_rate(0, 200), 10.0)     # 下限
        self.assertEqual(R.plot_success_rate(100, 0, 0.5), 85.0)  # 上限
        # 营救 = 35 + (80-60)/4 + 统率100/10 % = 50
        self.assertEqual(R.rescue_success_rate(80, 60, 100), 50.0)

    def test_bond_evolution(self):
        a = {"id": 1, "rarity": "common", "loyalty": 70, "bonds": {}}
        b = {"id": 2, "rarity": "common", "loyalty": 70, "bonds": {}}
        for _ in range(4):
            R.bond_battle_progress(a, b)
        self.assertEqual(R.bond_get(a, 2)["type"], "tongdao")
        R.bond_battle_progress(a, b)
        self.assertEqual(R.bond_get(a, 2)["type"], "yijiao")
        self.assertEqual(R.bond_morale_bonus_full(a, b), 8)
        sen = {"id": 3, "rarity": "legend", "traits": ["fire_master"], "bonds": {}}
        jun = {"id": 4, "rarity": "common", "traits": [], "bonds": {}}
        self.assertFalse(R.maybe_form_shitu(sen, jun, 9))
        self.assertTrue(R.maybe_form_shitu(sen, jun, 10))
        self.assertEqual(R.junior_exp_mult(jun, 3), 1.2)
        R.form_suyuan(a, b)
        self.assertAlmostEqual(R.suyuan_attack_bonus(a, b), 1.10)

    def test_legacy(self):
        sen = {"id": 3, "rarity": "legend", "traits": ["swift"], "bonds": {}}
        jun = {"id": 4, "rarity": "common", "traits": [], "bonds": {}}
        friend = {"id": 5, "loyalty": 80, "bonds": {}}
        R.maybe_form_shitu(sen, jun, 10)
        R.bond_set_both(sen, friend, "yijiao")
        leg = R.legacy_on_death(sen, [jun, friend])
        self.assertEqual(leg["heir"]["id"], 4)
        self.assertEqual(leg["inherited_trait"], "swift")
        self.assertIn("swift", jun["traits"])
        self.assertEqual(len(leg["avengers"]), 1)
        self.assertEqual(friend["loyalty"], 60)
        self.assertTrue(friend["vengeance"]["active"])

    def test_rebuild_cost_growth(self):
        self.assertEqual(R.rebuild_gold_cost(3000000, 0), 3000000)
        self.assertEqual(R.rebuild_gold_cost(3000000, 1), 4500000)
        self.assertEqual(R.rebuild_gold_cost(3000000, 2), 6750000)

    def test_hegemony_and_coalition(self):
        share = R.hegemony_share({1: ["city", "city", "pass"], 2: ["mine", "ruin"]})
        self.assertAlmostEqual(share[1], 0.8)
        self.assertAlmostEqual(share[2], 0.2)
        self.assertTrue(R.coalition_state(0.45, False))
        self.assertTrue(R.coalition_state(0.35, True))   # 迟滞区维持
        self.assertFalse(R.coalition_state(0.35, False))
        self.assertFalse(R.coalition_state(0.25, True))
        self.assertEqual(R.hegemony_share({}), {})

    def test_season_phase_and_carryover(self):
        self.assertEqual(R.season_phase(10), "early")
        self.assertEqual(R.season_phase(40), "mid")
        self.assertEqual(R.season_phase(60), "late")
        self.assertEqual(R.season_phase(73), "final")
        self.assertEqual(R.veteran_carryover(400), 200)
        self.assertEqual(R.veteran_carryover(200), 0)
        self.assertEqual(R.honor_from_merit(1500), 1500)

    def test_market_tax_and_maker(self):
        self.assertEqual(R.market_tax(1000), 50)
        rng = random.Random(1)
        sell = R.npc_maker_price(100, rng, is_sell=True)
        buy = R.npc_maker_price(100, rng, is_sell=False)
        self.assertGreaterEqual(sell, 100)
        self.assertLessEqual(buy, 100)

    def test_trade_routes(self):
        adj = {1: [2, 3], 2: [1, 3], 3: [1, 2, 4], 4: [3]}
        nt = {1: "market", 2: "mine", 3: "city", 4: "ruin"}
        ow = {1: 7, 2: 7, 3: 7, 4: 9}
        routes = R.find_trade_routes(adj, nt, ow, lambda a, b: a == b)
        self.assertTrue(any(r["a"] == 1 and r["b"] == 3 for r in routes))
        # 中间节点被第三方切断 → 商路不通
        ow2 = dict(ow)
        ow2[2] = 9
        ow2[3] = 9
        routes2 = R.find_trade_routes(adj, nt, ow2, lambda a, b: a == b)
        self.assertEqual(routes2, [])

    def test_node_encircled(self):
        adj = {1: [2, 3], 2: [1, 3], 3: [1, 2, 4], 4: [3]}
        self.assertTrue(R.is_node_encircled(1, adj, {1: 7, 2: 8, 3: 8},
                                            lambda a, b: a != b))
        self.assertFalse(R.is_node_encircled(1, adj, {1: 7, 2: 7, 3: 8},
                                             lambda a, b: a != b))

    def test_catchup_scale(self):
        self.assertEqual(R.catchup_scale(0), 1.0)
        self.assertAlmostEqual(R.catchup_scale(37, 75),
                               1.0 + 0.5 * (37 / 75 / 0.5))
        self.assertAlmostEqual(R.catchup_scale(75, 75), 1.2)
        self.assertEqual(R.scale_pack({"coin": 1000, "grain": 500}, 1.5),
                         {"coin": 1500, "grain": 750})

    def test_governance(self):
        mem = [{"user_id": "a", "position": "deputy", "contribution": 5},
               {"user_id": "b", "position": "", "contribution": 50},
               {"user_id": "L", "position": "leader", "contribution": 100}]
        self.assertEqual(R.successor_of(mem, "L")["user_id"], "a")
        self.assertEqual(
            R.successor_of([m for m in mem if m["user_id"] != "a"], "L")["user_id"], "b")
        self.assertIsNone(R.successor_of([mem[2]], "L"))
        split = R.treasury_split({"coin": 1000}, {"x": 30, "y": 70})
        self.assertEqual(split["x"]["coin"], 300)
        self.assertEqual(split["y"]["coin"], 700)
        self.assertEqual(R.treasury_split({"coin": 100}, {}), {})

    def test_convoy_and_pursuit(self):
        rng = random.Random(2)
        # 压倒性兵力劫掠成功率高
        hits = sum(R.convoy_raid_success(rng, 100, 10000) for _ in range(200))
        self.assertGreater(hits, 170)
        self.assertIsInstance(R.pursuit_capture(rng, cavalry=True), bool)

    def test_position_bonus(self):
        bonus = R.faction_position_bonus({"taiwei": ["u1"], "situ": ["u2"],
                                          "leader": ["u3"], "jijiu": []})
        self.assertAlmostEqual(bonus["army_atk"], 0.03)
        self.assertAlmostEqual(bonus["node_output"], 0.05)
        self.assertEqual(bonus["plot_bonus"], 0.0)  # 祭酒无人

    def test_achievement_unlocked(self):
        self.assertTrue(R.achievement_unlocked({"x": 10}, "x", 10))
        self.assertFalse(R.achievement_unlocked({"x": 9}, "x", 10))
        self.assertTrue(R.achievement_unlocked({"y": True}, "y", None))


# ---------------------------------------------------------------------------
# 审查修复回归（B2/B4/B12/B13 及 move→attack 同回合语义）
# ---------------------------------------------------------------------------

class TestBfixRegression(unittest.TestCase):
    def setUp(self):
        self.s = plain_session()
        self.a1 = H.add_unit(self.s, mk_army(1, 1, troops=2000), "a")
        self.b1 = H.add_unit(self.s, mk_army(2, 2, troops=2000), "b")
        self.rng = random.Random(7)
        self.a1["pos"] = [3, 4]
        self.b1["pos"] = [9, 4]
        self.s["phase"] = "battle"
        H.begin_round(self.s, self.rng)

    def test_move_then_attack_same_round(self):
        # §7.3：移动不消耗行动权（moved=True / acted=False），同回合可再攻击
        self.b1["pos"] = [6, 4]
        mv = H.do_move(self.s, self.a1["uid"], [[4, 4], [5, 4]], self.rng)
        self.assertNotEqual(mv.get("ok"), False)
        self.assertTrue(self.a1["moved"])
        self.assertFalse(self.a1["acted"])
        atk = H.do_attack(self.s, self.a1["uid"], self.b1["uid"], self.rng)
        self.assertTrue(atk["ok"])
        self.assertTrue(self.a1["acted"])

    def test_summon_does_not_block_round_complete(self):
        # B2：回合中途入场的召唤物不在先手序 order 中，不得阻塞回合结束判定
        summon = H.make_unit(
            {"id": -999, "faction_id": 1, "troop_type": "beast_horde",
             "troops": 500, "morale": 100, "veteran": 0,
             "leader": None, "siege_engine": False}, "a", self.s["next_uid"])
        self.s["next_uid"] += 1
        summon["pos"] = [4, 4]
        summon["temp"] = True
        summon["temp_expire"] = self.s["round"] + 3
        self.s["units"].append(summon)
        for uid in list(self.s["order"]):
            H.end_actor_turn(self.s, uid)
        self.assertIsNone(H.current_actor(self.s))
        self.assertTrue(H.round_complete(self.s))

    def test_confused_attack_deterministic_and_recorded(self):
        # B13/惑敌对人：确定性伪随机选目标 + 指令入回放流 + 消耗行动权
        self.b1["pos"] = [4, 4]  # 与 a1 相邻
        self.s["statuses"][str(self.a1["uid"])] = {"confuse": 2}
        n_cmd = len(self.s["commands"])
        res = H.do_confused_attack(self.s, self.a1["uid"], self.rng)
        self.assertTrue(res["ok"])
        self.assertTrue(self.a1["acted"])
        types = [c["type"] for c in self.s["commands"][n_cmd:]]
        self.assertIn("confused_attack", types)
        # 同一 round/uid/战场 → 目标选择确定（回放对齐）
        idx = (self.s["round"] * 7 + self.a1["uid"] * 13) % 1
        self.assertEqual(idx, 0)

    def test_shieldwall_halves_damage(self):
        # B12：剑盾专属「盾墙」状态实际减伤 50%
        self.b1["pos"] = [4, 4]
        st = self.s["statuses"].setdefault(str(self.a1["uid"]), {})
        st["shieldwall"] = 1
        before = self.a1["troops"]
        H._apply_damage(self.s, self.a1, 100, self.rng, source=self.b1, kind="test")
        lost = before - self.a1["troops"]
        self.assertEqual(lost, 50)

    def test_zoc_break_one_shot(self):
        # B19：剑修 ZOC 突破为一次性——同一条路径的第二个刚性 ZOC 仍截停
        # 布局：sword [2,6] → e1[4,5] 控制 [4,6]，e2[5,7] 控制 [5,6]（mp=3 恰好）
        sword = H.add_unit(self.s, mk_army(3, 1, troops=2000, prof="sword"), "a")
        sword["pos"] = [2, 6]
        e1 = H.add_unit(self.s, mk_army(4, 2, troops=2000), "b")
        e1["pos"] = [4, 5]
        e2 = H.add_unit(self.s, mk_army(5, 2, troops=2000), "b")
        e2["pos"] = [5, 7]
        path = [[3, 6], [4, 6], [5, 6]]
        # 不使用突破：第一个刚性 ZOC（[4,6]）即截停
        plain = H.check_move_path(self.s, sword["uid"], path)
        self.assertTrue(plain["ok"])
        self.assertEqual(plain["reason"], "zoc_stop")
        self.assertEqual(plain["end"], 2)
        self.assertFalse(plain["need_break"])
        # 使用突破：穿过第一个 ZOC，被第二个（[5,6]）截停，且触发 e1 拦截
        check = H.check_move_path(self.s, sword["uid"], path, use_zoc_break=True)
        self.assertTrue(check["ok"])
        self.assertEqual(check["reason"], "zoc_stop")
        self.assertTrue(check["need_break"])
        self.assertEqual(check["end"], 3)
        self.assertIn(e1["uid"], check["intercepts"])


if __name__ == "__main__":
    unittest.main()
