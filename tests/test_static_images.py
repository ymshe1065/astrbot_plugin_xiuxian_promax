# tests/test_static_images.py
# 固定接口列表页图片生成测试：/修仙帮助 调用时自动生成并复用图片
import asyncio
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


try:
    from PIL import Image  # noqa: F401
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


class _FakeEvent:
    def __init__(self):
        self.results = []

    def image_result(self, path):
        result = ("image", path)
        self.results.append(result)
        return result

    def plain_result(self, text):
        result = ("text", text)
        self.results.append(result)
        return result


class TestStaticPageContent(unittest.TestCase):
    def test_help_text_uses_shared_content_source(self):
        pages = _imp("utils.static_pages")
        text = pages.build_help_text()

        self.assertIn("修仙指令大全", text)
        self.assertIn("/我要修仙", text)
        self.assertIn("/宗门大比开启", text)
        self.assertIn("🛠️【管理员】", text)
        for line in pages.HELP_SECTIONS[-1]["lines"]:
            self.assertIn(line, text)

    def test_image_sections_match_text_content(self):
        pages = _imp("utils.static_pages")
        sections = pages.build_help_image_sections()
        image_lines = [line for sec in sections for line in sec["lines"]]
        text_lines = [line for sec in pages.HELP_SECTIONS for line in sec["lines"]]

        self.assertEqual(image_lines, text_lines)
        self.assertEqual(len(sections), len(pages.HELP_SECTIONS))

    def test_fingerprint_changes_when_content_changes(self):
        pages = _imp("utils.static_pages")
        before = pages.page_fingerprint("help")
        original = pages.HELP_SUBTITLE
        try:
            pages.HELP_SUBTITLE = original + " changed"
            pages.PAGES["help"]["subtitle"] = pages.HELP_SUBTITLE
            self.assertNotEqual(before, pages.page_fingerprint("help"))
        finally:
            pages.HELP_SUBTITLE = original
            pages.PAGES["help"]["subtitle"] = original


@unittest.skipUnless(HAS_PIL, "需要 Pillow 才能生成图片（pip install Pillow）")
class TestStaticImageGeneration(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_page_auto_generates_and_reuses_help_image(self):
        pages = _imp("utils.static_pages")
        generator = _imp("utils.static_image_generator")

        path = generator.image_path("help")
        path.unlink(missing_ok=True)

        first = await generator.ensure_page("help", allow_generate=True)
        self.assertIsNotNone(first)
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 10_000)

        with Image.open(path) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(img.width, pages.PAGES["help"]["width"])
            self.assertGreater(img.height, 1000)

        manifest = generator.read_manifest()
        self.assertIn("help", manifest)
        self.assertEqual(manifest["help"]["hash"], pages.page_fingerprint("help"))

        mtime = path.stat().st_mtime_ns
        second = await generator.ensure_page("help", allow_generate=True)
        self.assertEqual(first, second)
        self.assertEqual(mtime, path.stat().st_mtime_ns)

    async def test_generate_page_force_rewrites_existing_image(self):
        generator = _imp("utils.static_image_generator")
        path = generator.image_path("help")
        path.parent.mkdir(parents=True, exist_ok=True)
        await generator.generate_page("help", force=True)
        old_mtime = path.stat().st_mtime_ns
        await asyncio.sleep(0.02)

        result = await generator.generate_page("help", force=True)
        self.assertEqual(result, str(path))
        self.assertNotEqual(old_mtime, path.stat().st_mtime_ns)

    async def test_help_handler_sends_generated_image(self):
        image_generator = _imp("utils.image_generator")
        generator = _imp("utils.static_image_generator")
        MiscHandler = _imp("handlers.misc_handler").MiscHandler
        path = generator.image_path("help")
        path.unlink(missing_ok=True)

        image_generator.set_card_flags({"__enabled__": True, "CARD_HELP": True})
        handler = MiscHandler(
            None,
            {"IMAGE_CARDS": {"ENABLED": True, "CARD_HELP": True, "STATIC_AUTO_REGEN": True}},
        )
        event = _FakeEvent()
        results = [r async for r in handler.handle_help(event)]

        self.assertEqual(results[0][0], "image")
        self.assertEqual(results[0][1], str(path))
        self.assertTrue(path.exists())

    async def test_help_handler_falls_back_to_text_when_disabled(self):
        pages = _imp("utils.static_pages")
        image_generator = _imp("utils.image_generator")
        MiscHandler = _imp("handlers.misc_handler").MiscHandler

        image_generator.set_card_flags({"__enabled__": True, "CARD_HELP": False})
        handler = MiscHandler(None, {"IMAGE_CARDS": {"CARD_HELP": False}})
        event = _FakeEvent()
        results = [r async for r in handler.handle_help(event)]

        self.assertEqual(results[0][0], "text")
        self.assertEqual(results[0][1], pages.build_help_text())
        image_generator.set_card_flags({"__enabled__": True, "CARD_HELP": True})


if __name__ == "__main__":
    unittest.main()
