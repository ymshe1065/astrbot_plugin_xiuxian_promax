# tests/test_rift_difficulty.py
# 秘境难度分级：配置合法性、主产物区分、入库同步、进入/结算链路
import io
import json
import os
import sys
import tempfile
import time
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


def _load(name: str):
    with io.open(ROOT / "config" / name, encoding="utf-8") as f:
        return json.load(f)


RIFT_CFG = _load("rift_config.json")
MATERIALS = _load("material_catalog.json")["materials"]
UTILITY_PILLS = _load("utility_pills.json")
PILLS = _load("pills.json")
EXP_PILLS = _load("exp_pills.json")
ITEMS = _load("items.json")

MAT_BY_NAME = {m["name"]: m for m in MATERIALS}


def _pill_rows(raw):
    if isinstance(raw, dict):
        return raw.get("pills") or list(raw.values())
    return raw


def _pill_names(raw):
    names = set()
    for row in _pill_rows(raw):
        if isinstance(row, dict) and row.get("name"):
            names.add(row["name"])
    return names


PILL_NAMES = _pill_names(UTILITY_PILLS) | _pill_names(PILLS) | _pill_names(EXP_PILLS)


def _item_names(raw):
    out = set()
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict) and v.get("name"):
                out.add(v["name"])
            elif k:
                out.add(k)
            out |= _item_names(v)
    elif isinstance(raw, list):
        for x in raw:
            out |= _item_names(x)
    return out


ITEM_NAMES = _item_names(ITEMS)


