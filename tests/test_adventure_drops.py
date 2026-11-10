# tests/test_adventure_drops.py
# 历练掉落：路线专精、境界分档、圣品解锁、旧配置兜底
import io
import json
import random
import sys
import types
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()

def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


AdventureManager = _imp("managers.adventure_manager").AdventureManager

CFG_PATH = ROOT / "config" / "adventure_config.json"
MAT_PATH = ROOT / "config" / "material_catalog.json"
RIFT_PATH = ROOT / "config" / "rift_config.json"

with open(CFG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)
with open(MAT_PATH, encoding="utf-8") as _f:
    MATS = {m["name"]: m for m in json.load(_f)["materials"]}

ROUTES = {r["key"]: r for r in CFG["routes"]}
CATEGORIES = ("灵植", "矿材", "灵液", "妖兽")


class _Player:
    def __init__(self, level_index):
        self.level_index = level_index


def make_manager():
    return AdventureManager(None)


class TestConfigShape(unittest.TestCase):
    def test_no_legacy_drop_tables(self):
        """旧的 3 档表已由 drop_pools 取代，留着会和境界分档打架。"""
        self.assertNotIn("drop_tables", CFG)

    def test_every_route_has_product(self):
        for route in CFG["routes"]:
            product = route.get("product")
            self.assertIn(
                product, CATEGORIES + ("全产物",),
                f"{route['name']} 缺少合法主产物：{product}",
            )

    def test_realm_bands_cover_top_level(self):
        bands = CFG["realm_bands"]
        self.assertGreaterEqual(bands[0]["max_level"], 0)
        self.assertGreater(bands[-1]["max_level"], 35, "最高段必须覆盖满级")

    def test_band_bounds_ascending(self):
        bounds = [b["max_level"] for b in CFG["realm_bands"]]
        self.assertEqual(bounds, sorted(bounds), "境界段上限必须递增")

    def test_band_tiers_are_known(self):
        valid = {"凡品", "黄品", "玄品", "圣品", "帝品"}
        for band in CFG["realm_bands"]:
            for tier, weight in band["tiers"].items():
                self.assertIn(tier, valid, f"{band['label']} 出现未知品阶 {tier}")
                self.assertGreater(weight, 0, f"{band['label']}/{tier} 权重必须为正")

    def test_pool_materials_exist(self):
        for category, tiers in CFG["drop_pools"].items():
            self.assertIn(category, CATEGORIES)
            for tier, names in tiers.items():
                self.assertIn(tier, {"凡品", "黄品", "玄品", "圣品", "帝品"})
                self.assertTrue(names, f"{category}/{tier} 为空池")
                for name in names:
                    self.assertIn(name, MATS, f"{name} 不在 material_catalog")

    def test_pool_category_matches_declared(self):
        """材料不能串类别，否则炼丹按 (类别, 品阶) 匹配时会取错。"""
        for category, tiers in CFG["drop_pools"].items():
            for tier, names in tiers.items():
                for name in names:
                    self.assertEqual(
                        MATS[name]["category"], category,
                        f"{name} 实际类别是 {MATS[name]['category']}，被放进 {category} 池",
                    )

    def test_pool_tier_matches_declared(self):
        """同一池里的材料品阶要一致（妖兽低阶池除外，它只有黄品可用）。"""
        for category, tiers in CFG["drop_pools"].items():
            for tier, names in tiers.items():
                actual = {MATS[n]["tier"] for n in names}
                self.assertEqual(
                    actual, {tier},
                    f"{category}/{tier} 池里混入了其他品阶：{actual}",
                )


