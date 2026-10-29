# all 参数回归：灵田种植 / 洞天升级 / 自动代理格号 / 贪婪参数兜底
import sys
import types
import unittest
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


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


class _FakeEvent:
    """只实现参数兜底需要的方法。"""

    def __init__(self, text: str):
        self.text = text

    def get_message_str(self):
        return self.text


class TestPlantArgs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module = _imp("managers.spirit_farm_manager")
        cls.parse = staticmethod(module.parse_plant_args)
        cls.pick = staticmethod(module.pick_default_herb)

    def test_all_keywords(self):
        for raw in (
            "灵草 all", "灵草all", "灵草 ALL", "灵草 ＡＬＬ",
            "灵草 全部", "灵草 所有", "灵草 种满", "灵草 一键",
            "灵草 满", "灵草 全满", "灵草 满格", "灵草 all。",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(raw), ("灵草", 1, None, True))

    def test_all_without_herb(self):
        for raw in ("all", "全部", "一键", "all！", "  ALL  "):
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(raw), ("", 1, None, True))

    def test_counts_and_ranges(self):
        self.assertEqual(self.parse("灵草 6"), ("灵草", 6, None, False))
        self.assertEqual(self.parse("灵草6"), ("灵草", 6, None, False))
        self.assertEqual(self.parse("灵草 20株"), ("灵草", 20, None, False))
        self.assertEqual(self.parse("灵草２０个"), ("灵草", 20, None, False))
        self.assertEqual(self.parse("灵草 1-20"), ("灵草", 1, (1, 20), False))
        self.assertEqual(self.parse("灵草1-20"), ("灵草", 1, (1, 20), False))
        self.assertEqual(self.parse("灵草 1-20格"), ("灵草", 1, (1, 20), False))
        self.assertEqual(self.parse("灵草 20-1"), ("灵草", 1, (1, 20), False))
        self.assertEqual(self.parse("灵草 0"), ("灵草", 1, None, False))

    def test_zero_width_space(self):
        self.assertEqual(self.parse("灵草\u200ball"), ("灵草", 1, None, True))

    def test_pick_default_herb(self):
        crops = [
            {"name": "血灵草", "slot": 1},
            {"name": "灵草", "slot": 2},
            {"name": "血灵草", "slot": 3},
            {"name": "__HYBRID__", "slot": 4},
        ]
        self.assertEqual(self.pick(crops), "血灵草")
        self.assertEqual(self.pick([]), "灵草")


class TestBlessedLandArgs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parse = staticmethod(_imp("managers.blessed_land_manager").parse_upgrade_args)

    def test_all_variants(self):
        for raw in ("all", "ALL", "全部", "所有", "满级", "一键", "全部种满"):
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(raw), ("all", 0))

    def test_times_and_target(self):
        self.assertEqual(self.parse(""), ("times", 1))
        self.assertEqual(self.parse("3"), ("times", 3))
        self.assertEqual(self.parse("0"), ("times", 1))
        self.assertEqual(self.parse("1-5"), ("target", 5))
        self.assertEqual(self.parse("5-1"), ("target", 5))
        self.assertEqual(self.parse("乱写"), (None, None))


class TestPlotSelection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parse = staticmethod(_imp("managers.auto_proxy_manager").AutoProxyManager._parse_plot_selection)

    def test_all_keywords(self):
        for raw in ("all", "全部", "所有", "*", "全部格", "所有格"):
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(raw), (None, []))

    def test_plots(self):
        self.assertEqual(self.parse(""), (None, []))
        self.assertEqual(self.parse("1 2"), ([1, 2], []))
        self.assertEqual(self.parse("1,2"), ([1, 2], []))
        self.assertEqual(self.parse("3-1"), ([1, 2, 3], []))
        self.assertEqual(self.parse("5 0"), ([5], ["0"]))
        self.assertEqual(self.parse("灵田"), ([], ["灵田"]))


class TestGreedyFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.utils = _imp("handlers.utils")

    def test_raw_args_wins_when_greedy_truncated(self):
        event = _FakeEvent("/种植 灵草 all")
        self.assertEqual(
            self.utils.resolve_greedy_args(event, "灵草", "种植"), "灵草 all"
        )

    def test_keeps_greedy_when_complete(self):
        event = _FakeEvent("/种植 灵草 all")
        self.assertEqual(
            self.utils.resolve_greedy_args(event, "灵草 all", "种植"), "灵草 all"
        )

    def test_compact_and_prefixes(self):
        cases = {
            "/种植灵草all": "灵草all",
            "@机器人 /种植 灵草 全部": "灵草 全部",
            "[At:123] /种植 灵草 20株": "灵草 20株",
            "/关闭代理 灵田 1-3": "灵田 1-3",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.utils.get_command_args(_FakeEvent(text), "种植", "关闭代理"), expected)

    def test_no_args(self):
        self.assertEqual(self.utils.get_command_args(_FakeEvent("/种植"), "种植"), "")
        self.assertEqual(self.utils.resolve_greedy_args(_FakeEvent("/种植"), "", "种植"), "")


if __name__ == "__main__":
    unittest.main()
