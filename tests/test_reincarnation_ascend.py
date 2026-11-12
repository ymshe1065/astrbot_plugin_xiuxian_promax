# tests/test_reincarnation_ascend.py
# 飞升：二次确认解析、大道馈赠跨世账本、仙缘商店记账、确认页文案
import json
import sys
import unittest
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


rules = _imp("core.reincarnation_rules")
ReincarnationManager = _imp("managers.reincarnation_manager").ReincarnationManager
ReincarnationHandlers = _imp("handlers.reincarnation_handlers").ReincarnationHandlers
Player = _imp("models").Player
ConfigManager = _imp("config_manager").ConfigManager


class _FakeConn:
    def __init__(self):
        # 记录事务内执行过的 SQL，供断言「上一世灵根随飞升事务写入 system_config」
        self.executed = []

    def system_config_writes(self):
        out = {}
        for sql, params in self.executed:
            if "INSERT INTO system_config" in (sql or "") and params:
                out[params[0]] = params[1]
        return out

    async def execute(self, sql=None, params=None, *a, **k):
        self.executed.append((sql, params))
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _FakeExt:
    def __init__(self):
        self.config = {}

    async def get_active_loan(self, user_id):
        return None

    async def get_system_config(self, key):
        return self.config.get(key)

    async def return_gifts_for_receiver(self, user_id, commit=True):
        return None

    async def set_user_free(self, user_id, commit=True):
        return None


class _FakeDB:
    def __init__(self):
        self.conn = _FakeConn()
        self.ext = _FakeExt()
        self.saved = {}

    async def update_player(self, player, commit=True):
        self.saved[player.user_id] = player


class TestAscendArgs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parse = staticmethod(_imp("handlers.reincarnation_handlers").parse_ascend_args)

    def test_bare_command_does_not_confirm(self):
        self.assertEqual(self.parse(""), ("", False))
        self.assertEqual(self.parse("   "), ("", False))

    def test_confirm_only(self):
        self.assertEqual(self.parse("确认"), ("", True))
        self.assertEqual(self.parse("确定"), ("", True))
        self.assertEqual(self.parse("执行"), ("", True))

    def test_heirloom_only_is_preview(self):
        self.assertEqual(self.parse("青锋剑"), ("青锋剑", False))

    def test_heirloom_then_confirm(self):
        self.assertEqual(self.parse("青锋剑 确认"), ("青锋剑", True))
        self.assertEqual(self.parse("青锋剑   确认 "), ("青锋剑", True))


class TestDaoGiftLedger(unittest.TestCase):
    """道基淬炼：从「扁平 +100」改成「按层数放大基础属性」。"""

    def _player(self):
        player = Player(user_id="u1")
        player.mental_power = 1000
        player.magic_damage = 200
        return player

    def test_grant_adds_stats_and_ledger(self):
        player = self._player()
        bonus = {}
        rules.grant_dao_gift(player, bonus)
        # 500 -> 5% 放大
        self.assertEqual(player.mental_power, 1050)
        self.assertEqual(player.magic_damage, 210)
        self.assertEqual(bonus["dao_gift_times"], 1)
        self.assertNotIn("dao_gift", bonus)
        self.assertEqual(rules.dao_gift_times(bonus), 1)

    def test_stacks_are_multiplicative_percent(self):
        player = self._player()
        bonus = {}
        for _ in range(3):
            rules.grant_dao_gift(player, bonus)
        self.assertAlmostEqual(rules.dao_gift_pct(bonus), 0.15, places=6)
        # 1000 -> 1050 -> 1102（.5 取偶）-> 1157
        self.assertEqual(player.mental_power, 1157)

    def test_apply_replays_after_reset(self):
        player = self._player()
        player.mental_power = 1000  # 模拟 generate_new_player_stats 之后的裸值
        player.magic_damage = 200
        rules.apply_dao_gift(player, {"dao_gift_times": 2})
        self.assertEqual(player.mental_power, 1100)
        self.assertEqual(player.magic_damage, 220)

    def test_legacy_flat_ledger_counts_once(self):
        """旧存档的扁平账本不再按数值重放，但至少保留一层（5%），不会归零。"""
        player = self._player()
        bonus = {"dao_gift": {"mental_power": 100, "magic_damage": 20}}
        self.assertEqual(rules.dao_gift_times(bonus), 1)
        rules.apply_dao_gift(player, bonus)
        self.assertEqual(player.mental_power, 1050)

    def test_dao_gift_is_uncapped(self):
        """商品一律不设上限：12 层就是 12 层，不再在第 8 层被钳住。"""
        bonus = {}
        for _ in range(12):
            rules.add_item(bonus, 3)
        self.assertEqual(rules.dao_gift_times(bonus), 12)
        self.assertAlmostEqual(rules.dao_gift_pct(bonus), 0.60, places=6)

    def test_bad_ledger_is_ignored(self):
        player = self._player()
        rules.apply_dao_gift(player, {"dao_gift": "not-a-dict"})
        self.assertEqual(player.mental_power, 1000)

    def test_parse_bonus_tolerates_broken_json(self):
        player = self._player()
        player.permanent_bonus = "{broken"
        self.assertEqual(rules.parse_bonus(player), {})


