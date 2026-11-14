# tests/test_fate_shop_panel.py
# 网页聊天室「仙缘」面板：快照结构、点卡片直接买、灵石换仙缘点
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
panel = _imp("webchat_room.fate_shop_panel")
ReincarnationManager = _imp("managers.reincarnation_manager").ReincarnationManager
Player = _imp("models").Player
ConfigManager = _imp("config_manager").ConfigManager


class _FakeConn:
    def __init__(self):
        self.statements = []

    async def execute(self, sql=None, params=None, *a, **k):
        self.statements.append((sql, params))
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _FakeExt:
    def __init__(self):
        self.config = {}

    async def get_system_config(self, key):
        return self.config.get(key)

    async def set_system_config(self, key, value):
        self.config[key] = value

    async def return_gifts_for_receiver(self, user_id, commit=True):
        return None

    async def set_user_free(self, user_id, commit=True):
        return None


class _FakeDB:
    def __init__(self, player=None):
        self.conn = _FakeConn()
        self.ext = _FakeExt()
        self.player = player
        self.saved = []

    async def get_player_by_id(self, user_id):
        if self.player and self.player.user_id == user_id:
            return self.player
        return None

    async def update_player(self, player, commit=True):
        self.saved.append(player)
        self.player = player


class _FakePlugin:
    def __init__(self, player=None):
        self.db = _FakeDB(player)
        self.reincarnation_mgr = ReincarnationManager(
            self.db, ConfigManager(ROOT), None
        )