class TestCoverage(unittest.TestCase):
    def setUp(self):
        self.mgr = make_manager()

    def test_every_band_has_all_categories(self):
        """每个境界段都要能掉出四类材料 —— 否则某类专精路线在该段是白跑。"""
        for band in CFG["realm_bands"]:
            for category in CATEGORIES:
                names, _ = self.mgr._resolve_pool(category, next(iter(band["tiers"])))
                self.assertTrue(
                    names, f"{band['label']} 段的 {category} 没有可掉材料"
                )

    def test_category_available_at_every_level(self):
        for level in range(0, 36):
            band = self.mgr.realm_band(level)
            for category in CATEGORIES:
                tier = self.mgr._roll_tier(band)
                names, _ = self.mgr._resolve_pool(category, tier)
                self.assertTrue(names, f"lv{level} 的 {category} 抽不到材料")

    def test_all_product_routes_cover_four_categories(self):
        """「全产物」路线要真的能出四类，不能永远落到同一类。"""
        route = next(r for r in CFG["routes"] if r.get("product") == "全产物")
        seen = Counter(
            self.mgr._route_category(route) for _ in range(400)
        )
        for category in CATEGORIES:
            self.assertGreater(seen[category], 0, f"全产物路线从未出过 {category}")

    def test_specialty_routes_are_fixed(self):
        """专精路线必须只出自己那一类，否则「选路线 = 选材料」不成立。"""
        seen = {
            self.mgr._route_category(ROUTES["herb_valley"]) for _ in range(50)
        }
        self.assertEqual(seen, {"灵植"})
        seen = {
            self.mgr._route_category(ROUTES["moon_lake"]) for _ in range(50)
        }
        self.assertEqual(seen, {"妖兽"})


class TestRealmScaling(unittest.TestCase):
    def setUp(self):
        self.mgr = make_manager()

    def test_low_level_never_drops_saint(self):
        """12 级以下只出凡品，圣品掉出去会直接砸穿前期经济。"""
        for level in (0, 6, 12):
            band = self.mgr.realm_band(level)
            tiers = set(band["tiers"])
            self.assertEqual(tiers, {"凡品"}, f"lv{level} 应该只有凡品：{tiers}")

    def test_saint_unlocks_at_jindan(self):
        """丹方 lv12 起就要圣品，历练必须在 lv13 接上，否则 12~17 级是真空。"""
        band = self.mgr.realm_band(13)
        self.assertIn("圣品", band["tiers"], "lv13 起必须有圣品来源")

    def test_top_band_unlocks_emperor_material(self):
        band = self.mgr.realm_band(35)
        self.assertIn("圣品", band["tiers"])
        self.assertIn("帝品", band["tiers"], "满级段必须接上帝品材料，供道品以上丹方使用")

    def test_low_route_caps_material_band(self):
        """v7.9：材料段取 min(玩家段, 路线段)，低阶路线不许蹭玩家的高阶池。"""
        self.assertTrue(self.mgr.material_cap_by_route, "cap_by_route 必须开启，否则低阶路线又会白嫖")
        upgraded = []
        for route in ROUTES.values():
            if int(route["min_level"]) > 24:
                continue
            band = self.mgr._material_band(_Player(35), route)
            if "帝品" in band["tiers"]:
                upgraded.append((route["name"], route["min_level"], sorted(band["tiers"])))
        self.assertEqual(upgraded, [], f"这些低阶路线对满级玩家出了帝品池：{upgraded}")

    def test_low_route_never_drops_emperor(self):
        """实证侧：满级玩家刷低阶路线，采样里一件帝品都不该出现。"""
        dipin = set()
        for key in ("herb_valley", "soul_forge"):
            route = ROUTES[key]
            dropped = 0
            for _ in range(1500):
                drop = self.mgr._roll_material_drop(_Player(35), route, {})
                if not drop:
                    continue
                dropped += 1
                if MATS[drop[0]]["tier"] == "帝品":
                    dipin.add((route["name"], drop[0]))
            self.assertGreater(dropped, 0, f"{route['name']} 一个材料都没掉，采样无效")
        self.assertEqual(dipin, set(), f"低阶路线掉了帝品：{sorted(dipin)}")

    def test_saint_weight_grows_with_level(self):
        weights = []
        for level in (13, 19, 25, 31):
            band = self.mgr.realm_band(level)
            total = sum(band["tiers"].values())
            weights.append(band["tiers"].get("圣品", 0) / total)
        for prev, cur in zip(weights, weights[1:]):
            self.assertLess(prev, cur, f"圣品占比没有随境界上升：{weights}")

    def test_higher_route_gives_better_materials(self):
        """v7.9：材料品阶由路线等级封顶，所以是「路线越难材料越好」，而不是刷低阶路线。

        旧用例拿同一把 lv0 药圃对比 lv5→lv33，封顶后各境界都停在炼气段，
        反而成了「升级没收益」——真正该涨的是路线等级。
        """
        avg_price = {}
        order = ["herb_valley", "pill_valley", "soul_swamp", "creation_altar"]
        for key in order:
            route = ROUTES[key]
            total = 0
            for _ in range(400):
                drop = self.mgr._roll_material_drop(_Player(35), route, {})
                if drop:
                    total += MATS[drop[0]]["price"]
            avg_price[key] = total / 400
        for prev, cur in zip(order, order[1:]):
            self.assertGreater(
                avg_price[cur], avg_price[prev],
                f"{ROUTES[cur]['name']} 的材料价值 {avg_price[cur]:,.0f} "
                f"不如 {ROUTES[prev]['name']} 的 {avg_price[prev]:,.0f}",
            )

    def test_same_band_route_level_does_not_matter(self):
        """同一境界段内的路线（lv25/26/27）材料期望应接近，不该只挑最短的那条。"""
        vals = {}
        for key in ("chaos_throne", "dao_arena", "primordial_peak"):
            route = ROUTES[key]
            total = 0
            for _ in range(1500):
                drop = self.mgr._roll_material_drop(_Player(27), route, {})
                if drop:
                    total += MATS[drop[0]]["price"] * drop[1]
            vals[key] = total / 1500
        lo, hi = min(vals.values()), max(vals.values())
        self.assertLess((hi - lo) / hi, 0.6, f"同段路线期望差得太多：{vals}")