class TestFateShopCatalog(unittest.TestCase):
    def test_legacy_items_keep_their_prices(self):
        expected = {
            1: ("修炼加速", 100),
            2: ("转世资粮", 40),
            3: ("道基淬炼", 100),
            4: ("灵根洗炼", 25),
            5: ("破境感悟", 80),
            6: ("历练心得", 110),
            7: ("采药心得", 30),
            8: ("寻宝直觉", 40),
        }
        for item_id, (name, cost) in expected.items():
            item = rules.SHOP_ITEMS[item_id]
            self.assertEqual(item["name"], name)
            self.assertEqual(item["cost"], cost)

    def test_catalog_is_expanded_and_grouped(self):
        """商品目录扩到 50+ 项，且每一项都落在 8 个分组之一里。"""
        self.assertGreaterEqual(len(rules.SHOP_ITEMS), 50)
        group_names = [name for name, _ in rules.SHOP_GROUPS]
        self.assertEqual(len(group_names), 8)
        grouped_total = 0
        for name in group_names:
            ids = rules.items_in_group(name)
            self.assertTrue(ids, f"分组【{name}】没有商品")
            grouped_total += len(ids)
            for item_id in ids:
                self.assertEqual(rules.SHOP_ITEMS[item_id]["group"], name)
        self.assertEqual(grouped_total, len(rules.SHOP_ITEMS))

    def test_numeric_items_are_uncapped(self):
        """数值型商品一律 cap=0（无上限），只有结构性解锁才留硬顶。"""
        structural = {26, 31, 32, 39, 43, 44, 46, 48, 49, 50, 51}
        for item_id, item in rules.SHOP_ITEMS.items():
            if item_id in structural:
                self.assertGreater(item["cap"], 0, f"【{item['name']}】应保留硬顶")
            else:
                self.assertEqual(item["cap"], 0, f"【{item['name']}】不该封顶")

    def test_caps_stop_purchases(self):
        bonus = {}
        for _ in range(10):
            rules.add_item(bonus, 26)  # 天工开物 上限 3 档
        self.assertEqual(rules.item_owned(bonus, 26), 3)
        self.assertTrue(rules.item_at_cap(bonus, 26))
        self.assertFalse(rules.item_at_cap(bonus, 1))  # 修炼加速不封顶
        # 无上限商品：买多少次就有多少层
        for _ in range(10):
            rules.add_item(bonus, 7)
        self.assertEqual(rules.item_owned(bonus, 7), 10)
        self.assertFalse(rules.item_at_cap(bonus, 7))

    def test_effect_getters(self):
        bonus = {
            "global_cultivation_multiplier": 0.3,
            "breakthrough_bonus": 0.02,
            "adventure_bonus": 0.4,
            "rift_item_bonus": 0.09,
            "rift_equip_bonus": 0.3,
            "root_reroll_stock": 4,
            "newlife_trips": 6,
        }
        player = Player(user_id="u1")
        player.permanent_bonus = json.dumps(bonus, ensure_ascii=False)
        self.assertAlmostEqual(rules.permanent_cultivation_mult(player), 0.3)
        self.assertAlmostEqual(rules.permanent_breakthrough_bonus(player), 0.02)
        self.assertAlmostEqual(rules.permanent_adventure_bonus(player), 0.4)
        self.assertAlmostEqual(rules.permanent_rift_item_bonus(player), 0.09)
        self.assertAlmostEqual(rules.permanent_rift_equip_bonus(player), 0.3)
        self.assertEqual(rules.root_reroll_stock(player), 4)
        self.assertAlmostEqual(rules.newlife_trips(player), 6)

    def test_consume_reroll_stock(self):
        bonus = {"root_reroll_stock": 2}
        self.assertEqual(rules.consume_root_reroll(bonus), 1)
        self.assertEqual(rules.consume_root_reroll(bonus), 0)
        self.assertEqual(rules.consume_root_reroll(bonus), 0)  # 不会变负

    def test_reroll_budget_text(self):
        text = rules.format_reroll_budget(3, 5)
        self.assertIn("免费重抽剩余 3/5", text)
        self.assertIn("洗炼额度 5 次", text)

    def test_newlife_gold_scales_with_escort_trips(self):
        EscortManager = _imp("managers.escort_manager").EscortManager
        player = Player(user_id="u1")
        player.permanent_bonus = json.dumps({"newlife_trips": 4}, ensure_ascii=False)
        base = EscortManager.calculate_escort_gold_base(25)
        self.assertEqual(rules.newlife_gold(player, 25), base * 4)