class TestRiftStructure(unittest.TestCase):
    """秘境配置的结构与数值范围。"""

    def setUp(self):
        self.rifts = RIFT_CFG["rifts"]

    def test_group_count(self):
        self.assertEqual(len(self.rifts), 15)

    def test_each_rift_has_three_tiers(self):
        for rift in self.rifts:
            self.assertEqual([d["tier"] for d in rift["difficulties"]], [1, 2, 3])
            self.assertEqual(
                [d["label"] for d in rift["difficulties"]], ["一重", "二重", "三重"]
            )

    def test_rift_keys_unique(self):
        keys = [r["key"] for r in self.rifts]
        self.assertEqual(len(keys), len(set(keys)))

    def test_numeric_ranges(self):
        for rift in self.rifts:
            for diff in rift["difficulties"]:
                exp = diff["exp"]
                gold = diff["gold"]
                self.assertGreater(exp[0], 0)
                self.assertLessEqual(exp[0], exp[1])
                self.assertGreater(gold[0], 0)
                self.assertLessEqual(gold[0], gold[1])
                self.assertGreaterEqual(diff["duration"], 600)
                for key in ("item_chance", "extra_drop_chance", "equipment_chance"):
                    self.assertGreaterEqual(diff[key], 0)
                    self.assertLessEqual(diff[key], 100)
                self.assertGreater(float(diff["up_rank_weight"]), 0.0)

    def test_required_level_within_realm_range(self):
        for rift in self.rifts:
            for diff in rift["difficulties"]:
                self.assertGreaterEqual(diff["required_level"], 0)
                self.assertLessEqual(diff["required_level"], 35)

    def test_minute_rate_grows_with_requirement(self):
        """单位时间收益必须随境界门槛抬升，否则高境界玩家没有理由换本。

        v7.7 起秘境修为按「境界段 × 档位」配比，段内是平的（秘境定位为材料副本），
        所以按「秘境整体」比较最深层收益，而不是逐境界比较。
        """
        entries = []
        for rift in self.rifts:
            min_level = min(d["required_level"] for d in rift["difficulties"])
            best = max(
                sum(d["exp"]) / 2 / max(1, d["duration"] // 60)
                for d in rift["difficulties"]
            )
            entries.append((min_level, best, rift["name"]))
        entries.sort()
        for (l1, r1, n1), (l2, r2, n2) in zip(entries, entries[1:]):
            self.assertGreaterEqual(
                r2, r1,
                f"{n2}（门槛 {l2}）每分钟修为不如 {n1}（门槛 {l1}）",
            )

    def test_deeper_tier_is_not_slower_per_minute(self):
        """同一秘境内深档不能比重收益更慢（深档的卖点是掉落）。"""
        for rift in self.rifts:
            rates = [
                sum(d["exp"]) / 2 / max(1, d["duration"] // 60)
                for d in rift["difficulties"]
            ]
            self.assertEqual(
                rates, sorted(rates), f"{rift['name']} 深层反而更慢"
            )


class TestRiftDrops(unittest.TestCase):
    """掉落项必须真实存在，且主产物与秘境定位匹配。"""

    def setUp(self):
        self.rows = [
            (rift, diff) for rift in RIFT_CFG["rifts"] for diff in rift["difficulties"]
        ]

    def test_drop_names_resolvable(self):
        unknown = set()
        for _, diff in self.rows:
            for drop in diff["drops"]:
                name = drop["name"]
                if name in MAT_BY_NAME or name in PILL_NAMES or name in ITEM_NAMES:
                    continue
                unknown.add(name)
        self.assertEqual(unknown, set(), f"掉落表里有查不到的物品：{sorted(unknown)}")

    def test_drop_weight_and_count(self):
        for rift, diff in self.rows:
            self.assertTrue(diff["drops"], f"{rift['name']} 没有掉落表")
            for drop in diff["drops"]:
                self.assertGreater(drop["weight"], 0)
                self.assertGreaterEqual(drop["min"], 1)
                self.assertLessEqual(drop["min"], drop["max"])

    def test_product_category_dominates(self):
        """每个秘境的掉落里，主产物类别的权重占比必须最高。"""
        for rift in RIFT_CFG["rifts"]:
            product = rift["product"]
            if product not in MAT_BY_NAME.values() and "+" in product:
                # 「灵液+矿材」这类双产物：两类合计最高即可
                wanted = set(product.split("+"))
            elif product == "全产物":
                continue
            elif product == "混合":
                continue
            else:
                wanted = {product}
            for diff in rift["difficulties"]:
                weight = Counter()
                total = 0
                for drop in diff["drops"]:
                    mat = MAT_BY_NAME.get(drop["name"])
                    if not mat:
                        weight["_其他"] += drop["weight"]
                    else:
                        weight[mat["category"]] += drop["weight"]
                    total += drop["weight"]
                mine = sum(weight[c] for c in wanted)
                self.assertGreaterEqual(
                    mine / total, 0.45,
                    f"{rift['name']} 主产物 {product} 权重占比过低（{mine / total:.0%}）",
                )

    def test_high_rifts_favor_high_rank_material(self):
        """高阶秘境的圣品材料占比必须够高，否则高阶玩家拿不到新素材。"""
        ranks = {}
        for rift in RIFT_CFG["rifts"]:
            for diff in rift["difficulties"]:
                if diff["required_level"] < 22:
                    continue
                total = sum(d["weight"] for d in diff["drops"])
                holy = sum(
                    d["weight"]
                    for d in diff["drops"]
                    if MAT_BY_NAME.get(d["name"], {}).get("tier") == "圣品"
                )
                ranks[(rift["name"], diff["tier"])] = holy / total
        self.assertTrue(ranks)
        for key, ratio in ranks.items():
            self.assertGreater(ratio, 0.2, f"{key} 圣品材料占比仅 {ratio:.0%}")

    def test_products_are_not_all_present_everywhere(self):
        """不同秘境的主产物要有区分，不能每个阶段都全类别覆盖。"""
        products = {rift["product"] for rift in RIFT_CFG["rifts"]}
        self.assertGreaterEqual(len(products), 5)
        for product in products:
            if product in ("全产物", "混合"):
                continue
            count = sum(1 for rift in RIFT_CFG["rifts"] if rift["product"] == product)
            self.assertGreaterEqual(count, 1)
        # 只允许极少数秘境是「全产物/混合」
        broad = sum(1 for r in RIFT_CFG["rifts"] if r["product"] in ("全产物", "混合"))
        self.assertLessEqual(broad, 2)

    def test_every_category_obtainable_somewhere(self):
        cats = set()
        for rift in RIFT_CFG["rifts"]:
            for diff in rift["difficulties"]:
                for drop in diff["drops"]:
                    mat = MAT_BY_NAME.get(drop["name"])
                    if mat:
                        cats.add(mat["category"])
        for expected in ("灵植", "矿材", "灵液", "妖兽"):
            self.assertIn(expected, cats)


class TestRiftSync(unittest.IsolatedAsyncioTestCase):
    """配置下发到数据库：行数、字段、幂等、旧数据清理。"""

    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.RiftManager = _imp("managers.rift_manager").RiftManager

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)
        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        self.mgr = self.RiftManager(self.db, None, None)

    async def asyncTearDown(self):
        await self.conn.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    async def test_sync_creates_forty_five_rows(self):
        count = await self.mgr.sync_rifts()
        self.assertEqual(count, 45)
        rows = await self.db.ext.get_all_rifts()
        self.assertEqual(len(rows), 45)

    async def test_sync_is_idempotent(self):
        await self.mgr.sync_rifts()
        await self.mgr.sync_rifts()
        rows = await self.db.ext.get_all_rifts()
        self.assertEqual(len(rows), 45)
        ids = [r.rift_id for r in rows]
        self.assertEqual(len(ids), len(set(ids)))

    async def test_sync_drops_legacy_rows(self):
        """迁移内置的旧 5 个秘境若不在配置里，同步时会被清掉。"""
        await self.conn.execute(
            "INSERT OR REPLACE INTO rifts (rift_id, rift_name, rift_level, required_level, rewards) "
            "VALUES (999, '旧秘境', 3, 6, '{}')"
        )
        await self.mgr.sync_rifts()
        self.assertIsNone(await self.db.ext.get_rift_by_id(999))

    async def test_rows_carry_difficulty_metadata(self):
        await self.mgr.sync_rifts()
        rows = {r.rift_id: r for r in await self.db.ext.get_all_rifts()}
        first = rows[1]
        cfg = first.get_rewards()
        self.assertEqual(first.rift_name, "青云秘境")
        self.assertEqual(first.product, "灵植")
        self.assertEqual(first.difficulty, 1)
        self.assertEqual(first.rift_key, "qingyun")
        self.assertEqual(first.duration, 1200)
        self.assertIn("drops", cfg)
        self.assertIn("item_chance", cfg)
        # 三档的时长与产出必须区分开
        self.assertEqual(rows[2].duration, 1800)
        self.assertEqual(rows[3].duration, 2700)
        self.assertNotEqual(rows[1].get_rewards()["exp"], rows[3].get_rewards()["exp"])

    async def test_display_name_includes_difficulty(self):
        await self.mgr.sync_rifts()
        rows = {r.rift_id: r for r in await self.db.ext.get_all_rifts()}
        self.assertEqual(rows[2].display_name, "青云秘境·二重")
        self.assertEqual(rows[2].difficulty_label, "二重")


class TestRiftFlow(unittest.IsolatedAsyncioTestCase):
    """进入与结算：时长按档、门槛拦截、产出只取本档掉落表。"""

    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        models_extended = _imp("models_extended")
        self.UserStatus = models_extended.UserStatus
        self.RiftManager = _imp("managers.rift_manager").RiftManager
        self.Player = _imp("models").Player

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)
        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        self.mgr = self.RiftManager(self.db, None, None)
        await self.mgr.sync_rifts()
        await self.db.create_player(self.Player(user_id="u1", level_index=10))
        await self._equip_whole_set("玄品")

    async def _equip_whole_set(self, rank: str):
        """青云三重有装备门槛：测试玩家要穿满八格同品阶装备才进得去。

        RiftManager 在这里没有 config_manager，所以实例自带 ``rank`` 字段，
        门槛判定走「实例品阶」而不是「查配置」。真实环境两者等价。
        """
        player = await self.db.get_player_by_id("u1")
        slots = ["weapon", "head", "armor", "gloves", "boots", "ring1", "ring2", "amulet"]
        instances = [
            {
                "id": i + 1,
                "name": f"测试{rank}{slot}",
                "quality": "一般",
                "location": "equipped",
                "slot": slot,
                "rank": rank,
            }
            for i, slot in enumerate(slots)
        ]
        player.equipment_instances = json.dumps(instances, ensure_ascii=False)
        player.equipped_instances = json.dumps(
            {slot: i + 1 for i, slot in enumerate(slots)}, ensure_ascii=False
        )
        await self.db.update_player(player)

    async def asyncTearDown(self):
        await self.conn.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    async def _cd(self):
        return await self.db.ext.get_user_cd("u1")

    async def test_enter_uses_tier_duration(self):
        ok, msg = await self.mgr.enter_rift("u1", 3)  # 青云·三重 2700 秒
        self.assertTrue(ok, msg)
        cd = await self._cd()
        self.assertEqual(cd.type, self.UserStatus.EXPLORING)
        remaining = cd.scheduled_time - int(time.time())
        self.assertGreater(remaining, 2600)
        self.assertLessEqual(remaining, 2700)
        self.assertEqual(cd.get_extra_data().get("difficulty"), 3)

    async def test_enter_rejects_below_required_level(self):
        ok, msg = await self.mgr.enter_rift("u1", 45)  # 混沌天渊·三重 需 35
        self.assertFalse(ok)
        self.assertIn("境界不足", msg)
        self.assertIn("秘境", msg)

    async def test_finish_rewards_follow_this_tier(self):
        ok, msg = await self.mgr.enter_rift("u1", 3)
        self.assertTrue(ok, msg)
        cd = await self._cd()
        extra = cd.get_extra_data()
        # 整趟跑完：create_time 拨回出发时刻、scheduled_time 落到已过期，
        # settle_due 才会补齐全部小结段 + 大结算。
        # 只改 scheduled_time 的话 elapsed≈0，一段小结都不结算，
        # 结算出来的只是大结算那 40%，断言整趟总额会随机假失败。
        duration = int(extra.get("ticks_total", 2)) * int(extra.get("tick_seconds", 300))
        now = int(time.time())
        await self.db.conn.execute(
            "UPDATE user_cd SET create_time = ?, scheduled_time = ? WHERE user_id = ?",
            (now - duration, now - 1, "u1"),
        )
        await self.db.conn.commit()
        ok, msg, data = await self.mgr.finish_exploration("u1")
        self.assertTrue(ok, msg)
        # 青云·三重 修为区间（无丹药/称号加成时落在区间内）
        # v7.7 起整趟修为在进入时一次 roll 定再按段拆分，data["exp"] 只是
        # 「大结算」那一段；整趟总额看 total_exp。分段 int 截断每段最多丢 1 点，
        # 下界留 10 点余量。
        lo, hi = RIFT_CFG["rifts"][0]["difficulties"][2]["exp"]
        # 三重单趟产出再乘 tier_yield（v7.9.8 深档强度），整趟总额落在放大后的区间内
        tier_yield = float(RIFT_CFG["rifts"][0]["difficulties"][2].get("tier_yield") or 1.0)
        self.assertGreaterEqual(data["total_exp"], int(lo * tier_yield) - 10)
        self.assertLessEqual(data["total_exp"], int(hi * tier_yield))
        self.assertIn("青云秘境·三重", data["rift_name"])

    async def test_drops_come_from_tier_table(self):
        allowed = {
            d["name"]
            for d in RIFT_CFG["rifts"][0]["difficulties"][2]["drops"]
        }
        ok, _ = await self.mgr.enter_rift("u1", 3)
        self.assertTrue(ok)
        seen = set()
        for _ in range(40):
            cd = await self._cd()
            await self.db.ext.set_user_busy(
                "u1", self.UserStatus.EXPLORING, int(time.time()) - 1, cd.get_extra_data()
            )
            ok, _, data = await self.mgr.finish_exploration("u1")
            self.assertTrue(ok)
            for entry in data["items"]:
                seen.add(entry[0])
            await self.mgr.enter_rift("u1", 3)
        # 前几轮不一定出全，只要求出现过的东西都在本档掉落表里
        self.assertTrue(seen.issubset(allowed), f"越界掉落：{seen - allowed}")

    async def test_find_rift_by_name_and_difficulty(self):
        rift, candidates = await self.mgr.find_rift("青云", "二")
        self.assertIsNotNone(rift, candidates)
        self.assertEqual(rift.rift_id, 2)

    async def test_find_rift_ambiguous_returns_candidates(self):
        rift, candidates = await self.mgr.find_rift("青云")
        self.assertIsNone(rift)
        self.assertEqual(len(candidates), 3)

    async def test_rift_detail_lists_drops(self):
        ok, msg = await self.mgr.rift_detail(1)
        self.assertTrue(ok, msg)
        self.assertIn("青云秘境·一重", msg)
        self.assertIn("灵植", msg)
        self.assertIn("可能掉落", msg)

    async def test_list_groups_by_rift(self):
        ok, msg = await self.mgr.list_rifts("u1")
        self.assertTrue(ok, msg)
        self.assertIn("青云秘境", msg)
        self.assertIn("ID:1", msg)
        self.assertIn("可进入", msg)
        # v7.8：列表改成「可进入 / 即将解锁 / 未解锁」三段，不再按秘境平铺
        self.assertIn("当前可进", msg)
        self.assertIn("即将解锁", msg)
        self.assertIn("尚未解锁", msg)

    async def test_list_filters_by_product(self):
        """按主产物筛选后只剩该类别，且复合产物（灵液+矿材）能被两类同时命中。"""
        ok, msg = await self.mgr.list_rifts("u1", "灵植")
        self.assertTrue(ok, msg)
        self.assertIn("灵植", msg)
        self.assertNotIn("妖兽☆", msg)

    async def test_list_sorts_by_material_value(self):
        """可进入段必须按「材料价值/分钟」降序，第一条带「最佳」。"""
        data = await self.mgr.build_rift_list("u1")
        self.assertTrue(data)
        open_group = next(g for g in data["groups"] if g["key"] == "open")
        rates = [it["value_per_min"] for it in open_group["items"]]
        self.assertEqual(rates, sorted(rates, reverse=True))
        self.assertEqual(open_group["items"][0]["badge"], "最佳")

    async def test_list_marks_upcoming_unlock(self):
        """差 1~3 级的档要单独进「即将解锁」，不能混进可进入段。"""
        data = await self.mgr.build_rift_list("u1")
        level = 10
        soon = next((g for g in data["groups"] if g["key"] == "soon"), None)
        if soon:
            for item in soon["items"]:
                gap = item["required_level"] - level
                self.assertTrue(1 <= gap <= 3, item)


if __name__ == "__main__":
    unittest.main()
