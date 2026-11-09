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

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")


class _StubLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


api_mod.logger = _StubLogger()
api_mod.AstrBotConfig = dict
event_mod.AstrMessageEvent = object
api_mod.event = event_mod
all_mod = types.ModuleType("astrbot.api.all")
all_mod.At = object
all_mod.Plain = object
api_mod.all = all_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.api.event", event_mod)
sys.modules.setdefault("astrbot.api.all", all_mod)

from astrbot_plugin_xiuxian_promax.managers.adventure_manager import AdventureManager  # noqa: E402

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
        valid = {"凡品", "黄品", "玄品", "圣品"}
        for band in CFG["realm_bands"]:
            for tier, weight in band["tiers"].items():
                self.assertIn(tier, valid, f"{band['label']} 出现未知品阶 {tier}")
                self.assertGreater(weight, 0, f"{band['label']}/{tier} 权重必须为正")

    def test_pool_materials_exist(self):
        for category, tiers in CFG["drop_pools"].items():
            self.assertIn(category, CATEGORIES)
            for tier, names in tiers.items():
                self.assertIn(tier, {"凡品", "黄品", "玄品", "圣品"})
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

    def test_top_band_is_pure_saint(self):
        band = self.mgr.realm_band(35)
        self.assertEqual(set(band["tiers"]), {"圣品"})

    def test_saint_weight_grows_with_level(self):
        weights = []
        for level in (13, 19, 25, 31):
            band = self.mgr.realm_band(level)
            total = sum(band["tiers"].values())
            weights.append(band["tiers"].get("圣品", 0) / total)
        for prev, cur in zip(weights, weights[1:]):
            self.assertLess(prev, cur, f"圣品占比没有随境界上升：{weights}")

    def test_higher_level_gives_better_materials(self):
        """同一路线、同一类别，境界越高拿到的材料越值钱。"""
        route = ROUTES["herb_valley"]
        avg_price = {}
        for level in (5, 15, 21, 27, 33):
            total = 0
            for _ in range(300):
                drop = self.mgr._roll_material_drop(_Player(level), route, {})
                total += MATS[drop[0]]["price"]
            avg_price[level] = total / 300
        levels = sorted(avg_price)
        for prev, cur in zip(levels, levels[1:]):
            self.assertGreater(
                avg_price[cur], avg_price[prev],
                f"lv{cur} 的材料价值 {avg_price[cur]:,.0f} 不如 lv{prev} 的 {avg_price[prev]:,.0f}",
            )


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
            mgr._roll_material_drop(_Player(0), ROUTES["herb_valley"], {})[1]
            for _ in range(4000)
        )
        # 期望 = 1.5 × 0.5 = 0.75，但至少给 1 个 → 落在 [1, 1.5] 之间
        # 若用 round()，1×0.5→0/2×0.5→1，均值会被拉到 1.25 以上
        self.assertLess(total / 4000, 1.35)


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
    """历练是「材料第二来源」。v7.6 起分段结算并把总产出翻倍，
    历练材料价值已接近秘境（约 0.9~1.0 倍），这是需求方明确要求的结果。"""

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

    def test_empty_run_rate_is_acceptable(self):
        """全程空手的比例要压到 15% 以内：分段后每段都有机会。"""
        _, empty = self._run_stats()
        self.assertLess(empty, 0.15, f"空手率 {empty:.0%} 太高")

    def test_hourly_value_is_on_par_with_rift(self):
        """历练每小时材料价值约为秘境的 75%~125%（翻倍后与秘境基本持平）。"""
        adventure_hourly, _ = self._run_stats()
        ratio = adventure_hourly / self._rift_hourly()
        self.assertGreater(ratio, 0.75, f"历练只有秘境的 {ratio:.0%}，翻倍没落地")
        self.assertLess(ratio, 1.25, f"历练达到秘境的 {ratio:.0%}，再高就没人去秘境了")


if __name__ == "__main__":
    unittest.main()
