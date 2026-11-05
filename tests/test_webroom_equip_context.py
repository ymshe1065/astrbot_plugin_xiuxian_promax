# -*- coding: utf-8 -*-
"""webroom 装备快捷入口：可装备判定、槽位占用与替换选项。"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

_TMP = tempfile.mkdtemp(prefix="xiuxian_equip_ctx_")

astrbot = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
core_mod = types.ModuleType("astrbot.core")
utils_mod = types.ModuleType("astrbot.core.utils")
path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")


class _L:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


api_mod.logger = _L()
api_mod.AstrBotConfig = dict
path_mod.get_astrbot_data_path = lambda: _TMP
utils_mod.astrbot_path = path_mod
core_mod.utils = utils_mod
astrbot.api = api_mod
astrbot.core = core_mod
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api_mod)
sys.modules.setdefault("astrbot.core", core_mod)
sys.modules.setdefault("astrbot.core.utils", utils_mod)
sys.modules.setdefault("astrbot.core.utils.astrbot_path", path_mod)

try:
    rs = __import__(f"{PKG}.webchat_room.room_server", fromlist=["x"])
except Exception as exc:  # pragma: no cover - 依赖缺失时跳过
    raise unittest.SkipTest(f"webroom 依赖不可用: {exc}")


class FakePlayer:
    def __init__(self, techniques=(), main_technique=""):
        self._techniques = list(techniques)
        self.main_technique = main_technique

    def get_techniques_list(self):
        return list(self._techniques)


class FakeManager:
    def __init__(self, slots=None):
        self.slots = slots or {}

    def get_gear_slots(self, _player):
        return self.slots


class FakeRequest:
    def __init__(self, item):
        self.query = {"item": item}
        self.headers = {}


def _slots(**names):
    base = {
        "weapon": "", "head": "", "armor": "", "gloves": "", "boots": "",
        "ring1": "", "ring2": "", "amulet": "",
    }
    base.update(names)
    return {slot: (name, None if not name else {"name": name}) for slot, name in base.items()}


class EquipContextTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig_plugin = rs._plugin
        self._orig_token_user = rs._token_user
        rs._token_user = lambda _request: {"username": "alice"}
        self._orig_player_key_of = rs.accounts_store.player_key_of
        rs.accounts_store.player_key_of = lambda user: f"web_{user['username']}"

    async def asyncTearDown(self):
        rs._plugin = self._orig_plugin
        rs._token_user = self._orig_token_user
        rs.accounts_store.player_key_of = self._orig_player_key_of

    def _install(self, player, *, items=None, weapons=None, techniques=None, slots=None):
        class FakeDB:
            async def get_player_by_id(self, _user_id):
                return player

        rs._plugin = SimpleNamespace(
            db=FakeDB(),
            config_manager=SimpleNamespace(
                items_data=items or {},
                weapons_data=weapons or {},
                techniques_data=techniques or {},
            ),
            equipment_handler=SimpleNamespace(equipment_manager=FakeManager(slots)),
        )

    async def _call(self, item):
        response = await rs._api_equip_context(FakeRequest(item))
        return json.loads(response.body.decode("utf-8"))

    async def test_display_tag_is_stripped_before_lookup(self):
        self._install(
            FakePlayer(),
            items={"青锋剑": {"type": "武器"}},
            slots=_slots(weapon=""),
        )
        data = await self._call("青锋剑（装备·武器 | 黄品）")
        self.assertTrue(data["equippable"])
        self.assertEqual(data["item"], "青锋剑")

    async def test_non_equippable_item(self):
        self._install(FakePlayer(), items={"无极骨": {"type": "材料"}})
        data = await self._call("无极骨")
        self.assertTrue(data["equippable"] is False)
        self.assertFalse(data["needs_choice"])

    async def test_empty_single_slot_needs_no_choice(self):
        self._install(
            FakePlayer(),
            items={"青锋剑": {"type": "武器"}},
            slots=_slots(weapon=""),
        )
        data = await self._call("青锋剑")
        self.assertTrue(data["equippable"])
        self.assertFalse(data["needs_choice"])
        self.assertIn("武器", data["hint"])

    async def test_occupied_single_slot_offers_replace(self):
        self._install(
            FakePlayer(),
            items={"青锋剑": {"type": "武器"}},
            slots=_slots(weapon="旧剑"),
        )
        data = await self._call("青锋剑")
        self.assertTrue(data["needs_choice"])
        self.assertEqual(len(data["choices"]), 1)
        self.assertEqual(data["choices"][0]["value"], "武器")
        self.assertIn("旧剑", data["choices"][0]["label"])

    async def test_ring_with_one_free_slot_needs_no_choice(self):
        self._install(
            FakePlayer(),
            items={"幽影戒": {"type": "戒指"}},
            slots=_slots(ring1="旧戒"),
        )
        data = await self._call("幽影戒")
        self.assertFalse(data["needs_choice"])
        self.assertIn("戒指2", data["hint"])

    async def test_both_rings_occupied_offer_two_choices(self):
        self._install(
            FakePlayer(),
            items={"幽影戒": {"type": "戒指"}},
            slots=_slots(ring1="旧戒一", ring2="旧戒二"),
        )
        data = await self._call("幽影戒")
        self.assertTrue(data["needs_choice"])
        self.assertEqual([c["value"] for c in data["choices"]], ["戒指1", "戒指2"])

    async def test_technique_full_slots_offer_three_choices(self):
        self._install(
            FakePlayer(techniques=("功法甲", "功法乙", "功法丙")),
            techniques={"新功法": {"type": "功法"}},
        )
        data = await self._call("新功法")
        self.assertTrue(data["needs_choice"])
        self.assertEqual([c["value"] for c in data["choices"]], ["1", "2", "3"])

    async def test_technique_with_free_slot_needs_no_choice(self):
        self._install(
            FakePlayer(techniques=("功法甲",)),
            techniques={"新功法": {"type": "功法"}},
        )
        data = await self._call("新功法")
        self.assertFalse(data["needs_choice"])
        self.assertIn("1/3", data["hint"])

    async def test_already_equipped_technique(self):
        self._install(
            FakePlayer(techniques=("功法甲",)),
            techniques={"功法甲": {"type": "功法"}},
        )
        data = await self._call("功法甲")
        self.assertTrue(data["already_equipped"])
        self.assertIn("栏位 1", data["hint"])

    async def test_main_technique_replacement_offers_confirmation(self):
        self._install(
            FakePlayer(main_technique="旧心法"),
            techniques={"新心法": {"type": "main_technique"}},
        )
        data = await self._call("新心法")
        self.assertTrue(data["equippable"])
        self.assertTrue(data["needs_choice"])
        self.assertEqual(len(data["choices"]), 1)
        self.assertEqual(data["choices"][0]["value"], "")
        self.assertIn("旧心法", data["hint"])

    async def test_main_technique_already_equipped(self):
        self._install(
            FakePlayer(main_technique="新心法"),
            techniques={"新心法": {"type": "main_technique"}},
        )
        data = await self._call("新心法")
        self.assertTrue(data["already_equipped"])


if __name__ == "__main__":
    unittest.main()
