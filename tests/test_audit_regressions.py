import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name


class _L:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
event_mod = types.ModuleType("astrbot.api.event")
api_mod.logger = _L()
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


def _imp(path):
    return __import__(f"{PKG}.{path}", fromlist=["x"])


class TestAuditRegressions(unittest.TestCase):
    def setUp(self):
        utils = _imp("handlers.utils")
        self.allow = utils._is_command_allowed
        self.allowed = utils.BUSY_STATE_ALLOWED_COMMANDS
        self.set_event_flag = utils.set_event_flag
        self.get_event_flag = utils.get_event_flag
        models = _imp("models")
        self.Player = models.Player
        self.CombatManager = _imp("managers.combat_manager").CombatManager

    def test_busy_whitelist_does_not_allow_black_market_purchase(self):
        self.assertTrue(self.allow("黑市", self.allowed))
        self.assertFalse(self.allow("黑市购买 1", self.allowed))

    def test_busy_whitelist_allows_argument_commands(self):
        self.assertTrue(self.allow("自动代理 押镖", self.allowed))
        self.assertTrue(self.allow("坊市卖出 1", self.allowed))
        self.assertTrue(self.allow("世界Boss", self.allowed))
        self.assertTrue(self.allow("世界boss", self.allowed))

    def test_busy_whitelist_allows_all_ranking_commands(self):
        for command in ("境界排行", "战力排行", "灵石排行", "宗门排行", "存款排行",
                        "贡献排行", "传承排行", "功德榜", "气运榜", "师德榜",
                        "气运", "团本排行"):
            self.assertTrue(self.allow(command, self.allowed), command)

    def test_busy_whitelist_allows_combat_invite_responses(self):
        self.assertTrue(self.allow("接受切磋", self.allowed))
        self.assertTrue(self.allow("拒绝切磋", self.allowed))

    def test_busy_whitelist_allows_view_only_commands(self):
        for command in (
            "突破信息", "丹药信息", "丹阁", "器阁", "百宝阁", "丹药配方", "丹方",
            "秘境列表", "历练信息", "传承信息", "坊市", "悬赏令", "银行流水",
            "仙缘商店", "搜索物品", "宗门列表", "宗门命令列表", "宗门大比状态",
            "宗门战争状态", "宗门战报", "宗门建筑", "团本状态", "世界团本",
            "符箓", "季节", "仙界日历", "师徒", "称号图鉴", "我的称号",
            "一键日常", "修仙诊断",
        ):
            self.assertTrue(self.allow(command, self.allowed), command)

    def test_busy_whitelist_keeps_action_commands_blocked(self):
        for command in (
            "突破", "学习丹方", "炼丹", "种植", "购买 玄铁",
            "黑市购买 1", "坊市购买 1", "坊市上架 丹药 1 100",
            "宗门大比报名", "仙缘兑换 1", "接取悬赏 1",
        ):
            self.assertFalse(self.allow(command, self.allowed), command)

    def test_forge_multiplier(self):
        player = self.Player(user_id="u1", permanent_bonus=json.dumps({"forge_level": 5}))
        self.assertAlmostEqual(self.CombatManager.get_forge_multiplier(player), 1.10)

        capped = self.Player(user_id="u2", permanent_bonus=json.dumps({"forge_level": 99}))
        self.assertAlmostEqual(self.CombatManager.get_forge_multiplier(capped), 1.30)

    def test_event_flags_are_isolated_per_event(self):
        class Event:
            pass
        first, second = Event(), Event()
        self.set_event_flag(first, "shop_buy", True)
        self.assertTrue(self.get_event_flag(first, "shop_buy", False))
        self.assertFalse(self.get_event_flag(second, "shop_buy", False))

    def test_title_exp_bonus(self):
        TitleManager = _imp("managers.title_manager").TitleManager
        player = self.Player(
            user_id="u4",
            permanent_bonus=json.dumps({"titles": {"百变星君": 1.0}}),
        )
        self.assertAlmostEqual(TitleManager.get_exp_bonus(player), 0.15)

    def test_title_combat_bonus(self):
        player = self.Player(
            user_id="u3",
            permanent_bonus=json.dumps({"titles": {"灭世魔神": 1.0}}),
        )
        bonus = self.CombatManager.get_title_combat_bonus(player)
        self.assertEqual(bonus.get("atk"), 200)
        self.assertEqual(bonus.get("crit"), 5)


if __name__ == "__main__":
    unittest.main()
