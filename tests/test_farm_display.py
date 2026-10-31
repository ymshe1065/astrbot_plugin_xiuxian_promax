"""灵田图片卡片不得截断已种植灵植。"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_equipment_instances as fixtures
from test_equipment_instances import _imp


class _Event:
    def image_result(self, path):
        return ("image", path)

    def plain_result(self, text):
        return ("text", text)


class _FarmManager:
    async def get_user_farm(self, user_id):
        now = int(time.time())
        crops = []
        for slot in range(1, 9):
            crops.append({"name": "九叶灵芝", "slot": slot, "mature_time": now + 3600})
        crops.extend([
            {"name": "__HYBRID__", "job_id": "j1", "slot": 9, "product": "血玉灵芝",
             "parents": ["九叶灵芝", "血灵草"], "mature_time": now + 7200},
            {"name": "__HYBRID__", "job_id": "j1", "slot": 10, "product": "血玉灵芝",
             "parents": ["九叶灵芝", "血灵草"], "mature_time": now + 7200},
        ])
        for slot in range(11, 21):
            crops.append({"name": "冰心草", "slot": slot, "mature_time": now + 1800})
        return {"level": 5, "crops": crops, "fertility": 400}

    async def get_farm_info(self, user_id):
        return "text fallback"

    def get_max_fertility(self, farm):
        return 400


class TestFarmDisplay(unittest.TestCase):
    def test_all_twenty_slots_are_rendered(self):
        handler_mod = _imp("handlers.spirit_farm_handlers")
        image_mod = _imp("utils.image_generator")
        captured = {}

        async def fake_render(*args, **kwargs):
            captured["sections"] = kwargs.get("sections") or args[3]
            return "farm-card.jpg"

        handler = handler_mod.SpiritFarmHandlers(None, _FarmManager())
        player = SimpleNamespace(user_id="u1", user_name="测试道友")
        with patch.object(image_mod, "card_enabled", return_value=True), patch.object(
            image_mod, "render_card_async", side_effect=fake_render
        ):
            results = asyncio.run(
                self._collect(handler.handle_farm_info.__wrapped__(handler, player, _Event()))
            )

        rows = next(
            section["rows"] for section in captured["sections"] if section["name"] == "格位一览"
        )
        self.assertEqual(len(rows), 19)  # 18 普通格 + 1 行两格杂交；全部已种植都有展示
        labels = " ".join(label for label, _ in rows)
        self.assertIn("格1. 九叶灵芝", labels)
        self.assertIn("格9/格10 → 血玉灵芝", labels)
        self.assertIn("格20. 冰心草", labels)
        self.assertEqual(results, [("image", "farm-card.jpg")])

    async def _collect(self, agen):
        return [item async for item in agen]


if __name__ == "__main__":
    unittest.main()
