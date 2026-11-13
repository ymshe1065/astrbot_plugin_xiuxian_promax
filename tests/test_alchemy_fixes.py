# 炼丹管理器 P0 修复回归测试
import sys
import types
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


class _FakeDB:
    def __init__(self, player):
        self.player = player

    async def get_player_by_id(self, user_id):
        return self.player if user_id == self.player.user_id else None


class _FakeStorage:
    def get_item_count(self, player, item_name):
        return 3 if item_name == "灵草" else 0


class _FakeConfig:
    alchemy_config = {}
    alchemy_recipes = {
        "炼气丹": {
            "id": 1,
            "name": "炼气丹",
            "level_required": 0,
            "materials": {"灵草": 2, "灵石": 100},
            "success_rate": 85,
        },
        "聚气丹": {
            "id": 2,
            "name": "聚气丹",
            "level_required": 0,
            "materials": {"灵草": 1, "灵石": 50},
            "success_rate": 80,
        },
    }
    exp_pills_data = {"炼气丹": {"name": "炼气丹", "exp_gain": 500, "rank": "凡品"}}
    utility_pills_data = {}
    pills_data = {}


class TestAlchemyFixes(unittest.IsolatedAsyncioTestCase):
    async def test_recipe_list_available(self):
        Player = _imp("models").Player
        AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
        player = Player(user_id="u1", user_name="道友")
        mgr = AlchemyManager(_FakeDB(player), _FakeConfig(), _FakeStorage())
        ok, text, rows, card_lines, entries = await mgr.get_available_recipes("u1")
        self.assertTrue(ok)
        self.assertIn("[1] 炼气丹", text)
        self.assertIn("灵草x2(✓)", text)
        self.assertIn("灵石x100", text)
        self.assertIn("可炼制", text)
        # 图片卡片改为整行展示材料，且用文字标识拥有状态
        card_text = "\n".join(card_lines)
        self.assertIn("灵草 ×2（拥有 3/2 · 足够）", card_text)
        self.assertIn("灵石 ×100（持有 0/100 · 缺100）", card_text)

    async def test_recipe_list_only_shows_learned(self):
        Player = _imp("models").Player
        AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
        player = Player(user_id="u1", user_name="道友", learned_recipes="[1]")
        mgr = AlchemyManager(_FakeDB(player), _FakeConfig(), _FakeStorage())
        ok, text, rows, card_lines, entries = await mgr.get_available_recipes("u1")
        self.assertTrue(ok)
        self.assertIn("[1] 炼气丹", text)
        self.assertNotIn("[2] 聚气丹", text)
        self.assertNotIn("未学习", text)


    def test_fire_prompt_matches_actual_effect(self):
        import json

        AlchemyManager = _imp("managers.alchemy_manager").AlchemyManager
        fire_mods = json.loads(
            (ROOT / "config" / "pill_quality.json").read_text(encoding="utf-8")
        )["fire_modifiers"]
        for choice, success_up in (("1", False), ("2", True)):
            opt = AlchemyManager.FIRE_OPTIONS[choice]
            quality_up = float(fire_mods[choice]["极佳"]) > 0
            # 成功率与品质必须反向：猛火降成功率冲品质，文火相反
            self.assertEqual(opt["rate_bonus"] > 0, success_up)
            self.assertNotEqual(quality_up, success_up)
            if success_up:
                self.assertIn("成功率+", opt["desc"])
                self.assertIn("概率降低", opt["desc"])
            else:
                self.assertIn("成功率-", opt["desc"])
                self.assertIn("概率提升", opt["desc"])


if __name__ == "__main__":
    unittest.main()
