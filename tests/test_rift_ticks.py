# tests/test_rift_ticks.py
# v7.7 秘境分段结算：每 5 分钟一段 + 终段大结算、退出保留已结算、总量守恒
import asyncio
import json
import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub
from tests._cd_stub import SlotCdStubMixin

_install_astrbot_stub()

def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


RiftManager = _imp("managers.rift_manager").RiftManager
Player = _imp("models").Player
UserStatus = _imp("models_extended").UserStatus
Rift = _imp("models_extended").Rift

CFG_PATH = ROOT / "config" / "rift_config.json"
with open(CFG_PATH, encoding="utf-8") as _f:
    RIFT_CFG = json.load(_f)
TICK = RIFT_CFG["tick_settlement"]


def make_rift(duration=1800, exp=(100000, 100000), gold=(20000, 20000), drops=None):
    rewards = {
        "exp": list(exp),
        "gold": list(gold),
        "item_chance": 82,
        "extra_drop_chance": 45,
        "equipment_chance": 9,
        "drops": drops or [],
    }
    return Rift(
        rift_id=1,
        rift_name="测试秘境",
        rift_level=2,
        required_level=0,
        rewards=json.dumps(rewards),
        difficulty=2,
        duration=duration,
    )


class FakeCd:
    def __init__(self, now, duration, extra):
        self.type = UserStatus.EXPLORING
        self.create_time = now
        self.scheduled_time = now + duration
        self.extra_data = json.dumps(extra, ensure_ascii=False)

    def get_extra_data(self):
        return json.loads(self.extra_data)


class FakeExt(SlotCdStubMixin):
    def __init__(self, db):
        self.db = db
        self.cd = None

    async def get_user_cd(self, uid):
        return self.cd

    async def set_system_config(self, key, value):
        self.db.config[key] = value

    async def get_rift_by_id(self, rift_id):
        return self.db.rift


class FakeCursor:
    def __init__(self, db, sql, params):
        self.db = db
        self.rowcount = 1  # CAS 全部命中：真实库里 rowcount 由 sqlite 给出
        if sql.startswith("UPDATE user_cd SET extra_data") and db.ext.cd:
            db.ext.cd.extra_data = params[0]
        elif sql.startswith("UPDATE user_cd SET type") and db.ext.cd:
            db.ext.cd.type = params[0]

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
    def __init__(self, player, cd, rift):
        self.player = player
        self.rift = rift
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


def make_run(duration=1800, elapsed=0, extra=None):
    now = int(time.time())
    mgr = RiftManager(None)
    rift = make_rift(duration=duration)
    player = Player(user_id="u1", level_index=10, experience=0, gold=0)
    base_extra = {
        "rift_id": 1,
        "rift_level": 2,
        "difficulty": 2,
        "umo": "umo://test",
        "tick_seconds": mgr.tick_seconds,
        "ticks_total": mgr.tick_count(duration),
        "ticks_done": 0,
        "next_tick_at": now - elapsed + mgr.tick_seconds,
        "base_exp": 100000,
        "base_gold": 20000,
        "earned_exp": 0,
        "earned_gold": 0,
    }
    base_extra.update(extra or {})
    cd = FakeCd(now - elapsed, duration, base_extra)
    db = FakeDb(player, cd, rift)
    mgr.db = db
    mgr._pick_event = lambda: {"desc": "测试事件", "drop_mod": 0}
    return mgr, player, cd, db


def run(coro):
    return asyncio.run(coro)


class TestConfig(unittest.TestCase):
    def test_tick_seconds_is_five_minutes(self):
        self.assertEqual(TICK["tick_seconds"], 300)

    def test_share_split(self):
        self.assertAlmostEqual(TICK["tick_share"], 0.6, places=2)

    def test_item_chance_total_keeps_expectation(self):
        """分段摊薄 + 大结算 = 1.0，材料期望不因拆段而变化。"""
        total = TICK["tick_item_chance_mult"] + TICK["final_item_chance_mult"]
        self.assertAlmostEqual(total, 1.0, places=2)

    def test_tick_count_matches_duration(self):
        mgr = RiftManager(None)
        self.assertEqual(mgr.tick_count(1200), 4)
        self.assertEqual(mgr.tick_count(1800), 6)
        self.assertEqual(mgr.tick_count(2700), 9)


class TestSettlement(unittest.TestCase):
    def test_nothing_before_first_tick(self):
        mgr, player, _, _ = make_run(elapsed=120)
        result = run(mgr.settle_due("u1"))
        self.assertEqual(result["ticks"], 0)
        self.assertIsNone(result["final"])
        self.assertEqual(player.experience, 0)

    def test_first_tick_credits_share(self):
        mgr, player, _, _ = make_run(elapsed=300)
        result = run(mgr.settle_due("u1"))
        self.assertEqual(result["ticks"], 1)
        # 100000 × 0.6 ÷ 5 段 = 12000
        self.assertEqual(player.experience, 12000)

    def test_offline_catchup_settles_all_due(self):
        mgr, player, _, _ = make_run(elapsed=1500)
        result = run(mgr.settle_due("u1"))
        self.assertEqual(result["ticks"], 5)
        self.assertEqual(player.experience, 60000, "5 段小结应拿满 60%")
        self.assertIn("补算", result["messages"][0])

    def test_final_adds_rest_and_frees_player(self):
        mgr, player, cd, _ = make_run(elapsed=1800)
        result = run(mgr.settle_due("u1"))
        self.assertIsNotNone(result["final"])
        self.assertEqual(player.experience, 100000, "小结 60% + 大结算 40% = 100%")
        self.assertEqual(cd.type, UserStatus.IDLE)

    def test_settle_twice_does_not_double_pay(self):
        mgr, player, _, _ = make_run(elapsed=1800)
        run(mgr.settle_due("u1"))
        exp_after = player.experience
        result = run(mgr.settle_due("u1"))
        self.assertIsNone(result["final"])
        self.assertEqual(player.experience, exp_after)

    def test_exit_keeps_settled_rewards(self):
        mgr, player, cd, _ = make_run(elapsed=900)
        ok, msg = run(mgr.exit_rift("u1"))
        self.assertTrue(ok)
        # 3 段小结 = 36000，剩余段落与大结算放弃
        self.assertEqual(player.experience, 36000)
        self.assertEqual(cd.type, UserStatus.IDLE)
        self.assertIn("退出", msg)

    def test_exit_after_finish_settles_instead(self):
        mgr, player, cd, _ = make_run(elapsed=1800)
        ok, _ = run(mgr.exit_rift("u1"))
        self.assertTrue(ok)
        self.assertEqual(player.experience, 100000)
        self.assertEqual(cd.type, UserStatus.IDLE)

    def test_legacy_run_without_tick_state_is_adopted(self):
        """升级前进入的秘境：extra 里没有 ticks_total/base_exp，也要能接管。"""
        extra = {"tick_seconds": 300, "ticks_done": 0, "earned_exp": 0, "earned_gold": 0}
        mgr, player, _, _ = make_run(elapsed=300, extra=extra)
        # 手动去掉 ticks_total / base_exp 模拟旧存档
        mgr.db.ext.cd.extra_data = json.dumps(
            {"rift_id": 1, "rift_level": 2, "difficulty": 2, "umo": "umo://test"},
            ensure_ascii=False,
        )
        result = run(mgr.settle_due("u1"))
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["ticks"], 1)

    def test_not_in_rift_returns_idle(self):
        mgr, _, cd, _ = make_run(elapsed=0)
        cd.type = UserStatus.IDLE
        result = run(mgr.settle_due("u1"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "idle")


if __name__ == "__main__":
    unittest.main()
