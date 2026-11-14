# tests/test_auto_proxy_multi_slot.py
# 身外化身之后：历练代理不能被「别的分身/本体在忙」挡住
#
# 回归背景（v3.45.16）：user_cd 改成 (user_id, slot) 复合主键后，
# _run_adventure_cycle 仍在用 first_busy_cd 判断「这个号忙不忙」。
# 本体闭关 + 分身在历练时，first_busy_cd 只会返回 slot 0 的闭关，
# 于是那趟历练走完代理就静默停在原地，永远不开双线的另一条。
import json
import os
import sys
import tempfile
import time
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


class SlotAdventureManager:
    """只实现代理需要的接口，但按真实多槽位语义占槽。"""

    def __init__(self, db, status, avatar_rules):
        self.db = db
        self.status = status
        self.avatar_rules = avatar_rules
        self.calls = []
        self.routes = {
            "scout": {"key": "scout", "name": "巡山问道", "min_level": 0,
                      "duration": 600, "fatigue_cooldown": 300},
            "herb_valley": {"key": "herb_valley", "name": "灵谷采药", "min_level": 0,
                            "duration": 900, "fatigue_cooldown": 600},
        }
        self.route_alias_index = {}
        for key, route in self.routes.items():
            self.route_alias_index[key] = key
            self.route_alias_index[route["name"]] = key

    def resolve_route(self, token: str) -> str:
        return self.route_alias_index.get(str(token or "").strip(), "")

    def route_name(self, route_key: str) -> str:
        return str((self.routes.get(route_key or "") or {}).get("name") or route_key or "历练")

    async def get_route_cooldown(self, user_id: str, route_key: str) -> int:
        raw = await self.db.ext.get_system_config(f"adv_cd_{user_id}_{route_key}")
        return int(raw) if raw and str(raw).isdigit() else 0

    async def start_adventure(self, user_id: str, route_token: str, umo: str = ""):
        route_key = self.resolve_route(route_token) or route_token
        if await self.get_route_cooldown(user_id, route_key) > int(time.time()):
            return False, "⚠️ 该路线尚在休整中，请稍后再试。"
        player = await self.db.get_player_by_id(user_id)
        total = self.avatar_rules.total_slots(player)
        route = self.routes.get(route_key) or {}
        started, slot, why = await self.db.ext.start_activity(
            user_id,
            self.status.ADVENTURING,
            int(time.time()) + int(route.get("duration", 600) or 600),
            {"route_key": route_key, "umo": umo or ""},
            max_slots=total,
        )
        if not started:
            if why == "no_free_slot":
                return False, f"❌ 分身都出去了（{total}/{total} 在忙），没人手做别的。"
            return False, "❌ 已经开始历练了。"
        self.calls.append((user_id, route_key, slot))
        return True, f"出发前往{route.get('name', route_key)}"

    async def finish_adventure(self, user_id: str):
        """把到点的历练结算掉，返回 (ok, msg, reward)。"""
        cd = await self.db.ext.get_active_cd(user_id, self.status.ADVENTURING)
        if not cd:
            return False, "❌ 你当前不在历练中！", None
        route_key = (cd.get_extra_data() or {}).get("route_key") or "scout"
        fatigue = int((self.routes.get(route_key) or {}).get("fatigue_cooldown", 0) or 0)
        await self.db.ext.release_activity(user_id, self.status.ADVENTURING)
        if fatigue:
            await self.db.ext.set_system_config(
                f"adv_cd_{user_id}_{route_key}", str(int(time.time()) + fatigue)
            )
        return True, f"🎉 历练完成 · {route_key}", {"route_key": route_key}