class TestFateShopSnapshot(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = ConfigManager(ROOT)

    def _plugin(self, points=500, gold=0):
        player = Player(user_id="u1")
        player.fate_points = points
        player.gold = gold
        return _FakePlugin(player)

    async def test_snapshot_groups_cover_every_item(self):
        plugin = self._plugin()
        snap = await panel.fate_shop_snapshot(plugin, "u1")
        self.assertTrue(snap["ok"])
        self.assertTrue(snap["has_player"])
        self.assertEqual(snap["points"], 500)
        names = [g["name"] for g in snap["groups"]]
        self.assertEqual(names, [name for name, _ in rules.SHOP_GROUPS])
        listed = sum(len(g["items"]) for g in snap["groups"])
        self.assertEqual(listed, len(rules.SHOP_ITEMS))

    async def test_snapshot_marks_affordable_and_maxed(self):
        plugin = self._plugin(points=100)
        plugin.db.player.permanent_bonus = json.dumps(
            {"forge_affix_bonus": 3}, ensure_ascii=False
        )
        snap = await panel.fate_shop_snapshot(plugin, "u1")
        flat = {item["id"]: item for g in snap["groups"] for item in g["items"]}
        self.assertTrue(flat[26]["maxed"])          # 天工开物 3/3 已满
        self.assertFalse(flat[26]["affordable"])
        self.assertTrue(flat[1]["affordable"])      # 修炼加速 100 点买得起
        self.assertFalse(flat[43]["affordable"])    # 灵根天眷 300 点买不起

    async def test_snapshot_without_player_is_soft(self):
        plugin = _FakePlugin(None)
        snap = await panel.fate_shop_snapshot(plugin, "ghost")
        self.assertTrue(snap["ok"])
        self.assertFalse(snap["has_player"])


class TestFateShopBuy(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = ConfigManager(ROOT)

    async def test_buy_returns_fresh_snapshot(self):
        player = Player(user_id="u1")
        player.fate_points = 300
        plugin = _FakePlugin(player)
        data = await panel.fate_shop_buy(plugin, "u1", 1)
        self.assertTrue(data["ok"], data.get("message"))
        self.assertEqual(data["points"], 200)
        flat = {item["id"]: item for g in data["groups"] for item in g["items"]}
        self.assertEqual(flat[1]["owned"], 1)
        self.assertIn("修炼加速", data["message"])

    async def test_buy_uncapped_item_many_times(self):
        player = Player(user_id="u1")
        player.fate_points = 5000
        plugin = _FakePlugin(player)
        for _ in range(12):
            data = await panel.fate_shop_buy(plugin, "u1", 1)
            self.assertTrue(data["ok"], data.get("message"))
        flat = {item["id"]: item for g in data["groups"] for item in g["items"]}
        self.assertEqual(flat[1]["owned"], 12)   # 无上限：买 12 次就是 12 层
        self.assertFalse(flat[1]["maxed"])

    async def test_buy_too_poor_keeps_ok_false(self):
        player = Player(user_id="u1")
        player.fate_points = 10
        plugin = _FakePlugin(player)
        data = await panel.fate_shop_buy(plugin, "u1", 1)
        self.assertFalse(data["ok"])
        self.assertIn("仙缘点不足", data["message"])
        self.assertEqual(data["points"], 10)

    async def test_buy_capped_item_reports_已满(self):
        player = Player(user_id="u1")
        player.fate_points = 9999
        player.permanent_bonus = json.dumps({"forge_affix_bonus": 3}, ensure_ascii=False)
        plugin = _FakePlugin(player)
        data = await panel.fate_shop_buy(plugin, "u1", 26)
        self.assertFalse(data["ok"])
        self.assertIn("已满", data["message"])


class TestFateGoldExchange(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = ConfigManager(ROOT)

    def _plugin(self, gold, points=0):
        player = Player(user_id="u1")
        player.gold = gold
        player.fate_points = points
        return _FakePlugin(player)

    async def test_exchange_uses_max_by_default(self):
        unit = rules.GOLD_PER_FATE_POINT
        plugin = self._plugin(gold=unit * 3 + 123)
        data = await panel.fate_shop_exchange(plugin, "u1", 0)
        self.assertTrue(data["ok"], data.get("message"))
        self.assertEqual(data["points"], 3)
        self.assertEqual(plugin.db.player.gold, 123)
        self.assertEqual(data["exchange"]["left"], rules.FATE_EXCHANGE_MAX - 3)

    async def test_exchange_is_capped_per_life(self):
        unit = rules.GOLD_PER_FATE_POINT
        plugin = self._plugin(gold=unit * 200)
        data = await panel.fate_shop_exchange(plugin, "u1", 0)
        self.assertTrue(data["ok"], data.get("message"))
        self.assertEqual(data["points"], rules.FATE_EXCHANGE_MAX)
        self.assertEqual(data["exchange"]["left"], 0)
        # 换满了再换：直接拒绝，不会继续吃灵石
        gold_after = plugin.db.player.gold
        again = await panel.fate_shop_exchange(plugin, "u1", 0)
        self.assertFalse(again["ok"])
        self.assertEqual(plugin.db.player.gold, gold_after)
        self.assertIn("换满", again["message"])

    async def test_exchange_rejects_poor_player(self):
        plugin = self._plugin(gold=rules.GOLD_PER_FATE_POINT - 1)
        data = await panel.fate_shop_exchange(plugin, "u1", 0)
        self.assertFalse(data["ok"])
        self.assertIn("灵石不足", data["message"])
        self.assertEqual(plugin.db.player.gold, rules.GOLD_PER_FATE_POINT - 1)

    async def test_exchange_partial_request(self):
        unit = rules.GOLD_PER_FATE_POINT
        plugin = self._plugin(gold=unit * 10)
        data = await panel.fate_shop_exchange(plugin, "u1", 4)
        self.assertTrue(data["ok"], data.get("message"))
        self.assertEqual(data["points"], 4)
        self.assertEqual(plugin.db.player.gold, unit * 6)


class TestFateShopLocking(unittest.IsolatedAsyncioTestCase):
    """网页面板也必须走玩家锁：与聊天端并发时不能各写一份旧快照。"""

    async def test_concurrent_exchange_only_one_succeeds(self):
        import asyncio

        unit = rules.GOLD_PER_FATE_POINT
        player = Player(user_id="u1")
        player.gold = unit
        plugin = _FakePlugin(player)

        results = await asyncio.gather(
            panel.fate_shop_exchange(plugin, "u1", 1),
            panel.fate_shop_exchange(plugin, "u1", 1),
        )
        self.assertEqual(sum(1 for r in results if r["ok"]), 1)
        self.assertEqual(plugin.db.player.gold, 0)
        self.assertEqual(plugin.db.player.fate_points, 1)
        # 额度只记一次，不会超发
        self.assertEqual(
            plugin.db.ext.config.get(rules.fate_exchange_key("u1")), "1"
        )

    async def test_concurrent_buy_does_not_double_spend(self):
        import asyncio

        player = Player(user_id="u1")
        player.fate_points = rules.shop_price(52, 0)  # 刚好买一尊化身
        plugin = _FakePlugin(player)

        results = await asyncio.gather(
            panel.fate_shop_buy(plugin, "u1", 52),
            panel.fate_shop_buy(plugin, "u1", 52),
        )
        self.assertEqual(sum(1 for r in results if r["ok"]), 1)
        self.assertEqual(plugin.db.player.fate_points, 0)
        snap = await panel.fate_shop_snapshot(plugin, "u1")
        flat = {item["id"]: item for g in snap["groups"] for item in g["items"]}
        self.assertEqual(flat[52]["owned"], 1)


class TestFateExchangeKey(unittest.TestCase):
    def test_key_and_points_helpers(self):
        self.assertEqual(rules.fate_exchange_key("u1"), "fate_gold_exchange_u1")
        self.assertEqual(rules.fate_exchange_points(0), 0)
        self.assertEqual(
            rules.fate_exchange_points(rules.GOLD_PER_FATE_POINT * 7 + 999), 7
        )
        self.assertEqual(rules.GOLD_PER_FATE_POINT, 10_000_000)
        self.assertEqual(rules.FATE_EXCHANGE_MAX, 100)


class TestCrossLifeItems(unittest.TestCase):
    """跨世四件套：前世余荫 / 宿慧觉醒 / 一脉相承 / 万世传承。"""

    def test_keep_gold_ratio_is_hard_eye_capped(self):
        self.assertAlmostEqual(rules.keep_gold_ratio({}), 0.0)
        self.assertAlmostEqual(rules.keep_gold_ratio({"keep_gold_pct": 0.09}), 0.09)
        # 买再多也只能带走 90%，留 10% 的落差
        self.assertAlmostEqual(rules.keep_gold_ratio({"keep_gold_pct": 3.0}), 0.90)

    def test_keeps_life_skill_flag(self):
        self.assertFalse(rules.keeps_life_skill({}))
        self.assertTrue(rules.keeps_life_skill({"keep_life_skill": 1}))

    def test_heirloom_unseal_level_drops_per_stack(self):
        self.assertEqual(rules.heirloom_unseal_level({}), 10)
        self.assertEqual(rules.heirloom_unseal_level({"heirloom_slot_bonus": 1}), 7)
        self.assertEqual(rules.heirloom_unseal_level({"heirloom_slot_bonus": 2}), 4)
        # 超过 2 档也不会掉到负数（商品本身封顶 2 档）
        self.assertEqual(rules.heirloom_unseal_level({"heirloom_slot_bonus": 9}), 0)

    def test_start_level_never_exceeds_peak(self):
        levels = [{"level_name": f"lv{i}"} for i in range(20)]
        for i, row in enumerate(levels):
            row.update(
                {
                    "breakthrough_lifespan_gain": 5,
                    "breakthrough_mental_power_gain": 10,
                    "breakthrough_physical_damage_gain": 7,
                }
            )
        player = Player(user_id="u1")
        player.level_index = 0
        player.lifespan = 100
        player.mental_power = 1000
        player.physical_damage = 0
        # 买了 4 层（+8 级），但上一世只到 5 级 -> 只顶到 5
        gained = rules.apply_start_level(
            player, {"start_level_bonus": 8}, peak_level=5, level_data=levels
        )
        self.assertEqual(gained, 5)
        self.assertEqual(player.level_index, 5)
        self.assertEqual(player.lifespan, 125)
        self.assertEqual(player.mental_power, 1050)

    def test_start_level_zero_when_not_bought(self):
        levels = [{"level_name": "lv0"}, {"level_name": "lv1"}]
        player = Player(user_id="u1")
        self.assertEqual(
            rules.apply_start_level(player, {}, 10, levels), 0
        )
        self.assertEqual(player.level_index, 0)

    def test_fate_title_feeds_total_stats_multiplier(self):
        self.assertAlmostEqual(rules.permanent_stats_mult_from_bonus({}), 1.0)
        # 2 层称号（+2%）与 1 层道基（+5%）相乘
        mult = rules.permanent_stats_mult_from_bonus(
            {"dao_gift_times": 1, "fate_title": 2}
        )
        self.assertAlmostEqual(mult, 1.05 * 1.02, places=6)


if __name__ == "__main__":
    unittest.main()

