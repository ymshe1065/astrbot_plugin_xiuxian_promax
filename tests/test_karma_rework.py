# tests/test_karma_rework.py
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


class _FakeExt:
    def __init__(self):
        self.store = {}

    async def get_system_config(self, key):
        return self.store.get(key)

    async def set_system_config(self, key, value):
        self.store[key] = value


class _FakeDB:
    def __init__(self):
        self.ext = _FakeExt()

    async def update_player(self, player, commit=True):
        return None


class TestKarmaRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = _imp("karma_rules")

    def test_charity_cost(self):
        self.assertEqual(self.rules.charity_cost(0, 0), 2000)
        self.assertEqual(self.rules.charity_cost(100_000, 0), 5000)
        self.assertEqual(self.rules.charity_cost(30_000, 10_000), 2000)
        self.assertEqual(self.rules.charity_cost(1_000_000, 1_000_000), 100_000)

    def test_karma_rate_bonus_uncapped_positive_and_floor_negative(self):
        self.assertAlmostEqual(self.rules.karma_rate_bonus(5), 0.05)
        self.assertAlmostEqual(self.rules.karma_rate_bonus(50), 0.50)
        self.assertAlmostEqual(self.rules.karma_rate_bonus(-10), -0.10)
        self.assertAlmostEqual(self.rules.karma_rate_bonus(-100), -0.50)

    def test_encounter_karma_values_are_doubled(self):
        events = _imp("managers.encounter_events").EVENTS
        values = set()
        for ev in events:
            for opt in ev["options"]:
                for outcome in (opt.get("win"), opt.get("lose")):
                    if isinstance(outcome, dict) and outcome.get("karma"):
                        values.add(int(outcome["karma"]))
        self.assertTrue(values)
        self.assertTrue(all(abs(v) == 2 for v in values), values)


class TestMeritChoice(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.EncounterManager = _imp("managers.encounter_manager").EncounterManager
        cls.Player = _imp("models").Player

    async def test_merit_choice_deducts_reward_and_adds_karma_silently(self):
        mgr = self.EncounterManager(_FakeDB(), None, None)
        player = self.Player(user_id="u1", user_name="t", gold=10_000, experience=10_000, karma=0)
        prompt = await mgr.maybe_trigger_merit(player, 10_000, 10_000, chance=100)
        self.assertIsNotNone(prompt)
        self.assertNotIn("功德", prompt)

        ok, msg = await mgr.resolve_choice(player, 1)
        self.assertTrue(ok, msg)
        self.assertEqual(player.gold, 6_000)
        self.assertEqual(player.experience, 6_000)
        self.assertEqual(player.karma, 2)
        self.assertNotIn("功德", msg)

    async def test_merit_choice_decline_keeps_rewards(self):
        mgr = self.EncounterManager(_FakeDB(), None, None)
        player = self.Player(user_id="u1", user_name="t", gold=10_000, experience=10_000, karma=0)
        await mgr.maybe_trigger_merit(player, 10_000, 10_000, chance=100)
        ok, msg = await mgr.resolve_choice(player, 2)
        self.assertTrue(ok, msg)
        self.assertEqual(player.gold, 10_000)
        self.assertEqual(player.experience, 10_000)
        self.assertEqual(player.karma, 0)


class TestKarmaMigration(unittest.IsolatedAsyncioTestCase):
    async def test_v58_clears_all_karma(self):
        try:
            import aiosqlite
        except Exception:
            self.skipTest("aiosqlite 不可用")
        migration = _imp("data.migration")
        conn = await aiosqlite.connect(":memory:")
        try:
            await conn.execute("CREATE TABLE players (user_id TEXT, karma INTEGER)")
            await conn.executemany(
                "INSERT INTO players (user_id, karma) VALUES (?, ?)",
                [("u1", 10), ("u2", -3), ("u3", 0)],
            )
            await conn.commit()
            await migration._migrate_to_v58(conn, None)
            async with conn.execute("SELECT user_id, karma FROM players ORDER BY user_id") as cur:
                rows = await cur.fetchall()
            self.assertEqual([r[1] for r in rows], [0, 0, 0])
        finally:
            await conn.close()


if __name__ == "__main__":
    unittest.main()