class TestAutoProxyMultiSlot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.UserStatus = _imp("models_extended").UserStatus
        self.AutoProxyManager = _imp("managers.auto_proxy_manager").AutoProxyManager
        self.Player = _imp("models").Player
        self.avatar_rules = _imp("core.avatar_rules")

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        self.adventure_mgr = SlotAdventureManager(
            self.db, self.UserStatus, self.avatar_rules
        )
        self.notices = []

        async def notifier(origin, msg, user_id=""):
            self.notices.append(msg)

        async def settler(player):
            await self.db.ext.release_activity(player.user_id, self.UserStatus.CULTIVATING)
            return True, "🌟 出关成功"

        self.manager = self.AutoProxyManager(
            self.db,
            type("Escort", (), {"config_manager": None})(),
            type("Sect", (), {})(),
            notifier=notifier,
            adventure_mgr=self.adventure_mgr,
            cultivation_settler=settler,
        )

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _new_player(self, avatars: int = 0) -> None:
        player = self.Player(user_id="u1", user_name="云无月", level_index=9)
        player.permanent_bonus = json.dumps({"avatar_slots": int(avatars)})
        await self.db.create_player(player)

    async def _busy_elsewhere(self, status: int, slot_type: int = None) -> int:
        """让本体去闭关（占一个槽），模拟「本体闭关 + 分身历练」。"""
        started, slot, why = await self.db.ext.start_activity(
            "u1", status, 0, max_slots=self.avatar_rules.total_slots(
                await self.db.get_player_by_id("u1")
            )
        )
        self.assertTrue(started, why)
        return slot

    async def test_alternate_continues_after_body_seclusion(self):
        """本体闭关占着 0 号槽，历练走完后仍要自动开双线的另一条。"""
        await self._new_player(avatars=3)  # 4 槽
        await self._busy_elsewhere(self.UserStatus.CULTIVATING)

        ok, msg = await self.manager.enable("u1", "umo://g", "历练 巡山问道 灵谷采药")
        self.assertTrue(ok, msg)
        self.assertEqual([c[1] for c in self.adventure_mgr.calls], ["scout"])
        self.assertEqual(self.adventure_mgr.calls[0][2], 1)  # 历练落在 1 号槽

        ok, _msg, data = await self.adventure_mgr.finish_adventure("u1")
        self.assertTrue(ok)
        self.assertEqual(data["route_key"], "scout")

        self.adventure_mgr.calls.clear()
        await self.manager.process_due()
        self.assertEqual(
            [c[1] for c in self.adventure_mgr.calls], ["herb_valley"],
            "本体还在闭关，但分身空出来了，双线应该换到另一条继续",
        )
        row = await self.db.ext.get_auto_proxy("u1")
        cfg = self.manager._load_config(row)
        self.assertTrue(cfg["adventure"]["enabled"])
        self.assertEqual(cfg["adventure"]["last_route"], "herb_valley")

    async def test_single_slot_waits_without_disabling(self):
        """单槽玩家在闭关：不出发，但要保留代理，出关后自动接上。"""
        await self._new_player(avatars=0)  # 1 槽
        await self._busy_elsewhere(self.UserStatus.CULTIVATING)

        ok, msg = await self.manager.enable("u1", "umo://g", "历练 巡山问道")
        self.assertTrue(ok, msg)
        self.assertIn("结束后会自动开始历练", msg)
        self.assertEqual(self.adventure_mgr.calls, [])
        row = await self.db.ext.get_auto_proxy("u1")
        cfg = self.manager._load_config(row)
        self.assertTrue(cfg["adventure"]["enabled"])

        # 出关后下一个轮询就出发
        await self.db.ext.release_activity("u1", self.UserStatus.CULTIVATING)
        await self.manager.process_due()
        self.assertEqual([c[1] for c in self.adventure_mgr.calls], ["scout"])

    async def test_all_slots_busy_keeps_proxy_alive(self):
        """分身全满：只提示等空位，不能把代理关掉。"""
        await self._new_player(avatars=1)  # 2 槽
        started, _slot, why = await self.db.ext.start_activity(
            "u1", self.UserStatus.CULTIVATING, 0, max_slots=2
        )
        self.assertTrue(started, why)
        started, _slot, why = await self.db.ext.start_activity(
            "u1", self.UserStatus.DELIVERING, 0, max_slots=2
        )
        self.assertTrue(started, why)

        ok, msg = await self.manager.enable("u1", "umo://g", "历练 巡山问道")
        self.assertTrue(ok, msg)
        self.assertIn("分身都出去了", msg)
        self.assertEqual(self.adventure_mgr.calls, [])

        row = await self.db.ext.get_auto_proxy("u1")
        cfg = self.manager._load_config(row)
        self.assertTrue(cfg["adventure"]["enabled"], "槽位不够不是配置错误，不能关代理")
        self.assertEqual(cfg["adventure"]["note"], "wait_slot")

        # 腾出一个槽位就该自动出发
        await self.db.ext.release_activity("u1", self.UserStatus.DELIVERING)
        await self.manager.process_due()
        self.assertEqual([c[1] for c in self.adventure_mgr.calls], ["scout"])

    async def test_seclusion_not_started_when_no_slot(self):
        """路线 CD 要闭关，但没空槽时不能假装闭着，更不能写脏玩家状态。"""
        await self._new_player(avatars=0)  # 1 槽
        started, _slot, why = await self.db.ext.start_activity(
            "u1", self.UserStatus.DELIVERING, 0, max_slots=1
        )
        self.assertTrue(started, why)

        player = await self.db.get_player_by_id("u1")
        before = player.state
        ok, msg = await self.manager._start_adv_seclusion(
            "u1", player, int(time.time()) + 600, "scout"
        )
        self.assertFalse(ok, msg)
        self.assertIn("没有空闲分身", msg)
        fresh = await self.db.get_player_by_id("u1")
        self.assertEqual(fresh.state, before, "闭关没抢到槽，不应该把玩家状态改成修炼中")


if __name__ == "__main__":
    unittest.main()
