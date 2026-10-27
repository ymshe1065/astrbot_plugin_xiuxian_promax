# tests/test_pet_combat.py
import os
import sys
import tempfile
import time
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

    def exception(self, *a, **k):
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


class TestPetCombat(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        self.DataBase = _imp("data.data_manager").DataBase
        self.DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.PetManager = _imp("managers.pet_manager").PetManager
        self.CombatManager = _imp("managers.combat_manager").CombatManager
        self.CombatStats = _imp("managers.combat_manager").CombatStats
        self.Player = _imp("models").Player

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = self.DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = self.DatabaseExtended(self.conn)
        self.player = self.Player(
            user_id="u1", user_name="道友", gold=1_000_000, experience=100_000
        )
        await self.db.create_player(self.player)
        self.pet_mgr = self.PetManager(self.db)

    async def asyncTearDown(self):
        await self.conn.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    async def test_adopt_pet_full_energy_and_combat_coop(self):
        ok, _ = await self.pet_mgr.adopt_pet(self.player, "火灵狐")
        self.assertTrue(ok)
        pet = await self.pet_mgr.get_pet("u1")
        self.assertEqual(pet["hp"], pet["max_hp"])
        self.assertGreater(pet["max_hp"], 0)

        state = await self.pet_mgr.build_pet_combat_state(pet, 50_000, 10_000, 0)
        self.assertIsNotNone(state)
        attacker = self.CombatStats(
            user_id="u1", name="甲", hp=50_000, max_hp=50_000, mp=1, max_mp=1,
            atk=10_000, defense=0, effects={}, pet=state,
        )
        defender = self.CombatStats(
            user_id="u2", name="乙", hp=50_000, max_hp=50_000, mp=1, max_mp=1,
            atk=10_000, defense=0, effects={},
        )
        result = self.CombatManager.player_vs_player(attacker, defender, combat_type=2)
        self.assertTrue(any("🐾" in line for line in result["combat_log"]))
        self.assertLessEqual(state.hp, state.max_hp)

    async def test_out_of_combat_regen_and_death_revive(self):
        ok, _ = await self.pet_mgr.adopt_pet(self.player, "冰晶兽")
        self.assertTrue(ok)
        pet = await self.pet_mgr.get_pet("u1")
        max_hp = pet["max_hp"]

        now = int(time.time())
        await self.conn.execute(
            "UPDATE player_pets SET hp = ?, last_regen_time = ? WHERE user_id = ?",
            (100, now - 600, "u1"),
        )
        await self.conn.commit()
        pet = await self.pet_mgr.get_pet("u1")
        self.assertGreater(pet["hp"], 100)

        await self.conn.execute(
            "UPDATE player_pets SET hp = 0, dead_until = ? WHERE user_id = ?",
            (now + 600, "u1"),
        )
        await self.conn.commit()
        pet = await self.pet_mgr.get_pet("u1")
        self.assertEqual(pet["hp"], 0)
        self.assertEqual(self.pet_mgr.get_pet_combat_effects(pet), {})

        await self.conn.execute(
            "UPDATE player_pets SET dead_until = ? WHERE user_id = ?",
            (now - 1, "u1"),
        )
        await self.conn.commit()
        pet = await self.pet_mgr.get_pet("u1")
        self.assertEqual(pet["hp"], max(1, max_hp // 2))

    async def test_gold_pet_bonus(self):
        self.assertTrue((await self.pet_mgr.adopt_pet(self.player, "噬金鼠"))[0])
        pet = await self.pet_mgr.get_pet("u1")
        self.assertAlmostEqual(self.pet_mgr.get_gold_bonus(pet), 0.1)

    async def test_multi_pet_slots_exp_share_and_lineup(self):
        self.player.level_index = 10
        self.player.cultivation_type = "御兽"
        await self.db.update_player(self.player)
        self.assertEqual(self.pet_mgr.get_pet_slots(self.player), 3)

        for species in ("火灵狐", "火灵狐", "冰晶兽"):
            ok, _ = await self.pet_mgr.adopt_pet(self.player, species)
            self.assertTrue(ok)
        pets = await self.pet_mgr.get_pets("u1")
        self.assertEqual([p["pet_name"] for p in pets], ["火灵狐 1", "火灵狐 2", "冰晶兽 1"])
        self.assertEqual(sum(1 for p in pets if p["is_main"] == 1), 1)

        old_exp = sum(p["exp"] for p in pets)
        self.player.experience += 10_000
        await self.db.update_player(self.player)
        pets = await self.pet_mgr.get_pets("u1")
        self.assertGreater(sum(p["exp"] + p["level"] * 500 for p in pets), old_exp)

        ok, _ = await self.pet_mgr.set_main_pet("u1", pets[2]["id"])
        self.assertTrue(ok)
        main_pet, supports, _, atk = await self.pet_mgr.build_pet_lineup(
            "u1", 50_000, 10_000, 0, "御兽"
        )
        self.assertIsNotNone(main_pet)
        self.assertEqual(len(supports), 2)
        self.assertGreater(atk, 10_000)
