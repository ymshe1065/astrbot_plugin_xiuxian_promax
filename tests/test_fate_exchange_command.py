# tests/test_fate_exchange_command.py
# `/仙缘兑换` 指令回归
#
# 背景：v3.45.8 引入「灵石换仙缘点」后，`_exchange_gold_for_points` 一直引用
# 未定义的 `event`——只要玩家发 `/仙缘兑换 灵石 N` 就抛 NameError，
# 而网页端走的是 fate_shop_panel 的另一套实现，所以「网页能换、指令报错」，
# 这个洞藏了很久。这里把指令路径钉住：
#   1. 灵石换点必须真的走通（不抛 NameError）；
#   2. 额度、灵石不足、非法点数三种失败分支文案不丢；
#   3. `/仙缘兑换 <编号>` 买商品的路不被改坏。
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests._astrbot_stub import install as _install_astrbot_stub

_install_astrbot_stub()


def _imp(module_path: str):
    return __import__(f"{PKG}.{module_path}", fromlist=["x"])


rules = _imp("core.reincarnation_rules")
handlers_mod = _imp("handlers.reincarnation_handlers")
Player = _imp("models").Player
GOLD_PER_FATE_POINT = rules.GOLD_PER_FATE_POINT
FATE_EXCHANGE_MAX = rules.FATE_EXCHANGE_MAX
fate_exchange_key = rules.fate_exchange_key


class _Ev:
    """最小事件桩：只记录 plain_result 的文本。"""

    def __init__(self):
        self.plain = []

    def plain_result(self, text):
        self.plain.append(text)
        return ("plain", text)


class _FakeExt:
    def __init__(self, used=0):
        self.system = {}
        if used:
            self.system[fate_exchange_key("u1")] = str(used)

    async def get_system_config(self, key):
        return self.system.get(key)

    async def set_system_config(self, key, value):
        self.system[key] = value


class _FakeDB:
    def __init__(self, used=0):
        self.ext = _FakeExt(used)
        self.saved = {}

    async def update_player(self, player, commit=True):
        self.saved[player.user_id] = player


class _FakeReincMgr:
    def __init__(self, ok=True, msg="✅ 已购买 修炼加速"):
        self.ok = ok
        self.msg = msg
        self.calls = []

    async def buy_fate_shop_item(self, player, idx):
        self.calls.append((player.user_id, idx))
        return self.ok, self.msg


def _player(gold=0):
    p = Player(user_id="u1")
    p.gold = gold
    p.fate_points = 0
    return p


def _handlers(db=None, mgr=None):
    return handlers_mod.ReincarnationHandlers(db or _FakeDB(), mgr or _FakeReincMgr())


async def _run(handlers, player, event, item_id="0", count="0"):
    """绕过 @player_required（它会开玩家锁并重读档），直接跑被装饰的函数体。

    祭祀现在按概率判定，回归用例统一把基础成功率顶到 100%，
    只验证「扣钱 / 记点 / 记额度」的管线；概率本身在 test_fate_ritual 里单独测。
    """
    fn = handlers.handle_fate_exchange.__wrapped__
    out = []
    with mock.patch.object(rules, "FATE_RITUAL_BASE_RATE", 1.0), mock.patch.object(
        rules, "FATE_RITUAL_MAX_RATE", 1.0
    ):
        async for r in fn(handlers, player, event, item_id, count):
            out.append(r)
    return out