class TestRiskyEvents(unittest.TestCase):
    def test_risky_events_roll_extra(self):
        """高风险必须靠多一次判定换回报，不能再像旧配置那样压低概率。"""
        for event in CFG["event_groups"]["risky"]:
            self.assertEqual(
                event.get("extra_rolls"), 1,
                f"风险事件 {event['key']} 缺少 extra_rolls",
            )

    def test_risky_chance_not_below_safe(self):
        safe_min = min(e["item_chance"] for e in CFG["event_groups"]["safe"])
        for event in CFG["event_groups"]["risky"]:
            self.assertGreaterEqual(
                event["item_chance"], safe_min,
                f"风险事件 {event['key']} 掉落率 {event['item_chance']} 低于最安全的 {safe_min}",
            )


class TestFallback(unittest.TestCase):
    def test_legacy_config_falls_back(self):
        """老存档的配置没有 drop_pools，必须还能用旧的 3 档表。"""
        mgr = make_manager()
        mgr.drop_pools = {}
        mgr.realm_bands = []
        mgr.drop_tables = {
            "low": [{"name": "灵草", "weight": 100, "min": 1, "max": 3}],
        }
        drop = mgr._roll_material_drop(_Player(0), ROUTES["scout"], {"drop_tier": "low"})
        self.assertIsNotNone(drop)
        self.assertEqual(drop[0], "灵草")
        self.assertGreaterEqual(drop[1], 1)

    def test_broken_pool_returns_none_not_crash(self):
        mgr = make_manager()
        mgr.drop_pools = {"灵植": {"凡品": []}}
        mgr.category_weights = {"灵植": 100}
        self.assertIsNone(
            mgr._roll_material_drop(_Player(0), ROUTES["herb_valley"], {})
        )


