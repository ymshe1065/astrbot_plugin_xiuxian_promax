# tests/test_adventure_ticks.py
# v7.7 历练：走完全程一次性结算、修为按突破需求折算、灵根/功法不参与
import json
import sys
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

from astrbot_plugin_xiuxian_promax.managers.adventure_manager import AdventureManager  # noqa: E402
from astrbot_plugin_xiuxian_promax.models import Player  # noqa: E402
from astrbot_plugin_xiuxian_promax.models_extended import UserStatus  # noqa: E402

CFG_PATH = ROOT / "config" / "adventure_config.json"
with open(CFG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)
ROUTES = {r["key"]: r for r in CFG["routes"]}
LV_PATH = ROOT / "config" / "level_config.json"
with open(LV_PATH, encoding="utf-8") as _f:
    LEVELS = json.load(_f)

# 闭关满配：灵根 3.2（先天道体）× 功法 1.30 × 丹药符箓 3.0
CLOSED_ROOT_MAX = 3.2
CLOSED_TECHNIQUE_MAX = 1.30
CLOSED_PILL_MAX = 3.0
CLOSED_RATE_PER_HOUR = 0.0025


class FakeCM:
    def get_level_data(self, cultivation_type="灵修"):
        return LEVELS


def make_manager():
    return AdventureManager(None, config_manager=FakeCM())


def flat_event(route, key="stub"):
    return {
        "key": key,
        "name": "平稳",
        "desc": "一路无事。",
        "exp_mult": 1.0,
        "gold_mult": 1.0,
        "item_chance": 0,
        "bonus_progress": 0,
    }


# ---------- 极简假 DB ----------

class FakeCd:
    def __init__(self, now, duration, extra):
        self.type = UserStatus.ADVENTURING
        self.create_time = now
        self.scheduled_time = now + duration
        self.extra_data = json.dumps(extra, ensure_ascii=False)

    def get_extra_data(self):
        return json.loads(self.extra_data)


class FakeExt:
    def __init__(self, db):
        self.db = db
        self.cd = None

    async def get_user_cd(self, uid):
        return self.cd

    async def set_user_free(self, uid, commit=True):
        if self.cd:
            self.cd.type = UserStatus.IDLE

    async def set_system_config(self, key, value):
        self.db.config[key] = value


class FakeCursor:
    def __init__(self, db, sql, params):
        self.db = db
        self.sql = sql
        self.params = params
        if sql.startswith("UPDATE user_cd SET extra_data") and db.ext.cd:
            db.ext.cd.extra_data = params[0]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchall(self):
        return []

    async def fetchone(self):
        return None

    def __await__(self):
        async def _done():
            return self

        return _done().__await__()


class FakeConn:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, params=()):
        return FakeCursor(self.db, sql, params)

    async def commit(self):
        self.db.commits += 1

    async def rollback(self):
        self.db.rollbacks += 1


class FakeDb:
    def __init__(self, player, cd):
        self.player = player
        self.config = {}
        self.commits = 0
        self.rollbacks = 0
        self.conn = FakeConn(self)
        self.ext = FakeExt(self)
        self.ext.cd = cd

    async def get_player_by_id(self, uid):
        return self.player

    async def update_player(self, player, commit=True):
        self.player = player


def make_run(route, level=5, now=None, elapsed=0):
    now = int(time.time()) if now is None else now
    mgr = make_manager()
    player = Player(user_id="u1", level_index=level, experience=0, gold=0)
    cd = FakeCd(now - elapsed, route["duration"], {"route_key": route["key"], "umo": "umo://test"})
    db = FakeDb(player, cd)
    mgr.db = db
    mgr._trigger_route_event = lambda r: flat_event(r)
    return mgr, player, cd, db


class TestConfig(unittest.TestCase):
    def test_no_tick_settlement(self):
        self.assertNotIn("tick_settlement", CFG, "历练不应再有分段结算配置")

    def test_exp_model_present(self):
        model = CFG.get("exp_model") or {}
        rate = float(model.get("base_rate_per_hour"))
        # v7.8.2：基准从「满配灵根×功法反推」改成「闭关零加成 0.25% × 3」，
        # 差距不再随灵根漂移（旧值 0.01656 在零加成时是闭关的 6.6 倍）。
        self.assertAlmostEqual(rate, 0.0075, places=5)
        self.assertAlmostEqual(rate, 0.0025 * 3, places=6)
        self.assertTrue(model.get("pill_talisman_only"))

    def test_every_route_within_half_an_hour(self):
        for r in CFG["routes"]:
            self.assertGreaterEqual(r["duration"], 900, f"{r['name']} 短于 15 分钟")
            self.assertLessEqual(r["duration"], 1800, f"{r['name']} 超过 30 分钟")

    def test_no_route_keeps_old_hourly_duration(self):
        self.assertLess(max(r["duration"] for r in CFG["routes"]), 3600)


