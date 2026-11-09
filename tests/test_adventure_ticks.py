# tests/test_adventure_ticks.py
# 历练分段结算：每 5 分钟一段、终段大结算、中途退出无惩罚、总量翻倍
import asyncio
import json
import random
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
TICK = CFG["tick_settlement"]


def make_manager():
    return AdventureManager(None)


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


def old_full_rewards(route, level):
    """改版前的整趟收益公式（用于核对翻倍）。"""
    minutes = max(1, int(route.get("reward_duration") or route["duration"]) // 60)
    exp = (
        minutes * route["base_exp_per_min"]
        + level * route["level_bonus_exp"]
        + route["completion_bonus"]["exp"]
    )
    gold = (
        minutes * route["base_gold_per_min"]
        + level * route["level_bonus_gold"]
        + route["completion_bonus"]["gold"]
    )
    return exp, gold


# ---------- 极简假 DB：只实现分段结算用到的接口 ----------

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
    """同时支持 await conn.execute(...) 与 async with conn.execute(...)。"""

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


def make_run(route, level=5, now=None, extra=None):
    now = int(time.time()) if now is None else now
    mgr = make_manager()
    player = Player(user_id="u1", level_index=level, experience=0, gold=0)
    segments = mgr.tick_count(route["duration"])
    base_extra = {
        "route_key": route["key"],
        "umo": "umo://test",
        "tick_seconds": mgr.tick_seconds,
        "ticks_total": segments,
        "ticks_done": 0,
        "next_tick_at": now + mgr.tick_seconds,
        "injuries": 0,
        "earned_exp": 0,
        "earned_gold": 0,
    }
    base_extra.update(extra or {})
    cd = FakeCd(now, route["duration"], base_extra)
    db = FakeDb(player, cd)
    mgr.db = db
    # 事件倍率固定为 1，便于精确核对总量
    mgr._trigger_route_event = lambda r: flat_event(r)
    return mgr, player, cd, db


class TestTickConfig(unittest.TestCase):
    def test_tick_seconds_is_five_minutes(self):
        self.assertEqual(TICK["tick_seconds"], 300)

    def test_reward_multiplier_doubles(self):
        self.assertEqual(TICK["reward_multiplier"], 2.0)

    def test_share_split(self):
        self.assertGreater(TICK["tick_share"], 0)
        self.assertLess(TICK["tick_share"], 1)

    def test_tick_count_matches_duration(self):
        mgr = make_manager()
        for route in CFG["routes"]:
            expected = max(2, round(route["duration"] / TICK["tick_seconds"]))
            self.assertEqual(
                mgr.tick_count(route["duration"]), expected,
                f"{route['name']} 段数不对",
            )

    def test_every_route_has_at_least_one_tick(self):
        mgr = make_manager()
        for route in CFG["routes"]:
            self.assertGreaterEqual(mgr.tick_count(route["duration"]), 2, route["name"])


class TestRewardMath(unittest.TestCase):
    def test_full_pool_is_double_old_formula(self):
        mgr = make_manager()
        player = Player(user_id="u", level_index=20)
        for key in ("scout", "herb_valley", "chaos_throne"):
            route = ROUTES[key]
            pool = mgr._full_reward_pool(player, route)
            old_exp, old_gold = old_full_rewards(route, 20)
            self.assertAlmostEqual(pool["exp"] / old_exp, 2.0, places=2, msg=key)
            self.assertAlmostEqual(pool["gold"] / old_gold, 2.0, places=2, msg=key)

    def test_segments_sum_to_full_pool(self):
        """n-1 个小结 + 大结算 = 整趟总量（事件倍率为 1 时）。"""
        mgr = make_manager()
        route = ROUTES["scout"]
        player = Player(user_id="u", level_index=10)
        pool = mgr._full_reward_pool(player, route)
        segments = mgr.tick_count(route["duration"])
        event = flat_event(route)
        total = sum(
            mgr.segment_rewards(player, route, event, share=mgr.tick_share, segments=segments - 1)["exp"]
            for _ in range(segments - 1)
        )
        total += mgr.segment_rewards(
            player, route, event, share=1 - mgr.tick_share, segments=1
        )["exp"]
        self.assertAlmostEqual(total / pool["exp"], 1.0, delta=0.02)

    def test_tick_share_is_smaller_than_final(self):
        """小结算只是零花钱：单段收益必须明显小于终段。"""
        mgr = make_manager()
        route = ROUTES["chaos_throne"]
        player = Player(user_id="u", level_index=26)
        segments = mgr.tick_count(route["duration"])
        event = flat_event(route)
        tick = mgr.segment_rewards(player, route, event, share=mgr.tick_share, segments=segments - 1)
        final = mgr.segment_rewards(player, route, event, share=1 - mgr.tick_share, segments=1)
        self.assertLess(tick["exp"] * 2, final["exp"])


class TestTickFlow(unittest.TestCase):
    def test_no_settlement_before_first_tick(self):
        mgr, player, cd, _ = make_run(ROUTES["scout"])
        result = asyncio.run(mgr.settle_due("u1", now=cd.create_time + 60))
        self.assertEqual(result["ticks"], 0)
        self.assertEqual(player.experience, 0)
        self.assertEqual(cd.type, UserStatus.ADVENTURING)

    def test_first_tick_credits_small_reward(self):
        mgr, player, cd, _ = make_run(ROUTES["scout"])
        result = asyncio.run(mgr.settle_due("u1", now=cd.create_time + 300))
        self.assertEqual(result["ticks"], 1)
        self.assertGreater(player.experience, 0)
        self.assertGreater(player.gold, 0)
        self.assertIn("历练小结", result["messages"][0])
        self.assertEqual(cd.type, UserStatus.ADVENTURING)
        extra = cd.get_extra_data()
        self.assertEqual(extra["ticks_done"], 1)
        self.assertEqual(extra["earned_exp"], player.experience)

    def test_offline_catchup_settles_all_due_ticks(self):
        mgr, player, cd, _ = make_run(ROUTES["scout"])
        result = asyncio.run(mgr.settle_due("u1", now=cd.create_time + 900))
        self.assertEqual(result["ticks"], 3)
        self.assertIn("补算 3 段", result["messages"][0])

    def test_exit_keeps_settled_rewards_and_has_no_penalty(self):
        mgr, player, cd, db = make_run(ROUTES["scout"])
        asyncio.run(mgr.settle_due("u1", now=cd.create_time + 600))
        exp_before, gold_before = player.experience, player.gold
        self.assertGreater(exp_before, 0)

        ok, msg = asyncio.run(mgr.abandon_adventure("u1"))
        self.assertTrue(ok)
        self.assertIn("无任何惩罚", msg)
        self.assertEqual(player.experience, exp_before, "退出把已结算的修为扣掉了")
        self.assertEqual(player.gold, gold_before)
        self.assertEqual(cd.type, UserStatus.IDLE)
        self.assertFalse(db.config, "退出不应产生任何违约金/冷却")

    def test_exit_before_any_tick_keeps_nothing(self):
        mgr, player, cd, _ = make_run(ROUTES["scout"])
        ok, msg = asyncio.run(mgr.abandon_adventure("u1"))
        # 假时钟里刚出发，不会误判为完成
        self.assertTrue(ok)
        self.assertIn("已走完 0/", msg)

    def test_full_run_gives_big_final_and_frees_player(self):
        mgr, player, cd, _ = make_run(ROUTES["scout"])
        result = asyncio.run(mgr.settle_due("u1", now=cd.scheduled_time))
        self.assertIsNotNone(result["final"])
        self.assertEqual(cd.type, UserStatus.IDLE)
        self.assertIn("大结算", result["messages"][-1])
        self.assertGreater(result["final"]["exp_reward"], 0)
        self.assertGreater(result["final"]["total_exp"], result["final"]["exp_reward"])

    def test_full_run_total_is_double_old(self):
        mgr, player, cd, _ = make_run(ROUTES["scout"], level=5)
        result = asyncio.run(mgr.settle_due("u1", now=cd.scheduled_time))
        old_exp, old_gold = old_full_rewards(ROUTES["scout"], 5)
        self.assertAlmostEqual(result["final"]["total_exp"] / old_exp, 2.0, delta=0.05)
        self.assertAlmostEqual(result["final"]["total_gold"] / old_gold, 2.0, delta=0.05)

    def test_partial_run_beats_old_rate(self):
        """中途退出不亏：按时间折算后，小结收益必须不低于旧系统同时间的结算量。"""
        mgr, player, cd, _ = make_run(ROUTES["scout"], level=5)
        duration = ROUTES["scout"]["duration"]
        elapsed = 900
        asyncio.run(mgr.settle_due("u1", now=cd.create_time + elapsed))
        old_exp, _ = old_full_rewards(ROUTES["scout"], 5)
        ratio = player.experience / (old_exp * elapsed / duration)
        self.assertGreaterEqual(ratio, 1.0, f"提前退出只拿到旧系统的 {ratio:.0%}，等于变相惩罚")

    def test_settle_twice_does_not_double_pay(self):
        mgr, player, cd, _ = make_run(ROUTES["scout"])
        asyncio.run(mgr.settle_due("u1", now=cd.scheduled_time))
        exp_after = player.experience
        again = asyncio.run(mgr.settle_due("u1", now=cd.scheduled_time + 60))
        self.assertFalse(again["ok"])
        self.assertEqual(player.experience, exp_after)

    def test_legacy_run_without_tick_state_is_adopted(self):
        """升级前开始的一次性历练：存档里没有 ticks_total，也要能正常接管。"""
        mgr, player, cd, _ = make_run(
            ROUTES["scout"], extra={"ticks_total": 0, "ticks_done": 0}
        )
        result = asyncio.run(mgr.settle_due("u1", now=cd.create_time + 300))
        self.assertEqual(result["ticks"], 1)
        self.assertGreater(player.experience, 0)


class TestItemChanceNormalization(unittest.TestCase):
    """分段不是把掉落翻十倍：整趟期望掉落数要维持在旧版的 2 倍左右。"""

    def _run_drops(self, mgr, route, player, segments, trials=400):
        event = dict(flat_event(route), item_chance=100)
        total = 0
        for _ in range(trials):
            count = 0
            for _ in range(segments - 1):
                count += len(mgr._roll_drops(player, route, event, chance_scale=mgr.tick_item_mult / segments))
            count += len(mgr._roll_drops(player, route, event, chance_scale=mgr.final_item_mult))
            total += count
        return total / trials

    def test_drops_scale_with_run_not_with_segments(self):
        mgr = make_manager()
        route = ROUTES["scout"]
        player = Player(user_id="u", level_index=5)
        segments = mgr.tick_count(route["duration"])
        per_run = self._run_drops(mgr, route, player, segments)
        self.assertLess(per_run, 2.5, f"整趟掉了 {per_run:.1f} 件，分段把掉落放大了")
        self.assertGreater(per_run, 1.4, f"整趟只掉 {per_run:.1f} 件，翻倍没落地")

    def test_short_route_not_penalized(self):
        """段数少的长短路线，整趟期望掉落要接近，不能按段数分配。"""
        mgr = make_manager()
        player = Player(user_id="u", level_index=5)
        short = self._run_drops(mgr, ROUTES["scout"], player, mgr.tick_count(ROUTES["scout"]["duration"]))
        long = self._run_drops(mgr, ROUTES["chaos_throne"], player, mgr.tick_count(ROUTES["chaos_throne"]["duration"]))
        self.assertLess(abs(short - long), 0.35, f"短 {short:.2f} / 长 {long:.2f} 掉落差太多")


if __name__ == "__main__":
    unittest.main()
