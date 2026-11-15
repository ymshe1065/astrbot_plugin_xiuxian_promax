# tests/test_auto_proxy_adventure.py
# 自动代理历练：单线续跑、路线 CD 自动闭关、双线交叉、奇遇自动选择与关闭隔离
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


class FakeEscortManager:
    def __init__(self, db, status):
        self.db = db
        self.status = status

    async def start_escort(self, player):
        from astrbot_plugin_xiuxian_promax.core.avatar_rules import total_slots

        await self.db.ext.set_user_busy(
            player.user_id, self.status.DELIVERING, int(time.time()) + 1800, {},
            max_slots=total_slots(player),
        )
        return True, "escort started", {}

    async def finish_escort(self, player):
        await self.db.ext.set_user_free(player.user_id, cd_type=self.status.DELIVERING)
        return True, "escort finished", {}


class FakeSectManager:
    async def perform_sect_task(self, user_id):
        return False, "sect not used"

    async def finish_sect_task(self, user_id):
        return False, "sect not used"


class FakeAdventureManager:
    """只实现代理需要的路线/CD/出发接口，避免依赖真实奖励结算。"""

    def __init__(self, db, status):
        self.db = db
        self.status = status
        self.calls = []
        self.routes = {
            "scout": {
                "key": "scout",
                "name": "巡山问道",
                "min_level": 0,
                "duration": 600,
                "fatigue_cooldown": 300,
                "product": "灵植",
                "risk": "低",
            },
            "herb_valley": {
                "key": "herb_valley",
                "name": "灵谷采药",
                "min_level": 2,
                "duration": 900,
                "fatigue_cooldown": 600,
                "product": "灵植",
                "risk": "中",
            },
            "ruin_edge": {
                "key": "ruin_edge",
                "name": "遗迹外围",
                "min_level": 5,
                "duration": 1200,
                "fatigue_cooldown": 900,
                "product": "矿材",
                "risk": "高",
            },
        }
        self.route_alias_index = {}
        for key, route in self.routes.items():
            self.route_alias_index[key.lower()] = key
            self.route_alias_index[route["name"].lower()] = key

    def resolve_route(self, token: str) -> str:
        if not token:
            return ""
        return self.route_alias_index.get(str(token).strip().lower(), "")

    def route_name(self, route_key: str) -> str:
        route = self.routes.get(route_key or "")
        return str((route or {}).get("name") or route_key or "历练")

    async def get_route_cooldown(self, user_id: str, route_key: str) -> int:
        if not route_key:
            return 0
        raw = await self.db.ext.get_system_config(f"adv_cd_{user_id}_{route_key}")
        return int(raw) if raw and str(raw).isdigit() else 0

    async def start_adventure(self, user_id: str, route_token: str, umo: str = ""):
        route_key = self.resolve_route(route_token) or route_token
        if await self.get_route_cooldown(user_id, route_key) > int(time.time()):
            return False, "⚠️ 该路线尚在休整中，请稍后再试。"
        from astrbot_plugin_xiuxian_promax.core.avatar_rules import total_slots

        player = await self.db.get_player_by_id(user_id)
        self.calls.append((user_id, route_key))
        route = self.routes.get(route_key) or {}
        await self.db.ext.set_user_busy(
            user_id,
            self.status.ADVENTURING,
            int(time.time()) + int(route.get("duration", 600) or 600),
            {"route_key": route_key, "umo": umo or ""},
            max_slots=total_slots(player) if player else 1,
        )
        return True, f"出发前往{route.get('name', route_key)}"


class FakeEncounterManager:
    def __init__(self, db):
        self.db = db
        self.choices = []
        self.events = {
            "test_event": {
                "key": "test_event",
                "options": [
                    {"label": "硬闯", "weight": "risk", "win": {"gold": 10000}, "lose": {"hp_pct": 0.2}},
                    {"label": "稳取", "weight": "safe", "win": {"gold": 3000}, "lose": None},
                    {"label": "离开", "weight": "leave", "win": None, "lose": None},
                ],
            }
        }

    async def resolve_choice(self, player, choice: int, auto: bool = False):
        self.choices.append(int(choice))
        await self.db.ext.set_system_config(f"encounter_{player.user_id}", "")
        return True, f"已选择 {choice}"