class TestQuantityScale(unittest.TestCase):
    def test_quantity_within_configured_bounds(self):
        mgr = make_manager()
        for level in (0, 15, 25, 33):
            for _ in range(200):
                drop = mgr._roll_material_drop(_Player(level), ROUTES["herb_valley"], {})
                if drop is None:
                    continue
                tier = MATS[drop[0]]["tier"]
                low, high = CFG["drop_quantity"][tier]
                scale = CFG["category_quantity_scale"]["灵植"]
                self.assertGreaterEqual(drop[1], 1)
                self.assertLessEqual(drop[1], round(high * scale))

    def test_cheap_categories_drop_more(self):
        """单价低的类别靠数量补，避免四条专精线收益差一倍。"""
        scale = CFG["category_quantity_scale"]
        self.assertGreater(scale["灵植"], scale["妖兽"])

    def test_fractional_quantity_is_not_rounded_up(self):
        """缩放系数 0.8 不能因为 round() 退化成 1.0。

        妖兽类没有凡品材料，炼气期只能回退到黄品；如果不按小数概率进位，
        0.8 系数会稳定给出 1.5 个（期望 1.2），缩放等于没生效。
        """
        mgr = make_manager()
        mgr.drop_pools = {"灵植": {"凡品": ["灵草"]}}
        mgr.realm_bands = [{"key": "t", "label": "炼气", "max_level": 99, "tiers": {"凡品": 100}}]
        mgr.drop_quantity = {"凡品": [1, 2]}
        mgr.category_quantity_scale = {"灵植": 0.5}
        total = sum(
            (drop[1] if (drop := mgr._roll_material_drop(_Player(0), ROUTES["herb_valley"], {})) else 0)
            for _ in range(4000)
        )
        # 期望 = 1.5 × 0.5 = 0.75；允许掉 0 个后不能被 round() 抬到 1.25 以上
        self.assertLess(total / 4000, 1.05)


class TestTierFallbackBalance(unittest.TestCase):
    """品阶回退不能让某条专精线凭「捡到更贵材料」白赚。"""

    PRICE = {name: meta["price"] for name, meta in MATS.items()}

    def _expected_value(self, mgr, category, level, n=1500):
        route = next(r for r in CFG["routes"] if r.get("product") == category)
        total = 0
        for _ in range(n):
            drop = mgr._roll_material_drop(_Player(level), route, {})
            if drop:
                total += self.PRICE.get(drop[0], 0) * drop[1]
        return total / n

    def test_lianqi_beast_route_not_dominant(self):
        """炼气期妖兽没有凡品，会向上回退到黄品；回退要打折，否则夜猎肥 50%+。"""
        mgr = make_manager()
        herb = self._expected_value(mgr, "灵植", 5)
        beast = self._expected_value(mgr, "妖兽", 5)
        self.assertLess(
            beast, herb * 1.3,
            f"炼气期夜猎({beast:,.0f}) 比采药({herb:,.0f}) 高太多，新人只会刷妖兽",
        )

    def test_categories_balanced_at_high_level(self):
        """合体期四类专精的期望产出差距要控制在 25% 以内。"""
        mgr = make_manager()
        values = [self._expected_value(mgr, c, 26) for c in ("灵植", "矿材", "灵液", "妖兽")]
        mid = sum(values) / len(values)
        spread = (max(values) - min(values)) / mid
        self.assertLess(spread, 0.25, f"四类产出失衡：{[round(v) for v in values]}")