class TestFateExchangeCommand(unittest.IsolatedAsyncioTestCase):
    async def test_gold_to_points_succeeds(self):
        """灵石换点：这是原来直接 NameError 的那条路。"""
        player = _player(gold=GOLD_PER_FATE_POINT * 30)
        db = _FakeDB()
        h = _handlers(db)
        ev = _Ev()

        await _run(h, player, ev, item_id="灵石", count="10")

        self.assertEqual(len(ev.plain), 1, ev.plain)
        text = ev.plain[0]
        self.assertIn("仙缘点 +10", text)
        self.assertIn("当前仙缘点：10", text)
        self.assertEqual(player.gold, GOLD_PER_FATE_POINT * 20)
        self.assertEqual(player.fate_points, 10)
        self.assertIn("u1", db.saved, "改动必须落库")
        self.assertEqual(db.ext.system[fate_exchange_key("u1")], "10")

    async def test_gold_alias_works(self):
        """`/仙缘兑换 gold 5` 与中文写法等价。"""
        player = _player(gold=GOLD_PER_FATE_POINT * 5)
        ev = _Ev()
        await _run(_handlers(), player, ev, item_id="gold", count="5")
        self.assertIn("仙缘点 +5", ev.plain[0])

    async def test_zero_count_uses_all_available(self):
        """显式填 0 才按持币量算满；裸发命令默认只上一份祭品。"""
        player = _player(gold=GOLD_PER_FATE_POINT * 7)
        ev = _Ev()
        await _run(_handlers(), player, ev, item_id="灵石", count="0")
        self.assertIn("仙缘点 +7", ev.plain[0])

    async def test_blank_count_only_offers_once(self):
        """不填次数时只祭祀 1 次，避免误触烧光灵石。"""
        player = _player(gold=GOLD_PER_FATE_POINT * 7)
        ev = _Ev()
        await _run(_handlers(), player, ev, item_id="灵石", count="")
        self.assertIn("仙缘点 +1", ev.plain[0])
        self.assertEqual(player.gold, GOLD_PER_FATE_POINT * 6)

    async def test_quota_exhausted(self):
        player = _player(gold=GOLD_PER_FATE_POINT * 99)
        db = _FakeDB(used=FATE_EXCHANGE_MAX)
        ev = _Ev()
        await _run(_handlers(db), player, ev, item_id="灵石", count="1")
        self.assertIn("本轮已积满", ev.plain[0])
        self.assertEqual(player.gold, GOLD_PER_FATE_POINT * 99, "额度用完不该扣钱")

    async def test_not_enough_gold(self):
        player = _player(gold=100)
        ev = _Ev()
        await _run(_handlers(), player, ev, item_id="灵石", count="5")
        self.assertIn("灵石不足", ev.plain[0])

    async def test_bad_count_is_rejected(self):
        player = _player(gold=GOLD_PER_FATE_POINT * 5)
        ev = _Ev()
        await _run(_handlers(), player, ev, item_id="灵石", count="十个")
        self.assertIn("请输入祭祀次数", ev.plain[0])

    async def test_quota_is_capped_by_remaining(self):
        """要的比剩余额度多时按剩余额度成交。"""
        player = _player(gold=GOLD_PER_FATE_POINT * 50)
        db = _FakeDB(used=FATE_EXCHANGE_MAX - 3)
        ev = _Ev()
        await _run(_handlers(db), player, ev, item_id="灵石", count="20")
        self.assertIn("仙缘点 +3", ev.plain[0])
        self.assertEqual(player.fate_points, 3)

    async def test_index_path_still_buys(self):
        """`/仙缘兑换 3` 仍走商店购买，不被本次改动影响。"""
        mgr = _FakeReincMgr()
        player = _player()
        ev = _Ev()
        await _run(_handlers(mgr=mgr), player, ev, item_id="3")
        self.assertEqual(mgr.calls, [("u1", 3)])
        self.assertIn("已购买", ev.plain[0])

    async def test_index_rejects_non_numeric(self):
        ev = _Ev()
        await _run(_handlers(), _player(), ev, item_id="丹药")
        self.assertIn("请输入正确的商品编号", ev.plain[0])


class TestSignature(unittest.TestCase):
    def test_exchange_helper_takes_event(self):
        """直接盯签名：`_exchange_gold_for_points` 必须显式收 event。

        这个 bug 的形态就是「函数体用了 event，签名里没有」——行为测试能抓到，
        但签名断言更直白，防止有人把参数又「优化」掉。
        """
        import inspect

        sig = inspect.signature(
            handlers_mod.ReincarnationHandlers._exchange_gold_for_points
        )
        self.assertIn("event", sig.parameters)


if __name__ == "__main__":
    unittest.main()
