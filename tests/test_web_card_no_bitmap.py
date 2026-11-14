# tests/test_web_card_no_bitmap.py
# 网页端不出位图：只写 sidecar，Pillow 整条路径跳过。
#
# 背景：网页端命中 sidecar 时位图会被直接丢弃（webroom_event 里 continue），
# 但生成流程仍是「先画完 1~2 秒的位图，再写 JSON，才推送」。
# 丹方 18 条实测位图 1438 ms / 仅 sidecar 2.3 ms。
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestWebCardNoBitmap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.img = _load("image_generator_probe", "utils/image_generator.py")
        cls.sidecar = _load("card_sidecar_probe2", "webchat_room/card_sidecar.py")
        cls.tmp = tempfile.TemporaryDirectory()
        # 别往真实卡片目录里写测试产物
        cls._old_dir = cls.img.CARD_TEMP_DIR
        cls.img.CARD_TEMP_DIR = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.img.CARD_TEMP_DIR = cls._old_dir
        cls.tmp.cleanup()

    def setUp(self):
        for p in Path(self.tmp.name).glob("card_*"):
            p.unlink()

    def tearDown(self):
        # 开关是 ContextVar，用例失败也不能漏到别的用例
        self.assertTrue(self.img.render_bitmap_enabled())

    def _render(self, **kw):
        sections = [{
            "name": "已学丹方",
            "view": "grid",
            "lines": ["[1] 回春散（可炼制 · 成功率 90%）", "   · 效果：恢复 500~800 气血"],
            "entries": [
                {"id": 1, "name": "回春散", "rank": "凡品", "state": "ok",
                 "materials": [{"label": "凡品灵植", "need": 1, "have": 9}],
                 "max": 9, "source": "recipe", "action": "炼制"},
            ],
        }]
        return self.img.render_card("craft", "云无月 的已学丹方", "共 1 个配方",
                                    sections, "💡 /炼丹 <ID>", 1000, **kw)

    def test_sidecar_only_skips_pillow(self):
        """关了位图就只留 .json：路径仍以 .jpg 结尾，但文件不落地。"""
        token = self.img.set_render_bitmap(False)
        try:
            path = self._render()
        finally:
            self.img.reset_render_bitmap(token)

        self.assertTrue(path)
        self.assertTrue(path.endswith(".jpg"))
        self.assertFalse(Path(path).exists(), "不该再产位图")
        self.assertTrue(Path(path).with_suffix(".json").is_file())

    def test_sidecar_survives_without_image(self):
        """没有同名 jpg 时前端也要能读到卡片（card_sidecar 只认后缀，不看文件）。"""
        token = self.img.set_render_bitmap(False)
        try:
            path = self._render()
        finally:
            self.img.reset_render_bitmap(token)

        card = self.sidecar.load_card_for_image(path)
        self.assertIsNotNone(card)
        section = card["sections"][0]
        self.assertEqual(section["view"], "grid")
        self.assertEqual(section["entries"][0]["name"], "回春散")
        self.assertEqual(section["entries"][0]["max"], 9)
        self.assertTrue(section["lines"])

    def test_sidecar_keeps_section_facet(self):
        """分区级 facet 必须在 sidecar 往返后还在。

        丢了它，网页端拿不到「收集 / 分组 / 可兑」这些声明，只能按 entry.state 猜，
        于是称号图鉴和仙缘商店的筛选栏会变成炼丹那套「可炼制 / 缺材料 / 未达境界」。
        这是真实链路才暴露的问题：演示脚本直接广播 payload，绕过了 sidecar 反序列化。
        """
        sections = [{
            "name": "称号",
            "view": "grid",
            "facet": {"label": "收集", "okLabel": "已收集"},
            "entries": [{"name": "初入仙途", "rank": "凡品", "state": "ok"}],
        }]
        token = self.img.set_render_bitmap(False)
        try:
            path = self.img.render_card("info", "称号图鉴", "", sections, "", 1000)
        finally:
            self.img.reset_render_bitmap(token)

        card = self.sidecar.load_card_for_image(path)
        self.assertIsNotNone(card)
        self.assertEqual(card["sections"][0]["facet"],
                         {"label": "收集", "okLabel": "已收集"})

    def test_sidecar_keeps_default_rank(self):
        """货架的「默认看全量」声明必须过 sidecar。

        丹方货架没有 facet（品阶靠品阶条切），如果不显式声明 defaultRank，
        前端就按配方书那套「默认落在第一个可做档」处理，10 张丹方只剩 1 张。
        """
        sections = [{
            "name": "在售清单",
            "view": "grid",
            "defaultRank": "*",
            "entries": [{"name": "《筑基丹方》", "rank": "灵品", "state": ""}],
        }]
        token = self.img.set_render_bitmap(False)
        try:
            path = self.img.render_card("shop", "丹方阁 · 今日货架", "共 1 种在售",
                                        sections, "", 1000)
        finally:
            self.img.reset_render_bitmap(token)

        card = self.sidecar.load_card_for_image(path)
        self.assertIsNotNone(card)
        self.assertEqual(card["sections"][0]["defaultRank"], "*")

    def test_sidecar_does_not_invent_default_rank(self):
        """没写的分区不能凭空多一档默认视角。"""
        sections = [{"name": "已学丹方", "view": "grid", "defaultRank": "凡品",
                     "entries": [{"name": "回春散", "rank": "凡品"}]}]
        token = self.img.set_render_bitmap(False)
        try:
            path = self.img.render_card("craft", "丹方", "", sections, "", 1000)
        finally:
            self.img.reset_render_bitmap(token)

        card = self.sidecar.load_card_for_image(path)
        self.assertNotIn("defaultRank", card["sections"][0])

    def test_sidecar_keeps_none_as_null(self):
        """max=None 表示「不限量」，不能被序列化成字符串 "None"。

        前端拿 Number("None") 得 NaN、`|| 0` 一兜就成 0，
        于是常驻丹方被标红「材料不足」，明明库存不限量。
        """
        sections = [{
            "name": "在售清单",
            "view": "grid",
            "defaultRank": "*",
            "entries": [{"name": "《筑基丹方》", "rank": "灵品", "unlimited": True,
                         "max": None, "source": "shop"}],
        }]
        token = self.img.set_render_bitmap(False)
        try:
            path = self.img.render_card("shop", "突破丹方", "", sections, "", 1000)
        finally:
            self.img.reset_render_bitmap(token)

        raw = json.loads(Path(path).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertIsNone(raw["sections"][0]["entries"][0]["max"])
        card = self.sidecar.load_card_for_image(path)
        self.assertIsNone(card["sections"][0]["entries"][0]["max"])

    def test_sidecar_does_not_invent_facet(self):
        """没声明 facet 的老卡片不能凭空多出一个字段，否则前端会以为它要筛选条。"""
        sections = [{
            "name": "材料",
            "view": "grid",
            "entries": [{"name": "凡品灵植", "rank": "凡品", "state": "ok"}],
        }]
        token = self.img.set_render_bitmap(False)
        try:
            path = self.img.render_card("bag", "储物戒", "", sections, "", 1000)
        finally:
            self.img.reset_render_bitmap(token)

        card = self.sidecar.load_card_for_image(path)
        self.assertNotIn("facet", card["sections"][0])

    def test_sidecar_and_bitmap_payload_identical(self):
        """两条路径写出的 JSON 必须一模一样，否则网页端会看到两种卡片。"""
        token = self.img.set_render_bitmap(False)
        try:
            fast = self._render()
        finally:
            self.img.reset_render_bitmap(token)
        slow = self._render()
        self.assertTrue(Path(slow).exists(), "默认仍要出图（QQ 端）")

        fast_json = json.loads(Path(fast).with_suffix(".json").read_text(encoding="utf-8"))
        slow_json = json.loads(Path(slow).with_suffix(".json").read_text(encoding="utf-8"))
        fast_json.pop("id")
        slow_json.pop("id")
        self.assertEqual(fast_json, slow_json)

    def test_switch_does_not_leak(self):
        """reset 之后必须回到出图状态，否则同进程的 QQ 端会跟着没图。"""
        token = self.img.set_render_bitmap(False)
        self.assertFalse(self.img.render_bitmap_enabled())
        self.img.reset_render_bitmap(token)
        self.assertTrue(self.img.render_bitmap_enabled())

    def test_nested_switches_restore(self):
        """两层嵌套（指令里再包一层）要能逐层还原。"""
        outer = self.img.set_render_bitmap(False)
        inner = self.img.set_render_bitmap(True)
        self.assertTrue(self.img.render_bitmap_enabled())
        self.img.reset_render_bitmap(inner)
        self.assertFalse(self.img.render_bitmap_enabled())
        self.img.reset_render_bitmap(outer)
        self.assertTrue(self.img.render_bitmap_enabled())

    def test_cleanup_removes_orphan_sidecars(self):
        """只清 jpg 的话，网页端留下的 json 会无限堆积。"""
        for i in range(6):
            token = self.img.set_render_bitmap(False)
            try:
                self._render()
            finally:
                self.img.reset_render_bitmap(token)
        self.assertEqual(len(list(Path(self.tmp.name).glob("card_*.json"))), 6)
        self.img._cleanup_cards(keep=2)
        self.assertEqual(len(list(Path(self.tmp.name).glob("card_*.json"))), 2)


class TestHandlerWiring(unittest.TestCase):
    """main.py 是所有指令的统一入口，开关必须挂在那里才覆盖得到丹方/符箓/货架。"""

    def test_require_whitelist_toggles_bitmap(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        idx = src.index("def require_whitelist")
        body = src[idx: idx + 900]
        self.assertIn("is_webroom_platform(event)", body)
        self.assertIn("set_render_bitmap(False)", body)
        self.assertIn("reset_render_bitmap(token)", body)


class TestWebroomPlatformDetection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.img = _load("image_generator_probe2", "utils/image_generator.py")

    def test_platform_name_webroom(self):
        class Ev:
            def get_platform_name(self):
                return "webroom"
        self.assertTrue(self.img.is_webroom_platform(Ev()))

    def test_platform_name_qq_is_not_web(self):
        class Ev:
            def get_platform_name(self):
                return "aiocqhttp"
        self.assertFalse(self.img.is_webroom_platform(Ev()))

    def test_falls_back_to_class_name(self):
        """取不到平台名时按事件类型认（测试桩 / 旧核心）。"""
        class WebroomMessageEvent:
            def get_platform_name(self):
                return ""
        self.assertTrue(self.img.is_webroom_platform(WebroomMessageEvent()))

    def test_broken_event_does_not_raise(self):
        class Ev:
            def get_platform_name(self):
                raise RuntimeError("boom")
        self.assertFalse(self.img.is_webroom_platform(Ev()))


if __name__ == "__main__":
    unittest.main()