class TestAbsoluteYield(unittest.TestCase):
    """历练是「材料第二来源」。v7.6 起分段结算并把总产出翻倍；
    帝品材料重定价后秘境材料价值同步上升，历练约为秘境的 0.5~0.8 倍。"""

    LEVEL = 26
    TOP_ROUTE = "混沌王座"
    RIFT_NAME = "太虚雷池"
    RIFT_LABEL = "二重"

    def _run_stats(self, runs=4000):
        """v7.7 回到一次性结算：时长减半后每趟 1 次判定，单位时间产出与 v7.6 持平。"""
        mgr = make_manager()
        route = next(r for r in CFG["routes"] if r["name"] == self.TOP_ROUTE)
        player = _Player(self.LEVEL)
        value = 0
        empty = 0
        for _ in range(runs):
            event = mgr._trigger_route_event(route)
            run_value = 0
            for entry in mgr._roll_drops(
                player, route, event, chance_scale=mgr.ITEM_CHANCE_SCALE
            ):
                run_value += MATS[entry[0]]["price"] * entry[1]
            value += run_value
            if run_value == 0:
                empty += 1
        minutes = route["duration"] / 60
        return value / runs * (60 / minutes), empty / runs

    def _rift_hourly(self):
        with open(RIFT_PATH, encoding="utf-8") as f:
            rifts = json.load(f)["rifts"]
        for rift in rifts:
            if rift["name"] != self.RIFT_NAME:
                continue
            for diff in rift["difficulties"]:
                if diff["label"] != self.RIFT_LABEL:
                    continue
                total_weight = sum(d["weight"] for d in diff["drops"])
                per_pick = sum(
                    d["weight"] / total_weight
                    * MATS.get(d["name"], {}).get("price", 0)
                    * (d.get("min", 1) + d.get("max", 1)) / 2
                    for d in diff["drops"]
                )
                picks = (
                    diff["item_chance"]
                    + diff["item_chance"] * diff["extra_drop_chance"] / 100
                ) / 100
                return per_pick * picks * (60 / (diff["duration"] / 60))
        raise AssertionError(f"找不到秘境 {self.RIFT_NAME}·{self.RIFT_LABEL}")

    def test_empty_run_is_the_norm(self):
        """v7.9：材料是历练的副产物，空手才是常态（修为/灵石才是主线）。

        空手率过高说明系数砍过头了（材料彻底消失），过低说明又回到「刷材料」定位。
        """
        _, empty = self._run_stats()
        self.assertGreater(empty, 0.3, f"空手率只有 {empty:.0%}，历练又变回材料产地了")
        self.assertLess(empty, 0.85, f"空手率 {empty:.0%} 太高，材料几乎绝迹")

    def test_hourly_value_is_one_fifth_of_rift(self):
        """v7.9 新定位：历练材料价值约为秘境最优档的 1/5（材料主产地是秘境）。"""
        adventure_hourly, _ = self._run_stats()
        ratio = adventure_hourly / self._rift_hourly()
        self.assertGreater(ratio, 0.12, f"历练只有秘境的 {ratio:.0%}，材料等于不存在了")
        self.assertLess(ratio, 0.30, f"历练达到秘境的 {ratio:.0%}，又抢了秘境的材料定位")


class TestLateHetiEmperorFarming(unittest.TestCase):
    """合体后期（lv30）能靠历练专精路线自给大乘丹的五件帝品材料。

    材料品阶自 v7.9.0 起同时受路线 ``min_level`` 封顶（``material_model.cap_by_route``），
    所以只有 min_level 落在「合体大乘」段（25~30）的路线才可能出帝品：
    混沌王座 25（妖兽）、命运织机 28（灵液）、宇宙之眼 29（矿材）、造化祭坛 30（灵植）。
    低阶路线（如 22 的天门叩关）即使玩家已到合体期也只出圣品，避免短冷却白嫖。
    这里按实际事件/判定次数模拟，而不是只看权重。
    """

    LEVEL = 30
    RUNS = 3000
    # 大乘丹：帝品灵植 ×2、矿材 ×1、妖兽 ×1、灵液 ×1
    PLAN = (
        ("灵植", 2, "creation_altar"),
        ("矿材", 1, "cosmos_eye"),
        ("妖兽", 1, "chaos_throne"),
        ("灵液", 1, "fate_loom"),
    )
    # v7.9 起材料系数为 0.33，历练只作补充；五件帝品约需 80 趟（帝品主来源是 lv25+ 秘境）。
    MAX_TRIPS = 100

    def _emperor_per_trip(self, mgr, route):
        player = _Player(self.LEVEL)
        got = Counter()
        for _ in range(self.RUNS):
            event = mgr._trigger_route_event(route)
            for entry in mgr._roll_drops(
                player, route, event, chance_scale=mgr.ITEM_CHANCE_SCALE
            ):
                material = MATS.get(entry[0])
                if material and material["tier"] == "帝品":
                    got[material["category"]] += entry[1]
        return {category: got[category] / self.RUNS for category in CATEGORIES}

    def test_emperor_materials_farmable_by_switching_routes(self):
        """按产物换专精路线，合理趟数内能凑齐一套大乘丹帝品材料。"""
        random.seed(20261110)
        mgr = make_manager()
        total = 0.0
        for category, need, key in self.PLAN:
            rate = self._emperor_per_trip(mgr, ROUTES[key])[category]
            self.assertGreater(
                rate, 0,
                f"lv{self.LEVEL} 的 {ROUTES[key]['name']} 掉不出帝品{category}",
            )
            total += need / rate
        self.assertLessEqual(
            total, self.MAX_TRIPS,
            f"合体后期凑齐大乘丹帝品材料需要约 {total:.0f} 趟历练，超过 {self.MAX_TRIPS} 趟",
        )

    def test_specialty_routes_beat_full_product_route(self):
        """一条「全产物」路线走到黑会慢很多；专精换线才是可行解。"""
        random.seed(20261110)
        mgr = make_manager()
        full = self._emperor_per_trip(mgr, ROUTES["primordial_peak"])
        for category, _need, key in self.PLAN:
            specialized = self._emperor_per_trip(mgr, ROUTES[key])[category]
            self.assertGreater(
                specialized, full[category],
                f"{ROUTES[key]['name']} 的帝品{category}产出没有高于太初之巅",
            )
        total = sum(need / self._emperor_per_trip(mgr, ROUTES[key])[category]
                    for category, need, key in self.PLAN)
        full_trips = sum(need / full[cat] for cat, need, _ in self.PLAN)
        self.assertGreater(
            full_trips, total * 2,
            f"专精换线 {total:.0f} 趟、全产物 {full_trips:.0f} 趟，专精优势不明显",
        )


