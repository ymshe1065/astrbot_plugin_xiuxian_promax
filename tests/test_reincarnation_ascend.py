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
    async def execute(self, *a, **k):
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
    def _player(self):
        return Player(user_id="u1")

    def test_grant_adds_stats_and_ledger(self):
        player = self._player()
        player.mental_power = 500
        bonus = {}
        rules.grant_dao_gift(player, bonus)
        self.assertEqual(player.mental_power, 600)
        self.assertEqual(bonus["dao_gift"]["mental_power"], 100)
        self.assertEqual(bonus["dao_gift"]["magic_damage"], 20)
        self.assertEqual(rules.dao_gift_times(bonus), 1)

    def test_apply_replays_after_reset(self):
        player = self._player()
        bonus = {"dao_gift": {"mental_power": 300, "magic_damage": 60}}
        player.mental_power = 7  # 模拟被 generate_new_player_stats 覆盖
        player.magic_damage = 5
        rules.apply_dao_gift(player, bonus)
        self.assertEqual(player.mental_power, 307)
        self.assertEqual(player.magic_damage, 65)

    def test_bad_ledger_is_ignored(self):
        player = self._player()
        player.mental_power = 100
        rules.apply_dao_gift(player, {"dao_gift": "not-a-dict"})
        self.assertEqual(player.mental_power, 100)

    def test_parse_bonus_tolerates_broken_json(self):
        player = self._player()
        player.permanent_bonus = "{broken"
        self.assertEqual(rules.parse_bonus(player), {})


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
        ok, msg = await mgr.buy_fate_shop_item(player, 3)
        self.assertTrue(ok, msg)
        self.assertEqual(player.fate_points, 100)
        data = json.loads(player.permanent_bonus)
        self.assertEqual(data["dao_gift"]["mental_power"], 100)
        self.assertIn("飞升与弃道重修后都会重新加回", msg)

    async def test_speed_purchase_stacks(self):
        mgr, db = self._manager()
        player = Player(user_id="u1")
        player.fate_points = 250
        await mgr.buy_fate_shop_item(player, 1)
        await mgr.buy_fate_shop_item(player, 1)
        data = json.loads(player.permanent_bonus)
        self.assertAlmostEqual(data["global_cultivation_multiplier"], 0.2)


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

    async def test_preview_blocks_unknown_heirloom(self):
        mgr = self._manager()
        player = self._maxed_player()
        ok, msg = await mgr.preview_reincarnate(player, "不存在的剑")
        self.assertFalse(ok)
        self.assertIn("都没有", msg)

    async def test_try_reincarnate_replays_dao_gift(self):
        mgr = self._manager()
        player = self._maxed_player()
        player.permanent_bonus = json.dumps({"dao_gift": {"mental_power": 1000}}, ensure_ascii=False)
        ok, msg = await mgr.try_reincarnate(player)
        self.assertTrue(ok, msg)
        self.assertEqual(player.level_index, 0)
        self.assertEqual(player.reincarnation_count, 3)
        # 重置后基础精神只有几百，账本 +1000 必须加上去
        self.assertGreaterEqual(player.mental_power, 1000)
        self.assertIn("完美转生次数：3", msg)


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