class TestFateShop(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = ConfigManager(ROOT)

    def _manager(self):
        db = _FakeDB()
        return ReincarnationManager(db, self.cfg, None), db

    async def test_dao_gift_is_written_to_permanent_bonus(self):
        mgr, db = self._manager()
        player = Player(user_id="u1")
        player.fate_points = 600
        player.mental_power = 1000
        ok, msg = await mgr.buy_fate_shop_item(player, 3)
        self.assertTrue(ok, msg)
        self.assertEqual(player.fate_points, 500)
        data = json.loads(player.permanent_bonus)
        self.assertEqual(data["dao_gift_times"], 1)
        self.assertEqual(player.mental_power, 1050)
        self.assertIn("道基淬炼", msg)

    async def test_speed_purchase_stacks(self):
        mgr, db = self._manager()
        player = Player(user_id="u1")
        player.fate_points = 250
        await mgr.buy_fate_shop_item(player, 1)
        await mgr.buy_fate_shop_item(player, 1)
        data = json.loads(player.permanent_bonus)
        self.assertAlmostEqual(data["global_cultivation_multiplier"], 0.2)

    async def test_reroll_stock_is_granted(self):
        mgr, db = self._manager()
        player = Player(user_id="u1")
        player.fate_points = 100
        ok, msg = await mgr.buy_fate_shop_item(player, 4)
        self.assertTrue(ok, msg)
        self.assertEqual(rules.root_reroll_stock(player), 1)
        self.assertIn("灵根洗炼", msg)

    async def test_purchase_rejected_at_cap(self):
        mgr, db = self._manager()
        player = Player(user_id="u1")
        player.fate_points = 100000
        player.permanent_bonus = json.dumps({"forge_affix_bonus": 3}, ensure_ascii=False)
        ok, msg = await mgr.buy_fate_shop_item(player, 26)
        self.assertFalse(ok)
        self.assertIn("已满 3 层", msg)

    async def test_purchase_rejected_without_points(self):
        mgr, db = self._manager()
        player = Player(user_id="u1")
        player.fate_points = 10
        ok, msg = await mgr.buy_fate_shop_item(player, 6)
        self.assertFalse(ok)
        self.assertIn("仙缘点不足", msg)

    async def test_unknown_item(self):
        mgr, db = self._manager()
        ok, msg = await mgr.buy_fate_shop_item(Player(user_id="u1"), 99)
        self.assertFalse(ok)
        self.assertIn("1~51", msg)


class TestAscendPreview(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = ConfigManager(ROOT)
        levels = cls.cfg.get_level_data("灵修")
        cls.max_level = len(levels) - 1
        cls.max_name = levels[-1]["level_name"]
        cls.max_exp = levels[-1]["exp_needed"]

    def _manager(self):
        return ReincarnationManager(_FakeDB(), self.cfg, None)

    def _maxed_player(self):
        player = Player(user_id="u1")
        player.cultivation_type = "灵修"
        player.level_index = self.max_level
        player.experience = self.max_exp
        player.reincarnation_count = 2
        return player

    async def test_preview_rejects_unmaxed(self):
        mgr = self._manager()
        player = Player(user_id="u1")
        player.cultivation_type = "灵修"
        player.level_index = 3
        ok, msg = await mgr.preview_reincarnate(player)
        self.assertFalse(ok)
        self.assertIn("境界尚未达到巅峰", msg)
        self.assertIn(self.max_name, msg)

    async def test_preview_lists_gains_and_confirm_command(self):
        mgr = self._manager()
        player = self._maxed_player()
        ok, msg = await mgr.preview_reincarnate(player)
        self.assertTrue(ok, msg)
        # 确认页必须写清代价，并给出确认指令
        self.assertIn("飞升确认", msg)
        self.assertIn("不可撤销", msg)
        self.assertIn("/飞升 确认", msg)
        self.assertIn("第 3 次完美转生", msg)
        # 点数 = 100 + int(log10(exp)*10) + 2*50
        self.assertIn("预计获得仙缘点 307", msg)
        self.assertIn("将保留", msg)
        # 新规则：灵根/职业/生活职业不再保留，确认页必须写清
        self.assertIn("将重塑：灵根 · 职业 · 生活职业", msg)

    async def test_preview_blocks_unknown_heirloom(self):
        mgr = self._manager()
        player = self._maxed_player()
        ok, msg = await mgr.preview_reincarnate(player, "不存在的剑")
        self.assertFalse(ok)
        self.assertIn("都没有", msg)

    async def test_try_reincarnate_enters_newlife(self):
        db = _FakeDB()
        mgr = ReincarnationManager(db, self.cfg, None)
        player = self._maxed_player()
        player.spiritual_root = "火灵根"
        player.permanent_bonus = json.dumps({"dao_gift_times": 8}, ensure_ascii=False)
        player.life_skill = "炼丹师"
        ok, msg = await mgr.try_reincarnate(player)
        self.assertTrue(ok, msg)
        self.assertEqual(player.level_index, 0)
        self.assertEqual(player.reincarnation_count, 3)
        self.assertIn("完美转生次数：3", msg)
        # 飞升不再保留灵根/职业/生活职业，进入「待重塑」
        self.assertEqual(player.cultivation_type, rules.NEWLIFE_MARKER)
        self.assertEqual(player.spiritual_root, rules.NEWLIFE_ROOT_MARKER)
        self.assertEqual(player.life_skill, "")
        self.assertIn("我要修仙", msg)
        self.assertIn("5 次免费重抽", msg)
        # 道基淬炼 8 层 = +40%：基础精神必然高于 0
        self.assertGreaterEqual(player.mental_power, 140)
        # 上一世灵根随飞升事务落库：道体重塑按它保证「必出高一档灵根」
        writes = db.conn.system_config_writes()
        self.assertEqual(writes.get("rebirth_prev_root_u1"), "火灵根")


class TestAscendArgOptions(unittest.IsolatedAsyncioTestCase):
    """网页弹层：/飞升 先选传家宝，再选「确认」；两个字段都来自真实存档。"""

    @classmethod
    def setUpClass(cls):
        cls.ao = _imp("webchat_room.arg_options")
        cls.cmds = _imp("webchat_room.commands")
        cls.cfg = ConfigManager(ROOT)

    def test_field_plan_has_two_steps(self):
        plan = self.ao.FIELD_PLANS["飞升"]
        self.assertEqual([f["key"] for f in plan], ["heirloom", "confirm"])
        self.assertTrue(plan[0]["optional"])
        self.assertEqual(self.cmds.ARG_SPEC["飞升"]["min"], 0)

    async def test_heirloom_source_lists_ring_and_equipped(self):
        import types

        player = types.SimpleNamespace(
            level_index=35,
            cultivation_type="灵修",
            permanent_bonus="{}",
            get_storage_ring_items=lambda: {"玄铁剑": 1, "炼气丹": 3},
        )
        plugin = types.SimpleNamespace(config_manager=self.cfg)
        # 没有装备管理器时只走储物戒分支，不应抛错
        options, msg = await self.ao._src_heirloom_items(plugin, player, {}, [])
        values = [o["value"] for o in options]
        self.assertIn("玄铁剑", values)
        self.assertIn("炼气丹", values)
        self.assertEqual(msg, "")

    async def test_heirloom_source_warns_when_sealed(self):
        import types

        player = types.SimpleNamespace(
            level_index=35,
            cultivation_type="灵修",
            permanent_bonus=json.dumps({"heirloom": {"name": "旧剑", "sealed": True}}),
            get_storage_ring_items=lambda: {"玄铁剑": 1},
        )
        plugin = types.SimpleNamespace(config_manager=self.cfg)
        _options, msg = await self.ao._src_heirloom_items(plugin, player, {}, [])
        self.assertIn("旧剑", msg)

    async def test_confirm_source_only_one_option(self):
        options, _msg = await self.ao._src_ascend_confirm(None, None, {}, [])
        self.assertEqual([o["value"] for o in options], ["确认"])


if __name__ == "__main__":
    unittest.main()