class TestAutoProxyAdventure(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiosqlite

        migration = _imp("data.migration")
        DataBase = _imp("data.data_manager").DataBase
        DatabaseExtended = _imp("data.database_extended").DatabaseExtended
        self.UserStatus = _imp("models_extended").UserStatus
        self.AutoProxyManager = _imp("managers.auto_proxy_manager").AutoProxyManager
        self.Player = _imp("models").Player

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await migration._create_all_tables_v2(self.conn)
        await migration._ensure_latest_columns(self.conn)

        self.db = DataBase(self.db_path)
        self.db.conn = self.conn
        self.db.ext = DatabaseExtended(self.conn)
        await self.db.create_player(self.Player(user_id="u1", level_index=5))

        self.adventure_mgr = FakeAdventureManager(self.db, self.UserStatus)
        self.encounter_mgr = FakeEncounterManager(self.db)
        self.notices = []
        self.bumps = []
        self.settle_calls = []

        async def notifier(origin, msg):
            self.notices.append((origin, msg))

        async def bump(uid, key, count):
            self.bumps.append((uid, key, count))
            return ""

        async def settler(player):
            self.settle_calls.append(player.user_id)
            await self.db.ext.set_user_free(player.user_id)
            return True, "🌟 出关成功"

        outer = self

        class Bounty:
            async def add_bounty_progress(self, player, tag, count=1):
                return False, ""

        self.manager = self.AutoProxyManager(
            self.db,
            FakeEscortManager(self.db, self.UserStatus),
            FakeSectManager(),
            notifier=notifier,
            title_bump=bump,
            bounty_mgr=Bounty(),
            adventure_mgr=self.adventure_mgr,
            encounter_mgr=self.encounter_mgr,
            cultivation_settler=settler,
            rift_mgr=type("FakeRiftManager", (), {})(),
        )

    async def asyncTearDown(self):
        await self.db.close()
        os.remove(self.db_path)

    async def _config(self):
        row = await self.db.ext.get_auto_proxy("u1")
        return row, self.manager._load_config(row or {})

    async def _set_route_cd(self, route_key: str, end_ts: int):
        await self.db.ext.set_system_config(f"adv_cd_u1_{route_key}", str(end_ts))

    async def test_enable_without_route_lists_choices(self):
        ok, msg = await self.manager.enable("u1", "umo://group", "历练")
        self.assertFalse(ok)
        self.assertIn("请选择历练代理路线", msg)
        self.assertIn("巡山问道", msg)
        self.assertIn("/自动代理 历练", msg)
        self.assertIsNone(await self.db.ext.get_auto_proxy("u1"))

    async def test_enable_single_route_starts_adventure(self):
        ok, msg = await self.manager.enable("u1", "umo://group", "历练 巡山问道")
        self.assertTrue(ok, msg)
        self.assertIn("单线续跑", msg)
        row, config = await self._config()
        self.assertEqual(row["enabled"], 1)
        self.assertTrue(config["adventure"]["enabled"])
        self.assertEqual(config["adventure"]["route"], "scout")
        self.assertEqual(config["adventure"]["mode"], "continue")
        cd = await self.db.ext.get_user_cd("u1")
        self.assertEqual(cd.type, self.UserStatus.ADVENTURING)
        self.assertEqual(self.adventure_mgr.calls, [("u1", "scout")])

    async def test_route_cd_triggers_seclusion_then_resumes(self):
        await self.manager.enable("u1", "umo://group", "历练 巡山问道")
        await self.db.ext.set_user_free("u1")
        end_ts = int(time.time()) + 600
        await self._set_route_cd("scout", end_ts)

        self.assertEqual(await self.manager.process_due(), 1)
        cd = await self.db.ext.get_user_cd("u1")
        self.assertEqual(cd.type, self.UserStatus.CULTIVATING)
        _row, config = await self._config()
        self.assertEqual(config["adventure"]["close_until"], end_ts)
        self.assertTrue(any("自动闭关" in msg for _origin, msg in self.notices))

        # 模拟闭关到点：CD 与 close_until 都过期，代理应结算闭关并继续原路线
        past = int(time.time()) - 5
        await self._set_route_cd("scout", past)
        row, config = await self._config()
        config["adventure"]["close_until"] = past
        await self.manager._write_state(
            "u1", True, "", "umo://group", "", config
        )

        self.assertEqual(await self.manager.process_due(), 1)
        self.assertEqual(self.settle_calls, ["u1"])
        self.assertEqual(self.bumps, [("u1", "cultivate_count", 1)])
        cd = await self.db.ext.get_user_cd("u1")
        self.assertEqual(cd.type, self.UserStatus.ADVENTURING)
        self.assertEqual(self.adventure_mgr.calls[-1], ("u1", "scout"))

    async def test_short_cd_waits_without_seclusion(self):
        await self.manager.enable("u1", "umo://group", "历练 巡山问道")
        await self.db.ext.set_user_free("u1")
        await self._set_route_cd("scout", int(time.time()) + 30)

        self.assertEqual(await self.manager.process_due(), 1)
        cd = await self.db.ext.get_user_cd("u1")
        self.assertEqual(cd.type, self.UserStatus.IDLE)
        _row, config = await self._config()
        self.assertEqual(config["adventure"]["note"], "wait_cd")

    async def test_alternate_switches_to_other_route_when_primary_on_cd(self):
        ok, msg = await self.manager.enable(
            "u1", "umo://group", "历练 巡山问道 灵谷采药"
        )
        self.assertTrue(ok, msg)
        self.assertIn("双线交叉", msg)
        await self.db.ext.set_user_free("u1")
        _row, config = await self._config()
        config["adventure"]["last_route"] = "scout"
        await self.manager._write_state("u1", True, "", "umo://group", "", config)
        await self._set_route_cd("scout", int(time.time()) + 600)

        self.adventure_mgr.calls.clear()
        self.assertEqual(await self.manager.process_due(), 1)
        self.assertEqual(self.adventure_mgr.calls, [("u1", "herb_valley")])
        cd = await self.db.ext.get_user_cd("u1")
        self.assertEqual(cd.type, self.UserStatus.ADVENTURING)

    async def test_encounter_smart_choice(self):
        await self.manager.enable("u1", "umo://group", "历练 巡山问道")
        await self.db.ext.set_user_free("u1")
        await self.db.ext.set_system_config(
            "encounter_u1",
            json.dumps({"key": "test_event", "expire": int(time.time()) + 120}),
        )
        await self.db.ext.set_user_busy("u1", self.UserStatus.ADVENTURING, int(time.time()) + 600, {})

        await self.manager.process_due()
        self.assertEqual(self.encounter_mgr.choices, [1])

    async def test_encounter_fixed_choice(self):
        ok, msg = await self.manager.enable(
            "u1", "umo://group", "历练 巡山问道 奇遇3"
        )
        self.assertTrue(ok, msg)
        self.assertIn("奇遇固定选 [3]", msg)
        _row, config = await self._config()
        self.assertEqual(config["adventure"]["encounter_choice"], 3)

        await self.db.ext.set_system_config(
            "encounter_u1",
            json.dumps({"key": "test_event", "expire": int(time.time()) + 120}),
        )
        await self.manager.process_due()
        self.assertEqual(self.encounter_mgr.choices, [3])

    async def test_merit_default_collects_all(self):
        adv = {"encounter_choice": 0}
        self.assertEqual(
            self.manager._choose_encounter_option({"key": "__merit__"}, adv), 2
        )
        adv = {"encounter_choice": 1}
        self.assertEqual(
            self.manager._choose_encounter_option({"key": "__merit__"}, adv), 1
        )

    async def test_disable_adventure_keeps_other_proxies(self):
        await self.manager.enable("u1", "umo://group", "历练 巡山问道")
        row, config = await self._config()
        config["farm"]["enabled"] = True
        await self.manager._write_state("u1", True, "", "umo://group", "", config)

        ok, msg = await self.manager.disable_adventure("u1")
        self.assertTrue(ok)
        self.assertIn("历练代理已关闭", msg)
        row, config = await self._config()
        self.assertEqual(row["enabled"], 1)
        self.assertFalse(config["adventure"]["enabled"])
        self.assertTrue(config["farm"]["enabled"])

    async def test_disable_adventure_settles_auto_seclusion(self):
        await self.manager.enable("u1", "umo://group", "历练 巡山问道")
        await self.db.ext.set_user_free("u1")
        end_ts = int(time.time()) + 600
        await self._set_route_cd("scout", end_ts)
        await self.manager.process_due()
        self.assertEqual(
            (await self.db.ext.get_user_cd("u1")).type, self.UserStatus.CULTIVATING
        )

        ok, msg = await self.manager.disable_adventure("u1")
        self.assertTrue(ok)
        self.assertIn("已为你结算本次自动闭关", msg)
        self.assertEqual(self.settle_calls, ["u1"])

    async def test_manual_adventure_entry_is_not_external_activity(self):
        await self.manager.enable("u1", "umo://group", "历练 巡山问道")
        self.assertFalse(await self.manager.note_external_activity_started("u1"))
        row, config = await self._config()
        self.assertEqual(row["enabled"], 1)
        self.assertTrue(config["adventure"]["enabled"])

    async def test_enable_primary_closes_adventure(self):
        await self.manager.enable("u1", "umo://group", "历练 巡山问道")
        await self.db.ext.set_user_free("u1")
        ok, msg = await self.manager.enable("u1", "umo://group", "押镖")
        self.assertIn("历练代理已关闭", msg)
        _row, config = await self._config()
        self.assertFalse(config["adventure"]["enabled"])

    async def test_enable_rift_rejected_while_adventure_on(self):
        await self.manager.enable("u1", "umo://group", "历练 巡山问道")
        await self.db.ext.set_user_free("u1")
        ok, msg = await self.manager.enable("u1", "umo://group", "秘境 1")
        self.assertFalse(ok)
        self.assertIn("进行中的代理任务", msg)

    async def test_status_shows_adventure(self):
        await self.manager.enable("u1", "umo://group", "历练 巡山问道 灵谷采药 奇遇2")
        text = await self.manager.status_text("u1")
        self.assertIn("历练代理：运行中", text)
        self.assertIn("巡山问道", text)
        self.assertIn("灵谷采药", text)
        self.assertIn("奇遇固定[2]", text)

    async def test_disable_handler_accepts_adventure(self):
        handlers = _imp("handlers.auto_proxy_handlers").AutoProxyHandlers(
            self.db, self.manager
        )
        await self.manager.enable("u1", "umo://group", "历练 巡山问道")

        class FakeEvent:
            unified_msg_origin = "umo://group"

            def get_sender_id(self):
                return "u1"

            def plain_result(self, text):
                return text

        results = []
        async for result in handlers.handle_disable(FakeEvent(), "历练"):
            results.append(result)
        self.assertIn("历练代理已关闭", results[0])

    async def test_web_panel_toggle_adventure_defaults_highest_route(self):
        proxy_panel = _imp("webchat_room.proxy_panel")

        class FakePlugin:
            db = self.db
            auto_proxy_mgr = self.manager
            adventure_mgr = self.adventure_mgr

        plugin = FakePlugin()
        data = await proxy_panel.toggle_proxy(
            plugin, "u1", "adventure", True, origin="umo://group"
        )
        self.assertTrue(data.get("ok"), data)
        _row, config = await self._config()
        self.assertEqual(config["adventure"]["route"], "ruin_edge")
        adv_task = next(t for t in data["tasks"] if t["key"] == "adventure")
        self.assertTrue(adv_task["enabled"])
        self.assertIn("遗迹外围", adv_task["detail"])

        data = await proxy_panel.toggle_proxy(
            plugin, "u1", "adventure", False, origin="umo://group"
        )
        self.assertTrue(data.get("ok"), data)
        _row, config = await self._config()
        self.assertFalse(config["adventure"]["enabled"])

    async def test_web_panel_adventure_mode_and_routes(self):
        proxy_panel = _imp("webchat_room.proxy_panel")

        class FakePlugin:
            db = self.db
            auto_proxy_mgr = self.manager
            adventure_mgr = self.adventure_mgr

        plugin = FakePlugin()
        data = await proxy_panel.toggle_proxy(
            plugin,
            "u1",
            "adventure",
            True,
            origin="umo://group",
            adv_mode="alternate",
            adv_route="scout",
            adv_route2="herb_valley",
        )
        self.assertTrue(data.get("ok"), data)
        _row, config = await self._config()
        self.assertTrue(config["adventure"]["enabled"])
        self.assertEqual(config["adventure"]["mode"], "alternate")
        self.assertEqual(config["adventure"]["route"], "scout")
        self.assertEqual(config["adventure"]["route2"], "herb_valley")
        self.assertEqual(data["adventure"]["route_name"], "巡山问道")
        self.assertEqual(data["adventure"]["route2_name"], "灵谷采药")
        self.assertTrue(
            any(route["key"] == "herb_valley" for route in data["adv_routes"])
        )

        # 切回单线：备用路线应清空，避免旧配置残留
        data = await proxy_panel.toggle_proxy(
            plugin,
            "u1",
            "adventure",
            True,
            origin="umo://group",
            adv_mode="continue",
            adv_route="scout",
            adv_route2="herb_valley",
        )
        self.assertTrue(data.get("ok"), data)
        _row, config = await self._config()
        self.assertEqual(config["adventure"]["mode"], "continue")
        self.assertEqual(config["adventure"]["route"], "scout")
        self.assertEqual(config["adventure"]["route2"], "")

        # 双线但只传主路线：自动补一条不同的备用路线
        data = await proxy_panel.toggle_proxy(
            plugin,
            "u1",
            "adventure",
            True,
            origin="umo://group",
            adv_mode="alternate",
            adv_route="scout",
        )
        self.assertTrue(data.get("ok"), data)
        _row, config = await self._config()
        self.assertEqual(config["adventure"]["mode"], "alternate")
        self.assertNotEqual(config["adventure"]["route2"], "scout")
        self.assertTrue(config["adventure"]["route2"])


    async def _set_avatars(self, count: int):
        import json as _json

        player = await self.db.get_player_by_id("u1")
        player.permanent_bonus = _json.dumps({"avatar_slots": int(count)})
        await self.db.update_player(player)

    async def test_adventure_starts_in_free_slot_while_player_cultivates(self):
        """玩家自己在闭关、化身为空时，历练代理应当用另一个槽位出发。"""
        await self._set_avatars(1)  # 2 个槽位
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.CULTIVATING, int(time.time()) + 3600, max_slots=2
        )
        ok, msg = await self.manager.enable("u1", "umo://group", "历练 巡山问道")
        self.assertTrue(ok, msg)
        self.assertEqual(self.adventure_mgr.calls, [("u1", "scout")])
        cult = await self.db.ext.get_active_cd("u1", self.UserStatus.CULTIVATING)
        self.assertIsNotNone(cult)
        adv = await self.db.ext.get_active_cd("u1", self.UserStatus.ADVENTURING)
        self.assertIsNotNone(adv)
        self.assertNotEqual(int(cult.slot), int(adv.slot))

    async def test_adventure_waits_when_all_slots_busy(self):
        """唯一槽位被玩家自己闭关占着时，历练代理等槽位而不是自行关闭。"""
        await self.db.ext.set_user_busy(
            "u1", self.UserStatus.CULTIVATING, int(time.time()) + 3600
        )
        ok, msg = await self.manager.enable("u1", "umo://group", "历练 巡山问道")
        self.assertTrue(ok, msg)
        self.assertIn("分身都在忙", msg)
        _row, config = await self._config()
        self.assertTrue(config["adventure"]["enabled"])
        self.assertEqual(self.adventure_mgr.calls, [])
        self.assertEqual(config["adventure"]["note"], "wait_slot")

        # 槽位空出来后下一轮自动出发
        await self.db.ext.set_user_free("u1", cd_type=self.UserStatus.CULTIVATING)
        await self.manager.process_due()
        self.assertEqual(self.adventure_mgr.calls, [("u1", "scout")])


if __name__ == "__main__":
    unittest.main()
