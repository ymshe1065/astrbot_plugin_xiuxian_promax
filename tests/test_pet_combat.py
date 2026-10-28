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

    async def test_resting_accelerates_pet_energy_regen(self):
        UserStatus = _imp("models_extended").UserStatus
        ok, _ = await self.pet_mgr.adopt_pet(self.player, "冰晶兽")
        self.assertTrue(ok)
        pet = await self.pet_mgr.get_pet("u1")
        max_hp = pet["max_hp"]

        now = int(time.time())
        await self.db.ext.set_user_busy("u1", int(UserStatus.RESTING), 0)
        start_hp = max_hp // 2
        await self.conn.execute(
            "UPDATE player_pets SET hp = ?, last_regen_time = ? WHERE user_id = ?",
            (start_hp, now - 600, "u1"),
        )
        await self.conn.commit()

        pet = await self.pet_mgr.get_pet("u1")
        # 调息 10 分钟按 5%/分钟恢复，应明显高于自然恢复的 2%/分钟
        self.assertGreaterEqual(pet["hp"] - start_hp, int(max_hp * 0.45))
        # 恢复结果需落库，不能只改内存里的临时字典
        stored = await self.pet_mgr.get_pet("u1", apply_regen=False, sync_exp=False)
        self.assertEqual(stored["hp"], pet["hp"])

    async def test_pet_regen_bonus_is_applied(self):
        ok, _ = await self.pet_mgr.adopt_pet(self.player, "冰晶兽")
        self.assertTrue(ok)
        pet = await self.pet_mgr.get_pet("u1")
        max_hp = pet["max_hp"]

        now = int(time.time())
        await self.conn.execute(
            "UPDATE player_pets SET hp = 100, last_regen_time = ? WHERE user_id = ?",
            (now - 60, "u1"),
        )
        await self.conn.commit()

        pet = await self.pet_mgr.get_pet("u1", apply_regen=False, sync_exp=False)
        await self.pet_mgr.refresh_pet(pet, now=now, regen_rate=0.02, regen_bonus=1.0)
        self.assertEqual(pet["hp"], min(max_hp, 100 + int(max_hp * 0.02 * 2)))

    async def test_lazy_recovery_rest_also_settles_pet_energy(self):
        UserStatus = _imp("models_extended").UserStatus
        RecoveryManager = _imp("managers.recovery_manager").RecoveryManager
        ConfigManager = _imp("config_manager").ConfigManager

        ok, _ = await self.pet_mgr.adopt_pet(self.player, "冰晶兽")
        self.assertTrue(ok)
        pet = await self.pet_mgr.get_pet("u1")
        max_hp = pet["max_hp"]

        now = int(time.time())
        player = await self.db.get_player_by_id("u1")
        player.hp = 1
        player.spiritual_qi = 0
        await self.db.update_player(player)

        await self.db.ext.set_user_busy(
            "u1",
            int(UserStatus.RESTING),
            0,
            extra_data={"resting_since": now - 600, "rest_last_tick": now - 600},
        )
        start_hp = max_hp // 2
        await self.conn.execute(
            "UPDATE player_pets SET hp = ?, last_regen_time = ? WHERE user_id = ?",
            (start_hp, now - 600, "u1"),
        )
        await self.conn.commit()

        recovery = RecoveryManager(self.db, ConfigManager(ROOT))
        await recovery.apply_lazy_recovery(player, now=now)

        async with self.conn.execute(
            "SELECT hp FROM player_pets WHERE user_id = ?", ("u1",)
        ) as cursor:
            row = await cursor.fetchone()
        self.assertGreaterEqual(int(row[0]) - start_hp, int(max_hp * 0.45))

    async def test_gold_pet_bonus_stacks_per_pet(self):
        self.player.level_index = 10
        self.player.cultivation_type = "御兽"
        await self.db.update_player(self.player)
        self.assertTrue((await self.pet_mgr.adopt_pet(self.player, "噬金鼠"))[0])
        self.assertTrue((await self.pet_mgr.adopt_pet(self.player, "噬金鼠"))[0])
        pet = await self.pet_mgr.get_pet("u1")
        self.assertAlmostEqual(self.pet_mgr.get_gold_bonus(pet), 0.1)
        self.assertAlmostEqual(await self.pet_mgr.get_life_bonus("u1"), 0.2)

    async def test_feed_only_costs_gold(self):
        self.player.cultivation_type = "灵修"
        self.assertTrue((await self.pet_mgr.adopt_pet(self.player, "火灵狐"))[0])
        pet = await self.pet_mgr.get_pet("u1")
        exp_before = int(self.player.experience)
        gold_before = int(self.player.gold)
        ok, msg = await self.pet_mgr.feed_pet(self.player, "")
        self.assertTrue(ok)
        self.assertEqual(int(self.player.experience), exp_before)
        self.assertEqual(int(self.player.gold), gold_before - 1000)
        pet = await self.pet_mgr.get_pet("u1", apply_regen=False)
        self.assertEqual(int(pet["exp"]), 100)
        self.assertNotIn("修为不足", msg)

    async def test_pet_level_effects_strengthened(self):
        pet = {"pet_type": "火灵狐", "level": 10, "dead_until": 0}
        effects = self.pet_mgr.get_pet_combat_effects(pet)
        self.assertAlmostEqual(effects["crit_rate"], 15.0)

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
        # 御兽 30% 共享：10000 × 30% = 3000，每只灵兽各得 3000（Lv1→Lv4）
        self.assertEqual({(p["level"], p["exp"]) for p in pets}, {(4, 0)})

        ok, _ = await self.pet_mgr.set_main_pet("u1", pets[2]["id"])
        self.assertTrue(ok)
        main_pet, supports, _, atk = await self.pet_mgr.build_pet_lineup(
            "u1", 50_000, 10_000, 0, "御兽"
        )
        self.assertIsNotNone(main_pet)
        self.assertEqual(len(supports), 2)
        self.assertGreater(atk, 10_000)

    async def test_pet_encounter_chance_grows_with_empty_slots(self):
        self.assertAlmostEqual(self.pet_mgr.pet_encounter_chance(0), 0.50)
        self.assertAlmostEqual(self.pet_mgr.pet_encounter_chance(1), 0.65)
        self.assertAlmostEqual(self.pet_mgr.pet_encounter_chance(2), 0.80)
        self.assertAlmostEqual(self.pet_mgr.pet_encounter_chance(3), 0.95)
        self.assertAlmostEqual(self.pet_mgr.pet_encounter_chance(10), 0.95)

    async def test_only_yushou_has_multi_pet_slots(self):
        self.player.level_index = 14
        self.player.cultivation_type = "灵修"
        await self.db.update_player(self.player)
        self.assertEqual(self.pet_mgr.get_pet_slots(self.player), 1)
        self.assertTrue((await self.pet_mgr.adopt_pet(self.player, "火灵狐"))[0])
        ok, _ = await self.pet_mgr.adopt_pet(self.player, "冰晶兽")
        self.assertFalse(ok)

        self.player.cultivation_type = "御兽"
        self.assertEqual(self.pet_mgr.get_pet_slots(self.player), 4)

    async def test_pet_auto_evolves_at_level_20_and_40(self):
        self.player.level_index = 10
        self.player.cultivation_type = "御兽"
        await self.db.update_player(self.player)
        self.assertTrue((await self.pet_mgr.adopt_pet(self.player, "火灵狐"))[0])
        pet = await self.pet_mgr.get_pet("u1", apply_regen=False)
        await self.conn.execute("UPDATE player_pets SET level = 20 WHERE id = ?", (pet["id"],))
        await self.conn.commit()
        pets = await self.pet_mgr.get_pets("u1", apply_regen=False)
        self.assertEqual(pets[0]["pet_type"], "炎灵狐")
        self.assertEqual(pets[0]["pet_name"], "炎灵狐 1")
        self.assertEqual(self.pet_mgr.get_pet_info(pets[0])["stage"], 1)
        self.assertGreater(self.pet_mgr.get_pet_combat_effects(pets[0])["crit_rate"], 30.0)

        await self.conn.execute("UPDATE player_pets SET level = 40 WHERE id = ?", (pet["id"],))
        await self.conn.commit()
        pets = await self.pet_mgr.get_pets("u1", apply_regen=False)
        self.assertEqual(pets[0]["pet_type"], "九尾天狐")
        self.assertEqual(pets[0]["pet_name"], "九尾天狐 1")
        self.assertEqual(self.pet_mgr.get_pet_info(pets[0])["stage"], 2)
        self.assertGreater(self.pet_mgr.get_pet_combat_effects(pets[0])["crit_rate"], 45.0)

    async def test_overflow_conversion_and_counter_cap(self):
        raw_effects = {
            "crit_rate": 150.0,
            "crit_damage": 1.5,
            "lifesteal": 160.0,
            "counter_damage": 150.0,
        }
        effects = dict(raw_effects)
        attack_mult = self.CombatManager.normalize_overflow_effects(effects)
        self.assertAlmostEqual(attack_mult, 1.3)
        self.assertAlmostEqual(effects["crit_rate"], 100.0)
        self.assertAlmostEqual(effects["crit_damage"], 1.75)
        self.assertAlmostEqual(effects["lifesteal"], 100.0)
        self.assertAlmostEqual(effects["counter_damage"], 100.0)

        panel = {
            "hp": 10000, "atk": 2000,
            "physical_atk": 1000, "magic_atk": 1000,
            "defense": 0, "physical_defense": 0, "magic_defense": 0,
            "crit_rate": 0, "crit_damage": 1.5, "lifesteal": 0, "counter_damage": 0,
        }
        self.CombatManager.apply_pet_lineup(
            panel, None, [], dict(raw_effects), None
        )
        self.assertEqual(panel["physical_atk"], 1300)
        self.assertEqual(panel["magic_atk"], 1300)
        self.assertEqual(panel["atk"], 2600)
        self.assertEqual(panel["counter_damage"], 100.0)

    async def test_ice_final_stage_is_glacier_beast(self):
        self.player.cultivation_type = "灵修"
        self.assertTrue((await self.pet_mgr.adopt_pet(self.player, "冰晶兽"))[0])
        pet = await self.pet_mgr.get_pet("u1", apply_regen=False)
        await self.conn.execute("UPDATE player_pets SET level = 40 WHERE id = ?", (pet["id"],))
        await self.conn.commit()
        pets = await self.pet_mgr.get_pets("u1", apply_regen=False)
        self.assertEqual(pets[0]["pet_type"], "冰川巨兽")
        self.assertEqual(pets[0]["pet_name"], "冰川巨兽 1")

    async def test_gold_bonus_scales_after_evolution(self):
        self.player.cultivation_type = "灵修"
        self.assertTrue((await self.pet_mgr.adopt_pet(self.player, "噬金鼠"))[0])
        pet = await self.pet_mgr.get_pet("u1", apply_regen=False)
        await self.conn.execute("UPDATE player_pets SET level = 20 WHERE id = ?", (pet["id"],))
        await self.conn.commit()
        pets = await self.pet_mgr.get_pets("u1", apply_regen=False)
        self.assertEqual(pets[0]["pet_type"], "寻宝金鼠")
        self.assertGreater(await self.pet_mgr.get_life_bonus("u1"), 0.3)

    async def test_feed_crossing_level_20_evolves_for_free(self):
        self.player.cultivation_type = "灵修"
        self.assertTrue((await self.pet_mgr.adopt_pet(self.player, "火灵狐"))[0])
        pet = await self.pet_mgr.get_pet("u1", apply_regen=False)
        await self.conn.execute(
            "UPDATE player_pets SET level = 19, exp = 9450 WHERE id = ?", (pet["id"],)
        )
        await self.conn.commit()
        ok, msg = await self.pet_mgr.feed_pet(self.player, "")
        self.assertTrue(ok)
        self.assertIn("境界突破", msg)
        pet = await self.pet_mgr.get_pet("u1", apply_regen=False, sync_exp=False)
        self.assertEqual(pet["level"], 20)
        self.assertEqual(pet["pet_type"], "炎灵狐")

    async def test_grant_pet_at_high_level_starts_evolved(self):
        self.player.level_index = 10
        self.player.cultivation_type = "御兽"
        await self.db.update_player(self.player)
        ok, _, info = await self.pet_mgr.grant_pet(self.player, "青龙幼崽", level=45)
        self.assertTrue(ok)
        self.assertEqual(info["pet_type"], "太古青龙")
        self.assertEqual(info["pet_name"], "太古青龙 1")
        self.assertEqual(info["stage"], 2)

    async def test_grant_pet_is_free_and_respects_slots(self):
        self.player.level_index = 10
        self.player.cultivation_type = "御兽"
        await self.db.update_player(self.player)
        gold_before = int(self.player.gold)
        ok, _, info = await self.pet_mgr.grant_pet(self.player, "火灵狐", level=3)
        self.assertTrue(ok)
        self.assertEqual(int(self.player.gold), gold_before)
        self.assertEqual(info["level"], 3)
        self.assertEqual(info["slot"], 1)
        self.assertTrue(info["is_main"])

        ok, _, _ = await self.pet_mgr.grant_pet(self.player, "冰晶兽")
        self.assertTrue(ok)
        ok, _, _ = await self.pet_mgr.grant_pet(self.player, "噬金鼠")
        self.assertTrue(ok)
        ok, _, _ = await self.pet_mgr.grant_pet(self.player, "青龙幼崽")
        self.assertFalse(ok)
        self.assertIsNone(await self.pet_mgr.try_exploration_encounter(self.player))

    async def test_exploration_pet_encounter_triggers_with_empty_slot(self):
        self.player.level_index = 10
        self.player.cultivation_type = "御兽"
        await self.db.update_player(self.player)
        self.pet_mgr.PET_ENCOUNTER_BASE_CHANCE = 1.0
        self.pet_mgr.PET_ENCOUNTER_MAX_CHANCE = 1.0
        info = await self.pet_mgr.try_exploration_encounter(
            self.player, "秘境探索中", commit=True
        )
        self.assertIsNotNone(info)
        self.assertEqual(info["source"], "秘境探索中")
        self.assertEqual(info["empty_before"], 3)
        pets = await self.pet_mgr.get_pets("u1", apply_regen=False)
        self.assertEqual(len(pets), 1)
        self.assertIn("灵兽奇遇", self.pet_mgr.format_exploration_encounter(info))