class TestNoEarlyEmperor(unittest.TestCase):
    def test_no_emperor_material_before_heti(self):
        """lv24 及以下不能提前掉帝品，否则会砸穿合体前的丹方经济。"""
        mgr = make_manager()
        for level in (0, 12, 18, 24):
            self.assertNotIn("帝品", mgr.realm_band(level)["tiers"])
            player = _Player(level)
            for _ in range(400):
                drop = mgr._roll_material_drop(player, ROUTES["scout"], {})
                if drop:
                    self.assertNotEqual(
                        MATS[drop[0]]["tier"], "帝品",
                        f"lv{level} 提前掉出了帝品材料 {drop[0]}",
                    )

    def test_realm_band_boundary_at_great_vehicle(self):
        """大乘丹 lv27 必须落在合体大乘段，lv31 才切到渡劫段。"""
        mgr = make_manager()
        self.assertEqual(mgr.realm_band(27)["key"], "heti")
        self.assertEqual(mgr.realm_band(30)["key"], "heti")
        self.assertEqual(mgr.realm_band(31)["key"], "dujie")

    def test_low_route_is_capped_for_high_level_player(self):
        """路线封顶：lv30 刷 min_level=22 的天门叩关也掉不出帝品。"""
        mgr = make_manager()
        player = _Player(30)
        self.assertEqual(mgr._material_band(player, ROUTES["heaven_gate"])["key"], "huashen")
        for _ in range(400):
            drop = mgr._roll_material_drop(player, ROUTES["heaven_gate"], {})
            if drop:
                self.assertNotEqual(
                    MATS[drop[0]]["tier"], "帝品",
                    f"低阶路线掉出了帝品材料 {drop[0]}",
                )


class TestEmperorDropRateConfig(unittest.TestCase):
    def test_heti_emperor_share_is_enough_but_not_flooding(self):
        """合体大乘段帝品占比要够刷大乘丹，又不能高到让帝品变白菜。"""
        def share(band_key):
            band = next(b for b in CFG["realm_bands"] if b["key"] == band_key)
            total = sum(band["tiers"].values())
            return band["tiers"]["帝品"] / total

        heti = share("heti")
        dujie = share("dujie")
        self.assertGreaterEqual(heti, 0.20, f"合体段帝品占比只有 {heti:.0%}")
        self.assertLessEqual(heti, 0.35, f"合体段帝品占比 {heti:.0%} 过高")
        self.assertGreaterEqual(dujie, heti, "渡劫以上的帝品占比不能低于合体段")

    def test_emperor_pool_never_falls_back(self):
        """四类帝品池都必须真实存在，不能靠 TIER_ORDER 回退到别的品阶。"""
        mgr = make_manager()
        for category in CATEGORIES:
            names, actual = mgr._resolve_pool(category, "帝品")
            self.assertTrue(names, f"{category}/帝品 池为空")
            self.assertEqual(actual, "帝品", f"{category}/帝品 回退到了 {actual}")


if __name__ == "__main__":
    unittest.main()