class TestExpModel(unittest.TestCase):
    LEVEL = 26

    def _route(self):
        cands = [r for r in CFG["routes"] if r["min_level"] <= self.LEVEL]
        return max(cands, key=lambda r: r["duration"])

    def test_exp_scales_with_breakthrough_need(self):
        mgr = make_manager()
        route = self._route()
        low = mgr._full_reward_pool(Player(user_id="a", level_index=18), route)["exp"]
        high = mgr._full_reward_pool(Player(user_id="b", level_index=26), route)["exp"]
        self.assertGreater(high, low * 5, "高境界历练修为没有跟着突破需求放大")

    def test_root_does_not_affect_adventure(self):
        """灵根只在闭关上生效，历练拿了同样的默认倍率。"""
        mgr = make_manager()
        route = self._route()
        a = Player(user_id="a", level_index=self.LEVEL, spiritual_root="伪灵根")
        b = Player(user_id="b", level_index=self.LEVEL, spiritual_root="先天道体灵根")
        self.assertEqual(
            mgr._full_reward_pool(a, route)["exp"],
            mgr._full_reward_pool(b, route)["exp"],
        )

    def test_adventure_is_three_times_zero_bonus_closed(self):
        """零加成下历练应约为闭关的 3 倍（v7.8.2 新基准）。

        旧基准是「满配时历练 ≈ 闭关 90%」，但历练率是常数、闭关率随灵根/功法缩放，
        结果零加成时历练反而是闭关的 6.6 倍 —— 灵根越差越被迫全走历练。
        现在基准挪到零加成，差距恒定；满配时历练只有闭关的 41%，这是刻意接受的：
        顶级灵根换纯闭关路线，普通灵根靠历练补。
        """
        mgr = make_manager()
        route = self._route()
        player = Player(user_id="a", level_index=self.LEVEL)
        need = int(LEVELS[self.LEVEL]["exp_needed"])
        hours = route["duration"] / 3600.0

        adventure_hourly = mgr._full_reward_pool(player, route)["exp"] / hours
        closed_hourly_bare = need * CLOSED_RATE_PER_HOUR
        ratio = adventure_hourly / closed_hourly_bare
        self.assertAlmostEqual(ratio, 3.0, delta=0.05, msg=f"历练/零加成闭关 = {ratio:.2f}")

    def test_pill_bonus_applies(self):
        mgr = make_manager()
        route = self._route()
        player = Player(user_id="a", level_index=self.LEVEL)
        base = mgr._full_reward_pool(player, route)["exp"]
        mgr._cultivation_mult = lambda p: 3.0
        boosted = mgr._full_reward_pool(player, route)["exp"]
        self.assertAlmostEqual(boosted / base, 3.0, places=2)


class TestSettlement(unittest.TestCase):
    LEVEL = 20

    def _route(self):
        cands = [r for r in CFG["routes"] if r["min_level"] <= self.LEVEL]
        return max(cands, key=lambda r: r["duration"])

    def test_cannot_settle_before_finish(self):
        route = self._route()
        mgr, _, _, _ = make_run(route, level=self.LEVEL, elapsed=60)
        ok, msg, data = asyncio_run(mgr.finish_adventure("u1"))
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn("还未走完", msg)

    def test_settle_after_finish_grants_once(self):
        route = self._route()
        mgr, player, _, _ = make_run(route, level=self.LEVEL, elapsed=route["duration"] + 5)
        ok, msg, data = asyncio_run(mgr.finish_adventure("u1"))
        self.assertTrue(ok, msg)
        self.assertGreater(player.experience, 0)
        self.assertEqual(data["total_exp"], player.experience)

    def test_settle_twice_does_not_double_pay(self):
        route = self._route()
        mgr, player, _, _ = make_run(route, level=self.LEVEL, elapsed=route["duration"] + 5)
        asyncio_run(mgr.finish_adventure("u1"))
        exp_after_first = player.experience
        ok, _, _ = asyncio_run(mgr.finish_adventure("u1"))
        self.assertFalse(ok, "已结算后不应再次发奖")
        self.assertEqual(player.experience, exp_after_first)

    def test_exit_before_finish_gets_nothing(self):
        route = self._route()
        mgr, player, cd, _ = make_run(route, level=self.LEVEL, elapsed=60)
        ok, msg = asyncio_run(mgr.abandon_adventure("u1"))
        self.assertTrue(ok)
        self.assertEqual(player.experience, 0)
        self.assertEqual(player.gold, 0)
        self.assertEqual(cd.type, UserStatus.IDLE)
        self.assertIn("没有获得任何奖励", msg)

    def test_exit_after_finish_settles_instead(self):
        route = self._route()
        mgr, player, cd, _ = make_run(route, level=self.LEVEL, elapsed=route["duration"] + 5)
        ok, msg = asyncio_run(mgr.abandon_adventure("u1"))
        self.assertTrue(ok)
        self.assertGreater(player.experience, 0, "到点后退出应按完成结算")
        self.assertEqual(cd.type, UserStatus.IDLE)

    def test_finish_frees_player(self):
        route = self._route()
        mgr, _, cd, _ = make_run(route, level=self.LEVEL, elapsed=route["duration"] + 5)
        asyncio_run(mgr.finish_adventure("u1"))
        self.assertEqual(cd.type, UserStatus.IDLE)

    def test_gold_scales_with_reward_duration(self):
        route = self._route()
        mgr, player, _, _ = make_run(route, level=self.LEVEL, elapsed=route["duration"] + 5)
        asyncio_run(mgr.finish_adventure("u1"))
        minutes = max(1, route["reward_duration"] // 60)
        expected = (
            minutes * route["base_gold_per_min"]
            + self.LEVEL * route["level_bonus_gold"]
            + route["completion_bonus"]["gold"]
        ) * mgr.gold_multiplier
        self.assertEqual(player.gold, expected)


class TestItemRolls(unittest.TestCase):
    def test_one_roll_per_run(self):
        mgr = make_manager()
        self.assertEqual(mgr.ITEM_ROLLS, 1, "时长减半后每趟只该判定一次，否则材料翻倍")
        self.assertGreater(mgr.ITEM_CHANCE_SCALE, 1.0)


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
